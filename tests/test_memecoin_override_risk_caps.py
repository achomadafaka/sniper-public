"""Operator override (2026-09-22, operator order, relayed by Alpha): "Enter anyway" on a refused
or failed card bypasses every selection AND most safety gates -- kept: the kill switch, and the
mechanical need for a route/quote to exist. Token safety runs and is surfaced as a warning, never
a block. A $175 hard cap applies server-side regardless of what the request names.

Mirrors tests/test_memecoin_manual_gate_bypass.py's doctrine split (SELECTION vs SAFETY), one
level further: override_risk_caps is a NEW, separate switch from plain "manual" -- it must never
be inferred from signal_source alone, only from an explicit ev.override / override=True.
"""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_arming
from sniper.services import memecoin_evm_leg as leg
from sniper.services import memecoin_real_runner as RR
from sniper.services.memecoin_evm_risk import MemecoinEvmRiskCtx, evm_entry_allowed
from sniper.services.memecoin_risk import MemecoinRiskCtx, memecoin_entry_allowed
from sniper.settings import memecoin as cfg


def _c():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


# -- Solana: memecoin_entry_allowed --------------------------------------------------------------

def _sol_ctx(**overrides):
    base = dict(
        usdc_balance=300.0, sol_balance=1.0, effective_concurrent=0,
        effective_exposure_usd=0.0, effective_pending_notional_usd=0.0,
        held_coin_ids=frozenset(), daily_tripped=False, real_enabled=True,
        kill_tripped=False, trade_size_usd=50.0, max_concurrent=3,
        max_total_exposure_usd=700.0, min_trade_floor_usd=5.0,
        gas_entry_sol=0.001, gas_exit_sol=0.001, gas_retry_factor=1.0,
    )
    base.update(overrides)
    return MemecoinRiskCtx(**base)


def test_default_override_off_leaves_every_solana_cap_enforced():
    """The baseline every other test in this file diffs against: with override_risk_caps unset
    (False), daily-loss, one-per-coin, the chain cap, and the exposure cap all still refuse -- no
    behaviour changed for a caller that never named the new parameter."""
    assert not memecoin_entry_allowed(
        _sol_ctx(daily_tripped=True), mint="M1", trade_size=50.0).allowed
    assert not memecoin_entry_allowed(
        _sol_ctx(held_coin_ids=frozenset({("solana", "M1")})), mint="M1", trade_size=50.0).allowed
    assert not memecoin_entry_allowed(
        _sol_ctx(chain_max_concurrent=1, chain_automatic_count=1), mint="M1",
        trade_size=50.0).allowed
    assert not memecoin_entry_allowed(
        _sol_ctx(max_total_exposure_usd=10.0, effective_exposure_usd=100.0), mint="M1",
        trade_size=50.0).allowed


def test_override_bypasses_solana_daily_loss_latch():
    d = memecoin_entry_allowed(
        _sol_ctx(daily_tripped=True, override_risk_caps=True), mint="M1", trade_size=50.0)
    assert d.allowed is True


def test_override_bypasses_solana_one_per_coin():
    d = memecoin_entry_allowed(
        _sol_ctx(held_coin_ids=frozenset({("solana", "M1")}), override_risk_caps=True),
        mint="M1", trade_size=50.0)
    assert d.allowed is True


def test_override_bypasses_solana_chain_slot_cap():
    d = memecoin_entry_allowed(
        _sol_ctx(chain_max_concurrent=1, chain_automatic_count=5, override_risk_caps=True),
        mint="M1", trade_size=50.0)
    assert d.allowed is True


def test_override_bypasses_solana_exposure_cap():
    d = memecoin_entry_allowed(
        _sol_ctx(max_total_exposure_usd=10.0, effective_exposure_usd=100.0,
                override_risk_caps=True),
        mint="M1", trade_size=50.0)
    assert d.allowed is True


def test_override_never_bypasses_the_solana_kill_switch():
    d = memecoin_entry_allowed(
        _sol_ctx(kill_tripped=True, override_risk_caps=True), mint="M1", trade_size=50.0)
    assert d.allowed is False
    assert "kill switch" in d.reason


def test_override_never_bypasses_real_enabled():
    d = memecoin_entry_allowed(
        _sol_ctx(real_enabled=False, override_risk_caps=True), mint="M1", trade_size=50.0)
    assert d.allowed is False


def test_override_never_bypasses_insufficient_sol_gas():
    d = memecoin_entry_allowed(
        _sol_ctx(sol_balance=0.0, override_risk_caps=True), mint="M1", trade_size=50.0)
    assert d.allowed is False
    assert "SOL" in d.reason


# -- EVM: evm_entry_allowed -----------------------------------------------------------------------

def _evm_ctx(**overrides):
    base = dict(
        usdc_balance=300.0, native_balance=1.0, effective_concurrent=0,
        effective_exposure_usd=0.0, effective_pending_notional_usd=0.0,
        held_coin_ids=frozenset(), daily_tripped=False, real_enabled=True,
        kill_tripped=False, trade_size_usd=50.0, max_concurrent=3,
        max_total_exposure_usd=700.0, min_trade_floor_usd=5.0,
        gas_entry_native=0.001, gas_approval_native=0.001, gas_exit_native=0.001,
        gas_retry_factor=1.0, network_id="56",
    )
    base.update(overrides)
    return MemecoinEvmRiskCtx(**base)


def test_default_override_off_leaves_every_evm_cap_enforced():
    assert not evm_entry_allowed(
        _evm_ctx(daily_tripped=True), mint="0xM1", trade_size=50.0).allowed
    assert not evm_entry_allowed(
        _evm_ctx(held_coin_ids=frozenset({("56", "0xM1")})), mint="0xM1", trade_size=50.0).allowed
    assert not evm_entry_allowed(
        _evm_ctx(chain_max_concurrent=1, chain_automatic_count=1), mint="0xM1",
        trade_size=50.0).allowed
    assert not evm_entry_allowed(
        _evm_ctx(max_total_exposure_usd=10.0, effective_exposure_usd=100.0), mint="0xM1",
        trade_size=50.0).allowed


def test_override_bypasses_evm_daily_loss_latch():
    d = evm_entry_allowed(
        _evm_ctx(daily_tripped=True, override_risk_caps=True), mint="0xM1", trade_size=50.0)
    assert d.allowed is True


def test_override_bypasses_evm_one_per_coin():
    d = evm_entry_allowed(
        _evm_ctx(held_coin_ids=frozenset({("56", "0xM1")}), override_risk_caps=True),
        mint="0xM1", trade_size=50.0)
    assert d.allowed is True


def test_override_bypasses_evm_chain_slot_cap():
    d = evm_entry_allowed(
        _evm_ctx(chain_max_concurrent=1, chain_automatic_count=5, override_risk_caps=True),
        mint="0xM1", trade_size=50.0)
    assert d.allowed is True


def test_override_bypasses_evm_exposure_cap():
    d = evm_entry_allowed(
        _evm_ctx(max_total_exposure_usd=10.0, effective_exposure_usd=100.0,
                override_risk_caps=True),
        mint="0xM1", trade_size=50.0)
    assert d.allowed is True


def test_override_never_bypasses_the_evm_kill_switch():
    d = evm_entry_allowed(
        _evm_ctx(kill_tripped=True, override_risk_caps=True), mint="0xM1", trade_size=50.0)
    assert d.allowed is False
    assert "kill switch" in d.reason


def test_override_never_bypasses_insufficient_native_gas():
    d = evm_entry_allowed(
        _evm_ctx(native_balance=0.0, override_risk_caps=True), mint="0xM1", trade_size=50.0)
    assert d.allowed is False
    assert "gas" in d.reason


# -- the $175 cap, enforced in make_real_buy_fn's buy_fn, server-side --------------------------

def test_manual_override_size_is_capped_at_175_regardless_of_request(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_MANUAL_OVERRIDE_MAX_USD", 175.0)
    seen = {}

    def fake_hook(conn, ev, contexts, *, now_fn, trade_size, **kwargs):
        seen["trade_size"] = trade_size
        seen["override"] = getattr(ev, "override", None)
        return {"status": "filled", "position_id": 1}

    monkeypatch.setattr(RR, "real_entry_hook", fake_hook)
    buy_fn = RR.make_real_buy_fn({})
    conn = _c()
    result = buy_fn(conn, "0xMINT", "u1", ticker="T", network_id="56",
                    size_usd=100_000.0, override=True)
    assert seen["trade_size"] == 175.0
    assert seen["override"] is True
    assert result["resolved_size_usd"] == 175.0


def test_a_size_under_the_cap_is_left_alone(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_MANUAL_OVERRIDE_MAX_USD", 175.0)
    seen = {}

    def fake_hook(conn, ev, contexts, *, now_fn, trade_size, **kwargs):
        seen["trade_size"] = trade_size
        return {"status": "filled", "position_id": 1}

    monkeypatch.setattr(RR, "real_entry_hook", fake_hook)
    buy_fn = RR.make_real_buy_fn({})
    conn = _c()
    buy_fn(conn, "0xMINT", "u1", ticker="T", network_id="56", size_usd=42.0, override=True)
    assert seen["trade_size"] == 42.0


def test_a_non_override_manual_buy_is_never_capped_by_the_override_setting(monkeypatch):
    """The cap is override-specific -- a plain manual buy (no "Enter anyway") must not be
    silently capped by a setting the operator added for a different button."""
    monkeypatch.setattr(cfg, "MEMECOIN_MANUAL_OVERRIDE_MAX_USD", 175.0)
    seen = {}

    def fake_hook(conn, ev, contexts, *, now_fn, trade_size, **kwargs):
        seen["trade_size"] = trade_size
        seen["override"] = getattr(ev, "override", None)
        return {"status": "filled", "position_id": 1}

    monkeypatch.setattr(RR, "real_entry_hook", fake_hook)
    buy_fn = RR.make_real_buy_fn({})
    conn = _c()
    buy_fn(conn, "0xMINT", "u1", ticker="T", network_id="56", size_usd=500.0)
    assert seen["trade_size"] == 500.0
    assert seen["override"] is False


# -- token safety: runs, warns, never blocks, when override is on --------------------------------

class _OverrideEv:
    def __init__(self, mint="0xtok", override=True):
        self.mint = mint
        self.source_user_id = "u1"
        self.network_id = "8453"
        self.override = override


def _evm_conn(monkeypatch, tmp_path, armed=True):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", armed)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", armed)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c, str(tmp_path / "kill")


def test_override_lets_an_unsafe_token_fill_but_carries_the_warning(monkeypatch, tmp_path):
    c, kill = _evm_conn(monkeypatch, tmp_path)
    calls = {"exec": 0}

    def execute_fn(conn, pid, mint, size, now_iso):
        calls["exec"] += 1
        return {"status": "filled", "tx_hash": "0xh", "position_id": pid}

    r = leg.evm_real_entry(
        c, _OverrideEv(override=True), route="evm:8453", day="2026-07-09", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
        token_safety_fn=lambda mint, **kw: (False, "sell simulation reverted (honeypot)"),
        execute_fn=execute_fn, kill_path=kill)
    assert r["status"] == "filled"
    assert calls["exec"] == 1
    assert r["token_safety_warning"] == "sell simulation reverted (honeypot)"


def test_without_override_the_same_unsafe_verdict_still_refuses(monkeypatch, tmp_path):
    """The exact case override changes -- proves the warning path is override-gated, not a
    general relaxation of the safety check."""
    c, kill = _evm_conn(monkeypatch, tmp_path)
    calls = {"exec": 0}

    def execute_fn(conn, pid, mint, size, now_iso):
        calls["exec"] += 1
        return {"status": "filled", "tx_hash": "0xh", "position_id": pid}

    r = leg.evm_real_entry(
        c, _OverrideEv(override=False), route="evm:8453", day="2026-07-09", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
        token_safety_fn=lambda mint, **kw: (False, "sell simulation reverted (honeypot)"),
        execute_fn=execute_fn, kill_path=kill)
    assert r["status"] == "unsafe"
    assert calls["exec"] == 0
