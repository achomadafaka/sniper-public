"""One-time PumpSwap keep-window backlog drain (operator via Alpha 2026-10-28) and the tape reduction it goes with:
payload_b64 no longer written, PS_ARCHIVE_AFTER_S 7 d -> 2 d -> 6 h. The drain reuses Worker.archive_keep_ps, so its copy /
read-back / delete rule is the worker's; these pin the loop around it."""
import importlib.util
import sqlite3
import time
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_pumpswap as P

ROOT = Path(__file__).resolve().parents[1]
_r = importlib.util.spec_from_file_location('ps_backlog_ret', ROOT / 'tests/test_memecoin_desk_pumpswap_retention.py')
R = importlib.util.module_from_spec(_r)
_r.loader.exec_module(R)
_s = importlib.util.spec_from_file_location('ps_backlog_script', ROOT / 'scripts/archive_ps_keep_backlog.py')
S = importlib.util.module_from_spec(_s)
_s.loader.exec_module(S)
W = R.W


def tape(tmp_path, monkeypatch, windows):
    """A store-everything tape with one ps_keep window per (pool, keep_until age_s, rows)."""
    w = R.worker(tmp_path / 'desk', monkeypatch)
    now, o = time.time(), 0
    c = w.files.conn('swaps_ps', now)
    for pool, age, n in windows:
        until = now - age
        for _ in range(n):
            e = R.event()
            e['pool'] = pool
            monkeypatch.setattr(P, 'events', lambda _, e=e, o=o: [(o, e)])
            w.process(R.frame(), received_at=until - 300 + o)
            o += 1
        c.execute("INSERT OR REPLACE INTO ps_keep VALUES(?,?,?,?,?)", (pool, until - 660, until, 'sig', 1))
    c.commit()
    return w, now


def counts(tmp_path):
    c = sqlite3.connect(tmp_path / 'desk' / 'pumpswap' / 'tape.db')
    try:
        return {p: n for p, n in c.execute('SELECT pool, COUNT(*) FROM swaps_ps GROUP BY pool')}, \
               {r[0] for r in c.execute('SELECT pool FROM ps_keep')}
    finally:
        c.close()


def test_the_window_and_the_payload_change():
    assert W.PS_ARCHIVE_AFTER_S == 6 * 3600


def test_drain_moves_every_closed_window_equal_and_leaves_open_ones(tmp_path, monkeypatch):
    day = 86400
    w, now = tape(tmp_path, monkeypatch, [('A', 3 * day, 4), ('B', 5 * day, 3), ('C', 3600, 2)])
    before = {r[0]: r for r in sqlite3.connect(tmp_path / 'desk/pumpswap/tape.db').execute(
        'SELECT signature || ordinal, * FROM swaps_ps WHERE pool IN ("A","B")')}
    stats = S.drain(tmp_path / 'desk', now=now, batch=2, log=lambda *a: None)
    rows, keeps = counts(tmp_path)
    assert rows == {'C': 2} and keeps == {'C'}                                  # a 1 h old window stays live
    assert stats['ps_archived'] == 7 and stats['ps_archived_windows'] == 2 and stats['ps_archive_mismatch'] == 0
    archived = {}
    for f in (tmp_path / 'archive' / 'pumpswap').glob('keep-*.db'):
        archived.update({r[0]: r for r in sqlite3.connect(f).execute('SELECT signature || ordinal, * FROM swaps_ps')})
    assert archived == before                                                   # every column, every row


def test_drain_stops_when_the_worker_comes_back_and_deletes_nothing_uncopied(tmp_path, monkeypatch):
    w, now = tape(tmp_path, monkeypatch, [('A', 3 * 86400, 4)])
    calls, logs = [], []
    stats = S.drain(tmp_path / 'desk', now=now, batch=2, alive=lambda: calls.append(1) or len(calls) > 1, log=logs.append)
    rows, keeps = counts(tmp_path)
    assert stats['ps_archived'] == 2 and rows == {'A': 2} and keeps == {'A'}    # one batch, then the stop
    assert 'running again' in logs[-1]
    S.drain(tmp_path / 'desk', now=now, batch=2, log=lambda *a: None)          # a re-run finishes it
    assert counts(tmp_path) == ({}, set())


def test_a_mismatch_keeps_the_window_and_the_drain_ends(tmp_path, monkeypatch):
    w, now = tape(tmp_path, monkeypatch, [('A', 3 * 86400, 2), ('B', 4 * 86400, 2)])
    c = sqlite3.connect(tmp_path / 'desk/pumpswap/tape.db')
    sig, ordi, until = c.execute("SELECT s.signature, s.ordinal, k.keep_until FROM swaps_ps s JOIN ps_keep k USING(pool) "
                                 "WHERE pool = 'B' ORDER BY s.signature, s.ordinal").fetchone()
    a = w._ps_archive(W.utc_day(until))                                         # an earlier, different copy
    a.execute("INSERT INTO swaps_ps(signature, ordinal, pool) VALUES(?,?,'other')", (sig, ordi))
    a.commit()
    a.close()
    w.ps_archive_conn = None
    __import__("os").close(w.ps_archive_lock)                                 # the worker stops: its seal lock goes
    w.ps_archive_lock = None
    stats = S.drain(tmp_path / 'desk', now=now, batch=10, log=lambda *a: None)
    rows, keeps = counts(tmp_path)
    assert stats['ps_archive_mismatch'] == 1 and rows == {'B': 2} and keeps == {'B'}
    assert stats['ps_archived_windows'] == 1                                    # A still moved


def test_main_is_a_dry_run_by_default_and_refuses_a_running_worker(tmp_path, monkeypatch, capsys):
    tape(tmp_path, monkeypatch, [('A', 3 * 86400, 3)])
    assert S.main(['--base', str(tmp_path / 'desk')]) == 0
    assert 'eligible: 1 keep windows, 3 swaps_ps rows' in capsys.readouterr().out
    assert counts(tmp_path) == ({'A': 3}, {'A'})
    real = S._module

    def patched(name, rel):
        mod = real(name, rel)
        if rel.endswith('migrate_ps_tape.py'):
            mod.worker_alive = lambda _: True
        return mod
    monkeypatch.setattr(S, '_module', patched)
    with pytest.raises(SystemExit, match='universe worker is running'):
        S.main(['--base', str(tmp_path / 'desk'), '--confirm'])
    assert counts(tmp_path) == ({'A': 3}, {'A'})


def test_main_takes_a_relative_base(tmp_path, monkeypatch, capsys):
    """Delta HOLD 7e1ead403: --base data/desk (relative) crashed Path.as_uri with ValueError."""
    tape(tmp_path, monkeypatch, [('A', 3 * 86400, 2)])
    monkeypatch.chdir(tmp_path)
    assert S.main(['--base', 'desk']) == 0
    assert 'eligible: 1 keep windows, 2 swaps_ps rows' in capsys.readouterr().out


def test_after_s_overrides_the_worker_constant_and_must_be_positive(tmp_path, monkeypatch, capsys):
    day = 86400
    tape(tmp_path, monkeypatch, [('A', 3 * day, 2), ('B', 12 * 3600, 3), ('C', 3600, 1)])
    assert S.main(['--base', str(tmp_path / 'desk')]) == 0                      # default 6 h: A and B
    assert 'eligible: 2 keep windows, 5 swaps_ps rows' in capsys.readouterr().out
    assert S.main(['--base', str(tmp_path / 'desk'), '--after-s', str(2 * day)]) == 0
    assert 'eligible: 1 keep windows, 2 swaps_ps rows' in capsys.readouterr().out
    stats = S.drain(tmp_path / 'desk', now=time.time(), batch=10, after_s=2 * day, log=lambda *a: None)
    assert stats['ps_archived_windows'] == 1 and counts(tmp_path)[1] == {'B', 'C'}
    for bad in ('0', '-5', 'inf', 'nan'):
        with pytest.raises(SystemExit):
            S.main(['--base', str(tmp_path / 'desk'), '--after-s', bad])


def test_drain_releases_the_day_seal_lock(tmp_path, monkeypatch):
    """Delta 2012752e2: the offline drain shares the worker's per-day seal lock and releases it when done."""
    import fcntl
    import os
    w, now = tape(tmp_path, monkeypatch, [('A', 3 * 86400, 2)])
    S.drain(tmp_path / 'desk', now=now, batch=10, log=lambda *a: None)
    locks = list((tmp_path / 'archive' / 'pumpswap').glob('keep-*.db.lock'))
    assert locks
    for lock in locks:
        fd = os.open(str(lock), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)                    # free again
        finally:
            os.close(fd)
