#!/usr/bin/env python3
"""Extract a wallet decision rule from a v5 dossier (Path A step 1).

Reads either a dossier JSON export directory (produced by
``scripts/build_wallet_dossier.py --json-export DIR``) or builds a
dossier in-process from a fixture + protocol report.

Outputs a wallet_rule_v1 JSON structure.

Usage:
    # From an existing JSON export
    python3 scripts/extract_wallet_rule.py \\
        --from-export /tmp/v5_batch/0xdbade.../json \\
        --out /tmp/swissmiss_rule.json

    # Build a dossier first
    python3 scripts/extract_wallet_rule.py \\
        --wallet 0xdbade... \\
        --fixture /tmp/v2_batch/0xdbade.../tagged.json \\
        --protocol-report /tmp/v2_batch/0xdbade.../report.json \\
        --out /tmp/swissmiss_rule.json

    # Scope to a specific grain (e.g. the cross-wallet-promoted one)
    python3 scripts/extract_wallet_rule.py \\
        --from-export /tmp/v5_batch/0xdbade.../json \\
        --target-grain-id 074d8dd7a8db1aba4fe011b2 \\
        --out /tmp/swissmiss_rule.json

No DB writes. No live API calls.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sniper.analysis.wallet_parameter_extractor import extract_wallet_rule  # noqa: E402


_DOSSIER_TABLES = (
    "dossier_meta",
    "result_semantics",
    "mechanism_evidence",
    "thesis_candidates",
    "data_gaps",
    "descriptor_hour_distribution",
    "descriptor_cadence",
    "descriptor_sizing",
    "descriptor_time_in_position",
    "descriptor_fills_per_position",
)


def _load_export(export_dir: Path) -> dict[str, list]:
    tables: dict[str, list] = {}
    for name in _DOSSIER_TABLES:
        path = export_dir / f"{name}.json"
        if not path.exists():
            tables[name] = []
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"{path} must contain a JSON array")
        tables[name] = payload
    return tables


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Extract a wallet decision rule (wallet_rule_v1) from a v5 dossier."
        )
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--from-export", type=Path,
        help="Directory of dossier JSON exports.",
    )
    src.add_argument(
        "--wallet",
        help="Wallet address; requires --fixture and --protocol-report.",
    )
    p.add_argument(
        "--fixture", type=Path,
        help="(With --wallet) WalletData fixture JSON.",
    )
    p.add_argument(
        "--protocol-report", type=Path,
        help="(With --wallet) ProtocolReport JSON.",
    )
    p.add_argument(
        "--target-grain-id",
        help="Scope rule extraction to this grain id. Default: best interpretable grain.",
    )
    p.add_argument(
        "--out", type=Path, required=True,
        help="Write the wallet_rule JSON here.",
    )
    args = p.parse_args(argv)

    if args.from_export:
        if not args.from_export.exists():
            print(f"# error: export dir not found: {args.from_export}", file=sys.stderr)
            return 2
        tables = _load_export(args.from_export)
    else:
        if not args.fixture or not args.protocol_report:
            print(
                "# error: --wallet requires both --fixture and --protocol-report",
                file=sys.stderr,
            )
            return 2
        if not args.fixture.exists():
            print(f"# error: fixture not found: {args.fixture}", file=sys.stderr)
            return 2
        if not args.protocol_report.exists():
            print(f"# error: protocol report not found: {args.protocol_report}", file=sys.stderr)
            return 2
        # Lazy import to keep this CLI light when only --from-export is used
        from sniper.analysis.wallet_dossier.builder import build_dossier
        tables = build_dossier(
            wallet=args.wallet,
            fixture_path=args.fixture,
            protocol_report_path=args.protocol_report,
        )

    rule = extract_wallet_rule(tables, target_grain_id=args.target_grain_id)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(rule, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"# wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
