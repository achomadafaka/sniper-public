#!/usr/bin/env python3
"""Campaign-level guard for the crypto5m maker.

Reads the maker DB, evaluates the pre-registered campaign kill gate, and can
pause the paper maker if the hard gate fires. No network, no orders.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sniper.algos.polymarket_crypto5m import maker_regime_report as RPT  # noqa: E402


def evaluate_campaign_guard(conn, campaign: str, *, min_windows: int = 200,
                            interim_min_windows: int = 60,
                            bootstrap_samples: int | None = None) -> dict:
    hard = RPT._campaign_eval(  # noqa: SLF001 - guard is intentionally a thin wrapper around the judge.
        conn, campaign, min_windows=int(min_windows), bootstrap_samples=bootstrap_samples,
    )
    interim = RPT._campaign_eval(  # noqa: SLF001
        conn, campaign, min_windows=int(interim_min_windows), bootstrap_samples=bootstrap_samples,
    )
    kill = hard.get("recommendation") == "kill_campaign"
    return {
        "campaign": campaign,
        "hard_eval": hard,
        "interim_eval": interim,
        "interim_ready": bool(interim.get("eval_ready")),
        "kill_recommended": bool(kill),
        "action": "pause_maker" if kill else "keep_running",
        "reason": hard.get("reason"),
    }


def apply_pause(decision: dict, *, pidfile: str | None = None,
                marker_path: str | None = None) -> bool:
    if not decision.get("kill_recommended"):
        return False
    if marker_path:
        with open(marker_path, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "decision": decision}, fh, sort_keys=True)
            fh.write("\n")
    pid = _read_pid(pidfile)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return True


def _read_pid(pidfile: str | None) -> int | None:
    if not pidfile:
        return None
    try:
        raw = open(pidfile, encoding="utf-8").read().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _ro(path: str):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _print_text(decision: dict) -> None:
    hard = decision["hard_eval"]
    interim = decision["interim_eval"]
    print(
        "crypto5m campaign guard: "
        f"campaign={decision['campaign']} action={decision['action']} "
        f"hard={hard.get('n_settled')}/{hard.get('sample_gate', {}).get('min_windows')} "
        f"hard_status={hard.get('status')} hard_ci_high={hard.get('ci_high')} "
        f"interim={interim.get('n_settled')}/{interim.get('sample_gate', {}).get('min_windows')} "
        f"interim_status={interim.get('status')}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper-db", default=os.path.join(ROOT, "data", "crypto5m", "maker_v0.sqlite"))
    ap.add_argument("--campaign", default="maker-regime-v1")
    ap.add_argument("--min-windows", type=int, default=200)
    ap.add_argument("--interim-min-windows", type=int, default=60)
    ap.add_argument("--bootstrap-samples", type=int, default=0)
    ap.add_argument("--action", choices=("alert", "pause"), default="alert")
    ap.add_argument("--pid", default="")
    ap.add_argument("--marker", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = _ro(args.paper_db)
    try:
        decision = evaluate_campaign_guard(
            conn,
            args.campaign,
            min_windows=args.min_windows,
            interim_min_windows=args.interim_min_windows,
            bootstrap_samples=(args.bootstrap_samples or None),
        )
    finally:
        conn.close()
    if args.action == "pause":
        apply_pause(decision, pidfile=(args.pid or None), marker_path=(args.marker or None))
    if args.json:
        print(json.dumps(decision, indent=2, sort_keys=True))
    else:
        _print_text(decision)
    return 2 if decision.get("kill_recommended") else 0


if __name__ == "__main__":
    raise SystemExit(main())
