"""Replay adapter abstractions for strategy back-testing."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import db


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class ReplayLifecycleState(str, Enum):
    """Possible states of a replayed position at a given point in time."""

    ACTIVE = "active"   # position still open
    WON = "won"         # resolved as win
    LOST = "lost"       # resolved as loss
    EXITED = "exited"   # closed before resolution


@dataclass(slots=True)
class ReplayCandidate:
    """A historical entry candidate for replay."""

    condition_id: str
    outcome: str
    wallet: str
    entry_price: float
    entry_ts: int
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract adapter
# ---------------------------------------------------------------------------

class StrategyReplayAdapter(ABC):
    """Interface that strategy plugins expose for replay/back-testing.

    Each concrete adapter knows how to:
    1. Find historical entry candidates from the DB.
    2. Determine what state a position was in at any historical timestamp.
    3. Provide an exit price at any historical timestamp.
    """

    strategy_type: str = ""

    @abstractmethod
    def get_candidates(
        self,
        conn,
        config: dict,
        start_ts: int,
        end_ts: int,
    ) -> List[ReplayCandidate]:
        """Fetch historical entry candidates within the given time window.

        Args:
            conn: sqlite3.Connection
            config: strategy configuration dict
            start_ts: epoch start of the replay window (inclusive)
            end_ts: epoch end of the replay window (inclusive)

        Returns:
            List of ReplayCandidate objects.
        """
        raise NotImplementedError

    @abstractmethod
    def get_lifecycle_state(
        self,
        conn,
        candidate: ReplayCandidate,
        as_of_ts: int,
    ) -> ReplayLifecycleState:
        """Determine the lifecycle state of a candidate at a point in time.

        Args:
            conn: sqlite3.Connection
            candidate: the entry candidate
            as_of_ts: epoch timestamp to evaluate state at

        Returns:
            The lifecycle state at the given timestamp.
        """
        raise NotImplementedError

    @abstractmethod
    def get_exit_price(
        self,
        conn,
        candidate: ReplayCandidate,
        as_of_ts: int,
    ) -> float:
        """Get the exit price for a candidate at a point in time.

        Args:
            conn: sqlite3.Connection
            candidate: the entry candidate
            as_of_ts: epoch timestamp to get the price at

        Returns:
            The exit price (YES price, in [0.0, 1.0]).
        """
        raise NotImplementedError


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


class InsiderCopyReplayAdapter(StrategyReplayAdapter):
    """Replay adapter for insider-copy style strategies."""
    strategy_type = "insider_copy_v1"

    def get_candidates(
        self,
        conn,
        config: dict,
        start_ts: int,
        end_ts: int,
    ) -> List[ReplayCandidate]:
        rows = db.replay_curated_signals(
            conn,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        candidates: List[ReplayCandidate] = []
        for row in rows:
            candidates.append(
                ReplayCandidate(
                    condition_id=str(row.get("condition_id") or ""),
                    outcome=str(row.get("outcome") or ""),
                    wallet=str(row.get("wallet") or ""),
                    entry_price=_to_float(row.get("entry_price")),
                    entry_ts=int(row.get("entry_anchor_ts") or 0),
                    metadata={
                        "title": str(row.get("title") or ""),
                        "entry_notional": _to_float(row.get("entry_notional")),
                        "position_insider_score": _to_float(row.get("position_insider_score")),
                        "position_speculator_score": _to_float(row.get("position_speculator_score")),
                        "position_classification": str(row.get("position_classification") or ""),
                        "is_open": int(row.get("is_open") or 0),
                        "resolved_price": row.get("resolved_price"),
                        "resolved_at": int(row.get("resolved_at") or 0),
                    },
                )
            )
        return candidates

    def get_lifecycle_state(
        self,
        conn,
        candidate: ReplayCandidate,
        as_of_ts: int,
    ) -> ReplayLifecycleState:
        resolved_at = int(candidate.metadata.get("resolved_at") or 0)
        resolved_price = candidate.metadata.get("resolved_price")
        if resolved_at <= 0 or as_of_ts < resolved_at or resolved_price is None:
            return ReplayLifecycleState.ACTIVE
        if _to_float(resolved_price) >= 0.999:
            return ReplayLifecycleState.WON
        if _to_float(resolved_price) <= 0.001:
            return ReplayLifecycleState.LOST
        return ReplayLifecycleState.EXITED

    def get_exit_price(
        self,
        conn,
        candidate: ReplayCandidate,
        as_of_ts: int,
    ) -> float:
        return _to_float(candidate.metadata.get("resolved_price"))


def get_replay_adapter_for_strategy(strategy_type: str) -> Optional[StrategyReplayAdapter]:
    """Resolve the replay adapter for a strategy type via the plugin registry."""
    from sniper.strategies.registry import get_strategy_plugin

    plugin = get_strategy_plugin(strategy_type)
    if plugin is None:
        return None
    return plugin.replay_adapter()
