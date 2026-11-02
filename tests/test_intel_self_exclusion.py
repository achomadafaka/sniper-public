"""One open position per instrument, enforced by the DB.

A second position on a market we already hold is never a new observation: same
direction is silent double-sizing, opposite direction is a self-excluding pair that
pays the spread twice to learn nothing. The prompt asks the models not to propose
these; this is the check that holds when they do it anyway.
"""
from __future__ import annotations

import sqlite3

import pytest

from sniper.persistence import intel_paper
from sniper.services import intel_paper_open


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    intel_paper.ensure_schema(c)
    return c


def _fields(idea_id, instrument, direction, **kw):
    f = dict(
        idea_id=idea_id, venue="pm", instrument=instrument, token_id="tok",
        event_title="Ceasefire?", outcome_label="July 31", direction=direction,
        entry_price=0.5, entry_ts="2026-07-29T00:00:00+00:00", size_usdc=10.0,
        reviewer_verdict="pending", reviewer_score=None, holder_snapshot=None,
        neg_risk=0,
    )
    f.update(kw)
    return f


def test_opposite_direction_on_same_instrument_is_refused(conn):
    assert intel_paper.open_position(conn, **_fields(1, "0xcid", "buy_yes")) is not None
    assert intel_paper.open_position(conn, **_fields(2, "0xcid", "buy_no")) is None


def test_same_direction_on_same_instrument_is_refused(conn):
    assert intel_paper.open_position(conn, **_fields(1, "0xcid", "buy_no")) is not None
    assert intel_paper.open_position(conn, **_fields(2, "0xcid", "buy_no")) is None


def test_different_instruments_both_open(conn):
    assert intel_paper.open_position(conn, **_fields(1, "0xaaa", "buy_no")) is not None
    assert intel_paper.open_position(conn, **_fields(2, "0xbbb", "buy_no")) is not None
    assert len(intel_paper.open_positions(conn)) == 2


def test_instrument_frees_up_after_close(conn):
    """The constraint is on OPEN positions — a closed one must not block re-entry."""
    pid = intel_paper.open_position(conn, **_fields(1, "0xcid", "buy_no"))
    intel_paper.close_position(conn, pid, 0.6, "2026-07-30T00:00:00+00:00", "manual")
    assert intel_paper.open_position(conn, **_fields(2, "0xcid", "buy_yes")) is not None


def test_router_reports_conflict_with_the_blocking_position(conn, monkeypatch):
    monkeypatch.setattr(intel_paper_open, "fetch_market", lambda cid: {
        "token_ids": {"YES": "tY", "NO": "tN"},
        "prices": {"YES": 0.4, "NO": 0.6},
        "event_title": "Ceasefire?", "outcome_label": "July 31", "neg_risk": False,
    })
    first = intel_paper_open.open_for_idea(
        conn, {"id": 1, "condition_id": "0xcid", "direction": "buy_no"})
    assert first["disposition"] == "pm"

    second = intel_paper_open.open_for_idea(
        conn, {"id": 2, "condition_id": "0xcid", "direction": "buy_yes"})
    assert second["disposition"] == "conflict"
    assert second["position_id"] is None
    assert "already holding buy_no" in second["reason"]
    assert f"#{first['position_id']}" in second["reason"]
    # The original position is untouched — a refused entry must not disturb the book.
    assert [p["position_id"] for p in intel_paper.open_positions(conn)] == [first["position_id"]]


def test_hl_conflict_is_also_refused(conn, monkeypatch):
    monkeypatch.setattr(intel_paper_open, "resolve_symbol", lambda s, u: "flx:OIL")
    monkeypatch.setattr(intel_paper_open, "all_mids", lambda syms: {"flx:OIL": 70.0})
    ideas = [{"id": 10, "hl_symbol": "OIL", "direction": "long"},
             {"id": 11, "hl_symbol": "OIL", "direction": "short"}]
    first = intel_paper_open.open_for_idea(conn, ideas[0], hl_universe_set={"flx:OIL"})
    second = intel_paper_open.open_for_idea(conn, ideas[1], hl_universe_set={"flx:OIL"})
    assert first["disposition"] == "hl"
    assert second["disposition"] == "conflict"
    assert "already holding long" in second["reason"]


def test_rerunning_the_same_idea_is_idempotent_not_a_holding_conflict(conn, monkeypatch):
    """Re-running one idea must not be reported as if we held a rival position."""
    monkeypatch.setattr(intel_paper_open, "fetch_market", lambda cid: {
        "token_ids": {"YES": "tY", "NO": "tN"},
        "prices": {"YES": 0.4, "NO": 0.6},
        "event_title": "E", "outcome_label": "O", "neg_risk": False,
    })
    idea = {"id": 7, "condition_id": "0xcid", "direction": "buy_no"}
    intel_paper_open.open_for_idea(conn, idea)
    again = intel_paper_open.open_for_idea(conn, idea)
    assert again["disposition"] == "conflict"
    assert "already holding" in again["reason"]


def test_prompt_forbids_self_excluding_ideas():
    """The models are told the rule, not just blocked by the DB."""
    from sniper.analysis import world_context as wc
    assert "both win" in wc._PROMPT_TEMPLATE
    assert "both win" in wc._POSITIONS_SECTION

    from agents.adversarial_reviewer_agent import _build_review_prompt
    p = _build_review_prompt({"id": 1}, positions_block="#1 | PM | X | buy_no @ 0.5")
    assert "REJECT OUTRIGHT" in p


# --- reviewer verdict propagation ----------------------------------------

def test_verdict_lands_on_the_position_after_review(conn):
    """Positions open before the reviewer runs, so the verdict must be back-filled."""
    intel_paper.open_position(conn, **_fields(5, "0xcid", "buy_no"))
    assert intel_paper.open_positions(conn)[0]["reviewer_verdict"] == "pending"

    assert intel_paper.apply_reviewer_verdict(conn, 5, "rejected", 88) is True
    row = intel_paper.open_positions(conn)[0]
    assert row["reviewer_verdict"] == "rejected"
    assert row["reviewer_score"] == 88


def test_first_real_verdict_is_final(conn):
    """A later re-review must not reclassify a cohort mid-experiment."""
    intel_paper.open_position(conn, **_fields(6, "0xcid", "buy_no"))
    intel_paper.apply_reviewer_verdict(conn, 6, "rejected", 88)
    assert intel_paper.apply_reviewer_verdict(conn, 6, "approved", 12) is False
    assert intel_paper.open_positions(conn)[0]["reviewer_verdict"] == "rejected"


def test_verdict_for_an_unknown_idea_is_a_noop(conn):
    assert intel_paper.apply_reviewer_verdict(conn, 4242, "approved", 10) is False
