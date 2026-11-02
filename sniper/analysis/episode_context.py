"""WO-3 — episodic memory: what the coin remembers about ITS OWN history.

`compute_state` sees only the last <=5000 ticks — a coin's earlier shakeouts, LP behaviour, and
pre-position pivots are invisible to the exit brain. This pure function derives that longer memory
from the coin's full recorded tape.

DISPLAY / SHADOW ONLY. It is threaded into the decision LOG as context fields; NO rule may consume it
without a new dated prereg. This WO produces the evidence that would justify one — specifically the
number that decides whether a "shakeout-count" term ever enters v4+: does a coin's 2nd+ shakeout
resolve differently from its first? (see scripts/research/shakeout_resolution_report.py).

Pure — no DB, no network. Reuses the exit brain's own primitives (exec_price_path_per_block,
confirmed_pivots) so this "memory" can never drift from what the live brain computes on a shorter
window. A "shakeout" here matches the brain's structure-break vocabulary: price dips below a prior
CONFIRMED swing low, then recovers back above it (the break did not hold — a flow-rejected fakeout).
"""
from __future__ import annotations

from sniper.analysis.exit_features import confirmed_pivots, exec_price_path_per_block


def _max_drawdown(series):
    """Deepest peak-to-trough fractional drop over the price series, [0,1]. None if <2 points."""
    if len(series) < 2:
        return None
    peak = series[0][1]
    worst = 0.0
    for _, px in series:
        if px is None or px <= 0:
            continue
        if px > peak:
            peak = px
        elif peak > 0:
            worst = max(worst, (peak - px) / peak)
    return worst


def _count_shakeouts(series, lows):
    """Per prior confirmed swing low L: a shakeout is a later bar dipping below L then a still-later
    bar recovering above L (the break did not hold). Counts distinct shakeout lows, causally."""
    count = 0
    for tL, pL in lows:
        after = [px for (t, px) in series if t > tL and px is not None and px > 0]
        broke_at = next((i for i, px in enumerate(after) if px < pL), None)
        if broke_at is None:
            continue
        if any(px > pL for px in after[broke_at:]):    # recovered above the broken low
            count += 1
    return count


def episode_context(swaps, *, quote_side, confirm=3, as_of_ts=None):
    """Longer-horizon structural memory for one coin from its full recorded swap tape.

    Returns a JSON-safe dict of CONTEXT fields (never authority):
      prior_shakeouts     — confirmed swing lows that were broken then recovered (flow-rejected breaks)
      confirmed_swing_lows— total causal swing lows in the tape
      prior_peak_drawdown — deepest peak-to-trough drop over the tape, [0,1]
      structure_age_s     — event-time span of the recorded structure
      lp_adds/removes     — explicit liquidity events; None if the tape carries none
      n_price_bars        — per-block price bars the memory was built from
    """
    if as_of_ts is not None:
        swaps = [s for s in swaps if s.get("block_ts") is not None
                 and float(s["block_ts"]) <= float(as_of_ts)]
    if quote_side is None:
        return {"prior_shakeouts": None, "confirmed_swing_lows": None, "prior_peak_drawdown": None,
                "structure_age_s": None, "lp_adds": None, "lp_removes": None,
                "n_price_bars": 0}
    path = exec_price_path_per_block(swaps, quote_side=int(quote_side))
    series = [(ts, px) for (ts, px, _n, _k) in path]
    pivots = confirmed_pivots(series, confirm=confirm)
    lows = sorted((p.block_ts, p.price) for p in pivots if p.kind == "low")
    burn_kinds = [s for s in swaps if (str(s.get("kind", "")).endswith("burn")
                                       or str(s.get("kind", "")).endswith("remove_liquidity"))]
    mint_kinds = [s for s in swaps if (str(s.get("kind", "")).endswith("mint")
                                       or str(s.get("kind", "")).endswith("add_liquidity"))]
    has_lp_events = bool(burn_kinds or mint_kinds)
    return {
        "prior_shakeouts": _count_shakeouts(series, lows),
        "confirmed_swing_lows": len(lows),
        "prior_peak_drawdown": _max_drawdown(series),
        "structure_age_s": (series[-1][0] - series[0][0]) if len(series) >= 2 else None,
        "lp_adds": len(mint_kinds) if has_lp_events else None,
        "lp_removes": len(burn_kinds) if has_lp_events else None,
        "n_price_bars": len(series),
    }
