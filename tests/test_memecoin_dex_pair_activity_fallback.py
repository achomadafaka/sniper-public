"""DexScreener pair selection with the activity fallback (Alpha order B, operator decision 2026-09-17).

Today's rule ranks the listed pairs by quote-side USD liquidity. The fallback applies only when that top
pair holds under MEMECOIN_DEX_PAIR_DUST_USD on the quote side, or reports no liquidity: it then picks the
pair with the most h1 transactions, then h1 volume, then quote-side liquidity. The switch
MEMECOIN_DEX_PAIR_ACTIVITY_FALLBACK is OFF by default, and OFF must return today's pick unchanged. Both
picks are recorded from the same response (`pair_selection`), in the entry trace and on marks.
"""
import json
import random
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.execution import solana_quote as q
from sniper.settings import memecoin as cfg

MINT = "So11111111111111111111111111111111111111112"


def _pair(address, *, quote_side=None, liquidity=1000.0, base_reserve=None, price="0.01", h1_buys=None,
          h1_sells=None, volume_h1=None, volume_h24=100.0, dex="raydium", mcap=12345, chain="solana", base=MINT):
    pair = {"chainId": chain, "pairAddress": address, "dexId": dex, "baseToken": {"address": base, "symbol": "TOK"},
            "priceUsd": price, "volume": {"h24": volume_h24}, "marketCap": mcap}
    if liquidity is not None:
        pair["liquidity"] = {"usd": liquidity}
        if quote_side is not None:
            pair["liquidity"]["base"] = (liquidity - quote_side) / float(price)
        elif base_reserve is not None:
            pair["liquidity"]["base"] = base_reserve
    if volume_h1 is not None:
        pair["volume"]["h1"] = volume_h1
    if h1_buys is not None or h1_sells is not None:
        pair["txns"] = {"h1": {"buys": h1_buys, "sells": h1_sells}}
    return pair


@pytest.fixture
def fallback_on(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DEX_PAIR_ACTIVITY_FALLBACK", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_DEX_PAIR_DUST_USD", 100.0, raising=False)


def _legacy_select(pairs, mint, network_id=None):
    """Verbatim copy of select_dex_pair before this change: the oracle for the OFF path."""
    chain = q._expected_chain(mint, network_id)
    if not chain:
        return None
    wanted = q.norm_mint(mint)
    valid = []
    for pair in pairs or []:
        if not isinstance(pair, dict) or pair.get("chainId") != chain:
            continue
        base = (pair.get("baseToken") or {}).get("address")
        if not base or q.norm_mint(base) != wanted:
            continue
        if not pair.get("pairAddress") or q._positive_finite(pair.get("priceUsd")) is None:
            continue
        valid.append(pair)
    if not valid:
        return None
    return sorted(valid, key=lambda p: (-q._quote_side_usd(p), -q._nonnegative_finite(q._nested(p, "volume", "h24")),
                                        str(p.get("pairAddress"))))[0]


def _random_pairs(rng):
    out = []
    for i in range(rng.randint(0, 7)):
        liq = rng.choice([None, 0, 3.5, 99.99, 100.0, 100.01, 500, 25_000])
        pair = _pair("p%d" % rng.randint(0, 4), liquidity=liq, price=rng.choice(["0.01", "0", "nan", "2e-6"]),
                     volume_h24=rng.choice([0, 5, 5, 900]), h1_buys=rng.choice([None, 0, 3, 40]),
                     h1_sells=rng.choice([None, 0, 1, 12]), volume_h1=rng.choice([None, 0, 50.0, 7000.0]),
                     chain=rng.choice(["solana", "solana", "base"]), base=rng.choice([MINT, MINT, "OTHER"]))
        if liq and rng.random() < 0.5:
            pair["liquidity"]["base"] = rng.choice([0, 1e3, 4e5, 9e9])
        out.append(pair)
    return out


def test_the_switch_is_off_by_default_and_off_returns_todays_pick_unchanged(monkeypatch):
    assert cfg.MEMECOIN_DEX_PAIR_ACTIVITY_FALLBACK is False
    assert cfg.MEMECOIN_DEX_PAIR_DUST_USD == 100.0
    rng = random.Random(20260917)
    for _ in range(3000):
        pairs = _random_pairs(rng)
        got, want = q.select_dex_pair(pairs, MINT, "solana"), _legacy_select(pairs, MINT, "solana")
        assert got is want
        stats = q._stats_from_pairs(pairs, MINT, "solana")
        assert (stats or {}).get("pair_address") == (want or {}).get("pairAddress")


def test_a_dust_top_pair_falls_back_to_the_pair_with_the_most_h1_txns(fallback_on):
    pairs = [_pair("dust", quote_side=24.36, liquidity=48.0, h1_buys=0, h1_sells=0),
             _pair("active", quote_side=0.0, liquidity=None, h1_buys=180, h1_sells=150, dex="pumpswap"),
             _pair("quiet", quote_side=60.0, liquidity=120.0, h1_buys=2, h1_sells=1)]
    assert q.select_dex_pair(pairs, MINT, "solana")["pairAddress"] == "active"


def test_the_trigger_reads_the_quote_side_not_the_self_valued_total(fallback_on):
    # CUMINU 2026-09-17: liquidity.usd $348.95, quote side $2.75.
    pairs = [_pair("meteora", quote_side=2.75, liquidity=348.95, h1_buys=0, h1_sells=0, dex="meteora"),
             _pair("pumpswap", quote_side=1.0, liquidity=2.0, h1_buys=20, h1_sells=11, dex="pumpswap")]
    assert q.select_dex_pair(pairs, MINT, "solana")["pairAddress"] == "pumpswap"


def test_a_top_pair_that_reports_no_liquidity_triggers_the_fallback(fallback_on):
    pairs = [_pair("none", liquidity=None, h1_buys=0, h1_sells=0, volume_h24=900),
             _pair("active", liquidity=None, h1_buys=4, h1_sells=0)]
    assert q.select_dex_pair(pairs, MINT, "solana")["pairAddress"] == "active"


def test_a_top_pair_at_the_dust_threshold_or_above_keeps_todays_pick(fallback_on, monkeypatch):
    pairs = [_pair("deep", quote_side=100.0, liquidity=200.0, h1_buys=0, h1_sells=0),
             _pair("busy", quote_side=99.0, liquidity=198.0, h1_buys=500, h1_sells=500)]
    assert q.select_dex_pair(pairs, MINT, "solana")["pairAddress"] == "deep"
    pairs[0] = _pair("deep", quote_side=99.99, liquidity=199.99, h1_buys=0, h1_sells=0)
    assert q.select_dex_pair(pairs, MINT, "solana")["pairAddress"] == "busy"
    monkeypatch.setattr(cfg, "MEMECOIN_DEX_PAIR_DUST_USD", 50.0, raising=False)
    assert q.select_dex_pair(pairs, MINT, "solana")["pairAddress"] == "deep"


def test_a_top_pair_with_no_liquidity_triggers_even_with_a_zero_threshold(fallback_on, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DEX_PAIR_DUST_USD", 0.0, raising=False)
    pairs = [_pair("none", liquidity=None, h1_buys=0, h1_sells=0, volume_h24=900),
             _pair("active", liquidity=None, h1_buys=4, h1_sells=0)]
    assert q.select_dex_pair(pairs, MINT, "solana")["pairAddress"] == "active"
    pairs = [_pair("zero", liquidity=0.0, h1_buys=0, h1_sells=0, volume_h24=900),
             _pair("active", liquidity=0.0, h1_buys=4, h1_sells=0)]
    assert q.select_dex_pair(pairs, MINT, "solana")["pairAddress"] == "zero"


def test_the_tie_break_is_h1_txns_then_h1_volume_then_quote_side_liquidity(fallback_on):
    top = _pair("top", quote_side=10.0, liquidity=20.0, h1_buys=3, h1_sells=3, volume_h1=10.0)
    by_volume = _pair("by-volume", quote_side=1.0, liquidity=2.0, h1_buys=4, h1_sells=2, volume_h1=900.0)
    assert q.select_dex_pair([top, by_volume], MINT, "solana")["pairAddress"] == "by-volume"
    by_liquidity = _pair("by-liquidity", quote_side=5.0, liquidity=10.0, h1_buys=6, h1_sells=0, volume_h1=900.0)
    assert q.select_dex_pair([top, by_volume, by_liquidity], MINT, "solana")["pairAddress"] == "by-liquidity"
    more_txns = _pair("more-txns", quote_side=0.5, liquidity=1.0, h1_buys=7, h1_sells=0, volume_h1=1.0)
    assert q.select_dex_pair([top, by_volume, by_liquidity, more_txns], MINT, "solana")["pairAddress"] == "more-txns"


def test_a_pair_with_no_h1_txns_never_wins_over_a_pair_with_activity(fallback_on):
    pairs = [_pair("deepest-dust", quote_side=99.0, liquidity=198.0, h1_buys=0, h1_sells=0, volume_h1=5000.0),
             _pair("one-trade", quote_side=0.2, liquidity=0.4, h1_buys=1, h1_sells=None)]
    assert q.select_dex_pair(pairs, MINT, "solana")["pairAddress"] == "one-trade"


def test_no_activity_anywhere_keeps_todays_pick(fallback_on):
    pairs = [_pair("b", quote_side=50.0, liquidity=100.0, volume_h24=5),
             _pair("a", quote_side=50.0, liquidity=100.0, volume_h24=5),
             _pair("c", quote_side=10.0, liquidity=20.0)]
    assert q.select_dex_pair(pairs, MINT, "solana") is _legacy_select(pairs, MINT, "solana")


def test_stats_record_both_picks_from_the_same_response():
    dust = _pair("dust", quote_side=24.36, liquidity=48.0, price="0.02", mcap=90000, h1_buys=0, h1_sells=0,
                 volume_h1=12.0, dex="meteora")
    active = _pair("active", liquidity=20.0, price="0.01", mcap=45000, h1_buys=180, h1_sells=150,
                   volume_h1=20000.0, dex="pumpswap")
    other_chain = _pair("other-chain", chain="base", base="0xabcdefabcdefabcdefabcdefabcdefabcdefabcd")
    stats = q._stats_from_pairs([dust, active, other_chain], MINT, "solana")
    rec = stats["pair_selection"]
    assert (rec["rule"], rec["fallback_on"], rec["dust_usd"]) == ("activity_fallback_v1", False, 100.0)
    assert (rec["listed_pair_count"], rec["response_pair_count"]) == (2, 3)
    assert (rec["triggered"], rec["differs"]) == (True, True)
    assert (stats["pair_address"], stats["price"], stats["market_cap"]) == ("dust", 0.02, 90000)   # switch off
    assert rec["liquidity_pick"] == {"pair": "dust", "dex": "meteora", "quote_side_usd": 24.36, "liquidity_usd": 48.0,
                                     "txns_h1": 0, "volume_h1": 12.0, "price": 0.02, "mcap": 90000}
    assert rec["activity_pick"] == {"pair": "active", "dex": "pumpswap", "quote_side_usd": 20.0, "liquidity_usd": 20.0,
                                    "txns_h1": 330, "volume_h1": 20000.0, "price": 0.01, "mcap": 45000}
    assert stats["volume_h1"] == 20012.0
    json.dumps(stats)
    busy_dust = _pair("dust", quote_side=24.36, liquidity=48.0, h1_buys=900, h1_sells=0)
    rec = q._stats_from_pairs([busy_dust, active], MINT, "solana")["pair_selection"]
    assert (rec["triggered"], rec["differs"]) == (True, False)
    deep = _pair("deep", liquidity=5000.0, h1_buys=1, h1_sells=0)
    rec = q._stats_from_pairs([dust, active, deep], MINT, "solana")["pair_selection"]
    assert (rec["listed_pair_count"], rec["triggered"], rec["differs"]) == (3, False, False)
    assert rec["activity_pick"] == rec["liquidity_pick"] and rec["liquidity_pick"]["pair"] == "deep"


def test_with_the_switch_on_every_selected_pair_field_comes_from_the_activity_pick(fallback_on):
    pairs = [_pair("dust", quote_side=24.36, liquidity=48.0, price="0.02", mcap=90000, h1_buys=0, h1_sells=0,
                   volume_h1=12.0, dex="meteora"),
             _pair("active", liquidity=20.0, price="0.01", mcap=45000, h1_buys=180, h1_sells=150,
                   volume_h1=20000.0, dex="pumpswap")]
    stats = q._stats_from_pairs(pairs, MINT, "solana")
    assert (stats["pair_address"], stats["dex_id"], stats["price"], stats["market_cap"], stats["liquidity"]) == (
        "active", "pumpswap", 0.01, 45000, 20.0)
    assert (stats["pair_selection"]["fallback_on"], stats["pair_selection"]["differs"]) == (True, True)
    assert stats["volume_h1"] == 20012.0


def test_marks_store_the_pair_selection_record_and_null_without_one():
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_exits import write_marks
    conn = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(conn)
    record = {"rule": "activity_fallback_v1", "triggered": True, "differs": True}
    write_marks(conn, {("solana", "M1"): {"price": 1.0, "liquidity": 48.0, "source": "dexscreener",
                                          "pair_selection": record},
                       ("solana", "M2"): {"price": 2.0, "source": "fastlane_mid"},
                       ("solana", "M3"): {"price": 3.0, "source": "dexscreener",
                                          "pair_selection": {**record, "triggered": False, "differs": False}}},
                "2026-09-17T13:00:00+00:00", min_interval_s=0, open_min_interval_s=0)
    got = dict(conn.execute("SELECT mint, pair_selection_json FROM memecoin_position_marks").fetchall())
    assert json.loads(got["M1"]) == record and got["M2"] is None and got["M3"] is None


def test_an_unserializable_record_never_blocks_the_mark_write():
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_exits import write_marks
    conn = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(conn)
    write_marks(conn, {("solana", "M1"): {"price": 1.0, "pair_selection": {"triggered": True, "bad": float("nan")}}},
                "2026-09-17T13:00:00+00:00", min_interval_s=0, open_min_interval_s=0)
    assert conn.execute("SELECT mint, pair_selection_json FROM memecoin_position_marks").fetchall() == [("M1", None)]


SOL_MINT = "B5ANwoWkptDv1QszdPHXjCF2gTWdo27FGLffdfXzzoe6"
T = "2026-09-16T07:45:21.226799+00:00"


@pytest.fixture
def hook_env(monkeypatch):
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as R
    from sniper.services import memecoin_solana_address_activity as A
    for name, value in {
        "MEMECOIN_REAL_MIN_VOLUME_H1_USD": 3000.0, "MEMECOIN_REAL_MIN_TURNOVER_H1": 0.01,
        "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD": 2_000_000.0,
        "MEMECOIN_REAL_STRICT_VOLUME_SOURCE_IDS": frozenset(), "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED": True,
        "MEMECOIN_REAL_MIN_TXNS_M5": 0, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO": 0.0,
        "MEMECOIN_REAL_MAX_SELL_SHARE_PCT": 0.0,
    }.items():
        monkeypatch.setattr(R.cfg, name, value, raising=False)
    monkeypatch.setattr(R.cfg, "inband_min_turnover_m5_for_chain", lambda chain: 0.0)
    monkeypatch.setattr(R.cfg, "inband_max_turnover_m5_for", lambda *args: 0.0)
    monkeypatch.setattr(R, "_alert_entry_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(A, "runtime_probe", lambda: (None, "no_endpoint"))
    monkeypatch.setattr(m, "entry_gate_exemptions", lambda *a, **k: frozenset())
    monkeypatch.setattr(R, "real_entry", lambda *a, **k: {"status": "test_reached"})
    return m, R


def _run_hook(m, R, *, cached):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "Tekkerrss")
    event = SimpleNamespace(
        mint=SOL_MINT, ticker="POKEMON", source_user_id="u1", source_handle="Tekkerrss", network_id="solana",
        side="buy", fomo_event_id="swap:raybot:%s:u1:%s" % ("5" * 88, SOL_MINT), created_at=T,
        their_price=8.5e-05, their_size_usd=698.6, signal_source="follow_activity", is_first_buy=True)
    record = {"rule": "activity_fallback_v1", "fallback_on": False, "dust_usd": 100.0, "listed_pair_count": 3,
              "response_pair_count": 3, "triggered": True, "differs": True,
              "liquidity_pick": {"pair": "Dust1"}, "activity_pick": {"pair": "Active1"}}
    market = {"price": 8.5e-05, "market_cap": 79704, "volume_h1": 22.08, "volume_m5": None, "liquidity": 48.0,
              "pair_address": "Dust1", "dex_id": "meteora", "pair_selection": record}
    R.real_entry_hook(
        conn, event, {"solana": {}}, now_fn=lambda: T, trade_size=175, max_age_s=120,
        stats_fn=None if cached else (lambda mint, network_id=None: dict(market)),
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={"effective_mcap": 79_675.4, "assumed_micro": False, "consensus_n_60m": 1,
                                "market_stats": dict(market) if cached else None})
    trace = json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0])
    events = [e for e in trace["substage_events"] if e.get("name") == "dex_pair_selection"]
    return events, record


@pytest.mark.parametrize("cached", [True, False])
def test_the_entry_trace_records_the_pair_selection_for_cached_and_fetched_stats(hook_env, cached):
    m, R = hook_env
    events, record = _run_hook(m, R, cached=cached)
    assert len(events) == 1
    assert {k: events[0][k] for k in record} == record
