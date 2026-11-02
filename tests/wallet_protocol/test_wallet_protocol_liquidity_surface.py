from __future__ import annotations

import dataclasses
import json
import time

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.liquidity import compute_liquidity_surface
from sniper.analysis.wallet_protocol.report import render_report
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _now() -> int:
    return int(time.time())


def _pos(
    *,
    cid: str = "cid-a",
    asset: str = "tok-yes",
    opposite: str = "tok-no",
    outcome: str = "Yes",
    avg_price: float = 0.50,
    cur_price: float = 1.0,
    size: float = 100.0,
    pnl: float = 10.0,
    ts: int = 1_700_000_000,
    **extra,
) -> dict:
    row = {
        "conditionId": cid,
        "asset": asset,
        "oppositeAsset": opposite,
        "outcome": outcome,
        "avgPrice": avg_price,
        "curPrice": cur_price,
        "size": size,
        "totalBought": size * avg_price,
        "realizedPnl": pnl,
        "timestamp": ts,
        "title": f"Market {cid}",
    }
    row.update(extra)
    return row


def _trade(
    *,
    cid: str = "cid-a",
    asset: str = "tok-yes",
    outcome: str = "Yes",
    side: str = "BUY",
    price: float = 0.52,
    size: float = 100.0,
    ts: int = 1_700_000_010,
    volume: float | None = 10_000.0,
    **extra,
) -> dict:
    row = {
        "conditionId": cid,
        "asset": asset,
        "outcome": outcome,
        "side": side,
        "price": price,
        "size": size,
        "usdcSize": price * size,
        "timestamp": ts,
        "type": "TRADE",
        "transactionHash": f"tx-{asset}-{ts}",
        "title": f"Market {cid}",
    }
    if volume is not None:
        row["market_volume_24h"] = volume
    row.update(extra)
    return row


def _data(*, closed_positions=None, activity_rows=None, price_history=None) -> WalletData:
    return WalletData(
        wallet="0xabc",
        closed_positions=list(closed_positions or []),
        open_positions=[],
        activity_rows=list(activity_rows or []),
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=_now(),
        price_history_lookups=price_history,
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


def test_zero_closed_positions_degrades_to_data_unavailable():
    surface = compute_liquidity_surface([], [_trade()], {"tok-yes|1700000010": 0.50})
    assert surface["status"] == "data_unavailable"
    assert "no_closed_positions" in surface["degradation"]["reasons"]


def test_single_market_wallet_is_underpowered_but_reports_observation():
    surface = compute_liquidity_surface([_pos()], [_trade()], {"tok-yes|1700000010": 0.50})
    assert surface["status"] == "underpowered"
    assert surface["summary"]["n_markets"] == 1
    assert surface["market_observations"][0]["condition_id"] == "cid-a"


def test_no_price_history_keeps_slippage_unavailable():
    surface = compute_liquidity_surface([_pos(cur_price=0.50)], [_trade()], {})
    token = surface["market_observations"][0]["tokens"][0]
    assert token["price_history_state"] == "missing"
    assert token["modeled_slippage"]["status"] == "data_unavailable"
    assert token["modeled_slippage"]["weighted_cost_bps"] is None


def test_no_volume_data_is_explicit_inside_surface_only():
    surface = compute_liquidity_surface(
        [_pos()],
        [_trade(volume=None)],
        {"tok-yes|1700000010": 0.50},
    )
    token = surface["market_observations"][0]["tokens"][0]
    assert token["volume_state"] == "missing"
    assert "data_unavailable" in surface["degradation"]["states"]


def test_negative_risk_and_out_of_range_prices_guard_slippage_math():
    surface = compute_liquidity_surface(
        [_pos(avg_price=1.05, negRisk=True)],
        [_trade(price=1.05, negRisk=True)],
        {"tok-yes|1700000010": 0.50},
    )
    token = surface["market_observations"][0]["tokens"][0]
    assert token["negative_risk_guard"] is True
    assert token["modeled_slippage"]["status"] == "underpowered"
    assert "negative_risk_guard" in token["modeled_slippage"]["caveats"]


def test_near_resolution_prices_do_not_report_false_depth_or_slippage():
    surface = compute_liquidity_surface(
        [_pos(avg_price=0.995, cur_price=1.0)],
        [_trade(price=0.995)],
        {"tok-yes|1700000010": 0.9995},
    )
    token = surface["market_observations"][0]["tokens"][0]
    assert token["near_resolution"] is True
    assert token["modeled_slippage"]["weighted_cost_bps"] is None
    assert "near_resolution" in token["modeled_slippage"]["caveats"]


def test_price_history_state_distinguishes_guard_exclusion_from_missing():
    """Regression (reviewer finding, 2026-05-21): a token whose price history
    was CAPTURED but discarded by a guard must report price_history_state
    'excluded_by_guard', not 'missing'. The earlier form collapsed the two
    and implied a lookup failure that did not happen."""
    # Near-resolution: the mid IS present in the lookup dict; the guard
    # discards the row, so `modeled` is empty but history was NOT missing.
    guarded = compute_liquidity_surface(
        [_pos(avg_price=0.995, cur_price=1.0)],
        [_trade(price=0.995)],
        {"tok-yes|1700000010": 0.9995},
    )
    g_token = guarded["market_observations"][0]["tokens"][0]
    assert "near_resolution" in g_token["caveats"]
    assert g_token["price_history_state"] == "excluded_by_guard"

    # Genuinely missing: no price-history entry for the lookup key.
    missing = compute_liquidity_surface(
        [_pos(avg_price=0.50, cur_price=0.55)],
        [_trade(price=0.52)],
        {},
    )
    m_token = missing["market_observations"][0]["tokens"][0]
    assert m_token["price_history_state"] == "missing"


def test_multi_market_wallet_reports_every_market_exactly_once():
    """Regression guard for the per-cid asset index (O(markets), not
    O(markets^2)): every distinct market must appear exactly once with its
    token observation. Output must be identical to the pre-optimization scan."""
    n = 25
    positions = [_pos(cid=f"cid-{i}", asset=f"tok-{i}") for i in range(n)]
    trades = [_trade(cid=f"cid-{i}", asset=f"tok-{i}") for i in range(n)]
    surface = compute_liquidity_surface(positions, trades, {})
    cids = [m["condition_id"] for m in surface["market_observations"]]
    assert sorted(cids) == sorted(f"cid-{i}" for i in range(n))
    assert len(cids) == len(set(cids))  # each market exactly once
    for m in surface["market_observations"]:
        assert [t["token_id"] for t in m["tokens"]] == [
            f"tok-{m['condition_id'].split('-')[1]}"
        ]


def test_trade_size_far_larger_than_volume_proxy_caps_pressure():
    surface = compute_liquidity_surface(
        [_pos()],
        [_trade(price=0.52, size=1000, volume=10)],
        {"tok-yes|1700000010": 0.50},
    )
    token = surface["market_observations"][0]["tokens"][0]
    assert token["size_pressure"]["max_trade_to_volume_24h"] > 1.0
    assert token["size_pressure"]["capped"] is True
    assert "trade_size_exceeds_volume_proxy" in token["caveats"]


def test_zero_price_and_zero_midpoint_do_not_divide_by_zero():
    surface = compute_liquidity_surface(
        [_pos(avg_price=0.0, cur_price=0.0)],
        [_trade(price=0.0)],
        {"tok-yes|1700000010": 0.0},
    )
    token = surface["market_observations"][0]["tokens"][0]
    assert token["modeled_slippage"]["weighted_cost_bps"] is None
    assert "zero_price_guard" in token["modeled_slippage"]["caveats"]


def test_fixtures_predating_surface_analyze_cleanly():
    data = _data(closed_positions=[_pos()], activity_rows=[])
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100, seed=1)
    assert report.liquidity_surface["status"] == "data_unavailable"
    assert "no_trade_rows" in report.liquidity_surface["degradation"]["reasons"]


def test_replay_determinism_same_fixture_same_liquidity_surface_json():
    data = _data(
        closed_positions=[_pos(), _pos(cid="cid-b", asset="tok-b", opposite="tok-b-no")],
        activity_rows=[_trade(), _trade(cid="cid-b", asset="tok-b", ts=1_700_000_020)],
        price_history={"tok-yes|1700000010": 0.50, "tok-b|1700000020": 0.55},
    )
    first = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100, seed=1)
    second = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100, seed=1)
    assert json.dumps(first.liquidity_surface, sort_keys=True) == json.dumps(
        second.liquidity_surface, sort_keys=True
    )


def test_degradation_states_render_in_markdown_and_json():
    report = ProtocolReport(
        wallet="0xabc",
        liquidity_surface={
            "surface_version": "liquidity_surface_v1",
            "status": "proxy_only",
            "fidelity_tier": "proxy_only_no_historical_depth",
            "historical_depth_available": False,
            "summary": {
                "n_markets": 1,
                "n_trade_rows": 1,
                "weighted_execution_cost_bps": None,
                "liquidity_read": "proxy_only",
            },
            "degradation": {
                "states": ["proxy_only", "underpowered", "data_unavailable"],
                "reasons": ["historical_depth_unavailable"],
            },
            "market_observations": [],
        },
    )
    md = render_report(report)
    assert "## Liquidity surface" in md
    assert "proxy_only" in md
    assert "underpowered" in md
    assert "data_unavailable" in md
    assert "proxy_only" in dataclasses.asdict(report)["liquidity_surface"]["degradation"]["states"]


def test_large_wallet_compute_is_linear_in_price_lookup_accesses():
    class CountingDict(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.get_count = 0

        def get(self, key, default=None):
            self.get_count += 1
            return super().get(key, default)

    n = 2_000
    trades = [
        _trade(cid=f"cid-{i % 50}", asset=f"tok-{i % 50}", ts=1_700_000_000 + i)
        for i in range(n)
    ]
    closed = [
        _pos(cid=f"cid-{i}", asset=f"tok-{i}", opposite=f"tok-no-{i}")
        for i in range(50)
    ]
    lookups = CountingDict({f"tok-{i % 50}|{1_700_000_000 + i}": 0.50 for i in range(n)})
    surface = compute_liquidity_surface(closed, trades, lookups)
    assert surface["summary"]["n_trade_rows"] == n
    assert lookups.get_count <= n


def test_yes_no_token_liquidity_asymmetry_is_reported_per_market():
    surface = compute_liquidity_surface(
        [
            _pos(asset="tok-yes", opposite="tok-no", outcome="Yes"),
            _pos(asset="tok-no", opposite="tok-yes", outcome="No", pnl=-5.0),
        ],
        [
            _trade(asset="tok-yes", outcome="Yes", size=100, volume=10_000),
            _trade(asset="tok-no", outcome="No", size=100, volume=100, ts=1_700_000_020),
        ],
        {"tok-yes|1700000010": 0.50, "tok-no|1700000020": 0.48},
    )
    asym = surface["market_observations"][0]["token_asymmetry"]
    assert asym["state"] == "observed"
    assert asym["max_pressure_ratio"] > 1.0
    assert {item["outcome"] for item in asym["tokens"]} == {"Yes", "No"}


def test_liquidity_surface_does_not_alter_verdict_copyability_or_quality_flags():
    closed = [_pos(cid=f"cid-{i}", asset=f"tok-{i}", opposite=f"opp-{i}", pnl=5.0) for i in range(40)]
    activity = [
        _trade(cid=f"cid-{i}", asset=f"tok-{i}", ts=1_700_000_000 + i)
        for i in range(40)
    ]
    enriched = _data(
        closed_positions=closed,
        activity_rows=activity,
        price_history={f"tok-{i}|{1_700_000_000 + i}": 0.50 for i in range(40)},
    )
    base = _data(closed_positions=closed, activity_rows=[], price_history=None)
    r_base = analyze_wallet("0xabc", pre_fetched_data=base, n_iter=100, seed=1)
    r_enriched = analyze_wallet("0xabc", pre_fetched_data=enriched, n_iter=100, seed=1)
    assert r_enriched.liquidity_surface is not None
    assert _signature(r_base) == _signature(r_enriched)
