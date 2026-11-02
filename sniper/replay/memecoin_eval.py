"""Honest paired-arm evaluation primitives. Unit = token episode; statistic = recency-weighted
paired per-episode PnL difference d_i. ESS-sized (a fixed half-life makes thin chains ungraduatable),
leave-two-out robust (not a >20% concentration cap that penalizes tails), win-rate reported not gated."""
from __future__ import annotations


def recency_weight(age_hours: float, half_life_hours: float) -> float:
    return 2.0 ** (-float(age_hours) / float(half_life_hours))


def effective_sample_size(weights: list[float]) -> float:
    s = sum(weights)
    s2 = sum(w * w for w in weights)
    return (s * s / s2) if s2 > 0 else 0.0


def _weighted(pairs, half_life_hours):
    ws = [recency_weight(p["age_hours"], half_life_hours) for p in pairs]
    return ws, [p["d"] for p in pairs]


def paired_delta(pairs, half_life_hours):
    ws, ds = _weighted(pairs, half_life_hours)
    sw = sum(ws)
    return (sum(w * d for w, d in zip(ws, ds)) / sw) if sw > 0 else 0.0


def sign_flip_p(pairs, half_life_hours, n=2000, rng_seed_terms=None):
    """One-sided sign-flip permutation p-value for weighted mean paired delta > 0. Deterministic:
    flip signs by a fixed rotating pattern (no Math.random — reproducible in replay)."""
    obs = paired_delta(pairs, half_life_hours)
    if obs <= 0:
        return 1.0
    ws, ds = _weighted(pairs, half_life_hours)
    sw = sum(ws) or 1.0
    ge = 0
    for k in range(1, n + 1):
        signs = [1 if ((i * 2654435761 + k) >> 3) & 1 else -1 for i in range(len(ds))]
        stat = sum(w * s * d for w, s, d in zip(ws, signs, ds)) / sw
        if stat >= obs:
            ge += 1
    return (ge + 1) / (n + 1)


def leave_two_out_positive(pairs, half_life_hours):
    if len(pairs) <= 2:
        return False
    order = sorted(range(len(pairs)), key=lambda i: pairs[i]["d"], reverse=True)
    drop = set(order[:2])
    kept = [p for i, p in enumerate(pairs) if i not in drop]
    return paired_delta(kept, half_life_hours) > 0


def graduation_ok(pairs, *, half_life_hours, ess_floor=30):
    ws, _ = _weighted(pairs, half_life_hours)
    ess = effective_sample_size(ws)
    delta = paired_delta(pairs, half_life_hours)
    if pairs:
        recent = max(p["block_id"] for p in pairs)
        recent_pairs = [p for p in pairs if p["block_id"] == recent]
        recent_block_ok = sum(p["d"] for p in recent_pairs) >= 0
    else:
        recent_block_ok = False
    lto = leave_two_out_positive(pairs, half_life_hours)
    res = {"ess": ess, "ess_ok": ess >= ess_floor, "delta": delta,
           "recent_block_ok": recent_block_ok, "leave_two_out_ok": lto,
           "win_rate": (sum(1 for p in pairs if p["d"] > 0) / len(pairs)) if pairs else 0.0}
    res["pass"] = res["ess_ok"] and delta > 0 and recent_block_ok and lto
    return res
