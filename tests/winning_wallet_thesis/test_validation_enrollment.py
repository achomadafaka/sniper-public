from sniper.analysis.thesis_cards.enrollment import InMemoryPaperEnrollmentStore
from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster
from sniper.analysis.winning_wallet_thesis.validation.cards import method_finding_to_card
from sniper.analysis.winning_wallet_thesis.validation.spine import enroll_validation_plans
from sniper.analysis.winning_wallet_thesis.validation.types import ForwardPower, ValidationPlan


def _strong_paper_plan():
    finding = MethodFinding(
        "favorite_longshot_calibration", "supported_proxy", 12.0, 0.15, 40,
        evidence={}, controls={}, blockers=[], runnability="forward_testable",
        forward_test_design=ForwardTestDesign("cluster_calibration_edge", "to_resolution", .03, -.02, 20),
    )
    cluster = EdgeCluster("c1", "UFC", {"tag": "UFC"}, "tag", 40, 40, 100, 12)
    card = method_finding_to_card("0xw", cluster, finding)
    return ValidationPlan("c1:cal", "paper_queued", ForwardPower(40, 30, True), paper_card=card)


def _descriptive_plan():
    return ValidationPlan("c2:exit", "descriptive_only", ForwardPower(0, 30, False))


def test_enroll_validation_plans_only_enrolls_paper_queued_cards():
    store = InMemoryPaperEnrollmentStore()
    result = enroll_validation_plans("0xw", [_strong_paper_plan(), _descriptive_plan()],
                                     store=store, enrolled_at=123)
    assert len(result.enrollments) == 1            # only the strong paper_queued card admitted


def test_enroll_skips_when_no_paper_queued():
    store = InMemoryPaperEnrollmentStore()
    result = enroll_validation_plans("0xw", [_descriptive_plan()], store=store, enrolled_at=123)
    assert result.enrollments == []
