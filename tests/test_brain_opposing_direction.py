"""Regression tests for brain.add_trade_idea() opposing-direction expiry.

Critical behavior: when Claude produces a new signal in the opposite direction
on the same market (e.g., we had buy_yes, now Claude thinks buy_no), the old
open idea must be EXPIRED before the new one is inserted. Without this, the
Intel tab shows contradictory live signals — buy_yes AND buy_no on the same
market simultaneously — which destroys credibility.

The pre-existing test in test_brain_dedup.py checks that duplicate
(condition_id, direction) pairs are rejected (UNIQUE constraint behavior).
These tests check the *new* behavior: that opposing directions mutually expire.
"""
from __future__ import annotations

import unittest

from brain import Brain


class TestOpposingDirectionExpiry(unittest.TestCase):

    def _brain(self) -> Brain:
        return Brain(db_path=":memory:")

    def _add(self, brain: Brain, condition_id: str, direction: str) -> int | None:
        return brain.add_trade_idea(
            condition_id=condition_id,
            market_title="Test Market",
            outcome="YES" if direction == "buy_yes" else "NO",
            direction=direction,
            reasoning="Test reasoning",
            current_price=0.40,
            confidence="medium",
        )

    # ------------------------------------------------------------------ #
    # Core expiry behavior                                                 #
    # ------------------------------------------------------------------ #

    def test_buy_no_expires_existing_buy_yes(self):
        """When we add buy_no, any open buy_yes on the same condition must expire."""
        brain = self._brain()
        self._add(brain, "cond-1", "buy_yes")

        ideas = brain.get_all_trade_ideas(limit=10)
        self.assertEqual(len(ideas), 1)
        self.assertEqual(ideas[0]["status"], "open")

        # Now Claude flips to buy_no
        no_id = self._add(brain, "cond-1", "buy_no")
        self.assertIsNotNone(no_id)

        ideas = brain.get_all_trade_ideas(limit=10)
        by_direction = {i["direction"]: i for i in ideas}

        # The old buy_yes must be expired
        self.assertEqual(by_direction["buy_yes"]["status"], "expired",
                         "Old buy_yes should be expired when buy_no arrives")
        self.assertEqual(by_direction["buy_yes"]["notes"], "superseded by opposing signal")

        # The new buy_no must be open
        self.assertEqual(by_direction["buy_no"]["status"], "open",
                         "New buy_no should be open")

    def test_buy_yes_expires_existing_buy_no(self):
        """Symmetry check: buy_yes added after buy_no expires the buy_no."""
        brain = self._brain()
        self._add(brain, "cond-2", "buy_no")

        yes_id = self._add(brain, "cond-2", "buy_yes")
        self.assertIsNotNone(yes_id)

        ideas = brain.get_all_trade_ideas(limit=10)
        by_direction = {i["direction"]: i for i in ideas}

        self.assertEqual(by_direction["buy_no"]["status"], "expired")
        self.assertEqual(by_direction["buy_yes"]["status"], "open")

    # ------------------------------------------------------------------ #
    # Non-interference: different condition_ids must not affect each other #
    # ------------------------------------------------------------------ #

    def test_opposing_direction_only_expires_same_condition(self):
        """Expiry must be scoped to condition_id — other markets must not be touched."""
        brain = self._brain()
        self._add(brain, "cond-A", "buy_yes")
        self._add(brain, "cond-B", "buy_yes")  # different market

        # Flip only cond-A to buy_no
        self._add(brain, "cond-A", "buy_no")

        ideas = brain.get_all_trade_ideas(limit=10)
        by_cond = {}
        for i in ideas:
            by_cond.setdefault(i["condition_id"], {})[i["direction"]] = i

        # cond-A: buy_yes expired, buy_no open
        self.assertEqual(by_cond["cond-A"]["buy_yes"]["status"], "expired")
        self.assertEqual(by_cond["cond-A"]["buy_no"]["status"], "open")

        # cond-B: buy_yes untouched / still open
        self.assertEqual(by_cond["cond-B"]["buy_yes"]["status"], "open",
                         "cond-B buy_yes should not be affected by cond-A flip")

    # ------------------------------------------------------------------ #
    # Direction normalization                                              #
    # ------------------------------------------------------------------ #

    def test_uppercase_direction_still_expires_opposing(self):
        """Direction input may arrive as 'BUY_YES' — must normalise before comparing."""
        brain = self._brain()
        self._add(brain, "cond-3", "buy_yes")

        # Caller passes uppercase — should still expire the buy_yes
        no_id = brain.add_trade_idea(
            condition_id="cond-3",
            market_title="Test",
            outcome="NO",
            direction="BUY_NO",     # uppercase
            reasoning="flip",
            current_price=0.40,
            confidence="HIGH",      # uppercase confidence too
        )
        self.assertIsNotNone(no_id)

        ideas = brain.get_all_trade_ideas(limit=10)
        by_direction = {i["direction"]: i for i in ideas}
        self.assertEqual(by_direction["buy_yes"]["status"], "expired")
        self.assertEqual(by_direction["buy_no"]["status"], "open")

    # ------------------------------------------------------------------ #
    # Only expires *open* ideas — already-expired ideas stay expired      #
    # ------------------------------------------------------------------ #

    def test_does_not_resurface_previously_expired_idea(self):
        """If buy_yes was already expired by some other process, a new buy_no
        must not reset it or create any inconsistency."""
        brain = self._brain()
        conn = brain._get_conn()

        # Manually insert a pre-expired buy_yes row
        conn.execute(
            """INSERT INTO trade_ideas
               (condition_id, market_title, outcome, direction, reasoning,
                current_price, confidence, expires_at, status)
               VALUES ('cond-4','Market','YES','buy_yes','old signal',
                       0.40,'medium','2020-01-01 00:00:00','expired')"""
        )
        conn.commit()

        # Now add buy_no — should not change the already-expired buy_yes
        no_id = self._add(brain, "cond-4", "buy_no")
        self.assertIsNotNone(no_id)

        ideas = brain.get_all_trade_ideas(limit=10)
        # Exactly 2 rows: the manually inserted expired one + our new buy_no
        self.assertEqual(len(ideas), 2)
        by_direction = {i["direction"]: i for i in ideas}
        self.assertEqual(by_direction["buy_yes"]["status"], "expired")
        self.assertEqual(by_direction["buy_no"]["status"], "open")

    # ------------------------------------------------------------------ #
    # Idempotency: same direction twice still works as before              #
    # ------------------------------------------------------------------ #

    def test_same_direction_second_add_returns_none_and_does_not_expire_itself(self):
        """Adding buy_yes twice: second call returns None, first row stays open."""
        brain = self._brain()
        first_id = self._add(brain, "cond-5", "buy_yes")
        second_id = self._add(brain, "cond-5", "buy_yes")

        self.assertIsNotNone(first_id)
        self.assertIsNone(second_id)

        ideas = brain.get_all_trade_ideas(limit=10)
        self.assertEqual(len(ideas), 1)
        self.assertEqual(ideas[0]["status"], "open")


if __name__ == "__main__":
    unittest.main()
