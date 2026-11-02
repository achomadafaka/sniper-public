"""Network adapter for per-account fomo data (profile + recent swap ledger).

The ONLY network surface for the analyzer. fomo has NO working swap pagination
(verified 2026-07-06: page/offset/cursor/before all return the identical newest
100 swaps; limit caps at 100), so this does a SINGLE fetch and dedups by id —
never a page loop (a loop would just duplicate-inflate every downstream metric).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sniper.feeds.fomo_feed import BROWSER_HEADERS, fomo_get
from sniper.settings.memecoin import MEMECOIN_ANALYZER_SWAP_LIMIT


FOMO_ACTIVITY_WS = "wss://prod-api.fomo.family/ws"

_ACTIVITY_RECEIPT_FIELDS = (
    "type", "userId", "userHandle", "displayName", "tokenAddress", "tradeId", "id",
    "createdAt", "networkId", "ticker", "marketCap", "fdv", "price", "usdAmount",
)


def _ro(j):
    return j.get("responseObject", j) if isinstance(j, dict) else j


def get_profile(auth, uid, fetch=None):
    fetch = fetch or fomo_get
    _s, j, _d = fetch(auth, f"/v2/users/{uid}")
    return _ro(j) or {}


def fetch_leaderboard(auth, fetch=None, limit=50):
    """Return fomo's top-N leaderboard entries (server-side lifetime totalPnL over FULL
    history — the one number not bound by the 100-swap cap). limit caps at 50 server-side.
    Each entry carries totalPnL, topHoldings[] (per-token unrealized), totalVolume, etc.
    Returns [] on any non-200 / bad shape."""
    fetch = fetch or fomo_get
    try:
        st, j, _d = fetch(auth, f"/v2/leaderboard?limit={limit}")
    except Exception:
        return []
    if st != 200:
        return []
    o = _ro(j)
    lb = o.get("leaderboard") if isinstance(o, dict) else None
    return lb or []


def fetch_balances(auth, uid, fetch=None):
    """Return the trader's live balances payload: {balances[], livePerpPnl, otherPnl,
    nativeEvmBalances[]}. This is the mark-to-market leg the fomo UI uses that the
    realized-only swap reconstruction cannot see. Returns {} on any non-200 / bad shape
    so the analyzer degrades gracefully (unrealized fields just go NULL)."""
    fetch = fetch or fomo_get
    try:
        st, j, _d = fetch(auth, f"/v2/users/{uid}/balances")
    except Exception:
        return {}
    if st != 200:
        return {}
    o = _ro(j)
    return o if isinstance(o, dict) else {}


def fetch_recent_swaps(auth, uid, fetch=None, limit=None):
    """Return the newest <=100 swaps for a trader, deduped by id (first-seen order).
    Single call — fomo's pagination is a no-op, so looping would only duplicate."""
    fetch = fetch or fomo_get
    limit = limit or MEMECOIN_ANALYZER_SWAP_LIMIT
    _s, j, _d = fetch(auth, f"/v2/users/{uid}/swaps?limit={limit}")
    o = _ro(j)
    raw = (o.get("swaps") if isinstance(o, dict) else None) or []
    seen = set()
    out = []
    for s in raw:
        sid = s.get("id")
        if sid is not None and sid in seen:   # only dedup real ids; missing id -> keep
            continue
        if sid is not None:
            seen.add(sid)
        out.append(s)
    return out


def activity_payload_to_swap(payload):
    """Convert one Fomo UI trading-activity item to the existing swap shape."""
    if not isinstance(payload, dict) or payload.get("type") not in {"swap_buy", "swap_sell"}:
        return None
    uid = str(payload.get("userId") or "")
    mint = str(payload.get("tokenAddress") or "")
    sid = str(payload.get("tradeId") or payload.get("id") or "")
    created_at = str(payload.get("createdAt") or "")
    if not uid or not mint or not sid or not created_at:
        return None
    try:
        usd = float(payload.get("usdAmount") or 0)
        price = float(payload.get("price") or 0)
    except (TypeError, ValueError):
        return None
    if usd <= 0 or price <= 0:
        return None
    try:
        market_cap = float(payload.get("marketCap") or payload.get("fdv") or 0)
    except (TypeError, ValueError):
        market_cap = 0.0
    qty = usd / price if price > 0 else 0.0
    cash = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    buy = payload["type"] == "swap_buy"
    return {
        "id": sid,
        "userId": uid,
        "userHandle": str(payload.get("userHandle") or payload.get("displayName") or ""),
        "inTokenAddress": cash if buy else mint,
        "outTokenAddress": mint if buy else cash,
        "humanUsdAmountIn": usd,
        "humanUsdAmountOut": usd,
        "inHumanAmount": usd if buy else qty,
        "outHumanAmount": qty if buy else usd,
        "createdAt": created_at,
        "networkId": payload.get("networkId"),
        "ticker": str(payload.get("ticker") or ""),
        "marketCap": market_cap,
    }


def activity_receipt_observation(payload, observed_at):
    """Build an audit-only receipt without unknown fields or credentials."""
    import hashlib

    payload = payload if isinstance(payload, dict) else {}
    safe = {key: payload[key] for key in _ACTIVITY_RECEIPT_FIELDS if key in payload}
    canonical = json.dumps(safe, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    receipt_id = str(payload.get("tradeId") or payload.get("id") or "")
    if not receipt_id:
        receipt_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]

    def utc_iso(value):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            return None

    source_raw = str(payload.get("createdAt") or "")
    source_iso = utc_iso(source_raw)
    observed_parsed = utc_iso(observed_at)
    observed_iso = observed_parsed or str(observed_at)
    lag_s = None
    if source_iso and observed_parsed:
        lag_s = (
            datetime.fromisoformat(observed_parsed) - datetime.fromisoformat(source_iso)
        ).total_seconds()

    def number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return {
        "upstream_event_id": "ws:" + receipt_id,
        "raw_type": "ws_receipt",
        "normalized_side": None,
        "source_user_id": str(payload.get("userId") or ""),
        "source_handle": str(payload.get("userHandle") or payload.get("displayName") or ""),
        "network_id": str(payload.get("networkId") or ""),
        "mint": str(payload.get("tokenAddress") or ""),
        "ticker": str(payload.get("ticker") or ""),
        "their_price": number(payload.get("price")),
        "their_market_cap": number(payload.get("marketCap") or payload.get("fdv")),
        "their_size_usd": number(payload.get("usdAmount")),
        "source_ts_raw": source_raw,
        "source_ts": source_iso,
        "observed_at": observed_iso,
        "lag_s": lag_s,
        "raw_payload": {
            "delivery_lane": "websocket",
            "parser_status": "accepted" if activity_payload_to_swap(payload) else "ignored",
            "payload": safe,
        },
    }


def stream_trading_activity(auth, topic_id, *, connect_fn=None, on_subscribed=None):
    """Yield the Fomo UI trading-activity payloads for the current Fomo user."""
    if connect_fn is None:
        from websockets.sync.client import connect as connect_fn
    with connect_fn(
        FOMO_ACTIVITY_WS,
        origin="https://fomo.family",
        additional_headers={"User-Agent": BROWSER_HEADERS["user-agent"]},
        open_timeout=10,
        close_timeout=2,
    ) as socket:
        while True:
            try:
                raw = socket.recv(timeout=30)
            except TimeoutError:
                pong = socket.ping()
                if not pong.wait(5):
                    raise TimeoutError("Fomo WebSocket ping timeout")
                continue
            if not raw:
                raise ConnectionError("Fomo WebSocket closed")
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            kind = message.get("type")
            if kind == "challenge":
                socket.send(json.dumps({"type": "challengeResponse", "jwt": auth.access}))
            elif kind == "challengeAccepted":
                socket.send(json.dumps({
                    "type": "subscribe",
                    "topicType": "trading_activity",
                    "topicId": str(topic_id),
                }))
            elif (kind == "subscribed" and message.get("topicType") == "trading_activity"
                  and str(message.get("topicId")) == str(topic_id)):
                if on_subscribed is not None:
                    on_subscribed()
            elif kind == "error":
                raise RuntimeError(
                    "Fomo WebSocket error %s: %s"
                    % (message.get("code"), message.get("message"))
                )
            elif (kind == "data" and message.get("topicType") == "trading_activity"
                  and str(message.get("topicId")) == str(topic_id)
                  and isinstance(message.get("payload"), dict)):
                yield message["payload"]
