# tests/test_action_tool.py
"""Tests for agents/tools/action_tool.py — stop, create, activate commands."""
import sqlite3
import subprocess
import sys
import json
import pytest
from pathlib import Path

ROOT = Path(__file__).parent.parent


@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "test.db")
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE strategies (
        id INTEGER PRIMARY KEY, wallet TEXT, label TEXT, strategy_type TEXT,
        parameters TEXT DEFAULT '{}', status TEXT DEFAULT 'active',
        pause_reason TEXT, paused_at TEXT, created_at TEXT, updated_at TEXT
    )""")
    c.execute("""INSERT INTO strategies (id, wallet, label, strategy_type, status, created_at, updated_at)
                 VALUES (1, '0xtest', 'Test Strategy', 'insider_copy_v1', 'active', '2026-03-20', '2026-03-20')""")
    c.commit()
    c.close()
    return p


def _run_action(db_path, *args):
    result = subprocess.run(
        [sys.executable, str(ROOT / "agents/tools/action_tool.py")] + list(args),
        capture_output=True, text=True,
        env={**__import__("os").environ, "DB_PATH": db_path, "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "",
             "SUPABASE_URL": "", "SUPABASE_SERVICE_KEY": "", "SUPABASE_ANON_KEY": ""},
             timeout=60,
         )
    return result


def test_stop_sets_status_stopped(db):
    result = _run_action(db, "stop", "--strategy-id", "1", "--reason", "wallet dormant 30d")
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = json.loads(result.stdout)
    assert out["ok"] is True
    assert out["action"] == "stop"

    c = sqlite3.connect(db)
    row = c.execute("SELECT status, pause_reason FROM strategies WHERE id=1").fetchone()
    assert row[0] == "stopped"
    assert "scout_stop" in row[1]


def test_stop_already_stopped(db):
    c = sqlite3.connect(db)
    c.execute("UPDATE strategies SET status='stopped' WHERE id=1")
    c.commit()
    c.close()
    result = _run_action(db, "stop", "--strategy-id", "1", "--reason", "test")
    assert result.returncode == 1


def test_activate_changes_paused_to_active(db):
    c = sqlite3.connect(db)
    c.execute("INSERT INTO strategies (id, wallet, label, strategy_type, status, created_at, updated_at) VALUES (2, '0xtest2', 'Test2', 'insider_copy_v1', 'paused', '2026-03-20', '2026-03-20')")
    c.commit()
    c.close()
    result = _run_action(db, "activate", "--strategy-id", "2", "--reason", "vetted by Scout")
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = json.loads(result.stdout)
    assert out["ok"] is True

    c = sqlite3.connect(db)
    row = c.execute("SELECT status FROM strategies WHERE id=2").fetchone()
    assert row[0] == "active"


def test_activate_rejects_non_paused(db):
    # Strategy 1 is active, not paused
    result = _run_action(db, "activate", "--strategy-id", "1", "--reason", "test")
    assert result.returncode == 1
