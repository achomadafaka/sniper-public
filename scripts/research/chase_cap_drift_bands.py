#!/usr/bin/env python3
"""What the funded book says about MEMECOIN_REAL_CHASE_MAX_PCT. It does not say much.

The Solana exact-quote chase gate never refused an entry before 2026-08-19: the pre-buy token read
reported no decimals on a first buy, so the quote price was None and the gate skipped itself. That
failure is what makes this sample usable. Entry drift was never selected on, so the closed funded
book is an unselected observation of what each cap would have kept.

    python3 scripts/research/chase_cap_drift_bands.py --by-network      # the honest default view
    python3 scripts/research/chase_cap_drift_bands.py --network solana

ALWAYS SPLIT BY CHAIN. `MEMECOIN_REAL_CHASE_MAX_PCT` feeds two different gates: the Solana
exact-quote check in solana_swap_executor, and the shared display-price check at
memecoin_real_runner.py:1133, which is the only chase gate EVM ever gets. Pooling them tests
neither. A pooled run on 2026-08-19 read +3.72 / p=0.0072 and was used to argue a Solana cap of
10. Split by chain that is a 4663 result on top of a null Solana one, and NEITHER chain survives
its own scan:

    solana  n=190/167 mints  best cap 20  p=0.0075  vs bar 0.00625  -> no survivor
    4663    n= 58/ 56 mints  best cap 10  p=0.0137  vs bar 0.00625  -> no survivor
    pooled  n=248/223 mints  best cap 20  p=0.0029  -> 6 caps survive, testing no single gate

The pooled sample DOES survive Holm. That is not a defence of it: it pools two gates, and on
Solana alone the best cap is 20, not the 10 the pooled table suggested. `--pooled` is kept so the
refuted claim stays reproducible.

Three limits that apply to every number printed here:

1. MULTIPLICITY. This scans 8 caps, so it runs the full ordered Holm step-down over all 8
   p-values. Comparing only the smallest p to alpha/8 is Bonferroni, not Holm, and it answers a
   different question than "which caps survive this search".
2. UNIT MISMATCH. The cap table splits POSITIONS at the threshold. The permutation splits MINTS
   by their median drift, because positions on one mint are correlated and a per-position shuffle
   would treat correlated rows as independent. Those are different partitions - on Solana 7 mints
   have positions on both sides of 10%, on 4663 none do. `--report-crossing` names them.
3. `source_drift_pct` is QUOTE-to-source (entry_attribution.py:82), which is exactly what the
   Solana gate compares. `total_copy_drift_pct` is the fill-to-source figure; it is not used here.

Read-only. Opens the tracker with mode=ro and writes nothing.
"""
import argparse
import random
import sqlite3
import statistics as st
from collections import defaultdict


CAPS = (5, 8, 10, 12, 15, 20, 25, 30)
BANDS = ((-1e9, 0, "<=0%"), (0, 5, "0-5%"), (5, 10, "5-10%"), (10, 15, "10-15%"),
         (15, 20, "15-20%"), (20, 30, "20-30%"), (30, 1e9, ">30%"))

# Closed, funded, with a realized PnL and a measured drift against the copied trader's price.
# `source_drift_pct` = quote/source - 1 (entry_attribution.py:82), the same comparison the gate
# makes. Do not confuse it with `total_copy_drift_pct` = fill/source - 1.
#
# `origin_event_id IS NOT NULL` drops operator-opened positions. A manual entry has no copied feed
# event, so no chase decision was ever available for it and the gate skips it by design
# (test_a_manual_operator_entry_is_never_chase_gated). Leaving it in would score the cap on a
# position the cap can never act on. One row qualifies as of 2026-08-19: id 39545, handle
# '(real)', drift -12.68%, PnL -$10.17.
ROWS_SQL = """
SELECT id, mint, source_handle, network_id, source_drift_pct, realized_pnl, opened_at
FROM memecoin_positions
WHERE execution_mode = 'real'
  AND is_open = 0
  AND realized_pnl IS NOT NULL
  AND source_drift_pct IS NOT NULL
  AND origin_event_id IS NOT NULL
ORDER BY opened_at
"""


def load(db, since=None):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(ROWS_SQL)]
    conn.close()
    if since:
        rows = [r for r in rows if (r["opened_at"] or "") >= since]
    return rows


def _line(label, rows):
    pnl = [r["realized_pnl"] for r in rows]
    mints = len({r["mint"] for r in rows})
    wins = 100.0 * sum(1 for x in pnl if x > 0) / len(pnl)
    return (f"{label:>10} {len(rows):>5} {mints:>6} {sum(pnl):>10.2f} "
            f"{st.median(pnl):>9.2f} {wins:>6.0f}%")


def bands(rows):
    print(f"\n{'band':>10} {'n':>5} {'mints':>6} {'sumPnL':>10} {'medPnL':>9} {'win%':>7}")
    for lo, hi, label in BANDS:
        band = [r for r in rows if lo < r["source_drift_pct"] <= hi]
        if band:
            print(_line(label, band))
    print(_line("ALL", rows))
    print(f"\nmedian source drift: {st.median([r['source_drift_pct'] for r in rows]):.2f}%")


def cap_table(rows):
    print(f"\n{'cap':>6} {'kept':>6} {'mints':>6} {'keptPnL':>10} {'medPnL':>9} {'win%':>7} "
          f"| {'blockedPnL':>11} {'nblocked':>9}")
    for cap in CAPS:
        keep = [r for r in rows if r["source_drift_pct"] <= cap]
        block = [r for r in rows if r["source_drift_pct"] > cap]
        if not keep:
            continue
        pnl = [r["realized_pnl"] for r in keep]
        print(f"{cap:>6} {len(keep):>6} {len({r['mint'] for r in keep}):>6} {sum(pnl):>10.2f} "
              f"{st.median(pnl):>9.2f} "
              f"{100.0 * sum(1 for x in pnl if x > 0) / len(pnl):>6.0f}% "
              f"| {sum(r['realized_pnl'] for r in block):>11.2f} {len(block):>9}")


def permutation(rows, threshold, shuffles, seed, quiet=False):
    """Median PnL of low-drift mints minus high-drift mints, against a mint-label shuffle.

    A mint is LOW when its MEDIAN drift is at or under the threshold, so every position on one
    mint carries the same label and the shuffle moves whole mints.
    """
    by_mint = defaultdict(list)
    for r in rows:
        by_mint[r["mint"]].append(r)
    mints = sorted(by_mint)

    def gap(is_low):
        lo = [r["realized_pnl"] for mint in mints if is_low[mint] for r in by_mint[mint]]
        hi = [r["realized_pnl"] for mint in mints if not is_low[mint] for r in by_mint[mint]]
        if not lo or not hi:
            return None
        return st.median(lo) - st.median(hi)

    truth = {mint: st.median([r["source_drift_pct"] for r in by_mint[mint]]) <= threshold
             for mint in mints}
    observed = gap(truth)
    if observed is None:
        return 0.0, 1.0      # a threshold with an empty side cannot reject; Holm still needs a p

    labels = [truth[mint] for mint in mints]
    rng = random.Random(seed)
    at_least = 0
    for _ in range(shuffles):
        rng.shuffle(labels)
        drawn = gap(dict(zip(mints, labels)))
        if drawn is not None and drawn >= observed:
            at_least += 1
    p = (at_least + 1) / (shuffles + 1)      # add-one: a permutation p is never exactly 0
    if not quiet:
        n_low = sum(truth.values())
        print(f"\nmint-clustered permutation, threshold {threshold}%")
        print(f"  median(low) - median(high) = {observed:+.2f}")
        print(f"  p = {p:.4f}  ({at_least} of {shuffles} shuffles at or above, add-one corrected)")
        print(f"  mints: {len(mints)} ({n_low} low, {len(mints) - n_low} high), "
              f"positions: {len(rows)}")
    return observed, p


def ex_outliers(rows, k=2):
    """Drop the k best and k worst mints by total PnL. The knee must survive them."""
    by_mint = defaultdict(list)
    for r in rows:
        by_mint[r["mint"]].append(r)
    total = {mint: sum(r["realized_pnl"] for r in g) for mint, g in by_mint.items()}
    order = sorted(total, key=total.get)
    drop = set(order[:k]) | set(order[-k:])
    kept = [r for r in rows if r["mint"] not in drop]
    print(f"\nex-best-{k} / ex-worst-{k} mints ({len(kept)} of {len(rows)} positions, "
          f"dropped {sorted(drop)[:1]}...)")
    cap_table(kept)
    return kept


def crossing_mints(rows, threshold):
    """Mints with positions on BOTH sides of the threshold.

    The cap table splits positions; the permutation splits mints. Each crossing mint is a place
    where those two partitions disagree, so the count bounds how far the two can be reconciled.
    """
    by_mint = defaultdict(list)
    for r in rows:
        by_mint[r["mint"]].append(r)
    return sorted(mint for mint, g in by_mint.items()
                  if any(r["source_drift_pct"] <= threshold for r in g)
                  and any(r["source_drift_pct"] > threshold for r in g))


def holm(results, alpha):
    """The ordered Holm-Bonferroni step-down over the WHOLE scanned cap set.

    Not alpha/k applied to one p-value: that is Bonferroni on the smallest, and it silently
    answers a different question than "which caps in this scan survive the search". Holm sorts the
    k p-values ascending and tests p(i) against alpha/(k-i). The moment one retains, every larger
    p retains too, so the procedure must see EVERY cap's p-value, not just the winner's.

    Returns [(cap, p, bar, rejected)] in ascending p order.
    """
    ordered = sorted(results.items(), key=lambda kv: kv[1][1])
    k = len(ordered)
    out, still_rejecting = [], True
    for i, (cap, (_effect, p)) in enumerate(ordered):
        bar = alpha / (k - i)
        if still_rejecting and p <= bar:
            out.append((cap, p, bar, True))
        else:
            still_rejecting = False
            out.append((cap, p, bar, False))
    return out


def scan(rows, args, quiet=True):
    """One permutation per cap in CAPS. Holm needs all of them, not just the best."""
    results = {}
    for cap in CAPS:
        results[cap] = permutation(rows, float(cap), args.shuffles, args.seed, quiet=quiet)
    return results


def analyse(rows, label, args):
    print(f"\n{'=' * 78}\n{label}: {len(rows)} positions, "
          f"{len({r['mint'] for r in rows})} mints, "
          f"{rows[0]['opened_at'][:10]} -> {rows[-1]['opened_at'][:10]}\n{'=' * 78}")
    bands(rows)
    cap_table(rows)
    kept = [r["source_drift_pct"] for r in rows if r["source_drift_pct"] <= args.threshold]
    if kept:
        print(f"\nmedian drift of what a {args.threshold:g}% cap KEEPS: {st.median(kept):.2f}% "
              f"(not the full-sample median printed above)")

    results = scan(rows, args)
    steps = holm(results, args.alpha)
    print(f"\nHolm step-down over all {len(CAPS)} scanned caps, alpha={args.alpha}, "
          f"{args.shuffles} shuffles, seed {args.seed}")
    print(f"  {'rank':>4} {'cap':>5} {'effect':>8} {'p':>8} {'bar':>8}  verdict")
    for rank, (cap, p, bar, rejected) in enumerate(steps, 1):
        print(f"  {rank:>4} {cap:>5} {results[cap][0]:>+8.2f} {p:>8.4f} {bar:>8.5f}  "
              f"{'reject null' if rejected else 'retain null'}")
    survivors = [cap for cap, _p, _bar, rejected in steps if rejected]
    print(f"  caps surviving the scan: {survivors or 'NONE'}")

    cross = crossing_mints(rows, args.threshold)
    print(f"  mints straddling {args.threshold:g}% (cap table vs permutation disagree): {len(cross)}")
    if args.report_crossing and cross:
        for mint in cross:
            print(f"    {mint}")
    ex_outliers(rows)
    best_cap = steps[0][0]
    return {"label": label, "n": len(rows), "best_cap": best_cap,
            "effect": results[best_cap][0], "p": steps[0][1], "bar": steps[0][2],
            "survivors": survivors}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--since", default=None, help="only positions opened at/after this ISO date")
    ap.add_argument("--network", default=None,
                    help="restrict to one chain, e.g. solana or 4663")
    ap.add_argument("--by-network", action="store_true",
                    help="one independent analysis per chain (the default when neither is given)")
    ap.add_argument("--pooled", action="store_true",
                    help="pool every chain. Kept only so the refuted 2026-08-19 claim stays "
                         "reproducible; this pools two different gates and tests neither")
    ap.add_argument("--threshold", type=float, default=10.0)
    ap.add_argument("--alpha", type=float, default=0.05)
    # 5000 leaves the 4th decimal of p to Monte-Carlo noise (0.0060-0.0092 across five seeds).
    # 20000 settles it to 0.0063-0.0072. Quote p to two significant figures, not four.
    ap.add_argument("--shuffles", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--report-crossing", action="store_true",
                    help="name the mints that straddle the threshold")
    args = ap.parse_args()

    rows = load(args.db, args.since)
    if not rows:
        raise SystemExit("no closed funded positions with a measured source drift")

    results = []
    if args.pooled:
        results.append(analyse(rows, "POOLED (all chains - tests no single gate)", args))
    elif args.network:
        subset = [r for r in rows if str(r["network_id"]) == args.network]
        if not subset:
            raise SystemExit(f"no rows for network {args.network}")
        results.append(analyse(subset, f"network {args.network}", args))
    else:
        for network in sorted({str(r["network_id"]) for r in rows}):
            subset = [r for r in rows if str(r["network_id"]) == network]
            if len(subset) < 20:
                print(f"\nnetwork {network}: {len(subset)} positions - too few to analyse, skipped")
                continue
            results.append(analyse(subset, f"network {network}", args))

    print(f"\n{'=' * 78}\nSUMMARY - Holm step-down over {len(CAPS)} caps at alpha={args.alpha}")
    print(f"{'sample':<44} {'n':>5} {'cap':>4} {'effect':>8} {'p':>8} {'bar':>8}  survivors")
    for r in results:
        print(f"{r['label']:<44} {r['n']:>5} {r['best_cap']:>4} {r['effect']:>+8.2f} "
              f"{r['p']:>8.4f} {r['bar']:>8.5f}  {r['survivors'] or 'NONE'}")
    print("\nA cap change needs the chain it governs to clear on its own. A pooled result does "
          "not\ntransfer: the Solana exact-quote gate and the shared display gate are different "
          "gates.")


if __name__ == "__main__":
    main()
