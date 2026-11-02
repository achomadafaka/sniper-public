from __future__ import annotations

"""Stink Bid V1 — Tennis Panic Dip Strategy Plugin.

This plugin is PASSIVE on entry.  Order placement is handled by
``stink_bid_runner.py``.  The plugin provides:

1. Tennis market identification helpers (module-level, used by the runner).
2. Position management (manage / exit) for positions already created by the
   paper limit engine on strict fill.

``discover()`` always returns ``[]`` — the runner owns discovery.
``enter()``    always returns ``should_enter=False`` — the runner places orders.
``manage()``   returns the current price from the position dict, signal_open=True.
``exit()``     holds to resolution: exits at >= 0.97 (win) or <= 0.03 (loss).
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .base import (
    RESOLUTION_LOSS,
    RESOLUTION_WIN,
    StrategyEntryDecision,
    StrategyExitDecision,
    StrategyManageState,
    StrategyPlugin,
    StrategySignal,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tennis identification helpers (module-level so the runner can import them)
# ---------------------------------------------------------------------------

# Legacy tennis-keyword regex — still exported for backward compat with callers
# that may want a stricter in-title check. Not used by _is_tennis_match_title.
_TENNIS_KEYWORDS_RE = re.compile(
    r"\b(wta|atp|itf|tennis|wimbledon|roland.garros|us.open|australian.open|"
    r"french.open|grand.slam|davis.cup|fed.cup|billie.jean)\b",
    re.IGNORECASE,
)

# "Surname vs Surname" — one or more words on each side of " vs "
_VS_PATTERN_RE = re.compile(
    r"\b(\w[\w\-']+(?:\s+\w[\w\-']+)?)\s+vs\.?\s+(\w[\w\-']+(?:\s+\w[\w\-']+)?)\b",
    re.IGNORECASE,
)


def _is_tennis_match_title(title: str) -> bool:
    """Return True if *title* has the H2H 'X vs Y' shape.

    Tennis tag verification is handled upstream in stink_bid_runner's Gamma
    seed pass (tag == 'tennis' on the event). This filter only validates the
    H2H shape of the title so _extract_player_surnames can succeed. Titles of
    the form "Tournament: X vs Y" are accepted by stripping the prefix before
    the colon.
    """
    if not title:
        return False
    core = title.split(":", 1)[-1].strip()
    return bool(_VS_PATTERN_RE.search(core))


def _extract_tour(title: str) -> str:
    """Return ``"wta"`` if the title contains a WTA reference, else ``"atp"``."""
    if re.search(r"\bwta\b", title, re.IGNORECASE):
        return "wta"
    return "atp"


def _extract_player_surnames(title: str) -> Optional[Tuple[str, str]]:
    """Extract the two player surnames from a "X vs Y" title.

    Returns a ``(surname1, surname2)`` tuple using the last token of each
    matched name, or ``None`` if no match is found.
    """
    m = _VS_PATTERN_RE.search(title)
    if not m:
        return None
    name1 = m.group(1).strip().split()[-1]
    name2 = m.group(2).strip().split()[-1]
    return (name1, name2)


# ---------------------------------------------------------------------------
# Strategy Plugin
# ---------------------------------------------------------------------------


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


class StinkBidV1Strategy(StrategyPlugin):
    """Passive tennis panic-dip strategy.

    Entry is fully delegated to ``stink_bid_runner.py`` which places paper
    limit orders.  This plugin only manages already-open positions created
    when those limit orders are filled.
    """

    plugin_id = "stink_bid_v1"
    display_name = "Stink Bid V1 — Tennis Panic Dip"
    description = (
        "Places low-ball limit orders on tennis YES markets during panic dips. "
        "Entry is handled by stink_bid_runner; this plugin manages open positions."
    )

    strategy_family = "value"
    supported_execution_modes = ("paper",)
    required_feeds = ("polymarket",)
    default_loop_seconds = 900

    # ------------------------------------------------------------------
    # Discovery — delegated to runner
    # ------------------------------------------------------------------

    def discover(
        self, conn, strategy: Dict[str, Any]
    ) -> List[StrategySignal]:
        """Always returns empty list.

        Discovery (scanning for tennis markets and placing limit orders) is
        the responsibility of ``stink_bid_runner.py``, not this plugin.
        """
        return []

    # ------------------------------------------------------------------
    # Entry — delegated to runner
    # ------------------------------------------------------------------

    def enter(
        self,
        conn,
        strategy: Dict[str, Any],
        signal: StrategySignal,
        open_positions: List[Dict[str, Any]],
    ) -> StrategyEntryDecision:
        """Always returns should_enter=False.

        Order placement is handled by the runner via the paper limit engine.
        The strategy runner must never call enter() for this plugin — but if
        it does, we return a safe no-op.
        """
        return StrategyEntryDecision(
            should_enter=False,
            reason="entry_delegated_to_runner",
        )

    # ------------------------------------------------------------------
    # Manage — read current price from position dict
    # ------------------------------------------------------------------

    def manage(
        self, conn, strategy: Dict[str, Any], position: Dict[str, Any]
    ) -> StrategyManageState:
        """Return current price from the position dict; signal always open.

        Stink bid positions hold to resolution.  There is no source wallet
        to track, so ``signal_open`` is always ``True`` to suppress the
        ``source_exited`` stop that ``insider_copy_v1`` uses.
        """
        current_price = _to_float(
            position.get("current_price")
            or position.get("avg_entry_price"),
            0.0,
        )
        return StrategyManageState(
            current_price=current_price,
            signal_open=True,
            metadata={},
        )

    # ------------------------------------------------------------------
    # Exit — hold to resolution only
    # ------------------------------------------------------------------

    def exit(
        self,
        conn,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        """Exit only at resolution.

        Exits when current_price >= RESOLUTION_WIN (0.97) or
        current_price <= RESOLUTION_LOSS (0.03).  All other prices hold.
        """
        current_price = _to_float(
            state.current_price,
            _to_float(position.get("avg_entry_price")),
        )

        if current_price >= RESOLUTION_WIN:
            return StrategyExitDecision(
                should_exit=True,
                reason="resolution_win",
                exit_price=current_price,
                status="closed",
            )

        if current_price <= RESOLUTION_LOSS:
            return StrategyExitDecision(
                should_exit=True,
                reason="resolution_loss",
                exit_price=current_price,
                status="closed",
            )

        return StrategyExitDecision(
            should_exit=False,
            reason="hold",
            exit_price=current_price,
            status="open",
        )
