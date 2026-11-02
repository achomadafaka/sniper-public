"""Tests for CSE-F1 entity layering (selection vs execution grain)."""
from __future__ import annotations

import json
from pathlib import Path

from sniper.analysis.counterfactual_selection.entity_layers import (
    build_engagements, build_clips, build_sessions, build_baskets,
    selection_cadence, selection_schedule, execution_intensity,
)

REPO = Path(__file__).resolve().parents[2]


def _buy(cid, asset, price, size, ts, outcome="YES", category="sports"):
    return {"type": "TRADE", "side": "BUY", "conditionId": cid, "asset": asset,
            "price": price, "size": size, "timestamp": ts, "outcome": outcome,
            "market_category": category}


def _laddered(cid, asset, base_ts, n, *, gap=5, price=0.4, size=10):
    return [_buy(cid, asset, price, size, base_ts + i * gap) for i in range(n)]


def test_engagement_is_per_market_not_per_fill():
    rows = _laddered("0xA", "a", 1_000_000, 5) + _laddered("0xB", "b", 1_003_600, 8)
    eng = build_engagements(rows)
    assert len(eng) == 2                      # 13 fills → 2 engagements (selection unit)
    a = next(e for e in eng if e["conditionId"] == "0xA")
    assert a["n_fills"] == 5 and a["entry_vwap"] == 0.4
    assert eng[0]["nth_market"] == 1 and eng[1]["nth_market"] == 2


def test_clips_split_on_gap():
    rows = _laddered("0xA", "a", 1_000_000, 3, gap=5) + _laddered("0xA", "a", 1_000_500, 2, gap=5)
    clips = build_clips(rows, gap_s=120)
    assert len(clips) == 2                     # 500s gap > 120 → two clips
    eng = build_engagements(rows)
    assert eng[0]["n_clips"] == 2 and eng[0]["n_fills"] == 5


def test_selection_vs_execution_cadence_separated():
    # 2 markets entered 1h apart; each laddered with 5 fills 5s apart.
    rows = _laddered("0xA", "a", 1_000_000, 5, gap=5) + _laddered("0xB", "b", 1_003_600, 5, gap=5)
    eng = build_engagements(rows)
    sel = selection_cadence(eng)
    ex = execution_intensity(eng)
    assert sel["median_gap_s"] == 3600         # SELECTION: 1h between new markets
    assert ex["median_fills_per_market"] == 5  # EXECUTION: 5 fills/market
    assert sel["median_gap_s"] > 100 * 5       # selection ≫ execution


def test_selection_schedule_hours():
    # entries at UTC hours 3 and 4
    rows = [_buy("0xA", "a", 0.4, 10, 1_000_000 + 3 * 3600),
            _buy("0xB", "b", 0.4, 10, 1_000_000 + 4 * 3600)]
    eng = build_engagements(rows)
    sched = selection_schedule(eng)
    assert sched["hours_covered"] == 2 and sched["dead_hours"] == 22


def test_baskets_cluster_co_entries():
    rows = [_buy("0xA", "a", 0.4, 10, 1_000_000), _buy("0xB", "b", 0.4, 10, 1_000_030),  # together
            _buy("0xC", "c", 0.4, 10, 1_100_000)]                                          # later
    baskets = build_baskets(build_engagements(rows), gap_s=600)
    assert len(baskets) == 2 and baskets[0]["n_markets"] == 2


def test_sessions_break_on_gap():
    rows = _laddered("0xA", "a", 1_000_000, 3, gap=5) + _laddered("0xB", "b", 1_010_000, 3, gap=5)
    sessions = build_sessions(rows, gap_s=3600)
    assert len(sessions) == 2


def test_empty_input():
    assert build_engagements([]) == []
    assert selection_cadence([]) == {"n_markets": 0, "median_gap_s": None,
                                     "new_markets_per_hour": None, "span_hours": 0.0}


def test_real_swissmiss_fixture_selection_exceeds_execution():
    """On committed SwissMiss data, the SELECTION grain (new-market cadence) is far
    slower than the EXECUTION grain (fill cadence) — reproduces the live probe and
    is the evidence she selects by hand and ladders by bot."""
    fx = REPO / "tests/calibration/fixtures/0xdbade4c82fb72780a0db9a38f821d8671aba9c95_2026-05-13.json"
    if not fx.exists():
        return  # fixture optional in some checkouts
    data = json.loads(fx.read_text())
    eng = build_engagements(data.get("activity_rows") or [])
    assert len(eng) > 20
    sel = selection_cadence(eng)
    ex = execution_intensity(eng)
    # new-market entries are minutes-to-hours apart; fills are seconds apart.
    assert sel["median_gap_s"] is not None and sel["median_gap_s"] > 300
    assert ex["median_fills_per_market"] >= 2
    assert sel["median_gap_s"] > ex["median_fills_per_market"]  # selection grain ≫ execution
