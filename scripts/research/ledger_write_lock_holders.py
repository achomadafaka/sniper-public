#!/usr/bin/env python3
"""Name the PROCESS holding the funded ledger's write lock, from /proc/locks only.

Why: on 2026-09-26 two slot-lane sends (4022, 4029) waited 2,331 ms and 830 ms for the ledger write
lock after the transaction was already accepted, and 4029's attempt-shell INSERT stalled 340 ms
before classify, which cost it a slot. The J7 path never waited (n=119, max 121 ms). Something holds
the ledger write lock for 0.8-2.3 s; this names which process.

How: SQLite in WAL mode takes a POSIX fcntl write lock on byte 120 of the -shm file
(WAL_WRITE_LOCK, UNIX_SHM_BASE = (22 + 8) * 4) for the whole write transaction, and byte 121 while
it checkpoints. /proc/locks lists every such lock with the owning PID. This script samples it at a
fixed rate and records each EPISODE: consecutive samples in which one PID held the byte.

An episode is NOT a measured hold. Sampling cannot say how long the lock was held, only bracket it:
`observed_span_ms` (first to last positive sample) and `bracket_ms` (last negative sample before to
first negative sample after). A 1 ms hold caught by one sample reads observed_span_ms 0, bracket
about two periods. CONTINUITY IS UNKNOWN: several short transactions by one PID between samples read
as one episode, so neither number is a minimum duration of one transaction. Use the output to NAME
the holder; quote spans only as brackets.

It opens NO database connection, takes no lock, makes no network call and needs no ptrace. POSIX
locks belong to a process, so every thread of one process (the runner's main loop and its slot lane)
reports as one PID: a runner result needs a thread-level instrument next.

    python3 scripts/research/ledger_write_lock_holders.py --selftest
    python3 scripts/research/ledger_write_lock_holders.py --duration 7200 \
        --out logs/ledger_write_lock_holders.jsonl

Resolution is the sample period (50 ms at the default 20 Hz). Episodes whose bracket (the upper
envelope) is under --min-ms are counted per PID, not written.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WAL_WRITE_BYTE = 120
WAL_CKPT_BYTE = 121
BYTES = {WAL_WRITE_BYTE: "write", WAL_CKPT_BYTE: "checkpoint"}


def _utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def lock_key(shm_path):
    """The maj:min:inode string /proc/locks prints for a file (major/minor in hex)."""
    st = os.stat(shm_path)
    return "%02x:%02x:%d" % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)


def write_holders(key, text):
    """{byte: set(pid)} for WRITE locks on `key` covering bytes 120/121. Waiters ('->') skipped."""
    held = {b: set() for b in BYTES}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 8 or parts[1] == "->" or parts[3] != "WRITE" or parts[5] != key:
            continue
        start = int(parts[6])
        end = int(parts[7]) if parts[7] != "EOF" else float("inf")
        for b in BYTES:
            if start <= b <= end:
                held[b].add(int(parts[4]))
    return held


def _argv(pid, cache):
    if pid not in cache:
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                cache[pid] = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()[:300]
        except OSError:
            cache[pid] = None
    return cache[pid]


def sample(shm_path, *, duration_s, hz, min_ms, emit, stop=None, read_locks=None, key=None,
           clock=time.monotonic, wall=time.time, sleep=time.sleep):
    """Sample until `duration_s` or `stop` is set. emit(row) gets every episode whose bracket is
    >= min_ms and a final summary row. Returns the summary. `key`, `clock`, `wall`, `sleep` and
    `read_locks` are injectable so a deterministic clock can pin the bracket arithmetic."""
    read_locks = read_locks or (lambda: open("/proc/locks").read())
    period = 1.0 / hz
    fixed_key = key is not None
    key, key_at = (key if fixed_key else lock_key(shm_path)), clock()
    episodes = {}          # (byte, pid) -> dict(prev_neg, first_pos, last_pos, n) of (mono, epoch)
    short, long_n, samples, cache = {}, 0, 0, {}
    prev = None            # (mono, epoch) of the previous sample: every open-now pid was absent there
    t0 = clock()
    next_t = t0

    def ms(a, b):
        return None if a is None or b is None else round((b[0] - a[0]) * 1000.0, 1)

    def close(k, first_neg):
        nonlocal long_n
        ep = episodes.pop(k)
        observed, bracket = ms(ep["first_pos"], ep["last_pos"]), ms(ep["prev_neg"], first_neg)
        byte, pid = k
        if max(observed, bracket or 0.0) >= min_ms:
            long_n += 1
            u = lambda t: None if t is None else _utc(t[1])
            emit({"kind": "episode", "lock": BYTES[byte], "pid": pid, "argv": _argv(pid, cache),
                  "prev_neg_utc": u(ep["prev_neg"]), "first_pos_utc": u(ep["first_pos"]),
                  "last_pos_utc": u(ep["last_pos"]), "first_neg_utc": u(first_neg),
                  "positive_samples": ep["n"], "observed_span_ms": observed,
                  "bracket_ms": bracket, "continuity": "unknown",
                  "resolution_ms": round(period * 1000.0, 1)})
        else:
            short[pid] = short.get(pid, 0) + 1

    while not (stop and stop.is_set()):
        now_mono = clock()
        if now_mono - t0 >= duration_s:
            break
        if not fixed_key and now_mono - key_at >= 60.0:   # -shm is recreated if all connections close
            try:
                key, key_at = lock_key(shm_path), now_mono
            except OSError:
                pass
        held = write_holders(key, read_locks())
        now = (clock(), wall())
        samples += 1
        for byte, pids in held.items():
            for pid in pids:
                ep = episodes.get((byte, pid))
                if ep is None:
                    _argv(pid, cache)          # read argv while the pid is alive
                    episodes[(byte, pid)] = {"prev_neg": prev, "first_pos": now, "last_pos": now,
                                             "n": 1}
                else:
                    ep["last_pos"], ep["n"] = now, ep["n"] + 1
        for k in [k for k in episodes if k[1] not in held[k[0]]]:
            close(k, now)
        prev = now
        next_t += period
        sleep(max(0.0, next_t - clock()))
    for k in list(episodes):
        close(k, None)                     # still held at the end: no closing negative sample
    summary = {"kind": "summary", "shm": shm_path, "samples": samples, "hz": hz, "min_ms": min_ms,
               "episodes_at_or_over_min": long_n,
               "short_episodes_by_pid": {str(p): n for p, n in sorted(short.items())},
               "argv": {str(p): _argv(p, cache) for p in short},
               "ended_utc": _utc(wall())}
    emit(summary)
    return summary


def selftest():
    """The instrument must SEE a write lock this process holds on a throwaway DB, report nothing
    once it is released, and never open the funded ledger."""
    rows = []
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.db")
        conn = sqlite3.connect(db, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x)")
        stop = threading.Event()
        th = threading.Thread(target=sample, args=(db + "-shm",), daemon=True,
                              kwargs=dict(duration_s=5.0, hz=50, min_ms=200, emit=rows.append,
                                          stop=stop))
        th.start()
        time.sleep(0.15)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t VALUES (1)")
        time.sleep(0.5)                                   # a 500 ms hold must be recorded
        conn.execute("COMMIT")
        time.sleep(0.3)                                   # released: nothing new may open
        conn.execute("BEGIN IMMEDIATE")                   # a 50 ms hold must stay under 200 ms
        time.sleep(0.05)
        conn.execute("COMMIT")
        time.sleep(0.2)
        stop.set()
        th.join(5)
        conn.close()
    eps = [r for r in rows if r["kind"] == "episode"]
    summary = rows[-1]
    assert len(eps) == 1, rows
    e = eps[0]
    assert e["pid"] == os.getpid() and e["lock"] == "write" and e["continuity"] == "unknown", e
    # A 500 ms hold at 20 ms sampling: observed <= 500 <= bracket, each within one period or two.
    assert 440 <= e["observed_span_ms"] <= 500 <= e["bracket_ms"] <= 560, e
    assert summary["short_episodes_by_pid"].get(str(os.getpid()), 0) >= 1, summary
    _deterministic_brackets()
    # Parser edge cases: waiter lines, other inodes, READ locks and EOF ranges.
    key = "103:02:77"
    text = "\n".join([
        "1: POSIX  ADVISORY  WRITE 11 103:02:77 120 120",
        "2: -> POSIX  ADVISORY  WRITE 12 103:02:77 120 120",
        "3: POSIX  ADVISORY  WRITE 13 103:02:78 120 120",
        "4: POSIX  ADVISORY  READ  14 103:02:77 120 120",
        "5: POSIX  ADVISORY  WRITE 15 103:02:77 121 EOF",
    ])
    assert write_holders(key, text) == {120: {11}, 121: {15}}
    print("selftest ok: own 500 ms hold bracketed [%.0f, %.0f] ms, 50 ms hold ignored, "
          "one-sample episode claims no span" % (e["observed_span_ms"], e["bracket_ms"]))


def _deterministic_brackets():
    """RR-06 (Codex re-review 2026-09-26): a hold seen by ONE sample must not claim a 50 ms span.
    Fake clock, 20 Hz: samples at 0, 50, 100, ... ms; the pid holds only at 100 ms (a 1 ms hold,
    say), and again at 250-350 ms (three positives)."""
    t = [0.0]
    pos = "1: POSIX  ADVISORY  WRITE 42 aa:bb:1 120 120"
    script = iter(["", "", pos, "", "", pos, pos, pos, "", ""])
    rows = []
    sample("unused", duration_s=0.5, hz=20, min_ms=0, emit=rows.append, key="aa:bb:1",
           read_locks=lambda: next(script, ""), clock=lambda: t[0], wall=lambda: 1e9 + t[0],
           sleep=lambda s: t.__setitem__(0, t[0] + s))
    eps = [r for r in rows if r["kind"] == "episode"]
    assert [(e["positive_samples"], e["observed_span_ms"], e["bracket_ms"]) for e in eps] == [
        (1, 0.0, 100.0),       # one sample: observed 0, bracket = the two neighbouring negatives
        (3, 100.0, 200.0),     # 250..350 ms positive, 200 ms and 400 ms negative
    ], eps
    assert all("dur_ms_min" not in e for e in eps)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=os.path.join(REPO, "sniper_tracker.db"))
    ap.add_argument("--duration", type=float, default=7200.0)
    ap.add_argument("--hz", type=float, default=20.0)
    ap.add_argument("--min-ms", type=float, default=200.0)
    ap.add_argument("--out", default=os.path.join(REPO, "logs", "ledger_write_lock_holders.jsonl"))
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return 0
    with open(a.out, "a", buffering=1) as f:
        def emit(row):
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        emit({"kind": "start", "db": a.db, "started_utc": _utc(time.time()), "pid": os.getpid(),
              "duration_s": a.duration, "hz": a.hz, "min_ms": a.min_ms})
        s = sample(a.db + "-shm", duration_s=a.duration, hz=a.hz, min_ms=a.min_ms, emit=emit)
    print(json.dumps(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
