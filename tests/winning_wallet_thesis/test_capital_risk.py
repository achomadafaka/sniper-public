from sniper.analysis.winning_wallet_thesis.capital_risk import capital_row, exit_value_summary
from sniper.analysis.winning_wallet_thesis.types import (
    CapitalRiskSlice, ExitValueSummary, PositionEpisode,
)


def test_capital_risk_types_hold_budget_and_exit_fields():
    row = CapitalRiskSlice(
        slice_key="UFC", axis="tag", n_positions=10, n_events=8, buy_cost=1000.0,
        budget_share_pct=25.0, realized_pnl=100.0, turnover_roi_pct=10.0, avg_bet=100.0,
        median_bet=90.0, p90_bet=200.0, max_bet=250.0, bet_cv=0.5,
        top_event_budget_share_pct=20.0, peak_deployed=None, roi_on_peak_deployed_pct=None,
        max_drawdown=None, exit_value_added=None, loser_rescue_value=None, winner_upside_lost=None,
    )
    exits = ExitValueSummary(2, 1, 1, -10.0, 40.0, -50.0)
    assert row.slice_key == "UFC"
    assert exits.loser_rescue_value == 40.0


def _ep(event, cost, pnl):
    return PositionEpisode(
        wallet="0xw", condition_id=event, event_slug=event, outcome_index=0, episode_index=0,
        buy_shares=100, buy_cost=cost, sell_proceeds=cost + pnl, sell_shares=100,
        resolved=True, won=1,
    )


def test_capital_row_computes_budget_share_and_bet_distribution():
    row = capital_row("UFC", "tag", [_ep("e1", 100, 10), _ep("e2", 300, 30)], total_buy_cost=1000)
    assert row.buy_cost == 400
    assert row.budget_share_pct == 40.0
    assert row.avg_bet == 200
    assert row.max_bet == 300
    assert row.turnover_roi_pct == 10.0


def test_capital_row_fills_exit_value_fields():
    loser = PositionEpisode("0xw", "c1", "e1", 0, 0, buy_shares=100, buy_cost=60,
                            sell_shares=100, sell_proceeds=40, resolved=True, won=0)
    winner = PositionEpisode("0xw", "c2", "e2", 0, 0, buy_shares=100, buy_cost=60,
                             sell_shares=100, sell_proceeds=70, resolved=True, won=1)
    row = capital_row("UFC", "tag", [loser, winner], total_buy_cost=120)
    assert row.loser_rescue_value == 40
    assert row.winner_upside_lost == -30
    assert row.exit_value_added == 10


def test_peak_deployed_is_max_concurrent_not_gross_turnover():
    from sniper.analysis.winning_wallet_thesis.capital_risk import _peak_deployed, _realized_max_drawdown
    # two positions open at the same time (overlap) -> peak = both; one earlier closes first
    a = PositionEpisode("0xw", "a", "a", 0, 0, buy_shares=1, buy_cost=100,
                        first_buy_ts=10, last_ts=30, resolved=True, won=1, sell_shares=0)
    b = PositionEpisode("0xw", "b", "b", 0, 0, buy_shares=1, buy_cost=50,
                        first_buy_ts=20, last_ts=40, resolved=True, won=0, sell_shares=0)
    # gross turnover = 150, but max concurrent (both open in [20,30]) = 150 here;
    # a non-overlapping pair would peak at the larger single position.
    assert _peak_deployed([a, b]) == 150
    c = PositionEpisode("0xw", "c", "c", 0, 0, buy_shares=1, buy_cost=100,
                        first_buy_ts=100, last_ts=110, resolved=True, won=1)
    d = PositionEpisode("0xw", "d", "d", 0, 0, buy_shares=1, buy_cost=40,
                        first_buy_ts=200, last_ts=210, resolved=True, won=1)
    assert _peak_deployed([c, d]) == 100        # never overlap -> peak is the bigger one


def test_close_ts_uses_resolution_time_not_last_activity():
    from sniper.analysis.winning_wallet_thesis.capital_risk import _close_ts, _peak_deployed
    # held position: last buy at 100, resolved at 500, no redeem in window (last_ts=100).
    # Capital is at risk until resolution (500), NOT until the last buy (100).
    held = PositionEpisode("0xw", "c", "e", 0, 0, buy_shares=1, buy_cost=200,
                           first_buy_ts=100, last_ts=100, resolved=True, won=1,
                           resolution_ts=500, sell_shares=0)
    assert _close_ts(held) == 500                         # resolution time, not last_ts
    # sold-early: sold at 300, resolves at 500 -> freed at the sell
    sold = PositionEpisode("0xw", "d", "e2", 0, 0, buy_shares=1, buy_cost=50,
                           first_buy_ts=100, last_ts=300, sell_shares=1, sell_proceeds=60,
                           resolved=True, won=1, resolution_ts=500)
    assert _close_ts(sold) == 300                         # exited before resolution
    # the held position would have looked never-closing under the old last_ts logic;
    # now it closes at 500 so it doesn't accumulate forever.
    assert _peak_deployed([held]) == 200


def test_realized_max_drawdown_tracks_giveback():
    from sniper.analysis.winning_wallet_thesis.capital_risk import _realized_max_drawdown
    # equity path: +100 (ts1), -60 (ts2), +20 (ts3) -> peak 100, trough 40 -> dd 60
    eps = [
        PositionEpisode("0xw", "1", "e1", 0, 0, buy_shares=1, buy_cost=1, sell_shares=1,
                        sell_proceeds=101, last_ts=1, resolved=True, won=1),    # +100
        PositionEpisode("0xw", "2", "e2", 0, 0, buy_shares=1, buy_cost=60, sell_shares=1,
                        sell_proceeds=0, last_ts=2, resolved=True, won=0),       # -60
        PositionEpisode("0xw", "3", "e3", 0, 0, buy_shares=1, buy_cost=1, sell_shares=1,
                        sell_proceeds=21, last_ts=3, resolved=True, won=1),      # +20
    ]
    assert _realized_max_drawdown(eps) == 60


def test_exit_value_splits_loser_rescue_and_winner_upside_lost():
    loser = PositionEpisode("0xw", "c1", "e1", 0, 0, buy_shares=100, buy_cost=60,
                            sell_shares=100, sell_proceeds=40, resolved=True, won=0)
    winner = PositionEpisode("0xw", "c2", "e2", 0, 0, buy_shares=100, buy_cost=60,
                             sell_shares=100, sell_proceeds=70, resolved=True, won=1)
    out = exit_value_summary([loser, winner])
    assert out.n_sold == 2
    assert out.loser_rescue_value == 40        # rescued from $0
    assert out.winner_upside_lost == -30       # sold 100 shares @70 vs $100 if held
    assert out.sell_vs_hold_value == 10
