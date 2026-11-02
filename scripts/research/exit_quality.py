#!/usr/bin/env python3
"""Funded exit quality: what each exit realized against the exact quote the exit sweep decided on.

Operator order 2026-09-27 ~20:50Z ("do the exit quality study now"). Pre-registered with Alpha
before any outcome was read:

  Population  memecoin_positions execution_mode='real', is_open=0, closed_at in [--since, read time),
              all chains, grouped by network_id x exit_reason. Manual exits (exit_reason 'manual'
              or a manual_close fill) are reported apart.
  Unit        each exit fill (memecoin_fills kind in EXIT_KINDS). Primary = the position's final
              exit fill (max id).
  Q           memecoin_route_series seam='exit_decision' for the same position, the highest id with
              observed_at <= fill.created_at. Writer: _keep_route_quote in memecoin_real_runner,
              one row per exact whole-bag quote the exit sweep took (real-UTC clock). Order by id,
              never observed_at (the clock stepped backward 562 times on 2026-09-09).
              Q age = fill.created_at - Q.observed_at; age > 30 s is STALE, excluded from the gap.
  Gap         (notional / qty) / Q.unit_price - 1, in percent: unit space, so a partial exit is
              compared at its own size. $ gap = notional - Q.unit_price * qty.
  Decision    the latest allowed=1 memecoin_exit_trigger_audit row whose reason equals the exit
              reason, at or before the fill. Decision->fill = fill.created_at - its checked_at.
  Claim       "this exit realizes less than its decision quote" only if the bootstrap 95% CI upper
              bound of the mean gap is < 0 (10k draws, seed 1).

Not separated: the price move between the decision quote and the send sits inside the gap.

Ledger read-only (read-only URI). No network.
    python3 scripts/research/exit_quality.py [--since 2026-09-20T20:51:00]
    python3 scripts/research/exit_quality.py --selftest
"""
import argparse
import contextlib
import random
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXIT_KINDS = ("bracket_exit", "mirror_exit", "partial_exit", "derisk", "manual_close")
STALE_S = 30.0


def ts(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def quote_before(quotes, at):
    """quotes: (id, observed_at, unit_price). The highest id observed at or before `at`."""
    before = [q for q in quotes if q[2] and ts(q[1]) <= ts(at)]
    return max(before, key=lambda q: q[0]) if before else None


def gap(notional, qty, unit_price):
    """(gap %, $ gap) of a fill against a quoted unit price, or (None, None) when unpriceable."""
    try:
        notional, qty, unit_price = float(notional), float(qty), float(unit_price)
    except (TypeError, ValueError):
        return None, None
    if qty <= 0 or unit_price <= 0:
        return None, None
    return ((notional / qty) / unit_price - 1.0) * 100.0, notional - unit_price * qty


def decision_before(audit, reason, at):
    """audit: (reason, allowed, checked_at). checked_at of the latest allowed row of `reason`."""
    rows = [a for a in audit if a[0] == reason and a[1] and ts(a[2]) <= ts(at)]
    return max(rows, key=lambda a: ts(a[2]))[2] if rows else None


def boot_ci(values, draws=10_000, seed=1):
    if len(values) < 2:
        return None, None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(draws))
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def load(since):
    out = []
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % (REPO / "sniper_tracker.db"),
                                            uri=True, timeout=10)) as c:
        positions = c.execute(
            "SELECT id, network_id, exit_reason, source_handle FROM memecoin_positions "
            "WHERE execution_mode='real' AND is_open=0 AND closed_at >= ?", (since,)).fetchall()
        for pid, chain, reason, handle in positions:
            fills = c.execute(
                "SELECT id, kind, notional, qty, created_at, execution_slippage_pct FROM memecoin_fills "
                "WHERE position_id=? ORDER BY id", (pid,)).fetchall()
            exits = [f for f in fills if f[1] in EXIT_KINDS]
            if not exits:
                out.append({"id": pid, "chain": chain, "reason": reason, "missing": "no_exit_fill"})
                continue
            quotes = c.execute(
                "SELECT id, observed_at, unit_price FROM memecoin_route_series "
                "WHERE position_id=? AND seam='exit_decision'", (pid,)).fetchall()
            audit = c.execute(
                "SELECT reason, allowed, checked_at FROM memecoin_exit_trigger_audit "
                "WHERE position_id=?", (pid,)).fetchall()
            for f in exits:
                q = quote_before(quotes, f[4])
                g, usd = gap(f[2], f[3], q[2]) if q else (None, None)
                dec = decision_before(audit, reason, f[4])
                out.append({
                    "id": pid, "chain": chain, "reason": reason or "None", "handle": handle,
                    "fill_id": f[0], "kind": f[1], "final": f[0] == exits[-1][0],
                    "notional": f[2], "gap": g, "usd": usd, "slip": f[5],
                    "age": ts(f[4]) - ts(q[1]) if q else None,
                    "dec_to_fill": ts(f[4]) - ts(dec) if dec else None,
                    "manual": reason == "manual" or f[1] == "manual_close"})
    return out


def line(label, rows):
    gaps = [r["gap"] for r in rows]
    lo, hi = boot_ci(gaps)
    return ("%-34s n=%3d gap p10 %+6.2f p50 %+6.2f p90 %+6.2f mean %+6.2f CI[%s,%s] $%+8.2f | "
            "Qage p50 %s p90 %s | dec->fill p50 %s p90 %s (n=%d) | runner slip p50 %s" % (
                label, len(rows), pct(gaps, .1), pct(gaps, .5), pct(gaps, .9),
                statistics.mean(gaps), "%+.2f" % lo if lo is not None else "na",
                "%+.2f" % hi if hi is not None else "na", sum(r["usd"] for r in rows),
                _f(pct([r["age"] for r in rows], .5)), _f(pct([r["age"] for r in rows], .9)),
                _f(pct([r["dec_to_fill"] for r in rows], .5)),
                _f(pct([r["dec_to_fill"] for r in rows], .9)),
                sum(1 for r in rows if r["dec_to_fill"] is not None),
                _f(pct([r["slip"] for r in rows], .5))))


def _f(x):
    return "%.2f" % x if x is not None else "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-20T20:51:00")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--worst", type=int, default=0, help="list the N worst primary gaps")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    rows = load(a.since)
    missing = [r for r in rows if r.get("missing")]
    fills = [r for r in rows if not r.get("missing")]
    noquote = [r for r in fills if r["gap"] is None]
    stale = [r for r in fills if r["gap"] is not None and r["age"] > STALE_S]
    usable = [r for r in fills if r["gap"] is not None and r["age"] <= STALE_S]
    primary = [r for r in usable if r["final"] and not r["manual"]]
    print("positions %d | exit fills %d | no exit fill %d | no quote before fill %d | stale (>%.0f s) %d"
          % (len({r["id"] for r in rows}), len(fills), len(missing), len(noquote), STALE_S, len(stale)))
    print(line("PRIMARY final non-manual (all)", primary))
    groups = {}
    for r in primary:
        groups.setdefault((r["chain"], r["reason"]), []).append(r)
    for key in sorted(groups, key=lambda k: -len(groups[k])):
        print(line("  %s %s" % key, groups[key]))
    partials = [r for r in usable if not r["final"] and not r["manual"]]
    if partials:
        print(line("PARTIAL (non-final) exit fills", partials))
    manual = [r for r in usable if r["manual"]]
    if manual:
        print(line("MANUAL", manual))
    by_chain = {}
    for r in stale + noquote:
        by_chain.setdefault((r["chain"], r["reason"], "stale" if r["gap"] is not None else "noquote"), 0)
        by_chain[(r["chain"], r["reason"], "stale" if r["gap"] is not None else "noquote")] += 1
    print("excluded:", dict(sorted(by_chain.items())))
    for r in sorted(primary, key=lambda r: r["gap"])[:a.worst]:
        print("  worst pos %d %s %s %s gap %+.2f%% $%+.2f Qage %.2f s notional %.2f" % (
            r["id"], r["chain"], r["reason"], r["handle"], r["gap"], r["usd"], r["age"], r["notional"]))


def selftest():
    # 13 was written after 12 but stamped earlier (the clock stepped back): id order wins, so 13.
    # 14 is after the fill and has the highest id; 15 has no unit price.
    quotes = [(10, "2026-09-27T19:10:50+00:00", 1.00), (12, "2026-09-27T19:10:58+00:00", 0.95),
              (13, "2026-09-27T19:10:55+00:00", 0.90), (14, "2026-09-27T19:11:30+00:00", 0.50),
              (15, "2026-09-27T19:10:59+00:00", None)]
    assert quote_before(quotes, "2026-09-27T19:11:00+00:00")[0] == 13
    g, usd = gap(81.0, 100.0, 0.90)          # sold 100 at 0.81 against a 0.90 quote
    assert round(g, 6) == -10.0 and round(usd, 6) == -9.0, (g, usd)
    assert gap(81.0, 0, 0.9) == (None, None) and gap(None, 1, 1) == (None, None)
    # The disallowed row is the LATEST b_stop before the fill, so ignoring `allowed` picks it.
    audit = [("b_stop", 0, "2026-09-27T19:10:50+00:00"), ("b_stop", 1, "2026-09-27T19:10:45+00:00"),
             ("vol_death", 1, "2026-09-27T19:10:55+00:00"), ("b_stop", 1, "2026-09-27T19:12:00+00:00")]
    assert decision_before(audit, "b_stop", "2026-09-27T19:11:00+00:00") == "2026-09-27T19:10:45+00:00"
    assert decision_before(audit, "real_tp", "2026-09-27T19:11:00+00:00") is None
    lo, hi = boot_ci([1.0, 2.0, 3.0, 4.0])
    assert 1.0 <= lo <= hi <= 4.0
    print("selftest ok")


if __name__ == "__main__":
    main()
