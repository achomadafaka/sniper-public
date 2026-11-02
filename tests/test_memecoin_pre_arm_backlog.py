import sqlite3
from types import SimpleNamespace

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as runner
from sniper.settings import memecoin as cfg


ARMED_AT = "2026-08-19T15:17:15.853107+00:00"
NOW = "2026-08-19T15:17:19.093000+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "Binkieee")
    return conn


def _arm(conn, at=ARMED_AT):
    result = m.set_wallet_chain_armed(
        conn, "u1", "solana", True, force=True, now_iso=at,
    )
    assert result["ok"] is True


def _event(created_at, source="follow_activity"):
    return SimpleNamespace(
        fomo_event_id="event-1",
        source_user_id="u1",
        source_handle="Binkieee",
        mint="So1Mint",
        ticker="LONGCAT",
        side="buy",
        network_id="solana",
        created_at=created_at,
        received_at=NOW,
        received_at_normalized=NOW,
        clock_domain="observed_real_utc",
        source_timestamp_trust="claimed_untrusted",
        their_market_cap=500_000.0,
        their_price=1.0,
        their_size_usd=719.0,
        signal_source=source,
        lag_s=245.3,
    )


def _hook(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROSTER_GATE_ENABLED", True)
    monkeypatch.setattr(
        runner, "real_entry_hook", lambda *_args, **_kwargs: {"status": "filled"},
    )
    monkeypatch.setattr(runner, "_alert_entry_result", lambda *_args, **_kwargs: None)
    return runner.make_real_hook(
        None,
        {"solana": object()},
        now_fn=lambda: NOW,
        trade_size=10.0,
        max_age_s=900.0,
        blind_mcap_fn=lambda *_args: None,
    )


def test_initial_arm_can_supply_an_admission_start():
    conn = _conn()
    _arm(conn)

    assert m.wallet_real_cohort_start(
        conn, "u1", network_id="solana", include_initial=True,
    ) == ARMED_AT
    assert m.wallet_real_cohort_start(conn, "u1", network_id="solana") is None


def test_redundant_arm_does_not_move_the_admission_start():
    conn = _conn()
    _arm(conn, "2026-08-19T14:00:00+00:00")
    m.set_wallet_chain_armed(
        conn, "u1", "solana", False, now_iso="2026-08-19T15:00:00+00:00",
    )
    _arm(conn, ARMED_AT)
    _arm(conn, "2026-08-19T15:18:00+00:00")

    assert m.wallet_real_cohort_start(
        conn, "u1", network_id="solana", include_initial=True,
    ) == ARMED_AT


def test_follow_buy_before_the_arm_is_refused(monkeypatch):
    conn = _conn()
    _arm(conn)

    result = _hook(monkeypatch)(conn, _event("2026-08-19T15:13:13.153000+00:00"))

    assert result["status"] == "pre_arm_backlog"
    assert "predates the solana arm" in result["reason"]


def test_follow_buy_after_the_arm_is_admitted(monkeypatch):
    conn = _conn()
    _arm(conn)

    result = _hook(monkeypatch)(conn, _event("2026-08-19T15:17:16+00:00"))

    assert result["status"] == "filled"


def test_unreadable_source_time_fails_open(monkeypatch):
    conn = _conn()
    _arm(conn)

    result = _hook(monkeypatch)(conn, _event("not-a-time"))

    assert result["status"] == "filled"


def test_global_feed_does_not_use_the_arm_boundary(monkeypatch):
    conn = _conn()
    _arm(conn)

    result = _hook(monkeypatch)(
        conn, _event("2026-08-19T15:13:13.153000+00:00", source="global_feed"),
    )

    assert result["status"] == "filled"


def test_pre_arm_backlog_is_a_refusal():
    assert runner._trace_terminal(
        {"status": "pre_arm_backlog", "reason": "source event predates the arm"},
    ) == ("refused", "source event predates the arm")
