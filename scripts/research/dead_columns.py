#!/usr/bin/env python3
"""Find columns nothing writes — the column-shaped sibling of `dead_dispatch_keys.py`.

THE PATTERN THIS CATCHES. A column exists, a reasonable query trusts it, and nothing ever fills it.
Four found by hand in one week: `route_quote_value`/`route_quote_at` (0/1041, while a controller
input was believed to come from them), `assigned_policy_id`/`_version`/`_json` (0/1042, while the
live policy lives in `funded_contract_json.policy`), `fee_paid_lamports`/`fee_paid_microlamports`
(0/1529 and 0/2177, so the book's gas cost was never recorded), and `deterministic_rejection`
(produced and read by nothing). Each cost an investigation to find.

ZERO NON-NULL IS NOT THE SAME AS DEAD, and that is the whole design. A brand-new column, a column
written only on a rare branch, and a column nothing will ever write all read identically at the
query. So this reports THREE states:

  EMPTY_NO_WRITER   zero non-null AND no write-shaped reference in sniper/ or scripts/.
                    The real find.
  EMPTY_HAS_WRITER  zero non-null but a writer exists — named file:line, so the next reader goes
                    to the branch instead of re-deriving it. `executable_peak_at` was exactly this:
                    a real writer behind a condition that never held.
  SPARSE            non-null below the threshold. The fraction is PRINTED AND NOT JUDGED —
                    `entry_mcap` at 84% is an availability limit, not a defect.

WHAT THE WRITER SEARCH CANNOT DO. It is a text search over write-SHAPED references and it is a
CANDIDATE FINDER, not a proof. It can miss a writer built from a dynamic column list, and it can
name a line that never executes. EMPTY_HAS_WRITER means "a human should read this line", never
"this column is fine". EMPTY_NO_WRITER means "no write-shaped reference was found", never "no
writer exists". Both verdicts are questions.

Read-only on the ledger. No RPC of any kind.

    python3 scripts/research/dead_columns.py [--db sniper_tracker.db] [--like memecoin_%]
                                             [--min-rows 200] [--sparse-under 0.9]
"""
import argparse
import pathlib
import re
import sqlite3
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SEARCH_DIRS = ("sniper", "scripts")
DDL_FILES = ("sniper/persistence/db_memecoin.py", "sniper/persistence/db_core.py")

# Columns whose emptiness is structural rather than interesting.
IGNORE = {"id", "rowid"}


# TWO QUESTIONS, NOT ONE (2026-09-08, Alpha review). "Is there a writer" and "does that writer ever
# receive a value" are different questions, they need different actions from a reader, and
# collapsing them is what let two of us call `route_quote_at` a persistence gap when its writer
# simply never runs.
#
# A reference is classified by its RIGHT-HAND SIDE:
#   literal None/NULL  -> this reference can never fill the column (a parameter default, or a clear)
#   anything else      -> this reference could supply a real value
#
# The multi-line case is not hypothetical. `route_degradation_since` is written as
# `changes["route_degradation_since"] = (` with the value below it, and an earlier matcher that
# had no dict-key pattern saw only the `= None` two lines down — so excluding null assignments
# flipped the column to "no writer at all", manufacturing a false negative inside the verdict that
# means "this is the real find". The dict-key pattern is what catches it; the open paren is simply
# a non-null RHS, so no separate continuation rule is needed. An earlier draft had one and
# mutation-testing showed it decided nothing.
# A kwarg list captures to end of line — `fee_paid_lamports=None, depth_first_rung_pct=None,`
# is one match whose RHS carries the NEXT argument too. Only the LEADING token is this
# column's value, so anchor on it and let a trailing comma or bracket end it.
_NULL_RHS = re.compile(r"^\s*(?:None|NULL)\s*(?:[,)\]].*)?$", re.I)


def _assignments(column, line):
    """Every right-hand side this line assigns to `column`; empty when it assigns none.

    Covers the kwarg form (`col=value`), the dict-key form (`d["col"] = value`) and the payload form
    (`{"col": value}`). An INSERT column list mentions the name WITHOUT assigning it and correctly
    yields nothing here — the name in a column list proves a writer exists but says nothing about
    the value, which is exactly the split this function serves.
    """
    c = re.escape(column)
    out = []
    for pat in (rf'\[\s*["\']{c}["\']\s*\]\s*=\s*(.*)$',
                rf'(?<![\w.]){c}\s*=\s*(?!=)(.*)$',
                rf'["\']{c}["\']\s*:\s*(.*)$'):
        for m in re.finditer(pat, line):
            out.append(m.group(1).strip())
    return out


_CORPUS = None


def _corpus():
    """Every source line, read ONCE. Re-walking the repo per column is O(columns x files) and did
    not finish on this tree."""
    global _CORPUS
    if _CORPUS is None:
        _CORPUS = []
        for d in SEARCH_DIRS:
            base = ROOT / d
            if not base.exists():
                continue
            for path in sorted(base.rglob("*.py")):
                if path.resolve() == pathlib.Path(__file__).resolve():
                    continue          # a scanner must not read its own prose (see below)
                try:
                    text = path.read_text(errors="ignore")
                except OSError:
                    continue
                _CORPUS.append((str(path.relative_to(ROOT)), text, text.splitlines()))
    return _CORPUS


def find_writers(column, limit=4):
    """(file:line, snippet, supplies_value) for every write-shaped reference to `column`.

    `supplies_value` is False only when every RHS on that line is a literal None/NULL.
    """
    hits = []
    for rel, text, lines in _corpus():
        if column not in text:
            continue
        for n, line in enumerate(lines, start=1):
            if column not in line:
                continue
            # A SCANNER MUST NOT READ ITS OWN PROSE. This file quotes
            # `changes["route_degradation_since"] = (` in a comment as the example of the shape it
            # matches, and the first grouped run duly reported this file as a writer of that
            # column. Its own path is excluded above; a comment ANYWHERE is excluded here, because
            # any file may describe a write without performing one.
            if line.lstrip().startswith("#"):
                continue
            rhs_list = _assignments(column, line)
            if not rhs_list:
                continue
            supplies = any(not _NULL_RHS.match(r) for r in rhs_list)
            hits.append((f"{rel}:{n}", line.strip()[:96], supplies))
            if len(hits) >= limit:
                return hits
    return hits


def column_age(column):
    """Earliest commit date that introduced the column's name into a DDL file, or None.

    A column added last week with no rows is EXPECTED, and a report that cannot say so invites the
    reader to treat a new column and an abandoned one identically.
    """
    for f in DDL_FILES:
        if not (ROOT / f).exists():
            continue
        try:
            out = subprocess.run(
                ["git", "log", "-S", column, "--format=%ad", "--date=short", "--", f],
                cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if out:
            return out.splitlines()[-1]        # oldest
    return None


def scan(conn, *, like, min_rows, sparse_under, where=None):
    """`where` scopes the population. THIS MATTERS MORE THAN IT LOOKS.

    `memecoin_positions` holds 30,352 paper rows and 1,042 funded ones, so a column only the funded
    path writes reads as "3% sparse" against the whole table. The arithmetic is right and the signal
    is worthless: the denominator is a different population from the writer. Scope the run to the
    population whose writer you are asking about, or every funded column looks abandoned.
    """
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name", (like,))]
    findings, skipped = [], []
    for t in tables:
        clause = f" WHERE {where}" if where else ""
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}{clause}").fetchone()[0]
        except sqlite3.Error:
            continue
        if n < min_rows:
            skipped.append((t, n))
            continue
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        for col in cols:
            if col in IGNORE:
                continue
            try:
                filled = conn.execute(
                    f'SELECT COUNT("{col}") FROM {t}{clause}').fetchone()[0]
            except sqlite3.Error:
                continue
            frac = filled / n if n else 0.0
            if filled == 0:
                writers = find_writers(col)
                if not writers:
                    state = "NO_WRITE_REFERENCE"
                elif any(supplies for _w, _s, supplies in writers):
                    # A reference that could carry a real value exists and the column is still
                    # empty, so the branch holding it never ran. `route_quote_at` and
                    # `route_degradation_since` are this: the reducer writes them, and the reducer
                    # has never won a CAS on a funded row because the contract disarms it.
                    state = "WRITER_NEVER_REACHED"
                else:
                    # Every reference assigns a literal None. The writer is real and no caller ever
                    # learned the value — `record_exit_execution` INSERTs `fee_paid_lamports` by
                    # name and its docstring says "actual fee stays NULL until observed". A pending
                    # state, not a dead column, and it needs a different fix from the case above.
                    state = "WRITER_ALWAYS_NULL"
            elif frac < sparse_under:
                writers, state = [], "SPARSE"
            else:
                continue
            findings.append({"table": t, "column": col, "rows": n, "filled": filled,
                             "frac": frac, "state": state, "writers": writers,
                             "added": column_age(col)})
    return findings, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--like", default="memecoin_%", help="table name filter (default memecoin_%%)")
    ap.add_argument("--min-rows", type=int, default=200,
                    help="skip tables under this row count — a column on a 12-row table says "
                         "nothing (default 200)")
    ap.add_argument("--where", default=None,
                    help="SCOPE THE POPULATION, e.g. \"execution_mode='real'\". Read this before "
                         "trusting any output: memecoin_positions holds 30,352 paper rows against "
                         "1,042 funded, so AN UNSCOPED RUN ON A MIXED TABLE WILL TELL YOU THE "
                         "FUNDED LANE IS ABANDONED. The arithmetic is right and the denominator is "
                         "a different population from the writer.")
    ap.add_argument("--sparse-under", type=float, default=0.9,
                    help="report a partially-filled column below this fraction (default 0.9)")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    try:
        findings, skipped = scan(conn, like=args.like, min_rows=args.min_rows,
                                 sparse_under=args.sparse_under, where=args.where)
    finally:
        conn.close()

    order = {"NO_WRITE_REFERENCE": 0, "WRITER_NEVER_REACHED": 1,
             "WRITER_ALWAYS_NULL": 2, "SPARSE": 3}
    findings.sort(key=lambda f: (order[f["state"]], -f["rows"], f["table"], f["column"]))

    if args.where:
        print(f"population: {args.where}")
    for state in ("NO_WRITE_REFERENCE", "WRITER_NEVER_REACHED",
                  "WRITER_ALWAYS_NULL", "SPARSE"):
        rows = [f for f in findings if f["state"] == state]
        if not rows:
            continue
        print(f"\n## {state}  ({len(rows)})")
        # GROUPED BY SHARED WRITER FILE, NOT COLLAPSED (2026-09-08, Alpha).
        # Collapsing a family decides what is INTERESTING on the reader's behalf and destroys
        # information; grouping only changes ordering. Twenty-three columns off one contract flag
        # is the finding, not noise around it — but a column whose writer sits ALONE in some other
        # file must not be buried behind twenty-two siblings, so the SMALLEST groups print first.
        groups = {}
        for f in rows:
            key = tuple(sorted({w.rsplit(":", 1)[0] for w, _s, _v in f["writers"]}))
            groups.setdefault(key, []).append(f)
        for key in sorted(groups, key=lambda k: (len(groups[k]), k)):
            members = groups[key]
            if key and len(members) > 1:
                where = key[0] if len(key) == 1 else ", ".join(key)
                print(f"  -- {len(members)} columns, all writers in {where}")
            for f in members:
                age = f["added"] or "age unknown"
                if state == "SPARSE":
                    print(f"  {f['table']}.{f['column']:34s} {f['filled']:7d}/{f['rows']:<7d} "
                          f"{100*f['frac']:5.1f}%   added {age}")
                else:
                    print(f"  {f['table']}.{f['column']:34s}       0/{f['rows']:<7d}         "
                          f"added {age}")
                for w, snippet, supplies in f["writers"]:
                    mark = "value" if supplies else "None "
                    print(f"        writer[{mark}] {w}  {snippet}")

    print(f"\nskipped {len(skipped)} table(s) under --min-rows={args.min_rows}")
    print("\nEvery verdict is a QUESTION.")
    print("  WRITER_NEVER_REACHED  a human should read that line — it means the branch did not run,")
    print("                        not that the column is fine.")
    print("  WRITER_ALWAYS_NULL    the writer is real and no caller supplied a value. Often a")
    print("                        documented pending state, not a defect.")
    print("  NO_WRITE_REFERENCE    no write-shaped reference was FOUND, not proof none exists.")
    print("  SPARSE                the fraction is printed and NOT judged: entry_mcap is an")
    print("                        availability limit, not a defect.")
    print("The writer search is a text search. It can miss a dynamic column list and it can name a")
    print("line that never executes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
