"""Rolling PumpSwap tape retention and the slim payload (operator 2026-10-23: autoclear the capture)."""
import importlib.util
import json
import sqlite3
import time
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_pumpswap as P

spec = importlib.util.spec_from_file_location('ps_ret_worker', Path(__file__).resolve().parents[1] / 'scripts/memecoin_desk_universe_worker.py')
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)
_h = importlib.util.spec_from_file_location('ps_worker_helpers', Path(__file__).resolve().parent / 'test_memecoin_desk_pumpswap_worker.py')
H = importlib.util.module_from_spec(_h)
_h.loader.exec_module(H)
event, frame = H.event, H.frame


def worker(path, monkeypatch, **over):
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_CAPTURE', '1')
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_FILTER', '0')            # retention semantics are tested on a store-everything tape
    return W.Worker({**W.knobs({}), **over}, base=path, sol_price_fn=lambda: None, alert_fn=lambda *a: None)


def test_retention_deletes_only_rows_older_than_the_window_in_bounded_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(W, 'PS_PRUNE_BATCH', 3)
    w = worker(tmp_path, monkeypatch, ps_retention_h='1')
    now = time.time()
    monkeypatch.setattr(P, 'events', lambda _: [(i, event()) for i in range(5)])
    for i in range(5):                                            # 5 rows, two hours old (distinct stamps: a tie is deleted whole)
        monkeypatch.setattr(P, 'events', lambda _, i=i: [(i, event())])
        w.process(frame(), received_at=now - 7200 + i)
    monkeypatch.setattr(P, 'events', lambda _: [(i, event()) for i in range(10, 12)])
    w.process(frame(), received_at=now - 60)                      # 2 rows inside the window
    c = w.files.conn('swaps_ps', now)
    w.prune_ps(now)
    assert c.execute('SELECT COUNT(*) FROM swaps_ps').fetchone()[0] == 4 and w.stats['ps_pruned'] == 3
    w.prune_ps(now)
    w.prune_ps(now)
    assert sorted(r[0] for r in c.execute('SELECT ordinal FROM swaps_ps')) == [10, 11]
    assert w.stats['ps_pruned'] == 5 and w.stats['ps_prune_errors'] == 0


def test_commit_prunes_only_when_the_tape_is_caught_up(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch, ps_retention_h='1')
    monkeypatch.setattr(P, 'events', lambda _: [(1, event())])
    w.process(frame(), received_at=time.time() - 7200)
    w.ps_ring.append((b'x', 0.0))                                 # backlog: capture first, no delete
    w.commit_files()
    assert w.stats['ps_pruned'] == 0
    w.ps_ring.clear()
    w.commit_files()
    assert w.stats['ps_pruned'] == 1


def test_stored_payload_keeps_only_the_pricer_keys_and_b64_is_not_written(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    e = event()
    e['payload'] = {'ix_name': 'buy', 'virtual_quote_reserves': 5, 'virtual_quote_atomic': '5',
                    'virtual_quote_source': 'pumpswap_grpc:buy_event_virtual_quote', 'user': 'u' * 44, 'pool': 'p'}
    monkeypatch.setattr(P, 'events', lambda _: [(1, e)])
    w.process(frame(), received_at=time.time())
    js, b64 = w.files.conn('swaps_ps', time.time()).execute('SELECT payload_json, payload_b64 FROM swaps_ps').fetchone()
    assert json.loads(js) == {k: e['payload'][k] for k in W.PS_PAYLOAD_KEYS} and b64 is None


@pytest.mark.parametrize('bad', ['0', '-1', 'nan', 'x'])
def test_retention_hours_must_be_finite_and_positive(tmp_path, monkeypatch, bad):
    with pytest.raises(ValueError, match='retention'):
        worker(tmp_path, monkeypatch, ps_retention_h=bad)


def test_graduation_keep_window_survives_retention_and_does_not_block_older_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(W, 'PS_PRUNE_BATCH', 2)
    w = worker(tmp_path, monkeypatch, ps_retention_h='1')
    now = time.time()
    grad = now - 7200
    monkeypatch.setattr(P, 'events', lambda _: [(0, event('create_pool'))])
    w.process(frame(), received_at=grad)                                  # canonical: keep [grad-60, grad+600]
    monkeypatch.setattr(P, 'events', lambda _: [(i, event()) for i in range(1, 4)])
    w.process(frame(), received_at=grad + 30)                             # inside the keep window
    monkeypatch.setattr(P, 'events', lambda _: [(i, event()) for i in range(4, 7)])
    w.process(frame(), received_at=grad + 900)                            # old, outside it
    c = w.files.conn('swaps_ps', now)
    for _ in range(3):
        w.prune_ps(now)
    assert sorted(r[0] for r in c.execute('SELECT ordinal FROM swaps_ps')) == [1, 2, 3]
    assert w.stats['ps_pruned'] == 3


def test_open_backfills_keep_rows_for_graduations_already_in_the_tape(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    monkeypatch.setattr(P, 'events', lambda _: [(0, event('create_pool'))])
    w.process(frame(), received_at=1000.0)
    c = w.files.conn('swaps_ps', time.time())
    c.execute('DELETE FROM ps_keep')
    c.commit()
    w.files._open.clear()
    c2 = W.DayFiles(tmp_path).conn('swaps_ps', time.time())
    assert c2.execute('SELECT pool, keep_from, keep_until FROM ps_keep').fetchall() == [('pool', 940.0, 1600.0)]


_hs = importlib.util.spec_from_file_location('ps_hourly', Path(__file__).resolve().parents[1] / 'scripts/research/pumpswap_census_hourly.py')
HR = importlib.util.module_from_spec(_hs)
_hs.loader.exec_module(HR)


def _tape(base):
    c = sqlite3.connect(str(base / 'tape.db'))
    for ddl in W.DayFiles.DDL.values():
        c.execute(ddl)
    hour = 1_800_000_000 // 3600 * 3600
    rows = [('A', 'mA', W.P.WSOL_MINT if hasattr(W, 'P') else 'wsol', 1, 's%d' % i, i, 1, 1, 'u', None, '1', '1', None, None,
             None, None, '20', '5', '95', None, None, None, hour + 10, hour + 10.5, hour + 11, None,
             'mapped' if i < 3 else 'unmapped', '{}', 'raw', 'solana', 'pumpswap', 'p') for i in range(4)]
    rows[3] = ('B',) + rows[3][1:]
    c.executemany('INSERT INTO swaps_ps VALUES(' + ','.join('?' * 32) + ')', rows)
    c.execute("INSERT INTO pools VALUES('A','mA','wsol','x',1,1,'c',0,0,0,?,?,0,0,'{}','raw','solana','pumpswap','p')",
              (hour + 5, hour + 5.0))
    c.executemany('INSERT INTO gaps_ps VALUES(?,?,?)', [(hour + 1, 'decode_loss', 1), (hour + 2, 'worker_start', 1)])
    c.commit()
    c.close()
    return hour


def test_hourly_row_has_the_floor_fields_once_and_records_a_pool_mints_failure(tmp_path):
    hour = _tape(tmp_path)

    def down(pools):
        raise TimeoutError('rpc down')
    row = HR.roll(tmp_path, hour, down, now=lambda: 1.0)
    assert (row['total_swaps'], row['distinct_pools'], row['graduations_seen'], row['graduations_mapped']) == (4, 2, 1, 1)
    assert (row['swaps_per_pool_p50'], row['swaps_per_pool_max'], row['decode_failures']) == (1, 3, 1)
    assert row['gap_events'] == [{'start': hour + 2, 'cause': 'worker_start', 'last_slot': 1, 'end': hour + 10.5}]
    assert row['pool_mints_error'].startswith('TimeoutError')
    assert HR.roll(tmp_path, hour, down) is None                              # never written twice
    assert len((tmp_path / 'census_hourly.jsonl').read_text().splitlines()) == 1


def test_retention_cursor_walks_kept_rows_once(tmp_path, monkeypatch):
    """10-26: kept rows pile up at the old end of received_at; without a cursor every batch re-walked them (158 s)."""
    monkeypatch.setattr(W, 'PS_PRUNE_BATCH', 2)
    w = worker(tmp_path, monkeypatch, ps_retention_h='1')
    now = time.time()
    for k in range(4):
        monkeypatch.setattr(P, 'events', lambda _, k=k: [(k, event())])
        w.process(frame(), received_at=now - 9000 + k)
    c = w.files.conn('swaps_ps', now)
    c.execute("INSERT INTO ps_keep VALUES(?,?,?,?,?)", (c.execute('SELECT pool FROM swaps_ps').fetchone()[0], 0, now - 8999, 's', 1))
    c.commit()                                                    # rows 0 and 1 are kept, 2 and 3 are not
    seen = []
    c.set_trace_callback(seen.append)
    w.prune_ps(now)
    assert w.ps_prune_from == now - 8999 and w.stats['ps_pruned'] == 0
    w.prune_ps(now)
    assert sorted(r[0] for r in c.execute('SELECT ordinal FROM swaps_ps')) == [0, 1] and w.stats['ps_pruned'] == 2
    w.prune_ps(now)                                               # nothing left past the cursor: no delete issued
    c.set_trace_callback(None)
    assert sum(q.lstrip().startswith('DELETE') for q in seen) == 2

def test_capture_connection_caps_the_wal_it_leaves_behind(tmp_path, monkeypatch):
    """10-26: an 11.4 GB WAL filled the 40 GB cap; without journal_size_limit a checkpoint never shrinks the file."""
    w = worker(tmp_path, monkeypatch)
    c = w.files.conn('swaps_ps', time.time())
    assert c.execute('PRAGMA journal_size_limit').fetchone()[0] == W.PS_WAL_LIMIT_BYTES
    c.execute('CREATE TABLE IF NOT EXISTS fill(b BLOB)')
    for _ in range(40):
        c.execute('INSERT INTO fill VALUES(zeroblob(1000000))')
        c.commit()
    c.execute('DELETE FROM fill')
    c.commit()
    c.execute('PRAGMA wal_checkpoint(PASSIVE)')
    c.execute('INSERT INTO fill VALUES(1)')                      # the write after a full checkpoint resets and truncates the WAL
    c.commit()
    assert (tmp_path / 'pumpswap' / 'tape.db-wal').stat().st_size <= W.PS_WAL_LIMIT_BYTES


def test_open_folds_a_leftover_wal_back_before_the_cap_check(tmp_path, monkeypatch):
    """10-26 12:41Z: a restart re-stopped capture on the 11.4 GB WAL left by the previous run."""
    (tmp_path / 'pumpswap').mkdir()
    db = tmp_path / 'pumpswap' / 'tape.db'
    left = sqlite3.connect(db)
    left.execute('PRAGMA journal_mode=WAL')
    left.execute('PRAGMA wal_autocheckpoint=0')
    left.execute('CREATE TABLE fill(b BLOB)')
    for _ in range(20):
        left.execute('INSERT INTO fill VALUES(zeroblob(1000000))')
        left.commit()
    wal = tmp_path / 'pumpswap' / 'tape.db-wal'
    assert wal.stat().st_size > 15_000_000
    w = worker(tmp_path, monkeypatch)
    w.files.conn('swaps_ps', time.time())
    busy, log_frames, ckpt = w.files.ps_wal_checkpoint
    assert busy == 0 and log_frames > 0 and ckpt == log_frames                       # PASSIVE (AGENTS routine-maintenance rule): every frame is in the database; the file is reusable, not truncated
    import inspect
    src = inspect.getsource(W.DayFiles.conn)
    assert "wal_checkpoint(PASSIVE)" in src and not any(m in src for m in ("wal_checkpoint(TRUNCATE)", "wal_checkpoint(RESTART)", "wal_checkpoint(FULL)"))
    left.close()


def test_a_leftover_wal_does_not_re_stop_capture_at_the_first_cap_check(tmp_path, monkeypatch):
    """The 10-26 restart re-stop, without a TRUNCATE at open: the first check flushes the WAL (PASSIVE) and a flushed WAL does not count toward the cap."""
    (tmp_path / 'pumpswap').mkdir()
    left = sqlite3.connect(tmp_path / 'pumpswap' / 'tape.db')
    left.execute('PRAGMA journal_mode=WAL')
    left.execute('PRAGMA wal_autocheckpoint=0')
    left.execute('CREATE TABLE fill(b BLOB)')
    for _ in range(20):
        left.execute('INSERT INTO fill VALUES(zeroblob(1000000))')
        left.commit()
    w = worker(tmp_path, monkeypatch)
    w.cfg.update(ps_hard_gb="0.03", ps_resume_hyst_gb="0.005", min_free_gb="1")         # 20 MB of data fits a 30 MB cap; data plus the 20 MB leftover WAL does not
    monkeypatch.setattr(W.shutil, "disk_usage", lambda _: type("D", (), {"free": 500e9})())
    w.files.conn('swaps_ps', time.time())
    left.close()
    w.check_disk()
    assert w.ps_capture and w.ps_stopped is None


def _expired_window(tmp_path, monkeypatch, n=5):
    monkeypatch.setattr(W, 'PS_PRUNE_BATCH', 3)
    w = worker(tmp_path / 'desk', monkeypatch)
    now = time.time()
    old = now - W.PS_ARCHIVE_AFTER_S - 86400
    for i in range(n):
        monkeypatch.setattr(P, 'events', lambda _, i=i: [(i, event())])
        w.process(frame(), received_at=old + i)
    c = w.files.conn('swaps_ps', now)
    pool = c.execute('SELECT pool FROM swaps_ps').fetchone()[0]
    c.execute("INSERT OR REPLACE INTO ps_keep VALUES(?,?,?,?,?)", (pool, old - 60, old + 600, 'sig', 1))
    c.commit()
    return w, c, now, old


def test_expired_keep_window_moves_to_the_daily_archive_then_leaves_tape(tmp_path, monkeypatch):
    w, c, now, old = _expired_window(tmp_path, monkeypatch)
    before = sorted(c.execute('SELECT * FROM swaps_ps').fetchall())
    for _ in range(4):
        w.archive_keep_ps(now)
    path = w.ps_archive_path(W.utc_day(old + 600))
    a = sqlite3.connect(path)
    assert sorted(a.execute('SELECT * FROM swaps_ps').fetchall()) == before          # every column, every row
    assert c.execute('SELECT COUNT(*) FROM swaps_ps').fetchone()[0] == 0
    assert c.execute('SELECT COUNT(*) FROM ps_keep').fetchone()[0] == 0 and a.execute('SELECT COUNT(*) FROM ps_keep').fetchone()[0] == 1
    assert w.stats['ps_archived'] == 5 and w.stats['ps_archived_windows'] == 1 and w.stats['ps_archive_errors'] == 0


def test_no_tape_row_is_deleted_without_an_equal_archive_copy(tmp_path, monkeypatch):
    """A row whose archive copy differs (an earlier copy kept by INSERT OR IGNORE) stays in tape.db."""
    w, c, now, old = _expired_window(tmp_path, monkeypatch, n=2)
    a = w._ps_archive(W.utc_day(old + 600))
    sig, ordi = c.execute('SELECT signature, ordinal FROM swaps_ps ORDER BY signature, ordinal').fetchone()
    a.execute("INSERT INTO swaps_ps(signature, ordinal, pool) VALUES(?,?,'other')", (sig, ordi))
    a.commit()
    w.archive_keep_ps(now)
    assert c.execute('SELECT COUNT(*) FROM swaps_ps').fetchone()[0] == 2 and w.stats['ps_archive_mismatch'] == 1


def test_a_live_keep_window_is_not_archived(tmp_path, monkeypatch):
    w, c, now, old = _expired_window(tmp_path, monkeypatch, n=1)
    c.execute('UPDATE ps_keep SET keep_until = ?', (now - W.PS_ARCHIVE_AFTER_S + 60,))
    c.commit()
    w.archive_keep_ps(now)
    assert c.execute('SELECT COUNT(*) FROM swaps_ps').fetchone()[0] == 1 and w.stats['ps_archived'] == 0


def test_a_passed_archive_day_is_closed_read_only(tmp_path, monkeypatch):
    w, c, now, old = _expired_window(tmp_path, monkeypatch, n=1)
    w.archive_keep_ps(now)
    first = w.ps_archive_path(W.utc_day(old + 600))
    w._ps_archive(W.utc_day(old + 600 + 86400))
    assert (first.stat().st_mode & 0o777) == 0o444


# Delta review witnesses on ce420e1de (2026-10-26), adopted verbatim.
def test_delta_archive_lock_does_not_stall_capture_over_100ms(tmp_path, monkeypatch):
    import threading
    w, c, now, old = _expired_window(tmp_path, monkeypatch, n=1)
    w._ps_archive(W.utc_day(old + 600))
    locked = sqlite3.connect(w.ps_archive_path(W.utc_day(old + 600)), timeout=0, check_same_thread=False)
    locked.execute('BEGIN EXCLUSIVE')
    release = threading.Timer(0.3, locked.rollback)
    release.start()
    started = time.monotonic()
    try:
        w.archive_keep_ps(now)
        elapsed = time.monotonic() - started
    finally:
        release.join()
        locked.close()
    assert elapsed < 0.1, f'capture processor stalled for {elapsed * 1000:.1f} ms'


def test_delta_restart_does_not_poison_archive_queue_with_completed_day(tmp_path, monkeypatch):
    w, c, now, old = _expired_window(tmp_path, monkeypatch, n=1)
    monkeypatch.setattr(P, 'events', lambda _: [(10, event('create_pool'))])
    w.process(frame(), received_at=old)
    c.commit()
    for _ in range(3):
        w.archive_keep_ps(now)
    assert c.execute('SELECT COUNT(*) FROM ps_keep').fetchone()[0] == 0
    w._ps_archive(W.utc_day(old + 600 + 86400))
    c.close()
    restarted = worker(tmp_path / 'desk', monkeypatch)
    fresh = restarted.files.conn('swaps_ps', now)
    restarted.archive_keep_ps(now)
    assert restarted.stats['ps_archive_errors'] == 0, 'restart recreates archived keep row and writes a closed 0444 archive'
    assert fresh.execute('SELECT COUNT(*) FROM ps_keep').fetchone()[0] == 0


def test_delta_crash_after_archive_commit_keeps_identical_source_until_retry(tmp_path, monkeypatch):
    w, c, now, old = _expired_window(tmp_path, monkeypatch, n=2)
    a = w._ps_archive(W.utc_day(old + 600))
    before = c.execute('SELECT * FROM swaps_ps ORDER BY signature, ordinal').fetchall()
    a.executemany('INSERT INTO swaps_ps VALUES(' + ','.join('?' * len(before[0])) + ')', before)
    a.commit()
    assert c.execute('SELECT COUNT(*) FROM swaps_ps').fetchone()[0] == 2
    w.archive_keep_ps(now)
    assert c.execute('SELECT COUNT(*) FROM swaps_ps').fetchone()[0] == 0
    assert a.execute('SELECT * FROM swaps_ps ORDER BY signature, ordinal').fetchall() == before


def test_a_mismatched_window_does_not_pin_the_windows_behind_it(tmp_path, monkeypatch):
    w, c, now, old = _expired_window(tmp_path, monkeypatch, n=2)
    a = w._ps_archive(W.utc_day(old + 600))
    sig, ordi = c.execute('SELECT signature, ordinal FROM swaps_ps ORDER BY signature, ordinal').fetchone()
    a.execute("INSERT INTO swaps_ps(signature, ordinal, pool) VALUES(?,?,'other')", (sig, ordi))
    a.commit()
    c.execute("INSERT INTO ps_keep VALUES('later', ?, ?, 's2', 2)", (old, old + 700))
    c.commit()
    w.archive_keep_ps(now)
    w.archive_keep_ps(now)
    assert w.stats['ps_archive_mismatch'] == 1 and w.stats['ps_archived_windows'] == 1
    assert 'later' not in [r[0] for r in c.execute('SELECT pool FROM ps_keep')]


@pytest.mark.xfail(reason="Alpha 2026-10-26: 100 ms is a measured target (ps_archive_ms_max), not a hard bound; archive I/O stays on the capture loop", strict=True)
def test_delta_slow_archive_commit_does_not_stall_capture_over_100ms(tmp_path, monkeypatch):
    w, c, now, old = _expired_window(tmp_path, monkeypatch, n=1)
    archive = w._ps_archive(W.utc_day(old + 600))

    class SlowCommit:
        def __getattr__(self, name):
            return getattr(archive, name)

        def commit(self):
            time.sleep(0.15)
            archive.commit()

    monkeypatch.setattr(w, '_ps_archive', lambda day: SlowCommit())
    started = time.monotonic()
    w.archive_keep_ps(now)
    elapsed = time.monotonic() - started
    assert elapsed < 0.1, f'capture processor stalled for {elapsed * 1000:.1f} ms on archive commit'
