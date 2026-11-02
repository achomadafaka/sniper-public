from dataclasses import replace
import json

import pytest

from sniper.research.memecoin_opportunity.store import connect
from sniper.research.memecoin_opportunity.continuity import record_source_failure
from sniper.services.memecoin_opportunity_runtime import (
    OpportunityCaptureRuntime,
    OpportunityRuntimeDisabled,
    require_capture_enabled,
)
from sniper.settings.memecoin_opportunity import load_memecoin_opportunity_settings
from scripts.memecoin_opportunity_worker import _source_heartbeat
from scripts.memecoin_opportunity_worker import _engine_state_available
import scripts.memecoin_opportunity_worker as opportunity_worker
from sniper.research.memecoin_opportunity.decoders import DecodedVenueEvent
from sniper.research.memecoin_opportunity.types import EventKind


def test_runtime_refuses_to_start_when_default_off():
    with pytest.raises(OpportunityRuntimeDisabled, match="default OFF"):
        require_capture_enabled(load_memecoin_opportunity_settings({}))


def test_bounded_queue_overflow_is_visible_and_never_silent(tmp_path):
    settings = load_memecoin_opportunity_settings({
        "MEMECOIN_OPPORTUNITY_QUEUE_MAX": "1",
    })
    conn = connect(tmp_path / "op.db")
    runtime = OpportunityCaptureRuntime(
        conn,
        settings=settings,
        process_fn=lambda envelope: None,
        now_fn=lambda: "2026-07-26T12:00:00Z",
    )
    assert runtime.submit("source-a", {"id": 1}) is True
    assert runtime.submit("source-a", {"id": 2}) is False
    row = conn.execute("SELECT status,blocker_code,dropped_events FROM op_runtime_status").fetchone()
    assert tuple(row) == ("degraded", "EVENT_GAP", 1)
    assert conn.execute(
        "SELECT has_open_gap FROM op_source_frontiers WHERE source_key='source-a'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT count(*) FROM op_data_gaps WHERE source_key='source-a' AND resolved_at IS NULL"
    ).fetchone()[0] == 1


def test_drain_processes_in_order_and_updates_healthy_heartbeat(tmp_path):
    settings = load_memecoin_opportunity_settings({})
    conn = connect(tmp_path / "op.db")
    seen = []
    runtime = OpportunityCaptureRuntime(
        conn,
        settings=settings,
        process_fn=seen.append,
        now_fn=lambda: "2026-07-26T12:00:00Z",
    )
    runtime.submit("source-a", 1)
    runtime.submit("source-a", 2)
    assert runtime.drain(max_items=10) == 2
    assert seen == [1, 2]
    row = conn.execute("SELECT status,queue_depth,dropped_events FROM op_runtime_status").fetchone()
    assert tuple(row) == ("healthy", 0, 0)
    assert conn.execute("SELECT version FROM op_runtime_status").fetchone()[0] == 1


def test_drain_preserves_source_health_deadlines(tmp_path):
    conn = connect(tmp_path / "op.db")
    runtime = OpportunityCaptureRuntime(
        conn,
        settings=load_memecoin_opportunity_settings({}),
        process_fn=lambda _envelope: None,
        now_fn=lambda: "2026-07-26T12:00:00Z",
    )
    runtime.heartbeat(
        "pons_v1_4663:ws",
        detail={
            "transport_stale_after_s": 60,
            "expected_event_max_age_s": 300,
        },
    )
    runtime.submit("pons_v1_4663:ws", {"id": 1})

    runtime.drain(max_items=1)

    detail = json.loads(conn.execute(
        "SELECT detail_json FROM op_runtime_status WHERE source_key='pons_v1_4663:ws'"
    ).fetchone()[0])
    assert detail == {
        "expected_event_max_age_s": 300,
        "transport_stale_after_s": 60,
    }


def test_detail_less_heartbeat_preserves_source_health_deadlines(tmp_path):
    conn = connect(tmp_path / "op.db")
    runtime = OpportunityCaptureRuntime(
        conn,
        settings=load_memecoin_opportunity_settings({}),
        process_fn=lambda _envelope: None,
        now_fn=lambda: "2026-07-26T12:00:00Z",
    )
    runtime.heartbeat(
        "bags_solana:ws",
        detail={
            "transport_stale_after_s": 60,
            "expected_event_max_age_s": 900,
        },
    )

    runtime.heartbeat(
        "bags_solana:ws",
        status="degraded",
        blocker_code="CONFIRMATION_FAILURE",
    )

    detail = json.loads(conn.execute(
        "SELECT detail_json FROM op_runtime_status WHERE source_key='bags_solana:ws'"
    ).fetchone()[0])
    assert detail == {
        "expected_event_max_age_s": 900,
        "transport_stale_after_s": 60,
    }


def test_processing_failure_marks_only_affected_source(tmp_path):
    settings = load_memecoin_opportunity_settings({})
    conn = connect(tmp_path / "op.db")

    def fail(value):
        if value == "bad":
            raise ValueError("decode failed")

    runtime = OpportunityCaptureRuntime(
        conn,
        settings=settings,
        process_fn=fail,
        now_fn=lambda: "2026-07-26T12:00:00Z",
    )
    runtime.submit("bad-source", "bad")
    runtime.submit("good-source", "good")
    assert runtime.drain(max_items=10) == 2
    rows = {
        row["source_key"]: (row["status"], row["blocker_code"])
        for row in conn.execute("SELECT * FROM op_runtime_status")
    }
    assert rows["bad-source"] == ("degraded", "DECODER_FAILURE")
    assert rows["good-source"] == ("healthy", None)
    assert conn.execute(
        "SELECT has_open_gap FROM op_source_frontiers WHERE source_key='bad-source'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT count(*) FROM op_data_gaps WHERE source_key='bad-source' AND resolved_at IS NULL"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT detail FROM op_data_gaps WHERE source_key='bad-source' AND resolved_at IS NULL"
    ).fetchone()[0] == "runtime_processing_failure:ValueError:decode failed"


def test_unsupported_decoder_version_opens_persistent_source_gap(tmp_path):
    settings = load_memecoin_opportunity_settings({})
    conn = connect(tmp_path / "op.db")
    runtime = OpportunityCaptureRuntime(
        conn,
        settings=settings,
        process_fn=lambda envelope: None,
        now_fn=lambda: "2026-07-26T12:00:00Z",
    )

    runtime.heartbeat(
        "source-a",
        status="degraded",
        blocker_code="UNKNOWN_PROGRAM_OR_CONTRACT_VERSION",
    )

    assert runtime.source_gapped("source-a") is True
    assert conn.execute(
        "SELECT blocker_code FROM op_runtime_status WHERE source_key='source-a'"
    ).fetchone()[0] == "UNKNOWN_PROGRAM_OR_CONTRACT_VERSION"


def test_runtime_restart_does_not_promote_transient_blocker_to_durable_gap(tmp_path):
    settings = load_memecoin_opportunity_settings({})
    conn = connect(tmp_path / "op.db")
    runtime = OpportunityCaptureRuntime(
        conn,
        settings=settings,
        process_fn=lambda envelope: None,
        now_fn=lambda: "2026-07-26T12:00:00Z",
    )
    runtime.heartbeat(
        "bags_solana:ws",
        status="degraded",
        blocker_code="BAGS_SUBSCRIPTIONS_EMPTY",
    )

    restarted = OpportunityCaptureRuntime(
        conn,
        settings=settings,
        process_fn=lambda envelope: None,
        now_fn=lambda: "2026-07-26T12:00:01Z",
    )
    _source_heartbeat(restarted, "bags_solana:ws")

    assert restarted.source_gapped("bags_solana:ws") is False
    row = conn.execute(
        "SELECT status,blocker_code FROM op_runtime_status WHERE source_key='bags_solana:ws'"
    ).fetchone()
    assert tuple(row) == ("healthy", None)


def test_runtime_restart_preserves_persisted_integrity_blocker(tmp_path):
    settings = load_memecoin_opportunity_settings({})
    conn = connect(tmp_path / "op.db")
    runtime = OpportunityCaptureRuntime(
        conn,
        settings=settings,
        process_fn=lambda envelope: None,
        now_fn=lambda: "2026-07-26T12:00:00Z",
    )
    runtime.heartbeat(
        "source-a",
        status="degraded",
        blocker_code="UNKNOWN_PROGRAM_OR_CONTRACT_VERSION",
    )

    restarted = OpportunityCaptureRuntime(
        conn,
        settings=settings,
        process_fn=lambda envelope: None,
        now_fn=lambda: "2026-07-26T12:00:01Z",
    )

    assert restarted.source_gapped("source-a") is True


def test_runtime_restores_persisted_open_frontier_gap(tmp_path):
    settings = load_memecoin_opportunity_settings({})
    conn = connect(tmp_path / "op.db")
    record_source_failure(
        conn,
        source_key="source-a",
        observed_at="2026-07-26T12:00:00Z",
        detail="disconnect",
    )

    runtime = OpportunityCaptureRuntime(
        conn,
        settings=settings,
        process_fn=lambda envelope: None,
        now_fn=lambda: "2026-07-26T12:00:01Z",
    )

    assert runtime.source_gapped("source-a") is True


def test_transport_heartbeat_does_not_erase_integrity_blocker(tmp_path):
    settings = load_memecoin_opportunity_settings({})
    conn = connect(tmp_path / "op.db")
    runtime = OpportunityCaptureRuntime(
        conn,
        settings=settings,
        process_fn=lambda envelope: None,
        now_fn=lambda: "2026-07-26T12:00:01Z",
    )
    runtime.heartbeat(
        "source-a", status="degraded", blocker_code="DECODER_FAILURE"
    )

    _source_heartbeat(runtime, "source-a")

    row = conn.execute(
        "SELECT status,blocker_code FROM op_runtime_status WHERE source_key='source-a'"
    ).fetchone()
    assert tuple(row) == ("degraded", "DECODER_FAILURE")


def test_transport_heartbeat_preserves_last_real_event_time(tmp_path):
    settings = load_memecoin_opportunity_settings({})
    conn = connect(tmp_path / "op.db")
    times = iter((
        "2026-07-26T12:00:00Z",
        "2026-07-26T12:00:00Z",
        "2026-07-26T12:00:01Z",
    ))
    runtime = OpportunityCaptureRuntime(
        conn,
        settings=settings,
        process_fn=lambda envelope: None,
        now_fn=lambda: next(times),
    )
    runtime.submit("source-a", {"id": 1})
    runtime.drain(max_items=1)

    runtime.heartbeat("source-a")

    row = conn.execute(
        "SELECT last_event_at,heartbeat_at FROM op_runtime_status WHERE source_key='source-a'"
    ).fetchone()
    assert tuple(row) == ("2026-07-26T12:00:00Z", "2026-07-26T12:00:01Z")


def test_pons_heartbeat_bootstraps_freshness_from_persisted_chain_time(tmp_path):
    conn = connect(tmp_path / "op.db")
    conn.execute(
        "INSERT INTO op_raw_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "raw-1", "4663", "pons_v1_4663", "pons_v1_4663:ws",
            "eth_getLogs_poll", "1:0:0", "0xtoken", "0xtx", 1, 0, 0,
            "0xblock", "2026-07-26T11:50:00Z", "2026-07-26T12:00:09Z",
            "confirmed", "{}", "payload-hash", "row-fingerprint",
            "2026-07-26T12:00:09Z",
        ),
    )
    runtime = OpportunityCaptureRuntime(
        conn,
        settings=load_memecoin_opportunity_settings({}),
        process_fn=lambda _envelope: None,
        now_fn=lambda: "2026-07-26T12:00:10Z",
    )

    _source_heartbeat(runtime, "pons_v1_4663:ws")

    detail = json.loads(conn.execute(
        "SELECT detail_json FROM op_runtime_status WHERE source_key='pons_v1_4663:ws'"
    ).fetchone()[0])
    assert detail["last_chain_event_at"] == "2026-07-26T11:50:00Z"


def test_midstream_trade_waits_for_launch_state_without_failing_source():
    class Engine:
        states = {}

    trade = DecodedVenueEvent(event_kind=EventKind.BUY, token_id="mint")
    launch = DecodedVenueEvent(event_kind=EventKind.LAUNCH, token_id="mint")

    assert _engine_state_available(Engine(), trade, "launch-1") is False
    assert _engine_state_available(Engine(), launch, "launch-1") is True


def test_quote_completions_are_consumed_on_main_worker_thread():
    completion = object()

    class Probe:
        def drain_completions(self, limit=32):
            assert limit == 32
            return (completion,)

    class Engine:
        seen = []

        def observe_remote_completion(self, result, *, observed_at):
            self.seen.append((result, observed_at))

    engine = Engine()

    count = opportunity_worker._drain_solana_quote_completions(
        Probe(),
        engine,
        observed_at="2026-07-26T12:00:00Z",
    )

    assert count == 1
    assert engine.seen == [(completion, "2026-07-26T12:00:00Z")]


def test_quiet_bags_source_declares_no_event_expectation(tmp_path):
    """Bags is a low-cadence venue: sampled 2026-08-03, the newest feed pools had last
    traded 3-17 hours earlier and none within the hour. A 900s event expectation is
    therefore guaranteed to trip and reports a working transport as a dead one. The Pons
    readiness design reserves that expectation for high-cadence launch flows and says a
    quiet Bags source shows event age as telemetry instead."""
    conn = connect(tmp_path / "op.db")
    runtime = OpportunityCaptureRuntime(
        conn,
        settings=load_memecoin_opportunity_settings({}),
        process_fn=lambda _envelope: None,
        now_fn=lambda: "2026-07-26T12:00:00Z",
    )

    _source_heartbeat(runtime, "bags_solana:ws")

    detail = json.loads(conn.execute(
        "SELECT detail_json FROM op_runtime_status WHERE source_key='bags_solana:ws'"
    ).fetchone()[0])
    assert "expected_event_max_age_s" not in detail
    # Transport liveness is still bounded — silence must not hide a dead socket.
    assert detail["transport_stale_after_s"] > 0


def test_high_cadence_pons_still_declares_an_event_expectation(tmp_path):
    """Pons keeps declaring `expected_event_max_age_s` so silence stays VISIBLE.

    Since 2026-08-19 that number is activity data, not a health input: capture health is
    transport, finalized scan lag, typed failures and open gaps. The declaration is kept
    because an operator still needs to see that a venue has gone quiet -- it just no
    longer grades the lane unhealthy, which it did while Pons sat silent from 2026-08-12
    with a current scan and zero open gaps.
    """
    conn = connect(tmp_path / "op.db")
    runtime = OpportunityCaptureRuntime(
        conn,
        settings=load_memecoin_opportunity_settings({}),
        process_fn=lambda _envelope: None,
        now_fn=lambda: "2026-07-26T12:00:00Z",
    )

    _source_heartbeat(runtime, "pons_v1_4663:ws")

    detail = json.loads(conn.execute(
        "SELECT detail_json FROM op_runtime_status WHERE source_key='pons_v1_4663:ws'"
    ).fetchone()[0])
    assert detail["expected_event_max_age_s"] == 300
