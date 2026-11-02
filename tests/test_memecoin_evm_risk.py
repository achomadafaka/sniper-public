"""EVM risk venue: global funded caps with chain-local bankroll and native-gas reserve."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_risk as R
from sniper.settings import memecoin as cfg


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _armed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)


def _open_real(c, mint, network, realized=None, uid="u"):
    pid = m.open_position(c, source_user_id=uid, source_handle="h", mint=mint, ticker="T",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-09T00:00:00Z",
                          strategy="alpha_b", execution_mode="real", network_id=network)
    if realized is not None:
        m.close_position(c, pid, "2026-07-09T00:05:00Z", realized_pnl=realized)
    return pid


def _ctx(c, *, usdc=300.0, native=1.0, day="2026-07-09", network_id="8453"):
    return R.build_evm_risk_context(
        c, usdc_balance=usdc, native_balance=native, day=day, network_id=network_id
    )


def test_disarmed_rejects(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", False)
    d = R.evm_entry_allowed(_ctx(_c()), mint="0xabc", trade_size=cfg.MEMECOIN_TRADE_SIZE_USD)
    assert not d.allowed


def test_happy_path_allowed(monkeypatch):
    _armed(monkeypatch)
    d = R.evm_entry_allowed(_ctx(_c()), mint="0xabc", trade_size=cfg.MEMECOIN_TRADE_SIZE_USD)
    assert d.allowed


@pytest.mark.parametrize("mint", [
    "0x31fbb624efa7d6f6a3f7b1eb0dfc5cccbb211d10",
    "0x4030a670b27fcd711f304f3405908a785c3b848b",
])
@pytest.mark.parametrize("override", [False, True])
def test_verified_liquidity_removal_blocks_reentry_even_with_risk_override(monkeypatch, mint, override):
    from dataclasses import replace
    _armed(monkeypatch)
    ctx = replace(_ctx(_c(), network_id="56"), override_risk_caps=override)
    decision = R.evm_entry_allowed(ctx, mint=mint.upper(), trade_size=ctx.trade_size_usd)
    assert not decision.allowed
    assert decision.reason == "blocked_token: verified liquidity removal"


def test_verified_bad_token_block_is_chain_scoped(monkeypatch):
    _armed(monkeypatch)
    ctx = _ctx(_c(), network_id="8453")
    decision = R.evm_entry_allowed(
        ctx, mint="0x31fbb624efa7d6f6a3f7b1eb0dfc5cccbb211d10", trade_size=ctx.trade_size_usd)
    assert decision.allowed


def test_insufficient_native_gas_rejects(monkeypatch):
    _armed(monkeypatch)
    d = R.evm_entry_allowed(_ctx(_c(), native=0.0), mint="0xabc",
                            trade_size=cfg.MEMECOIN_TRADE_SIZE_USD)
    assert not d.allowed and "gas" in d.reason.lower()


def test_native_balance_read_failed_rejects_with_honest_reason(monkeypatch):
    # None = the balance READ failed (RPC), not a low balance. Still refuse (fail-closed), but the
    # reason must say the read failed — 2026-07-13/14 chain-4663 refusals blamed "insufficient gas"
    # while the on-chain balance actually covered the reserve.
    _armed(monkeypatch)
    d = R.evm_entry_allowed(_ctx(_c(), native=None), mint="0xabc",
                            trade_size=cfg.MEMECOIN_TRADE_SIZE_USD)
    assert not d.allowed
    assert "read failed" in d.reason.lower()
    assert "insufficient" not in d.reason.lower()


def test_insufficient_gas_reason_includes_gate_inputs(monkeypatch):
    # the refusal must be diagnosable from the log line alone: have / need / open+pending counts
    _armed(monkeypatch)
    c = _c()
    _open_real(c, "0xh0", "8453")
    d = R.evm_entry_allowed(_ctx(c, native=0.0001), mint="0xnew",
                            trade_size=cfg.MEMECOIN_TRADE_SIZE_USD)
    assert not d.allowed and "gas" in d.reason.lower()
    assert "have=0.000100" in d.reason
    assert "need=" in d.reason
    assert "concurrent=1" in d.reason and "pending=0" in d.reason


def test_native_reserve_includes_approval_and_full_book_exit(monkeypatch):
    _armed(monkeypatch)
    c = _c()
    for i in range(3):                                   # 3 open real positions already
        _open_real(c, f"0xh{i}", "8453")
    ctx = _ctx(c, native=0.001)
    # required = entry + approval + (3+1)*(exit + approval)*retry — 0.001 is well under that
    d = R.evm_entry_allowed(ctx, mint="0xnew", trade_size=cfg.MEMECOIN_TRADE_SIZE_USD)
    assert not d.allowed and "gas" in d.reason.lower()


def test_required_native_gas_uses_the_active_config(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE", 0.00005)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_APPROVAL_NATIVE", 0.00002)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_EXIT_NATIVE", 0.00005)
    monkeypatch.setattr(cfg, "MEMECOIN_GAS_RETRY_FACTOR", 2.0)

    assert R.required_native_gas(_ctx(_c())) == pytest.approx(0.00021)


def test_gas_constants_fall_back_to_the_global_when_no_chain_override(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE", 0.0007)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE_BY_CHAIN", {})
    assert cfg.gas_entry_native_for("5042") == pytest.approx(0.0007)
    assert cfg.gas_entry_native_for("8453") == pytest.approx(0.0007)


def test_a_per_chain_gas_override_does_not_move_another_chains_reserve(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE", 0.0005)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE_BY_CHAIN", {"5042": 0.01})
    assert cfg.gas_entry_native_for("5042") == pytest.approx(0.01)
    assert cfg.gas_entry_native_for("8453") == pytest.approx(0.0005)


def test_native_is_quote_reserve_is_inert_on_non_arc_chains(monkeypatch):
    # a chain where native != quote sizes exactly as today: the reserve is 0 and never touches the
    # clip, byte-for-byte, even when the gas constants are large enough that a reserve WOULD bite.
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE", 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_APPROVAL_NATIVE", 0.5)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_EXIT_NATIVE", 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_GAS_RETRY_FACTOR", 1.0)
    ctx = _ctx(_c(), usdc=12.0, native=12.0, network_id="8453")
    assert ctx.trade_size_usd == pytest.approx(cfg.MEMECOIN_TRADE_SIZE_USD)
    assert ctx.native_quote_gas_reserve_usd == 0.0
    assert ctx.native_quote_gas_deficit_usd == 0.0


def test_native_is_quote_reserve_shrinks_the_arc_clip(monkeypatch):
    # Arc: native gas IS the USDC quote/settlement asset, so the clip must set aside the same
    # reserve the exit gate already requires, not spend the whole balance sizing the entry.
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE", 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_APPROVAL_NATIVE", 0.5)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_EXIT_NATIVE", 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_GAS_RETRY_FACTOR", 1.0)
    # reserve = entry(1) + approval(0.5) + (0+1)*(exit(1)+approval(0.5))*retry(1) = 3.0
    ctx = _ctx(_c(), usdc=12.0, native=12.0, network_id="5042")
    assert ctx.native_quote_gas_reserve_usd == pytest.approx(3.0)
    assert ctx.trade_size_usd == pytest.approx(9.0)   # $10 fixed clip, shrunk by the $3 reserve
    assert ctx.native_quote_gas_deficit_usd == 0.0


def test_arc_balance_below_the_gas_reserve_refuses_honestly(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE", 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_APPROVAL_NATIVE", 0.5)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_EXIT_NATIVE", 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_GAS_RETRY_FACTOR", 1.0)
    ctx = _ctx(_c(), usdc=2.0, native=2.0, network_id="5042")   # reserve=3.0, balance=2.0: $1 short
    assert ctx.trade_size_usd == 0.0
    assert ctx.native_quote_gas_deficit_usd == pytest.approx(1.0)
    d = R.evm_entry_allowed(ctx, mint="0xarc", trade_size=ctx.trade_size_usd)
    assert not d.allowed
    assert "reserve" in d.reason.lower()
    assert "below min-trade floor" not in d.reason.lower()


def test_coin_dedup_blocks_held_mint(monkeypatch):
    _armed(monkeypatch)
    c = _c()
    _open_real(c, "0xdup", "8453")
    d = R.evm_entry_allowed(_ctx(c), mint="0xdup", trade_size=cfg.MEMECOIN_TRADE_SIZE_USD)
    assert not d.allowed and "coin" in d.reason.lower()


def test_solana_concurrent_cap_blocks_base_globally(monkeypatch):
    _armed(monkeypatch)
    c = _c()
    # Fill the global funded book on Solana. Base keeps local gas/equity but has no global slot.
    for i in range(cfg.MEMECOIN_MAX_CONCURRENT):
        _open_real(c, f"So1{i}", "solana")
    d = R.evm_entry_allowed(_ctx(c), mint="0xevm", trade_size=cfg.MEMECOIN_TRADE_SIZE_USD)
    assert not d.allowed and "concurrent" in d.reason
    assert _ctx(c).chain_concurrent == 0


def test_manual_evm_entry_bypasses_the_global_concurrency_cap(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 1)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 1_000.0)
    c = _c()
    _open_real(c, "So1Automatic", "solana")

    decision, pending_id = R.reserve_entry_evm(
        c,
        mint="0xmanual",
        source_user_id="u",
        strategy="alpha_b",
        network_id="8453",
        day="2026-07-09",
        usdc_balance=300.0,
        native_balance=1.0,
        now_iso="2026-07-09T00:06:00Z",
        signal_source="manual",
        enforce_concurrency=False,
    )

    assert decision.allowed
    assert pending_id is not None


def test_global_daily_breaker_shared_with_solana(monkeypatch):
    _armed(monkeypatch)
    c = _c()
    # a Solana real loss beyond the day limit must trip the breaker for the EVM leg too
    _open_real(c, "So1loss", "solana", realized=-(cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD + 1))
    decision, pid = R.reserve_entry_evm(
        c, mint="0xevm", source_user_id="u", strategy="alpha_b", network_id="8453",
        day="2026-07-09", usdc_balance=300.0, native_balance=1.0, now_iso="2026-07-09T00:06:00Z")
    assert not decision.allowed and pid is None


def test_reserve_evm_writes_network_id(monkeypatch):
    _armed(monkeypatch)
    c = _c()
    decision, pid = R.reserve_entry_evm(
        c, mint="0xevm", source_user_id="u", strategy="alpha_b", network_id="8453",
        day="2026-07-09", usdc_balance=300.0, native_balance=1.0, now_iso="2026-07-09T00:00:00Z")
    assert decision.allowed and pid is not None
    row = m.pending_row(c, pid)
    assert row["network_id"] == "8453" and row["status"] == "reserved"


def test_manual_evm_reservation_uses_the_requested_size(monkeypatch):
    _armed(monkeypatch)
    c = _c()
    decision, pid = R.reserve_entry_evm(
        c, mint="0xmanualsize", source_user_id=None, strategy="alpha_b", network_id="8453",
        day="2026-07-09", usdc_balance=300.0, native_balance=1.0,
        now_iso="2026-07-09T00:00:00Z", size_override=42.0,
        signal_source="manual", enforce_concurrency=False,
    )

    assert decision.allowed and decision.approved_size_usdc == 42.0
    assert m.pending_row(c, pid)["reserved_notional_usd"] == 42.0


def test_arc_retains_gas_floor_before_fraction_sizing(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_GAS_RESERVE_MIN_NATIVE_BY_CHAIN', {'5042': 10.0}, raising=False)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SIZING_BY_CHAIN', {'5042': 'fraction'})
    monkeypatch.setattr(cfg, 'MEMECOIN_BANKROLL_FRACTION_BY_CHAIN', {'5042': 0.5})
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN', {'5042': 150.0})
    ctx = _ctx(_c(), usdc=110, native=110, network_id='5042')
    assert ctx.native_quote_gas_reserve_usd == pytest.approx(10)
    assert ctx.trade_size_usd == pytest.approx(50)
    assert R.required_native_gas(ctx) == pytest.approx(10)
    capped = _ctx(_c(), usdc=1010, native=1010, network_id='5042')
    assert capped.trade_size_usd == pytest.approx(150)


def test_arc_gas_floor_cannot_be_spent_by_an_entry(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_GAS_RESERVE_MIN_NATIVE_BY_CHAIN', {'5042': 10.0}, raising=False)
    ctx = _ctx(_c(), usdc=9, native=9, network_id='5042')
    assert ctx.trade_size_usd == 0
    assert ctx.native_quote_gas_deficit_usd == pytest.approx(1)
    assert not R.evm_entry_allowed(ctx, mint='0xarc', trade_size=ctx.trade_size_usd).allowed
    other = _ctx(_c(), usdc=100, native=1, network_id='8453')
    assert other.native_quote_gas_reserve_usd == 0
    assert R.required_native_gas(other) < 1


@pytest.mark.parametrize("cap, allowed", [(float("nan"), False), (float("inf"), False), (0.0, False),
                                          (-1.0, False), ("x", False), (None, True), (50.0, True)])
def test_an_unusable_caller_cap_refuses_even_with_wallet_caps_off(monkeypatch, cap, allowed):
    """Alpha 2026-10-07 (the risk NaN cap): entry_cap_usd refuses a NaN or <= 0 cap only while
    MEMECOIN_REAL_WALLET_CAPS_ENABLED is on; off, the value went through and build_evm_risk_context
    ignored it (NaN > 0 is False), so the entry was uncapped. reserve_entry already refused it up
    front on Solana; EVM now runs the same check (float, finite, > 0). None stays "no cap"."""
    _armed(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_WALLET_CAPS_ENABLED", False)
    c = _c()
    decision, pid = R.reserve_entry_evm(
        c, mint="0xcap", source_user_id="u", strategy="alpha_b", network_id="8453",
        day="2026-07-09", usdc_balance=300.0, native_balance=1.0, now_iso="2026-07-09T00:00:00Z",
        size_cap_usd=cap)
    assert decision.allowed is allowed
    if allowed:
        assert pid is not None
    else:
        assert pid is None and decision.reason == "entry size cap unavailable"
        assert c.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0
