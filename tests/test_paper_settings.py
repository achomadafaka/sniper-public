import unittest

import sniper.settings.paper as paper_settings
import paper_trader
import sniper.settings.web as web_settings


class TestPaperSettings(unittest.TestCase):
    def test_entry_min_insider_score_is_90(self):
        """Raised from 80→90 (2026-03-30): only 90+ profitable at scale in 234 resolved trades.
        90-100: 61.4% WR +$384. 80-89: 51.5% WR -$36. Break-even is 55.9%."""
        self.assertEqual(paper_settings.PAPER_ENTRY_MIN_INSIDER_SCORE, 90)

    def test_shared_defaults_align_between_runtime_and_ui(self):
        self.assertEqual(paper_settings.PAPER_MAX_CONCURRENT, paper_trader.PAPER_MAX_CONCURRENT)
        self.assertEqual(paper_settings.PAPER_MAX_CONCURRENT, web_settings.PAPER_MAX_CONCURRENT)
        self.assertEqual(paper_settings.PAPER_POSITION_SIZE_USDC, web_settings.PAPER_POSITION_SIZE_USDC)
        self.assertEqual(paper_settings.PAPER_STOP_LOSS_PCT, web_settings.PAPER_STOP_LOSS_PCT)
        self.assertEqual(paper_settings.PAPER_TAKE_PROFIT_PCT, web_settings.PAPER_TAKE_PROFIT_PCT)


if __name__ == "__main__":
    unittest.main()
