import importlib
import os
from pathlib import Path

import pytest

from sniper.util import state_paths


def test_default_state_root_is_source_repository(monkeypatch):
    monkeypatch.delenv("SNIPER_STATE_ROOT", raising=False)
    assert state_paths.state_root() == Path(__file__).resolve().parents[1]
    assert state_paths.state_path("data", "x") == Path(__file__).resolve().parents[1] / "data" / "x"


def test_explicit_state_root_is_absolute_and_path_cannot_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    assert state_paths.state_path("logs", "x.log") == tmp_path / "logs" / "x.log"
    with pytest.raises(ValueError, match="escape"):
        state_paths.state_path("..", "outside")
    monkeypatch.setenv("SNIPER_STATE_ROOT", "relative")
    with pytest.raises(ValueError, match="absolute"):
        state_paths.state_root()


def test_runtime_environment_defaults_cover_all_mutable_paths(tmp_path):
    env = {"SNIPER_STATE_ROOT": str(tmp_path)}
    result = state_paths.configure_memecoin_state_env(env)
    expected = {
        "DB_PATH": tmp_path / "sniper_tracker.db",
        "MEMECOIN_RUN_LOCK": tmp_path / "data" / "memecoin_runner.lock",
        "MEMECOIN_HEARTBEAT_PATH": tmp_path / "data" / "memecoin_runner.heartbeat",
        "MEMECOIN_FEED_SNAPSHOT_PATH": tmp_path / "data" / "memecoin_feed.json",
        "SNIPER_CLOCK_OFFSET_FILE": tmp_path / "data" / "clock_offset.json",
        "FOMO_TOKEN_STORE": tmp_path / ".fomo_tokens.json",
        "MEMECOIN_KILL_SWITCH_FILE": tmp_path / "data" / "memecoin_kill_switch",
        "MEMECOIN_SOLANA_KEYPAIR": tmp_path / "data" / "solana_keypair.json",
        "MEMECOIN_EVM_KEYPAIR": tmp_path / "data" / "evm_keypair.json",
        "MEMECOIN_PIDFILE": tmp_path / "data" / "memecoin_runner.pid",
        "MEMECOIN_RUNNER_LOG": tmp_path / "logs" / "memecoin_runner.log",
        "MEMECOIN_WATCHDOG_LOG": tmp_path / "logs" / "memecoin_watchdog.log",
    }
    assert {key: Path(result[key]) for key in expected} == expected
    env["DB_PATH"] = "/custom/db"
    state_paths.configure_memecoin_state_env(env)
    assert env["DB_PATH"] == "/custom/db"


def test_modules_resolve_heartbeat_token_kill_and_key_defaults_from_state_root(tmp_path, monkeypatch):
    import sniper.feeds.fomo_feed as feed
    import sniper.services.memecoin_arming as arming
    import sniper.services.memecoin_runner as runner
    import sniper.settings.memecoin as settings
    with monkeypatch.context() as patch:
        patch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
        for key in (
            "MEMECOIN_HEARTBEAT_PATH", "MEMECOIN_FEED_SNAPSHOT_PATH", "FOMO_TOKEN_STORE",
            "MEMECOIN_KILL_SWITCH_FILE", "MEMECOIN_SOLANA_KEYPAIR", "MEMECOIN_EVM_KEYPAIR",
        ):
            patch.delenv(key, raising=False)
        feed = importlib.reload(feed)
        arming = importlib.reload(arming)
        runner = importlib.reload(runner)
        settings = importlib.reload(settings)
        assert Path(runner.HEARTBEAT_PATH) == tmp_path / "data" / "memecoin_runner.heartbeat"
        assert Path(runner.FEED_SNAPSHOT_PATH) == tmp_path / "data" / "memecoin_feed.json"
        assert Path(feed._TOKEN_STORE) == tmp_path / ".fomo_tokens.json"
        assert Path(arming._kill_path()) == tmp_path / "data" / "memecoin_kill_switch"
        assert Path(settings.MEMECOIN_SOLANA_KEYPAIR) == tmp_path / "data" / "solana_keypair.json"
        assert Path(settings.MEMECOIN_EVM_KEYPAIR) == tmp_path / "data" / "evm_keypair.json"
    importlib.reload(settings)
    importlib.reload(feed)
    importlib.reload(arming)
    importlib.reload(runner)


def test_env_files_load_from_state_not_release(tmp_path):
    (tmp_path / ".env").write_text("A=state\n")
    (tmp_path / ".fomo.env").write_text("B=secret\n")
    from sniper.util.envfiles import load_env_files
    env = {}
    assert load_env_files(str(tmp_path), env=env) == [".env", ".fomo.env"]
    assert env == {"A": "state", "B": "secret"}
