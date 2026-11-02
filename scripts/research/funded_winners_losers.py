#!/usr/bin/env python3
"""What separates funded winners from funded losers, feature by feature, with a held-out check.

Pre-registered in HANDOFF.md at 2026-09-29T21:24:49Z (Gamma, "PRE-REGISTRATION, funded WINNERS vs
LOSERS") before any PnL in the window was computed. Read-only: the ledger only, no RPC.

Population: funded closes (execution_mode='real', is_open=0, realized_pnl known), signal_source not
'manual', closed on or after --since. Winner = realized_pnl > 0. Return = realized / entry_notional.

Every feature is an ENTRY-TIME value from a stored row: classification facts, trace substages,
position/attempt columns, the source wallet's record from closes BEFORE this open, and its earlier
buys of the same mint. Exit reason, hold time and the executable top and bottom (whole-bag sell
quotes against the fill, from memecoin_position_exec_extremes) are outcome-side and descriptive
only. A position with no extremes row carries None there, never a display number.

HOLD-OUT: rows split at the median open time. The early half (H1) nominates a feature (AUC CI
excludes 0.5); the late half (H2) must confirm it in the same direction. The money effect is the H2
mean return above vs below the H1 median of the feature. No feature is ranked on the half that
confirms it.

    python3 scripts/research/funded_winners_losers.py [--since 2026-09-20T00:00:00]
    python3 scripts/research/funded_winners_losers.py --selftest
"""
import argparse
import collections
import contextlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
LEDGER = REPO / "sniper_tracker.db"
DRAWS, SEED = 2000, 20260929
TAIL = 0.10

FACT_NUM = ("effective_mcap", "liquidity", "pair_age_s", "volume_m5", "volume_h1", "runup_pct",
            "giveback_pct", "momentum_observations", "momentum_span_s", "consensus_n_60m",
            "regime_current_buyers", "regime_current_coins", "regime_baseline_buys",
            "liquidity_pair_count")
CLS_CAT = ("coin_band", "liquidity_class", "market_regime", "momentum_phase", "lifecycle", "data_quality")
POS_NUM = ("their_size_usd", "entry_lag_s", "entry_notional", "total_copy_drift_pct", "source_drift_pct",
           "execution_slippage_pct", "fill_gap_pct", "quote_age_ms", "entry_liquidity",
           "entry_token_age_s", "entry_mcap")
ATT_NUM = ("chase_naive_premium_pct", "chase_source_impact_pct", "chase_effective_cap_pct", "total_ms")
OUTCOME_SIDE = ("exit_reason", "hold_s", "exec_high_pct", "exec_low_pct", "time_to_high_s")


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def ratio(a, b):
    a, b = num(a), num(b)
    return a / b if a is not None and b else None


def auc(win, lose):
    """P(a winner's value > a loser's value), ties count half. None if either side is empty."""
    win, lose = np.asarray(win, float), np.sort(np.asarray(lose, float))
    if not len(win) or not len(lose):
        return None
    lo = np.searchsorted(lose, win, "left")
    hi = np.searchsorted(lose, win, "right")
    return float((lo + 0.5 * (hi - lo)).sum() / (len(win) * len(lose)))


def auc_ci(vals, labels, rng):
    vals, labels = np.asarray(vals, float), np.asarray(labels, bool)
    point = auc(vals[labels], vals[~labels])
    if point is None:
        return None, None, None
    n, draws = len(vals), []
    for _ in range(DRAWS):
        i = rng.integers(0, n, n)
        a = auc(vals[i][labels[i]], vals[i][~labels[i]])
        if a is not None:
            draws.append(a)
    return point, float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))


def diff_ci(a, b, rng):
    """Mean(a) - mean(b) with a 95% bootstrap CI, each side resampled on its own."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 2 or len(b) < 2:
        return None, None, None
    d = [rng.choice(a, len(a)).mean() - rng.choice(b, len(b)).mean() for _ in range(DRAWS)]
    return float(a.mean() - b.mean()), float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))


def verdict(h1, h2):
    """h1, h2 = (point, lo, hi) AUCs. candidate / replicated / direction held / not replicated."""
    p1, lo1, hi1 = h1
    if p1 is None or not (lo1 > 0.5 or hi1 < 0.5):
        return "not a candidate"
    up = lo1 > 0.5
    p2, lo2, hi2 = h2
    if p2 is None:
        return "no H2 data"
    if (up and lo2 > 0.5) or (not up and hi2 < 0.5):
        return "REPLICATED"
    if (p2 > 0.5) == up:
        return "direction held"
    return "not replicated"


def substage(trace, name):
    evs = [s for s in (trace.get("substage_events") or []) if s.get("name") == name]
    return evs[-1] if evs else {}


def route_label(rt):
    plan = rt.get("buy_route_plan") or []
    try:
        return plan[-1]["swapInfo"]["label"]
    except (KeyError, IndexError, TypeError):
        return None


def transfer_fee_bps(reason):
    for part in str(reason or "").split():
        if part.startswith("transfer_fee_bps="):
            return num(part.split("=", 1)[1])
    return None


def features(r, src_prior, mint_prior):
    cl = json.loads(r["classification_json"] or "{}")
    facts = cl.get("facts") or {}
    tr = json.loads(r["trace_json"] or "{}")
    f = {k: num(facts.get(k)) for k in FACT_NUM}
    mc = f["effective_mcap"]
    f.update(liq_over_mcap=ratio(facts.get("liquidity"), mc), m5_over_mcap=ratio(facts.get("volume_m5"), mc),
             h1_over_mcap=ratio(facts.get("volume_h1"), mc),
             m5_over_h1=ratio(facts.get("volume_m5"), facts.get("volume_h1")),
             risk_scale=num(cl.get("risk_scale")))
    for k in CLS_CAT:
        f[k] = cl.get(k)
    ss, vg, oc = substage(tr, "sell_share"), substage(tr, "volume_gate"), substage(tr, "onchain_volume_m5")
    ch, ep, sb = (substage(tr, "chase_quote_confirmation"), substage(tr, "execution_premium"),
                  substage(tr, "source_buy_floor"))
    rt, ts_, pp = substage(tr, "roundtrip_cap"), substage(tr, "token_safety"), substage(tr, "post_peak_confirmation")
    dex = (substage(tr, "dex_pair_selection").get("activity_pick") or {}).get("dex")
    f.update(sell_share_pct=num(ss.get("share_pct")), m5_txns=num(ss.get("txns")),
             organic_m5=num(vg.get("organic_volume_m5")), onchain_m5_usd=num(oc.get("usd")),
             chase_gap_pct=num(ch.get("gap_pct")), exec_premium_ratio=num(ep.get("ratio")),
             source_size_pct_mcap=num(sb.get("ratio_pct")), roundtrip_cost_pct=num(rt.get("cost_pct")),
             transfer_fee_bps=transfer_fee_bps(ts_.get("reason")),
             post_peak_nominated=(None if not pp else str(bool(pp.get("nominated")))),
             venue=route_label(rt) or dex)
    for k in POS_NUM:
        f[k] = num(r[k])
    for k in ATT_NUM:
        f[k] = num(r[k])
    f["chase_decided_by"] = r["chase_decided_by"]
    try:
        f["decision_to_fill_s"] = ts(r["opened_recorded_at"]) - ts(r["decision_started_at"])
    except (TypeError, ValueError):
        f["decision_to_fill_s"] = None
    f.update(network_id=r["network_id"], lane=r["signal_source"], source=r["source_user_id"],
             hour_utc_bucket="%02d-%02d" % (6 * (datetime.fromtimestamp(ts(r["opened_recorded_at"]),
                                                                        timezone.utc).hour // 6),
                                           6 * (datetime.fromtimestamp(ts(r["opened_recorded_at"]),
                                                                       timezone.utc).hour // 6) + 6),
             src_prior_n=src_prior[0], src_prior_wr=src_prior[1], src_prior_sum=src_prior[2],
             src_prior_buys_mint=mint_prior)
    return f


NUMERIC = (FACT_NUM + ("liq_over_mcap", "m5_over_mcap", "h1_over_mcap", "m5_over_h1", "risk_scale",
                       "sell_share_pct", "m5_txns", "organic_m5", "onchain_m5_usd", "chase_gap_pct",
                       "exec_premium_ratio", "source_size_pct_mcap", "roundtrip_cost_pct", "transfer_fee_bps")
           + POS_NUM + ATT_NUM + ("decision_to_fill_s", "src_prior_n", "src_prior_wr", "src_prior_sum",
                                  "src_prior_buys_mint"))
CATEGORICAL = CLS_CAT + ("post_peak_nominated", "venue", "chase_decided_by", "network_id", "lane",
                         "hour_utc_bucket", "source")


def load(since):
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % LEDGER, uri=True, timeout=30)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT p.id, p.mint, p.network_id, p.signal_source, p.source_user_id, p.entry_notional, "
            "p.realized_pnl, p.exit_reason, p.opened_recorded_at, p.closed_at, p.entry_price, "
            "p.their_size_usd, p.entry_lag_s, p.total_copy_drift_pct, p.source_drift_pct, "
            "p.execution_slippage_pct, p.fill_gap_pct, p.quote_age_ms, p.entry_liquidity, "
            "p.entry_token_age_s, p.entry_mcap, p.origin_event_id, a.decision_started_at, a.classification_json, "
            "a.trace_json, a.chase_naive_premium_pct, a.chase_source_impact_pct, a.chase_effective_cap_pct, "
            "a.chase_decided_by, a.total_ms FROM memecoin_positions p "
            "JOIN memecoin_entry_attempts a ON a.attempt_id=p.attempt_id "
            "WHERE p.execution_mode='real' AND p.is_open=0 AND p.realized_pnl IS NOT NULL "
            "AND p.signal_source != 'manual' AND p.closed_at >= ? ORDER BY p.opened_recorded_at", (since,)).fetchall()
        history = c.execute(
            "SELECT source_user_id, closed_at, realized_pnl FROM memecoin_positions WHERE execution_mode='real' "
            "AND is_open=0 AND realized_pnl IS NOT NULL AND closed_at IS NOT NULL").fetchall()
        from sniper.persistence import db_memecoin as m
        extremes = m.funded_exec_extremes(c, [r["id"] for r in rows])
        out = []
        for r in rows:
            exec_pct = m.exec_extremes_pct(extremes.get(r["id"]), r["entry_price"],
                                           opened_at=r["opened_recorded_at"])
            opened = ts(r["opened_recorded_at"])
            prior = [p for s, cl, p in history if s == r["source_user_id"] and ts(cl) < opened]
            src = (len(prior), (sum(p > 0 for p in prior) / len(prior)) if prior else None, sum(prior))
            ev = c.execute("SELECT id FROM memecoin_events WHERE fomo_event_id=?", (r["origin_event_id"],)).fetchone()
            mint_prior = (c.execute("SELECT COUNT(*) FROM memecoin_events WHERE mint=? AND source_user_id=? "
                                    "AND side='buy' AND id<?", (r["mint"], r["source_user_id"], ev[0])).fetchone()[0]
                          if ev else None)
            ret = r["realized_pnl"] / r["entry_notional"] if r["entry_notional"] else None
            out.append({"id": r["id"], "mint": r["mint"], "pnl": r["realized_pnl"], "ret": ret,
                        "win": r["realized_pnl"] > 0, "opened": opened, "closed_day": str(r["closed_at"])[:10],
                        "exit_reason": r["exit_reason"], "hold_s": ts(r["closed_at"]) - opened,
                        "exec_high_pct": exec_pct["high_pct"], "exec_low_pct": exec_pct["low_pct"],
                        "time_to_high_s": exec_pct["time_to_high_s"],
                        "f": features(r, src, mint_prior)})
    return out


def pct(x):
    return "   n/a" if x is None else "%+6.1f%%" % (100 * x)


def daily(rows):
    print("DAILY PnL by UTC close day (funded, non-manual entries)")
    by = collections.defaultdict(list)
    for r in rows:
        by[r["closed_day"]].append(r)
    for d in sorted(by):
        rs = by[d]
        s = sum(r["pnl"] for r in rs)
        print("  %s n=%3d wins %3d  sum $%+9.2f  %s" % (d, len(rs), sum(r["win"] for r in rs), s,
                                                     "LOSING DAY" if s < 0 else ""))
    s = sum(r["pnl"] for r in rows)
    print("  total n=%d wins %d sum $%+.2f; tails >= +10%%: %d, <= -10%%: %d" % (
        len(rows), sum(r["win"] for r in rows), s, sum(r["ret"] >= TAIL for r in rows),
        sum(r["ret"] <= -TAIL for r in rows)))


def numeric_table(rows, h1, h2, rng, label="winner vs loser", key="win"):
    out = []
    print("\nNUMERIC FEATURES, %s: AUC = P(value higher in the first group); CI from %d draws" % (label, DRAWS))
    print("  %-24s %5s %6s %12s %12s  %-22s %-22s %-22s %s" % (
        "feature", "n", "miss", "med first", "med second", "AUC full", "AUC H1", "AUC H2", "verdict"))
    for name in NUMERIC:
        def xy(rs):
            v = [(r["f"][name], r[key]) for r in rs if r["f"][name] is not None]
            return [a for a, _ in v], [b for _, b in v]
        vals, labs = xy(rows)
        miss = 1 - len(vals) / len(rows)
        if len(set(vals)) < 2:
            print("  %-24s %5d %5.0f%%  constant or empty" % (name, len(vals), 100 * miss))
            continue
        full = auc_ci(vals, labs, rng)
        a1, a2 = auc_ci(*xy(h1), rng), auc_ci(*xy(h2), rng)
        v = verdict(a1, a2)
        w = [x for x, l in zip(vals, labs) if l]
        lz = [x for x, l in zip(vals, labs) if not l]

        def fmt(a):
            return "n/a" if a[0] is None else "%.3f [%.3f,%.3f]" % a
        print("  %-24s %5d %5.0f%% %12.4g %12.4g  %-22s %-22s %-22s %s" % (
            name, len(vals), 100 * miss, np.median(w) if w else float("nan"),
            np.median(lz) if lz else float("nan"), fmt(full), fmt(a1), fmt(a2), v))
        out.append((name, miss, full, a1, a2, v))
    return out


def money_effect(name, h1, h2, rng):
    """H1 median split, H2 mean return above minus below, with CI and sums."""
    thr_vals = [r["f"][name] for r in h1 if r["f"][name] is not None]
    if not thr_vals:
        return None
    thr = float(np.median(thr_vals))
    hi = [r for r in h2 if r["f"][name] is not None and r["f"][name] > thr]
    lo = [r for r in h2 if r["f"][name] is not None and r["f"][name] <= thr]
    d = diff_ci([r["ret"] for r in hi], [r["ret"] for r in lo], rng)
    return thr, hi, lo, d


def categorical_table(rows, h1, h2, rng):
    out = []
    print("\nCATEGORICAL FEATURES: per level n, win rate, mean return, sum (full window)")
    for name in CATEGORICAL:
        levels = collections.Counter(r["f"][name] for r in rows)
        print("  %s (%d levels)" % (name, len(levels)))
        for lvl, n in levels.most_common(12 if name == "source" else 30):
            rs = [r for r in rows if r["f"][name] == lvl]
            print("    %-34s n=%3d  win %3.0f%%  mean %s  sum $%+9.2f" % (
                str(lvl)[:34], n, 100 * sum(r["win"] for r in rs) / n,
                pct(sum(r["ret"] for r in rs) / n), sum(r["pnl"] for r in rs)))
        for lvl in levels:
            a1 = [r for r in h1 if r["f"][name] == lvl]
            a2 = [r for r in h2 if r["f"][name] == lvl]
            if len(a1) < 10 or len(a2) < 10:
                continue
            r1 = [r for r in h1 if r["f"][name] != lvl]
            r2 = [r for r in h2 if r["f"][name] != lvl]
            w1 = diff_ci([float(r["win"]) for r in a1], [float(r["win"]) for r in r1], rng)
            w2 = diff_ci([float(r["win"]) for r in a2], [float(r["win"]) for r in r2], rng)
            m2 = diff_ci([r["ret"] for r in a2], [r["ret"] for r in r2], rng)
            if w1[0] is None or w2[0] is None or m2[0] is None:
                continue                   # no contrast: the level is (nearly) the whole half
            cand = w1[0] is not None and (w1[1] > 0 or w1[2] < 0)
            if not cand:
                v = "not a candidate"
            elif w2[0] is not None and ((w1[1] > 0 and w2[1] > 0) or (w1[2] < 0 and w2[2] < 0)):
                v = "REPLICATED"
            elif w2[0] is not None and (w2[0] > 0) == (w1[1] > 0):
                v = "direction held"
            else:
                v = "not replicated"
            out.append((name, lvl, len(a1), len(a2), w1, w2, m2, v))
    print("\n  CATEGORICAL HOLD-OUT (level vs rest, levels with n>=10 in both halves): win-rate diff H1 / H2, H2 mean-return diff")
    for name, lvl, n1, n2, w1, w2, m2, v in out:
        print("    %-16s %-26s n1=%3d n2=%3d  WR H1 %+5.0f [%+4.0f,%+4.0f]  WR H2 %+5.0f [%+4.0f,%+4.0f]  ret H2 %s [%s,%s]  %s" % (
            name, str(lvl)[:26], n1, n2, 100 * w1[0], 100 * w1[1], 100 * w1[2], 100 * w2[0], 100 * w2[1],
            100 * w2[2], pct(m2[0]), pct(m2[1]), pct(m2[2]), v))
    return out


def missing_indicators(rows, h1, h2, rng):
    print("\nMISSING-VALUE INDICATORS (numeric fields missing on 10-90% of rows): present vs missing")
    for name in NUMERIC:
        miss = sum(r["f"][name] is None for r in rows) / len(rows)
        if not 0.10 <= miss <= 0.90:
            continue
        pres = [r for r in rows if r["f"][name] is not None]
        gone = [r for r in rows if r["f"][name] is None]
        d = diff_ci([r["ret"] for r in pres], [r["ret"] for r in gone], rng)
        d2 = diff_ci([r["ret"] for r in h2 if r["f"][name] is not None],
                     [r["ret"] for r in h2 if r["f"][name] is None], rng)
        print("  %-24s missing %3.0f%%  present win %3.0f%% / missing win %3.0f%%  ret diff full %s [%s,%s]  H2 %s" % (
            name, 100 * miss, 100 * sum(r["win"] for r in pres) / len(pres),
            100 * sum(r["win"] for r in gone) / len(gone), pct(d[0]), pct(d[1]), pct(d[2]), pct(d2[0])))


def combos(names, rows, h1, h2):
    print("\nTOP-3 PAIRWISE BINS at H1 medians (full window | H2)")
    thr = {n: float(np.median([r["f"][n] for r in h1 if r["f"][n] is not None])) for n in names}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            print("  %s x %s  (cuts %.4g, %.4g)" % (a, b, thr[a], thr[b]))
            for sa in ("low", "high"):
                for sb in ("low", "high"):
                    def cell(rs):
                        return [r for r in rs if r["f"][a] is not None and r["f"][b] is not None
                                and (r["f"][a] > thr[a]) == (sa == "high") and (r["f"][b] > thr[b]) == (sb == "high")]
                    c, c2 = cell(rows), cell(h2)

                    def desc(rs):
                        if not rs:
                            return "n=0"
                        return "n=%3d win %3.0f%% mean %s sum $%+8.2f" % (
                            len(rs), 100 * sum(r["win"] for r in rs) / len(rs),
                            pct(sum(r["ret"] for r in rs) / len(rs)), sum(r["pnl"] for r in rs))
                    print("    %-5s %-5s  %s | %s" % (sa, sb, desc(c), desc(c2)))


def within(rows, rng):
    print("\nWITHIN-GROUP DIFFERENCES (DESCRIPTIVE, no hold-out): big vs small, per group")
    wins = [r for r in rows if r["win"]]
    loss = [r for r in rows if not r["win"]]
    for label, grp, key in (("winners: >= +10% vs (0, +10%)", wins, lambda r: r["ret"] >= TAIL),
                            ("losers: <= -10% vs (-10%, 0]", loss, lambda r: r["ret"] <= -TAIL)):
        big = sum(key(r) for r in grp)
        print("  %s  (%d vs %d)" % (label, big, len(grp) - big))
        for name in NUMERIC:
            v = [(r["f"][name], key(r)) for r in grp if r["f"][name] is not None]
            if len(set(x for x, _ in v)) < 2 or sum(b for _, b in v) < 5 or sum(not b for _, b in v) < 5:
                continue
            a = auc_ci([x for x, _ in v], [b for _, b in v], rng)
            if a[1] > 0.5 or a[2] < 0.5:
                print("    %-24s AUC %.3f [%.3f,%.3f]  (big higher if > 0.5)" % (name, *a))
    print("\n  EXIT REASONS (outcome-side, descriptive)")
    for label, grp in (("winners", wins), ("losers", loss)):
        cnt = collections.Counter(r["exit_reason"] for r in grp)
        holds = sorted(r["hold_s"] for r in grp)
        print("    %-8s %s  hold p50 %.0fs p90 %.0fs" % (label, dict(cnt.most_common()), holds[len(holds) // 2],
                                                        holds[int(0.9 * (len(holds) - 1))]))


def blind_spots(rows):
    print("\nBLIND-SPOT INVENTORY: features NULL, constant or unknown on > 30% of rows")
    for name in NUMERIC + CATEGORICAL:
        vals = [r["f"][name] for r in rows]
        unknown = sum(v is None or v in ("", "unknown") for v in vals) / len(vals)
        distinct = len({v for v in vals if v is not None})
        if unknown > 0.30 or distinct <= 1:
            print("  %-24s unknown %3.0f%%  distinct values %d" % (name, 100 * unknown, distinct))


def report(args):
    rng = np.random.default_rng(SEED)
    rows = load(args.since)
    daily(rows)
    cut = float(np.median([r["opened"] for r in rows]))
    h1 = [r for r in rows if r["opened"] < cut]
    h2 = [r for r in rows if r["opened"] >= cut]
    print("\nHOLD-OUT SPLIT at open %s: H1 n=%d (win %d), H2 n=%d (win %d)" % (
        datetime.fromtimestamp(cut, timezone.utc).isoformat(), len(h1), sum(r["win"] for r in h1),
        len(h2), sum(r["win"] for r in h2)))
    res = numeric_table(rows, h1, h2, rng)
    cand = [x for x in res if x[5] != "not a candidate"]
    print("\n  tested %d numeric features; H1 candidates %d (about %.1f expected by chance at 5%%)" % (
        len(res), len(cand), 0.05 * len(res)))
    print("\nHELD-OUT MONEY EFFECT (H2 mean return, above minus at-or-below the H1 median) for H1 candidates:")
    for name, _m, _f, a1, a2, v in cand:
        me = money_effect(name, h1, h2, rng)
        if me is None:
            continue
        thr, hi, lo, d = me
        print("  %-24s cut %-10.4g  H2 high n=%3d mean %s sum $%+8.2f | low n=%3d mean %s sum $%+8.2f | diff %s [%s,%s]  %s" % (
            name, thr, len(hi), pct(np.mean([r["ret"] for r in hi]) if hi else None), sum(r["pnl"] for r in hi),
            len(lo), pct(np.mean([r["ret"] for r in lo]) if lo else None), sum(r["pnl"] for r in lo),
            pct(d[0]), pct(d[1]), pct(d[2]), v))
    categorical_table(rows, h1, h2, rng)
    missing_indicators(rows, h1, h2, rng)
    top = sorted([x for x in res if x[5] == "REPLICATED"], key=lambda x: -abs(x[4][0] - 0.5))[:3]
    if len(top) >= 2:
        combos([x[0] for x in top], rows, h1, h2)
    else:
        print("\nTOP-3 PAIRWISE BINS: fewer than 2 replicated numeric features, no bins")
    within(rows, rng)
    if args.sensitivity:
        keep = [r for r in rows if r["exit_reason"] not in ("manual", "written_off")]
        k1 = [r for r in keep if r["opened"] < cut]
        k2 = [r for r in keep if r["opened"] >= cut]
        print("\nSENSITIVITY: operator-closed exits removed (manual, written_off): n=%d" % len(keep))
        numeric_table(keep, k1, k2, rng, label="winner vs loser, operator exits removed")
    blind_spots(rows)


def selftest():
    assert auc([3, 4], [1, 2]) == 1.0 and auc([1], [1]) == 0.5 and auc([1, 2], [2, 3]) == 0.125
    assert auc([], [1]) is None
    assert verdict((0.6, 0.55, 0.65), (0.58, 0.52, 0.64)) == "REPLICATED"
    assert verdict((0.6, 0.55, 0.65), (0.53, 0.45, 0.61)) == "direction held"
    assert verdict((0.6, 0.55, 0.65), (0.45, 0.40, 0.52)) == "not replicated"
    assert verdict((0.4, 0.35, 0.45), (0.42, 0.36, 0.48)) == "REPLICATED"
    assert verdict((0.55, 0.49, 0.61), (0.9, 0.8, 0.95)) == "not a candidate"
    assert transfer_fee_bps("ok transfer_fee_bps=300 fee_limit_bps=300") == 300.0
    assert transfer_fee_bps("ok") is None
    assert route_label({"buy_route_plan": [{"swapInfo": {"label": "A"}}, {"swapInfo": {"label": "Raydium CP"}}]}) == "Raydium CP"
    assert ratio(5, 0) is None and ratio(None, 3) is None and ratio(6, 3) == 2.0
    rng = np.random.default_rng(1)
    p, lo, hi = auc_ci([5, 6, 7, 1, 2, 3], [True, True, True, False, False, False], rng)
    assert p == 1.0 and lo <= hi
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--since", default="2026-09-20T00:00:00")
    ap.add_argument("--sensitivity", action="store_true", help="also rerun the numeric table without operator exits")
    a = ap.parse_args()
    return selftest() if a.selftest else report(a)


if __name__ == "__main__":
    main()
