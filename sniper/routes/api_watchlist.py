import datetime as dt
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request

from sniper.routes.contexts import ApiWatchlistContext

def _updated_at() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def create_api_watchlist_blueprint(ctx: ApiWatchlistContext) -> Blueprint:
    bp = Blueprint("api_watchlist", __name__)

    @bp.get("/api/watchlist-wallets")
    def api_watchlist_wallets():
        ignored = ctx["ignored_wallets"](ctx["conn"])
        rows = []
        for raw_row in ctx["watchlist_wallets"](ctx["conn"]):
            row = dict(raw_row)
            wallet = ctx["normalize_wallet"](row.get("wallet"))
            if not wallet or wallet in ignored:
                continue
            rows.append(row)
        data: List[Dict[str, Any]] = []
        for row in rows:
            data.append(
                {
                    "wallet": row.get("wallet", ""),
                    "label": row.get("label", ""),
                    "added_ts": row.get("added_ts", 0),
                    "added_at": ctx["format_unix"](row.get("added_ts")),
                    "last_synced_ts": row.get("last_synced_ts", 0),
                    "last_synced_at": ctx["format_unix"](row.get("last_synced_ts")),
                }
            )
        return jsonify(
            {
                "count": len(data),
                "updated_at": _updated_at(),
                "items": data,
            }
        )

    @bp.post("/api/watchlist-wallets")
    def api_watchlist_wallets_add():
        payload = request.get_json(silent=True) or {}
        wallet = ctx["normalize_wallet"](payload.get("wallet"))
        label = str(payload.get("label") or "").strip()
        if not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        if wallet in ctx["ignored_wallets"](ctx["conn"]):
            return jsonify({"error": "wallet is ignored; unignore it first"}), 409
        ctx["upsert_watchlist_wallet"](ctx["conn"], wallet, label)
        ctx["sync_watchlist_wallet"](wallet, force=True)
        return jsonify({"ok": True, "wallet": wallet})

    @bp.delete("/api/watchlist-wallets/<wallet>")
    def api_watchlist_wallets_remove(wallet: str):
        wallet = ctx["normalize_wallet"](wallet)
        if not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        ctx["remove_watchlist_wallet"](ctx["conn"], wallet)
        return jsonify({"ok": True, "wallet": wallet})

    @bp.get("/api/watchlist-trades")
    def api_watchlist_trades():
        wallet = ctx["normalize_wallet"](request.args.get("wallet"))
        ignored = ctx["ignored_wallets"](ctx["conn"])
        force = str(request.args.get("force") or "").strip() in {"1", "true", "yes"}
        sync_queued = False
        if wallet:
            if not ctx["wallet_valid"](wallet):
                return jsonify({"error": "wallet must be a valid 0x address"}), 400
            if wallet in ignored:
                return jsonify(
                    {
                        "count": 0,
                        "updated_at": _updated_at(),
                        "items": [],
                        "force_requested": force,
                        "sync_queued": False,
                    }
                )
            if force:
                sync_queued = ctx["enqueue_background_sync"](
                    f"watchlist:{wallet}",
                    ctx["background_sync_watchlist"],
                    wallet,
                )
            else:
                ctx["sync_watchlist_wallet"](wallet, force=False)
            rows = ctx["recent_watchlist_trades"](
                ctx["conn"], limit=ctx["MAX_WATCHLIST_UI_TRADES"], wallet=wallet
            )
        else:
            if force:
                sync_queued = ctx["enqueue_background_sync"](
                    "watchlist:*",
                    ctx["background_sync_watchlist"],
                    None,
                )
            else:
                ctx["sync_all_watchlist_wallets"](force=False)
            rows = ctx["recent_watchlist_trades"](ctx["conn"], limit=ctx["MAX_WATCHLIST_UI_TRADES"])

        now_ts = int(dt.datetime.now(dt.UTC).timestamp())
        deleted_hashes = ctx["deleted_trade_hashes"](
            ctx["conn"],
            (str(row["transaction_hash"] or "") for row in rows),
        )
        data: List[Dict[str, Any]] = []
        for row in rows:
            if ctx["normalize_wallet"](row["wallet"]) in ignored:
                continue
            tx_hash = ctx["normalize_tx_hash"](row["transaction_hash"])
            if tx_hash and tx_hash in deleted_hashes:
                continue
            notional = float(row["notional"] or 0.0)
            if notional < ctx["MIN_UI_ORDER_NOTIONAL"]:
                continue
            data.append(
                {
                    "wallet": row["wallet"],
                    "wallet_label": row["wallet_label"],
                    "transaction_hash": row["transaction_hash"],
                    "condition_id": row["condition_id"],
                    "traded_at": ctx["format_unix"](row["timestamp"]),
                    "timestamp": row["timestamp"],
                    "age_seconds": max(0, now_ts - int(row["timestamp"] or 0)),
                    "title": row["title"],
                    "outcome": row["outcome"],
                    "bet_direction": ctx["bet_direction"](row["outcome"]),
                    "side": row["side"],
                    "event_slug": row["event_slug"],
                    "price": row["price"],
                    "size": row["size"],
                    "notional": notional,
                }
            )
        ctx["annotate_order_patterns"](data)

        return jsonify(
            {
                "count": len(data),
                "updated_at": _updated_at(),
                "force_requested": force,
                "sync_queued": sync_queued,
                "items": data,
            }
        )

    @bp.delete("/api/watchlist-trades/<wallet>/<path:tx_hash>")
    def api_watchlist_trade_remove(wallet: str, tx_hash: str):
        _ = wallet
        result = ctx["delete_trade_and_tombstone"](tx_hash, source="/api/watchlist-trades")
        if not result.get("ok"):
            return jsonify({"error": result.get("error", "delete failed")}), int(result.get("status_code", 400))
        return jsonify(result)

    return bp
