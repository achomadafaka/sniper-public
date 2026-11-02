"""Offline Fomo watchdog checks; fake proc tables never signal host processes."""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/memecoin_desk_fomo_intake_watchdog.sh"
WORKER = "memecoin_desk_fomo_intake.py"


class Harness:
    def __init__(self, tmp_path):
        assert SOURCE.is_file(), "Fomo watchdog is not implemented"
        self.root = tmp_path
        self.release, self.state, self.home, self.proc, self.bin = [
            tmp_path / name for name in ("release", "state", "home", "proc", "bin")]
        for path in (self.release / "scripts/lib", self.state, self.proc, self.bin):
            path.mkdir(parents=True)
        self.profile = self.home / ".local/share/sniper/fomo_intake_profile"
        self.profile.mkdir(parents=True, mode=0o700)
        (self.profile / ".sniper-fomo-intake-profile").write_text("fomo-intake-v1\n")
        self.watchdog = self.release / "scripts/watchdog.sh"
        shutil.copyfile(SOURCE, self.watchdog)
        shutil.copyfile(ROOT / "scripts/lib/desk_proc_match.sh", self.release / "scripts/lib/desk_proc_match.sh")
        self.worker = self.release / "scripts" / WORKER
        self.worker.write_text("import json,os,sys\nfrom pathlib import Path\n"
                               "Path(os.environ['SNIPER_STATE_ROOT'],'worker.json').write_text(json.dumps(sys.argv))\n")
        for command in ("dirname", "readlink", "realpath", "stat", "cmp", "flock", "mkdir", "tr", "sed", "tail", "date", "sleep", "nohup"):
            executable = shutil.which(command)
            assert executable, command
            (self.bin / command).symlink_to(executable)
        self.xvfb_run = self.bin / "xvfb-run"
        self.xvfb_run.write_text("#!" + sys.executable + "\nimport json,os,subprocess,sys\nfrom pathlib import Path\n"
                                 "Path(os.environ['SNIPER_STATE_ROOT'],'xvfb.json').write_text(json.dumps(sys.argv[1:]))\n"
                                 "raise SystemExit(subprocess.call(sys.argv[4:]))\n")
        self.xvfb_run.chmod(0o755)
        for name in ("Xvfb", "xauth"):
            path = self.bin / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.bin), "HOME": str(self.home),
                    "SNIPER_STATE_ROOT": str(self.state), "SNIPER_PYTHON": sys.executable,
                    "DESK_FOMO_INTAKE_WATCHDOG_PROC_ROOT": str(self.proc)}

    def process(self, pid=101, *, script=None, profile="explicit", state=None, home=None, cwd=None, args=None):
        proc = self.proc / str(pid)
        proc.mkdir()
        argv = [sys.executable, str(script or self.worker)]
        if args is not None:
            argv += args
        elif profile == "explicit":
            argv += ["--profile-dir", str(self.profile)]
        elif profile != "default":
            argv += ["--profile-dir", str(profile)]
        (proc / "cmdline").write_bytes(b"\0".join(os.fsencode(arg) for arg in argv) + b"\0")
        (proc / "environ").write_bytes(os.fsencode("SNIPER_STATE_ROOT=" + str(state or self.state)) + b"\0" +
                                        os.fsencode("HOME=" + str(home or self.home)) + b"\0")
        (proc / "cwd").symlink_to(cwd or self.release)
        return proc

    def run(self):
        return subprocess.run(["/bin/bash", str(self.watchdog)], env=self.env, text=True,
                              capture_output=True, timeout=15)

    def launched(self):
        end = time.monotonic() + 2
        while time.monotonic() < end and not (self.state / "worker.json").exists():
            time.sleep(0.01)
        return (self.state / "worker.json").exists()


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


def test_launches_one_pinned_headed_worker_under_xvfb(harness):
    h = harness
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert h.launched()
    assert json.loads((h.state / "worker.json").read_text()) == [
        str(h.worker), "--profile-dir", str(h.profile), "--headed", "--loop"]
    assert json.loads((h.state / "xvfb.json").read_text()) == [
        "-a", "-s", "-screen 0 1280x1024x24 -nolisten tcp", sys.executable,
        str(h.worker), "--profile-dir", str(h.profile), "--headed", "--loop"]
    pid = int((h.state / "data/desk/intake/fomo/launcher.pid").read_text())
    assert pid > 0


@pytest.mark.parametrize("guard", ["halt.json", "SingletonLock", "SingletonSocket"])
def test_saved_halt_and_dangling_native_guards_never_get_removed_or_started(harness, guard):
    h = harness
    path = h.profile / guard
    if guard == "halt.json":
        path.write_text('{"reason":"auth_unproven"}')
    else:
        path.symlink_to(h.root / "nonexistent-native-owner")
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert path.exists() or path.is_symlink()
    assert guard in result.stderr and "no start" in result.stderr   # a silent block hid a dead collector for a day (2026-10-30)
    assert not (h.state / "xvfb.json").exists()


@pytest.mark.parametrize("dependency", ["Xvfb", "xvfb-run", "xauth"])
def test_missing_display_dependency_refuses_before_any_rollover(harness, dependency):
    h = harness
    (h.bin / dependency).unlink()
    h.process(script=h.root / "old/scripts" / WORKER)
    result = h.run()
    assert result.returncode != 0
    assert dependency in result.stderr
    assert "stopping" not in result.stderr
    assert not (h.state / "xvfb.json").exists()


@pytest.mark.parametrize("profile", ["explicit", "default", "equal", "relative", "alias"])
def test_same_release_resolved_profile_is_alive(harness, profile):
    h = harness
    if profile == "equal":
        h.process(args=["--profile-dir=" + str(h.profile)])
    elif profile == "relative":
        h.process(args=["--profile-dir", os.path.relpath(h.profile, h.release)])
    elif profile == "alias":
        alias = h.home / "profile-alias"
        alias.symlink_to(h.profile, target_is_directory=True)
        h.process(profile=alias)
    else:
        h.process(profile=profile)
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert not (h.state / "xvfb.json").exists()


@pytest.mark.parametrize("old", [False, True])
def test_another_profile_is_untouched_and_does_not_count_as_alive(harness, old):
    h = harness
    other = h.root / "another-profile"
    other.mkdir()
    proc = h.process(script=h.root / "old/scripts" / WORKER if old else h.worker, profile=other)
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert h.launched()
    assert proc.exists()
    assert "stopping" not in result.stderr


@pytest.mark.parametrize("kind", ["other_state", "missing_environment", "partial_cmdline", "unknown_profile", "default_other_home"])
def test_unproved_or_foreign_identity_has_no_rollover_authority(harness, kind):
    h = harness
    proc = h.process(script=h.root / "old/scripts" / WORKER,
                     state=h.root / "other-state" if kind == "other_state" else None,
                     home=h.root / "other-home" if kind == "default_other_home" else None,
                     profile="default" if kind == "default_other_home" else "explicit")
    if kind == "missing_environment":
        (proc / "environ").unlink()
    elif kind == "partial_cmdline":
        (proc / "cmdline").write_bytes((proc / "cmdline").read_bytes().rstrip(b"\0"))
    elif kind == "unknown_profile":
        (proc / "cmdline").write_bytes(os.fsencode(sys.executable) + b"\0" +
                                        os.fsencode(h.root / "old/scripts" / WORKER) + b"\0--prof=elsewhere\0")
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert h.launched()
    assert "stopping" not in result.stderr
    assert proc.exists()


def test_superseded_canonical_worker_must_exit_before_replacement(harness):
    h = harness
    h.process(script=h.root / "old/scripts" / WORKER)
    # A fake proc table can never signal. Keep its worker present through the bounded wait.
    (h.bin / "sleep").unlink()
    (h.bin / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (h.bin / "sleep").chmod(0o755)
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert "still exiting" in result.stderr
    assert not (h.state / "xvfb.json").exists()


def test_collector_lock_and_watchdog_lock_prevent_duplicate_launch(harness):
    h = harness
    h.process(pid=102, profile=h.root / "foreign-profile")
    for path in (h.profile / "collector.lock", h.state / "data/desk/intake/fomo/watchdog.lock"):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = h.run()
            assert result.returncode == 0, result.stderr
            assert not (h.state / "xvfb.json").exists()


def test_stale_launcher_pid_is_never_signal_authority(harness):
    h = harness
    data = h.state / "data/desk/intake/fomo"
    data.mkdir(parents=True)
    (data / "launcher.pid").write_text(str(os.getpid()))
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert h.launched()
    assert "stopping" not in result.stderr


@pytest.mark.parametrize("same_profile", [True, False])
def test_pending_display_launcher_is_bound_to_exact_command_state_and_profile(harness, same_profile):
    h = harness
    profile = h.profile if same_profile else h.root / "other-profile"
    h.process(script=h.xvfb_run, args=[
        "-a", "-s", "-screen 0 1280x1024x24 -nolisten tcp", sys.executable,
        str(h.worker), "--profile-dir", str(profile), "--headed", "--loop"])
    result = h.run()
    assert result.returncode == 0, result.stderr
    if same_profile:
        assert not h.launched()
    else:
        assert h.launched()
    assert "stopping" not in result.stderr


@pytest.mark.parametrize("native_guard_remains", [False, True])
def test_rollover_rechecks_chrome_ownership_after_the_old_worker_exits(harness, native_guard_remains):
    h = harness
    proc = h.process(script=h.root / "old/scripts" / WORKER)
    h.env["FAKE_OLD_PROC"] = str(proc)
    h.env["FAKE_PROFILE"] = str(h.profile)
    h.env["FAKE_NATIVE_GUARD"] = str(int(native_guard_remains))
    (h.bin / "sleep").unlink()
    (h.bin / "sleep").write_text("#!" + sys.executable + "\nimport os,shutil\nfrom pathlib import Path\n"
                                 "shutil.rmtree(os.environ['FAKE_OLD_PROC'],ignore_errors=True)\n"
                                 "if os.environ['FAKE_NATIVE_GUARD']=='1':\n"
                                 "    Path(os.environ['FAKE_PROFILE'],'SingletonLock').symlink_to('/nonexistent-test-owner')\n")
    (h.bin / "sleep").chmod(0o755)
    result = h.run()
    assert result.returncode == 0, result.stderr
    assert not proc.exists()
    if native_guard_remains:
        assert not h.launched()
        assert (h.profile / "SingletonLock").is_symlink()
    else:
        assert h.launched()
