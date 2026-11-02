#!/usr/bin/env python3
"""Rank memecoin wallets from LIVE Fomo reads. READ-ONLY — touches no ledger, arms nothing.

The local paper ledger is a copy record: it holds what OUR runner did with a signal, not what the
trader did. Ranking traders on it measures our own execution. This reads the venue directly.

FOUR limits are structural, not fixable here. Every one of them is printed with the table so a
number never travels without them:

1. Fomo returns no transaction hash, and the wallet fields do not reconcile against public chain
   history. Every figure is PLATFORM-REPORTED. It cannot authorize funded arming on its own.
2. `swaps?limit=N` is a RECENCY cap, so the visible window is shorter for a busier wallet. Dollar
   totals across wallets with different windows are not comparable, which is why the ranking key
   is per-day and the raw window is shown.
3. A sell whose buy predates the window has NO cost basis. Those sells are counted and reported as
   `unmatched`, never priced at zero — an unpriced sell is missing data, not a free win.
4. The leaderboard is served by lifetime PnL, so this pool is already survivor-selected. It cannot
   answer "is this trader good", only "among wallets the venue already promotes, which look best".

Ranking uses median close and top-coin share, not summed PnL. Summed PnL ranks lottery tickets:
see finding-memecoin-single-wallet-roster-2026-08-11 (6 of 8 top solana wallets were NEGATIVE
without a single coin).
"""
from __future__ import annotations

import argparse
import pathlib
import statistics
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone

sys.path.insert(0, "/home/bigpapi/Desktop/Sniper")

CHAIN_NAME = {1399811149: "solana", 4663: "robin"}
QUOTE_TOLERANCE = 0.01   # |usd - amount| / usd under this => the leg is a USD stablecoin
PACE_S = 2.5             # seconds between live calls; Fomo throttles well before this rate

# Fomo answers a throttled swaps request with HTTP 200 and an EMPTY LIST, not an error. An empty
# response is therefore AMBIGUOUS — it means "no trades" or "we refused you" and the two are
# indistinguishable in the payload. Verified 2026-08-11: the same wallet returned 100 rows, then 0
# rows on four consecutive requests, then 100 identical rows (same sha over swap ids, 4x) after a
# 90s pause. Complete-or-empty, never truncated, so a fetch that DOES return rows can be trusted.
# Retry empty, and if it stays empty report it as UNKNOWN. Scoring a throttled wallet as "no
# trades" would silently rank an active trader as inactive.
EMPTY_RETRIES = 2


def _with_backoff(call, tries=5, base_sleep=2.0):
    """Fomo rate-limits hard. Back off on 429 instead of losing the sweep to one refusal."""
    import time
    import urllib.error
    for attempt in range(tries):
        try:
            result = call()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == tries - 1:
                raise
            time.sleep(base_sleep * (2 ** attempt))
            continue
        time.sleep(PACE_S)          # pace EVERY call, not only the ones that already failed
        return result
    return None


def fetch_swaps_resolved(fetch, expect_active, retries=EMPTY_RETRIES, sleep_s=8.0):
    """(swaps, resolved). resolved=False means empty AND we expected trades, so it reads as a
    throttle and must be reported as unknown rather than zero.

    `expect_active` comes from OUR OWN feed: a wallet we watched trade recently should not answer
    empty. A genuinely quiet wallet gets no retry at all — paying 24s of backoff for every dormant
    wallet turned a 10-minute sweep into a 90-minute one.
    """
    import time
    swaps = fetch() or []
    if swaps or not expect_active:
        return swaps, True
    for attempt in range(retries):
        time.sleep(sleep_s * (attempt + 1))
        swaps = fetch() or []
        if swaps:
            return swaps, True
    return [], False


def _ledger_pool():
    """Every wallet we track, leaderboard or not. The leaderboard is served by lifetime PnL, so
    ranking only its members answers "who does the venue already promote". This pool does not."""
    import sqlite3
    try:
        conn = sqlite3.connect("file:/home/bigpapi/Desktop/Sniper/sniper_tracker.db?mode=ro",
                               uri=True, timeout=30)
        return [{"id": r[0], "userHandle": r[1], "followers": None} for r in conn.execute(
            "SELECT source_user_id, handle FROM memecoin_wallets "
            "WHERE source_user_id IS NOT NULL AND COALESCE(excluded,0)=0")]
    except sqlite3.Error:
        return []


def _active_ids(days):
    """Wallet ids OUR feed saw trade inside `days`.

    Consumed as PROVEN-DORMANT evidence, never as proven-active: a wallet outside this set is one
    we can call quiet, and everything else defaults to retry. The window must be at least as wide
    as the pool window. Earlier this was hardcoded to 2 days while the pool admitted 7 days of
    activity, so a wallet whose ten events all fell in days 3-7 was treated as legitimately quiet
    and its throttled empty response became "0 closes" — an active trader silently ranked inactive.
    A missing DB returns an empty set, which makes every wallet look non-dormant and retry. That is
    the safe direction.
    """
    import sqlite3
    from datetime import timedelta
    try:
        conn = sqlite3.connect("file:/home/bigpapi/Desktop/Sniper/sniper_tracker.db?mode=ro",
                               uri=True, timeout=30)
        cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT source_user_id FROM memecoin_events WHERE received_at > ?", (cut,))}
    except sqlite3.Error:
        return set()


def _feed_pool(days, min_events):
    """Wallets seen ACTING in the global feed, restricted to signal_source='global_feed'.

    This pool is NOT unselected. It is activity-selected (>= min_events in the window) and
    large-trade-selected (the feed only carries trades above its own size floor). The claim it
    supports is narrower than "unselected": it is not selected ON AN OUTCOME, unlike the
    leaderboard (served by lifetime PnL) or our tracked list (our own past choices).

    Identity is source_user_id — 412 wallet ids share a handle with another id and 14 handles map
    to several ids, so a handle is not a key (finding 2026-08-11).
    """
    import sqlite3
    from datetime import timedelta
    try:
        conn = sqlite3.connect("file:/home/bigpapi/Desktop/Sniper/sniper_tracker.db?mode=ro",
                               uri=True, timeout=30)
        cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return [{"id": r[0], "userHandle": r[1], "followers": None} for r in conn.execute(
            "SELECT source_user_id, MAX(source_handle) FROM memecoin_events "
            "WHERE received_at > ? AND source_user_id IS NOT NULL "
            # global_feed ONLY. follow_activity rows exist because WE follow that wallet, so
            # including them re-imports our own past choices into a pool whose whole purpose is to
            # be free of them. It admitted 2 extra wallets out of 499.
            "AND signal_source = 'global_feed' "
            "GROUP BY source_user_id HAVING COUNT(*) >= ? "
            "ORDER BY COUNT(*) DESC", (cut, int(min_events)))]
    except sqlite3.Error:
        return []


def _armed_from_ledger():
    """handle(lower) -> armed chains. Shown next to each row so the ranking is read against what
    the funded runner is ALREADY doing, instead of as an abstract list."""
    import sqlite3
    try:
        conn = sqlite3.connect("file:/home/bigpapi/Desktop/Sniper/sniper_tracker.db?mode=ro",
                               uri=True, timeout=30)
        return {str(r[0]).lower(): (r[1] or "") for r in conn.execute(
            "SELECT handle, real_chains FROM memecoin_wallets "
            "WHERE real_approved=1 AND COALESCE(excluded,0)=0")}
    except sqlite3.Error:
        return {}


def _handles_from_ledger():
    """uid -> handle from the local wallet table. Identity only — no performance number is read
    from SQLite here, which is the whole point of this script."""
    import sqlite3
    try:
        conn = sqlite3.connect("file:/home/bigpapi/Desktop/Sniper/sniper_tracker.db?mode=ro",
                               uri=True, timeout=30)
        return {r[0]: r[1] for r in conn.execute(
            "SELECT source_user_id, handle FROM memecoin_wallets WHERE source_user_id IS NOT NULL")}
    except sqlite3.Error:
        return {}


def _f(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return default if v != v else v


def is_quote_leg(human_amount, usd_amount):
    """True when a leg is a USD stablecoin: its token amount equals its USD value.

    Chain-agnostic on purpose — it needs no per-chain USDC address list to maintain, and a chain
    we have never seen classifies correctly the first time.
    """
    usd = _f(usd_amount)
    amt = _f(human_amount)
    if usd <= 0 or amt <= 0:
        return False
    return abs(usd - amt) / usd <= QUOTE_TOLERANCE


def classify(swap):
    """(side, token, chain, usd, qty) for one swap. side is 'buy', 'sell' or None."""
    in_is_quote = is_quote_leg(swap.get("inHumanAmount"), swap.get("humanUsdAmountIn"))
    out_is_quote = is_quote_leg(swap.get("outHumanAmount"), swap.get("humanUsdAmountOut"))
    if in_is_quote == out_is_quote:
        return None, None, None, 0.0, 0.0    # stable->stable, or neither leg priced
    if in_is_quote:
        return ("buy", swap.get("outTokenAddress"), swap.get("outNetworkId"),
                _f(swap.get("humanUsdAmountIn")), _f(swap.get("outHumanAmount")))
    return ("sell", swap.get("inTokenAddress"), swap.get("inNetworkId"),
            _f(swap.get("humanUsdAmountOut")), _f(swap.get("inHumanAmount")))


def round_trips(swaps):
    """Aggregate swaps into ROUND TRIPS per (chain, token). One trip is one observation.

    A trip opens on the first buy from a flat balance, absorbs every later buy and partial sell,
    and closes when the balance returns to flat. Scaling out of one winner in three tranches is
    ONE observation, not three: counting each partial sell separately inflated n and inflated the
    hit rate, because the slices of a single winning position are perfectly correlated and are not
    independent draws.

    Returns (trips, unmatched_sells, open_trips).
      trips          - closed round trips with a known cost basis
      unmatched_sells- sells against a flat balance; the buy predates the window so the profit is
                       UNKNOWN. Never priced at zero.
      open_trips     - still holding at the end of the window. CENSORED: not a close, and above
                       all not a loss. Dropping them silently would bias toward traders who exit.
    Cost basis is average cost within the trip, which is what a partial sell actually consumes.
    """
    state = {}
    trips, unmatched, open_trips = [], 0, []
    for swap in sorted(swaps, key=lambda s: str(s.get("createdAt") or "")):
        side, token, chain, usd, qty = classify(swap)
        if side is None or not token or qty <= 0 or usd <= 0:
            continue
        key = (chain, token)
        pos = state.get(key)
        if side == "buy":
            if pos is None:
                pos = state[key] = {"qty": 0.0, "cost": 0.0, "proceeds": 0.0,
                                    "chain": chain, "token": token, "buys": 0, "sells": 0,
                                    "opened_at": str(swap.get("createdAt") or "")}
            pos["qty"] += qty
            pos["cost"] += usd
            pos["buys"] += 1
            continue
        if pos is None or pos["qty"] <= 1e-12:
            unmatched += 1                      # sell with no open basis in this window
            continue
        take = min(qty, pos["qty"])
        pos["proceeds"] += usd * (take / qty)   # only the part backed by observed basis
        pos["qty"] -= take
        pos["sells"] += 1
        if qty - take > 1e-9:
            unmatched += 1                      # the uncovered remainder is still unknown
        if pos["qty"] <= max(1e-9, 1e-6 * (pos["cost"] or 1.0)):
            trips.append({"chain": chain, "token": token, "cost": pos["cost"],
                          "proceeds": pos["proceeds"],
                          "pnl": pos["proceeds"] - pos["cost"],
                          "ret": ((pos["proceeds"] - pos["cost"]) / pos["cost"]
                                  if pos["cost"] > 0 else 0.0),
                          "buys": pos["buys"], "sells": pos["sells"],
                          "at": str(swap.get("createdAt") or "")})
            state.pop(key, None)
    open_trips = [{"chain": v["chain"], "token": v["token"], "cost": v["cost"]}
                  for v in state.values() if v["qty"] > 0]
    return trips, unmatched, open_trips


def window_days(swaps):
    stamps = sorted(str(s.get("createdAt") or "") for s in swaps if s.get("createdAt"))
    if len(stamps) < 2:
        return 0.0
    try:
        lo = datetime.fromisoformat(stamps[0].replace("Z", "+00:00"))
        hi = datetime.fromisoformat(stamps[-1].replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max((hi - lo).total_seconds() / 86400.0, 0.0)


def summarize_cell(handle, uid, chain, trips, unmatched, open_trips, days):
    """One (wallet, chain) cell. Arming is per-chain, so the cell is the unit of both the ranking
    and the decision. A pooled wallet number can hide a losing chain inside a winning one — the
    earlier table ranked songz on a pooled row while its solana and robin cells disagreed."""
    total_sells = len(trips) + unmatched
    pnl = sum(t["pnl"] for t in trips)
    by_coin = defaultdict(float)
    for t in trips:
        by_coin[t["token"]] += t["pnl"]
    gains = sorted((v for v in by_coin.values() if v > 0), reverse=True)
    return {
        "handle": handle, "uid": uid, "chain": CHAIN_NAME.get(chain, str(chain)),
        "trips": len(trips), "coins": len({t["token"] for t in trips}),
        "days": days,
        "unmatched_pct": (100.0 * unmatched / total_sells) if total_sells else 0.0,
        "open_censored": len(open_trips),
        "pnl": pnl,
        "pnl_per_day": (pnl / days) if days > 0 else 0.0,
        "hit_rate": (100.0 * sum(1 for t in trips if t["pnl"] > 0) / len(trips)) if trips else 0.0,
        "median_ret": 100.0 * statistics.median([t["ret"] for t in trips]) if trips else 0.0,
        "top1_share": (100.0 * gains[0] / pnl) if gains and pnl > 0 else 0.0,
        "pnl_ex_top1": pnl - (gains[0] if gains else 0.0),
    }


def cells_for_wallet(handle, uid, swaps):
    """Split a wallet's swaps into per-chain cells. Chains are never pooled."""
    trips, unmatched, open_trips = round_trips(swaps)
    days = window_days(swaps)
    chains = ({t["chain"] for t in trips} | {o["chain"] for o in open_trips})
    out = []
    for chain in chains:
        c_trips = [t for t in trips if t["chain"] == chain]
        c_open = [o for o in open_trips if o["chain"] == chain]
        # unmatched is not chain-attributed by round_trips, so scale it by this chain's share of
        # closed trips rather than inventing a per-chain count we did not measure.
        share = (len(c_trips) / len(trips)) if trips else 0.0
        out.append(summarize_cell(handle, uid, chain, c_trips,
                                  int(round(unmatched * share)), c_open, days))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="live Fomo wallet ranking (read-only)")
    ap.add_argument("--top", type=int, default=50, help="leaderboard users to pull (API caps at 50)")
    ap.add_argument("--feed-days", type=int, default=7, help="feed pool: activity window")
    ap.add_argument("--feed-min-events", type=int, default=10,
                    help="feed pool: minimum feed events in the window")
    ap.add_argument("--limit-pool", type=int, default=0,
                    help="cap the pool size (0 = no cap). A cap is LOGGED, never silent.")
    ap.add_argument("--pool", choices=("leaderboard", "ledger", "both", "feed"), default="both",
                    help="leaderboard is survivor-selected by lifetime PnL; ledger is every wallet "
                         "we track; feed is every wallet seen ACTING, which is the only pool not "
                         "selected on an outcome")
    ap.add_argument("--swaps", type=int, default=100, help="swaps per user (recency-capped)")
    ap.add_argument("--min-trips", type=int, default=8,
                    help="hide CELLS under this many closed round trips; small n is not a rank")
    ap.add_argument("--max-unmatched", type=float, default=30.0,
                    help="hide wallets whose unmatched-sell %% exceeds this")
    a = ap.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv("/home/bigpapi/Desktop/Sniper/.env")
    from sniper.feeds import fomo_account as fa
    from sniper.feeds.fomo_feed import FomoAuth

    auth = FomoAuth()
    board = []
    if a.pool in ("leaderboard", "both"):
        board = _with_backoff(lambda: fa.fetch_leaderboard(auth, limit=a.top)) or []
        if not board and a.pool == "leaderboard":
            print("leaderboard returned nothing — check the Fomo session", file=sys.stderr)
            return 1
    if a.pool in ("ledger", "both", "feed"):
        seen_ids = {e.get("id") for e in board}
        extra = _ledger_pool() if a.pool != "feed" else []
        if a.pool == "feed":
            extra = _feed_pool(a.feed_days, a.feed_min_events)
        board = board + [e for e in extra if e["id"] not in seen_ids]
    if not board:
        print("empty pool", file=sys.stderr)
        return 1
    if a.limit_pool and len(board) > a.limit_pool:
        print(f"NOTE: pool capped {len(board)} -> {a.limit_pool}. The rest were NOT searched.")
        board = board[:a.limit_pool]

    known = _handles_from_ledger()   # identity only; every performance number stays live
    # dormancy evidence must cover AT LEAST the pool window, else a wallet active only in the
    # older part of the window is wrongly judged quiet. 2 was narrower than the 7-day pool.
    dormancy_days = max(a.feed_days, 7)
    active_ids = _active_ids(dormancy_days)
    feed_ever = _active_ids(3650)      # every wallet our feed has ever carried
    armed = _armed_from_ledger()     # so the table shows what we are ALREADY betting on

    rows, skipped, unknown, seen_handles = [], [], [], set()
    for entry in board:
        uid = entry.get("id") or entry.get("userId")
        if not uid:
            continue
        # The leaderboard row already carries userHandle AND followers, so no /v2/users call is
        # needed. That call was the only 429 source in the sweep and it bought nothing.
        handle = entry.get("userHandle") or known.get(uid) or uid[:8]
        try:
            swaps, resolved = fetch_swaps_resolved(
                lambda: _with_backoff(lambda: fa.fetch_recent_swaps(auth, uid, limit=a.swaps)),
                # Dormancy must be PROVEN, not inferred from absence. A wallet counts as quiet
                # only when our feed knows it (it appears in feed history) and did not trade in the
                # dormancy window. A wallet our feed never covered proves nothing, so it retries
                # and may end as UNKNOWN. Absence of evidence is not evidence of dormancy.
                expect_active=not (uid in feed_ever and uid not in active_ids))
        except Exception as exc:                      # one bad user must not kill the sweep
            skipped.append((handle, f"fetch failed: {exc}"))
            continue
        if not resolved:
            unknown.append(handle)
            continue
        done = len(seen_handles)
        seen_handles.add(uid)
        print(f"  [{done+1}/{len(board)}] {handle}", file=sys.stderr, flush=True)
        cells = cells_for_wallet(handle, uid, swaps)
        if not cells:
            skipped.append((handle, "no closed round trip in window"))
            continue
        for cell in cells:
            cell["followers"] = _f(entry.get("followers"))
            cell["armed"] = armed.get(str(handle).lower(), "")
            label = f"{handle}/{cell['chain']}"
            if cell["trips"] < a.min_trips:
                skipped.append((label, f"only {cell['trips']} round trips"))
                continue
            if cell["unmatched_pct"] > a.max_unmatched:
                skipped.append((label, f"{cell['unmatched_pct']:.0f}% sells lack cost basis"))
                continue
            rows.append(cell)

    # median return per close, then PnL per day. Neither is summed PnL, on purpose.
    rows.sort(key=lambda r: (r["median_ret"], r["pnl_per_day"]), reverse=True)

    print(f"LIVE Fomo ranking · {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"pool={a.pool} n={len(board)} swaps/user={a.swaps} min_trips={a.min_trips} "
          f"max_unmatched={a.max_unmatched:.0f}%")
    print("unit = one (wallet, chain) CELL; one observation = one closed ROUND TRIP\n")
    print(f"{'handle':<17}{'chain':<8}{'win':>5}{'med%':>8}{'trips':>7}{'coins':>7}{'days':>6}"
          f"{'$/day':>9}{'ex-top1':>10}{'top1%':>7}{'unm%':>6}{'held':>6}  armed")
    print("-" * 106)
    for r in rows:
        print(f"{str(r['handle'])[:16]:<17}{r['chain'][:7]:<8}{r['hit_rate']:>4.0f}%"
              f"{r['median_ret']:>+8.1f}{r['trips']:>7}{r['coins']:>7}{r['days']:>6.1f}"
              f"{r['pnl_per_day']:>+9.0f}{r['pnl_ex_top1']:>+10.0f}{r['top1_share']:>6.0f}%"
              f"{r['unmatched_pct']:>5.0f}%{r['open_censored']:>6}  {r.get('armed') or ''}")
    if not rows:
        print("  (nothing cleared the filters)")

    if unknown:
        print(f"\nUNKNOWN ({len(unknown)}) — every attempt returned an empty list, which is a "
              f"throttle and a quiet wallet at once.\n  These are NOT zero and NOT ranked: "
              f"{', '.join(str(u)[:16] for u in unknown[:14])}"
              f"{' ...' if len(unknown) > 14 else ''}")
    if skipped:
        print(f"\nexcluded ({len(skipped)}) — printed so the filter is not a silent truncation:")
        for handle, why in skipped[:20]:
            print(f"  {handle[:24]:<26}{why}")
        if len(skipped) > 20:
            print(f"  ... and {len(skipped)-20} more")

    print("\nLIMITS — these numbers are PLATFORM-REPORTED and cannot arm a wallet by themselves:")
    print("  1. Fomo returns no transaction hash; nothing here is reconciled against a public chain.")
    print("  2. The swap window is recency-capped, so a busier wallet gets a shorter window.")
    print("     Compare $/day and med%, never raw PnL, across different windows.")
    print("  3. Sells whose buy predates the window are UNMATCHED (unm%), not counted as wins.")
    print("  4. Pool selection: leaderboard is survivor-selected by lifetime PnL; the feed pool is")
    print("     activity- and large-trade-selected. Not outcome-selected is NOT unselected.")
    print("  5. `held` counts positions still open at the window edge. They are CENSORED, not losses.")
    print("  Funded use needs a transaction-linked indexer first.")
    return 0


def _self_check():
    """Covers every defect this scanner has actually had, not only the arithmetic.

    The previous version tested FIFO matching alone. Pool selection, throttled empties and retry
    behavior were untested, and all three were wrong.
    """
    def buy(token, usd, qty, at, chain=4663):
        return {"createdAt": at, "inHumanAmount": usd, "humanUsdAmountIn": usd,
                "outHumanAmount": qty, "humanUsdAmountOut": usd,
                "inTokenAddress": "USDC", "outTokenAddress": token, "outNetworkId": chain}

    def sell(token, usd, qty, at, chain=4663):
        return {"createdAt": at, "inHumanAmount": qty, "humanUsdAmountIn": usd,
                "outHumanAmount": usd, "humanUsdAmountOut": usd,
                "inTokenAddress": token, "outTokenAddress": "USDC", "inNetworkId": chain}

    # --- round trips: partial exits are ONE observation, not three ---
    trips, unmatched, open_trips = round_trips([
        buy("CAT", 100.0, 1000.0, "2026-08-11T00:00:00Z"),
        sell("CAT", 60.0, 333.0, "2026-08-11T01:00:00Z"),
        sell("CAT", 60.0, 333.0, "2026-08-11T02:00:00Z"),
        sell("CAT", 60.0, 334.0, "2026-08-11T03:00:00Z"),
    ])
    assert len(trips) == 1, f"three tranches of one position must be ONE trip, got {len(trips)}"
    assert unmatched == 0 and open_trips == []
    assert abs(trips[0]["pnl"] - 80.0) < 1e-6, trips[0]
    assert trips[0]["sells"] == 3

    # scaling out of a winner must not become 3 wins (the hit-rate inflation)
    assert sum(1 for t in trips if t["pnl"] > 0) == 1

    # --- a still-open position is CENSORED, never a loss ---
    trips, unmatched, open_trips = round_trips([
        buy("DOG", 100.0, 500.0, "2026-08-11T00:00:00Z"),
        sell("DOG", 20.0, 100.0, "2026-08-11T01:00:00Z"),
    ])
    assert trips == [], "a partly-sold position is not closed"
    assert len(open_trips) == 1 and unmatched == 0

    # --- a sell with no basis is UNMATCHED, not a free win ---
    trips, unmatched, _ = round_trips([sell("CAT", 150.0, 1000.0, "2026-08-11T01:00:00Z")])
    assert trips == [] and unmatched == 1

    # --- re-entry after flat is a SECOND, independent trip ---
    trips, _, _ = round_trips([
        buy("CAT", 100.0, 1000.0, "2026-08-11T00:00:00Z"),
        sell("CAT", 150.0, 1000.0, "2026-08-11T01:00:00Z"),
        buy("CAT", 100.0, 1000.0, "2026-08-11T02:00:00Z"),
        sell("CAT", 50.0, 1000.0, "2026-08-11T03:00:00Z"),
    ])
    assert len(trips) == 2, trips
    assert trips[0]["pnl"] > 0 and trips[1]["pnl"] < 0

    # --- chains are never pooled ---
    cells = cells_for_wallet("w", "u", [
        buy("CAT", 100.0, 1000.0, "2026-08-11T00:00:00Z", chain=4663),
        sell("CAT", 150.0, 1000.0, "2026-08-11T01:00:00Z", chain=4663),
        buy("SOL1", 100.0, 1000.0, "2026-08-11T00:00:00Z", chain=1399811149),
        sell("SOL1", 50.0, 1000.0, "2026-08-11T01:00:00Z", chain=1399811149),
    ])
    assert len(cells) == 2, cells
    by_chain = {c["chain"]: c for c in cells}
    assert by_chain["robin"]["pnl"] > 0 and by_chain["solana"]["pnl"] < 0, by_chain
    assert by_chain["robin"]["hit_rate"] == 100.0 and by_chain["solana"]["hit_rate"] == 0.0

    # --- ordering must not matter: trips sort by createdAt ---
    a1 = round_trips([buy("CAT", 100.0, 1000.0, "2026-08-11T00:00:00Z"),
                      sell("CAT", 150.0, 1000.0, "2026-08-11T01:00:00Z")])[0]
    a2 = round_trips([sell("CAT", 150.0, 1000.0, "2026-08-11T01:00:00Z"),
                      buy("CAT", 100.0, 1000.0, "2026-08-11T00:00:00Z")])[0]
    assert len(a1) == len(a2) == 1 and abs(a1[0]["pnl"] - a2[0]["pnl"]) < 1e-9

    # --- throttled empty: resolved=False when we EXPECT trades ---
    calls = {"n": 0}

    def always_empty():
        calls["n"] += 1
        return []

    swaps, resolved = fetch_swaps_resolved(always_empty, expect_active=True,
                                           retries=2, sleep_s=0.0)
    assert swaps == [] and resolved is False, "an expected-active empty must be UNKNOWN"
    assert calls["n"] == 3, f"must retry, got {calls['n']} calls"

    # --- a proven-dormant wallet is resolved immediately, with NO retry ---
    calls["n"] = 0
    swaps, resolved = fetch_swaps_resolved(always_empty, expect_active=False,
                                           retries=2, sleep_s=0.0)
    assert swaps == [] and resolved is True and calls["n"] == 1, calls

    # --- a retry that succeeds resolves ---
    seq = [[], [], [buy("CAT", 1.0, 1.0, "2026-08-11T00:00:00Z")]]
    swaps, resolved = fetch_swaps_resolved(lambda: seq.pop(0), expect_active=True,
                                           retries=2, sleep_s=0.0)
    assert resolved is True and len(swaps) == 1

    # --- pool selection: the feed pool must filter to signal_source='global_feed' ---
    src = pathlib.Path(__file__).read_text()
    pool_sql = src[src.index("def _feed_pool"):src.index("def _armed_from_ledger")]
    assert "signal_source = 'global_feed'" in pool_sql, \
        "the feed pool must exclude follow_activity, which re-imports our own past choices"

    # --- dormancy predicate: absence from our feed proves nothing ---
    pred = src[src.index("expect_active=not ("):]
    assert "uid in feed_ever" in pred.split(")")[0] + ")", \
        "dormancy must require positive feed evidence, not mere absence"

    assert is_quote_leg(100.0, 100.0) and not is_quote_leg(1000.0, 100.0)
    print("self-check OK")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        sys.exit(main())
