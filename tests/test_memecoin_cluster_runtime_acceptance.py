from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _worker_path() -> Path:
    path = ROOT / "scripts" / "memecoin_cluster_gate_worker.py"
    assert path.is_file()
    return path


def test_worker_refuses_start_without_research_rpc(tmp_path) -> None:
    worker = _worker_path()
    env = os.environ.copy()
    env.pop("MEMECOIN_RESEARCH_HELIUS_RPC_URL", None)
    env["SNIPER_STATE_ROOT"] = str(tmp_path)
    result = subprocess.run(
        ["python3", str(worker), "--once"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "MEMECOIN_RESEARCH_HELIUS_RPC_URL" in result.stderr


def test_worker_contract_uses_one_second_heartbeat_under_state_root() -> None:
    source = _worker_path().read_text()
    assert "SNIPER_STATE_ROOT" in source
    assert "memecoin_cluster_gate.heartbeat" in source
    assert "memecoin_cluster_gate.status.json" in source
    assert "1.0" in source or "1" in source


def test_cluster_watchdog_uses_proc_checks_not_pattern_kills() -> None:
    _worker_path()
    source = (ROOT / "scripts" / "memecoin_cluster_gate_watchdog.sh").read_text()
    assert "/proc" in source
    assert "pgrep -f" not in source
    assert "pkill -f" not in source


def test_deploy_keeps_cluster_activation_explicit_and_default_off() -> None:
    _worker_path()
    source = (ROOT / "scripts" / "deploy_memecoin_runtime.sh").read_text()
    assert "--activate-cluster-gate" in source
    assert "MEMECOIN_REAL_CLUSTER_GATE_ENABLED=1" in source
    assert "ACTIVATE_CLUSTER_GATE=0" in source


def test_activation_checks_worker_health_before_enabling_gate() -> None:
    _worker_path()
    source = (ROOT / "scripts" / "deploy_memecoin_runtime.sh").read_text()
    heartbeat = source.index("memecoin_cluster_gate.heartbeat")
    socket = source.index("memecoin_cluster_gate.sock")
    enabled = source.index("MEMECOIN_REAL_CLUSTER_GATE_ENABLED=1")
    assert heartbeat < enabled
    assert socket < enabled


def test_runtime_verifier_requires_pinned_worker_and_state_paths() -> None:
    _worker_path()
    source = (ROOT / "scripts" / "memecoin_runtime_verify.py").read_text()
    assert ".runtime/memecoin/current" in source
    assert "memecoin_cluster_gate.sock" in source
    assert "memecoin_cluster_gate.db" in source
    assert "SNIPER_STATE_ROOT" in source


def _fake_proc(root, pid, argv):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")


def _run_watchdog(tmp_path, procs):
    """The watchdog through a `current` symlink, on a FAKE process table (never the host's)."""
    import os
    import shutil
    import subprocess
    rel = tmp_path / "releases" / "A" / "scripts"
    rel.mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "memecoin_cluster_gate_watchdog.sh", rel)
    (rel / "memecoin_cluster_gate_worker.py").write_text("")
    (tmp_path / "current").symlink_to(tmp_path / "releases" / "A")
    fake_py = tmp_path / "python3"
    fake_py.write_text('#!/bin/sh\necho "$1" > "%s"\n' % (tmp_path / "started"))
    fake_py.chmod(0o755)
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid, argv in procs.items():
        _fake_proc(proc, pid, argv)
    out = subprocess.run(["bash", str(tmp_path / "current" / "scripts" / "memecoin_cluster_gate_watchdog.sh")],
                         env=dict(os.environ, SNIPER_STATE_ROOT=str(tmp_path / "state"),
                                  SNIPER_PYTHON=str(fake_py), CLUSTER_WATCHDOG_PROC_ROOT=str(proc)),
                         capture_output=True, text=True, timeout=30)
    import time
    for _ in range(50):
        if (tmp_path / "started").exists():
            break
        time.sleep(0.05)
    started = (tmp_path / "started").read_text().strip() if (tmp_path / "started").exists() else None
    return out.stderr, started, str((tmp_path / "releases" / "A" / "scripts" /
                                     "memecoin_cluster_gate_worker.py").resolve())


def test_the_cluster_watchdog_uses_the_physical_release_path_and_exact_argv(tmp_path):
    import subprocess
    old = str(tmp_path / "releases" / "OLD" / "scripts" / "memecoin_cluster_gate_worker.py")
    # The superseded entry carries our own sleep's pid: a fake table must never signal, and if it
    # ever did, the only process hit is this test's child, never a host process.
    child = subprocess.Popen(["sleep", "30"])
    try:
        stderr, started, pinned = _run_watchdog(tmp_path, {
            child.pid: ["/usr/bin/python3", old],                            # a superseded release
            900002: ["grep", "memecoin_cluster_gate_worker.py"],             # not a worker
            900003: ["vim", "memecoin_cluster_gate_worker.py"],              # not a worker
            900004: ["/usr/bin/python3", "/x/memecoin_cluster_gate_worker.py.bak"]})
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        assert child.returncode is None, "the watchdog signalled a pid from a fake process table"
    finally:
        child.kill()
        child.wait()
    assert f"stopping superseded cluster worker pid={child.pid}" in stderr
    assert "900002" not in stderr and "900003" not in stderr and "900004" not in stderr
    assert started == pinned and "/current/" not in started               # physical, not ".../current"


def test_the_cluster_watchdog_keeps_a_live_pinned_worker(tmp_path):
    pinned = str((tmp_path / "releases" / "A" / "scripts").resolve() / "memecoin_cluster_gate_worker.py")
    stderr, started, _ = _run_watchdog(tmp_path, {900005: ["/usr/bin/python3", pinned]})
    assert started is None and "stopping" not in stderr
