"""Per-follow activity coverage poll + signal-source attribution/A-B."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_follow_activity import follow_activity_pass

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NOW = "2026-07-06T12:00:00Z"


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); m.add_wallet(c, "u1", "alice", source="follow"); return c


def _swap(mint, usd, tok, ts, sid, network_id=1399811149):
    return {"id": sid, "inTokenAddress": USDC, "outTokenAddress": mint,
            "humanUsdAmountIn": str(usd), "humanUsdAmountOut": str(usd),
            "inHumanAmount": str(usd), "outHumanAmount": str(tok), "createdAt": ts,
            "isOffPlatform": "False", "networkId": network_id}


def _sell(mint, usd, tok, ts, sid, network_id=1399811149):
    # memecoin in, cash out -> classify_swap returns ("sell", mint, usd_out, qty)
    return {"id": sid, "inTokenAddress": mint, "outTokenAddress": USDC,
            "humanUsdAmountIn": str(usd), "humanUsdAmountOut": str(usd),
            "inHumanAmount": str(tok), "outHumanAmount": str(usd), "createdAt": ts,
            "isOffPlatform": "False", "networkId": network_id}


def _run(c, swaps, **kw):
    # now_fn (host) stamps positions; real_now_fn (real-UTC) drives the staleness age. Both
    # pinned to NOW here so age is deterministic vs the swaps' createdAt.
    return follow_activity_pass(
        c, None, 100.0, now_fn=lambda: NOW, real_now_fn=lambda: NOW,
        fill_fn=lambda *a, **k: (0.005, "jupiter"),
        swaps_fn=lambda uid: swaps, sleep_fn=lambda s: None, **kw)


def test_copies_fresh_feed_missed_large_buy_as_follow_activity():
    c = _conn()
    # buy $3000 at 11:55 (5m old < 15m max_age); price 0.005 == our fill -> no chase skip
    r = _run(c, [_swap("MINT", 3000, 600000, "2026-07-06T11:55:00Z", "s1")])
    assert r["copied"] == 1
    pos = m.get_open_position(c, "u1", "MINT")
    assert pos is not None and pos["signal_source"] == "follow_activity"
    row = c.execute("SELECT delivery_lane FROM memecoin_events WHERE fomo_event_id='act-s1'").fetchone()
    assert row["delivery_lane"] == "rest"


def test_real_leg_gated_off_still_copies_paper_but_skips_real_hook(monkeypatch):
    # MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED=off -> the delayed-catch REAL leg is suppressed
    # (operator safety gate while the fast-lane latency-cliff edge is unproven), but paper copy,
    # alerts, and sell coverage are untouched. Both lanes route through follow_activity_pass, so
    # the gate lives here and covers the fast worker too.
    monkeypatch.setattr(
        "sniper.settings.memecoin.MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", False, raising=False)
    c = _conn()
    calls = []
    r = _run(c, [_swap("MINT", 3000, 600000, "2026-07-06T11:55:00Z", "s1")],
             real_hook=lambda _conn, ev: calls.append(ev) or {"status": "filled"})
    assert r["copied"] == 1                       # paper copy still happens
    assert calls == []                            # real hook NEVER invoked
    assert m.get_open_position(c, "u1", "MINT") is not None


def test_real_leg_enabled_by_default_calls_real_hook(monkeypatch):
    monkeypatch.setattr(
        "sniper.settings.memecoin.MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", True, raising=False)
    c = _conn()
    calls = []
    _run(c, [_swap("MINT", 3000, 600000, "2026-07-06T11:55:00Z", "s1")],
         real_hook=lambda _conn, ev: calls.append(ev) or {"status": "filled"})
    assert len(calls) == 1                         # real leg fires when enabled


def test_follow_funded_hook_runs_before_paper_fanout(monkeypatch):
    monkeypatch.setattr(
        "sniper.settings.memecoin.MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", True, raising=False)
    calls = []
    monkeypatch.setattr(
        "sniper.services.memecoin_copy_trader.copy_event_to_arms",
        lambda *_args, **_kwargs: calls.append("paper") or {"control": "ignored"},
    )

    _run(
        _conn(), [_swap("MINT", 3000, 600000, "2026-07-06T11:55:00Z", "s1")],
        real_hook=lambda *_args: calls.append("funded") or {"status": "filled"},
    )

    assert calls == ["funded", "paper"]


def test_follow_paper_retry_does_not_repeat_the_funded_hook(monkeypatch):
    monkeypatch.setattr(
        "sniper.settings.memecoin.MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", True, raising=False)
    c = _conn()
    calls = []
    paper_attempts = [0]

    def paper(*_args, **_kwargs):
        calls.append("paper")
        paper_attempts[0] += 1
        if paper_attempts[0] == 1:
            raise RuntimeError("injected paper failure")
        return {"control": "ignored"}

    monkeypatch.setattr(
        "sniper.services.memecoin_copy_trader.copy_event_to_arms", paper)
    swaps = [_swap("MINT", 3000, 600000, "2026-07-06T11:55:00Z", "s1")]
    real_hook = lambda *_args: calls.append("funded") or {"status": "filled"}

    _run(c, swaps, real_hook=real_hook)
    _run(c, swaps, real_hook=real_hook)

    assert calls == ["funded", "paper", "paper"]


def test_holder_reconciliation_runs_after_funded_buy_hook(monkeypatch):
    monkeypatch.setattr(
        "sniper.settings.memecoin.MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", True, raising=False)
    calls = []
    monkeypatch.setattr(
        "sniper.services.memecoin_holder_evidence.reconcile_follow_poll",
        lambda *_args, **_kwargs: calls.append("evidence"),
    )
    c = _conn()
    _run(
        c, [_swap("MINT", 3000, 600000, "2026-07-06T11:55:00Z", "s1")],
        real_hook=lambda _conn, _ev: calls.append("funded") or {"status": "filled"},
    )
    assert calls == ["funded", "evidence"]


def test_network_waits_never_hold_follow_sweep_snapshot(monkeypatch):
    c = _conn()
    c.execute("CREATE TABLE sweep_writes(uid TEXT)")
    m.add_wallet(c, "u2", "bob", source="follow")
    c.commit()
    monkeypatch.setattr(
        "sniper.services.memecoin_holder_evidence.reconcile_follow_poll",
        lambda conn, uid, *_args, **_kwargs: conn.execute(
            "INSERT INTO sweep_writes(uid) VALUES (?)", (uid,)
        ),
    )
    observed = []
    swaps = {
        "u1": [_swap("MINT1", 3000, 600000, "2026-07-06T11:55:00Z", "s1")],
        "u2": [_swap("MINT2", 3000, 600000, "2026-07-06T11:55:00Z", "s2")],
    }

    follow_activity_pass(
        c,
        None,
        100.0,
        now_fn=lambda: NOW,
        real_now_fn=lambda: NOW,
        swaps_fn=lambda uid: observed.append(("network", c.in_transaction)) or swaps[uid],
        sleep_fn=lambda _seconds: observed.append(("sleep", c.in_transaction)),
        fill_fn=lambda *_args, **_kwargs: (0.005, "jupiter"),
    )

    assert observed == [
        ("network", False),
        ("sleep", False),
        ("network", False),
        ("sleep", False),
    ]


def test_fresh_follow_alert_runs_after_real_attempt_and_shows_exact_refusal(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MIN_USD", 1000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MAX_LAG_S", 60.0)
    c = _conn()
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    m.insert_event(c, {
        "fomo_event_id": "prior-jimothy", "source_user_id": "u2",
        "source_handle": "other", "mint": "MINT", "ticker": "Jimothy", "side": "buy",
        "their_price": 0.005, "their_market_cap": 912_861.0,
        "their_size_usd": 2000.0, "is_first_buy": 0,
        "created_at": "2026-07-06T11:58:00Z", "received_at": NOW, "lag_s": 1.0,
        "signal_source": "global_feed", "network_id": "solana",
        "received_at_normalized": "2026-07-06T11:58:01Z",
        "clock_domain": "observed_real_utc",
    })
    order = []
    messages = []

    def real_hook(conn, ev):
        order.append("real")
        m.start_entry_attempt(
            conn,
            attempt_id="attempt-follow-refusal",
            origin_event_id=ev.fomo_event_id,
            signal_source="follow_activity",
            network_id=ev.network_id,
            mint=ev.mint,
            decision_started_at="2026-07-06T12:00:00Z",
        )
        m.finish_entry_attempt(
            conn,
            "attempt-follow-refusal",
            "refused",
            "coin ran 5.2x above first-seen price (cap 2.0x)",
            "2026-07-06T12:00:00.029Z",
            "{}",
        )
        return {
            "status": "skipped_chase",
            "reason": "coin ran 5.2x above first-seen price (cap 2.0x)",
        }

    def alert_fn(message):
        order.append("alert")
        messages.append(message)
        return True, 1.0

    _run(
        c,
        [_swap("MINT", 1300, 260000, "2026-07-06T11:59:21.506Z", "s1")],
        real_hook=real_hook,
        alert_fn=alert_fn,
    )

    assert order == ["real", "alert"]
    assert len(messages) == 1
    message = messages[0]
    assert message.startswith("🟠 Jimothy — alice bought")
    assert "mcap ~$912.9k | chain solana" in message
    assert "entry: REFUSED · skipped_chase · pre-reservation" in message
    assert "reason: coin ran 5.2x above first-seen price (cap 2.0x)" in message
    assert "attempt: attempt-follow-refusal" in message


def test_follow_alert_says_not_attempted_when_real_lane_is_disabled(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MIN_USD", 1000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MAX_LAG_S", 60.0)
    c = _conn()
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    messages = []

    _run(
        c,
        [_swap("MINT", 1300, 260000, "2026-07-06T11:59:30Z", "s1")],
        real_hook=lambda *_args: (_ for _ in ()).throw(AssertionError("must not run")),
        alert_fn=lambda message: messages.append(message) or (True, 1.0),
    )

    assert len(messages) == 1
    assert "entry: NOT ATTEMPTED" in messages[0]
    assert "reason: follow real lane disabled" in messages[0]


def test_production_follow_alert_uses_nonblocking_dispatcher(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MIN_USD", 1000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MAX_LAG_S", 60.0)
    c = _conn()
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    enqueued = []
    monkeypatch.setattr(
        "sniper.services.memecoin_telegram.enqueue_telegram",
        lambda message, **meta: enqueued.append((message, meta)) or True,
        raising=False,
    )

    def real_hook(conn, ev):
        m.start_entry_attempt(
            conn,
            attempt_id="attempt-enqueued",
            origin_event_id=ev.fomo_event_id,
            decision_started_at=NOW,
        )
        m.finish_entry_attempt(
            conn, "attempt-enqueued", "refused", "not approved", NOW, "{}",
        )
        return {"status": "not_approved"}

    _run(
        c,
        [_swap("MINT", 1300, 260000, "2026-07-06T11:59:30Z", "s1")],
        real_hook=real_hook,
    )

    assert len(enqueued) == 1
    assert "attempt: attempt-enqueued" in enqueued[0][0]
    assert enqueued[0][1]["kind"] == "follow_activity:fresh"


def test_follow_hook_exception_still_produces_explicit_failed_alert(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MIN_USD", 1000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MAX_LAG_S", 60.0)
    c = _conn()
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    messages = []

    _run(
        c,
        [_swap("MINT", 1300, 260000, "2026-07-06T11:59:30Z", "s1")],
        real_hook=lambda *_args: (_ for _ in ()).throw(RuntimeError("injected hook failure")),
        alert_fn=lambda message: messages.append(message) or (True, 1.0),
    )

    assert len(messages) == 1
    assert messages[0].startswith("🟠")
    assert "entry: FAILED · hook_exception" in messages[0]
    assert "reason: injected hook failure" in messages[0]


def test_follow_activity_preserves_the_swap_network():
    c = _conn()
    r = _run(c, [_swap("0xRobin", 3000, 600000, "2026-07-06T11:55:00Z", "s1",
                       network_id=4663)])

    assert r["copied"] == 1
    event = c.execute(
        "SELECT network_id, received_at_normalized, clock_domain "
        "FROM memecoin_events WHERE fomo_event_id='act-s1'"
    ).fetchone()
    networks = {row[0] for row in c.execute(
        "SELECT DISTINCT network_id FROM memecoin_positions WHERE origin_event_id='act-s1'"
    )}
    assert event["network_id"] == "4663"
    assert event["received_at_normalized"] == NOW
    assert event["clock_domain"] == "observed_real_utc"
    assert networks == {"4663"}


def test_each_wallet_response_gets_its_own_observed_receipt_timestamp():
    c = _conn()
    m.add_wallet(c, "u2", "bob", source="follow")
    c.execute("UPDATE memecoin_wallets SET added_at='2026-07-06T00:00:00Z' "
              "WHERE source_user_id='u1'")
    c.execute("UPDATE memecoin_wallets SET added_at='2026-07-06T00:00:01Z' "
              "WHERE source_user_id='u2'")
    c.commit()
    clock_values = iter([
        "2026-07-06T12:00:00Z",  # sweep time for stable age comparisons
        "2026-07-06T12:00:01Z",  # u1 response received
        "2026-07-06T12:00:04Z",  # u2 response received after request/sleep latency
    ])
    swaps = {
        "u1": [_swap("MINT1", 3000, 600000, "2026-07-06T11:55:00Z", "s1")],
        "u2": [_swap("MINT2", 3000, 600000, "2026-07-06T11:55:00Z", "s2")],
    }
    handed_off = {}

    def real_hook(_conn, ev):
        handed_off[ev.source_user_id] = ev.received_at_normalized
        return {"status": "disarmed"}

    follow_activity_pass(
        c,
        None,
        100.0,
        now_fn=lambda: NOW,
        real_now_fn=lambda: next(clock_values),
        fill_fn=lambda *a, **k: (0.005, "jupiter"),
        swaps_fn=lambda uid: swaps[uid],
        sleep_fn=lambda _seconds: None,
        real_hook=real_hook,
    )

    observed = {
        row["source_user_id"]: row["received_at_normalized"]
        for row in c.execute(
            "SELECT source_user_id, received_at_normalized FROM memecoin_events "
            "WHERE fomo_event_id IN ('act-s1','act-s2')"
        )
    }
    assert observed == {
        "u1": "2026-07-06T12:00:01Z",
        "u2": "2026-07-06T12:00:04Z",
    }
    assert handed_off == observed


def test_skips_buy_already_captured_by_global_feed():
    c = _conn()
    # a global-feed buy for (u1, MINT) at 11:55:30 already exists (within dedup window)
    m.insert_event(c, {"fomo_event_id": "feed1", "source_user_id": "u1", "source_handle": "alice",
                       "mint": "MINT", "ticker": "T", "side": "buy", "their_price": 0.005,
                       "their_market_cap": 0, "their_size_usd": 3000, "is_first_buy": 0,
                       "created_at": "2026-07-06T11:55:30Z", "received_at": NOW, "lag_s": 0.3,
                       "signal_source": "global_feed"})
    r = _run(c, [_swap("MINT", 3000, 600000, "2026-07-06T11:55:00Z", "s1")])
    assert r["copied"] == 0 and r["skipped_seen"] == 1


def test_cross_source_dedup_preserves_same_address_on_different_evm_chains():
    c = _conn()
    m.insert_event(c, {
        "fomo_event_id": "base-feed", "source_user_id": "u1", "source_handle": "alice",
        "mint": "0xabc", "ticker": "T", "side": "buy", "their_price": 0.005,
        "their_market_cap": 0, "their_size_usd": 3000, "is_first_buy": 0,
        "created_at": "2026-07-06T11:55:30Z", "received_at": NOW, "lag_s": 0.3,
        "signal_source": "global_feed", "network_id": "8453",
    })

    r = _run(c, [_swap("0xabc", 3000, 600000, "2026-07-06T11:55:00Z", "rh-s1",
                       network_id=4663)])

    assert r["copied"] == 1 and r["skipped_seen"] == 0
    assert c.execute(
        "SELECT network_id FROM memecoin_events WHERE fomo_event_id='act-rh-s1'"
    ).fetchone()[0] == "4663"


def test_cross_source_dedup_still_collapses_same_chain_duplicate():
    c = _conn()
    m.insert_event(c, {
        "fomo_event_id": "base-feed", "source_user_id": "u1", "source_handle": "alice",
        "mint": "0xabc", "ticker": "T", "side": "buy", "their_price": 0.005,
        "their_market_cap": 0, "their_size_usd": 3000, "is_first_buy": 0,
        "created_at": "2026-07-06T11:55:30Z", "received_at": NOW, "lag_s": 0.3,
        "signal_source": "global_feed", "network_id": "8453",
    })

    r = _run(c, [_swap("0xabc", 3000, 600000, "2026-07-06T11:55:00Z", "base-s1",
                       network_id=8453)])

    assert r["copied"] == 0 and r["skipped_seen"] == 1


def test_skips_stale_buy_older_than_max_age():
    c = _conn()
    # buy at 11:00 (60m old > 15m max_age) must NOT be copied (avoid stale history)
    r = _run(c, [_swap("MINT", 3000, 600000, "2026-07-06T11:00:00Z", "s1")])
    assert r["copied"] == 0 and r["skipped_stale"] == 1


def test_age_uses_real_utc_not_host_clock(monkeypatch):
    """Regression: staleness age must diff fomo's real-UTC createdAt against REAL-UTC now,
    not the host wall clock (which runs ~+7h fast on this box). Simulate the skew: the host
    now_fn is +7h ahead; the real clock is 5m after the buy. The fresh buy must be COPIED,
    not silently dropped as stale (the bug that left the follow_activity arm dead since launch)."""
    import sniper.services.memecoin_follow_activity as fa
    c = _conn()
    # real-UTC clock the age math must use (5m after the buy -> fresh)
    monkeypatch.setattr(fa, "real_utc_now_iso", lambda *a, **k: "2026-07-06T12:00:00Z", raising=False)
    r = follow_activity_pass(
        c, None, 100.0,
        now_fn=lambda: "2026-07-06T19:00:00Z",   # host clock, +7h fast (stamps only)
        fill_fn=lambda *a, **k: (0.005, "jupiter"),
        swaps_fn=lambda uid: [_swap("MINT", 3000, 600000, "2026-07-06T11:55:00Z", "s1")],
        sleep_fn=lambda s: None)
    assert r["skipped_stale"] == 0 and r["copied"] == 1


def test_follow_lag_uses_response_receipt_time():
    clock = iter(["2026-07-06T12:00:00Z", "2026-07-06T12:00:30Z"])
    seen = []
    follow_activity_pass(
        _conn(), None, 100.0, now_fn=lambda: "2026-07-06T12:00:30Z",
        real_now_fn=lambda: next(clock),
        fill_fn=lambda *a, **k: (0.005, "jupiter"),
        swaps_fn=lambda _uid: [
            _swap("MINT", 3000, 600000, "2026-07-06T11:59:00Z", "receipt-lag")
        ],
        sleep_fn=lambda _seconds: None,
        real_hook=lambda _conn, ev: seen.append(ev.lag_s) or {"status": "filled"},
    )
    assert seen == [90.0]


def test_skips_small_buy():
    c = _conn()
    r = _run(c, [_swap("MINT", 500, 100000, "2026-07-06T11:55:00Z", "s1")])
    assert r["copied"] == 0


def test_default_follow_minimum_sends_one_hundred_dollar_buy_to_funded_hook(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW_ACTIVITY_MIN_USD", 100.0, raising=False)
    calls = []

    _run(
        _conn(),
        [_swap("MINT", 100, 20000, "2026-07-06T11:55:00Z", "small-100")],
        real_hook=lambda _conn, ev: calls.append(ev) or {"status": "filled"},
    )

    assert len(calls) == 1


def test_default_follow_minimum_rejects_buy_below_one_hundred_dollars(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW_ACTIVITY_MIN_USD", 100.0)
    calls = []

    _run(
        _conn(),
        [_swap("MINT", 99.99, 19998, "2026-07-06T11:55:00Z", "small-99")],
        real_hook=lambda _conn, ev: calls.append(ev) or {"status": "filled"},
    )

    assert calls == []


def test_explicit_follow_minimum_overrides_the_setting(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW_ACTIVITY_MIN_USD", 100.0)
    calls = []

    _run(
        _conn(),
        [_swap("MINT", 100, 20000, "2026-07-06T11:55:00Z", "small-override")],
        min_usd=500.0,
        real_hook=lambda _conn, ev: calls.append(ev) or {"status": "filled"},
    )

    assert calls == []


def test_dedup_across_polls_by_swap_id():
    c = _conn()
    swaps = [_swap("MINT", 3000, 600000, "2026-07-06T11:55:00Z", "s1")]
    _run(c, swaps)
    r2 = _run(c, swaps)                 # same swap id re-seen next poll
    assert r2["copied"] == 0           # insert_event dedups by act-<swap id>


def test_measures_missed_sell_coverage_once_and_stable():
    # P1 (deep review) + methodology fix: mirror exits depend on the global feed catching the
    # trader's SELL. This pass MEASURES (never acts on) how many large sells the feed missed.
    # Each sell is classified EXACTLY ONCE, only in a stable window [dedup_window, max_age] old
    # (so the feed had its fair chance and 'missed' can't later flip to 'seen'), and persisted so
    # a swap lingering in the newest-<=100 window isn't re-counted across passes.
    c = _conn()
    # feed delivered the SEEN sell (within the dedup window of its createdAt)
    m.insert_event(c, {"fomo_event_id": "sfeed", "source_user_id": "u1", "source_handle": "alice",
                       "mint": "SEEN", "ticker": "T", "side": "sell", "their_price": 0.01,
                       "their_market_cap": 0, "their_size_usd": 3000, "is_first_buy": 0,
                       "created_at": "2026-07-06T11:50:30Z", "received_at": NOW, "lag_s": 0.3,
                       "signal_source": "global_feed"})
    swaps = [_sell("SEEN", 3000, 300000, "2026-07-06T11:50:00Z", "s1"),    # 10m old (in window); feed saw -> seen
             _sell("MISSED", 3000, 300000, "2026-07-06T11:50:00Z", "s2"),  # feed missed -> missed
             _sell("SMALL", 200, 20000, "2026-07-06T11:50:00Z", "s3"),     # below min_usd -> ignored
             _sell("FRESH", 3000, 300000, "2026-07-06T11:59:30Z", "s4"),   # 30s old < dedup window -> not yet
             _sell("OLD", 3000, 300000, "2026-07-06T11:00:00Z", "s5")]     # 60m > max_age -> skipped
    r = _run(c, swaps)
    assert r["sells_seen"] == 1 and r["sells_missed"] == 1 and r["copied"] == 0
    r2 = _run(c, swaps)                        # same swaps re-seen next pass
    assert r2["sells_seen"] == 0 and r2["sells_missed"] == 0    # classified ONCE, no double-count
    assert m.sell_coverage_summary(c) == {"total": 2, "seen": 1, "missed": 1}


def test_sell_coverage_does_not_match_same_address_on_another_chain():
    c = _conn()
    m.insert_event(c, {
        "fomo_event_id": "base-sell", "source_user_id": "u1", "source_handle": "alice",
        "mint": "0xabc", "ticker": "T", "side": "sell", "their_price": 0.01,
        "their_market_cap": 0, "their_size_usd": 3000, "is_first_buy": 0,
        "created_at": "2026-07-06T11:50:30Z", "received_at": NOW, "lag_s": 0.3,
        "signal_source": "global_feed", "network_id": "8453",
    })

    result = _run(c, [
        _sell("0xabc", 3000, 300000, "2026-07-06T11:50:00Z", "rh-sell", network_id=4663)
    ])

    assert result["sells_seen"] == 0
    assert result["sells_missed"] == 1


def test_existing_follow_response_invalidates_holder_evidence_without_another_fetch():
    from sniper.services.memecoin_holder_evidence import reconcile_feed_observations
    from types import SimpleNamespace

    c = _conn()
    c.execute("UPDATE memecoin_wallets SET real_approved=1, real_chains='solana' "
              "WHERE source_user_id='u1'")   # arming is per chain (2026-07-30)
    c.commit()
    reconcile_feed_observations(c, [SimpleNamespace(
        upstream_event_id="positive", raw_type="large_buy", normalized_side="buy",
        source_user_id="u1", source_handle="alice", network_id="solana", mint="MINT",
        observed_at="2026-07-06T11:49:00Z",
        raw_payload={"body": {"currentSizeUsd": 100.0}},
    )], {"u1"})

    _run(c, [_sell("MINT", 3000, 300000, "2026-07-06T11:50:00Z", "s1")])

    row = c.execute("SELECT * FROM memecoin_holder_evidence").fetchone()
    assert row["evidence_state"] == "unknown"
    assert row["invalidation_reason"] == "follow_sell_missing_remaining"
    assert row["successful_poll_at"] == NOW


def test_failed_follow_poll_removes_positive_holder_confidence():
    from sniper.services.memecoin_holder_evidence import reconcile_feed_observations
    from types import SimpleNamespace

    c = _conn()
    c.execute("UPDATE memecoin_wallets SET real_approved=1, real_chains='solana' "
              "WHERE source_user_id='u1'")   # arming is per chain (2026-07-30)
    c.commit()
    reconcile_feed_observations(c, [SimpleNamespace(
        upstream_event_id="positive", raw_type="large_buy", normalized_side="buy",
        source_user_id="u1", source_handle="alice", network_id="solana", mint="MINT",
        observed_at="2026-07-06T11:59:00Z",
        raw_payload={"body": {"currentSizeUsd": 100.0}},
    )], {"u1"})

    follow_activity_pass(
        c, None, 100.0, now_fn=lambda: NOW, real_now_fn=lambda: NOW,
        swaps_fn=lambda _uid: (_ for _ in ()).throw(RuntimeError("poll failed")),
        sleep_fn=lambda _seconds: None,
    )

    row = c.execute("SELECT * FROM memecoin_holder_evidence").fetchone()
    assert row["evidence_state"] == "unknown"
    assert row["invalidation_reason"] == "follow_poll_failed"


def test_follow_activity_applies_conviction_sizing(monkeypatch):
    # P2 (deep review): conviction sizing was applied on the global-feed path but NOT the
    # follow_activity path -> if enabled, the signal-source A/B would confound bet size with
    # source. Both paths now scale identically.
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_CONVICTION_SIZING", True, raising=False)
    c = _conn()
    # their_size 180k, pivot 20k -> sqrt(9)=3.0 -> clamped at max mult 3.0 -> 100 base * 3 = 300
    r = _run(c, [_swap("MINT", 180000, 3.6e7, "2026-07-06T11:55:00Z", "s1")])
    assert r["copied"] == 1
    pos = m.get_open_position(c, "u1", "MINT")
    assert abs(pos["entry_notional"] - 300.0) < 1e-6


def test_signal_source_summary_partitions_by_source():
    c = _conn()
    _run(c, [_swap("MINT", 3000, 600000, "2026-07-06T11:55:00Z", "s1")])   # follow_activity
    # a global_feed position via open_position default
    m.open_position(c, source_user_id="u9", source_handle="bob", mint="M2", ticker="T",
                    entry_price=1.0, entry_notional=100.0, entry_fill_source="j",
                    entry_lag_s=0.1, their_size_usd=0.0, opened_at=NOW)  # default global_feed
    summ = {s["signal_source"]: s for s in m.signal_source_summary(c)}
    assert set(summ) == {"follow_activity", "global_feed"}
    assert summ["follow_activity"]["opened"] == 3   # follow_activity now fans to all 3 arms
    assert summ["global_feed"]["opened"] == 1


def test_follow_activity_event_fills_ticker_and_mcap_from_our_own_tables():
    """REGRESSION (2026-07-27): the activity endpoint returns no symbol and no mcap, and this lane
    hardcoded ticker='' / mcap=0.0 into every event it wrote. Those blanks are what resolve_mint_*()
    later reads, so a coin seen ONLY here could never be named or priced by anything downstream and
    the alert rendered "? — mcap ~?". Borrow from a feed event that already priced the same coin."""
    c = _conn()
    m.insert_event(c, {                                   # the global feed already priced BONK
        "source_user_id": "u-other", "source_handle": "bob", "mint": "BONKMINT",
        "ticker": "BONK", "side": "buy", "their_price": 0.004, "their_market_cap": 750_000.0,
        "their_size_usd": 500.0, "is_first_buy": False, "fomo_event_id": "feed-1",
        "created_at": "2026-07-06T11:00:00Z", "received_at": "2026-07-06T11:00:00Z",
        "lag_s": 1.0, "signal_source": "global_feed", "network_id": "solana",
    })
    r = _run(c, [_swap("BONKMINT", 3000, 600000, "2026-07-06T11:55:00Z", "s9")])
    assert r["copied"] == 1
    ev = c.execute("SELECT ticker, their_market_cap FROM memecoin_events "
                   "WHERE fomo_event_id='act-s9'").fetchone()
    assert ev["ticker"] == "BONK"
    assert ev["their_market_cap"] == 750_000.0


def test_follow_activity_event_stays_blank_when_nothing_ever_priced_the_coin():
    """The genuinely-unknown case must stay unknown — never invent a name or a market cap. This is
    the coin class that exposed the bug (seen only via follow_activity, no feed reading to borrow);
    the funded classifier resolves it live downstream, and the alert reads THAT."""
    c = _conn()
    r = _run(c, [_swap("NEWMINT", 3000, 600000, "2026-07-06T11:55:00Z", "s10")])
    assert r["copied"] == 1
    ev = c.execute("SELECT ticker, their_market_cap FROM memecoin_events "
                   "WHERE fomo_event_id='act-s10'").fetchone()
    assert ev["ticker"] == "" and ev["their_market_cap"] == 0.0


def test_a_j7_buy_refused_by_fast_lane_only_never_pages(monkeypatch):
    """Operator order 2026-10-06: under fast-lane-only a J7 buy can never be funded on Solana, so
    its card would page native_fast_lane_unavailable, which says nothing about the coin."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MIN_USD", 1000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MAX_LAG_S", 60.0)
    c = _conn()
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    messages = []
    _run(
        c,
        [_swap("MINT", 1300, 260000, "2026-07-06T11:59:30Z", "s1")],
        real_hook=lambda *_a: {"status": "rejected", "reason": "native_fast_lane_unavailable"},
        alert_fn=lambda message: messages.append(message) or (True, 1.0),
    )
    assert messages == []
