import json
import sqlite3

from scripts.analyze_tennis_inverse_paper import summarize_inverse_paper


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE strategies (
            id INTEGER PRIMARY KEY,
            label TEXT,
            strategy_type TEXT,
            status TEXT,
            lifecycle_stage TEXT,
            parameters TEXT,
            pnl_usdc REAL,
            trades_triggered INTEGER,
            instance_tag TEXT
        );
        CREATE TABLE strategy_positions (
            position_id INTEGER PRIMARY KEY,
            strategy_id TEXT,
            state TEXT,
            condition_id TEXT,
            outcome TEXT,
            entry_price REAL,
            current_price REAL,
            size_usdc REAL,
            pnl_usdc REAL,
            opened_ts INTEGER,
            updated_ts INTEGER,
            instance_tag TEXT
        );
        """
    )
    return conn


def test_summarize_inverse_paper_reports_strategy_and_positions(tmp_path):
    conn = _conn()
    conn.execute(
        """
        INSERT INTO strategies
        (id, label, strategy_type, status, lifecycle_stage, parameters, pnl_usdc, trades_triggered, instance_tag)
        VALUES (44, 'Tennis Inverse Fade Paper', 'tennis_inverse_fade', 'active', 'paper',
                '{"mode":"favorite_drop_fade"}', 0, 0, 'tennis-inverse-paper')
        """
    )
    conn.execute(
        """
        INSERT INTO strategy_positions
        (position_id, strategy_id, state, condition_id, outcome, entry_price, current_price,
         size_usdc, pnl_usdc, opened_ts, updated_ts, instance_tag)
        VALUES (1, '44', 'open', 'cid', 'Bob', 0.54, 0.60, 25, 2.77, 100, 200, 'tennis-inverse-paper')
        """
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"cid": {"ref_price": 0.72}, "cid2": {"ref_price": 0.80}}))

    out = summarize_inverse_paper(conn, strategy_id=44, state_path=state_path)

    assert out["strategy"]["status"] == "active"
    assert out["strategy"]["parameters"]["mode"] == "favorite_drop_fade"
    assert out["positions"]["open_count"] == 1
    assert out["positions"]["open_pnl_usdc"] == 2.77
    assert out["state"]["tracked_markets"] == 2


def test_summarize_inverse_paper_handles_missing_state_file():
    conn = _conn()
    conn.execute(
        """
        INSERT INTO strategies
        (id, label, strategy_type, status, lifecycle_stage, parameters, pnl_usdc, trades_triggered, instance_tag)
        VALUES (44, 'Tennis Inverse Fade Paper', 'tennis_inverse_fade', 'active', 'paper',
                '{}', 0, 0, 'tennis-inverse-paper')
        """
    )

    out = summarize_inverse_paper(conn, strategy_id=44, state_path="/missing/state.json")

    assert out["state"]["tracked_markets"] == 0
    assert out["positions"]["open_count"] == 0
