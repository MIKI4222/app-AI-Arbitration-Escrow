"""
Focused tests for the deposit and non-AI payout paths.

Run with:
    gltest tests/direct/test_deposit_and_payouts.py
"""
import pytest
from gltest import get_contract_factory, get_default_account, create_account, get_gl_client
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
T0 = "2024-01-01T00:00:00Z"


def _balance(address: str) -> int:
    # LocalAccount (eth_account) has no get_balance() — balances are read
    # through the GenLayer client, not the account object.
    return get_gl_client().get_balance(address)


def _ctx() -> dict:
    # The contract time-locks on the consensus clock, so every write pins a
    # deterministic genvm_datetime.
    return {"genvm_datetime": T0}


@pytest.fixture
def deployed_contract():
    return get_contract_factory("ArbitrationEscrow").deploy()


@pytest.fixture
def client_account():
    return get_default_account()


@pytest.fixture
def freelancer_account():
    return create_account()


def _create(contract, freelancer_account, description="Landing page redesign", amount=ONE_GEN, value=ONE_GEN):
    return contract.create_deal(
        args=[str(freelancer_account.address), description, amount],
        value=value,
    ).transact(transaction_context=_ctx())


# --- deposit --------------------------------------------------------------


def test_create_deal_requires_matching_deposit(deployed_contract, freelancer_account):
    receipt = _create(deployed_contract, freelancer_account, value=0)
    assert tx_execution_failed(receipt)


def test_create_deal_rejects_partial_deposit(deployed_contract, freelancer_account):
    receipt = _create(deployed_contract, freelancer_account, value=ONE_GEN // 2)
    assert tx_execution_failed(receipt)


def test_create_deal_rejects_overpayment(deployed_contract, freelancer_account):
    """Recorded amount must equal the custodied deposit, never less than it."""
    receipt = _create(deployed_contract, freelancer_account, value=2 * ONE_GEN)
    assert tx_execution_failed(receipt)


def test_create_deal_rejects_zero_amount(deployed_contract, freelancer_account):
    receipt = _create(deployed_contract, freelancer_account, amount=0, value=0)
    assert tx_execution_failed(receipt)


def test_create_deal_accepts_exact_deposit(deployed_contract, freelancer_account):
    receipt = _create(deployed_contract, freelancer_account)
    assert tx_execution_succeeded(receipt)

    deal = deployed_contract.get_deal(args=[0]).call()
    assert deal["status"] == "FUNDED"
    assert deal["amount"] == ONE_GEN
    assert deal["paid"] is False
    # Addresses are stored normalised so role checks are case-insensitive.
    assert deal["freelancer"] == str(freelancer_account.address).lower()


def test_get_deal_rejects_unknown_id(deployed_contract):
    with pytest.raises(Exception):
        deployed_contract.get_deal(args=[42]).call()


# --- voluntary release ----------------------------------------------------


def test_release_funds_pays_freelancer_and_updates_status(deployed_contract, freelancer_account):
    _create(deployed_contract, freelancer_account, description="Logo design")

    balance_before = _balance(freelancer_account.address)
    receipt = deployed_contract.release_funds(args=[0]).transact(
        transaction_context=_ctx(), **PAYOUT_WAIT
    )
    assert tx_execution_succeeded(receipt)

    deal = deployed_contract.get_deal(args=[0]).call()
    assert deal["status"] == "RELEASED_TO_FREELANCER"
    assert deal["paid"] is True
    assert _balance(freelancer_account.address) == balance_before + ONE_GEN


def test_release_funds_rejected_from_non_client(deployed_contract, freelancer_account):
    _create(deployed_contract, freelancer_account, description="Logo design")

    receipt = (
        deployed_contract.connect(freelancer_account)
        .release_funds(args=[0])
        .transact(transaction_context=_ctx())
    )
    assert tx_execution_failed(receipt)


def test_release_funds_cannot_be_repeated(deployed_contract, freelancer_account):
    """Regression guard for the repeated ACCEPTED (ERROR) release_funds calls:
    the second attempt reverts with a clear status message and pays nothing."""
    _create(deployed_contract, freelancer_account, description="Logo design")
    assert tx_execution_succeeded(
        deployed_contract.release_funds(args=[0]).transact(
            transaction_context=_ctx(), **PAYOUT_WAIT
        )
    )

    # Wait for the first payout to settle before snapshotting, otherwise the
    # balance could still move while the rejected second attempt is running.
    balance_before = _balance(freelancer_account.address)
    assert tx_execution_failed(
        deployed_contract.release_funds(args=[0]).transact(transaction_context=_ctx())
    )
    assert _balance(freelancer_account.address) == balance_before


def test_release_funds_rejected_on_already_disputed_deal(deployed_contract, freelancer_account):
    _create(deployed_contract, freelancer_account, description="Logo design")
    deployed_contract.submit_client_evidence(
        args=[0, "https://example.com/proof", "Work was never delivered"]
    ).transact(transaction_context=_ctx())

    receipt = deployed_contract.release_funds(args=[0]).transact(transaction_context=_ctx())
    assert tx_execution_failed(receipt)


def test_release_funds_rejects_unknown_deal_id(deployed_contract):
    receipt = deployed_contract.release_funds(args=[7]).transact(transaction_context=_ctx())
    assert tx_execution_failed(receipt)


# --- evidence guards ------------------------------------------------------


def test_only_client_can_open_a_dispute(deployed_contract, freelancer_account):
    _create(deployed_contract, freelancer_account)
    receipt = (
        deployed_contract.connect(freelancer_account)
        .submit_client_evidence(args=[0, "https://example.com/x", "claim"])
        .transact(transaction_context=_ctx())
    )
    assert tx_execution_failed(receipt)


def test_only_freelancer_can_submit_counter_evidence(deployed_contract, freelancer_account):
    _create(deployed_contract, freelancer_account)
    deployed_contract.submit_client_evidence(
        args=[0, "https://example.com/proof", "Not delivered"]
    ).transact(transaction_context=_ctx())

    # The client cannot impersonate the freelancer...
    assert tx_execution_failed(
        deployed_contract.submit_freelancer_evidence(
            args=[0, "https://example.com/repo", "Delivered"]
        ).transact(transaction_context=_ctx())
    )
    # ...but the freelancer can, and the deal stays DISPUTED with evidence set.
    assert tx_execution_succeeded(
        deployed_contract.connect(freelancer_account)
        .submit_freelancer_evidence(args=[0, "https://example.com/repo", "Delivered"])
        .transact(transaction_context=_ctx())
    )
    deal = deployed_contract.get_deal(args=[0]).call()
    assert deal["status"] == "DISPUTED"
    assert deal["freelancer_evidence_url"] == "https://example.com/repo"
    assert deal["freelancer_evidence_time"] > 0


def test_freelancer_evidence_requires_open_dispute(deployed_contract, freelancer_account):
    _create(deployed_contract, freelancer_account)
    receipt = (
        deployed_contract.connect(freelancer_account)
        .submit_freelancer_evidence(args=[0, "https://example.com/repo", "Delivered"])
        .transact(transaction_context=_ctx())
    )
    assert tx_execution_failed(receipt)
