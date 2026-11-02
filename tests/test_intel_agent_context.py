"""Shared agent context + advisory close recommendations.

The load-bearing property is that a recommendation is RECORDED, never ACTED ON,
and that a model-supplied position_id is validated before it is trusted.
"""
from __future__ import annotations

import sqlite3

import pytest

from sniper.analysis.world_context import _record_position_reviews
from sniper.persistence import intel_paper
from sniper.services.intel_positions_context import format_positions_block


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    intel_paper.ensure_schema(c)
    return c


def _open(c, idea_id=1, **kw):
    fields = dict(
        idea_id=idea_id, venue="pm", instrument="0xcid", token_id="tok",
        event_title="US x Iran Ceasefire", outcome_label="by July 31",
        direction="buy_no", entry_price=0.86, entry_ts="2026-07-27T00:00:00+00:00",
        size_usdc=10.0, reviewer_verdict="pending", reviewer_score=None,
        holder_snapshot=None, neg_risk=0,
    )
    fields.update(kw)
    return intel_paper.open_position(c, **fields)


# --- context block -------------------------------------------------------

def test_empty_book_renders_nothing(conn):
    """No positions must cost no prompt tokens — not an empty scaffold."""
    assert format_positions_block(conn) == ""


def test_block_shows_entry_mark_and_pnl(conn):
    pid = _open(conn)
    intel_paper.record_mark(conn, pid, "2026-07-29T00:00:00+00:00", 0.91, 5.81)
    block = format_positions_block(conn)
    assert f"#{pid}" in block
    assert "US x Iran Ceasefire" in block
    assert "0.86 -> 0.91" in block
    assert "+5.8%" in block


def test_unmarked_position_says_so_rather_than_faking_a_flat_move(conn):
    _open(conn)
    block = format_positions_block(conn)
    assert "no mark yet" in block
    assert "0.0%" not in block


def test_sibling_positions_are_flagged_as_correlated(conn):
    _open(conn, idea_id=1, outcome_label="by July 31")
    _open(conn, idea_id=2, instrument="0xcid2", outcome_label="by Aug 31")
    block = format_positions_block(conn)
    assert "2 open on this event" in block
    assert "correlated" in block


def test_block_never_raises_on_a_broken_connection():
    """Losing context must degrade the run, not fail it."""
    broken = sqlite3.connect(":memory:")
    broken.close()
    assert format_positions_block(broken) == ""


# --- recommendations -----------------------------------------------------

def test_close_recommendation_is_recorded_and_surfaced(conn):
    pid = _open(conn)
    n = _record_position_reviews(conn, [
        {"position_id": pid, "action": "close", "urgency": "high",
         "reason": "Ceasefire signed; thesis is dead."},
    ])
    assert n == 1
    row = intel_paper.open_positions(conn)[0]
    assert row["last_recommendation"] == "close"
    assert row["last_recommendation_urgency"] == "high"
    assert "Ceasefire signed" in row["last_recommendation_reason"]


def test_recommendation_does_not_close_the_position(conn):
    """The whole point: advisory only. An auto-close would make the exit
    endogenous to the signal this book exists to measure."""
    pid = _open(conn)
    _record_position_reviews(conn, [
        {"position_id": pid, "action": "close", "reason": "x"},
    ])
    still_open = [p["position_id"] for p in intel_paper.open_positions(conn)]
    assert pid in still_open


def test_hallucinated_position_id_is_dropped(conn):
    _open(conn)
    assert _record_position_reviews(conn, [
        {"position_id": 99999, "action": "close", "reason": "invented"},
    ]) == 0


def test_bad_action_is_dropped(conn):
    pid = _open(conn)
    assert _record_position_reviews(conn, [
        {"position_id": pid, "action": "liquidate_everything", "reason": "no"},
    ]) == 0


def test_latest_recommendation_wins(conn):
    pid = _open(conn)
    _record_position_reviews(conn, [{"position_id": pid, "action": "close", "reason": "early"}])
    intel_paper.record_recommendation(conn, pid, "2099-01-01T00:00:00+00:00",
                                      "hold", reason="later")
    row = intel_paper.open_positions(conn)[0]
    assert row["last_recommendation"] == "hold"


def test_empty_reviews_is_a_noop(conn):
    _open(conn)
    assert _record_position_reviews(conn, []) == 0
    assert intel_paper.open_positions(conn)[0]["last_recommendation"] is None


# --- self-reference ------------------------------------------------------

def test_a_position_is_hidden_from_its_own_ideas_review(conn):
    """Reviews run after the position exists. Showing the reviewer the result of the
    idea under review makes it reject the idea as a duplicate of itself."""
    _open(conn, idea_id=1, outcome_label="by July 31")
    _open(conn, idea_id=2, instrument="0xcid2", outcome_label="by Aug 31")

    own = format_positions_block(conn, exclude_idea_id=1)
    assert "by July 31" not in own, "idea 1 must not see its own position"
    assert "by Aug 31" in own, "but must still see the rest of the book"


def test_excluding_the_only_position_yields_an_empty_block(conn):
    _open(conn, idea_id=1)
    assert format_positions_block(conn, exclude_idea_id=1) == ""


def test_no_exclusion_shows_the_whole_book(conn):
    _open(conn, idea_id=1, outcome_label="by July 31")
    assert "by July 31" in format_positions_block(conn)


def test_reviewer_excludes_the_reviewed_ideas_position(conn):
    """End-to-end through the reviewer's own helper."""
    from agents.adversarial_reviewer_agent import _fetch_open_positions_block
    _open(conn, idea_id=1, outcome_label="by July 31")
    _open(conn, idea_id=2, instrument="0xcid2", outcome_label="by Aug 31")
    block = _fetch_open_positions_block(conn, 1)
    assert "by July 31" not in block and "by Aug 31" in block
