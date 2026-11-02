"""PumpSwap tape shrink: write filter + holdback, lean layout, live-data cap, 2 h retention, and the offline converter."""
import importlib.util
import sqlite3
import time
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_pumpswap as P
from sniper.services import memecoin_desk_safety as S

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ps_shrink_worker', ROOT / 'scripts/memecoin_desk_universe_worker.py')
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)
mig_spec = importlib.util.spec_from_file_location('ps_migrate', ROOT / 'scripts/migrate_ps_tape.py')
M = importlib.util.module_from_spec(mig_spec)
mig_spec.loader.exec_module(M)
_h = importlib.util.spec_from_file_location('ps_helpers_shrink', Path(__file__).resolve().parent / 'test_memecoin_desk_pumpswap_worker.py')
H = importlib.util.module_from_spec(_h)
_h.loader.exec_module(H)
MINT = P.WSOL_MINT
POOL = S.pumpswap_pool_address(MINT)


def worker(path, monkeypatch, filt=True, **over):
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_CAPTURE', '1')
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_FILTER', '1' if filt else '0')
    return W.Worker({**W.knobs({}), **over}, base=path, sol_price_fn=lambda: None, alert_fn=lambda *a: None)


def swap_event(pool=POOL, kind='buy'):
    e = H.event(kind)
    e['pool'] = pool
    return e


def feed(w, monkeypatch, events, *, slot=100, sig_byte=1, index=3, at=None):
    monkeypatch.setattr(P, 'events', lambda _: events)
    u = H.pb.SubscribeUpdate.FromString(H.frame())
    u.transaction.slot = slot
    u.transaction.transaction.signature = bytes([sig_byte]) * 64
    u.transaction.transaction.index = index
    w.process(u.SerializeToString(), received_at=at if at is not None else time.time())


def rows(w):
    return w.files.conn('swaps_ps', time.time()).execute('SELECT pool, slot, map_status FROM swaps_ps ORDER BY slot, signature').fetchall()


def test_a_swap_on_a_pool_that_does_not_matter_is_counted_but_never_stored(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    now = time.time()
    feed(w, monkeypatch, [(1, swap_event('OtherPool'))], at=now)
    assert rows(w) == [] and w.stats['ps_swaps'] == 0 and w.stats['ps_swaps_seen'] == 1
    w.expire_hold(now + W.PS_HOLDBACK_S + 1)
    assert w.stats['ps_filtered'] == 1 and w.ps_hold_rows == 0
    w.flush_agg(now)
    assert w.files.conn('ps_pool_hours', now).execute('SELECT pool, swaps, buys FROM ps_pool_hours').fetchall() == [('OtherPool', 1, 1)]


def test_the_first_swaps_of_a_graduating_pool_are_kept_with_their_original_map_status(tmp_path, monkeypatch):
    # The pool's swap is processed BEFORE its curve-side graduation fact reaches us (two filters on one stream). It waits in the
    # holdback and is stored when the pool enters M, still 'unmapped' (causality: the fact was not known when it was processed);
    # the next swap after the fact maps through the curve-derived row.
    w = worker(tmp_path, monkeypatch)
    now = time.time()
    feed(w, monkeypatch, [(1, swap_event())], slot=100, sig_byte=1, at=now)                 # before the fact
    assert rows(w) == []
    w.note_graduation(MINT, 'migration', 99, 'G' * 64, 0, 1, None, now, time.time())      # the curve event of an EARLIER slot
    assert w.stats['ps_held_flushed'] == 1
    feed(w, monkeypatch, [(1, swap_event())], slot=101, sig_byte=2, at=now + 1)             # after the fact
    got = rows(w)
    assert [(slot, status) for _p, slot, status in got] == [(100, 'unmapped'), (101, 'mapped_curve_derived')]


def test_a_captured_canonical_create_pool_also_enters_m_and_flushes(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    now = time.time()
    feed(w, monkeypatch, [(1, swap_event())], slot=100, sig_byte=1, at=now)
    create = H.event('create_pool', MINT)
    create['pool'] = POOL
    feed(w, monkeypatch, [(2, create)], slot=101, sig_byte=2, at=now + 1)
    assert [r[1:] for r in rows(w)] == [(100, 'unmapped')]
    assert POOL in w.matter_set(now)


def test_a_held_row_older_than_the_holdback_is_dropped_not_flushed(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    now = time.time()
    feed(w, monkeypatch, [(1, swap_event())], slot=100, at=now - 120)
    w.expire_hold(now)
    w.note_graduation(MINT, 'migration', 99, 'G' * 64, 0, 1, None, now, time.time())
    assert rows(w) == [] and w.stats['ps_filtered'] == 1 and w.stats['ps_held_flushed'] == 0


def test_the_holdback_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(W, 'PS_HOLD_MAX_ROWS', 5)
    w = worker(tmp_path, monkeypatch)
    now = time.time()
    for i in range(9):
        feed(w, monkeypatch, [(1, swap_event('P%d' % (i % 3)))], slot=100 + i, sig_byte=i + 1, at=now)
    assert w.ps_hold_rows == 5 and w.stats['ps_hold_overflow'] == 4


def test_m_is_reloaded_from_the_tape_after_a_restart(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    now = time.time()
    create = H.event('create_pool', MINT)
    create['pool'] = 'CanonPool'
    feed(w, monkeypatch, [(1, create)], slot=5, at=now)
    w.note_graduation(MINT, 'migration', 6, 'G' * 64, 0, 1, None, now, time.time())
    w.commit_files()
    w2 = worker(tmp_path, monkeypatch)
    assert {'CanonPool', POOL} <= w2.matter_set(time.time()) and w2.stats['ps_matter_loaded'] >= 2
    feed(w2, monkeypatch, [(1, swap_event())], slot=200, sig_byte=9, at=time.time())
    assert [r[1] for r in rows(w2)] == [200]                                                  # stored at once: already in M


def test_the_aggregate_counts_every_swap_stored_or_not_and_accumulates_across_flushes(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    t = 1_700_000_000.0
    feed(w, monkeypatch, [(1, swap_event('A')), (2, swap_event('A', 'sell'))], slot=10, at=t)
    w.flush_agg(t)
    feed(w, monkeypatch, [(1, swap_event('A'))], slot=11, sig_byte=2, at=t + 10)
    w.flush_agg(t)
    c = w.files.conn('ps_pool_hours', t)
    assert c.execute('SELECT pool, hour, swaps, buys, first_slot, last_slot FROM ps_pool_hours').fetchall() == [('A', 1_700_000_000 // 3600 * 3600, 3, 2, 10, 11)]


def test_new_files_get_the_lean_layout_old_files_keep_theirs_and_prune_works_on_both(tmp_path, monkeypatch):
    new = worker(tmp_path / 'new', monkeypatch, filt=False)
    now = time.time()
    c = new.files.conn('swaps_ps', now)
    assert new.files.rowid_layout is True and 'WITHOUT ROWID' not in c.execute("SELECT sql FROM sqlite_master WHERE name='swaps_ps'").fetchone()[0]
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name='swaps_ps_sig_ord'").fetchone()
    feed(new, monkeypatch, [(1, swap_event())], slot=1, at=now - 7200 * 3)
    feed(new, monkeypatch, [(1, swap_event())], slot=1, at=now - 7200 * 3)               # same (signature, ordinal): deduped by the unique index
    assert c.execute('SELECT COUNT(*) FROM swaps_ps').fetchone()[0] == 1 and new.stats['ps_duplicate'] == 1
    new.prune_ps(now)
    assert c.execute('SELECT COUNT(*) FROM swaps_ps').fetchone()[0] == 0 and new.stats['ps_pruned'] == 1
    # an OLD-layout file (WITHOUT ROWID, primary key) is left as it is, no extra unique index, and prune uses the key
    old_dir = tmp_path / 'old' / 'pumpswap'
    old_dir.mkdir(parents=True)
    oc = sqlite3.connect(old_dir / 'tape.db')
    ddl = W.DayFiles.DDL['swaps_ps'].replace('program TEXT)', 'program TEXT, PRIMARY KEY(signature,ordinal)) WITHOUT ROWID')
    oc.execute(ddl)
    oc.commit()
    oc.close()
    old = worker(tmp_path / 'old', monkeypatch, filt=False)
    c2 = old.files.conn('swaps_ps', now)
    assert old.files.rowid_layout is False and c2.execute("SELECT 1 FROM sqlite_master WHERE name='swaps_ps_sig_ord'").fetchone() is None
    feed(old, monkeypatch, [(1, swap_event())], slot=1, at=now - 7200 * 3)
    old.prune_ps(now)
    assert c2.execute('SELECT COUNT(*) FROM swaps_ps').fetchone()[0] == 0 and old.stats['ps_pruned'] == 1


def test_the_cap_counts_live_data_not_the_file(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch, filt=False)
    now = time.time()
    c = w.files.conn('swaps_ps', now)
    c.execute("CREATE TABLE IF NOT EXISTS ballast(x BLOB)")
    c.executemany("INSERT INTO ballast VALUES(?)", [(b'x' * 4000,) for _ in range(2000)])
    c.commit()
    full = w.ps_live_bytes()
    c.execute("DELETE FROM ballast")
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    file_size = (tmp_path / 'pumpswap' / 'tape.db').stat().st_size
    live = w.ps_live_bytes()
    assert file_size > 7_000_000 and live < file_size - 6_000_000 and live < full - 6_000_000      # freed pages are not 'used'


def test_retention_defaults_to_two_hours():
    assert W.knobs({})['ps_retention_h'] == '2'


def _old_tape(path, n_pools=6, per_pool=40):
    """An OLD-layout tape: WITHOUT ROWID swaps_ps, M = 2 pools (one canonical, one in ps_grads), the rest venue noise."""
    path.mkdir(parents=True)
    c = sqlite3.connect(path / 'tape.db')
    for table in W.DayFiles.PS_STREAMS:
        if table == 'ps_pool_hours':
            continue                                                # an older worker had no aggregate table
        ddl = W.DayFiles.DDL[table]
        if table == 'swaps_ps':
            ddl = ddl.replace('program TEXT)', 'program TEXT, PRIMARY KEY(signature,ordinal)) WITHOUT ROWID')
        c.execute(ddl)
    base = 1_700_000_000.0
    for p in range(n_pools):
        for i in range(per_pool):
            c.execute("INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      ('pool%d' % p, None, None, 100 + i, 'sig%d-%d' % (p, i), 0, i, i % 2, 'a', 'c', '1', '2', '3', '4', None, None, '20', '5', '0',
                       None, None, 1, 1, base + i, base + i, base, 'unmapped', '{}', 'b64', 'solana', 'pumpswap', 'prog'))
    c.execute("INSERT INTO pools(pool, mint, quote_mint, pool_creator, canonical, slot, signature, ordinal, received_at, known_at) VALUES('pool0','m','q','c',1,1,'ps',0,?,?)",
              (base, base))
    c.execute("INSERT INTO pools(pool, mint, quote_mint, pool_creator, canonical, slot, signature, ordinal) VALUES('pool5','m','q','c',0,1,'ps2',0)")   # captured, NOT canonical
    c.execute("INSERT INTO ps_grads(pool, mint, kind, slot, signature, ordinal, received_at, known_at) VALUES('pool1','m','migration',1,'gs',0,?,?)", (base, base))
    c.execute("INSERT INTO ps_keep VALUES('pool2', ?, ?, 's', 1)", (base, base + 600))
    c.execute("INSERT INTO gaps_ps VALUES(?, 'decode_loss', 5)", (base,))
    c.commit()
    c.close()


def test_the_converter_keeps_m_rows_exactly_rebuilds_the_venue_totals_and_archives_the_old_file(tmp_path):
    base = tmp_path / 'pumpswap'
    _old_tape(base)
    report = M.convert(base, log=lambda *a: None)
    assert report['ok'] and report['m_pools'] == 3 and report['m_rows'] == 120 and report['old_rows'] == 240
    new = sqlite3.connect(base / 'tape.new.db')
    assert new.execute('SELECT COUNT(*), COUNT(DISTINCT pool) FROM swaps_ps').fetchone() == (120, 3)
    assert 'WITHOUT ROWID' not in new.execute("SELECT sql FROM sqlite_master WHERE name='swaps_ps'").fetchone()[0]
    assert new.execute("SELECT COALESCE(SUM(swaps),0) FROM ps_pool_hours").fetchone()[0] == 240                    # venue totals survive
    assert new.execute("SELECT cause FROM gaps_ps ORDER BY at").fetchall() == [('decode_loss',), ('tape_conversion',)]
    assert new.execute("SELECT m_rows FROM ps_conversion").fetchone() == (120,)
    new.close()
    target = M.swap(base, tmp_path / 'archive')
    assert target.name == 'tape.pre-shrink.db' and sqlite3.connect(target).execute('SELECT COUNT(*) FROM swaps_ps').fetchone()[0] == 240
    assert sqlite3.connect(base / 'tape.db').execute('SELECT COUNT(*) FROM swaps_ps').fetchone()[0] == 120 and not (base / 'tape.new.db').exists()


def test_the_converter_refuses_a_live_worker_a_leftover_file_and_a_failed_verification_swaps_nothing(tmp_path):
    pid = tmp_path / 'universe.pid'
    pid.write_text('999999999')
    assert M.worker_alive(pid) is False and M.worker_alive(tmp_path / 'missing.pid') is False
    base = tmp_path / 'pumpswap'
    _old_tape(base)
    (base / 'tape.new.db').write_bytes(b'')
    with pytest.raises(SystemExit):
        M.convert(base, log=lambda *a: None)                                       # a leftover tape.new.db is never overwritten
    (base / 'tape.new.db').unlink()
    before = (base / 'tape.db').stat().st_size
    with pytest.raises(SystemExit):
        M.convert(base, log=lambda *a: None, _tamper=lambda c: c.execute("DELETE FROM main.swaps_ps WHERE pool='pool0' AND slot=100"))
    assert (base / 'tape.new.db').exists() and (base / 'tape.db').stat().st_size == before       # kept for inspection, old tape untouched
    (base / 'tape.new.db').unlink()
    with pytest.raises(SystemExit):
        M.convert(base, log=lambda *a: None, _tamper=lambda c: c.execute("UPDATE main.swaps_ps SET payload_b64='x' WHERE pool='pool1' AND slot=101"))


def test_a_worker_that_already_ran_on_the_old_file_does_not_cost_the_history_and_a_short_aggregate_fails_verification(tmp_path):
    base = tmp_path / 'pumpswap'
    _old_tape(base)
    c = sqlite3.connect(base / 'tape.db')                       # the NEW worker already ran: an aggregate since its restart exists
    c.execute(W.DayFiles.DDL['ps_pool_hours'])
    c.execute("INSERT INTO ps_pool_hours VALUES('pool3', ?, 7, 3, 1, 2, 1.0, 2.0)", (1_700_000_000 // 3600 * 3600,))
    c.execute("INSERT INTO ps_pool_hours VALUES('only_new', 1700006400, 5, 1, 1, 2, 1.0, 2.0)")
    c.commit()
    c.close()
    report = M.convert(base, log=lambda *a: None)
    new = sqlite3.connect(base / 'tape.new.db')
    got = dict(new.execute("SELECT pool, swaps FROM ps_pool_hours WHERE hour = ?", (1_700_000_000 // 3600 * 3600,)).fetchall())
    assert got['pool3'] == 40 and got['pool0'] == 40                   # rebuilt count wins over the post-restart 7; nothing skipped
    assert new.execute("SELECT swaps FROM ps_pool_hours WHERE pool='only_new'").fetchone() == (5,)     # kept
    assert report['ok'] and report['pool_hours_short'] == 0 and report['old_rows'] == 240
    new.close()
    (base / 'tape.new.db').unlink()
    with pytest.raises(SystemExit):                                     # an aggregate that undercounts the stored rows must FAIL
        M.convert(base, log=lambda *a: None, _tamper=lambda c: c.execute("UPDATE main.ps_pool_hours SET swaps = 1 WHERE pool = 'pool0'"))


def test_swap_only_needs_a_verified_build_of_the_current_old_tape(tmp_path):
    base = tmp_path / 'pumpswap'
    _old_tape(base)
    assert M.prepared(base) == (False, 'no tape.new.db')
    M.convert(base, log=lambda *a: None)
    assert M.prepared(base) == (True, '')                             # the 'build, then explicit --swap' flow works
    c = sqlite3.connect(base / 'tape.db')                             # the old tape moved on after the build
    c.execute("INSERT INTO swaps_ps(pool,slot,signature,ordinal,received_at) VALUES('pool4',999,'late',9,?)", (1_700_000_000.0 + 5000,))
    c.commit()
    c.close()
    ok, why = M.prepared(base)
    assert ok is False and 'changed' in why


def test_the_census_reports_whole_venue_totals_from_the_aggregate():
    spec2 = importlib.util.spec_from_file_location('census_hourly_shrink', ROOT / 'scripts/research/pumpswap_census_hourly.py')
    __import__('sys').path.insert(0, str(ROOT / 'scripts/research'))
    mod = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(mod)
    c = sqlite3.connect(':memory:')
    assert mod.venue_totals(c, 0, 3600) == {'venue_swaps': None, 'venue_pools': None}       # tape from before the filter: unknown, not 0
    c.execute('CREATE TABLE ps_pool_hours(pool TEXT, hour INTEGER, swaps INTEGER)')
    c.executemany('INSERT INTO ps_pool_hours VALUES(?,?,?)', [('a', 3600, 10), ('b', 3600, 5), ('a', 7200, 99)])
    assert mod.venue_totals(c, 3600, 7200) == {'venue_swaps': 15, 'venue_pools': 2}


def test_the_swap_is_refused_while_a_writer_holds_the_tape_or_the_worker_runs(tmp_path):
    import os
    import subprocess
    import sys
    base = tmp_path / 'pumpswap'
    _old_tape(base)
    M.convert(base, log=lambda *a: None)
    holder = subprocess.Popen([sys.executable, '-c', 'import sqlite3,sys,time; c=sqlite3.connect(sys.argv[1]); c.execute("select 1 from swaps_ps limit 1"); print("up", flush=True); time.sleep(30)',
                               str(base / 'tape.db')], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'up'
        assert holder.pid in M.holders(base / 'tape.db')
        with pytest.raises(SystemExit):                                          # the watchdog restarted a worker mid-build: refuse
            M.swap(base, tmp_path / 'archive')
        assert (base / 'tape.db').exists() and not (tmp_path / 'archive' / 'tape.pre-shrink.db').exists()
        pid = tmp_path / 'universe.pid'
        pid.write_text(str(os.getpid()))                                         # this very process "is" the worker: cmdline has no worker name
        assert M.worker_alive(pid) is False                                      # only a real memecoin_desk_universe_worker counts
    finally:
        holder.kill()
        holder.wait()
    M.swap(base, tmp_path / 'archive')                                           # quiet again: the swap goes through
    assert (tmp_path / 'archive' / 'tape.pre-shrink.db').exists()


def test_a_live_universe_worker_blocks_the_swap_by_its_pid_file(tmp_path):
    import subprocess
    import sys
    fake = tmp_path / 'memecoin_desk_universe_worker.py'
    fake.write_text('import time\ntime.sleep(30)\n')
    proc = subprocess.Popen([sys.executable, str(fake)])
    try:
        pid = tmp_path / 'universe.pid'
        pid.write_text(str(proc.pid))
        # Popen returns when the child's exec has begun, NOT when /proc/<pid>/cmdline is filled in: measured 967 of 1000 spawns
        # read an EMPTY cmdline right after Popen (vfork resumes the parent early). Wait for it, never assume it.
        for _ in range(100):
            if 'memecoin_desk_universe_worker' in Path('/proc/%d/cmdline' % proc.pid).read_text():
                break
            time.sleep(0.05)
        assert M.worker_alive(pid) is True
        base = tmp_path / 'pumpswap'
        _old_tape(base)
        M.convert(base, log=lambda *a: None)
        with pytest.raises(SystemExit):
            M.swap(base, tmp_path / 'archive', pid)                              # the watchdog brought the worker back mid-build
        assert not (tmp_path / 'archive' / 'tape.pre-shrink.db').exists()
    finally:
        proc.kill()
        proc.wait()


# ---- --drop-payload-b64 (operator via Alpha 2026-10-28): complete copy, payload_b64 NULL, no freelist

def _b64_tape(base):
    _old_tape(base)                                                     # 240 swaps (b64 'b64'), WITHOUT ROWID swaps_ps
    c = sqlite3.connect(base / 'tape.db')
    c.execute("UPDATE pools SET payload_b64 = 'raw' WHERE pool = 'pool0'")
    c.execute("DELETE FROM swaps_ps WHERE pool = 'pool9'")              # a freelist in the old file
    c.commit()
    c.close()


def test_drop_b64_copies_every_row_and_column_except_swaps_payload_b64_and_archives_by_day(tmp_path):
    base = tmp_path / 'pumpswap'
    _b64_tape(base)
    old = sqlite3.connect(base / 'tape.db')
    want = {t: sorted(old.execute('SELECT * FROM %s' % t).fetchall(), key=repr) for (t,) in
            old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ddl = sorted(old.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall())
    old.close()
    report = M.convert_drop_b64(base, log=lambda *a: None)
    assert report['ok'] and report['rows']['swaps_ps'] == len(want['swaps_ps']) > 0
    new = sqlite3.connect(base / 'tape.new.db')
    assert new.execute('SELECT COUNT(*) FROM swaps_ps WHERE payload_b64 IS NOT NULL').fetchone()[0] == 0
    names = [r[1] for r in new.execute('PRAGMA table_info(swaps_ps)')]
    i = names.index('payload_b64')
    got = sorted(new.execute('SELECT * FROM swaps_ps').fetchall(), key=repr)
    assert [r[:i] + r[i + 1:] for r in got] == sorted([r[:i] + r[i + 1:] for r in want['swaps_ps']], key=repr)
    for t, rows in want.items():
        if t != 'swaps_ps':
            assert sorted(new.execute('SELECT * FROM %s' % t).fetchall(), key=repr) == rows, t     # pools keeps 'raw'
    assert sorted(new.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name != ?",
                              (M.B64_TABLE,)).fetchall()) == ddl                                     # same layout
    assert new.execute('PRAGMA freelist_count').fetchone()[0] == 0
    new.close()
    assert M.prepared(base, M.B64_TABLE) == (True, '')
    target = M.swap(base, tmp_path / 'archive', name='tape.pre-shrink-2026-10-28.db')
    assert target.name == 'tape.pre-shrink-2026-10-28.db'
    assert sqlite3.connect(target).execute("SELECT COUNT(*) FROM swaps_ps WHERE payload_b64 = 'b64'").fetchone()[0] == 240


@pytest.mark.parametrize('tamper', [
    "DELETE FROM main.swaps_ps WHERE pool='pool0' AND slot=100",                  # a missing row
    "UPDATE main.swaps_ps SET slot = slot + 1 WHERE pool='pool1' AND slot=101",  # a changed column
    "UPDATE main.swaps_ps SET payload_b64 = 'x' WHERE pool='pool1' AND slot=102",  # b64 left behind
    "UPDATE main.pools SET payload_b64 = NULL",                                   # pools must keep its raw event
    "DELETE FROM main.gaps_ps",                                                   # another table short
])
def test_drop_b64_verification_refuses_and_nothing_is_swapped(tmp_path, tamper):
    base = tmp_path / 'pumpswap'
    _b64_tape(base)
    before = (base / 'tape.db').stat().st_size
    with pytest.raises(SystemExit, match='verification FAILED'):
        M.convert_drop_b64(base, log=lambda *a: None, _tamper=lambda c: c.execute(tamper))
    assert M.prepared(base, M.B64_TABLE)[0] is False and (base / 'tape.db').stat().st_size == before


def test_drop_b64_main_refuses_a_live_worker_and_a_leftover_build(tmp_path, monkeypatch):
    base = tmp_path / 'pumpswap'
    _b64_tape(base)
    monkeypatch.setattr(M, 'worker_alive', lambda _: True)
    with pytest.raises(SystemExit, match='universe worker is running'):
        M.main(['--base', str(base), '--archive-dir', str(tmp_path / 'a'), '--drop-payload-b64', '--swap'])
    assert not (base / 'tape.new.db').exists()
    monkeypatch.setattr(M, 'worker_alive', lambda _: False)
    (base / 'tape.new.db').write_bytes(b'')
    with pytest.raises(SystemExit):
        M.main(['--base', str(base), '--archive-dir', str(tmp_path / 'a'), '--drop-payload-b64', '--allow-live'])


# Delta HOLD witnesses on c179cacff (2026-10-28), adopted verbatim in substance.

def test_drop_b64_keeps_hidden_rowid_identity_of_loss_batches(tmp_path):
    base = tmp_path / 'pumpswap'
    _b64_tape(base)
    c = sqlite3.connect(base / 'tape.db')
    c.execute("INSERT INTO gaps_ps VALUES(1700000010,'decode_loss',9)")
    gap = c.execute('SELECT MAX(rowid) FROM gaps_ps').fetchone()[0]
    c.execute('DELETE FROM gaps_ps WHERE rowid<?', (gap,))                         # a hole: rowid 2 must stay 2
    c.execute('INSERT INTO ps_loss_batches VALUES(?,?,?,?,?,?)', (gap, 1700000010., 'decode_loss', 0, 1, 1700000011.))
    c.commit()
    want = c.execute('SELECT rowid,* FROM gaps_ps').fetchall()
    c.close()
    assert M.convert_drop_b64(base, log=lambda *a: None)['ok']
    c = sqlite3.connect(base / 'tape.new.db')
    assert c.execute('SELECT rowid,* FROM gaps_ps').fetchall() == want
    assert c.execute('SELECT COUNT(*) FROM ps_loss_batches b LEFT JOIN gaps_ps g ON g.rowid=b.gap_rowid '
                     'WHERE g.rowid IS NULL').fetchone()[0] == 0
    c.close()


def test_drop_b64_build_is_fenced_against_any_change_to_the_old_tape(tmp_path):
    base = tmp_path / 'pumpswap'
    _b64_tape(base)
    M.convert_drop_b64(base, log=lambda *a: None)
    assert M.prepared(base, M.B64_TABLE) == (True, '')
    c = sqlite3.connect(base / 'tape.db')
    c.execute("UPDATE pools SET pool_creator='changed-after-build' WHERE pool='pool0'")   # same MAX(received_at)
    c.commit()
    c.close()
    ok, why = M.prepared(base, M.B64_TABLE)
    assert ok is False and 'fence' in why
    with pytest.raises(SystemExit, match='not swapping'):
        M.swap(base, tmp_path / 'archive', name='tape.pre-shrink-x.db', marker=M.B64_TABLE)
    assert (base / 'tape.db').exists() and not (tmp_path / 'archive' / 'tape.pre-shrink-x.db').exists()


def test_drop_b64_rowid_tables_exclude_without_rowid_and_integer_pk(tmp_path):
    c = sqlite3.connect(tmp_path / 'x.db')
    c.execute('CREATE TABLE a(x)')
    c.execute('CREATE TABLE b(id INTEGER PRIMARY KEY, x)')
    c.execute('CREATE TABLE w(k TEXT PRIMARY KEY, x) WITHOUT ROWID')
    c.execute('CREATE TABLE t(k TEXT PRIMARY KEY, x)')
    assert M._rowid_tables(c, 'main') == {'a', 't'}


def _wal_tape(base):
    base.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(base / 'tape.db')
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA wal_autocheckpoint=0')
    c.execute('CREATE TABLE t(id INTEGER PRIMARY KEY, x BLOB)')
    c.executemany('INSERT INTO t VALUES(?,?)', [(i, b'a' * 4000) for i in range(1000)])
    c.commit()
    return c


def test_fence_detects_wal_append_inside_reused_physical_tail(tmp_path):
    """Delta HOLD 798106b0f: after a PASSIVE checkpoint the next commit restarts the WAL cycle but keeps its physical size;
    a further small commit fits inside it with no restart: same db header, WAL header and sizes."""
    base = tmp_path / 'pumpswap'
    c = _wal_tape(base)
    try:
        c.execute('PRAGMA wal_checkpoint(PASSIVE)')
        c.execute('UPDATE t SET x=? WHERE id=0', (b'b' * 4000,))
        c.commit()
        before = M.source_fence(base)
        c.execute('UPDATE t SET x=? WHERE id=1', (b'c' * 4000,))
        c.commit()
        assert M.source_fence(base) != before
    finally:
        c.close()


def test_fence_detects_a_commit_folded_back_by_a_truncate_checkpoint(tmp_path):
    """WAL mode: commit + TRUNCATE checkpoint leaves the db header and size identical and the WAL empty again."""
    import time
    base = tmp_path / 'pumpswap'
    c = _wal_tape(base)
    try:
        c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before = M.source_fence(base)
        time.sleep(0.05)                                    # past the filesystem timestamp tick
        c.execute('UPDATE t SET x=? WHERE id=2', (b'd' * 4000,))
        c.commit()
        c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        assert M.source_fence(base) != before
    finally:
        c.close()
