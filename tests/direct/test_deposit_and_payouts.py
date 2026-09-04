"""
Focused direct-mode tests for the deposit and simple (non-AI) payout paths.

Run with:
    gltest tests/direct/test_deposit_and_payouts.py

These run the contract in-memory (no Docker / Studio required), so they're
fast enough for CI on every commit.
"""
import pytest
from gltest import get_contract_factory, get_default_account, create_account, get_gl_client
from gltest.assertions import tx_execution_succeeded, tx_execution_failed

ONE_GEN = 10**18


def _balance(address: str) -> int:
    # LocalAccount (eth_account) has no get_balance() — balances are read
    # through the GenLayer client, not the account object.
    return get_gl_client().get_balance(address)


@pytest.fixture
def deployed_contract():
    factory = get_contract_factory("ArbitrationEscrow")
    return factory.deploy()


@pytest.fixture
def client_account():
    return get_default_account()


@pytest.fixture
def freelancer_account():
    return create_account()


def test_create_deal_requires_matching_deposit(deployed_contract, freelancer_account):
    """A deal must be rejected if the sent value doesn't match the stated amount."""
    receipt = deployed_contract.create_deal(
        args=[str(freelancer_account.address), "Landing page redesign", ONE_GEN],
        value=0,  # no deposit sent, despite amount=ONE_GEN
    ).transact()
    assert tx_execution_failed(receipt)


def test_create_deal_accepts_exact_deposit(deployed_contract, freelancer_account):
    """A deal funded with exactly the stated amount is created and stored as FUNDED."""
    receipt = deployed_contract.create_deal(
        args=[str(freelancer_account.address), "Landing page redesign", ONE_GEN],
        value=ONE_GEN,
    ).transact()
    assert tx_execution_succeeded(receipt)

    deal = deployed_contract.get_deal(args=[0]).call()
    assert deal["status"] == "FUNDED"
    assert deal["amount"] == ONE_GEN


def test_create_deal_rejects_partial_deposit(deployed_contract, freelancer_account):
    """A deal funded with less than the stated amount must be rejected, not custodied partially."""
    receipt = deployed_contract.create_deal(
        args=[str(freelancer_account.address), "Landing page redesign", ONE_GEN],
        value=ONE_GEN // 2,
    ).transact()
    assert tx_execution_failed(receipt)


def test_release_funds_pays_freelancer_and_updates_status(deployed_contract, freelancer_account):
    deployed_contract.create_deal(
        args=[str(freelancer_account.address), "Logo design", ONE_GEN],
        value=ONE_GEN,
    ).transact()

    balance_before = _balance(freelancer_account.address)
    receipt = deployed_contract.release_funds(args=[0]).transact()
    assert tx_execution_succeeded(receipt)

    deal = deployed_contract.get_deal(args=[0]).call()
    assert deal["status"] == "RELEASED_TO_FREELANCER"
    assert _balance(freelancer_account.address) == balance_before + ONE_GEN


def test_release_funds_rejected_from_non_client(deployed_contract, freelancer_account):
    deployed_contract.create_deal(
        args=[str(freelancer_account.address), "Logo design", ONE_GEN],
        value=ONE_GEN,
    ).transact()

    # freelancer tries to release funds to themself directly — must fail
    receipt = deployed_contract.connect(freelancer_account).release_funds(args=[0]).transact()
    assert tx_execution_failed(receipt)


def test_release_funds_rejected_on_already_disputed_deal(deployed_contract, freelancer_account):
    deployed_contract.create_deal(
        args=[str(freelancer_account.address), "Logo design", ONE_GEN],
        value=ONE_GEN,
    ).transact()
    deployed_contract.submit_client_evidence(
        args=[0, "https://example.com/proof", "Work was never delivered"]
    ).transact()

    receipt = deployed_contract.release_funds(args=[0]).transact()
    assert tx_execution_failed(receipt)
