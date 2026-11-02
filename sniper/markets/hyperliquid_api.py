"""Hyperliquid API client — the wallet-intelligence surface (mirrors markets/polymarket_api.py's
role for Polymarket). Read-only public endpoints, no SDK:
  - fetch_leaderboard():        public stats leaderboard (discover winning perp traders)
  - fetch_user_fills(addr):     a wallet's recent fills (entries/exits + closedPnl + funding)
  - fetch_clearinghouse_state(addr): current open positions + margin

HL is PERPS, not prediction markets: continuous price, leverage, funding, no resolution. So a
copyable edge here is entries AND exits (PnL = exit - entry +/- funding), and ROI is return on
ACCOUNT VALUE, not stake/volume. Discovery/analysis tooling builds on these. NOT live trading."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import requests
import time

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
_TIMEOUT = 30
_HEADERS = {"Content-Type": "application/json", "User-Agent": "sniper-hl/1"}
_SESSION = requests.Session()
_RATE_PATH = Path(os.environ.get("SNIPER_STATE_ROOT", Path(__file__).resolve().parents[2])) / "data" / "hl_info_rate_limit.state"


class InfoRateLimiter:
    """Cross-process token bucket for Hyperliquid's shared IP weight budget."""

    def __init__(self, path, *, capacity: int = 1000, window_s: float = 60.0):
        self.path = Path(path)
        self.capacity = int(capacity)
        self.window_s = float(window_s)

    def try_reserve(self, weight: int, *, now: float | None = None) -> bool:
        now = time.time() if now is None else float(now)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            try:
                state = json.loads(handle.read() or "{}")
            except (ValueError, TypeError):
                state = {}
            updated = float(state.get("updated", now))
            tokens = float(state.get("tokens", self.capacity))
            elapsed = max(0.0, now - updated)
            tokens = min(float(self.capacity), tokens + elapsed * self.capacity / self.window_s)
            allowed = int(weight) <= tokens
            if allowed:
                tokens -= int(weight)
                handle.seek(0)
                handle.truncate()
                json.dump({"updated": now, "tokens": tokens}, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            fcntl.flock(handle, fcntl.LOCK_UN)
            return allowed

    def reserve(self, weight: int) -> None:
        while not self.try_reserve(weight):
            time.sleep(1.0)

    def penalize(self, delay_s: float, *, now: float | None = None) -> None:
        """Make a provider 429 visible to every process sharing this IP bucket."""
        now = time.time() if now is None else float(now)
        blocked_until = now + max(0.0, float(delay_s))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            try:
                state = json.loads(handle.read() or "{}")
            except (ValueError, TypeError):
                state = {}
            blocked_until = max(blocked_until, float(state.get("updated", now)))
            handle.seek(0)
            handle.truncate()
            json.dump({"updated": blocked_until, "tokens": 0.0}, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle, fcntl.LOCK_UN)


_INFO_LIMITER = InfoRateLimiter(_RATE_PATH)

_WINDOWS = ("day", "week", "month", "allTime")


def _info_weight(payload: dict) -> int:
    return 2 if payload.get("type") in {
        "l2Book", "allMids", "clearinghouseState", "spotClearinghouseState",
        "exchangeStatus", "orderStatus",
    } else 20


def _post_info(payload: dict, timeout: int = _TIMEOUT) -> object:
    weight = _info_weight(payload)
    for attempt in range(4):
        if _INFO_LIMITER is not None:
            _INFO_LIMITER.reserve(weight)
        r = _SESSION.post(INFO_URL, json=payload, headers=_HEADERS, timeout=timeout)
        try:
            r.raise_for_status()
        except requests.HTTPError:
            if r.status_code != 429 or attempt == 3:
                raise
            try:
                retry_after = min(30.0, max(0.0, float(r.headers.get("Retry-After", 0.0))))
            except (TypeError, ValueError):
                retry_after = 0.0
            delay = max(retry_after, 5.0 * (2 ** attempt))
            if _INFO_LIMITER is not None:
                _INFO_LIMITER.penalize(delay)
            time.sleep(delay)
            continue
        return r.json()
    raise RuntimeError("unreachable")


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def fetch_leaderboard(timeout: int = 60) -> list[dict]:
    """Public HL leaderboard -> normalized rows. Each: {wallet, account_value, name, day/week/
    month/all_time: {pnl, roi, vlm}}. ROI is per-window return on account value."""
    r = _SESSION.get(LEADERBOARD_URL, headers={"User-Agent": "sniper-hl/1"}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    rows = data.get("leaderboardRows") if isinstance(data, dict) else data
    out = []
    for row in (rows or []):
        perf = {w: {"pnl": 0.0, "roi": 0.0, "vlm": 0.0} for w in _WINDOWS}
        for entry in (row.get("windowPerformances") or []):
            if isinstance(entry, list) and len(entry) == 2 and entry[0] in perf:
                p = entry[1] or {}
                perf[entry[0]] = {"pnl": _f(p.get("pnl")), "roi": _f(p.get("roi")), "vlm": _f(p.get("vlm"))}
        out.append({
            "wallet": (row.get("ethAddress") or "").lower(),
            "account_value": _f(row.get("accountValue")),
            "name": row.get("displayName") or "",
            "day": perf["day"], "week": perf["week"], "month": perf["month"], "all_time": perf["allTime"],
        })
    return out


def fetch_user_fills(wallet: str, timeout: int = _TIMEOUT) -> list[dict]:
    """A wallet's recent fills. Each HL fill: {coin, px, sz, side (B/A), time(ms), dir (Open
    Long/Close Long/...), closedPnl, fee, startPosition, ...}. dir + closedPnl let us reconstruct
    entries vs exits and realized PnL per round-trip."""
    data = _post_info({"type": "userFills", "user": wallet}, timeout)
    return data if isinstance(data, list) else []


def fetch_user_fills_by_time(wallet: str, start_ms: int, end_ms: int,
                             timeout: int = _TIMEOUT) -> list[dict]:
    """Bounded reconnect recovery; never used as another periodic scan."""
    data = _post_info({
        "type": "userFillsByTime", "user": str(wallet).lower(),
        "startTime": int(start_ms), "endTime": int(end_ms),
    }, timeout)
    return data if isinstance(data, list) else []


def fetch_clearinghouse_state(wallet: str, timeout: int = _TIMEOUT) -> dict:
    """Current open positions + margin. assetPositions[].position: {coin, szi (signed size),
    entryPx, positionValue, unrealizedPnl, leverage, liquidationPx, ...}."""
    data = _post_info({"type": "clearinghouseState", "user": wallet}, timeout)
    return data if isinstance(data, dict) else {}


def _fetch_marks(*, timeout: int, preserve_provider_case: bool) -> dict:
    data = _post_info({"type": "metaAndAssetCtxs"}, timeout)
    if not isinstance(data, list) or len(data) != 2:
        return {}
    meta, ctxs = data
    out = {}
    for u, c in zip((meta or {}).get("universe", []) or [], ctxs or []):
        px = (c or {}).get("markPx") or (c or {}).get("midPx")
        if px:
            symbol = str((u or {}).get("name", "")).strip()
            if symbol:
                out[symbol if preserve_provider_case else symbol.upper()] = _f(px)
    return out


def fetch_marks(timeout: int = _TIMEOUT) -> dict:
    """Legacy uppercase mark map. Provider-bound research must use ``fetch_provider_marks``."""
    return _fetch_marks(timeout=timeout, preserve_provider_case=False)


def fetch_provider_marks(timeout: int = _TIMEOUT) -> dict:
    """Current marks keyed by the exact case-sensitive symbol returned by Hyperliquid."""
    return _fetch_marks(timeout=timeout, preserve_provider_case=True)


def fetch_l2_book(coin: str, timeout: int = _TIMEOUT) -> dict:
    """Level-2 order book for a coin. HL returns levels=[bids, asks], each [{px, sz, n}].
    Normalized -> {coin, bids:[{px,sz}], asks:[{px,sz}], mid, spread_bps}. bids descend, asks
    ascend. Used to price real taker slippage the mark-fill sim ignores (spread + depth)."""
    data = _post_info({"type": "l2Book", "coin": coin}, timeout)
    levels = (data or {}).get("levels") if isinstance(data, dict) else None
    levels = levels or [[], []]
    bids = [{"px": _f(l.get("px")), "sz": _f(l.get("sz"))} for l in (levels[0] or [])]
    asks = [{"px": _f(l.get("px")), "sz": _f(l.get("sz"))} for l in (levels[1] or [])]
    mid = spread_bps = None
    if bids and asks and bids[0]["px"] > 0 and asks[0]["px"] > 0:
        mid = (bids[0]["px"] + asks[0]["px"]) / 2.0
        if mid > 0:
            spread_bps = (asks[0]["px"] - bids[0]["px"]) / mid * 1e4
    return {"coin": coin, "bids": bids, "asks": asks, "mid": mid, "spread_bps": spread_bps}


def fetch_funding_history(coin: str, start_ms: int, end_ms: int | None = None,
                          timeout: int = _TIMEOUT) -> list[dict]:
    """Hourly funding rates for a coin in [start_ms, end_ms]. Each HL row: {coin, fundingRate,
    premium, time(ms)}. Normalized -> [{time, rate}] sorted ascending. Long PAYS positive
    funding; short receives it. The copy sim ignores funding entirely — this quantifies it."""
    payload: dict = {"type": "fundingHistory", "coin": coin, "startTime": int(start_ms)}
    if end_ms is not None:
        payload["endTime"] = int(end_ms)
    data = _post_info(payload, timeout)
    rows = [{"time": int(x["time"]), "rate": _f(x.get("fundingRate"))}
            for x in (data or []) if isinstance(x, dict) and x.get("time") is not None]
    return sorted(rows, key=lambda r: r["time"])


def fetch_candles(coin: str, interval: str, start_ms: int, end_ms: int | None = None,
                  timeout: int = _TIMEOUT) -> list[dict]:
    """OHLC candles (interval e.g. '1h','5m','15m'). Each HL candle: {t(open ms), T(close ms),
    o,h,l,c,v,n}. Normalized -> [{t,o,h,l,c,v}] sorted by t. Used as a market factor (BTC) for
    beta-neutral alpha and for intra-hold path (MFE/MAE) reconstruction."""
    req: dict = {"coin": coin, "interval": interval, "startTime": int(start_ms)}
    if end_ms is not None:
        req["endTime"] = int(end_ms)
    data = _post_info({"type": "candleSnapshot", "req": req}, timeout)
    out = [{"t": int(k["t"]), "o": _f(k.get("o")), "h": _f(k.get("h")), "l": _f(k.get("l")),
            "c": _f(k.get("c")), "v": _f(k.get("v"))}
           for k in (data or []) if isinstance(k, dict) and k.get("t") is not None]
    return sorted(out, key=lambda r: r["t"])
