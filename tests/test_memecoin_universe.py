"""Coin Radar universe (Stage 1): coin rows, known-BUYER (armed) overlap, sort, filters, search."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_universe import (
    universe_rows, search_coins, enrich_live_quotes, armed_buyers_for,
)


def _c():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _obs(uid, mint, ticker, network="solana", observed="2026-07-19T00:00:00+00:00", side="buy"):
    return {
        "upstream_event_id": f"{uid}-{mint}-{observed}-{side}", "raw_type": f"large_{side}",
        "normalized_side": side, "source_user_id": uid, "source_handle": uid,
        "network_id": network, "mint": mint, "ticker": ticker, "their_price": 1.0,
        "their_market_cap": 1_000_000.0, "their_size_usd": 100.0,
        "source_ts_raw": observed, "source_ts": observed, "observed_at": observed,
        "lag_s": 0.0, "raw_payload": {},
    }


def _seed(c):
    # armed wallet + unarmed wallet
    m.add_wallet(c, "armed1", "ArmedAlice")
    m.set_wallet_real_approved(c, "armed1", True, force=True)
    m.add_wallet(c, "plain1", "PlainBob")            # not real_approved
    # coinA: bought by BOTH (armed overlap = 1); coinB: bought by the unarmed only
    m.insert_feed_observation_v2(c, _obs("armed1", "MINTA", "AAA"), was_curated=True, was_real_approved=True)
    m.insert_feed_observation_v2(c, _obs("plain1", "MINTA", "AAA"), was_curated=True, was_real_approved=False)
    m.insert_feed_observation_v2(c, _obs("plain1", "MINTB", "BBB"), was_curated=True, was_real_approved=False)
    # a stored quote for coinA only
    c.execute("INSERT INTO memecoin_quote_observations_v2"
              "(sweep_id, network_id, mint, price, market_cap, liquidity, volume_24h, quality, "
              " contexts, observed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
              ("sweep-1", "solana", "MINTA", 0.01, 5_000_000.0, 250_000.0, 900_000.0, "live",
               "{}", "2026-07-19T00:01:00+00:00"))
    c.commit()


def test_age_is_true_first_seen_not_within_window():
    # A-Crit1 regression: a coin first bought 5 days ago but re-pumping now must report its TRUE age
    # (~5 days), not the age of its earliest buy inside the 72h window. And a coin whose last activity
    # is outside the window must not appear.
    c = _c()
    m.add_wallet(c, "w1", "W1")
    # RETURN: first buy 5 days ago, latest buy 10 min ago -> in window via last_seen
    m.insert_feed_observation_v2(c, _obs("w1", "RETURN", "RET", observed="2026-07-14T05:50:00+00:00"),
                                 was_curated=True, was_real_approved=False)
    m.insert_feed_observation_v2(c, _obs("w1", "RETURN", "RET", observed="2026-07-19T05:50:00+00:00"),
                                 was_curated=True, was_real_approved=False)
    # DEAD: only old activity (5 days ago) -> outside window -> excluded
    m.insert_feed_observation_v2(c, _obs("w1", "DEAD", "DED", observed="2026-07-14T05:50:00+00:00"),
                                 was_curated=True, was_real_approved=False)
    rows = {r["mint"]: r for r in universe_rows(c, window_hours=72,
                                                now_iso="2026-07-19T06:00:00+00:00")}
    assert "DEAD" not in rows                                     # last activity outside window
    assert "RETURN" in rows
    assert rows["RETURN"]["age_s"] > 4 * 86400                    # ~5 days, not ~10 min
    assert rows["RETURN"]["recent_s"] < 3600                      # but recently active


def test_velocity_counts_recent_buyers_only():
    # Item 5: buyers_15m / buyers_60m count distinct buyers in the trailing windows (incoming velocity),
    # separate from the 72h cumulative buyers.
    c = _c()
    for i in range(4):
        m.add_wallet(c, f"w{i}", f"W{i}")
    # 2 buyers in the last 5m, 1 more at 40m ago, 1 more at 3h ago
    for uid, ts in [("w0", "2026-07-19T11:55:00+00:00"), ("w1", "2026-07-19T11:56:00+00:00"),
                    ("w2", "2026-07-19T11:20:00+00:00"), ("w3", "2026-07-19T09:00:00+00:00")]:
        m.insert_feed_observation_v2(c, _obs(uid, "VEL", "VL", observed=ts),
                                     was_curated=True, was_real_approved=False)
    c.commit()
    rows = {r["mint"]: r for r in universe_rows(c, window_hours=72, now_iso="2026-07-19T12:00:00+00:00")}
    r = rows["VEL"]
    assert r["buyers"] == 4                                       # cumulative
    assert r["buyers_60m"] == 3                                   # w0,w1,w2 within 60m
    assert r["buyers_15m"] == 2                                   # w0,w1 within 15m


def test_batched_latest_quote_returns_newest_per_coin():
    # Item 2: the batched quote lookup must return the LATEST row per coin (replacing the per-row N+1).
    from sniper.services.memecoin_universe import _latest_quotes_batch, _ratings_batch, _real_open_set
    c = _c()
    for sw, mc, ts in [("s1", 1.0, "2026-07-19T00:00:00+00:00"), ("s2", 99.0, "2026-07-19T05:00:00+00:00")]:
        c.execute("INSERT INTO memecoin_quote_observations_v2(sweep_id, network_id, mint, price, "
                  "market_cap, liquidity, volume_24h, quality, contexts, observed_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)", (sw, "solana", "M", 0.01, mc, 1.0, 1.0, "live", "{}", ts))
    c.execute("INSERT INTO memecoin_quote_observations_v2(sweep_id, network_id, mint, price, market_cap, "
              "liquidity, volume_24h, quality, contexts, observed_at) VALUES('s3','4663','0xa',1,7,1,1,'live','{}','2026-07-19T04:00:00+00:00')")
    c.commit()
    q = _latest_quotes_batch(c, [("solana", "M"), ("4663", "0xa"), ("solana", "MISSING")])
    assert q[("solana", "M")]["market_cap"] == 99.0     # newest, not the 1.0 row
    assert q[("4663", "0xa")]["market_cap"] == 7.0
    assert ("solana", "MISSING") not in q               # no quote -> absent
    assert _latest_quotes_batch(c, []) == {}            # empty pairs -> no query


def test_universe_rows_have_armed_buyer_overlap():
    c = _c(); _seed(c)
    rows = {r["mint"]: r for r in universe_rows(c, window_hours=None, now_iso="2026-07-19T01:00:00+00:00")}
    assert rows["MINTA"]["buyers"] == 2 and rows["MINTA"]["armed_buyers"] == 1
    assert rows["MINTB"]["buyers"] == 1 and rows["MINTB"]["armed_buyers"] == 0


def test_universe_rows_enrich_from_stored_quote():
    c = _c(); _seed(c)
    rows = {r["mint"]: r for r in universe_rows(c, window_hours=None, now_iso="2026-07-19T01:00:00+00:00")}
    assert rows["MINTA"]["market_cap"] == 5_000_000.0 and rows["MINTA"]["liquidity"] == 250_000.0
    assert rows["MINTB"]["market_cap"] is None      # no quote for coinB


def test_sort_by_volume_puts_none_last_both_directions():
    c = _c(); _seed(c)
    desc = universe_rows(c, sort="vol", desc=True, window_hours=None, now_iso="2026-07-19T01:00:00+00:00")
    assert desc[0]["mint"] == "MINTA" and desc[-1]["mint"] == "MINTB"   # None (B) last
    asc = universe_rows(c, sort="vol", desc=False, window_hours=None, now_iso="2026-07-19T01:00:00+00:00")
    assert asc[0]["mint"] == "MINTA" and asc[-1]["mint"] == "MINTB"     # None (B) STILL last


def test_min_armed_filter():
    c = _c(); _seed(c)
    rows = universe_rows(c, min_armed=1, window_hours=None, now_iso="2026-07-19T01:00:00+00:00")
    assert [r["mint"] for r in rows] == ["MINTA"]


def test_source_filter_separates_wallet_and_external_rows():
    c = _c(); _seed(c)
    extra = [{
        "network_id": "solana", "mint": "EXT", "ticker": "EXT",
        "buyers": 0, "armed_buyers": 0, "source": "dex-boost",
    }]

    wallet = universe_rows(c, source="wallet", window_hours=None, extra_sources=extra)
    external = universe_rows(c, source="external", window_hours=None, extra_sources=extra)
    boosted = universe_rows(c, source="dex-boost", window_hours=None, extra_sources=extra)

    assert {r["mint"] for r in wallet} == {"MINTA", "MINTB"}
    assert [r["mint"] for r in external] == ["EXT"]
    assert [r["mint"] for r in boosted] == ["EXT"]


def test_search_by_ticker_and_live_fallback():
    c = _c(); _seed(c)
    assert [r["mint"] for r in search_coins(c, "aaa")] == ["MINTA"]
    # live_fn appends an external coin not in our universe
    live = lambda q: [{"network_id": "solana", "mint": "MINTX", "ticker": "XXX", "armed_buyers": 0,
                       "buyers": 0, "market_cap": 1.0, "liquidity": 1.0, "volume_24h": 1.0}]
    out = search_coins(c, "zzz-not-local", live_fn=live)
    assert any(r["mint"] == "MINTX" and r["source"] == "live" for r in out)


def test_enrich_live_quotes_fills_blanks():
    c = _c(); _seed(c)
    rows = universe_rows(c, window_hours=None, now_iso="2026-07-19T01:00:00+00:00")
    dex = lambda keys: {("solana", "MINTB"): {"market_cap": 42.0, "liquidity": 7.0, "volume_24h": 9.0,
                                              "price_change_h1": 33.0}}
    enrich_live_quotes(c, rows, dex_batch_fn=dex, quote_for=lambda live, mint, net: live.get((net, mint)))
    b = next(r for r in rows if r["mint"] == "MINTB")
    assert b["market_cap"] == 42.0 and b["liquidity"] == 7.0 and b["volume_24h"] == 9.0
    assert b["price_change_h1"] == 33.0                       # 1h momentum captured for the classifier


def test_quote_cache_applied_before_sort():
    c = _c(); _seed(c)
    # coinB has no stored quote, but the cache gives it a huge volume -> it must sort FIRST by vol,
    # proving the cache is applied BEFORE sorting (not just as a post-hoc display fill).
    cache = {("solana", "MINTB"): {"volume_24h": 9_999_999.0, "market_cap": 1.0, "liquidity": 1.0}}
    rows = universe_rows(c, sort="vol", desc=True, window_hours=None, quote_cache=cache,
                         now_iso="2026-07-19T01:00:00+00:00")
    assert rows[0]["mint"] == "MINTB" and rows[0]["volume_24h"] == 9_999_999.0


def test_armed_buyers_for_lists_handles():
    c = _c(); _seed(c)
    assert armed_buyers_for(c, "solana", "MINTA") == ["ArmedAlice"]
    assert armed_buyers_for(c, "solana", "MINTB") == []


def test_sell_observations_do_not_count_as_coin_buyers():
    c = _c()
    m.add_wallet(c, "seller", "ArmedSeller")
    m.set_wallet_real_approved(c, "seller", True, force=True)
    m.insert_feed_observation_v2(
        c, _obs("seller", "MINTS", "SELL", side="sell"),
        was_curated=True, was_real_approved=True,
    )

    assert universe_rows(c, window_hours=None) == []
    assert armed_buyers_for(c, "solana", "MINTS") == []


def test_event_only_buy_is_included_and_event_sell_is_not():
    c = _c()
    m.add_wallet(c, "event-buyer", "EventAlice")
    m.set_wallet_real_approved(c, "event-buyer", True, force=True)
    for event_id, mint, side in (("buy-1", "EVENTBUY", "buy"), ("sell-1", "EVENTSELL", "sell")):
        c.execute(
            "INSERT INTO memecoin_events(fomo_event_id,source_user_id,source_handle,mint,ticker,"
            "side,created_at,received_at,network_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, "event-buyer", "EventAlice", mint, "EVT", side,
             "2026-07-19T00:00:00+00:00", "2026-07-19T00:00:01+00:00", "solana"),
        )
    c.commit()

    rows = {r["mint"]: r for r in universe_rows(c, window_hours=None)}

    assert rows["EVENTBUY"]["buyers"] == 1
    assert rows["EVENTBUY"]["armed_buyers"] == 1
    assert "EVENTSELL" not in rows
    assert armed_buyers_for(c, "solana", "EVENTBUY") == ["EventAlice"]


def test_episode_only_coin_is_included_without_invented_buyers():
    c = _c()
    c.execute(
        "INSERT INTO memecoin_token_episodes(episode_id,chain_id,token_address,started_ts,"
        "last_signal_ts,status) VALUES(?,?,?,?,?,?)",
        ("ep-1", "4663", "0xEPISODE", "2026-07-18T23:00:00+00:00",
         "2026-07-19T00:30:00+00:00", "open"),
    )
    c.commit()

    rows = universe_rows(c, window_hours=2, now_iso="2026-07-19T01:00:00+00:00")

    assert len(rows) == 1
    assert rows[0]["mint"] == "0xEPISODE"
    assert rows[0]["buyers"] == 0 and rows[0]["armed_buyers"] == 0
    assert rows[0]["age_s"] == 7200.0 and rows[0]["recent_s"] == 1800.0


def test_external_evm_address_case_dedupes_against_wallet_coin():
    c = _c()
    m.insert_feed_observation_v2(
        c, _obs("w1", "0xAbCd", "CASE", network="8453"),
        was_curated=True, was_real_approved=False,
    )
    extra = [{
        "network_id": "8453", "mint": "0xabcd", "ticker": "CASE",
        "buyers": 0, "armed_buyers": 0, "source": "dex-boost",
    }]

    rows = universe_rows(c, window_hours=None, extra_sources=extra)

    assert len(rows) == 1
    assert rows[0]["source"] == "wallet"


def test_dexscreener_search_normalizes_and_maps_chains():
    from sniper.execution.solana_quote import dexscreener_search

    payload = {"pairs": [
        {"chainId": "base", "baseToken": {"address": "0xBASE", "symbol": "BB", "name": "BeeBee"},
         "priceUsd": "0.5", "liquidity": {"usd": 12345.0}, "volume": {"h24": 9999.0}, "marketCap": 7.0e6},
        {"chainId": "solana", "baseToken": {"address": "SoLMint", "symbol": "SOLX"},
         "priceUsd": "1.0", "liquidity": {"usd": 500.0}, "volume": {"h24": 100.0}, "fdv": 1.0e6},
        {"chainId": "unknownchain", "baseToken": {"address": "0xNOPE", "symbol": "NO"},
         "priceUsd": "1.0"},                                   # unmapped chain -> dropped
        {"chainId": "base", "baseToken": {"address": "0xNOPRICE", "symbol": "NP"}},   # no price -> dropped
    ]}
    http = lambda method, url, **kw: (200, payload, None)
    out = dexscreener_search("bee", http=http)
    by_mint = {r["mint"]: r for r in out}
    # EVM mint is emitted LOWERCASED (was checksummed "0xBASE" -> silently missed every keyed lookup)
    assert "0xBASE" not in by_mint and by_mint["0xbase"]["network_id"] == "8453"
    assert by_mint["0xbase"]["ticker"] == "BB" and by_mint["0xbase"]["venue"] == "EVM"
    assert by_mint["0xbase"]["market_cap"] == 7.0e6 and by_mint["0xbase"]["liquidity"] == 12345.0
    # solana address is case-preserved (base58 is case-sensitive)
    assert by_mint["SoLMint"]["network_id"] == "solana" and by_mint["SoLMint"]["source"] == "live"
    assert "0xNOPE" not in by_mint and "0xNOPRICE" not in by_mint   # unmapped chain / no price dropped


def test_requested_sort_is_applied_before_candidate_cap():
    c = _c()
    for index in range(4):
        m.add_wallet(c, f"buyer-{index}", f"Buyer {index}")
        m.insert_feed_observation_v2(
            c,
            _obs(
                f"buyer-{index}", "POPULAR", "POP",
                observed="2026-07-19T00:00:00+00:00",
            ),
            was_curated=True,
            was_real_approved=False,
        )
    for index, mint in enumerate(("NEWEST", "NEWER")):
        m.insert_feed_observation_v2(
            c,
            _obs(
                "buyer-0", mint, mint[:3],
                observed=f"2026-07-19T00:0{index + 1}:00+00:00",
            ),
            was_curated=True,
            was_real_approved=False,
        )

    rows = universe_rows(
        c, sort="buyers", desc=True, window_hours=None, candidate_cap=2, limit=2,
        now_iso="2026-07-19T01:00:00+00:00",
    )

    assert rows[0]["mint"] == "POPULAR"
    assert rows[0]["buyers"] == 4


def test_current_radar_quote_sort_is_applied_before_candidate_cap():
    c = _c()
    for minute, mint in ((0, "HIGH"), (1, "LOW")):
        m.insert_feed_observation_v2(
            c,
            _obs(
                "buyer", mint, mint,
                observed=f"2026-07-19T00:0{minute}:00+00:00",
            ),
            was_curated=True,
            was_real_approved=False,
        )
    m.ensure_radar_quote_cache(c)
    c.executemany(
        "INSERT INTO memecoin_radar_quote "
        "(chain_id,mint,price,market_cap,liquidity,volume_24h,updated_at) "
        "VALUES('solana',?,?,?,?,?,?)",
        (
            ("HIGH", 1.0, 9_000_000.0, 500_000.0, 1_000_000.0,
             "2026-07-19T00:02:00+00:00"),
            ("LOW", 1.0, 1_000.0, 100.0, 100.0,
             "2026-07-19T00:02:00+00:00"),
        ),
    )
    c.commit()

    rows = universe_rows(
        c, sort="mcap", desc=True, window_hours=None, candidate_cap=1, limit=1,
        quote_cache=m.radar_quote_map(c, now_iso="2026-07-19T00:03:00+00:00"),
        now_iso="2026-07-19T00:03:00+00:00",
    )

    assert rows[0]["mint"] == "HIGH"
    assert rows[0]["market_cap"] == 9_000_000.0
