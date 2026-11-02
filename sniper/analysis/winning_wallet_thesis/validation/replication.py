from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.validation.types import ValidationPlan


def replication_required_summary(plans: list[ValidationPlan]) -> dict:
    """Summarize which theses need cross-wallet replication (thin single-wallet forward).
    Does NOT run cross-wallet batches — that needs operator-provided comparable winners
    and is wired to `sniper.analysis.thesis_cards.batch_runner.replication_check` later."""
    required = [p for p in plans if p.validation_state == "replication_required"]
    return {
        "replication_required": len(required),
        "thesis_ids": [p.thesis_id for p in required],
        "state": "not_run" if required else "not_required",
    }
