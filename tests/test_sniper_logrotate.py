import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_user_logrotate_config_is_repo_relative_and_parses(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / ".logs").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "logs" / "web.log").write_text("request\n")
    (tmp_path / "logs" / "memecoin_runner.log").write_text("runner\n")
    (tmp_path / ".logs" / "hl_copy.log").write_text("copy\n")
    env = {**os.environ, "SNIPER_LOG_ROOT": str(tmp_path)}

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "sniper_logrotate.sh"), "--debug"],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    rendered = (tmp_path / "data" / "logrotate" / "sniper.conf").read_text()
    assert str(tmp_path / "logs" / "web.log") in rendered
    assert str(tmp_path / "logs" / "memecoin_runner.log") in rendered
    assert str(tmp_path / ".logs" / "hl_*.log") in rendered
    assert "copytruncate" in rendered and "rotate 4" in rendered and "size 50M" in rendered


def test_pinned_runtime_symlink_defaults_to_shared_state_root(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "logs" / "web.log").write_text("request\n")
    runtime = tmp_path / ".runtime" / "memecoin"
    runtime.mkdir(parents=True)
    (runtime / "current").symlink_to(ROOT, target_is_directory=True)

    env = os.environ.copy()
    env.pop("SNIPER_LOG_ROOT", None)
    env.pop("SNIPER_STATE_ROOT", None)
    result = subprocess.run(
        ["bash", str(runtime / "current" / "scripts" / "sniper_logrotate.sh"), "--debug"],
        cwd=tmp_path, env=env, text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    rendered = (tmp_path / "data" / "logrotate" / "sniper.conf").read_text()
    assert str(tmp_path / "logs" / "web.log") in rendered
