"""Read-only API surface for Polymarket crypto-5m capture health."""
from __future__ import annotations

import os
import sqlite3

from flask import Flask

from sniper.algos.polymarket_crypto5m import forward_harness as FH
from sniper.algos.polymarket_crypto5m import forward_persistence as FPP
from sniper.algos.polymarket_crypto5m import forward_rules as FR
from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m.models import Settlement
from sniper.routes.api_crypto5m import create_api_crypto5m_blueprint


def _client(db_path: str, forward_db_path: str = "", quant_db_path: str = "", maker_db_path: str = "",
            adaptive_maker_db_path: str = "", adaptive_control_maker_db_path: str = ""):
    app = Flask(__name__)
    app.register_blueprint(create_api_crypto5m_blueprint(
        {"db_path": db_path, "forward_db_path": forward_db_path, "quant_db_path": quant_db_path,
         "maker_db_path": maker_db_path, "adaptive_maker_db_path": adaptive_maker_db_path,
         "adaptive_control_maker_db_path": adaptive_control_maker_db_path}))
    return app.test_client()


def test_status_reports_missing_capture_db_without_creating_it(tmp_path):
    db_path = tmp_path / "missing.sqlite"
    body = _client(str(db_path)).get("/api/crypto5m/status").get_json()
    assert body["available"] is False
    assert body["phase"] == 0
    assert body["report"]["settlements"]["resolved"] == 0
    assert "capture DB not found" in body["message"]
    assert not os.path.exists(db_path)


def test_status_reads_capture_report_from_existing_db(tmp_path):
    db_path = tmp_path / "capture.sqlite"
    conn = P.connect(str(db_path))
    P.ensure_schema(conn)
    P.record_settlement(conn, Settlement("0xready", "up-token", "Up", "2026-06-20T12:00:00Z",
                                         "resolved", ("1", "0"), "gamma", "agree", 100.0),
                        code_version="test", now_ts=1)
    conn.close()

    body = _client(str(db_path)).get("/api/crypto5m/status").get_json()
    assert body["available"] is True
    assert body["phase"] == 0
    assert body["report"]["settlements"]["resolved"] == 1
    assert "No paper trades" in body["disclaimer"]


UP_BOOK = {"best_bid": 0.60, "best_ask": 0.62, "bids": [[0.60, 800]], "asks": [[0.62, 1000]],
           "book_hash": "u", "book_integrity_status": "usable"}
DOWN_BOOK = {"best_bid": 0.38, "best_ask": 0.40, "bids": [[0.38, 200]], "asks": [[0.40, 1000]],
             "book_hash": "d", "book_integrity_status": "usable"}


def test_forward_status_reports_missing_db_without_creating_it(tmp_path):
    fwd = tmp_path / "missing_forward.sqlite"
    body = _client("", str(fwd)).get("/api/crypto5m/forward-status").get_json()
    assert body["available"] is False
    assert body["report"]["campaign"] is None
    assert body["report"]["milestones"]["targets"] == [200, 500, 1000]
    assert "forward paper DB not found" in body["message"]
    assert not os.path.exists(fwd)


def test_forward_status_reads_campaign_from_existing_db(tmp_path):
    fwd = tmp_path / "forward.sqlite"
    conn = P.connect(str(fwd))
    FPP.ensure_forward_schema(conn)
    FPP.start_campaign(conn, campaign_id=FR.CAMPAIGN_ID, manifest=FR.RULE_MANIFEST,
                       manifest_hash=FR.manifest_hash(), git_commit="deadbeef", now_ts=1000)
    ctx = FH.build_context(binance_now=101.0, binance_30s_ago=100.0, up_book=UP_BOOK, down_book=DOWN_BOOK)
    FH.persist_records(conn, campaign_id=FR.CAMPAIGN_ID, condition_id="0x1", rule_version="v0",
                       checkpoint=240, decision_ts=1240,
                       records=FH.decide_market(condition_id="0x1", ctx=ctx, up_book=UP_BOOK,
                                                down_book=DOWN_BOOK, up_token="UP", down_token="DOWN",
                                                decision_ts=1240, fill_book_ts=1241, gross=10.0,
                                                min_order=5, latency_ms=500, book_quality="usable"),
                       now_ts=1)
    FH.settle_market(conn, campaign_id=FR.CAMPAIGN_ID, condition_id="0x1", winning_outcome="Up",
                     reconciliation_status="gamma_only", now_ts=3)
    conn.close()

    body = _client("", str(fwd)).get("/api/crypto5m/forward-status").get_json()
    assert body["available"] is True
    assert body["report"]["campaign"]["manifest_hash"] == FR.manifest_hash()
    assert body["report"]["totals"]["n_resolved"] == 5
    assert "PAPER" in body["disclaimer"]


def test_quant_status_reports_missing_db_without_creating_it(tmp_path):
    qdb = tmp_path / "missing_quant.sqlite"
    body = _client("", "", str(qdb)).get("/api/crypto5m/quant-status").get_json()
    assert body["available"] is False
    assert body["report"]["campaign"] is None
    assert body["report"]["eval"]["n_resolved_actionable"] == 0
    assert "quant shadow DB not found" in body["message"]
    assert not os.path.exists(qdb)


def test_quant_status_reads_campaign_from_existing_db(tmp_path):
    from sniper.algos.polymarket_crypto5m import quant_persistence as QPP
    qdb = tmp_path / "quant.sqlite"
    conn = P.connect(str(qdb))
    QPP.ensure_quant_schema(conn)
    QPP.start_campaign(conn, campaign_id="btc5m-quant-shadow-v1", config={"model_version": "v1"},
                       git_commit="deadbeef", now_ts=100, model_version="v1")
    conn.close()
    body = _client("", "", str(qdb)).get("/api/crypto5m/quant-status").get_json()
    assert body["available"] is True
    assert body["report"]["campaign"]["model_version"] == "v1"
    assert "SHADOW" in body["disclaimer"]


def test_maker_fills_explain_cash_edge_and_settlement_pnl(tmp_path):
    import json
    import sqlite3
    import time

    maker = tmp_path / "maker.sqlite"
    conn = sqlite3.connect(maker)
    conn.executescript("""
      CREATE TABLE maker_state(id INTEGER PRIMARY KEY CHECK(id=1), think_json TEXT, ts INTEGER);
      CREATE TABLE maker_windows(
        condition_id TEXT PRIMARY KEY, window_start_ts INTEGER, up_inv REAL, down_inv REAL, cash REAL,
        realized_pnl REAL, settled INTEGER DEFAULT 0, winning_outcome TEXT, ts INTEGER,
        up_vwap REAL, down_vwap REAL, campaign_id TEXT);
      CREATE TABLE maker_fills(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
        token TEXT, side TEXT, price REAL, size REAL, fair REAL, ts INTEGER, reason TEXT, fill_model TEXT);
    """)
    now = int(time.time())
    think = {"campaign": "maker-unified-v4", "engine_version": "maker-unified-v4", "seconds_left": 100,
             "window_start_ts": 1300}
    conn.execute("INSERT INTO maker_state(id,think_json,ts) VALUES(1,?,?)", (json.dumps(think), now))
    conn.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,realized_pnl,
                  settled,winning_outcome,ts,up_vwap,down_vwap,campaign_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                 ("0xw", 1000, 40.0, 0.0, -13.0, 27.0, 1, "Up", now, 0.40, None,
                  "maker-unified-v4"))
    conn.execute("""INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,price,size,fair,ts,reason,fill_model)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                 ("maker-unified-v4", "0xw", 1000, "up", "bid", 0.40, 50.0, 0.46, 1010,
                  "entry", "print"))
    conn.execute("""INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,price,size,fair,ts,reason,fill_model)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                 ("maker-unified-v4", "0xw", 1000, "up", "ask", 0.70, 10.0, 0.65, 1020,
                  "scale out", "print"))
    conn.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,realized_pnl,
                  settled,winning_outcome,ts,up_vwap,down_vwap,campaign_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                 ("0xp", 700, 10.0, 0.0, -5.0, None, 0, None, now, 0.50, None,
                  "maker-unified-v4"))
    conn.execute("""INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,price,size,fair,ts,reason,fill_model)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                 ("maker-unified-v4", "0xp", 700, "up", "bid", 0.50, 10.0, 0.55, 1030,
                  "closed but awaiting settlement", "print"))
    conn.commit()
    conn.close()

    body = _client("", maker_db_path=str(maker)).get("/api/crypto5m/maker").get_json()
    fills = body["recent_fills"]
    pending, sell, buy = fills[0], fills[1], fills[2]

    assert pending["cash_delta"] == -5.0
    assert pending["edge_value"] == 0.5
    assert pending["settlement_pnl"] is None
    assert pending["window_status"] == "pending"
    assert sell["cash_delta"] == 7.0
    assert sell["edge_value"] == 0.5
    assert sell["settlement_pnl"] == -3.0
    assert sell["window_realized_pnl"] == 27.0
    assert sell["window_status"] == "settled"
    assert buy["cash_delta"] == -20.0
    assert buy["edge_value"] == 3.0
    assert buy["settlement_pnl"] == 30.0
    assert buy["window_realized_pnl"] == 27.0
    assert buy["window_status"] == "settled"


def test_maker_api_reports_settlement_identity_breaks(tmp_path):
    import json
    import sqlite3
    import time

    maker = tmp_path / "maker.sqlite"
    conn = sqlite3.connect(maker)
    conn.executescript("""
      CREATE TABLE maker_state(id INTEGER PRIMARY KEY CHECK(id=1), think_json TEXT, ts INTEGER);
      CREATE TABLE maker_windows(
        condition_id TEXT PRIMARY KEY, window_start_ts INTEGER, up_inv REAL, down_inv REAL, cash REAL,
        realized_pnl REAL, settled INTEGER DEFAULT 0, winning_outcome TEXT, ts INTEGER,
        up_vwap REAL, down_vwap REAL, campaign_id TEXT);
      CREATE TABLE maker_fills(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
        token TEXT, side TEXT, price REAL, size REAL, fair REAL, ts INTEGER, reason TEXT, fill_model TEXT);
    """)
    now = int(time.time())
    think = {"campaign": "maker-unified-v4", "engine_version": "maker-unified-v4", "seconds_left": 100,
             "window_start_ts": 1300}
    conn.execute("INSERT INTO maker_state(id,think_json,ts) VALUES(1,?,?)", (json.dumps(think), now))
    conn.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,realized_pnl,
                  settled,winning_outcome,ts,up_vwap,down_vwap,campaign_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                 ("0xpoison", 1000, 0.0, 0.0, 0.0, 51.25, 1, "Up", now, None, None,
                  "maker-unified-v4"))
    conn.execute("""INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,price,size,fair,ts,reason,fill_model)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                 ("maker-unified-v4", "0xpoison", 1000, "up", "bid", 0.40, 50.0, 0.46, 1010,
                  "entry", "print"))
    conn.commit()
    conn.close()

    body = _client("", maker_db_path=str(maker)).get("/api/crypto5m/maker").get_json()
    identity = body["session"]["settlement_identity"]
    win = body["recent_windows"][0]

    assert identity["checked"] == 1
    assert identity["breaks"] == 1
    assert identity["max_abs_error"] == 21.25
    assert body["session"]["realized_pnl"] == 30.0
    assert body["session"]["stored_realized_pnl"] == 51.25
    assert body["session"]["realized_pnl_source"] == "fill_ledger_when_available"
    assert win["ledger_realized_pnl"] == 30.0
    assert win["settlement_identity_error"] == 21.25
    assert win["settlement_identity_ok"] is False


def test_maker_api_reports_regime_shadow_by_regime_stats(tmp_path):
    import json
    import time

    from scripts import run_polymarket_crypto5m_maker as MK
    from sniper.algos.polymarket_crypto5m import maker_policy as MP
    from sniper.algos.polymarket_crypto5m import maker_regime as RG

    maker = tmp_path / "maker.sqlite"
    conn = MK._db(str(maker))
    now = int(time.time())
    live_campaign = "maker-unified-v4"
    shadow_campaign = "maker-regime-shadow-v1"
    think = {
        "campaign": live_campaign,
        "engine_version": "maker-unified-v4",
        "window_start_ts": 1300,
        "regime_shadow": {"campaign": shadow_campaign},
    }
    conn.execute("INSERT INTO maker_state(id,think_json,ts) VALUES(1,?,?)", (json.dumps(think), now))

    mid_policy = RG.MakerRegimePolicy(
        regime_id="mid_chop", regime_confidence="high", reason="calm mid-market chop")
    jump_policy = RG.MakerRegimePolicy(
        regime_id="jump", regime_confidence="medium", reason="jump/divergence state")
    conn.execute("""INSERT INTO maker_shadow_windows(campaign_id,condition_id,window_start_ts,up_inv,
                  down_inv,cash,realized_pnl,settled,winning_outcome,up_vwap,down_vwap,ts)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (shadow_campaign, "0xsettled", 1000, 0.0, 0.0, 5.5, 5.5, 1, "Up", None, None, now))
    wb = MK.WindowBook("0xopen", 1300)
    MK.record_fill(
        conn, shadow_campaign, wb, "up", "bid", 0.47, 12.0, 0.51,
        now_ts=1310, reason="entry", fill_model="print", regime_policy=mid_policy,
        regime_inputs_hash="hash-mid", margin_used=0.0085, clip_used=12.0,
        max_shares_used=50.0, min_edge_used=0.04, min_seconds_quote_used=30.0)
    MK.persist_shadow_window(conn, shadow_campaign, wb)
    MK.persist_regime_decision(
        conn, shadow_campaign, "0xopen", 1300, "up", mid_policy, inputs_hash="hash-mid",
        margin_used=0.0085, clip_used=12.0, max_shares_used=50.0, min_edge_used=0.04,
        min_seconds_quote_used=30.0, now_ts=1310)
    MK.persist_regime_decision(
        conn, shadow_campaign, "0xopen", 1300, "down", mid_policy, inputs_hash="hash-mid-down",
        margin_used=0.0085, clip_used=12.0, max_shares_used=50.0, min_edge_used=0.04,
        min_seconds_quote_used=30.0, now_ts=1311)
    MK.persist_regime_decision(
        conn, shadow_campaign, "0xopen", 1300, "up", jump_policy, inputs_hash="hash-jump",
        margin_used=0.02, clip_used=5.0, max_shares_used=25.0, min_edge_used=0.04,
        min_seconds_quote_used=30.0, now_ts=1312)
    MK.persist_quote_snapshot(
        conn, shadow_campaign, "0xopen", 1300, "up",
        {"bid": MP.Quote("bid", 0.47, 12.0), "ask": None, "reason": "mid quote"},
        fair=0.51, best_bid=0.46, best_ask=0.50, seconds_left=120, now_ts=1310,
        regime_policy=mid_policy, regime_inputs_hash="hash-mid", margin_used=0.0085,
        clip_used=12.0, max_shares_used=50.0, min_edge_used=0.04, min_seconds_quote_used=30.0)
    MK.persist_quote_snapshot(
        conn, shadow_campaign, "0xopen", 1300, "up",
        {"bid": None, "ask": None, "reason": "jump pulled"},
        fair=0.51, best_bid=0.46, best_ask=0.50, seconds_left=120, now_ts=1312,
        regime_policy=jump_policy, regime_inputs_hash="hash-jump", margin_used=0.02,
        clip_used=5.0, max_shares_used=25.0, min_edge_used=0.04, min_seconds_quote_used=30.0)
    conn.commit()
    conn.close()

    body = _client("", maker_db_path=str(maker)).get("/api/crypto5m/maker").get_json()
    shadow = body["regime_shadow"]

    assert shadow["available"] is True
    assert shadow["campaign"] == shadow_campaign
    assert shadow["windows"]["n_settled"] == 1
    assert shadow["windows"]["n_open"] == 1
    assert shadow["windows"]["realized_pnl"] == 5.5
    assert shadow["by_regime"]["mid_chop"]["decisions"] == 2
    assert shadow["by_regime"]["mid_chop"]["quotes_posted"] == 1
    assert shadow["by_regime"]["mid_chop"]["quotes_pulled"] == 1
    assert shadow["by_regime"]["mid_chop"]["fills"] == 1
    assert shadow["by_regime"]["mid_chop"]["cash_delta"] == -5.64
    assert shadow["by_regime"]["mid_chop"]["edge_value"] == 0.48
    assert shadow["by_regime"]["jump"]["decisions"] == 1
    assert shadow["by_regime"]["jump"]["quotes_posted"] == 0
    assert shadow["by_regime"]["jump"]["quotes_pulled"] == 2


def test_maker_api_discovers_shadow_campaign_without_binding_live_campaign_chars(tmp_path):
    import json
    import time

    from scripts import run_polymarket_crypto5m_maker as MK
    from sniper.algos.polymarket_crypto5m import maker_regime as RG

    maker = tmp_path / "maker.sqlite"
    conn = MK._db(str(maker))
    now = int(time.time())
    live_campaign = "maker-unified-v4"
    shadow_campaign = "maker-regime-shadow-v1"
    think = {
        "campaign": live_campaign,
        "engine_version": "maker-unified-v4",
        "window_start_ts": 1300,
    }
    conn.execute("INSERT INTO maker_state(id,think_json,ts) VALUES(1,?,?)", (json.dumps(think), now))
    policy = RG.MakerRegimePolicy(regime_id="mid_chop", regime_confidence="high", reason="calm")
    MK.persist_regime_decision(
        conn, shadow_campaign, "0xopen", 1300, "up", policy, inputs_hash="hash-mid",
        margin_used=0.0085, clip_used=12.0, max_shares_used=50.0, min_edge_used=0.04,
        min_seconds_quote_used=30.0, now_ts=1310)
    conn.commit()
    conn.close()

    body = _client("", maker_db_path=str(maker)).get("/api/crypto5m/maker").get_json()

    assert "message" not in body
    assert body["regime_shadow"]["campaign"] == shadow_campaign
    assert body["regime_shadow"]["by_regime"]["mid_chop"]["decisions"] == 1


def test_maker_api_suppresses_stale_shadow_when_active_regime_is_running(tmp_path):
    import json
    import time

    from scripts import run_polymarket_crypto5m_maker as MK
    from sniper.algos.polymarket_crypto5m import maker_regime as RG

    maker = tmp_path / "maker.sqlite"
    conn = MK._db(str(maker))
    now = int(time.time())
    think = {
        "campaign": "maker-regime-v1",
        "engine_version": "maker-unified-v4",
        "window_start_ts": 1300,
        "regime_active": {"up": {"regime_id": "mid_chop"}},
    }
    conn.execute("INSERT INTO maker_state(id,think_json,ts) VALUES(1,?,?)", (json.dumps(think), now))
    policy = RG.MakerRegimePolicy(regime_id="mid_chop", regime_confidence="high", reason="old shadow")
    MK.persist_regime_decision(
        conn, "maker-regime-shadow-v1", "0xopen", 1300, "up", policy, inputs_hash="hash-mid",
        margin_used=0.0085, clip_used=12.0, max_shares_used=50.0, min_edge_used=0.04,
        min_seconds_quote_used=30.0, now_ts=1310)
    conn.commit()
    conn.close()

    body = _client("", maker_db_path=str(maker)).get("/api/crypto5m/maker").get_json()

    assert body["session"]["campaign"] == "maker-regime-v1"
    assert body["session"]["eval_min_settled"] == 200
    assert body["session"]["interim_eval_min_settled"] == 60
    assert body["session"]["eval_ready"] is False
    assert body["regime_shadow"]["available"] is False
    assert body["regime_shadow"]["campaign"] is None


def test_maker_api_surfaces_regime_state_from_current_think(tmp_path):
    import json
    import time

    from scripts import run_polymarket_crypto5m_maker as MK

    maker = tmp_path / "maker.sqlite"
    conn = MK._db(str(maker))
    now = int(time.time())
    think = {
        "campaign": "maker-regime-v1",
        "engine_version": "maker-unified-v4",
        "window_start_ts": 1300,
        "seconds_left": 90,
        "regime_episode": {
            "episode_id": "ep_1",
            "current_regime": "mid_chop",
            "dwell_seconds": 12.0,
            "moment_key": {"window_phase": "mid_window", "odds_bucket": "mid_chop"},
        },
        "profile_decisions": {
            "up": {"profile_id": "quote_wide", "profile_mode": "log_only"},
            "down": {"profile_id": "quote_wide", "profile_mode": "log_only"},
        },
        "regime_active": {"up": {"regime_id": "mid_chop"}},
    }
    conn.execute("INSERT INTO maker_state(id,think_json,ts) VALUES(1,?,?)", (json.dumps(think), now))
    conn.commit()
    conn.close()

    body = _client("", maker_db_path=str(maker)).get("/api/crypto5m/maker").get_json()

    assert body["regime_state"]["available"] is True
    assert body["regime_state"]["current_regime"] == "mid_chop"
    assert body["regime_state"]["dwell_seconds"] == 12.0
    assert body["regime_state"]["moment_key"]["odds_bucket"] == "mid_chop"
    assert body["regime_state"]["profile_mode"] == "log_only"
    assert body["regime_state"]["profile_decisions"]["up"]["profile_id"] == "quote_wide"


def test_settlement_identity_refuses_without_campaign_filter():
    # campaign=None would replay main + shadow fills for the same condition_id mixed together —
    # the helper must refuse rather than report a phantom identity break
    from sniper.routes.api_crypto5m import _maker_settlement_identity

    window = {"settled": 1, "winning_outcome": "Up", "realized_pnl": 10.0, "condition_id": "0xc"}
    assert _maker_settlement_identity(None, window, None) is None
    assert _maker_settlement_identity(None, window, "") is None


def _mk_maker_db(tmp_path, think_campaign="maker-unified-v4"):
    import json
    import sqlite3
    import time

    maker = tmp_path / "maker.sqlite"
    conn = sqlite3.connect(maker)
    conn.executescript("""
      CREATE TABLE maker_state(id INTEGER PRIMARY KEY CHECK(id=1), think_json TEXT, ts INTEGER);
      CREATE TABLE maker_windows(
        condition_id TEXT PRIMARY KEY, window_start_ts INTEGER, up_inv REAL, down_inv REAL, cash REAL,
        realized_pnl REAL, settled INTEGER DEFAULT 0, winning_outcome TEXT, ts INTEGER,
        up_vwap REAL, down_vwap REAL, campaign_id TEXT);
      CREATE TABLE maker_fills(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
        token TEXT, side TEXT, price REAL, size REAL, fair REAL, ts INTEGER, reason TEXT, fill_model TEXT);
    """)
    now = int(time.time())
    if think_campaign is not None:
        think = {"campaign": think_campaign, "engine_version": think_campaign, "seconds_left": 100,
                 "window_start_ts": 1300}
        conn.execute("INSERT INTO maker_state(id,think_json,ts) VALUES(1,?,?)", (json.dumps(think), now))
    return maker, conn, now


def test_maker_session_counts_the_whole_campaign_not_a_page(tmp_path):
    # the counter froze at the old LIMIT 200 right at the n>=200 eval bar — aggregates must be unpaged
    maker, conn, now = _mk_maker_db(tmp_path)
    for i in range(250):
        conn.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,
                      realized_pnl,settled,winning_outcome,ts,campaign_id) VALUES(?,?,?,?,?,?,1,?,?,?)""",
                     (f"0x{i:04d}", 1000 + 300 * i, 0.0, 0.0, 0.0, 1.0, "Up", now, "maker-unified-v4"))
        # matching fill so the ledger agrees: buy 2 up @0.50 (cash -1), Up wins -> payout +2 -> realized +1
        conn.execute("""INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,price,
                      size,fair,ts,reason,fill_model) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                     ("maker-unified-v4", f"0x{i:04d}", 1000 + 300 * i, "up", "bid", 0.50, 2.0, 0.55,
                      1010 + 300 * i, "entry", "print"))
    conn.commit(); conn.close()

    body = _client("", maker_db_path=str(maker)).get("/api/crypto5m/maker").get_json()
    assert body["session"]["n_settled"] == 250
    assert body["session"]["realized_pnl"] == 250.0
    assert body["session"]["campaign"] == "maker-unified-v4"
    assert len(body["recent_windows"]) == 20   # display stays paged


def test_maker_session_reports_open_pending_and_unresolved_windows(tmp_path):
    maker, conn, now = _mk_maker_db(tmp_path, think_campaign="maker-regime-v1")
    rows = [
        ("0xsettled", 400, 0.0, 0.0, 0.0, 1.0, 1, "Up", "maker-regime-v1"),
        ("0xunresolved", 700, 0.0, 4.0, -6.0, None, 1, "unresolved", "maker-regime-v1"),
        ("0xpending", 1000, 0.0, 4.0, -3.0, None, 0, None, "maker-regime-v1"),
        ("0xopen", 1300, 5.0, 0.0, -2.0, None, 0, None, "maker-regime-v1"),
    ]
    conn.executemany("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,
                      realized_pnl,settled,winning_outcome,ts,campaign_id)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""",
                     [(cid, ws, up, down, cash, realized, settled, winner, now, campaign)
                      for cid, ws, up, down, cash, realized, settled, winner, campaign in rows])
    conn.execute("""INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,price,
                  size,fair,ts,reason,fill_model) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                 ("maker-regime-v1", "0xsettled", 400, "up", "bid", 0.50, 2.0, 0.55, now,
                  "entry", "print"))
    conn.commit(); conn.close()

    body = _client("", maker_db_path=str(maker)).get("/api/crypto5m/maker").get_json()

    assert body["session"]["n_settled"] == 1
    assert body["session"]["realized_pnl"] == 1.0
    assert body["session"]["n_open"] == 1
    assert body["session"]["open_cash"] == -2.0
    assert body["session"]["n_pending"] == 1
    assert body["session"]["pending_cash"] == -3.0
    assert body["session"]["n_unresolved"] == 1
    assert body["session"]["unresolved_cash"] == -6.0
    assert "excluded from realized PnL" in body["session"]["unresolved_note"]


def test_maker_api_surfaces_accounting_ladder_and_regime_eval(tmp_path):
    import json
    import time

    from scripts import run_polymarket_crypto5m_maker as MK

    maker = tmp_path / "maker.sqlite"
    conn = MK._db(str(maker))
    now = int(time.time())
    think = {
        "campaign": "maker-regime-v1",
        "engine_version": "maker-unified-v4",
        "seconds_left": 100,
        "window_start_ts": 1300,
        "unrealized_pnl": 2.25,
    }
    conn.execute("INSERT INTO maker_state(id,think_json,ts) VALUES(1,?,?)", (json.dumps(think), now))
    conn.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,
                  realized_pnl,settled,winning_outcome,ts,campaign_id)
                  VALUES(?,?,?,?,?,?,1,?,?,?)""",
                 ("0xsettled", 1000, 10.0, 0.0, -4.0, 6.0, "Up", now, "maker-regime-v1"))
    conn.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,
                  realized_pnl,settled,winning_outcome,ts,campaign_id)
                  VALUES(?,?,?,?,?,?,0,?,?,?)""",
                 ("0xpending", 700, 5.0, 1.0, -2.0, None, None, now, "maker-regime-v1"))
    moment = json.dumps({"window_phase": "mid_window", "odds_bucket": "mid_chop",
                         "volatility_state": "calm", "inventory_state": "flat",
                         "spread_state": "healthy", "direction_state": "unconfirmed",
                         "book_health": "healthy"}, sort_keys=True)
    conn.execute("""INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,price,
                  size,fair,ts,reason,fill_model,regime_id,profile_id,episode_id,moment_key)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 ("maker-regime-v1", "0xsettled", 1000, "up", "bid", 0.40, 10.0, 0.45, now - 30,
                  "entry", "print", "mid_chop", "quote_wide", "ep-1", moment))
    conn.execute("""INSERT INTO maker_quotes(campaign_id,condition_id,window_start_ts,token,side,status,
                  price,size,fair,best_bid,best_ask,seconds_left,ts,reason,regime_id,profile_id,episode_id,
                  moment_key)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 ("maker-regime-v1", "0xsettled", 1000, "up", "bid", "posted", 0.47, 1.0, None,
                  0.47, 0.49, 100, now - 20, "book", "mid_chop", "quote_wide", "ep-1", moment))
    conn.commit(); conn.close()

    body = _client("", maker_db_path=str(maker)).get("/api/crypto5m/maker").get_json()

    ladder = body["session"]["accounting_ladder"]
    assert ladder["settled_realized_pnl"] == 6.0
    assert ladder["current_open_mark_pnl"] == 2.25
    assert ladder["settled_plus_open_mark_pnl"] == 8.25
    assert ladder["pending_payout_min"] == -1.0
    assert ladder["pending_payout_max"] == 3.0
    assert body["regime_eval"]["available"] is True
    assert body["regime_eval"]["by_context_profile"][0]["profile_id"] == "quote_wide"
    assert body["regime_eval"]["campaign_eval"]["n_settled"] == 1
    assert body["regime_eval"]["campaign_eval"]["eval_ready"] is False
    assert body["regime_eval"]["campaign_eval"]["sample_gate"]["min_windows"] == 200
    assert body["session"]["eval_min_settled"] == 200
    assert body["session"]["interim_eval_min_settled"] == 60
    assert body["session"]["interim_eval_ready"] is False
    assert body["recent_fills"][0]["adverse_selection_10s_bps"] == 700.0
    assert body["recent_fills"][0]["profile_id"] == "quote_wide"


def test_maker_adaptive_reports_missing_dbs_without_creating_them(tmp_path):
    control = tmp_path / "missing_control.sqlite"
    adaptive = tmp_path / "missing_adaptive.sqlite"

    body = _client("", maker_db_path=str(control), adaptive_maker_db_path=str(adaptive)).get(
        "/api/crypto5m/maker-adaptive"
    ).get_json()

    assert body["available"] is False
    assert body["control"]["available"] is False
    assert body["adaptive"]["available"] is False
    assert "control DB not found" in body["warnings"]
    assert "adaptive DB not found" in body["warnings"]
    assert not control.exists()
    assert not adaptive.exists()


def test_maker_adaptive_pairs_settled_windows_and_surfaces_current_profile(tmp_path):
    import json
    import sqlite3
    import time

    control = tmp_path / "control.sqlite"
    adaptive = tmp_path / "adaptive.sqlite"
    now = int(time.time())
    for path, campaign, pnl in (
        (control, "maker-regime-v1", 10.0),
        (adaptive, "maker-adaptive-paper-v1", 13.5),
    ):
        conn = sqlite3.connect(path)
        conn.executescript("""
          CREATE TABLE maker_state(id INTEGER PRIMARY KEY CHECK(id=1), think_json TEXT, ts INTEGER);
          CREATE TABLE maker_windows(
            condition_id TEXT PRIMARY KEY, window_start_ts INTEGER, up_inv REAL, down_inv REAL, cash REAL,
            realized_pnl REAL, settled INTEGER DEFAULT 0, winning_outcome TEXT, ts INTEGER,
            up_vwap REAL, down_vwap REAL, campaign_id TEXT);
          CREATE TABLE maker_fills(
            id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
            token TEXT, side TEXT, price REAL, size REAL, fair REAL, ts INTEGER, reason TEXT, fill_model TEXT,
            profile_id TEXT, fill_profile_id TEXT, selection_mode TEXT, adaptive_context_key TEXT);
        """)
        think = {"campaign": campaign, "engine_version": campaign, "window_start_ts": 1000,
                 "condition_id": "0xcurrent", "seconds_left": 120, "fair_value_up": 0.52}
        if campaign == "maker-adaptive-paper-v1":
            think["adaptive_paper"] = {
                "selector_version": "adaptive-ucb-v1",
                "reward_version": "maker-reward-v1",
                "profiles": {
                    "up": {"profile_id": "quote_tight", "selection_source": "fallback",
                           "exploration": True,
                           "context_key": "mid|mid|calm|flat|healthy|unconfirmed|healthy",
                           "parent_context_used": "global", "n_fills": 0, "mean_reward": 0.0}
                },
            }
        conn.execute("INSERT INTO maker_state(id,think_json,ts) VALUES(1,?,?)", (json.dumps(think), now))
        conn.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,
                      realized_pnl,settled,winning_outcome,ts,campaign_id)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""",
                     ("0xpair", 700, 0.0, 0.0, 0.0, pnl, 1, "Up", now, campaign))
        conn.commit()
        conn.close()

    conn = sqlite3.connect(adaptive)
    conn.executescript("""
      CREATE TABLE crypto5m_maker_profile_stats(
        campaign_id TEXT, context_key TEXT, profile_id TEXT, n_decisions INTEGER, n_quotes INTEGER,
        n_fills INTEGER, reward_sum REAL, reward_sq_sum REAL, pnl_sum REAL, adverse_sum REAL,
        hard_cuts INTEGER, disabled INTEGER, last_updated_ts INTEGER);
      CREATE TABLE crypto5m_maker_profile_disables(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, context_key TEXT, profile_id TEXT,
        disabled_at INTEGER, disabled_reason TEXT, selector_version TEXT, reward_version TEXT);
      CREATE TABLE crypto5m_maker_adaptive_rewards(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, fill_id INTEGER, reward REAL,
        fee_adjusted_pnl REAL, hard_cut_penalty REAL, reward_version TEXT);
    """)
    conn.execute("""INSERT INTO crypto5m_maker_profile_stats VALUES(
                    'maker-adaptive-paper-v1','mid|mid|calm|flat|healthy|unconfirmed|healthy',
                    'quote_tight',5,5,2,1.5,1.25,1.5,0,0,0,?)""", (now,))
    conn.commit()
    conn.close()

    body = _client("", maker_db_path=str(control), adaptive_maker_db_path=str(adaptive)).get(
        "/api/crypto5m/maker-adaptive"
    ).get_json()

    assert body["available"] is True
    assert body["paired_summary"]["n_paired"] == 1
    assert body["paired_summary"]["adaptive_minus_control"] == 3.5
    assert body["current"]["profiles_by_token"]["up"]["profile_id"] == "quote_tight"
    assert body["profile_stats"][0]["mean_reward"] == 0.75


def test_maker_adaptive_surfaces_v3_brain_state(tmp_path):
    import json
    import sqlite3
    import time

    control = tmp_path / "control.sqlite"
    adaptive_control = tmp_path / "adaptive_v2.sqlite"
    adaptive = tmp_path / "adaptive_v31.sqlite"
    now = int(time.time())
    for path, campaign, pnl in (
        (control, "maker-regime-v1", 1.0),
        (adaptive_control, "maker-adaptive-paper-v2", 1.5),
        (adaptive, "maker-adaptive-paper-v3.1", 2.5),
    ):
        conn = sqlite3.connect(path)
        conn.executescript("""
          CREATE TABLE maker_state(id INTEGER PRIMARY KEY CHECK(id=1), think_json TEXT, ts INTEGER);
          CREATE TABLE maker_windows(
            condition_id TEXT PRIMARY KEY, window_start_ts INTEGER, up_inv REAL, down_inv REAL, cash REAL,
            realized_pnl REAL, settled INTEGER DEFAULT 0, winning_outcome TEXT, ts INTEGER,
            up_vwap REAL, down_vwap REAL, campaign_id TEXT);
        """)
        think = {"campaign": campaign, "engine_version": campaign, "window_start_ts": 1000,
                 "condition_id": "0xcurrent", "seconds_left": 118, "fair_value_up": 0.70}
        if campaign == "maker-adaptive-paper-v3.1":
            think["adaptive_v3"] = {
                "selector_version": "adaptive-maker-brain-v3.1",
                "reward_version": "maker-reward-v3.1",
                "ablation_groups": ["A0", "A1", "A2", "A3", "A4"],
                "tokens": {
                    "up": {
                        "fair": {"fair_model_id": "logit_blend_v1", "q_structural": 0.70,
                                 "q_market_mid": 0.54, "q_used": 0.60,
                                 "blend_weight": 0.35, "fair_confidence": "medium"},
                        "risk": {"risk_state": "caution", "tail_risk_score": 0.45},
                        "action": {"arm_id": "wide_small", "selection_source": "learned",
                                   "parent_context_used": "global", "n_fills": 12,
                                   "mean_reward": 0.05},
                    }
                },
            }
        conn.execute("INSERT INTO maker_state(id,think_json,ts) VALUES(1,?,?)", (json.dumps(think), now))
        conn.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,
                      realized_pnl,settled,winning_outcome,ts,campaign_id)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""",
                     ("0xpair", 700, 0.0, 0.0, 0.0, pnl, 1, "Up", now, campaign))
        conn.commit()
        conn.close()

    conn = sqlite3.connect(adaptive)
    conn.executescript("""
      CREATE TABLE crypto5m_maker_v3_campaigns(
        campaign_id TEXT PRIMARY KEY, learner_version TEXT, selector_version TEXT,
        reward_version TEXT, control_campaign TEXT, started_ts INTEGER, config_json TEXT);
      CREATE TABLE crypto5m_maker_v3_action_arm_stats(
        campaign_id TEXT, context_key TEXT, arm_id TEXT, n_decisions INTEGER,
        n_quotes INTEGER, n_fills INTEGER, reward_sum REAL, reward_sq_sum REAL,
        adverse_sum REAL, disabled INTEGER, paper_preferred INTEGER, status TEXT,
        disable_type TEXT, disabled_reason TEXT, disabled_at INTEGER, paper_preferred_reason TEXT,
        paper_preferred_at INTEGER, hard_cuts INTEGER, max_window_fill_share REAL,
        confidence_low REAL, confidence_high REAL, last_updated_ts INTEGER);
      CREATE TABLE crypto5m_maker_v3_fair_blend_stats(
        campaign_id TEXT, fair_context_key TEXT, blend_weight REAL, residual_adjustment REAL,
        n_observations INTEGER, reward_sum REAL, brier_sum REAL, log_loss_sum REAL,
        calibration_abs_sum REAL, settled_correct_sum INTEGER, best_blend_arm TEXT,
        arm_metrics_json TEXT, last_updated_ts INTEGER);
      CREATE TABLE crypto5m_maker_v3_policy_actions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, learner_version TEXT,
        selector_version TEXT, reward_version TEXT, condition_id TEXT, window_start_ts INTEGER,
        token TEXT, context_key TEXT, parent_context_used TEXT, fallback_depth INTEGER,
        fair_model_id TEXT, q_structural REAL, q_market_mid REAL, q_used REAL,
        fair_blend_weight REAL, residual_adjustment REAL, fair_confidence TEXT,
        risk_state TEXT, tail_risk_score REAL, quote_action_arm TEXT, selector_source TEXT,
        exploration_flag INTEGER, policy_action TEXT, disabled_profile_flag INTEGER, created_ts INTEGER);
      CREATE TABLE crypto5m_maker_v3_ablations(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, condition_id TEXT,
        window_start_ts INTEGER, token TEXT, context_key TEXT, ablation_group TEXT,
        selected_arm TEXT, q_used REAL, risk_state TEXT, created_ts INTEGER);
    """)
    conn.execute("""INSERT INTO crypto5m_maker_v3_campaigns VALUES(
                    'maker-adaptive-paper-v3.1','v3.1','adaptive-maker-brain-v3.1','maker-reward-v3.1',
                    'maker-adaptive-paper-v2',?, '{}')""", (now,))
    conn.execute("""INSERT INTO crypto5m_maker_v3_action_arm_stats VALUES(
                    'maker-adaptive-paper-v3.1','mid|mid|calm|flat|healthy|unconfirmed|healthy',
                    'wide_small',20,10,12,6.0,4.0,-100.0,0,1,'paper_preferred',
                    NULL,NULL,NULL,'good sample',?,0,0.25,0.1,0.9,?)""", (now, now))
    conn.execute("""INSERT INTO crypto5m_maker_v3_fair_blend_stats VALUES(
                    'maker-adaptive-paper-v3.1','mid|mid|calm|healthy',0.25,0.0,11,0.0,
                    1.1,2.2,3.3,8,'blend_25','{}',?)""", (now,))
    conn.execute("""INSERT INTO crypto5m_maker_v3_policy_actions(
                    campaign_id,learner_version,selector_version,reward_version,condition_id,
                    window_start_ts,token,context_key,parent_context_used,fallback_depth,fair_model_id,
                    q_structural,q_market_mid,q_used,fair_blend_weight,residual_adjustment,fair_confidence,
                    risk_state,tail_risk_score,quote_action_arm,selector_source,exploration_flag,
                    policy_action,disabled_profile_flag,created_ts)
                    VALUES('maker-adaptive-paper-v3.1','v3.1','adaptive-maker-brain-v3.1','maker-reward-v3.1',
                    '0xcurrent',1000,'up','mid|mid|calm|flat|healthy|unconfirmed|healthy',
                    'global',4,'logit_blend_v1',0.70,0.54,0.60,0.35,0.0,'medium',
                    'caution',0.45,'wide_small','paper_preferred',0,'paper_quote',0,?)""", (now,))
    for group in ("A0", "A1", "A2", "A3", "A4"):
        conn.execute("""INSERT INTO crypto5m_maker_v3_ablations(
                        campaign_id,condition_id,window_start_ts,token,context_key,ablation_group,
                        selected_arm,q_used,risk_state,created_ts)
                        VALUES('maker-adaptive-paper-v3.1','0xcurrent',1000,'up',
                        'mid|mid|calm|flat|healthy|unconfirmed|healthy',?,?,0.60,'caution',?)""",
	                     (group, "wide_small", now))
    conn.commit()
    conn.close()

    body = _client(
        "",
        maker_db_path=str(control),
        adaptive_maker_db_path=str(adaptive),
        adaptive_control_maker_db_path=str(adaptive_control),
    ).get(
        "/api/crypto5m/maker-adaptive"
    ).get_json()

    assert body["adaptive"]["campaign"] == "maker-adaptive-paper-v3.1"
    assert body["adaptive"]["selector_version"] == "adaptive-maker-brain-v3.1"
    assert body["adaptive"]["n_v3_fair_stats"] == 1
    assert body["adaptive"]["n_v3_paper_preferred_arms"] == 1
    assert body["current_v3"]["available"] is True
    assert body["current_v3"]["tokens"]["up"]["fair"]["fair_model_id"] == "logit_blend_v1"
    assert body["current_v3"]["tokens"]["up"]["action"]["arm_id"] == "wide_small"
    assert body["v3_action_stats"][0]["arm_id"] == "wide_small"
    assert body["v3_action_stats"][0]["mean_reward"] == 0.5
    assert body["v3_fair_stats"][0]["best_blend_arm"] == "blend_25"
    assert body["v3_selector_sources"] == {"paper_preferred": 1}
    assert body["v3_vs_v2_paired_summary"]["adaptive_minus_control"] == 1.0
    assert body["v3_ablations"]["groups"] == ["A0", "A1", "A2", "A3", "A4"]


def test_maker_session_withholds_stats_without_a_campaign_instead_of_pooling(tmp_path):
    # mid-reset (no think yet) the old fallback pooled ALL campaigns — the counter jumped around
    maker, conn, now = _mk_maker_db(tmp_path, think_campaign=None)
    conn.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,
                  realized_pnl,settled,winning_outcome,ts,campaign_id) VALUES(?,?,?,?,?,?,1,?,?,?)""",
                 ("0xold", 1000, 0.0, 0.0, 0.0, 99.0, "Up", now, "maker-unified-v3"))
    conn.commit(); conn.close()

    body = _client("", maker_db_path=str(maker)).get("/api/crypto5m/maker").get_json()
    assert body["session"]["n_settled"] == 0
    assert body["session"]["realized_pnl"] == 0
    assert body["session"]["campaign"] is None
    assert "withheld" in body["session"]["note"]


def test_regime_shadow_realized_attribution_splits_cash_and_payout_by_regime():
    import pytest

    from sniper.routes.api_crypto5m import _attribute_settled_realized

    by_regime: dict = {}
    fills = [
        {"regime_id": "normal", "token": "up", "side": "bid", "price": 0.40, "size": 10.0,
         "condition_id": "0xw"},
        {"regime_id": "mid_chop", "token": "up", "side": "ask", "price": 0.60, "size": 5.0,
         "condition_id": "0xw"},
    ]
    _attribute_settled_realized(by_regime, {"0xw": fills}, {"0xw": "up"})
    # normal: paid 4.00 for 10 sh; mid_chop sold 5 of them for 3.00; normal's remaining 5 pay $1 each
    assert by_regime["normal"]["realized_attr"] == pytest.approx(-4.0 + 5.0)
    assert by_regime["mid_chop"]["realized_attr"] == pytest.approx(3.0)
    # total equals the window ledger identity: cash (-4+3) + winner inventory (5) = +4
    total = sum(b["realized_attr"] for b in by_regime.values())
    assert total == pytest.approx(4.0)
    assert by_regime["normal"]["attr_windows"] == 1


def _mini_campaign_db(path, campaign, windows):
    import json
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE maker_windows(condition_id TEXT, window_start_ts INTEGER, up_inv REAL, down_inv REAL,
        cash REAL, realized_pnl REAL, settled INTEGER DEFAULT 0, winning_outcome TEXT, ts INTEGER,
        up_vwap REAL, down_vwap REAL, campaign_id TEXT);
      CREATE TABLE maker_state(id INTEGER PRIMARY KEY CHECK(id=1), think_json TEXT, ts INTEGER);
    """)
    conn.execute("INSERT INTO maker_state(id, think_json, ts) VALUES (1, ?, 100)",
                 (json.dumps({"campaign": campaign, "window_start_ts": windows[-1][0]}),))
    for ws, pnl in windows:
        conn.execute(
            "INSERT INTO maker_windows(condition_id, window_start_ts, cash, realized_pnl, settled,"
            " winning_outcome, ts, campaign_id) VALUES (?,?,?,?,1,'Up',?,?)",
            (f"0x{campaign}-{ws}", ws, pnl, pnl, ws, campaign))
    conn.commit()
    conn.close()


def test_maker_adaptive_campaign_param_selects_registry_entry(tmp_path, monkeypatch):
    from sniper.routes import api_crypto5m

    v32f = tmp_path / "v32f.sqlite"
    v31 = tmp_path / "v31.sqlite"
    _mini_campaign_db(v32f, "maker-adaptive-paper-v3.2f", [(1000, 5.0), (1300, -1.0)])
    _mini_campaign_db(v31, "maker-adaptive-paper-v3.1", [(1000, 1.0), (1300, 1.0)])
    monkeypatch.setitem(api_crypto5m.ADAPTIVE_CAMPAIGN_REGISTRY["v3.2f"], "db", str(v32f))
    monkeypatch.setitem(api_crypto5m.ADAPTIVE_CAMPAIGN_REGISTRY["v3.2f"], "iso_db", str(v31))

    body = _client("", maker_db_path=str(tmp_path / "missing_control.sqlite")).get(
        "/api/crypto5m/maker-adaptive?campaign=v3.2f"
    ).get_json()

    assert body["selected_campaign_key"] == "v3.2f"
    assert body["adaptive"]["campaign"] == "maker-adaptive-paper-v3.2f"
    assert body["iso_control_campaign"] == "maker-adaptive-paper-v3.1"
    assert body["v3_vs_v2_paired_summary"]["n_paired"] == 2
    assert any(c["key"] == "v3.2r" for c in body["campaigns"])


def test_maker_adaptive_unknown_campaign_key_is_rejected(tmp_path):
    resp = _client("", maker_db_path=str(tmp_path / "missing.sqlite")).get(
        "/api/crypto5m/maker-adaptive?campaign=../../etc/passwd"
    )

    assert resp.status_code == 400
    assert "unknown campaign key" in resp.get_json()["error"]


def test_maker_adaptive_ctx_override_still_wins_without_param(tmp_path):
    adaptive = tmp_path / "custom.sqlite"
    _mini_campaign_db(adaptive, "maker-adaptive-paper-v3.1", [(1000, 2.0)])

    body = _client(
        "",
        maker_db_path=str(tmp_path / "missing.sqlite"),
        adaptive_maker_db_path=str(adaptive),
        adaptive_control_maker_db_path=str(tmp_path / "missing2.sqlite"),
    ).get("/api/crypto5m/maker-adaptive").get_json()

    assert body["adaptive"]["campaign"] == "maker-adaptive-paper-v3.1"
    assert body["adaptive"]["db_path"] == str(adaptive)


def test_fleet_reports_per_campaign_since_anchor_and_paired_delta(tmp_path, monkeypatch):
    from sniper.routes import api_crypto5m

    control = tmp_path / "control.sqlite"
    anchorm = tmp_path / "anchor.sqlite"    # v3.2f: off-board (retired) but still the anchor source
    primary = tmp_path / "primary.sqlite"   # frozen-f: the board primary (haircut panel assertions)
    second = tmp_path / "second.sqlite"     # v3.3L: the secondary paired-delta check
    sweep1 = tmp_path / "sweep1.sqlite"
    sweep5 = tmp_path / "sweep5.sqlite"
    # control has an older window (500) that must be excluded from the since-anchor column
    _mini_campaign_db(control, "maker-regime-v1", [(500, 100.0), (1000, -2.0), (1300, 1.0)])
    _mini_campaign_db(anchorm, "maker-adaptive-paper-v3.2f", [(1000, 5.0)])   # anchor = 1000
    # PRIMARY + SECONDARY use current board members (v3.2f/v3.2r/v3.1/v2 retired 2026-07-07)
    _mini_campaign_db(primary, "maker-adaptive-paper-v3.2f-frozen", [(1000, 5.0), (1300, 2.0)])
    _mini_campaign_db(second, "maker-v33-learner-paper", [(1000, 1.0)])
    _mini_campaign_db(sweep1, "maker-sweep-floor-1e5", [(1000, 4.0)])
    _mini_campaign_db(sweep5, "maker-sweep-floor-5e5", [(1000, 3.0)])
    conn = sqlite3.connect(control)
    conn.executescript("""
      CREATE TABLE maker_fills(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, condition_id TEXT,
        window_start_ts INTEGER, token TEXT, side TEXT, price REAL, size REAL, fair REAL,
        ts INTEGER, reason TEXT, fill_model TEXT);
    """)
    conn.execute("""INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,price,
                  size,fair,ts,reason,fill_model) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                 ("maker-regime-v1", "0xmaker-regime-v1-1000", 1000,
                  "up", "bid", 0.42, 20.0, 0.45, 1001, "print fill", "print"))
    conn.commit()
    conn.close()
    conn = sqlite3.connect(primary)
    conn.executescript("""
      CREATE TABLE maker_fills(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, condition_id TEXT,
        window_start_ts INTEGER, token TEXT, side TEXT, price REAL, size REAL, fair REAL,
        ts INTEGER, reason TEXT, fill_model TEXT);
    """)
    conn.execute("""INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,price,
                  size,fair,ts,reason,fill_model) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                 ("maker-adaptive-paper-v3.2f-frozen", "0xprimary-1000", 1000,
                  "up", "bid", 0.40, 10.0, 0.45, 1001, "print fill", "print"))
    conn.execute("""INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,price,
                  size,fair,ts,reason,fill_model) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                 ("maker-adaptive-paper-v3.2f-frozen", "0xprimary-1300", 1300,
                  "up", "ask", 0.35, 5.0, 0.30, 1301, "hard cut", "hard-cut"))
    conn.commit()
    conn.close()
    monkeypatch.setitem(api_crypto5m.ADAPTIVE_CAMPAIGN_REGISTRY["v3.2f"], "db", str(anchorm))
    monkeypatch.setitem(api_crypto5m.ADAPTIVE_CAMPAIGN_REGISTRY["v3.2f-frozen"], "db", str(primary))
    monkeypatch.setitem(api_crypto5m.ADAPTIVE_CAMPAIGN_REGISTRY["v3.3L"], "db", str(second))
    monkeypatch.setitem(api_crypto5m.ADAPTIVE_CAMPAIGN_REGISTRY["sweep-1e-5"], "db", str(sweep1))
    monkeypatch.setitem(api_crypto5m.ADAPTIVE_CAMPAIGN_REGISTRY["sweep-5e-5"], "db", str(sweep5))
    # a fleet campaign whose DB is absent must report available=False
    monkeypatch.setitem(api_crypto5m.ADAPTIVE_CAMPAIGN_REGISTRY["deep-only-v1"], "db", str(tmp_path / "nope.sqlite"))

    body = _client("", maker_db_path=str(control)).get("/api/crypto5m/fleet").get_json()

    assert body["available"] is True
    assert [r["key"] for r in body["fleet"]] == [
        None, "v3.2f-frozen", "v3.3L", "deep-only-v1", "sweep-1e-5", "sweep-5e-5"
    ]
    assert body["anchor_window_start_ts"] == 1000
    rows = {r["campaign"]: r for r in body["fleet"]}
    ctl = rows["maker-regime-v1"]
    assert ctl["lifetime"] == {"n": 3, "total": 99.0}
    assert ctl["since"]["n"] == 2 and ctl["since"]["total"] == -1.0
    f = rows["maker-adaptive-paper-v3.2f-frozen"]
    assert f["since"]["total"] == 7.0
    # concentration check: best window (5.0) removed
    assert f["since"]["best_window"] == 5.0
    assert f["since"]["total_ex_best"] == 2.0
    assert f["paired_vs_control"]["n"] == 2
    assert f["paired_vs_control"]["delta_total"] == 8.0
    assert f["paired_vs_control"]["delta_mean"] == 4.0
    # deltas are (5-(-2))=7 and (2-1)=1 -> ex-best = 1
    assert f["paired_vs_control"]["delta_ex_best"] == 1.0
    assert f["paired_vs_control"]["pre_registered_gate"]["min_paired_windows"] == 200
    assert f["paired_vs_control"]["pre_registered_gate"]["status"] == "insufficient_sample"
    assert f["paired_vs_control"]["pre_registered_gate"]["recommendation"] == "keep_collecting"
    assert f["paired_vs_control"]["forward_pre_registered_gate"]["anchor_ts"] == 1783355700
    assert f["paired_vs_control"]["forward_pre_registered_gate"]["anchor_iso"] == "2026-07-06T16:35:00Z"
    assert f["paired_vs_control"]["forward_pre_registered_gate"]["n_paired"] == 0
    assert f["paired_vs_control"]["selection_note"] == "winner-picked-after-looking; paired gate is the honest test"
    assert f["paired_vs_control"]["paired_haircut_delta_panel"]["rows"][0] == {
        "haircut_cents": 1,
        "control_total_drag": 0.2,
        "adaptive_total_drag": 0.15,
        "unstressed_delta": 8.0,
        "delta_after_print_haircut": 8.1,
        "delta_after_total_haircut": 8.05,
    }
    assert f["execution_haircut_panel"]["base_realized_pnl"] == 7.0
    assert f["execution_haircut_panel"]["settled_print_shares"] == 10.0
    assert f["execution_haircut_panel"]["settled_hard_cut_shares"] == 5.0
    assert f["execution_haircut_panel"]["rows"][0] == {
        "haircut_cents": 1,
        "print_fill_drag": 0.1,
        "hard_cut_drag": 0.05,
        "total_drag": 0.15,
        "pnl_after_print_haircut": 6.9,
        "pnl_after_total_haircut": 6.85,
    }
    assert "flow_gate" in body
    r = rows["maker-v33-learner-paper"]
    assert r["paired_vs_control"]["delta_total"] == 3.0
    assert rows["maker-deep-only-v1"]["available"] is False
    assert rows["maker-sweep-floor-1e5"]["paired_vs_control"]["delta_total"] == 6.0
    assert rows["maker-sweep-floor-5e5"]["paired_vs_control"]["delta_total"] == 5.0


def test_paired_window_summary_gate_waits_for_200_paired_windows():
    from sniper.routes.api_crypto5m import _paired_window_summary

    control = [
        {"window_start_ts": 1000 + i * 300, "realized_pnl": 0.0, "winning_outcome": "Up"}
        for i in range(199)
    ]
    adaptive = [
        {"window_start_ts": 1000 + i * 300, "realized_pnl": 1.0, "winning_outcome": "Up"}
        for i in range(199)
    ]

    _, summary = _paired_window_summary(control, adaptive)

    assert summary["pre_registered_gate"]["n_paired"] == 199
    assert summary["pre_registered_gate"]["eval_ready"] is False
    assert summary["pre_registered_gate"]["status"] == "insufficient_sample"

    control.append({"window_start_ts": 1000 + 199 * 300, "realized_pnl": 0.0, "winning_outcome": "Up"})
    adaptive.append({"window_start_ts": 1000 + 199 * 300, "realized_pnl": 1.0, "winning_outcome": "Up"})

    _, summary = _paired_window_summary(control, adaptive)

    assert summary["pre_registered_gate"]["n_paired"] == 200
    assert summary["pre_registered_gate"]["eval_ready"] is True
    assert summary["pre_registered_gate"]["ci_low"] > 0
    assert summary["pre_registered_gate"]["recommendation"] == "candidate_good"


def test_paired_window_summary_forward_gate_excludes_pre_registered_windows():
    from sniper.routes.api_crypto5m import _paired_window_summary

    control = [
        {"window_start_ts": 1000, "realized_pnl": 0.0, "winning_outcome": "Up"},
        {"window_start_ts": 1300, "realized_pnl": 0.0, "winning_outcome": "Up"},
    ]
    adaptive = [
        {"window_start_ts": 1000, "realized_pnl": 10.0, "winning_outcome": "Up"},
        {"window_start_ts": 1300, "realized_pnl": -1.0, "winning_outcome": "Up"},
    ]

    _, summary = _paired_window_summary(control, adaptive, forward_anchor_ts=1300)

    assert summary["pre_registered_gate"]["n_paired"] == 2
    assert summary["forward_pre_registered_gate"]["anchor_ts"] == 1300
    assert summary["forward_pre_registered_gate"]["n_paired"] == 1
    assert summary["forward_pre_registered_gate"]["status"] == "insufficient_sample"


def test_maker_adaptive_versions_come_from_campaign_table_not_think(tmp_path, monkeypatch):
    """v3.2r regression: think_json embeds module constants (v3.1) but the campaign row is the
    source of truth (maker-reward-v3.2). The API must prefer the table."""
    import json
    import sqlite3

    from sniper.algos.polymarket_crypto5m import maker_adaptive_v3 as V3
    from sniper.routes import api_crypto5m

    db = tmp_path / "v32r.sqlite"
    _mini_campaign_db(db, "maker-adaptive-paper-v3.2r", [(1000, 1.0)])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    V3.register_campaign(conn, "maker-adaptive-paper-v3.2r", "maker-adaptive-paper-v3.1", 100, {},
                         reward_version="maker-reward-v3.2")
    conn.commit()
    # think blob carries the WRONG (module-constant) version, as the live runner did pre-fix
    conn.execute("UPDATE maker_state SET think_json=?", (json.dumps({
        "campaign": "maker-adaptive-paper-v3.2r", "window_start_ts": 1000,
        "adaptive_v3": {"selector_version": "adaptive-maker-brain-v3.1",
                        "reward_version": "maker-reward-v3.1"},
    }),))
    conn.commit()
    conn.close()
    monkeypatch.setitem(api_crypto5m.ADAPTIVE_CAMPAIGN_REGISTRY["v3.2r"], "db", str(db))
    monkeypatch.setitem(api_crypto5m.ADAPTIVE_CAMPAIGN_REGISTRY["v3.2r"], "iso_db", str(tmp_path / "no.sqlite"))

    body = _client("", maker_db_path=str(tmp_path / "missing.sqlite")).get(
        "/api/crypto5m/maker-adaptive?campaign=v3.2r"
    ).get_json()

    assert body["adaptive"]["reward_version"] == "maker-reward-v3.2"
