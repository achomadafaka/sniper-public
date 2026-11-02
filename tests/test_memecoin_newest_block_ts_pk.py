"""The volume gate's feed-age read walks block_times' primary key, not every header of the chain.

`_newest_block_ts` runs on every 4663/8453 entry's m5 gate read (memecoin_onchain_volume_m5).
`SELECT MAX(block_ts) ... WHERE chain_id=?` read all ~38.5k 4663 headers: p50 3.98 ms, p90 4.46 ms
on the live store, 2026-10-09. The newest height's time via the (chain_id, block_no) primary key
is p50/p90 0.007 ms and returns the same value (Gamma, measured on both chains). block_times
holds one row per height, REPLACEd on a reorg, and a chain's block times never decrease with
height, so the newest height is the newest header.
"""
import sqlite3
from datetime import datetime, timezone

import pytest

from sniper.services.memecoin_onchain_volume import _newest_block_ts

T0 = 1_791_500_000


def _store(n, *, text=True, chain="4663"):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE block_times (chain_id TEXT NOT NULL, block_no INTEGER NOT NULL, "
              "block_ts TEXT NOT NULL, fetched_at TEXT NOT NULL, block_hash TEXT, "
              "PRIMARY KEY (chain_id, block_no))")
    for i in range(n):
        ts = T0 + i // 4                         # several blocks per second, as on 4663
        c.execute("INSERT INTO block_times VALUES (?,?,?,?,?)",
                  (chain, 1000 + i,
                   datetime.fromtimestamp(ts, timezone.utc).isoformat() if text else ts, "", "0x"))
    c.execute("INSERT INTO block_times VALUES ('8453', 5, ?, '', '0x')",
              (datetime.fromtimestamp(T0 + 10_000, timezone.utc).isoformat(),))
    return c


@pytest.mark.parametrize("text", [True, False])
def test_the_newest_height_is_the_newest_header(text):
    c = _store(400, text=text)
    raw = c.execute("SELECT MAX(block_ts) FROM block_times WHERE chain_id='4663'").fetchone()[0]
    expected = float(raw) if not text else datetime.fromisoformat(raw).timestamp()
    assert _newest_block_ts(c, "4663") == expected == T0 + 399 // 4


def test_a_reorg_replaced_height_reads_its_canonical_time():
    c = _store(10)
    c.execute("INSERT OR REPLACE INTO block_times VALUES ('4663', 1009, ?, '', '0xnew')",
              (datetime.fromtimestamp(T0 + 3, timezone.utc).isoformat(),))
    assert _newest_block_ts(c, "4663") == T0 + 3


def test_an_empty_chain_is_none():
    assert _newest_block_ts(_store(5), "1") is None


def test_the_read_does_not_walk_every_header_of_the_chain():
    """The scan form costs a VM step per header; the primary-key walk a handful."""
    c = _store(5000)
    steps = []
    c.set_progress_handler(lambda: steps.append(1), 1)
    _newest_block_ts(c, "4663")
    c.set_progress_handler(None, 0)
    assert len(steps) < 200, len(steps)
