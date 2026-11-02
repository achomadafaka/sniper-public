"""A PumpSwap buy on a pool quoted in another token is valued from the route, or stays unknown.

Alert c3843ec8 (2026-09-27 11:38:12Z). SossaDotKek bought 36PAmhwW through Jupiter: 0.01 SOL ->
Ai66 (a pump token) -> PumpSwap buy_exact_quote_in on a pool QUOTED IN Ai66, Token-2022. The
event binding required a WSOL/classic-token quote, so the buy read 'quote_or_instruction_identity'
and never reached the route valuation, which already recovers it from the receipt (Codex,
docs/audits/2026-09-27-alert-c3843ec8.md). The buy was ~$1.24 and must still be refused, now as a
KNOWN small buy instead of an unknown size.

The fixture is the REAL receipt. Each negative case changes ONE named field of it, so each reaches
its branch on real structure. A verified non-SOL quote is the only relaxation: a forged quote mint
or program, a vault owned by anyone but the pool, or two matching instructions stay unknown.
"""
import copy
import json
from pathlib import Path

import pytest

FIX = json.loads((Path(__file__).parent / "fixtures/memecoin/nonsol_amm_source_buy.json").read_text())
OWNER = FIX["owner"]
MINT = "36PAmhwW5h9mbWxBAjD92Ttpt7tKi8WsWAjfBriQpump"
QUOTE = "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump"
POOL = "CUhwNfbQKpiTnqcwpA3raiRk77rsu4Qq7JF7WUHK7fxg"
POOL_QUOTE_VAULT = "HNzaSXVqC2RqoBbFeurcnrXuUpgJjxhiThaqGK1FGnDZ"
USER_QUOTE = "BD3nfnWecjSf5dtQHAzMwZLD87mF7K14tkCpHTyQfT2D"
WSOL = "So11111111111111111111111111111111111111112"
T22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SOL_USD = (124.0, 100.0)          # a controlled input, not the historical quote


def _tx():
    return copy.deepcopy(FIX["sossadotkek-nonsol-amm-c3843ec8"])


def _value(tx, sol_usd=SOL_USD, now=105.0):
    from sniper.services.memecoin_source_valuation import value_source_buy
    return value_source_buy(tx, OWNER, sol_usd=sol_usd, now=now)


def _amm_ix(tx):
    from sniper.execution.pumpswap_buy import AMM_PROGRAM
    found = [ix for g in tx["meta"]["innerInstructions"] for ix in g["instructions"]
             if ix.get("programId") == AMM_PROGRAM and len(ix.get("accounts", [])) >= 13]
    assert len(found) == 1
    return found[0]


def _balance_rows(tx, account):
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    return [b for side in ("preTokenBalances", "postTokenBalances") for b in tx["meta"][side]
            if keys[b["accountIndex"]] == account]


def test_the_c3843ec8_buy_is_valued_from_the_route():
    facts = _value(_tx())
    assert facts.get("unknown_reason") is None, facts
    assert facts["event_unknown_reason"] == "non_sol_quote_amm"
    assert facts["basis"] == "route_owner_quote_out"
    assert facts["mint"] == MINT
    assert facts["token_amount_atomic"] == "21266861342"
    assert facts["quote_out_atomic"][WSOL] == "10000000"
    assert facts["source_size_usd"] == pytest.approx(0.01 * 124.0)


def test_the_known_small_buy_is_still_refused_by_the_recorded_policy():
    from sniper.services.memecoin_real_runner import source_buy_family_reason
    size = _value(_tx())["source_size_usd"]
    reason, info = source_buy_family_reason(
        their_usd=size, source_mcap=54018.0, prior_net_usd=None, ratio=0.0025,
        unknown_mcap_floor=100.0, abs_min=197.0, abs_max=0.0, family_active=True,
        floor_tolerance_pct=2.0)
    assert reason and "unknown" not in reason, reason
    assert info["effective_floor_usd"] == pytest.approx(132.3441)


def _forge_quote_program(tx):
    _amm_ix(tx)["accounts"][12] = "11111111111111111111111111111111"


def _forge_quote_mint(tx):
    _amm_ix(tx)["accounts"][4] = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


def _vault_not_owned_by_pool(tx):
    for b in _balance_rows(tx, POOL_QUOTE_VAULT):
        b["owner"] = OWNER


def _vault_holds_another_mint(tx):
    for b in _balance_rows(tx, POOL_QUOTE_VAULT):
        b["mint"] = WSOL


def _user_quote_owned_by_someone_else(tx):
    for b in _balance_rows(tx, USER_QUOTE):
        b["owner"] = POOL


def _wsol_quote_on_token_2022(tx):
    ix = _amm_ix(tx)
    ix["accounts"][4] = WSOL                        # the WSOL path with the wrong token program
    for b in _balance_rows(tx, POOL_QUOTE_VAULT) + _balance_rows(tx, USER_QUOTE):
        b["mint"] = WSOL


def _two_matching_instructions(tx):
    group = next(g for g in tx["meta"]["innerInstructions"] if _amm_ix(tx) in g["instructions"])
    group["instructions"].append(copy.deepcopy(_amm_ix(tx)))


@pytest.mark.parametrize("forge", [
    _forge_quote_program, _forge_quote_mint, _vault_not_owned_by_pool, _vault_holds_another_mint,
    _user_quote_owned_by_someone_else, _wsol_quote_on_token_2022, _two_matching_instructions])
def test_an_unverified_quote_stays_unknown(forge):
    tx = _tx()
    forge(tx)
    facts = _value(tx)
    assert facts["source_size_usd"] is None, facts
    assert facts["unknown_reason"] == "quote_or_instruction_identity", facts


def test_a_stale_sol_usd_leaves_the_route_unknown():
    facts = _value(_tx(), sol_usd=(124.0, 60.0), now=105.0)
    assert facts["source_size_usd"] is None
    assert facts["event_unknown_reason"] == "non_sol_quote_amm"
    assert facts["unknown_reason"] == "sol_usd_stale_or_invalid"


def test_a_route_target_other_than_the_verified_base_mint_stays_unknown():
    from sniper.services.memecoin_source_valuation import _value_route
    facts = {"source_size_usd": None, "source_price": None, "source_slot": 1, "source_block_time": 1,
             "source_clock_provenance": "confirmed_getTransaction", "owner": OWNER,
             "unknown_reason": "non_sol_quote_amm", "route_expected_mint": QUOTE}
    out = _value_route(_tx(), OWNER, facts, SOL_USD, 105.0)
    assert out["source_size_usd"] is None
    assert out["unknown_reason"] == "route_target_mismatch"


def test_the_expected_mint_check_is_scoped_to_the_new_reason():
    # The 753be289 intermediate-leg fallback moves from an intermediate event to a DIFFERENT final
    # target by design; only non_sol_quote_amm carries an expected mint.
    from sniper.services.memecoin_source_valuation import _value_route
    facts = {"source_size_usd": None, "source_price": None, "source_slot": 1, "source_block_time": 1,
             "source_clock_provenance": "confirmed_getTransaction", "owner": OWNER,
             "unknown_reason": "output_identity_or_quantity"}
    out = _value_route(_tx(), OWNER, facts, SOL_USD, 105.0)
    assert out.get("unknown_reason") is None and out["mint"] == MINT, out
