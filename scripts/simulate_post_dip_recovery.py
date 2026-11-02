#!/usr/bin/env python3
"""Historically provable post-dip taker simulator for tennis favorites.

This avoids maker queue assumptions. It waits for an observable favorite price
drop, enters on the next available 1-minute price with explicit slippage, and
exits at aligned match resolution.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb

from scripts.simulate_stink_bid_smoke import (
    _entry_reference_price,
    _sports_fee,
    _to_ts,
    _winner_side,
)
from data.sackmann import player_sides_from_title, title_looks_like_doubles


@dataclass
class PostDipConfig:
    mode: str = "post_dip"  # post_dip | dip_reclaim | favorite_flip_reclaim | favorite_drop_fade | favorite_flip_fade
    drop_pct: float = 0.20
    reclaim_pct: float = 0.05
    reclaim_price: float = 0.52
    reclaim_window_minutes: int = 30
    max_trigger_minutes: int = 120
    min_ref: float = 0.60
    max_ref: float = 0.90
    slippage: float = 0.02
    position_size_usdc: float = 25.0
    max_entry_price: float = 0.98


@dataclass
class PostDipTrade:
    condition_id: str
    title: str
    tour: str
    sack_year: str
    favorite_side: str
    entry_side: str
    winner_side: str
    ref_price: float
    trigger_ts: int
    trigger_price: float
    local_low_price: Optional[float]
    entry_ts: int
    raw_entry_price: float
    entry_price: float
    pnl_usdc: float
    return_pct: float


@dataclass
class PostDipAttempt:
    condition_id: str
    title: str
    tour: str
    sack_year: str
    start_ts: Optional[int]
    favorite_side: Optional[str]
    entry_side: Optional[str]
    winner_side: Optional[str]
    ref_price: Optional[float]
    trigger_ts: Optional[int]
    trigger_price: Optional[float]
    local_low_price: Optional[float]
    entry_ts: Optional[int]
    entry_price: Optional[float]
    status: str
    reason: str


def _tour_key(raw: str | None) -> str:
    raw = (raw or "").lower()
    if raw.startswith("wta"):
        return "wta"
    if raw.startswith("atp"):
        return "atp"
    return raw or "unknown"


def _looks_like_moneyline_title(title: str) -> bool:
    candidate = title.split(":", 1)[1].strip() if ":" in title else title.strip()
    lowered = candidate.lower()
    if "will " in lowered or " by " in lowered or " games" in lowered or " set" in lowered:
        return False
    side1, side2 = player_sides_from_title(candidate)
    return bool(side1 and side2 and not title_looks_like_doubles(candidate))


def _summarize(trades: list[PostDipTrade]) -> dict:
    pnl = [trade.pnl_usdc for trade in trades]
    returns = [trade.return_pct for trade in trades]
    wins = sum(1 for trade in trades if trade.pnl_usdc > 0)
    gross_profit = sum(value for value in pnl if value > 0)
    gross_loss = -sum(value for value in pnl if value < 0)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade in sorted(trades, key=lambda item: (item.entry_ts, item.condition_id)):
        equity += trade.pnl_usdc
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    stdev = statistics.stdev(returns) if len(returns) > 1 else 0.0
    return {
        "entries": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate": wins / len(trades) if trades else 0.0,
        "total_pnl_usdc": sum(pnl),
        "avg_pnl_usdc": statistics.mean(pnl) if pnl else 0.0,
        "avg_entry_price": statistics.mean([trade.entry_price for trade in trades]) if trades else 0.0,
        "gross_profit_usdc": gross_profit,
        "gross_loss_usdc": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "max_drawdown_usdc": max_drawdown,
        "return_t_stat": (
            statistics.mean(returns) / (stdev / math.sqrt(len(returns)))
            if len(returns) >= 30 and stdev > 1e-9
            else None
        ),
    }


def _simulate_event(
    con,
    row,
    cfg: PostDipConfig,
    price_cache: Optional[dict[str, list[tuple[int, float]]]] = None,
) -> tuple[Optional[PostDipTrade], PostDipAttempt]:
    condition_id, title, tour_raw, start_date, token_yes, token_no, sack_winner, sack_date, sack_tour = row
    start_ts = _to_ts(start_date)
    sack_year = str(sack_date or "")[:4]
    tour = _tour_key(sack_tour or tour_raw)

    def attempt(status: str, reason: str, **kwargs) -> PostDipAttempt:
        return PostDipAttempt(
            condition_id=condition_id,
            title=title,
            tour=tour,
            sack_year=sack_year,
            start_ts=start_ts,
            favorite_side=kwargs.get("favorite_side"),
            entry_side=kwargs.get("entry_side"),
            winner_side=kwargs.get("winner_side"),
            ref_price=kwargs.get("ref_price"),
            trigger_ts=kwargs.get("trigger_ts"),
            trigger_price=kwargs.get("trigger_price"),
            local_low_price=kwargs.get("local_low_price"),
            entry_ts=kwargs.get("entry_ts"),
            entry_price=kwargs.get("entry_price"),
            status=status,
            reason=reason,
        )

    if not _looks_like_moneyline_title(title):
        return None, attempt("skipped", "non_moneyline_title")

    series = {}
    for side, token in (("yes", token_yes), ("no", token_no)):
        if price_cache is not None:
            series[side] = price_cache.get(token, [])
        else:
            rows = con.execute(
                """
                SELECT timestamp_sec, price
                FROM prices_history
                WHERE token_id = ? AND fidelity = 1
                ORDER BY timestamp_sec
                """,
                [token],
            ).fetchall()
            series[side] = [(int(ts), float(price)) for ts, price in rows]
    if not series["yes"] or not series["no"]:
        return None, attempt("skipped", "missing_price_history")

    ref_yes = _entry_reference_price(series["yes"], start_ts)
    ref_no = _entry_reference_price(series["no"], start_ts)
    if ref_yes is None or ref_no is None:
        return None, attempt("skipped", "missing_reference_price")
    favorite_side, ref_price = ("yes", ref_yes) if ref_yes >= ref_no else ("no", ref_no)
    winner_side = _winner_side(title, sack_winner)
    if winner_side is None:
        return None, attempt(
            "skipped",
            "winner_side_unmatched",
            favorite_side=favorite_side,
            ref_price=ref_price,
        )
    if ref_price < cfg.min_ref or ref_price > cfg.max_ref:
        return None, attempt(
            "skipped",
            "favorite_ref_outside_band",
            favorite_side=favorite_side,
            winner_side=winner_side,
            ref_price=ref_price,
        )

    cutoff_ts = start_ts + cfg.max_trigger_minutes * 60 if start_ts is not None else None
    threshold = ref_price * (1.0 - cfg.drop_pct)
    points = [
        (ts, price)
        for ts, price in series[favorite_side]
        if (start_ts is None or ts >= start_ts) and (cutoff_ts is None or ts <= cutoff_ts)
    ]
    underdog_side = "no" if favorite_side == "yes" else "yes"

    def next_price_after(side: str, ts: int) -> Optional[tuple[int, float]]:
        for price_ts, price in series[side]:
            if price_ts > ts:
                return price_ts, price
        return None

    if cfg.mode in {"favorite_flip_reclaim", "favorite_flip_fade"}:
        flip_idx = None
        for idx, (ts, price) in enumerate(points):
            if price < 0.50:
                flip_idx = idx
                break
        if flip_idx is None:
            return None, attempt(
                "no_entry",
                "no_favorite_flip",
                favorite_side=favorite_side,
                winner_side=winner_side,
                ref_price=ref_price,
            )
        trigger_ts, trigger_price = points[flip_idx]
        if cfg.mode == "favorite_flip_fade":
            next_entry = next_price_after(underdog_side, trigger_ts)
            if next_entry is None:
                return None, attempt(
                    "no_entry",
                    "no_next_price_after_trigger",
                    favorite_side=favorite_side,
                    entry_side=underdog_side,
                    winner_side=winner_side,
                    ref_price=ref_price,
                    trigger_ts=trigger_ts,
                    trigger_price=trigger_price,
                )
            entry_ts, raw_entry_price = next_entry
            entry_side = underdog_side
            entry_reason = "favorite_flip_fade_entry"
            local_low_price = trigger_price
        else:
            entry_side = favorite_side
            reclaim_cutoff = trigger_ts + cfg.reclaim_window_minutes * 60
            reclaim_idx = None
            for idx in range(flip_idx + 1, len(points)):
                ts, price = points[idx]
                if ts > reclaim_cutoff:
                    break
                if price >= cfg.reclaim_price:
                    reclaim_idx = idx
                    break
            if reclaim_idx is None:
                return None, attempt(
                    "no_entry",
                    "no_reclaim_after_flip",
                    favorite_side=favorite_side,
                    entry_side=entry_side,
                    winner_side=winner_side,
                    ref_price=ref_price,
                    trigger_ts=trigger_ts,
                    trigger_price=trigger_price,
                )
            entry_idx = reclaim_idx + 1
            if entry_idx >= len(points):
                return None, attempt(
                    "no_entry",
                    "no_next_price_after_trigger",
                    favorite_side=favorite_side,
                    entry_side=entry_side,
                    winner_side=winner_side,
                    ref_price=ref_price,
                    trigger_ts=trigger_ts,
                    trigger_price=trigger_price,
                )
            entry_ts, raw_entry_price = points[entry_idx]
            entry_reason = "favorite_flip_reclaim_entry"
            local_low_price = min(price for _ts, price in points[flip_idx : reclaim_idx + 1])
    else:
        entry_ts = None
        raw_entry_price = None
        entry_side = favorite_side
        entry_reason = "post_dip_entry"
        local_low_price = None

    trigger_idx = None
    if cfg.mode not in {"favorite_flip_reclaim", "favorite_flip_fade"}:
        for idx, (ts, price) in enumerate(points):
            if price <= threshold:
                trigger_idx = idx
                break
        if trigger_idx is None:
            return None, attempt(
                "no_entry",
                "no_drop_trigger",
                favorite_side=favorite_side,
                entry_side=entry_side,
                winner_side=winner_side,
                ref_price=ref_price,
            )
        trigger_ts, trigger_price = points[trigger_idx]
        entry_idx = trigger_idx + 1
        local_low_price = None
        entry_reason = "post_dip_entry"
        if cfg.mode == "favorite_drop_fade":
            next_entry = next_price_after(underdog_side, trigger_ts)
            if next_entry is None:
                return None, attempt(
                    "no_entry",
                    "no_next_price_after_trigger",
                    favorite_side=favorite_side,
                    entry_side=underdog_side,
                    winner_side=winner_side,
                    ref_price=ref_price,
                    trigger_ts=trigger_ts,
                    trigger_price=trigger_price,
                )
            entry_ts, raw_entry_price = next_entry
            entry_side = underdog_side
            entry_reason = "favorite_drop_fade_entry"
        elif cfg.mode == "dip_reclaim":
            local_low_price = trigger_price
            reclaim_cutoff = trigger_ts + cfg.reclaim_window_minutes * 60
            reclaim_idx = None
            for idx in range(trigger_idx + 1, len(points)):
                ts, price = points[idx]
                if ts > reclaim_cutoff:
                    break
                local_low_price = min(local_low_price, price)
                if price >= local_low_price * (1.0 + cfg.reclaim_pct):
                    reclaim_idx = idx
                    break
            if reclaim_idx is None:
                return None, attempt(
                    "no_entry",
                        "no_reclaim_after_drop",
                        favorite_side=favorite_side,
                        entry_side=entry_side,
                        winner_side=winner_side,
                        ref_price=ref_price,
                        trigger_ts=trigger_ts,
                    trigger_price=trigger_price,
                    local_low_price=local_low_price,
                )
            entry_idx = reclaim_idx
            entry_reason = "dip_reclaim_entry"
        elif cfg.mode != "post_dip":
            raise ValueError(f"unsupported mode: {cfg.mode}")

        if cfg.mode in {"post_dip", "dip_reclaim"} and entry_idx >= len(points):
            return None, attempt(
                "no_entry",
                "no_next_price_after_trigger",
                favorite_side=favorite_side,
                entry_side=entry_side,
                winner_side=winner_side,
                ref_price=ref_price,
                trigger_ts=trigger_ts,
                trigger_price=trigger_price,
                local_low_price=local_low_price,
            )
        if cfg.mode in {"post_dip", "dip_reclaim"}:
            entry_ts, raw_entry_price = points[entry_idx]
    entry_price = min(raw_entry_price + cfg.slippage, 0.999)
    if entry_price > cfg.max_entry_price:
        return None, attempt(
            "no_entry",
            "entry_price_above_max",
            favorite_side=favorite_side,
            entry_side=entry_side,
            winner_side=winner_side,
            ref_price=ref_price,
            trigger_ts=trigger_ts,
            trigger_price=trigger_price,
            local_low_price=local_low_price,
            entry_ts=entry_ts,
            entry_price=entry_price,
        )

    exit_price = 1.0 if entry_side == winner_side else 0.0
    pnl = (cfg.position_size_usdc / entry_price) * exit_price - cfg.position_size_usdc
    pnl -= _sports_fee(cfg.position_size_usdc, entry_price)
    trade = PostDipTrade(
        condition_id=condition_id,
        title=title,
        tour=tour,
        sack_year=sack_year,
        favorite_side=favorite_side,
        entry_side=entry_side,
        winner_side=winner_side,
        ref_price=ref_price,
        trigger_ts=trigger_ts,
        trigger_price=trigger_price,
        local_low_price=local_low_price,
        entry_ts=entry_ts,
        raw_entry_price=raw_entry_price,
        entry_price=entry_price,
        pnl_usdc=pnl,
        return_pct=pnl / cfg.position_size_usdc,
    )
    return trade, attempt(
        "entered",
        entry_reason,
        favorite_side=favorite_side,
        entry_side=entry_side,
        winner_side=winner_side,
        ref_price=ref_price,
        trigger_ts=trigger_ts,
        trigger_price=trigger_price,
        local_low_price=local_low_price,
        entry_ts=entry_ts,
        entry_price=entry_price,
    )


def run_sim(
    db_path: str | Path,
    cfg: PostDipConfig,
    *,
    since_year: Optional[int] = None,
    until_year: Optional[int] = None,
    tour: Optional[str] = None,
    limit: Optional[int] = None,
    include_audit: bool = False,
    trades_output: Optional[str | Path] = None,
) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        filters = []
        params = []
        if since_year is not None:
            filters.append("CAST(substr(a.sack_date, 1, 4) AS INTEGER) >= ?")
            params.append(since_year)
        if until_year is not None:
            filters.append("CAST(substr(a.sack_date, 1, 4) AS INTEGER) <= ?")
            params.append(until_year)
        if tour is not None:
            key = tour.lower()
            if key not in {"atp", "wta"}:
                raise ValueError(f"unsupported tour: {tour!r}")
            filters.append("LOWER(a.sack_tour) LIKE ?")
            params.append(f"{key}%")
        filter_clause = f"AND {' AND '.join(filters)}" if filters else ""
        rows = con.execute(
            f"""
            SELECT g.condition_id, g.title, g.tour_prefix, g.start_date,
                   g.token_id_yes, g.token_id_no, a.sack_winner, a.sack_date, a.sack_tour
            FROM gamma_events g
            JOIN event_alignment a USING(condition_id)
            WHERE EXISTS (SELECT 1 FROM prices_history p WHERE p.token_id = g.token_id_yes AND p.fidelity = 1)
              AND EXISTS (SELECT 1 FROM prices_history p WHERE p.token_id = g.token_id_no AND p.fidelity = 1)
              {filter_clause}
            ORDER BY g.end_date, g.condition_id
            """,
            params,
        ).fetchall()
        if limit:
            rows = rows[:limit]
        trades = []
        attempts = []
        for row in rows:
            trade, sim_attempt = _simulate_event(con, row, cfg)
            if trade is not None:
                trades.append(trade)
            if include_audit:
                attempts.append(sim_attempt)
        by_year = {
            year: _summarize([trade for trade in trades if trade.sack_year == year])
            for year in sorted({trade.sack_year for trade in trades})
        }
        by_tour = {
            tour_key: _summarize([trade for trade in trades if trade.tour == tour_key])
            for tour_key in sorted({trade.tour for trade in trades})
        }
        out = {
            "config": asdict(cfg),
            "eligible_priced_events": len(rows),
            "since_year": since_year,
            "until_year": until_year,
            "tour": tour,
            **_summarize(trades),
            "by_year": by_year,
            "by_tour": by_tour,
            "sample_trades": [asdict(trade) for trade in trades[:10]],
        }
        if include_audit:
            out["audit_events"] = [asdict(item) for item in attempts]
        if trades_output is not None:
            with open(trades_output, "w") as f:
                for trade in trades:
                    f.write(json.dumps(asdict(trade), sort_keys=True) + "\n")
            out["trades_output"] = str(trades_output)
            out["trades_written"] = len(trades)
        return out
    finally:
        con.close()


def parse_number_grid(raw: str, cast=float) -> list:
    return [cast(part.strip()) for part in raw.split(",") if part.strip()]


def _load_rows(
    con,
    *,
    since_year: Optional[int] = None,
    until_year: Optional[int] = None,
    tour: Optional[str] = None,
) -> list[tuple]:
    filters = []
    params = []
    if since_year is not None:
        filters.append("CAST(substr(a.sack_date, 1, 4) AS INTEGER) >= ?")
        params.append(since_year)
    if until_year is not None:
        filters.append("CAST(substr(a.sack_date, 1, 4) AS INTEGER) <= ?")
        params.append(until_year)
    if tour is not None:
        key = tour.lower()
        if key not in {"atp", "wta"}:
            raise ValueError(f"unsupported tour: {tour!r}")
        filters.append("LOWER(a.sack_tour) LIKE ?")
        params.append(f"{key}%")
    filter_clause = f"AND {' AND '.join(filters)}" if filters else ""
    return con.execute(
        f"""
        SELECT g.condition_id, g.title, g.tour_prefix, g.start_date,
               g.token_id_yes, g.token_id_no, a.sack_winner, a.sack_date, a.sack_tour
        FROM gamma_events g
        JOIN event_alignment a USING(condition_id)
        WHERE EXISTS (SELECT 1 FROM prices_history p WHERE p.token_id = g.token_id_yes AND p.fidelity = 1)
          AND EXISTS (SELECT 1 FROM prices_history p WHERE p.token_id = g.token_id_no AND p.fidelity = 1)
          {filter_clause}
        ORDER BY g.end_date, g.condition_id
        """,
        params,
    ).fetchall()


def _load_price_cache(con, rows: list[tuple]) -> dict[str, list[tuple[int, float]]]:
    token_ids = sorted({row[4] for row in rows} | {row[5] for row in rows})
    cache: dict[str, list[tuple[int, float]]] = {token_id: [] for token_id in token_ids}
    if not token_ids:
        return cache
    placeholders = ", ".join("?" for _ in token_ids)
    price_rows = con.execute(
        f"""
        SELECT token_id, timestamp_sec, price
        FROM prices_history
        WHERE fidelity = 1
          AND token_id IN ({placeholders})
        ORDER BY token_id, timestamp_sec
        """,
        token_ids,
    ).fetchall()
    for token_id, ts, price in price_rows:
        cache.setdefault(token_id, []).append((int(ts), float(price)))
    return cache


def _run_cached(rows: list[tuple], price_cache: dict[str, list[tuple[int, float]]], cfg: PostDipConfig) -> dict:
    trades = []
    for row in rows:
        trade, _attempt = _simulate_event(None, row, cfg, price_cache=price_cache)
        if trade is not None:
            trades.append(trade)
    return {"eligible_priced_events": len(rows), **_summarize(trades)}


def run_grid(
    db_path: str | Path,
    *,
    modes: Optional[list[str]] = None,
    drop_pcts: list[float],
    reclaim_pcts: Optional[list[float]] = None,
    reclaim_prices: Optional[list[float]] = None,
    reclaim_window_minutes: Optional[list[int]] = None,
    max_trigger_minutes: list[int],
    slippages: list[float],
    min_ref: float,
    max_ref: float,
    min_entries: int,
    since_year: Optional[int] = None,
    until_year: Optional[int] = None,
    tour: Optional[str] = None,
) -> dict:
    runs = []
    total = 0
    modes = modes or ["post_dip"]
    reclaim_pcts = reclaim_pcts or [0.05]
    reclaim_prices = reclaim_prices or [0.52]
    reclaim_window_minutes = reclaim_window_minutes or [30]
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = _load_rows(con, since_year=since_year, until_year=until_year, tour=tour)
        price_cache = _load_price_cache(con, rows)
        for mode, drop_pct, reclaim_pct, reclaim_price, reclaim_window, max_minutes, slippage in itertools.product(
            modes,
            drop_pcts,
            reclaim_pcts,
            reclaim_prices,
            reclaim_window_minutes,
            max_trigger_minutes,
            slippages,
        ):
            total += 1
            cfg = PostDipConfig(
                mode=mode,
                drop_pct=drop_pct,
                reclaim_pct=reclaim_pct,
                reclaim_price=reclaim_price,
                reclaim_window_minutes=reclaim_window,
                max_trigger_minutes=max_minutes,
                min_ref=min_ref,
                max_ref=max_ref,
                slippage=slippage,
            )
            summary = _run_cached(rows, price_cache, cfg)
            row = {
                "config": asdict(cfg),
                "eligible_priced_events": summary["eligible_priced_events"],
                "entries": summary["entries"],
                "wins": summary["wins"],
                "losses": summary["losses"],
                "win_rate": summary["win_rate"],
                "total_pnl_usdc": summary["total_pnl_usdc"],
                "avg_entry_price": summary["avg_entry_price"],
                "profit_factor": summary["profit_factor"],
                "max_drawdown_usdc": summary["max_drawdown_usdc"],
                "return_t_stat": summary["return_t_stat"],
            }
            if row["entries"] >= min_entries:
                row["objective_value"] = row["total_pnl_usdc"]
                runs.append(row)
    finally:
        con.close()
    runs.sort(key=lambda item: (item["objective_value"], item["entries"]), reverse=True)
    return {
        "runs_evaluated": total,
        "runs_after_min_entries": len(runs),
        "min_entries": min_entries,
        "best": runs[0] if runs else None,
        "runs": runs,
    }


def parse_args(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/backtest/stink_bid/backtest.db")
    parser.add_argument("--modes", default="post_dip")
    parser.add_argument("--drop-pcts", default="0.15,0.20,0.25,0.30")
    parser.add_argument("--reclaim-pcts", default="0.05")
    parser.add_argument("--reclaim-prices", default="0.52")
    parser.add_argument("--reclaim-window-minutes", default="30")
    parser.add_argument("--max-trigger-minutes", default="60,120,240")
    parser.add_argument("--slippage", default="0.01,0.02,0.03")
    parser.add_argument("--min-ref", type=float, default=0.60)
    parser.add_argument("--max-ref", type=float, default=0.90)
    parser.add_argument("--min-entries", type=int, default=30)
    parser.add_argument("--since-year", type=int)
    parser.add_argument("--until-year", type=int)
    parser.add_argument("--tour", choices=["atp", "wta"])
    parser.add_argument("--trades-output")
    parser.add_argument("--top", type=int, default=10)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    modes = [part.strip() for part in args.modes.split(",") if part.strip()]
    drop_pcts = parse_number_grid(args.drop_pcts, float)
    reclaim_pcts = parse_number_grid(args.reclaim_pcts, float)
    reclaim_prices = parse_number_grid(args.reclaim_prices, float)
    reclaim_windows = parse_number_grid(args.reclaim_window_minutes, int)
    trigger_minutes = parse_number_grid(args.max_trigger_minutes, int)
    slippages = parse_number_grid(args.slippage, float)
    if args.trades_output:
        if not (
            len(modes)
            == len(drop_pcts)
            == len(reclaim_pcts)
            == len(reclaim_prices)
            == len(reclaim_windows)
            == len(trigger_minutes)
            == len(slippages)
            == 1
        ):
            raise SystemExit("--trades-output requires single-value config args")
        out = run_sim(
            args.db,
            PostDipConfig(
                mode=modes[0],
                drop_pct=drop_pcts[0],
                reclaim_pct=reclaim_pcts[0],
                reclaim_price=reclaim_prices[0],
                reclaim_window_minutes=reclaim_windows[0],
                max_trigger_minutes=trigger_minutes[0],
                min_ref=args.min_ref,
                max_ref=args.max_ref,
                slippage=slippages[0],
            ),
            since_year=args.since_year,
            until_year=args.until_year,
            tour=args.tour,
            trades_output=args.trades_output,
        )
    else:
        out = run_grid(
            args.db,
            modes=modes,
            drop_pcts=drop_pcts,
            reclaim_pcts=reclaim_pcts,
            reclaim_prices=reclaim_prices,
            reclaim_window_minutes=reclaim_windows,
            max_trigger_minutes=trigger_minutes,
            slippages=slippages,
            min_ref=args.min_ref,
            max_ref=args.max_ref,
            min_entries=args.min_entries,
            since_year=args.since_year,
            until_year=args.until_year,
            tour=args.tour,
        )
        out["runs"] = out["runs"][: args.top]
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
