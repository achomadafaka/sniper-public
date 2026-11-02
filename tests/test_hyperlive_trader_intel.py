from sniper.hyperlive import trader_intel as TI


def _fill(coin="ETH", dir="Close Long", closedPnl=0.0, fee=0.5, crossed=True, sz=1.0, px=2000.0):
    return {"coin": coin, "dir": dir, "closedPnl": str(closedPnl), "fee": str(fee),
            "crossed": crossed, "sz": str(sz), "px": str(px)}


def test_reconstruct_ledger_nets_fees_and_flags_funding():
    fills = [_fill(closedPnl=100.0, fee=0.5), _fill(closedPnl=-40.0, fee=0.5)]
    led = TI.reconstruct_ledger(fills)
    assert led["gross_closed_pnl"] == 60.0
    assert led["total_fees"] == 1.0
    assert led["realized_pnl"] == 59.0                     # 60 gross - 1 fees
    assert led["funding_included"] is False                # honesty flag, no funding passed


def test_reconstruct_ledger_treats_negative_fee_as_rebate():
    # maker rebate: fee is negative -> it ADDS to realized
    fills = [_fill(closedPnl=10.0, fee=-2.0, crossed=False)]
    led = TI.reconstruct_ledger(fills)
    assert led["total_fees"] == -2.0
    assert led["realized_pnl"] == 12.0                     # 10 - (-2) = 12


def test_skill_metrics_profit_factor_and_concentration():
    fills = [_fill(coin="ETH", closedPnl=30.0), _fill(coin="ETH", closedPnl=30.0),
             _fill(coin="ETH", closedPnl=-20.0), _fill(coin="BTC", closedPnl=10.0)]
    m = TI.skill_metrics(fills)
    assert m["n_closes"] == 4
    assert m["win_rate"] == 0.75
    assert m["profit_factor"] == round(70.0 / 20.0, 2)     # wins 70 / losses 20
    assert 0.0 <= m["top_coin_pnl_frac"] <= 1.0


def test_archetype_flags_market_maker_from_maker_fraction():
    # almost all fills are maker (crossed False) with rebates -> market_maker, NOT copyable
    fills = [_fill(crossed=False, fee=-0.3, closedPnl=1.0) for _ in range(20)]
    arch = TI.archetype(fills)
    assert arch["archetype"] == "market_maker"
    assert arch["maker_frac"] >= 0.9


def test_archetype_flags_degen_when_one_fill_dominates_pnl():
    # 1 huge lucky hit + small noise, taker -> degen_leverage (concentration), not directional skill
    fills = [_fill(crossed=True, closedPnl=5000.0)] + [_fill(crossed=True, closedPnl=5.0) for _ in range(6)]
    arch = TI.archetype(fills)
    assert arch["archetype"] == "degen_leverage"


def test_directional_taker_with_breadth_is_copyable():
    # many taker round-trips, PF>1, no single-fill or single-coin domination -> directional, watch (phase-1)
    fills = []
    coins = ["ETH", "BTC", "SOL", "ARB", "OP"]
    for i in range(40):
        pnl = 25.0 if i % 3 else -15.0
        fills.append(_fill(coin=coins[i % 5], closedPnl=pnl, crossed=True, fee=0.4))
    v = TI.assess_trader(fills, state={})
    assert v["archetype"] == "directional"
    assert v["verdict"] == "watch"


def test_low_sample_is_rejected():
    v = TI.assess_trader([_fill(closedPnl=10.0)], state={})
    assert v["verdict"] == "low_sample"
    assert v["copyable"] is False


def test_unprofitable_directional_is_rejected():
    fills = [_fill(coin=["ETH", "BTC", "SOL"][i % 3], closedPnl=-10.0 if i % 2 else 5.0,
                   crossed=True) for i in range(30)]
    v = TI.assess_trader(fills, state={})
    assert v["verdict"] in ("unprofitable", "mixed")
    assert v["copyable"] is False


def test_copyability_sets_archetype_pass_for_directional():
    m = {"n_closes": 40, "profit_factor": 1.5, "top_coin_pnl_frac": 0.3, "single_fill_frac": 0.2}
    arch = {"archetype": "directional", "taker_frac": 0.8, "maker_frac": 0.2}
    out = TI.copyability(m, arch)
    assert out["archetype_pass"] is True
    assert out["verdict"] == "watch"          # phase-1 directional is 'watch', not 'copyable'


def test_copyability_archetype_pass_false_for_market_maker():
    m = {"n_closes": 40, "profit_factor": 1.5, "top_coin_pnl_frac": 0.3, "single_fill_frac": 0.2}
    arch = {"archetype": "market_maker", "taker_frac": 0.2, "maker_frac": 0.8}
    assert TI.copyability(m, arch)["archetype_pass"] is False


def test_phase2_proven_when_significant_positive():
    import sniper.hyperlive.trader_intel as TI
    # 32 positive closes spread across many coin×day clusters -> significant, low concentration
    by_cluster = {f"C{i}|{i}": 40.0 for i in range(32)}
    stats = {"n_closed": 32, "mean_net_bps": 40.0, "top_asset_pnl_frac": 0.1,
             "top_asset_count_frac": 0.1, "last_close_ts": 9_999_999_999_999, "by_cluster": by_cluster}
    out = TI.copyability_phase2(stats)
    assert out["phase2"] == "proven"
    assert out["clustered_t"] > TI.PHASE2_MIN_T

def test_phase2_watch_when_thin():
    import sniper.hyperlive.trader_intel as TI
    stats = {"n_closed": 5, "mean_net_bps": 40.0, "top_asset_pnl_frac": 0.1,
             "top_asset_count_frac": 0.1, "last_close_ts": 9_999_999_999_999, "by_cluster": {"A|1": 40.0}}
    assert TI.copyability_phase2(stats)["phase2"] == "watch"

def test_phase2_watch_when_over_concentrated():
    import sniper.hyperlive.trader_intel as TI
    by_cluster = {f"C{i}|{i}": 40.0 for i in range(40)}
    stats = {"n_closed": 40, "mean_net_bps": 40.0, "top_asset_pnl_frac": 0.9,   # one coin carries it
             "top_asset_count_frac": 0.2, "last_close_ts": 9_999_999_999_999, "by_cluster": by_cluster}
    assert TI.copyability_phase2(stats)["phase2"] == "watch"

def test_effective_verdict_combines():
    import sniper.hyperlive.trader_intel as TI
    by_cluster = {f"C{i}|{i}": 40.0 for i in range(32)}
    proven_stats = {"n_closed": 32, "mean_net_bps": 40.0, "top_asset_pnl_frac": 0.1,
                    "top_asset_count_frac": 0.1, "last_close_ts": 9_999_999_999_999, "by_cluster": by_cluster}
    assert TI.effective_verdict("copyable", 1, proven_stats) == "proven"
    assert TI.effective_verdict("copyable", 1, None) == "watch"
    assert TI.effective_verdict("market_maker", 0, None) == "market_maker"
