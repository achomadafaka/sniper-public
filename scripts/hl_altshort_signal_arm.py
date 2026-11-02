#!/usr/bin/env python3
"""Read-only reporter for the standalone HL exhaustion forward-paper arm."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
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
from sniper.util.clock import real_utc_now  # noqa: E402


DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"
HOUR_MS = 3_600_000


def _stats(values: list[float]) -> dict:
    return {
        "n": len(values), "mean_bps": statistics.fmean(values) if values else None,
        "median_bps": statistics.median(values) if values else None,
        "win_rate": sum(value > 0 for value in values) / len(values) if values else None,
        "sum_bps": sum(values),
    }


def _day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def _week(ts_ms: int) -> str:
    value = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isocalendar()
    return f"{value.year}-W{value.week:02d}"


def _regime_replication(rows: list[dict]) -> dict:
    by_episode: dict[int, list[float]] = {}
    for row in rows:
        regime = json.loads(row["regime_json"] or "{}")
        episode_id = regime.get("episode_id")
        benchmark = json.loads(row["benchmark_exit_json"] or "{}")
        if episode_id is None or not benchmark.get("valid"):
            continue
        basket = benchmark.get("basket_net_bps")
        if basket is None or row.get("net_bps") is None:
            continue
        by_episode.setdefault(int(episode_id), []).append(
            float(row["net_bps"]) - float(basket)
        )
    episodes = [
        {
            "episode_id": episode_id,
            "closed": len(values),
            "mean_residual_bps": statistics.fmean(values),
        }
        for episode_id, values in sorted(by_episode.items())
    ]
    all_values = [value for values in by_episode.values() for value in values]
    leave_one_out = []
    for episode_id in sorted(by_episode):
        remaining = [
            value for other_id, values in by_episode.items()
            if other_id != episode_id for value in values
        ]
        if remaining:
            leave_one_out.append({
                "left_out_episode_id": episode_id,
                "mean_residual_bps": statistics.fmean(remaining),
            })
    return {
        "episode_count": len(episodes),
        "min_episode_closes": min((row["closed"] for row in episodes), default=0),
        "min_episode_residual_bps": min(
            (row["mean_residual_bps"] for row in episodes), default=None
        ),
        "leave_one_episode_out_residual_bps": min(
            (row["mean_residual_bps"] for row in leave_one_out), default=None
        ),
        "episodes": episodes,
        "leave_one_out": leave_one_out,
        "closed_with_episode": len(all_values),
    }


def _open_funding_bps(conn, *, coin: str, entry_ts_ms: int,
                      now_ms: int) -> float | None:
    current_bucket = int(now_ms) // HOUR_MS * HOUR_MS
    expected = max(0, (current_bucket - int(entry_ts_ms)) // HOUR_MS)
    if expected == 0:
        return 0.0
    rows = conn.execute(
        "SELECT funding_rate FROM hl_altshort_regime_marks "
        "WHERE coin=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
        (coin, int(entry_ts_ms), current_bucket),
    ).fetchall()
    rates = [float(row["funding_rate"]) for row in rows if row["funding_rate"] is not None]
    if len(rates) < expected:
        return None
    return sum(rates) * 10_000.0


def build_signal_report(conn, *, arm_id: str, now_ms: int,
                        max_mark_age_ms: int = 300_000) -> dict:
    arm_row = conn.execute("SELECT * FROM hl_altshort_arms WHERE arm_id=?", (arm_id,)).fetchone()
    if arm_row is None:
        raise ValueError(f"unknown arm: {arm_id}")
    arm = dict(arm_row)
    policy = json.loads(arm["policy_json"])
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM hl_altshort_signal_observations WHERE arm_id=? "
        "ORDER BY decision_ts_ms, coin", (arm_id,)
    )]
    closed = [row for row in rows if row["status"] == "closed"]
    open_rows = [row for row in rows if row["status"] == "open"]
    valid_closed = [
        row for row in closed
        if row["provenance_status"] == "valid" and row["net_bps"] is not None
    ]
    closed_values = [float(row["net_bps"]) for row in valid_closed]
    stale = [
        row for row in open_rows
        if row["latest_mark_ts_ms"] is None
        or now_ms - int(row["latest_mark_ts_ms"]) > max_mark_age_ms
    ]
    stale_ids = {int(row["id"]) for row in stale}
    open_net = {}
    open_gross = {}
    open_funding = {}
    funding_missing = []
    for row in open_rows:
        row_id = int(row["id"])
        if row_id in stale_ids or not row["entry_mark"] or not row["latest_mark"]:
            continue
        gross = (float(row["entry_mark"]) - float(row["latest_mark"])) \
            / float(row["entry_mark"]) * 10_000.0
        funding = _open_funding_bps(
            conn, coin=row["coin"], entry_ts_ms=int(row["entry_ts_ms"]), now_ms=now_ms
        )
        if funding is None:
            funding_missing.append(row)
            continue
        open_gross[row_id] = gross
        open_funding[row_id] = funding
        open_net[row_id] = gross + funding - float(policy.get("cost_bps", 39.0))
    open_mtm = sum(open_net.values()) if not stale and not funding_missing else None

    residual_rows = []
    for row in valid_closed:
        benchmark = json.loads(row["benchmark_exit_json"] or "{}")
        if benchmark.get("valid") and benchmark.get("basket_net_bps") is not None:
            residual_rows.append({
                "day": _day(int(row["entry_ts_ms"])),
                "residual_bps": float(row["net_bps"]) - float(benchmark["basket_net_bps"]),
            })
    inference = block_bootstrap_mean(
        residual_rows, cluster_key="day", replications=2000, seed=17
    )
    residual_values = [row["residual_bps"] for row in residual_rows]
    regime_replication = _regime_replication(valid_closed)

    contribution_rows = [
        {"coin": row["coin"], "week": _week(int(row["entry_ts_ms"])),
         "net_bps": float(row["net_bps"])} for row in valid_closed
    ]
    contribution_rows.extend(
        {"coin": row["coin"], "week": _week(int(row["entry_ts_ms"])),
         "net_bps": open_net[int(row["id"])]}
        for row in open_rows if int(row["id"]) in open_net
    )
    concentration = concentration_report(contribution_rows)

    portfolio_rows = [
        {"id": row["id"], "coin": row["coin"], "status": "closed",
         "entry_ts_ms": row["entry_ts_ms"], "exit_ts_ms": row["exit_ts_ms"],
         "gross_pnl_bps": row["gross_bps"], "funding_bps": -float(row["funding_bps"] or 0)}
        for row in valid_closed
    ]
    portfolio_rows.extend(
        {"id": row["id"], "coin": row["coin"], "status": "open",
         "entry_ts_ms": row["entry_ts_ms"],
         "unrealized_bps": open_gross[int(row["id"])],
         "funding_bps": -open_funding[int(row["id"])],
         "mark_ts_ms": row["latest_mark_ts_ms"]}
        for row in open_rows if int(row["id"]) in open_net
    )
    portfolio = fixed_slot_portfolio(
        portfolio_rows, slots=int(policy.get("slots", 8)),
        initial_equity=float(policy.get("initial_equity", 1000)),
        costs=CostModel(round_trip_bps=float(policy.get("cost_bps", 39.0))),
        now_ms=now_ms, max_mark_age_ms=max_mark_age_ms,
    ) if portfolio_rows else {"valid": True, "total_return_bps": 0.0, "open_mtm": 0.0}
    if funding_missing:
        portfolio = {"valid": False, "reason": "missing_open_funding"}

    entry_days = len({_day(int(row["entry_ts_ms"])) for row in rows if row["entry_ts_ms"]})
    invalid_n = sum(row["provenance_status"] != "valid" for row in rows)
    incomplete = []
    if len(valid_closed) < int(policy.get("min_closed", 30)) or entry_days < int(
        policy.get("min_entry_days", 20)
    ):
        incomplete.append("forward_sample")
    if stale:
        incomplete.append("stale_open_marks")
    if funding_missing:
        incomplete.append("missing_open_funding")
    if regime_replication["episode_count"] < int(policy.get("min_regime_episodes", 2)):
        incomplete.append("regime_episodes")
    if regime_replication["min_episode_closes"] < int(policy.get("min_episode_closes", 5)):
        incomplete.append("episode_sample")
    if incomplete:
        verdict = {"status": "paper_continue", "blocked": [], "failed": [],
                   "incomplete": incomplete}
    else:
        regime_coverages = [float(json.loads(row["regime_json"]).get("coverage") or 0) for row in rows]
        verdict = altshort_verdict({
            "provenance_valid": invalid_n == 0,
            "causal_features": all(json.loads(row["regime_json"]).get("label") == "bearish"
                                   for row in rows if row["gated_in"]),
            "next_tradeable_entry": True,
            "residual_ci_low_bps": inference["ci95"][0] or 0,
            "fixed_slot_base_bps": portfolio.get("total_return_bps") or 0,
            "fixed_slot_stress_bps": portfolio.get("total_return_bps") or 0,
            "total_book_bps": portfolio.get("total_return_bps") or 0,
            "leave_top_coin_bps": concentration["leave_top_coin_bps"],
            "leave_top_week_bps": concentration["leave_top_week_bps"],
            "top_coin_share": concentration["top_coin_share"],
            "top_week_share": concentration["top_week_share"],
            "coverage": statistics.fmean(regime_coverages) if regime_coverages else 0,
            "exit_rule_valid": True, "day_clusters": inference["clusters"],
            "stale_open_marks": len(stale),
            "regime_episode_count": regime_replication["episode_count"],
            "min_episode_closes": regime_replication["min_episode_closes"],
            "min_episode_residual_bps": regime_replication["min_episode_residual_bps"],
            "leave_one_episode_out_residual_bps":
                regime_replication["leave_one_episode_out_residual_bps"],
        }, max_top_coin_share=float(policy.get("max_top_coin_share", 0.35)),
           max_top_week_share=float(policy.get("max_top_week_share", 0.5)),
           min_coverage=float(policy.get("min_coverage", 0.8)),
           min_day_clusters=int(policy.get("min_entry_days", 20)),
           min_regime_episodes=int(policy.get("min_regime_episodes", 2)),
           min_episode_closes=int(policy.get("min_episode_closes", 5)))
    status_counts = {}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    return {
        "arm": {
            "arm_id": arm["arm_id"], "activated_ts_ms": arm["activated_ts_ms"],
            "activated_utc": datetime.fromtimestamp(
                arm["activated_ts_ms"] / 1000, tz=timezone.utc
            ).isoformat(),
            "code_sha": arm["code_sha"], "policy_hash": arm["policy_hash"],
            "hypothesis": "standalone_exhaustion", "taxonomy_version": arm["taxonomy_version"],
            "benchmark_version": arm["benchmark_version"], "regime_version": arm["regime_version"],
        },
        "status_counts": status_counts,
        "closed": _stats(closed_values),
        "open": {"n": len(open_rows), "fresh_n": len(open_net), "stale_n": len(stale),
                 "funding_complete_n": len(open_funding),
                 "funding_missing_n": len(funding_missing), "mtm_bps": open_mtm},
        "beta_residual": {**_stats(residual_values), "day_block_ci95": inference["ci95"],
                          "days": inference["clusters"]},
        "regime_replication": regime_replication,
        "concentration": concentration,
        "portfolio": portfolio,
        "provenance": {"valid_n": len(rows) - invalid_n, "invalid_n": invalid_n},
        "forward_sample": {"closed": len(valid_closed), "days": entry_days,
                           "observations": len(rows)},
        "verdict": verdict,
    }


def render_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Standalone HL exhaustion paper-arm report")
    parser.add_argument("--db-path", default=DEFAULT_DB)
    parser.add_argument("--arm-id", required=True)
    parser.add_argument("--now-ms", type=int, default=None)
    parser.add_argument("--max-mark-age-ms", type=int, default=300_000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(f"file:{Path(args.db_path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    report = build_signal_report(
        conn, arm_id=args.arm_id,
        now_ms=args.now_ms if args.now_ms is not None else int(real_utc_now() * 1000),
        max_mark_age_ms=args.max_mark_age_ms,
    )
    if args.json:
        print(render_json(report), end="")
    else:
        print(f"arm={args.arm_id} closed={report['closed']['n']} open={report['open']['n']} "
              f"verdict={report['verdict']['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
