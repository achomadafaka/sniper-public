"""edge_position_slicer — (r-p)/PnL surfaces + within-wallet shuffle baseline (spec §4.4)."""
from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import datetime, timezone

from sniper.analysis.winning_wallet_thesis.types import EdgeSlice, PositionBook, ShuffleBaseline

NOISE_TYPES = {"REWARD", "YIELD", "MAKER_REBATE"}


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = (z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def ci_pp(s: EdgeSlice) -> tuple[float, float]:
    lo, hi = _wilson(s.wins, s.n_positions)
    return ((lo - s.mean_entry) * 100.0, (hi - s.mean_entry) * 100.0)


def _price_regime(entry: float) -> str:
    if entry < 0.10: return "deep-longshot"
    if entry < 0.30: return "longshot"
    if entry < 0.50: return "underdog"
    if entry < 0.70: return "lean-fav"
    if entry < 0.90: return "favorite"
    return "near-certain"


def _entry_hour(ep) -> str:
    if not ep.first_buy_ts:
        return "??"
    return f"{datetime.fromtimestamp(ep.first_buy_ts, tz=timezone.utc).hour:02d}h"


def reward_attribution(activity) -> dict:
    income, counts = defaultdict(float), defaultdict(int)
    usdc_total = non_usdc_total = 0.0
    for a in activity:
        t = a.get("type")
        if t in NOISE_TYPES:
            amt = float(a.get("usdcSize") or 0.0)
            income[t] += amt
            counts[t] += 1
            # REWARD/YIELD/MAKER_REBATE settle in pUSD/USDC (empty `asset`). A non-empty `asset`
            # means the row is denominated in an outcome/other token whose usdcSize is a notional,
            # not USD cash — exclude it from the USD reward total (verified empty for SwissMiss).
            if str(a.get("asset") or ""):
                non_usdc_total += amt
            else:
                usdc_total += amt
    return {"income": dict(income), "counts": dict(counts), "total": sum(income.values()),
            "usdc_total": round(usdc_total, 6), "non_usdc_total": round(non_usdc_total, 6),
            "non_usdc_flagged": non_usdc_total > 0.0}


def reward_share_pct(*, trading_pnl: float, reward_total: float) -> float:
    """Reward share with abs(trading_pnl) denominator — stays sane on losing books."""
    denom = abs(trading_pnl) + reward_total
    return reward_total / denom * 100.0 if denom else 0.0


def _add(table: dict, axis: str, key: str, ep) -> None:
    s = table.setdefault(key, EdgeSlice(slice_key=key, axis=axis))
    s.n_positions += 1
    s.wins += int(ep.won == 1)
    s.entry_sum += ep.entry_vwap
    s.pnl += ep.realized_pnl or 0.0
    s.cost += ep.buy_cost
    s.members.append(ep)


def _finalize_events(table: dict) -> None:
    for s in table.values():
        s.n_events = len({e.event_slug for e in s.members})


def slice_edges(book: PositionBook) -> dict[str, dict[str, EdgeSlice]]:
    rd = book.resolved_directional()
    axes = ("category", "tag", "subcat", "template", "price", "mechanism", "hour", "tag_price")
    tables: dict[str, dict[str, EdgeSlice]] = {a: {} for a in axes}
    for ep in rd:
        _add(tables["category"], "category", ep.category, ep)
        _add(tables["subcat"], "subcat", ep.subcat or "untagged", ep)
        _add(tables["template"], "template", ep.template or "untagged", ep)
        _add(tables["price"], "price", _price_regime(ep.entry_vwap), ep)
        _add(tables["mechanism"], "mechanism", ep.exit_mechanism, ep)
        _add(tables["hour"], "hour", _entry_hour(ep), ep)
        _add(tables["tag_price"], "tag_price",
             f"{ep.subcat} | {_price_regime(ep.entry_vwap)}", ep)
        for t in (ep.tags or ["untagged"]):
            _add(tables["tag"], "tag", t, ep)
    for tbl in tables.values():
        _finalize_events(tbl)
    return tables


def _count_green(tables: dict, *, min_pos: int, min_edge_pp: float) -> int:
    return sum(1 for tbl in tables.values() for s in tbl.values()
               if s.n_positions >= min_pos and s.edge_pp > min_edge_pp and s.pnl > 0)


def shuffle_baseline(book: PositionBook, *, n_shuffles: int = 50, seed: int = 0,
                     min_pos: int = 15, min_edge_pp: float = 2.0) -> ShuffleBaseline:
    """Shuffle win/loss within coarse price bands, rerun the slice family, and compare the
    observed green-slice count against the null distribution. Descriptive calibration,
    not a kill gate (spec §4.4)."""
    rd = book.resolved_directional()
    observed = _count_green(slice_edges(book), min_pos=min_pos, min_edge_pp=min_edge_pp)
    bands: dict[str, list] = defaultdict(list)
    for ep in rd:
        bands[_price_regime(ep.entry_vwap)].append(ep)
    rng = random.Random(seed)
    orig = {id(ep): ep.won for ep in rd}
    null_counts: list[int] = []
    try:
        for _ in range(n_shuffles):
            for band_eps in bands.values():
                outcomes = [orig[id(ep)] for ep in band_eps]
                rng.shuffle(outcomes)
                for ep, w in zip(band_eps, outcomes):
                    ep.won = w
            null_counts.append(_count_green(slice_edges(book), min_pos=min_pos,
                                            min_edge_pp=min_edge_pp))
    finally:
        for ep in rd:                   # always restore truth
            ep.won = orig[id(ep)]
    expected = sum(null_counts) / len(null_counts) if null_counts else 0.0
    over = observed / expected if expected else float(observed)
    n = len(null_counts)
    count_ge = sum(1 for c in null_counts if c >= observed)
    rank = count_ge / n if n else 1.0
    # Monte Carlo p-value with the standard (count+1)/(n+1) correction (Davison & Hinkley): the
    # observed arrangement is itself one draw, so a finite shuffle set can never justify p=0.000.
    p_value = (count_ge + 1) / (n + 1) if n else 1.0
    nc = sorted(null_counts)
    p95 = nc[min(len(nc) - 1, int(0.95 * len(nc)))] if nc else 0
    return ShuffleBaseline(n_shuffles=n_shuffles, expected_green=round(expected, 2),
                           observed_green=observed, obs_over_expected=round(over, 2),
                           top_slice_rank_under_null=round(rank, 3),
                           p_value=round(p_value, 3), null_p95=p95)
