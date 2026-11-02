from pathlib import Path

from sniper.analysis.public_truth_stale.report import PaperEntry, build_markdown_report
from sniper.analysis.public_truth_stale.types import ConfirmationStatus, SourceLane


def test_report_keeps_lane_a_and_lane_b_claims_separate():
    entries = [
        PaperEntry(
            signal_id="a1",
            source_lane=SourceLane.RESOLUTION_AUTHORITATIVE,
            confirmation_status=ConfirmationStatus.CONFIRMED_BY_MARKET_RESOLUTION,
            stake_usdc=20.0,
            avg_entry_price=0.80,
            shares=25.0,
            net_edge=0.195,
            entered=True,
            rejection_reason="",
        ),
        PaperEntry(
            signal_id="b1",
            source_lane=SourceLane.OFFICIAL_SOCIAL_SOURCE,
            confirmation_status=ConfirmationStatus.STILL_UNCONFIRMED,
            stake_usdc=20.0,
            avg_entry_price=0.70,
            shares=28.571428,
            net_edge=0.295,
            entered=True,
            rejection_reason="",
        ),
    ]

    report = build_markdown_report(entries, source_path=Path("signals.csv"))

    assert "Lane A: resolution_authoritative" in report
    assert "Lane B: official_social_source" in report
    assert "combined pnl" not in report.lower()
    assert "strategy works" not in report.lower()


def test_report_marks_rejected_candidates_without_entering_them():
    entries = [
        PaperEntry(
            signal_id="d1",
            source_lane=SourceLane.AMBIGUITY_DISPUTE,
            confirmation_status=ConfirmationStatus.AMBIGUOUS,
            stake_usdc=20.0,
            avg_entry_price=0.70,
            shares=28.571428,
            net_edge=0.295,
            entered=False,
            rejection_reason="lane_d_excluded",
        ),
    ]

    report = build_markdown_report(entries, source_path=Path("signals.csv"))

    assert "Lane D: ambiguity_dispute" in report
    assert "entered: 0" in report
    assert "lane_d_excluded" in report
