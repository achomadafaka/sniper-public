"""CSE-F3/F4 — stratification + decision-matrix builder.

Joins F1 engagements (her picks) with the F2 point-in-time context (the
unselected candidates that existed at her decision time) into the labelled
decision matrix the detection battery + learner run on.

Two resolution modes:
  - SYNTHETIC (meta carries `resolved_to_side` + explicit `outcome_role`): the
    original conditionId-keyed path; unit tests rely on it.
  - REAL per-side (meta carries `winning_index` + `outcomes`): resolves HER
    chosen outcome (not the YES side) and matches each candidate on the side with
    the SAME outcome_role (underdog↔underdog), curing the NO-side / multi-outcome
    mislabel. Every binary market offers both a favorite and an underdog side, so
    the candidate universe is scoped by (category, market_type) and the role
    selects which side to compare — outcome_role is NOT a universe filter here.

Spec: sre-f3-stratification-counterfactual.md.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from sniper.analysis.counterfactual_selection.pit_market_context import MarketContext

DEFAULT_SCOPE_AXES = ("category", "market_type", "outcome_role")
# In real per-side mode outcome_role is applied as a side-selector, not a filter.
_REAL_SCOPE_AXES = ("category", "market_type")


def _is_real(meta_entry: Mapping[str, Any]) -> bool:
    """Real per-side resolution data present (live/Gamma), vs synthetic fixture."""
    return "winning_index" in (meta_entry or {})


def _resolved_label(meta_entry: Mapping[str, Any]) -> Any:
    """Synthetic won-on-side label (resolved_to_side, else resolved_yes)."""
    if "resolved_to_side" in meta_entry:
        return meta_entry.get("resolved_to_side")
    return meta_entry.get("resolved_yes")


def _features(context: MarketContext, cid: str, T: int, *, entry_price: float | None) -> dict:
    st = context.market_state_at(cid, T)
    feats: dict[str, Any] = {
        "price": st["price"],
        "market_age": st["market_age_s"],
        "time_to_resolution": st["time_to_resolution_s"],
        "outcome_role": st["outcome_role"],
        "favorite_rank": st["favorite_rank"],
    }
    if entry_price is not None:
        feats["entry_price"] = entry_price
    return feats


def _match_outcome_index(her_outcome: str, outcomes: Sequence[Any]) -> int | None:
    """Index of her chosen outcome in the market's outcome list (case-insensitive)."""
    if not her_outcome or not outcomes:
        return None
    target = her_outcome.strip().lower()
    for i, o in enumerate(outcomes):
        if str(o).strip().lower() == target:
            return i
    return None


def _role_of(price: float | None) -> str | None:
    if price is None:
        return None
    return "favorite" if price > 0.5 else "underdog"


def _side_price_from_yes(price_yes: float | None, side_idx: int | None,
                         outcomes: Sequence[Any]) -> float | None:
    if price_yes is None or side_idx is None:
        return None
    try:
        idx = int(side_idx)
    except (TypeError, ValueError):
        return None
    if idx == 0:
        return float(price_yes)
    if len(outcomes or []) == 2 and idx == 1:
        return 1.0 - float(price_yes)
    return None


def _pick_row_real(context: MarketContext, e: Mapping[str, Any], cid: str, T: int,
                   real_axes: Sequence[str]) -> dict:
    """Her engagement → row resolved on HER chosen outcome at her entry price.
    Her exact side comes from outcome_index when present (positions), else by
    matching the outcome string. Group = the scope axes + her side-role."""
    m = context.market_meta.get(cid, {})
    st = context.market_state_at(cid, T)
    her_price = e.get("entry_vwap")
    her_idx = e.get("outcome_index")
    if her_idx is None:
        her_idx = _match_outcome_index(str(e.get("outcome") or ""), m.get("outcomes") or [])
    side_price = _side_price_from_yes(st.get("price"), her_idx, m.get("outcomes") or [])
    if her_price is None:
        her_price = side_price
    role = _role_of(side_price if side_price is not None else her_price)
    win_idx = m.get("winning_index")
    resolved = None if (win_idx is None or her_idx is None) else (1 if int(her_idx) == win_idx else 0)
    group_id = "|".join([str(m.get(ax) or "_") for ax in real_axes] + [str(role or "_")])
    return {
        "group_id": group_id, "picked": 1, "ts": T, "resolved": resolved,
        "features": {
            "price": side_price, "market_age": st.get("market_age_s"),
            "time_to_resolution": st.get("time_to_resolution_s"),
            "outcome_role": role, "favorite_rank": (1 if role == "favorite" else 2 if role else None),
            "entry_price": her_price,
        },
    }


def _candidate_row_real(context: MarketContext, c: str, T: int, role: str | None,
                        group_id: str) -> dict | None:
    """Candidate compared on the side with the SAME role as her pick. Binary: the
    two sides are complementary (side1 = 1 - side0), so derive both from side0."""
    m = context.market_meta.get(c, {})
    st = context.market_state_at(c, T)
    side0 = st.get("price")  # index-0 (YES) price at T
    if side0 is None:
        return None
    underdog_idx = 0 if side0 < 0.5 else 1
    favorite_idx = 1 - underdog_idx
    comp_idx = underdog_idx if role == "underdog" else favorite_idx
    comp_price = side0 if comp_idx == 0 else 1.0 - side0
    win_idx = m.get("winning_index")
    resolved = None if win_idx is None else (1 if comp_idx == win_idx else 0)
    return {
        "group_id": group_id, "picked": 0, "ts": T, "resolved": resolved,
        "features": {
            "price": comp_price, "market_age": st.get("market_age_s"),
            "time_to_resolution": st.get("time_to_resolution_s"),
            "outcome_role": role, "favorite_rank": (1 if comp_price >= 0.5 else 2),
        },
    }


MATCH_FEATURES = ("price", "market_age", "time_to_resolution")


def _group(rows: Sequence[Mapping[str, Any]]) -> dict[str, list]:
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(str(r.get("group_id")), []).append(r)
    return out


def nearest_neighbor_match(rows: Sequence[Mapping[str, Any]], *,
                           features: Sequence[str] = MATCH_FEATURES,
                           k: int = 1) -> list[dict]:
    """C7/G2 — within each group, match each picked row to its k nearest unselected
    controls on STANDARDIZED covariates (no replacement). Returns picks + their
    matched controls, so the edge test runs on covariate-balanced rows rather than
    the first-N same-scope slice. Cures confounds like a market_age imbalance
    masquerading as a selection signal."""
    matched: list[dict] = []
    for grp in _group(rows).values():
        picks = [r for r in grp if int(r.get("picked", 0)) == 1]
        cands = [r for r in grp if int(r.get("picked", 0)) == 0]
        if not picks or not cands:
            continue
        # pooled mean/std per feature across the whole group
        stats: dict[str, tuple[float, float]] = {}
        for f in features:
            vals = [float((r.get("features") or {}).get(f)) for r in grp
                    if (r.get("features") or {}).get(f) is not None]
            if len(vals) >= 2:
                mean = sum(vals) / len(vals)
                var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
                stats[f] = (mean, var ** 0.5)

        def _dist(a, b):
            fa, fb = a.get("features") or {}, b.get("features") or {}
            sq, n = 0.0, 0
            for f, (mean, std) in stats.items():
                if std <= 0:
                    continue
                va, vb = fa.get(f), fb.get(f)
                if va is None or vb is None:
                    continue
                sq += ((float(va) - float(vb)) / std) ** 2
                n += 1
            return (sq / n) ** 0.5 if n else None

        used: set[int] = set()
        for p in picks:
            matched.append(p)
            dists = []
            for j, c in enumerate(cands):
                if j in used:
                    continue
                d = _dist(p, c)
                if d is not None:
                    dists.append((d, j))
            dists.sort(key=lambda x: x[0])
            for _, j in dists[:k]:
                used.add(j)
                matched.append(cands[j])
    return matched


def build_decision_matrix(
    engagements: Sequence[Mapping[str, Any]],
    context: MarketContext,
    *,
    scope_axes: Sequence[str] = DEFAULT_SCOPE_AXES,
    real_scope_axes: Sequence[str] = _REAL_SCOPE_AXES,
    max_candidates: int = 50,
) -> list[dict]:
    """One row per (candidate market at a decision time). Picked rows = her
    engagements; unpicked rows = same-scope candidates that existed at T. Resolved
    is won-on-her-side (real per-side) or meta resolved_to_side (synthetic) —
    post-engagement label-only (edge test only).

    real_scope_axes selects the candidate-matching scope in real mode: default
    (category, market_type); pass ("event_family",) for the C4 event-sibling
    counterfactual (why this market, not the others of the same event)."""
    picked_cids = {str(e["conditionId"]) for e in engagements}
    rows: list[dict] = []
    for e in engagements:
        cid = str(e["conditionId"])
        T = int(e["first_entry_ts"])
        real = _is_real(context.market_meta.get(cid, {}))

        if real:
            pick = _pick_row_real(context, e, cid, T, real_scope_axes)
            role = pick["features"]["outcome_role"]
            rows.append(pick)
            scope = {ax: context.market_meta.get(cid, {}).get(ax)
                     for ax in real_scope_axes if context.market_meta.get(cid, {}).get(ax) is not None}
            cands = context.unselected_candidates(
                picked_cid=cid, T=T, filters=scope, exclude=picked_cids
            )[:max_candidates]
            for c in cands:
                row = _candidate_row_real(context, c, T, role, pick["group_id"])
                if row is not None:
                    rows.append(row)
            continue

        # synthetic path (unchanged)
        st = context.market_state_at(cid, T)
        scope = {ax: st.get(ax) for ax in scope_axes if st.get(ax) is not None}
        group_id = "|".join(str(scope.get(ax, "_")) for ax in scope_axes)
        rows.append({
            "group_id": group_id, "picked": 1, "ts": T,
            "features": _features(context, cid, T, entry_price=e.get("entry_vwap")),
            "resolved": _resolved_label(context.market_meta.get(cid, {})),
        })
        cands = context.unselected_candidates(
            picked_cid=cid, T=T, filters=scope, exclude=picked_cids
        )[:max_candidates]
        for c in cands:
            rows.append({
                "group_id": group_id, "picked": 0, "ts": T,
                "features": _features(context, c, T, entry_price=None),
                "resolved": _resolved_label(context.market_meta.get(c, {})),
            })
    return rows
