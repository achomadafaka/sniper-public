"""Liquidation cluster-map lane — HL is uniquely transparent: every account's liquidation
price is public via clearinghouseState. We harvest addresses, poll their positions, and
aggregate liquidationPx × notional into a per-asset price heatmap. The nearest dense cluster
is a magnet (price gravitates there to trigger forced fills) — exposed to the engine as an
entry-timing helper for the cluster_magnet setup, never a standalone trigger.
"""
from __future__ import annotations

from collections import defaultdict

BIN_BPS = 10.0           # price-bucket width for the heatmap


def positions_from_clearinghouse(state: dict) -> list[dict]:
    """Extract (asset, liq_px, notional) from one clearinghouseState response."""
    out = []
    for ap in (state or {}).get("assetPositions", []) or []:
        pos = ap.get("position") or {}
        coin = pos.get("coin")
        liq = _f(pos.get("liquidationPx"))
        szi = _f(pos.get("szi"))
        entry = _f(pos.get("entryPx"))
        if coin and liq is not None and liq > 0 and szi is not None and szi != 0:
            notional = abs(szi) * (entry if entry else liq)
            out.append({"asset": str(coin).upper(), "liq_px": liq, "notional": notional})
    return out


def build_heatmap(positions: list[dict]) -> dict[str, list[tuple[float, float]]]:
    by_asset = defaultdict(list)
    for p in positions:
        by_asset[p["asset"]].append((float(p["liq_px"]), float(p["notional"])))
    return dict(by_asset)


def cluster_signal(levels: list[tuple[float, float]], mid: float, *,
                   min_notional: float = 0.0, max_distance_bps: float = 300.0,
                   whale_weight: bool = True) -> tuple[str, float, float] | None:
    """Nearest dense liquidation cluster within range. Returns (side, distance_bps, notional)
    where side is the magnet direction (toward the cluster).

    whale_weight=True (default, Thesis-1 fix): rank bins by their single largest position notional
    first, then total — the accounts whose liquidation actually moves price, not a crowd of dust.
    whale_weight=False keeps the original equal-weight (max total notional) behavior."""
    if not levels or mid <= 0:
        return None
    bins_total: dict = defaultdict(float)
    bins_max: dict = defaultdict(float)
    bin_w = mid * BIN_BPS / 10000.0
    for price, notional in levels:
        if bin_w <= 0:
            continue
        b = round(price / bin_w)
        bins_total[b] += notional
        if notional > bins_max[b]:
            bins_max[b] = notional
    best = None
    best_rank = None
    for b, total in bins_total.items():
        center = b * bin_w
        dist_bps = abs(center - mid) / mid * 10000.0
        if dist_bps > max_distance_bps or total < min_notional or center == mid:
            continue
        rank = (bins_max[b], total) if whale_weight else (total, total)
        if best is None or rank > best_rank:
            side = "long" if center > mid else "short"
            best = (side, round(dist_bps, 1), total)
            best_rank = rank
    return best


def poll_clusters(info_fn, addresses: list[str], mids: dict[str, float], *,
                  min_notional: float = 0.0) -> dict[str, tuple[str, float, float]]:
    """Poll clearinghouseState for each address, build heatmaps, return per-asset signals.
    `info_fn(addr) -> clearinghouseState dict`. `mids[asset] -> current mid`."""
    positions = []
    for addr in addresses:
        try:
            positions.extend(positions_from_clearinghouse(info_fn(addr)))
        except Exception:
            continue
    heatmap = build_heatmap(positions)
    out = {}
    for asset, levels in heatmap.items():
        mid = mids.get(asset)
        if not mid:
            continue
        sig = cluster_signal(levels, mid, min_notional=min_notional)
        if sig:
            out[asset] = sig
    return out


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
