"""A two-leg PumpSwap chain leaves the owner a residue of its hop (Alpha GO 2026-10-16).

bigwarzeth's 8 apiANK buys (2026-10-14, USDC -> SOL -> apiANK -> coin via DFlow) kept a little apiANK.
That residue read as a second target (route_target_ambiguous): the watcher valued nothing, proved no
initiation and wrote no row, so no lane ever judged the buy. The hop is now named from the legs: leg 2
spends exactly the mint leg 1 bought, and its residue is never a target."""
import json
import sqlite3
import time

import pytest
from pathlib import Path

from sniper.services import memecoin_source_valuation as V
from sniper.services.memecoin_source_initiation import solana_receipt_initiation

FX = json.loads((Path(__file__).parent / "fixtures/memecoin/pump_amm_chain_buys_20261016.json").read_text())
PAPRBY = "paprbywJSpuiCTq7v8CYrSvtas7JFSYe7SiEMVdJj3t"
APIANK = "apiANKpA5BSUkrqqdEmehJPCUmvo4obWJ6nLxdaGxyL"


def _value(case):
    return V.value_source_buy(FX[case]["transaction"], FX[case]["owner"], sol_usd=150.0, now=time.time())


def test_a_chain_with_a_hop_residue_values_its_coin_and_proves_initiation():
    facts = _value("apiANK")
    assert facts["basis"] == "route_owner_quote_out" and facts.get("unknown_reason") is None
    assert facts["event_unknown_reason"] == "multiple_buy_events" and facts["mint"] == PAPRBY
    hop = facts["route_intermediate"]
    assert hop["mint"].startswith("apiANK") and int(hop["residue_atomic"]) > 0
    # $15 of USDC, pro-rated to the share of the hop that went on into the coin
    assert 14.9 < facts["source_size_usd"] < 15.0
    proof = solana_receipt_initiation(FX["apiANK"]["transaction"], FX["apiANK"]["owner"], facts)
    assert proof is not None and proof["mint"] == PAPRBY


def test_a_chain_without_a_residue_values_as_before():
    facts = _value("starman")
    assert facts["basis"] == "route_owner_quote_out" and facts["source_size_usd"] == 500.0
    assert facts["mint"] == "DiveEYHTf3LveSNifUzeCrTbrZUQEm9PSapERXM8pump"


def test_without_the_hop_the_residue_is_a_second_target(monkeypatch):
    """The defect, kept visible: no named hop, the residue makes the target ambiguous."""
    monkeypatch.setattr(V, "_amm_chain", lambda tx, found: None)
    assert _value("apiANK")["unknown_reason"] == "route_target_ambiguous"


def test_the_watcher_now_writes_the_chain_buys_detection_row(monkeypatch):
    from scripts.research import onchain_wallet_watch as w
    monkeypatch.setattr(w, "_stage_write", lambda *a: None)
    monkeypatch.setattr(w, "match_feed", lambda *a: (None, None))
    monkeypatch.setattr(w, "send_wake", lambda *a: None)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(w.SCHEMA)
    tx, owner = FX["apiANK"]["transaction"], FX["apiANK"]["owner"]
    meta = {"uid": "63993bb2-2afb-50c3-8eca-cb186edbe849", "handle": "bigwarzeth", "status": "armed"}
    assert w.process_transaction(tx["transaction"]["signatures"][0], owner, meta, c, tx=tx,
                                 require_initiation=True, signal_feed="shyft_confirmed:test") is not False
    (row,) = [dict(r) for r in c.execute("SELECT * FROM onchain_wallet_detections")]
    assert row["mint"] == PAPRBY and 14.9 < row["source_size_usd"] < 15.0
    assert json.loads(row["source_valuation_json"])["basis"] == "route_owner_quote_out"


# --- Gamma's pins on the chain predicate (2026-10-16): each guard line returns None on its own case ------

def _synthetic(*, hop="Hop1111111111111111111111111111111111111111", target="Coin111111111111111111111111111111111111111",
               leg2_quote=None, pools=("PoolA", "PoolB"), legs=2):
    """A minimal receipt and found-list for _amm_chain: leg 1 buys `hop` into account a1, leg 2 spends
    account a2 (holding `leg2_quote`, the hop by default) on `target` into account a3."""
    keys = ["owner", "a1", "a2", "a3"]
    mints = {"a1": hop, "a2": hop if leg2_quote is None else leg2_quote, "a3": target}
    tx = {"transaction": {"message": {"accountKeys": keys}}, "meta": {
        "preTokenBalances": [], "loadedAddresses": {},
        "postTokenBalances": [{"accountIndex": keys.index(k), "mint": m} for k, m in mints.items()]}}
    leg1 = {"pool": pools[0], "user_base_token_account": "a1", "user_quote_token_account": "wsol"}
    leg2 = {"pool": pools[1], "user_base_token_account": "a3", "user_quote_token_account": "a2"}
    found = [(V.AMM_PROGRAM, 10, leg1, "l1"), (V.AMM_PROGRAM, 20, leg2, "l2")]
    if legs == 3:
        found.append((V.AMM_PROGRAM, 30, dict(leg2, pool="PoolC"), "l3"))
    return tx, found


def test_the_synthetic_chain_names_its_hop_and_target():
    tx, found = _synthetic()
    assert V._amm_chain(tx, found) == ("Hop1111111111111111111111111111111111111111",
                                       "Coin111111111111111111111111111111111111111")


@pytest.mark.parametrize("label, kw", [
    ("same pool", dict(pools=("PoolA", "PoolA"))),
    ("hop is WSOL", dict(hop=V.WSOL)),
    ("hop is USDC", dict(hop=V.USDC)),
    ("leg 2 spends another mint", dict(leg2_quote="Other11111111111111111111111111111111111111")),
    ("target is the hop", dict(target="Hop1111111111111111111111111111111111111111")),
    ("three legs", dict(legs=3)),
])
def test_anything_but_one_two_leg_chain_is_not_a_chain(label, kw):
    tx, found = _synthetic(**kw)
    assert V._amm_chain(tx, found) is None, label


def test_the_reverse_log_order_is_not_a_chain():
    tx, found = _synthetic()
    found[0], found[1] = (found[0][0], 20) + found[0][2:], (found[1][0], 10) + found[1][2:]
    assert V._amm_chain(tx, found) is None


def test_a_route_resolving_to_another_mint_than_leg_2s_base_is_a_target_mismatch(monkeypatch):
    """The chain binds the target: if the route's single target is not leg 2's base mint, refuse."""
    real = V._amm_chain
    monkeypatch.setattr(V, "_amm_chain", lambda tx, found: (real(tx, found)[0], "Elsewhere111111111111111111111111111111111"))
    assert _value("apiANK")["unknown_reason"] == "route_target_mismatch"


def test_two_pumpswap_legs_beside_a_curve_buy_are_not_a_chain():
    """Gamma LOW: the len(found) term. Two AMM legs plus a pump curve buy event is three own buys."""
    tx, found = _synthetic()
    found.append((V.PUMP_PROGRAM, 30, {"mint": "Curve11111111111111111111111111111111111111"}, "l3"))
    assert V._amm_chain(tx, found) is None
