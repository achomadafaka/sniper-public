from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _ep(event_slug, condition_id, won, entry=0.40, cost=40.0):
    return PositionEpisode(
        wallet="0xw",
        condition_id=condition_id,
        event_slug=event_slug,
        outcome_index=0,
        episode_index=0,
        buy_shares=100,
        buy_cost=cost,
        resolved=True,
        won=won,
        tags=["UFC"],
        template="ufc-fight-night",
    )


def _cluster(keys):
    return EdgeCluster(cluster_id="c", label="UFC", scope={"tag": "UFC"}, axis="tag",
                       n_positions=len(keys), n_events=len(keys), pnl=60, edge_pp=60,
                       member_position_keys=keys)


def test_context_returns_cluster_member_episodes():
    ep = _ep("e1", "c1", 1)
    ctx = ClusterContext(cluster=_cluster([("c1", 0, 0)]),
                         book=PositionBook(wallet="0xw", episodes=[ep]))
    assert ctx.episodes == [ep]
    assert ctx.event_count == 1
    assert ctx.pnl == 60.0          # held winner 100 shares @0.40 cost -> +60


def test_context_filters_to_member_keys_only():
    members = [_ep("e1", "c1", 1), _ep("e2", "c2", 0)]
    other = _ep("e3", "c3", 1)
    ctx = ClusterContext(cluster=_cluster([("c1", 0, 0), ("c2", 0, 0)]),
                         book=PositionBook(wallet="0xw", episodes=members + [other]))
    assert {e.condition_id for e in ctx.episodes} == {"c1", "c2"}
    assert ctx.event_count == 2
