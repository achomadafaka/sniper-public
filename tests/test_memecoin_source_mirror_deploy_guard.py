from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy_memecoin_runtime.sh"


def _function(name):
    text = SCRIPT.read_text()
    start = text.index(name + "() {", text.index("SWITCHED=0") if name == "rollback" else 0)
    return text[start:text.index("\n}\n", start) + 3]


@pytest.mark.parametrize("pre_switch", [False, True])
def test_incompatible_rollback_keeps_candidate_owner_config_and_recovery_files(tmp_path, pre_switch):
    old, candidate = tmp_path / "old", tmp_path / "candidate"
    old.mkdir(); candidate.mkdir()
    current = tmp_path / "current"
    current.symlink_to(candidate)
    backup = tmp_path / "backup"; backup.write_text("retain recovery")
    events = tmp_path / "events"
    function = "pre_switch_rollover_rollback" if pre_switch else "rollback"
    harness = f'''set -euo pipefail
SUCCESS=0; SWITCHED=1; OLD_RELEASE={old}; RELEASE={candidate}; CURRENT={current}
PROVIDER_ENV=stage; SHYFT_RUNTIME=1; PROVIDER_RECOVERY_REQUIRED=0; PROVIDER_CANDIDATE_LAUNCHED=1
SNIPER_DEPLOY_NO_PROCESS=0; ROLLOVER_OLD_RUNNER_STOPPED=1
ROLLOVER_ENV_BACKUP={backup}; CLUSTER_WORKER_STARTED=0
source_mirror_rollback_safe() {{ echo guard >> {events}; return 1; }}
stop_provider_processes() {{ echo candidate_stop >> {events}; }}
restore_lifi_env() {{ echo lifi_restore >> {events}; }}
restore_rollover_env() {{ echo env_restore >> {events}; }}
restore_provider_env() {{ echo provider_restore >> {events}; }}
restart_old_rollover_runner() {{ echo old_start >> {events}; }}
{_function(function)}
false || {function}
'''
    result = subprocess.run(["bash", "-c", harness], text=True, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert events.read_text().splitlines() == ["guard"]
    assert current.resolve() == candidate
    assert backup.exists()
    assert "source mirror" in result.stderr.lower() and "preserved" in result.stderr


def test_funded_validation_runs_the_actual_candidate_script(tmp_path):
    recorder = tmp_path / "python"
    log = tmp_path / "args"
    recorder.write_text(f"#!{sys.executable}\nimport sys\nopen({str(log)!r},'w').write(sys.argv[1])\n")
    recorder.chmod(0o755)
    result = subprocess.run(["bash", "-c", f'''set -euo pipefail
PY={recorder}; DEFAULT_SOURCE={tmp_path / 'current'}; RELEASE={tmp_path / 'candidate'}
STATE_ROOT={tmp_path}; DB={tmp_path / 'scratch.db'}
fail() {{ echo "$*" >&2; return 1; }}
{_function('validate_funded_contracts')}
validate_funded_contracts
'''], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert log.read_text() == str(tmp_path / "candidate/scripts/validate_memecoin_funded_contracts.py")
