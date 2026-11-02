"""Desk tab feed panels: read-only, answer with every feed off, carry creators beside launches."""
import json
import sqlite3
import time

from flask import Flask

from sniper.routes import api_memecoin_desk as D
from sniper.routes import desk_feeds_view as V

NOW = 1_792_600_000.0


def _client(tmp_path):
    app = Flask(__name__)
    app.register_blueprint(D.create_api_memecoin_desk_blueprint({
        "desk_dir": str(tmp_path / "desk"), "desk_db": str(tmp_path / "memecoin_desk.db"),
        "ledger_db": str(tmp_path / "ledger.db")}))
    return app.test_client()


def test_every_feed_off_still_answers_and_writes_nothing(tmp_path):
    (tmp_path / "desk").mkdir()
    body = _client(tmp_path).get("/api/memecoin/desk/feeds").get_json()
    assert body["display_only"] is True and "Collect/report only" in body["collect_only"]
    assert body["trending"] == {"available": False, "reason": "no_trending_data"}
    assert body["narrative"] == {"available": False, "reason": "no_narrative_data"}
    assert [h["feed"] for h in body["heartbeats"]] == ["X producer", "crawler", "DexScreener", "trending", "narrative"]
    assert all(h["age_s"] is None for h in body["heartbeats"])
    assert list((tmp_path / "desk").iterdir()) == []                                  # nothing created


def test_panels_read_trending_narrative_and_heartbeats(tmp_path):
    d = tmp_path / "desk"
    for sub in ("trending", "narrative", "x", "dexscreener"):
        (d / sub).mkdir(parents=True)
    t = sqlite3.connect(d / "trending" / "2026-10-21.db")
    t.executescript("CREATE TABLE boards (at REAL, rule_version TEXT, coverage TEXT, sol_usd REAL, coins INTEGER, "
                    "koth_mint TEXT, koth_mcap_usd REAL, koth_curve_kind TEXT, curve_rule TEXT);"
                    "CREATE TABLE ranks (at REAL, rank INTEGER, mint TEXT, buy_usd_5m REAL, buy_usd_15m REAL, buy_usd_60m REAL, "
                    "sell_usd_15m REAL, swaps_15m INTEGER, makers_15m INTEGER, change_15m_pct REAL, mcap_usd REAL, curve_kind TEXT);")
    t.execute("INSERT INTO boards VALUES (?, 'trend-v1', 'ok', 100, 900, 'KOTHmint', 217000, 'nonstandard', 'curve-k-v1')", (NOW,))
    t.execute("INSERT INTO ranks VALUES (?, 1, 'MintA', 1, 2450, 3, 1, 98, 395, 12.5, 30000, 'standard')", (NOW,))
    t.commit()
    n = sqlite3.connect(d / "narrative" / "2026-10-21.db")
    n.executescript("CREATE TABLE token_hourly (hour_start REAL, kind TEXT, rank INTEGER, token TEXT, launches INTEGER, "
                    "creators INTEGER, prev_hour_launches INTEGER, known_at REAL, rules_version TEXT);"
                    "CREATE TABLE theme_agg (at REAL, theme TEXT, window_s INTEGER, launches INTEGER, band_entries INTEGER, "
                    "buy_usd REAL, buyers INTEGER, slope REAL, coverage TEXT, rules_version TEXT);")
    n.execute("INSERT INTO token_hourly VALUES (3600, 'name_token', 1, 'onboard', 96, 1, 0, 1, 'theme-rules-v1')")
    n.execute("INSERT INTO theme_agg VALUES (?, 'food', 3600, 142, 3, 90085, 3077, NULL, 'ok', 'theme-rules-v1')", (NOW,))
    n.commit()
    (d / "x" / "producer.heartbeat").write_text(json.dumps({"ts": NOW - 4, "connected": True, "last_error": None}))
    x = sqlite3.connect(d / "dexscreener" / "2026-10-21.db")
    x.execute("CREATE TABLE polls (id INTEGER PRIMARY KEY, endpoint TEXT, received_at REAL, status INTEGER, items INTEGER, "
              "error TEXT, source TEXT)")
    x.execute("INSERT INTO polls (endpoint, received_at, status, error) VALUES ('/x', ?, 429, 'http_429')", (NOW,))
    x.commit()
    f = V.feeds(d, now=NOW)
    assert f["trending"]["koth"] == {"mint": "KOTHmint", "mcap_usd": 217000, "curve_kind": "nonstandard"}
    assert f["trending"]["ranks"][0]["curve_kind"] == "standard"
    tok = f["narrative"]["tokens"]["name_token"][0]
    assert (tok["token"], tok["launches"], tok["creators"]) == ("onboard", 96, 1)     # a farm is visible
    assert f["narrative"]["themes_60m"][0]["theme"] == "food"
    beats = {h["feed"]: h for h in f["heartbeats"]}
    assert beats["X producer"]["age_s"] == 4.0 and beats["X producer"]["connected"] is True
    assert beats["DexScreener"]["last_error"] == "http_429" and beats["crawler"]["age_s"] is None


def test_the_workers_list_names_the_feed_processes(tmp_path):
    body = _client(tmp_path).get("/api/memecoin/desk/status").get_json()
    assert {"trending", "DexScreener", "narrative"} <= {w["worker"] for w in body["workers"]}
