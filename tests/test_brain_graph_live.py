"""Derived live state: never stored, never guessed, and knob values resolved the way the RUNNER
resolves them (plan review finding 21)."""
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from bot.brain_graph import live


def test_derive_never_guesses(tmp_path):
    d = live.derive(tmp_path / "nonexistent")
    assert d["origin_main"] is None and d["open_real_positions"] is None and d["heartbeat_age_s"] is None
    assert d["errors"]
    out = live.render(d)
    assert "UNKNOWN" in out and "read errors" in out


def test_state_root_is_the_main_checkout_even_from_a_worktree(tmp_path, monkeypatch):
    monkeypatch.delenv("SNIPER_STATE_ROOT", raising=False)
    main = tmp_path / "main"
    main.mkdir()
    g = lambda *a, cwd=main: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
    g("init", "-q", "-b", "main")
    (main / "f").write_text("x")
    g("add", "f")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")
    g("worktree", "add", "-q", str(tmp_path / "wt"), "-b", "b")
    assert live.state_root(tmp_path / "wt") == main.resolve()
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path / "elsewhere"))
    assert live.state_root(tmp_path / "wt") == tmp_path / "elsewhere"


def test_knob_values_follow_the_runners_order(tmp_path):
    env = tmp_path / ".env"
    env.write_text("\n".join([
        "MEMECOIN_J7_SOLANA_MODE=shadow",
        "MEMECOIN_SOL_TAPE_MIN_CALL_S=0.5",
        "MEMECOIN_SOL_TAPE_MIN_CALL_S=9.9",              # duplicate: load_env_files keeps the FIRST
        "MEMECOIN_REAL_SOURCE_BUY_ABS_MIN_USD=200",
        "SNIPER_STATE_ROOT=/from/dotenv",
        "MEMECOIN_EVM_RPC_POOL_URLS_4663=https://k/abc",
    ]))
    settings = SimpleNamespace(MEMECOIN_REAL_TP_PCT=25.0)
    got = live.knob_values(tmp_path, [
        "MEMECOIN_J7_SOLANA_MODE", "MEMECOIN_SOL_TAPE_MIN_CALL_S", "MEMECOIN_REAL_SOURCE_BUY_ABS_MIN_USD",
        "SNIPER_STATE_ROOT", "MEMECOIN_EVM_RPC_POOL_URLS_4663", "MEMECOIN_REAL_TP_PCT", "MEMECOIN_NOT_A_KNOB"],
        overrides={"MEMECOIN_REAL_SOURCE_BUY_ABS_MIN_USD": 197},
        runner_env={"SNIPER_STATE_ROOT": "/home/bigpapi/Desktop/Sniper"}, settings=settings)
    assert got["MEMECOIN_J7_SOLANA_MODE"] == ("shadow", ".env")
    assert got["MEMECOIN_SOL_TAPE_MIN_CALL_S"] == ("0.5", ".env")
    assert got["MEMECOIN_REAL_SOURCE_BUY_ABS_MIN_USD"] == (197, "cockpit overlay")
    assert got["SNIPER_STATE_ROOT"] == ("/home/bigpapi/Desktop/Sniper", "runner process env")
    assert got["MEMECOIN_EVM_RPC_POOL_URLS_4663"] == ("(secret, name only)", "-")
    assert got["MEMECOIN_REAL_TP_PCT"] == (25.0, "code default")
    assert got["MEMECOIN_NOT_A_KNOB"] == ("(unset)", "-")


def test_pending_restart_compares_the_env_file_with_the_runner_start(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    past = time.time() - 3600
    os.utime(env, (past, past))
    from datetime import datetime, timezone
    fomo, missing = tmp_path / ".fomo.env", tmp_path / "absent.env"
    now = datetime.now(timezone.utc).isoformat()
    assert live.pending_restart([env, missing], now) is False
    assert live.pending_restart([env], "2000-01-01T00:00:00+00:00") is True
    assert live.pending_restart([env], "2000-01-01T00:00:00") is True        # naive stamp read as UTC
    assert live.pending_restart([env], None) is None
    fomo.write_text("B=2\n")                                                 # .fomo.env edited after start
    assert live.pending_restart([env, fomo], datetime.fromtimestamp(past + 60, timezone.utc).isoformat()) is True


def test_deploy_detection_matches_a_real_invocation_only():
    lines = ["bash /home/bigpapi/Desktop/Sniper/scripts/deploy_memecoin_runtime.sh 0fdcffc2 --defer-backup",
             "/bin/bash -c cat logs/deploy_memecoin_runtime.log | tail",
             "/usr/bin/grep deploy_memecoin_runtime",
             "python3 /home/bigpapi/.claude/shell-snapshots/x deploy_memecoin_runtime.sh"]
    assert live.deploying(lines) is True
    assert live.deploying(lines[1:]) is False


def test_fomo_env_quotes_and_per_file_source(tmp_path):
    # Lambda's review L1/L3: the runner loads .env AND .fomo.env (first wins, quotes stripped).
    (tmp_path / ".env").write_text('MEMECOIN_J7_SOLANA_MODE="shadow"\n')
    (tmp_path / ".fomo.env").write_text("FOMO_ONLY_KNOB=abc\nMEMECOIN_J7_SOLANA_MODE=live\n")
    got = live.knob_values(tmp_path, ["MEMECOIN_J7_SOLANA_MODE", "FOMO_ONLY_KNOB"])
    assert got["MEMECOIN_J7_SOLANA_MODE"] == ("shadow", ".env")
    assert got["FOMO_ONLY_KNOB"] == ("abc", ".fomo.env")


def test_a_secret_looking_value_is_hidden_whatever_its_name(tmp_path):
    # L4: the name filter misses a future ..._ENDPOINT holding a keyed wss URL.
    (tmp_path / ".env").write_text("MEMECOIN_FOO_ENDPOINT=wss://x.example/?api-key=abc123def456ghi789\n"
                                   "MEMECOIN_PLAIN=25\n")
    got = live.knob_values(tmp_path, ["MEMECOIN_FOO_ENDPOINT", "MEMECOIN_PLAIN"])
    assert got["MEMECOIN_FOO_ENDPOINT"][0].startswith("(hidden") and got["MEMECOIN_PLAIN"] == ("25", ".env")


def test_the_overlay_is_what_the_runner_applies(tmp_path, monkeypatch):
    # L2: only ADJUSTABLE names apply, and one unparseable row must not drop the whole overlay.
    import sqlite3
    from sniper.services.memecoin_entry_knobs import ADJUSTABLE
    adjustable = sorted(ADJUSTABLE)[0]
    db = tmp_path / "sniper_tracker.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE memecoin_entry_knob_overrides (name TEXT PRIMARY KEY, value TEXT NOT NULL, "
              "active INTEGER NOT NULL DEFAULT 1)")
    c.executemany("INSERT INTO memecoin_entry_knob_overrides VALUES (?,?,1)",
                  [(adjustable, "197"), ("NOT_A_REGISTRY_KNOB", "1"), ("MEMECOIN_BROKEN_ROW", "{not json")])
    c.commit()
    c.close()
    assert live._overrides(tmp_path) == {adjustable: 197}
