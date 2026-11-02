#!/usr/bin/env python3
"""Does the Solana hourly volume floor refuse coins worth refusing? Refusal captures vs fills near the line.

Pre-registered in HANDOFF.md at 2026-09-29T21:08:38Z (Gamma, "PRE-REGISTRATION, Solana h1 volume
floor") before any outcome was computed. Read-only: the ledger and data/memecoin_refusal_captures.db.

Population: Solana attempts since 2026-09-13 refused "(turnover_under_floor" or "(under_abs_floor",
first attempt per origin. X and Y come from the reason itself ("h1 organic volume $X under floor $Y"),
which is the gate's own decided figure. The grade is cap_refusal_outcomes.grade(): the capture's
exact-route buy of the clip, then sells of the same quantity; TP +25%, stop -35%, else the +300 s mark.

    python3 scripts/research/solana_h1_refusal_outcomes.py [--since 2026-09-13T00:00:00]
    python3 scripts/research/solana_h1_refusal_outcomes.py --selftest
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import collections
import contextlib
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")           # before any sniper import: a settings read without it is the code default

from cap_refusal_outcomes import CAPTURES, LEDGER, MARK_S, STOP, TOL_S, TP, capture_obs, grade, mark, mint_ci, ro  # noqa: E402

REASON = re.compile(r"h1 organic volume \$([0-9.,]+) under floor \$([0-9.,]+) \((turnover_under_floor|under_abs_floor)")
MICRO_H1_LIVE_AT = "2026-09-28T18:51:04"   # runner start that activated MEMECOIN_REAL_SOLANA_MICRO_MIN_TURNOVER_H1
MICRO_H1 = 0.143
INDEX_LAG_S = 15.0                         # cfg.provider_index_lag_s("solana"), read with .env loaded
BINS = ((0.0, 0.1), (0.1, 0.5), (0.5, 1.0))


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def parse_reason(reason):
    """(X organic, Y floor, leg) from a rendered hourly-floor refusal, or None."""
    m = REASON.search(str(reason or ""))
    if not m:
        return None
    return float(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")), m.group(3)


def ratio_bin(x, y):
    r = x / y if y else None
    return next(("%g-%g" % b for b in BINS if r is not None and b[0] <= r < b[1]), None)


def current_rule_refuses(band, organic, effective_mcap):
    """The live Solana hourly rule: nano has none, micro needs organic >= 0.143 x effective mcap."""
    if band == "micro" and effective_mcap:
        return organic < MICRO_H1 * float(effective_mcap)
    return False


def route_grade(entry_notional, series):
    """series: [(age_s, expected_usdc, open_qty)] in id order. Same TP/stop/mark as the captures.

    After a partial exit the remaining bag is valued pro rata (expected_usdc x first_qty / open_qty).
    A series that ends before a decision is censored, never marked at its last quote."""
    if not entry_notional or not series:
        return None, "censored"
    first_qty = series[0][2]
    obs = []
    for age, usdc, qty in series:
        if usdc is None or not qty or not first_qty:
            continue
        obs.append((age, usdc * first_qty / qty / entry_notional - 1.0))
    return grade(obs)


def refusals(since):
    out = {}
    with contextlib.closing(ro(LEDGER)) as c:
        for r in c.execute(
                "SELECT origin_event_id, mint, decision_started_at, source_created_at, terminal_reason, "
                "classification_json, gate_inputs_json FROM memecoin_entry_attempts WHERE network_id='solana' "
                "AND decision_started_at>=? AND (terminal_reason LIKE '%(turnover_under_floor%' "
                "OR terminal_reason LIKE '%(under_abs_floor%') ORDER BY decision_started_at", (since,)):
            if r["origin_event_id"] in out:
                continue
            parsed = parse_reason(r["terminal_reason"])
            if parsed is None:
                continue
            x, y, leg = parsed
            cls = json.loads(r["classification_json"] or "{}")
            band = cls.get("coin_band")
            emcap = (cls.get("facts") or {}).get("effective_mcap")
            out[r["origin_event_id"]] = {
                "origin": r["origin_event_id"], "mint": r["mint"], "at": r["decision_started_at"],
                "leg": "turnover" if leg == "turnover_under_floor" else "abs", "band": band, "x": x, "y": y,
                "bin": ratio_bin(x, y), "now": current_rule_refuses(band, x, emcap),
                "m5": m5_would_refuse(r, json.loads(r["gate_inputs_json"] or "{}")),
                "live_micro": band == "micro" and r["decision_started_at"] >= MICRO_H1_LIVE_AT}
    return list(out.values())


def m5_would_refuse(row, gate):
    """Whether the in-band m5 ratio floor alone refuses, on the gate's recorded inputs. None if unknown."""
    from sniper.services.memecoin_real_runner import organic_volume
    try:
        age = ts(row["decision_started_at"]) - ts(row["source_created_at"])
    except (TypeError, ValueError):
        age = None
    mcap, floor = gate.get("mcap"), gate.get("ratio_floor")
    if floor is not None and float(floor) == 0.0:
        return False                   # the gate recorded its m5 ratio leg as OFF: it cannot refuse
    m5 = organic_volume(gate.get("volume_m5"), gate.get("their_size_usd") or 0.0,
                        source_age_s=age, index_lag_s=INDEX_LAG_S)
    if m5 is None or not mcap or not floor:
        return None
    return m5 < float(floor) * float(mcap)


def comparator(since):
    """Funded Solana fills that passed the hourly floor with organic / need in [1, 2)."""
    from sniper.services.memecoin_real_runner import organic_volume
    rows, below = [], 0
    with contextlib.closing(ro(LEDGER)) as c:
        for r in c.execute(
                "SELECT p.id, p.mint, p.entry_notional, p.realized_pnl, p.is_open, p.opened_recorded_at, "
                "a.decision_started_at, a.source_created_at, a.trace_json FROM memecoin_entry_attempts a "
                "JOIN memecoin_positions p ON p.id=a.position_id WHERE a.network_id='solana' "
                "AND a.decision_started_at>=? AND p.execution_mode='real' AND a.trace_json LIKE '%\"volume_gate\"%'",
                (since,)):
            evs = [s for s in json.loads(r["trace_json"]).get("substage_events") or []
                   if s.get("name") == "volume_gate"]
            ev = evs[-1] if evs else None
            if not ev or ev.get("reason") != "ok" or not ev.get("required_usd") or ev.get("volume_h1") is None:
                continue
            try:
                age = ts(r["decision_started_at"]) - ts(r["source_created_at"])
            except (TypeError, ValueError):
                age = None
            organic = organic_volume(ev["volume_h1"], ev.get("source_volume_excluded_usd") or 0.0,
                                     source_age_s=age, index_lag_s=INDEX_LAG_S)
            ratio = organic / float(ev["required_usd"])
            if ratio < 1.0:
                below += 1
                continue
            if ratio >= 2.0:
                continue
            series = [(ts(o) - ts(r["opened_recorded_at"]), u, q) for o, u, q in c.execute(
                "SELECT observed_at, expected_usdc, open_qty FROM memecoin_route_series WHERE position_id=? "
                "AND seam='exit_decision' ORDER BY id", (r["id"],))] if r["opened_recorded_at"] else []
            rows.append({"id": r["id"], "mint": r["mint"], "notional": r["entry_notional"],
                         "realized": (r["realized_pnl"] / r["entry_notional"]
                                      if not r["is_open"] and r["realized_pnl"] is not None and r["entry_notional"]
                                      else None),
                         "usd": r["realized_pnl"] if not r["is_open"] else None,
                         "c2": route_grade(r["entry_notional"], series), "scope": ev.get("h1_floor_scope")})
    return rows, below


def summary(label, rows):
    """rows: [(mint, ret, usd)]."""
    if len(rows) < 5:
        print("  %-44s n=%d (no CI below 5)%s" % (label, len(rows), "".join(
            "  %+.1f%%/$%+.2f" % (100 * r, u) for _m, r, u in rows)))
        return None
    rets = [r for _m, r, _u in rows]
    lo, hi = mint_ci([(m, r) for m, r, _u in rows])
    mean = sum(rets) / len(rets)
    print("  %-44s n=%3d mean %+6.1f%% CI [%+.1f, %+.1f]  sum $%+8.2f  wins %d" % (
        label, len(rows), 100 * mean, 100 * lo, 100 * hi, sum(u for _m, _r, u in rows), sum(r > 0 for r in rets)))
    return mean, lo, hi


def report(args):
    refs = refusals(args.since)
    print("Solana hourly-floor refusals since %s: %d origins / %d mints" % (
        args.since, len(refs), len({r["mint"] for r in refs})))
    print("  by leg x band:", dict(collections.Counter((r["leg"], r["band"]) for r in refs)))
    print("  current rule also refuses:", sum(r["now"] for r in refs),
          " live micro leg (>= %sZ): %d" % (MICRO_H1_LIVE_AT, sum(r["live_micro"] for r in refs)))
    print("  m5 ratio floor alone would refuse:", dict(collections.Counter(r["m5"] for r in refs)))
    with contextlib.closing(ro(CAPTURES)) as cap:
        for r in refs:
            r["clip"], r["obs"] = capture_obs(cap, r["origin"])
    for r in refs:
        r["ret"], r["how"] = grade(r["obs"]) if r["clip"] and r["obs"] else (None, "no_capture")
    print("  grade outcome by leg:", dict(collections.Counter((r["leg"], r["how"]) for r in refs)))

    def dec(rs):
        return [(r["mint"], r["ret"], r["ret"] * r["clip"]) for r in rs if r["ret"] is not None]

    print("\nPRIMARY TP%.0f/SL%.0f else +%.0f s mark (capture clip):" % (100 * TP, -100 * STOP, MARK_S))
    summary("G1 all refused", dec(refs))
    g2 = summary("G2 current rule still refuses (VERDICT)", dec([r for r in refs if r["now"]]))
    summary("G3 live micro leg, forward", dec([r for r in refs if r["live_micro"]]))
    for leg in ("turnover", "abs"):
        for band in ("nano", "micro"):
            rs = [r for r in refs if r["leg"] == leg and r["band"] == band]
            if rs:
                summary("leg %s, %s" % (leg, band), dec(rs))
    for lo, hi in BINS:
        summary("organic/need %g-%g" % (lo, hi), dec([r for r in refs if r["bin"] == "%g-%g" % (lo, hi)]))
    for m5 in (True, False, None):
        rs = [r for r in refs if r["m5"] is m5]
        if rs:
            summary("m5 ratio floor alone refuses=%s" % m5, dec(rs))
    for h in (60.0, 300.0):
        summary("plain mark +%ds (G1)" % h,
                [(r["mint"], v, v * r["clip"]) for r in refs if r["clip"] and r["obs"]
                 for v in [mark(r["obs"], h)] if v is not None])

    comp, below = comparator(args.since)
    print("\nCOMPARATOR: funded Solana fills passing with organic h1 / need in [1, 2): %d "
          "(passed below 1 on recompute, excluded: %d); scopes %s" % (
              len(comp), below, dict(collections.Counter(r["scope"] for r in comp))))
    c1 = summary("C1 realized / notional", [(r["mint"], r["realized"], r["usd"]) for r in comp
                                             if r["realized"] is not None])
    print("  C2 outcome:", dict(collections.Counter(h for _v, h in (r["c2"] for r in comp))))
    c2rows = [(r["mint"], r["c2"][0], r["c2"][0] * r["notional"]) for r in comp if r["c2"][0] is not None]
    c2 = summary("C2 route series TP25/SL35/+300 s", c2rows)

    print("\nRULE on G2:")
    if g2 is None:
        print("  no verdict: decided n < 5")
        return
    mean, lo, hi = g2
    if hi < 0:
        print("  KEEP: refused CI wholly below 0")
    elif lo > -0.05 and c1 and mean >= c1[0] and (len(c2rows) < 10 or (c2 and mean >= c2[0])):
        print("  REMOVE: refused CI lower %+.1f%% > -5%% and mean beats the comparator(s)" % (100 * lo))
    else:
        print("  UNDECIDED")


def selftest():
    assert parse_reason("h1 organic volume $277 under floor $586 (turnover_under_floor, revival guard)") == (
        277.0, 586.0, "turnover_under_floor")
    assert parse_reason("h1 organic volume $2969 under floor $3000 (under_abs_floor, revival guard)")[2] == "under_abs_floor"
    assert parse_reason("m5 organic volume $31 under floor $5000 (m5_volume_under_abs_floor, revival guard)") is None
    assert ratio_bin(277, 586) == "0.1-0.5" and ratio_bin(5, 586) == "0-0.1" and ratio_bin(2969, 3000) == "0.5-1"
    assert current_rule_refuses("micro", 3434, 60375) is True
    assert current_rule_refuses("micro", 9000, 60375) is False
    assert current_rule_refuses("nano", 1, 60375) is False
    assert route_grade(100.0, [(10, 90.0, 1000), (40, 130.0, 1000)]) == (0.30000000000000004, "tp")
    # a partial exit halves the bag; the remainder is valued pro rata: 65 on half = 130 on the whole
    # (+30%, tp). Read without the pro-rata step the same row is 65/100 = -35%, a stop.
    assert route_grade(100.0, [(10, 90.0, 1000), (40, 65.0, 500)])[1] == "tp"
    assert route_grade(100.0, [(10, 90.0, 1000)]) == (None, "censored")
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--since", default="2026-09-13T00:00:00")
    a = ap.parse_args()
    return selftest() if a.selftest else report(a)



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('report', 'main'))

if __name__ == "__main__":
    main()
