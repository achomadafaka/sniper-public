# tests/test_brain_dedup.py
"""Tests for trade idea deduplication in Brain (UNIQUE index on condition_id, direction)."""
from __future__ import annotations

import unittest

from brain import Brain


class TestTradeIdeaDedup(unittest.TestCase):
    """Verify that duplicate (condition_id, direction) pairs are rejected."""

    def _brain(self) -> Brain:
        """Create a Brain backed by an isolated in-memory SQLite DB."""
        return Brain(db_path=":memory:")

    def _add_idea(self, brain: Brain, condition_id: str, direction: str) -> int | None:
        return brain.add_trade_idea(
            condition_id=condition_id,
            market_title="Test Market",
            outcome="YES" if direction == "buy_yes" else "NO",
            direction=direction,
            reasoning="Test reasoning",
            current_price=0.45,
            confidence="medium",
        )

    def test_duplicate_trade_idea_rejected(self):
        """Second add_trade_idea with same (condition_id, direction) must return None."""
        brain = self._brain()
        first_id = self._add_idea(brain, "cond-abc", "buy_yes")
        self.assertIsNotNone(first_id)
        self.assertGreater(first_id, 0)

        second_id = self._add_idea(brain, "cond-abc", "buy_yes")
        self.assertIsNone(second_id)

        # Only one row should exist in the DB
        ideas = brain.get_all_trade_ideas(limit=10)
        self.assertEqual(len(ideas), 1)

    def test_different_direction_same_condition_allowed(self):
        """buy_yes and buy_no for the same condition_id must both be accepted."""
        brain = self._brain()
        yes_id = self._add_idea(brain, "cond-xyz", "buy_yes")
        no_id = self._add_idea(brain, "cond-xyz", "buy_no")

        self.assertIsNotNone(yes_id)
        self.assertIsNotNone(no_id)
        self.assertGreater(yes_id, 0)
        self.assertGreater(no_id, 0)
        self.assertNotEqual(yes_id, no_id)

        ideas = brain.get_all_trade_ideas(limit=10)
        self.assertEqual(len(ideas), 2)


if __name__ == "__main__":
    unittest.main()
