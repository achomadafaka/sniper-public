from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class SourceLane(str, Enum):
    RESOLUTION_AUTHORITATIVE = "resolution_authoritative"
    OFFICIAL_SOCIAL_SOURCE = "official_social_source"
    TRUSTED_NEWS_SOURCE = "trusted_news_source"
    SPECIALIST_ACCOUNT = "specialist_account"
    AMBIGUITY_DISPUTE = "ambiguity_dispute"


class ConfirmationStatus(str, Enum):
    CONFIRMED_BY_RESOLUTION_AUTHORITATIVE = "confirmed_by_resolution_authoritative"
    CONFIRMED_BY_MARKET_RESOLUTION = "confirmed_by_market_resolution"
    CONTRADICTED_BY_RESOLUTION_AUTHORITATIVE = "contradicted_by_resolution_authoritative"
    STILL_UNCONFIRMED = "still_unconfirmed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ManualObservation:
    signal_id: str
    market_url: str
    market_id: str
    condition_id: str
    token_id: str
    outcome: str
    rule_text_hash: str
    resolution_source_url: str
    source_lane: SourceLane
    source_family: str
    source_account_id: str
    source_url: str
    source_timestamp: str
    fetch_timestamp: str
    observed_source_value: str
    rule_match_status: str
    finality_status: str
    confirmation_status: ConfirmationStatus
    stake_usdc: float
    modeled_cost_per_share: float
    orderbook_file: Path
    notes: str
