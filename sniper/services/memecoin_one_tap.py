"""One-tap entry tokens (Phase 7, 2026-07-23) — closing the alert→entry loop.

A front-run alert carries a SIGNED action token whose position parameters were frozen at alert
time (mint, network_id, source_user_id, ticker). Acting on it goes through the EXISTING
/api/memecoin/real/buy machinery — full kill-switch/risk/reservation/safety/executor chain, no
bypass. The token only removes the hand-typing, never a gate.

Security properties:
  - HMAC-SHA256 over the canonical payload with MEMECOIN_ONE_TAP_SECRET (falls back to
    SNIPER_API_KEY); constant-time compare.
  - TTL <= MEMECOIN_ONE_TAP_TTL_S (default 120s) from the CORRECTED clock stamp frozen inside
    the payload; stale tokens refuse.
  - SINGLE-USE: consuming INSERTs the token id into memecoin_action_tokens (UNIQUE) — a replay
    hits the constraint and refuses. Mint-side is stateless (the runner's TG sender needs no
    schema coupling).
  - Parameter tamper = signature mismatch (params live inside the signed payload).
  - Telegram link-preview crawlers must never execute anything: the GET page is idempotent
    (render-only); only the explicit POST consumes.

ARMING: MEMECOIN_ONE_TAP_ARMED=0 (default) resolves a valid token to a dry-run PREVIEW, never a
fill. Flipping it to 1 is Checkpoint E — an operator announcement, not a code change.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets as _secrets
from datetime import datetime, timezone


def _secret():
    s = os.getenv("MEMECOIN_ONE_TAP_SECRET") or os.getenv("SNIPER_API_KEY") or ""
    if not s:
        raise ValueError("one-tap secret unset (MEMECOIN_ONE_TAP_SECRET / SNIPER_API_KEY)")
    return s.encode()


def _sign(blob: bytes) -> str:
    return hmac.new(_secret(), blob, hashlib.sha256).hexdigest()


def mint_token(*, mint, network_id, source_user_id, ticker, now_iso):
    """Freeze the alert's entry parameters into a signed, TTL'd, single-use token string."""
    payload = {
        "token_id": _secrets.token_hex(8),
        "mint": str(mint),
        "network_id": str(network_id or ""),
        "source_user_id": str(source_user_id),
        "ticker": str(ticker or ""),
        "issued_at": str(now_iso),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(blob).decode().rstrip("=") + "." + _sign(blob)


def verify_token(token, now_iso, *, ttl_s=None):
    """-> (payload, None) or (None, reason). Checks signature then TTL; single-use is enforced
    separately at consume time (needs the DB)."""
    if ttl_s is None:
        ttl_s = float(os.getenv("MEMECOIN_ONE_TAP_TTL_S", "120"))
    try:
        body, sig = str(token).rsplit(".", 1)
        blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, TypeError):
        return None, "malformed"
    if not hmac.compare_digest(_sign(blob), sig):
        return None, "bad_signature"
    try:
        payload = json.loads(blob)
        issued = datetime.fromisoformat(payload["issued_at"].replace("Z", "+00:00"))
        now = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        return None, "malformed"
    if issued.tzinfo is None or now.tzinfo is None:
        return None, "malformed"
    age = (now.astimezone(timezone.utc) - issued.astimezone(timezone.utc)).total_seconds()
    if age < 0 or age > float(ttl_s):
        return None, "expired"
    if not payload.get("mint") or not payload.get("source_user_id"):
        return None, "malformed"
    return payload, None


def consume_token(conn, payload, now_iso):
    """Atomic single-use: first consumer wins, replay refuses. True = this call owns the token."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memecoin_action_tokens ("
        " token_id TEXT PRIMARY KEY, mint TEXT, used_at TEXT NOT NULL)")
    try:
        conn.execute("INSERT INTO memecoin_action_tokens (token_id, mint, used_at) VALUES (?,?,?)",
                     (payload["token_id"], payload.get("mint"), str(now_iso)))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False


def one_tap_enabled():
    return os.getenv("MEMECOIN_ONE_TAP_ENABLED", "0").strip().lower() in ("1", "on", "true", "yes")


def one_tap_armed():
    return os.getenv("MEMECOIN_ONE_TAP_ARMED", "0").strip().lower() in ("1", "on", "true", "yes")


def tap_url(token):
    base = (os.getenv("MEMECOIN_ONE_TAP_BASE_URL") or "").rstrip("/")
    return f"{base}/memecoin/one-tap/{token}" if base else None
