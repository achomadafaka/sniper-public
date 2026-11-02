#!/usr/bin/env python3
"""Build a wallet dossier.

Spec: docs/superpowers/specs/2026-05-25-wallet-dossier-semantics-v0.md §5

Consumes:
  - WalletData fetch fixture JSON (e.g. tests/calibration/fixtures-v4-staging/<addr>_<date>.json)
  - ProtocolReport JSON (output of scripts/run_wallet_protocol.py --json)

Produces:
  - SQLite (default mode) at --out-db, OR
  - One JSON file per table at --json-export (test mode), OR
  - Both.

Current slice (`v0_complete`) ships twelve tables: positions,
position_exit_episodes, result_semantics, mechanism_evidence,
market_semantics, diagnostic_accounting, hypothesis_results, surfaces,
crosstabs, data_lineage, data_gaps, dossier_meta. The §10 markdown
report is generated via --out-md.

NO live API calls. NO mutation of ProtocolReport. NO LLM in the data path.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.analysis.wallet_dossier.builder import build_dossier  # noqa: E402
from sniper.analysis.wallet_dossier.schema import DOSSIER_SCHEMA_VERSION  # noqa: E402


def _default_out_db(wallet: str, fixture_path: Path) -> Path:
    """Per docstring: dossiers/v4/<wallet>_<fetched_at>.sqlite.

    Reads fetched_at from the fixture so repeat captures of the same
    wallet on different dates don't overwrite each other.
    """
    fetched_at = None
    try:
        fetched_at = json.loads(fixture_path.read_text()).get("fetched_at")
    except Exception:
        fetched_at = None
    suffix = str(fetched_at) if fetched_at is not None else "no_fetched_at"
    return Path("dossiers/v4") / f"{wallet.lower()}_{suffix}.sqlite"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Build a wallet dossier (v0 micro slice). "
            "Reads WalletData fixture + ProtocolReport JSON; "
            "writes SQLite and/or per-table JSON exports."
        )
    )
    p.add_argument("wallet", help="Wallet address (lowercased on insert).")
    p.add_argument(
        "--fixture", required=True, type=Path,
        help="Path to WalletData fetch fixture JSON (e.g. *_2026-05-25.json).",
    )
    p.add_argument(
        "--protocol-report", required=True, type=Path,
        help="Path to ProtocolReport JSON (run_wallet_protocol.py --json output).",
    )
    p.add_argument(
        "--out-db", type=Path, default=None,
        help="Destination SQLite path. Default: dossiers/v4/<wallet>_<fetched_at>.sqlite",
    )
    p.add_argument(
        "--out-md", type=Path, default=None,
        help="Markdown report destination (§10 structure A-J; K with --llm-summary).",
    )
    p.add_argument(
        "--json-export", type=Path, default=None,
        help="If set, also write per-table JSON files into this directory.",
    )
    p.add_argument(
        "--llm-summary", action="store_true",
        help="Append §10.K operator-requested slot-filling summary to the markdown.",
    )
    p.add_argument(
        "--since-days", type=int, default=None,
        help=(
            "Restrict the dossier to positions whose entry timestamp falls "
            "within the last N days of the fixture's fetched_at. Activity "
            "rows and redemptions outside the window are also dropped. "
            "Open positions are preserved (current snapshot)."
        ),
    )

    args = p.parse_args(argv)
    if args.since_days is not None and args.since_days <= 0:
        print("# error: --since-days must be a positive integer", file=sys.stderr)
        return 2

    if not args.fixture.exists():
        print(f"# error: fixture not found: {args.fixture}", file=sys.stderr)
        return 2
    if not args.protocol_report.exists():
        print(f"# error: protocol report not found: {args.protocol_report}", file=sys.stderr)
        return 2
    if args.llm_summary and args.out_md is None:
        print(
            "# error: --llm-summary requires --out-md (the LLM summary is "
            "section K of the markdown report).",
            file=sys.stderr,
        )
        return 2

    out_db = args.out_db
    if out_db is None and args.json_export is None:
        out_db = _default_out_db(args.wallet, args.fixture)

    tables = build_dossier(
        wallet=args.wallet,
        fixture_path=args.fixture,
        protocol_report_path=args.protocol_report,
        out_db=out_db,
        out_json_dir=args.json_export,
        out_md=args.out_md,
        include_llm_summary=args.llm_summary,
        since_days=args.since_days,
    )

    n_pos = len(tables.positions)
    n_ms = len(tables.market_semantics)
    n_da = len(tables.diagnostic_accounting)
    n_lin = len(tables.data_lineage)
    n_gap = len(tables.data_gaps)

    bucket_dist = {}
    for r in tables.diagnostic_accounting:
        bucket_dist[r["accounting_bucket"]] = bucket_dist.get(r["accounting_bucket"], 0) + 1
    mt_dist = {}
    for r in tables.market_semantics:
        mt_dist[r["market_type"]] = mt_dist.get(r["market_type"], 0) + 1

    res_dist: dict[str, int] = {}
    for r in tables.positions:
        v = r.get("selected_token_resolution") or "n/a"
        res_dist[v] = res_dist.get(v, 0) + 1

    print(f"# wallet={args.wallet.lower()} dossier built (schema={DOSSIER_SCHEMA_VERSION})")
    print(f"#   positions: {n_pos}")
    print(f"#   market_semantics: {n_ms}")
    print(f"#   diagnostic_accounting: {n_da}  buckets={bucket_dist}")
    print(f"#   market_type distribution: {mt_dist}")
    print(f"#   selected_token_resolution distribution: {res_dist}")
    print(f"#   data_lineage rows: {n_lin}")
    print(f"#   data_gaps: {n_gap}")
    if out_db is not None:
        print(f"# wrote {out_db}")
    if args.json_export is not None:
        print(f"# wrote JSON exports to {args.json_export}")
    if args.out_md is not None:
        print(
            f"# wrote markdown report to {args.out_md}"
            + (" (+ §10.K LLM summary)" if args.llm_summary else "")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
