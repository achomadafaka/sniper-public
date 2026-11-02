#!/usr/bin/env python3
"""Scheduled Codex ping for the Hyperliquid Market Radar paper lane.

The script always writes a review prompt/report. By default it also invokes local Codex in
read-only mode and stores the final answer. Use ``--skip-codex`` for tests or dry runs.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.algos.hl_microstructure import persistence as P  # noqa: E402
from sniper.algos.hl_microstructure.review import build_performance_report, format_markdown_report  # noqa: E402


DEFAULT_DB = ROOT / "data" / "hl_microstructure_paper.sqlite"
DEFAULT_OUTPUT = ROOT / ".logs" / "hl_market_radar_codex_ping_latest.md"
DEFAULT_CODEX_OUTPUT = ROOT / ".logs" / "hl_market_radar_codex_review_latest.md"
DEFAULT_LOCK = ROOT / ".logs" / "hl_market_radar_codex_ping.lock"


def _mkdir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _build_prompt(db_path: Path) -> str:
    if not db_path.exists():
        report = f"Paper DB missing: {db_path}"
        experts = []
    else:
        conn = P.connect(str(db_path))
        try:
            report = format_markdown_report(build_performance_report(conn))
            try:
                experts = P.expert_summary(conn, limit=12)
            except sqlite3.Error:
                experts = []
        finally:
            conn.close()
    expert_lines = "\n".join(
        f"- {e['expert_id']} family={e['family']} settled={e['settled']} "
        f"hit={100 * e['accuracy']:.1f}% avg_edge_bps={10000 * e['avg_signed_return']:.2f}"
        for e in experts
    ) or "- No settled expert predictions yet."
    return f"""ROLE: Codex scheduled reviewer

You are reviewing the live paper-only Hyperliquid Market Radar strategy in this repo.
Read-only review. Do not edit files. Do not restart services. Do not place orders.

Questions to answer:
1. Is paper trading still live?
2. What is working?
3. What is bad?
4. Which expert horizons/sources look useful or harmful?
5. What one code or parameter change should be tested next?
6. Is any UI wording misleading?

Current paper report:
{report}

Expert lab:
{expert_lines}

Output terse PR-review style. Include no hype and no real-trade recommendation.
"""


def _run_codex(prompt: str, *, output: Path, timeout_seconds: int) -> str:
    codex = shutil.which("codex")
    if not codex:
        return "codex CLI not found; prompt written only.\n"
    _mkdir(output)
    cmd = [
        codex,
        "exec",
        "--sandbox",
        "read-only",
        "-C",
        str(ROOT),
        "--output-last-message",
        str(output),
        prompt,
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=timeout_seconds)
    if proc.returncode != 0:
        output.write_text(
            "Codex scheduled review failed.\n\nSTDOUT:\n"
            + proc.stdout
            + "\nSTDERR:\n"
            + proc.stderr
        )
        return f"codex exec failed rc={proc.returncode}; see {output}\n"
    return f"codex review written to {output}\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ping Codex to review Hyperliquid Market Radar paper performance")
    ap.add_argument("--db-path", default=str(DEFAULT_DB))
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    ap.add_argument("--codex-output", default=str(DEFAULT_CODEX_OUTPUT))
    ap.add_argument("--lock-path", default=str(DEFAULT_LOCK))
    ap.add_argument("--timeout-seconds", type=int, default=900)
    ap.add_argument("--skip-codex", action="store_true")
    args = ap.parse_args(argv)

    lock_path = Path(args.lock_path)
    _mkdir(lock_path)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("codex ping already running")
            return 0
        prompt = _build_prompt(Path(args.db_path))
        output = Path(args.output)
        _mkdir(output)
        output.write_text(prompt)
        if args.skip_codex:
            print(f"codex prompt written to {output}; skipped codex exec")
            return 0
        try:
            print(_run_codex(prompt, output=Path(args.codex_output), timeout_seconds=args.timeout_seconds).strip())
        except subprocess.TimeoutExpired:
            Path(args.codex_output).write_text("Codex scheduled review timed out.\n")
            print("codex exec timed out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
