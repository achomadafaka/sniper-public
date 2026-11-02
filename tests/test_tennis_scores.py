"""Tests for feeds/tennis_scores.py — ESPN score feed parser.

All tests use synthetic data; no live HTTP calls are made.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers — synthetic ESPN data builders
# ---------------------------------------------------------------------------

def _make_competition(
    player1: str,
    player2: str,
    linescores1=None,
    linescores2=None,
    status: str = "In Progress",
) -> dict:
    return {
        "competitors": [
            {
                "athlete": {"displayName": player1},
                "linescores": [{"value": s} for s in (linescores1 or [])],
            },
            {
                "athlete": {"displayName": player2},
                "linescores": [{"value": s} for s in (linescores2 or [])],
            },
        ],
        "status": {"type": {"description": status}},
    }


def _make_scoreboard(competitions: list) -> dict:
    return {"events": [{"groupings": [{"competitions": competitions}]}]}


# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

from sniper.feeds.tennis_scores import (
    EspnOutageTracker,
    MatchState,
    find_match,
    get_sets_completed,
    parse_scoreboard,
)


# ---------------------------------------------------------------------------
# parse_scoreboard
# ---------------------------------------------------------------------------

class TestParseScoreboard:
    def test_parse_scoreboard_extracts_competitions(self):
        comp1 = _make_competition("Novak Djokovic", "Rafael Nadal", [6, 4], [4, 6])
        comp2 = _make_competition("Carlos Alcaraz", "Jannik Sinner", [6], [3])
        data = _make_scoreboard([comp1, comp2])

        parsed = parse_scoreboard(data)

        assert len(parsed) == 2
        names = {(r["player1"], r["player2"]) for r in parsed}
        assert ("Novak Djokovic", "Rafael Nadal") in names
        assert ("Carlos Alcaraz", "Jannik Sinner") in names

    def test_parse_scoreboard_empty_events(self):
        parsed = parse_scoreboard({"events": []})
        assert parsed == []

    def test_parse_scoreboard_missing_events_key(self):
        parsed = parse_scoreboard({})
        assert parsed == []

    def test_parse_scoreboard_includes_competition_dict(self):
        comp = _make_competition("Roger Federer", "Andy Murray")
        data = _make_scoreboard([comp])
        parsed = parse_scoreboard(data)
        assert len(parsed) == 1
        assert "competition" in parsed[0]
        # competition should be the raw dict we can pass to get_sets_completed
        assert "competitors" in parsed[0]["competition"]


# ---------------------------------------------------------------------------
# find_match
# ---------------------------------------------------------------------------

class TestFindMatch:
    def _parsed(self):
        comps = [
            _make_competition("Novak Djokovic", "Rafael Nadal"),
            _make_competition("Carlos Alcaraz", "Jannik Sinner"),
        ]
        return parse_scoreboard(_make_scoreboard(comps))

    def test_find_match_by_surname_forward_order(self):
        result = find_match(self._parsed(), "Djokovic", "Nadal")
        assert result is not None
        assert result["player1"] == "Novak Djokovic"
        assert result["player2"] == "Rafael Nadal"

    def test_find_match_by_surname_reverse_order(self):
        # Order-independent — both "Nadal, Djokovic" and "Djokovic, Nadal" should match
        result = find_match(self._parsed(), "Nadal", "Djokovic")
        assert result is not None
        assert result["player1"] == "Novak Djokovic"

    def test_find_match_returns_none_for_missing(self):
        result = find_match(self._parsed(), "Murray", "Federer")
        assert result is None

    def test_find_match_case_insensitive(self):
        result = find_match(self._parsed(), "DJOKOVIC", "nadal")
        assert result is not None

    def test_find_match_partial_surname_not_matched(self):
        # "Djok" is not a full surname — should not match
        result = find_match(self._parsed(), "Djok", "Nadal")
        assert result is None


# ---------------------------------------------------------------------------
# get_sets_completed
# ---------------------------------------------------------------------------

class TestGetSetsCompleted:
    def test_get_sets_completed_zero(self):
        comp = _make_competition("Player A", "Player B")
        state = get_sets_completed(comp)
        assert state.sets_completed == 0
        assert state.score_by_set == []

    def test_get_sets_completed_one(self):
        comp = _make_competition("Player A", "Player B", [6], [4])
        state = get_sets_completed(comp)
        assert state.sets_completed == 1
        assert state.score_by_set == [(6, 4)]

    def test_get_sets_completed_two(self):
        comp = _make_competition("Player A", "Player B", [6, 7], [4, 5])
        state = get_sets_completed(comp)
        assert state.sets_completed == 2
        assert state.score_by_set == [(6, 4), (7, 5)]

    def test_get_sets_completed_three(self):
        comp = _make_competition("Player A", "Player B", [6, 3, 6], [4, 6, 2])
        state = get_sets_completed(comp)
        assert state.sets_completed == 3
        assert state.score_by_set == [(6, 4), (3, 6), (6, 2)]

    def test_get_sets_completed_asymmetric_uses_min(self):
        # If one player has more linescores than the other, use min
        comp = _make_competition("Player A", "Player B", [6, 4, 6], [4])
        state = get_sets_completed(comp)
        assert state.sets_completed == 1

    def test_get_sets_completed_status_final(self):
        comp = _make_competition("Player A", "Player B", [6, 6], [3, 4], status="Final")
        state = get_sets_completed(comp)
        assert state.match_status == "final"

    def test_get_sets_completed_status_live(self):
        comp = _make_competition("Player A", "Player B", [6], [4], status="In Progress")
        state = get_sets_completed(comp)
        assert state.match_status == "live"

    def test_get_sets_completed_status_scheduled(self):
        comp = _make_competition("Player A", "Player B", status="Scheduled")
        state = get_sets_completed(comp)
        assert state.match_status == "scheduled"

    def test_get_sets_completed_status_postponed(self):
        comp = _make_competition("Player A", "Player B", status="Postponed")
        state = get_sets_completed(comp)
        assert state.match_status == "postponed"

    def test_get_sets_completed_status_suspended(self):
        comp = _make_competition("Player A", "Player B", status="Suspended")
        state = get_sets_completed(comp)
        assert state.match_status == "suspended"

    def test_get_sets_completed_unknown_status_maps_to_live(self):
        # Unknown ESPN status strings should fall through to "live" as default
        comp = _make_competition("Player A", "Player B", status="Some Unknown Status")
        state = get_sets_completed(comp)
        assert state.match_status == "live"

    def test_get_sets_completed_source_is_espn(self):
        comp = _make_competition("Player A", "Player B")
        state = get_sets_completed(comp)
        assert state.source == "espn"

    def test_get_sets_completed_returns_matchstate(self):
        comp = _make_competition("Player A", "Player B")
        state = get_sets_completed(comp)
        assert isinstance(state, MatchState)


# ---------------------------------------------------------------------------
# MatchState dataclass
# ---------------------------------------------------------------------------

class TestMatchState:
    def test_matchstate_fields(self):
        ms = MatchState(
            sets_completed=2,
            score_by_set=[(6, 4), (7, 5)],
            match_status="final",
            source="espn",
        )
        assert ms.sets_completed == 2
        assert ms.score_by_set == [(6, 4), (7, 5)]
        assert ms.match_status == "final"
        assert ms.source == "espn"


# ---------------------------------------------------------------------------
# EspnOutageTracker
# ---------------------------------------------------------------------------

class TestEspnOutageTracker:
    def test_initial_state_no_pause(self):
        tracker = EspnOutageTracker(pause_after=3, cancel_after_min=10)
        assert not tracker.should_pause_new_orders
        assert not tracker.should_cancel_all

    def test_one_failure_no_pause(self):
        tracker = EspnOutageTracker(pause_after=3, cancel_after_min=10)
        tracker.record_failure()
        assert not tracker.should_pause_new_orders

    def test_two_failures_no_pause(self):
        tracker = EspnOutageTracker(pause_after=3, cancel_after_min=10)
        tracker.record_failure()
        tracker.record_failure()
        assert not tracker.should_pause_new_orders

    def test_three_failures_triggers_pause(self):
        tracker = EspnOutageTracker(pause_after=3, cancel_after_min=10)
        tracker.record_failure()
        tracker.record_failure()
        tracker.record_failure()
        assert tracker.should_pause_new_orders

    def test_success_resets_consecutive_counter(self):
        tracker = EspnOutageTracker(pause_after=3, cancel_after_min=10)
        tracker.record_failure()
        tracker.record_failure()
        tracker.record_success()
        # Only 2 failures before reset, then 0 again
        assert not tracker.should_pause_new_orders

    def test_success_after_pause_clears_pause(self):
        tracker = EspnOutageTracker(pause_after=3, cancel_after_min=10)
        tracker.record_failure()
        tracker.record_failure()
        tracker.record_failure()
        assert tracker.should_pause_new_orders
        tracker.record_success()
        assert not tracker.should_pause_new_orders

    def test_cancel_not_triggered_without_failures(self):
        tracker = EspnOutageTracker(pause_after=3, cancel_after_min=10)
        assert not tracker.should_cancel_all

    def test_cancel_triggered_after_elapsed_time(self):
        import time
        tracker = EspnOutageTracker(pause_after=1, cancel_after_min=0)  # 0 min = immediate
        tracker.record_failure()
        # With cancel_after_min=0, cancel_after_sec=0; elapsed >= 0 is always True
        assert tracker.should_cancel_all

    def test_cancel_not_triggered_before_elapsed_time(self):
        tracker = EspnOutageTracker(pause_after=1, cancel_after_min=999)
        tracker.record_failure()
        # 999 minutes has not elapsed
        assert not tracker.should_cancel_all

    def test_success_resets_cancel_eligibility(self):
        tracker = EspnOutageTracker(pause_after=1, cancel_after_min=0)
        tracker.record_failure()
        assert tracker.should_cancel_all
        tracker.record_success()
        # After success, first_failure_ts resets, so cancel eligibility is cleared
        assert not tracker.should_cancel_all

    def test_custom_pause_threshold(self):
        tracker = EspnOutageTracker(pause_after=5, cancel_after_min=10)
        for _ in range(4):
            tracker.record_failure()
        assert not tracker.should_pause_new_orders
        tracker.record_failure()
        assert tracker.should_pause_new_orders
