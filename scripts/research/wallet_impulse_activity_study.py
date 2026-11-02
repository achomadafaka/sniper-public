#!/usr/bin/env python3
"""Activity/volatility floor, pile-up trigger, and coin predictability on the impulse events.

Adds what the prior cuts did not have:
- pre-event tape ACTIVITY: distinct consecutive mid changes in the 10 min before t0
  (row count lies about cadence; distinct changes do not)
- pre-event VOLATILITY: stdev of log returns over those distinct changes
- PILE: how many distinct tracked wallets bought this mint in the 120s up to and incl. t0
- PREDICTABILITY: after k tracked buys of a coin in 1h, chance of another within 10 min

Also renders event-anchored charts (-10min..+10min) so a human (and I) can SEE the shapes.
Read-only.
"""
import math
import sqlite3
import statistics as st
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/home/bigpapi/Desktop/Sniper/scripts/research")
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:          # bare `python3 scripts/research/X.py` puts
    sys.path.insert(0, str(REPO_ROOT))      # scripts/research on sys.path, not the repo
from sniper.persistence.db_core import get_readonly_connection
from wallet_impulse_screen import _iso, load_tape, measure
from wallet_impulse_conditioners import EVENTS_SQL, mint_clustered_median

DB = "/home/bigpapi/Desktop/Sniper/sniper_tracker.db"
SINCE = "2026-07-21"
OUT = Path("/tmp/claude-1000/-home-bigpapi-Desktop-Sniper/46d0835c-9fd3-4766-bee1-bdc4717dc22a/scratchpad")

conn = get_readonly_connection(str(DB))
conn.row_factory = sqlite3.Row
tape = load_tape(conn, SINCE)

# distinct-change series per mint: (times, mids) keeping only actual mid changes
changes = {}
for mint, (times, mids) in tape.items():
    ct, cm = [], []
    last = None
    for t, m in zip(times, mids):
        if m != last:
            ct.append(t); cm.append(m); last = m
    changes[mint] = (ct, cm)

# all buy times per mint (any wallet we track), for pile/predictability
raw_events = list(conn.execute(EVENTS_SQL, (SINCE,)))
buys_by_mint = defaultdict(list)   # (t, wallet)
for e in raw_events:
    t0 = _iso(e["received_at_normalized"] or e["received_at"])
    if t0:
        buys_by_mint[e["mint"]].append((t0.timestamp(), e["source_user_id"]))
for v in buys_by_mint.values():
    v.sort()

scored = []
by_wallet_hist = defaultdict(list)
for e in raw_events:
    got = measure(tape, e, (60,), 120)
    t0d = _iso(e["received_at_normalized"] or e["received_at"])
    key = (e["source_user_id"], e["source_handle"])
    r60 = got["responses"].get(60) if got else None
    # streak state BEFORE appending this event
    hist = by_wallet_hist[key]
    hot = len(hist) >= 3 and all(x > 0 for x in hist[-3:])
    if r60 is not None:
        by_wallet_hist[key].append(r60)
    if r60 is None or t0d is None:
        continue
    t0 = t0d.timestamp()
    ct, cm = changes.get(e["mint"], ([], []))
    lo = bisect_left(ct, t0 - 600); hi = bisect_left(ct, t0)
    window = cm[lo:hi]
    n_moves = max(0, hi - lo - 1)
    vol = None
    if len(window) >= 6:
        rets = [math.log(window[i+1] / window[i]) for i in range(len(window) - 1)
                if window[i] > 0]
        if len(rets) >= 5:
            vol = st.pstdev(rets) * 100
    # pile: distinct wallets buying this mint in [t0-120, t0]
    bt = buys_by_mint[e["mint"]]
    i0 = bisect_left(bt, (t0 - 120, "")); i1 = bisect_right(bt, (t0, "\xff"))
    pile = len({w for _, w in bt[i0:i1]})
    scored.append({"mint": e["mint"], "r": r60, "t": t0, "handle": e["source_handle"],
                   "mcap": e["their_market_cap"], "hot": hot,
                   "moves": n_moves, "vol": vol, "pile": pile})

print(f"scored events: {len(scored)}")

def cell(label, xs):
    if len(xs) < 8:
        print(f"  {label:<26} n={len(xs)} (too thin)"); return
    med, lo, hi = mint_clustered_median(xs)
    hit5 = sum(1 for s in xs if s["r"] >= 5) / len(xs)
    hit10 = sum(1 for s in xs if s["r"] >= 10) / len(xs)
    ci = f"[{lo:+.1f},{hi:+.1f}]" if lo is not None else "[-]"
    days = len({int(s['t']//86400) for s in xs})
    print(f"  {label:<26} n={len(xs):<5} mints={len({s['mint'] for s in xs}):<4} "
          f"med={med:+6.2f}% CI={ci} >=+5%:{hit5:5.1%} >=+10%:{hit10:5.1%} ev/day={len(xs)/max(1,days):.1f}")

sub1m = [s for s in scored if s["mcap"] and s["mcap"] < 1_000_000]

print("\nA. ACTIVITY (distinct mid changes, prior 10 min) — all scored events")
qs = sorted(s["moves"] for s in scored)
q1, q2, q3 = qs[len(qs)//4], qs[len(qs)//2], qs[3*len(qs)//4]
print(f"  quartile cuts: {q1}/{q2}/{q3} moves")
cell("dead (Q1)", [s for s in scored if s["moves"] <= q1])
cell("Q2", [s for s in scored if q1 < s["moves"] <= q2])
cell("Q3", [s for s in scored if q2 < s["moves"] <= q3])
cell("awake (Q4)", [s for s in scored if s["moves"] > q3])

print("\nB. VOLATILITY (stdev log-ret %, prior 10 min) — events with measurable vol")
withvol = [s for s in scored if s["vol"] is not None]
vs = sorted(s["vol"] for s in withvol)
v1, v2, v3 = vs[len(vs)//4], vs[len(vs)//2], vs[3*len(vs)//4]
print(f"  quartile cuts: {v1:.2f}/{v2:.2f}/{v3:.2f}")
cell("calm (Q1)", [s for s in withvol if s["vol"] <= v1])
cell("Q2", [s for s in withvol if v1 < s["vol"] <= v2])
cell("Q3", [s for s in withvol if v2 < s["vol"] <= v3])
cell("hot vol (Q4)", [s for s in withvol if s["vol"] > v3])

print("\nC. PILE-UP (distinct tracked buyers in trailing 120s incl. this one) — sub-1M band")
cell("solo (1)", [s for s in sub1m if s["pile"] == 1])
cell("pair (2)", [s for s in sub1m if s["pile"] == 2])
cell("pile (3+)", [s for s in sub1m if s["pile"] >= 3])

print("\nD. STACKS — sub-1M band")
awake = lambda s: s["moves"] > q2
cell("awake only", [s for s in sub1m if awake(s)])
cell("awake x hot-streak", [s for s in sub1m if awake(s) and s["hot"]])
cell("awake x pile2+", [s for s in sub1m if awake(s) and s["pile"] >= 2])
cell("awake x hot x pile2+", [s for s in sub1m if awake(s) and s["hot"] and s["pile"] >= 2])
cell("dead x hot-streak", [s for s in sub1m if not awake(s) and s["hot"]])

print("\nE. PREDICTABILITY — can we shortlist the next coin?")
# after the k-th distinct-wallet buy of a mint within 1h, does another tracked buy come within 10 min?
follow = {1: [0, 0], 2: [0, 0], 3: [0, 0]}
for mint, bt in buys_by_mint.items():
    for i, (t, w) in enumerate(bt):
        j0 = bisect_left(bt, (t - 3600, ""))
        k = len({ww for _, ww in bt[j0:i+1]})
        nxt = any(tt <= t + 600 and ww != w for tt, ww in bt[i+1:i+50])
        b = follow.get(min(k, 3))
        if b:
            b[0] += 1; b[1] += 1 if nxt else 0
for k, (n, y) in follow.items():
    lbl = f"{k}" if k < 3 else "3+"
    print(f"  after {lbl} distinct buyers in 1h: another tracked buy within 10 min: "
          f"{y}/{n} = {y/max(1,n):.1%}")

# F. CHARTS — event-anchored, so the shapes are visible
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

def montage(events, fname, title):
    events = events[:12]
    fig, axes = plt.subplots(3, 4, figsize=(20, 11))
    for ax, s in zip(axes.flat, events):
        ct, cm = changes.get(s["mint"], ([], []))
        lo = bisect_left(ct, s["t"] - 600); hi = bisect_left(ct, s["t"] + 600)
        xs = [(t - s["t"]) / 60 for t in ct[lo:hi]]; ys = cm[lo:hi]
        if not ys:
            continue
        base = ys[min(range(len(xs)), key=lambda i: abs(xs[i]))] or 1
        ax.plot(xs, [100 * (y / base - 1) for y in ys], lw=1.2)
        ax.axvline(0, color="red", lw=0.8)
        ax.axhline(0, color="gray", lw=0.5)
        ax.set_title(f"{(s['handle'] or '?')[:14]} r60={s['r']:+.1f}% mv={s['moves']} "
                     f"pile={s['pile']}", fontsize=9)
        ax.tick_params(labelsize=7)
    fig.suptitle(title, fontsize=13)
    fig.tight_layout()
    fig.savefig(OUT / fname, dpi=80)
    plt.close(fig)
    print(f"chart: {OUT / fname}")

best = sorted([s for s in sub1m if awake(s) and s["pile"] >= 2],
              key=lambda s: -s["t"])
dead = sorted([s for s in sub1m if not awake(s)], key=lambda s: -s["t"])
seen = set()
best_u = [s for s in best if not (s["mint"] in seen or seen.add(s["mint"]))]
seen = set()
dead_u = [s for s in dead if not (s["mint"] in seen or seen.add(s["mint"]))]
montage(best_u, "charts_awake_pile.png",
        "sub-1M, AWAKE tape, 2+ buyers in 120s — price vs minutes from signal (red line)")
montage(dead_u, "charts_dead.png",
        "sub-1M, DEAD tape (bottom-half activity) — price vs minutes from signal")
