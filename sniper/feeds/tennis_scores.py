"""ESPN tennis score feed — ATP/WTA scoreboard polling.

Polls the ESPN scoreboard API, parses match state (sets completed, score
by set, match status), and provides an outage tracker for the runtime runner.

No DB dependency — pure HTTP + parsing.

ESPN endpoints:
  ATP: https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard
  WTA: https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard

ESPN JSON structure:
  events[] -> groupings[] -> competitions[] -> competitors[]

Status vocabulary (ESPN -> ours):
  "Final"          -> "final"
  "Scheduled"      -> "scheduled"
  "In Progress"    -> "live"
  "Postponed"      -> "postponed"
  "Suspended"      -> "suspended"
  <anything else>  -> "live"
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ESPN endpoints
# ---------------------------------------------------------------------------

_ESPN_URLS: dict[str, str] = {
    "atp": "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard",
    "wta": "https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard",
}

# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

_STATUS_MAP: dict[str, str] = {
    "final": "final",
    "scheduled": "scheduled",
    "in progress": "live",
    "postponed": "postponed",
    "suspended": "suspended",
}


def _map_espn_status(raw_status: str) -> str:
    """Map an ESPN status description string to our vocabulary."""
    return _STATUS_MAP.get(raw_status.strip().lower(), "live")


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class MatchState:
    """Parsed state of a single tennis match."""

    sets_completed: int
    score_by_set: list[tuple[int, int]]
    match_status: str  # scheduled | live | final | postponed | suspended
    source: str        # always "espn" for this module


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _surname(display_name: str) -> str:
    """Extract last word from display name, lowercased.

    Examples:
        "Novak Djokovic"  -> "djokovic"
        "Carlos Alcaraz"  -> "alcaraz"
        "Djokovic"        -> "djokovic"
    """
    parts = display_name.strip().split()
    if not parts:
        return ""
    return parts[-1].lower()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_scoreboard(data: dict) -> list[dict]:
    """Parse ESPN scoreboard JSON into a flat list of competition dicts.

    Traverses: events[] -> groupings[] -> competitions[].

    Each returned item contains:
        player1 (str): displayName of first competitor
        player2 (str): displayName of second competitor
        competition (dict): raw competition dict (pass to get_sets_completed)

    Competitions with fewer than 2 competitors are skipped.
    """
    results: list[dict] = []
    for event in data.get("events", []):
        for grouping in event.get("groupings", []):
            for comp in grouping.get("competitions", []):
                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue
                player1 = competitors[0].get("athlete", {}).get("displayName", "")
                player2 = competitors[1].get("athlete", {}).get("displayName", "")
                results.append(
                    {
                        "player1": player1,
                        "player2": player2,
                        "competition": comp,
                    }
                )
    return results


def find_match(parsed: list[dict], surname1: str, surname2: str) -> dict | None:
    """Find a match in a parsed scoreboard by both player surnames.

    Matching is:
    - Case-insensitive
    - Order-independent (either player can be first or second)
    - Exact surname match (last word of displayName)

    Returns the first matching item from parse_scoreboard(), or None.
    """
    s1 = surname1.strip().lower()
    s2 = surname2.strip().lower()
    for entry in parsed:
        e1 = _surname(entry["player1"])
        e2 = _surname(entry["player2"])
        if (e1 == s1 and e2 == s2) or (e1 == s2 and e2 == s1):
            return entry
    return None


def get_sets_completed(competition: dict) -> MatchState:
    """Extract MatchState from a competition dict.

    sets_completed = min(len(linescores_p1), len(linescores_p2))
    score_by_set   = list of (p1_score, p2_score) tuples for each completed set

    ESPN status string is mapped through _map_espn_status().
    """
    competitors = competition.get("competitors", [])
    linescores1: list[int] = []
    linescores2: list[int] = []

    if len(competitors) >= 1:
        raw1 = competitors[0].get("linescores", [])
        linescores1 = [int(ls.get("value", 0)) for ls in raw1]
    if len(competitors) >= 2:
        raw2 = competitors[1].get("linescores", [])
        linescores2 = [int(ls.get("value", 0)) for ls in raw2]

    n = min(len(linescores1), len(linescores2))
    score_by_set = [(linescores1[i], linescores2[i]) for i in range(n)]

    raw_status = (
        competition.get("status", {})
        .get("type", {})
        .get("description", "")
    )
    match_status = _map_espn_status(raw_status)

    return MatchState(
        sets_completed=n,
        score_by_set=score_by_set,
        match_status=match_status,
        source="espn",
    )


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

def fetch_scoreboard(tour: str, timeout: int = 10) -> dict | None:
    """Fetch the ESPN scoreboard JSON for a tour.

    Args:
        tour: "atp" or "wta" (case-insensitive)
        timeout: HTTP timeout in seconds

    Returns:
        Parsed JSON dict, or None on any error.
    """
    url = _ESPN_URLS.get(tour.lower())
    if not url:
        logger.error("tennis_scores: unknown tour %r (expected 'atp' or 'wta')", tour)
        return None

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Sniper/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        logger.warning("tennis_scores: HTTP %s fetching %s tour scoreboard", exc.code, tour)
    except urllib.error.URLError as exc:
        logger.warning("tennis_scores: URL error fetching %s tour scoreboard: %s", tour, exc.reason)
    except Exception:
        logger.exception("tennis_scores: unexpected error fetching %s tour scoreboard", tour)
    return None


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def get_match_state(
    tour: str,
    player1: str,
    player2: str,
    event_date: Optional[str] = None,
) -> MatchState | None:
    """Fetch live scoreboard and return MatchState for the named match.

    Args:
        tour: "atp" or "wta"
        player1: surname of first player (case-insensitive)
        player2: surname of second player (case-insensitive)
        event_date: unused — reserved for future date-filtered queries

    Returns:
        MatchState if the match is found, or None.
    """
    data = fetch_scoreboard(tour)
    if data is None:
        return None
    parsed = parse_scoreboard(data)
    entry = find_match(parsed, player1, player2)
    if entry is None:
        logger.debug(
            "tennis_scores: match %s vs %s not found in %s scoreboard",
            player1, player2, tour,
        )
        return None
    return get_sets_completed(entry["competition"])


# ---------------------------------------------------------------------------
# Outage tracker
# ---------------------------------------------------------------------------

class EspnOutageTracker:
    """Tracks consecutive ESPN fetch failures and signals when to pause or cancel.

    Usage::

        tracker = EspnOutageTracker(pause_after=3, cancel_after_min=10)

        # On each poll cycle:
        data = fetch_scoreboard("atp")
        if data is None:
            tracker.record_failure()
        else:
            tracker.record_success()

        if tracker.should_pause_new_orders:
            # stop opening new positions
        if tracker.should_cancel_all:
            # cancel all open positions (ESPN down too long to trust state)

    Args:
        pause_after: Number of consecutive failures before should_pause_new_orders is True.
        cancel_after_min: Minutes elapsed since first failure before should_cancel_all is True.
    """

    def __init__(self, pause_after: int = 3, cancel_after_min: int = 10) -> None:
        self._pause_after = pause_after
        self._cancel_after_sec = cancel_after_min * 60
        self._consecutive_failures = 0
        self._first_failure_ts: Optional[float] = None

    def record_success(self) -> None:
        """Reset all counters — ESPN is responding normally."""
        self._consecutive_failures = 0
        self._first_failure_ts = None

    def record_failure(self) -> None:
        """Increment failure counter and record first failure timestamp."""
        if self._first_failure_ts is None:
            self._first_failure_ts = time.monotonic()
        self._consecutive_failures += 1

    @property
    def should_pause_new_orders(self) -> bool:
        """True when consecutive failures >= pause_after threshold."""
        return self._consecutive_failures >= self._pause_after

    @property
    def should_cancel_all(self) -> bool:
        """True when elapsed since first failure >= cancel_after_sec."""
        if self._first_failure_ts is None:
            return False
        elapsed = time.monotonic() - self._first_failure_ts
        return elapsed >= self._cancel_after_sec
