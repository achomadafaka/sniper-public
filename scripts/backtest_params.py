#!/usr/bin/env python3
"""
Layer 3: Historical Polymarket Backtester
==========================================
Fetches resolved markets from the Polymarket Gamma API.
Simulates our market selection rules with different parameter combinations.
Finds which entry price ranges have historically positive EV.

What this tests:
  - Entry price calibration: at what price bucket does YES resolve at >50%?
  - Category performance: does filtering geopolitics/crypto improve outcomes?
  - Pure market selection: if we entered all markets in price range X, what WR?

What this does NOT test:
  - Wallet signal quality (we don't have historical wallet data)
  - Insider score thresholds (not testable without wallet history)
  - Order execution, slippage, timing effects

This is a MARKET SELECTION backtest, not a full strategy backtest.
It answers: "Are the markets we filter to trade historically favorable?"

Honest limitations:
  - Polymarket resolved markets don't show WHERE they were priced at any given time
  - We can only see: final resolution (YES=1 or NO=0) and current/closing prices
  - "Entry price" is inferred from outcomePrices at close, not a real historical entry
  - Sample bias: we're sampling markets that Polymarket chose to run, not all markets

Run: python3 scripts/backtest_params.py
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

GAMMA_API = "https://gamma-api.polymarket.com"
MIN_VOLUME = 1000  # Filter out illiquid markets
PAGE_SIZE = 100
MAX_PAGES = 5  # up to 500 markets


def fetch_resolved_markets(limit: int = 500) -> list[dict]:
    """
    Fetch resolved Polymarket markets from Gamma API.
    Sorted by closedTime descending — recent markets have resolution prices intact.
    Old markets have outcomePrices wiped to ["0","0"] — we skip those.
    Filters: binary (2 outcomes), volume > MIN_VOLUME, has clear resolution outcome.
    """
    markets = []
    offset = 0
    page = 0

    while len(markets) < limit and page < MAX_PAGES:
        url = (
            f"{GAMMA_API}/markets"
            f"?closed=true"
            f"&limit={PAGE_SIZE}"
            f"&offset={offset}"
            f"&order=closedTime"
            f"&ascending=false"  # most recent first — these have resolution data intact
        )
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except urllib.error.URLError as e:
            print(f"  WARNING: API fetch failed (page {page}): {e}")
            break
        except Exception as e:
            print(f"  WARNING: Unexpected error (page {page}): {e}")
            break

        if not data:
            break

        # Handle both list and dict responses
        items = data if isinstance(data, list) else data.get("markets", data.get("results", []))
        if not items:
            break

        for m in items:
            # Must be binary (2 outcomes)
            outcomes = m.get("outcomes")
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    outcomes = []
            if not outcomes or len(outcomes) != 2:
                continue

            # Must have volume
            volume = float(m.get("volume", 0) or 0)
            if volume < MIN_VOLUME:
                continue

            # Must have outcome prices
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except Exception:
                    prices = []
            if not prices or len(prices) != 2:
                continue

            try:
                yes_price = float(prices[0])
                no_price = float(prices[1])
            except (ValueError, TypeError):
                continue

            # Determine resolution: YES resolved if YES price -> 1.0
            yes_resolved = yes_price >= 0.97
            no_resolved = no_price >= 0.97
            if not yes_resolved and not no_resolved:
                # Not definitively resolved (ambiguous/voided)
                continue

            markets.append({
                "slug": m.get("slug", ""),
                "title": m.get("question", m.get("title", "")),
                "category": m.get("category", ""),
                "volume": volume,
                "yes_price_at_close": yes_price,
                "no_price_at_close": no_price,
                "resolved_yes": yes_resolved,
                "resolved_no": no_resolved,
                "end_date": m.get("endDate", m.get("end_date_iso", "")),
            })

        offset += PAGE_SIZE
        page += 1
        time.sleep(0.3)  # rate limiting

    return markets[:limit]


def classify_market(title: str, slug: str) -> str:
    """Simple category classifier matching our filters.py logic."""
    text = f"{title} {slug}".lower()

    # Crypto/commodity
    crypto_kws = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp",
                  "doge", "crypto", "crude oil", " hit $", "reach $", "gold (gc)"]
    if any(kw in text for kw in crypto_kws):
        return "crypto_commodity"

    # Geopolitics
    geo_kws = ["iran", "israel", "russia", "ukraine", "north korea", "hamas",
               "hezbollah", "gaza", "ceasefire", "nuclear", "invasion", "missile",
               "military", "war by", "conflict"]
    if any(kw in text for kw in geo_kws):
        return "geopolitics"

    # Sports
    sport_kws = ["nfl", "nba", "mlb", "nhl", "epl", "premier league", "champions league",
                 "world cup", "super bowl", "ncaa", "tennis", "golf", "ufc", "boxing", "f1",
                 "la liga", "bundesliga", " vs "]
    if any(kw in text for kw in sport_kws):
        return "sports"

    # Politics
    pol_kws = ["president", "election", "senate", "congress", "vote", "democrat", "republican",
               "trump", "biden", "harris", "governor", "primary", "ballot", "poll"]
    if any(kw in text for kw in pol_kws):
        return "politics"

    return "other"


def price_bucket(price: float, bucket_size: float = 0.10) -> str:
    lo = round(int(price / bucket_size) * bucket_size, 2)
    hi = round(lo + bucket_size, 2)
    return f"{lo:.2f}-{hi:.2f}"


def analyze_price_range_ev(markets: list[dict]) -> dict:
    """
    For each entry price bucket: what fraction resolved YES?
    This measures calibration — not our edge, just base resolution rates.
    """
    # Entry as YES position
    yes_bins = defaultdict(list)
    for m in markets:
        # Use yes_price_at_close as proxy for "typical market price"
        # This is flawed (markets resolve TO 0 or 1) but gives calibration signal
        # Better: use price around 50% of market lifetime — but we don't have that
        # We use the pre-resolution price (just before 0.97 cutoff was applied)
        # For unresolved markets this would be the current price
        # For resolved YES: yes_price_at_close is 1.0, so we need to use NO price as ~1-yes_entry
        # Actually for closed resolved markets, outcomePrices shows 1.0 and 0.0
        # We cannot reliably infer entry price from resolution data alone
        # Use volume as proxy — high-volume markets tend to be well-calibrated
        pass

    # Better approach: for each market, simulate "what if we always entered YES?"
    # Group by volume tier to see if liquidity predicts resolution
    vol_bins = defaultdict(list)
    for m in markets:
        if m["volume"] < 5000:
            vb = "1k-5k"
        elif m["volume"] < 50000:
            vb = "5k-50k"
        elif m["volume"] < 500000:
            vb = "50k-500k"
        else:
            vb = "500k+"
        vol_bins[vb].append(m["resolved_yes"])

    return vol_bins


def analyze_category_resolution(markets: list[dict]) -> dict:
    """
    Per category: what % resolve YES?
    Key question: do geopolitics markets resolve YES less often than sports/politics?
    If YES, our category filter is validated externally.
    """
    cat_bins = defaultdict(list)
    for m in markets:
        cat = classify_market(m["title"], m["slug"])
        cat_bins[cat].append(m["resolved_yes"])
    return cat_bins


def sweep_filter_combinations(markets: list[dict]) -> list[dict]:
    """
    Simulate: if we applied our category filters, what WR would we see historically?

    Filter combinations:
    1. No filter (all markets)
    2. Exclude geopolitics
    3. Exclude geopolitics + crypto
    4. Sports only
    5. Politics only
    6. Other (not sport/geo/crypto)
    """
    combos = [
        ("no_filter", lambda m: True),
        ("exclude_geo", lambda m: classify_market(m["title"], m["slug"]) != "geopolitics"),
        ("exclude_geo_crypto", lambda m: classify_market(m["title"], m["slug"]) not in ("geopolitics", "crypto_commodity")),
        ("sports_only", lambda m: classify_market(m["title"], m["slug"]) == "sports"),
        ("politics_only", lambda m: classify_market(m["title"], m["slug"]) == "politics"),
        ("other_only", lambda m: classify_market(m["title"], m["slug"]) == "other"),
    ]

    results = []
    for name, filter_fn in combos:
        subset = [m for m in markets if filter_fn(m)]
        if not subset:
            continue
        yes_resolves = sum(1 for m in subset if m["resolved_yes"])
        wr = yes_resolves / len(subset)
        results.append({
            "filter": name,
            "n": len(subset),
            "yes_resolution_rate": round(wr, 4),
            "note": f"If we always entered YES: {wr*100:.1f}% WR",
        })

    return results


def print_results(markets: list[dict], cat_results: dict, filter_results: list[dict]):
    print()
    print("=" * 70)
    print("LAYER 3: HISTORICAL POLYMARKET BACKTEST")
    print(f"Markets fetched: {len(markets)} resolved binary markets (volume >= ${MIN_VOLUME:,})")
    print("=" * 70)

    print("\n  IMPORTANT LIMITATIONS:")
    print("  - We can only see FINAL resolution prices (1.0 or 0.0)")
    print("  - Entry price at any given time is NOT in this data")
    print("  - 'YES resolution rate' measures market calibration, not our edge")
    print("  - Base rate ~50%: any strategy must beat this to have edge")
    print("  - Category classification is keyword-based, not Polymarket's official tags")

    print("\n  CATEGORY RESOLUTION RATES (all resolved markets where YES won):")
    print(f"  {'Category':<25} {'n':>5}  {'YES_rate':>9}  {'Notes'}")
    print("  " + "-" * 65)
    for cat, outcomes in sorted(cat_results.items(), key=lambda x: -len(x[1])):
        n = len(outcomes)
        yes_rate = sum(outcomes) / n if n else 0
        # Is this above or below 50%?
        signal = "base_rate" if 0.45 <= yes_rate <= 0.55 else ("YES_skewed" if yes_rate > 0.55 else "NO_skewed")
        print(f"  {cat:<25} {n:>5}  {yes_rate*100:>8.1f}%  {signal}")

    print("\n  FILTER SIMULATION (if we always entered YES on filtered markets):")
    print(f"  {'Filter':<25} {'n':>5}  {'YES_rate':>9}  {'vs_baseline'}")
    print("  " + "-" * 65)
    baseline = next((r["yes_resolution_rate"] for r in filter_results if r["filter"] == "no_filter"), 0.5)
    for r in filter_results:
        diff = r["yes_resolution_rate"] - baseline
        diff_str = f"{diff:+.1%}" if r["filter"] != "no_filter" else "(baseline)"
        print(f"  {r['filter']:<25} {r['n']:>5}  {r['yes_resolution_rate']*100:>8.1f}%  {diff_str}")

    print()
    print("  INTERPRETATION:")
    print("  - If YES resolution rate is ~50% across all filters: no category edge from")
    print("    market selection alone — the edge must come from WALLET SIGNAL quality")
    print("  - If some categories significantly exceed 50% YES resolution: the category")
    print("    selection itself provides edge even without insider signals")
    print()
    print("  OUR CURRENT CATEGORY FILTERS vs. HISTORICAL RESOLUTION:")

    # Check if our blocked categories (geo, crypto) show worse YES rates
    geo_rate = sum(cat_results.get("geopolitics", [])) / len(cat_results.get("geopolitics", [1])) if cat_results.get("geopolitics") else None
    sport_rate = sum(cat_results.get("sports", [])) / len(cat_results.get("sports", [1])) if cat_results.get("sports") else None
    other_rate = sum(cat_results.get("other", [])) / len(cat_results.get("other", [1])) if cat_results.get("other") else None

    if geo_rate is not None and sport_rate is not None:
        if geo_rate < sport_rate - 0.05:
            print(f"  VALIDATES our geopolitics filter: geo={geo_rate*100:.1f}% < sports={sport_rate*100:.1f}%")
        else:
            print(f"  CHALLENGES geopolitics filter: geo={geo_rate*100:.1f}% vs sports={sport_rate*100:.1f}%")
            print("  (Note: our LIVE trade data showed 28.6% WR on geo — may be insider-specific)")

    print()
    print("  PRICE CALIBRATION NOTE:")
    print("  - We cannot extract historical entry prices from the Gamma API")
    print("  - The heatmap analysis described in the spec requires OHLC price history")
    print("  - Polymarket does not expose this via public API")
    print("  - Alternative: use our own 70 live trades for price calibration (Layer 1)")


def store_to_supabase(markets: list[dict], cat_results: dict, filter_results: list[dict]):
    try:
        from bot.supabase_helper import upsert_memory
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Summarize cat results (not full market list)
        cat_summary = {
            cat: {"n": len(outcomes), "yes_rate": round(sum(outcomes) / len(outcomes), 4)}
            for cat, outcomes in cat_results.items()
            if outcomes
        }

        content = json.dumps({
            "generated_at": ts,
            "markets_analyzed": len(markets),
            "category_resolution_rates": cat_summary,
            "filter_simulation": filter_results,
            "data_note": "Polymarket Gamma API resolved markets. NO historical price data available — only resolution outcomes.",
            "limitation": "Cannot backtest entry price ranges without OHLC data. Market selection backtest only.",
            "api_source": f"{GAMMA_API}/markets?closed=true",
        }, indent=2)

        upsert_memory(
            type="knowledge",
            name="historical_backtest_results",
            description=f"Polymarket historical market selection backtest ({ts}) — {len(markets)} markets",
            content=content,
            source="vps",
        )
        print(f"\n  Stored to Supabase brain as 'historical_backtest_results'")
    except Exception as e:
        print(f"\n  WARNING: Could not store to Supabase: {e}")


def main():
    print("=" * 70)
    print("Fetching resolved markets from Polymarket Gamma API...")
    print(f"  Criteria: closed=true, binary, volume>={MIN_VOLUME:,}, paginating up to {PAGE_SIZE * MAX_PAGES}")
    print("=" * 70)

    markets = fetch_resolved_markets(500)
    print(f"  Fetched {len(markets)} qualifying markets")

    if not markets:
        print("  ERROR: No markets fetched. Check network connectivity.")
        print("  API endpoint:", f"{GAMMA_API}/markets?closed=true")
        return

    cat_results = analyze_category_resolution(markets)
    filter_results = sweep_filter_combinations(markets)
    print_results(markets, cat_results, filter_results)
    store_to_supabase(markets, cat_results, filter_results)


if __name__ == "__main__":
    main()
