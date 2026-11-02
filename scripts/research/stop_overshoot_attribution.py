#!/usr/bin/env python3
"""Funded stop overshoot: attribution (gap / confirm / other delay / execution) and a replay of
confirm-count and stop-level variants over the recorded exact whole-bag quote paths.

Read-only: one `mode=ro` connection, four SELECTs, closed before any analysis. Path order is
`memecoin_route_series.id`, never `observed_at` — on 2026-09-09 the corrected clock stepped backward
562 times, so that day measures time in quote COUNT x 1.5 s. The return at a quote is
`unit_price / entry_price - 1`, the live rail's own convention (fill anchor, exact unit price).

A position is a PHANTOM trigger when its fill beat the quote that fired the stop by more than 5
points. Its gap and execution terms cancel inside the same position, so the attribution reports
it apart from the positions where the quote agrees with the fill.

    python3 scripts/research/stop_overshoot_attribution.py [--db sniper_tracker.db] [--since 2026-08-28]
"""
import argparse
import json
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--db", default="sniper_tracker.db")
ap.add_argument("--since", default="2026-08-28")
args = ap.parse_args()


def _pull(db, since):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        pos = [dict(r) for r in c.execute(
            "SELECT id, network_id, exit_reason, opened_at, opened_recorded_at, closed_at, "
            "entry_price, entry_notional, realized_pnl, funded_contract_json "
            "FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 "
            "AND COALESCE(signal_source,'')!='manual' AND opened_at>=?", (since,))]
        ids = [p["id"] for p in pos]
        q = ",".join("?" * len(ids)) or "NULL"
        rs = [dict(r) for r in c.execute(
            f"SELECT id, position_id, seam, observed_at, quoted_at, expected_usdc, open_qty, "
            f"unit_price FROM memecoin_route_series WHERE position_id IN ({q}) ORDER BY id", ids)]
        ex = [dict(r) for r in c.execute(
            f"SELECT id, position_id, status FROM memecoin_exit_execution_attempts "
            f"WHERE position_id IN ({q}) ORDER BY id", ids)]
    finally:
        c.close()
    return {"pos": pos, "rs": rs, "ex": ex}


QUOTE_S = 1.5                          # ~0.66 exact quotes/s per open position
INSTANT_FROM = "2026-09-03T13:56:28Z"  # fda39f3a: first read at <= -50% sells
NOLOSS_FROM = "2026-09-04T19:51:34Z"   # 9d866c34: no loss sell in the first 30 s
INSTANT, GRACE_S, NOLOSS_S, CATA = 0.50, 20.0, 30.0, 0.80
BAD_DAY = "2026-09-09"


def ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


d = _pull(args.db, args.since)
rows = defaultdict(list)
for r in d["rs"]:
    if r["seam"] == "exit_decision":
        if r["unit_price"] is not None:           # 2/37783 rows carry no price and no qty
            rows[r["position_id"]].append(r)      # already ORDER BY id
fails = defaultdict(int)
for e in d["ex"]:
    if e["status"] in ("stuck", "unknown"):          # 'already_closing' is a dedupe guard
        fails[e["position_id"]] += 1

P = []
for p in d["pos"]:
    pol = (json.loads(p["funded_contract_json"] or "{}") or {}).get("policy") or {}
    path = rows.get(p["id"], [])
    if not path or not p["entry_price"] or not p["entry_notional"]:
        continue
    r = [x["unit_price"] / p["entry_price"] - 1.0 for x in path]
    bad = str(p["opened_at"])[:10] == BAD_DAY
    fill = ts(p["opened_recorded_at"] or p["opened_at"])
    if bad or fill is None:
        age = [i * QUOTE_S for i in range(len(path))]          # quote COUNT on the bad day
    else:
        age = []
        for x in path:
            t = ts(x["quoted_at"] or x["observed_at"])
            age.append((t - fill).total_seconds() if t else None)
    P.append(dict(id=p["id"], chain=p["network_id"], reason=p["exit_reason"], day=str(p["opened_at"])[:10],
                  bad=bad, opened=str(p["opened_at"]), notional=p["entry_notional"],
                  R=p["realized_pnl"] / p["entry_notional"], pnl=p["realized_pnl"], r=r, age=age,
                  D=int(pol.get("stop_debounce_sweeps") or 2), L=float(pol.get("stop_pct") or 35) / 100))


def fire(pp, L, D, *, as_of=True):
    """First path index at which the funded stop sells, or None. Mirrors memecoin_real_runner:
    no-loss clears the reason and releases the counter; instant sells on one exact read unless in
    grace; otherwise D consecutive quoted hits, and any quoted non-stop read releases."""
    noloss = (not as_of) or pp["opened"] >= NOLOSS_FROM
    instant = (not as_of) or pp["opened"] >= INSTANT_FROM
    hits = 0
    for i, (x, a) in enumerate(zip(pp["r"], pp["age"])):
        if x > -L:
            hits = 0
            continue
        if noloss and a is not None and a < NOLOSS_S:
            hits = 0
            continue
        in_grace = a is not None and a < GRACE_S and x > -CATA
        if instant and x <= -INSTANT and not in_grace:
            return i
        hits += 1
        if hits >= D:
            return i
    return None


# ---- 1. attribution on the actual b_stops ---------------------------------------------------
bs = [pp for pp in P if pp["reason"] == "b_stop"]
att, miss = [], []
for pp in bs:
    L, r = pp["L"], pp["r"]
    i1 = next((i for i, x in enumerate(r) if x <= -L), None)
    if i1 is None:
        miss.append(pp)
        continue
    ic = fire(pp, L, pp["D"])
    last = len(r) - 1
    ic_eff = ic if (ic is not None and ic <= last) else last
    n = pp["notional"]
    att.append(dict(pp=pp, gap=(-L - r[i1]) * n, confirm=(r[i1] - r[ic_eff]) * n,
                    other=(r[ic_eff] - r[last]) * n, exe=(r[last] - pp["R"]) * n,
                    total=(-L - pp["R"]) * n, q_confirm=ic_eff - i1, q_other=last - ic_eff,
                    model_late=ic is None, fails=fails.get(pp["id"], 0)))


def summ(label, A):
    if not A:
        print(f"  {label:<38} n=0")
        return
    s_ = lambda k: sum(a[k] for a in A)
    print(f"  {label:<38} n={len(A):<3} total ${s_('total'):8.2f} = gap ${s_('gap'):8.2f}"
          f" + confirm ${s_('confirm'):7.2f} + other ${s_('other'):7.2f} + exec ${s_('exe'):7.2f}")


for a in att:
    pp = a["pp"]
    a["agree"] = (pp["R"] - pp["r"][-1]) <= 0.05        # fill not >5 pts better than its trigger
print(f"cohort: {len(P)} closed funded positions with an exact path; b_stop {len(bs)};"
      f" b_stop whose path never read <= stop: {len(miss)}")
print("\n1. OVERSHOOT ATTRIBUTION (dollars below the stop; positive = worse than the stop)")
summ("all b_stops", att)
A = [a for a in att if a["agree"]]
X = [a for a in att if not a["agree"]]
summ("quote AGREES with fill (<=5 pts)", A)
summ("PHANTOM trigger (fill >5 pts better)", X)
print("  -- within the AGREE set --")
for D in sorted({a["pp"]["D"] for a in A}):
    summ(f"contract debounce D={D}", [a for a in A if a["pp"]["D"] == D])
for ch in ("solana", "4663"):
    summ(f"chain {ch}", [a for a in A if a["pp"]["chain"] == ch])
summ("clean days", [a for a in A if not a["pp"]["bad"]])
summ("2026-09-09 (quote-count clock)", [a for a in A if a["pp"]["bad"]])
qc = [a["q_confirm"] for a in A if a["pp"]["D"] >= 2]
print(f"  quotes first-breach -> modelled confirm (D>=2, agree): median {st.median(qc):.0f}, p90 "
      f"{st.quantiles(qc, n=10)[8]:.0f}, max {max(qc)}")
print(f"  b_stops with a stuck/unknown exit attempt: {sum(1 for a in att if a['fails'])}"
      f" (their 'other' ${sum(a['other'] for a in att if a['fails']):.2f})")
for ch in ("solana", "4663"):
    v = [a for a in att if a["pp"]["chain"] == ch]
    xs = [a for a in v if not a["agree"]]
    print(f"  {ch}: phantom triggers {len(xs)}/{len(v)}; sold above -30%: "
          f"{sum(1 for a in v if a['pp']['R'] > -0.30)}")

# ---- 2. replay, each variant against the NULL model (same simulator, contract rules) ---------
STOPISH = {"b_stop", "catastrophe"}


def outcome(pp, L, D):
    Dv = pp["D"] if D == "contract" else D
    k = fire(pp, L, Dv)
    last = len(pp["r"]) - 1
    if k is not None and k < last:
        return ("earlier", pp["r"][k])
    if k is not None:
        return ("same", pp["R"])
    return ("censored", None) if pp["reason"] in STOPISH else ("unchanged", pp["R"])


VARIANTS = [("35 / 1 sweep", 0.35, 1), ("35 / 2 sweeps", 0.35, 2), ("30 / contract D", 0.30, "contract"),
            ("30 / 1 sweep", 0.30, 1), ("40 / contract D", 0.40, "contract"), ("40 / 1 sweep", 0.40, 1)]


def replay_table(label, pool):
    print(f"\n2. REPLAY vs the NULL model (35 / contract D), sold at the triggering quote — {label}")
    base = {pp["id"]: outcome(pp, 0.35, "contract") for pp in pool}
    null_err = [(base[pp["id"]][1] - pp["R"]) * pp["notional"] for pp in pool
                if base[pp["id"]][0] == "earlier"]
    print(f"  null model vs reality: fires earlier on {len(null_err)}, $ {sum(null_err):+.2f}"
          f" (the simulator's own error on this pool)")
    for lab, L, D in VARIANTS:
        tot, n, cut, cutd, cen = 0.0, 0, 0, 0.0, 0
        for pp in pool:
            kb, vb = base[pp["id"]]
            kx, vx = outcome(pp, L, D)
            if vb is None or vx is None:
                cen += 1
                continue
            dl = (vx - vb) * pp["notional"]
            if abs(dl) > 1e-9:
                n += 1
                tot += dl
                if pp["reason"] not in STOPISH and kx == "earlier":
                    cut += 1
                    cutd += dl
        print(f"  {lab:<18} Δ ${tot:+8.2f} over {n:<3} changed  (non-stop exits cut: {cut}, ${cutd:+.2f})"
              f"  censored={cen}")


replay_table("ALL chains", P)
replay_table("solana", [pp for pp in P if pp["chain"] == "solana"])
replay_table("4663 (quote can be phantom: LOW confidence)", [pp for pp in P if pp["chain"] == "4663"])

print("\n3. PER DAY vs NULL, at-quote; the 2026-09-09 rows use the quote-count clock")
for lab, L, D in [("35 / 1 sweep", 0.35, 1), ("30 / contract D", 0.30, "contract")]:
    days = defaultdict(lambda: [0, 0.0, 0])
    for pp in P:
        kb, vb = outcome(pp, 0.35, "contract")
        kx, vx = outcome(pp, L, D)
        dd = days[pp["day"]]
        dd[0] += 1
        if vb is None or vx is None:
            dd[2] += 1
        else:
            dd[1] += (vx - vb) * pp["notional"]
    print(f"  {lab}:")
    for day in sorted(days):
        n, t, c = days[day]
        print(f"    {day}{' *clock' if day == BAD_DAY else '       '} n={n:<3} Δ ${t:+8.2f}  censored={c}")
