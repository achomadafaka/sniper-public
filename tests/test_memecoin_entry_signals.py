"""Consensus entry signals — surfacing/ranking (never auto-buy)."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_entry_signals import consensus_setups

NOW = "2026-07-19T06:00:00+00:00"


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    for uid in ("a1", "a2", "a3", "cold"):
        m.add_wallet(c, uid, uid)
    for uid in ("a1", "a2", "a3"):
        m.set_wallet_real_approved(c, uid, True, force=True)
    return c


def _buy(c, uid, mint, ts, side="buy"):
    m.insert_feed_observation_v2(c, {
        "upstream_event_id": f"{uid}-{mint}-{ts}", "raw_type": "large_buy", "normalized_side": side,
        "source_user_id": uid, "source_handle": uid, "network_id": "solana", "mint": mint, "ticker": mint,
        "their_price": 1.0, "their_market_cap": 1.0, "their_size_usd": 1.0,
        "source_ts_raw": ts, "source_ts": ts, "observed_at": ts, "lag_s": 0.0, "raw_payload": {}},
        was_curated=True, was_real_approved=True)


def test_consensus_requires_min_armed_and_excludes_sells_and_cold():
    c = _c()
    # HOT: 3 armed buys, fresh
    for uid, t in [("a1", "05:50:00"), ("a2", "05:52:00"), ("a3", "05:55:00")]:
        _buy(c, uid, "HOT", f"2026-07-19T{t}+00:00")
    # ONE armed buyer -> below min_armed=2 -> excluded
    _buy(c, "a1", "SOLO", "2026-07-19T05:50:00+00:00")
    # a2 SELLS a coin + cold buys it -> not a buy-consensus -> excluded
    _buy(c, "a2", "SELLY", "2026-07-19T05:50:00+00:00", side="sell")
    _buy(c, "cold", "SELLY", "2026-07-19T05:51:00+00:00")
    qmap = {("solana", "HOT"): {"liquidity": 20000.0, "market_cap": 8e5, "volume_24h": 3e5}}
    out = consensus_setups(c, window_min=90, min_armed=2, quote_map=qmap, now_iso=NOW)
    assert [r["mint"] for r in out] == ["HOT"]
    assert out[0]["armed"] == 3 and out[0]["hot"] is True and out[0]["setup_score"] > 50


def test_excludes_held_and_stale_out_of_window():
    c = _c()
    for uid, t in [("a1", "05:55:00"), ("a2", "05:56:00")]:
        _buy(c, uid, "HELD", f"2026-07-19T{t}+00:00")
    for uid, t in [("a1", "03:00:00"), ("a2", "03:01:00")]:      # 3h ago -> outside 90m window
        _buy(c, uid, "OLD", f"2026-07-19T{t}+00:00")
    out = consensus_setups(c, window_min=90, min_armed=2, held_keys={("solana", "HELD")}, now_iso=NOW)
    assert out == []                                              # HELD excluded, OLD out of window


def test_hot_requires_liquidity_and_small_mcap():
    c = _c()
    for uid, t in [("a1", "05:55:00"), ("a2", "05:56:00")]:
        _buy(c, uid, "BIG", f"2026-07-19T{t}+00:00")
    qmap = {("solana", "BIG"): {"liquidity": 20000.0, "market_cap": 5e7}}   # $50M -> too big for hot
    out = consensus_setups(c, window_min=90, min_armed=2, quote_map=qmap, now_iso=NOW)
    assert out[0]["armed"] == 2 and out[0]["hot"] is False       # surfaced but not a hot setup


def _alpha_close(c, pid, uid, pnl):
    """A closed alpha_b PAPER position (honest copy evidence for a wallet — no fomo fill)."""
    c.execute("INSERT INTO memecoin_positions(id, mint, ticker, entry_price, entry_notional, network_id, "
              "is_open, execution_mode, strategy, source_user_id, realized_pnl, opened_at, closed_at) "
              "VALUES(?,?,?,?,?,?,0,'paper','alpha_b',?,?,?,?)",
              (pid, "X"+str(pid), "X", 0.001, 50.0, "solana", uid, pnl,
               "2026-07-18T00:00:00+00:00", "2026-07-18T01:00:00+00:00"))


def test_consensus_grades_armed_by_copy_pnl():
    # Item 1: a cluster of 2 PROVEN copiers outranks a cluster of 2 with a proven LOSER + unproven.
    c = _c()   # a1,a2,a3 real_approved; add a4
    m.add_wallet(c, "a4", "a4"); m.set_wallet_real_approved(c, "a4", True, force=True)
    # a1,a2 clear the promote gate (>=5 closes, >=$25); a3 is a proven loser (>=3 closes, negative)
    for i, pid in enumerate(range(1, 6)):
        _alpha_close(c, pid, "a1", 30.0)
        _alpha_close(c, 100 + pid, "a2", 25.0)
    for pid in range(1, 4):
        _alpha_close(c, 200 + pid, "a3", -20.0)
    # a4 has NO closes -> unproven
    for uid, t in [("a1", "05:55:00"), ("a2", "05:56:00")]:
        _buy(c, uid, "PROVEN", f"2026-07-19T{t}+00:00")
    for uid, t in [("a3", "05:55:00"), ("a4", "05:56:00")]:
        _buy(c, uid, "CAUTION", f"2026-07-19T{t}+00:00")
    c.commit()
    qmap = {("solana", "PROVEN"): {"liquidity": 20000.0, "market_cap": 8e5},
            ("solana", "CAUTION"): {"liquidity": 20000.0, "market_cap": 8e5}}
    out = consensus_setups(c, window_min=90, min_armed=2, quote_map=qmap, now_iso=NOW)
    by = {r["mint"]: r for r in out}
    assert by["PROVEN"]["armed_proven"] == 2 and by["PROVEN"]["armed_negative"] == 0
    assert by["PROVEN"]["quality"] == "proven"
    assert by["CAUTION"]["armed_proven"] == 0 and by["CAUTION"]["armed_negative"] == 1
    assert by["CAUTION"]["quality"] == "caution"
    assert by["PROVEN"]["setup_score"] > by["CAUTION"]["setup_score"]
    assert out[0]["mint"] == "PROVEN"                        # proven cluster ranks first


def test_hot_requires_60m_cluster_not_two_buys_over_window():
    # C2 regression: 1 armed buy 85m ago + 1 armed buy 5m ago = 2 armed over 90m but NOT a 60m cluster.
    # Must surface (armed==2) but NOT be hot (armed_60m==1).
    c = _c()
    _buy(c, "a1", "SPREAD", "2026-07-19T04:35:00+00:00")     # 85m ago -> in 90m window, outside 60m
    _buy(c, "a2", "SPREAD", "2026-07-19T05:55:00+00:00")     # 5m ago -> in the cluster
    # a true cluster for contrast
    _buy(c, "a1", "TIGHT", "2026-07-19T05:20:00+00:00")
    _buy(c, "a2", "TIGHT", "2026-07-19T05:55:00+00:00")
    qmap = {("solana", "SPREAD"): {"liquidity": 20000.0, "market_cap": 8e5},
            ("solana", "TIGHT"): {"liquidity": 20000.0, "market_cap": 8e5}}
    out = {r["mint"]: r for r in consensus_setups(c, window_min=90, min_armed=2, quote_map=qmap, now_iso=NOW)}
    assert out["SPREAD"]["armed"] == 2 and out["SPREAD"]["armed_60m"] == 1 and out["SPREAD"]["hot"] is False
    assert out["TIGHT"]["armed"] == 2 and out["TIGHT"]["armed_60m"] == 2 and out["TIGHT"]["hot"] is True


def _buy_evm(c, uid, mint, ts):
    m.insert_feed_observation_v2(c, {
        "upstream_event_id": f"{uid}-{mint}-{ts}", "raw_type": "large_buy", "normalized_side": "buy",
        "source_user_id": uid, "source_handle": uid, "network_id": "4663", "mint": mint, "ticker": "EVM",
        "their_price": 1.0, "their_market_cap": 1.0, "their_size_usd": 1.0,
        "source_ts_raw": ts, "source_ts": ts, "observed_at": ts, "lag_s": 0.0, "raw_payload": {}},
        was_curated=True, was_real_approved=True)


def test_held_excludes_checksummed_evm_mint_against_lowercase_feed():
    # C1 regression: a held 4663 position whose stored mint is CHECKSUMMED must exclude the coin even
    # though the feed row is lowercase — else it re-surfaces as a fresh HOT setup (manual re-buy path).
    c = _c()
    lower = "0xabcdef0123456789abcdef0123456789abcdef01"
    checksummed = "0xABCDEF0123456789ABCDEF0123456789ABCDEF01"
    for uid, t in [("a1", "05:55:00"), ("a2", "05:56:00")]:
        _buy_evm(c, uid, lower, f"2026-07-19T{t}+00:00")
    # quote_map keyed canonically (as radar_quote_map produces) still prices it
    qmap = {m.accounting_mark_key("4663", lower): {"liquidity": 20000.0, "market_cap": 8e5}}
    out_unheld = consensus_setups(c, window_min=90, min_armed=2, quote_map=qmap, now_iso=NOW)
    assert len(out_unheld) == 1 and out_unheld[0]["liquidity"] == 20000.0     # priced via canonical key
    out_held = consensus_setups(c, window_min=90, min_armed=2,
                                held_keys={("4663", checksummed)}, quote_map=qmap, now_iso=NOW)
    assert out_held == []                                          # checksummed held mint still excludes


def test_a_dead_quote_cache_no_longer_pins_hot_to_false():
    # `hot` reads liquidity ONLY from quote_map. The radar quote daemon that filled that cache has
    # been stopped since 2026-08-21, so every candidate scored hot=False no matter how it traded --
    # the badge went dark and stuck-bags could never name a blocked setup.
    c = _c()
    for uid, t in [("a1", "05:55:00"), ("a2", "05:56:00")]:
        _buy(c, uid, "LIVE", f"2026-07-19T{t}+00:00")

    cold = consensus_setups(c, window_min=90, min_armed=2, quote_map={}, now_iso=NOW)
    assert cold[0]["hot"] is False and cold[0]["liquidity"] is None    # the bug

    warm = consensus_setups(c, window_min=90, min_armed=2, quote_map={}, now_iso=NOW,
                            quote_fill_fn=lambda keys: {
                                ("solana", "LIVE"): {"liquidity": 20000.0, "market_cap": 8e5}})
    assert warm[0]["hot"] is True and warm[0]["liquidity"] == 20000.0


def test_the_fill_asks_only_for_candidates_it_still_needs():
    c = _c()
    for mint in ("NEED", "HELD", "HAVE"):
        for uid, t in [("a1", "05:55:00"), ("a2", "05:56:00")]:
            _buy(c, uid, mint, f"2026-07-19T{t}+00:00")
    asked = []

    consensus_setups(c, window_min=90, min_armed=2, now_iso=NOW,
                     held_keys={("solana", "HELD")},
                     quote_map={("solana", "HAVE"): {"liquidity": 9000.0, "market_cap": 4e5}},
                     quote_fill_fn=lambda keys: asked.extend(keys) or {})

    assert [mm for _n, mm in asked] == ["NEED"]     # held is excluded, already-priced is skipped


def test_a_failing_fill_leaves_liquidity_unknown_instead_of_raising():
    c = _c()
    for uid, t in [("a1", "05:55:00"), ("a2", "05:56:00")]:
        _buy(c, uid, "LIVE", f"2026-07-19T{t}+00:00")

    def _boom(keys):
        raise RuntimeError("provider down")

    out = consensus_setups(c, window_min=90, min_armed=2, quote_map={}, now_iso=NOW,
                           quote_fill_fn=_boom)

    assert out[0]["liquidity"] is None and out[0]["hot"] is False   # fail-open, never hot on unknown


def test_the_fill_does_not_mutate_the_callers_cache():
    c = _c()
    for uid, t in [("a1", "05:55:00"), ("a2", "05:56:00")]:
        _buy(c, uid, "LIVE", f"2026-07-19T{t}+00:00")
    shared = {}

    consensus_setups(c, window_min=90, min_armed=2, quote_map=shared, now_iso=NOW,
                     quote_fill_fn=lambda keys: {("solana", "LIVE"): {"liquidity": 20000.0}})

    assert shared == {}      # the route passes one qmap to several calls; the fill is per-call
