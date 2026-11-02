"""Regression tests for the 2026-07-28 signal-speed pass.

Three fixes, each with the failure it would have caught:

1. resolve_mint_mcap / resolve_mint_ticker were events-only, which is circular for a coin seen
   ONLY via follow_activity — the sole row for that mint is the blank one being filled. Measured
   94% of follow-lane events carried ticker='' and mcap=0 for exactly this reason.
2. The watch-only 300s follow lane is off by default (85% of its catches landed outside the
   +EV <60s band by construction).
3. MEMECOIN_POLL_MS had two different defaults (settings 500, runner 1500) for one knob.
"""
import os
import sqlite3

from sniper.persistence import db_memecoin as m

SOL_MINT = "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


# --- 1. capture-table fallback -------------------------------------------------------------

def test_mcap_falls_back_to_quote_observations_when_events_are_blank():
    """The follow_activity-only case: no priced event row exists, capture has the mcap."""
    c = _conn()
    assert m.resolve_mint_mcap(c, SOL_MINT, network_id="solana") is None

    for sweep, mcap in (("s1", 900_000.0), ("s2", 1_100_000.0), ("s3", 1_000_000.0)):
        c.execute(
            "INSERT INTO memecoin_quote_observations_v2"
            "(sweep_id,network_id,mint,observed_at,market_cap,quality,contexts) "
            "VALUES(?,?,?,?,?,?,?)",
            (sweep, "solana", SOL_MINT, f"2026-07-28T0{sweep[-1]}:00:00+00:00", mcap,
             "exact", "[]"),
        )
    c.commit()

    # median of the three readings, not the newest and not the mean
    assert m.resolve_mint_mcap(c, SOL_MINT, network_id="solana") == 1_000_000.0


def test_mcap_capture_fallback_is_network_scoped():
    """A 0x address is not a coin identity — a Base reading must not fill a 4663 lookup."""
    c = _conn()
    evm = "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18"
    c.execute(
        "INSERT INTO memecoin_quote_observations_v2"
        "(sweep_id,network_id,mint,observed_at,market_cap,quality,contexts) "
        "VALUES('s1','8453',?,'2026-07-28T00:00:00+00:00',5000000.0,'exact','[]')", (evm,))
    c.commit()
    assert m.resolve_mint_mcap(c, evm, network_id="8453") == 5_000_000.0
    assert m.resolve_mint_mcap(c, evm, network_id="4663") is None


def test_mcap_prefers_priced_events_over_capture():
    """Capture is a fallback, not an override — a real event reading still wins."""
    c = _conn()
    m.add_wallet(c, "u1", "alice", source="follow")
    c.execute(
        "INSERT INTO memecoin_events(fomo_event_id,source_user_id,mint,side,their_market_cap,"
        "network_id,created_at,received_at) VALUES('e1','u1',?,'buy',777.0,'solana',"
        "'2026-07-28T00:00:00+00:00','2026-07-28T00:00:00+00:00')", (SOL_MINT,))
    c.execute(
        "INSERT INTO memecoin_quote_observations_v2"
        "(sweep_id,network_id,mint,observed_at,market_cap,quality,contexts) "
        "VALUES('s1','solana',?,'2026-07-28T00:00:00+00:00',999999.0,'exact','[]')", (SOL_MINT,))
    c.commit()
    assert m.resolve_mint_mcap(c, SOL_MINT, network_id="solana") == 777.0


def test_mcap_still_none_when_nothing_ever_priced_the_coin():
    """Fail-closed is preserved: an unknown mint must not resolve to 0.0 or a guess."""
    c = _conn()
    assert m.resolve_mint_mcap(c, "NeverSeenMint111111111111111111111111111111",
                               network_id="solana") is None


def test_ticker_falls_back_to_feed_observations():
    c = _conn()
    assert m.resolve_mint_ticker(c, SOL_MINT, network_id="solana") is None
    c.execute(
        "INSERT INTO memecoin_feed_observations_v2"
        "(upstream_event_id,raw_type,source_user_id,network_id,mint,ticker,observed_at,raw_json,"
        " clock_domain) VALUES('u1','large_buy','uid','solana',?,'EPIK',"
        "'2026-07-28T00:00:00+00:00','{}','observed_real_utc')", (SOL_MINT,))
    c.commit()
    assert m.resolve_mint_ticker(c, SOL_MINT, network_id="solana") == "EPIK"


# --- 2. slow lane killed -------------------------------------------------------------------

def test_slow_follow_lane_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MEMECOIN_FOLLOW_SLOW_ENABLED", raising=False)
    import importlib
    import sniper.settings.memecoin as cfg
    importlib.reload(cfg)
    assert cfg.MEMECOIN_FOLLOW_SLOW_ENABLED is False
    assert cfg.MEMECOIN_FOLLOW_FAST_ENABLED is True, "killing slow must not kill the fast lane"


def test_slow_follow_lane_can_be_restored(monkeypatch):
    monkeypatch.setenv("MEMECOIN_FOLLOW_SLOW_ENABLED", "1")
    import importlib
    import sniper.settings.memecoin as cfg
    importlib.reload(cfg)
    assert cfg.MEMECOIN_FOLLOW_SLOW_ENABLED is True
    monkeypatch.delenv("MEMECOIN_FOLLOW_SLOW_ENABLED", raising=False)
    importlib.reload(cfg)


# --- 3. one poll knob ----------------------------------------------------------------------

def test_poll_ms_is_one_source_and_clamped_to_the_waf_floor(monkeypatch):
    import importlib
    import sniper.settings.memecoin as cfg

    monkeypatch.setenv("MEMECOIN_POLL_MS", "250")       # below the floor
    importlib.reload(cfg)
    assert cfg.MEMECOIN_POLL_MS == 1000, "must clamp up to the documented WAF floor"

    monkeypatch.setenv("MEMECOIN_POLL_MS", "2500")      # above the floor
    importlib.reload(cfg)
    assert cfg.MEMECOIN_POLL_MS == 2500

    monkeypatch.delenv("MEMECOIN_POLL_MS", raising=False)
    importlib.reload(cfg)
    assert cfg.MEMECOIN_POLL_MS == 1000, "default is the floor, not the old unreachable 500"


def test_runner_loop_uses_the_settings_poll_value():
    """The runner must not re-derive the cadence from env with a different default."""
    import scripts.memecoin_runner_loop as runner_loop
    from sniper.settings.memecoin import MEMECOIN_POLL_MS
    assert runner_loop.POLL_MS == MEMECOIN_POLL_MS
