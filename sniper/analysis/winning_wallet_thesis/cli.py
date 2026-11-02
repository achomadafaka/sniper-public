"""CLI for the winning-wallet thesis extractor (operator-triggered, cached).

V1 intake is a single operator-curated active winner passed as an argument."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from sniper.analysis.winning_wallet_thesis import ledger as L, enrichment as E
from sniper.analysis.winning_wallet_thesis import position_builder as PB, slicer as S
from sniper.analysis.winning_wallet_thesis import clusters as C, assembler as A
from sniper.analysis.winning_wallet_thesis.types import ThesisProfile


def extract_profile_and_book(wallet: str, *, refresh: bool = False, incremental: bool = False,
                             with_fills: bool = False):
    """Build the full thesis pipeline once and return (profile, book, enriched, tables, clusters).

    Single source for run_extractor and the analyzer worker, so the worker doesn't re-fetch the
    ledger just to get the PositionBook (it needs the book for the fresh-anomaly engine)."""
    enriched = E.enrich_ledger(L.build_ledger(wallet, refresh=refresh, incremental=incremental), refresh=refresh)
    book = PB.build_position_book(enriched)
    tables = S.slice_edges(book)
    baseline = S.shuffle_baseline(book, n_shuffles=200, seed=1)
    clusters = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)
    rd = book.resolved_directional()
    trading_pnl = sum(e.realized_pnl or 0.0 for e in rd)
    ra = S.reward_attribution(enriched.ledger.raw_activity)
    share = S.reward_share_pct(trading_pnl=trading_pnl, reward_total=ra["usdc_total"])
    fill_identity = None
    if with_fills:                                  # opt-in: fetch Goldsky fills, tag via own activity (free)
        from sniper.feeds.goldsky_fills import build_onchain_fill_provider, asset_to_condition_from_activity
        amap = asset_to_condition_from_activity(enriched.ledger.raw_activity)
        fill_identity = build_onchain_fill_provider(wallet, asset_to_condition=amap, refresh=refresh)
    profile = A.assemble_profile(wallet, book, tables, baseline, clusters, reward_share=share,
                                 reward_non_usdc_flagged=ra["non_usdc_flagged"], fill_identity=fill_identity)
    return profile, book, enriched, tables, clusters


def run_extractor(wallet: str, *, refresh: bool = False, incremental: bool = False, with_fills: bool = False,
                  persist_store: bool = False) -> ThesisProfile:
    profile, book, enriched, tables, clusters = extract_profile_and_book(
        wallet, refresh=refresh, incremental=incremental, with_fills=with_fills)
    if persist_store:                               # materialize the already-built objects into the queryable store
        from sniper.analysis.winning_wallet_thesis import db_store
        marks = _fetch_open_marks(wallet, book)     # live /positions snapshot for still-open episodes
        db_store.persist_run(profile=profile, book=book, slice_tables=tables,
                             clusters=clusters, ledger=enriched.ledger, current_marks=marks)
    return profile


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_open_marks(wallet: str, book) -> dict | None:
    """Live mark for each still-open position from the Polymarket /positions snapshot, keyed by
    (condition_id, outcome_index): current_price / current_value / unrealized cash PnL as of NOW.
    Non-fatal — a failure just leaves open positions without a real-time mark."""
    has_open = any(not e.resolved and e.held_shares > 1e-6 for e in book.episodes)  # held incl. convert flow
    if not has_open:
        return None
    try:
        from sniper.markets import polymarket_api as P
        rows, _term = P.fetch_user_positions(None, wallet, "positions")
    except Exception:
        return None
    marks: dict = {}
    for r in rows:
        cond = str(r.get("conditionId") or r.get("condition_id") or "")
        try:
            oidx = int(r.get("outcomeIndex"))
        except (TypeError, ValueError):
            continue
        if cond:
            marks[(cond, oidx)] = {"current_price": _f(r.get("curPrice")),
                                   "current_value": _f(r.get("currentValue")),
                                   "unrealized_pnl": _f(r.get("cashPnl"))}
    return marks


def _print_human(p: ThesisProfile) -> None:
    print(f"WALLET {p.wallet}  run_id={p.run_id}")
    print(p.headline)
    sb = p.shuffle_baseline
    print(f"reward_share={p.reward_share_pct}%  "
          f"shuffle obs/exp={sb.obs_over_expected if sb else 'n/a'} "
          f"(observed={sb.observed_green if sb else '-'}, expected={sb.expected_green if sb else '-'})")
    if p.data_gaps:
        print("data gaps: " + "; ".join(p.data_gaps))
    print("EDGE CLUSTERS:")
    for c in p.edge_clusters[:12]:
        print(f"  [{c.axis:<9}] {c.label[:26]:<26} n={c.n_positions:>4} ev={c.n_events:>4} "
              f"edge={c.edge_pp:+6.1f}pp pnl=${c.pnl:>12,.0f}")
    print("LOSS CENTERS:")
    for c in p.loss_centers[:6]:
        print(f"  [{c.axis:<9}] {c.label[:26]:<26} n={c.n_positions:>4} pnl=${c.pnl:>12,.0f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Winning-wallet thesis extractor (Phase 1).")
    ap.add_argument("wallet")
    ap.add_argument("--refresh", action="store_true", help="force refetch activity/resolutions/tags")
    ap.add_argument("--json", action="store_true", help="emit the full profile as JSON")
    ap.add_argument("--no-save", action="store_true",
                    help="do not persist the profile JSON or queryable Analyzer store")
    ap.add_argument("--with-fills", action="store_true",
                    help="fetch Goldsky on-chain fills to unblock first-attention/microstructure methods")
    args = ap.parse_args()
    profile = run_extractor(args.wallet, refresh=args.refresh, with_fills=args.with_fills,
                            persist_store=not args.no_save)
    if not args.no_save:
        from sniper.analysis.winning_wallet_thesis.store import persist_profile
        saved = persist_profile(profile)
        print(f"[stored] {saved}", file=sys.stderr)
    if args.json:
        print(json.dumps(asdict(profile), default=str, indent=2))
    else:
        _print_human(profile)
