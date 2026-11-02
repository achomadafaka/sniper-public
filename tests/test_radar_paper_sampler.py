import json

from sniper.research.memecoin_opportunity.paper_completion import PaperQuoteCompletion
from sniper.research.memecoin_opportunity.paper_sampler import (
    DETAIL_VERSION,
    SOURCE_KEY,
    PaperSampler,
)
from sniper.research.memecoin_opportunity.store import connect
from sniper.research.memecoin_opportunity.projection import upsert_runtime_status


def _request(observation_id, bucket, *, entry="entry-1", venue="pumpfun_solana"):
    return {
        "observation_id": observation_id,
        "request_id": entry,
        "venue_id": venue,
        "scheduled_age_ms": bucket,
    }


def _completion(observation_id, requested_at, completed_at, *, venue="pumpfun_solana"):
    return PaperQuoteCompletion(
        observation_id=observation_id,
        venue_id=venue,
        status="filled",
        economics={"expected_out_atomic": 25_000_000},
        failure_reason=None,
        requested_at=requested_at,
        completed_at=completed_at,
    )


def test_sampler_measures_round_trip_and_concurrent_buckets_per_position():
    sampler = PaperSampler(now_iso="2026-08-11T12:00:00Z")
    sampler.track(
        _request("obs-5000", 5_000),
        requested_at="2026-08-11T12:00:05Z",
    )
    sampler.track(
        _request("obs-10000", 10_000),
        requested_at="2026-08-11T12:00:10Z",
    )

    bound = sampler.consume(_completion(
        "obs-5000",
        "2026-08-11T12:00:05Z",
        "2026-08-11T12:00:11Z",
    ))
    detail = sampler.snapshot(now_iso="2026-08-11T12:00:11Z")

    assert bound.request["scheduled_age_ms"] == 5_000
    assert detail["requests"] == 2
    assert detail["completions"] == 1
    assert detail["active_contexts"] == 1
    assert detail["max_inflight_buckets"] == 2
    assert detail["max_position_inflight_buckets"] == 2
    assert detail["round_trip_ms"] == {
        "count": 1,
        "sum": 6_000,
        "max": 6_000,
        "buckets": {
            "1000": 0,
            "2000": 0,
            "5000": 0,
            "10000": 1,
            "15000": 0,
            "30000": 0,
            "60000": 0,
            "overflow": 0,
        },
    }
    assert detail["by_venue"]["pumpfun_solana"]["round_trip_ms"]["count"] == 1


def test_sampler_context_bound_counts_eviction_and_orphan_completion():
    sampler = PaperSampler(
        context_max=2,
        now_iso="2026-08-11T12:00:00Z",
    )
    for index in range(3):
        sampler.track(
            _request(f"obs-{index}", 5_000 * (index + 1), entry=f"entry-{index}"),
            requested_at=f"2026-08-11T12:00:0{index}Z",
        )

    assert sampler.active_count == 2
    assert sampler.consume(_completion(
        "obs-0",
        "2026-08-11T12:00:00Z",
        "2026-08-11T12:00:03Z",
    )) is None
    detail = sampler.snapshot(now_iso="2026-08-11T12:00:03Z")
    assert detail["context_evictions"] == 1
    assert detail["orphan_completions"] == 1


def test_sampler_persists_once_per_minute_and_restores_unfinished_window(tmp_path):
    conn = connect(tmp_path / "op.db")
    sampler = PaperSampler(now_iso="2026-08-11T12:00:00Z")
    sampler.track(
        _request("obs-5000", 5_000),
        requested_at="2026-08-11T12:00:05Z",
    )

    assert sampler.maybe_persist(
        conn,
        now_iso="2026-08-11T12:00:10Z",
        probe_metrics={"solana": {"pending": 1}},
    ) is True
    assert sampler.maybe_persist(
        conn,
        now_iso="2026-08-11T12:00:59Z",
        probe_metrics={"solana": {"pending": 1}},
    ) is False
    assert sampler.persistence_due(now_iso="2026-08-11T12:00:59Z") is False
    assert sampler.persistence_due(now_iso="2026-08-11T12:01:10Z") is True
    assert sampler.maybe_persist(
        conn,
        now_iso="2026-08-11T12:01:10Z",
        probe_metrics={"solana": {"pending": 1}},
    ) is True

    restored = PaperSampler.restore(conn, now_iso="2026-08-11T12:01:11Z")
    detail = restored.snapshot(now_iso="2026-08-11T12:01:11Z")
    assert detail["requests"] == 1
    assert detail["worker_epochs"] == 2
    assert detail["active_contexts"] == 0
    assert detail["probe_metrics"]["solana"]["pending"] == 1


def test_sampler_preserves_a_completed_window_after_restart(tmp_path):
    conn = connect(tmp_path / "op.db")
    sampler = PaperSampler(now_iso="2026-08-10T12:00:00Z")
    assert sampler.maybe_persist(
        conn,
        now_iso="2026-08-11T12:00:00Z",
        probe_metrics={},
    ) is True
    before = conn.execute(
        "SELECT version,detail_json FROM op_runtime_status WHERE source_key=?",
        (SOURCE_KEY,),
    ).fetchone()
    assert json.loads(before["detail_json"])["window_complete"] is True

    restored = PaperSampler.restore(conn, now_iso="2026-08-11T12:00:01Z")
    assert restored.maybe_persist(
        conn,
        now_iso="2026-08-11T12:01:01Z",
        probe_metrics={},
    ) is False
    after = conn.execute(
        "SELECT version,detail_json FROM op_runtime_status WHERE source_key=?",
        (SOURCE_KEY,),
    ).fetchone()
    assert tuple(after) == tuple(before)


def test_sampler_starts_a_new_window_after_malformed_saved_telemetry(tmp_path):
    conn = connect(tmp_path / "op.db")
    upsert_runtime_status(
        conn,
        source_key=SOURCE_KEY,
        status="healthy",
        heartbeat_at="2026-08-11T12:00:00Z",
        detail={"detail_version": "paper-sampler-v1"},
    )

    sampler = PaperSampler.restore(conn, now_iso="2026-08-11T13:00:00Z")

    assert sampler.window_started_at == "2026-08-11T13:00:00Z"
    assert sampler.worker_epochs == 1


def test_a_sealed_v1_window_does_not_restore_into_the_v2_identity(tmp_path):
    """A window seals forever, so a second measurement needs a new identity.

    v1 measured provider load from BEFORE `radar-realistic-v3`, when the v2 lifecycle
    stopped quoting a position at its own outcome. Under v3 a decided position keeps
    quoting to the 90 s terminal age. Restoring the sealed v1 row would carry both the
    seal and the wrong load into the v3 measurement.
    """
    conn = connect(tmp_path / "op.db")
    upsert_runtime_status(
        conn,
        source_key="opportunity:paper_sampler",
        status="healthy",
        blocker_code=None,
        heartbeat_at="2026-08-12T18:38:59Z",
        detail={
            "detail_version": "paper-sampler-v1",
            "window_started_at": "2026-08-11T18:38:59Z",
            "window_ends_at": "2026-08-12T18:38:59Z",
            "window_complete": True,
            "requests": 4_242,
        },
    )
    sampler = PaperSampler.restore(conn, now_iso="2026-08-12T19:00:00Z")
    assert sampler.window_started_at == "2026-08-12T19:00:00Z"
    assert sampler.requests == 0
    assert sampler.persistence_due(now_iso="2026-08-12T19:00:00Z") is True
    # the sealed v1 row survives under its own key as the before-and-after record
    row = conn.execute(
        "SELECT detail_json FROM op_runtime_status "
        "WHERE source_key='opportunity:paper_sampler'"
    ).fetchone()
    assert json.loads(row["detail_json"])["requests"] == 4_242


def test_the_retired_v1_sampler_key_never_grades_the_snapshot(tmp_path):
    """Nothing writes the v1 key again, so its heartbeat must not become an alarm."""
    from sniper.research.memecoin_opportunity.projection import read_radar_snapshot

    conn = connect(tmp_path / "op.db")
    upsert_runtime_status(
        conn,
        source_key="opportunity:paper_sampler",
        status="healthy",
        blocker_code=None,
        heartbeat_at="2026-08-11T20:00:00Z",
        detail={"detail_version": "paper-sampler-v1", "window_complete": False},
    )
    upsert_runtime_status(
        conn,
        source_key=SOURCE_KEY,
        status="healthy",
        blocker_code=None,
        heartbeat_at="2026-08-12T06:00:00Z",
        detail={"detail_version": DETAIL_VERSION, "window_complete": False},
    )
    snapshot = read_radar_snapshot(conn, now_iso="2026-08-12T06:00:01Z")
    keys = {row["source_key"] for row in snapshot["source_health"]}
    assert "opportunity:paper_sampler" not in keys
    assert SOURCE_KEY in keys
    # the row survives as the historical record even though it no longer grades
    assert conn.execute(
        "SELECT COUNT(*) FROM op_runtime_status "
        "WHERE source_key='opportunity:paper_sampler'"
    ).fetchone()[0] == 1


def test_an_incomplete_v1_window_also_does_not_restore_into_v2(tmp_path):
    """The real v1 row never sealed -- it was cut over mid-window at 176 requests."""
    conn = connect(tmp_path / "op.db")
    upsert_runtime_status(
        conn,
        source_key="opportunity:paper_sampler",
        status="healthy",
        blocker_code=None,
        heartbeat_at="2026-08-11T20:21:00Z",
        detail={
            "detail_version": "paper-sampler-v1",
            "window_started_at": "2026-08-11T18:38:59Z",
            "window_ends_at": "2026-08-12T18:38:59Z",
            "window_complete": False,
            "requests": 176,
        },
    )
    sampler = PaperSampler.restore(conn, now_iso="2026-08-11T20:21:56Z")
    assert sampler.window_started_at == "2026-08-11T20:21:56Z"
    assert sampler.requests == 0
