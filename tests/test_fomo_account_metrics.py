from sniper.analysis.fomo_account.reconstruct import reconstruct
from sniper.analysis.fomo_account.metrics import compute_metrics

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _buy(mint, usd, tok, ts):
    return {"inTokenAddress": USDC, "outTokenAddress": mint,
            "humanUsdAmountIn": str(usd), "humanUsdAmountOut": str(usd),
            "inHumanAmount": str(usd), "outHumanAmount": str(tok), "createdAt": ts,
            "isOffPlatform": "False", "networkId": 1399811149, "provider": "DFLOW",
            "platformFeeHumanAmount": "0.1", "referralFeeHumanAmount": "0"}


def _sell(mint, usd, tok, ts):
    return {"inTokenAddress": mint, "outTokenAddress": USDC,
            "humanUsdAmountIn": str(usd), "humanUsdAmountOut": str(usd),
            "inHumanAmount": str(tok), "outHumanAmount": str(usd), "createdAt": ts,
            "isOffPlatform": "False", "networkId": 1399811149, "provider": "DFLOW",
            "platformFeeHumanAmount": "0.1", "referralFeeHumanAmount": "0"}


PROFILE = {"id": "u1", "userHandle": "alice", "createdAt": "2026-01-01T00:00:00Z",
           "followers": 100, "following": 50, "numTrades": 3, "totalVolume": 1000.0,
           "swapCount": 6, "twitter": "alice", "isRestricted": False, "private": False}


def _metrics(swaps, swap_count=6):
    recon = reconstruct(swaps)
    return compute_metrics(PROFILE, swaps, recon, now_iso="2026-07-06T00:00:00Z",
                           large_buy_usd=100.0, swap_count=swap_count)


def test_power_law_winrate_low_pnl_high_win_concentration():
    M = "MINT111"
    swaps = []
    t = 1
    for _ in range(3):  # 3 small losers
        swaps += [_buy(M, 100, 1000, f"2026-07-0{t}T10:00:00Z"),
                  _sell(M, 50, 1000, f"2026-07-0{t}T11:00:00Z")]
        t += 1
    swaps += [_buy(M, 100, 1000, "2026-07-04T10:00:00Z"),
              _sell(M, 10100, 1000, "2026-07-04T11:00:00Z")]  # +10000%
    m = _metrics(swaps)
    assert m["n_closed"] == 4
    assert m["win_rate"] == 0.25
    assert m["realized_pnl_usd"] > 9000
    # one moonshot drives ALL the gains -> win concentration ~1.0; loss spread over 3 -> ~0.33..1
    assert m["top3_win_share"] is not None and m["top3_win_share"] >= 0.99
    assert "top3_pnl_share" not in m           # replaced by signed split
    assert "expectancy_pct" not in m           # dropped (duplicate of mean_pnl_pct)


def test_no_sell_trader_coverage_zero_no_crash():
    m = _metrics([_buy("MINT222", 100, 1000, "2026-07-05T10:00:00Z")], swap_count=1)
    assert m["n_closed"] == 0
    assert m["n_open_bags"] == 1
    assert m["closed_coverage"] == 0.0
    assert m["win_rate"] is None
    assert m["realized_pnl_usd"] == 0.0
    assert m["open_bag_cost_usd"] == 100.0
    assert m["hit_full_history"] == 1          # 1 analyzed >= 1 lifetime


def test_capital_and_coverage_metrics():
    M = "MINT555"
    # buy $100 -> sell $150 (closed), plus a $100 buy never sold (open bag)
    swaps = [_buy(M, 100, 1000, "2026-07-01T10:00:00Z"),
             _sell(M, 150, 1000, "2026-07-01T11:00:00Z"),
             _buy(M, 100, 1000, "2026-07-02T10:00:00Z")]
    m = _metrics(swaps, swap_count=100)   # 3 analyzed of 100 lifetime -> 3% coverage
    assert abs(m["coverage_ratio"] - 0.03) < 1e-6
    assert m["hit_full_history"] == 0
    assert abs(m["total_buy_notional"] - 200.0) < 1e-6
    assert abs(m["open_bag_cost_usd"] - 100.0) < 1e-6
    # $-weighted return = realized / (buy_notional - open_cost) = 50 / (200-100) = 50%
    assert abs(m["dollar_weighted_return_pct"] - 50.0) < 1e-6


def test_undefeated_and_profit_factor_states():
    M = "MINT666"
    swaps = [_buy(M, 100, 1000, "2026-07-01T10:00:00Z"),
             _sell(M, 150, 1000, "2026-07-01T11:00:00Z")]   # only a winner
    m = _metrics(swaps)
    assert m["undefeated"] == 1
    assert m["profit_factor"] is None    # no losses -> undefined, disambiguated by undefeated
    # no-data case
    m0 = _metrics([_buy(M, 100, 1000, "2026-07-01T10:00:00Z")], swap_count=1)
    assert m0["undefeated"] == 0
    assert m0["profit_factor"] is None


def test_dispersion_ratio_nulled_below_min_n():
    M = "MINT777"
    swaps = [_buy(M, 100, 1000, "2026-07-01T10:00:00Z"),
             _sell(M, 150, 1000, "2026-07-01T11:00:00Z")]
    m = _metrics(swaps)
    assert m["pnl_pct_dispersion_ratio"] is None   # n_closed=1 < 5
    assert "sharpe_like" not in m
    assert "realized_max_drawdown_usd" in m
    assert "max_drawdown_usd" not in m


def test_unmatched_sell_ratio_flags_truncation():
    M = "MINT888"
    # buy 500 then sell 1000 (leftover 500 unmatched) + one clean pair
    swaps = [_buy(M, 50, 500, "2026-07-01T10:00:00Z"),
             _sell(M, 150, 1000, "2026-07-01T11:00:00Z")]
    m = _metrics(swaps)
    assert m["n_unmatched_sells"] == 1
    # ratio = unmatched / (closed + unmatched) = 1 / (1+1) = 0.5
    assert abs(m["unmatched_sell_ratio"] - 0.5) < 1e-6


def test_fifo_lot_matches_are_not_presented_as_independent_exit_events():
    mint = "MINTLOTS"
    swaps = [
        _buy(mint, 50, 500, "2026-07-01T09:00:00Z"),
        _buy(mint, 50, 500, "2026-07-01T10:00:00Z"),
        _sell(mint, 150, 1000, "2026-07-01T11:00:00Z"),
    ]
    result = _metrics(swaps, swap_count=3)

    assert result["n_closed"] == 2
    assert result["matched_lots"] == 2
    assert result["distinct_exit_events"] == 1
    assert result["distinct_closed_mints"] == 1


def test_fee_drag_excludes_cash_route_volume():
    WSOL = "So11111111111111111111111111111111111111112"
    M = "MINTFEE"
    # one memecoin buy ($100 vol, $0.10 fee) + one USDC<->WSOL cash route ($10000 vol, $1 fee)
    swaps = [
        {"inTokenAddress": USDC, "outTokenAddress": M, "humanUsdAmountIn": "100",
         "humanUsdAmountOut": "100", "inHumanAmount": "100", "outHumanAmount": "1000",
         "createdAt": "2026-07-01T10:00:00Z", "isOffPlatform": "False", "networkId": 1,
         "provider": "x", "platformFeeHumanAmount": "0.1", "referralFeeHumanAmount": "0"},
        {"inTokenAddress": USDC, "outTokenAddress": WSOL, "humanUsdAmountIn": "10000",
         "humanUsdAmountOut": "10000", "inHumanAmount": "10000", "outHumanAmount": "50",
         "createdAt": "2026-07-01T11:00:00Z", "isOffPlatform": "False", "networkId": 1,
         "provider": "x", "platformFeeHumanAmount": "1.0", "referralFeeHumanAmount": "0"},
    ]
    m = _metrics(swaps, swap_count=2)
    # denominator excludes the $10000 cash-route volume: 0.1 / 100 = 0.001 (NOT 1.1/10100)
    assert abs(m["fee_drag_pct"] - 0.001) < 1e-9


def test_windowed_7d_realized_covered_excludes_older_roundtrips():
    # span oldest 06-27 .. newest 07-05 = ~8d >= 7 -> 7d COVERED. now=07-06.
    # rt closed 06-27 (>7d old, loser -50) excluded; rt closed 07-05 (<7d, +50) included.
    M = "MINTWIN"
    swaps = [_buy(M, 100, 1000, "2026-06-27T10:00:00Z"),
             _sell(M, 50, 1000, "2026-06-27T11:00:00Z"),    # rt1 -50, exit 06-27
             _buy(M, 100, 1000, "2026-07-05T10:00:00Z"),
             _sell(M, 150, 1000, "2026-07-05T11:00:00Z")]   # rt2 +50, exit 07-05
    m = _metrics(swaps, swap_count=100)   # not full history
    assert m["realized_7d_covered"] == 1
    assert abs(m["realized_pnl_7d"] - 50.0) < 1e-6          # only rt2, the last-7d one
    # window ~8d < 30 and not full history -> 30d NOT covered -> null, not a partial number
    assert m["realized_30d_covered"] == 0
    assert m["realized_pnl_30d"] is None


def test_windowed_not_covered_when_span_too_short():
    # span < 7d and not full history -> both windows null (we can't see back far enough)
    M = "MINTSHORT"
    swaps = [_buy(M, 100, 1000, "2026-07-05T10:00:00Z"),
             _sell(M, 150, 1000, "2026-07-05T11:00:00Z")]
    m = _metrics(swaps, swap_count=100)
    assert m["realized_7d_covered"] == 0 and m["realized_pnl_7d"] is None
    assert m["realized_30d_covered"] == 0 and m["realized_pnl_30d"] is None


def test_windowed_full_history_short_span_is_covered():
    # full history (2 of 2 lifetime) -> nothing older exists -> both windows COVERED
    M = "MINTFULL"
    swaps = [_buy(M, 100, 1000, "2026-07-05T10:00:00Z"),
             _sell(M, 150, 1000, "2026-07-05T11:00:00Z")]
    m = _metrics(swaps, swap_count=2)
    assert m["realized_7d_covered"] == 1 and abs(m["realized_pnl_7d"] - 50.0) < 1e-6
    assert m["realized_30d_covered"] == 1 and abs(m["realized_pnl_30d"] - 50.0) < 1e-6


def test_balances_uses_fomo_cost_basis_when_present():
    # /balances holding carries fomo's own userToken.currentCostBasisUsd (server-side,
    # full-history). value = 1000 * priceUSD 0.5 = 500; cost 80 -> unrealized +420, NOT
    # partial (fomo gave the cost). totalRealizedPnlUsd summed into fomo_realized_held_usd.
    M = "MINTBAL"
    swaps = [_buy(M, 100, 1000, "2026-07-05T10:00:00Z")]
    recon = reconstruct(swaps)
    balances = {"balances": [{"balance": {"tokenAddress": M, "shiftedBalance": 1000},
                              "tokenFilterResult": {"priceUSD": "0.5"},
                              "userToken": {"currentCostBasisUsd": 80.0,
                                            "totalRealizedPnlUsd": 250.0}}],
                "livePerpPnl": 30.0, "otherPnl": 7.0}
    m = compute_metrics(PROFILE, swaps, recon, now_iso="2026-07-06T00:00:00Z",
                        large_buy_usd=100.0, swap_count=1, balances=balances)
    assert abs(m["live_holdings_value_usd"] - 500.0) < 1e-6
    assert m["live_holdings_count"] == 1
    assert abs(m["unrealized_pnl_usd"] - 420.0) < 1e-6    # 500 mkt - 80 fomo cost
    assert m["unrealized_partial"] == 0                    # fomo supplied cost basis
    assert abs(m["fomo_realized_held_usd"] - 250.0) < 1e-6
    assert abs(m["live_perp_pnl"] - 30.0) < 1e-6 and abs(m["other_pnl"] - 7.0) < 1e-6


def test_balances_fallback_marks_open_bag_and_flags_partial():
    # NO userToken cost basis -> fall back to reconstructed open-bag cost. M is in-window
    # (cost 100), N predates the window (no cost) -> partial=1. Cash (USDC) excluded.
    M, N = "MINTINWIN", "MINTOLDER"
    swaps = [_buy(M, 100, 1000, "2026-07-05T10:00:00Z")]
    recon = reconstruct(swaps)
    balances = {"balances": [
        {"balance": {"tokenAddress": M, "shiftedBalance": 1000}, "tokenFilterResult": {"priceUSD": "0.2"}},
        {"balance": {"tokenAddress": N, "shiftedBalance": 500}, "tokenFilterResult": {"priceUSD": "1.0"}},
        {"balance": {"tokenAddress": USDC, "shiftedBalance": 999}, "tokenFilterResult": {"priceUSD": "1.0"}},
    ]}
    m = compute_metrics(PROFILE, swaps, recon, now_iso="2026-07-06T00:00:00Z",
                        large_buy_usd=100.0, swap_count=1, balances=balances)
    assert m["live_holdings_count"] == 2                       # USDC excluded
    assert abs(m["live_holdings_value_usd"] - 700.0) < 1e-6    # 200 + 500, no cash
    assert abs(m["unrealized_pnl_usd"] - 600.0) < 1e-6         # 700 value - 100 open-bag cost
    assert m["unrealized_partial"] == 1                        # N has no in-window cost basis


def test_balances_absent_leaves_fields_unset():
    m = _metrics([_buy("MZ", 100, 1000, "2026-07-05T10:00:00Z")], swap_count=1)
    assert "live_holdings_value_usd" not in m       # no balances -> field absent -> NULL on write
    assert "unrealized_pnl_usd" not in m


def test_re_entry_rate_is_fraction_of_buys_that_rebuy():
    # 3 buys of M + 1 buy of N -> 4 buys, 2 distinct mints -> re-entry rate = (4-2)/4 = 0.5.
    # (old formula was total_swaps/distinct = counted sells as re-entries and wasn't a rate.)
    swaps = [_buy("M", 100, 1000, "2026-07-01T10:00:00Z"),
             _buy("M", 100, 1000, "2026-07-01T11:00:00Z"),
             _buy("M", 100, 1000, "2026-07-01T12:00:00Z"),
             _buy("N", 100, 1000, "2026-07-01T13:00:00Z")]
    m = _metrics(swaps, swap_count=4)
    assert abs(m["re_entry_rate"] - 0.5) < 1e-9
    assert abs(m["avg_buys_per_token"] - 2.0) < 1e-9   # 4 buys / 2 mints (distinct metric)


def test_recency_and_profile_fields():
    M = "MINT333"
    swaps = [_buy(M, 200, 1000, "2026-07-05T23:00:00Z"),
             _sell(M, 250, 1000, "2026-07-05T23:30:00Z")]
    m = _metrics(swaps)
    assert m["swaps_24h"] == 2
    assert m["swaps_7d"] == 2
    assert m["large_buys_7d"] == 1
    assert m["last_swap_age_h"] is not None
    assert m["follower_ratio"] == 2.0
    assert m["has_twitter"] == 1
    assert m["profile_swap_count"] == 6
    assert "1399811149" in m["chain_mix"]
    assert "metrics_json" in m
