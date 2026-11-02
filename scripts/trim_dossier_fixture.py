"""Trim a full WalletData capture down to the fields the dossier consumes.

The full v4 captures under `tests/calibration/fixtures-v4-staging/` are
multi-megabyte and carry many fields the dossier never reads. For routine
CI regression we want a small, deterministic, in-repo fixture.

This script is the reproducible recipe — given a source capture, it emits
the trimmed JSON the dossier tests use. Run after capturing new fixtures.

Usage:
    python3 scripts/trim_dossier_fixture.py \\
        --in  tests/calibration/fixtures-v4-staging/0xWALLET_DATE.json \\
        --out tests/calibration/dossier-fixtures/0xWALLET_DATE.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allowlist of fields the dossier reads. Source of truth for what the
# trimmer keeps. Adding a new field to the dossier? Add it here too.
TOP_LEVEL_KEEP = (
    "wallet",
    "fetched_at",
    "schema_version",
    "closed_positions",
    "open_positions",
    "activity_rows",
    "redemptions",
    "leafs_by_cid",
)

CLOSED_POSITION_KEEP = (
    "conditionId",
    "asset",
    "oppositeAsset",
    "outcome",
    "title",
    "avgPrice",
    "totalBought",
    "realizedPnl",
    "cashPnl",
    "outcomeIndex",
    "curPrice",  # used by §7.5 rule 2 verification (flag is OFF in v0)
    "endDate",
    "timestamp",
)

OPEN_POSITION_KEEP = CLOSED_POSITION_KEEP + ("size",)

ACTIVITY_ROW_KEEP = (
    "type",
    "side",
    "asset",
    "conditionId",
    "price",
    "size",
    "timestamp",
    "outcomeIndex",
)

REDEMPTION_KEEP = (
    "condition",
    "indexSets",
    "payout",
    "timestamp",
)


def _trim_dict(d: dict, keep: tuple[str, ...]) -> dict:
    return {k: d[k] for k in keep if k in d}


def trim(src: dict) -> dict:
    out: dict = {k: src[k] for k in TOP_LEVEL_KEEP if k in src}
    if "closed_positions" in out:
        out["closed_positions"] = [_trim_dict(r, CLOSED_POSITION_KEEP) for r in out["closed_positions"] or []]
    if "open_positions" in out:
        out["open_positions"] = [_trim_dict(r, OPEN_POSITION_KEEP) for r in out["open_positions"] or []]
    if "activity_rows" in out:
        out["activity_rows"] = [_trim_dict(r, ACTIVITY_ROW_KEEP) for r in out["activity_rows"] or []]
    if "redemptions" in out:
        out["redemptions"] = [_trim_dict(r, REDEMPTION_KEEP) for r in out["redemptions"] or []]
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--in", dest="src", required=True, type=Path)
    p.add_argument("--out", dest="dst", required=True, type=Path)
    args = p.parse_args()

    src = json.loads(args.src.read_text())
    out = trim(src)
    args.dst.parent.mkdir(parents=True, exist_ok=True)
    args.dst.write_text(json.dumps(out, indent=2, sort_keys=True))
    src_size = args.src.stat().st_size
    dst_size = args.dst.stat().st_size
    print(
        f"trimmed {args.src} ({src_size:,} bytes) → "
        f"{args.dst} ({dst_size:,} bytes; "
        f"{100 * dst_size / src_size:.1f}% of source)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
