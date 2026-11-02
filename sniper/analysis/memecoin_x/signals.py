"""Deterministic display-only events derived from exact forward X observations."""
from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timedelta, timezone


CONSENSUS_WINDOW_S = 3600


def _aware(value):
    parsed = datetime.fromisoformat(str(value))
    if parsed.utcoffset() is None:
        raise ValueError("observation timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _actors_for_observation(conn, observation_id):
    rows = conn.execute("""
        SELECT actor_handle, relationship, observed_at
        FROM memecoin_x_relationships
        WHERE source_observation_id=? AND actor_is_tier1_at_obs=1
          AND clock_domain='observed_real_utc'
          AND relationship IN ('direct','repost','quote','reply')
        ORDER BY id
    """, (int(observation_id),)).fetchall()
    actors = {}
    for row in rows:
        actor = str(row["actor_handle"] or "").strip().lstrip("@").lower()
        if actor and actor not in actors:
            actors[actor] = dict(row)
    return actors


def _base_event(event_type, call, *, evidence, actors, relationship_counts):
    logical = f"{event_type}:{call['network_id']}:{call['mint']}:{call['observation_id']}"
    return {
        "logical_key": logical,
        "event_type": event_type,
        "network_id": call["network_id"],
        "mint": call["mint"],
        "triggered_at": call["observed_at"],
        "source_handle": call["source_handle"],
        "evidence_observation_ids": evidence,
        "actor_handles": actors,
        "relationship_counts": dict(sorted(relationship_counts.items())),
        "display_only": True,
    }


def _consensus_event(conn, call, current_actors):
    if call["source_role"] == "official_context" or len(current_actors) != 1:
        return None
    current_time = _aware(call["observed_at"])
    floor = current_time - timedelta(seconds=CONSENSUS_WINDOW_S)
    rows = conn.execute("""
        SELECT DISTINCT o.id AS observation_id, o.observed_at,
               r.actor_handle, r.relationship
        FROM memecoin_x_coin_mentions m
        JOIN memecoin_x_card_observations o ON o.id=m.source_observation_id
        JOIN memecoin_x_relationships r ON r.source_observation_id=o.id
        LEFT JOIN memecoin_x_forward_posts p
          ON p.canonical_post_id=m.canonical_post_id
        WHERE m.eligible=1 AND m.network_id=? AND m.mint=?
          AND o.id < ?
          AND o.clock_domain='observed_real_utc'
          AND r.clock_domain='observed_real_utc'
          AND r.actor_is_tier1_at_obs=1
          AND r.relationship IN ('direct','repost','quote','reply')
          AND COALESCE(p.author_role_at_capture, '') <> 'official_context'
        ORDER BY o.id
    """, (call["network_id"], call["mint"], call["observation_id"])).fetchall()
    history = []
    for row in rows:
        observed = _aware(row["observed_at"])
        if observed > current_time:
            continue
        actor = str(row["actor_handle"] or "").strip().lstrip("@").lower()
        if actor:
            history.append((observed, int(row["observation_id"]), actor, dict(row)))
    history.sort(key=lambda item: (item[0], item[1]))
    active = deque()
    active_counts = Counter()
    for observed, _, actor, _ in history:
        while active and (observed - active[0][0]).total_seconds() > CONSENSUS_WINDOW_S:
            _, expired = active.popleft()
            active_counts[expired] -= 1
            if active_counts[expired] == 0:
                del active_counts[expired]
        if any(prior_actor != actor for prior_actor in active_counts):
            return None  # an earlier second actor already established this coin's event
        active.append((observed, actor))
        active_counts[actor] += 1
    prior = {}
    for observed, _, actor, row in history:
        if observed >= floor:
            prior[actor] = row
    if len(prior) != 1:
        return None
    current_actor, current_row = next(iter(current_actors.items()))
    prior_actor, prior_row = next(iter(prior.items()))
    if current_actor == prior_actor:
        return None
    counts = Counter([prior_row["relationship"], current_row["relationship"]])
    event = _base_event(
        "fast_consensus", call,
        evidence=[prior_row["observation_id"], call["observation_id"]],
        actors=[prior_actor, current_actor], relationship_counts=counts,
    )
    event["logical_key"] = (
        f"fast_consensus:{call['network_id']}:{call['mint']}:{call['observation_id']}")
    return event


def events_for_observation(conn, *, observation_id: int) -> list[dict]:
    rows = conn.execute("""
        SELECT DISTINCT o.id AS observation_id, o.observed_at,
               m.network_id, m.mint,
               COALESCE(p.author_handle, o.author_handle) AS source_handle,
               p.author_role_at_capture AS source_role
        FROM memecoin_x_card_observations o
        JOIN memecoin_x_coin_mentions m ON m.source_observation_id=o.id
        LEFT JOIN memecoin_x_forward_posts p
          ON p.canonical_post_id=m.canonical_post_id
        WHERE o.id=? AND o.clock_domain='observed_real_utc'
          AND m.eligible=1 AND m.network_id IS NOT NULL AND m.mint IS NOT NULL
        ORDER BY m.network_id, m.mint
    """, (int(observation_id),)).fetchall()
    events = []
    current_actors = _actors_for_observation(conn, observation_id)
    actor_names = list(current_actors)
    current_counts = Counter(row["relationship"] for row in current_actors.values())
    for raw in rows:
        call = dict(raw)
        call["source_handle"] = str(call["source_handle"] or "").strip().lstrip("@").lower()
        events.append(_base_event(
            "capture_tag", call, evidence=[call["observation_id"]], actors=actor_names,
            relationship_counts=current_counts))
        if call["source_role"] == "tier2_source":
            events.append(_base_event(
                "tier2_scout", call, evidence=[call["observation_id"]], actors=actor_names,
                relationship_counts=current_counts))
        elif call["source_role"] == "official_context":
            events.append(_base_event(
                "official_context", call, evidence=[call["observation_id"]], actors=actor_names,
                relationship_counts=current_counts))
        consensus = _consensus_event(conn, call, current_actors)
        if consensus is not None:
            events.append(consensus)
    return events
