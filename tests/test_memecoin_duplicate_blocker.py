import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as risk
from sniper.settings import memecoin as cfg


@pytest.mark.parametrize("network", ["solana", "8453"])
@pytest.mark.parametrize("kind", ["position", "reservation"])
def test_duplicate_refusal_keeps_decision_time_blocker(monkeypatch, network, kind):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ONE_POSITION_PER_COIN", True)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    mint = "M" if network == "solana" else "0xABC"
    if kind == "position":
        blocker_id = m.open_position(
            c, source_user_id="u", source_handle="h", mint=mint, ticker="T",
            entry_price=1., entry_notional=10., entry_fill_source="chain", entry_lag_s=0.,
            their_size_usd=None, opened_at="2026-09-26T00:00:00Z",
            execution_mode="real", network_id=network, attempt_id="blocker")
    else:
        blocker_id = m.reserve_pending(
            c, mint=mint, source_user_id="u", strategy="control", notional=10.,
            reserved_at="2026-09-26T00:00:00Z", network_id=network, attempt_id="blocker")
        # An unresolved signed attempt remains excluded, just like an unsigned reservation.
        c.execute("UPDATE memecoin_pending_entries SET tx_sig='signed', "
                  "recovery_status='submitted_unknown' WHERE id=?", (blocker_id,))
        c.commit()
    if network == "solana":
        build, check, balances = risk.build_memecoin_risk_context, risk.memecoin_entry_allowed, {"sol_balance": 1.}
    else:
        from sniper.services.memecoin_evm_risk import build_evm_risk_context, evm_entry_allowed
        build, check = build_evm_risk_context, evm_entry_allowed
        balances = {"native_balance": 1., "network_id": network}
    ctx = build(c, usdc_balance=300., day="2026-09-26", **balances)
    decision = check(ctx, mint=mint, trade_size=ctx.trade_size_usd)
    assert not decision.allowed
    assert ("open funded position" if kind == "position" else
            "another entry attempt was in progress at decision time") in decision.reason
    expected = {"kind": kind, f"{kind}_id": blocker_id, "attempt_id": "blocker"}
    assert decision.duplicate_blocker == expected
    if kind == "reservation":
        m.release_pending(c, blocker_id, "chase_quote")
    result = risk.risk_refusal_result(decision)
    assert result["duplicate_blocker"] == expected
    assert json.loads(result["gate_inputs_json"])["duplicate_blocker"] == expected
    other = check(ctx, mint="OtherMint", trade_size=ctx.trade_size_usd)
    assert other.duplicate_blocker is None


def test_reservation_reader_preserves_identity_status_and_unknown_attempt():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="0xABC", source_user_id="u", strategy="control",
                            notional=10., reserved_at="t", network_id="8453")
    failed = m.reserve_pending(c, mint="0xABC", source_user_id="u", strategy="control",
                               notional=10., reserved_at="t", network_id="4663")
    m.release_pending(c, failed, "test")
    blockers = {}
    queries = []
    c.set_trace_callback(queries.append)
    held = m.pending_open_coin_ids(c, blockers=blockers)
    c.set_trace_callback(None)
    assert len(queries) == 1
    assert held == {m.coin_identity("8453", "0xABC")}
    assert blockers == {m.coin_identity("8453", "0xABC"): {
        "kind": "reservation", "reservation_id": pid, "attempt_id": None}}


def test_slot_reservation_trace_link_shares_reservation_transaction():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.start_entry_attempt(c, attempt_id="slot", decision_started_at="t")
    c.execute("BEGIN IMMEDIATE")
    pid = m.reserve_pending(c, mint="M", source_user_id="u", strategy="control",
                            notional=10., reserved_at="t", network_id="solana",
                            attempt_id="slot", signal_source="slot_lane", commit=False)
    assert c.execute("SELECT pending_id FROM memecoin_entry_attempts WHERE attempt_id='slot'").fetchone()[0] == pid
    assert c.in_transaction
    c.rollback()
    assert c.execute("SELECT pending_id FROM memecoin_entry_attempts WHERE attempt_id='slot'").fetchone()[0] is None
    assert m.pending_totals(c) == (0, 0.)


@pytest.mark.parametrize("network", ["solana", "4663"])
def test_a_duplicate_that_ate_the_bankroll_says_duplicate_not_min_floor(monkeypatch, network):
    """Live 2026-09-30 attempt 822b4ac1 (4663 DOTSLNCH): the J7 lane reserved the whole $29.62
    for the same Degen tx 0.4 s earlier, this lane sized $0 and was told "below min-trade floor".
    25 of 85 floor refusals since 09-16 were this. The duplicate check must decide first."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ONE_POSITION_PER_COIN", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")    # live mode: clip = equity/slots
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING_BY_CHAIN", {})
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    mint = "M" if network == "solana" else "0x0401324674a0a3df33f1fc23bfcb04c13fe39fbc"
    m.reserve_pending(c, mint=mint, source_user_id="u", strategy="control", notional=29.62,
                      reserved_at="2026-09-30T09:56:13Z", network_id=network,
                      attempt_id="first_lane")
    if network == "solana":
        build, check, extra = risk.build_memecoin_risk_context, risk.memecoin_entry_allowed, {
            "sol_balance": 1.}
    else:
        from sniper.services.memecoin_evm_risk import build_evm_risk_context, evm_entry_allowed
        build, check, extra = build_evm_risk_context, evm_entry_allowed, {
            "native_balance": 1., "network_id": network}
    ctx = build(c, usdc_balance=29.62, day="2026-09-30", **extra)
    assert ctx.trade_size_usd < ctx.min_trade_floor_usd        # the precondition of the bug
    decision = check(ctx, mint=mint, trade_size=ctx.trade_size_usd)
    assert not decision.allowed
    assert "below min-trade floor" not in decision.reason
    assert decision.duplicate_blocker["attempt_id"] == "first_lane"


def test_evm_floor_message_names_the_quote_asset_and_what_is_reserved(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING_BY_CHAIN", {})
    from sniper.services.memecoin_evm_risk import build_evm_risk_context, evm_entry_allowed
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.reserve_pending(c, mint="0xOTHER", source_user_id="u", strategy="control", notional=27.0,
                      reserved_at="2026-09-30T09:56:13Z", network_id="4663", attempt_id="x")
    ctx = build_evm_risk_context(c, usdc_balance=29.62, native_balance=1., day="2026-09-30",
                                 network_id="4663")
    decision = evm_entry_allowed(ctx, mint="0xNEW", trade_size=ctx.trade_size_usd)
    assert decision.reason.startswith("below min-trade floor")
    assert "WETH balance $29.62" in decision.reason and "$27.00 reserved" in decision.reason
    assert "USDC" not in decision.reason
