"""Robust wallet selection for the copy funnel — pure functions.

The lesson from the fidelity study: trading stats (profit_factor/win_rate) do NOT predict forward
follow PnL — win_rate is inversely correlated (ADR-006 repeating on HL). The only key with any
signal is a wallet's OWN forward-follow record. So selection ranks by that, robustly:
  * central tendency = MEDIAN (fat tails; one outlier must not promote a wallet)
  * n-gated         = enough forward closes before any verdict
  * sign-stable     = the record holds across the first vs second half of its history
Promotion is asymmetric: trusting a winner needs more evidence than killing a loser."""
from __future__ import annotations

import random
import statistics as st


def bootstrap_p_positive(pnls, *, iters: int = 20000, seed: int = 17) -> float | None:
    """One-sided bootstrap p-value that the wallet's mean follow-PnL > 0 (fraction of resample
    means <= 0). None if too few. Used to correct the per-wallet 'looks positive' claims."""
    xs = _clean(pnls)
    n = len(xs)
    if n < 3:
        return None
    rnd = random.Random(seed)
    le0 = 0
    for _ in range(iters):
        s = sum(xs[rnd.randrange(n)] for _ in range(n))
        if s <= 0:
            le0 += 1
    return le0 / iters


def fdr_bh(pvals, *, q: float = 0.10) -> list[bool]:
    """Benjamini-Hochberg: which p-values pass at false-discovery-rate q. Corrects for testing
    many wallets at once — picking the best of N looks inflates significance without this."""
    idx = sorted(range(len(pvals)), key=lambda i: (pvals[i] if pvals[i] is not None else 1.0))
    m = len(pvals)
    crit = -1
    for rank_i, i in enumerate(idx, 1):
        p = pvals[i] if pvals[i] is not None else 1.0
        if p <= (rank_i / m) * q:
            crit = rank_i
    passed = [False] * m
    for rank_i, i in enumerate(idx, 1):
        if crit >= 1 and rank_i <= crit:
            passed[i] = True
    return passed


def _clean(pnls) -> list[float]:
    return [float(p) for p in pnls if p is not None]


def split_half_sign_stable(pnls_time_ordered, *, min_n: int = 6):
    """Does the wallet's mean-sign hold across the first vs second half of its (time-ordered)
    follows? None if too few to split. A front-loaded winner (good half then bleed) flunks this."""
    xs = _clean(pnls_time_ordered)
    if len(xs) < min_n:
        return None
    h = len(xs) // 2
    a, b = xs[:h], xs[h:]
    if not a or not b:
        return None
    return (st.mean(a) > 0) == (st.mean(b) > 0)


def wallet_score(pnls_time_ordered, *, min_n: int = 8) -> dict:
    """Robust forward-follow score for one wallet. pnls must be time-ordered for sign-stability."""
    xs = _clean(pnls_time_ordered)
    n = len(xs)
    if n == 0:
        return {"n": 0, "median": None, "mean": None, "win_rate": None,
                "sign_stable": None, "eligible": False}
    return {
        "n": n,
        "median": round(st.median(xs), 2),
        "mean": round(st.mean(xs), 2),
        "win_rate": round(sum(1 for x in xs if x > 0) / n, 3),
        "sign_stable": split_half_sign_stable(xs),
        "eligible": n >= min_n,
    }


def classify(score: dict, *, promote_min_n: int = 8, cull_min_n: int = 5,
             promote_median: float = 10.0, cull_median: float = -25.0) -> str:
    """'promote' | 'cull' | 'hold' from a wallet_score. Asymmetric: culling a loser needs fewer
    closes (cull_min_n) than promoting a winner (promote_min_n + sign-stability)."""
    n = score.get("n", 0)
    med = score.get("median")
    if med is None:
        return "hold"
    if n >= cull_min_n and med <= cull_median:
        return "cull"
    if n >= promote_min_n and med >= promote_median and score.get("sign_stable"):
        return "promote"
    return "hold"


def rank(wallet_pnls: dict, *, min_n: int = 5, q: float = 0.10, **classify_kw) -> list[dict]:
    """wallet_pnls: {wallet: [net_bps time-ordered]}. -> ranked-by-median list of
    {wallet, action, p_positive, fdr_significant, ...score}. Only wallets with >= min_n closes.
    fdr_significant applies a Benjamini-Hochberg correction so 'promote' leads that only look
    good because we examined many wallets are flagged honestly."""
    out = []
    for wallet, pnls in wallet_pnls.items():
        s = wallet_score(pnls)
        if s["n"] < min_n:
            continue
        s["wallet"] = wallet
        s["action"] = classify(s, **classify_kw)
        s["p_positive"] = bootstrap_p_positive(pnls)
        out.append(s)
    flags = fdr_bh([w["p_positive"] for w in out], q=q)
    for w, f in zip(out, flags):
        w["fdr_significant"] = f
    return sorted(out, key=lambda d: (d["median"] if d["median"] is not None else -1e9), reverse=True)
