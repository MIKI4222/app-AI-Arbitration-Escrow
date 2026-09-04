"""
Focused tests for:
  - AI dispute resolution (resolve_dispute) with a mocked LLM verdict
  - rejection of an invalid/malformed winner value returned by the LLM
  - cancel_stalled_dispute (freelancer never responds)
  - resolve_stalled_arbitration (both sides responded, but resolve_dispute
    could never reach a valid, agreed verdict)

Run with:
    gltest tests/direct/test_arbitration_and_recovery.py

Requires a running GenLayer network (e.g. GenLayer Studio's localnet via
Docker) reachable at the RPC URL configured in gltest.config.yaml — despite
living under tests/direct/, these still go through the JSON-RPC client, not
an in-process VM.

STATUS OF THIS FILE (verified against genlayer-test==0.29.2 source, but not
yet executed against a live network — see conversation/README notes):

  - get_validator_factory(), .batch_create_mock_validators(), contract
    method-call syntax, and tx_execution_succeeded/failed were verified
    against the installed package source and should be correct.
  - Account balances are read via get_gl_client().get_balance(address) —
    LocalAccount has no get_balance() method of its own.
  - mock_llm_response["nondet_exec_prompt"] must be a dict of
    {prompt_text: response_string}, and ["eq_principle_prompt_comparative"]
    a dict of {principle_text: bool} (per gltest.types.MockedLLMResponse) —
    NOT a single JSON string as an earlier draft of this file assumed. The
    exact key-matching behaviour (literal prompt match vs. substring vs.
    wildcard "") is implemented in the GenVM/LLM plugin, which ships
    separately from this package, so the placeholder "" keys below need to
    be confirmed/adjusted against your GenLayer Studio version before these
    tests will actually match and mock the contract's real prompts.
  - contract.mine_blocks(n) / any other block-advancement helper DOES NOT
    EXIST anywhere in genlayer-test==0.29.2. There is currently no
    documented way to fast-forward gl.message.block_number in a test. As a
    result, the "delay has elapsed, action now succeeds" half of the
    cancel_stalled_dispute / resolve_stalled_arbitration tests is marked
    pytest.mark.skip below rather than shipped as a silently-broken test.
    The "too early, action correctly rejected" half needs no time travel
    (it runs immediately after evidence submission) and IS fully testable.
    If a block-advancement API exists in a newer genlayer-test release, or
    a chain/network fixture the CLI docs mention, un-skip these and wire it
    in.
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

ONE_GEN = 10**18
EVIDENCE_URL = "https://example.com/evidence"


def _balance(address: str) -> int:
    return get_gl_client().get_balance(address)


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


def _mocked_context(winner: str, reasoning: str, validator_count: int = 5):
    """
    Build a transaction_context with mock validators that make the LLM
    "agree" on the given winner. The "" keys mean "respond this way to any
    prompt" IF the plugin treats an empty key as a wildcard — confirm this
    against your GenLayer Studio version; if it requires literal prompt
    text instead, replace "" with the exact prompt string the contract's
    verify_dispute() builds (it's plain Python string formatting, so you
    can print it directly from the contract code to get the exact text).
    """
    factory = get_validator_factory()
    validators = factory.batch_create_mock_validators(
        count=validator_count,
        mock_llm_response={
            "nondet_exec_prompt": {
                "": f'{{"winner": {winner!r}, "reasoning": {reasoning!r}}}'
            },
            "eq_principle_prompt_comparative": {"": True},
            "eq_principle_prompt_non_comparative": {},
        },
    )
    return {
        "validators": [v.to_dict() for v in validators],
        "genvm_datetime": "2024-01-01T00:00:00Z",
    }


def _mocked_invalid_context(raw_json_body: str, validator_count: int = 5):
    """Same as _mocked_context but for malformed/invalid LLM JSON payloads."""
    factory = get_validator_factory()
    validators = factory.batch_create_mock_validators(
        count=validator_count,
        mock_llm_response={
            "nondet_exec_prompt": {"": raw_json_body},
            "eq_principle_prompt_comparative": {"": True},
            "eq_principle_prompt_non_comparative": {},
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
    ctx = _mocked_context("freelancer", "Repo shows completed work.")

    balance_before = _balance(freelancer_account.address)
    receipt = contract.resolve_dispute(args=[0]).transact(transaction_context=ctx)
    assert tx_execution_succeeded(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "RESOLVED_FOR_FREELANCER"
    assert _balance(freelancer_account.address) == balance_before + ONE_GEN


def test_resolve_dispute_pays_client_when_llm_rules_for_client(freelancer_account):
    contract = _setup_disputed_deal(freelancer_account)
    ctx = _mocked_context("client", "No proof of delivery.")

    client_account = get_default_account()
    balance_before = _balance(client_account.address)
    receipt = contract.resolve_dispute(args=[0]).transact(transaction_context=ctx)
    assert tx_execution_succeeded(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "RESOLVED_FOR_CLIENT"
    assert _balance(client_account.address) == balance_before + ONE_GEN


@pytest.mark.parametrize(
    "bad_json_body",
    [
        '{"winner": "both", "reasoning": "Undecided."}',
        '{"winner": null, "reasoning": "Model refused to answer."}',
        '{"reasoning": "Missing winner field entirely."}',
        '{"winner": "Client", "reasoning": "Wrong casing, must not silently pass."}',
    ],
)
def test_resolve_dispute_rejects_invalid_winner_values(freelancer_account, bad_json_body):
    """
    Regression test for the original defect: an unparseable / out-of-range
    winner must NEVER resolve the deal or trigger a payout — it must
    revert the whole transaction.
    """
    contract = _setup_disputed_deal(freelancer_account)
    ctx = _mocked_invalid_context(bad_json_body)

    receipt = contract.resolve_dispute(args=[0]).transact(transaction_context=ctx)
    assert tx_execution_failed(receipt)

    # The deal must remain DISPUTED and untouched — no partial state change,
    # no payout, so it can still be retried or fall back to recovery.
    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "DISPUTED"


def test_cancel_stalled_dispute_rejected_too_early(freelancer_account):
    """The non-time-travel half: rejected immediately, before any delay has passed."""
    factory = get_contract_factory("ArbitrationEscrow")
    contract = factory.deploy()
    contract.create_deal(
        args=[str(freelancer_account.address), "Copywriting", ONE_GEN],
        value=ONE_GEN,
    ).transact()
    contract.submit_client_evidence(
        args=[0, EVIDENCE_URL, "Freelancer went silent"]
    ).transact()

    receipt = contract.cancel_stalled_dispute(args=[0]).transact()
    assert tx_execution_failed(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "DISPUTED"


def test_cancel_stalled_dispute_rejected_once_freelancer_responded(freelancer_account):
    contract = _setup_disputed_deal(freelancer_account)  # freelancer already responded
    receipt = contract.cancel_stalled_dispute(args=[0]).transact()
    assert tx_execution_failed(receipt)


@pytest.mark.skip(
    reason="genlayer-test==0.29.2 has no block-advancement API; needs a "
    "time-travel helper to reach DISPUTE_CANCEL_DELAY_BLOCKS. See module "
    "docstring."
)
def test_cancel_stalled_dispute_succeeds_after_delay(freelancer_account):
    factory = get_contract_factory("ArbitrationEscrow")
    contract = factory.deploy()
    contract.create_deal(
        args=[str(freelancer_account.address), "Copywriting", ONE_GEN],
        value=ONE_GEN,
    ).transact()
    contract.submit_client_evidence(
        args=[0, EVIDENCE_URL, "Freelancer went silent"]
    ).transact()

    # contract.mine_blocks(50_000)  # <-- no such API currently exists
    receipt = contract.cancel_stalled_dispute(args=[0]).transact()
    assert tx_execution_succeeded(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "RESOLVED_FOR_CLIENT"


def test_resolve_stalled_arbitration_rejected_too_early(freelancer_account):
    """The non-time-travel half: both sides responded, but delay hasn't elapsed."""
    contract = _setup_disputed_deal(freelancer_account)
    receipt = contract.resolve_stalled_arbitration(args=[0]).transact()
    assert tx_execution_failed(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "DISPUTED"


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

    # Freelancer never submitted evidence — must use cancel_stalled_dispute,
    # not resolve_stalled_arbitration. (No block advancement needed: this
    # must fail on the "no freelancer evidence" guard regardless of delay.)
    receipt = contract.resolve_stalled_arbitration(args=[0]).transact()
    assert tx_execution_failed(receipt)


@pytest.mark.skip(
    reason="genlayer-test==0.29.2 has no block-advancement API; needs a "
    "time-travel helper to reach ARBITRATION_RECOVERY_DELAY_BLOCKS. See "
    "module docstring."
)
def test_resolve_stalled_arbitration_succeeds_after_delay(freelancer_account):
    contract = _setup_disputed_deal(freelancer_account)
    # contract.mine_blocks(20_000)  # <-- no such API currently exists
    receipt = contract.resolve_stalled_arbitration(args=[0]).transact()
    assert tx_execution_succeeded(receipt)

    deal = contract.get_deal(args=[0]).call()
    assert deal["status"] == "RESOLVED_FOR_CLIENT"


def test_resolve_stalled_arbitration_unavailable_once_resolved(freelancer_account):
    contract = _setup_disputed_deal(freelancer_account)
    ctx = _mocked_context("client", "No proof of delivery.")
    receipt = contract.resolve_dispute(args=[0]).transact(transaction_context=ctx)
    assert tx_execution_succeeded(receipt)

    receipt = contract.resolve_stalled_arbitration(args=[0]).transact()
    assert tx_execution_failed(receipt)
