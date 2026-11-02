"""Quote-derived fills for qualifying Radar v6 decisions and their matched control.

The Radar arms are scored against local constant-product math on the captured decision
state. This adapter produces the other thing: a fill derived from a fresh executable
quote. The receipt records both the provider's expected amount and its slippage
threshold; `paper_lifecycle` scores the expected amount less a MEASURED execution
haircut. The threshold is a signer bound that the caller sets, not a fill, and scoring
at it made the result move with `slippageBps` instead of with the market.

A quote is not proof of signing, submission, inclusion, or execution -- it is a tighter,
provider-sourced bound than local math, and nothing more.

Two append-only rows per decision. The request is written in the same transaction as
the alert, so a crash leaves the qualifying signal visible as `pending` rather than
erasing it. The terminal receipt is written once, later. `pending` and `expired` are
derived from a request without a receipt; they are never stored, because a stored
lifecycle column would need an UPDATE and neither table accepts one.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .identity import content_fingerprint, require_utc_iso
from .store import ImmutableConflict
from .venue_policy import scoreable_venues


ADAPTER_VERSION = "radar-paper-adapter-v1"
FILL_MODEL_VERSION = "route-floor-v1"
# Cross-system cohort key. `memecoin_v6_paper_runner` and `memecoin_v6_paper_report`
# split on this exact literal, so it does NOT track the rule version.
SIGNAL_SOURCE = "radar_v5"
PAPER_RULE_VERSION = "pump-confirmation-truth-v6"
# The matched control. It runs the identical lifecycle, clip, cadence, and exit rules on
# the same observable band as v6 -- standard mode, measured round trip at or above the
# solvency floor -- and differs only in the confirmation predicate. Without it a forward
# v6 number has no counterfactual and the funding gate cannot be read.
PAPER_CONTROL_RULE_VERSION = "route-control-v2"
# Keyed by venue and filtered to the SCOREABLE ones. These maps feed cohort
# eligibility, the arm registry, and every `by_venue` result block, so a retired venue
# left in here keeps a UI tile and a result row it can never fill.
_ALL_PAPER_RULE_BY_VENUE = {
    "pumpfun_solana": PAPER_RULE_VERSION,
    "bags_robinhood_4663": "bags-confirmation-v1",
    "pons_v1_4663": "pons-confirmation-v1",
}
_ALL_PAPER_CONTROL_BY_VENUE = {
    "pumpfun_solana": PAPER_CONTROL_RULE_VERSION,
    "bags_robinhood_4663": "bags-route-control-v1",
    "pons_v1_4663": "pons-route-control-v1",
}
_SCOREABLE = frozenset(venue.value for venue in scoreable_venues())
PAPER_RULE_BY_VENUE = {
    venue: rule for venue, rule in _ALL_PAPER_RULE_BY_VENUE.items() if venue in _SCOREABLE
}
PAPER_CONTROL_BY_VENUE = {
    venue: rule for venue, rule in _ALL_PAPER_CONTROL_BY_VENUE.items() if venue in _SCOREABLE
}
PAPER_RULE_VERSIONS = (
    *PAPER_RULE_BY_VENUE.values(),
    *PAPER_CONTROL_BY_VENUE.values(),
)
# The control fires about 57 times a day against v6's 11. One open paper position already
# takes most of the 2-second global quote pace, so the control is sampled down to the
# arm's rate. The sample is deterministic on `alert_id`, which is a content fingerprint,
# so it is reproducible and independent of the outcome.
PAPER_CONTROL_SAMPLE_DENOMINATOR = 5

# Every terminal non-fill carries one of these. Unfilled is evidence, not an error:
# the rate and the reason mix are a primary output of this work, so an untyped or
# free-text reason would destroy the only thing the row is for.
UNFILLED_REASONS = frozenset({
    "capacity_expired",
    "quote_unavailable",
    "no_route",
    "malformed_quote",
    "stale_quote",
    "identity_invalid",
    "consumer_unavailable",
})

_LAUNCH_MODES = frozenset({"standard", "cashback", "mayhem"})


def paper_request_id(
    *,
    alert_id: str,
    adapter_version: str,
    fill_model_version: str,
    clip_usd_micros: int,
) -> str:
    """Deterministic request identity.

    Version-scoped on purpose: the same alert under a different adapter or fill model
    is a different measurement and must not collide with the first one. Determinism is
    what makes a retried request a no-op instead of a duplicate.
    """
    return content_fingerprint({
        "alert_id": alert_id,
        "adapter_version": adapter_version,
        "fill_model_version": fill_model_version,
        "clip_usd_micros": int(clip_usd_micros),
    })


@dataclass(frozen=True)
class PaperFillRequest:
    request_id: str
    alert_id: str
    launch_id: str
    network_id: str
    venue_id: str
    token_id: str
    creator_id: str | None
    launch_mode: str
    state_fingerprint: str
    rule_version: str
    evaluator_version: str
    adapter_version: str
    fill_model_version: str
    clip_usd_micros: int
    decision_at: str
    decision_age_ms: int
    intent_at: str
    expires_at: str
    local_qty_atomic: int | None

    def __post_init__(self) -> None:
        if self.launch_mode not in _LAUNCH_MODES:
            raise ValueError(f"unknown launch_mode: {self.launch_mode!r}")
        for field_name in ("decision_at", "intent_at", "expires_at"):
            object.__setattr__(
                self, field_name, require_utc_iso(getattr(self, field_name))
            )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(require_utc_iso(value).replace("Z", "+00:00"))


def _elapsed_ms(later: str | None, earlier: str | None) -> int | None:
    if later is None or earlier is None:
        return None
    return max(0, int((_parse(later) - _parse(earlier)).total_seconds() * 1000))


def _sqlite_atomic(value: object) -> str | None:
    """Bind an exact atomic quantity without an SQLite 64-bit limit."""
    return None if value is None else str(int(value))


def append_paper_fill_request(conn: sqlite3.Connection, request: PaperFillRequest) -> str:
    """Append one request. Idempotent by deterministic `request_id`."""
    values = {
        "request_id": request.request_id,
        "alert_id": request.alert_id,
        "launch_id": request.launch_id,
        "network_id": request.network_id,
        "venue_id": request.venue_id,
        "token_id": request.token_id,
        "creator_id": request.creator_id,
        "launch_mode": request.launch_mode,
        "state_fingerprint": request.state_fingerprint,
        "rule_version": request.rule_version,
        "evaluator_version": request.evaluator_version,
        "adapter_version": request.adapter_version,
        "fill_model_version": request.fill_model_version,
        "clip_usd_micros": int(request.clip_usd_micros),
        "decision_at": request.decision_at,
        "decision_age_ms": int(request.decision_age_ms),
        "intent_at": request.intent_at,
        "expires_at": request.expires_at,
        "local_qty_atomic": request.local_qty_atomic,
    }
    fingerprint = content_fingerprint(values)
    stored_values = {
        **values,
        "local_qty_atomic": _sqlite_atomic(values["local_qty_atomic"]),
    }
    cursor = conn.execute(
        "INSERT OR IGNORE INTO op_paper_fill_requests "
        f"({','.join(values)},row_fingerprint,inserted_at) "
        f"VALUES ({','.join('?' for _ in values)},?,?)",
        (*stored_values.values(), fingerprint, request.intent_at),
    )
    if cursor.rowcount == 0:
        existing = conn.execute(
            "SELECT row_fingerprint FROM op_paper_fill_requests WHERE request_id=?",
            (request.request_id,),
        ).fetchone()
        if existing is None or str(existing[0]) != fingerprint:
            raise ImmutableConflict(
                f"op_paper_fill_requests immutable conflict: request_id={request.request_id!r}"
            )
    return request.request_id


def append_paper_fill_receipt(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    status: str,
    now_iso: str,
    unfilled_reason: str | None = None,
    economics: dict[str, object] | None = None,
    quote_started_at: str | None = None,
    quote_completed_at: str | None = None,
) -> int:
    """Append the one terminal receipt for a request. Returns its `receipt_seq`."""
    if status not in {"filled", "entry_unfilled"}:
        raise ValueError(f"unknown receipt status: {status!r}")
    if status == "filled":
        if unfilled_reason is not None:
            raise ValueError("a filled receipt cannot carry an unfilled reason")
    elif unfilled_reason not in UNFILLED_REASONS:
        raise ValueError(f"unknown unfilled reason: {unfilled_reason!r}")

    economics = dict(economics or {})
    if status == "filled":
        # Both legs score the floor or neither does. A filled receipt without a floor
        # would silently fall back to the expected amount and invent a spread.
        for required in ("expected_out_atomic", "floor_out_atomic"):
            value = economics.get(required)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(
                    f"a filled receipt requires a positive {required}; got {value!r}"
                )
        if int(economics["floor_out_atomic"]) > int(economics["expected_out_atomic"]):
            raise ValueError("floor_out_atomic cannot exceed expected_out_atomic")
        for required in ("quote_provider", "quote_in_asset", "quote_out_asset"):
            value = economics.get(required)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"a filled receipt requires a non-empty {required}")
        value = economics.get("quote_in_atomic")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("a filled receipt requires a positive quote_in_atomic")
        for required in ("quote_in_decimals", "quote_out_decimals"):
            value = economics.get(required)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 30
            ):
                raise ValueError(f"a filled receipt requires {required} between 0 and 30")
        for required in ("route_binding", "quote_payload"):
            value = economics.get(required)
            if not isinstance(value, dict) or not value:
                raise ValueError(f"a filled receipt requires a non-empty {required}")
        slippage = economics.get("slippage_bps")
        if (
            not isinstance(slippage, int)
            or isinstance(slippage, bool)
            or slippage < 0
        ):
            raise ValueError("a filled receipt requires a non-negative slippage_bps")
        if quote_started_at is None or quote_completed_at is None:
            raise ValueError("a filled receipt requires quote_started_at and quote_completed_at")

    receipt_at = require_utc_iso(now_iso)
    request = conn.execute(
        "SELECT intent_at, decision_at FROM op_paper_fill_requests WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if request is None:
        raise ValueError(f"no paper fill request for receipt: {request_id!r}")
    if status == "filled":
        started = _parse(quote_started_at)
        completed = _parse(quote_completed_at)
        if started < _parse(request["intent_at"]):
            raise ValueError("quote_started_at cannot precede request intent_at")
        if completed < started:
            raise ValueError("quote_completed_at cannot precede quote_started_at")
        if _parse(receipt_at) < completed:
            raise ValueError("receipt_at cannot precede quote_completed_at")

    payload = economics.get("quote_payload")
    values = {
        "request_id": request_id,
        "status": status,
        "unfilled_reason": unfilled_reason,
        "quote_provider": economics.get("quote_provider"),
        "route_binding_json": (
            json.dumps(economics["route_binding"], sort_keys=True)
            if economics.get("route_binding") is not None else None
        ),
        "quote_in_asset": economics.get("quote_in_asset"),
        "quote_in_atomic": economics.get("quote_in_atomic"),
        "quote_in_decimals": economics.get("quote_in_decimals"),
        "quote_out_asset": economics.get("quote_out_asset"),
        "quote_out_decimals": economics.get("quote_out_decimals"),
        "expected_out_atomic": economics.get("expected_out_atomic"),
        "floor_out_atomic": economics.get("floor_out_atomic"),
        "slippage_bps": economics.get("slippage_bps"),
        "quote_payload_json": (
            json.dumps(payload, sort_keys=True) if payload is not None else None
        ),
        "quote_fingerprint": (
            content_fingerprint(payload) if isinstance(payload, dict) else None
        ),
        "quote_started_at": (
            require_utc_iso(quote_started_at) if quote_started_at else None
        ),
        "quote_completed_at": (
            require_utc_iso(quote_completed_at) if quote_completed_at else None
        ),
        "receipt_at": receipt_at,
        "queue_delay_ms": _elapsed_ms(quote_started_at, request["intent_at"]),
        "quote_latency_ms": _elapsed_ms(quote_completed_at, quote_started_at),
        "receipt_latency_ms": _elapsed_ms(receipt_at, request["decision_at"]),
    }
    stored_values = {
        **values,
        "quote_in_atomic": _sqlite_atomic(values["quote_in_atomic"]),
        "expected_out_atomic": _sqlite_atomic(values["expected_out_atomic"]),
        "floor_out_atomic": _sqlite_atomic(values["floor_out_atomic"]),
    }
    conn.execute(
        "INSERT INTO op_paper_fills "
        f"({','.join(values)},row_fingerprint,inserted_at) "
        f"VALUES ({','.join('?' for _ in values)},?,?)",
        (*stored_values.values(), content_fingerprint(values), receipt_at),
    )
    return int(
        conn.execute(
            "SELECT receipt_seq FROM op_paper_fills WHERE request_id=?", (request_id,)
        ).fetchone()[0]
    )


def pending_requests(conn: sqlite3.Connection, *, limit: int = 64) -> tuple[sqlite3.Row, ...]:
    """Requests with no terminal receipt yet, oldest intent first."""
    return tuple(conn.execute(
        "SELECT r.* FROM op_paper_fill_requests r "
        "LEFT JOIN op_paper_fills f ON f.request_id=r.request_id "
        "WHERE f.receipt_seq IS NULL ORDER BY r.intent_at, r.request_id LIMIT ?",
        (int(limit),),
    ).fetchall())


def expired_requests(conn: sqlite3.Connection, *, now_iso: str) -> tuple[sqlite3.Row, ...]:
    """Requests past their expiry with no terminal receipt. Derived, never stored."""
    return tuple(conn.execute(
        "SELECT r.* FROM op_paper_fill_requests r "
        "LEFT JOIN op_paper_fills f ON f.request_id=r.request_id "
        "WHERE f.receipt_seq IS NULL AND r.expires_at <= ? "
        "ORDER BY r.intent_at, r.request_id",
        (require_utc_iso(now_iso),),
    ).fetchall())


def sweep_expired_requests(conn: sqlite3.Connection, *, now_iso: str) -> int:
    """Append `capacity_expired` receipts for every expired request.

    Appends; never updates. Idempotent because the receipt itself removes the request
    from `expired_requests` on the next pass.
    """
    swept = 0
    for row in expired_requests(conn, now_iso=now_iso):
        append_paper_fill_receipt(
            conn,
            request_id=str(row["request_id"]),
            status="entry_unfilled",
            unfilled_reason="capacity_expired",
            now_iso=now_iso,
        )
        swept += 1
    return swept


class QuoteRejected(RuntimeError):
    """A quote attempt failed with a typed, terminal reason."""

    def __init__(self, reason: str):
        if reason not in UNFILLED_REASONS:
            raise ValueError(f"unknown unfilled reason: {reason!r}")
        super().__init__(reason)
        self.reason = reason


class QuoteDeferred(RuntimeError):
    """The bounded probe accepted or retained work that is not complete yet."""


def drive_paper_adapter(
    conn: sqlite3.Connection,
    *,
    quote_fn,
    now_iso: str,
    limit: int = 16,
) -> dict[str, int]:
    """Resolve pending requests into terminal receipts. Returns per-outcome counters.

    `quote_fn(request) -> dict` must return a mapping carrying at least
    `expected_out_atomic` and `floor_out_atomic`, and raise `QuoteRejected` with a
    typed reason otherwise. It is expected to be backed by the existing
    `OpportunitySolanaProbe` worker at lower priority than capture, so this function
    never opens its own transport and never blocks capture.

    A receipt is Radar's own entry-realism evidence and does not depend on any reader.
    An earlier version refused to quote unless the V6 consumer heartbeat was fresh, so
    stopping V6 silently ended Radar's measurement. V6 defends its own ledger: it admits
    a receipt exactly once and censors a late one as `censored_handoff_stale`.
    """
    counters: dict[str, int] = {}

    def _count(key: str) -> None:
        counters[key] = counters.get(key, 0) + 1

    for row in pending_requests(conn, limit=limit):
        request_id = str(row["request_id"])
        if row["expires_at"] <= require_utc_iso(now_iso):
            append_paper_fill_receipt(
                conn, request_id=request_id, status="entry_unfilled",
                unfilled_reason="capacity_expired", now_iso=now_iso,
            )
            _count("capacity_expired")
            continue
        try:
            economics = dict(quote_fn(row))
        except QuoteDeferred:
            continue
        except QuoteRejected as rejected:
            append_paper_fill_receipt(
                conn, request_id=request_id, status="entry_unfilled",
                unfilled_reason=rejected.reason, now_iso=now_iso,
            )
            _count(rejected.reason)
            continue
        append_paper_fill_receipt(
            conn,
            request_id=request_id,
            status="filled",
            now_iso=now_iso,
            economics=economics,
            quote_started_at=economics.pop("quote_started_at", None),
            quote_completed_at=economics.pop("quote_completed_at", None),
        )
        _count("filled")
    return counters
