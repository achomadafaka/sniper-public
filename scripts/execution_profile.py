"""Wallet execution profile — Layer 1/3 hybrid observation module.

Per spec `docs/superpowers/specs/2026-04-23-wallet-execution-profile-design.md`.

Phases:
- A (shipped): scaffold + stub + gating.
- B (this commit): per-fill compute helpers (preceding-bar, tape-midpoint,
  drift, in-play + thin-market gates, resolve_ts lookup).
- C: aggregation + calibration signature → status="populated".
- D: Layer 3 mapping (entry_timing_edge promotion on drift evidence).
- E: E2E fixtures + rollout.

Public surface: `compute_execution_profile(wallet, activity, client, ...)`.
Callers: `scripts.wallet_archetype_extractor.extract_wallet_archetype`.

The module is gated by an explicit flag. Never runs in default extraction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from statistics import mean, median, pstdev
from typing import Any, Callable, Iterable, Iterator

logger = logging.getLogger(__name__)

# Layer 1 terminal set mirror — keep in sync with
# scripts/wallet_archetype_extractor.py:_LAYER1_TERMINAL. The spec §7.1 skip
# rule depends on this exact set. multi_outcome_basket_builder is NOT terminal
# (copy_policy="manual_review") and DOES get profiled — basket arbs can have
# real selection/portfolio edges worth measuring.
LAYER1_TERMINAL = frozenset({"paired_outcome_arb", "two_sided_inventory_trader"})

# Gate thresholds from spec §7.1 item 3.
MIN_RESOLVED_FOR_RUN = 10
MIN_BUYS_FOR_RUN = 30

# Per-fill gate thresholds from spec §3 + §6.2.
PRECEDING_BAR_WINDOW_SEC = 1800      # 30-minute lookback for preceding bar
TAPE_PRE_WINDOW_SEC = 300            # 5-minute tape midpoint window
TAPE_CONFIDENCE_MIN = 3              # tape_n >= 3 required for skew contribution
IN_PLAY_THRESHOLD_SEC = 7200         # time_to_resolution < 2h = in-play
DRIFT_HORIZONS = (3600, 21600, 86400)  # 1h, 6h, 24h

# Sample-size tiers from spec §6.4.
SAMPLE_TIER_MEDIUM = 30
SAMPLE_TIER_HIGH = 100

# Calibration thresholds from spec §6.5.
CALIB_SKEW_MEDIAN_MAX = 0.02
CALIB_SKEW_MEAN_MAX = 0.03
CALIB_DRIFT_MEDIAN_MAX = 0.02
CALIB_DRIFT_MIN_N = 10

# Hard per-wallet wall-clock ceiling from spec §6.7.
WALL_CLOCK_CEILING_SEC = 300

# Inter-fill pacing (probe uses 0.25s; same here to stay well inside the
# public rate limit).
FILL_PACE_SEC = 0.25

_GAMMA_MARKET_URL = "https://gamma-api.polymarket.com/markets"


def _stub(reason: str) -> dict[str, Any]:
    """Non-populated execution profile envelope. Spec §5."""
    return {"status": "not_run", "reason": reason}


def _should_run(
    classification: dict[str, Any] | None,
    counts: dict[str, Any] | None,
    returns: dict[str, Any] | None,
    *,
    force: bool,
) -> tuple[bool, str]:
    """Decide whether to run the execution profile. Spec §7.1.

    Returns (run, reason). When run=False, reason is the stub reason.
    When run=True, reason is informational ("ok" or "forced").
    """
    classification = classification or {}
    archetype = str(classification.get("archetype") or "")

    if archetype in LAYER1_TERMINAL and not force:
        return (False, "layer1_terminal")

    counts = counts or {}
    returns = returns or {}
    resolved = int(returns.get("resolved_position_count") or 0)
    buys = int(counts.get("trade_records") or 0)
    if resolved < MIN_RESOLVED_FOR_RUN and buys < MIN_BUYS_FOR_RUN and not force:
        return (False, "insufficient_activity")

    return (True, "forced" if force else "ok")


# -----------------------------------------------------------------------------
# Phase B: reference-price and per-fill helpers
# -----------------------------------------------------------------------------


@dataclass
class FillResult:
    """One BUY fill probed against price-history + tape + future drift.

    Field semantics per spec §6.2. `None` values mean the probe couldn't source
    that reference (stale window, thin tape, drift past resolution, etc.) —
    aggregation treats them as missing, not zero.
    """
    trade_ts: int
    condition_id: str
    asset: str
    title: str
    fill_price: float
    ref_preceding: float | None
    ref_tape: float | None
    tape_n: int
    drift_1h: float | None
    drift_6h: float | None
    drift_24h: float | None
    in_play: bool
    thin_market: bool
    resolution_ts_unknown: bool


def _preceding_bar_record(
    history: list[dict[str, Any]], trade_ts: int
) -> tuple[float | None, int | None]:
    """Like `_preceding_bar` but returns `(price, ts)` of the chosen bar.

    Returns `(None, None)` when no qualifying bar exists in the 30-min lookback
    window.
    """
    best: dict[str, Any] | None = None
    floor = trade_ts - PRECEDING_BAR_WINDOW_SEC
    for point in history:
        ts = point.get("timestamp")
        if ts is None or ts >= trade_ts or ts < floor:
            continue
        if best is None or ts > best["timestamp"]:
            best = point
    if best is None:
        return None, None
    return float(best["price"]), int(best["timestamp"])


def _preceding_bar(history: list[dict[str, Any]], trade_ts: int) -> float | None:
    """Last bar with `timestamp < trade_ts` inside the 30-min lookback window.

    Returns None when no qualifying bar exists (asset has no pre-trade
    activity in the window). Spec §6.2 step 2.
    """
    price, _ts = _preceding_bar_record(history, trade_ts)
    return price


def _bar_at_offset_record(
    history: list[dict[str, Any]], trade_ts: int, offset_sec: int
) -> tuple[float | None, int | None]:
    """Like `_bar_at_offset` but returns `(price, ts)` of the chosen bar."""
    target = trade_ts + offset_sec
    tolerance = max(300, offset_sec // 12)
    best: dict[str, Any] | None = None
    best_gap = tolerance + 1
    for point in history:
        ts = point.get("timestamp")
        if ts is None:
            continue
        gap = abs(ts - target)
        if gap < best_gap:
            best = point
            best_gap = gap
    if best is None:
        return None, None
    return float(best["price"]), int(best["timestamp"])


def _bar_at_offset(
    history: list[dict[str, Any]], trade_ts: int, offset_sec: int
) -> float | None:
    """Closest bar to `trade_ts + offset_sec` within a half-window tolerance.

    Tolerance = `max(300, offset_sec // 12)` per the MVE probe. Returns None
    when the target window has no bar (market resolved, fidelity gap). Spec §6.2
    step 4.
    """
    price, _ts = _bar_at_offset_record(history, trade_ts, offset_sec)
    return price


def _tape_midpoint(
    client: Any,
    condition_id: str,
    asset: str,
    trade_ts: int,
    exclude_wallet: str,
) -> tuple[float | None, int]:
    """Mean BUY price from OTHER wallets on the same asset in [t-5m, t).

    Returns (midpoint, n). n is the count of qualifying foreign trades and is
    useful independent of the midpoint for the thin-market gate. Spec §6.2
    step 3.

    Pagination cap: `max_pages=1` (200 trades in 5 min). On liquid markets
    the first page trivially exceeds TAPE_CONFIDENCE_MIN; on thin markets
    further paging only returns trades on the sibling asset we'd filter
    out anyway. Measured 2026-04-23: cuts SwissMiss-class walls from ~309s
    to ~60s with zero information loss on the thin-market gate.
    """
    start = trade_ts - TAPE_PRE_WINDOW_SEC
    end = trade_ts - 1
    trades = client.get_trades_window(condition_id, start, end, page_size=200, max_pages=1)
    exclude = (exclude_wallet or "").lower()
    prices: list[float] = []
    for trade in trades:
        if str(trade.get("asset") or "") != asset:
            continue
        if str(trade.get("proxyWallet") or "").lower() == exclude:
            continue
        if str(trade.get("side") or "").upper() != "BUY":
            continue
        raw = trade.get("price")
        if raw is None:
            continue
        try:
            prices.append(float(raw))
        except (TypeError, ValueError):
            continue
    if not prices:
        return None, 0
    return mean(prices), len(prices)


# -----------------------------------------------------------------------------
# resolve_ts lookup (market_metadata.end_date_iso → gamma fallback → unknown)
# -----------------------------------------------------------------------------


def _parse_iso_to_ts(value: Any) -> int | None:
    """Parse an ISO-8601 date or datetime string into a unix timestamp (UTC).

    Bare dates like '2026-07-31' are bumped to 23:59:59 UTC of that day —
    aligns with Polymarket market resolution semantics where `end_date_iso`
    is the calendar day of resolution. Returns None on any parse failure.
    Defensive; callers treat None as resolution_ts_unknown.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Bare-date detection before fromisoformat (which would happily accept
    # 'YYYY-MM-DD' but as midnight-naive — we want end-of-day).
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            d = datetime.strptime(s, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
            return int(d.timestamp())
        except ValueError:
            return None
    # Normalise common Polymarket variants.
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _resolve_ts_from_sqlite(
    condition_id: str, *, db_path: str | None = None
) -> int | None:
    """Lookup `market_metadata.end_date_iso` for the condition. Spec §6.2 step 5.

    Returns None on any failure (missing row, unparseable date, DB error).
    Caller chains to gamma fallback.
    """
    if not condition_id:
        return None
    try:
        from db import get_db_path  # lazy — keep module importable in tests
        path = db_path or get_db_path()
        conn = sqlite3.connect(path, timeout=5)
        try:
            row = conn.execute(
                "SELECT end_date_iso FROM market_metadata WHERE condition_id = ?",
                (condition_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row or row[0] is None:
        return None
    return _parse_iso_to_ts(row[0])


def _resolve_ts_from_gamma(condition_id: str) -> int | None:
    """Fallback: hit gamma-api.polymarket.com/markets?conditionId=<cid>.

    Returns None on any HTTP/parse failure. One call per cache miss. Caller
    is responsible for caching.
    """
    if not condition_id:
        return None
    qs = urllib.parse.urlencode({"conditionId": condition_id})
    url = f"{_GAMMA_MARKET_URL}?{qs}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sniper-exec-profile/1"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    # gamma returns either a list of markets or {data: [...]} depending on
    # endpoint version. Handle both defensively.
    rows = data if isinstance(data, list) else (data or {}).get("data") or []
    for row in rows:
        end = row.get("endDate") or row.get("end_date") or row.get("end_date_iso")
        ts = _parse_iso_to_ts(end)
        if ts is not None:
            return ts
    return None


_GAMMA_FALLBACK_SENTINEL = object()


def _resolve_ts_lookup(
    condition_id: str,
    *,
    db_path: str | None = None,
    gamma_fallback: Callable[[str], int | None] | None | object = _GAMMA_FALLBACK_SENTINEL,
    cache: dict[str, int | None] | None = None,
) -> int | None:
    """Lookup resolution timestamp for a market. Spec §6.2 step 5.

    Priority:
      1. `market_metadata.end_date_iso` in local SQLite (fast, mostly covers
         markets the system has already seen).
      2. Gamma API fallback (one network call per cache miss).
      3. None (caller treats as resolution_ts_unknown=true; conservative
         in-play gate keeps the fill).

    Callers supply `cache` (dict) to amortise repeated lookups within a
    wallet run.

    `gamma_fallback` defaults to looking up `_resolve_ts_from_gamma` on the
    module at call time so that test monkey-patches via
    `monkeypatch.setattr("scripts.execution_profile._resolve_ts_from_gamma", ...)`
    actually take effect. Pass `None` to disable the gamma fallback
    explicitly. (Codex PR #6 review item 3 — earlier behaviour captured
    `_resolve_ts_from_gamma` as a default arg, so monkey-patches at the
    module level were silently bypassed and tests that thought they had
    disabled gamma were actually still hitting the live network.)
    """
    if cache is not None and condition_id in cache:
        return cache[condition_id]

    ts = _resolve_ts_from_sqlite(condition_id, db_path=db_path)
    if ts is None:
        if gamma_fallback is _GAMMA_FALLBACK_SENTINEL:
            # Look up at call time so module-level monkey-patches apply.
            fallback = globals().get("_resolve_ts_from_gamma")
        else:
            fallback = gamma_fallback
        if fallback is not None:
            ts = fallback(condition_id)

    if cache is not None:
        cache[condition_id] = ts
    return ts


# -----------------------------------------------------------------------------
# Per-fill probe
# -----------------------------------------------------------------------------


def _probe_fill(
    client: Any,
    wallet: str,
    trade_ts: int,
    condition_id: str,
    asset: str,
    title: str,
    fill_price: float,
    history: list[dict[str, Any]],
    resolve_ts: int | None,
) -> FillResult:
    """Build a FillResult for one BUY fill. Spec §6.2.

    `history` is a pre-fetched prices-history series for the asset covering
    at least `[trade_ts-1800, trade_ts+86400+300]`. Batching is the caller's
    responsibility (one get_prices_series call per distinct asset per wallet).

    `resolve_ts` is the market resolution unix timestamp, or None for
    resolution_ts_unknown. Conservative: when unknown, `in_play=False`
    (keep the fill for skew) — spec §9.4 treats time_to_resolution as
    infinite in that case.
    """
    ref_prec = _preceding_bar(history, trade_ts)
    drift_1h = _bar_at_offset(history, trade_ts, DRIFT_HORIZONS[0])
    drift_6h = _bar_at_offset(history, trade_ts, DRIFT_HORIZONS[1])
    drift_24h = _bar_at_offset(history, trade_ts, DRIFT_HORIZONS[2])
    ref_tape, tape_n = _tape_midpoint(client, condition_id, asset, trade_ts, wallet)

    # Adaptive drift: if the target horizon overruns resolution (less a 300s
    # buffer), return None so we don't measure a post-resolution flat line.
    def _adaptive(bar: float | None, horizon: int) -> float | None:
        if bar is None:
            return None
        if resolve_ts is not None and horizon > (resolve_ts - trade_ts) - 300:
            return None
        return bar - fill_price

    in_play = False
    resolution_unknown = resolve_ts is None
    if resolve_ts is not None and (resolve_ts - trade_ts) < IN_PLAY_THRESHOLD_SEC:
        in_play = True

    thin_market = tape_n < TAPE_CONFIDENCE_MIN

    return FillResult(
        trade_ts=trade_ts,
        condition_id=condition_id,
        asset=asset,
        title=title,
        fill_price=fill_price,
        ref_preceding=ref_prec,
        ref_tape=ref_tape,
        tape_n=tape_n,
        drift_1h=_adaptive(drift_1h, DRIFT_HORIZONS[0]),
        drift_6h=_adaptive(drift_6h, DRIFT_HORIZONS[1]),
        drift_24h=_adaptive(drift_24h, DRIFT_HORIZONS[2]),
        in_play=in_play,
        thin_market=thin_market,
        resolution_ts_unknown=resolution_unknown,
    )


# -----------------------------------------------------------------------------
# Phase C: aggregation + calibration + populated envelope
# -----------------------------------------------------------------------------


def _dist_summary(values: list[float]) -> dict[str, Any]:
    """Distribution summary matching the MVE probe output schema.

    `n=0` returns a zero/null-filled dict so the schema stays stable regardless
    of whether the gate dropped all samples.
    """
    clean = [v for v in values if v is not None]
    if not clean:
        return {"n": 0, "mean": None, "median": None, "stdev": None, "min": None, "max": None}
    return {
        "n": len(clean),
        "mean": round(mean(clean), 5),
        "median": round(median(clean), 5),
        "stdev": round(pstdev(clean), 5) if len(clean) >= 2 else 0.0,
        "min": round(min(clean), 5),
        "max": round(max(clean), 5),
    }


def _confidence_tier(n_skew: int, n_drift_1h: int) -> str:
    """Sample-size tier per spec §6.4.

    n_eff = min(n_skew, n_drift_1h) if both > 0 else max(n_skew, n_drift_1h).
    """
    if n_skew > 0 and n_drift_1h > 0:
        n_eff = min(n_skew, n_drift_1h)
    else:
        n_eff = max(n_skew, n_drift_1h)
    if n_eff >= SAMPLE_TIER_HIGH:
        return "high"
    if n_eff >= SAMPLE_TIER_MEDIUM:
        return "medium"
    return "unknown"


def _calibration_signature(
    skew: dict[str, Any], drift_1h: dict[str, Any]
) -> dict[str, Any]:
    """Per spec §6.5. `drift_near_zero` is None when drift_1h sample too small."""
    central = None
    if skew.get("n", 0) > 0:
        med = abs(skew.get("median") or 0.0)
        mn = abs(skew.get("mean") or 0.0)
        central = med <= CALIB_SKEW_MEDIAN_MAX and mn <= CALIB_SKEW_MEAN_MAX

    drift_flag: bool | None
    n_drift = drift_1h.get("n", 0) or 0
    if n_drift >= CALIB_DRIFT_MIN_N:
        dmed = abs(drift_1h.get("median") or 0.0)
        drift_flag = dmed <= CALIB_DRIFT_MEDIAN_MAX
    else:
        drift_flag = None

    return {
        "central_tendency_near_zero": central,
        "drift_near_zero": drift_flag,
    }


def _aggregate(fills: list[FillResult]) -> dict[str, Any]:
    """Aggregate FillResult list into the populated envelope per spec §5–§6.

    Gating rules per spec §6.3:
    - skew_vs_preceding_bar excludes fills where in_play==True OR thin_market==True
    - skew_vs_tape_5min requires tape_n >= TAPE_CONFIDENCE_MIN
    - drift_Xh keeps all fills with non-null drift_Xh, including in_play fills
    """
    n_total = len(fills)
    if n_total == 0:
        return {"status": "insufficient_sample", "reason": "no_qualifying_buys"}

    skew_prec_vals = [
        f.fill_price - f.ref_preceding
        for f in fills
        if not f.in_play
        and not f.thin_market
        and f.ref_preceding is not None
    ]
    skew_tape_vals = [
        f.fill_price - f.ref_tape
        for f in fills
        if f.tape_n >= TAPE_CONFIDENCE_MIN and f.ref_tape is not None
    ]
    drift_1h_vals = [f.drift_1h for f in fills if f.drift_1h is not None]
    drift_6h_vals = [f.drift_6h for f in fills if f.drift_6h is not None]
    drift_24h_vals = [f.drift_24h for f in fills if f.drift_24h is not None]

    # Per-fill condition_ids parallel to the drift value lists. Used by H4
    # cluster-adjusted bootstrap (M6 closure of wallet-protocol-bulletproofing).
    # Fills clustering into few markets (eatpraylove: 73/100 fills at 2 distinct
    # drifts) make per-fill bootstrap CIs ~20x too narrow; H4 resamples by
    # condition_id instead.
    drift_1h_clusters = [f.condition_id for f in fills if f.drift_1h is not None]
    drift_6h_clusters = [f.condition_id for f in fills if f.drift_6h is not None]
    drift_24h_clusters = [f.condition_id for f in fills if f.drift_24h is not None]

    skew_prec = _dist_summary(skew_prec_vals)
    skew_tape = _dist_summary(skew_tape_vals)
    drift_1h = _dist_summary(drift_1h_vals)
    drift_6h = _dist_summary(drift_6h_vals)
    drift_24h = _dist_summary(drift_24h_vals)

    coverage = {
        "preceding_bar": round(
            sum(1 for f in fills if f.ref_preceding is not None) / n_total, 3
        ),
        "tape_5min": round(
            sum(1 for f in fills if f.ref_tape is not None) / n_total, 3
        ),
        "drift_1h": round(len(drift_1h_vals) / n_total, 3),
        "drift_6h": round(len(drift_6h_vals) / n_total, 3),
        "drift_24h": round(len(drift_24h_vals) / n_total, 3),
    }

    tier = _confidence_tier(skew_prec["n"], drift_1h["n"])

    status = "populated" if tier != "unknown" else "insufficient_sample"

    n_in_play = sum(1 for f in fills if f.in_play)
    n_in_play_skew_excl = sum(
        1 for f in fills if f.in_play and f.ref_preceding is not None
    )
    n_thin_skew_excl = sum(
        1 for f in fills
        if not f.in_play and f.thin_market and f.ref_preceding is not None
    )

    return {
        "status": status,
        "n_trades_total": n_total,
        "n_trades_skew": skew_prec["n"],
        "n_trades_drift_1h": drift_1h["n"],
        "n_trades_drift_6h": drift_6h["n"],
        "n_trades_drift_24h": drift_24h["n"],
        "n_trades_in_play": n_in_play,
        "confidence_tier": tier,
        # Alias for the wallet-analysis-protocol H4 hypothesis, which reads
        # `sample_tier`. Same value as confidence_tier; never diverges.
        "sample_tier": tier,
        "coverage": coverage,
        "tape_trade_count_median": int(median([f.tape_n for f in fills])),
        "skew_vs_preceding_bar": skew_prec,
        "skew_vs_tape_5min": skew_tape,
        "drift_1h": drift_1h,
        "drift_6h": drift_6h,
        "drift_24h": drift_24h,
        # Raw drift lists for downstream bootstrap CIs (e.g. H4 entry-timing).
        # Summary dicts above keep their existing shape; these are additive.
        # Plural-with-s key names match the wallet-analysis-protocol contract.
        "drifts_1h": list(drift_1h_vals),
        "drifts_6h": list(drift_6h_vals),
        "drifts_24h": list(drift_24h_vals),
        # Parallel condition_id lists for H4 cluster-bootstrap (M6).
        "drifts_1h_clusters": list(drift_1h_clusters),
        "drifts_6h_clusters": list(drift_6h_clusters),
        "drifts_24h_clusters": list(drift_24h_clusters),
        "calibration_signature": _calibration_signature(skew_prec, drift_1h),
        "data_quality": {
            "in_play_excluded_from_skew": n_in_play_skew_excl,
            "thin_market_excluded_from_skew": n_thin_skew_excl,
            "resolution_ts_unknown_count": sum(1 for f in fills if f.resolution_ts_unknown),
            "insufficient_sample": tier == "unknown",
        },
    }


# -----------------------------------------------------------------------------
# Fill selection + history batching (Phase C integration layer)
# -----------------------------------------------------------------------------


def _select_buys(
    activity: Iterable[dict[str, Any]],
    max_trades: int | None = None,
) -> list[tuple[int, str, str, str, float]]:
    """Extract qualifying BUY fills from raw activity rows.

    Returns list of (trade_ts, condition_id, asset, title, fill_price) tuples
    in ascending trade_ts order. Matches the probe script's selection rules.
    """
    out: list[tuple[int, str, str, str, float]] = []
    for row in activity:
        if str(row.get("type") or "").upper() != "TRADE":
            continue
        if str(row.get("side") or "").upper() != "BUY":
            continue
        condition_id = str(row.get("conditionId") or "")
        asset = str(row.get("asset") or "")
        try:
            ts = int(row.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        raw_price = row.get("price")
        if raw_price is None or not condition_id or not asset or ts <= 0:
            continue
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if price <= 0.0 or price >= 1.0:
            continue
        title = str(row.get("title") or "")
        out.append((ts, condition_id, asset, title, price))
    out.sort(key=lambda tup: tup[0])
    if max_trades is not None and len(out) > max_trades:
        out = out[:max_trades]
    return out


def _build_history_cache(
    client: Any,
    fills: list[tuple[int, str, str, str, float]],
) -> dict[str, list[dict[str, Any]]]:
    """One `get_prices_series` call per distinct asset, spanning all its fills.

    Implements spec §8 batching: ~distinct_assets calls per wallet, not
    per_fill. Returns asset_id → history list.
    """
    span_by_asset: dict[str, tuple[int, int]] = {}
    for trade_ts, _cid, asset, _title, _price in fills:
        lo, hi = span_by_asset.get(asset, (trade_ts, trade_ts))
        span_by_asset[asset] = (min(lo, trade_ts), max(hi, trade_ts))
    cache: dict[str, list[dict[str, Any]]] = {}
    for asset, (lo, hi) in span_by_asset.items():
        try:
            cache[asset] = client.get_prices_series(
                token_id=asset,
                start_ts=lo - PRECEDING_BAR_WINDOW_SEC,
                end_ts=hi + DRIFT_HORIZONS[-1] + 300,
                fidelity=1,
            )
        except Exception as exc:
            logger.warning("prices-history fetch failed for %s: %s", asset, exc)
            cache[asset] = []
    return cache


def _run_probes(
    client: Any,
    wallet: str,
    fills: list[tuple[int, str, str, str, float]],
    history_cache: dict[str, list[dict[str, Any]]],
    *,
    db_path: str | None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[FillResult], dict[str, int]]:
    """Drive per-fill probes with a wall-clock budget. Returns (results, counters)."""
    resolve_cache: dict[str, int | None] = {}
    results: list[FillResult] = []
    counters = {"probed": 0, "api_skips": 0, "truncated": 0}
    start = now()
    for trade_ts, condition_id, asset, title, fill_price in fills:
        if now() - start > WALL_CLOCK_CEILING_SEC:
            counters["truncated"] = len(fills) - len(results)
            break
        resolve_ts = _resolve_ts_lookup(
            condition_id, db_path=db_path, cache=resolve_cache
        )
        try:
            fr = _probe_fill(
                client=client,
                wallet=wallet,
                trade_ts=trade_ts,
                condition_id=condition_id,
                asset=asset,
                title=title,
                fill_price=fill_price,
                history=history_cache.get(asset, []),
                resolve_ts=resolve_ts,
            )
        except Exception as exc:
            logger.warning("probe_fill failed ts=%s cid=%s: %s", trade_ts, condition_id[:10], exc)
            counters["api_skips"] += 1
            continue
        results.append(fr)
        counters["probed"] += 1
        if FILL_PACE_SEC > 0 and trade_ts != fills[-1][0]:
            sleep(FILL_PACE_SEC)
    return results, counters


# -----------------------------------------------------------------------------
# Slice 4 — per-fill probe primitive (FillProbeRecord)
# -----------------------------------------------------------------------------


# Locked spec §11 default probe configuration. The reference rule matches the
# legacy `_preceding_bar` picker so ledger probes and aggregator share one
# reference value-for-value.
DEFAULT_PROBE_CONFIG: dict[str, Any] = {
    "probe_version": "v1",
    "horizons_seconds": [3600, 21600, 86400],
    "reference_rule": "preceding_bar_30min_lookback",
    "reference_lookback_seconds": 1800,
    "min_history_seconds": 300,
    "tolerance_rule": "max(300, offset_sec // 12)",
    "fidelity_minutes": 1,
    "price_source_version": "batch-prices-history-v1",
}


def _probe_config_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FillProbeHorizon:
    """One horizon row for a probed fill. Maps to `wallet_execution_probe_horizons`."""

    horizon_seconds: int
    reference_price: float | None
    reference_ts: int | None
    target_ts: int
    target_price: float | None
    drift: float | None
    return_pct: float | None
    horizon_status: str  # 'success' | 'no_history' | 'invalid_reference' | 'error'


@dataclass(frozen=True)
class FillProbeRecord:
    """One probed fill. Persisted columns map 1:1 to `wallet_execution_probes`.

    Auxiliary fields (`title`, `ref_tape`, `tape_n`, `in_play`, `thin_market`,
    `resolution_ts_unknown`) are NOT persisted to the ledger — they exist so
    the legacy aggregator can reconstruct a `FillResult` without losing tape
    or in-play context.
    """

    fill_event_uid: str
    probe_config_hash: str
    probe_version: str
    horizons_seconds_json: str
    reference_rule: str
    condition_id: str
    token_id: str
    fill_ts: int
    fill_price: float
    side: str
    size_tokens: float | None
    reference_price: float | None
    reference_ts: int | None
    probe_status: str  # 'success' | 'no_history' | 'truncated' | 'error'
    probe_error_msg: str | None
    horizons: tuple[FillProbeHorizon, ...]
    title: str = ""
    ref_tape: float | None = None
    tape_n: int = 0
    in_play: bool = False
    thin_market: bool = False
    resolution_ts_unknown: bool = False


def _build_horizon(
    history: list[dict[str, Any]],
    trade_ts: int,
    fill_price: float,
    horizon_seconds: int,
    reference_price: float | None,
    reference_ts: int | None,
    resolve_ts: int | None,
) -> FillProbeHorizon:
    target_price, target_ts_actual = _bar_at_offset_record(
        history, trade_ts, horizon_seconds
    )
    target_ts = target_ts_actual if target_ts_actual is not None else trade_ts + horizon_seconds

    # Adaptive drift: drop horizons that overrun resolution (less a 300s buffer).
    if resolve_ts is not None and horizon_seconds > (resolve_ts - trade_ts) - 300:
        target_price = None
        target_ts = trade_ts + horizon_seconds

    drift = (target_price - fill_price) if target_price is not None else None

    if reference_price is None or reference_price <= 0:
        return_pct = None
        horizon_status = "invalid_reference"
    elif drift is None:
        return_pct = None
        horizon_status = "no_history"
    else:
        return_pct = drift / reference_price
        horizon_status = "success"

    return FillProbeHorizon(
        horizon_seconds=horizon_seconds,
        reference_price=reference_price,
        reference_ts=reference_ts,
        target_ts=target_ts,
        target_price=target_price,
        drift=drift,
        return_pct=return_pct,
        horizon_status=horizon_status,
    )


def _build_truncated_record(
    *,
    fill_event_uid: str,
    cfg: dict[str, Any],
    cfg_hash: str,
    horizons_json: str,
    horizons_list: list[int],
    condition_id: str,
    token_id: str,
    fill_ts: int,
    fill_price: float,
    title: str,
) -> FillProbeRecord:
    horizons = tuple(
        FillProbeHorizon(
            horizon_seconds=hz,
            reference_price=None,
            reference_ts=None,
            target_ts=fill_ts + hz,
            target_price=None,
            drift=None,
            return_pct=None,
            horizon_status="error",
        )
        for hz in horizons_list
    )
    return FillProbeRecord(
        fill_event_uid=fill_event_uid,
        probe_config_hash=cfg_hash,
        probe_version=cfg["probe_version"],
        horizons_seconds_json=horizons_json,
        reference_rule=cfg["reference_rule"],
        condition_id=condition_id,
        token_id=token_id,
        fill_ts=fill_ts,
        fill_price=fill_price,
        side="BUY",
        size_tokens=None,
        reference_price=None,
        reference_ts=None,
        probe_status="truncated",
        probe_error_msg=None,
        horizons=horizons,
        title=title,
    )


def _build_error_record(
    *,
    fill_event_uid: str,
    cfg: dict[str, Any],
    cfg_hash: str,
    horizons_json: str,
    horizons_list: list[int],
    condition_id: str,
    token_id: str,
    fill_ts: int,
    fill_price: float,
    title: str,
    error: BaseException,
) -> FillProbeRecord:
    horizons = tuple(
        FillProbeHorizon(
            horizon_seconds=hz,
            reference_price=None,
            reference_ts=None,
            target_ts=fill_ts + hz,
            target_price=None,
            drift=None,
            return_pct=None,
            horizon_status="error",
        )
        for hz in horizons_list
    )
    return FillProbeRecord(
        fill_event_uid=fill_event_uid,
        probe_config_hash=cfg_hash,
        probe_version=cfg["probe_version"],
        horizons_seconds_json=horizons_json,
        reference_rule=cfg["reference_rule"],
        condition_id=condition_id,
        token_id=token_id,
        fill_ts=fill_ts,
        fill_price=fill_price,
        side="BUY",
        size_tokens=None,
        reference_price=None,
        reference_ts=None,
        probe_status="error",
        probe_error_msg=str(error),
        horizons=horizons,
        title=title,
    )


def iter_fill_probes(
    wallet: str,
    activity: Iterable[dict[str, Any]],
    *,
    classification: dict[str, Any] | None = None,
    counts: dict[str, Any] | None = None,
    returns: dict[str, Any] | None = None,
    force: bool = False,
    max_trades: int | None = None,
    db_path: str | None = None,
    client: Any | None = None,
    probe_config: dict[str, Any] | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall_clock_ceiling_seconds: float = WALL_CLOCK_CEILING_SEC,
) -> Iterator[FillProbeRecord]:
    """Per-fill probe primitive yielding one `FillProbeRecord` per qualifying BUY.

    Public surface for both the legacy aggregator (`compute_execution_profile`)
    and the Slice 4 ledger writer (`sniper.analysis.wallet_protocol.ledger.probes`).
    Iteration order matches `_select_buys` (ascending `trade_ts`).

    `fill_event_uid` is taken from `row["event_uid"]` when the caller has
    pre-joined activity rows with `wallet_events`; otherwise the field is
    the empty string and the ledger writer must reject those records before
    persisting.

    Yields nothing when gating skips the wallet, no client is provided, or
    no qualifying BUYs exist — matches `compute_execution_profile`'s
    short-circuit conditions so the aggregator can pre-detect those cases
    without iterating.
    """
    cfg = dict(probe_config or DEFAULT_PROBE_CONFIG)
    cfg_hash = _probe_config_hash(cfg)
    horizons_list = list(cfg["horizons_seconds"])
    horizons_json = json.dumps(
        horizons_list, sort_keys=True, separators=(",", ":")
    )

    run, _reason = _should_run(classification, counts, returns, force=force)
    if not run:
        return
    if client is None:
        return

    activity_list = list(activity)
    fills = _select_buys(activity_list, max_trades=max_trades)
    if not fills:
        return

    history_cache = _build_history_cache(client, fills)
    resolve_cache: dict[str, int | None] = {}

    # Codex PR #6 review item 4: same-second BUY fills on the same
    # (conditionId, asset, ts) previously collapsed into a single
    # event_uid via `event_uid_index[key] = ev`, so the loop below
    # tagged every same-second fill with the LAST row's event_uid.
    # Downstream `upsert_execution_probes` then deduplicated by
    # fill_event_uid → silently dropped fills.
    #
    # Fix: build a FIFO queue per (cid, asset, ts) so each fill pops
    # its own event_uid in the order the activity rows arrived. The
    # queue is built from the SAME filter predicate as `_select_buys`
    # (TRADE/BUY/non-empty cid+asset/ts>0/parseable price in (0,1))
    # so queue depth at every key matches the number of fills the
    # outer loop will draw from that key. After max_trades truncation
    # excess entries on the tail are simply ignored — we only ever
    # pop while iterating `fills`. The activity_list MUST also be
    # sorted ascending-by-trade_ts to match `_select_buys`'s post-sort
    # output; we sort the queue source explicitly to lock that.
    event_uid_queue: dict[tuple[str, str, int], list[str]] = {}
    queue_source: list[tuple[int, str, str, str]] = []
    for row in activity_list:
        if str(row.get("type") or "").upper() != "TRADE":
            continue
        if str(row.get("side") or "").upper() != "BUY":
            continue
        cid_raw = row.get("conditionId") or ""
        asset_raw = row.get("asset") or ""
        try:
            ts_raw = int(row.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        raw_price = row.get("price")
        if raw_price is None or not cid_raw or not asset_raw or ts_raw <= 0:
            continue
        try:
            price_val = float(raw_price)
        except (TypeError, ValueError):
            continue
        if price_val <= 0.0 or price_val >= 1.0:
            continue
        ev = row.get("event_uid")
        ev_str = str(ev) if ev else ""
        queue_source.append((ts_raw, str(cid_raw), str(asset_raw), ev_str))
    # Match `_select_buys`'s sort: ascending trade_ts, stable on ties.
    queue_source.sort(key=lambda tup: tup[0])
    for ts_raw, cid_str, asset_str, ev_str in queue_source:
        event_uid_queue.setdefault((cid_str, asset_str, ts_raw), []).append(ev_str)

    last_ts = fills[-1][0]
    start = now()
    for trade_ts, condition_id, asset, title, fill_price in fills:
        key = (condition_id, asset, trade_ts)
        queue = event_uid_queue.get(key)
        ev_uid = queue.pop(0) if queue else ""
        if now() - start > wall_clock_ceiling_seconds:
            yield _build_truncated_record(
                fill_event_uid=ev_uid,
                cfg=cfg,
                cfg_hash=cfg_hash,
                horizons_json=horizons_json,
                horizons_list=horizons_list,
                condition_id=condition_id,
                token_id=asset,
                fill_ts=trade_ts,
                fill_price=fill_price,
                title=title,
            )
            continue

        resolve_ts = _resolve_ts_lookup(
            condition_id, db_path=db_path, cache=resolve_cache
        )
        history = history_cache.get(asset, [])
        try:
            fr = _probe_fill(
                client=client,
                wallet=wallet,
                trade_ts=trade_ts,
                condition_id=condition_id,
                asset=asset,
                title=title,
                fill_price=fill_price,
                history=history,
                resolve_ts=resolve_ts,
            )
        except Exception as exc:
            logger.warning(
                "probe_fill failed ts=%s cid=%s: %s",
                trade_ts,
                condition_id[:10],
                exc,
            )
            yield _build_error_record(
                fill_event_uid=ev_uid,
                cfg=cfg,
                cfg_hash=cfg_hash,
                horizons_json=horizons_json,
                horizons_list=horizons_list,
                condition_id=condition_id,
                token_id=asset,
                fill_ts=trade_ts,
                fill_price=fill_price,
                title=title,
                error=exc,
            )
            continue

        ref_price, ref_ts = _preceding_bar_record(history, trade_ts)
        horizons = tuple(
            _build_horizon(
                history=history,
                trade_ts=trade_ts,
                fill_price=fill_price,
                horizon_seconds=hz,
                reference_price=ref_price,
                reference_ts=ref_ts,
                resolve_ts=resolve_ts,
            )
            for hz in horizons_list
        )

        probe_status = "success" if ref_price is not None else "no_history"
        yield FillProbeRecord(
            fill_event_uid=ev_uid,
            probe_config_hash=cfg_hash,
            probe_version=cfg["probe_version"],
            horizons_seconds_json=horizons_json,
            reference_rule=cfg["reference_rule"],
            condition_id=condition_id,
            token_id=asset,
            fill_ts=trade_ts,
            fill_price=fill_price,
            side="BUY",
            size_tokens=None,
            reference_price=ref_price,
            reference_ts=ref_ts,
            probe_status=probe_status,
            probe_error_msg=None,
            horizons=horizons,
            title=title,
            ref_tape=fr.ref_tape,
            tape_n=fr.tape_n,
            in_play=fr.in_play,
            thin_market=fr.thin_market,
            resolution_ts_unknown=fr.resolution_ts_unknown,
        )

        if FILL_PACE_SEC > 0 and trade_ts != last_ts:
            sleep(FILL_PACE_SEC)


def rehydrate_fill_probe_records_from_ledger(
    conn: "Any",
    wallet: str,
) -> list[FillProbeRecord]:
    """Read `wallet_execution_probes` + `wallet_execution_probe_horizons`
    for `wallet` and reconstruct in-memory `FillProbeRecord`s.

    Slice 5 Task 7 wrapper. Lets the legacy aggregator (`_aggregate`)
    operate on records that originated in the DuckDB warehouse rather
    than from a live `iter_fill_probes` call. Together with Task 0's
    auxiliary-column persistence this round-trip is lossless for every
    field `_record_to_fill_result` reads.

    `title` is not persisted by the probe writer (it lives on
    `market_outcome_dim_snapshot`, not on the probe row); rehydrated
    records carry the dataclass default `title=""`. `_aggregate` does
    not consume `title`.

    Args:
        conn: DuckDB connection on the wallet warehouse.
        wallet: 0x-prefixed wallet address. Compared lowercased.

    Returns:
        List of `FillProbeRecord`s ordered by `fill_ts ASC, fill_event_uid ASC`,
        each with its `horizons` tuple ordered by `horizon_seconds ASC` to
        mirror `iter_fill_probes`'s deterministic emission order.
    """
    wallet_lc = wallet.lower()
    probe_rows = conn.execute(
        """
        SELECT
            fill_event_uid,
            probe_config_hash,
            probe_version,
            horizons_seconds_json,
            reference_rule,
            condition_id,
            token_id,
            fill_ts,
            fill_price,
            side,
            size_tokens,
            reference_price,
            reference_ts,
            probe_status,
            probe_error_msg,
            ref_tape,
            tape_n,
            in_play,
            thin_market,
            resolution_ts_unknown
        FROM wallet_execution_probes
        WHERE wallet = ?
        ORDER BY fill_ts ASC, fill_event_uid ASC
        """,
        [wallet_lc],
    ).fetchall()

    horizon_rows = conn.execute(
        """
        SELECT
            fill_event_uid,
            probe_config_hash,
            horizon_seconds,
            reference_price,
            reference_ts,
            target_ts,
            target_price,
            drift,
            return_pct,
            horizon_status
        FROM wallet_execution_probe_horizons
        WHERE wallet = ?
        ORDER BY fill_event_uid, probe_config_hash, horizon_seconds ASC
        """,
        [wallet_lc],
    ).fetchall()

    horizons_by_probe: dict[tuple[str, str], list[FillProbeHorizon]] = {}
    for row in horizon_rows:
        key = (row[0], row[1])
        horizons_by_probe.setdefault(key, []).append(
            FillProbeHorizon(
                horizon_seconds=int(row[2]),
                reference_price=row[3],
                reference_ts=row[4],
                target_ts=int(row[5]),
                target_price=row[6],
                drift=row[7],
                return_pct=row[8],
                horizon_status=str(row[9]),
            )
        )

    records: list[FillProbeRecord] = []
    for row in probe_rows:
        key = (row[0], row[1])
        records.append(
            FillProbeRecord(
                fill_event_uid=str(row[0]),
                probe_config_hash=str(row[1]),
                probe_version=str(row[2]),
                horizons_seconds_json=str(row[3]),
                reference_rule=str(row[4]),
                condition_id=str(row[5]) if row[5] is not None else "",
                token_id=str(row[6]) if row[6] is not None else "",
                fill_ts=int(row[7]),
                fill_price=float(row[8]),
                side=str(row[9]) if row[9] is not None else "BUY",
                size_tokens=row[10],
                reference_price=row[11],
                reference_ts=row[12],
                probe_status=str(row[13]),
                probe_error_msg=row[14],
                horizons=tuple(horizons_by_probe.get(key, [])),
                ref_tape=row[15],
                tape_n=int(row[16]) if row[16] is not None else 0,
                in_play=bool(row[17]) if row[17] is not None else False,
                thin_market=bool(row[18]) if row[18] is not None else False,
                resolution_ts_unknown=(
                    bool(row[19]) if row[19] is not None else False
                ),
            )
        )
    return records


def aggregate_execution_profile_from_ledger(
    conn: "Any",
    wallet: str,
) -> dict[str, Any]:
    """End-to-end materialized-aggregate path. Spec §6 envelope.

    Pulls probe rows out of the DuckDB warehouse, rehydrates them into
    `FillProbeRecord`s, converts each successful record into a
    `FillResult` via `_record_to_fill_result`, and runs the same
    `_aggregate` used by `compute_execution_profile`.

    Returns the populated envelope, identical byte-for-byte to a live
    `compute_execution_profile` run over the same fills (verified by
    `test_compute_execution_profile_aggregate_unchanged_after_refactor`
    against the live path and Slice 5 Task 7 against this path).
    """
    records = rehydrate_fill_probe_records_from_ledger(conn, wallet)
    fill_results: list[FillResult] = [
        _record_to_fill_result(record)
        for record in records
        if record.probe_status in ("success", "no_history")
    ]
    return _aggregate(fill_results)


def _record_to_fill_result(record: FillProbeRecord) -> FillResult:
    """Convert a `FillProbeRecord` back into the legacy `FillResult` shape used
    by `_aggregate`. Drift values are pulled from the per-horizon rows.
    """
    drift_by_horizon = {h.horizon_seconds: h.drift for h in record.horizons}
    return FillResult(
        trade_ts=record.fill_ts,
        condition_id=record.condition_id,
        asset=record.token_id,
        title=record.title,
        fill_price=record.fill_price,
        ref_preceding=record.reference_price,
        ref_tape=record.ref_tape,
        tape_n=record.tape_n,
        drift_1h=drift_by_horizon.get(DRIFT_HORIZONS[0]),
        drift_6h=drift_by_horizon.get(DRIFT_HORIZONS[1]),
        drift_24h=drift_by_horizon.get(DRIFT_HORIZONS[2]),
        in_play=record.in_play,
        thin_market=record.thin_market,
        resolution_ts_unknown=record.resolution_ts_unknown,
    )


# -----------------------------------------------------------------------------
# Public entry
# -----------------------------------------------------------------------------


def compute_execution_profile(
    wallet: str,
    activity: Iterable[dict[str, Any]],
    client: Any | None = None,
    *,
    classification: dict[str, Any] | None = None,
    counts: dict[str, Any] | None = None,
    returns: dict[str, Any] | None = None,
    force: bool = False,
    max_trades: int | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Return execution_profile dict per spec §5.

    Pipeline:
      1. Gate check (`_should_run`) — stub when skipped.
      2. Extract BUYs from activity.
      3. Iterate per-fill probes via `iter_fill_probes` (Slice 4 primitive).
      4. Aggregate the resulting `FillResult` values into the populated envelope.

    Failure modes (spec §9):
      - no client passed but run required → status="api_failure"
      - per-fill probe failures counted; if >=50% of fills skip → status="api_failure"
      - wall-clock budget exceeded → remaining fills flagged as truncated

    Args:
        wallet: wallet address (lowercased by caller; not re-normalized here)
        activity: iterable of raw activity dicts
        client: PolymarketClient or compatible (get_prices_series + get_trades_window)
        classification: features["classification"] dict
        counts: features["counts"] dict
        returns: features["returns"] dict
        force: bypass Layer 1 terminal skip (for calibration/QA)
        max_trades: cap on fills processed (None = no cap)
        db_path: optional override for market_metadata SQLite lookup
    """
    run, reason = _should_run(classification, counts, returns, force=force)
    if not run:
        return _stub(reason)

    if client is None:
        return {"status": "api_failure", "reason": "no_client_provided"}

    activity_list = list(activity)
    if not _select_buys(activity_list, max_trades=max_trades):
        return {"status": "insufficient_sample", "reason": "no_qualifying_buys"}

    records = list(
        iter_fill_probes(
            wallet,
            activity_list,
            classification=classification,
            counts=counts,
            returns=returns,
            force=force,
            max_trades=max_trades,
            db_path=db_path,
            client=client,
        )
    )

    counters = {"probed": 0, "api_skips": 0, "truncated": 0}
    fill_results: list[FillResult] = []
    for record in records:
        if record.probe_status in ("success", "no_history"):
            counters["probed"] += 1
            fill_results.append(_record_to_fill_result(record))
        elif record.probe_status == "error":
            counters["api_skips"] += 1
        elif record.probe_status == "truncated":
            counters["truncated"] += 1

    if records and counters["api_skips"] >= 0.5 * len(records):
        return {
            "status": "api_failure",
            "reason": "per_fill_error_rate_over_50pct",
            "api_skips": counters["api_skips"],
            "probed": counters["probed"],
        }

    profile = _aggregate(fill_results)
    if counters.get("truncated"):
        profile.setdefault("data_quality", {})["truncated_input"] = True
        profile["data_quality"]["truncated_count"] = counters["truncated"]
    if counters.get("api_skips"):
        profile.setdefault("data_quality", {})["api_skips"] = counters["api_skips"]
    return profile


# Convenience: callers that want the raw FillResult payload (aggregator
# research scripts) can serialise via this helper.
def fill_result_to_dict(fr: FillResult) -> dict[str, Any]:
    return asdict(fr)


def fill_result_from_probe_dict(d: dict[str, Any]) -> FillResult:
    """Rehydrate a FillResult from a probe.json row (MVE schema).

    The MVE probe output uses snake_case keys that are close but not identical
    to FillResult fields (e.g. `ref_bar_preceding` vs `ref_preceding`, no
    `in_play` / `thin_market` / `resolution_ts_unknown`). This adapter lets
    committed fixtures replay through the Phase C aggregator.

    Any missing gate flags default to False: the committed fixtures were
    generated before the gates existed; the replay test compensates by
    asserting raw coverage and central tendency, not post-gate counts.
    """
    return FillResult(
        trade_ts=int(d["trade_ts"]),
        condition_id=str(d.get("condition_id") or ""),
        asset=str(d.get("asset") or ""),
        title=str(d.get("title") or ""),
        fill_price=float(d["fill_price"]),
        ref_preceding=d.get("ref_bar_preceding"),
        ref_tape=d.get("ref_tape_5min"),
        tape_n=int(d.get("tape_trade_count") or 0),
        drift_1h=d.get("drift_1h"),
        drift_6h=d.get("drift_6h"),
        drift_24h=d.get("drift_24h"),
        in_play=bool(d.get("in_play", False)),
        thin_market=bool(d.get("tape_trade_count", 0) < TAPE_CONFIDENCE_MIN),
        resolution_ts_unknown=bool(d.get("resolution_ts_unknown", False)),
    )
