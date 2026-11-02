"""A row that predates the hash column must be able to gain one.

N3 of the 2026-09-23 recheck. `backfill_block_times` skips a height it already has BEFORE it
consults `hash_fn`, so a row written before `block_hash` existed — or migrated in with NULL — can
never acquire an identity. Every tick in that block then reads `hash_unverified` forever, which the
m5 window turns into an unknown and the gate turns into a fall-back to the provider.

Identity enrichment requires a paired header and replaces the legacy timestamp too. A known
hash remains immutable here; unknown legacy times cannot borrow a hash from another observation.
"""
import sqlite3

import pytest

from sniper.persistence import db_ticks


CHAIN = "4663"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db_ticks.ensure_block_times(c)
    return c


def _row(c, block):
    return c.execute("SELECT block_ts, fetched_at, block_hash FROM block_times "
                     "WHERE chain_id=? AND block_no=?", (CHAIN, block)).fetchone()


def _seed(c, block, *, block_hash=None, ts="2026-09-01T00:00:00+00:00"):
    c.execute("INSERT INTO block_times (chain_id, block_no, block_ts, fetched_at, block_hash) "
              "VALUES (?,?,?,?,?)", (CHAIN, block, ts, "2026-09-01T00:00:00+00:00", block_hash))
    c.commit()


def test_an_existing_row_with_no_hash_gains_one():
    """⚠️ THE REACH VALUE IS THE PRE-SEEDED ROW. Without it the block is NEW and the insert path
    already carried the hash — the defect only exists for a height we already have."""
    c = _conn()
    _seed(c, 700)
    assert _row(c, 700)["block_hash"] is None
    db_ticks.backfill_block_times(c, CHAIN, [700], lambda b: 1_790_000_000, "2026-09-24T00:00:00+00:00",
                                  header_fn=lambda b: (1_790_000_000, "0xABC"))
    assert _row(c, 700)["block_hash"] == "0xabc", "lowercased, like every other identity write"


def test_hash_only_enrichment_leaves_the_legacy_row_unknown():
    """A height can name a new fork; a hash alone cannot verify the old time."""
    c = _conn()
    _seed(c, 700, ts="2026-09-01T00:00:00+00:00")
    db_ticks.backfill_block_times(c, CHAIN, [700], lambda b: 1_790_999_999, "2026-09-24T00:00:00+00:00",
                                  hash_fn=lambda b: "0xABC")
    row = _row(c, 700)
    assert row["block_ts"] == "2026-09-01T00:00:00+00:00"
    assert row["fetched_at"] == "2026-09-01T00:00:00+00:00"
    assert row["block_hash"] is None


def test_an_existing_hash_is_never_replaced():
    """A disagreeing hash is a REORG, a different operation with a different safety argument."""
    c = _conn()
    _seed(c, 700, block_hash="0xold")
    db_ticks.backfill_block_times(c, CHAIN, [700], lambda b: 1_790_000_000, "2026-09-24T00:00:00+00:00",
                                  header_fn=lambda b: (1_790_000_000, "0xnew"))
    assert _row(c, 700)["block_hash"] == "0xold"


def test_no_hash_available_leaves_the_row_exactly_as_it_was():
    c = _conn()
    _seed(c, 700)
    db_ticks.backfill_block_times(c, CHAIN, [700], lambda b: 1_790_000_000, "2026-09-24T00:00:00+00:00",
                                  hash_fn=lambda b: None)
    assert _row(c, 700)["block_hash"] is None


def test_a_raising_header_fn_cannot_break_the_pass():
    c = _conn()
    _seed(c, 700)
    _seed(c, 800)

    def boom(block):
        if block == 700:
            raise RuntimeError("no log for this height")
        return (1_790_000_000, "0xdef")

    db_ticks.backfill_block_times(c, CHAIN, [700, 800], lambda b: 1_790_000_000,
                                  "2026-09-24T00:00:00+00:00", header_fn=boom)
    assert _row(c, 700)["block_hash"] is None
    assert _row(c, 800)["block_hash"] == "0xdef"


def test_new_blocks_still_insert_with_their_hash():
    """The path that already worked must keep working."""
    c = _conn()
    n = db_ticks.backfill_block_times(c, CHAIN, [900], lambda b: 1_790_000_000,
                                      "2026-09-24T00:00:00+00:00", hash_fn=lambda b: "0xfeed")
    assert n == 1 and _row(c, 900)["block_hash"] == "0xfeed"


def test_the_return_value_still_counts_inserts_only():
    """An enrichment repairs a row; it does not insert one, and callers count inserts."""
    c = _conn()
    _seed(c, 700)
    n = db_ticks.backfill_block_times(c, CHAIN, [700, 900], lambda b: 1_790_000_000,
                                      "2026-09-24T00:00:00+00:00", header_fn=lambda b: (1_790_000_000, "0xaa"))
    assert n == 1, "only block 900 was new"
    assert _row(c, 700)["block_hash"] == "0xaa"


def test_hash_without_a_paired_timestamp_cannot_enrich_a_legacy_row():
    c = _conn()
    _seed(c, 700)
    db_ticks.backfill_block_times(c, CHAIN, [700], lambda b: None, "later",
                                  hash_fn=lambda b: "0xnew")
    assert _row(c, 700)["block_hash"] is None


def test_verified_header_replaces_legacy_time_and_hash_together():
    from datetime import datetime, timezone
    c = _conn()
    _seed(c, 700)
    stamp = 1_790_999_999
    db_ticks.backfill_block_times(c, CHAIN, [700], None, "later",
                                  header_fn=lambda b: (stamp, "0xNEW"))
    row = _row(c, 700)
    assert row["block_hash"] == "0xnew"
    assert row["block_ts"] == datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    assert row["fetched_at"] == "later"
