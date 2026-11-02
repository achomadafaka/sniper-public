#!/usr/bin/env python3
"""Refusal families and what the refused coins did afterwards, per chain, over a window.

Operator question (2026-09-23): does the quiet-hours block stay, and do other gates need
readjusting? This is the FIRST read: every refusal family over the last N days, how many coins it
turned away, and what those coins' mids did at +5, +15 and +60 minutes and at their peak inside the
hour, next to the admitted side's realized outcomes on the same chain and window.

READ THE CAVEATS BEFORE THE NUMBERS.
  * Refused outcomes are DexScreener/GeckoTerminal mids from `memecoin_quote_ledger`, which read
    high on a dumping coin and lag ~25 s; they are the OPTIMISTIC end. Admitted outcomes are real
    fills and real exits. The comparison flatters the refused side by construction (Lambda,
    docs/audits/2026-09-22-mcap-ceiling-refusal-outcomes.md), so a family that looks good here
    earns a chain-truth replay, not a knob change.
  * A coin is counted ONCE per family per chain (first refusal), so repeated signals on one coin do
    not multiply it. "$100 each" is arithmetic on the count.
  * Windows are built in Python with a 'T' separator. SQLite's datetime('now') renders a SPACE and
    every 'T' stamp sorts after it, which silently turns a window into a whole day.
  * Every join filters by network_id: mints collide across chains.
  * `not_approved`, `disarmed` and `unroutable_chain` are governance, not gates; they are listed but
    not priced.
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import datetime as dt
import re
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOVERNANCE = ("not_approved", "disarmed", "unroutable_chain", "excluded", "filled")

FAMILIES = [
    (r"^quiet_hours", "quiet_hours"),
    (r"^mcap \$[\d,]+ over ceiling", "mcap over ceiling"),
    (r"^mcap .*under floor", "mcap under floor"),
    (r"^source buy .*under absolute floor", "source buy under absolute floor"),
    (r"^source buy \$\d+( \+ .*)?$", "source buy under ratio floor"),
    (r"^source buy .*under unknown-mcap floor", "source buy under ratio floor"),
    (r"^our quote is \+", "chase: quote above source price"),
    (r"^coin ran \d", "chase: coin already ran"),
    (r"^re-entry cooldown", "re-entry cooldown"),
    (r"^one position per coin", "one position per coin"),
    (r"^below min-trade floor", "below min-trade floor"),
    (r"^no quote", "no quote"),
    (r"^route build refused", "route build refused"),
    (r"^exact quote is \+\d+% above source", "chase: quote above source price"),
    (r"^coin already ran", "chase: coin already ran"),
    (r"^entry older than", "entry age"),
    (r"^coin_too_young", "coin too young"),
    (r"^m5 organic volume .*under floor|^m5 volume .*under floor", "m5 volume under floor"),
    (r"^m5 (organic )?volume .*OVER ceiling", "m5 volume over ceiling"),
    (r"^h1 organic volume .*under floor", "h1 volume under floor"),
    (r"^m5 trades", "m5 trade count"),
    (r"^round trip", "round trip over cap"),
    (r"^price impact", "price impact over cap"),
    (r"^route quote is", "route quote vs chart mid"),
    (r"^drawdown pause", "drawdown pause"),
    (r"^kill_switch", "kill switch"),
    (r"^exit route pays", "unsellable quote asset"),
    (r"^no route|^no_route", "no route"),
    (r"^entry_simulation", "preflight simulation"),
    (r"^reverted", "reverted"),
]


def family(reason: str) -> str:
    r = (reason or "").strip()
    for g in GOVERNANCE:
        if r.startswith(g):
            return g
    for pat, name in FAMILIES:
        if re.match(pat, r):
            return name
    return "other: " + re.sub(r"[\d$,.%]+", "N", r.split(":")[0].split(" (")[0])[:40]


def iso(t: dt.datetime) -> str:
    return t.isoformat()


# THE LOOKUP NEEDS AN AGE FLOOR, OR SILENCE BECOMES A MEASURED ZERO (Codex review R6,
# 2026-09-23). The original `ts<=?` lookup had no lower bound, so a coin that stopped being quoted
# returned THE SAME row for the baseline and for every horizon, and `p/base - 1` printed 0.0% at
# 5, 15 and 60 minutes while the row counted as priced. A zero return requires TWO distinct
# observations; one observation reused three times is no evidence at all.
#
# 180 s is measured, not chosen: over 3 days and 549 coins the consecutive-gap distribution in
# `memecoin_quote_ledger` is p50 91 s, p90 124 s, p95 127 s, p99 183 s, and only 0.4% of gaps
# exceed 5 minutes. So 180 s admits the normal cadence and censors a genuinely stale series.
#
# The bounded-window shape here is the one `mcap_ceiling_refusal_outcomes.py:111-123` already
# uses; this script was the only one missing it.
MAX_OBS_AGE_S = 180


def mid_before(c, net, mint, t, *, max_age_s=MAX_OBS_AGE_S):
    """(price, ts) for the newest observation at or before `t` and no older than `max_age_s`.

    Returns (None, None) when the nearest observation is outside tolerance, which the caller must
    treat as CENSORED rather than as a flat price.
    """
    lo = iso(dt.datetime.fromisoformat(t) - dt.timedelta(seconds=max_age_s))
    row = c.execute("SELECT price, ts FROM memecoin_quote_ledger "
                    "WHERE network_id=? AND mint=? AND ts<=? AND ts>=? AND price>0 "
                    "ORDER BY ts DESC LIMIT 1", (net, mint, t, lo)).fetchone()
    return (row[0], row[1]) if row and row[0] else (None, None)


def outcomes(c, net, mint, t0: dt.datetime):
    base, base_ts = mid_before(c, net, mint, iso(t0))
    if not base:
        return None
    out = {}
    for m in (5, 15, 60):
        p, p_ts = mid_before(c, net, mint, iso(t0 + dt.timedelta(minutes=m)))
        # A horizon that resolves to the BASELINE observation carries no new evidence, so it is
        # censored rather than reported as a 0% return.
        out[m] = (p / base - 1) * 100 if (p and p_ts != base_ts) else None
    pk = c.execute("SELECT MAX(price) FROM memecoin_quote_ledger WHERE network_id=? AND mint=? AND ts>? AND ts<=?",
                   (net, mint, iso(t0), iso(t0 + dt.timedelta(minutes=60)))).fetchone()[0]
    out["peak"] = (pk / base - 1) * 100 if pk else None
    # A mid that moves more than 100x either way inside an hour is a bad read (a wrong pair, a
    # decimals slip, a scale change in the series), not a return. Excluded and COUNTED.
    if any(v is not None and (v > 9900 or v < -99) for v in out.values()):
        return {"bad_read": True}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--db", default=str(ROOT / "sniper_tracker.db"))
    ap.add_argument("--min-n", type=int, default=3)
    args = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    since = iso(now - dt.timedelta(days=args.days))
    c = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT network_id, mint, ticker, decision_started_at, terminal_reason, terminal_status "
                     "FROM memecoin_entry_attempts WHERE decision_started_at>=? ORDER BY decision_started_at",
                     (since,)).fetchall()
    first = {}
    counts = defaultdict(int)
    for r in rows:
        fam = family(r["terminal_reason"] or r["terminal_status"] or "")
        key = (fam, r["network_id"])
        counts[key] += 1
        ck = (fam, r["network_id"], r["mint"])
        if ck not in first:
            first[ck] = r
    print(f"refusal families, last {args.days:g} days (since {since[:19]}Z); refusals -> distinct coins -> priced from a mid")
    print("CAVEAT: refused outcomes are optimistic mids; admitted outcomes are real fills. Not the same instrument.")
    table = defaultdict(list)
    for (fam, net, mint), r in first.items():
        if fam in GOVERNANCE:
            continue
        t0 = dt.datetime.fromisoformat(r["decision_started_at"])
        o = outcomes(c, net, mint, t0)
        table[(fam, net)].append(o)
    # `based` is coins with a BASELINE observation; `n@60` is coins actually observed again at
    # the 60-minute horizon. They are different numbers, and an aggregate over an empty `n@60` is
    # unknown, not zero, so those columns print "-" (Codex review R6).
    print(f"\n{'family':34} {'chain':7} {'refusals':>8} {'coins':>6} {'based':>6} {'n@60':>5} {'med@5':>7} {'med@15':>7} {'med@60':>7} {'mean@60':>8} {'>=+20%pk':>9} {'<=-20%@60':>9} {'$100 sum@60':>11}")
    for (fam, net), lst in sorted(table.items(), key=lambda kv: -counts[kv[0]]):
        bad = sum(1 for o in lst if o and o.get("bad_read"))
        priced = [o for o in lst if o and not o.get("bad_read")]
        if counts[(fam, net)] < args.min_n:
            continue
        def med(m):
            v = [o[m] for o in priced if o.get(m) is not None]
            return f"{statistics.median(v):+.1f}" if v else "-"
        at60 = [o[60] for o in priced if o.get(60) is not None]
        pk = [o["peak"] for o in priced if o.get("peak") is not None]
        mean60 = f"{statistics.fmean(at60):+.1f}" if at60 else "-"
        sum60 = f"{sum(at60):+.0f}" if at60 else "-"
        # The peak count carries its own denominator: 0 of 30 observed and 0 of 0 observed are
        # different facts and must not print the same.
        peak_hits = f"{sum(1 for p in pk if p >= 20)}/{len(pk)}" if pk else "-"
        down60 = f"{sum(1 for a in at60 if a <= -20)}" if at60 else "-"
        print(f"{fam:34} {net:7} {counts[(fam, net)]:>8} {len(lst):>6} {len(priced):>6} {len(at60):>5} "
              f"{med(5):>7} {med(15):>7} {med(60):>7} {mean60:>8} {peak_hits:>9} {down60:>9} "
              f"{sum60:>11}" + (f"  bad reads {bad}" if bad else ""))
    gov = [(k, v) for k, v in counts.items() if k[0] in GOVERNANCE]
    print("\ngovernance (not priced): " + ", ".join(f"{k[0]}/{k[1]} {v}" for k, v in sorted(gov, key=lambda kv: -kv[1])))
    print("\nadmitted side, same window (real fills, realized):")
    for r in c.execute("SELECT network_id, COUNT(*) n, SUM(realized_pnl>0) w, ROUND(SUM(realized_pnl),2) pnl, "
                       "ROUND(AVG(realized_pnl),2) avg FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 "
                       "AND opened_at>=? GROUP BY 1 ORDER BY 2 DESC", (since,)):
        print(f"  {r['network_id']:7} closes {r['n']:>4}  wins {r['w']:>4} ({100*r['w']/r['n']:.0f}%)  pnl ${r['pnl']:+.2f}  avg ${r['avg']:+.2f}")
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    raise SystemExit(main())
