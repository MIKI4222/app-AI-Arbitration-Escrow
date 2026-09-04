"""
Focused direct-mode tests for:
  - AI dispute resolution (resolve_dispute) with a mocked LLM verdict
  - rejection of an invalid/malformed winner value returned by the LLM
  - cancel_stalled_dispute (freelancer never responds)
  - resolve_stalled_arbitration (both sides responded, but resolve_dispute
    could never reach a valid, agreed verdict)

Run with:
    gltest tests/direct/test_arbitration_and_recovery.py

NOTE ON MOCKING: mock key names ("nondet_exec_prompt",
"eq_principle_prompt_comparative", "nondet_web_request"/"nondet_web_render")
follow the genlayer-test cheatcode conventions at the time this suite was
written. If your installed genlayer-test version renames these, update the
mock dictionaries below accordingly — the assertions and scenarios they
support stay the same.

NOTE ON BLOCK ADVANCEMENT: `contract.mine_blocks(n)` represents "advance the
simulated chain by n blocks" in direct mode. If your installed genlayer-test
version exposes this differently (e.g. a `chain`/`network` fixture method
such as `chain.mine(n)`), swap the calls below accordingly — the block-delay
assertions themselves are what matters and should be adapted to whichever
helper your genlayer-test version ships.
"""
import json
import pytest
from gltest import get_contract_factory, get_default_account, create_account
from gltest.assertions import tx_execution_succeeded, tx_execution_failed
from gltest.validators import validator_factory

ONE_GEN = 10**18
EVIDENCE_URL = "https://example.com/evidence"


def _setup_disputed_deal(freelancer_account):
    factory = get_contract_factory("ArbitrationEscrow")
    contract = factory.deploy()
    contract.create_deal(
        args=[str(freelancer_account.address), "Backend API integration", ONE_GEN],
        value=ONE_GEN,
    ).transact()
    contract.submit_client_evidence(
        args=[0, EVIDENCE_URL, "The API was never delivered"]
    ).transact()
    contract.connect(freelancer_account).submit_freelancer_evidence(
        args=[0, EVIDENCE_URL, "The API was delivered on time, see the repo"]
    ).transact()
    return contract


def _mocked_context(llm_response: dict, validator_count: int = 5):
    validators = validator_factory.batch_create_mock_validators(
        count=validator_count,
        mock_llm_response={
            "nondet_exec_prompt": json.dumps(llm_response),
            "eq_principle_prompt_comparative": json.dumps(llm_response),
        },
    )
    return {
        "validators": [v.to_dict() for v in validators],
        "genvm_datetime": "2024-01-01T00:00:00Z",
    }


@pytest.fixture
def freelancer_account():
    return create_account()


def test_resolve_dispute_pays_freelancer_when_llm_rules_for_freelancer(freelancer_account):
    contract = _setup_disputed_deal(freelancer_account)
    ctx = _mocked_context({"winner": "freelancer", "reasoning": "Repo shows completed work."})

    balance_before = freelancer_account.get_balance()
    receipt = contract.resolve_dispute(args=[0]).transact(transaction_context=ctx)
    assert tx_execution_succeeded(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "RESOLVED_FOR_FREELANCER"
    assert freelancer_account.get_balance() == balance_before + ONE_GEN


def test_resolve_dispute_pays_client_when_llm_rules_for_client(freelancer_account):
    contract = _setup_disputed_deal(freelancer_account)
    ctx = _mocked_context({"winner": "client", "reasoning": "No proof of delivery."})

    client_account = get_default_account()
    balance_before = client_account.get_balance()
    receipt = contract.resolve_dispute(args=[0]).transact(transaction_context=ctx)
    assert tx_execution_succeeded(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "RESOLVED_FOR_CLIENT"
    assert client_account.get_balance() == balance_before + ONE_GEN


@pytest.mark.parametrize(
    "bad_verdict",
    [
        {"winner": "both", "reasoning": "Undecided."},
        {"winner": None, "reasoning": "Model refused to answer."},
        {"reasoning": "Missing winner field entirely."},
        {"winner": "Client", "reasoning": "Wrong casing, must not silently pass."},
    ],
)
def test_resolve_dispute_rejects_invalid_winner_values(freelancer_account, bad_verdict):
    """
    Regression test for the original defect: an unparseable / out-of-range
    winner must NEVER resolve the deal or trigger a payout — it must
    revert the whole transaction.
    """
    contract = _setup_disputed_deal(freelancer_account)
    ctx = _mocked_context(bad_verdict)

    receipt = contract.resolve_dispute(args=[0]).transact(transaction_context=ctx)
    assert tx_execution_failed(receipt)

    # The deal must remain DISPUTED and untouched — no partial state change,
    # no payout, so it can still be retried or fall back to recovery.
    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "DISPUTED"


def test_cancel_stalled_dispute_requires_delay_and_no_freelancer_evidence(freelancer_account):
    factory = get_contract_factory("ArbitrationEscrow")
    contract = factory.deploy()
    contract.create_deal(
        args=[str(freelancer_account.address), "Copywriting", ONE_GEN],
        value=ONE_GEN,
    ).transact()
    contract.submit_client_evidence(
        args=[0, EVIDENCE_URL, "Freelancer went silent"]
    ).transact()

    # Too early — freelancer just went quiet, delay hasn't elapsed.
    receipt = contract.cancel_stalled_dispute(args=[0]).transact()
    assert tx_execution_failed(receipt)

    # Advance the chain past DISPUTE_CANCEL_DELAY_BLOCKS, then retry.
    contract.mine_blocks(50_000)
    receipt = contract.cancel_stalled_dispute(args=[0]).transact()
    assert tx_execution_succeeded(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "RESOLVED_FOR_CLIENT"


def test_cancel_stalled_dispute_rejected_once_freelancer_responded(freelancer_account):
    contract = _setup_disputed_deal(freelancer_account)  # freelancer already responded
    contract.mine_blocks(50_000)

    receipt = contract.cancel_stalled_dispute(args=[0]).transact()
    assert tx_execution_failed(receipt)


def test_resolve_stalled_arbitration_requires_both_evidence_and_delay(freelancer_account):
    """
    Bounded recovery path: only usable once both sides have submitted
    evidence AND resolve_dispute has had ARBITRATION_RECOVERY_DELAY_BLOCKS
    to succeed and hasn't. Ensures funds can never be stuck forever if
    evidence pages go offline or the LLM can't produce a valid verdict.
    """
    contract = _setup_disputed_deal(freelancer_account)

    # Too early — recovery window hasn't elapsed yet.
    receipt = contract.resolve_stalled_arbitration(args=[0]).transact()
    assert tx_execution_failed(receipt)

    contract.mine_blocks(20_000)
    receipt = contract.resolve_stalled_arbitration(args=[0]).transact()
    assert tx_execution_succeeded(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "RESOLVED_FOR_CLIENT"


def test_resolve_stalled_arbitration_rejected_without_freelancer_evidence(freelancer_account):
    factory = get_contract_factory("ArbitrationEscrow")
    contract = factory.deploy()
    contract.create_deal(
        args=[str(freelancer_account.address), "Copywriting", ONE_GEN],
        value=ONE_GEN,
    ).transact()
    contract.submit_client_evidence(
        args=[0, EVIDENCE_URL, "Freelancer went silent"]
    ).transact()
    contract.mine_blocks(20_000)

    # Freelancer never submitted evidence — must use cancel_stalled_dispute,
    # not resolve_stalled_arbitration.
    receipt = contract.resolve_stalled_arbitration(args=[0]).transact()
    assert tx_execution_failed(receipt)


def test_resolve_stalled_arbitration_unavailable_once_resolved(freelancer_account):
    contract = _setup_disputed_deal(freelancer_account)
    ctx = _mocked_context({"winner": "client", "reasoning": "No proof of delivery."})
    contract.resolve_dispute(args=[0]).transact(transaction_context=ctx)

    contract.mine_blocks(20_000)
    receipt = contract.resolve_stalled_arbitration(args=[0]).transact()
    assert tx_execution_failed(receipt)
