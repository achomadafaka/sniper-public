"""Versioned, advisory-only facts for attempted real memecoin entries."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone


RATING_VERSION = 1
LATENCY_WARNING_S = 60.0
MCAP_WARNING_USD = 30_000_000.0

_RED_TERMINAL_STATUSES = {
    "safety_failed", "route_failed", "quote_failed", "rejected", "aborted", "error",
}
_RED_REASON_TOKENS = (
    "unresolved_network", "ambiguous_network", "network_mint_mismatch",
    "unsupported_or_unarmed_network", "unroutable_chain", "dropped_network",
    "evm_unarmed", "skipped_chase", "skipped_mcap", "above_cap", "stale",
    "unsafe", "disarmed", "rejected", "no route", "route_failed", "quote_failed",
)


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _parse(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _elapsed(start, end, multiplier=1.0):
    a, b = _parse(start), _parse(end)
    if a is None or b is None:
        return None
    value = (b - a).total_seconds() * multiplier
    return value if value >= 0 else None


def _existing_refusal(facts):
    status = str(facts.get("terminal_status") or "").strip().lower()
    reason = str(facts.get("terminal_reason") or "").strip().lower()
    if reason == "not_approved":
        return None
    if status in _RED_TERMINAL_STATUSES:
        return reason or status
    if status == "refused" and any(token in reason for token in _RED_REASON_TOKENS):
        return reason or status
    return None


def rate_entry_facts(facts, *, chase_cap_pct=None):
    """Return `(rating, deterministic_reason_codes)` without changing a decision."""
    if chase_cap_pct is None:
        from sniper.settings.memecoin import MEMECOIN_REAL_CHASE_MAX_PCT
        chase_cap_pct = MEMECOIN_REAL_CHASE_MAX_PCT

    refusal = _existing_refusal(facts)
    red = [f"existing_refusal:{refusal}"] if refusal else []
    yellow = []
    if str(facts.get("terminal_reason") or "").strip().lower() == "not_approved":
        yellow.append("operator_not_approved")
    claimed_age = _finite(facts.get("claimed_source_age_s"))
    if claimed_age is not None and claimed_age >= LATENCY_WARNING_S:
        yellow.append("claimed_source_age_ge_60s_untrusted")
    local_latency = _finite(facts.get("observed_to_decision_ms"))
    if local_latency is not None and local_latency >= LATENCY_WARNING_S * 1000.0:
        yellow.append("observed_to_decision_ge_60s")
    mcap = _positive(facts.get("entry_mcap"))
    if mcap is not None and mcap >= MCAP_WARNING_USD:
        yellow.append("entry_mcap_ge_30m")
    runup = _finite(facts.get("episode_executable_runup_pct"))
    if (runup is not None and chase_cap_pct is not None
            and runup >= float(chase_cap_pct)):
        yellow.append("episode_runup_ge_chase_cap")

    required = ["source_price", "quote_price", "quote_age_ms", "quote_id", "entry_mcap",
                "entry_liquidity", "observed_at"]
    if str(facts.get("terminal_status") or "").strip().lower() == "filled":
        required.append("fill_price")
    for field in required:
        value = facts.get(field)
        missing = (value is None
                   or (field == "quote_id" and not str(value).strip())
                   or (field in {"source_price", "quote_price", "entry_mcap",
                                 "entry_liquidity"} and _positive(value) is None))
        if missing:
            yellow.append(f"missing_{field}")

    if red:
        return "red", red
    return ("yellow", yellow) if yellow else ("green", [])


def _snapshot_for_event(conn, event_id):
    if not event_id:
        return None
    row = conn.execute(
        "SELECT * FROM memecoin_entry_snapshots WHERE upstream_event_id=?",
        (event_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _json_object(value):
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def attempt_facts(conn, attempt_id):
    row = conn.execute(
        "SELECT * FROM memecoin_entry_attempts WHERE attempt_id=?", (attempt_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown entry attempt: {attempt_id}")
    attempt = dict(row)
    snapshot = _snapshot_for_event(conn, attempt.get("origin_event_id"))
    features = _json_object((snapshot or {}).get("features_json"))
    source_price = _positive(attempt.get("source_price"))
    quote_price = _positive(attempt.get("quote_price"))
    source_drift = _finite(attempt.get("source_drift_pct"))
    if source_drift is None and source_price is not None and quote_price is not None:
        source_drift = (quote_price / source_price - 1.0) * 100.0
    observed_at = attempt.get("observed_at")
    decision_at = attempt.get("decision_started_at")
    clock_domain = attempt.get("clock_domain") or "legacy_unknown"
    facts = {
        "attempt_id": attempt_id,
        "upstream_event_id": attempt.get("origin_event_id"),
        "snapshot_id": (snapshot or {}).get("snapshot_id"),
        "episode_id": (snapshot or {}).get("episode_id"),
        "terminal_status": attempt.get("terminal_status"),
        "terminal_reason": attempt.get("terminal_reason"),
        "network_id": attempt.get("network_id"),
        "mint": attempt.get("mint"),
        "signal_source": attempt.get("signal_source"),
        "source_user_id": attempt.get("source_user_id"),
        "source_timestamp_trust": attempt.get("source_timestamp_trust"),
        "source_created_at": attempt.get("source_created_at"),
        "observed_at": observed_at,
        "decision_started_at": decision_at,
        "clock_domain": clock_domain,
        "claimed_source_age_s": _elapsed(attempt.get("source_created_at"), observed_at),
        "observed_to_decision_ms": (
            _elapsed(observed_at, decision_at, 1000.0)
            if clock_domain == "observed_real_utc" else None
        ),
        "source_price": source_price,
        "quote_price": quote_price,
        "quote_observed_at": attempt.get("quote_observed_at"),
        "quote_age_ms": _finite(attempt.get("quote_age_ms")),
        "quote_id": attempt.get("quote_id"),
        "quote_route_target": attempt.get("quote_route_target"),
        "source_drift_pct": source_drift,
        "fill_price": _positive(attempt.get("fill_price")),
        "execution_slippage_pct": _finite(attempt.get("execution_slippage_pct")),
        "total_copy_drift_pct": _finite(attempt.get("total_copy_drift_pct")),
        "entry_mcap": (_positive((snapshot or {}).get("mcap"))
                       or _positive(attempt.get("source_market_cap"))),
        "entry_liquidity": _positive((snapshot or {}).get("liquidity")),
        "consensus_n_60m": (snapshot or {}).get("consensus_n_60m"),
        "episode_entry_sequence": features.get("episode_entry_sequence"),
        "episode_source_runup_pct": _finite(features.get("episode_source_runup_pct")),
        "episode_executable_runup_pct": _finite(
            features.get("episode_executable_runup_pct")),
        "coin_phase": (
            "protected_hold_shadow" if attempt.get("terminal_status") == "filled"
            else "owned_unconfirmed" if attempt.get("tx_sig") else "watching"
        ),
    }
    return facts


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def record_attempt_rating(conn, attempt_id):
    """Insert one immutable rating for a terminal real attempt."""
    facts = attempt_facts(conn, attempt_id)
    if not facts.get("terminal_status"):
        raise ValueError(f"entry attempt is not terminal: {attempt_id}")
    rating, reasons = rate_entry_facts(facts)
    facts_json = _canonical_json(facts)
    facts_hash = hashlib.sha256(facts_json.encode()).hexdigest()
    existing = conn.execute(
        "SELECT * FROM memecoin_radar_ratings WHERE attempt_id=?", (attempt_id,),
    ).fetchone()
    if existing is not None:
        if existing["facts_hash"] != facts_hash:
            raise ValueError(f"immutable radar collision for attempt {attempt_id}")
        return dict(existing)
    created_at = conn.execute(
        "SELECT completed_at FROM memecoin_entry_attempts WHERE attempt_id=?", (attempt_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO memecoin_radar_ratings(attempt_id, upstream_event_id, snapshot_id, "
        "episode_id, network_id, mint, signal_source, rating_version, rating, "
        "reason_codes_json, facts_json, facts_hash, decided_at, created_at, clock_domain) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (attempt_id, facts.get("upstream_event_id"), facts.get("snapshot_id"),
         facts.get("episode_id"), facts.get("network_id"), facts.get("mint"),
         facts.get("signal_source"), RATING_VERSION, rating, _canonical_json(reasons),
         facts_json, facts_hash, facts.get("decision_started_at"), created_at,
         facts.get("clock_domain") or "legacy_unknown"),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM memecoin_radar_ratings WHERE attempt_id=?", (attempt_id,),
    ).fetchone()
    return dict(row)
