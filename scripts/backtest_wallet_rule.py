#!/usr/bin/env python3
"""Run a wallet_rule_v1 against a wallet's historical positions (Path A step 2).

Reads a rule JSON (from ``scripts/extract_wallet_rule.py``) and a
WalletData fixture, runs the backtest_harness with explicit in-sample /
out-of-sample split + slippage, and writes a structured result JSON.

Usage:
    python3 scripts/backtest_wallet_rule.py \\
        --rule /tmp/swissmiss_rule.json \\
        --wallet-data /tmp/v2_batch/0xdbade.../tagged.json \\
        --oos-ratio 0.25 \\
        --slippage-pct 0.005 \\
        --out /tmp/swissmiss_backtest.json

No live API. No DB writes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sniper.analysis.backtest_harness import backtest_rule_on_wallet  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Backtest a wallet_rule_v1 on a wallet's historical positions."
    )
    p.add_argument("--rule", required=True, type=Path, help="wallet_rule_v1 JSON")
    p.add_argument("--wallet-data", required=True, type=Path, help="WalletData fixture JSON")
    p.add_argument("--oos-ratio", type=float, default=0.25,
                   help="Fraction of newest positions held as OOS (0 < r < 1). Default 0.25.")
    p.add_argument("--slippage-pct", type=float, default=0.005,
                   help="Flat slippage added to entry price. Default 0.5%%.")
    p.add_argument("--include-per-position", action="store_true",
                   help="Emit per-position result rows in the output JSON.")
    p.add_argument("--out", type=Path, required=True, help="Write backtest result JSON here.")
    args = p.parse_args(argv)

    if not (0.0 < args.oos_ratio < 1.0):
        print(f"# error: --oos-ratio must be in (0, 1); got {args.oos_ratio}", file=sys.stderr)
        return 2
    if args.slippage_pct < 0:
        print("# error: --slippage-pct must be >= 0", file=sys.stderr)
        return 2
    if not args.rule.exists():
        print(f"# error: rule not found: {args.rule}", file=sys.stderr)
        return 2
    if not args.wallet_data.exists():
        print(f"# error: wallet-data not found: {args.wallet_data}", file=sys.stderr)
        return 2

    rule = json.loads(args.rule.read_text(encoding="utf-8"))
    wallet_data = json.loads(args.wallet_data.read_text(encoding="utf-8"))

    result = backtest_rule_on_wallet(
        rule,
        wallet_data,
        oos_split_ratio=args.oos_ratio,
        slippage_pct=args.slippage_pct,
        include_per_position=args.include_per_position,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"# wrote {args.out}")
    print(f"# oos_match_count={result['oos_match_count']} "
          f"sim_pnl=${result['oos_simulated_pnl_usdc']:.2f} "
          f"wallet_pnl_scaled=${result['oos_actual_pnl_usdc_on_same_positions']:.2f} "
          f"delta=${result['pnl_vs_wallet_delta_usdc']:.2f} "
          f"sim_wr={result['oos_simulated_win_rate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
