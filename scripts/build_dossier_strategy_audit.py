#!/usr/bin/env python3
"""Build a cross-wallet strategy audit from dossier JSON exports."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # bare-cron import bootstrap

from sniper.analysis.strategy_pattern_audit import (
    build_thesis_validation_workflow,
    compute_dossier_strategy_pattern_audit,
)


REQUIRED_TABLES = (
    "dossier_meta",
    "result_semantics",
    "mechanism_evidence",
    "thesis_candidates",
    "data_gaps",
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build Strategy Pattern Audit output from wallet dossier JSON exports."
    )
    parser.add_argument(
        "--dossier-json",
        action="append",
        required=True,
        help="Directory created by scripts/build_wallet_dossier.py --json-export.",
    )
    parser.add_argument("--out-json", required=True, help="Path for audit JSON output.")
    parser.add_argument(
        "--out-validation-json",
        help="Optional path for thesis validation workflow JSON output.",
    )
    parser.add_argument(
        "--dataset-inventory",
        default="",
        help="Comma-separated dataset names currently available for validation planning.",
    )
    parser.add_argument(
        "--min-supporting-wallets",
        type=int,
        default=2,
        help="Minimum wallets required to promote a cross-wallet thesis; clamped to 2.",
    )
    args = parser.parse_args(argv)

    dossiers = [_load_dossier_json_export(Path(p)) for p in args.dossier_json]
    audit = compute_dossier_strategy_pattern_audit(
        dossiers,
        min_supporting_wallets=args.min_supporting_wallets,
    )
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    if args.out_validation_json:
        inventory = [p.strip() for p in args.dataset_inventory.split(",") if p.strip()]
        workflow = build_thesis_validation_workflow(
            audit or {"theses": [], "data_gaps": []},
            dataset_inventory=inventory,
        )
        out_validation = Path(args.out_validation_json)
        out_validation.parent.mkdir(parents=True, exist_ok=True)
        out_validation.write_text(
            json.dumps(workflow, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return 0


def _load_dossier_json_export(path: Path) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    for table in REQUIRED_TABLES:
        table_path = path / f"{table}.json"
        if not table_path.exists():
            raise FileNotFoundError(f"missing dossier table export: {table_path}")
        payload = json.loads(table_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"{table_path} must contain a JSON array")
        tables[table] = payload
    return tables


if __name__ == "__main__":
    raise SystemExit(main())
