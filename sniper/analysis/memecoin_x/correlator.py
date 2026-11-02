"""Forward correlator: join eligible X coin-mentions to later eligible Tier-1 Fomo buys.

Evidence contract (display-only; never gates trading):
- X mention time comes from card_observations.observed_at via source_observation_id (never
  forward_posts.first_observed_at). Both sides must be clock_domain='observed_real_utc'.
- Exact normalized (network, mint) match only; ticker-only mentions are never links.
- Buyers restricted to the point-in-time Tier-1 roster (fomo_handle -> X handle).
- Require strictly positive lead within the horizon (default 6h). posted_at is never used.
- Lanes: 'amplified' = the buyer's own pre-buy repost/quote/reply on the source post;
  'coincidence' = mention preceded the buy without buyer amplification (never labeled causal).
- Self-post/self-buy (source author == buyer) is a seed, not an upstream edge -> skipped.
- Official-context source -> stored but eligible=0 (excluded from source rankings).
Idempotent: forward_links dedupe on the stable (canonical, fomo-event, buyer, network, mint) key.
"""
from __future__ import annotations

from datetime import datetime

from sniper.analysis.memecoin_x.normalize import (
    is_valid_network_address,
    normalize_address,
    normalize_handle,
)

DEFAULT_HORIZON_S = 21600  # 6 hours


def _parse(iso):
    value = datetime.fromisoformat(iso)
    if value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


def _normalize_buy(buy):
    if not isinstance(buy, dict):
        return None
    item = dict(buy)
    for key in ("upstream_event_id", "source_user_id", "observed_at"):
        value = str(item.get(key) or "").strip()
        if not value:
            return None
        item[key] = value
    item["source_handle"] = normalize_handle(item.get("source_handle"))
    item["network_id"] = str(item.get("network_id") or "").strip()
    raw_mint = str(item.get("mint") or "").strip()
    if (not item["source_handle"] or not item["network_id"]
            or not is_valid_network_address(raw_mint, item["network_id"])):
        return None
    item["mint"] = normalize_address(raw_mint, item["network_id"])
    return item


def _eligible_buy(buy):
    return bool(
        buy.get("capture_version") == 2
        and buy.get("normalized_side") == "buy"
        and buy.get("was_real_approved") == 1
        and buy.get("clock_domain") == "observed_real_utc"
        and buy.get("upstream_event_id")
        and buy.get("source_user_id")
        and buy.get("source_handle")
        and buy.get("network_id")
        and buy.get("mint")
        and buy.get("observed_at")
    )


def _buyer_snapshot(conn, buy, identity_map):
    key = (buy["upstream_event_id"], buy["source_user_id"])
    row = conn.execute("""
        SELECT buyer_x_handle_at_obs, tier1_at_obs
        FROM memecoin_x_fomo_buyer_snapshots
        WHERE fomo_upstream_event_id=? AND buying_source_user_id=?
    """, key).fetchone()
    if row:
        return row

    buyer_x = identity_map.get(buy["source_handle"])
    if not buyer_x:
        prior = conn.execute("""
            SELECT buyer_x_handle_at_obs
            FROM memecoin_x_fomo_buyer_snapshots
            WHERE buying_source_user_id=? AND buyer_x_handle_at_obs IS NOT NULL
            ORDER BY id DESC LIMIT 1
        """, (buy["source_user_id"],)).fetchone()
        buyer_x = prior["buyer_x_handle_at_obs"] if prior else None
    conn.execute("""
        INSERT OR IGNORE INTO memecoin_x_fomo_buyer_snapshots
            (fomo_upstream_event_id, buying_source_user_id, source_handle_at_obs,
             buyer_x_handle_at_obs, tier1_at_obs, fomo_observed_at, created_at)
        VALUES (?,?,?,?,1,?,?)
    """, (*key, buy["source_handle"], buyer_x, buy["observed_at"], buy["observed_at"]))
    return conn.execute("""
        SELECT buyer_x_handle_at_obs, tier1_at_obs
        FROM memecoin_x_fomo_buyer_snapshots
        WHERE fomo_upstream_event_id=? AND buying_source_user_id=?
    """, key).fetchone()


def build_forward_links(x_conn, fomo_buys, *, horizon_s=DEFAULT_HORIZON_S) -> dict:
    """fomo_buys: iterable of dicts with keys upstream_event_id, source_handle,
    source_user_id, network_id, mint, observed_at, clock_domain, capture_version,
    normalized_side, and was_real_approved."""
    fomo_to_x = {}
    for r in x_conn.execute("SELECT handle, role, fomo_handle FROM memecoin_x_accounts"):
        if r["fomo_handle"]:
            fomo_to_x[r["fomo_handle"].lower()] = r["handle"].lower()

    rows = x_conn.execute("""
        SELECT m.id AS mid, m.canonical_post_id AS pid, m.network_id AS net, m.mint AS mint,
               o.observed_at AS x_obs, p.author_handle AS src_author,
               p.author_role_at_capture AS src_role
        FROM memecoin_x_coin_mentions m
        JOIN memecoin_x_card_observations o ON o.id = m.source_observation_id
        LEFT JOIN memecoin_x_forward_posts p ON p.canonical_post_id = m.canonical_post_id
        WHERE m.eligible = 1 AND o.clock_domain = 'observed_real_utc'
    """).fetchall()
    by_coin = {}
    for m in rows:
        by_coin.setdefault((m["net"], m["mint"]), []).append(m)

    created = upgraded = amplified = coincidence = 0
    for raw_buy in fomo_buys:
        buy = _normalize_buy(raw_buy)
        if not buy:
            continue
        if not _eligible_buy(buy):
            continue
        fomo_obs = buy.get("observed_at")
        try:
            t_buy = _parse(fomo_obs)
        except (TypeError, ValueError):
            continue
        buyer = _buyer_snapshot(x_conn, buy, fomo_to_x)
        if (not buyer or not buyer["tier1_at_obs"]
                or not buyer["buyer_x_handle_at_obs"]):
            continue  # only buyers approved at Fomo observation time
        buyer_x = buyer["buyer_x_handle_at_obs"].lower()
        for m in by_coin.get((buy.get("network_id"), buy.get("mint")), []):
            try:
                lead = (t_buy - _parse(m["x_obs"])).total_seconds()
            except (TypeError, ValueError):
                continue
            if lead <= 0 or lead > horizon_s:
                continue
            src = (m["src_author"] or "").lower()
            if src and src == buyer_x:
                continue  # self-post/self-buy = seed, not an upstream edge
            src_role = m["src_role"]
            excl = "official_context_source" if src_role == "official_context" else None
            eligible = 0 if excl else 1
            rel = x_conn.execute("""
                SELECT relationship FROM memecoin_x_relationships
                WHERE canonical_post_id=? AND actor_handle=? AND clock_domain='observed_real_utc'
                  AND actor_is_tier1_at_obs=1
                  AND relationship IN ('repost','quote','reply')
                  AND observed_at >= ? AND observed_at < ?
                ORDER BY observed_at LIMIT 1
            """, (m["pid"], buyer_x, m["x_obs"], fomo_obs)).fetchone()
            lane = "amplified" if rel else "coincidence"
            buyer_rel = rel["relationship"] if rel else None
            existing = x_conn.execute("""
                SELECT id, evidence_lane, eligible FROM memecoin_x_forward_links
                WHERE canonical_post_id=? AND fomo_upstream_event_id=?
                  AND buying_source_user_id=?
                  AND COALESCE(network_id,'')=COALESCE(?, '')
                  AND COALESCE(mint,'')=COALESCE(?, '')
            """, (m["pid"], buy["upstream_event_id"], buy["source_user_id"],
                  m["net"], m["mint"])).fetchone()
            if not existing:
                x_conn.execute("""
                    INSERT INTO memecoin_x_forward_links
                    (canonical_post_id, coin_mention_id, fomo_upstream_event_id,
                     buying_source_user_id, network_id, mint, x_observed_at, x_clock_domain,
                     fomo_observed_at, fomo_clock_domain, lead_seconds, evidence_lane,
                     buyer_relationship, tier1_at_obs, source_role_at_obs, eligible, exclusion_reason)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (m["pid"], m["mid"], buy["upstream_event_id"],
                      buy["source_user_id"], m["net"], m["mint"], m["x_obs"],
                      "observed_real_utc", fomo_obs, "observed_real_utc", lead, lane,
                      buyer_rel, buyer["tier1_at_obs"], src_role, eligible, excl))
                created += 1
                if eligible and lane == "amplified":
                    amplified += 1
                elif eligible:
                    coincidence += 1
            elif (existing["eligible"] and eligible
                  and existing["evidence_lane"] == "coincidence" and lane == "amplified"):
                x_conn.execute("""
                    UPDATE memecoin_x_forward_links
                    SET evidence_lane='amplified', buyer_relationship=?
                    WHERE id=? AND evidence_lane='coincidence'
                """, (buyer_rel, existing["id"]))
                upgraded += 1
    x_conn.commit()
    return {"created": created, "upgraded": upgraded,
            "amplified": amplified, "coincidence": coincidence}
