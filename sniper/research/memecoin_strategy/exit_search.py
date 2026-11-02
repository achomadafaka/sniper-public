from __future__ import annotations

import math
import hashlib
import json
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sniper.research.wallet_execution.publication import diagnostic_active
from .models import CandidateSpec
from .store import enroll_candidate


TP_VALUES = range(20, 201, 20)
SL_VALUES = range(20, 81, 10)
STOP_CONFIRM_VALUES = (1, 2, 4)
TIMECAP_VALUES = (3600, 21600, 43200, 86400)


@dataclass(frozen=True)
class GateResult:
    passed: bool
    distinct_days: int
    complete_paths: int
    distinct_coins: int
    coverage: float
    budget_skipped: int
    deficits: dict


@dataclass(frozen=True)
class ExitResult:
    valid: bool
    reason: str
    exit_price: float | None
    exited_at: str | None
    gross_pnl: float | None
    net_pnl: float | None
    net_return: float | None


@dataclass(frozen=True)
class SearchResult:
    status: str
    gate: GateResult
    selected_policy: dict | None = None
    train_mean: float | None = None
    forward_mean: float | None = None
    baseline_forward_mean: float | None = None
    train_end: str | None = None
    forward_start: str | None = None
    strata: dict | None = None
    spawned_candidate: object | None = None
    search_read_id: str | None = None


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("exit-search timestamps must be timezone aware")
    return parsed


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 and math.isfinite(value) else None


def data_gate(paths, coverage):
    # This legacy loader supplies sampled nominal marks, never the new protocol.
    complete = [path for path in paths if path.get("complete")]
    days = {_time(path["signal_at"]).astimezone(timezone.utc).date().isoformat()
            for path in complete}
    coins = {(str(path.get("network_id") or ""), str(path.get("mint") or ""))
             for path in complete if path.get("network_id") and path.get("mint")}
    try:
        expected = int(coverage.get("expected", 0))
        observed = int(coverage.get("observed", 0))
        budget_skipped = int(coverage.get("budget_skipped", 0))
    except (TypeError, ValueError, AttributeError):
        expected = observed = budget_skipped = 0
    ratio = observed / expected if expected > 0 and observed >= 0 else 0.0
    requirements = {
        "distinct_days": (len(days), 21),
        "complete_paths": (len(complete), 200),
        "distinct_coins": (len(coins), 100),
        "coverage": (ratio, 0.90),
    }
    deficits = {
        name: {"have": have, "need": need, "short": max(0, need - have)}
        for name, (have, need) in requirements.items() if have < need
    }
    deficits["protocol_missing_or_legacy"] = {"have": 0, "need": 1, "short": 1}
    return GateResult(
        passed=False, distinct_days=len(days), complete_paths=len(complete),
        distinct_coins=len(coins), coverage=ratio, budget_skipped=max(0, budget_skipped),
        deficits=deficits,
    )


def _cost_rate(costs, name):
    try:
        value = float((costs or {}).get(name, 0))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be finite and nonnegative") from None
    if value < 0 or not math.isfinite(value):
        raise ValueError(f"{name} must be finite and nonnegative")
    return value / 10_000.0


def _result(path, price, exited_at, reason, costs):
    entry = _positive(path.get("entry_price"))
    notional = _positive(path.get("notional", 100))
    if entry is None or notional is None:
        return ExitResult(False, "invalid_entry", None, None, None, None, None)
    proceeds = notional / entry * price
    gross = proceeds - notional
    net = gross - notional * _cost_rate(costs, "entry_bps") - proceeds * _cost_rate(costs, "exit_bps")
    return ExitResult(True, reason, price, exited_at, gross, net, net / notional)


def simulate_exit_path(path, policy, costs):
    if not diagnostic_active():
        return ExitResult(False, "protocol_missing_or_legacy", None, None, None, None, None)
    if not path.get("complete"):
        return ExitResult(False, "incomplete_path", None, None, None, None, None)
    entry = _positive(path.get("entry_price"))
    if entry is None:
        return ExitResult(False, "invalid_entry", None, None, None, None, None)
    try:
        tp = float(policy["tp_pct"])
        stop = float(policy["sl_pct"])
        confirms = int(policy["sl_confirm"])
        timecap = int(policy["timecap_s"])
    except (KeyError, TypeError, ValueError):
        return ExitResult(False, "invalid_policy", None, None, None, None, None)
    if tp <= 0 or stop <= 0 or confirms <= 0 or timecap <= 0:
        return ExitResult(False, "invalid_policy", None, None, None, None, None)
    signal_at = _time(path["signal_at"])
    deadline = signal_at + timedelta(seconds=timecap)
    quotes = []
    for quote in path.get("observations") or []:
        if quote.get("quality") != "ok":
            continue
        price = _positive(quote.get("price"))
        if price is None:
            continue
        at = _time(quote.get("observed_at"))
        if signal_at <= at <= deadline:
            # Lowest same-timestamp price first is conservative when source ordering is unknowable.
            quotes.append((at, price, int(quote.get("id") or 0), quote))
    quotes.sort(key=lambda item: (item[0], item[1], item[2]))
    stop_hits = 0
    last_price = entry
    for at, price, _observation_id, _quote in quotes:
        last_price = price
        gain = (price / entry - 1.0) * 100.0
        if gain >= tp:
            return _result(path, price, at.isoformat(), "tp", costs)
        if gain <= -stop:
            stop_hits += 1
        else:
            stop_hits = 0
        if stop_hits >= confirms:
            return _result(path, price, at.isoformat(), "stop", costs)
    return _result(path, last_price, deadline.isoformat(), "timecap", costs)


def _grid():
    for tp in TP_VALUES:
        for stop in SL_VALUES:
            for confirms in STOP_CONFIRM_VALUES:
                for timecap in TIMECAP_VALUES:
                    yield {"tp_pct": tp, "sl_pct": stop, "sl_confirm": confirms,
                           "timecap_s": timecap, "mirror": False}


def _score(paths, policy, costs):
    results = [simulate_exit_path(path, policy, costs) for path in paths]
    returns = [result.net_return for result in results if result.valid]
    return statistics.fmean(returns) if returns else float("-inf")


def _select(paths, costs):
    best_policy = None
    best_score = float("-inf")
    for policy in _grid():
        score = _score(paths, policy, costs)
        if score > best_score:
            best_policy, best_score = policy, score
    return best_policy, best_score


def _chronological_split(paths):
    ordered = sorted(paths, key=lambda path: (_time(path["signal_at"]), path.get("path_id", "")))
    days = sorted({_time(path["signal_at"]).date().isoformat() for path in ordered})
    train_day_count = max(1, min(len(days) - 1, int(len(days) * 2 / 3)))
    train_days = set(days[:train_day_count])
    train = [path for path in ordered if _time(path["signal_at"]).date().isoformat() in train_days]
    forward = [path for path in ordered if _time(path["signal_at"]).date().isoformat() not in train_days]
    return train, forward


def _record_search_read(conn, candidate_id, asof, status, gate, paths, payload):
    evidence = [{
        "path_id": path.get("path_id"), "network_id": path.get("network_id"),
        "mint": path.get("mint"), "signal_at": path.get("signal_at"),
        "entry_price": path.get("entry_price"), "complete": bool(path.get("complete")),
        "observations": path.get("observations") or [],
    } for path in paths]
    evidence_json = json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()
    manifest = {"status": status, "gate": asdict(gate), "asof": asof,
                "evidence_hash": evidence_hash, **payload}
    manifest_json = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    manifest_hash = hashlib.sha256(manifest_json.encode()).hexdigest()
    read_id = "exit-" + hashlib.sha256(
        f"{candidate_id}|{asof}|{manifest_hash}".encode(),
    ).hexdigest()[:24]
    conn.execute(
        "INSERT OR IGNORE INTO memecoin_candidate_reads(read_id,candidate_id,read_type,asof,"
        "manifest_json,evidence_hash,created_at) VALUES (?,?,'exit_search',?,?,?,?)",
        (read_id, candidate_id, asof, manifest_json, evidence_hash, asof),
    )
    conn.commit()
    return read_id


def walk_forward_search(paths, candidate, asof, *, coverage, conn=None):
    gate = data_gate(paths, coverage)
    if not gate.passed:
        read_id = (_record_search_read(
            conn, candidate.candidate_id, asof, "accruing", gate, paths,
            {"selected_policy": None},
        ) if conn is not None else None)
        return SearchResult(status="accruing", gate=gate, strata={}, spawned_candidate=None,
                            search_read_id=read_id)
    # This legacy loader supplies sampled nominal marks, never the new protocol.
    complete = [path for path in paths if path.get("complete")]
    train, forward = _chronological_split(complete)
    if not train or not forward:
        return SearchResult(status="accruing", gate=gate,
                            strata={}, spawned_candidate=None)
    selected, train_mean = _select(train, candidate.costs)
    forward_mean = _score(forward, selected, candidate.costs)
    baseline_forward = _score(forward, candidate.exit_policy, candidate.costs)
    strata = {}
    bands = sorted({str(path.get("market_cap_band") or "unknown") for path in complete})
    for band in bands:
        band_train = [path for path in train if str(path.get("market_cap_band") or "unknown") == band]
        if len(band_train) < 50:
            policy, shrunk = selected, True
        else:
            policy, _ = _select(band_train, candidate.costs)
            shrunk = False
        strata[band] = {"train_paths": len(band_train), "policy": policy,
                        "shrunk_to_global": shrunk}
    survived = forward_mean > 0 and forward_mean > baseline_forward
    status = "survived" if survived else "rejected"
    read_payload = {
        "selected_policy": selected, "train_mean": train_mean,
        "forward_mean": forward_mean, "baseline_forward_mean": baseline_forward,
        "train_end": max(_time(path["signal_at"]) for path in train).isoformat(),
        "forward_start": min(_time(path["signal_at"]) for path in forward).isoformat(),
        "strata": strata,
    }
    read_id = (_record_search_read(
        conn, candidate.candidate_id, asof, status, gate, paths, read_payload,
    ) if conn is not None else None)
    spawned = None
    if survived and conn is not None:
        spec = CandidateSpec(
            name=f"{candidate.name}_exitsearch_{asof[:10]}",
            predicate=candidate.predicate, exit_policy=selected, sizing=candidate.sizing,
            costs=candidate.costs, controls=candidate.controls, gates=candidate.gates,
            discovery_cutoff=asof, capture_version_floor=candidate.capture_version_floor,
            generation=candidate.generation + 1, parent_id=candidate.candidate_id,
            notes=f"Auto-enrolled from locked exit-search read {read_id}; paper only.",
        )
        spawned = enroll_candidate(conn, spec, asof)
    return SearchResult(
        status=status, gate=gate,
        selected_policy=selected, train_mean=train_mean, forward_mean=forward_mean,
        baseline_forward_mean=baseline_forward,
        train_end=max(_time(path["signal_at"]) for path in train).isoformat(),
        forward_start=min(_time(path["signal_at"]) for path in forward).isoformat(),
        strata=strata, spawned_candidate=spawned, search_read_id=read_id,
    )
