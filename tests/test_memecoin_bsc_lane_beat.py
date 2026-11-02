"""The 56 lane's 60 s beat row publishes its paced host and wallet snapshot counters (Alpha 2026-10-02,
GAP 1): `live.paced` and the snapshot reader counted calls, 429s, reads and errors that no row ever
showed. One JSON column, `lane_json`, added to the existing table at lane build (never on the
listener thread between datagrams); NULL while the lane is runner_shadow (no paced host, no
snapshot)."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_bsc_fast_lane as bsc
from test_memecoin_bsc_fast_lane_shadow import lane56  # noqa: F401  (fixture)

OLD_BEAT = ("CREATE TABLE bsc_lane_beat (at TEXT NOT NULL, signals INTEGER, drops TEXT, dex_calls INTEGER, "
            "dex_429 INTEGER, dex_stopped TEXT)")


def _columns(path):
    with sqlite3.connect(path) as c:
        return [r[1] for r in c.execute("PRAGMA table_info(bsc_lane_beat)")]


def test_the_beat_column_is_added_to_an_existing_table_once(tmp_path):
    path = str(tmp_path / "lane.db")
    with sqlite3.connect(path) as c:
        c.execute(OLD_BEAT)
    bsc.ensure_beat_schema(path)
    bsc.ensure_beat_schema(path)                                  # idempotent
    assert _columns(path)[-1] == "lane_json" and _columns(path).count("lane_json") == 1


def _lane(tmp_path, live):
    dispatch = SimpleNamespace(drops={}, counts={"signals": 3})
    dex = SimpleNamespace(counts={"calls": 4, "http_429": 1}, stopped=None)
    return {"dispatch": dispatch, "dex": dex, "live": live, "db_path": str(tmp_path / "lane.db")}


def test_a_live_beat_carries_the_paced_and_snapshot_counters(tmp_path):
    import collections
    paced = SimpleNamespace(counts={"calls": 12, "http_429": 2, "transport_errors": 4, "dead_marked": 1},
                            dead=lambda: True)
    snapshot = SimpleNamespace(counts=collections.Counter(reads=58, errors=1))
    lane = _lane(tmp_path, SimpleNamespace(paced=paced, snapshot=snapshot))
    bsc.ensure_beat_schema(lane["db_path"])
    bsc.write_beat(lane)
    with sqlite3.connect(lane["db_path"]) as c:
        got = json.loads(c.execute("SELECT lane_json FROM bsc_lane_beat").fetchone()[0])
    # Beta's dead-host counters (230aed62e): transport failures, times marked dead, dead right now.
    assert got == {"paced_calls": 12, "paced_http_429": 2, "paced_transport_errors": 4,
                   "paced_dead_marked": 1, "paced_dead": 1, "snapshot_reads": 58, "snapshot_errors": 1,
                   "presignal_warm_enabled": 0}


def test_a_snapshot_with_no_read_yet_says_zero_and_a_shadow_lane_says_null(tmp_path):
    import collections
    lane = _lane(tmp_path, SimpleNamespace(paced=SimpleNamespace(counts={"calls": 0, "http_429": 0},
                                                                 dead=lambda: False),
                                           snapshot=SimpleNamespace(counts=collections.Counter())))
    bsc.ensure_beat_schema(lane["db_path"])
    bsc.write_beat(lane)
    bsc.write_beat(_lane(tmp_path, None))
    with sqlite3.connect(lane["db_path"]) as c:
        rows = [r[0] for r in c.execute("SELECT lane_json FROM bsc_lane_beat ORDER BY rowid")]
    assert json.loads(rows[0])["snapshot_reads"] == 0 and rows[1] is None
    assert not {"verdicts_ok", "verdicts_unsafe", "verdict_errors"}.intersection(json.loads(rows[0]))


@pytest.mark.parametrize("completed", [{}, {"verdicts_ok": 7, "verdicts_unsafe": 3, "verdict_errors": 2}])
def test_live_beat_distinguishes_completed_safety_checks_from_zero_or_unknown(tmp_path, completed):
    import collections
    cache = SimpleNamespace(counts=collections.Counter(completed), preparation_counts=lambda **kw: {
        "candidates_active": 4, "candidates_ready": 0, "candidates_queued": 4})
    lane = _lane(tmp_path, SimpleNamespace(cache=cache, generation_fn=lambda: 0.0))
    bsc.ensure_beat_schema(lane["db_path"])
    bsc.write_beat(lane)
    with sqlite3.connect(lane["db_path"]) as c:
        got = json.loads(c.execute("SELECT lane_json FROM bsc_lane_beat").fetchone()[0])
    assert got == {"presignal_warm_enabled": 0, "candidate_evicted": 0, "candidate_expired": 0,
                   "candidate_identity_changed": 0, "preparation_cancelled": 0,
                   "preparation_skipped": 0, "preparation_request_attempts": 0,
                   "unknown_first_encounter": 0, "safety_hit": 0, "safety_miss": 0,
                   "ready_at_original_ingress": 0, "warm_set": 0, "warm_routed": 0,
                   "candidates_active": 4,
                   "candidates_ready": 0, "candidates_queued": 4,
                   "verdicts_ok": completed.get("verdicts_ok", 0),
                   "verdicts_unsafe": completed.get("verdicts_unsafe", 0),
                   "verdict_errors": completed.get("verdict_errors", 0)}


def test_the_lane_build_adds_the_column_before_any_beat(lane56):  # noqa: F811
    lane = lane56[0]
    assert "lane_json" in _columns(lane["db_path"])
