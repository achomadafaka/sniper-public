"""Read-only shadow analysis for crypto5m volatility-floor choices.

This does not change the live desk. It recomputes fair values from captured Binance
prices under candidate vol floors, then compares Brier and maker-fill economics.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
import math
from typing import Iterable

from sniper.algos.polymarket_crypto5m import live_desk as LD


DEFAULT_FLOORS = (1e-5, 2e-5, 5e-5, 1e-4)
DEFAULT_BLEND_WEIGHTS = (0.25, 0.50, 0.75)


def floor_key(floor: float) -> str:
    return f"{float(floor):g}"


def blend_key(weight_structural: float, floor: float) -> str:
    return f"{float(weight_structural):g}@{floor_key(floor)}"


def candidate_fairs(*, btc_now: float, btc_start: float, raw_vol: float | None,
                    seconds_left: int | float, floors: Iterable[float] = DEFAULT_FLOORS) -> dict:
    out = {}
    for floor in floors:
        f = float(floor)
        vol = max(float(raw_vol), f) if raw_vol is not None else f
        fair = LD.fair_value_up(btc_now, btc_start, vol, seconds_left)
        out[floor_key(f)] = {
            "floor": f,
            "raw_vol": raw_vol,
            "vol_used": vol,
            "floor_bound": raw_vol is None or float(raw_vol) < f,
            "fair_up": fair,
        }
    return out


def fill_edge(*, token: str, side: str, price: float, fair_up: float) -> float | None:
    token_fair = _token_fair(token, fair_up)
    if token_fair is None:
        return None
    if str(side).lower() == "bid":
        return round(token_fair - float(price), 8)
    if str(side).lower() == "ask":
        return round(float(price) - token_fair, 8)
    return None


def settled_fill_pnl(*, token: str, side: str, price: float, size: float, winner: str) -> float | None:
    winner_l = str(winner or "").lower()
    token_l = str(token or "").lower()
    if winner_l not in ("up", "down") or token_l not in ("up", "down"):
        return None
    payoff = 1.0 if token_l == winner_l else 0.0
    price_f = float(price)
    size_f = float(size)
    if str(side).lower() == "bid":
        return round((payoff - price_f) * size_f, 8)
    if str(side).lower() == "ask":
        return round((price_f - payoff) * size_f, 8)
    return None


def build_vol_floor_shadow_report(capture_conn, maker_conn=None, *, campaign: str | None = None,
                                  floors: Iterable[float] = DEFAULT_FLOORS, sample_step_s: int = 5,
                                  lookback_s: int = LD.VOL_LOOKBACK_S,
                                  limit_windows: int | None = None,
                                  before_window_start_ts: int | None = None,
                                  blend_weights: Iterable[float] = DEFAULT_BLEND_WEIGHTS) -> dict:
    floor_tuple = tuple(float(f) for f in floors)
    blend_tuple = tuple(float(w) for w in blend_weights)
    series = _load_binance_series(capture_conn)
    series_index = _series_index(series)
    vol_by_sec = rolling_raw_vols(series, lookback_s=lookback_s)
    windows = _load_resolved_windows(
        capture_conn,
        limit_windows=limit_windows,
        before_window_start_ts=before_window_start_ts,
    )
    up_books = _load_up_books(capture_conn, windows)
    brier_by_floor = {floor_key(f): {"n": 0, "_sum": 0.0} for f in floor_tuple}
    brier_by_blend = {
        blend_key(w, f): {"n": 0, "_sum": 0.0, "weight_structural": w, "floor": f}
        for f in floor_tuple for w in blend_tuple
    }
    market_brier = {"n": 0, "_sum": 0.0}
    sample_count = 0

    for w in windows:
        start = _price_at_or_before(series_index, int(w["window_start_ts"]))
        if start is None or start <= 0:
            continue
        label = 1.0 if str(w["winning_outcome"]).lower() == "up" else 0.0
        for sec in range(int(w["window_start_ts"]) + 1, int(w["window_end_ts"]), int(sample_step_s)):
            px = _price_at_or_before(series_index, sec)
            raw_vol = vol_by_sec.get(sec)
            left = int(w["window_end_ts"]) - sec
            if px is None or raw_vol is None or left <= 0:
                continue
            sample_count += 1
            for key, row in candidate_fairs(
                btc_now=px, btc_start=start, raw_vol=raw_vol, seconds_left=left, floors=floor_tuple,
            ).items():
                fair = row["fair_up"]
                if fair is None:
                    continue
                brier_by_floor[key]["n"] += 1
                brier_by_floor[key]["_sum"] += (fair - label) ** 2
            mid = _book_mid_at(up_books.get(w["condition_id"], ([], [])), sec)
            if mid is not None:
                market_brier["n"] += 1
                market_brier["_sum"] += (mid - label) ** 2
                fairs = candidate_fairs(
                    btc_now=px, btc_start=start, raw_vol=raw_vol, seconds_left=left, floors=floor_tuple,
                )
                for floor in floor_tuple:
                    fair = fairs[floor_key(floor)]["fair_up"]
                    if fair is None:
                        continue
                    for weight in blend_tuple:
                        blended = blend_fair(fair, mid, weight)
                        if blended is None:
                            continue
                        key = blend_key(weight, floor)
                        brier_by_blend[key]["n"] += 1
                        brier_by_blend[key]["_sum"] += (blended - label) ** 2

    fill_edge_by_floor = (
        _fill_edge_report(
            maker_conn,
            campaign,
            series_index,
            vol_by_sec,
            floor_tuple,
            before_window_start_ts=before_window_start_ts,
        )
        if maker_conn is not None and campaign else {}
    )
    fill_edge_by_blend = (
        _fill_edge_report(maker_conn, campaign, series_index, vol_by_sec, floor_tuple,
                          up_books=up_books, blend_weights=blend_tuple,
                          before_window_start_ts=before_window_start_ts)
        if maker_conn is not None and campaign else {}
    )
    return {
        "floors": [floor_key(f) for f in floor_tuple],
        "blend_weights": [float(w) for w in blend_tuple],
        "lookback_s": int(lookback_s),
        "sample_step_s": int(sample_step_s),
        "before_window_start_ts": int(before_window_start_ts) if before_window_start_ts is not None else None,
        "windows": len(windows),
        "samples": sample_count,
        "vol_summary": _vol_summary(vol_by_sec.values(), floor_tuple),
        "brier_by_floor": _finalize_brier(brier_by_floor),
        "brier_by_blend": _finalize_brier(brier_by_blend),
        "brier_market_mid": _finalize_one_brier(market_brier),
        "fill_edge_by_floor": fill_edge_by_floor,
        "fill_edge_by_blend": fill_edge_by_blend,
    }


def rolling_raw_vols(series: list[tuple[int, float]], *, lookback_s: int = LD.VOL_LOOKBACK_S) -> dict[int, float]:
    secs = [int(s) for s, _ in series]
    prices = [float(p) for _, p in series]
    out: dict[int, float] = {}
    for i, sec in enumerate(secs):
        j = bisect_left(secs, sec - int(lookback_s))
        closes = prices[j:i + 1]
        vol = LD.realized_vol_per_s(closes)
        if vol is not None:
            out[sec] = vol
    return out


def _load_binance_series(conn) -> list[tuple[int, float]]:
    rows = conn.execute(
        "SELECT source_event_ts, price FROM crypto5m_binance_observations "
        "WHERE symbol='BTCUSDT' AND price IS NOT NULL ORDER BY source_event_ts, id"
    ).fetchall()
    by_sec: dict[int, float] = {}
    for row in rows:
        by_sec[int(row["source_event_ts"] // 1000)] = float(row["price"])
    return sorted(by_sec.items())


def _load_resolved_windows(conn, *, limit_windows: int | None = None,
                           before_window_start_ts: int | None = None) -> list:
    q = (
        "SELECT m.condition_id, m.window_start_ts, m.window_end_ts, m.up_token_id, s.winning_outcome "
        "FROM crypto5m_markets m JOIN crypto5m_settlements s ON s.condition_id=m.condition_id "
        "WHERE s.winning_outcome IN ('Up','Down') "
    )
    params: list[int] = []
    if before_window_start_ts is not None:
        q += "AND m.window_start_ts < ? "
        params.append(int(before_window_start_ts))
    q += "ORDER BY m.window_start_ts"
    rows = conn.execute(q, params).fetchall()
    if limit_windows:
        return rows[-int(limit_windows):]
    return rows


def _load_up_books(conn, windows: list) -> dict[str, tuple[list[int], list[tuple[float | None, float | None]]]]:
    out: dict[str, tuple[list[int], list[tuple[float | None, float | None]]]] = {}
    for w in windows:
        rows = conn.execute(
            "SELECT exchange_ts, best_bid, best_ask FROM crypto5m_book_snapshots "
            "WHERE condition_id=? AND token_id=? ORDER BY exchange_ts",
            (w["condition_id"], w["up_token_id"]),
        ).fetchall()
        times, books = [], []
        for row in rows:
            times.append(int(row["exchange_ts"] // 1000))
            books.append((_float_or_none(row["best_bid"]), _float_or_none(row["best_ask"])))
        out[w["condition_id"]] = (times, books)
    return out


def blend_fair(structural_fair_up: float | None, market_mid_up: float | None,
               weight_structural: float) -> float | None:
    if structural_fair_up is None or market_mid_up is None:
        return None
    w = min(1.0, max(0.0, float(weight_structural)))
    return w * float(structural_fair_up) + (1.0 - w) * float(market_mid_up)


def _fill_edge_report(maker_conn, campaign: str, series_index: tuple[list[int], list[float]],
                      vol_by_sec: dict[int, float],
                      floors: tuple[float, ...], *,
                      up_books: dict[str, tuple[list[int], list[tuple[float | None, float | None]]]] | None = None,
                      blend_weights: tuple[float, ...] | None = None,
                      before_window_start_ts: int | None = None) -> dict:
    q = (
        "SELECT f.*, w.winning_outcome FROM maker_fills f "
        "JOIN maker_windows w ON w.campaign_id=f.campaign_id AND w.condition_id=f.condition_id "
        "WHERE f.campaign_id=? AND w.settled=1 AND w.winning_outcome IN ('Up','Down') "
    )
    params: list[object] = [campaign]
    if before_window_start_ts is not None:
        q += "AND f.window_start_ts < ? "
        params.append(int(before_window_start_ts))
    q += "ORDER BY f.ts, f.rowid"
    rows = maker_conn.execute(q, params).fetchall()
    if blend_weights:
        groups = {
            blend_key(w, f): {
                "n": 0, "_edge_value": 0.0, "_size": 0.0, "_realized": 0.0,
                "weight_structural": float(w), "floor": float(f),
            }
            for f in floors for w in blend_weights
        }
    else:
        groups = {
            floor_key(f): {"n": 0, "_edge_value": 0.0, "_size": 0.0, "_realized": 0.0}
            for f in floors
        }
    for row in rows:
        ts = int(row["ts"])
        start = _price_at_or_before(series_index, int(row["window_start_ts"]))
        px = _price_at_or_before(series_index, ts)
        raw_vol = vol_by_sec.get(ts)
        left = max(1, int(row["window_start_ts"]) + LD.WINDOW_S - ts)
        if start is None or px is None or raw_vol is None:
            continue
        fairs = candidate_fairs(btc_now=px, btc_start=start, raw_vol=raw_vol, seconds_left=left, floors=floors)
        realized = settled_fill_pnl(
            token=row["token"], side=row["side"], price=row["price"], size=row["size"], winner=row["winning_outcome"],
        )
        if realized is None:
            continue
        size = float(row["size"] or 0.0)
        if size <= 0:
            continue
        if blend_weights:
            market_mid = _book_mid_at((up_books or {}).get(row["condition_id"], ([], [])), ts)
            if market_mid is None:
                continue
            for floor in floors:
                fair_row = fairs[floor_key(floor)]
                for weight in blend_weights:
                    fair_up = blend_fair(fair_row["fair_up"], market_mid, weight)
                    edge = fill_edge(token=row["token"], side=row["side"], price=row["price"], fair_up=fair_up)
                    if edge is None:
                        continue
                    g = groups[blend_key(weight, floor)]
                    g["n"] += 1
                    g["_edge_value"] += edge * size
                    g["_size"] += size
                    g["_realized"] += realized
        else:
            for key, fair_row in fairs.items():
                edge = fill_edge(token=row["token"], side=row["side"], price=row["price"], fair_up=fair_row["fair_up"])
                if edge is None:
                    continue
                g = groups[key]
                g["n"] += 1
                g["_edge_value"] += edge * size
                g["_size"] += size
                g["_realized"] += realized
    out = {}
    for key, g in groups.items():
        size = g["_size"]
        out[key] = {
            "n_fills": g["n"],
            "mean_claimed_edge": round(g["_edge_value"] / size, 8) if size else None,
            "mean_realized_pnl_per_share": round(g["_realized"] / size, 8) if size else None,
            "total_realized_pnl": round(g["_realized"], 4),
        }
        if "weight_structural" in g:
            out[key]["weight_structural"] = g["weight_structural"]
            out[key]["floor"] = g["floor"]
    return out


def _vol_summary(vols: Iterable[float], floors: tuple[float, ...]) -> dict:
    vals = sorted(float(v) for v in vols if v is not None)
    return {
        "n": len(vals),
        "p10": _pct(vals, 0.10),
        "p50": _pct(vals, 0.50),
        "p90": _pct(vals, 0.90),
        "floors": {
            floor_key(f): {
                "bind_count": sum(1 for v in vals if v < f),
                "bind_rate": round(sum(1 for v in vals if v < f) / len(vals), 6) if vals else None,
            }
            for f in floors
        },
    }


def _finalize_brier(rows: dict) -> dict:
    return {key: _finalize_one_brier(row) for key, row in rows.items()}


def _finalize_one_brier(row: dict) -> dict:
    n = int(row["n"])
    out = {"n": n, "brier": round(row["_sum"] / n, 8) if n else None}
    for key in ("weight_structural", "floor"):
        if key in row:
            out[key] = row[key]
    return out


def _book_mid_at(index: tuple[list[int], list[tuple[float | None, float | None]]], sec: int) -> float | None:
    times, books = index
    i = bisect_right(times, int(sec)) - 1
    if i < 0:
        return None
    bid, ask = books[i]
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return bid if bid is not None else ask


def _series_index(series: list[tuple[int, float]]) -> tuple[list[int], list[float]]:
    return [int(s) for s, _ in series], [float(p) for _, p in series]


def _price_at_or_before(index: tuple[list[int], list[float]], sec: int) -> float | None:
    secs, prices = index
    i = bisect_right(secs, int(sec)) - 1
    if i < 0:
        return None
    return prices[i]


def _token_fair(token: str, fair_up: float | None) -> float | None:
    if fair_up is None:
        return None
    token_l = str(token or "").lower()
    if token_l == "up":
        return float(fair_up)
    if token_l == "down":
        return 1.0 - float(fair_up)
    return None


def _float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    idx = min(len(vals) - 1, max(0, int(q * (len(vals) - 1))))
    return vals[idx]
