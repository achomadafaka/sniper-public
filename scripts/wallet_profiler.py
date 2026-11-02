#!/usr/bin/env python3
"""
Wallet Profiler — multilayered analysis of Polymarket trading wallets.

5-layer analysis framework:
  Layer 1: Classification (market maker, algorithmic bot, manual trader, hybrid)
  Layer 2: Domain (sports breakdown, political, crypto + concentration metrics)
  Layer 3: Behavior (entry/exit patterns, timing, sizing, hold duration)
  Layer 4: Performance (per-domain WR/PnL, profit/loss ratio, drawdown)
  Layer 5: Replicability (edge source identification, data requirements)

Usage:
  python3 scripts/wallet_profiler.py 0xWALLET_ADDRESS
  python3 scripts/wallet_profiler.py 0xWALLET --save-brain  # save to Supabase
  python3 scripts/wallet_profiler.py 0xWALLET --json        # JSON output
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("DB_PATH", str(ROOT / "sniper_tracker.db"))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gate 0 — Polymarket Lifetime P&L Disqualification
# ---------------------------------------------------------------------------

# Wallets with lifetime PnL below this are disqualified regardless of signals.
# Default: -$10,000 (configurable via env var GATE0_MIN_LIFETIME_PNL)
GATE0_LIFETIME_PNL_FLOOR: float = float(os.getenv("GATE0_MIN_LIFETIME_PNL", "-10000"))

# Wallets with fewer than this many Polymarket trades are treated as unproven/phantom.
GATE0_MIN_TRADE_COUNT: int = int(os.getenv("GATE0_MIN_TRADE_COUNT", "5"))
LOCAL_LEADERBOARD_MAX_AGE_SECONDS: int = int(
    os.getenv("LOCAL_LEADERBOARD_MAX_AGE_SECONDS", "7200")
)

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# PnL field names Polymarket uses (try each in order)
_PNL_FIELDS = ("profit", "pnl", "earnings", "netPnl", "netEarnings", "realizedPnl",
               "totalPnl", "lifetimePnl", "lifetimeProfit")


def fetch_wallet_lifetime_stats(wallet: str) -> Dict[str, Any]:
    """Fetch Polymarket lifetime stats for a wallet — used as Gate 0.

    Strategy (in priority order):
      1. Fresh local DB external_leaderboard_entries with time_period='ALL'
         (only available for top-50 leaderboard wallets — useful for big winners)
      2. Profile page __NEXT_DATA__ hydration — parses the 'user-stats' React
         Query cache for lifetime pnl, trade_count, and volume.
      3. Activity existence check — if zero activity records returned, wallet
         is likely phantom or does not exist on Polymarket.

    Returns:
        {
          "exists":       bool   — False = phantom/non-existent wallet
          "lifetime_pnl": float|None  — None = could not determine
          "trade_count":  int|None
          "volume":       float|None
          "source":       str    — where the data came from
          "disqualified": bool   — True if Gate 0 fails
          "disqualify_reason": str|None
        }
    """
    wallet = wallet.strip().lower()
    result: Dict[str, Any] = {
        "exists": None,          # None = unknown until checked
        "lifetime_pnl": None,
        "trade_count": None,
        "volume": None,
        "source": "none",
        "disqualified": False,
        "disqualify_reason": None,
    }

    # ── Step 1: Local leaderboard DB (fast, no network) ──────────────────────
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        lb_row = conn.execute(
            """SELECT official_pnl, official_volume, fetched_at
               FROM external_leaderboard_entries
               WHERE wallet = ? AND time_period = 'ALL' AND order_by = 'PNL'
               ORDER BY fetched_at DESC LIMIT 1""",
            (wallet,),
        ).fetchone()
        conn.close()

        if lb_row:
            fetched_at = int(lb_row["fetched_at"] or 0)
            if fetched_at and time.time() - fetched_at > LOCAL_LEADERBOARD_MAX_AGE_SECONDS:
                logger.debug(
                    "Ignoring stale local leaderboard row for %s fetched_at=%s",
                    wallet[:12],
                    fetched_at,
                )
            elif not fetched_at:
                logger.debug(
                    "Ignoring local leaderboard row with no fetched_at for %s",
                    wallet[:12],
                )
            else:
                pnl = float(lb_row["official_pnl"])
                result.update({
                    "exists": True,
                    "lifetime_pnl": pnl,
                    "volume": float(lb_row["official_volume"]),
                    "source": "leaderboard_all",
                })
                # Leaderboard ALL-time only shows positive performers — a wallet
                # appearing here with negative PnL is very unusual but check anyway.
                if pnl < GATE0_LIFETIME_PNL_FLOOR:
                    result["disqualified"] = True
                    result["disqualify_reason"] = (
                        f"Gate0: lifetime PnL ${pnl:+,.0f} < floor ${GATE0_LIFETIME_PNL_FLOOR:,.0f}"
                        f" (source: leaderboard_all)"
                    )
                return result
    except Exception as exc:
        logger.debug("Leaderboard DB check failed for %s: %s", wallet[:12], exc)

    # ── Step 2: Profile page __NEXT_DATA__ scrape ────────────────────────────
    try:
        profile_url = f"https://polymarket.com/profile/{wallet}"
        resp = requests.get(
            profile_url,
            timeout=15,
            headers={"User-Agent": "polymarket-tracker/1.0"},
        )

        if resp.status_code == 404:
            result.update({
                "exists": False,
                "source": "profile_404",
                "disqualified": True,
                "disqualify_reason": "Gate0: wallet does not exist on Polymarket (404)",
            })
            return result

        if resp.ok:
            match = _NEXT_DATA_RE.search(resp.text)
            if match:
                try:
                    next_data = json.loads(match.group(1))
                except json.JSONDecodeError:
                    next_data = {}

                queries = (
                    next_data.get("props", {})
                    .get("pageProps", {})
                    .get("dehydratedState", {})
                    .get("queries", [])
                )
                # We need data from two separate queries:
                #   /api/profile/volume → pnl (lifetime), amount (volume)
                #   user-stats          → trades (trade count)
                # Iterate all queries collecting both before breaking.
                for query in queries:
                    if not isinstance(query, dict):
                        continue
                    qkey = query.get("queryKey", [])
                    data = query.get("state", {}).get("data", {})
                    if not isinstance(data, dict):
                        continue
                    key0 = str(qkey[0] if qkey else "")

                    if key0 == "/api/profile/volume":
                        # Primary source for lifetime P&L and volume
                        result["exists"] = True
                        result["source"] = "profile_next_data"
                        pnl_val = data.get("pnl")
                        if pnl_val is not None:
                            try:
                                result["lifetime_pnl"] = float(pnl_val)
                            except (TypeError, ValueError):
                                pass
                        vol_val = data.get("amount")
                        if vol_val is not None:
                            try:
                                result["volume"] = float(vol_val)
                            except (TypeError, ValueError):
                                pass

                    elif key0 == "user-stats":
                        result["exists"] = True
                        if result["source"] == "none":
                            result["source"] = "profile_next_data"
                        # Trade count from user-stats
                        for field in ("trades", "numTrades", "tradeCount", "predictions"):
                            val = data.get(field)
                            if val is not None:
                                try:
                                    result["trade_count"] = int(val)
                                    break
                                except (TypeError, ValueError):
                                    pass

    except Exception as exc:
        logger.debug("Profile page fetch failed for %s: %s", wallet[:12], exc)

    # ── Step 3: Activity existence check (fallback) ───────────────────────────
    # If profile page didn't give us existence info, try activity API
    if result["exists"] is None:
        try:
            r = requests.get(
                "https://data-api.polymarket.com/activity",
                params={"user": wallet, "limit": 10, "offset": 0},
                timeout=10,
            )
            if r.ok:
                activity_records = r.json() if isinstance(r.json(), list) else []
                if len(activity_records) == 0:
                    result.update({
                        "exists": False,
                        "source": "activity_empty",
                        "disqualified": True,
                        "disqualify_reason": (
                            "Gate0: no Polymarket activity found — phantom or new wallet"
                        ),
                    })
                    return result
                else:
                    result["exists"] = True
                    if result["source"] == "none":
                        result["source"] = "activity_exists"
        except Exception as exc:
            logger.debug("Activity check failed for %s: %s", wallet[:12], exc)

    # ── Gate 0 evaluation ─────────────────────────────────────────────────────
    if result.get("exists") is False and not result["disqualified"]:
        result["disqualified"] = True
        result["disqualify_reason"] = "Gate0: wallet does not exist on Polymarket"

    elif result.get("lifetime_pnl") is not None:
        pnl = result["lifetime_pnl"]
        if pnl < GATE0_LIFETIME_PNL_FLOOR:
            result["disqualified"] = True
            result["disqualify_reason"] = (
                f"Gate0: lifetime PnL ${pnl:+,.0f} < floor ${GATE0_LIFETIME_PNL_FLOOR:,.0f}"
                f" (source: {result['source']})"
            )

    elif result.get("trade_count") is not None:
        if result["trade_count"] < GATE0_MIN_TRADE_COUNT:
            result["disqualified"] = True
            result["disqualify_reason"] = (
                f"Gate0: only {result['trade_count']} Polymarket trades"
                f" < minimum {GATE0_MIN_TRADE_COUNT}"
            )

    return result


# ---------------------------------------------------------------------------
# Data Fetchers
# ---------------------------------------------------------------------------

def fetch_positions(wallet: str) -> List[Dict]:
    """Fetch current positions from Polymarket data API."""
    try:
        r = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": wallet, "sizeThreshold": 0},
            timeout=15,
        )
        return r.json() if r.ok else []
    except Exception:
        return []


def fetch_activity(wallet: str, limit: int = 500) -> List[Dict]:
    """Fetch trade activity history."""
    all_acts: List[Dict] = []
    offset = 0
    while len(all_acts) < limit:
        try:
            r = requests.get(
                "https://data-api.polymarket.com/activity",
                params={"user": wallet, "limit": 100, "offset": offset},
                timeout=15,
            )
            if not r.ok:
                break
            batch = r.json()
            if not batch:
                break
            all_acts.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
            time.sleep(0.3)
        except Exception:
            break
    return all_acts[:limit]


def fetch_db_data(wallet: str) -> Dict[str, Any]:
    """Fetch our internal data on this wallet."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    ua = conn.execute(
        "SELECT * FROM user_analysis WHERE wallet = ?", (wallet,)
    ).fetchone()

    paper_trades = conn.execute(
        """SELECT trade_id, market_title, outcome, entry_price, exit_price,
                  pnl_usdc, roi_percent, exit_classification, status,
                  signal_insider_score, opened_at, closed_at, size_usdc
           FROM paper_trades WHERE wallet = ? AND duplicate_of IS NULL
           ORDER BY opened_at DESC""",
        (wallet,),
    ).fetchall()

    signals = conn.execute(
        "SELECT COUNT(*) FROM position_signals WHERE wallet = ?", (wallet,)
    ).fetchone()[0]

    conn.close()
    return {
        "user_analysis": dict(ua) if ua else None,
        "paper_trades": [dict(r) for r in paper_trades],
        "signal_count": signals,
    }


# ---------------------------------------------------------------------------
# Layer 1: Classification
# ---------------------------------------------------------------------------

def classify_wallet(positions: List[Dict], activity: List[Dict]) -> Dict[str, Any]:
    """Determine if market maker, algorithmic bot, manual trader, or hybrid."""

    # Both-sides detection (market maker check)
    by_market: Dict[str, set] = defaultdict(set)
    for p in positions:
        title = p.get("title") or "?"
        outcome = p.get("outcome") or "?"
        by_market[title].add(outcome)

    total_markets = len(by_market)
    both_sides = sum(1 for outcomes in by_market.values() if len(outcomes) >= 2)
    both_sides_ratio = both_sides / max(total_markets, 1)

    # Algorithmic detection: check for identical price fills
    buy_prices = [float(a.get("price") or 0) for a in activity if a.get("side") == "BUY" and a.get("price")]
    price_counts = Counter(round(p, 3) for p in buy_prices)
    most_common_price_count = price_counts.most_common(1)[0][1] if price_counts else 0
    price_repetition_ratio = most_common_price_count / max(len(buy_prices), 1)

    # Frequency detection
    buys = sum(1 for a in activity if a.get("side") == "BUY")
    sells = sum(1 for a in activity if a.get("side") == "SELL")
    buy_sell_ratio = buys / max(sells, 1)

    # Classification logic
    if both_sides_ratio > 0.50:
        classification = "market_maker"
        confidence = "high" if both_sides_ratio > 0.80 else "medium"
    elif price_repetition_ratio > 0.30 and buys > 50:
        classification = "algorithmic_bot"
        confidence = "high" if price_repetition_ratio > 0.50 else "medium"
    elif buys < 20 and len(set(round(p, 2) for p in buy_prices)) > buys * 0.7:
        classification = "manual_trader"
        confidence = "medium"
    else:
        classification = "hybrid"
        confidence = "low"

    return {
        "classification": classification,
        "confidence": confidence,
        "both_sides_ratio": round(both_sides_ratio, 3),
        "both_sides_markets": both_sides,
        "total_markets": total_markets,
        "buy_count": buys,
        "sell_count": sells,
        "buy_sell_ratio": round(buy_sell_ratio, 2),
        "price_repetition_ratio": round(price_repetition_ratio, 3),
        "most_repeated_price": price_counts.most_common(1)[0] if price_counts else None,
    }



# Stage 5 of the wallet-taxonomy migration removed:
#   - SPORT_KEYWORDS, POLITICAL_KEYWORDS, CRYPTO_KEYWORDS (hardcoded taxonomy)
#   - analyze_domain, analyze_behavior, analyze_performance,
#     assess_replicability, profile_wallet, save_to_brain, __main__ CLI
#
# The CLI entry point and category-based analytics have been superseded by
# scripts/wallet_archetype_extractor.py (opaque-tag layer 1-3 + execution
# profile) and docs/superpowers/specs/2026-04-24-tag-analytics.md (layer 4).
#
# This module now retains only the Gate0 lifetime stats fetch + DB helpers
# that other production modules still import.
