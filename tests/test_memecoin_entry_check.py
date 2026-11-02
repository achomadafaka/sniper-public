from sniper.services.memecoin_risk import MemecoinRiskCtx, memecoin_entry_allowed


def _ctx(**kw):
    base = dict(usdc_balance=300.0, sol_balance=1.0, effective_concurrent=0,
               effective_exposure_usd=0.0, effective_pending_notional_usd=0.0,
               held_coin_ids=frozenset(), daily_tripped=False, real_enabled=True, kill_tripped=False,
               trade_size_usd=10.0, max_concurrent=6, max_total_exposure_usd=60.0,
               min_trade_floor_usd=5.0, gas_entry_sol=0.002, gas_exit_sol=0.002, gas_retry_factor=2.0)
    base.update(kw); return MemecoinRiskCtx(**base)


def test_rejects_when_real_disabled():
    assert memecoin_entry_allowed(_ctx(real_enabled=False), mint="M", trade_size=10.0).allowed is False


def test_rejects_when_kill_tripped():
    assert memecoin_entry_allowed(_ctx(kill_tripped=True), mint="M", trade_size=10.0).allowed is False


def test_rejects_when_daily_latched():
    assert memecoin_entry_allowed(_ctx(daily_tripped=True), mint="M", trade_size=10.0).allowed is False


def test_rejects_size_not_equal_base():
    assert memecoin_entry_allowed(_ctx(), mint="M", trade_size=12.0).allowed is False


def test_rejects_below_min_floor():
    assert memecoin_entry_allowed(_ctx(trade_size_usd=4.0), mint="M", trade_size=4.0).allowed is False


def test_rejects_coin_already_held_or_reserved():
    assert memecoin_entry_allowed(
        _ctx(held_coin_ids=frozenset({("solana", "M")})), mint="M", trade_size=10.0
    ).allowed is False


def test_rejects_at_concurrency_cap():
    assert memecoin_entry_allowed(_ctx(effective_concurrent=6), mint="M", trade_size=10.0).allowed is False


def test_rejects_over_exposure_cap():
    ctx = _ctx(effective_concurrent=5, effective_exposure_usd=55.0, max_concurrent=99)
    assert memecoin_entry_allowed(ctx, mint="M", trade_size=10.0).allowed is False   # 55+10 > 60


def test_rejects_when_effective_usdc_insufficient():
    # balance $12 but $10 already reserved pending -> effective $2 < $10
    ctx = _ctx(usdc_balance=12.0, effective_pending_notional_usd=10.0,
               effective_exposure_usd=10.0, effective_concurrent=1)
    assert memecoin_entry_allowed(ctx, mint="M", trade_size=10.0).allowed is False


def test_approves_clean_entry():
    assert memecoin_entry_allowed(_ctx(), mint="M", trade_size=10.0).allowed is True
