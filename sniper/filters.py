import re
from datetime import datetime, timezone
from typing import Iterable, Optional, Tuple


# ---------------------------------------------------------------------------
# Shared date utility (used by strategy plugins — lives here to avoid circular
# import with strategy_runner, which itself imports strategies.registry)
# ---------------------------------------------------------------------------

def days_to_resolution(end_date_iso: Optional[str]) -> Optional[int]:
    """Return calendar days from today to the market resolution date.

    Returns None if end_date_iso is absent or unparseable.
    Negative values mean the market is already past its resolution date.
    """
    if not end_date_iso:
        return None
    try:
        end_dt = datetime.fromisoformat(str(end_date_iso).replace("Z", "+00:00"))
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        return (end_dt.date() - datetime.now(timezone.utc).date()).days
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Shared sizing utility (used by political_no_value_v1 and sports_no_value_v1)
# ---------------------------------------------------------------------------

def kelly_size(
    yes_price: float,
    estimated_win_prob: float,
    bankroll: float,
    kelly_fraction: float,
    min_size: float,
    max_size: float,
) -> float:
    """Kelly criterion for a NO position where win_prob > (1 - yes_price).

    Edge = win_prob * yes_price - (1 - win_prob) * (1 - yes_price)
    Full Kelly fraction = edge / yes_price
    Apply kelly_fraction (default 0.10 = 1/10 Kelly for safety).
    Returns 0.0 when there is no edge (do not bet).
    """
    no_price = 1.0 - yes_price
    if no_price <= 0 or yes_price <= 0:
        return min_size
    edge = estimated_win_prob * yes_price - (1.0 - estimated_win_prob) * no_price
    if edge <= 0:
        return 0.0
    full_kelly = edge / yes_price
    size = bankroll * full_kelly * kelly_fraction
    return max(min_size, min(max_size, round(size, 2)))

ELON_PATTERNS = [
    re.compile(r"\belon\b", re.IGNORECASE),
    re.compile(r"\bmusk\b", re.IGNORECASE),
    re.compile(r"\btesla\b", re.IGNORECASE),
    re.compile(r"\bspacex\b", re.IGNORECASE),
    re.compile(r"\btweet\b", re.IGNORECASE),
    re.compile(r"\btweets\b", re.IGNORECASE),
    re.compile(r"\bx\s*post\b", re.IGNORECASE),
    re.compile(r"\btwitter\b", re.IGNORECASE),
]

CRYPTO_PATTERNS = [
    re.compile(r"\bcrypto\b", re.IGNORECASE),
    re.compile(r"\bbitcoin\b", re.IGNORECASE),
    re.compile(r"\bbtc\b", re.IGNORECASE),
    re.compile(r"\bethereum\b", re.IGNORECASE),
    re.compile(r"\beth\b", re.IGNORECASE),
    re.compile(r"\bsolana\b", re.IGNORECASE),
    re.compile(r"\bdogecoin\b", re.IGNORECASE),
    re.compile(r"\bdoge\b", re.IGNORECASE),
    re.compile(r"\bxrp\b", re.IGNORECASE),
    re.compile(r"\bcardano\b", re.IGNORECASE),
    re.compile(r"\bada\b", re.IGNORECASE),
    re.compile(r"\bavalanche\b", re.IGNORECASE),
    re.compile(r"\bavax\b", re.IGNORECASE),
    re.compile(r"\bbnb\b", re.IGNORECASE),
    re.compile(r"\bup\s*or\s*down\b", re.IGNORECASE),
    re.compile(r"\bupdown\b", re.IGNORECASE),
    re.compile(r"\bmemecoin\b", re.IGNORECASE),
]

SPORT_CONTEXT_PATTERNS = [
    re.compile(
        r"\b(nfl|nba|mlb|nhl|epl|premier\s+league|champions\s+league|la\s+liga|bundesliga|"
        r"serie\s+a|world\s+cup|super\s+bowl|stanley\s+cup|world\s+series|ncaa|college\s+football|"
        r"college\s+basketball|soccer|football|basketball|baseball|hockey|tennis|golf|formula\s*1|f1|"
        r"ufc|mma|boxing|cricket)\b",
        re.IGNORECASE,
    )
]

SPORT_PROP_PATTERNS = [
    re.compile(
        r"\b(mvp|most\s+valuable\s+player|first\s+song|first\s+touchdown|first\s+basket|first\s+goal|"
        r"first\s+map|map\s+\d+|coin\s+toss|anthem|yards?|rebounds?|assists?|touchdowns?|passing|rushing|"
        r"receiving|shots?|kills?|aces?|over\/under|o\/u|point\s+spread|spread|handicap|stat\s+line|"
        r"player\s+prop|prop(?:s)?|exact(?:ly)?|at\s+least|more\s+than|less\s+than)\b",
        re.IGNORECASE,
    )
]

SPORT_GENERIC_WINNER_PATTERNS = [
    re.compile(r"\bwho\s+will\s+win\b", re.IGNORECASE),
    re.compile(r"\bmatch\s+winner\b", re.IGNORECASE),
    re.compile(r"\bgame\s+winner\b", re.IGNORECASE),
    re.compile(r"\bto\s+win\b", re.IGNORECASE),
    re.compile(
        r"\bwin\s+(?:the\s+)?(super\s+bowl|world\s+series|stanley\s+cup|nba\s+finals|championship|title)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bwill\s+.+\s+win\s+on\b", re.IGNORECASE),
    re.compile(r"\bwill\s+.+\s+win\s+\d{1,2}\b", re.IGNORECASE),
]

SPORT_SLUG_PREFIXES = (
    "nba-",
    "nfl-",
    "mlb-",
    "nhl-",
    "epl-",
    "cbb-",
    "cfb-",
    "ncaab-",
    "ncaaf-",
    "wnba-",
    "fl1-",
    "lal-",
    "sea-",
)


def _blob(parts: Iterable[str | None]) -> str:
    return " ".join((p or "").strip() for p in parts if p)


def is_generic_sports_winner_market(
    title: str | None,
    slug: str | None,
    event_slug: str | None = None,
) -> Tuple[bool, str]:
    text = _blob([title, slug, event_slug]).lower()
    if not text:
        return False, ""

    slug_text = f"{str(slug or '').strip().lower()} {str(event_slug or '').strip().lower()}".strip()
    has_sport_context = any(pattern.search(text) for pattern in SPORT_CONTEXT_PATTERNS) or any(
        token and (slug_text.startswith(token) or f" {token}" in slug_text)
        for token in SPORT_SLUG_PREFIXES
    )
    if not has_sport_context:
        return False, ""

    if any(pattern.search(text) for pattern in SPORT_PROP_PATTERNS):
        return False, ""

    if any(pattern.search(text) for pattern in SPORT_GENERIC_WINNER_PATTERNS):
        return True, "generic_sports_winner_market"

    if "-win-" in slug_text or "-winner-" in slug_text or "-to-win-" in slug_text:
        return True, "generic_sports_winner_market"

    return False, ""



# ── Market Exploitability Scorer ──────────────────────────────────────────────
# Returns a score (0-100) and size multiplier for a market based on empirical
# edge evidence from our resolved paper trades (75 trades, 2026-03-17).
#
# This is NOT a filter — markets are never hard-blocked by exploitability score.
# It's a sizing signal: high-score markets get larger positions, low-score markets
# get smaller ones. The existing is_excluded_market() handles hard blocks.
#
# Category scores (empirical basis — updated 2026-03-17 from 75 resolved trades):
#   other/unknown             : 65  (+1.10×) — 66.7% WR, +$276 PnL  [n=18] GENUINE EDGE
#   sports_individual_matchup : 68  (+1.00×) — 54.3% WR, +$153 PnL  [n=35] near-neutral (was overstated at 65.5%/n=29)
#   sports_other              : 60  (+1.0×)  — n=0, insufficient data (neutral)
#   sports_major              : 60  (+1.0×)  — n=0, insufficient data (neutral)
#   us_politics               : 50  (+1.0×)  — no sufficient data yet
#   geopolitics/conflict      : 28  (+0.70×) — 28.6% WR, -$70  PnL  [n=14] VALIDATED NEGATIVE
#   tech/ai_product_launch    : 18  (+0.70×) — 0% WR, -$12 PnL [n=3] (n<5, penalty conservative)
#   crypto/commodity          : 12  (+0.50×) — 0% WR, -$69 PnL [n=5] VALIDATED NEGATIVE

_INDIVIDUAL_MATCHUP_PATTERNS = re.compile(
    r"\b(vs\.?|versus|qualification[:\-]|atp|wta|bnp|miami open|indian wells|roland garros|"
    r"wimbledon|australian open|us open|chile open|dubai tennis|o\/u \d|over\/under \d|"
    r"\d+\.\d+ points|spurs vs|knicks vs|cavaliers vs|rockets vs|kings vs|blue jackets|"
    r"senators vs|flames)\b",
    re.IGNORECASE,
)

_GEOPOLITICS_PATTERNS = re.compile(
    r"\b(iran|israel|russia|ukraine|china taiwan|north korea|hamas|hezbollah|gaza|"
    r"ceasefire|nuclear|netanyahu|putin|zelensky|strait of hormuz|gulf state|"
    r"invasion|coup|missile strike|drone strike|military|conflict ends|war by)\b",
    re.IGNORECASE,
)

_CRYPTO_PRICE_TARGET_PATTERNS = re.compile(
    r"\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|doge|crypto|crude oil|oil hit|"
    r"gold \(gc\)| hit \$|reach \$|dip to \$)\b",
    re.IGNORECASE,
)

_TECH_LAUNCH_PATTERNS = re.compile(
    r"\b(claude \d|gpt-\d|openai|chatgpt|gemini|will .+ be released|will .+ launch|"
    r"ipo|stock split)\b",
    re.IGNORECASE,
)

_MAJOR_LEAGUE_PATTERNS = re.compile(
    r"\b(super bowl|nfl playoffs|nba finals|nba championship|stanley cup|world series|"
    r"march madness|ncaa tournament|champions league final|world cup final|"
    r"la liga|bundesliga|premier league|serie a|nhl playoffs)\b",
    re.IGNORECASE,
)


def market_exploitability(
    title: str | None,
    slug: str | None = None,
    event_slug: str | None = None,
) -> tuple[int, float, str]:
    """Score a market's exploitability from our empirical data.

    Returns (score: 0-100, size_multiplier: float, category: str).
    Caller can use size_multiplier to adjust position sizing.

    Examples:
        Academy Awards / politics → (65, 1.10, "other")  — 66.7% WR observed [n=18]
        Tennis matchup            → (68, 1.00, "sports_individual_matchup")  — 54.3% WR [n=35]
        Iran ceasefire            → (28, 0.70, "geopolitics")  — 28.6% WR [n=14]
        Unknown                   → (65, 1.10, "other")
    """
    text = _blob([title, slug, event_slug]).lower()
    if not text:
        return 65, 1.10, "other"

    # Hard-negative: crypto/commodity price targets (belt+suspenders — also hard-filtered)
    # Empirical: 0% WR, -$69 PnL [n=5] — validated
    if _CRYPTO_PRICE_TARGET_PATTERNS.search(text):
        return 12, 0.50, "crypto/commodity"

    # Hard-negative: tech/AI product launches
    # Empirical: 0% WR, -$12 PnL [n=3] — n<5 but trend clear; conservative penalty
    if _TECH_LAUNCH_PATTERNS.search(text):
        return 18, 0.70, "tech/ai_launch"

    # Hard-negative: geopolitical conflict markets
    # Empirical: 28.6% WR, -$70 PnL [n=14] — validated negative edge
    if _GEOPOLITICS_PATTERNS.search(text):
        return 28, 0.70, "geopolitics"

    # Moderate: individual sports matchups — was overstated, corrected to actual data
    # Empirical: 54.3% WR, +$153 PnL [n=35] — near-neutral, not a strong edge
    if _INDIVIDUAL_MATCHUP_PATTERNS.search(text):
        return 68, 1.00, "sports_individual_matchup"

    # Major league tournaments/championships — insufficient resolved data (n=0)
    if _MAJOR_LEAGUE_PATTERNS.search(text):
        return 60, 1.00, "sports_major"

    # Generic sports context — insufficient resolved data (n=0)
    has_sport = any(p.search(text) for p in SPORT_CONTEXT_PATTERNS)
    if has_sport:
        return 60, 1.00, "sports_other"

    # Unknown / other — 66.7% WR, +$276 PnL [n=18] — genuine edge, bump up
    return 65, 1.10, "other"


_PROP_MARKET_PATTERNS = [
    re.compile(r'\bO/U\b', re.IGNORECASE),
    re.compile(r'\bOver/Under\b', re.IGNORECASE),
    re.compile(r'\bSpread\s*:', re.IGNORECASE),
    re.compile(r'\bhandicap\b', re.IGNORECASE),
    re.compile(r'\bpoints spread\b', re.IGNORECASE),
    re.compile(r'\b[-+]\d+\.?\d*\s*points?\b', re.IGNORECASE),  # e.g. "-7.5 points"
    re.compile(r'\b\d+\.?\d*\s*[-+]\s*points?\b', re.IGNORECASE),
    re.compile(r'\bTotal\s+(?:points?|goals?|runs?)\b', re.IGNORECASE),
    re.compile(r'\bscore more than\b', re.IGNORECASE),
]


def is_prop_market(title: str) -> bool:
    """Returns True if market is a sports prop/spread/O-U that has no insider edge."""
    for pat in _PROP_MARKET_PATTERNS:
        if pat.search(title or ""):
            return True
    return False


def is_excluded_market(
    title: str | None,
    slug: str | None,
    event_slug: str | None = None,
    extra_text: str | None = None,
) -> Tuple[bool, str]:
    text = _blob([title, slug, event_slug, extra_text])

    for pattern in ELON_PATTERNS:
        if pattern.search(text):
            return True, "elon_or_tweet_market"

    for pattern in CRYPTO_PATTERNS:
        if pattern.search(text):
            return True, "crypto_market"

    sports_generic, sports_reason = is_generic_sports_winner_market(title, slug, event_slug)
    if sports_generic:
        return True, sports_reason

    return False, ""
