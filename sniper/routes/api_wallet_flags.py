"""Favorite / delete wallet endpoints (operator actions on analyzed wallets).

- favorite: pin to the top of the surfaces + re-engage (clears a prior soft-delete) + enqueue a
  boosted re-analysis. It does not create paper positions or mutate strategy state.
- delete: reversible SOFT delete — hides the wallet from surfaces and blacklists it from intake.
  Soft (not hard) so a misclick isn't catastrophic and we never destroy on-chain-derived analysis.
Blueprint takes a `conn` ctx, like the other read/CRUD blueprints."""
from __future__ import annotations

import re

from flask import Blueprint, jsonify, request

from sniper.persistence import db_wallet_flags as F

_WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_FAVORITE_PRIORITY = 10  # lower = sooner; boosts above default discovery enqueue (100)


def _ok(w: str) -> bool:
    return bool(_WALLET_RE.match((w or "").strip()))


def create_api_wallet_flags_blueprint(ctx) -> Blueprint:
    bp = Blueprint("api_wallet_flags", __name__)
    conn = ctx["conn"]

    @bp.post("/api/wallet-analyzer/favorite")
    def favorite():
        wallet = ((request.get_json(silent=True) or {}).get("wallet") or "").strip()
        if not _ok(wallet):
            return jsonify({"error": "invalid wallet — expected 0x + 40 hex"}), 400
        F.restore(conn, wallet)        # re-engaging a wallet un-deletes it
        F.set_favorite(conn, wallet)
        extra = {}
        try:                            # boosted re-analyze; idempotent if already queued
            from sniper.persistence import db_wallet_analysis_queue as Q
            Q.ensure_queue_tables(conn)
            Q.enqueue(conn, wallet.lower(), "favorite", reason="favorited", priority=_FAVORITE_PRIORITY)
        except Exception as e:  # noqa: BLE001 — favoriting must not fail if the queue is unavailable
            extra["enqueue_error"] = str(e)
        return jsonify({"ok": True, "favorite": True, **extra})

    @bp.delete("/api/wallet-analyzer/favorite/<wallet>")
    def unfavorite(wallet):
        if not _ok(wallet):
            return jsonify({"error": "invalid wallet"}), 400
        F.clear_favorite(conn, wallet)
        return jsonify({"ok": True, "favorite": False})

    @bp.delete("/api/wallet-analyzer/wallet/<wallet>")
    def delete_wallet(wallet):
        if not _ok(wallet):
            return jsonify({"error": "invalid wallet"}), 400
        F.soft_delete(conn, wallet)
        return jsonify({"ok": True, "deleted": True})

    @bp.post("/api/wallet-analyzer/restore/<wallet>")
    def restore_wallet(wallet):
        if not _ok(wallet):
            return jsonify({"error": "invalid wallet"}), 400
        F.restore(conn, wallet)        # un-delete (reverses a soft-delete)
        return jsonify({"ok": True, "deleted": False})

    return bp
