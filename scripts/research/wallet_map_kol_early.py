#!/usr/bin/env python3
"""Wallet Map: Axiom smart-money wallets that bought BEFORE our sources. Descriptive, 0 calls, read-only.

Overhaul spec docs/superpowers/specs/2026-09-30-memecoin-wallet-map-overhaul-design.md §2 and §6 (Alpha
GO 2026-10-01). Joins what we already hold:
  * the Axiom smart-money directory (`axiom_trader_directory`, Solana addresses) and, for each address,
    the 7-day lists it was on and its rank there (the `smart-money-traders` REST payloads of the
    2026-09-16 capture in data/axiom_raw.db);
  * slice 3b's early buys (`memecoin_wallet_early_buys`, signer-decoded by wallet_upstream, so the
    trade_of v1 defect does not touch them) on reads with status 'ok' only, slice 3b's own
    denominator: one read = one coin, at the first buy by one of our Solana sources;
  * our roster's proven Solana addresses (`memecoin_address_registry`, `memecoin_wallet_addresses`).

NOTHING IS TESTED. The report shows counts, leads and which source each wallet preceded; it shows no
outcome of the coins (win, return), so nothing here selects on a result before Gamma registers a
rule. The baseline row is every early trader in the same reads, so "k coins" has a denominator.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE = Path(os.environ.get("SNIPER_STATE_ROOT") or REPO)


def ro(path):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def axiom_lists(raw_path):
    """{address: {list: rank_7d}} from the deduplicated smart-money-traders payloads."""
    out = collections.defaultdict(dict)
    if not Path(raw_path).exists():
        return out
    c = ro(raw_path)
    for (payload,) in c.execute(
            "SELECT payload FROM axiom_raw_messages INDEXED BY idx_axiom_raw_sha"
            " WHERE source LIKE '%/smart-money-traders' AND body_sha IS NOT NULL"):
        try:
            p = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(p, dict) or p.get("chain") != "sol" or p.get("window") != "7d":
            continue
        for row in p.get("rows") or []:
            addr = (((row.get("wallets") or {}).get("solana") or {}).get("address"))
            if addr:
                rank = ((row.get("windows") or {}).get("7d") or {}).get("rank")
                out[addr][p.get("source") or "?"] = rank
    return out


def roster_addresses(ledger):
    out = {}
    for table, cols in (("memecoin_address_registry", "address, source_user_id"),
                        ("memecoin_wallet_addresses", "address, source_user_id")):
        try:
            for r in ledger.execute("SELECT %s FROM %s WHERE network_id='solana'" % (cols, table)):
                out.setdefault(r["address"], r["source_user_id"])
        except sqlite3.OperationalError:
            continue
    return out


def build(ledger, graph, lists):
    handles = {r["source_user_id"]: r["handle"] for r in ledger.execute(
        "SELECT source_user_id, handle FROM memecoin_wallets")}
    axiom = collections.defaultdict(lambda: {"handles": set(), "followers": None, "pnl_24h": None})
    for r in ledger.execute("SELECT address, x_handle, followers, pnl_24h FROM axiom_trader_directory"
                            " WHERE chain='solana'"):
        a = axiom[r["address"]]
        a["handles"].add(r["x_handle"])
        a["followers"] = a["followers"] if r["followers"] is None else r["followers"]
        a["pnl_24h"] = a["pnl_24h"] if r["pnl_24h"] is None else r["pnl_24h"]
    reads = {r["signature"]: r for r in graph.execute(
        "SELECT signature, mint, source_user_id FROM memecoin_wallet_upstream_reads WHERE status='ok'")}
    early = collections.defaultdict(list)
    for r in graph.execute("SELECT signature, trader, lead_s FROM memecoin_wallet_early_buys"):
        if r["signature"] in reads:
            early[r["trader"]].append((r["signature"], r["lead_s"]))
    roster = roster_addresses(ledger)
    rows = []
    for addr, hits in early.items():
        if addr not in axiom:
            continue
        coins = {reads[s]["mint"] for s, _l in hits}
        sources = collections.Counter(handles.get(reads[s]["source_user_id"], reads[s]["source_user_id"])
                                      for s, _l in hits)
        leads = [l for _s, l in hits if l is not None]
        rows.append({
            "address": addr, "x_handles": sorted(h for h in axiom[addr]["handles"] if h),
            "axiom_7d_lists": lists.get(addr, {}), "followers": axiom[addr]["followers"],
            "on_roster": handles.get(roster[addr]) if addr in roster else None,
            "k_coins": len(coins), "early_rows": len(hits),
            "lead_s_median": statistics.median(leads) if leads else None,
            "lead_s_min": min(leads) if leads else None,
            "sources_preceded": dict(sources.most_common())})
    rows.sort(key=lambda r: (-r["k_coins"], r["lead_s_median"] if r["lead_s_median"] is not None else 1e9))
    k_all = collections.Counter(len({reads[s]["mint"] for s, _l in hits}) for hits in early.values())
    base = {"reads_ok": len(reads), "early_traders": len(early),
            "early_traders_k_ge_2": sum(n for k, n in k_all.items() if k >= 2),
            "early_traders_k_ge_3": sum(n for k, n in k_all.items() if k >= 3),
            "axiom_solana_addresses": len(axiom), "axiom_seen_early": len(rows),
            "axiom_k_ge_2": sum(1 for r in rows if r["k_coins"] >= 2),
            "axiom_k_ge_3": sum(1 for r in rows if r["k_coins"] >= 3),
            "axiom_on_roster": sum(1 for r in rows if r["on_roster"])}
    return base, rows


# --------------------------------------------------------------- the pre-registered group test
# docs/audits/2026-10-01-wallet-map-kol-early-preregistration.md (Gamma 037d1c6d8). ONE test over coins:
# no per-wallet verdict, ever, from this slice.
SEED, N_PERM = 20261001, 10_000
MIN_UNITS, ALPHA, LOO_ALPHA, MIN_WIN_DAYS, CONTROL_MIN = 30, 0.05, 0.10, 3, 0.90


def a1_units(graph):
    """Amendment A1b's sensitivity line: every stored read under its A1 label (wallet_upstream.a1_label),
    with the nulls recomputed on that view (wallet_upstream.unit_nulls). A read whose A1 label is
    unknown is left out."""
    import wallet_upstream as up
    view = []
    for r in graph.execute("SELECT * FROM memecoin_wallet_upstream_reads"):
        r = dict(r)
        label = up.a1_label(r)
        if label is not None:
            view.append(dict(r, **label))
    nulls = up.unit_nulls(view)
    return [dict(r, p_null=nulls[r["signature"]][0]) for r in view if r["signature"] in nulls]


def a1b_pending(graph):
    """Reads still stored under A1, waiting for their one A1b re-read (wallet_upstream.relabel_a1b).
    A group test refuses while any remain: the primary would mix in A1 labels and stale nulls."""
    import wallet_upstream as up
    return graph.execute("SELECT COUNT(*) FROM memecoin_wallet_upstream_reads WHERE method_version=?",
                         (up.UPSTREAM_METHOD_A1,)).fetchone()[0]


def test_units(graph, kol_addresses, *, after=None, fx_band="a1b"):
    """3b's units (ok, detection-fed, win and p_null set), each with its distinct early buyers. A1b is
    the primary (A1b-method rows only); fx_band 'a1' is its labelled sensitivity line (a1_units)."""
    import wallet_upstream as up
    rows = a1_units(graph) if fx_band == "a1" else [dict(r) for r in graph.execute(
        "SELECT signature, source_user_id, t0, launch, win, p_null, read_at FROM memecoin_wallet_upstream_reads"
        " WHERE status='ok' AND origin='detection' AND win IS NOT NULL AND p_null IS NOT NULL"
        " AND method_version=?", (up.UPSTREAM_METHOD,))]
    if after is not None:
        rows = [r for r in rows if (r["read_at"] or "") > after]
    want = {r["signature"] for r in rows}
    early = collections.defaultdict(set)
    for r in graph.execute("SELECT signature, trader FROM memecoin_wallet_early_buys"):
        if r["signature"] in want:
            early[r["signature"]].add(r["trader"])
    for r in rows:
        r["n_early"] = len(early[r["signature"]])
        r["kols"] = early[r["signature"]] & kol_addresses
    return rows


def control_share(graph):
    """3b's control at this read: named / total over ok reads (it must stand at >= 90%)."""
    named, total = graph.execute(
        "SELECT COALESCE(SUM(control_named), 0), COUNT(*) FROM memecoin_wallet_upstream_reads"
        " WHERE status='ok' AND control_named IS NOT NULL").fetchone()
    return (named / total) if total else None


def strata(units):
    """launch x tercile of n_early, terciles cut once on these units before any shuffle."""
    v = sorted(u["n_early"] for u in units)
    t1, t2 = (v[len(v) // 3], v[2 * len(v) // 3]) if v else (0, 0)
    return [(int(bool(u["launch"])), 0 if u["n_early"] <= t1 else 1 if u["n_early"] <= t2 else 2) for u in units]


def statistic(units, labels):
    return sum(u["win"] - u["p_null"] for u, lab in zip(units, labels) if lab)


def perm_p(units, labels, groups, *, seed=SEED, n_perm=N_PERM):
    """One-sided: the share of within-stratum shuffles of the label whose S >= the observed S."""
    import random
    rng = random.Random(seed)
    by = collections.defaultdict(list)
    for i, g in enumerate(groups):
        by[g].append(i)
    observed = statistic(units, labels)
    hits = 0
    for _ in range(n_perm):
        perm = list(labels)
        for idx in by.values():
            vals = [labels[i] for i in idx]
            rng.shuffle(vals)
            for i, val in zip(idx, vals):
                perm[i] = val
        hits += statistic(units, perm) >= observed - 1e-12
    return hits / n_perm


def _day(t0):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(t0), timezone.utc).date().isoformat() if t0 else None


def group_test(units, *, seed=SEED, n_perm=N_PERM, notdecu_sources=()):
    """The pre-registered read: S, p, leave-the-top-wallet-out, win days, strata, verdict."""
    groups = strata(units)
    labels = [bool(u["kols"]) for u in units]
    n_kol = sum(labels)
    out = {"units": len(units), "kol_units": n_kol}
    per_wallet = collections.Counter(w for u in units for w in u["kols"])
    out["wallets"] = dict(per_wallet.most_common())
    table = collections.defaultdict(lambda: {"units": 0, "kol_units": 0, "sum_win_minus_p": 0.0})
    for u, g, lab in zip(units, groups, labels):
        cell = table["launch=%d n_early_tercile=%d" % g]
        cell["units"] += 1
        cell["kol_units"] += lab
        cell["sum_win_minus_p"] += u["win"] - u["p_null"]
    out["strata"] = {k: dict(v, mean_win_minus_p=round(v["sum_win_minus_p"] / v["units"], 4))
                     for k, v in sorted(table.items())}
    if n_kol < MIN_UNITS:
        out["verdict"] = "not shown: fewer than %d KOL-preceded units" % MIN_UNITS
        return out
    out["S"] = statistic(units, labels)
    out["p"] = perm_p(units, labels, groups, seed=seed, n_perm=n_perm)
    top = per_wallet.most_common(1)[0][0] if per_wallet else None
    loo = [bool(u["kols"] - {top}) for u in units]
    out["loo_wallet"], out["loo_S"] = top, statistic(units, loo)
    out["loo_p"] = perm_p(units, loo, groups, seed=seed, n_perm=n_perm)
    days = {_day(u["t0"]) for u, lab in zip(units, labels) if lab and u["win"] == 1}
    out["win_days"] = len(days - {None})
    if notdecu_sources:                       # descriptive extra, not a condition (Gamma)
        keep = [u["source_user_id"] not in notdecu_sources for u in units]
        sub = [u for u, k in zip(units, keep) if k]
        sub_labels = [lab for lab, k in zip(labels, keep) if k]
        out["without_notdecu"] = {"units": len(sub), "kol_units": sum(sub_labels),
                                  "S": statistic(sub, sub_labels),
                                  "p": perm_p(sub, sub_labels, strata(sub), seed=seed, n_perm=n_perm)}
    for cond, ok in (("S > 0 and p <= %.2f" % ALPHA, out["S"] > 0 and out["p"] <= ALPHA),
                     ("without the top wallet S > 0 and p <= %.2f" % LOO_ALPHA, out["loo_S"] > 0 and out["loo_p"] <= LOO_ALPHA),
                     ("KOL-preceded wins on >= %d UTC days" % MIN_WIN_DAYS, out["win_days"] >= MIN_WIN_DAYS)):
        if not ok:
            out["verdict"] = "not shown: " + cond
            break
    else:
        out["verdict"] = "pass"
    return out


def kol_addresses(ledger):
    """The Axiom Solana addresses, our roster's proven addresses removed (never counted as KOLs)."""
    axiom = {r["address"] for r in ledger.execute("SELECT address FROM axiom_trader_directory WHERE chain='solana'")}
    return axiom - set(roster_addresses(ledger))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--test", choices=("read1", "read2"), help="the pre-registered group test")
    ap.add_argument("--after", help="read 2: only units first read after this ISO time (the stamp)")
    ap.add_argument("--fx-band", choices=("a1b", "a1"), default="a1b",
                    help="amendment A1b is the primary; a1 is its labelled sensitivity line")
    args = ap.parse_args()
    if args.test:
        ledger, graph = ro(STATE / "sniper_tracker.db"), ro(STATE / "data" / "memecoin_wallet_graph.db")
        if args.test == "read2" and not args.after:
            ap.error("read2 needs --after <stamp time>")
        waiting = a1b_pending(graph)
        if waiting:
            print(json.dumps({"stopped": "a1b re-read pending n=%d" % waiting}))
            return 0
        share = control_share(graph)
        if share is None or share < CONTROL_MIN:
            print(json.dumps({"stopped": "3b control %s < %.2f" % (share, CONTROL_MIN)}))
            return 0
        notdecu = {r["source_user_id"] for r in ledger.execute(
            "SELECT source_user_id FROM memecoin_wallets WHERE handle='notdecu'")}
        units = test_units(graph, kol_addresses(ledger), after=args.after if args.test == "read2" else None,
                           fx_band=args.fx_band)
        out = dict(group_test(units, notdecu_sources=notdecu), read=args.test, control=round(share, 4),
                   fx_band=args.fx_band)
        print(json.dumps(out, indent=1, default=str))
        return 0
    base, rows = build(ro(STATE / "sniper_tracker.db"), ro(STATE / "data" / "memecoin_wallet_graph.db"),
                       axiom_lists(STATE / "data" / "axiom_raw.db"))
    if args.json:
        print(json.dumps({"baseline": base, "wallets": rows}, indent=1, default=str))
        return 0
    print(json.dumps(base))
    for r in rows:
        print("%-12s k=%d rows=%d lead_med=%s lists=%s roster=%s preceded=%s %s" % (
            r["address"][:12], r["k_coins"], r["early_rows"], r["lead_s_median"], r["axiom_7d_lists"],
            r["on_roster"], r["sources_preceded"], ",".join(r["x_handles"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
