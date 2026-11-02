"""Blocker 5 — one env loader, both files. The runner loaded only .fomo.env, the smoke only .env, so
arm creds in the wrong file strand the arm silently (smoke passes, runner boots paper-only). load_env_files
loads BOTH; setdefault means the real shell env wins and an earlier file wins over a later one."""
from sniper.util.envfiles import load_env_files


def test_loads_both_files(tmp_path):
    (tmp_path / ".env").write_text("MEMECOIN_REAL_ENABLED=1\nMEMECOIN_SOLANA_RPC_URL=https://rpc\n")
    (tmp_path / ".fomo.env").write_text("FOMO_REFRESH_TOKEN=abc\n")
    env = {}
    loaded = load_env_files(str(tmp_path), env=env)
    assert set(loaded) == {".env", ".fomo.env"}
    assert env["MEMECOIN_REAL_ENABLED"] == "1"
    assert env["MEMECOIN_SOLANA_RPC_URL"] == "https://rpc"
    assert env["FOMO_REFRESH_TOKEN"] == "abc"


def test_existing_value_wins_setdefault(tmp_path):
    (tmp_path / ".env").write_text("K=fromfile\n")
    env = {"K": "fromshell"}
    load_env_files(str(tmp_path), env=env)
    assert env["K"] == "fromshell"                     # real shell env is never overridden


def test_earlier_file_wins_on_conflict(tmp_path):
    (tmp_path / ".env").write_text("K=fromdotenv\n")
    (tmp_path / ".fomo.env").write_text("K=fromfomo\n")
    env = {}
    load_env_files(str(tmp_path), names=(".env", ".fomo.env"), env=env)
    assert env["K"] == "fromdotenv"                    # first file loaded wins


def test_ignores_comments_and_blank_and_quotes(tmp_path):
    (tmp_path / ".env").write_text("# a comment\n\nK='quoted'\nJ=\"dq\"\n")
    env = {}
    load_env_files(str(tmp_path), names=(".env",), env=env)
    assert env["K"] == "quoted" and env["J"] == "dq"


def test_missing_files_are_skipped(tmp_path):
    env = {}
    assert load_env_files(str(tmp_path), env=env) == []   # neither exists -> nothing loaded, no error


def test_web_runtime_loader_uses_state_root(tmp_path, monkeypatch):
    (tmp_path / ".fomo.env").write_text("WEB_STATE_ROOT_SENTINEL=loaded\n")
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("WEB_STATE_ROOT_SENTINEL", raising=False)

    from sniper.web import server

    assert server._load_runtime_env() == [".fomo.env"]
    assert server.os.environ["WEB_STATE_ROOT_SENTINEL"] == "loaded"
