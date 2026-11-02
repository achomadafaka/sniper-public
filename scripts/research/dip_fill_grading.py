#!/usr/bin/env python3
"""Dip fill: after a slippage failure or a chase-cap refusal, would waiting for a dip back under the cap
have filled, and would that fill have paid?

Operator via Alpha, 2026-09-30: keep looking after the signal and fill on a dip ("tons of times
there's a tiny dip we can exploit and fill even better than him"). Pre-registered as brain episode
bb0ce337 before any dip outcome was computed.

POPULATIONS: (A) the slippage failures of slippage_fail_grading.py; (B) Solana attempts refused
'exact quote is N% above source price (cap C%)', first attempt per origin.
LIMIT P_lim (SOL per token): A slot lane = spend SOL / signed min tokens; A Jupiter lane =
min(source*(1+C), quote*(1+S)) / sol_usd; B = quote*(1+C)/(1+N) / sol_usd (the gate's own N and C).
sol_usd = Binance SOLUSDT 1m close at t0.
DIP FILL at window W: the first BUY trade (successful, SOL-quoted, on the mint) with blockTime in
[t0 + LATENCY_S, t0 + W] and average price <= P_lim. Fill price = that trade's average price
(optimistic: no own impact). Outcome from the fill: cap_refusal_outcomes.grade on every later trade.
The fill is nested across W (a dip at 20 s fills at W >= 30), so each signal has one outcome.

    python3 scripts/research/dip_fill_grading.py [--since 2026-09-13] [--pages 40]
    python3 scripts/research/dip_fill_grading.py --report logs/acceptance/dip_fill_grading.jsonl
    python3 scripts/research/dip_fill_grading.py --selftest
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
import os
import re
import statistics
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]

from cap_refusal_outcomes import MARK_S, TOL_S, grade, mint_ci  # noqa: E402
from slippage_fail_grading import LEDGER, failures, gtfa, ro, sol_trade  # noqa: E402

WINDOWS = (15, 30, 60, 120)
LATENCY_S = 1.0
SEED = 20261003
EXCLUDE = {"edd46490d9004f7596035c5dc16d8c2a"}    # data defect, 8c3bbaba2 audit: a +9,390% mis-decoded trade
CHASE = re.compile(r"exact quote is \+?(-?[0-9.]+)% above source price \(cap ([0-9.]+)%\)")
BINANCE = "https://api.binance.com/api/v3/klines?symbol=SOLUSDT&interval=1m&startTime=%d&limit=1"


def epoch(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


# AMENDMENTS cb26bd77 + 9a035717 (2026-10-01, POST HOC, forward-only). For signals at or after AMEND_AT a
# trade spending under DUST_SOL is not price evidence: it is dropped from the fill candidates AND the
# outcome path (counted n_dust), and a fill candidate under MIN_FILL_OVER_LIM x the limit is skipped
# (counted n_implausible). Cause: two dust "fills" at 0.012x / 0.028x the limit graded +104x / +197x, the
# same class as EXCLUDE. Signals before AMEND_AT keep bb0ce337 unchanged.
AMEND_AT = epoch("2026-10-01T08:10:00+00:00")
DUST_SOL, MIN_FILL_OVER_LIM = 0.01, 0.10


def price_evidence(sol, amended):
    return not amended or sol >= DUST_SOL


_SOL = {}


def sol_usd(t):
    minute = int(t // 60) * 60
    if minute not in _SOL:
        with urllib.request.urlopen(BINANCE % (minute * 1000), timeout=10) as r:
            rows = json.loads(r.read())
        _SOL[minute] = float(rows[0][4]) if rows else None
    return _SOL[minute]


def chase_rows(conn, since):
    out, seen = [], set()
    for r in conn.execute(
            "SELECT attempt_id, origin_event_id, mint, source_handle, source_user_id, decision_started_at, "
            "terminal_reason, quote_price, signal_source FROM memecoin_entry_attempts WHERE network_id='solana' "
            "AND decision_started_at>=? AND terminal_reason LIKE 'exact quote is %above source price%' "
            "ORDER BY decision_started_at", (since,)):
        m = CHASE.search(r["terminal_reason"] or "")
        key = r["origin_event_id"] or r["attempt_id"]
        if not m or key in seen or r["signal_source"] == "manual":
            continue
        seen.add(key)
        n, c = float(m.group(1)) / 100.0, float(m.group(2)) / 100.0
        out.append({"pop": "B", "attempt": r["attempt_id"], "mint": r["mint"], "wallet": r["source_handle"],
                    "at": r["decision_started_at"], "t0": epoch(r["decision_started_at"]),
                    "lim_usd": (r["quote_price"] * (1 + c) / (1 + n)) if r["quote_price"] else None})
    return out


def limit_a(row, extra, slip):
    """P_lim in SOL per token for a slippage failure, or None."""
    if row.get("min") and row.get("spend_sol") and row.get("decimals") is not None:
        return row["spend_sol"] / (row["min"] / 10 ** row["decimals"]), None
    cap = (extra["cap"] or 30.0) / 100.0
    legs = [x for x in ((extra["source"] or 0) * (1 + cap), (extra["quote"] or 0) * (1 + slip)) if x > 0]
    return None, (min(legs) if legs else None)


def dip_fill(trades, t0, lim, wmax, min_sol=0.0, floor=0.0):
    """Index of the first BUY in [t0+LATENCY_S, t0+wmax] priced in [floor x lim, lim] and spending >= min_sol,
    or None. min_sol > 0 is the POST HOC sized variant: a buy at least our clip's size proves the clip fits.
    floor > 0 is amendment 9a035717 (a fill under 0.10x the limit is skipped)."""
    for i, (bt, _k, side, px, sol) in enumerate(trades):
        if bt > t0 + wmax:
            return None
        if bt >= t0 + LATENCY_S and side == "buy" and floor * lim <= px <= lim and sol >= min_sol:
            return i
    return None


def outcome(trades, i):
    bt0, _k, _s, p0, _sol = trades[i]
    return grade([(bt - bt0, px / p0 - 1) for bt, _k, _s, px, _sol in trades[i + 1:]])


def decided(trades, t0, wmax, fill):
    if fill is None:
        return bool(trades) and trades[-1][0] > t0 + wmax
    return outcome(trades, fill)[1] in ("tp", "stop")


def graduated(kinds, t0, wmax):
    """a8833925 (2026-09-30T09:05Z): the mint's trades move from the pump.fun curve to a pool
    inside [t0 - 2, t0 + wmax]. A coin that never traded on the curve is never flagged."""
    win = [k for bt, k in kinds if t0 - 2 <= bt <= t0 + wmax]
    return "curve" in win and "pool" in win[win.index("curve"):]


def observe(row, pages, conn_fn):
    import dump_forensics as dfx
    import funded_exit_truth as fet
    from slot_minout_grading import wait_flat
    fet.SPACING_S = 0.5
    t0, lim, wmax = row["t0"], row["lim"], max(WINDOWS)
    amended = epoch(row["at"]) >= AMEND_AT
    floor = MIN_FILL_OVER_LIM if amended else 0.0
    trades, token, n, fill, n_dust = [], None, 0, None, 0
    kinds = []                                  # (blockTime, 'curve'|'pool') for a8833925's flag
    while n < pages:
        wait_flat(conn_fn)
        opts = {"transactionDetails": "full", "sortOrder": "asc", "limit": 100, "encoding": "json",
                "maxSupportedTransactionVersion": 1, "commitment": "confirmed",
                "filters": {"blockTime": {"gte": int(t0) - 2, "lte": int(t0 + wmax + MARK_S + TOL_S + 10)},
                            "status": "succeeded"}}
        if token:
            opts["paginationToken"] = token
        res = fet.rpc("getTransactionsForAddress", [row["mint"], opts]) or {}
        page, token = res.get("data") or [], res.get("paginationToken")
        n += 1
        for t in page:
            px = sol_trade(dfx, t, row["mint"])
            if px is None or not t.get("blockTime"):
                continue
            tr = dfx.trade_of(t, row["mint"], 1.0) or {}
            kinds.append((t["blockTime"], tr.get("kind")))
            if not price_evidence(float(tr.get("sol") or 0.0), amended):
                n_dust += 1
                continue
            trades.append((t["blockTime"], (t["slot"], t["transactionIndex"]), tr.get("side"), px,
                           float(tr.get("sol") or 0.0)))
        if not token or len(page) < 100:
            token = None
            break
        if all(decided(trades, t0, wmax, dip_fill(trades, t0, lim, wmax, ms, floor)) for ms in (0.0, row["clip_sol"])):
            break                                   # both variants decided: no dip in the window, or a touch
    out = dict(row, pages=n, truncated=token is not None, n_trades=len(trades),
               graduated_in_window=graduated(kinds, t0, wmax), amended=amended, n_dust=n_dust,
               n_implausible=sum(1 for bt, _k, side, px, _sol in trades
                                 if t0 + LATENCY_S <= bt <= t0 + wmax and side == "buy" and px < floor * lim))
    reached = bool(trades) and trades[-1][0] > t0 + wmax
    for tag, ms in (("", 0.0), ("sized_", row["clip_sol"])):
        fill = dip_fill(trades, t0, lim, wmax, ms, floor)
        if fill is None:
            # A truncated read that never reached t0+wmax cannot say "no dip".
            out[tag + "status"] = "no_dip" if reached or token is None else "unread_window"
            continue
        bt, _k, _s, px, sol = trades[fill]
        ret, how = outcome(trades, fill)
        out.update({tag + "status": "dip", tag + "dip_s": bt - t0, tag + "fill_px": px,
                    tag + "fill_over_lim": px / lim, tag + "fill_sol": sol, tag + "ret": ret, tag + "how": how})
    return out


def summarize(rows, label, clip, tag=""):
    rows = [dict(r, status=r.get(tag + "status"), dip_s=r.get(tag + "dip_s"), ret=r.get(tag + "ret"),
                 how=r.get(tag + "how")) for r in rows]
    sig = len(rows)
    for w in WINDOWS:
        filled = [r for r in rows if r.get("status") == "dip" and r["dip_s"] <= w]
        g = [r for r in filled if r.get("ret") is not None]
        if not g:
            print("  %-26s W=%3ds signals %3d  dip-filled %3d  graded 0" % (label, w, sig, len(filled)))
            continue
        mean = statistics.mean(r["ret"] for r in g)
        lo, hi = mint_ci([(r["mint"], r["ret"]) for r in g], seed=SEED) if len(g) >= 5 else (None, None)
        print("  %-26s W=%3ds signals %3d  dip-filled %3d (%4.0f%%)  graded %3d  mean %+6.1f%%  CI %s  sum $%+8.2f  tp/stop/mark %s" % (
            label, w, sig, len(filled), 100.0 * len(filled) / sig if sig else 0, len(g), 100 * mean,
            "[%+.1f, %+.1f]" % (100 * lo, 100 * hi) if lo is not None else "(n<5)",
            sum(r["ret"] for r in g) * clip,
            "/".join(str(sum(r["how"] == h for r in g)) for h in ("tp", "stop", "mark"))))


def report(rows, clip):
    grad = [r for r in rows if r.get("graduated_in_window")]
    if grad:
        print("a8833925 graduated_in_window, reported apart: %s" % [
            (r["at"][:16], r["wallet"], r.get("status"), r.get("ret")) for r in grad])
    rows = [r for r in rows if not r.get("graduated_in_window") and r.get("attempt") not in EXCLUDE]
    print("rows %d; status %s; clip $%.2f (median funded Solana clip)" % (
        len(rows), dict(collections.Counter((r["pop"], r.get("status")) for r in rows)), clip))
    for label, sel in (("A slippage failures", lambda r: r["pop"] == "A"),
                       ("B chase refusals", lambda r: r["pop"] == "B"),
                       ("A+B pooled", lambda r: True),
                       ("DegenCapitalLLC A+B", lambda r: r["wallet"] == "DegenCapitalLLC"),
                       ("notdecu A+B", lambda r: r["wallet"] == "notdecu")):
        summarize([r for r in rows if sel(r) and r.get("status") in ("dip", "no_dip")], label, clip)
    print("POST HOC sized variant (the dip buy spent >= our clip in SOL):")
    for label, sel in (("A+B pooled sized", lambda r: True),
                       ("DegenCapitalLLC sized", lambda r: r["wallet"] == "DegenCapitalLLC"),
                       ("notdecu sized", lambda r: r["wallet"] == "notdecu")):
        summarize([r for r in rows if sel(r) and r.get("sized_status") in ("dip", "no_dip")], label, clip, "sized_")
    d = [r for r in rows if r.get("status") == "dip"]
    if d:
        print("  dip timing s p50 %.0f; fill/limit p50 %.3f" % (
            statistics.median(r["dip_s"] for r in d), statistics.median(r["fill_over_lim"] for r in d)))


def selftest():
    tr = [(100.0, (1, 0), "buy", 2.0, 5.0), (100.5, (1, 1), "sell", 0.9, 5.0), (101.0, (2, 0), "buy", 1.0, 0.1),
          (102.0, (2, 1), "buy", 0.95, 2.0), (140.0, (3, 0), "buy", 1.3, 1.0)]
    assert dip_fill(tr, 100.0, 1.0, 120) == 2            # the sell under the limit is not a fill; latency 1 s
    assert dip_fill(tr, 100.0, 0.5, 120) is None
    assert outcome(tr, 2) == (0.30000000000000004, "tp")
    assert dip_fill(tr, 100.0, 1.0, 120, min_sol=1.0) == 3   # the 0.1 SOL buy is too small for our clip
    dust = [(101.0, (2, 0), "buy", 0.012, 2.0), (102.0, (2, 1), "buy", 0.95, 2.0)]
    assert dip_fill(dust, 100.0, 1.0, 120) == 0                  # bb0ce337: the 0.012x buy is a fill
    assert dip_fill(dust, 100.0, 1.0, 120, floor=MIN_FILL_OVER_LIM) == 1   # 9a035717: skipped
    assert price_evidence(0.0001, False) and not price_evidence(0.0001, True) and price_evidence(0.01, True)
    m = CHASE.search("exact quote is +89% above source price (cap 35%)")
    assert m and m.group(1) == "89" and m.group(2) == "35"
    lim, lim_usd = limit_a({"min": 2_000_000, "spend_sol": 1.0, "decimals": 6}, {}, 0.15)
    assert lim == 0.5 and lim_usd is None
    assert limit_a({}, {"cap": 30.0, "source": 1.0, "quote": 1.0}, 0.15) == (None, 1.15)
    assert graduated([(99, "curve"), (105, "pool")], 100.0, 120) is True
    assert graduated([(99, "pool"), (105, "curve")], 100.0, 120) is False
    assert graduated([(50, "curve"), (105, "pool")], 100.0, 120) is False   # curve before the window
    assert graduated([(99, "pool"), (130, "pool")], 100.0, 120) is False
    print("selftest ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="2026-09-13")
    ap.add_argument("--pages", type=int, default=40)
    ap.add_argument("--graded", default=str(REPO / "logs" / "acceptance" / "slippage_fail_grading.jsonl"))
    ap.add_argument("--out", default=str(REPO / "logs" / "acceptance" / "dip_fill_grading.jsonl"))
    ap.add_argument("--after", help="forward re-run: only signals after this ISO time (clip stays --since)")
    ap.add_argument("--report")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    from sniper.util.envfiles import load_env_files
    load_env_files(str(REPO))
    slip = float(os.environ.get("MEMECOIN_ENTRY_TX_SLIPPAGE_PCT") or 15) / 100.0
    conn_fn = lambda: ro(LEDGER)          # noqa: E731
    with contextlib.closing(conn_fn()) as c:
        clip = statistics.median(r[0] for r in c.execute(
            "SELECT entry_notional FROM memecoin_positions WHERE execution_mode='real' AND network_id='solana' "
            "AND opened_at>=? AND entry_notional>0", (args.since,)))
        if args.report:
            report([json.loads(x) for x in open(args.report)], clip)
            return 0
        known = {}
        if Path(args.graded).exists():
            for x in open(args.graded):
                g = json.loads(x)
                if g.get("kind") == "fail" and g.get("our_time"):
                    known[g["attempt"]] = g["our_time"]
        todo = []
        for f in failures(c, args.since):
            e = c.execute("SELECT quote_price, source_price, chase_effective_cap_pct FROM memecoin_entry_attempts "
                          "WHERE attempt_id=?", (f["attempt"],)).fetchone()
            lim, lim_usd = limit_a(f, {"quote": e[0], "source": e[1], "cap": e[2]}, slip)
            todo.append({"pop": "A", "attempt": f["attempt"], "mint": f["mint"], "wallet": f["wallet"],
                         "at": f["at"], "sig": f["sig"], "t0": known.get(f["attempt"]), "lim": lim,
                         "lim_usd": lim_usd, "clip_usd": f.get("clip_usd")})
        todo += chase_rows(c, args.since)
        if args.after:
            todo = [r for r in todo if epoch(r["at"]) > epoch(args.after)]
    print("signals A %d, B %d; tx slippage %.0f%%" % (
        sum(r["pop"] == "A" for r in todo), sum(r["pop"] == "B" for r in todo), 100 * slip), flush=True)
    import dump_forensics as dfx
    import funded_exit_truth as fet
    with open(args.out, "w") as fh:
        for r in todo:
            try:
                if r["t0"] is None and r.get("sig"):
                    tx = dfx.get_tx(r["sig"])
                    r["t0"] = (tx or {}).get("blockTime")
                if r["t0"] is None:
                    g = dict(r, status="no_t0")
                else:
                    if r.get("lim") is None and r.get("lim_usd"):
                        su = sol_usd(r["t0"])
                        r["lim"] = r["lim_usd"] / su if su else None
                    su = sol_usd(r["t0"])
                    r["clip_sol"] = (r.get("clip_usd") or clip) / su if su else float("inf")
                    g = dict(r, status="no_limit") if not r.get("lim") else observe(r, args.pages, conn_fn)
            except fet.Stop as exc:
                print("STOP: %s" % exc, flush=True)
                break
            fh.write(json.dumps(g) + "\n")
            fh.flush()
            print("  %s %s %-16s %-13s %s" % (g["at"][:16], g["pop"], (g["wallet"] or "?")[:16], g["status"],
                                             "dip@%.0fs %+.1f%% %s" % (g["dip_s"], 100 * g["ret"], g["how"])
                                             if g.get("status") == "dip" and g.get("ret") is not None
                                             else ("dip@%.0fs %s" % (g["dip_s"], g.get("how"))
                                                   if g.get("status") == "dip" else "")), flush=True)
    report([json.loads(x) for x in open(args.out)], clip)
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('summarize', 'report', 'main'))

if __name__ == "__main__":
    raise SystemExit(main())
