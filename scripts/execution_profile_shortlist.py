#!/usr/bin/env python3
"""Execution profile shortlist runner — scheduled/manual.

Reads the union of `followed_wallets` and `watchlist_wallets`, runs the
extractor with `--with-execution` on each, and writes one JSON file per
wallet under `data/execution_profiles/<wallet>.json`.

Usage:
    # Default: read both tables, run all non-terminal wallets, write JSONs
    python3 scripts/execution_profile_shortlist.py

    # Force-run Layer 1 terminals too (for calibration / QA)
    python3 scripts/execution_profile_shortlist.py --force

    # Limit to N wallets (dev/debug)
    python3 scripts/execution_profile_shortlist.py --max-wallets 3

    # Alternate output directory
    python3 scripts/execution_profile_shortlist.py --out data/exec_profiles_qa/

Per spec §7.3 the shortlist runner was scoped out of the v1 execution-profile
implementation. This lands it as a standalone script: no systemd timer, no
brain-manifest wiring. Operators invoke it manually; adding a timer later is
one ExecStart= line.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.wallet_archetype_extractor import extract_wallet_archetype

logger = logging.getLogger("exec_profile_shortlist")

DEFAULT_OUT_DIR = ROOT / "data" / "execution_profiles"


def collect_shortlist(db_path: str) -> list[str]:
    """Union of followed_wallets.wallet and watchlist_wallets.wallet, lowercased + deduped."""
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        wallets: set[str] = set()
        for q in (
            "SELECT wallet FROM followed_wallets",
            "SELECT wallet FROM watchlist_wallets",
        ):
            try:
                for row in conn.execute(q):
                    w = str(row[0] or "").strip().lower()
                    if w and w.startswith("0x") and len(w) == 42:
                        wallets.add(w)
            except sqlite3.OperationalError as exc:
                logger.warning("skip %s: %s", q, exc)
        return sorted(wallets)
    finally:
        conn.close()


def profile_one(
    wallet: str,
    *,
    force: bool,
    skip_gate0: bool,
) -> dict:
    """Run the full extractor with execution profile, return the envelope."""
    return extract_wallet_archetype(
        wallet,
        skip_gate0=skip_gate0,
        with_execution=True,
        force_execution=force,
    )


def summarise(envelope: dict) -> dict:
    """One-line-per-wallet summary used for stdout + manifest."""
    cls = (envelope.get("classification") or {}).get("archetype")
    ep = envelope.get("execution_profile") or {}
    entry = ((envelope.get("edge_hypotheses") or {}).get("entry_timing_edge") or {})
    d1 = ep.get("drift_1h") or {}
    sk = ep.get("skew_vs_preceding_bar") or {}
    return {
        "wallet": envelope.get("wallet"),
        "classification": cls,
        "status": ep.get("status"),
        "reason": ep.get("reason", ""),
        "tier": ep.get("confidence_tier"),
        "n_skew": sk.get("n"),
        "n_drift_1h": d1.get("n"),
        "drift_1h_mean": d1.get("mean"),
        "skew_median": sk.get("median"),
        "entry_timing_level": entry.get("level"),
    }


def run(
    wallets: Iterable[str],
    *,
    out_dir: Path,
    force: bool,
    skip_gate0: bool,
) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries: list[dict] = []
    for idx, wallet in enumerate(wallets, start=1):
        t0 = time.time()
        try:
            envelope = profile_one(wallet, force=force, skip_gate0=skip_gate0)
            path = out_dir / f"{wallet}.json"
            path.write_text(json.dumps(envelope, indent=2, sort_keys=True))
            summary = summarise(envelope)
            summary["wall_seconds"] = round(time.time() - t0, 1)
            try:
                summary["out_path"] = str(path.relative_to(ROOT))
            except ValueError:
                summary["out_path"] = str(path)
            logger.info(
                "[%d] %s cls=%s status=%s tier=%s entry=%s (%.1fs)",
                idx, wallet, summary["classification"], summary["status"],
                summary["tier"], summary["entry_timing_level"], summary["wall_seconds"],
            )
        except Exception as exc:
            logger.exception("[%d] %s FAILED: %s", idx, wallet, exc)
            summary = {
                "wallet": wallet,
                "status": "runner_error",
                "reason": str(exc),
                "wall_seconds": round(time.time() - t0, 1),
            }
        summaries.append(summary)
    return summaries


def write_manifest(summaries: list[dict], out_dir: Path) -> Path:
    """Write a `_manifest.json` beside the per-wallet files for downstream discovery."""
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_wallets": len(summaries),
        "summaries": summaries,
    }
    path = out_dir / "_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Execution profile shortlist runner")
    parser.add_argument("--db-path", default=None, help="SQLite DB path (defaults to db.get_db_path())")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="Output directory")
    parser.add_argument("--force", action="store_true", help="Bypass Layer 1 terminal skip")
    parser.add_argument("--max-wallets", type=int, default=None, help="Cap for dev/debug")
    parser.add_argument("--skip-gate0", action="store_true", default=True, help="Skip gate0 profiler (default on — shortlist assumes prior filtering)")
    parser.add_argument("--include-gate0", dest="skip_gate0", action="store_false", help="Include gate0 profiler")
    parser.add_argument("--wallets", nargs="*", default=None, help="Explicit wallet list (overrides DB lookup)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    if args.wallets:
        wallets = [w.strip().lower() for w in args.wallets if w.strip()]
    else:
        from db import get_db_path
        db_path = args.db_path or get_db_path()
        wallets = collect_shortlist(db_path)

    if args.max_wallets is not None:
        wallets = wallets[: args.max_wallets]

    logger.info("shortlist runner: %d wallets → %s (force=%s)", len(wallets), args.out, args.force)
    summaries = run(wallets, out_dir=args.out, force=args.force, skip_gate0=args.skip_gate0)

    manifest_path = write_manifest(summaries, args.out)
    try:
        logger.info("wrote %s", manifest_path.relative_to(ROOT))
    except ValueError:
        logger.info("wrote %s", manifest_path)

    # stdout table
    print(f"\n{'wallet':44} {'cls':30} {'status':14} {'tier':8} {'entry':8} {'n_skew':>6} {'n_drift':>8} {'drift_1h_mean':>13}")
    for s in summaries:
        print(
            f"{(s.get('wallet') or '')[:42]:44} "
            f"{(s.get('classification') or '')[:28]:30} "
            f"{(s.get('status') or '')[:12]:14} "
            f"{(s.get('tier') or ''):8} "
            f"{(s.get('entry_timing_level') or ''):8} "
            f"{s.get('n_skew') or '—':>6} "
            f"{s.get('n_drift_1h') or '—':>8} "
            f"{s.get('drift_1h_mean') if s.get('drift_1h_mean') is not None else '—':>13}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
