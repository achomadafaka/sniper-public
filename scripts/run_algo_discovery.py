#!/usr/bin/env python3
"""Algorithm Discovery workflow CLI.

Drives the end-to-end chain on one wallet:
  fetch → protocol → dossier → wallet_rule → OOS backtest → decision gate
  → (optional, --enroll) shadow_copy paper-mirror enrollment.

Each stage writes a JSON artifact into a per-run directory; re-runs resume via
the manifest. A reject / insufficient_data verdict is a SUCCESSFUL run (exit 0)
— it's a valid analytical outcome, not an error. Stage failures exit 1.

Usage:
    python3 scripts/run_algo_discovery.py --wallet 0xABC --from-fixture wd.json
    python3 scripts/run_algo_discovery.py --wallet 0xABC --from-fixture wd.json --enroll
    python3 scripts/run_algo_discovery.py --wallet 0xABC --from-fixture wd.json --force

v1 is fixture-only (live Polymarket fetch is a named gap — region-restricted).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.analysis.algo_discovery.pipeline import run_discovery  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the end-to-end algorithm-discovery workflow on a wallet")
    ap.add_argument("--wallet", required=True, help="0x wallet address")
    ap.add_argument("--from-fixture", type=Path, default=None,
                    help="WalletData fixture JSON; omit and pass --live to capture fresh from the API")
    ap.add_argument("--live", action="store_true",
                    help="capture a fresh snapshot from the live Polymarket API (record_fetch_snapshot)")
    ap.add_argument("--copy-latency-grid", action="store_true",
                    help="live: also capture the copy-latency price grid (slower; activates the copyable verdict)")
    ap.add_argument("--run-root", type=Path, default=ROOT / "runs" / "algo_discovery",
                    help="parent dir for the per-run artifact directory")
    ap.add_argument("--run-backtest", action="store_true",
                    help="also run the OOS backtest as a DIAGNOSTIC artifact (v2 does not gate on it)")
    ap.add_argument("--force", action="store_true", help="ignore cached artifacts; re-run every stage")
    ap.add_argument("--n-iter", type=int, default=None, help="protocol bootstrap iterations (smaller = faster)")
    ap.add_argument("--cse-context", type=Path, default=None,
                    help="prebuilt CSE MarketContext JSON (scripts/build_cse_pit_index.py); "
                         "when set, classify→route uses the Counterfactual Selection Engine")
    args = ap.parse_args(argv)

    fixture = None
    if args.from_fixture is not None:
        fixture = json.loads(args.from_fixture.read_text())

    try:
        res = run_discovery(
            args.wallet,
            run_root=args.run_root,
            from_fixture=fixture,
            live=args.live,
            capture_copy_latency=args.copy_latency_grid,
            force=args.force,
            n_iter=args.n_iter,
            run_backtest=args.run_backtest,
            cse_context_path=args.cse_context,
        )
    except NotImplementedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    classification = res.get("classification") or {}
    routing = res.get("routing") or {}
    print(f"route: {res.get('route')}")
    print(f"classification: {classification.get('label')} (confidence {classification.get('confidence')})")
    if routing.get("reason"):
        print(f"reason: {routing['reason']}")
    enr = routing.get("enrollment")
    if enr:
        status = f" [{enr['status']}]" if enr.get("status") else ""
        print(f"enrollment: {enr.get('strategy_type')}{status}")
    print(f"run_dir: {res['run_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
