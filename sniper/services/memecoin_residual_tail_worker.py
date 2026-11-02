"""Per-close timers that price the residual tail at 2/5/10/20/30/60s. MEASUREMENT ONLY.

WHY A TIMER AND NOT THE QUEUE. The existing post-TP follow-up drains one sample every 30 seconds,
so six horizons for one close take 180 seconds and a "2 second" sample lands 30s late at best.
`elapsed_s` would record that truthfully, which is worse than useless: six honestly-labelled
samples all taken far too late to say anything about a tail whose entire thesis lives inside the
first minute. The early horizons ARE the study, so they get a clock of their own.

WHAT IT MAY NEVER DO — the post-exit doctrine (2026-07-27), unchanged. It reads closed positions
after the fact and writes only its own evidence table. It never touches an exit, a reservation, a
rail, inventory, the intent queue, or any funded state, and nothing in a decision path reads it.

THE OPERATING RULES (Alpha, 2026-08-27), each one load-bearing:
  - OWN CONNECTION. sqlite3 connections are not shareable across threads, and borrowing the main
    loop's would put a measurement thread inside the funded writer's transaction scope.
  - HARD CONCURRENCY CAP. At the cap a close is DROPPED, counted and logged — never queued. An
    unbounded queue turns a provider stall into a thread pile, and a dropped close is a missing
    sample, not an errand to retry.
  - HARD LIFETIME. The worker exits after the last horizon plus slack whatever happens, so a
    wedged provider call cannot leave a thread resident for the life of the process.
  - NO RETRIES. Any exception is logged once and the worker dies. elapsed_s already records
    reality; a retry would produce a sample labelled with a horizon it did not observe.

FUNDED KEY, DELIBERATELY. This runs inside the runner, so its quotes spend MEMECOIN_JUPITER_API_KEY
and are counted by JUPITER_LOAD like every other call in the process. At ~19 take-profits a day —
six horizon quotes plus at most six resolve probes each — that is roughly 230 calls a day. Small,
and stated here rather than discovered later.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

HORIZONS_S = (2.0, 5.0, 10.0, 20.0, 30.0, 60.0)
MAX_WORKERS = 8
LIFETIME_SLACK_S = 30.0          # the hard stop is max(HORIZONS_S) + this, whatever else happens

STATUS_HEALTHY = "healthy"
STATUS_ROUTE_ABSENT = "route_absent"
STATUS_PROVIDER_UNAVAILABLE = "provider_unavailable"
STATUS_NO_RESIDUAL = "no_residual"     # principal needed the whole bag; nothing would have been kept

COUNTS = {"spawned": 0, "dropped_at_cap": 0, "completed": 0, "died": 0, "samples": 0}
_LOCK = threading.Lock()
_ACTIVE = set()


def active_count():
    with _LOCK:
        return len(_ACTIVE)


def _elapsed_s(a, b):
    from datetime import datetime
    try:
        return (datetime.fromisoformat(str(b).replace("Z", "+00:00"))
                - datetime.fromisoformat(str(a).replace("Z", "+00:00"))).total_seconds()
    except (TypeError, ValueError):
        return None


def _run(close, *, connect_fn, decimals_fn, exit_quote_fn, now_fn, fee_buffer_pct, monotonic):
    """One close's whole life. Never raises out; always deregisters."""
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_residual_tail import (
        resolve_residual, RESOLVED, NO_ROUTE, UNREACHABLE)
    from sniper.services.memecoin_post_tp_followup import atomic_from_qty

    pid = close.get("position_id")
    deadline = monotonic() + max(HORIZONS_S) + LIFETIME_SLACK_S
    conn = None
    try:
        conn = connect_fn()
        decimals = decimals_fn(close["mint"])
        total_atomic = atomic_from_qty(close.get("exit_qty"), decimals)
        if not total_atomic:
            logger.info("residual tail: position %s has no usable exit quantity", pid)
            return

        def quote_out(atomic):
            q = exit_quote_fn(close["mint"], int(atomic), 1.0)
            return (int(q.get("outAmount") or 0) / 1e6) if q else None

        started = monotonic()
        target = float(close["entry_usdc"]) * (1.0 + float(fee_buffer_pct) / 100.0)
        res = resolve_residual(open_qty_atomic=total_atomic, target_usdc=target,
                               quote_out_fn=quote_out)
        resolve_ms = (monotonic() - started) * 1000.0
        residual_atomic = res.get("residual_qty_atomic")

        ctx = dict(position_id=pid, mint=close["mint"], network_id=close.get("network_id"),
                   closed_at=close.get("closed_at"), entry_usdc=close.get("entry_usdc"),
                   exit_usdc=close.get("exit_usdc"), resolve_status=res["status"],
                   resolve_fraction=res.get("fraction"),
                   residual_qty_atomic=residual_atomic,
                   sold_qty_atomic=res.get("sold_qty_atomic"), sold_usdc=res.get("sold_usdc"),
                   resolve_probes=res.get("probes"), resolve_ms=resolve_ms)

        if res["status"] != RESOLVED or not residual_atomic:
            # Record ONE row saying why there is no tail to price, rather than six empty horizons.
            # `unreachable` and a zero residual are findings about this close, not failures.
            # UNREACHABLE is a FINDING, not a provider fault: the bag never covered principal, so
            # the tail design would have had nothing to keep. Typing it as provider_unavailable
            # would file a real result under an infrastructure excuse and quietly shrink the
            # denominator of the study. Caught by its own test.
            status = (STATUS_NO_RESIDUAL if res["status"] in (RESOLVED, UNREACHABLE)
                      else STATUS_ROUTE_ABSENT if res["status"] == NO_ROUTE
                      else STATUS_PROVIDER_UNAVAILABLE)
            now = now_fn()
            m.record_residual_tail_sample(
                conn, horizon_s=0.0, observed_at=now,
                elapsed_s=_elapsed_s(close.get("closed_at"), now), status=status,
                detail=res.get("detail"), **ctx)
            with _LOCK:
                COUNTS["samples"] += 1
            return

        for horizon in HORIZONS_S:
            wait = (started + horizon) - monotonic()
            if wait > 0:
                time.sleep(wait)
            if monotonic() > deadline:
                logger.warning("residual tail: position %s hit its lifetime bound at %.0fs",
                               pid, horizon)
                return
            status, usdc, unit, detail = STATUS_PROVIDER_UNAVAILABLE, None, None, None
            try:
                usdc = quote_out(residual_atomic)
                if usdc is None:
                    status, detail = STATUS_ROUTE_ABSENT, "no route for the residual quantity"
                else:
                    status = STATUS_HEALTHY
                    unit = usdc / (int(residual_atomic) / (10 ** int(decimals)))
            except Exception as exc:
                detail = type(exc).__name__
            now = now_fn()
            m.record_residual_tail_sample(
                conn, horizon_s=horizon, observed_at=now,
                elapsed_s=_elapsed_s(close.get("closed_at"), now), status=status,
                residual_usdc=usdc, unit_price=unit, detail=detail, **ctx)
            with _LOCK:
                COUNTS["samples"] += 1
        with _LOCK:
            COUNTS["completed"] += 1
    except Exception:
        with _LOCK:
            COUNTS["died"] += 1
        logger.exception("residual tail worker died for position %s", pid)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        with _LOCK:
            _ACTIVE.discard(pid)


def spawn_for_close(close, *, connect_fn, decimals_fn, exit_quote_fn, now_fn,
                    fee_buffer_pct=1.0, monotonic=time.monotonic, spawn_fn=None):
    """Start one worker, or drop the close. Returns True iff a worker started. Never raises."""
    pid = close.get("position_id")
    with _LOCK:
        if pid in _ACTIVE:
            return False                       # already being measured; never two clocks per close
        if len(_ACTIVE) >= MAX_WORKERS:
            COUNTS["dropped_at_cap"] += 1
            logger.warning("residual tail: at the %d-worker cap, dropping position %s "
                           "(a dropped close is a missing sample, never a retry)", MAX_WORKERS, pid)
            return False
        _ACTIVE.add(pid)
        COUNTS["spawned"] += 1
    args = dict(connect_fn=connect_fn, decimals_fn=decimals_fn, exit_quote_fn=exit_quote_fn,
                now_fn=now_fn, fee_buffer_pct=fee_buffer_pct, monotonic=monotonic)
    try:
        if spawn_fn is not None:
            spawn_fn(_run, close, args)        # tests run it inline
        else:
            threading.Thread(target=_run, args=(close,), kwargs=args, daemon=True,
                             name="residual-tail-%s" % pid).start()
        return True
    except Exception:
        with _LOCK:
            _ACTIVE.discard(pid)
            COUNTS["died"] += 1
        logger.exception("residual tail: could not start a worker for position %s", pid)
        return False


def snapshot():
    """Heartbeat projection. A collector that silently stops must not look like a quiet market."""
    with _LOCK:
        return dict(COUNTS, active=len(_ACTIVE))
