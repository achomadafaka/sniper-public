#!/usr/bin/env python3
"""Cache-only, manifest-pinned causal study for the HL alt-short paper program."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive.altshort_evidence import (  # noqa: E402
    CostModel,
    altshort_verdict,
    block_bootstrap_mean,
    concentration_report,
    fixed_slot_portfolio,
    matched_alt_basket_return,
    next_tradeable_price,
    passive_alt_basket,
    simulate_exit,
)
from sniper.hyperlive.altshort_signal_paper import evaluate_sampled_exit  # noqa: E402
from sniper.hyperlive.altshort_universe import (  # noqa: E402
    coin_comparison_key,
    lookup_coin,
    normalize_coin,
    provider_coin_index,
    strategy_bucket,
)


REQUIRED_MANIFEST_FIELDS = (
    "schema_version",
    "study_id",
    "code_sha",
    "events_path",
    "universe_path",
    "candle_dir",
    "funding_dir",
    "inputs",
    "evaluation_windows",
    "window_bounds",
    "costs",
    "portfolio",
    "bootstrap",
    "thresholds",
    "survivorship_bias",
)

STUDY_CODE_PATHS = [
    "scripts/hl_altshort_study.py",
    "sniper/hyperlive/altshort_evidence.py",
    "sniper/hyperlive/altshort_signal_paper.py",
    "sniper/hyperlive/altshort_universe.py",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(path: Path, pattern: str) -> str:
    digest = hashlib.sha256()
    for child in sorted(path.glob(pattern), key=lambda item: item.name):
        if not child.is_file():
            continue
        digest.update(child.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(child)))
    return digest.hexdigest()


def validate_manifest(manifest: dict, *, input_root: Path, actual_code_sha: str,
                      dirty: bool) -> list[str]:
    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in manifest:
            raise ValueError(f"manifest missing required field: {field}")
    if manifest["schema_version"] != 2:
        raise ValueError("unsupported manifest schema_version")
    blockers = []
    if manifest["code_sha"] != actual_code_sha:
        blockers.append("code_sha_mismatch")
    if dirty:
        blockers.append("unversioned_code")
    for item in manifest["inputs"]:
        relative = str(item["path"])
        path = input_root / relative
        if not path.exists():
            blockers.append(f"missing_input:{relative}")
            continue
        if "sha256" in item and sha256_file(path) != item["sha256"]:
            blockers.append(f"hash_mismatch:{relative}")
        if "sha256_tree" in item:
            actual = sha256_tree(path, str(item.get("glob") or "*"))
            if actual != item["sha256_tree"]:
                blockers.append(f"hash_mismatch:{relative}")
    return blockers


def validate_cache_seal(seal: dict, *, universe_path: Path, cache_dir: Path) -> list[str]:
    blockers = []
    if int(seal.get("schema_version") or 0) != 2:
        return ["cache_seal_schema_invalid"]
    if sha256_file(universe_path) != seal.get("universe_sha256"):
        blockers.append("cache_seal_universe_hash_mismatch")
    universe = json.loads(universe_path.read_text(encoding="utf-8"))
    declared = {row.get("provider_symbol"): row for row in seal.get("symbols") or []}
    if set(declared) != set(universe):
        blockers.append("cache_seal_symbol_set_mismatch")
    for coin in universe:
        row = declared.get(coin) or {}
        for kind in ("candles", "funding"):
            meta = row.get(kind) or {}
            expected_name = f"{kind}_{coin}.json"
            if meta.get("path") != expected_name:
                blockers.append(f"cache_seal_path_mismatch:{coin}:{kind}")
                continue
            path = cache_dir / expected_name
            if not path.exists():
                if meta.get("status") != "missing" or meta.get("sha256") is not None:
                    blockers.append(f"cache_seal_missing_mismatch:{coin}:{kind}")
            elif sha256_file(path) != meta.get("sha256"):
                blockers.append(f"cache_seal_hash_mismatch:{coin}:{kind}")
    return blockers


def _load_candles(candle_dir: Path, universe: list[str]) -> dict[str, list[dict]]:
    out = {}
    for coin in sorted(set(universe) | {"BTC"}):
        path = candle_dir / f"candles_{coin}.json"
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not raw:
            continue
        out[coin] = [
            {
                "ts_ms": int(row["t"]),
                "open": float(row["o"]),
                "high": float(row.get("h", row["o"])),
                "low": float(row.get("l", row["o"])),
                "close": float(row["c"]),
            }
            for row in raw
        ]
    return out


def _load_funding(funding_dir: Path, universe: list[str]) -> dict[str, list[dict]]:
    out = {}
    for coin in sorted(set(universe) | {"BTC"}):
        path = funding_dir / f"funding_{coin}.json"
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list) and raw:
            out[coin] = raw
    return out


def _load_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _day_week(ts_ms: int) -> tuple[str, str]:
    value = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    iso = value.isocalendar()
    return value.date().isoformat(), f"{iso.year}-W{iso.week:02d}"


def _eligible_universe_at(bars_by_coin: dict[str, list[dict]], *, decision_ts_ms: int,
                          lookback_ms: int) -> list[str]:
    cutoff = int(decision_ts_ms) - int(lookback_ms)
    eligible = []
    for coin, bars in bars_by_coin.items():
        if coin == "BTC" or not bars:
            continue
        first_ts = min(int(row["ts_ms"]) for row in bars)
        has_tradeable_bar = any(int(row["ts_ms"]) > decision_ts_ms for row in bars)
        if first_ts <= cutoff and has_tradeable_bar:
            eligible.append(coin)
    return sorted(eligible)


def _funding_bps(rows: list[dict], *, entry_ts_ms: int, exit_ts_ms: int) -> float:
    total = 0.0
    for row in rows:
        try:
            ts_ms = int(row.get("time", row.get("ts_ms")))
            rate = float(row["rate"])
        except (KeyError, TypeError, ValueError):
            continue
        if int(entry_ts_ms) < ts_ms <= int(exit_ts_ms):
            total += rate * 10_000.0
    return total


def hourly_close_bridge_event(
    trade: dict, bars_by_coin: dict[str, list[dict]], *, universe,
    funding_by_coin: dict[str, list[dict]], costs: CostModel,
    min_coverage: float = 0.8, stop_multiple: float = 1.20,
    take_profit_multiple: float = 0.85, cap_hours: int = 8,
) -> dict:
    """Replay one event using hourly closes as the only observable exit samples."""
    coin = normalize_coin(trade.get("coin"))
    if coin is None or strategy_bucket(coin) != "alt":
        return {"valid": False, "reason": "not_alt_short"}
    normalized = {normalize_coin(key): value for key, value in bars_by_coin.items()}
    normalized_funding = {
        normalize_coin(key): value for key, value in funding_by_coin.items()
    }
    signal_bars = normalized.get(coin, [])
    entry = next_tradeable_price(signal_bars, decision_ts_ms=int(trade["decision_ts_ms"]))
    if entry is None:
        return {"valid": False, "reason": "missing_signal_entry"}
    entry_ts_ms, entry_mark = entry
    exit_result = None
    exit_observation_ts_ms = None
    for row in sorted(signal_bars, key=lambda item: int(item["ts_ms"])):
        ts_ms = int(row["ts_ms"])
        if ts_ms < entry_ts_ms or ts_ms > entry_ts_ms + int(cap_hours) * 3_600_000:
            continue
        close = float(row["close"])
        sampled = evaluate_sampled_exit(
            entry_mark=entry_mark, entry_ts_ms=entry_ts_ms, now_ts_ms=ts_ms,
            current={"close": close, "high": close, "low": close},
            stop_multiple=stop_multiple,
            take_profit_multiple=take_profit_multiple,
            cap_hours=cap_hours,
        )
        if sampled["exit"]:
            exit_result = sampled
            exit_observation_ts_ms = ts_ms
            break
    if exit_result is None or exit_observation_ts_ms is None:
        return {"valid": False, "reason": "missing_sampled_exit"}

    eligible = sorted({
        normalized_coin for value in universe
        if (normalized_coin := normalize_coin(value)) is not None
        and normalized_coin != coin and strategy_bucket(normalized_coin) == "alt"
    })
    basket_values = []
    for candidate in eligible:
        candidate_bars = normalized.get(candidate, [])
        candidate_entry = next_tradeable_price(
            candidate_bars, decision_ts_ms=int(trade["decision_ts_ms"])
        )
        exit_rows = [
            row for row in candidate_bars
            if int(row["ts_ms"]) == exit_observation_ts_ms
        ]
        if candidate_entry is None or not exit_rows:
            continue
        exit_mark = float(exit_rows[0]["close"])
        gross = (candidate_entry[1] - exit_mark) / candidate_entry[1] * 10_000.0
        gross += _funding_bps(
            normalized_funding.get(candidate, []), entry_ts_ms=candidate_entry[0],
            exit_ts_ms=exit_observation_ts_ms,
        )
        basket_values.append(gross)
    coverage = len(basket_values) / len(eligible) if eligible else 0.0
    if coverage < float(min_coverage):
        return {
            "valid": False, "reason": "insufficient_basket_coverage",
            "coverage": coverage, "eligible_n": len(eligible),
            "covered_n": len(basket_values),
        }
    signal_gross = (entry_mark - float(exit_result["exit_mark"])) / entry_mark * 10_000.0
    signal_gross += _funding_bps(
        funding_by_coin.get(coin, []), entry_ts_ms=entry_ts_ms,
        exit_ts_ms=exit_observation_ts_ms,
    )
    signal_net = signal_gross - costs.round_trip_bps
    basket_net = statistics.fmean(basket_values) - costs.round_trip_bps
    return {
        "valid": True, "reason": "ok", "entry_ts_ms": entry_ts_ms,
        "exit_observation_ts_ms": exit_observation_ts_ms,
        "exit_reason": exit_result["reason"], "coverage": coverage,
        "eligible_n": len(eligible), "covered_n": len(basket_values),
        "signal_net_bps": signal_net, "basket_net_bps": basket_net,
        "residual_bps": signal_net - basket_net,
    }


def _exit_attribution(manifest: dict, input_root: Path, bars_by_coin: dict[str, list[dict]],
                      funding_by_coin: dict[str, list[dict]], costs: CostModel) -> dict:
    reference_path = manifest.get("wallet_reference_path")
    if not reference_path:
        return {"valid": False, "reason": "missing_wallet_reference", "holdout_advantage_bps": None}
    rows = sorted(
        _load_csv(input_root / reference_path),
        key=lambda row: int(row["detect_ts_ms"]),
    )
    horizons = [int(value) for value in manifest.get(
        "exit_horizons_ms", [3_600_000, 14_400_000, 28_800_000, 86_400_000, 172_800_000]
    )]
    scored = []
    for row in rows:
        matched = lookup_coin(bars_by_coin, row["coin"])
        if matched is None:
            continue
        coin, bars = matched
        by_horizon = {}
        for horizon in horizons:
            result = simulate_exit(
                {"coin": coin, "side": "short", "decision_ts_ms": int(row["detect_ts_ms"])},
                bars,
                rule={"id": str(horizon), "kind": "fixed", "horizon_ms": horizon},
                costs=costs,
            )
            if result.get("valid"):
                funding = 0.0
                funding_match = lookup_coin(funding_by_coin, coin)
                for print_ in funding_match[1] if funding_match is not None else []:
                    ts_ms = int(print_.get("time", 0))
                    if result["entry_ts_ms"] < ts_ms <= result["exit_ts_ms"]:
                        funding += float(print_.get("rate") or 0.0) * 10_000.0
                by_horizon[str(horizon)] = float(result["net_bps"]) + funding
        if by_horizon:
            scored.append({
                "coin": coin,
                "detect_ts_ms": int(row["detect_ts_ms"]),
                "wallet_net_bps": float(row["ref_pnl_bps"]),
                "mechanical": by_horizon,
            })
    if len(scored) < 10:
        return {"valid": False, "reason": "insufficient_exit_sample", "n": len(scored),
                "holdout_advantage_bps": None}
    split = max(1, int(len(scored) * 0.7))
    train, holdout = scored[:split], scored[split:]
    means = {
        str(horizon): statistics.fmean(
            row["mechanical"][str(horizon)] for row in train if str(horizon) in row["mechanical"]
        )
        for horizon in horizons
        if any(str(horizon) in row["mechanical"] for row in train)
    }
    selected = max(means, key=means.get)
    paired = [row for row in holdout if selected in row["mechanical"]]
    if not paired:
        return {"valid": False, "reason": "empty_exit_holdout", "holdout_advantage_bps": None}
    wallet_mean = statistics.fmean(row["wallet_net_bps"] for row in paired)
    mechanical_mean = statistics.fmean(row["mechanical"][selected] for row in paired)
    return {
        "valid": True,
        "reason": "ok",
        "n_total": len(scored),
        "train_n": len(train),
        "holdout_n": len(paired),
        "selected_horizon_ms": int(selected),
        "wallet_holdout_mean_bps": wallet_mean,
        "mechanical_holdout_mean_bps": mechanical_mean,
        "holdout_advantage_bps": wallet_mean - mechanical_mean,
    }


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8")


def _write_csv(path: Path, rows: list[dict]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field) for field in fields} for row in rows])


def _compact_result(value: dict) -> dict:
    omitted = {"equity_curve", "accepted_ids", "rejected_ids"}
    return {key: item for key, item in value.items() if key not in omitted}


def run_study(manifest_path: Path, *, input_root: Path, output_dir: Path,
              actual_code_sha: str, dirty: bool) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cache_seal_path = (
        input_root / str(manifest["cache_seal_path"])
        if manifest.get("cache_seal_path") else None
    )
    cache_seal = (
        json.loads(cache_seal_path.read_text(encoding="utf-8"))
        if cache_seal_path is not None else {}
    )
    evidence_status = str(cache_seal.get("evidence_status") or "complete")
    blockers = validate_manifest(
        manifest, input_root=input_root, actual_code_sha=actual_code_sha, dirty=dirty
    )
    universe = json.loads((input_root / manifest["universe_path"]).read_text(encoding="utf-8"))
    if cache_seal_path is not None:
        blockers.extend(validate_cache_seal(
            cache_seal, universe_path=input_root / manifest["universe_path"],
            cache_dir=input_root / manifest["candle_dir"],
        ))
    provider_symbols = provider_coin_index(universe)
    bars_by_coin = _load_candles(input_root / manifest["candle_dir"], universe)
    funding_by_coin = _load_funding(input_root / manifest["funding_dir"], universe)
    windows = set(manifest["evaluation_windows"])
    raw_events = [
        row for row in _load_csv(input_root / manifest["events_path"])
        if row.get("window") in windows
    ]
    costs = CostModel(round_trip_bps=float(manifest["costs"]["round_trip_bps"]))
    min_coverage = float(manifest["thresholds"].get("min_coverage", 0.8))
    eligibility_lookback_ms = int(manifest.get("historical_eligibility_lookback_ms", 0))
    events = []
    bridge_events = []
    exclusions: dict[str, int] = {}
    for index, row in enumerate(raw_events, start=1):
        decision_ts = int(row["entry_t"])
        event_universe = _eligible_universe_at(
            bars_by_coin, decision_ts_ms=decision_ts,
            lookback_ms=eligibility_lookback_ms,
        )
        result = matched_alt_basket_return(
            {"coin": row["coin"], "side": "short", "decision_ts_ms": decision_ts,
             "exit_ts_ms": int(row["exit_t"])},
            bars_by_coin,
            universe=event_universe,
            costs=costs,
            min_coverage=min_coverage,
            funding_by_coin=funding_by_coin,
        )
        bridge = hourly_close_bridge_event(
            {"coin": row["coin"], "decision_ts_ms": decision_ts},
            bars_by_coin, universe=event_universe, funding_by_coin=funding_by_coin,
            costs=costs, min_coverage=min_coverage,
            stop_multiple=float(manifest.get("exit_observation_bridge", {}).get(
                "stop_multiple", 1.20
            )),
            take_profit_multiple=float(manifest.get("exit_observation_bridge", {}).get(
                "take_profit_multiple", 0.85
            )),
            cap_hours=int(manifest.get("exit_observation_bridge", {}).get("cap_hours", 8)),
        )
        if bridge.get("valid"):
            bridge_events.append({"id": index, "coin": row["coin"], **bridge})
        if not result.get("valid"):
            reason = str(result.get("reason") or "unknown")
            exclusions[reason] = exclusions.get(reason, 0) + 1
            continue
        day, week = _day_week(int(result["entry_ts_ms"]))
        events.append({
            "id": index,
            "coin": provider_symbols.get(
                coin_comparison_key(row["coin"]), str(row["coin"]).strip()
            ),
            "window": row["window"],
            "universe_n": len(event_universe),
            "decision_ts_ms": decision_ts,
            "day": day,
            "week": week,
            **result,
        })

    bootstrap_cfg = manifest["bootstrap"]
    inference = block_bootstrap_mean(
        events,
        cluster_key="day",
        replications=int(bootstrap_cfg["replications"]),
        seed=int(bootstrap_cfg["seed"]),
    )
    portfolio_rows = [
        {
            "id": row["id"],
            "coin": row["coin"],
            "entry_ts_ms": row["entry_ts_ms"],
            "exit_ts_ms": row["exit_ts_ms"],
            "status": "closed",
            "gross_pnl_bps": row["signal_net_bps"] + costs.round_trip_bps,
        }
        for row in events
    ]
    portfolio_cfg = manifest["portfolio"]
    now_ms = max((row["exit_ts_ms"] for row in events), default=0)
    portfolio = fixed_slot_portfolio(
        portfolio_rows,
        slots=int(portfolio_cfg["slots"]),
        initial_equity=float(portfolio_cfg["initial_equity"]),
        costs=costs,
        now_ms=now_ms,
        max_mark_age_ms=0,
    )
    stress_costs = CostModel(round_trip_bps=costs.round_trip_bps * 2)
    stress = fixed_slot_portfolio(
        portfolio_rows,
        slots=int(portfolio_cfg["slots"]),
        initial_equity=float(portfolio_cfg["initial_equity"]),
        costs=stress_costs,
        now_ms=now_ms,
        max_mark_age_ms=0,
    )
    concentration = concentration_report([
        {"coin": row["coin"], "week": row["week"], "net_bps": row["signal_net_bps"]}
        for row in events
    ])
    exit_attribution = _exit_attribution(
        manifest, input_root, bars_by_coin, funding_by_coin, costs
    )
    passive = {"valid": False, "reason": "requires_one_evaluation_window"}
    if len(windows) == 1:
        window = next(iter(windows))
        bounds = manifest["window_bounds"][window]
        passive_universe = _eligible_universe_at(
            bars_by_coin, decision_ts_ms=int(bounds["start_ms"]),
            lookback_ms=eligibility_lookback_ms,
        )
        passive = passive_alt_basket(
            bars_by_coin,
            start_ms=int(bounds["start_ms"]),
            end_ms=int(bounds["end_ms"]),
            universe=passive_universe,
            costs=costs,
            min_coverage=min_coverage,
            funding_by_coin=funding_by_coin,
        )
    valid_n = len(events)
    event_summary = {
        "input_n": len(raw_events),
        "valid_n": valid_n,
        "mean_signal_net_bps": statistics.fmean(row["signal_net_bps"] for row in events)
        if events else None,
        "mean_basket_net_bps": statistics.fmean(row["basket_net_bps"] for row in events)
        if events else None,
        "mean_residual_bps": statistics.fmean(row["residual_bps"] for row in events)
        if events else None,
        "coins": len({row["coin"] for row in events}),
        "days": len({row["day"] for row in events}),
    }
    bridge_summary = {
        "method": "hourly_close_observations",
        "input_n": len(raw_events),
        "valid_n": len(bridge_events),
        "mean_signal_net_bps": statistics.fmean(
            row["signal_net_bps"] for row in bridge_events
        ) if bridge_events else None,
        "mean_basket_net_bps": statistics.fmean(
            row["basket_net_bps"] for row in bridge_events
        ) if bridge_events else None,
        "mean_residual_bps": statistics.fmean(
            row["residual_bps"] for row in bridge_events
        ) if bridge_events else None,
        "primary_residual_gap_bps": (
            statistics.fmean(row["residual_bps"] for row in bridge_events)
            - float(event_summary["mean_residual_bps"])
            if bridge_events and event_summary["mean_residual_bps"] is not None else None
        ),
        "limitation": "coarse_hourly_close_bridge_not_live_sampled_mark_replay",
    }
    regime_replication = dict(manifest.get("regime_replication") or {})
    episode_counts = [int(value) for value in regime_replication.get("closed_counts", [])]
    regime_replication.setdefault("episode_count", len(episode_counts))
    regime_replication.setdefault("min_episode_closes", min(episode_counts, default=0))
    evidence = {
        "provenance_valid": not blockers,
        "causal_features": True,
        "next_tradeable_entry": True,
        "residual_ci_low_bps": inference["ci95"][0] or 0.0,
        "fixed_slot_base_bps": portfolio.get("total_return_bps") or 0.0,
        "fixed_slot_stress_bps": stress.get("total_return_bps") or 0.0,
        "total_book_bps": portfolio.get("total_return_bps") or 0.0,
        "leave_top_coin_bps": concentration.get("leave_top_coin_bps") or 0.0,
        "leave_top_week_bps": concentration.get("leave_top_week_bps") or 0.0,
        "top_coin_share": concentration.get("top_coin_share") or 0.0,
        "top_week_share": concentration.get("top_week_share") or 0.0,
        "coverage": statistics.fmean(row["coverage"] for row in events) if events else 0.0,
        "exit_rule_valid": bool(manifest.get("primary_exit_rule_frozen", True)),
        "day_clusters": inference["clusters"],
        "stale_open_marks": 0,
        "regime_episode_count": int(regime_replication.get("episode_count") or 0),
        "min_episode_closes": int(regime_replication.get("min_episode_closes") or 0),
        "min_episode_residual_bps": regime_replication.get("min_episode_residual_bps"),
        "leave_one_episode_out_residual_bps":
            regime_replication.get("leave_one_episode_out_residual_bps"),
    }
    verdict = altshort_verdict(
        evidence,
        min_coverage=min_coverage,
        min_day_clusters=int(manifest["thresholds"].get("min_day_clusters", 20)),
        max_top_coin_share=float(manifest["thresholds"].get("max_top_coin_share", 0.35)),
        max_top_week_share=float(manifest["thresholds"].get("max_top_week_share", 0.50)),
        min_regime_episodes=int(manifest["thresholds"].get("min_regime_episodes", 2)),
        min_episode_closes=int(manifest["thresholds"].get("min_episode_closes", 5)),
    )
    if blockers:
        verdict = {"status": "blocked_missing_feed", "blocked": blockers,
                   "incomplete": [], "failed": []}
    elif evidence_status == "partial_censored":
        verdict = {"status": "partial_censored", "blocked": ["censored_delisted_tail"],
                   "incomplete": [], "failed": []}
    report = {
        "study_id": manifest["study_id"],
        "evidence_status": evidence_status,
        "events": event_summary,
        "exit_observation_bridge": bridge_summary,
        "regime_replication": regime_replication,
        "inference": {key: value for key, value in inference.items() if key != "samples"},
        "portfolio": _compact_result(portfolio),
        "cost_stress_portfolio": _compact_result(stress),
        "passive_alt_beta": _compact_result(passive),
        "concentration": concentration,
        "exit_attribution": exit_attribution,
        "hypotheses": {
            "standalone_exhaustion": {"verdict": verdict["status"]},
            "wallet_exit_discipline": {
                "verdict": (
                    "supported" if exit_attribution.get("valid")
                    and float(exit_attribution.get("holdout_advantage_bps") or 0.0) > 0
                    else "rejected"
                ),
                "holdout_advantage_bps": exit_attribution.get("holdout_advantage_bps"),
            },
            "passive_alt_beta": {
                "verdict": "descriptive_benchmark" if passive.get("valid") else "blocked_missing_feed",
                "net_bps": passive.get("net_bps"),
            },
        },
        "exclusions": exclusions,
        "survivorship_bias": bool(manifest["survivorship_bias"]),
        "funding_coverage": {
            "universe_n": len(universe),
            "coins_with_prints": len(funding_by_coin),
        },
        "provenance": {
            "code_sha": actual_code_sha,
            "dirty": dirty,
            "blockers": blockers,
            "cache_seal_sha256": sha256_file(cache_seal_path) if cache_seal_path else None,
        },
        "verdict": verdict,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "report.json", report)
    _write_json(output_dir / "exclusions.json", exclusions)
    _write_json(output_dir / "run_meta.json", report["provenance"])
    _write_csv(output_dir / "events.csv", events)
    _write_csv(output_dir / "portfolio.csv", portfolio.get("equity_curve", []))
    return report


def _git_state(root: Path, *, pinned_code_sha: str | None = None,
               code_paths: list[str] | None = None) -> tuple[str, bool]:
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True,
        capture_output=True,
    ).stdout.strip()
    status_command = ["git", "status", "--porcelain", "--untracked-files=no"]
    if code_paths:
        status_command.extend(["--", *code_paths])
    dirty = bool(subprocess.run(
        status_command,
        cwd=root, check=True, text=True,
        capture_output=True,
    ).stdout.strip())
    code_sha = head_sha
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Causal HL alt-short study (cache only)")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--input-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    root = Path(args.input_root).resolve()
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    code_sha, dirty = _git_state(
        Path(__file__).resolve().parents[1],
        pinned_code_sha=manifest.get("code_sha"),
        code_paths=STUDY_CODE_PATHS,
    )
    report = run_study(
        manifest_path, input_root=root,
        output_dir=Path(args.output_dir).resolve(), actual_code_sha=code_sha, dirty=dirty,
    )
    print(json.dumps({"study_id": report["study_id"], "verdict": report["verdict"]},
                     sort_keys=True))
    return 0 if report["verdict"]["status"] != "blocked_missing_feed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
