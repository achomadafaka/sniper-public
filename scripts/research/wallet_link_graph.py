#!/usr/bin/env python3
"""Memecoin wallet link graph, v2 (spec docs/superpowers/specs/2026-09-29-memecoin-wallet-link-graph-design.md,
Alpha GO 2026-09-29). Research only: it describes wallets and never admits, arms, sizes or exits.

v1 edges, from J7 events in the funded ledger (read-only):
  follows   leader -> follower: first buys of one coin, the follower within W = 600 s; >= 3 shared
            coins, the leader first on >= 80% of them, and the shared count must beat the p99 of a
            within-day permutation null (shuffles of the follower's buy times across its coins that
            UTC day), so co-activity inside one window cannot manufacture a follower. v2 (Alpha and
            Gamma, 2026-09-29): exact p = (1 + shuffles >= observed) / 2,001 <= 0.01, on each of 5
            seeds drawn per pair; an edge that passes on fewer than 5 seeds is kept and flagged
            'boundary' (at a p99 of 200 shuffles, 4 of 9 live edges depended on the seed).
  dumps_on  seller -> buyer: the seller, having bought the coin BEFORE the buyer's first buy, makes
            its first sell within +-600 s of that buy in the 120 s after it; >= 3 such coins, and
            the hits must beat a binomial null (q = 120 / 1200 = 0.1 over the coins with a sell
            within +-600 s) at p <= 0.01. v1 had no null and scored the same in the adjacent windows
            (Gamma MED, 2026-09-29): co-activity, not dumping.
Clock: J7 received_at (the corrected clock domain). Sample: the J7 roster only; every edge says so.
Identity: the J7 source_user_id (its chain address is attached when one is proven, in a later step).

Edges go to a SEPARATE DB, data/memecoin_wallet_graph.db under the state root, never the funded
ledger (Alpha, 2026-09-29).

    python3 scripts/research/wallet_link_graph.py run [--days 14]
    python3 scripts/research/wallet_link_graph.py project     # the current edges into the brain
"""
import argparse
import collections
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import statistics
import sys
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

METHOD_VERSION = "wallet-graph-v3:w600:c3:o80:p01x2000s5:d120binom600:slotbinom"
# Slot edges built from a sample that holds coverage reads (intake 'j7') carry their own method: the
# base rates come from a different sample, and verdicts of the two must not share a string (Gamma,
# pre-registration 2026-09-30 A). The job still writes and replaces both.
SLOT_COVERAGE_METHOD = METHOD_VERSION + ":cov1"
WINDOW_S = 600.0
MIN_COINS = 3
MIN_ORDER = 0.8
SHUFFLES = 2000
SEEDS = (0, 1, 2, 3, 4)
ALPHA_P = 0.01
DUMP_WINDOW_S = 120.0
DUMP_Q = DUMP_WINDOW_S / (2 * WINDOW_S)      # the hit share when sells are uniform within +-W
EVIDENCE_MAX = 10
COVERAGE = {"source": "memecoin_events j7_wallets", "clock": "received_at (J7 corrected clock)",
            "sample": "J7 roster only", "evm_funding": "unknown (no explorer API)"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS memecoin_wallet_edges (
    id INTEGER PRIMARY KEY,
    network_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    src_user_id TEXT,
    dst_user_id TEXT,
    n_coins INTEGER NOT NULL,
    n_consistent INTEGER,
    lag_p50_s REAL,
    lag_p90_s REAL,
    null_p99 REAL,
    null_p REAL,
    flag TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    coverage TEXT NOT NULL,
    evidence TEXT NOT NULL,
    method_version TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    UNIQUE (network_id, edge_type, src, dst, method_version)
);
CREATE TABLE IF NOT EXISTS memecoin_wallet_graph_runs (
    id INTEGER PRIMARY KEY,
    method_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    window_from TEXT,
    edges INTEGER,
    helius_calls INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS memecoin_wallet_graph_brain (
    edge_key TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    projected_at TEXT NOT NULL
);
"""


from sniper.services.memecoin_wallet_coverage import (  # noqa: E402  one definition, the page reads it too
    DETECTION_BUYS_SQL, J7_SOLANA_BUYS_SQL, PAIR_PCT, PAIR_S, coverage_records, coverage_units, first_id_since,
    funded_solana_fills, j7_solana_buys)


def _ts(iso):
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()


def _pct(values, q):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * (len(values) - 1) + 0.5))], 3) if values else None


def _first_buys(events):
    """{network: {mint: {wallet: ts}}}, the first buy per wallet per coin."""
    out = collections.defaultdict(lambda: collections.defaultdict(dict))
    for e in events:
        if e["side"] != "buy":
            continue
        t = _ts(e["at"])
        coin = out[e["network_id"]][e["mint"]]
        if e["source_user_id"] not in coin or t < coin[e["source_user_id"]]:
            coin[e["source_user_id"]] = t
    return out


def _shared(a_times, b_times):
    """Coins both bought within WINDOW_S: [(coin, ta, tb)]."""
    return [(m, a_times[m], b_times[m]) for m in a_times.keys() & b_times.keys()
            if abs(a_times[m] - b_times[m]) <= WINDOW_S]


def _null(a_times, b_times, rng, real):
    """(p99, exceed): the p99 shared count when B's buy times are shuffled across B's coins within
    each UTC day, and how many shuffles reached the real count."""
    by_day = collections.defaultdict(list)
    for m, t in b_times.items():
        by_day[datetime.fromtimestamp(t, timezone.utc).date()].append(m)
    counts = []
    for _ in range(SHUFFLES):
        shuffled = {}
        for coins in by_day.values():
            times = [b_times[m] for m in coins]
            rng.shuffle(times)
            shuffled.update(zip(coins, times))
        counts.append(len(_shared(a_times, shuffled)))
    return _pct(counts, 0.99), sum(1 for c in counts if c >= real)


def null_verdict(per_seed):
    """(seeds passed, flag): p <= ALPHA_P on every seed is clean, on some 'boundary', on none no edge."""
    passed = sum(1 for s in per_seed if s["p"] <= ALPHA_P)
    return passed, ("boundary" if 0 < passed < len(per_seed) else None)


def follows_edges(events, *, seeds=SEEDS, stats=None):
    """`stats`, when given, counts the pairs that reached the null test: at p99 about 1% of them pass
    by chance, so an edge count near 0.01 * pairs_tested is noise."""
    stats = stats if stats is not None else {}
    stats.setdefault("pairs_tested", 0)
    edges = []
    for network, coins in _first_buys(events).items():
        per_wallet = collections.defaultdict(dict)
        for mint, buyers in coins.items():
            for wallet, t in buyers.items():
                per_wallet[wallet][mint] = t
        wallets = sorted(per_wallet)
        for i, a in enumerate(wallets):
            for b in wallets[i + 1:]:
                shared = _shared(per_wallet[a], per_wallet[b])
                if len(shared) < MIN_COINS:
                    continue
                a_first = [s for s in shared if s[1] < s[2]]
                b_first = [s for s in shared if s[2] < s[1]]
                lead, follow, ordered = (a, b, a_first) if len(a_first) >= len(b_first) else (b, a, b_first)
                if len(ordered) / len(shared) < MIN_ORDER:
                    continue
                stats["pairs_tested"] += 1
                per_seed = []
                for seed in seeds:                        # per pair: other pairs cannot move it
                    rng = random.Random("%s:%s:%s:%s" % (seed, network, lead, follow))
                    p99, exceed = _null(per_wallet[lead], per_wallet[follow], rng, len(shared))
                    per_seed.append({"seed": seed, "p99": p99, "exceed": exceed,
                                     "p": round((1 + exceed) / (SHUFFLES + 1), 6)})
                passed, flag = null_verdict(per_seed)
                if not passed:
                    continue
                lags = [abs(tb - ta) for _m, ta, tb in ordered]
                times = sorted(min(ta, tb) for _m, ta, tb in shared)
                edges.append(_edge(network, "follows", lead, follow, n_coins=len(shared),
                                   n_consistent=len(ordered), lags=lags,
                                   null_p99=max(s["p99"] for s in per_seed),
                                   null_p=max(s["p"] for s in per_seed), flag=flag,
                                   null={"shuffles": SHUFFLES, "seeds_passed": passed,
                                         "per_seed": per_seed},
                                   first=times[0], last=times[-1],
                                   evidence=[{"mint": m, "leader_at": min(ta, tb), "lag_s": round(abs(tb - ta), 1)}
                                             for m, ta, tb in sorted(ordered, key=lambda s: s[1])][:EVIDENCE_MAX]))
    return edges


def binomial_tail(k, n, q):
    """P(X >= k) for X ~ Binomial(n, q), exact."""
    return sum(math.comb(n, i) * q ** i * (1 - q) ** (n - i) for i in range(k, n + 1))


def dumps_on_edges(events):
    by_key = collections.defaultdict(list)                # (network, mint) -> events
    for e in events:
        by_key[(e["network_id"], e["mint"])].append(dict(e, t=_ts(e["at"])))
    tally = collections.defaultdict(lambda: {"near": 0, "hits": [], "bins": [0] * 10})  # (network, seller, buyer)
    for (network, mint), evs in by_key.items():
        first_buy, sells = {}, collections.defaultdict(list)
        for e in sorted(evs, key=lambda e: e["t"]):
            if e["side"] == "buy":
                first_buy.setdefault(e["source_user_id"], e["t"])
            else:
                sells[e["source_user_id"]].append(e["t"])
        for buyer, tb in first_buy.items():
            for seller, times in sells.items():
                if seller == buyer or first_buy.get(seller, float("inf")) >= tb:
                    continue                              # the seller did not hold before the buy
                near = [t for t in times if abs(t - tb) <= WINDOW_S]
                if not near:
                    continue
                cell = tally[(network, seller, buyer)]
                cell["near"] += 1
                cell["bins"][min(9, int((near[0] - tb + WINDOW_S) // DUMP_WINDOW_S))] += 1
                if 0 <= near[0] - tb <= DUMP_WINDOW_S:     # the first sell near the buy, only
                    cell["hits"].append((mint, tb, near[0]))
    edges = []
    for (network, seller, buyer), cell in tally.items():
        hits = cell["hits"]
        if len(hits) < MIN_COINS:
            continue
        p = round(binomial_tail(len(hits), cell["near"], DUMP_Q), 6)
        if p > ALPHA_P:
            continue
        lags = [ts - tb for _m, tb, ts in hits]
        times = sorted(tb for _m, tb, _ts in hits)
        edges.append(_edge(network, "dumps_on", seller, buyer, n_coins=len(hits), n_consistent=len(hits),
                           lags=lags, null_p99=None, null_p=p, first=times[0], last=times[-1],
                           null={"test": "binomial", "near": cell["near"], "hits": len(hits),
                                 "q": DUMP_Q, "p": p,
                                 # first-sell offsets in 120 s bins from -600 s: the rule is bin 5;
                                 # bin 6 shows whether the timing is the 120 s or merely "soon after"
                                 "bins_120s": cell["bins"]},
                           evidence=[{"mint": m, "buy_at": tb, "sell_lag_s": round(ts - tb, 1)}
                                     for m, tb, ts in sorted(hits, key=lambda h: h[1])[:EVIDENCE_MAX]]))
    return edges


def empty_days(events, since_iso, now_iso):
    """Whole UTC days inside (since, now) with no J7 event: a feed gap is named, never read as quiet.
    The partial first and last days are not judged."""
    seen = {datetime.fromisoformat(e["at"].replace("Z", "+00:00")).date() for e in events}
    day = datetime.fromisoformat(since_iso).date() + timedelta(days=1)
    last = datetime.fromisoformat(now_iso).date()
    out = []
    while day < last:
        if day not in seen:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _edge(network, edge_type, src, dst, *, n_coins, n_consistent, lags, null_p99, first, last, evidence,
          null_p=None, flag=None, null=None):
    return {"network_id": network, "edge_type": edge_type, "src": src, "dst": dst,
            "src_user_id": src, "dst_user_id": dst, "n_coins": n_coins, "n_consistent": n_consistent,
            "lag_p50_s": _pct(lags, 0.5), "lag_p90_s": _pct(lags, 0.9), "null_p99": null_p99,
            "null_p": null_p, "flag": flag, "first_seen": _iso(first), "last_seen": _iso(last), "coverage": dict(COVERAGE),
            "evidence": dict({"coins": [dict(e, **{k: _iso(v) for k, v in e.items() if k.endswith("_at")})
                                        for e in evidence]}, **({"null": null} if null else {}))}


def hubs(edges, handles, top=5):
    """The wallets on the most edges, with their J7 handle (Alpha: name the hub)."""
    degree = collections.Counter()
    for e in edges:
        degree[e["src"]] += 1
        degree[e["dst"]] += 1
    ranked = sorted(degree.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    return [{"source_user_id": w, "handle": handles.get(w), "edges": n} for w, n in ranked]


def wallet_handles(ledger):
    return {str(r[0]): r[1] for r in ledger.execute(
        "SELECT source_user_id, handle FROM memecoin_wallets WHERE source_user_id IS NOT NULL")}


def graph_connection(path):
    conn = sqlite3.connect(str(path), timeout=5)
    conn.executescript(SCHEMA)
    return conn


def ledger_connection(path):
    from sniper.persistence.db_core import get_readonly_connection
    return get_readonly_connection(str(path))


def write_edges(path, edges, *, computed_at):
    """Replace this method version's edges with `edges` in one transaction. Returns the row count."""
    conn = graph_connection(path)
    try:
        with conn:
            conn.execute("DELETE FROM memecoin_wallet_edges WHERE method_version IN (?,?)",
                         (METHOD_VERSION, SLOT_COVERAGE_METHOD))
            conn.executemany(
                "INSERT INTO memecoin_wallet_edges (network_id, edge_type, src, dst, src_user_id, "
                "dst_user_id, n_coins, n_consistent, lag_p50_s, lag_p90_s, null_p99, null_p, flag, "
                "first_seen, last_seen, coverage, evidence, method_version, computed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(e["network_id"], e["edge_type"], e["src"], e["dst"], e["src_user_id"],
                  e["dst_user_id"], e["n_coins"], e["n_consistent"], e["lag_p50_s"], e["lag_p90_s"],
                  e["null_p99"], e["null_p"], e["flag"], e["first_seen"], e["last_seen"], json.dumps(e["coverage"]),
                  json.dumps(e["evidence"]), e.get("method_version") or METHOD_VERSION, computed_at)
                 for e in edges])
        return len(edges)
    finally:
        conn.close()


J7_EVENTS_SQL = ("SELECT network_id, mint, source_user_id, side, received_at FROM memecoin_events "
                 "WHERE id >= ? AND received_at >= ? AND delivery_lane='j7_wallets' "
                 "AND side IN ('buy','sell') AND source_user_id IS NOT NULL AND mint IS NOT NULL")


def j7_events(ledger, since_iso):
    rows = ledger.execute(J7_EVENTS_SQL, (first_id_since(ledger, since_iso), since_iso)).fetchall()
    return [{"network_id": str(r[0]), "mint": r[1], "source_user_id": str(r[2]), "side": r[3],
             "at": r[4]} for r in rows]


def _rate_429():
    """The research key's rate-429 count in this process (funded_exit_truth.rpc counts every one)."""
    sys.path.insert(0, str(REPO / "scripts" / "research"))
    import funded_exit_truth as fet
    return fet.RATE_429[0]


def run(args):
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    state = state_root()
    load_env_files(str(state), names=(".env",))
    ledger_path = Path(args.ledger or state / "sniper_tracker.db")
    graph_path = Path(args.graph or state / "data" / "memecoin_wallet_graph.db")
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=args.days)).isoformat()
    budget = max(0, min(args.helius_budget, SLOT_BUDGET_MAX))
    slot_calls, slot_why, slot_new = 0, "skipped (budget 0)", 0
    fill_calls, fill_why, fill_new = 0, "skipped (budget 0)", 0
    coverage = {"reads_new": 0, "calls": 0, "stop": "off", "pending": 0, "slot_unknown": 0, "units": 0,
                "no_signature": 0}
    rate_429_start = _rate_429()
    trail = {"probe_days": 0, "calls": 0, "stop": "off", "leads": 0, "pending": 0}
    axiom = {"wallets": 0, "calls": 0, "stop": "off", "reads": 0, "pending": 0}
    ledger = ledger_connection(ledger_path)
    try:
        events = j7_events(ledger, since)
        handles = wallet_handles(ledger)
        owners = solana_owners(ledger)
        flat = lambda: not solana_book_open(ledger)        # noqa: E731
        ours = our_entry_sigs(ledger) if budget else set()
        fill_budget = budget if args.fill_read_budget is None else max(0, min(budget, args.fill_read_budget))
        if fill_budget:                                        # our funded fills first, then the backlog
            by_uid = collections.defaultdict(list)
            for addr, uid in owners.items():
                by_uid[uid].append(addr)
            fills = pending_fills(graph_path, fills_without_reads(funded_solana_fills(ledger, since),
                                                                  load_reads(graph_path, since)))
            sigs = [f["source_sig"] for f in fills if f.get("source_sig")]
            slots, sig_calls, fill_why = {}, 0, "done"
            try:
                slots, sig_calls = helius_sig_slots(sigs[:1024], flat_fn=flat) if sigs else ({}, 0)
            except ReadStop as exc:
                fill_why = "stopped: %s" % exc
            freads, fstatus, fill_calls = [], [], 0
            if not fill_why.startswith("stopped"):
                freads, fstatus, fill_calls, fill_why = read_fills(
                    fills, addresses=by_uid, budget=fill_budget - sig_calls, flat_fn=flat, ours=ours,
                    slots=slots, fetch_buy=lambda a, m, o: helius_buy_fetch(a, m, o, flat_fn=flat),
                    fetch_slot=lambda det: helius_slot_fetch(det, flat_fn=flat))
            fill_calls += sig_calls
            now_iso = datetime.now(timezone.utc).isoformat()
            save_reads(graph_path, freads, read_at=now_iso)
            save_fill_status(graph_path, fstatus, read_at=now_iso)
            fill_new = len(freads)
        if budget - fill_calls > 0:
            dets = pending_detections(graph_path, solana_detections(ledger, since))
            reads, slot_calls, slot_why = read_slots(
                dets, fetch=lambda det: helius_slot_fetch(det, flat_fn=flat), budget=budget - fill_calls,
                ours=ours, flat_fn=flat, intake="detection")
            save_reads(graph_path, reads, read_at=datetime.now(timezone.utc).isoformat())
            slot_new = len(reads)
        slot_calls += fill_calls
        slot_pass_calls = slot_calls                           # fills and detections: the slot budget's use
        from sniper.services.memecoin_wallet_map import own_address_set
        # A failure here must not end the hourly run (Gamma LOW): the edges, the funder reads and
        # the run row below belong to slices 1 and 2. The note names the error; the units stay
        # pending and the next run tries again.
        impact = {"reads_new": 0, "calls": 0, "stop": "not run", "units": 0, "wallets": 0}
        try:
            impact = impact_pass(ledger, graph_path, since, flat_fn=flat,
                                 budget=max(0, min(args.impact_budget, SLOT_BUDGET_MAX - slot_calls)),
                                 own_addresses=own_address_set(os.environ.get("MEMECOIN_OWN_ADDRESSES", "")))
        except Exception as exc:
            traceback.print_exc()
            impact["stop"] = "error:%s" % type(exc).__name__
        slot_calls += impact["calls"]
        # Slice 3b. Same rule as the impact pass: a failure here never ends the hourly run.
        upstream = {"reads_new": 0, "calls": 0, "stop": "not run", "status": {}, "control": "0/0",
                    "control_fails": {}, "thin_losers": 0, "fx_rejected": 0, "pruned": 0, "wallets": None,
                    "funder_wanted": []}
        try:
            upstream = upstream_pass(ledger, graph_path, since, flat_fn=flat, verdicts=args.upstream_verdicts,
                                     budget=max(0, min(args.upstream_budget, SLOT_BUDGET_MAX - slot_calls)),
                                     own_addresses=own_address_set(os.environ.get("MEMECOIN_OWN_ADDRESSES", "")))
        except Exception as exc:
            traceback.print_exc()
            upstream["stop"] = "error:%s" % type(exc).__name__
        slot_calls += upstream["calls"]
        # Coverage (overhaul spec section 0; operator 2026-09-30): AFTER slices 3 and 3b, from what the
        # slot budget has left, and not at all once the research key has answered 429 in this run.
        if getattr(args, "coverage", False):
            left = max(0, min(budget - slot_pass_calls, SLOT_BUDGET_MAX - slot_calls))
            if _rate_429() > rate_429_start:
                coverage["stop"] = "skipped (429 earlier in this run)"
            else:
                try:
                    units, no_signature = coverage_units(coverage_records(ledger, since))
                    coverage = dict(coverage_pass(
                        units, graph_path, budget=left, flat_fn=flat, ours=ours, no_signature=no_signature,
                        sig_slots=lambda sigs: helius_sig_slots(sigs, flat_fn=flat),
                        fetch_slot=lambda det: helius_slot_fetch(det, flat_fn=flat)),
                        units=len(units) + len(no_signature), no_signature=len(no_signature))
                except Exception as exc:
                    traceback.print_exc()
                    coverage["stop"] = "error:%s" % type(exc).__name__
            slot_calls += coverage["calls"]
        # Bot trail and best herd member (pre-registration C and D): after coverage, only with Gamma's
        # frozen list, and not at all once the research key has answered 429 in this run.
        if getattr(args, "trail", None):
            if _rate_429() > rate_429_start:
                trail["stop"] = "skipped (429 earlier in this run)"
            else:
                try:
                    frozen = json.loads(Path(args.trail).read_text())
                    herders = set(frozen.get("herders") or [])
                    herder_buys = collections.defaultdict(list)
                    for r in load_reads(graph_path, since):
                        if r["status"] == "ok" and r["source_user_id"] in herders and r.get("detected_at"):
                            herder_buys[r["mint"]].append(_ts(r["detected_at"]))
                    trail = trail_pass(graph_path, frozen, budget=max(0, min(TRAIL_BUDGET, SLOT_BUDGET_MAX - slot_calls)),
                                       flat_fn=flat, now_t=now.timestamp(), herder_buys=herder_buys,
                                       own_addresses=own_address_set(os.environ.get("MEMECOIN_OWN_ADDRESSES", "")))
                except Exception as exc:
                    traceback.print_exc()
                    trail["stop"] = "error:%s" % type(exc).__name__
            slot_calls += trail["calls"]
        # The one-off Axiom kol-list read (pre-registration F): after the trail, same 429 rule.
        if getattr(args, "axiom", None):
            if _rate_429() > rate_429_start:
                axiom["stop"] = "skipped (429 earlier in this run)"
            else:
                try:
                    axiom = axiom_pass(graph_path, json.loads(Path(args.axiom).read_text()),
                                       budget=max(0, min(AXIOM_BUDGET, SLOT_BUDGET_MAX - slot_calls)), flat_fn=flat,
                                       ours=ours, tracked=set(owners))
                except Exception as exc:
                    traceback.print_exc()
                    axiom["stop"] = "error:%s" % type(exc).__name__
            slot_calls += axiom["calls"]
        funder_calls, funder_why, funder_new = 0, "skipped (budget 0)", 0
        f_budget = max(0, min(args.funder_budget, SLOT_BUDGET_MAX - slot_calls))
        if f_budget:
            companions = {c["trader"] for r in load_reads(graph_path, since) for c in r["companions"]}
            # The roster first, then the wallets slice 3b cannot test until their funder is read.
            family = [w for w in upstream["funder_wanted"] if w not in owners]
            wanted = sorted(owners) + family + sorted(companions - set(owners) - set(family))
            freads, funder_calls, funder_why = read_funders(
                pending_funders(graph_path, wanted), budget=f_budget, flat_fn=flat,
                fetch=lambda w: helius_funder_fetch(w, flat_fn=flat))
            save_funders(graph_path, freads, read_at=datetime.now(timezone.utc).isoformat())
            funder_new = len(freads)
    finally:
        ledger.close()
    stats = {}
    j7_edges = follows_edges(events, stats=stats) + dumps_on_edges(events)
    gaps = empty_days(events, since, now.isoformat())
    for e in j7_edges:
        e["coverage"]["j7_empty_days"] = gaps
    funders, funded_at = load_funders(graph_path, with_times=True)
    edges = (j7_edges + slot_edges(load_reads(graph_path, since), owners)
             + shared_funder_edges(funders, owners, funded_at))
    written = write_edges(graph_path, edges, computed_at=now.isoformat())
    conn = graph_connection(graph_path)
    with conn:
        conn.execute("INSERT INTO memecoin_wallet_graph_runs (method_version, started_at, finished_at, "
                     "window_from, edges, helius_calls, note) VALUES (?,?,?,?,?,?,?)",
                     (METHOD_VERSION, now.isoformat(), datetime.now(timezone.utc).isoformat(), since,
                      written, slot_calls + funder_calls,
                      "events=%d pairs_tested=%d j7_empty_days=%s fill_reads_new=%d fill_stop=%s slot_reads_new=%d "
                      "slot_stop=%s funders_new=%d funder_stop=%s impact_reads_new=%d impact_stop=%s "
                      "impact_calls=%d upstream_reads_new=%d upstream_stop=%s upstream_calls=%d "
                      "upstream_control=%s upstream_control_fails=%s upstream_status=%s upstream_thin_losers=%d "
                      "upstream_fx_rejected=%d upstream_pruned=%s upstream_wallets=%s coverage_reads_new=%d "
                      "coverage_stop=%s coverage_calls=%d coverage_units=%d coverage_pending=%d coverage_no_signature=%d "
                      "coverage_slot_unknown=%d trail_probe_days=%d trail_stop=%s trail_calls=%d trail_leads=%d "
                      "trail_pending=%d axiom_wallets=%d axiom_stop=%s axiom_calls=%d axiom_reads=%d axiom_pending=%d "
                      "rate_429=%d"
                      % (len(events), stats["pairs_tested"], ",".join(gaps) or "none", fill_new, fill_why, slot_new,
                         slot_why, funder_new, funder_why, impact["reads_new"], impact["stop"], impact["calls"],
                         upstream["reads_new"], upstream["stop"], upstream["calls"], upstream["control"],
                         json.dumps(upstream["control_fails"], sort_keys=True, separators=(",", ":")),
                         json.dumps(upstream["status"], sort_keys=True, separators=(",", ":")),
                         upstream["thin_losers"], upstream["fx_rejected"], upstream["pruned"],
                         "off" if upstream["wallets"] is None else upstream["wallets"], coverage["reads_new"],
                         coverage["stop"], coverage["calls"], coverage.get("units", 0), coverage["pending"],
                         coverage.get("no_signature", 0), coverage["slot_unknown"], trail["probe_days"],
                         trail["stop"], trail["calls"], trail["leads"], trail["pending"], axiom["wallets"],
                         axiom["stop"], axiom["calls"], axiom["reads"], axiom["pending"],
                         _rate_429() - rate_429_start)))
    conn.close()
    kinds = collections.Counter(e["edge_type"] for e in edges)
    flags = collections.Counter(e["flag"] for e in edges if e["flag"])
    # Identity refresh only consumes saved evidence; it must not break a completed graph run.
    try:
        from scripts.research.wallet_identity import refresh_identity
        identity = refresh_identity(ledger_path, graph_path, graph_path.parent / 'wallet_identity.json',
                                    own_addresses=own_address_set(os.environ.get('MEMECOIN_OWN_ADDRESSES','')))
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        identity = {"state": "unavailable", "reason": type(exc).__name__}
        print("wallet identity refresh failed: %s" % exc, file=sys.stderr)
    print(json.dumps({"events": len(events), "follows_pairs_tested": stats["pairs_tested"],
                      "edges": dict(kinds), "flags": dict(flags), "j7_empty_days": gaps,
                      "helius_calls": slot_calls + funder_calls, "slot_reads_new": slot_new, "slot_stop": slot_why,
                      "fill_reads_new": fill_new, "fill_stop": fill_why,
                      "funders_new": funder_new, "funder_stop": funder_why,
                      "impact_reads_new": impact["reads_new"], "impact_stop": impact["stop"],
                      "impact_calls": impact["calls"],
                      "upstream": {k: v for k, v in upstream.items() if k != "funder_wanted"},
                      "hubs": hubs(edges, handles),
                      "identity": identity, "graph": str(graph_path)}))


# ---------------------------------------------------------------- slot edges (Solana; spec section 2)
# co_buy_slot (source -> companion): the companion bought the coin inside the source's own buy slot;
# slot_follower: it landed AFTER the source in that slot (higher transactionIndex). Read around the
# armed wallets' own buys (onchain_wallet_detections) on the research Helius key: one
# getTransactionsForAddress page per read (the mint's succeeded transactions in the source's
# blockTime second), cached per signature in the graph DB. A failed read is 'unread' (censored),
# never "no companions". An edge needs >= 3 coins and must beat a binomial null against the
# companion's own base rate over all read source slots, so a crowd executor is not an edge.
SLOT_BUDGET = 300                        # Helius calls per run (the backlog drains over hours)
SLOT_BUDGET_MAX = 2000                   # Alpha: <= 2,000 per run
SLOT_PAGES = 3

SLOT_READS = "memecoin_wallet_slot_reads"
SLOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS memecoin_wallet_slot_reads (
    signature TEXT PRIMARY KEY,
    source_user_id TEXT,
    network_id TEXT,
    mint TEXT,
    detected_at TEXT,
    status TEXT NOT NULL,
    slot INTEGER,
    idx INTEGER,
    companions TEXT,
    undecoded INTEGER,
    truncated INTEGER,
    read_at TEXT NOT NULL
);
"""


class ReadStop(Exception):
    """A slot read that ends the run (quota, HTTP or RPC error, timeout); the read is 'unread'."""


def _default_sig(tx):
    return tx["transaction"]["signatures"][0]


def _default_trade(tx, mint):
    """trade_of with the companion's quote amount in its own unit (Alpha GO 2026-09-29: follower size
    was censored). trade_of's `sol` is SOL on a curve or a WSOL pool, the USDC amount on a USDC pool,
    and USD at its fixed ETH price on a WETH pool; the unit is named and WETH is converted back."""
    sys.path.insert(0, str(REPO / "scripts" / "research"))
    import dump_forensics as dfx
    t = dfx.trade_of(tx, mint, 1.0)
    if t is None:
        return None
    amount, unit = t.get("sol"), t.get("quote") or "SOL"     # trade_of names the unit it priced in
    if unit == "WETH" and amount is not None:
        amount = amount / dfx.ETH_USD
    return dict(t, amount=amount, unit=unit)


def slot_read(txs, source_sig, mint, *, ours, trade_fn=None, sig_fn=None):
    """{status, slot, index, companions, undecoded}: other BUYERS of `mint` in the source's slot."""
    trade_fn, sig_fn = trade_fn or _default_trade, sig_fn or _default_sig
    src = next((t for t in txs if sig_fn(t) == source_sig), None)
    if src is None:
        return {"status": "not_found"}
    slot, index = src.get("slot"), src.get("transactionIndex")
    mine = trade_fn(src, mint)
    source_trader = mine["trader"] if mine else None
    companions, undecoded = [], 0
    for t in sorted(txs, key=lambda t: t.get("transactionIndex") or 0):
        if t.get("slot") != slot or sig_fn(t) == source_sig or sig_fn(t) in ours:
            continue
        tr = trade_fn(t, mint)
        if tr is None:
            undecoded += 1                        # a lower bound on the companions, by this many
            continue
        if tr["side"] == "buy" and tr["trader"] != source_trader:
            amount = tr.get("amount", tr.get("sol"))
            companions.append({"trader": tr["trader"], "index": t.get("transactionIndex"),
                               "amount": None if amount is None else round(float(amount), 6),
                               "unit": tr.get("unit")})
    return {"status": "ok", "slot": slot, "index": index, "companions": companions, "undecoded": undecoded}


def read_slots(dets, *, fetch, budget, flat_fn, ours, trade_fn=None, sig_fn=None,
               max_calls_per_read=SLOT_PAGES, intake=None):
    """(reads, calls, why stopped). A read starts only if its worst case fits the budget (a hard
    cap), and only while the Solana book is flat: research reads wait while a real Solana position
    or reservation is open. `intake` names what picked the buy ('detection', 'j7')."""
    reads, calls = [], 0
    for det in dets:
        if calls + max_calls_per_read > budget:
            return reads, calls, "budget"
        if not flat_fn():
            return reads, calls, "solana book open"
        base = {k: det.get(k) for k in ("signature", "source_user_id", "mint", "detected_at")}
        base["network_id"], base["intake"] = "solana", intake
        try:
            txs, used, truncated = fetch(det)
        except ReadStop as exc:
            reads.append(dict(base, status="unread", companions=[]))
            return reads, calls, "stopped: %s" % exc
        calls += used
        reads.append(dict(base, truncated=truncated, **slot_read(txs, det["signature"], det["mint"], ours=ours,
                                                                 trade_fn=trade_fn, sig_fn=sig_fn)))
    return reads, calls, "done"


class _NoStop(Exception):
    pass


def helius_slot_fetch(det, *, rpc=None, flat_fn=lambda: True, window=None, pages=SLOT_PAGES):
    """The mint's succeeded transactions in the source's blockTime second: (txs, calls, truncated).
    The book is checked before every page (a read can sit in 429 backoff between pages), a short
    page ends the read (Helius returns a token even then: Gamma LOW 2), and a token left after
    `pages` (SLOT_PAGES unless the caller says otherwise) marks the read truncated (later
    transactions may be missing). A caller's `window` replaces the slot or blockTime filter."""
    stop = _NoStop
    if rpc is None:
        sys.path.insert(0, str(REPO / "scripts" / "research"))
        import funded_exit_truth as fet         # the research key; refuses a funded key (pick_research_url)
        rpc, stop = fet.rpc, fet.Stop
    limit = 100
    # By slot when it is known (a fill's exact source signature), else by the detection's blockTime;
    # a caller's own `window` (slice 3: +-60 s) replaces both.
    if window is None:
        window = ({"slot": {"gte": int(det["slot"]), "lte": int(det["slot"])}} if det.get("slot") is not None
                  else {"blockTime": {"gte": int(det["block_time"]), "lte": int(det["block_time"])}})
    opts = {"transactionDetails": "full", "sortOrder": "asc", "limit": limit, "encoding": "json",
            "maxSupportedTransactionVersion": 1, "filters": dict({"status": "succeeded"}, **window)}
    txs, used = [], 0
    for _ in range(pages):
        if not flat_fn():
            raise ReadStop("solana book open mid-read")
        try:
            res = rpc("getTransactionsForAddress", [det["mint"], opts]) or {}
        except stop as exc:
            raise ReadStop(str(exc)[:160])
        except (TimeoutError, OSError) as exc:
            raise ReadStop("%s: %s" % (type(exc).__name__, str(exc)[:120]))
        used += 1
        page = res.get("data") or []
        txs.extend(page)
        if len(page) < limit or not res.get("paginationToken"):
            return txs, used, False
        opts["paginationToken"] = res["paginationToken"]
    return txs, used, True


def helius_impact_window(mint, t0, *, rpc=None, flat_fn=lambda: True):
    """Slice 3: the mint's succeeded transactions with blockTime in t0 +- 60 s, ascending, at most
    IMPACT_PAGES pages; (txs, calls, truncated)."""
    return helius_slot_fetch({"mint": mint}, rpc=rpc, flat_fn=flat_fn, pages=IMPACT_PAGES,
                             window={"blockTime": {"gte": int(t0) - IMPACT_WINDOW_S,
                                                   "lte": int(t0) + IMPACT_WINDOW_S}})


def helius_first_tx_time(mint, *, rpc=None, flat_fn=lambda: True):
    """Slice 3 age probe (Gamma L1): the blockTime of the mint's FIRST transaction (ascending, limit 1,
    no filter), or None on an empty page (an index gap, never a launch); (block_time, 1)."""
    tx, used = helius_first_tx(mint, rpc=rpc, flat_fn=flat_fn)
    bt = tx.get("blockTime") if tx else None
    return (None if bt is None else int(bt)), used


def helius_first_tx(mint, *, rpc=None, flat_fn=lambda: True):
    """The mint's FIRST transaction (ascending, limit 1, no filter), or None on an empty page; (tx, 1)."""
    stop = _NoStop
    if rpc is None:
        sys.path.insert(0, str(REPO / "scripts" / "research"))
        import funded_exit_truth as fet         # the research key; refuses a funded key (pick_research_url)
        rpc, stop = fet.rpc, fet.Stop
    if not flat_fn():
        raise ReadStop("solana book open mid-read")
    opts = {"transactionDetails": "full", "sortOrder": "asc", "limit": 1, "encoding": "json",
            "maxSupportedTransactionVersion": 1}
    try:
        res = rpc("getTransactionsForAddress", [mint, opts]) or {}
    except stop as exc:
        raise ReadStop(str(exc)[:160])
    except (TimeoutError, OSError) as exc:
        raise ReadStop("%s: %s" % (type(exc).__name__, str(exc)[:120]))
    page = res.get("data") or []
    return (page[0] if page else None), 1


def helius_address_pages(address, *, since_t, pages, rpc=None, flat_fn=lambda: True, enough=None):
    """(txs, calls, cut): the address's transactions, newest first, EVERY status (a failed one costs a
    fee), blockTime >= since_t, at most `pages` pages of 100. `cut` when a token was left: the stretch
    is shorter than asked, and the caller states the hours it covers. `enough(txs)` true after a FULL
    page ends the listing there, and that is a cut too: older history exists."""
    stop = _NoStop
    if rpc is None:
        sys.path.insert(0, str(REPO / "scripts" / "research"))
        import funded_exit_truth as fet         # the research key; refuses a funded key (pick_research_url)
        rpc, stop = fet.rpc, fet.Stop
    opts = {"transactionDetails": "full", "sortOrder": "desc", "limit": 100, "encoding": "json",
            "maxSupportedTransactionVersion": 1, "filters": {"blockTime": {"gte": int(since_t)}}}
    txs, used = [], 0
    for _ in range(pages):
        if not flat_fn():
            raise ReadStop("solana book open mid-read")
        try:
            res = rpc("getTransactionsForAddress", [address, opts]) or {}
        except stop as exc:
            raise ReadStop(str(exc)[:160])
        except (TimeoutError, OSError) as exc:
            raise ReadStop("%s: %s" % (type(exc).__name__, str(exc)[:120]))
        used += 1
        page = res.get("data") or []
        txs.extend(page)
        if len(page) < 100 or not res.get("paginationToken"):
            return txs, used, False
        if enough and enough(txs):
            return txs, used, True
        opts["paginationToken"] = res["paginationToken"]
    return txs, used, True


# ---- fills first (Alpha GO 2026-09-29): the copier-crowd read matched only 25 of 407 funded fills,
# because slot reads were fed only by watcher detections. Each hour the cron first finds the source
# buy of every funded Solana fill that has no read, through the source's own address (the pump:<addr>
# uid, or a proven address), then reads that slot; the detection backlog gets the rest of the budget.
FILL_SCHEMA = """
CREATE TABLE IF NOT EXISTS memecoin_wallet_fill_sources (
    position_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    signature TEXT,
    read_at TEXT NOT NULL
);
"""
FILL_WINDOW_S = 600.0


def find_source_buy(txs, address, mint, *, before):
    """(signature, blockTime) of the address's latest buy of `mint` at or before `before`, or None:
    a transaction that raised the address's balance of the mint."""
    sys.path.insert(0, str(REPO / "scripts" / "research"))
    import dump_forensics as dfx
    best = None
    for tx in txs:
        t = tx.get("blockTime")
        if t is None or t > before or (tx.get("meta") or {}).get("err") is not None:
            continue
        pre, post = dfx.owner_bag(tx, address, mint)
        if post > pre and (best is None or t > best[1]):
            best = (tx["transaction"]["signatures"][0], t)
    return best


def helius_buy_fetch(address, mint, opened_ts, *, rpc=None, flat_fn=lambda: True):
    """((signature, blockTime) or None, calls): one page of the address's transactions in the
    FILL_WINDOW_S before our fill."""
    stop = _NoStop
    if rpc is None:
        sys.path.insert(0, str(REPO / "scripts" / "research"))
        import funded_exit_truth as fet
        rpc, stop = fet.rpc, fet.Stop
    if not flat_fn():
        raise ReadStop("solana book open mid-read")
    lo, hi = int(opened_ts - FILL_WINDOW_S), int(opened_ts)
    opts = {"transactionDetails": "full", "sortOrder": "desc", "limit": 50, "encoding": "json",
            "maxSupportedTransactionVersion": 1,
            "filters": {"status": "succeeded", "blockTime": {"gte": lo, "lte": hi}}}
    try:
        res = rpc("getTransactionsForAddress", [address, opts]) or {}
    except stop as exc:
        raise ReadStop(str(exc)[:160])
    except (TimeoutError, OSError) as exc:
        raise ReadStop("%s: %s" % (type(exc).__name__, str(exc)[:120]))
    return find_source_buy(res.get("data") or [], address, mint, before=hi), 1


def helius_sig_slots(sigs, *, rpc=None, flat_fn=lambda: True):
    """({signature: slot}, calls) for the fills' exact source signatures, 256 per getSignatureStatuses
    call with history search. A signature the node cannot find is absent (the fill falls back)."""
    stop = _NoStop
    if rpc is None:
        sys.path.insert(0, str(REPO / "scripts" / "research"))
        import funded_exit_truth as fet
        rpc, stop = fet.rpc, fet.Stop
    out, calls = {}, 0
    for i in range(0, len(sigs), 256):
        chunk = list(sigs[i:i + 256])
        if not flat_fn():
            raise ReadStop("solana book open mid-read")
        try:
            res = rpc("getSignatureStatuses", [chunk, {"searchTransactionHistory": True}]) or {}
        except stop as exc:
            raise ReadStop(str(exc)[:160])
        except (TimeoutError, OSError) as exc:
            raise ReadStop("%s: %s" % (type(exc).__name__, str(exc)[:120]))
        calls += 1
        for sig, st in zip(chunk, res.get("value") or []):
            if st and st.get("slot") is not None and not st.get("err"):
                out[sig] = int(st["slot"])
    return out, calls


def read_fills(fills, *, addresses, fetch_buy, fetch_slot, budget, flat_fn, ours, trade_fn=None, sig_fn=None,
               slots=None):
    """(slot reads, fill statuses, calls, why stopped). A fill whose exact source signature has a
    known slot is read there directly (Gamma LOW 1: the source may have bought the coin twice);
    otherwise the source's address is searched. A fill starts only if its worst case fits the
    budget; the book is checked before each fill."""
    reads, status, calls = [], [], 0
    sig_fn = sig_fn or _default_sig
    for f in fills:
        uid = f["source_user_id"]
        sig = f.get("source_sig")
        if sig and (slots or {}).get(sig) is not None:
            if calls + SLOT_PAGES > budget:
                return reads, status, calls, "budget"
            if not flat_fn():
                return reads, status, calls, "solana book open"
            try:
                txs, used, truncated = fetch_slot({"mint": f["mint"], "slot": slots[sig], "signature": sig})
            except ReadStop as exc:
                status.append({"position_id": f["id"], "status": "unread"})
                return reads, status, calls, "stopped: %s" % exc
            calls += used
            read = slot_read(txs, sig, f["mint"], ours=ours, trade_fn=trade_fn, sig_fn=sig_fn)
            bt = next((t.get("blockTime") for t in txs if sig_fn(t) == sig), None)
            reads.append(dict(read, signature=sig, source_user_id=uid, mint=f["mint"], network_id="solana",
                              detected_at=_iso(bt) if bt is not None else None, truncated=truncated,
                              intake="fill"))
            status.append({"position_id": f["id"], "status": read["status"], "signature": sig})
            continue
        addrs = [uid[5:]] if uid.startswith("pump:") else list(addresses.get(uid) or [])[:1]
        if not addrs:
            status.append({"position_id": f["id"], "status": "no_address"})
            continue
        if calls + 1 + SLOT_PAGES > budget:
            return reads, status, calls, "budget"
        if not flat_fn():
            return reads, status, calls, "solana book open"
        try:
            buy, used = fetch_buy(addrs[0], f["mint"], _ts(f["opened_at"]))
            calls += used
            if buy is None:
                status.append({"position_id": f["id"], "status": "no_buy_found"})
                continue
            sig, bt = buy
            txs, used, truncated = fetch_slot({"mint": f["mint"], "block_time": bt, "signature": sig})
            calls += used
        except ReadStop as exc:
            status.append({"position_id": f["id"], "status": "unread"})
            return reads, status, calls, "stopped: %s" % exc
        read = slot_read(txs, sig, f["mint"], ours=ours, trade_fn=trade_fn, sig_fn=sig_fn)
        reads.append(dict(read, signature=sig, source_user_id=uid, mint=f["mint"], network_id="solana",
                          detected_at=_iso(bt), truncated=truncated, intake="fill"))
        status.append({"position_id": f["id"], "status": read["status"], "signature": sig})
    return reads, status, calls, "done"


def _fill_db(path):
    conn = graph_connection(path)
    conn.executescript(FILL_SCHEMA)
    return conn


def save_fill_status(path, status, *, read_at):
    conn = _fill_db(path)
    try:
        with conn:
            conn.executemany("INSERT OR REPLACE INTO memecoin_wallet_fill_sources (position_id, status, signature, "
                             "read_at) VALUES (?,?,?,?)",
                             [(s["position_id"], s["status"], s.get("signature"), read_at) for s in status])
    finally:
        conn.close()


def pending_fills(path, fills):
    """Fills never tried, plus 'unread' ones (retried); a final status is never retried."""
    conn = _fill_db(path)
    try:
        done = {r[0] for r in conn.execute(
            "SELECT position_id FROM memecoin_wallet_fill_sources WHERE status != 'unread'")}
    finally:
        conn.close()
    return [f for f in fills if f["id"] not in done]


def fills_without_reads(fills, reads):
    """Fills whose source buy has no ok read yet (same source and mint, read within FILL_WINDOW_S
    before the fill), so a watcher-fed read is never paid for twice."""
    by = collections.defaultdict(list)
    for r in reads:
        if r["status"] == "ok" and r.get("detected_at"):
            by[(r["source_user_id"], r["mint"])].append(_ts(r["detected_at"]))
    out = []
    for f in fills:
        t = _ts(f["opened_at"])
        if not any(t - FILL_WINDOW_S <= d <= t for d in by.get((f["source_user_id"], f["mint"]), [])):
            out.append(f)
    return out


def solana_owners(ledger):
    """{address: source_user_id} of every VERIFIED Solana address, armed now or not: the address
    registry (armed_verified_owners, the slot lane's one definition of watched, including a
    pump:<address> id unless refuted) plus the axiom proofs. watcher_address_owners alone knew 2
    Solana wallets against the registry's 83 (2026-09-29); past fills need disarmed wallets too."""
    from sniper.services.axiom_address_verify import watcher_address_owners
    from sniper.services.memecoin_address_registry import armed_verified_owners
    out = {str(a): str(v[0]) for (n, a), v in watcher_address_owners(ledger).items() if str(n) == "solana"}
    everyone = {str(r[0]) for r in ledger.execute(
        "SELECT source_user_id FROM memecoin_wallets WHERE source_user_id IS NOT NULL")}
    try:
        everyone |= {str(r[0]) for r in ledger.execute(
            "SELECT DISTINCT source_user_id FROM memecoin_address_registry WHERE network_id='solana'")}
    except sqlite3.OperationalError:
        pass                                      # no registry: armed_verified_owners returns None
    out.update({str(a): str(v[0]) for a, v in (armed_verified_owners(ledger, armed=everyone) or {}).items()})
    return out


def solana_book_open(ledger):
    """Open real Solana positions plus reserved Solana entries (copier_crowd_and_fees.open_solana_book).
    A tracking-paused (Long Hold) position does not count: the runner spends no funded reads on it."""
    return (ledger.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                           "AND network_id='solana' AND is_open=1 AND COALESCE(tracking_paused_at,'')=''").fetchone()[0]
            + ledger.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved' "
                             "AND COALESCE(network_id,'solana')='solana'").fetchone()[0]) > 0


def solana_detections(ledger, since_iso):
    """Armed-wallet Solana buys with a chain time, newest first."""
    rows = ledger.execute(
        "SELECT signature, source_user_id, mint, block_time, detected_at FROM onchain_wallet_detections "
        "WHERE network_id='solana' AND block_time IS NOT NULL AND (token_delta IS NULL OR token_delta > 0) "
        "AND detected_at >= ? ORDER BY id DESC", (since_iso,)).fetchall()
    return [dict(zip(("signature", "source_user_id", "mint", "block_time", "detected_at"), r)) for r in rows]


def our_entry_sigs(ledger):
    return {r[0] for r in ledger.execute(
        "SELECT f.tx_sig FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
        "WHERE f.kind='entry' AND p.network_id='solana' AND f.tx_sig IS NOT NULL")}


AXIOM_READS = "memecoin_wallet_axiom_reads"      # the one-off Axiom read's slot reads: nothing else reads them


def _slot_db(path, table=SLOT_READS):
    """The reads table; one made before the coverage intake gains `intake` (NULL = a fill or a
    detection read of an earlier release; slices 3 and 3b derive their origin as before). `table` is
    SLOT_READS or AXIOM_READS, the same columns."""
    conn = graph_connection(path)
    conn.executescript(SLOT_SCHEMA.replace(SLOT_READS, table))
    if "intake" not in {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}:
        conn.execute("ALTER TABLE %s ADD COLUMN intake TEXT" % table)
    return conn


def save_reads(path, reads, *, read_at, table=SLOT_READS):
    conn = _slot_db(path, table)
    try:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO " + table + " (signature, source_user_id, network_id, "
                "mint, detected_at, status, slot, idx, companions, undecoded, truncated, read_at, intake) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r["signature"], r.get("source_user_id"), r.get("network_id"), r.get("mint"),
                  r.get("detected_at"), r["status"], r.get("slot"), r.get("index"),
                  json.dumps(r.get("companions") or []), r.get("undecoded"), int(bool(r.get("truncated"))),
                  read_at, r.get("intake")) for r in reads])
    finally:
        conn.close()


def load_reads(path, since_iso=None, table=SLOT_READS):
    conn = _slot_db(path, table)
    try:
        rows = conn.execute("SELECT signature, source_user_id, network_id, mint, detected_at, status, slot, "
                            "idx, companions, truncated, intake FROM " + table + " "
                            "WHERE detected_at >= ?", (since_iso or "",)).fetchall()
    finally:
        conn.close()
    keys = ("signature", "source_user_id", "network_id", "mint", "detected_at", "status", "slot", "index")
    return [dict(zip(keys, r[:8]), companions=json.loads(r[8] or "[]"), truncated=bool(r[9]), intake=r[10])
            for r in rows]


def pending_detections(path, dets):
    """Detections never read, plus those whose read was 'unread' (censored): retried."""
    conn = _slot_db(path)
    try:
        done = {r[0] for r in conn.execute(
            "SELECT signature FROM memecoin_wallet_slot_reads WHERE status != 'unread'")}
    finally:
        conn.close()
    return [d for d in dets if d["signature"] not in done]


def coverage_pending(path, units):
    """Units still to read, by buy time, newest first, all wallets interleaved (pre-registration A).

    A unit is DONE FOR GOOD when its first-buy transaction has a final read (any intake), or when
    its (wallet, coin) has a final read, of any window and any intake, at or before the unit's buy
    time. The second rule stops the sliding window from substituting: when a read first buy leaves
    the 14 days, the next buy becomes the window's "first buy" and must not be read in its place
    (Gamma MEDIUM 2026-09-30: 36 of 2,570 pairs change first-buy signature per day of slide). An
    'unread' read is retried."""
    conn = _slot_db(path)
    try:
        done, earliest = set(), {}
        for sig, uid, mint, at in conn.execute("SELECT signature, source_user_id, mint, detected_at FROM "
                                               "memecoin_wallet_slot_reads WHERE status != 'unread'"):
            done.add(sig)
            if at:
                key, t = (uid, mint), _ts(at)
                earliest[key] = min(t, earliest.get(key, t))
    finally:
        conn.close()
    return sorted((u for u in units if u["signature"] not in done
                   and earliest.get((u["source_user_id"], u["mint"]), float("inf")) > u["t"]),
                  key=lambda u: -u["t"])


def save_no_signature(path, no_signature):
    """A no_signature unit is a FINAL row for its pair ('nosig:<wallet>:<mint>', status 'no_signature',
    detected_at = the uuid first buy's time), so rule (ii) of coverage_pending holds the pair when that
    buy leaves the window and a later signed buy would otherwise stand in (Gamma, 09e0bd3ff: about 5 a
    day). The first time written is kept. Never an 'ok' read: no edge, flow or herd counts it."""
    conn = _slot_db(path)
    try:
        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO memecoin_wallet_slot_reads (signature, source_user_id, network_id, mint, "
                "detected_at, status, companions, truncated, read_at, intake) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [("nosig:%s:%s" % (u["source_user_id"], u["mint"]), u["source_user_id"], "solana", u["mint"],
                  _iso(u["t"]), "no_signature", "[]", 0, datetime.now(timezone.utc).isoformat(), "j7")
                 for u in no_signature])
    finally:
        conn.close()


def coverage_pass(units, graph_path, *, budget, flat_fn, ours, sig_slots, fetch_slot, trade_fn=None, sig_fn=None,
                  no_signature=()):
    """Overhaul spec section 0 / pre-registration A: one slot read per pending unit. The slots come
    from one status call per 256 signatures, asked only for the units the budget can read at their
    worst case; a signature with no status is a final 'no_slot' row (the pre-registration's
    'slot_unknown'). Reads are tagged 'j7'. `no_signature` units are stored first, whatever the
    budget or the book. {reads_new, calls, stop, pending, slot_unknown}."""
    save_no_signature(graph_path, no_signature)
    pending = coverage_pending(graph_path, units)
    out = {"reads_new": 0, "calls": 0, "stop": "done", "pending": len(pending), "slot_unknown": 0}
    if not pending:
        return out
    n = min(len(pending), max(0, budget) // SLOT_PAGES)
    while n and -(-n // 256) + n * SLOT_PAGES > budget:
        n -= 1
    if not n:
        out["stop"] = "budget"
        return out
    if not flat_fn():
        out["stop"] = "solana book open"
        return out
    take = pending[:n]
    try:
        slots, calls = sig_slots([u["signature"] for u in take])
    except ReadStop as exc:
        out["stop"] = "stopped: %s" % exc
        return out
    missing = [dict(u, network_id="solana", status="no_slot", intake="j7", detected_at=_iso(u["t"]))
               for u in take if u["signature"] not in slots]
    dets = [dict(u, slot=slots[u["signature"]], detected_at=_iso(u["t"])) for u in take if u["signature"] in slots]
    reads, used, why = read_slots(dets, fetch=fetch_slot, budget=budget - calls, flat_fn=flat_fn, ours=ours,
                                  trade_fn=trade_fn, sig_fn=sig_fn, intake="j7")
    save_reads(graph_path, missing + reads, read_at=datetime.now(timezone.utc).isoformat())
    out.update(reads_new=len(reads), calls=calls + used, slot_unknown=len(missing),
               stop=why if why != "done" else ("budget" if n < len(pending) else "done"))
    return out


# ---------------------------------------------------------------- bot trail and best herd member
# Pre-registration docs/audits/2026-09-30-wallet-map-reads-preregistration.md C and D, with Gamma's
# answers of 2026-09-30: 42 + 10 calls per probe-day (C's frame is D's first two pages, succeeded
# only), the creator read once per mint per run, launch-bundle wallets only from the probe buy's own
# window pages, a failed fee charged to the one stretch coin in its keys. Rules: wallet_probes.py.
TRAIL_BUDGET = 150
TRAIL_STRETCH_S = 86400
TRAIL_LIST_PAGES = 10
TRAIL_FRAME_TXS = 200
TRAIL_WINDOW_PAGES = 2                     # Gamma LOW 1: the pre-registered worst case is 42 calls
TRAIL_WORST = TRAIL_LIST_PAGES + 10 * (1 + TRAIL_WINDOW_PAGES)   # 40 calls: one probe-day at its worst
TRAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS memecoin_wallet_probe_days (
    probe TEXT NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL, seed INTEGER, calls INTEGER,
    stretch_from INTEGER, stretch_to INTEGER, list_pages INTEGER, cut INTEGER, txs INTEGER, failed_n INTEGER,
    unattributed_fee_sol REAL, closed INTEGER, open INTEGER, partial INTEGER, unpriced INTEGER, net_sol REAL,
    median_net_sol REAL, gain_share REAL, median_hold_s REAL, profitable INTEGER, sells_into_herd INTEGER,
    sampled INTEGER, frozen_at TEXT, read_at TEXT NOT NULL, PRIMARY KEY (probe, day)
);
CREATE TABLE IF NOT EXISTS memecoin_wallet_probe_coins (
    probe TEXT NOT NULL, day TEXT NOT NULL, mint TEXT NOT NULL, status TEXT NOT NULL, net_sol REAL,
    buys INTEGER, sells INTEGER, first_buy INTEGER, last_sell INTEGER, fees_failed_sol REAL,
    PRIMARY KEY (probe, day, mint)
);
CREATE TABLE IF NOT EXISTS memecoin_wallet_probe_leads (
    probe TEXT NOT NULL, day TEXT NOT NULL, mint TEXT NOT NULL, buy_sig TEXT NOT NULL, leader TEXT NOT NULL,
    PRIMARY KEY (probe, day, buy_sig, leader)
);
"""


def _trail_db(path):
    conn = graph_connection(path)
    conn.executescript(TRAIL_SCHEMA)
    return conn


def _seed(probe, day):
    return int(hashlib.sha256(("%s|%s" % (probe, day)).encode()).hexdigest()[:8], 16)


def trail_pass(graph_path, frozen, *, budget, flat_fn, own_addresses, now_t, herder_buys=None, fetch_pages=None,
               fetch_first=None, fetch_window=None):
    """One read per frozen probe per UTC day, while the budget holds its worst case (TRAIL_WORST):

    - D: the newest 24 h of the probe's history, every status, at most 10 pages; its round trips and
      the profitable verdict are stored per probe-day, the covered hours when the pages ran out;
    - C: 10 of its succeeded buys among the newest 200 transactions, drawn with a seed from
      (probe, day); for each, the coin's creator (cached per mint for the run) and the coin's
      transactions in the probe's slot and the 10 s before; the leaders are stored.

    `frozen` is Gamma's frozen list ({"frozen_at", "herders", "probes"}): without it nothing runs.
    A failed read stores the probe-day as 'unread' (retried) and stops the pass."""
    from scripts.research import wallet_probes as wp
    probes = sorted(set((frozen or {}).get("probes") or []))
    out = {"probe_days": 0, "calls": 0, "stop": "done", "leads": 0, "pending": 0}
    if not probes or not (frozen or {}).get("frozen_at"):
        out["stop"] = "no frozen list"
        return out
    fetch_pages = fetch_pages or (lambda a, since_t: helius_address_pages(
        a, since_t=since_t, pages=TRAIL_LIST_PAGES, flat_fn=flat_fn))
    fetch_first = fetch_first or (lambda mint: helius_first_tx(mint, flat_fn=flat_fn))
    fetch_window = fetch_window or (lambda mint, bt: helius_slot_fetch(
        {"mint": mint}, flat_fn=flat_fn, pages=TRAIL_WINDOW_PAGES,
        window={"blockTime": {"gte": int(bt) - wp.LEAD_S, "lte": int(bt)}}))
    day = datetime.fromtimestamp(now_t, timezone.utc).date().isoformat()
    conn = _trail_db(graph_path)
    try:
        done = {r[0] for r in conn.execute("SELECT probe FROM memecoin_wallet_probe_days WHERE day=? "
                                           "AND status != 'unread'", (day,))}
        todo = [pr for pr in probes if pr not in done]
        out["pending"] = len(todo)
        creators, excluded_base = {}, set(probes) | set(own_addresses)
        for probe in todo:
            if out["calls"] + TRAIL_WORST > budget:
                out["stop"] = "budget"
                break
            if not flat_fn():
                out["stop"] = "solana book open"
                break
            seed, leads, start_calls = _seed(probe, day), [], out["calls"]
            try:
                txs, list_pages, cut = fetch_pages(probe, now_t - TRAIL_STRETCH_S)
                out["calls"] += list_pages
                sampled = wp.sample_buys(txs[:TRAIL_FRAME_TXS], probe, seed=seed)
                for buy in sampled:
                    (_order, bt, mint, sig) = buy
                    if mint not in creators:
                        creators[mint], used = fetch_first(mint)
                        out["calls"] += used
                    window, used, _truncated = fetch_window(mint, bt)
                    out["calls"] += used
                    excluded = excluded_base | wp.launch_excluded(creators[mint], window, mint)
                    leads += [(mint, sig, w) for w in sorted(wp.leaders(window, buy, excluded=excluded))]
            except ReadStop as exc:
                with conn:
                    conn.execute("INSERT OR REPLACE INTO memecoin_wallet_probe_days (probe, day, status, seed, calls, "
                                 "frozen_at, read_at) VALUES (?,?,?,?,?,?,?)",
                                 (probe, day, "unread", seed, out["calls"] - start_calls, frozen["frozen_at"],
                                  datetime.now(timezone.utc).isoformat()))
                out["stop"] = "stopped: %s" % exc
                break
            rows, unattributed, failed_n = wp.round_trips(txs, probe)
            v = wp.profitable(rows, unattributed)
            times = [t.get("blockTime") for t in txs if t.get("blockTime") is not None]
            stretch_from = min(times) if cut and times else int(now_t - TRAIL_STRETCH_S)
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO memecoin_wallet_probe_days (probe, day, status, seed, calls, stretch_from, "
                    "stretch_to, list_pages, cut, txs, failed_n, unattributed_fee_sol, closed, open, partial, unpriced, "
                    "net_sol, median_net_sol, gain_share, median_hold_s, profitable, sells_into_herd, sampled, frozen_at, "
                    "read_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (probe, day, "ok", seed, out["calls"] - start_calls, stretch_from, int(now_t), list_pages,
                     int(bool(cut)), len(txs), failed_n,
                     unattributed, v["closed"], v["open"], v["partial"], v["unpriced"], v["net_sol"],
                     v["median_net_sol"], v["gain_share"], v["median_hold_s"], int(v["profitable"]),
                     wp.sells_into(txs, probe, herder_buys or {}), len(sampled), frozen["frozen_at"],
                     datetime.now(timezone.utc).isoformat()))
                conn.executemany(
                    "INSERT OR REPLACE INTO memecoin_wallet_probe_coins (probe, day, mint, status, net_sol, buys, sells, "
                    "first_buy, last_sell, fees_failed_sol) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(probe, day, m, r["status"], r["net_sol"], r["buys"], r["sells"], r["first_buy"], r["last_sell"],
                      r["fees_failed_sol"]) for m, r in rows.items()])
                conn.executemany("INSERT OR IGNORE INTO memecoin_wallet_probe_leads (probe, day, mint, buy_sig, leader) "
                                 "VALUES (?,?,?,?,?)", [(probe, day, m, sg, w) for m, sg, w in leads])
            out["probe_days"] += 1
            out["leads"] += len(leads)
    finally:
        conn.close()
    return out


# ---------------------------------------------------------------- Axiom kol-list read (one-off)
# Pre-registration F: each kol-list wallet (seed: scripts/research/axiom_kol_seed.py), in 7-day rank
# order: its newest transactions until 20 first buys of distinct coins or 3 list calls, then one slot
# read per coin at that first buy. The reads go to AXIOM_READS: no edge, flow, herd or coverage count
# ever reads them. A wallet is read once; an 'unread' one is retried.
AXIOM_BUDGET = 150
AXIOM_PAGES = 3
AXIOM_COINS = 20
AXIOM_WORST = AXIOM_PAGES + AXIOM_COINS * SLOT_PAGES    # 63 calls: one wallet at its worst
AXIOM_SCHEMA = """
CREATE TABLE IF NOT EXISTS memecoin_wallet_axiom_wallets (
    address TEXT PRIMARY KEY, handle TEXT, rank_7d INTEGER, lists TEXT, tracked INTEGER, status TEXT NOT NULL,
    coins INTEGER, list_pages INTEGER, list_cut INTEGER, prior_positions INTEGER, calls INTEGER,
    read_at TEXT NOT NULL
);
"""


def axiom_pass(graph_path, seed, *, budget, flat_fn, ours, tracked, fetch_pages=None, fetch_slot=None, trade_fn=None,
               sig_fn=None):
    """{wallets, calls, stop, reads, pending}. `seed` = {"wallets": [{address, handle, rank_7d, lists}]} in
    read order; `tracked` = addresses we already follow (labelled, never dropped)."""
    from scripts.research import wallet_probes as wp
    fetch_pages = fetch_pages or (lambda a: helius_address_pages(
        a, since_t=0, pages=AXIOM_PAGES, flat_fn=flat_fn,
        enough=lambda txs: len(wp.first_buys(txs, a, AXIOM_COINS)[0]) >= AXIOM_COINS))
    fetch_slot = fetch_slot or (lambda det: helius_slot_fetch(det, flat_fn=flat_fn))
    out = {"wallets": 0, "calls": 0, "stop": "done", "reads": 0, "pending": 0}
    conn = graph_connection(graph_path)
    conn.executescript(AXIOM_SCHEMA)
    try:
        done = {r[0] for r in conn.execute("SELECT address FROM memecoin_wallet_axiom_wallets WHERE status != 'unread'")}
        todo = [w for w in (seed or {}).get("wallets") or [] if w["address"] not in done]
        out["pending"] = len(todo)
        for w in todo:
            addr, reads, start = w["address"], [], out["calls"]
            if out["calls"] + AXIOM_WORST > budget:
                out["stop"] = "budget"
                break
            if not flat_fn():
                out["stop"] = "solana book open"
                break
            status, list_cut, prior = "ok", None, []
            try:
                txs, list_pages, list_cut = fetch_pages(addr)
                out["calls"] += list_pages
                buys, prior = wp.first_buys(txs, addr, AXIOM_COINS)
                for (slot, _idx), bt, mint, sig in buys:
                    got, used, truncated = fetch_slot({"mint": mint, "slot": slot, "signature": sig})
                    out["calls"] += used
                    reads.append(dict(slot_read(got, sig, mint, ours=ours, trade_fn=trade_fn, sig_fn=sig_fn),
                                      signature=sig, source_user_id=addr, mint=mint, network_id="solana",
                                      detected_at=_iso(bt), truncated=truncated, intake="axiom"))
            except ReadStop as exc:
                status, list_pages = "unread", None
                out["stop"] = "stopped: %s" % exc
            now_iso = datetime.now(timezone.utc).isoformat()
            if status == "ok":
                save_reads(graph_path, reads, read_at=now_iso, table=AXIOM_READS)
            with conn:
                conn.execute("INSERT OR REPLACE INTO memecoin_wallet_axiom_wallets (address, handle, rank_7d, lists, "
                             "tracked, status, coins, list_pages, list_cut, prior_positions, calls, read_at) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (addr, w.get("handle"), w.get("rank_7d"), ",".join(w.get("lists") or []),
                              int(addr in tracked), status, len(reads) if status == "ok" else None, list_pages,
                              None if list_cut is None else int(bool(list_cut)),
                              len(prior) if status == "ok" else None, out["calls"] - start, now_iso))
            if status != "ok":
                break
            out["wallets"] += 1
            out["reads"] += len(reads)
    finally:
        conn.close()
    return out


def _after(read, comp):
    return read["index"] is not None and comp["index"] is not None and comp["index"] > read["index"]


def slot_edges(reads, owners):
    """co_buy_slot and slot_follower edges from cached reads; `owners` maps a proven roster address to
    its source_user_id, so a roster companion joins the follows identity."""
    status = collections.Counter(r["status"] for r in reads)
    status.update({"truncated": sum(1 for r in reads if r.get("truncated"))})
    status = {k: v for k, v in status.items() if v}
    intakes = dict(collections.Counter(r.get("intake") or "legacy" for r in reads if r["status"] == "ok"))
    method = SLOT_COVERAGE_METHOD if "j7" in intakes else METHOD_VERSION
    ok = [r for r in reads if r["status"] == "ok"]
    ident = lambda addr: owners.get(addr, addr)          # noqa: E731
    base = {"co_buy_slot": collections.Counter(), "slot_follower": collections.Counter()}
    for r in ok:
        base["co_buy_slot"].update({ident(c["trader"]) for c in r["companions"]})
        base["slot_follower"].update({ident(c["trader"]) for c in r["companions"] if _after(r, c)})
    by_src = collections.defaultdict(list)
    for r in ok:
        by_src[(r["network_id"], r["source_user_id"])].append(r)
    edges = []
    for (network, src), rs in by_src.items():
        n = len({r["mint"] for r in rs})
        for edge_type in ("co_buy_slot", "slot_follower"):
            hits = collections.defaultdict(dict)                       # dst -> {mint: (read, comp)}
            for r in rs:
                for c in r["companions"]:
                    dst = ident(c["trader"])
                    if dst != src and (edge_type == "co_buy_slot" or _after(r, c)):
                        hits[dst].setdefault(r["mint"], (r, c))
            for dst, coins in hits.items():
                k = len(coins)
                if k < MIN_COINS:
                    continue
                q = base[edge_type][dst] / len(ok)
                p = round(binomial_tail(k, n, q), 6)
                if p > ALPHA_P:
                    continue
                times = sorted(_ts(r["detected_at"]) for r, _c in coins.values())
                e = _edge(network, edge_type, src, dst, n_coins=k, n_consistent=k, lags=[], null_p99=None,
                          null_p=p, first=times[0], last=times[-1],
                          null={"test": "binomial", "n": n, "k": k, "q": round(q, 6), "p": p},
                          evidence=[{"mint": m, "slot": r["slot"], "src_index": r["index"], "dst_index": c["index"]}
                                    for m, (r, c) in sorted(coins.items())[:EVIDENCE_MAX]])
                e["dst_user_id"] = dst if dst in owners.values() else None
                e["method_version"] = method
                e["coverage"] = dict(COVERAGE, source="onchain_wallet_detections + Helius getTransactionsForAddress",
                                     clock="slot order (transactionIndex)",
                                     sample="our wallets' Solana buys: our fills, the watcher's detections "
                                            "(armed wallets) and, with --coverage, every relay-reported "
                                            "buy that carries its transaction; companions are any wallet",
                                     decoding="dump_forensics.trade_of: aggregator-routed swaps are often "
                                              "undecoded, so companions are a lower bound",
                                     slot_reads=status, slot_intakes=intakes)
                edges.append(e)
    return edges


# ---------------------------------------------------------------- funded_by / shared_funder (spec section 3)
# The funder of a Solana wallet is the fee payer of the first transaction that takes it from 0 to a
# positive SOL balance, read from ONE Helius getTransactionsForAddress page of its oldest
# transactions (memecoin_cluster_rpc.trace_first_funder pages getSignaturesForAddress up to 20 x
# 1,000 and gives up on active traders). Cached forever per wallet. A failed read is 'unread'
# (censored), never "self-funded". shared_funder links two of our wallets with the same funder; a
# funder of more than HUB_FUNDED of our wallets is flagged as a hub (likely an exchange), no edges.
FUNDER_METHOD = "funder-v1:first-credit-sender:gtfa-asc20"
FUNDER_BUDGET = 50
HUB_FUNDED = 3

FUNDER_SCHEMA = """
CREATE TABLE IF NOT EXISTS memecoin_wallet_funders (
    wallet TEXT PRIMARY KEY,
    network_id TEXT NOT NULL DEFAULT 'solana',
    status TEXT NOT NULL,
    funder TEXT,
    payer TEXT,
    funding_sig TEXT,
    funded_at INTEGER,
    read_at TEXT NOT NULL
);
"""


def first_funder(txs, wallet):
    """{status, funder, funding_sig, funded_at} from the wallet's oldest transactions (ascending)."""
    if not txs:
        return {"status": "no_history"}
    for tx in txs:
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            continue
        keys = list(tx["transaction"]["message"].get("accountKeys") or [])
        keys += list((meta.get("loadedAddresses") or {}).get("writable") or [])
        if wallet not in keys:
            continue
        i = keys.index(wallet)
        pres, posts = meta.get("preBalances") or [], meta.get("postBalances") or []
        pre, post = pres[i:i + 1], posts[i:i + 1]
        if not pre or not post:
            continue
        if pre[0] == 0 and post[0] > 0 and keys[0] != wallet:
            # The funder is the account that SENT the SOL (largest lamport decrease), not the fee
            # payer: a relayed or program transfer has a payer that sent nothing (Gamma LOW).
            drops = [(posts[j] - pres[j], keys[j]) for j in range(min(len(keys), len(pres), len(posts)))
                     if keys[j] != wallet]
            funder = min(drops)[1] if drops and min(drops)[0] < 0 else keys[0]
            return {"status": "resolved", "funder": funder, "payer": keys[0], "payer_differs": funder != keys[0],
                    "funding_sig": tx["transaction"]["signatures"][0], "funded_at": tx.get("blockTime")}
        if pre[0] > 0:
            break                                   # history starts funded: the funding tx is not in it
    return {"status": "no_funding_tx"}


def read_funders(wallets, *, fetch, budget, flat_fn):
    """(reads, calls, why stopped), one Helius call per wallet, flat-gated like the slot reads."""
    reads, calls = [], 0
    for w in wallets:
        if calls + 1 > budget:
            return reads, calls, "budget"
        if not flat_fn():
            return reads, calls, "solana book open"
        try:
            txs, used = fetch(w)
        except ReadStop as exc:
            reads.append({"wallet": w, "status": "unread"})
            return reads, calls, "stopped: %s" % exc
        calls += used
        reads.append(dict(first_funder(txs, w), wallet=w))
    return reads, calls, "done"


def helius_funder_fetch(wallet, *, rpc=None, flat_fn=lambda: True):
    """The wallet's 20 oldest succeeded transactions: (txs, calls)."""
    stop = _NoStop
    if rpc is None:
        sys.path.insert(0, str(REPO / "scripts" / "research"))
        import funded_exit_truth as fet
        rpc, stop = fet.rpc, fet.Stop
    if not flat_fn():
        raise ReadStop("solana book open mid-read")
    opts = {"transactionDetails": "full", "sortOrder": "asc", "limit": 20, "encoding": "json",
            "maxSupportedTransactionVersion": 1, "filters": {"status": "succeeded"}}
    try:
        res = rpc("getTransactionsForAddress", [wallet, opts]) or {}
    except stop as exc:
        raise ReadStop(str(exc)[:160])
    except (TimeoutError, OSError) as exc:
        raise ReadStop("%s: %s" % (type(exc).__name__, str(exc)[:120]))
    return res.get("data") or [], 1


def _funder_db(path):
    conn = graph_connection(path)
    conn.executescript(FUNDER_SCHEMA)
    return conn


def save_funders(path, reads, *, read_at):
    conn = _funder_db(path)
    try:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO memecoin_wallet_funders (wallet, status, funder, payer, funding_sig, "
                "funded_at, read_at) VALUES (?,?,?,?,?,?,?)",
                [(r["wallet"], r["status"], r.get("funder"), r.get("payer"), r.get("funding_sig"),
                  r.get("funded_at"), read_at) for r in reads])
    finally:
        conn.close()


def load_funders(path, with_times=False):
    conn = _funder_db(path)
    try:
        rows = conn.execute("SELECT wallet, funder, funded_at FROM memecoin_wallet_funders "
                            "WHERE status='resolved'").fetchall()
    finally:
        conn.close()
    if with_times:
        return {w: f for w, f, _t in rows}, {w: t for w, _f, t in rows if t}
    return {w: f for w, f, _t in rows}


def pending_funders(path, wallets):
    conn = _funder_db(path)
    try:
        done = {r[0] for r in conn.execute("SELECT wallet FROM memecoin_wallet_funders WHERE status != 'unread'")}
    finally:
        conn.close()
    return [w for w in wallets if w not in done]


def shared_funder_edges(funders, owners, funded_at=None):
    """One symmetric shared_funder edge per pair with the same first funder, only where at least one
    side is a ROSTER wallet (Gamma MED: an exchange funding two random slot companions is not a
    fact about us). Companions are still resolved: they make the hub count better. Degree and hub
    are counted by identity over every resolved wallet."""
    roster = set(owners.values())
    by = collections.defaultdict(set)
    when = {}
    for wallet, funder in funders.items():
        ident = owners.get(wallet, wallet)
        by[funder].add(ident)
        t = (funded_at or {}).get(wallet)
        if t:
            when[ident] = t
    resolved = len({owners.get(w, w) for w in funders})
    hubs = {f: len(ws) for f, ws in by.items() if len(ws) > HUB_FUNDED}
    edges = []
    for funder, ws in by.items():
        if funder in hubs or len(ws) < 2:
            continue
        ws = sorted(ws)
        for i, a in enumerate(ws):
            for b in ws[i + 1:]:
                if a not in roster and b not in roster:
                    continue
                times = [when[x] for x in (a, b) if x in when]
                e = _edge("solana", "shared_funder", a, b, n_coins=1, n_consistent=1, lags=[], null_p99=None,
                          first=min(times) if times else 0, last=max(times) if times else 0, evidence=[])
                if not times:
                    e["first_seen"] = e["last_seen"] = "unknown"
                e["src_user_id"] = a if a in roster else None
                e["dst_user_id"] = b if b in roster else None
                e["evidence"] = {"funder": funder, "funder_degree": len(ws), "resolved_wallets": resolved,
                                 "method": FUNDER_METHOD}
                e["coverage"] = dict(COVERAGE, source="Helius getTransactionsForAddress (oldest 20)",
                                     sample="our roster's proven Solana addresses and slot companions",
                                     hub_funders=hubs)
                edges.append(e)
    return edges


# ---------------------------------------------------------------- flow change around a source buy
# Wallet Map slice 3 (spec docs/superpowers/specs/2026-09-30-memecoin-wallet-map-slice3-impact-design.md,
# Gamma CLEAN 45c9385ae). One read per source buy (the first ok slot read per source and mint): other
# traders' net SOL flow 60 s after minus 60 s before, split by chain order. Descriptive, not causal;
# nothing here arms, sizes, admits or exits. Research key only, and only while the Solana book is flat.
IMPACT_METHOD = "impact-v1:w60:sol:first-buy:det-origin:age60:show10:test20:cens25:loo-median:bh05"
IMPACT_WINDOW_S = 60
IMPACT_PAGES = 10
IMPACT_CALLS_MAX = 1 + IMPACT_PAGES      # the age probe plus the window pages: a read's worst case
IMPACT_BUDGET = 300                      # Helius calls per run for these reads
UPSTREAM_BUDGET = 150                    # slice 3b: Helius calls per run (wallet_upstream.UPSTREAM_BUDGET)
LAUNCH_AGE_S = 60                        # the mint's first transaction this close to t0: a launch read
PROBE_EMPTY_MAX = 3                      # Gamma LOW (build): an index gap is retried 3 runs, then censored
SHOW_N, TEST_N, CENSORED_MAX, BH_Q = 10, 20, 0.25, 0.05
CENSORED = ("truncated", "not_found", "clock_mismatch", "non_sol", "probe_empty")


def impact_read(txs, source_sig, t0, mint, *, excluded, ours, trade_fn=None, sig_fn=None):
    """Other traders' net SOL flow in the IMPACT_WINDOW_S before and after the source transaction, split
    by chain order (slot, transactionIndex). Status ok, not_found, clock_mismatch or non_sol; the numbers
    exist only when ok. `excluded` holds the source's addresses and ours (the source transaction's own
    trader is added here); a transaction in `ours` (our fill signatures) is dropped entirely."""
    trade_fn, sig_fn = trade_fn or _default_trade, sig_fn or _default_sig
    src = next((t for t in txs if sig_fn(t) == source_sig), None)
    if src is None:
        return {"status": "not_found"}
    if src.get("blockTime") != t0:
        return {"status": "clock_mismatch"}

    def order(t):
        return (t.get("slot") or 0, t.get("transactionIndex") or 0)
    mine = trade_fn(src, mint)
    skip = set(excluded) | ({mine["trader"]} if mine else set())
    net, buyers = {"before": 0.0, "after": 0.0}, {"before": set(), "after": set()}
    n_tx, undecoded = {"before": 0, "after": 0}, {"before": 0, "after": 0}
    for t in txs:
        sig, bt = sig_fn(t), t.get("blockTime")
        if sig == source_sig or sig in ours or bt is None:
            continue
        if order(t) < order(src) and bt >= t0 - IMPACT_WINDOW_S:
            side = "before"
        elif order(t) > order(src) and bt <= t0 + IMPACT_WINDOW_S:
            side = "after"
        else:
            continue
        n_tx[side] += 1
        tr = trade_fn(t, mint)
        if tr is None:
            undecoded[side] += 1
            continue
        if tr.get("unit") != "SOL":
            return {"status": "non_sol"}
        if tr["trader"] in skip:
            continue
        amount = tr.get("amount", tr.get("sol"))
        if amount is None:
            undecoded[side] += 1                  # a trade without a size is not a zero
            continue
        if tr["side"] == "buy":
            net[side] += float(amount)
            buyers[side].add(tr["trader"])
        else:
            net[side] -= float(amount)
    return {"status": "ok", "net_before": round(net["before"], 9), "net_after": round(net["after"], 9),
            "delta_sol": round(net["after"] - net["before"], 9),
            "buyers_before": len(buyers["before"]), "buyers_after": len(buyers["after"]),
            "tx_before": n_tx["before"], "tx_after": n_tx["after"],
            "undecoded_before": undecoded["before"], "undecoded_after": undecoded["after"]}


def read_impacts(units, *, fetch_age, fetch_window, budget, flat_fn, excluded_for, ours, trade_fn=None,
                 sig_fn=None):
    """(reads, calls, why stopped). Per unit: the age probe first -- an empty page is unread (index gap),
    probe_empty after PROBE_EMPTY_MAX empty runs; age <= LAUNCH_AGE_S is launch and skips the window.
    Then the window: truncated wins over not_found (a cut page can miss the source). A read starts only
    if its worst case fits the budget and while the Solana book is flat; a ReadStop records the read
    unread and ends the pass. A failed call's quota use is not counted, and a ReadStop INSIDE the
    window read also drops the pages that read had already fetched (up to IMPACT_PAGES - 1), so a
    run can pass its budget by that much: the same as the slot reads (Gamma LOW)."""
    reads, calls = [], 0
    for u in units:
        if calls + IMPACT_CALLS_MAX > budget:
            return reads, calls, "budget"
        if not flat_fn():
            return reads, calls, "solana book open"
        base = {k: u[k] for k in ("signature", "source_user_id", "mint", "origin", "t0")}
        base.update(network_id="solana", empty_probes=int(u.get("empty_probes") or 0))
        used = 0
        try:
            first, n = fetch_age(u)
            used += n
            age = None if first is None else int(u["t0"]) - int(first)
            if age is None or age < 0:
                # An empty page, or a "first" transaction AFTER the source buy (Gamma LOW): either
                # way the probe did not return the mint's first transaction. An index fault, never
                # a launch: unread, then probe_empty after PROBE_EMPTY_MAX runs.
                empties = base["empty_probes"] + 1
                reads.append(dict(base, status="probe_empty" if empties >= PROBE_EMPTY_MAX else "unread",
                                  empty_probes=empties, calls=used))
                calls += used
                continue
            if age <= LAUNCH_AGE_S:
                reads.append(dict(base, status="launch", age_s=age, calls=used))
                calls += used
                continue
            txs, n, truncated = fetch_window(u)
            used += n
        except ReadStop as exc:
            reads.append(dict(base, status="unread", calls=used))
            return reads, calls + used, "stopped: %s" % exc
        calls += used
        if truncated:
            reads.append(dict(base, status="truncated", age_s=age, calls=used))
            continue
        reads.append(dict(base, age_s=age, calls=used,
                          **impact_read(txs, u["signature"], int(u["t0"]), u["mint"],
                                        excluded=excluded_for(u["source_user_id"]), ours=ours,
                                        trade_fn=trade_fn, sig_fn=sig_fn)))
    return reads, calls, "done"


IMPACT_SCHEMA = """
CREATE TABLE IF NOT EXISTS memecoin_wallet_impact_reads (
    signature TEXT PRIMARY KEY,
    source_user_id TEXT,
    network_id TEXT,
    mint TEXT,
    origin TEXT NOT NULL,
    t0 INTEGER,
    age_s INTEGER,
    status TEXT NOT NULL,
    delta_sol REAL,
    net_before REAL,
    net_after REAL,
    buyers_before INTEGER,
    buyers_after INTEGER,
    tx_before INTEGER,
    tx_after INTEGER,
    undecoded_before INTEGER,
    undecoded_after INTEGER,
    empty_probes INTEGER NOT NULL DEFAULT 0,
    calls INTEGER,
    method_version TEXT NOT NULL,
    read_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memecoin_wallet_impact (
    source_user_id TEXT NOT NULL,
    network_id TEXT NOT NULL,
    n INTEGER NOT NULL,
    median_delta_sol REAL,
    p25_delta_sol REAL,
    p75_delta_sol REAL,
    share_positive REAL,
    median_delta_buyers REAL,
    median_undecoded_share REAL,
    censored TEXT NOT NULL,
    censored_share REAL,
    n_fill INTEGER NOT NULL,
    median_delta_sol_fill REAL,
    k INTEGER,
    k_needed INTEGER,
    p_value REAL,
    q_value REAL,
    verdict TEXT NOT NULL,
    verdict_reason TEXT,
    window_from TEXT,
    computed_at TEXT NOT NULL,
    method_version TEXT NOT NULL,
    PRIMARY KEY (source_user_id, network_id, method_version)
);
"""
READ_COLS = ("signature", "source_user_id", "network_id", "mint", "origin", "t0", "age_s", "status",
             "delta_sol", "net_before", "net_after", "buyers_before", "buyers_after", "tx_before", "tx_after",
             "undecoded_before", "undecoded_after", "empty_probes", "calls")


def detection_block_times(ledger, sigs):
    """{signature: block_time} of watcher detections with a chain time; 500 per query on the unique
    signature index."""
    out, sigs = {}, list(sigs)
    for i in range(0, len(sigs), 500):
        chunk = sigs[i:i + 500]
        out.update({r[0]: int(r[1]) for r in ledger.execute(
            "SELECT signature, block_time FROM onchain_wallet_detections WHERE signature IN (%s) "
            "AND block_time IS NOT NULL" % ",".join("?" * len(chunk)), chunk)})
    return out


def impact_units(slot_reads, block_times):
    """One unit per (source_user_id, mint): its first ok Solana slot read by chain time. origin
    'detection' when the watcher saw the signature with a chain time (our gates did not select it),
    else 'fill'. t0: the detection's block_time; else detected_at, which only fill-fed reads reach and
    which they store as the page blockTime (a detection-fed detected_at is HOST time, never t0)."""
    best = {}
    for r in slot_reads:
        if r["status"] != "ok" or r.get("network_id") != "solana" or not r.get("detected_at"):
            continue
        if r.get("intake") == "j7":
            continue                        # coverage reads: impact-v1/upstream-v1 do not name this origin
        sig = r["signature"]
        det = block_times.get(sig)
        u = {"signature": sig, "source_user_id": r["source_user_id"], "mint": r["mint"],
             "origin": "detection" if det is not None else "fill",
             "t0": int(det) if det is not None else int(_ts(r["detected_at"])),
             "slot": r.get("slot") or 0, "idx": r.get("index") or 0}
        key = (u["source_user_id"], u["mint"])
        if key not in best or (u["t0"], u["slot"], u["idx"]) < (best[key]["t0"], best[key]["slot"], best[key]["idx"]):
            best[key] = u
    return list(best.values())


def our_fill_sigs(ledger):
    """Every Solana fill signature of ours, entries AND exits: our own sells never read as other
    traders' flow (our_entry_sigs covers entries only)."""
    return {r[0] for r in ledger.execute(
        "SELECT f.tx_sig FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
        "WHERE p.network_id='solana' AND f.tx_sig IS NOT NULL")}


def excluded_traders(owners, own_addresses):
    """uid -> the addresses whose trades are never market flow around that source's buy: its verified
    Solana addresses (solana_owners), its pump:<address> id, and our own addresses."""
    by_uid = collections.defaultdict(set)
    for address, uid in owners.items():
        by_uid[uid].add(address)
    own = set(own_addresses)
    return lambda uid: by_uid.get(uid, set()) | ({uid[5:]} if uid.startswith("pump:") else set()) | own


def _impact_db(path):
    conn = graph_connection(path)
    conn.executescript(IMPACT_SCHEMA)
    return conn


def save_impact_reads(path, reads, *, read_at):
    conn = _impact_db(path)
    try:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO memecoin_wallet_impact_reads (%s, method_version, read_at) VALUES (%s)"
                % (", ".join(READ_COLS), ",".join("?" * (len(READ_COLS) + 2))),
                [tuple(int(r.get(c) or 0) if c == "empty_probes" else r.get(c) for c in READ_COLS)
                 + (IMPACT_METHOD, read_at) for r in reads])
    finally:
        conn.close()


def load_impact_reads(path):
    conn = _impact_db(path)
    try:
        rows = conn.execute("SELECT %s FROM memecoin_wallet_impact_reads WHERE method_version=?"
                            % ", ".join(READ_COLS), (IMPACT_METHOD,)).fetchall()
    finally:
        conn.close()
    return {r[0]: dict(zip(READ_COLS, r)) for r in rows}


def pending_impacts(units, done):
    """Units with no final read (an unread one is retried with its empty-probe count), detection-fed
    first, then newest t0 first."""
    out = []
    for u in units:
        prior = done.get(u["signature"])
        if prior is not None and prior["status"] != "unread":
            continue
        out.append(dict(u, empty_probes=(prior or {}).get("empty_probes") or 0))
    return sorted(out, key=lambda u: (u["origin"] != "detection", -u["t0"]))


IMPACT_ROW_COLS = ("source_user_id", "network_id", "n", "median_delta_sol", "p25_delta_sol", "p75_delta_sol",
                   "share_positive", "median_delta_buyers", "median_undecoded_share", "censored", "censored_share",
                   "n_fill", "median_delta_sol_fill", "k", "k_needed", "p_value", "q_value", "verdict",
                   "verdict_reason", "window_from", "computed_at")


def _bh(pvalues):
    """Benjamini-Hochberg adjusted q per position (step-up), capped at 1."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    q, running = [None] * m, 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        q[i] = running
    return q


def _k_needed(n, m):
    """The smallest k whose one-sided binomial tail passes a rank-1 BH bar (BH_Q / m)."""
    return next((k for k in range(n + 1) if binomial_tail(k, n, 0.5) <= BH_Q / m), None)


def wallet_impacts(reads, *, window_from, computed_at):
    """Per-wallet rows from the current units' reads (spec 'Per wallet' and 'Mover test'). n, the stats,
    the roster median and the test use detection-fed ok reads only; fill-fed reads are reported apart."""
    by = collections.defaultdict(lambda: {"det": [], "fill": [], "buyers": [], "undec": [],
                                          "counts": collections.Counter()})
    for r in reads:
        w = by[r["source_user_id"]]
        if r["origin"] == "fill":
            if r["status"] == "ok":
                w["fill"].append(r["delta_sol"])
            continue
        if r["status"] == "ok":
            w["det"].append(r["delta_sol"])
            w["buyers"].append(r["buyers_after"] - r["buyers_before"])
            n_tx = (r["tx_before"] or 0) + (r["tx_after"] or 0)
            if n_tx:
                w["undec"].append(((r["undecoded_before"] or 0) + (r["undecoded_after"] or 0)) / n_tx)
        elif r["status"] != "unread":
            w["counts"][r["status"]] += 1
    rows = []
    for uid, w in sorted(by.items()):
        n, d = len(w["det"]), w["det"]
        cens = sum(w["counts"][s] for s in CENSORED)
        share = cens / (cens + n) if cens + n else None
        row = dict.fromkeys(IMPACT_ROW_COLS)
        row.update(source_user_id=uid, network_id="solana", n=n, censored=dict(w["counts"]),
                   censored_share=None if share is None else round(share, 4), n_fill=len(w["fill"]),
                   median_delta_sol_fill=(round(statistics.median(w["fill"]), 6) if len(w["fill"]) >= SHOW_N
                                          else None),
                   verdict="unknown", window_from=window_from, computed_at=computed_at)
        if n >= SHOW_N:
            row.update(median_delta_sol=round(statistics.median(d), 6), p25_delta_sol=_pct(d, 0.25),
                       p75_delta_sol=_pct(d, 0.75), share_positive=round(sum(x > 0 for x in d) / n, 4),
                       median_delta_buyers=statistics.median(w["buyers"]),
                       median_undecoded_share=round(statistics.median(w["undec"]), 4) if w["undec"] else None)
        if n < SHOW_N:
            row["verdict_reason"] = "n"
        elif share is not None and share > CENSORED_MAX:
            row["verdict_reason"] = "censored"
        else:
            row["verdict"] = "untested"
        rows.append(row)
    tested = []
    for row in rows:
        others = [x for uid, w in by.items() if uid != row["source_user_id"] for x in w["det"]]
        if row["verdict"] == "untested" and row["n"] >= TEST_N and others:
            mine, cut = by[row["source_user_id"]]["det"], statistics.median(others)
            row["k"] = sum(x > cut for x in mine)
            row["p_value"] = binomial_tail(row["k"], len(mine), 0.5)
            tested.append(row)
    for row, q in zip(tested, _bh([r["p_value"] for r in tested])):
        row["k_needed"] = _k_needed(row["n"], len(tested))
        row["q_value"] = round(q, 6)
        row["verdict"] = "mover" if q <= BH_Q else "not_distinguished"
    return rows


def write_impacts(path, rows):
    """Replace this method version's per-wallet rows in one transaction; returns the row count."""
    conn = _impact_db(path)
    try:
        with conn:
            conn.execute("DELETE FROM memecoin_wallet_impact WHERE method_version=?", (IMPACT_METHOD,))
            conn.executemany(
                "INSERT INTO memecoin_wallet_impact (%s, method_version) VALUES (%s)"
                % (", ".join(IMPACT_ROW_COLS), ",".join("?" * (len(IMPACT_ROW_COLS) + 1))),
                [tuple(json.dumps(r[c]) if c == "censored" else r[c] for c in IMPACT_ROW_COLS) + (IMPACT_METHOD,)
                 for r in rows])
        return len(rows)
    finally:
        conn.close()


def impact_pass(ledger, graph_path, since_iso, *, budget, flat_fn, own_addresses, fetch_age=None,
                fetch_window=None, now_iso=None, trade_fn=None, sig_fn=None):
    """Slice 3, once per run: read pending source buys (detection-fed first, newest first) within
    `budget`, then rewrite the per-wallet rows from every current unit's stored read."""
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    slot_reads = load_reads(graph_path, since_iso)
    units = impact_units(slot_reads, detection_block_times(
        ledger, [r["signature"] for r in slot_reads if r["status"] == "ok"]))
    done = load_impact_reads(graph_path)
    new, calls, why = [], 0, "skipped (budget 0)"
    if budget > 0:
        new, calls, why = read_impacts(
            pending_impacts(units, done), budget=budget, flat_fn=flat_fn,
            excluded_for=excluded_traders(solana_owners(ledger), own_addresses), ours=our_fill_sigs(ledger),
            fetch_age=fetch_age or (lambda u: helius_first_tx_time(u["mint"], flat_fn=flat_fn)),
            fetch_window=fetch_window or (lambda u: helius_impact_window(u["mint"], u["t0"], flat_fn=flat_fn)),
            trade_fn=trade_fn, sig_fn=sig_fn)
        save_impact_reads(graph_path, new, read_at=now_iso)
        done = load_impact_reads(graph_path)
    rows = wallet_impacts([done[u["signature"]] for u in units if u["signature"] in done],
                          window_from=since_iso, computed_at=now_iso)
    write_impacts(graph_path, rows)
    return {"reads_new": len(new), "calls": calls, "stop": why, "units": len(units), "wallets": len(rows)}


# ---------------------------------------------------------------- upstream discovery (slice 3b)
# Pre-registration docs/superpowers/specs/2026-09-30-memecoin-wallet-map-slice3b-upstream-design.md
# (Gamma CLEAN a6ccd12dd). The rule, the null and the storage are in wallet_upstream.py (pure); the
# reads and the pass are here. Research key only, and only while the Solana book is flat.
def helius_upstream_window(mint, start, t0, *, rpc=None, flat_fn=lambda: True):
    """The mint's succeeded transactions with blockTime in [start, t0 + 60], ascending, at most 10
    pages; (txs, calls, truncated). `start` is t0 - 60, or the mint's first transaction on a launch."""
    from scripts.research import wallet_upstream as up
    return helius_slot_fetch({"mint": mint}, rpc=rpc, flat_fn=flat_fn, pages=up.UP_PAGES,
                             window={"blockTime": {"gte": int(start), "lte": int(t0) + up.LABEL_S}})


def helius_end_page(mint, t0, *, rpc=None, flat_fn=lambda: True):
    """The LAST 100 succeeded transactions of the mint with blockTime in [t0, t0 + 60] (one page,
    descending): the end of a minute whose ascending window was cut; (txs, 1)."""
    from scripts.research import wallet_upstream as up
    stop = _NoStop
    if rpc is None:
        sys.path.insert(0, str(REPO / "scripts" / "research"))
        import funded_exit_truth as fet         # the research key; refuses a funded key (pick_research_url)
        rpc, stop = fet.rpc, fet.Stop
    if not flat_fn():
        raise ReadStop("solana book open mid-read")
    opts = {"transactionDetails": "full", "sortOrder": "desc", "limit": 100, "encoding": "json",
            "maxSupportedTransactionVersion": 1,
            "filters": {"status": "succeeded", "blockTime": {"gte": int(t0), "lte": int(t0) + up.LABEL_S}}}
    try:
        res = rpc("getTransactionsForAddress", [mint, opts]) or {}
    except stop as exc:
        raise ReadStop(str(exc)[:160])
    except (TimeoutError, OSError) as exc:
        raise ReadStop("%s: %s" % (type(exc).__name__, str(exc)[:120]))
    return res.get("data") or [], 1


def our_fill_times(ledger):
    """{mint: [epoch]} of our funded Solana fills, entries and exits, by ledger time."""
    out = collections.defaultdict(list)
    for mint, at in ledger.execute(
            "SELECT p.mint, f.created_at FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
            "WHERE p.network_id='solana' AND f.tx_sig IS NOT NULL AND f.created_at IS NOT NULL"):
        out[mint].append(_ts(at))
    return out


def _curve_creator(tx, mint):
    """The creator a pump curve trade event names, or None (a pool trade names none)."""
    sys.path.insert(0, str(REPO / "scripts" / "research"))
    import dump_forensics as dfx
    return (dfx.trade_of(tx, mint, 1.0) or {}).get("creator")


def copy_bot_addresses(graph_path):
    """Wallets that are a slot_follower companion of any source in the current graph."""
    conn = graph_connection(graph_path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT dst FROM memecoin_wallet_edges WHERE edge_type='slot_follower' AND network_id='solana'")}
    finally:
        conn.close()


def funder_state(graph_path):
    """(wallets whose funder read is final, wallets funded by a hub). A hub funds more than HUB_FUNDED
    resolved wallets, the same bound as the shared_funder edges."""
    conn = _funder_db(graph_path)
    try:
        rows = conn.execute("SELECT wallet, status, funder FROM memecoin_wallet_funders "
                            "WHERE status != 'unread'").fetchall()
    finally:
        conn.close()
    degree = collections.Counter(f for _w, status, f in rows if status == "resolved" and f)
    return {w for w, _s, _f in rows}, {w for w, status, f in rows if status == "resolved" and degree[f] > HUB_FUNDED}


def upstream_pass(ledger, graph_path, since_iso, *, budget, flat_fn, own_addresses, verdicts, now_iso=None,
                  fetch_age=None, fetch_window=None, fetch_end=None, creator_fn=_curve_creator):
    """Slice 3b, once per run: read pending coins (detection-fed first, newest first) within `budget`,
    drop the units that left the window, report the control, and ONLY with `verdicts` rewrite the
    per-wallet rows. The control is reported by every run, before any verdict exists (Gamma R2)."""
    from scripts.research import wallet_upstream as up
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    slot_reads = load_reads(graph_path, since_iso)
    units = up.upstream_units(impact_units(slot_reads, detection_block_times(
        ledger, [r["signature"] for r in slot_reads if r["status"] == "ok"])))
    owners = solana_owners(ledger)
    by_uid = collections.defaultdict(set)
    for address, uid in owners.items():
        by_uid[uid].add(address)
    for uid in {u["source_user_id"] for u in units} | set(owners.values()):
        if uid.startswith("pump:"):
            by_uid[uid].add(uid[5:])
    roster = set().union(*by_uid.values()) if by_uid else set()
    conn = graph_connection(graph_path)
    try:
        relabel = up.relabel_a1b(conn)            # amendment A1b, once; its re-reads go through the budget
        done = up.load_reads(conn)
        new, calls, why = [], 0, "skipped (budget 0)"
        if budget > 0:
            fills = our_fill_times(ledger)
            ages = {sig: r["age_s"] for sig, r in load_impact_reads(graph_path).items() if r.get("age_s") is not None}
            new, buys, calls, why = up.read_upstream(
                up.pending(units, done, ages, carry=up.reread_carry(conn)), stored_windows=up.stored_windows(conn),
                budget=budget, flat_fn=flat_fn, stop=ReadStop,
                excluded=roster | set(own_addresses), addresses_for=lambda uid: by_uid.get(uid, set()),
                ours=our_fill_sigs(ledger), creator_fn=creator_fn,
                our_fill_near=lambda u: any(
                    u["t0"] - up.OUR_FILL_SLACK_S <= t <= u["t0"] + up.LABEL_S + up.OUR_FILL_SLACK_S
                    for t in fills.get(u["mint"], ())),
                fetch_age=fetch_age or (lambda u: helius_first_tx_time(u["mint"], flat_fn=flat_fn)),
                fetch_window=fetch_window or (lambda u, start: helius_upstream_window(
                    u["mint"], start, u["t0"], flat_fn=flat_fn)),
                fetch_end=fetch_end or (lambda u: helius_end_page(u["mint"], u["t0"], flat_fn=flat_fn)))
            up.save_reads(conn, new, buys, read_at=now_iso)
        # An empty unit list is a slot-read table that answered nothing for the window, not a window
        # with no coins: nothing is dropped then (Gamma LOW). The stored reads cost research calls and
        # a coin that left the index cannot be read again.
        pruned = up.prune(conn, [u["signature"] for u in units]) if units else "skipped_no_units"
        done = up.load_reads(conn)
        current = [done[u["signature"]] for u in units if u["signature"] in done]
        named, total, fails = up.control(current)
        out = {"reads_new": len(new), "calls": calls, "stop": why, "units": len(units), "pruned": pruned,
               "status": dict(collections.Counter(r["status"] for r in current)),
               "control": "%d/%d" % (named, total), "control_fails": fails,
               # ok units that are not winners only because the minute held under 3 priced trades (A2),
               # and units whose window rate fell outside the run's band (A1)
               "thin_losers": sum(1 for r in current if r["status"] == "ok" and (r["n_end"] or 0) < 3),
               "fx_rejected": sum(1 for r in current if r["fx_rejected"] is not None),
               "fx_a1_rejected": sum(1 for r in current if r.get("fx_a1_rejected") is not None),
               "a1b_reread_left": len(relabel["reread"]),
               "wallets": None, "funder_wanted": []}
        if verdicts:
            final, hubs = funder_state(graph_path)
            rows, nulls = up.wallet_rows(
                current, up.load_buys(conn, [r["signature"] for r in current]),
                copy_bots=copy_bot_addresses(graph_path), hub_funded=hubs, funder_read=final,
                prior=up.load_rows(conn), window_from=since_iso, computed_at=now_iso)
            out["wallets"] = up.write_rows(conn, rows, nulls)
            out["funder_wanted"] = sorted(r["trader"] for r in rows if r["verdict_reason"] == "funder_unread")
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------- brain projection (Alpha, 2026-09-29)
# Each projected edge is ONE measurement fact on its two wallet entities, with the method version and
# the coverage caveats in the statement. Wallets are named by J7 handle, the name existing brain
# wallet entities use, with the source_user_id and proven addresses as aliases. The graph DB keeps
# edge_key -> fact_id, so a recompute rewrites only a changed edge, and an edge that no longer passes
# is retracted. A stale or empty graph run projects nothing: a J7 outage must not retract the graph.
BRAIN_SECTION = "memecoin"
BRAIN_AUTHOR = "cron"
BRAIN_RETRACT_ROLE = "lambda"                  # retract needs a crew role; the projector is Lambda's
RUN_MAX_AGE_S = 2 * 3600
WINDOW_DAYS = 14                               # the method window; a shorter run is not projected
TOP_PER_TYPE = 10
SPEC_DOC = "docs/superpowers/specs/2026-09-29-memecoin-wallet-link-graph-design.md"
SCRIPT_PATH = "scripts/research/wallet_link_graph.py"


def _key(e):
    return "|".join((e["network_id"], e["edge_type"], e["src"], e["dst"]))


def select_edges(edges, top=TOP_PER_TYPE):
    """At most `top` edges of a type per wallet, best first (lowest p, then most coins). Greedy: an
    edge is kept while both its wallets are under the cap."""
    ranked = sorted(edges, key=lambda e: (1.0 if e.get("null_p") is None else e["null_p"],
                                          -e["n_coins"], _key(e)))
    used, kept = collections.Counter(), []
    for e in ranked:
        a, b = (e["edge_type"], e["network_id"], e["src"]), (e["edge_type"], e["network_id"], e["dst"])
        if used[a] < top and used[b] < top:
            used[a] += 1
            used[b] += 1
            kept.append(e)
    return kept


def _name(uid, names):
    return names.get(uid) or uid[:8]


def statement_for(e, names):
    a, b, net = _name(e["src"], names), _name(e["dst"], names), e["network_id"]
    null = (e.get("evidence") or {}).get("null") or {}
    gaps = (e.get("coverage") or {}).get("j7_empty_days") or []
    tail = "J7 roster only (J7 clock), %s..%s%s, %s; not proof of coordination." % (
        e["first_seen"][:10], e["last_seen"][:10], ", J7 empty days " + ",".join(gaps) if gaps else "",
        e.get("method_version") or METHOD_VERSION)
    if e["edge_type"] == "shared_funder":
        ev = e.get("evidence") or {}
        return ("%s and %s on %s share the same first funder %s (it funds %d of the %d wallets we resolved; may be "
                "an exchange or a service): the account that sent each wallet's first SOL. %s; not proof of "
                "common ownership." % (a, b, net, ev.get("funder"), ev.get("funder_degree", 0),
                                       ev.get("resolved_wallets", 0), ev.get("method")))
    if e["edge_type"] in ("co_buy_slot", "slot_follower"):
        reads = (e.get("coverage") or {}).get("slot_reads") or {}
        sample = ("our fills, watcher detections and relay-reported buys"
                  if "j7" in ((e.get("coverage") or {}).get("slot_intakes") or {}) else "armed-wallet buys only")
        tail = ("Read around %s's buys only: %s (slot order; decoded buyers only, a lower "
                "bound; Helius reads ok %d, unread %d, not_found %d, truncated %d), %s..%s, %s; not proof of "
                "coordination." % (a, sample, reads.get("ok", 0), reads.get("unread", 0), reads.get("not_found", 0),
                                   reads.get("truncated", 0), e["first_seen"][:10], e["last_seen"][:10],
                                   e.get("method_version") or METHOD_VERSION))
        what = ("bought inside %s's own buy slot" % a if e["edge_type"] == "co_buy_slot"
                else "landed after %s inside %s's buy slot" % (a, a))
        return "%s %s on %s on %d of %d of %s's read buys (base rate %g, binomial p %g). %s" % (
            b, what, net, e["n_coins"], null.get("n", 0), a, null.get("q", 0), e["null_p"], tail)
    if e["edge_type"] == "follows":
        flag = " (BOUNDARY)" if e.get("flag") == "boundary" else ""
        return ("%s follows %s on %s: %s bought first on %d of %d shared coins (first buys within %d s), "
                "lag p50 %.0f s, p90 %.0f s; within-day shuffle null: worst-seed p %g, p <= %g on %d/%d "
                "seeds%s. %s" % (b, a, net, a, e["n_consistent"], e["n_coins"], WINDOW_S, e["lag_p50_s"],
                                 e["lag_p90_s"], e["null_p"], ALPHA_P, null.get("seeds_passed", 0), len(SEEDS),
                                 flag, tail))
    bins = null.get("bins_120s") or [0] * 10
    caveat = ("; nearly as many in the next 120 s, so 'soon after', not a 120 s dump"
              if bins[6] * 2 >= bins[5] else "")
    return ("%s sells soon after %s buys on %s: on %d of %d coins %s held and sold within +-%d s of %s's "
            "first buy, its first sell came 0-%d s after (binomial q %g, p %g); first sells 0-120 s: %d, "
            "120-240 s: %d%s. %s" % (a, b, net, e["n_coins"], null.get("near", 0), a, WINDOW_S, b,
                                    DUMP_WINDOW_S, DUMP_Q, e["null_p"], bins[5], bins[6], caveat, tail))


def detail_for(e, computed_at):
    ev = e.get("evidence") or {}
    d = {"edge_key": _key(e), "computed_at": computed_at, "method_version": e.get("method_version") or METHOD_VERSION,
         "null": dict(ev.get("null") or {}), "coins": [c["mint"] for c in ev.get("coins") or []][:5],
         "coverage": e.get("coverage")}
    d.update({k: e.get(k) for k in ("network_id", "edge_type", "src", "dst", "n_coins", "n_consistent",
                                    "lag_p50_s", "lag_p90_s", "null_p", "flag", "first_seen", "last_seen")})
    text = json.dumps(d, separators=(",", ":"))
    if len(text) > DETAIL_MAX:
        d.pop("coins")
        d["null"].pop("per_seed", None)
        text = json.dumps(d, separators=(",", ":"))
    return _fit_detail(d, text)


DETAIL_MAX = 4096                       # the brain write gate's detail bound


def _fit_detail(d, text):
    """Cap the largest field, then the next, until `text` fits DETAIL_MAX, each with an explicit
    "...(truncated, N chars)" marker (N = its full JSON length). The 6 shared_funder edges carry a
    hub_funders map in `coverage` that alone exceeds the bound, so the gate refused them every hour
    (Wallet Map D1, 2026-10-07)."""
    capped = set()
    while len(text) > DETAIL_MAX:
        left = [k for k in d if k not in capped]
        if not left:
            break                               # every field is a marker: the gate decides
        field = max(left, key=lambda k: len(json.dumps(d[k], separators=(",", ":"))))
        raw = json.dumps(d[field], separators=(",", ":"))
        marker = "\u2026(truncated, %d chars)" % len(raw)
        budget = DETAIL_MAX - (len(text) - len(raw))
        keep = max(0, budget - len(json.dumps(marker)))
        while keep and len(json.dumps(raw[:keep] + marker)) > budget:
            keep -= len(json.dumps(raw[:keep] + marker)) - budget
        d[field] = raw[:max(0, keep)] + marker
        capped.add(field)
        text = json.dumps(d, separators=(",", ":"))
    return text


def read_edges(conn):
    cols = ("network_id", "edge_type", "src", "dst", "n_coins", "n_consistent", "lag_p50_s", "lag_p90_s",
            "null_p99", "null_p", "flag", "first_seen", "last_seen", "coverage", "evidence", "method_version")
    rows = conn.execute("SELECT %s FROM memecoin_wallet_edges WHERE method_version IN (?,?)" % ", ".join(cols),
                        (METHOD_VERSION, SLOT_COVERAGE_METHOD)).fetchall()
    out = []
    for r in rows:
        e = dict(zip(cols, r))
        e["coverage"], e["evidence"] = json.loads(e["coverage"]), json.loads(e["evidence"])
        out.append(e)
    return out


def _run_refusal(run, now_iso):
    """Why the last graph run (finished_at, note, window_from) must not be projected, or None."""
    if run is None or not run[0]:
        return "no finished graph run for %s" % METHOD_VERSION
    age = _ts(now_iso) - _ts(run[0])
    if age > RUN_MAX_AGE_S:
        return "the last graph run finished %.1f h ago" % (age / 3600)
    span = _ts(run[0]) - _ts(run[2]) if run[2] else 0
    if span < WINDOW_DAYS * 86400 - 60:        # Gamma LOW 1: a manual --days 1 would retract the rest
        return "the last graph run's window is %.1f d, shorter than the %d d method window" % (
            span / 86400, WINDOW_DAYS)
    m = re.search(r"events=(\d+)", run[1] or "")
    if not m or int(m.group(1)) == 0:
        return "the last graph run read no J7 events (feed gap): nothing is retracted"
    return None


def _graph_verdict(store, fact_id, key, ours):
    """'supersedes' for a graph fact of the same edge (the map may be lost: Gamma LOW), 'unrelated'
    for any other graph fact, None for a fact the crew wrote."""
    fact = store.get("brain_facts", fact_id)
    if not fact or not (fact_id in ours or str(fact.get("author_session") or "").startswith("wallet-graph:")):
        return None
    try:
        same = json.loads(fact.get("detail") or "{}").get("edge_key") == key
    except (TypeError, ValueError):
        same = False
    return "supersedes" if same else "unrelated"


def _live_fact(store, fact_id):
    from bot.brain_graph import READ_STATUSES
    fact = store.get("brain_facts", fact_id)
    return bool(fact) and fact["status"] in READ_STATUSES and fact.get("valid_to") is None


def _entity(uid, names, aliases):
    return {"type": "wallet", "name": names.get(uid) or uid, "section": BRAIN_SECTION,
            "aliases": [uid] + [a for a in aliases.get(uid, []) if a != uid]}


def project(store, graph_path, *, now_iso, names, aliases, repo, secret_values):
    """Project the current edges into the brain. Returns counts; `refused` says why nothing ran."""
    from bot.brain_graph import api
    out = {"refused": None, "written": 0, "unchanged": 0, "retracted": 0, "errors": []}
    conn = graph_connection(graph_path)
    try:
        run = conn.execute("SELECT finished_at, note, window_from FROM memecoin_wallet_graph_runs "
                           "WHERE method_version=? ORDER BY id DESC LIMIT 1", (METHOD_VERSION,)).fetchone()
        out["refused"] = _run_refusal(run, now_iso)
        if out["refused"]:
            return out
        projected = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT edge_key, fact_id, statement FROM memecoin_wallet_graph_brain")}
        ours = {fact_id for fact_id, _statement in projected.values()}
        passing = read_edges(conn)
        # One fact per slot pair (Alpha, 2026-09-29: 38 of 40 co_buy_slot pairs were also
        # slot_follower pairs). slot_follower is the stronger statement; co_buy_slot is projected
        # only where no follower edge exists.
        followers = {(e["network_id"], e["src"], e["dst"]) for e in passing if e["edge_type"] == "slot_follower"}
        dedup = {_key(e) for e in passing if e["edge_type"] == "co_buy_slot"
                 and (e["network_id"], e["src"], e["dst"]) in followers}
        passing = [e for e in passing if _key(e) not in dedup]
        selected = select_edges(passing, top=TOP_PER_TYPE)
        capped = {_key(e) for e in passing} - {_key(e) for e in selected}
        keep = set()
        for e in selected:
            key, statement = _key(e), statement_for(e, names)
            keep.add(key)
            old = projected.get(key)
            old_live = bool(old) and _live_fact(store, old[0])
            if old_live and old[1] == statement:
                out["unchanged"] += 1
                continue
            # An edge is a relationship between two wallets, not a performance claim: the research
            # publication gate (c46c0aaae) refuses an unscoped memecoin measurement, which left the
            # projector writing nothing from 2026-10-04.
            draft = {"kind": "measurement", "statement": statement, "detail": detail_for(e, run[0]),
                     "author": BRAIN_AUTHOR, "author_session": "wallet-graph:" + METHOD_VERSION,
                     "section": BRAIN_SECTION, "claim_scope": "relationship",
                     "entities": [_entity(e["src"], names, aliases), _entity(e["dst"], names, aliases)],
                     "evidence": [{"type": "query", "ref": (
                         "data/memecoin_wallet_graph.db: SELECT * FROM memecoin_wallet_edges WHERE "
                         "network_id='%s' AND edge_type='%s' AND src='%s' AND dst='%s' AND method_version='%s'"
                         % (e["network_id"], e["edge_type"], e["src"], e["dst"],
                            e.get("method_version") or METHOD_VERSION))},
                                  {"type": "doc", "ref": SPEC_DOC}, {"type": "path", "ref": SCRIPT_PATH}]}
            wid = str(uuid.uuid5(uuid.NAMESPACE_URL, "wallet-graph|%s|%s|%s" % (key, statement, run[0])))
            # Gamma MED: judge our own candidates, or every sibling edge on a shared wallet (Jaccard
            # >= 0.3 on the template) leaves the fact judge_pending. The same edge supersedes; any
            # other graph fact is unrelated; a human-written candidate is left to the crew.
            judgements = {old[0]: "supersedes"} if old_live else {}
            r = api.write_fact(store, draft, repo=repo, judgements=judgements, interactive=True,
                               secret_values=secret_values, fetched=False, write_id=wid)
            if r["status"] == "needs_judgement":
                for c in r["candidates"]:
                    verdict = None if c["id"] in judgements else _graph_verdict(store, c["id"], key, ours)
                    if verdict:
                        judgements[c["id"]] = verdict
                r = api.write_fact(store, draft, repo=repo, judgements=judgements, interactive=False,
                                   secret_values=secret_values, fetched=False, write_id=wid)
            if r["status"] != "written":
                out["errors"].append({"edge_key": key, "status": r["status"], "errors": r.get("errors")})
                continue
            new_id = r["fact"]["id"]
            if old_live and old[0] != new_id and _live_fact(store, old[0]):
                api.retract_fact(store, old[0], role=BRAIN_RETRACT_ROLE, secret_values=secret_values,
                                 reason="wallet-graph projector (cron): replaced by %s" % new_id)
            with conn:
                conn.execute("INSERT OR REPLACE INTO memecoin_wallet_graph_brain VALUES (?,?,?,?)",
                             (key, new_id, statement, now_iso))
            out["written"] += 1
        for key, (fact_id, _statement) in projected.items():
            if key in keep:
                continue
            if _live_fact(store, fact_id):
                why = ("deduplicated: the slot_follower fact for this pair covers it" if key in dedup
                       else "the edge still passes but was dropped by the top-%d cap per wallet per type" % TOP_PER_TYPE
                       if key in capped else "the edge no longer passes %s" % METHOD_VERSION)
                api.retract_fact(store, fact_id, role=BRAIN_RETRACT_ROLE, secret_values=secret_values,
                                 reason="wallet-graph projector (cron): %s at %s" % (why, run[0]))
                out["retracted"] += 1
            with conn:
                conn.execute("DELETE FROM memecoin_wallet_graph_brain WHERE edge_key=?", (key,))
        return out
    finally:
        conn.close()


def run_project(args):
    """CLI: read handles and proven addresses from the ledger (read-only), then project."""
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    state = state_root()
    load_env_files(str(state))                               # .env and .fomo.env, as brain_cli
    from bot.brain_graph.gate import secret_values_from_env, secret_values_from_mapping
    from bot.brain_graph.store import BrainUnavailable, RestStore
    from sniper.services.axiom_address_verify import watcher_address_owners
    ledger = ledger_connection(Path(args.ledger or state / "sniper_tracker.db"))
    try:
        names = {k: v for k, v in wallet_handles(ledger).items() if v}
        aliases = collections.defaultdict(list)
        pairs = [(str(a), str(u)) for (_n, a), (u, _h) in sorted(watcher_address_owners(ledger).items())]
        pairs += sorted(solana_owners(ledger).items())
        for address, uid in pairs:
            if address not in aliases[uid]:
                aliases[uid].append(address)
    finally:
        ledger.close()
    secrets = (secret_values_from_env(state / ".env") | secret_values_from_env(state / ".fomo.env")
               | secret_values_from_mapping(os.environ))
    try:
        out = project(RestStore(), Path(args.graph or state / "data" / "memecoin_wallet_graph.db"),
                      now_iso=datetime.now(timezone.utc).isoformat(), names=names, aliases=dict(aliases),
                      repo=REPO, secret_values=secrets)
    except BrainUnavailable as exc:
        print(json.dumps({"brain_unavailable": str(exc)[:200]}))
        return 4
    print(json.dumps(out))
    return 2 if out["errors"] else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=("run", "project"))
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--helius-budget", type=int, default=SLOT_BUDGET,
                    help="Helius calls for slot reads this run (capped at %d; 0 skips)" % SLOT_BUDGET_MAX)
    ap.add_argument("--fill-read-budget", type=int, default=None,
                    help="Helius calls for funded-fill source reads, taken FIRST from --helius-budget "
                         "(default: all of it; 0 skips)")
    ap.add_argument("--impact-budget", type=int, default=IMPACT_BUDGET,
                    help="Helius calls for slice-3 flow-change reads this run (after the slot reads; 0 skips)")
    ap.add_argument("--upstream-budget", type=int, default=UPSTREAM_BUDGET,
                    help="Helius calls for slice-3b upstream reads this run (after the impact reads; 0 skips)")
    ap.add_argument("--upstream-verdicts", action="store_true",
                    help="slice 3b: also compute the per-wallet verdicts. Off until the control has been "
                         "read from the run notes (the control is reported before any verdict)")
    ap.add_argument("--coverage", action="store_true",
                    help="overhaul spec section 0: also read every relay-reported signed Solana buy's slot "
                         "(one per wallet and coin), after slices 3 and 3b, from the slot budget's remainder. "
                         "Off until Gamma's pre-registration")
    ap.add_argument("--trail", default=None, metavar="FROZEN_JSON",
                    help="pre-registration C and D: read the frozen probes ({frozen_at, herders, probes}) once "
                         "per UTC day, after coverage, at most %d calls per run. Off without Gamma's frozen list"
                         % TRAIL_BUDGET)
    ap.add_argument("--axiom", default=None, metavar="SEED_JSON",
                    help="pre-registration F: the one-off Axiom kol-list read (seed from axiom_kol_seed.py), after the "
                         "trail, at most %d calls per run. Off until Gamma says go" % AXIOM_BUDGET)
    ap.add_argument("--funder-budget", type=int, default=FUNDER_BUDGET,
                    help="Helius calls for first-funder reads this run (after the slot reads; 0 skips)")
    ap.add_argument("--ledger")
    ap.add_argument("--graph")
    args = ap.parse_args()
    return run_project(args) if args.mode == "project" else run(args)


if __name__ == "__main__":
    sys.exit(main())
