#!/usr/bin/env python3
"""
Polymarket client for fetching historical prices.
Uses snapshot-first approach: CLOB prices-history API as primary,
data-api/trades as fallback with noise labeling.
"""

import requests
import time
from typing import Optional, Dict, Any
from dataclasses import dataclass

from data.sackmann import player_sides_from_title, title_looks_like_doubles


@dataclass
class PricePoint:
    """Represents a price observation with metadata."""
    price: Optional[float]
    matched_ts: Optional[int]
    lag_seconds: Optional[int]
    source: str  # 'clob_prices_history' or 'data_trades_proxy'
    confidence: str  # 'high' or 'low'
    noise_band_pct: Optional[float]  # None for high confidence, ~5.0 for trade proxy
    validated: bool
    note: str


class PolymarketClient:
    """Client for Polymarket price data with snapshot-first approach."""
    
    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_API = "https://clob.polymarket.com"
    DATA_API = "https://data-api.polymarket.com"
    
    def __init__(self, max_retries: int = 3, retry_delay: float = 1.0, timeout: float = 30.0):
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout = timeout
        self.session = requests.Session()
    
    def _get(self, url: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Make GET request with retries."""
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    continue
                return None
        return None
    
    def get_market_meta(self, condition_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch market metadata including token mappings.
        
        Returns:
            {
                'condition_id': str,
                'outcomes': list[str],
                'clob_token_ids': list[str],
                'outcome_to_token': dict[str, str]
            }
        """
        url = f"{self.GAMMA_API}/markets"
        params = {'condition_ids': condition_id, 'limit': 1}
        
        data = self._get(url, params)
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
        
        market = data[0]
        
        # Both outcomes and clobTokenIds are JSON-encoded strings
        import json
        
        raw_outcomes = market.get('outcomes', '[]')
        if isinstance(raw_outcomes, str):
            try:
                outcomes = json.loads(raw_outcomes)
            except json.JSONDecodeError:
                outcomes = []
        else:
            outcomes = raw_outcomes
        
        raw_token_ids = market.get('clobTokenIds', '[]')
        if isinstance(raw_token_ids, str):
            try:
                token_ids = json.loads(raw_token_ids)
            except json.JSONDecodeError:
                token_ids = []
        else:
            token_ids = raw_token_ids
        
        # Build outcome -> token mapping
        outcome_to_token = {}
        for i, outcome in enumerate(outcomes):
            if i < len(token_ids):
                outcome_to_token[outcome.upper()] = token_ids[i]
        
        return {
            'condition_id': condition_id,
            'outcomes': outcomes,
            'clob_token_ids': token_ids,
            'outcome_to_token': outcome_to_token
        }
    
    def get_price_at(
        self,
        condition_id: str,
        outcome: str,
        timestamp: int,
        window_seconds: int = 3600,
        max_gap_seconds: int = 300
    ) -> PricePoint:
        """
        Get price at specific timestamp using snapshot-first approach.
        
        Args:
            condition_id: Market condition ID
            outcome: 'YES' or 'NO'
            timestamp: Target Unix timestamp (seconds)
            window_seconds: Time window to query around target
            max_gap_seconds: Maximum acceptable time gap from target
        
        Returns:
            PricePoint with confidence and source labeling
        """
        # Step 1: Get market metadata
        meta = self.get_market_meta(condition_id)
        if not meta:
            return PricePoint(
                price=None,
                matched_ts=None,
                lag_seconds=None,
                source='error',
                confidence='none',
                noise_band_pct=None,
                validated=False,
                note='Failed to fetch market metadata'
            )
        
        # Step 2: Get token ID for outcome
        token_id = meta['outcome_to_token'].get(outcome.upper())
        if not token_id:
            return PricePoint(
                price=None,
                matched_ts=None,
                lag_seconds=None,
                source='error',
                confidence='none',
                noise_band_pct=None,
                validated=False,
                note=f'Outcome {outcome} not found in market'
            )
        
        # Step 3: Try CLOB prices-history (primary source)
        start_ts = timestamp - window_seconds
        end_ts = timestamp + window_seconds
        
        price_point = self._get_clob_snapshot(
            token_id, timestamp, start_ts, end_ts, max_gap_seconds
        )
        
        if price_point and price_point.confidence == 'high':
            return price_point
        
        # Step 4: Fallback to trade proxy
        return self._get_trade_proxy_price(
            condition_id, outcome, timestamp, max_gap_seconds
        )
    
    def get_prices_series(
        self,
        token_id: str,
        start_ts: int,
        end_ts: int,
        fidelity: int = 1,
        max_window_seconds: int = 7 * 86400,
    ) -> list:
        """Fetch all prices-history points for a token in [start_ts, end_ts].

        fidelity=1 -> 1-minute bars (default; universally supported per
        Phase 0.5 audit). fidelity=60 -> 60-minute (hourly) bars.

        Returns list of {timestamp: int, price: float} sorted ascending.
        Empty list on error, missing history, or empty response.
        """
        url = f"{self.CLOB_API}/prices-history"
        if end_ts < start_ts:
            return []
        if max_window_seconds <= 0:
            max_window_seconds = end_ts - start_ts + 1

        out = []
        seen = set()
        cursor = start_ts
        while cursor <= end_ts:
            chunk_end = min(cursor + max_window_seconds - 1, end_ts)
            params = {
                "market": token_id,
                "startTs": cursor,
                "endTs": chunk_end,
                "fidelity": fidelity,
            }
            data = self._get(url, params)
            hist = (data or {}).get("history") or []
            for p in hist:
                ts = p.get("t")
                price = p.get("p")
                if ts is None or price is None:
                    continue
                ts = int(ts)
                key = (ts, fidelity)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"timestamp": ts, "price": float(price)})
            cursor = chunk_end + 1
        out.sort(key=lambda x: x["timestamp"])
        return out

    def get_orderbook(self, token_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the public CLOB orderbook snapshot for one token.

        Polymarket documents this as `GET https://clob.polymarket.com/book`
        with required query parameter `token_id`.
        """
        return self._get(f"{self.CLOB_API}/book", {"token_id": token_id})

    def get_trades_window(
        self,
        condition_id: str,
        start_ts: int,
        end_ts: int,
        page_size: int = 200,
        max_pages: int = 50,
    ) -> list:
        """Fetch trades for a market with timestamp in [start_ts, end_ts] (seconds).

        Paginates Polymarket /trades via offset+limit. /trades returns descending
        timestamp order (newest first) — verified 2026-04-22. Stops early when
        an entire page is fully older than start_ts. Hitting max_pages logs a
        warning and returns what we have.

        Each returned trade has `_ts_sec` injected (unix seconds) for downstream use.
        Raw trade dict fields are otherwise preserved as-is.
        """
        return self.get_trades_window_with_meta(
            condition_id,
            start_ts,
            end_ts,
            page_size=page_size,
            max_pages=max_pages,
        )["trades"]

    def get_trades_window_with_meta(
        self,
        condition_id: str,
        start_ts: int,
        end_ts: int,
        page_size: int = 200,
        max_pages: int = 50,
    ) -> dict:
        """Fetch trades plus pagination metadata for coverage auditing."""
        import logging
        logger = logging.getLogger(__name__)
        url = f"{self.DATA_API}/trades"
        trades = []
        offset = 0
        hit_cap = True
        pages_fetched = 0
        for _ in range(max_pages):
            params = {
                "market": condition_id,
                "limit": page_size,
                "offset": offset,
                "takerOnly": "false",
            }
            page = self._get(url, params)
            if not page or not isinstance(page, list):
                hit_cap = False
                break
            pages_fetched += 1
            page_older_count = 0
            for t in page:
                ts = t.get("timestamp", 0)
                if ts > 1_000_000_000_000:
                    ts //= 1000
                ts = int(ts)
                t["_ts_sec"] = ts
                if ts < start_ts:
                    page_older_count += 1
                elif start_ts <= ts <= end_ts:
                    trades.append(t)
            if page_older_count == len(page):
                hit_cap = False
                break
            if len(page) < page_size:
                hit_cap = False
                break
            offset += page_size
        if hit_cap:
            logger.warning("get_trades_window hit max_pages=%d for %s", max_pages, condition_id)
        ts_values = [int(t["_ts_sec"]) for t in trades if t.get("_ts_sec") is not None]
        return {
            "trades": trades,
            "pages_fetched": pages_fetched,
            "hit_page_cap": hit_cap,
            "first_trade_ts": min(ts_values) if ts_values else None,
            "last_trade_ts": max(ts_values) if ts_values else None,
        }

    def get_resolved_tennis_markets(
        self,
        since_ts: int = None,
        until_ts: int = None,
        page_size: int = 500,
        max_pages: int = 500,
    ) -> list:
        """Paginate Gamma /events?closed=true, filter tennis H2H, return structured dicts.

        Per-year windowing is mandatory: Polymarket's Gamma paginator returns
        events in endDate-desc order and depth-caps at 200K+ events all in 2026
        (per Phase 0.4 audit). Using end_date_min/end_date_max filters per year
        forces coverage of earlier periods.

        Filters applied:
        - event.tags includes 'tennis'
        - event.title matches r"\\bvs\\.?\\b" (case insensitive)
        - at least one market where market.question.strip() == event.title.strip()
        - event.endDate in [since_ts, until_ts] (via per-year filter params)

        Returns list of dicts with:
        - condition_id, token_id_yes, token_id_no, title, end_date (iso),
          start_date (iso), event_slug, tour_prefix, event_id
        Deduped by event_id across year windows.
        """
        import json as _json

        non_match_market_types = {
            "tennis_first_set_winner",
            "tennis_first_set_totals",
            "tennis_match_totals",
            "tennis_set_handicap",
            "tennis_set_totals",
        }

        def market_title(event_title, market):
            question = (market.get("question") or "").strip()
            side1, side2 = player_sides_from_title(event_title)
            if side1 and side2 and not title_looks_like_doubles(event_title):
                return event_title
            return question

        def is_h2h_moneyline(event_title, market):
            if not market.get("conditionId"):
                return False
            if (market.get("sportsMarketType") or "").strip() in non_match_market_types:
                return False
            title = market_title(event_title, market)
            side1, side2 = player_sides_from_title(title)
            if not side1 or not side2 or title_looks_like_doubles(title):
                return False
            market_type = (market.get("sportsMarketType") or "").strip()
            return market_type == "moneyline" or (market.get("question") or "").strip() == event_title

        def select_h2h_market(event):
            event_title = (event.get("title") or "").strip()
            candidates = [m for m in (event.get("markets") or []) if is_h2h_moneyline(event_title, m)]
            if not candidates:
                return (None, "")
            candidates.sort(
                key=lambda m: (
                    (m.get("question") or "").strip() != event_title,
                    (m.get("sportsMarketType") or "").strip() != "moneyline",
                    str(m.get("groupItemThreshold") or "") != "0",
                )
            )
            market = candidates[0]
            return (market, market_title(event_title, market))

        out = []
        seen_ids = set()
        for year_start, year_end in _year_windows(since_ts, until_ts):
            offset = 0
            for _ in range(max_pages):
                params = {
                    "closed": "true",
                    "limit": page_size,
                    "offset": offset,
                    "end_date_min": year_start,
                    "end_date_max": year_end,
                }
                page = self._get(f"{self.GAMMA_API}/events", params)
                if not page or not isinstance(page, list):
                    break
                for e in page:
                    eid = e.get("id")
                    if eid in seen_ids:
                        continue
                    tags = [t.get("slug", "") if isinstance(t, dict) else str(t)
                            for t in (e.get("tags") or [])]
                    if "tennis" not in tags:
                        continue
                    etitle = (e.get("title") or "").strip()
                    h2h, match_title = select_h2h_market(e)
                    if not h2h:
                        continue
                    try:
                        tids = _json.loads(h2h.get("clobTokenIds", "[]"))
                    except Exception:
                        continue
                    if not tids:
                        continue
                    slug = (e.get("slug") or "").strip()
                    tour_prefix = slug.split("-", 1)[0] if slug else ""
                    out.append({
                        "condition_id": h2h["conditionId"],
                        "token_id_yes": tids[0],
                        "token_id_no": tids[1] if len(tids) > 1 else None,
                        "title": match_title,
                        "end_date": e.get("endDate"),
                        "start_date": e.get("startDate"),
                        "event_slug": slug,
                        "tour_prefix": tour_prefix,
                        "event_id": eid,
                    })
                    seen_ids.add(eid)
                if len(page) < page_size:
                    break
                offset += page_size
                time.sleep(0.15)
        return out

    def _get_clob_snapshot(
        self,
        token_id: str,
        target_ts: int,
        start_ts: int,
        end_ts: int,
        max_gap_seconds: int
    ) -> Optional[PricePoint]:
        """Query CLOB prices-history for nearest snapshot."""
        url = f"{self.CLOB_API}/prices-history"
        params = {
            'market': token_id,
            'interval': '1m',
            'fidelity': '60',
            'startTs': start_ts,
            'endTs': end_ts
        }
        
        data = self._get(url, params)
        if not data or 'history' not in data or not data['history']:
            return None
        
        # Find nearest snapshot to target timestamp
        history = data['history']
        nearest = None
        min_gap = float('inf')
        
        for point in history:
            point_ts = point.get('t', 0)
            gap = abs(point_ts - target_ts)
            if gap < min_gap:
                min_gap = gap
                nearest = point
        
        if not nearest or min_gap > max_gap_seconds:
            return None
        
        price = nearest.get('p')
        if price is None:
            return None
        
        return PricePoint(
            price=float(price),
            matched_ts=int(nearest['t']),
            lag_seconds=int(min_gap),
            source='clob_prices_history',
            confidence='high',
            noise_band_pct=None,
            validated=True,
            note='Snapshot from CLOB prices-history'
        )
    
    def _get_trade_proxy_price(
        self,
        condition_id: str,
        outcome: str,
        timestamp: int,
        max_gap_seconds: int
    ) -> PricePoint:
        """Fallback: Use data-api/trades to estimate price."""
        url = f"{self.DATA_API}/trades"
        
        # Query window around target
        start_time = timestamp - max_gap_seconds
        
        params = {
            'market': condition_id,
            'limit': 100,
            'offset': 0
        }
        
        data = self._get(url, params)
        if not data or not isinstance(data, list):
            return PricePoint(
                price=None,
                matched_ts=None,
                lag_seconds=None,
                source='data_trades_proxy',
                confidence='none',
                noise_band_pct=None,
                validated=False,
                note='Failed to fetch trades'
            )
        
        # Find nearest trade matching outcome
        nearest = None
        min_gap = float('inf')
        
        for trade in data:
            trade_ts = trade.get('timestamp', 0)
            # Convert ms to seconds if needed
            if trade_ts > 1_000_000_000_000:
                trade_ts = trade_ts // 1000
            
            trade_outcome = trade.get('outcome', '').upper()
            if trade_outcome != outcome.upper():
                continue
            
            gap = abs(trade_ts - timestamp)
            if gap < min_gap:
                min_gap = gap
                nearest = trade
        
        if not nearest or min_gap > max_gap_seconds:
            return PricePoint(
                price=None,
                matched_ts=None,
                lag_seconds=None,
                source='data_trades_proxy',
                confidence='none',
                noise_band_pct=None,
                validated=False,
                note=f'No matching trade within {max_gap_seconds}s'
            )
        
        price = nearest.get('price')
        if price is None:
            return PricePoint(
                price=None,
                matched_ts=None,
                lag_seconds=None,
                source='data_trades_proxy',
                confidence='none',
                noise_band_pct=None,
                validated=False,
                note='Trade found but no price available'
            )
        
        return PricePoint(
            price=float(price),
            matched_ts=int(nearest.get('timestamp', 0)) // 1000,
            lag_seconds=int(min_gap),
            source='data_trades_proxy',
            confidence='low',
            noise_band_pct=5.0,  # Estimated noise from trade slippage
            validated=False,
            note='Trade proxy fallback with ±5% noise band'
        )


def _year_windows(since_ts, until_ts):
    """Yield (start_iso, end_iso) per calendar year inside [since_ts, until_ts].

    If since_ts is None, defaults to 2024-01-01 (Polymarket's earliest tennis
    listing per Phase 0.4). If until_ts is None, defaults to now UTC.
    """
    from datetime import datetime, timezone
    if since_ts is None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    else:
        start = datetime.fromtimestamp(since_ts, tz=timezone.utc)
    if until_ts is None:
        end = datetime.now(timezone.utc)
    else:
        end = datetime.fromtimestamp(until_ts, tz=timezone.utc)
    y = start.year
    while y <= end.year:
        yield (f"{y}-01-01T00:00:00Z", f"{y}-12-31T23:59:59Z")
        y += 1


if __name__ == '__main__':
    # Quick test
    client = PolymarketClient()
    
    # Test with a known market (replace with actual condition ID from your DB)
    test_condition = "0x61b66d02793b4a68ab0cc25be60d65f517fe18c7d654041281bb130341244fcc"
    
    print("Testing market metadata fetch...")
    meta = client.get_market_meta(test_condition)
    print(f"Meta: {meta}")
    
    if meta:
        print("\nTesting price fetch...")
        price_point = client.get_price_at(
            test_condition,
            'YES',
            int(time.time()) - 3600,  # 1 hour ago
            window_seconds=7200,
            max_gap_seconds=300
        )
        print(f"PricePoint: {price_point}")
