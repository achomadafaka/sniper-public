#!/usr/bin/env python3
"""Deterministic, cache-only HL cross-sectional rollover backtest."""
from __future__ import annotations

import argparse
import csv
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.hyperlive.altshort_universe import (  # noqa: E402
    coin_comparison_key,
    provider_coin,
    provider_coin_index,
)
from sniper.hyperlive.rollover_research import (  # noqa: E402
    RolloverPolicy,
    attach_matched_basket,
    build_causal_frames,
    causal_regime_report,
    continuation_feature_report,
    historical_verdict,
    period_report,
    policy_sha256,
    replay_sampled_exits,
    simulate_rollover,
    summarize_events,
)


RESEARCH_CODE_PATHS = (
    "sniper/hyperlive/rollover_research.py",
    "scripts/research/hl_rollover_backtest.py",
    "data/research/hl_rollover_backtest/policy.v1.json",
    "data/research/hl_rollover_backtest/selective_prereg.v1.json",
    "data/research/hl_rollover_backtest/selective_prereg.v2.json",
)
SELECTIVE_PREREG_PATH = (
    ROOT / "data/research/hl_rollover_backtest/selective_prereg.v2.json"
)
SELECTIVE_PREREG_SHA256 = {
    "selective-rollover-forward-v1":
        "68599c746d351170c01ab0554b41f8797dd0d74b19990f198d25327e91889cd1",
    "selective-rollover-forward-v2":
        "bb9ced9dda198d98ffbdc254a60af89a808f6d481acffed0d1c03add7c33dfcd",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def research_git_state() -> dict:
    code_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    status = subprocess.check_output(
        ["git", "status", "--short", "--", *RESEARCH_CODE_PATHS],
        cwd=ROOT,
        text=True,
    )
    dirty_paths = sorted(line[3:] for line in status.splitlines() if line.strip())
    return {"code_sha": code_sha, "dirty": bool(dirty_paths), "dirty_paths": dirty_paths}


def _load_policy(path: Path) -> RolloverPolicy:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid policy file: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("policy must be a JSON object")
    return RolloverPolicy.from_dict(value)


def _load_selective_prereg(path: Path) -> tuple[dict, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid selective prereg file: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("selective prereg must be a JSON object")
    canonical = (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    version = value.get("prereg_version")
    if version not in SELECTIVE_PREREG_SHA256 or digest != SELECTIVE_PREREG_SHA256[version]:
        raise ValueError("frozen selective prereg differs from registered version")
    return value, digest


def _load_cache(
    cache_dir: Path, policy: RolloverPolicy, *, require_provider_identity: bool = False
) -> tuple[dict, dict, dict]:
    universe_path = cache_dir / "_universe.json"
    if not universe_path.is_file():
        raise ValueError(f"missing universe: {universe_path}")
    universe = json.loads(universe_path.read_text(encoding="utf-8"))
    if not isinstance(universe, list):
        raise ValueError("cache universe must be a list")
    provider_identity_valid = False
    identity_path = cache_dir / "_cache_identity.json"
    if identity_path.is_file():
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        expected_identity = {
            "schema_version": 1,
            "source": "metaAndAssetCtxs.universe.name",
            "symbol_identity": "provider_exact_case_sensitive",
        }
        if any(identity.get(key) != value for key, value in expected_identity.items()):
            raise ValueError("invalid provider cache identity")
        if identity.get("universe_sha256") != _sha256(universe_path):
            raise ValueError("provider cache universe hash mismatch")
        provider_identity_valid = True
    elif require_provider_identity:
        raise ValueError("missing provider cache identity")
    provider_coin_index(universe)
    coins = sorted({
        coin
        for value in universe
        if (coin := provider_coin(value)) is not None
        and coin_comparison_key(coin) not in policy.excluded_coins
    })
    candles = {}
    funding = {}
    missing_candles = []
    missing_funding = []
    max_candle_ts_ms = None
    hashes = {"_universe.json": _sha256(universe_path)}
    if identity_path.is_file():
        hashes["_cache_identity.json"] = _sha256(identity_path)
    for coin in coins:
        candle_path = cache_dir / f"candles_{coin}.json"
        funding_path = cache_dir / f"funding_{coin}.json"
        if not candle_path.is_file():
            missing_candles.append(coin)
            continue
        candle_rows = json.loads(candle_path.read_text(encoding="utf-8"))
        if not candle_rows:
            missing_candles.append(coin)
            continue
        candles[coin] = pd.DataFrame(candle_rows)
        coin_times = pd.to_numeric(candles[coin].get("t"), errors="coerce").dropna()
        if not coin_times.empty:
            coin_max = int(coin_times.max())
            max_candle_ts_ms = (
                coin_max
                if max_candle_ts_ms is None
                else max(max_candle_ts_ms, coin_max)
            )
        hashes[candle_path.name] = _sha256(candle_path)
        if not funding_path.is_file():
            missing_funding.append(coin)
            funding[coin] = pd.DataFrame(columns=["time", "rate"])
        else:
            funding[coin] = pd.DataFrame(
                json.loads(funding_path.read_text(encoding="utf-8"))
            )
            hashes[funding_path.name] = _sha256(funding_path)
    if len(candles) < 20:
        raise ValueError(f"insufficient candle universe: {len(candles)}")
    meta = {
        "loaded_coins": sorted(candles),
        "missing_candle_coins": missing_candles,
        "missing_funding_coins": missing_funding,
        "universe_nonmajor_n": len(coins),
        "historical_universe_coverage": len(candles) / len(coins) if coins else 0.0,
        "provider_symbol_identity_valid": provider_identity_valid,
        "max_candle_ts_ms": max_candle_ts_ms,
        "input_hashes": dict(sorted(hashes.items())),
    }
    return candles, funding, meta


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _flatten_event(event: dict) -> dict:
    row = dict(event)
    row["decision_features_json"] = json.dumps(
        row.pop("decision_features", {}),
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
    return row


def _write_csv(path: Path, rows: list[dict]) -> None:
    flattened = [_flatten_event(row) if "decision_features" in row else dict(row) for row in rows]
    fields = sorted({key for row in flattened for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in flattened:
            writer.writerow(
                {
                    key: json.dumps(value, separators=(",", ":"), default=_json_default)
                    if isinstance(value, (list, dict))
                    else value
                    for key, value in row.items()
                }
            )


def _top_only_frames(frames, policy):
    signal = (
        frames.eligible
        & frames.features["return_24h_percentile"].ge(policy.return_percentile_min)
        & frames.features["runup_24h_percentile"].ge(policy.runup_percentile_min)
    )
    return replace(frames, signal=signal)


def _next_execution_gate(frames, rule: dict) -> pd.DataFrame:
    features = frames.features
    return (
        features["return_1h_bps"].gt(rule["return_1h_bps_gt"])
        & features["return_12h_bps"].le(rule["return_12h_bps_lte"])
        & features["volume_ratio_24h"].le(rule["volume_ratio_24h_lte"])
    )


def _compact_tail_summary(events: list[dict]) -> dict:
    closed = [event for event in events if event.get("status") == "closed"]
    net = [float(event["net_bps"]) for event in closed]
    residual = [
        float(event["residual_bps"])
        for event in closed
        if event.get("residual_bps") is not None
    ]
    cap = [event for event in closed if event.get("exit_reason") == "time_cap"]
    return {
        "n": len(closed),
        "mean_net_bps": float(np.mean(net)) if net else None,
        "mean_residual_bps": float(np.mean(residual)) if residual else None,
        "cap_exit_n": len(cap),
        "worst_net_bps": min(net) if net else None,
    }


def _passes_next_rule(
    event: dict, rule: dict, *, field: str | None = None, value=None
) -> bool:
    features = event.get("decision_features", {})
    rule = dict(rule)
    if field is not None:
        rule[field] = value
    required = ("return_1h_bps", "return_12h_bps", "volume_ratio_24h")
    if any(features.get(name) is None for name in required):
        return False
    return (
        features["return_1h_bps"] > rule["return_1h_bps_gt"]
        and features["return_12h_bps"] <= rule["return_12h_bps_lte"]
        and features["volume_ratio_24h"] <= rule["volume_ratio_24h_lte"]
    )


def _forward_prereg_report(
    prereg: dict,
    prereg_sha256: str,
    cache_meta: dict,
    execution_events: list[dict],
) -> dict:
    start_ts_ms = int(prereg["forward_start_ts_ms"])
    forward_closed = [
        event
        for event in execution_events
        if event.get("status") == "closed"
        and int(event.get("decision_ts_ms") or 0) >= start_ts_ms
    ]
    blockers = []
    required = prereg["forward_evidence"]
    if len(forward_closed) < int(required["min_closed_events"]):
        blockers.append("independent_forward_sample_missing")
    blockers.append("nomination_episode_accounting_missing")
    coverage = float(cache_meta.get("historical_universe_coverage") or 0.0)
    if prereg["prereg_version"] == "selective-rollover-forward-v1":
        if coverage < float(prereg["coverage"]["historical_universe_min"]):
            blockers.append("historical_survivorship_unbounded")
        feature_names = prereg["coverage"]["required_decision_feeds"]
        feature_prefix = "decision_feed_missing"
    else:
        if prereg["historical_tail"]["status"] != "bounded":
            blockers.append("historical_delisted_tail_unbounded")
        feature_names = (
            prereg["decision_features"]["required_direct"]
            + prereg["decision_features"]["required_proxies"]
        )
        feature_prefix = "decision_feature_missing"
    blockers.extend(f"{feature_prefix}:{feature}" for feature in feature_names)
    blockers.append("subhour_execution_path_missing")
    if prereg["risk"]["status"] != "defined":
        blockers.append("risk_envelope_undefined")
    return {
        "status": "blocked_prereg_only" if blockers else "eligible_for_review",
        "eligible_for_review": not blockers,
        "prereg_sha256": prereg_sha256,
        "policy": prereg,
        "current_evidence": {
            "forward_closed_events": len(forward_closed),
            "historical_universe_coverage": coverage,
            "historical_cache_max_ts_ms": cache_meta.get("max_candle_ts_ms"),
            "decision_feed_coverage": {
                feature: 0.0 for feature in feature_names
            },
            "execution_sample_seconds": 3600,
        },
        "blockers": blockers,
    }


def _exit_sensitivity(events, frames, funding, policy):
    variants = [(cap_hours, None) for cap_hours in (12, 24, 48, 72, 96)]
    variants.extend((96, stop) for stop in (300.0, 500.0, 1000.0, 2000.0))
    rows = []
    for index, (cap_hours, stop_loss_bps) in enumerate(variants):
        replayed = replay_sampled_exits(
            events,
            frames,
            funding,
            policy,
            cap_hours=cap_hours,
            stop_loss_bps=stop_loss_bps,
        )
        attached = attach_matched_basket(replayed, frames, funding, policy)
        rows.append(
            {
                "cap_hours": cap_hours,
                "stop_loss_bps": stop_loss_bps,
                **summarize_events(
                    attached,
                    seed=policy.bootstrap_seed + 1200 + index,
                    replications=policy.bootstrap_replications,
                ),
            }
        )
    return rows


def _next_hypothesis_report(
    events,
    execution_events,
    replacement_events,
    exit_sensitivity,
    policy,
    prereg,
    prereg_sha256,
    cache_meta,
):
    seed = policy.bootstrap_seed
    train_cutoff_ms = int(pd.Timestamp("2026-05-01", tz="UTC").timestamp() * 1000)
    train = [
        event
        for event in events
        if event.get("status") == "closed"
        and int(event["entry_ts_ms"]) < train_cutoff_ms
    ]
    train_medians = {}
    for feature in ("return_1h_bps", "return_12h_bps", "volume_ratio_24h"):
        values = [
            float(event["decision_features"][feature])
            for event in train
            if event.get("decision_features", {}).get(feature) is not None
        ]
        train_medians[feature] = float(np.median(values)) if values else None
    later = [
        event
        for event in execution_events
        if event.get("entry_ts_ms") is not None
        and int(event["entry_ts_ms"]) >= train_cutoff_ms
    ]
    sensitivity = []
    grids = {
        "return_1h_bps_gt": [-120.0, -90.0, -60.0, -30.0, 0.0],
        "return_12h_bps_lte": [200.0, 300.0, 350.0, 400.0, 500.0, 700.0],
        "volume_ratio_24h_lte": [0.8, 1.0, 1.2, 1.5, 2.0],
    }
    for field, values in grids.items():
        for value in values:
            selected = [
                event
                for event in events
                if _passes_next_rule(event, prereg["rule"], field=field, value=value)
            ]
            sensitivity.append(
                {"field": field, "value": value, **_compact_tail_summary(selected)}
            )
    return {
        "status": "posthoc_not_validated",
        "inference_status": "invalid_post_selection",
        "headline_allowed": False,
        "confidence_interval_use": "descriptive_audit_only",
        "rule": dict(prereg["rule"]),
        "ordering": (
            "nominate_top_five_then_gate_without_replacement_"
            "and_track_rejections_as_shadow_positions"
        ),
        "shadow_cooldown": "first_sampled_200bps_short_profit_or_96h_cap",
        "selection_source": (
            "exploratory_v1_splits_with_cutoffs_anchored_to_"
            "2026_02_through_2026_04_medians"
        ),
        "selection_train_medians": train_medians,
        "holdout_warning": "May-July had already been inspected; this is not sealed OOS evidence.",
        "execution_after_nomination": {
            "overall": summarize_events(
                execution_events, seed=seed + 606, replications=policy.bootstrap_replications
            ),
            "months": period_report(
                execution_events,
                period="month",
                seed=seed + 707,
                replications=policy.bootstrap_replications,
            ),
            "pseudo_holdout_from_2026_05_01": summarize_events(
                later, seed=seed + 808, replications=policy.bootstrap_replications
            ),
        },
        "replacement_control": summarize_events(
            replacement_events, seed=seed + 909, replications=policy.bootstrap_replications
        ),
        "exit_sensitivity": exit_sensitivity,
        "threshold_sensitivity": sensitivity,
        "forward_prereg": _forward_prereg_report(
            prereg, prereg_sha256, cache_meta, execution_events
        ),
    }


def run(
    *,
    cache_dir: Path,
    policy_path: Path,
    output_dir: Path,
    selective_prereg_path: Path = SELECTIVE_PREREG_PATH,
) -> dict:
    cache_dir = Path(cache_dir).resolve()
    policy_path = Path(policy_path).resolve()
    output_dir = Path(output_dir).resolve()
    selective_prereg_path = Path(selective_prereg_path).resolve()
    if output_dir.exists():
        raise FileExistsError(f"output already exists: {output_dir}")
    policy = _load_policy(policy_path)
    selective_prereg, selective_prereg_sha256 = _load_selective_prereg(
        selective_prereg_path
    )
    if selective_prereg["base_policy_sha256"] != policy_sha256(policy):
        raise ValueError("selective prereg base policy differs from loaded policy")
    git_state = research_git_state()
    if git_state["dirty"]:
        raise ValueError(f"dirty research code: {git_state['dirty_paths']}")
    candles, funding, cache_meta = _load_cache(
        cache_dir,
        policy,
        require_provider_identity=(
            selective_prereg["prereg_version"] == "selective-rollover-forward-v2"
        ),
    )
    if int(cache_meta.get("max_candle_ts_ms") or 0) > int(
        selective_prereg["historical_data_cutoff_ts_ms"]
    ):
        raise ValueError("historical runner refuses post-freeze candle data")
    frames = build_causal_frames(candles, policy)
    events = attach_matched_basket(
        simulate_rollover(frames, funding, policy), frames, funding, policy
    )
    top_only_events = attach_matched_basket(
        simulate_rollover(_top_only_frames(frames, policy), funding, policy),
        frames,
        funding,
        policy,
    )
    max_open_events = attach_matched_basket(
        simulate_rollover(frames, funding, policy, max_open=5), frames, funding, policy
    )
    next_gate = _next_execution_gate(frames, selective_prereg["rule"])
    next_execution_raw = simulate_rollover(
        frames, funding, policy, execution_gate=next_gate
    )
    next_execution_events = attach_matched_basket(
        next_execution_raw,
        frames,
        funding,
        policy,
    )
    replacement_events = attach_matched_basket(
        simulate_rollover(frames, funding, policy, candidate_gate=next_gate),
        frames,
        funding,
        policy,
    )
    seed = policy.bootstrap_seed
    reps = policy.bootstrap_replications
    overall = summarize_events(events, seed=seed, replications=reps)
    top_only = summarize_events(top_only_events, seed=seed + 101, replications=reps)
    max_open = summarize_events(max_open_events, seed=seed + 202, replications=reps)
    months = period_report(
        events, period="month", seed=seed + 303, replications=reps
    )
    weeks = period_report(
        events, period="week", seed=seed + 404, replications=reps
    )
    regimes = causal_regime_report(events, seed=seed + 505, replications=reps)
    features = continuation_feature_report(events)
    report = {
        "study_id": "hl-cross-sectional-rollover-v1",
        "policy_sha256": policy_sha256(policy),
        "provenance_valid": True,
        "older_history": "blocked_paid_archive",
        "overall": overall,
        "months": months,
        "weeks": weeks,
        "regimes": regimes,
        "baselines": {
            "extreme_winner_without_rollover": top_only,
            "max_five_open_sensitivity": max_open,
        },
        "double_slippage_mean_net_bps": (
            float(overall["mean_net_bps"]) - 2.0 * policy.slippage_each_bps
            if overall["mean_net_bps"] is not None
            else None
        ),
        "admissions": {
            "total": len(events),
            "closed": sum(event.get("status") == "closed" for event in events),
            "invalid": sum(event.get("status") == "invalid" for event in events),
            "censored": sum(event.get("status") == "censored" for event in events),
        },
        "missing_feeds": [
            "open_interest",
            "liquidation_volume",
            "spot_perp_basis",
            "orderbook_replenishment",
        ],
        "exploratory_next_hypothesis": _next_hypothesis_report(
            events,
            next_execution_events,
            replacement_events,
            _exit_sensitivity(next_execution_raw, frames, funding, policy),
            policy,
            selective_prereg,
            selective_prereg_sha256,
            cache_meta,
        ),
    }
    report["verdict"] = historical_verdict(report)
    periods = [
        {"period_kind": "month", **row} for row in months
    ] + [{"period_kind": "week", **row} for row in weeks]
    run_meta = {
        **git_state,
        **cache_meta,
        "policy_path": str(policy_path),
        "policy_sha256": policy_sha256(policy),
        "selective_prereg_path": str(selective_prereg_path),
        "selective_prereg_sha256": selective_prereg_sha256,
        "network_used": False,
        "cache_dir": str(cache_dir),
    }

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temp = output_dir.parent / f".{output_dir.name}.tmp-{uuid.uuid4().hex}"
    temp.mkdir()
    try:
        _write_csv(temp / "events.csv", events)
        _write_csv(temp / "periods.csv", periods)
        _write_csv(temp / "regimes.csv", regimes)
        _write_csv(temp / "feature_falsification.csv", features)
        _write_json(temp / "report.json", report)
        _write_json(temp / "run_meta.json", run_meta)
        temp.rename(output_dir)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return {"report": report, "run_meta": run_meta, "events": events}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument(
        "--selective-prereg", type=Path, default=SELECTIVE_PREREG_PATH
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    result = run(
        cache_dir=args.cache_dir,
        policy_path=args.policy,
        output_dir=args.output_dir,
        selective_prereg_path=args.selective_prereg,
    )
    print(json.dumps(result["report"]["verdict"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
