from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping

from sniper.analysis.memecoin_thesis import thesis_stance


WINDOWS_MINUTES = (5, 15, 30, 60)


@dataclass(frozen=True)
class ReentrySignal:
    confirmed_at: str
    entry_at: str
    entry_price: float
    post_stop_low: float
    distinct_buyers: tuple[str, ...]
    source_event_ids: tuple[int | str, ...]
    entry_quote_id: int | str


def _time(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("context timestamps must be timezone aware")
    return parsed


def _mint(value) -> str:
    text = str(value or "")
    return text.lower() if text.lower().startswith("0x") else text


def _same_coin(row: Mapping, network_id: str, mint: str) -> bool:
    return str(row.get("network_id") or "") == str(network_id) and _mint(row.get("mint")) == _mint(mint)


def _notional(value) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed > 0 else 0.0


def _activity(rows: list[Mapping]) -> dict:
    buy_counts = Counter()
    sellers = set()
    buy_notional = 0.0
    sell_notional = 0.0
    for row in rows:
        user = str(row.get("source_user_id") or "")
        side = row.get("normalized_side")
        if side == "buy":
            if user:
                buy_counts[user] += 1
            buy_notional += _notional(row.get("their_size_usd"))
        elif side == "sell":
            if user:
                sellers.add(user)
            sell_notional += _notional(row.get("their_size_usd"))
    total = buy_notional + sell_notional
    return {
        "distinct_buyers": len(buy_counts),
        "distinct_sellers": len(sellers),
        "repeat_buyers": sum(count > 1 for count in buy_counts.values()),
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "activity_imbalance": ((buy_notional - sell_notional) / total if total else None),
    }


def conviction_windows(signal_at, rows: Iterable[Mapping], *, lane: str, network_id: str, mint: str) -> dict:
    if lane not in {"legacy", "v2"}:
        raise ValueError("lane must be legacy or v2")
    signal = _time(signal_at)
    prepared = []
    unknown_cohort_rows = 0
    for row in rows:
        if not _same_coin(row, network_id, mint):
            continue
        observed_at = _time(row.get("observed_at"))
        if observed_at < signal:
            continue
        item = dict(row)
        item["_time"] = observed_at
        prepared.append(item)
        if lane == "v2" and row.get("was_curated") is None:
            unknown_cohort_rows += 1
    prepared.sort(key=lambda row: (row["_time"], str(row.get("id") or "")))
    windows = {}
    for minutes in WINDOWS_MINUTES:
        deadline = signal + timedelta(minutes=minutes)
        selected = [row for row in prepared if row["_time"] <= deadline]
        windows[f"{minutes}m"] = {
            "observed": _activity(selected),
            "curated": (
                _activity([row for row in selected if row.get("was_curated") in (True, 1)])
                if lane == "v2" else None
            ),
        }
    return {
        "lane": lane,
        "cohort_claim": "curated_conviction" if lane == "v2" else "observed_activity",
        "unknown_cohort_rows": unknown_cohort_rows,
        "windows": windows,
    }


def _valid_quotes(rows: Iterable[Mapping], network_id: str, mint: str, start: datetime, end: datetime):
    quotes = []
    for ordinal, row in enumerate(rows):
        if not _same_coin(row, network_id, mint) or row.get("quality") != "ok":
            continue
        try:
            observed_at = _time(row.get("observed_at"))
            price = float(row.get("price"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0 or not start <= observed_at <= end:
            continue
        quotes.append((observed_at, price, row.get("id", ordinal)))
    quotes.sort(key=lambda item: (item[0], item[1], str(item[2])))
    return quotes


def thesis_context(
    signal_at,
    theses: Iterable[Mapping],
    feed_rows: Iterable[Mapping],
    quote_rows: Iterable[Mapping],
    *,
    network_id: str,
    mint: str,
    asof,
) -> dict:
    signal = _time(signal_at)
    cutoff = _time(asof)
    if cutoff < signal:
        raise ValueError("asof must not precede signal_at")
    eligible = []
    for row in theses:
        if not _same_coin(row, network_id, mint):
            continue
        observed_at = _time(row.get("observed_at"))
        if observed_at <= cutoff:
            eligible.append((observed_at, str(row.get("id") or ""), row))
    eligible.sort(key=lambda item: (item[0], item[1]))
    if not eligible:
        return {
            "count": 0, "first_at": None, "first_stance": None,
            "lag_from_first_buy_s": None, "lag_from_poster_buy_s": None,
            "forward_return_pct": None, "forward_age_s": None,
        }

    thesis_at, _thesis_id, first = eligible[0]
    poster = str(first.get("source_user_id") or "")
    poster_buys = []
    for row in feed_rows:
        if not _same_coin(row, network_id, mint):
            continue
        if row.get("normalized_side") != "buy" or str(row.get("source_user_id") or "") != poster:
            continue
        observed_at = _time(row.get("observed_at"))
        if observed_at <= thesis_at:
            poster_buys.append(observed_at)
    poster_buy = max(poster_buys) if poster_buys else None
    quotes = _valid_quotes(quote_rows, network_id, mint, thesis_at, cutoff)
    forward_return = None
    forward_age = None
    if quotes:
        reference = quotes[0]
        terminal = quotes[-1]
        forward_return = (terminal[1] / reference[1] - 1.0) * 100.0
        forward_age = (terminal[0] - reference[0]).total_seconds()
    return {
        "count": len(eligible),
        "first_at": thesis_at.isoformat(),
        "first_stance": thesis_stance(first.get("their_pnl_pct")),
        "lag_from_first_buy_s": (thesis_at - signal).total_seconds(),
        "lag_from_poster_buy_s": (
            (thesis_at - poster_buy).total_seconds() if poster_buy is not None else None
        ),
        "forward_return_pct": forward_return,
        "forward_age_s": forward_age,
    }


def find_fresh_conviction_reentry(
    stop_at,
    original_source_user_id,
    feed_rows: Iterable[Mapping],
    quote_rows: Iterable[Mapping],
    *,
    network_id: str,
    mint: str,
    window_s: int = 1800,
    reclaim_pct: float = 20.0,
) -> ReentrySignal | None:
    stop = _time(stop_at)
    if window_s <= 0 or reclaim_pct <= 0:
        raise ValueError("reentry window and reclaim must be positive")
    deadline = stop + timedelta(seconds=window_s)
    buys = []
    for ordinal, row in enumerate(feed_rows):
        if not _same_coin(row, network_id, mint):
            continue
        if row.get("normalized_side") != "buy" or row.get("was_curated") not in (True, 1):
            continue
        user = str(row.get("source_user_id") or "")
        if not user:
            continue
        observed_at = _time(row.get("observed_at"))
        if stop < observed_at <= deadline:
            buys.append((observed_at, str(row.get("id", ordinal)), user, row.get("id", ordinal)))
    buys.sort(key=lambda item: (item[0], item[1]))
    users = set()
    source_ids = []
    confirmed_at = None
    for observed_at, _stable_id, user, source_id in buys:
        users.add(user)
        source_ids.append(source_id)
        if len(users) >= 2 and not (users == {str(original_source_user_id or "")}):
            confirmed_at = observed_at
            break
    if confirmed_at is None:
        return None

    quotes = _valid_quotes(quote_rows, network_id, mint, stop, deadline)
    low = None
    for observed_at, price, quote_id in quotes:
        low = price if low is None else min(low, price)
        if observed_at < confirmed_at:
            continue
        if price >= low * (1.0 + reclaim_pct / 100.0):
            return ReentrySignal(
                confirmed_at=confirmed_at.isoformat(), entry_at=observed_at.isoformat(),
                entry_price=price, post_stop_low=low,
                distinct_buyers=tuple(sorted(users)),
                source_event_ids=tuple(source_ids), entry_quote_id=quote_id,
            )
    return None
