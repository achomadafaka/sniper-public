"""Test pool resolution with captured funded Solana entry transactions."""
import json
from pathlib import Path

import pytest

from sniper.services.memecoin_solana_amounts import (
    NATIVE_SOL, resolve_pool_accounts_dynamic, resolve_pump_curve_accounts,
)
from sniper.services.memecoin_solana_tape import _resolve_accounts


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "solana" / "funded_entry_venues.json").read_text())


def _case(name):
    f = FIXTURES[name]
    tx = f["tx"]
    payer = tx["transaction"]["message"]["accountKeys"][0]["pubkey"]
    return tx, f["mint"], payer


CURVES = ["pump_curve_entry", "pump_curve_entry_2"]
ALL = CURVES + ["pumpswap_entry"]


def _reject_unknown_quote(_mint):
    return False


def test_the_fixtures_are_our_own_confirmed_entries():
    for name in ALL:
        tx, mint, payer = _case(name)
        assert tx["meta"]["err"] is None
        assert mint and payer
        held = {b["owner"] for b in tx["meta"]["postTokenBalances"] if b.get("mint") == mint}
        assert payer in held, "the payer must hold the purchased coin"


@pytest.mark.parametrize("name", CURVES)
def test_a_pump_bonding_curve_entry_binds(name):
    """A native-SOL curve has no quote token account."""
    tx, mint, payer = _case(name)
    assert resolve_pool_accounts_dynamic(
        tx, mint, exclude_owner=payer, quote_ok_fn=_reject_unknown_quote) is None

    resolved = resolve_pump_curve_accounts(tx, mint, exclude_owner=payer)
    assert resolved is not None, "the curve decoder must resolve our own entry"
    owner, vault, quote_mint, quote_vault = resolved
    assert quote_mint == NATIVE_SOL
    assert owner == quote_vault != payer
    assert vault != payer


@pytest.mark.parametrize("name", CURVES)
def test_the_fee_payer_alone_made_the_curve_ambiguous(name):
    """The fee payer creates a second curve candidate without the exclusion."""
    tx, mint, payer = _case(name)
    assert resolve_pump_curve_accounts(tx, mint) is None
    assert resolve_pump_curve_accounts(tx, mint, exclude_owner=payer) is not None


def test_resolve_accounts_threads_the_exclusion_to_the_curve_path():
    """The position resolver must exclude the fee payer on its curve path."""
    tx, mint, payer = _case("pump_curve_entry")
    resolved = _resolve_accounts(tx, mint, exclude_owner=payer)
    assert resolved is not None
    assert resolved[0] != payer


def test_a_pumpswap_amm_entry_still_binds_through_the_dynamic_path():
    """The dynamic path must continue to resolve a PumpSwap entry."""
    tx, mint, payer = _case("pumpswap_entry")
    dynamic = resolve_pool_accounts_dynamic(
        tx, mint, exclude_owner=payer, quote_ok_fn=_reject_unknown_quote)
    assert dynamic is not None
    assert _resolve_accounts(tx, mint, exclude_owner=payer) == dynamic
    assert resolve_pump_curve_accounts(tx, mint, exclude_owner=payer) is None


@pytest.mark.parametrize("name", ALL)
def test_no_venue_ever_resolves_our_own_wallet_as_the_pool(name):
    """A pool binding must not use the funded wallet as a pool account."""
    tx, mint, payer = _case(name)
    resolved = _resolve_accounts(tx, mint, exclude_owner=payer)
    if resolved is None:
        pytest.fail(f"{name} did not resolve at all")
    assert payer not in {str(x) for x in resolved}
