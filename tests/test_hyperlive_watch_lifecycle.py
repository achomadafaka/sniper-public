from sniper.hyperlive import intel_store as IS
from sniper.hyperlive import watch_lifecycle as WL


DAY = 86_400_000


def _db(tmp_path):
    conn = IS.connect(str(tmp_path / "hl_intel.sqlite"))
    IS.ensure_schema(conn)
    return conn


def _seed_watch(conn, wallet, *, ts_ms=1, month_roi=1.0, profit_factor=3.0, win_rate=0.7,
                realized=0.0):
    info = {
        "verdict": "watch",
        "archetype": "directional",
        "archetype_pass": 1,
        "month_roi": month_roi,
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "n_closes": 100,
        "realized": realized,
    }
    IS.upsert_screened(conn, wallet, info, ts_ms=ts_ms)
    IS.upsert_tracked(conn, wallet, info, ts_ms=ts_ms)


def _closed_follow(conn, wallet, *, n, net_bps):
    coins = ["BTC", "ETH", "SOL", "HYPE", "DOGE", "XRP", "FARTCOIN", "PURR"]
    for i in range(n):
        ts_ms = (i + 1) * DAY
        fid = IS.open_follow(
            conn,
            {"wallet": wallet, "coin": coins[i % len(coins)], "side": "long", "entry_mark": 1.0},
            ts_ms=ts_ms,
        )
        IS.close_follow(conn, fid, exit_mark=1.0, net_bps=net_bps, reason="mirror_exit", ts_ms=ts_ms)


def _closed_copy(conn, wallet, *, n, pnl_bps):
    coins = ["BTC", "ETH", "SOL", "HYPE", "DOGE", "XRP", "FARTCOIN", "PURR"]
    for i in range(n):
        ts_ms = (i + 1) * DAY
        cid = IS.open_copy(conn, wallet, coins[i % len(coins)], "long", 1.0, ts_ms=ts_ms)
        IS.close_copy(conn, cid, exit_mark=1.0, pnl_bps=pnl_bps, ts_ms=ts_ms)


def test_lifecycle_keeps_watch_and_activates_fallback_when_no_proven(tmp_path):
    conn = _db(tmp_path)
    _seed_watch(conn, "0xwatch", ts_ms=10, month_roi=2.0)

    summary = WL.maintain_watchlist(conn, now_ms=20, max_watch_targets=5)

    row = IS.get_tracked(conn)[0]
    targets = IS.get_executable_copy_targets(conn, now_ms=20)
    assert summary["active_watch"] == 1
    assert row["lifecycle_status"] == "active"
    assert targets[0]["wallet"] == "0xwatch"
    assert targets[0]["copy_tier"] == "watch_fallback"


def test_lifecycle_culls_wallet_rejected_by_latest_screen(tmp_path):
    conn = _db(tmp_path)
    _seed_watch(conn, "0xreject", ts_ms=10)
    IS.upsert_screened(conn, "0xreject", {"verdict": "market_maker", "archetype_pass": 0,
                                          "reason": "maker rebate"}, ts_ms=20)
    IS.set_copy_target(conn, "0xreject", active=True, copy_tier="watch_fallback", ts_ms=10)

    summary = WL.maintain_watchlist(conn, now_ms=30)

    row = IS.get_tracked(conn, include_culled=True)[0]
    target = IS.get_copy_targets(conn, active_only=False)[0]
    assert summary["culled"] == 1
    assert row["lifecycle_status"] == "culled"
    assert "latest screen rejected" in row["lifecycle_reason"]
    assert target["active"] == 0


def test_lifecycle_culls_mature_negative_follow_wallet(tmp_path):
    conn = _db(tmp_path)
    _seed_watch(conn, "0xnegative", ts_ms=10)
    _closed_follow(conn, "0xnegative", n=30, net_bps=-1.0)

    WL.maintain_watchlist(conn, now_ms=40 * DAY)

    row = IS.get_tracked(conn, include_culled=True)[0]
    assert row["lifecycle_status"] == "culled"
    assert "30 closed follows" in row["lifecycle_reason"]


def test_lifecycle_culls_mature_non_positive_copy_wallet(tmp_path):
    conn = _db(tmp_path)
    _seed_watch(conn, "0xnegativecopy", ts_ms=10)
    _closed_copy(conn, "0xnegativecopy", n=20, pnl_bps=0.0)

    WL.maintain_watchlist(conn, now_ms=40 * DAY)

    row = IS.get_tracked(conn, include_culled=True)[0]
    assert row["lifecycle_status"] == "culled"
    assert "20 closed copies" in row["lifecycle_reason"]


def test_lifecycle_culls_early_bad_copy_wallet(tmp_path):
    conn = _db(tmp_path)
    _seed_watch(conn, "0xbadcopy", ts_ms=10)
    IS.set_copy_target(conn, "0xbadcopy", active=True, copy_tier="watch_fallback", ts_ms=11)
    _closed_copy(conn, "0xbadcopy", n=5, pnl_bps=-30.0)

    WL.maintain_watchlist(conn, now_ms=10 * DAY)

    row = IS.get_tracked(conn, include_culled=True)[0]
    target = IS.get_copy_targets(conn, active_only=False)[0]
    assert row["lifecycle_status"] == "culled"
    assert "5 closed copies" in row["lifecycle_reason"]
    assert target["active"] == 0


def test_lifecycle_keeps_stale_wallet_with_open_follow(tmp_path):
    conn = _db(tmp_path)
    _seed_watch(conn, "0xopenfollow", ts_ms=1)
    IS.open_follow(conn, {"wallet": "0xopenfollow", "coin": "BTC", "side": "long",
                          "entry_mark": 100.0}, ts_ms=2)

    WL.maintain_watchlist(conn, now_ms=10 * DAY)

    row = IS.get_tracked(conn)[0]
    assert row["wallet"] == "0xopenfollow"
    assert row["lifecycle_status"] == "active"


def test_lifecycle_keeps_stale_archetype_pass_wallet(tmp_path):
    # New contract (2026-07-09): archetype-pass wallets are NOT culled for staleness alone.
    # Re-screening (screen --refresh-tracked) keeps them fresh; a stale gap is not evidence
    # the wallet is bad. Only a re-screen downgrade or negative forward evidence culls them.
    conn = _db(tmp_path)
    _seed_watch(conn, "0xstale", ts_ms=1)

    summary = WL.maintain_watchlist(conn, now_ms=10 * DAY)

    row = IS.get_tracked(conn)[0]
    assert row["wallet"] == "0xstale"
    assert row["lifecycle_status"] == "active"
    assert summary["culled"] == 0


def test_lifecycle_keeps_stale_archetype_pass_wallet_with_open_copy(tmp_path):
    conn = _db(tmp_path)
    _seed_watch(conn, "0xopencopy", ts_ms=1)
    IS.set_copy_target(conn, "0xopencopy", active=True, copy_tier="watch_fallback", ts_ms=2)
    IS.open_copy(conn, "0xopencopy", "BTC", "long", 100.0, ts_ms=2)

    WL.maintain_watchlist(conn, now_ms=10 * DAY)

    row = IS.get_tracked(conn, include_culled=True)[0]
    assert row["lifecycle_status"] == "active"


def test_lifecycle_ranking_does_not_reward_negative_follow_history(tmp_path):
    conn = _db(tmp_path)
    _seed_watch(conn, "0xnegative", ts_ms=10, month_roi=10.0, profit_factor=50.0, win_rate=0.9)
    _seed_watch(conn, "0xclean", ts_ms=10, month_roi=1.0, profit_factor=3.0, win_rate=0.7)
    _closed_follow(conn, "0xnegative", n=1, net_bps=-20.0)

    summary = WL.maintain_watchlist(conn, now_ms=2 * DAY, max_watch_targets=1)

    assert summary["selected_targets"] == {"0xclean": "watch_fallback"}


def test_lifecycle_ranking_is_quality_led_over_positive_copy(tmp_path):
    # New contract (2026-07-09): ranking is quality-led. A wallet with much stronger
    # fundamentals outranks a weak-fundamentals incumbent that merely has a positive copy book.
    conn = _db(tmp_path)
    _seed_watch(conn, "0xcopypos", ts_ms=10, month_roi=1.0, profit_factor=3.0, win_rate=0.7)
    _seed_watch(conn, "0xclean", ts_ms=10, month_roi=10.0, profit_factor=50.0, win_rate=0.9)
    _closed_copy(conn, "0xcopypos", n=1, pnl_bps=40.0)

    summary = WL.maintain_watchlist(conn, now_ms=2 * DAY, max_watch_targets=1)

    assert summary["selected_targets"] == {"0xclean": "watch_fallback"}


def test_lifecycle_ranking_positive_copy_breaks_ties(tmp_path):
    # Forward evidence is a bounded tiebreak: with equal fundamentals, positive copy wins.
    conn = _db(tmp_path)
    _seed_watch(conn, "0xcopypos", ts_ms=10, month_roi=1.0, profit_factor=3.0, win_rate=0.7)
    _seed_watch(conn, "0xtie", ts_ms=10, month_roi=1.0, profit_factor=3.0, win_rate=0.7)
    _closed_copy(conn, "0xcopypos", n=1, pnl_bps=40.0)

    summary = WL.maintain_watchlist(conn, now_ms=2 * DAY, max_watch_targets=1)

    assert summary["selected_targets"] == {"0xcopypos": "watch_fallback"}


def test_lifecycle_ranking_quality_over_incumbency(tmp_path):
    # A fresh high-realized wallet with zero forward history outranks a low-realized
    # incumbent that already has a positive copy book (the incumbency-bias fix).
    conn = _db(tmp_path)
    _seed_watch(conn, "0xbest", ts_ms=10, month_roi=1.0, profit_factor=3.0, win_rate=0.7,
                realized=100_000.0)
    _seed_watch(conn, "0xincumbent", ts_ms=10, month_roi=1.0, profit_factor=3.0, win_rate=0.7,
                realized=1_000.0)
    _closed_copy(conn, "0xincumbent", n=1, pnl_bps=40.0)

    summary = WL.maintain_watchlist(conn, now_ms=2 * DAY, max_watch_targets=1)

    assert summary["selected_targets"] == {"0xbest": "watch_fallback"}


def test_lifecycle_ranking_does_not_reward_negative_copy_history(tmp_path):
    conn = _db(tmp_path)
    _seed_watch(conn, "0xnegativecopy", ts_ms=10, month_roi=10.0, profit_factor=50.0, win_rate=0.9)
    _seed_watch(conn, "0xclean", ts_ms=10, month_roi=1.0, profit_factor=3.0, win_rate=0.7)
    _closed_copy(conn, "0xnegativecopy", n=1, pnl_bps=-20.0)

    summary = WL.maintain_watchlist(conn, now_ms=2 * DAY, max_watch_targets=1)

    assert summary["selected_targets"] == {"0xclean": "watch_fallback"}


def test_lifecycle_proven_ranking_uses_follow_not_copy_history(tmp_path):
    conn = _db(tmp_path)
    _seed_watch(conn, "0xcopypos", ts_ms=10, month_roi=1.0, profit_factor=3.0, win_rate=0.7)
    _seed_watch(conn, "0xfollowbest", ts_ms=10, month_roi=1.0, profit_factor=3.0, win_rate=0.7)
    _closed_follow(conn, "0xcopypos", n=32, net_bps=10.0)
    _closed_follow(conn, "0xfollowbest", n=32, net_bps=50.0)
    _closed_copy(conn, "0xcopypos", n=1, pnl_bps=100.0)

    summary = WL.maintain_watchlist(conn, now_ms=40 * DAY, max_watch_targets=1)

    assert summary["selected_targets"] == {"0xfollowbest": "proven"}


def test_lifecycle_proven_targets_override_watch_fallback(tmp_path):
    conn = _db(tmp_path)
    _seed_watch(conn, "0xwatch", ts_ms=10, month_roi=5.0)
    _seed_watch(conn, "0xproven", ts_ms=10, month_roi=1.0)
    _closed_follow(conn, "0xproven", n=32, net_bps=40.0)

    summary = WL.maintain_watchlist(conn, now_ms=40 * DAY, max_watch_targets=5)

    targets = IS.get_executable_copy_targets(conn, now_ms=40 * DAY)
    all_targets = {row["wallet"]: row for row in IS.get_copy_targets(conn, active_only=False)}
    assert summary["proven"] == 1
    assert [t["wallet"] for t in targets] == ["0xproven"]
    assert all_targets["0xproven"]["copy_tier"] == "proven"
    assert all_targets["0xwatch"]["active"] == 0


def test_lifecycle_no_cap_follows_all_eligible_archetype_pass(tmp_path):
    # New contract (2026-07-09): max_watch_targets=None means no cap — every eligible
    # archetype-pass wallet is followed ("if it's good, it's good").
    conn = _db(tmp_path)
    for i in range(7):
        _seed_watch(conn, f"0xwatch{i}", ts_ms=10, month_roi=float(i))

    summary = WL.maintain_watchlist(conn, now_ms=20, max_watch_targets=None)

    targets = {t["wallet"] for t in IS.get_executable_copy_targets(conn, now_ms=20)}
    assert len(targets) == 7
    assert targets == {f"0xwatch{i}" for i in range(7)}
    assert len(summary["selected_targets"]) == 7


def test_lifecycle_no_cap_is_the_default(tmp_path):
    conn = _db(tmp_path)
    for i in range(6):
        _seed_watch(conn, f"0xw{i}", ts_ms=10, month_roi=float(i))

    summary = WL.maintain_watchlist(conn, now_ms=20)  # no max_watch_targets arg

    assert len(summary["selected_targets"]) == 6


def test_lifecycle_numeric_cap_still_truncates(tmp_path):
    # Regression: an explicit numeric cap still truncates to the top-N by quality.
    conn = _db(tmp_path)
    for i in range(6):
        _seed_watch(conn, f"0xw{i}", ts_ms=10, month_roi=float(i))

    summary = WL.maintain_watchlist(conn, now_ms=20, max_watch_targets=2)

    assert len(summary["selected_targets"]) == 2
    # highest month_roi wins under quality-led ranking
    assert set(summary["selected_targets"]) == {"0xw5", "0xw4"}
