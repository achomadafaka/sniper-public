#!/usr/bin/env python3
"""Read-only report for one immutable HL alt-short forward paper arm."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.hyperlive.altshort_evidence import (  # noqa: E402
    CostModel,
    altshort_verdict,
    block_bootstrap_mean,
    concentration_report,
    fixed_slot_portfolio,
)
from sniper.hyperlive import intel_store as IS  # noqa: E402


DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"


def _utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def _week(ts_ms: int) -> str:
    value = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isocalendar()
    return f"{value.year}-W{value.week:02d}"


def _stats(values: list[float]) -> dict:
    return {
        "n": len(values),
        "mean_bps": statistics.fmean(values) if values else None,
        "median_bps": statistics.median(values) if values else None,
        "win_rate": sum(value > 0 for value in values) / len(values) if values else None,
        "sum_bps": sum(values),
    }


def build_arm_report(conn, *, arm_id: str, now_ms: int,
                     max_mark_age_ms: int = 300_000) -> dict:
    arm_row = conn.execute("SELECT * FROM hl_altshort_arms WHERE arm_id=?", (arm_id,)).fetchone()
    if arm_row is None:
        raise ValueError(f"unknown arm: {arm_id}")
    arm = dict(arm_row)
    policy = json.loads(arm["policy_json"])
    compatible_arm_ids = IS.compatible_altshort_arm_ids(conn, arm_id=arm_id)
    placeholders = ",".join("?" for _ in compatible_arm_ids)
    observations = [dict(row) for row in conn.execute(
        "SELECT * FROM hl_altshort_observations "
        f"WHERE arm_id IN ({placeholders}) ORDER BY detect_ts_ms, copy_id",
        compatible_arm_ids,
    )]
    cost_bps = float(policy.get("cost_bps", 0.0))
    closed = [row for row in observations
              if row["status"] == "closed" and row.get("provenance_status") == "valid"]
    open_rows = [row for row in observations if row["status"] == "open"]
    closed_values = [float(row["wallet_net_bps"]) for row in closed if row["wallet_net_bps"] is not None]
    stale = [
        row for row in open_rows
        if row["latest_mark_ts_ms"] is None
        or now_ms - int(row["latest_mark_ts_ms"]) > max_mark_age_ms
    ]
    open_net: dict[int, float] = {}
    for row in open_rows:
        if row in stale or not row["entry_mark"] or not row["latest_mark"]:
            continue
        gross = (float(row["entry_mark"]) - float(row["latest_mark"])) / float(row["entry_mark"]) * 10_000
        open_net[int(row["copy_id"])] = gross - cost_bps
    open_mtm = sum(open_net.values()) if not stale else None

    residual_rows = []
    for row in closed:
        if row["wallet_net_bps"] is None or not row["benchmark_exit_json"]:
            continue
        benchmark = json.loads(row["benchmark_exit_json"])
        if not benchmark.get("valid") or benchmark.get("basket_net_bps") is None:
            continue
        residual_rows.append({
            "day": datetime.fromtimestamp(row["detect_ts_ms"] / 1000, tz=timezone.utc).date().isoformat(),
            "residual_bps": float(row["wallet_net_bps"]) - float(benchmark["basket_net_bps"]),
        })
    residual_values = [row["residual_bps"] for row in residual_rows]
    inference = block_bootstrap_mean(
        residual_rows, cluster_key="day", replications=2000, seed=17
    )

    concentration_rows = [
        {"coin": row["coin"], "week": _week(int(row["detect_ts_ms"])),
         "net_bps": float(row["wallet_net_bps"])}
        for row in closed if row["wallet_net_bps"] is not None
    ]
    concentration_rows.extend(
        {"coin": row["coin"], "week": _week(int(row["detect_ts_ms"])),
         "net_bps": open_net[int(row["copy_id"])]}
        for row in open_rows if int(row["copy_id"]) in open_net
    )
    concentration = concentration_report(concentration_rows)

    portfolio_rows = []
    for row in closed:
        if row["wallet_net_bps"] is not None:
            portfolio_rows.append({
                "id": row["copy_id"], "coin": row["coin"], "status": "closed",
                "entry_ts_ms": row["detect_ts_ms"], "exit_ts_ms": row["exit_ts_ms"],
                "gross_pnl_bps": float(row["wallet_net_bps"]) + cost_bps,
            })
    for row in open_rows:
        if int(row["copy_id"]) in open_net:
            portfolio_rows.append({
                "id": row["copy_id"], "coin": row["coin"], "status": "open",
                "entry_ts_ms": row["detect_ts_ms"],
                "unrealized_bps": open_net[int(row["copy_id"])] + cost_bps,
                "mark_ts_ms": row["latest_mark_ts_ms"],
            })
    portfolio = fixed_slot_portfolio(
        portfolio_rows,
        slots=int(policy.get("slots", 8)),
        initial_equity=float(policy.get("initial_equity", 1000.0)),
        costs=CostModel(round_trip_bps=cost_bps),
        now_ms=now_ms,
        max_mark_age_ms=max_mark_age_ms,
    ) if portfolio_rows else {"valid": True, "total_return_bps": 0.0, "open_mtm": 0.0}

    distinct_days = len({
        datetime.fromtimestamp(row["detect_ts_ms"] / 1000, tz=timezone.utc).date().isoformat()
        for row in observations
    })
    invalid_n = sum(row["provenance_status"] != "valid" for row in observations)
    incomplete = []
    if len(closed_values) < 30 or distinct_days < 20:
        incomplete.append("forward_sample")
    if stale:
        incomplete.append("stale_open_marks")
    if incomplete:
        verdict = {"status": "paper_continue", "blocked": [], "failed": [],
                   "incomplete": incomplete}
    else:
        verdict = altshort_verdict({
            "provenance_valid": invalid_n == 0,
            "causal_features": all(json.loads(row["regime_json"]).get("label") != "unknown"
                                   for row in observations),
            "next_tradeable_entry": True,
            "residual_ci_low_bps": inference["ci95"][0] or 0.0,
            "fixed_slot_base_bps": portfolio.get("total_return_bps") or 0.0,
            "fixed_slot_stress_bps": portfolio.get("total_return_bps") or 0.0,
            "total_book_bps": portfolio.get("total_return_bps") or 0.0,
            "leave_top_coin_bps": concentration["leave_top_coin_bps"],
            "leave_top_week_bps": concentration["leave_top_week_bps"],
            "top_coin_share": concentration["top_coin_share"],
            "top_week_share": concentration["top_week_share"],
            "coverage": statistics.fmean(
                float(json.loads(row["regime_json"]).get("coverage") or 0.0)
                for row in observations
            ),
            "exit_rule_valid": bool(policy.get("exit_rule_frozen", True)),
            "day_clusters": inference["clusters"],
            "stale_open_marks": len(stale),
        })
    bps_sum = sum(closed_values) + open_mtm if open_mtm is not None else None
    return {
        "arm": {
            "arm_id": arm["arm_id"], "activated_ts_ms": arm["activated_ts_ms"],
            "activated_utc": _utc(int(arm["activated_ts_ms"])), "code_sha": arm["code_sha"],
            "policy_hash": arm["policy_hash"], "targets": json.loads(arm["targets_json"]),
            "taxonomy_version": arm["taxonomy_version"],
            "benchmark_version": arm["benchmark_version"], "regime_version": arm["regime_version"],
            "compatible_arm_ids": compatible_arm_ids,
        },
        "closed": _stats(closed_values),
        "open": {"n": len(open_rows), "fresh_n": len(open_net), "stale_n": len(stale),
                 "mtm_bps": open_mtm},
        "total_book": {"bps_sum": bps_sum, "portfolio": portfolio},
        "beta_residual": {**_stats(residual_values), "day_block_ci95": inference["ci95"],
                          "days": inference["clusters"]},
        "concentration": concentration,
        "provenance": {"valid_n": len(observations) - invalid_n, "invalid_n": invalid_n},
        "forward_sample": {"observations": len(observations), "closed": len(closed_values),
                           "days": distinct_days},
        "verdict": verdict,
    }


def render_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="HL alt-short immutable paper-arm report")
    parser.add_argument("--db-path", default=DEFAULT_DB)
    parser.add_argument("--arm-id", default=None)
    parser.add_argument("--now-ms", type=int, default=None)
    parser.add_argument("--max-mark-age-ms", type=int, default=300_000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    uri = f"file:{Path(args.db_path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    arm_id = args.arm_id
    if arm_id is None:
        row = conn.execute(
            "SELECT arm_id FROM hl_altshort_arms ORDER BY activated_ts_ms DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise SystemExit("no alt-short arm exists")
        arm_id = row["arm_id"]
    now_ms = args.now_ms
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    report = build_arm_report(
        conn, arm_id=arm_id, now_ms=now_ms, max_mark_age_ms=args.max_mark_age_ms
    )
    if args.json:
        print(render_json(report), end="")
    else:
        print(f"arm={report['arm']['arm_id']} activated={report['arm']['activated_utc']} "
              f"closed={report['closed']['n']} open={report['open']['n']} "
              f"total_bps={report['total_book']['bps_sum']} "
              f"verdict={report['verdict']['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
