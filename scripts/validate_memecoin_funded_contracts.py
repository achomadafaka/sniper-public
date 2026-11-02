#!/usr/bin/env python3
"""Read-only deployment/startup gate for the funded-contract cohort."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import sys


def _code_root(argv, default):
    """`--code-root DIR` (or `--code-root=DIR`) before argparse: the imports below need it."""
    for i, arg in enumerate(argv):
        if arg == "--code-root" and i + 1 < len(argv):
            return Path(argv[i + 1]).resolve()
        if arg.startswith("--code-root="):
            return Path(arg.split("=", 1)[1]).resolve()
    return default


def import_escape(module_file, root):
    """None when `module_file` lives under `root`, else the refusal text."""
    path = Path(module_file).resolve()
    return None if path.is_relative_to(root) else (
        f"funded contract reconciliation REFUSED: sniper imported from {path}, not from the code "
        f"root {root}; stale code would grade the release's rows")


# THE RELEASE GRADES ITS OWN ROWS (2026-10-21). A deploy whose checkout sat on a pre-#482 branch
# ran its own copy of this script with PYTHONPATH=$RELEASE, and the CHECKOUT's policy reader (no
# source-mirror-tp100 exemption) refused live rows the release accepts. The code root is now
# always first on sys.path, and an import from anywhere else refuses instead of grading.
ROOT = _code_root(sys.argv[1:], Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))

import sniper  # noqa: E402

_escape = import_escape(sniper.__file__, ROOT)
if _escape:
    print(_escape, file=sys.stderr)
    raise SystemExit(3)

from sniper.services.memecoin_funded_reconcile import (  # noqa: E402
    reconcile_severity,
)

# The sweep's own parse, when this tree can provide it. Byte integrity is not parseability: four
# `out_of_scope` contracts were authentic, consistent across every table, and unreadable by the
# exit sweep, so those positions ran with no rails (44703, 2026-08-26). Imported softly because
# this gate runs against a RELEASE TREE — a real release always carries the policy stack, so the
# fallback below is unreachable in production and says so loudly if that ever stops being true.
try:  # noqa: E402
    from sniper.services.memecoin_funded_policy import frozen_policy_from_position
except Exception:  # pragma: no cover - a real release always has it
    frozen_policy_from_position = None


def _load_operator_env(state_root):
    values = dict(os.environ)
    for name in (".env", ".fomo.env"):
        path = Path(state_root) / name
        if not path.is_file():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip().strip("'").strip('"'))
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--cutover-at")
    parser.add_argument("--code-root", help="tree whose sniper package grades the rows "
                        "(default: this script's own tree); read before argparse")
    args = parser.parse_args()

    if frozen_policy_from_position is None:
        print("funded contract reconciliation: policy stack unavailable in this tree — "
              "byte checks only, the exit-sweep parse did NOT run", file=sys.stderr)
    env = _load_operator_env(args.state_root)
    armed = str(env.get("MEMECOIN_REAL_ENABLED", "0")).strip().lower() in {
        "1", "true", "yes",
    }
    cutoff = (args.cutover_at or env.get(
        "MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT", "") or "").strip()
    if not cutoff:
        if armed:
            print(
                "funded contract reconciliation failed: armed runner requires "
                "MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT",
                file=sys.stderr,
            )
            return 1
        print("funded contract reconciliation skipped: real leg disarmed and no cutoff")
        return 0

    db_uri = f"file:{Path(args.db).resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True)
        blocking, warnings = reconcile_severity(
            conn, cutover_at=cutoff, policy_parse=frozen_policy_from_position)
        # Deploys are attended, so terminal audit debt is fatal here too (unchanged).
        errors = list(dict.fromkeys(blocking + warnings))
    except (OSError, sqlite3.Error) as exc:
        print(f"funded contract reconciliation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if "conn" in locals():
            conn.close()
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"funded contract reconciliation OK cutoff={cutoff}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
