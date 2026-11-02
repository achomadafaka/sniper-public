"""Phase 2 — flip-ROI peer baseline source.

Reads from `wallet_flipper_tag_grain` (populated by the Phase 1.2
writer) and produces the `(mean_flip_roi, peer_count)` tuple FH4
consumes via the `FlipRoiBaselineFn` callable.

Two implementations exported:

  build_flip_roi_baseline_fn(supabase, *, wallet_protocol_version,
                              min_peer_flips=30, walk_full_history=True)
      Returns a closure suitable for passing as
      `flip_roi_baseline_fn` to `run_flipper_branch`. The closure
      runs one Supabase query per (grain, tag) lookup.

  build_cached_flip_roi_baseline_fn(...)
      Same as above but pre-fetches all relevant (grain, tag) peer
      data in a single query at construction time, then serves
      lookups from an in-memory dict. Use for analyzer runs that hit
      many (grain, tag) pairs — typical wallet has 200+ flipper
      grains.

Both honor the spec §4 filters:
  * status = 'active'
  * walk_full_history = true (configurable, default True)
  * protocol_version = $wallet_protocol_version (cross-version safety)
  * n_at_grain >= $min_peer_flips (default 30 per spec §3.2)
  * wallet != $wallet_under_analysis (self-exclusion)
"""
from __future__ import annotations

from typing import Callable

from sniper.analysis.wallet_protocol.ledger.supabase_io import SupabaseHttp


FlipRoiBaselineFn = Callable[[str, str, str], tuple[float, int] | None]


def build_flip_roi_baseline_fn(
    supabase: SupabaseHttp,
    *,
    wallet_protocol_version: str,
    min_peer_flips: int = 30,
    require_walk_full_history: bool = True,
) -> FlipRoiBaselineFn:
    """Build a per-call FlipRoiBaselineFn.

    Each invocation triggers a Supabase SELECT for the specific
    (grain, tag). Suitable for ad-hoc lookups. For analyzer runs that
    will hit many grains, prefer `build_cached_flip_roi_baseline_fn`.
    """
    def _baseline(grain: str, tag: str, wallet: str) -> tuple[float, int] | None:
        where: dict = {
            "grain": grain,
            "tag": tag,
            "status": "active",
            "protocol_version": wallet_protocol_version,
        }
        if require_walk_full_history:
            where["walk_full_history"] = True
        rows = supabase.select(
            "wallet_flipper_tag_grain",
            where=where,
            columns=("wallet", "grain_roi", "n_at_grain"),
        )
        # Apply self-exclusion + per-peer-flip floor in Python — the
        # SupabaseHttp Protocol's `where` is equality-only.
        wallet_lc = wallet.lower()
        peer_rois = [
            float(r["grain_roi"]) for r in rows
            if r["wallet"] != wallet_lc and int(r["n_at_grain"]) >= min_peer_flips
        ]
        if not peer_rois:
            return None
        return (sum(peer_rois) / len(peer_rois), len(peer_rois))

    return _baseline


def build_cached_flip_roi_baseline_fn(
    supabase: SupabaseHttp,
    *,
    wallet_protocol_version: str,
    min_peer_flips: int = 30,
    require_walk_full_history: bool = True,
) -> FlipRoiBaselineFn:
    """Build a FlipRoiBaselineFn backed by an upfront full fetch.

    Pre-fetches all active rows matching the version + walk filters,
    indexes them by (grain, tag) → list[(wallet, grain_roi)] in
    memory, and serves lookups locally. Single Supabase round-trip
    regardless of how many grains the caller queries.

    Note: the cache snapshot is taken at construction time. A long-
    running analyzer that depends on fresh peer data should rebuild
    the fn between runs.
    """
    where: dict = {
        "status": "active",
        "protocol_version": wallet_protocol_version,
    }
    if require_walk_full_history:
        where["walk_full_history"] = True

    rows = supabase.select(
        "wallet_flipper_tag_grain",
        where=where,
        columns=("wallet", "grain", "tag", "grain_roi", "n_at_grain"),
    )

    by_grain_tag: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for r in rows:
        if int(r["n_at_grain"]) < min_peer_flips:
            continue
        key = (r["grain"], r["tag"])
        by_grain_tag.setdefault(key, []).append(
            (r["wallet"], float(r["grain_roi"]))
        )

    def _baseline(grain: str, tag: str, wallet: str) -> tuple[float, int] | None:
        peers = by_grain_tag.get((grain, tag), [])
        wallet_lc = wallet.lower()
        peer_rois = [roi for w, roi in peers if w != wallet_lc]
        if not peer_rois:
            return None
        return (sum(peer_rois) / len(peer_rois), len(peer_rois))

    return _baseline


__all__ = [
    "FlipRoiBaselineFn",
    "build_flip_roi_baseline_fn",
    "build_cached_flip_roi_baseline_fn",
]
