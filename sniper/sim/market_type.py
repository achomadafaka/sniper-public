"""
Canonical market type classifier.

One function. One place. Every consumer uses this.
Do not re-derive market types in study scripts, agents, or queries.
"""

import re

# Kalshi series prefix → type
_KALSHI_PREFIX_MAP = {
    "KXBTC": "crypto_threshold",
    "KXETH": "crypto_threshold",
    "KXSOL": "crypto_threshold",
    "KXDOGE": "crypto_threshold",
    "KXXRP": "crypto_threshold",
    "KXHYPE": "crypto_threshold",
    "KXBTCD": "crypto_threshold",
    "KXHIGH": "weather_temp",
    "KXLOW": "weather_temp",
    "KXNCAAF": "sports_ncaaf",
    "KXNCAAM": "sports_ncaam",
    "KXNFL": "sports_nfl",
    "KXNBA": "sports_nba",
    "KXMLB": "sports_mlb",
    "KXNHL": "sports_nhl",
    "KXMLS": "sports_mls",
    "KXFED": "economics_fed",
    "KXCPI": "economics_cpi",
    "KXGDP": "economics_gdp",
    "KXJOB": "economics_jobs",
    "KXNEXTIRANLEADER": "geopolitical",
    "KXMVE": "sports_multi",
    "KXIPO": "corporate",
}

# Polymarket question patterns → type
# Order matters: topic-specific patterns BEFORE structural patterns.
# "Russia ceasefire in 2025?" is geopolitical, not timebound.
_PM_PATTERNS = [
    # 1. Specific asset/instrument patterns (most specific)
    (re.compile(r"price of .+(?:above|below|between)", re.I), "crypto_threshold"),
    (re.compile(r"Up or Down", re.I), "crypto_updown"),
    (re.compile(r"FDV|market cap", re.I), "valuation_target"),
    (re.compile(r"Spread:|O/U |Over/Under", re.I), "sports_spread_ou"),
    (re.compile(r"vs\.|vs ", re.I), "matchup"),
    # 2. Topic-specific patterns (before structural)
    (re.compile(r"tweet|Musk|Elon", re.I), "social_media"),
    (re.compile(r"Iran|Russia|Ukraine|war|Israel|military|strike|ceasefire", re.I), "geopolitical"),
    (re.compile(r"Trump|election|president|Congress|governor|Senate", re.I), "politics_us"),
    (re.compile(r"Fed |interest rate", re.I), "economics_fed"),
    (re.compile(r"GDP", re.I), "economics_gdp"),
    (re.compile(r"CPI|inflation", re.I), "economics_cpi"),
    (re.compile(r"tariff|trade war", re.I), "economics_trade"),
    (re.compile(r"temperature|weather|hurricane", re.I), "weather_temp"),
    (re.compile(r"launch|token", re.I), "crypto_launch"),
    # 3. Structural patterns (most generic — checked last)
    (re.compile(r"(?:by |before )\w+ \d", re.I), "deadline_event"),
    (re.compile(r"in 20\d\d\?|this year\?|this month\?", re.I), "timebound_event"),
    (re.compile(r"(?:win |reach |hit |record)", re.I), "achievement"),
]


def classify_kalshi(ticker: str) -> str:
    """Classify a Kalshi market by its ticker prefix. Longest prefix wins."""
    best_prefix = ""
    best_type = "other"
    for prefix, mtype in _KALSHI_PREFIX_MAP.items():
        if ticker.startswith(prefix) and len(prefix) > len(best_prefix):
            best_prefix = prefix
            best_type = mtype
    return best_type


def classify_polymarket(question: str) -> str:
    """Classify a Polymarket market by its question text."""
    if not question:
        return "other"
    for pattern, mtype in _PM_PATTERNS:
        if pattern.search(question):
            return mtype
    return "other"


# Coarse group mapping — for when you need category-level aggregation
_TYPE_TO_GROUP = {
    "crypto_threshold": "crypto",
    "crypto_updown": "crypto",
    "crypto_launch": "crypto",
    "valuation_target": "crypto",
    "matchup": "sports",
    "sports_spread_ou": "sports",
    "sports_ncaaf": "sports",
    "sports_ncaam": "sports",
    "sports_nfl": "sports",
    "sports_nba": "sports",
    "sports_mlb": "sports",
    "sports_nhl": "sports",
    "sports_mls": "sports",
    "sports_multi": "sports",
    "social_media": "social",
    "geopolitical": "geopolitical",
    "politics_us": "politics",
    "economics_fed": "economics",
    "economics_cpi": "economics",
    "economics_gdp": "economics",
    "economics_jobs": "economics",
    "economics_trade": "economics",
    "weather_temp": "weather",
    "corporate": "corporate",
    "deadline_event": "structural",
    "timebound_event": "structural",
    "achievement": "structural",
}


def group(market_type: str) -> str:
    """Map a specific market_type to its coarse group."""
    return _TYPE_TO_GROUP.get(market_type, "other")


def classify(venue: str, identifier: str) -> str:
    """
    Classify a market type from any venue.

    Args:
        venue: "kalshi" or "polymarket"
        identifier: ticker (kalshi) or question text (polymarket)

    Returns:
        Canonical market type string.
    """
    if venue == "kalshi":
        return classify_kalshi(identifier)
    if venue == "polymarket":
        return classify_polymarket(identifier)
    return "other"
