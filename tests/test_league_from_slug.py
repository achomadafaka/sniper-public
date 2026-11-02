"""Public slug/title -> coarse league classifier (fallback for scans without Gamma tags), so the
screen + discovery scripts don't each reach into leagues._ACTIVE_KW."""
from __future__ import annotations

from sniper.analysis import market_taxonomy as LG


def test_league_from_slug_known():
    cases = {
        "nba-lal-bos-2026": "Basketball",
        "nhl-edm-fla": "NHL",
        "world-cup-arg-bra": "Soccer",
        "epl-ars-che": "Soccer",
        "wimbledon-mens-final": "Tennis",
        "ufc-300-main": "MMA/Box",
        "cbb-duke-unc": "CBB",
    }
    for slug, lg in cases.items():
        assert LG.league_from_slug(slug) == lg, slug


def test_slug_classifier_covers_active_sports_and_matches_tag_names():
    # the screen compares slug-classified positions against the tag-derived active_leagues set, so
    # the slug classifier must (a) cover the currently-active sports and (b) use the SAME coarse
    # names the tag taxonomy produces — else in-season bettors score active_share~0 (the bug that
    # rejected all 59 candidates: wnba mis-read as NBA; cricket/golf unclassified).
    assert LG.league_from_slug("wnba-lv-nyl") == "WNBA"        # not "NBA" (substring collision)
    assert LG.league_from_slug("nba-lal-bos") == "Basketball"  # canonical tag name, not "NBA"
    assert LG.league_from_slug("cricket-ind-aus") == "Cricket"
    assert LG.league_from_slug("golf-pga-championship") == "Golf"
    # names must equal what league_from_tags produces for the same sport
    assert LG.league_from_slug("wnba-x") == LG.league_from_tags(["Sports", "WNBA"])
    assert LG.league_from_slug("nba-x") == LG.league_from_tags(["Sports", "Basketball", "NBA"])
    assert LG.league_from_slug("cricket-x") == LG.league_from_tags(["Sports", "Cricket"])
    assert LG.league_from_slug("golf-x") == LG.league_from_tags(["Sports", "Golf"])


def test_league_from_slug_unknown_is_none():
    assert LG.league_from_slug("trump-vs-biden-2028") is None
    assert LG.league_from_slug("") is None
    assert LG.league_from_slug(None) is None


def test_is_outright_does_not_drop_per_game_champions_league():
    # REGRESSION: "champion" substring used to match per-game UCL slugs and silently drop them from
    # the mechanics screen. Genuine winner-futures must still be caught (via "winner").
    assert LG.is_outright("champions-league-man-city-real-madrid-2026") is False
    assert LG.is_outright("Champions League: Real Madrid vs Man City") is False
    assert LG.is_outright("champions-league-winner-2026") is True          # genuine outright (via "winner")
    assert LG.is_outright("world-cup-2026-winner") is True
    assert LG.is_outright("nba-lal-bos-2026") is False                     # plain per-game


def test_us_open_not_misclassified_as_tennis_via_slug():
    # "us-open" is ambiguous (tennis AND golf) — removed from the Tennis slug set so a golf US Open
    # isn't mislabeled Tennis on the fallback path. Unambiguous tennis tokens still classify.
    assert LG.league_from_slug("2026-us-open-golf") == "Golf"
    assert LG.league_from_slug("wimbledon-final") == "Tennis"


def test_screen_delegates_to_leagues():
    import importlib
    S = importlib.import_module("scripts.screen_rising_mechanics")
    assert S._league_kw("nba-lal-bos") == "Basketball"
    assert S._league_kw("nba-lal-bos") == LG.league_from_slug("nba-lal-bos")
