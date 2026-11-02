#!/usr/bin/env python3
"""Phase 1.4 — population check for wallet_flipper_tag_grain.

Reports which (grain, tag) pairs have enough peer mass for Phase 2's
FH4 inference to be runnable. Gate threshold: MIN_PEERS_FOR_INFERENCE = 10
(per Phase 0 spec §6 risk #1).

Output: stdout JSON + a markdown summary at /tmp/ksg_flipper_pop_<ts>.md.

Usage:
    python3 scripts/ksg_flipper_population_check.py
    python3 scripts/ksg_flipper_population_check.py --min-peers 5   # softer
    python3 scripts/ksg_flipper_population_check.py --target-peers 30 --near-window 5 --json-out /tmp/gaps.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Allow running from any cwd — add repo root to sys.path before sniper.* imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sniper.analysis.wallet_protocol.ledger.bot_supabase_adapter import BotSupabaseAdapter  # noqa: E402


def fetch_active_grain_rows(
    supabase,
    *,
    n_at_grain_floor: int,
    protocol_version: str | None = None,
) -> list[dict]:
    """Fetch active wallet_flipper_tag_grain rows above the floor.

    Routes through `BotSupabaseAdapter.select(...)` so Range-header
    pagination is honored. Operator filters go through `where_ops`
    instead of a raw `_request(...limit=10000)` call — the latter
    silently undercounts once the table exceeds PostgREST's
    `db-max-rows` cap (reviewer finding #1, 2026-05-17).
    """
    where: dict[str, object] = {
        "status": "active",
        "walk_full_history": True,
    }
    if protocol_version is not None:
        where["protocol_version"] = protocol_version
    return supabase.select(
        "wallet_flipper_tag_grain",
        where=where,
        where_ops={"n_at_grain": ("gte", n_at_grain_floor)},
        columns=("wallet", "grain", "tag", "grain_roi", "n_at_grain", "protocol_version"),
    )


def build_gaps_payload(
    rows: list[dict],
    *,
    target_peers: int,
    near_window: int,
    n_at_grain_floor: int,
    protocol_version: str | None,
    n_active_rows_baseline: int,
    thin_top_limit: int = 20,
) -> dict:
    """Build the JSON contract emitted to stdout / --json-out.

    Downstream tools (candidate scorer) consume the `gaps` key, which
    is the FULL flat list from `classify_population_gaps`. The `ok` /
    `near` / `thin_top` keys are operator-readability views derived
    from `gaps` — `thin_top` truncates to the first N for terminal
    output, but `gaps` is never truncated (reviewer finding #5).
    """
    gaps = classify_population_gaps(
        rows, target_peers=target_peers, near_window=near_window,
    )
    return {
        "target_peers": target_peers,
        "near_window": near_window,
        "n_at_grain_floor": n_at_grain_floor,
        "protocol_version_filter": protocol_version,
        "n_active_rows_baseline": n_active_rows_baseline,
        "n_distinct_grain_tags": len({(g["grain"], g["tag"]) for g in gaps}),
        "gaps": gaps,
        "ok": [g for g in gaps if g["population_status"] == "ok"],
        "near": [g for g in gaps if g["population_status"] == "near"],
        "thin_top": [g for g in gaps if g["population_status"] == "thin"][:thin_top_limit],
    }


def classify_population_gaps(rows, *, target_peers=30, near_window=5):
    """Classify (grain, tag) pairs by peer-population status.

    Args:
        rows: Iterable of dicts with keys: wallet, grain, tag, grain_roi.
        target_peers: Target minimum peer count for "ok" status.
        near_window: Gap ≤ this many peers → "near" instead of "thin".

    Returns:
        List of dicts sorted by (peers_needed_to_ok asc, n_peers desc, grain, tag).
        Each dict has: grain, tag, n_peers, target_peers, peers_needed_to_ok,
        population_status ("ok" | "near" | "thin"), mean_flip_roi, wallets.
    """
    by_grain_tag = {}
    for r in rows:
        key = (r["grain"], r["tag"])
        by_grain_tag.setdefault(key, []).append(r)
    out = []
    for (grain, tag), wallet_rows in by_grain_tag.items():
        wallets = sorted({r["wallet"] for r in wallet_rows})
        n_peers = len(wallets)
        mean_roi = sum(float(r["grain_roi"]) for r in wallet_rows) / len(wallet_rows)
        peers_needed = max(0, target_peers - n_peers)
        if n_peers >= target_peers:
            status = "ok"
        elif peers_needed <= near_window:
            status = "near"
        else:
            status = "thin"
        out.append({
            "grain": grain, "tag": tag, "n_peers": n_peers, "target_peers": target_peers,
            "peers_needed_to_ok": peers_needed, "population_status": status,
            "mean_flip_roi": round(mean_roi, 6), "wallets": wallets,
        })
    return sorted(out, key=lambda r: (r["peers_needed_to_ok"], -r["n_peers"], r["grain"], r["tag"]))


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--min-peers", type=int, default=10,
                   help="Floor: only report (grain, tag) pairs with at least this many wallets (default 10)")
    p.add_argument("--protocol-version", default=None,
                   help="Optional: filter to one protocol version")
    p.add_argument("--n-at-grain-floor", type=int, default=30,
                   help="Per-wallet n_at_grain floor for inclusion in the peer set (default 30, per spec)")
    p.add_argument("--target-peers", type=int, default=30,
                   help="Target peer count for classify_population_gaps (default 30)")
    p.add_argument("--near-window", type=int, default=5,
                   help="Peers-needed gap threshold for 'near' vs 'thin' (default 5)")
    p.add_argument("--json-out", default=None,
                   help="If set, write classify_population_gaps JSON output to this path")
    args = p.parse_args(argv)

    supabase = BotSupabaseAdapter.from_env_or_dotenv()

    # Paginated fetch — the adapter walks PostgREST's `db-max-rows` cap
    # via Range headers, and `where_ops` keeps the n_at_grain floor
    # server-side. See fetch_active_grain_rows() docstring for why a
    # raw `_request(... limit=10000)` is forbidden here.
    rows = fetch_active_grain_rows(
        supabase,
        n_at_grain_floor=args.n_at_grain_floor,
        protocol_version=args.protocol_version,
    )

    print(f"Total active rows passing baseline filter: {len(rows)}", file=sys.stderr)

    by_grain_tag: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        key = (r["grain"], r["tag"])
        by_grain_tag.setdefault(key, []).append(r)

    qualifying: list[dict] = []
    near_qualifying: list[dict] = []
    for (grain, tag), wallets_rows in by_grain_tag.items():
        # Use deduplicated wallet count so this summary matches classify_population_gaps output.
        n_peers = len({w["wallet"] for w in wallets_rows})
        mean_roi = sum(w["grain_roi"] for w in wallets_rows) / len(wallets_rows)
        entry = {
            "grain": grain,
            "tag": tag,
            "n_peers": n_peers,
            "mean_flip_roi": round(mean_roi, 4),
            "wallets_sample": sorted({w["wallet"][:14] + "…" for w in wallets_rows})[:5],
        }
        if n_peers >= args.min_peers:
            qualifying.append(entry)
        elif n_peers >= max(1, args.min_peers - 5):
            near_qualifying.append(entry)

    qualifying.sort(key=lambda e: -e["n_peers"])
    near_qualifying.sort(key=lambda e: -e["n_peers"])

    summary = {
        "min_peers_floor": args.min_peers,
        "n_at_grain_floor": args.n_at_grain_floor,
        "protocol_version_filter": args.protocol_version,
        "n_active_rows_baseline": len(rows),
        "n_distinct_grain_tags": len(by_grain_tag),
        "n_qualifying_for_phase2": len(qualifying),
        "n_near_qualifying": len(near_qualifying),
        "qualifying_grain_tags": qualifying[:50],
        "near_qualifying_grain_tags": near_qualifying[:20],
    }

    # Old min_peers_floor / qualifying_grain_tags summary — operator
    # observability only. Goes to stderr so the canonical stdout JSON
    # (gaps_out below) stays pipe-clean for downstream tools.
    print(json.dumps(summary, indent=2, default=str), file=sys.stderr)

    # Canonical stdout: full gaps payload. Downstream tools must read
    # `gaps` here (or via --json-out), not the truncated `thin_top` view.
    gaps_out = build_gaps_payload(
        rows,
        target_peers=args.target_peers,
        near_window=args.near_window,
        n_at_grain_floor=args.n_at_grain_floor,
        protocol_version=args.protocol_version,
        n_active_rows_baseline=len(rows),
    )
    print(json.dumps(gaps_out, indent=2, default=str))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(gaps_out, indent=2, default=str))
        print(f"Gaps JSON written: {args.json_out}", file=sys.stderr)

    # Markdown report
    ts = int(time.time())
    md_path = Path(f"/tmp/ksg_flipper_pop_{ts}.md")
    lines = [
        f"# Flipper Tag-Grain Population Check\n",
        f"**Generated:** {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ts))}",
        f"**Floor:** min_peers={args.min_peers}, n_at_grain≥{args.n_at_grain_floor}, walk_full_history=true",
        f"",
        f"## Summary",
        f"- Active rows baseline: {len(rows)}",
        f"- Distinct (grain, tag) pairs: {len(by_grain_tag)}",
        f"- **Pairs qualifying for Phase 2 FH4 inference:** {len(qualifying)}",
        f"- Pairs within 5 peers of qualifying: {len(near_qualifying)}",
        f"",
    ]
    if qualifying:
        lines += [
            f"## Qualifying (grain, tag) pairs (Phase 2 FH4 can run)",
            f"",
            f"| Grain | Tag | n_peers | mean_flip_roi |",
            f"|---|---|---:|---:|",
        ]
        for e in qualifying:
            lines.append(f"| {e['grain']} | {e['tag']} | {e['n_peers']} | {e['mean_flip_roi']:+.4f} |")
        lines.append("")
    else:
        lines += [
            f"## No qualifying (grain, tag) pairs at the current floor",
            f"",
            f"Phase 2 cannot proceed with FH4 inference on any (grain, tag) at the configured floor.",
            f"Next-step options:",
            f"1. Lower the floor (e.g. min_peers=5) — increases false-positive risk in the aggregator.",
            f"2. Seed more wallets via additional --persist-flipper-grains runs.",
            f"3. Defer Phase 2 until organic analyzer-run population grows.",
            f"",
        ]
    if near_qualifying:
        lines += [
            f"## Near-qualifying (within 5 peers of floor)",
            f"",
            f"| Grain | Tag | n_peers | mean_flip_roi |",
            f"|---|---|---:|---:|",
        ]
        for e in near_qualifying:
            lines.append(f"| {e['grain']} | {e['tag']} | {e['n_peers']} | {e['mean_flip_roi']:+.4f} |")

    md_path.write_text("\n".join(lines) + "\n")
    print(f"\nMarkdown report: {md_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
