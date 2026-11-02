import json
import os
import re
import sys
import time
from typing import Any, Dict, List

# ── World context boost cache ──────────────────────────────────────────────────
# Written by scripts/world_analyst.py → Supabase market_boosts_current.
# Applied to priority_score so world-confirmed signals surface higher in the UI.
_PIPELINE_BOOSTS_CACHE: dict = {}
_PIPELINE_BOOSTS_CACHE_TIME: float = 0.0
_PIPELINE_BOOSTS_TTL: float = 900.0  # 15 minutes


def _pipeline_load_world_boosts() -> dict:
    """Load market_boosts_current from Supabase. 15-min cached. Silent on failure."""
    global _PIPELINE_BOOSTS_CACHE, _PIPELINE_BOOSTS_CACHE_TIME
    now = time.time()
    if now - _PIPELINE_BOOSTS_CACHE_TIME < _PIPELINE_BOOSTS_TTL:
        return _PIPELINE_BOOSTS_CACHE
    try:
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from bot.supabase_helper import get_memory
        entry = get_memory("market_boosts_current", type_filter="knowledge")
        if entry:
            raw = entry.get("content", "{}")
            boosts = json.loads(raw) if isinstance(raw, str) else {}
            _PIPELINE_BOOSTS_CACHE = boosts
    except Exception:
        pass
    _PIPELINE_BOOSTS_CACHE_TIME = now
    return _PIPELINE_BOOSTS_CACHE


_ELIGIBILITY_BLOCKER_KEYS = {
    "insider_score_gte_threshold",
    "emerging_score_gte_threshold",
    "repeat_hits_ok",
    "repeat_or_position_signal_ok",
    "positions_ok",
    "pnl_ok",
    "top_open_value_ok",
    "total_open_value_ok",
    "max_position_ok",
    "serious_trades_ok",
    "serious_or_position_signal_ok",
    "serious_notional_ok",
    "position_signal_strength_ok",
    "position_speculator_guard_ok",
    "emerging_top_open_value_ok",
    "emerging_total_open_value_ok",
}

_RE_REASON_CHECK = re.compile(r"^([a-z0-9_]+)=(yes|no)$", re.IGNORECASE)


def eligibility_blockers_from_reason(reason: str | None) -> List[str]:
    raw = str(reason or "").strip()
    if not raw:
        return []
    blockers: List[str] = []
    for token in raw.split(","):
        part = token.strip()
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key not in _ELIGIBILITY_BLOCKER_KEYS:
            continue
        if value.strip().lower() in {"no", "false", "0"}:
            blockers.append(key)
    return blockers


def open_signal_is_focused(
    item: Dict[str, Any],
    *,
    min_insider_score: float,
    min_speculator_score: float,
    min_position_value: float,
    min_abs_net_notional_24h: float,
    safe_float,
    safe_int,
) -> bool:
    if bool(item.get("is_followed_position")):
        return True

    classification = str(item.get("position_classification") or item.get("classification") or "").strip().lower()
    insider_score = safe_float(item.get("position_insider_score"), safe_float(item.get("insider_score")))
    speculator_score = safe_float(item.get("position_speculator_score"), safe_float(item.get("speculator_score")))
    position_value = safe_float(item.get("position_value"))
    trades_24h = safe_int(item.get("trades_24h"))
    buys_24h = safe_int(item.get("buys_24h"))
    sells_24h = safe_int(item.get("sells_24h"))
    abs_net_notional_24h = abs(safe_float(item.get("net_notional_24h")))

    if classification and classification != "position_low_signal":
        return True
    has_activity_24h = trades_24h > 0 or buys_24h > 0 or sells_24h > 0
    has_score_signal = insider_score >= min_insider_score or speculator_score >= min_speculator_score
    has_flow_signal = abs_net_notional_24h >= min_abs_net_notional_24h
    has_size_signal = position_value >= min_position_value
    return has_size_signal and (has_score_signal or has_flow_signal or has_activity_24h)


def open_signal_is_active_non_holding(item: Dict[str, Any], *, safe_float, safe_int) -> bool:
    activity_state = str(item.get("activity_state") or "").strip().lower()
    trades_24h = safe_int(item.get("trades_24h"))
    buys_24h = safe_int(item.get("buys_24h"))
    sells_24h = safe_int(item.get("sells_24h"))
    abs_net_notional_24h = abs(safe_float(item.get("net_notional_24h")))
    if activity_state and activity_state != "holding":
        return True
    return trades_24h > 0 or buys_24h > 0 or sells_24h > 0 or abs_net_notional_24h > 0


def agent_position_scope_key(condition_id: str | None, outcome: str | None) -> str:
    cid = str(condition_id or "").strip().lower()
    out = str(outcome or "").strip().lower()
    if not cid:
        return ""
    return f"{cid}:{out}"


def agent_active_mute_sets(conn_obj, *, prune_expired_agent_mutes_fn, agent_mutes_fn, normalize_wallet) -> Dict[str, Any]:
    prune_expired_agent_mutes_fn(conn_obj)
    rows = agent_mutes_fn(conn_obj, include_expired=False, limit=5000)
    wallets: set[str] = set()
    dedupe_keys: set[str] = set()
    positions: set[str] = set()
    for row in rows:
        scope_type = str(row["scope_type"] or "").strip().lower()
        scope_value = str(row["scope_value"] or "").strip().lower()
        if not scope_value:
            continue
        if scope_type == "wallet":
            wallets.add(normalize_wallet(scope_value))
        elif scope_type == "dedupe_key":
            dedupe_keys.add(scope_value)
        elif scope_type == "position":
            positions.add(scope_value)
    return {
        "rows": rows,
        "wallets": wallets,
        "dedupe_keys": dedupe_keys,
        "positions": positions,
    }


def agent_signal_muted(item: Dict[str, Any], mute_sets: Dict[str, Any], *, normalize_wallet) -> bool:
    wallet = normalize_wallet(str(item.get("wallet") or ""))
    dedupe_key = str(item.get("dedupe_key") or "").strip().lower()
    position_key = agent_position_scope_key(item.get("condition_id"), item.get("outcome"))
    if wallet and wallet in mute_sets["wallets"]:
        return True
    if dedupe_key and dedupe_key in mute_sets["dedupe_keys"]:
        return True
    if position_key and position_key in mute_sets["positions"]:
        return True
    return False


def agent_signal_priority(item: Dict[str, Any], *, safe_float, safe_int) -> float:
    signal_type = str(item.get("signal_type") or "").strip().lower()
    insider_score = safe_float(item.get("insider_score"))
    speculator_score = safe_float(item.get("speculator_score"))
    position_value = safe_float(item.get("position_value"))
    trades_1h = safe_int(item.get("trades_1h"))
    buys_1h = safe_int(item.get("buys_1h"))
    max_position_value = safe_float(item.get("max_position_value"))

    if signal_type.startswith("position_"):
        burst_bonus = min(12.0, float(max(trades_1h, buys_1h)) * 1.5)
        value_bonus = min(20.0, position_value / 5000.0)
        spec_penalty = 0.0 if signal_type == "position_speculator_ladder" else min(16.0, speculator_score * 0.2)
        if signal_type == "position_speculator_ladder":
            return max(0.0, min(100.0, speculator_score + value_bonus + burst_bonus))
        return max(0.0, min(100.0, insider_score + value_bonus + burst_bonus - spec_penalty))

    value_bonus = min(20.0, max_position_value / 5000.0)
    spec_penalty = min(16.0, speculator_score * 0.2)
    return max(0.0, min(100.0, insider_score + value_bonus - spec_penalty))


def agent_signal_severity(item: Dict[str, Any], *, safe_float, speculator_score_high: float, safe_int) -> str:
    signal_type = str(item.get("signal_type") or "").strip().lower()
    insider_score = safe_float(item.get("insider_score"))
    speculator_score = safe_float(item.get("speculator_score"))
    priority = agent_signal_priority(item, safe_float=safe_float, safe_int=safe_int)
    if signal_type in {"wallet_insider_candidate", "position_insider_candidate"}:
        if insider_score >= 85 or priority >= 85:
            return "critical"
        if insider_score >= 75 or priority >= 75:
            return "high"
        return "medium"
    if signal_type == "position_speculator_ladder":
        if speculator_score >= 85:
            return "high"
        if speculator_score >= 70:
            return "medium"
        return "low"
    if priority >= 80:
        return "high"
    if priority >= 65:
        return "medium"
    return "low"


def agent_parse_reason_checks(reason: str) -> Dict[str, bool]:
    checks: Dict[str, bool] = {}
    for part in str(reason or "").split(","):
        tok = part.strip()
        if not tok:
            continue
        m = _RE_REASON_CHECK.match(tok)
        if not m:
            continue
        checks[m.group(1)] = m.group(2).lower() == "yes"
    return checks


def agent_failed_reason_checks(reason: str) -> List[str]:
    checks = agent_parse_reason_checks(reason)
    return [k for k, ok in sorted(checks.items()) if not ok]


def agent_signal_score_scopes(item: Dict[str, Any]) -> Dict[str, str]:
    signal_type = str(item.get("signal_type") or "").strip().lower()
    if signal_type.startswith("position_"):
        return {"insider_score": "position", "speculator_score": "position"}
    return {"insider_score": "wallet", "speculator_score": "wallet"}


def agent_signal_severity_and_reason(
    item: Dict[str, Any],
    *,
    safe_float,
    safe_int,
    speculator_score_high: float,
) -> tuple[str, str]:
    signal_type = str(item.get("signal_type") or "").strip().lower()
    insider_score = safe_float(item.get("insider_score"))
    speculator_score = safe_float(item.get("speculator_score"))
    priority = safe_float(item.get("priority_score"))
    if priority <= 0:
        priority = agent_signal_priority(item, safe_float=safe_float, safe_int=safe_int)

    severity = agent_signal_severity(
        item,
        safe_float=safe_float,
        safe_int=safe_int,
        speculator_score_high=speculator_score_high,
    )

    if signal_type in {"wallet_insider_candidate", "position_insider_candidate"}:
        if severity == "critical":
            return (
                severity,
                (
                    f"{signal_type}: critical because insider_score>=85 or priority>=85 "
                    f"(insider_score={insider_score:.1f}, priority={priority:.1f})"
                ),
            )
        if severity == "high":
            return (
                severity,
                (
                    f"{signal_type}: high because insider_score>=75 or priority>=75 "
                    f"(insider_score={insider_score:.1f}, priority={priority:.1f})"
                ),
            )
        return severity, f"{signal_type}: {severity} (insider_score={insider_score:.1f}, priority={priority:.1f})"

    if signal_type == "position_speculator_ladder":
        if severity == "high":
            return severity, f"{signal_type}: high because speculator_score>=85 (speculator_score={speculator_score:.1f})"
        if severity == "medium":
            return severity, f"{signal_type}: medium because speculator_score>=70 (speculator_score={speculator_score:.1f})"
        return severity, f"{signal_type}: {severity} (speculator_score={speculator_score:.1f})"

    if priority >= 80:
        return (
            severity,
            f"{signal_type or 'signal'}: high because priority>=80 (priority={priority:.1f}; critical reserved for insider_candidate lanes)",
        )
    if priority >= 65:
        return severity, f"{signal_type or 'signal'}: medium because priority>=65 (priority={priority:.1f})"
    return severity, f"{signal_type or 'signal'}: low because priority<65 (priority={priority:.1f})"


def agent_signal_summary(item: Dict[str, Any], *, short_wallet, safe_float) -> str:
    signal_type = str(item.get("signal_type") or "")
    title = str(item.get("title") or item.get("top_open_position_title") or "").strip()
    wallet = short_wallet(str(item.get("wallet") or ""))
    insider = safe_float(item.get("insider_score"))
    speculator = safe_float(item.get("speculator_score"))
    position_value = safe_float(item.get("position_value"), safe_float(item.get("max_position_value")))
    parts = [
        signal_type or "signal",
        wallet or "wallet",
        f"insider={insider:.1f}",
        f"spec={speculator:.1f}",
        f"value={position_value:.2f}",
    ]
    if title:
        parts.append(title)
    return " | ".join(parts)


def collect_agent_high_signals(
    conn_obj,
    *,
    since_ts: int | None = None,
    limit: int = 200,
    include_speculators: bool = False,
    agent_feed_max_limit: int,
    agent_signal_min_insider_score: float,
    agent_signal_min_position_insider_score: float,
    agent_signal_min_position_value: float,
    speculator_score_high: float,
    open_signals_focus_min_insider_score: float,
    open_signals_focus_min_speculator_score: float,
    open_signals_focus_min_position_value: float,
    open_signals_focus_min_abs_net_notional_24h: float,
    ignored_wallets_fn,
    prune_expired_agent_mutes_fn,
    agent_mutes_fn,
    flagged_users_fn,
    emerging_insider_wallets_fn,
    position_signals_fn,
    wallet_profiles_map_fn,
    wallet_identity_fn,
    normalize_wallet_fn,
    safe_float_fn,
    safe_int_fn,
    short_wallet_fn,
) -> List[Dict[str, Any]]:
    ignored = ignored_wallets_fn(conn_obj)
    mute_sets = agent_active_mute_sets(
        conn_obj,
        prune_expired_agent_mutes_fn=prune_expired_agent_mutes_fn,
        agent_mutes_fn=agent_mutes_fn,
        normalize_wallet=normalize_wallet_fn,
    )
    ignored.update(mute_sets["wallets"])
    min_ts = int(since_ts or 0)
    requested_limit = max(1, min(int(limit), agent_feed_max_limit))
    wallet_rows = flagged_users_fn(conn_obj, limit=max(requested_limit * 3, 300))
    emerging_rows = emerging_insider_wallets_fn(conn_obj, limit=max(requested_limit * 3, 300))
    position_rows = position_signals_fn(
        conn_obj,
        limit=max(requested_limit * 12, 1500),
        open_only=True,
    )

    raw: List[Dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str, str]] = set()

    for row in wallet_rows:
        wallet = normalize_wallet_fn(row["wallet"])
        if not wallet or wallet in ignored:
            continue
        signal_ts = safe_int_fn(row["flagged_ts"])
        if min_ts > 0 and signal_ts < min_ts:
            continue
        insider_score = safe_float_fn(row["insider_score"])
        speculator_score = safe_float_fn(row["speculator_score"])
        if insider_score < agent_signal_min_insider_score:
            continue
        if not include_speculators and speculator_score >= speculator_score_high and insider_score < speculator_score:
            continue
        key = ("wallet_insider_candidate", wallet, "", "")
        if key in seen_keys:
            continue
        seen_keys.add(key)
        raw.append(
            {
                "signal_type": "wallet_insider_candidate",
                "source": "flagged_users",
                "wallet": wallet,
                "condition_id": "",
                "outcome": "",
                "title": str(row["top_open_position_title"] or row["last_title"] or ""),
                "signal_ts": signal_ts,
                "insider_score": insider_score,
                "speculator_score": speculator_score,
                "classification": str(row["classification"] or ""),
                "max_position_value": safe_float_fn(row["max_position_value"]),
                "total_open_value": safe_float_fn(row["total_open_value"]),
                "reason": str(row["source_note"] or ""),
                "dedupe_key": f"wallet_insider:{wallet}",
            }
        )

    for row in emerging_rows:
        wallet = normalize_wallet_fn(row["wallet"])
        if not wallet or wallet in ignored:
            continue
        signal_ts = safe_int_fn(row["analyzed_ts"])
        if min_ts > 0 and signal_ts < min_ts:
            continue
        insider_score = safe_float_fn(row["insider_score"])
        speculator_score = safe_float_fn(row["speculator_score"])
        if insider_score < agent_signal_min_insider_score:
            continue
        if not include_speculators and speculator_score >= speculator_score_high:
            continue
        key = ("wallet_emerging_insider", wallet, "", "")
        if key in seen_keys:
            continue
        seen_keys.add(key)
        raw.append(
            {
                "signal_type": "wallet_emerging_insider",
                "source": "emerging_insiders",
                "wallet": wallet,
                "condition_id": "",
                "outcome": "",
                "title": str(row["top_open_position_title"] or row["last_title"] or ""),
                "signal_ts": signal_ts,
                "insider_score": insider_score,
                "speculator_score": speculator_score,
                "classification": str(row["classification"] or ""),
                "max_position_value": safe_float_fn(row["max_position_value"]),
                "total_open_value": safe_float_fn(row["total_open_value"]),
                "reason": str(row["reason"] or ""),
                "dedupe_key": f"wallet_emerging:{wallet}",
            }
        )

    for row in position_rows:
        item = dict(row)
        wallet = normalize_wallet_fn(item.get("wallet"))
        if not wallet or wallet in ignored:
            continue
        signal_ts = safe_int_fn(item.get("analyzed_ts"))
        if min_ts > 0 and signal_ts < min_ts:
            continue
        if not open_signal_is_focused(
            item,
            min_insider_score=open_signals_focus_min_insider_score,
            min_speculator_score=open_signals_focus_min_speculator_score,
            min_position_value=open_signals_focus_min_position_value,
            min_abs_net_notional_24h=open_signals_focus_min_abs_net_notional_24h,
            safe_float=safe_float_fn,
            safe_int=safe_int_fn,
        ):
            continue
        if not open_signal_is_active_non_holding(item, safe_float=safe_float_fn, safe_int=safe_int_fn):
            continue

        classification = str(item.get("position_classification") or "position_low_signal").strip().lower()
        insider_score = safe_float_fn(item.get("position_insider_score"))
        speculator_score = safe_float_fn(item.get("position_speculator_score"))
        position_value = safe_float_fn(item.get("position_value"))
        if classification == "position_low_signal":
            continue
        if not include_speculators and classification == "position_speculator_ladder":
            continue
        if classification != "position_speculator_ladder" and insider_score < agent_signal_min_position_insider_score:
            continue
        if position_value < agent_signal_min_position_value and insider_score < 75:
            continue

        condition_id = str(item.get("condition_id") or "").strip().lower()
        outcome = str(item.get("outcome") or "").strip().lower()
        signal_type = classification if classification else "position_signal"
        key = (signal_type, wallet, condition_id, outcome)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        raw.append(
            {
                "signal_type": signal_type,
                "source": "position_signals",
                "wallet": wallet,
                "condition_id": condition_id,
                "outcome": outcome,
                "title": str(item.get("title") or ""),
                "event_slug": str(item.get("event_slug") or ""),
                "signal_ts": signal_ts,
                "insider_score": insider_score,
                "speculator_score": speculator_score,
                "classification": classification,
                "position_value": position_value,
                "position_share": safe_float_fn(item.get("position_share")),
                "buys_1h": safe_int_fn(item.get("buys_1h")),
                "trades_1h": safe_int_fn(item.get("trades_1h")),
                "net_notional_24h": safe_float_fn(item.get("net_notional_24h")),
                "reason": str(item.get("signal_reason") or ""),
                "dedupe_key": f"position:{wallet}:{condition_id}:{outcome}:{signal_type}",
            }
        )

    if not raw:
        return []

    raw = [item for item in raw if not agent_signal_muted(item, mute_sets, normalize_wallet=normalize_wallet_fn)]
    if not raw:
        return []

    wallets = sorted({normalize_wallet_fn(item.get("wallet")) for item in raw if item.get("wallet")})
    profiles_by_wallet = wallet_profiles_map_fn(conn_obj, wallets)
    for item in raw:
        identity = wallet_identity_fn(item.get("wallet"), profiles_by_wallet)
        item["username"] = identity["username"]
        item["pseudonym"] = identity["pseudonym"]
        item["profile_image"] = identity["profile_image"]
        base_priority = agent_signal_priority(item, safe_float=safe_float_fn, safe_int=safe_int_fn)
        # Apply world context boost: world_analyst.py writes per-market boosts to
        # Supabase brain. Surfacing boosted signals higher in the UI lets the user
        # see which signals are corroborated by current world events.
        _condition_id = str(item.get("condition_id") or "")
        _world_boosts = _pipeline_load_world_boosts()
        _boost_entry = _world_boosts.get(_condition_id, {})
        _boost_val = int(_boost_entry.get("boost", 0)) if _boost_entry else 0
        item["priority_score"] = max(0.0, min(100.0, base_priority + _boost_val))
        if _boost_val != 0:
            item["world_boost"] = _boost_val
            item["world_boost_reason"] = _boost_entry.get("reason", "")
            item["world_boost_direction"] = _boost_entry.get("direction", "")
        severity, severity_reason = agent_signal_severity_and_reason(
            item,
            safe_float=safe_float_fn,
            safe_int=safe_int_fn,
            speculator_score_high=speculator_score_high,
        )
        item["severity"] = severity
        item["severity_reason"] = severity_reason
        item["summary"] = agent_signal_summary(item, short_wallet=short_wallet_fn, safe_float=safe_float_fn)
        item["score_scopes"] = agent_signal_score_scopes(item)
        item["failed_checks"] = agent_failed_reason_checks(str(item.get("reason") or ""))

    raw.sort(
        key=lambda row: (
            safe_float_fn(row.get("priority_score")),
            safe_int_fn(row.get("signal_ts")),
        ),
        reverse=True,
    )
    return raw[:requested_limit]


def emit_agent_alerts(
    conn_obj,
    signals: List[Dict[str, Any]],
    *,
    enqueue_alert_outbox_fn,
    alert_dedupe_seconds: int,
    safe_int_fn,
) -> Dict[str, Any]:
    created = 0
    deduped = 0
    reactivated = 0
    failed = 0
    ids: List[int] = []
    for signal in signals:
        result = enqueue_alert_outbox_fn(
            conn_obj,
            dedupe_key=str(signal.get("dedupe_key") or ""),
            alert_type=str(signal.get("signal_type") or ""),
            wallet=str(signal.get("wallet") or ""),
            condition_id=str(signal.get("condition_id") or ""),
            outcome=str(signal.get("outcome") or ""),
            severity=str(signal.get("severity") or "medium"),
            title=str(signal.get("title") or signal.get("summary") or ""),
            message=str(signal.get("summary") or ""),
            payload=signal,
            event_ts=safe_int_fn(signal.get("signal_ts")),
            dedupe_seconds=alert_dedupe_seconds,
        )
        if not bool(result.get("ok")):
            failed += 1
            continue
        alert_id = safe_int_fn(result.get("id"))
        if alert_id > 0:
            ids.append(alert_id)
        if bool(result.get("created")):
            created += 1
        elif bool(result.get("deduped")):
            deduped += 1
        elif bool(result.get("reactivated")):
            reactivated += 1
    return {
        "created": created,
        "deduped": deduped,
        "reactivated": reactivated,
        "failed": failed,
        "alert_ids": ids,
    }


def serialize_alert_row(row: Any, profiles_by_wallet: Dict[str, Any], *, wallet_identity_fn, format_unix_fn) -> Dict[str, Any]:
    payload_json = str(row["payload_json"] or "").strip()
    payload: Dict[str, Any] = {}
    if payload_json:
        try:
            parsed = json.loads(payload_json)
            if isinstance(parsed, dict):
                payload = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}

    wallet = str(row["wallet"] or "")
    identity = wallet_identity_fn(wallet, profiles_by_wallet)
    return {
        "id": int(row["id"] or 0),
        "dedupe_key": str(row["dedupe_key"] or ""),
        "alert_type": str(row["alert_type"] or ""),
        "wallet": wallet,
        "username": identity["username"],
        "pseudonym": identity["pseudonym"],
        "profile_image": identity["profile_image"],
        "condition_id": str(row["condition_id"] or ""),
        "outcome": str(row["outcome"] or ""),
        "severity": str(row["severity"] or "info"),
        "title": str(row["title"] or ""),
        "message": str(row["message"] or ""),
        "payload": payload,
        "status": str(row["status"] or "new"),
        "first_seen_ts": int(row["first_seen_ts"] or 0),
        "first_seen_at": format_unix_fn(row["first_seen_ts"]),
        "last_seen_ts": int(row["last_seen_ts"] or 0),
        "last_seen_at": format_unix_fn(row["last_seen_ts"]),
        "seen_count": int(row["seen_count"] or 0),
        "dedupe_until_ts": int(row["dedupe_until_ts"] or 0),
        "dedupe_until_at": format_unix_fn(row["dedupe_until_ts"]),
        "sent_ts": int(row["sent_ts"] or 0),
        "sent_at": format_unix_fn(row["sent_ts"]),
        "acknowledged_ts": int(row["acknowledged_ts"] or 0),
        "acknowledged_at": format_unix_fn(row["acknowledged_ts"]),
        "updated_at": str(row["updated_at"] or ""),
    }


def serialize_agent_mute_row(row: Any, *, format_unix_fn) -> Dict[str, Any]:
    scope_type = str(row["scope_type"] or "").strip().lower()
    scope_value = str(row["scope_value"] or "").strip()
    condition_id = ""
    outcome = ""
    if scope_type == "position" and ":" in scope_value:
        condition_id, outcome = scope_value.split(":", 1)
    return {
        "id": int(row["id"] or 0),
        "scope_type": scope_type,
        "scope_value": scope_value,
        "condition_id": condition_id,
        "outcome": outcome,
        "reason": str(row["reason"] or ""),
        "expires_ts": int(row["expires_ts"] or 0),
        "expires_at": format_unix_fn(row["expires_ts"]),
        "created_ts": int(row["created_ts"] or 0),
        "created_at": format_unix_fn(row["created_ts"]),
        "updated_at": str(row["updated_at"] or ""),
    }
