"""Tag-level analytics over market_tags × market_outcomes × market_metadata.

Treats gamma tag slugs as opaque. No hardcoded tag values.

Phase 1 surface: `compute_tag_market_stats(conn)` — per-tag resolve-side
distribution + volume + co-occurrence. See
`docs/superpowers/specs/2026-04-24-tag-analytics.md` §3.1.

Phase 3 Workstream D: `persist_tag_market_stats` + `get_tag_market_stats`
write/read the `tag_market_stats` table whose schema lives in
``sniper/persistence/db_layer5.py:ensure_tag_market_stats``. The reader
returns a typed ``TagMarketStats`` dataclass; the dict-shape reader at
``sniper/analysis/wallet_protocol/tag_market_stats_reader.py`` (Workstream
A Task A11) is a separate, simpler API.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

YES_WIN_THRESHOLD = 0.97
NO_WIN_THRESHOLD = 0.97

_UNCLASSIFIED_LABEL = "(unclassified)"


def _fetchall(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.row_factory = prev


def _tag_to_cids(conn: sqlite3.Connection) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for row in _fetchall(conn, "SELECT tag, condition_id FROM market_tags"):
        tag = row["tag"]
        cid = row["condition_id"]
        if not tag or not cid:
            continue
        out[tag].add(cid)
    return out


def _cid_to_tags(conn: sqlite3.Connection) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for row in _fetchall(conn, "SELECT tag, condition_id FROM market_tags"):
        tag = row["tag"]
        cid = row["condition_id"]
        if not tag or not cid:
            continue
        out[cid].add(tag)
    return out


def _binary_resolutions(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """Return {condition_id: {"yes_p": float, "no_p": float}} for cids that
    have both a 'yes' and 'no' outcome row. Other resolutions are skipped."""
    bucket: dict[str, dict[str, float]] = defaultdict(dict)
    for row in _fetchall(
        conn,
        "SELECT condition_id, outcome, resolved_price FROM market_outcomes "
        "WHERE outcome IN ('yes', 'no')",
    ):
        cid = row["condition_id"]
        outcome = (row["outcome"] or "").lower()
        price = row["resolved_price"]
        if price is None:
            continue
        if outcome == "yes":
            bucket[cid]["yes_p"] = float(price)
        elif outcome == "no":
            bucket[cid]["no_p"] = float(price)
    return {
        cid: vals
        for cid, vals in bucket.items()
        if "yes_p" in vals and "no_p" in vals
    }


def _resolved_cids(conn: sqlite3.Connection) -> set[str]:
    rows = _fetchall(
        conn, "SELECT DISTINCT condition_id FROM market_outcomes"
    )
    return {r["condition_id"] for r in rows if r["condition_id"]}


def _volume_map(conn: sqlite3.Connection) -> dict[str, float]:
    rows = _fetchall(
        conn,
        "SELECT condition_id, volume_24h FROM market_metadata",
    )
    return {
        r["condition_id"]: float(r["volume_24h"] or 0.0)
        for r in rows
        if r["condition_id"]
    }


def _classify_binary(yes_p: float, no_p: float) -> str | None:
    """Return 'yes' if YES side resolved (yes_p > threshold),
    'no' if NO side resolved, else None (unresolved / tie / partial)."""
    if yes_p is None or no_p is None:
        return None
    if yes_p > YES_WIN_THRESHOLD and no_p < (1.0 - YES_WIN_THRESHOLD + 0.5):
        return "yes"
    if no_p > NO_WIN_THRESHOLD and yes_p < (1.0 - NO_WIN_THRESHOLD + 0.5):
        return "no"
    return None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.fmean(values))


def compute_tag_market_stats(
    conn: sqlite3.Connection,
    *,
    min_markets: int = 1,
) -> dict[str, dict[str, Any]]:
    """Per-tag resolve-side distribution. Spec §3.1.

    Returns {tag: {...}} where each tag row carries:
      - n_markets: distinct condition_ids tagged with this slug
      - n_resolved: of those, how many appear in market_outcomes
      - n_binary_resolved: of those, how many are binary (yes+no rows) with clear winner
      - resolved_yes_count: binary resolutions with yes side winning
      - resolved_no_count: binary resolutions with no side winning
      - yes_share: resolved_yes / (resolved_yes + resolved_no) [None if denom=0]
      - avg_resolved_price_yes_side: mean yes_p over binary markets with clear winner
      - median_market_volume_24h: over tagged markets with metadata rows
      - distinct_co_occurring_tags_top5: [(other_tag, count), ...]

    `min_markets` filters out tags below the sample threshold. Default 1
    keeps every tag so callers can inspect long-tail slugs.
    """
    tag_cids = _tag_to_cids(conn)
    cid_tags = _cid_to_tags(conn)
    binaries = _binary_resolutions(conn)
    resolved_any = _resolved_cids(conn)
    volume_map = _volume_map(conn)

    stats: dict[str, dict[str, Any]] = {}
    for tag, cids in tag_cids.items():
        n_markets = len(cids)
        if n_markets < min_markets:
            continue
        n_resolved = sum(1 for cid in cids if cid in resolved_any)

        yes_count = 0
        no_count = 0
        yes_side_prices: list[float] = []
        n_binary_resolved = 0
        for cid in cids:
            bin_row = binaries.get(cid)
            if not bin_row:
                continue
            side = _classify_binary(bin_row["yes_p"], bin_row["no_p"])
            if side is None:
                continue
            n_binary_resolved += 1
            yes_side_prices.append(bin_row["yes_p"])
            if side == "yes":
                yes_count += 1
            elif side == "no":
                no_count += 1

        denom = yes_count + no_count
        yes_share = (yes_count / denom) if denom else None

        volumes = [volume_map[cid] for cid in cids if cid in volume_map]
        median_volume = _median(volumes)

        co_counter: Counter[str] = Counter()
        for cid in cids:
            for other in cid_tags.get(cid, set()):
                if other == tag:
                    continue
                co_counter[other] += 1
        co_top5 = co_counter.most_common(5)

        stats[tag] = {
            "tag": tag,
            "n_markets": n_markets,
            "n_resolved": n_resolved,
            "n_binary_resolved": n_binary_resolved,
            "resolved_yes_count": yes_count,
            "resolved_no_count": no_count,
            "yes_share": yes_share,
            "avg_resolved_price_yes_side": _mean(yes_side_prices),
            "median_market_volume_24h": median_volume,
            "distinct_co_occurring_tags_top5": co_top5,
        }
    return stats


@dataclass
class TagMarketStats:
    """Persisted shape of a per-tag market-stats row.

    Mirrors the columns in ``sniper/persistence/db_layer5.py:
    ensure_tag_market_stats``. Constructed either directly or via
    ``_from_compute_dict`` when persisting the output of
    :func:`compute_tag_market_stats`.

    Notes:
    - ``computed_at`` is unix epoch seconds. ``None`` is allowed at
      construction time so callers can defer to "now" inside
      :func:`persist_tag_market_stats`.
    - ``top_co_tags`` is the in-memory list of ``(tag, count)`` pairs;
      it serializes to a JSON array of ``[tag, count]`` arrays in the
      ``top_co_tags_json`` column.
    - ``avg_resolved_price_yes`` is the schema column name; the
      ``compute_tag_market_stats`` output uses
      ``avg_resolved_price_yes_side`` — :func:`_from_compute_dict`
      handles the rename.
    """

    tag: str
    n_markets: int
    n_resolved: int
    resolved_yes_count: int
    yes_share: float | None
    avg_resolved_price_yes: float | None
    median_market_volume_24h: float | None
    top_co_tags: list[tuple[str, int]] = field(default_factory=list)
    computed_at: int | None = None
    inputs_version: str = "v1"


def _from_compute_dict(tag: str, row: dict[str, Any]) -> TagMarketStats:
    """Build a TagMarketStats from one entry of compute_tag_market_stats."""
    return TagMarketStats(
        tag=tag,
        n_markets=int(row.get("n_markets") or 0),
        n_resolved=int(row.get("n_resolved") or 0),
        resolved_yes_count=int(row.get("resolved_yes_count") or 0),
        yes_share=row.get("yes_share"),
        avg_resolved_price_yes=row.get("avg_resolved_price_yes_side"),
        median_market_volume_24h=row.get("median_market_volume_24h"),
        top_co_tags=[
            (t, int(c)) for t, c in (row.get("distinct_co_occurring_tags_top5") or [])
        ],
    )


def _normalize_to_dataclasses(
    payload: dict[str, dict[str, Any]] | Iterable[TagMarketStats],
) -> list[TagMarketStats]:
    """Accept compute_tag_market_stats output dict OR an iterable of
    TagMarketStats. Returns a list of TagMarketStats."""
    if isinstance(payload, dict):
        return [_from_compute_dict(tag, row) for tag, row in payload.items()]
    return list(payload)


def persist_tag_market_stats(
    conn: sqlite3.Connection,
    stats: dict[str, dict[str, Any]] | Iterable[TagMarketStats],
    *,
    computed_at: int | None = None,
    inputs_version: str = "v1",
) -> None:
    """Upsert per-tag stats into ``tag_market_stats``.

    Idempotent: re-running with the same input produces identical rows
    (PK=tag, ON CONFLICT DO UPDATE SET …). Each row is committed atomically.

    Args:
        conn: SQLite connection. Caller must ensure the table exists
            (``ensure_tag_market_stats(conn)`` from
            ``sniper.persistence.db_layer5``).
        stats: Either the dict shape returned by ``compute_tag_market_stats``
            or an iterable of pre-built ``TagMarketStats`` instances.
        computed_at: Default unix-epoch stamp applied when an instance has
            ``computed_at is None``. ``None`` means "use ``int(time.time())``
            at write time" — useful for fresh persistence runs.
        inputs_version: Default version tag applied when an instance has
            an empty/None inputs_version.
    """
    items = _normalize_to_dataclasses(stats)
    if not items:
        return

    default_ts = computed_at if computed_at is not None else int(time.time())

    rows: list[tuple] = []
    for item in items:
        ts = item.computed_at if item.computed_at is not None else default_ts
        ver = item.inputs_version or inputs_version
        co_json = json.dumps(
            [list(t) if isinstance(t, tuple) else t for t in item.top_co_tags]
        )
        rows.append(
            (
                item.tag,
                int(item.n_markets),
                int(item.n_resolved),
                int(item.resolved_yes_count),
                item.yes_share,
                item.avg_resolved_price_yes,
                item.median_market_volume_24h,
                co_json,
                int(ts),
                ver,
            )
        )

    conn.executemany(
        """
        INSERT INTO tag_market_stats (
            tag, n_markets, n_resolved, resolved_yes_count,
            yes_share, avg_resolved_price_yes, median_market_volume_24h,
            top_co_tags_json, computed_at, inputs_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tag) DO UPDATE SET
            n_markets = excluded.n_markets,
            n_resolved = excluded.n_resolved,
            resolved_yes_count = excluded.resolved_yes_count,
            yes_share = excluded.yes_share,
            avg_resolved_price_yes = excluded.avg_resolved_price_yes,
            median_market_volume_24h = excluded.median_market_volume_24h,
            top_co_tags_json = excluded.top_co_tags_json,
            computed_at = excluded.computed_at,
            inputs_version = excluded.inputs_version
        """,
        rows,
    )
    conn.commit()


def get_tag_market_stats(
    conn: sqlite3.Connection, tag: str
) -> TagMarketStats | None:
    """Return the persisted ``TagMarketStats`` for ``tag`` or ``None``.

    The Workstream A reader at
    ``sniper/analysis/wallet_protocol/tag_market_stats_reader.py``
    returns a dict instead — that's the simpler shape for raw analyzer
    consumption. This reader is the typed-dataclass surface.
    """
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM tag_market_stats WHERE tag = ?", (tag,),
        ).fetchone()
    finally:
        conn.row_factory = prev

    if row is None:
        return None
    raw = json.loads(row["top_co_tags_json"] or "[]")
    co_tags: list[tuple[str, int]] = [
        (t, int(c)) for t, c in raw
    ]
    return TagMarketStats(
        tag=row["tag"],
        n_markets=row["n_markets"],
        n_resolved=row["n_resolved"],
        resolved_yes_count=row["resolved_yes_count"],
        yes_share=row["yes_share"],
        avg_resolved_price_yes=row["avg_resolved_price_yes"],
        median_market_volume_24h=row["median_market_volume_24h"],
        top_co_tags=co_tags,
        computed_at=row["computed_at"],
        inputs_version=row["inputs_version"],
    )


def summarize_coverage(
    conn: sqlite3.Connection,
    stats: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Report how much of the resolved-market universe is tag-covered.

    Surface the sparsity explicitly so downstream analysis does not mistake
    low counts for low signal.
    """
    if stats is None:
        stats = compute_tag_market_stats(conn)
    resolved_any = _resolved_cids(conn)
    binaries = _binary_resolutions(conn)
    cid_tags = _cid_to_tags(conn)
    tagged_resolved = sum(1 for cid in resolved_any if cid in cid_tags)
    tagged_binary = sum(1 for cid in binaries if cid in cid_tags)
    return {
        "distinct_tags": len(stats),
        "resolved_markets_total": len(resolved_any),
        "resolved_markets_with_tags": tagged_resolved,
        "binary_markets_total": len(binaries),
        "binary_markets_with_tags": tagged_binary,
        "tag_coverage_pct_of_resolved": (
            (tagged_resolved / len(resolved_any)) if resolved_any else None
        ),
    }
