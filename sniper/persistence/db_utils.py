"""
Utility helpers for the Sniper data layer.
Pure functions: coerce, normalize, classify, score.
No database connections. No side effects.
"""
import hashlib
import math
import os
import re
import time
import datetime as dt
from typing import Any, Dict, List, Tuple

from sniper.utils import _normalize_tx_hash as _utils_normalize_tx_hash
from sniper.utils import _normalize_wallet as _utils_normalize_wallet


# ---------------------------------------------------------------------------
# Coerce helpers
# ---------------------------------------------------------------------------

def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# ---------------------------------------------------------------------------
# Normalize helpers
# ---------------------------------------------------------------------------

def _normalize_wallet(wallet: str) -> str:
    return _utils_normalize_wallet(wallet)


def _normalize_tx_hash(tx_hash: str) -> str:
    return _utils_normalize_tx_hash(tx_hash)


def _normalize_condition_id(condition_id: str | None) -> str:
    return str(condition_id or "").strip().lower()


def _normalize_outcome(outcome: str | None) -> str:
    return str(outcome or "").strip().lower()


def _coerce_history_ts(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return 0
        try:
            value = float(raw)
        except ValueError:
            try:
                parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return 0
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return int(parsed.timestamp())
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return 0
    if ts > 1e12:
        ts = ts / 1000.0
    return int(ts)


def _followed_wallet_activity_key(
    wallet: str,
    transaction_hash: str,
    condition_id: str,
    outcome: str,
    side: str,
    activity_type: str,
    activity_ts: int,
    price: float,
    size: float,
) -> str:
    base = "|".join(
        (
            _normalize_wallet(wallet),
            _normalize_tx_hash(transaction_hash),
            _normalize_condition_id(condition_id),
            _normalize_outcome(outcome),
            str(side or "").strip().upper(),
            str(activity_type or "").strip().upper(),
            str(int(activity_ts or 0)),
            f"{float(price or 0.0):.10f}",
            f"{float(size or 0.0):.10f}",
        )
    )
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def generate_ws_synthetic_hash(
    wallet: str,
    condition_id: str,
    outcome: str,
    side: str,
    activity_type: str,
    raw_timestamp_ms: int,
    price: float,
    size: float,
) -> str:
    """Generate a synthetic tx hash for WS-detected followed-wallet trades.

    Uses raw millisecond timestamp (NOT second-resolution activity_ts)
    to prevent collisions in burst trades. The result is passed as
    transaction_hash to _followed_wallet_activity_key() for the final
    activity_key computation.
    """
    base = "|".join((
        _normalize_wallet(wallet),
        _normalize_condition_id(condition_id),
        _normalize_outcome(outcome),
        str(side or "").strip().upper(),
        str(activity_type or "").strip().upper(),
        str(int(raw_timestamp_ms)),
        f"{float(price):.10f}",
        f"{float(size):.10f}",
    ))
    return "ws:" + hashlib.sha1(base.encode("utf-8")).hexdigest()


def is_synthetic_ws_hash(tx_hash: str) -> bool:
    """Check if a transaction_hash is a synthetic WS hash."""
    return isinstance(tx_hash, str) and tx_hash.startswith("ws:")


# ---------------------------------------------------------------------------
# Signal identity helpers
# ---------------------------------------------------------------------------

def _extract_signal_identity(signal: Dict[str, Any]) -> tuple[str, str, str, int, int]:
    wallet = _normalize_wallet(signal.get("wallet") or signal.get("proxy_wallet") or signal.get("proxyWallet"))
    condition_id = _normalize_condition_id(signal.get("condition_id") or signal.get("conditionId"))
    outcome = _normalize_outcome(signal.get("outcome"))
    signal_ts = _coerce_int(
        signal.get("signal_ts")
        or signal.get("entry_anchor_ts")
        or signal.get("analyzed_ts")
        or signal.get("latest_trade_ts")
        or time.time(),
        default=int(time.time()),
    )
    analysis_version = _coerce_int(signal.get("analysis_version") or signal.get("analysisVersion"), default=0)
    if not wallet or not condition_id:
        raise ValueError("signal must include wallet and condition_id")
    return wallet, condition_id, outcome, signal_ts, analysis_version


def _build_execution_key(
    *,
    wallet: str,
    condition_id: str,
    outcome: str,
    strategy_id: str,
    action: str,
    analysis_version: int,
) -> str:
    return "|".join(
        (
            wallet,
            condition_id,
            outcome,
            str(strategy_id or "").strip(),
            action.upper(),
            str(int(analysis_version or 0)),
        )
    )


# ---------------------------------------------------------------------------
# Domain classification
# ---------------------------------------------------------------------------

_DOMAIN_PATTERNS: List[Tuple[str, List[str]]] = [
    # Order matters: more specific domains checked first, generic Sports last.
    ("Tennis", [
        "Open:", "ATP", "WTA", "Roland Garros", "Wimbledon",
        "Australian Open", "US Open:", "Tennis",
        "Set 1 Winner", "Set 2 Winner",
        "Dubai Duty Free", "Rotterdam Open", "Rio Open",
        "Midland:", "Lille:",
    ]),
    ("Esports", [
        "LoL:", "CS2:", "CSGO", "Dota 2",
        "Map Handicap", "Esports",
    ]),
    ("Geopolitics", [
        "Iran", "Khamenei", "ceasefire", "military operations",
        "strike Iran", "regime fall", "Supreme Leader",
        "Ukraine", "Russia", "NATO", "Gaza", "Israel",
        "North Korea", "Kim Jong", "sanctions",
        "tariff", "trade war",
    ]),
    ("US Politics", [
        "Presidential Election", "Midterm", "Republican Party",
        "Democratic Party", "Congress", "Senate confirmation",
        "Trump", "Biden", "Governor", "confirmed to have",
        "Executive Order", "impeach", "veto",
    ]),
    ("Crypto", [
        "Bitcoin", "Ethereum", "BTC ", " ETH ", "Solana",
        "crypto", "blockchain", "DeFi", "NFT",
        "Coinbase", "Binance", "halving",
    ]),
    ("Entertainment", [
        "Academy Award", "Oscar", "Grammy", "Emmy",
        "MrBeast", "YouTube", "Netflix", "Spotify",
        "box office", "views on day",
        "Best Actor", "Best Picture", "Best Director",
    ]),
    ("Soccer", [
        "FC ", " FC", "Premier League", "La Liga", "Bundesliga",
        "Serie A", "Champions League", "World Cup",
        "Guadalajara", "Pumas", "São Paulo",
        "Manchester", "Liverpool", "Barcelona", "Real Madrid",
    ]),
    ("Finance", [
        "S&P 500", "Nasdaq", "Dow Jones", "FOMC",
        "interest rate", "GDP ", "inflation", "CPI ",
        "market cap", "IPO ", "earnings",
    ]),
    ("Pop Culture", [
        "Epstein", "celebrity", "scandal",
        "social media", "TikTok", "Instagram",
        "viral", "controversy",
    ]),
    ("Sports", [
        "Spread:", "O/U ", "Over/Under",
        "NBA", "NFL", "MLB", "NHL", "MLS",
        "Bucks", "Lakers", "Celtics", "Warriors", "Nuggets", "Cavaliers",
        "Thunder", "Rockets", "Mavericks", "Clippers", "Suns", "Pacers",
        "76ers", "Grizzlies", "Pistons", "Nets", "Spurs", "Hornets",
        "Timberwolves", "Trail Blazers", "Pelicans", "Kings", "Wizards",
        "Knicks", "Hawks", "Magic", "Raptors", "Heat", "Bulls",
        "Super Bowl", "World Series", "Stanley Cup",
        "F1 ", "Formula 1", "UFC ", "Grand Prix",
        " vs. ", " vs ",
    ]),
]


def _classify_title_domain(title: str) -> str:
    """Return the first matching domain label for a market title, or ''."""
    title_lower = title.lower()
    for domain, patterns in _DOMAIN_PATTERNS:
        for pat in patterns:
            if pat.lower() in title_lower:
                return domain
    return ""


def _classify_news_domain(title: str) -> str:
    """Classify a news article title into a domain category.

    Separate from ``_classify_title_domain()`` which is tuned for Polymarket
    market titles.  This function uses keyword patterns suited to news headlines.
    """
    lower = title.lower()
    # Check most specific first
    oil_kw = ("oil", "crude", "brent", "wti", "petroleum", "opec",
              "refinery", "pipeline", "energy price", "barrel")
    if any(kw in lower for kw in oil_kw):
        return "Oil"
    geo_kw = ("iran", "sanctions", "military", "conflict",
              "nato", "ceasefire", "invasion", "troops", "missile", "airstrike")
    if any(kw in lower for kw in geo_kw) or re.search(r'\bwar\b', lower):
        return "Geopolitics"
    fin_kw = ("fed ", "interest rate", "inflation", "gdp", "stock market",
              "treasury", "central bank", "federal reserve")
    if any(kw in lower for kw in fin_kw):
        return "Finance"
    crypto_kw = ("bitcoin", "ethereum", "crypto", "blockchain")
    if any(kw in lower for kw in crypto_kw):
        return "Crypto"
    return "General"


# ---------------------------------------------------------------------------
# Followability scoring
# ---------------------------------------------------------------------------

def _followable_clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


_LOG20 = math.log(20)
_LOG200 = math.log(200)
_LOG_DENOM = _LOG200 - _LOG20


def compute_followability_score(
    *,
    win_rate: float,
    qualifying_roi: float,
    closed_sample_size: int,
    qualifying_capital: float,
    insider_score: float,
    speculator_score: float,
    classification: str,
    lb_pnl: float | None = None,
    closed_realized_pnl: float | None = None,
    open_cash_pnl: float | None = None,
) -> float:
    """Followability composite score — single source of truth.

    Used by both wallet_followable_leaderboard_rows() and cached into
    user_analysis.followability_cached during analyze_wallet().

    P&L-first design: Polymarket lifetime P&L (lb_pnl, fallback to
    closed_realized_pnl) is the dominant signal at 45% weight.
    Behavioral signals (win_rate, roi, sample) provide supporting evidence.
    spec_penalty removed: algorithmic/speculator wallets are empirically
    the most profitable class and must not be penalized.
    """
    # Market makers manufacture win rate via both-sides trading, not alpha.
    if classification == "market_maker":
        return 0.0

    # --- P&L score (45%) ---
    # Use best available lifetime P&L: lb_pnl preferred, fallback to realized.
    best_pnl = lb_pnl if lb_pnl is not None else closed_realized_pnl
    if best_pnl is not None:
        # S-curve: $0 → 50, $150K → ~73, $500K → ~97, -$150K → ~27, -$300K → ~6
        pnl_score = _followable_clamp(50.0 + 50.0 * math.tanh(best_pnl / 150_000.0))
    else:
        pnl_score = 50.0  # neutral when no lifetime data yet

    # --- Win rate score (15%) with zombie-order correction ---
    # Wallets that hold losers and close winners overstate win rate by ~15%.
    adjusted_win_rate = win_rate * 0.85
    win_rate_score = _followable_clamp(((adjusted_win_rate - 0.50) / 0.40) * 100)

    # --- ROI score (15%) ---
    roi_score = _followable_clamp((qualifying_roi / 40.0) * 100)

    # --- Sample size score (15%) ---
    sample_score = _followable_clamp(
        ((math.log(max(closed_sample_size, 20)) - _LOG20) / _LOG_DENOM) * 100
    )

    # --- Capital fit score (10%) ---
    if 10000 <= qualifying_capital <= 100000:
        capital_fit_score = 100.0
    elif 5000 <= qualifying_capital < 10000:
        capital_fit_score = 60 + 40 * ((qualifying_capital - 5000) / 5000)
    elif 100000 < qualifying_capital <= 250000:
        capital_fit_score = 100 - 40 * ((qualifying_capital - 100000) / 150000)
    else:
        capital_fit_score = 0.0

    # --- Hidden loss penalty ---
    # open_cash_pnl < 0 means unrealized losses not visible in win_rate/roi.
    # Deduct up to 20 points for wallets sitting on large hidden losses.
    open_loss_penalty = 0.0
    if open_cash_pnl is not None and open_cash_pnl < -10_000:
        open_loss_penalty = min(20.0, (-open_cash_pnl - 10_000) / 5_000)

    raw = (
        0.45 * pnl_score
        + 0.15 * roi_score
        + 0.15 * win_rate_score
        + 0.15 * sample_score
        + 0.10 * capital_fit_score
        - open_loss_penalty
    )
    return _followable_clamp(raw)


def _followable_short_wallet(wallet: str) -> str:
    if len(wallet) > 10:
        return wallet[:6] + "..." + wallet[-4:]
    return wallet


# ---------------------------------------------------------------------------
# Instance tag
# ---------------------------------------------------------------------------

def _current_instance_tag(default: str = "") -> str:
    """Return the current deployment instance tag from env, if configured."""
    return str(os.environ.get("INSTANCE_TAG", "") or default).strip()


# ---------------------------------------------------------------------------
# Alert normalization
# ---------------------------------------------------------------------------

_ALERT_STATUSES = {"new", "sent", "acknowledged"}
_ALERT_SEVERITIES = {"info", "low", "medium", "high", "critical"}
_AGENT_MUTE_SCOPE_TYPES = {"wallet", "dedupe_key", "position"}


def _normalize_alert_status(status: str | None, default: str = "new") -> str:
    key = str(status or "").strip().lower()
    return key if key in _ALERT_STATUSES else default


def _normalize_alert_severity(severity: str | None, default: str = "medium") -> str:
    key = str(severity or "").strip().lower()
    return key if key in _ALERT_SEVERITIES else default


def _normalize_agent_mute_scope(scope_type: str | None) -> str:
    key = str(scope_type or "").strip().lower()
    return key if key in _AGENT_MUTE_SCOPE_TYPES else "wallet"


def _normalize_agent_mute_scope_value(
    scope_type: str,
    scope_value: str | None = None,
    *,
    wallet: str | None = None,
    dedupe_key: str | None = None,
    condition_id: str | None = None,
    outcome: str | None = None,
) -> str:
    if scope_type == "wallet":
        return _normalize_wallet(wallet or scope_value or "")
    if scope_type == "dedupe_key":
        return str(dedupe_key or scope_value or "").strip().lower()
    if scope_type == "position":
        cid = _normalize_condition_id(condition_id)
        out = _normalize_outcome(outcome)
        if not cid and scope_value:
            raw = str(scope_value).strip().lower()
            if ":" in raw:
                cid_raw, out_raw = raw.split(":", 1)
                cid = _normalize_condition_id(cid_raw)
                out = _normalize_outcome(out_raw)
            else:
                cid = _normalize_condition_id(raw)
        if not cid:
            return ""
        return f"{cid}:{out}"
    return str(scope_value or "").strip().lower()
