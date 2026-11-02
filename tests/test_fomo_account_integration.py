import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.analysis.fomo_account.analyzer import analyze_account
from sniper.analysis.fomo_account.reconstruct import reconstruct
from sniper.analysis.fomo_account.metrics import compute_metrics

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MEME = "MINTINT"


def test_full_pipeline_persists_a_rankable_row():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)

    profile = {"id": "u1", "userHandle": "alice", "createdAt": "2026-01-01T00:00:00Z",
               "followers": 10, "following": 5, "swapCount": 2}
    page = {"responseObject": {"swaps": [
        {"id": "s1", "inTokenAddress": USDC, "outTokenAddress": MEME, "humanUsdAmountIn": "100",
         "humanUsdAmountOut": "100", "inHumanAmount": "100", "outHumanAmount": "1000",
         "createdAt": "2026-07-01T10:00:00Z", "isOffPlatform": "False",
         "networkId": 1399811149, "provider": "DFLOW"},
        {"id": "s2", "inTokenAddress": MEME, "outTokenAddress": USDC, "humanUsdAmountIn": "150",
         "humanUsdAmountOut": "150", "inHumanAmount": "1000", "outHumanAmount": "150",
         "createdAt": "2026-07-01T11:00:00Z", "isOffPlatform": "False",
         "networkId": 1399811149, "provider": "DFLOW"},
    ], "hasNextPage": False}}

    def fetch(auth, path):
        return (200, {"responseObject": profile}, "d") if path == "/v2/users/u1" \
            else (200, page, "d")

    row = analyze_account(None, "u1", now_iso="2026-07-06T00:00:00Z", fetch=fetch)
    m.upsert_trader_analytics(c, row)
    listed = m.list_trader_analytics(c)
    assert len(listed) == 1
    assert listed[0]["realized_pnl_usd"] == 50.0
    assert listed[0]["win_rate"] == 1.0


def test_every_metric_key_is_a_persisted_column():
    # guards against metrics.py adding a key the table can't store (silent data loss)
    USDC_ = USDC
    swaps = [{"id": "a", "inTokenAddress": USDC_, "outTokenAddress": MEME,
              "humanUsdAmountIn": "100", "humanUsdAmountOut": "100", "inHumanAmount": "100",
              "outHumanAmount": "1000", "createdAt": "2026-07-01T10:00:00Z",
              "isOffPlatform": "False", "networkId": 1, "provider": "x"}]
    row = compute_metrics({"id": "u", "swapCount": 1}, swaps, reconstruct(swaps),
                          now_iso="2026-07-06T00:00:00Z", large_buy_usd=100.0, swap_count=1)
    unknown = set(row) - set(m.TRADER_ANALYTICS_COLUMNS)
    assert unknown == set(), f"metrics keys not persisted: {unknown}"
