from pathlib import Path


def test_watchdog_launches_worker_from_its_pinned_release():
    script = Path("scripts/memecoin_sol_tape_watchdog.sh").read_text()
    worker = Path("scripts/memecoin_sol_tape_worker.py").read_text()

    assert 'CODE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)' in script
    assert 'export SNIPER_STATE_ROOT="$STATE_ROOT"' in script
    # The LAUNCH is still pinned to the release. Only DISCOVERY became release-independent, so a
    # deploy that repoints `current` cannot make the watchdog miss the running worker and start a
    # second one. See tests/test_memecoin_watchdog_lock_inheritance.py for both invariants.
    assert 'LAUNCH=("$PY" -u "$CODE_ROOT/scripts/memecoin_sol_tape_worker.py")' in script
    assert 'MATCH="scripts/memecoin_sol_tape_worker.py"' in script
    assert 'LAUNCH="$PY -u scripts/memecoin_sol_tape_worker.py"' not in script
    assert 'state_root = os.getenv("SNIPER_STATE_ROOT", _REPO_ROOT)' in worker
    assert 'load_dotenv(os.path.join(state_root, ".env"))' in worker
