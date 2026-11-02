import os
import sqlite3

import pytest

from sniper.hyperlive import intel_store as IS


def test_readonly_connection_rejects_writes(tmp_path):
    db = str(tmp_path / "intel.db")
    writer = IS.connect(db)
    IS.ensure_schema(writer)
    writer.close()

    reader = IS.connect_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO hl_meta(key, value) VALUES ('x', 'y')")
    finally:
        reader.close()


def test_unchanged_copy_excursion_does_not_write(tmp_path):
    conn = IS.connect(str(tmp_path / "intel.db"))
    IS.ensure_schema(conn)
    copy_id = IS.open_copy(conn, "0xabc", "BTC", "long", 100.0, ts_ms=1)
    IS.update_copy_excursion(conn, copy_id, gross_bps=25.0)
    IS.update_copy_excursion(conn, copy_id, gross_bps=-10.0)
    before = conn.total_changes

    IS.update_copy_excursion(conn, copy_id, gross_bps=10.0)

    assert conn.total_changes == before


def test_current_schema_check_does_not_rewrite_regime_rows(tmp_path):
    conn = IS.connect(str(tmp_path / "intel.db"))
    IS.ensure_schema(conn)
    conn.execute(
        "INSERT INTO hl_altshort_regime_marks"
        "(ts_ms, coin, mark, open_mark, high_mark, low_mark) VALUES (1,'BTC',100,100,100,100)"
    )
    conn.commit()
    before = conn.total_changes

    IS.ensure_schema(conn)

    assert conn.total_changes == before


def _db(tmp_path):
    c = IS.connect(os.path.join(str(tmp_path), "intel.sqlite"))
    IS.ensure_schema(c)
    return c


def _seed_proven_follows(conn, wallet="0xproven", *, n=32, net_bps=40.0):
    day = 86_400_000
    coins = ["BTC", "ETH", "SOL", "HYPE", "DOGE", "XRP", "FARTCOIN", "PURR"]
    for i in range(n):
        fid = IS.open_follow(
            conn,
            {"wallet": wallet, "coin": coins[i % len(coins)], "side": "long", "entry_mark": 1.0},
            ts_ms=(i + 1) * day,
        )
        IS.close_follow(conn, fid, exit_mark=1.0, net_bps=net_bps, reason="mirror_exit", ts_ms=(i + 1) * day)


def test_tracked_trader_upsert_roundtrip(tmp_path):
    c = _db(tmp_path)
    IS.upsert_tracked(c, "0xabc", {"verdict": "copyable", "archetype": "directional",
                                   "taker_frac": 1.0, "profit_factor": 2.4, "win_rate": 0.7,
                                   "n_closes": 800, "month_roi": 3.1, "realized": 50000.0}, ts_ms=1000)
    rows = IS.get_tracked(c)
    assert len(rows) == 1 and rows[0]["wallet"] == "0xabc" and rows[0]["verdict"] == "copyable"
    # update keeps one row
    IS.upsert_tracked(c, "0xabc", {"verdict": "copyable", "profit_factor": 2.6}, ts_ms=2000)
    assert len(IS.get_tracked(c)) == 1


def test_trader_state_roundtrip(tmp_path):
    c = _db(tmp_path)
    IS.save_trader_state(c, "0xabc", {"ETH": {"szi": 8.0, "side": "long", "entry_px": 2000.0}}, ts_ms=1000)
    st = IS.get_trader_state(c, "0xabc")
    assert st["ETH"]["side"] == "long"
    assert IS.get_trader_state(c, "0xnope") == {}


def test_follow_open_close_and_summary(tmp_path):
    c = _db(tmp_path)
    fid = IS.open_follow(c, {"wallet": "0xabc", "coin": "ETH", "side": "long", "kind": "new",
                             "entry_mark": 2000.0}, ts_ms=1000)
    assert IS.get_open_follows(c)[0]["coin"] == "ETH"
    IS.close_follow(c, fid, exit_mark=2040.0, net_bps=191.0, reason="mirror_exit", ts_ms=2000)
    assert IS.get_open_follows(c) == []
    s = IS.follow_summary(c)
    assert s["closed"] == 1 and s["wins"] == 1 and round(s["sum_net_bps"]) == 191


def test_screen_run_roundtrip(tmp_path):
    c = _db(tmp_path)
    IS.save_screen_run(c, {"leaderboard_n": 39000, "prefiltered_n": 35, "screened_n": 32,
                           "verdicts": {"copyable": 10, "market_maker": 7}}, ts_ms=1000)
    run = IS.latest_screen_run(c)
    assert run["screened_n"] == 32 and run["verdicts"]["copyable"] == 10


def test_screened_traders_grade_list_orders_copyable_first(tmp_path):
    c = _db(tmp_path)
    IS.upsert_screened(c, "0xmm", {"verdict": "market_maker", "month_roi": 9.0, "reason": "rebate"}, ts_ms=1)
    IS.upsert_screened(c, "0xok", {"verdict": "copyable", "month_roi": 2.0, "archetype": "directional"}, ts_ms=1)
    rows = IS.get_screened(c)
    assert rows[0]["wallet"] == "0xok"          # copyable sorts first regardless of ROI
    assert rows[1]["verdict"] == "market_maker"


def test_archetype_pass_column_and_backfill(tmp_path):
    import sniper.hyperlive.intel_store as IS
    db = str(tmp_path / "intel.sqlite")
    conn = IS.connect(db)
    # simulate a PRE-migration row written by the old code path (no archetype_pass)
    IS.ensure_schema(conn)
    conn.execute("INSERT INTO hl_tracked_traders(wallet, verdict, archetype, added_ts_ms, updated_ts_ms) "
                 "VALUES ('0xold', 'copyable', 'directional', 1, 1)")
    conn.commit()
    IS._migrate(conn)                         # idempotent; backfills legacy copyable -> archetype_pass=1
    row = conn.execute("SELECT archetype_pass FROM hl_tracked_traders WHERE wallet='0xold'").fetchone()
    assert row["archetype_pass"] == 1
    IS._migrate(conn)                         # second call must not throw (idempotent)


def test_upsert_persists_archetype_pass(tmp_path):
    import sniper.hyperlive.intel_store as IS
    conn = IS.connect(str(tmp_path / "intel.sqlite"))
    IS.ensure_schema(conn)
    IS.upsert_tracked(conn, "0xnew", {"verdict": "watch", "archetype": "directional",
                                      "archetype_pass": 1}, ts_ms=10)
    row = conn.execute("SELECT archetype_pass FROM hl_tracked_traders WHERE wallet='0xnew'").fetchone()
    assert row["archetype_pass"] == 1


def test_upsert_tracked_update_path_sets_archetype_pass(tmp_path):
    import sniper.hyperlive.intel_store as IS
    conn = IS.connect(str(tmp_path / "intel.sqlite"))
    IS.ensure_schema(conn)
    # INSERT with archetype_pass=0
    IS.upsert_tracked(conn, "0xupd", {"verdict": "watch", "archetype_pass": 0}, ts_ms=10)
    row = conn.execute("SELECT archetype_pass FROM hl_tracked_traders WHERE wallet='0xupd'").fetchone()
    assert row["archetype_pass"] == 0
    # UPDATE the same wallet to archetype_pass=1 — the UPDATE path must persist it
    IS.upsert_tracked(conn, "0xupd", {"archetype_pass": 1}, ts_ms=20)
    row = conn.execute("SELECT archetype_pass FROM hl_tracked_traders WHERE wallet='0xupd'").fetchone()
    assert row["archetype_pass"] == 1


def test_migrate_leaves_reject_verdicts_unflagged(tmp_path):
    import sniper.hyperlive.intel_store as IS
    conn = IS.connect(str(tmp_path / "intel.sqlite"))
    IS.ensure_schema(conn)
    # Insert a market_maker row (pre-migration style, archetype_pass=0)
    conn.execute(
        "INSERT INTO hl_tracked_traders(wallet, verdict, archetype, added_ts_ms, updated_ts_ms, archetype_pass) "
        "VALUES ('0xmm', 'market_maker', 'mm', 1, 1, 0)"
    )
    conn.commit()
    IS._migrate(conn)  # backfill only touches verdict='copyable'; market_maker stays 0
    row = conn.execute("SELECT archetype_pass FROM hl_tracked_traders WHERE wallet='0xmm'").fetchone()
    assert row["archetype_pass"] == 0


def test_upsert_screened_explicit_zero_not_overridden(tmp_path):
    import sniper.hyperlive.intel_store as IS
    conn = IS.connect(str(tmp_path / "intel.sqlite"))
    IS.ensure_schema(conn)
    # explicit archetype_pass=0 with verdict="copyable" — explicit value must win
    IS.upsert_screened(conn, "0xexplicit", {"verdict": "copyable", "archetype_pass": 0}, ts_ms=1)
    row = conn.execute("SELECT archetype_pass FROM hl_screened_traders WHERE wallet='0xexplicit'").fetchone()
    assert row["archetype_pass"] == 0


def test_copy_target_and_book_roundtrip(tmp_path):
    c = _db(tmp_path)
    IS.set_copy_target(c, "0xabc", label="sharp1", ts_ms=1)
    assert IS.get_copy_targets(c)[0]["wallet"] == "0xabc"
    cid = IS.open_copy(c, "0xabc", "ETH", "long", 2000.0, copy_tier="watch_fallback", ts_ms=1)
    assert IS.get_open_copies(c, target="0xabc")[0]["coin"] == "ETH"
    assert IS.get_open_copies(c, target="0xabc")[0]["copy_tier"] == "watch_fallback"
    IS.close_copy(c, cid, exit_mark=2040.0, pnl_bps=191.0, ts_ms=2)
    assert IS.recent_closed_copies(c)[0]["copy_tier"] == "watch_fallback"
    s = IS.copy_summary(c)
    assert s["closed"] == 1 and s["wins"] == 1 and round(s["realized_sum_bps"]) == 191
    assert s["sum_pnl_bps"] == s["realized_sum_bps"]
    assert s["total_bps"] == 191.0 and s["episode_n"] == 1
    # deactivate
    IS.set_copy_target(c, "0xabc", active=False, ts_ms=3)
    assert IS.get_copy_targets(c) == []


def test_copy_summary_marks_open_book_and_reports_concentration(tmp_path):
    c = _db(tmp_path)
    closed_id = IS.open_copy(c, "0xabc", "ETH", "long", 100.0, ts_ms=1)
    IS.close_copy(c, closed_id, exit_mark=101.0, pnl_bps=100.0, ts_ms=2)
    IS.open_copy(c, "0xdef", "BTC", "long", 100.0, ts_ms=3)
    c.execute(
        "INSERT INTO hl_altshort_regime_marks"
        "(ts_ms,coin,mark,open_mark,high_mark,low_mark) VALUES (4,'BTC',90,90,90,90)"
    )
    c.commit()

    summary = IS.copy_summary(c)

    assert summary["open_mtm_bps"] == -1009.0
    assert summary["total_bps"] == -909.0
    assert summary["top1_share"] == 1.0
    assert summary["episode_n"] == 1


def test_copy_target_status_rejects_watch_without_forward_proof(tmp_path):
    c = _db(tmp_path)
    IS.upsert_screened(c, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)

    status = IS.copy_target_status(c, "0xwatch", now_ms=1_000_000)

    assert status["eligible"] is False
    assert status["effective_verdict"] == "watch"
    assert status["reason"] == "0 closes < 30"


def test_copy_target_status_allows_proven_forward_follow(tmp_path):
    c = _db(tmp_path)
    IS.upsert_screened(c, "0xproven", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    _seed_proven_follows(c, "0xproven")

    status = IS.copy_target_status(c, "0xproven", now_ms=40 * 86_400_000)

    assert status["eligible"] is True
    assert status["effective_verdict"] == "proven"
    assert status["n_closed"] == 32


def test_get_proven_copy_targets_filters_active_watch_targets(tmp_path):
    c = _db(tmp_path)
    IS.upsert_screened(c, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_screened(c, "0xproven", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    _seed_proven_follows(c, "0xproven")
    IS.set_copy_target(c, "0xwatch", ts_ms=1)
    IS.set_copy_target(c, "0xproven", ts_ms=1)

    targets = IS.get_proven_copy_targets(c, now_ms=40 * 86_400_000)

    assert [t["wallet"] for t in targets] == ["0xproven"]
    assert targets[0]["copy_status"]["effective_verdict"] == "proven"


def test_follow_stats_by_wallet(tmp_path):
    import sniper.hyperlive.intel_store as IS
    conn = IS.connect(str(tmp_path / "intel.sqlite"))
    IS.ensure_schema(conn)
    day = 86_400_000
    rows = [("0xa", "BTC", 50.0, day + 1), ("0xa", "BTC", -10.0, day + 2),
            ("0xa", "ETH", 30.0, 2 * day + 1), ("0xb", "SOL", 5.0, day + 1)]
    for w, coin, net, ts in rows:
        fid = IS.open_follow(conn, {"wallet": w, "coin": coin, "side": "long", "entry_mark": 1.0}, ts_ms=ts)
        IS.close_follow(conn, fid, exit_mark=1.0, net_bps=net, reason="mirror", ts_ms=ts)
    stats = IS.follow_stats_by_wallet(conn)
    assert stats["0xa"]["n_closed"] == 3
    assert abs(stats["0xa"]["mean_net_bps"] - round((50 - 10 + 30) / 3, 3)) < 1e-9
    assert stats["0xa"]["last_close_ts"] == 2 * day + 1
    # BTC contributes 50+(-10)=40 of 70 positive pnl -> top_asset_pnl_frac ~ 40/80
    assert 0.0 <= stats["0xa"]["top_asset_pnl_frac"] <= 1.0
    assert set(stats["0xa"]["by_cluster"]) == {"BTC|1", "ETH|2"}


def test_copy_stats_by_wallet(tmp_path):
    import sniper.hyperlive.intel_store as IS
    conn = IS.connect(str(tmp_path / "intel.sqlite"))
    IS.ensure_schema(conn)
    day = 86_400_000
    rows = [("0xa", "BTC", 50.0, day + 1), ("0xa", "BTC", -10.0, day + 2),
            ("0xa", "ETH", 30.0, 2 * day + 1), ("0xb", "SOL", -5.0, day + 1)]
    for wallet, coin, pnl, ts in rows:
        cid = IS.open_copy(conn, wallet, coin, "long", 1.0, ts_ms=ts)
        IS.close_copy(conn, cid, exit_mark=1.0, pnl_bps=pnl, ts_ms=ts)
    IS.open_copy(conn, "0xa", "HYPE", "long", 1.0, ts_ms=3 * day)
    IS.open_copy(conn, "0xc", "BTC", "long", 1.0, ts_ms=3 * day)
    cid = IS.open_copy(conn, "0xa", "SOL", "short", 1.0, copy_tier="proven", ts_ms=3 * day + 1)
    IS.close_copy(conn, cid, exit_mark=1.0, pnl_bps=9.0, ts_ms=3 * day + 2)

    stats = IS.copy_stats_by_wallet(conn)

    assert stats["0xa"]["n_closed"] == 4
    assert abs(stats["0xa"]["mean_pnl_bps"] - round((50 - 10 + 30 + 9) / 4, 3)) < 1e-9
    assert stats["0xa"]["sum_pnl_bps"] == 79.0
    assert stats["0xa"]["wins"] == 3
    assert stats["0xa"]["open_n"] == 1
    assert stats["0xa"]["last_close_ts"] == 3 * day + 2
    assert stats["0xa"]["by_tier"]["unknown"]["n_closed"] == 3
    assert stats["0xa"]["by_tier"]["proven"]["mean_pnl_bps"] == 9.0
    assert stats["0xb"]["mean_pnl_bps"] == -5.0
    assert stats["0xc"]["n_closed"] == 0
    assert stats["0xc"]["open_n"] == 1


def test_watch_fallback_copy_status_self_disqualifies_before_lifecycle(tmp_path):
    import sniper.hyperlive.intel_store as IS
    conn = IS.connect(str(tmp_path / "intel.sqlite"))
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xbadcopy", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_tracked(conn, "0xbadcopy", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xbadcopy", active=True, copy_tier="watch_fallback", ts_ms=2)
    for i in range(5):
        cid = IS.open_copy(conn, "0xbadcopy", "BTC", "long", 1.0, ts_ms=10 + i)
        IS.close_copy(conn, cid, exit_mark=1.0, pnl_bps=-25.0, ts_ms=20 + i)

    status = IS.copy_target_status(conn, "0xbadcopy", now_ms=100, copy_tier="watch_fallback")
    targets = IS.get_executable_copy_targets(conn, now_ms=100)

    assert status["eligible"] is False
    assert "5 closed copies" in status["reason"]
    assert targets == []


def test_lifecycle_and_copy_tier_columns_migrate_additively(tmp_path):
    conn = _db(tmp_path)

    tracked_cols = {row[1] for row in conn.execute("PRAGMA table_info(hl_tracked_traders)")}
    target_cols = {row[1] for row in conn.execute("PRAGMA table_info(hl_copy_targets)")}
    copy_book_cols = {row[1] for row in conn.execute("PRAGMA table_info(hl_copy_book)")}

    assert {"lifecycle_status", "lifecycle_reason", "lifecycle_updated_ts_ms",
            "last_seen_ts_ms", "culled_ts_ms"} <= tracked_cols
    assert {"copy_tier", "reason", "updated_ts_ms"} <= target_cols
    assert "copy_tier" in copy_book_cols


def test_copy_book_legacy_rows_migrate_to_unknown_copy_tier(tmp_path):
    import sqlite3
    db = str(tmp_path / "old_intel.sqlite")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE hl_copy_book (
            id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, coin TEXT NOT NULL,
            side TEXT, entry_mark REAL, detect_ts_ms INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', exit_ts_ms INTEGER, exit_mark REAL, pnl_bps REAL
        );
        INSERT INTO hl_copy_book(target, coin, side, entry_mark, detect_ts_ms, status, exit_ts_ms, exit_mark, pnl_bps)
        VALUES ('0xlegacy', 'BTC', 'long', 100.0, 1, 'closed', 2, 101.0, 91.0);
    """)
    conn.commit()
    conn.close()

    conn = IS.connect(db)
    IS.ensure_schema(conn)
    row = conn.execute("SELECT copy_tier FROM hl_copy_book WHERE target='0xlegacy'").fetchone()

    assert row["copy_tier"] == "unknown"


def test_get_tracked_hides_culled_by_default(tmp_path):
    conn = _db(tmp_path)
    IS.upsert_tracked(conn, "0xkeep", {"verdict": "watch", "archetype_pass": 1, "month_roi": 1.0}, ts_ms=1)
    IS.upsert_tracked(conn, "0xcull", {"verdict": "watch", "archetype_pass": 1, "month_roi": 2.0}, ts_ms=1)

    IS.set_tracked_lifecycle(conn, "0xcull", status="culled", reason="negative follow", ts_ms=2)

    assert [row["wallet"] for row in IS.get_tracked(conn)] == ["0xkeep"]
    assert {row["wallet"] for row in IS.get_tracked(conn, include_culled=True)} == {"0xkeep", "0xcull"}


def test_copy_target_tier_roundtrip(tmp_path):
    conn = _db(tmp_path)

    IS.set_copy_target(conn, "0xwatch", active=True, copy_tier="watch_fallback",
                       reason="fallback basket rank 1", ts_ms=10)
    row = IS.get_copy_targets(conn)[0]

    assert row["wallet"] == "0xwatch"
    assert row["copy_tier"] == "watch_fallback"
    assert row["reason"] == "fallback basket rank 1"
    assert row["updated_ts_ms"] == 10


def test_executable_copy_targets_allow_watch_fallback_only_without_proven(tmp_path):
    conn = _db(tmp_path)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_tracked(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xwatch", active=True, copy_tier="watch_fallback", ts_ms=2)

    targets = IS.get_executable_copy_targets(conn, now_ms=3)

    assert [t["wallet"] for t in targets] == ["0xwatch"]
    assert targets[0]["copy_status"]["copy_tier"] == "watch_fallback"
    assert targets[0]["copy_status"]["eligible"] is True


def test_executable_copy_targets_disable_watch_fallback_when_proven_exists(tmp_path):
    conn = _db(tmp_path)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_tracked(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xwatch", active=True, copy_tier="watch_fallback", ts_ms=2)
    IS.upsert_screened(conn, "0xproven", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_tracked(conn, "0xproven", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    _seed_proven_follows(conn, "0xproven")

    targets = IS.get_executable_copy_targets(conn, now_ms=40 * 86_400_000)

    assert targets == []


def test_executable_copy_targets_ignore_copy_disqualified_proven_for_fallback(tmp_path):
    conn = _db(tmp_path)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_tracked(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xwatch", active=True, copy_tier="watch_fallback", ts_ms=2)
    IS.upsert_screened(conn, "0xproven", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_tracked(conn, "0xproven", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xproven", active=True, copy_tier="proven", ts_ms=2)
    _seed_proven_follows(conn, "0xproven")
    for i in range(5):
        cid = IS.open_copy(conn, "0xproven", "BTC", "long", 1.0, ts_ms=10 + i)
        IS.close_copy(conn, cid, exit_mark=1.0, pnl_bps=-25.0, ts_ms=20 + i)

    targets = IS.get_executable_copy_targets(conn, now_ms=40 * 86_400_000)

    assert [t["wallet"] for t in targets] == ["0xwatch"]
    assert targets[0]["copy_tier"] == "watch_fallback"
