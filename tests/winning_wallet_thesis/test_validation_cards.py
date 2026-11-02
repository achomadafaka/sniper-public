from sniper.analysis.thesis_cards.contract import valid_forward_test_design
from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster
from sniper.analysis.winning_wallet_thesis.validation.cards import method_finding_to_card


def test_supported_forward_testable_finding_converts_to_thesis_card():
    finding = MethodFinding(
        "specialist_domain_edge", "supported_proxy", 10, 0.08, 30, evidence={"tag": "UFC"},
        controls={}, blockers=[], runnability="forward_testable",
        forward_test_design=ForwardTestDesign("cluster_calibration_edge", "to_resolution", .03, -.02, 20),
    )
    cluster = EdgeCluster("cluster1", "UFC", {"tag": "UFC"}, "tag", 30, 30, 100, 8)
    card = method_finding_to_card("0xw", cluster, finding)
    assert card.card_id == "winning_wallet:cluster1:specialist_domain_edge"
    assert card.forward_test_design["metric"] == "cluster_calibration_edge"
    # the design satisfies the existing conveyor enrollment contract
    assert valid_forward_test_design(card.forward_test_design)
    assert card.evidence["data_tier"] == "HAVE"


def test_blocked_finding_card_has_no_valid_design():
    finding = MethodFinding("stale_liquidity_sniping", "blocked_missing_feed", 0.0, None, 0,
                            blockers=["missing_price_path"], runnability="data_blocked")
    cluster = EdgeCluster("c2", "x", {}, "tag", 0, 0, 0, 0)
    card = method_finding_to_card("0xw", cluster, finding)
    assert card.forward_test_design is None
    assert card.evidence["data_tier"] == "BLOCKED"


def test_proxy_p_value_uses_n_events_not_positions():
    from sniper.analysis.winning_wallet_thesis.validation.cards import _proxy_p_value
    # same effect, but few independent EVENTS -> larger (less confident) p than many events
    f_events = MethodFinding("favorite_longshot_calibration", "supported_proxy", 5, 0.10, 50,
                             n_events=5)
    f_positions = MethodFinding("favorite_longshot_calibration", "supported_proxy", 5, 0.10, 50,
                                n_events=50)
    assert _proxy_p_value(f_events) > _proxy_p_value(f_positions)


def test_proxy_p_value_falls_back_to_n_when_n_events_unset():
    from sniper.analysis.winning_wallet_thesis.validation.cards import _proxy_p_value
    # n_events default 0 -> uses n (legacy behavior preserved)
    legacy = MethodFinding("favorite_longshot_calibration", "supported_proxy", 5, 0.10, 50)
    explicit = MethodFinding("favorite_longshot_calibration", "supported_proxy", 5, 0.10, 50,
                             n_events=50)
    assert _proxy_p_value(legacy) == _proxy_p_value(explicit)


def test_card_admission_sample_size_uses_independent_events_not_positions():
    finding = MethodFinding(
        "sizing_skill", "supported_proxy", 20.0, 0.40, 100,
        evidence={}, controls={}, blockers=[], runnability="forward_testable",
        n_events=1,
        forward_test_design=ForwardTestDesign("cluster_sizing_edge", "to_resolution", .01, -.01, 20),
    )
    cluster = EdgeCluster("one_event", "one event basket", {}, "tag", 100, 1, 4000, 20)
    card = method_finding_to_card("0xw", cluster, finding)

    assert card.n == 1
    assert card.p_value is None
    assert card.evidence["n_positions"] == 100
    assert card.evidence["n_events"] == 1
