#!/usr/bin/env python3
"""Advance the 5m forward paper book by one capture, for EACH configured asset (BTC, ETH, SOL by
default — `BTC5M_SYMBOLS` to override). Per asset: fetch the latest CLOSED 5m bars from Binance, run
the FROZEN signal, and open/close fee-adjusted PAPER positions on a signal change. Paper-only — never
places live orders. Idempotent per closed bar (won't double-count). One asset's failure is isolated
(logged; the others still run). Run on a 5m cron: `*/5 * * * * python3 scripts/btc5m_forward.py`.
Optional positional args restrict the run to specific symbols.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sniper.algos.btc5m import data as D, forward as FW  # noqa: E402


def advance_symbol(conn, symbol: str) -> int:
    """Advance one asset's forward book by one capture. Returns # closed paper trades booked."""
    # ensure we have a recent window for the signal lookback (>= params['n'] + buffer)
    fresh = D.fetch_klines(symbol, limit=300)
    D.save_bars(conn, fresh)
    bars = D.load_bars(conn, symbol, limit=500)
    if len(bars) < FW.FROZEN["params"]["n"] + 5:
        print(f"[{FW.label(symbol)}] not enough bars yet ({len(bars)})", file=sys.stderr)
        return 0

    # Binance returns the in-progress (not-yet-closed) bar last; drop it so we only act on CLOSED bars.
    now_ms = D.server_time_ms()
    closed = [b for b in bars if int(b["close_ms"]) <= now_ms]
    if not closed:
        return 0
    last_ms = int(closed[-1]["close_ms"])

    initialized = D.state_exists(conn, symbol)
    st = D.load_state(conn, symbol)
    if not initialized:
        D.save_state(conn, symbol, FW.new_state(), last_ms)
        print(f"[{FW.label(symbol)}] bootstrapped forward clock at bar={last_ms}; no historical paper trades",
              file=sys.stderr)
        return 0
    if int(st.get("last_bar_ms", 0)) >= last_ms:
        print(f"[{FW.label(symbol)}] no new closed bar (last={last_ms})", file=sys.stderr)
        return 0

    # REPLAY every bar since the last processed one (not just the latest) so an infrequent run / a
    # skipped cron doesn't undercount fires that opened+closed in the gap.
    since = int(st.get("last_bar_ms", 0))
    new_state, closed_trades = FW.advance(st, closed, since_ms=since, config=FW.FROZEN)
    for t in closed_trades:
        D.save_paper_trade(conn, symbol, t)
    D.save_state(conn, symbol, new_state, last_ms)

    pos = {1: "LONG", -1: "SHORT", 0: "FLAT"}[new_state["position"]]
    print(f"[{FW.label(symbol)}] bar={last_ms} px={closed[-1]['close']:.1f} -> {pos} "
          f"(+{len(closed_trades)} closed paper trade(s), replayed "
          f"{sum(1 for b in closed if int(b['close_ms'])>since)} bars)", file=sys.stderr)
    return len(closed_trades)


def main(argv=None) -> int:
    import db
    conn = db.get_connection()
    symbols = [s.strip().upper() for s in (argv if argv is not None else sys.argv[1:])] or FW.SYMBOLS
    for symbol in symbols:
        try:
            advance_symbol(conn, symbol)
        except Exception as e:  # noqa: BLE001 — isolate one asset's failure; the others still run
            print(f"[{FW.label(symbol)}] advance failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
