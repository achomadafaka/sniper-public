#!/usr/bin/env python3
"""
Odds API Integration — Pinnacle de-vigged moneyline reference prices.

STATUS: STUB — requires ODDS_API_KEY environment variable to activate.

When ODDS_API_KEY is set, this becomes the PRIMARY reference source for sports
markets, overriding Kalshi cross-reference. Pinnacle is the sharpest book in the
world — their lines reflect sophisticated model-based pricing, not retail crowd
sentiment. The de-vigged Pinnacle line is the gold standard reference prior.

Cost: ~$30/month for 20K credits on https://the-odds-api.com
      One NBA season (~1200 games) at 1 request/game = 1200 credits = trivial.

Integration with reference_price.py:
    When ODDS_API_KEY is set, reference_price._fetch_kalshi_price() defers to
    this module's get_pinnacle_line() if it returns a result. Pinnacle weight in
    the aggregate reference price = 0.95 (highest confidence of any source).

Sign up at: https://the-odds-api.com
Docs at:    https://the-odds-api.com/lol-of-the-api/

Environment:
    ODDS_API_KEY=<your_key>   # Set in .env or system environment

Supported sports (Odds API sport keys):
    basketball_nba          — NBA game moneylines
    americanfootball_nfl    — NFL game moneylines
    baseball_mlb            — MLB game moneylines
    icehockey_nhl           — NHL game moneylines
    soccer_epl              — English Premier League
    soccer_uefa_champs_league — UEFA Champions League
    mma_mixed_martial_arts  — UFC
    tennis_atp_french_open  — ATP tennis

De-vigging math (2-way market):
    Given American odds: TeamA -150, TeamB +130
    Implied probs: p_A = 150/(150+100) = 0.60, p_B = 100/(130+100) = 0.435
    Sum = 1.035 (3.5% vig)
    De-vigged: p_A_fair = 0.60/1.035 = 0.5797, p_B_fair = 0.435/1.035 = 0.4203
    Sum = 1.00 ✓
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
import urllib.parse
from typing import Any

logger = logging.getLogger(__name__)

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Cache for fetched lines (avoid repeated API calls for same game)
_ODDS_CACHE: dict[str, Any] = {}
_ODDS_CACHE_TIME: float = 0.0
_ODDS_CACHE_TTL: float = 300.0  # 5 minutes

# Sport key mapping from common keywords to Odds API sport_key
_SPORT_KEYS = {
    "nba": "basketball_nba",
    "basketball": "basketball_nba",
    "nfl": "americanfootball_nfl",
    "football": "americanfootball_nfl",
    "mlb": "baseball_mlb",
    "baseball": "baseball_mlb",
    "nhl": "icehockey_nhl",
    "hockey": "icehockey_nhl",
    "epl": "soccer_epl",
    "premier league": "soccer_epl",
    "champions league": "soccer_uefa_champs_league",
    "ufc": "mma_mixed_martial_arts",
    "mma": "mma_mixed_martial_arts",
}


def _devig_pinnacle(team_a_decimal: float, team_b_decimal: float) -> tuple[float, float]:
    """Convert decimal odds to de-vigged fair probabilities.

    Args:
        team_a_decimal: Decimal odds for team A (e.g. 1.65 = -154 American)
        team_b_decimal: Decimal odds for team B (e.g. 2.40 = +140 American)

    Returns:
        (fair_prob_a, fair_prob_b) summing to 1.0
    """
    implied_a = 1.0 / team_a_decimal
    implied_b = 1.0 / team_b_decimal
    total = implied_a + implied_b
    return round(implied_a / total, 4), round(implied_b / total, 4)


def _american_to_decimal(american: int) -> float:
    """Convert American moneyline odds to decimal odds."""
    if american > 0:
        return 1.0 + american / 100.0
    else:
        return 1.0 + 100.0 / abs(american)


def _detect_sport_key(market_title: str) -> str | None:
    """Detect which Odds API sport_key to use from a market title."""
    title_lower = market_title.lower()
    for kw, sport_key in _SPORT_KEYS.items():
        if kw in title_lower:
            return sport_key
    return None


def _extract_teams_from_title(market_title: str) -> tuple[str | None, str | None]:
    """Extract two team names from a market title like 'Will Team A beat Team B?'."""
    # Pattern: "Will X beat Y?" or "X vs Y" or "X at Y"
    patterns = [
        r"Will (.+?) beat (.+?)[\?$]",
        r"(.+?)\s+(?:vs\.?|versus|at|@)\s+(.+?)(?:\s+Winner)?[\?$]",
        r"(.+?)\s+at\s+(.+?)[\?$]",
    ]
    for pat in patterns:
        m = re.search(pat, market_title, re.IGNORECASE)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return None, None


def get_odds_data(sport_key: str) -> list[dict] | None:
    """Fetch current odds for all games in a sport from The Odds API.

    Uses Pinnacle bookmaker (h2h market = moneyline).
    Returns list of game odds objects or None if API unavailable.
    """
    global _ODDS_CACHE, _ODDS_CACHE_TIME

    if not ODDS_API_KEY:
        return None

    cache_key = f"odds_{sport_key}"
    now = time.time()
    if now - _ODDS_CACHE_TIME < _ODDS_CACHE_TTL and cache_key in _ODDS_CACHE:
        return _ODDS_CACHE[cache_key]

    params = urllib.parse.urlencode({
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "bookmakers": "pinnacle",
        "oddsFormat": "decimal",
    })
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds?{params}"
    logger.debug("Fetching Pinnacle odds: %s", url[:80])

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sniper-odds/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            # Track remaining credits from headers
            credits_remaining = resp.headers.get("x-requests-remaining", "?")
            credits_used = resp.headers.get("x-requests-used", "?")
            logger.debug(
                "Odds API credits: used=%s remaining=%s",
                credits_used, credits_remaining,
            )
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("Odds API request failed: %s", e)
        return None

    _ODDS_CACHE[cache_key] = data
    _ODDS_CACHE_TIME = now
    return data


def get_pinnacle_line(
    team_a: str,
    team_b: str,
    sport: str,
    game_date: str | None = None,
) -> dict | None:
    """Fetch de-vigged Pinnacle moneyline for a game.

    NOTE: Requires ODDS_API_KEY environment variable.
    When not set, returns None immediately (graceful no-op).

    Args:
        team_a:     First team name (home or team being bet on)
        team_b:     Second team name (opponent)
        sport:      Sport name or Odds API sport_key
                    (e.g. "nba", "basketball_nba", "nfl")
        game_date:  Optional ISO date string for disambiguation (YYYY-MM-DD)

    Returns:
        {
            'team_a': str,
            'team_b': str,
            'team_a_prob': float,   # de-vigged Pinnacle fair probability
            'team_b_prob': float,
            'team_a_decimal': float,
            'team_b_decimal': float,
            'game_id': str,
            'commence_time': str,
            'source': 'pinnacle',
            'retrieved_at': str,
        }
        or None if ODDS_API_KEY not set or game not found.
    """
    if not ODDS_API_KEY:
        logger.debug("get_pinnacle_line: ODDS_API_KEY not set — skipping")
        return None

    # Resolve sport key
    sport_key = _SPORT_KEYS.get(sport.lower(), sport)

    games = get_odds_data(sport_key)
    if not games:
        return None

    # Normalise team names for fuzzy matching
    team_a_lower = team_a.lower().strip()
    team_b_lower = team_b.lower().strip()

    # Extract key words (last word of team name usually = nickname)
    team_a_key = team_a_lower.split()[-1] if team_a_lower else ""
    team_b_key = team_b_lower.split()[-1] if team_b_lower else ""

    best_match = None
    best_score = 0

    for game in games:
        home_team = game.get("home_team", "").lower()
        away_team = game.get("away_team", "").lower()
        commence = game.get("commence_time", "")

        # Optional date filter
        if game_date and game_date not in commence:
            continue

        # Score based on keyword overlap
        teams_str = f"{home_team} {away_team}"
        score = 0
        if team_a_key and team_a_key in teams_str:
            score += 2
        if team_b_key and team_b_key in teams_str:
            score += 2
        if team_a_lower in teams_str:
            score += 1
        if team_b_lower in teams_str:
            score += 1

        if score > best_score:
            best_score = score
            best_match = game

    if not best_match or best_score < 2:
        logger.debug(
            "No Pinnacle match for '%s' vs '%s' in %s (best_score=%d)",
            team_a, team_b, sport_key, best_score,
        )
        return None

    # Extract Pinnacle h2h odds
    bookmakers = best_match.get("bookmakers", [])
    pinnacle_data = None
    for bk in bookmakers:
        if bk.get("key") == "pinnacle":
            pinnacle_data = bk
            break

    if not pinnacle_data:
        logger.debug("Pinnacle not available for this game")
        return None

    # Find h2h market
    markets = pinnacle_data.get("markets", [])
    h2h = None
    for m in markets:
        if m.get("key") == "h2h":
            h2h = m
            break

    if not h2h:
        return None

    outcomes = h2h.get("outcomes", [])
    if len(outcomes) < 2:
        return None

    # Map outcomes to our team_a / team_b
    home = best_match["home_team"]
    away = best_match["away_team"]

    home_odds = None
    away_odds = None
    for o in outcomes:
        name = o.get("name", "")
        price = float(o.get("price", 0))
        if name.lower() == home.lower():
            home_odds = price
        elif name.lower() == away.lower():
            away_odds = price

    if not home_odds or not away_odds:
        return None

    prob_home, prob_away = _devig_pinnacle(home_odds, away_odds)

    # Assign to team_a / team_b
    home_lower = home.lower()
    if team_a_key in home_lower or team_a_lower in home_lower:
        prob_a, prob_b = prob_home, prob_away
        dec_a, dec_b = home_odds, away_odds
    else:
        prob_a, prob_b = prob_away, prob_home
        dec_a, dec_b = away_odds, home_odds

    import datetime
    return {
        "team_a": team_a,
        "team_b": team_b,
        "team_a_prob": prob_a,
        "team_b_prob": prob_b,
        "team_a_decimal": dec_a,
        "team_b_decimal": dec_b,
        "game_id": best_match.get("id", ""),
        "commence_time": best_match.get("commence_time", ""),
        "home_team": home,
        "away_team": away,
        "source": "pinnacle",
        "retrieved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def get_pinnacle_reference_for_polymarket(
    market_title: str,
    outcome: str,
) -> dict | None:
    """High-level wrapper: given a Polymarket title and outcome, return Pinnacle reference price.

    This is the function called by reference_price.py when ODDS_API_KEY is set.
    It overrides Kalshi as the primary sports reference.

    Returns:
        {
            'reference_price': float,   # Pinnacle fair probability for our outcome
            'confidence': 'high',       # Pinnacle is always high confidence
            'source': 'pinnacle',
            'game_detail': dict,        # full Pinnacle line
        }
        or None.
    """
    if not ODDS_API_KEY:
        return None

    sport_key = _detect_sport_key(market_title)
    if not sport_key:
        return None

    team_a, team_b = _extract_teams_from_title(market_title)
    if not team_a or not team_b:
        return None

    line = get_pinnacle_line(team_a, team_b, sport_key)
    if not line:
        return None

    # Map outcome to probability
    outcome_upper = outcome.upper()
    if outcome_upper == "YES" or outcome_upper == team_a.upper():
        ref_price = line["team_a_prob"]
    elif outcome_upper == "NO" or outcome_upper == team_b.upper():
        ref_price = 1.0 - line["team_a_prob"]
    else:
        # Try to match outcome as a team name fragment
        if any(kw in outcome_upper.lower() for kw in team_a.lower().split()):
            ref_price = line["team_a_prob"]
        else:
            ref_price = line["team_b_prob"]

    return {
        "reference_price": ref_price,
        "confidence": "high",
        "source": "pinnacle",
        "game_detail": line,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI test
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    if not ODDS_API_KEY:
        print("ODDS_API_KEY not set — this module is in stub mode.")
        print("Set ODDS_API_KEY env var to activate Pinnacle line fetching.")
        print()
        print("Example usage once API key is set:")
        print('  result = get_pinnacle_line("Lakers", "Rockets", "nba")')
        print('  result = get_pinnacle_reference_for_polymarket(')
        print('      "Will the Los Angeles Lakers beat the Houston Rockets?", "YES")')
    else:
        import pprint
        result = get_pinnacle_line("Lakers", "Rockets", "nba")
        print("Pinnacle line for Lakers vs Rockets:")
        pprint.pprint(result)
