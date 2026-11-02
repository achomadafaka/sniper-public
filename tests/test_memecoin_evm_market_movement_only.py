"""Ordinary EVM has no proved pre-copy marginal spot: unknown must not become an average cap."""
import json
from types import SimpleNamespace

import pytest

from sniper.execution import evm_swap_executor as evm
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg
from test_memecoin_evm_entry_executor import _QUOTE, _TraceRecorder, _aged_quote, _c, _seams


@pytest.fixture
def market_mode(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_POST_APPROVAL_WINNER_REQUOTE", True)
    monkeypatch.setenv("MEMECOIN_REAL_QUOTE_OVER_SOURCE_MAX_PCT_4663", "30")


def _entry(monkeypatch, *, phase="initial", dip_cap=None, minimum="4000000",
           token=((0.0, 6), (5.0, 6))):
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id="4663",
                            source_price=1.0)
    expensive = {**_QUOTE, "buyAmount": "5000000", "minBuyAmount": minimum,
                 "_sniper_winner": "v3"}
    initial = expensive
    if phase != "initial":
        initial = {**_QUOTE, "buyAmount": "10000000", "minBuyAmount": "9700000",
                   "_sniper_winner": "v3", "_requote": lambda **_: expensive}
        if phase == "refresh":
            initial = {**_aged_quote(5.0, lambda **_: expensive), **initial}
    seams, calls = _seams(quote=initial, token=token,
                          approve={"standing": True} if phase == "refresh" else True)
    signed = []
    original_sign = seams["sign_fn"]
    seams["sign_fn"] = lambda tx: signed.append(tx) or original_sign(tx)
    trace = _TraceRecorder()
    result = evm.execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="4663",
        usd_amount=10.0, strategy="alpha_b", source_user_id="trader1", now_iso="t",
        chase_max_pct=30.0, dip_cap=dip_cap, trace=trace, **seams)
    c.close()
    return result, calls, signed, trace


@pytest.mark.parametrize("phase", ["initial", "refresh", "post_approval"])
def test_unknown_movement_never_reintroduces_source_or_our_buy_impact(market_mode, monkeypatch, phase):
    result, calls, signed, trace = _entry(monkeypatch, phase=phase,
        dip_cap={"reference_price": 0.5, "max_pct": 10.0})
    assert result["status"] == "filled", result
    assert calls["sign"] == calls["send"] == 1
    assert signed and result["source_drift_pct"] == pytest.approx(100.0)
    marks = [data for name, data in trace.substages if name == "copy_market_movement"]
    assert marks and all(mark["state"] == "unknown" for mark in marks)
    assert all(mark["movement_pct"] is None for mark in marks)


def test_mode_off_retains_the_source_relative_refusal(market_mode, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False)
    result, calls, _signed, trace = _entry(monkeypatch)
    assert result["status"] == "skipped_chase" and calls["sign"] == calls["send"] == 0
    assert not any(name == "copy_market_movement" for name, _ in trace.substages)


@pytest.mark.parametrize("enabled", [False, True])
def test_a_dip_source_cap_does_not_block_lifi_under_market_only_policy(market_mode, monkeypatch, enabled):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", enabled)
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="t", network_id="4663", source_price=1.0)
    seams, _calls = _seams(quote=None)
    result = evm.execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="4663",
        usd_amount=10.0, strategy="alpha_b", source_user_id="trader1", now_iso="t",
        dip_cap={"reference_price": 0.5, "max_pct": 10.0},
        quote_miss_fn=lambda: {"code": "no_route", "detail": "none", "rungs": []},
        lifi_entry_fn=lambda _miss: {"status": "aborted", "reason": "lifi_checked"}, **seams)
    assert (result.get("reason") == "lifi_checked") is enabled
    c.close()


@pytest.mark.parametrize("minimum", [None, "0", "5000001"])
def test_unknown_market_movement_keeps_a_valid_signed_minimum_required(market_mode, monkeypatch, minimum):
    result, calls, _signed, _trace = _entry(monkeypatch, minimum=minimum)
    assert result["status"] == "skipped_chase"
    assert "minimum" in result["reason"]
    assert calls["sign"] == calls["send"] == 0


def test_valid_signed_minimum_does_not_require_an_average_quote_price(market_mode, monkeypatch):
    result, calls, _signed, _trace = _entry(monkeypatch, token=(0.0, 5.0))
    assert result["status"] == "filled", result
    assert result["quote_price"] is None and result["source_drift_pct"] is None
    assert calls["sign"] == calls["send"] == 1


@pytest.mark.parametrize("display,source,off,allowed", [(None, 1.0, False, True),
                                                       (1.0, 10.0, False, False),
                                                       (1.0, 10.0, True, True)])
def test_source_premium_is_unknown_but_chart_premium_still_decides(market_mode, monkeypatch,
                                                                  display, source, off, allowed):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E11:4663" if off else "")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 1.5)
    fields = {"funded_contract_json": json.dumps({"classification": {
        "facts": {"display_price": display}}})}
    premium = evm._execution_premium(fields, 2.0, None, attempt_no=1, revision="initial",
        source_price=source, post_trade_price=1.0, network_id="4663")
    assert (evm._premium_refusal(premium) is None) is allowed
    assert premium.reference_basis != "source" and premium.reference_basis != "post_trade"


def test_lifi_unknown_movement_does_not_cap_its_guaranteed_output_average(market_mode, monkeypatch):
    from sniper.execution import lifi_runtime as runtime
    from test_memecoin_lifi_runtime import NOW, TOKEN, WETH, _conn, _runtime
    c = _conn()
    pid = m.reserve_pending(c, mint=TOKEN, source_user_id="wallet", strategy="control",
                            notional=10.0, reserved_at=NOW, network_id="4663", source_price=1.0)
    trace = _TraceRecorder()

    def execute(**kwargs):
        for _revision in ("initial", "refreshed"):
            decision = kwargs["gate_fn"]("chase", SimpleNamespace(output_min_atomic=5 * 10**18), None)
            assert decision.allowed and "unknown" in decision.code
        return {"status": "aborted", "reason": "fixture_checked"}

    monkeypatch.setattr(runtime, "execute_lifi_entry", execute)
    result = _runtime(c)["entry_fn"](conn=c, pending_id=pid, mint=TOKEN, usd_amount=10.0,
        now_iso=NOW, direct_miss={"code": "no_route"}, trace=trace, sell_token=WETH,
        sell_token_usd=2500.0, local={}, safety_gate_fn=lambda: (True, "ok"),
        chase_max_pct=30.0, impact_cap_pct=30.0)
    assert result["reason"] == "fixture_checked"
    marks = [data for name, data in trace.substages if name == "lifi.copy_market_movement"]
    assert len(marks) == 2 and all(mark["state"] == "unknown" for mark in marks)
    c.close()
