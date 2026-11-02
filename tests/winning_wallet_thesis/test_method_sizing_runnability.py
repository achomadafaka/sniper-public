from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.runnability import classify_runnability
from sniper.analysis.winning_wallet_thesis.methods.sizing import sizing_skill
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _ctx():
    eps = [
        PositionEpisode("0xw", "c1", "e1", 0, 0, buy_shares=200, buy_cost=80, resolved=True, won=1),
        PositionEpisode("0xw", "c2", "e2", 0, 0, buy_shares=20, buy_cost=10, resolved=True, won=0),
    ]
    cluster = EdgeCluster("c", "sizing", {}, "tag", 2, 2, 110, 30,
                          [(e.condition_id, e.outcome_index, e.episode_index) for e in eps])
    return ClusterContext(cluster, PositionBook("0xw", eps))


def test_sizing_returns_descriptive_under_low_n():
    finding = sizing_skill(_ctx(), min_n=20)
    assert finding.state == "descriptive"
    assert finding.forward_test_design is None


def test_runnability_flags_blocked_feed():
    finding = MethodFinding("x", "blocked_missing_feed", 0, None, 0, blockers=["missing_news_feed"])
    assert classify_runnability(finding) == "data_blocked"


def test_runnability_forward_testable_for_supported_with_design():
    from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign
    f = MethodFinding("x", "supported_proxy", 1.0, 5.0, 30, runnability="forward_testable",
                      forward_test_design=ForwardTestDesign("m", "to_resolution", 0.03, -0.02, 20))
    assert classify_runnability(f) == "forward_testable"
