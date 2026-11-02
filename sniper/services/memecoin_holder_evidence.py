"""Fail-closed holder evidence derived from already-observed feed and follow responses."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime

from sniper.analysis.fomo_account.classify import classify_swap
from sniper.feeds.fomo_feed import _norm_network
from sniper.persistence import db_memecoin as m


def _row(value):
    return value if isinstance(value, dict) else value.__dict__


def _parse(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _remaining(raw, raw_type):
    return m._explicit_remaining_usd(raw, raw_type)


def _network(value, mint):
    network = _norm_network(value)
    return m.normalized_coin_network(network, mint)


def _hash(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _write(conn, *, network_id, mint, source_user_id, source_handle, state,
           remaining_usd, observed_at, source, upstream_event_id=None,
           successful_poll_at=None, reason=None, source_event_at=None, commit=False):
    exact_network = _network(network_id, mint)
    if exact_network == "evm-unknown":
        state, remaining_usd, reason = "unknown", None, "unresolved_network"
    confirmation = "explicit_post_trade" if state in ("positive", "zero") else "unconfirmed"
    invalidated = observed_at if state == "unknown" else None
    payload = {
        "network_id": exact_network, "mint": mint, "source_user_id": source_user_id,
        "state": state, "remaining_usd": remaining_usd,
        "source": source, "upstream_event_id": upstream_event_id,
        "reason": reason, "source_event_at": source_event_at,
    }
    m.upsert_holder_evidence(
        conn, network_id=exact_network, mint=mint, source_user_id=source_user_id,
        source_handle=source_handle, evidence_state=state, remaining_usd=remaining_usd,
        observed_at=observed_at, source=source, upstream_event_id=upstream_event_id,
        confirmation_state=confirmation, successful_poll_at=successful_poll_at,
        invalidated_at=invalidated, invalidation_reason=reason, updated_at=observed_at,
        clock_domain="observed_real_utc", source_event_at=source_event_at,
        source_timestamp_trust=("claimed_untrusted" if source_event_at else "missing"),
        evidence_hash=_hash(payload), commit=commit,
    )


def reconcile_feed_observations(conn, observations, armed_ids):
    """Reconcile already-retained v2 observations; no fetch and one local commit."""
    if conn.in_transaction:
        raise RuntimeError("holder reconciliation refused inside caller-owned transaction")
    armed_ids = set(armed_ids or ())
    ordered = sorted(
        observations or (),
        key=lambda item: (
            str(_row(item).get("observed_at") or ""),
            1 if _row(item).get("normalized_side") == "sell" else 0,
        ),
    )
    try:
        for observation in ordered:
            row = _row(observation)
            uid = str(row.get("source_user_id") or "")
            mint = str(row.get("mint") or "")
            side = row.get("normalized_side")
            observed_at = row.get("observed_at")
            if uid not in armed_ids or not mint or side not in ("buy", "sell") or not observed_at:
                continue
            raw_type = str(row.get("raw_type") or "")
            remaining = _remaining(row.get("raw_payload") or {}, raw_type)
            if remaining is None:
                state = "unknown"
                reason = f"{side}_missing_remaining"
            elif remaining == 0:
                state, reason = "zero", None
            else:
                state, reason = "positive", None
            _write(
                conn, network_id=row.get("network_id"), mint=mint, source_user_id=uid,
                source_handle=row.get("source_handle"), state=state, remaining_usd=remaining,
                observed_at=observed_at, source="global_feed_v2",
                upstream_event_id=row.get("upstream_event_id"), reason=reason,
                source_event_at=row.get("source_ts"),
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def reconcile_follow_poll(conn, source_user_id, swaps, observed_at):
    """Consume one existing successful `/swaps` response and invalidate ambiguous holdings."""
    if conn.in_transaction:
        raise RuntimeError("holder follow reconciliation refused inside caller-owned transaction")
    try:
        m.mark_holder_poll_success(conn, source_user_id, observed_at, commit=False)
        for swap in swaps or ():
            kind, mint, _usd, _qty = classify_swap(swap)
            if kind not in ("buy", "sell") or not mint:
                continue
            remaining = None
            if "currentSizeUsd" in swap:
                try:
                    candidate = float(swap.get("currentSizeUsd"))
                    if math.isfinite(candidate) and candidate >= 0:
                        remaining = candidate
                except (TypeError, ValueError):
                    remaining = None
            if remaining is None:
                state, reason = "unknown", f"follow_{kind}_missing_remaining"
            elif remaining == 0:
                state, reason = "zero", None
            else:
                state, reason = "positive", None
            _write(
                conn, network_id=swap.get("networkId") or swap.get("network_id"), mint=mint,
                source_user_id=source_user_id, source_handle=None, state=state,
                remaining_usd=remaining, observed_at=observed_at, source="follow_activity",
                upstream_event_id=("act-" + str(swap.get("id"))) if swap.get("id") else None,
                successful_poll_at=observed_at, reason=reason,
                source_event_at=swap.get("createdAt"),
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def invalidate_wallet_evidence(conn, source_user_id, observed_at, reason):
    if conn.in_transaction:
        raise RuntimeError("holder invalidation refused inside caller-owned transaction")
    rows = conn.execute(
        "SELECT * FROM memecoin_holder_evidence WHERE source_user_id=? "
        "AND evidence_state='positive'",
        (source_user_id,),
    ).fetchall()
    try:
        for row in rows:
            _write(
                conn, network_id=row["network_id"], mint=row["mint"],
                source_user_id=source_user_id, source_handle=row["source_handle"],
                state="unknown", remaining_usd=None, observed_at=observed_at,
                source="follow_activity", upstream_event_id=None, reason=reason,
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def fresh_positive_holders(conn, *, network_id, mint, exclude_user_id, now_iso,
                           max_age_s, min_remaining_usd):
    try:
        max_age_s = float(max_age_s)
        min_remaining_usd = float(min_remaining_usd)
    except (TypeError, ValueError):
        return []
    if not (math.isfinite(max_age_s) and max_age_s > 0
            and math.isfinite(min_remaining_usd) and min_remaining_usd > 0):
        return []
    network = m.normalized_coin_network(network_id, mint)
    if network == "evm-unknown" or _parse(now_iso) is None:
        return []
    rows = conn.execute(
        "SELECT h.* FROM memecoin_holder_evidence h "
        "JOIN memecoin_wallets w ON w.source_user_id=h.source_user_id "
        "WHERE h.network_id=? AND h.mint=? AND h.source_user_id<>? "
        "AND h.evidence_state='positive' AND h.remaining_usd>=? "
        "AND w.real_approved=1 AND w.paused=0 AND w.excluded=0 "
        "AND julianday(h.observed_at)<=julianday(?) "
        "AND julianday(h.observed_at)>=julianday(?) - (? / 86400.0) "
        "ORDER BY h.source_user_id",
        (network, mint, exclude_user_id or "", min_remaining_usd,
         now_iso, now_iso, max_age_s),
    ).fetchall()
    return [dict(row) for row in rows]


def holder_evidence_at(conn, *, network_id, mint, as_of, max_age_s):
    """Latest fresh holder fact per wallet known at-or-before ``as_of`` from append-only history.

    Both observed_at and recorded_at are bounded. That prevents a late-arriving historical event
    from leaking into an earlier decision reconstruction. Unknown is current-state-only.
    """
    try:
        max_age_s = float(max_age_s)
    except (TypeError, ValueError):
        return []
    if not mint or _parse(as_of) is None or not math.isfinite(max_age_s) or max_age_s <= 0:
        return []
    network = m.normalized_coin_network(network_id, mint)
    if network == "evm-unknown":
        return []
    rows = conn.execute(
        "SELECT * FROM memecoin_holder_evidence_history WHERE network_id=? AND mint=? "
        "AND julianday(observed_at)<=julianday(?) AND julianday(recorded_at)<=julianday(?) "
        "AND julianday(observed_at)>=julianday(?) - (? / 86400.0) "
        "ORDER BY source_user_id, julianday(observed_at) DESC, id DESC",
        (network, mint, as_of, as_of, as_of, max_age_s)).fetchall()
    latest = {}
    for row in rows:
        uid = row["source_user_id"]
        if uid not in latest:
            latest[uid] = dict(row)
    return [latest[uid] for uid in sorted(latest)]


def future_context_at(conn, position, *, decision_at, max_age_s):
    """WO-16 observation-only context at one historical decision boundary.

    Source-wallet state is referenced from the mirror evidence path; co-holder facts use the same
    point-in-time history. A causal SOL-market feed is not installed, so the missing family is
    explicit rather than substituted with present-day or broad-tape data.
    """
    pos = dict(position or {})
    source_uid = str(pos.get("source_user_id") or "")
    rows = holder_evidence_at(
        conn, network_id=pos.get("network_id"), mint=pos.get("mint"), as_of=decision_at,
        max_age_s=max_age_s)
    source_row = next((row for row in rows if row["source_user_id"] == source_uid), None)
    source_state = {
        "state": source_row["evidence_state"] if source_row else "unknown",
        "remaining_usd": source_row["remaining_usd"] if source_row else None,
        "observed_at": source_row["observed_at"] if source_row else None,
        "source": source_row["source"] if source_row else None,
        "confirmation_state": source_row["confirmation_state"] if source_row else "unconfirmed",
        "reason": None if source_row else "no_fresh_point_in_time_evidence",
    }
    counts = {"positive": 0, "zero": 0, "unknown": 0}
    for row in rows:
        if row["source_user_id"] == source_uid:
            continue
        state = row["evidence_state"]
        counts[state if state in counts else "unknown"] += 1
    return {
        "as_of": decision_at,
        "max_age_s": float(max_age_s),
        "source_wallet_exit_state": source_state,
        "trusted_coholders": counts,
        "sol_market_context": {
            "status": "blocked_missing_feed",
            "reason": "verified_causal_sol_market_feed_unavailable",
        },
        "observation_only": True,
    }
