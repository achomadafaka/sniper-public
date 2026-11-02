"""Pure data carriers + enums for the crypto-5m capture spine.

No behavior here beyond trivial helpers — discovery/validation/capture logic lives in sibling
modules. Everything is JSON-serializable-friendly so raw payloads can be persisted verbatim.

Three *independent* gates (do not conflate — see the spec's "Three independent gates"):
  1. ``RuleVerdict.rule_supported`` — the market matches the contract we understand.
  2. ``market_resolver.capture_ready`` — the live book is capturable *now* (market-state).
  3. ``PriceToBeatQuality`` — do we have an authoritative Chainlink start reference?
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

CHAINLINK_FIXED_POINT_SCALE = Decimal(10) ** 18  # full_accuracy_value = price * 10**18 (exact)


class RuleReason(str, enum.Enum):
    """Verdict reasons from the rule contract (persisted as text). Does NOT include priceToBeat —
    that is a data-availability concern handled by the price-to-beat quality lane, not the rule."""

    SUPPORTED = "supported"
    UNSUPPORTED_RULE = "unsupported_rule"
    UNSUPPORTED_SOURCE = "unsupported_source"
    MALFORMED_WINDOW = "malformed_window"
    MISSING_TOKEN_MAPPING = "missing_token_mapping"
    UNEXPECTED_OUTCOME_SHAPE = "unexpected_outcome_shape"
    MISSING_CONDITION_ID = "missing_condition_id"
    WRONG_SERIES = "wrong_series"
    AMBIGUOUS_MARKET = "ambiguous_market"


class PriceToBeatQuality(str, enum.Enum):
    """Authority of a reconstructed Chainlink start reference. Fail-closed: only the top two are
    acceptable for a paper decision; ``proxy_only``/``unavailable`` must not drive decisions."""

    EXACT_CHAINLINK_REPORT = "exact_chainlink_report"           # authenticated signed report (deferred)
    VALIDATED_RTDS_RECONSTRUCTION = "validated_rtds_reconstruction"  # locked RTDS boundary rule
    PROXY_ONLY = "proxy_only"                                   # Binance etc. — research/basis only
    UNAVAILABLE = "unavailable"                                 # no trustworthy boundary observation


class RtdsDeliveryMode(str, enum.Enum):
    """How a window's Chainlink reference was actually delivered (never assume — record it)."""

    LIVE_UPDATES = "live_updates"
    SNAPSHOT_RECONSTRUCTION = "snapshot_reconstruction"
    AUTHENTICATED_CHAINLINK_REPORT = "authenticated_chainlink_report"
    UNAVAILABLE = "unavailable"


class ClockSource(str, enum.Enum):
    """Where 'now' came from. Binance server time is a fallback, not hidden canonical truth."""

    SYSTEM_NTP = "system_ntp"
    POLYMARKET_HTTP_DATE = "polymarket_http_date"
    BINANCE_SERVER = "binance_server"
    MANUAL_OFFSET = "manual_offset"


class QualityFlag(str, enum.Enum):
    """Capture quality flags. An uncertain book must never be treated as executable truth."""

    BOOK_STALE = "book_stale"
    WEBSOCKET_GAP = "websocket_gap"
    REST_RECOVERY = "rest_recovery"
    TIMESTAMP_REGRESSION = "timestamp_regression"
    MALFORMED_UPDATE = "malformed_update"
    TOKEN_UNMAPPED = "token_unmapped"
    RULE_CHANGED = "rule_changed"
    MARKET_CLOSED = "market_closed"
    CAPTURE_INCOMPLETE = "capture_incomplete"


@dataclass(frozen=True)
class RuleVerdict:
    """Result of validating a candidate market against the supported 5m BTC Up/Down *contract*.

    ``rule_supported`` answers only "is this the instrument we understand?" — it says nothing about
    whether the market is live (``capture_ready``) or whether we have a start reference
    (``PriceToBeatQuality``). Those are deliberately separate gates.
    """

    reason: RuleReason
    rule_supported: bool
    rule_contract_version: int
    detail: str = ""


@dataclass(frozen=True)
class BookLevel:
    """A single price level. ``price``/``size`` kept as floats here; the book computes best via a
    price→size map (never array position)."""

    price: float
    size: float


@dataclass(frozen=True)
class ChainlinkTick:
    """One Chainlink BTC/USD observation from the RTDS stream. ``full_accuracy_value`` (price * 10**18
    integer string) is the EXACT value; ``price`` derives a precise ``Decimal`` from it, falling back
    to the float ``value`` only when the exact field is absent (e.g. snapshot backfill items)."""

    symbol: str
    source_ts_ms: int            # Chainlink observation time (ms; 1s-aligned)
    server_ts_ms: Optional[int]  # outer RTDS message timestamp (ms; sub-second)
    recv_ts: Optional[int]       # our server-time seconds at receipt (never local +7h clock)
    value: float                 # float convenience field
    full_accuracy_value: Optional[str]  # exact price * 10**18 as integer string
    is_snapshot: bool = False    # from the on-subscribe data[] backfill rather than a live update

    @property
    def price(self) -> Decimal:
        if self.full_accuracy_value:
            return Decimal(self.full_accuracy_value) / CHAINLINK_FIXED_POINT_SCALE
        return Decimal(str(self.value))


@dataclass(frozen=True)
class MarketSpec:
    """A discovered, contract-validated BTC 5m market. ``price_to_beat`` is the Gamma oracle value
    (None until resolution); the live reference comes from the RTDS lane, not this field."""

    event_id: str
    market_id: str
    condition_id: str
    slug: str
    question: str
    raw_rule_text: str
    rule_text_hash: str
    resolution_source: str
    series_id: str
    market_created_ts: Optional[str]
    window_start_ts: int
    window_end_ts: int
    up_token_id: str
    down_token_id: str
    outcomes: tuple  # ("Up", "Down")
    price_to_beat: Optional[float]   # Gamma oracle (post-resolution); NOT the live reference
    fees_enabled: bool
    fee_type: Optional[str]
    taker_base_fee: Optional[float]
    maker_base_fee: Optional[float]
    tick_size: Optional[float]
    min_order_size: Optional[float]
    active: bool = False
    closed: bool = False
    accepting_orders: bool = False
    enable_order_book: bool = False
    asset: str = "BTC"
    raw_payload: dict = field(default_factory=dict)

    def token_side(self, token_id: str) -> Optional[str]:
        """Map a token id to its outcome side ("Up"/"Down"), or None if unmapped."""
        if token_id == self.up_token_id:
            return "Up"
        if token_id == self.down_token_id:
            return "Down"
        return None


@dataclass(frozen=True)
class Settlement:
    """Economic truth: which Polymarket token won. Exact Chainlink end value not required here."""

    condition_id: str
    winning_token_id: Optional[str]
    winning_outcome: Optional[str]   # "Up" / "Down"
    resolution_ts: Optional[str]
    uma_resolution_status: Optional[str]
    outcome_prices: tuple            # ("1","0") or ("0","1")
    source_used: str                 # "websocket" / "gamma" / "clob"
    reconciliation_status: str       # "agree" / "gamma_only" / "ws_only" / "conflict" / "unresolved"
    gamma_price_to_beat: Optional[float] = None  # the post-hoc oracle for boundary-rule calibration


@dataclass(frozen=True)
class ReferenceObservation:
    """A *proxy* price snapshot (Lane B, research only). Binance is never settlement truth and never
    drives a paper decision — it exists to quantify basis vs the Chainlink-settled winner."""

    condition_id: str
    source: str                      # "binance"
    checkpoint: str                  # "start" / "mid" / "end"
    observed_ts: int                 # unix seconds (server time, never local +7h clock)
    price: Optional[float]


@dataclass(frozen=True)
class CaptureGap:
    """An interval where the captured book is uncertain and was recovered (or could not be)."""

    condition_id: str
    window_start_ts: int
    gap_kind: str                    # a QualityFlag value
    detected_ts: int
    recovery_action: str             # "rest_resnapshot" / "none" / "abandoned"
    reconnect_generation: int
