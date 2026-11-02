#!/usr/bin/env python3
"""Rank wallets by the market's RESPONSE to their buys, priced from where WE could have entered.

Ranking wallets by their own PnL has failed repeatedly here: `their_price` is the trader's own
fill, so every follow-through horizon credits the pump their own buy caused, and summed PnL just
ranks whoever held the biggest lottery ticket. This screen never uses their PnL and never uses
their fill as a baseline.

    python3 scripts/research/wallet_impulse_screen.py --since 2026-07-21
    python3 scripts/research/wallet_impulse_screen.py --since 2026-07-21 --min-events 8

## The measurement

For each buy event we observed:

1. t0 = the instant WE observed it (`received_at_normalized`, the trusted receive stamp - never
   `created_at`, which is source-claimed and untrusted).
2. baseline = the first clean tape mark at or after t0. This is the earliest price a copier could
   actually have paid, so the response is measured from OUR achievable entry, not from theirs.
   The baseline's age is recorded and an event whose first mark is older than --max-baseline-age
   is CENSORED, not scored: we could not have priced it, so we do not get to claim it.
3. response(h) = mark at t0+h against the baseline, for each horizon.

A wallet's score is the median response across its events, clustered by mint.

## Why obs+3s is not the baseline

The original idea was to price the impulse at obs+3s. The tape cannot support it: of 400 sampled
buy events since 2026-08-05, only 7% had any clean mark within 5s, 20% within 15s, 36% within 30s
and 53% within 60s. A +3s baseline would silently score the 7% of coins whose pools we happened to
have bound, which is a coverage screen wearing an impulse screen's clothes. The first-available
baseline with an explicit age is the honest version of the same idea.

## The placebo

A wallet can look good merely by buying on days when everything pumped. Each wallet's response is
therefore reported against a SAME-DAY control: the pooled median response of every OTHER wallet's
events on the same UTC days, weighted to that wallet's own day mix. A wallet that does not beat
its own day mix has shown nothing.

## What this screen does NOT do

It does not measure copy PnL, and a high response is not an edge on its own: it says the market
moves after this wallet buys, not that a copier keeps the move after cost. Entry drift (median
+10.8%) and exit slippage are separate and must be subtracted before any expectation is claimed.
Treat the output as a candidate list for arming, never as a projected return.

Read-only. Opens the tracker with mode=ro and writes nothing.
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
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta

HORIZONS = (60, 300, 900)
MAX_BASELINE_AGE_S = 120
MIN_EVENTS = 5
BOOTSTRAP = 2000
SEED = 7

EVENTS_SQL = """
SELECT fomo_event_id, source_user_id, source_handle, mint, network_id,
       received_at_normalized, received_at, their_price, their_size_usd
FROM memecoin_events
WHERE side = 'buy'
  AND COALESCE(received_at_normalized, received_at) >= ?
  AND their_price > 0
ORDER BY COALESCE(received_at_normalized, received_at)
"""

# One pass over the tape for every mint we care about. A per-event query was O(events) round
# trips against a multi-million-row table and did not finish in 15 minutes.
MARKS_SQL = """
SELECT mint, sweep_ts, mid FROM memecoin_position_marks
WHERE quality != 'quarantined_spike' AND mid > 0 AND sweep_ts >= ?
ORDER BY mint, sweep_ts
"""


def _iso(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def load_tape(conn, since):
    """{mint: ([epoch...], [mid...])} sorted by time, for bisect lookups."""
    tape = defaultdict(lambda: ([], []))
    for mint, ts, mid in conn.execute(MARKS_SQL, (since,)):
        moment = _iso(ts)
        if moment is None:
            continue
        times, mids = tape[mint]
        times.append(moment.timestamp())
        mids.append(mid)
    return tape


def measure(tape, event, horizons, max_baseline_age_s):
    """One event -> {baseline_age_s, responses{h: pct}} or None when we could not have priced it."""
    t0 = _iso(event["received_at_normalized"] or event["received_at"])
    if t0 is None or event["mint"] not in tape:
        return None
    times, mids = tape[event["mint"]]
    start = t0.timestamp()
    i = bisect_left(times, start)
    if i >= len(times):
        return None
    age = times[i] - start
    if age > max_baseline_age_s:
        return None                      # censored: no price we could have acted on
    baseline = mids[i]
    responses = {}
    for h in horizons:
        j = bisect_right(times, start + h) - 1
        if j <= i:                       # need a mark strictly after the baseline
            continue
        responses[h] = (mids[j] / baseline - 1.0) * 100.0
    if not responses:
        return None
    return {"baseline_age_s": age, "responses": responses,
            "mint": event["mint"], "day": t0.date().isoformat()}


def collect(conn, since, horizons, max_baseline_age_s):
    tape = load_tape(conn, since)
    by_wallet = defaultdict(list)
    seen = defaultdict(int)
    for event in conn.execute(EVENTS_SQL, (since,)):
        key = (event["source_user_id"], event["source_handle"])
        seen[key] += 1
        got = measure(tape, event, horizons, max_baseline_age_s)
        if got:
            by_wallet[key].append(got)
    return by_wallet, seen


def _mint_clustered_ci(samples, horizon, rounds, seed):
    """Bootstrap the median by resampling MINTS, not events. Two events on one mint are not two
    independent observations of the market's reaction."""
    by_mint = defaultdict(list)
    for s in samples:
        if horizon in s["responses"]:
            by_mint[s["mint"]].append(s["responses"][horizon])
    mints = list(by_mint)
    if len(mints) < 3:
        return None, None
    rng = random.Random(seed)
    medians = []
    for _ in range(rounds):
        drawn = [v for _ in mints
                 for v in by_mint[mints[rng.randrange(len(mints))]]]
        if drawn:
            medians.append(st.median(drawn))
    if not medians:
        return None, None
    medians.sort()
    return medians[int(0.025 * (len(medians) - 1))], medians[int(0.975 * (len(medians) - 1))]


def day_control(by_wallet, key, horizon):
    """Median response of every OTHER wallet's events, restricted to this wallet's own UTC days.

    A wallet that only buys on days the whole market ran will show a high raw response. The
    control removes exactly that: it is the same days, the same tape, other people's picks.
    """
    days = {s["day"] for s in by_wallet[key] if horizon in s["responses"]}
    others = [s["responses"][horizon]
              for other, samples in by_wallet.items() if other != key
              for s in samples if s["day"] in days and horizon in s["responses"]]
    return st.median(others) if len(others) >= 10 else None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--since", default="2026-07-21")
    ap.add_argument("--horizons", default=",".join(str(h) for h in HORIZONS))
    ap.add_argument("--max-baseline-age", type=float, default=MAX_BASELINE_AGE_S)
    ap.add_argument("--min-events", type=int, default=MIN_EVENTS)
    ap.add_argument("--rank-horizon", type=int, default=60)
    ap.add_argument("--bootstrap", type=int, default=BOOTSTRAP)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--armed-only", action="store_true")
    args = ap.parse_args()

    horizons = tuple(int(x) for x in args.horizons.split(","))
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA temp_store=MEMORY")

    armed = {r[0] for r in conn.execute(
        "SELECT source_user_id FROM memecoin_wallets WHERE real_approved=1")}
    excluded = {r[0] for r in conn.execute(
        "SELECT source_user_id FROM memecoin_wallets WHERE excluded=1 OR paused=1")}

    by_wallet, seen = collect(conn, args.since, horizons, args.max_baseline_age)

    print(f"buy events since {args.since}: {sum(seen.values())} across {len(seen)} wallets")
    print(f"priced (baseline within {args.max_baseline_age:g}s): "
          f"{sum(len(v) for v in by_wallet.values())}")
    print(f"\nRanked by median response at +{args.rank_horizon}s, measured from OUR first "
          f"available price.\nA wallet must beat its own same-day control to mean anything.\n")
    head = (f"{'wallet':<20} {'ev':>4} {'cov':>5} {'age':>6} "
            + " ".join(f"{'+' + str(h) + 's':>8}" for h in horizons)
            + f" {'ctrl':>7} {'edge':>7} {'CI95':>17} {'mints':>6}  flags")
    print(head)
    print("-" * len(head))

    rows = []
    for key, samples in by_wallet.items():
        uid, handle = key
        if args.armed_only and uid not in armed:
            continue
        if len(samples) < args.min_events:
            continue
        if args.rank_horizon not in {h for s in samples for h in s["responses"]}:
            continue
        med = {h: st.median([s["responses"][h] for s in samples if h in s["responses"]])
               for h in horizons
               if any(h in s["responses"] for s in samples)}
        if args.rank_horizon not in med:
            continue
        lo, hi = _mint_clustered_ci(samples, args.rank_horizon, args.bootstrap, args.seed)
        ctrl = day_control(by_wallet, key, args.rank_horizon)
        rows.append({
            "handle": handle or uid[:8], "uid": uid, "n": len(samples),
            "coverage": len(samples) / max(1, seen[key]),
            "age": st.median([s["baseline_age_s"] for s in samples]),
            "med": med, "lo": lo, "hi": hi, "ctrl": ctrl,
            "edge": None if ctrl is None else med[args.rank_horizon] - ctrl,
            # Mints AT THE RANKED HORIZON, not overall. A row whose ranked median rests on two
            # coins must not borrow support from coins that only have a longer-horizon reading.
            "mints": len({s["mint"] for s in samples if args.rank_horizon in s["responses"]}),
            "mints_any": len({s["mint"] for s in samples}),
            # How much of the +60s pop survives to the longest horizon. The screen's whole point
            # is a tradeable impulse; one that has fully decayed by the time we could exit is a
            # measurement, not an opportunity.
            "retained": (med.get(max(horizons)) if args.rank_horizon in med else None),
            "armed": uid in armed, "excluded": uid in excluded,
        })

    rows.sort(key=lambda r: (r["edge"] if r["edge"] is not None else -1e9), reverse=True)
    for r in rows:
        cells = " ".join(f"{r['med'][h]:>+8.1f}" if h in r["med"] else f"{'-':>8}"
                         for h in horizons)
        ci = (f"[{r['lo']:>+6.1f},{r['hi']:>+6.1f}]" if r["lo"] is not None else f"{'-':>17}")
        flags = ("ARMED " if r["armed"] else "") + ("EXCLUDED " if r["excluded"] else "")
        if r["n"] < 10 or r["mints"] < 6:
            flags += "THIN "
        if r["retained"] is not None and r["med"][args.rank_horizon] > 0 and \
                r["retained"] < 0.25 * r["med"][args.rank_horizon]:
            flags += "DECAYS "
        if r["lo"] is not None and r["lo"] <= 0:
            flags += "CI-CROSSES-0 "
        ctrl_cell = f"{r['ctrl']:+7.1f}" if r["ctrl"] is not None else f"{'-':>7}"
        edge_cell = f"{r['edge']:+7.1f}" if r["edge"] is not None else f"{'-':>7}"
        print(f"{r['handle'][:20]:<20} {r['n']:>4} {r['coverage']:>4.0%} {r['age']:>5.0f}s "
              f"{cells} {ctrl_cell} {edge_cell} {ci} {r['mints']:>6}  {flags.strip()}")

    print("\ncov = share of this wallet's observed buys we could price at all. A low number means "
          "the screen\n      saw a biased slice of what it did, not that it did less.")
    print("age = median seconds from our observation to the first price we could have acted on.")
    print("edge = median response minus the same-day control. Negative means the market moved "
          "MORE\n       after other people's buys on the same days.")
    print("CI = mint-clustered bootstrap of the ranked horizon. Crossing zero means not "
          "distinguishable\n     from no response. A dash means fewer than 3 mints carry the "
          "ranked horizon at all.")
    print("mints = distinct coins carrying the RANKED horizon, not the wallet's whole history.")
    print("DECAYS = less than a quarter of the +%ds move survives to +%ds. The impulse is real "
          "but\n         not tradeable unless the exit is inside that window."
          % (args.rank_horizon, max(horizons)))
    print(f"\nSELECTION WARNING: {len(rows)} wallets cleared the minimums and the table is sorted "
          "by edge.\nThe top rows are the extreme of a large search. Nothing here is a verdict; "
          "it is a\ncandidate list that needs a forward test on new events.")



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    main()
