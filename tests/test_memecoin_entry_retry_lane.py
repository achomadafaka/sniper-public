"""Fresh-pool re-quote lane: a buy on a pool no aggregator has indexed retries until it fills.

2026-08-14: MOX and 3Knru refused 'no quote' one-shot, then quoted 200 on Jupiter AND Raydium ten
minutes later — the pool simply had not been indexed at signal time. The entry reservation is
released the instant a quote misses, so the retry re-runs the funded hook on the stored event with
no slot held across the wait; reserve_pending's UNIQUE(mint) blocks a double-open under any race.

Invariants under test: only the two transient misses retry; a real gate verdict ends it; a fill
dequeues; the window and attempt cap bound it; and the retry lane's own attempts never re-enqueue.
"""
import json
import sqlite3
from dataclasses import asdict

import pytest

from sniper.feeds.fomo_feed import CopyEvent
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _ev(fid="e1", mint="MintFresh"):
    return CopyEvent(
        source_user_id="u1", source_handle="fibs", mint=mint, ticker="", side="buy",
        their_price=1e-5, their_market_cap=15000.0, their_size_usd=1000.0, is_first_buy=True,
        fomo_event_id=fid, created_at="2026-08-14T20:00:00Z", received_at="2026-08-14T20:00:01Z",
        lag_s=1.0, signal_source="follow_activity", network_id="solana")


def _enqueue(c, ev, now="2026-08-14T20:00:00+00:00"):
    m.enqueue_entry_retry(c, ev.fomo_event_id, ev.mint, ev.network_id,
                          json.dumps(asdict(ev)), now, retry_after_s=8)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 120.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_INTERVAL_S", 8.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_MAX_ATTEMPTS", 12)


def _hook(results):
    """A hook whose successive calls return `results`; records each event it saw."""
    seen = []
    it = iter(results)
    last = results[-1]

    def hook(conn, ev):
        seen.append(ev.fomo_event_id)
        return next(it, last)
    hook.seen = seen
    return hook


def test_transient_miss_is_classified():
    assert R._is_transient_entry_miss({"status": "aborted", "reason": "no quote"})
    assert R._is_transient_entry_miss(
        {"status": "route_failed", "reason": "no planned-quantity sell route"})
    assert R._is_transient_entry_miss({
        "status": "route_failed",
        "reason": (
            "configured sell venue returned no executable quote "
            "(route=direct_v3_single_hop+v2_fallback+pons_v2+v4+v4_multihop; "
            "other venues not tested)"
        ),
    })
    assert not R._is_transient_entry_miss({"status": "aborted", "reason": "price impact 40% over cap"})
    assert not R._is_transient_entry_miss({"status": "skipped_mcap"})
    assert not R._is_transient_entry_miss({"status": "insufficient bankroll"})
    assert not R._is_transient_entry_miss({"status": "filled"})
    assert not R._is_transient_entry_miss(None)


@pytest.mark.parametrize("blocker,transient", [
    ({"kind": "reservation", "reservation_id": 1}, True),
    ({"kind": "reservation", "reservation_id": 1, "signal_source": "relay_prebuy"}, True),
    (None, False), ("reservation", False),
    ({"kind": "position", "reservation_id": 1}, False),
    ({"kind": "reservation"}, False),
    *[({"kind": "reservation", "reservation_id": value, "signal_source": "relay_prebuy"}, False)
      for value in (None, 0, -1, True, "1", 1.0)],
])
def test_temporary_reservation_retry_requires_the_exact_structured_blocker(blocker, transient, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_UNRESOLVED_S", 1.0)
    result = {"status": "rejected", "reason": R._IN_PROGRESS_REFUSAL, "duplicate_blocker": blocker}
    assert R._is_transient_entry_miss(result) is transient
    if transient:
        assert R._entry_retry_delay(result, 8.0) == 1.0
    for changed in ({"status": "filled"}, {"reason": "one position per coin: already held or reserved"}):
        assert not R._is_transient_entry_miss({**result, **changed})


@pytest.mark.parametrize("disabled", ["window", "cooldown"])
def test_temporary_reservation_retry_honors_disabled_window_and_cooldown(monkeypatch, disabled):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_UNRESOLVED_S", 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S" if disabled == "window"
                        else "MEMECOIN_ENTRY_RETRY_UNRESOLVED_S", 0.0)
    c = _conn()
    result = {"status": "rejected", "reason": R._IN_PROGRESS_REFUSAL,
              "duplicate_blocker": {"kind": "reservation", "reservation_id": 1}}
    assert not R._maybe_enqueue_retry(c, _ev(), result, "2026-08-14T20:00:00+00:00")
    assert not m.due_entry_retries(c, "2026-08-14T20:00:20+00:00", window_s=120, max_attempts=12)


@pytest.mark.parametrize("signed", [False, True])
def test_reservation_local_wait_never_calls_the_hook_or_moves_the_original_retry_deadline(monkeypatch, signed):
    from dataclasses import replace
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_UNRESOLVED_S", 1.0)
    c, ev = _conn(), replace(_ev(), network_id="4663", mint="0x" + "ab" * 20)
    pid = m.reserve_pending(c, mint=ev.mint, source_user_id="owner", strategy="alpha_b",
                            notional=20.0, reserved_at=DETECTED, network_id="4663",
                            signal_source="onchain_watch")
    if signed:
        m.set_pending_broadcast(c, pid, "0x" + "cd" * 32, 17, DETECTED)
    collision = {"status": "rejected", "reason": R._IN_PROGRESS_REFUSAL,
                 "duplicate_blocker": {"kind": "reservation", "reservation_id": pid}}
    assert R._maybe_enqueue_retry(c, ev, collision, DETECTED)
    before = dict(c.execute("SELECT * FROM memecoin_entry_retry_queue").fetchone())
    hook = _hook([collision])  # Calling the hook would start funded prereads/provider work.
    for second in (1, 2, 3):
        result = R.run_entry_retry_lane(c, hook, f"2026-08-14T20:00:0{second}+00:00",
                                        network_id="4663", include_dips=False)
        assert result == {"retried": 0, "filled": 0, "gave_up": 0} and hook.seen == []
        assert dict(c.execute("SELECT * FROM memecoin_entry_retry_queue").fetchone()) == before
        assert m.pending_row(c, pid)["status"] == "reserved"
    if not signed:
        assert m.release_pending(c, pid, "quote_over_source")
        fresh = _hook([{"status": "filled"}])
        assert R.run_entry_retry_lane(c, fresh, "2026-08-14T20:00:03+00:00",
                                      network_id="4663", include_dips=False)["filled"] == 1
        assert fresh.seen == [ev.fomo_event_id]


def test_reservation_local_wait_still_reaps_the_original_window(monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(R, "_safe_alert", lambda *_a, **_k: None)
    monkeypatch.setattr(R, "_notify_entry_projection", lambda **_k: None)
    c, ev = _conn(), replace(_ev(), network_id="4663", mint="0x" + "ab" * 20)
    pid = m.reserve_pending(c, mint=ev.mint, source_user_id="owner", strategy="alpha_b", notional=20.0,
                            reserved_at=DETECTED, network_id="4663", signal_source="onchain_watch")
    _enqueue(c, ev)
    hook = _hook([{"status": "filled"}])
    result = R.run_entry_retry_lane(c, hook, "2026-08-14T20:02:01+00:00", include_dips=False)
    assert result == {"retried": 0, "filled": 0, "gave_up": 1} and hook.seen == []
    assert not c.execute("SELECT 1 FROM memecoin_entry_retry_queue").fetchall()
    assert m.pending_row(c, pid)["status"] == "reserved"


@pytest.mark.parametrize("owner_chain,owner_mint", [
    ("8453", "0x" + "ab" * 20), ("4663", "0x" + "cd" * 20)])
def test_reservation_local_wait_is_scoped_to_the_retry_chain_and_coin(owner_chain, owner_mint):
    from dataclasses import replace
    c, ev = _conn(), replace(_ev(), network_id="4663", mint="0x" + "ab" * 20)
    pid = m.reserve_pending(c, mint=owner_mint, source_user_id="owner", strategy="alpha_b", notional=20.0,
                            reserved_at=DETECTED, network_id=owner_chain, signal_source="onchain_watch")
    _enqueue(c, ev)
    hook = _hook([{"status": "filled"}])
    result = R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:09+00:00",
                                    network_id="4663", include_dips=False)
    assert result["filled"] == 1 and hook.seen == [ev.fomo_event_id]
    assert m.pending_row(c, pid)["status"] == "reserved"


def test_reservation_local_wait_cannot_fill_the_batch_limit_and_starve_an_eligible_coin():
    from dataclasses import replace
    c = _conn()
    held_mint = "0x" + "ab" * 20
    m.reserve_pending(c, mint=held_mint, source_user_id="owner", strategy="alpha_b", notional=20.0,
                      reserved_at=DETECTED, network_id="4663", signal_source="onchain_watch")
    for index in range(16):
        _enqueue(c, replace(_ev(fid=f"held-{index}", mint=held_mint), network_id="4663"))
    eligible = replace(_ev(fid="eligible", mint="0x" + "cd" * 20), network_id="4663")
    _enqueue(c, eligible, now="2026-08-14T20:00:01+00:00")
    before = [dict(row) for row in c.execute(
        "SELECT * FROM memecoin_entry_retry_queue WHERE fomo_event_id!=? ORDER BY fomo_event_id",
        (eligible.fomo_event_id,))]
    seen = []

    def hook(_conn, event):
        seen.append(event.fomo_event_id)
        return ({"status": "rejected", "reason": R._IN_PROGRESS_REFUSAL,
                 "duplicate_blocker": {"kind": "reservation", "reservation_id": 1}}
                if event.mint == held_mint else {"status": "filled"})

    result = R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:09+00:00",
                                    network_id="4663", include_dips=False)
    assert result == {"retried": 1, "filled": 1, "gave_up": 0} and seen == [eligible.fomo_event_id]
    assert [dict(row) for row in c.execute(
        "SELECT * FROM memecoin_entry_retry_queue ORDER BY fomo_event_id")] == before


def test_reservation_local_wait_fails_closed_when_the_pending_book_is_unreadable(monkeypatch):
    c, ev = _conn(), _ev()
    _enqueue(c, ev)
    before = dict(c.execute("SELECT * FROM memecoin_entry_retry_queue").fetchone())
    reads = []

    def unreadable(_conn, **_kw):
        reads.append(1)
        raise sqlite3.OperationalError("pending book unavailable")

    monkeypatch.setattr(m, "pending_open_coin_ids", unreadable)
    hook = _hook([{"status": "filled"}])
    with pytest.raises(sqlite3.OperationalError, match="pending book unavailable"):
        R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:09+00:00", include_dips=False)
    assert hook.seen == []
    assert reads == [1] and dict(c.execute("SELECT * FROM memecoin_entry_retry_queue").fetchone()) == before


def test_a_safety_timeout_retries_instead_of_marking_the_token_unsafe():
    assert R._is_transient_entry_miss({
        "status": "unsafe",
        "reason": "safety_exception: safety check timed out",
    })
    assert not R._is_transient_entry_miss({
        "status": "unsafe",
        "reason": "route_loss_over_cap: quoted round-trip loss 20.0% > 10.0% cap",
    })
    assert R._is_transient_entry_miss({
        "status": "unsafe",
        "reason": "safety_deadline_exceeded",
    })
    assert R._is_transient_entry_miss({
        "status": "unsafe",
        "reason": "code read failed (fail-closed)",
    })


def test_a_sell_simulation_rpc_fault_retries():
    assert R._is_transient_entry_miss({
        "status": "unsafe",
        "reason": "sell simulation failed (fail-closed)",
    })


def test_an_unknown_quote_balance_retries():
    assert R._is_transient_entry_miss({
        "status": "rejected", "reason": "quote balance unavailable",
    })


def test_a_native_balance_rpc_fault_remains_in_the_retry_lane():
    c = _conn(); ev = _ev(); _enqueue(c, ev)
    hook = _hook([{
        "status": "rejected",
        "reason": "native balance read failed (rpc) — refusing fail-closed, balance unknown",
    }])

    R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:09+00:00")

    assert m.due_entry_retries(
        c, "2026-08-14T20:00:20+00:00", window_s=120, max_attempts=12,
    )


def test_a_pool_that_indexes_fills_on_a_later_attempt():
    c = _conn(); ev = _ev(); _enqueue(c, ev)
    hook = _hook([{"status": "aborted", "reason": "no quote"},
                  {"status": "filled", "notional": 40.0}])
    r1 = R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:09+00:00")   # still no route -> bump
    assert r1["retried"] == 1 and r1["filled"] == 0
    assert m.due_entry_retries(c, "2026-08-14T20:00:20+00:00", window_s=120, max_attempts=12)
    r2 = R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:20+00:00")   # now indexed -> fill
    assert r2["filled"] == 1
    assert not m.due_entry_retries(c, "2026-08-14T20:01:00+00:00", window_s=120, max_attempts=12)


def test_a_real_gate_verdict_stops_the_retry():
    c = _conn(); ev = _ev(); _enqueue(c, ev)
    hook = _hook([{"status": "skipped_mcap", "reason": "over_ceiling"}])
    R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:09+00:00")
    assert not m.due_entry_retries(c, "2026-08-14T20:00:20+00:00", window_s=120, max_attempts=12), \
        "a mcap/bankroll decision must not spin the retry"


def test_the_window_expiry_gives_up_without_a_hook_call():
    c = _conn(); ev = _ev()
    _enqueue(c, ev, now="2026-08-14T20:00:00+00:00")
    hook = _hook([pytest.fail])   # must not be called for a reaped row
    r = R.run_entry_retry_lane(c, hook, "2026-08-14T20:05:00+00:00")   # 5min > 120s window
    assert r["gave_up"] == 1 and r["retried"] == 0
    assert hook.seen == []


def test_not_due_rows_are_left_alone():
    c = _conn(); ev = _ev(); _enqueue(c, ev, now="2026-08-14T20:00:00+00:00")
    hook = _hook([{"status": "filled"}])
    r = R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:03+00:00")   # < 8s interval
    assert r["retried"] == 0 and hook.seen == []


def test_retry_does_not_start_inside_its_final_interval():
    """Without the guard, a slow hook starts at 115s and overruns the 120s window."""
    c = _conn(); ev = _ev(); _enqueue(c, ev, now="2026-08-14T20:00:00+00:00")
    hook = _hook([{"status": "filled"}])

    result = R.run_entry_retry_lane(c, hook, "2026-08-14T20:01:55+00:00")

    assert result == {"retried": 0, "filled": 0, "gave_up": 1}
    assert hook.seen == []
    assert not m.due_entry_retries(
        c, "2026-08-14T20:01:55+00:00", window_s=120, max_attempts=12)


def test_disabled_window_is_a_noop():
    from sniper.settings import memecoin as cfg
    c = _conn(); ev = _ev(); _enqueue(c, ev)
    cfg.MEMECOIN_ENTRY_RETRY_WINDOW_S = 0.0
    hook = _hook([{"status": "filled"}])
    try:
        r = R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:20+00:00")
        assert r == {"retried": 0, "filled": 0, "gave_up": 0}
    finally:
        cfg.MEMECOIN_ENTRY_RETRY_WINDOW_S = 120.0


def test_enqueue_helper_queues_a_transient_miss(monkeypatch):
    c = _conn(); ev = _ev()
    assert R._maybe_enqueue_retry(c, ev, {"status": "aborted", "reason": "no quote"},
                                  "2026-08-14T20:00:00+00:00")
    assert m.due_entry_retries(c, "2026-08-14T20:00:20+00:00", window_s=120, max_attempts=12)


def test_provider_rate_limit_retries_when_the_cooldown_ends(monkeypatch):
    """A 3-second provider cooldown must not wait for the normal 8-second retry."""
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_GT_429_COOLDOWN_S", 3.0)
    from dataclasses import replace
    c = _conn(); ev = replace(_ev(), network_id="4663")
    result = {"status": "aborted", "reason": "no quote (provider_rate_limit: v4 rate limit)"}

    assert R._maybe_enqueue_retry(c, ev, result, "2026-08-14T20:00:00+00:00")
    assert not m.due_entry_retries(
        c, "2026-08-14T20:00:02+00:00", window_s=120, max_attempts=12,
    )
    assert m.due_entry_retries(
        c, "2026-08-14T20:00:03+00:00", window_s=120, max_attempts=12,
    )


def test_an_rpc_429_waits_longer_and_gets_no_instant_requote(monkeypatch):
    """2026-09-14: PLCANARY re-quoted 11 times in 3 min into a custody-RPC 429."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_RPC_429_S", 20.0)
    res = {"status": "aborted", "reason": "no quote (provider_rpc_rate_limit: v2 could not read)"}
    assert R._entry_retry_delay(res, 6.0) == 20.0
    assert R._entry_retry_delay(res, 30.0) == 30.0
    c = _conn(); ev = _ev()
    assert R._maybe_enqueue_retry(c, ev, res, "2026-08-14T20:00:00+00:00")
    assert not m.due_entry_retries(c, "2026-08-14T20:00:19+00:00", window_s=120, max_attempts=12)
    assert m.due_entry_retries(c, "2026-08-14T20:00:20+00:00", window_s=120, max_attempts=12)
    import inspect
    src = inspect.getsource(R)
    block = src[src.index('"provider_rate_limit", "provider_rpc_transport"'):
                src.index('"provider_ladder_deadline"')]
    assert "provider_rpc_rate_limit" not in block, "no instant re-quote into an RPC 429"


def test_enqueue_helper_ignores_a_gate_decision():
    c = _conn(); ev = _ev()
    assert not R._maybe_enqueue_retry(c, ev, {"status": "skipped_mcap", "reason": "over_ceiling"},
                                      "2026-08-14T20:00:00+00:00")
    assert R.m.reap_entry_retries(c, "2026-08-14T20:00:01+00:00", window_s=120, max_attempts=12) == []


def test_enqueue_helper_ignores_a_sell():
    c = _conn()
    from dataclasses import replace
    sell = replace(_ev(), side="sell")
    assert not R._maybe_enqueue_retry(c, sell, {"status": "aborted", "reason": "no quote"},
                                      "2026-08-14T20:00:00+00:00")


def test_enqueue_helper_respects_the_disable_flag(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 0.0)
    c = _conn(); ev = _ev()
    assert not R._maybe_enqueue_retry(c, ev, {"status": "aborted", "reason": "no quote"},
                                      "2026-08-14T20:00:00+00:00")


def test_a_transient_miss_does_not_page_when_the_retry_lane_is_enabled(monkeypatch):
    """Tortuga, 2026-08-14: a fresh-pool no-route buy paged on every re-attempt. With the retry
    lane on, the refusal is handled — page only on give-up, never per attempt."""
    pages = []
    monkeypatch.setattr(R, "_safe_alert", lambda fn, *a, **k: pages.append(1))
    R._alert_entry_result("MintX", 40.0,
                          {"status": "route_failed", "reason": "no planned-quantity sell route"},
                          armed=True)
    assert pages == [], "a retryable miss must not page while the lane is enabled"


def test_a_transient_miss_still_pages_when_the_lane_is_disabled(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 0.0)
    pages = []
    monkeypatch.setattr(R, "_safe_alert", lambda fn, *a, **k: pages.append(1))
    R._alert_entry_result("MintX", 40.0, {"status": "aborted", "reason": "no quote"}, armed=True)
    assert pages == [1], "with the lane off, the one-shot refusal is the only signal"


def test_give_up_pages_exactly_once(monkeypatch):
    pages = []
    monkeypatch.setattr(R, "_safe_alert", lambda fn, *a, **k: pages.append(1))
    c = _conn(); ev = _ev()
    _enqueue(c, ev, now="2026-08-14T20:00:00+00:00")
    R.run_entry_retry_lane(c, lambda conn, e: {"status": "filled"}, "2026-08-14T20:05:00+00:00")
    assert pages == [1], "one ENTRY-GAVE-UP page when a coin never indexed"


def test_give_up_alert_does_not_claim_an_unobserved_revert(monkeypatch):
    alerts = []
    monkeypatch.setattr(R, "_safe_alert", lambda fn, *a, **k: fn())
    monkeypatch.setattr(R.alerts, "send_alert", lambda kind, message: alerts.append((kind, message)))
    c = _conn(); ev = _ev(); _enqueue(c, ev, now="2026-08-14T20:00:00+00:00")
    R.run_entry_retry_lane(c, lambda conn, e: {"status": "filled"}, "2026-08-14T20:05:00+00:00")
    assert alerts == [("ENTRY-GAVE-UP", "MintFresh… never landed in 120s — retry window exhausted")]


def test_the_suppress_flag_clears_after_the_lane(monkeypatch):
    monkeypatch.setattr(R, "_safe_alert", lambda *a, **k: None)
    c = _conn(); ev = _ev(); _enqueue(c, ev)
    hook = lambda conn, e: {"status": "route_failed", "reason": "no planned-quantity sell route"}
    hook.attempt = hook
    R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:09+00:00")
    assert getattr(R._entry_alert_suppress, "on", False) is False, "flag must not leak to live entries"


# --- Solana slippage revert (Jupiter 6001, J61gb… 2026-08-17) is retryable -------------------
# A confirmed revert is atomic (no funds moved) and release_pending already ran, so a re-quote
# at the moved price is safe; the chase guard on the re-attempt bounds an upward chase. Only the
# Solana revert returns status 'failed' with NO reason — every EVM failed carries one.

def test_confirmed_revert_is_classified_transient():
    assert R._is_transient_entry_miss({"status": "failed", "sig": "SIG"}) is True
    assert R._is_transient_entry_miss({"status": "failed", "reason": "broadcast_rejected"}) is False
    assert R._is_transient_entry_miss({"status": "failed", "reason": "reverted_min_out"}) is False


def test_confirmed_revert_enqueues_for_requote():
    c = _conn()
    assert R._maybe_enqueue_retry(c, _ev(), {"status": "failed", "sig": "SIG"},
                                  "2026-08-14T20:00:00+00:00") is True
    assert m.due_entry_retries(c, "2026-08-14T20:00:20+00:00", window_s=120, max_attempts=12)


def test_revert_page_is_suppressed_only_during_retry_attempts(monkeypatch):
    paged = []
    monkeypatch.setattr(R.alerts, "alert_failed_tx",
                        lambda mint, sig, reason=None: paged.append((mint, sig, reason)))
    res = {"status": "failed", "sig": "SIG"}
    R._alert_entry_result("MINT", 10.0, res)                      # first attempt: pages
    assert len(paged) == 1
    R._entry_alert_suppress.on = True
    try:
        R._alert_entry_result("MINT", 10.0, res)                  # retry attempt: silent
        assert len(paged) == 1
        # a NON-retryable failed still pages even mid-retry
        R._alert_entry_result("MINT", 10.0, {"status": "failed", "reason": "broadcast_rejected",
                                             "sig": "SIG2"})
        assert len(paged) == 2
    finally:
        R._entry_alert_suppress.on = False


def test_typed_evm_quote_miss_is_transient():
    """671dfb39 typed the reason ('no quote (no_route: ...)') and the exact-pair match silently
    stopped retrying EVM fresh-pool misses (KARMA 0xc9ce5584, 2026-08-17)."""
    assert R._is_transient_entry_miss(
        {"status": "aborted", "reason": "no quote (no_route: no route on v3/v2/v4)"}) is True
    assert R._is_transient_entry_miss(
        {"status": "aborted", "reason": "no quote (provider_rate_limit: gt cooldown)"}) is True
    assert R._is_transient_entry_miss(
        {"status": "aborted", "reason": "token decimals unavailable"}) is True
    assert R._is_transient_entry_miss({"status": "aborted", "reason": "sign failed"}) is False


def test_an_unavailable_sell_simulation_rpc_result_retries_but_a_chain_revert_does_not():
    assert R._is_transient_entry_miss({
        "status": "unsafe",
        "reason": "sell_sim_rpc_unknown: funded sell simulation RPC result unavailable — fail-closed",
    })
    assert not R._is_transient_entry_miss({
        "status": "unsafe",
        "reason": "sell_sim_reverted_unknown: funded sell simulation reverted with an unclassified reason",
    })


def test_a_repeated_sell_simulation_min_out_retries():
    """FOMOX 0x3f439a39 2026-09-17: refused `unsafe` on a repeated router min-out while the coin ran
    1.58x and the custody RPC was quarantined; the same funded check passed 30 minutes later."""
    assert R._is_transient_entry_miss({
        "status": "unsafe",
        "reason": "sell_sim_min_out_repeated: fresh funded sell simulation still hit router "
                  "min-out — fail-closed",
    }) is True
    assert not R._is_transient_entry_miss({
        "status": "unsafe",
        "reason": "sell_sim_reverted_unknown|TRANSFER_FROM_FAILED (token_pull_ok=false)",
    })


# --- the Solana revert re-quote lane was dead for two weeks (2026-09-08) -------------------------
#
# 2026-08-17 added `("failed", None)` to `_RETRY_TRANSIENT` so a confirmed Solana revert would
# re-quote at the moved price. 2026-08-24 made `solana_swap_executor` keep the chain's own words:
# `reason = getattr(status, "reason_code", None) or "tx_failed"`. After that the reason is NEVER
# None, so the tuple could never match and no revert ever retried. `deterministic_rejection`, the
# typed flag built to decide this properly, is produced and read by nothing.
#
# The re-arm is deliberately NARROW. Most InstructionErrors are a program refusing this route on
# its merits — a mint constraint, an unsupported token program, insufficient funds — and re-quoting
# the same venue refuses identically, so those stay one-shot. Only a MINIMUM-OUT failure means the
# price moved under a still-valid route, which is exactly what a re-quote fixes. The retry lane
# bounds the chase (window, interval, and the chase cap on the re-attempt), so a coin running away
# is refused on the new quote rather than followed up.

def test_a_slippage_revert_retries():
    """B5vZ3spp/JUST 2026-09-08: Raydium refused at 2.33% short. Atomic, no funds moved."""
    assert R._is_transient_entry_miss(
        {"status": "failed", "reason": "chain_error:exceeds desired slippage limit"}) is True
    assert R._is_transient_entry_miss(
        {"status": "failed", "reason": "chain_error:SlippageToleranceExceeded"}) is True


def test_a_non_slippage_revert_stays_one_shot():
    """A program that refused this route on its merits will refuse the re-quote identically."""
    for reason in ("chain_error:ConstraintMintTokenProgram",
                   "chain_error:AccountNotEnoughKeys",
                   "chain_error:LiquidityInsufficient",
                   "chain_error:InvalidFirstTickArrayAccount",
                   "tx_failed", "broadcast_rejected"):
        assert R._is_transient_entry_miss({"status": "failed", "reason": reason}) is False, reason


def test_an_unnamed_revert_code_stays_one_shot():
    """Under-retrying is the safe direction. Some programs log only a hex code — 0x1771 IS
    Jupiter's slippage error, but 6001 in another Anchor program means something else entirely,
    and guessing which program emitted it is how a mint-constraint refusal becomes a retry loop."""
    assert R._is_transient_entry_miss(
        {"status": "failed", "reason": "chain_error:custom program error: 0x1771"}) is False


def test_the_slippage_match_does_not_fire_on_a_refusal_that_merely_mentions_it():
    """`_is_transient_entry_miss` is consulted for every status, not just `failed`."""
    assert R._is_transient_entry_miss(
        {"status": "rejected", "reason": "slippage budget exhausted"}) is False


def test_a_route_priced_and_declined_on_cost_is_not_retried():
    """OP829 (2e3b7a5d, 2026-09-13): six identical re-quotes in 48 s at -11.8% vs a 10% budget."""
    declined = ("no quote (route_over_budget: v4 priced this route and declined it: "
                "round_trip_over_budget:-11.793397189982112)")
    assert not R._is_transient_entry_miss({"status": "aborted", "reason": declined})
    assert not R._is_transient_entry_miss({"status": "aborted", "reason": "no quote",
                                           "quote_miss_code": "route_over_budget"})
    # A blind rung still retries, even when another rung priced and declined.
    assert R._is_transient_entry_miss({"status": "aborted", "reason": (
        "no quote (provider_rpc_transport: v2 could not read: rpc_transport; "
        "pons_v2 priced this route and declined it: round_trip_over_budget:-13.4)")})


@pytest.mark.parametrize("chain,expected_delay,expected_blind", [
    ("solana", 8, 0), ("4663", 3, 1),
])
def test_provider_labels_preserve_chain_retry_policy(monkeypatch, chain, expected_delay, expected_blind):
    from dataclasses import replace
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_GT_429_COOLDOWN_S", 3.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_BLIND_WINDOW_S", 120.0)
    monkeypatch.setattr(R, "_notify_entry_projection", lambda **kw: None)
    c = _conn()
    ev = replace(_ev(), network_id=chain)
    result = {"status": "aborted", "reason": "no quote (provider_rate_limit: HTTP 429)",
              "quote_miss_code": "provider_rate_limit"}
    assert R._maybe_enqueue_retry(c, ev, result, "2026-08-14T20:00:00+00:00")
    row = dict(c.execute("SELECT * FROM memecoin_entry_retry_queue").fetchone())
    assert row["blind_misses"] == expected_blind
    assert row["next_attempt_at"] == f"2026-08-14T20:00:0{expected_delay}+00:00"
    hook = _hook([result])
    R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:08+00:00")
    row = dict(c.execute("SELECT * FROM memecoin_entry_retry_queue").fetchone())
    assert row["next_attempt_at"] == f"2026-08-14T20:00:{8 + expected_delay:02d}+00:00"
    R.run_entry_retry_lane(c, hook, "2026-08-14T20:01:01+00:00")
    assert c.execute("SELECT COUNT(*) FROM memecoin_entry_retry_queue").fetchone()[0] == expected_blind


def test_a_marginal_round_trip_miss_retries_but_a_priced_refusal_does_not():
    """SKIBIDI 0x38931deb 2026-09-17: -15.09% against the 15% budget, -13.35% on the same route
    thirteen minutes later. OP829's -11.79% against a 10% budget stays one-shot."""
    assert R._is_transient_entry_miss({
        "status": "aborted",
        "reason": "no quote (route_over_budget_marginal: v4_multihop priced this route and "
                  "declined it: round_trip_over_budget_marginal:-15.0927)",
    }) is True
    assert R._is_transient_entry_miss({
        "status": "aborted",
        "quote_miss_code": "route_over_budget",
        "reason": "no quote (route_over_budget: v4_multihop priced this route and declined it: "
                  "round_trip_over_budget:-11.79)",
    }) is False


# ---- the on-chain lane (review section 4, 2026-09-27) ----------------------------------------
# ea94beab stopped asdict() raising on the on-chain lane's plain OnchainEv by skipping it, so a
# transient miss on that lane was never re-quoted. It now queues through an explicit field list,
# keeps the lane's own 45 s freshness, and yields to a feed attempt on the same transaction.

DETECTED = "2026-08-14T20:00:00+00:00"


def _onchain(size=250.0, sig="sigX", lane=None):
    from sniper.services.memecoin_onchain_lane import OnchainEv
    ev = OnchainEv({"mint": "MintFresh", "source_user_id": "u1", "handle": "h",
                    "network_id": "solana", "source_size_usd": size, "source_price": 1e-5,
                    "signature": sig, "detected_at": DETECTED})
    if lane:
        ev.delivery_lane = lane
    return ev


@pytest.fixture
def _lane_age(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_LANE_MAX_AGE_S", 45.0)
    monkeypatch.setattr(R, "_safe_alert", lambda *a, **k: None)
    monkeypatch.setattr(R, "_notify_entry_projection", lambda **kw: None)


def test_an_onchain_transient_miss_is_queued_and_retried_as_the_same_event(_lane_age):
    c = _conn()
    ev = _onchain(size=None, lane="onchain_fallback_after_slot_lane")
    assert R._maybe_enqueue_retry(c, ev, {"status": "aborted", "reason": "no quote"},
                                  "2026-08-14T20:00:01+00:00") is True
    [row] = c.execute("SELECT fomo_event_id, event_json FROM memecoin_entry_retry_queue").fetchall()
    data = json.loads(row["event_json"])
    assert row["fomo_event_id"] == "onchain:sigX" and data["their_size_usd"] is None
    hook = _hook([{"status": "filled"}])
    seen = []
    hook.attempt = lambda conn, e: (seen.append(e), {"status": "filled"})[1]
    out = R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:10+00:00")
    assert out["filled"] == 1
    [e] = seen
    assert (e.fomo_event_id, e.signal_source, e.source_timestamp_trust, e.created_at) == (
        "onchain:sigX", "onchain_watch", "observed_detection", DETECTED)
    assert e.their_size_usd is None and e.delivery_lane == "onchain_fallback_after_slot_lane"
    assert not c.execute("SELECT 1 FROM memecoin_entry_retry_queue").fetchall()


def _graduation_event():
    ev = _onchain(lane="onchain_fallback_after_slot_lane")
    ev.slot_lane_failure_reason = "chain_error:BondingCurveComplete"
    return ev


def test_graduation_requotes_three_times_quickly_then_uses_normal_schedule(_lane_age):
    c = _conn()
    miss = {"status": "aborted", "reason": "no quote (no_route: jupiter 400 NO_ROUTES_FOUND)"}
    assert R._maybe_enqueue_retry(c, _graduation_event(), miss, DETECTED)
    assert not m.due_entry_retries(c, "2026-08-14T20:00:00.999+00:00",
                                   window_s=120, max_attempts=12)
    seen = []
    def hook(conn, ev):
        seen.append(ev)
        return miss
    for second in (1, 2, 3):
        tally = R.run_entry_retry_lane(c, hook, f"2026-08-14T20:00:0{second}+00:00")
        assert tally["retried"] == 1
    assert len(seen) == 3
    for ev in seen:
        assert ev.slot_lane_failure_reason == "chain_error:BondingCurveComplete"
        assert ev.fomo_event_id == "onchain:sigX"
        assert ev.their_price == 1e-5 and ev.created_at == DETECTED
    assert R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:04+00:00")["retried"] == 0
    assert R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:11+00:00")["retried"] == 1


@pytest.mark.parametrize("result", [
    {"status": "aborted", "reason": "no quote (provider_rate_limit: HTTP 429)"},
    {"status": "aborted", "reason": "no quote (provider_timeout: read unavailable)"},
    {"status": "route_failed", "reason": "no planned-quantity sell route"},
])
def test_graduation_does_not_accelerate_provider_faults_or_sell_proof(result, _lane_age):
    c = _conn()
    assert R._maybe_enqueue_retry(c, _graduation_event(), result, DETECTED)
    assert not m.due_entry_retries(c, "2026-08-14T20:00:01+00:00",
                                   window_s=120, max_attempts=12)
    assert m.due_entry_retries(c, "2026-08-14T20:00:08+00:00",
                              window_s=120, max_attempts=12)


@pytest.mark.parametrize("ray", [
    {"status": 429, "payload": {"msg": "Too Many Requests"}},
    {"status": 503, "payload": {"msg": "Unavailable"}},
    {"exception": TimeoutError("timed out")},
    {"status": 200, "payload": {}},
])
def test_jupiter_no_route_with_an_unproven_raydium_answer_keeps_normal_timing(ray, _lane_age):
    from sniper.execution import solana_real_seams as seams
    miss = seams.classify_entry_quote_miss([
        {"url": seams.JUP_BASES[0] + "/quote", "status": 400,
         "payload": {"errorCode": "NO_ROUTES_FOUND"}},
        {"url": seams.RAY_BASE + "/compute/swap-base-in", **ray},
    ])
    c = _conn()
    R._maybe_enqueue_retry(c, _graduation_event(), {
        "status": "aborted", "reason": f"no quote ({miss['code']}: {miss['detail']})",
    }, DETECTED)
    assert not m.due_entry_retries(c, "2026-08-14T20:00:01+00:00",
                                   window_s=120, max_attempts=12)
    assert m.due_entry_retries(c, "2026-08-14T20:00:08+00:00",
                              window_s=120, max_attempts=12)


def test_a_graduation_retry_ends_at_the_chase_gate(_lane_age):
    c = _conn()
    R._maybe_enqueue_retry(c, _graduation_event(),
                           {"status": "aborted", "reason": "no quote (no_route: jupiter 400 NO_ROUTES_FOUND)"}, DETECTED)
    hook = _hook([{"status": "skipped_chase", "reason": "exact quote +130% above source"}])
    assert R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:01+00:00")["retried"] == 1
    assert hook.seen == ["onchain:sigX"]
    assert not c.execute("SELECT 1 FROM memecoin_entry_retry_queue").fetchall()


def test_a_replacement_route_can_fill_before_the_previous_eight_second_due_time(_lane_age):
    c = _conn()
    miss = {"status": "aborted", "reason": "no quote (no_route: jupiter 400 NO_ROUTES_FOUND; raydium ROUTE_NOT_FOUND)"}
    R._maybe_enqueue_retry(c, _graduation_event(), miss, DETECTED)
    hook = _hook([miss, {"status": "filled"}])
    assert R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:01+00:00")["filled"] == 0
    assert R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:02+00:00")["filled"] == 1
    assert hook.seen == ["onchain:sigX", "onchain:sigX"]
    assert not c.execute("SELECT 1 FROM memecoin_entry_retry_queue").fetchall()


def test_an_onchain_retry_never_runs_past_the_lanes_own_freshness(_lane_age):
    c = _conn()
    R._maybe_enqueue_retry(c, _onchain(), {"status": "aborted", "reason": "no quote"},
                           "2026-08-14T20:00:30+00:00")
    hook = _hook([{"status": "filled"}])
    out = R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:46+00:00")   # 46 s after detection
    assert hook.seen == [] and out["gave_up"] == 1
    assert not c.execute("SELECT 1 FROM memecoin_entry_retry_queue").fetchall()


def test_an_onchain_retry_yields_to_a_feed_attempt_on_the_same_transaction(_lane_age):
    c = _conn()
    R._maybe_enqueue_retry(c, _onchain(), {"status": "aborted", "reason": "no quote"},
                           "2026-08-14T20:00:01+00:00")
    c.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source,"
              " decision_started_at) VALUES ('a1', 'swap:raybot:sigX:u1:MintFresh',"
              " 'follow_activity', '2026-08-14T20:00:02+00:00')")
    hook = _hook([{"status": "filled"}])
    out = R.run_entry_retry_lane(c, hook, "2026-08-14T20:00:10+00:00")
    assert hook.seen == [] and out["gave_up"] == 0
    assert not c.execute("SELECT 1 FROM memecoin_entry_retry_queue").fetchall()


def test_an_onchain_policy_refusal_and_a_slot_lane_event_are_not_queued(_lane_age):
    from types import SimpleNamespace
    c = _conn()
    assert R._maybe_enqueue_retry(c, _onchain(), {"status": "skipped_mcap"},
                                  "2026-08-14T20:00:01+00:00") is False
    slot = SimpleNamespace(side="buy", signal_source="slot_lane", fomo_event_id="slotlane:s",
                           mint="M", network_id="solana")
    assert R._maybe_enqueue_retry(c, slot, {"status": "aborted", "reason": "no quote"},
                                  "2026-08-14T20:00:01+00:00") is False
    assert not c.execute("SELECT 1 FROM memecoin_entry_retry_queue").fetchall()


def test_a_marginal_round_trip_miss_is_re_asked_after_one_second_three_times(monkeypatch):
    """73849 (2026-10-14, 56 Flap): the fast lane priced the round trip at 15.80% against the budget
    and aborted 'route_over_budget_marginal'; the generic 6 s retry interval made the fill land 21
    blocks late. A marginal miss is one read of a moving cost: re-ask it after 1 s, three times."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_MARGINAL_S", 1.0, raising=False)
    res = {"status": "aborted", "reason": "no quote (route_over_budget_marginal: flap_curve priced this "
           "route and declined it: round_trip_over_budget_marginal:15.799404861077516)"}
    assert [R._entry_retry_delay(res, 6.0, attempts=a, network_id="56") for a in range(4)] == [
        1.0, 1.0, 1.0, 6.0]
    # a priced miss beyond the margin and an RPC 429 keep their own delays
    assert R._entry_retry_delay({"status": "aborted", "reason": "no quote (route_over_budget: x)"},
                                6.0, network_id="56") == 6.0
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_MARGINAL_S", 0.0, raising=False)   # rollback
    assert R._entry_retry_delay(res, 6.0, attempts=0, network_id="56") == 6.0
