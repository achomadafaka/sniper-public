"""Venue-scoped sizing/gas plus GLOBAL funded-book caps.

Sizing (2026-07-10): clip = max(floor, venue_equity * frac) [fraction] or venue_equity / MAX_CONCURRENT
[bankroll, the 2026-07-15 prod default], where venue_equity = THAT venue's free USDC + THAT venue's
deployed cost basis (open + reserved).

Caps (2026-07-18): concurrency and exposure are global across every funded chain. Sizing equity,
pending USDC encumbrance, and exit-gas reserve remain chain-local. Duplicate state stays global but
is keyed by chain+mint."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as risk
from sniper.services import memecoin_evm_risk as evm_risk
from sniper.settings import memecoin as cfg


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open_real(c, entry_notional, mint, network_id, *, mark_at=None):
    """A HEALTHY open position: freshly marked and still worth what it cost.

    Sizing values an open position at its last FRESH executable mark since 2026-09-06, so a fixture
    with no mark is a DEAD bag worth zero, not a live one. These tests are about venue scoping and
    fractions, so the mark is stamped now and equal to entry — which is what they always meant by
    "deployed cost basis". `mark_at` lets a test say otherwise.
    """
    from sniper.util.clock import real_utc_now_iso
    pid = m.open_position(c, source_user_id="u1", source_handle="(real)", mint=mint, ticker="",
                          entry_price=0.02, entry_notional=entry_notional,
                          entry_fill_source="chain", entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="2026-07-10T00:00:00+00:00", strategy="alpha_b",
                          execution_mode="real", network_id=network_id)
    c.execute("UPDATE memecoin_positions SET last_executable_price=0.02, last_executable_at=? "
              "WHERE id=?", (mark_at or real_utc_now_iso(), pid))
    c.commit()
    return pid


def _fraction(monkeypatch, frac=0.13, floor=30.0):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fraction")
    monkeypatch.setattr(cfg, "MEMECOIN_BANKROLL_FRACTION", frac)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", floor)


# --- real_trade_size: fraction mode ---

def test_fraction_clip_is_equity_times_fraction(monkeypatch):
    _fraction(monkeypatch)
    # $390 free + $0 deployed -> 13% = $50.70
    assert risk.real_trade_size(usdc_balance=390.0, open_notional_usd=0.0) == 50.70
    # deployed counts toward equity: $339.30 free + $50.70 deployed -> same clip
    assert risk.real_trade_size(usdc_balance=339.30, open_notional_usd=50.70) == 50.70


def test_fraction_floor_binds_on_small_equity(monkeypatch):
    _fraction(monkeypatch)
    assert risk.real_trade_size(usdc_balance=100.0, open_notional_usd=0.0) == 30.0   # 13% = $13 -> floor


def test_fraction_rounds_to_cents(monkeypatch):
    _fraction(monkeypatch)
    assert risk.real_trade_size(usdc_balance=284.39, open_notional_usd=0.0) == 36.97


def test_manual_percent_size_uses_selected_chain_cost_basis():
    assert risk.manual_percent_size(
        quote_balance=340.0, open_notional_usd=50.0,
        pending_notional_usd=0.0, global_exposure_usd=50.0,
        max_total_exposure_usd=0.0, pct=0.13,
    ) == 50.70


def test_manual_percent_size_does_not_count_reserved_cash_twice():
    assert risk.manual_percent_size(
        quote_balance=340.0, open_notional_usd=50.0,
        pending_notional_usd=20.0, global_exposure_usd=70.0,
        max_total_exposure_usd=0.0, pct=0.13,
    ) == 50.70


def test_manual_percent_size_clamps_to_exposure_headroom():
    assert risk.manual_percent_size(
        quote_balance=600.0, open_notional_usd=100.0,
        pending_notional_usd=0.0, global_exposure_usd=680.0,
        max_total_exposure_usd=700.0, pct=0.26,
    ) == 20.0


def test_manual_percent_size_clamps_to_spendable_quote_cash():
    assert risk.manual_percent_size(
        quote_balance=40.0, open_notional_usd=160.0,
        pending_notional_usd=10.0, global_exposure_usd=170.0,
        max_total_exposure_usd=0.0, pct=0.26,
    ) == 30.0


@pytest.mark.parametrize("pct", [0, -0.1, 1.01, float("nan"), True])
def test_manual_percent_size_rejects_an_invalid_percent(pct):
    with pytest.raises(ValueError):
        risk.manual_percent_size(
            quote_balance=100.0, open_notional_usd=0.0,
            pending_notional_usd=0.0, global_exposure_usd=0.0,
            max_total_exposure_usd=0.0, pct=pct,
        )


def test_bankroll_mode_unchanged(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 3)
    assert risk.real_trade_size(usdc_balance=300.0, open_notional_usd=0.0) == 100.0


# --- venue scoping in the ctx builders ---

def test_solana_ctx_sizes_on_solana_deployed_only(monkeypatch):
    _fraction(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 7)
    c = _c()
    _open_real(c, 50.0, "SOLMINT", "solana")
    _open_real(c, 200.0, "0xEVMMINT", "8453")   # EVM book must NOT inflate the Solana clip
    ctx = risk.build_memecoin_risk_context(c, usdc_balance=340.0, sol_balance=1.0, day="2026-07-10")
    # solana equity = 340 free + 50 solana deployed = 390 -> clip 50.70 (not 13% of 590)
    assert ctx.trade_size_usd == 50.70
    assert ctx.effective_concurrent == 2                 # global funded slots
    assert ctx.effective_exposure_usd == 250.0           # global funded exposure
    assert ctx.chain_concurrent == 1                     # SOL gas reserve remains local
    assert ctx.chain_exposure_usd == 50.0


def test_evm_pending_does_not_encumber_solana_usdc(monkeypatch):
    _fraction(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 7)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 0.0)   # prod: cap off
    c = _c()
    m.reserve_pending(c, mint="0xPENDING", source_user_id="u1", strategy="alpha_b",
                      notional=37.0, reserved_at="2026-07-10T00:00:00+00:00", network_id="8453")
    ctx = risk.build_memecoin_risk_context(c, usdc_balance=390.0, sol_balance=1.0, day="2026-07-10")
    assert ctx.effective_pending_notional_usd == 0.0     # venue-scoped encumbrance
    assert ctx.trade_size_usd == 50.70                   # 13% of 390, EVM reservation ignored
    d = risk.memecoin_entry_allowed(ctx, mint="FRESH", trade_size=ctx.trade_size_usd)
    assert d.allowed, d.reason


def test_evm_ctx_sizes_on_its_chain_only(monkeypatch):
    _fraction(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 7)
    c = _c()
    _open_real(c, 100.0, "SOLMINT", "solana")
    _open_real(c, 36.97, "0xBASE1", "8453")
    ctx = evm_risk.build_evm_risk_context(c, usdc_balance=247.42, native_balance=1.0,
                                          day="2026-07-10", network_id="8453")
    # base equity = 247.42 free + 36.97 base deployed = 284.39 -> clip 36.97 (solana book excluded)
    assert ctx.trade_size_usd == 36.97
    assert ctx.effective_concurrent == 2
    assert ctx.effective_exposure_usd == 136.97
    assert ctx.chain_concurrent == 1
    assert ctx.chain_exposure_usd == 36.97


def _bankroll(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 7)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 0.0)   # prod default: cap off


def test_bankroll_clip_is_chain_equity_over_max_concurrent(monkeypatch):
    """Canonical prod sizing (2026-07-15): clip = VENUE_equity / MAX_CONCURRENT so each chain's
    capital is fully spread across its slots — no more laughable fixed $10."""
    _bankroll(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    c = _c()
    _open_real(c, 140.0, "0xR1", "4663")                 # 4663 already has $140 deployed
    ctx = evm_risk.build_evm_risk_context(c, usdc_balance=70.0, native_balance=1.0,
                                          day="2026-07-15", network_id="4663")
    # 4663 equity = 70 free + 140 deployed = 210 -> clip 210/7 = 30.00 (Solana book irrelevant)
    assert ctx.trade_size_usd == 30.0


def test_full_evm_book_blocks_solana_globally_without_inflating_sol_gas(monkeypatch):
    _bankroll(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    c = _c()
    for i in range(7):
        _open_real(c, 30.0, f"0xR{i}", "4663")           # 4663 book FULL (7)
    ctx = risk.build_memecoin_risk_context(c, usdc_balance=210.0, sol_balance=1.0, day="2026-07-15")
    assert ctx.effective_concurrent == 7                 # global book is full
    assert ctx.chain_concurrent == 0                     # Solana gas reserve sees no EVM exits
    d = risk.memecoin_entry_allowed(ctx, mint="SOLFRESH", trade_size=ctx.trade_size_usd)
    assert not d.allowed and "concurrent" in d.reason


def test_global_concurrency_cap_binds_on_every_chain(monkeypatch):
    _bankroll(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    c = _c()
    for i in range(7):
        _open_real(c, 30.0, f"SOL{i}", "solana")         # Solana FULL (7)
    sol = risk.build_memecoin_risk_context(c, usdc_balance=210.0, sol_balance=1.0, day="2026-07-15")
    assert sol.effective_concurrent == 7
    assert risk.memecoin_entry_allowed(sol, mint="SOL8", trade_size=sol.trade_size_usd).allowed is False
    evm = evm_risk.build_evm_risk_context(c, usdc_balance=210.0, native_balance=1.0,
                                          day="2026-07-15", network_id="4663")
    assert evm.effective_concurrent == 7
    assert evm.chain_concurrent == 0
    assert not evm_risk.evm_entry_allowed(
        evm, mint="0xFRESH", trade_size=evm.trade_size_usd).allowed


def test_global_exposure_cap_includes_other_chains(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fixed")
    monkeypatch.setattr(cfg, "MEMECOIN_TRADE_SIZE_USD", 20.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 10)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 100.0)
    c = _c()
    _open_real(c, 60.0, "SOL", "solana")
    _open_real(c, 30.0, "0xBASE", "8453")

    ctx = evm_risk.build_evm_risk_context(
        c, usdc_balance=100.0, native_balance=1.0, day="2026-07-18", network_id="4663")
    decision = evm_risk.evm_entry_allowed(
        ctx, mint="0xROBIN", trade_size=ctx.trade_size_usd)

    assert ctx.effective_exposure_usd == 90.0
    assert ctx.chain_exposure_usd == 0.0
    assert not decision.allowed and decision.reason == "total exposure limit exceeded"


def test_pending_totals_network_scoped():
    c = _c()
    m.reserve_pending(c, mint="SOLP", source_user_id="u1", strategy="alpha_b",
                      notional=50.0, reserved_at="t")
    m.reserve_pending(c, mint="0xP", source_user_id="u1", strategy="alpha_b",
                      notional=37.0, reserved_at="t", network_id="8453")
    assert m.pending_totals(c) == (2, 87.0)
    assert m.pending_totals(c, network="solana") == (1, 50.0)
    assert m.pending_totals(c, network="8453") == (1, 37.0)


# --- real_trade_size: the clip must never exceed the equity it came from (2026-08-23) ---
#
# At MEMECOIN_MAX_CONCURRENT=1 the bankroll clip IS the whole balance, so the clip and the
# bankroll check read the same number. `round()` rounds a half cent UP, which put the clip one
# cent ABOVE the balance, and evaluate_entry_risk refuses when `bankroll_usdc < proposed_size`.
# The refusal is deterministic: it repeats for every signal until the balance changes. Two live
# funded signals (Leekor, LUIGI) died at this gate on 2026-08-23 with no pending reservation and
# no open position. `scaled_funded_clip` in this same module already floors to the cent for the
# identical reason; this path was missed.

def _bankroll_slots(monkeypatch, concurrent=1, floor=5.0):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", concurrent)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", floor)


def test_bankroll_clip_never_exceeds_equity(monkeypatch):
    _bankroll_slots(monkeypatch)
    for balance in (20.006, 19.997, 20.005, 20.0051, 34.126, 12.349, 7.999):
        clip = risk.real_trade_size(usdc_balance=balance, open_notional_usd=0.0)
        assert clip <= balance, f"clip {clip} exceeds balance {balance}"


def test_bankroll_clip_never_exceeds_spendable_cash_after_open_loss(monkeypatch):
    _bankroll_slots(monkeypatch, concurrent=3)
    clip = risk.real_trade_size(
        usdc_balance=114.55, open_notional_usd=229.77, max_concurrent=3)
    assert clip == 114.55


def test_bankroll_clip_at_full_balance_passes_the_bankroll_gate(monkeypatch):
    """The whole-balance clip must survive the gate that refused it in production."""
    from sniper.risk.models import RiskContext
    from sniper.risk.policy import evaluate_entry_risk
    _bankroll_slots(monkeypatch)
    for balance in (20.006, 19.997, 34.126):
        clip = risk.real_trade_size(usdc_balance=balance, open_notional_usd=0.0)
        ctx = RiskContext(
            bankroll_usdc=balance, total_exposure_usdc=0.0, market_exposure_usdc=0.0,
            concurrent_positions=0, daily_realized_pnl_usdc=0.0,
            max_total_exposure_usdc=1e12, max_market_exposure_usdc=1e12,
            max_concurrent_positions=1, daily_loss_limit_usdc=1e12,
            paper_only=False, real_trading_enabled=True, venue="solana_memecoin")
        decision = evaluate_entry_risk(ctx, clip, condition_id="mint", execution_mode="real")
        assert decision.allowed, f"balance {balance} clip {clip}: {decision.reason}"
        assert not decision.clamped


def test_bankroll_clip_caps_at_cash_below_the_min_trade_floor(monkeypatch):
    _bankroll_slots(monkeypatch, floor=5.0)
    assert risk.real_trade_size(usdc_balance=1.234, open_notional_usd=0.0) == 1.23


def test_bankroll_clip_splits_equity_across_slots(monkeypatch):
    _bankroll_slots(monkeypatch, concurrent=4)
    # 100.00 / 4 = 25.00 exactly; flooring must not shave a cent off a clean division
    assert risk.real_trade_size(usdc_balance=100.0, open_notional_usd=0.0) == 25.0


# --- per-chain bankroll fraction / max clip (2026-09-16, Arc build) ---------------------------

def test_bankroll_fraction_param_overrides_the_global_default(monkeypatch):
    _fraction(monkeypatch, frac=0.13, floor=5.0)
    # global 13% of 80 would be 10.40; an explicit override must win
    assert risk.real_trade_size(
        usdc_balance=80.0, open_notional_usd=0.0, bankroll_fraction=0.5) == 40.0


def test_bankroll_fraction_param_default_none_keeps_the_global(monkeypatch):
    _fraction(monkeypatch, frac=0.13, floor=5.0)
    assert risk.real_trade_size(usdc_balance=80.0, open_notional_usd=0.0) == pytest.approx(10.40)


def test_bankroll_fraction_for_falls_back_to_the_global_when_unset(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_BANKROLL_FRACTION", 0.13)
    monkeypatch.setattr(cfg, "MEMECOIN_BANKROLL_FRACTION_BY_CHAIN", {})
    assert cfg.bankroll_fraction_for("5042") == pytest.approx(0.13)
    assert cfg.bankroll_fraction_for("8453") == pytest.approx(0.13)


def test_a_per_chain_bankroll_fraction_does_not_move_another_chain(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_BANKROLL_FRACTION", 0.13)
    monkeypatch.setattr(cfg, "MEMECOIN_BANKROLL_FRACTION_BY_CHAIN", {"5042": 0.5})
    assert cfg.bankroll_fraction_for("5042") == pytest.approx(0.5)
    assert cfg.bankroll_fraction_for("8453") == pytest.approx(0.13)


def test_evm_max_clip_usd_for_falls_back_to_the_global_when_unset(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN", {})
    assert cfg.evm_max_clip_usd_for("5042") == 0.0


def test_a_per_chain_max_clip_does_not_move_another_chain(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN", {"5042": 150.0})
    assert cfg.evm_max_clip_usd_for("5042") == 150.0
    assert cfg.evm_max_clip_usd_for("8453") == 0.0


def test_arc_ctx_uses_its_own_fraction_and_clip_operator_numbers(monkeypatch):
    """Pins the operator's own worked example (2026-09-16): $80 equity, 50% fraction, $150 cap,
    zero gas reserve here (constants left at 0) -> first clip is exactly half the balance."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fraction")
    monkeypatch.setattr(cfg, "MEMECOIN_BANKROLL_FRACTION_BY_CHAIN", {"5042": 0.5})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN", {"5042": 150.0})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE_BY_CHAIN", {"5042": 0.0})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_APPROVAL_NATIVE_BY_CHAIN", {"5042": 0.0})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_EXIT_NATIVE_BY_CHAIN", {"5042": 0.0})
    c = _c()
    ctx = evm_risk.build_evm_risk_context(
        c, usdc_balance=80.0, native_balance=80.0, day="2026-09-16", network_id="5042")
    assert ctx.trade_size_usd == 40.0
    # a chain WITHOUT the override is untouched
    ctx_base = evm_risk.build_evm_risk_context(
        c, usdc_balance=80.0, native_balance=1.0, day="2026-09-16", network_id="8453")
    assert ctx_base.trade_size_usd != 40.0


def test_real_sizing_for_falls_back_to_the_global_when_unset(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING_BY_CHAIN", {})
    assert cfg.real_sizing_for("5042") == "bankroll"
    assert cfg.real_sizing_for("8453") == "bankroll"


def test_a_per_chain_sizing_mode_does_not_move_another_chain(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING_BY_CHAIN", {"5042": "fraction"})
    assert cfg.real_sizing_for("5042") == "fraction"
    assert cfg.real_sizing_for("8453") == "bankroll"


def test_real_trade_size_mode_param_overrides_the_global(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_BANKROLL_FRACTION", 0.13)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 5.0)
    # global mode is bankroll, but an explicit mode="fraction" must win
    assert risk.real_trade_size(
        usdc_balance=80.0, open_notional_usd=0.0,
        mode="fraction", bankroll_fraction=0.5) == 40.0


def test_arc_sizes_as_fraction_while_the_live_default_stays_bankroll(monkeypatch):
    """Pins the operator's actual production knob: MEMECOIN_REAL_SIZING=bankroll globally, Arc
    alone running 50% of equity via the per-chain mode + fraction override. Live .env has
    MEMECOIN_EVM_MAX_CLIP_USD=150 (global) already applying to 4663/56 -- unaffected here since
    each chain's override is independent."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 3)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING_BY_CHAIN", {"5042": "fraction"})
    monkeypatch.setattr(cfg, "MEMECOIN_BANKROLL_FRACTION_BY_CHAIN", {"5042": 0.5})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN", {"5042": 150.0})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE_BY_CHAIN", {"5042": 0.0})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_APPROVAL_NATIVE_BY_CHAIN", {"5042": 0.0})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_EXIT_NATIVE_BY_CHAIN", {"5042": 0.0})
    c = _c()
    arc_ctx = evm_risk.build_evm_risk_context(
        c, usdc_balance=80.0, native_balance=80.0, day="2026-09-16", network_id="5042")
    assert arc_ctx.trade_size_usd == 40.0                 # 50% fraction, not 80/3 bankroll slots

    # 4663 stays on the GLOBAL bankroll mode, untouched by Arc's override. 80/3 = 26.67 is below
    # the conftest-pinned $30 floor, so the floor binds -- itself proof this is real bankroll
    # math, not a leaked fraction from Arc's override.
    base_ctx = evm_risk.build_evm_risk_context(
        c, usdc_balance=80.0, native_balance=1.0, day="2026-09-16", network_id="4663")
    assert base_ctx.trade_size_usd == 30.0


def test_gas_reserve_is_subtracted_before_bankroll_mode_sizes_the_clip(monkeypatch):
    """The reserve must come off the balance BEFORE 'bankroll' mode computes equity/slots, not
    only as an end clamp -- otherwise a chain with more than one slot sizes each slot off a
    balance that includes money already earmarked for gas."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 1)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE_BY_CHAIN", {"5042": 1.0})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_APPROVAL_NATIVE_BY_CHAIN", {"5042": 0.5})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_EXIT_NATIVE_BY_CHAIN", {"5042": 1.0})
    monkeypatch.setattr(cfg, "MEMECOIN_GAS_RETRY_FACTOR", 1.0)
    c = _c()
    # reserve = 1 + 0.5 + (0+1)*(1+0.5)*1 = 3.0; at 1 slot bankroll mode is equity/1 = equity
    ctx = evm_risk.build_evm_risk_context(
        c, usdc_balance=100.0, native_balance=100.0, day="2026-09-16", network_id="5042")
    assert ctx.trade_size_usd == 97.0                     # 100 - 3.0 reserve, not the full 100


def test_arc_clip_cap_binds_once_equity_passes_300(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fraction")
    monkeypatch.setattr(cfg, "MEMECOIN_BANKROLL_FRACTION_BY_CHAIN", {"5042": 0.5})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN", {"5042": 150.0})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE_BY_CHAIN", {"5042": 0.0})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_APPROVAL_NATIVE_BY_CHAIN", {"5042": 0.0})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_EXIT_NATIVE_BY_CHAIN", {"5042": 0.0})
    c = _c()
    # $320 equity * 50% = $160, which the $150 cap must clip down to exactly $150
    ctx = evm_risk.build_evm_risk_context(
        c, usdc_balance=320.0, native_balance=320.0, day="2026-09-16", network_id="5042")
    assert ctx.trade_size_usd == 150.0
