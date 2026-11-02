"""The three surfaces that trusted `memecoin_radar_quote`, and what they do now it is empty.

The radar discovery+quote daemon was stopped on 2026-08-21 ("radar resource isolation" in the
crontab). `radar_quote_map` has returned {} ever since -- newest row 2026-08-21T07:23:51Z against a
600s freshness window -- so every coin on these pages read as unpriced. Nothing said so: `hot` is
computed from liquidity, so it silently pinned to False, and the universe page rendered a wall of
blank market caps.
"""
import sqlite3

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes import api_memecoin as API
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.services import memecoin_universe as U

NOW = "2026-07-19T06:00:00+00:00"


@pytest.fixture
def client():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    for uid in ("a1", "a2"):
        m.add_wallet(c, uid, uid)
        m.set_wallet_real_approved(c, uid, True, force=True)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    return app.test_client(), c


def _buy(c, uid, mint, ts):
    m.insert_feed_observation_v2(c, {
        "upstream_event_id": f"{uid}-{mint}-{ts}", "raw_type": "large_buy", "normalized_side": "buy",
        "source_user_id": uid, "source_handle": uid, "network_id": "solana", "mint": mint,
        "ticker": mint, "their_price": 1.0, "their_market_cap": 1.0, "their_size_usd": 1.0,
        "source_ts_raw": ts, "source_ts": ts, "observed_at": ts, "lag_s": 0.0, "raw_payload": {}},
        was_curated=True, was_real_approved=True)


def _consensus(c, mint="LIVE"):
    for uid in ("a1", "a2"):
        _buy(c, uid, mint, "2026-07-19T05:55:00+00:00")
    c.commit()


def test_entry_signals_prices_its_candidates_instead_of_scoring_them_all_cold(client, monkeypatch):
    cl, c = client
    _consensus(c)
    monkeypatch.setattr(API, "_now_iso", lambda: NOW)
    monkeypatch.setattr(API, "_live_quote_map",
                        lambda keys: {("solana", "LIVE"): {"liquidity": 20000.0, "market_cap": 8e5}})

    j = cl.get("/api/memecoin/entry-signals").get_json()

    assert [s["mint"] for s in j["setups"]] == ["LIVE"]
    assert j["setups"][0]["hot"] is True
    assert j["no_liquidity"] == []


def test_entry_signals_names_the_candidates_whose_depth_it_could_not_read(client, monkeypatch):
    cl, c = client
    _consensus(c)
    monkeypatch.setattr(API, "_now_iso", lambda: NOW)
    monkeypatch.setattr(API, "_live_quote_map", lambda keys: {})   # every provider dark

    j = cl.get("/api/memecoin/entry-signals").get_json()

    assert j["setups"][0]["hot"] is False           # unknown liquidity is never hot
    assert j["no_liquidity"] == ["LIVE"]           # ...and the page says why, instead of looking calm


def test_the_universe_page_enriches_what_it_shows(client, monkeypatch):
    cl, _c = client
    seen = {}

    def _spy(conn, rows, **kw):
        seen["called"] = True
        return rows

    monkeypatch.setattr(U, "enrich_live_quotes", _spy)

    j = cl.get("/api/memecoin/universe").get_json()

    assert seen.get("called") is True     # it built rows off the dead cache and never enriched them
    assert j["unpriced"] == []            # contract holds on an empty page


def test_the_live_quote_map_falls_back_to_geckoterminal_for_the_blind_chain(monkeypatch):
    # DexScreener indexes no pair for 4663, so a Solana-only fill would leave Robinhood coins unpriced
    # forever — the same silent blindness one chain down.
    monkeypatch.setattr(API, "_cached_dex_batch",
                        lambda keys, ttl_s=1.0: {("solana", "SOL1"): {"price": 2.0,
                                                                      "liquidity": 5000.0}})
    monkeypatch.setattr(API, "gt_stats_batch_keyed",
                        lambda keys, ttl_s=15: {("4663", "0xwood"): {"price": 1.0, "mcap": 3e5}})

    out = API._live_quote_map([("solana", "SOL1"), ("4663", "0xwood")])

    assert out[m.accounting_mark_key("solana", "SOL1")]["liquidity"] == 5000.0
    assert out[m.accounting_mark_key("4663", "0xwood")]["market_cap"] == 3e5   # mcap -> market_cap


def test_the_live_quote_map_returns_empty_when_every_provider_is_down(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(API, "_cached_dex_batch", _boom)
    monkeypatch.setattr(API, "gt_stats_batch_keyed", _boom)

    assert API._live_quote_map([("solana", "SOL1")]) == {}   # unknown, never fabricated


def test_unpriced_is_not_keyed_on_liquidity(client, monkeypatch):
    """GeckoTerminal is the ONLY source for the DexScreener-blind 4663 chain and it never returns
    liquidity — it answers with price and sometimes market cap. Keying "unpriced" on liquidity marked
    every 4663 coin unpriced forever (27 of 29 rows on the live page), which is not information.
    """
    cl, _c = client

    def _enrich(conn, rows, **kw):
        for r in rows:                       # what a GT-priced 4663 row actually looks like
            r["price"], r["market_cap"], r["liquidity"] = 0.0068, 6_651_994.0, None
        return rows

    monkeypatch.setattr(U, "enrich_live_quotes", _enrich)
    monkeypatch.setattr(U, "universe_rows", lambda *a, **k: [
        {"mint": "0xwood", "ticker": "W", "network_id": "4663",
         "price": None, "market_cap": None, "liquidity": None, "volume_24h": None},
    ])

    j = cl.get("/api/memecoin/universe").get_json()

    assert j["rows"][0]["market_cap"] == 6_651_994.0
    assert j["unpriced"] == []          # it HAS a price and a mcap; only the depth is unreadable
