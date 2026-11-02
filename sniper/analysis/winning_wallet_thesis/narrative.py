"""Display-only synthesis of a stored thesis profile into a plain-English verdict + the 2-3
patterns that carry the PnL. Re-presents EXISTING fields (headline, edge_clusters, loss_centers,
capital_risk, shuffle_baseline, data_gaps) — it computes NO new judgment, does not touch any stored
analysis, and must never emit copy-advice / expected-PnL / sizing language. The analyzer is a
strategy-intelligence surface, not a copy bot."""
from __future__ import annotations

VERDICT = {
    "profitable_provable": "Profitable historically and clearly above chance — worth a forward test.",
    "profitable_unproven": "Profitable historically, but no single repeatable rule is provable yet.",
    "mixed": "Roughly break-even — no clear edge stands out.",
    "net_loser": "Net loser over the cached history.",
}


def _roi(rep: dict) -> float:
    cr = rep.get("capital_risk") or {}
    for k in ("roi_on_peak_deployed_pct", "total_realized_pnl_with_inventory", "total_realized_pnl"):
        if cr.get(k) is not None:
            return float(cr[k])
    return 0.0


def _state(rep: dict) -> str:
    roi = _roi(rep)
    over = (rep.get("shuffle_baseline") or {}).get("obs_over_expected")
    if roi <= 0:
        return "net_loser" if roi < 0 else "mixed"
    return "profitable_provable" if (over is not None and over >= 2) else "profitable_unproven"


# Generic axis buckets that describe the whole book or an exit/side axis rather than a market —
# low information for "where does the money come from". Prefer specific market/league clusters.
_GENERIC_LABELS = {"held", "sold", "open", "all", "sports", "games", "yes", "no", "buy", "sell",
                   "won", "lost", "win", "loss", "other", "other-sport", "unknown", "directional"}


def _is_specific(label) -> bool:
    s = (label or "").strip().lower()
    return bool(s) and s not in _GENERIC_LABELS and len(s) > 1


def _keys(c) -> set:
    return {tuple(k) if isinstance(k, (list, tuple)) else k for k in (c.get("member_position_keys") or [])}


def _dominated(c, kept: list) -> bool:
    """True if c covers (near-)the same positions as an already-kept higher-PnL cluster — a sliced
    re-cut of the same book on a different axis (the analyzer-clustering audit found 98% of clusters
    are overlap-dominated). Uses member_position_keys when present (catches near-dupes like a
    'sports'/'Sports' pair $99 apart that exact-PnL dedup misses); falls back to exact rounded PnL."""
    ks = _keys(c)
    if ks:
        for k in kept:
            kk = _keys(k)
            if kk and len(ks & kk) >= 0.9 * len(ks):
                return True
        return False
    pnl = round(float(c.get("pnl") or 0), 2)
    return any(round(float(k.get("pnl") or 0), 2) == pnl for k in kept)


def _patterns(rep: dict):
    ec = [c for c in (rep.get("edge_clusters") or []) if not c.get("is_loss_center")]
    # Sort SPECIFIC-first (then by |PnL|) so a specific market pattern (NBA, a subset of "held") is
    # KEPT during overlap-dedup and a generic whole-book bucket (held/sports) can't absorb the
    # specific subset it contains — otherwise only generics survive and the verdict says nothing.
    ec = sorted(ec, key=lambda c: (0 if _is_specific(c.get("label")) else 1,
                                   -abs(float(c.get("pnl") or 0))))
    uniq = []
    for c in ec:
        if not _dominated(c, uniq):     # drop same-positions re-cuts (by overlap, else exact PnL)
            uniq.append(c)
    top = [{"label": c.get("label") or "?", "pnl": c.get("pnl"),
            "n_positions": c.get("n_positions"), "edge_pp": c.get("edge_pp"),
            "specific": _is_specific(c.get("label")),
            "plain": f"{c.get('label', '?')} — {int(c.get('n_positions') or 0)} bets"}
           for c in uniq[:3]]
    return top, max(0, len(uniq) - 3)


def _top_loss(rep: dict):
    lc = [c for c in (rep.get("loss_centers") or []) if (c.get("pnl") or 0) < 0]
    if not lc:
        return None
    w = min(lc, key=lambda c: float(c.get("pnl") or 0))    # most negative
    return {"label": w.get("label") or "?", "pnl": w.get("pnl"),
            "n_positions": w.get("n_positions"),
            "plain": f"{w.get('label', '?')} — {int(w.get('n_positions') or 0)} bets"}


def synthesize(rep: dict) -> dict:
    rep = rep or {}
    state = _state(rep)
    top, more = _patterns(rep)
    # name only SPECIFIC markets in the verdict (e.g. "NBA, Soccer"), not generic axis buckets
    spec_labels = [t["label"] for t in top if t.get("specific")][:2]
    leagues = ", ".join(spec_labels) if spec_labels else "several markets"
    line = VERDICT[state]
    if state.startswith("profitable") and top:
        line = f"Makes money mostly from {leagues}. " + VERDICT[state]
    gaps = rep.get("data_gaps") or []
    return {
        "state": state,
        "verdict_line": line,
        "top_patterns": top,
        "more_patterns": more,
        "top_loss": _top_loss(rep),
        "missing_data": [str(g).replace("_", " ") for g in gaps][:5],
        "copyable_plain": ("This explains past behavior — it is not a recommendation. "
                           "Whether the edge holds is what a derived strategy's paper test checks."),
    }
