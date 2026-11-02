"""Does a wallet's PAST FUNDED record predict its NEXT funded close better than its PAPER record?

The arming gate scores wallets on honest alpha_b PAPER closes. We now hold 282 funded closes, and
several wallets disagree sharply between the two ledgers (DegenCapitalLLC: 13 funded closes at 77%
win, blocked by a paper gate holding 3). This asks whether that disagreement means the gate reads
the wrong ledger, or merely that funded samples are small.

WALK-FORWARD, causal by construction. For every funded close, both predictors are recomputed from
ONLY the rows that had already CLOSED when that position OPENED. No wallet is ever scored with
evidence from its own future, and no full-period aggregate is used to "predict" a row inside it --
that circularity is what makes retrospective wallet rankings look predictive when they are not.

The paper predictor reuses `measurable_outcome_sql()` and the fomo exclusion verbatim, so it is the
same number the gate reads, just truncated in time.

Read-only.
"""
import argparse
import os
import sqlite3
import statistics as st
import sys
from random import Random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sniper.persistence.db_memecoin import measurable_outcome_sql  # noqa: E402


def _rank(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0] * len(values)
    for pos, i in enumerate(order):
        ranks[i] = pos + 1
    return ranks


def spearman(xs, ys):
    if len(xs) < 3:
        return None
    rx, ry = _rank(xs), _rank(ys)
    n = len(xs)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den = (sum((rx[i] - mx) ** 2 for i in range(n))
           * sum((ry[i] - my) ** 2 for i in range(n))) ** 0.5
    return num / den if den else None


def perm_p(xs, ys, iters=20000, seed=17):
    obs = spearman(xs, ys)
    if obs is None:
        return None, None
    rng = Random(seed)
    hits = 0
    pool = list(ys)
    for _ in range(iters):
        rng.shuffle(pool)
        s = spearman(xs, pool)
        if s is not None and abs(s) >= abs(obs):
            hits += 1
    return obs, hits / iters


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--min-prior", type=int, default=3,
                    help="minimum prior closes on a ledger before it may be scored")
    a = ap.parse_args()
    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    funded = c.execute(
        "SELECT id, source_user_id, source_handle, ticker, opened_at, closed_at, realized_pnl, "
        "entry_notional FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 "
        "AND realized_pnl IS NOT NULL AND entry_notional>0 AND source_user_id IS NOT NULL "
        "AND opened_at IS NOT NULL ORDER BY opened_at"
    ).fetchall()

    paper_sql = (
        "SELECT COUNT(*) n, COALESCE(SUM(realized_pnl),0) pnl FROM memecoin_positions "
        "WHERE source_user_id=? AND strategy='alpha_b' "
        "AND COALESCE(execution_mode,'paper')!='real' AND closed_at < ? AND "
        + measurable_outcome_sql()
        + " AND memecoin_positions.id NOT IN "
          "(SELECT position_id FROM memecoin_fills WHERE fill_source='fomo')")
    funded_sql = (
        "SELECT COUNT(*) n, COALESCE(SUM(realized_pnl),0) pnl FROM memecoin_positions "
        "WHERE source_user_id=? AND execution_mode='real' AND is_open=0 "
        "AND realized_pnl IS NOT NULL AND closed_at < ?")

    obs = []
    for r in funded:
        cut = r["opened_at"]
        p = c.execute(paper_sql, (r["source_user_id"], cut)).fetchone()
        f = c.execute(funded_sql, (r["source_user_id"], cut)).fetchone()
        obs.append({
            "handle": r["source_handle"], "ticker": r["ticker"],
            "ret": r["realized_pnl"] / r["entry_notional"] * 100.0,
            "pnl": r["realized_pnl"],
            "paper_n": int(p["n"] or 0), "paper_pnl": float(p["pnl"] or 0.0),
            "funded_n": int(f["n"] or 0), "funded_pnl": float(f["pnl"] or 0.0),
        })

    print(f"funded closes with a causal history: {len(obs)}\n")
    for label, nkey, pkey in (("PAPER  (the gate's ledger)", "paper_n", "paper_pnl"),
                              ("FUNDED (the real ledger) ", "funded_n", "funded_pnl")):
        elig = [o for o in obs if o[nkey] >= a.min_prior]
        if len(elig) < 5:
            print(f"{label}: only {len(elig)} rows had >={a.min_prior} prior closes — not testable")
            continue
        xs = [o[pkey] for o in elig]
        ys = [o["ret"] for o in elig]
        rho, p = perm_p(xs, ys)
        pos = [o["ret"] for o in elig if o[pkey] > 0]
        neg = [o["ret"] for o in elig if o[pkey] <= 0]
        print(f"{label}  n={len(elig)}")
        print(f"   spearman(prior {pkey}, next close return) = {rho:+.3f}   permutation p = {p:.4f}")
        if pos and neg:
            print(f"   prior POSITIVE ledger -> next close median {st.median(pos):+7.2f}%  "
                  f"win {100*sum(1 for v in pos if v>0)/len(pos):>3.0f}%  n={len(pos)}")
            print(f"   prior NEGATIVE ledger -> next close median {st.median(neg):+7.2f}%  "
                  f"win {100*sum(1 for v in neg if v>0)/len(neg):>3.0f}%  n={len(neg)}")
        print()

    both = [o for o in obs if o["paper_n"] >= a.min_prior and o["funded_n"] >= a.min_prior]
    print(f"HEAD TO HEAD on the {len(both)} closes where BOTH ledgers were scoreable")
    if len(both) >= 5:
        for key in ("paper_pnl", "funded_pnl"):
            rho, p = perm_p([o[key] for o in both], [o["ret"] for o in both])
            print(f"   {key:<11} spearman {rho:+.3f}  p={p:.4f}")
        agree = [o for o in both if (o["paper_pnl"] > 0) == (o["funded_pnl"] > 0)]
        disagree = [o for o in both if (o["paper_pnl"] > 0) != (o["funded_pnl"] > 0)]
        print(f"   ledgers AGREE on {len(agree)} closes, DISAGREE on {len(disagree)}")
        for name, g in (("funded says GOOD, paper says bad",
                         [o for o in disagree if o["funded_pnl"] > 0]),
                        ("paper says GOOD, funded says bad",
                         [o for o in disagree if o["paper_pnl"] > 0])):
            if g:
                print(f"     {name}: n={len(g)} median {st.median(o['ret'] for o in g):+7.2f}%  "
                      f"win {100*sum(1 for o in g if o['ret']>0)/len(g):>3.0f}%  "
                      f"sum ${sum(o['pnl'] for o in g):+.2f}")


if __name__ == "__main__":
    main()
