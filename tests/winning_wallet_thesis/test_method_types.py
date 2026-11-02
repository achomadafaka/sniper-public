import pytest

from sniper.analysis.winning_wallet_thesis.methods.types import (
    ForwardTestDesign,
    MethodFinding,
    valid_forward_test_design,
)


def test_method_finding_requires_known_state():
    finding = MethodFinding(
        method="specialist_domain_edge",
        state="supported_proxy",
        score=1.2,
        effect=7.5,
        n=30,
        evidence={"tag": "UFC"},
        controls={"nearest_sibling": "sports_non_ufc"},
        blockers=[],
        runnability="forward_testable",
        forward_test_design=ForwardTestDesign(
            metric="cluster_calibration_edge",
            horizon="to_resolution",
            success_threshold=0.03,
            kill_threshold=-0.02,
            min_forward_n=20,
            stake_convention="uniform_paper_unit",
        ),
    )
    assert finding.state == "supported_proxy"
    assert valid_forward_test_design(finding.forward_test_design)


def test_blocked_finding_has_no_forward_design():
    finding = MethodFinding(
        method="stale_liquidity_sniping",
        state="blocked_missing_feed",
        score=0.0,
        effect=None,
        n=0,
        evidence={},
        controls={},
        blockers=["missing_price_path"],
        runnability="data_blocked",
    )
    assert finding.forward_test_design is None


def test_unknown_state_rejected():
    with pytest.raises(ValueError):
        MethodFinding(method="x", state="bogus", score=0.0, effect=None, n=0)


def test_negative_score_rejected():
    with pytest.raises(ValueError):
        MethodFinding(method="x", state="descriptive", score=-1.0, effect=None, n=0)
