#!/usr/bin/env python3
"""Run the refused-mint forward capture. DEFAULT OFF; the operator enables it in `.env`.

    python3 scripts/memecoin_refusal_capture_loop.py [--once] [--dry-run]

Reads the funded ledger READ-ONLY and writes only its own database. It never enters, sizes,
re-asks, or grades, and it adds no code to the funded entry path -- it learns about a refusal from
the durable record the runner already writes.

Design: docs/superpowers/specs/2026-08-26-memecoin-refused-mint-forward-capture-design.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# THE SETTINGS LIVE WITH THE STATE, NOT WITH THE CODE (2026-09-18). This used to load `.env` from
# its OWN directory, which is correct only while the loop runs from the mutable checkout — and that
# was the defect: the worker never moved to a pinned release, so a deploy could not reach its
# classifier and it spent 2026-09-17/18 unable to see two refusal families that shipped after it
# started. A release checkout has no `.env`, so pinning it under the old loader would have started
# the loop on library defaults instead, silently. `load_env_files(state_root())` is the same loader
# the funded runner uses and it reads both `.env` and `.fomo.env` from the state root.
from sniper.util.envfiles import load_env_files                         # noqa: E402
from sniper.util.state_paths import state_root                          # noqa: E402
load_env_files(str(state_root()))

from sniper.persistence import db_memecoin_captures as store            # noqa: E402
from sniper.services import memecoin_refusal_capture as cap             # noqa: E402
from sniper.util.clock import real_utc_now_iso                          # noqa: E402
from sniper.util.state_paths import state_path                          # noqa: E402
from sniper.persistence.db_core import get_readonly_connection

logger = logging.getLogger("memecoin_refusal_capture")


def poll_refusals(ledger, *, since_iso, limit=200, networks=("solana",)):
    """Read new refusals and retain unfinished attempts in the returned cursor.

    Accept an initial ISO timestamp or the previous cursor. The cursor uses insertion order;
    completion timestamps describe each trace, not commit order. Each poll reads at most
    one new page and one pending page. The worker still starts fresh after a restart.
    `networks` names the chains to capture (2026-09-13: the loop adds the EVM lane's chains);
    every item carries its `network_id`.
    """
    limit = max(1, int(limit))
    networks = tuple(str(n) for n in networks) or ("solana",)
    in_nets = ",".join("?" for _ in networks)
    cursor = (dict(since_iso) if isinstance(since_iso, dict)
              else {"since_iso": since_iso, "after_rowid": 0, "pending": [],
                    "watermark": since_iso})
    columns = ("rowid, attempt_id, mint, origin_event_id, terminal_reason, "
               "decision_started_at, network_id, terminal_status")
    upper_rowid = ledger.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM memecoin_entry_attempts").fetchone()[0]
    rows = ledger.execute(
        f"SELECT {columns} "
        "FROM memecoin_entry_attempts "
        "WHERE rowid > ? AND rowid <= ? AND decision_started_at > ? "
        f"AND network_id IN ({in_nets}) AND mint IS NOT NULL AND terminal_status IS NOT 'filled' "
        "ORDER BY rowid LIMIT ?",
        (cursor["after_rowid"], upper_rowid, cursor["since_iso"], *networks, limit)).fetchall()
    cursor["after_rowid"] = rows[-1][0] if len(rows) == limit else upper_rowid
    if rows:
        cursor["watermark"] = max(cursor["watermark"], *(row[5] for row in rows))
    pending = cursor["pending"][:limit]
    cursor["pending"] = cursor["pending"][limit:]
    if pending:
        marks = ",".join("?" for _ in pending)
        rows = ledger.execute(
            f"SELECT {columns} FROM memecoin_entry_attempts WHERE attempt_id IN ({marks})",
            pending).fetchall() + rows
    out = []
    for _, attempt_id, mint, origin, reason, at, network, status in rows:
        if network not in networks or mint is None or status == "filled":
            continue
        if reason is None and status is None:
            cursor["pending"].append(attempt_id)
            continue
        gate = cap.classify_gate(reason)
        # A named family is not automatically a capturable one — see CAPTURED_FAMILIES. A refusal
        # about the copied trader rather than the coin has no forward counterfactual worth a quote.
        if not cap.should_capture(gate):
            gate = None
        if gate:
            item = {"mint": mint, "origin_event_id": origin, "gate": gate, "refused_at": at,
                    "network_id": network, "attempt_id": attempt_id}
            if gate in cap.SIZED_FAMILIES:
                from sniper.persistence.db_memecoin import entry_attempt_notional
                item["quote_input_usd"] = entry_attempt_notional(ledger, attempt_id)
            if gate == "slippage_fail":
                trace = ledger.execute("SELECT trace_json FROM memecoin_entry_attempts "
                                       "WHERE attempt_id=?", (attempt_id,)).fetchone()
                item["slippage_available_over_min"] = cap.slippage_available_over_min(
                    trace[0] if trace else None)
            out.append(item)
    return out, cursor


def slot_end_io(ledger_path, store_path):
    """(trace_fn, flat_fn, record_fn) for the slot-end shadow worker, each opening its OWN
    connections on the worker's thread (a sqlite handle belongs to the thread that made it). The
    ledger side is read-only; a failed flat check reads as NOT flat, so an error never unlocks a
    research read while a real Solana position might be open."""
    import threading
    local = threading.local()

    def ledger():
        if getattr(local, "ledger", None) is None:
            local.ledger = get_readonly_connection(str(ledger_path))
        return local.ledger

    def trace_fn(attempt_id):
        row = ledger().execute("SELECT trace_json FROM memecoin_entry_attempts WHERE attempt_id=?",
                               (attempt_id,)).fetchone()
        return row[0] if row else None

    def flat_fn():
        # The research-read rule: no open real Solana position AND no reserved Solana entry.
        try:
            c = ledger()
            return (c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                              "AND is_open=1 AND network_id='solana' AND COALESCE(tracking_paused_at,'')=''").fetchone()[0] == 0
                    and c.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved' "
                                  "AND network_id='solana'").fetchone()[0] == 0)
        except Exception:
            logger.exception("slot-end shadow: flat-book check failed; waiting")
            return False

    def record_fn(row):
        if getattr(local, "store", None) is None:
            local.store = store.connect(store_path)
        store.record_slot_end_shadow(local.store, row)
    return trace_fn, flat_fn, record_fn


def handed_off(ledger, capture):
    """A funded position opened on this mint after the refusal owns the coin from that moment.

    Two quote streams on one mint would compete for the same provider budget and produce two
    disagreeing series for one coin, so this capture stops and says why.
    """
    row = ledger.execute(
        "SELECT id FROM memecoin_positions WHERE mint=? AND network_id=? "
        "AND execution_mode='real' AND opened_at >= ? LIMIT 1",
        (capture.mint, capture.network_id, capture.refused_at)).fetchone()
    return row[0] if row else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="assert the key pool and report what WOULD be captured; no requests")
    ap.add_argument("--since", default=None,
                    help="start the refusal watermark at this ISO time instead of now. For "
                         "verification runs; a backdated start captures a series whose age_s "
                         "begins well past zero, so never pool those with live captures.")
    ap.add_argument("--reap", action="store_true",
                    help="completeness reaper only. Runs on its OWN clock, deliberately: every "
                         "other outcome is written by the capture loop and so cannot report the "
                         "loop being dead. Give this its own cron line, never the loop's.")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import sniper.settings.memecoin as cfg
    horizon_cfg = float(cfg.MEMECOIN_REAL_MAX_HOLD_S) + float(cfg.MEMECOIN_CAPTURE_SETTLE_MARGIN_S)
    if args.reap:
        conn = store.connect(state_path("data", "memecoin_refusal_captures.db"))
        now_iso = real_utc_now_iso()
        counts = store.observation_counts(conn, before_iso=now_iso)
        n = cap.reap(counts, now_iso=now_iso, horizon_s=horizon_cfg,
                     cadence_s=float(cfg.MEMECOIN_CAPTURE_CADENCE_S),
                     stamp_fn=lambda cid, outcome, **kw: store.terminal_row(
                         conn, cid, outcome, **kw))
        logger.info("reaper stamped %d capture(s) of %d scanned", n, len(counts))
        return 0
    if not cfg.MEMECOIN_CAPTURE_ENABLED and not args.dry_run:
        logger.info("MEMECOIN_CAPTURE_ENABLED is off; nothing to do")
        return 0

    # SINGLE INSTANCE, ENFORCED IN THE PROCESS, not only by the launcher. The watchdog wraps its
    # launch in flock, but a lock held by the launcher only serializes launches that GO THROUGH
    # the launcher — a manual `nohup` start holds nothing, so the watchdog's next tick sees a free
    # lock and starts a second loop beside it. Reproduced 2026-08-28: the first watchdog run
    # against a hand-started loop produced two, competing for the same provider budget and writing
    # the same database. Same doctrine as the funded runner's own run lock.
    from sniper.services.memecoin_runner import acquire_run_lock
    _run_lock = acquire_run_lock(str(state_path("data", "memecoin_refusal_capture.worker.lock")))
    if _run_lock is None:
        # SILENT. Cron runs the watchdog every minute and every one of those launches loses this
        # lock while a healthy loop holds it, so a line here is one guaranteed log entry per
        # minute saying nothing happened — and noise on a channel whose whole job is to make a
        # real event visible. The heartbeat staleness check is the wedge detector; this path is
        # the normal case.
        return 0

    # Day one, before any request: research load must not ride a funded quota.
    # This process spends the MEASUREMENT Jupiter key, never the funded one. Declared before any
    # quote can be issued, for the same reason assert_pool_disjoint runs here: a research lane on a
    # funded budget is the 2026-08-21 failure, and the funded key IS the exit brain's budget.
    from sniper.execution.solana_real_seams import declare_measurement_process
    declare_measurement_process()
    urls = cap.assert_pool_disjoint(os.environ)
    logger.info("capture RPC pool: %d endpoint(s), disjoint from every funded role", len(urls))

    horizon = float(cfg.MEMECOIN_REAL_MAX_HOLD_S) + float(cfg.MEMECOIN_CAPTURE_SETTLE_MARGIN_S)
    cadence = float(cfg.MEMECOIN_CAPTURE_CADENCE_S)
    logger.info("horizon=%.0fs (rail %.0f + margin %.0f) cadence=%.0fs expected_obs=%d clip=$%.0f",
                horizon, cfg.MEMECOIN_REAL_MAX_HOLD_S, cfg.MEMECOIN_CAPTURE_SETTLE_MARGIN_S,
                cadence, int(horizon // cadence), cfg.MEMECOIN_CAPTURE_CLIP_USD)
    # EVM LANE (2026-09-13): its own chains, cadence and capacity; DexScreener display marks only.
    evm_networks = tuple(cfg.MEMECOIN_CAPTURE_EVM_NETWORKS)
    evm_cadence = float(cfg.MEMECOIN_CAPTURE_EVM_CADENCE_S)
    # EXACT EVM LANE (2026-09-28): our own quoters on a research host per chain. Default off. A
    # chain whose read host is unset, shared with another role, or on the wrong chain is refused
    # here, loudly, and the rest of the worker runs as before. A host that did not answer the
    # identity check (a 429 at a relaunch, twice by 2026-09-30) is PENDING: the lane retries it
    # and its captures are gaps until it proves its chain.
    # It runs on ITS OWN THREAD: a first-buy scan takes seconds, and the Solana grid below must
    # never wait for it. This loop only hands it refusals.
    exact_worker, exact_networks = None, ()
    if cfg.memecoin_capture_evm_exact_chains() and not args.dry_run:
        from sniper.services import memecoin_evm_exact_capture as exact_capture
        exact_worker, exact_networks = exact_capture.build_worker(
            os.environ, cfg, ledger_path=state_path("sniper_tracker.db"),
            store_path=state_path("data", "memecoin_refusal_captures.db"),
            horizon_s=horizon, cadence_s=evm_cadence, handed_off_fn=handed_off)
    # SLOT-END CHASE SHADOW (2026-09-29): Solana chase refusals only, on its own thread, research key
    # only (refused when it is also a funded key), and no read while a real Solana position is open.
    slot_end = None
    if getattr(cfg, "MEMECOIN_CAPTURE_SLOT_END_ENABLED", False) and not args.dry_run:
        from sniper.services import memecoin_slot_end_shadow as slot_end_shadow
        rpc, why = slot_end_shadow.rpc_from_env(os.environ)
        if rpc is None:
            logger.warning("slot-end shadow OFF: %s", why)
        else:
            trace_fn, flat_fn, record_fn = slot_end_io(
                state_path("sniper_tracker.db"), state_path("data", "memecoin_refusal_captures.db"))
            slot_end = slot_end_shadow.Worker(rpc=rpc, trace_fn=trace_fn, record_fn=record_fn,
                                              flat_fn=flat_fn)
            slot_end.start()
            logger.info("slot-end shadow ON (research key, flat-book gated)")
    # EXIT-VENUE SHADOW (2026-09-30): for each exit quote the runner stores, what the position's
    # ENTRY pool would have paid. Its own thread, its own read host per chain, address only. OFF
    # until MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_<chain> and MEMECOIN_EXIT_VENUE_SHADOW_OWNER are set.
    exit_shadow = None
    if not args.dry_run:
        from sniper.services import memecoin_exit_venue_shadow as exit_venue_shadow
        exit_shadow, why = exit_venue_shadow.build_worker(
            os.environ, cfg, ledger_path=state_path("sniper_tracker.db"),
            store_path=state_path("data", "memecoin_refusal_captures.db"))
        if exit_shadow is None:
            logger.info("exit venue shadow OFF: %s", why)
        else:
            exit_shadow.start()
            logger.info("exit venue shadow ON: chains=%s",
                        ",".join(shadow.chain for shadow in exit_shadow.shadows))
    networks = ("solana",) + evm_networks + tuple(n for n in exact_networks if n not in evm_networks)
    lane_cadence = lambda net: cadence if net == "solana" else evm_cadence
    lane_max = lambda net: (cfg.MEMECOIN_CAPTURE_MAX_CONCURRENT if net == "solana"
                            else cfg.MEMECOIN_CAPTURE_EVM_MAX_CONCURRENT)
    logger.info("EVM lane: chains=%s cadence=%.0fs max=%d basis=display (DexScreener)",
                ",".join(evm_networks) or "off", evm_cadence, cfg.MEMECOIN_CAPTURE_EVM_MAX_CONCURRENT)

    # Held for the life of the worker, so it MUST be the helper: a raw handle here leaves a read
    # snapshot open between statements and stops every WAL checkpoint at it.
    ledger = get_readonly_connection(str(state_path("sniper_tracker.db")))
    conn = store.connect(state_path("data", "memecoin_refusal_captures.db"))

    from sniper.execution.solana_wallet import _http as raw_http
    from sniper.execution.solana_rpc_pool import RotatingSolanaRpcHttp
    http = RotatingSolanaRpcHttp(raw_http, urls)
    yielder = cap.JupiterYield(hold_s=float(cfg.MEMECOIN_CAPTURE_JUPITER_YIELD_S))
    display_fn = cap.dexscreener_display_fn() if evm_networks else None
    display_yield = cap.JupiterYield(hold_s=float(cfg.MEMECOIN_CAPTURE_DISPLAY_YIELD_S),
                                     name="DexScreener")

    captures = {}
    watermark = args.since or real_utc_now_iso()

    def on_refuse(cid, outcome, mint, gate, refused_at):
        # terminal_row takes the chain from the capture id; the cadence is the lane's.
        hz, cadence_s = horizon, lane_cadence(str(cid).split(":", 1)[0])
        if str(cid).endswith(cap.DIP_SUFFIX):            # the dip shadow's own window and cadence
            hz = float(cfg.MEMECOIN_CAPTURE_DIP_WINDOW_S)
            cadence_s = float(cfg.MEMECOIN_CAPTURE_DIP_CADENCE_S)
        store.terminal_row(conn, cid, outcome, mint=mint, gate=gate, refused_at=refused_at,
                           horizon_s=hz, expected_obs=int(hz // cadence_s))

    # HEARTBEAT. Without one this loop is silent whether it is healthy or wedged: after three
    # startup lines it logs only when something happens, and "nothing happened" is the normal
    # case at ~4 capturable refusals an hour. A restart-if-absent watchdog cannot see a wedge,
    # which is how the funded runner died six times with no log line. Written at the TOP of each
    # pass, so it stops advancing the moment the pass does.
    heartbeat = state_path("data", "memecoin_refusal_capture.heartbeat")
    pump_ms = None              # the Solana/display pump's last wall time, in the heartbeat
    if exact_worker is not None:
        exact_worker.start()

    while True:
        now_mono, now_iso = time.monotonic(), real_utc_now_iso()
        try:
            heartbeat.write_text(json.dumps({
                "ts": now_iso, "pid": os.getpid(), "active": len(captures),
                "watermark": (watermark["watermark"]
                              if isinstance(watermark, dict) else watermark),
                "pump_ms": pump_ms,
                "evm_exact": exact_worker.stats() if exact_worker is not None else None,
                "slot_end_done": slot_end.done if slot_end is not None else None,
                "exit_venue_shadow": exit_shadow.stats() if exit_shadow is not None else None}))
        except Exception:
            logger.exception("could not write the heartbeat")
        fresh, watermark = poll_refusals(ledger, since_iso=watermark, networks=networks)
        for r in fresh:
            if args.dry_run:
                logger.info("WOULD capture chain=%s mint=%s gate=%s refused_at=%s",
                            r["network_id"], r["mint"], r["gate"], r["refused_at"])
                continue
            if r["network_id"] == "solana" or r["network_id"] in evm_networks:
                cap.start(captures, mint=r["mint"], origin_event_id=r["origin_event_id"],
                          gate=r["gate"], refused_at=r["refused_at"], network_id=r["network_id"],
                          horizon_s=horizon, cadence_s=lane_cadence(r["network_id"]),
                          max_concurrent=lane_max(r["network_id"]),
                          now=now_mono, on_refuse=on_refuse, now_epoch=time.time(),
                          quote_input_usd=r.get("quote_input_usd"),
                          slippage_available_over_min=r.get("slippage_available_over_min"))
                if r["network_id"] == "solana" and r["gate"] in cap.DIP_FAMILIES:
                    cap.start_dip(captures, r, now=now_mono, now_epoch=time.time(),
                                  on_refuse=on_refuse)
            if r["network_id"] in exact_networks:
                exact_worker.submit(r)
            if slot_end is not None and r["network_id"] == "solana" and r["gate"] == "chase_quote":
                slot_end.submit(r)

        for c in list(captures.values()):
            if c.done:
                continue
            pid = handed_off(ledger, c)
            if pid is not None:
                c.done = True
                store.terminal_row(conn, c.capture_id, "handed_off_to_position", mint=c.mint,
                                   gate=c.gate, refused_at=c.refused_at, horizon_s=c.horizon_s,
                                   expected_obs=c.expected_obs, quote_id=str(pid))
                logger.info("capture handed off to funded position %s (mint=%s)", pid, c.mint)

        if not args.dry_run:
            pump_started = time.monotonic()
            cap.pump(captures, now_mono=now_mono, now_iso=now_iso, http=http, rpc_url=urls[0],
                     clip_usd=cfg.MEMECOIN_CAPTURE_CLIP_USD,
                     record_fn=lambda row: store.record_observation(conn, row),
                     jup_yield=yielder, display_fn=display_fn, display_yield=display_yield)
            pump_ms = round((time.monotonic() - pump_started) * 1000.0, 1)

        for key in [k for k, c in captures.items() if c.done]:
            captures.pop(key, None)

        if args.once or args.dry_run:
            return 0
        time.sleep(float(cfg.MEMECOIN_CAPTURE_POLL_S))


if __name__ == "__main__":
    raise SystemExit(main())
