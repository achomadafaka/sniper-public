from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping

from .exit_search import simulate_exit_path
from .pattern_context import ReentrySignal
from .pattern_paths import analyze_path


POLICIES = {
    "tp60_sl50x4_24h": {
        "kind": "full", "tp_pct": 60, "sl_pct": 50, "sl_confirm": 4,
        "timecap_s": 86400,
    },
    "tp60_sl40x4_24h": {
        "kind": "full", "tp_pct": 60, "sl_pct": 40, "sl_confirm": 4,
        "timecap_s": 86400,
    },
    "tp100_sl50x4_24h": {
        "kind": "full", "tp_pct": 100, "sl_pct": 50, "sl_confirm": 4,
        "timecap_s": 86400,
    },
    "principal_at_60_runner_150": {
        "kind": "principal_runner", "principal_pct": 60, "runner_tp_pct": 150,
        "sl_pct": 50, "sl_confirm": 4, "timecap_s": 86400,
    },
    "stop_then_fresh_conviction_reentry": {
        "kind": "reentry", "tp_pct": 60, "sl_pct": 50, "sl_confirm": 4,
        "timecap_s": 86400, "reentry_tp_pct": 100, "reentry_sl_pct": 40,
        "reentry_sl_confirm": 4, "reentry_timecap_s": 43200,
    },
    "timecap_6h": {
        "kind": "full", "tp_pct": 1_000_000_000, "sl_pct": 50,
        "sl_confirm": 4, "timecap_s": 21600,
    },
    "timecap_24h": {
        "kind": "full", "tp_pct": 1_000_000_000, "sl_pct": 50,
        "sl_confirm": 4, "timecap_s": 86400,
    },
}


@dataclass(frozen=True)
class PolicyResult:
    policy_name: str
    valid: bool
    reason: str
    exit_price: float | None
    exited_at: str | None
    capital_deployed: float | None
    gross_pnl: float | None
    net_pnl: float | None
    net_return: float | None
    legs: tuple[dict, ...]


def _time(value) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("policy timestamps must be timezone aware")
    return parsed


def _rate(bps, name: str) -> float:
    try:
        value = float(bps)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be finite and nonnegative") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return value / 10_000.0


def _invalid(policy_name: str, reason: str) -> PolicyResult:
    return PolicyResult(policy_name, False, reason, None, None, None, None, None, None, ())


def _full_result(path, policy_name, policy, entry_bps, exit_bps) -> PolicyResult:
    result = simulate_exit_path(
        path, policy, {"entry_bps": entry_bps, "exit_bps": exit_bps},
    )
    if not result.valid:
        return _invalid(policy_name, result.reason)
    notional = float(path.get("notional", 100))
    quantity = notional / float(path["entry_price"])
    proceeds = quantity * float(result.exit_price)
    return PolicyResult(
        policy_name=policy_name, valid=True, reason=result.reason,
        exit_price=result.exit_price, exited_at=result.exited_at,
        capital_deployed=notional, gross_pnl=result.gross_pnl, net_pnl=result.net_pnl,
        net_return=result.net_return,
        legs=({
            "reason": result.reason, "entry_price": float(path["entry_price"]),
            "price": result.exit_price, "quantity": quantity, "proceeds": proceeds,
            "exited_at": result.exited_at,
        },),
    )


def _principal_runner(path, policy_name, policy, entry_bps, exit_bps) -> PolicyResult:
    if not path.get("complete"):
        return _invalid(policy_name, "incomplete_path")
    try:
        entry = float(path["entry_price"])
        notional = float(path.get("notional", 100))
    except (KeyError, TypeError, ValueError):
        return _invalid(policy_name, "invalid_entry")
    if not math.isfinite(entry) or entry <= 0 or not math.isfinite(notional) or notional <= 0:
        return _invalid(policy_name, "invalid_entry")
    metrics = analyze_path(entry, path["signal_at"], path.get("observations") or [], complete=True)
    signal_at = _time(path["signal_at"])
    deadline = signal_at + timedelta(seconds=policy["timecap_s"])
    quotes = [quote for quote in metrics.observations if quote.observed_at <= deadline]
    quantity = notional / entry
    remaining = quantity
    stop_hits = 0
    derisked = False
    legs = []
    last_price = entry
    last_at = deadline
    final_reason = "timecap"
    for quote in quotes:
        price = quote.price
        last_price = price
        last_at = quote.observed_at
        gain = (price / entry - 1.0) * 100.0
        stop_hits = stop_hits + 1 if gain <= -policy["sl_pct"] else 0
        if stop_hits >= policy["sl_confirm"]:
            proceeds = remaining * price
            legs.append({
                "reason": "runner_stop" if derisked else "stop", "entry_price": entry,
                "price": price, "quantity": remaining, "proceeds": proceeds,
                "exited_at": quote.observed_at.isoformat(),
            })
            remaining = 0.0
            final_reason = legs[-1]["reason"]
            break
        if not derisked and gain >= policy["principal_pct"]:
            sold = min(remaining, notional / price)
            legs.append({
                "reason": "principal", "entry_price": entry, "price": price,
                "quantity": sold, "proceeds": sold * price,
                "exited_at": quote.observed_at.isoformat(),
            })
            remaining -= sold
            derisked = True
        if derisked and remaining > 0 and gain >= policy["runner_tp_pct"]:
            legs.append({
                "reason": "runner_tp", "entry_price": entry, "price": price,
                "quantity": remaining, "proceeds": remaining * price,
                "exited_at": quote.observed_at.isoformat(),
            })
            remaining = 0.0
            final_reason = "runner_tp"
            break
    if remaining > 0:
        legs.append({
            "reason": "timecap", "entry_price": entry, "price": last_price,
            "quantity": remaining, "proceeds": remaining * last_price,
            "exited_at": deadline.isoformat(),
        })
        final_reason = "timecap"

    proceeds = sum(leg["proceeds"] for leg in legs)
    gross = proceeds - notional
    net = gross - notional * _rate(entry_bps, "entry_bps") - proceeds * _rate(exit_bps, "exit_bps")
    return PolicyResult(
        policy_name, True, final_reason, legs[-1]["price"], legs[-1]["exited_at"],
        notional, gross, net, net / notional, tuple(legs),
    )


def _reentry_result(path, policy_name, policy, entry_bps, exit_bps, reentry) -> PolicyResult:
    baseline_policy = {
        "tp_pct": policy["tp_pct"], "sl_pct": policy["sl_pct"],
        "sl_confirm": policy["sl_confirm"], "timecap_s": policy["timecap_s"],
    }
    baseline = _full_result(path, policy_name, baseline_policy, entry_bps, exit_bps)
    if not baseline.valid or baseline.reason != "stop" or reentry is None:
        return baseline
    if _time(reentry.entry_at) < _time(baseline.exited_at):
        return _invalid(policy_name, "reentry_before_stop")
    observations = [
        row for row in path.get("observations") or []
        if _time(row.get("observed_at")) >= _time(reentry.entry_at)
    ]
    reentry_path = {
        "path_id": f"{path.get('path_id', '')}:reentry",
        "network_id": path.get("network_id"), "mint": path.get("mint"),
        "signal_at": reentry.entry_at, "entry_price": reentry.entry_price,
        "notional": float(path.get("notional", 100)), "observations": observations,
        "complete": True,
    }
    reentry_policy = {
        "tp_pct": policy["reentry_tp_pct"], "sl_pct": policy["reentry_sl_pct"],
        "sl_confirm": policy["reentry_sl_confirm"],
        "timecap_s": policy["reentry_timecap_s"],
    }
    second = _full_result(reentry_path, policy_name, reentry_policy, entry_bps, exit_bps)
    if not second.valid:
        return _invalid(policy_name, second.reason)
    capital = float(baseline.capital_deployed) + float(second.capital_deployed)
    gross = float(baseline.gross_pnl) + float(second.gross_pnl)
    net = float(baseline.net_pnl) + float(second.net_pnl)
    second_leg = dict(second.legs[0])
    second_leg["reason"] = f"reentry_{second.reason}"
    return PolicyResult(
        policy_name, True, f"reentry_{second.reason}", second.exit_price,
        second.exited_at, capital, gross, net, net / capital,
        baseline.legs + (second_leg,),
    )


def simulate_policy(
    path: Mapping,
    policy_name: str,
    *,
    entry_bps: float,
    exit_bps: float,
    reentry: ReentrySignal | None = None,
) -> PolicyResult:
    if policy_name not in POLICIES:
        raise ValueError(f"unknown policy: {policy_name}")
    policy = POLICIES[policy_name]
    if policy["kind"] == "principal_runner":
        return _principal_runner(path, policy_name, policy, entry_bps, exit_bps)
    if policy["kind"] == "reentry":
        return _reentry_result(path, policy_name, policy, entry_bps, exit_bps, reentry)
    return _full_result(path, policy_name, policy, entry_bps, exit_bps)


def match_past_control(case: Mapping, candidates: Iterable[Mapping]):
    case_time = _time(case["signal_at"])
    fields = ("lane", "network_id", "market_cap_band", "liquidity_band", "initial_15m_return_band")
    choices = []
    for candidate in candidates:
        if any(candidate.get(field) != case.get(field) for field in fields):
            continue
        if candidate.get("mint") == case.get("mint"):
            continue
        if str(candidate.get("archetype") or "").startswith("deep_recovery"):
            continue
        candidate_time = _time(candidate["signal_at"])
        if candidate_time > case_time:
            continue
        age = (case_time - candidate_time).total_seconds()
        choices.append((age, str(candidate.get("episode_id") or ""), candidate))
    return min(choices, key=lambda item: (item[0], item[1]))[2] if choices else None


def _percentile(values, fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def rank_policies(rows: Iterable[Mapping], *, baseline_name: str, minimum_coins: int = 20) -> dict:
    grouped: dict[str, list[Mapping]] = {}
    for row in rows:
        try:
            value = float(row.get("net_return"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        grouped.setdefault(str(row.get("policy_name")), []).append({**row, "net_return": value})
    summaries = {}
    for name, policy_rows in sorted(grouped.items()):
        values = [row["net_return"] for row in policy_rows]
        summaries[name] = {
            "episodes": len(values),
            "distinct_coins": len({str(row.get("mint") or "") for row in policy_rows if row.get("mint")}),
            "mean": statistics.fmean(values),
            "median": statistics.median(values),
            "p10": _percentile(values, 0.10),
        }
    from sniper.research.wallet_execution.publication import legacy_eligibility
    for summary in summaries.values():
        summary['eligible'] = False
    return {'status': 'blocked_protocol', 'winner': None, 'policies': summaries,
            'research_eligibility': legacy_eligibility()}
