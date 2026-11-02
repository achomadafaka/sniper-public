"""Immutable asynchronous paper quote completion identity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class PaperQuoteCompletion:
    """One paper quote result bound to the observation that requested it."""

    observation_id: str
    venue_id: str
    status: str
    economics: Mapping[str, object] | None
    failure_reason: str | None
    requested_at: str
    completed_at: str
