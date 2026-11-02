from sniper.analysis.winning_wallet_thesis.methods.calibration import favorite_longshot_calibration
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _ep(event_slug, condition_id, won, cost):
    return PositionEpisode(
        wallet="0xw", condition_id=condition_id, event_slug=event_slug, outcome_index=0,
        episode_index=0, buy_shares=100, buy_cost=cost, resolved=True, won=won,
    )


def _ctx(eps):
    cluster = EdgeCluster("c", "test", {}, "price", len(eps),
                          len({e.event_slug for e in eps}), 0, 0,
                          [(e.condition_id, e.outcome_index, e.episode_index) for e in eps])
    return ClusterContext(cluster, PositionBook("0xw", eps))


def test_calibration_uses_market_level_not_position_weighting():
    # e1 has two positions but counts ONCE at market/event level -> n == 2 events.
    eps = [_ep("e1", "c1", 1, 20), _ep("e1", "c1b", 1, 20), _ep("e2", "c2", 0, 90)]
    finding = favorite_longshot_calibration(_ctx(eps), min_events=2)
    assert finding.evidence["market_weighted"] is True
    assert finding.n == 2


def test_calibration_descriptive_under_min_events():
    eps = [_ep("e1", "c1", 1, 20)]
    finding = favorite_longshot_calibration(_ctx(eps), min_events=5)
    assert finding.state == "descriptive" and finding.forward_test_design is None
