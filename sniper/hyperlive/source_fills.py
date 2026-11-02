"""Prospective Hyperliquid source-fill capture for the frozen paper-copy cohort."""
from __future__ import annotations

from collections import deque
import json
import random
import threading
import time


HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
MAX_USER_SUBSCRIPTIONS = 10


def subscription_messages(wallets) -> list[str]:
    unique = sorted({str(wallet).strip().lower() for wallet in wallets if str(wallet).strip()})
    if len(unique) > MAX_USER_SUBSCRIPTIONS:
        raise ValueError(f"userFills subscriptions exceed {MAX_USER_SUBSCRIPTIONS}")
    return [json.dumps({
        "method": "subscribe", "subscription": {"type": "userFills", "user": wallet},
    }, separators=(",", ":")) for wallet in unique]


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_user_fills_message(message: dict, *, received_ts_ms: int,
                                 connection_epoch: int, recovery: bool = False) -> list[dict]:
    if not isinstance(message, dict) or message.get("channel") != "userFills":
        return []
    data = message.get("data") or {}
    wallet = str(data.get("user") or "").strip().lower()
    if not wallet:
        return []
    snapshot = int(bool(data.get("isSnapshot")))
    out = []
    for fill in data.get("fills") or []:
        try:
            tid = str(fill["tid"])
            coin = str(fill["coin"])
            source_ts = int(fill["time"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({
            "wallet": wallet, "tid": tid, "coin": coin,
            "side": str(fill.get("side") or ""),
            "direction": str(fill.get("dir") or ""),
            "px": _float(fill.get("px")), "sz": _float(fill.get("sz")),
            "start_position": _float(fill.get("startPosition")),
            "oid": str(fill["oid"]) if fill.get("oid") is not None else None,
            "tx_hash": fill.get("hash"),
            "source_event_ts_ms": source_ts,
            "received_ts_ms": int(received_ts_ms),
            "is_snapshot": snapshot,
            "is_recovery": int(bool(recovery)),
            "connection_epoch": int(connection_epoch),
        })
    return out


class SourceEventBuffer:
    def __init__(self, maxsize: int = 4096):
        self.maxsize = max(1, int(maxsize))
        self._rows = deque()
        self._lock = threading.Lock()
        self._overflow = 0
        self._gaps = {}

    def put_many(self, rows) -> int:
        accepted = 0
        with self._lock:
            for row in rows:
                if len(self._rows) >= self.maxsize:
                    self._overflow += 1
                    epoch = int(row.get("connection_epoch") or 0)
                    received = int(row.get("received_ts_ms") or 0)
                    gap = self._gaps.setdefault(epoch, {
                        "start_received_ts_ms": received, "end_received_ts_ms": received,
                        "connection_epoch": epoch, "dropped_count": 0,
                        "reason": "queue_overflow",
                    })
                    gap["start_received_ts_ms"] = min(gap["start_received_ts_ms"], received)
                    gap["end_received_ts_ms"] = max(gap["end_received_ts_ms"], received)
                    gap["dropped_count"] += 1
                    continue
                self._rows.append(dict(row))
                accepted += 1
        return accepted

    def drain(self, limit: int | None = None) -> list[dict]:
        out = []
        with self._lock:
            take = len(self._rows) if limit is None else min(len(self._rows), int(limit))
            for _ in range(take):
                out.append(self._rows.popleft())
        return out

    def drain_gaps(self) -> list[dict]:
        with self._lock:
            gaps = [dict(self._gaps[key]) for key in sorted(self._gaps)]
            self._gaps.clear()
        return gaps

    def status(self) -> dict:
        with self._lock:
            return {"queue_depth": len(self._rows), "overflow_count": self._overflow}


def match_entry(events, *, wallet: str, coin: str, side: str, prior_size: float,
                current_size: float, after_ts_ms: int, before_ts_ms: int) -> dict:
    wallet = str(wallet).lower()
    direction = "Open Long" if str(side).lower() == "long" else "Open Short"
    candidates = [
        event for event in events
        if str(event.get("wallet", "")).lower() == wallet
        and event.get("coin") == coin
        and event.get("direction") == direction
        and not int(event.get("is_snapshot") or 0)
        and int(after_ts_ms) < int(event.get("source_event_ts_ms") or -1) <= int(before_ts_ms)
        and int(event.get("received_ts_ms") or -1) <= int(before_ts_ms)
    ]
    if not candidates:
        return {"status": "unmatched", "tids": [], "reason": "no_causal_fill"}
    if any(str(event.get("clock_domain") or "corrected_utc") != "corrected_utc"
           for event in candidates):
        return {"status": "ambiguous", "tids": [str(event["tid"]) for event in candidates],
                "reason": "mixed_clock_domain"}
    delta = max(0.0, abs(float(current_size)) - abs(float(prior_size)))
    total = sum(abs(float(event.get("sz") or 0.0)) for event in candidates)
    tolerance = max(1e-9, delta * 0.02)
    if delta <= 0 or abs(total - delta) > tolerance:
        return {"status": "ambiguous", "tids": [event["tid"] for event in candidates],
                "reason": "fill_size_does_not_match_position_delta"}
    ordered = sorted(candidates, key=lambda event: (
        int(event["source_event_ts_ms"]), int(event["received_ts_ms"]), str(event["tid"])
    ))
    causal_completion = ordered[-1]
    recovered = any(int(event.get("is_recovery") or 0) for event in ordered)
    return {
        "status": "matched_recovery" if recovered else "matched",
        "reason": "exact_position_delta_recovery" if recovered else "exact_position_delta",
        "tids": [str(event["tid"]) for event in ordered],
        "source_event_ts_ms": int(causal_completion["source_event_ts_ms"]),
        "source_received_ts_ms": int(causal_completion["received_ts_ms"]),
    }


class SourceFillCollector:
    """One fail-open WebSocket thread; SQLite remains owned by the copy runner."""

    def __init__(self, wallets, *, now_ms, recovery_fetch, cursors=None,
                 buffer_size: int = 4096, connect=None, sleep=time.sleep):
        self.wallets = sorted({str(value).lower() for value in wallets})
        subscription_messages(self.wallets)
        self.now_ms = now_ms
        self.recovery_fetch = recovery_fetch
        self.cursors = {str(key).lower(): int(value) for key, value in (cursors or {}).items()}
        self.buffer = SourceEventBuffer(buffer_size)
        self.connect = connect
        self.sleep = sleep
        self._stop = threading.Event()
        self._thread = None
        self._state_lock = threading.Lock()
        self._state = {
            "connected": False, "connection_epoch": 0, "reconnect_count": 0,
            "recovery_count": 0, "last_event_ts_ms": None, "last_error": None,
            "instrumented_wallets": len(self.wallets),
        }

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="hl-source-fills", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def drain(self, limit: int | None = None) -> list[dict]:
        return self.buffer.drain(limit)

    def drain_gaps(self) -> list[dict]:
        return self.buffer.drain_gaps()

    def status(self) -> dict:
        with self._state_lock:
            state = dict(self._state)
        return {**state, **self.buffer.status()}

    def _set(self, **values):
        with self._state_lock:
            self._state.update(values)

    def _recover(self, epoch: int):
        end_ms = int(self.now_ms())
        for wallet in self.wallets:
            start_ms = int(self.cursors.get(wallet, end_ms))
            if start_ms > end_ms:
                continue
            rows = self.recovery_fetch(wallet, start_ms, end_ms) or []
            wrapped = {"channel": "userFills", "data": {
                "user": wallet, "isSnapshot": False, "fills": rows,
            }}
            normalized = normalize_user_fills_message(
                wrapped, received_ts_ms=int(self.now_ms()), connection_epoch=epoch, recovery=True,
            )
            self.buffer.put_many(normalized)
            if normalized:
                self.cursors[wallet] = max(row["source_event_ts_ms"] for row in normalized)
                self._set(recovery_count=self.status()["recovery_count"] + len(normalized))

    def _run(self):
        import websocket

        delay = 1.0
        while not self._stop.is_set():
            ws = None
            try:
                if self.connect is None:
                    ws = websocket.create_connection(HL_WS_URL, timeout=15)
                else:
                    ws = self.connect(HL_WS_URL, timeout=15)
                epoch = int(self.status()["connection_epoch"]) + 1
                self._set(connected=True, connection_epoch=epoch, last_error=None)
                for payload in subscription_messages(self.wallets):
                    ws.send(payload)
                if epoch > 1 or self.cursors:
                    self._recover(epoch)
                delay = 1.0
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        ws.send(json.dumps({"method": "ping"}, separators=(",", ":")))
                        continue
                    if not raw:
                        raise ConnectionError("empty websocket frame")
                    message = json.loads(raw)
                    rows = normalize_user_fills_message(
                        message, received_ts_ms=int(self.now_ms()), connection_epoch=epoch,
                    )
                    self.buffer.put_many(rows)
                    for row in rows:
                        self.cursors[row["wallet"]] = max(
                            self.cursors.get(row["wallet"], 0), row["source_event_ts_ms"]
                        )
                    if rows:
                        self._set(last_event_ts_ms=max(row["source_event_ts_ms"] for row in rows))
            except Exception as exc:
                state = self.status()
                self._set(connected=False, reconnect_count=state["reconnect_count"] + 1,
                          last_error=f"{type(exc).__name__}:{exc}")
                if self._stop.wait(delay + random.uniform(0.0, min(1.0, delay / 4))):
                    break
                delay = min(30.0, delay * 2.0)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
