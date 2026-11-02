"""Reservation timing separates BEGIN elapsed from work without changing admission or rollback."""
import json
import math
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as risk
from sniper.services import memecoin_slot_lane as lane
from sniper.services.memecoin_market_state import classify_funded_entry
from test_memecoin_slot_lane import conn, _buy, _live_lane, _scaled_lane


NOW = "2026-09-26T20:30:00+00:00"
FIELDS = {"lock_ms", "risk_ms", "policy_ms", "insert_ms", "commit_ms"}


class _Clock:
    value = 100.0

    def perf_counter(self):
        return self.value

    def advance(self, ms):
        self.value += ms / 1000.0


class _TimedConnection:
    def __init__(self, conn, clock):
        self.conn, self.clock = conn, clock

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, *args):
        result = self.conn.execute(sql, *args)
        if sql == "BEGIN IMMEDIATE":
            self.clock.advance(2)
        return result

    def commit(self):
        self.conn.commit()
        self.clock.advance(31)


def _delay(monkeypatch, owner, name, clock, ms):
    real = getattr(owner, name)

    def timed(*args, **kwargs):
        try:
            return real(*args, **kwargs)
        finally:
            clock.advance(ms)

    monkeypatch.setattr(owner, name, timed)


@pytest.fixture
def reservation(conn, monkeypatch):
    monkeypatch.setattr(risk.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(risk.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    monkeypatch.setattr(risk.cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 0.0)
    m.start_entry_attempt(
        conn, attempt_id="reservation-timing", decision_started_at=NOW,
        network_id="solana", mint="TIMING", source_user_id="timing-wallet",
        signal_source="slot_lane")
    clock = _Clock()
    monkeypatch.setattr(risk, "time", SimpleNamespace(perf_counter=clock.perf_counter), raising=False)
    for owner, name, ms in (
        (m, "check_and_latch_daily_loss", 3), (risk, "trial_refusal", 5),
        (risk, "build_memecoin_risk_context", 11), (risk, "memecoin_entry_allowed", 13),
        (risk, "winner_reentry_refusal", 17), (risk, "prepare_funded_contract", 19),
        (risk, "coin_age_refusal", 23), (m, "reserve_pending", 29),
    ):
        _delay(monkeypatch, owner, name, clock, ms)
    classification = classify_funded_entry(
        conn, network_id="solana", mint="TIMING", decision_at=NOW, signal_source="slot_lane",
        effective_mcap=5000.0, assumed_micro=False, market_stats={},
        base_clip_usd=10.0, consensus_n_60m=None)
    params = dict(mint="TIMING", source_user_id="timing-wallet", strategy="alpha_b",
                  day="2026-09-26", now_iso=NOW, usdc_balance=400.0, sol_balance=2.0,
                  size_override=10.0, entry_tip_lamports=0, classification=classification,
                  attempt_id="reservation-timing", signal_source="slot_lane")
    return _TimedConnection(conn, clock), params


def _valid_durations(timing):
    assert set(timing) <= FIELDS
    assert all(type(value) in (int, float) and math.isfinite(value) and value >= 0
               for value in timing.values())


def test_success_reports_each_boundary_in_milliseconds(reservation):
    conn, params = reservation
    timing = {}
    decision, pid = risk.reserve_entry(conn, **params, timing=timing)
    assert decision.allowed and pid is not None
    assert timing == pytest.approx({"lock_ms": 2, "risk_ms": 19, "policy_ms": 72,
                                    "insert_ms": 29, "commit_ms": 31})
    _valid_durations(timing)
    pending = m.pending_row(conn, pid)
    assert pending["reserved_notional_usd"] == 10.0
    assert pending["funded_contract_json"]
    assert not conn.in_transaction


def test_omitting_timing_preserves_reservation_result(reservation):
    conn, params = reservation
    decision, pid = risk.reserve_entry(conn, **params)
    assert decision.allowed and pid is not None
    assert m.pending_row(conn, pid)["reserved_notional_usd"] == 10.0
    assert not conn.in_transaction


def test_policy_refusal_does_not_report_insert_or_success_commit(reservation):
    conn, params = reservation
    held = m.reserve_pending(conn, mint="TIMING", source_user_id="other", strategy="alpha_b",
                             notional=10.0, reserved_at=NOW, network_id="solana")
    timing = {}
    decision, pid = risk.reserve_entry(conn, **params, timing=timing)
    assert not decision.allowed and pid is None
    assert "one position per coin" in decision.reason
    assert timing["lock_ms"] == pytest.approx(2)
    assert timing["risk_ms"] == pytest.approx(19)
    assert "insert_ms" not in timing and "commit_ms" not in timing
    _valid_durations(timing)
    assert m.pending_row(conn, held)["status"] == "reserved"
    assert not conn.in_transaction


def test_insert_failure_still_rolls_back_and_propagates(reservation, monkeypatch):
    conn, params = reservation
    real = m.reserve_pending

    def fail_after_insert(*args, **kwargs):
        real(*args, **kwargs)
        raise sqlite3.OperationalError("injected reservation failure")

    monkeypatch.setattr(m, "reserve_pending", fail_after_insert)
    timing = {}
    with pytest.raises(sqlite3.OperationalError, match="injected reservation failure"):
        risk.reserve_entry(conn, **params, timing=timing)
    assert "commit_ms" not in timing
    _valid_durations(timing)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0
    assert not conn.in_transaction


def test_validation_refusal_has_no_transaction_timing(reservation):
    conn, params = reservation
    timing = {}
    decision, pid = risk.reserve_entry(conn, **{**params, "entry_tip_lamports": -1}, timing=timing)
    assert not decision.allowed and pid is None
    assert timing == {}
    assert not conn.in_transaction


@pytest.mark.parametrize("fallback", [False, True])
def test_slot_trace_keeps_each_reservation_attempt_distinct(conn, tmp_path, monkeypatch, fallback):
    ln, _ = _live_lane(conn, tmp_path, monkeypatch)
    ln.sol_buffer_cap_usd = 175.0
    captured = []
    real = risk.reserve_entry

    def reserve(*args, **kwargs):
        timing = kwargs.get("timing")
        captured.append(timing)
        if fallback and len(captured) == 1:
            assert kwargs["funding_asset"] == "sol"
            if timing is not None:
                timing.update(lock_ms=1.0, risk_ms=2.0, policy_ms=3.0)
            return risk._reject("SOL buffer insufficient"), None
        if fallback:
            assert kwargs.get("funding_asset", "usdc") == "usdc"
        return real(*args, **kwargs)

    def build(buy, clip, snap, *, funding_lamports=None):
        detail = ({"funding_asset": "sol", "funding_amount_atomic": funding_lamports,
                   "target_mint": buy.mint} if funding_lamports is not None else {})
        return "MESSAGE", detail

    monkeypatch.setattr(risk, "reserve_entry", reserve)
    ln.build_fn = build
    result = ln.handle(_buy())
    assert result["status"] == "filled", result
    row = conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()
    event = next(e for e in json.loads(row[0])["substage_events"]
                 if e["name"] == "slot_reservation")
    assert len(captured) == (2 if fallback else 1)
    assert all(isinstance(timing, dict) for timing in captured)
    assert event["attempts"] == captured
    assert set(captured[-1]) == FIELDS
    for timing in captured:
        _valid_durations(timing)
    if fallback:
        assert captured[0] is not captured[1]
        assert captured[0] == {"lock_ms": 1.0, "risk_ms": 2.0, "policy_ms": 3.0}


@pytest.mark.parametrize("kind", ["trial", "scaled"])
def test_sol_short_early_return_keeps_reservation_timing(conn, tmp_path, monkeypatch, kind):
    if kind == "trial":
        from sniper.services import memecoin_trial as trials
        ln, sent = _live_lane(conn, tmp_path, monkeypatch)
        m.start_trial(conn, source_user_id="w1", chain="solana", clip_usd=20.0,
                      rule=trials.RULE, actor="test", now_iso=NOW)
    else:
        ln, sent, _ = _scaled_lane(conn, tmp_path, monkeypatch, leg_scale=1.0, coin_scale=0.75)
        monkeypatch.setattr(lane.cfg, "MEMECOIN_SLOT_LANE_SCALED_SOL", True)
    ln.sol_buffer_cap_usd = 175.0
    attempts = []

    def sol_short(*args, **kwargs):
        assert kwargs["funding_asset"] == "sol"
        timing = kwargs["timing"]
        timing.update(lock_ms=1.0, risk_ms=2.0)
        attempts.append(timing)
        return risk._reject("SOL buffer insufficient"), None

    monkeypatch.setattr(risk, "reserve_entry", sol_short)
    result = ln.handle(_buy())
    assert result["status"] == "rejected" and not sent
    if kind == "trial":
        assert result["reason"].startswith(trials.TRIAL_UNMARKED)
    else:
        assert result["reason"] == lane.SCALED_CLIP_REASON
    assert len(attempts) == 1
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0
    row = conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()
    trace = json.loads(row[0])
    event = next(e for e in trace["substage_events"] if e["name"] == "slot_reservation")
    assert event["attempts"] == [{"lock_ms": 1.0, "risk_ms": 2.0}]
    assert "reservation" in trace["stages"]
