import sqlite3

from scripts import crypto5m_campaign_guard as GUARD
from scripts import run_polymarket_crypto5m_maker as MK


def _loss_window(conn, *, idx: int, campaign: str = "maker-regime-v1"):
    ws = 1000 + idx * 300
    cid = f"0xloss{idx}"
    conn.execute(
        """INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,
           realized_pnl,settled,winning_outcome,ts,campaign_id)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (cid, ws, 10.0, 0.0, -5.0, -5.0, 1, "Down", ws + 300, campaign),
    )
    conn.execute(
        """INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,
           price,size,fair,ts,reason,fill_model)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (campaign, cid, ws, "up", "bid", 0.50, 10.0, 0.55, ws + 10, "entry", "print"),
    )


def test_guard_does_not_pause_below_hard_gate_even_when_interim_ready():
    conn = MK._db(":memory:")
    for i in range(60):
        _loss_window(conn, idx=i)
    conn.commit()

    decision = GUARD.evaluate_campaign_guard(
        conn,
        "maker-regime-v1",
        min_windows=200,
        interim_min_windows=60,
        bootstrap_samples=200,
    )

    assert decision["hard_eval"]["n_settled"] == 60
    assert decision["interim_ready"] is True
    assert decision["kill_recommended"] is False
    assert decision["action"] == "keep_running"


def test_guard_pauses_when_hard_gate_ci_upper_is_below_zero():
    conn = MK._db(":memory:")
    for i in range(200):
        _loss_window(conn, idx=i)
    conn.commit()

    decision = GUARD.evaluate_campaign_guard(
        conn,
        "maker-regime-v1",
        min_windows=200,
        interim_min_windows=60,
        bootstrap_samples=200,
    )

    assert decision["hard_eval"]["eval_ready"] is True
    assert decision["hard_eval"]["ci_high"] < 0
    assert decision["kill_recommended"] is True
    assert decision["action"] == "pause_maker"
