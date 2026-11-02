#!/usr/bin/env python3
"""Bootstrap the local HL research workflow against a dedicated SQLite DB.

This keeps research tables out of the main runtime database and makes the full
backfill -> dataset -> analysis path runnable from a laptop.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "hl_research.db"
DEFAULT_OUTPUT_DIR = ROOT / "output" / "hl_research_local"
STEP_ORDER = ["funding", "price", "oi", "dataset", "v2", "combo", "regression"]


@dataclass(frozen=True)
class StepCommand:
    name: str
    cmd: list[str]
    capture_output: bool = False
    stdout_file: str | None = None


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def parse_steps(raw: str) -> list[str]:
    if not raw or raw == "all":
        return STEP_ORDER[:]
    steps = [part.strip().lower() for part in raw.split(",") if part.strip()]
    invalid = [step for step in steps if step not in STEP_ORDER]
    if invalid:
        raise ValueError(f"Unknown steps: {invalid}. Valid steps: {STEP_ORDER}")
    return steps


def reset_db_files(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()


def build_step_commands(args) -> list[StepCommand]:
    db = str(Path(args.db).resolve())
    output_dir = Path(args.output_dir).resolve()
    assets = args.assets
    months = str(args.months)

    commands = {
        "funding": StepCommand(
            name="funding",
            cmd=[
                sys.executable,
                "scripts/backfill_hl_funding.py",
                "--db",
                db,
                "--months",
                months,
                "--assets",
                assets,
            ],
        ),
        "price": StepCommand(
            name="price",
            cmd=[
                sys.executable,
                "scripts/backfill_binance_klines.py",
                "--db",
                db,
                "--months",
                months,
                "--assets",
                assets,
            ],
        ),
        "oi": StepCommand(
            name="oi",
            cmd=[
                sys.executable,
                "scripts/backfill_oi_history.py",
                "--db",
                db,
                "--months",
                months,
                "--assets",
                assets,
                *(
                    ["--require-coinalyze"]
                    if args.require_coinalyze
                    else []
                ),
            ],
        ),
        "dataset": StepCommand(
            name="dataset",
            cmd=[
                sys.executable,
                "scripts/build_research_dataset.py",
                "--db",
                db,
                "--assets",
                assets,
            ],
        ),
        "v2": StepCommand(
            name="v2",
            cmd=[
                sys.executable,
                "scripts/hl_research_v2.py",
                "--db",
                db,
            ],
            capture_output=True,
            stdout_file=str(output_dir / "hl_research_v2.txt"),
        ),
        "combo": StepCommand(
            name="combo",
            cmd=[
                sys.executable,
                "scripts/run_signal_research.py",
                "--db",
                db,
                "--json-out",
                str(output_dir / "signal_research_results.json"),
            ],
            capture_output=True,
            stdout_file=str(output_dir / "signal_research_summary.txt"),
        ),
        "regression": StepCommand(
            name="regression",
            cmd=[
                sys.executable,
                "scripts/hl_regression_analysis.py",
                "--db",
                db,
            ],
            capture_output=True,
            stdout_file=str(output_dir / "hl_regression_analysis.txt"),
        ),
    }
    return [commands[name] for name in parse_steps(args.steps)]


def run_step(step: StepCommand, cwd: Path) -> dict:
    started_at = time.time()
    print(f"\n=== STEP {step.name.upper()} ===")
    print(" ".join(step.cmd))

    if step.capture_output:
        result = subprocess.run(
            step.cmd,
            cwd=cwd,
            text=True,
            capture_output=True,
            env=os.environ.copy(),
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if step.stdout_file:
            Path(step.stdout_file).write_text(stdout + (("\n[stderr]\n" + stderr) if stderr else ""))
        if stdout:
            print(stdout, end="" if stdout.endswith("\n") else "\n")
        if result.returncode != 0 and stderr:
            print(stderr, end="" if stderr.endswith("\n") else "\n")
    else:
        result = subprocess.run(step.cmd, cwd=cwd, env=os.environ.copy())
        stdout = ""
        stderr = ""

    duration_s = round(time.time() - started_at, 2)
    return {
        "step": step.name,
        "returncode": result.returncode,
        "duration_s": duration_s,
        "stdout_file": step.stdout_file,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local HL research bootstrap")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Dedicated local research DB path")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for local research artifacts")
    parser.add_argument("--months", type=int, default=6, help="Months of history to backfill")
    parser.add_argument("--assets", default="BTC,ETH,SOL", help="Comma-separated assets")
    parser.add_argument(
        "--steps",
        default="all",
        help=f"Comma-separated subset of steps or 'all'. Valid: {', '.join(STEP_ORDER)}",
    )
    parser.add_argument(
        "--require-coinalyze",
        action="store_true",
        help="Fail if Coinalyze OI cannot be used for the OI step",
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Delete the local research DB and WAL/SHM files before running",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved commands without executing them",
    )
    parser.add_argument(
        "--list-steps",
        action="store_true",
        help="Print valid step names and exit",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")

    if args.list_steps:
        print("\n".join(STEP_ORDER))
        return

    try:
        steps = build_step_commands(args)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    db_path = Path(args.db).resolve()
    output_dir = Path(args.output_dir).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.require_coinalyze and not os.getenv("COINALYZE_API_KEY"):
        print("ERROR: --require-coinalyze was set but COINALYZE_API_KEY is missing")
        sys.exit(1)

    if args.reset_db:
        reset_db_files(db_path)

    print(f"Local HL research DB: {db_path}")
    print(f"Artifacts directory: {output_dir}")
    print(f"Steps: {[step.name for step in steps]}")

    if args.dry_run:
        for step in steps:
            print(" ".join(step.cmd))
        return

    manifest = {
        "db": str(db_path),
        "output_dir": str(output_dir),
        "months": args.months,
        "assets": args.assets,
        "steps": [],
        "started_at_epoch": int(time.time()),
    }

    for step in steps:
        result = run_step(step, ROOT)
        manifest["steps"].append(result)
        if result["returncode"] != 0:
            manifest["status"] = "failed"
            manifest["failed_step"] = step.name
            Path(output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
            print(f"ERROR: step '{step.name}' failed")
            sys.exit(result["returncode"])

    manifest["status"] = "ok"
    manifest["finished_at_epoch"] = int(time.time())
    Path(output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print("\nLocal HL research bootstrap completed.")
    print(f"Manifest: {output_dir / 'manifest.json'}")


if __name__ == "__main__":
    main()
