import sqlite3
from types import SimpleNamespace

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as rr
from sniper.settings import memecoin as cfg


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _position(conn, *, entry_notional=100.0, entry_price=1.0,
              opened_at="2026-07-11T00:00:00+00:00"):
    pid = m.open_position(
        conn, source_user_id="u", source_handle="h", mint="M", ticker="T",
        entry_price=entry_price, entry_notional=entry_notional, entry_fill_source="chain",
        entry_lag_s=0, their_size_usd=0, opened_at=opened_at, strategy="alpha_b",
        execution_mode="real", network_id="solana",
    )
    m.insert_fill(
        conn, position_id=pid, kind="entry", price=entry_price,
        qty=entry_notional / entry_price, notional=entry_notional,
        fill_source="chain", fraction=1.0, created_at=opened_at,
    )
    return m.get_position(conn, pid)


def _preview(usd):
    return SimpleNamespace(
        expected_usdc=usd, amount_atomic=1000, route_id="route", quoted_at="2026-07-11T01:00:00+00:00",
    )


def test_execution_quantity_uses_the_frozen_stop_after_config_drift(monkeypatch):
    from test_memecoin_real_rails_v2 import _frozen_pos
    conn = _conn()
    pos = _frozen_pos(stop=35)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 5)
    assert not rr.executable_exit_allowed(conn, pos, "b_stop", _preview(9), quote_qty=10)[0]
    assert rr.executable_exit_allowed(conn, pos, "b_stop", _preview(6), quote_qty=10)[0]
    assert not rr.executable_exit_allowed(conn, pos, "b_stop", _preview(6), quote_qty=0)[0]


def test_tp_and_stop_require_executable_thresholds(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 50.0)
    conn = _conn()
    pos = _position(conn)

    assert rr.executable_exit_allowed(conn, pos, "real_tp", _preview(159.99))[0] is False
    assert rr.executable_exit_allowed(conn, pos, "real_tp", _preview(160.0))[0] is True
    assert rr.executable_exit_allowed(conn, pos, "b_stop", _preview(50.01))[0] is False
    assert rr.executable_exit_allowed(conn, pos, "b_stop", _preview(50.0))[0] is True


def test_prior_partial_proceeds_are_included_in_projected_exit_pnl(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    conn = _conn()
    pos = _position(conn)
    m.insert_fill(
        conn, position_id=pos["id"], kind="derisk", price=1.0, qty=50,
        notional=50.0, fill_source="chain", fraction=0.5,
        created_at="2026-07-11T00:30:00+00:00",
    )
    assert m.open_qty(conn, pos["id"]) == 50.0
    allowed, detail = rr.executable_exit_allowed(conn, pos, "real_tp", _preview(110.0))
    assert allowed is True
    assert detail["projected_proceeds"] == 160.0
    assert detail["projected_pnl_pct"] == 60.0


def test_missing_or_nonfinite_preview_suppresses_price_exit():
    conn = _conn()
    pos = _position(conn)
    assert rr.executable_exit_allowed(conn, pos, "real_tp", None)[0] is False
    assert rr.executable_exit_allowed(conn, pos, "b_stop", _preview(float("nan")))[0] is False


def test_real_sweep_rejects_display_stop_when_executable_quote_is_better(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    conn = _conn()
    pos = _position(conn)
    calls = []
    context = {
        "preview_exit_fn": lambda _pos: _preview(60.0),  # executable loss is only -40%
        "exit_fn": lambda *_args: calls.append("exit") or {"status": "closed"},
    }
    rr.real_exit_sweep(
        conn, {"solana": context}, price_fn=lambda _mints: {"M": {"price": 0.40}},
        now_fn=lambda: "2026-07-11T01:00:00+00:00",
    )
    assert calls == []
    assert m.get_position(conn, pos["id"])["stop_hits"] == 0
    audit = conn.execute(
        "SELECT allowed, detail FROM memecoin_exit_trigger_audit WHERE position_id=?",
        (pos["id"],),
    ).fetchone()
    assert audit["allowed"] == 0 and "threshold" in audit["detail"]


def test_real_sweep_executes_stop_when_preview_confirms_loss(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    conn = _conn()
    _position(conn)
    calls = []
    context = {
        "preview_exit_fn": lambda _pos: _preview(40.0),
        "exit_fn": lambda _conn, _pos, reason, _now: calls.append(reason) or {"status": "unknown"},
    }
    rr.real_exit_sweep(
        conn, {"solana": context}, price_fn=lambda _mints: {"M": {"price": 0.40}},
        now_fn=lambda: "2026-07-11T01:00:00+00:00",
    )
    assert calls == ["b_stop"]


def test_derisk_fraction_uses_executable_proceeds_not_display_mid():
    conn = _conn()
    pos = _position(conn, entry_notional=30.0, entry_price=0.02)
    seen = {}
    context = {
        "derisk_fn": lambda _conn, _pos, fraction, _now: seen.update(fraction=fraction)
        or {"status": "derisked"},
    }
    result = rr._run_derisk(
        conn, context, pos, mid=0.08, now_iso="2026-07-11T01:00:00+00:00",
        preview=_preview(60.0),
    )
    assert result["status"] == "derisked"
    assert seen["fraction"] == 0.5


def test_timecap_executes_without_preview(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 60.0)
    conn = _conn()
    _position(conn)
    calls = []
    context = {
        "exit_fn": lambda _conn, _pos, reason, _now: calls.append(reason) or {"status": "unknown"},
    }
    rr.real_exit_sweep(
        conn, {"solana": context}, price_fn=lambda _mints: {},
        now_fn=lambda: "2026-07-11T01:00:00+00:00",
    )
    assert calls == ["b_timecap"]


def test_execution_stop_refuses_only_on_evidence_and_survives_a_locked_audit(monkeypatch):
    """2026-09-11 (Gamma D2). An unreadable execution quote lets the sweep's confirmed stop sell;
    a computed value above the stop still holds it; a failing audit write changes nothing."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as R
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="M", ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source="chain",
                          entry_lag_s=0.0, their_size_usd=0.0, opened_at="2026-09-11T00:00:00Z",
                          strategy="alpha_b", execution_mode="real", network_id="4663")
    pos = m.get_position(c, pid)
    ok, detail = R.confirm_execution_stop(c, pos, None, 100.0)
    assert ok is True and detail["code"] == "execution_quote_unreadable_sweep_verdict_stands"
    assert detail["unreadable_code"] == "preview_unavailable"
    ok, detail = R.confirm_execution_stop(c, pos, 95.0, 100.0)       # -5%: nowhere near a stop
    assert ok is False and detail["code"] == "executable_threshold_not_met"
    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(m, "record_exit_trigger_audit", locked)
    ok, detail = R.confirm_execution_stop(c, pos, None, 100.0)
    assert ok is True


def test_the_execution_check_does_not_overwrite_the_sweeps_stop_audit():
    """2026-09-11 (Gamma D6): the audit upserts on (position, reason, minute)."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as R
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="M", ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source="chain",
                          entry_lag_s=0.0, their_size_usd=0.0, opened_at="2026-09-11T00:00:00Z",
                          strategy="alpha_b", execution_mode="real", network_id="4663")
    from sniper.util.clock import real_utc_now_iso
    m.record_exit_trigger_audit(c, position_id=pid, reason="b_stop", display_price=0.6,
                                preview_usdc=60.0, allowed=True, detail="{}",
                                checked_at=real_utc_now_iso(), lane="fastlane_poll")
    R.confirm_execution_stop(c, m.get_position(c, pid), 95.0, 100.0)
    reasons = sorted(r["reason"] for r in c.execute(
        "SELECT reason FROM memecoin_exit_trigger_audit WHERE position_id=?", (pid,)))
    assert reasons == ["b_stop", "b_stop_execution"]
