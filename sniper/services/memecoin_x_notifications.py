"""Bounded display-only delivery for persisted memecoin X Telegram outbox rows."""
from __future__ import annotations

import json

from sniper.persistence.db_memecoin_x import record_notification_attempt


MAX_ATTEMPTS = 3


def _network_label(network_id):
    return {
        "solana": "Solana", "4663": "Robinhood 4663", "8453": "Base 8453",
        "1": "Ethereum 1", "56": "BSC 56",
    }.get(str(network_id), str(network_id))


def render_notification(event: dict, evidence: dict) -> str:
    if str(event.get("event_type") or "").startswith("cashtag_"):
        is_consensus = event.get("event_type") == "cashtag_consensus"
        details = event.get("details") or {}
        tags = details.get("cashtags") or ([event.get("ticker")] if event.get("ticker") else [])
        cashtag_text = " ".join(f"${str(tag).upper().lstrip('$')}" for tag in tags)
        activity = ("fast consensus across distinct curated actors" if is_consensus else
                    "amplified from referenced Post"
                    if event.get("cashtag_source") == "referenced_post"
                    else "mentioned in actor text")
        identity = (f"{_network_label(event.get('network_id'))} {event.get('mint')}"
                    if event.get("identity_state") == "exact_coin"
                    else "ticker only")
        return "\n".join([
            f"X CASHTAG — @{str(event.get('source_handle') or 'unknown').lstrip('@')} · "
            f"{'CONSENSUS' if is_consensus else str(event.get('relationship') or 'direct').upper()}",
            "",
            cashtag_text or "$UNKNOWN",
            str(event.get("excerpt") or "")[:280],
            "",
            f"Activity: {activity}",
            f"Identity: {identity}",
            f"Consensus: {int(event.get('consensus_count') or 1)} curated actor(s) in 60m",
            f"Observed: {event.get('triggered_at') or 'unknown'}",
            "",
            f"Open Post: {event.get('post_url') or 'reference unavailable'}",
        ])
    actors = event.get("actor_handles") or []
    actor_text = ", ".join(f"@{str(actor).lstrip('@')}" for actor in actors) or "none recorded"
    event_name = str(event.get("event_type") or "observed_event").replace("_", " ")
    return "\n".join([
        "X observed correlation — display-only",
        f"Event: {event_name}",
        f"Source: @{str(event.get('source_handle') or 'unknown').lstrip('@')}",
        f"Network: {_network_label(event.get('network_id'))}",
        f"Contract: {event.get('mint') or 'unknown'}",
        f"Observed: {event.get('triggered_at') or 'unknown'}",
        f"Actors: {actor_text}",
        f"Evidence: {evidence.get('logical_key') or 'unknown'}",
        "Informational record; no action recommendation.",
    ])


def _event(row):
    event = dict(row)
    try:
        actors = json.loads(event.pop("actor_handles_json") or "[]")
    except (TypeError, ValueError):
        actors = []
    event["actor_handles"] = actors if isinstance(actors, list) else []
    try:
        details = json.loads(event.pop("details_json") or "{}")
    except (TypeError, ValueError):
        details = {}
    event["details"] = details if isinstance(details, dict) else {}
    return event


def _pending_rows(conn, limit):
    limit = int(limit)
    if not 1 <= limit <= 100:
        raise ValueError("limit must be in 1..100")
    return conn.execute("""
        SELECT o.id AS outbox_id, o.logical_key AS outbox_key, o.attempts,
               e.*
        FROM memecoin_x_notification_outbox o
        JOIN memecoin_x_signal_events e ON e.id=o.signal_event_id
        WHERE o.channel='telegram' AND o.state='pending' AND o.attempts < ?
        ORDER BY o.id LIMIT ?
    """, (MAX_ATTEMPTS, limit)).fetchall()


def preview_pending(conn, *, limit=20) -> list[dict]:
    """Render the exact pending Telegram messages without mutating the outbox."""
    previews = []
    for row in _pending_rows(conn, limit):
        event = _event(row)
        previews.append({
            "outbox_id": event["outbox_id"],
            "logical_key": event["outbox_key"],
            "signal_event_id": event["id"],
            "text": render_notification(event, {"logical_key": event["outbox_key"]}),
        })
    return previews


def deliver_pending(conn, *, sender, observed_at, enabled=False, limit=20) -> dict:
    if not enabled:
        return {"enabled": False, "selected": 0, "sent": 0, "failed": 0}
    rows = _pending_rows(conn, limit)
    sent = failed = 0
    for row in rows:
        event = _event(row)
        text = render_notification(event, {"logical_key": event["outbox_key"]})
        latency_ms = error = None
        try:
            outcome = sender(text)
            ok, latency_ms, error = outcome
            ok = bool(ok)
        except Exception as exc:
            ok, error = False, str(exc)
        if ok:
            record_notification_attempt(
                conn, outbox_id=event["outbox_id"], attempted_at=observed_at,
                result="sent", latency_ms=latency_ms, final_state="sent")
            sent += 1
        else:
            attempts = int(event["attempts"] or 0) + 1
            final_state = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            record_notification_attempt(
                conn, outbox_id=event["outbox_id"], attempted_at=observed_at,
                result="failed", latency_ms=latency_ms, bounded_error=error,
                final_state=final_state)
            failed += 1
    return {"enabled": True, "selected": len(rows), "sent": sent, "failed": failed}
