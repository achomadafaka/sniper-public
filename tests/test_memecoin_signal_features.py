"""Signal-time feature capture (adaptive policy engine, phase 1).

The copy path ALREADY calls DexScreener at entry to model the fill, and the response already
carries volatility, short-horizon volume and buy/sell pressure — we parse none of it and throw it
away. Those are exactly the features the operator wants the exit policy conditioned on, and they
cost ZERO extra latency because the call is already in flight.

Nothing here changes a trading decision. It captures the vector so a policy can later be FITTED —
see docs/superpowers/specs/2026-07-15-memecoin-adaptive-policy-engine-design.md.
"""
import json
import sqlite3
from types import SimpleNamespace

from sniper.execution.solana_quote import _stats_from_pairs
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_copy_trader import copy_event_to_arms


def _pair(**over):
    p = {
        "chainId": "solana",
        "pairAddress": "PAIR1",
        "dexId": "raydium",
        "baseToken": {"address": "MintAAA", "symbol": "AAA"},
        "priceUsd": "0.5",
        "marketCap": 1_000_000,
        "fdv": 1_200_000,
        "liquidity": {"usd": 80_000.0},
        "volume": {"h24": 500_000.0, "h6": 200_000.0, "h1": 60_000.0, "m5": 9_000.0},
        "priceChange": {"h24": 120.0, "h6": 40.0, "h1": 12.5, "m5": -3.25},
        "txns": {"m5": {"buys": 42, "sells": 7}, "h1": {"buys": 300, "sells": 120}},
        "pairCreatedAt": 1_700_000_000_000,
    }
    p.update(over)
    return p


def test_parses_volatility_volume_and_buy_pressure():
    """priceChange / short-horizon volume / txns are the operator's 'volatility at signal',
    'volume at signal' and buy-pressure features. All free — same HTTP response."""
    st = _stats_from_pairs([_pair()], "MintAAA", "solana")
    assert st["price_change_m5"] == -3.25
    assert st["price_change_h1"] == 12.5
    assert st["price_change_h6"] == 40.0
    assert st["price_change_h24"] == 120.0
    assert st["volume_m5"] == 9_000.0
    assert st["volume_h1"] == 60_000.0
    assert st["txns_m5_buys"] == 42
    assert st["txns_m5_sells"] == 7
    assert st["fdv"] == 1_200_000


def test_existing_fields_are_unchanged():
    """The fill path reads these — a regression here would mis-price real money."""
    st = _stats_from_pairs([_pair()], "MintAAA", "solana")
    assert st["price"] == 0.5
    assert st["market_cap"] == 1_000_000
    assert st["liquidity"] == 80_000.0
    assert st["volume_24h"] == 500_000.0
    assert st["pair_address"] == "PAIR1"


def test_missing_blocks_are_none_not_zero():
    """A coin with no priceChange block must yield None (unknown), never 0.0 (known-flat). The
    policy engine has to tell 'we don't know this coin's volatility' from 'it didn't move' —
    conflating them is how the mcap gate silently dropped sub-$1M winners on feed gaps."""
    st = _stats_from_pairs([_pair(priceChange={}, volume={}, txns={})], "MintAAA", "solana")
    assert st["price_change_m5"] is None
    assert st["volume_m5"] is None
    assert st["txns_m5_buys"] is None
    assert st["volume_24h"] is None


def test_negative_price_change_survives():
    """priceChange is SIGNED — a dumping coin is the whole point of the feature. It must not be
    filtered by the positive-finite guard the price/liquidity fields use."""
    st = _stats_from_pairs([_pair(priceChange={"m5": -80.0, "h1": -95.5})], "MintAAA", "solana")
    assert st["price_change_m5"] == -80.0
    assert st["price_change_h1"] == -95.5


def test_mcap_fallback_is_scoped_to_network():
    """An EVM address is not a coin identity. The same 0x address can exist on Base and 4663;
    policy assignment must never borrow the other chain's market cap."""
    conn = _conn()
    conn.executemany(
        "INSERT INTO memecoin_events(fomo_event_id,mint,network_id,their_market_cap,created_at,side) "
        "VALUES(?,?,?,?,?,'buy')",
        [
            ("base-1", "0xSame", "8453", 2_000_000, "2026-07-14T20:00:00+00:00"),
            ("robin-1", "0xSame", "4663", 80_000_000, "2026-07-14T20:01:00+00:00"),
        ],
    )
    conn.commit()

    assert m.resolve_mint_mcap(conn, "0xSame", network_id="8453") == 2_000_000
    assert m.resolve_mint_mcap(conn, "0xSame", network_id="4663") == 80_000_000


def _event(*, event_mcap=2_000_000):
    return SimpleNamespace(
        side="buy", source_user_id="u1", source_handle="alice", mint="MintAAA", ticker="AAA",
        their_price=0.5, their_market_cap=event_mcap, their_size_usd=2_500.0,
        is_first_buy=True, fomo_event_id="event-1", created_at="2026-07-14T20:00:00+00:00",
        received_at="2026-07-14T20:00:01+00:00", received_at_normalized=None,
        lag_s=1.0, signal_source="global_feed", network_id="solana",
    )


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.execute(
        "INSERT INTO memecoin_trader_analytics("
        "source_user_id, computed_at, data_as_of, median_hold_s, pct_held_under_60s, "
        "pct_held_over_1d, avg_buy_usd, size_cv, re_entry_rate) VALUES(?,?,?,?,?,?,?,?,?)",
        ("u1", "2026-07-14T19:00:00+00:00", "2026-07-14T18:59:00+00:00", 45.0,
         0.8, 0.0, 4_000.0, 0.25, 0.1),
    )
    conn.commit()
    return conn


def test_copy_snapshot_persists_full_existing_stats_call_without_another_request(monkeypatch):
    """The parser additions are useless unless the entry snapshot keeps them. The stats seam is
    already called once per entry; feature persistence must not add a second call."""
    # The provider shadow is retired by default (cb8cbb094, PROVIDER_SHADOW = False); this test
    # covers the shadow path itself, so it turns it on.
    import sniper.services.memecoin_copy_trader as copy_trader
    monkeypatch.setattr(copy_trader, "PROVIDER_SHADOW", True)
    conn = _conn()
    calls = []

    def stats(mint, network_id=None):
        calls.append((mint, network_id))
        return {
            "market_cap": 1_500_000.0,
            "fdv": 1_700_000.0,
            "liquidity": 80_000.0,
            "volume_24h": 500_000.0,
            "volume_m5": 9_000.0,
            "volume_h1": 60_000.0,
            "price_change_m5": -3.25,
            "price_change_h1": 12.5,
            "price_change_h6": 40.0,
            "price_change_h24": 120.0,
            "txns_m5_buys": 42.0,
            "txns_m5_sells": 7.0,
            "symbol": "AAA",
            "chain": "solana",
        }

    out = copy_event_to_arms(
        conn, _event(), 100.0, "2026-07-14T20:00:01+00:00", arms=("control",),
        fill_fn=lambda *args, **kwargs: (0.5, "dexscreener"), stats_fn=stats,
    )

    assert out == {"control": "opened"}
    assert calls == [("MintAAA", "solana")]
    row = conn.execute(
        "SELECT mcap, features_json FROM memecoin_entry_snapshots "
        "WHERE upstream_event_id='event-1'",
    ).fetchone()
    features = json.loads(row["features_json"])
    assert row["mcap"] == 2_000_000.0       # feed fact wins
    assert features["their_market_cap"] == 2_000_000.0
    # Paper step 3 (spec 2026-10-08): the provider payload rides ONLY under provider_shadow, so no
    # provider key sits at the top level under an old name.
    shadow = features["provider_shadow"]
    assert shadow["market_cap"] == 1_500_000.0
    assert shadow["price_change_m5"] == -3.25
    assert shadow["volume_m5"] == 9_000.0
    assert shadow["txns_m5_buys"] == 42.0
    assert not {"dex_market_cap", "price_change_m5", "volume_m5", "txns_m5_buys"} & set(features)
    assert features["is_first_buy"] is True
    assert features["lag_s"] == 1.0
    assert features["trader_profile"]["archetype"] == "scalper"
    assert features["trader_profile"]["median_hold_s"] == 45.0
    pos = conn.execute(
        "SELECT entry_mcap FROM memecoin_positions WHERE strategy='control'",
    ).fetchone()
    assert pos["entry_mcap"] == 2_000_000.0


def test_default_paper_fill_reuses_the_signal_stats_response(monkeypatch):
    """Runtime passes both honest_fill and dexscreener_stats. That used to fetch the same pair
    twice; the feature vector must come from ONE response on the hot path.

    The FILL itself is no longer taken from that response — it is an executable route quote — but
    the single-fetch property this test guards still holds, so it is asserted against the injected
    quote seam instead of against a mid-derived entry_price."""
    import sniper.services.memecoin_copy_trader as copy_trader
    monkeypatch.setattr(copy_trader, "PROVIDER_SHADOW", True)  # shadow path; retired by default (cb8cbb094)
    from sniper.execution import solana_quote
    conn = _conn()
    hidden_fill_fetches = []
    stats_fetches = []

    monkeypatch.setattr(
        solana_quote, "dexscreener_stats",
        lambda *args, **kwargs: hidden_fill_fetches.append(args) or {
            "price": 99.0, "liquidity": 1.0,
        },
    )

    def stats(mint, network_id=None):
        stats_fetches.append((mint, network_id))
        return {"price": 0.5, "liquidity": 1e15, "market_cap": 1_500_000.0}

    copy_event_to_arms(
        conn, _event(), 100.0, "2026-07-14T20:00:01+00:00", arms=("control",),
        fill_fn=solana_quote.honest_fill, stats_fn=stats,
        quote_fn=lambda *_a, **_k: (0.5075, "route_quote"),
    )
    assert stats_fetches == [("MintAAA", "solana")]      # exactly one stats fetch, the point here
    assert hidden_fill_fetches == []                     # and the fill never re-fetches DexScreener
    assert abs(conn.execute(
        "SELECT entry_price FROM memecoin_positions WHERE strategy='control'",
    ).fetchone()[0] - 0.5075) < 1e-12                    # entry is the ROUTE quote, not the 0.5 mid


def test_entry_mcap_takes_no_dex_fallback_and_preserves_unknown():
    """Paper step 3 (spec 2026-10-08): the DexScreener mcap is no longer a fallback. Without an
    event mcap or a funded decision on the origin, the feed resolver decides, and here it is unknown."""
    conn = _conn()
    dex_event = _event(event_mcap=None)
    copy_event_to_arms(
        conn, dex_event, 100.0, "2026-07-14T20:00:01+00:00", arms=("control",),
        fill_fn=lambda *args, **kwargs: (0.5, "dexscreener"),
        stats_fn=lambda *args, **kwargs: {"market_cap": 750_000.0},
    )
    assert conn.execute(
        "SELECT entry_mcap FROM memecoin_positions WHERE origin_event_id='event-1'",
    ).fetchone()[0] is None

    unknown = _event(event_mcap=None)
    unknown.fomo_event_id = "event-2"
    unknown.mint = "MintBBB"
    copy_event_to_arms(
        conn, unknown, 100.0, "2026-07-14T20:00:02+00:00", arms=("control",),
        fill_fn=lambda *args, **kwargs: (0.5, "fomo"), stats_fn=lambda *args, **kwargs: {},
    )
    assert conn.execute(
        "SELECT entry_mcap FROM memecoin_positions WHERE origin_event_id='event-2'",
    ).fetchone()[0] is None


def test_snapshot_consensus_counts_prior_distinct_curated_buyers_on_observation_clock():
    conn = _conn()
    rows = [
        ("prior-1", "u2", "2026-07-14T19:30:01+00:00", 1),
        ("prior-2", "u2", "2026-07-14T19:45:01+00:00", 1),  # same buyer: count once
        ("uncurated", "u3", "2026-07-14T19:50:01+00:00", 0),
        ("event-1", "u1", "2026-07-14T20:00:01+00:00", 1),
    ]
    conn.executemany(
        "INSERT INTO memecoin_feed_observations_v2("
        "upstream_event_id, raw_type, normalized_side, source_user_id, network_id, mint, "
        "observed_at, was_curated, raw_json) VALUES(?, 'large_buy', 'buy', ?, 'solana', "
        "'MintAAA', ?, ?, '{}')",
        rows,
    )
    conn.commit()

    copy_event_to_arms(
        conn, _event(), 100.0, "2026-07-14T20:00:01+00:00", arms=("control",),
        fill_fn=lambda *args, **kwargs: (0.5, "dexscreener"), stats_fn=lambda *args, **kwargs: {},
    )
    row = conn.execute(
        "SELECT consensus_n_60m FROM memecoin_entry_snapshots WHERE upstream_event_id='event-1'",
    ).fetchone()
    assert row["consensus_n_60m"] == 1


def test_snapshot_consensus_is_unknown_without_current_capture_row():
    conn = _conn()
    copy_event_to_arms(
        conn, _event(), 100.0, "2026-07-14T20:00:01+00:00", arms=("control",),
        fill_fn=lambda *args, **kwargs: (0.5, "dexscreener"), stats_fn=lambda *args, **kwargs: {},
    )
    row = conn.execute(
        "SELECT consensus_n_60m FROM memecoin_entry_snapshots WHERE upstream_event_id='event-1'",
    ).fetchone()
    assert row["consensus_n_60m"] is None
