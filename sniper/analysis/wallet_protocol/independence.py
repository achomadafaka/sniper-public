"""§6.2 — Independence rule (eventSlug-only clustering).

Two positions are in the same cluster iff they share the same `eventSlug`. The
v1 24h+tag clause was dropped because it produced transitive over-clustering
(Russia + Venezuela + Pokrovsk merged via consecutive geopolitics-tagged dates
despite being independent geopolitical markets).

Cluster-reduced n is the inflation-corrected sample size used by H2's
independence-check column. The flag fires when concentrated clustering may
inflate the apparent power of H2 — operator should down-weight verdicts.

This module operates on `closed_positions` rows from /closed-positions.
The Polymarket API returns `eventSlug` directly on each row; no extra fetch
required.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


# Threshold at which we consider clustering "concentrated" (§6.2 limitation).
# Operational rule: if >=20% of positions sit in clusters of size 3 or more,
# the wallet's apparent independence is materially inflated.
CONCENTRATED_FRACTION_THRESHOLD = 0.20
CLUSTER_SIZE_FLAG_MIN = 3


@dataclass
class ClusterReductionResult:
    """Output of cluster_by_eventslug for a wallet's closed positions."""

    n_total: int
    n_clusters: int                                        # distinct eventSlugs (positions w/o eventSlug counted as singletons)
    n_in_concentrated_clusters: int                        # positions in clusters of size >= 3
    fraction_in_concentrated: float                        # n_in_concentrated_clusters / n_total
    flag: str                                              # "concentrated" | "normal"
    cluster_to_indices: dict[str, list[int]]               # eventSlug → list of position indices


def cluster_by_eventslug(
    positions: Sequence[dict],
) -> ClusterReductionResult:
    """Build eventSlug → indices map and tally concentration.

    Positions missing `eventSlug` are treated as singletons (their own
    cluster, identified by f"_orphan_{idx}"). They are independent by
    construction, which is the safer assumption — clustering with no
    grouping key would over-cluster.

    Args:
        positions: list of /closed-positions rows. Each must have `eventSlug`
            (or `eventSlug` may be missing/None, in which case singleton).

    Returns:
        ClusterReductionResult with the cluster map and concentration flag.
        n_clusters = distinct eventSlugs + orphan count.
    """
    n_total = len(positions)
    if n_total == 0:
        return ClusterReductionResult(
            n_total=0, n_clusters=0, n_in_concentrated_clusters=0,
            fraction_in_concentrated=0.0, flag="normal",
            cluster_to_indices={},
        )

    cluster_to_indices: dict[str, list[int]] = {}
    for i, pos in enumerate(positions):
        slug = pos.get("eventSlug") or pos.get("event_slug")
        if not slug:
            slug = f"_orphan_{i}"
        cluster_to_indices.setdefault(slug, []).append(i)

    n_in_concentrated = sum(
        len(idxs)
        for idxs in cluster_to_indices.values()
        if len(idxs) >= CLUSTER_SIZE_FLAG_MIN
    )
    fraction = n_in_concentrated / n_total
    flag = "concentrated" if fraction > CONCENTRATED_FRACTION_THRESHOLD else "normal"

    return ClusterReductionResult(
        n_total=n_total,
        n_clusters=len(cluster_to_indices),
        n_in_concentrated_clusters=n_in_concentrated,
        fraction_in_concentrated=fraction,
        flag=flag,
        cluster_to_indices=cluster_to_indices,
    )


def cluster_reduced_n(
    positions: Sequence[dict],
    cluster_map: dict[str, list[int]] | None = None,
) -> int:
    """Effective independent sample size after eventSlug clustering.

    Conservative rule: each cluster contributes 1 to n_eff regardless of size.
    This is the most pessimistic adjustment and matches "treat each event as
    one independent observation." Caller can use this in a sensitivity check
    on H2's CI: re-bootstrap with only one randomly-drawn position per cluster
    to confirm the verdict survives.
    """
    if cluster_map is None:
        cluster_map = cluster_by_eventslug(positions).cluster_to_indices
    return len(cluster_map)


def _position_ts(position: dict) -> int | None:
    value = (
        position.get("timestamp")
        or position.get("entry_ts")
        or position.get("entryTimestamp")
        or position.get("createdAt")
        or position.get("endDate")
    )
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value)
    if text.isdigit():
        return int(text)
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _position_nav(
    position: dict,
    leafs_by_cid: Mapping[str, Sequence[str]],
    nav_lookup: Mapping[str, str],
) -> str | None:
    cid = str(position.get("conditionId") or position.get("condition_id") or "")
    for leaf in leafs_by_cid.get(cid, []) or []:
        nav = nav_lookup.get(str(leaf))
        if nav:
            return str(nav)
    return None


def cluster_by_event_or_news_window(
    positions: Sequence[dict],
    leafs_by_cid: Mapping[str, Sequence[str]] | None,
    nav_lookup: Mapping[str, str] | None,
    *,
    window_seconds: int = 86_400,
) -> ClusterReductionResult:
    """Cluster positions by eventSlug or anchored same-nav 24h news windows.

    The old eventSlug-only rule misses correlated bets across separate
    markets that are reactions to one news cycle. The naive pairwise graph
    rule over-clusters by transitivity: day-0 Iran + day-1 Russia + day-2
    Venezuela can become one giant geopolitics cluster. This implementation
    uses anchored windows per nav category instead: a cluster starts at the
    first timestamp and includes same-nav positions within `window_seconds`
    of that anchor; later positions start a new window rather than extending
    the first one.
    """
    n_total = len(positions)
    if n_total == 0:
        return ClusterReductionResult(
            n_total=0, n_clusters=0, n_in_concentrated_clusters=0,
            fraction_in_concentrated=0.0, flag="normal",
            cluster_to_indices={},
        )

    leafs = leafs_by_cid or {}
    navs = nav_lookup or {}
    assigned: set[int] = set()
    cluster_to_indices: dict[str, list[int]] = {}

    event_groups: dict[str, list[int]] = {}
    nav_items: list[tuple[str, int, int]] = []
    for i, pos in enumerate(positions):
        slug = pos.get("eventSlug") or pos.get("event_slug")
        if slug:
            event_groups.setdefault(str(slug), []).append(i)
            continue
        nav = _position_nav(pos, leafs, navs)
        ts = _position_ts(pos)
        if nav and ts is not None:
            nav_items.append((nav, ts, i))
        else:
            key = f"_orphan_{i}"
            cluster_to_indices[key] = [i]
            assigned.add(i)

    for slug, indices in event_groups.items():
        key = f"event:{slug}"
        cluster_to_indices[key] = indices
        assigned.update(indices)

    by_nav: dict[str, list[tuple[int, int]]] = {}
    for nav, ts, idx in nav_items:
        if idx not in assigned:
            by_nav.setdefault(nav, []).append((ts, idx))

    for nav, items in by_nav.items():
        items = sorted(items)
        anchor_ts: int | None = None
        current: list[int] = []
        cluster_idx = 0
        for ts, idx in items:
            if anchor_ts is None or ts - anchor_ts > window_seconds:
                if current:
                    cluster_to_indices[f"nav:{nav}:{cluster_idx}"] = current
                    cluster_idx += 1
                anchor_ts = ts
                current = [idx]
            else:
                current.append(idx)
        if current:
            cluster_to_indices[f"nav:{nav}:{cluster_idx}"] = current

    n_in_concentrated = sum(
        len(idxs)
        for idxs in cluster_to_indices.values()
        if len(idxs) >= CLUSTER_SIZE_FLAG_MIN
    )
    fraction = n_in_concentrated / n_total
    flag = "concentrated" if fraction > CONCENTRATED_FRACTION_THRESHOLD else "normal"
    return ClusterReductionResult(
        n_total=n_total,
        n_clusters=len(cluster_to_indices),
        n_in_concentrated_clusters=n_in_concentrated,
        fraction_in_concentrated=fraction,
        flag=flag,
        cluster_to_indices=cluster_to_indices,
    )


__all__ = [
    "ClusterReductionResult",
    "CONCENTRATED_FRACTION_THRESHOLD",
    "CLUSTER_SIZE_FLAG_MIN",
    "cluster_by_eventslug",
    "cluster_by_event_or_news_window",
    "cluster_reduced_n",
]
