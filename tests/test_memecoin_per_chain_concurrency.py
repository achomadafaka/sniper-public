"""Per-chain concurrency: one chain's slot limit must not set another chain's clip.

2026-08-30, operator. `MEMECOIN_MAX_CONCURRENT` is global and does THREE jobs — portfolio slot
cap, per-chain slot cap, and the bankroll sizing divisor in `real_trade_size`. That coupling is
why the requested arrangement is unreachable with one knob:

    solana: 1 concurrent, clip = FULL solana equity   (stated twice; MUST NOT CHANGE)
    4663:   2 concurrent, clip = 4663 equity / 2

Raising the global to let 4663 reach 2 halves the solana clip. Measured: divisor 1 -> $35.42,
divisor 2 -> $17.71, divisor 3 -> $11.80 on the live balance.
"""
import math
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_risk as evm_risk
from sniper.services import memecoin_risk as sol_risk
from sniper.services.memecoin_risk import real_trade_size


@pytest.fixture
def cfg(monkeypatch):
    """The already-imported settings module, with the knobs monkeypatched IN PLACE.

    NEVER `importlib.reload` here. Reload rebinds the module object's attributes globally, so
    every other module holding `from sniper.settings import memecoin as cfg` sees the test's
    values — and monkeypatch cannot undo it, because it restores the ENV VAR, not the module
    state the reload baked in. Inside one xdist worker that leaks into whatever runs next: the
    first version of this file failed four tests in test_memecoin_evm_risk.py and
    test_memecoin_funded_contract_storage.py, none of them its own, and each passed alone. Same
    class as the `load_env_files()`-at-import leak on 2026-08-30, different entry point.
    `monkeypatch.setattr` against the live module gives identical coverage and genuinely reverses.
    """
    import sniper.settings.memecoin as _cfg
    return _cfg


def _configure(monkeypatch, *, global_slots=1, by_chain=None, total=None,
               sizing="bankroll", real_enabled=True, tiers_enabled=True):
    """Set the knobs on the live settings module. Reversed by monkeypatch, no reload."""
    import sniper.settings.memecoin as _cfg
    monkeypatch.setattr(_cfg, "MEMECOIN_MAX_CONCURRENT", int(global_slots), raising=False)
    monkeypatch.setattr(_cfg, "MEMECOIN_MAX_CONCURRENT_BY_CHAIN",
                        dict(by_chain or {}), raising=False)
    monkeypatch.setattr(_cfg, "MEMECOIN_MAX_CONCURRENT_TOTAL",
                        int(global_slots if total is None else total), raising=False)
    # conftest pins sizing to the canonical default ('fixed'); the funded lane runs 'bankroll',
    # and every clip assertion here is about that mode.
    monkeypatch.setattr(_cfg, "MEMECOIN_REAL_SIZING", sizing, raising=False)
    monkeypatch.setattr(_cfg, "MEMECOIN_REAL_ENABLED", bool(real_enabled), raising=False)
    monkeypatch.setattr(_cfg, "MEMECOIN_EQUITY_SLOT_TIERS_ENABLED",
                        bool(tiers_enabled), raising=False)
    monkeypatch.setattr(_cfg, "MEMECOIN_TWO_SLOT_EQUITY_USD", 100.0, raising=False)
    monkeypatch.setattr(_cfg, "MEMECOIN_THREE_SLOT_EQUITY_USD", 300.0, raising=False)
    return _cfg


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


# ------------------------------------------------------------------ unconfigured == today

def test_unconfigured_chains_fall_back_to_the_global_value(cfg):
    for chain in ("solana", "4663", "8453", "", None):
        assert cfg.max_concurrent_for(chain) == cfg.MEMECOIN_MAX_CONCURRENT


def test_the_total_cap_falls_back_to_the_global_when_unset(cfg):
    assert cfg.MEMECOIN_MAX_CONCURRENT_TOTAL == cfg.MEMECOIN_MAX_CONCURRENT


# ------------------------------------------------------------------ the operator's arrangement

def test_a_per_chain_override_does_not_move_another_chains_clip(monkeypatch):
    c = _configure(monkeypatch, global_slots=1, by_chain={"4663": 2}, total=3)
    assert c.max_concurrent_for("solana") == 1
    assert c.max_concurrent_for("4663") == 2
    sol = real_trade_size(usdc_balance=35.42, open_notional_usd=0.0,
                          max_concurrent=c.max_concurrent_for("solana"))
    rob = real_trade_size(usdc_balance=65.00, open_notional_usd=0.0,
                          max_concurrent=c.max_concurrent_for("4663"))
    assert sol == 35.42, "raising 4663 to 2 slots moved the solana clip"
    assert rob == 32.50


def test_solana_clip_equals_its_balance_EXACTLY_at_one_slot(monkeypatch):
    """At divisor 1 the clip IS the whole balance, and `evaluate_entry_risk` compares the two.
    One cent of drift refuses every signal with "insufficient bankroll" until the balance moves —
    two funded signals died that way on 2026-08-23, which is why the code floors and never rounds.
    """
    c = _configure(monkeypatch, global_slots=1, by_chain={"4663": 2})
    # Only ABOVE the min-trade floor is the clip the balance; below it the floor dominates by
    # design. Derive the cases from the live floor rather than hardcoding one — conftest pins it.
    floor = float(c.MEMECOIN_MIN_TRADE_USD)
    for bal in (floor + 0.005, floor + 5.42, floor * 3, 106.14):
        clip = real_trade_size(usdc_balance=bal, open_notional_usd=0.0,
                               max_concurrent=c.max_concurrent_for("solana"))
        assert clip <= bal, f"clip {clip} exceeds balance {bal} -> insufficient-bankroll loop"
        assert clip == math.floor(bal * 100) / 100, f"{bal} -> {clip}: not floored to the cent"


def test_the_default_divisor_is_unchanged_for_callers_that_pass_nothing(monkeypatch):
    _configure(monkeypatch, global_slots=2)
    assert real_trade_size(usdc_balance=100.0, open_notional_usd=0.0) == 50.0


def test_disabled_equity_tiers_keep_the_static_limit(monkeypatch):
    c = _configure(monkeypatch, global_slots=3, tiers_enabled=False)
    assert c.max_concurrent_for("solana", equity=50.0) == 3


@pytest.mark.parametrize("equity, slots, clip", [
    (99.99, 1, 99.99),
    (100.00, 2, 50.00),
    (299.99, 2, 149.99),
    (300.00, 3, 100.00),
])
def test_both_funded_chains_split_equity_at_100_and_300(
        monkeypatch, equity, slots, clip):
    _configure(monkeypatch, global_slots=3, by_chain={"4663": 3}, total=6)

    sol = sol_risk.build_memecoin_risk_context(
        _db(), usdc_balance=equity, sol_balance=1.0, day="2026-08-30")
    evm = evm_risk.build_evm_risk_context(
        _db(), usdc_balance=equity, native_balance=1.0,
        day="2026-08-30", network_id="4663")

    assert (sol.chain_max_concurrent, sol.base_trade_size_usd) == (slots, clip)
    assert (evm.chain_max_concurrent, evm.base_trade_size_usd) == (slots, clip)


def test_a_pending_reservation_does_not_create_false_equity(monkeypatch):
    _configure(monkeypatch, global_slots=3, by_chain={"4663": 3}, total=6)
    conn = _db()
    m.reserve_pending(
        conn, mint="0xheld", source_user_id="u", strategy="alpha_b",
        notional=45.0, reserved_at="2026-08-30T00:00:00Z", network_id="4663")

    ctx = evm_risk.build_evm_risk_context(
        conn, usdc_balance=90.0, native_balance=1.0,
        day="2026-08-30", network_id="4663")

    assert ctx.chain_max_concurrent == 1
    assert ctx.base_trade_size_usd == 45.0


# ------------------------------------------------------------------ the global cap survives

def test_per_chain_limits_are_not_a_way_past_the_portfolio_total(monkeypatch):
    """solana 1 + 4663 2 = 3 chain slots. With the total at 2, the third must still be refused.

    Mutation check: point `max_concurrent` at MEMECOIN_MAX_CONCURRENT_BY_CHAIN instead of
    MEMECOIN_MAX_CONCURRENT_TOTAL and this goes red.
    """
    # The two caps must hold DIFFERENT values or the assertion cannot tell them apart — a first
    # version of this test used 2 and 2, and the mutation that points the global cap at the
    # chain's value passed it clean.
    c = _configure(monkeypatch, global_slots=1, by_chain={"4663": 2}, total=5)
    conn = _db()
    ctx = evm_risk.build_evm_risk_context(
        conn, usdc_balance=100.0, native_balance=1.0, day="2026-08-30", network_id="4663")
    assert ctx.max_concurrent == 5, "the global cap must read its own knob, not a chain's"
    assert ctx.chain_max_concurrent == 2

    # And it must actually BIND. One solana position against a total of 1 refuses a 4663 entry
    # even though 4663's own limit of 2 is untouched.
    c = _configure(monkeypatch, global_slots=1, by_chain={"4663": 2}, total=1)
    conn2 = _db()
    m.open_position(conn2, source_user_id="u", source_handle="h", mint="SolMint", ticker="T",
                    entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                    entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-08-30T00:00:00Z",
                    strategy="alpha_b", execution_mode="real", network_id="solana")
    ctx2 = evm_risk.build_evm_risk_context(
        conn2, usdc_balance=100.0, native_balance=1.0, day="2026-08-30", network_id="4663")
    assert ctx2.chain_automatic_count == 0, "a solana position must not fill a 4663 chain slot"
    decision = evm_risk.evm_entry_allowed(ctx2, mint="0xnew", trade_size=ctx2.trade_size_usd)
    assert decision.allowed is False, "the portfolio total was bypassed by a per-chain limit"
    assert "4663" not in decision.reason, (
        "a GLOBAL refusal must not be labelled with a chain: " + decision.reason)


# ------------------------------------------------------------------ counts are chain-scoped

def test_a_position_on_one_chain_does_not_consume_another_chains_slot():
    conn = _db()
    m.open_position(conn, source_user_id="u", source_handle="h", mint="SolMint", ticker="T",
                    entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                    entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-08-30T00:00:00Z",
                    strategy="alpha_b", execution_mode="real", network_id="solana")
    assert m.automatic_funded_count(conn, network="solana") == 1
    assert m.automatic_funded_count(conn, network="4663") == 0
    assert m.automatic_funded_count(conn) == 1


def test_a_manual_buy_takes_no_automatic_slot_but_still_counts_for_gas():
    """The asymmetry is deliberate: an operator's own buy does not consume an automatic slot,
    but it DOES need gas to exit. Tidying the two counts into one would break one of them."""
    conn = _db()
    m.open_position(conn, source_user_id="u", source_handle="h", mint="0xabc", ticker="T",
                    entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                    entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-08-30T00:00:00Z",
                    strategy="alpha_b", execution_mode="real", network_id="4663",
                    signal_source="manual")
    assert m.automatic_funded_count(conn, network="4663") == 0, "manual consumed an automatic slot"
    assert m.real_open_snapshot(conn, network="4663")["count"] == 1, "gas count lost the manual bag"


# ------------------------------------------------------------------ the refusal names its chain

def test_a_chain_local_refusal_names_the_chain(monkeypatch):
    """Both caps used to write the bare string "max concurrent positions reached". Telling a
    chain-local cap from the portfolio one then needed a network_id group-by, which is how four
    4663 refusals from 2026-07-18 were read as current starvation six weeks later.

    Mutation check: drop the per-chain block from `evm_entry_allowed` and this goes red.
    """
    c = _configure(monkeypatch, global_slots=9, by_chain={"4663": 1}, total=9)
    conn = _db()
    m.open_position(conn, source_user_id="u", source_handle="h", mint="0xheld", ticker="T",
                    entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                    entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-08-30T00:00:00Z",
                    strategy="alpha_b", execution_mode="real", network_id="4663")
    ctx = evm_risk.build_evm_risk_context(
        conn, usdc_balance=100.0, native_balance=1.0, day="2026-08-30", network_id="4663")
    decision = evm_risk.evm_entry_allowed(ctx, mint="0xnew", trade_size=ctx.trade_size_usd)
    assert decision.allowed is False
    assert "4663" in decision.reason and "max concurrent" in decision.reason, decision.reason


def test_arc_explicit_slots_do_not_use_other_chains_equity_tiers(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_EQUITY_SLOT_TIERS_ENABLED', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_EQUITY_SLOT_TIERS_ENABLED_BY_CHAIN', {'5042': False}, raising=False)
    monkeypatch.setattr(cfg, 'MEMECOIN_MAX_CONCURRENT_BY_CHAIN', {'5042': 3, '8453': 3})
    assert cfg.max_concurrent_for('5042', equity=99) == 3
    assert cfg.max_concurrent_for('5042', equity=1000) == 3
    assert cfg.max_concurrent_for('8453', equity=99) == 1
