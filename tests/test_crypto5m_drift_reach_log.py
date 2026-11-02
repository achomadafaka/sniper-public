"""TDD for the drift-reachability SHADOW log (mirrors ev_observation_log: record at a confirmed tick,
resolve the realized mid move at tick_ts + t_hold). Tested against a real in-memory sqlite so the schema
constraints are exercised, not stubbed."""
import sqlite3

import pytest

from sniper.algos.polymarket_crypto5m import drift_reach_log as DL


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    DL.ensure_schema(c)
    return c


def _obs(**over):
    base = {"engine_version": "drift-reach-shadow-v1", "window_start_ts": 1000, "tick_ts": 1100,
            "side": "Up", "condition_id": "0xabc", "z": 0.2, "vol_per_s": 5e-5, "seconds_left": 200,
            "mu_hat": 5e-5, "b_barrier": 6e-5, "touch_prob": 0.7, "exp_fav_move": 0.03,
            "exp_fav_move_driftless": 0.0, "max_hold_s": 64.0, "jump_flag": 0,
            "side_ask": 0.55, "side_bid": 0.53, "side_mid": 0.54, "mid_at_entry": 0.54,
            "t_hold": 10.0, "resolve_at_ts": 1110}
    base.update(over)
    return base


def test_ensure_schema_idempotent(conn):
    DL.ensure_schema(conn)  # second call must not raise
    cols = {r[1] for r in conn.execute("PRAGMA table_info(crypto5m_drift_reach)")}
    for c in ("mu_hat", "exp_fav_move", "exp_fav_move_driftless", "realized_mid_move", "status"):
        assert c in cols


def test_record_pending_inserts_and_returns_id(conn):
    rid = DL.record_pending(conn, _obs())
    assert isinstance(rid, int) and rid > 0
    row = conn.execute("SELECT status, mu_hat, exp_fav_move FROM crypto5m_drift_reach WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "pending"
    assert row["mu_hat"] == pytest.approx(5e-5)
    assert row["exp_fav_move"] == pytest.approx(0.03)


def test_due_pending_only_returns_elapsed_same_window(conn):
    DL.record_pending(conn, _obs(tick_ts=1100, resolve_at_ts=1110))           # due at 1110
    DL.record_pending(conn, _obs(tick_ts=1200, resolve_at_ts=1260))           # not due until 1260
    DL.record_pending(conn, _obs(window_start_ts=9999, resolve_at_ts=1100))   # other window
    due = DL.due_pending(conn, 1000, 1115)
    assert len(due) == 1 and due[0]["resolve_at_ts"] == 1110


def test_complete_observation_computes_realized_move(conn):
    rid = DL.record_pending(conn, _obs(mid_at_entry=0.54))
    DL.complete_observation(conn, rid, mid=0.61, realized_ts=1110)
    row = conn.execute("SELECT status, mid_at_thold, realized_mid_move FROM crypto5m_drift_reach WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "complete"
    assert row["mid_at_thold"] == pytest.approx(0.61)
    assert row["realized_mid_move"] == pytest.approx(0.07)                    # 0.61 - 0.54


def test_sweep_incomplete_marks_past_window_close(conn):
    DL.record_pending(conn, _obs(window_start_ts=1000, resolve_at_ts=1110))
    n = DL.sweep_incomplete(conn, 1000 + 300 + 1)                            # past window close
    assert n == 1
    assert conn.execute("SELECT status FROM crypto5m_drift_reach").fetchone()["status"] == "incomplete"
