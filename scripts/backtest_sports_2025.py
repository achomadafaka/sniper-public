#!/usr/bin/env python3
"""
backtest_sports_2025.py — Sports Line Dislocation Backtest

Fetches closed Polymarket game markets, historical CLOB prices 24h before
game start, and Vegas closing moneylines via Action Network.

Simulates edge detection logic:
  edge = fair_prob - poly_price >= min_edge (default 0.04)

Supports:
  --sport march-madness  (original NCAAB tournament 2025)
  --sport nba            (NBA regular season Oct 2024 – Apr 2025)
  --sport ncaab          (NCAAB regular season Nov 2024 – Mar 2025)
  --sport all            (all of the above combined)

Usage:
  python3 scripts/backtest_sports_2025.py
  python3 scripts/backtest_sports_2025.py --sport nba
  python3 scripts/backtest_sports_2025.py --sport ncaab
  python3 scripts/backtest_sports_2025.py --sport all
  python3 scripts/backtest_sports_2025.py --dry-run
  python3 scripts/backtest_sports_2025.py --min-edge 0.06
  python3 scripts/backtest_sports_2025.py --bet-size 25
  python3 scripts/backtest_sports_2025.py --max-games 20
  python3 scripts/backtest_sports_2025.py --season 2025
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"
AN_BASE    = "https://api.actionnetwork.com/web/v1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# Action Network book IDs: 15=DraftKings, 30=FanDuel, 76=Caesars, 75=BetMGM
AN_BOOK_IDS = "15,30,76,75"

# Sport configurations: tag_slug, AN sport key, season date windows
SPORT_CONFIGS = {
    "march-madness": {
        "tag_slug":    "march-madness",
        "an_sport":    "ncaab",
        "start_dates": ["2025-03"],
        "end_dates":   ["2025-04"],
        "label":       "March Madness 2025",
        "min_volume":  500,
    },
    "nba": {
        "tag_slug":    "nba",
        "an_sport":    "nba",
        "start_dates": ["2024-10", "2024-11", "2024-12", "2025-01", "2025-02", "2025-03", "2025-04"],
        "end_dates":   None,   # any date in range accepted
        "date_range":  ("2024-10-01", "2025-04-30"),
        "label":       "NBA Regular Season 2024-25",
        "min_volume":  1000,
    },
    "ncaab": {
        # NOTE: Polymarket did not have individual NCAAB regular-season game markets in 2024-25.
        # The 'ncaab' tag only has tournament/champion markets.
        # Individual game markets (tag: ncaa-basketball) only started in the 2025-26 season.
        # For 2024-25 regular season, use --sport march-madness instead.
        # This config is set up for 2025-26 season onwards.
        "tag_slug":    "ncaa-basketball",
        "an_sport":    "ncaab",
        "start_dates": None,
        "end_dates":   None,
        "date_range":  ("2024-11-01", "2025-03-14"),   # before Selection Sunday
        "label":       "NCAAB Regular Season 2024-25",
        "min_volume":  200,
    },
}

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def get_json(url: str, retries: int = 3, delay: float = 1.2) -> Any:
    """GET url, return parsed JSON.  Returns None on failure."""
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=20)
            if r.status_code == 200:
                return r.json()
            print(f"  [warn] HTTP {r.status_code} for {url[:90]}")
        except Exception as exc:
            print(f"  [warn] request error ({exc}) for {url[:90]}")
        if attempt < retries - 1:
            time.sleep(delay)
    return None


# ---------------------------------------------------------------------------
# Maths
# ---------------------------------------------------------------------------

def american_to_prob(ml: int) -> float:
    """Convert American moneyline to raw implied probability (with vig)."""
    if ml > 0:
        return 100.0 / (ml + 100.0)
    else:
        return abs(ml) / (abs(ml) + 100.0)


def remove_vig(p1: float, p2: float) -> tuple[float, float]:
    """Normalise two raw implied probabilities to sum to 1 (vig removal)."""
    total = p1 + p2
    return p1 / total, p2 / total


def parse_ts(s: str) -> int:
    """Parse various datetime-string formats to UTC unix timestamp."""
    s = s.rstrip("Z")
    # Handle '+00' timezone suffix (e.g. '2025-03-19 03:09:57+00')
    for sep in ["+00", "+00:00"]:
        if s.endswith(sep):
            s = s[: -len(sep)]
    s = s.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {s!r}")


def in_date_range(date_str: str, date_range: tuple[str, str]) -> bool:
    """Return True if date_str (YYYY-MM-DD prefix) falls within [start, end]."""
    d = date_str[:10]
    return date_range[0] <= d <= date_range[1]


# ---------------------------------------------------------------------------
# Step 1 — Discover Polymarket markets (paginated)
# ---------------------------------------------------------------------------

def fetch_polymarket_games_paginated(
    tag_slug: str,
    date_range: Optional[tuple[str, str]],
    min_volume: float,
    label: str,
    max_pages: int = 20,
) -> list[dict]:
    """
    Query Gamma API events for a given tag_slug with pagination.
    Filters to game matchups ('X vs. Y' titles) within date_range.
    Returns list of dicts with all data needed for price lookup and simulation.
    """
    print(f"\n[Step 1] Fetching Polymarket {label} markets ...")
    all_games: list[dict] = []
    seen_event_ids: set = set()

    for page in range(max_pages):
        offset = page * 100
        url = (
            f"{GAMMA_BASE}/events"
            f"?active=false&closed=true&tag_slug={tag_slug}&limit=100&offset={offset}"
        )
        data = get_json(url)
        if not data:
            print(f"  [warn] No data at offset={offset}, stopping pagination")
            break

        if not isinstance(data, list):
            # Some endpoints return dict with 'events' key
            data = data.get("events", []) if isinstance(data, dict) else []

        if not data:
            print(f"  Reached end of results at offset={offset}")
            break

        page_games = 0
        for event in data:
            event_id = event.get("id")
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)

            end_date = event.get("endDate", "")
            title    = event.get("title", "")

            # Only "X vs. Y" individual game matchups
            if " vs. " not in title:
                continue
            if not event.get("closed"):
                continue

            # Date range filter
            if date_range and not in_date_range(end_date, date_range):
                continue

            # Filter out non-game markets (totals, props)
            title_lower = title.lower()
            skip_keywords = ["combine for", "total points", "first basket",
                             "first score", "first half", "halftime", "quarter",
                             "most points", "mvp", "championship", "will make",
                             "will reach", "advance to", "reach the"]
            if any(kw in title_lower for kw in skip_keywords):
                continue

            markets = event.get("markets", [])
            if not markets:
                continue

            m = markets[0]

            # Volume filter
            vol = float(m.get("volume") or 0)
            if vol < min_volume:
                continue

            # Parse clobTokenIds — stored as JSON-encoded string
            raw_tokens = m.get("clobTokenIds", "[]")
            if isinstance(raw_tokens, str):
                try:
                    token_ids = json.loads(raw_tokens)
                except Exception:
                    token_ids = []
            else:
                token_ids = raw_tokens

            # Parse outcomes and prices
            try:
                outcomes = json.loads(m.get("outcomes", "[]"))
            except Exception:
                outcomes = []
            try:
                outcome_prices = [float(p) for p in json.loads(m.get("outcomePrices", "[0,1]"))]
            except Exception:
                outcome_prices = [0.0, 1.0]

            # Use gameStartTime as reference; fall back to closedTime or endDate
            game_start = m.get("gameStartTime") or m.get("closedTime") or end_date

            all_games.append({
                "event_id":      event_id,
                "title":         title,
                "end_date":      end_date,
                "game_start":    game_start,
                "market_id":     m.get("id"),
                "condition_id":  m.get("conditionId"),
                "clob_token_ids": token_ids,
                "outcomes":      outcomes,
                "outcome_prices": outcome_prices,
                "last_trade_price": float(m.get("lastTradePrice") or 0),
                "volume":        vol,
                "closed_time":   m.get("closedTime", ""),
            })
            page_games += 1

        print(f"  Page {page+1}: {len(data)} events scanned, {page_games} games added (total={len(all_games)})")

        if len(data) < 100:
            # Last page — no more results
            break

        time.sleep(0.3)

    print(f"  Found {len(all_games)} individual game markets for {label}")
    return all_games


# ---------------------------------------------------------------------------
# Step 2 — Fetch CLOB price 24h before game start
# ---------------------------------------------------------------------------

def get_poly_price_24h_before(g: dict) -> Optional[float]:
    """
    Return Polymarket YES (team1) price ~24h before game start.
    Falls back to last_trade_price if CLOB returns no data.
    Returns None if no pre-game price is available.
    """
    token_ids      = g.get("clob_token_ids", [])
    game_start_str = g.get("game_start", "")

    if not token_ids:
        return _fallback_price(g)

    try:
        game_ts = parse_ts(game_start_str)
    except ValueError:
        return _fallback_price(g)

    target_ts = game_ts - 86400   # 24h before game
    start_ts  = target_ts - 3600  # 1-hour window around target

    yes_token = token_ids[0]
    url = (
        f"{CLOB_BASE}/prices-history"
        f"?market={yes_token}"
        f"&startTs={start_ts}"
        f"&endTs={target_ts + 7200}"
        f"&fidelity=60"
    )
    data = get_json(url)
    if data:
        history = data.get("history", [])
        if history:
            # Price closest to 24h before game start
            closest = min(history, key=lambda h: abs(h["t"] - target_ts))
            return float(closest["p"])

    return _fallback_price(g)


def _fallback_price(g: dict) -> Optional[float]:
    """Use lastTradePrice if it's in the pre-resolution zone (0.02–0.98)."""
    ltp = g.get("last_trade_price", 0)
    if 0.02 < ltp < 0.98:
        return float(ltp)
    return None


# ---------------------------------------------------------------------------
# Step 3 — Vegas moneylines via Action Network
# ---------------------------------------------------------------------------

def fetch_an_games(date_str: str, sport: str = "ncaab") -> list[dict]:
    """
    Fetch Action Network scoreboard for a YYYYMMDD date string.
    sport: 'ncaab' or 'nba'
    Returns list of game dicts with teams, odds, winning_team_id.
    """
    url = (
        f"{AN_BASE}/scoreboard/{sport}"
        f"?period=game&bookIds={AN_BOOK_IDS}&date={date_str}"
    )
    data = get_json(url)
    if not data:
        return []
    return data.get("games", [])


def _team_name_matches(t: dict, query: str) -> bool:
    """Fuzzy name match: query substring in any team name field."""
    query_l = query.lower().strip()
    if not query_l:
        return False
    for field in ("full_name", "display_name", "location", "abbr", "nickname"):
        raw = t.get(field) or ""
        val = str(raw).lower().strip()
        if not val:
            continue   # skip empty values — empty string matches everything
        if query_l in val or val in query_l:
            return True
    # Also try first word of query (e.g. "Los Angeles" → "LA Lakers")
    first_word = query_l.split()[0] if query_l.split() else ""
    if len(first_word) > 3:
        for field in ("full_name", "display_name"):
            raw = t.get(field) or ""
            val = str(raw).lower().strip()
            if val and first_word in val:
                return True
    return False


def match_game_to_an(g: dict, an_games: list[dict]) -> Optional[dict]:
    """
    Match Polymarket event ("TeamA vs. TeamB") to an Action Network game.
    """
    title  = g["title"]
    parts  = title.split(" vs. ")
    if len(parts) != 2:
        return None
    team_a, team_b = parts[0].strip(), parts[1].strip()

    for an_game in an_games:
        teams   = an_game.get("teams", [])
        a_match = any(_team_name_matches(t, team_a) for t in teams)
        b_match = any(_team_name_matches(t, team_b) for t in teams)
        if a_match and b_match:
            return an_game
    return None


def compute_book_fair_prob(an_game: dict, team1_is_away: bool) -> Optional[float]:
    """
    Compute vig-free fair win probability for outcomes[0] (team1).
    Prefers DraftKings (book_id=15); falls back to first available book.
    """
    odds_list = an_game.get("odds", [])
    if not odds_list:
        return None

    preferred = next((o for o in odds_list if o.get("book_id") == 15), None)
    if not preferred:
        preferred = odds_list[0]

    ml_away = preferred.get("ml_away")
    ml_home = preferred.get("ml_home")
    if ml_away is None or ml_home is None:
        return None

    raw_away = american_to_prob(int(ml_away))
    raw_home = american_to_prob(int(ml_home))
    fair_away, fair_home = remove_vig(raw_away, raw_home)

    return fair_away if team1_is_away else fair_home


def get_team1_is_away(an_game: dict, team1_name: str) -> bool:
    """Return True if team1 is the away team in the Action Network game."""
    away_id = an_game.get("away_team_id")
    for t in an_game.get("teams", []):
        if t.get("id") == away_id and _team_name_matches(t, team1_name):
            return True
    return False


def did_team1_win(g: dict) -> Optional[bool]:
    """
    Determine if outcomes[0] won.  Uses Polymarket outcomePrices as ground
    truth: prices[0] >= 0.99 means Team1 won, <= 0.01 means Team2 won.
    """
    prices = g.get("outcome_prices", [])
    if len(prices) >= 1:
        if prices[0] >= 0.99:
            return True
        elif prices[0] <= 0.01:
            return False
    return None


# ---------------------------------------------------------------------------
# Step 4 — Edge simulation
# ---------------------------------------------------------------------------

def simulate_entry(
    poly_price: float,
    fair_prob:  float,
    outcome:    bool,
    min_edge:   float,
    bet_size:   float,
) -> Optional[dict]:
    """
    If edge >= min_edge and poly_price <= 0.75, simulate a $bet_size entry.
    Returns result dict or None (no entry taken).
    """
    edge = fair_prob - poly_price
    if edge < min_edge:
        return None
    if poly_price > 0.75:
        return None

    pnl   = bet_size * (1.0 / poly_price - 1.0) if outcome else -bet_size
    brier = (poly_price - (1.0 if outcome else 0.0)) ** 2

    return {
        "pnl":   pnl,
        "brier": brier,
        "outcome_str": "WIN" if outcome else "LOSS",
    }


# ---------------------------------------------------------------------------
# Calibration analysis (no sportsbook required)
# ---------------------------------------------------------------------------

CALIB_BUCKETS = [
    (0.05, 0.15),
    (0.15, 0.30),
    (0.30, 0.45),
    (0.45, 0.55),
    (0.55, 0.70),
    (0.70, 0.85),
    (0.85, 0.96),
]


def print_calibration(calib_games: list[dict]) -> None:
    """
    Print calibration table for all games where we have a poly price and outcome.
    calib_games: list of dicts with keys: poly_price, outcome (bool)
    """
    if not calib_games:
        return

    print("\n=== CALIBRATION ANALYSIS (all games, no sportsbook required) ===")
    print(f"  {'Price bucket':<14} {'Games':>6} {'PolyMid':>8} {'ActualWin%':>11} {'Bias':>7}")
    print("  " + "-" * 54)

    for lo, hi in CALIB_BUCKETS:
        bucket = [g for g in calib_games if lo <= g["poly_price"] < hi]
        if not bucket:
            continue
        n        = len(bucket)
        avg_poly = sum(g["poly_price"] for g in bucket) / n
        actual_wr = sum(1 for g in bucket if g["outcome"]) / n
        bias     = actual_wr - avg_poly
        print(
            f"  {lo:.2f}–{hi:.2f}       {n:>6}   {avg_poly:>6.1%}     {actual_wr:>8.1%}   {bias:>+6.1%}"
        )

    total      = len(calib_games)
    overall_wr = sum(1 for g in calib_games if g["outcome"]) / total
    avg_poly   = sum(g["poly_price"] for g in calib_games) / total
    print(f"\n  Total: {total} games   Avg Poly: {avg_poly:.1%}   Overall win%: {overall_wr:.1%}")

    # Longshot bias indicator
    low  = [g for g in calib_games if g["poly_price"] < 0.40]
    high = [g for g in calib_games if g["poly_price"] >= 0.60]
    if low and high:
        low_bias  = sum(1 for g in low if g["outcome"]) / len(low) - sum(g["poly_price"] for g in low) / len(low)
        high_bias = sum(1 for g in high if g["outcome"]) / len(high) - sum(g["poly_price"] for g in high) / len(high)
        if low_bias < -0.02:
            print(f"\n  LONGSHOT BIAS DETECTED: underdogs (<40¢) underperform by {low_bias:.1%}")
            print("  => Poly crowd OVERprices underdogs; crowd_fade for sports should BUY FAVORITES")
        elif low_bias > 0.02:
            print(f"\n  REVERSE LONGSHOT: underdogs (<40¢) OUTperform by {low_bias:.1%}")
            print("  => Poly crowd UNDERprices underdogs")
        else:
            print(f"\n  No strong longshot bias detected (underdog bias={low_bias:.1%})")


# ---------------------------------------------------------------------------
# Volume-weighted edge analysis
# ---------------------------------------------------------------------------

EDGE_BUCKETS = [
    ("0–2%",   0.00, 0.02),
    ("2–4%",   0.02, 0.04),
    ("4–6%",   0.04, 0.06),
    ("6%+",    0.06, 1.00),
]


def print_edge_buckets(results: list[dict]) -> None:
    """Print edge-bucketed WR / EV table for games that had sportsbook data."""
    if not results:
        return

    print("\n=== VOLUME-WEIGHTED EDGE ANALYSIS (games with sportsbook data) ===")
    print(f"  {'Edge bucket':<12} {'Entries':>8} {'WR':>7} {'ExpWR':>7} {'EV/$10':>8} {'TotalPnL':>10}")
    print("  " + "-" * 60)

    for label, lo, hi in EDGE_BUCKETS:
        bucket = [r for r in results if lo <= r.get("edge", 0) < hi]
        if not bucket:
            continue
        n        = len(bucket)
        wins     = sum(1 for r in bucket if r["outcome"] == "WIN")
        wr       = wins / n
        exp_wr   = sum(r["book_fair"] for r in bucket) / n
        # EV per $10 bet at poly price (with no edge filter)
        ev10 = sum(
            10.0 * (1.0 / r["poly"] - 1.0) * r["book_fair"] - 10.0 * (1 - r["book_fair"])
            for r in bucket
        ) / n
        total_pnl = sum(
            (10.0 * (1.0 / r["poly"] - 1.0) if r["outcome"] == "WIN" else -10.0)
            for r in bucket
        )
        print(
            f"  {label:<12} {n:>8} {wr:>7.1%} {exp_wr:>7.1%} {ev10:>+8.2f} {total_pnl:>+10.2f}"
        )

    print()


# ---------------------------------------------------------------------------
# Walk-forward monthly rolling analysis
# ---------------------------------------------------------------------------

def print_walk_forward(results: list[dict]) -> None:
    """Print monthly rolling window analysis and cumulative PnL curve."""
    # Only consider entered trades with a resolved PnL
    entered = [r for r in results if r.get("entered") and r.get("pnl") is not None]
    if not entered:
        return

    # Group by YYYY-MM using the "date" field
    from collections import defaultdict
    months: dict[str, list[dict]] = defaultdict(list)
    for r in entered:
        month_key = r["date"][:7]  # "YYYY-MM"
        months[month_key].append(r)

    if not months:
        return

    sorted_months = sorted(months.keys())

    print("\n=== WALK-FORWARD ANALYSIS (monthly rolling windows) ===")
    print(f"  {'Month':<10} {'Entries':>8} {'WR':>7} {'ROI':>8} {'Cumul PnL':>12} {'Edge Avg':>9}")
    print("  " + "-" * 57)

    cumul_pnl = 0.0
    month_stats: list[tuple] = []  # (month, n, wr_pct, roi_pct, cumul_pnl, avg_edge)

    bet_size = 10.0  # default; used only for ROI denominator (consistent with rest of script)
    # Try to infer bet_size from PnL magnitudes: most losses are exactly -bet_size
    losses = [abs(r["pnl"]) for r in entered if r["outcome"] == "LOSS" and r["pnl"] is not None]
    if losses:
        # modal loss amount ≈ bet_size
        import statistics
        try:
            bet_size = statistics.mode(round(x, 2) for x in losses)
        except statistics.StatisticsError:
            bet_size = sum(losses) / len(losses)

    for month in sorted_months:
        rows = months[month]
        n = len(rows)
        wins = sum(1 for r in rows if r["outcome"] == "WIN")
        wr_pct = wins / n * 100
        month_pnl = sum(r["pnl"] for r in rows)
        cumul_pnl += month_pnl
        roi_pct = month_pnl / (n * bet_size) * 100
        avg_edge = sum(r["edge"] for r in rows) / n * 100  # as %

        month_stats.append((month, n, wr_pct, roi_pct, cumul_pnl, avg_edge))

        roi_str = f"{roi_pct:+.1f}%"
        wr_str  = f"{wr_pct:.1f}%"
        print(
            f"  {month:<10} {n:>8} {wr_str:>7} {roi_str:>8} "
            f"  ${cumul_pnl:>+9.2f}   {avg_edge:.1f}%"
        )

    print()

    # Trend detection
    if len(month_stats) >= 3:
        first_half = month_stats[: len(month_stats) // 2]
        second_half = month_stats[len(month_stats) // 2 :]
        avg_edge_early = sum(s[5] for s in first_half) / len(first_half)
        avg_edge_late  = sum(s[5] for s in second_half) / len(second_half)
        avg_roi_early  = sum(s[3] for s in first_half) / len(first_half)
        avg_roi_late   = sum(s[3] for s in second_half) / len(second_half)

        edge_delta = avg_edge_late - avg_edge_early
        roi_delta  = avg_roi_late - avg_roi_early

        if edge_delta < -1.0 or roi_delta < -10.0:
            trend = "gradually decaying"
        elif edge_delta > 1.0 or roi_delta > 10.0:
            trend = "improving late-season"
        elif avg_roi_early > 20.0 and avg_roi_late < 5.0:
            trend = "concentrated early"
        else:
            trend = "edge stable"

        print(f"  Trend: {trend}")
    else:
        trend = "insufficient months for trend"
        print(f"  Trend: {trend}")

    # Best / worst months
    if month_stats:
        best  = max(month_stats, key=lambda s: s[3])
        worst = min(month_stats, key=lambda s: s[3])
        print(f"  Best month:  {best[0]} (WR {best[2]:.0f}%, ROI {best[3]:+.0f}%)")
        print(f"  Worst month: {worst[0]} (WR {worst[2]:.0f}%, ROI {worst[3]:+.0f}%)")

    # Monthly WR std-dev
    if len(month_stats) >= 2:
        import statistics
        wr_values = [s[2] for s in month_stats]
        wr_std = statistics.stdev(wr_values)
        stability = "stable" if wr_std < 12.0 else ("volatile" if wr_std > 20.0 else "moderate")
        print(f"  Monthly WR std-dev: {wr_std:.1f}%  (stable=<12%, volatile=>20%) [{stability}]")

    # Cumulative PnL bar chart
    print("\n=== CUMULATIVE PnL CURVE (monthly) ===")
    max_cumul = max(abs(s[4]) for s in month_stats) if month_stats else 1.0
    bar_width  = 30

    for month, _n, _wr, _roi, cum_pnl, _edge in month_stats:
        # month label: show abbreviated month name + year
        try:
            dt = datetime.strptime(month, "%Y-%m")
            label_str = dt.strftime("%b")
        except ValueError:
            label_str = month[5:]

        filled = int(abs(cum_pnl) / max_cumul * bar_width) if max_cumul > 0 else 0
        bar = "█" * filled
        sign = "+" if cum_pnl >= 0 else "-"
        print(f"  {label_str:<4} {bar:<30}  {sign}${abs(cum_pnl):.0f}")

    print()


# ---------------------------------------------------------------------------
# Per-sport processing
# ---------------------------------------------------------------------------

def process_sport(
    sport_key:  str,
    args,
    an_cache:   dict,
) -> tuple[list[dict], list[dict]]:
    """
    Fetch and process a single sport.
    Returns (results_with_book, calib_games).
    results_with_book: games that had sportsbook match
    calib_games: all games with poly price + outcome (for calibration)
    """
    cfg = SPORT_CONFIGS[sport_key]
    label     = cfg["label"]
    an_sport  = cfg["an_sport"]
    min_vol   = cfg["min_volume"]

    date_range = cfg.get("date_range")  # (start, end) or None

    games = fetch_polymarket_games_paginated(
        tag_slug   = cfg["tag_slug"],
        date_range = date_range,
        min_volume = min_vol,
        label      = label,
    )

    if not games:
        print(f"  [warn] No games found for {label}")
        return [], []

    if args.max_games:
        games = games[:args.max_games]
        print(f"  (Capped to {args.max_games} games via --max-games)")

    if args.dry_run:
        print(f"\nDry-run ({label}): {len(games)} markets discovered.\n")
        for g in games:
            token_count = len(g.get("clob_token_ids", []))
            print(
                f"  [{g['end_date'][:10]}] {g['title']:<50} "
                f"vol=${g['volume']:>8,.0f}  tokens={token_count}"
            )
        return [], []

    print(f"\n[Processing] Fetching CLOB prices & Vegas lines for {len(games)} {label} games ...")

    def get_an_for_game(g: dict) -> list[dict]:
        """
        Return AN games list for the game's date.
        NBA/NCAAB games often tip off at 7-10 PM US ET, which is 00:00-04:00 UTC next day.
        We try both the UTC date AND UTC-6h date (US Central approximation) and merge.
        """
        ref = g.get("game_start") or g.get("end_date")
        date_str_utc = ref[:10].replace("-", "")   # YYYYMMDD from UTC game_start

        # Compute previous-day date string (UTC-6h approximation for US timezones)
        try:
            game_ts = parse_ts(ref)
            from datetime import datetime, timezone
            dt_us = datetime.fromtimestamp(game_ts - 6 * 3600, tz=timezone.utc)
            date_str_us = dt_us.strftime("%Y%m%d")
        except Exception:
            date_str_us = date_str_utc

        combined: list[dict] = []
        for ds in dict.fromkeys([date_str_utc, date_str_us]):   # dedup, preserve order
            cache_key = f"{an_sport}_{ds}"
            if cache_key not in an_cache:
                print(f"  Fetching Action Network ({an_sport}) for {ds} ...")
                an_cache[cache_key] = fetch_an_games(ds, sport=an_sport)
                time.sleep(0.4)
            # Add only games not already in combined (by team names)
            existing_ids = {id(x) for x in combined}
            for gm in an_cache[cache_key]:
                if id(gm) not in existing_ids:
                    combined.append(gm)
        return combined

    results:     list[dict] = []
    calib_games: list[dict] = []

    for i, g in enumerate(games):
        title  = g["title"]
        end_dt = g["end_date"][:10]

        if (i + 1) % 25 == 0 or i == 0:
            print(f"\n  --- Progress: {i+1}/{len(games)} games processed ---")
            if results:
                _print_running_totals(results, args)

        print(f"\n  [{i+1}/{len(games)}] {title[:55]:55s}  ({end_dt})")

        # 2a — Polymarket price T-24h
        poly_price = get_poly_price_24h_before(g)
        if poly_price is None:
            print("    [skip] No pre-game Poly price available")
            continue
        if poly_price <= 0.01 or poly_price >= 0.99:
            print(f"    [skip] Price {poly_price:.3f} is at/near resolution (not pre-game)")
            continue
        print(f"    Poly T-24h price: {poly_price:.4f}")

        # 2b — Actual game outcome
        outcome = did_team1_win(g)
        if outcome is None:
            print("    [skip] Cannot determine outcome from outcomePrices")
            continue

        # Always record for calibration
        calib_games.append({"poly_price": poly_price, "outcome": outcome, "title": title})

        # 3a — Action Network Vegas odds
        an_games_list = get_an_for_game(g)
        an_game       = match_game_to_an(g, an_games_list)
        if an_game is None:
            print("    [no AN match] Calibration only")
            continue

        outcomes_list = g.get("outcomes", [])
        team1         = outcomes_list[0] if outcomes_list else title.split(" vs. ")[0]
        t1_away       = get_team1_is_away(an_game, team1)

        fair_prob = compute_book_fair_prob(an_game, t1_away)
        if fair_prob is None:
            print("    [skip] No moneyline from Action Network")
            continue

        edge        = fair_prob - poly_price
        outcome_str = "WIN" if outcome else "LOSS"
        print(f"    BookFair({team1[:20]}): {fair_prob:.4f}  edge={edge:+.4f}  {outcome_str}")

        # 4 — Simulate
        sim = simulate_entry(
            poly_price = poly_price,
            fair_prob  = fair_prob,
            outcome    = outcome,
            min_edge   = args.min_edge,
            bet_size   = args.bet_size,
        )

        results.append({
            "game":      title,
            "date":      end_dt,
            "sport":     sport_key,
            "book_fair": fair_prob,
            "poly":      poly_price,
            "edge":      edge,
            "outcome":   outcome_str,
            "entered":   sim is not None,
            "pnl":       sim["pnl"]   if sim else None,
            "brier":     sim["brier"] if sim else (poly_price - (1.0 if outcome else 0.0)) ** 2,
        })

        time.sleep(0.15)

    return results, calib_games


def _print_running_totals(results: list[dict], args) -> None:
    """Print a brief mid-run summary."""
    entered = [r for r in results if r["entered"] and r["pnl"] is not None]
    n_wins  = sum(1 for r in entered if r["outcome"] == "WIN")
    tot_pnl = sum(r["pnl"] for r in entered)
    wr      = n_wins / len(entered) * 100 if entered else 0
    pos_edge = sum(1 for r in results if r["edge"] > 0)
    print(
        f"    Running: {len(results)} w/ book data | "
        f"{len(entered)} entries | WR={wr:.0f}% | PnL=${tot_pnl:+.2f} | "
        f"positive-edge={pos_edge}/{len(results)}"
    )


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_sport_table(results: list[dict], label: str) -> None:
    """Print full per-game results table."""
    if not results:
        print(f"\n  No results with sportsbook data for {label}.")
        return

    print(f"\n{'=' * 110}")
    print(f"  {label}")
    print(f"  {'Game':<38} {'Date':>10} {'Sport':>6} {'BookFair':>9} {'PolyPrice':>9} "
          f"{'Edge':>7} {'Outcome':>7} {'Entry':>6} {'PnL':>9}")
    print("-" * 110)

    for r in results:
        pnl_str   = f"${r['pnl']:+.2f}" if r["pnl"] is not None else "   n/a"
        entry_str = "YES" if r["entered"] else " no"
        sport_lbl = r.get("sport", "")[:6]
        print(
            f"  {r['game'][:38]:<38} {r['date']:>10} {sport_lbl:>6} "
            f"{r['book_fair']:>9.4f} {r['poly']:>9.4f} "
            f"{r['edge']:>7.4f} {r['outcome']:>7} {entry_str:>6} {pnl_str:>9}"
        )

    print("=" * 110)


def print_sport_summary(results: list[dict], calib_games: list[dict], label: str, args) -> None:
    """Print per-sport summary statistics."""
    print(f"\n{'─' * 70}")
    print(f"  SUMMARY: {label}")
    print(f"{'─' * 70}")

    n_calib  = len(calib_games)
    n_book   = len(results)
    entered  = [r for r in results if r["entered"] and r["pnl"] is not None]
    n_entered = len(entered)
    n_wins   = sum(1 for r in entered if r["outcome"] == "WIN")
    total_pnl = sum(r["pnl"] for r in entered)
    wr_pct   = n_wins / n_entered * 100 if n_entered else 0.0
    avg_edge = sum(r["edge"] for r in entered) / n_entered if n_entered else 0.0

    all_brier = [r["brier"] for r in results] if results else []
    avg_brier = sum(all_brier) / len(all_brier) if all_brier else float("nan")

    print(f"  Games with Poly price+outcome: {n_calib}")
    print(f"  Games with sportsbook match:   {n_book}")
    print(f"  Entries (edge>={args.min_edge:.0%}):         {n_entered}")
    if n_entered:
        print(f"    Win rate:    {n_wins}/{n_entered} = {wr_pct:.1f}%")
        print(f"    Total PnL:   ${total_pnl:+.2f}  (${args.bet_size:.0f}/bet)")
        print(f"    Avg edge:    {avg_edge:.4f}")
        roi = total_pnl / (n_entered * args.bet_size) * 100
        print(f"    ROI:         {roi:+.1f}%")
    if all_brier:
        print(f"  Brier score: {avg_brier:.4f}  (lower=better; 0.25=coin flip)")

    if results:
        edges    = [r["edge"] for r in results]
        pos_edge = sum(1 for e in edges if e > 0)
        print(
            f"  Edge distribution: min={min(edges):.4f}  max={max(edges):.4f}  "
            f"positive={pos_edge}/{n_book} ({pos_edge/n_book*100:.0f}%)"
        )

    # Edge buckets for 4%+ frequency question
    ge4_pct = sum(1 for r in results if r["edge"] >= 0.04)
    if results:
        print(f"\n  Key question — edges >=4%: {ge4_pct}/{n_book} = {ge4_pct/n_book*100:.1f}% of games")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest sports line dislocation on Polymarket (2024-25 season)"
    )
    parser.add_argument(
        "--sport",
        choices=["march-madness", "nba", "ncaab", "all"],
        default="march-madness",
        help="Which sport/dataset to backtest (default: march-madness)",
    )
    parser.add_argument("--season", type=int, default=2025,
                        help="Season end year (default: 2025)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover markets only, skip simulation")
    parser.add_argument("--min-edge", type=float, default=0.04,
                        help="Minimum edge threshold (default: 0.04)")
    parser.add_argument("--bet-size", type=float, default=10.0,
                        help="Simulated bet size in dollars (default: $10)")
    parser.add_argument("--max-games", type=int, default=None,
                        help="Cap number of games processed per sport (for quick tests)")
    args = parser.parse_args()

    # Determine which sports to run
    if args.sport == "all":
        sports = ["march-madness", "nba", "ncaab"]
    else:
        sports = [args.sport]

    print("=" * 72)
    print("  Polymarket Sports Line Dislocation Backtest")
    print(f"  Sport(s): {', '.join(sports)}   Season: {args.season}")
    print("=" * 72)
    print(f"  min_edge={args.min_edge:.2%}  bet_size=${args.bet_size:.2f}"
          f"  dry_run={args.dry_run}")

    # Shared Action Network cache across sports (keyed by "sport_YYYYMMDD")
    an_cache: dict[str, list] = {}

    all_results:     list[dict] = []
    all_calib:       list[dict] = []
    per_sport_data:  dict       = {}

    for sport_key in sports:
        label   = SPORT_CONFIGS[sport_key]["label"]
        results, calib = process_sport(sport_key, args, an_cache)

        per_sport_data[sport_key] = (results, calib)
        all_results.extend(results)
        all_calib.extend(calib)

    if args.dry_run:
        sys.exit(0)

    # ── Per-sport output ─────────────────────────────────────────────────────
    for sport_key in sports:
        label            = SPORT_CONFIGS[sport_key]["label"]
        results, calib   = per_sport_data[sport_key]
        print_sport_table(results, label)
        print_calibration(calib)
        print_edge_buckets(results)
        print_sport_summary(results, calib, label, args)

    # ── Combined summary (only if multiple sports) ───────────────────────────
    if len(sports) > 1 and all_results:
        print(f"\n{'=' * 72}")
        print("  COMBINED SUMMARY (all sports)")
        print(f"{'=' * 72}")
        print_calibration(all_calib)
        print_edge_buckets(all_results)
        print_sport_summary(all_results, all_calib, "ALL SPORTS COMBINED", args)

        # Final key-question answer
        ge4 = [r for r in all_results if r["edge"] >= 0.04]
        if ge4:
            entered_ge4 = [r for r in ge4 if r["entered"]]
            wins_ge4    = sum(1 for r in entered_ge4 if r["outcome"] == "WIN")
            pnl_ge4     = sum(r["pnl"] for r in entered_ge4 if r["pnl"] is not None)
            print(f"\n  KEY FINDING: 4%+ edge opportunities")
            for sport_key in sports:
                sport_ge4 = [r for r in all_results if r.get("sport") == sport_key and r["edge"] >= 0.04]
                pct = len(sport_ge4) / len([r for r in all_results if r.get("sport") == sport_key]) * 100 \
                    if any(r.get("sport") == sport_key for r in all_results) else 0
                print(f"    {SPORT_CONFIGS[sport_key]['label']}: {len(sport_ge4)} games = {pct:.1f}% frequency")
            if entered_ge4:
                roi = pnl_ge4 / (len(entered_ge4) * args.bet_size) * 100
                print(f"    Win rate on 4%+ entries: {wins_ge4}/{len(entered_ge4)} = {wins_ge4/len(entered_ge4)*100:.1f}%")
                print(f"    ROI on 4%+ entries: {roi:+.1f}%")

    # ── Walk-forward analysis (NBA / multi-sport; skipped for march-madness-only) ──
    non_madness_sports = [s for s in sports if s != "march-madness"]
    if non_madness_sports:
        wf_results = [r for r in all_results if r.get("sport") != "march-madness"]
        if len(wf_results) >= 10:
            print_walk_forward(wf_results)
    elif sports == ["march-madness"] and len(all_results) >= 10:
        # march-madness only run: skip walk-forward (too few games for monthly breakdown)
        pass

    print()


if __name__ == "__main__":
    main()
