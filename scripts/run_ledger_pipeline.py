"""Phase 7 — runtime CLI for `run_analysis_pipeline`.

Materialize one or more wallets from the local DuckDB ledger root,
run the protocol per wallet, optionally write the analysis row +
findings + warehouse artifact to Supabase via an explicit-constructor
`RestSupabaseHttp`. Dry-run is the default; `--write` opts in.

Wallet sources are mutually exclusive (Phase 7.2 D1):

* `--wallet <addr>` — single wallet (Phase 7.1 path).
* `--from-list <path>` — newline-delimited file of 0x addresses.
  Comments (`#`) and blank lines are skipped; invalid addresses are
  logged and skipped, never abort the batch.
* `--from-followed` — Phase 7.2 Task 2 will wire this to the local
  SQLite `db.get_followed_wallets(status='active')` accessor.

Per-wallet error isolation (D2): one wallet failing never aborts the
others. Final exit code is 1 if any wallet hit an exception, 0
otherwise. Skipped wallets (invalid address, missing warehouse, etc.)
do NOT bubble to exit code 1 — they are an expected steady state.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# Allow running as `python3 scripts/run_ledger_pipeline.py` without
# requiring an editable install — matches the
# `scripts/run_wallet_protocol.py:29` convention.
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover — dotenv is in requirements.txt
    load_dotenv = None

from sniper.analysis.wallet_protocol.ledger.run_pipeline import (  # noqa: E402
    run_analysis_pipeline,
)
from sniper.analysis.wallet_protocol.ledger.supabase_io import (  # noqa: E402
    RestSupabaseHttp,
)
from sniper.analysis.wallet_protocol.ledger.window import (  # noqa: E402
    AnalysisWindow,
)
from sniper.persistence.db_followed import (  # noqa: E402
    get_followed_wallets,
)


_WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="run_ledger_pipeline.py",
        description=(
            "Phase 7 runtime CLI: materialize one or more wallets from "
            "the local DuckDB ledger, run the protocol, optionally "
            "write the analysis row + findings + warehouse artifact to "
            "Supabase. Dry-run by default; --write opts in."
        ),
    )

    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--wallet",
        help="0x-prefixed wallet address (40 hex chars).",
    )
    source.add_argument(
        "--from-list",
        help=(
            "Path to a newline-delimited file of 0x addresses. "
            "Comments (#) and blank lines are skipped; invalid "
            "addresses log a warning and skip without aborting."
        ),
    )
    source.add_argument(
        "--from-followed",
        action="store_true",
        help=(
            "Read wallets from the local SQLite "
            "db.get_followed_wallets(status='active') accessor."
        ),
    )

    ap.add_argument(
        "--window-id",
        help=(
            "Stable analysis-window slug. Required unless --auto-window "
            "is set, in which case it is derived from the wallet's "
            "most recent completed capture run."
        ),
    )
    ap.add_argument(
        "--start-ts",
        type=int,
        help=(
            "Unix-epoch lower bound on facts (inclusive). Required "
            "unless --auto-window is set."
        ),
    )
    ap.add_argument(
        "--end-ts",
        type=int,
        help=(
            "Unix-epoch upper bound on facts (inclusive). Required "
            "unless --auto-window is set."
        ),
    )
    ap.add_argument(
        "--capture-run-id",
        action="append",
        help=(
            "One or more capture-run identifiers whose facts are "
            "materialized. Pass multiple times for multi-capture "
            "windows. Required unless --auto-window is set."
        ),
    )
    ap.add_argument(
        "--source-version",
        action="append",
        default=[],
        help=(
            "Optional key=value source-version pin. Pass multiple "
            "times. Tupled into AnalysisWindow.source_versions."
        ),
    )
    ap.add_argument(
        "--ledger-root",
        help=(
            "Directory containing <wallet>.duckdb, market_data.duckdb, "
            "index.duckdb. Falls back to LEDGER_ROOT env var when "
            "omitted."
        ),
    )
    ap.add_argument(
        "--run-id",
        help=(
            "Operator-supplied analysis run identity. Stable; re-runs "
            "with the same slug hit insert-ignore idempotency. "
            "Required unless --auto-window is set, in which case the "
            "default is the picked capture_run_id (D4)."
        ),
    )
    ap.add_argument(
        "--auto-window",
        action="store_true",
        help=(
            "Derive AnalysisWindow per-wallet from the most recent "
            "status='completed' capture_runs row in the wallet's "
            "warehouse. Ignores --window-id / --start-ts / --end-ts / "
            "--capture-run-id / --source-version when set; emits a "
            "one-line warning when those flags are also passed."
        ),
    )
    ap.add_argument(
        "--write",
        action="store_true",
        help=(
            "Opt in to Supabase writes. Requires "
            "LEDGER_RUNTIME_SUPABASE_URL and "
            "LEDGER_RUNTIME_SUPABASE_KEY in the environment."
        ),
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Bootstrap / permutation seed (default: 42).",
    )
    ap.add_argument(
        "--n-iter",
        type=int,
        default=10_000,
        help="Bootstrap / permutation iteration count (default: 10000).",
    )
    return ap


def _load_dotenv_if_available() -> None:
    if load_dotenv is None:
        return
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)


def _resolve_ledger_root(args: argparse.Namespace) -> Path | None:
    if args.ledger_root:
        return Path(args.ledger_root)
    env_value = os.environ.get("LEDGER_ROOT")
    if env_value:
        return Path(env_value)
    return None


def _resolve_supabase_env() -> tuple[str | None, str | None]:
    return (
        os.environ.get("LEDGER_RUNTIME_SUPABASE_URL"),
        os.environ.get("LEDGER_RUNTIME_SUPABASE_KEY"),
    )


def _validate_args(args: argparse.Namespace) -> tuple[Path | None, list[str]]:
    """Return `(ledger_root, errors)`. `errors` empty == valid."""
    errors: list[str] = []

    ledger_root = _resolve_ledger_root(args)
    if ledger_root is None:
        errors.append(
            "--ledger-root is required (or set the LEDGER_ROOT env var)."
        )

    # Explicit-mode requirements: when --auto-window is NOT set, the
    # original Phase 7.1 contract holds — operator supplies every
    # window field plus --run-id explicitly.
    if not args.auto_window:
        missing_explicit: list[str] = []
        if not args.window_id:
            missing_explicit.append("--window-id")
        if args.start_ts is None:
            missing_explicit.append("--start-ts")
        if args.end_ts is None:
            missing_explicit.append("--end-ts")
        if not args.capture_run_id:
            missing_explicit.append("--capture-run-id")
        if not args.run_id:
            missing_explicit.append("--run-id")
        if missing_explicit:
            errors.append(
                "the following arguments are required (without "
                "--auto-window): " + ", ".join(missing_explicit)
            )

    if args.write:
        url, key = _resolve_supabase_env()
        missing: list[str] = []
        if not url:
            missing.append("LEDGER_RUNTIME_SUPABASE_URL")
        if not key:
            missing.append("LEDGER_RUNTIME_SUPABASE_KEY")
        if missing:
            errors.append(
                "--write requires "
                + " and ".join(missing)
                + " in the environment."
            )
    return ledger_root, errors


def _resolve_code_version() -> str:
    """`git rev-parse --short HEAD` with `unknown` fallback."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip() or "unknown"


def _parse_source_versions(
    raw: list[str],
) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for entry in raw or []:
        if "=" not in entry:
            raise SystemExit(
                f"--source-version expected key=value, got: {entry!r}"
            )
        key, _, value = entry.partition("=")
        if not key or not value:
            raise SystemExit(
                f"--source-version expected key=value, got: {entry!r}"
            )
        out.append((key, value))
    return tuple(out)


def _build_window(args: argparse.Namespace) -> AnalysisWindow:
    return AnalysisWindow(
        window_id=args.window_id,
        capture_run_ids=tuple(args.capture_run_id),
        start_ts=int(args.start_ts),
        end_ts=int(args.end_ts),
        source_versions=_parse_source_versions(args.source_version),
    )


# ---------------------------------------------------------------------------
# Auto-window derivation (Phase 7.2 D3)
# ---------------------------------------------------------------------------


_AUTO_WINDOW_CONFLICT_FLAGS = (
    "--window-id",
    "--start-ts",
    "--end-ts",
    "--capture-run-id",
    "--source-version",
)


def _explicit_window_flags_passed(args: argparse.Namespace) -> bool:
    return any(
        [
            bool(args.window_id),
            args.start_ts is not None,
            args.end_ts is not None,
            bool(args.capture_run_id),
            bool(args.source_version),
        ]
    )


def _derive_auto_window(
    *,
    wallet: str,
    ledger_root: Path,
) -> AnalysisWindow | str:
    """Read `<ledger_root>/<wallet>.duckdb`'s `capture_runs` table and
    return an `AnalysisWindow` for the most recent completed capture.
    Returns a skip-reason string when no usable row exists.
    """
    import duckdb

    wallet_path = ledger_root / f"{wallet.lower()}.duckdb"
    if not wallet_path.is_file():
        return "warehouse_missing"

    try:
        with duckdb.connect(str(wallet_path), read_only=True) as conn:
            row = conn.execute(
                """
                SELECT run_id, started_at, finished_at,
                       source_versions_json
                FROM capture_runs
                WHERE status = 'completed'
                  AND finished_at IS NOT NULL
                ORDER BY finished_at DESC, started_at DESC
                LIMIT 1
                """
            ).fetchone()
    except duckdb.Error:
        return "warehouse_unreadable"

    if row is None:
        return "no_completed_capture"

    run_id, start_ts, finished_at, sv_json = row
    try:
        sv_dict = json.loads(sv_json) if sv_json else {}
    except (TypeError, ValueError):
        sv_dict = {}
    source_versions = tuple(
        (str(k), str(v))
        for k, v in sorted(sv_dict.items(), key=lambda kv: kv[0])
    )
    return AnalysisWindow(
        window_id=f"auto-{run_id}",
        capture_run_ids=(str(run_id),),
        start_ts=int(start_ts),
        end_ts=int(finished_at),
        source_versions=source_versions,
    )


# ---------------------------------------------------------------------------
# Wallet sources
# ---------------------------------------------------------------------------


def _strip_inline_comment(line: str) -> str:
    """Return the address portion of `line`, stripping comments + ws."""
    no_comment = line.split("#", 1)[0]
    return no_comment.strip()


def _resolve_wallets(args: argparse.Namespace) -> list[str]:
    """Return the list of wallet addresses to process.

    Invalid addresses are logged to stderr and skipped — they are not
    a hard error, so the batch can continue. Returns lower-cased
    addresses ready for the pipeline.
    """
    if args.wallet:
        return [args.wallet]

    if args.from_list:
        list_path = Path(args.from_list)
        if not list_path.is_file():
            print(
                f"--from-list: file not found: {list_path}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        out: list[str] = []
        for raw in list_path.read_text().splitlines():
            stripped = _strip_inline_comment(raw)
            if not stripped:
                continue
            if not _WALLET_RE.fullmatch(stripped):
                print(
                    f"wallet={stripped} SKIP reason=invalid_address",
                    file=sys.stderr,
                )
                continue
            out.append(stripped)
        return out

    if args.from_followed:
        rows = get_followed_wallets(status="active")
        out = []
        for row in rows:
            wallet = row["wallet"] if row["wallet"] is not None else ""
            wallet = str(wallet).strip()
            if not _WALLET_RE.fullmatch(wallet):
                print(
                    f"wallet={wallet} SKIP reason=invalid_address",
                    file=sys.stderr,
                )
                continue
            out.append(wallet)
        return out

    # argparse mutually-exclusive group with required=True guarantees
    # one of the source flags is set; this is unreachable.
    raise AssertionError("no wallet source resolved")


# ---------------------------------------------------------------------------
# Per-wallet execution
# ---------------------------------------------------------------------------


def _run_one_wallet(
    *,
    wallet: str,
    args: argparse.Namespace,
    window: AnalysisWindow,
    run_id: str,
    ledger_root: Path,
    code_version: str,
    supabase: object | None,
) -> str:
    """Run the pipeline for one wallet. Returns 'ok' / 'failed' /
    'skipped'. Prints the per-wallet stdout / stderr lines as a side
    effect."""
    try:
        result = run_analysis_pipeline(
            wallet=wallet,
            window=window,
            ledger_root=ledger_root,
            run_id=run_id,
            supabase=supabase,
            code_version=code_version,
            seed=args.seed,
            n_iter=args.n_iter,
        )
    except Exception as exc:
        print(
            f"wallet={wallet} ERROR class={type(exc).__name__} msg={exc}",
            file=sys.stderr,
        )
        return "failed"

    print(json.dumps(dataclasses.asdict(result.report), default=str))
    print(
        f"wallet={wallet} derived {len(result.findings)} findings",
        file=sys.stderr,
    )

    if args.write:
        wc = result.write_counts
        print(
            f"wallet={wallet} wrote "
            f"results={wc.get('wallet_analysis_results', 0)} "
            f"findings={wc.get('wallet_findings', 0)} "
            f"artifacts={wc.get('artifact_registry', 0)} "
            f"run_id={run_id}",
            file=sys.stderr,
        )
    return "ok"


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_available()
    parser = _build_parser()
    args = parser.parse_args(argv)

    ledger_root, errors = _validate_args(args)
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 2

    assert ledger_root is not None  # validation guarantees this

    if args.auto_window and _explicit_window_flags_passed(args):
        print(
            "warning: --auto-window ignores explicit window flags "
            f"({', '.join(_AUTO_WINDOW_CONFLICT_FLAGS)})",
            file=sys.stderr,
        )

    wallets = _resolve_wallets(args)
    code_version = _resolve_code_version()

    supabase = None
    if args.write:
        url = os.environ["LEDGER_RUNTIME_SUPABASE_URL"]
        key = os.environ["LEDGER_RUNTIME_SUPABASE_KEY"]
        supabase = RestSupabaseHttp(url=url, key=key)
        print(f"writing to {url}", file=sys.stderr)
    else:
        print(
            "dry-run: running pipeline without Supabase writes",
            file=sys.stderr,
        )

    explicit_window = (
        None if args.auto_window else _build_window(args)
    )

    counts = {"ok": 0, "failed": 0, "skipped": 0}
    for wallet in wallets:
        if args.auto_window:
            window_or_skip = _derive_auto_window(
                wallet=wallet, ledger_root=ledger_root
            )
            if isinstance(window_or_skip, str):
                print(
                    f"wallet={wallet} SKIP reason={window_or_skip}",
                    file=sys.stderr,
                )
                counts["skipped"] += 1
                continue
            window = window_or_skip
            run_id = args.run_id or window.capture_run_ids[0]
        else:
            assert explicit_window is not None
            window = explicit_window
            run_id = args.run_id

        outcome = _run_one_wallet(
            wallet=wallet,
            args=args,
            window=window,
            run_id=run_id,
            ledger_root=ledger_root,
            code_version=code_version,
            supabase=supabase,
        )
        counts[outcome] = counts.get(outcome, 0) + 1

    total = sum(counts.values())
    print(
        f"batch: ok={counts['ok']} failed={counts['failed']} "
        f"skipped={counts['skipped']} total={total}",
        file=sys.stderr,
    )
    return 1 if counts["failed"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
