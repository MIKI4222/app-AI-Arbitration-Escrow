"""
Focused tests for arbitration, invalid-verdict rejection and both bounded
recovery paths.

Run with:
    gltest tests/direct/test_arbitration_and_recovery.py

Time travel
-----------
v3 time-locked on `gl.message.block_number` (which does not exist in the SDK
and made every write revert) and there is no block-advancement helper in
genlayer-test. The contract now reads the consensus clock, which the harness
controls through `transaction_context["genvm_datetime"]` — so the
"delay elapsed" halves of the recovery tests are real tests, not skips.
"""
import pytest
from gltest import (
    get_contract_factory,
    get_default_account,
    create_account,
    get_gl_client,
    get_validator_factory,
)
from gltest.assertions import tx_execution_succeeded, tx_execution_failed

try:  # TransactionStatus is re-exported by gltest in newer releases.
    from gltest.types import TransactionStatus
except ImportError:  # pragma: no cover - older gltest
    from genlayer_py.types import TransactionStatus

ONE_GEN = 10**18

# Payouts leave the contract as EXTERNAL messages (see the _Wallet interface in
# the contract). An external message runs only once its parent transaction is
# FINALIZED, and the GEN is credited by a separate triggered child transaction.
# The .transact() defaults (wait_transaction_status=ACCEPTED,
# wait_triggered_transactions=False) return long before either happens, so any
# assertion on balances must opt into waiting or it will read a stale balance.
PAYOUT_WAIT = {
    "wait_transaction_status": TransactionStatus.FINALIZED,
    "wait_triggered_transactions": True,
    "wait_triggered_transactions_status": TransactionStatus.FINALIZED,
}
EVIDENCE_URL = "https://example.com/evidence"

T0 = "2024-01-01T00:00:00Z"
# 7-day cancel window and 3-day recovery window, plus a margin.
T_AFTER_CANCEL_DELAY = "2024-01-09T00:00:00Z"
T_AFTER_RECOVERY_DELAY = "2024-01-05T00:00:00Z"


def _balance(address: str) -> int:
    return get_gl_client().get_balance(address)


def _ctx(genvm_datetime: str, validators=None) -> dict:
    ctx = {"genvm_datetime": genvm_datetime}
    if validators is not None:
        ctx["validators"] = [v.to_dict() for v in validators]
    return ctx


def _mock_validators(raw_llm_response: str, count: int = 5):
    """
    Mock validators that answer any arbitration prompt with the given raw
    body. "" is the wildcard prompt key; if your Studio build requires a
    literal prompt match, print the prompt from verify_dispute() and use it
    as the key instead.
    """
    factory = get_validator_factory()
    return factory.batch_create_mock_validators(
        count=count,
        mock_llm_response={
            "nondet_exec_prompt": {"": raw_llm_response},
            "eq_principle_prompt_comparative": {"": True},
            "eq_principle_prompt_non_comparative": {},
        },
    )


def _verdict(winner: str, reasoning: str) -> str:
    return '{"winner": "%s", "reasoning": "%s"}' % (winner, reasoning)


@pytest.fixture
def freelancer_account():
    return create_account()


def _deploy():
    return get_contract_factory("ArbitrationEscrow").deploy()


def _funded_deal(freelancer_account, description="Backend API integration"):
    contract = _deploy()
    contract.create_deal(
        args=[str(freelancer_account.address), description, ONE_GEN],
        value=ONE_GEN,
    ).transact(transaction_context=_ctx(T0))
    return contract


def _disputed_deal(freelancer_account):
    """Client raised a dispute; freelancer never answered."""
    contract = _funded_deal(freelancer_account)
    contract.submit_client_evidence(
        args=[0, EVIDENCE_URL, "The API was never delivered"]
    ).transact(transaction_context=_ctx(T0))
    return contract


def _contested_deal(freelancer_account):
    """Both sides submitted evidence."""
    contract = _disputed_deal(freelancer_account)
    contract.connect(freelancer_account).submit_freelancer_evidence(
        args=[0, EVIDENCE_URL, "The API was delivered on time, see the repo"]
    ).transact(transaction_context=_ctx(T0))
    return contract


# --- AI arbitration payouts ------------------------------------------------


def test_resolve_dispute_pays_freelancer_when_llm_rules_for_freelancer(freelancer_account):
    contract = _contested_deal(freelancer_account)
    validators = _mock_validators(_verdict("freelancer", "Repo shows completed work."))

    balance_before = _balance(freelancer_account.address)
    receipt = contract.resolve_dispute(args=[0]).transact(
        transaction_context=_ctx(T0, validators), **PAYOUT_WAIT
    )
    assert tx_execution_succeeded(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "RESOLVED_FOR_FREELANCER"
    assert deal["paid"] is True
    assert _balance(freelancer_account.address) == balance_before + ONE_GEN


def test_resolve_dispute_pays_client_when_llm_rules_for_client(freelancer_account):
    contract = _contested_deal(freelancer_account)
    validators = _mock_validators(_verdict("client", "No proof of delivery."))

    client_account = get_default_account()
    balance_before = _balance(client_account.address)
    receipt = contract.resolve_dispute(args=[0]).transact(
        transaction_context=_ctx(T0, validators), **PAYOUT_WAIT
    )
    assert tx_execution_succeeded(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "RESOLVED_FOR_CLIENT"
    assert _balance(client_account.address) == balance_before + ONE_GEN


def test_resolve_dispute_requires_freelancer_evidence(freelancer_account):
    contract = _disputed_deal(freelancer_account)
    validators = _mock_validators(_verdict("client", "One-sided."))
    receipt = contract.resolve_dispute(args=[0]).transact(
        transaction_context=_ctx(T0, validators)
    )
    assert tx_execution_failed(receipt)


def test_resolve_dispute_cannot_run_twice(freelancer_account):
    contract = _contested_deal(freelancer_account)
    validators = _mock_validators(_verdict("client", "No proof of delivery."))
    assert tx_execution_succeeded(
        contract.resolve_dispute(args=[0]).transact(transaction_context=_ctx(T0, validators))
    )
    # Second attempt must revert: no double payout out of the escrow balance.
    assert tx_execution_failed(
        contract.resolve_dispute(args=[0]).transact(transaction_context=_ctx(T0, validators))
    )


# --- invalid verdict rejection --------------------------------------------


@pytest.mark.parametrize(
    "bad_llm_body",
    [
        '{"winner": "both", "reasoning": "Undecided."}',
        '{"winner": null, "reasoning": "Model refused to answer."}',
        '{"reasoning": "Missing winner field entirely."}',
        '{"winner": "Client", "reasoning": "Wrong casing must not silently pass."}',
        '{"winner": "client"',  # truncated / unparseable JSON
        "I cannot decide this dispute.",  # not JSON at all
    ],
)
def test_resolve_dispute_rejects_invalid_winner_values(freelancer_account, bad_llm_body):
    """
    An out-of-range, missing or unparseable winner must NEVER resolve the deal
    or trigger a payout — the whole transaction reverts and the deal stays
    retryable / recoverable.
    """
    contract = _contested_deal(freelancer_account)
    validators = _mock_validators(bad_llm_body)

    client_account = get_default_account()
    client_before = _balance(client_account.address)
    freelancer_before = _balance(freelancer_account.address)

    receipt = contract.resolve_dispute(args=[0]).transact(
        transaction_context=_ctx(T0, validators)
    )
    assert tx_execution_failed(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "DISPUTED"
    assert deal["paid"] is False
    assert deal["resolution_reasoning"] == ""
    assert _balance(client_account.address) == client_before
    assert _balance(freelancer_account.address) == freelancer_before


# --- stalled dispute (freelancer never responded) -------------------------


def test_cancel_stalled_dispute_rejected_too_early(freelancer_account):
    contract = _disputed_deal(freelancer_account)
    receipt = contract.cancel_stalled_dispute(args=[0]).transact(
        transaction_context=_ctx(T0)
    )
    assert tx_execution_failed(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "DISPUTED"


def test_cancel_stalled_dispute_rejected_once_freelancer_responded(freelancer_account):
    contract = _contested_deal(freelancer_account)
    receipt = contract.cancel_stalled_dispute(args=[0]).transact(
        transaction_context=_ctx(T_AFTER_CANCEL_DELAY)
    )
    assert tx_execution_failed(receipt)


def test_cancel_stalled_dispute_rejected_from_non_client(freelancer_account):
    contract = _disputed_deal(freelancer_account)
    receipt = (
        contract.connect(freelancer_account)
        .cancel_stalled_dispute(args=[0])
        .transact(transaction_context=_ctx(T_AFTER_CANCEL_DELAY))
    )
    assert tx_execution_failed(receipt)


def test_cancel_stalled_dispute_refunds_client_after_delay(freelancer_account):
    contract = _disputed_deal(freelancer_account)
    client_account = get_default_account()
    balance_before = _balance(client_account.address)

    receipt = contract.cancel_stalled_dispute(args=[0]).transact(
        transaction_context=_ctx(T_AFTER_CANCEL_DELAY), **PAYOUT_WAIT
    )
    assert tx_execution_succeeded(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "RESOLVED_FOR_CLIENT"
    assert deal["paid"] is True
    assert _balance(client_account.address) == balance_before + ONE_GEN


# --- bounded arbitration recovery (both sides responded) ------------------


def test_resolve_stalled_arbitration_rejected_too_early(freelancer_account):
    contract = _contested_deal(freelancer_account)
    receipt = contract.resolve_stalled_arbitration(args=[0]).transact(
        transaction_context=_ctx(T0)
    )
    assert tx_execution_failed(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "DISPUTED"


def test_resolve_stalled_arbitration_rejected_without_freelancer_evidence(freelancer_account):
    contract = _disputed_deal(freelancer_account)
    receipt = contract.resolve_stalled_arbitration(args=[0]).transact(
        transaction_context=_ctx(T_AFTER_RECOVERY_DELAY)
    )
    assert tx_execution_failed(receipt)


def test_resolve_stalled_arbitration_recovers_funds_after_delay(freelancer_account):
    """Evidence unreachable / no agreed verdict: deposit is never stuck."""
    contract = _contested_deal(freelancer_account)
    client_account = get_default_account()
    balance_before = _balance(client_account.address)

    receipt = contract.resolve_stalled_arbitration(args=[0]).transact(
        transaction_context=_ctx(T_AFTER_RECOVERY_DELAY), **PAYOUT_WAIT
    )
    assert tx_execution_succeeded(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "RESOLVED_FOR_CLIENT"
    assert deal["paid"] is True
    assert "could not reach a valid consensus" in deal["resolution_reasoning"]
    assert _balance(client_account.address) == balance_before + ONE_GEN


def test_recovery_is_callable_by_a_third_party(freelancer_account):
    contract = _contested_deal(freelancer_account)
    stranger = create_account()
    receipt = (
        contract.connect(stranger)
        .resolve_stalled_arbitration(args=[0])
        .transact(transaction_context=_ctx(T_AFTER_RECOVERY_DELAY))
    )
    assert tx_execution_succeeded(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "RESOLVED_FOR_CLIENT"


def test_recovery_unavailable_once_resolved(freelancer_account):
    contract = _contested_deal(freelancer_account)
    validators = _mock_validators(_verdict("client", "No proof of delivery."))
    assert tx_execution_succeeded(
        contract.resolve_dispute(args=[0]).transact(transaction_context=_ctx(T0, validators))
    )

    receipt = contract.resolve_stalled_arbitration(args=[0]).transact(
        transaction_context=_ctx(T_AFTER_RECOVERY_DELAY)
    )
    assert tx_execution_failed(receipt)


def test_recovery_after_invalid_verdict_unblocks_the_deal(freelancer_account):
    """The end-to-end story: verdict rejected → recovery frees the deposit."""
    contract = _contested_deal(freelancer_account)
    validators = _mock_validators('{"winner": "nobody", "reasoning": "n/a"}')
    assert tx_execution_failed(
        contract.resolve_dispute(args=[0]).transact(transaction_context=_ctx(T0, validators))
    )

    client_account = get_default_account()
    balance_before = _balance(client_account.address)
    assert tx_execution_succeeded(
        contract.resolve_stalled_arbitration(args=[0]).transact(
            transaction_context=_ctx(T_AFTER_RECOVERY_DELAY), **PAYOUT_WAIT
        )
    )
    assert _balance(client_account.address) == balance_before + ONE_GEN


def test_get_timelocks_exposes_delays_and_valid_winners():
    import json

    contract = _deploy()
    config = json.loads(contract.get_timelocks(args=[]).call())
    assert config["dispute_cancel_delay_seconds"] == 7 * 24 * 60 * 60
    assert config["arbitration_recovery_delay_seconds"] == 3 * 24 * 60 * 60
    assert config["valid_winners"] == ["client", "freelancer"]
