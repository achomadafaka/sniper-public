"""Route an intel candidate to a venue and open a paper position.

Nothing here gates. A reviewer-rejected idea still opens, tagged `rejected`, because
if only approved ideas are papered the reviewer can never be evaluated — there is no
rejected cohort to compare against. Gating a signal before measuring its value is the
failure mode recorded in finding-crypto5m-floor-endogeneity-2026-07-07.

Spec: docs/superpowers/specs/2026-07-29-intel-paper-testing-design.md
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sniper.markets.hl_universe import all_mids, resolve_symbol
from sniper.markets.pm_resolver import fetch_market
from sniper.persistence import intel_paper

logger = logging.getLogger(__name__)

DEFAULT_SIZE_USDC = 10.0


def _verdict(idea: dict) -> tuple[str, Optional[int]]:
    """The reviewer verdict at entry, recorded as a tag rather than enforced."""
    approved = idea.get("adversarial_approved")
    if approved is None:
        label = "pending"
    elif int(approved):
        label = "approved"
    else:
        label = "rejected"
    return label, idea.get("adversarial_score")


def _conflict(conn, instrument: str, venue: str) -> dict:
    """Describe why the DB refused a second open position on this instrument.

    Reached when `open_position` inserts nothing. The one-open-position-per-instrument
    index is what actually blocks it — this only explains the refusal for the stats.
    """
    holder = next((p for p in intel_paper.open_positions(conn)
                   if p.get("instrument") == instrument), None)
    if holder is None:
        # No open row, so the block came from the per-idea unique index: this idea
        # already has a position and the run is simply idempotent.
        reason = "idea already has a position"
    else:
        reason = (f"already holding {holder.get('direction')} on this instrument "
                  f"(position #{holder.get('position_id')}) — a second position is "
                  f"double-sizing or self-excluding, not a new observation")
    logger.info("idea not opened on %s %s: %s", venue, instrument, reason)
    return {"disposition": "conflict", "position_id": None, "reason": reason}


def open_for_idea(conn, idea: dict[str, Any],
                  hl_universe_set: Optional[set[str]] = None) -> dict:
    """Open a paper position for one candidate. Never raises on an unopenable idea.

    Returns {"disposition": "pm"|"hl"|"no_instrument"|"conflict", "position_id",
    "reason"}. `no_instrument` is a first-class outcome, not a failure: if most theses
    cannot be expressed as a position, the constraint is market availability rather
    than analysis quality, and that is worth counting. `conflict` means we already
    hold this instrument.
    """
    now = datetime.now(timezone.utc).isoformat()
    verdict, score = _verdict(idea)
    common = dict(idea_id=idea["id"], reviewer_verdict=verdict,
                  reviewer_score=score, holder_snapshot=None,
                  entry_ts=now, size_usdc=DEFAULT_SIZE_USDC)

    cid = idea.get("condition_id")
    if cid:
        market = fetch_market(cid)
        if market:
            # ADR-003: store the price of the side actually held. A buy_no position
            # stores the NO token price, never the YES price.
            side = "NO" if str(idea.get("direction")) == "buy_no" else "YES"
            token = market["token_ids"].get(side)
            price = market["prices"].get(side)
            if token and price is not None:
                # negRisk markets are still binary (verified live: 2 outcomes, 2
                # tokens, YES+NO == 1.0000), so no special pricing. The flag is
                # recorded because sibling outcomes of a negRisk event are
                # mechanically linked and must not be counted as independent.
                pid = intel_paper.open_position(
                    conn, venue="pm", instrument=cid, token_id=token,
                    event_title=market.get("event_title"),
                    outcome_label=market.get("outcome_label"),
                    neg_risk=1 if market.get("neg_risk") else 0,
                    direction=idea["direction"], entry_price=price, **common)
                if pid is None:
                    return _conflict(conn, cid, "pm")
                return {"disposition": "pm", "position_id": pid, "reason": None}

    sym = idea.get("hl_symbol")
    if sym and hl_universe_set:
        resolved = resolve_symbol(sym, hl_universe_set)
        if resolved:
            price = all_mids({resolved}).get(resolved)
            if price is not None:
                pid = intel_paper.open_position(
                    conn, venue="hl", instrument=resolved, token_id=None,
                    event_title=None, outcome_label=None,
                    direction=idea.get("direction") or "long",
                    neg_risk=0, entry_price=price, **common)
                if pid is None:
                    return _conflict(conn, resolved, "hl")
                return {"disposition": "hl", "position_id": pid, "reason": None}

    logger.info("idea %s: no tradeable instrument", idea.get("id"))
    return {"disposition": "no_instrument", "position_id": None,
            "reason": "no tradeable instrument on either venue"}
