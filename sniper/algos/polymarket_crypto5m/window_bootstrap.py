"""Window-clustered bootstrap shared by crypto5m evaluators (lane-eval + brain-v2 z-validation).

Resamples WINDOWS with replacement (observations within a window are correlated) and returns the sorted
list of resampled means. Factored out of crypto5m_lane_eval.py so the z-validation harness reuses the
SAME stats path and seed rather than copying it."""
from __future__ import annotations

import random

BOOT_N = 2000           # bootstrap resamples
BOOT_SEED = 20260621    # fixed seed -> deterministic output


def boot_means(by_window: dict, rng: random.Random, nb: int = BOOT_N) -> list[float]:
    """by_window: {window_key: [values]}. Resample windows with replacement, pool their values, take the
    mean; repeat nb times; return sorted means. Empty input -> []."""
    ws = list(by_window)
    if not ws:
        return []
    out = []
    for _ in range(nb):
        vals = []
        for _ in ws:
            vals.extend(by_window[ws[rng.randrange(len(ws))]])
        out.append(sum(vals) / len(vals))
    out.sort()
    return out


def pctile(sorted_vals, q):
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, max(0, int(q * (len(sorted_vals) - 1))))]
