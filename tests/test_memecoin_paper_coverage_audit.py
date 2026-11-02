"""Unknown paper coverage is a recorded hold, never an invented exit witness."""
import pytest

from sniper.services import memecoin_exits as ex, memecoin_volume_death as volume
from sniper.settings import memecoin as cfg
from test_memecoin_paper_vol_death_rail import _conn, _pos, NOW


@pytest.mark.parametrize('healthy,expected', [(False, 'blind_frozen'), (True, 'swap_lane_uncovered')])
def test_real_uncovered_evaluation_records_hold(monkeypatch, healthy, expected):
    monkeypatch.setattr(cfg, 'MEMECOIN_VOLDEATH_ARM_MIN_OPENED_AT', '2026-01-01T00:00:00+00:00')
    monkeypatch.setattr(volume, 'stream_healthy', lambda *a, **kw: healthy)
    conn = _conn()
    try:
        assert ex._paper_vol_death_reason(conn, _pos(), NOW) is None
        row = conn.execute('SELECT allowed,detail,lane FROM memecoin_exit_trigger_audit '
                           "WHERE reason='paper_vol_death'").fetchone()
        assert row is not None
        assert row['allowed'] == 0 and row['lane'] == 'paper'
        assert row['detail'] == expected + ' silence=unknown'
        ex._paper_vol_death_reason(conn, _pos(), NOW)
        assert conn.execute('SELECT COUNT(*) FROM memecoin_exit_trigger_audit').fetchone()[0] == 1
    finally:
        conn.close()


def test_paper_evaluation_failure_is_recorded_as_unknown(monkeypatch):
    def fail(*a, **kw):
        raise RuntimeError('fixture unavailable')
    monkeypatch.setattr(volume, 'armed_exit_decision', fail)
    conn = _conn()
    try:
        assert ex._paper_vol_death_reason(conn, _pos(), NOW) is None
        row = conn.execute('SELECT allowed,detail FROM memecoin_exit_trigger_audit').fetchone()
        assert row is not None and row['allowed'] == 0
        assert row['detail'] == 'evaluation_error silence=unknown'
    finally:
        conn.close()
