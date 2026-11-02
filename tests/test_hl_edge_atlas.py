from __future__ import annotations

import sqlite3

from sniper.hyperlive import edge_atlas as EA
from sniper.hyperlive import intel_store as IS


DAY_MS = 86_400_000
NOW_MS = 20 * DAY_MS


def _follow(*, days_ago, coin="PEPE", side="short", bps=100.0,
            reason="mirror_exit", hold_hours=2.0):
    exit_ts = NOW_MS - int(days_ago * DAY_MS)
    return {
        "coin": coin,
        "side": side,
        "net_bps": bps,
        "exit_reason": reason,
        "exit_ts_ms": exit_ts,
        "detect_ts_ms": exit_ts - int(hold_hours * 3_600_000),
    }


def _copy(*, days_ago, coin="PEPE", side="short", bps=100.0, cold=False):
    return {
        "coin": coin,
        "side": side,
        "pnl_bps": bps,
        "exit_ts_ms": NOW_MS - int(days_ago * DAY_MS),
        "detect_ts_ms": NOW_MS - int(days_ago * DAY_MS) - 3_600_000,
        "is_cold_start": int(cold),
    }


def test_build_edge_atlas_uses_fixed_cells_and_day_block_intervals():
    rows = [
        _follow(days_ago=10, coin="BTC", side="long", bps=-20),
        _follow(days_ago=9, coin="BTC", side="short", bps=-50),
        _follow(days_ago=8, coin="PEPE", side="long", bps=-30),
        _follow(days_ago=7, coin="PEPE", side="short", bps=100),
        _follow(days_ago=6, coin="WIF", side="short", bps=200),
        _follow(days_ago=5, coin="BONK", side="short", bps=-50),
        _follow(days_ago=4, coin="UNKNOWN", side="flat", bps=999),
    ]

    atlas = EA.build_edge_atlas(rows, [], [], now_ms=NOW_MS)
    cells = {cell["id"]: cell for cell in atlas["cells"]}

    assert list(cells) == ["core_long", "core_short", "alt_long", "alt_short"]
    assert cells["core_long"]["history"]["mean_bps"] == -20.0
    assert cells["core_short"]["history"]["mean_bps"] == -50.0
    assert cells["alt_long"]["history"]["mean_bps"] == -30.0
    assert cells["alt_short"]["history"] == {
        "n": 3,
        "mean_bps": 83.33,
        "median_bps": 100.0,
        "sum_bps": 250.0,
        "win_rate": 0.667,
        "day_blocks": 3,
        "ci95_bps": [-50.0, 200.0],
    }
    assert "samples" not in cells["alt_short"]["history"]
    assert atlas["excluded_rows"] == 1


def test_recent_pulse_marks_drift_degraded_and_keeps_week_order():
    recent = [
        _follow(
            days_ago=(i % 7) + 0.1,
            bps=(-500 if i < 5 else 40),
            reason=("time_cap" if i < 5 else "mirror_exit"),
            hold_hours=(20 if i < 5 else 3),
        )
        for i in range(35)
    ]
    older = [_follow(days_ago=8 + i, bps=120) for i in range(15)]

    atlas = EA.build_edge_atlas(recent + older, [], [], now_ms=NOW_MS)
    focus = atlas["focus"]

    assert focus["status"] == "degraded"
    assert focus["recent_7d"]["n"] == 35
    assert focus["recent_7d"]["mean_bps"] < 0
    assert 1 <= len(focus["weekly"]) <= 6
    assert [row["week_start_ts_ms"] for row in focus["weekly"]] == sorted(
        row["week_start_ts_ms"] for row in focus["weekly"]
    )
    assert focus["data_through_ts_ms"] == max(row["exit_ts_ms"] for row in recent)


def test_exit_gap_separates_source_exits_time_cap_and_future_hold_buckets():
    follows = [
        _follow(days_ago=1, bps=120, reason="mirror_exit", hold_hours=2),
        _follow(days_ago=1, bps=80, reason="mirror_exit", hold_hours=6),
        _follow(days_ago=1, bps=-700, reason="time_cap", hold_hours=20),
        _follow(days_ago=1, bps=50, reason="manual", hold_hours=50),
    ]
    copies = [
        _copy(days_ago=1, bps=180),
        _copy(days_ago=1, bps=999, cold=True),
        _copy(days_ago=1, coin="BTC", bps=500),
    ]
    arm_rows = [{"status": "open"}, {"status": "closed"}, {"status": "closed"}]

    atlas = EA.build_edge_atlas(follows, copies, arm_rows, now_ms=NOW_MS)
    exits = atlas["exit_gap"]

    assert exits["by_reason"]["mirror_exit"]["mean_bps"] == 100.0
    assert exits["by_reason"]["time_cap"]["mean_bps"] == -700.0
    assert exits["by_reason"]["other"]["n"] == 1
    assert exits["hold_buckets"]["1-4h"]["n"] == 1
    assert exits["hold_buckets"]["4-12h"]["n"] == 1
    assert exits["hold_buckets"]["12-48h"]["mean_bps"] == -700.0
    assert exits["hold_time_warning"].startswith("Outcome attribution")
    assert atlas["copy_comparison"]["recent_clean_copy"]["mean_bps"] == 180.0
    assert atlas["copy_comparison"]["warning"].startswith("Closed-copy result excludes open")
    assert atlas["forward"] == {
        "open": 1,
        "closed": 2,
        "total": 3,
        "status": "accruing",
        "confirmation": "insufficient_forward_closes",
    }
    assert "source-directed wallet exits" in atlas["next_direction"]["question"]


def test_read_edge_atlas_fails_locally_when_required_ledger_is_missing():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    atlas = EA.read_edge_atlas(conn, current_arm_id=None, now_ms=NOW_MS)

    assert atlas == {"available": False, "reason": "schema_missing"}


def test_read_edge_atlas_does_not_infer_forward_arm_when_runtime_is_unverified():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE hl_follow_positions(status TEXT, net_bps REAL);"
        "CREATE TABLE hl_copy_book(status TEXT, pnl_bps REAL);"
    )

    atlas = EA.read_edge_atlas(conn, current_arm_id=None, now_ms=NOW_MS)

    assert atlas["available"] is True
    assert atlas["forward"] == {
        "open": None,
        "closed": None,
        "total": None,
        "status": "unknown",
        "confirmation": "current_arm_unverified",
    }


def test_read_edge_atlas_aggregates_semantically_identical_arm_versions(tmp_path):
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    manifest = {
        "arm_id": "forward-v1", "policy": {"alt_short_only": True, "cost_bps": 39},
        "targets": ["0xabc"], "taxonomy_version": "alt-v1",
        "benchmark_version": "loo-v1", "regime_version": "causal-v1",
    }
    IS.create_altshort_arm(
        conn, manifest, activated_ts_ms=1, code_sha="aaa", created_ts_ms=1
    )
    IS.create_altshort_arm(
        conn, {**manifest, "arm_id": "forward-v2"}, activated_ts_ms=2,
        code_sha="bbb", created_ts_ms=2,
    )
    for copy_id, arm_id, status in ((1, "forward-v1", "closed"), (2, "forward-v2", "open")):
        conn.execute(
            "INSERT INTO hl_altshort_observations("
            "copy_id,arm_id,target,coin,side,detect_ts_ms,entry_mark,regime_json,"
            "benchmark_entry_json,status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (copy_id, arm_id, "0xabc", "ALT", "short", copy_id, 1.0, "{}", "{}", status),
        )
    conn.commit()

    atlas = EA.read_edge_atlas(conn, current_arm_id="forward-v2", now_ms=NOW_MS)

    assert atlas["forward"] == {
        "open": 1,
        "closed": 1,
        "total": 2,
        "status": "accruing",
        "confirmation": "insufficient_forward_closes",
    }
