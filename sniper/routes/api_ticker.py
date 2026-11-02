"""
REST fallback for ticker data.
The primary delivery mechanism is WebSocket (ws_stream.py injects ticker into push cycle).
This endpoint is used on initial page load and as a fallback for non-WS clients.
"""
from flask import Blueprint, jsonify

bp = Blueprint("api_ticker", __name__)


@bp.get("/api/ticker")
def api_ticker():
    """Return current market ticker snapshot."""
    try:
        from sniper.routes.ws_stream import _get_ticker_snapshot
        snapshot = _get_ticker_snapshot()
        return jsonify({"ok": True, **snapshot})
    except Exception as e:
        return jsonify({"ok": False, "symbols": {}, "error": str(e)})
