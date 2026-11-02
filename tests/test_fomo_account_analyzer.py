from sniper.analysis.fomo_account.analyzer import analyze_account

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
M = "MINT999"


def _page(profile, swaps):
    return profile, {"responseObject": {"swaps": swaps, "hasNextPage": False}}


def test_analyze_account_end_to_end_with_stubbed_fetch():
    profile = {"id": "u1", "userHandle": "alice", "createdAt": "2026-01-01T00:00:00Z",
               "followers": 10, "following": 5, "swapCount": 2}
    swaps = [
        {"inTokenAddress": USDC, "outTokenAddress": M, "humanUsdAmountIn": "100",
         "humanUsdAmountOut": "100", "inHumanAmount": "100", "outHumanAmount": "1000",
         "createdAt": "2026-07-01T10:00:00Z", "isOffPlatform": "False",
         "networkId": 1399811149, "provider": "DFLOW"},
        {"inTokenAddress": M, "outTokenAddress": USDC, "humanUsdAmountIn": "150",
         "humanUsdAmountOut": "150", "inHumanAmount": "1000", "outHumanAmount": "150",
         "createdAt": "2026-07-01T11:00:00Z", "isOffPlatform": "False",
         "networkId": 1399811149, "provider": "DFLOW"},
    ]

    def fetch(auth, path):
        if path == "/v2/users/u1":
            return 200, {"responseObject": profile}, "date"
        return 200, {"responseObject": {"swaps": swaps, "hasNextPage": False}}, "date"

    row = analyze_account(None, "u1", now_iso="2026-07-06T00:00:00Z",
                          large_buy_usd=100.0, fetch=fetch)
    assert row["source_user_id"] == "u1"
    assert row["n_closed"] == 1
    assert abs(row["realized_pnl_usd"] - 50.0) < 1e-6
    # 2 swaps analyzed of swapCount=2 -> full history
    assert row["hit_full_history"] == 1
    assert abs(row["coverage_ratio"] - 1.0) < 1e-6


def test_analyze_account_partial_coverage_when_swapcount_exceeds_window():
    profile = {"id": "u2", "userHandle": "bob", "swapCount": 500}
    swaps = [{"inTokenAddress": USDC, "outTokenAddress": M, "humanUsdAmountIn": "100",
              "humanUsdAmountOut": "100", "inHumanAmount": "100", "outHumanAmount": "1000",
              "createdAt": "2026-07-01T10:00:00Z", "isOffPlatform": "False",
              "networkId": 1399811149, "provider": "DFLOW"}]

    def fetch(auth, path):
        if path == "/v2/users/u2":
            return 200, {"responseObject": profile}, "d"
        return 200, {"responseObject": {"swaps": swaps}}, "d"

    row = analyze_account(None, "u2", now_iso="2026-07-06T00:00:00Z", fetch=fetch)
    assert row["hit_full_history"] == 0
    assert abs(row["coverage_ratio"] - (1 / 500)) < 1e-9
