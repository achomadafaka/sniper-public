from __future__ import annotations

import hashlib

import pytest

from sniper.research.memecoin_opportunity.landmarks import (
    LandmarkStatus,
    LaunchSnapshot,
    OutOfOrderObservation,
    append_landmark,
    list_entry_eligible_landmarks,
    materialize_landmarks,
)
from sniper.research.memecoin_opportunity.store import (
    LaunchIdentity,
    append_canonical_event,
    append_raw_event,
    connect,
    upsert_launch_identity,
)
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


NOW = "2026-07-26T14:00:00Z"


def _snapshot(**overrides) -> LaunchSnapshot:
    values = {
        "launch_id": "launch-1",
        "observed_event_id": "event-observed",
        "observed_at": "2026-07-26T14:00:05Z",
        "age_ms": 5_000,
        "progress_bps": 1_000,
        "confirmation": ConfirmationState.CONFIRMED,
        "state_fingerprint": "state-v1",
        "source_gap_open": False,
    }
    values.update(overrides)
    return LaunchSnapshot(**values)


def _raw(raw_event_id: str, event_id: str, confirmation: ConfirmationState) -> RawEvent:
    payload = f'{{"event":"{event_id}"}}'
    order = 1 if event_id == "event-launch" else 2 if "tentative" in event_id else 3
    return RawEvent(
        raw_event_id=raw_event_id,
        network_id=NetworkId.ROBINHOOD_4663,
        venue_id=VenueId.BAGS_ROBINHOOD_4663,
        source_key="4663:bags:provider-a",
        source_provider="provider-a",
        source_cursor=f"101:0:{order}",
        token_id="0xabc",
        transaction_id=f"0xtx{order}",
        chain_order=ChainOrder(101, 0, order, "0xblock"),
        chain_timestamp=NOW,
        received_at=NOW,
        confirmation=confirmation,
        raw_payload_json=payload,
        raw_payload_hash=hashlib.sha256(payload.encode()).hexdigest(),
    )


def _event(
    canonical_event_id: str,
    raw_event_id: str,
    confirmation: ConfirmationState,
    *,
    event_kind: EventKind = EventKind.BUY,
) -> CanonicalEvent:
    order = 1 if canonical_event_id == "event-launch" else 2 if "tentative" in canonical_event_id else 3
    return CanonicalEvent(
        canonical_event_id=canonical_event_id,
        raw_event_id=raw_event_id,
        launch_id="launch-1",
        network_id=NetworkId.ROBINHOOD_4663,
        venue_id=VenueId.BAGS_ROBINHOOD_4663,
        token_id="0xabc",
        lifecycle=LifecycleState.PRE_MIGRATION,
        event_kind=event_kind,
        transaction_id=f"0xtx{order}",
        chain_order=ChainOrder(101, 0, order, "0xblock"),
        chain_timestamp=NOW,
        received_at=NOW,
        decoded_at=NOW,
        state_applied_at=NOW,
        confirmation=confirmation,
        actor_id="0xbuyer",
        quote_delta_atomic=1,
        token_delta_atomic=1,
        progress_bps=1_000,
        creator_linked=False,
        decoder_version="bags-4663-v1",
    )


def _seed_launch(conn):
    raw = _raw("raw-launch", "event-launch", ConfirmationState.CONFIRMED)
    event = _event(
        "event-launch",
        "raw-launch",
        ConfirmationState.CONFIRMED,
        event_kind=EventKind.LAUNCH,
    )
    append_raw_event(conn, raw)
    append_canonical_event(conn, event)
    upsert_launch_identity(
        conn,
        LaunchIdentity(
            launch_id="launch-1",
            network_id=NetworkId.ROBINHOOD_4663,
            venue_id=VenueId.BAGS_ROBINHOOD_4663,
            token_id="0xabc",
            creator_id="0xcreator",
            launch_event_id="event-launch",
            launched_at=NOW,
        ),
    )


def _append_observed_event(conn, event_id: str, confirmation: ConfirmationState):
    raw_id = f"raw-{event_id}"
    append_raw_event(conn, _raw(raw_id, event_id, confirmation))
    append_canonical_event(conn, _event(event_id, raw_id, confirmation))


def test_first_snapshot_tags_every_already_crossed_threshold_in_one_state():
    state = materialize_landmarks(
        None,
        _snapshot(age_ms=5_500, progress_bps=1_200),
    )

    assert state.crossed_time_ms == (1_000, 2_000, 5_000)
    assert state.crossed_progress_bps == (200, 500, 1_000)


def test_one_event_crossing_multiple_time_and_progress_thresholds_emits_one_state():
    previous = _snapshot(age_ms=1_000, progress_bps=200, observed_event_id="event-previous")
    current = _snapshot(age_ms=10_500, progress_bps=2_500)

    state = materialize_landmarks(previous, current)

    assert state is not None
    assert state.crossed_time_ms == (2_000, 5_000, 10_000)
    assert state.crossed_progress_bps == (500, 1_000, 2_000)


def test_age_jump_tags_only_new_time_thresholds():
    previous = _snapshot(age_ms=2_000, progress_bps=1_000, observed_event_id="event-previous")
    current = _snapshot(age_ms=20_000, progress_bps=1_000)

    state = materialize_landmarks(previous, current)

    assert state.crossed_time_ms == (5_000, 10_000, 20_000)
    assert state.crossed_progress_bps == ()


def test_progress_jump_tags_only_new_progress_thresholds():
    previous = _snapshot(age_ms=5_000, progress_bps=500, observed_event_id="event-previous")
    current = _snapshot(age_ms=5_500, progress_bps=6_500)

    state = materialize_landmarks(previous, current)

    assert state.crossed_time_ms == ()
    assert state.crossed_progress_bps == (1_000, 2_000, 4_000, 6_000)


def test_snapshot_without_a_new_crossing_emits_no_state():
    previous = _snapshot(age_ms=5_000, progress_bps=1_000, observed_event_id="event-previous")
    current = _snapshot(age_ms=5_500, progress_bps=1_100)

    assert materialize_landmarks(previous, current) is None


def test_progress_regression_is_rejected_without_reorg_correction():
    previous = _snapshot(age_ms=5_000, progress_bps=1_000, observed_event_id="event-previous")
    current = _snapshot(age_ms=6_000, progress_bps=900)

    with pytest.raises(OutOfOrderObservation, match="progress"):
        materialize_landmarks(previous, current)


def test_open_source_gap_emits_ineligible_landmark_with_reason():
    state = materialize_landmarks(None, _snapshot(source_gap_open=True))

    assert state.status == LandmarkStatus.INELIGIBLE
    assert state.ineligible_reason == "source_gap_open"


def test_tentative_landmark_can_be_superseded_by_canonical_landmark(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    _seed_launch(conn)
    _append_observed_event(conn, "event-tentative", ConfirmationState.TENTATIVE)
    _append_observed_event(conn, "event-confirmed", ConfirmationState.CONFIRMED)
    tentative = materialize_landmarks(
        None,
        _snapshot(
            observed_event_id="event-tentative",
            confirmation=ConfirmationState.TENTATIVE,
        ),
    )
    append_landmark(conn, tentative)
    canonical = materialize_landmarks(
        _snapshot(
            observed_event_id="event-tentative",
            confirmation=ConfirmationState.TENTATIVE,
        ),
        _snapshot(
            observed_event_id="event-confirmed",
            confirmation=ConfirmationState.CONFIRMED,
            supersedes_landmark_id=tentative.landmark_id,
        ),
    )

    assert canonical.crossed_time_ms == ()
    assert canonical.crossed_progress_bps == ()
    assert canonical.status == LandmarkStatus.CANONICAL
    assert append_landmark(conn, canonical) is True
    assert [row["status"] for row in list_entry_eligible_landmarks(conn, "launch-1")] == [
        "tentative"
    ]


def test_orphaned_tentative_landmark_is_preserved_for_chain_reorg_label(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    _seed_launch(conn)
    _append_observed_event(conn, "event-tentative", ConfirmationState.TENTATIVE)
    _append_observed_event(conn, "event-orphaned", ConfirmationState.ORPHANED)
    tentative = materialize_landmarks(
        None,
        _snapshot(
            observed_event_id="event-tentative",
            confirmation=ConfirmationState.TENTATIVE,
        ),
    )
    append_landmark(conn, tentative)
    orphaned = materialize_landmarks(
        _snapshot(
            observed_event_id="event-tentative",
            confirmation=ConfirmationState.TENTATIVE,
        ),
        _snapshot(
            observed_event_id="event-orphaned",
            confirmation=ConfirmationState.ORPHANED,
            supersedes_landmark_id=tentative.landmark_id,
        ),
    )

    append_landmark(conn, orphaned)

    rows = conn.execute(
        "SELECT landmark_id, status, supersedes_landmark_id FROM op_landmark_states ORDER BY inserted_at, landmark_id"
    ).fetchall()
    assert {row["status"] for row in rows} == {"tentative", "orphaned"}
    assert orphaned.supersedes_landmark_id == tentative.landmark_id
    assert [row["status"] for row in list_entry_eligible_landmarks(conn, "launch-1")] == ["tentative"]


def test_replayed_materialization_is_idempotent_in_store(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    _seed_launch(conn)
    _append_observed_event(conn, "event-confirmed", ConfirmationState.CONFIRMED)
    state = materialize_landmarks(
        None,
        _snapshot(observed_event_id="event-confirmed"),
    )

    assert append_landmark(conn, state) is True
    assert append_landmark(conn, state) is False
    assert conn.execute("SELECT count(*) FROM op_landmark_states").fetchone()[0] == 1
