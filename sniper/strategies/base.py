from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

# Resolution price thresholds — shared by all strategy plugins.
# Override via env vars (same names as paper_settings.py uses).
RESOLUTION_WIN: float = float(os.getenv("RESOLUTION_WIN_THRESHOLD", "0.97"))
RESOLUTION_LOSS: float = float(os.getenv("RESOLUTION_LOSS_THRESHOLD", "0.03"))

if TYPE_CHECKING:
    from sniper.replay.adapters import StrategyReplayAdapter


@dataclass(slots=True)
class StrategySignal:
    wallet: str
    condition_id: str
    outcome: str
    title: str
    current_price: float
    position_value: float
    analyzed_ts: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StrategyEntryDecision:
    should_enter: bool
    reason: str
    size_usdc: float = 0.0
    entry_price: float = 0.0
    execution_mode: str = "paper"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StrategyManageState:
    current_price: float | None = None
    signal_open: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StrategyExitDecision:
    should_exit: bool
    reason: str
    exit_price: float = 0.0
    status: str = "closed"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StrategyReport:
    summary: Dict[str, Any] = field(default_factory=dict)


class StrategyPlugin(ABC):
    plugin_id: str = ""
    display_name: str = ""
    description: str = ""

    # --- Chunk 4 extensions (all have defaults for backward compatibility) ---
    strategy_family: str = ""
    supported_execution_modes: Tuple[str, ...] = ("paper",)
    required_feeds: Tuple[str, ...] = ()
    required_tables: Tuple[str, ...] = ()
    default_loop_seconds: int = 60

    def validate_parameters(self, parameters: dict) -> List[str]:
        """Return a list of validation errors for the given parameters.

        Empty list means the parameters are valid.  Default implementation
        accepts anything.
        """
        return []

    def replay_adapter(self) -> Optional["StrategyReplayAdapter"]:
        """Return a replay adapter for this strategy, or None if replay
        is not supported.  Default returns None.
        """
        return None

    @abstractmethod
    def discover(self, conn, strategy: Dict[str, Any]) -> List[StrategySignal]:
        raise NotImplementedError

    @abstractmethod
    def enter(
        self,
        conn,
        strategy: Dict[str, Any],
        signal: StrategySignal,
        open_positions: List[Dict[str, Any]],
    ) -> StrategyEntryDecision:
        raise NotImplementedError

    @abstractmethod
    def manage(self, conn, strategy: Dict[str, Any], position: Dict[str, Any]) -> StrategyManageState:
        raise NotImplementedError

    @abstractmethod
    def exit(
        self,
        conn,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        raise NotImplementedError

    def report(self, conn, strategy: Dict[str, Any], positions: List[Dict[str, Any]]) -> StrategyReport:
        return StrategyReport()
