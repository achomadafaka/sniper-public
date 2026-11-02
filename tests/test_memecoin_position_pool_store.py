"""Persisted fill-anchored pool per position (2026-09-02).

`resolve_pool_for_position` sits on the funded exit path at four call sites. An RPC inside it would
turn a transient network failure into `None`, which the caller reads as "no usable pool" and
converts into an exit refusal indistinguishable from a genuine ambiguity afterwards. So the receipt
is read ONCE, persisted, and the resolver does a pure read.

Keyed by POSITION, not by mint: the whole point is that the fill knows something the mint does not,
and writing a fill-derived answer into mint-keyed `pool_meta` would give that column two writers
with different semantics — the trap that cost hours on `source_drift_pct`.

`resolution_tx` and `resolution_log_index` are stored so the derivation stays auditable: a bare
address nobody can re-derive is not evidence.
"""
import sqlite3

import pytest

from sniper.services.memecoin_position_pool import (
    ensure_position_pool_table, get_position_pool, record_position_pool)


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_position_pool_table(c)
    return c


POOL = "0x" + "b1" * 32
TX = "0x" + "de" * 32
MINT = "0x" + "aa" * 20


def test_a_recorded_pool_reads_back_by_position():
    c = _db()
    assert record_position_pool(c, 7, "4663", MINT, POOL, tx=TX, log_index=3,
                                now_iso="2026-09-02T10:00:00+00:00") is True
    assert get_position_pool(c, 7, "4663") == POOL.lower()


def test_the_derivation_is_auditable():
    c = _db()
    record_position_pool(c, 7, "4663", MINT, POOL, tx=TX, log_index=3,
                         now_iso="2026-09-02T10:00:00+00:00")
    row = c.execute("SELECT * FROM evm_position_pools WHERE position_id=7").fetchone()
    assert (row["resolution_tx"], row["resolution_log_index"]) == (TX.lower(), 3)
    assert row["venue_kind"] == "v4"     # 66-char id
    assert row["mint"] == MINT.lower()


def test_a_42_char_pool_records_as_v2_v3():
    c = _db()
    record_position_pool(c, 8, "4663", MINT, "0x" + "c2" * 20, tx=TX, log_index=0,
                         now_iso="2026-09-02T10:00:00+00:00")
    assert c.execute("SELECT venue_kind FROM evm_position_pools WHERE position_id=8"
                     ).fetchone()[0] == "v2v3"


def test_the_mapping_is_IMMUTABLE_once_written():
    """A position traded one pool. A second write means something is wrong upstream; it must not
    silently repoint a live position's price source."""
    c = _db()
    record_position_pool(c, 7, "4663", MINT, POOL, tx=TX, log_index=3,
                         now_iso="2026-09-02T10:00:00+00:00")
    other = "0x" + "e4" * 32
    assert record_position_pool(c, 7, "4663", MINT, other, tx=TX, log_index=9,
                                now_iso="2026-09-02T11:00:00+00:00") is False
    assert get_position_pool(c, 7, "4663") == POOL.lower()


def test_an_unresolved_position_reads_as_None_not_as_an_error():
    assert get_position_pool(_db(), 999, "4663") is None


def test_the_chain_is_part_of_the_key():
    """Position ids are per-ledger; never hand a 4663 pool to another chain's lookup."""
    c = _db()
    record_position_pool(c, 7, "4663", MINT, POOL, tx=TX, log_index=3,
                         now_iso="2026-09-02T10:00:00+00:00")
    assert get_position_pool(c, 7, "8453") is None


def test_a_missing_table_does_not_raise_on_read():
    """The resolver calls this on the funded exit path. A missing table must degrade to 'unknown',
    never to an exception that aborts an exit sweep."""
    bare = sqlite3.connect(":memory:")
    bare.row_factory = sqlite3.Row
    assert get_position_pool(bare, 7, "4663") is None


@pytest.mark.parametrize("bad", ["", None, "0xzz", "0x" + "ab" * 10])
def test_a_malformed_pool_is_refused(bad):
    """Only a 42-char address or a 66-char V4 pool id. A shape we cannot classify is not stored."""
    c = _db()
    assert record_position_pool(c, 7, "4663", MINT, bad, tx=TX, log_index=0,
                                now_iso="2026-09-02T10:00:00+00:00") is False
    assert get_position_pool(c, 7, "4663") is None
