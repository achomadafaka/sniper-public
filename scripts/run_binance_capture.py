#!/usr/bin/env python3
"""Capture Binance USD-M futures market data (public, no keys) for the lead-lag study.

Append-only raw layer. Records source-event / transaction / local-receive timestamps
separately. Exits 2 on socket close so a watchdog can restart it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.research.leadlag import binance_capture as B  # noqa: E402


WS_BASE = "wss://fstream.binance.com/stream?streams="
DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/leadlag_capture.sqlite"
DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT,SOLUSDT"
DEFAULT_STREAMS = "bookTicker,trade,depth5@100ms"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stream_url(symbols: list[str], streams: list[str]) -> str:
    parts = [f"{sym.lower()}@{st}" for sym in symbols for st in streams]
    return WS_BASE + "/".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Binance USD-M lead-lag capture (public data only)")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    ap.add_argument("--streams", default=DEFAULT_STREAMS)
    ap.add_argument("--duration-seconds", type=int, default=0, help="0 = run until interrupted")
    ap.add_argument("--flush-every", type=int, default=200)
    args = ap.parse_args(argv)

    import websocket  # noqa: E402
    closed_exc = getattr(websocket, "WebSocketConnectionClosedException", None)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    streams = [s.strip() for s in args.streams.split(",") if s.strip()]
    conn = B.connect(args.db_path)
    B.ensure_schema(conn)

    ws = websocket.create_connection(_stream_url(symbols, streams), timeout=15)
    buf: list[dict] = []
    written = 0
    start = time.monotonic()
    try:
        while True:
            if args.duration_seconds > 0 and time.monotonic() - start >= args.duration_seconds:
                break
            ws.settimeout(5)
            try:
                raw = ws.recv()
            except TimeoutError:
                raw = None
            except Exception as e:
                if (closed_exc is not None and isinstance(e, closed_exc)) or "socket is already closed" in str(e).lower():
                    written += B.insert_rows(conn, buf)
                    print(f"binance capture closed; exiting for restart: {e}", file=sys.stderr)
                    return 2
                continue
            if not raw:
                if raw == "":
                    written += B.insert_rows(conn, buf)
                    print("binance capture empty frame; exiting for restart", file=sys.stderr)
                    return 2
            else:
                row = B.parse_message(json.loads(raw), _now_ms())
                if row:
                    buf.append(row)
            if len(buf) >= args.flush_every:
                written += B.insert_rows(conn, buf)
                buf = []
    finally:
        written += B.insert_rows(conn, buf)
        try:
            ws.close()
        except Exception:
            pass
        conn.close()
    print(f"binance_capture rows_written={written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
