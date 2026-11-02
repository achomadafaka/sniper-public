import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "open_ui.sh"


def test_open_ui_launcher_uses_repo_root_and_current_server_module():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    script = SCRIPT.read_text()
    assert 'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"' in script
    assert 'ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"' in script
    assert "select_python()" in script
    assert "import flask" in script
    assert '"$PYTHON_BIN" -m sniper.web.server' in script
    assert "sniper.web.server" in script
    assert "web_server.py" not in script
