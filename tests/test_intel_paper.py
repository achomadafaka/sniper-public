"""Intel paper book — positions, marks, routing, and rendering.

Plan: docs/superpowers/plans/2026-07-29-intel-paper-testing.md
Spec: docs/superpowers/specs/2026-07-29-intel-paper-testing-design.md
"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from sniper.persistence import intel_paper


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    intel_paper.ensure_schema(c)
    return c


def _open(conn, **over):
    fields = dict(
        idea_id=1, venue="pm",
        instrument="0xabc", token_id="111",
        event_title="US x Iran Ceasefire", outcome_label="July 31",
        direction="buy_no", entry_price=0.635,
        entry_ts="2026-07-29T11:08:00+00:00", size_usdc=10.0,
        reviewer_verdict="pending", reviewer_score=None, holder_snapshot=None,
    )
    fields.update(over)
    return intel_paper.open_position(conn, **fields)


# ---------------------------------------------------------------------------
# Task 1 — persistence
# ---------------------------------------------------------------------------

def test_open_and_read_back(conn):
    pid = _open(conn)
    assert pid is not None
    rows = intel_paper.open_positions(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["venue"] == "pm"
    assert r["outcome_label"] == "July 31"
    assert r["status"] == "open"
    assert r["last_mark_price"] is None, "an unmarked position must not fake a price"


def test_one_position_per_idea(conn):
    assert _open(conn) is not None
    assert _open(conn) is None, "a second open for the same idea_id must be refused"


def test_marks_attach_and_latest_wins(conn):
    pid = _open(conn)
    intel_paper.record_mark(conn, pid, "2026-07-29T12:00:00+00:00", 0.60, -5.51)
    intel_paper.record_mark(conn, pid, "2026-07-29T13:00:00+00:00", 0.58, -8.66)
    r = intel_paper.open_positions(conn)[0]
    assert r["last_mark_price"] == 0.58
    assert r["last_mark_ts"] == "2026-07-29T13:00:00+00:00"
    assert r["last_pnl_pct"] == -8.66


def test_venue_filter_and_close(conn):
    pm = _open(conn)
    _open(conn, idea_id=2, venue="hl", instrument="flx:OIL", token_id=None,
          direction="long", entry_price=62.4, event_title=None, outcome_label=None)
    assert len(intel_paper.open_positions(conn, venue="pm")) == 1
    assert len(intel_paper.open_positions(conn, venue="hl")) == 1

    intel_paper.close_position(conn, pm, 0.71, "2026-07-31T00:00:00+00:00", "resolved")
    assert len(intel_paper.open_positions(conn, venue="pm")) == 0


# ---------------------------------------------------------------------------
# Task 2 — one-shot Gamma market lookup
# ---------------------------------------------------------------------------

def _resp(payload):
    r = MagicMock()
    r.read.return_value = json.dumps(payload).encode()
    r.__enter__ = lambda s: s
    r.__exit__ = lambda s, *a: False
    return r


_GAMMA_MARKET = [{
    "question": "US x Iran Effective Ceasefire by July 31?",
    "groupItemTitle": "July 31",
    "events": [{"title": "US x Iran Effective Ceasefire (2 week pause)"}],
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0.385", "0.615"]',
    "clobTokenIds": '["95577979648711137561", "34707224416470630443"]',
    "bestBid": 0.38, "bestAsk": 0.39,
    "endDateIso": "2026-07-31", "negRisk": False, "closed": False,
}]


def test_fetch_market_returns_both_tokens_and_labels():
    from sniper.markets import pm_resolver

    with patch("urllib.request.urlopen", return_value=_resp(_GAMMA_MARKET)):
        m = pm_resolver.fetch_market("0xabc")

    assert m["token_ids"]["YES"] == "95577979648711137561"
    assert m["token_ids"]["NO"] == "34707224416470630443"
    assert m["prices"]["YES"] == 0.385
    assert m["prices"]["NO"] == 0.615
    assert m["outcome_label"] == "July 31"
    assert m["event_title"] == "US x Iran Effective Ceasefire (2 week pause)"
    assert m["neg_risk"] is False


def test_fetch_market_returns_none_when_absent():
    from sniper.markets import pm_resolver

    with patch("urllib.request.urlopen", return_value=_resp([])):
        assert pm_resolver.fetch_market("0xnope") is None
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert pm_resolver.fetch_market("0xnope") is None


def test_fetch_market_rejects_a_closed_market():
    from sniper.markets import pm_resolver

    payload = [dict(_GAMMA_MARKET[0], closed=True)]
    with patch("urllib.request.urlopen", return_value=_resp(payload)):
        assert pm_resolver.fetch_market("0xabc") is None, "a closed market is not openable"


# ---------------------------------------------------------------------------
# Task 3 — Hyperliquid universe (main + HIP-3 dexes)
# ---------------------------------------------------------------------------

def test_resolve_symbol_exact_and_suffix():
    from sniper.markets import hl_universe

    uni = {"BTC", "ETH", "flx:OIL", "km:USOIL", "xyz:TSLA", "cash:TSLA"}
    assert hl_universe.resolve_symbol("BTC", uni) == "BTC"
    assert hl_universe.resolve_symbol("flx:OIL", uni) == "flx:OIL"
    # unique across dexes -> resolves
    assert hl_universe.resolve_symbol("OIL", uni) == "flx:OIL"
    # present on two dexes -> ambiguous, refuse
    assert hl_universe.resolve_symbol("TSLA", uni) is None
    # absent -> refuse (this is the EIS class of bug, audit F-01)
    assert hl_universe.resolve_symbol("EIS", uni) is None
    assert hl_universe.resolve_symbol("OIL-USD", uni) is None
    assert hl_universe.resolve_symbol("", uni) is None


def test_load_universe_merges_main_and_dexes():
    from sniper.markets import hl_universe

    def fake_post(payload, timeout=10.0):
        if payload["type"] == "perpDexs":
            return [None, {"name": "flx"}]
        if payload["type"] == "meta" and payload.get("dex") == "flx":
            return {"universe": [{"name": "flx:OIL"}, {"name": "flx:GOLD"}]}
        if payload["type"] == "meta":
            return {"universe": [{"name": "BTC"}, {"name": "ETH"}]}
        raise AssertionError(payload)

    with patch.object(hl_universe, "_post", fake_post):
        uni = hl_universe.load_universe()
    assert uni == {"BTC", "ETH", "flx:OIL", "flx:GOLD"}


def test_all_mids_groups_calls_by_dex():
    from sniper.markets import hl_universe

    calls = []

    def fake_post(payload, timeout=10.0):
        calls.append(payload.get("dex"))
        if payload.get("dex") == "flx":
            return {"flx:OIL": "62.4"}
        return {"BTC": "91470.2", "ETH": "3050.0"}

    with patch.object(hl_universe, "_post", fake_post):
        mids = hl_universe.all_mids({"BTC", "flx:OIL"})

    assert mids == {"BTC": 91470.2, "flx:OIL": 62.4}
    assert sorted(c or "" for c in calls) == ["", "flx"], "one call per distinct dex, no more"


# ---------------------------------------------------------------------------
# Task 4 — routing and opening
# ---------------------------------------------------------------------------

_MARKET = {
    "condition_id": "0xabc", "question": "Q", "event_title": "E",
    "outcome_label": "July 31",
    "token_ids": {"YES": "111", "NO": "222"},
    "prices": {"YES": 0.385, "NO": 0.615},
    "best_bid": 0.38, "best_ask": 0.39,
    "end_date_iso": "2026-07-31", "neg_risk": False, "closed": False,
}


def test_open_for_idea_routes_pm_with_outcome_native_price(conn):
    from sniper.services import intel_paper_open

    idea = {"id": 7, "condition_id": "0xabc", "direction": "buy_no",
            "adversarial_approved": 0, "adversarial_score": 87}
    with patch.object(intel_paper_open, "fetch_market", return_value=_MARKET):
        out = intel_paper_open.open_for_idea(conn, idea)

    assert out["disposition"] == "pm"
    row = intel_paper.open_positions(conn)[0]
    assert row["token_id"] == "222", "buy_no must store the NO token (ADR-003)"
    assert row["entry_price"] == 0.615, "buy_no must store the NO price, not the YES price"
    assert row["outcome_label"] == "July 31"
    assert row["reviewer_verdict"] == "rejected", "a rejected idea still opens, tagged"
    assert row["reviewer_score"] == 87


def test_open_for_idea_buy_yes_stores_yes_side(conn):
    from sniper.services import intel_paper_open

    idea = {"id": 11, "condition_id": "0xabc", "direction": "buy_yes",
            "adversarial_approved": 1, "adversarial_score": 30}
    with patch.object(intel_paper_open, "fetch_market", return_value=_MARKET):
        intel_paper_open.open_for_idea(conn, idea)

    row = intel_paper.open_positions(conn)[0]
    assert row["token_id"] == "111"
    assert row["entry_price"] == 0.385
    assert row["reviewer_verdict"] == "approved"


def test_open_for_idea_routes_hl(conn):
    from sniper.services import intel_paper_open

    idea = {"id": 8, "condition_id": None, "hl_symbol": "OIL",
            "direction": "long", "adversarial_approved": None}
    with patch.object(intel_paper_open, "all_mids", return_value={"flx:OIL": 62.4}):
        out = intel_paper_open.open_for_idea(conn, idea, hl_universe_set={"flx:OIL"})

    assert out["disposition"] == "hl"
    row = intel_paper.open_positions(conn, venue="hl")[0]
    assert row["instrument"] == "flx:OIL"
    assert row["entry_price"] == 62.4
    assert row["reviewer_verdict"] == "pending"


def test_open_for_idea_refuses_unknown_instrument(conn):
    from sniper.services import intel_paper_open

    idea = {"id": 9, "condition_id": None, "hl_symbol": "EIS", "direction": "short"}
    with patch.object(intel_paper_open, "fetch_market", return_value=None):
        out = intel_paper_open.open_for_idea(conn, idea, hl_universe_set={"flx:OIL"})

    assert out["disposition"] == "no_instrument"
    assert out["position_id"] is None
    assert intel_paper.open_positions(conn) == []


# ---------------------------------------------------------------------------
# Task 5 — mark cycle
# ---------------------------------------------------------------------------

def test_pnl_pct_signs():
    from sniper.services.intel_paper_mark import pnl_pct

    # PM: buying NO at 0.60, NO now 0.66 -> +10%
    assert pnl_pct("buy_no", 0.60, 0.66) == pytest.approx(10.0)
    assert pnl_pct("buy_yes", 0.50, 0.40) == pytest.approx(-20.0)
    # HL
    assert pnl_pct("long", 100.0, 110.0) == pytest.approx(10.0)
    assert pnl_pct("short", 100.0, 90.0) == pytest.approx(10.0)
    assert pnl_pct("short", 100.0, 110.0) == pytest.approx(-10.0)
    # never divide by zero
    assert pnl_pct("long", 0.0, 5.0) == 0.0


def test_mark_cycle_uses_one_batch_call_per_venue(conn):
    from sniper.services import intel_paper_mark

    for i, (venue, instr, tok, direction, px) in enumerate([
        ("pm", "0xa", "111", "buy_no", 0.60),
        ("pm", "0xb", "222", "buy_yes", 0.30),
        ("hl", "flx:OIL", None, "long", 60.0),
    ], start=1):
        intel_paper.open_position(
            conn, idea_id=i, venue=venue, instrument=instr, token_id=tok,
            event_title=None, outcome_label=None, direction=direction,
            entry_price=px, entry_ts="2026-07-29T10:00:00+00:00", size_usdc=10.0,
            reviewer_verdict="pending", reviewer_score=None, holder_snapshot=None)

    calls = []

    def fake_clob(token_ids, timeout=10.0):
        calls.append(sorted(token_ids))
        return {"111": 0.66, "222": 0.27}

    gamma_calls = []

    def fake_markets(cids, timeout=10.0):
        gamma_calls.append(sorted(cids))
        return {}

    with patch.object(intel_paper_mark, "_clob_bids", fake_clob), \
         patch.object(intel_paper_mark, "all_mids", return_value={"flx:OIL": 66.0}), \
         patch.object(intel_paper_mark, "fetch_markets", fake_markets):
        out = intel_paper_mark.mark_cycle(conn)

    assert out == {"pm": 2, "hl": 1, "closed": 0, "errors": 0}
    assert calls == [["111", "222"]], "all PM tokens must go in ONE request"
    assert gamma_calls == [["0xa", "0xb"]], \
        "resolution check must also batch — one Gamma call for all open markets"

    rows = {r["instrument"]: r for r in intel_paper.open_positions(conn)}
    assert rows["0xa"]["last_pnl_pct"] == pytest.approx(10.0)
    assert rows["flx:OIL"]["last_pnl_pct"] == pytest.approx(10.0)


def test_mark_cycle_skips_missing_prices_without_failing(conn):
    from sniper.services import intel_paper_mark

    intel_paper.open_position(
        conn, idea_id=1, venue="pm", instrument="0xa", token_id="111",
        event_title=None, outcome_label=None, direction="buy_no",
        entry_price=0.6, entry_ts="2026-07-29T10:00:00+00:00", size_usdc=10.0,
        reviewer_verdict="pending", reviewer_score=None, holder_snapshot=None)

    with patch.object(intel_paper_mark, "_clob_bids", return_value={}), \
         patch.object(intel_paper_mark, "all_mids", return_value={}), \
         patch.object(intel_paper_mark, "fetch_markets", return_value={}):
        out = intel_paper_mark.mark_cycle(conn)

    assert out["pm"] == 0
    assert intel_paper.open_positions(conn)[0]["last_mark_price"] is None


# ---------------------------------------------------------------------------
# Task 6 — cron entrypoint
# ---------------------------------------------------------------------------

def test_mark_job_runs_as_a_bare_script():
    """Cron invokes this as `python3 scripts/...`, which does not put the repo on
    sys.path — the same bug that silently disabled intel_gate_read (audit F-11)."""
    import os
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "scripts/intel_paper_mark_job.py", "--dry-run"],
        capture_output=True, text=True, timeout=120,
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
    )
    assert "Traceback" not in proc.stderr, proc.stderr[-400:]
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# Task 7 — read API
# ---------------------------------------------------------------------------

def test_positions_endpoint_reports_mark_age_and_staleness(conn):
    from datetime import datetime, timedelta, timezone

    from flask import Flask

    from sniper.routes.api_intel_paper import create_api_intel_paper_blueprint

    pid = intel_paper.open_position(
        conn, idea_id=1, venue="pm", instrument="0xa", token_id="111",
        event_title="E", outcome_label="July 31", direction="buy_no",
        entry_price=0.60, entry_ts="2026-07-29T10:00:00+00:00", size_usdc=10.0,
        reviewer_verdict="rejected", reviewer_score=87, holder_snapshot=None)
    old = (datetime.now(timezone.utc) - timedelta(seconds=1800)).isoformat()
    intel_paper.record_mark(conn, pid, old, 0.66, 10.0)

    app = Flask(__name__)
    app.register_blueprint(create_api_intel_paper_blueprint({"conn": conn}))
    body = app.test_client().get("/api/intel-paper/positions?venue=pm").get_json()

    assert body["count"] == 1
    assert body["items"][0]["outcome_label"] == "July 31"
    assert body["items"][0]["reviewer_verdict"] == "rejected"
    assert body["mark_age_s"] >= 1700
    assert body["stale"] is True, "a 30-minute-old mark must not present as current"


def test_positions_endpoint_makes_no_network_call(conn):
    from flask import Flask

    from sniper.routes.api_intel_paper import create_api_intel_paper_blueprint

    app = Flask(__name__)
    app.register_blueprint(create_api_intel_paper_blueprint({"conn": conn}))
    with patch("urllib.request.urlopen", side_effect=AssertionError("no HTTP in a GET")):
        assert app.test_client().get("/api/intel-paper/positions").status_code == 200


def test_positions_endpoint_rejects_a_bad_venue(conn):
    from flask import Flask

    from sniper.routes.api_intel_paper import create_api_intel_paper_blueprint

    app = Flask(__name__)
    app.register_blueprint(create_api_intel_paper_blueprint({"conn": conn}))
    assert app.test_client().get("/api/intel-paper/positions?venue=nope").status_code == 400


# ---------------------------------------------------------------------------
# Task 8 — UI wiring
# ---------------------------------------------------------------------------

def test_ui_wires_both_surfaces():
    from pathlib import Path

    html = Path("templates/index.html").read_text()
    js = Path("static/app.js").read_text()

    assert "intel-paper-hl" in html, "Geopolitics needs a Paper fold for HL positions"
    assert "intel-paper-pm" in html, "PM Paper Trades sub-tab needs an intel container"

    assert "/api/intel-paper/positions" in js
    body = js.split("renderIntelPaper(containerId, venue)")[1][:4000]
    assert "outcome_label" in body, "multi-outcome rows must name the outcome"
    assert "event_title" in body, "rows must name the parent event"
    assert "reviewer_verdict" in body, "the reviewer verdict is a visible tag"
    assert "mark_age_s" in body, "mark age must be rendered, not hidden"

    # both call sites wired
    assert "renderIntelPaper('intel-paper-hl', 'hl')" in js
    assert "renderIntelPaper('intel-paper-pm', 'pm')" in js


# ---------------------------------------------------------------------------
# Task 9 — producer wiring
# ---------------------------------------------------------------------------

def test_producer_opens_a_paper_position_per_candidate(tmp_path, monkeypatch):
    import brain as brain_mod
    from sniper.services import news_intel as ni

    b = brain_mod.Brain(db_path=str(tmp_path / "b.db"))
    intel = ni.NewsIntel.__new__(ni.NewsIntel)
    intel.brain = b
    intel.db_conn = None

    monkeypatch.setattr(ni, "fetch_all_feeds", lambda: [
        {"source": "bbc_world", "title": "Iran escalation continues in the region",
         "description": "d", "url": "u", "published_at": "2026-07-29T08:00:00+00:00"}])
    monkeypatch.setattr(ni, "fetch_polymarket_markets", lambda: [
        {"conditionId": "0xgood", "question": "Q1", "outcomePrices": '["0.40","0.60"]'}])
    monkeypatch.setattr(b, "store_news", lambda items: len(items))
    monkeypatch.setattr(b, "expire_stale_ideas", lambda: 0)
    monkeypatch.setattr("sniper.analysis.world_context.update_world_context",
                        lambda news, markets, **kw: [
                            {"condition_id": "0xgood", "market_title": "Q1",
                             "direction": "buy_yes", "confidence": "medium",
                             "causal_chain": "c",
                             "news_trigger_ts": "2026-07-29T08:00:00+00:00"}])
    monkeypatch.setattr(ni, "resolve_token", lambda cid, outcome: "tok-1")
    monkeypatch.setattr(ni, "snapshot_prices", lambda t, e, d, price_fn=None: (0.31, 0.44))
    monkeypatch.setattr(ni, "_send_telegram_idea", lambda *a, **k: None)
    monkeypatch.setattr(ni, "open_for_idea",
                        lambda conn, idea, hl_universe_set=None:
                            {"disposition": "pm", "position_id": 1, "reason": None})
    monkeypatch.setattr(ni, "load_universe", lambda: set())

    stats = intel.run_once()
    assert stats["ideas_created"] == 1
    assert stats["paper_pm"] == 1
    assert stats["paper_hl"] == 0
    assert stats["paper_none"] == 0


# ---------------------------------------------------------------------------
# Closing — manual, or on resolution. Nothing else closes a position.
# ---------------------------------------------------------------------------

def test_fetch_markets_batches_with_repeated_params():
    from sniper.markets import pm_resolver

    captured = {}

    def _capture(req, timeout=None):
        captured["url"] = req.full_url
        return _resp([
            dict(_GAMMA_MARKET[0]),
            dict(_GAMMA_MARKET[0], groupItemTitle="August 31",
                 clobTokenIds='["333", "444"]'),
        ])

    with patch("urllib.request.urlopen", _capture):
        out = pm_resolver.fetch_markets(["0xabc", "0xdef"])

    assert captured["url"].count("condition_ids=") == 2, \
        "must use repeated params — comma-joining returns nothing"
    assert set(out) == {"0xabc", "0xdef"}
    assert out["0xdef"]["outcome_label"] == "August 31"


def test_mark_cycle_closes_a_resolved_position(conn):
    from sniper.services import intel_paper_mark

    pid = intel_paper.open_position(
        conn, idea_id=1, venue="pm", instrument="0xa", token_id="222",
        event_title="E", outcome_label="July 31", direction="buy_no",
        entry_price=0.62, entry_ts="2026-07-29T10:00:00+00:00", size_usdc=10.0,
        reviewer_verdict="pending", reviewer_score=None, holder_snapshot=None)

    resolved = {"0xa": {"condition_id": "0xa", "closed": True,
                        "token_ids": {"YES": "111", "NO": "222"},
                        "prices": {"YES": 0.0, "NO": 1.0},
                        "event_title": "E", "outcome_label": "July 31",
                        "question": "Q", "best_bid": None, "best_ask": None,
                        "end_date_iso": "2026-07-31", "neg_risk": False}}

    with patch.object(intel_paper_mark, "_clob_bids", return_value={"222": 0.99}), \
         patch.object(intel_paper_mark, "all_mids", return_value={}), \
         patch.object(intel_paper_mark, "fetch_markets", return_value=resolved):
        out = intel_paper_mark.mark_cycle(conn)

    assert out["closed"] == 1
    assert intel_paper.open_positions(conn) == [], "a resolved position must not stay open"
    row = conn.execute(
        "SELECT status, close_price, close_reason FROM intel_paper_positions "
        "WHERE position_id=?", (pid,)).fetchone()
    assert row["status"] == "closed"
    assert row["close_price"] == 1.0, "close at OUR side's resolved price"
    assert row["close_reason"] == "resolved"


def test_mark_cycle_leaves_unresolved_positions_open(conn):
    from sniper.services import intel_paper_mark

    intel_paper.open_position(
        conn, idea_id=1, venue="pm", instrument="0xa", token_id="222",
        event_title="E", outcome_label="July 31", direction="buy_no",
        entry_price=0.62, entry_ts="2026-07-29T10:00:00+00:00", size_usdc=10.0,
        reviewer_verdict="pending", reviewer_score=None, holder_snapshot=None)

    live = {"0xa": {"condition_id": "0xa", "closed": False,
                    "token_ids": {"YES": "111", "NO": "222"},
                    "prices": {"YES": 0.2, "NO": 0.8},
                    "event_title": "E", "outcome_label": "July 31",
                    "question": "Q", "best_bid": None, "best_ask": None,
                    "end_date_iso": "2026-07-31", "neg_risk": False}}

    with patch.object(intel_paper_mark, "_clob_bids", return_value={"222": 0.79}), \
         patch.object(intel_paper_mark, "all_mids", return_value={}), \
         patch.object(intel_paper_mark, "fetch_markets", return_value=live):
        out = intel_paper_mark.mark_cycle(conn)

    assert out["closed"] == 0
    assert len(intel_paper.open_positions(conn)) == 1
    # HL positions never resolve, so they are manual-close only — no time rule exists.


def test_manual_close_endpoint(conn):
    from flask import Flask

    from sniper.routes.api_intel_paper import create_api_intel_paper_blueprint

    pid = intel_paper.open_position(
        conn, idea_id=1, venue="hl", instrument="flx:OIL", token_id=None,
        event_title=None, outcome_label=None, direction="long",
        entry_price=60.0, entry_ts="2026-07-29T10:00:00+00:00", size_usdc=10.0,
        reviewer_verdict="pending", reviewer_score=None, holder_snapshot=None)
    intel_paper.record_mark(conn, pid, "2026-07-29T12:00:00+00:00", 66.0, 10.0)

    app = Flask(__name__)
    app.register_blueprint(create_api_intel_paper_blueprint({"conn": conn}))
    body = app.test_client().post(f"/api/intel-paper/positions/{pid}/close").get_json()

    assert body["ok"] is True
    assert body["close_price"] == 66.0, "manual close uses the last mark"
    row = conn.execute(
        "SELECT status, close_reason FROM intel_paper_positions WHERE position_id=?",
        (pid,)).fetchone()
    assert row["status"] == "closed"
    assert row["close_reason"] == "manual"


def test_manual_close_refuses_an_unmarked_position(conn):
    from flask import Flask

    from sniper.routes.api_intel_paper import create_api_intel_paper_blueprint

    pid = intel_paper.open_position(
        conn, idea_id=1, venue="hl", instrument="flx:OIL", token_id=None,
        event_title=None, outcome_label=None, direction="long",
        entry_price=60.0, entry_ts="2026-07-29T10:00:00+00:00", size_usdc=10.0,
        reviewer_verdict="pending", reviewer_score=None, holder_snapshot=None)

    app = Flask(__name__)
    app.register_blueprint(create_api_intel_paper_blueprint({"conn": conn}))
    res = app.test_client().post(f"/api/intel-paper/positions/{pid}/close")

    assert res.status_code == 400, "closing with no price would invent one"
    assert intel_paper.open_positions(conn)[0]["status"] == "open"


# ---------------------------------------------------------------------------
# negRisk + sibling-market independence
# ---------------------------------------------------------------------------

def test_negrisk_markets_are_still_binary():
    """27/100 top markets are negRisk, so they WILL enter the book. Verified live
    2026-07-29: every sampled one has 2 outcomes, 2 tokens and YES+NO == 1.0000.
    No special pricing logic is needed — but pin the assumption so a venue change
    surfaces as a test failure rather than as wrong PnL."""
    from sniper.markets import pm_resolver

    payload = [dict(_GAMMA_MARKET[0], negRisk=True,
                    outcomePrices='["0.2395", "0.7605"]')]
    with patch("urllib.request.urlopen", return_value=_resp(payload)):
        m = pm_resolver.fetch_market("0xneg")

    assert m["neg_risk"] is True
    assert set(m["token_ids"]) == {"YES", "NO"}
    assert abs(sum(m["prices"].values()) - 1.0) < 0.02, \
        "negRisk market stopped summing to 1 — pricing assumptions need review"


def test_neg_risk_is_persisted(conn):
    pid = _open(conn, neg_risk=1)
    row = conn.execute(
        "SELECT neg_risk FROM intel_paper_positions WHERE position_id=?", (pid,)).fetchone()
    assert row["neg_risk"] == 1


def test_sibling_positions_are_flagged_as_one_cluster(conn):
    """Two outcomes of the same event are mechanically correlated — nested or
    mutually exclusive — so they are ONE observation, not two. Surfacing the count
    stops a future sample size being silently inflated."""
    # Sibling outcomes of one event are separate markets with their own
    # condition_ids, so each gets a distinct instrument here.
    _open(conn, idea_id=1, instrument="0xjul", outcome_label="July 31")
    _open(conn, idea_id=2, instrument="0xaug", outcome_label="August 31")
    _open(conn, idea_id=3, instrument="0xoth", event_title="Different Event",
          outcome_label="X")

    rows = {r["outcome_label"]: r for r in intel_paper.open_positions(conn)}
    assert rows["July 31"]["event_position_count"] == 2
    assert rows["August 31"]["event_position_count"] == 2
    assert rows["X"]["event_position_count"] == 1


def test_hl_positions_do_not_cluster_on_null_event(conn):
    """HL positions have no event_title; NULLs must not all collapse into one cluster."""
    _open(conn, idea_id=1, venue="hl", instrument="flx:OIL", token_id=None,
          event_title=None, outcome_label=None, direction="long", entry_price=60.0)
    _open(conn, idea_id=2, venue="hl", instrument="xyz:TSLA", token_id=None,
          event_title=None, outcome_label=None, direction="long", entry_price=400.0)

    for r in intel_paper.open_positions(conn, venue="hl"):
        assert r["event_position_count"] == 1, "NULL event_title is not a cluster key"
