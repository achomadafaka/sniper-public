import sqlite3
from sniper.algos.polymarket_crypto5m import ev_observation_log as EVLOG


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    EVLOG.ensure_schema(c)
    return c


def _obs(ws, tick, *, resolve_at, mid, passed_007=0, passed_realfee=1):
    return {"engine_version": "ev-reach-v1", "campaign_id": "active-paper-v0", "window_start_ts": ws,
            "tick_ts": tick, "side": "Up", "condition_id": "0xc", "token_id": "t",
            "z_model": -0.2, "market_probit_z": -0.21, "conviction": 0.7, "vol_per_s": 0.0002,
            "seconds_left": 200, "side_ask": 0.45, "side_bid": 0.44, "side_mid": mid, "spread": 0.01,
            "delta": 1.2, "theta": 0.001, "t_star": 9.0, "t_hold": 12.0, "gain": 0.012, "gate_stat": 0.05,
            "cost_007": 0.027, "cost_realfee": 0.01, "ev_007": -0.015, "ev_realfee": 0.002,
            "passed_007": passed_007, "passed_realfee": passed_realfee,
            "mid_at_entry": mid, "bid_at_entry": 0.44, "ask_at_entry": 0.45, "resolve_at_ts": resolve_at}


def test_record_due_complete_roundtrip(tmp_path):
    c = _conn(str(tmp_path / "ev.sqlite"))
    oid = EVLOG.record_pending(c, _obs(1782000000, 1782000100, resolve_at=1782000112, mid=0.45))
    assert EVLOG.due_pending(c, 1782000000, 1782000110) == []          # not due yet
    due = EVLOG.due_pending(c, 1782000000, 1782000113)
    assert len(due) == 1 and due[0]["id"] == oid
    EVLOG.complete_observation(c, oid, mid=0.52, bid=0.51, ask=0.53, realized_ts=1782000114)
    row = c.execute("SELECT * FROM crypto5m_ev_observations WHERE id=?", (oid,)).fetchone()
    assert row["status"] == "complete"
    assert abs(row["realized_mid_move"] - 0.07) < 1e-9                  # 0.52 - 0.45


def test_pending_persists_across_reconnect(tmp_path):
    p = str(tmp_path / "ev.sqlite")
    c1 = _conn(p)
    EVLOG.record_pending(c1, _obs(1782000000, 1782000100, resolve_at=1782000112, mid=0.45))
    c1.close()
    c2 = _conn(p)                                                       # simulate restart
    assert len(EVLOG.due_pending(c2, 1782000000, 1782000113)) == 1


def test_sweep_incomplete_on_window_close(tmp_path):
    c = _conn(str(tmp_path / "ev.sqlite"))
    EVLOG.record_pending(c, _obs(1782000000, 1782000100, resolve_at=1782000112, mid=0.45))
    assert EVLOG.sweep_incomplete(c, 1782000000 + 100) == 0            # window not closed
    assert EVLOG.sweep_incomplete(c, 1782000000 + 301) == 1            # closed -> incomplete
    row = c.execute("SELECT status FROM crypto5m_ev_observations").fetchone()
    assert row["status"] == "incomplete"


def test_admit_fraction(tmp_path):
    c = _conn(str(tmp_path / "ev.sqlite"))
    EVLOG.record_pending(c, _obs(1, 11, resolve_at=99, mid=0.45, passed_007=0, passed_realfee=1))
    EVLOG.record_pending(c, _obs(1, 12, resolve_at=99, mid=0.45, passed_007=1, passed_realfee=1))
    af = EVLOG.admit_fraction(c, "ev-reach-v1")
    assert af["n"] == 2 and af["n_pass_007"] == 1 and af["frac_007"] == 0.5
    assert af["n_pass_realfee"] == 2 and af["frac_realfee"] == 1.0
