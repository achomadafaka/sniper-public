"""The mcap-scaled minimum-buy alert rule, and the funded leg's source-size record.

Scope note (2026-08-12): the rule is ALERT-ONLY. The 0.1% arm showed no per-coin separation in
paper (mean delta -0.19 pts, 90% CI [-11.35, +15.04], median against the rule), and the funded leg
cannot even compute it yet, so nothing here gates admission or money.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg
from sniper.services.memecoin_runner import want_front_run_alert


class _Ev:
    def __init__(self, size, mcap, uid="u1"):
        self.side = "buy"
        self.their_size_usd = size
        self.their_market_cap = mcap
        self.source_user_id = uid


def test_min_buy_usd_is_the_larger_of_the_floor_and_the_mcap_share():
    assert cfg.tg_min_buy_usd(5_000, floor=100.0, pct=0.1) == 100.0        # floor wins
    assert cfg.tg_min_buy_usd(1_000_000, floor=100.0, pct=0.1) == 1000.0   # share wins
    assert cfg.tg_min_buy_usd(30_000_000, floor=100.0, pct=0.1) == 30000.0


def test_a_missing_mcap_falls_back_to_the_flat_floor():
    """An unknown mcap must not silently disable the floor, and must not invent a share of zero."""
    assert cfg.tg_min_buy_usd(None, floor=100.0, pct=0.1) == 100.0
    assert cfg.tg_min_buy_usd(0, floor=100.0, pct=0.1) == 100.0


def test_pct_zero_is_the_old_flat_behaviour():
    """Default-inert: with the share at 0 the rule is byte-identical to the shipped flat floor."""
    assert cfg.tg_min_buy_usd(30_000_000, floor=100.0, pct=0.0) == 100.0


def test_alert_rejects_a_buy_under_the_mcap_share(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MIN_USD", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MIN_MCAP_PCT", 0.1)
    curated = {"u1"}
    # $1M mcap -> needs $1,000
    assert want_front_run_alert(_Ev(999.0, 1_000_000), curated, prior_fn=lambda ev: 0) is False
    assert want_front_run_alert(_Ev(1500.0, 1_000_000), curated, prior_fn=lambda ev: 0) is True


def test_alert_still_applies_the_floor_on_a_tiny_coin(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MIN_USD", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MIN_MCAP_PCT", 0.1)
    curated = {"u1"}
    # $5k mcap -> 0.1% is $5, so the $100 floor is the binding rule
    assert want_front_run_alert(_Ev(50.0, 5_000), curated, prior_fn=lambda ev: 0) is False
    assert want_front_run_alert(_Ev(150.0, 5_000), curated, prior_fn=lambda ev: 0) is True


# --- funded leg records the source size ---------------------------------------------------------

def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _event(c, event_id, size):
    c.execute(
        "INSERT INTO memecoin_events (fomo_event_id, source_user_id, mint, side, their_size_usd,"
        " capture_version, clock_domain) VALUES (?,?,?,?,?,?,?)",
        (event_id, "u1", "MINT1", "buy", size, 2, "observed_real_utc"))


def test_resolve_source_size_reads_the_originating_event():
    c = _conn()
    _event(c, "evt-1", 12729.05)
    assert m.resolve_source_size_usd(c, "evt-1") == pytest.approx(12729.05)


def test_resolve_source_size_preserves_a_recorded_zero():
    """A measured zero remains distinct from an absent size."""
    c = _conn()
    _event(c, "evt-0", 0.0)
    assert m.resolve_source_size_usd(c, "evt-0") == 0.0


def test_resolve_source_size_is_none_when_unknown():
    """None means unknown. 0.0 would read as a real zero-dollar buy, which is what the funded
    executors wrote for every position — 129 closed funded rows with no recoverable size."""
    c = _conn()
    assert m.resolve_source_size_usd(c, "missing") is None
    assert m.resolve_source_size_usd(c, None) is None


# ---------- alert mcap ceiling (2026-08-17, operator: alerts only for sub-$100k coins) ----------


def test_tg_mcap_ok_semantics():
    assert cfg.tg_mcap_ok(50_000, ceiling=100_000) is True
    assert cfg.tg_mcap_ok(100_000, ceiling=100_000) is True     # at the ceiling: still under
    assert cfg.tg_mcap_ok(150_000, ceiling=100_000) is False
    # fail-open: an unknown mcap is a fresh coin, not a big one; ceiling 0 = off
    assert cfg.tg_mcap_ok(None, ceiling=100_000) is True
    assert cfg.tg_mcap_ok(0, ceiling=100_000) is True
    assert cfg.tg_mcap_ok("garbage", ceiling=100_000) is True
    assert cfg.tg_mcap_ok(150_000, ceiling=0) is True


def test_front_run_alert_refuses_a_known_big_coin_even_curated(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MIN_USD", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MIN_MCAP_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MAX_MCAP_USD", 100_000.0)
    curated = {"u1"}
    assert want_front_run_alert(_Ev(500.0, 250_000), curated, prior_fn=lambda ev: 0) is False
    assert want_front_run_alert(_Ev(500.0, 90_000), curated, prior_fn=lambda ev: 0) is True
    # unknown mcap = fresh coin: the alert still fires (fail-open, matches assume-micro admission)
    assert want_front_run_alert(_Ev(500.0, None), curated, prior_fn=lambda ev: 0) is True


def test_follow_alert_kind_refuses_a_known_big_coin(monkeypatch):
    from sniper.services.memecoin_follow_activity import _follow_alert_kind
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MAX_MCAP_USD", 100_000.0)
    big = _Ev(500.0, 250_000); big.lag_s = 1.0
    assert _follow_alert_kind(big, armed=True, min_usd=100.0) is None
    small = _Ev(500.0, 90_000); small.lag_s = 1.0
    assert _follow_alert_kind(small, armed=True, min_usd=100.0) == "fresh"
    fresh = _Ev(500.0, None); fresh.lag_s = 1.0
    assert _follow_alert_kind(fresh, armed=True, min_usd=100.0) == "fresh"
