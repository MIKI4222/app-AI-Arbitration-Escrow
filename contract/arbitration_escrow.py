# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
import json
from genlayer import *

# Minimum blocks that must pass after dispute is raised
# before the client can cancel a stalled dispute.
# ~50 000 blocks ≈ 7 days at ~12 s/block on Bradbury Testnet.
DISPUTE_CANCEL_DELAY_BLOCKS = 50_000


class ArbitrationEscrow(gl.Contract):
    """
    AI Arbitration Escrow — v3.

    Amount is recorded on deal creation. On resolution the full amount
    is transferred to the winner via emit_transfer; on a voluntary release
    it is transferred to the freelancer. A stalled dispute (freelancer
    never submits evidence) can only be cancelled by the client after
    DISPUTE_CANCEL_DELAY_BLOCKS blocks have elapsed, ensuring a genuine
    time-lock rather than an instant escape hatch.

    Deal lifecycle:
        FUNDED
          -> release_funds (client)          -> RELEASED_TO_FREELANCER
          -> submit_client_evidence (client) -> DISPUTED
               -> submit_freelancer_evidence
               -> resolve_dispute (anyone)   -> RESOLVED_FOR_CLIENT
                                             -> RESOLVED_FOR_FREELANCER
               -> cancel_stalled_dispute     -> RESOLVED_FOR_CLIENT
                 (client, after delay, only if freelancer never responded)
    """

    deals: DynArray[str]

    def __init__(self):
        pass

    @gl.public.write.payable
    def create_deal(self, freelancer: str, description: str, amount: int) -> None:
        """Open a new escrow deal and deposit the agreed amount into custody."""
        assert len(freelancer) > 0, "Freelancer address cannot be empty"
        assert len(description) > 0, "Description cannot be empty"
        assert amount > 0, "Amount must be greater than zero"
        assert gl.message.value == amount, \
            "Sent value must exactly match the stated deal amount"
        deal = {
            "id": len(self.deals),
            "client": str(gl.message.sender_address),
            "freelancer": freelancer,
            "description": description,
            "amount": amount,
            "status": "FUNDED",
            "client_evidence_url": "",
            "client_claim": "",
            "freelancer_evidence_url": "",
            "freelancer_claim": "",
            "resolution_reasoning": "",
            "dispute_start_block": 0,
        }
        self.deals.append(json.dumps(deal, sort_keys=True))

    def _payout(self, recipient: str, amount: int) -> None:
        """Send custodied GEN to a deal participant via the SDK transfer primitive."""
        gl.ContractAt(Address(recipient)).emit_transfer(value=amount)

    @gl.public.write
    def release_funds(self, deal_id: int) -> None:
        """Client voluntarily releases the deposit to the freelancer."""
        deal = json.loads(self.deals[deal_id])
        assert deal["status"] == "FUNDED", "Deal is not in a releasable state"
        assert str(gl.message.sender_address) == deal["client"], \
            "Only the client can release funds without a dispute"
        deal["status"] = "RELEASED_TO_FREELANCER"
        self.deals[deal_id] = json.dumps(deal, sort_keys=True)
        self._payout(deal["freelancer"], deal["amount"])

    @gl.public.write
    def submit_client_evidence(self, deal_id: int, evidence_url: str, claim: str) -> None:
        """Client raises a dispute by submitting evidence."""
        deal = json.loads(self.deals[deal_id])
        assert deal["status"] == "FUNDED", "Deal is not open for evidence"
        assert str(gl.message.sender_address) == deal["client"], \
            "Only the client can submit client-side evidence"
        deal["client_evidence_url"] = evidence_url
        deal["client_claim"] = claim
        deal["status"] = "DISPUTED"
        deal["dispute_start_block"] = gl.message.block_number
        self.deals[deal_id] = json.dumps(deal, sort_keys=True)

    @gl.public.write
    def submit_freelancer_evidence(self, deal_id: int, evidence_url: str, claim: str) -> None:
        """Freelancer submits counter-evidence to contest the dispute."""
        deal = json.loads(self.deals[deal_id])
        assert deal["status"] == "DISPUTED", "A dispute must be raised first"
        assert str(gl.message.sender_address) == deal["freelancer"], \
            "Only the freelancer can submit freelancer-side evidence"
        deal["freelancer_evidence_url"] = evidence_url
        deal["freelancer_claim"] = claim
        self.deals[deal_id] = json.dumps(deal, sort_keys=True)

    @gl.public.write
    def resolve_dispute(self, deal_id: int) -> None:
        """Anyone can trigger AI resolution once both sides have submitted evidence."""
        deal = json.loads(self.deals[deal_id])
        assert deal["status"] == "DISPUTED", "Deal is not under dispute"
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
            parsed = json.loads(result)
            normalized = {
                "winner": parsed.get("winner", "client"),
                "reasoning": parsed.get("reasoning", ""),
            }
            return json.dumps(normalized, sort_keys=True)

        consensus_result = gl.eq_principle.prompt_comparative(
            verify_dispute,
            principle="The winner field must be exactly the same.",
        )
        parsed_result = json.loads(consensus_result)
        winner = parsed_result.get("winner", "client")
        deal["status"] = (
            "RESOLVED_FOR_FREELANCER" if winner == "freelancer" else "RESOLVED_FOR_CLIENT"
        )
        deal["resolution_reasoning"] = parsed_result.get("reasoning", "")
        self.deals[deal_id] = json.dumps(deal, sort_keys=True)
        recipient = deal["freelancer"] if winner == "freelancer" else deal["client"]
        self._payout(recipient, deal["amount"])

    @gl.public.write
    def cancel_stalled_dispute(self, deal_id: int) -> None:
        """
        Bounded escape from the DISPUTED state with enforced block delay.
        The client may cancel only after DISPUTE_CANCEL_DELAY_BLOCKS blocks
        have elapsed since the dispute was opened.
        """
        deal = json.loads(self.deals[deal_id])
        assert deal["status"] == "DISPUTED", "Deal is not under dispute"
        assert deal["freelancer_evidence_url"] == "", \
            "Freelancer already submitted evidence; use resolve_dispute instead"
        assert str(gl.message.sender_address) == deal["client"], \
            "Only the client can cancel a stalled dispute"
        blocks_elapsed = gl.message.block_number - deal["dispute_start_block"]
        assert blocks_elapsed >= DISPUTE_CANCEL_DELAY_BLOCKS, (
            f"Dispute delay not yet elapsed: {blocks_elapsed} of "
            f"{DISPUTE_CANCEL_DELAY_BLOCKS} blocks have passed"
        )
        deal["status"] = "RESOLVED_FOR_CLIENT"
        deal["resolution_reasoning"] = (
            "Freelancer did not submit counter-evidence within the required "
            f"{DISPUTE_CANCEL_DELAY_BLOCKS}-block window. "
            "Deal resolved in favour of the client by default."
        )
        self.deals[deal_id] = json.dumps(deal, sort_keys=True)
        self._payout(deal["client"], deal["amount"])

    @gl.public.view
    def get_deal(self, deal_id: int) -> str:
        assert 0 <= deal_id < len(self.deals), "Invalid deal ID"
        return self.deals[deal_id]

    @gl.public.view
    def get_all_deals(self) -> list[str]:
        return list(self.deals)

    @gl.public.view
    def total_deals(self) -> int:
        return len(self.deals)
