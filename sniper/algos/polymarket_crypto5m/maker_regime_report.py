"""Cold-path reporting for crypto5m maker regime/profile outcomes.

No network, no order code, no live-loop side effects.
"""
from __future__ import annotations

from bisect import bisect_left
import json
import random
from typing import Any, Iterable

from sniper.algos.polymarket_crypto5m import maker_regime_eval as EV
from sniper.algos.polymarket_crypto5m import window_bootstrap as WB


def empty_report(campaign: str | None = None) -> dict:
    return {
        "available": False,
        "campaign": campaign,
        "horizons_s": [],
        "samples": {"n_fills": 0, "n_quotes": 0, "n_context_profiles": 0},
        "fill_evaluations": [],
        "by_context_profile": [],
        "by_regime": [],
        "recommendations": [],
        "campaign_eval": _empty_campaign_eval(campaign),
    }


def build_maker_regime_report(conn, campaign: str | None, *, horizons_s: Iterable[int] = (3, 10, 30),
                              min_windows: int = 20, min_episodes: int = 30, min_fills: int = 50,
                              lookback_fills: int = 1500, campaign_min_windows: int = 200,
                              bootstrap_samples: int | None = None) -> dict:
    if not campaign or not _table_exists(conn, "maker_fills"):
        return empty_report(campaign)
    campaign_eval = _campaign_eval(
        conn, campaign, min_windows=campaign_min_windows, bootstrap_samples=bootstrap_samples,
    )
    horizons = tuple(int(h) for h in horizons_s)
    fills = conn.execute(
        "SELECT * FROM maker_fills WHERE campaign_id=? ORDER BY ts DESC, id DESC LIMIT ?",
        (campaign, int(lookback_fills)),
    ).fetchall()
    if not fills:
        out = empty_report(campaign)
        out["campaign_eval"] = campaign_eval
        return out

    cids = sorted({r["condition_id"] for r in fills if r["condition_id"]})
    windows = _load_windows(conn, campaign, cids)
    quotes = _load_quotes(conn, campaign, cids)
    quote_index = _index_quotes(quotes)
    quote_counts = _quote_counts(quotes)

    groups: dict[tuple[str, str, str], dict] = {}
    fill_evals = []
    for fill in fills:
        fe = _evaluate_fill(fill, windows.get(fill["condition_id"]), quote_index, horizons)
        fill_evals.append(fe)
        key = _group_key(fill)
        g = groups.setdefault(key, _new_group(fill))
        _add_fill(g, fill, fe)

    for key, counts in quote_counts.items():
        g = groups.setdefault(key, _new_group_from_key(key))
        g["n_quotes"] += counts["n_quotes"]
        g["quotes_posted"] += counts["quotes_posted"]
        g["quotes_pulled"] += counts["quotes_pulled"]

    rows = [_finalize_group(g, min_windows=min_windows, min_episodes=min_episodes, min_fills=min_fills)
            for g in groups.values()]
    rows.sort(key=lambda r: (
        r["profile_id"] != "unknown",
        r["moment_key"] != "{}",
        r["sample_ok"],
        r["n_fills"],
        abs(r.get("mean_adverse_selection_10s_bps") or 0),
    ), reverse=True)
    by_regime = _by_regime(rows)
    recommendations = [
        {
            "moment_key": r["moment_key"],
            "regime_id": r["regime_id"],
            "profile_id": r["profile_id"],
            "status": r["status"],
            "recommendation": r["recommendation"],
            "reason": r["recommendation_reason"],
        }
        for r in rows if r["recommendation"] in ("disable_profile_in_context", "promote_profile_in_context")
    ]
    return {
        "available": bool(rows),
        "campaign": campaign,
        "horizons_s": list(horizons),
        "samples": {
            "n_fills": len(fills),
            "n_quotes": len(quotes),
            "n_context_profiles": len(rows),
            "n_evaluable_10s": sum(1 for f in fill_evals if f.get("adverse_selection_10s_bps") is not None),
        },
        "fill_evaluations": fill_evals,
        "by_context_profile": rows,
        "by_regime": by_regime,
        "recommendations": recommendations,
        "campaign_eval": campaign_eval,
    }


def _empty_campaign_eval(campaign: str | None = None) -> dict:
    return {
        "campaign": campaign,
        "n_settled": 0,
        "total_pnl": 0.0,
        "avg_window_pnl": None,
        "ci_low": None,
        "ci_high": None,
        "eval_ready": False,
        "sample_gate": {"min_windows": 200},
        "status": "insufficient_sample",
        "recommendation": "keep_collecting",
        "reason": "no settled windows",
    }


def _campaign_eval(conn, campaign: str, *, min_windows: int, bootstrap_samples: int | None = None) -> dict:
    out = _empty_campaign_eval(campaign)
    out["sample_gate"] = {"min_windows": int(min_windows)}
    if not _table_exists(conn, "maker_windows"):
        return out
    rows = conn.execute(
        "SELECT * FROM maker_windows "
        "WHERE campaign_id=? AND settled=1 AND realized_pnl IS NOT NULL "
        "ORDER BY window_start_ts",
        (campaign,),
    ).fetchall()
    values = [
        _window_realized_pnl(conn, campaign, r)
        for r in rows
        if r["realized_pnl"] is not None
    ]
    n = len(values)
    if not n:
        return out
    by_window = {
        int(r["window_start_ts"]): [_window_realized_pnl(conn, campaign, r)]
        for r in rows
        if r["realized_pnl"] is not None
    }
    nb = WB.BOOT_N if bootstrap_samples is None else int(bootstrap_samples)
    means = WB.boot_means(by_window, random.Random(WB.BOOT_SEED), nb=nb)
    total = sum(values)
    ci_low = WB.pctile(means, 0.025)
    ci_high = WB.pctile(means, 0.975)
    ready = n >= int(min_windows)
    status = "insufficient_sample"
    recommendation = "keep_collecting"
    reason = "below settled-window sample gate"
    if ready and ci_high is not None and ci_high < 0:
        status = "kill_recommended"
        recommendation = "kill_campaign"
        reason = "window-clustered CI upper bound is below zero"
    elif ready and ci_low is not None and ci_low > 0:
        status = "candidate_good"
        recommendation = "keep_running"
        reason = "window-clustered CI lower bound is above zero"
    elif ready:
        status = "needs_review"
        recommendation = "keep_collecting"
        reason = "window-clustered CI overlaps zero"
    return {
        "campaign": campaign,
        "n_settled": n,
        "total_pnl": round(total, 4),
        "avg_window_pnl": round(total / n, 4),
        "ci_low": _round_or_none(ci_low),
        "ci_high": _round_or_none(ci_high),
        "eval_ready": ready,
        "sample_gate": {"min_windows": int(min_windows)},
        "status": status,
        "recommendation": recommendation,
        "reason": reason,
    }


def _window_realized_pnl(conn, campaign: str, row) -> float:
    ledger = _ledger_state(conn, campaign, row["condition_id"])
    winner = str(row["winning_outcome"] or "").lower()
    if ledger is not None and winner in ("up", "down"):
        payout = ledger["up_inv"] if winner == "up" else ledger["down_inv"]
        return float(ledger["cash"] + payout)
    return float(row["realized_pnl"] or 0.0)


def _ledger_state(conn, campaign: str, condition_id: str) -> dict | None:
    if not _table_exists(conn, "maker_fills"):
        return None
    rows = conn.execute(
        "SELECT token, side, price, size FROM maker_fills "
        "WHERE campaign_id=? AND condition_id=? ORDER BY id",
        (campaign, condition_id),
    ).fetchall()
    if not rows:
        return None
    up_inv = down_inv = cash = 0.0
    for row in rows:
        token = str(row["token"] or "").lower()
        side = str(row["side"] or "").lower()
        price = float(row["price"] or 0.0)
        size = float(row["size"] or 0.0)
        if side == "bid":
            cash -= price * size
            if token == "up":
                up_inv += size
            elif token == "down":
                down_inv += size
        elif side == "ask":
            if token == "up":
                sold = min(size, max(0.0, up_inv))
                cash += price * sold
                up_inv -= sold
            elif token == "down":
                sold = min(size, max(0.0, down_inv))
                cash += price * sold
                down_inv -= sold
    return {"up_inv": up_inv, "down_inv": down_inv, "cash": cash}


def _evaluate_fill(fill, window, quote_index: dict, horizons: tuple[int, ...]) -> dict:
    future_books = []
    out = {
        "fill_id": fill["id"],
        "condition_id": fill["condition_id"],
        "window_start_ts": fill["window_start_ts"],
        "token": fill["token"],
        "side": fill["side"],
        "regime_id": _regime(fill),
        "profile_id": _profile(fill),
        "edge_bps": _edge_bps(fill),
        "settled_fill_pnl": _settled_fill_pnl(fill, window),
    }
    rows, ts_values = quote_index.get((fill["condition_id"], fill["token"]), ([], []))
    for horizon in horizons:
        book = _future_book(rows, ts_values, float(fill["ts"] or 0) + horizon)
        future_books.append(book)
        out[f"adverse_selection_{horizon}s_bps"] = EV.adverse_selection_bps(fill, book) if book else None
        unwind = EV.future_unwind_value(fill, book) if book else None
        out[f"unwind_{horizon}s"] = unwind
        out[f"unwind_{horizon}s_bps"] = _value_bps(unwind, fill)
    mfe, mae = EV.mfe_mae_bps(fill, [b for b in future_books if b])
    out["mfe_bps"] = mfe
    out["mae_bps"] = mae
    return out


def _future_book(rows, ts_values, target_ts: float) -> dict | None:
    if not rows:
        return None
    idx = bisect_left(ts_values, target_ts)
    if idx >= len(rows):
        return None
    row = rows[idx]
    return {"bid": row["best_bid"], "ask": row["best_ask"]}


def _new_group(fill) -> dict:
    moment = _moment_key(fill)
    return {
        "moment_key": moment,
        "moment_key_label": _moment_label(moment),
        "regime_id": _regime(fill),
        "profile_id": _profile(fill),
        "profile_mode": fill["profile_mode"] if "profile_mode" in fill.keys() else None,
        "windows": set(),
        "episodes": set(),
        "fill_models": {},
        "edge_bps": [],
        "adv_10s": [],
        "unwind_10s_bps": [],
        "settled_fill_pnl": [],
        "window_fill_counts": {},
        "n_fills": 0,
        "n_quotes": 0,
        "quotes_posted": 0,
        "quotes_pulled": 0,
        "hard_cuts": 0,
    }


def _new_group_from_key(key: tuple[str, str, str]) -> dict:
    moment, regime, profile = key
    return {
        **_new_group({
            "moment_key": moment, "regime_id": regime, "profile_id": profile,
            "profile_mode": None, "window_start_ts": None, "episode_id": None, "fill_model": None,
        }),
        "moment_key": moment,
        "moment_key_label": _moment_label(moment),
        "regime_id": regime,
        "profile_id": profile,
    }


def _add_fill(group: dict, fill, fe: dict) -> None:
    group["n_fills"] += 1
    if fill["window_start_ts"] is not None:
        group["windows"].add(fill["window_start_ts"])
        group["window_fill_counts"][fill["window_start_ts"]] = group["window_fill_counts"].get(fill["window_start_ts"], 0) + 1
    if _get(fill, "episode_id"):
        group["episodes"].add(_get(fill, "episode_id"))
    model = fill["fill_model"] or "untagged"
    group["fill_models"][model] = group["fill_models"].get(model, 0) + 1
    if model == "hard-cut":
        group["hard_cuts"] += 1
    _append_if_number(group["edge_bps"], fe.get("edge_bps"))
    _append_if_number(group["adv_10s"], fe.get("adverse_selection_10s_bps"))
    _append_if_number(group["unwind_10s_bps"], fe.get("unwind_10s_bps"))
    _append_if_number(group["settled_fill_pnl"], fe.get("settled_fill_pnl"))


def _finalize_group(group: dict, *, min_windows: int, min_episodes: int, min_fills: int) -> dict:
    n_windows = len(group["windows"])
    n_episodes = len(group["episodes"])
    n_fills = int(group["n_fills"])
    max_window_share = (
        max(group["window_fill_counts"].values()) / n_fills
        if n_fills and group["window_fill_counts"] else 0.0
    )
    mean_adv = _mean(group["adv_10s"])
    mean_pnl = _mean(group["settled_fill_pnl"])
    sample_ok = n_windows >= min_windows and n_episodes >= min_episodes and n_fills >= min_fills
    status, recommendation, reason = _status(sample_ok, mean_adv, mean_pnl)
    return {
        "moment_key": group["moment_key"],
        "moment_key_label": group["moment_key_label"],
        "regime_id": group["regime_id"],
        "profile_id": group["profile_id"],
        "profile_mode": group["profile_mode"],
        "n_windows": n_windows,
        "n_episodes": n_episodes,
        "n_quotes": int(group["n_quotes"]),
        "quotes_posted": int(group["quotes_posted"]),
        "quotes_pulled": int(group["quotes_pulled"]),
        "n_fills": n_fills,
        "hard_cuts": int(group["hard_cuts"]),
        "fill_models": group["fill_models"],
        "mean_edge_bps": _round_or_none(_mean(group["edge_bps"])),
        "mean_adverse_selection_10s_bps": _round_or_none(mean_adv),
        "mean_executable_markout_10s_bps": _round_or_none(_mean(group["unwind_10s_bps"])),
        "mean_settled_fill_pnl": _round_or_none(mean_pnl),
        "max_window_fill_share": round(max_window_share, 4),
        "sample_ok": sample_ok,
        "sample_gate": {"min_windows": min_windows, "min_episodes": min_episodes, "min_fills": min_fills},
        "status": status,
        "recommendation": recommendation,
        "recommendation_reason": reason,
    }


def _status(sample_ok: bool, mean_adv: float | None, mean_pnl: float | None) -> tuple[str, str, str]:
    if not sample_ok:
        return "insufficient_sample", "keep_collecting", "below context/profile sample gate"
    if mean_adv is not None and mean_adv <= -50 and mean_pnl is not None and mean_pnl < 0:
        return "disabled_recommended", "disable_profile_in_context", "sampled negative markout and losing settled fills"
    if mean_adv is not None and mean_adv >= 50 and mean_pnl is not None and mean_pnl > 0:
        return "promote_recommended", "promote_profile_in_context", "sampled positive markout and winning settled fills"
    if (mean_adv is not None and mean_adv < -25) or (mean_pnl is not None and mean_pnl < 0):
        return "candidate_bad", "needs_review", "negative markout or settlement PnL, but promotion gate not decisive"
    if (mean_adv is not None and mean_adv > 0) and (mean_pnl is None or mean_pnl >= 0):
        return "candidate_good", "needs_review", "positive markout, still needs more confirmation"
    return "needs_review", "needs_review", "mixed or weak evidence"


def _by_regime(rows: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for row in rows:
        r = out.setdefault(row["regime_id"], {
            "regime_id": row["regime_id"],
            "n_context_profiles": 0,
            "n_fills": 0,
            "n_quotes": 0,
            "hard_cuts": 0,
            "statuses": {},
        })
        r["n_context_profiles"] += 1
        r["n_fills"] += row["n_fills"]
        r["n_quotes"] += row["n_quotes"]
        r["hard_cuts"] += row["hard_cuts"]
        r["statuses"][row["status"]] = r["statuses"].get(row["status"], 0) + 1
    return sorted(out.values(), key=lambda r: (r["n_fills"], r["n_quotes"]), reverse=True)


def _load_windows(conn, campaign: str, cids: list[str]) -> dict:
    if not cids or not _table_exists(conn, "maker_windows"):
        return {}
    ph = ",".join("?" for _ in cids)
    rows = conn.execute(
        f"SELECT * FROM maker_windows WHERE campaign_id=? AND condition_id IN ({ph})",
        [campaign, *cids],
    ).fetchall()
    return {r["condition_id"]: r for r in rows}


def _load_quotes(conn, campaign: str, cids: list[str]) -> list:
    if not cids or not _table_exists(conn, "maker_quotes"):
        return []
    ph = ",".join("?" for _ in cids)
    return conn.execute(
        f"SELECT * FROM maker_quotes WHERE campaign_id=? AND condition_id IN ({ph}) ORDER BY condition_id, token, ts",
        [campaign, *cids],
    ).fetchall()


def _index_quotes(quotes: list) -> dict:
    out: dict[tuple[str, str], tuple[list, list]] = {}
    tmp: dict[tuple[str, str], list] = {}
    for row in quotes:
        tmp.setdefault((row["condition_id"], row["token"]), []).append(row)
    for key, rows in tmp.items():
        rows.sort(key=lambda r: float(r["ts"] or 0))
        out[key] = (rows, [float(r["ts"] or 0) for r in rows])
    return out


def _quote_counts(quotes: list) -> dict:
    out: dict[tuple[str, str, str], dict] = {}
    for q in quotes:
        key = _group_key(q)
        row = out.setdefault(key, {"n_quotes": 0, "quotes_posted": 0, "quotes_pulled": 0})
        row["n_quotes"] += 1
        if q["status"] == "posted":
            row["quotes_posted"] += 1
        else:
            row["quotes_pulled"] += 1
    return out


def _group_key(row) -> tuple[str, str, str]:
    return (_moment_key(row), _regime(row), _profile(row))


def _moment_key(row) -> str:
    raw = _get(row, "moment_key")
    return raw if raw else "{}"


def _moment_label(raw: str) -> str:
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return str(raw or "?")
    parts = [
        data.get("window_phase"),
        data.get("odds_bucket"),
        data.get("volatility_state"),
        data.get("inventory_state"),
        data.get("direction_state"),
    ]
    return " / ".join(str(p) for p in parts if p) or "unknown"


def _regime(row) -> str:
    return _get(row, "fill_regime_id") or _get(row, "regime_id") or _get(row, "quote_regime_id") or "unknown"


def _profile(row) -> str:
    return _get(row, "fill_profile_id") or _get(row, "profile_id") or _get(row, "quote_profile_id") or "unknown"


def _edge_bps(fill) -> float | None:
    fair = _float_or_none(_get(fill, "fair"))
    price = _float_or_none(_get(fill, "price"))
    if fair is None or price is None:
        return None
    edge = (fair - price) if str(_get(fill, "side") or "").lower() == "bid" else (price - fair)
    return round(edge * 10_000, 8)


def _settled_fill_pnl(fill, window) -> float | None:
    if window is None or not window["settled"] or window["realized_pnl"] is None:
        return None
    winner = str(window["winning_outcome"] or "").lower()
    if winner not in ("up", "down"):
        return None
    payoff = 1.0 if str(fill["token"] or "").lower() == winner else 0.0
    price = float(fill["price"] or 0.0)
    size = float(fill["size"] or 0.0)
    if str(fill["side"] or "").lower() == "bid":
        return round((payoff - price) * size, 8)
    return round((price - payoff) * size, 8)


def _value_bps(value: float | None, fill) -> float | None:
    if value is None:
        return None
    size = _float_or_none(_get(fill, "size"))
    if not size:
        return None
    return round(value / size * 10_000, 8)


def _append_if_number(target: list, value: Any) -> None:
    if isinstance(value, (int, float)):
        target.append(float(value))


def _mean(values: list[float]) -> float | None:
    return None if not values else sum(values) / len(values)


def _round_or_none(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        if key in row.keys():
            return row[key]
    except AttributeError:
        pass
    except (KeyError, IndexError, TypeError):
        return None
    return None


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
