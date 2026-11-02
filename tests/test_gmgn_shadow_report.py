import sqlite3

from scripts.research.gmgn_shadow_report import paired_dex_mark


def test_pair_uses_only_nearby_nonquarantined_dex_marks():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_position_marks(chain_id TEXT, mint TEXT, sweep_ts TEXT, "
              "mid REAL, liquidity REAL, source TEXT, quality TEXT)")
    c.executemany("INSERT INTO memecoin_position_marks VALUES(?,?,?,?,?,?,?)", [
        ("solana", "CAT", "2026-09-25T12:00:00Z", 1.0, 10, "fastlane_mid", "live"),
        ("solana", "CAT", "2026-09-25T12:00:03Z", 2.0, 20, "fastlane_sol_swap", "live"),
        ("solana", "CAT", "2026-09-25T12:00:04Z", 3.0, 30, "fastlane_mid", "quarantined_spike"),
        ("4663", "CAT", "2026-09-25T12:00:00Z", 4.0, 40, "dexscreener", "live"),
    ])
    assert paired_dex_mark(c, "solana", "CAT", "2026-09-25T12:00:02Z") == (1.0, 10)
    assert paired_dex_mark(c, "solana", "CAT", "2026-09-25T12:00:10Z") is None
    assert paired_dex_mark(c, "4663", "CAT", "2026-09-25T12:00:02Z") == (4.0, 40)
