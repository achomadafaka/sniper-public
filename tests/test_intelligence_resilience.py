import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from sniper.services import finance_analyst
import sniper.routes.ws_stream as ws_stream
import scripts.world_analyst as world_analyst
from sniper.routes.api_core import create_api_core_blueprint
from sniper.analysis.world_context import get_world_events_status
import datetime as real_dt


class _FakeResponse:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.ok = ok

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, timeout):
        self.calls.append((url, timeout))
        return _FakeResponse(self.payload)


class TestWorldContextStatus(unittest.TestCase):
    def test_reports_supabase_not_configured(self):
        with patch.dict(os.environ, {"SUPABASE_URL": "", "SUPABASE_ANON_KEY": ""}, clear=False):
            payload = get_world_events_status()

        self.assertFalse(payload["available"])
        self.assertTrue(payload["degraded"])
        self.assertEqual(payload["code"], "supabase_not_configured")
        self.assertIn("SUPABASE_URL", payload["message"])

    def test_api_world_context_returns_status_payload(self):
        app = Flask(__name__)
        app.register_blueprint(create_api_core_blueprint({}))

        fake_payload = {
            "events": [],
            "count": 0,
            "available": False,
            "degraded": True,
            "code": "supabase_not_configured",
            "message": "World context unavailable — SUPABASE_URL and SUPABASE_ANON_KEY are not configured.",
            "source": "unconfigured",
        }

        with patch("sniper.analysis.world_context.get_world_events_status", return_value=fake_payload):
            res = app.test_client().get("/api/world-context")

        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["code"], "supabase_not_configured")
        self.assertEqual(body["source"], "unconfigured")
        self.assertFalse(body["available"])
        self.assertIn("updated_at", body)

    def test_api_world_context_reuses_a_recent_success(self):
        app = Flask(__name__)
        app.register_blueprint(create_api_core_blueprint({}))
        fake_payload = {
            "events": [{"last_updated": "2026-08-14T12:00:00+00:00"}],
            "count": 1,
            "available": True,
            "source": "supabase",
        }

        with patch(
            "sniper.analysis.world_context.get_world_events_status",
            return_value=fake_payload,
        ) as reader:
            first = app.test_client().get("/api/world-context").get_json()
            second = app.test_client().get("/api/world-context").get_json()

        self.assertEqual(reader.call_count, 1)
        self.assertEqual(first["count"], 1)
        self.assertEqual(second["count"], 1)
        self.assertGreaterEqual(second["cache_age_s"], 0)


class TestMarketOpportunitiesProjection(unittest.TestCase):
    def test_get_market_opportunities_strips_sizing_and_pnl(self):
        import io
        from sniper.analysis import world_context as WC
        raw = [{"instrument": "Will the Fed raise rates in 2026?", "direction": "buy_yes",
                "thesis": "macro", "confidence": "high", "status": "idea", "created_at": "t",
                "suggested_size_pct": 5.0, "max_loss_pct": 2.0, "pnl_pct": 12.3,
                "entry_price": 0.4, "current_price": 0.5, "polymarket_condition_id": None}]
        class _Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with patch.dict(os.environ, {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_ANON_KEY": "k"}, clear=False), \
             patch("urllib.request.urlopen", return_value=_Resp(json.dumps(raw).encode())):
            out = WC.get_market_opportunities(limit=10)
        self.assertTrue(out["available"])
        item = out["items"][0]
        for forbidden in ("suggested_size_pct", "max_loss_pct", "pnl_pct", "entry_price", "current_price"):
            self.assertNotIn(forbidden, item, forbidden)
        self.assertEqual(item["instrument"], "Will the Fed raise rates in 2026?")
        self.assertIn("market_verified", item)
        self.assertFalse(item["market_verified"])   # NULL condition_id -> unverified


class TestMarketOpportunities(unittest.TestCase):
    def test_api_market_opportunities_returns_items_and_disclaimer(self):
        app = Flask(__name__)
        app.register_blueprint(create_api_core_blueprint({"safe_int": lambda v, d: d}))
        fake = {"items": [{"instrument": "Will the Fed raise rates in 2026?", "direction": "buy_yes",
                           "confidence": "high", "thesis": "t", "polymarket_condition_id": None}],
                "count": 1, "available": True, "source": "market_opportunities_table"}
        with patch("sniper.analysis.world_context.get_market_opportunities", return_value=fake):
            res = app.test_client().get("/api/market-opportunities")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["available"])
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["items"][0]["instrument"], "Will the Fed raise rates in 2026?")
        self.assertIn("NOT advice", body["disclaimer"])
        self.assertIn("updated_at", body)

    def test_api_market_opportunities_reuses_a_recent_success(self):
        app = Flask(__name__)
        app.register_blueprint(create_api_core_blueprint({"safe_int": lambda v, d: d}))
        fake = {"items": [], "count": 0, "available": True, "source": "supabase"}

        with patch(
            "sniper.analysis.world_context.get_market_opportunities",
            return_value=fake,
        ) as reader:
            first = app.test_client().get("/api/market-opportunities").get_json()
            second = app.test_client().get("/api/market-opportunities").get_json()

        self.assertEqual(reader.call_count, 1)
        self.assertEqual(first["count"], 0)
        self.assertEqual(second["count"], 0)
        self.assertGreaterEqual(second["cache_age_s"], 0)

    def test_api_market_opportunities_degrades_on_reader_error(self):
        app = Flask(__name__)
        app.register_blueprint(create_api_core_blueprint({"safe_int": lambda v, d: d}))
        with patch("sniper.analysis.world_context.get_market_opportunities", side_effect=RuntimeError("boom")):
            res = app.test_client().get("/api/market-opportunities")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertFalse(body["available"])
        self.assertEqual(body["items"], [])


class TestFinanceContextStatus(unittest.TestCase):
    def test_finance_context_reports_unconfigured_brain(self):
        app = Flask(__name__)
        app.register_blueprint(create_api_core_blueprint({}))

        fake_brain = type("FakeBrain", (), {"sb_available": False})()
        with patch("brain.Brain", return_value=fake_brain):
            res = app.test_client().get("/api/finance-context")

        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertFalse(body["available"])
        self.assertFalse(body["report_available"])
        self.assertIn("Supabase brain not configured", body["message"])

    def test_finance_context_distinguishes_empty_report_from_outage(self):
        app = Flask(__name__)
        app.register_blueprint(create_api_core_blueprint({}))

        class FakeBrain:
            sb_available = True

            def read_entry(self, key):
                return None

        with patch("brain.Brain", return_value=FakeBrain()):
            res = app.test_client().get("/api/finance-context")

        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["available"])
        self.assertFalse(body["report_available"])
        self.assertEqual(body["message"], "No market intelligence report yet.")

    def test_finance_context_uses_memory_timestamps(self):
        app = Flask(__name__)
        app.register_blueprint(create_api_core_blueprint({}))

        class FakeBrain:
            sb_available = True

            def read_entry(self, key):
                if key == "markets/state":
                    return {
                        "content": "report body",
                        "type": "knowledge",
                        "updated_at": "2026-03-24T12:00:00Z",
                    }
                if key == "market_boosts_current":
                    return {
                        "content": "[]",
                        "type": "knowledge",
                        "updated_at": "2026-03-24T12:05:00Z",
                    }
                return None

        with patch("brain.Brain", return_value=FakeBrain()):
            res = app.test_client().get("/api/finance-context")

        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["available"])
        self.assertTrue(body["report_available"])
        self.assertEqual(body["report_updated_at"], "2026-03-24T12:00:00Z")
        self.assertEqual(body["boosts_updated_at"], "2026-03-24T12:05:00Z")
        self.assertEqual(body["updated_at"], "2026-03-24T12:05:00Z")


class TestTradeIdeasExpiryFilter(unittest.TestCase):
    def test_trade_ideas_same_day_iso_expiry_remains_visible(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE trade_ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT NOT NULL,
                market_title TEXT NOT NULL,
                outcome TEXT NOT NULL,
                direction TEXT NOT NULL,
                current_price REAL,
                confidence TEXT NOT NULL DEFAULT 'low',
                reasoning TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                expires_at TEXT,
                source_agent TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO trade_ideas (
                condition_id, market_title, outcome, direction, current_price,
                confidence, reasoning, status, created_at, expires_at, source_agent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cond-live",
                "Visible idea",
                "YES",
                "buy_yes",
                0.45,
                "medium",
                "still valid",
                "open",
                "2026-03-24 11:00:00",
                "2026-03-24T13:00:00",
                "world_analyst",
            ),
        )
        conn.execute(
            """
            INSERT INTO trade_ideas (
                condition_id, market_title, outcome, direction, current_price,
                confidence, reasoning, status, created_at, expires_at, source_agent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cond-dead",
                "Expired idea",
                "NO",
                "buy_no",
                0.55,
                "low",
                "expired",
                "open",
                "2026-03-24 10:00:00",
                "2026-03-24T11:59:00",
                "world_analyst",
            ),
        )
        conn.commit()

        app = Flask(__name__)
        app.register_blueprint(
            create_api_core_blueprint(
                {
                    "conn": conn,
                    "safe_int": lambda value, default=0: int(value if value is not None else default),
                }
            )
        )

        fake_now = real_dt.datetime(2026, 3, 24, 12, 0, 0, tzinfo=real_dt.UTC)

        class FakeDateTime(real_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now if tz is not None else fake_now.replace(tzinfo=None)

        try:
            with patch("sniper.routes.api_core.dt.datetime", FakeDateTime):
                res = app.test_client().get("/api/trade-ideas")
        finally:
            conn.close()

        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        titles = [item["market_title"] for item in body["items"]]
        self.assertEqual(titles, ["Visible idea"])


class TestWsStreamSubscriptions(unittest.TestCase):
    def tearDown(self):
        ws_stream._ws_watch_counts.clear()
        ws_stream._ws_needs_resub.clear()
        ws_stream._market_token_cache.clear()

    def test_replace_poly_ws_tokens_tracks_refcounts(self):
        ws_stream._replace_poly_ws_tokens(set(), {"tok-a", "tok-b"})
        ws_stream._replace_poly_ws_tokens(set(), {"tok-b"})

        self.assertEqual(ws_stream._current_poly_token_ids(), {"tok-a", "tok-b"})

        ws_stream._replace_poly_ws_tokens({"tok-a", "tok-b"}, {"tok-b"})
        self.assertEqual(ws_stream._current_poly_token_ids(), {"tok-b"})

        ws_stream._replace_poly_ws_tokens({"tok-b"}, set())
        self.assertEqual(ws_stream._current_poly_token_ids(), {"tok-b"})

        ws_stream._replace_poly_ws_tokens({"tok-b"}, set())
        self.assertEqual(ws_stream._current_poly_token_ids(), set())

    def test_resolve_subscription_pairs_uses_token_ids_from_market_payload(self):
        session = _FakeSession(
            {
                "tokens": [
                    {"outcome": "Yes", "token_id": "tok-yes"},
                    {"outcome": "No", "token_id": "tok-no"},
                ]
            }
        )

        resolved = ws_stream._resolve_subscription_pairs(
            [
                {"condition_id": "0xabc", "outcome": "YES"},
                {"condition_id": "0xabc", "outcome": "NO"},
            ],
            session,
        )

        self.assertEqual(
            resolved,
            {
                ("0xabc", "YES"): "tok-yes",
                ("0xabc", "NO"): "tok-no",
            },
        )
        self.assertEqual(len(session.calls), 1)


class TestWsStreamOptionalDependency(unittest.TestCase):
    def test_has_sync_websockets_handles_missing_parent_module(self):
        with patch(
            "sniper.routes.ws_stream.importlib.util.find_spec",
            side_effect=ModuleNotFoundError,
        ):
            self.assertFalse(ws_stream._has_sync_websockets())

    def test_poly_ws_thread_state_matches_dependency_availability(self):
        # The thread starts in register_ws_stream now, not at import (2026-09-29); the starter
        # still refuses without the websockets dependency.
        with patch.object(ws_stream, "_poly_ws_thread", None), \
                patch.object(ws_stream, "_poly_ws_price_loop", lambda: None):
            started = ws_stream._start_poly_ws_thread(force=True)
            if ws_stream._has_sync_websockets():
                self.assertIsNotNone(started)
                started.join(2.0)
            else:
                self.assertIsNone(started)


class TestWorldAnalystResilience(unittest.TestCase):
    def test_retry_empty_stop_for_instant_zero_tool_exit(self):
        self.assertTrue(world_analyst._should_retry_empty_stop("stop_sequence", 0, 2.0))

    def test_does_not_retry_when_tools_ran_or_stop_is_not_suspicious(self):
        self.assertFalse(world_analyst._should_retry_empty_stop("stop_sequence", 1, 2.0))
        self.assertFalse(world_analyst._should_retry_empty_stop("end_turn", 0, 2.0))
        self.assertFalse(world_analyst._should_retry_empty_stop("stop_sequence", 0, 12.0))

    def test_compose_system_prompt_appends_calibration_when_available(self):
        fake_module = types.SimpleNamespace(
            get_calibration_blurb=lambda agent: "\nCALIBRATION BLURB" if agent == "world_analyst" else ""
        )
        with patch.dict(sys.modules, {"sniper.analysis.agent_calibration": fake_module}):
            prompt, has_calibration = world_analyst._compose_system_prompt()

        self.assertTrue(has_calibration)
        self.assertTrue(prompt.endswith("\nCALIBRATION BLURB"))

    def test_compose_system_prompt_falls_back_when_calibration_errors(self):
        def _boom(_agent):
            raise RuntimeError("db unavailable")

        fake_module = types.SimpleNamespace(get_calibration_blurb=_boom)
        with patch.dict(sys.modules, {"sniper.analysis.agent_calibration": fake_module}):
            prompt, has_calibration = world_analyst._compose_system_prompt()

        self.assertFalse(has_calibration)
        self.assertEqual(prompt, world_analyst.ANALYST_SYSTEM_PROMPT)

    def test_recover_world_brain_from_temp_files(self):
        upserts = []

        def _upsert_memory(*args, **kwargs):
            upserts.append((args, kwargs))

        fake_supabase = types.SimpleNamespace(upsert_memory=_upsert_memory)
        state_payload = {
            "updated": "2026-03-29T20:00:00+00:00",
            "regime": "risk-off",
            "hot_regions": ["Middle East"],
            "tail_risks": ["Hormuz escalation"],
            "macro_bias": "risk-off",
            "cross_domain": "Oil and vol stay bid",
            "agent_note": "Avoid new YES",
            "confidence": 0.68,
        }
        narrative = (
            "## EXECUTIVE SUMMARY: What changed\n"
            "Recovered narrative after agent timeout.\n\n"
            "## GEOPOLITICS: Core shifts\n"
            "- Middle East escalation remained active.\n"
            "- Oil-sensitive markets stayed vulnerable.\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            world_analyst, "WORLD_STATE_TMP", Path(tmpdir) / "world_state_report.json"
        ), patch.object(
            world_analyst, "WORLD_NARRATIVE_TMP", Path(tmpdir) / "world_narrative_report.md"
        ), patch.dict(sys.modules, {"bot.supabase_helper": fake_supabase}):
            world_analyst.WORLD_STATE_TMP.write_text(json.dumps(state_payload), encoding="utf-8")
            world_analyst.WORLD_NARRATIVE_TMP.write_text(narrative + ("x" * 150), encoding="utf-8")

            recovered_state, recovered_narrative = world_analyst._recover_world_brain_from_tmp(max_age_seconds=3600)

        self.assertTrue(recovered_state)
        self.assertTrue(recovered_narrative)
        self.assertEqual([args[1] for args, _kwargs in upserts], ["world_state", "world_narrative"])


class TestFinanceAnalystGuards(unittest.TestCase):
    def test_filter_market_boost_targets_drops_malformed_and_unknown_ids(self):
        good = "0x" + ("a" * 64)
        unknown = "0x" + ("b" * 64)
        filtered, removed = finance_analyst._filter_market_boost_targets(
            {
                good.upper(): {"boost": 12, "direction": "YES"},
                unknown: {"boost": -12, "direction": "NO"},
                "0x1234": {"boost": 9},
            },
            {good},
        )

        self.assertEqual(filtered, {good: {"boost": 12, "direction": "YES"}})
        self.assertEqual(removed, 2)

    def test_load_known_boost_target_ids_reads_metadata_and_open_positions(self):
        good_meta = "0x" + ("1" * 64)
        good_strategy = "0x" + ("2" * 64)
        good_paper = "0x" + ("3" * 64)

        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            conn = sqlite3.connect(tmp.name)
            conn.execute("CREATE TABLE market_metadata (condition_id TEXT)")
            conn.execute("CREATE TABLE strategy_positions (condition_id TEXT, state TEXT)")
            conn.execute("CREATE TABLE paper_trades (condition_id TEXT, status TEXT)")
            conn.execute("INSERT INTO market_metadata (condition_id) VALUES (?)", (good_meta,))
            conn.execute("INSERT INTO strategy_positions (condition_id, state) VALUES (?, 'open')", (good_strategy,))
            conn.execute("INSERT INTO strategy_positions (condition_id, state) VALUES (?, 'closed')", ("0x" + ("4" * 64),))
            conn.execute("INSERT INTO paper_trades (condition_id, status) VALUES (?, 'open')", (good_paper,))
            conn.commit()
            conn.close()

            known = finance_analyst._load_known_boost_target_ids(tmp.name)

        self.assertEqual(known, {good_meta, good_strategy, good_paper})


class TestUpdateWorldContextTradeIdeasOrdering(unittest.TestCase):
    """Codex 2026-05-27 audit regression: trade_ideas was referenced inside
    the run_summary block before being assigned. Finance branch raised
    UnboundLocalError silently (swallowed by the surrounding try/except)."""

    def test_finance_run_summary_does_not_unbound_trade_ideas(self):
        from sniper.analysis import world_context

        upserts: list[dict] = []

        def _fake_upsert(**kwargs):
            upserts.append(kwargs)

        fake_module = types.SimpleNamespace(upsert_memory=_fake_upsert)

        # Stub the heavy claude call → return a result with run_summary AND
        # trade_ideas. Pre-fix, the finance branch would silently fail to
        # log/persist trade_ideas_generated count because trade_ideas wasn't
        # yet bound when the f-string evaluated.
        fake_result = {
            "world_events": [],
            "trade_ideas": [{"id": "t1"}, {"id": "t2"}],
            "run_summary": "Markets quiet today.",
        }

        with patch.dict(sys.modules, {"bot.supabase_helper": fake_module}), \
             patch.object(world_context, "_claude_available", return_value=True), \
             patch.object(world_context, "get_world_events", return_value=[]), \
             patch.object(world_context, "analyze_news_with_claude", return_value=fake_result), \
             patch.object(world_context, "_save_world_event", lambda *_a, **_kw: None):
            out = world_context.update_world_context(
                news_items=[{"title": "Fed signals dovish pivot on rate trajectory today"}],
                markets=[{"yes_price": 0.4}],
                domain="finance",
            )

        self.assertEqual(len(out), 2)
        # The fake upsert MUST have been called with trade_ideas_generated=2
        self.assertEqual(len(upserts), 1)
        content = json.loads(upserts[0]["content"])
        self.assertEqual(content["trade_ideas_generated"], 2)


class TestUiShellHygiene(unittest.TestCase):
    def test_main_shell_avoids_inline_dom_handlers(self):
        root = Path(__file__).resolve().parents[1]
        targets = [
            root / "static" / "app.js",
            root / "templates" / "index.html",
        ]
        forbidden = ("onclick=", "onerror=", "onchange=")

        for path in targets:
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                self.assertNotIn(marker, text, f"{path.name} should not contain {marker}")

    def test_polymarket_link_helper_escapes_link_text(self):
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("const safeText = esc(text);", app_js)
        self.assertNotIn("polymarketLink(esc(", app_js)

    def test_paper_trader_synthetic_card_does_not_fetch_numeric_detail_api(self):
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("coercedId === 'paper_trader'", app_js)
        self.assertIn("state.strategies.find(s => String(s.id) === 'paper_trader')", app_js)

    def test_no_forward_copy_tab(self):
        # Removed cohort-following tab stays gone: no nav item, panel, dispatch, or enroll copy.
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("convictionForward", app_js)
        removed_label = "Paper" + " Watch"
        removed_slug = "paper" + "-watch"
        self.assertNotIn(removed_label, app_js)
        self.assertNotIn(removed_slug, app_js.lower())
        self.assertNotIn("favorite to forward-test", app_js.lower())
        removed_prefix = "favorite to "
        removed_suffix = "en" + "roll"
        self.assertNotIn(removed_prefix + removed_suffix, app_js.lower())
        self.assertNotIn("position(s) enrolled", app_js)
        index_html = (root / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn(removed_label, index_html)
        self.assertNotIn("tab-forward", index_html)
        self.assertNotIn('data-tab="forward"', index_html)

    def test_analyzer_has_live_positions_tab(self):
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-dview="live"', app_js)
        self.assertIn("Closed Positions", app_js)
        self.assertIn("/api/wallet-analyzer/live-positions?wallet=", app_js)
        self.assertIn("/api/wallet-analyzer/analysis-state?wallet=", app_js)
        self.assertIn("known_to_analyzer", app_js)
        self.assertIn("new since analysis", app_js)
        self.assertIn("Closed Positions is the last analyzed ledger", app_js)

    def test_wallet_intelligence_narrative_surfaces(self):
        """The Analyzer renders the synthesized plain-English verdict, uses no copy-advice language,
        and the removed cohort-following surface is fully gone."""
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
        # Analyzer: synthesized verdict narrative still renders
        self.assertIn("_verdictHtml", app_js)
        self.assertIn("verdict_line", app_js)
        # removed surface route/link stays gone
        self.assertNotIn("/api/" + "conviction" + "-forward", app_js)
        self.assertNotIn("Proving " + "live", app_js)
        # doctrine: no copy-advice framing
        low = app_js.lower()
        self.assertNotIn("copy this wallet", low)
        self.assertNotIn("expected profit", low)

    def test_live_signals_board(self):
        """The Signals tab hosts the live graded-wallet board (archetype-aware confidence), labelled
        in-sample hypothesis, no insider_score, no copy-advice."""
        root = Path(__file__).resolve().parents[1]
        app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/live-wallet-signals", app_js)
        self.assertIn("const liveSignals", app_js)
        self.assertIn("live-signals-board", app_js)
        # the live board itself must not rank on insider_score nor frame as advice
        board = app_js[app_js.index("const liveSignals"):app_js.index("const signals = {")]
        self.assertNotIn("insider_score", board.lower())
        signals_delegate = app_js[app_js.index("const signals = {"):app_js.index("// ============================================================================", app_js.index("const signals = {"))]
        self.assertIn("return liveSignals.load()", signals_delegate)
        self.assertNotIn("signals-tbody", signals_delegate)
        self.assertNotIn("renderConvergence", signals_delegate)
        self.assertNotIn("renderThesis", signals_delegate)
        self.assertNotIn("insider_score", signals_delegate.lower())
        self.assertNotIn("copy this wallet", app_js.lower())
        idx = (root / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="live-signals-board"', idx)

    def test_analyzer_prose_is_wallet_neutral(self):
        """Analyzer findings must read for ANY wallet, not the SwissMiss-specific she/her prose
        it was templated from. (Codex review LOW 2026-06-17)"""
        import re
        app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        gendered = re.findall(r"\b(she|her|hers|herself|he|him|his)\b", app_js, flags=re.IGNORECASE)
        self.assertEqual(gendered, [], f"gendered pronouns in analyzer prose: {gendered[:5]}")


if __name__ == "__main__":
    unittest.main()
