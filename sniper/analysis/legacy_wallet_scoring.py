"""Legacy wallet scoring and classification helpers extracted from analyze_users.py."""

from __future__ import annotations

import datetime as dt
import os
import re
from collections import defaultdict, deque
from typing import Any, Dict, List, Tuple

from sniper.filters import is_excluded_market
from sniper.utils import (
    _optional_float as _utils_optional_float,
    _parse_any_ts as _utils_parse_any_ts,
    _safe_float as _utils_safe_float,
    _safe_int as _utils_safe_int,
)


MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "10"))
INSIDER_MAX_POSITIONS = int(os.getenv("INSIDER_MAX_POSITIONS", "5"))
MIN_PNL = float(os.getenv("MIN_PNL", "-100000"))
MAX_PNL = float(os.getenv("MAX_PNL", "250000"))
MIN_POSITION_VALUE = float(os.getenv("MIN_POSITION_VALUE", "2000"))
MIN_ORDER_NOTIONAL = float(os.getenv("MIN_ORDER_NOTIONAL", "2000"))
MIN_TRADE_NOTIONAL = float(os.getenv("MIN_TRADE_NOTIONAL", str(MIN_ORDER_NOTIONAL)))

LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "14"))
MIN_LOW_BUY_NOTIONAL_FOR_FEATURES = float(
    os.getenv("MIN_LOW_BUY_NOTIONAL_FOR_FEATURES", str(MIN_TRADE_NOTIONAL))
)
MIN_LOW_BUY_IMPACT_RATIO_FOR_FEATURES = float(
    os.getenv("MIN_LOW_BUY_IMPACT_RATIO_FOR_FEATURES", "0.005")
)
MIN_REPEAT_HITS_14D = int(os.getenv("MIN_REPEAT_HITS_14D", "3"))
SCORE_FLAG_THRESHOLD = float(os.getenv("SCORE_FLAG_THRESHOLD", "70"))
SPECULATOR_SCORE_HIGH = float(os.getenv("SPECULATOR_SCORE_HIGH", "65"))
MIN_WATCH_TOTAL_OPEN_VALUE = float(os.getenv("MIN_WATCH_TOTAL_OPEN_VALUE", "1000"))
MIN_WATCH_TOP_POSITION_VALUE = float(os.getenv("MIN_WATCH_TOP_POSITION_VALUE", "1000"))
FRESH_ACCOUNT_DAYS = int(os.getenv("FRESH_ACCOUNT_DAYS", "90"))
# === OPTIMIZED FOR $200 BANKROLL ===
WINRATE_THRESHOLD = float(os.getenv("WINRATE_THRESHOLD", "0.65"))  # Was 0.70
WINRATE_MIN_SAMPLE = int(os.getenv("WINRATE_MIN_SAMPLE", "10"))   # Was 20
INSIDER_MIN_TOTAL_PNL = float(os.getenv("INSIDER_MIN_TOTAL_PNL", "20000"))
INSIDER_MIN_TOP_OPEN_VALUE = float(os.getenv("INSIDER_MIN_TOP_OPEN_VALUE", "3000"))
INSIDER_MIN_TOTAL_OPEN_VALUE = float(os.getenv("INSIDER_MIN_TOTAL_OPEN_VALUE", "10000"))
INSIDER_MIN_REALIZED_PNL_SOFT_TARGET = float(
    os.getenv("INSIDER_MIN_REALIZED_PNL_SOFT_TARGET", os.getenv("INSIDER_MIN_TOTAL_PNL", "20000"))
)
INSIDER_MIN_SERIOUS_TRADE_NOTIONAL = float(
    os.getenv("INSIDER_MIN_SERIOUS_TRADE_NOTIONAL", str(MIN_TRADE_NOTIONAL))
)
INSIDER_MIN_SERIOUS_TRADES_14D = int(os.getenv("INSIDER_MIN_SERIOUS_TRADES_14D", "3"))
PROFILE_REFRESH_SECONDS = int(os.getenv("PROFILE_REFRESH_SECONDS", "21600"))
PROFILE_ENRICHMENT_REFRESH_SECONDS = int(os.getenv("PROFILE_ENRICHMENT_REFRESH_SECONDS", "86400"))
USER_ACTIVITY_LIMIT = int(os.getenv("USER_ACTIVITY_LIMIT", "200"))
USER_ACTIVITY_MAX_PAGES = int(os.getenv("USER_ACTIVITY_MAX_PAGES", "5"))
EMERGING_SCORE_THRESHOLD = float(os.getenv("EMERGING_SCORE_THRESHOLD", "65"))
EMERGING_MIN_POSITION_INSIDER_SCORE = float(os.getenv("EMERGING_MIN_POSITION_INSIDER_SCORE", "68"))
EMERGING_MIN_TOP_OPEN_VALUE = float(os.getenv("EMERGING_MIN_TOP_OPEN_VALUE", "2500"))
EMERGING_MIN_TOTAL_OPEN_VALUE = float(os.getenv("EMERGING_MIN_TOTAL_OPEN_VALUE", "5000"))
EMERGING_MAX_POSITION_SPECULATOR_SCORE = float(os.getenv("EMERGING_MAX_POSITION_SPECULATOR_SCORE", "70"))
EMERGING_MAX_POSITION_SPECULATOR_COUNT = int(os.getenv("EMERGING_MAX_POSITION_SPECULATOR_COUNT", "1"))
POSITION_MIN_OPEN_VALUE = float(os.getenv("POSITION_MIN_OPEN_VALUE", "2000"))
POSITION_SCORE_THRESHOLD = float(os.getenv("POSITION_SCORE_THRESHOLD", "65"))
POSITION_SPECULATOR_THRESHOLD = float(os.getenv("POSITION_SPECULATOR_THRESHOLD", "65"))
POSITION_WINDOW_5M = int(os.getenv("POSITION_WINDOW_5M", "300"))
POSITION_WINDOW_1H = int(os.getenv("POSITION_WINDOW_1H", "3600"))
POSITION_WINDOW_24H = int(os.getenv("POSITION_WINDOW_24H", "86400"))
POSITION_HOLD_IDLE_SECONDS = int(os.getenv("POSITION_HOLD_IDLE_SECONDS", "86400"))
POSITION_CHURN_HIGH_RATIO = float(os.getenv("POSITION_CHURN_HIGH_RATIO", "1.5"))
POSITION_CHURN_MED_RATIO = float(os.getenv("POSITION_CHURN_MED_RATIO", "0.8"))
POSITION_SCAN_BATCH_SIZE = int(os.getenv("POSITION_SCAN_BATCH_SIZE", "150"))
POSITION_SCAN_STALE_SECONDS = int(os.getenv("POSITION_SCAN_STALE_SECONDS", "600"))
POSITION_SETTLEMENT_PRICE_EPS = float(os.getenv("POSITION_SETTLEMENT_PRICE_EPS", "0.001"))
POSITION_SETTLEMENT_END_GRACE_SECONDS = int(os.getenv("POSITION_SETTLEMENT_END_GRACE_SECONDS", "21600"))
POSITION_SUPPRESS_EXEC_BOT_ENABLED = str(os.getenv("POSITION_SUPPRESS_EXEC_BOT_ENABLED", "true")).strip().lower() not in {
    "0",
    "false",
    "no",
}
POSITION_SUPPRESS_EXEC_BOT_MIN_STRATEGY_CONFIDENCE = float(
    os.getenv("POSITION_SUPPRESS_EXEC_BOT_MIN_STRATEGY_CONFIDENCE", "90")
)
POSITION_SUPPRESS_EXEC_BOT_MIN_RAPID_BURST_RATIO = float(
    os.getenv("POSITION_SUPPRESS_EXEC_BOT_MIN_RAPID_BURST_RATIO", "0.60")
)
POSITION_SUPPRESS_EXEC_BOT_MIN_MAX_TRADES_60S = int(
    os.getenv("POSITION_SUPPRESS_EXEC_BOT_MIN_MAX_TRADES_60S", "20")
)
POSITION_SUPPRESS_EXEC_BOT_MIN_SPECULATOR_SCORE = float(
    os.getenv("POSITION_SUPPRESS_EXEC_BOT_MIN_SPECULATOR_SCORE", "90")
)

ANALYSIS_BATCH_SIZE = int(os.getenv("ANALYSIS_BATCH_SIZE", "120"))
ANALYSIS_STALE_SECONDS = int(os.getenv("ANALYSIS_STALE_SECONDS", "600"))
ANALYSIS_POLL_SECONDS = int(os.getenv("ANALYSIS_POLL_SECONDS", "30"))
USER_TRADES_LIMIT = int(os.getenv("USER_TRADES_LIMIT", "300"))
ANALYSIS_SOURCE = str(os.getenv("ANALYSIS_SOURCE", "legacy")).strip().lower()
ANALYSIS_SOURCE = ANALYSIS_SOURCE if ANALYSIS_SOURCE in {"legacy", "queue", "hybrid"} else "legacy"
ANALYZER_WORKER_ID = str(os.getenv("ANALYZER_WORKER_ID", f"analyzer-{os.getpid()}")).strip()
QUEUE_CLAIM_LEASE_SECONDS = int(os.getenv("QUEUE_CLAIM_LEASE_SECONDS", "300"))
DISCOVERY_SCORE_THRESHOLD = int(os.getenv("DISCOVERY_SCORE_THRESHOLD", "50"))
DISCOVERY_SCORE_BATCH = int(os.getenv("DISCOVERY_SCORE_BATCH", "2000"))
QUEUE_ENQUEUE_LIMIT = int(os.getenv("QUEUE_ENQUEUE_LIMIT", "1000"))
QUEUE_MAX_RETRIES = int(os.getenv("QUEUE_MAX_RETRIES", "5"))
QUEUE_RETRY_BASE_SECONDS = int(os.getenv("QUEUE_RETRY_BASE_SECONDS", "60"))

EXEC_CLUSTER_MAX_GAP_SECONDS = int(os.getenv("EXEC_CLUSTER_MAX_GAP_SECONDS", "120"))
EXEC_CLUSTER_MAX_SPAN_SECONDS = int(os.getenv("EXEC_CLUSTER_MAX_SPAN_SECONDS", "900"))
EXEC_RAPID_MIN_TRADES = int(os.getenv("EXEC_RAPID_MIN_TRADES", "6"))
EXEC_RAPID_MIN_UNIQUE_SIZES = int(os.getenv("EXEC_RAPID_MIN_UNIQUE_SIZES", "3"))

STRAT_EXEC_MIN_TRADE_COUNT = int(os.getenv("STRAT_EXEC_MIN_TRADE_COUNT", "20"))
STRAT_EXEC_MIN_BURST_RATIO = float(os.getenv("STRAT_EXEC_MIN_BURST_RATIO", "0.35"))
STRAT_EXEC_MIN_MAX_TRADES_60S = int(os.getenv("STRAT_EXEC_MIN_MAX_TRADES_60S", "8"))
STRAT_THEME_MIN_OPEN_SHARE = float(os.getenv("STRAT_THEME_MIN_OPEN_SHARE", "0.60"))
STRAT_THEME_MIN_MARKETS = int(os.getenv("STRAT_THEME_MIN_MARKETS", "3"))
STRAT_SINGLE_THESIS_MIN_CONCENTRATION = float(os.getenv("STRAT_SINGLE_THESIS_MIN_CONCENTRATION", "0.70"))
STRAT_DIVERSIFIED_MIN_MARKET_DIVERSITY = int(os.getenv("STRAT_DIVERSIFIED_MIN_MARKET_DIVERSITY", "8"))
INSIDER_OVERLAY_MANY_POSITIONS = int(os.getenv("INSIDER_OVERLAY_MANY_POSITIONS", "8"))
INSIDER_OVERLAY_VERY_MANY_POSITIONS = int(os.getenv("INSIDER_OVERLAY_VERY_MANY_POSITIONS", "12"))
INSIDER_OVERLAY_MAX_PENALTY = float(os.getenv("INSIDER_OVERLAY_MAX_PENALTY", "30"))

_MONTH_TOKEN_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
)
_DATE_TOKEN_RE = re.compile(r"\b\d{1,4}(?:st|nd|rd|th)?\b")
_PUNCT_RE = re.compile(r"[^a-z0-9\s]")


def _safe_float(value: Any, default: float = 0.0) -> float:
    return _utils_safe_float(value, default)


def _is_excluded_record(record: Dict[str, Any]) -> bool:
    excluded, _ = is_excluded_market(
        record.get("title"),
        record.get("slug"),
        record.get("eventSlug"),
    )
    return excluded


def _safe_int(value: Any, default: int = 0) -> int:
    return _utils_safe_int(value, default)


def _optional_float(value: Any) -> float | None:
    return _utils_optional_float(value)


def _parse_any_ts(value: Any) -> int:
    parsed = _utils_parse_any_ts(value)
    return parsed if parsed is not None else 0


def _classify_open_position_state(
    row: Dict[str, Any],
    *,
    now_ts: int,
) -> tuple[str, str]:
    cur_price = _optional_float(row.get("curPrice"))
    current_value = max(0.0, _safe_float(row.get("currentValue")))
    size = max(0.0, _safe_float(row.get("size")))
    cash_pnl = _safe_float(row.get("cashPnl"))
    end_ts = _parse_any_ts(row.get("endDate"))
    end_reached = bool(end_ts and now_ts >= (end_ts - POSITION_SETTLEMENT_END_GRACE_SECONDS))
    eps = max(0.0, POSITION_SETTLEMENT_PRICE_EPS)

    value_ratio = 0.0
    if size > 0:
        value_ratio = current_value / size

    likely_win = False
    likely_loss = False
    if cur_price is not None:
        likely_win = cur_price >= (1.0 - eps)
        likely_loss = cur_price <= eps
    if size > 0:
        if value_ratio >= (1.0 - eps):
            likely_win = True
        if value_ratio <= eps:
            likely_loss = True

    if end_reached and likely_win:
        return "pending_settlement_win", "event ended and payout is at/near 1.0"
    if end_reached and likely_loss:
        return "pending_settlement_loss", "event ended and payout is at/near 0.0"
    if end_reached and cash_pnl > 0:
        return "pending_settlement_win", "event ended and open cash PNL is positive"
    if end_reached and cash_pnl < 0:
        return "pending_settlement_loss", "event ended and open cash PNL is negative"

    # Some rows miss endDate; fall back to strongly settled price/value signatures.
    if end_ts <= 0 and likely_win and size > 0 and value_ratio >= (1.0 - eps):
        return "pending_settlement_win", "position value already settled near full payout"
    if end_ts <= 0 and likely_loss and size > 0 and value_ratio <= eps:
        return "pending_settlement_loss", "position value already settled near zero payout"

    return "active", "position still carries live event risk"


def _split_open_positions(
    rows: List[Dict[str, Any]],
    *,
    now_ts: int,
) -> Dict[str, Any]:
    active: List[Dict[str, Any]] = []
    pending_win: List[Dict[str, Any]] = []
    pending_loss: List[Dict[str, Any]] = []

    for row in rows:
        state, reason = _classify_open_position_state(row, now_ts=now_ts)
        enriched = dict(row)
        enriched["_open_state"] = state
        enriched["_open_state_reason"] = reason

        if state == "pending_settlement_win":
            pending_win.append(enriched)
        elif state == "pending_settlement_loss":
            pending_loss.append(enriched)
        else:
            active.append(enriched)

    return {
        "active": active,
        "pending_settlement_win": pending_win,
        "pending_settlement_loss": pending_loss,
    }


def _normalize_theme_key(text: str) -> str:
    raw = str(text or "").strip().lower()
    if not raw:
        return ""
    raw = _MONTH_TOKEN_RE.sub(" ", raw)
    raw = _DATE_TOKEN_RE.sub(" ", raw)
    raw = raw.replace(" by ", " ").replace(" before ", " ").replace(" after ", " ")
    raw = _PUNCT_RE.sub(" ", raw)
    stop_words = {
        "a",
        "an",
        "and",
        "are",
        "be",
        "does",
        "happen",
        "happens",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "will",
    }
    tokens = [token for token in raw.split() if token and token not in stop_words]
    return " ".join(tokens)


def _human_theme(text: str) -> str:
    key = _normalize_theme_key(text)
    if not key:
        return ""
    return " ".join(token.capitalize() for token in key.split()[:8]).strip()


def _trade_activity_features(trades: List[Dict[str, Any]]) -> Dict[str, float]:
    if not trades:
        return {
            "max_trades_60s": 0.0,
            "rapid_cluster_count": 0.0,
            "rapid_cluster_trades": 0.0,
            "rapid_burst_trade_ratio": 0.0,
            "median_gap_seconds": 0.0,
        }

    timestamps = sorted([_safe_int(row.get("timestamp")) for row in trades if _safe_int(row.get("timestamp")) > 0])
    max_trades_60s = 0
    window: deque[int] = deque()
    for ts in timestamps:
        window.append(ts)
        while window and ts - window[0] > 60:
            window.popleft()
        if len(window) > max_trades_60s:
            max_trades_60s = len(window)

    gaps = [max(0, b - a) for a, b in zip(timestamps, timestamps[1:])]
    median_gap = 0.0
    if gaps:
        ordered = sorted(gaps)
        mid = len(ordered) // 2
        if len(ordered) % 2 == 1:
            median_gap = float(ordered[mid])
        else:
            median_gap = float((ordered[mid - 1] + ordered[mid]) / 2)

    grouped: Dict[tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in trades:
        condition_id = str(row.get("conditionId") or row.get("market") or "").strip().lower()
        outcome = str(row.get("outcome") or "").strip().lower()
        ts = _safe_int(row.get("timestamp"))
        if not condition_id or ts <= 0:
            continue
        grouped[(condition_id, outcome)].append(row)

    rapid_cluster_count = 0
    rapid_cluster_trades = 0
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda r: _safe_int(r.get("timestamp")))
        if not ordered:
            continue
        current_cluster: List[Dict[str, Any]] = [ordered[0]]
        for row in ordered[1:]:
            prev_ts = _safe_int(current_cluster[-1].get("timestamp"))
            start_ts = _safe_int(current_cluster[0].get("timestamp"))
            ts = _safe_int(row.get("timestamp"))
            gap = ts - prev_ts
            span = ts - start_ts
            if gap <= EXEC_CLUSTER_MAX_GAP_SECONDS and span <= EXEC_CLUSTER_MAX_SPAN_SECONDS:
                current_cluster.append(row)
                continue
            rapid_cluster_count, rapid_cluster_trades = _accumulate_rapid_cluster_stats(
                current_cluster,
                rapid_cluster_count,
                rapid_cluster_trades,
            )
            current_cluster = [row]
        rapid_cluster_count, rapid_cluster_trades = _accumulate_rapid_cluster_stats(
            current_cluster,
            rapid_cluster_count,
            rapid_cluster_trades,
        )

    trade_count = max(1, len(trades))
    return {
        "max_trades_60s": float(max_trades_60s),
        "rapid_cluster_count": float(rapid_cluster_count),
        "rapid_cluster_trades": float(rapid_cluster_trades),
        "rapid_burst_trade_ratio": float(rapid_cluster_trades / trade_count),
        "median_gap_seconds": float(median_gap),
    }


def _accumulate_rapid_cluster_stats(
    cluster: List[Dict[str, Any]],
    rapid_cluster_count: int,
    rapid_cluster_trades: int,
) -> tuple[int, int]:
    if not cluster:
        return rapid_cluster_count, rapid_cluster_trades
    trade_count = len(cluster)
    started_ts = _safe_int(cluster[0].get("timestamp"))
    ended_ts = _safe_int(cluster[-1].get("timestamp"))
    span_seconds = max(0, ended_ts - started_ts)
    unique_sizes = len({round(_safe_float(row.get("size")), 2) for row in cluster})
    rapid_span_ceiling = min(EXEC_CLUSTER_MAX_SPAN_SECONDS, 600)
    is_rapid = (
        trade_count >= EXEC_RAPID_MIN_TRADES
        and span_seconds <= rapid_span_ceiling
        and unique_sizes >= EXEC_RAPID_MIN_UNIQUE_SIZES
    )
    if is_rapid:
        rapid_cluster_count += 1
        rapid_cluster_trades += trade_count
    return rapid_cluster_count, rapid_cluster_trades


def _theme_features(
    *,
    trades: List[Dict[str, Any]],
    open_positions: List[Dict[str, Any]],
    total_open_value: float,
) -> Dict[str, float | str]:
    theme_open_value: Dict[str, float] = defaultdict(float)
    theme_markets: Dict[str, set[str]] = defaultdict(set)
    theme_titles: Dict[str, set[str]] = defaultdict(set)
    theme_trade_count: Dict[str, int] = defaultdict(int)

    for row in open_positions:
        title = str(row.get("title") or "")
        key = _normalize_theme_key(title) or _normalize_theme_key(str(row.get("eventSlug") or row.get("slug") or ""))
        if not key:
            continue
        current_value = max(0.0, _safe_float(row.get("currentValue")))
        theme_open_value[key] += current_value
        condition_id = str(row.get("conditionId") or "").strip().lower()
        if condition_id:
            theme_markets[key].add(condition_id)
        if title.strip():
            theme_titles[key].add(title.strip().lower())

    for row in trades:
        title = str(row.get("title") or "")
        key = _normalize_theme_key(title) or _normalize_theme_key(str(row.get("eventSlug") or row.get("slug") or ""))
        if not key:
            continue
        theme_trade_count[key] += 1
        condition_id = str(row.get("conditionId") or "").strip().lower()
        if condition_id:
            theme_markets[key].add(condition_id)
        if title.strip():
            theme_titles[key].add(title.strip().lower())

    top_theme_key = ""
    top_theme_open_value = 0.0
    if theme_open_value:
        top_theme_key = max(theme_open_value, key=theme_open_value.get)
        top_theme_open_value = float(theme_open_value[top_theme_key])
    elif theme_trade_count:
        top_theme_key = max(theme_trade_count, key=theme_trade_count.get)
        top_theme_open_value = 0.0

    open_share = (top_theme_open_value / total_open_value) if total_open_value > 0 else 0.0
    top_theme_market_count = len(theme_markets.get(top_theme_key, set())) if top_theme_key else 0
    top_theme_title_count = len(theme_titles.get(top_theme_key, set())) if top_theme_key else 0
    top_theme_trade_count = int(theme_trade_count.get(top_theme_key, 0)) if top_theme_key else 0
    top_theme_label = _human_theme(top_theme_key) if top_theme_key else ""

    return {
        "top_theme": top_theme_label,
        "top_theme_open_share": float(open_share),
        "top_theme_market_count": float(top_theme_market_count),
        "top_theme_title_count": float(top_theme_title_count),
        "top_theme_trade_count": float(top_theme_trade_count),
    }


# ── Wallet quality hard filters (Task 2) ──────────────────────────────
GATE0_MIN_LIFETIME_PNL = float(os.getenv("GATE0_MIN_LIFETIME_PNL", "-10000"))
WALLET_MIN_CLOSED_SAMPLE = int(os.getenv("WALLET_MIN_CLOSED_SAMPLE", "10"))
MARKET_MAKER_BOTH_SIDES_THRESHOLD = float(os.getenv("MARKET_MAKER_BOTH_SIDES_THRESHOLD", "0.50"))


def _detect_market_maker(positions: List[Dict[str, Any]]) -> bool:
    """Detect if a wallet is a market maker by checking both-sides presence.

    A wallet is classified as a market maker if it holds BOTH YES and NO
    outcome tokens on more than 50% of its distinct markets (conditionIds).
    """
    if not positions:
        return False
    cid_outcomes: Dict[str, set] = defaultdict(set)
    for pos in positions:
        cid = str(pos.get("conditionId") or "").strip().lower()
        outcome = str(pos.get("outcome") or "").strip().lower()
        if cid and outcome:
            cid_outcomes[cid].add(outcome)
    if not cid_outcomes:
        return False
    both_sides_count = sum(1 for v in cid_outcomes.values() if len(v) >= 2)
    total_markets = len(cid_outcomes)
    return (both_sides_count / total_markets) > MARKET_MAKER_BOTH_SIDES_THRESHOLD


def _wallet_passes_hard_filters(
    *,
    lb_pnl: float | None,
    closed_sample_size: int,
    market_maker_detected: bool,
) -> Dict[str, Any]:
    """Evaluate hard pass/fail filters before wallet quality scoring.

    Returns a dict with:
      passes: bool          - True if wallet passes all hard filters
      eligible: int         - 1 if eligible, 0 if not
      fail_reason: str|None - human-readable reason if not eligible
      gate0_status: str     - "passed", "failed", or "unknown"
    """
    # Gate 0: lifetime P&L floor
    if lb_pnl is not None and lb_pnl < GATE0_MIN_LIFETIME_PNL:
        return {
            "passes": False,
            "eligible": 0,
            "fail_reason": f"gate0: lb_pnl=${lb_pnl:.0f} < ${GATE0_MIN_LIFETIME_PNL:.0f}",
            "gate0_status": "failed",
        }

    # Determine gate0 status for wallets that pass
    gate0_status = "passed" if lb_pnl is not None else "unknown"

    # Market maker override
    if market_maker_detected:
        return {
            "passes": False,
            "eligible": 0,
            "fail_reason": "market_maker: both-sides detected",
            "gate0_status": gate0_status,
        }

    # Minimum evidence floor
    if closed_sample_size < WALLET_MIN_CLOSED_SAMPLE:
        return {
            "passes": False,
            "eligible": 0,
            "fail_reason": f"evidence: {closed_sample_size} closed < {WALLET_MIN_CLOSED_SAMPLE} minimum",
            "gate0_status": gate0_status,
        }

    return {"passes": True, "eligible": 1, "fail_reason": None, "gate0_status": gate0_status}


# ── Wallet quality score computation (Task 3) ─────────────────────────


def _compute_wallet_quality_score(
    *,
    lb_pnl: float | None,
    closed_realized_pnl: float | None,
    open_cash_pnl: float | None,
    qualifying_roi: float | None,
    closed_sample_size: int,
    total_capital_deployed: float | None,
) -> float:
    """Compute wallet quality score (0-100) for wallet selection.

    Weighting:
      45% lb_pnl (lifetime profitability - dominant signal)
      20% net P&L health (closed + open, drawdown correction)
      15% qualifying_roi (capital efficiency)
      20% sample/confidence (sample size, capital deployed)

    This score is used for WALLET SELECTION only.
    Position-level insider_score remains separate for entry timing.
    """
    # -- 1. Lifetime P&L component (45%) ------------------------------------
    pnl_val = lb_pnl if lb_pnl is not None else (closed_realized_pnl or 0)
    if pnl_val <= 0:
        pnl_score = 0.0
    elif pnl_val >= 200_000:
        pnl_score = 90.0 + min(10.0, (pnl_val - 200_000) / 100_000)
    elif pnl_val >= 50_000:
        pnl_score = 50.0 + (pnl_val - 50_000) / 150_000 * 40.0
    elif pnl_val >= 10_000:
        pnl_score = 20.0 + (pnl_val - 10_000) / 40_000 * 30.0
    elif pnl_val >= 1_000:
        pnl_score = 5.0 + (pnl_val - 1_000) / 9_000 * 15.0
    else:
        pnl_score = pnl_val / 1_000 * 5.0
    pnl_component = min(100.0, max(0.0, pnl_score)) * 0.45

    # -- 2. Net P&L health adjustment (20%) ---------------------------------
    realized = closed_realized_pnl or 0
    open_pnl = open_cash_pnl or 0
    if realized > 0 and open_pnl < 0:
        drawdown_ratio = abs(open_pnl) / max(realized, 1)
        health_score = max(0.0, 100.0 * (1.0 - drawdown_ratio))
    elif realized <= 0:
        health_score = max(0.0, min(100.0, 50.0 + open_pnl / 1000))
    else:
        health_score = 100.0
    health_component = min(100.0, max(0.0, health_score)) * 0.20

    # -- 3. Qualifying ROI (15%) --------------------------------------------
    roi_val = qualifying_roi or 0
    if roi_val >= 30:
        roi_score = 100.0
    elif roi_val >= 15:
        roi_score = 60.0 + (roi_val - 15) / 15 * 40.0
    elif roi_val >= 5:
        roi_score = 20.0 + (roi_val - 5) / 10 * 40.0
    elif roi_val > 0:
        roi_score = roi_val / 5 * 20.0
    else:
        roi_score = 0.0
    roi_component = min(100.0, max(0.0, roi_score)) * 0.15

    # -- 4. Sample/confidence (20%) -----------------------------------------
    sample_score = 0.0
    if closed_sample_size >= 200:
        sample_score += 50.0
    elif closed_sample_size >= 50:
        sample_score += 20.0 + (closed_sample_size - 50) / 150 * 30.0
    elif closed_sample_size >= 10:
        sample_score += (closed_sample_size - 10) / 40 * 20.0

    capital = total_capital_deployed or 0
    if capital >= 1_000_000:
        sample_score += 50.0
    elif capital >= 100_000:
        sample_score += 20.0 + (capital - 100_000) / 900_000 * 30.0
    elif capital >= 10_000:
        sample_score += (capital - 10_000) / 90_000 * 20.0

    confidence_component = min(100.0, max(0.0, sample_score)) * 0.20

    # -- Final score --------------------------------------------------------
    total = pnl_component + health_component + roi_component + confidence_component
    # Negative P&L penalty: if the primary P&L signal is non-positive, cap the
    # total score heavily.  Confidence/health alone cannot salvage a losing wallet
    # for copy-trading purposes.
    if pnl_val <= 0:
        total = min(total, 5.0)
    return round(max(0.0, min(100.0, total)), 2)


def _compute_wallet_edge_tier(
    *,
    win_rate: float,
    closed_sample_size: int,
    qualifying_roi: float | None,
    speculator_score: float,
) -> str:
    """Bucket historical wallet edge independent of classification label."""
    qr = qualifying_roi if qualifying_roi is not None else 0.0

    if closed_sample_size < 5:
        return "unscored"

    wr_pts = max(0, min(35, (win_rate - 0.50) / 0.40 * 35))
    roi_pts = max(0, min(30, qr / 40.0 * 30))
    sample_pts = max(0, min(15, (closed_sample_size - 5) / 45 * 15))

    quality = wr_pts + roi_pts + sample_pts

    if quality >= 65:
        return "elite"
    if quality >= 45:
        return "strong"
    if quality >= 25:
        return "moderate"
    return "marginal"


def _has_followability_inputs(
    qualifying_roi: float | None,
    closed_sample_size: int,
    qualifying_capital: float,
    win_rate: float,
    lb_pnl: float | None = None,
    closed_realized_pnl: float | None = None,
) -> bool:
    """Return True if inputs are sufficient to compute a meaningful followability score.

    When strong P&L evidence exists (lb_pnl or closed_realized_pnl > $5K),
    the behavioral gates (win_rate, roi, capital) are relaxed — a wallet with
    $500K lifetime P&L should always receive a score regardless of win rate.
    The hard floor is closed_sample_size >= 5 (need some trade history).
    """
    # Hard floor: need at least a few closed trades regardless of P&L data.
    if closed_sample_size < 5:
        return False

    # When we have reliable P&L data, skip the behavioral gates entirely.
    best_pnl = lb_pnl if lb_pnl is not None else closed_realized_pnl
    if best_pnl is not None and best_pnl > 5_000:
        return True

    # Without P&L data, require the original behavioral gates.
    if qualifying_roi is None or qualifying_roi <= 0:
        return False
    if closed_sample_size < 20:
        return False
    if qualifying_capital < 5000 or qualifying_capital > 250000:
        return False
    if win_rate < 0.55:
        return False
    return True


def _classify_strategy(
    *,
    position_count: int,
    trade_count: int,
    market_diversity_14d: int,
    concentration_ratio: float,
    activity: Dict[str, float],
    theme: Dict[str, float | str],
) -> tuple[str, float, str]:
    rapid_ratio = float(activity.get("rapid_burst_trade_ratio") or 0.0)
    max_trades_60s = int(activity.get("max_trades_60s") or 0)
    median_gap = float(activity.get("median_gap_seconds") or 0.0)
    top_theme = str(theme.get("top_theme") or "")
    top_theme_open_share = float(theme.get("top_theme_open_share") or 0.0)
    top_theme_market_count = int(theme.get("top_theme_market_count") or 0.0)
    top_theme_title_count = int(theme.get("top_theme_title_count") or 0.0)

    if (
        trade_count >= STRAT_EXEC_MIN_TRADE_COUNT
        and rapid_ratio >= STRAT_EXEC_MIN_BURST_RATIO
        and max_trades_60s >= STRAT_EXEC_MIN_MAX_TRADES_60S
    ):
        confidence = min(98.0, 58.0 + (rapid_ratio * 45.0) + min(20.0, max_trades_60s))
        note = (
            f"Execution bursts dominate: {rapid_ratio:.0%} of trades are rapid clusters; "
            f"max {max_trades_60s} trades/60s; median gap {median_gap:.1f}s"
        )
        return "execution_bot", confidence, note

    if (
        top_theme_open_share >= STRAT_THEME_MIN_OPEN_SHARE
        and top_theme_market_count >= STRAT_THEME_MIN_MARKETS
        and top_theme_title_count >= STRAT_THEME_MIN_MARKETS
    ):
        confidence = min(
            96.0,
            56.0 + (top_theme_open_share * 40.0) + min(16.0, top_theme_market_count * 2.0),
        )
        theme_display = top_theme or "single_theme"
        note = (
            f"Theme rollover exposure: '{theme_display}' has {top_theme_open_share:.0%} of open value "
            f"across {top_theme_market_count} markets ({top_theme_title_count} title variants)"
        )
        return "theme_roller", confidence, note

    if (
        concentration_ratio >= STRAT_SINGLE_THESIS_MIN_CONCENTRATION
        and market_diversity_14d <= 3
        and position_count <= 7
    ):
        confidence = min(93.0, 52.0 + (concentration_ratio * 45.0))
        note = (
            f"Single-thesis concentration: top position is {concentration_ratio:.0%} of open value "
            f"with low 14d market diversity ({market_diversity_14d})"
        )
        return "single_thesis", confidence, note

    if market_diversity_14d >= STRAT_DIVERSIFIED_MIN_MARKET_DIVERSITY and position_count >= 8:
        confidence = min(88.0, 48.0 + min(30.0, (market_diversity_14d * 2.2)))
        note = (
            f"Diversified scanner: broad 14d market diversity ({market_diversity_14d}) "
            f"across {position_count} open positions"
        )
        return "diversified_scanner", confidence, note

    note = (
        f"Balanced/mixed behavior: concentration {concentration_ratio:.0%}, "
        f"rapid ratio {rapid_ratio:.0%}, 14d diversity {market_diversity_14d}"
    )
    return "balanced_mixed", 45.0, note


def _tier(score: float, max_position_value: float, total_open_value: float) -> str:
    # Downgrade tiny-capital wallets so "watch" reflects meaningful exposure.
    if (
        total_open_value < MIN_WATCH_TOTAL_OPEN_VALUE
        and max_position_value < MIN_WATCH_TOP_POSITION_VALUE
    ):
        return "low"

    if score >= 85:
        return "high"
    if score >= 70:
        return "medium"
    if score >= 55:
        return "watch"
    return "low"


def _closed_outcome_metrics(
    closed_positions: List[Dict[str, Any]],
    open_positions: List[Dict[str, Any]] | None = None,
) -> Dict[str, float]:
    """Compute win/loss metrics from closed positions and effectively-resolved open positions.

    Win rate is calculated ONLY from positions entered below avgPrice < 0.80.
    Positions entered at >= 0.80 are excluded from win/loss counting because:
      - They are near-resolution bets with little informational content
      - A 0.95 → 1.0 outcome is trivially predictable; it inflates win rate
      - A 0.90 → 0.10 loss is catastrophic; it deflates win rate unfairly

    Effectively-resolved open positions:
      Some positions are still technically "open" (Polymarket hasn't closed them yet)
      but the market has clearly resolved — curPrice >= 0.97 (YES resolved) or
      curPrice <= 0.02 (NO resolved).  These count as wins/losses in the scored sample
      provided their entry_price < 0.80.  The 0.97 / 0.02 thresholds are tight
      enough to exclude genuine low-probability open markets (e.g. 0.04 on a long-shot)
      and only capture truly resolved outcomes.

    Resolution rate: % of ALL closed positions that resolved at curPrice >= 0.95
    (true YES win) or curPrice <= 0.05 (true NO win) vs sold early.
    Open positions are NOT included in resolution rate — only officially closed ones.
    """
    open_positions = open_positions or []

    ENTRY_PRICE_GATE = 0.80     # only count wr from positions entered below this
    RESOLUTION_WIN_EPS  = 0.95  # closed curPrice >= this = held to resolution (win)
    RESOLUTION_LOSS_EPS = 0.05  # closed curPrice <= this = held to resolution (loss)
    EFFECTIVE_WIN_PRICE  = 0.97  # open curPrice >= this = effectively resolved YES
    EFFECTIVE_LOSS_PRICE = 0.02  # open curPrice <= this = effectively resolved NO

    wins = 0
    losses = 0
    total_closed = len(closed_positions)
    resolved_count = 0  # held to full resolution (win OR loss) — closed positions only

    # Qualifying ROI tracking: PnL and capital for ONLY sub-ENTRY_PRICE_GATE positions
    # These are the only positions where we have genuine informational content to measure.
    # We use realizedPnl (provided by Polymarket) and initialValue (total capital deployed
    # into that position including all re-buys) as the cost basis.
    qualifying_pnl = 0.0
    qualifying_capital = 0.0

    # --- Closed positions ---
    for row in closed_positions:
        avg_price = _safe_float(row.get("avgPrice"))
        cur_price = _optional_float(row.get("curPrice"))
        realized  = _safe_float(row.get("realizedPnl"))

        # Resolution rate: was this position held until market resolved?
        if cur_price is not None:
            if cur_price >= RESOLUTION_WIN_EPS or cur_price <= RESOLUTION_LOSS_EPS:
                resolved_count += 1

        # Win/loss rate + qualifying ROI: only count entry-price-gated positions
        if avg_price >= ENTRY_PRICE_GATE:
            continue  # exclude — buying certainty, not predicting outcomes

        if realized > 0:
            wins += 1
        elif realized < 0:
            losses += 1

        # Track qualifying capital: use totalBought if available (actual USDC spent),
        # fall back to initialValue, fall back to avg_price * size estimate.
        # totalBought is the most reliable for closed positions that were held to resolution.
        total_bought = _safe_float(row.get("totalBought"))
        initial_value = _safe_float(row.get("initialValue"))
        size = _safe_float(row.get("size"))
        cap = total_bought if total_bought > 0 else (initial_value if initial_value > 0 else avg_price * size)
        if cap > 0:
            qualifying_capital += cap
            qualifying_pnl     += realized

    # --- Effectively-resolved open positions ---
    # Open positions whose market has clearly resolved but Polymarket hasn't
    # officially closed them yet.  Count only sub-0.80 entries — same gate as above.
    effective_wins   = 0
    effective_losses = 0
    for row in open_positions:
        avg_price = _safe_float(row.get("avgPrice"))
        cur_price = _optional_float(row.get("curPrice"))

        if avg_price >= ENTRY_PRICE_GATE:
            continue  # same entry-price gate

        if cur_price is None:
            continue

        if cur_price >= EFFECTIVE_WIN_PRICE:
            effective_wins += 1
            wins += 1
            # Count open effectively-resolved as effectively realized
            cash_pnl = _safe_float(row.get("cashPnl"))
            current_val = _safe_float(row.get("currentValue"))
            initial_val = _safe_float(row.get("initialValue"))
            total_bought = _safe_float(row.get("totalBought"))
            cap = total_bought if total_bought > 0 else (initial_val if initial_val > 0 else 0.0)
            if cap > 0:
                qualifying_capital += cap
                qualifying_pnl     += cash_pnl if cash_pnl != 0.0 else (current_val - cap)
        elif cur_price <= EFFECTIVE_LOSS_PRICE:
            effective_losses += 1
            losses += 1
            cash_pnl = _safe_float(row.get("cashPnl"))
            initial_val = _safe_float(row.get("initialValue"))
            total_bought = _safe_float(row.get("totalBought"))
            cap = total_bought if total_bought > 0 else (initial_val if initial_val > 0 else 0.0)
            if cap > 0:
                qualifying_capital += cap
                qualifying_pnl     += cash_pnl if cash_pnl != 0.0 else -cap

    sample = wins + losses
    win_rate = (wins / sample) if sample > 0 else 0.0
    resolution_rate = (resolved_count / total_closed) if total_closed > 0 else 0.0
    high_winrate_qualified = sample >= WINRATE_MIN_SAMPLE and win_rate >= WINRATE_THRESHOLD

    # Qualifying ROI: return on capital deployed exclusively in sub-0.80 entry positions.
    # This is the only ROI figure we can trust across all wallet types — it uses only
    # the positions meeting our entry criteria, where we have reliable cost basis data.
    # NOTE: For wallets with 0 qualifying capital (e.g., all positions sold mid-market),
    # we cannot compute ROI and return None to prevent division by zero / misleading display.
    qualifying_roi = (qualifying_pnl / qualifying_capital * 100.0) if qualifying_capital > 0 else None

    return {
        "wins": float(wins),
        "losses": float(losses),
        "closed_sample_size": float(sample),
        "win_rate": float(win_rate),
        "high_winrate_qualified": 1.0 if high_winrate_qualified else 0.0,
        "total_closed_count": float(total_closed),
        "effective_resolved_wins":   float(effective_wins),
        "effective_resolved_losses": float(effective_losses),
        "resolution_rate": float(resolution_rate),
        "qualifying_pnl":     qualifying_pnl,
        "qualifying_capital": qualifying_capital,
        "qualifying_roi":     qualifying_roi,
    }


def _min_positive_ts(rows: List[Dict[str, Any]]) -> int:
    min_ts = 0
    for row in rows:
        ts = _safe_int(row.get("timestamp"))
        if ts <= 0:
            continue
        if min_ts <= 0 or ts < min_ts:
            min_ts = ts
    return min_ts


def _extract_profile_payload(
    *,
    wallet: str,
    activity_rows: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    existing_profile: Dict[str, Any],
) -> Dict[str, str]:
    username = str(existing_profile.get("username") or "").strip()
    pseudonym = str(existing_profile.get("pseudonym") or "").strip()
    profile_image = str(existing_profile.get("profile_image") or "").strip()
    profile_image_optimized = str(existing_profile.get("profile_image_optimized") or "").strip()
    bio = str(existing_profile.get("bio") or "").strip()

    sources = activity_rows + trades
    for row in sources:
        if not username:
            username = str(row.get("name") or "").strip()
        if not pseudonym:
            pseudonym = str(row.get("pseudonym") or "").strip()
        if not profile_image:
            profile_image = str(row.get("profileImage") or "").strip()
        if not profile_image_optimized:
            profile_image_optimized = str(row.get("profileImageOptimized") or "").strip()
        if not bio:
            bio = str(row.get("bio") or "").strip()

    return {
        "wallet": wallet,
        "username": username,
        "pseudonym": pseudonym,
        "profile_image": profile_image,
        "profile_image_optimized": profile_image_optimized,
        "bio": bio,
    }


def _recent_trade_notional_features(
    trades: List[Dict[str, Any]],
    *,
    now_ts: int,
    lookback_days: int,
    serious_notional: float,
) -> Dict[str, float]:
    cutoff_ts = int(now_ts) - int(lookback_days) * 86400
    notionals: List[float] = []
    serious_count = 0
    max_notional = 0.0

    for row in trades:
        ts = _safe_int(row.get("timestamp"))
        if ts <= 0 or ts < cutoff_ts:
            continue
        price = _safe_float(row.get("price"))
        size = _safe_float(row.get("size"))
        notional = _safe_float(row.get("notional"), price * size)
        if notional <= 0:
            notional = price * size
        if notional <= 0:
            continue
        notionals.append(notional)
        if notional > max_notional:
            max_notional = notional
        if notional >= serious_notional:
            serious_count += 1

    if notionals:
        ordered = sorted(notionals)
        mid = len(ordered) // 2
        if len(ordered) % 2 == 1:
            median_notional = float(ordered[mid])
        else:
            median_notional = float((ordered[mid - 1] + ordered[mid]) / 2.0)
    else:
        median_notional = 0.0

    return {
        "serious_trade_count_14d": float(serious_count),
        "median_trade_notional_14d": float(median_notional),
        "max_trade_notional_14d": float(max_notional),
    }


def _normalize_position_key(condition_id: Any, outcome: Any) -> tuple[str, str]:
    return (str(condition_id or "").strip().lower(), str(outcome or "").strip().lower())


def _matching_position_trades(
    trades: List[Dict[str, Any]],
    *,
    condition_id: str,
    outcome: str,
    sides: set[str] | None = None,
) -> List[Dict[str, Any]]:
    condition_key, outcome_key = _normalize_position_key(condition_id, outcome)
    allowed_sides = {str(side or "").strip().upper() for side in (sides or {"BUY", "SELL"})}
    if not allowed_sides:
        allowed_sides = {"BUY", "SELL"}

    out: List[Dict[str, Any]] = []
    for row in trades:
        row_condition, row_outcome = _normalize_position_key(
            row.get("conditionId") or row.get("market"),
            row.get("outcome"),
        )
        if row_condition != condition_key:
            continue
        if outcome_key and row_outcome and row_outcome != outcome_key:
            continue
        side = str(row.get("side") or "BUY").strip().upper()
        if side not in allowed_sides:
            continue
        ts = _safe_int(row.get("timestamp"))
        if ts <= 0:
            continue
        out.append(row)
    out.sort(key=lambda r: _safe_int(r.get("timestamp")))
    return out


def _matching_position_buy_trades(
    trades: List[Dict[str, Any]],
    *,
    condition_id: str,
    outcome: str,
) -> List[Dict[str, Any]]:
    return _matching_position_trades(
        trades,
        condition_id=condition_id,
        outcome=outcome,
        sides={"BUY"},
    )


def _max_trades_in_window(timestamps: List[int], window_seconds: int = 60) -> int:
    if not timestamps:
        return 0
    ordered = sorted(ts for ts in timestamps if ts > 0)
    if not ordered:
        return 0
    max_count = 0
    window: deque[int] = deque()
    for ts in ordered:
        window.append(ts)
        while window and ts - window[0] > window_seconds:
            window.popleft()
        if len(window) > max_count:
            max_count = len(window)
    return max_count


def _classify_position_activity_state(
    *,
    trades_24h: int,
    buys_24h: int,
    sells_24h: int,
    net_notional_24h: float,
) -> str:
    if trades_24h <= 0:
        return "holding"
    if net_notional_24h > 0 and buys_24h > sells_24h:
        return "accumulating"
    if net_notional_24h < 0 and sells_24h > buys_24h:
        return "distributing"
    return "balanced"


def _around_position_metrics(
    matching_trades: List[Dict[str, Any]],
    *,
    anchor_ts: int,
    now_ts: int | None = None,
) -> Dict[str, float]:
    latest_trade_ts = max((_safe_int(row.get("timestamp")) for row in matching_trades), default=0)
    if now_ts is None:
        now_ts = int(time.time())
    hold_hours = 99999.0
    if latest_trade_ts > 0:
        hold_hours = max(0.0, float(now_ts - latest_trade_ts) / 3600.0)

    if anchor_ts <= 0 or not matching_trades:
        activity_state = _classify_position_activity_state(
            trades_24h=0,
            buys_24h=0,
            sells_24h=0,
            net_notional_24h=0.0,
        )
        return {
            "buys_5m": 0.0,
            "buys_1h": 0.0,
            "buys_24h": 0.0,
            "trades_5m": 0.0,
            "trades_1h": 0.0,
            "trades_24h": 0.0,
            "sells_5m": 0.0,
            "sells_1h": 0.0,
            "sells_24h": 0.0,
            "notional_5m": 0.0,
            "notional_1h": 0.0,
            "notional_24h": 0.0,
            "sell_notional_5m": 0.0,
            "sell_notional_1h": 0.0,
            "sell_notional_24h": 0.0,
            "buys_since_entry": 0.0,
            "sells_since_entry": 0.0,
            "buy_notional_since_entry": 0.0,
            "sell_notional_since_entry": 0.0,
            "net_notional_24h": 0.0,
            "net_notional_since_entry": 0.0,
            "last_trade_side": "",
            "activity_state": activity_state,
            "hold_hours": float(hold_hours),
            "unique_sizes_5m": 0.0,
            "max_trades_60s": float(_max_trades_in_window([])),
            "latest_trade_ts": float(latest_trade_ts),
        }

    buys_5m = 0
    buys_1h = 0
    buys_24h = 0
    trades_5m = 0
    trades_1h = 0
    trades_24h = 0
    sells_5m = 0
    sells_1h = 0
    sells_24h = 0
    notional_5m = 0.0
    notional_1h = 0.0
    notional_24h = 0.0
    sell_notional_5m = 0.0
    sell_notional_1h = 0.0
    sell_notional_24h = 0.0
    buys_since_entry = 0
    sells_since_entry = 0
    buy_notional_since_entry = 0.0
    sell_notional_since_entry = 0.0
    sizes_5m: set[float] = set()
    timestamps: List[int] = []
    last_trade_side = ""

    for row in matching_trades:
        ts = _safe_int(row.get("timestamp"))
        if ts <= 0:
            continue
        timestamps.append(ts)
        if ts >= latest_trade_ts:
            last_trade_side = str(row.get("side") or "BUY").strip().upper()

        side = str(row.get("side") or "BUY").strip().upper()
        if side not in {"BUY", "SELL"}:
            side = "BUY"

        price = _safe_float(row.get("price"))
        size = _safe_float(row.get("size"))
        notional = _safe_float(row.get("notional"), price * size)
        if notional <= 0:
            notional = price * size

        # Use recency from now (Option C): how recently did this trade happen?
        # This answers "is this wallet currently active?" not "did they trade near entry?"
        recency = now_ts - ts  # seconds ago (positive = in the past)
        if recency < 0:
            recency = 0  # future timestamps treated as now
        if recency <= POSITION_WINDOW_24H:
            trades_24h += 1
            if side == "BUY":
                buys_24h += 1
                notional_24h += notional
            else:
                sells_24h += 1
                sell_notional_24h += notional
        if recency <= POSITION_WINDOW_1H:
            trades_1h += 1
            if side == "BUY":
                buys_1h += 1
                notional_1h += notional
            else:
                sells_1h += 1
                sell_notional_1h += notional
        if recency <= POSITION_WINDOW_5M:
            trades_5m += 1
            if side == "BUY":
                buys_5m += 1
                notional_5m += notional
            else:
                sells_5m += 1
                sell_notional_5m += notional
            sizes_5m.add(round(size, 4))

        if ts >= anchor_ts:
            if side == "BUY":
                buys_since_entry += 1
                buy_notional_since_entry += notional
            else:
                sells_since_entry += 1
                sell_notional_since_entry += notional

    net_notional_24h = notional_24h - sell_notional_24h
    net_notional_since_entry = buy_notional_since_entry - sell_notional_since_entry
    activity_state = _classify_position_activity_state(
        trades_24h=trades_24h,
        buys_24h=buys_24h,
        sells_24h=sells_24h,
        net_notional_24h=net_notional_24h,
    )

    return {
        "buys_5m": float(buys_5m),
        "buys_1h": float(buys_1h),
        "buys_24h": float(buys_24h),
        "trades_5m": float(trades_5m),
        "trades_1h": float(trades_1h),
        "trades_24h": float(trades_24h),
        "sells_5m": float(sells_5m),
        "sells_1h": float(sells_1h),
        "sells_24h": float(sells_24h),
        "notional_5m": float(notional_5m),
        "notional_1h": float(notional_1h),
        "notional_24h": float(notional_24h),
        "sell_notional_5m": float(sell_notional_5m),
        "sell_notional_1h": float(sell_notional_1h),
        "sell_notional_24h": float(sell_notional_24h),
        "buys_since_entry": float(buys_since_entry),
        "sells_since_entry": float(sells_since_entry),
        "buy_notional_since_entry": float(buy_notional_since_entry),
        "sell_notional_since_entry": float(sell_notional_since_entry),
        "net_notional_24h": float(net_notional_24h),
        "net_notional_since_entry": float(net_notional_since_entry),
        "last_trade_side": str(last_trade_side or ""),
        "activity_state": activity_state,
        "hold_hours": float(hold_hours),
        "unique_sizes_5m": float(len(sizes_5m)),
        "max_trades_60s": float(_max_trades_in_window(timestamps)),
        "latest_trade_ts": float(latest_trade_ts),
    }


def _score_position_speculator(
    *,
    buys_5m: int,
    trades_5m: int | None = None,
    buys_1h: int,
    unique_sizes_5m: int,
    max_trades_60s: int,
    notional_5m: float,
    buy_notional_24h: float = 0.0,
    sell_notional_24h: float = 0.0,
    position_value: float,
) -> float:
    score = 0.0
    burst_5m = int(trades_5m if trades_5m is not None else buys_5m)

    if burst_5m >= 12:
        score += 44
    elif burst_5m >= 8:
        score += 34
    elif burst_5m >= 5:
        score += 26
    elif burst_5m >= 3:
        score += 16
    elif buys_5m >= 2:
        score += 8
    elif buys_5m >= 1:
        score += 4

    if buys_5m >= 10:
        score += 42
    elif buys_5m >= 6:
        score += 32
    elif buys_5m >= 4:
        score += 24
    elif buys_5m >= 2:
        score += 12

    if max_trades_60s >= 10:
        score += 28
    elif max_trades_60s >= 7:
        score += 21
    elif max_trades_60s >= 4:
        score += 12
    elif max_trades_60s >= 3:
        score += 7

    if unique_sizes_5m >= 6:
        score += 16
    elif unique_sizes_5m >= 4:
        score += 11
    elif unique_sizes_5m >= 2:
        score += 6

    if buys_1h >= 16:
        score += 12
    elif buys_1h >= 10:
        score += 8
    elif buys_1h >= 6:
        score += 4

    five_min_notional_ratio = (notional_5m / position_value) if position_value > 0 else 0.0
    if five_min_notional_ratio >= 1.0:
        score += 14
    elif five_min_notional_ratio >= 0.6:
        score += 10
    elif five_min_notional_ratio >= 0.35:
        score += 6

    churn_ratio = ((buy_notional_24h + sell_notional_24h) / position_value) if position_value > 0 else 0.0
    if churn_ratio >= POSITION_CHURN_HIGH_RATIO:
        score += 16
    elif churn_ratio >= POSITION_CHURN_MED_RATIO:
        score += 10
    elif churn_ratio >= 0.45:
        score += 4

    return max(0.0, min(score, 100.0))


def _score_position_insider(
    *,
    position_value: float,
    position_share: float,
    position_count_wallet: int,
    low_buy_hits_14d: int,
    is_fresh_account: bool,
    win_rate: float,
    closed_sample_size: int,
    high_winrate_qualified: bool,
    buys_1h: int,
    buys_5m: int,
    buys_24h: int,
    net_notional_24h: float = 0.0,
    churn_ratio: float = 0.0,
    max_trades_60s: int,
    position_speculator_score: float,
    days_to_resolution: float = 999.0,
    is_negative_risk: bool = False,
    wallet_insider_score: float = 0.0,
) -> float:
    score = 0.0

    if position_value >= 50000:
        score += 24
    elif position_value >= 20000:
        score += 20
    elif position_value >= 10000:
        score += 16
    elif position_value >= 5000:
        score += 12
    elif position_value >= POSITION_MIN_OPEN_VALUE:
        score += 8

    if position_share >= 0.80:
        score += 20
    elif position_share >= 0.60:
        score += 15
    elif position_share >= 0.45:
        score += 11
    elif position_share >= 0.30:
        score += 7

    if position_count_wallet <= 3:
        score += 12
    elif position_count_wallet <= INSIDER_MAX_POSITIONS:
        score += 8
    elif position_count_wallet <= 8:
        score += 3
    else:
        score -= 6

    if low_buy_hits_14d >= 8:
        score += 10
    elif low_buy_hits_14d >= 5:
        score += 8
    elif low_buy_hits_14d >= 3:
        score += 5

    if is_fresh_account:
        score += 8

    # === ENHANCED WIN RATE SCORING (Optimized for $200 bankroll) ===
    # high_winrate_qualified = sample >= WINRATE_MIN_SAMPLE (10) AND win_rate >= WINRATE_THRESHOLD (0.65)
    if high_winrate_qualified:
        score += 20  # Was 12 - win rate is critical for small bankroll survival
    elif closed_sample_size >= 5 and win_rate >= 0.65:
        score += 15  # Smaller sample but still strong win rate
    elif closed_sample_size >= 10 and win_rate >= 0.60:
        score += 8  # Decent win rate with good sample

    # === TRACK RECORD OVERRIDE ===
    # The momentum-based scoring above misses proven performers who aren't
    # actively buying. RIU8 (100% WR, 186 samples, $80K PnL) scores 21 because
    # they have 0 recent low-price buys. bcda ($15.8M PnL) scores 42.
    #
    # 263 wallets with WR>=70%, 15+ samples, $5K+ PnL are invisible to our system.
    # Their combined PnL is $483M — vs $52M for wallets we CAN see.
    #
    # Fix: if the historical sample proves edge, guarantee a floor score regardless of
    # momentum signals. This doesn't replace the momentum scoring — it ensures
    # genuinely skilled wallets can't be scored below 70.
    if (
        high_winrate_qualified
        and closed_sample_size >= 15
        and win_rate >= 0.70
    ):
        # Scale bonus by evidence strength
        sample_bonus = min(15, closed_sample_size // 10)  # +1 per 10 samples, cap 15
        wr_bonus = min(15, int((win_rate - 0.70) * 50))    # +5 per 10% above 70%, cap 15
        history_floor = 70 + sample_bonus + wr_bonus
        if score < history_floor:
            score = history_floor

    # === WALLET PEDIGREE BONUS (legacy — still useful for moderate cases) ===
    if high_winrate_qualified and wallet_insider_score >= 80 and closed_sample_size >= 20:
        score += 12
    elif high_winrate_qualified and wallet_insider_score >= 70 and closed_sample_size >= 15:
        score += 6

    # Moderate accumulation around entry can be informative.
    if 2 <= buys_1h <= 8 and max_trades_60s <= 4:
        score += 9
    elif buys_1h >= 2:
        score += 4
    if buys_24h >= 4 and buys_5m <= 3:
        score += 4
    if net_notional_24h > 0:
        score += 4
    elif net_notional_24h < 0:
        score -= 6

    if position_speculator_score >= 80:
        score -= 20
    elif position_speculator_score >= POSITION_SPECULATOR_THRESHOLD:
        score -= 12
    elif position_speculator_score >= 50:
        score -= 6

    if max_trades_60s >= 8:
        score -= 10
    elif max_trades_60s >= 5:
        score -= 6

    if buys_5m >= 8:
        score -= 10
    elif buys_5m >= 5:
        score -= 6
    if churn_ratio >= POSITION_CHURN_HIGH_RATIO:
        score -= 12
    elif churn_ratio >= POSITION_CHURN_MED_RATIO:
        score -= 7

    # === RESOLUTION TIMING SIGNAL ===
    # The closer a bet is placed to resolution, the stronger the insider signal.
    # A bet placed hours before a surprise outcome is the strongest signal in Polymarket.
    # days_to_resolution: days until market end (999 = unknown, negative = market past end date)
    if days_to_resolution <= 0:
        # Market is past its end date — cannot be traded; do not reward.
        # We cannot distinguish winner from loser at this stage without outcome data.
        pass
    elif days_to_resolution <= 1:  # < 24 hours to resolution
        score += 18
    elif days_to_resolution <= 3:
        score += 12
    elif days_to_resolution <= 7:
        score += 6
    elif days_to_resolution <= 14:
        score += 2
    # else: > 14 days — no bonus (too early to be meaningful)

    # === NEGATIVE RISK PENALTY ===
    # negativeRisk = multi-outcome market (NOT binary YES/NO)
    # These are NOT prediction markets in the classic sense — insider edge is harder to define.
    # Score penalty to deprioritize these positions.
    if is_negative_risk:
        score -= 10

    return max(0.0, min(score, 100.0))


def _classify_position_signal(
    *,
    position_insider_score: float,
    position_speculator_score: float,
    buys_1h: int,
    position_value: float = 0.0,
) -> str:
    insider_high = position_insider_score >= POSITION_SCORE_THRESHOLD
    spec_high = position_speculator_score >= POSITION_SPECULATOR_THRESHOLD
    has_burst_context = buys_1h >= 2
    # Large established positions (>= $10k) are strong signals on their own;
    # they don't need a burst of buys in the last hour to qualify.
    large_position_bypass = position_value >= 10_000.0

    if insider_high and not spec_high and (has_burst_context or large_position_bypass):
        return "position_insider_candidate"
    if spec_high:
        return "position_speculator_ladder"
    if (55.0 <= position_insider_score < POSITION_SCORE_THRESHOLD) or (insider_high and spec_high) or (
        insider_high and not has_burst_context
    ):
        return "position_watch"
    return "position_low_signal"


def _should_suppress_position_insider_candidate(
    *,
    strategy_group: str,
    strategy_confidence: float,
    rapid_burst_trade_ratio: float,
    max_trades_60s: int,
    speculator_score: float,
) -> bool:
    if not POSITION_SUPPRESS_EXEC_BOT_ENABLED:
        return False
    if str(strategy_group or "").strip().lower() != "execution_bot":
        return False
    if strategy_confidence < POSITION_SUPPRESS_EXEC_BOT_MIN_STRATEGY_CONFIDENCE:
        return False

    extreme_signals = 0
    if rapid_burst_trade_ratio >= POSITION_SUPPRESS_EXEC_BOT_MIN_RAPID_BURST_RATIO:
        extreme_signals += 1
    if max_trades_60s >= POSITION_SUPPRESS_EXEC_BOT_MIN_MAX_TRADES_60S:
        extreme_signals += 1
    if speculator_score >= POSITION_SUPPRESS_EXEC_BOT_MIN_SPECULATOR_SCORE:
        extreme_signals += 1
    return extreme_signals >= 2


def _position_signal_reason(
    *,
    position_classification: str,
    position_insider_score: float,
    position_speculator_score: float,
    position_value: float,
    position_share: float,
    buys_5m: int,
    buys_1h: int,
    buys_24h: int,
    sells_5m: int,
    sells_1h: int,
    sells_24h: int,
    trades_5m: int,
    trades_1h: int,
    trades_24h: int,
    notional_1h: float,
    sell_notional_1h: float,
    net_notional_24h: float,
    churn_ratio: float,
    activity_state: str,
    hold_hours: float,
    max_trades_60s: int,
    unique_sizes_5m: int,
) -> str:
    return (
        f"class={position_classification}, insider={position_insider_score:.1f}, "
        f"speculator={position_speculator_score:.1f}, position_value={position_value:.2f}, "
        f"position_share={position_share:.3f}, activity_state={activity_state}, hold_hours={'unknown' if hold_hours >= 99998 else f'{hold_hours:.2f}'}, "
        f"trades_5m={trades_5m}, trades_1h={trades_1h}, trades_24h={trades_24h}, "
        f"buys_5m={buys_5m}, buys_1h={buys_1h}, buys_24h={buys_24h}, "
        f"sells_5m={sells_5m}, sells_1h={sells_1h}, sells_24h={sells_24h}, "
        f"notional_1h={notional_1h:.2f}, sell_notional_1h={sell_notional_1h:.2f}, "
        f"net_notional_24h={net_notional_24h:.2f}, churn_ratio={churn_ratio:.2f}, "
        f"max_trades_60s={max_trades_60s}, unique_sizes_5m={unique_sizes_5m}"
    )


def _score_speculator(
    *,
    trade_count: int,
    strategy_group: str,
    strategy_confidence: float,
    max_trades_60s: int,
    rapid_burst_trade_ratio: float,
    rapid_cluster_count: int,
    rapid_cluster_trades: int,
    median_gap_seconds: float,
) -> Tuple[float, List[str]]:
    score = 0.0
    reasons: List[str] = []

    if rapid_burst_trade_ratio >= 0.60:
        score += 42
        reasons.append("extreme_rapid_burst_ratio")
    elif rapid_burst_trade_ratio >= 0.45:
        score += 34
        reasons.append("high_rapid_burst_ratio")
    elif rapid_burst_trade_ratio >= 0.30:
        score += 24
    elif rapid_burst_trade_ratio >= 0.20:
        score += 12

    if max_trades_60s >= 20:
        score += 30
        reasons.append("very_high_orders_per_minute")
    elif max_trades_60s >= 12:
        score += 22
    elif max_trades_60s >= 8:
        score += 14
    elif max_trades_60s >= 5:
        score += 7

    if rapid_cluster_count >= 4:
        score += 18
        reasons.append("multi_cluster_bursting")
    elif rapid_cluster_count >= 2:
        score += 12
    elif rapid_cluster_count >= 1:
        score += 7

    if trade_count >= 20 and rapid_cluster_trades >= 15:
        score += 8
    if trade_count >= 30 and median_gap_seconds <= 3:
        score += 6

    strategy_key = str(strategy_group or "").strip().lower()
    if strategy_key == "execution_bot":
        score += 20 + min(10.0, max(0.0, strategy_confidence) / 10.0)
        reasons.append("execution_bot_strategy")

    return max(0.0, min(score, 100.0)), reasons


def _speculative_overlay_insider_penalty(
    *,
    position_count: int,
    rapid_burst_trade_ratio: float,
    max_trades_60s: int,
    speculator_score: float,
    position_speculator_count: int,
    max_position_speculator_score: float,
) -> Tuple[float, List[str]]:
    penalty = 0.0
    reasons: List[str] = []

    if position_count >= INSIDER_OVERLAY_VERY_MANY_POSITIONS:
        penalty += 8.0
        reasons.append("many_positions_overlay_penalty")
    elif position_count >= INSIDER_OVERLAY_MANY_POSITIONS:
        penalty += 4.0

    if rapid_burst_trade_ratio >= 0.60:
        penalty += 10.0
        reasons.append("extreme_burst_overlay_penalty")
    elif rapid_burst_trade_ratio >= 0.45:
        penalty += 8.0
    elif rapid_burst_trade_ratio >= 0.30:
        penalty += 5.0

    if max_trades_60s >= 20:
        penalty += 10.0
        reasons.append("very_high_orders_per_minute_overlay_penalty")
    elif max_trades_60s >= 12:
        penalty += 7.0
    elif max_trades_60s >= 8:
        penalty += 4.0

    if speculator_score >= 80:
        penalty += 8.0
    elif speculator_score >= 65:
        penalty += 6.0
    elif speculator_score >= 50:
        penalty += 3.0

    if position_speculator_count >= 2:
        penalty += 5.0
        reasons.append("multiple_speculator_positions_overlay_penalty")
    elif position_speculator_count >= 1:
        penalty += 3.0

    if max_position_speculator_score >= 85:
        penalty += 8.0
        reasons.append("extreme_position_speculator_score_overlay_penalty")
    elif max_position_speculator_score >= 70:
        penalty += 5.0
    elif max_position_speculator_score >= POSITION_SPECULATOR_THRESHOLD:
        penalty += 3.0

    return max(0.0, min(penalty, INSIDER_OVERLAY_MAX_PENALTY)), reasons


def _score_insider(
    *,
    position_count: int,
    pnl: float,
    max_position_value: float,
    total_open_value: float,
    low_buy_hits_14d: int,
    avg_low_buy_notional_14d: float,
    market_diversity_14d: int,
    concentration_ratio: float,
    strategy_group: str,
    strategy_confidence: float,
    max_trades_60s: int,
    rapid_burst_trade_ratio: float,
    top_theme_open_share: float,
    top_theme_market_count: int,
    is_fresh_account: bool,
    win_rate: float,
    closed_sample_size: int,
    high_winrate_qualified: bool,
    speculator_score: float,
    serious_trade_count_14d: int,
    median_trade_notional_14d: float,
    max_trade_notional_14d: float,
    account_age_days: float = 99999.0,
    avg_entry_price: float = 0.0,
    hedging_detected: bool = False,
    high_prob_bias: bool = False,
    resolution_wins: int = 0,
    resolution_losses: int = 0,
) -> Tuple[float, List[str]]:
    reasons: List[str] = []

    # ============================================================
    # TYPE 1 — FRESH CONVICTION SIGNAL
    # New/sparse wallet, big concentrated bet, short horizon
    # ============================================================
    type1_score = 0.0

    # Account age bonus (fresh account = stronger signal)
    if account_age_days <= 7:
        type1_score += 35
    elif account_age_days <= 30:
        type1_score += 25
    elif account_age_days <= 90:
        type1_score += 10

    # Position size (big bet = conviction)
    if max_position_value >= 40000:
        type1_score += 35
    elif max_position_value >= 20000:
        type1_score += 28
    elif max_position_value >= 10000:
        type1_score += 20
    elif max_position_value >= 5000:
        type1_score += 12

    # Capital concentration (this position vs all capital deployed)
    if concentration_ratio >= 0.80:
        type1_score += 20
    elif concentration_ratio >= 0.60:
        type1_score += 15
    elif concentration_ratio >= 0.40:
        type1_score += 8

    # Entry price quality (meaningful uncertainty)
    if 0.10 <= avg_entry_price <= 0.35:
        type1_score += 10
    elif 0.35 < avg_entry_price <= 0.60:
        type1_score += 5

    # Penalties
    if hedging_detected:  # holds both YES and NO in same market
        type1_score -= 25
    if rapid_burst_trade_ratio >= 0.30:  # bot-like trading
        type1_score -= 20
    elif rapid_burst_trade_ratio >= 0.20:
        type1_score -= 10
    if speculator_score >= 70:
        type1_score -= 15

    type1_score = max(0.0, min(type1_score, 100.0))

    # ============================================================
    # TYPE 2 — ESTABLISHED INSIDER SIGNAL
    # Proven track record, resolution accuracy, niche focus
    # ============================================================
    type2_score = 0.0

    # Resolution win rate (exits at >=0.90 = win, <=0.10 = loss, else excluded)
    resolution_total = resolution_wins + resolution_losses
    if resolution_total >= 5:
        resolution_win_rate = resolution_wins / resolution_total
        if resolution_win_rate >= 0.75:
            type2_score += 40
        elif resolution_win_rate >= 0.65:
            type2_score += 28
        elif resolution_win_rate >= 0.55:
            type2_score += 15
        # Penalty for bad resolution rate
        if resolution_win_rate < 0.40:
            type2_score -= 15
    elif resolution_total >= 3:
        resolution_win_rate = resolution_wins / resolution_total
        if resolution_win_rate >= 0.70:
            type2_score += 20

    # Niche consistency (specialization in one category)
    if top_theme_open_share >= 0.70 and top_theme_market_count >= 3:
        type2_score += 20
    elif top_theme_open_share >= 0.50 and top_theme_market_count >= 2:
        type2_score += 12
    elif top_theme_open_share >= 0.40:
        type2_score += 6

    # Early entry quality on winning positions
    # Use avg entry price as proxy (lower = entered earlier when more uncertain)
    if avg_entry_price <= 0.25:
        type2_score += 15
    elif avg_entry_price <= 0.40:
        type2_score += 10
    elif avg_entry_price <= 0.55:
        type2_score += 5

    # Capital concentration in current position
    if concentration_ratio >= 0.60:
        type2_score += 15
    elif concentration_ratio >= 0.40:
        type2_score += 10
    elif concentration_ratio >= 0.25:
        type2_score += 5

    # Position size (meaningful bet)
    if max_position_value >= 10000:
        type2_score += 10
    elif max_position_value >= 5000:
        type2_score += 6
    elif max_position_value >= 2000:
        type2_score += 3

    # Penalties
    if speculator_score >= 80:
        type2_score -= 20
    elif speculator_score >= 65:
        type2_score -= 10
    if rapid_burst_trade_ratio >= 0.45:
        type2_score -= 15
    elif rapid_burst_trade_ratio >= 0.30:
        type2_score -= 10
    if high_prob_bias:  # mostly bets at >0.80 entry — not real insight
        type2_score -= 15

    type2_score = max(0.0, min(type2_score, 100.0))

    # ============================================================
    # FINAL SCORE — max of both types, with a bonus when BOTH qualify
    #
    # When both type1 AND type2 score above 40, it means a wallet shows
    # BOTH fresh conviction (new account, big bet) AND established track
    # record (resolution accuracy, niche focus). This is the strongest
    # possible insider signal — someone who either just created a new
    # account despite having history, or a fast-maturing confirmed edge.
    # In that case we reward with a weighted blend: max + 30% of the min.
    # ============================================================
    BOTH_QUALIFY_THRESHOLD = 40.0
    if type1_score >= BOTH_QUALIFY_THRESHOLD and type2_score >= BOTH_QUALIFY_THRESHOLD:
        # Both types qualify — blend: 70% max + 30% min (capped at 100)
        high = max(type1_score, type2_score)
        low  = min(type1_score, type2_score)
        final_score = min(100.0, high + 0.30 * low)
        reasons.append(f"dual_type_bonus={final_score:.1f}")
    else:
        final_score = max(type1_score, type2_score)

    if type1_score > 30:
        reasons.append(f"type1_conviction_signal={type1_score:.1f}")
    if type2_score > 30:
        reasons.append(f"type2_established_insider={type2_score:.1f}")
    if type1_score >= type2_score and type1_score > 30:
        reasons.append("signal_type=fresh_conviction")
    elif type2_score > type1_score and type2_score > 30:
        reasons.append("signal_type=established_insider")

    return max(0.0, min(final_score, 100.0)), reasons


def _classify_wallet(
    *,
    insider_score: float,
    speculator_score: float,
    position_count: int,
    pnl: float,
    max_position_value: float,
    total_open_value: float,
    low_buy_hits_14d: int,
    serious_trade_count_14d: int,
    max_trade_notional_14d: float,
    high_winrate_qualified: bool,
    closed_sample_size: int = 0,
    trade_count: int = 0,
    account_age_days: float = 99999.0,
    position_insider_candidate_count: int = 0,
    position_speculator_count: int = 0,
    max_position_insider_score: float = 0.0,
    max_position_speculator_score: float = 0.0,
    # New fields for probabilistic tier system
    win_rate: float = 0.0,
    qualifying_roi: float | None = None,
    suspicion_score: float = 0.0,
    lb_pnl: float | None = None,
    is_bot: bool = False,
) -> tuple[str, bool, Dict[str, bool], Dict[str, Any]]:
    """
    Tier-based probabilistic wallet classification.

    Returns a 4-tuple:
        (wallet_class, matches_criteria, checks, tier_data)

    tier_data keys:
        wallet_tier: int (1-6)
        wallet_confidence_score: float (0-100)
        effective_copy_fraction: float (0.0-1.0)
        insider_score_trend: float
        classification_notes: list[str]

    Backward compatibility: wallet_class is still one of the existing
    string values (insider_candidate, emerging_insider, speculator_bot,
    mixed_review, high_winrate_review, low_signal).
    """
    # ── Shared pre-computation ─────────────────────────────────────────────────
    trades_per_day = trade_count / max(account_age_days, 1.0)
    low_frequency_ok = trades_per_day <= 10.0
    position_signal_strength_ok = (
        int(position_insider_candidate_count or 0) > 0
        or float(max_position_insider_score or 0.0) >= EMERGING_MIN_POSITION_INSIDER_SCORE
    )
    repeat_or_position_signal_ok = low_buy_hits_14d >= MIN_REPEAT_HITS_14D or position_signal_strength_ok
    position_speculator_guard_ok = (
        int(position_speculator_count or 0) <= EMERGING_MAX_POSITION_SPECULATOR_COUNT
        and float(max_position_speculator_score or 0.0) < EMERGING_MAX_POSITION_SPECULATOR_SCORE
    )
    emerging_top_open_value_ok = max_position_value >= EMERGING_MIN_TOP_OPEN_VALUE
    emerging_total_open_value_ok = total_open_value >= EMERGING_MIN_TOTAL_OPEN_VALUE
    serious_or_position_signal_ok = (
        serious_trade_count_14d >= INSIDER_MIN_SERIOUS_TRADES_14D or position_signal_strength_ok
    )
    insider_high = insider_score >= SCORE_FLAG_THRESHOLD
    emerging_insider_high = insider_score >= EMERGING_SCORE_THRESHOLD
    spec_high = speculator_score >= SPECULATOR_SCORE_HIGH

    checks = {
        "insider_score_gte_threshold": insider_high,
        "emerging_score_gte_threshold": emerging_insider_high,
        "repeat_hits_ok": low_buy_hits_14d >= MIN_REPEAT_HITS_14D,
        "positions_ok": position_count <= INSIDER_MAX_POSITIONS,
        "pnl_ok": MIN_PNL < pnl < MAX_PNL,
        "top_open_value_ok": max_position_value >= INSIDER_MIN_TOP_OPEN_VALUE,
        "total_open_value_ok": total_open_value >= INSIDER_MIN_TOTAL_OPEN_VALUE,
        "max_position_ok": max_position_value > MIN_POSITION_VALUE,
        "serious_trades_ok": serious_trade_count_14d >= INSIDER_MIN_SERIOUS_TRADES_14D,
        "serious_notional_ok": max_trade_notional_14d >= INSIDER_MIN_SERIOUS_TRADE_NOTIONAL,
        "low_frequency_ok": low_frequency_ok,
        "position_signal_strength_ok": position_signal_strength_ok,
        "repeat_or_position_signal_ok": repeat_or_position_signal_ok,
        "position_speculator_guard_ok": position_speculator_guard_ok,
        "emerging_top_open_value_ok": emerging_top_open_value_ok,
        "emerging_total_open_value_ok": emerging_total_open_value_ok,
        "serious_or_position_signal_ok": serious_or_position_signal_ok,
    }
    core_hard_gates = all(
        checks[key]
        for key in (
            "insider_score_gte_threshold",
            "repeat_hits_ok",
            "positions_ok",
            "pnl_ok",
            "top_open_value_ok",
            "total_open_value_ok",
            "max_position_ok",
            "serious_trades_ok",
            "serious_notional_ok",
            "low_frequency_ok",
        )
    )
    emerging_hard_gates = all(
        checks[key]
        for key in (
            "emerging_score_gte_threshold",
            "repeat_or_position_signal_ok",
            "positions_ok",
            "pnl_ok",
            "emerging_top_open_value_ok",
            "emerging_total_open_value_ok",
            "max_position_ok",
            "serious_or_position_signal_ok",
            "serious_notional_ok",
            "position_speculator_guard_ok",
            "low_frequency_ok",
        )
    )
    HIGH_WINRATE_REVIEW_MIN_SAMPLE = int(os.getenv("HIGH_WINRATE_REVIEW_MIN_SAMPLE", "15"))
    high_winrate_review_ok = (
        high_winrate_qualified
        and closed_sample_size >= HIGH_WINRATE_REVIEW_MIN_SAMPLE
        and checks["top_open_value_ok"]
        and checks["total_open_value_ok"]
        and checks["serious_trades_ok"]
        and checks["serious_notional_ok"]
    )

    # ── Legacy classification (backward-compatible string label) ───────────────
    if insider_high and not spec_high and core_hard_gates:
        legacy_class, legacy_matches = "insider_candidate", True
    elif spec_high and not insider_high:
        legacy_class, legacy_matches = "speculator_bot", False
    elif insider_high and spec_high:
        legacy_class, legacy_matches = "mixed_review", False
    elif not spec_high and emerging_hard_gates:
        legacy_class, legacy_matches = "emerging_insider", False
    elif high_winrate_review_ok:
        legacy_class, legacy_matches = "high_winrate_review", True
    else:
        legacy_class, legacy_matches = "low_signal", False

    # ── TIER ASSIGNMENT (first match wins, highest tier number = worst) ────────
    notes: List[str] = []

    # TIER 6 — BOT/SPAM: block entirely
    # NOTE: suspicion_score >= 80 guard REMOVED (audit C6, 2026-03-30).
    # suspicion_score was aliased to insider_score, causing best wallets
    # to be classified as bots. The remaining guards (is_bot + high
    # speculator_score + high trade frequency) are sufficient.
    if (
        is_bot
        or (speculator_score >= 85 and trades_per_day > 20)
    ):
        tier_num = 6
        base_conf = 0.0
        copy_frac = 0.0
        notes.append("tier6_bot_spam")

    # TIER 5 — INSUFFICIENT DATA: no copy
    elif trade_count < 10 or account_age_days < 20:
        tier_num = 5
        base_conf = 15.0
        copy_frac = 0.0
        notes.append("tier5_insufficient_data")

    # TIER 3 — HIGH-FREQUENCY TRADER: potential fade signal, check before tier 4
    # I19 note: Tier 3 < Tier 4 numerically but copy_frac=0.0 vs 0.3.
    # Tier numbers are NOT ordinal quality rankings — they're checked in
    # elif order. What matters is copy_frac and base_conf, not the number.
    elif speculator_score >= 65 and trades_per_day > 10 and insider_score < 70:
        tier_num = 3
        base_conf = 35.0
        copy_frac = 0.0
        notes.append("tier3_high_freq_trader")

    # TIER 4 — NOISY/MIXED: watch only with partial copy allowed
    elif (
        (insider_score >= 65 and speculator_score >= 55)
        or (insider_score >= 60 and win_rate < 0.45 and trade_count >= 10)
    ):
        tier_num = 4
        base_conf = 40.0
        copy_frac = 0.3
        notes.append("tier4_noisy_mixed")

    # TIER 1 — CONVICTION INSIDER: full copy (check before tier 2)
    elif (
        insider_score >= 70
        and win_rate >= 0.58
        and closed_sample_size >= 15
        and account_age_days >= 45
        and speculator_score < 60
        and (qualifying_roi is not None and qualifying_roi >= 15.0)
        and max_position_value >= 2000
    ):
        tier_num = 1
        base_conf = 82.0
        copy_frac = 1.0
        notes.append("tier1_conviction_insider")

    # TIER 2 — EMERGING INSIDER: partial copy
    elif (
        insider_score >= 65
        and win_rate >= 0.52
        and closed_sample_size >= 8
        and account_age_days >= 25
        and speculator_score < 55
    ):
        tier_num = 2
        base_conf = 62.0
        copy_frac = 0.5
        notes.append("tier2_emerging_insider")

    # DEFAULT — LOW SIGNAL (Tier 5 floor for wallets that don't fit higher tiers)
    else:
        tier_num = 5
        base_conf = 15.0
        copy_frac = 0.0
        notes.append("tier5_low_signal_default")

    # ── CONFIDENCE SCORE MODIFIERS ─────────────────────────────────────────────
    wr = float(win_rate or 0.0)
    total_trades = int(trade_count or 0)
    age_days = float(account_age_days or 0.0)
    susp = float(suspicion_score or 0.0)

    # Simple trend proxy: if qualifying ROI is disproportionate to win rate → improving
    qroi_val = float(qualifying_roi) if qualifying_roi is not None else 0.0
    if wr > 0 and qroi_val > wr * 80.0:
        insider_score_trend = 0.1
        notes.append("trend_improving_roi_quality")
    elif wr > 0 and qroi_val < wr * 30.0 and qroi_val > 0:
        insider_score_trend = -0.2
        notes.append("trend_degrading_roi_quality")
    else:
        insider_score_trend = 0.0

    confidence_modifiers = [
        (wr - 0.50) * 60.0,                                     # win-rate quality: +/-6 per 10%
        min(15.0, max(0.0, (total_trades - 10) * 0.5)),         # sample size: up to +15
        min(8.0, max(0.0, (age_days - 30.0) * 0.1)),            # account maturity: up to +8
        min(5.0, max(-10.0, insider_score_trend * 20.0)),        # trend signal
        -min(20.0, susp * 0.25),                                 # suspicion penalty: up to -20
        8.0 if (lb_pnl is not None and lb_pnl > 0) else 0.0,   # leaderboard validation
    ]
    final_confidence = max(0.0, min(100.0, base_conf + sum(confidence_modifiers)))

    tier_data: Dict[str, Any] = {
        "wallet_tier": tier_num,
        "wallet_confidence_score": round(final_confidence, 2),
        "effective_copy_fraction": round(copy_frac, 4),
        "insider_score_trend": round(insider_score_trend, 4),
        "classification_notes": notes,
    }

    return legacy_class, legacy_matches, checks, tier_data


def _reason(
    *,
    insider_score: float,
    speculator_score: float,
    classification: str,
    tier: str,
    strategy_group: str,
    strategy_confidence: float,
    checks: Dict[str, bool],
    position_count: int,
    pnl: float,
    max_position_value: float,
    low_buy_hits_14d: int,
    avg_low_buy_notional_14d: float,
    concentration_ratio: float,
    rapid_burst_trade_ratio: float,
    max_trades_60s: int,
    top_theme_open_share: float,
    top_theme_market_count: int,
    win_rate: float,
    closed_sample_size: int,
    trade_count: int,
    account_age_days: float,
    serious_trade_count_14d: int,
    median_trade_notional_14d: float,
    max_trade_notional_14d: float,
    total_position_count: int,
    pending_settlement_win_count: int,
    pending_settlement_loss_count: int,
    pending_settlement_win_value: float,
    pending_settlement_loss_value: float,
    position_signal_count: int,
    position_insider_candidate_count: int,
    position_speculator_count: int,
    max_position_insider_score: float,
    max_position_speculator_score: float,
    insider_overlay_penalty: float,
) -> str:
    summary = [f"{k}={'yes' if v else 'no'}" for k, v in checks.items()]
    summary.append(f"classification={classification}")
    summary.append(f"tier={tier}")
    summary.append(f"strategy={strategy_group}")
    summary.append(f"strategy_confidence={strategy_confidence:.1f}")
    summary.append(f"insider_score={insider_score:.1f}")
    summary.append(f"speculator_score={speculator_score:.1f}")
    summary.append(f"position_count={position_count}")
    summary.append(f"pnl={pnl:.2f}")
    summary.append(f"max_position_value={max_position_value:.2f}")
    summary.append(f"low_buy_hits_14d={low_buy_hits_14d}")
    summary.append(f"avg_low_buy_notional_14d={avg_low_buy_notional_14d:.2f}")
    summary.append(f"concentration={concentration_ratio:.2f}")
    summary.append(f"rapid_burst_trade_ratio={rapid_burst_trade_ratio:.2f}")
    summary.append(f"max_trades_60s={max_trades_60s}")
    summary.append(f"top_theme_open_share={top_theme_open_share:.2f}")
    summary.append(f"top_theme_market_count={top_theme_market_count}")
    summary.append(f"win_rate={win_rate:.4f}")
    summary.append(f"closed_sample_size={closed_sample_size}")
    summary.append(f"account_age_days={account_age_days:.1f}")
    trades_per_day = trade_count / max(account_age_days, 1.0)
    summary.append(f"trades_per_day={trades_per_day:.1f}")
    summary.append(f"serious_trade_count_14d={serious_trade_count_14d}")
    summary.append(f"median_trade_notional_14d={median_trade_notional_14d:.2f}")
    summary.append(f"max_trade_notional_14d={max_trade_notional_14d:.2f}")
    summary.append(f"total_position_count={total_position_count}")
    summary.append(f"pending_settlement_win_count={pending_settlement_win_count}")
    summary.append(f"pending_settlement_loss_count={pending_settlement_loss_count}")
    summary.append(f"pending_settlement_win_value={pending_settlement_win_value:.2f}")
    summary.append(f"pending_settlement_loss_value={pending_settlement_loss_value:.2f}")
    summary.append(f"position_signal_count={position_signal_count}")
    summary.append(f"position_insider_candidate_count={position_insider_candidate_count}")
    summary.append(f"position_speculator_count={position_speculator_count}")
    summary.append(f"max_position_insider_score={max_position_insider_score:.1f}")
    summary.append(f"max_position_speculator_score={max_position_speculator_score:.1f}")
    summary.append(f"insider_overlay_penalty={insider_overlay_penalty:.1f}")
    return ", ".join(summary)

__all__ = [
    name
    for name, value in globals().items()
    if name.isupper() or (name.startswith("_") and callable(value))
]
