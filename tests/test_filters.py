import unittest

from sniper.filters import is_excluded_market, is_generic_sports_winner_market


class SportsFilterTests(unittest.TestCase):
    def test_generic_sports_winner_is_excluded(self) -> None:
        title = "Will the New England Patriots win Super Bowl 2026?"
        slug = "nfl-will-the-new-england-patriots-win-super-bowl-2026"
        generic, reason = is_generic_sports_winner_market(title, slug)
        self.assertTrue(generic)
        self.assertEqual(reason, "generic_sports_winner_market")

        excluded, excluded_reason = is_excluded_market(title, slug)
        self.assertTrue(excluded)
        self.assertEqual(excluded_reason, "generic_sports_winner_market")

    def test_specific_sports_prop_is_not_excluded(self) -> None:
        title = "Will Devon Witherspoon win the Super Bowl LX MVP?"
        slug = "nfl-super-bowl-lx-mvp-devon-witherspoon"
        generic, _ = is_generic_sports_winner_market(title, slug)
        self.assertFalse(generic)

        excluded, _ = is_excluded_market(title, slug)
        self.assertFalse(excluded)


if __name__ == "__main__":
    unittest.main()
