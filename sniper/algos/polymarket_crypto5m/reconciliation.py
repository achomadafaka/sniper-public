"""Automatic post-resolution reconciliation.

A capture ending near market close stores ``winner=None`` / ``priceToBeat=None`` because both become
available later. This worker revisits closed-but-unresolved windows until Gamma (and/or the WS) yields
the winning token, outcome, UMA status, post-hoc Gamma ``priceToBeat`` and resolution ts. Idempotent
resume. The decision logic is pure (``now`` injected); the fetch is injected so it's testable offline.
"""
from __future__ import annotations

import enum

from . import market_resolver as MR
from . import settlement as S


class ReconcileStatus(str, enum.Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    TIMED_OUT = "timed_out"
    CONFLICT = "conflict"


def needs_reconcile(*, window_end_ts: int, now: int, has_resolution: bool) -> bool:
    """A window needs reconciliation once it has closed and is not yet resolved."""
    return now >= window_end_ts and not has_resolution


def next_status(*, has_resolution: bool, conflict: bool, now: int, window_end_ts: int,
                timeout_s: int) -> ReconcileStatus:
    if conflict:
        return ReconcileStatus.CONFLICT
    if has_resolution:
        return ReconcileStatus.RESOLVED
    if now - window_end_ts > timeout_s:
        return ReconcileStatus.TIMED_OUT
    return ReconcileStatus.PENDING


def reconcile_market(fetch, spec, *, ws_settlement=None):
    """Re-fetch a window's Gamma event and build a settlement. ``fetch(url)->json`` is injected. When a
    ``ws_settlement`` (from a ``market_resolved`` WS event) is supplied it is reconciled against Gamma
    via ``settlement.reconcile`` — so a gamma-vs-ws disagreement surfaces as a quarantined ``conflict``.
    Returns the reconciled ``Settlement`` (possibly unresolved/conflict) or ``None`` if the event
    could not be fetched/parsed."""
    events = fetch(f"{MR.GAMMA_EVENTS_URL}?slug={spec.slug}")
    if not events:
        return None
    rspec, verdict = MR.parse_event(events[0])
    if rspec is None:
        return None
    gamma = S.from_gamma(rspec)
    if ws_settlement is not None:
        return S.reconcile(gamma, ws_settlement)
    return gamma


def status_for(settlement, *, now, window_end_ts, timeout_s) -> ReconcileStatus:
    """Map a (possibly None) reconciled ``Settlement`` to a ``ReconcileStatus``."""
    conflict = bool(settlement) and settlement.reconciliation_status == "conflict"
    has_resolution = bool(settlement) and settlement.winning_outcome is not None and not conflict
    return next_status(has_resolution=has_resolution, conflict=conflict, now=now,
                       window_end_ts=window_end_ts, timeout_s=timeout_s)
