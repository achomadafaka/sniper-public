"""One mark cycle across both venues.

Cost is O(1) HTTP calls, not O(positions): every open PM token id goes in a single
`clob POST /prices`, and HL `allMids` returns every mid per call. A full cycle is
about three requests regardless of book size — that is what makes a */5 cron
affordable next to the existing per-minute watchdogs.

Spec: docs/superpowers/specs/2026-07-29-intel-paper-testing-design.md
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone

from sniper.markets.hl_universe import all_mids
from sniper.markets.pm_resolver import fetch_markets
from sniper.persistence import intel_paper

logger = logging.getLogger(__name__)

CLOB_PRICES_URL = "https://clob.polymarket.com/prices"


def pnl_pct(direction: str, entry: float, current: float) -> float:
    """Signed PnL %. PM buy_* and HL long move with price; short moves against it.

    PM prices are outcome-native (ADR-003), so a buy_no position's entry and current
    are both NO prices and the arithmetic is the same as a long.
    """
    entry = float(entry or 0.0)
    if entry <= 0:
        return 0.0
    raw = (float(current) - entry) / entry * 100.0
    return -raw if str(direction).lower() == "short" else raw


def _clob_bids(token_ids: list[str], timeout: float = 10.0) -> dict[str, float]:
    """Best bid per token in ONE request.

    Marking a long at the bid is deliberately conservative — it is what you could
    actually sell into. This is not an execution-cost model; fees, depth and slippage
    are still unmodelled.
    """
    if not token_ids:
        return {}
    body = json.dumps([{"token_id": t, "side": "BUY"} for t in token_ids]).encode()
    req = urllib.request.Request(
        CLOB_PRICES_URL, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "sniper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        logger.warning("clob prices failed: %s", exc)
        return {}
    out: dict[str, float] = {}
    for tok, sides in (raw or {}).items():
        try:
            out[tok] = float(sides["BUY"])
        except (TypeError, ValueError, KeyError):
            continue
    return out


def mark_cycle(conn, timeout: float = 10.0) -> dict:
    """Mark every open position. Returns counts written per venue.

    A missing price is skipped and counted, never written as a fabricated mark —
    a gap in the series is honest, an invented price is not.
    """
    now = datetime.now(timezone.utc).isoformat()
    positions = intel_paper.open_positions(conn)
    written = {"pm": 0, "hl": 0, "closed": 0, "errors": 0}

    pm = [p for p in positions if p["venue"] == "pm" and p["token_id"]]

    # Resolution check: ONE batched Gamma call for every open PM market. A position
    # closes on resolution or manually — there is no time-based rule, and HL perps
    # never resolve so they are manual-close only.
    resolved = fetch_markets([p["instrument"] for p in pm], timeout) if pm else {}
    still_open = []
    for p in pm:
        market = resolved.get(p["instrument"])
        if market and market.get("closed"):
            side = "NO" if str(p["direction"]) == "buy_no" else "YES"
            px = market["prices"].get(side)
            if px is not None:
                intel_paper.close_position(conn, p["position_id"], px, now, "resolved")
                written["closed"] += 1
                continue
        still_open.append(p)
    pm = still_open
    prices = _clob_bids([p["token_id"] for p in pm], timeout) if pm else {}
    for p in pm:
        px = prices.get(p["token_id"])
        if px is None:
            written["errors"] += 1
            continue
        intel_paper.record_mark(conn, p["position_id"], now, px,
                                pnl_pct(p["direction"], p["entry_price"], px))
        written["pm"] += 1

    hl = [p for p in positions if p["venue"] == "hl"]
    mids = all_mids({p["instrument"] for p in hl}, timeout) if hl else {}
    for p in hl:
        px = mids.get(p["instrument"])
        if px is None:
            written["errors"] += 1
            continue
        intel_paper.record_mark(conn, p["position_id"], now, px,
                                pnl_pct(p["direction"], p["entry_price"], px))
        written["hl"] += 1

    return written
