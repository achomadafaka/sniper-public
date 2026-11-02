"""Continuous window-lifecycle manager (pure state machine).

Tracks current/next/recently-closed BTC 5m windows simultaneously so a long-running collector can
roll over without a per-boundary restart gap. The driver wraps this; the state transitions are pure
and deterministic (``now`` is injected — never the local clock). Dynamic subscribe/unsubscribe is
derived from ``active_tokens()`` so closed-but-unresolved windows keep their subscription until
settlement/reconciliation.
"""
from __future__ import annotations

import enum
from typing import Optional


class WindowState(str, enum.Enum):
    DISCOVERED = "discovered"
    CAPTURING = "capturing"
    AWAITING_RESOLUTION = "awaiting_resolution"
    RECONCILED = "reconciled"          # terminal (success)
    EXPIRED = "expired"                # terminal (settlement timeout)


_TERMINAL = (WindowState.RECONCILED, WindowState.EXPIRED)
_LIVE = (WindowState.CAPTURING, WindowState.AWAITING_RESOLUTION)


class _Entry:
    __slots__ = ("spec", "state", "discovered_ts")

    def __init__(self, spec, state, discovered_ts):
        self.spec = spec
        self.state = state
        self.discovered_ts = discovered_ts


class WindowLifecycle:
    def __init__(self, *, settlement_timeout_s: int = 600, max_windows: Optional[int] = None):
        self.settlement_timeout_s = settlement_timeout_s
        self.max_windows = max_windows
        self._w: dict = {}   # condition_id -> _Entry

    # --- mutations ---------------------------------------------------------------------------

    def discover(self, spec, *, now: int) -> bool:
        """Register a newly-discovered window. Returns False if already known or at ``max_windows``."""
        if spec.condition_id in self._w:
            return False
        if self.max_windows is not None and self.count() >= self.max_windows:
            return False
        self._w[spec.condition_id] = _Entry(spec, WindowState.DISCOVERED, now)
        return True

    def start_capture(self, condition_id: str) -> None:
        e = self._w.get(condition_id)
        if e and e.state is WindowState.DISCOVERED:
            e.state = WindowState.CAPTURING

    def mark_reconciled(self, condition_id: str) -> None:
        e = self._w.get(condition_id)
        if e and e.state is not WindowState.EXPIRED:
            e.state = WindowState.RECONCILED

    def tick(self, *, now: int) -> None:
        """Advance time-driven transitions: a DISCOVERED window whose window already ended without
        ever being captured is abandoned (EXPIRED) so it can't leak / hold a max_windows slot;
        capturing->awaiting at window_end; awaiting->expired after the settlement timeout."""
        for e in self._w.values():
            we = e.spec.window_end_ts
            if e.state is WindowState.DISCOVERED and now >= we:
                e.state = WindowState.EXPIRED            # too late to capture -> abandon, don't leak
            if e.state is WindowState.CAPTURING and now >= we:
                e.state = WindowState.AWAITING_RESOLUTION
            if e.state is WindowState.AWAITING_RESOLUTION and now - we > self.settlement_timeout_s:
                e.state = WindowState.EXPIRED

    # --- reads -------------------------------------------------------------------------------

    def state(self, condition_id: str) -> Optional[WindowState]:
        e = self._w.get(condition_id)
        return e.state if e else None

    def count(self) -> int:
        return len(self._w)

    def discovered_windows(self) -> list:
        """Windows registered but not yet capturing — the driver starts these (the deadlock fix)."""
        return [e.spec for e in self._w.values() if e.state is WindowState.DISCOVERED]

    def live_windows(self) -> list:
        return [e.spec for e in self._w.values() if e.state in _LIVE]

    def awaiting_windows(self) -> list:
        return [e.spec for e in self._w.values() if e.state is WindowState.AWAITING_RESOLUTION]

    def active_tokens(self) -> list:
        """Token ids that should currently be subscribed (capturing + awaiting-resolution windows)."""
        toks = []
        for spec in self.live_windows():
            toks.extend([spec.up_token_id, spec.down_token_id])
        return toks

    def to_subscribe(self, currently_subscribed: set) -> list:
        return [t for t in self.active_tokens() if t not in currently_subscribed]

    def to_unsubscribe(self, currently_subscribed: set) -> list:
        active = set(self.active_tokens())
        return [t for t in currently_subscribed if t not in active]

    def prune_terminal(self) -> int:
        """Drop terminal (reconciled/expired) windows; returns how many were removed."""
        dead = [cid for cid, e in self._w.items() if e.state in _TERMINAL]
        for cid in dead:
            del self._w[cid]
        return len(dead)
