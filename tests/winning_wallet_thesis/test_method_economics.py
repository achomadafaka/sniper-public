from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.exit_hold import exit_hold_mechanism
from sniper.analysis.winning_wallet_thesis.methods.reward import reward_attribution
from sniper.analysis.winning_wallet_thesis.methods.velocity import capital_velocity
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _ctx(reward_income=None):
    eps = [
        PositionEpisode("0xw", "c1", "e1", 0, 0, buy_shares=100, buy_cost=40,
                        resolved=True, won=1, redeemed=True),
        PositionEpisode("0xw", "c2", "e2", 0, 0, buy_shares=100, buy_cost=60,
                        sell_shares=100, sell_proceeds=55, resolved=True, won=1),
    ]
    cluster = EdgeCluster("c", "test", {}, "tag", 2, 2, 55, 25,
                          [(e.condition_id, e.outcome_index, e.episode_index) for e in eps])
    return ClusterContext(cluster, PositionBook("0xw", eps, reward_income=reward_income or {"wallet": 3.0}))


def test_reward_attribution_is_descriptive_prediction_vs_rewards_gate():
    finding = reward_attribution(_ctx())
    assert finding.method == "reward_attribution"
    assert finding.state == "descriptive"
    assert finding.evidence["reward_share_pct"] >= 0


def test_exit_hold_is_descriptive_not_promotable():
    finding = exit_hold_mechanism(_ctx())
    assert finding.state == "descriptive"
    assert finding.forward_test_design is None
    assert "missing_exit_time_price_path" in finding.blockers


def test_velocity_is_meta_method_only():
    finding = capital_velocity(_ctx())
    assert finding.method == "capital_velocity"
    assert finding.runnability == "descriptive_only"
