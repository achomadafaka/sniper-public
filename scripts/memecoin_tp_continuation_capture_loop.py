#!/usr/bin/env python3
"""Capture the 60 seconds after every funded take-profit exit, at ONE-SECOND cadence. DEFAULT OFF.

Distinct from the in-runner `post_tp_pass` collector, which samples four fixed horizons into the
funded ledger for the laddered-TP question. This one answers the CONFIRMATION-WINDOW question and
has its own flag so the two are independently switchable.

    python3 scripts/memecoin_post_tp_followup_loop.py [--once] [--dry-run]

Reads the funded ledger READ-ONLY and writes only the research database. It never enters, exits,
sizes, or grades, and it adds no code to the funded exit path — it learns about a take-profit from
the durable record the runner already wrote. That is a stronger guarantee than a fire-and-forget
call inside the executor, which could still raise or block in the caller's thread.

Design: docs/superpowers/specs — the operator's TP-confirmation hypothesis, 2026-08-29.
Gamma's pre-registration governs what gets COMPUTED; this only produces the instrument.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv                                          # noqa: E402
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from sniper.persistence import db_memecoin_captures as store            # noqa: E402
from sniper.services import memecoin_tp_continuation_capture as ptf            # noqa: E402
from sniper.persistence.db_core import get_readonly_connection          # noqa: E402
from sniper.util.clock import real_utc_now_iso                          # noqa: E402
from sniper.util.state_paths import state_path                          # noqa: E402

logger = logging.getLogger("memecoin_post_tp_followup")

POLL_S = 1.0            # the detection lag IS the instrument's floor, so poll at the cadence
MAX_CONCURRENT = 6
_LOCK = threading.Lock()
_ACTIVE = {"n": 0}


def _capture(plan, *, db_path, rpc_url, http):
    """One follow-up, on its own thread and its own connection. Never raises out."""
    from sniper.execution.solana_real_seams import jupiter_exit_quote
    conn, ages, qualities = None, [], []
    try:
        conn = store.connect(db_path)
        qty = int(plan["exit_qty_atomic"])
        last_step = -1
        while True:
            now = datetime.now(plan["_exited"].tzinfo)
            elapsed = (now - plan["_exited"]).total_seconds()
            if elapsed > ptf.HORIZON_S:
                break
            step = int(elapsed // ptf.CADENCE_S)
            if step <= last_step:
                time.sleep(0.15)
                continue
            last_step = step
            t0 = time.monotonic()
            failure = None
            try:
                quote = jupiter_exit_quote(plan["mint"], qty, 1.0, http=http)
            except Exception as exc:
                # A raised call is OUR blindness, not the coin's death. Recorded as
                # provider_error so the read can tell them apart — merged, our outage would file
                # as a dying coin, in the direction that favours the hypothesis.
                logger.exception("post-tp quote failed pos=%s", plan["position_id"])
                quote, failure = None, exc
            if quote is not None:
                quote = dict(quote)
                quote["_latency_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
            row = ptf.observation(plan, quote=quote, now_iso=real_utc_now_iso(), now=now,
                                  failure=failure)
            store.record_followup_observation(conn, row)
            if row["age_s"] is not None:
                ages.append(row["age_s"])
            qualities.append(row["quality"])
        store.record_followup_terminal(
            conn, ptf.terminal(plan, ages, qualities=qualities))
        logger.info("post-tp pos=%s %d obs, first at %.1fs, lag %.1fs, %s",
                    plan["position_id"], len(ages), ages[0] if ages else -1.0,
                    plan["detection_lag_s"],
                    "CENSORED" if any(q != ptf.OK for q in qualities) else "clean")
    except Exception:
        logger.exception("post-tp capture died pos=%s", plan.get("position_id"))
        try:
            if conn is not None:
                store.record_followup_terminal(
                    conn, ptf.terminal(plan, ages, outcome=ptf.TRUNCATED,
                                       qualities=qualities))
        except Exception:
            logger.exception("post-tp could not stamp a truncated terminal row")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        with _LOCK:
            _ACTIVE["n"] -= 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="assert the pool and report what WOULD be captured; no quotes")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import sniper.settings.memecoin as cfg
    if not getattr(cfg, "MEMECOIN_TP_CONTINUATION_CAPTURE_ENABLED", False) and not a.dry_run:
        logger.info("MEMECOIN_TP_CONTINUATION_CAPTURE_ENABLED is off; nothing to do")
        return 0

    # SINGLE INSTANCE, ENFORCED IN THE PROCESS. A launcher-held lock only serializes launches that
    # go through the launcher, so a manual start holds nothing and the watchdog's next tick starts
    # a second worker beside it — reproduced on the capture loop 2026-08-28, two processes on one
    # research budget writing one database. Same doctrine as the funded runner's own run lock.
    from sniper.services.memecoin_runner import acquire_run_lock
    _run_lock = acquire_run_lock(str(state_path("data", "memecoin_tp_continuation.worker.lock")))
    if _run_lock is None:
        # Silent: cron runs the watchdog every minute and every launch loses this lock while a
        # healthy worker holds it. A line here is one guaranteed log entry per minute saying
        # nothing happened, on the channel whose job is to make a real event visible.
        return 0

    # Before any request. This process spends a MEASUREMENT key, never the funded one — a research
    # lane on the exit brain's Jupiter quota is the 2026-08-21 failure, and the funded key is the
    # budget the exits themselves depend on.
    from sniper.execution.solana_real_seams import (
        CAPTURE_JUP_API_KEY_VAR, declare_measurement_process, jup_key_configured)
    from sniper.services import memecoin_refusal_capture as cap
    declare_measurement_process(CAPTURE_JUP_API_KEY_VAR)
    if not jup_key_configured() and not a.dry_run:
        logger.error("%s is not set. Refusing to run on the shared keyless route — funded exit "
                     "quotes share that IP budget.", CAPTURE_JUP_API_KEY_VAR)
        return 2
    urls = cap.assert_pool_disjoint(os.environ)
    logger.info("post-tp RPC pool: %d endpoint(s), disjoint from every funded role", len(urls))

    from sniper.execution.solana_wallet import _http as raw_http
    from sniper.execution.solana_rpc_pool import RotatingSolanaRpcHttp
    from sniper.execution.solana_real_seams import token_decimals
    http = RotatingSolanaRpcHttp(raw_http, urls)

    db_path = state_path("data", "memecoin_refusal_captures.db")
    seed = store.connect(db_path)
    watermark = store.followup_watermark(seed) or real_utc_now_iso()
    seed.close()
    logger.info("horizon=%.0fs cadence=%.0fs -> %d expected obs per exit; watermark %s",
                ptf.HORIZON_S, ptf.CADENCE_S, int(ptf.HORIZON_S // ptf.CADENCE_S), watermark)

    ledger = get_readonly_connection(str(state_path("sniper_tracker.db")))
    ledger.row_factory = sqlite3.Row
    decimals_cache = {}

    # HEARTBEAT. Written at the top of every pass, so the watchdog can tell a wedged worker from a
    # quiet one — and this worker is quiet by design, since a day with no take-profits produces no
    # output at all. A restart-if-gone watchdog cannot see a wedge; that is how the funded runner
    # died six times with nothing in the log.
    heartbeat = state_path("data", "memecoin_tp_continuation.heartbeat")

    while True:
        try:
            try:
                heartbeat.write_text(json.dumps({
                    "ts": real_utc_now_iso(), "pid": os.getpid(),
                    "active": _ACTIVE["n"], "watermark": watermark}))
            except Exception:
                logger.exception("could not write the heartbeat")
            for row in ptf.new_tp_exits(ledger, since_iso=watermark):
                watermark = max(watermark, row["closed_at"])
                if a.dry_run:
                    logger.info("WOULD capture pos=%s mint=%s qty=%s exit_value=%s",
                                row["id"], row["mint"], row["qty"], row["notional"])
                    continue
                with _LOCK:
                    if _ACTIVE["n"] >= MAX_CONCURRENT:
                        logger.info("post-tp at the %d-capture cap; pos=%s not captured",
                                    MAX_CONCURRENT, row["id"])
                        continue
                key = (row["network_id"], row["mint"])
                if key not in decimals_cache:
                    try:
                        decimals_cache[key] = token_decimals(row["mint"], urls[0], http=http)
                    except Exception:
                        logger.exception("post-tp could not read decimals for %s", row["mint"])
                        decimals_cache[key] = None
                plan = ptf.schedule(row, decimals=decimals_cache[key],
                                    now_iso=real_utc_now_iso())
                if plan is None:
                    continue
                with _LOCK:
                    _ACTIVE["n"] += 1
                threading.Thread(target=_capture, args=(plan,),
                                 kwargs={"db_path": db_path, "rpc_url": urls[0], "http": http},
                                 daemon=True).start()
        except Exception:
            logger.exception("post-tp poll failed; continuing")
        if a.once or a.dry_run:
            break
        time.sleep(POLL_S)
    while _ACTIVE["n"] > 0 and not (a.once or a.dry_run):
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
