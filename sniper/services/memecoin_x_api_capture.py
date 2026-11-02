"""Transactional X cashtag ingestion, stream handling, and bounded gap repair."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from sniper.analysis.memecoin_x.api_normalize import normalize_api_payload
from sniper.analysis.memecoin_x.api_rules import compile_stream_rules, roster_hash
from sniper.persistence import db_memecoin_x as X
from sniper.services.memecoin_x_api_client import XApiError


def _utc_day(iso: str) -> str:
    value = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("corrected observation clock must be timezone-aware")
    return value.astimezone(timezone.utc).date().isoformat()


def _post_max(left: str | None, right: str | None) -> str | None:
    if left is None:
        return right
    if right is None:
        return left
    try:
        return str(max(int(left), int(right)))
    except ValueError:
        return max(str(left), str(right))


def _set_state(conn, observed_at: str, **updates) -> dict:
    state = X.api_capture_state(conn)
    return X.update_api_capture_state(
        conn, expected_revision=state["revision"], updated_at=observed_at, **updates)


def reconcile_rules(conn, client, *, observed_at_fn) -> list[dict]:
    observed_at = observed_at_fn()
    roster = X.active_api_roster(conn)
    desired = compile_stream_rules(roster)
    existing = client.list_rules()
    client.replace_rules(existing=existing, desired=desired)
    rule_digest = hashlib.sha256(json.dumps(
        desired, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    _set_state(conn, observed_at, roster_hash=roster_hash(roster), rule_hash=rule_digest)
    return desired


def ingest_payload(conn, payload, *, source, settings, observed_at_fn,
                   advance_frontier=True) -> dict:
    if source not in {"stream", "recovery", "reference", "fixture"}:
        raise ValueError(f"invalid X API source: {source}")
    observed_at = observed_at_fn()
    utc_day = _utc_day(observed_at)
    post_id = str((payload.get("data") or {}).get("id") or "")
    if not post_id:
        raise ValueError("X API payload is missing actor Post ID")
    already_counted = conn.execute(
        "SELECT 1 FROM memecoin_x_api_resources WHERE utc_day=? AND resource_id=?",
        (utc_day, post_id)).fetchone() is not None
    if (not already_counted and X.api_resource_count(conn, utc_day=utc_day)
            >= int(settings.max_post_reads_per_day)):
        return {"events_created": 0, "resource_counted": False,
                "budget_exhausted": True, "post_id": post_id}

    roster = {row["x_user_id"]: row for row in X.active_api_roster(conn)}
    normalized = normalize_api_payload(
        payload, roster_by_user_id=roster, observed_at=observed_at)
    try:
        conn.execute("BEGIN IMMEDIATE")
        counted = X.count_api_resource(
            conn, utc_day=utc_day, resource_id=post_id, source=source,
            counted_at=observed_at, commit=False)
        persisted = {"events_created": 0}
        if normalized is not None:
            persisted = X.persist_api_event(
                conn, normalized, source=source, observed_at=observed_at,
                clock_domain="corrected_utc", created_at=observed_at)
        if advance_frontier:
            state = X.api_capture_state(conn)
            frontier = _post_max(state.get("frontier_post_id"), post_id)
            X.update_api_capture_state(
                conn, expected_revision=state["revision"], state="healthy",
                frontier_post_id=frontier, last_healthy_at=observed_at,
                bounded_detail=None, updated_at=observed_at, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {**persisted, "resource_counted": counted, "budget_exhausted": False,
            "post_id": post_id, "eligible": normalized is not None}


def _recovery_query(roster: list[dict]) -> str:
    values = [row["value"] for row in compile_stream_rules(roster)]
    return values[0] if len(values) == 1 else " OR ".join(f"({value})" for value in values)


def run_recovery(conn, client, *, settings, observed_at_fn) -> dict:
    observed_at = observed_at_fn()
    roster = X.active_api_roster(conn)
    state = X.api_capture_state(conn)
    since_id = state.get("recovery_since_id") or state.get("frontier_post_id")
    next_token = state.get("recovery_next_token")
    max_post_id = state.get("recovery_max_post_id")
    _set_state(
        conn, observed_at, state="recovering", recovery_since_id=since_id,
        bounded_detail=None)

    envelopes = []
    pages = 0
    complete = False
    while pages < int(settings.recovery_max_pages):
        response = client.recent_search(
            query=_recovery_query(roster), since_id=since_id,
            next_token=next_token, max_results=100)
        pages += 1
        meta = response.get("meta") or {}
        max_post_id = _post_max(max_post_id, meta.get("newest_id"))
        includes = response.get("includes") or {}
        for post in response.get("data") or []:
            max_post_id = _post_max(max_post_id, str(post.get("id") or "") or None)
            envelopes.append({"data": post, "includes": includes, "matching_rules": []})
        next_token = meta.get("next_token")
        if not next_token:
            complete = True
            break

    events_created = 0
    resources_counted = 0
    for envelope in sorted(envelopes, key=lambda row: int(row["data"]["id"])):
        result = ingest_payload(
            conn, envelope, source="recovery", settings=settings,
            observed_at_fn=observed_at_fn, advance_frontier=False)
        if result.get("budget_exhausted"):
            complete = False
            break
        events_created += int(result.get("events_created") or 0)
        resources_counted += 1 if result.get("resource_counted") else 0

    finished_at = observed_at_fn()
    if complete:
        _set_state(
            conn, finished_at, state="healthy",
            frontier_post_id=_post_max(state.get("frontier_post_id"), max_post_id),
            recovery_since_id=None, recovery_next_token=None,
            recovery_max_post_id=None, last_recovery_at=finished_at,
            last_healthy_at=finished_at, bounded_detail=None)
    else:
        _set_state(
            conn, finished_at, state="coverage_gap", recovery_since_id=since_id,
            recovery_next_token=next_token, recovery_max_post_id=max_post_id,
            last_recovery_at=finished_at,
            bounded_detail="bounded Recent Search recovery remains incomplete")
    return {"complete": complete, "pages": pages, "events_created": events_created,
            "resources_counted": resources_counted, "next_token": next_token}


def run_stream(conn, client, *, settings, observed_at_fn, stop_event) -> dict:
    consumed = 0
    iterator = iter(client.stream())
    try:
        while not stop_event.is_set():
            observed_at = observed_at_fn()
            if X.api_resource_count(conn, utc_day=_utc_day(observed_at)) >= int(
                    settings.max_post_reads_per_day):
                _set_state(
                    conn, observed_at, state="budget_exhausted",
                    bounded_detail="local daily Post resource cap reached")
                return {"state": "budget_exhausted", "payloads_consumed": consumed}
            try:
                payload = next(iterator)
            except StopIteration:
                break
            result = ingest_payload(
                conn, payload, source="stream", settings=settings,
                observed_at_fn=observed_at_fn)
            if result.get("budget_exhausted"):
                _set_state(
                    conn, observed_at_fn(), state="budget_exhausted",
                    bounded_detail="local daily Post resource cap reached")
                return {"state": "budget_exhausted", "payloads_consumed": consumed}
            consumed += 1
        return {"state": X.api_capture_state(conn)["state"],
                "payloads_consumed": consumed}
    except XApiError as exc:
        state_name = "auth_failed" if exc.kind == "auth" else "reconnecting"
        _set_state(conn, observed_at_fn(), state=state_name,
                   bounded_detail=f"X API {exc.kind} failure")
        return {"state": state_name, "payloads_consumed": consumed,
                "error_kind": exc.kind}
    finally:
        close = getattr(iterator, "close", None)
        if close:
            close()
