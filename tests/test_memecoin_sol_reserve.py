from sniper.services.memecoin_risk import MemecoinRiskCtx, memecoin_entry_allowed


def _ctx(**kw):
    base = dict(usdc_balance=300.0, sol_balance=1.0, effective_concurrent=0,
               effective_exposure_usd=0.0, effective_pending_notional_usd=0.0,
               held_coin_ids=frozenset(), daily_tripped=False, real_enabled=True, kill_tripped=False,
               trade_size_usd=10.0, max_concurrent=6, max_total_exposure_usd=60.0,
               min_trade_floor_usd=5.0, gas_entry_sol=0.002, gas_exit_sol=0.002, gas_retry_factor=2.0)
    base.update(kw); return MemecoinRiskCtx(**base)


def test_blocks_when_sol_cannot_cover_full_book_exit():
    # required = entry 0.002 + (0+1)*exit 0.002*retry 2 = 0.006 ; balance 0.005 -> blocked
    ctx = _ctx(sol_balance=0.005)
    assert memecoin_entry_allowed(ctx, mint="M", trade_size=10.0).allowed is False


def test_sol_reserve_uses_effective_N_not_confirmed_only():
    # confirmed-only N would pass, but a PENDING reservation pushes the post-entry book over.
    # effective_concurrent=2 (e.g. 0 confirmed + 2 pending). required = 0.002 + (2+1)*0.002*2 = 0.014
    ctx = _ctx(sol_balance=0.013, effective_concurrent=2, chain_concurrent=2,
               effective_exposure_usd=20.0,
               effective_pending_notional_usd=20.0)
    assert memecoin_entry_allowed(ctx, mint="M", trade_size=10.0).allowed is False


def test_sol_reserve_passes_with_headroom():
    assert memecoin_entry_allowed(_ctx(sol_balance=1.0), mint="M", trade_size=10.0).allowed is True


def test_sol_read_failure_is_not_reported_as_low_balance():
    decision = memecoin_entry_allowed(
        _ctx(sol_balance=None), mint="M", trade_size=10.0)

    assert decision.allowed is False
    assert decision.reason == "SOL balance unavailable"


def test_reserve_covers_ata_rent_not_only_fees():
    # 2026-08-25 sig 5G5w4Ynt, with the LIVE gas config (exit 0.0004). The fee-only reserve was
    # 0.002 + 1*0.0004*2 = 0.0028, so 0.00446 SOL passed the gate and the swap then died creating
    # the third token account: `Transfer: insufficient lamports 312142, need 2039280`.
    ctx = _ctx(sol_balance=0.00446, gas_exit_sol=0.0004)
    fee_only = 0.002 + (0 + 1) * 0.0004 * 2.0
    assert 0.00446 > fee_only                                       # the old gate let this through
    decision = memecoin_entry_allowed(ctx, mint="M", trade_size=10.0)
    assert decision.allowed is False
    assert "SOL" in decision.reason
