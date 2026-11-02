#!/usr/bin/env python3
"""What happened to the coins the funded market-cap ceiling refused, against the coins it admitted.

THE QUESTION (Alpha, 2026-09-22): the ceiling idles 15 of the 26 armed wallets that closed nothing in
14 days, so is it leaving money on the table? For every funded attempt refused with `mcap $X over
ceiling $Y`, this measures the coin's price change at 5, 30 and 60 minutes after the decision, and
compares it with the coins we admitted, banded by market cap.

THE ANSWER IT PRODUCES CANNOT CARRY A KNOB CHANGE, and the reason is in the measurement rather than
the sample size. An ADMITTED row's t0 is `opened_at`, which is AFTER our own buy moved the price. A
REFUSED row's t0 is a mid nobody traded against. So the refused columns measure drift from an
unimpacted price and the admitted columns measure decay from a price we paid up for, and the bias runs
in exactly the direction that would make relaxing the ceiling look good. Anyone rerunning this must
carry that caveat with the numbers. See docs/audits/2026-09-22-mcap-ceiling-refusal-outcomes.md.

BANDS, NOT "JUST UNDER THE CEILING", because the ceiling MOVED inside the window: the refusals' own
recorded ceilings are $250k, $500k, $1M and $6M. One current value applied retroactively would
misclassify the early rows, so the band gradient is reported and `$125k-250k` is the just-under row.
The ceiling is parsed from each refusal's own reason string, which is the value that actually applied
at decision time -- never re-read from config.

TWO DEFECTS THAT MADE THE FIRST RUN OF THIS STUDY WRONG. It reported means of +1.7e20%.

  1. The price reader fell back from `memecoin_position_marks` to `memecoin_quote_ledger` INSIDE one
     path, so a ratio could have its two ends in different instruments. `path()` now picks one reader
     and uses it for the base and every leg.
  2. The quote read matched on `mint` ALONE with no chain filter, so a mint present on more than one
     chain collided. `memecoin_quote_ledger` carries a `(network_id, mint, ts)` index that went
     unused. Both readers are now chain-filtered.

The two tables were checked before being blamed: on matched minutes they agree within 10% on 99.3% of
pairs, so cross-table SCALE was never the problem -- the missing chain filter was. After the fix there
were 0 extreme ratios in 1,500 sampled refusals, against 28 across the whole corrected run, and those
are almost all one chain-56 coin whose own series changes scale.

THREE NAMES FOR ONE CONCEPT, in one query path: `memecoin_position_marks` keys on `chain_id`, while
`memecoin_quote_ledger`, `memecoin_entry_attempts` and `memecoin_positions` key on `network_id` (and
`memecoin_wallet_approvals` uses `chain`). Canonicalize before comparing; intersecting a canonical id
set with the NAMES from `funded_chain_keys()` is how a sibling audit put 12 armed wallets in "armed
nowhere".

`.env` IS LOADED ON PURPOSE. A settings read from a plain script silently returns the code default --
`MEMECOIN_EVM_ALLOWLIST` defaults to `8453,4663` against a live `8453,4663,56,5042`, which is NARROWER
than production and raises no error. See
[[finding-a-config-read-without-dotenv-is-the-code-default-2026-09-22]].

LOAD: read-only, and every price lookup is an indexed point-range read per attempt
(`(chain_id, mint, sweep_ts)` and `(network_id, mint, ts)`). No correlated subquery touches
`memecoin_position_marks` or `memecoin_quote_ledger`; both are multi-million-row tapes.

  python3 scripts/research/mcap_ceiling_refusal_outcomes.py            # 30-day study, prints tables
  python3 scripts/research/mcap_ceiling_refusal_outcomes.py --days 14
  python3 scripts/research/mcap_ceiling_refusal_outcomes.py --selftest # no DB, checks the pure logic
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

HORIZONS = ((5, 5), (30, 10), (60, 15))          # (minutes after t0, tolerance minutes)
BANDS = ((0, 125_000, "<$125k"), (125_000, 250_000, "$125k-250k"),
         (250_000, 500_000, "$250k-500k"), (500_000, 1_000_000, "$500k-1M"),
         (1_000_000, float("inf"), ">$1M"))
BAND_NAMES = [b[2] for b in BANDS]
CHAINS = ("solana", "4663", "56", "5042", "8453")
MIN_CELL = 20                                    # a median under this many samples is not reported
RATIO_LO, RATIO_HI = 0.001, 1000.0               # outside this is an instrument change, not a move
CEILING_RE = re.compile(r"mcap \$([\d,]+) over ceiling \$([\d,]+)")


def band_of(mcap):
    """The coin's own market-cap band, or None when the market cap was never recorded."""
    if mcap is None:
        return None
    for lo, hi, name in BANDS:
        if lo <= mcap < hi:
            return name
    return None


def ratio_usable(ratio):
    """A price ratio we are willing to call a return. Excluded ones are COUNTED, never dropped."""
    return RATIO_LO < ratio < RATIO_HI


def _parse(value):
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


class Prices:
    """Both readers are chain-filtered, and a path never mixes them."""

    def __init__(self, conn):
        self.conn = conn

    def mark_at(self, chain, mint, lo, hi, *, last=False):
        order = "DESC" if last else "ASC"
        row = self.conn.execute(
            "SELECT sweep_ts AS ts, mid AS px, source FROM memecoin_position_marks "
            "WHERE chain_id=? AND mint=? AND sweep_ts>=? AND sweep_ts<=? AND mid>0 "
            f"ORDER BY sweep_ts {order} LIMIT 1",
            (chain, mint, lo.isoformat(), hi.isoformat())).fetchone()
        return (_parse(row["ts"]), float(row["px"]), "mark:" + str(row["source"] or "?")) if row else None

    def quote_at(self, chain, mint, lo, hi, *, last=False):
        order = "DESC" if last else "ASC"
        row = self.conn.execute(
            "SELECT ts, price AS px, source FROM memecoin_quote_ledger "
            "WHERE network_id=? AND mint=? AND ts>=? AND ts<=? AND price>0 "
            f"ORDER BY ts {order} LIMIT 1",
            (chain, mint, lo.isoformat(), hi.isoformat())).fetchone()
        return (_parse(row["ts"]), float(row["px"]), "quote:" + str(row["source"] or "?")) if row else None

    def path(self, chain, mint, t0):
        """One instrument for the base and every leg. Mixing them is defect 1 in the docstring."""
        lo, hi = t0 - dt.timedelta(minutes=5), t0 + dt.timedelta(minutes=1)
        for reader in (self.mark_at, self.quote_at):
            base = reader(chain, mint, lo, hi, last=True)
            if not base:
                continue
            legs = {}
            for mins, tol in HORIZONS:
                target = t0 + dt.timedelta(minutes=mins)
                legs[mins] = reader(chain, mint, target, target + dt.timedelta(minutes=tol))
            if any(legs.values()):
                return {"base": base, "legs": legs}
        return {"base": None, "legs": {}}


def run(days):
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
    from sniper.util.state_paths import state_path
    from sniper.util.clock import real_utc_now

    db = os.getenv("DB_PATH") or str(state_path("sniper_tracker.db"))
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    now = dt.datetime.fromtimestamp(real_utc_now(), dt.timezone.utc)
    cut = (now - dt.timedelta(days=days)).isoformat()
    prices = Prices(conn)
    cells = defaultdict(lambda: defaultdict(list))
    counts = defaultdict(lambda: defaultdict(int))
    ceilings, sources, extremes = set(), defaultdict(int), []

    def record(group, chain, mcap, t0, mint):
        band = band_of(mcap)
        key = (group, chain, band)
        counts[key]["rows"] += 1
        if band is None:
            counts[key]["no_mcap"] += 1
            return
        if t0 is None:
            counts[key]["no_t0"] += 1
            return
        path = prices.path(chain, mint, t0)
        if not path["base"]:
            counts[key]["no_base_price"] += 1
            return
        counts[key]["priced"] += 1
        sources[path["base"][2]] += 1
        for mins, _ in HORIZONS:
            leg = path["legs"].get(mins)
            if not leg:
                counts[key]["censored_%d" % mins] += 1
                continue
            ratio = leg[1] / path["base"][1]
            if not ratio_usable(ratio):
                counts[key]["excluded_ratio_%d" % mins] += 1
                extremes.append((ratio, chain, mint, mins))
                continue
            cells[key][mins].append(ratio - 1.0)
            sources[leg[2]] += 1

    refused = conn.execute(
        "SELECT network_id, mint, decision_started_at, source_market_cap, terminal_reason "
        "FROM memecoin_entry_attempts WHERE decision_started_at>=? AND terminal_status='refused' "
        "AND terminal_reason LIKE 'mcap%over ceiling%'", (cut,)).fetchall()
    admitted = conn.execute(
        "SELECT network_id, mint, opened_at, entry_mcap FROM memecoin_positions "
        "WHERE execution_mode='real' AND opened_at>=?", (cut,)).fetchall()
    for row in refused:
        found = CEILING_RE.match(str(row["terminal_reason"] or ""))
        if found:
            ceilings.add(int(found.group(2).replace(",", "")))
        record("refused", str(row["network_id"]), row["source_market_cap"],
               _parse(row["decision_started_at"]), str(row["mint"]))
    for row in admitted:
        record("admitted", str(row["network_id"]), row["entry_mcap"],
               _parse(row["opened_at"]), str(row["mint"]))
    conn.close()

    def pooled(group, band, mins):
        out = []
        for chain in CHAINS:
            out.extend(cells.get((group, chain, band), {}).get(mins, []))
        return sorted(out)

    print("window: %s -> %s  (%d days, corrected clock)" % (cut, now.isoformat(), days))
    print("refused=%d  admitted=%d  ceilings recorded in the window=%s"
          % (len(refused), len(admitted), sorted(ceilings)))
    print("\nCAUTION: the refused side is measured from an unimpacted mid and the admitted side from")
    print("our own paid-up entry, so this comparison FLATTERS relaxing the ceiling. See the docstring.")
    for mins, _ in HORIZONS:
        print("\n=== +%d min === median / p90 / mean / share >=+20%%  (cells under n=%d suppressed)"
              % (mins, MIN_CELL))
        print("  %-9s %-11s %8s %9s %9s %9s %8s" % ("group", "band", "n", "median", "p90", "mean", ">=+20%"))
        for group in ("refused", "admitted"):
            for band in BAND_NAMES:
                vals = pooled(group, band, mins)
                if len(vals) < MIN_CELL:
                    continue
                p90 = vals[min(len(vals) - 1, int(0.9 * len(vals)))]
                print("  %-9s %-11s %8d %+8.2f%% %+8.1f%% %+8.1f%% %7.1f%%"
                      % (group, band, len(vals), 100 * st.median(vals), 100 * p90,
                         100 * sum(vals) / len(vals),
                         100.0 * sum(1 for v in vals if v >= 0.20) / len(vals)))
    totals = defaultdict(int)
    for cell in counts.values():
        for field, n in cell.items():
            totals[field] += n
    print("\ncensoring and exclusions:", dict(sorted(totals.items())))
    print("price sources:", dict(sorted(sources.items(), key=lambda kv: -kv[1])))
    if extremes:
        extremes.sort(reverse=True)
        print("excluded ratios: %d, worst %s" % (len(extremes), extremes[:3]))
    return 0


def selftest():
    """Pure logic only, no database. The two things that made the first run wrong live here."""
    assert band_of(None) is None, "an unrecorded market cap must not land in a band"
    assert band_of(0) == "<$125k"
    assert band_of(124_999) == "<$125k"
    assert band_of(125_000) == "$125k-250k", "band edges are lower-inclusive"
    assert band_of(250_000) == "$250k-500k", "a coin AT the base ceiling is above it, not under"
    assert band_of(999_999) == "$500k-1M"
    assert band_of(1_000_000) == ">$1M"
    assert band_of(10 ** 12) == ">$1M", "the top band must be unbounded"
    assert ratio_usable(1.0) and ratio_usable(2.5) and ratio_usable(0.01)
    assert not ratio_usable(1.7e20), "the defect that produced +1.7e20% must be excluded"
    assert not ratio_usable(0.0), "a zero price is not a 100% loss, it is a bad read"
    assert not ratio_usable(RATIO_HI) and not ratio_usable(RATIO_LO), "bounds are exclusive"
    assert CEILING_RE.match("mcap $20,153,474 over ceiling $250,000 (over_ceiling)").group(2) == "250,000"
    assert CEILING_RE.match("mcap $1,309,732 over ceiling $500,000").group(1) == "1,309,732"
    assert CEILING_RE.match("some other refusal") is None
    print("selftest ok")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    sys.exit(selftest() if args.selftest else run(args.days))
