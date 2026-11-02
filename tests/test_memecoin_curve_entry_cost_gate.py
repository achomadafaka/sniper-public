"""Audit E2: a curve ENTRY must meet the same round-trip cost policy as every other venue.

The launchpad-curve rung entered the route list unwrapped, so its hit carried no
`route.round_trip_pct`, and the ladder accepts an unmeasured hit when no measured one exists — even
with MEMECOIN_EVM_ENTRY_MEASURED_LADDER on. The curve's own safety wrapper only asks for a positive
whole-quantity sell quote and never calls `round_trip_within_budget`, so neither path enforced the
cost policy the venue spec requires (2026-09-17 curve design, Exit section item 5).

THE FIXTURE IS THE AUDIT'S: a native-counter curve, 100 bps fee, reserves of 1 ETH against
1,000,000 tokens, and a 1 ETH entry. The curve builders measure a same-state round trip of about
-67% on it, which `round_trip_within_budget` rejects.

READ THAT NUMBER CORRECTLY. It is a same-state PRE-TRADE measure — buy the clip and sell it
straight back against unchanged reserves — the same convention the other direct rungs are judged
on. It is not a prediction that this entry would realize a 67% loss.

The tests run the WHOLE ladder, never the quote builder alone: the defect was that the producer
never set the field, and a test that injects the field cannot see that.
"""
import sniper.execution.evm_launchpad_curve as curve_mod
from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback

WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
MINT = "0x5e9ca04c6fe6397f287091130a63072a91afd14f"
CURVE = "0xd1255489d894452dd234a001f0fbcab6d4037040"
NATIVE = "0x" + "00" * 20
ONE_ETH = 10 ** 18


def _on(monkeypatch, *, measured=True):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", measured)


def _curve(monkeypatch, *, weth_reserve, token_reserve, fee_bps=100):
    def state_fn(chain, mint, rpc_url, http=None):
        if str(mint).lower() != MINT:
            return None
        return {"coin": MINT, "curve": CURVE, "counter": NATIVE, "fee_bps": fee_bps, "state": 0}

    def reserves_fn(curve, rpc_url, http=None):
        return (weth_reserve, token_reserve)

    monkeypatch.setattr(curve_mod, "curve_state", state_fn)
    monkeypatch.setattr(curve_mod, "curve_reserves", reserves_fn)


def _ladder(rungs, **kw):
    return venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain="4663", quote_token=WETH, taker="0xtaker",
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append, **kw)


def _curve_rung(rungs):
    return next(r for r in rungs if r["rung"] == "launchpad_curve")


def test_the_ladder_refuses_a_curve_entry_whose_round_trip_is_over_budget(monkeypatch):
    """The audit's fixture, through the real ladder: 1 ETH into a 1 ETH / 1,000,000 token curve."""
    _on(monkeypatch)
    _curve(monkeypatch, weth_reserve=ONE_ETH, token_reserve=1_000_000 * 10 ** 18)
    rungs = []
    assert _ladder(rungs)(WETH, MINT, ONE_ETH) is None, "an over-budget curve entry must not fill"
    rung = _curve_rung(rungs)
    assert rung["hit"] is False
    assert str(rung.get("reason") or "").startswith("round_trip_over_budget"), rung
    measured = float(str(rung["reason"]).split(":")[1])
    assert -95.0 < measured < -50.0, "the audit measured about -67%% here, got %s" % measured


def test_a_healthy_curve_entry_still_fills_and_carries_its_round_trip(monkeypatch):
    """Not a switch-off. A curve whose round trip is ordinary still wins the rung, and the route
    now carries the evidence, so this rung can be compared with the venues it competes against."""
    _on(monkeypatch)
    _curve(monkeypatch, weth_reserve=1689900000000000001, token_reserve=994141665187289188709391089)
    rungs = []
    result = _ladder(rungs)(WETH, MINT, 10 ** 16)
    assert result is not None and result["route"]["protocol"] == "launchpad_curve"
    route = result["route"]
    assert route.get("round_trip_pct") is not None, "the hit must carry its measured cost"
    assert route.get("round_trip_measured_by") == "launchpad_curve"
    assert route["round_trip_pct"] > -50.0
    assert _curve_rung(rungs)["hit"] is True


def test_an_exit_is_never_refused_on_cost(monkeypatch):
    """The asymmetry every other venue keeps: the alternative to an expensive exit is a bag we
    cannot sell at all. The same curve that refuses the entry above must still quote the sell."""
    _on(monkeypatch)
    _curve(monkeypatch, weth_reserve=ONE_ETH, token_reserve=1_000_000 * 10 ** 18)
    rungs = []
    result = _ladder(rungs)(MINT, WETH, 1_000 * 10 ** 18)          # selling the coin
    assert result is not None, "a curve exit must not be gated on round-trip cost"
    assert result["route"]["protocol"] == "launchpad_curve"
    assert "round_trip_pct" not in (result["route"] or {})
    assert _curve_rung(rungs)["hit"] is True


def test_the_gate_is_the_ladder_s_own_switch(monkeypatch):
    """With the measured ladder off, the rung keeps its old behaviour, so this change cannot
    silently become a second, always-on curve policy."""
    _on(monkeypatch, measured=False)
    _curve(monkeypatch, weth_reserve=ONE_ETH, token_reserve=1_000_000 * 10 ** 18)
    rungs = []
    assert _ladder(rungs)(WETH, MINT, ONE_ETH) is not None
    assert _curve_rung(rungs)["hit"] is True
