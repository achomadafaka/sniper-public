#!/usr/bin/env python3
"""DB-only standalone HL exhaustion paper scanner. No network, keys, or orders."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.hyperlive import intel_store as IS  # noqa: E402
from sniper.hyperlive.altshort_signal_paper import (  # noqa: E402
    HOUR_MS,
    assign_regime_episodes,
    causal_basket_regime,
    evaluate_sampled_exit,
    matched_forward_basket,
    runup_crossing,
)
from sniper.hyperlive import funding as FUND  # noqa: E402
from sniper.hyperlive.altshort_universe import strategy_bucket  # noqa: E402
from sniper.util.clock import real_utc_now  # noqa: E402


DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"


def _policy(arm: dict) -> dict:
    return json.loads(arm["policy_json"])


def _git_state(*, root: Path | None = None, pinned_code_sha: str | None = None,
               code_paths: list[str] | None = None) -> tuple[str, bool]:
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()
    status_command = ["git", "status", "--porcelain", "--untracked-files=no"]
    if code_paths:
        status_command.extend(["--", *code_paths])
    dirty = bool(subprocess.run(
        status_command, cwd=root,
        check=True, text=True, capture_output=True,
    ).stdout.strip())
    code_sha = head
    if pinned_code_sha and code_paths:
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{pinned_code_sha}^{{commit}}"], cwd=root,
            capture_output=True,
        ).returncode == 0
        unchanged = exists and subprocess.run(
            ["git", "diff", "--quiet", pinned_code_sha, "--", *code_paths], cwd=root,
        ).returncode == 0
        if unchanged:
            code_sha = pinned_code_sha
    return code_sha, dirty


def _load_arm(conn, manifest_path: Path, *, now_ms: int) -> dict:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    code_sha, dirty = _git_state(
        pinned_code_sha=manifest.get("code_sha"),
        code_paths=[
            "scripts/run_hl_altshort_signal_paper.py",
            "sniper/hyperlive/altshort_signal_paper.py",
            "sniper/hyperlive/altshort_universe.py",
            "sniper/hyperlive/intel_store.py",
        ],
    )
    if manifest.get("code_sha") != code_sha:
        raise ValueError("code_sha_mismatch")
    if dirty:
        raise ValueError("dirty_tree")
    if manifest.get("hypothesis") != "standalone_exhaustion":
        raise ValueError("wrong_hypothesis")
    if manifest.get("targets") != ["standalone-exhaustion"]:
        raise ValueError("wrong_target_identity")
    existing = IS.get_altshort_arm(conn, manifest["arm_id"])
    epoch = int(existing["activated_ts_ms"]) if existing else int(now_ms)
    created = int(existing["created_ts_ms"]) if existing else int(now_ms)
    return IS.create_altshort_arm(
        conn, manifest, activated_ts_ms=epoch, code_sha=code_sha, created_ts_ms=created
    )


def _bucket_rows(conn, bucket_ms: int) -> dict[str, dict]:
    return {
        row["coin"]: dict(row)
        for row in conn.execute(
            "SELECT * FROM hl_altshort_regime_marks WHERE ts_ms=?", (int(bucket_ms),)
        )
    }


def _funding_for_interval(conn, coins, *, entry_ts_ms: int,
                          exit_ts_ms: int) -> tuple[dict[str, float], float]:
    out = {}
    for coin in coins:
        rows = conn.execute(
            "SELECT ts_ms,funding_rate FROM hl_altshort_regime_marks "
            "WHERE coin=? AND ts_ms>? AND ts_ms<=? ORDER BY ts_ms",
            (coin, int(entry_ts_ms), int(exit_ts_ms)),
        ).fetchall()
        funding = FUND.funding_pnl(
            [{"time": row["ts_ms"], "rate": row["funding_rate"]} for row in rows
             if row["funding_rate"] is not None],
            side="short", entry_ts_ms=entry_ts_ms, exit_ts_ms=exit_ts_ms,
        )
        if funding.status in FUND.COMPLETE_STATUSES:
            out[coin] = float(funding.value_bps or 0.0)
    coverage = len(out) / len(set(coins)) if coins else 0.0
    return out, coverage


def _cycle(conn, *, arm: dict, now_ms: int) -> dict:
    policy = _policy(arm)
    current_bucket = int(now_ms) // HOUR_MS * HOUR_MS
    last_closed = current_bucket - HOUR_MS
    current = _bucket_rows(conn, current_bucket)
    counts = {"decisions": 0, "opened": 0, "closed": 0, "rejected": 0}

    open_rows = IS.list_altshort_signals(conn, arm_id=arm["arm_id"], status="open")
    available = max(0, int(policy.get("slots", 8)) - len(open_rows))
    for row in IS.list_altshort_signals(conn, arm_id=arm["arm_id"], status="pending"):
        next_bucket = (int(row["decision_ts_ms"]) // HOUR_MS + 1) * HOUR_MS
        bar = current.get(row["coin"])
        if current_bucket == next_bucket and bar is not None and available > 0:
            eligible_benchmark = set(json.loads(row["benchmark_entry_json"]))
            benchmark = {
                coin: float(value["open_mark"])
                for coin, value in sorted(current.items())
                if coin in eligible_benchmark
                and value["open_mark"] is not None and float(value["open_mark"]) > 0
            }
            IS.open_altshort_signal(
                conn, signal_id=row["id"], entry_ts_ms=current_bucket,
                entry_mark=float(bar["open_mark"]), benchmark_entry=benchmark,
            )
            available -= 1
            counts["opened"] += 1
        elif current_bucket > next_bucket:
            IS.reject_altshort_signal(
                conn, signal_id=row["id"], reason="missed_next_hour_entry", ts_ms=now_ms
            )
            counts["rejected"] += 1
        elif current_bucket == next_bucket and bar is not None and available <= 0:
            IS.reject_altshort_signal(
                conn, signal_id=row["id"], reason="slot_capacity", ts_ms=now_ms
            )
            counts["rejected"] += 1

    for row in IS.list_altshort_signals(conn, arm_id=arm["arm_id"], status="open"):
        bar = current.get(row["coin"])
        if bar is None:
            continue
        IS.update_altshort_signal_mark(
            conn, signal_id=row["id"], mark=float(bar["mark"]), ts_ms=now_ms
        )
        exit_ = evaluate_sampled_exit(
            entry_mark=float(row["entry_mark"]), entry_ts_ms=int(row["entry_ts_ms"]),
            now_ts_ms=now_ms,
            current={
                "open": bar["open_mark"], "high": bar["high_mark"],
                "low": bar["low_mark"], "close": bar["mark"],
            },
            stop_multiple=float(policy.get("stop_multiple", 1.20)),
            take_profit_multiple=float(policy.get("take_profit_multiple", 0.85)),
            cap_hours=int(policy.get("cap_hours", 8)),
        )
        if not exit_["exit"]:
            continue
        entries = json.loads(row["benchmark_entry_json"])
        signal_funding, signal_coverage = _funding_for_interval(
            conn, [row["coin"]], entry_ts_ms=row["entry_ts_ms"], exit_ts_ms=now_ms
        )
        basket_funding, _ = _funding_for_interval(
            conn, entries, entry_ts_ms=row["entry_ts_ms"], exit_ts_ms=now_ms
        )
        exits = {
            coin: float(value["mark"])
            for coin, value in current.items() if coin in entries and value["mark"] is not None
        }
        benchmark = matched_forward_basket(
            entries=entries, exits=exits, funding_bps=basket_funding,
            cost_bps=float(policy.get("cost_bps", 39.0)),
            min_coverage=float(policy.get("min_coverage", 0.80)),
        )
        provenance = "valid"
        if signal_coverage < 1.0:
            provenance = "missing_signal_funding"
        elif not benchmark["valid"]:
            provenance = "incomplete_benchmark"
        IS.close_altshort_signal(
            conn, signal_id=row["id"], exit_ts_ms=now_ms,
            exit_mark=float(exit_["exit_mark"]), exit_reason=exit_["reason"],
            funding_pnl_bps=signal_funding.get(row["coin"]),
            funding_provenance_status=("complete" if signal_coverage >= 1.0 else "missing"),
            benchmark=benchmark,
            cost_bps=float(policy.get("cost_bps", 39.0)), provenance_status=provenance,
        )
        counts["closed"] += 1

    meta_key = f"altshort_signal_last_decision:{arm['arm_id']}"
    last_processed = int(IS.get_meta(conn, meta_key, -1))
    if last_closed <= last_processed or last_closed < int(arm["activated_ts_ms"]):
        return counts
    history_hours = int(policy.get("history_hours", 720))
    bars = IS.load_altshort_regime_bars(
        conn, start_ts_ms=last_closed - history_hours * HOUR_MS - HOUR_MS,
        end_ts_ms=last_closed,
    )
    candidates = sorted(coin for coin in bars if strategy_bucket(coin) == "alt")
    crossings = {
        coin: runup_crossing(
            bars[coin], decision_ts_ms=last_closed,
            lookback_hours=int(policy.get("runup_lookback_hours", 72)),
            threshold=float(policy.get("runup_threshold", 0.30)),
            history_hours=history_hours,
            min_history_coverage=float(policy.get("min_coverage", 0.80)),
        )
        for coin in candidates
    }
    universe = [coin for coin in candidates if crossings[coin].get("valid")]
    regime = causal_basket_regime(
        bars, decision_ts_ms=last_closed, universe=universe,
        lookback_hours=int(policy.get("regime_lookback_hours", 72)),
        bearish_threshold_bps=float(policy.get("bearish_threshold_bps", -500.0)),
        bullish_threshold_bps=float(policy.get("bullish_threshold_bps", 500.0)),
        min_coverage=float(policy.get("min_coverage", 0.80)),
    )
    IS.record_altshort_signal_regime(
        conn, arm_id=arm["arm_id"], decision_ts_ms=last_closed, regime=regime,
    )
    episode_rows = []
    for row in IS.list_altshort_signal_regimes(conn, arm_id=arm["arm_id"]):
        payload = json.loads(row["regime_json"])
        episode_rows.append({"decision_ts_ms": row["decision_ts_ms"], **payload})
    annotated = assign_regime_episodes(
        episode_rows,
        separation_hours=int(policy.get("regime_separation_hours", 24)),
    )
    current_regime = annotated[-1]
    if current_regime.get("episode_id") is not None:
        regime = {**regime, "episode_id": int(current_regime["episode_id"])}
    gated_in = regime["label"] == "bearish"
    for coin in universe:
        crossing = crossings[coin]
        if not crossing.get("crossed"):
            continue
        IS.record_altshort_signal_decision(
            conn, arm_id=arm["arm_id"], coin=coin, decision_ts_ms=last_closed,
            decision_mark=float(crossing["decision_mark"]), runup=float(crossing["runup"]),
            regime=regime,
            benchmark_entry={candidate: None for candidate in universe if candidate != coin},
            gated_in=gated_in,
        )
        counts["decisions"] += 1
    IS.set_meta(conn, meta_key, str(last_closed), ts_ms=now_ms)
    return counts


def _write_heartbeat(path: Path, *, conn, arm: dict, now_ms: int, counts: dict) -> None:
    status_counts = {
        row["status"]: int(row["n"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM hl_altshort_signal_observations "
            "WHERE arm_id=? GROUP BY status",
            (arm["arm_id"],),
        )
    }
    last_processed = int(IS.get_meta(
        conn, f"altshort_signal_last_decision:{arm['arm_id']}", -1
    ))
    payload = {
        "ts_ms": int(now_ms), "arm_id": arm["arm_id"], "code_sha": arm["code_sha"],
        "activated_ts_ms": int(arm["activated_ts_ms"]), "policy_hash": arm["policy_hash"],
        "hypothesis": "standalone_exhaustion", "dirty": False, "counts": counts,
        "last_processed_closed_hour_ms": last_processed, "status_counts": status_counts,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Standalone HL exhaustion paper scanner")
    parser.add_argument("--db-path", default=DEFAULT_DB)
    parser.add_argument("--arm-manifest", required=True)
    parser.add_argument("--heartbeat-path", default="data/hl_altshort_signal.heartbeat")
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    conn = IS.connect(args.db_path)
    IS.ensure_schema(conn)
    now_ms = int(real_utc_now() * 1000)
    arm = _load_arm(conn, Path(args.arm_manifest), now_ms=now_ms)
    try:
        while True:
            now_ms = int(real_utc_now() * 1000)
            counts = _cycle(conn, arm=arm, now_ms=now_ms)
            _write_heartbeat(
                Path(args.heartbeat_path), conn=conn, arm=arm, now_ms=now_ms, counts=counts
            )
            if args.once:
                break
            time.sleep(args.interval_seconds)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
