"""edge_cluster_builder — turns slices into guarded candidate edge groups (spec §4.5).

A wallet emits many clusters (edges AND loss centers). Guards keep them above 'descriptive':
n_events >= 5, n_positions >= 15, conjunction depth <= 2 (the slicer's `tag_price` axis is
the only depth-2 axis in V1). Overlap between clusters is reported, never silently summed."""
from __future__ import annotations

import hashlib

from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, EdgeSlice

_NON_INFORMATIVE = {"untagged", "??", "other-tagged"}


def _axis_depth(axis: str) -> int:
    """Conjunction depth = number of `_`-joined facets (e.g. `tag_price` -> 2). Structural,
    so an unknown future axis like `tag_price_hour` is correctly depth 3 and gets capped."""
    return axis.count("_") + 1


def _cid(axis: str, key: str) -> str:
    return hashlib.sha256(f"{axis}|{key}".encode()).hexdigest()[:16]


def _cluster_from_slice(s: EdgeSlice) -> EdgeCluster:
    return EdgeCluster(
        cluster_id=_cid(s.axis, s.slice_key), label=s.slice_key,
        scope={"axis": s.axis, "key": s.slice_key}, axis=s.axis,
        n_positions=s.n_positions, n_events=s.n_events, pnl=round(s.pnl, 2),
        edge_pp=round(s.edge_pp, 2),
        member_position_keys=[(e.condition_id, e.outcome_index, e.episode_index)
                              for e in s.members],
        is_loss_center=s.pnl < 0)


def build_clusters(slice_tables: dict[str, dict[str, EdgeSlice]], *,
                   min_events: int = 5, min_positions: int = 15,
                   max_depth: int = 2) -> list[EdgeCluster]:
    """Single- and depth-2 clusters that clear the guards, ranked by |pnl|, with overlap."""
    candidates: list[EdgeCluster] = []
    for axis, tbl in slice_tables.items():
        if _axis_depth(axis) > max_depth:
            continue
        for s in tbl.values():
            if s.slice_key in _NON_INFORMATIVE:
                continue
            if s.n_positions < min_positions or s.n_events < min_events:
                continue
            if abs(s.pnl) < 1.0:
                continue
            candidates.append(_cluster_from_slice(s))
    candidates.sort(key=lambda c: abs(c.pnl), reverse=True)
    for i, c in enumerate(candidates):
        ci = set(c.member_position_keys)
        for prev in candidates[:i]:
            inter = ci & set(prev.member_position_keys)
            if inter:
                c.overlap.append({"cluster_id": prev.cluster_id,
                                  "share": round(len(inter) / max(len(ci), 1), 3)})
    return candidates
