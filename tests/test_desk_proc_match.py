"""scripts/lib/desk_proc_match.sh: a desk worker is ours only when the interpreter, the exact script
argument AND the process's state root match (Gamma (A), Alpha 2026-10-21). Fake /proc tables only."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "lib" / "desk_proc_match.sh"


def _proc(table, pid, argv, *, environ=None, cwd=None):
    d = table / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    if environ is not None:
        (d / "environ").write_bytes(b"".join(("%s=%s" % kv).encode() + b"\0" for kv in environ.items()))
    if cwd is not None:
        (d / "cwd").symlink_to(cwd)


def _scan(table, worker, state, default="/home/bigpapi/Desktop/Sniper"):
    out = subprocess.run(
        ["env", "DESK_DEFAULT_STATE_ROOT=%s" % default, "bash", "-c", 'set -euo pipefail; source "$1"; desk_proc_scan "$2" "$3" "$4"; '
         'echo "alive=$DESK_ALIVE"; echo "superseded=${DESK_SUPERSEDED[*]:-}"; '
         'desk_proc_term "$2" 999999 && echo signalled || echo not_signalled',
         "_", str(HELPER), str(table), str(worker), str(state)],
        capture_output=True, text=True, check=True, timeout=10).stdout.splitlines()
    return dict(line.split("=", 1) for line in out[:2]), out[2]


def test_only_this_state_roots_copies_count(tmp_path):
    state, other = tmp_path / "state", tmp_path / "other"
    state.mkdir(), other.mkdir()
    rel = tmp_path / "rel" / "scripts" / "w.py"
    old = tmp_path / "old" / "scripts" / "w.py"
    table = tmp_path / "proc"
    _proc(table, 101, ["/usr/bin/python3", str(old)], environ={"SNIPER_STATE_ROOT": str(state)})
    _proc(table, 102, ["/usr/bin/python3", str(old)], environ={"SNIPER_STATE_ROOT": str(other)})
    _proc(table, 103, ["/usr/bin/python3", str(old)], cwd=state)              # environ unreadable
    _proc(table, 104, ["/usr/bin/python3", str(old)], environ={"SNIPER_STATE_ROOT": str(state / "x" / "..")})
    _proc(table, 105, ["/usr/bin/python3", str(old)], environ={"HOME": "/x"})   # unset: the default root
    _proc(table, 106, ["vim", str(old)], environ={"SNIPER_STATE_ROOT": str(state)})        # not an interpreter
    _proc(table, 107, ["/usr/bin/python3", str(old) + ".bak"], environ={"SNIPER_STATE_ROOT": str(state)})
    _proc(table, 108, ["/usr/bin/python3", str(rel)], environ={"SNIPER_STATE_ROOT": str(other)})
    got, signal = _scan(table, rel, state)
    assert got == {"alive": "0", "superseded": "101 104"}     # another root's copy of WORKER is not alive
    assert signal == "not_signalled"                          # a fake table never signals
    _proc(table, 109, ["/usr/bin/python3", str(rel)], environ={"SNIPER_STATE_ROOT": str(state) + "/"})
    assert _scan(table, rel, state)[0]["alive"] == "1"        # the same root, spelled with a slash
    assert _scan(table, rel, state, default=str(state))[0]["superseded"] == "101 104 105"


def test_every_desk_watchdog_uses_the_helper_and_keeps_a_proc_seam():
    for name in ("assembler", "cluster", "crawler", "dexscreener", "judge", "narrative", "offchain", "ranker",
                 "trending", "universe", "x_producer"):
        text = (ROOT / "scripts" / ("memecoin_desk_%s_watchdog.sh" % name)).read_text()
        assert 'source "$REPO_ROOT/scripts/lib/desk_proc_match.sh"' in text, name
        assert 'desk_proc_scan "$PROC_ROOT" "$WORKER" "$STATE_ROOT"' in text, name
        assert "PROC_ROOT=\"${DESK_" in text and "kill -TERM" not in text, name


def test_the_x_watchdog_restarts_unless_the_receiver_itself_runs(tmp_path):
    receiver = tmp_path / "scripts" / "run_memecoin_x_receiver.py"
    table = tmp_path / "proc"
    _proc(table, 201, ["vim", str(receiver)], environ={})                          # an editor
    _proc(table, 202, ["grep", "run_memecoin_x_receiver.py"], environ={})          # a grep
    check = ['env', 'DESK_DEFAULT_STATE_ROOT=%s' % tmp_path, 'bash', '-c',
             'source "$1"; desk_proc_running "$2" "$3" "$4"', '_', str(HELPER), str(table), str(receiver),
             str(tmp_path)]
    assert subprocess.run(check, timeout=10).returncode == 1                        # down: the watchdog starts it
    _proc(table, 203, ["/usr/bin/python3", str(receiver), "--port", "8787"], environ={"HOME": "/x"})
    assert subprocess.run(check, timeout=10).returncode == 0                        # up, default root
    text = (ROOT / "scripts" / "memecoin_x_watchdog.sh").read_text()
    assert "pgrep -f \"" not in text and 'desk_proc_running "$PROC_ROOT" "$ROOT/scripts/run_memecoin_x_receiver.py" "$ROOT"' in text


def test_a_fake_table_never_signals_a_real_pid(tmp_path):
    # Gamma LOW: the fake entry names this test's own live sleeper as a superseded copy
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        table = tmp_path / "proc"
        _proc(table, sleeper.pid, ["/usr/bin/python3", str(tmp_path / "old" / "w.py")],
              environ={"SNIPER_STATE_ROOT": str(tmp_path)})
        out = subprocess.run(
            ["bash", "-c", 'source "$1"; desk_proc_scan "$2" "$3" "$4"; '
             'for p in "${DESK_SUPERSEDED[@]}"; do desk_proc_term "$2" "$p" && echo "signalled $p"; done; true',
             "_", str(HELPER), str(table), str(tmp_path / "rel" / "w.py"), str(tmp_path)],
            capture_output=True, text=True, check=True, timeout=10).stdout
        assert out == ""
        try:
            sleeper.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        assert sleeper.returncode is None, "a fake process table signalled a real pid"
    finally:
        sleeper.kill()
        sleeper.wait()
