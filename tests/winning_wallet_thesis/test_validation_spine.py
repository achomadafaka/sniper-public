from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook
from sniper.analysis.winning_wallet_thesis.validation.spine import build_validation_plans


def _supported(min_n=20):
    return MethodFinding(
        "specialist_domain_edge", "supported_proxy", 5, 0.05, 5,
        evidence={}, controls={}, blockers=[], runnability="forward_testable",
        forward_test_design=ForwardTestDesign("cluster_calibration_edge", "to_resolution", .03, -.02, min_n),
    )


def test_unpowered_supported_finding_becomes_replication_required():
    cluster = EdgeCluster("c", "thin", {}, "tag", 5, 5, 10, 5)
    plans = build_validation_plans("0xw", PositionBook("0xw"), [cluster], {"c": [_supported()]})
    assert plans[0].validation_state == "replication_required"


def test_descriptive_finding_routes_descriptive_only():
    cluster = EdgeCluster("c", "x", {}, "tag", 5, 5, 10, 5)
    desc = MethodFinding("exit_hold_mechanism", "descriptive", 0.0, None, 5,
                         blockers=["missing_exit_time_price_path"], runnability="descriptive_only")
    plans = build_validation_plans("0xw", PositionBook("0xw"), [cluster], {"c": [desc]})
    assert plans[0].validation_state == "descriptive_only"


def test_blocked_feed_finding_stays_blocked_without_forward_design():
    cluster = EdgeCluster("c", "x", {}, "tag", 5, 5, 10, 5)
    blocked = MethodFinding("stale_liquidity_sniping", "blocked_missing_feed", 0.0, None, 0,
                            blockers=["missing_price_path"], runnability="data_blocked")
    plans = build_validation_plans("0xw", PositionBook("0xw"), [cluster], {"c": [blocked]})
    assert plans[0].validation_state == "blocked_missing_feed"
    assert plans[0].blockers == ["missing_price_path"]


def test_powered_supported_finding_becomes_paper_queued():
    from sniper.analysis.winning_wallet_thesis.types import PositionEpisode
    # dense cadence so forward power passes
    eps = [PositionEpisode("0xw", f"c{i}", f"e{i}", 0, 0, buy_shares=1, buy_cost=.4,
                           resolved=True, won=1, first_buy_ts=1_700_000_000 + i * 86400)
           for i in range(40)]
    cluster = EdgeCluster("c", "dense", {}, "tag", 40, 40, 100, 8,
                          [(e.condition_id, e.outcome_index, e.episode_index) for e in eps])
    plans = build_validation_plans("0xw", PositionBook("0xw", eps), [cluster],
                                   {"c": [_supported(min_n=20)]}, min_events=20)
    assert plans[0].validation_state == "paper_queued"
    assert plans[0].paper_card is not None
