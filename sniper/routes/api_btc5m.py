"""Read-only API for the BTC 5m algorithm surface. Display-only — paper trades + backtest summary;
NEVER places orders. /status = live price + current signal + forward paper verdict; /backtest =
the frozen in-sample/OOS sweep summary."""
from __future__ import annotations

import json
import math
import os

from flask import Blueprint, jsonify


def _json_safe(o):
    """Coerce non-finite floats (Infinity/NaN) to None recursively. jsonify serializes float('inf')
    as the bare token `Infinity`, which is INVALID JSON — browsers' JSON.parse rejects it (e.g. a
    profit_factor of +inf from a no-loss paper book). This keeps every payload strictly valid JSON."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BEST_CONFIG = os.path.join(ROOT, "runs", "btc5m", "best_config.json")
SYMBOL = "BTCUSDT"
DISCLAIMER = ("Paper-only algorithmic backtest/forward test — NOT advice, not live trading. Fills are "
              "booked at the NEXT bar's close (1-bar execution lag); the model does NOT simulate "
              "limit-order fill probability — a resting quote at the band suffers adverse selection "
              "(it tends to fill on the losing cases), so live results carry an extra unmeasured drag. "
              "Backtest config is selected on train and checked on a short OOS holdout; unproven until "
              "the pre-registered forward pass bar clears.")


def _status_for(conn, symbol):
    """Per-asset forward status payload (price, current signal, OOS verdict, recent paper trades)."""
    from sniper.algos.btc5m import data as D, forward as FW
    bars = D.load_bars(conn, symbol, limit=120)
    state = D.load_state(conn, symbol)
    trades = D.load_paper_trades(conn, symbol)
    verdict = FW.evaluate(trades)
    last = bars[-1] if bars else None
    spark = [round(b["close"], 2) for b in bars[-60:]]
    pos = int(state.get("position", 0))
    return {
        "symbol": symbol,
        "asset": FW.label(symbol),
        "price": (last["close"] if last else None),
        "last_bar_ms": (last["close_ms"] if last else None),
        "position": pos,
        "position_label": {1: "LONG", -1: "SHORT", 0: "FLAT"}[pos],
        "config": FW.FROZEN,
        "pass_bar": FW.PASS,
        "verdict": verdict,
        "recent_trades": trades[-20:],
        "price_spark": spark,
        "available": True,
        "disclaimer": DISCLAIMER,
    }


def create_api_btc5m_blueprint(ctx):
    bp = Blueprint("api_btc5m", __name__)
    conn = ctx["conn"]

    @bp.get("/api/btc5m/status")
    def status():
        from flask import request
        symbol = (request.args.get("symbol") or SYMBOL).upper()
        try:
            return jsonify(_json_safe(_status_for(conn, symbol)))
        except Exception as e:  # noqa: BLE001 — never crash the tab
            return jsonify({"available": False, "error": str(e), "disclaimer": DISCLAIMER})

    @bp.get("/api/btc5m/status-all")
    def status_all():
        """Every configured asset's forward status (BTC/ETH/SOL by default). The SAME frozen signal
        is forward-tested INDEPENDENTLY on each — no per-asset edge claim (the search was a null)."""
        from sniper.algos.btc5m import forward as FW
        assets = []
        for sym in FW.SYMBOLS:
            try:
                assets.append(_status_for(conn, sym))
            except Exception as e:  # noqa: BLE001 — one asset's failure must not blank the board
                assets.append({"symbol": sym, "asset": FW.label(sym), "available": False,
                               "error": str(e), "disclaimer": DISCLAIMER})
        return jsonify(_json_safe({"assets": assets, "count": len(assets), "disclaimer": DISCLAIMER}))

    @bp.get("/api/btc5m/backtest")
    def backtest():
        try:
            if not os.path.exists(BEST_CONFIG):
                return jsonify({"available": False, "disclaimer": DISCLAIMER,
                                "message": "No backtest yet — run scripts/btc5m_backtest.py."})
            with open(BEST_CONFIG) as f:
                payload = json.load(f)
            payload["available"] = True
            payload["disclaimer"] = DISCLAIMER
            return jsonify(_json_safe(payload))
        except Exception as e:  # noqa: BLE001
            return jsonify({"available": False, "error": str(e), "disclaimer": DISCLAIMER})

    return bp
