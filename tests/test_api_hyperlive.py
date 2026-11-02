import os

from flask import Flask

import scripts.run_hyperlive_engine as CLI
from sniper.routes.api_hyperlive import create_api_hyperlive_blueprint


def _app(db):
    app = Flask(__name__)
    app.register_blueprint(create_api_hyperlive_blueprint(db_path=db))
    return app.test_client()


def _make_app(bp):
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app


def _seed_proven_hl_follow_stats(conn, wallet="0xproven", *, n=32, net_bps=40.0):
    import time

    from sniper.hyperlive import intel_store as IS

    day = 86_400_000
    base = int(time.time() * 1000) - n * day
    coins = ["BTC", "ETH", "SOL", "HYPE", "DOGE", "XRP", "FARTCOIN", "PURR"]
    for i in range(n):
        ts_ms = base + (i + 1) * day
        fid = IS.open_follow(
            conn,
            {"wallet": wallet, "coin": coins[i % len(coins)], "side": "long", "entry_mark": 1.0},
            ts_ms=ts_ms,
        )
        IS.close_follow(conn, fid, exit_mark=1.0, net_bps=net_bps, reason="mirror_exit", ts_ms=ts_ms)


def test_state_route_unavailable_before_engine_starts(tmp_path):
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))
    assert c.get("/api/hyperlive/state").get_json()["available"] is False


def test_state_and_history_routes_serve_engine_output(tmp_path):
    db = os.path.join(str(tmp_path), "hl.sqlite")
    assert CLI.main(["--fixture-mode", "--db-path", db]) == 0
    c = _app(db)

    state = c.get("/api/hyperlive/state").get_json()
    assert state["available"] is True
    assert "tracks" in state and "fast" in state["tracks"] and "swing" in state["tracks"]
    fast = state["tracks"]["fast"]
    assert fast["state"] in (
        "SCANNING", "SELECTING", "DESIGNING", "ARMED", "MANAGING", "CLOSING", "EVALUATING")
    assert "calibration" in fast and "ranked" in fast

    hist = c.get("/api/hyperlive/history").get_json()["rows"]
    assert len(hist) >= 1
    assert hist[0]["setup"] and hist[0]["reason"]


def test_intel_route_serves_cohort_and_follow(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS
    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.upsert_tracked(conn, "0xabc", {"verdict": "copyable", "archetype": "directional",
                                      "taker_frac": 1.0, "profit_factor": 2.4, "win_rate": 0.7,
                                      "n_closes": 800, "month_roi": 3.1, "realized": 50000.0}, ts_ms=1)
    IS.save_screen_run(conn, {"leaderboard_n": 39000, "prefiltered_n": 35, "screened_n": 32,
                              "verdicts": {"copyable": 10, "market_maker": 7}}, ts_ms=1)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)

    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))     # engine db absent -> tests fallbacks
    j = c.get("/api/hyperlive/intel").get_json()
    assert j["available"] is True
    assert j["screen"]["verdicts"]["watch"] == 10
    assert j["screen"]["is_stale"] is True
    assert j["cohort"][0]["wallet"] == "0xabc" and j["cohort"][0]["verdict"] == "watch"
    assert j["cohort"][0]["stored_verdict"] == "copyable"
    assert "open" in j["follow"] and "closed" in j["follow"]
    assert "xyz" in j["forward_tests"] and "cascade" in j["forward_tests"]


def test_copy_get_uses_readonly_connection_and_can_omit_open_rows(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.open_copy(conn, "0xabc", "BTC", "long", 100.0, ts_ms=1)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    monkeypatch.setattr(IS, "connect", lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("GET route opened a writable connection")
    ))

    body = _app(os.path.join(str(tmp_path), "missing.sqlite")).get(
        "/api/hyperlive/copy?include_open=0"
    ).get_json()

    assert body["available"] is True
    assert body["open"] == []
    assert body["summary"]["open"] == 1


def test_analyzer_compact_mode_skips_duplicate_copy_target_details(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.set_copy_target(
        conn, "0xabc", active=True, copy_tier="watch_fallback", reason="test", ts_ms=1
    )
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)

    body = _app(os.path.join(str(tmp_path), "missing.sqlite")).get(
        "/api/hyperlive/analyzer?include_copy_details=0"
    ).get_json()

    assert body["copy_details_included"] is False
    assert body["copy_targets"] == []
    assert body["copy_target_rows"] == []
    assert body["blocked_copy_targets"] == []


def test_copy_target_grouping_computes_proven_wallet_once(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    conn = IS.connect(str(tmp_path / "hl_intel.sqlite"))
    IS.ensure_schema(conn)
    for wallet in ("0xa", "0xb"):
        IS.set_copy_target(
            conn, wallet, active=True, copy_tier="watch_fallback", reason="test", ts_ms=1
        )
    calls = {"proven": 0, "statuses": []}

    def has_proven(*_args, **_kwargs):
        calls["proven"] += 1
        return False

    def status(_conn, wallet, **kwargs):
        calls["statuses"].append(kwargs.get("proven_exists"))
        return {"wallet": wallet, "eligible": True}

    monkeypatch.setattr(IS, "has_proven_wallet", has_proven)
    monkeypatch.setattr(IS, "copy_target_status", status)

    eligible, blocked = MOD._copy_target_groups(conn, now_ms=2)

    assert len(eligible) == 2 and blocked == []
    assert calls == {"proven": 1, "statuses": [False, False]}


def test_runtime_route_reports_hyperliquid_live_rails(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS
    from sniper.hyperlive import persistence as P
    from sniper.hyperlive import scan_log as SL

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1, "month_roi": 1.0}, ts_ms=1)
    IS.upsert_screened(conn, "0xreject", {"verdict": "market_maker", "archetype_pass": 0, "month_roi": 9.0}, ts_ms=1)
    IS.upsert_tracked(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1, "month_roi": 1.0}, ts_ms=1)
    IS.open_follow(conn, {"wallet": "0xwatch", "coin": "BTC", "side": "long", "entry_mark": 100.0}, ts_ms=2)
    IS.save_screen_run(conn, {"leaderboard_n": 100, "screened_n": 2, "verdicts": {"watch": 1}}, ts_ms=3)
    conn.close()

    xyz_db = os.path.join(str(tmp_path), "xyz.sqlite")
    xconn = SL.connect(xyz_db)
    SL.ensure_schema(xconn)
    SL.log_refresh(xconn, 10, [{"asset": "BTC", "binance_basis_bps": 12.0}], refresh_interval_s=150)
    xconn.close()

    engine_db = os.path.join(str(tmp_path), "engine.sqlite")
    econn = P.connect(engine_db)
    P.ensure_schema(econn)
    P.save_cascade_obs(econn, {"BTC": (100.0, 1e9, 5, 1e5)}, ts_ms=10)
    econn.close()

    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    monkeypatch.setattr(MOD, "XYZ_DB", xyz_db)
    monkeypatch.setattr(MOD, "_runtime_pid", lambda match: {"xyz": 111, "follow": 222}.get(match))
    monkeypatch.setattr(MOD, "_pid_elapsed", lambda pid: "00:03:00")
    monkeypatch.setattr(MOD, "_crontab_text", lambda: "\n".join([
        "xyz_leadlag_watchdog.sh",
        "run_xyz_leadlag_audit.py",
        "hl_follow_watchdog.sh",
        "screen_hl_smart_money.py",
        "cascade_capture_watchdog.sh",
    ]))

    c = _app(engine_db)
    j = c.get("/api/hyperlive/runtime").get_json()
    assert j["available"] is True
    assert j["data"]["intel"]["screened"] == 2
    assert j["data"]["intel"]["watch"] == 1
    assert j["data"]["follow"]["open"] == 1
    assert j["data"]["xyz"]["refreshes"] == 1
    assert j["data"]["cascade"]["obs"] == 1

    processes = {p["id"]: p for p in j["processes"]}
    assert processes["xyz_capture"]["state"] == "running" and processes["xyz_capture"]["pid"] == 111
    assert processes["follow_runner"]["state"] == "running" and processes["follow_runner"]["pid"] == 222
    assert processes["cascade_capture"]["state"] == "idle"

    crons = {r["id"]: r for r in j["scheduled"]}
    assert crons["xyz_watchdog"]["installed"] is True
    assert crons["cascade_capture"]["installed"] is True

    lanes = {r["id"]: r for r in j["lanes"]}
    # runtime = raw process state; status = disposition (live/paused/retired/built_idle).
    assert lanes["xyz_leadlag"]["runtime"] == "running"
    assert lanes["xyz_leadlag"]["status"] == "retired"
    assert lanes["forward_follow"]["runtime"] == "running"
    assert lanes["forward_follow"]["status"] == "paused"
    assert lanes["cascade_detector"]["runtime"] == "idle"
    assert lanes["cascade_detector"]["status"] == "built_idle"
    # cascade idle in this fixture => built_idle, not live
    assert j["headline"]["summary"] == "0 live · 1 paused · 2 retired"


def test_runtime_process_match_rejects_pgrep_self():
    import sniper.routes.api_hyperlive as MOD

    assert MOD._cmdline_matches_runner(
        "pgrep -f run_cascade_capture.py",
        "run_cascade_capture.py",
    ) is False
    assert MOD._cmdline_matches_runner(
        "/home/bigpapi/.pyenv/versions/3.13.0/bin/python3 -u scripts/run_cascade_capture.py --db data/hyperlive_engine.sqlite",
        "run_cascade_capture.py",
    ) is True


def test_analyzer_and_copy_routes(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS
    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xok", {"verdict": "copyable", "archetype": "directional",
                                      "month_roi": 2.0, "profit_factor": 2.4}, ts_ms=1)
    IS.upsert_screened(conn, "0xmm", {"verdict": "market_maker", "reason": "rebate", "month_roi": 9.0}, ts_ms=1)
    IS.save_screen_run(conn, {"leaderboard_n": 100, "prefiltered_n": 2, "screened_n": 2,
                              "verdicts": {"copyable": 1, "market_maker": 1}}, ts_ms=1)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    a = c.get("/api/hyperlive/analyzer?view=all&page_size=100").get_json()
    assert a["available"] is True and len(a["traders"]) == 2
    assert a["traders"][0]["wallet"] == "0xok"               # copyable first
    assert a["traders"][0]["verdict"] == "watch"             # legacy raw copyable reads as watch
    assert a["screen"]["verdicts"] == {"watch": 1, "market_maker": 1}
    assert a["screen"]["is_stale"] is True and a["screen"]["age_days"] > 0

    # A phase-1 / legacy-copyable wallet is research-grade only; copy target creation hard-fails.
    resp = c.post("/api/hyperlive/copy/set", json={"wallet": "0xok"})
    r = resp.get_json()
    assert resp.status_code == 403
    assert r["ok"] is False and r["effective_verdict"] == "watch"
    cp = c.get("/api/hyperlive/copy").get_json()
    assert cp["available"] is True and "summary" in cp and "open" in cp


def test_analyzer_excludes_blocked_active_copy_targets_from_copy_badge(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xwatch", active=True, ts_ms=1)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    body = c.get("/api/hyperlive/analyzer?view=all&page_size=100").get_json()

    assert body["copy_targets"] == []
    assert body["blocked_copy_targets"][0]["wallet"] == "0xwatch"
    assert body["blocked_copy_targets"][0]["copy_status"]["effective_verdict"] == "watch"


def test_analyzer_reports_watch_fallback_copy_targets(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_tracked(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xwatch", active=True, copy_tier="watch_fallback",
                       reason="fallback basket rank 1", ts_ms=2)
    cid = IS.open_copy(conn, "0xwatch", "BTC", "long", 1.0, ts_ms=3)
    IS.close_copy(conn, cid, exit_mark=1.0, pnl_bps=42.0, ts_ms=4)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    body = c.get("/api/hyperlive/analyzer").get_json()

    assert body["copy_targets"] == ["0xwatch"]
    assert body["copy_target_rows"][0]["copy_tier"] == "watch_fallback"
    assert body["copy_target_rows"][0]["copy_status"]["copy_tier"] == "watch_fallback"
    assert body["copy_target_rows"][0]["copy_status"]["copy_stats"]["mean_pnl_bps"] == 42.0
    assert body["traders"][0]["copy"]["n_closed"] == 1


def test_analyzer_surfaces_tracked_lifecycle_on_screened_rows(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_tracked(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_tracked_lifecycle(conn, "0xwatch", status="culled", reason="manual cull: noisy book", ts_ms=2)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    body = c.get("/api/hyperlive/analyzer?view=all&page_size=100").get_json()

    assert body["traders"][0]["wallet"] == "0xwatch"
    assert body["traders"][0]["lifecycle_status"] == "culled"
    assert body["traders"][0]["lifecycle_reason"] == "manual cull: noisy book"


def test_analyzer_bounds_attention_archive_search_and_page_size(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    for i in range(30):
        wallet = f"0xactive{i:02d}"
        IS.upsert_screened(
            conn,
            wallet,
            {"verdict": "watch", "archetype_pass": 1, "reason": "active review"},
            ts_ms=1_000 + i,
        )
        IS.upsert_tracked(
            conn, wallet, {"verdict": "watch", "archetype_pass": 1}, ts_ms=1_000 + i
        )
    for i in range(40):
        wallet = "0xculledexact" if i == 0 else f"0xarchive{i:02d}"
        IS.upsert_screened(
            conn,
            wallet,
            {"verdict": "market_maker", "reason": "archive rebate"},
            ts_ms=2_000 + i,
        )
    IS.set_copy_target(conn, "0xactive00", active=True, copy_tier="proven", ts_ms=3_000)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    first = c.get("/api/hyperlive/analyzer").get_json()
    archive = c.get(
        "/api/hyperlive/analyzer?view=archive&page=2&page_size=10"
    ).get_json()
    exact = c.get(
        "/api/hyperlive/analyzer?view=priority&q=0xculledexact"
    ).get_json()
    watchlist = c.get(
        "/api/hyperlive/analyzer?view=watchlist&page_size=100"
    ).get_json()
    attention_alias = c.get(
        "/api/hyperlive/analyzer?view=attention"
    ).get_json()
    filtered = c.get(
        "/api/hyperlive/analyzer?view=archive&q=rebate&verdict=rejected&page_size=10"
    ).get_json()
    capped = c.get("/api/hyperlive/analyzer?page_size=9999").get_json()

    assert first["page"] == 1
    assert first["page_size"] == 25
    assert [row["wallet"] for row in first["traders"]] == ["0xactive00"]
    assert first["view"] == "priority"
    assert first["total"] == 1
    assert first["has_more"] is False
    assert first["counts"] == {
        "priority": 1, "watchlist": 29, "archive": 40, "all": 70
    }
    assert watchlist["total"] == 29
    assert attention_alias["view"] == "priority"
    assert archive["page"] == 2 and len(archive["traders"]) == 10
    assert archive["total"] == 40 and archive["has_more"] is True
    assert [row["wallet"] for row in exact["traders"]] == ["0xculledexact"]
    assert filtered["total"] == 40 and len(filtered["traders"]) == 10
    assert capped["page_size"] == 100


def test_manual_cleaner_culls_tracked_wallet_and_disables_copy(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_tracked(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xwatch", active=True, copy_tier="watch_fallback", ts_ms=2)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    resp = c.post("/api/hyperlive/cleaner/manual",
                  json={"wallet": "0xWATCH", "reason": "bad paper copy", "confirm": True})
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["wallet"] == "0xwatch"
    assert body["lifecycle_status"] == "culled"
    assert body["reason"] == "manual cull: bad paper copy"
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    try:
        tracked = conn.execute("SELECT lifecycle_status, lifecycle_reason FROM hl_tracked_traders WHERE wallet='0xwatch'").fetchone()
        target = conn.execute("SELECT active, reason FROM hl_copy_targets WHERE wallet='0xwatch'").fetchone()
    finally:
        conn.close()
    assert tracked["lifecycle_status"] == "culled"
    assert tracked["lifecycle_reason"] == "manual cull: bad paper copy"
    assert target["active"] == 0
    assert target["reason"] == "manual cull: bad paper copy"


def test_manual_cleaner_requires_confirmation_without_mutation(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.upsert_tracked(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xwatch", active=True, copy_tier="watch_fallback", ts_ms=2)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    resp = c.post("/api/hyperlive/cleaner/manual", json={"wallet": "0xwatch", "reason": "fat finger"})

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["reason"] == "confirmation required"
    assert body["error"] == "confirmation required"
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    try:
        tracked = conn.execute("SELECT lifecycle_status, lifecycle_reason FROM hl_tracked_traders WHERE wallet='0xwatch'").fetchone()
        target = conn.execute("SELECT active FROM hl_copy_targets WHERE wallet='0xwatch'").fetchone()
    finally:
        conn.close()
    assert tracked["lifecycle_status"] == "active"
    assert tracked["lifecycle_reason"] == "screen archetype-pass"
    assert target["active"] == 1


def test_manual_cleaner_rejects_unknown_wallet(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    resp = c.post("/api/hyperlive/cleaner/manual", json={"wallet": "0xmissing", "reason": "junk", "confirm": True})

    assert resp.status_code == 404
    assert resp.get_json()["reason"] == "wallet not tracked"


def test_system_cleaner_runs_watch_lifecycle_and_returns_copy_state(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.upsert_tracked(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "market_maker", "archetype_pass": 0,
                                         "reason": "rebate farming"}, ts_ms=2)
    IS.set_copy_target(conn, "0xwatch", active=True, copy_tier="watch_fallback", ts_ms=3)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    resp = c.post("/api/hyperlive/cleaner/system", json={"max_watch_targets": 5, "confirm": True})
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["cleanup"]["culled"] == 1
    assert body["targets"] == []
    assert body["blocked_targets"] == []
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    try:
        tracked = conn.execute("SELECT lifecycle_status, lifecycle_reason FROM hl_tracked_traders WHERE wallet='0xwatch'").fetchone()
        target = conn.execute("SELECT active FROM hl_copy_targets WHERE wallet='0xwatch'").fetchone()
    finally:
        conn.close()
    assert tracked["lifecycle_status"] == "culled"
    assert "latest screen rejected" in tracked["lifecycle_reason"]
    assert target["active"] == 0


def test_system_cleaner_requires_confirmation_without_mutation(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.upsert_tracked(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "market_maker", "archetype_pass": 0,
                                         "reason": "rebate farming"}, ts_ms=2)
    IS.set_copy_target(conn, "0xwatch", active=True, copy_tier="watch_fallback", ts_ms=3)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    resp = c.post("/api/hyperlive/cleaner/system", json={"max_watch_targets": 5})

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["reason"] == "confirmation required"
    assert body["error"] == "confirmation required"
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    try:
        tracked = conn.execute("SELECT lifecycle_status, lifecycle_reason FROM hl_tracked_traders WHERE wallet='0xwatch'").fetchone()
        target = conn.execute("SELECT active FROM hl_copy_targets WHERE wallet='0xwatch'").fetchone()
    finally:
        conn.close()
    assert tracked["lifecycle_status"] == "active"
    assert tracked["lifecycle_reason"] == "screen archetype-pass"
    assert target["active"] == 1


def test_copy_route_reports_copy_target_stats(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_tracked(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xwatch", active=True, copy_tier="watch_fallback", ts_ms=2)
    cid = IS.open_copy(conn, "0xwatch", "ETH", "long", 1.0, ts_ms=3)
    IS.close_copy(conn, cid, exit_mark=1.0, pnl_bps=33.0, ts_ms=4)
    IS.open_copy(conn, "0xwatch", "BTC", "short", 2.0, copy_tier="watch_fallback", ts_ms=5)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    body = c.get("/api/hyperlive/copy").get_json()

    assert body["available"] is True
    assert body["targets"][0]["wallet"] == "0xwatch"
    assert body["targets"][0]["copy_status"]["copy_stats"]["mean_pnl_bps"] == 33.0
    assert body["targets"][0]["copy_status"]["copy_stats"]["by_tier"]["unknown"]["n_closed"] == 1
    assert body["open"][0]["copy_tier"] == "watch_fallback"
    assert body["recent_closed"][0]["copy_tier"] == "unknown"


def test_copy_set_rejects_manual_watch_fallback_activation(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.upsert_tracked(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    resp = c.post("/api/hyperlive/copy/set", json={"wallet": "0xwatch", "copy_tier": "watch_fallback"})
    body = resp.get_json()

    assert resp.status_code == 403
    assert body["ok"] is False
    assert body["copy_status"]["copy_tier"] == "proven"


def test_copy_set_allows_only_proven_wallets(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xproven", {"verdict": "watch", "archetype": "directional",
                                          "archetype_pass": 1, "month_roi": 1.0}, ts_ms=1)
    _seed_proven_hl_follow_stats(conn, "0xproven")
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    resp = c.post("/api/hyperlive/copy/set", json={"wallet": "0xproven"})
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True and body["effective_verdict"] == "proven"
    assert body["targets"] == ["0xproven"]


def test_copy_set_string_false_deactivates_without_proven_gate(tmp_path, monkeypatch):
    import sniper.routes.api_hyperlive as MOD
    from sniper.hyperlive import intel_store as IS

    intel_db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    conn = IS.connect(intel_db)
    IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xwatch", {"verdict": "watch", "archetype_pass": 1}, ts_ms=1)
    IS.set_copy_target(conn, "0xwatch", active=True, ts_ms=1)
    conn.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    resp = c.post("/api/hyperlive/copy/set", json={"wallet": "0xwatch", "active": "false"})
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["ok"] is True and body["active"] is False
    assert body["targets"] == []


def test_hyperlive_writer_migrates_old_intel_schema_before_read_routes(tmp_path, monkeypatch):
    import sqlite3
    import sniper.routes.api_hyperlive as MOD

    intel_db = os.path.join(str(tmp_path), "old_hl_intel.sqlite")
    conn = sqlite3.connect(intel_db)
    conn.executescript("""
        CREATE TABLE hl_tracked_traders (
            wallet TEXT PRIMARY KEY, label TEXT, verdict TEXT, archetype TEXT,
            taker_frac REAL, profit_factor REAL, win_rate REAL, n_closes INTEGER,
            month_roi REAL, realized REAL, archetype_pass INTEGER DEFAULT 0,
            added_ts_ms INTEGER, updated_ts_ms INTEGER
        );
        CREATE TABLE hl_trader_state (
            wallet TEXT PRIMARY KEY, positions_json TEXT NOT NULL, ts_ms INTEGER NOT NULL
        );
        CREATE TABLE hl_follow_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL, coin TEXT NOT NULL, side TEXT, kind TEXT,
            detect_ts_ms INTEGER NOT NULL, entry_mark REAL,
            status TEXT NOT NULL DEFAULT 'open',
            exit_ts_ms INTEGER, exit_mark REAL, net_bps REAL, exit_reason TEXT
        );
        CREATE TABLE hl_screen_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL,
            leaderboard_n INTEGER, prefiltered_n INTEGER, screened_n INTEGER, verdicts_json TEXT
        );
        CREATE TABLE hl_screened_traders (
            wallet TEXT PRIMARY KEY, verdict TEXT, archetype TEXT, taker_frac REAL,
            profit_factor REAL, win_rate REAL, n_closes INTEGER, top_coin_frac REAL,
            single_fill_frac REAL, month_roi REAL, realized REAL, account_value REAL,
            reason TEXT, archetype_pass INTEGER DEFAULT 0, ts_ms INTEGER
        );
        CREATE TABLE hl_copy_targets (
            wallet TEXT PRIMARY KEY, active INTEGER DEFAULT 1, label TEXT, added_ts_ms INTEGER
        );
        CREATE TABLE hl_copy_book (
            id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, coin TEXT NOT NULL,
            side TEXT, entry_mark REAL, detect_ts_ms INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', exit_ts_ms INTEGER, exit_mark REAL, pnl_bps REAL
        );
        INSERT INTO hl_tracked_traders(wallet, verdict, archetype_pass, month_roi, added_ts_ms, updated_ts_ms)
        VALUES ('0xold', 'watch', 1, 1.0, 1, 1);
        INSERT INTO hl_screened_traders(wallet, verdict, archetype_pass, month_roi, ts_ms)
        VALUES ('0xold', 'watch', 1, 1.0, 1);
    """)
    conn.commit()
    conn.close()
    from sniper.hyperlive import intel_store as IS
    writer = IS.connect(intel_db)
    IS.ensure_schema(writer)
    writer.close()
    monkeypatch.setattr(MOD, "INTEL_DB", intel_db)
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))

    intel = c.get("/api/hyperlive/intel")
    analyzer = c.get("/api/hyperlive/analyzer")

    assert intel.status_code == 200
    assert analyzer.status_code == 200
    assert intel.get_json()["cohort"][0]["wallet"] == "0xold"
    conn = sqlite3.connect(intel_db)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(hl_copy_book)")}
    finally:
        conn.close()
    assert "copy_tier" in cols


def test_trader_route_returns_assessment(monkeypatch):
    from sniper.routes import api_hyperlive as AH
    from sniper.markets import hyperliquid_api as HL
    fills = [{"coin": "BTC", "dir": "Close Long", "closedPnl": "12.0", "fee": "0.1", "crossed": True}]
    monkeypatch.setattr(HL, "fetch_user_fills", lambda w: fills)
    app = _make_app(AH.create_api_hyperlive_blueprint(db_path="/nonexistent.sqlite"))
    r = app.test_client().get("/api/hyperlive/trader/0xabc")
    assert r.status_code == 200
    body = r.get_json()
    assert body["available"] is True
    assert body["wallet"] == "0xabc"
    assert "verdict" in body and isinstance(body["breakdown"], list)


def test_trader_route_handles_fetch_error(monkeypatch):
    from sniper.routes import api_hyperlive as AH
    from sniper.markets import hyperliquid_api as HL
    def boom(w): raise TimeoutError("slow")
    monkeypatch.setattr(HL, "fetch_user_fills", boom)
    app = _make_app(AH.create_api_hyperlive_blueprint(db_path="/nonexistent.sqlite"))
    r = app.test_client().get("/api/hyperlive/trader/0xabc")
    assert r.get_json()["available"] is False
    assert r.get_json()["reason"] == "TimeoutError"


def test_analyzer_emits_two_tier_verdict(tmp_path, monkeypatch):
    import sniper.hyperlive.intel_store as IS
    from sniper.routes import api_hyperlive as AH
    db = str(tmp_path / "hl_intel.sqlite")
    conn = IS.connect(db); IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xa", {"verdict": "watch", "archetype": "directional",
                                     "archetype_pass": 1, "month_roi": 0.5}, ts_ms=1)
    monkeypatch.setattr(AH, "INTEL_DB", db)
    app = _make_app(AH.create_api_hyperlive_blueprint(db_path="/nonexistent.sqlite"))
    body = app.test_client().get(
        "/api/hyperlive/analyzer?view=all&page_size=100"
    ).get_json()
    t = [x for x in body["traders"] if x["wallet"] == "0xa"][0]
    assert t["effective_verdict"] == "watch"        # archetype-pass but no follow proof yet
    assert "follow" in t                              # per-wallet stats key present (null/empty ok)


def test_cascade_route_no_db_is_graceful(tmp_path):
    c = _app(os.path.join(str(tmp_path), "missing.sqlite"))
    j = c.get("/api/hyperlive/cascade").get_json()
    assert j["available"] is False and j["status"] == "none" and j["n_obs"] == 0


def test_cascade_route_reports_status(tmp_path):
    from sniper.hyperlive import persistence as P
    db = os.path.join(str(tmp_path), "engine.sqlite")
    conn = P.connect(db)
    P.ensure_schema(conn)
    P.save_cascade_obs(conn, {"BTC": (100.0, 1e9, 5, 1e5)}, ts_ms=1000)
    P.save_cascade_audit(conn, {"status": "no-edge", "n_events": 60, "edges": []}, ts_ms=2000)
    conn.close()

    c = _app(db)
    j = c.get("/api/hyperlive/cascade").get_json()
    assert j["available"] is True and j["status"] == "no-edge"
    assert j["n_obs"] == 1 and j["verdict"]["n_events"] == 60


def test_runtime_far_whale_lane_sparse_sample_labelling(tmp_path):
    from sniper.hyperlive import far_whale_store as S
    from sniper.routes.api_hyperlive import _runtime_far_whale_data, _runtime_payload
    db = str(tmp_path / "hl.sqlite")
    conn = S.connect(db)
    S.ensure_schema(conn)
    conn.close()
    fw = _runtime_far_whale_data(db)
    assert fw["status"] == "sweep_mode" or fw["status"] == "not_started"   # tables exist, empty
    payload = _runtime_payload(db)
    lane = next(l for l in payload["lanes"] if l["id"] == "far_whale_magnet")
    assert "Sparse visible whale sample — NOT full liquidation density." in lane["note"]
    assert "liquidation book" not in lane["note"].lower()
    assert "crowd cluster" not in lane["note"].lower()
    assert "far_whale" in payload["data"]
