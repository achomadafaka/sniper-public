"""Shared open-position context for the two LLM agents in the intel loop.

Both agents reason better knowing what the book already holds. The reviewer can
spot a correlated re-entry it would otherwise wave through; the idea creator can
notice that today's news cuts against a position it opened two days ago. One
formatter feeds both, so the two never disagree about what we own.

Sibling outcomes of a single event are mechanically linked, so N rows are not N
independent bets (finding-polymarket-sibling-market-independence-2026-07-29). The
block says so inline rather than leaving a model to miscount its own exposure.

Spec: docs/superpowers/specs/2026-07-29-intel-paper-testing-design.md
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from sniper.persistence import intel_paper

logger = logging.getLogger(__name__)

MAX_POSITIONS = 25


def _age_days(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds() / 86400.0


def _line(p: dict) -> str:
    label = p.get("event_title") or p.get("instrument") or "?"
    outcome = p.get("outcome_label")
    if outcome and outcome not in label:
        label = f"{label} — {outcome}"

    entry = p.get("entry_price")
    mark = p.get("last_mark_price")
    pnl = p.get("last_pnl_pct")
    # An unmarked position reports as unmarked. Substituting entry for mark would
    # render a 0% move that never happened.
    if mark is None:
        px = f"@ {entry} (no mark yet)"
    else:
        px = f"@ {entry} -> {mark}" + (f" ({pnl:+.1f}%)" if pnl is not None else "")

    age = _age_days(p.get("entry_ts"))
    bits = [
        f"#{p.get('position_id')}",
        str(p.get("venue", "")).upper(),
        label,
        f"{p.get('direction')} {px}",
    ]
    if age is not None:
        bits.append(f"held {age:.1f}d")
    bits.append(f"reviewer:{p.get('reviewer_verdict') or 'pending'}")

    siblings = p.get("event_position_count") or 1
    if siblings > 1:
        bits.append(f"{siblings} open on this event — correlated, not independent")

    prior = p.get("last_recommendation")
    if prior:
        bits.append(f"prior rec:{prior}")

    return " | ".join(b for b in bits if b)


def format_positions_block(conn, limit: int = MAX_POSITIONS,
                           exclude_idea_id: Optional[int] = None) -> str:
    """Render open paper positions as prompt text. Empty string when nothing is open.

    `exclude_idea_id` drops the position that this idea itself opened. Reviews can run
    after the position exists, so without it the reviewer is shown the consequence of
    the very idea under review and reliably calls it "a duplicate of existing exposure"
    — rejecting the idea for being itself. Always pass the idea being reviewed.

    Never raises: an agent that cannot see the book must still run. A failure here
    degrades context, it does not fail the intel run.
    """
    try:
        rows = intel_paper.open_positions(conn)
    except Exception:  # noqa: BLE001
        logger.exception("could not load open positions for agent context")
        return ""

    if exclude_idea_id is not None:
        rows = [r for r in rows if r.get("idea_id") != exclude_idea_id]

    if not rows:
        return ""

    lines = [_line(p) for p in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"[...{len(rows) - limit} more open positions not shown]")
    return "\n".join(lines)
