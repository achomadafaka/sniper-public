#!/usr/bin/env python3
"""Coin Radar Stage 2 discovery worker.

Single-shot external-coin discovery (DexScreener boosts + GeckoTerminal trending/new pools) into
memecoin_external_discovery, so the radar surfaces hot coins before our wallets touch them. Low cadence
(run via cron ~every 5-10 min) so it shares the per-IP GT rate budget with the funded runner rather
than starving it.

    python3 scripts/memecoin_discovery_worker.py            # one pass (cron)
    python3 scripts/memecoin_discovery_worker.py --loop 300 # standalone daemon, 5-min cadence
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import sys as _sys, pathlib as _pathlib   # a bare `python3 scripts/memecoin_discovery_worker.py` puts this file's
_R = str(_pathlib.Path(__file__).resolve().parents[1])  # own dir on sys.path,
if _R not in _sys.path:                    # never the repo root
    _sys.path.insert(0, _R)
from sniper.persistence import db_memecoin as m
from sniper.util.state_paths import state_path
from sniper.util.clock import real_utc_now_iso
from sniper.services import memecoin_discovery_external as D


def _classify_new_entries(conn, now_iso):
    try:
        from sniper.services.memecoin_entry_classifier import apply_entry_classification
        from sniper.persistence.db_memecoin import accounting_mark_key
        qmap = m.radar_quote_map(conn, now_iso=now_iso, max_age_s=1800)

        def _qf(net, mint):
            # radar_quote_map is keyed by accounting_mark_key; match it (raw (net,mint) missed EVM).
            base = dict(qmap.get(accounting_mark_key(net, mint)) or {})
            # entry momentum = DexScreener 1h price change, cached with the quote. Position marks don't
            # exist yet inside the <45-min classify window, so the old mark-tape momentum was always None
            # and the runner/dip archetypes could never fire.
            base["momentum_1h"] = base.get("price_change_h1")
            return base
        return apply_entry_classification(conn, quote_fn=_qf, now_iso=now_iso)
    except Exception as e:
        print(f"[discovery] entry-classify skipped: {e}", file=sys.stderr, flush=True)
        return []


def _maybe_triage(conn, now_iso, *, top_n=15):
    """Run the LLM triage over the top-N radar coins IF an LLM key is configured; otherwise a cheap
    no-op (don't even build fact sheets). Throttled by the caller. Returns the verdict count. Turns the
    triage column from a manual button into a continuously-populated draft signal.

    Returns (count, state). State is reported so a permanently-disabled feature is visible
    as `unconfigured` rather than as a stream of zero-verdict passes indistinguishable from
    "the model had nothing to say"."""
    if not (os.getenv("XAI_API_KEY") or os.getenv("OPENAI_API_KEY")):
        return 0, "unconfigured"
    try:
        from sniper.services import memecoin_universe as U
        from sniper.services import memecoin_radar_triage as T
        try:
            extra = m.external_discovery_rows(conn, window_hours=72.0, now_iso=now_iso)
        except Exception:
            extra = []
        rows = U.universe_rows(conn, sort="armed", window_hours=72.0, extra_sources=extra,
                               quote_cache=m.radar_quote_map(conn), limit=int(top_n), now_iso=now_iso)
        result = T.triage(conn, rows, top_n=int(top_n), now_iso=now_iso)
        state = str(result.get("llm") or "unknown")
        if result.get("error"):
            print(f"[discovery] triage {state}: {result['error']}", file=sys.stderr, flush=True)
        return int(result.get("n", 0)), state
    except Exception as e:
        print(f"[discovery] triage skipped: {e}", file=sys.stderr, flush=True)
        return 0, "error"


def run_once(db_path, *, retention_hours=168.0, now_iso=None, host_now_iso=None, include_gt=True,
             do_triage=False):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        now_iso = now_iso or real_utc_now_iso()
        retention_hours = float(retention_hours)
        if retention_hours <= 0:
            raise ValueError("retention_hours must be positive")
        real_now = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
        host_now = datetime.fromisoformat(str(
            host_now_iso or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00"))
        repaired = m.repair_radar_future_timestamps(
            conn, now_iso=now_iso, host_skew_s=(host_now - real_now).total_seconds())
        # include_gt=False -> fast DexScreener-only pass (Solana/Base), leaving the runner's GT budget
        # alone. The GT (4663) leg runs only on the throttled full pass.
        n = D.discover(conn, now_iso=now_iso, include_gt=include_gt)
        q = D.refresh_universe_quotes(conn, now_iso=now_iso, include_gt=include_gt)
        cutoff = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                  - timedelta(hours=retention_hours)).isoformat()
        pruned = m.prune_external_discovery(conn, before_iso=cutoff)
        # #2 auto-exit-classifier: tailor exits for NEW real entries by live liquidity + momentum
        # (overlay override; never touches the frozen contract; always yields to an operator override).
        classified = _classify_new_entries(conn, now_iso)
        # #4 socials: refresh 'is anyone talking about it' on the throttled pass (socials change slowly).
        socials = D.refresh_socials(conn, now_iso=now_iso) if include_gt else 0
        # LLM triage on its own (slower) throttle — no-op without a key.
        triaged, triage_state = _maybe_triage(conn, now_iso) if do_triage else (0, "throttled")
        print(f"[discovery] {n} external coins, {q} quotes refreshed (gt={include_gt}); "
              f"pruned {pruned} stale coins; auto-classified {len(classified)} new entries; "
              f"socials {socials}; triaged {triaged} ({triage_state}); "
              f"clock-repaired {repaired['external'] + repaired['quotes']} rows", flush=True)
        return n, q
    finally:
        conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="sqlite path (default: state sniper_tracker.db)")
    ap.add_argument("--loop", type=float, default=0.0,
                    help="seconds between passes; 0 = single shot (for cron)")
    ap.add_argument("--retention-hours", type=float, default=168.0,
                    help="delete external rows not seen within this window (default: 168h)")
    ap.add_argument("--gt-interval", type=float,
                    default=float(os.getenv("MEMECOIN_RADAR_GT_INTERVAL_S", "300")),
                    help="min seconds between GT (4663) passes; DexScreener refreshes every loop")
    ap.add_argument("--triage-interval", type=float,
                    default=float(os.getenv("MEMECOIN_RADAR_TRIAGE_INTERVAL_S", "1800")),
                    help="min seconds between LLM triage passes (no-op without an LLM key; costs tokens)")
    args = ap.parse_args(argv)
    db_path = args.db or str(state_path("sniper_tracker.db"))
    last_gt = None                                    # force GT on the first pass
    last_triage = None                                # force triage on the first pass
    while True:
        now = time.monotonic()
        # Single-shot (cron) always does the full pass; the daemon throttles the GT leg to --gt-interval
        # while refreshing DexScreener (Solana/Base) every fast loop.
        do_gt = (not args.loop) or last_gt is None or (now - last_gt) >= float(args.gt_interval)
        if do_gt:
            last_gt = now
        # LLM triage on its own, slower throttle (it costs tokens) — no-op without a key regardless.
        do_triage = (not args.loop) or last_triage is None or (now - last_triage) >= float(args.triage_interval)
        if do_triage:
            last_triage = now
        try:
            run_once(db_path, retention_hours=args.retention_hours, include_gt=do_gt, do_triage=do_triage)
        except Exception as e:                       # never let the worker die on a transient fetch
            print(f"[discovery] pass failed: {e}", file=sys.stderr, flush=True)
        if not args.loop:
            break
        time.sleep(max(30.0, float(args.loop)))


if __name__ == "__main__":
    main()
