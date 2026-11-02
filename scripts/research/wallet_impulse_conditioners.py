#!/usr/bin/env python3
"""Condition the buy-impulse response on streak, buyer order, and market cap.

This study extends `wallet_impulse_screen.py`. That screen ranks wallets by the median
response. This study asks three different questions on the same scored events:

1. STREAK - does a wallet's recent scored responses predict the response of its next buy?
2. ORDER  - inside a coin episode, does the first tracked buyer get a bigger response than
   the buyers who come after? Which wallets lead episodes that other tracked wallets join?
3. MCAP   - is the response larger on small source-claimed market caps?

The measurement is identical to the screen: t0 is our trusted receive stamp, the baseline
is the first clean tape mark at or after t0, and an event with no mark inside the age cap
is censored. All aggregates cluster by mint. Read-only; writes nothing.

    python3 scripts/research/wallet_impulse_conditioners.py --since 2026-07-21
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import random
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wallet_impulse_screen import MAX_BASELINE_AGE_S, _iso, load_tape, measure

HORIZON = 60
EPISODE_GAP_S = 600
STREAK_K = 3

EVENTS_SQL = """
SELECT fomo_event_id, source_user_id, source_handle, mint, network_id,
       received_at_normalized, received_at, their_price, their_size_usd,
       their_market_cap, is_first_buy
FROM memecoin_events
WHERE side = 'buy'
  AND COALESCE(received_at_normalized, received_at) >= ?
  AND their_price > 0
ORDER BY COALESCE(received_at_normalized, received_at)
"""

MCAP_BANDS = (
    ("<50k", 0, 50_000),
    ("50-250k", 50_000, 250_000),
    ("250k-1M", 250_000, 1_000_000),
    ("1-10M", 1_000_000, 10_000_000),
    (">10M", 10_000_000, float("inf")),
)


def mint_clustered_median(samples, rounds=2000, seed=7):
    """(median, ci_lo, ci_hi) of response values, bootstrap over mints."""
    by_mint = defaultdict(list)
    for s in samples:
        by_mint[s["mint"]].append(s["r"])
    values = [v for vs in by_mint.values() for v in vs]
    if not values:
        return None, None, None
    med = st.median(values)
    mints = list(by_mint)
    if len(mints) < 3:
        return med, None, None
    rng = random.Random(seed)
    meds = []
    for _ in range(rounds):
        drawn = [v for _ in mints for v in by_mint[mints[rng.randrange(len(mints))]]]
        if drawn:
            meds.append(st.median(drawn))
    meds.sort()
    return med, meds[int(0.025 * (len(meds) - 1))], meds[int(0.975 * (len(meds) - 1))]


def describe(label, samples):
    med, lo, hi = mint_clustered_median(samples)
    if med is None:
        print(f"  {label:<14} n=0")
        return
    n = len(samples)
    mints = len({s["mint"] for s in samples})
    hit5 = sum(1 for s in samples if s["r"] >= 5.0) / n
    ci = f"[{lo:+.1f},{hi:+.1f}]" if lo is not None else "[-]"
    print(f"  {label:<14} n={n:<5} mints={mints:<4} med={med:+6.2f}% "
          f">=+5%: {hit5:5.1%}  CI95={ci}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--since", default="2026-07-21")
    ap.add_argument("--max-baseline-age", type=float, default=MAX_BASELINE_AGE_S)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA temp_store=MEMORY")

    tape = load_tape(conn, args.since)
    scored = []
    total = 0
    for event in conn.execute(EVENTS_SQL, (args.since,)):
        total += 1
        got = measure(tape, event, (HORIZON,), args.max_baseline_age)
        if not got or HORIZON not in got["responses"]:
            continue
        t0 = _iso(event["received_at_normalized"] or event["received_at"])
        scored.append({
            "wallet": (event["source_user_id"], event["source_handle"]),
            "handle": event["source_handle"] or str(event["source_user_id"])[:8],
            "mint": event["mint"],
            "t": t0.timestamp(),
            "r": got["responses"][HORIZON],
            "mcap": event["their_market_cap"],
            "is_first_buy": event["is_first_buy"],
        })
    scored.sort(key=lambda s: s["t"])
    print(f"buy events since {args.since}: {total}; scored at +{HORIZON}s: {len(scored)} "
          f"({len({s['mint'] for s in scored})} mints, "
          f"{len({s['wallet'] for s in scored})} wallets)")

    # ---- 1. STREAK ------------------------------------------------------------
    print(f"\n1. STREAK - trailing {STREAK_K} scored responses of the SAME wallet, any coin.")
    by_wallet = defaultdict(list)
    hot, cold, warm = [], [], []
    for s in scored:
        hist = by_wallet[s["wallet"]]
        if len(hist) >= STREAK_K:
            trail = [h["r"] for h in hist[-STREAK_K:]]
            pos = sum(1 for r in trail if r > 0)
            (hot if pos == STREAK_K else cold if pos == 0 else warm).append(s)
        by_wallet[s["wallet"]].append(s)
    describe(f"hot ({STREAK_K}/{STREAK_K} up)", hot)
    describe("mixed", warm)
    describe(f"cold (0/{STREAK_K})", cold)
    print("  Caution: the streak and the outcome share the day regime. A hot-vs-cold gap can be "
          "a market\n  regime, not a wallet property. Treat a null as a null; treat a gap as a "
          "candidate only.")

    # ---- 2. ORDER -------------------------------------------------------------
    print(f"\n2. ORDER - rank inside a coin episode (gap > {EPISODE_GAP_S}s starts a new episode).")
    by_mint = defaultdict(list)
    for s in scored:
        by_mint[s["mint"]].append(s)
    rank_buckets = defaultdict(list)
    lead = defaultdict(lambda: {"led": 0, "joined_by": 0, "rs": []})
    for mint, events in by_mint.items():
        events.sort(key=lambda s: s["t"])
        episode = []
        for s in events:
            if episode and s["t"] - episode[-1]["t"] > EPISODE_GAP_S:
                episode = []
            episode.append(s)
            rank = len(episode)
            rank_buckets[min(rank, 3)].append(s)
            if rank == 1:
                s["_ep_lead"] = True
            elif rank == 2:
                leader = episode[0]
                rec = lead[leader["wallet"]]
                rec["joined_by"] += 1
        for s in events:
            if s.pop("_ep_lead", False):
                rec = lead[s["wallet"]]
                rec["led"] += 1
                rec["rs"].append(s)
                rec["handle"] = s["handle"]
    describe("rank 1 (lead)", rank_buckets[1])
    describe("rank 2", rank_buckets[2])
    describe("rank 3+", rank_buckets[3])

    print("\n  Wallets that LEAD episodes which at least one other tracked wallet then joins")
    print("  (>=3 led episodes, ranked by joined episodes):")
    rows = [(k, v) for k, v in lead.items() if v["led"] >= 3 and v["joined_by"] >= 1]
    rows.sort(key=lambda kv: kv[1]["joined_by"], reverse=True)
    for key, v in rows[:12]:
        med, lo, hi = mint_clustered_median(v["rs"])
        ci = f"[{lo:+.1f},{hi:+.1f}]" if lo is not None else "[-]"
        print(f"    {v['handle'][:20]:<20} led={v['led']:<4} joined={v['joined_by']:<4} "
              f"lead-med={med:+6.2f}% CI95={ci}")
    if not rows:
        print("    none reached the minimums")

    # ---- 3. MCAP --------------------------------------------------------------
    print("\n3. MCAP - response by source-claimed market cap at the event (untrusted field).")
    for label, lo_b, hi_b in MCAP_BANDS:
        band = [s for s in scored if s["mcap"] and lo_b <= s["mcap"] < hi_b]
        describe(label, band)
    describe("mcap missing", [s for s in scored if not s["mcap"]])
    print("  Caution: the tape covers only coins our runner traded, so every band is "
        "gate-selected.\n  A small-cap medium here is NOT an executable fill; route cost on "
        "these caps is separate.")



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    main()
