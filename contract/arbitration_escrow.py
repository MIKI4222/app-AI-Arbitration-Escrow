# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
import json
from genlayer import *

# ---------------------------------------------------------------------------
# Time locks
#
# v3 used `gl.message.block_number`, which does not exist in the py-genlayer
# SDK: every write that touched it (submit_client_evidence,
# cancel_stalled_dispute, resolve_stalled_arbitration) raised AttributeError
# and the transaction came back as ACCEPTED (ERROR) / FINALIZED (ERROR).
#
# Time locks are now expressed in seconds and read from the consensus clock
# (`gl.message_raw["datetime"]`, an ISO-8601 string; the same value the test
# harness overrides with
# `genvm_datetime`), so they are deterministic across validators AND
# testable via time travel.
# ---------------------------------------------------------------------------

# Client may cancel a stalled dispute (freelancer never responded) after 7 days.
DISPUTE_CANCEL_DELAY_SECONDS = 7 * 24 * 60 * 60

# Bounded recovery: once both sides submitted evidence but arbitration could
# never complete (evidence pages unreachable, validators unable to agree on a
# valid winner), anyone may force a conservative resolution after 3 days.
ARBITRATION_RECOVERY_DELAY_SECONDS = 3 * 24 * 60 * 60

VALID_WINNERS = ("client", "freelancer")

# Sentinel returned by the arbitration prompt when the model produced a value
# outside VALID_WINNERS. It travels through consensus as data and is rejected
# by an assert afterwards, so an invalid verdict can never trigger a payout.
UNRESOLVED = "unresolved"

STATUS_FUNDED = "FUNDED"
STATUS_DISPUTED = "DISPUTED"
STATUS_RELEASED = "RELEASED_TO_FREELANCER"
STATUS_FOR_CLIENT = "RESOLVED_FOR_CLIENT"
STATUS_FOR_FREELANCER = "RESOLVED_FOR_FREELANCER"


_MONTH_START_DAYS = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)


def _is_leap_year(year: int) -> bool:
    if year % 400 == 0:
        return True
    if year % 100 == 0:
        return False
    return year % 4 == 0


def _days_from_epoch(year: int, month: int, day: int) -> int:
    """Days between 1970-01-01 and the given civil date."""
    days = (year - 1970) * 365
    days += (year - 1969) // 4
    days -= (year - 1901) // 100
    days += (year - 1601) // 400
    days += _MONTH_START_DAYS[month - 1]
    if month > 2 and _is_leap_year(year):
        days += 1
    return days + day - 1


def _parse_iso_seconds(text) -> int | None:
    """
    Epoch seconds from an ISO-8601 stamp such as "2026-09-04T19:12:38Z".

    Plain string and integer operations only: no imports, and nothing that
    inspects an object, since the deploy-time validator rejected both.
    """
    cleaned = str(text).strip().replace(" ", "T")
    if cleaned.endswith("Z") or cleaned.endswith("z"):
        cleaned = cleaned[:-1]

    parts = cleaned.split("T")
    if len(parts) != 2:
        return None

    body = parts[1]
    offset = 0
    for sign in ("+", "-"):
        pieces = body.split(sign)
        if len(pieces) == 2:
            tail = pieces[1].replace(":", "")
            if len(tail) == 4 and tail.isdigit():
                offset = int(tail[:2]) * 3600 + int(tail[2:]) * 60
                if sign == "-":
                    offset = -offset
                body = pieces[0]
            break

    date_bits = parts[0].split("-")
    time_bits = body.split(".")[0].split(":")
    while len(time_bits) < 3:
        time_bits.append("0")
    if len(date_bits) != 3 or len(time_bits) != 3:
        return None
    for bit in date_bits + time_bits:
        if len(bit) == 0 or not bit.isdigit():
            return None

    year = int(date_bits[0])
    month = int(date_bits[1])
    day = int(date_bits[2])
    if month < 1 or month > 12 or day < 1 or day > 31:
        return None

    seconds = _days_from_epoch(year, month, day) * 86400
    seconds += int(time_bits[0]) * 3600
    seconds += int(time_bits[1]) * 60
    seconds += int(time_bits[2])
    return seconds - offset


def _to_timestamp(value) -> int | None:
    """Best-effort conversion of a clock-ish value into epoch seconds."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        return _parse_iso_seconds(value)
    timestamp = getattr(value, "timestamp", None)
    if callable(timestamp):
        try:
            return int(timestamp())
        except Exception:
            return None
    return None


def _resolve_clock() -> tuple:
    """
    Locate the deterministic consensus clock, returning (seconds, source).

    SDK builds expose it under different names, and a missing attribute must
    never blow up a write the way `gl.message.block_number` did — so this
    returns (None, "unavailable") instead of raising.
    """
    raw = getattr(gl, "message_raw", None)
    if raw is None:
        return None, "unavailable:no-message-raw"
    if not isinstance(raw, dict):
        return None, "unavailable:raw-not-a-dict"

    value = raw.get("datetime")
    if value is None:
        return None, "unavailable:no-datetime-key"

    seconds = _to_timestamp(value)
    if seconds is None:
        return None, "unavailable:datetime-unparsed"
    return seconds, "gl.message_raw[datetime]"


def _now_ts(required: bool = False) -> int:
    """
    Consensus timestamp in seconds, or 0 when this build exposes no clock.

    Only the time-locked escape hatches pass `required=True`: creating,
    releasing, disputing and arbitrating a deal must keep working even if the
    clock is unavailable, so a missing attribute can never break the whole
    contract again.
    """
    seconds, _ = _resolve_clock()
    if seconds is None:
        assert not required, (
            "Consensus clock unavailable in this GenVM build, so time-locked "
            "actions cannot be verified — check clock_source in get_timelocks()"
        )
        return 0
    return seconds


def _norm_address(value: str) -> str:
    """Case-insensitive address comparison (v3 compared checksummed strings)."""
    return str(value).strip().lower()


# Payouts go to wallets (EOAs), not to other intelligent contracts, so they
# must travel as EXTERNAL messages (IC -> Chain Layer) rather than internal
# IC -> IC ones. `gl.get_contract_at(...).emit_transfer(...)` emits an
# INTERNAL message: it is delivered as a call to an intelligent contract and
# silently goes nowhere when the recipient is a plain wallet.
#
# The documented way to send value to an EOA is an empty EVM contract
# interface used purely as a transfer target.
# Note: external messages are always emitted on="finalized"; on="accepted"
# is not supported for them.
@gl.evm.contract_interface
class _Wallet:
    """Empty EVM interface used only as a value-transfer target for wallets."""

    class View:
        pass

    class Write:
        pass


class ArbitrationEscrow(gl.Contract):
    """
    AI Arbitration Escrow — v4.

    Deal lifecycle:
        FUNDED
          -> release_funds (client)          -> RELEASED_TO_FREELANCER
          -> submit_client_evidence (client) -> DISPUTED
               -> submit_freelancer_evidence
               -> resolve_dispute (anyone)   -> RESOLVED_FOR_CLIENT
                                             -> RESOLVED_FOR_FREELANCER
                  (a verdict outside ("client", "freelancer") is rejected and
                   the whole transaction reverts: no state change, no payout)
               -> cancel_stalled_dispute     -> RESOLVED_FOR_CLIENT
                  (client only, after DISPUTE_CANCEL_DELAY_SECONDS, only while
                   the freelancer never responded)
               -> resolve_stalled_arbitration -> RESOLVED_FOR_CLIENT
                  (anyone, after ARBITRATION_RECOVERY_DELAY_SECONDS, only when
                   both sides submitted evidence but arbitration never
                   completed — bounded recovery so funds are never stuck)
    """

    deals: DynArray[str]

    def __init__(self):
        pass

    # -- storage helpers ----------------------------------------------------

    def _load(self, deal_id: int) -> dict:
        deal_id = int(deal_id)
        assert 0 <= deal_id < len(self.deals), "Invalid deal ID"
        deal = json.loads(self.deals[deal_id])
        # Backfill fields for deals written by older contract versions.
        deal.setdefault("dispute_start_time", 0)
        deal.setdefault("freelancer_evidence_time", 0)
        deal.setdefault("paid", False)
        return deal

    def _store(self, deal_id: int, deal: dict) -> None:
        self.deals[int(deal_id)] = json.dumps(deal, sort_keys=True)

    def _settle(self, deal_id: int, deal: dict, status: str, recipient: str, reasoning: str) -> None:
        """
        Persist the terminal state first, then pay out exactly once.

        `paid` makes a payout non-repeatable even if a deal record is ever
        re-entered, and a zero amount is never sent to the transfer
        primitive (which would fail the transaction).
        """
        assert not deal["paid"], "Deal has already been paid out"
        amount = int(deal["amount"])
        assert amount > 0, "Deal has no custodied amount to pay out"
        deal["status"] = status
        deal["resolution_reasoning"] = reasoning
        deal["paid"] = True
        deal["resolved_time"] = _now_ts()
        self._store(deal_id, deal)
        # External message to a wallet. See the _Wallet note above for why
        # `gl.get_contract_at(...)` is wrong here.
        _Wallet(Address(recipient)).emit_transfer(value=u256(amount))

    # -- deal creation ------------------------------------------------------

    @gl.public.write.payable
    def create_deal(self, freelancer: str, description: str, amount: int) -> None:
        """Open a new escrow deal and deposit the agreed amount into custody."""
        assert len(freelancer.strip()) > 0, "Freelancer address cannot be empty"
        assert len(description.strip()) > 0, "Description cannot be empty"
        amount = int(amount)
        assert amount > 0, "Amount must be greater than zero"

        # gl.message.value is None when no value is attached.
        deposit = int(gl.message.value or 0)
        assert deposit == amount, (
            f"Sent value must exactly match the stated deal amount "
            f"(sent {deposit}, stated {amount})"
        )

        client = _norm_address(gl.message.sender_address)
        freelancer = _norm_address(freelancer)
        assert freelancer != client, "Client and freelancer must be different accounts"

        now = _now_ts()
        deal = {
            "id": len(self.deals),
            "client": client,
            "freelancer": freelancer,
            "description": description.strip(),
            # Amount is taken from the verified deposit, so the recorded value
            # can never exceed what the contract actually custodies.
            "amount": deposit,
            "status": STATUS_FUNDED,
            "client_evidence_url": "",
            "client_claim": "",
            "freelancer_evidence_url": "",
            "freelancer_claim": "",
            "resolution_reasoning": "",
            "created_time": now,
            "dispute_start_time": 0,
            "freelancer_evidence_time": 0,
            "resolved_time": 0,
            "paid": False,
        }
        self.deals.append(json.dumps(deal, sort_keys=True))

    # -- happy path ---------------------------------------------------------

    @gl.public.write
    def release_funds(self, deal_id: int) -> None:
        """Client voluntarily releases the deposit to the freelancer."""
        deal = self._load(deal_id)
        assert deal["status"] == STATUS_FUNDED, (
            f"Deal is not in a releasable state (current status: {deal['status']})"
        )
        assert _norm_address(gl.message.sender_address) == deal["client"], \
            "Only the client can release funds without a dispute"
        self._settle(
            deal_id,
            deal,
            STATUS_RELEASED,
            deal["freelancer"],
            "Client voluntarily released the deposit to the freelancer.",
        )

    # -- dispute ------------------------------------------------------------

    @gl.public.write
    def submit_client_evidence(self, deal_id: int, evidence_url: str, claim: str) -> None:
        """Client raises a dispute by submitting evidence."""
        deal = self._load(deal_id)
        assert deal["status"] == STATUS_FUNDED, (
            f"Deal is not open for evidence (current status: {deal['status']})"
        )
        assert _norm_address(gl.message.sender_address) == deal["client"], \
            "Only the client can submit client-side evidence"
        assert len(evidence_url.strip()) > 0, "Evidence URL cannot be empty"
        assert len(claim.strip()) > 0, "Claim cannot be empty"
        deal["client_evidence_url"] = evidence_url.strip()
        deal["client_claim"] = claim.strip()
        deal["status"] = STATUS_DISPUTED
        deal["dispute_start_time"] = _now_ts()
        self._store(deal_id, deal)

    @gl.public.write
    def submit_freelancer_evidence(self, deal_id: int, evidence_url: str, claim: str) -> None:
        """Freelancer submits counter-evidence to contest the dispute."""
        deal = self._load(deal_id)
        assert deal["status"] == STATUS_DISPUTED, "A dispute must be raised first"
        assert _norm_address(gl.message.sender_address) == deal["freelancer"], \
            "Only the freelancer can submit freelancer-side evidence"
        assert len(evidence_url.strip()) > 0, "Evidence URL cannot be empty"
        assert len(claim.strip()) > 0, "Claim cannot be empty"
        deal["freelancer_evidence_url"] = evidence_url.strip()
        deal["freelancer_claim"] = claim.strip()
        deal["freelancer_evidence_time"] = _now_ts()
        self._store(deal_id, deal)

    @gl.public.write
    def resolve_dispute(self, deal_id: int) -> None:
        """AI resolution, callable by anyone once both sides submitted evidence."""
        deal = self._load(deal_id)
        assert deal["status"] == STATUS_DISPUTED, "Deal is not under dispute"
        assert deal["freelancer_evidence_url"] != "", \
            "Both sides must submit evidence before AI resolution"

        client_claim = deal["client_claim"]
        client_url = deal["client_evidence_url"]
        freelancer_claim = deal["freelancer_claim"]
        freelancer_url = deal["freelancer_evidence_url"]
        description = deal["description"]

        def verify_dispute() -> str:
            client_content = gl.nondet.web.render(client_url, mode="text")[:6000]
            freelancer_content = gl.nondet.web.render(freelancer_url, mode="text")[:6000]
            prompt = f"""You are an impartial arbitrator resolving a dispute between a client and
a freelancer over a work agreement.

AGREEMENT DESCRIPTION:
{description}

CLIENT'S CLAIM:
{client_claim}
CLIENT'S EVIDENCE (fetched from {client_url}):
{client_content}

FREELANCER'S CLAIM:
{freelancer_claim}
FREELANCER'S EVIDENCE (fetched from {freelancer_url}):
{freelancer_content}

Decide, based ONLY on the evidence above, whether the agreed work was
genuinely delivered as described. Be conservative: only rule in favor of
the freelancer if the evidence clearly supports that the work was
completed as agreed. Otherwise rule in favor of the client.

Return ONLY valid JSON in exactly this structure:
{{"winner": "client" or "freelancer", "reasoning": "one or two short sentences"}}"""
            result = gl.nondet.exec_prompt(prompt)
            result = result.replace("```json", "").replace("```", "").strip()

            # Never raise inside the non-deterministic block: an exception here
            # aborts the equivalence principle itself instead of producing a
            # comparable value. Malformed output is normalised to UNRESOLVED
            # and rejected by the assert below, outside consensus.
            winner = UNRESOLVED
            reasoning = ""
            try:
                parsed = json.loads(result)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                candidate = parsed.get("winner")
                if isinstance(candidate, str) and candidate in VALID_WINNERS:
                    winner = candidate
                raw_reasoning = parsed.get("reasoning", "")
                reasoning = raw_reasoning if isinstance(raw_reasoning, str) else ""

            return json.dumps({"winner": winner, "reasoning": reasoning}, sort_keys=True)

        consensus_result = gl.eq_principle.prompt_comparative(
            verify_dispute,
            principle="The winner field must be exactly the same.",
        )
        parsed_result = json.loads(consensus_result)
        winner = parsed_result.get("winner")

        # Any value outside the allowed winners reverts the transaction: the
        # deal stays DISPUTED, nothing is paid, and it can be retried or fall
        # through to resolve_stalled_arbitration.
        assert winner in VALID_WINNERS, \
            "Arbitration returned an invalid winner value; resolution rejected"

        if winner == "freelancer":
            status = STATUS_FOR_FREELANCER
            recipient = deal["freelancer"]
        else:
            status = STATUS_FOR_CLIENT
            recipient = deal["client"]
        reasoning = parsed_result.get("reasoning", "")
        self._settle(deal_id, deal, status, recipient, reasoning if isinstance(reasoning, str) else "")

    # -- bounded escape hatches --------------------------------------------

    @gl.public.write
    def cancel_stalled_dispute(self, deal_id: int) -> None:
        """Client escape from a dispute the freelancer never answered."""
        deal = self._load(deal_id)
        assert deal["status"] == STATUS_DISPUTED, "Deal is not under dispute"
        assert deal["freelancer_evidence_url"] == "", \
            "Freelancer already submitted evidence; use resolve_dispute instead"
        assert _norm_address(gl.message.sender_address) == deal["client"], \
            "Only the client can cancel a stalled dispute"
        started = int(deal["dispute_start_time"])
        assert started > 0, \
            "Dispute has no recorded start time, so the delay cannot be verified"
        elapsed = _now_ts(required=True) - started
        assert elapsed >= DISPUTE_CANCEL_DELAY_SECONDS, (
            f"Dispute delay not yet elapsed: {elapsed}s of "
            f"{DISPUTE_CANCEL_DELAY_SECONDS}s have passed"
        )
        self._settle(
            deal_id,
            deal,
            STATUS_FOR_CLIENT,
            deal["client"],
            "Freelancer did not submit counter-evidence within the required "
            f"{DISPUTE_CANCEL_DELAY_SECONDS // 86400}-day window. "
            "Deal resolved in favour of the client by default.",
        )

    @gl.public.write
    def resolve_stalled_arbitration(self, deal_id: int) -> None:
        """
        Bounded recovery when both sides submitted evidence but arbitration
        could not complete (evidence unreachable, or no agreed valid winner).
        Callable by anyone after the recovery delay; resolves conservatively
        for the client so the deposit is never permanently stuck.
        """
        deal = self._load(deal_id)
        assert deal["status"] == STATUS_DISPUTED, "Deal is not under dispute"
        assert deal["freelancer_evidence_url"] != "", \
            "Freelancer has not submitted evidence; use cancel_stalled_dispute instead"
        submitted = int(deal["freelancer_evidence_time"])
        assert submitted > 0, \
            "Freelancer evidence has no recorded time, so the delay cannot be verified"
        elapsed = _now_ts(required=True) - submitted
        assert elapsed >= ARBITRATION_RECOVERY_DELAY_SECONDS, (
            f"Arbitration recovery delay not yet elapsed: {elapsed}s of "
            f"{ARBITRATION_RECOVERY_DELAY_SECONDS}s have passed"
        )
        self._settle(
            deal_id,
            deal,
            STATUS_FOR_CLIENT,
            deal["client"],
            "AI arbitration could not reach a valid consensus (evidence "
            "unreachable or no agreed winner) within the recovery window. "
            "Deal resolved in favour of the client by default.",
        )

    # -- views --------------------------------------------------------------

    @gl.public.view
    def get_deal(self, deal_id: int) -> str:
        deal_id = int(deal_id)
        assert 0 <= deal_id < len(self.deals), "Invalid deal ID"
        return self.deals[deal_id]

    @gl.public.view
    def get_all_deals(self) -> list[str]:
        return list(self.deals)

    @gl.public.view
    def total_deals(self) -> int:
        return len(self.deals)

    @gl.public.view
    def get_timelocks(self) -> str:
        """
        Delays in seconds, so the UI can show accurate eligibility times.

        Also reports the clock, which doubles as a health check: `now` of 0 or
        a `clock_source` starting with "unavailable" means time-locked actions
        cannot be verified on this deployment.
        """
        seconds, source = _resolve_clock()
        return json.dumps(
            {
                "dispute_cancel_delay_seconds": DISPUTE_CANCEL_DELAY_SECONDS,
                "arbitration_recovery_delay_seconds": ARBITRATION_RECOVERY_DELAY_SECONDS,
                "valid_winners": list(VALID_WINNERS),
                "now": seconds if seconds is not None else 0,
                "clock_source": source,
            },
            sort_keys=True,
        )
