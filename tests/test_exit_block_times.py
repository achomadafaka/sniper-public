"""Phase 1 Task 1 — block_times fail-closed map + bounded backfill (exit brain).

The tape stores block_no + ingest-time observed_at, NOT block timestamps. Time-based features
must window on true event time from an immutable block->time map. A window whose blocks lack a
recorded timestamp is DATA_INSUFFICIENT, never converted with an assumed constant. These tests
pin the fail-closed contract.
"""
import sqlite3

from sniper.persistence import db_ticks as t


def _mem():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t.ensure_block_times(c)
    return c


def test_block_ts_map_is_fail_closed():
    c = _mem()
    t.backfill_block_times(
        c, "4663", [100, 101],
        lambda b: {100: 1_700_000_000, 101: 1_700_000_003}.get(b),
        "2026-07-23T00:00:00+00:00")
    m = t.block_ts_map(c, "4663", [100, 101, 102])
    assert set(m) == {100, 101}                    # 102 unknown -> absent, never guessed


def test_backfill_is_immutable_and_idempotent():
    c = _mem()
    n1 = t.backfill_block_times(c, "4663", [100], lambda b: 1_700_000_000, "2026-07-23T00:00:00+00:00")
    # a re-run with a DIFFERENT timestamp must NOT overwrite (block time is immutable) and inserts nothing
    n2 = t.backfill_block_times(c, "4663", [100], lambda b: 9_999_999_999, "2026-07-23T01:00:00+00:00")
    assert n1 == 1 and n2 == 0
    assert t.block_ts_map(c, "4663", [100])[100].startswith("2023-11-14T")  # 1_700_000_000 UTC, unchanged


def test_backfill_skips_missing_headers_without_faking_them():
    c = _mem()
    # get_block_fn returns None for a block the RPC could not serve -> that block stays absent
    n = t.backfill_block_times(c, "4663", [100, 101, 102],
                               lambda b: 1_700_000_000 if b != 101 else None,
                               "2026-07-23T00:00:00+00:00")
    assert n == 2
    assert set(t.block_ts_map(c, "4663", [100, 101, 102])) == {100, 102}


def test_chain_scoped_and_empty_blocks():
    c = _mem()
    t.backfill_block_times(c, "4663", [100], lambda b: 1_700_000_000, "2026-07-23T00:00:00+00:00")
    assert t.block_ts_map(c, "8453", [100]) == {}   # different chain -> not shared
    assert t.block_ts_map(c, "4663", []) == {}      # no blocks queried -> empty, no SQL error
