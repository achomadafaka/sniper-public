"""Re-read a confirmed buy's receipt soon after it came back unreadable (2026-09-27, Alpha GO G1).

A confirmed Solana entry whose receipt cannot be read ends as an `anomaly` and stays reserved. The
stale-reservation recovery books it from the receipt, but looks at it no earlier than 90 s after
broadcast and runs every 120 s: up to 210 s with no position, so no rails and no mirror exit.

This re-runs the executor's own `recover_confirmed_entry_fn` at +5, +15 and +30 s after the anomaly,
at most three reads, on one daemon thread with its own ledger connection: never the lane thread or
the main loop. The booking is the reconcile pass's: recover_confirmed_entry takes BEGIN IMMEDIATE,
re-checks the reservation is still `reserved` with the same signature and dedupes the position and
fill by attempt_id, so a retry racing the reconcile books one position. A reservation already
resolved elsewhere is not read again. After the third miss the reconcile pass owns it, as before.

MEMECOIN_RECEIPT_RETRY_ENABLED=0 (runner restart) turns scheduling off.
"""
import heapq
import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DELAYS_S = (5.0, 15.0, 30.0)     # after the anomaly, not between attempts


def enabled(env=None):
    env = os.environ if env is None else env
    return str(env.get("MEMECOIN_RECEIPT_RETRY_ENABLED", "1")).strip().lower() not in {
        "0", "false", "no", "off"}


class ReceiptRetry:
    def __init__(self, *, recover_fn, conn_factory, now_fn, delays=DELAYS_S, clock=time.monotonic):
        self._recover = recover_fn
        self._conn_factory = conn_factory
        self._now = now_fn
        self._delays = tuple(delays)
        self._clock = clock
        self._cv = threading.Condition()
        self._heap = []           # (due, pending_id, attempt_index, scheduled_at)
        self._active = set()      # pending ids with a retry in flight
        self._thread = None
        self._closed = False      # close(): tests and an orderly shutdown; the runner never calls it

    def after(self, result, pending_id):
        """Schedule retries for a SIGNED anomaly. Returns True when scheduled. Never raises."""
        try:
            if not (isinstance(result, dict) and result.get("status") == "anomaly"
                    and result.get("sig") and pending_id is not None and enabled()):
                return False
            with self._cv:
                if pending_id in self._active:
                    return False
                self._active.add(pending_id)
                now = self._clock()
                heapq.heappush(self._heap, (now + self._delays[0], pending_id, 0, now))
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, daemon=True,
                                                    name="memecoin-receipt-retry")
                    self._thread.start()
                self._cv.notify()
            return True
        except Exception:
            logger.exception("receipt retry could not be scheduled pending=%s", pending_id)
            return False

    def close(self, timeout_s=5.0):
        """Stop the worker after its current read (house rule b18c16b4); True when it has ended."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout_s)
        return thread is None or not thread.is_alive()

    def _run(self):
        conn = self._conn_factory()
        while True:
            with self._cv:
                while not self._closed and (not self._heap or self._heap[0][0] > self._clock()):
                    wait = self._heap[0][0] - self._clock() if self._heap else None
                    self._cv.wait(timeout=wait)
                if self._closed:
                    conn.close()
                    return
                _due, pending_id, index, scheduled_at = heapq.heappop(self._heap)
            outcome = self._attempt(conn, pending_id)
            logger.warning("%s receipt retry pending=%s attempt=%d/%d outcome=%s",
                           datetime.now(timezone.utc).isoformat(), pending_id, index + 1,
                           len(self._delays), outcome)
            with self._cv:
                if outcome == "unreadable" and index + 1 < len(self._delays):
                    heapq.heappush(self._heap, (scheduled_at + self._delays[index + 1],
                                                pending_id, index + 1, scheduled_at))
                else:
                    self._active.discard(pending_id)

    def _attempt(self, conn, pending_id):
        from sniper.persistence import db_memecoin as m
        try:
            row = m.pending_row(conn, pending_id)
            if not row or row.get("status") != "reserved" or not row.get("tx_sig"):
                return "resolved_elsewhere"
            return "booked" if self._recover(conn, row, self._now()) else "unreadable"
        except Exception:
            logger.exception("receipt retry failed pending=%s", pending_id)
            return "unreadable"
        finally:
            if conn.in_transaction:
                conn.rollback()
