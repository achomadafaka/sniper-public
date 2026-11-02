from sniper.analysis.fomo_account.reconstruct import reconstruct

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MEME = "4U4U8oXwDyVXGeTffMXds4NAgBgLFwq3wNvTCRTSpump"


def _buy(mint, usd, tok, ts, off="False"):
    return {"inTokenAddress": USDC, "outTokenAddress": mint,
            "humanUsdAmountIn": str(usd), "humanUsdAmountOut": str(usd),
            "inHumanAmount": str(usd), "outHumanAmount": str(tok),
            "createdAt": ts, "isOffPlatform": off, "networkId": 1399811149, "provider": "DFLOW"}


def _sell(mint, usd, tok, ts, off="False"):
    return {"inTokenAddress": mint, "outTokenAddress": USDC,
            "humanUsdAmountIn": str(usd), "humanUsdAmountOut": str(usd),
            "inHumanAmount": str(tok), "outHumanAmount": str(usd),
            "createdAt": ts, "isOffPlatform": off, "networkId": 1399811149, "provider": "DFLOW"}


def test_clean_roundtrip():
    swaps = [_buy(MEME, 100, 1000, "2026-07-01T10:00:00Z"),
             _sell(MEME, 150, 1000, "2026-07-01T11:00:00Z")]
    r = reconstruct(swaps)
    assert len(r["roundtrips"]) == 1
    rt = r["roundtrips"][0]
    assert abs(rt["realized_usd"] - 50.0) < 1e-6
    assert abs(rt["pnl_pct"] - 50.0) < 1e-6
    assert abs(rt["hold_s"] - 3600.0) < 1e-6
    assert r["open_bags"] == []
    assert abs(r["total_buy_notional"] - 100.0) < 1e-6
    assert abs(r["open_bag_cost"] - 0.0) < 1e-6


def test_partial_sell_leaves_open_bag():
    swaps = [_buy(MEME, 100, 1000, "2026-07-01T10:00:00Z"),
             _sell(MEME, 75, 500, "2026-07-01T11:00:00Z")]
    r = reconstruct(swaps)
    assert len(r["roundtrips"]) == 1
    assert len(r["open_bags"]) == 1
    assert abs(r["open_bags"][0]["qty"] - 500.0) < 1e-6
    assert abs(r["open_bag_cost"] - 50.0) < 1e-6   # 500 tok * $0.10


def test_average_down_fifo():
    swaps = [_buy(MEME, 100, 1000, "2026-07-01T10:00:00Z"),
             _buy(MEME, 50, 1000, "2026-07-01T10:30:00Z"),
             _sell(MEME, 200, 1000, "2026-07-01T11:00:00Z")]
    r = reconstruct(swaps)
    assert len(r["roundtrips"]) == 1
    assert abs(r["roundtrips"][0]["realized_usd"] - 100.0) < 1e-6  # matched first lot @.10
    assert len(r["open_bags"]) == 1  # the .05 lot still open


def test_no_sell_trader_all_open_bags():
    swaps = [_buy(MEME, 100, 1000, "2026-07-01T10:00:00Z")]
    r = reconstruct(swaps)
    assert r["roundtrips"] == []
    assert len(r["open_bags"]) == 1
    assert r["sells"] == 0


def test_unmatched_sell_no_prior_buy():
    swaps = [_sell(MEME, 150, 1000, "2026-07-01T11:00:00Z")]
    r = reconstruct(swaps)
    assert r["roundtrips"] == []
    assert r["unmatched_sells"] == 1


def test_partial_match_leftover_counts_unmatched():
    # REGRESSION (review blocker): buy 500, sell 1000 -> 1 round-trip (500 qty)
    # AND the 500-share leftover must count as unmatched, not vanish.
    swaps = [_buy(MEME, 50, 500, "2026-07-01T10:00:00Z"),
             _sell(MEME, 150, 1000, "2026-07-01T11:00:00Z")]
    r = reconstruct(swaps)
    assert len(r["roundtrips"]) == 1
    assert abs(r["roundtrips"][0]["qty"] - 500.0) < 1e-6
    assert r["unmatched_sells"] == 1


def test_routes_and_offplatform_counted():
    swaps = [
        {"inTokenAddress": USDC, "outTokenAddress": "So11111111111111111111111111111111111111112",
         "humanUsdAmountIn": "10", "humanUsdAmountOut": "10", "inHumanAmount": "10",
         "outHumanAmount": "0.05", "createdAt": "2026-07-01T09:00:00Z", "isOffPlatform": "False"},
        _buy(MEME, 100, 1000, "2026-07-01T10:00:00Z", off="True"),
    ]
    r = reconstruct(swaps)
    assert r["routes"] == 1
    assert r["off_platform"] == 1
    assert r["total"] == 2


def test_token_route_splits_into_sell_and_buy():
    # A->B direct rotation (neither leg cash) is economically a sell of A + buy of B.
    A, B = "AAAAmint", "BBBBmint"
    swaps = [
        _buy(A, 100, 1000, "2026-07-01T10:00:00Z"),      # buy A: 1000 @ $0.10
        {"inTokenAddress": A, "outTokenAddress": B,      # rotate A->B
         "humanUsdAmountIn": "150", "humanUsdAmountOut": "150",
         "inHumanAmount": "1000", "outHumanAmount": "5000",
         "createdAt": "2026-07-01T11:00:00Z", "isOffPlatform": "False",
         "networkId": 1399811149, "provider": "DFLOW"},
    ]
    r = reconstruct(swaps)
    # A sell leg realizes (0.15 - 0.10) * 1000 = +$50 against the tracked lot
    assert len(r["roundtrips"]) == 1
    assert r["roundtrips"][0]["mint"] == A
    assert abs(r["roundtrips"][0]["realized_usd"] - 50.0) < 1e-6
    # B buy leg opens a bag: 5000 @ $0.03, cost $150
    assert len(r["open_bags"]) == 1
    assert r["open_bags"][0]["mint"] == B
    assert abs(r["open_bags"][0]["cost_usd"] - 150.0) < 1e-6
    # still flagged for transparency
    assert r["token_routes"] == 1
    assert r["routes"] == 1
    # both buy legs (A + B) counted in deployed notional
    assert abs(r["total_buy_notional"] - 250.0) < 1e-6


def test_token_route_same_mint_is_routing_noise_not_a_trade():
    # in==out (multi-hop router arbing back to the same token) must NOT manufacture PnL
    A = "AAAAmint"
    swaps = [{"inTokenAddress": A, "outTokenAddress": A,
              "humanUsdAmountIn": "100", "humanUsdAmountOut": "99",
              "inHumanAmount": "1000", "outHumanAmount": "990",
              "createdAt": "2026-07-01T10:00:00Z", "isOffPlatform": "False"}]
    r = reconstruct(swaps)
    assert r["roundtrips"] == []
    assert r["buys"] == []
    assert r["routes"] == 1          # counted as a route (transparency)
    assert r["token_routes"] == 0    # but NOT a real token rotation


def test_token_route_sell_leg_unmatched_when_in_token_off_window():
    # rotate A->B but A was never bought in-window -> A sell leg is unmatched,
    # B buy leg still opens a bag (consistent with off-platform/window truncation).
    A, B = "AAAAmint", "BBBBmint"
    swaps = [{"inTokenAddress": A, "outTokenAddress": B,
              "humanUsdAmountIn": "150", "humanUsdAmountOut": "150",
              "inHumanAmount": "1000", "outHumanAmount": "5000",
              "createdAt": "2026-07-01T11:00:00Z", "isOffPlatform": "False",
              "networkId": 1399811149, "provider": "DFLOW"}]
    r = reconstruct(swaps)
    assert r["roundtrips"] == []
    assert r["unmatched_sells"] == 1
    assert len(r["open_bags"]) == 1 and r["open_bags"][0]["mint"] == B


def test_window_days():
    swaps = [_buy(MEME, 100, 1000, "2026-07-01T00:00:00Z"),
             _sell(MEME, 150, 1000, "2026-07-03T00:00:00Z")]
    r = reconstruct(swaps)
    assert abs(r["window"]["days"] - 2.0) < 1e-6


def test_malformed_sell_is_dropped_not_booked():
    # a sell with non-positive qty/price (malformed feed) must be dropped, not create a
    # garbage round-trip — symmetric with _buy_leg's price>0 guard.
    USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    swaps = [
        {"inTokenAddress": USDC, "outTokenAddress": "M", "humanUsdAmountIn": "100",
         "humanUsdAmountOut": "100", "inHumanAmount": "100", "outHumanAmount": "1000",
         "createdAt": "2026-07-01T10:00:00Z", "isOffPlatform": "False", "networkId": 1},
        {"inTokenAddress": "M", "outTokenAddress": USDC, "humanUsdAmountIn": "0",
         "humanUsdAmountOut": "0", "inHumanAmount": "0", "outHumanAmount": "0",   # malformed sell
         "createdAt": "2026-07-01T11:00:00Z", "isOffPlatform": "False", "networkId": 1},
    ]
    r = reconstruct(swaps)
    assert r["roundtrips"] == []          # no garbage round-trip from the zero sell
    assert r["sells"] == 0                # malformed sell not counted
    assert len(r["open_bags"]) == 1       # the buy is still an open bag
