"""The shared 4663 scan cursor: enumeration progress, not event durability.

Before this cursor existed the resume block came from the venues' EVENT frontiers. That
conflates two different facts. An event frontier only proves where an event LANDED, so a
quiet venue lags it and a retired venue freezes it. With
`MEMECOIN_OPPORTUNITY_4663_WARM_START_BLOCKS=0` the warm floor is the head, so a restart
after downtime could resume at the head and never enumerate the interval it missed.

The cursor is deliberately NOT an `op_source_frontiers` row: that writer rejects a cursor
with no row in `op_raw_events`, because a source frontier means "events durable through
C". Enumerating a range and finding nothing is still progress, and it must be storable.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from sniper.research.memecoin_opportunity.frontier import (
    FrontierBatch,
    apply_frontier_batch,
)
from sniper.research.memecoin_opportunity.store import (
    advance_evm_scan_cursor,
    connect,
    load_evm_scan_cursor,
)


CHAIN = "robinhood_4663"
NOW = "2026-08-19T16:00:00.000000Z"


def _conn():
    return connect(pathlib.Path(tempfile.mkdtemp()) / "op.db")


def test_an_event_frontier_cannot_hold_a_scan_cursor():
    """This is why the cursor has its own store rather than a source-frontier row."""
    conn = _conn()
    with pytest.raises(ValueError, match="absent from source raw ledger"):
        apply_frontier_batch(conn, FrontierBatch(
            source_key=f"{CHAIN}:scan", expected_version=0,
            previous_durable_cursor=None, newest_observed_cursor="40593387:0:0",
            contiguous_durable_cursor="40593387:0:0", resume_cursor="40593387:0:0",
            reached_previous_frontier=True, enumeration_complete=True,
            decode_complete=True, observed_at=NOW,
        ))


def test_the_cursor_starts_unset_and_records_its_seed_provenance():
    conn = _conn()
    assert load_evm_scan_cursor(conn, CHAIN) is None
    advance_evm_scan_cursor(
        conn, chain_id=CHAIN, through_block=40587380, now_iso=NOW,
        seeded_from="migration:event_frontier",
    )
    assert load_evm_scan_cursor(conn, CHAIN) == 40587380
    row = conn.execute(
        "SELECT seeded_from FROM op_evm_scan_cursors WHERE chain_id=?", (CHAIN,)
    ).fetchone()
    assert row[0] == "migration:event_frontier"


def test_an_empty_range_still_advances_the_cursor():
    """Enumerating a range and finding nothing is the fact being stored."""
    conn = _conn()
    advance_evm_scan_cursor(conn, chain_id=CHAIN, through_block=100, now_iso=NOW)
    # a later range that produced no event at all
    assert advance_evm_scan_cursor(
        conn, chain_id=CHAIN, through_block=600, now_iso=NOW
    ) == 600
    assert load_evm_scan_cursor(conn, CHAIN) == 600


def test_the_cursor_never_rewinds():
    conn = _conn()
    advance_evm_scan_cursor(conn, chain_id=CHAIN, through_block=40593387, now_iso=NOW)
    assert advance_evm_scan_cursor(conn, chain_id=CHAIN, through_block=1, now_iso=NOW) == 40593387
    assert load_evm_scan_cursor(conn, CHAIN) == 40593387
    with pytest.raises(Exception, match="rewind"):
        conn.execute("UPDATE op_evm_scan_cursors SET scanned_through_block=1")


def test_a_crash_before_the_cursor_commit_replays_the_range_never_skips_it():
    """Events commit first; the cursor commits after, in its own transaction.

    So the only reachable inconsistency is a range whose events are stored while the
    cursor still points behind them. Resuming then RE-enumerates that range, which
    duplicates work and never loses a block.
    """
    conn = _conn()
    advance_evm_scan_cursor(conn, chain_id=CHAIN, through_block=1_000, now_iso=NOW)
    # range 1001..1500 processed, crash before the cursor advanced
    assert load_evm_scan_cursor(conn, CHAIN) == 1_000
    resume_from = load_evm_scan_cursor(conn, CHAIN) + 1
    assert resume_from == 1_001, "must replay the range, not resume past it"


def test_resume_is_the_block_after_the_cursor():
    conn = _conn()
    advance_evm_scan_cursor(conn, chain_id=CHAIN, through_block=40593387, now_iso=NOW)
    assert load_evm_scan_cursor(conn, CHAIN) + 1 == 40593388


def test_downtime_is_backfilled_rather_than_skipped():
    """The failure this cursor exists to prevent.

    With warm start at 0 the warm floor is the head, so a restart with no cursor would
    resume at the head and never enumerate the downtime interval.
    """
    conn = _conn()
    before_downtime, head_after = 40593387, 40700000
    advance_evm_scan_cursor(conn, chain_id=CHAIN, through_block=before_downtime, now_iso=NOW)

    warm_start_blocks = 0
    warm_floor = max(0, head_after - warm_start_blocks + 1) if warm_start_blocks else head_after
    resume = load_evm_scan_cursor(conn, CHAIN) + 1

    assert warm_floor == head_after, "warm start 0 would have jumped to the head"
    assert resume == before_downtime + 1
    assert head_after - resume > 100_000, "the whole downtime interval is queued for backfill"


def test_the_cursor_is_shared_and_survives_a_retired_venue():
    """A retired venue's event frontier freezes; the chain position must not."""
    conn = _conn()
    advance_evm_scan_cursor(conn, chain_id=CHAIN, through_block=40587381, now_iso=NOW)
    # bags is retired and writes no further events, so nothing updates its frontier
    for block in (40590000, 40593387, 40600000):
        advance_evm_scan_cursor(conn, chain_id=CHAIN, through_block=block, now_iso=NOW)
    assert load_evm_scan_cursor(conn, CHAIN) == 40600000
    # one row for the chain, not one per venue
    assert conn.execute("SELECT COUNT(*) FROM op_evm_scan_cursors").fetchone()[0] == 1


def test_the_cursor_survives_the_schema_migration_from_16():
    conn = _conn()
    assert conn.execute("SELECT version FROM op_schema_version").fetchone()[0] == 17
    conn.execute("UPDATE op_schema_version SET version=16")
    conn.execute("DROP TABLE op_evm_scan_cursors")
    path = pathlib.Path(conn.execute("PRAGMA database_list").fetchall()[0][2])
    conn.close()

    reopened = connect(path)
    assert reopened.execute("SELECT version FROM op_schema_version").fetchone()[0] == 17
    assert load_evm_scan_cursor(reopened, CHAIN) is None
    assert advance_evm_scan_cursor(
        reopened, chain_id=CHAIN, through_block=42, now_iso=NOW
    ) == 42


# --- real `_evm_poll_lane` integration -------------------------------------------------
# A no-op stub of `advance_evm_scan_cursor` passed every store-level test, so those tests
# proved arithmetic and nothing about the lane. These drive the real lane and assert the
# PERSISTED cursor.

import asyncio

import scripts.memecoin_opportunity_worker as worker


def _settings_stub(queue_max=256):
    """Minimal settings object for driving the real capture runtime."""
    class _S:
        pass
    st = _S()
    st.queue_max = queue_max
    st.stale_after_s = 15.0
    return st


def _lane_conn(tmp_path, frontiers=(("bags_robinhood_4663:ws", 100), ("pons_v1_4663:ws", 500))):
    conn = connect(tmp_path / "op.db")
    conn.executemany(
        "INSERT INTO op_source_frontiers VALUES(?,?,?,?,?,?,?)",
        [(key, f"{blk}:0:0", None, f"{blk}:0:0", 0, 1, "2026-07-29T12:00:00Z")
         for key, blk in frontiers],
    )
    return conn


class _Runtime:
    def __init__(self, conn):
        self.conn = conn
        self.gapped_sources = set()
        self.heartbeats = []

    def mark_recovered(self, _source_key):
        pass

    def source_gapped(self, _source_key):
        return False

    def heartbeat(self, *args, **kwargs):
        # Record what the lane REPORTS. A no-op here made the health assertions vacuous.
        self.heartbeats.append((args, kwargs))


def _run_lane(monkeypatch, conn, activity, head=600, finalized=599, polls=1,
              return_runtime=False):
    """Drive the real lane for `polls` ranges.

    Stopping after ONE poll is what hid every multi-poll defect: an incomplete range
    followed by a complete one, and an unfinalized tail that the next poll skipped.
    `activity` may be a list, one entry per poll.
    """
    stop = asyncio.Event()
    calls = []
    results = activity if isinstance(activity, list) else [activity]

    async def rpc_batch(*_args, **_kwargs):
        return [hex(head), {"number": hex(finalized)}]

    async def poll_range(_runtime, *, from_block, to_block, canonical_through_block=None, **_kw):
        calls.append((from_block, to_block, canonical_through_block))
        result = results[min(len(calls) - 1, len(results) - 1)]
        if len(calls) >= polls:
            stop.set()
        return {"events": 0, "launches": 0, "last_launch_at": None,
                "by_source": {}, **result}

    monkeypatch.setattr(worker, "_rpc_batch_call", rpc_batch)
    monkeypatch.setattr(worker, "_evm_poll_range", poll_range)
    runtime = _Runtime(conn)
    asyncio.run(worker._evm_poll_lane(
        runtime, rpc_url="https://fixture.invalid", poll_s=0.001, stop=stop,
    ))
    return runtime if return_runtime else calls


def test_lane_persists_the_cursor_only_up_to_the_finalized_block(tmp_path, monkeypatch):
    """head=600 finalized=599 must store 599. A reorg can replace block 600."""
    conn = _lane_conn(tmp_path)
    calls = _run_lane(monkeypatch, conn, {"persisted_completely": True}, head=600, finalized=599)
    # the lane now POLLS only to finality, so the tail is never read-and-forgotten
    assert calls == [(500, 599, 599)]
    assert load_evm_scan_cursor(conn, CHAIN) == 599, "must not record an unfinalized block"


def test_lane_does_not_advance_the_cursor_when_the_range_did_not_persist(tmp_path, monkeypatch):
    """`drain` swallows a processing failure and a rejected submission never queues.

    Either leaves a hole, so the cursor must stay behind it and force a replay.
    """
    conn = _lane_conn(tmp_path)
    advance_evm_scan_cursor(conn, chain_id=CHAIN, through_block=499, now_iso=NOW)
    seeded = load_evm_scan_cursor(conn, CHAIN)
    _run_lane(monkeypatch, conn, {"persisted_completely": False}, head=600, finalized=599)
    after = load_evm_scan_cursor(conn, CHAIN)
    assert after == seeded == 499, f"cursor moved past an incomplete range: {seeded} -> {after}"


def test_lane_treats_a_missing_completeness_flag_as_incomplete(tmp_path, monkeypatch):
    """Fail closed: a caller that does not report completeness must not advance it."""
    conn = _lane_conn(tmp_path)
    advance_evm_scan_cursor(conn, chain_id=CHAIN, through_block=499, now_iso=NOW)
    _run_lane(monkeypatch, conn, {}, head=600, finalized=599)
    assert load_evm_scan_cursor(conn, CHAIN) == 499


def test_lane_seeds_from_the_newest_event_frontier_and_replays_it_inclusive(tmp_path, monkeypatch):
    conn = _lane_conn(tmp_path)
    calls = _run_lane(monkeypatch, conn, {"persisted_completely": True})
    assert calls[0][0] == 500, "replays the newest event frontier block inclusive"
    row = conn.execute(
        "SELECT seeded_from FROM op_evm_scan_cursors WHERE chain_id=?", (CHAIN,)
    ).fetchone()
    assert row[0] in {"migration:event_frontier", "range_complete"}


def test_lane_resumes_from_the_cursor_not_from_the_event_frontiers(tmp_path, monkeypatch):
    """A retired venue freezes its event frontier; the cursor must still drive resume."""
    conn = _lane_conn(tmp_path)
    advance_evm_scan_cursor(conn, chain_id=CHAIN, through_block=580, now_iso=NOW)
    calls = _run_lane(monkeypatch, conn, {"persisted_completely": True}, head=600, finalized=599)
    assert calls[0][0] == 581, "resume is cursor+1, not the newest frontier (500)"


# --- multi-poll: the cases a single-poll harness cannot see ---------------------------

def test_an_incomplete_range_is_replayed_and_never_stepped_over(tmp_path, monkeypatch):
    """Incomplete 500..600 then complete 601 stored cursor 601 and hid the hole."""
    conn = _lane_conn(tmp_path)
    advance_evm_scan_cursor(conn, chain_id=CHAIN, through_block=499, now_iso=NOW)
    calls = _run_lane(
        monkeypatch, conn,
        [{"persisted_completely": False}, {"persisted_completely": True}],
        head=700, finalized=700, polls=2,
    )
    # The second poll must RE-READ the same range rather than move past it. The cursor
    # may then advance, but only because the hole was actually replayed.
    assert len(calls) == 2, f"expected a replay, got {calls}"
    assert calls[0][0] == calls[1][0] == 500, f"stepped over the hole: {calls}"
    assert calls[0][1] == calls[1][1], f"replayed a different range: {calls}"
    # and the cursor never sits above a range that was not persisted
    assert load_evm_scan_cursor(conn, CHAIN) == calls[1][1]


def test_the_unfinalized_tail_is_never_polled_and_so_never_skipped(tmp_path, monkeypatch):
    """head=600 finalized=599: reading to 600 then clamping left 600 read once, forever.

    Polling only finalized blocks keeps `next_block` and the cursor in step.
    """
    conn = _lane_conn(tmp_path)
    advance_evm_scan_cursor(conn, chain_id=CHAIN, through_block=499, now_iso=NOW)
    calls = _run_lane(
        monkeypatch, conn, {"persisted_completely": True},
        head=600, finalized=599, polls=1,
    )
    assert calls[0][1] == 599, f"polled past finality: {calls}"
    assert load_evm_scan_cursor(conn, CHAIN) == 599
    # block 600 is still ahead of the cursor, so a later poll will read it
    assert load_evm_scan_cursor(conn, CHAIN) + 1 == 600


def test_a_repeat_failure_on_an_already_gapped_source_is_counted_not_inferred(tmp_path):
    """Execute the drain. Reading its source proved nothing about its behaviour.

    Comparing `gapped_sources` before and after only detects a NEW key, so a second
    failure on an already-gapped source looked like success.
    """
    from sniper.services.memecoin_opportunity_runtime import OpportunityCaptureRuntime

    conn = connect(tmp_path / "op.db")

    class _Env:
        source_cursor = "1:0:0"

    def _boom(_envelope):
        raise RuntimeError("processing failed")

    runtime = OpportunityCaptureRuntime(
        conn=conn, settings=_settings_stub(), process_fn=_boom,
        now_fn=lambda: "2026-08-19T16:00:00.000000Z",
    )
    runtime.gapped_sources.add("pons_v1_4663:ws")  # ALREADY gapped
    runtime.queue.append(("pons_v1_4663:ws", _Env()))
    runtime.drain(max_items=1)
    assert runtime.drain_failures == 1, "a repeat failure must be counted, not inferred"


def test_gap_abandonment_never_moves_the_scan_position(tmp_path, monkeypatch):
    """With no scan row, abandonment set next_block=head and skipped the seed range.

    The earlier version never supplied `abandon_fn` or an open gap, so the abandonment
    branch was never entered and the test proved nothing.
    """
    conn = _lane_conn(tmp_path)
    # an OPEN, discardable gap on the quiet lane is what triggers abandonment
    conn.execute(
        "UPDATE op_source_frontiers SET has_open_gap=1 WHERE source_key='bags_robinhood_4663:ws'"
    )
    abandoned = []

    stop = asyncio.Event()
    calls = []

    async def rpc_batch(*_args, **_kwargs):
        return [hex(600), {"number": hex(600)}]

    async def poll_range(_runtime, *, from_block, to_block, canonical_through_block=None, **_kw):
        calls.append((from_block, to_block, canonical_through_block))
        stop.set()
        return {"events": 0, "launches": 0, "last_launch_at": None, "by_source": {},
                "persisted_completely": True, "rejected_submissions": 0,
                "drain_failures": 0, "still_queued": 0}

    monkeypatch.setattr(worker, "_rpc_batch_call", rpc_batch)
    monkeypatch.setattr(worker, "_evm_poll_range", poll_range)
    monkeypatch.setattr(worker, "frontier_gap_is_discardable", lambda *_a, **_k: True)

    asyncio.run(worker._evm_poll_lane(
        _Runtime(conn), rpc_url="https://fixture.invalid", poll_s=0.001, stop=stop,
        abandon_fn=lambda venue, at: abandoned.append(venue),
    ))

    assert abandoned, "the abandonment branch must actually run for this test to mean anything"
    assert calls[0][0] == 500, f"abandonment skipped the migration range: {calls}"


def test_a_foreign_queued_envelope_cannot_hide_an_unpersisted_range_event(tmp_path):
    """The queue is SHARED and `drain` takes from the oldest side.

    Draining `pending_items` could consume a foreign envelope queued earlier and leave
    this range's event queued, while the range still reported success. The caller would
    advance the scan cursor over an event that is still volatile.
    """
    from sniper.services.memecoin_opportunity_runtime import OpportunityCaptureRuntime

    conn = connect(tmp_path / "op.db")
    processed = []

    class _Env:
        def __init__(self, cursor):
            self.source_cursor = cursor

    runtime = OpportunityCaptureRuntime(
        conn=conn, settings=_settings_stub(), process_fn=lambda e: processed.append(e.source_cursor),
        now_fn=lambda: "2026-08-19T16:00:00.000000Z",
    )
    # a FOREIGN envelope queued before this range runs
    runtime.queue.append(("solana:ws", _Env("older-envelope")))
    # then this range's own event
    range_keys = {("pons_v1_4663:ws", "16:1:0")}
    runtime.queue.append(("pons_v1_4663:ws", _Env("16:1:0")))

    # draining only this range's item count consumes the FOREIGN one first
    worker._drain_evm_poll_batch(runtime, 1)
    assert processed == ["older-envelope"]
    assert worker._queued_range_items(runtime, range_keys) == 1, "range event still queued"

    # the range must keep draining until its own envelopes are gone
    worker._drain_until_range_is_clear(runtime, range_keys)
    assert worker._queued_range_items(runtime, range_keys) == 0
    assert "16:1:0" in processed


def test_health_reports_the_durable_cursor_not_the_loop_variable(tmp_path, monkeypatch):
    conn = _lane_conn(tmp_path)
    advance_evm_scan_cursor(conn, chain_id=CHAIN, through_block=499, now_iso=NOW)
    runtime = _run_lane(
        monkeypatch, conn, {"persisted_completely": False},
        head=600, finalized=600, polls=1, return_runtime=True,
    )
    # Unconditional: the earlier version guarded every assertion behind `if` and read a
    # row that a no-op heartbeat never wrote, so it asserted nothing at all.
    details = [
        kwargs["detail"] for _args, kwargs in runtime.heartbeats
        if isinstance(kwargs.get("detail"), dict)
        and "scanned_through_block" in kwargs["detail"]
    ]
    assert details, "the lane must report health even after an incomplete range"
    assert details[-1]["scanned_through_block"] == 499, (
        "health must report the DURABLE cursor, not the in-memory loop variable"
    )
    assert details[-1]["blocks_behind_head"] == 600 - 499


def test_a_reader_spans_the_16_to_17_migration_window():
    """A shared database cannot bump schema unless the reader accepts both versions.

    The 15->16 bump took the Radar UI dark because the reader demanded exact equality,
    so whichever runtime was repointed second rejected the database for the whole
    window. Deploy order follows from this: the READER goes first.
    """
    import tempfile as _tf
    from pathlib import Path as _P

    from sniper.research.memecoin_opportunity.store import (
        READER_COMPATIBLE_SCHEMA_VERSIONS,
        SCHEMA_VERSION,
        connect_existing,
    )

    assert READER_COMPATIBLE_SCHEMA_VERSIONS == (SCHEMA_VERSION, SCHEMA_VERSION - 1)

    path = _P(_tf.mkdtemp()) / "op.db"
    writer = connect(path)
    writer.execute("UPDATE op_schema_version SET version=?", (SCHEMA_VERSION - 1,))
    writer.close()

    # a reader on the PREVIOUS version must open, so it survives the pre-migration window
    older = connect_existing(path)
    assert older.execute("SELECT version FROM op_schema_version").fetchone()[0] == SCHEMA_VERSION - 1
    older.close()

    # the writer migrates, and the same reader still opens
    migrated = connect(path)
    assert migrated.execute("SELECT version FROM op_schema_version").fetchone()[0] == SCHEMA_VERSION
    migrated.close()
    current = connect_existing(path)
    assert current.execute("SELECT version FROM op_schema_version").fetchone()[0] == SCHEMA_VERSION
    current.close()

    # two versions back is still refused: the window is one, not unbounded
    stale = connect(path)
    stale.execute("UPDATE op_schema_version SET version=?", (SCHEMA_VERSION - 2,))
    stale.close()
    with pytest.raises(Exception, match="unsupported opportunity schema"):
        connect_existing(path)


def test_evm_poll_range_itself_clears_its_envelopes_from_the_shared_queue(tmp_path, monkeypatch):
    """Drive the REAL `_evm_poll_range`, not its helpers.

    The helper-level regression passes even if the wiring in `_evm_poll_range`
    disappears, because it calls `_drain_until_range_is_clear` itself. This drives the
    range end to end with a foreign envelope already queued, so only the wiring can
    make it pass.
    """
    from sniper.services.memecoin_opportunity_runtime import OpportunityCaptureRuntime

    conn = connect(tmp_path / "op.db")
    processed = []

    class _Foreign:
        source_cursor = "older-envelope"

    runtime = OpportunityCaptureRuntime(
        conn=conn, settings=_settings_stub(),
        process_fn=lambda e: processed.append(e.source_cursor),
        now_fn=lambda: NOW,
    )
    # queued BEFORE the range runs, by a different source task
    runtime.queue.append(("solana:ws", _Foreign()))

    log = {
        "address": worker.PONS_FACTORY,
        "topics": [worker.PONS_TOKEN_LAUNCHED],
        "data": "0x",
        "blockNumber": "0x10",
        "transactionIndex": "0x1",
        "logIndex": "0x0",
        "blockHash": "0xblock10",
        "transactionHash": "0xtx-a",
        "removed": False,
    }

    async def rpc_batch(*args, **_kwargs):
        calls = args[-1]
        if calls and calls[0][0] == "eth_getLogs":
            return [[log] for _ in calls]
        return [{"timestamp": "0x64"} for _ in calls]

    monkeypatch.setattr(worker, "_rpc_batch_call", rpc_batch)

    summary = asyncio.run(worker._evm_poll_range(
        runtime, rpc_url="https://fixture.invalid", from_block=16, to_block=16,
    ))

    assert summary["persisted_completely"] is True
    assert summary["drain_failures"] == 0
    assert summary["still_queued"] == 0, "the range left its own envelope queued"
    assert processed[0] == "older-envelope", "the foreign envelope drains first, as before"
    assert "16:1:0" in processed, "the range's own event must have persisted"
    assert len(runtime.queue) == 0
