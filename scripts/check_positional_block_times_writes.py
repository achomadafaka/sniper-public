#!/usr/bin/env python3
"""Refuse a release whose writers insert into a guarded table without naming their columns.

The guarded tables are listed in `_TABLES` below, with the reason each one is there.

WHY THIS EXISTS. `block_times` gained a fifth column, `block_hash`, on 2026-09-23 (6baf398e), so the
canonical-identity join could tell an orphan tick from a live one. Adding a column breaks any writer
that inserts POSITIONALLY: SQLite refuses four values for five columns, at prepare time, before any
row is written.

The two statements that break are both in `sniper/services/memecoin_volume_writer.py` in pre-6baf398e
code -- the live newHeads path, which runs on EVERY header, and the backfill's `executemany`. A
rollback to such a release therefore does not degrade the on-chain volume tape, it stops it on the
first block of every chain. (`sniper/persistence/db_ticks.py` was never affected: its statement named
its columns before the column existed and still does.)

None of the retained release directories contains the defect -- they all postdate the column -- but
`scripts/deploy_memecoin_runtime.sh <sha>` materializes a fresh release from ANY sha reachable from
origin/main, so rolling back past that commit is a supported operation. The old code cannot be
edited; the act of deploying it can be refused. That is what this does, at deploy time, with the
incompatibility named, instead of an OperationalError on the first header.

⚠️ A SOURCE-SCANNING GUARD MUST NOT READ ITS OWN PROSE (2026-08-27: a guard named the string it
forbade and failed on itself). Two defences, both structural rather than a file allowlist. The
pattern is assembled at runtime from fragments, so no forbidden statement exists as a literal
anywhere in this file; and the scan only looks at the SQL argument of an `execute`/`executemany`
call, so a docstring, a comment or a variable holding prose is not reachable by it at all.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

# WHICH TABLES THIS COVERS, and why it is a list rather than one name (2026-09-24). Every table
# here is written by code that a column addition would break at prepare time. `volume_quote_prices`
# joined on 2026-09-24 with three live positional writers of its own -- two in the volume writer's
# `set_quote_price`, which runs on every quote refresh for both chains, and one in the hop pricer's
# per-unit loop -- all three named in the same commit that widened this guard, because a guard that
# refuses the tree it ships with is not a guard.
#
# Each name is assembled, never written out whole: see the warning above.
_TABLES = ("block_" + "times", "volume_" + "quote_prices")
# The lookahead has to swallow the whitespace itself. `\s*(?!\()` backtracks to zero width and
# then sees a SPACE rather than the paren, so it matched every well-formed statement too -- the
# first version of this guard refused the current tree, all four of its own correct writers.
_POSITIONAL = re.compile(
    r"\binsert\b(?:\s+or\s+\w+)?\s+into\s+(" + "|".join(_TABLES) + r")\b(?!\s*\()",
    re.IGNORECASE)
SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", "site-packages", "archive"}


def sql_arguments(tree):
    """Every string literal passed as the first argument of an execute/executemany call.

    Anything else -- a docstring, a comment, a name, an f-string -- is out of reach by construction,
    which is what keeps this guard from matching its own explanation.
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("execute", "executemany"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            out.append((first.lineno, first.value))
    return out


def offenders(path):
    """(line, table, statement) for every positional write to a guarded table in this file."""
    try:
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    found = []
    for line, sql in sql_arguments(tree):
        flat = " ".join(sql.split())
        hit = _POSITIONAL.search(flat)
        if hit:
            found.append((line, hit.group(1), flat[:120]))
    return found


def scan(root):
    me = pathlib.Path(__file__).resolve()
    hits = []
    for path in sorted(pathlib.Path(root).rglob("*.py")):
        if set(path.parts) & SKIP_DIRS or path.resolve() == me:
            continue
        for line, table, sql in offenders(path):
            hits.append((path, line, table, sql))
    return hits


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    hits = scan(args.root)
    for path, line, table, sql in hits:
        print(f"{path}:{line}: positional write to {table}: {sql}", file=sys.stderr)
    if hits:
        tables = ", ".join(sorted({table for _p, _l, table, _s in hits}))
        print(f"deploy refused: {len(hits)} statement(s) insert into {tables} without naming "
              f"columns; that release cannot write those tables' current shape", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
