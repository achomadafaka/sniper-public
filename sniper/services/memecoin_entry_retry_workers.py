"""Per-chain retry scheduling, independent of the main loop's exit/research sweep.

No fresh-entry work runs here. Each worker owns a connection and a nonblocking pass lock; the
main loop covers a dead worker with the same lock, never a duplicate retry. Existing due times,
windows, gates, reservation and execution ownership stay in run_entry_retry_lane.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class _State:
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_pass: float | None = None
    error: str | None = None
    retried: int = 0
    filled: int = 0


def _empty():
    return {"retried": 0, "filled": 0, "gave_up": 0}


class EntryRetryWorkers:
    POLL_S = 1.0
    STALE_S = 15.0

    def __init__(self, networks, *, conn_factory, hook, now_fn, finish_fn=None,
                 clock=time.monotonic):
        self.states = {str(n): _State() for n in sorted(set(networks))}
        self.conn_factory, self.hook, self.now_fn = conn_factory, hook, now_fn
        self.finish_fn, self.clock = finish_fn, clock
        self.stopping = threading.Event()
        self.threads = []

    def pass_network(self, network, conn, *, background):
        from sniper.services.memecoin_real_runner import run_entry_retry_lane
        state = self.states[network]
        if not state.lock.acquire(blocking=False):
            return _empty()
        try:
            result = run_entry_retry_lane(conn, self.hook, self.now_fn(),
                                          network_id=network, include_dips=False)
            state.retried += result["retried"]
            state.filled += result["filled"]
            state.error = None
            if background:
                state.last_pass = self.clock()
            return result
        except Exception as exc:
            state.error = type(exc).__name__
            raise
        finally:
            state.lock.release()

    def fallback(self, conn, now_iso):
        from sniper.services.memecoin_real_runner import _run_dip_hits
        _run_dip_hits(conn, self.hook, now_iso)  # preserve the existing dip-lane fallback
        result = _empty()
        for network, state in self.states.items():
            if (state.error is None and state.last_pass is not None
                    and self.clock() - state.last_pass < self.STALE_S):
                continue
            tally = self.pass_network(network, conn, background=False)
            for key in result:
                result[key] += tally[key]
        return result

    def _next_wait(self, network, conn):
        now = self.now_fn()
        # Due/busy rows keep the ordinary poll; only known future deadlines shorten it.
        row = conn.execute(
            "SELECT MIN(next_attempt_at) FROM memecoin_entry_retry_queue "
            "WHERE network_id=? AND dip_json IS NULL AND next_attempt_at>?", (network, now)).fetchone()
        if row[0] is None:
            return self.POLL_S
        try:
            current, due = datetime.fromisoformat(now), datetime.fromisoformat(row[0])
            if current.tzinfo is None or due.tzinfo is None:
                return self.POLL_S
            return min(self.POLL_S, max(.01, (due - current).total_seconds()))
        except (TypeError, ValueError):
            return self.POLL_S

    def _worker(self, network):
        conn = None
        try:
            while not self.stopping.is_set():
                wait_s = self.POLL_S
                try:
                    if conn is None:
                        conn = self.conn_factory()
                    self.pass_network(network, conn, background=True)
                    wait_s = self._next_wait(network, conn)
                    if self.finish_fn is not None:
                        self.finish_fn(conn)
                except Exception as exc:
                    self.states[network].error = type(exc).__name__
                    logger.exception("entry retry worker %s failed", network)
                    if conn is not None:
                        conn.close()
                        conn = None
                self.stopping.wait(wait_s)
        finally:
            if conn is not None:
                conn.close()

    def start(self):
        for network in self.states:
            thread = threading.Thread(target=self._worker, args=(network,), daemon=True,
                                      name="entry-retry-" + network)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        self.stopping.set()
        for thread in self.threads:
            thread.join(timeout=3.0)

    def status(self):
        now = self.clock()
        return {network: {"healthy": state.error is None and state.last_pass is not None
                         and now - state.last_pass < self.STALE_S,
                         "last_pass_age_s": None if state.last_pass is None else
                         round(max(0.0, now - state.last_pass), 3),
                         "busy": state.lock.locked(), "error": state.error,
                         "retried": state.retried, "filled": state.filled}
                for network, state in self.states.items()}
