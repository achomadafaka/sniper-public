"""Read-only adapter for eligible Tier-1 Fomo v2 buy observations."""
from __future__ import annotations

from dataclasses import dataclass

from sniper.analysis.memecoin_x.normalize import is_valid_network_address, normalize_address



@dataclass(frozen=True)
class FomoBuyPage:
    buys: tuple[dict, ...]
    through_id: int
    source_rows: int


def fetch_eligible_fomo_buy_page(conn, *, after_id=0, limit=1000) -> FomoBuyPage:
    after_id = max(0, int(after_id))
    limit = int(limit)
    if limit < 1 or limit > 10_000:
        raise ValueError("limit must be between 1 and 10000")
    rows = conn.execute("""
        SELECT id, upstream_event_id, source_user_id, source_handle, network_id, mint,
               observed_at, clock_domain, capture_version, normalized_side,
               was_real_approved
        FROM memecoin_feed_observations_v2
        WHERE id > ?
          AND capture_version=2
          AND normalized_side='buy'
          AND was_real_approved=1
          AND clock_domain='observed_real_utc'
          AND upstream_event_id IS NOT NULL
          AND TRIM(upstream_event_id) <> ''
          AND source_user_id IS NOT NULL
          AND TRIM(source_user_id) <> ''
          AND source_handle IS NOT NULL
          AND TRIM(source_handle) <> ''
          AND network_id IS NOT NULL
          AND TRIM(network_id) <> ''
          AND mint IS NOT NULL
          AND TRIM(mint) <> ''
          AND observed_at IS NOT NULL
          AND TRIM(observed_at) <> ''
        ORDER BY id
        LIMIT ?
    """, (after_id, limit)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ("upstream_event_id", "source_user_id", "source_handle", "observed_at"):
            item[key] = str(item[key]).strip()
        if not all(item[key] for key in (
                "upstream_event_id", "source_user_id", "source_handle", "observed_at")):
            continue
        item["network_id"] = str(item["network_id"]).strip()
        raw_mint = str(item["mint"]).strip()
        if not is_valid_network_address(raw_mint, item["network_id"]):
            continue
        item["mint"] = normalize_address(raw_mint, item["network_id"])
        result.append(item)
    through_id = int(rows[-1]["id"]) if rows else after_id
    return FomoBuyPage(
        buys=tuple(result), through_id=through_id, source_rows=len(rows))


def fetch_eligible_fomo_buys(conn, *, after_id=0, limit=1000) -> list[dict]:
    return list(fetch_eligible_fomo_buy_page(
        conn, after_id=after_id, limit=limit).buys)
