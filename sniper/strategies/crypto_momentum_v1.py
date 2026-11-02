"""Crypto Momentum V1 strategy plugin.

Trades Polymarket crypto binary markets (Bitcoin price targets, up/down markets)
using real-time price feeds from Binance.

Three market types:
1. Price targets: "Will Bitcoin reach $X by [date]?" — momentum + proximity
2. Daily above/below: "Bitcoin above $X on [date]?" — momentum + proximity  
3. 5-min binary: "Bitcoin Up or Down [time]" — pure short-term momentum

Signal logic:
- Poll Binance for real-time BTC price
- Compute momentum (% change over configurable window)
- Find matching Polymarket crypto markets
- Score proximity to strike price
- Generate signals for markets where momentum aligns with direction

This is adapted from oil_momentum_v1.py but uses live crypto price feeds
instead of commodity_snapshots table.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sniper.markets.polymarket_api import fetch_live_clob_price

from .base import (
    StrategyEntryDecision,
    StrategyExitDecision,
    StrategyManageState,
    StrategyPlugin,
    StrategyReport,
    StrategySignal,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Price feed
# ---------------------------------------------------------------------------

_BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
_BINANCE_DEPTH_URL = "https://api.binance.com/api/v3/depth"
_BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
_COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"

# Market type patterns
_PRICE_TARGET_RE = re.compile(
    r"(?:reach|hit)\s*\$?([\d,]+)", re.IGNORECASE
)
_ABOVE_BELOW_RE = re.compile(
    r"(?:above|below|over|under)\s*\$?([\d,]+)", re.IGNORECASE
)
_DIP_TO_RE = re.compile(
    r"dip\s+to\s+\$?([\d,]+)", re.IGNORECASE
)
_UP_DOWN_RE = re.compile(
    r"bitcoin\s+(?:up|down)", re.IGNORECASE
)
_STRIKE_RE = re.compile(
    r"\$?([\d]{2,3}[,.]?\d{3})", re.IGNORECASE
)

# Direction keywords
_UP_KEYWORDS = ("reach", "hit", "above", "over", "up", "higher", "exceed")
_DOWN_KEYWORDS = ("below", "under", "down", "fall", "drop", "crash", "dip")

# Macro bias scoring keywords (used by _fetch_btc_macro_bias)
_MACRO_BULL_KEYWORDS = (
    "rally", "inflow", "_up", "up_", "ceasefire", "surge", "relief",
    "strong", "bull", "positive", "high",
)
_MACRO_BEAR_KEYWORDS = (
    "fear", "extreme_fear", "red_month", "crash", "drop", "bear",
    "extreme_risk", "war_risk", "panic", "sell",
)

# Default configuration
_DEFAULT_BET_SIZE = 15.0
_DEFAULT_MAX_ENTRY_PRICE = 0.70
_DEFAULT_MIN_ENTRY_PRICE = 0.10
_DEFAULT_MAX_CONCURRENT = 8
_DEFAULT_MAX_EXPOSURE = 150.0
_DEFAULT_STALE_DAYS = 7
_DEFAULT_WIN_THRESHOLD = 0.97
_DEFAULT_LOSS_THRESHOLD = 0.03
_DEFAULT_TAKE_PROFIT_ROI = 0.40
_DEFAULT_MOMENTUM_WINDOW_HOURS = 24
_DEFAULT_MIN_MOMENTUM_PCT = 0.5
_DEFAULT_MAX_STRIKE_DISTANCE_PCT = 10.0  # max % away from current price (backtest showed 5% cuts biggest wins)
_DEFAULT_MIN_VOLUME_24H = 1000.0         # minimum 24h volume for a market to be tradeable


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _parse_params(strategy: Dict[str, Any]) -> Dict[str, Any]:
    params = strategy.get("parameters") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (json.JSONDecodeError, TypeError):
            return {}
    return params if isinstance(params, dict) else {}


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _fetch_btc_price() -> Optional[float]:
    """Fetch current BTC/USDT price from Binance."""
    import requests
    try:
        resp = requests.get(
            f"{_BINANCE_TICKER_URL}?symbol=BTCUSDT",
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception:
        logger.warning("crypto_momentum: Binance price fetch failed, trying CoinGecko")
        try:
            resp = requests.get(
                f"{_COINGECKO_URL}?ids=bitcoin&vs_currencies=usd",
                timeout=10,
            )
            resp.raise_for_status()
            return float(resp.json()["bitcoin"]["usd"])
        except Exception:
            logger.exception("crypto_momentum: all price feeds failed")
            return None


def _fetch_btc_momentum(window_hours: int = 24) -> Optional[Dict[str, Any]]:
    """Compute BTC price momentum over window using Binance klines."""
    import requests
    try:
        # Use hourly candles for momentum calculation
        interval = "1h"
        limit = min(window_hours, 500)
        resp = requests.get(
            _BINANCE_KLINES_URL,
            params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        klines = resp.json()

        if len(klines) < 2:
            return None

        oldest_close = float(klines[0][4])  # close price
        newest_close = float(klines[-1][4])
        current_price = newest_close

        if oldest_close <= 0:
            return None

        pct_change = (newest_close - oldest_close) / oldest_close * 100.0

        # Also compute short-term momentum (last 4 hours)
        short_window = min(4, len(klines))
        short_oldest = float(klines[-short_window][4])
        short_pct = (newest_close - short_oldest) / short_oldest * 100.0 if short_oldest > 0 else 0

        return {
            "current_price": current_price,
            "momentum_pct": pct_change,
            "short_momentum_pct": short_pct,
            "direction": "up" if pct_change > 0 else "down",
            "window_hours": window_hours,
            "computed_ts": _now_ts(),
        }
    except Exception:
        logger.exception("crypto_momentum: Binance klines fetch failed")
        return None


def _fetch_btc_funding_rate() -> Optional[float]:
    """Fetch latest BTC perpetual funding rate from Binance Futures (in percent).

    Positive rate  → longs pay shorts → crowded long → confirms upside momentum.
    Negative rate  → shorts pay longs → crowded short → confirms downside momentum.
    Extreme values (> ±0.15 %) may signal overextension; still directionally valid
    but flagged so callers can decide whether to fade the crowd.
    Returns None on any fetch failure so callers degrade gracefully.
    """
    import requests
    try:
        resp = requests.get(
            _BINANCE_FUNDING_URL,
            params={"symbol": "BTCUSDT", "limit": 1},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        if data and isinstance(data, list):
            return float(data[-1]["fundingRate"]) * 100.0  # raw is a fraction, convert to %
    except Exception:
        logger.warning("crypto_momentum: funding rate fetch failed")
    return None


def _fetch_btc_book_imbalance() -> Optional[float]:
    """Compute BTC order-book bid/ask volume ratio from top-20 Binance levels.

    Returns a float in [0, 1]:
    - > 0.55 : bid-heavy → buy pressure → supports 'up' direction
    - < 0.45 : ask-heavy → sell pressure → supports 'down' direction
    - 0.45–0.55 : balanced → direction-neutral
    Returns None on failure.
    """
    import requests
    try:
        resp = requests.get(
            _BINANCE_DEPTH_URL,
            params={"symbol": "BTCUSDT", "limit": 20},
            timeout=8,
        )
        resp.raise_for_status()
        book = resp.json()
        bid_vol = sum(float(b[1]) for b in book.get("bids", []))
        ask_vol = sum(float(a[1]) for a in book.get("asks", []))
        total = bid_vol + ask_vol
        return bid_vol / total if total > 0 else None
    except Exception:
        logger.warning("crypto_momentum: order book fetch failed")
    return None


def _fetch_btc_macro_bias(conn: sqlite3.Connection) -> str:
    """Derive a macro bias label from recent BTC rows in the situation_model table.

    Scans object strings of situation_model rows written in the last 48 h whose
    subject or slug relates to BTC/bitcoin/crypto and tallies weighted bullish /
    bearish signals.

    Returns 'bullish', 'bearish', or 'neutral'.
    """
    try:
        rows = conn.execute(
            """SELECT object, confidence
               FROM situation_model
               WHERE (subject LIKE '%BTC%'
                   OR subject LIKE '%bitcoin%'
                   OR slug LIKE '%btc%'
                   OR slug LIKE '%bitcoin%'
                   OR slug LIKE '%crypto%')
                 AND valid_from >= datetime('now', '-48 hours')
               ORDER BY valid_from DESC
               LIMIT 20""",
        ).fetchall()
    except Exception:
        return "neutral"

    bull_score = 0.0
    bear_score = 0.0
    for row in rows:
        obj = str(row[0] or "").lower()
        conf = float(row[1] if row[1] is not None else 0.5)
        if any(w in obj for w in _MACRO_BULL_KEYWORDS):
            bull_score += conf
        if any(w in obj for w in _MACRO_BEAR_KEYWORDS):
            bear_score += conf

    if bull_score > bear_score * 1.5:
        return "bullish"
    if bear_score > bull_score * 1.5:
        return "bearish"
    return "neutral"


def _check_btc_news_sentiment(conn: sqlite3.Connection, hours: int = 24) -> Dict[str, int]:
    """Count bullish/bearish/neutral Bitcoin news items from the last `hours` hours.

    Reads the news_items table (topic_tags column).  The sentiment field stores
    labels like 'bullish', 'bearish', 'bullish_no' (= bearish event for bulls),
    or 'neutral'.

    Returns dict with keys: bullish, bearish, neutral, total.
    """
    empty: Dict[str, int] = {"bullish": 0, "bearish": 0, "neutral": 0, "total": 0}
    try:
        rows = conn.execute(
            """SELECT sentiment FROM news_items
               WHERE (topic_tags LIKE '%bitcoin%' OR topic_tags LIKE '%BTC%')
                 AND published_at >= datetime('now', ?)
               ORDER BY published_at DESC
               LIMIT 60""",
            (f"-{hours} hours",),
        ).fetchall()
    except Exception:
        return empty

    counts: Dict[str, int] = {"bullish": 0, "bearish": 0, "neutral": 0, "total": len(rows)}
    for row in rows:
        raw = str(row[0] or "neutral").lower()
        if raw == "bullish":
            counts["bullish"] += 1
        elif raw in ("bearish", "bullish_no"):
            # 'bullish_no' = a bad-for-bulls event → treat as bearish signal
            counts["bearish"] += 1
        else:
            counts["neutral"] += 1
    return counts


def _extract_strike_price(title: str) -> Optional[float]:
    """Extract the strike/target price from a market title."""
    # Try specific patterns first
    for pattern in [_PRICE_TARGET_RE, _ABOVE_BELOW_RE, _DIP_TO_RE]:
        match = pattern.search(title)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                pass

    # Fallback: find any dollar amount that looks like a BTC price ($50K-$500K range)
    for match in _STRIKE_RE.finditer(title):
        try:
            val = float(match.group(1).replace(",", ""))
            if 10_000 <= val <= 500_000:
                return val
        except ValueError:
            pass

    return None


def _classify_market_direction(title: str) -> Optional[str]:
    """Classify if a market benefits from price going up or down."""
    lower = title.lower()
    has_up = any(kw in lower for kw in _UP_KEYWORDS)
    has_down = any(kw in lower for kw in _DOWN_KEYWORDS)

    if has_up and not has_down:
        return "up"
    if has_down and not has_up:
        return "down"
    # "Up or Down" markets — handled separately
    if "up or down" in lower:
        return None
    return None


def _score_proximity(
    current_price: float,
    strike_price: float,
    direction: str,
) -> float:
    """Score how close the current price is to triggering the market.
    
    Returns 0.0-1.0 where higher = closer to triggering.
    For 'up' markets: higher score when price is just below strike.
    For 'down' markets: higher score when price is just above strike.
    """
    if current_price <= 0 or strike_price <= 0:
        return 0.0

    distance_pct = abs(current_price - strike_price) / strike_price * 100

    if direction == "up":
        # For upside markets, best when price is just below strike
        if current_price >= strike_price:
            return 1.0  # Already above — very likely to resolve YES
        # Score based on proximity
        if distance_pct <= 1:
            return 0.95
        elif distance_pct <= 3:
            return 0.80
        elif distance_pct <= 5:
            return 0.60
        elif distance_pct <= 10:
            return 0.30
        else:
            return 0.10
    elif direction == "down":
        if current_price <= strike_price:
            return 1.0
        if distance_pct <= 1:
            return 0.95
        elif distance_pct <= 3:
            return 0.80
        elif distance_pct <= 5:
            return 0.60
        elif distance_pct <= 10:
            return 0.30
        else:
            return 0.10

    return 0.0


class CryptoMomentumV1Strategy(StrategyPlugin):
    """Crypto price momentum strategy for Polymarket binary markets."""

    plugin_id = "crypto_momentum_v1"
    display_name = "Crypto Momentum V1"
    description = (
        "Trades Polymarket crypto binary markets using real-time BTC price "
        "momentum from Binance. Targets price targets, daily above/below, "
        "and short-term up/down markets."
    )

    strategy_family = "crypto_momentum"
    supported_execution_modes = ("paper",)
    required_feeds = ("polymarket",)
    default_loop_seconds = 120  # Check every 2 minutes

    def discover(
        self, conn: sqlite3.Connection, strategy: Dict[str, Any]
    ) -> List[StrategySignal]:
        """Find crypto binary markets aligned with BTC momentum."""
        params = _parse_params(strategy)
        window_hours = int(params.get("momentum_window_hours", _DEFAULT_MOMENTUM_WINDOW_HOURS))
        min_momentum_pct = _to_float(
            params.get("min_momentum_pct", _DEFAULT_MIN_MOMENTUM_PCT)
        )
        max_strike_dist = _to_float(
            params.get("max_strike_distance_pct", _DEFAULT_MAX_STRIKE_DISTANCE_PCT)
        )
        max_entry_price = _to_float(
            params.get("max_entry_price", _DEFAULT_MAX_ENTRY_PRICE)
        )
        min_entry_price = _to_float(
            params.get("min_entry_price", _DEFAULT_MIN_ENTRY_PRICE)
        )
        min_volume_24h = _to_float(
            params.get("min_volume_24h", _DEFAULT_MIN_VOLUME_24H)
        )

        # Get BTC momentum
        momentum = _fetch_btc_momentum(window_hours)
        if momentum is None:
            logger.info("crypto_momentum: no momentum data available")
            return []

        btc_price = momentum["current_price"]
        mom_pct = momentum["momentum_pct"]
        short_mom = momentum["short_momentum_pct"]

        if abs(mom_pct) < min_momentum_pct:
            logger.info(
                "crypto_momentum: momentum %.2f%% below threshold %.2f%%",
                mom_pct, min_momentum_pct,
            )
            return []

        mom_direction = momentum["direction"]

        # ---------------------------------------------------------------
        # Fetch one-time auxiliary signals (attached to every signal so
        # enter() can gate on them without extra API calls per signal).
        # All are best-effort; None means unavailable and gates degrade.
        # The two Binance HTTP calls are independent — run concurrently.
        # ---------------------------------------------------------------
        with ThreadPoolExecutor(max_workers=2) as _pool:
            funding_future = _pool.submit(_fetch_btc_funding_rate)
            book_future = _pool.submit(_fetch_btc_book_imbalance)
            funding_rate = funding_future.result()
            book_imbalance = book_future.result()
        macro_bias = _fetch_btc_macro_bias(conn)
        news_sentiment = _check_btc_news_sentiment(conn)

        logger.info(
            "crypto_momentum: BTC=$%.0f mom=%.2f%%(%s) short=%.2f%% | "
            "macro=%s funding=%s book=%s | news bull=%d bear=%d",
            btc_price, mom_pct, mom_direction, short_mom,
            macro_bias,
            f"{funding_rate:.4f}%" if funding_rate is not None else "n/a",
            f"{book_imbalance:.2f}" if book_imbalance is not None else "n/a",
            news_sentiment["bullish"], news_sentiment["bearish"],
        )

        # Find crypto markets directly from market_metadata
        # (can't use find_affected_markets — it limits to 500 by volume
        # and BTC markets are numerous but individually lower volume)
        try:
            from datetime import datetime as _dt, timezone as _tz
            _cutoff = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows = conn.execute(
                """SELECT condition_id, title, end_date_iso, volume_24h
                   FROM market_metadata
                   WHERE is_open = 1
                     AND (LOWER(title) LIKE '%bitcoin%' OR LOWER(title) LIKE '%btc%')
                     AND (end_date_iso IS NULL OR end_date_iso >= ?)
                     AND (volume_24h IS NULL OR volume_24h >= ?)
                   ORDER BY volume_24h DESC
                   LIMIT 200""",
                (_cutoff, min_volume_24h),
            ).fetchall()
            markets = []
            for r in rows:
                if isinstance(r, tuple):
                    markets.append({
                        "condition_id": r[0], "title": r[1],
                        "end_date_iso": r[2], "volume_24h": r[3],
                    })
                else:
                    markets.append(dict(r))
        except Exception:
            logger.exception("crypto_momentum: market query failed")
            return []

        signals: List[StrategySignal] = []
        for market in markets:
            condition_id = str(market.get("condition_id") or "")
            title = str(market.get("title") or "")
            end_date_iso = str(market.get("end_date_iso") or "")

            # Skip 5-min binary markets (need different logic)
            if _UP_DOWN_RE.search(title) and ("AM" in title or "PM" in title):
                continue

            # Extract strike price
            strike = _extract_strike_price(title)
            if strike is None:
                continue

            # Check distance from current price (tight 5 % default)
            distance_pct = abs(btc_price - strike) / strike * 100
            if distance_pct > max_strike_dist:
                continue

            # Determine market direction
            direction = _classify_market_direction(title)
            if direction is None:
                continue

            # Only enter if momentum aligns with market direction
            if direction != mom_direction:
                continue

            # Score proximity
            proximity = _score_proximity(btc_price, strike, direction)

            # Get live price
            live_price = fetch_live_clob_price(condition_id, "Yes")
            if live_price is None or live_price <= 0:
                continue

            if live_price > max_entry_price or live_price < min_entry_price:
                continue

            signals.append(
                StrategySignal(
                    wallet="crypto_momentum_bot",
                    condition_id=condition_id,
                    outcome="Yes",
                    title=title,
                    current_price=live_price,
                    position_value=0.0,
                    analyzed_ts=momentum["computed_ts"],
                    metadata={
                        "direction": direction,
                        "btc_price": btc_price,
                        "strike_price": strike,
                        "distance_pct": distance_pct,
                        "proximity_score": proximity,
                        "momentum_pct": mom_pct,
                        "short_momentum_pct": short_mom,
                        "end_date_iso": end_date_iso,
                        # Auxiliary signals (attached once, reused in enter())
                        "funding_rate_pct": funding_rate,
                        "book_imbalance": book_imbalance,
                        "macro_bias": macro_bias,
                        "news_bullish": news_sentiment["bullish"],
                        "news_bearish": news_sentiment["bearish"],
                        "news_total": news_sentiment["total"],
                    },
                )
            )

        # Sort by proximity score (best opportunities first)
        signals.sort(key=lambda s: s.metadata.get("proximity_score", 0), reverse=True)
        return signals

    def enter(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        signal: StrategySignal,
        open_positions: List[Dict[str, Any]],
    ) -> StrategyEntryDecision:
        """Gate checks and entry decision."""
        params = _parse_params(strategy)
        bet_size = _to_float(params.get("bet_size_usdc", _DEFAULT_BET_SIZE))
        max_entry_price = _to_float(params.get("max_entry_price", _DEFAULT_MAX_ENTRY_PRICE))
        min_entry_price = _to_float(params.get("min_entry_price", _DEFAULT_MIN_ENTRY_PRICE))
        max_concurrent = int(params.get("max_concurrent", _DEFAULT_MAX_CONCURRENT))
        max_exposure = _to_float(params.get("max_exposure_per_market_usdc", _DEFAULT_MAX_EXPOSURE))
        min_proximity = _to_float(params.get("min_proximity_score", 0.30))

        # Gate 1: Duplicate check
        if any(
            p.get("condition_id") == signal.condition_id
            and str(p.get("outcome") or "").upper() == signal.outcome.upper()
            for p in open_positions
        ):
            return StrategyEntryDecision(False, "already open for this market/outcome")

        # Gate 2: Max concurrent
        if len(open_positions) >= max_concurrent:
            return StrategyEntryDecision(
                False, f"max concurrent positions reached ({max_concurrent})"
            )

        # Gate 3: Price range
        if signal.current_price > max_entry_price:
            return StrategyEntryDecision(
                False, f"price {signal.current_price:.3f} > {max_entry_price:.2f}"
            )
        if signal.current_price < min_entry_price:
            return StrategyEntryDecision(
                False, f"price {signal.current_price:.3f} < {min_entry_price:.2f}"
            )

        # Gate 3b: Entry price must be safely above the loss threshold so the
        # position isn't instantly stopped out on the first manage() cycle.
        # (loss_threshold defaults to 0.15; min_entry_price defaults to 0.20 —
        # but validate here explicitly in case params diverge.)
        loss_threshold = _to_float(params.get("loss_threshold", 0.15))
        if signal.current_price <= loss_threshold:
            return StrategyEntryDecision(
                False,
                f"entry price {signal.current_price:.3f} ≤ loss_threshold {loss_threshold:.3f}; "
                "would be stopped out immediately",
            )

        # Gate 4: Proximity score
        proximity = signal.metadata.get("proximity_score", 0)
        if proximity < min_proximity:
            return StrategyEntryDecision(
                False, f"proximity {proximity:.2f} < {min_proximity:.2f}"
            )

        # Gate 5: Re-verify momentum at entry time
        window_hours = int(params.get("momentum_window_hours", _DEFAULT_MOMENTUM_WINDOW_HOURS))
        min_momentum_pct = _to_float(params.get("min_momentum_pct", _DEFAULT_MIN_MOMENTUM_PCT))
        momentum = _fetch_btc_momentum(window_hours)
        if momentum is None:
            return StrategyEntryDecision(False, "momentum data unavailable at entry")
        if momentum["direction"] != signal.metadata.get("direction"):
            return StrategyEntryDecision(
                False,
                f"momentum direction changed: {signal.metadata.get('direction')} → {momentum['direction']}",
            )

        # Gate 6: Market exposure cap
        current_exposure = sum(
            _to_float(p.get("entry_shares"), 0) * _to_float(p.get("avg_entry_price"), 0)
            for p in open_positions
            if str(p.get("condition_id") or "").lower() == signal.condition_id.lower()
        )
        if current_exposure >= max_exposure:
            return StrategyEntryDecision(
                False, f"market exposure ${current_exposure:.0f} >= ${max_exposure:.0f}"
            )

        # Gate 7: Resolution date check
        max_days = int(params.get("max_days_to_resolution", 30))

        end_date_iso = str(signal.metadata.get("end_date_iso") or "")
        if end_date_iso:
            try:
                end_dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                days_left = (end_dt.date() - datetime.now(timezone.utc).date()).days
                if days_left < 0:
                    return StrategyEntryDecision(False, "market past end date")
                if days_left > max_days:
                    return StrategyEntryDecision(
                        False, f"resolves in {days_left}d > {max_days}d max"
                    )
            except Exception:
                pass

        direction = signal.metadata.get("direction")
        macro_bias = signal.metadata.get("macro_bias", "neutral")

        # Gate 8: Macro bias must not contradict trade direction.
        # We allow neutral macro; only block on explicit opposition.
        # (Param 'require_macro_alignment': True/False, default True)
        if params.get("require_macro_alignment", True):
            if macro_bias == "bearish" and direction == "up":
                return StrategyEntryDecision(
                    False,
                    f"macro_bias={macro_bias} contradicts direction={direction}; "
                    "situation_model shows BTC headwinds",
                )
            if macro_bias == "bullish" and direction == "down":
                return StrategyEntryDecision(
                    False,
                    f"macro_bias={macro_bias} contradicts direction={direction}; "
                    "situation_model shows BTC tailwinds",
                )

        # Signal 9: Funding rate (logged, not a hard gate).
        # Backtested on 28 real trades — funding was flat 0.01% for the entire
        # March 2026 test window, so it had zero discriminatory power.
        # Keeping it as a logged observation; thresholds need a longer sample
        # before it earns hard-gate status.
        funding_rate = signal.metadata.get("funding_rate_pct")

        # Signal 10: Order-book imbalance (logged, not a hard gate).
        # No historical depth snapshots exist to backtest against; cannot validate
        # thresholds. Kept as a logged signal for future analysis.
        book_imbalance = signal.metadata.get("book_imbalance")

        # Gate 11: Block entry when recent BTC news volume is excessive.
        # Backtest finding (28 trades): the March 23 losing cluster coincided
        # with 137+ news items in 24 h (fear/war coverage); the winning March 16
        # session had 0–3 news items.  High news volume is a regime indicator
        # that the market is already pricing in a directional move — the edge
        # is gone.  Hard ceiling: if news_total > max_news_items_24h, skip.
        # Default is 100 (conservative — blocks obvious panic/hype regimes).
        news_total = int(signal.metadata.get("news_total", 0))
        max_news = int(params.get("max_news_items_24h", 100))
        if news_total > max_news:
            return StrategyEntryDecision(
                False,
                f"{news_total} BTC news items in last 24 h exceeds max {max_news}; "
                "high-noise regime — market likely already priced in directional move",
            )

        market_headroom = max_exposure - current_exposure
        size_usdc = min(bet_size, market_headroom)
        size_usdc = max(1.0, size_usdc)

        funding_str = f"{funding_rate:.4f}%" if funding_rate is not None else "n/a"
        book_str = f"{book_imbalance:.2f}" if book_imbalance is not None else "n/a"

        return StrategyEntryDecision(
            True,
            f"crypto_momentum: BTC ${momentum['current_price']:,.0f} "
            f"{direction} {momentum['momentum_pct']:.1f}% "
            f"→ strike ${signal.metadata.get('strike_price', 0):,.0f} "
            f"(prox={proximity:.2f} macro={macro_bias} fund={funding_str} book={book_str})",
            size_usdc=size_usdc,
            entry_price=signal.current_price,
            execution_mode="paper",
            metadata={
                "direction": direction,
                "btc_price_at_entry": momentum["current_price"],
                "strike_price": signal.metadata.get("strike_price"),
                "distance_pct": signal.metadata.get("distance_pct"),
                "proximity_score": proximity,
                "momentum_pct": momentum["momentum_pct"],
                "short_momentum_pct": momentum["short_momentum_pct"],
                "macro_bias": macro_bias,
                "funding_rate_pct": funding_rate,
                "book_imbalance": book_imbalance,
                "news_total_24h": news_total,
            },
        )

    def manage(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
    ) -> StrategyManageState:
        """Check market status and fetch live price."""
        condition_id = str(position.get("condition_id") or "")
        outcome = str(position.get("outcome") or "Yes")
        entry_price = _to_float(position.get("avg_entry_price"), 0.5)

        row = conn.execute(
            "SELECT is_open FROM market_metadata WHERE condition_id = ? LIMIT 1",
            (condition_id,),
        ).fetchone()
        market_open = bool(int(row["is_open"] or 0)) if row is not None else True

        live_price = fetch_live_clob_price(condition_id, outcome)
        current_price = live_price if live_price is not None else entry_price

        return StrategyManageState(
            current_price=current_price,
            signal_open=market_open,
            metadata={},
        )

    def exit(
        self,
        conn: sqlite3.Connection,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        """Exit rules for crypto momentum positions."""
        params = _parse_params(strategy)
        stale_days = int(params.get("stale_days", _DEFAULT_STALE_DAYS))
        win_threshold = _to_float(params.get("win_threshold", _DEFAULT_WIN_THRESHOLD))
        loss_threshold = _to_float(params.get("loss_threshold", _DEFAULT_LOSS_THRESHOLD))
        take_profit_roi = _to_float(params.get("take_profit_roi", _DEFAULT_TAKE_PROFIT_ROI))

        current_price = _to_float(state.current_price, _to_float(position.get("avg_entry_price")))
        entry_price = _to_float(position.get("avg_entry_price"), 0.0)
        opened_ts = int(position.get("opened_ts") or 0)
        age_secs = max(0, _now_ts() - opened_ts) if opened_ts else 0
        age_days = age_secs // 86400

        # Min-hold guard: never exit within the first 60 s of opening.
        # Prevents same-run-cycle phantom closes when the live price feed
        # falls back to entry_price and immediately fires loss_threshold.
        if age_secs < 60:
            return StrategyExitDecision(False, "min_hold", exit_price=current_price, status="open")

        if not state.signal_open:
            return StrategyExitDecision(True, "market_closed", exit_price=current_price, status="closed")

        if current_price >= win_threshold:
            return StrategyExitDecision(True, "early_close_win", exit_price=current_price, status="closed")

        if current_price <= loss_threshold:
            return StrategyExitDecision(True, "early_close_loss", exit_price=current_price, status="closed")

        if entry_price > 0:
            roi = (current_price - entry_price) / entry_price
            if roi >= take_profit_roi:
                return StrategyExitDecision(True, "take_profit", exit_price=current_price, status="closed")

        if age_days >= stale_days:
            return StrategyExitDecision(True, f"stale_{age_days}d", exit_price=current_price, status="closed")

        return StrategyExitDecision(False, "hold", exit_price=current_price, status="open")

    def report(
        self, conn: sqlite3.Connection, strategy: Dict[str, Any], positions: List[Dict[str, Any]]
    ) -> StrategyReport:
        return StrategyReport(summary={"position_count": len(positions)})

    def replay_adapter(self):
        return None
