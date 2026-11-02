"""Discover + parse Polymarket BTC 5m Up/Down markets from Gamma.

Pure parsing (``parse_event``) and the market-state gate (``capture_ready``) are unit-tested from
recorded fixtures; the thin network layer (``discover`` / ``fetch_event_by_slug``) takes an injected
``fetch(url)->json`` callable so no test touches the live API. Acceptance is multi-gate via
``rule_contract`` — never a title regex — and includes a series-identity check so a direct slug fetch
cannot accept a look-alike event that merely copies the rule text.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Callable, Optional

from . import rule_contract as RC
from .models import MarketSpec, RuleReason, RuleVerdict

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
BTC_5M_SERIES_ID = RC.BTC_5M_SERIES_ID
BTC_5M_SERIES_SLUG = "btc-up-or-down-5m"


def window_from_slug(slug: str) -> Optional[int]:
    """The slug suffix is the window-start epoch ('btc-updown-5m-1781947200' -> 1781947200)."""
    try:
        return int((slug or "").rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None


def rule_text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _json_list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    return value if isinstance(value, list) else None


def _epoch_from_iso(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _extract_series_id(raw_event: dict) -> str:
    """Series id from the event's ``series`` list, falling back to a known ``seriesSlug``."""
    series = raw_event.get("series") or []
    if series and isinstance(series, list):
        sid = str((series[0] or {}).get("id") or "")
        if sid:
            return sid
    if raw_event.get("seriesSlug") == BTC_5M_SERIES_SLUG:
        return BTC_5M_SERIES_ID
    return ""


def _is_btc_candidate(mk: dict) -> bool:
    outs = _json_list(mk.get("outcomes")) or []
    toks = _json_list(mk.get("clobTokenIds")) or []
    return tuple(outs) == ("Up", "Down") and bool(mk.get("conditionId")) and len(toks) == 2


def _select_market(markets: list):
    """Pick THE market for a Gamma event deterministically. ``(mk, ambiguous)``.

    One market -> use it (validated normally). Multiple -> select the single BTC Up/Down candidate;
    if zero or more than one candidate, it's ambiguous (never blindly take ``markets[0]``).
    """
    if not markets:
        return {}, False
    if len(markets) == 1:
        return markets[0], False
    cands = [m for m in markets if _is_btc_candidate(m)]
    if len(cands) == 1:
        return cands[0], False
    return None, True


def parse_event(raw_event: dict) -> tuple[Optional[MarketSpec], RuleVerdict]:
    """Parse a Gamma event into ``(MarketSpec|None, RuleVerdict)``.

    A spec is built only when the market is ``rule_supported`` (which already requires a condition id,
    series identity, and exactly two distinct tokens). ``price_to_beat`` may be None (live window).
    """
    md = raw_event.get("eventMetadata") or {}
    markets = raw_event.get("markets") or []
    mk, ambiguous = _select_market(markets)
    if ambiguous:
        return None, RC._verdict(RuleReason.AMBIGUOUS_MARKET, detail=f"{len(markets)} markets")
    mk = mk or {}

    slug = raw_event.get("slug") or ""
    series_id = _extract_series_id(raw_event)
    condition_id = mk.get("conditionId") or ""
    outcomes = tuple(_json_list(mk.get("outcomes")) or ())
    tokens = _json_list(mk.get("clobTokenIds")) or []
    token_count = len(tokens)
    up_token = tokens[0] if token_count >= 1 else None
    down_token = tokens[1] if token_count >= 2 else None
    rule_text = mk.get("description") or ""
    window_start = window_from_slug(slug)
    window_end = _epoch_from_iso(mk.get("endDate") or raw_event.get("endDate"))

    verdict = RC.validate_rule(
        series_id=series_id,
        condition_id=condition_id,
        outcomes=outcomes,
        resolution_source=mk.get("resolutionSource"),
        rule_text=rule_text,
        window_start_ts=window_start,
        window_end_ts=window_end,
        up_token_id=up_token,
        down_token_id=down_token,
        token_count=token_count,
    )

    if not verdict.rule_supported:
        return None, verdict

    spec = MarketSpec(
        event_id=str(raw_event.get("id") or ""),
        market_id=str(mk.get("id") or ""),
        condition_id=condition_id,
        slug=slug,
        question=mk.get("question") or raw_event.get("title") or "",
        raw_rule_text=rule_text,
        rule_text_hash=rule_text_hash(rule_text),
        resolution_source=mk.get("resolutionSource") or "",
        series_id=series_id,
        market_created_ts=mk.get("createdAt"),
        window_start_ts=int(window_start),
        window_end_ts=int(window_end),
        up_token_id=up_token,
        down_token_id=down_token,
        outcomes=outcomes,
        price_to_beat=_num(md.get("priceToBeat")),
        fees_enabled=bool(mk.get("feesEnabled")),
        fee_type=mk.get("feeType"),
        taker_base_fee=mk.get("takerBaseFee"),
        maker_base_fee=mk.get("makerBaseFee"),
        tick_size=_num(mk.get("orderPriceMinTickSize")),
        min_order_size=_num(mk.get("orderMinSize")),
        active=bool(raw_event.get("active")) and bool(mk.get("active")),
        closed=bool(raw_event.get("closed")) or bool(mk.get("closed")),
        accepting_orders=bool(mk.get("acceptingOrders")),
        enable_order_book=bool(raw_event.get("enableOrderBook")) and bool(mk.get("enableOrderBook")),
        raw_payload=raw_event,
    )
    return spec, verdict


def capture_ready(raw_event: dict, *, now_ts: int) -> tuple[bool, str]:
    """Market-state gate (separate from ``rule_supported``): can the live book be captured NOW?

    Returns ``(ready, reason)``. ``now_ts`` is server time (unix seconds), never the local clock.
    """
    markets = raw_event.get("markets") or []
    if not markets:
        return False, "no_market"
    mk, ambiguous = _select_market(markets)        # same deterministic BTC selection as parse_event
    if ambiguous:
        return False, "ambiguous_market"
    mk = mk or {}
    if not mk:
        return False, "no_market"
    if bool(raw_event.get("closed")) or bool(mk.get("closed")):
        return False, "closed"
    if not (bool(raw_event.get("active")) and bool(mk.get("active"))):
        return False, "not_active"
    if not (bool(raw_event.get("enableOrderBook")) and bool(mk.get("enableOrderBook"))):
        return False, "orderbook_disabled"
    if not bool(mk.get("acceptingOrders")):
        return False, "not_accepting_orders"
    window_end = _epoch_from_iso(mk.get("endDate") or raw_event.get("endDate"))
    if window_end is not None and int(now_ts) >= window_end:
        return False, "window_ended"
    return True, "ready"


# --- thin network discovery (fetch injected; not unit-tested against live) -------------------

def _events_url(*, page_size: int, offset: int, closed: Optional[bool]) -> str:
    parts = [f"series_id={BTC_5M_SERIES_ID}", f"limit={page_size}", f"offset={offset}",
             "order=endDate", "ascending=false"]
    if closed is not None:
        parts.append(f"closed={'true' if closed else 'false'}")
    return GAMMA_EVENTS_URL + "?" + "&".join(parts)


def discover(fetch: Callable[[str], object], *, page_size: int = 50, max_pages: int = 10,
             closed: Optional[bool] = None) -> dict:
    """Paginate the BTC 5m series and partition events into ``rule_supported`` specs / rejections."""
    accepted: list[MarketSpec] = []
    rejected: list[tuple] = []
    for page in range(max_pages):
        events = fetch(_events_url(page_size=page_size, offset=page * page_size, closed=closed))
        if not events:
            break
        for raw in events:
            spec, verdict = parse_event(raw)
            if verdict.rule_supported and spec is not None:
                accepted.append(spec)
            else:
                rejected.append((raw.get("slug"), verdict.reason.value))
        if len(events) < page_size:
            break
    return {
        "accepted": accepted,
        "rejected": rejected,
        "counts": {"accepted": len(accepted), "rejected": len(rejected)},
    }


def fetch_event_by_slug(fetch: Callable[[str], object], slug: str):
    events = fetch(f"{GAMMA_EVENTS_URL}?slug={slug}")
    if not events:
        return None, RuleVerdict(RuleReason.WRONG_SERIES, False, RC.RULE_CONTRACT_VERSION,
                                 detail="event not found")
    return parse_event(events[0])
