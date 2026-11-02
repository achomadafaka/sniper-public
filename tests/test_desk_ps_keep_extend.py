"""PumpSwap keep extension: extend-only, host clock, never able to stop capture. Temporary tapes only."""
import json
import os
import time

import pytest

from sniper.services import memecoin_desk_keep_extend as KE
from test_desk_ps_keep_window import anchor, make_worker  # noqa: F401  (fixture reuse)

NOW = 2_000_000.0
POOL = '9' * 43


def put(path, pools, written=NOW, version=1):
    path.write_text(json.dumps(dict(version=version, written_at=written, pools=pools)))
    return str(path)


def test_read_accepts_a_current_list_and_drops_absurd_entries(tmp_path):
    f = put(tmp_path / 'k.json', {POOL: NOW + 3000, '0' * 43: NOW + 3000, '8' * 43: NOW - 1, '7' * 43: NOW + 99 * 3600, '6' * 43: 'soon', 'short': NOW + 5})
    got = KE.read(f, NOW)
    assert got['state'] == 'ok' and got['pools'] == {POOL: NOW + 3000} and got['dropped'] == 5


@pytest.mark.parametrize('why,make', [
    ('missing', lambda t: str(t / 'none.json')),
    ('stale', lambda t: put(t / 'k.json', {POOL: NOW + 3000}, written=NOW - KE.FRESH_S - 1)),
    ('clock_disagreement', lambda t: put(t / 'k.json', {POOL: NOW + 3000}, written=NOW + KE.FUTURE_S + 1)),
    ('malformed', lambda t: put(t / 'k.json', {POOL: NOW + 3000}, version=2)),
])
def test_an_unusable_list_changes_nothing_and_says_why(tmp_path, why, make):
    got = KE.read(make(tmp_path), NOW)
    assert got['state'] == 'unavailable' and got['reason'] == why and got['pools'] == {}


def test_oversized_and_unparseable_and_unknown_clock(tmp_path):
    big = tmp_path / 'big.json'; big.write_text(' ' * (KE.MAX_BYTES + 1))
    bad = tmp_path / 'bad.json'; bad.write_text('{nope')
    assert KE.read(str(big), NOW)['reason'] == 'oversized' and KE.read(str(bad), NOW)['reason'] == 'unreadable'
    assert KE.read(str(bad), float('nan'))['reason'] == 'clock_unknown'


def test_warn_once_per_cause_per_hour():
    w = KE.Warn()
    assert w.due('a', 0) and not w.due('a', 3599) and w.due('a', 3600) and w.due('b', 1)


def test_extension_only_widens_keep_until_and_never_touches_keep_from(make_worker, monkeypatch):
    w = make_worker('1260')
    now = time.time()
    c = anchor(w, monkeypatch, now - 30, pool=POOL)                     # window [now-90, now+1170]
    before = c.execute('SELECT keep_from,keep_until FROM ps_keep').fetchone()
    KE.write(str(w.ps_ext_path), {POOL: now + 3700}, now)
    w.keep_extend_tick(now)
    after = c.execute('SELECT keep_from,keep_until FROM ps_keep').fetchone()
    assert after[0] == before[0] and after[1] == pytest.approx(now + 3700) and after[1] > before[1]
    KE.write(str(w.ps_ext_path), {POOL: now + 100}, now)                  # a shorter request never shortens
    w.keep_extend_tick(now)
    assert c.execute('SELECT keep_until FROM ps_keep').fetchone()[0] == pytest.approx(now + 3700)
    os.remove(w.ps_ext_path)                                              # rollback stops future extension only: the written window stays
    w.keep_extend_tick(now)
    assert c.execute('SELECT keep_until FROM ps_keep').fetchone()[0] == pytest.approx(now + 3700)


def test_a_pool_outside_the_stored_set_is_stored_while_listed_and_selected_from_its_inclusion(make_worker):
    w = make_worker('1260')
    now = time.time()
    other = '8' * 43
    KE.write(str(w.ps_ext_path), {other: now + 3700}, now)
    w.keep_extend_tick(now)
    assert other in w.ps_extended and not w.files.conn('ps_keep', now).execute('SELECT 1 FROM ps_keep WHERE pool=?', (other,)).fetchone()   # no ps_keep row, keep_from untouched
    rows = w.files.conn('ps_pool_selection', now).execute('SELECT start_at,end_at FROM ps_pool_selection WHERE pool=?', (other,)).fetchall()
    assert rows == [(pytest.approx(now), None)]                           # never earlier than inclusion
    KE.write(str(w.ps_ext_path), {}, now)                                 # leaves the list: selection ends then
    w.keep_extend_tick(now + 1)
    assert other not in w.ps_extended
    assert w.files.conn('ps_pool_selection', now).execute('SELECT end_at FROM ps_pool_selection WHERE pool=?', (other,)).fetchone()[0] == pytest.approx(now + 1)


def test_soft_shed_drops_extensions_below_the_hard_cap_and_never_stops_capture(make_worker):
    w = make_worker('1260')
    now = time.time()
    other = '8' * 43
    KE.write(str(w.ps_ext_path), {other: now + 3700}, now)
    w.keep_extend_tick(now)
    cap = float(w.cfg['ps_hard_gb'])
    w.keep_extend_shed(0.81 * cap, 500.0, now)                            # under the hard cap (1.0), over the soft line (0.8)
    assert w.ps_ext_shed and other not in w.ps_extended and w.ps_capture
    w.keep_extend_tick(now + 1)                                           # a shed worker ignores the list
    assert other not in w.ps_extended
    w.keep_extend_shed(0.5 * cap, 500.0, now + 2)                         # hysteresis: back on below 70 percent
    assert not w.ps_ext_shed
    w.keep_extend_shed(0.1 * cap, KE.SHED_FREE_GB - 1, now + 3)           # host free space under 100 GB sheds too
    assert w.ps_ext_shed


def _load_writer():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('ke_writer', Path(__file__).resolve().parents[1] / 'scripts/memecoin_desk_keep_extend_writer.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_writer_keeps_the_first_sight_deadline_and_open_orders_refresh():
    Wr = _load_writer()
    pools, state = Wr.build({}, {'a' * 43}, set(), 1000.0)
    assert pools == {'a' * 43: 1000.0 + KE.SPAN_S + 60.0}
    later, state = Wr.build(state, {'a' * 43}, set(), 1500.0)                  # seen again: the deadline does not slide
    assert later == pools
    expired, state = Wr.build(state, set(), set(), 1000.0 + KE.SPAN_S + 61.0)
    assert expired == {} and state == {}
    held, _ = Wr.build({}, set(), {'b' * 43}, 5000.0)                           # an open order is refreshed to now + SPAN_S each run
    assert held == {'b' * 43: 5000.0 + KE.SPAN_S}


def test_writer_output_is_what_the_worker_reads(tmp_path):
    Wr = _load_writer()
    pools, _ = Wr.build({}, {POOL}, set(), time.time())
    path = tmp_path / 'pumpswap' / KE.FILE_NAME
    KE.write(str(path), pools, time.time())
    got = KE.read(str(path), time.time())
    assert got['state'] == 'ok' and set(got['pools']) == {POOL}


def test_expiry_runs_even_when_the_file_is_gone_or_broken(make_worker):
    w = make_worker('1260')
    now = time.time()
    other = '8' * 43
    KE.write(str(w.ps_ext_path), {other: now + 10}, now)
    w.keep_extend_tick(now)
    assert other in w.ps_extended
    os.remove(w.ps_ext_path)                                              # writer dead, file gone
    w.keep_extend_tick(now + 11)
    assert other not in w.ps_extended
    assert w.files.conn('ps_pool_selection', now).execute('SELECT end_at FROM ps_pool_selection WHERE pool=?', (other,)).fetchone()[0] == pytest.approx(now + 11)


def test_extension_closes_only_the_selection_row_it_opened_in_the_real_capture_session(make_worker):
    w = make_worker('1260')
    now = time.time()
    other = '8' * 43
    sel = w.files.conn('ps_pool_selection', now)
    sel.execute('INSERT INTO ps_pool_selection VALUES(?,?,?,?,?)', (other, w.ps_coverage_session, now - 5, None, 1))      # the feed's row, still open
    KE.write(str(w.ps_ext_path), {other: now + 10}, now)
    w.keep_extend_tick(now)
    w.keep_extend_tick(now + 11)                                          # extension expires
    rows = dict(sel.execute('SELECT start_at, end_at FROM ps_pool_selection WHERE pool=?', (other,)).fetchall())
    assert rows[pytest.approx(now - 5) if False else now - 5] is None and rows[now] == pytest.approx(now + 11)       # feed row open, ours closed
    assert {r[0] for r in sel.execute('SELECT session FROM ps_pool_selection WHERE pool=?', (other,))} == {w.ps_coverage_session}      # PSL.pool_coverage joins on this


def test_a_later_ordinary_anchor_is_baseline_and_a_shed_does_not_undo_it(make_worker, monkeypatch):
    w = make_worker('1260')
    now = time.time()
    c = anchor(w, monkeypatch, now - 30, pool=POOL)
    KE.write(str(w.ps_ext_path), {POOL: now + 3700}, now)
    w.keep_extend_tick(now)
    anchor(w, monkeypatch, now + 10, pool=POOL, ordinal=2)                 # a legitimate second anchor: its end (now+1170+) becomes the baseline
    baseline = now + 10 + 1260 - 60
    w.keep_extend_shed(0.81 * float(w.cfg['ps_hard_gb']), 500.0, now + 11)
    assert c.execute('SELECT keep_until FROM ps_keep').fetchone()[0] == pytest.approx(baseline)


def test_shed_rolls_back_the_durable_widening_of_existing_pools_explicitly(make_worker, monkeypatch):
    w = make_worker('1260')
    now = time.time()
    c = anchor(w, monkeypatch, now - 30, pool=POOL)
    base = c.execute('SELECT keep_until FROM ps_keep').fetchone()[0]
    KE.write(str(w.ps_ext_path), {POOL: now + 3700}, now)
    w.keep_extend_tick(now)
    assert c.execute('SELECT keep_until FROM ps_keep').fetchone()[0] == pytest.approx(now + 3700)
    w.keep_extend_shed(0.81 * float(w.cfg['ps_hard_gb']), 500.0, now + 1)
    assert c.execute('SELECT keep_until FROM ps_keep').fetchone()[0] == pytest.approx(max(base, now + 1))      # back to its own end, counted
    assert w.stats['ps_ext_rolled_back'] == 1 and w.ps_capture
    w.keep_extend_tick(now + 2)                                           # shed: the list is ignored
    assert c.execute('SELECT keep_until FROM ps_keep').fetchone()[0] == pytest.approx(max(base, now + 1))


def test_a_feed_that_lets_go_first_reopens_selection_for_a_still_extended_pool(make_worker):
    from types import SimpleNamespace
    w = make_worker('1260')
    now = time.time()
    other = '8' * 43
    sel = w.files.conn('ps_pool_selection', now)
    book = SimpleNamespace(pools={other}, apply_listed=lambda read, at: None)
    w.intake_book = book
    w.ps_selected = {other}
    sel.execute('INSERT INTO ps_pool_selection VALUES(?,?,?,?,?)', (other, w.ps_coverage_session, now - 5, None, 1))
    KE.write(str(w.ps_ext_path), {other: now + 3700}, now)
    w.keep_extend_tick(now)
    assert w.ps_ext_rows[other] is None                                    # the feed row covers it, we opened none
    book.pools = set()                                                     # the feed lists it no more
    w.intake_apply({}, now + 1)
    open_rows = sel.execute('SELECT start_at FROM ps_pool_selection WHERE pool=? AND end_at IS NULL', (other,)).fetchall()
    assert open_rows == [(pytest.approx(now + 1),)] and w.ps_ext_rows[other] == pytest.approx(now + 1)    # still being stored, so still covered
