"""Auto-exit classifier — archetype policies + post-entry sweep."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_entry_classifier import classify_entry, apply_entry_classification


def test_thin_liquidity_gets_fast_tp():
    c = classify_entry(liquidity=600.0, mcap=9e5)
    assert c["archetype"] == "thin_liq"
    assert c["policy"]["tp_pct"] == 30.0 and c["policy"]["trail_pct"] == 20.0
    assert c["policy"]["derisk_pct"] == 40.0     # trail-set overrides MUST also set derisk


def test_runner_gets_protective_trail_not_loose():
    c = classify_entry(liquidity=50000.0, mcap=5e5, momentum_1h=40.0)
    assert c["archetype"] == "runner" and c["policy"]["trail_pct"] == 25.0
    assert c["policy"]["derisk_pct"] == 40.0


def test_dip_gets_tighter_stop():
    c = classify_entry(liquidity=50000.0, mcap=5e5, momentum_1h=-25.0)
    assert c["archetype"] == "dip" and c["policy"]["sl_pct"] == 45.0


def test_liquid_neutral_keeps_band_default():
    c = classify_entry(liquidity=50000.0, mcap=5e5, momentum_1h=5.0)
    assert c["archetype"] == "normal" and c["policy"] is None


def test_radar_quote_cache_roundtrips_h1_momentum_and_drives_runner():
    # B2 regression: the runner/dip archetypes need 1h momentum at classify time. Position marks don't
    # exist in the <45m window, so momentum comes from the cached DexScreener price_change_h1.
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    now = "2026-07-19T06:00:00+00:00"
    m.upsert_radar_quotes(c, [{"network_id": "solana", "mint": "RUN", "market_cap": 5e5,
                               "liquidity": 5e4, "price_change_h1": 40.0}], now)
    q = m.radar_quote_map(c, now_iso=now)[m.accounting_mark_key("solana", "RUN")]
    assert q["price_change_h1"] == 40.0                       # persisted + returned
    cls = classify_entry(liquidity=q["liquidity"], mcap=q["market_cap"], momentum_1h=q["price_change_h1"])
    assert cls["archetype"] == "runner" and cls["policy"]["trail_pct"] == 25.0


def _open(c, pid, mint, opened, liq_net="4663", signal_source="global_feed"):
    c.execute("INSERT INTO memecoin_positions(id, mint, ticker, entry_price, entry_notional, network_id, "
              "is_open, execution_mode, opened_at, signal_source) VALUES(?,?,?,?,?,?,1,'real',?,?)",
              (pid, mint, "T"+str(pid), 0.001, 50.0, liq_net, opened, signal_source))


def test_sweep_applies_to_recent_thin_liq_only_and_respects_overrides():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    now = "2026-07-19T06:00:00+00:00"
    _open(c, 1, "0xTHIN", "2026-07-19T05:50:00+00:00")     # recent, thin liq -> classified
    _open(c, 2, "0xDEEP", "2026-07-19T05:50:00+00:00")     # recent, deep liq neutral -> skipped
    _open(c, 3, "0xOLD", "2026-07-19T04:00:00+00:00")      # too old -> skipped
    _open(c, 4, "0xOPR", "2026-07-19T05:55:00+00:00")      # recent thin, but operator override exists
    m.set_position_exit_override(c, 4, "operator", tp_pct=99.0, now_iso=now)
    c.commit()
    quotes = {"0xTHIN": {"liquidity": 500.0, "market_cap": 9e5},
              "0xDEEP": {"liquidity": 80000.0, "market_cap": 5e5, "momentum_1h": 3.0},
              "0xOLD": {"liquidity": 400.0}, "0xOPR": {"liquidity": 400.0}}
    applied = apply_entry_classification(c, quote_fn=lambda net, mint: quotes.get(mint), now_iso=now)
    assert {a["id"] for a in applied} == {1}                # only the recent thin-liq, un-overridden bag
    ov = m.effective_exit_override(c, 1)
    assert ov["source"] == "auto-classify" and ov["tp_pct"] == 30.0
    # operator override on #4 is untouched (respected)
    assert m.effective_exit_override(c, 4)["source"] == "operator"
    # a second sweep is idempotent (auto-classify already present -> not reapplied)
    assert apply_entry_classification(c, quote_fn=lambda net, mint: quotes.get(mint), now_iso=now) == []


def test_sweep_defers_to_runner_cohort_override():
    # B1 regression: a thin-liq position that already carries a runner cohort policy (source 'micro')
    # must be SKIPPED — the tested cohort policy wins and no inert auto-classify row is written.
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    now = "2026-07-19T06:00:00+00:00"
    _open(c, 1, "0xMICRO", "2026-07-19T05:55:00+00:00")     # recent + thin, but runner set 'micro'
    m.set_position_exit_override(c, 1, "micro", derisk_pct=30.0, trail_pct=20.0, sl_pct=50.0, now_iso=now)
    c.commit()
    quotes = {"0xMICRO": {"liquidity": 500.0, "market_cap": 9e5}}
    applied = apply_entry_classification(c, quote_fn=lambda net, mint: quotes.get(mint), now_iso=now)
    assert applied == []                                     # deferred to the cohort policy
    eff = m.effective_exit_override(c, 1)
    assert eff["source"] == "micro"                          # micro still governs the exit
    # even if an auto-classify row somehow coexisted, micro outranks it in the priority list
    assert m._EXIT_OVERRIDE_PRIORITY.index("micro") < m._EXIT_OVERRIDE_PRIORITY.index("auto-classify")


def test_sweep_never_sets_manual_exit_rails():
    """Removing the source filter must add an automatic manual-position rail."""
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    now = "2026-07-19T06:00:00+00:00"
    _open(c, 1, "0xMANUAL", "2026-07-19T05:55:00+00:00", signal_source="manual")

    applied = apply_entry_classification(
        c, quote_fn=lambda _net, _mint: {"liquidity": 500.0}, now_iso=now)

    assert applied == []
    assert m.effective_exit_override(c, 1) is None
