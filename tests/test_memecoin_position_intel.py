"""Per-position recovery intelligence — verdict + score logic."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_position_intel import position_verdict, last_armed_buy_minutes


def _pos(entry=0.001, notional=50.0, opened="2026-07-19T00:00:00+00:00", derisked=None, lqp=None):
    return {"entry_price": entry, "entry_notional": notional, "opened_at": opened,
            "derisked_at": derisked, "last_quote_price": lqp}


NOW = "2026-07-19T06:00:00+00:00"


def test_dead_liquidity_is_untradeable():
    v = position_verdict(_pos(entry=0.001), {"price": 0.0005, "liquidity": 186.0, "market_cap": 1e5,
                                             "volume_24h": 2e5}, last_armed_buy_min=300, now_iso=NOW)
    assert v["verdict"] == "DEAD" and v["recovery_score"] < 30 and "untradeable" in v["suggested"]


def test_derisked_runner_rides():
    v = position_verdict(_pos(entry=0.0002, derisked="2026-07-19T03:00:00+00:00"),
                         {"price": 0.00037, "liquidity": 54000.0, "market_cap": 350000.0,
                          "volume_24h": 660000.0}, last_armed_buy_min=30, now_iso=NOW)
    assert v["verdict"] == "RIDE" and v["pnl_pct"] > 30 and "trail" in v["suggested"]


def test_up_position_says_lock():
    v = position_verdict(_pos(entry=0.001), {"price": 0.0013, "liquidity": 40000.0, "market_cap": 5e5,
                                             "volume_24h": 3e5}, last_armed_buy_min=60, now_iso=NOW)
    assert v["verdict"] == "LOCK" and v["pnl_pct"] >= 15


def test_deep_loss_stale_thin_is_cut():
    v = position_verdict(_pos(entry=0.001), {"price": 0.00032, "liquidity": 7000.0, "market_cap": 185000.0,
                                             "volume_24h": 7e5}, last_armed_buy_min=None, now_iso=NOW)
    assert v["verdict"] == "CUT" and v["pnl_pct"] <= -55


def test_healthy_recoverable_holds():
    v = position_verdict(_pos(entry=0.001), {"price": 0.0008, "liquidity": 130000.0, "market_cap": 6e5,
                                             "volume_24h": 4e5}, last_armed_buy_min=120, now_iso=NOW)
    assert v["verdict"] == "HOLD" and v["recovery_score"] >= 55


def test_score_rewards_liquidity_and_penalizes_drawdown():
    good = position_verdict(_pos(), {"price": 0.001, "liquidity": 50000.0, "market_cap": 5e5,
                                     "volume_24h": 5e5}, last_armed_buy_min=10, now_iso=NOW)
    bad = position_verdict(_pos(), {"price": 0.0003, "liquidity": 800.0, "market_cap": 5e7,
                                    "volume_24h": 1e4}, last_armed_buy_min=None, now_iso=NOW)
    assert good["recovery_score"] > bad["recovery_score"]
    assert good["components"]["liq"] > bad["components"]["liq"]
    assert good["components"]["drawdown"] > bad["components"]["drawdown"]


def test_unknown_liquidity_not_dead_and_not_depressed():
    # B3 regression: a 4663/no-pair position with unknown liquidity (None or 0.0) must NOT be DEAD, and
    # its score must reflect the KNOWN components (liq excluded + renormalized), not be dragged by liq=0.
    for liq in (None, 0.0):
        v = position_verdict(_pos(entry=0.001),
                             {"price": 0.0009, "liquidity": liq, "market_cap": 6e5, "volume_24h": 4e5},
                             last_armed_buy_min=60, now_iso=NOW)
        assert v["verdict"] != "DEAD"
        assert v["components"]["liq"] is None                 # excluded, not scored 0
        assert v["recovery_score"] >= 55 and v["verdict"] == "HOLD"


def test_manual_buy_missing_freshness_not_penalized():
    # B4 regression: a healthy manual-buy position (no armed-wallet buy row) must not be dragged toward
    # WATCH/CUT purely for missing freshness — the freshness component is excluded, not scored 0.
    v = position_verdict(_pos(entry=0.001),
                         {"price": 0.0009, "liquidity": 130000.0, "market_cap": 6e5, "volume_24h": 4e5},
                         last_armed_buy_min=None, now_iso=NOW)
    assert v["components"]["freshness"] is None
    assert v["verdict"] == "HOLD" and v["recovery_score"] >= 55


def test_last_armed_buy_minutes():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "a1", "A1"); m.set_wallet_real_approved(c, "a1", True, force=True)
    m.add_wallet(c, "c1", "C1")   # cold
    for uid, side, ts in [("a1", "buy", "2026-07-19T05:30:00+00:00"),
                          ("c1", "buy", "2026-07-19T05:55:00+00:00")]:   # cold buy ignored
        m.insert_feed_observation_v2(c, {
            "upstream_event_id": f"{uid}-{ts}", "raw_type": "large_buy", "normalized_side": side,
            "source_user_id": uid, "source_handle": uid, "network_id": "solana", "mint": "MX",
            "ticker": "MX", "their_price": 1.0, "their_market_cap": 1.0, "their_size_usd": 1.0,
            "source_ts_raw": ts, "source_ts": ts, "observed_at": ts, "lag_s": 0.0, "raw_payload": {}},
            was_curated=True, was_real_approved=True)
    mins = last_armed_buy_minutes(c, "solana", "MX", now_iso=NOW)
    assert mins == 30.0            # armed buy at 05:30, now 06:00 (cold 05:55 ignored)
    assert last_armed_buy_minutes(c, "solana", "NONE", now_iso=NOW) is None
