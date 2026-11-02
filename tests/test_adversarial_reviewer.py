from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from brain import Brain, ensure_trade_ideas_schema
from db import get_connection, init_db
from sniper.strategies.base import (
    StrategyEntryDecision,
    StrategyExitDecision,
    StrategyManageState,
    StrategyPlugin,
    StrategyReport,
    StrategySignal,
)
from sniper.services.strategy_runner import process_plugin_strategy


class _FakePlugin(StrategyPlugin):
    plugin_id = "news_intel_v1"

    def discover(self, conn, strategy):
        return [
            StrategySignal(
                wallet="news_deadbeef",
                condition_id="0x" + "1" * 64,
                outcome="YES",
                title="Adversarial gate test",
                current_price=0.45,
                position_value=0.0,
                analyzed_ts=int(time.time()),
                metadata={"idea_id": 1},
            )
        ]

    def enter(self, conn, strategy, signal, open_positions):
        return StrategyEntryDecision(
            should_enter=True,
            reason="ok",
            size_usdc=10.0,
            entry_price=0.45,
        )

    def manage(self, conn, strategy, position):
        return StrategyManageState(current_price=0.45, signal_open=True)

    def exit(self, conn, strategy, position, state):
        return StrategyExitDecision(should_exit=False, reason="hold")

    def report(self, conn, strategy, positions):
        return StrategyReport()


def test_brain_records_adversarial_review_fields():
    brain = Brain(db_path=":memory:")
    idea_id = brain.add_trade_idea(
        condition_id="0x" + "2" * 64,
        market_title="Will test happen?",
        outcome="YES",
        direction="buy_yes",
        reasoning="test reasoning",
        current_price=0.42,
        confidence="high",
    )
    assert idea_id is not None

    row = brain.get_all_trade_ideas(limit=1)[0]
    assert row["adversarial_score"] is None
    assert row["adversarial_reasoning"] is None
    assert row["adversarial_approved"] is None

    brain.record_adversarial_review(
        int(idea_id),
        adversarial_score=81,
        adversarial_reasoning="Too crowded and already priced in",
        adversarial_approved=False,
    )

    updated = brain.get_all_trade_ideas(limit=1)[0]
    assert updated["adversarial_score"] == 81
    assert updated["adversarial_reasoning"] == "Too crowded and already priced in"
    assert updated["adversarial_approved"] == 0


def _setup_runner_db() -> tuple[sqlite3.Connection, dict]:
    conn = get_connection(":memory:")
    init_db(conn)
    ensure_trade_ideas_schema(conn)
    conn.execute(
        """INSERT INTO strategies (id, wallet, label, strategy_type, parameters, status)
           VALUES (1, 'system', 'News Intel', 'news_intel_v1', '{}', 'active')"""
    )
    strategy = dict(conn.execute("SELECT * FROM strategies WHERE id = 1").fetchone())
    return conn, strategy


def test_strategy_runner_skips_pending_adversarial_review():
    conn, strategy = _setup_runner_db()
    conn.execute(
        """INSERT INTO trade_ideas (
               id, condition_id, market_title, outcome, direction, current_price,
               confidence, reasoning, status, adversarial_approved
           ) VALUES (
               1, ?, 'Gate Test', 'YES', 'buy_yes', 0.45,
               'high', 'test', 'open', NULL
           )""",
        ("0x" + "1" * 64,),
    )
    conn.commit()

    with patch("sniper.services.strategy_runner.get_strategy_plugin", return_value=_FakePlugin()), patch(
        "sniper.services.strategy_runner._enrich_signal_with_live_price", side_effect=lambda signal: signal
    ):
        process_plugin_strategy(conn, strategy)

    count = conn.execute("SELECT COUNT(*) FROM strategy_positions").fetchone()[0]
    assert count == 0
    conn.close()


def _make_fake_subprocess(review_json: str):
    """Return a subprocess.run side_effect that writes review_json to the -o output file."""

    def _fake_run(cmd, **kwargs):
        if "-o" in cmd:
            out_path = cmd[cmd.index("-o") + 1]
            Path(out_path).write_text(review_json)
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    return _fake_run


def test_call_codex_review_parses_output_file():
    """_call_codex_review reads GPT response from the codex -o output file."""
    from agents.adversarial_reviewer_agent import _call_codex_review

    review_payload = json.dumps({
        "adversarial_score": 25,
        "reasoning": "Genuine information asymmetry — crowd underpricing tail risk",
        "approved": True,
    })

    idea = {
        "id": 99,
        "condition_id": "0x" + "a" * 64,
        "market_title": "Codex output file test",
        "outcome": "YES",
        "direction": "buy_yes",
        "confidence": "medium",
        "current_price": 0.38,
        "news_source": "test",
        "reasoning": "test",
        "source_agent": "test",
    }

    with patch("agents.adversarial_reviewer_agent.subprocess.run",
               side_effect=_make_fake_subprocess(review_payload)):
        result = _call_codex_review(idea)

    assert result["adversarial_approved"] is True
    assert result["adversarial_score"] == 25
    assert "asymmetry" in result["adversarial_reasoning"]


def test_call_codex_review_raises_on_empty_output():
    """Confirm RuntimeError is raised when codex returns empty output file and empty stdout."""
    from agents.adversarial_reviewer_agent import _call_codex_review

    def _fake_empty_run(cmd, **kwargs):
        if "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_text("")
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    idea = {
        "id": 1, "condition_id": "0x" + "b" * 64, "market_title": "x",
        "outcome": "YES", "direction": "buy_yes", "confidence": "low",
        "current_price": 0.3, "news_source": "", "reasoning": "", "source_agent": "",
    }

    import pytest
    with patch("agents.adversarial_reviewer_agent.subprocess.run", side_effect=_fake_empty_run):
        with pytest.raises(RuntimeError, match="empty output"):
            _call_codex_review(idea)


# ── Context injection + brain writes ─────────────────────────────────────────


def test_fetch_context_for_source_world_analyst():
    """_fetch_context_for_source returns world_state snippet for world_analyst ideas."""
    from agents.adversarial_reviewer_agent import _fetch_context_for_source

    fake_content = "World state: Iran tensions elevated. Market impact: YES markets elevated."
    with patch("agents.adversarial_reviewer_agent._try_get_memory", return_value=fake_content):
        result = _fetch_context_for_source("world_analyst")

    assert result is not None
    assert "Iran" in result


def test_fetch_context_for_source_unknown_agent():
    """_fetch_context_for_source returns None for unknown/empty source agents."""
    from agents.adversarial_reviewer_agent import _fetch_context_for_source

    assert _fetch_context_for_source("insider_copy") is None
    assert _fetch_context_for_source("") is None
    assert _fetch_context_for_source(None) is None


def test_build_review_prompt_includes_context_block():
    """_build_review_prompt prepends the context block when provided."""
    from agents.adversarial_reviewer_agent import _build_review_prompt

    idea = {
        "id": 7, "condition_id": "0xabc", "market_title": "Test market",
        "outcome": "YES", "direction": "buy_yes", "confidence": "medium",
        "current_price": 0.5, "news_source": "bbc", "reasoning": "test",
        "source_agent": "world_analyst",
    }
    context = "Iran launched missiles. Markets are reactive."
    prompt = _build_review_prompt(idea, context_block=context)

    assert "Context from the source agent" in prompt
    assert "Iran launched missiles" in prompt
    assert "Test market" in prompt
    # Context block must appear BEFORE the trade idea fields
    assert prompt.index("Iran launched missiles") < prompt.index("Test market")


def test_build_review_prompt_no_context():
    """_build_review_prompt works fine without a context block."""
    from agents.adversarial_reviewer_agent import _build_review_prompt

    idea = {
        "id": 8, "condition_id": "0xdef", "market_title": "No context market",
        "outcome": "NO", "direction": "buy_no", "confidence": "low",
        "current_price": 0.3, "news_source": "", "reasoning": "test", "source_agent": "",
    }
    prompt = _build_review_prompt(idea)
    assert "Context from the source agent" not in prompt
    assert "No context market" in prompt


def test_review_pending_writes_brain_stats_after_run(tmp_path):
    """review_pending_trade_ideas writes adversarial/run_stats and rejection_patterns."""
    from agents.adversarial_reviewer_agent import review_pending_trade_ideas

    # Use a real temp-file DB so both the setup Brain and review_pending_trade_ideas
    # share the same SQLite file (separate :memory: DBs are always isolated).
    db_file = str(tmp_path / "test_stats.db")

    brain = Brain(db_path=db_file)
    brain.add_trade_idea(
        condition_id="0x" + "d" * 64,
        market_title="Stats write test market",
        outcome="YES",
        direction="buy_yes",
        reasoning="test reasoning",
        current_price=0.4,
        confidence="high",
    )

    review_payload = json.dumps({
        "adversarial_score": 80,
        "reasoning": "Obvious headline, already priced in.",
        "approved": False,
    })

    upserted: list[dict] = []

    with patch("agents.adversarial_reviewer_agent.subprocess.run",
               side_effect=_make_fake_subprocess(review_payload)), \
         patch("agents.adversarial_reviewer_agent._try_get_memory", return_value=None), \
         patch("agents.adversarial_reviewer_agent._try_upsert_memory",
               side_effect=lambda name, desc, content: upserted.append(
                   {"name": name, "content": content}
               )):
        result = review_pending_trade_ideas(db_path=db_file)

    assert result["reviewed"] == 1, f"expected 1 reviewed, got {result}"
    assert result["rejected"] == 1

    stats_names = [u["name"] for u in upserted]
    assert "adversarial/run_stats" in stats_names, f"run_stats not written; got: {stats_names}"
    assert "adversarial/rejection_patterns" in stats_names, (
        f"rejection_patterns not written; got: {stats_names}"
    )

    stats_entry = next(u for u in upserted if u["name"] == "adversarial/run_stats")
    stats = json.loads(stats_entry["content"])
    assert stats["total_reviewed"] == 1
    assert stats["total_rejected"] == 1
    assert stats["total_approved"] == 0
    assert stats["last_run_reviewed"] == 1
    assert stats["avg_score_last_run"] == 80.0

    patterns_entry = next(u for u in upserted if u["name"] == "adversarial/rejection_patterns")
    patterns = json.loads(patterns_entry["content"])
    assert len(patterns) == 1
    assert patterns[0]["score"] == 80
    assert "priced in" in patterns[0]["reason"]


def test_update_brain_stats_merges_cumulative():
    """_update_brain_stats accumulates totals across runs."""
    from agents.adversarial_reviewer_agent import _update_brain_stats

    existing = json.dumps({
        "total_reviewed": 10,
        "total_approved": 2,
        "total_rejected": 8,
    })

    upserted: list[str] = []

    with patch("agents.adversarial_reviewer_agent._try_get_memory", return_value=existing), \
         patch("agents.adversarial_reviewer_agent._try_upsert_memory",
               side_effect=lambda name, desc, content: upserted.append(content)):
        _update_brain_stats(
            run_reviewed=3,
            run_approved=1,
            run_scores=[25, 70, 85],
        )

    assert upserted, "expected upsert to be called"
    stats = json.loads(upserted[0])
    assert stats["total_reviewed"] == 13
    assert stats["total_approved"] == 3
    assert stats["total_rejected"] == 10
    assert stats["approval_rate_pct"] == round(100 * 3 / 13, 1)
    assert stats["avg_score_last_run"] == round((25 + 70 + 85) / 3, 1)


def test_update_rejection_patterns_keeps_last_20():
    """_update_rejection_patterns trims the list to 20 entries."""
    from agents.adversarial_reviewer_agent import _update_rejection_patterns

    existing_patterns = [{"idea_id": i, "score": 80} for i in range(18)]
    existing_raw = json.dumps(existing_patterns)

    upserted: list[str] = []
    new_rejections = [
        {"idea_id": 100, "score": 90, "reason": "priced in"},
        {"idea_id": 101, "score": 88, "reason": "crowded"},
        {"idea_id": 102, "score": 85, "reason": "weak causal chain"},
    ]

    with patch("agents.adversarial_reviewer_agent._try_get_memory", return_value=existing_raw), \
         patch("agents.adversarial_reviewer_agent._try_upsert_memory",
               side_effect=lambda name, desc, content: upserted.append(content)):
        _update_rejection_patterns(new_rejections)

    assert upserted
    patterns = json.loads(upserted[0])
    assert len(patterns) == 20  # 18 + 3 = 21, trimmed to 20 (oldest dropped)
    assert patterns[-1]["idea_id"] == 102
    assert patterns[-2]["idea_id"] == 101


def test_strategy_runner_allows_adversarially_approved_idea():
    conn, strategy = _setup_runner_db()
    conn.execute(
        """INSERT INTO trade_ideas (
               id, condition_id, market_title, outcome, direction, current_price,
               confidence, reasoning, status, adversarial_score, adversarial_reasoning,
               adversarial_approved
           ) VALUES (
               1, ?, 'Gate Test', 'YES', 'buy_yes', 0.45,
               'high', 'test', 'open', 12, 'Survived adversarial review', 1
           )""",
        ("0x" + "1" * 64,),
    )
    conn.commit()

    with patch("sniper.services.strategy_runner.get_strategy_plugin", return_value=_FakePlugin()), patch(
        "sniper.services.strategy_runner._enrich_signal_with_live_price", side_effect=lambda signal: signal
    ):
        process_plugin_strategy(conn, strategy)

    count = conn.execute("SELECT COUNT(*) FROM strategy_positions").fetchone()[0]
    assert count == 1
    conn.close()
