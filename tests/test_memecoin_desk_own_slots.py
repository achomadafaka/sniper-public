"""1a (operator 2026-10-22): desk positions count only in MEMECOIN_DESK_MAX_CONCURRENT (inside the
reservation, _DESK_CAP_SQL) and are EXCLUDED from the automatic copy slot count, so a full desk book
never takes a copy slot. Exposure still counts both; the copy bankroll divisor does not move."""
import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as risk
from sniper.settings import memecoin as cfg
from test_memecoin_desk_origin import _conn, _contract, _open_position


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 3)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_TOTAL", 11)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_EQUITY_SLOT_TIERS_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EQUITY_SLOT_TIERS_ENABLED_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 700.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 5)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CHAINS", frozenset({"solana"}))
    from test_memecoin_desk_origin import BOUNDS
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RAIL_BOUNDS", cfg.parse_desk_rail_bounds(BOUNDS))
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_DAILY_LOSS_USD", 200.0)


def _book(c, desk=0, copy=0):
    for i in range(desk):
        _open_position(c, pid=100 + i, signal_source="desk", mint="DeskMint%d" % i)
    for i in range(copy):
        _open_position(c, pid=200 + i, signal_source="follow_activity", uid="u%d" % i, mint="CopyMint%d" % i)


def _ctx(c, **kw):
    return risk.build_memecoin_risk_context(c, usdc_balance=600.0, sol_balance=1.0, day="2026-10-22", **kw)


def test_open_desk_positions_take_no_copy_slot():
    c = _conn()
    _book(c, desk=5, copy=2)
    assert m.automatic_funded_count(c) == 2 and m.automatic_funded_count(c, network="solana") == 2


def test_a_reserved_desk_entry_takes_no_copy_slot():
    c = _conn()
    m.reserve_pending(c, mint="DeskPending", source_user_id=None, strategy="alpha_b", notional=25.0,
                      reserved_at="2026-10-22T10:00:00+00:00", network_id="solana", attempt_id="a1",
                      signal_source="desk", funded_contract_json=_contract(c))
    assert m.pending_totals(c, network="solana") == (1, 25.0)
    assert m.automatic_funded_count(c, network="solana") == 0
    assert m.pending_totals(c, network="solana")[0] == 1          # still a liability for exposure


def test_the_desk_cap_still_refuses_a_sixth_desk_entry():
    c = _conn()
    _book(c, desk=5, copy=0)
    assert m.reserve_pending(c, mint="Sixth", source_user_id=None, strategy="alpha_b", notional=25.0,
                             reserved_at="2026-10-22T10:00:00+00:00", network_id="solana", attempt_id="a6",
                             signal_source="desk", funded_contract_json=_contract(c)) is None


def test_copy_keeps_its_three_solana_slots_with_five_desk_open():
    c = _conn()
    _book(c, desk=5, copy=2)
    ctx = _ctx(c)
    assert ctx.chain_max_concurrent == 3 and ctx.chain_automatic_count == 2
    assert risk.memecoin_entry_allowed(ctx, mint="NewCopy", trade_size=ctx.trade_size_usd).allowed
    _open_position(c, pid=300, signal_source="follow_activity", uid="u9", mint="CopyMint9")
    full = _ctx(c)
    assert not risk.memecoin_entry_allowed(full, mint="NewCopy", trade_size=full.trade_size_usd).allowed


def test_a_desk_entry_is_not_gated_by_full_copy_slots():
    c = _conn()
    _book(c, copy=3)
    ctx = _ctx(c, copy_slots=False, size_override=25.0)
    assert risk.memecoin_entry_allowed(ctx, mint="DeskNew", trade_size=25.0).allowed


def test_exposure_700_still_binds_across_desk_and_copy(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 140.0)
    c = _conn()
    _book(c, desk=5, copy=0)                                      # $125 of desk cost basis
    ctx = _ctx(c, size_override=20.0)
    decision = risk.memecoin_entry_allowed(ctx, mint="NewCopy", trade_size=20.0)
    assert not decision.allowed and "exposure" in decision.reason


def test_the_copy_bankroll_divisor_does_not_move_with_the_desk_book():
    c0, c5 = _conn(), _conn()
    _book(c5, desk=5)
    a, b = _ctx(c0), _ctx(c5)
    assert a.chain_max_concurrent == b.chain_max_concurrent == 3


def test_a_live_desk_entry_fills_past_full_copy_slots(monkeypatch):
    """Through the runner's buy path and reserve_entry (pins copy_slots=desk is None): 3 copy
    positions fill the Solana copy slots; the desk entry still reserves and fills."""
    from test_memecoin_desk_audit_fixes import _e2e_on
    c, _iid, seen, _buy = _e2e_on(monkeypatch, pre=lambda conn: _book(conn, copy=3))
    from sniper.services import memecoin_manual_intents as _mi
    _st = _mi.read_status(c, _iid)
    assert seen["spent"], {k: _st.get(k) for k in ("status", "reason", "result", "label")}
    assert c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE signal_source='desk'").fetchone()[0] == 1
    ctx = _ctx(c)                                                  # and copy is still full
    assert not risk.memecoin_entry_allowed(ctx, mint="NewCopy", trade_size=ctx.trade_size_usd).allowed
