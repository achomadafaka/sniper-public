#!/usr/bin/env python3
"""Source-entry chart study extractor (docs/audits/2026-10-21-source-entry-chart-preregistration.md).

Read-only. One slot-ordered pass over producer U's Pump-curve tape (data/desk/swaps/<day>.db) builds:
  SOURCE   the first buy of a mint by each frozen-roster wallet (FIRST-SOURCE = the earliest per mint)
  CONTROL  hash-sampled buys by non-registry actors with no registry buy on the mint in the prior 600 s
with the pre-registered features at t0 (swaps with slot < S only). Outcomes (MAX/DD/R at 5/15/60 min,
UP15, Y2, D20, censoring) are computed only with --outcomes; --exposure-only prints counts and writes
no outcome. Clock (pre-reg §1): block time where known (swaps.block_time, data/desk/blocks, exact
blocks_backfill rows); otherwise the nearest earlier known slot + 0.4 s per slot, flagged clock='slot'.
A first buy is "first on tape": a mint launched before capture can hide an earlier buy (launch_on_tape).

    python3 scripts/research/source_entry_extract.py --since 2026-10-21 --until 2026-10-21 --exposure-only
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import sqlite3
import sys
from collections import defaultdict, deque
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROSTER = REPO / "docs/audits/frozen/2026-10-21-source-entry-roster.tsv"
ROSTER_SHA = "634cd723152dbf35074dc9e0c116c492d67776eaf1932218d7d4794967e359ef"
SLOT_S = 0.4
HISTORY_S = 300                           # features look back 300 s
CONTROL_GUARD_S = 600                     # no registry buy in the prior 600 s
WINDOWS = {"5": 300, "15": 900, "60": 3600}
LABEL_S = 600                             # runner-study labels
PRICE_SCALE = 1e-3                        # (lamports / 1e9) / (token atomic / 1e6)
SALT = "gamma-srcentry-v1|"


def load_roster(path=ROSTER, sha=ROSTER_SHA):
    raw = Path(path).read_bytes()
    got = hashlib.sha256(raw).hexdigest()
    if got != sha:
        raise SystemExit("roster sha256 %s != frozen %s; refusing" % (got, sha))
    return {line.split("\t")[0] for line in raw.decode().splitlines() if line.strip()}


def keep_control(signature, k):
    return int(hashlib.sha256((SALT + signature).encode()).hexdigest()[:15], 16) % k == 0


def price(vq, vt):
    return vq / vt * PRICE_SCALE if vq and vt else None


def ln(a, b):
    return math.log(a / b) if a and b and a > 0 and b > 0 else None


class Clock:
    """slot -> seconds. Exact at a known block time; else the nearest earlier known slot + 0.4 s/slot
    (the nearest later one - 0.4 s/slot before the first anchor; slot x 0.4 with no anchor at all)."""

    def __init__(self, known):
        self.slots = sorted(known)
        self.times = [known[x] for x in self.slots]
        self.known = known

    def __call__(self, slot):
        if slot in self.known:
            return float(self.known[slot])
        i = bisect.bisect_right(self.slots, slot) - 1
        if i >= 0:
            return self.times[i] + SLOT_S * (slot - self.slots[i])
        if self.slots:
            return self.times[0] - SLOT_S * (self.slots[0] - slot)
        return slot * SLOT_S

    def exact(self, slot):
        return slot in self.known


def features(hist, S, t0, launch_t, prior_swaps, sol_usd):
    """hist: [(slot, t, p, is_buy, quote_sol, actor)] for this mint; only slot < S is used."""
    h = [(r[1], r[2], r[3], r[4], r[5]) for r in hist if r[0] < S]     # (t, p, is_buy, quote, actor)
    if not h:
        return {"n_hist": 0}

    def last_at(t_max):
        for r in reversed(h):
            if r[0] <= t_max:
                return r[1]
        return None

    def within(sec):
        return [r for r in h if r[0] > t0 - sec]

    c = h[-1][1]
    f = {"n_hist": len(h), "mcap_usd": c * 1e9 * sol_usd if sol_usd else None,
         "age_s": t0 - launch_t if launch_t is not None else None,
         "prior_swaps": prior_swaps if launch_t is not None else None}
    for k in (5, 15, 60):
        f["r%d" % k] = ln(c, last_at(t0 - k))
    w15, w60, w300 = within(15), within(60), within(300)
    b15 = sum(r[3] for r in w15 if r[2]); s15 = sum(r[3] for r in w15 if not r[2])
    f["flow_imb_15"] = (b15 - s15) / (b15 + s15) if b15 + s15 > 0 else None

    def maker_rate(lo_s, hi_s):
        secs = defaultdict(set)
        for r in h:
            age = t0 - r[0]
            if lo_s <= age < hi_s:
                secs[int(r[0])].add(r[4])
        return sum(len(v) for v in secs.values()) / (hi_s - lo_s)
    f["maker_rate_15"] = maker_rate(0, 15)
    f["maker_accel"] = f["maker_rate_15"] - maker_rate(15, 30)
    hi60 = max((r[1] for r in w60), default=None)
    f["pullback_depth"] = 1 - c / hi60 if hi60 else None
    run_hi, since = None, None
    for r in w60:
        if run_hi is None or r[1] > run_hi:
            run_hi, since = r[1], t0 - r[0]
    f["since_hh_s"] = since
    lows = []
    for b in range(12):                     # 5 s blocks, oldest first
        blk = [r[1] for r in w60 if 60 - 5 * b > t0 - r[0] >= 55 - 5 * b]
        lows.append(min(blk) if blk else None)
    f["hl_count_60"] = sum(1 for a, b in zip(lows, lows[1:]) if a is not None and b is not None and b > a)
    n10 = sum(1 for r in w60 if t0 - r[0] < 10)
    n_prior = len(w60) - n10
    f["dryup"] = (n10 / 10) / (n_prior / 50) if n_prior else None
    hi300 = max((r[1] for r in w300), default=None); lo300 = min((r[1] for r in w300), default=None)
    f["dist_hi_300"] = 1 - c / hi300 if hi300 else None
    f["dist_lo_300"] = c / lo300 - 1 if lo300 else None
    buys60 = [r for r in w60 if r[2]]
    f["buy_n_60"] = len(buys60)
    f["buy_usd_60"] = sum(r[3] for r in buys60) * sol_usd if sol_usd else None
    prior = sum(r[3] for r in w300 if r[2] and t0 - r[0] >= 60)
    f["buy_trend"] = (sum(r[3] for r in buys60) / 60) / (prior / 240) if prior else None
    return f


class Outcome:
    """Path of one unit from P0 over (t0, t0 + 60 min]; same-slot swaps with another signature count."""

    def __init__(self, S, t0, p0, sig):
        self.S, self.t0, self.p0, self.sig = S, t0, p0, sig
        self.mx = {w: None for w in WINDOWS}; self.mn = dict(self.mx); self.last = dict(self.mx)
        self.up15, self.y2, self.d20 = None, None, None
        self.grad_slot, self.done = None, False

    def see(self, slot, t, sig, p, graduated):
        if slot < self.S or (slot == self.S and sig == self.sig) or self.done:
            return
        dt = t - self.t0
        if dt > WINDOWS["60"]:
            self.done = True
            return
        if graduated and self.grad_slot is None:
            self.grad_slot = slot
        r = ln(p, self.p0)
        if r is None:
            return
        for w, n in WINDOWS.items():
            if dt <= n:
                self.mx[w] = r if self.mx[w] is None else max(self.mx[w], r)
                self.mn[w] = r if self.mn[w] is None else min(self.mn[w], r)
                self.last[w] = r
        if self.up15 is None and dt <= WINDOWS["15"]:
            if r >= math.log(1.5):
                self.up15 = 1
            elif r <= math.log(0.7):
                self.up15 = 0
        if dt <= LABEL_S:
            if self.y2 is None and r >= math.log(2):
                self.y2 = 1
            if self.d20 is None:
                if r >= math.log(1.2):
                    self.d20 = 0
                elif r <= math.log(0.8):
                    self.d20 = 1

    def labels(self, end_t, gap_ts):
        def censored(n):
            hi = self.t0 + n
            return end_t < hi or any(self.t0 < g <= hi for g in gap_ts)
        out = {}
        for w, n in WINDOWS.items():
            cen = censored(n)
            out.update({"max_%s" % w: None if cen else self.mx[w], "dd_%s" % w: None if cen else self.mn[w],
                        "r_%s" % w: None if cen else self.last[w]})
        undecided_at_grad = self.grad_slot is not None and self.up15 is None
        out["up15"] = (None if censored(WINDOWS["15"]) or undecided_at_grad else (self.up15 or 0))
        out["y2"] = None if censored(LABEL_S) else (self.y2 or 0)
        out["d20"] = None if censored(LABEL_S) else (self.d20 if self.d20 is not None else 0)
        out["graduated_slot"] = self.grad_slot
        return out


def day_files(root, sub, since, until):
    return [p for p in sorted((root / "data/desk" / sub).glob("*.db")) if since <= p.stem <= until]


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)


def sol_usd_from_emissions(root, day):
    """Producer U's own SOL/USD for a day: the median of mcap_usd / (curve price x 1e9) over its candidate
    emissions, price from the tape at (mint, band_entry_slot). The heartbeat's sol_usd is not logged
    historically, so this recovers the same number U used. None when the day has no usable emission."""
    path = root / "data/desk/candidate_emissions" / ("%s.jsonl" % day)
    tape = root / "data/desk/swaps" / ("%s.db" % day)
    if not path.exists() or not tape.exists():
        return None
    con, ratios = ro(tape), []
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
            mcap = e["snapshot"]["fields"]["mcap_usd"]["value"]
            row = con.execute("SELECT virtual_quote, virtual_token FROM swaps WHERE mint=? AND slot=? "
                              "ORDER BY ordinal DESC LIMIT 1", (e["mint"], e["band_entry_slot"])).fetchone()
        except (ValueError, KeyError, TypeError):
            continue
        p = price(*row) if row else None
        if p and mcap:
            ratios.append(mcap / (p * 1e9))
    con.close()
    ratios.sort()
    return ratios[len(ratios) // 2] if ratios else None


def registry_and_ours(ledger):
    con = ro(ledger)
    reg = {r[0] for r in con.execute("SELECT address FROM memecoin_address_registry WHERE network_id='solana'")}
    ours = {r[0] for r in con.execute("SELECT tx_sig FROM memecoin_fills WHERE network_id='solana' "
                                      "AND tx_sig IS NOT NULL")}
    con.close()
    return reg, ours


def load_clock(root, since, until):
    known = {}
    for sub, sql in (("blocks_backfill", "SELECT slot, block_time_lo FROM blocks_backfill WHERE exact=1"),
                     ("blocks", "SELECT slot, block_time FROM blocks WHERE block_time IS NOT NULL"),
                     ("swaps", "SELECT DISTINCT slot, block_time FROM swaps WHERE block_time IS NOT NULL")):
        for p in day_files(root, sub, "0000", until):
            con = ro(p)
            known.update({a: b for a, b in con.execute(sql)})
            con.close()
    return Clock(known)


def extract(root, since, until, *, roster, registry, our_sigs, sol_usd, k, outcomes):
    clock = load_clock(root, since, until)
    launches = {}
    for p in day_files(root, "launches", "0000", until):
        con = ro(p)
        launches.update({m: s for m, s in con.execute("SELECT mint, slot FROM launches")})
        con.close()
    gap_slots = []
    for p in day_files(root, "gaps", since, until):
        con = ro(p)
        # a worker_start row has last_slot NULL (no block seen yet): its host time `at` names the hole
        gap_slots += [clock(s) if s is not None else at for at, s in con.execute("SELECT at, last_slot FROM gaps")]
        con.close()
    hist = defaultdict(deque)
    prior_n = defaultdict(int)
    last_reg_buy = {}
    first_buy = set()
    first_source_mint = set()
    open_outcomes = defaultdict(list)
    units, end_t = [], None
    stats = defaultdict(int)
    for p in day_files(root, "swaps", since, until):
        day = p.stem
        su = sol_usd.get(day) or sol_usd_from_emissions(root, day)
        stats["sol_usd_" + day] = su
        con = ro(p)
        for slot, sig, mint, is_buy, qa, ta, actor, vq, vt, real_token in con.execute(
                "SELECT slot, signature, mint, is_buy, quote_atomic, token_atomic, actor, virtual_quote, "
                "virtual_token, real_token FROM swaps ORDER BY slot, signature, ordinal"):
            t = clock(slot)
            end_t = t
            stats["swaps"] += 1
            if sig in our_sigs:
                stats["ours_excluded"] += 1
                continue
            p_mark = price(vq, vt)
            if outcomes:
                for o in open_outcomes.get(mint, ()):
                    o[1].see(slot, t, sig, p_mark, real_token == 0)
            if is_buy and p_mark:
                unit = None
                if actor in roster and (actor, mint) not in first_buy:
                    first_buy.add((actor, mint))
                    unit = {"kind": "SOURCE", "first_source": mint not in first_source_mint}
                    first_source_mint.add(mint)
                elif (actor not in registry and keep_control(sig, k)
                      and not (mint in last_reg_buy and t - last_reg_buy[mint] <= CONTROL_GUARD_S)):
                    unit = {"kind": "CONTROL", "first_source": None}
                if unit is not None:
                    p0 = qa / ta * PRICE_SCALE if qa and ta else None
                    ls = launches.get(mint)
                    unit.update({"day": day, "mint": mint, "actor": actor, "signature": sig, "slot": slot,
                                 "t0": t, "clock": "block" if clock.exact(slot) else "slot",
                                 "p0_fill": p0, "p0_marginal": p_mark, "sol_usd": su,
                                 "features": features(hist[mint], slot, t, clock(ls) if ls is not None else None,
                                                      prior_n[mint], su)})
                    units.append(unit)
                    stats[unit["kind"]] += 1
                    if outcomes and p0:
                        open_outcomes[mint].append((unit, Outcome(slot, t, p0, sig)))
            if actor in registry and is_buy:
                last_reg_buy[mint] = t
            if p_mark:
                q = hist[mint]
                q.append((slot, t, p_mark, bool(is_buy), (qa or 0) / 1e9, actor))
                while q and q[0][1] < t - HISTORY_S:
                    q.popleft()
            prior_n[mint] += 1
        con.close()
    registry_mints = set(last_reg_buy)
    for u in units:
        u["launch_on_tape"] = u["mint"] in launches
        if u["kind"] == "CONTROL":
            u["strict_ok"] = u["mint"] not in registry_mints     # no registry buy at any time on the tape
    if outcomes:
        for mint, lst in open_outcomes.items():
            for unit, o in lst:
                unit["outcome"] = o.labels(end_t or 0, gap_slots)
    stats["first_source_mints"] = len(first_source_mint)
    stats["source_wallets"] = len({u["actor"] for u in units if u["kind"] == "SOURCE"})
    stats["launch_known_share_source"] = round(
        sum(1 for u in units if u["kind"] == "SOURCE" and u["features"].get("age_s") is not None)
        / max(1, stats["SOURCE"]), 3)
    stats["gap_rows"] = len(gap_slots)
    stats["clock_exact_share"] = round(sum(1 for u in units if u["clock"] == "block") / max(1, len(units)), 3)
    return units, dict(stats)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(REPO))
    ap.add_argument("--ledger", default=None, help="sniper_tracker.db (read-only); default <root>/sniper_tracker.db")
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--k", type=int, default=50, help="control hash rate 1/k (frozen on H1 day 1)")
    ap.add_argument("--out", default=None, help="units jsonl (gitignored location)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--exposure-only", action="store_true")
    mode.add_argument("--outcomes", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.root)
    ledger = Path(a.ledger or root / "sniper_tracker.db")
    roster = load_roster()
    registry, ours = registry_and_ours(ledger)
    sol = {}          # U's own price per day (sol_usd_from_emissions); pass a dict here only in tests
    units, stats = extract(root, a.since, a.until, roster=roster, registry=registry | roster, our_sigs=ours,
                           sol_usd=sol, k=a.k, outcomes=a.outcomes)
    if a.out:
        with open(a.out, "w") as fh:
            for u in units:
                fh.write(json.dumps(u, sort_keys=True) + "\n")
    print(json.dumps(stats, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
