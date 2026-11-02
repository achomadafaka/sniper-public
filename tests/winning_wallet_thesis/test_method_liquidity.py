from sniper.analysis.winning_wallet_thesis.data.price_path import InMemoryPricePathProvider
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.liquidity import dead_hour_timing, stale_liquidity
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _ctx():
    ep = PositionEpisode("0xw", "c1", "e1", 0, 0, buy_shares=10, buy_cost=4,
                         first_buy_ts=100, resolved=True, won=1)
    cluster = EdgeCluster("c", "test", {}, "tag", 1, 1, 1, 10,
                          [(ep.condition_id, ep.outcome_index, ep.episode_index)])
    return ClusterContext(cluster, PositionBook("0xw", [ep]))


def test_stale_liquidity_blocks_without_price_path():
    finding = stale_liquidity(_ctx(), price_path=None)
    assert finding.state == "blocked_missing_feed"
    assert "missing_price_path" in finding.blockers
    assert finding.runnability == "data_blocked"


def test_stale_liquidity_uses_post_entry_drift_when_available():
    provider = InMemoryPricePathProvider({"c1": [(110, .55), (200, .60)]})
    finding = stale_liquidity(_ctx(), price_path=provider)
    assert finding.evidence["mean_post_entry_drift"] > 0
    assert finding.state == "supported_proxy"


def test_stale_liquidity_passes_outcome_side_to_price_path():
    class SideAwareProvider:
        def path(self, condition_id, *, start_ts, end_ts, outcome_index=None):
            assert condition_id == "c1"
            assert outcome_index == 0
            return [(110, .55)]

    finding = stale_liquidity(_ctx(), price_path=SideAwareProvider())
    assert finding.state == "supported_proxy"


def test_dead_hour_timing_is_descriptive():
    finding = dead_hour_timing(_ctx())
    assert finding.method == "dead_hour_timing"
    assert finding.runnability == "descriptive_only"
