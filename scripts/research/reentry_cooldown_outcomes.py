#!/usr/bin/env python3
"""Is the re-entry cooldown costing money? Refused-coin counterfactual, split by why it refused.

Alpha's task 2026-09-28, built on Alpha's first pass (scratchpad gate_outcomes.py): TP 25% / stop 35%
applied to the forward exact-route capture of every Solana `reentry_cooldown` refusal. Read-only:
data/memecoin_refusal_captures.db and the ledger, no network.

The gate (sniper/services/memecoin_risk.py, `re-entry cooldown:`) refuses ANY source buying a coin
whose most recent REAL close is inside MEMECOIN_REAL_REENTRY_COOLDOWN_S (86400). With
MEMECOIN_REENTRY_WINNER_UNBLOCKED=0 (.env, operator 2026-09-16) winners are cooled too. The
losing-re-entry block runs right AFTER it with the same 24 h window (MEMECOIN_REAL_LOSING_REENTRY_HOURS
=24), so a refusal whose last close LOST would be refused anyway: only win/zero last closes are the
cooldown's own marginal refusals.

Per capture it adds: the last real close on the coin before the refusal (win / loss / zero, same or
other source), whether we held the coin then, whether we opened a real position on the coin inside
the capture's horizon (the counterfactual would double-count it), overlapping captures of one coin
(one path counted twice), and the capture buy price against the source's price, beside our own
fills' price against the source's.

    python3 scripts/research/reentry_cooldown_outcomes.py [--since 2026-09-14]
    python3 scripts/research/reentry_cooldown_outcomes.py --selftest
"""
import argparse
import collections
import contextlib
import random
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CAPS = f"file:{REPO / 'data' / 'memecoin_refusal_captures.db'}?mode=ro"
LEDGER = f"file:{REPO / 'sniper_tracker.db'}?mode=ro"
TP, SL = 0.25, -0.35


def ts(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def path_outcome(sells, clip):
    """(policy return, last return) from [(age_s, usdc_out)] sell quotes of the bought bag: TP or
    stop at the first quote that crosses it, else the last quote inside the horizon."""
    path = [(age, out / clip - 1) for age, out in sorted(sells)]
    for _, ret in path:
        if ret >= TP:
            return TP, path[-1][1]
        if ret <= SL:
            return SL, path[-1][1]
    return path[-1][1], path[-1][1]


def dedupe(caps):
    """Mark a capture `dup` when an earlier kept capture of the same mint is still inside its
    horizon at this one's refusal: both would describe one price path."""
    last_kept_end = {}
    for c in sorted(caps, key=lambda c: c["refused"]):
        end = last_kept_end.get(c["mint"])
        c["dup"] = end is not None and c["refused"] < end
        if not c["dup"]:
            last_kept_end[c["mint"]] = c["refused"] + timedelta(seconds=c["horizon_s"])
    return caps


def last_close_class(pnl):
    return "none" if pnl is None else "win" if pnl > 0 else "loss" if pnl < 0 else "zero"


def load(since):
    with contextlib.closing(sqlite3.connect(CAPS, uri=True, timeout=10)) as c:
        rows = c.execute(
            "SELECT capture_id, origin_event_id, mint, refused_at, age_s, horizon_s, quote_side, "
            "quote_price, quote_input_usd, quote_output_atomic FROM memecoin_refusal_captures "
            "WHERE gate='reentry_cooldown' AND network_id='solana' AND quality='ok' AND refused_at>=? "
            "ORDER BY capture_id, age_s", (since,)).fetchall()
    series = collections.defaultdict(list)
    for r in rows:
        series[r[0]].append(r)
    caps = []
    for cid, rs in series.items():
        buy = [r for r in rs if r[6] == "buy"]
        sells = [(r[4], int(r[9]) / 1e6) for r in rs if r[6] == "sell" and r[9]]
        if not buy or not sells or not buy[0][8]:
            continue
        pol, last = path_outcome(sells, buy[0][8])
        caps.append({"capture_id": cid, "origin": rs[0][1], "mint": rs[0][2], "refused": ts(rs[0][3]),
                     "horizon_s": rs[0][5], "buy_age_s": buy[0][4], "buy_price": buy[0][7],
                     "clip": buy[0][8], "policy": pol, "last": last})
    return dedupe(caps), len(series)


def enrich(caps):
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=10)) as c:
        c.row_factory = sqlite3.Row
        for cap in caps:
            att = c.execute(
                "SELECT source_user_id, source_handle, source_price, quote_price, decision_started_at "
                "FROM memecoin_entry_attempts WHERE origin_event_id=? AND network_id='solana' AND mint=? "
                "AND terminal_reason LIKE 're-entry cooldown%'", (cap["origin"], cap["mint"])).fetchall()
            att = min(att, key=lambda a: abs((ts(a["decision_started_at"]) - cap["refused"]).total_seconds()),
                      default=None)
            cap["source"] = att["source_user_id"] if att else None
            cap["handle"] = att["source_handle"] if att else None
            cap["source_price"] = att["source_price"] if att else None
            cap["attempt_quote_price"] = att["quote_price"] if att else None
            pos = c.execute(
                "SELECT id, source_user_id, opened_at, closed_at, is_open, realized_pnl FROM memecoin_positions "
                "WHERE execution_mode='real' AND network_id='solana' AND mint=?", (cap["mint"],)).fetchall()
            closed = [p for p in pos if p["closed_at"] and ts(p["closed_at"]) <= cap["refused"]]
            lc = max(closed, key=lambda p: p["closed_at"], default=None)
            cap["last_close"] = last_close_class(lc["realized_pnl"]) if lc else "no_close"
            cap["last_close_age_s"] = (cap["refused"] - ts(lc["closed_at"])).total_seconds() if lc else None
            cap["same_source"] = bool(lc and lc["source_user_id"] == cap["source"])
            cap["held"] = any(ts(p["opened_at"]) <= cap["refused"]
                              and (p["is_open"] or ts(p["closed_at"]) > cap["refused"]) for p in pos)
            end = cap["refused"] + timedelta(seconds=cap["horizon_s"])
            cap["opened_in_horizon"] = [p["id"] for p in pos if cap["refused"] < ts(p["opened_at"]) <= end]
    return caps


def our_fills(since):
    """Realized return and fill-vs-source premium of our real Solana entries since `since`."""
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=10)) as c:
        pnl = [r[0] / r[1] for r in c.execute(
            "SELECT realized_pnl, entry_notional FROM memecoin_positions WHERE execution_mode='real' "
            "AND network_id='solana' AND is_open=0 AND opened_at>=? AND entry_notional>0 "
            "AND realized_pnl IS NOT NULL", (since,))]
        prem = [r[0] / r[1] - 1 for r in c.execute(
            "SELECT fill_price, source_price FROM memecoin_entry_attempts WHERE network_id='solana' "
            "AND decision_started_at>=? AND position_id IS NOT NULL AND fill_price>0 AND source_price>0",
            (since,))]
    return pnl, prem


def ci(xs, seed=1):
    rnd = random.Random(seed)
    b = sorted(statistics.mean(rnd.choices(xs, k=len(xs))) for _ in range(2000))
    return b[50], b[1949]


def line(label, caps):
    if not caps:
        return f"{label:44s} n=0"
    p = [c["policy"] * 100 for c in caps]
    lo, hi = ci(p) if len(p) > 1 else (p[0], p[0])
    return (f"{label:44s} n={len(p):3d} policy mean {statistics.mean(p):+6.1f}% [{lo:+6.1f},{hi:+6.1f}]"
            f" tp_first {sum(c['policy'] == TP for c in caps):3d} stop_first {sum(c['policy'] == SL for c in caps):3d}"
            f" last p50 {statistics.median(c['last'] * 100 for c in caps):+6.1f}%")


def q(xs, p):
    v = sorted(x for x in xs if x is not None)
    return v[min(len(v) - 1, int(p * (len(v) - 1) + .5))] if v else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-14")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    caps, n_series = load(a.since)
    caps = enrich(caps)
    kept = [c for c in caps if not c["dup"]]
    print(f"capture series {n_series}, priced {len(caps)}, overlapping duplicates {len(caps) - len(kept)}, "
          f"distinct mints {len({c['mint'] for c in caps})}; clip {sorted({c['clip'] for c in caps})}")
    print(line("ALL priced captures", caps))
    print(line("deduped (one path per coin window)", kept))
    for key, vals in (("last_close", ("win", "zero", "none", "loss", "no_close")),
                      ("held", (True, False)), ("same_source", (True, False))):
        for v in vals:
            print(line(f"deduped {key}={v}", [c for c in kept if c[key] == v]))
    marginal = [c for c in kept if c["last_close"] in ("win", "zero", "none")]
    print(line("deduped MARGINAL (losing-reentry would pass)", marginal))
    for lc in ("win", "loss"):
        for same in (True, False):
            print(line(f"deduped last_close={lc} same_source={same}",
                       [c for c in kept if c["last_close"] == lc and c["same_source"] == same]))
    ov = [c for c in caps if c["opened_in_horizon"]]
    print(f"captures with a real position opened on the coin inside their horizon: {len(ov)} "
          f"{[(c['capture_id'][-12:], c['opened_in_horizon']) for c in ov]}")
    print(f"refused attempts carrying their own quote_price: "
          f"{sum(1 for c in caps if c['attempt_quote_price'])} of {len(caps)}")
    ages = [c["last_close_age_s"] for c in kept if c["last_close_age_s"] is not None]
    print(f"last close -> refusal, s: p10/p50/p90 {q(ages, .1)}/{q(ages, .5)}/{q(ages, .9)}")
    prem = [c["buy_price"] / c["source_price"] - 1 for c in kept if c["buy_price"] and c["source_price"]]
    pnl, fill_prem = our_fills(a.since)
    print(f"capture buy age_s p50/p90 {q([c['buy_age_s'] for c in kept], .5):.2f}/"
          f"{q([c['buy_age_s'] for c in kept], .9):.2f}")
    print(f"capture buy vs source price: n={len(prem)} p25/p50/p75 "
          f"{q(prem, .25) * 100:+.1f}/{q(prem, .5) * 100:+.1f}/{q(prem, .75) * 100:+.1f}%")
    print(f"our fills vs source price:   n={len(fill_prem)} p25/p50/p75 "
          f"{q(fill_prem, .25) * 100:+.1f}/{q(fill_prem, .5) * 100:+.1f}/{q(fill_prem, .75) * 100:+.1f}%")
    lo, hi = ci([x * 100 for x in pnl])
    print(f"our real Solana closes since {a.since}: n={len(pnl)} realized mean "
          f"{statistics.mean(pnl) * 100:+.1f}% [{lo:+.1f},{hi:+.1f}]")


def selftest():
    # TP crossed first, then stop: TP wins; stop first; neither -> last quote.
    assert path_outcome([(10, 130.0), (20, 50.0)], 100.0) == (TP, -0.5)
    pol, last = path_outcome([(20, 130.0), (10, 60.0)], 100.0)            # order by age, not input
    assert pol == SL and abs(last - 0.3) < 1e-9, (pol, last)
    pol, last = path_outcome([(10, 110.0), (20, 95.0)], 100.0)
    assert abs(pol + 0.05) < 1e-9 and abs(last + 0.05) < 1e-9, (pol, last)
    t0 = datetime(2026, 9, 20)
    caps = dedupe([{"mint": "A", "refused": t0, "horizon_s": 810},
                   {"mint": "A", "refused": t0 + timedelta(seconds=300), "horizon_s": 810},
                   {"mint": "A", "refused": t0 + timedelta(seconds=900), "horizon_s": 810},
                   {"mint": "B", "refused": t0 + timedelta(seconds=300), "horizon_s": 810}])
    assert [c["dup"] for c in caps] == [False, True, False, False], caps
    assert [last_close_class(x) for x in (None, 1.0, -1.0, 0.0)] == ["none", "win", "loss", "zero"]
    print("selftest ok")


if __name__ == "__main__":
    main()
