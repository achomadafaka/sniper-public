import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as runner
from sniper.services.memecoin_holder_evidence import reconcile_feed_observations
from sniper.settings import memecoin as cfg


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "source", "source")
    m.add_wallet(conn, "holder", "holder")
    conn.execute("UPDATE memecoin_wallets SET real_approved=1")
    conn.commit()
    m.open_position(
        conn, source_user_id="source", source_handle="source", mint="MINT", ticker="T",
        entry_price=1.0, entry_notional=100.0, entry_fill_source="chain", entry_lag_s=0.0,
        their_size_usd=1000.0, opened_at="2026-07-16T00:00:00+00:00",
        strategy="alpha_b", execution_mode="real", network_id="solana",
    )
    return conn


def _positive(observed_at):
    return SimpleNamespace(
        upstream_event_id="holder-buy", raw_type="large_buy", normalized_side="buy",
        source_user_id="holder", source_handle="holder", network_id="solana", mint="MINT",
        observed_at=observed_at, source_ts="2026-07-16T00:59:00+00:00",
        raw_payload={"body": {"currentSizeUsd": 100.0}},
    )


def _run_stop(conn, calls):
    preview = SimpleNamespace(expected_usdc=40.0)
    context = {
        "preview_exit_fn": lambda _pos: preview,
        "exit_fn": lambda _conn, _pos, reason, _now: calls.append(reason)
        or {"status": "unknown"},
    }
    runner.real_exit_sweep(
        conn, {"solana": context}, price_fn=lambda _mints: {"MINT": {"price": 0.4}},
        now_fn=lambda: "2026-07-16T01:00:00+00:00",
    )


def test_fresh_consensus_logs_would_protect_but_funded_stop_still_executes(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    monkeypatch.setattr(cfg, "MEMECOIN_HOLDER_EVIDENCE_MAX_AGE_S", 120.0)
    conn = _conn()
    reconcile_feed_observations(conn, [_positive("2026-07-16T00:59:30+00:00")], {"holder"})
    calls = []

    _run_stop(conn, calls)

    assert calls == ["b_stop"]
    audit = conn.execute(
        "SELECT allowed, detail FROM memecoin_exit_trigger_audit "
        "WHERE reason='protected_hold_shadow'",
    ).fetchone()
    detail = json.loads(audit["detail"])
    assert audit["allowed"] == 1
    assert detail["would_protect"] is True
    assert detail["consensus_handles"] == ["holder"]
    assert detail["authority"] == "shadow_only"
    assert detail["funded_stop_suppressed"] is False


def test_shadow_audit_runs_after_funded_exit_submission(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    calls = []
    monkeypatch.setattr(
        runner, "_record_protected_hold_shadow",
        lambda *_args, **_kwargs: calls.append("shadow"),
    )
    conn = _conn()
    preview = SimpleNamespace(expected_usdc=40.0)
    runner.real_exit_sweep(
        conn,
        {"solana": {
            "preview_exit_fn": lambda _pos: preview,
            "exit_fn": lambda *_args: calls.append("exit") or {"status": "unknown"},
        }},
        price_fn=lambda _mints: {"MINT": {"price": 0.4}},
        now_fn=lambda: "2026-07-16T01:00:00+00:00",
    )
    assert calls == ["exit", "shadow"]


@pytest.mark.parametrize("observed_at", [None, "2026-07-16T00:57:59+00:00"])
def test_missing_or_stale_consensus_logs_no_protection_and_stop_executes(monkeypatch, observed_at):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    monkeypatch.setattr(cfg, "MEMECOIN_HOLDER_EVIDENCE_MAX_AGE_S", 120.0)
    conn = _conn()
    if observed_at:
        reconcile_feed_observations(conn, [_positive(observed_at)], {"holder"})
    calls = []

    _run_stop(conn, calls)

    assert calls == ["b_stop"]
    audit = conn.execute(
        "SELECT allowed, detail FROM memecoin_exit_trigger_audit "
        "WHERE reason='protected_hold_shadow'",
    ).fetchone()
    assert audit["allowed"] == 0
    assert json.loads(audit["detail"])["would_protect"] is False
