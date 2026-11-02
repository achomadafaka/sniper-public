"""Prospective PumpSwap retention; temporary tapes only, no network or live settings."""
import json

import pytest

from test_memecoin_desk_pumpswap_worker import P, W, event, frame


@pytest.fixture
def make_worker(tmp_path, monkeypatch):
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_CAPTURE', '1')
    monkeypatch.setenv('MEMECOIN_DESK_INTAKE_EMIT', '0')
    workers = []

    def make(total=None, name='tape', **over):
        env = {} if total is None else {'MEMECOIN_DESK_PS_KEEP_WINDOW_S': total}
        cfg = {**W.knobs(env), 'txshape_pct': '0', **over}
        w = W.Worker(cfg, base=tmp_path / name, sol_price_fn=lambda: None, alert_fn=lambda *a: None)
        workers.append(w)
        return w

    yield make
    for w in workers:
        for conn in w.files._open.values():
            conn.close()


def anchor(w, monkeypatch, received, *, pool='pool', ordinal=1):
    row = event('create_pool')
    row['pool'] = pool
    monkeypatch.setattr(P, 'events', lambda _: [(ordinal, row)])
    w.process(frame(), received_at=received)
    return w.files.conn('ps_keep', received)


@pytest.mark.parametrize('total,want', [(None, (940, 1600)), ('1260', (940, 2200))])
def test_new_create_anchor_uses_total_window_with_fixed_sixty_second_preroll(make_worker, monkeypatch, total, want):
    w = make_worker(total)
    c = anchor(w, monkeypatch, 1000)
    assert c.execute('SELECT keep_from,keep_until FROM ps_keep').fetchone() == want


@pytest.mark.parametrize('bad', ['0', '-1', '659.9', 'nan', 'inf', 'bad', ''])
def test_invalid_total_refuses_before_creating_a_tape(make_worker, bad):
    with pytest.raises(ValueError, match='keep window'):
        make_worker(bad)


def test_workers_keep_independent_startup_windows(make_worker, monkeypatch):
    wide = make_worker('1260', name='wide')
    default = make_worker(name='default')
    for w, until in [(wide, 2200), (default, 1600)]:
        c = anchor(w, monkeypatch, 1000)
        assert c.execute('SELECT keep_until FROM ps_keep').fetchone() == (until,)


def test_backfill_and_rollback_keep_old_bounds_and_new_pool_is_prospective(make_worker, monkeypatch):
    old = make_worker()
    c = anchor(old, monkeypatch, 1000)
    c.execute('DELETE FROM ps_keep')
    c.commit()
    for conn in old.files._open.values():
        conn.close()
    old.files._open.clear()

    wide = make_worker('1260')
    c = wide.files.conn('ps_keep', 2000)
    assert c.execute('SELECT keep_from,keep_until FROM ps_keep').fetchone() == (940, 1600)
    anchor(wide, monkeypatch, 2000, pool='new', ordinal=2)
    wide.keep_window(c, 'new', 2100, 'second', 11)
    wide.keep_window(c, 'new', 1900, 'earlier', 9)
    c.commit()
    assert c.execute("SELECT keep_from,keep_until FROM ps_keep WHERE pool='new'").fetchone() == (1840, 3300)
    for conn in wide.files._open.values():
        conn.close()
    wide.files._open.clear()
    rollback = make_worker()
    c = rollback.files.conn('ps_keep', 3000)
    assert c.execute("SELECT keep_from,keep_until FROM ps_keep WHERE pool='new'").fetchone() == (1840, 3300)


def test_disk_stop_reports_end_without_silently_reverting_the_window(make_worker, monkeypatch):
    w = make_worker('1260', ps_hard_gb='0.000001', ps_resume_hyst_gb='0')
    anchor(w, monkeypatch, 1000)
    w.check_disk()
    w.heartbeat(True)
    beat = json.loads((w.base / 'universe.heartbeat').read_text())
    assert beat['pumpswap_capture'] is False
    assert beat['ps_keep_window_s'] == 1260
    assert isinstance(beat['ps_keep_widen_end'], (int, float)) and beat['ps_keep_widen_end'] > 0
