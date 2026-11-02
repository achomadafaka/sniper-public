"""A manual entry is the operator naming a coin, not a copy of somebody's buy.

Operator 2026-08-23, after mint 45fnDDoS... was refused "source buy $0 under floor $249": the
source-buy floor is fail-closed on a missing size, and a manual entry has no source buy at all
(their_size_usd is 0 by construction), so EVERY UI buy was blocked from the moment the floor
shipped. Three properties are pinned here:

  1. the floor exempts signal_source='manual' and still refuses a copy of the same size
  2. a manual entry takes MEMECOIN_MANUAL_TRADE_SIZE_USD (or the caller's size_usd), never the
     bankroll clip, and the override faces the whole risk gate rather than bypassing it
  3. naming a trader is optional and records source attribution only
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_arming
from sniper.services import memecoin_real_runner as runner
from sniper.services import memecoin_risk as risk
from sniper.settings import memecoin as cfg

NOW = "2026-07-15T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.005,
                        raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 100.0,
                        raising=False)
    monkeypatch.setattr(runner, "_alert_entry_result", lambda *a, **k: None)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "alice")
    m.set_wallet_real_approved(conn, "u1", True, chains=["solana"], force=True)
    return conn


def _event(*, signal_source, their_size_usd, source_user_id="u1", ticker="ABC"):
    return SimpleNamespace(
        side="buy", source_user_id=source_user_id, source_handle=None, mint="So1Mint",
        ticker=ticker, network_id="solana", their_price=None, their_market_cap=100_000.0,
        their_size_usd=their_size_usd, is_first_buy=True, fomo_event_id=None,
        created_at="2026-07-15T11:59:59+00:00", received_at="2026-07-15T11:59:59+00:00",
        received_at_normalized="2026-07-15T11:59:59.900000+00:00",
        clock_domain="observed_real_utc", lag_s=0.1, signal_source=signal_source,
    )


def _context(spent, impact_caps=None):
    def execute(conn, pending_id, mint, size, now_iso, impact_cap_pct=None, **kw):
        spent.append(size)
        if impact_caps is not None:
            impact_caps.append(impact_cap_pct)
        return {"status": "unknown", "reason": "confirmation unknown", "sig": "sig"}

    return {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0,
            "token_safety_fn": lambda mint, **kw: (True, "ok"),
            "execute_fn": execute, "kill_path": None}


def _open_real(conn, mint, *, signal_source):
    m.open_position(
        conn,
        source_user_id="u1",
        source_handle="(real)",
        mint=mint,
        ticker="T",
        entry_price=1.0,
        entry_notional=20.0,
        entry_fill_source="chain",
        entry_lag_s=0.0,
        their_size_usd=0.0,
        opened_at=NOW,
        strategy="alpha_b",
        execution_mode="real",
        network_id="solana",
        signal_source=signal_source,
    )


def _run(conn, ev, trade_size=20.0, classification_context=None):
    spent = []
    res = runner.real_entry_hook(
        conn, ev, {"solana": _context(spent)}, now_fn=lambda: NOW,
        trade_size=trade_size, max_age_s=120,
        classification_context=classification_context)
    return res, spent


# --- 1. the floor exempts manual, and only manual -----------------------------------------

def test_manual_entry_is_not_refused_by_the_source_buy_floor():
    conn = _conn()
    res, spent = _run(conn, _event(signal_source="manual", their_size_usd=0.0))
    assert res["status"] != "rejected", res
    assert spent, "the manual entry never reached the executor"


def test_a_copy_with_the_same_zero_size_is_still_refused():
    """The exemption is scoped by signal_source; it must not open the floor for copies."""
    conn = _conn()
    res, spent = _run(conn, _event(signal_source="global_feed", their_size_usd=0.0))
    assert res["status"] == "rejected"
    assert "under mcap floor 0.500% ($500)" in res["reason"]
    assert spent == []


def test_follow_activity_copy_under_the_floor_is_still_refused():
    conn = _conn()
    res, spent = _run(conn, _event(signal_source="follow_activity", their_size_usd=200.0))
    assert res["status"] == "rejected"
    assert spent == []


# --- 2. sizing ----------------------------------------------------------------------------

def test_manual_entry_spends_the_requested_clip_not_the_bankroll(monkeypatch):
    """Bankroll sizing would spend the whole balance; a manual entry must spend what was asked."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 1)
    conn = _conn()
    res, spent = _run(conn, _event(signal_source="manual", their_size_usd=0.0), trade_size=20.0)
    assert res["status"] != "rejected", res
    assert spent == [20.0], f"expected a $20 clip, spent {spent} (balance was $300)"


def test_manual_percent_is_resolved_in_the_runner_from_chain_equity(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 0.0)
    conn = _conn()
    spent = []
    fn = runner.make_real_buy_fn(
        {"solana": _context(spent)},
        stats_fn=lambda *a, **k: {"market_cap": 100_000.0},
    )

    result = fn(
        conn, "So1PercentMint", None, network_id="solana", size_pct_equity=0.13,
        origin_event_id="origin-percent",
    )

    assert result["status"] != "rejected", result
    assert result["resolved_size_usd"] == 39.0
    assert spent == [39.0]
    attempt = conn.execute(
        "SELECT origin_event_id FROM memecoin_entry_attempts ORDER BY decision_started_at DESC LIMIT 1"
    ).fetchone()
    assert attempt["origin_event_id"] == "origin-percent"


@pytest.mark.parametrize("funding_asset,expected", [("usdc", 30.0), ("sol", 40.0)])
@pytest.mark.parametrize("with_evidence", [False, True])
def test_manual_percent_deducts_completed_shadow_cash_before_equity(
        monkeypatch, funding_asset, expected, with_evidence):
    """A fill booked after the quote read must not add its cost to old cash a second time."""
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 0.0)
    conn = _conn()
    pending = m.reserve_pending(
        conn, mint="Earlier", source_user_id="u1", strategy="alpha_b", notional=100,
        reserved_at=NOW, network_id="solana", attempt_id="earlier",
        **({"funding_asset": "sol", "funding_amount_atomic": 1_000_000_000,
            "funding_sol_usd": 100, "funding_overhead_atomic": 2_000_000}
           if funding_asset == "sol" else {}))
    stamps = []

    def quote_balance():
        m.confirm_pending(conn, pending)
        return 300.0

    ctx = _context([])
    evidence = ({"pending_ids": (pending,), "extra_sol_atomic": 5_000_000}
                if with_evidence else (pending,))
    ctx.update(usdc_fn=quote_balance, slot_snapshot_debits_fn=lambda started: (
        stamps.append(started) or evidence))
    monkeypatch.setattr(m, "real_open_snapshot", lambda *a, **k: {
        "mark_notional": 100, "notional": 100, "count": 1})
    monkeypatch.setattr(runner, "real_entry_hook", lambda *a, **k: {"status": "stub"})
    before = runner.time.monotonic()
    result = runner.make_real_buy_fn(
        {"solana": ctx}, stats_fn=lambda *a, **k: {"market_cap": 100_000})(
        conn, "Next", network_id="solana", size_pct_equity=.1)
    assert result["resolved_size_usd"] == expected
    assert len(stamps) == 1 and before <= stamps[0] <= runner.time.monotonic()


def test_manual_equity_ledger_snapshot_survives_completion_between_reads(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 0.0)
    path = str(tmp_path / "manual.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    m.ensure_memecoin_tables(conn)
    other = sqlite3.connect(path)
    other.row_factory = sqlite3.Row
    pending = m.reserve_pending(
        conn, mint="Earlier", source_user_id="u1", strategy="alpha_b", notional=100,
        reserved_at=NOW, network_id="solana", attempt_id="earlier")
    original_snapshot = m.real_open_snapshot
    completed = []

    def snapshot(book, *a, **kw):
        result = original_snapshot(book, *a, **kw)
        if not completed:
            m.open_position(
                other, source_user_id="u1", source_handle="u1", mint="Earlier", ticker="E",
                entry_price=1, entry_notional=100, entry_fill_source="chain", entry_lag_s=0,
                their_size_usd=0, opened_at=NOW, execution_mode="real",
                network_id="solana", attempt_id="earlier")
            m.confirm_pending(other, pending)
            completed.append(True)
        return result

    monkeypatch.setattr(m, "real_open_snapshot", snapshot)
    monkeypatch.setattr(runner, "real_entry_hook", lambda *a, **k: {"status": "stub"})
    ctx = _context([])
    ctx["slot_snapshot_debits_fn"] = lambda started: (pending,)
    result = runner.make_real_buy_fn(
        {"solana": ctx}, stats_fn=lambda *a, **k: {"market_cap": 100_000})(
        conn, "Next", network_id="solana", size_pct_equity=.1)
    assert result["resolved_size_usd"] == 30.0
    assert not conn.in_transaction
    assert m.pending_row(conn, pending)["status"] == "confirmed"
    other.close()
    conn.close()


def test_manual_snapshot_debit_does_not_coerce_an_invalid_quote_balance(monkeypatch):
    conn = _conn()
    ctx = _context([])
    ctx.update(usdc_fn=lambda: True, slot_snapshot_debits_fn=lambda started: ())
    monkeypatch.setattr(runner, "real_entry_hook", lambda *a, **k: {"status": "stub"})
    result = runner.make_real_buy_fn(
        {"solana": ctx}, stats_fn=lambda *a, **k: {"market_cap": 100_000})(
        conn, "Next", network_id="solana", size_pct_equity=.1)
    assert result == {"status": "rejected", "reason": "invalid manual size"}


@pytest.mark.parametrize("cached", [False, True])
def test_ordinary_manual_reservation_uses_shadow_callback_after_balance_read(monkeypatch, cached):
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 0.0)
    conn = _conn()
    pending = m.reserve_pending(
        conn, mint="Earlier", source_user_id="u1", strategy="alpha_b", notional=100,
        reserved_at=NOW, network_id="solana", attempt_id="earlier")
    m.confirm_pending(conn, pending)
    spent, stamps = [], []
    ctx = _context(spent)
    ctx["usdc_fn"] = lambda: 200.0
    monkeypatch.setattr(runner, "_gate_balance_cache", {})
    monkeypatch.setattr(runner, "_gate_balance_generation", {})
    old_start = runner.time.monotonic() - 1
    if cached:
        runner._gate_balances("solana", lambda: 200, lambda: 1, now_ts=old_start)
        ctx["usdc_fn"] = lambda: None

    def debits(started):
        assert conn.in_transaction, "the history must be captured after reservation serialization"
        stamps.append(started)
        return (pending,)

    ctx["slot_snapshot_debits_fn"] = debits
    before = runner.time.monotonic()
    result = runner.real_entry_hook(
        conn, _event(signal_source="manual", their_size_usd=0), {"solana": ctx},
        now_fn=lambda: NOW, trade_size=150, max_age_s=120)
    assert result["status"] == "rejected", result
    assert spent == [] and len(stamps) == 1, result
    if cached:
        assert stamps == [old_start]
    else:
        assert before <= stamps[0] <= runner.time.monotonic()


def test_manual_nano_entry_uses_the_market_cap_impact_tier(monkeypatch):
    """A manual nano entry must not fall back to the global impact cap."""
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_IMPACT_CAP_PCT", 5.0)
    monkeypatch.setattr(cfg, "_IMPACT_CAP_TIERS", [(50_000.0, 30.0)])
    spent, impact_caps = [], []
    buy = runner.make_real_buy_fn(
        {"solana": _context(spent, impact_caps)},
        stats_fn=lambda *a, **k: {"market_cap": 12_755.92},
    )

    result = buy(_conn(), "So1NanoManual", None, network_id="solana", size_usd=8.0)

    assert result["status"] == "unknown"
    assert spent == [8.0]
    assert impact_caps == [30.0]


def test_4663_manual_percent_reduces_a_clip_that_exceeds_route_capacity(monkeypatch):
    """The runner must reduce the clip before it creates the reservation."""
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 0.0)
    captured = []
    probes = []

    def hook(*_args, **kwargs):
        captured.append(kwargs["trade_size"])
        return {"status": "unknown", "reason": "test seam"}

    def probe(_mint, size):
        probes.append(size)
        return "route_capacity" if size > 10.0 else None

    monkeypatch.setattr(runner, "real_entry_hook", hook)
    context = _context([])
    context["entry_quote_probe_fn"] = probe
    fn = runner.make_real_buy_fn(
        {"4663": context},
        stats_fn=lambda *a, **k: {"market_cap": 50_000.0},
    )

    result = fn(
        _conn(), "0x" + "2d" * 20, network_id="4663", size_pct_equity=0.13,
    )

    assert probes == [39.0, 19.5, 9.75]
    assert captured == [9.75]
    assert result["resolved_size_usd"] == 9.75


def test_manual_classification_keeps_price_for_evm_safety(monkeypatch):
    """Manual entry skips selection gates, but the EVM premium safety gate still needs price."""
    captured = {}

    def hook(*_args, **kwargs):
        captured.update(kwargs["classification_context"])
        return {"status": "rejected", "reason": "test seam"}

    monkeypatch.setattr(runner, "real_entry_hook", hook)
    fn = runner.make_real_buy_fn(
        {},
        stats_fn=lambda *_a, **_k: {
            "price": 0.0000123,
            "market_cap": 25_000.0,
        },
    )

    fn(_conn(), "0x" + "12" * 20, network_id="4663", size_usd=30.0)

    assert captured["market_stats"]["price"] == pytest.approx(0.0000123)


def test_manual_entry_does_not_apply_the_automatic_regime_scale(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 0.75)
    conn = _conn()
    res, spent = _run(
        conn,
        _event(signal_source="manual", their_size_usd=0.0),
        trade_size=20.0,
        classification_context={
            "effective_mcap": 100_000.0,
            "assumed_micro": False,
            "consensus_n_60m": None,
            "market_stats": {},
        },
    )
    assert res["status"] != "rejected", res
    assert spent == [20.0]


def test_manual_entry_freezes_the_no_automatic_exit_policy():
    conn = _conn()
    res, _spent = _run(
        conn,
        _event(signal_source="manual", their_size_usd=0.0),
        trade_size=20.0,
        classification_context={
            "effective_mcap": 100_000.0,
            "assumed_micro": False,
            "consensus_n_60m": None,
            "market_stats": {},
        },
    )

    assert res["status"] != "rejected", res
    row = conn.execute(
        "SELECT funded_contract_json FROM memecoin_entry_attempts "
        "ORDER BY decision_started_at DESC LIMIT 1"
    ).fetchone()
    assert json.loads(row["funded_contract_json"])["policy"]["policy_id"] == "manual-unmanaged-v1"


def test_manual_entry_bypasses_the_global_concurrency_cap(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 1)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 1_000.0)
    conn = _conn()
    _open_real(conn, "AutomaticMint", signal_source="follow_activity")

    res, spent = _run(
        conn, _event(signal_source="manual", their_size_usd=0.0), trade_size=20.0)

    assert res["status"] != "rejected", res
    assert spent == [20.0]


def test_manual_entry_stops_for_an_unresolved_signed_transaction():
    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint="PriorMint", source_user_id="u1", strategy="alpha_b",
        notional=20.0, reserved_at=NOW, network_id="solana",
    )
    m.set_pending_sig(conn, pending_id, "PriorSig", NOW)

    res, spent = _run(
        conn, _event(signal_source="manual", their_size_usd=0.0), trade_size=20.0)

    assert res == {
        "status": "rejected",
        "reason": "unresolved signed transaction on solana",
    }
    assert spent == []


def test_manual_position_does_not_consume_an_automatic_slot(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fixed")
    monkeypatch.setattr(cfg, "MEMECOIN_TRADE_SIZE_USD", 20.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 1)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 1_000.0)
    conn = _conn()
    _open_real(conn, "ManualMint", signal_source="manual")

    res, spent = _run(
        conn, _event(signal_source="global_feed", their_size_usd=500.0), trade_size=20.0)

    assert res["status"] != "rejected", res
    assert spent == [20.0]


def test_a_copy_still_takes_the_bankroll_clip(monkeypatch):
    """The override is manual-only: an automated signal keeps the configured sizing mode."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 1)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 1000.0)
    conn = _conn()
    res, spent = _run(conn, _event(signal_source="global_feed", their_size_usd=500.0),
                      trade_size=20.0)
    assert res["status"] != "rejected", res
    # the whole $300 balance, not the caller's $20 -- the override never reaches a copy
    assert spent == [300.0], f"a copy must take the bankroll clip, spent {spent}"


def test_size_override_faces_the_risk_gate_rather_than_bypassing_it():
    """An override larger than the venue holds is REFUSED, never clamped (reservation==spend)."""
    conn = _conn()
    ctx = risk.build_memecoin_risk_context(
        conn, usdc_balance=30.0, sol_balance=1.0, day="2026-07-15", size_override=500.0)
    assert ctx.trade_size_usd == 500.0
    decision = risk.memecoin_entry_allowed(ctx, mint="So1Mint", trade_size=ctx.trade_size_usd)
    assert not decision.allowed
    assert "bankroll" in decision.reason


def test_no_override_keeps_the_configured_sizing_mode():
    conn = _conn()
    ctx = risk.build_memecoin_risk_context(
        conn, usdc_balance=30.0, sol_balance=1.0, day="2026-07-15")
    assert ctx.trade_size_usd != 500.0


# --- 3. the trader is optional -------------------------------------------------------------

def test_manual_entry_without_a_trader_still_enters():
    """The mint alone is enough to enter."""
    conn = _conn()
    res, spent = _run(conn, _event(signal_source="manual", their_size_usd=0.0,
                                   source_user_id=None))
    assert res["status"] != "rejected", res
    assert spent, "a manual entry with no trader never reached the executor"


def test_manual_ev_normalises_a_blank_trader_to_none():
    fn = runner.make_real_buy_fn({"solana": _context([])})
    assert callable(fn)


def test_manual_entry_keeps_the_symbol_from_classification():
    conn = _conn()
    ev = _event(signal_source="manual", their_size_usd=0.0, ticker=None)

    runner.real_entry_hook(
        conn,
        ev,
        {"solana": _context([])},
        now_fn=lambda: NOW,
        trade_size=20.0,
        max_age_s=120,
        classification_context={
            "effective_mcap": 100_000.0,
            "assumed_micro": False,
            "consensus_n_60m": None,
            "market_stats": {"symbol": "NAMED", "market_cap": 100_000.0},
        },
    )

    assert ev.ticker == "NAMED"
