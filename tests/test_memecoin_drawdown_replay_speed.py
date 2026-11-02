"""Historical replay must leave dispatch faster without weakening fresh-ledger risk checks."""
import concurrent.futures
from datetime import datetime, timedelta, timezone
import sqlite3
import threading

import pytest

from sniper.persistence import db_memecoin as m
from test_memecoin_drawdown_pause import DAY, _c, _closed


@pytest.fixture(autouse=True)
def cold_replay_cache(monkeypatch):
    monkeypatch.setattr(m, '_DRAWDOWN_REPLAY', threading.local(), raising=False)


def _history():
    c = _c()
    _closed(c, -110.125, '23:55', day='2026-09-14')
    _closed(c, -120.375, '00:10')
    return c


def _until(c, *, drop=100, pause=3600, now='00:20'):
    return m.drawdown_pause_until(c, DAY, drop, pause, DAY + 'T' + now + ':00+00:00')


def test_unchanged_closes_are_not_replayed_but_the_ledger_is_read_again():
    c = _history()
    _closed(c, 33.0625, '10:00', day='2026-09-10')
    m.set_risk_reset(c, '2026-01-01', actor='test', reason='old reset',
                     now_iso='2026-01-01T00:00:00+00:00')
    converted, reads = [], []

    class ObservedPnl(float):
        def __float__(self):
            converted.append(float.__float__(self))
            return float.__float__(self)

    def row_factory(cursor, row):
        if [x[0] for x in cursor.description] == ['closed_at', 'realized_pnl']:
            return row[0], ObservedPnl(row[1])
        return row

    c.row_factory = row_factory
    c.set_trace_callback(reads.append)
    assert _until(c) == DAY + 'T00:55:00+00:00'
    assert len(converted) == 3
    assert _until(c, now='00:21') == DAY + 'T00:55:00+00:00'
    assert converted.count(33.0625) == 1
    assert len(converted) == 5
    assert sum('SELECT closed_at, realized_pnl' in sql for sql in reads) == 2
    assert sum('FROM memecoin_risk_resets' in sql for sql in reads) == 2


def test_a_corrected_prior_day_recomputes_the_current_days_incoming_pause():
    c = _history()
    assert _until(c) == DAY + 'T00:55:00+00:00'
    c.execute("UPDATE memecoin_positions SET realized_pnl=10 WHERE closed_at=?",
              ('2026-09-14T23:55:00+00:00',))
    assert _until(c) == DAY + 'T01:10:00+00:00'
    c.rollback()
    assert _until(c) == DAY + 'T00:55:00+00:00'


def test_settings_and_operator_reset_are_not_cached():
    c = _history()
    assert _until(c) == DAY + 'T00:55:00+00:00'
    assert _until(c, drop=200) is None
    assert _until(c, pause=7200) == DAY + 'T01:55:00+00:00'
    m.set_risk_reset(c, DAY, actor='test', reason='reset', now_iso=DAY + 'T00:15:00+00:00')
    assert _until(c) is None


def test_old_corrections_invalidate_a_cached_pause_spanning_several_midnights():
    c = _history()
    _closed(c, -101.875, '12:00', day='2026-09-10')
    pause = 7 * 86400
    assert _until(c, pause=pause) == '2026-09-17T12:00:00+00:00'
    assert _until(c, pause=pause) == '2026-09-17T12:00:00+00:00'
    c.execute("UPDATE memecoin_positions SET realized_pnl=1 WHERE closed_at=?",
              ('2026-09-10T12:00:00+00:00',))
    assert _until(c, pause=pause) == '2026-09-21T23:55:00+00:00'


def test_a_close_past_the_exact_clock_cannot_trigger_a_pause():
    c = _c()
    _closed(c, -101.875, '00:10')
    c.execute("UPDATE memecoin_positions SET closed_at=?",
              (DAY + 'T00:10:00.000001+00:00',))
    assert m.drawdown_pause_until(c, DAY, 100, 3600, DAY + 'T00:10:00+00:00') is None


def test_history_beyond_the_cache_bound_still_propagates_an_old_correction():
    c = _c()
    start = datetime(2025, 1, 1, 12, tzinfo=timezone.utc)
    for index in range(160):
        _closed(c, -101.0, '12:00', day=(start + timedelta(days=index)).date().isoformat())
    now = (start + timedelta(days=160, hours=1)).isoformat()
    args = (c, now[:10], 100, 7 * 86400, now)
    expected = (start + timedelta(days=161)).isoformat()
    assert m.drawdown_pause_until(*args) == expected
    assert m.drawdown_pause_until(*args) == expected
    c.execute('UPDATE memecoin_positions SET realized_pnl=0 WHERE closed_at=?', (start.isoformat(),))
    assert m.drawdown_pause_until(*args) == (start + timedelta(days=162)).isoformat()


def test_a_failed_ledger_read_never_reuses_the_last_good_pause():
    c = _history()
    assert _until(c) == DAY + 'T00:55:00+00:00'
    c.execute('DROP TABLE memecoin_positions')
    with pytest.raises(sqlite3.OperationalError):
        _until(c)


def test_replay_reads_are_covered_without_fetching_wide_position_records():
    c = _history()
    reads = []
    c.set_trace_callback(reads.append)
    _until(c)
    sql = next(sql for sql in reads if 'SELECT closed_at, realized_pnl' in sql)
    plan = c.execute('EXPLAIN QUERY PLAN ' + sql).fetchall()
    assert any('COVERING INDEX idx_mc_real_drawdown_replay' in row[3] for row in plan)


def test_independent_thread_connections_keep_their_own_risk_state():
    def run(pnl):
        c = _c()
        _closed(c, pnl, '00:10')
        result = [_until(c) for _ in range(4)]
        c.close()
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        paused, allowed = list(pool.map(run, (-130.625, 13.875)))
    assert paused == [DAY + 'T01:10:00+00:00'] * 4
    assert allowed == [None] * 4
