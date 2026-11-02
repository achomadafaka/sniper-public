#!/usr/bin/env python3
"""W5.3 — replay a previously-persisted analyzer run.

Loads ``analyzer_runs.args_json`` for ``<run_id>``, reconstructs the
``run_wallet_protocol.py`` argv, and re-executes the analysis. By default
runs the replay twice and asserts both outputs match within ``1e-9``
tolerance via :func:`sniper.analysis.wallet_protocol.replay.reports_match` —
self-consistency is what proves the analyzer is deterministic given a
fixed fixture and seed.

When ``--from-log <path>`` is given, the recorded ``analyze_completed``
event payload is used as the expected baseline instead. Use this when
the original run had non-deterministic live fetch and the JSONL log is
the only ground truth available — see ``docs/runbooks/analyzer-replay.md``.

Exit codes:
    0  reports match within tolerance
    1  reports diverge beyond tolerance
    2  missing run_id / malformed args / no recorded event
    3  replay sub-run failed (analyzer raised during re-execution)

Plan ref: docs/superpowers/plans/2026-04-30-analyzer-completion-phase-1.md W5.3.
Spec ref: docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md §7.4.
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

# Make repo importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.analysis.wallet_protocol.replay import reports_match


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_id")
    ap.add_argument(
        "--db", default="sniper_tracker.db",
        help="Path to SQLite DB containing analyzer_runs.",
    )
    ap.add_argument(
        "--from-log", default=None,
        help="Optional path to a per-run JSONL log. When set, the recorded "
             "analyze_completed event payload is the comparison baseline. "
             "Use this when the original run had non-deterministic live fetch.",
    )
    ap.add_argument(
        "--tol", type=float, default=1e-9,
        help="Absolute tolerance for float comparison (default 1e-9).",
    )
    return ap


def _load_run_row(db_path: str, run_id: str) -> sqlite3.Row | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM analyzer_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return row
    finally:
        conn.close()


# Mapping of args_json keys → CLI flag emitters. Keeps the CLI shape in
# one place so any new flag added to run_wallet_protocol.py is wired here
# in the same patch.
_BOOL_FLAGS = (
    ("capital_ledger", "--capital-ledger"),
    ("full_history", "--full-history"),
    ("no_tags", "--no-tags"),
    ("no_peers", "--no-peers"),
    ("no_exec", "--no-exec"),
    ("no_history", "--no-history"),
    ("no_archetype", "--no-archetype"),
    ("force_exec", "--force-exec"),
    ("analyst_fast", "--analyst-fast"),
)


def _argv_from_args_json(args_json: str, wallet: str) -> list[str]:
    """Reconstruct ``run_wallet_protocol.py`` argv from ``args_json``."""
    payload: dict[str, Any] = json.loads(args_json or "{}")
    argv: list[str] = [wallet]

    if payload.get("from_fixture"):
        argv += ["--from-fixture", str(payload["from_fixture"])]

    for key, flag in _BOOL_FLAGS:
        if payload.get(key):
            argv.append(flag)

    if "seed" in payload and payload["seed"] is not None:
        argv += ["--seed", str(int(payload["seed"]))]
    if "n_iter" in payload and payload["n_iter"] is not None:
        argv += ["--n-iter", str(int(payload["n_iter"]))]
    if payload.get("archetype"):
        argv += ["--archetype", str(payload["archetype"])]
    if "max_exec_trades" in payload and payload["max_exec_trades"] is not None:
        argv += ["--max-exec-trades", str(int(payload["max_exec_trades"]))]
    # G3 — paired arg with raw JSON string verbatim. We never re-serialize
    # a parsed dict here; key order across Python versions isn't stable.
    if payload.get("triage_context_json") is not None:
        argv += ["--triage-context-json", str(payload["triage_context_json"])]

    # Replay must never persist — the run is purely diagnostic.
    argv += ["--json", "--no-persist"]
    return argv


def _run_collect_json(argv: list[str]) -> dict:
    """Invoke ``run_wallet_protocol.main()`` and parse its JSON stdout."""
    import scripts.run_wallet_protocol as mod
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main(argv)
    if rc != 0:
        raise RuntimeError(
            f"run_wallet_protocol returned rc={rc} on argv={argv!r}"
        )
    text = buf.getvalue().strip()
    if not text:
        raise RuntimeError("run_wallet_protocol produced empty stdout")
    return json.loads(text)


def _load_event_payload_from_log(
    log_path: str, event_name: str,
) -> dict | None:
    """Return the most-recent payload for ``event_name`` in a JSONL log."""
    last: dict | None = None
    with Path(log_path).open() as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if row.get("event") == event_name:
                last = row.get("payload") or {}
    return last


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    row = _load_run_row(args.db, args.run_id)
    if row is None:
        print(
            f"# error: run_id={args.run_id!r} not found in {args.db}",
            file=sys.stderr,
        )
        return 2

    wallet = row["wallet"]
    args_json = row["args_json"] or "{}"
    try:
        runner_argv = _argv_from_args_json(args_json, wallet)
    except json.JSONDecodeError as e:
        print(
            f"# error: malformed args_json for run_id={args.run_id}: {e}",
            file=sys.stderr,
        )
        return 2

    print(
        f"# replaying run_id={args.run_id} wallet={wallet} "
        f"argv={runner_argv}",
        file=sys.stderr,
    )

    if args.from_log:
        recorded = _load_event_payload_from_log(
            args.from_log, "analyze_completed",
        )
        if recorded is None:
            print(
                f"# error: no analyze_completed event in {args.from_log!r}",
                file=sys.stderr,
            )
            return 2
        try:
            replayed = _run_collect_json(runner_argv)
        except Exception as e:
            print(f"# replay failed: {e!r}", file=sys.stderr)
            return 3
        matched, diffs = reports_match(recorded, replayed, tol=args.tol)
    else:
        try:
            a = _run_collect_json(runner_argv)
            b = _run_collect_json(runner_argv)
        except Exception as e:
            print(f"# replay failed: {e!r}", file=sys.stderr)
            return 3
        matched, diffs = reports_match(a, b, tol=args.tol)

    if matched:
        print(
            f"# replay match for run_id={args.run_id} (tol={args.tol})",
        )
        return 0

    print(
        f"# replay DIVERGED for run_id={args.run_id} (tol={args.tol})",
        file=sys.stderr,
    )
    for d in diffs:
        print(f"#   {d}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
