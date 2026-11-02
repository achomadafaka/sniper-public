import datetime as dt
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request

from sniper.routes.contexts import ApiAgentContext


def _updated_at() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def create_api_agent_blueprint(ctx: ApiAgentContext) -> Blueprint:
    bp = Blueprint("api_agent", __name__)

    @bp.get("/api/agent/capabilities")
    def api_agent_capabilities():
        return jsonify(
            {
                "ok": True,
                "api_version": ctx["SNIPER_API_VERSION"],
                "auth_required": bool(ctx["SNIPER_API_KEY"]),
                "auth_agent_required": bool(ctx["SNIPER_API_KEY"]),
                "auth_mutating_required": bool(
                    ctx["SNIPER_API_KEY"] and ctx["SNIPER_API_KEY_PROTECT_MUTATING"]
                ),
                "limits": {
                    "agent_feed_default_limit": ctx["AGENT_FEED_DEFAULT_LIMIT"],
                    "agent_feed_max_limit": ctx["AGENT_FEED_MAX_LIMIT"],
                    "agent_alert_dedupe_seconds": ctx["AGENT_ALERT_DEDUPE_SECONDS"],
                },
                "lanes": [
                    "wallet_insider_candidate",
                    "wallet_emerging_insider",
                    "position_insider_candidate",
                    "position_watch",
                    "position_speculator_ladder",
                ],
                "endpoints": {
                    "high_signal": "/api/agent/high-signal",
                    "alerts_generate": "/api/agent/alerts/generate",
                    "alerts_list": "/api/agent/alerts",
                    "alerts_mark_sent": "/api/agent/alerts/<id>/sent",
                    "alerts_ack": "/api/agent/alerts/<id>/ack",
                    "alerts_ack_by_key": "/api/agent/alerts/ack-by-key",
                    "alerts_ack_bulk": "/api/agent/alerts/ack-bulk",
                    "mutes_list": "/api/agent/mutes",
                    "mutes_add": "/api/agent/mutes",
                    "mutes_delete": "/api/agent/mutes/<id>",
                },
            }
        )

    @bp.get("/api/agent/high-signal")
    def api_agent_high_signal():
        safe_int = ctx["safe_int"]
        limit = safe_int(request.args.get("limit"), ctx["AGENT_FEED_DEFAULT_LIMIT"])
        if limit <= 0:
            limit = ctx["AGENT_FEED_DEFAULT_LIMIT"]
        limit = min(limit, ctx["AGENT_FEED_MAX_LIMIT"])
        since_ts = safe_int(request.args.get("since_ts"), 0)
        include_speculators = str(request.args.get("include_speculators") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        enqueue_alerts = str(request.args.get("enqueue_alerts") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }

        signals = ctx["collect_agent_high_signals"](
            ctx["conn"],
            since_ts=since_ts if since_ts > 0 else None,
            limit=limit,
            include_speculators=include_speculators,
        )
        alert_result: Dict[str, Any] | None = None
        if enqueue_alerts:
            alert_result = ctx["emit_agent_alerts"](ctx["conn"], signals)

        response: Dict[str, Any] = {
            "count": len(signals),
            "updated_at": _updated_at(),
            "since_ts": since_ts if since_ts > 0 else 0,
            "include_speculators": include_speculators,
            "items": signals,
        }
        if alert_result is not None:
            response["alerts"] = alert_result
        return jsonify(response)

    @bp.post("/api/agent/alerts/generate")
    def api_agent_alerts_generate():
        payload = request.get_json(silent=True) or {}
        safe_int = ctx["safe_int"]
        limit = safe_int(payload.get("limit"), ctx["AGENT_FEED_DEFAULT_LIMIT"])
        if limit <= 0:
            limit = ctx["AGENT_FEED_DEFAULT_LIMIT"]
        limit = min(limit, ctx["AGENT_FEED_MAX_LIMIT"])
        since_ts = safe_int(payload.get("since_ts"), 0)
        include_speculators = bool(payload.get("include_speculators"))

        signals = ctx["collect_agent_high_signals"](
            ctx["conn"],
            since_ts=since_ts if since_ts > 0 else None,
            limit=limit,
            include_speculators=include_speculators,
        )
        alerts = ctx["emit_agent_alerts"](ctx["conn"], signals)
        return jsonify(
            {
                "ok": True,
                "count": len(signals),
                "updated_at": _updated_at(),
                "alerts": alerts,
                "items": signals,
            }
        )

    @bp.get("/api/agent/alerts")
    def api_agent_alerts():
        safe_int = ctx["safe_int"]
        normalize_wallet = ctx["normalize_wallet"]
        wallet_valid = ctx["wallet_valid"]

        status_param = str(request.args.get("status") or "new,sent").strip().lower()
        statuses = [token.strip() for token in status_param.split(",") if token.strip()]
        since_ts = safe_int(request.args.get("since_ts"), 0)
        wallet = normalize_wallet(request.args.get("wallet"))
        if wallet and not wallet_valid(wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        limit = safe_int(request.args.get("limit"), ctx["AGENT_FEED_DEFAULT_LIMIT"])
        if limit <= 0:
            limit = ctx["AGENT_FEED_DEFAULT_LIMIT"]
        limit = min(limit, ctx["AGENT_FEED_MAX_LIMIT"])

        rows = ctx["alert_outbox_rows"](
            ctx["conn"],
            statuses=statuses if statuses else None,
            since_ts=since_ts if since_ts > 0 else None,
            wallet=wallet or None,
            limit=limit,
        )
        profiles = ctx["wallet_profiles_map"](
            ctx["conn"], [str(row["wallet"] or "") for row in rows if row["wallet"]]
        )
        data = [ctx["serialize_alert_row"](row, profiles) for row in rows]
        return jsonify(
            {
                "count": len(data),
                "updated_at": _updated_at(),
                "items": data,
            }
        )

    @bp.post("/api/agent/alerts/<int:alert_id>/sent")
    def api_agent_alert_mark_sent(alert_id: int):
        ok = ctx["mark_alert_outbox_sent"](ctx["conn"], alert_id=alert_id)
        if not ok:
            return jsonify({"error": "alert not found"}), 404
        return jsonify({"ok": True, "alert_id": int(alert_id), "status": "sent"})

    @bp.post("/api/agent/alerts/<int:alert_id>/ack")
    def api_agent_alert_ack(alert_id: int):
        ok = ctx["acknowledge_alert_outbox"](ctx["conn"], alert_id=alert_id)
        if not ok:
            return jsonify({"error": "alert not found"}), 404
        return jsonify({"ok": True, "alert_id": int(alert_id), "status": "acknowledged"})

    @bp.post("/api/agent/alerts/ack-by-key")
    def api_agent_alert_ack_by_key():
        payload = request.get_json(silent=True) or {}
        dedupe_key = str(payload.get("dedupe_key") or "").strip().lower()
        if not dedupe_key:
            return jsonify({"error": "dedupe_key is required"}), 400
        ok = ctx["acknowledge_alert_outbox_by_dedupe_key"](ctx["conn"], dedupe_key=dedupe_key)
        if not ok:
            return jsonify({"error": "alert not found"}), 404
        return jsonify({"ok": True, "dedupe_key": dedupe_key, "status": "acknowledged"})

    @bp.post("/api/agent/alerts/ack-bulk")
    def api_agent_alert_ack_bulk():
        payload = request.get_json(silent=True) or {}
        safe_int = ctx["safe_int"]
        ids_raw = payload.get("ids") or []
        keys_raw = payload.get("dedupe_keys") or []

        alert_ids: List[int] = []
        for value in ids_raw if isinstance(ids_raw, list) else []:
            alert_id = safe_int(value, 0)
            if alert_id > 0:
                alert_ids.append(alert_id)

        dedupe_keys: List[str] = []
        for value in keys_raw if isinstance(keys_raw, list) else []:
            key = str(value or "").strip().lower()
            if key:
                dedupe_keys.append(key)

        acked_ids: List[int] = []
        missing_ids: List[int] = []
        acked_keys: List[str] = []
        missing_keys: List[str] = []

        for alert_id in alert_ids:
            if ctx["acknowledge_alert_outbox"](ctx["conn"], alert_id=alert_id):
                acked_ids.append(alert_id)
            else:
                missing_ids.append(alert_id)

        for dedupe_key in dedupe_keys:
            if ctx["acknowledge_alert_outbox_by_dedupe_key"](ctx["conn"], dedupe_key=dedupe_key):
                acked_keys.append(dedupe_key)
            else:
                missing_keys.append(dedupe_key)

        return jsonify(
            {
                "ok": True,
                "acked_ids": acked_ids,
                "missing_ids": missing_ids,
                "acked_dedupe_keys": acked_keys,
                "missing_dedupe_keys": missing_keys,
                "ack_count": len(acked_ids) + len(acked_keys),
            }
        )

    @bp.get("/api/agent/mutes")
    def api_agent_mutes():
        safe_int = ctx["safe_int"]
        include_expired = str(request.args.get("include_expired") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        limit = safe_int(request.args.get("limit"), 1000)
        if limit <= 0:
            limit = 1000
        limit = min(limit, 5000)
        rows = ctx["agent_mutes"](ctx["conn"], include_expired=include_expired, limit=limit)
        data = [ctx["serialize_agent_mute_row"](row) for row in rows]
        return jsonify(
            {
                "count": len(data),
                "updated_at": _updated_at(),
                "items": data,
            }
        )

    @bp.post("/api/agent/mutes")
    def api_agent_mutes_add():
        payload = request.get_json(silent=True) or {}
        safe_int = ctx["safe_int"]
        normalize_wallet = ctx["normalize_wallet"]

        scope_type = str(payload.get("scope_type") or "").strip().lower()
        scope_value = str(payload.get("scope_value") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        ttl_seconds = safe_int(payload.get("ttl_seconds"), 0)
        wallet = normalize_wallet(payload.get("wallet"))
        dedupe_key = str(payload.get("dedupe_key") or "").strip().lower()
        condition_id = str(payload.get("condition_id") or "").strip().lower()
        outcome = str(payload.get("outcome") or "").strip().lower()

        result = ctx["add_agent_mute"](
            ctx["conn"],
            scope_type=scope_type,
            scope_value=scope_value or None,
            wallet=wallet or None,
            dedupe_key=dedupe_key or None,
            condition_id=condition_id or None,
            outcome=outcome or None,
            ttl_seconds=ttl_seconds,
            reason=reason,
        )
        if not bool(result.get("ok")):
            return jsonify({"error": str(result.get("error") or "unable to create mute")}), 400

        row = result.get("row")
        if row is None:
            return jsonify({"error": "mute row not found after insert"}), 500
        return jsonify({"ok": True, "item": ctx["serialize_agent_mute_row"](row)})

    @bp.delete("/api/agent/mutes/<int:mute_id>")
    def api_agent_mutes_delete(mute_id: int):
        ok = ctx["delete_agent_mute"](ctx["conn"], mute_id)
        if not ok:
            return jsonify({"error": "mute not found"}), 404
        return jsonify({"ok": True, "id": int(mute_id)})

    return bp
