"""Pure causal evidence functions for the HL alt-short paper program."""
from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
import random
import statistics

from sniper.hyperlive.altshort_universe import normalize_coin, strategy_bucket


@dataclass(frozen=True)
class CostModel:
    round_trip_bps: float = 0.0


def _valid_price(value) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _price_at_or_before(bars: list[dict], ts_ms: int, field: str = "close") -> float | None:
    eligible = [b for b in bars if int(b["ts_ms"]) <= ts_ms]
    if not eligible:
        return None
    row = max(eligible, key=lambda b: int(b["ts_ms"]))
    return _valid_price(row.get(field))


def _price_strictly_after(bars: list[dict], ts_ms: int, field: str) -> tuple[int, float] | None:
    eligible = [b for b in bars if int(b["ts_ms"]) > ts_ms]
    if not eligible:
        return None
    row = min(eligible, key=lambda b: int(b["ts_ms"]))
    price = _valid_price(row.get(field))
    return (int(row["ts_ms"]), price) if price is not None else None


def _short_bps(entry: float, exit_: float) -> float:
    return (entry - exit_) / entry * 10_000.0


def _short_funding_bps(rows: list[dict], entry_ts_ms: int, exit_ts_ms: int) -> float:
    total = 0.0
    for row in rows:
        try:
            ts_ms = int(row.get("time", row.get("ts_ms")))
            rate = float(row["rate"])
        except (KeyError, TypeError, ValueError):
            continue
        if entry_ts_ms < ts_ms <= exit_ts_ms:
            total += rate * 10_000.0
    return total


def _eligible_alts(universe) -> list[str]:
    normalized = {normalize_coin(coin) for coin in universe}
    return sorted(coin for coin in normalized if coin and strategy_bucket(coin) == "alt")


def causal_regime_features(
    bars_by_coin: dict[str, list[dict]],
    *,
    decision_ts_ms: int,
    universe,
    lookbacks_ms: tuple[int, ...] = (86_400_000, 259_200_000, 604_800_000, 1_209_600_000),
) -> dict:
    """Point-in-time cross-sectional features using bars closed by ``decision_ts_ms``."""
    normalized_bars = {normalize_coin(k): v for k, v in bars_by_coin.items()}
    eligible = _eligible_alts(universe)
    current = {
        coin: _price_at_or_before(normalized_bars.get(coin, []), decision_ts_ms)
        for coin in eligible
    }
    covered = sum(price is not None for price in current.values())
    basket_returns: dict[str, float | None] = {}
    medians: dict[str, float | None] = {}
    breadth: dict[str, float | None] = {}
    spreads: dict[str, float | None] = {}
    btc_now = _price_at_or_before(normalized_bars.get("BTC", []), decision_ts_ms)
    for lookback in lookbacks_ms:
        key = str(int(lookback))
        vals = []
        for coin in eligible:
            now = current[coin]
            anchor = _price_at_or_before(
                normalized_bars.get(coin, []), decision_ts_ms - int(lookback)
            )
            if now is not None and anchor is not None:
                vals.append((now - anchor) / anchor * 10_000.0)
        basket = statistics.fmean(vals) if vals else None
        basket_returns[key] = basket
        medians[key] = statistics.median(vals) if vals else None
        breadth[key] = sum(value < 0 for value in vals) / len(vals) if vals else None
        btc_anchor = _price_at_or_before(
            normalized_bars.get("BTC", []), decision_ts_ms - int(lookback)
        )
        btc_return = (
            (btc_now - btc_anchor) / btc_anchor * 10_000.0
            if btc_now is not None and btc_anchor is not None
            else None
        )
        spreads[key] = basket - btc_return if basket is not None and btc_return is not None else None
    return {
        "decision_ts_ms": int(decision_ts_ms),
        "eligible_n": len(eligible),
        "covered_n": covered,
        "coverage": covered / len(eligible) if eligible else 0.0,
        "basket_return_bps": basket_returns,
        "median_return_bps": medians,
        "breadth_negative": breadth,
        "alt_minus_btc_bps": spreads,
    }


def next_tradeable_price(bars: list[dict], *, decision_ts_ms: int) -> tuple[int, float] | None:
    return _price_strictly_after(bars, decision_ts_ms, "open")


def matched_alt_basket_return(
    trade: dict,
    bars_by_coin: dict[str, list[dict]],
    *,
    universe,
    costs: CostModel,
    min_coverage: float = 0.8,
    funding_by_coin: dict[str, list[dict]] | None = None,
) -> dict:
    coin = normalize_coin(trade.get("coin"))
    normalized_bars = {normalize_coin(k): v for k, v in bars_by_coin.items()}
    normalized_funding = {
        normalize_coin(k): v for k, v in (funding_by_coin or {}).items()
    }
    eligible = [candidate for candidate in _eligible_alts(universe) if candidate != coin]

    def interval_return(candidate: str) -> tuple[float, int, int] | None:
        bars = normalized_bars.get(candidate, [])
        entry = next_tradeable_price(bars, decision_ts_ms=int(trade["decision_ts_ms"]))
        exit_ = _price_strictly_after(bars, int(trade["exit_ts_ms"]), "open")
        if entry is None or exit_ is None:
            return None
        funding = normalized_funding.get(candidate, [])
        gross = _short_bps(entry[1], exit_[1])
        gross += _short_funding_bps(funding, entry[0], exit_[0])
        return gross, entry[0], exit_[0]

    basket_intervals = [
        value for candidate in eligible if (value := interval_return(candidate)) is not None
    ]
    basket_gross = [value[0] for value in basket_intervals]
    coverage = len(basket_gross) / len(eligible) if eligible else 0.0
    base = {
        "eligible_n": len(eligible),
        "covered_n": len(basket_gross),
        "coverage": coverage,
    }
    if coin is None or str(trade.get("side", "")).strip().lower() != "short":
        return {**base, "valid": False, "reason": "not_alt_short", "residual_bps": None}
    if coverage < min_coverage:
        return {
            **base,
            "valid": False,
            "reason": "insufficient_basket_coverage",
            "residual_bps": None,
        }
    signal_interval = interval_return(coin)
    if signal_interval is None:
        return {**base, "valid": False, "reason": "missing_signal_price", "residual_bps": None}
    signal_gross, entry_ts_ms, exit_ts_ms = signal_interval
    basket_net = statistics.fmean(basket_gross) - costs.round_trip_bps
    signal_net = signal_gross - costs.round_trip_bps
    return {
        **base,
        "valid": True,
        "reason": "ok",
        "entry_ts_ms": entry_ts_ms,
        "exit_ts_ms": exit_ts_ms,
        "signal_net_bps": signal_net,
        "basket_net_bps": basket_net,
        "residual_bps": signal_net - basket_net,
    }


def passive_alt_basket(
    bars_by_coin: dict[str, list[dict]],
    *,
    start_ms: int,
    end_ms: int,
    universe,
    costs: CostModel,
    min_coverage: float = 0.8,
    funding_by_coin: dict[str, list[dict]] | None = None,
) -> dict:
    normalized_bars = {normalize_coin(k): v for k, v in bars_by_coin.items()}
    normalized_funding = {
        normalize_coin(k): v for k, v in (funding_by_coin or {}).items()
    }
    eligible = _eligible_alts(universe)
    usable: dict[str, tuple[float, float]] = {}
    for coin in eligible:
        bars = normalized_bars.get(coin, [])
        start = _price_at_or_before(bars, start_ms)
        end = _price_at_or_before(bars, end_ms)
        if start is not None and end is not None:
            usable[coin] = (start, end)
    coverage = len(usable) / len(eligible) if eligible else 0.0
    if coverage < min_coverage:
        return {"valid": False, "reason": "insufficient_basket_coverage", "coverage": coverage}
    timestamps = sorted({
        int(row["ts_ms"])
        for coin in usable
        for row in normalized_bars[coin]
        if start_ms <= int(row["ts_ms"]) <= end_ms
    })
    curve = []
    ordered_bars = {
        coin: sorted(normalized_bars[coin], key=lambda row: int(row["ts_ms"]))
        for coin in usable
    }
    offsets = {coin: 0 for coin in usable}
    latest: dict[str, float] = {}
    for ts_ms in timestamps:
        vals = []
        for coin, (entry, _) in usable.items():
            rows = ordered_bars[coin]
            offset = offsets[coin]
            while offset < len(rows) and int(rows[offset]["ts_ms"]) <= ts_ms:
                mark = _valid_price(rows[offset].get("close"))
                if mark is not None:
                    latest[coin] = mark
                offset += 1
            offsets[coin] = offset
            mark = latest.get(coin)
            if mark is not None:
                vals.append(_short_bps(entry, mark))
        if len(vals) == len(usable):
            curve.append({"ts_ms": ts_ms, "equity_bps": statistics.fmean(vals)})
    net_by_coin = []
    for coin, (entry, exit_) in usable.items():
        value = _short_bps(entry, exit_)
        value += _short_funding_bps(normalized_funding.get(coin, []), start_ms, end_ms)
        net_by_coin.append(value)
    net_bps = statistics.fmean(net_by_coin)
    net_bps -= costs.round_trip_bps
    if curve:
        curve[-1] = {**curve[-1], "equity_bps": curve[-1]["equity_bps"] - costs.round_trip_bps}
    peak = 0.0
    max_drawdown = 0.0
    for point in curve:
        peak = max(peak, point["equity_bps"])
        max_drawdown = min(max_drawdown, point["equity_bps"] - peak)
    return {
        "valid": True,
        "reason": "ok",
        "eligible_n": len(eligible),
        "covered_n": len(usable),
        "coverage": coverage,
        "net_bps": net_bps,
        "max_drawdown_bps": max_drawdown,
        "equity_curve": curve,
    }


def _bar_at_or_after(bars: list[dict], ts_ms: int) -> dict | None:
    eligible = [row for row in bars if int(row["ts_ms"]) >= ts_ms]
    return min(eligible, key=lambda row: int(row["ts_ms"])) if eligible else None


def simulate_exit(trade: dict, bars: list[dict], *, rule: dict, costs: CostModel) -> dict:
    entry = next_tradeable_price(bars, decision_ts_ms=int(trade["decision_ts_ms"]))
    if entry is None:
        return {"valid": False, "reason": "missing_entry_price"}
    entry_ts, entry_px = entry
    candidates = sorted(
        (row for row in bars if int(row["ts_ms"]) > entry_ts),
        key=lambda row: int(row["ts_ms"]),
    )
    kind = rule.get("kind")
    selected = None
    reason = None
    if kind == "fixed":
        selected = _bar_at_or_after(candidates, entry_ts + int(rule["horizon_ms"]))
        reason = "time_cap"
    elif kind in {"tp_sl_cap", "trailing"}:
        cap_ts = entry_ts + int(rule["cap_ms"])
        peak_bps = float("-inf")
        armed = False
        for row in candidates:
            if int(row["ts_ms"]) > cap_ts:
                break
            px = _valid_price(row.get("close"))
            if px is None:
                continue
            gross = _short_bps(entry_px, px)
            if kind == "tp_sl_cap":
                if gross <= -float(rule["stop_loss_bps"]):
                    selected, reason = row, "stop_loss"
                    break
                if gross >= float(rule["take_profit_bps"]):
                    selected, reason = row, "take_profit"
                    break
            else:
                peak_bps = max(peak_bps, gross)
                armed = armed or peak_bps >= float(rule["arm_bps"])
                if armed and peak_bps - gross >= float(rule["giveback_bps"]):
                    selected, reason = row, "trailing_giveback"
                    break
        if selected is None:
            selected = _bar_at_or_after(candidates, cap_ts)
            reason = "time_cap"
    else:
        return {"valid": False, "reason": "unknown_exit_rule"}
    if selected is None:
        return {"valid": False, "reason": "missing_exit_price"}
    exit_px = _valid_price(selected.get("close"))
    if exit_px is None:
        return {"valid": False, "reason": "missing_exit_price"}
    gross = _short_bps(entry_px, exit_px)
    return {
        "valid": True,
        "rule_id": rule.get("id"),
        "entry_ts_ms": entry_ts,
        "entry_px": entry_px,
        "exit_ts_ms": int(selected["ts_ms"]),
        "exit_px": exit_px,
        "gross_bps": gross,
        "net_bps": gross - costs.round_trip_bps - float(trade.get("funding_bps") or 0.0),
        "reason": reason,
    }


def _mean_score(scores: dict[str, list[float]], rule_id: str) -> float:
    values = [float(value) for value in scores.get(rule_id, [])]
    return statistics.fmean(values) if values else float("-inf")


def select_exit_rule(train_scores: dict[str, list[float]], validation_scores: dict[str, list[float]],
                     *, candidates: list[dict]) -> dict:
    """Pick parameters on train, then pick between families on validation."""
    by_family: dict[str, list[dict]] = defaultdict(list)
    for candidate in candidates:
        by_family[str(candidate["family"])].append(candidate)
    family_winners = [
        max(group, key=lambda rule: (_mean_score(train_scores, str(rule["id"])), str(rule["id"])))
        for group in by_family.values()
    ]
    if not family_winners:
        raise ValueError("at least one exit candidate is required")
    return dict(max(
        family_winners,
        key=lambda rule: (_mean_score(validation_scores, str(rule["id"])), str(rule["id"])),
    ))


def evaluate_frozen_exit(holdout_scores: dict[str, list[float]], *, rule: dict) -> dict:
    values = [float(value) for value in holdout_scores.get(str(rule["id"]), [])]
    return {
        "rule_id": rule["id"],
        "n": len(values),
        "mean_bps": statistics.fmean(values) if values else None,
    }


def fixed_slot_portfolio(
    trades: list[dict],
    *,
    slots: int,
    initial_equity: float,
    costs: CostModel,
    now_ms: int,
    max_mark_age_ms: int,
) -> dict:
    if slots <= 0 or initial_equity <= 0:
        raise ValueError("slots and initial_equity must be positive")
    slot_notional = initial_equity / slots
    active: list[dict] = []
    accepted: list[dict] = []
    rejected: list[dict] = []
    max_concurrent = 0
    for trade in sorted(trades, key=lambda row: (int(row["entry_ts_ms"]), str(row.get("id")))):
        entry_ts = int(trade["entry_ts_ms"])
        active = [
            row for row in active
            if row.get("status") != "closed" or int(row["exit_ts_ms"]) > entry_ts
        ]
        if len(active) >= slots:
            rejected.append(trade)
            continue
        accepted.append(trade)
        active.append(trade)
        max_concurrent = max(max_concurrent, len(active))

    stale = [
        row.get("id") for row in accepted
        if row.get("status") == "open"
        and (row.get("mark_ts_ms") is None or now_ms - int(row["mark_ts_ms"]) > max_mark_age_ms)
    ]
    if stale:
        return {
            "valid": False,
            "reason": "stale_open_marks",
            "stale_ids": stale,
            "accepted_ids": [row.get("id") for row in accepted],
            "rejected_ids": [row.get("id") for row in rejected],
        }

    def net_bps(row: dict) -> float:
        raw = row.get("gross_pnl_bps") if row.get("status") == "closed" else row.get("unrealized_bps")
        return float(raw or 0.0) - costs.round_trip_bps - float(row.get("funding_bps") or 0.0)

    closed_pnl = sum(
        slot_notional * net_bps(row) / 10_000.0
        for row in accepted if row.get("status") == "closed"
    )
    open_mtm = sum(
        slot_notional * net_bps(row) / 10_000.0
        for row in accepted if row.get("status") == "open"
    )
    events = sorted(
        (
            int(row["exit_ts_ms"]),
            slot_notional * net_bps(row) / 10_000.0,
        )
        for row in accepted if row.get("status") == "closed"
    )
    equity = initial_equity
    peak = initial_equity
    max_drawdown = 0.0
    curve = [{"ts_ms": None, "equity": equity}]
    for ts_ms, pnl in events:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
        curve.append({"ts_ms": ts_ms, "equity": equity})
    total_equity = initial_equity + closed_pnl + open_mtm
    return {
        "valid": True,
        "reason": "ok",
        "accepted_ids": [row.get("id") for row in accepted],
        "rejected_ids": [row.get("id") for row in rejected],
        "accepted_n": len(accepted),
        "rejected_n": len(rejected),
        "max_concurrent": max_concurrent,
        "closed_pnl": closed_pnl,
        "open_mtm": open_mtm,
        "total_equity": total_equity,
        "total_return_bps": (total_equity / initial_equity - 1.0) * 10_000.0,
        "max_drawdown": max_drawdown,
        "equity_curve": curve,
    }


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot take a quantile of no values")
    index = (len(ordered) - 1) * probability
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    fraction = index - low
    return ordered[low] * (1 - fraction) + ordered[high] * fraction


def block_bootstrap_mean(rows: list[dict], *, cluster_key: str, value_key: str = "residual_bps",
                         replications: int = 2000, seed: int = 0) -> dict:
    grouped: dict[object, list[float]] = defaultdict(list)
    for row in rows:
        if row.get(cluster_key) is not None and row.get(value_key) is not None:
            grouped[row[cluster_key]].append(float(row[value_key]))
    keys = sorted(grouped, key=str)
    if not keys:
        return {"clusters": 0, "samples": [], "mean_bps": None, "ci95": [None, None]}
    rng = random.Random(seed)
    samples = []
    for _ in range(replications):
        values = []
        for _ in keys:
            values.extend(grouped[rng.choice(keys)])
        samples.append(statistics.fmean(values))
    observed = [value for key in keys for value in grouped[key]]
    return {
        "clusters": len(keys),
        "samples": samples,
        "mean_bps": statistics.fmean(observed),
        "ci95": [_quantile(samples, 0.025), _quantile(samples, 0.975)],
    }


def concentration_report(rows: list[dict], *, value_key: str = "net_bps") -> dict:
    values = [float(row.get(value_key) or 0.0) for row in rows]
    positive_total = sum(max(0.0, value) for value in values)

    def grouped(field: str) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for row, value in zip(rows, values):
            out[str(row.get(field) or "unknown")] += value
        return dict(out)

    by_coin = grouped("coin")
    by_week = grouped("week")
    top_coin = max(by_coin, key=by_coin.get) if by_coin else None
    top_week = max(by_week, key=by_week.get) if by_week else None
    total = sum(values)
    return {
        "top_coin": top_coin,
        "top_coin_share": max(0.0, by_coin.get(top_coin, 0.0)) / positive_total if positive_total else 0.0,
        "top_week": top_week,
        "top_week_share": max(0.0, by_week.get(top_week, 0.0)) / positive_total if positive_total else 0.0,
        "leave_top_coin_bps": total - by_coin.get(top_coin, 0.0),
        "leave_top_week_bps": total - by_week.get(top_week, 0.0),
    }


def altshort_verdict(evidence: dict, *, min_coverage: float = 0.8,
                     min_day_clusters: int = 20, max_top_coin_share: float = 0.35,
                     max_top_week_share: float = 0.50,
                     min_regime_episodes: int = 2,
                     min_episode_closes: int = 5) -> dict:
    blocked = []
    if not evidence.get("provenance_valid"):
        blocked.append("provenance")
    if not evidence.get("causal_features"):
        blocked.append("causal_features")
    if not evidence.get("next_tradeable_entry"):
        blocked.append("entry_timing")
    if float(evidence.get("coverage") or 0.0) < min_coverage:
        blocked.append("coverage")
    if blocked:
        return {"status": "blocked_missing_feed", "blocked": blocked, "failed": []}

    incomplete = []
    if int(evidence.get("day_clusters") or 0) < min_day_clusters:
        incomplete.append("day_clusters")
    if int(evidence.get("stale_open_marks") or 0) > 0:
        incomplete.append("stale_open_marks")
    if int(evidence.get("regime_episode_count") or 0) < min_regime_episodes:
        incomplete.append("regime_episodes")
    if int(evidence.get("min_episode_closes") or 0) < min_episode_closes:
        incomplete.append("episode_sample")

    if incomplete:
        return {"status": "paper_continue", "blocked": [], "incomplete": incomplete, "failed": []}

    checks = [
        ("residual_ci", float(evidence.get("residual_ci_low_bps") or 0.0) > 0),
        ("fixed_slot", float(evidence.get("fixed_slot_base_bps") or 0.0) > 0),
        ("cost_stress", float(evidence.get("fixed_slot_stress_bps") or 0.0) > 0),
        ("total_book", float(evidence.get("total_book_bps") or 0.0) > 0),
        ("leave_top_coin", float(evidence.get("leave_top_coin_bps") or 0.0) > 0),
        ("leave_top_week", float(evidence.get("leave_top_week_bps") or 0.0) > 0),
        ("top_coin_concentration", float(evidence.get("top_coin_share") or 0.0) < max_top_coin_share),
        ("top_week_concentration", float(evidence.get("top_week_share") or 0.0) < max_top_week_share),
        ("exit_rule", bool(evidence.get("exit_rule_valid"))),
        ("episode_residual", float(evidence.get("min_episode_residual_bps") or 0.0) > 0),
        ("episode_leave_one_out",
         float(evidence.get("leave_one_episode_out_residual_bps") or 0.0) > 0),
    ]
    failed = [name for name, passed in checks if not passed]
    if failed:
        return {"status": "paper_reject", "blocked": [], "incomplete": incomplete, "failed": failed}
    return {"status": "paper_promote", "blocked": [], "incomplete": [], "failed": []}
