"""Capture coordinator — per-token CLOB book dispatch, gap detection, snapshot persistence.

The pure coordination logic lives here and is unit-tested; the live WS/REST loop (sockets, PING,
reconnect timing) is assembled in ``scripts/run_polymarket_crypto5m_capture.py`` and exercised by the
read-only live smoke. An uncertain book is never treated as executable truth — gaps raise quality
flags and force a REST re-snapshot.
"""
from __future__ import annotations

from typing import Optional

from . import persistence as P
from .book_state import BookState
from .models import MarketSpec, QualityFlag

CLOB_BOOK_URL = "https://clob.polymarket.com/book"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _event_type(msg: dict) -> Optional[str]:
    return msg.get("event_type") or msg.get("type")


class CaptureCoordinator:
    """Holds the Up/Down books for one market window and applies incoming CLOB messages to the right
    one. Never infers a token's price from its complement."""

    def __init__(self, spec: MarketSpec, *, stale_book_ms: int = 3000):
        self.spec = spec
        self.stale_book_ms = stale_book_ms
        self.reconnect_generation = 0
        self._books = {
            spec.up_token_id: BookState(token_id=spec.up_token_id, condition_id=spec.condition_id,
                                        side="Up"),
            spec.down_token_id: BookState(token_id=spec.down_token_id,
                                          condition_id=spec.condition_id, side="Down"),
        }

    def book_for(self, token_id: str) -> Optional[BookState]:
        return self._books.get(token_id)

    def mark_reconnect(self) -> int:
        self.reconnect_generation += 1
        for b in self._books.values():
            b.reconnect_generation = self.reconnect_generation
        return self.reconnect_generation

    def ingest_clob(self, msg: dict, *, recv_ts: int) -> dict:
        """Apply a CLOB market-channel message to the correct token book; return a result dict.

        The dict always carries an ``events`` list of per-level persist records
        (``{event_type, token_id, side, flags, payload}``) — one entry for a single-token message,
        one entry per changed level for a multi-asset ``price_change``. The top-level
        ``token_id``/``side``/``flags`` mirror the single-token case for backward compatibility; for
        ``price_change`` (which spans tokens) the top-level ``token_id`` is None and callers must use
        ``events``.
        """
        etype = _event_type(msg)
        if etype == "price_change":
            # Polymarket price_change has NO top-level asset_id; each price_changes[] item carries its
            # own asset_id and one frame can span both tokens. Route each per its own asset_id.
            return self._ingest_price_change(msg, recv_ts=recv_ts)

        flags: set = set()
        token_id = msg.get("asset_id") or msg.get("token_id")
        book = self._books.get(token_id)
        if book is None:
            flags.add(QualityFlag.TOKEN_UNMAPPED.value)
            return {"event_type": etype, "token_id": token_id, "side": None, "flags": flags,
                    "events": [{"event_type": etype, "token_id": token_id, "side": None,
                                "flags": flags, "payload": msg}]}

        exch_ts = msg.get("timestamp")
        if (exch_ts is not None and book.exchange_ts is not None
                and int(exch_ts) < int(book.exchange_ts)):
            flags.add(QualityFlag.TIMESTAMP_REGRESSION.value)

        try:
            if etype == "book":
                book.apply_book(bids=msg.get("bids"), asks=msg.get("asks"),
                                book_hash=msg.get("hash"), tick_size=msg.get("tick_size"),
                                min_order_size=msg.get("min_order_size"),
                                exchange_ts=exch_ts, recv_ts=recv_ts)
            elif etype == "best_bid_ask":
                book.apply_best_bid_ask(best_bid=msg.get("best_bid"), best_ask=msg.get("best_ask"),
                                        spread=msg.get("spread"))
            elif etype == "last_trade_price":
                book.apply_last_trade_price(price=msg.get("price"), side=msg.get("side"),
                                            size=msg.get("size"),
                                            fee_rate_bps=msg.get("fee_rate_bps"))
            elif etype == "tick_size_change":
                book.apply_tick_size_change(old=msg.get("old_tick_size"),
                                            new=msg.get("new_tick_size"))
            else:
                flags.add(QualityFlag.MALFORMED_UPDATE.value)
        except (KeyError, TypeError, ValueError):
            flags.add(QualityFlag.MALFORMED_UPDATE.value)

        return {"event_type": etype, "token_id": token_id, "side": book.side, "flags": flags,
                "events": [{"event_type": etype, "token_id": token_id, "side": book.side,
                            "flags": flags, "payload": msg}]}

    @staticmethod
    def _level_payload(msg: dict, ch: dict) -> dict:
        """One persist row per changed level: the nested change plus parent frame context."""
        return {"event_type": "price_change", "market": msg.get("market"),
                "timestamp": msg.get("timestamp"), **ch}

    def _ingest_price_change(self, msg: dict, *, recv_ts: int) -> dict:
        exch_ts = msg.get("timestamp")
        groups: dict = {}
        for ch in (msg.get("price_changes") or []):
            groups.setdefault(ch.get("asset_id"), []).append(ch)

        all_flags: set = set()
        events: list = []
        for tid, changes in groups.items():
            book = self._books.get(tid)
            if book is None:
                all_flags.add(QualityFlag.TOKEN_UNMAPPED.value)
                for ch in changes:
                    events.append({"event_type": "price_change", "token_id": tid, "side": None,
                                   "flags": {QualityFlag.TOKEN_UNMAPPED.value},
                                   "payload": self._level_payload(msg, ch)})
                continue
            flags: set = set()
            if (exch_ts is not None and book.exchange_ts is not None
                    and int(exch_ts) < int(book.exchange_ts)):
                flags.add(QualityFlag.TIMESTAMP_REGRESSION.value)
            try:
                book.apply_price_change(changes, exchange_ts=exch_ts, recv_ts=recv_ts)
            except (KeyError, TypeError, ValueError):
                flags.add(QualityFlag.MALFORMED_UPDATE.value)
            all_flags |= flags
            for ch in changes:
                events.append({"event_type": "price_change", "token_id": tid, "side": book.side,
                               "flags": flags, "payload": self._level_payload(msg, ch)})

        return {"event_type": "price_change", "token_id": None, "side": None, "flags": all_flags,
                "events": events}

    def is_stale(self, token_id: str, *, now: int, max_gap: float) -> bool:
        """True if the token's book hasn't been updated within ``max_gap`` (same unit as ``now`` and
        the stored ``recv_ts``). An unseen book is stale by definition."""
        book = self._books.get(token_id)
        if book is None or book.recv_ts is None:
            return True
        return (now - int(book.recv_ts)) > max_gap

    def stale_tokens(self, *, now: int, max_gap: float) -> list:
        return [tid for tid in self._books if self.is_stale(tid, now=now, max_gap=max_gap)]

    def persist_snapshot(self, conn, token_id: str, *, code_version: str, source_event: str,
                         recv_ts: int, now_ts: int, flags=None) -> int:
        book = self._books[token_id]
        levels = book.snapshot_levels()
        return P.write_snapshot(
            conn, condition_id=book.condition_id, token_id=token_id, side=book.side,
            bids=levels["bids"], asks=levels["asks"], best_bid=book.best_bid(),
            best_ask=book.best_ask(), spread=book.spread(), book_hash=book.book_hash,
            tick_size=book.tick_size, min_order_size=book.min_order_size,
            exchange_ts=book.exchange_ts, recv_ts=recv_ts, source_event=source_event,
            quality_flags=list(flags or []), reconnect_generation=self.reconnect_generation,
            code_version=code_version, now_ts=now_ts,
        )
