#!/usr/bin/env python3
"""POST60 negative diagnostic — READ-ONLY. No writes, no funded action, no roster action.

THIS IS NOT THE PUMP20 SPECIFICATION. The PUMP20 gate additionally requires the most recent 20
coins, a 14-day window, a 15-coin evidence floor and 80% matched-control coverage. On this tape
that combination evaluates ONE wallet-chain and passes none, so it is not yet runnable. This is a
deliberately weaker 31-day NEGATIVE diagnostic: first buys only, an 8-coin floor, no positive gate.
Version the two separately and never quote one as the other.

Multiplicity is not optional here. Reading a 90% CI bound per wallet is an uncorrected one-sided
5% test; across ~142 wallet-chains that expects ~7 false rejections. Correcting it took a published
list of 17 rejections down to 1 — the other 16 were the family-wise error rate.

What it measures: POST-IMPACT CONTINUATION. After 60s — long enough for the source wallet's own buy
and the immediate reaction to print — do its coins outperform contemporaneous market-cap-matched
coins signalled by other wallets?

It is NOT "return excluding self-impact". That quantity is not identifiable here: `their_price`
carries the source's own impact and `wouldbe_price` is populated on 0.9% of buys. A 60s baseline
removes the obvious contamination without recovering the counterfactual path.

DIRECTION OF USE IS ASYMMETRIC AND DELIBERATE:
  strong negative -> POST60_STRATEGY_REJECTED. Rejects a post-60s hold policy, NOTHING MORE. It
                     cannot revoke wallet authority: the screen is blind to the first 60 seconds,
                     which is exactly where a fast exit policy operates.
  positive        -> nothing. It never observes the executable price our own $50 order would get.
The positive gate is intentionally absent. Do not add one.

Identity rules that are load-bearing:
  * wallets are keyed by `source_user_id`, NEVER by handle — 412 wallet ids carry more than one
    handle (one carries seven) and 14 handles map to more than one wallet id;
  * the unit of observation is ONE COIN (earliest signal per wallet/chain/mint), never one buy —
    measured clustering runs 2.5x-9.9x signals per coin and repeat buys are endogenous;
  * chains are never pooled, and 4663/robin fold to one identity exactly once.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict

import numpy as np

DB = "/home/bigpapi/Desktop/Sniper/sniper_tracker.db"
SPEC = "post60-neg-diagnostic-v2"
CHAINS = ("solana", "robin")
BASE_S, TARGET_S = 60, 900
# The 900s cap passes 100% of marks; a 30s cap on the 60s leg costs 28% of baselines and alone
# collapses the evaluable set to one wallet. 60s on the baseline leg keeps the intent.
LATE_BASE_S, LATE_TARGET_S = 60.0, 135.0
MATCH_MIN_S, MATCH_WIDE_S = 1800, 7200          # +/-30min, widened once to +/-2h
MCAP_LOG_TOL = 0.5
MIN_CONTROLS = 10
MIN_COMPLETENESS = 0.80


def canon_chain(network_id):
    """One chain identity. The tape writes both 4663 and robin for the same chain."""
    return "robin" if network_id in ("4663", "robin") else network_id


def load_episodes(conn, since, as_of, first_buy_only=True):
    """One row per (source_user_id, chain, mint), in TWO ORDERED STAGES.

    STAGE 1 selects the episode — the wallet's EARLIEST signal on that coin — blind to marks.
    STAGE 2 validates that episode's own marks and DROPS it when they are invalid.

    The order is load-bearing. Validating first and then taking the earliest SURVIVOR silently
    substitutes a later entry whenever the first lacks marks, which selects on a variable
    correlated with the outcome: coins that die fast are exactly the ones that lose their marks.

    Every mark must have been observed BEFORE `as_of`, or a historical rerun quietly consumes
    marks from its own future. A NULL `lateness_s` is rejected rather than read as zero — unknown
    freshness is not perfect freshness.

    Returns (valid_episodes, {(uid, chain): n_selected}).
    """
    rows = conn.execute(
        """
        SELECT o.outcome_id, o.source_user_id, o.handle, o.network_id, o.mint,
               o.signal_observed_at, e.their_market_cap AS mcap
        FROM memecoin_signal_outcomes_v2 o
        -- upstream_event_id matches fomo_event_id, NOT memecoin_events.id (joining on id
        -- returns zero rows and the screen silently passes on an empty set)
        JOIN memecoin_events e ON e.fomo_event_id = o.upstream_event_id
        WHERE o.signal_observed_at >= ? AND o.signal_observed_at < ?
          AND COALESCE(o.source_user_id,'') != '' AND COALESCE(o.mint,'') != ''
          -- An add-on is not a new observation of the wallet's judgement: it is conditioned on
          -- the first buy already working. 25,837 of 52,789 outcomes are not first buys.
          AND (? = 0 OR COALESCE(e.is_first_buy, 0) = 1)
        """,
        (since, as_of, 1 if first_buy_only else 0),
    ).fetchall()

    selected = {}
    for r in rows:
        key = (r["source_user_id"], canon_chain(r["network_id"]), r["mint"])
        ts = str(r["signal_observed_at"] or "")
        if key not in selected or ts < selected[key]["ts"]:
            selected[key] = {
                "oid": r["outcome_id"], "uid": r["source_user_id"], "handle": r["handle"],
                "net": canon_chain(r["network_id"]), "mint": r["mint"], "ts": ts,
                "mcap": float(r["mcap"]) if r["mcap"] and float(r["mcap"]) > 0 else None,
            }

    marks = defaultdict(dict)
    for r in conn.execute(
        """SELECT outcome_id, target_s, price, lateness_s FROM memecoin_horizon_marks_v2
           WHERE status='observed' AND price > 0 AND target_s IN (?, ?)
             AND quality IN ('ok','probe_ok')      -- redundant today; it is the data contract
             AND observed_at IS NOT NULL AND observed_at < ?
             AND lateness_s IS NOT NULL AND actual_age_s >= target_s""",
            (BASE_S, TARGET_S, as_of)):
        marks[r["outcome_id"]][int(r["target_s"])] = (float(r["price"]), float(r["lateness_s"]))

    n_selected = defaultdict(int)
    episodes = []
    for ep in selected.values():
        n_selected[(ep["uid"], ep["net"])] += 1
        hs = marks.get(ep["oid"], {})
        if BASE_S not in hs or TARGET_S not in hs:
            continue
        (p_base, late_base), (p_tgt, late_tgt) = hs[BASE_S], hs[TARGET_S]
        if p_base <= 0 or late_base > LATE_BASE_S or late_tgt > LATE_TARGET_S:
            continue
        ep["ret"] = 100.0 * (p_tgt / p_base - 1.0)
        episodes.append(ep)
    return episodes, dict(n_selected)


def epoch(ts):
    """ISO-8601 -> unix seconds. The tape mixes 'Z' and '+00:00'; both parse after this swap."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def build_controls(eps):
    """Matched control set per episode: same chain, different WALLET ID, different mint, nearby in
    time, within 0.5 log10 of market cap, collapsed to one row per control mint (nearest in time).

    Market cap comes from `memecoin_events.their_market_cap` (96.9% populated). An episode with no
    market cap, or with fewer than MIN_CONTROLS matches, is UNMATCHED: it contributes to
    POST60_ABS20 but never to PUMP20, because a residual against an unmatched baseline is not a
    residual.
    """
    for e in eps:
        e["t"] = epoch(e["ts"])
        e["lmc"] = float(np.log10(e["mcap"])) if e["mcap"] else None

    by_chain = defaultdict(list)
    for e in eps:
        if e["t"] is not None:
            by_chain[e["net"]].append(e)
    for pool in by_chain.values():
        pool.sort(key=lambda x: x["t"])

    for e in eps:
        e["control"] = None
        if e["t"] is None or e["lmc"] is None:
            continue
        for window in (MATCH_MIN_S, MATCH_WIDE_S):
            best = {}
            for o in by_chain[e["net"]]:
                if o["uid"] == e["uid"] or o["mint"] == e["mint"] or o["lmc"] is None:
                    continue
                dt = abs(o["t"] - e["t"])
                if dt > window or abs(o["lmc"] - e["lmc"]) > MCAP_LOG_TOL:
                    continue
                if o["mint"] not in best or dt < best[o["mint"]][0]:
                    best[o["mint"]] = (dt, o["ret"])
            if len(best) >= MIN_CONTROLS:
                e["control"] = np.array([v[1] for v in best.values()])
                break


def winsor_caps(values):
    a = np.asarray(values, dtype=float)
    return (float(np.percentile(a, 1)), float(np.percentile(a, 99))) if a.size else (-1e18, 1e18)


def screen(eps, chain_key, n_selected, min_coins, boot, perm, rng):
    pool = [e for e in eps if e["net"] == chain_key]
    if not pool:
        return []
    r_lo, r_hi = winsor_caps([e["ret"] for e in pool])
    matched = [e for e in pool if e["control"] is not None]
    d_lo, d_hi = winsor_caps([e["ret"] - float(np.median(e["control"])) for e in matched])

    by_wallet = defaultdict(list)
    for e in pool:
        by_wallet[e["uid"]].append(e)

    out = []
    for uid, rows in by_wallet.items():
        if len(rows) < min_coins:
            continue
        sel = n_selected.get((uid, chain_key), len(rows))
        r = np.clip([e["ret"] for e in rows], r_lo, r_hi)
        m = [e for e in rows if e["control"] is not None]
        res = {
            "uid": uid, "handle": max(rows, key=lambda e: e["ts"])["handle"] or uid[:8],
            "chain": chain_key, "n_coins": len(rows), "n_selected": sel,
            "completeness": (len(rows) / sel) if sel else 0.0, "n_matched": len(m),
            "abs20": float(r.mean()), "median_ret": float(np.median(r)),
            "winsor_r": [r_lo, r_hi], "winsor_d": [d_lo, d_hi],
            "pump20": None, "pump20_ci90": None, "p_perm": None,
        }
        # bootstrap by COIN: the resampling unit must be the cluster, not the buy
        idx = rng.integers(0, len(r), size=(boot, len(r)))
        means = r[idx].mean(axis=1)
        res["abs20_ci90"] = [float(x) for x in np.percentile(means, [5, 95])]
        # One-sided bootstrap p for "this wallet's POST60_ABS20 is negative". Reading the 90% CI
        # upper bound directly is a 5% one-sided test PER WALLET with no correction: across ~142
        # wallet-chains that is ~7 false rejections expected, which was most of the published list.
        res["p_boot"] = float((1 + np.sum(means >= 0)) / (boot + 1))

        if len(m) >= min_coins:
            d = np.clip([e["ret"] - float(np.median(e["control"])) for e in m], d_lo, d_hi)
            res["pump20"] = float(d.mean())
            idx = rng.integers(0, len(d), size=(boot, len(d)))
            res["pump20_ci90"] = [float(x) for x in np.percentile(d[idx].mean(axis=1), [5, 95])]
            # Activity-matched permutation, ONE null for every wallet: replace each episode's own
            # return with a draw WITH REPLACEMENT from that episode's own matched control set,
            # preserving this wallet's exact timing and market-cap exposure. Never switch schemes
            # per wallet — that is a different null distribution wearing the same name.
            draws = np.empty((perm, len(m)))
            for j, e in enumerate(m):
                ctrl = e["control"]
                draws[:, j] = ctrl[rng.integers(0, ctrl.size, size=perm)] - float(np.median(ctrl))
            null = np.clip(draws, d_lo, d_hi).mean(axis=1)
            res["p_perm"] = float((1 + np.sum(null <= d.mean())) / (perm + 1))
        out.append(res)
    return out


def holm(results, alpha=0.05):
    """Holm step-down within a chain over EVERY wallet tested — ONE test per wallet, ONE family.

    Each wallet contributes exactly one p-value. A matched wallet uses the permutation test, which
    controls the coin effect; an unmatched wallet falls back to the one-sided bootstrap. Taking the
    better of the two per wallet would be max-of-k dressed as a correction, so the choice is made
    by data availability, never by which p is smaller.

    Reading a 90% CI bound directly — the previous behaviour — is an uncorrected 5% one-sided test
    per wallet. Across ~142 wallet-chains that expects ~7 false rejections, and every rejection the
    screen published came through that path.
    """
    for r in results:
        if r.get("p_perm") is not None:
            r["p_value"], r["p_source"] = r["p_perm"], "permutation"
        elif r.get("p_boot") is not None:
            r["p_value"], r["p_source"] = r["p_boot"], "bootstrap"
        else:
            r["p_value"], r["p_source"] = None, None
        r["holm_sig"] = False

    tested = sorted([r for r in results if r["p_value"] is not None], key=lambda r: r["p_value"])
    n = len(tested)
    for r in results:
        r["holm_family_size"] = n
    for i, r in enumerate(tested):
        if r["p_value"] <= alpha / (n - i):
            r["holm_sig"] = True
        else:
            break                       # step-down: the first failure stops the cascade
    return results


def is_rejected(r):
    """POST60_STRATEGY_REJECTED. Rejects a post-60s hold policy for this wallet — never the
    wallet's authority, and never a fast-exit policy this screen cannot see.

    THREE conditions, all required:
      1. mark completeness >= MIN_COMPLETENESS. Marks are lost precisely when a coin dies, so a
         partial sample skews healthy and is biased toward NOT rejecting;
      2. a Holm-significant p-value within its chain's family — no unadjusted CI-bound path;
      3. a negative effect large enough to matter on whichever estimand was tested.
    Failing any of these is never evidence of quality: the wallet stays `watch`.
    """
    if r["completeness"] < MIN_COMPLETENESS or not r.get("holm_sig"):
        return False
    if r.get("p_source") == "permutation":
        return r["pump20"] is not None and r["pump20"] <= -3.0
    return r["abs20"] < 0 and r["abs20_ci90"][1] < 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="POST60 negative diagnostic (read-only)")
    ap.add_argument("--since", default="2026-07-11")
    ap.add_argument("--as-of", default=None,
                    help="frozen cutoff; signals AND marks are both bounded by it")
    ap.add_argument("--min-coins", type=int, default=8)
    ap.add_argument("--all-buys", action="store_true",
                    help="include add-on buys (a DIFFERENT statistic; default is first buys only)")
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--perm", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=20260811)
    args = ap.parse_args(argv)

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    rng = np.random.default_rng(args.seed)

    as_of = args.as_of or conn.execute(
        "SELECT MAX(signal_observed_at) FROM memecoin_signal_outcomes_v2").fetchone()[0]
    eps, n_selected = load_episodes(conn, args.since, as_of,
                                    first_buy_only=not args.all_buys)
    build_controls(eps)

    print(f"spec={SPEC} | since={args.since} as_of={as_of} min_coins={args.min_coins} "
          f"boot={args.boot} perm={args.perm} seed={args.seed} "
          f"first_buy_only={not args.all_buys}")
    print(f"episodes selected (stage 1, blind to marks): {sum(n_selected.values())} | "
          f"valid after mark validation: {len(eps)} | "
          f"matched to >={MIN_CONTROLS} controls: {sum(e['control'] is not None for e in eps)}")
    print("NOT the PUMP20 gate — 31-day negative diagnostic, no positive gate.")

    for chain in CHAINS:
        res = holm(screen(eps, chain, n_selected, args.min_coins, args.boot, args.perm, rng))
        if not res:
            print(f"\n=== {chain}: no wallet reaches {args.min_coins} coins ===")
            continue
        fam = res[0]["holm_family_size"]
        res.sort(key=lambda r: r["abs20"])
        print(f"\n=== {chain}: {len(res)} wallets >= {args.min_coins} coins | Holm family={fam} "
              f"| winsor R=[{res[0]['winsor_r'][0]:.1f},{res[0]['winsor_r'][1]:.1f}] ===")
        print(f"{'wallet':<20}{'coins':<7}{'compl':<7}{'match':<7}{'ABS20':<8}{'ci90':<17}"
              f"{'PUMP20':<9}{'p':<9}{'src':<7}{'holm':<6}verdict")
        for r in res[:12]:
            p20 = "-" if r["pump20"] is None else round(r["pump20"], 1)
            pp = "-" if r["p_perm"] is None else round(r["p_perm"], 4)
            ci = f"[{r['abs20_ci90'][0]:.1f},{r['abs20_ci90'][1]:.1f}]"
            pv = "-" if r["p_value"] is None else round(r["p_value"], 4)
            print(f"{str(r['handle'])[:19]:<20}{r['n_coins']:<7}"
                  f"{str(round(100*r['completeness']))+'%':<7}{r['n_matched']:<7}"
                  f"{round(r['abs20'],1):<8}{ci:<17}{str(p20):<9}{str(pv):<9}"
                  f"{str(r['p_source'] or '-')[:6]:<7}{'yes' if r['holm_sig'] else 'no':<6}"
                  f"{'POST60_STRATEGY_REJECTED' if is_rejected(r) else 'watch'}")
        rej = [r for r in res if is_rejected(r)]
        withheld = [r for r in res if r["completeness"] < MIN_COMPLETENESS
                    and r["abs20_ci90"][1] < 0]
        uncorrected = [r for r in res if r["abs20_ci90"][1] < 0
                       and r["completeness"] >= MIN_COMPLETENESS and not r["holm_sig"]]
        print(f"  POST60_STRATEGY_REJECTED: {len(rej)} | withheld for completeness < "
              f"{int(100*MIN_COMPLETENESS)}%: {len(withheld)} | would have been rejected by the "
              f"uncorrected CI-bound path: {len(uncorrected)}")
        for r in rej:
            print(f"    {str(r['handle'])[:22]:<24}coins={r['n_coins']:<4}"
                  f"compl={round(100*r['completeness'])}%  ABS20={round(r['abs20'],1):<7}"
                  f"ci90=[{r['abs20_ci90'][0]:.1f},{r['abs20_ci90'][1]:.1f}]")
    print("\nThis diagnostic authorises no roster action and cannot authorise funding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
