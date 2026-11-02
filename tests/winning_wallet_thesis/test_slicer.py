import random

from sniper.analysis.winning_wallet_thesis import slicer as S
from sniper.analysis.winning_wallet_thesis.types import PositionBook, PositionEpisode


def _ep(cat, won, entry, slug, cost=100.0):
    e = PositionEpisode(wallet="0xw", condition_id=slug, event_slug=slug, outcome_index=0,
                        episode_index=0, buy_shares=cost / entry, buy_cost=cost,
                        resolved=True, won=won)
    e.category = cat
    e.subcat = cat.upper()
    e.tags = [cat.upper()]
    return e


def _book(eps):
    return PositionBook(wallet="0xw", episodes=eps)


def test_slice_edges_computes_edge_and_n_events():
    eps = [_ep("sports", 1, 0.4, f"ufc-{i}") for i in range(10)]
    tables = S.slice_edges(_book(eps))
    s = tables["category"]["sports"]
    assert s.n_positions == 10 and s.n_events == 10
    assert s.win_rate == 1.0 and round(s.mean_entry, 2) == 0.40
    assert round(s.edge_pp, 1) == 60.0          # 100% WR - 0.40 entry


def test_reward_share_pct_sane_on_losing_book():
    assert S.reward_share_pct(trading_pnl=-100.0, reward_total=25.0) == 20.0
    assert S.reward_share_pct(trading_pnl=0.0, reward_total=0.0) == 0.0


def test_reward_attribution_flags_non_usdc_token():
    ra = S.reward_attribution([
        {"type": "REWARD", "usdcSize": 100.0, "asset": ""},               # USDC/pUSD cash
        {"type": "REWARD", "usdcSize": 50.0, "asset": "SOME_TOKEN_ID"},   # token-denominated
    ])
    assert ra["usdc_total"] == 100.0                # only the cash row counts as USD
    assert ra["non_usdc_total"] == 50.0
    assert ra["non_usdc_flagged"] is True


def test_reward_attribution_all_usdc_is_not_flagged():
    ra = S.reward_attribution([
        {"type": "REWARD", "usdcSize": 10.0, "asset": ""},
        {"type": "YIELD", "usdcSize": 5.0},                                # absent asset -> USD
    ])
    assert ra["usdc_total"] == 15.0 and ra["non_usdc_flagged"] is False


def test_shuffle_baseline_reports_expected_vs_observed_and_restores_truth():
    rng = random.Random(0)
    eps = [_ep("sports", rng.randint(0, 1), 0.5, f"e{i}") for i in range(60)]
    book = _book(eps)
    before = [e.won for e in eps]
    base = S.shuffle_baseline(book, n_shuffles=20, seed=1)
    after = [e.won for e in eps]
    assert before == after                       # truth restored exactly
    assert base.n_shuffles == 20
    assert 0.0 <= base.top_slice_rank_under_null <= 1.0
    # obs_over_expected is internally consistent with observed/expected
    if base.expected_green > 0:
        assert abs(base.obs_over_expected - base.observed_green / base.expected_green) < 0.01


def test_shuffle_baseline_detects_greens_and_stays_consistent():
    # a clearly-edged book (UFC win / NBA lose in one band): mechanism finds green slices
    # and reports an internally-consistent expected/observed. Direction is structure-
    # dependent (validated on real data in the SwissMiss parity test), so we don't assert it.
    eps = [_ep("sports", 1, 0.4, f"ufc-{i}") for i in range(20)]
    for e in eps:
        e.subcat = "UFC"; e.tags = ["UFC"]
    nba = [_ep("sports", 0, 0.4, f"nba-{i}") for i in range(20)]
    for e in nba:
        e.subcat = "NBA"; e.tags = ["NBA"]
    base = S.shuffle_baseline(_book(eps + nba), n_shuffles=30, seed=1)
    assert base.observed_green > 0 and base.expected_green > 0
    assert abs(base.obs_over_expected - base.observed_green / base.expected_green) < 0.01
