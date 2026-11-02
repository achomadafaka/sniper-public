from sniper.analysis.winning_wallet_thesis import types as T


def test_position_episode_realized_pnl_held_winner():
    ep = T.PositionEpisode(
        wallet="0xw", condition_id="0xc", event_slug="ufc-x", outcome_index=0,
        episode_index=0, buy_shares=100.0, buy_cost=40.0, sell_shares=0.0,
        sell_proceeds=0.0, resolved=True, won=1,
    )
    # held 100 winning shares -> $100 payout, cost $40 -> +$60
    assert round(ep.realized_pnl, 2) == 60.0
    assert round(ep.entry_vwap, 4) == 0.40


def test_position_episode_unresolved_pnl_is_none():
    ep = T.PositionEpisode(wallet="0xw", condition_id="0xc", event_slug="e",
                           outcome_index=0, episode_index=0, buy_shares=10.0,
                           buy_cost=5.0, resolved=False)
    assert ep.realized_pnl is None


def test_position_episode_held_loser_pnl_is_negative_cost():
    ep = T.PositionEpisode(wallet="0xw", condition_id="0xc", event_slug="e",
                           outcome_index=0, episode_index=0, buy_shares=100.0,
                           buy_cost=40.0, resolved=True, won=0)
    assert round(ep.realized_pnl, 2) == -40.0


def test_position_episode_push_pays_half_and_is_a_real_loss_above_entry():
    # bought 100 @ 0.70 ($70), market pushed -> 0.5/share refund ($50) -> -$20 (a real loss, not 0)
    ep = T.PositionEpisode(wallet="0xw", condition_id="0xc", event_slug="e", outcome_index=0,
                           episode_index=0, buy_shares=100.0, buy_cost=70.0,
                           resolved=True, won=None, is_push=True)
    assert round(ep.realized_pnl, 2) == -20.0


def test_position_episode_push_below_entry_is_a_real_gain():
    # bought 100 @ 0.30 ($30), pushed -> $50 refund -> +$20
    ep = T.PositionEpisode(wallet="0xw", condition_id="0xc", event_slug="e", outcome_index=0,
                           episode_index=0, buy_shares=100.0, buy_cost=30.0,
                           resolved=True, won=None, is_push=True)
    assert round(ep.realized_pnl, 2) == 20.0


def test_position_episode_sold_out_unresolved_returns_banked_cash():
    # bought 100 @ 0.40 ($40), sold all 100 @ 0.55 ($55) before the market resolved -> +$15 banked
    ep = T.PositionEpisode(wallet="0xw", condition_id="0xc", event_slug="e", outcome_index=0,
                           episode_index=0, buy_shares=100.0, buy_cost=40.0,
                           sell_shares=100.0, sell_proceeds=55.0, resolved=False)
    assert round(ep.realized_pnl, 2) == 15.0


def test_position_episode_partially_sold_unresolved_still_none():
    # still holding 40 of 100 in an unresolved market -> not yet realized
    ep = T.PositionEpisode(wallet="0xw", condition_id="0xc", event_slug="e", outcome_index=0,
                           episode_index=0, buy_shares=100.0, buy_cost=40.0,
                           sell_shares=60.0, sell_proceeds=33.0, resolved=False)
    assert ep.realized_pnl is None
