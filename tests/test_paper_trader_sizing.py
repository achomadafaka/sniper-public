import unittest

import paper_trader


class TestPaperTraderSizing(unittest.TestCase):
    def setUp(self) -> None:
        # Avoid DB setup; sizing logic is pure and does not require initialization.
        self.trader = paper_trader.PaperTrader.__new__(paper_trader.PaperTrader)

    @staticmethod
    def _signal(
        *,
        current_price: float = 0.45,
        win_rate: float = 0.65,
        qualifying_roi=None,
        position_insider_score: float = 70.0,
    ):
        return {
            "current_price": current_price,
            "win_rate": win_rate,
            "qualifying_roi": qualifying_roi,
            "position_insider_score": position_insider_score,
        }

    def test_higher_qualifying_roi_increases_size(self):
        low = self.trader.calculate_position_size(
            self._signal(qualifying_roi=10.0, position_insider_score=75.0)
        )
        high = self.trader.calculate_position_size(
            self._signal(qualifying_roi=55.0, position_insider_score=75.0)
        )
        self.assertGreater(high, low)

    def test_higher_insider_score_increases_size(self):
        low = self.trader.calculate_position_size(
            self._signal(qualifying_roi=25.0, position_insider_score=45.0)
        )
        high = self.trader.calculate_position_size(
            self._signal(qualifying_roi=25.0, position_insider_score=90.0)
        )
        self.assertGreater(high, low)

    def test_unknown_qualifying_roi_is_more_conservative_than_known(self):
        unknown = self.trader.calculate_position_size(
            self._signal(qualifying_roi=None, position_insider_score=90.0)
        )
        known = self.trader.calculate_position_size(
            self._signal(qualifying_roi=20.0, position_insider_score=90.0)
        )
        self.assertLess(unknown, known)

    def test_size_clamped_to_minimum(self):
        tiny = self.trader.calculate_position_size(
            self._signal(
                current_price=0.79,
                win_rate=0.10,
                qualifying_roi=None,
                position_insider_score=45.0,
            )
        )
        self.assertEqual(tiny, min(paper_trader.PAPER_MIN_POSITION_SIZE, paper_trader.PAPER_MAX_POSITION_SIZE))

    def test_size_clamped_to_maximum(self):
        original_base = paper_trader.PAPER_POSITION_SIZE
        try:
            paper_trader.PAPER_POSITION_SIZE = 250.0
            huge = self.trader.calculate_position_size(
                self._signal(
                    current_price=0.05,
                    win_rate=0.99,
                    qualifying_roi=300.0,
                    position_insider_score=100.0,
                )
            )
            self.assertEqual(huge, max(paper_trader.PAPER_MIN_POSITION_SIZE, paper_trader.PAPER_MAX_POSITION_SIZE))
        finally:
            paper_trader.PAPER_POSITION_SIZE = original_base


    def test_win_rate_normalised_from_percentage(self):
        """Win rate stored as 0-100 (legacy) must be normalised to 0-1 before sizing."""
        size_legacy = self.trader.calculate_position_size(
            self._signal(win_rate=65.0, qualifying_roi=25.0, position_insider_score=75.0)
        )
        size_fraction = self.trader.calculate_position_size(
            self._signal(win_rate=0.65, qualifying_roi=25.0, position_insider_score=75.0)
        )
        self.assertAlmostEqual(size_legacy, size_fraction, places=4,
            msg="Legacy 0-100 win_rate must produce same size as 0-1 fraction")

    def test_insider_score_fallback_to_insider_score_key(self):
        """When position_insider_score is absent, insider_score key is used as fallback."""
        signal_with_position_key = {
            "current_price": 0.30,
            "win_rate": 0.65,
            "qualifying_roi": 25.0,
            "position_insider_score": 80.0,
        }
        signal_with_fallback_key = {
            "current_price": 0.30,
            "win_rate": 0.65,
            "qualifying_roi": 25.0,
            "insider_score": 80.0,   # no position_insider_score key
        }
        size_explicit = self.trader.calculate_position_size(signal_with_position_key)
        size_fallback = self.trader.calculate_position_size(signal_with_fallback_key)
        self.assertAlmostEqual(size_explicit, size_fallback, places=4,
            msg="Fallback from position_insider_score to insider_score must produce same result")

    def test_size_monotonic_with_entry_price(self):
        """Lower entry price (more upside) must produce larger or equal size."""
        sizes = [
            self.trader.calculate_position_size(
                self._signal(current_price=p, qualifying_roi=25.0, position_insider_score=75.0)
            )
            for p in [0.10, 0.25, 0.40, 0.60, 0.75]
        ]
        for i in range(len(sizes) - 1):
            self.assertGreaterEqual(
                sizes[i], sizes[i + 1],
                msg=f"Size at lower price must be >= size at higher price: {sizes}"
            )

    def test_elite_insider_score_above_threshold_gets_bonus(self):
        """Insider score above PAPER_SIZING_ELITE_INSIDER_SCORE gets a small additional bonus."""
        at_elite = self.trader.calculate_position_size(
            self._signal(qualifying_roi=30.0, position_insider_score=paper_trader.PAPER_SIZING_ELITE_INSIDER_SCORE)
        )
        above_elite = self.trader.calculate_position_size(
            self._signal(qualifying_roi=30.0, position_insider_score=paper_trader.PAPER_SIZING_ELITE_INSIDER_SCORE + 5.0)
        )
        self.assertGreaterEqual(above_elite, at_elite,
            msg="Score above elite threshold must get bonus, not penalty")

    def test_all_factors_at_defaults_produce_valid_size(self):
        """Default signal values must produce a size within [min, max]."""
        size = self.trader.calculate_position_size(self._signal())
        self.assertGreaterEqual(size, paper_trader.PAPER_MIN_POSITION_SIZE)
        self.assertLessEqual(size, paper_trader.PAPER_MAX_POSITION_SIZE)


if __name__ == "__main__":
    unittest.main()
