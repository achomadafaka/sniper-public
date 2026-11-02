from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.specialist import specialist_domain
from sniper.analysis.winning_wallet_thesis.methods.template import recurring_template
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _episode(i, template="ufc-fight-night"):
    return PositionEpisode(
        wallet="0xw", condition_id=f"c{i}", event_slug=f"e{i}", outcome_index=0,
        episode_index=0, buy_shares=100, buy_cost=40, resolved=True, won=1,
        tags=["UFC"], category="sports", template=template,
    )


def _ctx(n=6, edge_pp=60):
    episodes = [_episode(i) for i in range(n)]
    cluster = EdgeCluster(
        cluster_id="ufc", label="UFC held", scope={"tag": "UFC"}, axis="tag",
        n_positions=n, n_events=n, pnl=360, edge_pp=edge_pp,
        member_position_keys=[(ep.condition_id, ep.outcome_index, ep.episode_index) for ep in episodes],
    )
    return ClusterContext(cluster=cluster, book=PositionBook(wallet="0xw", episodes=episodes))


def test_specialist_supported_for_powered_positive_tag_cluster():
    finding = specialist_domain(_ctx())
    assert finding.method == "specialist_domain_edge"
    assert finding.state == "supported_proxy"
    assert finding.runnability == "forward_testable"
    assert finding.forward_test_design is not None


def test_specialist_descriptive_under_min_events():
    finding = specialist_domain(_ctx(n=3))
    assert finding.state == "descriptive" and finding.forward_test_design is None


def test_recurring_template_supported_when_events_share_template():
    finding = recurring_template(_ctx())
    assert finding.method == "recurring_template_edge"
    assert finding.state == "supported_proxy"
    assert finding.evidence["template"] == "ufc-fight-night"
