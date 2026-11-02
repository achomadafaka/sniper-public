"""The pump / capture-v2 research pass, run on its own thread with its own ledger connection.

WHY OFF THE MAIN LOOP (2026-09-25). This pass ran inline in the funded runner loop every 30 s and
fetches DexScreener. At 18:30Z a host DNS stall held that fetch ~100 s inside getaddrinfo. The loop
that also runs the exit sweep and the entry-retry lane stopped, the watchdog killed the runner for
`sweep_stale` at 18:32:02Z, and a funded buy (CROWD, OmakaseOnly) lost its retry window. The same
pass also wrote to the funded connection before the fetch, holding the write lock across it.

It now runs here: a stall costs research samples, never exits or retries. Every write is committed
before any network wait. The trace interval from BEGIN to COMMIT/ROLLBACK is measured;
it is not a write-lock duration and excludes completion of the terminal statement.
"""
from __future__ import annotations

import collections
import os
import threading
import time

_HOLDS_KEPT = 500


class TransactionSpanMeter:
    """BEGIN-to-COMMIT/ROLLBACK trace interval on the research connection.

    Deferred BEGIN need not acquire a writer lock; the terminal trace runs before completion.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._began = None
        self._lock = threading.Lock()
        self._holds_ms = collections.deque(maxlen=_HOLDS_KEPT)

    def trace(self, statement):
        head = str(statement or "").lstrip()[:8].upper()
        if head.startswith("BEGIN"):
            self._began = self._clock()
        elif head.startswith(("COMMIT", "ROLLBACK")) and self._began is not None:
            held = (self._clock() - self._began) * 1000.0
            self._began = None
            with self._lock:
                self._holds_ms.append(held)

    def snapshot(self):
        with self._lock:
            holds = sorted(self._holds_ms)
        if not holds:
            return {"transaction_span_n": 0, "transaction_span_ms_p50": None, "transaction_span_ms_p90": None,
                    "transaction_span_ms_max": None}
        return {"transaction_span_n": len(holds),
                "transaction_span_ms_p50": round(holds[len(holds) // 2], 1),
                "transaction_span_ms_p90": round(holds[min(len(holds) - 1, int(len(holds) * 0.9))], 1),
                "transaction_span_ms_max": round(holds[-1], 1)}


def research_capture_pass(conn, now_iso, *, v2_enabled, pump_enabled, log=print):
    """One pass, exactly the work the main loop used to do inline, committed before each fetch."""
    v2 = None
    if v2_enabled:
        from sniper.services.memecoin_capture_v2 import capture_v2_pass
        v2 = capture_v2_pass(conn, now_iso)
        conn.commit()
        # Shadow-only learning reuses persisted evidence and performs no network request.
        from sniper.services.memecoin_shadow_signals import evaluate_shadow_signals
        shadow = evaluate_shadow_signals(conn, now_iso)
        if shadow["created"] or shadow["collisions"]:
            log("[memecoin] shadow signals: " + str(shadow))
        conn.commit()
    if pump_enabled:
        from sniper.services.memecoin_pump import pump_capture_pass
        if v2 is not None:
            # Reuse the same network-keyed batch for legacy marks during transition.
            pump_capture_pass(conn, now_iso, stats_batch_fn=lambda _mints: v2.price_map)
        else:
            pump_capture_pass(conn, now_iso)
        conn.commit()


class ResearchCaptureWorker:
    """Loop state for the heartbeat: a stuck research thread is visible without stalling anything."""

    def __init__(self, *, period_s, connect, now_iso, log=print, sleep=time.sleep,
                 clock=time.monotonic):
        self.period_s = max(1.0, float(period_s))
        self._connect, self._now_iso, self._log = connect, now_iso, log
        self._sleep, self._clock = sleep, clock
        self.meter = TransactionSpanMeter(clock)
        self.passes = self.failures = 0
        self.last_pass_at = self.last_pass_s = self.last_error = None

    def run_once(self, conn):
        from sniper.settings.memecoin import MEMECOIN_CAPTURE_V2_ENABLED
        pump = os.getenv("MEMECOIN_PUMP_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
        started = self._clock()
        try:
            research_capture_pass(conn, self._now_iso(), v2_enabled=MEMECOIN_CAPTURE_V2_ENABLED,
                                  pump_enabled=pump, log=self._log)
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            self.failures += 1
            self.last_error = type(exc).__name__
            import traceback
            self._log("[err] research capture pass failed:\n" + traceback.format_exc())
        else:
            self.passes += 1
            self.last_pass_at = self._now_iso()
            self.last_pass_s = round(self._clock() - started, 2)
        return self._clock() - started

    def run_forever(self):
        conn = self._connect()
        conn.set_trace_callback(self.meter.trace)
        while True:
            took = self.run_once(conn)
            self._sleep(max(1.0, self.period_s - took))

    def status(self):
        return {"last_pass_at": self.last_pass_at, "last_pass_s": self.last_pass_s,
                "passes": self.passes, "failures": self.failures,
                "last_error": self.last_error, **self.meter.snapshot()}
