"""Coin Radar #4: token socials — DexScreener extraction, cache, refresh sweep."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.execution.solana_quote import dexscreener_socials_batch
from sniper.services import memecoin_discovery_external as D


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def test_socials_batch_extracts_links_and_boosts():
    payload = {"pairs": [
        {"chainId": "solana", "pairAddress": "pA", "baseToken": {"address": "MINTA"}, "priceUsd": "1.0",
         "liquidity": {"usd": 50000.0},
         "info": {"socials": [{"type": "twitter", "url": "https://x.com/foo"},
                              {"type": "telegram", "url": "https://t.me/foo"}],
                  "websites": [{"url": "https://foo.xyz"}]},
         "boosts": {"active": 3}},
        {"chainId": "solana", "pairAddress": "pB", "baseToken": {"address": "NOSOC"}, "priceUsd": "1.0",
         "liquidity": {"usd": 40000.0}, "info": {}},          # no socials/boosts -> omitted
    ]}
    http = lambda method, url, **kw: (200, payload, None)
    out = dexscreener_socials_batch([("solana", "MINTA"), ("solana", "NOSOC")], http=http)
    assert ("solana", "MINTA") in out and ("solana", "NOSOC") not in out
    s = out[("solana", "MINTA")]
    assert s["twitter"] == "https://x.com/foo" and s["telegram"] == "https://t.me/foo"
    assert s["website"] == "https://foo.xyz" and s["boosts"] == 3


def test_social_cache_roundtrip_and_has_socials_flag():
    c = _c()
    m.upsert_token_socials(c, {("solana", "M1"): {"twitter": "x", "boosts": 2},
                               ("4663", "0xABC"): {"boosts": 0}}, "2026-07-19T00:00:00+00:00")
    sm = m.token_social_map(c)
    assert sm[m.accounting_mark_key("solana", "M1")]["twitter"] == "x"
    assert sm[m.accounting_mark_key("solana", "M1")]["has_socials"] is True
    assert sm[m.accounting_mark_key("4663", "0xABC")]["has_socials"] is False   # boosts only, no links
    # EVM key normalizes case
    assert m.accounting_mark_key("4663", "0xabc") in sm


def test_social_and_triage_freshness_flags():
    # A-Imp5 regression: a socials row / triage verdict cached long ago must be flagged stale so the UI
    # doesn't render a days-old draft as current.
    c = _c()
    now = "2026-07-19T12:00:00+00:00"
    m.upsert_token_socials(c, {("solana", "FRESH"): {"twitter": "x"}}, "2026-07-19T11:00:00+00:00")
    m.upsert_token_socials(c, {("solana", "OLD"): {"twitter": "y"}}, "2026-07-16T00:00:00+00:00")   # 84h
    sm = m.token_social_map(c, now_iso=now)          # socials stale after 48h
    assert sm[m.accounting_mark_key("solana", "FRESH")]["stale"] is False
    assert sm[m.accounting_mark_key("solana", "OLD")]["stale"] is True
    assert sm[m.accounting_mark_key("solana", "OLD")]["age_h"] > 48
    m.upsert_radar_triage(c, [{"network_id": "solana", "mint": "TFRESH", "verdict": "promising",
                               "reason": "r"}], "2026-07-19T11:00:00+00:00")
    m.upsert_radar_triage(c, [{"network_id": "solana", "mint": "TOLD", "verdict": "promising",
                               "reason": "r"}], "2026-07-18T00:00:00+00:00")   # 36h
    tm = m.radar_triage_map(c, now_iso=now)          # triage stale after 12h
    assert tm[m.accounting_mark_key("solana", "TFRESH")]["stale"] is False
    assert tm[m.accounting_mark_key("solana", "TOLD")]["stale"] is True


def test_refresh_socials_caches_for_universe_coins():
    c = _c()
    m.add_wallet(c, "w1", "W1")
    m.insert_feed_observation_v2(c, {
        "upstream_event_id": "e1", "raw_type": "large_buy", "normalized_side": "buy",
        "source_user_id": "w1", "source_handle": "w1", "network_id": "solana", "mint": "COINS",
        "ticker": "S", "their_price": 1.0, "their_market_cap": 1.0, "their_size_usd": 1.0,
        "source_ts_raw": "2026-07-19T00:00:00+00:00", "source_ts": "2026-07-19T00:00:00+00:00",
        "observed_at": "2026-07-19T00:00:00+00:00", "lag_s": 0.0, "raw_payload": {}},
        was_curated=True, was_real_approved=False)
    fake = lambda keys: {("solana", "COINS"): {"twitter": "https://x.com/s", "boosts": 1}}
    n = D.refresh_socials(c, socials_fn=fake, now_iso="2026-07-19T01:00:00+00:00")
    assert n == 1
    assert m.token_social_map(c)[m.accounting_mark_key("solana", "COINS")]["twitter"] == "https://x.com/s"
