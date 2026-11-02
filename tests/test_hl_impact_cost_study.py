"""Impact-cost kill test: cost identity, causal volume mapping, and fail-closed decision."""
import sqlite3

import pytest

from scripts.research import hl_impact_cost_study as study


def test_bid_to_ask_is_one_roundtrip_and_is_not_doubled():
    row = {"mid_px": 100.0, "impact_bid": 99.9, "impact_ask": 100.2}
    assert study.valid_spread(row) == pytest.approx(30.0)


@pytest.mark.parametrize(
    "row",
    [
        {"mid_px": 0.0, "impact_bid": 99.9, "impact_ask": 100.2},
        {"mid_px": 100.0, "impact_bid": None, "impact_ask": 100.2},
        {"mid_px": 100.0, "impact_bid": 100.2, "impact_ask": 99.9},
    ],
)
def test_invalid_depth_is_missing_not_zero(row):
    assert study.valid_spread(row) is None


def test_recharge_replaces_thirty_bps_but_keeps_every_other_term():
    event = {"coin": "ALT", "residual_full_bps": 190.0}
    charged = study.recharge_event(event, 70.0)
    assert charged["impact_recharged_residual_bps"] == 150.0
    assert charged["assigned_impact_bps"] == 70.0
    assert charged["coin"] == "ALT"


def test_event_volume_uses_only_twenty_four_completed_pre_entry_bars():
    series = {"t": list(range(25)), "c": [2.0] * 25, "v": [3.0] * 25}
    assert study.event_quote_volume(series, 24) == 144.0


def test_event_volume_requires_a_complete_positive_window():
    short = {"t": list(range(24)), "c": [2.0] * 24, "v": [3.0] * 24}
    zero = {"t": list(range(25)), "c": [2.0] * 25, "v": [0.0] * 25}
    assert study.event_quote_volume(short, 23) is None
    assert study.event_quote_volume(zero, 24) is None


def test_liquidity_tiers_are_coin_weighted_and_clamp_extremes():
    rows = []
    for i in range(25):
        for ts in (1, 2):
            rows.append({
                "coin": f"C{i:02d}", "ts_ms": ts,
                "mid_px": 100.0, "impact_bid": 100.0 - i / 100.0,
                "impact_ask": 100.0 + i / 100.0,
                "day_ntl_vlm": float((i + 1) * 1_000),
            })
    tiers = study.build_liquidity_tiers(rows)
    assert [tier["coins"] for tier in tiers] == [5, 5, 5, 5, 5]
    assert study.assign_tier(1.0, tiers)["tier"] == 1
    assert study.assign_tier(1_000_000.0, tiers)["tier"] == 5


def test_tape_reader_is_cutoff_bounded_and_read_only(tmp_path):
    path = tmp_path / "tape.db"
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE hl_squeeze_marks (
            ts_ms INTEGER, coin TEXT, mid_px REAL, impact_bid REAL, impact_ask REAL,
            day_ntl_vlm REAL, is_delisted INTEGER
        )
    """)
    conn.executemany(
        "INSERT INTO hl_squeeze_marks VALUES (?,?,?,?,?,?,?)",
        [(100, "A", 100, 99, 101, 1_000, 0), (101, "A", 100, 98, 102, 1_000, 0)],
    )
    conn.commit()
    conn.close()

    rows = study.load_tape_rows(path, cutoff_ms=100)
    assert [row["ts_ms"] for row in rows] == [100]


def test_decision_fails_closed_on_coverage_small_tier_or_nonpositive_ci():
    assert not study.decision({"coverage": 0.94, "min_tier_coins": 20, "p75_ci_low": 1})["pass"]
    assert not study.decision({"coverage": 1.0, "min_tier_coins": 19, "p75_ci_low": 1})["pass"]
    assert not study.decision({"coverage": 1.0, "min_tier_coins": 20, "p75_ci_low": 0})["pass"]
    assert study.decision({"coverage": 1.0, "min_tier_coins": 20, "p75_ci_low": 0.1})["pass"]


def test_charge_events_maps_pre_entry_volume_to_the_requested_tier_cost():
    tiers = [{
        "tier": 1, "coins": 20, "volume_min": 1.0, "volume_max": 1_000.0,
        "spread_median_bps": 20.0, "spread_p75_bps": 40.0, "spread_p90_bps": 60.0,
    }]
    series = {"t": list(range(25)), "c": [2.0] * 25, "v": [3.0] * 25}
    events = [{"coin": "ALT", "entry_ts": 24, "residual_full_bps": 190.0}]
    charged, uncovered = study.charge_events(
        events, {"ALT": series}, tiers, spread_field="spread_p75_bps"
    )
    assert uncovered == []
    assert charged[0]["event_quote_volume"] == 144.0
    assert charged[0]["liquidity_tier"] == 1
    assert charged[0]["assigned_impact_bps"] == 40.0
    assert charged[0]["impact_recharged_residual_bps"] == 180.0


def test_charge_events_reports_uncovered_volume_instead_of_zero_cost():
    event = {"coin": "ALT", "entry_ts": 1, "residual_full_bps": 190.0}
    charged, uncovered = study.charge_events([event], {"ALT": {"t": [1], "c": [2], "v": [3]}},
                                              [], spread_field="spread_p75_bps")
    assert charged == []
    assert uncovered == [{"coin": "ALT", "entry_ts": 1, "reason": "event_volume_missing"}]
