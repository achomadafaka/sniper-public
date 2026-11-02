"""Read-only rolling source-graph models over forward-observed X evidence."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations


WINDOWS = {"24h": 86400, "7d": 604800, "30d": 2592000}
_RELATIONSHIPS = ("direct", "repost", "quote", "reply")
_DEGRADED_CAPTURE_STATUSES = frozenset({
    "coverage_gap", "wrong_feed", "logged_out", "challenge", "rate_limited",
    "page_unready", "clock_unhealthy", "parser_error", "error",
})


def _aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("timestamp must be timezone-aware ISO") from exc
    if parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware ISO")
    return parsed.astimezone(timezone.utc)


def _in_window(value, since: datetime, as_of: datetime) -> bool:
    try:
        stamp = _aware(value)
    except ValueError:
        return False
    return since <= stamp < as_of


def _call_rows(conn, since: datetime, as_of: datetime):
    rows = conn.execute("""
        SELECT m.id AS mention_id, m.canonical_post_id, m.network_id, m.mint,
               m.source_observation_id, o.observed_at,
               COALESCE(p.author_handle, o.author_handle) AS author_handle,
               p.author_role_at_capture AS role
        FROM memecoin_x_coin_mentions m
        JOIN memecoin_x_card_observations o ON o.id=m.source_observation_id
        LEFT JOIN memecoin_x_forward_posts p
          ON p.canonical_post_id=m.canonical_post_id
        WHERE m.eligible=1 AND m.network_id IS NOT NULL AND m.mint IS NOT NULL
          AND o.clock_domain='observed_real_utc'
    """).fetchall()
    calls = {}
    for row in rows:
        if not _in_window(row["observed_at"], since, as_of):
            continue
        key = (row["canonical_post_id"], row["network_id"], row["mint"])
        current = calls.get(key)
        if current is None or _aware(row["observed_at"]) < _aware(current["observed_at"]):
            calls[key] = dict(row)
    return calls


def _link_rows(conn, calls, since: datetime, as_of: datetime):
    call_keys = set(calls)
    result = []
    for row in conn.execute("""
        SELECT canonical_post_id, network_id, mint, fomo_upstream_event_id,
               buying_source_user_id, fomo_observed_at, evidence_lane,
               tier1_at_obs, eligible
        FROM memecoin_x_forward_links
        WHERE eligible=1 AND tier1_at_obs=1
          AND x_clock_domain='observed_real_utc'
          AND fomo_clock_domain='observed_real_utc'
    """):
        key = (row["canonical_post_id"], row["network_id"], row["mint"])
        if key in call_keys and _in_window(row["fomo_observed_at"], since, as_of):
            result.append(dict(row))
    return result


def _relationship_rows(conn, calls, since: datetime, as_of: datetime):
    observation_to_calls = defaultdict(set)
    post_to_calls = defaultdict(set)
    for key, call in calls.items():
        observation_to_calls[call["source_observation_id"]].add(key)
        post_to_calls[call["canonical_post_id"]].add(key)
    result = []
    for row in conn.execute("""
        SELECT id, canonical_post_id, actor_handle, actor_is_tier1_at_obs,
               relationship, observed_at, source_observation_id
        FROM memecoin_x_relationships
        WHERE clock_domain='observed_real_utc'
    """):
        if not _in_window(row["observed_at"], since, as_of):
            continue
        keys = observation_to_calls.get(row["source_observation_id"])
        if not keys:
            keys = post_to_calls.get(row["canonical_post_id"], set())
        for key in keys:
            if _aware(row["observed_at"]) < _aware(calls[key]["observed_at"]):
                continue
            result.append((key, dict(row)))
    return result


def _source_rows(calls, links, relationships):
    grouped = {}
    call_to_group = {}
    for key, call in calls.items():
        handle = str(call["author_handle"] or "").strip().lstrip("@").lower()
        role = call["role"]
        group_key = (handle, role, call["network_id"])
        group = grouped.setdefault(group_key, {
            "handle": handle,
            "role": role,
            "network_id": call["network_id"],
            "call_keys": set(),
            "followed_keys": set(),
            "amplified_links": 0,
            "coincidence_links": 0,
            "buyers": set(),
            "relationships": {name: 0 for name in _RELATIONSHIPS},
        })
        group["call_keys"].add(key)
        call_to_group[key] = group_key

    for link in links:
        key = (link["canonical_post_id"], link["network_id"], link["mint"])
        group_key = call_to_group.get(key)
        if group_key is None:
            continue
        group = grouped[group_key]
        group["followed_keys"].add(key)
        lane = link["evidence_lane"]
        if lane == "amplified":
            group["amplified_links"] += 1
        elif lane == "coincidence":
            group["coincidence_links"] += 1
        group["buyers"].add(link["buying_source_user_id"])

    for key, relationship in relationships:
        group_key = call_to_group.get(key)
        if group_key is not None and relationship["relationship"] in _RELATIONSHIPS:
            grouped[group_key]["relationships"][relationship["relationship"]] += 1

    rows = []
    for group in grouped.values():
        exact = len(group["call_keys"])
        followed = len(group["followed_keys"])
        row = {
            "handle": group["handle"],
            "role": group["role"],
            "network_id": group["network_id"],
            "exact_calls": exact,
            "followed_calls": followed,
            "follow_through_rate": followed / exact if exact else None,
            "amplified_links": group["amplified_links"],
            "coincidence_links": group["coincidence_links"],
            "distinct_tier1_buyers": len(group["buyers"]),
        }
        row.update({f"{name}_count": group["relationships"][name]
                    for name in _RELATIONSHIPS})
        rows.append(row)
    rows.sort(key=lambda row: (-row["distinct_tier1_buyers"],
                               -row["amplified_links"], row["handle"].casefold(),
                               row["network_id"]))
    return rows


def _cooccurrence(calls, relationships):
    actor_coins = defaultdict(set)
    for call_key, relationship in relationships:
        if relationship["actor_is_tier1_at_obs"] != 1:
            continue
        actor = str(relationship["actor_handle"] or "").strip().lstrip("@").lower()
        if actor:
            actor_coins[actor].add((call_key[1], call_key[2]))
    rows = []
    for actor_a, actor_b in combinations(sorted(actor_coins, key=str.casefold), 2):
        shared = actor_coins[actor_a] & actor_coins[actor_b]
        if shared:
            rows.append({
                "actor_a": actor_a,
                "actor_b": actor_b,
                "shared_exact_coins": len(shared),
                "actor_a_exact_coins": len(actor_coins[actor_a]),
                "actor_b_exact_coins": len(actor_coins[actor_b]),
            })
    rows.sort(key=lambda row: (-row["shared_exact_coins"],
                               row["actor_a"].casefold(), row["actor_b"].casefold()))
    return rows


def _coverage(conn, calls, since: datetime, as_of: datetime):
    capture_runs = healthy_clock_runs = coverage_gap_runs = 0
    for row in conn.execute("""
        SELECT started_at, clock_domain, status FROM memecoin_x_capture_runs
    """):
        if not _in_window(row["started_at"], since, as_of):
            continue
        capture_runs += 1
        healthy_clock_runs += int(row["clock_domain"] == "observed_real_utc")
        coverage_gap_runs += int(row["status"] in _DEGRADED_CAPTURE_STATUSES)

    diagnostic_observations = sum(
        1 for row in conn.execute("SELECT observed_at FROM memecoin_x_capture_diagnostics")
        if _in_window(row["observed_at"], since, as_of)
    )
    eligible_buys = set()
    for row in conn.execute("""
        SELECT fomo_upstream_event_id, buying_source_user_id, fomo_observed_at
        FROM memecoin_x_fomo_buyer_snapshots
        WHERE tier1_at_obs=1 AND buyer_x_handle_at_obs IS NOT NULL
    """):
        if _in_window(row["fomo_observed_at"], since, as_of):
            eligible_buys.add((row["fomo_upstream_event_id"], row["buying_source_user_id"]))
    linked_buys = set()
    for row in conn.execute("""
        SELECT fomo_upstream_event_id, buying_source_user_id, fomo_observed_at
        FROM memecoin_x_forward_links
        WHERE eligible=1 AND tier1_at_obs=1
          AND x_clock_domain='observed_real_utc'
          AND fomo_clock_domain='observed_real_utc'
    """):
        key = (row["fomo_upstream_event_id"], row["buying_source_user_id"])
        if key in eligible_buys and _in_window(row["fomo_observed_at"], since, as_of):
            linked_buys.add(key)
    denominator = len(eligible_buys)
    return {
        "capture_runs": capture_runs,
        "healthy_clock_runs": healthy_clock_runs,
        "coverage_gap_runs": coverage_gap_runs,
        "eligible_observations": len({call["source_observation_id"] for call in calls.values()}),
        "diagnostic_observations": diagnostic_observations,
        "eligible_tier1_buys": denominator,
        "linked_tier1_buys": len(linked_buys),
        "buy_coverage_rate": len(linked_buys) / denominator if denominator else None,
    }


def build_window(conn, *, since: str, as_of: str, label: str) -> dict:
    """Build one half-open [since, as_of) raw-count window."""
    since_dt, as_of_dt = _aware(since), _aware(as_of)
    if since_dt >= as_of_dt:
        raise ValueError("since must be before as_of")
    calls = _call_rows(conn, since_dt, as_of_dt)
    links = _link_rows(conn, calls, since_dt, as_of_dt)
    relationships = _relationship_rows(conn, calls, since_dt, as_of_dt)
    rows = _source_rows(calls, links, relationships)
    suggestions = []
    official_context = []
    sources = []
    for row in rows:
        if row["role"] == "official_context":
            official_context.append(row)
        elif row["role"] in {None, "suggested"}:
            suggestions.append({**row, "needs_review": True})
        else:
            sources.append(row)
    return {
        "label": label,
        "since": since_dt.isoformat(),
        "as_of": as_of_dt.isoformat(),
        "sources": sources,
        "suggestions": suggestions,
        "official_context": official_context,
        "actor_cooccurrence": _cooccurrence(calls, relationships),
        "coverage": _coverage(conn, calls, since_dt, as_of_dt),
    }


def build_source_graph(conn, *, as_of: str) -> dict:
    as_of_dt = _aware(as_of)
    windows = {}
    for label, seconds in WINDOWS.items():
        since = as_of_dt - timedelta(seconds=seconds)
        windows[label] = build_window(
            conn, since=since.isoformat(), as_of=as_of_dt.isoformat(), label=label)
    return {"as_of": as_of_dt.isoformat(), "windows": windows}
