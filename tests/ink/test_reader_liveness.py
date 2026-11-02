"""The Ink Subblocks reader's liveness (Task 11): a heartbeat the watchdog can judge.

The watchdog only checked that a reader PROCESS existed. A hung process, or one whose journal
worker thread had died (frames then pile into the bounded queue and overflow), looked healthy
forever. The reader now writes a heartbeat from its own thread, and the watchdog kills a reader
whose beat is stale or reports a dead thread, with a cooldown; the next pass relaunches it.
"""
import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts.research import onchain_ink_watch as ink  # noqa: E402


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def test_the_heartbeat_carries_frames_and_thread_liveness(tmp_path):
    path = tmp_path / "onchain_ink_watch.heartbeat"
    stop = threading.Event()
    alive, dead = threading.Thread(target=stop.wait), threading.Thread(target=lambda: None)
    alive.start(); dead.start(); dead.join()
    stats = {"frames": 7, "overflows": 1, "sessions": 2, "last_frame_utc": "2026-09-29T20:00:00Z"}
    t = threading.Thread(target=ink.heartbeat_loop,
                         args=(stop, path, stats, {"journal": alive, "projection": dead}),
                         kwargs={"period_s": 0.05})
    t.start()
    for _ in range(100):
        if path.exists():
            break
        stop.wait(0.02)
    stop.set(); t.join(2); alive.join(2)
    beat = json.loads(path.read_text())
    assert beat["frames"] == 7 and beat["overflows"] == 1 and beat["sessions"] == 2
    assert beat["threads"] == {"journal": True, "projection": False}
    assert beat["last_frame_utc"] == "2026-09-29T20:00:00Z" and beat["ts"].endswith("Z")


def test_a_fresh_beat_with_live_threads_is_healthy():
    now = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
    beat = {"ts": _iso(now - timedelta(seconds=10)), "threads": {"journal": True}}
    assert ink.beat_verdict(beat, now.timestamp()) == ""


def test_a_stale_beat_or_a_dead_thread_is_a_kill_reason():
    now = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
    stale = {"ts": _iso(now - timedelta(seconds=200)), "threads": {"journal": True}}
    assert ink.beat_verdict(stale, now.timestamp()).startswith("beat_stale:")
    dead = {"ts": _iso(now), "threads": {"journal": False, "canonical": True, "projection": False}}
    assert ink.beat_verdict(dead, now.timestamp()) == "thread_dead:journal,projection"


def test_an_unreadable_beat_is_not_a_kill_reason(tmp_path):
    """Absent or garbled: the absent-process launcher owns that case, never a kill."""
    assert ink.beat_verdict(None, 0.0) == "" and ink.beat_verdict({"ts": "garbage"}, 0.0) == ""
    out = subprocess.run([sys.executable, str(REPO / "scripts/research/onchain_ink_watch.py"),
                          "--check-beat", str(tmp_path / "absent")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == ""


def test_the_cli_prints_the_kill_reason(tmp_path):
    path = tmp_path / "beat"
    path.write_text(json.dumps({"ts": "2026-09-01T00:00:00Z", "threads": {"journal": True}}))
    out = subprocess.run([sys.executable, str(REPO / "scripts/research/onchain_ink_watch.py"),
                          "--check-beat", str(path)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.startswith("beat_stale:"), out.stderr


def test_the_watchdog_kills_only_its_own_unhealthy_reader_with_a_cooldown():
    """Source-level: the Ink block asks the reader's own --check-beat, kills only pids it matched in
    THIS release tree, and holds a cooldown marker, so a flapping provider cannot kill-loop it."""
    text = (REPO / "scripts/memecoin_watchdog.sh").read_text()
    block = text.split("# Ink's raw Subblocks reader")[1].split("# Check eligibility before")[0]
    assert "--check-beat" in block and "onchain_ink_watch.heartbeat" in block
    assert "onchain_ink_watch.lastkill" in block and "ONCHAIN_KILL_COOLDOWN_S" in block
    assert 'kill $_ink_live_pids' in block
    assert "*[!0-9]*) _ink_last=0" in block, "an empty or partial marker reads as 0, not an arithmetic error"
