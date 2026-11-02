from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sniper.analysis.public_truth_stale.types import ConfirmationStatus, SourceLane


LANE_LABELS = {
    SourceLane.RESOLUTION_AUTHORITATIVE: "Lane A: resolution_authoritative",
    SourceLane.OFFICIAL_SOCIAL_SOURCE: "Lane B: official_social_source",
    SourceLane.TRUSTED_NEWS_SOURCE: "Lane C: trusted_news_source",
    SourceLane.SPECIALIST_ACCOUNT: "Lane C: specialist_account",
    SourceLane.AMBIGUITY_DISPUTE: "Lane D: ambiguity_dispute",
}


@dataclass(frozen=True)
class PaperEntry:
    signal_id: str
    source_lane: SourceLane
    confirmation_status: ConfirmationStatus
    stake_usdc: float
    avg_entry_price: float
    shares: float
    net_edge: float
    entered: bool
    rejection_reason: str


def build_markdown_report(entries: list[PaperEntry], *, source_path: Path) -> str:
    lines = [
        "# Public-Truth Stale Manual Paper Report",
        "",
        f"Source log: `{source_path}`",
        "",
        "Claims are lane-specific. Lane A, B, and C results are not combined.",
        "Lane D is excluded from v0 pass/fail.",
        "",
    ]

    if not entries:
        lines.extend(
            [
                "## Deployment State",
                "",
                "- candidates: 0",
                "- entered: 0",
                "- state: initialized, waiting for manually captured signals",
                "",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    for lane in SourceLane:
        lane_entries = [entry for entry in entries if entry.source_lane is lane]
        if not lane_entries:
            continue
        label = LANE_LABELS[lane]
        entered_entries = [entry for entry in lane_entries if entry.entered]
        avg_edge = sum(entry.net_edge for entry in lane_entries) / len(lane_entries)
        confirmed = sum(
            1
            for entry in lane_entries
            if entry.confirmation_status
            in {
                ConfirmationStatus.CONFIRMED_BY_RESOLUTION_AUTHORITATIVE,
                ConfirmationStatus.CONFIRMED_BY_MARKET_RESOLUTION,
            }
        )
        lines.extend(
            [
                f"## {label}",
                "",
                f"- candidates: {len(lane_entries)}",
                f"- entered: {len(entered_entries)}",
                f"- confirmed: {confirmed}",
                f"- mean_net_edge: {avg_edge:.4f}",
                "",
                "| signal_id | entry_status | rejection_reason | confirmation_status | stake_usdc | avg_entry_price | shares | net_edge |",
                "|---|---:|---|---|---:|---:|---:|---:|",
            ]
        )
        for entry in lane_entries:
            lines.append(
                "| "
                f"{entry.signal_id} | "
                f"{'entered' if entry.entered else 'rejected'} | "
                f"{entry.rejection_reason} | "
                f"{entry.confirmation_status.value} | "
                f"{entry.stake_usdc:.2f} | "
                f"{entry.avg_entry_price:.4f} | "
                f"{entry.shares:.4f} | "
                f"{entry.net_edge:.4f} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
