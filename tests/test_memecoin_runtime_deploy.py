import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import fcntl
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_memecoin_runtime.sh"
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_deploy_closes_its_lock_before_each_watchdog_launch():
    calls = [
        line.strip() for line in SCRIPT.read_text().splitlines()
        if line.lstrip().startswith("SNIPER_STATE_ROOT=")
        and "memecoin_watchdog.sh" in line and "bash" in line
    ]

    assert len(calls) == 3
    assert all("9>&-" in line for line in calls)
    assert all("7>&-" in line for line in calls), 'runner must not inherit the RH cutover lock'


def test_deploy_holds_the_watchdog_lock_through_cutover():
    text = SCRIPT.read_text()
    calls = [
        line.strip() for line in text.splitlines()
        if line.lstrip().startswith("SNIPER_STATE_ROOT=")
        and "memecoin_watchdog.sh" in line and "bash" in line
    ]

    assert 'flock -w 120 8' in text
    assert all("MEMECOIN_WATCHDOG_LOCK_HELD=1" in line for line in calls)
    assert all("8>&-" in line for line in calls)


def _run(args, cwd, env=None, check=True):
    return subprocess.run(
        args, cwd=cwd, env=env, text=True, capture_output=True, check=check,
    )


_HOST_ONLY_ENV = ("MEMECOIN_WATCHDOG_LOCK", "MEMECOIN_PIDFILE", "MEMECOIN_DEPLOY_TEST_FILES_FILE")


def _setup(tmp_path):
    origin = tmp_path / "origin.git"
    source = tmp_path / "source"
    _run(["git", "init", "--bare", "-q", str(origin)], cwd=tmp_path)
    source.mkdir(); _run(["git", "init", "-q", "-b", "main"], cwd=source)
    _run(["git", "config", "user.email", "test@example.com"], cwd=source)
    _run(["git", "config", "user.name", "Test"], cwd=source)
    (source / "tracked.txt").write_text("one\n")
    (source / "scripts").mkdir()
    shutil.copy2(
        REPO_ROOT / "scripts" / "validate_memecoin_funded_contracts.py",
        source / "scripts" / "validate_memecoin_funded_contracts.py",
    )
    (source / "sniper" / "services").mkdir(parents=True)
    (source / "sniper" / "__init__.py").write_text("")
    (source / "sniper" / "services" / "__init__.py").write_text("")
    shutil.copy2(
        REPO_ROOT / "sniper" / "services" / "memecoin_funded_reconcile.py",
        source / "sniper" / "services" / "memecoin_funded_reconcile.py",
    )
    lifi = source / "sniper" / "execution"
    lifi.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "sniper/execution/lifi_4663_manifest.json", lifi)
    _run(["git", "add", "."], cwd=source); _run(["git", "commit", "-qm", "one"], cwd=source)
    _run(["git", "remote", "add", "origin", str(origin)], cwd=source)
    _run(["git", "push", "-qu", "origin", "main"], cwd=source)
    sha = _run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
    state = tmp_path / "state"; (state / "data" / "backups").mkdir(parents=True)
    (state / "data" / ".sniper_deploy_test_fixture").write_text("isolated\n")
    (state / "logs").mkdir(); (state / ".fomo.env").write_text("MEMECOIN_REAL_ENABLED=0\n")
    (state / ".env").write_text(
        "MEMECOIN_REAL_TP_PCT=60\n"
        "MEMECOIN_REAL_DERISK_PCT=0\n"
        "MEMECOIN_SLOT_LANE_RABBIT_TAPE=0\n"  # The provider-stage fixture proves nine streams.
    )
    db = state / "sniper_tracker.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE memecoin_pending_entries(id INTEGER,status TEXT)")
    conn.commit(); conn.close()
    time.sleep(0.01)
    (state / "data" / "backups" / "sniper_tracker-test.db").write_bytes(db.read_bytes())
    runtime = tmp_path / "runtime"
    # A REAL PATH EXPORTED IN THE CALLING SHELL NEVER REACHES THE HARNESS (2026-10-15, Alpha). The
    # script defaults these to the tmp state root, but an inherited value wins: a harness flock on
    # the live watchdog lock could hold off the real watchdog, and the deploy shell exports the test
    # manifest. Popped, not overridden, so the script's own tmp default applies.
    env = {k: v for k, v in os.environ.items() if k not in _HOST_ONLY_ENV}
    env.update({
        "SNIPER_SOURCE_ROOT": str(source), "SNIPER_STATE_ROOT": str(state),
        "DB_PATH": str(db),
        "FOMO_TOKEN_STORE": str(state / ".fomo_tokens.json"),
        "SNIPER_RUNTIME_ROOT": str(runtime), "SNIPER_DEPLOY_SKIP_VALIDATION": "1",
        "SNIPER_DEPLOY_VERIFY_COMMAND": "true", "SNIPER_DEPLOY_NO_PROCESS": "1",
        "SNIPER_DEPLOY_SKIP_TPU_BUILD": "1",   # the real sidecar build is exercised separately
    })
    return source, state, runtime, env, sha


def test_an_exported_host_path_never_reaches_the_harness(tmp_path, monkeypatch):
    live = str(REPO_ROOT / "data" / "memecoin_watchdog.lock")
    keys = ("MEMECOIN_WATCHDOG_LOCK", "MEMECOIN_PIDFILE", "MEMECOIN_DEPLOY_TEST_FILES_FILE")
    for key in keys:
        monkeypatch.setenv(key, live)
    *_rest, env, _sha = _setup(tmp_path)
    assert [key for key in keys if key in env] == [] and live not in env.values()


def test_live_deploy_rejects_validation_skip(tmp_path):
    source, _state, _runtime, env, sha = _setup(tmp_path)
    result = _run(["bash", str(SCRIPT), sha], source,
                  env | {"SNIPER_DEPLOY_NO_PROCESS": "0"}, False)
    assert result.returncode != 0
    assert "validation override is only for isolated no-process tests" in result.stderr


def test_live_deploy_rejects_verifier_override(tmp_path):
    source, _state, _runtime, env, sha = _setup(tmp_path)
    result = _run(["bash", str(SCRIPT), sha], source,
                  env | {"SNIPER_DEPLOY_NO_PROCESS": "0",
                         "SNIPER_DEPLOY_SKIP_VALIDATION": "0"}, False)
    assert result.returncode != 0
    assert "validation override is only for isolated no-process tests" in result.stderr


def test_deploy_uses_trusted_structural_checker_even_if_candidate_checker_lies(tmp_path):
    source, _state, _runtime, env, _sha = _setup(tmp_path)
    (source / "sniper" / "services" / "bad_writer.py").write_text(
        'def write(conn):\n    conn.execute("INSERT INTO block_times VALUES (1,2,3,4)")\n')
    (source / "scripts" / "check_positional_block_times_writes.py").write_text(
        'raise SystemExit(0)\n')
    _run(["git", "add", "."], source)
    _run(["git", "commit", "-qm", "bad candidate checker"], source)
    _run(["git", "push", "-q", "origin", "main"], source)
    sha = _run(["git", "rev-parse", "HEAD"], source).stdout.strip()
    python_wrapper = tmp_path / "python-wrapper"
    python_wrapper.write_text(
        '#!/usr/bin/env bash\n'
        'case "${1:-}" in *validate_schema_refs.py) exit 0;; esac\n'
        'exec "$REAL_PYTHON" "$@"\n')
    python_wrapper.chmod(0o755)
    result = _run(["bash", str(SCRIPT), sha], source,
                  env | {"SNIPER_DEPLOY_SKIP_VALIDATION": "0",
                         "SNIPER_PYTHON": str(python_wrapper),
                         "REAL_PYTHON": os.environ.get("PYTHON", "python3")}, False)
    assert result.returncode != 0
    assert "release writes block_times positionally" in result.stderr


def test_deploy_uses_trusted_manifest_verifier_even_if_candidate_verifier_lies(tmp_path):
    source, _state, _runtime, env, _sha = _setup(tmp_path)
    (source / "scripts" / "memecoin_runtime_verify.py").write_text('raise SystemExit(0)\n')
    _run(["git", "add", "."], source)
    _run(["git", "commit", "-qm", "bad candidate verifier"], source)
    _run(["git", "push", "-q", "origin", "main"], source)
    sha = _run(["git", "rev-parse", "HEAD"], source).stdout.strip()
    result = _run(["bash", str(SCRIPT), sha, "--dry-run"], source,
                  {k: v for k, v in env.items() if k != "SNIPER_DEPLOY_VERIFY_COMMAND"}, False)
    assert result.returncode != 0
    assert "release verification failed" in result.stderr


def test_deploy_refuses_while_another_memecoin_deploy_holds_the_lock(tmp_path):
    _source, state, _runtime, env, sha = _setup(tmp_path)
    lock_path = state / "data" / "memecoin_deploy.lock"
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run(["bash", str(SCRIPT), sha], tmp_path, env, False)

    assert result.returncode != 0
    assert "another memecoin deploy is running" in result.stderr
    assert not (state / "data" / "memecoin_deploy.json").exists()


# A fake builder invoked as: bash -c "$_FAKE_TPU_BUILD" -- <release> <artifact-root> <sha>
# so $1=release, $2=artifact-root, $3=sha. Writes a fake binary + a real metadata.json.
_FAKE_TPU_BUILD = (
    "d=\"$2/$3\"; mkdir -p \"$d\"; printf BIN > \"$d/memecoin-tpu-sidecar\"; "
    "chmod 0555 \"$d/memecoin-tpu-sidecar\"; "
    "s=$(sha256sum \"$d/memecoin-tpu-sidecar\" | cut -d\" \" -f1); "
    "l=$(sha256sum \"$1/rust/solana-tpu-sidecar/Cargo.lock\" | cut -d\" \" -f1); "
    "printf '{\"path\":\"%s\",\"sha256\":\"%s\",\"source_sha\":\"%s\","
    "\"cargo_lock_sha256\":\"%s\",\"target\":\"x86_64-unknown-linux-gnu\","
    "\"build_image_digest\":\"sha256:abc\"}' "
    "\"$d/memecoin-tpu-sidecar\" \"$s\" \"$3\" \"$l\" > \"$d/metadata.json\""
)


def test_deploy_records_sidecar_object_in_manifest(tmp_path):
    source, state, runtime, env, sha = _setup(tmp_path)
    # the release tree needs a Cargo.lock for the fake builder to hash
    (source / "rust" / "solana-tpu-sidecar").mkdir(parents=True)
    (source / "rust" / "solana-tpu-sidecar" / "Cargo.lock").write_text("LOCK\n")
    _run(["git", "add", "."], cwd=source)
    _run(["git", "commit", "-qm", "lock"], cwd=source)
    _run(["git", "push", "-q", "origin", "main"], cwd=source)
    sha = _run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
    build_env = {k: v for k, v in env.items() if k != "SNIPER_DEPLOY_SKIP_TPU_BUILD"}
    build_env["SNIPER_DEPLOY_TPU_BUILD_COMMAND"] = _FAKE_TPU_BUILD
    res = subprocess.run(["bash", str(SCRIPT), sha], env=build_env, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr
    manifest = json.loads((state / "data" / "memecoin_deploy.json").read_text())
    assert manifest["sidecar"]["source_sha"] == sha
    assert manifest["sidecar"]["target"] == "x86_64-unknown-linux-gnu"
    value = json.loads((source / "sniper/execution/lifi_4663_manifest.json").read_text())
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    assert manifest["lifi_manifest_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    # the artifact landed under .runtime/memecoin/artifacts/<sha>/
    assert (runtime / "artifacts" / sha / "memecoin-tpu-sidecar").is_file()


def test_sidecar_build_failure_aborts_before_switch(tmp_path):
    source, state, runtime, env, sha = _setup(tmp_path)
    fail_env = {k: v for k, v in env.items() if k != "SNIPER_DEPLOY_SKIP_TPU_BUILD"}
    fail_env["SNIPER_DEPLOY_TPU_BUILD_COMMAND"] = "false"   # the build fails
    res = subprocess.run(["bash", str(SCRIPT), sha], env=fail_env, text=True, capture_output=True)
    assert res.returncode != 0
    assert "TPU sidecar build failed" in res.stderr
    # nothing was switched: no deploy manifest exists, the old runner was never stopped
    assert not (state / "data" / "memecoin_deploy.json").exists()


def test_deploy_reactivates_lifi_after_it_loads_the_state_environment(tmp_path):
    source, state, runtime, env, _sha = _setup(tmp_path)
    manifest = json.loads((source / "sniper" / "execution" / "lifi_4663_manifest.json").read_text())
    manifest_hash = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (state / ".env").write_text(
        "MEMECOIN_LIFI_MODE=authenticated_funded\n"
        f"MEMECOIN_LIFI_ACTIVE_MANIFEST_HASH={manifest_hash}\n"
    )
    (source / "sniper" / "util").mkdir()
    (source / "sniper" / "util" / "__init__.py").write_text("")
    (source / "sniper" / "util" / "envfiles.py").write_text(
        "import os\n"
        "def load_env_files(root):\n"
        "    for line in open(os.path.join(root, '.env')):\n"
        "        key, value = line.strip().split('=', 1)\n"
        "        os.environ.setdefault(key, value)\n"
    )
    (source / "sniper" / "execution" / "__init__.py").write_text("")
    (source / "sniper" / "execution" / "evm_wallet.py").write_text(
        "from types import SimpleNamespace\n"
        "def load_keypair(_value): return SimpleNamespace(address='0x' + '1' * 40)\n"
    )
    (source / "sniper" / "persistence").mkdir()
    (source / "sniper" / "persistence" / "__init__.py").write_text("")
    (source / "sniper" / "persistence" / "db_core.py").write_text(
        "from contextlib import contextmanager\n"
        "@contextmanager\n"
        "def get_connection():\n"
        "    yield object()\n"
    )
    (source / "sniper" / "settings").mkdir()
    (source / "sniper" / "settings" / "__init__.py").write_text("")
    (source / "sniper" / "settings" / "memecoin.py").write_text(
        "import os\n"
        "MEMECOIN_EVM_KEYPAIR = 'key'\n"
        "def memecoin_lifi_mode(): return os.getenv('MEMECOIN_LIFI_MODE', 'disabled')\n"
        "def load_lifi_manifest(): return {}, os.environ['MEMECOIN_LIFI_ACTIVE_MANIFEST_HASH']\n"
        "def validate_lifi_config(**_kwargs): return {'canary_lifecycle_id': 'canary'}\n"
    )
    (source / "scripts" / "memecoin_lifi_activate.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['SNIPER_STATE_ROOT']).joinpath('lifi-activation.txt').write_text(' '.join(sys.argv[1:]))\n"
    )
    _run(["git", "add", "."], source)
    _run(["git", "commit", "-qm", "lifi deploy fixtures"], source)
    _run(["git", "push", "-q", "origin", "main"], source)
    sha = _run(["git", "rev-parse", "HEAD"], source).stdout.strip()

    # THIS TEST OWNS THE MODE. Its fixture `load_env_files` uses `os.environ.setdefault`, which is
    # the real script's semantic: a value already in the environment WINS over the state .env. The
    # suite pins MEMECOIN_LIFI_MODE=disabled (conftest, because the operator's .env leaks
    # authenticated_funded into unrelated context-build tests), and that pin is inherited by this
    # deploy subprocess — so the state .env below could never take effect and LI.FI was never
    # reactivated. Drop the key so the subprocess starts without it and the fixture's setdefault
    # does what the deploy really does.
    verify_env = {k: v for k, v in env.items() if k != "MEMECOIN_LIFI_MODE"} | {
        "SNIPER_DEPLOY_VERIFY_COMMAND": (
            "if [ -f \"$2/lifi-activation.txt\" ] && "
            "[ \"$3\" != \"$2/data/memecoin_deploy.json\" ]; then exit 7; fi"
        ),
    }
    _run(["bash", str(SCRIPT), sha], source, verify_env)

    assert (runtime / "current").resolve().name == sha
    assert f"--release-sha {sha}" in (state / "lifi-activation.txt").read_text()


def _commit(source, text):
    (source / "tracked.txt").write_text(text)
    _run(["git", "add", "tracked.txt"], cwd=source)
    _run(["git", "commit", "-qm", text.strip()], cwd=source)
    _run(["git", "push", "-q", "origin", "main"], cwd=source)
    return _run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()


@pytest.mark.parametrize("configured", [None, "10"])
def test_deploy_caps_validation_workers(tmp_path, configured):
    source, _state, _runtime, env, _sha = _setup(tmp_path)
    (source / "tests").mkdir()
    (source / "tests" / "test_memecoin_stub.py").write_text(
        "def test_stub():\n    assert True\n"
    )
    _run(["git", "add", "tests/test_memecoin_stub.py"], cwd=source)
    _run(["git", "commit", "-qm", "add test stub"], cwd=source)
    _run(["git", "push", "-q", "origin", "main"], cwd=source)
    sha = _run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()

    worker_log = tmp_path / "worker-count.txt"
    python_wrapper = tmp_path / "python-wrapper"
    python_wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "if [ \"${1:-}\" = -m ] && [ \"${2:-}\" = pytest ]; then\n"
        "  printf '%s\\n' \"$@\" > \"$WORKER_LOG\"\n"
        "  exit 0\n"
        "fi\n"
        "case \"${1:-}\" in\n"
        "  *validate_schema_refs.py|*validate_memecoin_funded_contracts.py|*check_positional_block_times_writes.py) exit 0 ;;\n"
        "esac\n"
        "if [ \"${1:-}\" = -m ] && [ \"${2:-}\" = compileall ]; then exit 0; fi\n"
        "exec \"$REAL_PYTHON\" \"$@\"\n"
    )
    python_wrapper.chmod(0o755)
    run_env = env | {
        "SNIPER_DEPLOY_SKIP_VALIDATION": "0",
        "SNIPER_PYTHON": str(python_wrapper),
        "REAL_PYTHON": os.environ.get("PYTHON", "python3"),
        "WORKER_LOG": str(worker_log),
    }
    if configured is not None:
        run_env["MEMECOIN_DEPLOY_TEST_WORKERS"] = configured

    isolated_script = tmp_path / "deploy.sh"
    isolated_script.write_text(SCRIPT.read_text().replace(
        '"$HOME/.cache/codex-pytest-full.lock"',
        str(tmp_path / "pytest-full.lock"),
    ))
    result = _run(["bash", str(isolated_script), sha, "--dry-run"], source, run_env, False)

    assert result.returncode == 0, result.stderr
    args = worker_log.read_text().splitlines()
    assert args[args.index("-n") + 1] == "2"


@pytest.mark.parametrize("comparable_live_sha", [True, False])
def test_deploy_validation_selects_connected_tests_and_holds_host_lock(tmp_path, comparable_live_sha):
    source, _state, runtime, env, old_sha = _setup(tmp_path)
    (source / "tests").mkdir()
    for name in ("memecoin_real_runner", "memecoin_shared", "hyperliquid_shared",
                 "polymarket_shared", "shared_gate"):
        (source / "tests" / f"test_{name}.py").write_text(
            "from sniper.services import shared_gate\n"
        )
    # The script's own core list, as bash expands it (it grew the tree-walking tests 2026-10-15).
    from test_memecoin_deploy_core_treewalk import _core
    core = set(_core())
    for path in core:
        if not (source / path).exists():
            (source / path).write_text("def test_ok():\n    assert True\n")
    (source / "sniper/services/shared_gate.py").write_text("VALUE = 1\n")
    _run(["git", "add", "."], source)
    _run(["git", "commit", "-qm", "shared module and domain tests"], source)
    _run(["git", "push", "-q", "origin", "main"], source)
    sha = _run(["git", "rev-parse", "HEAD"], source).stdout.strip()
    if comparable_live_sha:
        old_release = runtime / "releases" / old_sha
        old_release.mkdir(parents=True)
        (runtime / "current").symlink_to(old_release)

    lock_path = tmp_path / "pytest-full.lock"
    args_log = tmp_path / "pytest-args.json"
    python_wrapper = tmp_path / "python-wrapper"
    python_wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import fcntl, json, os, sys\n"
        "if sys.argv[1:3] == ['-m', 'pytest']:\n"
        "    with open(os.environ['TEST_LOCK'], 'a') as lock:\n"
        "        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "        except BlockingIOError: held = True\n"
        "        else: held = False\n"
        "    with open(os.environ['ARGS_LOG'], 'w') as log:\n"
        "        json.dump({'args': sys.argv[1:], 'lock_held': held}, log)\n"
        "    sys.exit(1)\n"
        "if sys.argv[1].endswith('validate_schema_refs.py'): sys.exit(0)\n"
        "os.execvp('python3', ['python3', *sys.argv[1:]])\n"
    )
    python_wrapper.chmod(0o755)
    isolated_script = tmp_path / "deploy.sh"
    isolated_script.write_text(SCRIPT.read_text().replace(
        '"$HOME/.cache/codex-pytest-full.lock"', str(lock_path),
    ))
    # Structural validation must still use the real trusted checker.
    isolated_script.write_text(isolated_script.read_text().replace(
        'DEFAULT_SOURCE="$(cd "$(dirname "$SCRIPT")/.." && pwd -P)"',
        f'DEFAULT_SOURCE="{REPO_ROOT}"',
    ))
    result = _run(["bash", str(isolated_script), sha], source, env | {
        "SNIPER_DEPLOY_SKIP_VALIDATION": "0", "SNIPER_PYTHON": str(python_wrapper),
        "TEST_LOCK": str(lock_path), "ARGS_LOG": str(args_log),
    }, False)

    assert result.returncode != 0
    assert "memecoin suite failed" in result.stderr
    recorded = json.loads(args_log.read_text())
    expected = core | {"tests/test_memecoin_shared.py"}
    if comparable_live_sha:
        expected |= {"tests/test_hyperliquid_shared.py", "tests/test_polymarket_shared.py",
                     "tests/test_shared_gate.py"}
    assert set(arg for arg in recorded["args"] if arg.startswith("tests/")) == expected
    assert recorded["lock_held"]
    if comparable_live_sha:
        assert (runtime / "current").resolve().name == old_sha
    else:
        assert not (runtime / "current").exists()


def test_rejects_nonfull_unreachable_dirty_and_pending(tmp_path):
    source, _state, _runtime, env, sha = _setup(tmp_path)
    assert _run(["bash", str(SCRIPT), sha[:8], "--dry-run"], source, env, False).returncode != 0
    assert _run(["bash", str(SCRIPT), "f" * 40, "--dry-run"], source, env, False).returncode != 0
    (source / "tracked.txt").write_text("dirty\n")
    assert _run(["bash", str(SCRIPT), sha, "--dry-run"], source, env, False).returncode != 0
    _run(["git", "checkout", "--", "tracked.txt"], cwd=source)
    conn = sqlite3.connect(env["SNIPER_STATE_ROOT"] + "/sniper_tracker.db")
    conn.execute("INSERT INTO memecoin_pending_entries(status) VALUES ('reserved')")
    conn.commit(); conn.close()
    assert _run(["bash", str(SCRIPT), sha, "--dry-run"], source, env, False).returncode != 0


@pytest.mark.parametrize("case", ["default", "local", "no_remote", "missing", "dirty", "short"])
def test_local_commit_opt_in_preserves_source_guards(tmp_path, case):
    source, state, runtime, env, _sha = _setup(tmp_path)
    (source / "tracked.txt").write_text("local reviewed change\n")
    _run(["git", "commit", "-qam", "local only"], source)
    sha = _run(["git", "rev-parse", "HEAD"], source).stdout.strip()
    if case == "no_remote":
        _run(["git", "update-ref", "-d", "refs/remotes/origin/main"], source)
    elif case == "missing":
        sha = "f" * 40
    elif case == "dirty":
        (source / "tracked.txt").write_text("unreviewed tracked change\n")
    elif case == "short":
        sha = sha[:8]
    args = ["bash", str(SCRIPT), sha, "--dry-run"]
    if case != "default":
        args.append("--allow-local-commit")
    result = _run(args, source, env, False)
    if case in {"local", "no_remote"}:
        assert result.returncode == 0, result.stderr
        assert "dry-run OK" in result.stdout
    else:
        assert result.returncode != 0
        expected = {"default": "not reachable from origin/main", "missing": "commit does not exist",
                    "dirty": "tracked changes", "short": "full lowercase"}
        assert expected[case] in result.stderr
    assert not (runtime / "current").exists()
    assert not (state / "data" / "memecoin_deploy.json").exists()


def test_deploy_allows_only_an_unsigned_reservation_through_preflight(tmp_path):
    source, state, _runtime, env, sha = _setup(tmp_path)
    conn = sqlite3.connect(state / "sniper_tracker.db")
    for name, ddl in (
        ("tx_sig", "TEXT"), ("broadcast_at", "TEXT"), ("nonce", "INTEGER"),
        ("signed_payload_b64", "TEXT"), ("signed_payload_sha256", "TEXT"),
        ("submission_blockhash", "TEXT"), ("submission_last_valid_height", "INTEGER"),
        ("submission_backend", "TEXT"), ("submission_bound_at", "TEXT"),
        ("cleanup_tx_sig", "TEXT"), ("cleanup_nonce", "INTEGER"),
        ("cleanup_amount_atomic", "TEXT"), ("cleanup_status", "TEXT"),
        ("cleanup_signed_at", "TEXT"), ("cleanup_signed_payload_sha256", "TEXT"),
    ):
        conn.execute(f"ALTER TABLE memecoin_pending_entries ADD COLUMN {name} {ddl}")
    conn.execute("INSERT INTO memecoin_pending_entries(status) VALUES ('reserved')")
    conn.commit(); conn.close()

    result = _run(["bash", str(SCRIPT), sha, "--dry-run"], source, env, False)

    assert result.returncode == 0, result.stderr
    assert "pending=1" in result.stdout


def test_deploy_blocks_a_reservation_with_a_submission_identity(tmp_path):
    source, state, _runtime, env, sha = _setup(tmp_path)
    conn = sqlite3.connect(state / "sniper_tracker.db")
    for name, ddl in (
        ("tx_sig", "TEXT"), ("broadcast_at", "TEXT"), ("nonce", "INTEGER"),
        ("signed_payload_b64", "TEXT"), ("signed_payload_sha256", "TEXT"),
        ("submission_blockhash", "TEXT"), ("submission_last_valid_height", "INTEGER"),
        ("submission_backend", "TEXT"), ("submission_bound_at", "TEXT"),
        ("cleanup_tx_sig", "TEXT"), ("cleanup_nonce", "INTEGER"),
        ("cleanup_amount_atomic", "TEXT"), ("cleanup_status", "TEXT"),
        ("cleanup_signed_at", "TEXT"), ("cleanup_signed_payload_sha256", "TEXT"),
    ):
        conn.execute(f"ALTER TABLE memecoin_pending_entries ADD COLUMN {name} {ddl}")
    conn.execute("INSERT INTO memecoin_pending_entries(status,tx_sig) VALUES ('reserved','0xbound')")
    conn.commit(); conn.close()

    result = _run(["bash", str(SCRIPT), sha, "--dry-run"], source, env, False)

    assert result.returncode != 0
    assert "signed or ambiguous reservation(s) block deploy" in result.stderr


def test_normal_deploy_rechecks_signed_reservations_before_switch(tmp_path):
    source, state, runtime, env, sha = _setup(tmp_path)
    conn = sqlite3.connect(state / "sniper_tracker.db")
    for name, ddl in (
        ("tx_sig", "TEXT"), ("broadcast_at", "TEXT"), ("nonce", "INTEGER"),
        ("signed_payload_b64", "TEXT"), ("signed_payload_sha256", "TEXT"),
        ("submission_blockhash", "TEXT"), ("submission_last_valid_height", "INTEGER"),
        ("submission_backend", "TEXT"), ("submission_bound_at", "TEXT"),
        ("cleanup_tx_sig", "TEXT"), ("cleanup_nonce", "INTEGER"),
        ("cleanup_amount_atomic", "TEXT"), ("cleanup_status", "TEXT"),
        ("cleanup_signed_at", "TEXT"), ("cleanup_signed_payload_sha256", "TEXT"),
    ):
        conn.execute(f"ALTER TABLE memecoin_pending_entries ADD COLUMN {name} {ddl}")
    conn.commit()
    conn.close()
    injector = tmp_path / "inject_signed.py"
    injector.write_text(
        "import os, sqlite3\n"
        "conn = sqlite3.connect(os.environ['DB_PATH'])\n"
        "conn.execute(\"INSERT INTO memecoin_pending_entries(status,tx_sig) "
        "VALUES ('reserved','0xlate')\")\n"
        "conn.commit()\n"
    )
    late_env = env | {"SNIPER_DEPLOY_VERIFY_COMMAND": f"python3 {injector}"}

    result = _run(["bash", str(SCRIPT), sha], source, late_env, False)

    assert result.returncode != 0
    assert "signed or ambiguous reservation(s) block cutover" in result.stderr
    assert not (runtime / "current").exists()


@pytest.mark.parametrize("late_position", [False, True])
def test_require_flat_checks_after_candidate_validation(tmp_path, late_position):
    source, state, runtime, env, sha = _setup(tmp_path)
    with sqlite3.connect(state / "sniper_tracker.db") as conn:
        conn.execute("CREATE TABLE memecoin_positions(execution_mode TEXT,is_open INTEGER)")
    injector = tmp_path / "validate.py"
    injector.write_text(
        "import os,sqlite3\n"
        "with sqlite3.connect(os.environ['DB_PATH']) as c:\n"
        + (" c.execute(\"INSERT INTO memecoin_positions VALUES('real',1)\")\n"
           if late_position else " pass\n"))
    result = _run(["bash", str(SCRIPT), sha, "--require-flat"], source,
                  env | {"SNIPER_DEPLOY_VERIFY_COMMAND": f"python3 {injector}"}, False)
    if late_position:
        assert result.returncode != 0
        assert "funded book is not flat" in result.stderr
        assert not (runtime / "current").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert (runtime / "current").resolve().name == sha


def test_deploy_switches_manifest_and_same_sha_is_idempotent(tmp_path):
    source, state, runtime, env, sha = _setup(tmp_path)
    result = _run(["bash", str(SCRIPT), sha], source, env)
    current = (runtime / "current").resolve()
    manifest = json.loads((state / "data" / "memecoin_deploy.json").read_text())
    assert current == runtime / "releases" / sha
    assert manifest["sha"] == sha and Path(manifest["release_path"]) == current
    again = _run(["bash", str(SCRIPT), sha], source, env)
    assert "already current" in again.stdout
    assert (runtime / "current").resolve() == current
    assert "deployed" in result.stdout


def test_deploy_carries_rotated_token_store_to_new_state_root(tmp_path):
    source, state, _runtime, env, sha = _setup(tmp_path)
    token = source / ".fomo_tokens.json"
    token.write_text('{"access":"new","refresh":"rotated"}\n')
    token.chmod(0o600)

    _run(["bash", str(SCRIPT), sha], source, env)

    carried = state / ".fomo_tokens.json"
    assert json.loads(carried.read_text())["refresh"] == "rotated"
    assert carried.stat().st_mode & 0o777 == 0o600


def test_dry_run_validates_without_creating_gigabyte_backup(tmp_path):
    source, state, _runtime, env, sha = _setup(tmp_path)
    shutil.rmtree(state / "data" / "backups")

    result = _run(["bash", str(SCRIPT), sha, "--dry-run"], source, env)

    backups = list((state / "data" / "backups").glob("sniper_tracker-predeploy-*.db"))
    assert backups == []
    assert "backup=not-created" in result.stdout


def test_real_deploy_skips_backup_by_default(tmp_path):
    source, state, _runtime, env, sha = _setup(tmp_path)
    shutil.rmtree(state / "data" / "backups")
    result = _run(["bash", str(SCRIPT), sha], source, env)
    backups = list((state / "data" / "backups").glob("sniper_tracker-predeploy-*.db"))
    assert backups == []
    assert "backup=deferred" in result.stdout
    evidence = json.loads((state / "data" / "memecoin_deploy.json").read_text())["backup"]
    assert evidence == {"status": "deferred", "path": None, "size_bytes": None,
                        "completed_at": None, "integrity_check": None,
                        "restore_verification": "unverified"}


def test_explicit_backup_creates_consistent_copy_after_validation(tmp_path):
    source, state, _runtime, env, sha = _setup(tmp_path)
    shutil.rmtree(state / "data" / "backups")
    _run(["bash", str(SCRIPT), sha, "--with-backup"], source, env)
    backups = list((state / "data" / "backups").glob("sniper_tracker-predeploy-*.db"))
    assert len(backups) == 1
    conn = sqlite3.connect(backups[0])
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    evidence = json.loads((state / "data" / "memecoin_deploy.json").read_text())["backup"]
    assert evidence["status"] == "completed"
    assert evidence["path"] == str(backups[0])
    assert evidence["size_bytes"] == backups[0].stat().st_size > 0
    from datetime import datetime
    assert datetime.fromisoformat(evidence["completed_at"]).tzinfo is not None
    assert evidence["integrity_check"] == "ok"
    assert evidence["restore_verification"] == "unverified"
    assert "sha256" not in evidence


def test_deploy_can_explicitly_defer_backup_without_pruning_existing_backup(tmp_path):
    source, state, _runtime, env, sha = _setup(tmp_path)
    backup_dir = state / "data" / "backups"
    existing = backup_dir / "sniper_tracker-predeploy-existing.db"
    existing.write_bytes((state / "sniper_tracker.db").read_bytes())

    result = _run(["bash", str(SCRIPT), sha, "--defer-backup"], source, env)

    assert existing.exists()
    assert list(backup_dir.glob(f"sniper_tracker-predeploy-{sha[:12]}-*.db")) == []
    assert "backup=deferred" in result.stdout


def test_backup_retention_keeps_just_created_backup_despite_future_old_mtime(tmp_path):
    source, state, _runtime, env, sha = _setup(tmp_path)
    old = state / "data" / "backups" / "sniper_tracker-predeploy-old.db"
    old.write_bytes((state / "sniper_tracker.db").read_bytes())
    future = time.time() + 86_400
    os.utime(old, (future, future))

    _run(["bash", str(SCRIPT), sha, "--with-backup"], source, env)

    backups = list((state / "data" / "backups").glob("sniper_tracker-predeploy-*.db"))
    assert len(backups) == 1
    assert sha[:12] in backups[0].name


def test_backup_retention_removes_pruned_sqlite_sidecars(tmp_path):
    source, state, _runtime, env, sha = _setup(tmp_path)
    backup_dir = state / "data" / "backups"
    old = backup_dir / "sniper_tracker-predeploy-old.db"
    old.write_bytes((state / "sniper_tracker.db").read_bytes())
    (backup_dir / f"{old.name}-wal").write_bytes(b"stale-wal")
    (backup_dir / f"{old.name}-shm").write_bytes(b"stale-shm")

    _run(["bash", str(SCRIPT), sha, "--with-backup"], source, env)

    assert not list(backup_dir.glob("sniper_tracker-predeploy-old.db*"))


def test_pre_switch_failure_leaves_current_and_post_switch_failure_rolls_back(tmp_path):
    source, state, runtime, env, sha1 = _setup(tmp_path)
    _run(["bash", str(SCRIPT), sha1], source, env)
    sha2 = _commit(source, "two\n")
    time.sleep(0.01)
    db = state / "sniper_tracker.db"
    (state / "data" / "backups" / "sniper_tracker-two.db").write_bytes(db.read_bytes())

    pre_env = env | {"MEMECOIN_DEPLOY_FAIL_STAGE": "pre_switch"}
    assert _run(["bash", str(SCRIPT), sha2], source, pre_env, False).returncode != 0
    assert (runtime / "current").resolve().name == sha1
    assert json.loads((state / "data" / "memecoin_deploy.json").read_text())["sha"] == sha1

    post_env = env | {"MEMECOIN_DEPLOY_FAIL_STAGE": "post_switch"}
    assert _run(["bash", str(SCRIPT), sha2], source, post_env, False).returncode != 0
    assert (runtime / "current").resolve().name == sha1
    manifest = json.loads((state / "data" / "memecoin_deploy.json").read_text())
    assert manifest["sha"] == sha1
    assert (runtime / "releases" / sha1).is_dir() and (runtime / "releases" / sha2).is_dir()


def _set_contract_cutover(state, cutoff, env):
    # The runtime loader intentionally gives explicit shell variables precedence over .env.
    # Pytest pins/discovers live memecoin settings in worker processes; clear those ambient values
    # so this fixture tests the isolated operator file it writes instead of xdist scheduling order.
    env.pop("MEMECOIN_REAL_ENABLED", None)
    env.pop("MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT", None)
    with (state / ".env").open("a") as handle:
        handle.write(
            "MEMECOIN_REAL_ENABLED=1\n"
            f"MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT={cutoff}\n"
        )


def _add_post_cutover_contractless_reservation(state, reserved_at):
    conn = sqlite3.connect(state / "sniper_tracker.db")
    for name, ddl in (
        ("reserved_at", "TEXT"),
        ("attempt_id", "TEXT"),
        ("funded_contract_version", "INTEGER"),
        ("funded_contract_hash", "TEXT"),
        ("funded_contract_json", "TEXT"),
    ):
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memecoin_pending_entries)")}
        if name not in columns:
            conn.execute(f"ALTER TABLE memecoin_pending_entries ADD COLUMN {name} {ddl}")
    conn.execute(
        "INSERT INTO memecoin_pending_entries(status,reserved_at,attempt_id) VALUES (?,?,?)",
        ("failed", reserved_at, "attempt-missing-contract"),
    )
    conn.commit()
    conn.close()


def test_contract_reconciliation_failure_preserves_prior_symlink(tmp_path):
    source, state, runtime, env, sha1 = _setup(tmp_path)
    _run(["bash", str(SCRIPT), sha1], source, env)
    sha2 = _commit(source, "two\n")
    cutoff = "2026-07-18T12:00:00+00:00"
    _set_contract_cutover(state, cutoff, env)
    _add_post_cutover_contractless_reservation(state, "2026-07-18T12:00:01+00:00")

    result = _run(["bash", str(SCRIPT), sha2], source, env, False)

    assert result.returncode != 0
    assert "post-cutover pending" in result.stderr
    assert (runtime / "current").resolve().name == sha1
    assert json.loads((state / "data" / "memecoin_deploy.json").read_text())["sha"] == sha1


def test_contract_reconciliation_ignores_pre_cutover_legacy_reservation(tmp_path):
    source, state, runtime, env, sha = _setup(tmp_path)
    _set_contract_cutover(state, "2026-07-18T12:00:00+00:00", env)
    _add_post_cutover_contractless_reservation(state, "2026-07-18T11:59:59+00:00")

    result = _run(["bash", str(SCRIPT), sha], source, env)

    assert result.returncode == 0
    assert (runtime / "current").resolve().name == sha


def test_invalid_contract_cutover_fails_before_switch(tmp_path):
    source, state, runtime, env, sha1 = _setup(tmp_path)
    _run(["bash", str(SCRIPT), sha1], source, env)
    sha2 = _commit(source, "two\n")
    _set_contract_cutover(state, "not-a-timestamp", env)

    result = _run(["bash", str(SCRIPT), sha2], source, env, False)

    assert result.returncode != 0
    assert "aware ISO timestamp" in result.stderr
    assert (runtime / "current").resolve().name == sha1


def test_rollover_activation_writes_complete_policy_at_explicit_cutoff(tmp_path):
    source, state, _runtime, env, sha = _setup(tmp_path)
    cutoff = "2026-07-18T08:15:00+00:00"
    activated = env | {"MEMECOIN_ROLLOVER_CUTOFF_OVERRIDE": cutoff}

    _run(["bash", str(SCRIPT), sha, "--activate-rollover"], source, activated)

    values = dict(
        line.split("=", 1) for line in (state / ".env").read_text().splitlines() if "=" in line
    )
    assert values == {
        "MEMECOIN_MAX_CONCURRENT": "7",
        "MEMECOIN_MAX_TOTAL_EXPOSURE_USD": "700",
        "MEMECOIN_SLOT_LANE_RABBIT_TAPE": "0",
        "MEMECOIN_REAL_TP_PCT": "0",
        "MEMECOIN_REAL_DERISK_PCT": "40",
        "MEMECOIN_REAL_LEGACY_TP_PCT": "60",
        "MEMECOIN_REAL_TRAIL_PCT": "30",
        "MEMECOIN_REAL_TRAIL_ARM_PCT": "15",
        "MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT": cutoff,
        "MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT": cutoff,
        "MEMECOIN_FUNDED_CLASSIFICATION_VERSION": "1",
        "MEMECOIN_FUNDED_REGIME_RISK_SCALE": "0.75",
        "MEMECOIN_FUNDED_TAPE_WINDOW_S": "900",
        "MEMECOIN_FUNDED_TAPE_MIN_OBSERVATIONS": "3",
        "MEMECOIN_FUNDED_TAPE_MIN_SPAN_S": "30",
        "MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT": "20",
        "MEMECOIN_FUNDED_POST_PEAK_GIVEBACK_PCT": "30",
        "MEMECOIN_FUNDED_REGIME_BUCKET_S": "900",
        "MEMECOIN_FUNDED_REGIME_BASELINE_BUCKETS": "23",
        "MEMECOIN_FUNDED_REGIME_MIN_NONEMPTY_BUCKETS": "8",
        "MEMECOIN_FUNDED_REGIME_MIN_BASELINE_BUYS": "30",
        "MEMECOIN_FUNDED_REGIME_MAX_STALENESS_S": "180",
        "MEMECOIN_REAL_NANO_DERISK_PCT": "40",
        "MEMECOIN_REAL_NANO_TRAIL_PCT": "40",
        "MEMECOIN_REAL_NANO_SL_PCT": "60",
        "MEMECOIN_REAL_MICRO_DERISK_PCT": "50",
        "MEMECOIN_REAL_MICRO_TRAIL_PCT": "50",
        "MEMECOIN_REAL_MICRO_SL_PCT": "60",
        "MEMECOIN_REAL_MID_DERISK_PCT": "30",
        "MEMECOIN_REAL_MID_TRAIL_PCT": "35",
        "MEMECOIN_REAL_MID_SL_PCT": "60",
    }


def test_band_policy_activation_preserves_contract_cutoff(tmp_path):
    source, state, _runtime, env, sha = _setup(tmp_path)
    old_contract_cutoff = "2026-07-18T13:50:27.456628+00:00"
    new_rollover_cutoff = "2026-08-19T17:30:00+00:00"
    (state / ".env").write_text(
        "MEMECOIN_FOLLOW3050_ALL_WALLETS=1\n"
        "MEMECOIN_REAL_RESERVABLE_BANDS=nano,micro,small,mid,out_of_scope\n"
        "MEMECOIN_REAL_STOP_PCT=0\n"
        "MEMECOIN_REAL_MID_SL_PCT=0\n"
        f"MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT={old_contract_cutoff}\n"
    )
    activated = env | {"MEMECOIN_ROLLOVER_CUTOFF_OVERRIDE": new_rollover_cutoff}

    _run(["bash", str(SCRIPT), sha, "--activate-band-policy"], source, activated)

    values = dict(
        line.split("=", 1) for line in (state / ".env").read_text().splitlines() if "=" in line
    )
    assert values["MEMECOIN_FOLLOW3050_ALL_WALLETS"] == "1"
    assert values["MEMECOIN_REAL_BAND_POLICY_BANDS"] == "nano,micro"
    assert values["MEMECOIN_REAL_NANO_SL_PCT"] == "60"
    assert values["MEMECOIN_REAL_MICRO_SL_PCT"] == "60"
    assert values["MEMECOIN_REAL_PARTIAL_DERISK_PCT"] == "20"
    assert values["MEMECOIN_REAL_PARTIAL_DERISK_FRACTION"] == "0.5"
    assert values["MEMECOIN_REAL_PARTIAL_TRAIL_PCT"] == "30"
    assert values["MEMECOIN_REAL_STOP_PCT"] == "0"
    assert values["MEMECOIN_REAL_MID_SL_PCT"] == "0"
    assert values["MEMECOIN_REAL_RESERVABLE_BANDS"] == "nano,micro,small,mid,out_of_scope"
    assert values["MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT"] == new_rollover_cutoff
    assert values["MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT"] == old_contract_cutoff


def test_band_policy_activation_requires_existing_contract_cutoff(tmp_path):
    source, state, _runtime, env, sha = _setup(tmp_path)
    before = (state / ".env").read_bytes()
    activated = env | {
        "MEMECOIN_ROLLOVER_CUTOFF_OVERRIDE": "2026-08-19T17:30:00+00:00",
    }

    result = _run(
        ["bash", str(SCRIPT), sha, "--activate-band-policy"], source, activated, False,
    )

    assert result.returncode != 0
    assert "existing funded contract cutoff" in result.stderr
    assert (state / ".env").read_bytes() == before


def test_rollover_dry_run_does_not_mutate_environment(tmp_path):
    source, state, _runtime, env, sha = _setup(tmp_path)
    before = (state / ".env").read_bytes()
    activated = env | {
        "MEMECOIN_ROLLOVER_CUTOFF_OVERRIDE": "2026-07-18T08:15:00+00:00",
    }

    _run(["bash", str(SCRIPT), sha, "--activate-rollover", "--dry-run"], source, activated)

    assert (state / ".env").read_bytes() == before


def test_rollover_rechecks_reservations_after_validation_before_env_mutation(tmp_path):
    source, state, runtime, env, sha = _setup(tmp_path)
    before = (state / ".env").read_bytes()
    injector = tmp_path / "inject_reservation.py"
    injector.write_text(
        "import os, pathlib, sqlite3\n"
        "marker = pathlib.Path(os.environ['SNIPER_STATE_ROOT']) / 'data' / 'reservation-injected'\n"
        "if not marker.exists():\n"
        "    conn = sqlite3.connect(os.environ['DB_PATH'])\n"
        "    conn.execute(\"INSERT INTO memecoin_pending_entries(status) VALUES ('reserved')\")\n"
        "    conn.commit()\n"
        "    conn.close()\n"
        "    marker.touch()\n"
    )
    activated = env | {
        "MEMECOIN_ROLLOVER_CUTOFF_OVERRIDE": "2026-07-18T08:15:00+00:00",
        "SNIPER_DEPLOY_VERIFY_COMMAND": f"python3 {injector}",
    }

    result = _run(
        ["bash", str(SCRIPT), sha, "--activate-rollover"], source, activated, False,
    )

    assert result.returncode != 0
    assert "pending reservation(s) block rollover cutover" in result.stderr
    assert (state / ".env").read_bytes() == before
    assert not (runtime / "current").exists()


def test_rollover_activation_restores_environment_on_switch_failures(tmp_path):
    source, state, runtime, env, sha1 = _setup(tmp_path)
    _run(["bash", str(SCRIPT), sha1], source, env)
    sha2 = _commit(source, "two\n")
    before = (state / ".env").read_bytes()
    common = env | {"MEMECOIN_ROLLOVER_CUTOFF_OVERRIDE": "2026-07-18T08:15:00+00:00"}

    pre = common | {"MEMECOIN_DEPLOY_FAIL_STAGE": "pre_switch"}
    assert _run(["bash", str(SCRIPT), sha2, "--activate-rollover"], source, pre, False).returncode != 0
    assert (state / ".env").read_bytes() == before
    assert (runtime / "current").resolve().name == sha1

    post = common | {"MEMECOIN_DEPLOY_FAIL_STAGE": "post_switch"}
    assert _run(["bash", str(SCRIPT), sha2, "--activate-rollover"], source, post, False).returncode != 0
    assert (state / ".env").read_bytes() == before
    assert (runtime / "current").resolve().name == sha1


def _provider_stage(tmp_path, state):
    from datetime import datetime, timezone
    from test_shyft_migration_preflight import allocation_record, candidate_env
    from scripts.research import shyft_migration_preflight as p
    stage = tmp_path / "provider.env"
    stage.write_text((state / ".env").read_text() + "".join(
        f"{k}={v}\n" for k, v in candidate_env().items() if k in p.PROVIDER_KEYS))
    stage.chmod(0o600)
    record = allocation_record()
    now = datetime.now(timezone.utc).isoformat()
    record["allocations"][0]["verified_at"] = now
    record["capabilities"]["observed_at"] = now
    allocation = tmp_path / "allocation.json"
    allocation.write_text(json.dumps(record))
    proof = p.check_preflight(p.read_env(stage), record)
    proof.update(source_env_sha256=p.file_digest(state / ".env"),
                 fomo_env_sha256=p.file_digest(state / ".fomo.env"),
                 staged_env_sha256=p.file_digest(stage), allocation_record=str(allocation),
                 allocation_record_sha256=p.file_digest(allocation))
    report = tmp_path / "provider-preflight.json"
    report.write_text(json.dumps(proof))
    report.chmod(0o600)
    return stage, report


def test_private_provider_stage_applies_only_after_validation_and_cleans_up(tmp_path):
    source, state, runtime, env, sha1 = _setup(tmp_path)
    _run(["bash", str(SCRIPT), sha1], source, env)
    sha2 = _commit(source, "provider switch\n")
    _web, env = _web_env(tmp_path, env)
    stage, proof = _provider_stage(tmp_path, state)
    cmd = ["bash", str(SCRIPT), sha2, "--restart", "--defer-backup",
           "--provider-env", str(stage), "--provider-preflight", str(proof)]
    result = _run(cmd, source, env, False)
    assert result.returncode == 0, result.stderr
    assert (state / ".env").read_bytes() == stage.read_bytes()
    assert (runtime / "current").resolve().name == sha2
    manifest = json.loads((state / "data/memecoin_deploy.json").read_text())
    assert manifest["solana_provider"]["profile"] == "shyft"
    assert manifest["solana_provider"]["staged_env_sha256"] == hashlib.sha256(stage.read_bytes()).hexdigest()
    assert manifest["backup"]["status"] == "deferred"
    assert list((state / "data").glob(".provider-*")) == []
    assert "private-grpc-token-DO-NOT-PRINT" not in result.stdout + result.stderr


@pytest.mark.parametrize("stage_name", ["env_write", "old_stop", "manifest_switch", "new_start"])
def test_provider_stage_restores_exact_env_manifest_release_on_each_interruption(tmp_path, stage_name):
    source, state, runtime, env, sha1 = _setup(tmp_path)
    _run(["bash", str(SCRIPT), sha1], source, env)
    sha2 = _commit(source, "provider switch\n")
    before = (state / ".env").read_bytes()
    old_manifest = (state / "data/memecoin_deploy.json").read_bytes()
    stage, proof = _provider_stage(tmp_path, state)
    result = _run(["bash", str(SCRIPT), sha2, "--restart", "--provider-env", str(stage),
                   "--provider-preflight", str(proof)], source,
                  env | {"MEMECOIN_DEPLOY_FAIL_STAGE": stage_name}, False)
    assert result.returncode != 0
    assert f"injected {stage_name}" in result.stderr, result.stderr
    assert (state / ".env").read_bytes() == before
    assert (state / "data/memecoin_deploy.json").read_bytes() == old_manifest
    assert (runtime / "current").resolve().name == sha1
    assert list((state / "data").glob(".provider-*")) == []


def test_provider_stage_refuses_peer_env_drift_before_entry_gate_or_stop(tmp_path):
    source, state, runtime, env, sha1 = _setup(tmp_path)
    _run(["bash", str(SCRIPT), sha1], source, env)
    sha2 = _commit(source, "provider switch\n")
    stage, proof = _provider_stage(tmp_path, state)
    with (state / ".env").open("a") as handle:
        handle.write("PEER_SETTING=preserve\n")
    changed = (state / ".env").read_bytes()
    result = _run(["bash", str(SCRIPT), sha2, "--restart", "--provider-env", str(stage),
                   "--provider-preflight", str(proof)], source, env, False)
    assert result.returncode != 0 and "source_env_changed" in result.stdout + result.stderr
    assert "entry_gate_trip" not in result.stdout
    assert (state / ".env").read_bytes() == changed
    assert (runtime / "current").resolve().name == sha1


@pytest.mark.parametrize('preserve_original', [False, True])
def test_provider_rollback_stops_only_exact_owned_processes(tmp_path, preserve_original):
    import sys
    text = SCRIPT.read_text()
    assert "stop_provider_processes() {" in text, "exact provider process rollback missing"
    function = text[text.index("stop_provider_processes() {"):]
    function = function[:function.index("\n}\n") + 3]
    state = tmp_path / "state"
    foreign_state = tmp_path / "foreign"
    state.mkdir(); foreign_state.mkdir()
    release = state / ".runtime/memecoin/releases/current"
    other_release = state / ".runtime/memecoin/releases/old"
    scripts = [release / "scripts/memecoin_runner_loop.py",
               release / "scripts/research/onchain_wallet_watch.py",
               other_release / "scripts/research/onchain_wallet_watch.py"]
    for path in scripts:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("import time\ntime.sleep(30)\n")
    owned = [subprocess.Popen([sys.executable, str(path)], env=os.environ | {"SNIPER_STATE_ROOT": str(state)})
             for path in scripts[:2]]
    retained = [subprocess.Popen([sys.executable, str(scripts[1])],
                                env=os.environ | {"SNIPER_STATE_ROOT": str(foreign_state)}),
                subprocess.Popen([sys.executable, str(scripts[0])],
                                 env=os.environ | {"SNIPER_STATE_ROOT": str(foreign_state)}),
                subprocess.Popen([sys.executable, str(scripts[2])],
                                 env=os.environ | {"SNIPER_STATE_ROOT": str(state)})]
    original_file = tmp_path / 'original.json'
    if preserve_original:
        original = subprocess.Popen([sys.executable, str(scripts[1])],
            env=os.environ | {'SNIPER_STATE_ROOT': str(state)})
        retained.append(original)
        birth = (Path('/proc') / str(original.pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]
        original_file.write_text(json.dumps([[original.pid, str(scripts[1]), birth]]))
    try:
        harness = f'''set -euo pipefail
PY={sys.executable}
DEFAULT_SOURCE={REPO_ROOT}
RELEASE={release}
STATE_ROOT={state}
PROVIDER_ORIGINAL_IDENTITIES={original_file if preserve_original else ''}
{function}
stop_provider_processes
'''
        result = _run(["bash", "-c", harness], tmp_path, check=False)
        assert result.returncode == 0, result.stderr
        assert all(p.wait(timeout=2) == -15 for p in owned)
        assert all(p.poll() is None for p in retained)
    finally:
        for process in owned + retained:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)

@pytest.mark.parametrize("failure", ["candidate_stop", "environment_restore", "old_start", "old_ready"])
def test_provider_failed_rollback_preserves_hold_and_recovery_copy(tmp_path, failure):
    text = SCRIPT.read_text()
    body = text[text.index("rollback() {", text.index("SWITCHED=0")):]
    body = body[:body.index("\n}\n") + 3]
    state = tmp_path / "state"; state.mkdir()
    current, old = tmp_path / "current", tmp_path / "old"
    old.mkdir(); current.symlink_to(old)
    watchdog = old / "scripts/memecoin_watchdog.sh"
    watchdog.parent.mkdir(); watchdog.write_text("exit 1\n" if failure == "old_start" else "exit 0\n")
    recovery = state / "provider-backup"; recovery.write_text("private old env")
    hold = state / "hold"; hold.write_text("deploy hold")
    harness = f'''set -euo pipefail
SUCCESS=0; SWITCHED=1; PROVIDER_ENV=stage; PROVIDER_RECOVERY_REQUIRED=0; PROVIDER_CANDIDATE_LAUNCHED=1
SNIPER_DEPLOY_NO_PROCESS=0; OLD_RELEASE={old}; RELEASE={tmp_path / 'candidate'}
CURRENT={current}; MANIFEST={state / 'manifest'}; OLD_MANIFEST={state / 'old-manifest'}
STATE_ROOT={state}; LAUNCH_VIA_CRON=0; ROLLOVER_OLD_RUNNER_STOPPED=1
CANDIDATE_MANIFEST={state / 'candidate-manifest'}; ROLLOVER_ENV_BACKUP=''; LIFI_ENV_ROLLBACK=''
DEPLOY_INTENT={state / 'intent'}; PROVIDER_BACKUP={recovery}
restore_lifi_env() {{ :; }}
restore_rollover_env() {{ :; }}
restore_provider_env() {{ {'return 1' if failure == 'environment_restore' else ':'}; }}
source_mirror_rollback_safe() {{ :; }}
stop_provider_processes() {{ {'return 1' if failure == 'candidate_stop' else ':'}; }}
verify_provider_rollback() {{ {'return 1' if failure == 'old_ready' else ':'}; }}
clear_deploy_entry_gate() {{ rm -f {hold}; }}
cleanup_provider_stage() {{ [ "$PROVIDER_RECOVERY_REQUIRED" -eq 1 ] || rm -f "$PROVIDER_BACKUP"; }}
{body}
false || rollback
'''
    result = _run(["bash", "-c", harness], tmp_path, check=False)
    assert result.returncode != 0
    assert hold.exists(), "unsafe rollback cleared funded entry hold"
    assert recovery.exists(), "unsafe rollback removed private recovery environment"
    assert "provider rollback incomplete" in result.stderr


def test_same_sha_prelaunch_stage_failure_never_stops_existing_processes(tmp_path):
    text = SCRIPT.read_text()
    body = text[text.index('rollback() {', text.index('SWITCHED=0')):]
    body = body[:body.index('\n}\n') + 3]
    events = tmp_path / 'events'
    operator_kill = tmp_path / 'operator-kill'; operator_kill.write_bytes(b'operator owns this kill\n')
    harness = f'''set -euo pipefail
SUCCESS=0; SWITCHED=0; PROVIDER_ENV=stage; PROVIDER_RECOVERY_REQUIRED=0; PROVIDER_CANDIDATE_LAUNCHED=0
SNIPER_DEPLOY_NO_PROCESS=0; OLD_RELEASE={tmp_path}; RELEASE={tmp_path}
CURRENT={tmp_path / 'current'}; MANIFEST={tmp_path / 'manifest'}; OLD_MANIFEST={tmp_path / 'old-manifest'}
STATE_ROOT={tmp_path}; LAUNCH_VIA_CRON=0; ROLLOVER_OLD_RUNNER_STOPPED=0
CANDIDATE_MANIFEST=''; ROLLOVER_ENV_BACKUP=''; LIFI_ENV_ROLLBACK=''; DEPLOY_INTENT=''
restore_lifi_env() {{ :; }}
restore_rollover_env() {{ :; }}
restore_provider_env() {{ :; }}
stop_provider_processes() {{ echo stopped_existing >> {events}; }}
source_mirror_rollback_safe() {{ :; }}
clear_deploy_entry_gate() {{ echo clear_own_hold >> {events}; }}
cleanup_provider_stage() {{ :; }}
{body}
false || rollback
'''
    result = _run(['bash', '-c', harness], tmp_path, check=False)
    assert result.returncode != 0
    assert events.read_text().splitlines() == ['clear_own_hold']
    assert operator_kill.read_bytes() == b'operator owns this kill\n'


@pytest.mark.parametrize('switched', [False, True])
@pytest.mark.parametrize('old_ready', [False, True])
def test_shyft_redeploy_without_env_stage_uses_scoped_recovery(tmp_path, switched, old_ready):
    text = SCRIPT.read_text()
    body = text[text.index('rollback() {', text.index('SWITCHED=0')):]
    body = body[:body.index('\n}\n') + 3]
    old = tmp_path / 'old'; (old / 'scripts').mkdir(parents=True)
    (old / 'scripts/memecoin_watchdog.sh').write_text('exit 0\n')
    current = tmp_path / 'current'; current.symlink_to(old)
    events = tmp_path / 'events'
    operator_kill = tmp_path / 'operator-kill'; operator_kill.write_bytes(b'operator kill\n')
    harness = f'''set -euo pipefail
SUCCESS=0; SWITCHED={int(switched)}; PROVIDER_ENV=''; SHYFT_RUNTIME=1
PROVIDER_RECOVERY_REQUIRED=0; PROVIDER_CANDIDATE_LAUNCHED=1; PROVIDER_RH_STOPPED=1
SNIPER_DEPLOY_NO_PROCESS=0; OLD_RELEASE={old}; RELEASE={tmp_path / 'candidate'}
CURRENT={current}; MANIFEST={tmp_path / 'manifest'}; OLD_MANIFEST={tmp_path / 'old-manifest'}
STATE_ROOT={tmp_path}; LAUNCH_VIA_CRON=0; ROLLOVER_OLD_RUNNER_STOPPED=1
CANDIDATE_MANIFEST=''; ROLLOVER_ENV_BACKUP=''; LIFI_ENV_ROLLBACK=''; DEPLOY_INTENT=''
restore_lifi_env() {{ :; }}
restore_rollover_env() {{ :; }}
restore_provider_env() {{ :; }}
stop_provider_processes() {{ echo scoped_stop >> {events}; }}
source_mirror_rollback_safe() {{ :; }}
pkill() {{ echo unscoped_stop >> {events}; }}
start_provider_rh() {{ echo old_rh_start >> {events}; }}
restart_old_rollover_runner() {{ echo old_runner_start >> {events}; }}
verify_provider_rollback() {{ echo old_graph_verify >> {events}; return {0 if old_ready else 1}; }}
clear_deploy_entry_gate() {{ echo clear_own_hold >> {events}; }}
cleanup_provider_stage() {{ :; }}
{body}
false || rollback
'''
    result = _run(['bash', '-c', harness], tmp_path, check=False)
    assert result.returncode != 0
    lines = events.read_text().splitlines()
    assert lines[0] == 'scoped_stop', 'Shyft redeploy skipped exact candidate cleanup'
    assert 'unscoped_stop' not in lines
    assert 'old_rh_start' in lines and 'old_graph_verify' in lines
    assert ('clear_own_hold' in lines) is old_ready
    assert operator_kill.read_bytes() == b'operator kill\n'


@pytest.mark.parametrize("missing_backup", [False, True])
def test_provider_restore_preserves_concurrent_peer_env_edit(tmp_path, missing_backup):
    import sys
    text = SCRIPT.read_text()
    function = text[text.index("restore_provider_env() {"):]
    function = function[:function.index("\n}\n") + 3]
    state = tmp_path / "state"; state.mkdir()
    old, staged = tmp_path / "old", tmp_path / "staged"
    old.write_text("before\n"); staged.write_text("candidate\n")
    if missing_backup:
        old.unlink()
    peer = b"candidate\nPEER_SETTING=preserve\n"
    (state / ".env").write_bytes(peer)
    harness = f'''set -euo pipefail
PY={sys.executable}; PROVIDER_CHANGED=1; PROVIDER_BACKUP={old}; PROVIDER_ENV={staged}
STATE_ROOT={state}; PROVIDER_OLD_MODE=600
{function}
restore_provider_env
'''
    result = _run(["bash", "-c", harness], tmp_path, check=False)
    assert result.returncode != 0
    assert (state / ".env").read_bytes() == peer
    if not missing_backup:
        assert old.read_text() == "before\n"


def test_verifier_failure_removes_candidate_manifest(tmp_path):
    source, state, _runtime, env, sha = _setup(tmp_path)
    failed = env | {"SNIPER_DEPLOY_VERIFY_COMMAND": "false"}

    result = _run(["bash", str(SCRIPT), sha, "--dry-run"], source, failed, False)

    assert result.returncode != 0
    assert list((state / "data").glob(".memecoin_deploy.candidate.*")) == []


def test_restart_discovers_runners_instead_of_trusting_only_pidfile():
    text = SCRIPT.read_text()
    assert "memecoin_runtime_processes.py" in text
    assert "stale or missing PID file" in text
    assert 'pgrep -f -- "$SOURCE_MATCH"' not in text


def test_restart_releases_only_unsigned_reservations_after_old_runner_stops():
    text = SCRIPT.read_text()
    stop = text.index('kill "$OLD_PID" 2>/dev/null || true', text.index('CUTOVER_STARTED'))
    release = text.index("release_unsigned_reservations_after_runner_stop", stop)
    start = text.index('memecoin_watchdog.sh', release)
    assert stop < release < start


def test_deploy_requires_a_post_cutover_exit_sweep_before_success():
    text = SCRIPT.read_text()
    assert "CUTOVER_STARTED" in text
    assert "runner_launch_health" in text


def test_prune_keeps_current_previous_recent_and_removes_older(tmp_path):
    """2026-07-23: after a verified deploy, retention keeps current + previous + the newest
    MEMECOIN_RELEASE_KEEP-2 others and git-worktree-removes the rest. Pruned releases stay
    re-creatable (redeploy re-adds the worktree from the SHA)."""
    source, state, runtime, env, sha1 = _setup(tmp_path)
    env = dict(env, MEMECOIN_RELEASE_KEEP="3")
    releases = runtime / "releases"

    def kept():
        return {p.name for p in releases.iterdir() if p.is_dir()}

    shas = [sha1]
    _run(["bash", str(SCRIPT), sha1], source, env)
    for txt in ("two\n", "three\n", "four\n", "five\n"):
        time.sleep(0.02)                       # keep release-dir mtimes strictly increasing
        s = _commit(source, txt)
        shas.append(s)
        _run(["bash", str(SCRIPT), s], source, env)

    final = kept()
    cur, prev = shas[-1], shas[-2]
    assert cur in final and prev in final           # current + previous always retained
    assert shas[0] not in final                     # the oldest is pruned
    assert len(final) == 3                           # keep=3 total (current + previous + 1 other)
    assert (runtime / "current").resolve() == releases / cur     # symlink intact
    # git worktree metadata is clean (no dangling entries left by the removes)
    wl = _run(["git", "worktree", "list"], source)
    for s in shas[:-3]:
        assert s not in wl.stdout

    # re-creatability: redeploy a pruned SHA restores its worktree from git
    _run(["bash", str(SCRIPT), shas[0], "--allow-rollback"], source, env)
    assert (releases / shas[0]).is_dir()


def test_deploy_refuses_an_older_release_unless_rollback_is_explicit(tmp_path):
    # 2026-09-16 18:20Z: a restart ran an older SHA over a newer live release for ~2 min.
    source, state, runtime, env, old_sha = _setup(tmp_path)
    _run(["bash", str(SCRIPT), old_sha], source, env)
    (source / "tracked.txt").write_text("two\n")
    _run(["git", "commit", "-qam", "two"], cwd=source)
    _run(["git", "push", "-q", "origin", "main"], cwd=source)
    new_sha = _run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
    _run(["bash", str(SCRIPT), new_sha], source, env)

    refused = _run(["bash", str(SCRIPT), old_sha], source, env, False)
    assert refused.returncode != 0
    assert "older than the live release" in (refused.stdout + refused.stderr)
    assert (runtime / "current").resolve() == runtime / "releases" / new_sha

    allowed = _run(["bash", str(SCRIPT), old_sha, "--allow-rollback"], source, env, False)
    assert allowed.returncode == 0, allowed.stderr
    assert (runtime / "current").resolve() == runtime / "releases" / old_sha


# ---- the runner launch leaves the caller's scope (2026-09-27) --------------------------------
#
# At 14:50:48Z an out-of-memory kill in the Claude desktop app's systemd scope made systemd stop
# the whole scope, and with it the funded runner a deploy had launched from a session: 26.4 s with
# no runner. A deploy whose shell is not under system.slice now hands the launch to the cron
# watchdog (system.slice/cron.service) and defaults to --require-flat.

APP_SCOPE = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-com.anthropic.Claude-1.scope"


def _open_real_position(state):
    with sqlite3.connect(state / "sniper_tracker.db") as conn:
        conn.execute("CREATE TABLE memecoin_positions(execution_mode TEXT,is_open INTEGER)")
        conn.execute("INSERT INTO memecoin_positions VALUES('real',1)")


def test_the_caller_cgroup_override_is_refused_outside_the_harness(tmp_path):
    source, state, runtime, env, sha = _setup(tmp_path)
    (state / "data" / ".sniper_deploy_test_fixture").unlink()
    live = {k: v for k, v in env.items()
            if k not in ("SNIPER_DEPLOY_SKIP_VALIDATION", "SNIPER_DEPLOY_VERIFY_COMMAND")}
    result = _run(["bash", str(SCRIPT), sha], source,
                  live | {"MEMECOIN_DEPLOY_CALLER_CGROUP": APP_SCOPE}, False)
    assert result.returncode != 0
    assert "MEMECOIN_DEPLOY_CALLER_CGROUP is only for the isolated deploy harness" in result.stderr


def test_an_app_scope_caller_hands_the_launch_to_cron_and_requires_a_flat_book(tmp_path):
    source, state, runtime, env, sha = _setup(tmp_path)
    _open_real_position(state)
    result = _run(["bash", str(SCRIPT), sha], source,
                  env | {"MEMECOIN_DEPLOY_CALLER_CGROUP": APP_SCOPE}, False)
    assert result.returncode != 0
    assert "the runner launch is handed to cron" in result.stderr
    assert "funded book is not flat" in result.stderr
    assert not (runtime / "current").exists()


def test_allow_open_book_overrides_the_flat_default(tmp_path):
    source, state, runtime, env, sha = _setup(tmp_path)
    _open_real_position(state)
    result = _run(["bash", str(SCRIPT), sha, "--allow-open-book"], source,
                  env | {"MEMECOIN_DEPLOY_CALLER_CGROUP": APP_SCOPE}, False)
    assert result.returncode == 0, result.stderr
    assert (runtime / "current").resolve().name == sha


def test_a_system_slice_caller_keeps_the_direct_launch_and_no_forced_flat(tmp_path):
    source, state, runtime, env, sha = _setup(tmp_path)
    _open_real_position(state)
    result = _run(["bash", str(SCRIPT), sha], source,
                  env | {"MEMECOIN_DEPLOY_CALLER_CGROUP": "0::/system.slice/cron.service"}, False)
    assert result.returncode == 0, result.stderr
    assert "handed to cron" not in result.stderr


def _cron_wait(tmp_path, accept_prefix=None):
    """Run the script's own wait_for_cron_launch against a live process whose command line names
    the release runner and whose health check answers ok, so ONLY the cgroup can refuse it."""
    import sys
    text = SCRIPT.read_text()
    fn = text[text.index("wait_for_cron_launch() {"):]
    fn = fn[:fn.index("\n}\n") + 3]
    if accept_prefix is not None:
        fn = fn.replace('"0::/system.slice/"*', '"%s"*' % accept_prefix)
    release = tmp_path / "rel"
    (release / "scripts").mkdir(parents=True)
    runner = release / "scripts" / "memecoin_runner_loop.py"
    runner.write_text("import time\ntime.sleep(30)\n")
    proc = subprocess.Popen([sys.executable, str(runner)])
    try:
        pidfile = tmp_path / "runner.pid"
        pidfile.write_text(str(proc.pid))
        fake_py = tmp_path / "fake_py"
        fake_py.write_text("#!/bin/sh\necho ok\n")
        fake_py.chmod(0o755)
        script = (f"set -u\nstep() {{ :; }}\nPY={fake_py}\nSTATE_ROOT={tmp_path}\n"
                  f"WATCHDOG_LOCK={tmp_path}/wd.lock\nMEMECOIN_PIDFILE={pidfile}\n"
                  f"MEMECOIN_DEPLOY_CRON_WAIT_S=2\nexec 8>\"$WATCHDOG_LOCK\"\n{fn}\n"
                  f"wait_for_cron_launch {release} && echo ACCEPTED || echo REFUSED\n")
        own = Path(f"/proc/{proc.pid}/cgroup").read_text().strip()
        out = _run(["bash", "-c", script], tmp_path).stdout.strip()
        return own, out
    finally:
        proc.kill()
        proc.wait()


def test_the_cron_wait_refuses_a_runner_outside_system_slice(tmp_path):
    if not Path("/proc/self/cgroup").exists():
        pytest.skip("needs /proc cgroups")
    own, out = _cron_wait(tmp_path)
    if own.startswith("0::/system.slice/"):
        pytest.skip("this test process already runs under system.slice")
    assert out == "REFUSED", (own, out)


def test_the_cron_wait_accepts_when_the_cgroup_matches(tmp_path):
    """Negative control: the same process is accepted once the cgroup rule matches it, so the
    refusal above is the cgroup check and not a broken harness."""
    if not Path("/proc/self/cgroup").exists():
        pytest.skip("needs /proc cgroups")
    own_prefix = Path("/proc/self/cgroup").read_text().strip()[:4]     # "0::/"
    own, out = _cron_wait(tmp_path, accept_prefix=own_prefix)
    assert out == "ACCEPTED", (own, out)


def test_the_cron_wait_default_covers_two_cron_runs():
    """Cron fires at :01 and a handoff lands at any second, so a 100 s wait saw ONE cron run whenever
    the handoff came before ~:53. On 2026-09-27 (21:26:13Z) that one run refused on a test's fake
    runner under /tmp ("foreign runner") and the deploy fell back to an app-scope launch. Two runs
    need 60 + 60 s plus the watchdog's ~5-10 s launch and verify."""
    import re
    defaults = re.findall(r"MEMECOIN_DEPLOY_CRON_WAIT_S:-(\d+)", SCRIPT.read_text())
    assert defaults and all(int(d) >= 130 for d in defaults), defaults


def test_every_launch_site_tries_cron_first_and_the_fallback_exits_non_zero():
    text = SCRIPT.read_text()
    calls = [i for i, line in enumerate(text.splitlines())
             if line.lstrip().startswith("SNIPER_STATE_ROOT=") and "memecoin_watchdog.sh" in line]
    lines = text.splitlines()
    for i in calls:
        window = "\n".join(lines[max(0, i - 6):i])
        assert "wait_for_cron_launch" in window, lines[i]
        assert "cron_launch_failed_loudly" in window, lines[i]
    fn = text[text.index("wait_for_cron_launch() {"):]
    fn = fn[:fn.index("\n}\n")]
    assert fn.index("exec 8>&-") < fn.index("while ")          # lock released for the wait
    assert fn.count('flock -w 120 8') == 2                    # and re-taken on both exits
    tail = text[text.index('echo "deployed memecoin runtime'):]
    assert 'if [ "$CRON_LAUNCH_FALLBACK" -eq 1 ]' in tail and "exit 3" in tail


# --- the web follows every restarted runner deploy (operator 2026-10-02: "dashboard is being left
# behind on each update"). Only memecoin_deploy_pinned.sh deployed the web, so every direct deploy and
# every --activate-* mode left .runtime/web on an old SHA (web e37d439e while the runner ran 1e0a3a045).

def _web_stub(tmp_path, *, fail=False):
    """A stand-in for deploy_web_runtime.sh that flips $SNIPER_WEB_RUNTIME/current like the real one
    and records its arguments. The harness source tree has no web server to import."""
    stub = tmp_path / "web_stub.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$@" > "$SNIPER_WEB_RUNTIME/args"\n'
        'flock -n "$SNIPER_STATE_ROOT/data/memecoin_watchdog.lock" true '
        '&& echo free > "$SNIPER_WEB_RUNTIME/watchdog_lock" || echo held > "$SNIPER_WEB_RUNTIME/watchdog_lock"\n'
        + ("echo 'web smoke failed' >&2; exit 1\n" if fail else
           'mkdir -p "$SNIPER_WEB_RUNTIME/releases/$1"\n'
           'ln -sfn "$SNIPER_WEB_RUNTIME/releases/$1" "$SNIPER_WEB_RUNTIME/current"\n'))
    stub.chmod(0o755)
    return stub


def _web_env(tmp_path, env, *, fail=False):
    web = tmp_path / "web"
    web.mkdir()
    return web, env | {"SNIPER_WEB_RUNTIME": str(web), "SNIPER_WEB_DEPLOY_SCRIPT": str(_web_stub(tmp_path, fail=fail))}


def test_a_restarted_runner_deploy_leaves_the_web_on_the_same_sha(tmp_path):
    source, state, runtime, env, sha = _setup(tmp_path)
    web, env = _web_env(tmp_path, env)
    result = _run(["bash", str(SCRIPT), sha, "--restart", "--defer-backup"], source, env, check=False)
    assert result.returncode == 0, result.stderr
    assert (runtime / "current").resolve().name == sha
    assert (web / "current").resolve().name == sha, "a runner deploy finished with the web on another SHA"
    # the harness never cycles a process: the real web script's cycle kills the LIVE dashboard by argv
    assert (web / "args").read_text().split() == [sha, "--no-cycle"]
    # and the runner's watchdog lock is free while the web deploys (it can take a minute)
    assert (web / "watchdog_lock").read_text().strip() == "free"


def test_local_commit_opt_in_reaches_the_web_deploy(tmp_path):
    source, _state, runtime, env, _sha = _setup(tmp_path)
    (source / "tracked.txt").write_text("local reviewed change\n")
    _run(["git", "commit", "-qam", "local only"], source)
    sha = _run(["git", "rev-parse", "HEAD"], source).stdout.strip()
    web, env = _web_env(tmp_path, env)
    result = _run(["bash", str(SCRIPT), sha, "--restart", "--defer-backup",
                   "--allow-local-commit"], source, env, False)
    assert result.returncode == 0, result.stderr
    assert (runtime / "current").resolve().name == sha
    assert (web / "current").resolve().name == sha
    args = (web / "args").read_text().split()
    assert args[0] == sha
    assert sorted(args[1:]) == ["--allow-local-commit", "--no-cycle"]


def test_a_failed_web_deploy_keeps_the_runner_and_ends_loudly_non_zero(tmp_path):
    source, state, runtime, env, sha = _setup(tmp_path)
    web, env = _web_env(tmp_path, env, fail=True)
    result = _run(["bash", str(SCRIPT), sha, "--restart", "--defer-backup"], source, env, check=False)
    assert result.returncode == 4
    assert "runner deployed, web deploy FAILED" in result.stderr
    assert (runtime / "current").resolve().name == sha          # the runner is never rolled back for it


def test_a_deploy_without_restart_leaves_the_web_alone(tmp_path):
    source, state, runtime, env, sha = _setup(tmp_path)
    web, env = _web_env(tmp_path, env)
    result = _run(["bash", str(SCRIPT), sha, "--defer-backup"], source, env, check=False)
    assert result.returncode == 0, result.stderr
    assert not (web / "args").exists() and not (web / "current").exists()


def test_the_web_script_override_is_refused_outside_the_harness(tmp_path):
    source, state, runtime, env, sha = _setup(tmp_path)
    (state / "data" / ".sniper_deploy_test_fixture").unlink()
    web, env = _web_env(tmp_path, env)
    result = _run(["bash", str(SCRIPT), sha, "--restart"], source,
                  {k: v for k, v in env.items() if not k.startswith("SNIPER_DEPLOY_")}, check=False)
    assert result.returncode != 0 and "SNIPER_WEB_DEPLOY_SCRIPT" in result.stderr
    assert not (web / "args").exists()


def test_the_pinned_wrapper_no_longer_deploys_the_web_a_second_time():
    pinned = (REPO_ROOT / "scripts" / "memecoin_deploy_pinned.sh").read_text()
    calls = [l for l in pinned.splitlines() if "deploy_web_runtime.sh" in l and not l.lstrip().startswith("#")]
    assert calls == []
