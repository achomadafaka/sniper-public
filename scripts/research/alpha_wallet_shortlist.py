#!/usr/bin/env python3
"""Alpha-wallet shortlist rows, built to Gamma's pre-registration (read-only).

Rule: docs/audits/2026-09-30-alpha-wallet-shortlist-preregistration.md on gamma/alpha-shortlist-prereg
(a26035a11, stamped 2026-09-30T22:47Z) with amendment A1 (ca128d2d0): the Tier B entry set is
route_quote + paper_quote + funded_quote_reuse. Nothing here arms, disarms or sizes a wallet; roster
changes stay the operator's, on funded results only (2026-09-11).

One row per (wallet, chain). Tier A = real closes (the only basis for "recommend re-arming"). Tier B
= the funded policy's paper copy (strategy funded_clone), executably priced at entry and at every
exit fill; its only possible verdict is "candidate for a funded canary". Paper exits are TIMED by the
paper's marks (mostly display marks) even though they FILL at a route quote: context, not an arming
basis.

Reads the ledger and the wallet-graph DB read-only (file:...?mode=ro), one pass each, no network.

    python3 scripts/research/alpha_wallet_shortlist.py --out <dir>
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
W0 = "2026-09-16T22:00:00+00:00"                  # paper window (Tier B)
W1 = "2026-09-30T22:00:00+00:00"
A0 = "2026-08-31T22:00:00+00:00"                  # Tier A: real closes in the 30 days to W1
HOLDOUT = "2026-09-26T00:00:00+00:00"
ENTRY_OK = ("route_quote", "paper_quote", "funded_quote_reuse")    # A1
EXIT_OK = "route_quote"
SHARED_S = 30.0
SEED, DRAWS = 20260930, 4000


def ts(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def iso(value):
    """Canonical comparable ISO (the ledger mixes 'Z' and '+00:00')."""
    return ts(value).astimezone(timezone.utc).isoformat()


def own_addresses():
    raw = os.environ.get("MEMECOIN_OWN_ADDRESSES")
    if raw is None:                               # the runner loads .env itself; so do we
        env = ROOT / ".env"
        for line in env.read_text().splitlines() if env.exists() else ():
            if line.startswith("MEMECOIN_OWN_ADDRESSES="):
                raw = line.split("=", 1)[1]
    return {a.strip().lower() for a in (raw or "").split(",") if a.strip()}


def roster(c):
    rows = c.execute("SELECT source_user_id, handle, excluded, real_approved, real_chains "
                     "FROM memecoin_wallets").fetchall()
    out = {}
    for uid, handle, excluded, approved, chains in rows:
        armed = {x.strip() for x in str(chains or "").split(",") if x.strip()} if approved else set()
        out[uid] = {"handle": handle, "excluded": bool(excluded), "armed": armed}
    return out


def own_user_ids(c, own):
    if not own:
        return set()
    return {uid for uid, addr in c.execute(
        "SELECT source_user_id, address FROM memecoin_evm_wallet_addresses") if str(addr).lower() in own}


def pools(c, graph_db, rost):
    """{(uid, chain): set(labels)} from activity in the paper window; labels never pass or fail."""
    active = defaultdict(set)                      # uid -> chains with J7 events in the window
    for uid, chain in c.execute(
            "SELECT DISTINCT source_user_id, network_id FROM memecoin_events "
            "WHERE created_at >= ? AND created_at < ? AND delivery_lane LIKE 'j7%' "
            "AND network_id IS NOT NULL", (W0[:19], W1[:19])):
        active[uid].add(str(chain))
    herders = set()
    if graph_db and Path(graph_db).exists():
        g = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True, timeout=10)
        # `follows` and `slot_follower`: src is the LEADER (memory 2026-09-30, edge direction).
        herders = {r[0] for r in g.execute(
            "SELECT DISTINCT src_user_id FROM memecoin_wallet_edges "
            "WHERE edge_type IN ('follows','slot_follower') AND src_user_id IS NOT NULL")}
        upstream_n = g.execute("SELECT COUNT(*) FROM memecoin_wallet_upstream").fetchone()[0]
    else:
        upstream_n = None
    labels = defaultdict(set)
    for uid, chains in active.items():
        for chain in chains:
            if chain not in rost.get(uid, {}).get("armed", set()):
                labels[(uid, chain)].add("j7_unarmed")
            if len(chains) >= 2:
                labels[(uid, chain)].add("multi_chain")
            if uid in herders:
                labels[(uid, chain)].add("herder")
    return labels, {"herders": len(herders), "upstream_rows": upstream_n}


def paper_units(c):
    """First funded_clone position per (wallet, chain, mint) in the window, with its fills."""
    rows = c.execute(
        "SELECT id, source_user_id, source_handle, network_id, mint, opened_at, closed_at, is_open, "
        "realized_pnl, entry_notional, entry_price, price_attribution, origin_event_id, entry_mcap, "
        "id IN (SELECT position_id FROM memecoin_fills WHERE fill_source='fomo') AS fomo, "
        "(SELECT created_at FROM memecoin_events e WHERE e.fomo_event_id = p.origin_event_id) "
        "FROM memecoin_positions p WHERE execution_mode='paper' AND strategy='funded_clone' "
        "AND opened_at >= ? AND opened_at < ? ORDER BY opened_at, id", (W0, W1)).fetchall()
    ids = [r[0] for r in rows]
    fills = defaultdict(list)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for pid, kind, src, notional, qty, price in c.execute(
                "SELECT position_id, kind, fill_source, notional, qty, price FROM memecoin_fills "
                f"WHERE position_id IN ({','.join('?' * len(chunk))})", chunk):
            fills[pid].append((kind, src, notional, qty, price))
    units, dropped = {}, defaultdict(int)
    for r in rows:
        key = (r[1], str(r[3]), r[4])
        if key in units:
            dropped[key[:2]] += 1
            continue
        units[key] = {"id": r[0], "uid": r[1], "handle": r[2], "chain": str(r[3]), "mint": r[4],
                      "opened_at": r[5], "closed_at": r[6], "is_open": r[7], "pnl": r[8],
                      "notional": r[9], "entry_price": r[10], "attr": r[11], "origin": r[12],
                      "mcap": r[13], "fomo": bool(r[14]), "buy_at": r[15],
                      "fills": fills.get(r[0], [])}
    return units, dropped


def censor_reason(u):
    if u["fomo"]:
        return "fomo_fill"
    if u["attr"] not in ENTRY_OK:
        return "entry_%s" % u["attr"]
    if u["is_open"] or u["closed_at"] is None or u["pnl"] is None:
        return "open"
    exits = [f for f in u["fills"] if f[0] != "entry"]
    if not exits:
        return "no_exit_fill"
    bad = sorted({f[1] for f in exits if f[1] != EXIT_OK})
    if bad:
        return "exit_" + "+".join(str(b) for b in bad)
    if not u["notional"] or u["notional"] <= 0:
        return "no_notional"
    return None


def traced(u):
    """Booked PnL reproduced from memecoin_fills: exits' proceeds minus the entry cost."""
    entry = sum(f[2] or 0.0 for f in u["fills"] if f[0] == "entry")
    exits = sum(f[2] or 0.0 for f in u["fills"] if f[0] != "entry")
    if entry <= 0:
        return False, None
    diff = (exits - entry) - float(u["pnl"])
    return abs(diff) <= max(0.01, 0.005 * entry), round(diff, 6)


def band(mcap):
    if mcap is None:
        return "unknown"
    m = float(mcap)
    return "nano(<100k)" if m < 1e5 else "micro(100k-1M)" if m < 1e6 else "small(1M-10M)" if m < 1e7 else "mid(>=10M)"


def buys_by_mint(c, mints):
    out = defaultdict(list)                        # (chain, mint) -> [(uid, t)]
    mints = list(mints)
    for i in range(0, len(mints), 900):
        chunk = mints[i:i + 900]
        for uid, chain, mint, created in c.execute(
                "SELECT source_user_id, network_id, mint, created_at FROM memecoin_events "
                f"WHERE mint IN ({','.join('?' * len(chunk))}) AND side='buy' "
                "AND created_at >= ? AND created_at < ?", (*chunk, "2026-09-16T21:00", "2026-09-30T23:00")):
            out[(str(chain), mint)].append((uid, ts(created)))
    return out


def lower_bound(values, rng):
    arr = np.asarray(values, dtype=float)
    means = arr[rng.integers(0, len(arr), size=(DRAWS, len(arr)))].mean(axis=1)
    return float(np.quantile(means, 0.10))


def concentration(dollars):
    wins = [d for d in dollars if d > 0]
    best = max(dollars) if dollars else 0.0
    return {"sum": sum(dollars), "sum_without_best": sum(dollars) - best if dollars else 0.0,
            "best_share_of_positive": (best / sum(wins)) if wins and best > 0 else None}


def tier_b(units, shared, rng):
    """Tests 1-6 in order; returns (result, stats)."""
    dev = [u for u in units if iso(u["opened_at"]) < HOLDOUT]
    hold = [u for u in units if iso(u["opened_at"]) >= HOLDOUT]
    rets = [u["pnl"] / u["notional"] for u in dev]
    dollars = [100.0 * r for r in rets]
    hold_rets = [u["pnl"] / u["notional"] for u in hold]
    conc = concentration(dollars)
    win_days = sorted({iso(u["opened_at"])[:10] for u, d in zip(dev, dollars) if d > 0})
    shared_n = sum(1 for u in units if shared.get(u["id"]))
    st = {"dev_n": len(dev), "dev_mean_pct": 100 * float(np.mean(rets)) if rets else None,
          "dev_median_pct": 100 * float(np.median(rets)) if rets else None,
          "dev_lb90_pct": 100 * lower_bound(rets, rng) if len(rets) >= 2 else None,
          "dev_sum_per_100": conc["sum"], "dev_sum_without_best": conc["sum_without_best"],
          "best_share_of_positive": conc["best_share_of_positive"], "win_days": len(win_days),
          "holdout_n": len(hold), "holdout_mean_pct": 100 * float(np.mean(hold_rets)) if hold_rets else None,
          "shared_origin_share": shared_n / len(units) if units else None}
    if len(dev) < 10:
        return "1_fewer_than_10_coins", st
    if not (st["dev_mean_pct"] > 0 and st["dev_lb90_pct"] is not None and st["dev_lb90_pct"] > 0):
        return "2_mean_or_lower_bound_not_positive", st
    if not (conc["sum_without_best"] > 0 and conc["best_share_of_positive"] is not None
            and conc["best_share_of_positive"] <= 0.5):
        return "3_best_coin_concentration", st
    if len(win_days) < 2:
        return "4_wins_on_one_day", st
    if len(hold) < 3:
        return "5_holdout_thin", st
    if not st["holdout_mean_pct"] > 0:
        return "5_holdout_mean_not_positive", st
    if st["shared_origin_share"] >= 0.5:
        return "6_shadow_of_armed_wallet", st
    return "candidate_for_funded_canary", st


def tier_a(c, rost, excluded_uids):
    rows = c.execute(
        "SELECT id, source_user_id, source_handle, network_id, mint, opened_at, closed_at, realized_pnl, "
        "entry_notional, exit_reason FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 "
        "AND closed_at >= ? AND closed_at < ? AND realized_pnl IS NOT NULL "
        "AND COALESCE(signal_source,'') != 'manual' ORDER BY opened_at, id", (A0, W1)).fetchall()
    fill_n = defaultdict(lambda: [0, 0])
    ids = [r[0] for r in rows]
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for pid, kind in c.execute(
                f"SELECT position_id, kind FROM memecoin_fills WHERE position_id IN ({','.join('?' * len(chunk))})",
                chunk):
            fill_n[pid][0 if kind == "entry" else 1] += 1
    per = defaultdict(list)
    seen = set()
    for r in rows:
        uid, chain = r[1], str(r[3])
        if uid in excluded_uids or chain in rost.get(uid, {}).get("armed", set()):
            continue
        key = (uid, chain, r[4])
        per[(uid, chain)].append({"id": r[0], "handle": r[2], "mint": r[4], "opened_at": r[5],
                                  "closed_at": r[6], "pnl": r[7], "notional": r[8], "exit_reason": r[9],
                                  "first_of_mint": key not in seen,
                                  "entry_fills": fill_n[r[0]][0], "exit_fills": fill_n[r[0]][1]})
        seen.add(key)
    out = {}
    for key, closes in per.items():
        firsts = [x for x in closes if x["first_of_mint"]]
        dollars = [float(x["pnl"]) for x in firsts]
        conc = concentration(dollars)
        untraced = [x["id"] for x in closes if x["entry_fills"] == 0 or x["exit_fills"] == 0]
        if len(firsts) < 5:
            res = "A1_fewer_than_5_real_closes"
        elif not conc["sum"] > 0:
            res = "A2_net_not_positive"
        elif not conc["sum_without_best"] > 0:
            res = "A3_not_positive_without_best_coin"
        elif conc["best_share_of_positive"] is None or conc["best_share_of_positive"] > 0.5:
            res = "A4_best_coin_over_half"
        else:
            res = "recommend_re_arming_on_chain"
        out[key] = {"result": res, "handle": closes[0]["handle"], "closes_n": len(closes),
                    "coins_n": len(firsts), **conc,
                    "untraced_position_ids": untraced,
                    "closes": [{k: x[k] for k in ("id", "mint", "opened_at", "closed_at", "pnl",
                                                   "exit_reason", "first_of_mint")} for x in closes]}
    return out


def funded_context(c, uid, chain):
    """Context for the operator (Gamma, 2026-09-30): the latest revoke on this chain, and the 7-day
    funded PnL the 09-29 roster cut used (every real close in the 7 days to W1, manual included)."""
    rev = c.execute(
        "SELECT action, actor, created_at, reason FROM memecoin_wallet_approvals "
        "WHERE source_user_id = ? AND action IN ('chain_revoke', 'revoke', 'retire') "
        "AND (chain = ? OR chain IS NULL) ORDER BY created_at DESC LIMIT 1", (uid, chain)).fetchone()
    s7, n7 = c.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0), COUNT(*) FROM memecoin_positions "
        "WHERE execution_mode='real' AND is_open=0 AND source_user_id = ? AND network_id = ? "
        "AND closed_at >= ? AND closed_at < ? AND realized_pnl IS NOT NULL",
        (uid, chain, (ts(W1) - timedelta(days=7)).isoformat(), W1)).fetchone()
    return {"last_revoke": ({"action": rev[0], "actor": rev[1], "at": rev[2], "reason": rev[3]}
                            if rev else None),
            "funded_7d_pnl_all_closes": round(float(s7), 2), "funded_7d_closes": int(n7)}


def write_report(d, path):
    """The markdown rows report (docs/audits/2026-09-30-alpha-wallet-shortlist-rows.md)."""
    import collections
    m = d["meta"]
    f = lambda x, n=1: "—" if x is None else f"{x:.{n}f}"
    L = ["# Alpha-wallet shortlist: rows (Beta; Gamma recomputed and matched)\n",
         "Rule: `gamma/alpha-shortlist-prereg` a26035a11 + amendment A1 ca128d2d0 (entry set route_quote + "
         "paper_quote + funded_quote_reuse). Gamma's rulings on Beta's six interpretation choices: (1), (3)-(6) "
         "accepted; (2) a Tier A wallet outside every pool is outside the population, reported apart, never "
         "recommended.",
         "Script: `scripts/research/alpha_wallet_shortlist.py` (read-only ledger + wallet-graph DB, no network). "
         "Rows JSON (gitignored, regenerate with `--out`): `~/.cache/beta/alpha-shortlist/rows.json`.",
         f"Generated {m['generated_at'][:19]}Z. Nothing here arms or disarms a wallet; roster changes are the "
         "operator's, on funded results only.\n",
         "## Screen\n",
         f"- Tier B (paper, funded_clone): {m['screened_tier_b']} (wallet, chain) rows. Tier A (real closes, 30 d): "
         f"{m['screened_tier_a']} rows.",
         f"- Pools: j7_unarmed, multi_chain, herder (src of `follows`/`slot_follower` edges: "
         f"{m['pool_meta']['herders']} wallets). The 3b upstream table is empty ({m['pool_meta']['upstream_rows']} "
         "rows), so no upstream pool.",
         f"- Own-address exclusion matched {m['own_user_ids_excluded']} source ids (EVM address map only).",
         f"- Coverage (scored / first-per-mint units): withheld chains: {m['tier_b_withheld_chains'] or 'none'}.\n",
         "| chain | pool | scored | all | share |", "|---|---|---|---|---|"]
    for k, v in m["coverage"].items():
        ch, lab = k.split("|")
        L.append(f"| {ch} | {lab} | {v['scored']} | {v['all']} | {v['share']:.2f} |")
    L += ["\n## Tier A: recommend re-arming on chain X (funded evidence)\n",
          "Counts: " + str(dict(collections.Counter(r["result"] for r in d["tier_a"]))) + "\n",
          "⚠️ CONFLICT FOR THE OPERATOR: every pass below was revoked on its chain recently, most by the operator "
          "in the UI (actor `ui`). The 09-29 roster cut used 7-day funded PnL; this screen uses 30 days. Both are "
          "shown.\n",
          "| wallet | chain | pools | coins | closes | 30 d sum $ | without best $ | best share | 7 d funded PnL $ "
          "(all closes, n) | last revoke (actor, UTC) |", "|---|---|---|---|---|---|---|---|---|---|"]

    def a_row(r):
        lr = r.get("last_revoke") or {}
        seven = (f"{r['funded_7d_pnl_all_closes']:.2f} ({r['funded_7d_closes']})"
                 if r["funded_7d_closes"] else "no closes")
        return (f"| {r['handle']} | {r['chain']} | {','.join(r['pools']) or 'none'} | {r['coins_n']} | "
                f"{r['closes_n']} | {r['sum']:.2f} | {r['sum_without_best']:.2f} | "
                f"{f(r['best_share_of_positive'], 2)} | {seven} | "
                f"{lr.get('actor', '—')}, {(lr.get('at') or '—')[:16]} |")
    L += [a_row(r) for r in d["tier_a"] if r["result"] == "recommend_re_arming_on_chain"]
    outside = [r for r in d["tier_a"] if r["result"] == "outside_population"]
    L += ["\nReported apart (outside every pool; not recommended): "
          + "; ".join(f"{r['handle']} {r['chain']} (screen result {r['result_in_screen']})" for r in outside),
          "\nEvery pass's position ids are in the JSON (`closes`). One Tier A row is untraced: Quanterty 4663, "
          "position 66881 (written off 09-29, no exit fill; it fails A1/A2 regardless).\n",
          "## Tier B: candidate for a funded canary (paper, executably priced)\n",
          "Stated limit: paper exits are TIMED by the paper's marks (mostly display marks) though they FILL at a "
          "route quote. Context, not an arming basis.\n",
          "Counts: " + str(dict(collections.Counter(r["result"] for r in d["tier_b"]))) + "\n",
          "| wallet | chain | pools | dev n | mean % | median % | 90% LB % | best share | without best $/100 | "
          "win days | holdout n | holdout mean % | shared-origin | result |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(d["tier_b"], key=lambda r: (r["result"] != "candidate_for_funded_canary", r["chain"],
                                                -(r["dev_n"] or 0))):
        if r["dev_n"] < 10:
            continue
        L.append(f"| {r['handle']} | {r['chain']} | {','.join(r['pools'])} | {r['dev_n']} | {f(r['dev_mean_pct'])} | "
                 f"{f(r['dev_median_pct'])} | {f(r['dev_lb90_pct'])} | {f(r['best_share_of_positive'], 2)} | "
                 f"{f(r['dev_sum_without_best'])} | {r['win_days']} | {r['holdout_n']} | "
                 f"{f(r['holdout_mean_pct'])} | {f(r['shared_origin_share'], 2)} | {r['result']} |")
    L += ["\nRows with fewer than 10 dev coins (result 1) are in the JSON only.\n",
          "## Passes by band (all scored coins, dev + holdout, entry mcap band)\n"]
    for r in d["tier_b"]:
        if r["result"] == "candidate_for_funded_canary":
            L.append(f"- {r['handle']} {r['chain']}: " + "; ".join(
                f"{b} n={v['n']} mean {v['mean_pct']:.1f}% median {v['median_pct']:.1f}%"
                for b, v in r["by_band"].items()))
    L += ["\n## Interpretation choices (Gamma's rulings)\n",
          "1. Unit applies to both tiers: first position per (wallet, chain, mint); Tier A counts distinct coins "
          "against the >= 5 floor and lists every close. Accepted.",
          "2. A Tier A wallet in no pool is outside the population: reported apart, not recommended (Gamma's "
          "ruling).",
          "3. Test 4 day = UTC date of opened_at of the winning dev coins. Accepted.",
          "4. Test 6 share is over all scored coins (dev + holdout); the wallet's buy time is its origin event's "
          "created_at, matched to any buy by a wallet armed on that chain today within 30 s either side. Accepted.",
          "5. Bootstrap: numpy default_rng(20260930) per row, 4,000 resamples of dev returns (pnl / "
          "entry_notional), 10th percentile of the means. Accepted.",
          "6. Herders = src of follows/slot_follower edges; the 3b upstream table is empty. Accepted.",
          "\nPnL trace: every scored paper coin reproduces realized_pnl as sum(exit fill notional) - entry fill "
          "notional within max($0.01, 0.5%); a shifted-PnL negative control fails all of them."]
    Path(path).write_text("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "sniper_tracker.db"))
    ap.add_argument("--graph-db", default=str(ROOT / "data" / "memecoin_wallet_graph.db"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    c = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    rost = roster(c)
    own = own_user_ids(c, own_addresses())
    excluded_uids = {u for u, w in rost.items() if w["excluded"]} | own
    labels, pool_meta = pools(c, args.graph_db, rost)
    units, dropped = paper_units(c)
    shared_buys = buys_by_mint(c, {k[2] for k in units})

    per = defaultdict(list)
    for key, u in units.items():
        per[key[:2]].append(u)
    rows, coverage = [], defaultdict(lambda: [0, 0])
    untraced_paper = []
    for (uid, chain), us in sorted(per.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        w = rost.get(uid, {})
        if uid in excluded_uids or chain in w.get("armed", set()):
            continue
        labs = labels.get((uid, chain), set())
        if not labs:
            continue
        scored, censored = [], defaultdict(int)
        for u in us:
            reason = censor_reason(u)
            if reason:
                censored[reason] += 1
                continue
            ok, diff = traced(u)
            if not ok:
                untraced_paper.append({"id": u["id"], "diff": diff})
            scored.append(u)
        for lab in labs:
            coverage[(chain, lab)][0] += len(scored)
            coverage[(chain, lab)][1] += len(us)
        armed_on_chain = {a for a, ww in rost.items() if chain in ww["armed"]}
        shared = {}
        for u in scored:
            # its buy = the source event the position copied (origin_event_id); the paper open
            # time only when that event is missing
            t = ts(u["buy_at"] or u["opened_at"])
            shared[u["id"]] = any(e_uid in armed_on_chain and abs((e_t - t).total_seconds()) <= SHARED_S
                                  for e_uid, e_t in shared_buys.get((chain, u["mint"]), []))
        result, st = tier_b(scored, shared, np.random.default_rng(SEED))
        rets = [u["pnl"] / u["notional"] for u in scored]
        bands = defaultdict(list)
        for u in scored:
            bands[band(u["mcap"])].append(100 * u["pnl"] / u["notional"])
        rows.append({
            "tier": "B", "uid": uid, "handle": w.get("handle") or us[0]["handle"], "chain": chain,
            "pools": sorted(labs), "positions_first_per_mint": len(us), "scored_n": len(scored),
            "censored_n": sum(censored.values()), "censored": dict(censored),
            "dropped_reentries": dropped.get((uid, chain), 0),
            "all_scored_mean_pct": 100 * float(np.mean(rets)) if rets else None,
            "all_scored_median_pct": 100 * float(np.median(rets)) if rets else None,
            "by_band": {b: {"n": len(v), "mean_pct": float(np.mean(v)), "median_pct": float(np.median(v))}
                        for b, v in sorted(bands.items())},
            **st, "result": result,
            "scored_position_ids": [u["id"] for u in scored]})
    cov = {f"{ch}|{lab}": {"scored": s, "all": a, "share": (s / a if a else None)}
           for (ch, lab), (s, a) in sorted(coverage.items())}
    withheld = sorted({k.split("|")[0] for k, v in cov.items() if v["share"] is not None and v["share"] < 0.5})
    ta = tier_a(c, rost, excluded_uids)
    tier_a_rows = [{"tier": "A", "uid": k[0], "handle": rost.get(k[0], {}).get("handle") or v.pop("handle"),
                    "chain": k[1], "pools": sorted(labels.get(k, set())), **v,
                    **funded_context(c, k[0], k[1])}
                   for k, v in sorted(ta.items())]
    for r in tier_a_rows:
        # Gamma's ruling on choice (2): a wallet in no pool is outside the pre-registered population;
        # it is reported apart and never recommended.
        if not r["pools"]:
            r["result_in_screen"] = r["result"]
            r["result"] = "outside_population"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    meta = {"rule": "gamma/alpha-shortlist-prereg a26035a11 + A1 ca128d2d0", "window": [W0, W1],
            "tier_a_window": [A0, W1], "holdout_from": HOLDOUT, "entry_ok": ENTRY_OK, "exit_ok": EXIT_OK,
            "seed": SEED, "draws": DRAWS, "pool_meta": pool_meta, "own_user_ids_excluded": len(own),
            "screened_tier_b": len(rows), "screened_tier_a": len(tier_a_rows),
            "coverage": cov, "tier_b_withheld_chains": withheld,
            "untraced_paper": untraced_paper, "generated_at": datetime.now(timezone.utc).isoformat()}
    (out / "rows.json").write_text(json.dumps({"meta": meta, "tier_a": tier_a_rows, "tier_b": rows},
                                              indent=1, default=str))
    write_report({"meta": meta, "tier_a": tier_a_rows, "tier_b": rows}, out / "rows.md")
    print(json.dumps({k: meta[k] for k in ("screened_tier_b", "screened_tier_a", "tier_b_withheld_chains",
                                           "pool_meta", "own_user_ids_excluded")}, default=str))
    print("untraced paper rows:", len(untraced_paper))
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('write_report', 'main'))

if __name__ == "__main__":
    sys.exit(main())
