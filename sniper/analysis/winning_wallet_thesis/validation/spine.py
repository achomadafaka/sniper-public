from __future__ import annotations

from sniper.analysis.thesis_cards.admission import explain_paper_admission
from sniper.analysis.thesis_cards.enrollment import enroll_profile
from sniper.analysis.thesis_cards.profile import ThesisProfile as PaperProfile, _run_id
from sniper.analysis.winning_wallet_thesis.methods.types import valid_forward_test_design
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook
from sniper.analysis.winning_wallet_thesis.validation.cards import method_finding_to_card
from sniper.analysis.winning_wallet_thesis.validation.power import estimate_forward_power
from sniper.analysis.winning_wallet_thesis.validation.types import ValidationPlan


def build_validation_plans(
    wallet: str,
    book: PositionBook,
    clusters: list[EdgeCluster],
    findings_by_cluster: dict[str, list],
    *,
    horizon_days: int = 30,
    min_events: int = 20,
) -> list[ValidationPlan]:
    """Route each finding (spec §4.8): forward-power gate -> paper_queued if powered,
    replication_required if thin, blocked/descriptive otherwise. Never enrolls live here."""
    cluster_by_id = {c.cluster_id: c for c in clusters}
    plans: list[ValidationPlan] = []
    for cluster_id, findings in findings_by_cluster.items():
        cluster = cluster_by_id.get(cluster_id)
        if cluster is None:
            continue
        power = estimate_forward_power(book, cluster, horizon_days=horizon_days, min_events=min_events)
        for finding in findings:
            thesis_id = f"{cluster.cluster_id}:{finding.method}"
            if finding.runnability == "data_blocked" or finding.state == "blocked_missing_feed":
                plans.append(ValidationPlan(thesis_id, "blocked_missing_feed", power,
                                            blockers=list(finding.blockers)))
                continue
            if not valid_forward_test_design(finding.forward_test_design):
                plans.append(ValidationPlan(thesis_id, "descriptive_only", power,
                                            blockers=["missing_forward_test_design"]))
                continue
            if not power.powered:
                plans.append(ValidationPlan(thesis_id, "replication_required", power,
                                            blockers=["underpowered_single_wallet_forward"]))
                continue
            card = method_finding_to_card(wallet, cluster, finding)
            plans.append(ValidationPlan(thesis_id, "paper_queued", power, paper_card=card))
    return plans


def enroll_validation_plans(wallet: str, plans: list[ValidationPlan], *, store, enrolled_at: int):
    """OPT-IN (operator/dry-run): enroll only `paper_queued` cards through the existing
    paper-enrollment conveyor. Admission (FDR + placebo + min-n) still applies; descriptive/
    replication/blocked plans are never enrolled."""
    cards = [p.paper_card for p in plans
             if p.validation_state == "paper_queued" and p.paper_card is not None]
    decisions = explain_paper_admission(cards)
    eligible = [d.card for d in decisions if d.decision == "admitted"]
    profile = PaperProfile(
        wallet=wallet,
        n_positions=sum(card.n for card in cards),
        cards=cards,
        headline=f"{len(eligible)} winning-wallet theses queued for paper",
        profile_run_id=_run_id(wallet, cards),
        decisions=decisions,
        eligible=eligible,
    )
    return enroll_profile(profile, store=store, enrolled_at=enrolled_at)
