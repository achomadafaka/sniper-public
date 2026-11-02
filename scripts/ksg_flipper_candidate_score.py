#!/usr/bin/env python3
"""FH4 peer-population growth — candidate wallet scorer.

Given a gaps JSON (output of ksg_flipper_population_check --json-out) and a
source of candidate wallets (either a JSON file or the local SQLite DB), ranks
wallets by how well they fill the scarcest near/thin (grain, tag) gaps.

Usage:
    python3 scripts/ksg_flipper_candidate_score.py \
        --gaps-json /tmp/gaps.json \
        --wallet-tag-pnl-db sniper_tracker.db \
        --out /tmp/candidates.json \
        --limit 20
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path


def score_candidate_wallets(candidate_rows, gaps):
    """Score and rank candidate wallets against open population gaps.

    Only "near" and "thin" gaps generate scarcity weight. Wallets whose tags
    don't match any gap are excluded from output entirely.

    Args:
        candidate_rows: Iterable of dicts with keys: wallet, tag, and at least
            one of (n_positions | n_held) and (total_pnl | realized_pnl).
        gaps: Iterable of gap dicts from classify_population_gaps — must have
            keys: tag, population_status, peers_needed_to_ok.

    Returns:
        List of dicts sorted by score desc then wallet asc, each with:
        wallet, score, matched_target_tags.
    """
    # Collect all near/thin gaps; a tag may appear multiple times (different grains).
    target_gaps = [g for g in gaps if g.get("population_status") in {"near", "thin"}]
    by_wallet = defaultdict(lambda: {"wallet": "", "score": 0.0, "matched_target_tags": []})
    for row in candidate_rows:
        tag = row.get("tag")
        # Accumulate scarcity across every matching gap (handles multi-grain duplicates).
        matching = [g for g in target_gaps if g["tag"] == tag]
        if not matching:
            continue
        wallet = str(row["wallet"]).lower()
        entry = by_wallet[wallet]
        entry["wallet"] = wallet
        n_positions = float(row.get("n_positions") or row.get("n_held") or 0)
        total_pnl = abs(float(row.get("total_pnl") or row.get("realized_pnl") or 0))
        base_weight = (1.0 + min(n_positions, 100.0) / 100.0) * (1.0 + min(total_pnl, 100000.0) / 100000.0)
        for gap in matching:
            scarcity = 1.0 / max(1, int(gap["peers_needed_to_ok"]))
            entry["score"] += scarcity * base_weight
        entry["matched_target_tags"].append(tag)
    scored = list(by_wallet.values())
    for entry in scored:
        entry["matched_target_tags"] = sorted(set(entry["matched_target_tags"]))
        entry["score"] = round(float(entry["score"]), 6)
    return sorted(scored, key=lambda r: (-r["score"], r["wallet"]))


def load_wallet_tag_pnl_candidates(db_path, *, tags):
    """Read candidate rows from the local SQLite wallet_tag_pnl table.

    Args:
        db_path: Path to the SQLite DB file.
        tags: List of tag strings to filter on. Empty list → returns [] immediately.

    Returns:
        List of dicts with keys: wallet (lowercase), tag, n_positions, realized_pnl.
        Ordered by abs(realized_pnl) desc, n_positions desc.
    """
    if not tags:
        return []
    placeholders = ",".join("?" for _ in tags)
    sql = (
        f"SELECT wallet, tag, n_positions, realized_pnl "
        f"FROM wallet_tag_pnl "
        f"WHERE tag IN ({placeholders}) "
        f"ORDER BY ABS(realized_pnl) DESC, n_positions DESC"
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, tags).fetchall()
    finally:
        conn.close()
    return [
        {
            "wallet": str(r["wallet"]).lower(),
            "tag": r["tag"],
            "n_positions": int(r["n_positions"]),
            "realized_pnl": float(r["realized_pnl"]),
        }
        for r in rows
    ]


def load_gap_rows_from_json(path: str) -> list[dict]:
    """Load gap rows from a JSON file, accepting two shapes.

    Modern population summary writes `{target_peers: N, gaps: [...],
    ok: [...], near: [...], thin_top: [...]}` — the full flat `gaps`
    list is authoritative; the named views are operator-readability
    convenience. Legacy callers may pass a raw list of gap rows.

    Raises `ValueError` if neither shape matches, rather than silently
    producing zero rows (which would hide upstream contract drift).
    """
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("gaps"), list):
        return payload["gaps"]
    raise ValueError(
        f"--gaps-json {path!r} must be a raw gap list or a population "
        "summary dict containing a 'gaps' list; got "
        f"{type(payload).__name__} without a usable 'gaps' key"
    )


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Score candidate wallets against peer-population gaps.")
    p.add_argument("--gaps-json", required=True,
                   help="Path to gaps JSON produced by ksg_flipper_population_check --json-out")
    p.add_argument("--candidate-json", default=None,
                   help="Optional: path to a JSON list of candidate rows (wallet, tag, n_positions, total_pnl)")
    p.add_argument("--wallet-tag-pnl-db", default=None,
                   help="Optional: path to local SQLite DB with wallet_tag_pnl table")
    p.add_argument("--out", default=None,
                   help="Write scored output JSON to this path (default: stdout)")
    p.add_argument("--limit", type=int, default=50,
                   help="Max candidates to output (default 50)")
    args = p.parse_args(argv)

    # Read the full `gaps` list (or raw list for back-compat). Reading
    # near + thin_top would silently miss any thin gap outside the
    # truncated top-N (reviewer finding #5).
    gaps = [
        g for g in load_gap_rows_from_json(args.gaps_json)
        if g.get("population_status") in {"near", "thin"}
    ]

    candidate_rows: list[dict] = []
    if args.candidate_json:
        candidate_rows = json.loads(Path(args.candidate_json).read_text())
    elif args.wallet_tag_pnl_db:
        target_tags = list({g["tag"] for g in gaps})
        candidate_rows = load_wallet_tag_pnl_candidates(args.wallet_tag_pnl_db, tags=target_tags)
    else:
        print("ERROR: one of --candidate-json or --wallet-tag-pnl-db is required", file=sys.stderr)
        return 2

    scored = score_candidate_wallets(candidate_rows, gaps)[: args.limit]

    output = json.dumps(scored, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(output)
        print(f"Scored candidates written: {args.out}", file=sys.stderr)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
