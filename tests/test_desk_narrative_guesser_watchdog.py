"""J7-only launcher identity (Delta c5eebabff F1/F2): a fake process table, nothing is signalled or started for real (SNIPER_PYTHON=/bin/false)."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts" / "memecoin_desk_narrative_guesser_worker.py"
WATCHDOG = ROOT / "scripts" / "memecoin_desk_narrative_guesser_watchdog.sh"


def table(tmp_path, procs):
    proc = tmp_path / "proc"
    for pid, spec in procs.items():
        argv, env_root = spec[0], spec[1]
        p = proc / str(pid)
        p.mkdir(parents=True)
        if len(spec) > 2 and spec[2] is not None:
            (p / "cwd").symlink_to(spec[2])                                          # the process's own working directory
        (p / "cmdline").write_bytes(b"".join(os.fsencode(a) + b"\0" for a in argv))
        (p / "environ").write_bytes(b"SNIPER_STATE_ROOT=" + os.fsencode(env_root) + b"\0")
    proc.mkdir(exist_ok=True)
    return proc


def run(tmp_path, procs):
    state = tmp_path / "state"
    proc = table(tmp_path, procs(state))
    env = {**os.environ, "SNIPER_STATE_ROOT": str(state), "SNIPER_PYTHON": "/bin/false", "DESK_NARRATIVE_GUESSER_WATCHDOG_PROC_ROOT": str(proc)}
    r = subprocess.run(["bash", str(WATCHDOG)], env=env, capture_output=True, text=True, timeout=20)
    return r, (state / "data" / "desk" / "narrative_guesser_worker.pid").exists()


def argv(state, *extra, worker=WORKER):
    return ["python3", str(worker), "--state-dir", str(state / "data" / "desk"), *extra]


def test_a_compliant_worker_is_alive_and_nothing_is_started(tmp_path):
    r, started = run(tmp_path, lambda s: {12345: (argv(s, "--no-model"), s)})
    assert r.returncode == 0 and not started and "stopping" not in r.stderr


def test_a_same_path_worker_without_no_model_is_never_alive_and_is_stopped(tmp_path):
    r, started = run(tmp_path, lambda s: {12345: (argv(s), s)})
    assert r.returncode == 0 and started                                           # a J7-only worker is started instead
    assert "stopping non-J7-only or superseded narrative-guesser worker pid=12345" in r.stderr


def test_a_worker_serving_another_state_dir_is_not_alive(tmp_path):
    r, started = run(tmp_path, lambda s: {12345: (["python3", str(WORKER), "--state-dir", str(s.parent / "other" / "data" / "desk"), "--no-model"], s)})
    assert r.returncode == 0 and started and "stopping" not in r.stderr            # not counted, not killed


def test_state_dir_equals_form_and_no_state_dir_at_all(tmp_path):
    r, started = run(tmp_path, lambda s: {12345: (["python3", str(WORKER), "--state-dir=%s" % (s / "data" / "desk"), "--no-model"], s)})
    assert not started                                                              # --state-dir=VALUE is accepted
    r, started = run(tmp_path / "b", lambda s: {12345: (["python3", str(WORKER), "--no-model"], s)})
    assert started                                                                  # no --state-dir: the default dir is not proven to be ours


def test_another_releases_copy_is_superseded_and_a_foreign_state_root_is_ignored(tmp_path):
    other = tmp_path / "old_release" / "scripts" / WORKER.name
    r, started = run(tmp_path, lambda s: {111: (argv(s, "--no-model", worker=other), s), 222: (argv(s, "--no-model"), tmp_path / "foreign_root")})
    assert started and "pid=111" in r.stderr and "pid=222" not in r.stderr


def test_f3_a_foreign_state_dir_is_never_signalled_even_for_a_model_capable_or_other_release_copy(tmp_path):
    other = tmp_path / "old_release" / "scripts" / WORKER.name
    foreign = ["python3", str(WORKER), "--state-dir", str(tmp_path / "foreign" / "data" / "desk")]
    r, started = run(tmp_path, lambda s: {111: (foreign, s),                                              # same path, no --no-model, FOREIGN dir
                                         222: (["python3", str(other), "--state-dir", str(tmp_path / "foreign" / "data" / "desk"), "--no-model"], s)})   # other release, foreign dir
    assert started and "stopping" not in r.stderr                                                         # neither is signalled; a J7-only worker for OUR dir is started


def test_f3_ours_is_still_stopped_when_it_serves_this_state_dir(tmp_path):
    other = tmp_path / "old_release" / "scripts" / WORKER.name
    r, started = run(tmp_path, lambda s: {111: (argv(s), s), 222: (argv(s, "--no-model", worker=other), s)})
    assert started and "pid=111" in r.stderr and "pid=222" in r.stderr


def test_f4_a_relative_state_dir_is_resolved_against_the_workers_own_cwd(tmp_path):
    ours_cwd, theirs_cwd = tmp_path / "state" , tmp_path / "elsewhere"
    ours_cwd.mkdir(parents=True)
    theirs_cwd.mkdir(parents=True)
    rel = ["python3", str(WORKER), "--state-dir", "data/desk", "--no-model"]
    r, started = run(tmp_path, lambda s: {12345: (rel, s, ours_cwd)})
    assert not started and "stopping" not in r.stderr                                                    # cwd = state root: data/desk IS ours -> alive
    r, started = run(tmp_path / "b", lambda s: {12345: (rel, s, theirs_cwd)})
    assert started                                                                                        # same argv, cwd elsewhere: serves another root, NOT ours
    r, started = run(tmp_path / "c", lambda s: {12345: (rel, s)})
    assert started and "stopping" not in r.stderr                                                         # cwd unreadable: unknown = not ours, not signalled
