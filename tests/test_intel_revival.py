"""Regression tests for the Intel/Geopolitics revival.

Plan:  docs/superpowers/plans/2026-07-29-intel-geopolitics-revival.md
Audit: docs/audits/2026-07-29-intel-geopolitics-end-to-end-audit.md

Finding IDs (F-01…F-21) in the assertions refer to the audit.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch


def _fake_urlopen(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


# ---------------------------------------------------------------------------
# Task 1 — expiry filter + event lineage (F-03, F-21)
# ---------------------------------------------------------------------------

def test_market_opportunities_requests_unexpired_only_and_keeps_event_id(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "key")
    from sniper.analysis import world_context

    captured = {}

    def _capture(req, timeout=None):
        captured["url"] = req.full_url
        return _fake_urlopen([
            {"instrument": "X", "direction": "buy_no", "thesis": "t",
             "entry_reasoning": "r", "exit_conditions": None, "confidence": "medium",
             "status": "idea", "source_agent": "world_analyst",
             "created_at": "2026-07-14T00:00:00+00:00", "expires_at": "2026-07-22T00:00:00+00:00",
             "polymarket_condition_id": None, "event_id": "evt-123"},
        ])

    with patch("urllib.request.urlopen", _capture):
        out = world_context.get_market_opportunities(limit=5)

    assert "expires_at" in captured["url"], "query must constrain expires_at (F-03)"
    assert "now()" in captured["url"], "query must compare against server time (F-03)"
    assert out["items"][0]["event_id"] == "evt-123", "lineage must survive the projection (F-21)"
    assert out["items"][0]["market_verified"] is False


# ---------------------------------------------------------------------------
# Task 2 — data age is visible (F-02, F-06)
# ---------------------------------------------------------------------------

def test_geo_brief_shows_event_age_and_panel_is_labelled_archive():
    app_js = Path("static/app.js").read_text()
    index_html = Path("templates/index.html").read_text()

    # Event-field card must carry the data timestamp, not just a count
    # Split on the label literals, not bare prose — comments mention the labels too.
    brief = app_js.split("_renderGeoBrief()")[1].split("_deriveFinanceRegime")[0]
    assert "label: 'Event field'" in brief
    event_card = brief.split("label: 'Event field'")[1].split("label: 'Idea queue'")[0]
    assert "freshness" in event_card, "Event field card must render freshness (F-02)"

    # News pulse must use the NEWS timestamp, not the world-event one
    news_card = brief.split("label: 'News pulse'")[1]
    assert "newsUpdatedAt" in news_card, "News pulse must use its own timestamp (F-06)"
    assert "freshness" not in news_card, "News pulse must not reuse the world-event freshness (F-06)"

    # Research cards must show their own age
    research = app_js.split("_loadResearchIdeas(")[1].split("async loadIdeas")[0]
    assert "created_at" in research, "research cards must render created_at (F-02)"

    # Stale data must be announced, not inferred
    assert "intel-geo-stale-banner" in app_js
    assert "intel-geo-stale-banner" in index_html

    # Panel must not be titled 'Trade Ideas' while it renders unverified archive research
    assert "Research notes" in index_html


# ---------------------------------------------------------------------------
# Task 3 — honest availability + working dismiss (F-08, F-10)
# ---------------------------------------------------------------------------

def test_news_feed_reports_unavailable_when_empty():
    import inspect
    from sniper.routes import api_core

    src = inspect.getsource(api_core)
    fn = src.split("def api_news_feed()")[1].split("@bp.post")[0]
    assert '"available": bool(news)' in fn, \
        "empty news must not report available:true (F-08)"
    assert '"stale"' in fn, "news feed must expose staleness (F-08)"
    docstring = fn.split('"""')[1]
    assert "Supabase" not in docstring, \
        "docstring must not claim Supabase — get_recent_news reads SQLite (F-08)"


def test_dismiss_checks_response_before_fading():
    app_js = Path("static/app.js").read_text()
    fn = app_js.split("async dismissIdea(")[1].split("scrollToEvent(")[0]
    assert "res.ok" in fn, "dismiss must check the response before fading the card (F-10)"
    # The optimistic fade must come AFTER the success check, not before it
    assert fn.index("res.ok") < fn.index("opacity"), \
        "response check must precede the fade (F-10)"


# ---------------------------------------------------------------------------
# Task 4 — producer is startable, weakest path deleted (F-04, audit §12.1/§12.2)
# ---------------------------------------------------------------------------

def test_news_intel_creates_the_lock_directory():
    from sniper.services import news_intel

    lock_dir = Path(news_intel.LOCK_PATH).parent
    assert lock_dir.name == "logs", f"lock must live in logs/, got {lock_dir}"
    assert (lock_dir.parent / "sniper").is_dir(), \
        f"lock dir must be under the repo root, got {lock_dir}"
    source = Path("sniper/services/news_intel.py").read_text()
    entrypoint = source.split('if __name__ == "__main__":', 1)[1]
    create = "os.makedirs(os.path.dirname(_lock_path), exist_ok=True)"
    lock_open = 'with open(_lock_path, "w")'
    assert create in entrypoint
    assert entrypoint.index(create) < entrypoint.index(lock_open)


def test_keyword_fallback_candidate_path_is_gone():
    src = Path("sniper/services/news_intel.py").read_text()
    assert "def match_news_to_markets" not in src, "keyword candidate path must be deleted"
    assert "def build_reasoning" not in src
    assert "def _keywords_from_market" not in src
    # Display heuristics survive — sentiment drives a dot, topics gate relevance
    assert "YES_SIGNALS" in src, "sentiment display heuristic is kept"
    assert "def extract_topics" in src, "topic tagging is kept"


# ---------------------------------------------------------------------------
# Task 5 — retire the supabase SDK path (F-12)
# ---------------------------------------------------------------------------

def test_brain_does_not_import_supabase_sdk():
    src = Path("brain.py").read_text()
    assert "from supabase import create_client" not in src, \
        "brain.py must not depend on the supabase SDK (F-12) — use bot.supabase_helper"


def test_brain_reports_supabase_available_from_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "key")
    import importlib
    import brain as brain_mod
    importlib.reload(brain_mod)
    assert brain_mod.Brain(db_path=":memory:").sb_available is True

    monkeypatch.delenv("SUPABASE_URL")
    monkeypatch.delenv("SUPABASE_SERVICE_KEY")
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    assert brain_mod.Brain(db_path=":memory:").sb_available is False


def test_brain_kv_reads_still_filter_active_status(monkeypatch):
    """Ported from test_brain_read_active_filter — retired rows must never surface.

    The SDK's .eq('status','active') becomes 'status=eq.active' in the REST path.
    """
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "key")
    import importlib
    import brain as brain_mod
    importlib.reload(brain_mod)

    paths: list = []

    def _record(method, path, body=None):
        paths.append(path)
        return []

    monkeypatch.setattr(brain_mod, "_sb_request", _record)
    b = brain_mod.Brain(db_path=":memory:")

    b.read_entry("markets/state")
    assert any("status=eq.active" in p for p in paths), "read_entry must filter status=active"
    assert any("name=eq.markets%2Fstate" in p for p in paths), \
        "the key must be URL-encoded — 'markets/state' contains a path separator"

    paths.clear()
    b.read_all()
    assert any("type=eq.reference" in p for p in paths)
    assert any("status=eq.active" in p for p in paths), "read_all must filter status=active"


# ---------------------------------------------------------------------------
# Task 6 — the paper strategy row (F-09)
# ---------------------------------------------------------------------------

_STRATEGIES_DDL = """CREATE TABLE strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    label TEXT,
    strategy_type TEXT NOT NULL,
    parameters TEXT NOT NULL DEFAULT "{}",
    status TEXT NOT NULL DEFAULT "active",
    ai_generated INTEGER DEFAULT 0,
    ai_analysis TEXT,
    trades_triggered INTEGER DEFAULT 0,
    pnl_usdc REAL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
, instance_tag TEXT, lifecycle_stage TEXT NOT NULL DEFAULT 'paper')"""


def test_seed_creates_exactly_one_paper_strategy(tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute(_STRATEGIES_DDL)
    conn.commit()

    from scripts.seed_news_intel_strategy import seed

    assert seed(conn) is not None
    assert seed(conn) is None, "second call must be idempotent"

    rows = conn.execute(
        "SELECT strategy_type, status, lifecycle_stage FROM strategies").fetchall()
    assert rows == [("news_intel_v1", "active", "paper")]


def test_seeded_strategy_is_paper_and_not_momentum(tmp_path):
    """direction_mode=momentum consistently loses on geopolitical signals —
    brain: patterns/strategy-news-intel-geo-momentum-misalignment."""
    import json as _json
    import sqlite3

    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute(_STRATEGIES_DDL)
    conn.commit()

    from scripts.seed_news_intel_strategy import seed

    seed(conn)
    params = _json.loads(conn.execute("SELECT parameters FROM strategies").fetchone()[0])
    assert params["execution_mode"] == "paper", "no funded route is enabled by this plan"
    assert params["direction_mode"] == "idea", "momentum mode loses on geopolitical signals"


# ---------------------------------------------------------------------------
# Task 7 — freshness pager (audit §15: the outage was invisible to process watchdogs)
# ---------------------------------------------------------------------------

def test_freshness_alert_is_data_based_not_process_based():
    script = Path("scripts/intel_freshness_alert.sh")
    assert script.exists(), "freshness alert script must exist"
    assert os.access(script, os.X_OK), "script must be executable"

    body = script.read_text()
    assert "news_items" in body, \
        "must check data freshness — there is no process to watch (audit §15)"
    assert "SENTINEL" in body, "must be rate-limited by a sentinel"
    assert "mode=ro" in body, "freshness check must open the DB read-only"
    assert 'rm -f "$SENTINEL"' in body, "must re-arm once the feed recovers"


# ---------------------------------------------------------------------------
# Task 8 — resolver: token id + price snapshots (F-01, F-14, audit §8)
# ---------------------------------------------------------------------------

def test_resolve_token_returns_asset_id_for_requested_outcome():
    from sniper.markets import pm_resolver

    payload = {"tokens": [
        {"token_id": "111", "outcome": "Yes", "price": 0.42},
        {"token_id": "222", "outcome": "No", "price": 0.58},
    ]}
    with patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        assert pm_resolver.resolve_token("0xcid", "NO") == "222"
    with patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        assert pm_resolver.resolve_token("0xcid", "YES") == "111"


def test_resolve_token_returns_none_on_unknown_market():
    from sniper.markets import pm_resolver

    with patch("urllib.request.urlopen", side_effect=OSError("404")):
        assert pm_resolver.resolve_token("0xbogus", "YES") is None


def test_resolve_token_rejects_bad_inputs():
    from sniper.markets import pm_resolver

    assert pm_resolver.resolve_token("", "YES") is None
    assert pm_resolver.resolve_token("0xcid", "MAYBE") is None
    # Outcome present but token_id missing -> unresolvable, not a blank id
    payload = {"tokens": [{"outcome": "Yes", "price": 0.42}]}
    with patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        assert pm_resolver.resolve_token("0xcid", "YES") is None


def test_snapshot_prices_uses_supplied_price_fn_at_both_timestamps():
    from sniper.markets import pm_resolver

    calls = []

    def fake_fn(market_id, ts):
        calls.append((market_id, ts))
        return 0.30 if ts == 1000 else 0.55

    assert pm_resolver.snapshot_prices("111", 1000, 2000, price_fn=fake_fn) == (0.30, 0.55)
    assert calls == [("111", 1000), ("111", 2000)]


# ---------------------------------------------------------------------------
# Task 9 — measurement columns + "no candidate" as a first-class output (audit §11)
# ---------------------------------------------------------------------------

def test_unresolvable_market_is_recorded_as_not_executable(tmp_path):
    import brain as brain_mod

    b = brain_mod.Brain(db_path=str(tmp_path / "b.db"))
    idea_id = b.add_trade_idea(
        condition_id="0xbogus", market_title="T", outcome="YES", direction="buy_yes",
        reasoning="r", source_agent="news_intel", reject_reason="unresolvable_market",
    )
    assert idea_id not in (None, -1), "rejections are recorded, not silently dropped"

    row = b._get_conn().execute(
        "SELECT status, reject_reason, token_id FROM trade_ideas WHERE id=?", (idea_id,)
    ).fetchone()
    assert row["status"] == "not_executable"
    assert row["reject_reason"] == "unresolvable_market"
    assert row["token_id"] is None


def test_resolved_candidate_persists_both_price_snapshots(tmp_path):
    import brain as brain_mod

    b = brain_mod.Brain(db_path=str(tmp_path / "b.db"))
    idea_id = b.add_trade_idea(
        condition_id="0xreal", market_title="T", outcome="NO", direction="buy_no",
        reasoning="r", source_agent="news_intel",
        token_id="222", event_ts="2026-07-29T08:00:00+00:00",
        price_at_event=0.30, price_at_decision=0.55,
    )
    row = b._get_conn().execute(
        "SELECT status, token_id, event_ts, price_at_event, price_at_decision "
        "FROM trade_ideas WHERE id=?", (idea_id,)).fetchone()
    assert row["status"] == "open"
    assert row["token_id"] == "222"
    assert row["event_ts"] == "2026-07-29T08:00:00+00:00"
    assert (row["price_at_event"], row["price_at_decision"]) == (0.30, 0.55)


def test_rejection_does_not_block_a_later_real_candidate(tmp_path):
    """UNIQUE(condition_id, direction) + INSERT OR IGNORE means a stored rejection
    would permanently shadow the same market/direction. A rejection is a transient
    observation; the newest row wins."""
    import brain as brain_mod

    b = brain_mod.Brain(db_path=str(tmp_path / "b.db"))
    first = b.add_trade_idea(
        condition_id="0xsame", market_title="T", outcome="YES", direction="buy_yes",
        reasoning="r", source_agent="news_intel", reject_reason="unresolvable_market",
    )
    assert first not in (None, -1)

    second = b.add_trade_idea(
        condition_id="0xsame", market_title="T", outcome="YES", direction="buy_yes",
        reasoning="r2", source_agent="news_intel",
        token_id="111", price_at_event=0.20, price_at_decision=0.25,
    )
    assert second not in (None, -1), "a prior rejection must not shadow a real candidate"

    rows = b._get_conn().execute(
        "SELECT status, token_id FROM trade_ideas WHERE condition_id='0xsame'").fetchall()
    assert len(rows) == 1, "the stale rejection is replaced, not accumulated"
    assert rows[0]["status"] == "open" and rows[0]["token_id"] == "111"


def test_open_candidates_are_still_deduplicated(tmp_path):
    """CS-guard: the dedup index must still stop a genuine duplicate open idea."""
    import brain as brain_mod

    b = brain_mod.Brain(db_path=str(tmp_path / "b.db"))
    kw = dict(condition_id="0xdup", market_title="T", outcome="YES", direction="buy_yes",
              reasoning="r", source_agent="news_intel", token_id="111",
              price_at_decision=0.25)
    assert b.add_trade_idea(**kw) not in (None, -1)
    assert b.add_trade_idea(**kw) is None, "duplicate open idea must still be ignored"


def test_producer_wiring_end_to_end(tmp_path, monkeypatch):
    """Integration: one Claude idea -> resolver -> persisted candidate with prices,
    and one unresolvable idea -> recorded rejection. Catches wiring bugs (wrong
    kwarg, wrong variable) that unit tests on either half would miss."""
    import brain as brain_mod
    from sniper.services import news_intel as ni

    b = brain_mod.Brain(db_path=str(tmp_path / "b.db"))
    intel = ni.NewsIntel.__new__(ni.NewsIntel)
    intel.brain = b
    intel.db_conn = None

    monkeypatch.setattr(ni, "fetch_all_feeds", lambda: [
        {"source": "bbc_world", "title": "Iran strikes base in escalation" * 2,
         "description": "d", "url": "u", "published_at": "2026-07-29T08:00:00+00:00"},
    ])
    monkeypatch.setattr(ni, "fetch_polymarket_markets", lambda: [
        {"conditionId": "0xgood", "question": "Q1", "outcomePrices": '["0.40","0.60"]'},
        {"conditionId": "0xbad", "question": "Q2", "outcomePrices": '["0.40","0.60"]'},
    ])
    monkeypatch.setattr(b, "store_news", lambda items: len(items))
    monkeypatch.setattr(b, "expire_stale_ideas", lambda: 0)
    monkeypatch.setattr(
        "sniper.analysis.world_context.update_world_context",
        lambda news, markets, **kw: [
            {"condition_id": "0xgood", "market_title": "Q1", "direction": "buy_yes",
             "confidence": "medium", "causal_chain": "c",
             "news_trigger_ts": "2026-07-29T08:00:00+00:00"},
            {"condition_id": "0xbad", "market_title": "Q2", "direction": "buy_no",
             "confidence": "medium", "causal_chain": "c"},
        ],
    )
    monkeypatch.setattr(ni, "resolve_token",
                        lambda cid, outcome: "tok-1" if cid == "0xgood" else None)
    monkeypatch.setattr(ni, "snapshot_prices",
                        lambda tok, ev, dec, price_fn=None: (0.31, 0.44))
    monkeypatch.setattr(ni, "_send_telegram_idea", lambda *a, **k: None)

    stats = intel.run_once()
    assert stats["ideas_created"] == 1, "only the resolvable market becomes a candidate"

    rows = {r["condition_id"]: dict(r) for r in b._get_conn().execute(
        "SELECT condition_id, status, token_id, price_at_event, price_at_decision, "
        "event_ts, reject_reason FROM trade_ideas").fetchall()}

    good = rows["0xgood"]
    assert good["status"] == "open"
    assert good["token_id"] == "tok-1"
    assert (good["price_at_event"], good["price_at_decision"]) == (0.31, 0.44)
    assert good["event_ts"].startswith("2026-07-29T08:00:00"), \
        "the LLM-supplied news timestamp must be used, not wall time"

    bad = rows["0xbad"]
    assert bad["status"] == "not_executable"
    assert bad["reject_reason"] == "unresolvable_market"
    assert bad["token_id"] is None


# ---------------------------------------------------------------------------
# Task 11 — pre-registered Gate A (audit §8: "already priced" was never computed)
# ---------------------------------------------------------------------------

def _cap_row(pe, pd, pf):
    return {"price_at_event": pe, "price_at_decision": pd, "price_final": pf}


def test_capture_gate_verdicts():
    from scripts.intel_gate_read import compute_capture

    assert compute_capture([_cap_row(0.3, 0.31, 0.5)] * 9)["verdict"] == "insufficient_evidence"

    # 95% of the move already gone at decision time -> the program is too late
    late = compute_capture([_cap_row(0.30, 0.49, 0.50)] * 10)
    assert late["n"] == 10
    assert late["median_capture"] > 0.9
    assert late["verdict"] == "kill"

    # 20% gone -> there is residual move to compete for
    assert compute_capture([_cap_row(0.30, 0.34, 0.50)] * 10)["verdict"] == "continue"

    # 70% gone -> ambiguous, needs more samples before deciding
    assert compute_capture([_cap_row(0.30, 0.44, 0.50)] * 10)["verdict"] == "ambiguous"


def test_capture_excludes_zero_move_rows_without_dividing_by_zero():
    from scripts.intel_gate_read import compute_capture

    # No move at all carries no information about lateness — must not become inf/NaN
    out = compute_capture([_cap_row(0.3, 0.3, 0.3)] * 10)
    assert out["verdict"] == "insufficient_evidence"
    assert out["n"] == 0


def test_capture_ignores_incomplete_rows():
    from scripts.intel_gate_read import compute_capture

    rows = [_cap_row(0.30, 0.34, 0.50)] * 10 + [
        _cap_row(None, 0.34, 0.50), _cap_row(0.30, None, 0.50), _cap_row(0.30, 0.34, None),
    ]
    assert compute_capture(rows)["n"] == 10, "rows missing any price are excluded, not defaulted"


def test_gate_thresholds_match_the_preregistered_plan():
    """Thresholds are pre-registered; drifting them after seeing results is the
    exact failure mode recorded in finding-crypto5m-floor-endogeneity."""
    from scripts import intel_gate_read as g

    assert (g.GATE_A_MIN_N, g.GATE_A_KILL, g.GATE_A_AMBIGUOUS) == (10, 0.80, 0.60)


def test_gate_read_runs_as_a_bare_script(tmp_path):
    """The bare script must load project modules and read an isolated database."""
    import subprocess
    import sys as _sys

    db_path = tmp_path / "intel-gate.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE trade_ideas (
               id INTEGER PRIMARY KEY,
               token_id TEXT,
               event_ts TEXT,
               price_at_event REAL,
               price_at_decision REAL
           )"""
    )
    conn.close()

    proc = subprocess.run(
        [
            _sys.executable,
            "scripts/intel_gate_read.py",
            "--gate",
            "a",
            "--db",
            str(db_path),
        ],
        capture_output=True, text=True, timeout=120,
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
    )
    assert "Traceback" not in proc.stderr, f"gate read crashed: {proc.stderr[-400:]}"
    assert "GATE A" in proc.stdout


def test_gate_alert_is_one_shot():
    script = Path("scripts/intel_gate_alert.sh")
    assert script.exists() and os.access(script, os.X_OK)
    body = script.read_text()
    assert "SENTINEL" in body and "intel_gate_read.py" in body


def test_market_opportunities_still_strips_sizing_and_pnl():
    """CS-3 must not regress: sizing / PnL never reach the Intel tab."""
    from sniper.analysis import world_context
    import inspect

    src = inspect.getsource(world_context.get_market_opportunities)
    allowed = src.split("_ALLOWED = (")[1].split(")")[0]
    for banned in ("suggested_size_pct", "max_loss_pct", "pnl_pct", "entry_price", "current_price"):
        assert banned not in allowed, f"{banned} must never be projected to the UI"
