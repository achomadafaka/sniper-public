"""Funded exit price tape, part A-lite: one row per exit CONFIRMATION, in its own file.

Spec docs/superpowers/specs/2026-09-30-memecoin-exit-price-tape-design.md §3a (Alpha, 2026-10-01).
`memecoin_route_series` already keeps the exit path's priced whole-bag previews; what it lacks is an
exact key from a decision preview to the attempt it fed, the previews that FAILED, and the lane. This
keeps those, at the one site that matters: the executable confirmation in `real_exit_sweep`.

THE JOIN. `decision_at` is the sweep's `now_iso`. The executor writes the same value as
`memecoin_exit_execution_attempts.attempted_at` (Solana and EVM seams both), so a confirmation pairs
with its attempt on `(position_id, decision_at = attempted_at, reason = exit_reason)`, and the
attempt with its fill on `sig = memecoin_fills.tx_sig`. No time window.

OFF THE FILL PATH. `record_confirm` builds a small dict and appends it to a bounded deque under one
lock: no file, DB or network I/O. A daemon writer drains it every second into its own file on its
own connection. Anything that raises inside `record_confirm` is logged and the row is dropped; it
never reaches the exit. A full queue drops the oldest row and counts it.

Measurement only. Nothing in the funded path reads this file.
"""
from __future__ import annotations

import collections
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_LEDGER_NAME = "sniper_tracker.db"
_MAXLEN = 4096

_COLUMNS = (
    "position_id", "network_id", "mint", "reason", "lane", "decision_at", "allowed",
    "refusal_code", "preview_src", "status", "expected_usdc", "amount_atomic", "quoted_at",
    "provider_id", "provider_group", "venue", "route_target", "expected_output_atomic",
    "context_slot", "preview_ms", "host_at", "host_mono",
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS exit_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    network_id TEXT,
    mint TEXT,
    reason TEXT NOT NULL,
    lane TEXT,
    decision_at TEXT NOT NULL,
    allowed INTEGER,
    refusal_code TEXT,
    preview_src TEXT,
    status TEXT NOT NULL,
    expected_usdc REAL,
    amount_atomic TEXT,
    quoted_at TEXT,
    provider_id TEXT,
    provider_group TEXT,
    venue TEXT,
    route_target TEXT,
    expected_output_atomic TEXT,
    context_slot INTEGER,
    preview_ms REAL,
    host_at TEXT NOT NULL,
    host_mono REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exit_confirmations_join
    ON exit_confirmations(position_id, decision_at);
"""

# Assigned once by `start()` at runner start, before any sweep thread exists; never reassigned.
_TAPE = None


def _is_ledger(path):
    real = os.path.realpath(path)
    ledger = os.path.realpath(os.getenv("DB_PATH") or _LEDGER_NAME)
    return os.path.basename(real) == _LEDGER_NAME or real == ledger


class Tape:
    """The queue and its writer. The writer thread owns the only connection to `path`."""

    def __init__(self, path, maxlen=_MAXLEN):
        if _is_ledger(path):
            raise ValueError("exit tape: refusing the funded ledger %s" % path)
        self.path = str(path)
        self._q = collections.deque(maxlen=int(maxlen))
        self._lock = threading.Lock()
        self.appended = self.dropped = self.written = self.errors = 0
        self._conn = None
        self._thread = None
        self._stop = threading.Event()

    def append(self, row):
        with self._lock:
            if len(self._q) == self._q.maxlen:
                self.dropped += 1
            self._q.append(row)
            self.appended += 1

    def drain_once(self):
        """Write what is queued. Rows leave the queue before the write: a failed batch is logged
        and lost, never retried against a broken file."""
        with self._lock:
            batch = list(self._q)
            self._q.clear()
        if not batch:
            return 0
        try:
            if self._conn is None:
                os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
                self._conn = sqlite3.connect(self.path, timeout=5)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.executescript(_SCHEMA)
            self._conn.executemany(
                "INSERT INTO exit_confirmations(%s) VALUES(%s)"
                % (",".join(_COLUMNS), ",".join("?" * len(_COLUMNS))),
                [tuple(r.get(k) for k in _COLUMNS) for r in batch])
            self._conn.commit()
        except Exception:
            with self._lock:
                self.errors += 1
            logger.exception("exit tape: write failed; %d row(s) dropped", len(batch))
            try:
                if self._conn is not None:
                    self._conn.rollback()
            except Exception:
                logger.exception("exit tape: rollback failed")
            return 0
        with self._lock:
            self.written += len(batch)
        return len(batch)

    def _run(self):
        while not self._stop.wait(1.0):
            try:
                self.drain_once()
            except Exception:                    # drain_once already catches; belt and braces
                logger.exception("exit tape: writer pass failed; the thread continues")

    def start(self):
        self._thread = threading.Thread(target=self._run, name="exit-tape-writer", daemon=True)
        self._thread.start()
        return self

    def snapshot(self):
        with self._lock:
            return {"appended": self.appended, "written": self.written, "dropped": self.dropped,
                    "errors": self.errors, "queued": len(self._q),
                    "alive": bool(self._thread and self._thread.is_alive())}


def start(path=None):
    """Start the writer and make `record_confirm` live. Call once, before the sweep threads.
    The default file sits under the state root, beside the ledger, never inside a release."""
    global _TAPE
    if path is None:
        from sniper.util.state_paths import state_path
        path = str(state_path("data", "memecoin_exit_price_tape.db"))
    _TAPE = Tape(path).start()
    logger.info("exit tape: writing confirmations to %s", path)
    return _TAPE


def snapshot():
    tape = _TAPE
    return tape.snapshot() if tape is not None else None


def _value(preview, name):
    if isinstance(preview, dict):
        return preview.get(name)
    return getattr(preview, name, None)


def _text(value):
    return None if value is None else str(int(value))


def record_confirm(pos, *, reason, decision_at, lane, preview, allowed, detail, preview_src):
    """Queue one confirmation row. Never raises, never does I/O. No tape: a no-op."""
    tape = _TAPE
    if tape is None:
        return
    try:
        failure = _value(preview, "failure")
        expected = _value(preview, "expected_usdc") if preview is not None else None
        if preview is None:
            status = "no_preview"
        elif failure is not None:
            status = str(getattr(failure, "value", failure))
        elif expected is None:
            status = "no_price"
        else:
            status = "ok"
        evidence = _value(preview, "quote_evidence") if preview is not None else None
        evidence = evidence if isinstance(evidence, dict) else {}
        slot = evidence.get("context_slot")
        tape.append({
            "position_id": pos["id"], "network_id": pos.get("network_id"),
            "mint": pos.get("mint"), "reason": reason, "lane": lane, "decision_at": decision_at,
            "allowed": None if allowed is None else int(bool(allowed)),
            "refusal_code": (detail or {}).get("code") if not allowed else None,
            "preview_src": preview_src, "status": status,
            "expected_usdc": None if expected is None else float(expected),
            "amount_atomic": _text(_value(preview, "amount_atomic")) if preview is not None else None,
            "quoted_at": _value(preview, "quoted_at") if preview is not None else None,
            "provider_id": _value(preview, "provider_id") if preview is not None else None,
            "provider_group": _value(preview, "provider_group") if preview is not None else None,
            "venue": evidence.get("venue"), "route_target": evidence.get("route_target"),
            "expected_output_atomic": evidence.get("expected_output_atomic"),
            "context_slot": None if slot is None else int(slot),
            "preview_ms": pos.get("_exit_preview_ms"),
            "host_at": datetime.now(timezone.utc).isoformat(), "host_mono": time.monotonic(),
        })
    except Exception:
        logger.exception("exit tape: confirmation row dropped pos=%s reason=%s",
                         (pos or {}).get("id"), reason)
