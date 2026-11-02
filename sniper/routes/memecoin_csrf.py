"""One CSRF check for every mutating memecoin web route (2026-09-27).

The web binds 0.0.0.0:5000 behind Basic Auth only, so a page the operator visits in another tab can
submit a form to it with the operator's credentials. 21 mutating /api/memecoin routes checked no
token, including arm/disarm, the kill switch and manual close. `guard_mutations` puts the check on
the whole blueprint instead: every POST/PUT/PATCH/DELETE needs a valid X-CSRF-Token, except
endpoints named in `exempt` (each with its reason at the call site).

MEMECOIN_WEB_CSRF_ENFORCE_ALL=0 (from .env only) drops the blanket guard with a loud warning, so a
cockpit that refuses its own close or kill can be undone with a web restart instead of a deploy.
Routes that call csrf_ok() themselves keep their own check either way.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from flask import current_app, jsonify, request

logger = logging.getLogger(__name__)

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# app.secret_key is not set, so this per-process key makes the token rotate on every web restart;
# the client refreshes it from /api/memecoin/csrf-token on a 403 and retries once.
_FALLBACK_KEY = secrets.token_bytes(32)


def csrf_token():
    secret = current_app.secret_key
    key = str(secret).encode() if secret else _FALLBACK_KEY
    return hmac.new(key, b"memecoin-money-v1", hashlib.sha256).hexdigest()


def csrf_ok():
    supplied = str(request.headers.get("X-CSRF-Token") or "")
    if not supplied or not hmac.compare_digest(supplied, csrf_token()):
        return False
    if request.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
        return False
    origin = request.headers.get("Origin")
    expected_origin = request.host_url.rstrip("/")
    if str(request.remote_addr or "") in {"127.0.0.1", "::1"}:
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0]
        if forwarded_proto in {"http", "https"}:
            expected_origin = f"{forwarded_proto}://{request.host}"
    return origin is None or origin.rstrip("/") == expected_origin


def guard_mutations(bp, *, exempt=frozenset(), enforce=None):
    """Refuse every mutating request on `bp` without a valid token, before its handler runs."""
    if enforce is None:
        import sniper.settings.web as web_settings
        enforce = web_settings.MEMECOIN_WEB_CSRF_ENFORCE_ALL
    if not enforce:
        logger.warning("MEMECOIN_WEB_CSRF_ENFORCE_ALL=0: mutating routes on blueprint %s accept "
                       "requests WITHOUT a CSRF token", bp.name)
        return

    @bp.before_request
    def _require_csrf():
        if request.method in MUTATING and request.endpoint not in exempt and not csrf_ok():
            return jsonify({"ok": False, "error": "csrf rejected"}), 403
        return None
