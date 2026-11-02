"""Desk offchain heat, per-coin CODE part (design docs/superpowers/specs/2026-10-21-desk-offchain-heat-seat-design.md
§2, §7 phasing): attention measured off the tape, per mint, from data the desk already collects. COLLECT AND
REPORT ONLY: nothing here reaches a JUDGE sheet, a gate or any money path. Pure code; the worker
(scripts/memecoin_desk_offchain.py) owns the reads and its own DB.

Paid is never organic: DexScreener boosts, ads and profiles are bought and feed only paid_* fields;
community takeovers are a listing; X mentions are organic except from accounts on the operator's
promoter list. A source that was not healthy over the window makes its fields null (missing), never 0.
"""
from __future__ import annotations

import json

OFFCHAIN_VERSION = "desk-offchain-coin-v1"
WINDOW_S = 60 * 60            # DexScreener presence: boosts, ads, profiles, takeovers seen in the last hour
X_WINDOW_S = 15 * 60
CRAWL_MAX_AGE_S = 6 * 3600
DEX_FRESH_S = 180.0           # every DexScreener endpoint needs a 200 poll this recent
X_FRESH_S = 120.0             # the X producer heartbeat must be this recent
PAID_ENDPOINTS = {"/token-boosts/top/v1": "boost", "/token-boosts/latest/v1": "boost",
                  "/ads/latest/v1": "ad", "/token-profiles/latest/v1": "profile",
                  "/community-takeovers/latest/v1": "takeover"}


def dex_healthy(polls, *, now):
    """True only when every per-token endpoint had a successful (200) poll in the last DEX_FRESH_S."""
    last_ok = {}
    for endpoint, received_at, status in polls:
        if status == 200 and received_at <= now:
            last_ok[endpoint] = max(last_ok.get(endpoint, 0.0), received_at)
    return all(now - last_ok.get(ep, -1e18) <= DEX_FRESH_S for ep in PAID_ENDPOINTS)


def boost_units(payload):
    """DexScreener's boost total for the token (its own units, not USD); None when absent."""
    try:
        p = json.loads(payload) if isinstance(payload, str) else payload
    except ValueError:
        return None
    for key in ("totalAmount", "amount"):
        v = p.get(key) if isinstance(p, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            return float(v)
    return None


def coin_row(mint, *, now, dex_items, dex_ok, x_links, x_ok, promoters, crawl):
    """One per-coin row. dex_items: [(endpoint, received_at, payload_json)] for this mint;
    x_links: [(tweet_id, handle, received_at)] token_meta links for this mint; crawl: (intent, known_at)
    or None; promoters: set of lower-cased handles."""
    row = {"mint": mint, "network_id": "solana", "window_end": now, "offchain_version": OFFCHAIN_VERSION}
    if dex_ok:
        recent = [(PAID_ENDPOINTS[e], p) for e, t, p in dex_items if e in PAID_ENDPOINTS and now - WINDOW_S < t <= now]
        units = [u for kind, p in recent if kind == "boost" for u in [boost_units(p)] if u is not None]
        row.update(paid_boost_units=max(units) if units else 0.0,
                   paid_ads=any(k == "ad" for k, _ in recent), paid_profile=any(k == "profile" for k, _ in recent),
                   listing_takeover=any(k == "takeover" for k, _ in recent), dex_state="ok")
    else:
        row.update(paid_boost_units=None, paid_ads=None, paid_profile=None, listing_takeover=None, dex_state="missing")
    if x_ok:
        recent = {(t, (h or "").lower()) for t, h, r in x_links if now - X_WINDOW_S < r <= now and t}
        organic = {(t, h) for t, h in recent if h not in promoters}
        row.update(x_mentions_15m=len({t for t, _ in organic}), x_accounts_15m=len({h for _, h in organic if h}),
                   x_promoter_mentions_15m=len({t for t, h in recent if h in promoters}), x_state="ok")
    else:
        row.update(x_mentions_15m=None, x_accounts_15m=None, x_promoter_mentions_15m=None, x_state="missing")
    if crawl and crawl[1] <= now and now - crawl[1] <= CRAWL_MAX_AGE_S:
        row.update(crawler_intent=crawl[0], crawler_known_at=crawl[1])
    else:
        row.update(crawler_intent=None, crawler_known_at=None)
    return row


SIGNATURE = ("paid_boost_units", "paid_ads", "paid_profile", "listing_takeover", "dex_state", "x_mentions_15m",
             "x_accounts_15m", "x_promoter_mentions_15m", "x_state", "crawler_intent", "crawler_known_at")


def signature(row):
    """What a stored row means; an unchanged signature is not written again."""
    return tuple(row.get(k) for k in SIGNATURE)
