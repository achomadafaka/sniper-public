"""Fresh-anomaly engine v2 — calibrated surprise over INDEPENDENT event families, not
cheap-win counts. The motivating false positive (3 of 12 cheap bets) must read ordinary;
correlated wins must collapse; public-truth-stale and structural shapes must be excluded."""
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis import fresh_anomaly as FA
from sniper.analysis.winning_wallet_thesis.types import PositionEpisode
from helpers_thesis import make_book


def test_12_bets_three_cheap_wins_not_strong():            # case 1
    book = make_book([dict(area="nba", entry=0.2, cost=600, won=w, slug=f"g{i}")
                      for i, w in enumerate([1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0])])
    a = FA.assess_fresh_anomaly(book)
    assert a.n_independent_events == 12
    assert a.channels["A"]["level"] in ("none", "weak")
    assert a.ex_post_level in ("none", "weak")
    assert a.high_surprise is False


def test_three_independent_penny_wins_strong():            # case 2
    book = make_book([dict(area="pol", entry=0.01, cost=8000, won=1, slug=f"e{i}") for i in range(3)])
    a = FA.assess_fresh_anomaly(book)
    assert a.channels["A"]["level"] == "strong"
    assert a.high_surprise is True


def test_three_cheap_wins_same_event_collapse():           # case 3
    book = make_book([dict(area="pol", entry=0.2, cost=8000, won=1, slug="same") for _ in range(3)])
    a = FA.assess_fresh_anomaly(book)
    assert a.n_independent_events == 1
    assert a.alt_explanation == "single_event"
    assert a.high_surprise is False


def test_public_truth_stale_suppresses_flag():             # case 9
    book = make_book([dict(area="pol", entry=0.1, cost=9000, won=1, slug="x",
                           first_buy_ts=1000, resolution_ts=500)])
    a = FA.assess_fresh_anomaly(book)
    assert a.alt_explanation == "public_truth_stale"
    assert a.high_surprise is False


def test_structural_paired_excluded():
    book = make_book([dict(area="x", entry=0.5, cost=5000, won=1, slug="m", cid="c1", oidx=0),
                      dict(area="x", entry=0.5, cost=5000, won=0, slug="m", cid="c1", oidx=1)])
    a = FA.assess_fresh_anomaly(book)
    assert a.excludes == "structural"
    assert a.assessable is False


def test_churn_flipper_excluded():
    # buys big, sells most back before resolution -> trader/flipper, not conviction
    book = make_book([dict(area="x", entry=0.4, cost=10000, won=1, slug=f"s{i}", sell_shares=24000)
                      for i in range(3)])  # buy_shares = 10000/0.4 = 25000; sells 24000 (96%)
    a = FA.assess_fresh_anomaly(book)
    assert a.excludes == "churn_or_arb"


def test_no_directional_is_not_assessable():
    a = FA.assess_fresh_anomaly(make_book([]))
    assert a.assessable is False


def test_clean_strong_case_two_channels_and_no_insider_text():
    # concentrated (C strong) + genuine surprise (A strong) -> high_surprise, >=2 channels
    book = make_book([dict(area="pol", entry=0.05, cost=9000, won=1, slug=f"e{i}") for i in range(5)])
    a = FA.assess_fresh_anomaly(book)
    assert a.high_surprise is True
    assert a.channels_strong >= 2
    blob = (a.sample_warning + " ".join(a.reasons)).lower()
    assert "insider" not in blob


def test_concentration_and_size_alone_do_not_flag():
    # big concentrated bets that simply LOST -> no surprise, must not high_surprise on C+D alone
    book = make_book([dict(area="pol", entry=0.6, cost=30000, won=0, slug=f"e{i}") for i in range(4)])
    a = FA.assess_fresh_anomaly(book)
    assert a.channels["C"]["level"] == "strong" and a.channels["D"]["level"] == "strong"
    assert a.high_surprise is False


def test_history_thin_counts_directional_not_structural_superset():
    # M2: 5 resolved directional + 40 small sold-out legs. resolved_with_pnl=45 (>40) but
    # resolved_directional=5 -> must read as THIN (the bug counted the superset).
    rows = [dict(area="pol", entry=0.05, cost=9000, won=1, slug=f"win{i}") for i in range(5)]
    rows += [dict(area="pol", entry=0.5, cost=10, won=1, slug=f"sold{i}", resolved=False, sell_shares=20)
             for i in range(40)]
    a = FA.assess_fresh_anomaly(make_book(rows))
    assert a.history_thin is True


def test_degenerate_zero_price_family_is_dropped_not_spurious():
    # CR5: a buy_cost=0 / buy_shares>0 leg has entry_vwap=0; a null p~0 would blow the e-value into
    # a spurious anomaly. It must be dropped (data_gap) and not counted as an independent event.
    book = make_book([dict(area="pol", entry=0.05, cost=9000, won=1, slug="r1"),
                      dict(area="pol", entry=0.05, cost=9000, won=1, slug="r2")])
    book.episodes.append(PositionEpisode(
        wallet="0xt", condition_id="degc", event_slug="degslug", outcome_index=0, episode_index=99,
        buy_shares=5.0, buy_cost=0.0, resolved=True, won=1, category="pol", tags=["pol"]))
    a = FA.assess_fresh_anomaly(book)
    assert a.n_independent_events == 2  # degenerate family excluded
    assert any("entry price" in g for g in a.data_gaps)
    import math
    assert math.isfinite(a.e_value)


def test_event_family_ties_are_fractional_not_wins():
    rows = []
    for i in range(8):
        rows.append(dict(area="nba", entry=0.5, cost=1000, won=1, slug=f"tie{i}", cid=f"tie{i}a"))
        rows.append(dict(area="nba", entry=0.5, cost=1000, won=0, slug=f"tie{i}", cid=f"tie{i}b"))
    a = FA.assess_fresh_anomaly(make_book(rows))
    assert a.n_independent_events == 8
    assert a.obs_over_expected == 1.0
    assert a.channels["A"]["level"] in ("none", "weak")
    assert a.high_surprise is False
