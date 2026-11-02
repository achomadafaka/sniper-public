#!/usr/bin/env python3
"""Does on-chain order flow predict our funded Solana exits early enough to pay?

Operator question 2026-09-28: can we see sells (and buys) as they land and exit before the stop
loss, or take profit faster, instead of waiting for the price rail? Research only: read-only on
the ledger and data/memecoin_sol_tape.db, nothing touches the funded path.

Instrument. For every closed funded Solana position the tape followed:
- the pool is the coin vault from the tape; our own entry fill (sol_position_pools.resolution_tx)
  names our wallet as its maker, and our exit is that maker's first sell on the vault after entry;
- spot is the vault reserve ratio (quote/coin); for a pump curve, (30 SOL + quote)/(coin + 73M)
  virtual reserves. A pool is used only when our own entry fill priced within 15% of that spot,
  which drops concentrated-liquidity pools where the vault ratio is not a price;
- actual exit spot is the state just BEFORE our sell (post-state minus our own deltas);
- a rule fires at slot s on the trailing W-slot flow of OTHER makers; its exit lands at s+L and
  gets the spot at that slot. Both legs are spot, so neither side pays impact in the comparison.

Rules (each gated on current PnL vs entry spot):
- sell_flow: others' net sells in (s-W, s] >= theta of quote reserve, while pnl <= gate;
- buy_flow (control): same threshold on net BUYS, same gate. If it scores like sell_flow, the
  flow carries no information and the gain is only "leave losers earlier";
- plain (control): pnl <= gate alone, no flow. A flow rule must beat this to be worth building;
- tp_burst: others' net buys >= theta while pnl >= gate: sell into a buy burst.
Positions are split by open time: thresholds are picked on the first half, reported on the second.

The pull tape (data/memecoin_sol_tape.db) holds swaps for fewer than 80 of ~1,110 funded positions
(measured 2026-09-28), so `backfill` rebuilds each position's pool history from the chain first:
Helius getTransactionsForAddress on the position's coin vault over [open-5 s, close+30 s], on the
research key only (funded_exit_truth.rpc: refuses a funded key, stops on quota or repeated 429),
decoded with the tape's own vault_rows / pump_curve_rows into BACKFILL (same pool_ticks shape plus
the in-slot transaction index). Resumable by position id.

    python3 scripts/research/flow_exit_study.py backfill [--limit N]
    python3 scripts/research/flow_exit_study.py [--latency-slots 5] [--json out.json]
    python3 scripts/research/flow_exit_study.py --selftest
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import bisect
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
LEDGER = REPO / "sniper_tracker.db"
TAPE = REPO / "data" / "memecoin_sol_tape.db"      # read only for pool bindings
BACKFILL = REPO / "data" / "research_flow_exit_backfill.db"
MAX_PAGES = 20           # 2,000 transactions per window
WINDOW_S = 180           # operator 2026-09-28: a few minutes per coin, the 200 most recent positions.
RECENT_N = 200           # The window is the LAST 180 s before our exit (whole hold if shorter), so
                         # every stop keeps its run-up; the entry tx is read alone for the entry spot.
CURVE_VSOL = 30_000_000_000        # pump curve virtual SOL (lamports)
CURVE_VTOK = 73_000_000_000_000    # virtual token minus the curve account's unsold 1B (atomic)
FIT_TOL = 0.15
# 'vault': reserve-ratio spot, constant-product pools only (FIT_TOL drops the rest: 18 of the first
# 38 recent positions sat on concentrated-liquidity pools, fill/spot 1.15-2.05x). 'trade': each
# OTHER trader's own execution price |quote|/|coin|, any venue; entry is our fill, exit is the last
# other trade before our sell. Swaps under MIN_TRADE_Q atomic quote units are dust and ignored.
PRICE_MODE = "vault"
MIN_TRADE_Q = 1_000_000


def spot(kind, coin, quote):
    if coin is None or quote is None:
        return None
    if kind.startswith("sol_curve"):
        coin, quote = coin + CURVE_VTOK, quote + CURVE_VSOL
    return quote / coin if coin > 0 and quote > 0 else None


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def backfill_db():
    b = sqlite3.connect(BACKFILL, timeout=10)
    b.executescript("""
        CREATE TABLE IF NOT EXISTS pool_ticks(position_id INTEGER, chain_id TEXT, pool TEXT, kind TEXT,
            block_no INTEGER, tx_index INTEGER, tx_sig TEXT, log_index INTEGER, maker TEXT,
            amount0 TEXT, amount1 TEXT, block_time INTEGER,
            UNIQUE(position_id, tx_sig, log_index, kind));
        CREATE INDEX IF NOT EXISTS idx_bf_pos ON pool_ticks(position_id, block_no, tx_index);
        CREATE TABLE IF NOT EXISTS done(position_id INTEGER PRIMARY KEY, status TEXT, n_tx INTEGER,
            complete INTEGER, at TEXT);""")
    return b


def expand_lookup_keys(tx):
    """Raw `json` keeps v0 lookup-table keys in meta.loadedAddresses; the tape decoders index
    accountKeys as jsonParsed does (static, then loaded writable, then loaded readonly). Without
    this, 30 of 30 sampled transactions on one pool hid its vault (2026-09-28)."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    la = (tx.get("meta") or {}).get("loadedAddresses") or {}
    if la and not tx.get("_expanded"):
        msg["accountKeys"] = list(msg.get("accountKeys") or []) + list(la.get("writable") or []) + \
            list(la.get("readonly") or [])
        tx["_expanded"] = True
    return tx


def backfill(limit=None):
    """Chain history of each position's pool over its hold window, into BACKFILL. Resumable."""
    import funded_exit_truth as fet
    import wallet_crowd_dump_scoreboard as sb
    from datetime import datetime, timezone
    from sniper.services.memecoin_solana_amounts import vault_rows, pump_curve_rows
    fet.SPACING_S = 0.5
    t, b = ro(TAPE), backfill_db()
    binds = {pid: (pool, venue) for pid, pool, venue in
             t.execute("SELECT position_id, pool, venue_kind FROM sol_position_pools")}
    vaults = {pool: (mint, qv) for pool, mint, qv in
              t.execute("SELECT pool, mint, quote_vault FROM sol_pool_vaults")}
    seen = {r[0] for r in b.execute("SELECT position_id FROM done")}
    c = ro(LEDGER)
    todo = [r for r in c.execute(
        "SELECT id, mint, opened_at, closed_at FROM memecoin_positions WHERE execution_mode='real' "
        "AND network_id='solana' AND is_open=0 AND entry_notional>0 "
        "ORDER BY opened_at DESC LIMIT ?", (RECENT_N,)).fetchall() if r[0] not in seen]
    entry_sigs = dict(t.execute("SELECT position_id, resolution_tx FROM sol_position_pools"))
    ep = lambda s: int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    for n, (pid, mint, opened, closed) in enumerate(todo[:limit] if limit else todo):
        bind = binds.get(pid)
        if not bind or bind[0] not in vaults or vaults[bind[0]][0] != mint:
            b.execute("INSERT OR REPLACE INTO done VALUES(?,?,?,?,?)", (pid, "no_binding", 0, 0,
                      datetime.now(timezone.utc).isoformat()))
            b.commit()
            continue
        vault, venue = bind
        qv = vaults[vault][1]
        try:
            txs, complete = sb.gtfa(vault, {"blockTime": {"gte": max(ep(opened) - 5, ep(closed) - WINDOW_S),
                                                    "lte": ep(closed) + 15}},
                                    limit=100, pages=MAX_PAGES)
            esig = entry_sigs.get(pid)
            if esig and not any(((x.get("transaction") or {}).get("signatures") or [None])[0] == esig
                                for x in txs):
                etx = fet.rpc("getTransaction", [esig, {"encoding": "json", "commitment": "confirmed",
                                                         "maxSupportedTransactionVersion": 1}])
                if etx:
                    txs = [etx] + txs
        except fet.Stop as exc:
            print(f"STOP at position {pid}: {exc}", flush=True)
            break
        rows = []
        for tx in txs:
            expand_lookup_keys(tx)
            sig = ((tx.get("transaction") or {}).get("signatures") or [None])[0]
            kw = dict(slot=tx.get("slot"), signature=sig)
            got = (pump_curve_rows(tx, mint, vault, qv, **kw) if venue == "pump_curve"
                   else vault_rows(tx, mint, vault, quote_vault=qv, **kw))
            for r in got:
                rows.append((pid, "solana", vault, r["kind"], r["block_no"], tx.get("transactionIndex"),
                             sig, r["log_index"], r["maker"], r["amount0"], r["amount1"], tx.get("blockTime")))
        b.executemany("INSERT OR IGNORE INTO pool_ticks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        b.execute("INSERT OR REPLACE INTO done VALUES(?,?,?,?,?)",
                  (pid, "ok", len(txs), int(complete), datetime.now(timezone.utc).isoformat()))
        b.commit()
        if n % 25 == 0:
            print(f"{n}/{len(todo)} pos={pid} txs={len(txs)} complete={complete} rows={len(rows)} "
                  f"calls={sb.CALLS[0]} 429s={fet.RATE_429[0]}", flush=True)


def load_positions():
    c = ro(LEDGER)
    rows = c.execute(
        "SELECT id, mint, opened_at, exit_reason, entry_notional, realized_pnl FROM memecoin_positions "
        "WHERE execution_mode='real' AND network_id='solana' AND is_open=0 "
        "AND opened_at>='2026-07-26' AND entry_notional>0 ORDER BY opened_at").fetchall()
    return [dict(zip(("id", "mint", "opened_at", "reason", "notional", "pnl"), r)) for r in rows]


def build_case(t, pos, entry_sig):
    """One position's tape: our entry/exit slots and spots, the per-slot spot and others' flow."""
    ours = t.execute("SELECT pool, maker, block_no, amount0, amount1, kind FROM pool_ticks "
                     "WHERE position_id=? AND tx_sig=? AND kind IN ('sol_swap','sol_curve_swap')",
                     (pos["id"], entry_sig)).fetchall()
    ours = [r for r in ours if r[4] is not None and int(r[4]) > 0]
    if len(ours) != 1:
        return None, "entry_not_on_tape"
    vault, me, s0, c_in, q_in, _ = ours[0]
    rows = t.execute("SELECT kind, block_no, tx_sig, maker, amount0, amount1, tx_index FROM pool_ticks "
                     "WHERE position_id=? AND block_no>=? ORDER BY block_no, tx_index",
                     (pos["id"], s0)).fetchall()
    sync = {}                      # tx_sig -> (kind, coin_post, quote_post)
    swaps = []                     # (slot, sig, maker, coin, quote, kind)
    for kind, slot, sig, maker, a0, a1, _id in rows:
        if a0 is None or a1 is None:
            continue
        if kind in ("sol_sync", "sol_curve_sync"):
            sync[sig] = (kind, int(a0), int(a1), slot)
        elif kind in ("sol_swap", "sol_curve_swap"):
            swaps.append((slot, sig, maker, int(a0), int(a1), kind))
    exit_sw = next((s for s in swaps if s[2] == me and s[4] < 0 and s[0] >= s0), None)
    if exit_sw is None:
        return None, "exit_not_on_tape"
    s_exit = exit_sw[0]

    def pre_spot(sw):
        st = sync.get(sw[1])
        if not st:
            return None
        return spot(st[0], st[1] - sw[3], st[2] - sw[4])

    entry_pre = pre_spot(("", entry_sig, me, int(c_in), int(q_in)))
    exit_pre = pre_spot(exit_sw)
    if not entry_pre or not exit_pre:
        return None, "no_sync_for_own_tx"
    fill = int(q_in) / -int(c_in) if int(c_in) < 0 else None
    if ours[0][5] == "sol_curve_swap" and fill:
        pass                       # curve fill includes the fee; tolerance covers it
    if PRICE_MODE == "trade":
        if not fill:
            return None, "no_fill_price"
        end, last_before_exit = {}, None
        for sw in swaps:
            slot, sig, maker, c, q, kind = sw
            if sw is exit_sw:
                break
            if maker == me or not c or abs(q) < MIN_TRADE_Q or not (s0 <= slot):
                continue
            st = sync.get(sig)
            res = (st[2] + (CURVE_VSOL if kind.startswith("sol_curve") else 0)) if st else None
            if res:
                end[slot] = (abs(q) / abs(c), res)
                last_before_exit = abs(q) / abs(c)
        if not last_before_exit:
            return None, "no_other_trades"
        slots = sorted(end)
        flow = defaultdict(int)
        for slot, sig, maker, c, q, kind in swaps:
            if maker != me and s0 < slot < s_exit:
                flow[slot] += q
        return {"id": pos["id"], "reason": pos["reason"], "notional": pos["notional"], "pnl": pos["pnl"],
                "opened_at": pos["opened_at"], "s0": s0, "s_exit": s_exit, "entry": fill,
                "exit": last_before_exit, "slots": slots, "state": [end[x] for x in slots],
                "flow": dict(flow)}, None
    if not fill or abs(fill / entry_pre - 1) > FIT_TOL:
        return None, "spot_not_a_price"
    # per-slot END state: rows arrive in (slot, transactionIndex) order, so the last post-state wins
    end = {}
    for sig, (kind, cp, qp, slot) in sync.items():
        if s0 <= slot <= s_exit + 60:
            p = spot(kind, cp, qp)
            if p:
                end[slot] = (p, qp if not kind.startswith("sol_curve") else qp + CURVE_VSOL)
    slots = sorted(end)
    state = [end[s] for s in slots]
    flow = defaultdict(int)
    for slot, sig, maker, c, q, kind in swaps:
        if maker != me and s0 < slot < s_exit:
            flow[slot] += q
    return {"id": pos["id"], "reason": pos["reason"], "notional": pos["notional"], "pnl": pos["pnl"],
            "opened_at": pos["opened_at"], "s0": s0, "s_exit": s_exit, "entry": entry_pre,
            "exit": exit_pre, "slots": slots, "state": state, "flow": dict(flow)}, None


def state_at(case, slot):
    i = bisect.bisect_right(case["slots"], slot) - 1
    return case["state"][i] if i >= 0 else None


def simulate(case, rule, theta, window, gate, lat):
    """Return (fired_slot or None, delta_return vs actual exit)."""
    flow_slots = sorted(case["flow"])
    cands = flow_slots if rule != "plain" else [s for s in case["slots"] if s > case["s0"]]
    for s in cands:
        if s >= case["s_exit"]:
            break
        st = state_at(case, s)
        if not st:
            continue
        price, reserve = st
        pnl = price / case["entry"] - 1
        if rule == "plain":
            hit = pnl <= gate
        else:
            net = sum(v for k, v in case["flow"].items() if s - window < k <= s)
            if rule == "sell_flow":
                hit = pnl <= gate and -net >= theta * reserve
            elif rule == "buy_flow":
                hit = pnl <= gate and net >= theta * reserve
            else:                                   # tp_burst
                hit = pnl >= gate and net >= theta * reserve
        if hit:
            land = s + lat
            if land >= case["s_exit"]:
                return s, 0.0
            p2 = state_at(case, land)
            return s, (p2[0] - case["exit"]) / case["entry"] if p2 else 0.0
    return None, 0.0


def score(cases, rule, theta, window, gate, lat):
    tot = 0.0
    fired = 0
    by = defaultdict(lambda: [0, 0.0])
    for c in cases:
        s, d = simulate(c, rule, theta, window, gate, lat)
        usd = d * c["notional"]
        tot += usd
        if s is not None:
            fired += 1
            b = by["b_stop" if c["reason"] == "b_stop" else "real_tp" if c["reason"] == "real_tp" else "other"]
            b[0] += 1
            b[1] += usd
    return {"usd": round(tot, 1), "fired": fired, "n": len(cases),
            "by_reason": {k: [v[0], round(v[1], 1)] for k, v in by.items()}}


GRID = {
    "sell_flow": [(th, w, g) for th in (0.01, 0.02, 0.04, 0.08) for w in (1, 3, 8, 20)
                  for g in (0.0, -0.05, -0.10, -0.15)],
    "buy_flow": None,   # uses sell_flow's chosen params
    "plain": [(0, 0, g) for g in (-0.05, -0.10, -0.15, -0.20)],
    "tp_burst": [(th, w, g) for th in (0.02, 0.04, 0.08) for w in (1, 3, 8)
                 for g in (0.10, 0.20, 0.30)],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--latency-slots", type=int, default=5)
    ap.add_argument("--json")
    ap.add_argument("--price", choices=("vault", "trade"), default="vault")
    a = ap.parse_args()
    global PRICE_MODE
    PRICE_MODE = a.price
    sigs = dict(ro(TAPE).execute("SELECT position_id, resolution_tx FROM sol_position_pools").fetchall())
    t = ro(BACKFILL)
    cases, drop = [], defaultdict(int)
    fetched = {r[0] for r in t.execute("SELECT position_id FROM done")}
    for p in (p for p in load_positions() if p["id"] in fetched):
        sig = sigs.get(p["id"])
        if not sig:
            drop["no_pool_row"] += 1
            continue
        c, why = build_case(t, p, sig)
        if c:
            cases.append(c)
        else:
            drop[why] += 1
    print(f"cases={len(cases)} dropped={dict(drop)}")
    rs = defaultdict(lambda: [0, 0.0])
    for c in cases:
        rs[c["reason"]][0] += 1
        rs[c["reason"]][1] += c["pnl"] or 0
    print("sample by exit reason:", {k: [v[0], round(v[1], 1)] for k, v in rs.items()})
    half = len(cases) // 2
    fit, test = cases[:half], cases[half:]
    print(f"fit n={len(fit)} ({fit[0]['opened_at'][:10]}..{fit[-1]['opened_at'][:10]}) "
          f"test n={len(test)} (..{test[-1]['opened_at'][:10]})  latency={a.latency_slots} slots")
    out = {}
    for rule in ("sell_flow", "plain", "tp_burst"):
        best = max(GRID[rule], key=lambda p: score(fit, rule, *p, a.latency_slots)["usd"])
        out[rule] = {"params": best, "fit": score(fit, rule, *best, a.latency_slots),
                     "test": score(test, rule, *best, a.latency_slots),
                     "all": score(cases, rule, *best, a.latency_slots)}
        if rule == "sell_flow":
            out["buy_flow"] = {"params": best, "fit": score(fit, "buy_flow", *best, a.latency_slots),
                               "test": score(test, "buy_flow", *best, a.latency_slots),
                               "all": score(cases, "buy_flow", *best, a.latency_slots)}
    # the whole sell_flow grid on ALL cases, to see if any setting is robustly positive
    grid = sorted(((score(cases, "sell_flow", *p, a.latency_slots)["usd"], p) for p in GRID["sell_flow"]),
                  reverse=True)
    for k, v in out.items():
        print(f"{k:10s} params(theta,window,gate)={v['params']}  fit={v['fit']}  test={v['test']}")
    print("sell_flow grid on all, top 5:", grid[:5], " bottom 3:", grid[-3:])
    pos = sum(1 for u, _ in grid if u > 0)
    print(f"sell_flow grid cells positive: {pos}/{len(grid)}")
    if a.json:
        Path(a.json).write_text(json.dumps({"cases": len(cases), "drop": drop, "rules": out,
                                            "grid": grid}, default=str))


def selftest():
    base = {"id": 1, "reason": "b_stop", "notional": 100.0, "pnl": -20, "opened_at": "x",
            "s0": 0, "s_exit": 20, "entry": 1.0, "exit": 0.8,
            "slots": [0, 5, 10, 15], "state": [(1.0, 100), (0.95, 100), (0.9, 100), (0.85, 100)],
            "flow": {5: -10}}
    s, d = simulate(base, "sell_flow", 0.05, 1, 0.0, 5)       # fires at 5, lands at 10 -> 0.9
    assert s == 5 and abs(d - 0.1) < 1e-9, (s, d)
    s, d = simulate(base, "buy_flow", 0.05, 1, 0.0, 5)        # no buys -> no fire
    assert s is None and d == 0.0
    s, d = simulate(base, "sell_flow", 0.05, 1, 0.0, 16)      # lands after actual exit -> no change
    assert s == 5 and d == 0.0
    s, d = simulate(base, "sell_flow", 0.20, 1, 0.0, 5)       # below threshold -> no fire
    assert s is None
    s, d = simulate(base, "plain", 0, 0, -0.04, 5)            # pnl -5% at slot 5 -> lands at 10
    assert s == 5 and abs(d - 0.1) < 1e-9
    assert abs(spot("sol_swap", 100, 50) - 0.5) < 1e-12
    tx = {"transaction": {"message": {"accountKeys": ["a"]}},
          "meta": {"loadedAddresses": {"writable": ["w"], "readonly": ["r"]}}}
    assert expand_lookup_keys(expand_lookup_keys(tx))["transaction"]["message"]["accountKeys"] == ["a", "w", "r"]
    print("selftest ok")



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif sys.argv[1:2] == ["backfill"]:
        lim = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
        backfill(lim)
    else:
        main()
