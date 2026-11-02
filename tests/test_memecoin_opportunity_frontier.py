from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from scripts.memecoin_opportunity_worker import _finish_recovery

from sniper.research.memecoin_opportunity.frontier import (
    FrontierBatch,
    FrontierConflict,
    GapKind,
    SourceFrontier,
    apply_frontier_batch,
    decide_frontier,
    frontier_gap_is_discardable,
    list_open_gaps,
    load_frontier,
    rebase_frontier_after_discard,
    resolve_gap,
)
from sniper.research.memecoin_opportunity.continuity import (
    record_processed_cursor,
    record_source_failure,
)
from sniper.research.memecoin_opportunity.store import append_raw_event, connect
from sniper.research.memecoin_opportunity.types import (
    ChainOrder,
    ConfirmationState,
    NetworkId,
    RawEvent,
    VenueId,
)


NOW = "2026-07-26T14:00:00Z"
SOURCE_KEY = "4663:bags:provider-a"


def _raw_event(cursor: str, *, raw_event_id: str | None = None) -> RawEvent:
    payload = f'{{"cursor":"{cursor}"}}'
    return RawEvent(
        raw_event_id=raw_event_id or f"raw-{cursor}",
        network_id=NetworkId.ROBINHOOD_4663,
        venue_id=VenueId.BAGS_ROBINHOOD_4663,
        source_key=SOURCE_KEY,
        source_provider="provider-a",
        source_cursor=cursor,
        token_id="0xabc",
        transaction_id=f"0x{cursor}",
        chain_order=ChainOrder(int(cursor), 0, 0, f"0xblock{cursor}"),
        chain_timestamp=NOW,
        received_at=NOW,
        confirmation=ConfirmationState.CONFIRMED,
        raw_payload_json=payload,
        raw_payload_hash=hashlib.sha256(payload.encode()).hexdigest(),
    )


def _batch(**overrides) -> FrontierBatch:
    values = {
        "source_key": SOURCE_KEY,
        "expected_version": 0,
        "previous_durable_cursor": None,
        "newest_observed_cursor": "100",
        "contiguous_durable_cursor": "100",
        "resume_cursor": None,
        "reached_previous_frontier": True,
        "enumeration_complete": True,
        "decode_complete": True,
        "observed_at": NOW,
    }
    values.update(overrides)
    return FrontierBatch(**values)


def _seed_frontier(conn, cursor: str = "100") -> SourceFrontier:
    append_raw_event(conn, _raw_event(cursor))
    return apply_frontier_batch(
        conn,
        _batch(
            newest_observed_cursor=cursor,
            contiguous_durable_cursor=cursor,
        ),
    )


def test_complete_contiguous_batch_advances_frontier():
    decision = decide_frontier(None, _batch())

    assert decision.frontier.durable_cursor == "100"
    assert decision.frontier.resume_cursor is None
    assert decision.frontier.has_open_gap is False
    assert decision.frontier.version == 1
    assert decision.open_gap is False


def test_live_processed_cursor_advances_only_after_raw_event_exists(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_raw_event(conn, _raw_event("100"))
    frontier = record_processed_cursor(
        conn, source_key=SOURCE_KEY, source_cursor="100", observed_at=NOW,
        decode_complete=True,
    )
    assert frontier.durable_cursor == "100"
    assert frontier.has_open_gap is False


def test_source_failure_opens_durable_gap_and_new_events_cannot_skip_it(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_raw_event(conn, _raw_event("100"))
    before = record_processed_cursor(
        conn, source_key=SOURCE_KEY, source_cursor="100", observed_at=NOW,
        decode_complete=True,
    )
    failed = record_source_failure(
        conn, source_key=SOURCE_KEY, observed_at="2026-07-26T14:00:01Z",
        detail="websocket disconnected",
    )
    append_raw_event(conn, _raw_event("110"))
    after = record_processed_cursor(
        conn, source_key=SOURCE_KEY, source_cursor="110",
        observed_at="2026-07-26T14:00:02Z", decode_complete=True,
    )
    assert before.durable_cursor == "100"
    assert failed.has_open_gap is True
    assert after.durable_cursor == "100"
    assert after.has_open_gap is True


def test_rebase_frontier_after_discard_preserves_an_honest_resolved_gap(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_raw_event(conn, _raw_event("100"))
    record_processed_cursor(
        conn, source_key=SOURCE_KEY, source_cursor="100", observed_at=NOW,
        decode_complete=True,
    )
    record_source_failure(
        conn, source_key=SOURCE_KEY, observed_at="2026-07-26T14:00:01Z",
        detail="fixture disconnect",
    )
    append_raw_event(conn, _raw_event("110"))
    record_processed_cursor(
        conn, source_key=SOURCE_KEY, source_cursor="110",
        observed_at="2026-07-26T14:00:02Z", decode_complete=True,
    )

    frontier = rebase_frontier_after_discard(
        conn, SOURCE_KEY, observed_at="2026-07-26T14:00:03Z",
        detail="bounded recovery exhausted; affected launch states quarantined",
    )

    assert frontier.durable_cursor == "110"
    assert frontier.latest_observed_cursor == "110"
    assert frontier.has_open_gap is False
    assert list_open_gaps(conn, SOURCE_KEY) == []
    audit = conn.execute(
        "SELECT gap_kind,detail,resolved_at FROM op_data_gaps "
        "WHERE gap_kind='recovery_discarded'"
    ).fetchone()
    assert tuple(audit) == (
        "recovery_discarded",
        "bounded recovery exhausted; affected launch states quarantined",
        "2026-07-26T14:00:03Z",
    )


def test_decode_gap_is_not_discardable(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    _seed_frontier(conn, "100")
    append_raw_event(conn, _raw_event("101"))
    record_processed_cursor(
        conn,
        source_key=SOURCE_KEY,
        source_cursor="101",
        observed_at="2026-07-26T14:00:01Z",
        decode_complete=False,
    )

    assert frontier_gap_is_discardable(conn, SOURCE_KEY) is False
    with pytest.raises(ValueError, match="decode failure cannot be discarded"):
        rebase_frontier_after_discard(
            conn,
            SOURCE_KEY,
            observed_at="2026-07-26T14:00:02Z",
            detail="must remain blocked",
        )


def test_decode_gap_only_closes_when_failed_cursor_decodes_successfully(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_raw_event(conn, _raw_event("100"))
    record_processed_cursor(
        conn, source_key=SOURCE_KEY, source_cursor="100", observed_at=NOW,
        decode_complete=True,
    )
    append_raw_event(conn, _raw_event("101"))
    failed = record_processed_cursor(
        conn, source_key=SOURCE_KEY, source_cursor="101",
        observed_at="2026-07-26T14:00:01Z", decode_complete=False,
    )
    append_raw_event(conn, _raw_event("102"))
    later = record_processed_cursor(
        conn, source_key=SOURCE_KEY, source_cursor="102",
        observed_at="2026-07-26T14:00:02Z", decode_complete=True,
    )

    assert failed.durable_cursor == "100"
    assert later.durable_cursor == "100"
    assert later.resume_cursor == "101"
    assert list_open_gaps(conn, SOURCE_KEY)[0]["gap_kind"] == "decode_failure"

    retried = record_processed_cursor(
        conn, source_key=SOURCE_KEY, source_cursor="101",
        observed_at="2026-07-26T14:00:03Z", decode_complete=True,
    )

    assert retried.durable_cursor == "101"
    assert retried.has_open_gap is False
    assert list_open_gaps(conn, SOURCE_KEY) == []


def test_recovery_finish_never_clears_an_unresolved_decode_gap(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_raw_event(conn, _raw_event("100"))
    record_processed_cursor(
        conn, source_key=SOURCE_KEY, source_cursor="100", observed_at=NOW,
        decode_complete=True,
    )
    append_raw_event(conn, _raw_event("101"))
    record_processed_cursor(
        conn, source_key=SOURCE_KEY, source_cursor="101",
        observed_at="2026-07-26T14:00:01Z", decode_complete=False,
    )

    class Runtime:
        recovered = False

        def mark_recovered(self, _source_key):
            self.recovered = True

    runtime = Runtime()
    _finish_recovery(
        conn, runtime, SOURCE_KEY, observed_at="2026-07-26T14:00:02Z"
    )

    assert runtime.recovered is False
    assert load_frontier(conn, SOURCE_KEY).has_open_gap is True
    assert list_open_gaps(conn, SOURCE_KEY)[0]["gap_kind"] == "decode_failure"


def test_recovery_finish_closes_empty_bootstrap_gap(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    record_source_failure(
        conn,
        source_key=SOURCE_KEY,
        observed_at=NOW,
        detail="disconnected before first event",
    )

    class Runtime:
        recovered = False

        def mark_recovered(self, _source_key):
            self.recovered = True

    runtime = Runtime()
    _finish_recovery(
        conn,
        runtime,
        SOURCE_KEY,
        observed_at="2026-07-26T14:00:01Z",
    )

    frontier = load_frontier(conn, SOURCE_KEY)
    assert frontier.durable_cursor is None
    assert frontier.has_open_gap is False
    assert list_open_gaps(conn, SOURCE_KEY) == []
    assert runtime.recovered is True


def test_enumeration_bound_holds_frontier_and_sets_resume_cursor():
    before = SourceFrontier(SOURCE_KEY, "90", None, "90", False, 3, NOW)
    batch = _batch(
        expected_version=3,
        previous_durable_cursor="90",
        newest_observed_cursor="110",
        contiguous_durable_cursor="105",
        resume_cursor="105",
        reached_previous_frontier=False,
        enumeration_complete=False,
        gap_kind=GapKind.ENUMERATION_BOUND,
        gap_detail="page budget reached",
    )

    decision = decide_frontier(before, batch)

    assert decision.frontier.durable_cursor == "90"
    assert decision.frontier.resume_cursor == "105"
    assert decision.frontier.has_open_gap is True
    assert decision.open_gap is True


def test_decode_failure_holds_frontier_before_failed_event():
    before = SourceFrontier(SOURCE_KEY, "90", None, "90", False, 1, NOW)
    batch = _batch(
        expected_version=1,
        previous_durable_cursor="90",
        newest_observed_cursor="100",
        contiguous_durable_cursor="95",
        resume_cursor="96",
        decode_complete=False,
        gap_kind=GapKind.DECODE_FAILURE,
        gap_detail="event 96 unsupported",
    )

    decision = decide_frontier(before, batch)

    assert decision.frontier.durable_cursor == "90"
    assert decision.frontier.resume_cursor == "96"


def test_newer_observations_do_not_skip_an_existing_gap():
    before = SourceFrontier(SOURCE_KEY, "90", "96", "100", True, 2, NOW)
    batch = _batch(
        expected_version=2,
        previous_durable_cursor="90",
        newest_observed_cursor="120",
        contiguous_durable_cursor="120",
        reached_previous_frontier=True,
    )

    decision = decide_frontier(before, batch)

    assert decision.frontier.durable_cursor == "90"
    assert decision.frontier.resume_cursor == "96"
    assert decision.frontier.has_open_gap is True


def test_recovery_batch_advances_after_resolving_every_prior_gap(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    before = _seed_frontier(conn, "90")
    gap_state = apply_frontier_batch(
        conn,
        _batch(
            expected_version=before.version,
            previous_durable_cursor="90",
            newest_observed_cursor="110",
            contiguous_durable_cursor="95",
            resume_cursor="96",
            reached_previous_frontier=False,
            enumeration_complete=False,
            gap_kind=GapKind.ENUMERATION_BOUND,
            gap_detail="bounded",
        ),
    )
    gap_id = list_open_gaps(conn, SOURCE_KEY)[0]["gap_id"]
    append_raw_event(conn, _raw_event("110"))

    recovered = apply_frontier_batch(
        conn,
        _batch(
            expected_version=gap_state.version,
            previous_durable_cursor="90",
            newest_observed_cursor="110",
            contiguous_durable_cursor="110",
            resolved_gap_ids=(gap_id,),
        ),
    )

    assert recovered.durable_cursor == "110"
    assert recovered.resume_cursor is None
    assert recovered.has_open_gap is False
    assert list_open_gaps(conn, SOURCE_KEY) == []


def test_stale_frontier_version_is_rejected():
    current = SourceFrontier(SOURCE_KEY, "90", None, "90", False, 4, NOW)

    with pytest.raises(FrontierConflict, match="version"):
        decide_frontier(
            current,
            _batch(expected_version=3, previous_durable_cursor="90"),
        )


def test_gap_resolution_does_not_itself_advance_frontier(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    before = _seed_frontier(conn, "90")
    held = apply_frontier_batch(
        conn,
        _batch(
            expected_version=before.version,
            previous_durable_cursor="90",
            resume_cursor="96",
            reached_previous_frontier=False,
            enumeration_complete=False,
            gap_kind=GapKind.ENUMERATION_BOUND,
            gap_detail="bounded",
        ),
    )
    gap_id = list_open_gaps(conn, SOURCE_KEY)[0]["gap_id"]

    resolve_gap(conn, gap_id, resolved_at="2026-07-26T14:01:00Z")

    after = load_frontier(conn, SOURCE_KEY)
    assert after == held
    assert after.durable_cursor == "90"
    assert after.has_open_gap is True


def test_complete_batch_advances_after_gap_was_resolved_separately(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    before = _seed_frontier(conn, "90")
    held = apply_frontier_batch(
        conn,
        _batch(
            expected_version=before.version,
            previous_durable_cursor="90",
            resume_cursor="96",
            reached_previous_frontier=False,
            enumeration_complete=False,
            gap_kind=GapKind.ENUMERATION_BOUND,
            gap_detail="bounded",
        ),
    )
    gap_id = list_open_gaps(conn, SOURCE_KEY)[0]["gap_id"]
    resolve_gap(conn, gap_id, resolved_at="2026-07-26T14:01:00Z")
    append_raw_event(conn, _raw_event("110"))

    recovered = apply_frontier_batch(
        conn,
        _batch(
            expected_version=held.version,
            previous_durable_cursor="90",
            newest_observed_cursor="110",
            contiguous_durable_cursor="110",
        ),
    )

    assert recovered.durable_cursor == "110"
    assert recovered.has_open_gap is False


def test_frontier_and_gap_recovery_roll_back_atomically(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    before = _seed_frontier(conn, "90")
    held = apply_frontier_batch(
        conn,
        _batch(
            expected_version=before.version,
            previous_durable_cursor="90",
            resume_cursor="96",
            reached_previous_frontier=False,
            enumeration_complete=False,
            gap_kind=GapKind.ENUMERATION_BOUND,
            gap_detail="bounded",
        ),
    )
    gap_id = list_open_gaps(conn, SOURCE_KEY)[0]["gap_id"]

    with pytest.raises(ValueError, match="durable cursor"):
        apply_frontier_batch(
            conn,
            _batch(
                expected_version=held.version,
                previous_durable_cursor="90",
                newest_observed_cursor="999",
                contiguous_durable_cursor="999",
                resolved_gap_ids=(gap_id,),
            ),
        )

    assert load_frontier(conn, SOURCE_KEY) == held
    assert [row["gap_id"] for row in list_open_gaps(conn, SOURCE_KEY)] == [gap_id]


def test_replayed_incomplete_batch_does_not_duplicate_gap(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    before = _seed_frontier(conn, "90")
    batch = _batch(
        expected_version=before.version,
        previous_durable_cursor="90",
        resume_cursor="96",
        reached_previous_frontier=False,
        enumeration_complete=False,
        gap_kind=GapKind.ENUMERATION_BOUND,
        gap_detail="bounded",
    )
    once = apply_frontier_batch(conn, batch)

    apply_frontier_batch(conn, replace(batch, expected_version=once.version))

    assert conn.execute("SELECT count(*) FROM op_data_gaps").fetchone()[0] == 1


def test_recovery_chunk_replaces_resolved_resume_cursor(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    before = _seed_frontier(conn, "90")
    first = apply_frontier_batch(
        conn,
        _batch(
            expected_version=before.version,
            previous_durable_cursor="90",
            resume_cursor="page-1",
            reached_previous_frontier=False,
            enumeration_complete=False,
            gap_kind=GapKind.ENUMERATION_BOUND,
            gap_detail="first bounded page",
        ),
    )
    first_gap = list_open_gaps(conn, SOURCE_KEY)[0]["gap_id"]

    second = apply_frontier_batch(
        conn,
        _batch(
            expected_version=first.version,
            previous_durable_cursor="90",
            resume_cursor="page-2",
            reached_previous_frontier=False,
            enumeration_complete=False,
            gap_kind=GapKind.ENUMERATION_BOUND,
            gap_detail="second bounded page",
            resolved_gap_ids=(first_gap,),
        ),
    )

    assert second.resume_cursor == "page-2"
    open_gaps = list_open_gaps(conn, SOURCE_KEY)
    assert len(open_gaps) == 1
    assert open_gaps[0]["resume_cursor"] == "page-2"


def test_newer_observation_of_same_open_gap_updates_frontier_without_conflict(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    before = _seed_frontier(conn, "90")
    batch = _batch(
        expected_version=before.version,
        previous_durable_cursor="90",
        newest_observed_cursor="100",
        resume_cursor="96",
        reached_previous_frontier=False,
        enumeration_complete=False,
        gap_kind=GapKind.ENUMERATION_BOUND,
        gap_detail="bounded at 100",
    )
    once = apply_frontier_batch(conn, batch)

    twice = apply_frontier_batch(
        conn,
        replace(
            batch,
            expected_version=once.version,
            newest_observed_cursor="120",
            gap_detail="bounded at 120",
        ),
    )

    assert twice.durable_cursor == "90"
    assert twice.latest_observed_cursor == "120"
    assert conn.execute("SELECT count(*) FROM op_data_gaps").fetchone()[0] == 1


def test_durable_cursor_must_exist_in_same_source_raw_ledger(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")

    with pytest.raises(ValueError, match="durable cursor"):
        apply_frontier_batch(conn, _batch())

    assert load_frontier(conn, SOURCE_KEY) is None


def test_repeat_failure_at_unchanged_frontier_opens_a_new_gap(tmp_path):
    """A quiet source that fails, recovers, then fails again at the same cursor.

    `bags_solana:ws` held one cursor for 17h. Its second transport failure
    recomputed the first gap's identity, which was already resolved, so
    `_persist_gap` raised `ImmutableConflict` from inside the capture lane's own
    `except` handler and killed every Bags shard with no heartbeat, no gap row and
    a runtime row frozen at `healthy`.
    """
    conn = connect(tmp_path / "opportunity.sqlite")
    _seed_frontier(conn, "100")

    record_source_failure(
        conn,
        source_key=SOURCE_KEY,
        observed_at="2026-07-26T14:01:00Z",
        detail="ConnectionClosedError: no close frame received or sent",
    )
    first_gap = list_open_gaps(conn, SOURCE_KEY)[0]
    recovered = apply_frontier_batch(
        conn,
        _batch(
            expected_version=load_frontier(conn, SOURCE_KEY).version,
            previous_durable_cursor="100",
            resolved_gap_ids=(first_gap["gap_id"],),
        ),
    )
    assert recovered.has_open_gap is False

    second = record_source_failure(
        conn,
        source_key=SOURCE_KEY,
        observed_at="2026-07-26T14:02:00Z",
        detail="InvalidStatus: server rejected WebSocket connection: HTTP 429",
    )

    assert second.has_open_gap is True
    open_gaps = list_open_gaps(conn, SOURCE_KEY)
    assert len(open_gaps) == 1
    assert open_gaps[0]["gap_id"] != first_gap["gap_id"]
    assert open_gaps[0]["detail"].startswith("InvalidStatus")
    assert conn.execute("SELECT count(*) FROM op_data_gaps").fetchone()[0] == 2
