"""Resolve a Polymarket conditionId to a CLOB asset id and snapshot its price.

Two jobs, both preconditions for a *measurable* candidate:

1. ``resolve_token`` — a candidate that cannot be resolved to a real asset id is
   not a candidate. The audit found 11/11 research rows carrying a market *title*
   with ``polymarket_condition_id = NULL``, and 5/5 Hyperliquid rows naming
   instruments absent from HL's universe (F-01). Resolution is the validation:
   if the venue does not know the market, there is nothing to trade.

2. ``snapshot_prices`` — price at event time vs price at decision time. Their
   ratio against the eventual move IS the "already priced" measurement, which
   the system has never computed (audit §8). Two lookups, and they decide
   whether this whole program is viable (Gate A).

Price history comes from the existing wallet-protocol adapter — this module does
not open its own history connection. That adapter's contract is important: it is
keyed on the **CLOB asset id, not the conditionId**; the endpoint silently
returns an empty history for conditionIds. ``resolve_token`` exists to produce
that asset id.

Plan: docs/superpowers/plans/2026-07-29-intel-geopolitics-revival.md (Task 8)
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

CLOB_MARKET_URL = "https://clob.polymarket.com/markets/{condition_id}"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


def _as_list(raw):
    """Gamma returns outcomes / prices / token ids as JSON-encoded strings."""
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []


def _parse_market(m: dict, condition_id: str) -> Optional[dict]:
    """Normalise one Gamma market row. None when it is not a binary market."""
    outcomes = [str(o).upper() for o in _as_list(m.get("outcomes"))]
    tokens = [str(t) for t in _as_list(m.get("clobTokenIds"))]
    prices = _as_list(m.get("outcomePrices"))
    if len(outcomes) != 2 or len(tokens) != 2 or len(prices) != 2:
        logger.info("gamma market %s is not binary", condition_id)
        return None

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    events = m.get("events") or []
    return {
        "condition_id": condition_id,
        "question": m.get("question") or "",
        "event_title": (events[0].get("title") if events else None),
        "outcome_label": m.get("groupItemTitle"),
        "token_ids": dict(zip(outcomes, tokens)),
        "prices": {o: _f(p) for o, p in zip(outcomes, prices)},
        "best_bid": _f(m.get("bestBid")),
        "best_ask": _f(m.get("bestAsk")),
        "end_date_iso": m.get("endDateIso"),
        "neg_risk": bool(m.get("negRisk")),
        "closed": bool(m.get("closed")),
    }


def fetch_markets(condition_ids: list[str], timeout: float = 15.0) -> dict:
    """Batch lookup keyed by condition_id — ONE request for every id.

    Gamma wants REPEATED `condition_ids` params; a comma-joined value silently
    returns zero rows. This keeps resolution detection O(1) calls rather than
    O(positions). Closed markets ARE returned here (unlike fetch_market, which
    refuses them) because detecting closure is the whole point.
    """
    ids = [c for c in (condition_ids or []) if c]
    if not ids:
        return {}
    query = "&".join(f"condition_ids={urllib.parse.quote(str(c))}" for c in ids)
    try:
        req = urllib.request.Request(f"{GAMMA_MARKETS_URL}?{query}",
                                     headers={"User-Agent": "sniper/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rows = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_markets failed for %d ids: %s", len(ids), exc)
        return {}

    out: dict[str, dict] = {}
    for i, m in enumerate(rows or []):
        cid = m.get("conditionId") or m.get("condition_id") or (ids[i] if i < len(ids) else None)
        parsed = _parse_market(m, str(cid))
        if parsed:
            out[str(cid)] = parsed
    return out


def fetch_market(condition_id: str, timeout: float = 10.0) -> Optional[dict]:
    """One-shot lookup: metadata AND both CLOB token ids in a single request.

    Gamma returns `clobTokenIds` alongside the metadata, so opening a position needs
    no separate CLOB call. Also carries the labels a multi-outcome row needs to render:
    an event holds N binary markets, so "Election 2028 -> YES" is meaningless without
    `groupItemTitle` ("JD Vance").

    Returns None when the market cannot be resolved, is not binary, or is closed.
    """
    if not condition_id:
        return None
    url = f"{GAMMA_MARKETS_URL}?" + urllib.parse.urlencode({"condition_ids": condition_id})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sniper/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rows = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 — unresolvable is a normal outcome
        logger.info("fetch_market: %s unresolvable: %s", condition_id, exc)
        return None

    if not rows:
        return None
    m = rows[0]
    if m.get("closed"):
        return None

    outcomes = [str(o).upper() for o in _as_list(m.get("outcomes"))]
    tokens = [str(t) for t in _as_list(m.get("clobTokenIds"))]
    prices = _as_list(m.get("outcomePrices"))
    if len(outcomes) != 2 or len(tokens) != 2 or len(prices) != 2:
        logger.info("fetch_market: %s is not a binary market", condition_id)
        return None

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    events = m.get("events") or []
    return {
        "condition_id": condition_id,
        "question": m.get("question") or "",
        "event_title": (events[0].get("title") if events else None),
        "outcome_label": m.get("groupItemTitle"),
        "token_ids": dict(zip(outcomes, tokens)),
        "prices": {o: _f(p) for o, p in zip(outcomes, prices)},
        "best_bid": _f(m.get("bestBid")),
        "best_ask": _f(m.get("bestAsk")),
        "end_date_iso": m.get("endDateIso"),
        "neg_risk": bool(m.get("negRisk")),
        "closed": bool(m.get("closed")),
    }


def resolve_token(condition_id: str, outcome: str, timeout: float = 5.0) -> Optional[str]:
    """Return the CLOB asset id for `outcome` on `condition_id`, or None.

    None is a normal, expected result — an unresolvable market is recorded as a
    rejection upstream, never silently skipped.
    """
    want = str(outcome or "").strip().upper()
    if not condition_id or want not in ("YES", "NO"):
        return None

    req = urllib.request.Request(
        CLOB_MARKET_URL.format(condition_id=condition_id),
        headers={"User-Agent": "sniper/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 — unresolvable market is a normal outcome
        logger.info("resolve_token: %s unresolvable: %s", condition_id, exc)
        return None

    for tok in data.get("tokens") or []:
        if str(tok.get("outcome", "")).strip().upper() == want:
            token_id = str(tok.get("token_id") or "").strip()
            return token_id or None
    return None


def snapshot_prices(
    token_id: str,
    event_ts: int,
    decision_ts: int,
    price_fn: Optional[Callable[[str, int], Optional[float]]] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """Return (price_at_event, price_at_decision) for a CLOB asset id.

    Either element may be None when the endpoint has no bar at that timestamp;
    callers decide what a missing decision price means (it is disqualifying).
    """
    if price_fn is None:
        from sniper.analysis.wallet_protocol.price_history_adapter import build_price_history_fn
        price_fn = build_price_history_fn()
    return price_fn(token_id, int(event_ts)), price_fn(token_id, int(decision_ts))
