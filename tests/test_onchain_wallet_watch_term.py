"""SOL-3 (Codex review 2026-10-06): TERM/INT reach the watcher's writer shutdown accounting.

The production exit thread called os._exit at once, so queued observations vanished with no
writer_shutdown line. It now gives the writer a bounded drain, records the final counters, and
still exits unconditionally by a deadline. Each case runs the real signal path in its own
subprocess against an offline SQLite file that this test holds locked.
"""
import json
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m

REPO = Path(__file__).resolve().parents[1]
CHILD = r'''
import sys, time
from pathlib import Path
from scripts.research import onchain_wallet_watch as w
path, stages = sys.argv[1], Path(sys.argv[2])
w.STAGE_STAMPS, w.STAGE_LOG, w.send_wake = True, stages, (lambda _: None)
w.log_exit_signals()                                   # before any thread, as main() does
writer = w.DetectionWriter(path, retry_s=30)
w._ACTIVE_WRITER = writer
stamp = "2026-10-02T12:00:00Z"
for i in range(3):
    assert writer.offer(("sig%d" % i, "u", "h", "owner", "B", 100.0, 1.0, 1, stamp, 0.5,
                         None, None, stamp, 100.0, 1.0, "{}", 100), (), {})
while writer.stats["retries"] == 0:
    time.sleep(.01)
print("ready", flush=True)
time.sleep(60)
'''


def _ledger(tmp_path):
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    m.ensure_memecoin_tables(conn)
    conn.commit()
    return path, conn


def _start(tmp_path, path):
    stages = tmp_path / "stages.jsonl"
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(path), str(stages)], cwd=REPO,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env={"PYTHONPATH": str(REPO), "SNIPER_STATE_ROOT": str(tmp_path),
                                  "DB_PATH": str(path), "PATH": "/usr/bin:/bin"})
    assert child.stdout.readline().strip() == b"ready"
    return child, stages


def _shutdown_row(stages):
    rows = [json.loads(x) for x in stages.read_text().splitlines()] if stages.exists() else []
    return next((r for r in rows if r.get("outcome") == "writer_shutdown"), None)


def _term(child):
    started = time.monotonic()
    child.send_signal(signal.SIGTERM)
    code = child.wait(timeout=10)
    return code, time.monotonic() - started


def test_term_with_a_locked_ledger_counts_every_queued_observation(tmp_path):
    from scripts.research import onchain_wallet_watch as w
    path, lock = _ledger(tmp_path)
    lock.execute("BEGIN IMMEDIATE")
    child, stages = _start(tmp_path, path)
    try:
        code, took = _term(child)
    finally:
        child.kill()
        lock.rollback()
    assert code == 128 + signal.SIGTERM and took < w.SHUTDOWN_DEADLINE_S + 1.0
    row = _shutdown_row(stages)
    assert row is not None, "TERM skipped the writer's shutdown accounting"
    stats = row["writer"]
    assert stats["offered"] == 3 and stats["written"] + stats["shutdown_dropped"] == 3
    assert stats["written"] == 0


def test_term_drains_queued_observations_once_the_lock_clears(tmp_path):
    path, lock = _ledger(tmp_path)
    lock.execute("BEGIN IMMEDIATE")
    child, stages = _start(tmp_path, path)
    try:
        child.send_signal(signal.SIGTERM)
        time.sleep(.2)
        lock.rollback()                                  # inside the drain window
        code = child.wait(timeout=10)
    finally:
        child.kill()
    assert code == 128 + signal.SIGTERM
    assert _shutdown_row(stages)["writer"]["written"] == 3
    assert lock.execute("SELECT COUNT(*) FROM onchain_wallet_detections").fetchone()[0] == 3


def test_term_still_exits_by_the_deadline_when_printing_fails(tmp_path):
    from scripts.research import onchain_wallet_watch as w
    path, lock = _ledger(tmp_path)
    lock.execute("BEGIN IMMEDIATE")
    child, stages = _start(tmp_path, path)
    child.stdout.close()                                 # every later print raises
    try:
        code, took = _term(child)
    finally:
        child.kill()
        lock.rollback()
    assert code == 128 + signal.SIGTERM and took < w.SHUTDOWN_DEADLINE_S + 1.0
    assert _shutdown_row(stages) is not None
