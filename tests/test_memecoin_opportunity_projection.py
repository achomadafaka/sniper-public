from __future__ import annotations

import hashlib

import sniper.research.memecoin_opportunity.projection as opportunity_projection
from sniper.research.memecoin_opportunity.curve_math import constant_product_clip_quote
from sniper.research.memecoin_opportunity.decoders import DecodedVenueEvent
from sniper.research.memecoin_opportunity.directional import DirectionalVerdict, RadarZone
from sniper.research.memecoin_opportunity.projection import (
    _arm_fields,
    append_forward_alert_receipt,
    persist_state_evaluation,
    read_radar_snapshot,
    upsert_runtime_status,
)
from sniper.research.memecoin_opportunity.state import launch_state
from sniper.research.memecoin_opportunity.store import (
    LaunchIdentity,
    append_canonical_event,
    append_raw_event,
    connect,
    upsert_launch_identity,
)
from sniper.research.memecoin_opportunity.reconciliation import reconcile_confirmation
from sniper.research.memecoin_opportunity.types import (
    CanonicalEvent,
    ChainOrder,
    ConfirmationState,
    EventKind,
    LifecycleState,
    NetworkId,
    RawEvent,
    VenueId,
)
from sniper.research.memecoin_opportunity.paper_adapter import (
    ADAPTER_VERSION,
    FILL_MODEL_VERSION,
    PaperFillRequest,
    append_paper_fill_receipt,
    append_paper_fill_request,
)


NOW = "2026-07-26T12:00:00Z"


def _paper_request(request_id, **overrides):
    values = {
        "request_id": request_id,
        "alert_id": f"alert-{request_id}",
        "launch_id": f"launch-{request_id}",
        "network_id": "solana",
        "venue_id": "pumpfun_solana",
        "token_id": f"mint-{request_id}",
        "creator_id": "creator",
        "launch_mode": "standard",
        "state_fingerprint": f"state-{request_id}",
        "rule_version": "pump-confirmation-truth-v6",
        "evaluator_version": "alert-outcome-v4",
        "adapter_version": ADAPTER_VERSION,
        "fill_model_version": FILL_MODEL_VERSION,
        "clip_usd_micros": 25_000_000,
        "decision_at": "2026-08-06T12:00:00Z",
        "decision_age_ms": 20_000,
        "intent_at": "2026-08-06T12:00:00Z",
        "expires_at": "2026-08-06T12:00:30Z",
        "local_qty_atomic": 1_000,
    }
    values.update(overrides)
    return PaperFillRequest(**values)


def test_paper_entry_test_summary_is_exact_current_contract(tmp_path):
    conn = connect(tmp_path / "op.db")
    for request_id in ("filled", "unfilled", "pending"):
        append_paper_fill_request(conn, _paper_request(request_id))
    append_paper_fill_receipt(
        conn,
        request_id="filled",
        status="filled",
        now_iso="2026-08-06T12:00:02Z",
        quote_started_at="2026-08-06T12:00:01Z",
        quote_completed_at="2026-08-06T12:00:02Z",
        economics={
            "expected_out_atomic": 1_000,
            "floor_out_atomic": 940,
            "quote_provider": "jupiter",
            "quote_in_asset": "USDC",
            "quote_in_atomic": 25_000_000,
            "quote_in_decimals": 6,
            "quote_out_asset": "mint-filled",
            "quote_out_decimals": 6,
            "slippage_bps": 300,
            "route_binding": {"amm": "fixture"},
            "quote_payload": {"outAmount": 1_000, "otherAmountThreshold": 940},
        },
    )
    append_paper_fill_receipt(
        conn,
        request_id="unfilled",
        status="entry_unfilled",
        unfilled_reason="no_route",
        now_iso="2026-08-06T12:00:03Z",
    )
    append_paper_fill_request(
        conn, _paper_request("old", adapter_version="radar-paper-adapter-v0")
    )
    append_paper_fill_receipt(
        conn,
        request_id="old",
        status="entry_unfilled",
        unfilled_reason="stale_quote",
        now_iso="2026-08-06T12:00:04Z",
    )

    assert opportunity_projection.paper_entry_test_summary(conn) == {
        "signals_tested": 3,
        "paper_buys": 1,
        "could_not_buy": 1,
        "awaiting_quote": 1,
        "unfilled_reasons": {"no_route": 1},
        "latest_receipt_at": "2026-08-06T12:00:03Z",
    }


def _seed(conn):
    payload = '{}'
    raw = RawEvent(
        "raw-1", NetworkId.ROBINHOOD_4663, VenueId.PONS_V1_ROBINHOOD_4663,
        "source", "provider", "1:0:0", "0xabc", "0xtx",
        ChainOrder(1, 0, 0, "0xblock"), NOW, NOW, ConfirmationState.CONFIRMED,
        payload, hashlib.sha256(payload.encode()).hexdigest(),
    )
    event = CanonicalEvent(
        "event-1", "raw-1", "launch-1", NetworkId.ROBINHOOD_4663,
        VenueId.PONS_V1_ROBINHOOD_4663, "0xabc", LifecycleState.PRE_MIGRATION,
        EventKind.LAUNCH, "0xtx", ChainOrder(1, 0, 0, "0xblock"), NOW, NOW,
        NOW, NOW, ConfirmationState.CONFIRMED, "0xcreator", None, None, 0,
        True, "fixture-v1",
    )
    append_raw_event(conn, raw)
    append_canonical_event(conn, event)
    upsert_launch_identity(conn, LaunchIdentity(
        "launch-1", NetworkId.ROBINHOOD_4663, VenueId.PONS_V1_ROBINHOOD_4663,
        "0xabc", "0xcreator", "event-1", NOW,
    ))


def test_projection_persists_explainable_latest_dot_and_clip_ladder(tmp_path):
    conn = connect(tmp_path / "op.db")
    _seed(conn)
    state = launch_state(
        "launch-1",
        VenueId.PONS_V1_ROBINHOOD_4663,
        DecodedVenueEvent(
            event_kind=EventKind.LAUNCH,
            lifecycle=LifecycleState.PRE_MIGRATION,
            token_id="0xabc",
            curve_id="0xcurve",
            creator_id="0xcreator",
            virtual_token_reserves=1_000_000,
            virtual_quote_reserves=500_000,
            real_token_reserves=800_000,
            decoder_version="fixture-v1",
        ),
        NOW,
    )
    quotes = {
        25_000_000: constant_product_clip_quote(
            virtual_token_reserves=1_000_000,
            virtual_quote_reserves=500_000,
            quote_in_atomic=25_000,
            fee_bps=200,
        )
    }
    verdict = DirectionalVerdict(
        RadarZone.CENTER,
        True,
        ("REMOTE_ROUND_TRIP_EXECUTABLE",),
    )

    snapshot_id = persist_state_evaluation(
        conn,
        state=state,
        observed_event_id="event-1",
        clip_quotes=quotes,
        verdict=verdict,
        observed_at=NOW,
        dominant_risk="profit",
        features={
            "capital_acceleration_atomic_per_s2": 1_250,
            "sell_pressure_bps": 500,
        },
        market_input_as_of="2026-07-26T11:59:59Z",
        network_cost_quote_atomic=5_000,
    )
    assert persist_state_evaluation(
        conn,
        state=state,
        observed_event_id="event-1",
        clip_quotes=quotes,
        verdict=verdict,
        observed_at=NOW,
        dominant_risk="profit",
        features={
            "capital_acceleration_atomic_per_s2": 1_250,
            "sell_pressure_bps": 500,
        },
        market_input_as_of="2026-07-26T11:59:59Z",
        network_cost_quote_atomic=5_000,
    ) == snapshot_id

    payload = read_radar_snapshot(conn, now_iso="2026-07-26T12:00:01Z", stale_after_s=5)
    assert payload["mode"] == "EXPLORATORY — EDGE NOT YET ESTABLISHED"
    assert payload["stale"] is False
    assert payload["summary"]["would_fire"] == 0
    dot = payload["opportunities"][0]
    assert dot["zone"] == "CENTER"
    assert dot["distance_bps"] == 0
    assert dot["clip_quotes"][0]["clip_usd_micros"] == 25_000_000
    assert dot["clip_quotes"][0]["market_input_as_of"] == "2026-07-26T11:59:59Z"
    assert dot["clip_quotes"][0]["network_cost_quote_atomic"] == "5000"
    assert dot["facts"]["effective_buyer_count"] == 0
    assert dot["facts"]["capital_acceleration_atomic_per_s2"] == 1_250
    assert dot["facts"]["sell_pressure_bps"] == 500
    assert dot["action_class"] == "wait"
    assert dot["arm_label"] == "WAIT"


def test_forward_alert_receipt_is_explicit_and_immutable(tmp_path):
    conn = connect(tmp_path / "op.db")
    _seed(conn)
    state = launch_state(
        "launch-1",
        VenueId.PONS_V1_ROBINHOOD_4663,
        DecodedVenueEvent(
            event_kind=EventKind.LAUNCH,
            lifecycle=LifecycleState.PRE_MIGRATION,
            token_id="0xabc",
            curve_id="0xcurve",
            creator_id="0xcreator",
            virtual_token_reserves=1_000,
            virtual_quote_reserves=1_000,
            real_token_reserves=800,
            progress_bps=2_500,
            decoder_version="v1",
        ),
        NOW,
    )
    snapshot_id = persist_state_evaluation(
        conn,
        state=state,
        observed_event_id="event-1",
        clip_quotes={},
        verdict=DirectionalVerdict(RadarZone.INNER, False, ("NO_FORWARD_ARM",)),
        observed_at=NOW,
        dominant_risk="incomplete",
    )

    values = dict(
        state=state,
        snapshot_id=snapshot_id,
        observed_at=NOW,
        clip_usd_micros=25_000_000,
        rule_version="route-control-v1",
        quote_provider="fixture",
        round_trip_return_bps=-1_300,
        truth_label="ROUTE CONTROL — PAPER ONLY",
    )
    first = append_forward_alert_receipt(conn, **values)
    second = append_forward_alert_receipt(conn, **values)

    assert first == second
    row = conn.execute(
        "SELECT rule_version,round_trip_return_bps,truth_label "
        "FROM op_forward_alerts WHERE alert_id=?",
        (first,),
    ).fetchone()
    assert tuple(row) == (
        "route-control-v1",
        -1_300,
        "ROUTE CONTROL — PAPER ONLY",
    )
    payload = read_radar_snapshot(conn, now_iso="2026-07-26T12:00:01Z", stale_after_s=5)
    receipt = payload["forward_alerts"][0]
    assert receipt["arm_key"] == "route_control"
    assert receipt["arm_label"] == "ROUTE CONTROL"
    assert receipt["action_class"] == "research_only"


def test_projection_labels_forward_candidate_as_fire_now(tmp_path):
    conn = connect(tmp_path / "op.db")
    _seed(conn)
    state = launch_state(
        "launch-1",
        VenueId.PONS_V1_ROBINHOOD_4663,
        DecodedVenueEvent(
            event_kind=EventKind.LAUNCH,
            lifecycle=LifecycleState.PRE_MIGRATION,
            token_id="0xabc",
            curve_id="0xcurve",
            creator_id="0xcreator",
            virtual_token_reserves=1_000,
            virtual_quote_reserves=1_000,
            real_token_reserves=800,
            progress_bps=2_500,
            decoder_version="v1",
        ),
        NOW,
    )
    verdict = DirectionalVerdict(
        RadarZone.CENTER,
        True,
        ("PUMP_IGNITION_FORWARD_ARM",),
        rule_version="pump-ignition-v1",
    )
    snapshot_id = persist_state_evaluation(
        conn,
        state=state,
        observed_event_id="event-1",
        clip_quotes={},
        verdict=verdict,
        observed_at=NOW,
        dominant_risk="profit",
        features={"launch_age_ms": 5_000},
    )
    append_forward_alert_receipt(
        conn,
        state=state,
        snapshot_id=snapshot_id,
        observed_at=NOW,
        clip_usd_micros=25_000_000,
        rule_version="pump-ignition-v1",
        quote_provider="fixture",
        round_trip_return_bps=-500,
        truth_label=verdict.truth_label,
    )

    payload = read_radar_snapshot(conn, now_iso="2026-07-26T12:00:01Z", stale_after_s=5)

    assert payload["opportunities"][0]["arm_label"] == "PAPER QUALIFIED — IGNITION"
    assert payload["opportunities"][0]["action_class"] == "fire_now"
    assert payload["forward_alerts"][0]["launch_age_ms"] == 5_000
    assert payload["forward_alerts"][0]["progress_bps"] == 2_500


def test_projection_labels_v3_primary_and_delayed_receipts():
    assert _arm_fields("pump-confirmation-truth-v3") == {
        "arm_key": "pump_confirmation_v3",
        "arm_label": "PAPER QUALIFIED — CONFIRMED",
        "action_class": "fire_now",
    }
    assert _arm_fields("pump-confirmation-delayed-v2") == {
        "arm_key": "pump_confirmation_delayed",
        "arm_label": "DELAYED CONTROL",
        "action_class": "research_only",
    }


def test_projection_keeps_v4_measurement_receipts_diagnostic_only():
    assert _arm_fields("pump-confirmation-truth-v4") == {
        "arm_key": "pump_confirmation_v4",
        "arm_label": "DIAGNOSTIC CONFIRMATION",
        "action_class": "research_only",
    }
    assert _arm_fields("route-control-v2") == {
        "arm_key": "route_control_v2",
        "arm_label": "MATCHED CONTROL",
        "action_class": "research_only",
    }


def test_projection_marks_old_state_stale_and_suppresses_current_fire(tmp_path):
    conn = connect(tmp_path / "op.db")
    _seed(conn)
    state = launch_state(
        "launch-1", VenueId.PONS_V1_ROBINHOOD_4663,
        DecodedVenueEvent(
            event_kind=EventKind.LAUNCH, lifecycle=LifecycleState.PRE_MIGRATION,
            token_id="0xabc", curve_id="0xcurve", creator_id="0xcreator",
            virtual_token_reserves=1000, virtual_quote_reserves=1000,
            real_token_reserves=800, decoder_version="v1",
        ), NOW,
    )
    persist_state_evaluation(
        conn, state=state, observed_event_id="event-1", clip_quotes={},
        verdict=DirectionalVerdict(RadarZone.CENTER, True, ("REMOTE_ROUND_TRIP_EXECUTABLE",)),
        observed_at=NOW, dominant_risk="incomplete",
    )
    payload = read_radar_snapshot(conn, now_iso="2026-07-26T12:00:10Z", stale_after_s=5)
    assert payload["stale"] is False
    assert payload["summary"]["would_fire"] == 0
    assert payload["summary"]["zones"]["CENTER"] == 0
    assert payload["opportunities"] == []
    assert payload["summary"]["active"] == 0
    assert payload["summary"]["archived"] == 1
    assert payload["history"][0]["stale"] is True
    assert payload["history"][0]["zone"] == "CENTER"
    assert payload["history"][0]["would_fire"] is False
    assert payload["history"][0]["historical_would_fire"] is True


def test_projection_ranks_equal_distance_by_inward_velocity(tmp_path):
    conn = connect(tmp_path / "op.db")
    _seed(conn)
    state = launch_state(
        "launch-1", VenueId.PONS_V1_ROBINHOOD_4663,
        DecodedVenueEvent(
            event_kind=EventKind.LAUNCH, lifecycle=LifecycleState.PRE_MIGRATION,
            token_id="0xabc", curve_id="0xcurve", creator_id="0xcreator",
            virtual_token_reserves=1000, virtual_quote_reserves=1000,
            real_token_reserves=800, decoder_version="v1",
        ), NOW,
    )
    persist_state_evaluation(
        conn, state=state, observed_event_id="event-1", clip_quotes={},
        verdict=DirectionalVerdict(RadarZone.INNER, False, ("REMOTE_QUOTE_PENDING",)),
        observed_at=NOW, dominant_risk="incomplete",
        features={"inward_velocity_bps_per_s": 1200, "data_quality_bps": 9000},
    )
    payload = read_radar_snapshot(conn, now_iso="2026-07-26T12:00:01Z", stale_after_s=5)
    assert payload["opportunities"][0]["inward_velocity_bps_per_s"] == 1200
    assert payload["opportunities"][0]["zone_transition"] is None


def test_projection_reports_runtime_heartbeat_age_and_staleness(tmp_path):
    conn = connect(tmp_path / "op.db")
    conn.execute(
        "INSERT INTO op_runtime_status "
        "(source_key,status,blocker_code,last_event_at,heartbeat_at,queue_depth,dropped_events,detail_json,version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        # A socket declares its own bound; only loops fall back to the worker default.
        ("pumpfun_solana:ws", "healthy", None, NOW, NOW, 0, 0,
         '{"transport_stale_after_s": 5}', 1),
    )
    payload = read_radar_snapshot(
        conn, now_iso="2026-07-26T12:00:10Z", stale_after_s=5
    )
    source = payload["source_health"][0]
    assert source["heartbeat_age_s"] == 10
    assert source["transport_stale_after_s"] == 5
    assert source["stale"] is True
    assert payload["stale"] is True


def test_projection_separates_transport_liveness_from_event_silence(tmp_path):
    conn = connect(tmp_path / "op.db")
    conn.execute(
        "INSERT INTO op_runtime_status "
        "(source_key,status,blocker_code,last_event_at,heartbeat_at,queue_depth,dropped_events,detail_json,version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "pons_v1_4663:ws", "healthy", None,
            "2026-07-26T00:00:00Z", "2026-07-26T11:59:40Z", 0, 0,
            '{"transport_stale_after_s":60}', 1,
        ),
    )

    source = read_radar_snapshot(
        conn, now_iso="2026-07-26T12:00:10Z", stale_after_s=5
    )["source_health"][0]

    assert source["heartbeat_age_s"] == 30
    assert source["transport_stale_after_s"] == 60
    assert source["transport_stale"] is False
    assert source["event_age_s"] == 43_210
    assert source["event_stale"] is None
    assert source["event_freshness_status"] == "observed"
    assert source["stale"] is False


def test_projection_marks_declared_high_cadence_event_source_stale(tmp_path):
    conn = connect(tmp_path / "op.db")
    conn.execute(
        "INSERT INTO op_runtime_status "
        "(source_key,status,blocker_code,last_event_at,heartbeat_at,queue_depth,dropped_events,detail_json,version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "pons_v1_4663:ws", "healthy", None,
            "2026-07-26T11:50:00Z", "2026-07-26T12:00:09Z", 0, 0,
            '{"expected_event_max_age_s":300}', 1,
        ),
    )

    payload = read_radar_snapshot(
        conn, now_iso="2026-07-26T12:00:10Z", stale_after_s=5
    )
    source = payload["source_health"][0]

    assert source["transport_stale"] is False
    assert source["event_age_s"] == 610
    assert source["expected_event_max_age_s"] == 300
    assert source["event_stale"] is True
    assert source["event_freshness_status"] == "stale"
    # This source declares NO scan contract, so event age is its only delivery check.
    # A scan-less lane that goes silent must still grade stale.
    assert source["stale"] is True
    assert payload["stale"] is True


def test_projection_uses_chain_event_time_during_http_replay(tmp_path):
    conn = connect(tmp_path / "op.db")
    conn.execute(
        "INSERT INTO op_runtime_status "
        "(source_key,status,blocker_code,last_event_at,heartbeat_at,queue_depth,dropped_events,detail_json,version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "pons_v1_4663:ws", "healthy", None,
            "2026-07-26T12:00:09Z", "2026-07-26T12:00:09Z", 0, 0,
            '{"expected_event_max_age_s":300,"last_chain_event_at":"2026-07-26T11:50:00Z"}',
            1,
        ),
    )

    source = read_radar_snapshot(
        conn, now_iso="2026-07-26T12:00:10Z", stale_after_s=5
    )["source_health"][0]

    assert source["last_event_at"] == "2026-07-26T11:50:00Z"
    assert source["event_age_s"] == 610
    assert source["event_stale"] is True
    # no scan contract here either, so event age still decides
    assert source["stale"] is True


def test_projection_times_out_high_cadence_source_that_never_emits(tmp_path):
    conn = connect(tmp_path / "op.db")
    conn.execute(
        "INSERT INTO op_runtime_status "
        "(source_key,status,blocker_code,last_event_at,heartbeat_at,queue_depth,dropped_events,detail_json,version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "pons_v1_4663:ws", "healthy", None, None,
            "2026-07-26T12:00:09Z", 0, 0,
            '{"expected_event_max_age_s":300,"event_expectation_started_at":"2026-07-26T11:50:00Z"}',
            1,
        ),
    )

    source = read_radar_snapshot(
        conn, now_iso="2026-07-26T12:00:10Z", stale_after_s=5
    )["source_health"][0]

    assert source["event_age_s"] == 610
    assert source["event_stale"] is True
    assert source["event_freshness_status"] == "stale"


def test_projection_excludes_snapshot_from_orphaned_event(tmp_path):
    conn = connect(tmp_path / "op.db")
    payload = '{}'
    raw = RawEvent(
        "raw-tentative", NetworkId.ROBINHOOD_4663, VenueId.PONS_V1_ROBINHOOD_4663,
        "source", "provider", "1:0:0", "0xabc", "0xtx",
        ChainOrder(1, 0, 0, "0xblock"), NOW, NOW, ConfirmationState.TENTATIVE,
        payload, hashlib.sha256(payload.encode()).hexdigest(),
    )
    event = CanonicalEvent(
        "event-tentative", "raw-tentative", "launch-1", NetworkId.ROBINHOOD_4663,
        VenueId.PONS_V1_ROBINHOOD_4663, "0xabc", LifecycleState.PRE_MIGRATION,
        EventKind.LAUNCH, "0xtx", ChainOrder(1, 0, 0, "0xblock"), NOW, NOW,
        NOW, NOW, ConfirmationState.TENTATIVE, "0xcreator", None, None, 0,
        True, "fixture-v1",
    )
    append_raw_event(conn, raw)
    append_canonical_event(conn, event)
    upsert_launch_identity(conn, LaunchIdentity(
        "launch-1", NetworkId.ROBINHOOD_4663, VenueId.PONS_V1_ROBINHOOD_4663,
        "0xabc", "0xcreator", "event-tentative", NOW,
    ))
    state = launch_state(
        "launch-1", VenueId.PONS_V1_ROBINHOOD_4663,
        DecodedVenueEvent(
            event_kind=EventKind.LAUNCH, lifecycle=LifecycleState.PRE_MIGRATION,
            token_id="0xabc", curve_id="0xcurve", creator_id="0xcreator",
            virtual_token_reserves=1_000, virtual_quote_reserves=1_000,
            real_token_reserves=800, decoder_version="v1",
        ), NOW,
    )
    persist_state_evaluation(
        conn, state=state, observed_event_id="event-tentative", clip_quotes={},
        verdict=DirectionalVerdict(RadarZone.CENTER, True, ("fixture",)),
        observed_at=NOW, dominant_risk="profit",
    )
    reconcile_confirmation(
        conn, canonical_event_id="event-tentative",
        confirmation=ConfirmationState.ORPHANED,
        reconciled_at="2026-07-26T12:00:01Z",
    )

    snapshot = read_radar_snapshot(
        conn, now_iso="2026-07-26T12:00:01Z", stale_after_s=5
    )
    assert snapshot["opportunities"] == []
    assert snapshot["summary"]["would_fire"] == 0


def _health_row(conn, source_key, *, heartbeat_at, last_event_at=None, detail=None):
    upsert_runtime_status(
        conn,
        source_key=source_key,
        status="healthy",
        heartbeat_at=heartbeat_at,
        last_event_at=last_event_at,
        detail=detail,
    )


def test_current_health_excludes_historical_only_sources(tmp_path):
    conn = connect(tmp_path / "op.db")
    for source_key in (
        "pumpfun_solana:ws",
        "pumpfun_solana:pricing",
        "bags_robinhood_4663:ws",
        "bags_robinhood_4663:pricing",
        "pons_v1_4663:ws",
        "pons_v1_4663:pricing",
        "bags_solana:ws",
        "bags_solana:pricing",
        "opportunity:bags_coverage",
        "opportunity:bags_feed_recovery",
    ):
        _health_row(
            conn,
            source_key,
            heartbeat_at=NOW,
            last_event_at=NOW if source_key.endswith(":ws") else None,
            detail={"transport_stale_after_s": 60}
            if source_key.endswith(":ws") else None,
        )

    snapshot = read_radar_snapshot(
        conn,
        now_iso="2026-07-26T12:00:01Z",
        stale_after_s=5,
    )
    by_key = {row["source_key"]: row for row in snapshot["source_health"]}

    assert "bags_solana:ws" not in by_key
    assert "bags_solana:pricing" not in by_key
    assert "opportunity:bags_coverage" not in by_key
    assert "opportunity:bags_feed_recovery" not in by_key
    # bags_robinhood_4663 retired 2026-08-19: it is HISTORICAL_ONLY now, so it must be
    # excluded exactly like bags_solana rather than reported as a live lane.
    assert "bags_robinhood_4663:ws" not in by_key
    assert "bags_robinhood_4663:pricing" not in by_key
    assert by_key["pumpfun_solana:ws"]["venue_authority"] == "scoreable"
    assert by_key["pons_v1_4663:ws"]["venue_authority"] == "scoreable"


def test_stale_transport_downgrades_health_status_instead_of_reporting_healthy(tmp_path):
    """A dead transport must not keep reporting the persisted `healthy` column."""
    conn = connect(tmp_path / "op.db")
    _health_row(
        conn, "pons_v1_4663:ws",
        heartbeat_at="2026-07-26T06:00:00Z",
        last_event_at="2026-07-26T06:00:00Z",
        detail={"transport_stale_after_s": 60, "expected_event_max_age_s": 900},
    )
    _health_row(conn, "pumpfun_solana:ws", heartbeat_at=NOW,
                last_event_at=NOW, detail={"transport_stale_after_s": 60})

    payload = read_radar_snapshot(conn, now_iso="2026-07-26T12:00:01Z", stale_after_s=5)
    by_key = {row["source_key"]: row for row in payload["source_health"]}

    assert by_key["pons_v1_4663:ws"]["status"] == "healthy"
    assert by_key["pons_v1_4663:ws"]["health_status"] == "degraded"
    assert by_key["pumpfun_solana:ws"]["health_status"] == "healthy"


def test_one_stale_capture_source_does_not_stale_the_full_snapshot(tmp_path):
    conn = connect(tmp_path / "op.db")
    upsert_runtime_status(
        conn,
        source_key="pons_v1_4663:ws",
        status="healthy",
        heartbeat_at="2026-07-26T11:00:00Z",
        detail={"transport_stale_after_s": 60},
    )
    upsert_runtime_status(
        conn,
        source_key="pumpfun_solana:ws",
        status="healthy",
        heartbeat_at=NOW,
        detail={"transport_stale_after_s": 60},
    )

    payload = read_radar_snapshot(
        conn, now_iso="2026-07-26T12:00:10Z", stale_after_s=5
    )
    by_key = {row["source_key"]: row for row in payload["source_health"]}

    assert by_key["pons_v1_4663:ws"]["stale"] is True
    assert by_key["pumpfun_solana:ws"]["stale"] is False
    assert payload["stale"] is False
    assert payload["stale_sources"] == []
    assert payload["reason"] is None


def test_internal_worker_cadence_does_not_permanently_flag_the_radar_stale(tmp_path):
    """The 5s transport bound is meaningless for periodic workers."""
    conn = connect(tmp_path / "op.db")
    _health_row(conn, "opportunity:storage", heartbeat_at="2026-07-26T11:59:45Z")
    _health_row(conn, "pumpfun_solana:ws", heartbeat_at=NOW,
                last_event_at=NOW, detail={"transport_stale_after_s": 60})

    payload = read_radar_snapshot(conn, now_iso="2026-07-26T12:00:01Z", stale_after_s=5)
    by_key = {row["source_key"]: row for row in payload["source_health"]}

    assert by_key["opportunity:storage"]["stale"] is False
    assert by_key["opportunity:storage"]["health_status"] == "healthy"
    assert payload["stale"] is False
    assert payload["reason"] is None


def test_venue_with_no_live_transport_and_no_recovery_is_reported_down(tmp_path):
    conn = connect(tmp_path / "op.db")
    _health_row(
        conn, "pons_v1_4663:ws",
        heartbeat_at="2026-07-26T06:00:00Z",
        last_event_at="2026-07-26T06:00:00Z",
        detail={"transport_stale_after_s": 60, "expected_event_max_age_s": 900},
    )

    payload = read_radar_snapshot(conn, now_iso="2026-07-26T12:00:01Z", stale_after_s=5)
    row = payload["source_health"][0]

    assert row["capture_mode"] == "down"


def test_pricing_loop_cadence_is_not_graded_against_the_socket_bound(tmp_path):
    """Pricing rides the sweep loop (~19s observed), not a socket. 5s flagged it stale."""
    conn = connect(tmp_path / "op.db")
    _health_row(conn, "pumpfun_solana:pricing", heartbeat_at="2026-07-26T11:59:42Z")
    _health_row(conn, "pumpfun_solana:ws", heartbeat_at=NOW,
                last_event_at=NOW, detail={"transport_stale_after_s": 60})

    payload = read_radar_snapshot(conn, now_iso="2026-07-26T12:00:01Z", stale_after_s=5)
    by_key = {row["source_key"]: row for row in payload["source_health"]}

    assert by_key["pumpfun_solana:pricing"]["heartbeat_age_s"] == 19
    assert by_key["pumpfun_solana:pricing"]["stale"] is False
    assert by_key["pumpfun_solana:pricing"]["health_status"] == "healthy"
    assert payload["stale"] is False


def test_a_genuinely_dead_worker_loop_still_degrades(tmp_path):
    conn = connect(tmp_path / "op.db")
    _health_row(conn, "pumpfun_solana:pricing", heartbeat_at="2026-07-26T11:55:00Z")

    payload = read_radar_snapshot(conn, now_iso="2026-07-26T12:00:01Z", stale_after_s=5)
    row = payload["source_health"][0]

    assert row["health_status"] == "degraded"
    assert payload["reason"] == "stale sources: pumpfun_solana:pricing"


def test_creator_prior_arm_is_labelled_research_only():
    """Diagnostic paper, like every other v4-era arm. It is not an action."""
    assert _arm_fields("pump-creator-prior-v1") == {
        "arm_key": "pump_creator_prior",
        "arm_label": "CREATOR PRIOR",
        "action_class": "research_only",
    }


# --- scan lag: a live socket on a lane that cannot keep up is not a healthy lane -------

def _runtime_row(conn, source_key, detail_json, *, heartbeat="2026-07-26T12:00:05Z"):
    conn.execute(
        "INSERT INTO op_runtime_status "
        "(source_key,status,blocker_code,last_event_at,heartbeat_at,queue_depth,"
        "dropped_events,detail_json,version) VALUES (?,?,?,?,?,?,?,?,?)",
        (source_key, "healthy", None, heartbeat, heartbeat, 0, 0, detail_json, 1),
    )


def test_projection_grades_a_lane_that_trails_the_chain_head_as_degraded(tmp_path):
    """Bags 4663 held a fresh socket and no event-age bound while the shared scan sat
    ~11,700 blocks behind head, so a 20-minute-late venue read `healthy`."""
    conn = connect(tmp_path / "op.db")
    _runtime_row(
        conn, "pons_v1_4663:ws",
        '{"transport_stale_after_s":60,"blocks_behind_head":11766,'
        '"max_blocks_behind_head":600}',
    )

    source = read_radar_snapshot(
        conn, now_iso="2026-07-26T12:00:10Z", stale_after_s=5
    )["source_health"][0]

    assert source["transport_stale"] is False   # the socket is alive
    assert source["event_stale"] is None        # a quiet venue declares no event bound
    assert source["scan_behind"] is True
    assert source["blocks_behind_head"] == 11_766
    assert source["health_status"] == "degraded"
    assert source["stale"] is True


def test_projection_keeps_a_caught_up_lane_healthy(tmp_path):
    conn = connect(tmp_path / "op.db")
    _runtime_row(
        conn, "pons_v1_4663:ws",
        '{"transport_stale_after_s":60,"blocks_behind_head":12,'
        '"max_blocks_behind_head":600}',
    )

    source = read_radar_snapshot(
        conn, now_iso="2026-07-26T12:00:10Z", stale_after_s=5
    )["source_health"][0]

    assert source["scan_behind"] is False
    assert source["health_status"] == "healthy"
    assert source["stale"] is False


def test_projection_cannot_grade_scan_lag_a_writer_never_declared(tmp_path):
    """A socket lane reports no block cursor. Absence of a bound is unknown, not good."""
    conn = connect(tmp_path / "op.db")
    _runtime_row(conn, "pumpfun_solana:ws", '{"transport_stale_after_s":60}')

    source = read_radar_snapshot(
        conn, now_iso="2026-07-26T12:00:10Z", stale_after_s=5
    )["source_health"][0]

    assert source["scan_behind"] is None
    assert source["blocks_behind_head"] is None
    assert source["health_status"] == "healthy"


def test_history_is_newest_stale_first_so_a_bounded_slice_is_drawable(tmp_path):
    """Sorting oldest-first and capping kept the rows least worth drawing."""
    conn = connect(tmp_path / "op.db")
    _runtime_row(conn, "pumpfun_solana:ws", '{"transport_stale_after_s":60}')
    payload = read_radar_snapshot(
        conn, now_iso="2026-07-26T12:00:10Z", stale_after_s=5
    )
    ages = [row["age_since_update_s"] for row in payload["history"]]
    assert ages == sorted(ages)


def test_a_scanned_source_stays_healthy_while_its_venue_is_quiet(tmp_path):
    """Pons: scan current, venue silent. Event age is activity data here.

    Pons produced no event after 2026-08-12 while its shared scan stayed current with
    zero open gaps. Grading on event age held a working lane degraded, and no backfill
    could clear it, because backfill does not manufacture events.
    """
    conn = connect(tmp_path / "op.db")
    _health_row(
        conn,
        "pons_v1_4663:ws",
        heartbeat_at="2026-07-26T12:00:00Z",
        last_event_at="2026-07-26T11:50:00Z",
        detail={
            "transport_stale_after_s": 60,
            "expected_event_max_age_s": 300,
            # DECLARES a scan contract, and is caught up
            "blocks_behind_head": 0,
            "max_blocks_behind_head": 600,
        },
    )
    source = read_radar_snapshot(
        conn, now_iso="2026-07-26T12:00:10Z", stale_after_s=5
    )["source_health"][0]

    assert source["event_stale"] is True, "silence must stay VISIBLE"
    assert source["scan_behind"] is False
    assert source["stale"] is False, "a current scan outranks event age"
    assert source["health_status"] == "healthy"


def test_a_scanless_source_that_stops_delivering_grades_degraded(tmp_path):
    """Pump: no scan contract, so event age is the ONLY delivery evidence.

    On 2026-08-07 publicnode accepted the pump.fun `logsSubscribe`, returned a
    subscription id, and delivered zero events. The socket heartbeated throughout and the
    lane read healthy for twenty minutes. Removing event age everywhere took that
    detector away.
    """
    conn = connect(tmp_path / "op.db")
    _health_row(
        conn,
        "pumpfun_solana:ws",
        heartbeat_at="2026-07-26T12:00:00Z",   # socket alive
        last_event_at="2026-07-26T11:50:00Z",  # but nothing delivered for 610s
        detail={
            "transport_stale_after_s": 60,
            "expected_event_max_age_s": 300,
            # NO blocks_behind_head / max_blocks_behind_head: no scan contract
        },
    )
    source = read_radar_snapshot(
        conn, now_iso="2026-07-26T12:00:10Z", stale_after_s=5
    )["source_health"][0]

    assert source["transport_stale"] is False, "the socket is alive; that is the trap"
    assert source["scan_behind"] is None, "this lane declares no scan contract"
    assert source["event_stale"] is True
    assert source["stale"] is True, "a subscribed-but-silent lane must not read healthy"
