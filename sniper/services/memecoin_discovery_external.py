"""Coin Radar Stage 2: external discovery.

Surfaces hot coins BEFORE any of our wallets touch them, from two sources:
  - DexScreener token-boosts (promoted/trending) for the priced chains (solana/base/…), enriched via
    the batch stats endpoint.
  - GeckoTerminal trending + new pools for Robinhood 4663 (the DexScreener-blind chain), whose stats
    come back inline.

All fetchers are injectable (unit-testable, no live endpoint required). Runs on a low-cadence worker so
it shares the per-IP GT rate budget with the funded runner rather than starving it. Upserts into
memecoin_external_discovery; the radar `/universe` endpoint unions those rows in.
"""
from __future__ import annotations

from sniper.execution.solana_quote import (
    _http as _dex_http, DEX_CHAIN_TO_FOMO, dexscreener_stats_batch_keyed, quote_for, norm_mint,
)
from sniper.execution.geckoterminal_quote import gt_get_json, GT_NETWORK, gt_stats_batch_keyed
from sniper.util.clock import real_utc_now_iso

DEX_BOOST_URLS = ["https://api.dexscreener.com/token-boosts/top/v1",
                  "https://api.dexscreener.com/token-boosts/latest/v1"]
GT_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/{net}/{kind}"


def dexscreener_boost_keys(http=None):
    """Boosted/promoted tokens -> [(network_id, mint)] for chains we map (DEX_CHAIN_TO_FOMO). The
    'trending' proxy for priced chains. Best-effort; deduped; [] on failure."""
    http = http or _dex_http
    keys, seen = [], set()
    for url in DEX_BOOST_URLS:
        try:
            status, payload, _date = http("GET", url)
        except Exception:
            continue
        if status != 200:
            continue
        items = payload if isinstance(payload, list) else (
            payload.get("data") if isinstance(payload, dict) else [])
        for it in items or []:
            if not isinstance(it, dict):
                continue
            net = DEX_CHAIN_TO_FOMO.get(it.get("chainId"))
            mint = it.get("tokenAddress")
            if not net or not mint:
                continue
            k = (net, norm_mint(mint))
            if k in seen:
                continue
            seen.add(k)
            keys.append(k)
    return keys


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_gt_pools(payload, network_id):
    """Parse a GT trending_pools / new_pools response -> normalized discovery rows (stats inline)."""
    rows = []
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    for d in data or []:
        if not isinstance(d, dict):
            continue
        attr = d.get("attributes") or {}
        rel = ((d.get("relationships") or {}).get("base_token") or {}).get("data") or {}
        tid = rel.get("id") or ""                     # e.g. "robinhood_0xMINT"
        mint = norm_mint(tid.split("_", 1)[1]) if "_" in tid else None
        if not mint:
            continue
        ticker = (attr.get("name") or "").split("/")[0].strip() or None
        rows.append({
            "network_id": str(network_id), "mint": mint, "ticker": ticker, "source": "gt-pools",
            "price": _f(attr.get("base_token_price_usd")),
            "market_cap": _f(attr.get("market_cap_usd")) or _f(attr.get("fdv_usd")),
            "liquidity": _f(attr.get("reserve_in_usd")),
            "volume_24h": _f((attr.get("volume_usd") or {}).get("h24")),
        })
    return rows


def gt_pool_rows(network_id, *, kinds=("trending_pools", "new_pools"), fetch=None):
    """Fetch GT trending + new pools for a mapped network -> normalized rows. `fetch(url)` injectable;
    the default honors the shared GT cooldown/gate (gt_get_json) so it never amplifies a runner 429."""
    slug = GT_NETWORK.get(str(network_id))
    if not slug:
        return []
    fetch = fetch or (lambda url: gt_get_json(url))
    out = []
    for kind in kinds:
        try:
            payload = fetch(GT_POOLS_URL.format(net=slug, kind=kind))
        except Exception:
            payload = None
        if payload:
            out.extend(parse_gt_pools(payload, network_id))
    return out


def refresh_universe_quotes(conn, *, dex_batch_fn=None, gt_batch_fn=None, quote_for_fn=None,
                            top_n=150, include_gt=True, now_iso=None):
    """Background pass: fetch fresh mcap/liq/vol for the top-N radar coins and write them to the
    radar quote cache, so /universe reads them instantly (no per-request live fetch). Injectable.
    `include_gt=False` skips the GeckoTerminal (4663) leg — a FAST DexScreener-only pass on its own
    rate budget, so Solana/Base coins can refresh often without touching the funded runner's shared GT
    budget. Returns the number of coins cached."""
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_universe import universe_rows, enrich_live_quotes
    now_iso = now_iso or real_utc_now_iso()
    try:
        extra = m.external_discovery_rows(conn, window_hours=72.0, now_iso=now_iso)
    except Exception:
        extra = []
    rows = universe_rows(conn, sort="buyers", window_hours=72.0, extra_sources=extra,
                         limit=int(top_n), now_iso=now_iso)
    if gt_batch_fn is None:
        gt_batch_fn = (lambda ks: gt_stats_batch_keyed(ks, ttl_s=15)) if include_gt else None
    enrich_live_quotes(
        conn, rows,
        dex_batch_fn=dex_batch_fn or dexscreener_stats_batch_keyed,
        gt_batch_fn=gt_batch_fn,
        quote_for=quote_for_fn or quote_for)
    priced = [r for r in rows if r.get("market_cap") is not None or r.get("liquidity") is not None
              or r.get("volume_24h") is not None]
    return m.upsert_radar_quotes(conn, priced, now_iso)


def refresh_socials(conn, *, socials_fn=None, top_n=120, now_iso=None):
    """Fetch & cache token socials (twitter/telegram/website/boosts) for the top-N radar coins so the
    radar can show 'is anyone talking about it'. Socials change slowly -> run on the throttled pass.
    Injectable. Returns coins cached."""
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_universe import universe_rows
    from sniper.execution.solana_quote import dexscreener_socials_batch
    now_iso = now_iso or real_utc_now_iso()
    rows = universe_rows(conn, sort="buyers", window_hours=72.0, limit=int(top_n), now_iso=now_iso)
    keys = [(r["network_id"], r["mint"]) for r in rows]
    # always include OPEN real positions so held bags get their socials even when they're no longer
    # top-of-radar by buyer count.
    try:
        keys += [(p["network_id"], p["mint"]) for p in conn.execute(
            "SELECT network_id, mint FROM memecoin_positions WHERE execution_mode='real' AND is_open=1")]
    except Exception:
        pass
    keys = list(dict.fromkeys(keys))
    socials_fn = socials_fn or dexscreener_socials_batch
    try:
        socials = socials_fn(keys) or {}
    except Exception:
        socials = {}
    return m.upsert_token_socials(conn, socials, now_iso)


def discover(conn, *, boost_keys_fn=None, dex_stats_fn=None, gt_rows_fn=None,
             gt_networks=("4663",), include_gt=True, quote_for_fn=None, now_iso=None):
    """Gather external coins from DexScreener boosts (priced chains) + GT pools (4663), enrich, and
    upsert into memecoin_external_discovery. `include_gt=False` skips the GT-pool leg (fast DexScreener
    pass). Every fetcher is injectable. Returns rows upserted."""
    from sniper.persistence import db_memecoin as m
    now_iso = now_iso or real_utc_now_iso()
    quote_for_fn = quote_for_fn or quote_for
    rows = []

    boost_keys_fn = boost_keys_fn or dexscreener_boost_keys
    try:
        keys = boost_keys_fn() or []
    except Exception:
        keys = []
    if keys:
        dex_stats_fn = dex_stats_fn or dexscreener_stats_batch_keyed
        try:
            stats = dex_stats_fn(keys) or {}
        except Exception:
            stats = {}
        for net, mint in keys:
            q = quote_for_fn(stats, mint, net) or {}
            rows.append({"network_id": net, "mint": mint, "ticker": q.get("symbol"),
                         "source": "dex-boost", "price": q.get("price"),
                         "market_cap": q.get("market_cap"), "liquidity": q.get("liquidity"),
                         "volume_24h": q.get("volume_24h")})

    if include_gt:
        gt_rows_fn = gt_rows_fn or (lambda net: gt_pool_rows(net))
        for net in gt_networks:
            try:
                rows.extend(gt_rows_fn(net) or [])
            except Exception:
                pass

    return m.upsert_external_discovery(conn, rows, now_iso)
