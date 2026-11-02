"""The desk native rung's exit probe uses Codex's native_curve_sell on the curve AFTER our buy (2026-10-23).
Pre-buy, a sell of the tokens we are about to buy can need more SOL than the curve holds (curve_real_sol_short):
our own buy's SOL is what pays it. Post-buy state = reserves + curve_buy_net(spend), tokens - expected out."""
import base64
import copy
import sqlite3
import struct

import pytest
from solders.keypair import Keypair

from sniper.execution import desk_native_curve as D
from sniper.execution import pump_curve_buy as P
from sniper.execution import pump_native_exit as X
from sniper.execution.solana_native_entry_quote import accounts_for

MINT = "Hj8T9wWjsfzQ6ARzrnmFrDGvGqXA4P6nYgoXyx7Spump"
STATE = {"virtual_sol_reserves": 30_000_000_000, "real_sol_reserves": 100_000_000,
         "virtual_token_reserves": 1_000_000_000_000_000, "real_token_reserves": 790_000_000_000_000,
         "fee_basis_points": 95, "creator_fee_basis_points": 30, "creator": "C" * 43 + "x",
         "token_program": P.TOKEN_PROGRAM, "complete": False, "mayhem_mode": False, "is_cashback_coin": False,
         "is_holder_reward": False, "quote_mint": X.SYSTEM_PROGRAM}


def _curve_account(creator_fee_bps=0):
    raw = bytearray(166)
    struct.pack_into("<Q", raw, 115, creator_fee_bps)
    return {"data": [base64.b64encode(bytes(raw)).decode(), "base64"]}


def _info(spend=500_000_000):
    return {"spend_lamports": spend, "expected_tokens_out": P.expected_tokens_out(spend, STATE),
            "min_tokens_out": P.expected_tokens_out(spend, STATE) * 95 // 100}


def test_post_buy_state_adds_our_net_sol_and_removes_our_tokens():
    info = _info()
    net = P.curve_buy_net(500_000_000, STATE)
    s = D.post_buy_sell_state(MINT, STATE, _curve_account(7), info, ("F", "R", "B"), True)
    assert s["virtual_sol_reserves"] == STATE["virtual_sol_reserves"] + net
    assert s["real_sol_reserves"] == STATE["real_sol_reserves"] + net
    assert s["virtual_token_reserves"] == STATE["virtual_token_reserves"] - info["expected_tokens_out"]
    assert (s["curve"], s["curve_layout_size"], s["creator_fee_bps"], s["creator_fee_configurable"],
            s["fee_recipient"], s["buyback_fee_recipient"]) == (accounts_for(MINT)["curve"], 166, 7, True, "F", "B")


def test_pre_buy_state_refuses_the_sell_post_buy_state_quotes_it():
    info = _info()
    amount = info["min_tokens_out"]
    pre, why = X.curve_sell_quote(STATE, amount, 25.0, protocol_bps=95, creator_bps=30)
    assert pre is None and why == "curve_real_sol_short"
    post = D.post_buy_sell_state(MINT, STATE, _curve_account(), info, ("F", "R", "B"), True)
    quote, why = X.curve_sell_quote(post, amount, 25.0, protocol_bps=95, creator_bps=30)
    assert why is None and quote["gross_out"] <= post["real_sol_reserves"]


def test_unknown_inputs_reach_the_sell_as_none_so_it_refuses():
    s = D.post_buy_sell_state(MINT, STATE, None, _info(), None, None)
    assert s["curve_layout_size"] is None and s["creator_fee_bps"] is None and s["fee_recipient"] is None
    assert s["creator_fee_configurable"] is None


def test_global_read_keeps_creator_fee_configurable():
    from test_pump_curve_fee_recipient import FX
    acc = copy.deepcopy(FX["global_account"])
    raw = bytearray(base64.b64decode(acc["data"][0]))
    for flag in (0, 1):
        raw[1045] = flag
        acc["data"][0] = base64.b64encode(bytes(raw)).decode()
        b = P.SlotLaneCurveBuilder(user=FX["payer"], rpc=lambda m, p: {"value": copy.deepcopy(acc)})
        b._read_global()
        assert b._creator_fee_configurable is bool(flag) and b._fee_config is not None
    raw[1045] = 7
    acc["data"][0] = base64.b64encode(bytes(raw)).decode()
    b = P.SlotLaneCurveBuilder(user=FX["payer"], rpc=lambda m, p: {"value": copy.deepcopy(acc)})
    b._read_global()
    assert b._creator_fee_configurable is None


def test_seam_desk_rung_probes_native_curve_sell_on_the_post_buy_state(monkeypatch):
    from test_desk_native_curve import _seam
    conn, pid, ex, m = _seam(monkeypatch, "no_route")
    info = _info()
    sell_in = {"state": STATE, "curve_account": _curve_account(), "fee_config": ("F", "R", "B")}
    monkeypatch.setattr(D, "desk_curve_entry", lambda *a, **k: (k["fee_config_fn"](), ("MSG", {   # warms the Global
        "native_curve": info, "native_curve_ms": {}, "_sell_inputs": sell_in}))[1])
    seen = {}

    def fake_sell(mint, amount, slip, **kw):
        seen.update(kw, mint=mint, amount=amount)
        return None
    monkeypatch.setattr(X, "native_curve_sell", fake_sell)
    monkeypatch.setattr(P.SlotLaneCurveBuilder, "_creator_fee_configurable", True, raising=False)
    monkeypatch.setattr(P.SlotLaneCurveBuilder, "_read_global", lambda self: setattr(self, "_fee_config", ("F", "R", "B")))
    monkeypatch.setattr(X, "build_native_exit", lambda *a, **k: pytest.fail("the desk rung must not re-read the curve"))
    out = ex["execute_fn"](conn, pid, MINT, 10.0, "t", desk_price_cap={"ref_price_usd": 1.0, "cap_pct": 5.0})
    assert out["status"] == "aborted" and "native exit does not build" in out["reason"]
    assert seen["simulate"] is False and seen["amount"] == info["min_tokens_out"]
    assert seen["state"]["real_sol_reserves"] == STATE["real_sol_reserves"] + P.curve_buy_net(info["spend_lamports"], STATE)
    assert m.pending_row(conn, pid)["signed_payload_b64"] is None


def test_cold_start_global_not_ready_refuses_with_its_own_reason(monkeypatch):
    from test_desk_native_curve import _seam
    conn, pid, ex, m = _seam(monkeypatch, "no_route")
    sell_in = {"state": STATE, "curve_account": _curve_account(), "fee_config": ("F", "R", "B")}
    monkeypatch.setattr(D, "desk_curve_entry", lambda *a, **k: ("MSG", {
        "native_curve": _info(), "native_curve_ms": {}, "_sell_inputs": sell_in}))
    monkeypatch.setattr(X, "native_curve_sell", lambda *a, **k: pytest.fail("no probe before Global is read"))
    out = ex["execute_fn"](conn, pid, MINT, 10.0, "t", desk_price_cap={"ref_price_usd": 1.0, "cap_pct": 5.0})
    assert out["status"] == "aborted" and "global_not_ready" in out["reason"]
