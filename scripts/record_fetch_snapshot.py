#!/usr/bin/env python3
"""W5.1 — record one wallet's fetch result into a JSON snapshot.

Wraps ``sniper.analysis.wallet_protocol.fetch.fetch_wallet_data`` and writes
the result through ``sniper.analysis.wallet_protocol.replay.save_fetch_snapshot``
so the same bytes can later drive ``run_wallet_protocol.py --from-fixture``
or ``replay_wallet_run.py``.

This is the only side-effecting step in the replay loop — it makes one live
fetch and writes one file. Downstream replay never touches the network.

Phase 2B-capture (fixture v2) extends this script to also stamp the
``archetype_layer1``, ``execution_profile``, and ``replay_surface_meta``
fields on the captured ``WalletData`` so replay can drive analyzer surfaces
without re-running classifiers.

Example:

    python3 scripts/record_fetch_snapshot.py \
        0xdbade4c82fb72780a0db9a38f821d8671aba9c95 \
        --out tests/replay/fixtures/swissmiss_fetch_snapshot.json \
        --capital-ledger

Plan ref: docs/superpowers/plans/2026-04-30-analyzer-completion-phase-1.md W5.1.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

# Make repo importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

import db
from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data
from sniper.analysis.wallet_protocol.copy_latency import copy_latency_grid_pairs
from sniper.analysis.wallet_protocol.replay import save_fetch_snapshot
from sniper.analysis.wallet_protocol.exec_profile_adapter import (
    build_execution_profile,
)
from sniper.analysis.wallet_protocol.flip_reconstruction import (
    reconstruct_flips,
)
from sniper.analysis.wallet_protocol.price_history_adapter import (
    build_price_history_fn,
)
from sniper.analysis.wallet_protocol.tag_inference import (
    enumerate_tag_grains,
    fetch_tag_lookups_from_db,
)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("wallet")
    ap.add_argument(
        "--out", required=True,
        help="Path to write the JSON snapshot to. Parent directories are created.",
    )
    ap.add_argument(
        "--seed", type=int, default=1,
        help="Reserved — kept for parity with run_wallet_protocol.py CLI; "
             "fetch is not seeded.",
    )
    ap.add_argument(
        "--full-history", action="store_true",
        help="Walk /activity windows backward to recover history beyond the "
             "3000-row offset cap.",
    )
    ap.add_argument(
        "--capital-ledger", action="store_true",
        help="Fetch the C1 capital ledger (SPLIT/MERGE/REDEMPTION) via the "
             "Goldsky activity-subgraph.",
    )
    ap.add_argument(
        "--no-archetype", action="store_true",
        help="Skip the offline archetype classifier; archetype_layer1 stays None.",
    )
    ap.add_argument(
        "--no-execution-profile", action="store_true",
        help="Skip execution-profile capture; execution_profile stays None.",
    )
    ap.add_argument(
        "--no-price-history", action="store_true",
        help="Reserved (Task 5) — skip price_history_lookups capture.",
    )
    ap.add_argument(
        "--no-copy-latency-grid", action="store_true",
        help="Skip the copy-latency post-entry price grid (entry, +5m, "
             "+15m, +60m per closed position). Cheaper capture; the "
             "copy_latency_surface stays data_unavailable.",
    )
    ap.add_argument(
        "--price-history-workers",
        type=int,
        default=max(1, int(os.getenv("PM_PRICE_HISTORY_WORKERS", "8"))),
        help="Concurrent workers for price_history_lookups capture.",
    )
    ap.add_argument(
        "--no-tags", action="store_true",
        help="Reserved (Task 5) — skip leafs_by_cid capture.",
    )
    ap.add_argument(
        "--no-nav", action="store_true",
        help="Reserved (Task 6) — skip nav_lookup capture.",
    )
    ap.add_argument(
        "--no-peers", action="store_true",
        help="Reserved (Task 6) — skip peer_baselines capture.",
    )
    ap.add_argument(
        "--db", default="sniper_tracker.db",
        help="SQLite path for surfaces that need historical reads (Tasks 5/6).",
    )
    return ap


_EXTRACTOR_FEATURE_KEYS = (
    ("trade_shape", "usdc_size_cv"),
    ("trade_shape", "top_condition_fill_share"),
    ("redeems", "redeem_ratio"),
    ("domain_tags", "primary_tag_share"),
    ("returns", "pnl_cv_abs"),
)


def _build_extractor_features_summary(features: dict) -> dict | None:
    """Edge Surface v1 — same shape as run_wallet_protocol.py's helper.

    Kept duplicated (not imported) to avoid an import-edge between the
    capture script and the runner.
    """
    if not isinstance(features, dict):
        return None
    summary: dict = {}
    for parent, key in _EXTRACTOR_FEATURE_KEYS:
        block = features.get(parent)
        if not isinstance(block, dict):
            continue
        if key not in block:
            continue
        out_key = "domain_concentration" if key == "primary_tag_share" else key
        summary[out_key] = block[key]
    return summary or None


def _meta_entry(status: str, source: str, producer: str, **extra) -> dict:
    """Build a replay_surface_meta entry with timestamp + provenance."""
    return {
        "status": status,
        "captured_at": int(time.time()),
        "source": source,
        "producer": producer,
        **extra,
    }


PriceHistoryFn = Callable[[str, int], float | None]


def _capture_price_history_lookups(
    required: set[tuple[str, int]],
    *,
    price_fn_factory: Callable[[], PriceHistoryFn],
    max_workers: int,
) -> tuple[dict[str, float | None], int]:
    ordered = sorted(required)
    if not ordered:
        return {}, 0

    workers = max(1, int(max_workers or 1))
    values: dict[tuple[str, int], float | None] = {}

    if workers == 1 or len(ordered) == 1:
        price_fn = price_fn_factory()
        for asset, ts in ordered:
            values[(asset, ts)] = price_fn(asset, ts)
    else:
        thread_state = threading.local()

        def _worker(pair: tuple[str, int]) -> tuple[tuple[str, int], float | None]:
            price_fn = getattr(thread_state, "price_fn", None)
            if price_fn is None:
                price_fn = price_fn_factory()
                thread_state.price_fn = price_fn
            asset, ts = pair
            return pair, price_fn(asset, ts)

        with ThreadPoolExecutor(max_workers=min(workers, len(ordered))) as pool:
            futures = [pool.submit(_worker, pair) for pair in ordered]
            for future in as_completed(futures):
                pair, value = future.result()
                values[pair] = value

    lookups: dict[str, float | None] = {}
    n_missing = 0
    for asset, ts in ordered:
        value = values.get((asset, ts))
        lookups[f"{asset}|{ts}"] = value
        if value is None:
            n_missing += 1
    return lookups, n_missing


def _request_failed_fetch_endpoints(data) -> list[str]:
    termination = getattr(data, "fetch_termination", None)
    if not isinstance(termination, dict):
        return []
    return sorted(
        str(endpoint)
        for endpoint, reason in termination.items()
        if reason == "request_failed"
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    wallet = args.wallet.lower()
    session = requests.Session()

    def _progress_emitter(_event_name: str, _payload: dict) -> None:
        return None

    print(f"# fetching {wallet}...", file=sys.stderr)
    data = fetch_wallet_data(
        wallet,
        session=session,
        archetype_extractor=None,
        walk_full_history=args.full_history,
        with_capital_ledger=args.capital_ledger,
        progress_emitter=_progress_emitter,
    )
    meta: dict[str, dict] = {}

    # Surface 1 — archetype (offline classifier).
    if args.no_archetype:
        meta["archetype_layer1"] = _meta_entry(
            "skipped", "cli flag --no-archetype",
            "wallet_archetype_extractor",
        )
    else:
        try:
            from scripts.wallet_archetype_extractor import (
                classify_wallet_archetype_from_snapshot,
                _fetch_tag_map,
                _fetch_wallet_tenure_metadata,
            )
            cids_for_archetype = [
                str(r.get("conditionId") or "")
                for r in data.activity_rows[:500]
            ] + [
                str(p.get("conditionId") or "") for p in data.open_positions
            ]
            archetype_tag_map = _fetch_tag_map(cids_for_archetype)
            archetype_tenure = _fetch_wallet_tenure_metadata(wallet)
            features = classify_wallet_archetype_from_snapshot(
                data.activity_rows[:500], data.open_positions,
                tag_map=archetype_tag_map,
                tenure_metadata=archetype_tenure,
                wallet=wallet, skip_gate0=True, with_execution=False,
            )
            data.archetype_layer1 = (
                features.get("classification", {}).get("archetype")
            )
            # Layer 2 is display-only; never feeds verdict logic. Captured
            # here so fixture-replay reports can show it alongside Layer 1.
            layer2_dict = features.get("classification_layer2") or {}
            if isinstance(layer2_dict, dict):
                data.archetype_layer2 = layer2_dict.get("archetype")
            # Edge Surface v1: capture the small extractor-feature subset.
            # Display-only — never feeds verdict logic.
            data.extractor_features_summary = (
                _build_extractor_features_summary(features)
            )
            meta["archetype_layer1"] = _meta_entry(
                "captured",
                "scripts.wallet_archetype_extractor.classify_wallet_archetype_from_snapshot",
                "wallet_archetype_extractor (offline classifier; tag_map + tenure_metadata pre-fetched)",
                input_activity_rows=min(len(data.activity_rows), 500),
                tag_map_size=len(archetype_tag_map),
                tenure_lifetime_source=archetype_tenure.get("wallet_lifetime_source"),
                tenure_pagination_complete=bool(archetype_tenure.get("pagination_complete")),
                archetype_layer2_captured=data.archetype_layer2,
            )
        except Exception as e:
            meta["archetype_layer1"] = _meta_entry(
                "failed", str(e)[:200],
                "wallet_archetype_extractor (offline classifier)",
            )

    # Surface 2 — execution profile.
    if args.no_execution_profile:
        meta["execution_profile"] = _meta_entry(
            "skipped", "cli flag --no-execution-profile",
            "exec_profile_adapter.build_execution_profile",
        )
    else:
        try:
            ep = build_execution_profile(
                wallet, data.activity_rows, data.closed_positions,
                archetype=data.archetype_layer1,
            )
            data.execution_profile = ep
            meta["execution_profile"] = _meta_entry(
                "captured",
                "scripts.execution_profile.compute_execution_profile",
                "exec_profile_adapter.build_execution_profile",
                inner_status=(ep or {}).get("status"),
            )
        except Exception as e:
            meta["execution_profile"] = _meta_entry(
                "failed", str(e)[:200],
                "exec_profile_adapter.build_execution_profile",
            )

    # Surface 3 — price_history_lookups
    H6_OFFSET = 24 * 3600  # mirrors held_analyst.H6_POST_SELL_OFFSET_SECONDS
    # Default — overwritten by every real path below; guarantees the
    # copy_latency_grid meta key exists even if the price-history section
    # exits abnormally (so "meta set on all paths" is literally true).
    meta["copy_latency_grid"] = _meta_entry(
        "skipped", "price-history section did not complete",
        "copy_latency.copy_latency_grid_pairs",
    )
    if args.no_price_history:
        meta["price_history_lookups"] = _meta_entry(
            "skipped", "cli flag --no-price-history",
            "price_history_adapter.build_price_history_fn",
        )
        meta["copy_latency_grid"] = _meta_entry(
            "skipped", "cli flag --no-price-history",
            "copy_latency.copy_latency_grid_pairs",
        )
    else:
        try:
            required: set[tuple[str, int]] = set()
            # H6 inputs: every SELL row's (asset, sell_ts + 24h)
            for row in data.activity_rows:
                if str(row.get("side", "")).upper() != "SELL":
                    continue
                asset = row.get("asset")
                ts = int(row.get("timestamp") or 0)
                if asset and ts:
                    required.add((asset, ts + H6_OFFSET))
            # FH6 inputs: every Flip's (asset_id, buy_ts) and (asset_id, sell_ts)
            flips = reconstruct_flips(data).flips
            for f in flips:
                if f.asset_id:
                    required.add((f.asset_id, int(f.buy_ts)))
                    required.add((f.asset_id, int(f.sell_ts)))

            def _price_fn_factory() -> PriceHistoryFn:
                return build_price_history_fn(session=requests.Session())

            lookups, n_missing = _capture_price_history_lookups(
                required,
                price_fn_factory=_price_fn_factory,
                max_workers=args.price_history_workers,
            )
            data.price_history_lookups = lookups
            meta["price_history_lookups"] = _meta_entry(
                "captured" if n_missing == 0 else "partial",
                "polymarket clob /prices-history",
                "price_history_adapter.build_price_history_fn",
                n_entries=len(lookups),
                n_missing=n_missing,
                workers=max(1, int(args.price_history_workers or 1)),
            )
        except Exception as e:
            meta["price_history_lookups"] = _meta_entry(
                "failed", str(e)[:200],
                "price_history_adapter.build_price_history_fn",
            )

        # Copy-latency post-entry grid — a SEPARATE capture pass at
        # fidelity=1 (the minute-scale-drift precedent; execution_profile
        # uses fidelity=1). Kept OUT of price_history_lookups: one
        # "<asset>|<ts>" key cannot safely hold both this fidelity and the
        # H6/FH6 default fidelity. Meta is written AFTER the pass with
        # resolved/missing counts, and marked failed in the except path.
        if args.no_copy_latency_grid:
            meta["copy_latency_grid"] = _meta_entry(
                "skipped", "cli flag --no-copy-latency-grid",
                "copy_latency.copy_latency_grid_pairs",
            )
        else:
            try:
                grid_pairs = copy_latency_grid_pairs(
                    data.closed_positions, data.activity_rows)

                def _grid_fn_factory() -> PriceHistoryFn:
                    return build_price_history_fn(
                        session=requests.Session(), fidelity=1)

                grid_lookups, grid_missing = _capture_price_history_lookups(
                    grid_pairs,
                    price_fn_factory=_grid_fn_factory,
                    max_workers=args.price_history_workers,
                )
                data.copy_latency_prices = grid_lookups
                meta["copy_latency_grid"] = _meta_entry(
                    "captured" if grid_missing == 0 else "partial",
                    "polymarket clob /prices-history fidelity=1",
                    "copy_latency.copy_latency_grid_pairs",
                    n_pairs=len(grid_pairs),
                    n_entries=len(grid_lookups),
                    n_missing=grid_missing,
                )
            except Exception as e:
                meta["copy_latency_grid"] = _meta_entry(
                    "failed", str(e)[:200],
                    "copy_latency.copy_latency_grid_pairs",
                )

    # Surfaces 4-5-6: leafs_by_cid + nav_lookup + peer_baselines
    need_db = not (args.no_tags and args.no_nav and args.no_peers)
    conn = sqlite3.connect(args.db) if need_db else None

    # Compute the condition-id universe ONCE up front so every DB-surface
    # branch can reference it. Universe = closed ∪ open ∪ flip-derived.
    # Always-defined; empty when wallet has no positions or activity.
    flips = reconstruct_flips(data).flips
    cids: set[str] = set()
    for p in data.closed_positions:
        cid = p.get("conditionId")
        if cid:
            cids.add(cid)
    for p in data.open_positions:
        cid = p.get("conditionId")
        if cid:
            cids.add(cid)
    for f in flips:
        if f.condition_id:
            cids.add(f.condition_id)

    if args.no_tags:
        meta["leafs_by_cid"] = _meta_entry(
            "skipped", "cli flag --no-tags",
            "tag_inference.fetch_tag_lookups_from_db",
        )
    if args.no_nav:
        meta["nav_lookup"] = _meta_entry(
            "skipped", "cli flag --no-nav",
            "tag_inference.fetch_tag_lookups_from_db",
        )

    if not args.no_tags or not args.no_nav:
        try:
            leafs_by_cid, nav_lookup = fetch_tag_lookups_from_db(conn, cids)
            if not args.no_tags:
                data.leafs_by_cid = leafs_by_cid
                meta["leafs_by_cid"] = _meta_entry(
                    "captured",  # empty dict still 'captured'
                    "db.fetch_market_tags_for_cids",
                    "tag_inference.fetch_tag_lookups_from_db",
                    db_path=args.db, n_entries=len(leafs_by_cid),
                )
            if not args.no_nav:
                data.nav_lookup = nav_lookup
                meta["nav_lookup"] = _meta_entry(
                    "captured", "db.fetch_tag_nav_category_map",
                    "tag_inference.fetch_tag_lookups_from_db",
                    db_path=args.db, n_entries=len(nav_lookup),
                )
        except Exception as e:
            if not args.no_tags:
                meta["leafs_by_cid"] = _meta_entry(
                    "failed", str(e)[:200],
                    "tag_inference.fetch_tag_lookups_from_db",
                )
            if not args.no_nav:
                meta["nav_lookup"] = _meta_entry(
                    "failed", str(e)[:200],
                    "tag_inference.fetch_tag_lookups_from_db",
                )

    if args.no_peers:
        meta["peer_baselines"] = _meta_entry(
            "skipped", "cli flag --no-peers",
            "db.peer_baseline_for_grain",
        )
    else:
        try:
            # Reuse leafs/navs if Tasks 4-5 captured them; otherwise re-fetch.
            # `cids` is always defined (computed above), so this branch works
            # even when --no-tags --no-nav were both set.
            leafs = data.leafs_by_cid
            navs = data.nav_lookup
            if leafs is None or navs is None:
                l_re, n_re = fetch_tag_lookups_from_db(conn, cids)
                leafs = leafs if leafs is not None else l_re
                navs = navs if navs is not None else n_re
            positions_for_enum = list(data.closed_positions) + list(data.open_positions)
            for f in flips:
                positions_for_enum.append(
                    {"conditionId": f.condition_id, "outcome": f.outcome},
                )
            enumeration = enumerate_tag_grains(positions_for_enum, leafs, navs)
            baselines: dict[str, list | None] = {}
            for tag in enumeration.leaf_grain_members.keys():
                result = db.peer_baseline_for_grain(
                    conn, "leaf", tag, exclude_wallet=wallet,
                )
                baselines[f"leaf|{tag}"] = (
                    [float(result[0]), int(result[1])] if result is not None else None
                )
            for nav in enumeration.nav_grain_members.keys():
                result = db.peer_baseline_for_grain(
                    conn, "nav", nav, exclude_wallet=wallet,
                )
                baselines[f"nav|{nav}"] = (
                    [float(result[0]), int(result[1])] if result is not None else None
                )
            data.peer_baselines = baselines
            meta["peer_baselines"] = _meta_entry(
                "captured", "db.peer_baseline_for_grain (exclude_wallet=<wallet>)",
                "db.peer_baseline_for_grain",
                db_path=args.db, exclude_wallet=wallet, n_entries=len(baselines),
            )
        except Exception as e:
            meta["peer_baselines"] = _meta_entry(
                "failed", str(e)[:200], "db.peer_baseline_for_grain",
            )

    if conn is not None:
        conn.close()

    data.replay_surface_meta = meta
    failed_endpoints = _request_failed_fetch_endpoints(data)
    if failed_endpoints:
        print(
            "# refusing to write partial fetch snapshot: "
            f"fetch_termination request_failed for {failed_endpoints}",
            file=sys.stderr,
        )
        return 2

    out_path = save_fetch_snapshot(data, args.out)
    print(
        f"# wrote {out_path} "
        f"(closed={len(data.closed_positions)} open={len(data.open_positions)} "
        f"activity={len(data.activity_rows)})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
