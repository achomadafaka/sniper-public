"""A height is only canonical if THIS pass could decide it.

F1 of the 2026-09-23 Codex recheck. `6baf398e` made the window sum join on canonical block identity,
and `dac76cb4` then made every write preserve an identity rather than erase one
(`COALESCE(excluded.block_hash, block_times.block_hash)`). Together they leave a hole: for an
interpolated, non-anchor height with no newly fetched log, the repair has NO new hash, COALESCE
keeps the OLD FORK's hash, and the surviving orphan tick matches that stale identity and passes the
join. The review's proof: a $2,400 orphan at block 750 against canonical swaps at 900 and 990 sums
$4,800 over two swaps with `reason=ok`, where the canonical chain holds $2,400 over one.

WHAT THE BACKFILL CAN AND CANNOT DECIDE, which is what scopes the repair. It issues exactly two log
requests per page: V4 swaps on the PoolManager address, and Pons `Launched` on the launcher. It
never re-fetches V3 swaps or Pons CURVE trades, because this endpoint refuses topic-only getLogs.
So a missing V4 log at a tick-bearing height is EVIDENCE (that pass asked for every V4 log in the
range and got none there), while a missing V3 or Pons log is nothing at all. The repair therefore
covers the V4 population only, and the V3/Pons exposure stays open and named.
"""
import runpy
import pathlib

import pytest

from sniper.services import memecoin_volume_writer as w

H = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1]
                       / "tests" / "test_memecoin_volume_writer.py"))
CHAIN, MINT, KEY, POOL = [H[k] for k in ("CHAIN", "MINT", "KEY", "POOL")]
V3_POOL = "0x" + "cd" * 20        # 42 chars: a V3 address, not a 66-char V4 pool id


def _tape(tmp_path):
    conn = w.connect(tmp_path / "v.db")
    tape = w.VolumeTape(conn, CHAIN, 1800.0)
    tape.register_pool(POOL, KEY, 1800.0)
    tape.set_quote_price(2400, 2000.0)
    return conn, tape


def _orphan_at(tape, block, block_hash="0xoldfork"):
    """One V4 swap stored from a block that the canonical chain later replaced.

    ⚠️ THE REACH VALUE IS THE BLOCK NUMBER. 750 is off the 60-block anchor grid AND below the
    240-block seed window, so the pass fetches no header for it and finds no log in it. Put the
    orphan at 900 or 990 and it is an anchor with a canonical swap, where the defect cannot appear —
    which is exactly how the block-hash regression test passed against its own bug earlier today.
    """
    tape.head({"number": hex(block), "timestamp": hex(1000 + block), "hash": block_hash}, 1750.0)
    tape.log(H["swap"](block, tx="0xorphan", block_hash=block_hash), 1750.0)
    tape.reset(1990.0, "reorg")


def test_an_orphan_at_a_height_the_pass_cannot_confirm_is_excluded(tmp_path):
    conn, tape = _tape(tmp_path)
    _orphan_at(tape, 750)
    w.backfill_window(tape, CHAIN, H["_chain_call"](), 2000.0, stored_pool_key=lambda *a: KEY)
    out = w.measure(conn, CHAIN, MINT, 2001.0, exclude_tx="0xback900")
    assert out["usd"] == 2400 and out["swaps"] == 1, out
    assert out["reason"] == "ok", "a decided orphan is an exclusion, not an unknown"
    conn.close()


def test_the_repair_reads_the_real_header_rather_than_assuming_the_tick_is_wrong(tmp_path):
    """A canonical tick whose log this pass simply missed must be CONFIRMED, not dropped.

    This is why the repair fetches a header instead of NULLing the identity: a header read can
    confirm or refute, it cannot invent. The fixture gives block 800 the hash the fake chain will
    return, so the only honest answer is to keep it.
    """
    conn, tape = _tape(tmp_path)
    canonical = "0x%064x" % 800
    _orphan_at(tape, 800, block_hash=canonical)
    w.backfill_window(tape, CHAIN, H["_chain_call"](), 2000.0, stored_pool_key=lambda *a: KEY)
    out = w.measure(conn, CHAIN, MINT, 2001.0, exclude_tx="0xback900")
    assert out["swaps"] == 2, "the tick at 800 is on the canonical chain and must still count"
    assert out["usd"] == 4800, out
    conn.close()


def test_a_v3_height_is_left_exactly_as_it_was(tmp_path):
    """The pass asks for no V3 logs, so their absence is evidence of nothing.

    Touching those heights would blind the majority population on every backfill: 4663 carries
    63,978 V3 and 24,909 Pons ticks against 103,058 V4, and on 8453 V3 outnumbers V4 outright.
    """
    conn, tape = _tape(tmp_path)
    _orphan_at(tape, 750)
    conn.execute("INSERT OR REPLACE INTO block_times (chain_id, block_no, block_ts, fetched_at,"
                 " block_hash) VALUES (?,?,?,?,?)", (CHAIN, 700, "2026-09-23T00:00:00+00:00",
                                                     "2026-09-23T00:00:00+00:00", "0xv3stale"))
    conn.execute("INSERT OR IGNORE INTO pool_ticks (chain_id, pool, kind, block_no, tx_sig,"
                 " log_index, amount0, amount1, observed_at, block_hash)"
                 " VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (CHAIN, V3_POOL, "v3_swap", 700, "0xv3tick", 0, 1, -1,
                  "2026-09-23T00:00:00+00:00", "0xv3stale"))
    conn.commit()
    w.backfill_window(tape, CHAIN, H["_chain_call"](), 2000.0, stored_pool_key=lambda *a: KEY)
    kept = conn.execute("SELECT block_hash FROM block_times WHERE chain_id=? AND block_no=700",
                        (CHAIN,)).fetchone()
    assert kept is not None and kept[0] == "0xv3stale", \
        "a V3 height is neither re-fetched nor invalidated by a pass that never asked for V3 logs"
    conn.close()


def test_above_the_repair_cap_the_identity_is_invalidated_rather_than_asserted(tmp_path):
    """A suspect set too large to verify means the log fetch itself is in doubt.

    The honest answer there is unknown -- NULL, which the reader turns into `hash_unverified` and a
    blind window -- never a smaller number and never an assertion that all of them are orphans.
    """
    conn, tape = _tape(tmp_path)
    _orphan_at(tape, 750)
    out = w.backfill_window(tape, CHAIN, H["_chain_call"](), 2000.0,
                            stored_pool_key=lambda *a: KEY, repair_cap=0)
    assert out and out.get("identity_invalidated") == 1, out
    row = conn.execute("SELECT block_hash FROM block_times WHERE chain_id=? AND block_no=750",
                       (CHAIN,)).fetchone()
    assert row is not None and row[0] is None, "an unverifiable identity is NULL, not the old fork's"
    blind = w.measure(conn, CHAIN, MINT, 2001.0, exclude_tx="0xback900")
    assert blind["reason"] != "ok", "a window holding an unverifiable tick cannot read ok"
    conn.close()
