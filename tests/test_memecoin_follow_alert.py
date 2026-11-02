"""Delayed-catch alert for follow-activity buys.

The operator saw a curated trader (Farmercharts) place bets they were never notified about. Cause:
the front-run Telegram alert fires ONLY for `global_feed` buys (a latency alert on a 120-900s-old
backfill signal would be meaningless). But the `follow_activity` path — the 300s poll that recovers
buys the real-time feed missed — was completely silent, and this session wired it to the REAL leg, so
those buys can now open real money with ZERO notification.

Fix: a SEPARATE, clearly-labeled "delayed catch" alert for follow-activity buys. It must never be
confused with the front-run alert (different edge, different latency semantics) and must never
pollute the front-run latency instrument (memecoin_alert_latency stays global_feed-only).
"""
from types import SimpleNamespace

from sniper.services.memecoin_telegram import build_alert_message, build_follow_activity_alert
from sniper.services.memecoin_follow_activity import _should_follow_alert


def _ev(**kw):
    d = dict(source_handle="Farmercharts", ticker="", mint="0xabc123def456",
             their_size_usd=4977.0, network_id="4663", lag_s=320.0, their_market_cap=0.0)
    d.update(kw)
    return SimpleNamespace(**d)


def test_message_is_labeled_a_delayed_catch_not_a_front_run():
    msg = build_follow_activity_alert(_ev())
    assert "DELAYED" in msg.upper() or "follow-activity" in msg.lower()
    assert "Farmercharts" in msg
    # ticker is blank on this path -> the mint must still identify the coin
    assert "0xabc123def456" in msg
    assert "4977" in msg or "$5k" in msg or "5.0k" in msg.lower()


def test_message_shows_the_chain_and_age():
    msg = build_follow_activity_alert(_ev(network_id="4663", lag_s=320.0))
    assert "4663" in msg
    assert "320" in msg or "5m" in msg.lower() or "min" in msg.lower()


def test_falls_back_to_ticker_when_present():
    msg = build_follow_activity_alert(_ev(ticker="PONS"))
    assert "PONS" in msg


def test_fresh_alert_uses_mint_prefix_instead_of_question_mark(monkeypatch):
    # The feed-name lookup reads the live ledger, which now names this mint; this test is about
    # a coin the feed never named.
    import sniper.services.memecoin_telegram as tg
    monkeypatch.setattr(tg, "_ticker_from_feed", lambda _mint: None)
    msg = build_alert_message(_ev(mint="4T4WTEV7vCa1WeaKoHiqus4v9J8GfkuHYPDUas8wpMZx"))

    assert "? —" not in msg
    assert "4T4WTEV7vCa1" in msg.splitlines()[0]


def test_alerts_on_armed_wallet_buy_above_floor(monkeypatch):
    """Since 2026-07-16 the delayed-catch alert is opt-in (MEMECOIN_TG_DELAYED_CATCH_ENABLED,
    default off — 120-900s-old signals are unactionable channel noise; operator call)."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_TG_DELAYED_CATCH_ENABLED", True)
    assert _should_follow_alert(_ev(their_size_usd=4977.0), armed=True, min_usd=1000) is True


def test_delayed_catch_alert_defaults_off():
    assert _should_follow_alert(_ev(their_size_usd=4977.0), armed=True, min_usd=1000) is False


# ── fresh-catch alerts (2026-07-16, fast lane real leg live) ────────────────────────────────
# A <MEMECOIN_TG_MAX_LAG_S armed catch is ARMED — the operator must see it as a normal
# signal (front-run style), independent of the delayed-catch toggle. Stale catches stay behind
# MEMECOIN_TG_DELAYED_CATCH_ENABLED. (The operator never saw smol_intern's micro buys: fresh
# fast-lane catches had NO alert path at all once delayed-catch went dark.)

def test_fresh_armed_catch_alerts_front_run_style(monkeypatch):
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_follow_activity import _follow_alert_kind
    monkeypatch.setattr(cfg, "MEMECOIN_TG_DELAYED_CATCH_ENABLED", False)
    assert _follow_alert_kind(_ev(lag_s=45.0), armed=True, min_usd=1000) == "fresh"


def test_stale_catch_is_delayed_kind_and_flag_gated(monkeypatch):
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_follow_activity import _follow_alert_kind
    monkeypatch.setattr(cfg, "MEMECOIN_TG_DELAYED_CATCH_ENABLED", False)
    assert _follow_alert_kind(_ev(lag_s=320.0), armed=True, min_usd=1000) is None
    monkeypatch.setattr(cfg, "MEMECOIN_TG_DELAYED_CATCH_ENABLED", True)
    assert _follow_alert_kind(_ev(lag_s=320.0), armed=True, min_usd=1000) == "delayed"


def test_fresh_kind_still_requires_armed_and_size_floor():
    from sniper.services.memecoin_follow_activity import _follow_alert_kind
    assert _follow_alert_kind(_ev(lag_s=45.0), armed=False, min_usd=1000) is None
    assert _follow_alert_kind(_ev(lag_s=45.0, their_size_usd=200.0), armed=True,
                              min_usd=1000) is None


def test_follow_alert_context_carries_real_armed_flag():
    """2026-07-16 19:55 bug: capodon (armed, gate ✓) alerted as 'watch-only' — the follow-path
    context lacked real_armed, so build_alert_message fell back to the watch-only label."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_follow_activity import _follow_alert_context
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    ctx = _follow_alert_context(c, "uid1", armed=True, ev=_ev(network_id="4663"))
    assert ctx["real_armed"] is True
    assert ctx["authority_status"] == "armed"
    assert ctx["gate_applicable"] is True
    assert "paper_pnl" in ctx and "paper_n" in ctx and "gate_ok" in ctx


def test_follow_alert_context_does_not_cross_chain_enrich_unresolved_event():
    import sqlite3
    from types import SimpleNamespace
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_follow_activity import _follow_alert_context
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.insert_event(c, {
        "fomo_event_id": "base", "source_user_id": "other", "source_handle": "other",
        "mint": "0xabc", "ticker": "BASE", "side": "buy", "their_price": 1.0,
        "their_market_cap": 800_000.0, "their_size_usd": 1000.0, "is_first_buy": 0,
        "created_at": "2026-07-17T00:00:00Z", "received_at": "2026-07-17T00:00:01Z",
        "lag_s": 1.0, "signal_source": "global_feed", "network_id": "8453",
    })
    unresolved = SimpleNamespace(
        fomo_event_id="unresolved", source_user_id="u1", mint="0xabc", ticker="",
        their_market_cap=0.0, network_id=None, created_at="2026-07-17T00:01:00Z",
    )

    ctx = _follow_alert_context(c, "u1", armed=True, ev=unresolved)

    assert ctx["display_ticker"] is None
    assert ctx["display_market_cap"] is None
    assert ctx["prior_buyers_60m"] is None


def test_does_not_alert_below_the_size_floor():
    assert _should_follow_alert(_ev(their_size_usd=200.0), armed=True, min_usd=1000) is False


def test_does_not_alert_a_non_armed_wallet():
    """Bounded to real-money-relevant wallets — the whole point is 'a bet we could have placed'."""
    assert _should_follow_alert(_ev(their_size_usd=9999.0), armed=False, min_usd=1000) is False


def test_alerts_disabled_when_floor_is_none():
    assert _should_follow_alert(_ev(their_size_usd=9999.0), armed=True, min_usd=None) is False


def test_fast_lane_uses_its_own_wallet_spacing(monkeypatch):
    """Signals must be faster: the fast lane's per-wallet spacing is its own knob
    (MEMECOIN_FOLLOW_FAST_SLEEP_S, default 1.0s -> ~33s sweep over 30 wallets), decoupled from
    the analyzer's 2s WAF spacing. The slow lane keeps the analyzer spacing."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_follow_activity import follow_activity_pass
    from sniper.settings import memecoin as cfg
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    slept = []
    follow_activity_pass(c, None, 100.0, swaps_fn=lambda uid: [],
                         sleep_fn=slept.append, wallets=[{"source_user_id": "u1"}],
                         sleep_s=1.25)
    assert slept == [1.25]
    slept.clear()
    follow_activity_pass(c, None, 100.0, swaps_fn=lambda uid: [],
                         sleep_fn=slept.append, wallets=[{"source_user_id": "u1"}])
    assert slept == [cfg.MEMECOIN_ANALYZER_SLEEP_S]
