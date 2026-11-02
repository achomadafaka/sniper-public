"""Operator digest (2026-07-17): the system reports its own anomalies to Telegram.

"if I'm not reading the logs we wouldn't even know" — one compact message per period built ONLY
from the DB (no live APIs), with real-UTC window anchors computed in Python (the host clock is
+7h skewed; sqlite datetime('now') string-compares are forbidden here). Every warning line must
be actionable; the first line carries a severity emoji (green/yellow/red).
"""
import sqlite3
from datetime import datetime, timezone

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_digest as dg
from sniper.settings import memecoin as cfg

# 18:00 UTC = inside the 17:00-03:00 UTC activity peak; 12:00 UTC = outside it.
PEAK_NOW = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc).timestamp()
OFFPEAK_NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc).timestamp()


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _seed_event(c, *, received_at, fid, decision=None, reason=None, decided_at=None,
                mint="So1mint", network_id="solana", signal_source="global_feed"):
    c.execute(
        "INSERT INTO memecoin_events(fomo_event_id, source_user_id, source_handle, mint, side, "
        "their_price, received_at, created_at, real_decision, real_decision_reason, "
        "real_decision_at, network_id, signal_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, "u1", "u1", mint, "buy", 1.0, received_at, received_at,
         decision, reason, decided_at, network_id, signal_source))
    c.commit()


def _seed_mark(c, *, chain_id, sweep_ts, mint="So1mint"):
    c.execute(
        "INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, source, quality) "
        "VALUES (?,?,?,?,?,?)", (chain_id, mint, sweep_ts, 1.0, "geckoterminal", "live"))
    c.commit()


def _open_real(c, *, mint, opened_at, network_id="solana", entry_mcap=None,
               ticker="TICK", notional=50.0, opened_recorded_at=None):
    return m.open_position(
        c, source_user_id="u1", source_handle="u1", mint=mint, ticker=ticker,
        entry_price=1.0, entry_notional=notional, entry_fill_source="jupiter",
        entry_lag_s=1.0, their_size_usd=1000.0, opened_at=opened_at,
        execution_mode="real", network_id=network_id, entry_mcap=entry_mcap,
        opened_recorded_at=opened_recorded_at)


def _seed_reservation(c, *, mint, reserved_at, status="reserved", network_id="solana",
                      resolution_reason=None):
    c.execute(
        "INSERT INTO memecoin_pending_entries(mint, source_user_id, strategy, "
        "reserved_notional_usd, reserved_at, status, network_id, resolution_reason) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (mint, "u1", "control", 50.0, reserved_at, status, network_id, resolution_reason))
    c.commit()


def _seed_attempt(c, *, attempt_id, started_at, terminal_status, terminal_reason,
                  mint="So1mint", network_id="solana", completed_at=None, position_id=None):
    c.execute(
        "INSERT INTO memecoin_entry_attempts(attempt_id, decision_started_at, terminal_status, "
        "terminal_reason, completed_at, mint, network_id, position_id) VALUES (?,?,?,?,?,?,?,?)",
        (attempt_id, started_at, terminal_status, terminal_reason,
         completed_at or started_at, mint, network_id, position_id))
    c.commit()


def _healthy_seed(c, now):
    """A no-warning baseline: fresh feed event, one entry + one profitable exit in-window,
    marks present for the open position's chain, no reservations."""
    _seed_event(c, received_at=_iso(now - 60), fid="fresh-1")
    open_pid = _open_real(c, mint="OpenMint", opened_at=_iso(now - 1800), network_id="solana")
    _seed_attempt(
        c, attempt_id="healthy-open", started_at=_iso(now - 1801),
        completed_at=_iso(now - 1800), terminal_status="filled", terminal_reason="filled",
        mint="OpenMint", position_id=open_pid,
    )
    pid = _open_real(c, mint="ExitMint", opened_at=_iso(now - 3000), ticker="EXIT")
    _seed_attempt(
        c, attempt_id="healthy-exit", started_at=_iso(now - 3001),
        completed_at=_iso(now - 3000), terminal_status="filled", terminal_reason="filled",
        mint="ExitMint", position_id=pid,
    )
    m.close_position(
        c, pid, _iso(now - 900), 12.50, "real_tp", recorded_at=_iso(now - 900))
    _seed_mark(c, chain_id="solana", sweep_ts=_iso(now - 300))
    return pid


def _msg(c, now, period_s=3600.0):
    data = dg.collect_digest(c, now_epoch=now, period_s=period_s)
    return data, dg.format_digest(data)


# --- normal (green) digest ---------------------------------------------------------------

def test_normal_digest_is_green_with_fills_and_exits(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 150.0)
    c = _conn()
    _healthy_seed(c, OFFPEAK_NOW)
    data, msg = _msg(c, OFFPEAK_NOW)
    assert msg.startswith("\U0001F7E2")            # green circle first line
    assert data["severity"] == "green"
    assert "⚠" not in msg                     # no warning lines
    assert "TICK" in msg                           # entry surfaced
    assert "EXIT" in msg and "+$12.50" in msg      # exit with PnL
    assert "real_tp" in msg
    assert "EXIT +$12.50 (real_tp) @solana" in msg  # explicit network survives telemetry


def test_daily_pnl_line_always_present(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 150.0)
    c = _conn()
    _healthy_seed(c, OFFPEAK_NOW)
    _, msg = _msg(c, OFFPEAK_NOW)
    assert "+$12.50" in msg and "$150" in msg      # realized today vs the configured limit


# --- refusal counts ----------------------------------------------------------------------

def test_refusals_top6_by_reason_within_window():
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")
    reasons = ["r_alpha", "r_bravo", "r_charlie", "r_delta", "r_echo", "r_foxtrot", "r_golf"]
    fid = 0
    for i, rsn in enumerate(reasons):
        for _ in range(len(reasons) - i):          # r_alpha x7 ... r_golf x1
            fid += 1
            _seed_attempt(
                c, attempt_id="a%d" % fid, started_at=_iso(OFFPEAK_NOW - 200),
                terminal_status="refused", terminal_reason=rsn, mint="M%d" % fid,
            )
    # outside the window -> never counted
    _seed_attempt(
        c, attempt_id="old-1", started_at=_iso(OFFPEAK_NOW - 7200),
        terminal_status="refused", terminal_reason="r_alpha", mint="OLD",
    )
    _, msg = _msg(c, OFFPEAK_NOW)
    assert "r_alpha ×7" in msg                     # in-window count only (not 8)
    assert "r_foxtrot ×2" in msg
    assert "r_golf" not in msg                     # 7th reason cut by top-6


def test_execution_failure_is_not_double_counted_as_a_refusal():
    c = _conn()
    decided_at = _iso(OFFPEAK_NOW - 60)
    _seed_event(
        c, received_at=decided_at, fid="failed-origin", decision="failed",
        reason="broadcast_rejected", decided_at=decided_at,
    )
    _seed_attempt(
        c, attempt_id="failed-origin-attempt", started_at=decided_at,
        terminal_status="failed", terminal_reason="broadcast_rejected",
    )

    data, msg = _msg(c, OFFPEAK_NOW)

    assert data["refusals"] == []
    assert data["failed_attempts"] == [("failed", "broadcast_rejected", 1)]
    assert "refusals: broadcast_rejected" not in msg


# --- failed/unknown entry attempts -------------------------------------------------------

def test_failed_and_unknown_attempts_surface_terminal_reasons():
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")
    _seed_attempt(c, attempt_id="a1", started_at=_iso(OFFPEAK_NOW - 600),
                  terminal_status="failed", terminal_reason="reverted_min_out")
    _seed_attempt(c, attempt_id="a2", started_at=_iso(OFFPEAK_NOW - 500),
                  terminal_status="unknown", terminal_reason="confirm timeout")
    # refusals are NOT failures — must not appear in the failed section
    _seed_attempt(c, attempt_id="a3", started_at=_iso(OFFPEAK_NOW - 400),
                  terminal_status="refused", terminal_reason="not_approved")
    data, msg = _msg(c, OFFPEAK_NOW)
    assert "reverted_min_out" in msg
    assert "confirm timeout" in msg
    assert data["refusals"] == [("not_approved", 1)]
    assert all(reason != "not_approved" for _, reason, _ in data["failed_attempts"])
    assert data["severity"] == "yellow"            # failed/unknown attempts warrant a warning
    assert "⚠" in msg


def test_failure_warning_names_the_statuses_present_and_not_unknown():
    """4 aborted + 2 safety_failed used to print "FAILED/unknown" and send the operator to a
    chain explorer for reservations that never existed (2026-08-07 4663 window)."""
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")
    for i in range(4):
        _seed_attempt(c, attempt_id="ab%d" % i, started_at=_iso(OFFPEAK_NOW - 600 + i),
                      terminal_status="aborted", terminal_reason="no quote (no_route: ...)")
    for i in range(2):
        _seed_attempt(c, attempt_id="sf%d" % i, started_at=_iso(OFFPEAK_NOW - 500 + i),
                      terminal_status="safety_failed", terminal_reason="no sell route")
    _data, msg = _msg(c, OFFPEAK_NOW)
    warn = next(l for l in msg.splitlines() if "failure status" in l)
    assert "6 entry attempt(s)" in warn
    assert "aborted/safety_failed" in warn
    assert "unknown" not in warn


def test_failure_warning_still_explains_unknown_when_one_is_present():
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")
    _seed_attempt(c, attempt_id="u1", started_at=_iso(OFFPEAK_NOW - 600),
                  terminal_status="unknown", terminal_reason="confirm timeout")
    _data, msg = _msg(c, OFFPEAK_NOW)
    warn = next(l for l in msg.splitlines() if "failure status" in l)
    assert "chain explorer check" in warn


def test_attempt_completed_in_window_is_not_lost_when_it_started_earlier():
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")
    _seed_attempt(
        c, attempt_id="slow-attempt", started_at=_iso(OFFPEAK_NOW - 7200),
        completed_at=_iso(OFFPEAK_NOW - 60), terminal_status="failed",
        terminal_reason="completed_inside_window",
    )

    data, msg = _msg(c, OFFPEAK_NOW)

    assert data["failed_attempts"] == [
        ("failed", "completed_inside_window", 1),
    ]
    assert "completed_inside_window" in msg


def test_accounting_anomaly_and_error_attempts_are_failures():
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")
    _seed_attempt(
        c, attempt_id="anomaly-attempt", started_at=_iso(OFFPEAK_NOW - 120),
        terminal_status="anomaly", terminal_reason="missing_token_delta",
    )
    _seed_attempt(
        c, attempt_id="error-attempt", started_at=_iso(OFFPEAK_NOW - 60),
        terminal_status="error", terminal_reason="pre_handoff_exception",
    )

    data, msg = _msg(c, OFFPEAK_NOW)

    assert ("anomaly", "missing_token_delta", 1) in data["failed_attempts"]
    assert ("error", "pre_handoff_exception", 1) in data["failed_attempts"]
    assert data["severity"] == "yellow"
    assert "missing_token_delta" in msg
    assert "pre_handoff_exception" in msg


def test_future_clock_rows_are_excluded_from_every_window():
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")
    future = _iso(OFFPEAK_NOW + 3600)
    _seed_event(
        c, received_at=future, fid="future-event", decision="refused",
        reason="future_refusal", decided_at=future, mint="FutureEventMint",
    )
    _seed_attempt(
        c, attempt_id="future-attempt", started_at=future,
        terminal_status="failed", terminal_reason="future_failure",
        mint="FutureAttemptMint",
    )
    future_pid = _open_real(c, mint="FuturePositionMint", opened_at=future, ticker="FUTURE")
    _seed_attempt(
        c, attempt_id="future-fill", started_at=future, completed_at=future,
        terminal_status="filled", terminal_reason="filled", mint="FuturePositionMint",
        position_id=future_pid,
    )

    data, msg = _msg(c, OFFPEAK_NOW)

    assert data["entries"] == []
    assert data["failed_attempts"] == []
    assert "future_refusal" not in msg
    assert "future_failure" not in msg
    assert "FUTURE" not in msg


def test_contiguous_windows_do_not_double_count_shared_boundary():
    c = _conn()
    since = OFFPEAK_NOW - 3600
    boundary = _iso(since)
    _seed_event(
        c, received_at=boundary, fid="boundary-refusal", decision="refused",
        reason="already_reported", decided_at=boundary,
    )
    _seed_attempt(
        c, attempt_id="boundary-attempt", started_at=boundary,
        completed_at=boundary, terminal_status="failed",
        terminal_reason="already_reported",
    )
    boundary_pid = _open_real(c, mint="BoundaryMint", opened_at=boundary, ticker="BOUNDARY")
    _seed_attempt(
        c, attempt_id="boundary-fill", started_at=boundary, completed_at=boundary,
        terminal_status="filled", terminal_reason="filled", mint="BoundaryMint",
        position_id=boundary_pid,
    )

    data = dg.collect_digest(
        c, now_epoch=OFFPEAK_NOW, period_s=3600.0, since_epoch=since,
    )
    msg = dg.format_digest(data)

    assert data["entries"] == []
    assert data["refusals"] == []
    assert data["failed_attempts"] == []
    assert "already_reported" not in msg
    assert "BOUNDARY" not in msg


def test_entry_persisted_after_watermark_is_reported_by_completion_time():
    c = _conn()
    first_end = OFFPEAK_NOW - 60
    first = dg.collect_digest(
        c, now_epoch=first_end, period_s=60.0, since_epoch=first_end - 60,
    )
    assert first["entries"] == []

    # The funded opened_at stamp predates the first watermark, but confirmation/persistence finish
    # after it. The second pass must not lose the entry permanently.
    opened_at = _iso(first_end - 1)
    completed_at = _iso(first_end + 1)
    pid = _open_real(c, mint="LateCommitMint", opened_at=opened_at, ticker="LATE")
    _seed_attempt(
        c, attempt_id="late-commit", started_at=opened_at, completed_at=completed_at,
        terminal_status="filled", terminal_reason="filled", mint="LateCommitMint",
        position_id=pid,
    )

    second = dg.collect_digest(
        c, now_epoch=OFFPEAK_NOW, period_s=60.0, since_epoch=first_end,
    )

    assert [entry["mint"] for entry in second["entries"]] == ["LateCommitMint"]


def test_filled_position_without_trace_uses_recorded_time_fallback():
    c = _conn()
    first_end = OFFPEAK_NOW - 60
    opened_at = _iso(first_end - 1)
    recorded_at = _iso(first_end + 1)
    _open_real(
        c, mint="TraceLessMint", opened_at=opened_at, ticker="TRACELESS",
        opened_recorded_at=recorded_at,
    )

    data = dg.collect_digest(
        c, now_epoch=OFFPEAK_NOW, period_s=60.0, since_epoch=first_end,
    )

    assert [entry["mint"] for entry in data["entries"]] == ["TraceLessMint"]


def test_exit_persisted_after_watermark_is_reported_by_recorded_time():
    c = _conn()
    first_end = OFFPEAK_NOW - 60
    first = dg.collect_digest(
        c, now_epoch=first_end, period_s=60.0, since_epoch=first_end - 60,
    )
    assert first["exits"] == []

    # Exit evaluation can stamp closed_at before a slow chain confirmation. Record the DB commit
    # time separately so a close that lands after the watermark is not lost.
    closed_at = _iso(first_end - 1)
    recorded_at = _iso(first_end + 1)
    pid = _open_real(c, mint="LateExitMint", opened_at=_iso(first_end - 600), ticker="LATEEXIT")
    m.close_position(c, pid, closed_at, 7.0, "real_tp", recorded_at=recorded_at)

    second = dg.collect_digest(
        c, now_epoch=OFFPEAK_NOW, period_s=60.0, since_epoch=first_end,
    )

    assert [exit_row["mint"] for exit_row in second["exits"]] == ["LateExitMint"]


# --- daily loss vs breaker ---------------------------------------------------------------

def test_daily_loss_beyond_60pct_of_limit_is_red(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 150.0)
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")
    pid = _open_real(c, mint="LossMint", opened_at=_iso(OFFPEAK_NOW - 3000), ticker="LOSS")
    m.close_position(c, pid, _iso(OFFPEAK_NOW - 900), -100.0, "b_stop")
    _seed_mark(c, chain_id="solana", sweep_ts=_iso(OFFPEAK_NOW - 300))
    data, msg = _msg(c, OFFPEAK_NOW)
    assert msg.startswith("\U0001F534")            # red circle — breaker-relevant
    assert data["severity"] == "red"
    assert "⚠" in msg and "-$100.00" in msg and "$150" in msg


def test_breaker_tripped_is_red():
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")
    day = _iso(OFFPEAK_NOW)[:10]
    c.execute("INSERT INTO memecoin_daily_loss_trips(day, tripped_at) VALUES (?,?)", (day, day))
    c.commit()
    data, msg = _msg(c, OFFPEAK_NOW)
    assert data["severity"] == "red"
    assert "TRIPPED" in msg


def test_breaker_off_no_loss_warning(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 0.0)
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")
    pid = _open_real(c, mint="LossMint", opened_at=_iso(OFFPEAK_NOW - 3000))
    m.close_position(c, pid, _iso(OFFPEAK_NOW - 900), -500.0, "b_stop")
    _seed_mark(c, chain_id="solana", sweep_ts=_iso(OFFPEAK_NOW - 300))
    data, msg = _msg(c, OFFPEAK_NOW)
    assert data["severity"] != "red"
    assert "breaker OFF" in msg


# --- stale reservations ------------------------------------------------------------------

def test_stale_reservation_warns(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 150.0)
    c = _conn()
    _healthy_seed(c, OFFPEAK_NOW)
    _seed_reservation(
        c, mint="0xStuckMintAbcdef", reserved_at=_iso(OFFPEAK_NOW - 1200),
        network_id="4663",
    )
    data, msg = _msg(c, OFFPEAK_NOW)
    assert data["severity"] == "yellow"
    assert "⚠" in msg
    assert "0xStuckMin" in msg                     # names the stuck mint — actionable
    assert "4663" in msg                           # identity is (network_id, mint)
    assert "20m" in msg                            # its age


def test_fresh_reservation_does_not_warn(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 150.0)
    c = _conn()
    _healthy_seed(c, OFFPEAK_NOW)
    _seed_reservation(c, mint="FreshMint", reserved_at=_iso(OFFPEAK_NOW - 120))
    data, msg = _msg(c, OFFPEAK_NOW)
    assert data["severity"] == "green"


def test_malformed_reservation_timestamp_is_explicitly_unknown(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 150.0)
    c = _conn()
    _healthy_seed(c, OFFPEAK_NOW)
    _seed_reservation(c, mint="BadClockMint", reserved_at="not-a-time", network_id="8453")

    data, msg = _msg(c, OFFPEAK_NOW)

    assert data["severity"] == "yellow"
    assert "8453/BadClockMi" in msg
    assert "unparseable reserved_at" in msg
    assert "oldest age unknown" in msg


# --- feed freshness (events gap) ---------------------------------------------------------

def test_events_gap_warns_during_peak_only(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 150.0)
    for now, expect_warn in ((PEAK_NOW, True), (OFFPEAK_NOW, False)):
        c = _conn()
        _seed_event(c, received_at=_iso(now - 1200), fid="stale-1")     # 20 min old
        _seed_mark(c, chain_id="solana", sweep_ts=_iso(now - 300))
        data, msg = _msg(c, now)
        if expect_warn:
            assert data["severity"] == "yellow"
            assert "feed" in msg.lower() and "⚠" in msg
        else:
            assert data["severity"] == "green"


def test_a_lane_retired_for_days_does_not_decide_feed_freshness(monkeypatch):
    """2026-09-18 06:36Z: the digest read "last feed event: 12408m ago" while the J7 relay was
    delivering and an entry had just filled. It measured `global_feed` alone, and that lane has
    delivered nothing since 2026-09-09."""
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 150.0)
    c = _conn()
    _seed_event(c, received_at=_iso(PEAK_NOW - 744480), fid="retired-global",
                signal_source="global_feed")
    _seed_event(c, received_at=_iso(PEAK_NOW - 60), fid="live-relay",
                signal_source="follow_activity")

    data, msg = _msg(c, PEAK_NOW)

    assert data["events_age_s"] == pytest.approx(60.0)
    assert data["events_lane"] == "follow_activity"
    assert data["quiet_lanes"] == []
    assert "(follow_activity)" in msg
    assert "12408m" not in msg


def test_fresh_follow_activity_cannot_mask_stale_global_feed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 150.0)
    c = _conn()
    _seed_event(
        c, received_at=_iso(PEAK_NOW - 1200), fid="stale-global",
        signal_source="global_feed",
    )
    _seed_event(
        c, received_at=_iso(PEAK_NOW - 60), fid="fresh-recovery",
        signal_source="follow_activity",
    )

    data, msg = _msg(c, PEAK_NOW)

    # The headline age is the live lane's; the lane that stopped TODAY still raises the alarm.
    assert data["events_age_s"] == pytest.approx(60.0)
    assert data["quiet_lanes"] == [("global_feed", pytest.approx(1200.0))]
    assert data["severity"] == "yellow"
    assert "global_feed quiet for" in msg


def test_no_events_at_all_warns_during_peak():
    c = _conn()
    data, msg = _msg(c, PEAK_NOW)
    assert data["severity"] == "yellow"
    assert "⚠" in msg


# --- GT/marks health ---------------------------------------------------------------------

def test_zero_marks_for_open_real_chain_warns(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 150.0)
    c = _conn()
    _healthy_seed(c, OFFPEAK_NOW)                  # solana open + solana marks -> healthy
    _open_real(c, mint="0xEvmMint", opened_at=_iso(OFFPEAK_NOW - 7200), network_id="4663")
    data, msg = _msg(c, OFFPEAK_NOW)               # no 4663 marks in the last hour
    assert data["severity"] == "yellow"
    assert "⚠" in msg and "4663" in msg
    assert "blind" in msg.lower()                  # says what it means: exits are blind


# --- ladder cohort progress --------------------------------------------------------------

def test_ladder_cohort_progress_by_band():
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")
    old = _iso(OFFPEAK_NOW - 5 * 86400)
    pids = {}
    for mint, mcap in (("NanoMint", None), ("MicroMint", 100_000.0), ("MidMint", 1_500_000.0)):
        pid = _open_real(c, mint=mint, opened_at=old, entry_mcap=mcap, network_id="solana")
        pids[mint] = pid
        m.close_position(c, pid, old, 1.0, "real_tp")
        c.execute("INSERT INTO memecoin_position_exit_overrides(position_id, source, active, "
                  "created_at) VALUES (?, 'micro', 1, ?)", (pid, old))
    c.commit()
    # Replacing an override preserves the old inactive row for audit. It is still one position.
    m.set_position_exit_override(
        c, pids["NanoMint"], "micro", tp_pct=50.0,
        now_iso=_iso(OFFPEAK_NOW - 4 * 86400),
    )
    # Clearing the active rail later must not erase historical cohort membership.
    m.clear_position_exit_override(
        c, pids["NanoMint"], "micro",
        now_iso=_iso(OFFPEAK_NOW - 3 * 86400),
    )
    _, msg = _msg(c, OFFPEAK_NOW)
    assert "nano 1/30" in msg
    assert "micro 1/30" in msg
    assert "mid 1/30" in msg


# --- failed pending entries --------------------------------------------------------------

def test_failed_reservations_are_not_timestamped_from_reservation_creation():
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")
    _seed_reservation(c, mint="FailMint", reserved_at=_iso(OFFPEAK_NOW - 600),
                      status="failed", resolution_reason="not_found_terminal")
    data, msg = _msg(c, OFFPEAK_NOW)

    # pending_entries has no resolution timestamp. Using reserved_at would falsely claim when
    # this failure happened; completed entry attempts are the timestamped terminal source.
    assert "pending_failed" not in data
    assert "pending failed" not in msg
    assert "not_found_terminal" not in msg


# --- digest_pass: send + fail-soft -------------------------------------------------------

def test_digest_pass_queues_and_reports_without_synchronous_send(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 150.0)
    c = _conn()
    _healthy_seed(c, OFFPEAK_NOW)
    queued = []

    def fake_enqueue(text):
        queued.append(text)
        return True

    res = dg.digest_pass(
        c, now_epoch=OFFPEAK_NOW, period_s=3600.0, enqueue_fn=fake_enqueue,
    )
    assert res["queued"] is True and res["ok"] is True
    assert res["severity"] == "green"
    assert len(queued) == 1 and queued[0].startswith("\U0001F7E2")


def test_digest_pass_fail_soft_on_queue_exception():
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")

    def broken_enqueue(text):
        raise RuntimeError("queue down")

    res = dg.digest_pass(
        c, now_epoch=OFFPEAK_NOW, period_s=3600.0, enqueue_fn=broken_enqueue,
    )
    assert res["ok"] is False                      # never raises into the runner loop
    assert res["queued"] is False


def test_digest_pass_fail_soft_on_queue_refusal():
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")
    res = dg.digest_pass(
        c, now_epoch=OFFPEAK_NOW, period_s=3600.0,
        enqueue_fn=lambda text: False,
    )
    assert res["queued"] is False and res["ok"] is False


def test_worker_sleeps_before_connect_and_uses_contiguous_windows():
    steps = []
    monotonic_now = [0.0]
    clock_values = iter((1000.0, 1062.0, 1125.0))
    conn = object()
    windows = []

    def sleep_fn(delay):
        steps.append(("sleep", delay))
        monotonic_now[0] += delay

    def connection_factory():
        steps.append(("connect", monotonic_now[0]))
        return conn

    def digest_fn(actual_conn, **kwargs):
        steps.append(("digest", monotonic_now[0]))
        assert actual_conn is conn
        windows.append((kwargs["since_epoch"], kwargs["now_epoch"]))
        return {"queued": True, "severity": "green"}

    dg.run_digest_worker(
        connection_factory=connection_factory,
        period_s=60.0,
        clock_fn=lambda: next(clock_values),
        monotonic_fn=lambda: monotonic_now[0],
        sleep_fn=sleep_fn,
        digest_fn=digest_fn,
        max_runs=2,
    )

    assert steps[0] == ("sleep", 60.0)
    assert steps[1] == ("connect", 60.0)
    assert windows == [(1000.0, 1062.0), (1062.0, 1125.0)]
    assert [step for step in steps if step[0] == "sleep"] == [
        ("sleep", 60.0), ("sleep", 60.0),
    ]


def test_worker_retries_unadvanced_window_after_connection_failure():
    monotonic_now = [0.0]
    clock_values = iter((1000.0, 1060.0, 1120.0))
    attempts = {"connections": 0}
    windows = []

    def sleep_fn(delay):
        monotonic_now[0] += delay

    def connection_factory():
        attempts["connections"] += 1
        if attempts["connections"] == 1:
            raise RuntimeError("db unavailable")
        return object()

    def digest_fn(_conn, **kwargs):
        windows.append((kwargs["since_epoch"], kwargs["now_epoch"]))
        return {"queued": True, "severity": "yellow"}

    dg.run_digest_worker(
        connection_factory=connection_factory,
        period_s=60.0,
        clock_fn=lambda: next(clock_values),
        monotonic_fn=lambda: monotonic_now[0],
        sleep_fn=sleep_fn,
        digest_fn=digest_fn,
        log_fn=lambda _message: None,
        max_runs=2,
    )

    assert attempts["connections"] == 2
    assert windows == [(1000.0, 1120.0)]


def test_worker_reopens_connection_after_collection_failure():
    monotonic_now = [0.0]
    clock_values = iter((1000.0, 1060.0, 1120.0))
    connections = []
    calls = {"digest": 0}

    class FakeConnection:
        in_transaction = False

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    def sleep_fn(delay):
        monotonic_now[0] += delay

    def connection_factory():
        conn = FakeConnection()
        connections.append(conn)
        return conn

    def digest_fn(_conn, **_kwargs):
        calls["digest"] += 1
        if calls["digest"] == 1:
            return {"collected": False, "queued": False, "severity": None}
        return {"collected": True, "queued": True, "severity": "green"}

    dg.run_digest_worker(
        connection_factory=connection_factory,
        period_s=60.0,
        clock_fn=lambda: next(clock_values),
        monotonic_fn=lambda: monotonic_now[0],
        sleep_fn=sleep_fn,
        digest_fn=digest_fn,
        max_runs=2,
    )

    assert len(connections) == 2
    assert connections[0].closed is True


def test_digest_pass_fail_soft_on_missing_schema():
    c = sqlite3.connect(":memory:")
    queued = []

    result = dg.digest_pass(
        c, now_epoch=OFFPEAK_NOW, period_s=3600,
        enqueue_fn=lambda text: queued.append(text),
    )

    assert result == {
        "collected": False, "queued": False, "ok": False,
        "severity": None, "message": None,
    }
    assert queued == []


def test_digest_message_is_bounded_for_telegram():
    c = _conn()
    _seed_event(c, received_at=_iso(OFFPEAK_NOW - 50), fid="fresh-1")
    for i in range(6):
        _seed_attempt(
            c, attempt_id="long-%d" % i, started_at=_iso(OFFPEAK_NOW - 100),
            terminal_status="failed", terminal_reason=("x" * 1000) + str(i),
        )

    _, msg = _msg(c, OFFPEAK_NOW)

    assert len(msg) <= dg._MAX_MESSAGE_CHARS
    assert msg.endswith(" …")


@pytest.mark.parametrize("recent", ["2026-07-17 17:59:50", "2026-07-17T13:59:50-04:00"])
def test_feed_age_uses_latest_instant_across_timestamp_formats(recent):
    c = _conn()
    _seed_event(c, received_at="2026-07-17T17:00:00+00:00", fid="older")
    _seed_event(c, received_at=recent, fid="newer")
    _seed_event(c, received_at="2026-07-17T19:00:00+00:00", fid="future")
    data = dg.collect_digest(c, now_epoch=PEAK_NOW)
    assert data["events_age_s"] == pytest.approx(10.0)
    assert data["severity"] == "green"


@pytest.mark.parametrize("limit,tripped", [(0.0, False), (125.0, True)])
def test_digest_resolves_daily_override_before_reporting_a_latched_halt(limit, tripped):
    c = _conn()
    day = _iso(OFFPEAK_NOW)[:10]
    c.execute("INSERT INTO memecoin_daily_loss_trips(day, tripped_at) VALUES (?,?)", (day, day))
    m.set_entry_knob_override(c, "MEMECOIN_DAILY_LOSS_LIMIT_USD", limit,
                             active=True, actor="test", reason="test", now_iso=_iso(OFFPEAK_NOW))
    data, msg = _msg(c, OFFPEAK_NOW)
    assert data["daily_limit"] == limit
    assert data["breaker_tripped"] is tripped
    assert ("entries halted" in msg) is tripped
    assert m.is_daily_loss_tripped(c, day)
