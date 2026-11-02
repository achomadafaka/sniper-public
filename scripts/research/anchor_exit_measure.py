#!/usr/bin/env python3
"""Measurement for the anchor-checked EVM exit spec (2026-10-01, operator via Alpha). Read-only.

Part 1 (ticks): every EVM exit_decision tick (memecoin_route_series, 30 d) whose winning whole-bag
value fell below RATIO x the previous tick's (the anchor, fresh <= 60 s); whether the winner was the
anchor's own venue (V4/V3 pool id, V2 path); and how soon a tick at >= RATIO x anchor came back.
Part 2 (sells): every EVM real sell fill against the last exit_decision tick before it.

    python3 scripts/research/anchor_exit_measure.py [ratio]
"""
from bisect import bisect_right

import json, sqlite3, sys, statistics
from collections import defaultdict, Counter
from datetime import datetime

DB = "file:/home/bigpapi/Desktop/Sniper/sniper_tracker.db?mode=ro"
SINCE = "2026-09-01T00:00:00"
FRESH_S, RATIO = 60.0, float(sys.argv[1]) if len(sys.argv) > 1 else 0.5
ts = lambda s: datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()

c = sqlite3.connect(DB, uri=True, timeout=30)
real = {pid: (net, pool) for pid, net, pool in c.execute(
    "SELECT id, network_id, lower(executed_pool) FROM memecoin_positions WHERE execution_mode='real' "
    "AND network_id NOT IN ('solana') AND opened_at >= ?", (SINCE,))}
rows = defaultdict(list)
for pid, net, obs, val, qty, unit, ev in c.execute(
        "SELECT position_id, network_id, observed_at, expected_usdc, open_qty, unit_price, quote_evidence_json "
        "FROM memecoin_route_series WHERE seam='exit_decision' AND network_id != 'solana' AND observed_at >= ? "
        "ORDER BY position_id, observed_at", (SINCE,)):
    if pid not in real or not unit or unit <= 0:
        continue
    try:
        e = json.loads(ev or "{}")
    except ValueError:
        e = {}
    r = e.get("route") or {}
    ident = r.get("pool_id") or r.get("pool") or (",".join(str(x).lower() for x in r.get("path") or ()) or None)
    venue = (str(r.get("protocol") or e.get("venue") or ""), str(ident or "").lower()) if (r or e) else ("?", "?")
    rows[pid].append((ts(obs), float(unit), venue))

gaps = []
ev_n = Counter(); same_venue = Counter(); other_venue = Counter(); unknown_venue = Counter()
rec = {k: Counter() for k in ("any_2", "any_10", "any_30", "anchor_2", "anchor_10", "anchor_30", "never_30")}
examples = []
ticks = Counter()
for pid, seq in rows.items():
    net = real[pid][0]
    ticks[net] += len(seq)
    for i in range(1, len(seq)):
        t0, u0, v0 = seq[i - 1]
        t1, u1, v1 = seq[i]
        gaps.append(t1 - t0)
        if t1 - t0 > FRESH_S or u1 >= RATIO * u0:
            continue
        ev_n[net] += 1
        same = v1 == v0
        if v0 == ("?", "?") or v1 == ("?", "?"):
            unknown_venue[net] += 1
        else:
            (same_venue if same else other_venue)[net] += 1
        hit_any = hit_anchor = None
        for t2, u2, v2 in seq[i + 1:]:
            if t2 - t1 > 30:
                break
            if u2 >= RATIO * u0:
                hit_any = hit_any if hit_any is not None else t2 - t1
                if v2 == v0:
                    hit_anchor = hit_anchor if hit_anchor is not None else t2 - t1
        for lim in (2, 10, 30):
            if hit_any is not None and hit_any <= lim:
                rec["any_%d" % lim][net] += 1
            if hit_anchor is not None and hit_anchor <= lim:
                rec["anchor_%d" % lim][net] += 1
        if hit_any is None:
            rec["never_30"][net] += 1
        if len(examples) < 20:
            examples.append((pid, net, round(u1 / u0, 4), v0[0], v0[1][:14], v1[0], v1[1][:14], same, hit_any, hit_anchor))
print("ratio", RATIO, "fresh_s", FRESH_S)
print("ticks by chain:", dict(ticks))
print("tick gap s p50/p90:", round(statistics.median(gaps), 2), round(sorted(gaps)[int(0.9 * len(gaps))], 2))
print("low ticks (< ratio x fresh anchor):", dict(ev_n))
print("  winner = anchor's own venue (answered low):", dict(same_venue))
print("  winner = another venue (anchor venue missed or lower):", dict(other_venue))
print("  venue unknown (no route evidence on the row):", dict(unknown_venue))
for k, v in rec.items():
    print("  recovered", k, dict(v))
for x in examples:
    print("  ex", x)

# --- Part 1b: the STICKY anchor (Gamma HIGH on 68864c224) ----------------------------------------
# The anchor is the last tick value that PASSED (>= RATIO x the anchor then), captured before the
# tick's own write; a tick below RATIO never moves it; it expires FRESH_S after it was set. This is
# what the runtime must read, because last_executable_price is overwritten by the trigger tick itself.
print("--- sticky anchor")
st_ticks, st_eps, st_runs, st_rec = Counter(), Counter(), defaultdict(list), defaultdict(list)
for pid, seq in rows.items():
    net = real[pid][0]
    a_t = a_u = a_v = None
    run = 0; run_start = None
    for t1, u1, v1 in seq:
        fresh = a_t is not None and t1 - a_t <= FRESH_S
        if fresh and u1 < RATIO * a_u:
            st_ticks[net] += 1
            if run == 0:
                st_eps[net] += 1; run_start = t1
            run += 1
            continue
        if run:
            st_runs[net].append((run, round(t1 - run_start, 1), "recovered" if fresh else "anchor_aged_out"))
            if fresh:
                st_rec[net].append(t1 - run_start)
            run = 0
        a_t, a_u, a_v = t1, u1, v1
    if run:
        st_runs[net].append((run, None, "open_at_end"))
print("low ticks:", dict(st_ticks), "episodes:", dict(st_eps))
for net, rs in st_runs.items():
    lengths = Counter(r[0] for r in rs)
    print(" ", net, "run lengths (ticks x episodes):", sorted(lengths.items()), "| ends:", Counter(r[2] for r in rs),
          "| long:", [r for r in rs if r[0] > 2])
for net, ts_ in st_rec.items():
    print(" ", net, "recovered within 2/10/30/60 s:", [sum(x <= lim for x in ts_) for lim in (2, 10, 30, 60)], "of", len(ts_))

# --- Part 1c: the simplified rule (Alpha 2026-10-01): hold up to HOLD_S, sell the first tick that is
# back >= RATIO x anchor, else the largest answer at HOLD_S. Per sticky episode: caught (back in time),
# else the value at the bound against the value at the first low tick (the cost of the delay).
print("--- hold_s")
episodes = []
for pid, seq in rows.items():
    net = real[pid][0]
    a_t = a_u = None
    i = 0
    while i < len(seq):
        t1, u1, _v = seq[i]
        if a_t is not None and t1 - a_t <= FRESH_S and u1 < RATIO * a_u:
            j = i
            while j + 1 < len(seq) and seq[j + 1][1] < RATIO * a_u and seq[j + 1][0] - a_t <= FRESH_S:
                j += 1
            episodes.append((pid, net, a_u, seq[i:], t1))
            i = j + 1
            continue
        a_t, a_u = t1, u1
        i += 1
for hold in (6, 10, 20):
    caught = Counter(); delayed = Counter(); cost = defaultdict(list); open_ = Counter()
    for pid, net, a_u, tail, t0 in episodes:
        back = next((t for t, u, _ in tail[1:] if t - t0 <= hold and u >= RATIO * a_u), None)
        if back is not None:
            caught[net] += 1
            continue
        within = [(t, u) for t, u, _ in tail if t - t0 <= hold]
        delayed[net] += 1
        if tail[-1][0] - t0 < hold:
            open_[net] += 1                   # the series ends (the position closed) inside the window:
            continue                          # the value at the bound was never observed
        u_end = within[-1][1]                 # the answer at the bound tick, which the rule sells
        cost[net].append((pid, round(u_end / tail[0][1] - 1.0, 3), round(within[-1][0] - t0, 1)))
    print("HOLD_S", hold, "caught", dict(caught), "sold at the bound", dict(delayed),
          "unobserved (position sold inside the window)", dict(open_), "| (pid, value at the bound vs first low tick, s):",
          {k: v for k, v in cost.items()})

# --- Part 1d: the FINAL rule (Alpha + Gamma, 2026-10-01). Sticky anchor = the last passing tick; no
# wall-clock expiry. A low tick (< RATIO x anchor) opens a hold; the first tick back >= RATIO x anchor
# closes it (caught: the dislocation was not sold); otherwise the first tick at or after HOLD_S, with
# >= 2 low ticks seen, re-bases the anchor to it (the bound: the rule sells there). Per episode: the hold
# save for a catch (value at recovery - value at the first low tick) and the crash cost for a bound
# (value at the bound tick - value at the first low tick), in USD of the whole bag.
print("--- final rule, per episode")
vals = defaultdict(list)
for pid, net, obs, val, unit in c.execute(
        "SELECT position_id, network_id, observed_at, expected_usdc, unit_price FROM memecoin_route_series "
        "WHERE seam='exit_decision' AND network_id != 'solana' AND observed_at >= ? ORDER BY position_id, observed_at",
        (SINCE,)):
    if pid in real and unit and unit > 0:
        vals[pid].append((ts(obs), float(unit), float(val)))
for hold in (6, 10, 20):
    summary = Counter(); save = defaultdict(float); crash = defaultdict(float); lines = []
    for pid, seq in vals.items():
        net = real[pid][0]
        anchor = None; start = None; lows = 0
        for t, u, v in seq:
            if anchor is None:
                anchor = (u, v); continue
            if start is None:
                if u >= RATIO * anchor[0]:
                    anchor = (u, v); continue
                start, first_v, lows = t, v, 1
                continue
            if u >= RATIO * anchor[0]:
                summary[(net, "caught")] += 1; save[net] += v - first_v
                lines.append((net, pid, "caught", round(t - start, 1), round(v - first_v, 2)))
                anchor = (u, v); start = None; continue
            lows += 1
            if t - start >= hold and lows >= 2:
                summary[(net, "bound")] += 1; crash[net] += v - first_v
                lines.append((net, pid, "bound", round(t - start, 1), round(v - first_v, 2)))
                anchor = (u, v); start = None
        if start is not None:
            summary[(net, "open_at_end")] += 1
            lines.append((net, pid, "open_at_end", round(seq[-1][0] - start, 1), None))
    print("HOLD_S", hold, dict(summary), "| hold save $", {k: round(v, 2) for k, v in save.items()},
          "| crash cost $", {k: round(v, 2) for k, v in crash.items()})
    for x in lines:
        print("   ", x)

print('--- sells')

print("exit fill kinds:", c.execute("SELECT f.kind, f.fill_source, COUNT(*) FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id WHERE p.execution_mode='real' AND p.network_id!='solana' AND f.kind!='entry' AND f.created_at>=? GROUP BY 1,2", (SINCE,)).fetchall())
series = defaultdict(list)
for pid, obs, unit in c.execute("SELECT position_id, observed_at, unit_price FROM memecoin_route_series WHERE seam='exit_decision' AND network_id!='solana' AND observed_at>=? ORDER BY position_id, observed_at", (SINCE,)):
    if unit and unit > 0:
        series[pid].append((ts(obs), float(unit)))
ratios = defaultdict(list); stale = Counter(); none = Counter(); low = []
for pid, net, kind, created, notional, qty, reason in c.execute(
        "SELECT f.position_id, p.network_id, f.kind, f.created_at, f.notional, f.qty, p.exit_reason FROM memecoin_fills f "
        "JOIN memecoin_positions p ON p.id=f.position_id WHERE p.execution_mode='real' AND p.network_id!='solana' "
        "AND f.kind!='entry' AND f.created_at>=?", (SINCE,)):
    if not qty or qty <= 0 or notional is None:
        none[net] += 1; continue
    s = series.get(pid, [])
    t = ts(created)
    k = bisect_right([x[0] for x in s], t) - 1
    if k < 0:
        none[net] += 1; continue
    t0, u0 = s[k]
    if t - t0 > 60 + 30:          # the decision tick precedes the fill by the send+confirm time
        stale[net] += 1; continue
    r = (float(notional) / float(qty)) / u0
    ratios[net].append(r)
    if r < 0.5:
        low.append((pid, net, kind, reason, round(r, 3), round(t - t0, 1)))
for net, rs in ratios.items():
    rs.sort()
    print(net, "sells", len(rs), "median paid/anchor", round(statistics.median(rs), 3), "p5", round(rs[int(0.05 * len(rs))], 3),
          "<0.5:", sum(r < 0.5 for r in rs), "<0.8:", sum(r < 0.8 for r in rs))
print("no anchor:", dict(none), "stale anchor:", dict(stale))
for x in low: print("  low", x)
