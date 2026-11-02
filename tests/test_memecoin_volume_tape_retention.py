"""The 4663 tape keeps 65 minutes (operator yes 2026-10-01) and the funded volume gate reads exactly
the same values as with the 60-minute tape.

The gate's tape reads are `measure` (a WINDOW_S = 300 s window, a header check over the same window,
and MAX(block_ts) for freshness) behind `read_for_gate`. Retention only removes rows older than the
horizon, so a longer horizon cannot change a value the gate reads. These tests prove it on the
production reader over a tape that spans 70 minutes, pruned by the production `prune`.
"""
import shutil
import sqlite3

import pytest

from sniper.services import memecoin_volume_writer as w
from sniper.services.memecoin_evm_v4_events import V4_SWAP
from sniper.execution.evm_v4_launchpad import pool_id_for_key

CHAIN = '4663'
MINT = '0x' + 'ab' * 20
KEY = {'currency0': w.NATIVE, 'currency1': MINT, 'fee': 3000, 'tick_spacing': 60, 'hooks': w.NATIVE}
POOL = pool_id_for_key(KEY)
T0, STEP, BLOCKS = 1_000, 60, 71          # one header a minute for 70 minutes
NOW = T0 + STEP * (BLOCKS - 1)


def _swap(block, tx):
    words = [10 ** 18, (1 << 256) - 10 ** 18, 1, 1, 0, 3000]
    return {'address': w.MANAGERS[CHAIN], 'topics': [V4_SWAP, POOL, '0x' + '00' * 32],
            'data': '0x' + ''.join(f'{v:064x}' for v in words), 'blockNumber': hex(block),
            'logIndex': hex(0), 'transactionHash': tx, 'blockHash': f'0x{block:x}'}


def _long_tape(path):
    """A swap in every block, a header a minute, priced at NOW: the gate's window holds blocks
    66-71 and the tape holds swaps up to 70 minutes old."""
    conn = w.connect(path)
    tape = w.VolumeTape(conn, CHAIN)
    tape.register_pool(POOL, KEY, T0)
    for n in range(1, BLOCKS + 1):
        ts = T0 + STEP * (n - 1)
        tape.head({'number': hex(n), 'timestamp': hex(ts), 'hash': f'0x{n:x}'}, ts)
        tape.log(_swap(n, f'0x{n:064x}'), ts)
    tape.set_quote_price(2400, NOW)
    return conn


def _oldest_age_s(conn):
    oldest = conn.execute("SELECT MIN(block_no) FROM pool_ticks WHERE chain_id=?", (CHAIN,)).fetchone()[0]
    return NOW - (T0 + STEP * (oldest - 1))


def test_the_gate_reads_the_same_values_on_a_60_and_a_65_minute_tape(tmp_path, monkeypatch):
    base = tmp_path / 'tape.db'
    _long_tape(base).close()
    shutil.copy(base, tmp_path / 'old.db')
    shutil.copy(base, tmp_path / 'new.db')
    unpruned = w.measure(w.connect(base), CHAIN, MINT, NOW)
    assert unpruned['usd'] and unpruned['swaps'] == 5       # blocks 67-71 inside the 300 s window

    old, new = w.connect(tmp_path / 'old.db'), w.connect(tmp_path / 'new.db')
    monkeypatch.setattr(w, 'KEEP_S', {})                    # today's horizon, 3600 s everywhere
    w.prune(old, CHAIN, NOW)
    monkeypatch.undo()
    w.prune(new, CHAIN, NOW)
    assert _oldest_age_s(old) <= 3600 < _oldest_age_s(new) <= 3900

    assert w.measure(old, CHAIN, MINT, NOW) == w.measure(new, CHAIN, MINT, NOW) == unpruned
    from sniper.util import clock
    monkeypatch.setattr(clock, 'real_utc_now', lambda: NOW)
    reads = []
    for name in ('old.db', 'new.db'):
        monkeypatch.setattr(w, 'db_path', lambda name=name: str(tmp_path / name))
        reads.append(w.read_for_gate(CHAIN, MINT))
    assert reads[0] == reads[1] and reads[0]['usd'] == unpruned['usd']


def test_only_4663_keeps_the_longer_horizon(tmp_path):
    conn = _long_tape(tmp_path / 'tape.db')
    for chain in ('8453', '56'):
        conn.executemany(
            "INSERT INTO pool_ticks(chain_id, pool, kind, block_no, tx_sig, log_index, observed_at) "
            "VALUES (?,?,?,?,?,0,?)",
            [(chain, POOL, 'v4_swap', n, f'0x{chain}{n}', w.iso(NOW - age)) for n, age in ((1, 3700), (2, 3500))])
        w.prune(conn, chain, NOW)
        kept = [r[0] for r in conn.execute("SELECT block_no FROM pool_ticks WHERE chain_id=?", (chain,))]
        assert kept == [2]
    w.prune(conn, CHAIN, NOW)
    assert 3600 < _oldest_age_s(conn) <= 3900
    oldest_header = conn.execute("SELECT MIN(block_no) FROM block_times WHERE chain_id=?", (CHAIN,)).fetchone()[0]
    assert 3600 < NOW - (T0 + STEP * (oldest_header - 1)) <= 3900     # headers on the same horizon


def test_the_stream_loop_prunes_through_prune():
    import inspect
    src = inspect.getsource(w)
    assert src.count("DELETE FROM pool_ticks WHERE chain_id=? AND observed_at<?") == 1
    assert "prune(conn,chain,now)" in src
