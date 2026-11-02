"""The paper copy fan-out, off the funded threads (2026-09-27, Alpha GO).

WHY. Both watchdog kills on 2026-09-27 (12:38:01Z "stale", 12:44:01Z "sweep_stale") caught the funded
main loop inside the PAPER copy trader: run_once -> copy_event_to_arms -> executable_entry_fill ->
quote_entry -> an EVM V4 pool scan / erc20_decimals through evm_wallet's direct urlopen (a 10 s socket
timeout per call, many calls, no whole-request deadline). The funded runner was down about 7 minutes.
The same fan-out also runs inline on the Fomo-activity dispatch threads, where it delays the next
event's funded hook.

WHAT. The funded thread still runs the funded hook first, then only reads the funded quote/clip for
the origin on ITS connection (it can see its own writes; the worker could not) and puts one job on a
bounded queue. It never blocks: a full queue drops the job, labels the event
`skipped_paper_queue_full` and counts it. One worker thread, with its own writable ledger connection,
runs the unchanged copy_event_to_arms per job and COMMITS per job:
  - a buy reads, then quotes, then writes, so no write lock is held across its network;
  - a sell's `claim_close` commits before the exit quote, so none is held there either;
  - the commit at the end of every job means nothing is carried into the next job's quote.
A job that waited longer than MAX_LAG_S is censored as `skipped_paper_late`: a quote taken that late
is a different instant from the event. Neither censor label is a fill; an event without a position is
exactly what an unquotable event already is (`skipped_no_executable_quote`).

Rollback: MEMECOIN_PAPER_FANOUT_THREAD=0 installs nothing and both call sites run inline as before.
"""
from __future__ import annotations

import collections
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger("memecoin")

MAX_QUEUE = 64
MAX_LAG_S = 5.0
QUEUE_FULL = "skipped_paper_queue_full"
LATE = "skipped_paper_late"

# Installed ONCE by the runner at startup, before any thread that submits starts; only read after.
_ACTIVE = None


def enabled(env=None):
    env = os.environ if env is None else env
    return str(env.get("MEMECOIN_PAPER_FANOUT_THREAD", "1")).strip().lower() not in {
        "0", "false", "no", "off"}


def install(fanout):
    global _ACTIVE
    _ACTIVE = fanout


def active():
    return _ACTIVE


def _feed_action(vals):
    return "opened" if "opened" in vals else "exit" if "exit" in vals else (
        vals[0] if vals else "ignored")


def _follow_action(vals):
    return "opened" if "opened" in vals else (vals[0] if vals else "ignored")


ACTION_RULES = {"feed": _feed_action, "follow": _follow_action}


@dataclass(frozen=True)
class PaperJob:
    ev: object
    size_usd: float
    arms: tuple
    fill_fn: object
    stats_fn: object
    funded_quote: object
    funded_clip: object
    delete_on_failure: bool
    action_rule: str
    enqueued_mono: float


class PaperFanout:
    def __init__(self, *, connect, now_iso_fn, maxsize=MAX_QUEUE, max_lag_s=MAX_LAG_S,
                 clock=time.monotonic):
        self._connect, self._now_iso, self.clock = connect, now_iso_fn, clock
        self.max_lag_s = float(max_lag_s)
        self._queue = queue.Queue(maxsize=int(maxsize))
        self._thread = None
        self._start_lock = threading.Lock()     # taken only when the worker is not alive
        # Telemetry only. `+=` from two submitting threads can lose an increment; a counter that is
        # off by one never decides anything.
        self.queued = self.dropped_full = self.censored_late = 0
        self.processed = self.opened = self.failed = 0
        self.last_job_at = None
        self._lags_ms = collections.deque(maxlen=256)

    # --- funded side: never blocks ------------------------------------------------------------
    def submit(self, conn, job):
        """True when queued. False when full: the event is labelled and counted, never waited on."""
        self.ensure_started()
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            self.dropped_full += 1
            try:
                from sniper.persistence import db_memecoin as m
                m.set_event_action(conn, job.ev.fomo_event_id, QUEUE_FULL)
            except Exception:
                logger.exception("paper fan-out: could not label a dropped event %s",
                                 getattr(job.ev, "fomo_event_id", None))
            return False
        self.queued += 1
        return True

    def ensure_started(self):
        if self._thread is not None and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self.run_forever,
                                                name="memecoin-paper-fanout", daemon=True)
                self._thread.start()

    # --- worker side ----------------------------------------------------------------------------
    def connect_with_retry(self, *, sleep=time.sleep, retry_s=5.0):
        """The worker's ledger connection, retried until it opens. Logged once, counted, never fatal:
        a dead worker would be restarted by every submit and each restart would fail the same way."""
        logged = False
        while True:
            try:
                return self._connect()
            except Exception:
                self.failed += 1
                if not logged:
                    logger.exception("paper fan-out: ledger connect failed; retrying every %.0fs",
                                     retry_s)
                    logged = True
                sleep(retry_s)

    def run_forever(self):
        conn = self.connect_with_retry()
        while True:
            self.run_job(conn, self._queue.get())

    def run_job(self, conn, job):
        from sniper.persistence import db_memecoin as m
        from sniper.services.memecoin_copy_trader import copy_event_to_arms
        event_id = getattr(job.ev, "fomo_event_id", None)
        lag_s = self.clock() - job.enqueued_mono
        self._lags_ms.append(lag_s * 1000.0)
        try:
            if conn.in_transaction:                 # never carry a write into this job's network
                conn.commit()
            if lag_s > self.max_lag_s:
                self.censored_late += 1
                m.set_event_action(conn, event_id, LATE)
                return
            outcomes = copy_event_to_arms(
                conn, job.ev, job.size_usd, self._now_iso(), arms=list(job.arms),
                fill_fn=job.fill_fn, stats_fn=job.stats_fn,
                funded_quote=job.funded_quote, funded_clip=job.funded_clip)
            vals = list(outcomes.values())
            m.set_event_action(conn, event_id, ACTION_RULES[job.action_rule](vals))
            self.processed += 1
            self.opened += "opened" in vals
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            self.failed += 1
            logger.exception("paper fan-out failed for %s (sqlite_errorcode=%s)", event_id,
                             getattr(exc, "sqlite_errorcode", None))
            try:
                if job.delete_on_failure:
                    m.delete_event(conn, event_id)      # paper-only mode: redelivery retries it
                else:
                    m.set_event_action(conn, event_id, "paper_failed")
            except Exception:
                logger.exception("paper fan-out: could not label a failed event %s", event_id)
        finally:
            try:
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
            self.last_job_at = self._now_iso()

    def status(self):
        lags = sorted(self._lags_ms)

        def pct(p):
            return round(lags[int(p * (len(lags) - 1))], 1) if lags else None
        return {"alive": bool(self._thread is not None and self._thread.is_alive()),
                "depth": self._queue.qsize(), "queued": self.queued,
                "dropped_full": self.dropped_full, "censored_late": self.censored_late,
                "processed": self.processed, "opened": self.opened, "failed": self.failed,
                "lag_ms_p50": pct(0.5), "lag_ms_p90": pct(0.9), "last_job_at": self.last_job_at}


def submit_or_none(conn, ev, size_usd, *, arms, fill_fn, stats_fn, delete_on_failure,
                   action_rule):
    """Queue the paper fan-out when one is installed and return whether it was queued; None means
    no fan-out is installed and the caller runs it inline, as before."""
    from sniper.settings.memecoin import MEMECOIN_FUNDED_ONLY
    if MEMECOIN_FUNDED_ONLY:
        return False  # An uninstalled fanout must not move paper quotes onto the funded thread.
    fan = _ACTIVE
    if fan is None:
        return None
    from sniper.persistence import db_memecoin as m
    origin = getattr(ev, "fomo_event_id", None)
    job = PaperJob(ev=ev, size_usd=size_usd, arms=tuple(arms), fill_fn=fill_fn, stats_fn=stats_fn,
                   funded_quote=m.funded_quote_for_origin(conn, origin),
                   funded_clip=m.funded_clip_for_origin(conn, origin),
                   delete_on_failure=bool(delete_on_failure), action_rule=action_rule,
                   enqueued_mono=fan.clock())
    return fan.submit(conn, job)
