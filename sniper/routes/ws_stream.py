"""
WebSocket price stream endpoint for the Sniper dashboard.

The browser connects to ws://host/stream, sends a JSON message:
    {"subscribe": [{"condition_id": "0x...", "outcome": "YES"}, ...]}

The server pushes back prices from the Polymarket live feed:
    {"prices": [{"condition_id": "0x...", "outcome": "YES", "price": 0.72}, ...]}

Architecture:
  A single persistent WebSocket to wss://ws-subscriptions-clob.polymarket.com/ws/market
  receives last_trade_price events for all watched token IDs and caches them in
  _ws_price_cache.  Each browser connection's poll_loop reads from this cache (cheap
  dict lookup) rather than making 100 parallel REST requests every 2s.

  REST polling is used only as a one-time cold-start fill for markets that have not
  yet traded since the Polymarket WS connection was opened.

The browser can send new subscribe messages at any time to update
the watched set (e.g. when switching tabs).
"""

import json
import importlib.util
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask_sock import Sock

logger = logging.getLogger(__name__)

import threading as _threading

# ---------------------------------------------------------------------------
# Global ticker cache — refreshed by one background thread, read by all WS
# connections. This means every connected browser tab gets ticker data from
# a single external fetch, not N separate polling loops.
# ---------------------------------------------------------------------------
_ticker_lock = _threading.Lock()
_ticker_cache: dict = {}          # {symbol: {price, change_pct, direction}}
_ticker_last_fetch: float = 0.0   # unix timestamp of last successful fetch
_TICKER_TTL = 30.0                # seconds between refreshes
_TICKER_PUSH_INTERVAL = 30        # push ticker to WS every N price-poll cycles


def _fetch_ticker_data() -> dict:
    """Fetch index + crypto prices. Returns {symbol: {price, change_pct, direction}}.

    Uses yfinance for stock indices and CoinGecko for crypto.
    Both are free with no API key required.
    Falls back gracefully if either source fails.
    """
    result = {}

    # --- Stock indices via yfinance ---
    try:
        import yfinance as yf
        symbols = {
            "^GSPC":  "S&P 500",
            "^IXIC":  "NASDAQ",
            "^DJI":   "DOW",
            "^VIX":   "VIX",
            "^TNX":   "10Y",
            "GC=F":   "GOLD",
            "CL=F":   "OIL",
        }
        tickers = yf.Tickers(" ".join(symbols.keys()))
        for sym, label in symbols.items():
            try:
                info = tickers.tickers[sym].fast_info
                price = float(info.last_price or 0)
                prev = float(info.previous_close or price)
                change_pct = ((price - prev) / prev * 100) if prev else 0.0
                if price > 0:
                    result[label] = {
                        "price": round(price, 2),
                        "change_pct": round(change_pct, 2),
                        "direction": "up" if change_pct > 0 else ("down" if change_pct < 0 else "flat"),
                        "symbol": sym,
                    }
            except Exception:
                pass
    except Exception:
        pass

    # --- Crypto via CoinGecko (no API key, free) ---
    try:
        import requests as _req
        resp = _req.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": "bitcoin,ethereum,solana",
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
            timeout=6,
        )
        if resp.ok:
            data = resp.json()
            mapping = {
                "bitcoin":  "BTC",
                "ethereum": "ETH",
                "solana":   "SOL",
            }
            for coin_id, label in mapping.items():
                if coin_id in data:
                    price = float(data[coin_id].get("usd", 0))
                    change = float(data[coin_id].get("usd_24h_change", 0))
                    if price > 0:
                        result[label] = {
                            "price": round(price, 2),
                            "change_pct": round(change, 2),
                            "direction": "up" if change > 0 else ("down" if change < 0 else "flat"),
                            "symbol": coin_id,
                        }
    except Exception:
        pass

    return result


def _ticker_refresh_loop():
    """Background thread: refresh ticker data every _TICKER_TTL seconds."""
    global _ticker_cache, _ticker_last_fetch
    import time as _time
    while True:
        try:
            data = _fetch_ticker_data()
            if data:
                with _ticker_lock:
                    _ticker_cache = data
                    _ticker_last_fetch = _time.time()
                logger.debug("Ticker refreshed: %d symbols", len(data))
        except Exception as e:
            logger.debug("Ticker refresh failed: %s", e)
        _time.sleep(_TICKER_TTL)


def _get_ticker_snapshot() -> dict:
    """Return a copy of the current ticker cache (thread-safe)."""
    import time as _time
    _ensure_ticker_thread()
    with _ticker_lock:
        age = _time.time() - _ticker_last_fetch
        return {
            "symbols": dict(_ticker_cache),
            "age_seconds": round(age, 0),
            "stale": age > _TICKER_TTL * 3,
        }


_ticker_thread: _threading.Thread | None = None
_ticker_thread_lock = _threading.Lock()


def _ensure_ticker_thread() -> None:
    """Start the ticker refresh thread on first use.

    Import-time startup can race with test collection because yfinance imports
    NumPy in the background. Lazy startup keeps imports deterministic while
    preserving the dashboard ticker behavior.
    """
    global _ticker_thread
    if _ticker_thread and _ticker_thread.is_alive():
        return
    with _ticker_thread_lock:
        if _ticker_thread and _ticker_thread.is_alive():
            return
        _ticker_thread = _threading.Thread(
            target=_ticker_refresh_loop,
            name="ticker-refresh",
            daemon=True,
        )
        _ticker_thread.start()


# ---------------------------------------------------------------------------
# Polymarket live price feed — single persistent WS, shared across all tabs
# ---------------------------------------------------------------------------
# The public market channel subscribes by asset_id/token_id, not condition_id.
# Browser subscriptions therefore resolve each watched (condition_id, outcome)
# pair to the matching token_id before registering it with the shared upstream WS.

POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_MARKET_URL = "https://clob.polymarket.com/markets/{}"
POLY_WS_HEARTBEAT_SECONDS = 10.0

_ws_price_cache: dict[str, float] = {}  # token_id -> latest price
_ws_price_lock = _threading.Lock()

_ws_watch_counts: dict[str, int] = {}   # token_id -> active browser subscriptions
_ws_sub_lock = _threading.Lock()
_ws_needs_resub = _threading.Event()

_market_token_cache: dict[str, dict[str, str]] = {}  # condition_id -> {OUTCOME: token_id}
_market_token_lock = _threading.Lock()


def _normalize_outcome_label(value: str | None) -> str:
    return str(value or "").strip().upper()


def _current_poly_token_ids() -> set[str]:
    with _ws_sub_lock:
        return {token_id for token_id, count in _ws_watch_counts.items() if count > 0}


def _replace_poly_ws_tokens(previous_ids: set[str], new_ids: set[str]) -> None:
    """Adjust upstream token refcounts for one browser connection."""
    removed = previous_ids - new_ids
    added = new_ids - previous_ids
    if not removed and not added:
        return
    with _ws_sub_lock:
        for token_id in removed:
            current = _ws_watch_counts.get(token_id, 0)
            if current <= 1:
                _ws_watch_counts.pop(token_id, None)
            else:
                _ws_watch_counts[token_id] = current - 1
        for token_id in added:
            _ws_watch_counts[token_id] = _ws_watch_counts.get(token_id, 0) + 1
        _ws_needs_resub.set()


def _fetch_market_tokens(session: requests.Session, condition_id: str) -> dict[str, str]:
    """Resolve outcome -> token_id for a market using the CLOB market endpoint."""
    cid = str(condition_id or "").strip()
    if not cid:
        return {}
    with _market_token_lock:
        cached = _market_token_cache.get(cid)
    if cached is not None:
        return dict(cached)

    token_map: dict[str, str] = {}
    try:
        resp = session.get(CLOB_MARKET_URL.format(cid), timeout=REQUEST_TIMEOUT)
        if resp.ok:
            payload = resp.json()
            for token in payload.get("tokens", []):
                outcome = _normalize_outcome_label(token.get("outcome"))
                token_id = str(token.get("token_id") or token.get("asset_id") or "").strip()
                if outcome and token_id:
                    token_map[outcome] = token_id
    except Exception as exc:
        logger.debug("Failed to resolve token ids for %s: %s", cid, exc)

    if token_map:
        with _market_token_lock:
            _market_token_cache[cid] = dict(token_map)
    return token_map


def _resolve_subscription_pairs(
    raw_pairs: list,
    session: requests.Session,
) -> dict[tuple[str, str], str]:
    """Normalize browser subscriptions into (condition_id, outcome) -> token_id."""
    resolved: dict[tuple[str, str], str] = {}
    for item in raw_pairs or []:
        if not isinstance(item, dict):
            continue
        condition_id = str(item.get("condition_id") or "").strip()
        outcome = str(item.get("outcome") or "").strip()
        if not condition_id or not outcome:
            continue
        token_map = _fetch_market_tokens(session, condition_id)
        token_id = token_map.get(_normalize_outcome_label(outcome))
        if token_id:
            resolved[(condition_id, outcome)] = token_id
    return resolved


def _handle_poly_price_msg(msg: dict) -> None:
    """Update the shared token_id -> price cache from a Polymarket WS message."""
    if str(msg.get("event_type") or "").lower() != "last_trade_price":
        return
    token_id = str(msg.get("asset_id") or "")
    price_raw = msg.get("price")
    if not token_id or price_raw is None:
        return
    try:
        price = float(price_raw)
    except (TypeError, ValueError):
        return
    if 0.01 < price < 0.99:
        with _ws_price_lock:
            _ws_price_cache[token_id] = price


def _has_sync_websockets() -> bool:
    """Return True when the upstream sync websocket client is importable."""
    try:
        return importlib.util.find_spec("websockets.sync.client") is not None
    except ModuleNotFoundError:
        return False


def _poly_ws_price_loop() -> None:
    """
    Background thread: maintain one upstream market-channel WS keyed by token_id.

    Polymarket's market channel requires:
      - asset IDs in subscriptions
      - a valid subscription shortly after connect
      - explicit `PING` heartbeats every 10s
    """
    if not _has_sync_websockets():
        logger.info("poly_ws_price: websockets.sync.client unavailable; upstream WS disabled")
        return

    import inspect
    from websockets.sync.client import connect as _ws_connect

    connect_sig = inspect.signature(_ws_connect)
    connect_kwargs = {"open_timeout": 10}
    if "ping_interval" in connect_sig.parameters:
        # We send Polymarket's protocol-level PING frames ourselves below.
        connect_kwargs["ping_interval"] = None

    backoff = 1.0
    while True:
        desired_ids = _current_poly_token_ids()
        if not desired_ids:
            _ws_needs_resub.wait(timeout=5.0)
            _ws_needs_resub.clear()
            continue
        try:
            with _ws_connect(POLY_WS_URL, **connect_kwargs) as ws:
                logger.info("poly_ws_price: connected")
                backoff = 1.0
                active_ids: set[str] = set()
                last_ping = 0.0

                while True:
                    desired_ids = _current_poly_token_ids()
                    if desired_ids != active_ids:
                        if not desired_ids:
                            logger.info("poly_ws_price: no watched assets, closing upstream socket")
                            break
                        if not active_ids:
                            ws.send(json.dumps({
                                "type": "market",
                                "assets_ids": sorted(desired_ids),
                                "custom_feature_enabled": True,
                            }))
                            logger.debug("poly_ws_price: subscribed to %d ids", len(desired_ids))
                        else:
                            to_sub = desired_ids - active_ids
                            to_unsub = active_ids - desired_ids
                            if to_sub:
                                ws.send(json.dumps({
                                    "operation": "subscribe",
                                    "assets_ids": sorted(to_sub),
                                    "custom_feature_enabled": True,
                                }))
                            if to_unsub:
                                ws.send(json.dumps({
                                    "operation": "unsubscribe",
                                    "assets_ids": sorted(to_unsub),
                                }))
                            logger.debug(
                                "poly_ws_price: subscription delta +%d/-%d",
                                len(to_sub),
                                len(to_unsub),
                            )
                        active_ids = set(desired_ids)
                        _ws_needs_resub.clear()

                    now = time.monotonic()
                    if now - last_ping >= POLY_WS_HEARTBEAT_SECONDS:
                        ws.send("PING")
                        last_ping = now

                    try:
                        raw = ws.recv(timeout=3.0)
                    except TimeoutError:
                        continue

                    if raw in {"PONG", "PING", None}:
                        continue

                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue

                    if isinstance(data, list):
                        for item in data:
                            _handle_poly_price_msg(item)
                    else:
                        _handle_poly_price_msg(data)

        except Exception as exc:
            logger.warning("poly_ws_price: disconnected (%s) — retry in %ds", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


# STARTED WITH THE WEB APP, NOT AT IMPORT (2026-09-29). It used to start at import, so every process
# that imported this module -- every pytest xdist worker, via conftest's server import -- ran a live
# upstream socket loop that sleeps on the process-wide time.sleep and reads the process-wide
# time.monotonic per message. `register_ws_stream` (called by the web server at its import, as before)
# starts it once; conftest sets `_POLY_WS_AUTOSTART = False`.
_poly_ws_thread = None
_poly_ws_lock = _threading.Lock()
_POLY_WS_AUTOSTART = True


def _start_poly_ws_thread(force: bool = False):
    """Start the upstream price socket once per process; returns it, or None when not started."""
    global _poly_ws_thread
    if not (_POLY_WS_AUTOSTART or force):
        return None
    with _poly_ws_lock:
        if _poly_ws_thread is not None:
            return _poly_ws_thread
        if not _has_sync_websockets():
            logger.info("poly_ws_price: websocket dependency missing; thread not started")
            return None
        _poly_ws_thread = _threading.Thread(
            target=_poly_ws_price_loop, name="poly-ws-prices", daemon=True
        )
        _poly_ws_thread.start()
        return _poly_ws_thread


# ---------------------------------------------------------------------------
# REST fallback — used only for cold-start fill of markets with no cached price
# ---------------------------------------------------------------------------
POLL_INTERVAL = 2.0  # seconds between browser pushes
PRICE_URL = "https://clob.polymarket.com/last-trade-price"
REQUEST_TIMEOUT = 4  # seconds per price request
MAX_CONDITION_IDS = 100  # cap to avoid overloading
MAX_WORKERS = 20  # concurrent requests for cold-start REST fill

MIDPOINT_URL = "https://clob.polymarket.com/midpoint"
PRICE_SENTINEL = 0.5  # Polymarket returns 0.5 when no recent trade exists


def _fetch_price(session: requests.Session, token_id: str) -> tuple[str, float | None]:
    """Fetch the live price for a single token_id.

    Strategy:
      1. Try CLOB midpoint (best bid+ask average) — most accurate for active markets.
      2. Fall back to last-trade-price.
      3. Return None if price is exactly 0.5 (Polymarket sentinel for "no data").
    """
    try:
        # 1. Try midpoint first (accurate for active orderbooks)
        resp = session.get(MIDPOINT_URL, params={"token_id": token_id}, timeout=REQUEST_TIMEOUT)
        if resp.ok:
            data = resp.json()
            mid = data.get("mid")
            if mid is not None:
                price = float(mid)
                if price != PRICE_SENTINEL:
                    return token_id, price
    except Exception as e:
        logger.debug("Midpoint fetch error for %s: %s", token_id, e)

    try:
        # 2. Fall back to last-trade-price
        resp = session.get(PRICE_URL, params={"token_id": token_id}, timeout=REQUEST_TIMEOUT)
        if resp.ok:
            data = resp.json()
            price_raw = data.get("price")
            if price_raw is not None:
                price = float(price_raw)
                # 0.5 is Polymarket's sentinel for "no recent trade" — skip it
                # so the browser keeps the DB-stored price instead of showing stale 50¢
                if price != PRICE_SENTINEL:
                    return token_id, price
    except Exception as e:
        logger.debug("Price fetch error for %s: %s", token_id, e)

    return token_id, None


def _fetch_prices_batch(
    token_ids: list[str],
    session: requests.Session,
    executor: ThreadPoolExecutor,
) -> dict[str, float]:
    """Fetch prices for all token_ids concurrently.

    Accepts a pre-created session and executor so callers can reuse them across
    poll cycles instead of constructing and tearing down 20+ threads every 2s.
    """
    results = {}
    if not token_ids:
        return results
    futures = {executor.submit(_fetch_price, session, token_id): token_id for token_id in token_ids}
    for future in as_completed(futures):
        token_id, price = future.result()
        if price is not None:
            results[token_id] = price
    return results


def register_ws_stream(app, sock: Sock):
    """Register the /stream WebSocket endpoint on the Flask app, and start the upstream socket."""
    _start_poly_ws_thread()

    @sock.route("/stream")
    def price_stream(ws):
        """WebSocket handler — runs in its own thread per connection."""
        logger.info("WS client connected")
        watched_pairs: dict[tuple[str, str], str] = {}
        stop_event = threading.Event()
        resolve_session = requests.Session()

        def poll_loop():
            # Session + executor kept for one-time cold-start REST fill only.
            # Steady-state price delivery comes from _ws_price_cache (free).
            _session = requests.Session()
            _executor = ThreadPoolExecutor(
                max_workers=MAX_WORKERS, thread_name_prefix="ws-price"
            )
            _cold_fetched: set[str] = set()   # token_ids already REST-filled once
            cycle = 0
            try:
                while not stop_event.is_set():
                    if watched_pairs:
                        items = list(watched_pairs.items())[:MAX_CONDITION_IDS]
                        price_rows: list[dict[str, object]] = []

                        with _ws_price_lock:
                            cached_prices = dict(_ws_price_cache)

                        uncached_token_ids: list[str] = []
                        uncached_pairs: list[tuple[tuple[str, str], str]] = []
                        for (condition_id, outcome), token_id in items:
                            if token_id in cached_prices:
                                price_rows.append({
                                    "condition_id": condition_id,
                                    "outcome": outcome,
                                    "price": cached_prices[token_id],
                                })
                            elif token_id not in _cold_fetched:
                                uncached_token_ids.append(token_id)
                                uncached_pairs.append(((condition_id, outcome), token_id))

                        if uncached_token_ids:
                            rest = _fetch_prices_batch(uncached_token_ids, _session, _executor)
                            _cold_fetched.update(uncached_token_ids)
                            with _ws_price_lock:
                                _ws_price_cache.update(rest)
                            for (condition_id, outcome), token_id in uncached_pairs:
                                if token_id in rest:
                                    price_rows.append({
                                        "condition_id": condition_id,
                                        "outcome": outcome,
                                        "price": rest[token_id],
                                    })

                        msg = {}
                        if price_rows:
                            msg["prices"] = price_rows
                        # Push ticker every 30 cycles (~60s)
                        if cycle % 30 == 0:
                            snapshot = _get_ticker_snapshot()
                            if snapshot["symbols"]:
                                msg["ticker"] = snapshot
                        if msg:
                            try:
                                ws.send(json.dumps(msg))
                            except Exception:
                                stop_event.set()
                                return
                    cycle += 1
                    time.sleep(POLL_INTERVAL)
            finally:
                _executor.shutdown(wait=False)
                _session.close()

        # Start poll loop in background thread
        poll_thread = threading.Thread(target=poll_loop, daemon=True)
        poll_thread.start()

        try:
            while True:
                msg = ws.receive(timeout=30)
                if msg is None:
                    # Timeout — send a keepalive ping
                    try:
                        ws.send(json.dumps({"ping": True}))
                    except Exception:
                        break
                    continue
                try:
                    data = json.loads(msg)
                    if "subscribe" in data:
                        resolved = _resolve_subscription_pairs(
                            list(data["subscribe"])[:MAX_CONDITION_IDS],
                            resolve_session,
                        )
                        new_token_ids = set(resolved.values())
                        _replace_poly_ws_tokens(set(watched_pairs.values()), new_token_ids)
                        watched_pairs = resolved
                        logger.debug("WS subscribed to %d market sides", len(watched_pairs))
                    elif "unsubscribe" in data:
                        previous_token_ids = set(watched_pairs.values())
                        watched_pairs = {}
                        _replace_poly_ws_tokens(previous_token_ids, set())
                except json.JSONDecodeError:
                    pass
        except Exception as e:
            logger.debug("WS connection closed: %s", e)
        finally:
            stop_event.set()
            _replace_poly_ws_tokens(set(watched_pairs.values()), set())
            resolve_session.close()
            logger.info("WS client disconnected")
