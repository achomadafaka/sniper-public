"""G4b: model-arm launcher mode + stale-heartbeat page. A fake process table; the python launched is a recorder script, nothing real starts, nothing real is paged."""
import json
import os
import subprocess
from pathlib import Path

from sniper.services import memecoin_desk_narrative_guesser_page as P

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts" / "memecoin_desk_narrative_guesser_worker.py"
WATCHDOG = ROOT / "scripts" / "memecoin_desk_narrative_guesser_watchdog.sh"


def table(tmp_path, procs):
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    for pid, (argv, root) in procs.items():
        p = proc / str(pid)
        p.mkdir()
        (p / "cmdline").write_bytes(b"".join(os.fsencode(a) + b"\0" for a in argv))
        (p / "environ").write_bytes(b"SNIPER_STATE_ROOT=" + os.fsencode(root) + b"\0")
    return proc


def run(tmp_path, procs, mode):
    state = tmp_path / "state"
    rec = tmp_path / "rec.sh"
    rec.write_text('#!/bin/sh\necho "$@" >> "%s"\n' % (tmp_path / "launched.txt"))
    rec.chmod(0o755)
    env = {**os.environ, "SNIPER_STATE_ROOT": str(state), "SNIPER_PYTHON": str(rec), "DESK_NARRATIVE_GUESSER_WATCHDOG_PROC_ROOT": str(table(tmp_path, procs(state)))}
    if mode:
        env["DESK_NARRATIVE_GUESSER_MODE"] = mode
    r = subprocess.run(["bash", str(WATCHDOG)], env=env, capture_output=True, text=True, timeout=20)
    import time
    time.sleep(0.4)                                                             # the launch is nohup'd in the background
    launched = (tmp_path / "launched.txt").read_text() if (tmp_path / "launched.txt").exists() else ""
    return r, launched


def argv(state, *extra):
    return ["python3", str(WORKER), "--state-dir", str(state / "data" / "desk"), *extra]


def test_model_mode_launches_the_worker_without_no_model_and_pages_helper_runs(tmp_path):
    r, launched = run(tmp_path, lambda s: {}, "model")
    worker_lines = [l for l in launched.splitlines() if "narrative_guesser_worker" in l]
    assert len(worker_lines) == 1 and "--no-model" not in worker_lines[0] and "--state-dir" in worker_lines[0]
    assert any("narrative_guesser_page.py" in l and "--started-this-run" in l for l in launched.splitlines())


def test_model_mode_a_model_capable_worker_is_alive_and_nothing_is_started(tmp_path):
    r, launched = run(tmp_path, lambda s: {101: (argv(s), s)}, "model")
    assert [l for l in launched.splitlines() if "narrative_guesser_worker" in l] == [] and "STOP" not in r.stderr


def test_model_mode_supersedes_a_j7_only_copy_serving_this_state_dir(tmp_path):
    r, launched = run(tmp_path, lambda s: {102: (argv(s, "--no-model"), s)}, "model")
    assert "stopping" in r.stderr and "pid=102" in r.stderr
    assert len([l for l in launched.splitlines() if "narrative_guesser_worker" in l]) == 1          # and a model-capable one is started in its place


def test_model_mode_never_touches_a_worker_serving_a_foreign_state_dir(tmp_path):
    foreign = tmp_path / "other"
    r, launched = run(tmp_path, lambda s: {103: (["python3", str(WORKER), "--state-dir", str(foreign / "data" / "desk"), "--no-model"], s)}, "model")
    assert "pid=103" not in r.stderr


def test_default_mode_is_still_j7_only_and_an_unknown_mode_is_refused(tmp_path):
    r, launched = run(tmp_path, lambda s: {}, None)
    assert any("--no-model" in l for l in launched.splitlines() if "narrative_guesser_worker" in l) and "narrative_guesser_page.py" not in launched
    (tmp_path / "x").mkdir()
    r2, _ = run(tmp_path / "x", lambda s: {}, "bogus")
    assert r2.returncode == 64


# ---- stale-heartbeat page
def beat(base, ts):
    base.mkdir(parents=True, exist_ok=True)
    (base / "worker.heartbeat.json").write_text(json.dumps({"ts": ts}))


class Sink:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def __call__(self, text):
        self.sent.append(text)
        return self.ok


def test_a_fresh_heartbeat_pages_nothing(tmp_path):
    beat(tmp_path, 1000.0)
    s = Sink()
    assert P.check(tmp_path, 1060.0, s) == "ok" and s.sent == []


def test_stale_over_120s_pages_once_then_recovery_pages_once(tmp_path):
    beat(tmp_path, 1000.0)
    s = Sink()
    assert P.check(tmp_path, 1119.0, s) == "ok"                                      # 119 s: not yet
    assert P.check(tmp_path, 1121.0, s) == "paged_stale" and len(s.sent) == 1
    assert P.check(tmp_path, 1500.0, s) == "stale" and len(s.sent) == 1               # no storm
    beat(tmp_path, 1600.0)
    assert P.check(tmp_path, 1605.0, s) == "paged_recovered" and len(s.sent) == 2
    assert P.check(tmp_path, 1610.0, s) == "ok" and len(s.sent) == 2


def test_a_failed_send_is_retried_next_minute_not_swallowed(tmp_path):
    beat(tmp_path, 1000.0)
    bad, good = Sink(ok=False), Sink()
    assert P.check(tmp_path, 1500.0, bad) == "send_failed"
    assert P.check(tmp_path, 1560.0, good) == "paged_stale" and len(good.sent) == 1


def test_missing_unreadable_malformed_and_future_heartbeats_are_stale_with_a_typed_reason(tmp_path):
    for i, (content, reason) in enumerate(((None, "no_heartbeat"), ("{not json", "heartbeat_unreadable"), (json.dumps({"ts": "x"}), "heartbeat_malformed"),
                                           (json.dumps({"ts": 9e9}), "heartbeat_in_future"), (json.dumps({"ts": True}), "heartbeat_malformed"))):
        base = tmp_path / str(i)
        base.mkdir()
        if content is not None:
            (base / "worker.heartbeat.json").write_text(content)
        s = Sink()
        assert P.check(base, 5000.0, s) == "paged_stale" and reason in s.sent[0]


def test_a_worker_started_this_minute_is_judged_next_minute(tmp_path):
    s = Sink()
    assert P.check(tmp_path, 5000.0, s, started_this_run=True) == "skipped_just_started" and s.sent == []
    assert P.check(tmp_path, 5060.0, s) == "paged_stale" and len(s.sent) == 1


def test_a_crash_looping_worker_that_never_writes_a_heartbeat_still_pages_after_the_grace(tmp_path):         # Delta 42eb82f3e
    sent = []
    send = lambda text: (sent.append(text), True)[1]
    results = [P.check(tmp_path, now, send, started_this_run=True) for now in (5000, 5060, 5119, 5121, 5180)]
    assert results == ["skipped_just_started", "skipped_just_started", "skipped_just_started", "paged_stale", "stale"] and len(sent) == 1


def test_a_healthy_beat_clears_the_grace_clock_so_a_later_restart_gets_a_fresh_grace(tmp_path):
    send = lambda text: True
    P.check(tmp_path, 5000, send, started_this_run=True)                                                       # grace starts
    beat(tmp_path, 5050.0)
    assert P.check(tmp_path, 5055, send) == "ok" and json.loads((tmp_path / "page_state.json").read_text())["grace_since"] is None
    (tmp_path / "worker.heartbeat.json").unlink()
    assert P.check(tmp_path, 9000, send, started_this_run=True) == "skipped_just_started"                      # a new restart long after: fresh grace, no false page
    assert P.check(tmp_path, 9100, send, started_this_run=True) == "skipped_just_started"


def test_the_grace_expiry_page_that_fails_to_send_is_retried_and_recovery_still_pages_once(tmp_path):
    bad, good = Sink(ok=False), Sink()
    P.check(tmp_path, 5000, good, started_this_run=True)
    assert P.check(tmp_path, 5200, bad, started_this_run=True) == "send_failed"
    assert P.check(tmp_path, 5260, good, started_this_run=True) == "paged_stale" and len(good.sent) == 1
    beat(tmp_path, 5300.0)
    assert P.check(tmp_path, 5305, good) == "paged_recovered" and len(good.sent) == 2


def test_a_worker_that_fails_before_creating_its_directory_still_gets_a_bounded_grace_and_a_page(tmp_path):    # Delta 9b8fbe4b7
    base = tmp_path / "never_created" / "narrative_guess"
    sent = []
    send = lambda text: (sent.append(text), True)[1]
    results = [P.check(base, now, send, started_this_run=True) for now in (5000, 5060, 5121)]
    assert results == ["skipped_just_started", "skipped_just_started", "paged_stale"] and len(sent) == 1 and (base / "page_state.json").exists()


def test_the_page_env_comes_from_the_state_root_not_only_the_release_dir(tmp_path):                           # found by the real drill: a release dir has no .env
    state, release = tmp_path / "state", tmp_path / "release"
    state.mkdir()
    release.mkdir()
    (state / ".env").write_text("TELEGRAM_BOT_TOKEN=tok\nTELEGRAM_CHAT_ID=42\n")
    env = {"SNIPER_STATE_ROOT": str(state)}
    assert P.load_page_env(release, env) == [".env"] and env["TELEGRAM_BOT_TOKEN"] == "tok" and env["TELEGRAM_CHAT_ID"] == "42"
    (release / ".env").write_text("TELEGRAM_BOT_TOKEN=other\nEXTRA=1\n")
    env2 = {"SNIPER_STATE_ROOT": str(state)}
    P.load_page_env(release, env2)
    assert env2["TELEGRAM_BOT_TOKEN"] == "tok" and env2["EXTRA"] == "1"                                      # the state root wins, the release file only fills gaps
    assert P.load_page_env(release, {}) == [".env"]                                                           # no state root set: the release dir alone still loads
