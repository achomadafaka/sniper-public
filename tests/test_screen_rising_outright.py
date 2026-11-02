"""The rising/soccer mechanical screen must not count long-dated OUTRIGHT/futures markets (winner,
top-scorer, relegation...) as in-season per-game soccer — they carry a sport keyword but aren't
near-term per-game markets. (discovery memo 2026-06-17)"""
from __future__ import annotations

import importlib

S = importlib.import_module("scripts.screen_rising_mechanics")
from sniper.analysis import market_taxonomy as LG


def test_shared_detector_is_the_leagues_one():
    # screen + discovery both use the single source in leagues.py (no duplicated keyword list)
    assert S._is_outright is LG.is_outright


def test_outright_markets_detected():
    # the actual discovery false-positives (Spain/Brazil/etc. WC-winner outrights) carry a clear
    # tournament-winner token; those are what we exclude.
    for slug in ["world-cup-2026-winner", "epl-2025-top-scorer", "premier-league-relegation",
                 "champions-league-winner", "golden-boot-2026", "nba-mvp-2026"]:
        assert S._is_outright(slug), slug


def test_per_game_markets_not_outright():
    # per-game directional bets (incl. "will X win tonight") must NOT be flagged — that's why the
    # detector keys on tournament-winner tokens, not the bare word "win" (which a per-game market
    # also uses). Question-phrased tournament outrights ("will-spain-win-the-world-cup") are left to
    # the endDate/season filter, not keyword detection, to avoid false-positiving per-game markets.
    for slug in ["nba-lal-bos-2026-06-15", "wc-arg-bra", "epl-ars-che", "nhl-edm-fla",
                 "will-lakers-win-tonight"]:
        assert not S._is_outright(slug), slug


def _clean(actv=0.5, **over):
    m = dict(fills_per_pos=10, median_fill=200, straddle_rate=0.02, exit_rate=0.20,
             active_share=actv, median_pos=5000, n_positions=20)
    m.update(over)
    return m


def test_verdict_active_share_floor_relaxed():
    # a CLEAN wallet with some (not majority) in-season activity now passes; below the floor it's
    # skipped; quality gates still bind.
    assert S.verdict(_clean(actv=0.30)) == "ENROLL"          # 0.30 >= 0.20 floor
    assert S.verdict(_clean(actv=0.10)) == "skip"            # below floor
    assert S.verdict(_clean(actv=0.5, fills_per_pos=60)) == "skip"   # churner still rejected
    assert S.verdict(_clean(actv=0.5, exit_rate=0.50)) == "skip"     # exit-driven still rejected
    assert S.verdict(_clean(actv=0.5, straddle_rate=0.5)) == "skip"  # straddler still rejected
    assert S.ACTIVE_SHARE_FLOOR == 0.20
