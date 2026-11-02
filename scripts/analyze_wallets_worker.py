#!/usr/bin/env python3
"""Analyzer worker — drains the universal `analyzer_read_queue`, runs the full read on each
wallet (thesis + fresh-anomaly + archetype -> synthesis -> immutable read), rate-limited and
fail-loud. Operator/cron-triggered (or always-on where the host is in a Polymarket-permitted
region; the Amsterdam VPS is geo-restricted — see the design spec §6.3).

Subcommands:
    enqueue <wallet> [--source manual] [--priority N]   add a wallet to the queue (idempotent)
    run [--once] [--owner NAME] [--rps 8]               lease + analyze until the queue drains

The fetch path classifies region/auth refusals as GeoBlockedError so a geo-block surfaces and
leaves NO read — never a false `insufficient_evidence`. Pure cores are unit-tested; this live
wiring is exercised against the network.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.analysis.winning_wallet_thesis import orchestrator as ORCH
from sniper.analysis.winning_wallet_thesis import clv as CLV, funding_graph as FG
from sniper.persistence import db_wallet_reads as RD
from sniper.persistence import db_wallet_analysis_queue as Q

_STRUCTURAL = ("paired_outcome_arb", "two_sided_inventory_trader", "multi_outcome_basket_builder")
_GEO_HINTS = ("403", "451", "region", "blocked", "forbidden", "geo")
# Bump when the synthesis/verdict RULES change so a re-run supersedes prior reads even when the
# wallet's underlying data is unchanged (rule_version is part of the read idempotency key).
# v2 = post consumer-review verdict fixes (artifact gating, structural-exclude, axis-filtered domains).
# v3 = established significance = shuffle test only (fresh e-value no longer establishes deep wallets;
#      kills the favorite-scraper / huge-n marginal-oe false positive).
# v4 = new fresh_high_conviction verdict — a thin-history large-conviction wallet (gardenshed/GRIMDRIP)
#      surfaces for strategy review instead of burying it as no_supported_edge.
# v5 = fresh_high_conviction hardened (Codex P1): require assessable fresh read + no exclude/alt +
#      POSITIVE realized PnL (no 'made money' claim for losers; no stale-buyer / blind promotion).
# v6 = route label cleanup: new reads emit strategy_candidate, not the retired follow-book label.
_RULE_VERSION = 6


def _is_geo(err: Exception) -> bool:
    s = str(err).lower()
    return any(h in s for h in _GEO_HINTS)


def _archetype_for(wallet: str) -> dict:
    """Map the archetype extractor's snapshot features into the synthesis archetype view dict."""
    from wallet_archetype_extractor import extract_wallet_archetype  # scripts/ is on sys.path
    feats = extract_wallet_archetype(wallet) or {}
    cls = feats.get("classification", {}) or {}
    label = cls.get("archetype", "mixed_unresolved")
    layer2 = feats.get("layer2") or feats.get("intent") or {}
    structural = label in _STRUCTURAL
    return {"archetype": label, "intent": (layer2.get("archetype", "") if isinstance(layer2, dict) else ""),
            "structural_share_high": structural, "structural_share": 0.9 if structural else 0.1}


def analyze_wallet_live(conn, wallet: str, *, now_ts: int, test_index: int,
                        rate_limiter: ORCH.RateLimiter | None = None,
                        analyzer_version: int = 1, rule_version: int = _RULE_VERSION):
    """Fetch + analyze one wallet live. Raises GeoBlockedError on region/auth refusal."""
    from sniper.analysis.winning_wallet_thesis import cli as CLI
    from sniper.analysis.winning_wallet_thesis.ledger import LedgerFetchError
    from sniper.persistence import db_wallet_flags as F
    if F.is_deleted(conn, wallet):
        return None  # blacklisted (soft-deleted) — drain from the queue without analyzing, no read
    if rate_limiter:
        rate_limiter.acquire()
    try:
        profile, book, enriched, _t, _c = CLI.extract_profile_and_book(wallet)
        archetype = _archetype_for(wallet)
    except ORCH.GeoBlockedError:
        raise
    except LedgerFetchError as e:
        # Abnormal fetch termination. A region/auth refusal -> GeoBlockedError; any other
        # abnormal end re-raises -> generic fail with backoff. EITHER WAY: no read is written.
        if e.status in (401, 403, 451) or e.reason == "api_refused" or _is_geo(e):
            raise ORCH.GeoBlockedError(str(e)) from e
        raise
    except Exception as e:  # noqa: BLE001
        if _is_geo(e):
            raise ORCH.GeoBlockedError(str(e)) from e
        raise

    # Persist the thesis (best-effort) so the wallet also shows in the Analyzer tab's report list
    # alongside the canonical read. Mirrors cli.run_extractor(persist_store=True).
    try:
        from sniper.analysis.winning_wallet_thesis import db_store
        marks = CLI._fetch_open_marks(wallet, book)
        db_store.persist_run(profile=profile, book=book, slice_tables=_t, clusters=_c,
                             ledger=enriched.ledger, current_marks=marks)
    except Exception:  # noqa: BLE001 — persistence is best-effort; the read is the primary output
        pass

    raw = enriched.ledger.raw_activity
    clv_fn = CLV.build_clv_fn(raw)  # real CLOB price-history closure
    rpc = os.getenv("POLYGON_RPC_URL")
    funding = FG.build_funding(wallet, rpc_url=rpc, now_ts=now_ts)
    # data_as_of = latest data timestamp -> identical data yields one read (content-idempotent),
    # independent of wall-clock re-runs. now_ts stays the real clock for recent-window / funding age.
    data_as_of = max((int(getattr(e, "last_ts", 0) or 0) for e in book.episodes), default=int(now_ts))
    return ORCH.analyze_one(conn, wallet, thesis_profile=profile, book=book, archetype=archetype,
                            clv_fn=clv_fn, funding=funding, now_ts=now_ts, data_as_of=data_as_of,
                            test_index=test_index, analyzer_version=analyzer_version,
                            rule_version=rule_version)


def _next_test_index(conn) -> int:
    return Q.allocate_test_index(conn)


def cmd_enqueue(conn, args) -> None:
    from sniper.persistence import db_wallet_flags as F
    if F.is_deleted(conn, args.wallet):
        print(f"enqueue {args.wallet}: skipped (soft-deleted / blacklisted)")
        return
    ok = Q.enqueue(conn, args.wallet, args.source, source_ref=args.source_ref, reason=args.reason,
                   priority=args.priority)
    print(f"enqueue {args.wallet}: {'queued' if ok else 'already live (no-op)'}")


def cmd_run(conn, args) -> None:
    rl = ORCH.RateLimiter(rps=args.rps)
    processed = 0
    while True:
        now = int(time.time())
        row = Q.lease(conn, args.owner, now=now)
        if not row:
            break
        ti = _next_test_index(conn)
        ORCH.process_queue_row(
            conn, row,
            lambda w: analyze_wallet_live(conn, w, now_ts=int(time.time()), test_index=ti, rate_limiter=rl),
            now=int(time.time()))
        processed += 1
        if args.once:
            break
    print(f"processed {processed} wallet(s)")


def main() -> None:
    from db import get_connection  # project DB accessor
    ap = argparse.ArgumentParser(description="Analyzer full-read worker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enqueue")
    e.add_argument("wallet")
    e.add_argument("--source", default="manual")
    e.add_argument("--source-ref", default="")
    e.add_argument("--reason", default="")
    e.add_argument("--priority", type=int, default=100)
    r = sub.add_parser("run")
    r.add_argument("--once", action="store_true")
    r.add_argument("--owner", default="worker-1")
    r.add_argument("--rps", type=float, default=8.0)
    args = ap.parse_args()

    conn = get_connection()
    from sniper.persistence import db_wallet_flags as F
    RD.ensure_wallet_reads_tables(conn)
    Q.ensure_queue_tables(conn)
    F.ensure_wallet_flags_tables(conn)
    if args.cmd == "enqueue":
        cmd_enqueue(conn, args)
    elif args.cmd == "run":
        cmd_run(conn, args)


if __name__ == "__main__":
    main()
