"""Tests for the PreToolUse secret-file blocker hook."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = str(Path(__file__).resolve().parents[1] / "scripts" / "secret_file_guard.py")


def _run_guard(file_path: str) -> subprocess.CompletedProcess:
    """Run the guard script with a simulated TOOL_INPUT payload."""
    payload = json.dumps({"file_path": file_path})
    return subprocess.run(
        [sys.executable, SCRIPT],
        input=payload,
        capture_output=True,
        text=True,
        timeout=5,
    )


class TestSecretFileGuard:
    """Path-based blocking rules."""

    def test_blocks_dot_env(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/.env")
        assert r.returncode == 1
        assert "BLOCKED" in r.stderr

    def test_blocks_dot_env_production(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/.env.production")
        assert r.returncode == 1

    def test_blocks_dot_env_local(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/.env.local")
        assert r.returncode == 1

    def test_allows_dot_env_example(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/.env.example")
        assert r.returncode == 0

    def test_allows_dot_env_sample(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/.env.sample")
        assert r.returncode == 0

    def test_blocks_pem_file(self):
        r = _run_guard("/home/user/certs/server.pem")
        assert r.returncode == 1
        assert "BLOCKED" in r.stderr

    def test_blocks_key_file(self):
        r = _run_guard("/home/user/ssh/id_rsa.key")
        assert r.returncode == 1

    def test_blocks_secrets_dir(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/secrets/api_keys.json")
        assert r.returncode == 1

    def test_blocks_credentials_dir(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/credentials/gcp.json")
        assert r.returncode == 1

    def test_allows_config_dir(self):
        """config/ is NOT blocked -- contains normal working files."""
        r = _run_guard("/home/bigpapi/Desktop/Sniper/config/ui_live_audit_manifest.yaml")
        assert r.returncode == 0

    def test_allows_normal_python(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/paper_trader.py")
        assert r.returncode == 0

    def test_allows_normal_markdown(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/CLAUDE.md")
        assert r.returncode == 0

    def test_blocks_nested_secrets(self):
        r = _run_guard("/home/bigpapi/project/secrets/nested/deep/token.txt")
        assert r.returncode == 1

    def test_handles_relative_path(self):
        r = _run_guard(".env")
        assert r.returncode == 1

    def test_error_message_names_rule(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/.env")
        assert "Matched rule:" in r.stderr

    def test_handles_missing_file_path(self):
        """If TOOL_INPUT has no file_path, allow through (not our concern)."""
        payload = json.dumps({"content": "some text"})
        r = subprocess.run(
            [sys.executable, SCRIPT],
            input=payload,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert r.returncode == 0

    def test_handles_env_var_input(self):
        """Script should read from TOOL_INPUT env var as fallback."""
        import os
        payload = json.dumps({"file_path": "/home/user/.env"})
        env = {**os.environ, "TOOL_INPUT": payload}
        r = subprocess.run(
            [sys.executable, SCRIPT],
            input="",
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        assert r.returncode == 1
