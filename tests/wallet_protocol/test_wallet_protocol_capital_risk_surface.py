"""Acceptance tests — Capital & Risk surface (`capital_risk_surface`).

Spec: docs/superpowers/specs/2026-05-22-capital-risk-surface.md

Display-only surface: peak concurrent deployment (capital to play), max
drawdown (capital to survive), recycle factor, return-on-base, PnL
concentration. Hard wall identical to liquidity_surface — must never feed
verdict / gate / primary_edge_type / copyability_note / data_quality_flags.

RED until `compute_capital_risk_surface` exists and is wired into
`analyze_wallet` + `ProtocolReport.capital_risk_surface`.
"""
from __future__ import annotations

import dataclasses
import json
import time

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.capital_risk import compute_capital_risk_surface
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _now() -> int:
    return int(time.time())


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def _trade(side: str, cid: str, ts: int, *, price: float = 0.5,
           size: float = 100.0, usdc: float | None = None,
           asset: str | None = None) -> dict:
    # asset defaults to one-per-condition so per-asset capital tracking is
    # exercised correctly; pass `asset` explicitly for multi-token markets.
    row = {"type": "TRADE", "side": side, "conditionId": cid,
           "asset": asset or f"tok-{cid}",
           "price": price, "size": size, "timestamp": ts}
    if usdc is not None:
        row["usdcSize"] = usdc
    return row


def _redeem(cid: str, usdc: float, ts: int) -> dict:
    return {"kind": "redemption", "condition": cid, "usdc": usdc,
            "timestamp": ts}


def _ledger(events: list[dict]) -> dict:
    return {"events": list(events)}


def _pos(cid: str, pnl: float, *, ts: int = 1_700_000_000,
         end_date: str | None = None) -> dict:
    row = {"conditionId": cid, "realizedPnl": pnl, "timestamp": ts,
           "outcome": "Yes", "avgPrice": 0.5, "curPrice": 1.0}
    if end_date is not None:
        row["endDate"] = end_date
    return row


def _data(*, closed_positions=None, activity_rows=None,
          capital_ledger=None) -> WalletData:
    return WalletData(
        wallet="0xabc",
        closed_positions=list(closed_positions or []),
        open_positions=[],
        activity_rows=list(activity_rows or []),
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=_now(),
        capital_ledger=capital_ledger,
        replay_surface_meta={
            "archetype_layer1": {"status": "skipped"},
            "execution_profile": {"status": "skipped"},
            "price_history_lookups": {"status": "skipped"},
            "leafs_by_cid": {"status": "skipped"},
            "nav_lookup": {"status": "skipped"},
            "peer_baselines": {"status": "skipped"},
        },
    )


def _signature(report: ProtocolReport) -> tuple:
    return (
        report.overall_verdict,
        report.gate_result,
        report.primary_edge_type,
        report.copyability_note,
        tuple(report.data_quality_flags),
    )


# ==========================================================================
# §4.1 — peak / cumulative / recycle
# ==========================================================================
def test_peak_is_max_concurrent_two_overlapping_positions():
    """Two positions open at once → peak = sum of both."""
    activity = [
        _trade("BUY", "a", 100, usdc=100.0),
        _trade("BUY", "b", 110, usdc=100.0),
    ]
    ledger = _ledger([_redeem("a", 0.0, 200), _redeem("b", 0.0, 210)])
    s = compute_capital_risk_surface([_pos("a", -100.0), _pos("b", -100.0)],
                                     activity, ledger, net_pnl=-200.0)
    assert s["peak_concurrent_deployed_usdc"] == 200.0
    assert s["cumulative_deployed_usdc"] == 200.0
    assert s["recycle_factor"] == 1.0


def test_recycle_factor_counts_sequential_reuse():
    """Same pool reused: deploy→redeem→deploy→redeem → peak 100, recycle 2."""
    activity = [
        _trade("BUY", "a", 100, usdc=100.0),
        _trade("BUY", "b", 300, usdc=100.0),
    ]
    ledger = _ledger([_redeem("a", 150.0, 200), _redeem("b", 150.0, 400)])
    s = compute_capital_risk_surface([_pos("a", 50.0), _pos("b", 50.0)],
                                     activity, ledger, net_pnl=100.0)
    assert s["peak_concurrent_deployed_usdc"] == 100.0
    assert s["cumulative_deployed_usdc"] == 200.0
    assert s["recycle_factor"] == 2.0


def test_sell_release_capped_at_committed():
    """A winning SELL returns more than committed; only the committed
    portion is a capital release — peak stays at the committed amount."""
    activity = [
        _trade("BUY", "a", 100, usdc=100.0),
        _trade("SELL", "a", 200, usdc=300.0),  # sold for 300, committed 100
    ]
    s = compute_capital_risk_surface([_pos("a", 200.0)], activity,
                                     _ledger([]), net_pnl=200.0)
    assert s["peak_concurrent_deployed_usdc"] == 100.0


def test_losing_sell_full_exit_releases_cost_basis_not_proceeds():
    """Codex review blocker: a full losing exit must release the position's
    COST BASIS, not the sale proceeds. buy A $100 → sell all of A for $50
    (loss) → buy B $100. A is fully closed, so peak concurrent = $100, not
    $150 (proceeds-based release would leave $50 phantom committed in A)."""
    activity = [
        _trade("BUY", "a", 100, usdc=100.0, size=100.0),
        _trade("SELL", "a", 200, usdc=50.0, size=100.0),   # full exit, a loss
        _trade("BUY", "b", 300, usdc=100.0, size=100.0),
    ]
    s = compute_capital_risk_surface(
        [_pos("a", -50.0), _pos("b", 0.0)], activity, _ledger([]),
        net_pnl=-50.0)
    assert s["peak_concurrent_deployed_usdc"] == 100.0


def test_partial_losing_sell_releases_proportional_cost_basis():
    """Selling half a position at a loss releases half the cost basis; a
    later buy stacks on the remaining half."""
    activity = [
        _trade("BUY", "a", 100, usdc=100.0, size=100.0),
        _trade("SELL", "a", 200, usdc=20.0, size=50.0),    # half, at a loss
        _trade("BUY", "b", 300, usdc=70.0, size=70.0),
    ]
    s = compute_capital_risk_surface(
        [_pos("a", -30.0), _pos("b", 0.0)], activity, _ledger([]),
        net_pnl=-30.0)
    # after the half-sell: 50 still committed in A; + 70 in B = 120 concurrent
    assert s["peak_concurrent_deployed_usdc"] == 120.0


def test_redemption_releases_remaining_committed():
    """Held-to-resolution loser: BUY commits, redemption (usdc=0) releases."""
    activity = [_trade("BUY", "a", 100, usdc=100.0),
                _trade("BUY", "b", 150, usdc=100.0)]
    ledger = _ledger([_redeem("a", 0.0, 300)])  # 'a' resolves, 'b' still open
    s = compute_capital_risk_surface([_pos("a", -100.0), _pos("b", -100.0)],
                                     activity, ledger, net_pnl=-200.0)
    # peak was 200 (both open); after 'a' redeems, 100 still committed in 'b'
    assert s["peak_concurrent_deployed_usdc"] == 200.0


def test_usdcsize_and_price_times_size_yield_identical_curve():
    """REST rows carry usdcSize; subgraph rows carry price+size. Same peak."""
    rest = [_trade("BUY", "a", 100, usdc=120.0)]
    sub = [_trade("BUY", "a", 100, price=0.6, size=200.0)]  # 0.6*200 = 120
    s_rest = compute_capital_risk_surface([_pos("a", 0.0)], rest, _ledger([]), 0.0)
    s_sub = compute_capital_risk_surface([_pos("a", 0.0)], sub, _ledger([]), 0.0)
    assert s_rest["peak_concurrent_deployed_usdc"] == 120.0
    assert s_sub["peak_concurrent_deployed_usdc"] == 120.0


# ==========================================================================
# §4.2 — max drawdown
# ==========================================================================
def _many(n: int, cid_prefix: str = "c") -> list[dict]:
    """n closed positions + matching activity/ledger, for floor tests."""
    closed, activity, events = [], [], []
    for i in range(n):
        cid = f"{cid_prefix}{i}"
        closed.append(_pos(cid, 1.0, ts=1_700_000_000 + i))
        activity.append(_trade("BUY", cid, 1_700_000_000 + i, usdc=10.0))
        events.append(_redeem(cid, 11.0, 1_700_000_500 + i))
    return closed, activity, events


def test_max_drawdown_zero_for_monotonic_winner():
    closed, activity, events = _many(30)
    s = compute_capital_risk_surface(closed, activity, _ledger(events),
                                     net_pnl=30.0)
    assert s["max_drawdown_usdc"] == 0.0


def test_max_drawdown_captures_underwater_trough():
    """Equity path +100, -300, +500 → trough at -200 below the +100 peak."""
    closed = [
        _pos("a", 100.0, end_date="2026-01-01T00:00:00Z"),
        _pos("b", -300.0, end_date="2026-02-01T00:00:00Z"),
        _pos("c", 500.0, end_date="2026-03-01T00:00:00Z"),
    ]
    activity = [_trade("BUY", c, 100 + i, usdc=50.0)
                for i, c in enumerate(("a", "b", "c"))]
    s = compute_capital_risk_surface(closed, activity, _ledger([]),
                                     net_pnl=300.0)
    assert s["max_drawdown_usdc"] == 300.0


def test_max_drawdown_pct_none_when_no_peak():
    """No BUY cashflow → peak None → pct ratio None, not a crash."""
    closed = [_pos("a", 10.0)]
    s = compute_capital_risk_surface(closed, [], _ledger([]), net_pnl=10.0)
    assert s["max_drawdown_pct_of_peak"] is None


# ==========================================================================
# §4.3 — PnL concentration
# ==========================================================================
def test_pnl_concentration_flags_a_few_lucky_hits():
    closed = ([_pos("big", 10000.0)]
              + [_pos(f"s{i}", 10.0) for i in range(20)])
    activity = [_trade("BUY", "big", 100, usdc=50.0)] + [
        _trade("BUY", f"s{i}", 100 + i, usdc=50.0) for i in range(20)]
    s = compute_capital_risk_surface(closed, activity, _ledger([]),
                                     net_pnl=10200.0)
    pc = s["pnl_concentration"]
    assert pc["bucket"] == "concentrated"
    assert pc["top5_share_of_gross_profit"] > 0.6


def test_pnl_concentration_broad_when_spread():
    closed = [_pos(f"s{i}", 100.0) for i in range(40)]
    activity = [_trade("BUY", f"s{i}", 100 + i, usdc=50.0) for i in range(40)]
    s = compute_capital_risk_surface(closed, activity, _ledger([]),
                                     net_pnl=4000.0)
    assert s["pnl_concentration"]["bucket"] == "broad"


def test_pnl_concentration_gini_none_under_two_winners():
    closed = [_pos("a", 50.0)] + [_pos(f"L{i}", -5.0) for i in range(25)]
    activity = ([_trade("BUY", "a", 100, usdc=50.0)]
                + [_trade("BUY", f"L{i}", 100 + i, usdc=5.0) for i in range(25)])
    s = compute_capital_risk_surface(closed, activity, _ledger([]),
                                     net_pnl=-75.0)
    assert s["pnl_concentration"]["gini"] is None


# ==========================================================================
# §5 — data_state
# ==========================================================================
def test_data_state_unavailable_with_no_closed_positions():
    s = compute_capital_risk_surface([], [], _ledger([]), net_pnl=0.0)
    assert s["data_state"] == "data_unavailable"


def test_data_state_underpowered_below_floor():
    closed, activity, events = _many(10)  # < 20
    s = compute_capital_risk_surface(closed, activity, _ledger(events), 10.0)
    assert s["data_state"] == "underpowered"


def test_data_state_ok_on_clean_full_computation():
    closed, activity, events = _many(40)
    # two winners minimum for concentration; _many gives all +1.0 winners
    s = compute_capital_risk_surface(closed, activity, _ledger(events), 40.0)
    assert s["data_state"] == "ok"


def test_data_state_proxy_only_when_drawdown_ordering_approximate():
    """No redemption events → close time falls back to endDate/timestamp →
    proxy_only, with a caveat."""
    closed, activity, _events = _many(40)
    s = compute_capital_risk_surface(closed, activity, _ledger([]), 40.0)
    assert s["data_state"] == "proxy_only"
    assert any("DRAWDOWN_ORDERING_APPROXIMATE" in c for c in s["caveats"])


# ==========================================================================
# §4.4 / §6 — return-on-base, division guards, hard wall
# ==========================================================================
def test_return_on_peak_deployed_basic():
    closed, activity, events = _many(40)
    s = compute_capital_risk_surface(closed, activity, _ledger(events),
                                     net_pnl=200.0)
    # peak 10 (positions are sequential single-buy in _many's timeline)
    assert s["return_on_peak_deployed"] == 200.0 / s["peak_concurrent_deployed_usdc"]


def test_division_guards_no_crash_on_zero_peak():
    s = compute_capital_risk_surface([_pos("a", 5.0)], [], _ledger([]), 5.0)
    assert s["peak_concurrent_deployed_usdc"] is None
    assert s["recycle_factor"] is None
    assert s["return_on_peak_deployed"] is None


def test_surface_is_json_serializable_and_deterministic():
    closed, activity, events = _many(40)
    a = compute_capital_risk_surface(closed, activity, _ledger(events), 40.0)
    b = compute_capital_risk_surface(closed, activity, _ledger(events), 40.0)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ==========================================================================
# Integration — analyze_wallet wiring + hard wall
# ==========================================================================
def test_field_present_on_live_path():
    closed, activity, events = _many(40)
    data = _data(closed_positions=closed, activity_rows=activity)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100, seed=1)
    assert isinstance(report.capital_risk_surface, dict)
    assert "data_state" in report.capital_risk_surface


def test_hard_wall_surface_never_mutates_verdict(monkeypatch):
    """Replace the producer with a garbage-returning stub: verdict, gate,
    primary_edge_type, copyability_note and data_quality_flags must be
    byte-identical — proving the surface is display-only."""
    closed, activity, events = _many(40)
    data = _data(closed_positions=closed, activity_rows=activity)
    baseline = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100, seed=1)

    # analyze_wallet imports the producer function-locally, so patch the
    # source module — the function-local `from capital_risk import ...`
    # picks up the patched binding at call time.
    import sniper.analysis.wallet_protocol.capital_risk as cr

    monkeypatch.setattr(
        cr, "compute_capital_risk_surface",
        lambda *a, **k: {"data_state": "ok", "GARBAGE": 9.99e9},
    )
    poisoned = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100, seed=1)
    # The poison must actually have reached the surface — otherwise this test
    # could false-pass if the monkeypatch silently stopped taking effect.
    assert poisoned.capital_risk_surface.get("GARBAGE") == 9.99e9
    assert _signature(poisoned) == _signature(baseline)
    assert "GARBAGE" not in str(poisoned.data_quality_flags)


def test_backward_compat_protocolreport_without_field():
    """A ProtocolReport constructed without capital_risk_surface defaults
    to None and serializes cleanly."""
    rep = ProtocolReport(wallet="0xabc")
    assert rep.capital_risk_surface is None


# ==========================================================================
# Reviewer fixes (platform-reviewer adversarial pass, 2026-05-22)
# ==========================================================================
def test_no_trade_cashflow_is_underpowered_not_unavailable():
    """Reviewer Critical #1: closed positions but no trade cashflow — the
    capital-base half is dead, but the realized-PnL equity curve IS
    computable. data_state must be `underpowered` (numbers shipped), NOT
    `data_unavailable` (which would imply nothing was computed)."""
    closed = [_pos(f"c{i}", (50.0 if i % 2 else -30.0), ts=1_700_000_000 + i)
              for i in range(25)]
    s = compute_capital_risk_surface(closed, [], _ledger([]), net_pnl=250.0)
    assert s["data_state"] == "underpowered"
    assert s["max_drawdown_usdc"] is not None        # equity curve computed
    assert s["pnl_concentration"] is not None
    assert s["peak_concurrent_deployed_usdc"] is None  # capital base dead


def test_unmapped_fills_degrade_to_proxy_only():
    """Reviewer should-fix / spec §5: n_unmapped_fills > 0 means BUY/SELL
    cashflow is under-counted → proxy_only with a caveat."""
    closed, activity, events = _many(40)
    s = compute_capital_risk_surface(closed, activity, _ledger(events),
                                     net_pnl=40.0, n_unmapped_fills=137)
    assert s["data_state"] == "proxy_only"
    assert any("UNMAPPED_FILLS" in c for c in s["caveats"])


def test_drawdown_curve_is_fetch_order_independent():
    """Reviewer Important #3: two positions sharing a close timestamp must
    produce the same drawdown regardless of closed-list order — the sort
    tiebreaks on conditionId."""
    p_lo = _pos("aaa", -400.0, end_date="2026-02-01T00:00:00Z")
    p_hi = _pos("bbb", 100.0, end_date="2026-02-01T00:00:00Z")
    pre = _pos("z_first", 100.0, end_date="2026-01-01T00:00:00Z")
    act = [_trade("BUY", c, 100 + i, usdc=50.0)
           for i, c in enumerate(("z_first", "aaa", "bbb"))]
    s1 = compute_capital_risk_surface([pre, p_lo, p_hi], act, _ledger([]), -200.0)
    s2 = compute_capital_risk_surface([pre, p_hi, p_lo], act, _ledger([]), -200.0)
    assert s1["max_drawdown_usdc"] == s2["max_drawdown_usdc"]


# ==========================================================================
# Re-review fixes (platform-reviewer SELL-rewrite pass, 2026-05-22)
# ==========================================================================
def test_buy_missing_size_derives_shares_from_price():
    """Re-review Important #1: a BUY row with no `size` (subgraph
    irregularity) derives shares from cost/price so committed and shares
    stay in sync — a later full SELL releases the whole cost basis, leaving
    no phantom committed capital."""
    activity = [
        _trade("BUY", "a", 100, usdc=100.0, size=0.0, price=0.5),   # → 200 sh
        _trade("SELL", "a", 200, size=200.0),                        # full exit
        _trade("BUY", "b", 300, usdc=100.0, size=100.0),
    ]
    s = compute_capital_risk_surface(
        [_pos("a", -10.0), _pos("b", 0.0)], activity, _ledger([]),
        net_pnl=-10.0)
    assert s["peak_concurrent_deployed_usdc"] == 100.0


def test_untracked_buy_degrades_to_proxy_only():
    """Re-review Important #1: a BUY with cost but neither size nor price →
    no derivable shares → UNTRACKED_BUYS caveat + proxy_only."""
    closed, activity, events = _many(25)
    activity.append(_trade("BUY", "untrk", 1_700_000_900,
                           usdc=50.0, size=0.0, price=0.0))
    closed.append(_pos("untrk", 5.0, ts=1_700_000_900))
    events.append(_redeem("untrk", 55.0, 1_700_001_400))
    s = compute_capital_risk_surface(closed, activity, _ledger(events),
                                     net_pnl=205.0)
    assert s["data_state"] == "proxy_only"
    assert any("UNTRACKED_BUYS" in c for c in s["caveats"])


def test_condition_id_join_is_case_insensitive():
    """Re-review Important #2: the activity↔ledger condition join must
    survive 0x-case differences between REST and subgraph sources. A
    case-mismatched redemption must still release the position."""
    activity = [
        _trade("BUY", "0xABC123", 100, usdc=100.0, size=100.0),
        _trade("BUY", "b", 300, usdc=100.0, size=100.0),
    ]
    ledger = _ledger([_redeem("0xabc123", 0.0, 200)])  # lowercase, before B
    s = compute_capital_risk_surface(
        [_pos("0xABC123", -100.0), _pos("b", -100.0)], activity, ledger,
        net_pnl=-200.0)
    # A redeems (case-mismatched key) before B's buy. Join works → A
    # released → peak 100. Join fails → A phantom → peak 200.
    assert s["peak_concurrent_deployed_usdc"] == 100.0


# ==========================================================================
# Codex review #3 — missing SELL size (symmetric to the BUY-size fix)
# ==========================================================================
def test_sell_missing_size_derives_shares_from_price():
    """Codex review: a SELL row with no `size` (subgraph irregularity)
    derives its share count from proceeds/price so it still releases the
    position. buy A $100 → sell all of A (size omitted, proceeds $50 @
    price 0.5 → 100 shares) → buy B $100. A fully exits → peak $100, not
    $200 (no phantom committed capital)."""
    activity = [
        _trade("BUY", "a", 100, usdc=100.0, size=100.0),
        _trade("SELL", "a", 200, usdc=50.0, price=0.5, size=0.0),  # → 100 sh
        _trade("BUY", "b", 300, usdc=100.0, size=100.0),
    ]
    s = compute_capital_risk_surface(
        [_pos("a", -50.0), _pos("b", 0.0)], activity, _ledger([]),
        net_pnl=-50.0)
    assert s["peak_concurrent_deployed_usdc"] == 100.0


def test_untracked_sell_against_held_position_degrades_to_proxy_only():
    """Codex review: a SELL against a held position with no derivable share
    count (no size, no price, no proceeds) cannot release capital →
    UNTRACKED_SELLS caveat + proxy_only, rather than a silent overstated
    peak."""
    closed, activity, events = _many(25)
    activity.append(_trade("BUY", "stuck", 1_700_000_900,
                           usdc=100.0, size=100.0))
    activity.append(_trade("SELL", "stuck", 1_700_000_950,
                           usdc=0.0, price=0.0, size=0.0))   # underivable
    closed.append(_pos("stuck", -100.0, ts=1_700_000_900))
    events.append(_redeem("stuck", 0.0, 1_700_001_400))
    s = compute_capital_risk_surface(closed, activity, _ledger(events),
                                     net_pnl=100.0)
    assert s["data_state"] == "proxy_only"
    assert any("UNTRACKED_SELLS" in c for c in s["caveats"])
