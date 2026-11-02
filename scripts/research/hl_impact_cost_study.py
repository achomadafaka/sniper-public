#!/usr/bin/env python3
"""Cache-only execution-cost falsification for the frozen HL 24h run-up short."""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import sqlite3
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OLD_SLIPPAGE_BPS = 30.0
TIER_COUNT = 5
SOURCE_COMMIT = "b115cc8e6ce33630913e65b1cf49abb7b8ad0838"
PREREG_COMMIT = "8ed2819c"
FILTER_CUT = 0.94993
DEFAULT_CUTOFF_MS = 1785154010747


def valid_spread(row) -> float | None:
    """Return the full bid-to-ask impact spread in bps, or None for invalid depth."""
    try:
        mid = float(row["mid_px"])
        bid = float(row["impact_bid"])
        ask = float(row["impact_ask"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (mid, bid, ask)):
        return None
    if mid <= 0 or bid <= 0 or ask < bid:
        return None
    return (ask - bid) / mid * 1e4


def _percentile(values: list[float], q: float) -> float:
    """Linear percentile using the `(n - 1) * q` convention."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    at = (len(ordered) - 1) * q
    lo = math.floor(at)
    hi = math.ceil(at)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (at - lo)


def _ntile_sizes(n: int, buckets: int) -> list[int]:
    base, extra = divmod(n, buckets)
    return [base + (1 if index < extra else 0) for index in range(buckets)]


def coin_impact_aggregates(rows) -> list[dict]:
    """One current depth observation per coin, so 60s samples never become fake replication."""
    by_coin: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        spread = valid_spread(row)
        try:
            volume = float(row["day_ntl_vlm"])
            coin = str(row["coin"])
        except (KeyError, TypeError, ValueError):
            continue
        if spread is None or not coin or not math.isfinite(volume) or volume <= 0:
            continue
        bucket = by_coin.setdefault(coin, {"spreads": [], "volumes": []})
        bucket["spreads"].append(spread)
        bucket["volumes"].append(volume)

    return sorted(
        (
            {
                "coin": coin,
                "median_volume": st.median(values["volumes"]),
                "median_spread_bps": st.median(values["spreads"]),
                "observations": len(values["spreads"]),
            }
            for coin, values in by_coin.items()
        ),
        key=lambda row: (row["median_volume"], row["coin"]),
    )


def build_liquidity_tiers(rows) -> list[dict]:
    """Aggregate tape observations per coin, then form equal-count volume quintiles."""
    coins = coin_impact_aggregates(rows)
    if len(coins) < TIER_COUNT:
        return []

    tiers = []
    offset = 0
    for tier_number, size in enumerate(_ntile_sizes(len(coins), TIER_COUNT), start=1):
        members = coins[offset:offset + size]
        offset += size
        spreads = [member["median_spread_bps"] for member in members]
        volumes = [member["median_volume"] for member in members]
        tiers.append({
            "tier": tier_number,
            "coins": len(members),
            "coin_names": [member["coin"] for member in members],
            "volume_min": min(volumes),
            "volume_max": max(volumes),
            "spread_median_bps": _percentile(spreads, 0.50),
            "spread_p75_bps": _percentile(spreads, 0.75),
            "spread_p90_bps": _percentile(spreads, 0.90),
        })
    return tiers


def assign_tier(volume: float | None, tiers: list[dict]) -> dict | None:
    try:
        parsed = float(volume)
    except (TypeError, ValueError):
        return None
    if not tiers or not math.isfinite(parsed) or parsed <= 0:
        return None
    for tier in tiers:
        if parsed <= tier["volume_max"]:
            return tier
    return tiers[-1]


def event_quote_volume(series: dict, entry_ts: int) -> float | None:
    """Quote volume over the 24 completed hourly bars immediately before entry."""
    try:
        entry_index = series["t"].index(entry_ts)
    except (KeyError, ValueError):
        return None
    if entry_index < 24:
        return None
    total = 0.0
    for close, base_volume in zip(
        series["c"][entry_index - 24:entry_index],
        series["v"][entry_index - 24:entry_index],
    ):
        try:
            contribution = float(close) * float(base_volume)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(contribution) or contribution < 0:
            return None
        total += contribution
    return total if total > 0 else None


def recharge_event(event: dict, impact_bps: float) -> dict:
    return {
        **event,
        "assigned_impact_bps": float(impact_bps),
        "impact_recharged_residual_bps": (
            float(event["residual_full_bps"]) + OLD_SLIPPAGE_BPS - float(impact_bps)
        ),
    }


def charge_events(events: list[dict], series_by_coin: dict, tiers: list[dict], *,
                  spread_field: str) -> tuple[list[dict], list[dict]]:
    charged, uncovered = [], []
    for event in events:
        coin = event["coin"]
        volume = event_quote_volume(series_by_coin.get(coin, {}), event["entry_ts"])
        if volume is None:
            uncovered.append({"coin": coin, "entry_ts": event["entry_ts"],
                              "reason": "event_volume_missing"})
            continue
        tier = assign_tier(volume, tiers)
        if tier is None:
            uncovered.append({"coin": coin, "entry_ts": event["entry_ts"],
                              "reason": "liquidity_tier_missing"})
            continue
        cost = tier.get(spread_field)
        if cost is None:
            uncovered.append({"coin": coin, "entry_ts": event["entry_ts"],
                              "reason": "tier_cost_missing"})
            continue
        charged.append({
            **recharge_event(event, cost),
            "event_quote_volume": volume,
            "liquidity_tier": tier["tier"],
        })
    return charged, uncovered


def load_tape_rows(db_path: str | Path, cutoff_ms: int) -> list[dict]:
    path = Path(db_path).resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT ts_ms, coin, mid_px, impact_bid, impact_ask, day_ntl_vlm
            FROM hl_squeeze_marks
            WHERE ts_ms <= ? AND is_delisted = 0
            ORDER BY ts_ms, coin
            """,
            (int(cutoff_ms),),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def decision(metrics: dict) -> dict:
    reasons = []
    if float(metrics["coverage"]) < 0.95:
        reasons.append("event_coverage_below_0.95")
    if int(metrics["min_tier_coins"]) < 20:
        reasons.append("liquidity_tier_has_fewer_than_20_coins")
    if float(metrics["p75_ci_low"]) <= 0:
        reasons.append("p75_coin_block_ci_not_clear_of_zero")
    return {"pass": not reasons, "reasons": reasons or ["all_pre_registered_bars_pass"]}


def _load_funding(cache_dir: Path) -> dict[str, dict[int, float]]:
    funding = {}
    for path in sorted((cache_dir / "funding").glob("funding_*.json")):
        coin = path.stem.removeprefix("funding_")
        try:
            funding[coin] = {int(key): float(value)
                             for key, value in json.loads(path.read_text()).items()}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return funding


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _funding_tree_sha256(cache_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((cache_dir / "funding").glob("funding_*.json")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(_sha256(path).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _canonical_rows_sha256(rows: list[dict]) -> str:
    payload = json.dumps(rows, separators=(",", ":"), sort_keys=True, allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _round_tree(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, list):
        return [_round_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: _round_tree(item) for key, item in value.items()}
    return value


def run_study(*, cache_dir: str | Path, tape_db: str | Path, cutoff_ms: int,
              study_dir: str | Path = "data/research/hl_squeeze_study") -> dict:
    """Execute the sealed impact-cost test without network or mutable DB access."""
    from scripts.research import hl_funding_signal as signal_study
    from scripts.research import hl_funding_study as funding_study
    from scripts.research import hl_squeeze_residual as residual_study
    from scripts.research import hl_squeeze_study as squeeze_study

    cache_dir = Path(cache_dir)
    study_dir = Path(study_dir)
    split = json.loads((study_dir / "split.v1.json").read_text())
    series = squeeze_study._load(cache_dir)
    funding = _load_funding(cache_dir)
    price_index = residual_study.build_price_index(series)
    base = residual_study.events_with_times(series, 24)
    complete = funding_study.enrich(base, price_index, funding)
    signaled = signal_study.add_signals(complete, funding)
    for event in signaled:
        event["split"] = split[event["coin"]]
    selected = [
        event for event in signaled
        if event["split"] == "HOLDOUT"
        and event.get("funding_self_pct") is not None
        and event["funding_self_pct"] <= FILTER_CUT
    ]

    tape_rows = load_tape_rows(tape_db, cutoff_ms)
    aggregates = coin_impact_aggregates(tape_rows)
    tiers = build_liquidity_tiers(tape_rows)
    original = funding_study.summ(selected, "residual_full_bps", "cap24/holdout_selected")

    charged_results = {}
    charged_sets = {}
    for label, field in (
        ("tier_median", "spread_median_bps"),
        ("tier_p75", "spread_p75_bps"),
        ("tier_p90", "spread_p90_bps"),
    ):
        charged, uncovered = charge_events(selected, series, tiers, spread_field=field)
        charged_sets[label] = charged
        summary = funding_study.summ(
            charged, "impact_recharged_residual_bps", f"cap24/impact/{label}"
        )
        charged_results[label] = {
            "summary": summary,
            "coverage": len(charged) / len(selected) if selected else 0.0,
            "uncovered_n": len(uncovered),
            "uncovered": uncovered,
            "event_weighted_impact_bps": (
                st.fmean(event["assigned_impact_bps"] for event in charged) if charged else None
            ),
            "events_by_liquidity_tier": dict(sorted(collections.Counter(
                event["liquidity_tier"] for event in charged
            ).items())),
        }

    uniform_sensitivity = {}
    for cost in (30.0, 40.0, 50.0, 55.0, 60.0, 75.0, 100.0, 150.0):
        charged = [recharge_event(event, cost) for event in selected]
        uniform_sensitivity[str(int(cost))] = funding_study.summ(
            charged, "impact_recharged_residual_bps", f"cap24/impact/uniform_{int(cost)}bps"
        )

    current_cost = {row["coin"]: row["median_spread_bps"] for row in aggregates}
    same_coin, same_coin_missing = [], []
    for event in selected:
        cost = current_cost.get(event["coin"])
        if cost is None:
            same_coin_missing.append(event["coin"])
        else:
            same_coin.append(recharge_event(event, cost))
    same_coin_result = {
        "diagnostic_only": True,
        "coverage": len(same_coin) / len(selected) if selected else 0.0,
        "missing_coins": sorted(set(same_coin_missing)),
        "event_weighted_impact_bps": (
            st.fmean(event["assigned_impact_bps"] for event in same_coin) if same_coin else None
        ),
        "summary": funding_study.summ(
            same_coin, "impact_recharged_residual_bps", "cap24/impact/current_same_coin"
        ),
    }

    p75 = charged_results["tier_p75"]
    decision_result = decision({
        "coverage": p75["coverage"],
        "min_tier_coins": min((tier["coins"] for tier in tiers), default=0),
        "p75_ci_low": p75["summary"].get("ci_low", float("-inf")),
    })
    valid_rows = sum(
        1 for row in tape_rows
        if valid_spread(row) is not None
        and row.get("day_ntl_vlm") is not None
        and float(row["day_ntl_vlm"]) > 0
    )
    paths = {
        "manifest": cache_dir / "manifest.json",
        "split": study_dir / "split.v1.json",
        "funding_result": study_dir / "funding.v1.json",
        "funding_signal_result": study_dir / "funding_signal.v1.json",
        "impact_prereg": study_dir / "PREREG.impact.v1.md",
        "impact_analyzer": Path(__file__).resolve(),
    }
    report = {
        "status": "survives_bounded_proxy" if decision_result["pass"] else "killed_by_impact_cost",
        "authority": "DISCOVERY_ONLY_NO_ARM_NO_FUNDING",
        "source_commit": SOURCE_COMMIT,
        "impact_prereg_commit": PREREG_COMMIT,
        "tape": {
            "db": str(Path(tape_db).resolve()),
            "cutoff_ms": int(cutoff_ms),
            "rows": len(tape_rows),
            "valid_rows": valid_rows,
            "coins": len(aggregates),
            "mark_times": len({row["ts_ms"] for row in tape_rows}),
            "first_ts_ms": min((row["ts_ms"] for row in tape_rows), default=None),
            "last_ts_ms": max((row["ts_ms"] for row in tape_rows), default=None),
            "canonical_rows_sha256": _canonical_rows_sha256(tape_rows),
        },
        "candidate": {
            "cap_h": 24,
            "funding_self_pct_side": "<=",
            "funding_self_pct_cut": FILTER_CUT,
            "split": "HOLDOUT",
            "selected_events": len(selected),
            "selected_coins": len({event["coin"] for event in selected}),
            "old_total_cost_bps": 39.0,
            "old_slippage_replaced_bps": OLD_SLIPPAGE_BPS,
            "fees_retained_bps": 9.0,
            "original": original,
        },
        "impact_definition": {
            "formula": "(impact_ask-impact_bid)/mid_px*10000",
            "side_count": 2,
            "doubled": False,
            "percentile_method": "linear_(n-1)*q",
        },
        "liquidity_tiers": tiers,
        "recharged": charged_results,
        "uniform_impact_sensitivity_diagnostic": uniform_sensitivity,
        "current_same_coin": same_coin_result,
        "decision": decision_result,
        "limitations": [
            "impactPxs is a fixed provider probe, not a declared strategy order notional",
            "same-state bid-to-ask depth does not reconstruct the exit book up to 24h later",
            "the tape spans only the frozen interval and not the historical event timestamps",
            "stop fills remain modeled at the stop and exclude gap-through tail loss",
            "the holdout was previously read; this result remains discovery-grade",
        ],
        "provenance_sha256": {name: _sha256(path) for name, path in paths.items()},
        "funding_cache_tree_sha256": _funding_tree_sha256(cache_dir),
    }
    return _round_tree(report)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--tape-db", required=True)
    parser.add_argument("--cutoff-ms", type=int, default=DEFAULT_CUTOFF_MS)
    parser.add_argument("--study-dir", default="data/research/hl_squeeze_study")
    parser.add_argument("--output", default="data/research/hl_squeeze_study/impact_cost.v1.json")
    args = parser.parse_args(argv)
    report = run_study(
        cache_dir=args.cache_dir,
        tape_db=args.tape_db,
        cutoff_ms=args.cutoff_ms,
        study_dir=args.study_dir,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    primary = report["recharged"]["tier_p75"]
    summary = primary["summary"]
    print(
        f"{report['status']}: n={summary.get('n', 0)} "
        f"mean={summary.get('mean_bps')} "
        f"CI=[{summary.get('ci_low')},{summary.get('ci_high')}] "
        f"impact={primary.get('event_weighted_impact_bps')}bps"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
