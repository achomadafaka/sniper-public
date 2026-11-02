import sqlite3
from sniper.algos.polymarket_crypto5m import z_shadow_log as ZL


def _conn(p):
    c = sqlite3.connect(p); c.row_factory = sqlite3.Row
    ZL.ensure_schema(c); return c


def _row(ws, tick, **kw):
    d = {"engine_version": "brain-v2-shadow-v1", "window_start_ts": ws, "tick_ts": tick,
         "condition_id": "0xc", "btc_now": 60050.0, "btc_start": 60000.0, "seconds_left": 100,
         "vol_slow": 1e-4, "vol_fast": 3e-4, "jump_flag": 0, "divergence": 3.0,
         "z": 0.5, "z_ours": 0.52, "z_market": 0.20, "market_mid": 0.55, "z_v2": 0.40, "weight": 0.6}
    d.update(kw); return d


def test_record_and_resolve_window(tmp_path):
    c = _conn(str(tmp_path / "z.sqlite"))
    ws = 1782000000
    ZL.record_tick(c, _row(ws, ws + 60, jump_flag=1))
    ZL.record_tick(c, _row(ws, ws + 120))
    n = ZL.resolve_window(c, ws, settlement=1, sharp_label="sharp_revert", now_ts=ws + 305)
    assert n == 2
    rows = c.execute("SELECT status, settlement, sharp_label FROM crypto5m_z_shadow").fetchall()
    assert all(r["status"] == "resolved" and r["settlement"] == 1 and r["sharp_label"] == "sharp_revert"
               for r in rows)


def test_unresolved_window_swept_incomplete_not_zero(tmp_path):
    c = _conn(str(tmp_path / "z.sqlite"))
    ws = 1782000000
    ZL.record_tick(c, _row(ws, ws + 60))
    assert ZL.sweep_incomplete(c, ws + 100) == 0          # window still open
    assert ZL.sweep_incomplete(c, ws + 301) == 1          # closed, never resolved
    r = c.execute("SELECT status, settlement FROM crypto5m_z_shadow").fetchone()
    assert r["status"] == "incomplete" and r["settlement"] is None   # excluded, not counted as 0


def test_pending_persists_across_reconnect(tmp_path):
    p = str(tmp_path / "z.sqlite")
    c1 = _conn(p); ZL.record_tick(c1, _row(1782000000, 1782000060)); c1.close()
    c2 = _conn(p)
    assert c2.execute("SELECT COUNT(*) FROM crypto5m_z_shadow WHERE status='pending'").fetchone()[0] == 1
