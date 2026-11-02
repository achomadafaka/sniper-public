#!/usr/bin/env python3
"""Read-only crypto5m vol-floor shadow report.

Recomputes fair values from captured Binance prices under candidate vol floors and compares:
- Brier vs resolved outcomes and market midpoint
- claimed maker fill edge vs realized settlement PnL

No writes, no live APIs, no engine changes.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sniper.algos.polymarket_crypto5m import vol_floor_shadow as VFS  # noqa: E402


def _ro(path: str):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_floors(raw: str) -> tuple[float, ...]:
    return tuple(float(x.strip()) for x in raw.split(",") if x.strip())


def _parse_weights(raw: str) -> tuple[float, ...]:
    return tuple(float(x.strip()) for x in raw.split(",") if x.strip())


def _fmt(value, places=4):
    return "n/a" if value is None else f"{value:.{places}f}"


def _print_text(report: dict) -> None:
    print("crypto5m vol-floor shadow")
    print(f"windows={report['windows']} samples={report['samples']} step={report['sample_step_s']}s")
    if report.get("before_window_start_ts") is not None:
        print(f"before_window_start_ts={report['before_window_start_ts']} (exclusive)")
    vs = report["vol_summary"]
    print(
        "raw_vol p10={p10} p50={p50} p90={p90}".format(
            p10=_fmt(vs.get("p10"), 8), p50=_fmt(vs.get("p50"), 8), p90=_fmt(vs.get("p90"), 8),
        )
    )
    print("")
    print("floor        bind_rate  brier     fill_edge  realized/share  fills")
    market = report.get("brier_market_mid", {})
    print(f"market_mid   n/a       {_fmt(market.get('brier'), 6):>8}  n/a        n/a             {market.get('n', 0)}")
    for key in report["floors"]:
        bind = report["vol_summary"]["floors"][key]
        br = report["brier_by_floor"].get(key, {})
        fills = report["fill_edge_by_floor"].get(key, {})
        print(
            f"{key:<12} {bind.get('bind_rate', 0):>8.2%} "
            f"{_fmt(br.get('brier'), 6):>8} "
            f"{_fmt(fills.get('mean_claimed_edge'), 6):>10} "
            f"{_fmt(fills.get('mean_realized_pnl_per_share'), 6):>15} "
            f"{fills.get('n_fills', 0):>6}"
        )
    if report.get("brier_by_blend"):
        print("")
        print("blend w@floor  brier     fill_edge  realized/share  fills")
        for key in sorted(report["brier_by_blend"]):
            br = report["brier_by_blend"][key]
            fills = report["fill_edge_by_blend"].get(key, {})
            print(
                f"{key:<13} {_fmt(br.get('brier'), 6):>8} "
                f"{_fmt(fills.get('mean_claimed_edge'), 6):>10} "
                f"{_fmt(fills.get('mean_realized_pnl_per_share'), 6):>15} "
                f"{fills.get('n_fills', 0):>6}"
            )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture-db", default=os.path.join(ROOT, "data", "crypto5m_capture.sqlite"))
    ap.add_argument("--maker-db", default=os.path.join(ROOT, "data", "crypto5m", "maker_v0.sqlite"))
    ap.add_argument("--campaign", default="maker-regime-v1")
    ap.add_argument("--floors", default="1e-5,2e-5,5e-5,1e-4")
    ap.add_argument("--blend-weights", default="0.25,0.5,0.75")
    ap.add_argument("--sample-step-s", type=int, default=5)
    ap.add_argument("--limit-windows", type=int, default=0)
    ap.add_argument(
        "--before-window-start-ts",
        type=int,
        default=0,
        help="Exclusive engine-time window_start_ts cutoff; use it to avoid tournament forward windows.",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    capture = _ro(args.capture_db)
    maker = _ro(args.maker_db)
    try:
        report = VFS.build_vol_floor_shadow_report(
            capture,
            maker,
            campaign=args.campaign,
            floors=_parse_floors(args.floors),
            blend_weights=_parse_weights(args.blend_weights),
            sample_step_s=args.sample_step_s,
            limit_windows=(args.limit_windows or None),
            before_window_start_ts=(args.before_window_start_ts or None),
        )
    finally:
        capture.close()
        maker.close()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
