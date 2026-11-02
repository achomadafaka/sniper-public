"""The chase gate measures what we control: our quote vs the source's POST-trade price.

Spec docs/superpowers/specs/2026-09-28-memecoin-chase-post-trade-reference-design.md. Shape case:
3310 (attempt 5a4a32af, DegenCapitalLLC 2 x $2k): average fill 7.74e-5, our quote 1.39e-4, "+80%
above source (cap 30)". The source's own buy moved the pool; the post-trade price is the fair base.
"""
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_onchain_lane as lane
from sniper.settings import memecoin as cfg
from sniper.services import memecoin_real_runner as rr
from sniper.services.memecoin_source_valuation import value_source_buy

FIX = Path(__file__).parent / "fixtures/memecoin/confirmed_source_buys.json"


def receipt(name):
    tx = json.loads(FIX.read_text())[name]
    return tx, tx["meta"]["postTokenBalances"][0]["owner"]


@pytest.mark.parametrize("name", ["untaxxable", "notdecu-later"])       # PumpSwap, pump curve
def test_real_receipts_give_a_post_trade_price_at_or_above_the_average(name):
    tx, owner = receipt(name)
    f = value_source_buy(tx, owner, sol_usd=(120.0, 100.0), now=105.0)
    assert f["source_impact_ratio"] >= 1.0
    assert f["source_post_price"] == pytest.approx(f["source_price"] * f["source_impact_ratio"])


def test_no_usd_price_still_gives_the_unit_free_ratio_but_no_post_price():
    tx, owner = receipt("notdecu-later")
    f = value_source_buy(tx, owner, sol_usd=None, now=105.0)
    assert f["source_price"] is None and f["source_post_price"] is None
    assert f["source_impact_ratio"] >= 1.0


def _row(valuation, source_price=7.743e-5):
    return {"mint": "m", "source_user_id": "u", "handle": "h", "network_id": "solana",
            "source_size_usd": 2000.0, "source_price": source_price, "signature": "sig",
            "detected_at": "2026-09-28T14:13:24+00:00",
            "source_valuation_json": json.dumps(valuation)}


def test_event_carries_the_post_trade_reference_only_when_enabled(monkeypatch):
    val = {"source_post_price": 1.25e-4, "source_impact_ratio": 1.614}
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "avg_fill", raising=False)
    assert lane.OnchainEv(_row(val)).chase_reference_price is None
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade", raising=False)
    ev = lane.OnchainEv(_row(val))
    assert ev.chase_reference_price == pytest.approx(1.25e-4)
    assert ev.their_price == pytest.approx(7.743e-5)      # the average fill stays the source price
    assert ev.chase_reference_basis == "post_trade"


@pytest.mark.parametrize("val", [{}, {"source_post_price": None}, {"source_post_price": 0},
                                 {"source_post_price": "nan"}, {"source_post_price": -1}])
def test_absent_or_invalid_post_price_falls_back_to_the_average(monkeypatch, val):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade", raising=False)
    ev = lane.OnchainEv(_row(val))
    assert ev.chase_reference_price is None and ev.chase_reference_basis == "avg_fill"


def test_retry_payload_keeps_the_reference(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade", raising=False)
    ev = lane.OnchainEv(_row({"source_post_price": 1.25e-4}))
    back = lane.event_from_retry_payload(lane.retry_payload(ev))
    assert back.chase_reference_price == pytest.approx(1.25e-4)


def test_pre_quote_gap_uses_the_reference():
    class Ev:
        their_price = 7.743e-5
        chase_reference_price = 1.25e-4
    assert rr.chase_gap_pct(Ev(), our_price=1.3934e-4) == pytest.approx((1.3934e-4 / 1.25e-4 - 1) * 100)
    Ev.chase_reference_price = None
    assert rr.chase_gap_pct(Ev(), our_price=1.3934e-4) == pytest.approx(79.95, abs=0.1)


def test_executor_exact_quote_gate_admits_3310_on_the_post_trade_reference():
    from sniper.execution.solana_swap_executor import chase_reference
    assert chase_reference(7.743e-5, None) == (pytest.approx(7.743e-5), "avg_fill")
    assert chase_reference(7.743e-5, 1.25e-4) == (pytest.approx(1.25e-4), "post_trade")
    assert chase_reference(7.743e-5, 0) == (pytest.approx(7.743e-5), "avg_fill")
    quote = 1.3934e-4
    ref, _ = chase_reference(7.743e-5, None)
    assert (quote / ref - 1) * 100 > 30                       # today: refused
    ref, _ = chase_reference(7.743e-5, 1.25e-4)
    assert (quote / ref - 1) * 100 < 30                       # post-trade: admitted


def test_executor_end_to_end_post_trade_reference_admits_and_average_refuses():
    """Through execute_entry, on the recheck test's harness: quote 0.02 vs source average 0.01 is
    +100% (refused before signing); the same quote vs a post-trade reference of 0.018 is +11%."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))      # `tests.` resolves to a stray package here
    from test_memecoin_entry_chase_quote_recheck import _c, _Deps
    from sniper.execution import solana_swap_executor as ex

    def run(ref):
        c, pid = _c(source_price=0.01)
        d = _Deps()
        r = ex.execute_entry(
            c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
            source_user_id="u1", now_iso="2026-08-19T00:00:00Z",
            quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn,
            sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
            usdc_fn=d.usdc_fn, token_fn=d.token_fn, impact_cap_pct=12.0,
            chase_max_pct=30.0, chase_reference_price=ref)
        return r, d
    r, d = run(None)
    assert r["status"] == "skipped_chase" and "sign" not in d.calls
    r, d = run(0.018)
    assert r["status"] != "skipped_chase" and "sign" in d.calls


def test_no_impact_allowance_on_a_post_trade_reference():
    calls = []

    class Ev:
        mint, their_price, their_size_usd = "m", 7.743e-5, 2000.0
        chase_reference_price = 1.25e-4
    got = rr.chase_impact_allowance(Ev(), network_id="solana", quote_fn=lambda usd: calls.append(usd))
    assert got == (None, rr.IMPACT_POST_TRADE_REFERENCE) and calls == []    # no quote spent
    Ev.chase_reference_price = None
    rr.chase_impact_allowance(Ev(), network_id="solana", quote_fn=lambda usd: calls.append(usd))
    assert calls                                                           # the old path quotes


def test_reference_is_clamped_at_the_max_ratio(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_CHASE_POST_TRADE_MAX_RATIO", 2.0, raising=False)
    ev = lane.OnchainEv(_row({"source_post_price": 5e-4}, source_price=1e-4))     # ratio 5
    assert ev.chase_reference_price == pytest.approx(2e-4) and ev.chase_reference_basis == "post_trade_clamped"
    ev = lane.OnchainEv(_row({"source_post_price": 1.25e-4}))                      # 3310, ratio ~1.6
    assert ev.chase_reference_basis == "post_trade"


def test_oversize_retry_keeps_the_reference():
    import ast
    import inspect
    from sniper.execution import solana_swap_executor as ex
    tree = ast.parse(inspect.getsource(ex.execute_entry))
    inner = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "execute_entry"]
    assert inner, "the oversize retry re-calls execute_entry"
    for call in inner:
        assert "chase_reference_price" in {k.arg for k in call.keywords}
