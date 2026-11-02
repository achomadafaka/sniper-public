#!/usr/bin/env python3
"""
Reference Price Aggregator — independent prior probability before touching Polymarket prices.

Root problem: the bot anchors entirely to Polymarket's retail crowd price. This module
establishes an independent prior from external reference sources, then quantifies how
much Polymarket diverges from that prior.

Usage:
    from scripts.reference_price import get_reference_assessment
    result = get_reference_assessment(
        market_title="Will the Los Angeles Lakers beat the Houston Rockets?",
        condition_id="abc123",
        outcome="YES",
        poly_price=0.52,
        category="sports_individual_matchup",
    )

Sources used (in priority order):
    1. Kalshi KXNBAGAME / KXNFLGAME cross-reference (direct competitor market)
    2. Polymarket historical calibration (empirical bias correction)
    3. Hyperliquid funding signal (crypto markets only)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from typing import Any

logger = logging.getLogger(__name__)

# ── In-memory caches ────────────────────────────────────────────────────────
_KALSHI_CACHE: dict[str, Any] = {}        # ticker → market dict
_KALSHI_CACHE_TIME: float = 0.0
_KALSHI_CACHE_TTL: float = 180.0          # 3 minutes — Kalshi prices update often

_CALIBRATION_CACHE: dict[str, Any] = {}   # loaded from Supabase brain
_CALIBRATION_CACHE_TIME: float = 0.0
_CALIBRATION_CACHE_TTL: float = 3600.0    # 1 hour

_HL_CACHE: dict[str, Any] = {}            # asset → signal dict
_HL_CACHE_TIME: float = 0.0
_HL_CACHE_TTL: float = 120.0              # 2 minutes

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
HYPERLIQUID_API = "https://api.hyperliquid.xyz/info"
REQUEST_TIMEOUT = 8  # seconds


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = REQUEST_TIMEOUT) -> dict | list | None:
    """Simple JSON GET with timeout. Returns parsed response or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sniper-reference/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug("HTTP GET failed %s: %s", url[:80], e)
        return None


def _http_post(url: str, payload: dict, timeout: int = REQUEST_TIMEOUT) -> dict | list | None:
    """Simple JSON POST with timeout. Returns parsed response or None on failure."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "sniper-reference/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.debug("HTTP POST failed %s: %s", url[:80], e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# A. Kalshi Cross-Reference
# ─────────────────────────────────────────────────────────────────────────────

# Maps common NBA/NFL team name fragments to their Kalshi ticker abbreviations.
# These appear in Kalshi's KXNBAGAME-YYMMDDTEAM1TEAM2-TEAM format.
_NBA_TEAM_ABBREVS = {
    # Full name fragments → Kalshi suffix as it appears in ticker/title
    "lakers": ("LAL", "Los Angeles L"),
    "clippers": ("LAC", "Los Angeles C"),
    "warriors": ("GSW", "Golden State"),
    "celtics": ("BOS", "Boston"),
    "bucks": ("MIL", "Milwaukee"),
    "heat": ("MIA", "Miami"),
    "bulls": ("CHI", "Chicago"),
    "knicks": ("NYK", "New York"),
    "nets": ("BKN", "Brooklyn"),
    "76ers": ("PHI", "Philadelphia"),
    "sixers": ("PHI", "Philadelphia"),
    "raptors": ("TOR", "Toronto"),
    "cavaliers": ("CLE", "Cleveland"),
    "pistons": ("DET", "Detroit"),
    "pacers": ("IND", "Indiana"),
    "hawks": ("ATL", "Atlanta"),
    "hornets": ("CHA", "Charlotte"),
    "magic": ("ORL", "Orlando"),
    "wizards": ("WAS", "Washington"),
    "nuggets": ("DEN", "Denver"),
    "thunder": ("OKC", "Oklahoma City"),
    "blazers": ("POR", "Portland"),
    "jazz": ("UTA", "Utah"),
    "suns": ("PHX", "Phoenix"),
    "kings": ("SAC", "Sacramento"),
    "spurs": ("SAS", "San Antonio"),
    "mavericks": ("DAL", "Dallas"),
    "rockets": ("HOU", "Houston"),
    "grizzlies": ("MEM", "Memphis"),
    "pelicans": ("NOP", "New Orleans"),
    "timberwolves": ("MIN", "Minnesota"),
    "wolves": ("MIN", "Minnesota"),
}

_NFL_TEAM_ABBREVS = {
    "chiefs": ("KC", "Kansas City"),
    "eagles": ("PHI", "Philadelphia"),
    "cowboys": ("DAL", "Dallas"),
    "patriots": ("NE", "New England"),
    "bills": ("BUF", "Buffalo"),
    "dolphins": ("MIA", "Miami"),
    "jets": ("NYJ", "New York J"),
    "ravens": ("BAL", "Baltimore"),
    "steelers": ("PIT", "Pittsburgh"),
    "browns": ("CLE", "Cleveland"),
    "bengals": ("CIN", "Cincinnati"),
    "texans": ("HOU", "Houston"),
    "colts": ("IND", "Indianapolis"),
    "titans": ("TEN", "Tennessee"),
    "jaguars": ("JAX", "Jacksonville"),
    "packers": ("GB", "Green Bay"),
    "bears": ("CHI", "Chicago"),
    "vikings": ("MIN", "Minnesota"),
    "lions": ("DET", "Detroit"),
    "giants": ("NYG", "New York G"),
    "commanders": ("WAS", "Washington"),
    "49ers": ("SF", "San Francisco"),
    "seahawks": ("SEA", "Seattle"),
    "rams": ("LAR", "Los Angeles R"),
    "cardinals": ("ARI", "Arizona"),
    "buccaneers": ("TB", "Tampa Bay"),
    "saints": ("NO", "New Orleans"),
    "falcons": ("ATL", "Atlanta"),
    "panthers": ("CAR", "Carolina"),
    "broncos": ("DEN", "Denver"),
    "raiders": ("LV", "Las Vegas"),
    "chargers": ("LAC", "Los Angeles C"),
    "chiefs": ("KC", "Kansas City"),
}


def _extract_teams_from_title(market_title: str) -> list[str]:
    """Extract probable team/entity names from a market title."""
    title_lower = market_title.lower()
    found = []
    for team_frag in list(_NBA_TEAM_ABBREVS.keys()) + list(_NFL_TEAM_ABBREVS.keys()):
        if team_frag in title_lower:
            found.append(team_frag)
    return found


def _load_kalshi_nba_markets() -> dict[str, Any]:
    """Load and cache current NBA game markets from Kalshi KXNBAGAME series.

    Returns a dict mapping (title_fragment → market_data) for fast lookup.
    """
    global _KALSHI_CACHE, _KALSHI_CACHE_TIME
    now = time.time()
    if now - _KALSHI_CACHE_TIME < _KALSHI_CACHE_TTL and _KALSHI_CACHE:
        return _KALSHI_CACHE

    cache: dict[str, Any] = {}
    for series in ("KXNBAGAME", "KXNFLGAME", "KXWNBAGAME"):
        url = f"{KALSHI_API}/markets?limit=50&series_ticker={series}"
        data = _http_get(url)
        if not data or "markets" not in data:
            continue
        for mkt in data["markets"]:
            ticker = mkt.get("ticker", "")
            title = mkt.get("title", "")
            yes_bid = mkt.get("yes_bid_dollars")
            yes_ask = mkt.get("yes_ask_dollars")
            status = mkt.get("status", "")
            close_time = mkt.get("close_time", "")

            if status not in ("active",):
                continue
            if yes_bid is None or yes_ask is None:
                continue
            try:
                bid = float(yes_bid)
                ask = float(yes_ask)
            except (TypeError, ValueError):
                continue
            if bid <= 0 and ask <= 0:
                continue

            cache[ticker] = {
                "ticker": ticker,
                "title": title,
                "yes_bid": bid,
                "yes_ask": ask,
                "yes_mid": round((bid + ask) / 2, 4),
                "close_time": close_time,
                "series": series,
            }

    _KALSHI_CACHE = cache
    _KALSHI_CACHE_TIME = now
    logger.debug("Loaded %d Kalshi game markets", len(cache))
    return cache


def _fetch_kalshi_price(market_title: str, outcome: str) -> dict | None:
    """Match a Polymarket market title to a Kalshi game market using team name extraction.

    Strategy:
      1. Extract team name fragments from market_title.
      2. Search cached KXNBAGAME / KXNFLGAME markets for title overlap.
      3. Return the YES price for the team corresponding to `outcome`.

    Returns:
        {
          'kalshi_ticker': str,
          'kalshi_yes_price': float,    # mid of bid/ask for YES (home team wins)
          'kalshi_volume': str,
          'confidence': 'high'|'medium'|'low',
          'matched_title': str,
        }
        or None if no match found.
    """
    title_lower = market_title.lower()
    outcome_upper = outcome.upper()

    # Quick abort: if outcome is clearly YES/NO for a generic binary question,
    # we need to match to the home/away team outcome.
    markets = _load_kalshi_nba_markets()
    if not markets:
        return None

    # 1. Direct team name matching from title
    matched_teams = _extract_teams_from_title(market_title)
    if not matched_teams:
        # Attempt keyword-based Kalshi search as fallback
        # Extract the first capitalised multi-word entity
        words = re.findall(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*", market_title)
        search_kw = words[0] if words else None
        if search_kw and len(search_kw) > 3:
            url = f"{KALSHI_API}/markets?limit=10&keyword={urllib.parse.quote(search_kw)}"
            extra = _http_get(url)
            if extra and "markets" in extra:
                for mkt in extra["markets"]:
                    ticker = mkt.get("ticker", "")
                    t = mkt.get("title", "")
                    yes_bid = mkt.get("yes_bid_dollars")
                    yes_ask = mkt.get("yes_ask_dollars")
                    if yes_bid and yes_ask:
                        try:
                            bid, ask = float(yes_bid), float(yes_ask)
                            if bid > 0 or ask > 0:
                                mid = round((bid + ask) / 2, 4)
                                return {
                                    "kalshi_ticker": ticker,
                                    "kalshi_yes_price": mid,
                                    "kalshi_volume": mkt.get("volume_fp", "?"),
                                    "confidence": "low",
                                    "matched_title": t,
                                }
                        except (TypeError, ValueError):
                            pass
        return None

    # Build a mapping of team_frag → Kalshi display name fragment for title matching.
    # _NBA_TEAM_ABBREVS values are (ticker_abbrev, kalshi_display_fragment).
    # E.g. "lakers" → ("LAL", "Los Angeles L"), so we match "los angeles l" in
    # Kalshi titles rather than the fan nickname "lakers".
    _all_abbrevs = {**_NBA_TEAM_ABBREVS, **_NFL_TEAM_ABBREVS}
    # matched_teams contains fan-nickname fragments; map them to Kalshi display names
    matched_kalshi_display: list[str] = []
    matched_ticker_abbrevs: list[str] = []
    for team_frag in matched_teams:
        if team_frag in _all_abbrevs:
            abbrev, display = _all_abbrevs[team_frag]
            matched_kalshi_display.append(display.lower())
            matched_ticker_abbrevs.append(abbrev.upper())

    if not matched_kalshi_display:
        return None

    # 2. Look for Kalshi markets whose title contains the same teams
    best_match = None
    best_score = 0

    for ticker, mkt in markets.items():
        kalshi_title_lower = mkt["title"].lower()
        # Score by how many of our teams appear in the Kalshi title
        score = sum(1 for disp in matched_kalshi_display if disp.lower() in kalshi_title_lower)
        if score > best_score:
            best_score = score
            best_match = mkt

    if not best_match or best_score == 0:
        return None

    confidence = "high" if best_score >= 2 else "medium" if best_score == 1 else "low"

    # 3. Determine which side of the Kalshi market corresponds to our outcome.
    # The Kalshi ticker ends with the team ticker abbreviation:
    #   KXNBAGAME-26MAR18LALHOU-LAL  → YES = LAL (Lakers win)
    #   KXNBAGAME-26MAR18LALHOU-HOU  → YES = HOU (Rockets win)
    #
    # For Polymarket outcome="YES", we want the probability the specified team wins.
    # We need to find which Kalshi ticker matches the team our Polymarket market is
    # asking about (the team in the subject of the question).
    #
    # Strategy: extract the first team mentioned in the Polymarket title (the subject),
    # find its ticker abbreviation, then look for the Kalshi market whose suffix matches.
    yes_price = best_match["yes_mid"]

    # Identify the subject team (first team in the title) and its Kalshi ticker
    subject_team_frag = matched_teams[0] if matched_teams else None
    subject_abbrev = matched_ticker_abbrevs[0] if matched_ticker_abbrevs else None

    if subject_abbrev:
        # Look for the Kalshi market where the ticker suffix = subject_abbrev
        subject_mkt = None
        for ticker, mkt in markets.items():
            if mkt["title"].lower() == best_match["title"].lower():
                # Same game — check if this side's ticker suffix matches our team
                ticker_suffix = ticker.split("-")[-1].upper()
                if ticker_suffix == subject_abbrev:
                    subject_mkt = mkt
                    break
        if subject_mkt:
            yes_price = subject_mkt["yes_mid"]
            best_match = subject_mkt

        # For "NO" outcome on Polymarket: invert
        if outcome_upper == "NO":
            yes_price = round(1.0 - yes_price, 4)

    return {
        "kalshi_ticker": best_match["ticker"],
        "kalshi_yes_price": yes_price,
        "kalshi_volume": best_match.get("volume_fp", "?"),
        "confidence": confidence,
        "matched_title": best_match["title"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# B. Polymarket Historical Calibration
# ─────────────────────────────────────────────────────────────────────────────

def _load_calibration_data() -> dict[str, Any]:
    """Load Polymarket calibration data from Supabase brain.

    Returns cached calibration dict or empty dict if unavailable.
    """
    global _CALIBRATION_CACHE, _CALIBRATION_CACHE_TIME
    now = time.time()
    if now - _CALIBRATION_CACHE_TIME < _CALIBRATION_CACHE_TTL and _CALIBRATION_CACHE:
        return _CALIBRATION_CACHE

    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from bot.supabase_helper import get_memory
        entry = get_memory("polymarket_calibration_data", type_filter="knowledge")
        if entry:
            raw = entry.get("content", "{}")
            data = json.loads(raw) if isinstance(raw, str) else {}
            if data:
                _CALIBRATION_CACHE = data
                _CALIBRATION_CACHE_TIME = now
                logger.debug("Loaded Polymarket calibration data (%d entries)", len(data))
                return data
    except Exception as e:
        logger.debug("Could not load calibration data from Supabase: %s", e)

    _CALIBRATION_CACHE_TIME = now
    return _CALIBRATION_CACHE


def _get_polymarket_calibration(category: str, price_bucket: float) -> dict:
    """Return empirical calibration stats for a category + price bucket.

    Price bucket is the Polymarket YES price rounded to nearest 0.10.
    A calibration_bias > 0 means Polymarket overprices at this bucket (market says
    0.30 but only 25% resolve YES → bias = +0.05).

    Returns:
        {
            'actual_resolution_rate': float,
            'sample_size': int,
            'calibration_bias': float,  # poly_price - actual_rate (+ = overpriced)
            'confidence': 'high'|'medium'|'low'|'insufficient',
        }
    """
    cal = _load_calibration_data()
    bucket_str = f"{round(price_bucket, 1):.1f}"

    # Try specific category first, then generic "all" bucket
    key = f"{category}|{bucket_str}"
    if key not in cal:
        key = f"all|{bucket_str}"
    if key not in cal:
        return {
            "actual_resolution_rate": None,
            "sample_size": 0,
            "calibration_bias": None,
            "confidence": "insufficient",
        }

    entry = cal[key]
    n = int(entry.get("n", 0))
    rate = float(entry.get("yes_resolution_rate", 0))
    confidence = "high" if n >= 50 else "medium" if n >= 20 else "low" if n >= 5 else "insufficient"

    return {
        "actual_resolution_rate": rate,
        "sample_size": n,
        "calibration_bias": round(price_bucket - rate, 4) if n > 0 else None,
        "confidence": confidence,
    }


# ─────────────────────────────────────────────────────────────────────────────
# C. Hyperliquid Funding Signal (crypto markets only)
# ─────────────────────────────────────────────────────────────────────────────

_CRYPTO_KEYWORDS = {
    "btc": "BTC", "bitcoin": "BTC",
    "eth": "ETH", "ethereum": "ETH",
    "sol": "SOL", "solana": "SOL",
    "trump": "TRUMP",
    "xrp": "XRP",
    "doge": "DOGE", "dogecoin": "DOGE",
    "bnb": "BNB",
    "avax": "AVAX", "avalanche": "AVAX",
}


def _detect_crypto_asset(market_title: str) -> str | None:
    """Return the Hyperliquid asset ticker if market_title is a crypto price-range market."""
    title_lower = market_title.lower()
    for kw, asset in _CRYPTO_KEYWORDS.items():
        if re.search(r'\b' + re.escape(kw) + r'\b', title_lower):
            return asset
    return None


def _load_hl_data() -> dict[str, Any]:
    """Load and cache Hyperliquid asset context data."""
    global _HL_CACHE, _HL_CACHE_TIME
    now = time.time()
    if now - _HL_CACHE_TIME < _HL_CACHE_TTL and _HL_CACHE:
        return _HL_CACHE

    data = _http_post(HYPERLIQUID_API, {"type": "metaAndAssetCtxs"})
    if not data or len(data) < 2:
        _HL_CACHE_TIME = now
        return _HL_CACHE

    assets = data[0].get("universe", [])
    ctxs = data[1]
    cache = {}
    for asset_meta, ctx in zip(assets, ctxs):
        name = asset_meta.get("name", "")
        if not name:
            continue
        try:
            funding = float(ctx.get("funding", 0))
            mark_px = float(ctx.get("markPx", 0))
            oi = float(ctx.get("openInterest", 0))
        except (TypeError, ValueError):
            continue
        cache[name] = {
            "funding_rate_hr": funding,         # per-hour rate
            "mark_price": mark_px,
            "open_interest": oi,
        }

    _HL_CACHE = cache
    _HL_CACHE_TIME = now
    logger.debug("Loaded Hyperliquid data for %d assets", len(cache))
    return cache


def _get_hyperliquid_crypto_signal(market_title: str) -> dict | None:
    """Return a funding-rate directional signal for crypto price-range markets.

    For "Will BTC be above $X?" markets, positive funding = longs dominant =
    bullish bias. Negative = shorts dominant = bearish bias.

    Returns:
        {
            'asset': str,
            'funding_rate_8h': float,    # 8h equivalent rate (rate * 8)
            'funding_annualized_pct': float,
            'funding_sentiment': 'bullish'|'bearish'|'neutral',
            'mark_price': float,
            'signal_strength': float,    # 0.0-1.0
        }
        or None if not a crypto market or data unavailable.
    """
    asset = _detect_crypto_asset(market_title)
    if not asset:
        return None

    hl_data = _load_hl_data()
    if asset not in hl_data:
        return None

    ctx = hl_data[asset]
    funding_hr = ctx["funding_rate_hr"]
    funding_8h = funding_hr * 8
    funding_ann_pct = funding_hr * 3 * 365 * 100  # annualized %

    BULLISH_THRESHOLD = 0.0001  # 0.01% per hour
    BEARISH_THRESHOLD = -0.0001

    if funding_hr > BULLISH_THRESHOLD:
        sentiment = "bullish"
        # Scale: 0.0001 = 0.3 strength, 0.001 = 1.0 strength
        strength = min(1.0, abs(funding_hr) / 0.001)
    elif funding_hr < BEARISH_THRESHOLD:
        sentiment = "bearish"
        strength = min(1.0, abs(funding_hr) / 0.001)
    else:
        sentiment = "neutral"
        strength = 0.0

    return {
        "asset": asset,
        "funding_rate_8h": round(funding_8h * 100, 5),  # as percentage
        "funding_annualized_pct": round(funding_ann_pct, 2),
        "funding_sentiment": sentiment,
        "mark_price": ctx["mark_price"],
        "signal_strength": round(strength, 3),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main: Reference Assessment
# ─────────────────────────────────────────────────────────────────────────────

def get_reference_assessment(
    market_title: str,
    condition_id: str,
    outcome: str,
    poly_price: float,
    category: str = "unknown",
) -> dict:
    """Compute an independent reference prior for a Polymarket market.

    Aggregates Kalshi cross-reference, historical calibration bias, and Hyperliquid
    funding signals into a single structured assessment with a trade recommendation.

    Args:
        market_title:  Polymarket question text (e.g. "Will the Lakers beat the Rockets?")
        condition_id:  Polymarket condition ID (used only for logging/context)
        outcome:       "YES", "NO", or team name
        poly_price:    Current Polymarket YES price (0.0 – 1.0)
        category:      Category string from market_exploitability() (e.g. "sports_individual_matchup")

    Returns:
        {
            'has_reference': bool,
            'reference_price': float | None,        # independent prior probability
            'divergence': float | None,             # poly_price - reference_price
            'divergence_direction': str,            # 'poly_overpriced'|'poly_underpriced'|'aligned'
            'edge_estimate': float | None,          # expected value if reference is correct
            'sources_used': list[str],
            'recommendation': str,                  # 'enter'|'avoid'|'investigate'|'no_reference'
            'notes': str,
            'kalshi': dict | None,
            'calibration': dict | None,
            'hyperliquid': dict | None,
        }
    """
    sources_used: list[str] = []
    notes_parts: list[str] = []
    reference_prices: list[float] = []
    weights: list[float] = []

    # ── Source A: Kalshi ─────────────────────────────────────────────────────
    kalshi_result = None
    try:
        kalshi_result = _fetch_kalshi_price(market_title, outcome)
    except Exception as e:
        logger.debug("Kalshi fetch error: %s", e)

    kalshi_price = None
    if kalshi_result:
        sources_used.append("kalshi")
        kalshi_price = kalshi_result["kalshi_yes_price"]
        conf = kalshi_result["confidence"]
        weight = {"high": 0.8, "medium": 0.5, "low": 0.3}.get(conf, 0.3)
        reference_prices.append(kalshi_price)
        weights.append(weight)
        notes_parts.append(
            f"Kalshi={kalshi_price:.3f} ({conf}) [{kalshi_result['kalshi_ticker']}]"
        )
        logger.debug(
            "Kalshi match: %s → %s = %.3f (conf=%s)",
            market_title[:50], kalshi_result["kalshi_ticker"], kalshi_price, conf,
        )

    # ── Source B: Historical Calibration ─────────────────────────────────────
    price_bucket = round(round(poly_price / 0.10) * 0.10, 1)
    calibration_result = _get_polymarket_calibration(category, price_bucket)

    cal_adjusted_price = None
    if calibration_result["confidence"] not in ("insufficient",) and calibration_result["actual_resolution_rate"] is not None:
        sources_used.append("calibration")
        cal_adjusted_price = calibration_result["actual_resolution_rate"]
        bias = calibration_result["calibration_bias"]
        conf = calibration_result["confidence"]
        weight = {"high": 0.4, "medium": 0.25, "low": 0.1}.get(conf, 0.1)
        reference_prices.append(cal_adjusted_price)
        weights.append(weight)
        notes_parts.append(
            f"CalBias={bias:+.3f} (n={calibration_result['sample_size']}, {conf})"
        )

    # ── Source C: Hyperliquid (crypto only) ───────────────────────────────────
    hl_result = None
    try:
        hl_result = _get_hyperliquid_crypto_signal(market_title)
    except Exception as e:
        logger.debug("Hyperliquid signal error: %s", e)

    if hl_result and hl_result.get("signal_strength", 0) > 0.1:
        sources_used.append("hyperliquid")
        sentiment = hl_result["funding_sentiment"]
        strength = hl_result["signal_strength"]
        # Translate funding sentiment to a probability nudge.
        # Bullish funding → market likely continues up → YES price gets nudged up.
        # We only note this; don't include it in the weighted reference price
        # unless we have no other sources.
        notes_parts.append(
            f"HL-funding={hl_result['funding_rate_8h']:+.4f}%/8h ({sentiment}, strength={strength:.2f})"
        )
        if not reference_prices:
            # No other reference — synthesize a weak signal from funding direction
            nudge = 0.03 * strength * (1 if sentiment == "bullish" else -1)
            hl_ref_price = max(0.01, min(0.99, poly_price + nudge))
            reference_prices.append(hl_ref_price)
            weights.append(0.15)

    # ── Aggregate reference price ─────────────────────────────────────────────
    has_reference = len(reference_prices) > 0
    reference_price: float | None = None
    divergence: float | None = None
    divergence_direction = "unknown"
    edge_estimate: float | None = None

    if has_reference:
        total_weight = sum(weights)
        reference_price = round(sum(p * w for p, w in zip(reference_prices, weights)) / total_weight, 4)
        divergence = round(poly_price - reference_price, 4)

        if abs(divergence) < 0.02:
            divergence_direction = "aligned"
        elif divergence > 0:
            divergence_direction = "poly_overpriced"
        else:
            divergence_direction = "poly_underpriced"

        # Edge estimate: if we're fading the overpriced side (NO bet on overpriced YES),
        # or buying the underpriced side (YES bet on underpriced YES).
        # For now: edge ≈ abs(divergence) * quality_factor
        quality_factor = min(1.0, total_weight / max(1.0, len(weights)))
        edge_estimate = round(abs(divergence) * quality_factor, 4)

    # ── Recommendation ────────────────────────────────────────────────────────
    # Logic (for all strategies):
    #   aligned (|div| < 0.02)      → AVOID — both retail venues agree, no edge
    #   moderate divergence (2-5¢)  → INVESTIGATE — noteworthy but not conclusive
    #   strong divergence (5-8¢)    → ENTER — meaningful mispricing
    #   very strong (>8¢)           → ENTER with confidence
    #
    # Special case for insider_copy (YES entries):
    #   poly > ref by >5¢ → insider is in an already-overpriced market → flag
    #   poly < ref by >5¢ → double confirmation (insider + reference agree) → size up

    recommendation = "no_reference"
    if has_reference and divergence is not None:
        abs_div = abs(divergence)
        if abs_div < 0.02:
            recommendation = "avoid"
        elif abs_div < 0.05:
            recommendation = "investigate"
        elif abs_div < 0.08:
            recommendation = "enter"
        else:
            recommendation = "enter"  # strong divergence

        # Modifier: calibration-only with low confidence stays at investigate
        if sources_used == ["calibration"] and calibration_result.get("confidence") == "low":
            if recommendation == "enter":
                recommendation = "investigate"

    notes = "; ".join(notes_parts) if notes_parts else "no reference sources available"

    result = {
        "has_reference": has_reference,
        "reference_price": reference_price,
        "divergence": divergence,
        "divergence_direction": divergence_direction,
        "edge_estimate": edge_estimate,
        "sources_used": sources_used,
        "recommendation": recommendation,
        "notes": notes,
        "kalshi": kalshi_result,
        "calibration": calibration_result if sources_used and "calibration" in sources_used else None,
        "hyperliquid": hl_result,
    }

    if has_reference:
        logger.info(
            "RefPrice %s: poly=%.3f ref=%.3f div=%+.3f → %s [%s]",
            market_title[:45],
            poly_price,
            reference_price,
            divergence,
            recommendation,
            ", ".join(sources_used),
        )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# CLI smoke-test
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    import pprint
    test_cases = [
        ("Will the Los Angeles Lakers beat the Houston Rockets?", "YES", 0.52, "sports_individual_matchup"),
        ("Will the Denver Nuggets beat the Memphis Grizzlies?", "YES", 0.87, "sports_individual_matchup"),
        ("Will the Atlanta Hawks beat the Dallas Mavericks?", "YES", 0.65, "sports_individual_matchup"),
        ("Will Bitcoin be above $80,000 on March 20?", "YES", 0.45, "crypto/commodity"),
    ]
    for title, outcome, price, cat in test_cases:
        print(f"\n{'='*70}")
        print(f"MARKET: {title}")
        print(f"POLY PRICE: {price:.3f} | OUTCOME: {outcome}")
        result = get_reference_assessment(title, "test", outcome, price, cat)
        pprint.pprint({k: v for k, v in result.items() if k not in ("kalshi", "calibration", "hyperliquid")})
        if result.get("kalshi"):
            print("  kalshi:", result["kalshi"])
        if result.get("hyperliquid"):
            print("  hl:", result["hyperliquid"])
