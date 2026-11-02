"""Active-winner intake (spec §1 doctrine). V1 intake = operator-curated active winners.
Every candidate is tagged with explicit survivorship risk — the extractor conditions on
winning, so the risk is named, not hidden."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CandidateWallet:
    wallet: str
    source: str
    selection_note: str
    survivorship_risk: str = "unknown"


class CandidateSource(Protocol):
    def candidates(self) -> list[CandidateWallet]:
        ...


def load_curated_candidates(path: str) -> list[CandidateWallet]:
    """Operator-curated active winners from a JSON list. survivorship_risk defaults to
    'high' — curated winners are conditioned on past wins by construction."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return [
        CandidateWallet(
            wallet=str(row["wallet"]),
            source=str(row.get("source", "curated")),
            selection_note=str(row.get("selection_note", "")),
            survivorship_risk=str(row.get("survivorship_risk", "high")),
        )
        for row in raw
    ]
