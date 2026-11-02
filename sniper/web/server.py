import datetime as dt
import gzip
import logging
import hmac
import os
import time

# Load .env/.fomo.env BEFORE any settings import (they read os.getenv at import time). Without
# this the cockpit/status API reports config defaults — e.g. armed=false while the runner is
# armed — because the web process never saw the operator's env files (same class of bug the
# runner/smoke loaders fixed: creds stranded in a file one loader ignores). Entrypoint-run ONLY
# (`python -m sniper.web.server`): loading on plain import would pollute os.environ for every
# pytest process (conftest imports this module), the exact flake class fixed in e008ac2.
def _load_runtime_env():
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    return load_env_files(str(state_root()))


if __name__ == "__main__":
    _load_runtime_env()

from concurrent.futures import ThreadPoolExecutor
from threading import Lock, local
from typing import Any, Dict, List
from flask import Flask, Response, current_app, jsonify, render_template, request
from flask_sock import Sock
from db import (
    acknowledge_alert_outbox,
    acknowledge_alert_outbox_by_dedupe_key,
    add_agent_mute,
    agent_mutes,
    alert_outbox_rows,
    auto_follow_scope_wallets,
    clear_dismissed_suspicious_trades,
    block_wallet,
    blocked_wallets_list,
    is_wallet_blocked,
    clear_ignored_wallets,
    delete_agent_mute,
    delete_trade_globally,
    deleted_trade_hashes,
    emerging_insider_wallets,
    followed_positions,
    followed_trade_wallets_for_sync,
    remove_followed_position,
    sync_auto_followed_positions_for_wallet,
    upsert_followed_position,
    wallet_profiles_map,
    watchlist_wallets,
    followed_trades,
    flagged_users,
    get_connection,
    high_winrate_wallets,
    thesis_signal_rows,
    wallet_leaderboard_rows,
    wallet_leaderboard_v2_rows,
    wallet_followable_leaderboard_rows,
    ignore_wallet,
    ignored_wallet_set,
    ignored_wallets,
    init_db,
    enqueue_alert_outbox,
    mark_watchlist_wallet_synced,
    mark_trade_deleted,
    mark_alert_outbox_sent,
    position_signals,
    recent_low_buy_trades,
    real_trades_positions_summary,
    wallet_resolved_outcome_stats,
    recent_watchlist_trades,
    remove_followed_trade,
    remove_watchlist_wallet,
    ensure_paper_trades_slippage_columns,
    speculator_flow_wallets,
    prune_expired_agent_mutes,
    thesis_candidate_rows,
    update_followed_trade_status,
    undismiss_suspicious_trade,
    unignore_wallet,
    is_trade_deleted,
    upsert_followed_trade,
    upsert_watchlist_trades,
    upsert_watchlist_wallet,
    position_snapshot_history,
    followed_wallet_activity,
    research_position_building_clusters,
)
from sniper.filters import is_excluded_market
from sniper.web.agent_pipeline import (
    collect_agent_high_signals as _collect_agent_high_signals_impl,
    emit_agent_alerts as _emit_agent_alerts_impl,
    eligibility_blockers_from_reason as _eligibility_blockers_from_reason_impl,
    open_signal_is_active_non_holding as _open_signal_is_active_non_holding_impl,
    open_signal_is_focused as _open_signal_is_focused_impl,
    serialize_agent_mute_row as _serialize_agent_mute_row_impl,
    serialize_alert_row as _serialize_alert_row_impl,
)
from sniper.markets.patterns import (
    annotate_order_patterns as _annotate_order_patterns_impl,
    fetch_wallet_order_pattern_map as _fetch_wallet_order_pattern_map_impl,
    get_suspicious_pattern_cache as _get_suspicious_pattern_cache_impl,
    get_suspicious_status_cache as _get_suspicious_status_cache_impl,
    set_suspicious_pattern_cache as _set_suspicious_pattern_cache_impl,
    set_suspicious_status_cache as _set_suspicious_status_cache_impl,
    trade_row_id as _trade_row_id_impl,
)
from sniper.markets.polymarket_api import build_session, fetch_user_positions, fetch_user_trades
from sniper.routes.api_agent import create_api_agent_blueprint
from sniper.routes.api_core import create_api_core_blueprint
from sniper.routes.api_intel_paper import create_api_intel_paper_blueprint
from sniper.routes.api_followed import create_api_followed_blueprint
from sniper.routes.api_misc import create_api_misc_blueprint
from sniper.routes.api_watchlist import create_api_watchlist_blueprint
from sniper.routes.api_strategies import create_api_strategies_blueprint
from sniper.routes.contexts import (
    build_api_agent_context,
    build_api_core_context,
    build_api_followed_context,
    build_api_misc_context,
    build_api_strategies_context,
    build_api_watchlist_context,
)
from sniper.services.stream_heartbeat import read_stream_heartbeat
from sniper.web.ops_health import classify_load, usable_cpu_count
from sniper.runtime.wallet_key_loader import startup_validate_key
from sniper.services.sync import (
    background_sync_external_leaderboard as _background_sync_external_leaderboard_impl,
    background_sync_followed_positions as _background_sync_followed_positions_impl,
    background_sync_followed_trades as _background_sync_followed_trades_impl,
    background_sync_watchlist as _background_sync_watchlist_impl,
    fetch_wallet_position_maps as _fetch_wallet_position_maps_impl,
    followed_position_key as _followed_position_key,
    is_excluded_position_record as _is_excluded_position_record,
    position_key as _position_key,
    resolve_followed_status as _resolve_followed_status_impl,
    sync_all_watchlist_wallets as _sync_all_watchlist_wallets_impl,
    sync_followed_positions as _sync_followed_positions_impl,
    sync_followed_positions_wallet as _sync_followed_positions_wallet_impl,
    sync_followed_trades as _sync_followed_trades_impl,
    sync_followed_wallet as _sync_followed_wallet_impl,
    sync_watchlist_wallet as _sync_watchlist_wallet_impl,
)
from sniper.utils import (
    _format_unix as _utils_format_unix,
    _normalize_tx_hash as _utils_normalize_tx_hash,
    _normalize_wallet as _utils_normalize_wallet,
    _optional_float as _utils_optional_float,
    _parse_any_ts as _utils_parse_any_ts,
    _safe_float as _utils_safe_float,
    _safe_int as _utils_safe_int,
    _short_wallet as _utils_short_wallet,
)
from sniper.settings.web import (
    AGENT_ALERT_DEDUPE_SECONDS,
    AGENT_FEED_DEFAULT_LIMIT,
    AGENT_FEED_MAX_LIMIT,
    AGENT_SIGNAL_MIN_INSIDER_SCORE,
    AGENT_SIGNAL_MIN_POSITION_INSIDER_SCORE,
    AGENT_SIGNAL_MIN_POSITION_VALUE,
    BACKGROUND_SYNC_WORKERS,
    EMERGING_MIN_INSIDER_SCORE,
    EMERGING_MIN_MAX_POSITION_VALUE,
    EMERGING_MIN_TOTAL_OPEN_VALUE,
    EXEC_CLUSTER_MAX_GAP_SECONDS,
    EXEC_CLUSTER_MAX_SPAN_SECONDS,
    EXEC_RAPID_MIN_TRADES,
    FLOW_MOVER_MIN_ABS_PNL,
    FLOW_MOVER_MIN_MAX_POSITION_VALUE,
    FLOW_MOVER_MIN_POSITION_SPECULATOR_SCORE,
    FLOW_MOVER_MIN_SPECULATOR_SCORE,
    FLOW_MOVER_MIN_TOTAL_OPEN_VALUE,
    FOLLOWED_MAX_SYNC_WALLETS_PER_REQUEST,
    FOLLOWED_POSITION_MAX_SYNC_WALLETS_PER_REQUEST,
    FOLLOWED_POSITION_SYNC_MIN_INTERVAL_SECONDS,
    FOLLOWED_TRADE_SYNC_MIN_INTERVAL_SECONDS,
    HARD_FILTER_TOP_POSITION,
    HIGH_WINRATE_MIN_MAX_POSITION_VALUE,
    HIGH_WINRATE_MIN_PNL,
    MAX_FOLLOWED_POSITION_UI_ROWS,
    MAX_FOLLOWED_UI_TRADES,
    MAX_UI_TRADES,
    MAX_WATCHLIST_UI_TRADES,
    MIN_ORDER_NOTIONAL,
    MIN_UI_IMPACT_RATIO,
    MIN_UI_ORDER_NOTIONAL,
    MIN_UI_TOP_POSITION_VALUE,
    OPEN_SIGNALS_FOCUSED_DEFAULT,
    OPEN_SIGNALS_FOCUS_MIN_ABS_NET_NOTIONAL_24H,
    OPEN_SIGNALS_FOCUS_MIN_INSIDER_SCORE,
    OPEN_SIGNALS_FOCUS_MIN_POSITION_VALUE,
    OPEN_SIGNALS_FOCUS_MIN_SPECULATOR_SCORE,
    POSITION_MIN_OPEN_VALUE,
    POSITION_SETTLEMENT_END_GRACE_SECONDS,
    POSITION_SETTLEMENT_PRICE_EPS,
    SNIPER_API_KEY,
    SNIPER_API_KEY_PROTECT_MUTATING,
    SNIPER_API_VERSION,
    SPECULATOR_SCORE_HIGH,
    THESIS_SIGNAL_ALLOWED_CATEGORIES,
    THESIS_SIGNAL_EXCLUDE_SPECULATOR_BOTS,
    THESIS_SIGNAL_HIGH_CONVICTION_MIN_POSITION_VALUE,
    THESIS_SIGNAL_MAX_ACCOUNT_AGE_DAYS,
    THESIS_SIGNAL_MAX_ENTRY_PRICE,
    THESIS_SIGNAL_MAX_LIFETIME_POSITIONS,
    THESIS_SIGNAL_MIN_POSITION_VALUE,
    THESIS_SIGNAL_SINGLE_MARKET_ONLY,
    THESIS_SIGNAL_WINDOW_HOURS,
    SUSPICIOUS_PATTERN_MAX_WALLET_FETCHES_PER_REQUEST,
    SUSPICIOUS_PATTERN_SYNC_MIN_INTERVAL_SECONDS,
    SUSPICIOUS_PATTERN_TRADE_FETCH_LIMIT,
    SUSPICIOUS_STATUS_MAX_WALLET_FETCHES_PER_REQUEST,
    SUSPICIOUS_STATUS_SYNC_MIN_INTERVAL_SECONDS,
    SYNC_CONFIG,
    WALLET_RE,
    WATCHLIST_MAX_SYNC_WALLETS_PER_REQUEST,
    WATCHLIST_SYNC_MIN_INTERVAL_SECONDS,
    WATCHLIST_TRADE_FETCH_LIMIT,
    PROOF_MIN_TRADES,
)
import sniper.markets.patterns as _patterns
# templates/ and static/ live at the repo root, not under sniper/web/; point Flask there
# explicitly so the app runs from any cwd (python -m, script, or systemd) without symlinks.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
app = Flask(__name__,
            template_folder=os.path.join(_REPO_ROOT, "templates"),
            static_folder=os.path.join(_REPO_ROOT, "static"))
# Serve index.html fresh from disk on every request (debug=False otherwise caches the compiled
# Jinja template, so template/HTML edits wouldn't show without a server restart). static/app.js
# already serves live from disk; this makes index.html behave the same. Negligible overhead for a
# single-machine dashboard; opt out with SNIPER_TEMPLATE_CACHE=1.
if os.getenv("SNIPER_TEMPLATE_CACHE", "0").strip() not in {"1", "true", "yes"}:
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
sock = Sock(app)

_TRUSTED_PROXY_AUTH_HEADER = "X-Sniper-Proxy-Auth"
# A SECRET shared with a local reverse proxy that injects it after its own auth (2026-09-27). It was
# a fixed public constant: with the cloudflared tunnel forwarding the internet to 127.0.0.1:5000,
# anyone who knew the tunnel URL and that string skipped Basic Auth on every route, the funded
# cockpit included. Unset or empty: the marker is never trusted (no proxy sends it today).
_TRUSTED_PROXY_AUTH_VALUE = os.getenv("SNIPER_PROXY_AUTH_SECRET", "").strip()
_bootstrap_conn = get_connection()
init_db(_bootstrap_conn)
ensure_paper_trades_slippage_columns(_bootstrap_conn)
_bootstrap_conn.close()


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


# Live redemption is opt-in and fails fast on missing/invalid key material.
_redeem_live_required = (not _env_bool("REDEEM_DRY_RUN", True)) and _env_bool("REDEEM_ARMED", False)
if _redeem_live_required:
    _key_validation = startup_validate_key(require_key=True)
    logging.getLogger(__name__).info(
        "Live redemption key loaded for signer %s from %s",
        _key_validation.get("signer", ""),
        _key_validation.get("path", ""),
    )

# ── Background end-date fetcher ───────────────────────────────
from sniper.services.fetch_end_dates import start_background_fetcher as _start_end_date_fetcher
from db import get_db_path as _get_db_path
_start_end_date_fetcher(_get_db_path())
_thread_state = local()
def _close_thread_resources() -> None:
    thread_conn = getattr(_thread_state, "conn", None)
    if thread_conn is not None:
        try:
            thread_conn.close()
        except Exception:
            pass
        _thread_state.conn = None
    thread_http = getattr(_thread_state, "http", None)
    if thread_http is not None:
        try:
            thread_http.close()
        except Exception:
            pass
        _thread_state.http = None
def _get_thread_conn():
    thread_conn = getattr(_thread_state, "conn", None)
    if thread_conn is None:
        thread_conn = get_connection()
        _thread_state.conn = thread_conn
    return thread_conn
def _get_thread_http():
    thread_http = getattr(_thread_state, "http", None)
    if thread_http is None:
        thread_http = build_session()
        _thread_state.http = thread_http
    return thread_http
class _ConnProxy:
    def __getattr__(self, name: str):
        return getattr(_get_thread_conn(), name)
class _HttpProxy:
    def __getattr__(self, name: str):
        return getattr(_get_thread_http(), name)
conn = _ConnProxy()
http = _HttpProxy()

# ops_health result cache — watchdog polls every 30s, 15s TTL means at most
# one real DB query burst per cache window.
_ops_health_cache: dict = {}
_OPS_HEALTH_CACHE_TTL = 15  # seconds
@app.teardown_appcontext
def _teardown_thread_resources(_exc=None):
    _close_thread_resources()
# Keep these aliases for test hooks/backward compatibility.
_suspicious_status_cache = _patterns._suspicious_status_cache
_suspicious_status_cache_lock = _patterns._suspicious_status_cache_lock
_suspicious_pattern_cache = _patterns._suspicious_pattern_cache
_suspicious_pattern_cache_lock = _patterns._suspicious_pattern_cache_lock
_background_executor = ThreadPoolExecutor(max_workers=max(1, BACKGROUND_SYNC_WORKERS), thread_name_prefix="sniper-bg")
_background_jobs_lock = Lock()
_background_pending_jobs: set[str] = set()
def _format_unix(ts: int | float | None) -> str:
    return _utils_format_unix(ts)
def _bet_direction(outcome: str | None) -> str:
    label = str(outcome or "").strip()
    if not label:
        return ""
    lowered = label.lower()
    if lowered in {"yes", "true"}:
        return "Bet event happens"
    if lowered in {"no", "false"}:
        return "Bet event does not happen"
    return "Bet specific outcome"
def _normalize_wallet(wallet: str | None) -> str:
    return _utils_normalize_wallet(wallet)
def _wallet_valid(wallet: str) -> bool:
    return bool(WALLET_RE.fullmatch(wallet))
def _normalize_tx_hash(tx_hash: str | None) -> str:
    return _utils_normalize_tx_hash(tx_hash)
def _safe_float(value: Any, default: float = 0.0) -> float:
    return _utils_safe_float(value, default)
def _optional_float(value: Any) -> float | None:
    return _utils_optional_float(value)
def _safe_int(value: Any, default: int = 0) -> int:
    return _utils_safe_int(value, default)
def _short_wallet(wallet: str | None) -> str:
    return _utils_short_wallet(wallet)
def _wallet_identity(wallet: str | None, profiles: Dict[str, Any]) -> Dict[str, str]:
    key = _normalize_wallet(wallet)
    profile = profiles.get(key)
    username = ""
    pseudonym = ""
    profile_image = ""
    profile_image_optimized = ""
    if profile is not None:
        username = str(profile["username"] or "").strip()
        pseudonym = str(profile["pseudonym"] or "").strip()
        profile_image = str(profile["profile_image"] or "").strip()
        profile_image_optimized = str(profile["profile_image_optimized"] or "").strip()
    display_name = username or pseudonym or _short_wallet(key)
    return {
        "username": display_name,
        "pseudonym": pseudonym,
        "profile_image": profile_image_optimized or profile_image,
    }
def _request_api_key() -> str:
    header = str(request.headers.get("X-API-Key") or "").strip()
    if header:
        return header
    auth = str(request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""
def _is_trusted_gateway_request() -> bool:
    # Nginx basic auth is the browser-facing gate. Only trust the marker when
    # the request reaches Flask over a loopback hop from the local proxy.
    if not _TRUSTED_PROXY_AUTH_VALUE:
        return False
    marker = str(request.headers.get(_TRUSTED_PROXY_AUTH_HEADER) or "").strip()
    if not hmac.compare_digest(marker.encode(), _TRUSTED_PROXY_AUTH_VALUE.encode()):
        return False
    remote_addr = str(request.remote_addr or "").strip()
    return remote_addr in {"127.0.0.1", "::1"}
def _api_key_required_for_request() -> bool:
    if not SNIPER_API_KEY:
        return False
    path = str(request.path or "")
    if path.startswith("/api/agent/"):
        return True
    if (
        SNIPER_API_KEY_PROTECT_MUTATING
        and path.startswith("/api/")
        and request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    ):
        return True
    return False
@app.before_request
def _api_key_guard():
    if not _api_key_required_for_request():
        return None
    if _is_trusted_gateway_request():
        return None
    provided = _request_api_key()
    if provided == SNIPER_API_KEY:
        return None
    return jsonify({"error": "unauthorized"}), 401


# Site-wide HTTP Basic Auth — the gate for exposing the dashboard over a PUBLIC tunnel
# (Cloudflare/ngrok). Enforced for EVERY route (pages + API, incl. the real-money cockpit) ONLY
# when both SNIPER_DASH_USER and SNIPER_DASH_PASS are set, so local/LAN/test runs are unchanged.
# Constant-time compares; browsers show a native login prompt on 401.
import hmac as _hmac

_DASH_USER = os.getenv("SNIPER_DASH_USER", "")
_DASH_PASS = os.getenv("SNIPER_DASH_PASS", "")
_DASH_SESSION_COOKIE = "sniper_dash_session"
_DASH_SESSION_TTL_S = 12 * 60 * 60


def _basic_auth_credentials_valid():
    auth = request.authorization
    return bool(
        auth and auth.type == "basic"
        and _hmac.compare_digest(auth.username or "", _DASH_USER)
        and _hmac.compare_digest(auth.password or "", _DASH_PASS)
    )


def _dash_session_token(bucket=None):
    bucket = int(time.time() // _DASH_SESSION_TTL_S) if bucket is None else int(bucket)
    payload = f"sniper-dashboard:{_DASH_USER}:{bucket}".encode()
    digest = _hmac.new(_DASH_PASS.encode(), payload, "sha256").hexdigest()
    return f"{bucket}.{digest}"


def _dash_session_valid():
    supplied = str(request.cookies.get(_DASH_SESSION_COOKIE) or "")
    bucket_raw, separator, _digest = supplied.partition(".")
    if not separator:
        return False
    try:
        bucket = int(bucket_raw)
    except ValueError:
        return False
    current = int(time.time() // _DASH_SESSION_TTL_S)
    if bucket not in (current, current - 1):
        return False
    return _hmac.compare_digest(supplied, _dash_session_token(bucket))


_DASH_LOGIN_HTML = """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sniper login</title>
<body style="background:#0b0d12;color:#dde;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0;font-family:system-ui,sans-serif">
<form method="post" action="/dash-login" style="background:#141824;padding:2rem 2.5rem;
border-radius:12px;display:flex;flex-direction:column;gap:.8rem;min-width:260px">
<h2 style="margin:0 0 .5rem">🎯 Sniper</h2>{err}
<input name="user" placeholder="user" autocomplete="username" autofocus
 autocapitalize="none" autocorrect="off" spellcheck="false"
 style="padding:.6rem;border-radius:8px;border:1px solid #333;background:#0b0d12;color:#dde">
<input name="pass" type="password" placeholder="password" autocomplete="current-password"
 style="padding:.6rem;border-radius:8px;border:1px solid #333;background:#0b0d12;color:#dde">
<button style="padding:.6rem;border-radius:8px;border:0;background:#3b82f6;color:#fff;
font-weight:600;cursor:pointer">Sign in</button></form>"""


@app.route("/dash-login", methods=["GET", "POST"])
def _dash_login():
    """Form-based login for browsers that cannot render the native basic-auth popup
    (embedded/preview browsers). Same credentials, same constant-time compare, same
    session cookie as the basic-auth path. GET serves the form; POST validates."""
    if not (_DASH_USER and _DASH_PASS) or _dash_session_valid():
        return app.redirect("/")
    if request.method == "GET":
        return _DASH_LOGIN_HTML.format(err="")
    # .strip() because a phone keyboard appends a space after an autocompleted word. Applied to
    # the user name only — a password may legitimately carry an edge space.
    u = str(request.form.get("user") or "").strip()
    p = str(request.form.get("pass") or "")
    if _hmac.compare_digest(u, _DASH_USER) and _hmac.compare_digest(p, _DASH_PASS):
        resp = app.redirect("/")
        forwarded_proto = str(request.headers.get("X-Forwarded-Proto") or "")
        secure = request.is_secure or forwarded_proto.split(",", 1)[0].strip().lower() == "https"
        resp.set_cookie(_DASH_SESSION_COOKIE, _dash_session_token(),
                        max_age=_DASH_SESSION_TTL_S, httponly=True,
                        secure=secure, samesite="Strict", path="/")
        return resp
    return _DASH_LOGIN_HTML.format(
        err='<div style="color:#f87171">wrong credentials</div>'), 401


@app.before_request
def _basic_auth_guard():
    if not (_DASH_USER and _DASH_PASS):
        return None   # gate disabled -> unchanged behavior
    if request.path == "/dash-login":
        return None
    if _dash_session_valid():
        return None
    if _is_trusted_gateway_request():
        return None
    if _basic_auth_credentials_valid():
        return None
    # Browsers get the form; curl/API keep the basic-auth 401 contract.
    accept = str(request.headers.get("Accept") or "")
    if "text/html" in accept and request.method == "GET":
        return app.redirect("/dash-login")
    return Response("Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="Sniper"'})


@app.after_request
def _remember_basic_auth(response):
    if (_DASH_USER and _DASH_PASS and _basic_auth_credentials_valid()
            and not _dash_session_valid()):
        forwarded_proto = str(request.headers.get("X-Forwarded-Proto") or "")
        secure = request.is_secure or forwarded_proto.split(",", 1)[0].strip().lower() == "https"
        response.set_cookie(
            _DASH_SESSION_COOKIE,
            _dash_session_token(),
            max_age=_DASH_SESSION_TTL_S,
            httponly=True,
            secure=secure,
            samesite="Strict",
            path="/",
        )
    return response


# Compress large JSON payloads and versioned text assets.
# The dashboard uses local authentication, so response compression does not expose a shared secret.
_GZIP_MIN_BYTES = 8192


def _set_versioned_static_cache(response):
    static_url = str(current_app.static_url_path or "/static").rstrip("/")
    prefix = static_url + "/"
    if not request.path.startswith(prefix):
        return False
    static_root = os.path.realpath(str(current_app.static_folder or ""))
    asset_path = os.path.realpath(os.path.join(static_root, request.path[len(prefix):]))
    try:
        if os.path.commonpath((static_root, asset_path)) != static_root:
            return False
        version = str(int(os.path.getmtime(asset_path)))
    except (OSError, ValueError):
        return False
    if request.args.get("v") != version:
        return False
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return True


@app.after_request
def _gzip_json(response):
    static_text = response.mimetype in {
        "application/javascript",
        "text/javascript",
        "text/css",
    }
    if static_text:
        _set_versioned_static_cache(response)
    if (response.status_code != 200
            or request.method == "HEAD"
            or request.headers.get("Range")
            or "gzip" not in str(request.headers.get("Accept-Encoding") or "").lower()
            or response.headers.get("Content-Encoding")
            or not (str(response.mimetype or "").startswith("application/json") or static_text)):
        return response
    if response.direct_passthrough:
        response.direct_passthrough = False
    body = response.get_data()
    if len(body) < _GZIP_MIN_BYTES:
        return response
    response.set_data(gzip.compress(body, 6))
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(response.get_data()))
    response.vary.add("Accept-Encoding")
    return response


def _parse_any_ts(value: Any) -> int | None:
    return _utils_parse_any_ts(value)
def _trade_row_id(row: Dict[str, Any]) -> str:
    tx = _normalize_tx_hash(row.get("transaction_hash"))
    if tx:
        return tx
    return (
        f"{_normalize_wallet(row.get('wallet'))}:"
        f"{str(row.get('condition_id') or '').strip().lower()}:"
        f"{str(row.get('outcome') or '').strip().lower()}:"
        f"{_safe_int(row.get('timestamp'))}:"
        f"{_safe_float(row.get('price')):.6f}:"
        f"{_safe_float(row.get('size')):.6f}"
    )
def _ignored_wallets(conn_obj) -> set[str]:
    return {_normalize_wallet(wallet) for wallet in ignored_wallet_set(conn_obj)}
def _delete_trade_and_tombstone(tx_hash: str, source: str = "") -> Dict[str, Any]:
    normalized = _normalize_tx_hash(tx_hash)
    if not normalized:
        return {
            "ok": False,
            "error": "transaction_hash is required",
            "status_code": 400,
        }
    mark_trade_deleted(conn, normalized, deleted_source=str(source or "").strip())
    deleted_counts = delete_trade_globally(conn, normalized)
    return {
        "ok": True,
        "transaction_hash": normalized,
        "deleted_counts": deleted_counts,
        "tombstoned": True,
    }
def _enqueue_background_sync(job_key: str, fn, *args, **kwargs) -> bool:
    normalized = str(job_key or "").strip()
    if not normalized:
        return False
    with _background_jobs_lock:
        if normalized in _background_pending_jobs:
            return False
        _background_pending_jobs.add(normalized)
    def _runner() -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            logging.exception("background sync failed job=%s", normalized)
        finally:
            _close_thread_resources()
            with _background_jobs_lock:
                _background_pending_jobs.discard(normalized)
    _background_executor.submit(_runner)
    return True
def _background_sync_watchlist(wallet: str | None = None) -> None:
    _background_sync_watchlist_impl(wallet, config=SYNC_CONFIG)
def _background_sync_followed_trades(wallet: str | None = None) -> None:
    _background_sync_followed_trades_impl(wallet, config=SYNC_CONFIG)
def _background_sync_followed_positions(wallet: str | None = None) -> None:
    _background_sync_followed_positions_impl(wallet, config=SYNC_CONFIG)
def _background_sync_external_leaderboard() -> None:
    _background_sync_external_leaderboard_impl(config=SYNC_CONFIG)
def _annotate_order_patterns(trade_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return _annotate_order_patterns_impl(
        trade_rows,
        exec_cluster_max_gap_seconds=EXEC_CLUSTER_MAX_GAP_SECONDS,
        exec_cluster_max_span_seconds=EXEC_CLUSTER_MAX_SPAN_SECONDS,
        exec_rapid_min_trades=EXEC_RAPID_MIN_TRADES,
    )
def _fetch_wallet_order_pattern_map(wallet: str, limit: int) -> Dict[str, Dict[str, Any]]:
    return _fetch_wallet_order_pattern_map_impl(
        wallet,
        http=http,
        limit=limit,
        exec_cluster_max_gap_seconds=EXEC_CLUSTER_MAX_GAP_SECONDS,
        exec_cluster_max_span_seconds=EXEC_CLUSTER_MAX_SPAN_SECONDS,
        exec_rapid_min_trades=EXEC_RAPID_MIN_TRADES,
    )
def _get_suspicious_pattern_cache(wallet: str) -> Dict[str, Any] | None:
    return _get_suspicious_pattern_cache_impl(wallet)
def _set_suspicious_pattern_cache(
    wallet: str,
    *,
    patterns_by_trade_id: Dict[str, Dict[str, Any]],
    synced_ts: int,
) -> None:
    _set_suspicious_pattern_cache_impl(
        wallet,
        patterns_by_trade_id=patterns_by_trade_id,
        synced_ts=synced_ts,
    )
def _get_suspicious_status_cache(wallet: str) -> Dict[str, Any] | None:
    return _get_suspicious_status_cache_impl(wallet)
def _set_suspicious_status_cache(
    wallet: str,
    *,
    open_by_key: Dict[tuple[str, str], Dict[str, Any]],
    closed_by_key: Dict[tuple[str, str], Dict[str, Any]],
    synced_ts: int,
) -> None:
    _set_suspicious_status_cache_impl(
        wallet,
        open_by_key=open_by_key,
        closed_by_key=closed_by_key,
        synced_ts=synced_ts,
    )
def _sync_watchlist_wallet(
    wallet: str,
    force: bool = False,
    *,
    conn_obj=None,
    http_session=None,
) -> None:
    _sync_watchlist_wallet_impl(
        wallet,
        config=SYNC_CONFIG,
        force=force,
        conn_obj=conn_obj or conn,
        http_session=http_session or http,
    )
def _sync_all_watchlist_wallets(
    force: bool = False,
    *,
    conn_obj=None,
    http_session=None,
) -> None:
    _sync_all_watchlist_wallets_impl(
        config=SYNC_CONFIG,
        force=force,
        conn_obj=conn_obj or conn,
        http_session=http_session or http,
    )
def _fetch_wallet_position_maps(
    wallet: str,
    *,
    http_session=None,
) -> tuple[Dict[tuple[str, str], Dict[str, Any]], Dict[tuple[str, str], Dict[str, Any]]]:
    return _fetch_wallet_position_maps_impl(wallet, http_session=http_session or http)
def _resolve_followed_status(
    *,
    open_position: Dict[str, Any] | None,
    closed_position: Dict[str, Any] | None,
) -> Dict[str, Any]:
    return _resolve_followed_status_impl(
        open_position=open_position,
        closed_position=closed_position,
        position_settlement_price_eps=POSITION_SETTLEMENT_PRICE_EPS,
        position_settlement_end_grace_seconds=POSITION_SETTLEMENT_END_GRACE_SECONDS,
    )
def _sync_followed_wallet(
    wallet: str,
    force: bool = False,
    *,
    conn_obj=None,
    http_session=None,
) -> None:
    _sync_followed_wallet_impl(
        wallet,
        config=SYNC_CONFIG,
        force=force,
        conn_obj=conn_obj or conn,
        http_session=http_session or http,
    )
def _sync_followed_trades(
    force: bool = False,
    *,
    conn_obj=None,
    http_session=None,
) -> None:
    _sync_followed_trades_impl(
        config=SYNC_CONFIG,
        force=force,
        conn_obj=conn_obj or conn,
        http_session=http_session or http,
    )
def _sync_followed_positions_wallet(
    wallet: str,
    *,
    force: bool = False,
    conn_obj=None,
    http_session=None,
    auto_scope_wallets: set[str] | None = None,
) -> None:
    _sync_followed_positions_wallet_impl(
        wallet,
        config=SYNC_CONFIG,
        force=force,
        conn_obj=conn_obj or conn,
        http_session=http_session or http,
        auto_scope_wallets=auto_scope_wallets,
    )
def _sync_followed_positions(
    force: bool = False,
    *,
    conn_obj=None,
    http_session=None,
) -> None:
    _sync_followed_positions_impl(
        config=SYNC_CONFIG,
        force=force,
        conn_obj=conn_obj or conn,
        http_session=http_session or http,
    )
def _open_signal_is_focused(item: Dict[str, Any]) -> bool:
    return _open_signal_is_focused_impl(
        item,
        min_insider_score=OPEN_SIGNALS_FOCUS_MIN_INSIDER_SCORE,
        min_speculator_score=OPEN_SIGNALS_FOCUS_MIN_SPECULATOR_SCORE,
        min_position_value=OPEN_SIGNALS_FOCUS_MIN_POSITION_VALUE,
        min_abs_net_notional_24h=OPEN_SIGNALS_FOCUS_MIN_ABS_NET_NOTIONAL_24H,
        safe_float=_safe_float,
        safe_int=_safe_int,
    )
def _open_signal_is_active_non_holding(item: Dict[str, Any]) -> bool:
    return _open_signal_is_active_non_holding_impl(item, safe_float=_safe_float, safe_int=_safe_int)
def _eligibility_blockers_from_reason(reason: str | None) -> List[str]:
    return _eligibility_blockers_from_reason_impl(reason)
def _collect_agent_high_signals(
    conn_obj,
    *,
    since_ts: int | None = None,
    limit: int = 200,
    include_speculators: bool = False,
) -> List[Dict[str, Any]]:
    return _collect_agent_high_signals_impl(
        conn_obj,
        since_ts=since_ts,
        limit=limit,
        include_speculators=include_speculators,
        agent_feed_max_limit=AGENT_FEED_MAX_LIMIT,
        agent_signal_min_insider_score=AGENT_SIGNAL_MIN_INSIDER_SCORE,
        agent_signal_min_position_insider_score=AGENT_SIGNAL_MIN_POSITION_INSIDER_SCORE,
        agent_signal_min_position_value=AGENT_SIGNAL_MIN_POSITION_VALUE,
        speculator_score_high=SPECULATOR_SCORE_HIGH,
        open_signals_focus_min_insider_score=OPEN_SIGNALS_FOCUS_MIN_INSIDER_SCORE,
        open_signals_focus_min_speculator_score=OPEN_SIGNALS_FOCUS_MIN_SPECULATOR_SCORE,
        open_signals_focus_min_position_value=OPEN_SIGNALS_FOCUS_MIN_POSITION_VALUE,
        open_signals_focus_min_abs_net_notional_24h=OPEN_SIGNALS_FOCUS_MIN_ABS_NET_NOTIONAL_24H,
        ignored_wallets_fn=_ignored_wallets,
        prune_expired_agent_mutes_fn=prune_expired_agent_mutes,
        agent_mutes_fn=agent_mutes,
        flagged_users_fn=flagged_users,
        emerging_insider_wallets_fn=emerging_insider_wallets,
        position_signals_fn=position_signals,
        wallet_profiles_map_fn=wallet_profiles_map,
        wallet_identity_fn=_wallet_identity,
        normalize_wallet_fn=_normalize_wallet,
        safe_float_fn=_safe_float,
        safe_int_fn=_safe_int,
        short_wallet_fn=_short_wallet,
    )
def _emit_agent_alerts(conn_obj, signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    return _emit_agent_alerts_impl(
        conn_obj,
        signals,
        enqueue_alert_outbox_fn=enqueue_alert_outbox,
        alert_dedupe_seconds=AGENT_ALERT_DEDUPE_SECONDS,
        safe_int_fn=_safe_int,
    )
def _serialize_alert_row(row: Any, profiles_by_wallet: Dict[str, Any]) -> Dict[str, Any]:
    return _serialize_alert_row_impl(
        row,
        profiles_by_wallet,
        wallet_identity_fn=_wallet_identity,
        format_unix_fn=_format_unix,
    )
def _serialize_agent_mute_row(row: Any) -> Dict[str, Any]:
    return _serialize_agent_mute_row_impl(row, format_unix_fn=_format_unix)
def _asset_versions() -> dict:
    """Cache-buster per static asset = its file mtime. Hardcoded ?v=N drifted stale (app.js
    changed 4+ times while stuck at v=176, so browsers served old JS and UI edits 'didn't take
    effect' — 2026-07-18). mtime auto-busts on every deploy; fails safe to a constant if unreadable."""
    out = {}
    static_dir = app.static_folder or os.path.join(_REPO_ROOT, "static")
    for key, rel in (("app_js", "app.js"), ("styles", "styles.css"),
                     ("mc_tokens", "css/mc-tokens.css"), ("world_map", "world-map.js"),
                     ("ticker_bar", "ticker-bar.js"), ("mc_range", "mc-range.js"),
                     ("wallet_map", "wallet_map.js"), ("wallet_universe", "wallet_universe.js"),
                     ("wallet_universe_css", "wallet_universe.css"), ("desk", "desk.js"),
                     ("office_network_js", "office-network.js"), ("office_v2_js", "office-v2.js"), ("office_communications_js", "office-communications.js"), ("office_floor_js", "office-floor.js"), ("office_floor_css", "office-floor.css")):
        try:
            out[key] = int(os.path.getmtime(os.path.join(static_dir, rel)))
        except OSError:
            out[key] = 1
    return out


@app.get("/")
def index():
    # Never cache the HTML shell — it must always reference the current ?v= asset versions,
    # otherwise a device can render a stale shell against new/old static files (blank UI).
    resp = app.make_response(render_template("index.html", asset_v=_asset_versions()))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp
@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/ops/health")
def ops_health():
    """Operational health summary for monitoring (Pablo's watchdog)."""
    import time as _time
    from pathlib import Path as _Path

    now_ts = int(_time.time())

    # Return cached result if fresh — avoids repeated full-table scans.
    _cached = _ops_health_cache.get("result")
    if _cached is not None and (now_ts - _ops_health_cache.get("ts", 0)) < _OPS_HEALTH_CACHE_TTL:
        return jsonify(_cached)
    _db_path_str = _get_db_path()
    db_path = _Path(_db_path_str)
    wal_path = _Path(_db_path_str + "-wal")

    result = {"ok": True, "ts": now_ts, "issues": []}

    # DB sizes
    result["db_mb"] = round(db_path.stat().st_size / (1024 * 1024), 1) if db_path.exists() else 0
    result["wal_mb"] = round(wal_path.stat().st_size / (1024 * 1024), 1) if wal_path.exists() else 0
    if result["wal_mb"] > 200:
        result["issues"].append(f"wal_bloat:{result['wal_mb']}MB")

    # Stream freshness
    try:
        heartbeat = read_stream_heartbeat()
        last_ws_message_ts = int(float(heartbeat.get("last_ws_message_ts") or 0)) if heartbeat else 0
        last_meta_refresh_ts = int(float(heartbeat.get("last_meta_refresh_ts") or 0)) if heartbeat else 0
        result["last_meta_refresh_ts"] = last_meta_refresh_ts or None

        row = conn.execute("SELECT MAX(timestamp) as ts FROM low_buy_trades").fetchone()
        last_low_buy_trade_ts = int(row["ts"] or 0) if row else 0
        result["last_low_buy_trade_ts"] = last_low_buy_trade_ts or None

        last_trade_ts = last_ws_message_ts or last_low_buy_trade_ts
        result["last_trade_ts"] = last_trade_ts or None
        result["trade_lag_min"] = round((now_ts - last_trade_ts) / 60, 1) if last_trade_ts else None
        result["stream_health_source"] = "heartbeat" if last_ws_message_ts else "low_buy_trades"
        if last_trade_ts and (now_ts - last_trade_ts) > 1800:
            result["issues"].append(f"streamer_stale:{result['trade_lag_min']}min")
    except Exception:
        result["last_trade_ts"] = None
        result["last_low_buy_trade_ts"] = None
        result["issues"].append("trade_ts_query_failed")

    # Paper trade summary
    try:
        # All actual terminal statuses used by paper_trader._close_position.
        # "closed" was a legacy status migrated by backfill_legacy_closed_status() — no rows exist.
        # "source_exited" / "stale" are valid states but have not triggered yet.
        # "manual" = manually-created paper trades (should count toward PnL monitoring).
        terminal = ("resolution_win", "resolution_loss", "early_exit", "source_exited", "stale", "manual")
        ph = ",".join("?" for _ in terminal)
        row = conn.execute(
            f"""SELECT COUNT(*) as total,
                       COALESCE(SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END), 0) as wins,
                       COALESCE(SUM(pnl_usdc), 0) as pnl
                FROM paper_trades WHERE status IN ({ph}) AND duplicate_of IS NULL""",
            terminal,
        ).fetchone()
        closed = row["total"] or 0
        wins = row["wins"] or 0
        result["paper"] = {
            "closed": closed,
            "wins": wins,
            "losses": closed - wins,
            "win_rate": round(wins / closed * 100, 1) if closed > 0 else 0,
            "pnl": round(float(row["pnl"] or 0), 2),
            "target": PROOF_MIN_TRADES,
            "progress_pct": round(closed / max(PROOF_MIN_TRADES, 1) * 100, 1),
        }
        open_row = conn.execute("SELECT COUNT(*) as c FROM paper_trades WHERE status='open'").fetchone()
        result["paper"]["open"] = open_row["c"] if open_row else 0
    except Exception:
        result["paper"] = None
        result["issues"].append("paper_query_failed")

    # System resources
    try:
        import shutil
        disk = shutil.disk_usage("/")
        result["disk_pct"] = round(disk.used / disk.total * 100, 1)
        if result["disk_pct"] > 80:
            result["issues"].append(f"disk_high:{result['disk_pct']}%")
    except Exception:
        pass

    try:
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    meminfo[parts[0].strip()] = int(parts[1].strip().split()[0])
            total = meminfo.get("MemTotal", 1)
            available = meminfo.get("MemAvailable", 0)
            swap_total = meminfo.get("SwapTotal", 1)
            swap_free = meminfo.get("SwapFree", 0)
            result["ram_pct"] = round((1 - available / total) * 100, 1)
            result["swap_pct"] = round((1 - swap_free / swap_total) * 100, 1) if swap_total > 0 else 0
            # High swap is only a real problem under actual memory pressure. Full swap with abundant
            # free RAM is leftover/idle allocation — flagging it alone made a healthy box read DEGRADED.
            if result["swap_pct"] > 50 and result["ram_pct"] > 80:
                result["issues"].append(f"swap_high:{result['swap_pct']}% (ram {result['ram_pct']}%)")
    except Exception:
        pass

    try:
        with open("/proc/loadavg") as _f:
            _parts = _f.read().split()
        result["load_1min"] = float(_parts[0])
        result["load_5min"] = float(_parts[1])
        result["cpu_count"] = usable_cpu_count()
        result["load_warn_threshold"], load_issue = classify_load(
            result["load_1min"], result["cpu_count"], result["load_5min"]
        )
        if load_issue:
            result["issues"].append(load_issue)
    except Exception:
        pass

    try:
        from sniper.services import brain_health_tile
        from sniper.util.state_paths import state_path
        result["brain"] = brain_health_tile.summary(state_path("data", "brain_health_latest.json"), now_ts)
    except Exception:
        result["brain"] = {"state": "error", "reason": "tile_failed"}

    if result["issues"]:
        result["ok"] = False

    _ops_health_cache["result"] = result
    _ops_health_cache["ts"] = now_ts
    return jsonify(result)
app.register_blueprint(
    create_api_agent_blueprint(
        build_api_agent_context(
            conn=conn,
            SNIPER_API_VERSION=SNIPER_API_VERSION,
            SNIPER_API_KEY=SNIPER_API_KEY,
            SNIPER_API_KEY_PROTECT_MUTATING=SNIPER_API_KEY_PROTECT_MUTATING,
            AGENT_FEED_DEFAULT_LIMIT=AGENT_FEED_DEFAULT_LIMIT,
            AGENT_FEED_MAX_LIMIT=AGENT_FEED_MAX_LIMIT,
            AGENT_ALERT_DEDUPE_SECONDS=AGENT_ALERT_DEDUPE_SECONDS,
            safe_int=_safe_int,
            normalize_wallet=_normalize_wallet,
            wallet_valid=_wallet_valid,
            collect_agent_high_signals=_collect_agent_high_signals,
            emit_agent_alerts=_emit_agent_alerts,
            alert_outbox_rows=alert_outbox_rows,
            wallet_profiles_map=wallet_profiles_map,
            serialize_alert_row=_serialize_alert_row,
            mark_alert_outbox_sent=mark_alert_outbox_sent,
            acknowledge_alert_outbox=acknowledge_alert_outbox,
            acknowledge_alert_outbox_by_dedupe_key=acknowledge_alert_outbox_by_dedupe_key,
            agent_mutes=agent_mutes,
            serialize_agent_mute_row=_serialize_agent_mute_row,
            add_agent_mute=add_agent_mute,
            delete_agent_mute=delete_agent_mute,
        )
    )
)
app.register_blueprint(
    create_api_core_blueprint(
        build_api_core_context(
            conn=conn,
            HARD_FILTER_TOP_POSITION=HARD_FILTER_TOP_POSITION,
            MIN_UI_TOP_POSITION_VALUE=MIN_UI_TOP_POSITION_VALUE,
            HIGH_WINRATE_MIN_PNL=HIGH_WINRATE_MIN_PNL,
            HIGH_WINRATE_MIN_MAX_POSITION_VALUE=HIGH_WINRATE_MIN_MAX_POSITION_VALUE,
            EMERGING_MIN_INSIDER_SCORE=EMERGING_MIN_INSIDER_SCORE,
            EMERGING_MIN_MAX_POSITION_VALUE=EMERGING_MIN_MAX_POSITION_VALUE,
            EMERGING_MIN_TOTAL_OPEN_VALUE=EMERGING_MIN_TOTAL_OPEN_VALUE,
            FLOW_MOVER_MIN_SPECULATOR_SCORE=FLOW_MOVER_MIN_SPECULATOR_SCORE,
            FLOW_MOVER_MIN_POSITION_SPECULATOR_SCORE=FLOW_MOVER_MIN_POSITION_SPECULATOR_SCORE,
            FLOW_MOVER_MIN_MAX_POSITION_VALUE=FLOW_MOVER_MIN_MAX_POSITION_VALUE,
            FLOW_MOVER_MIN_TOTAL_OPEN_VALUE=FLOW_MOVER_MIN_TOTAL_OPEN_VALUE,
            FLOW_MOVER_MIN_ABS_PNL=FLOW_MOVER_MIN_ABS_PNL,
            OPEN_SIGNALS_FOCUSED_DEFAULT=OPEN_SIGNALS_FOCUSED_DEFAULT,
            THESIS_SIGNAL_WINDOW_HOURS=THESIS_SIGNAL_WINDOW_HOURS,
            THESIS_SIGNAL_MAX_ENTRY_PRICE=THESIS_SIGNAL_MAX_ENTRY_PRICE,
            THESIS_SIGNAL_MIN_POSITION_VALUE=THESIS_SIGNAL_MIN_POSITION_VALUE,
            THESIS_SIGNAL_HIGH_CONVICTION_MIN_POSITION_VALUE=THESIS_SIGNAL_HIGH_CONVICTION_MIN_POSITION_VALUE,
            THESIS_SIGNAL_MAX_ACCOUNT_AGE_DAYS=THESIS_SIGNAL_MAX_ACCOUNT_AGE_DAYS,
            THESIS_SIGNAL_MAX_LIFETIME_POSITIONS=THESIS_SIGNAL_MAX_LIFETIME_POSITIONS,
            THESIS_SIGNAL_ALLOWED_CATEGORIES=THESIS_SIGNAL_ALLOWED_CATEGORIES,
            THESIS_SIGNAL_EXCLUDE_SPECULATOR_BOTS=THESIS_SIGNAL_EXCLUDE_SPECULATOR_BOTS,
            THESIS_SIGNAL_SINGLE_MARKET_ONLY=THESIS_SIGNAL_SINGLE_MARKET_ONLY,
            MAX_FOLLOWED_POSITION_UI_ROWS=MAX_FOLLOWED_POSITION_UI_ROWS,
            MIN_ORDER_NOTIONAL=MIN_ORDER_NOTIONAL,
            MIN_UI_ORDER_NOTIONAL=MIN_UI_ORDER_NOTIONAL,
            MIN_UI_IMPACT_RATIO=MIN_UI_IMPACT_RATIO,
            MAX_UI_TRADES=MAX_UI_TRADES,
            SUSPICIOUS_STATUS_SYNC_MIN_INTERVAL_SECONDS=SUSPICIOUS_STATUS_SYNC_MIN_INTERVAL_SECONDS,
            SUSPICIOUS_STATUS_MAX_WALLET_FETCHES_PER_REQUEST=SUSPICIOUS_STATUS_MAX_WALLET_FETCHES_PER_REQUEST,
            SUSPICIOUS_PATTERN_SYNC_MIN_INTERVAL_SECONDS=SUSPICIOUS_PATTERN_SYNC_MIN_INTERVAL_SECONDS,
            SUSPICIOUS_PATTERN_MAX_WALLET_FETCHES_PER_REQUEST=SUSPICIOUS_PATTERN_MAX_WALLET_FETCHES_PER_REQUEST,
            SUSPICIOUS_PATTERN_TRADE_FETCH_LIMIT=SUSPICIOUS_PATTERN_TRADE_FETCH_LIMIT,
            safe_int=_safe_int,
            optional_float=_optional_float,
            normalize_wallet=_normalize_wallet,
            wallet_valid=_wallet_valid,
            ignored_wallets=_ignored_wallets,
            flagged_users=flagged_users,
            high_winrate_wallets=high_winrate_wallets,
            wallet_leaderboard_rows=wallet_leaderboard_rows,
            wallet_leaderboard_v2_rows=wallet_leaderboard_v2_rows,
            wallet_followable_leaderboard_rows=wallet_followable_leaderboard_rows,
            wallet_resolved_outcome_stats=wallet_resolved_outcome_stats,
            emerging_insider_wallets=emerging_insider_wallets,
            speculator_flow_wallets=speculator_flow_wallets,
            position_signals=position_signals,
            thesis_signal_rows=thesis_signal_rows,
            thesis_candidate_rows=thesis_candidate_rows,
            followed_positions=followed_positions,
            recent_low_buy_trades=recent_low_buy_trades,
            wallet_profiles_map=wallet_profiles_map,
            wallet_identity=_wallet_identity,
            eligibility_blockers_from_reason=_eligibility_blockers_from_reason,
            format_unix=_format_unix,
            followed_position_key=_followed_position_key,
            open_signal_is_focused=_open_signal_is_focused,
            open_signal_is_active_non_holding=_open_signal_is_active_non_holding,
            get_suspicious_status_cache=_get_suspicious_status_cache,
            set_suspicious_status_cache=_set_suspicious_status_cache,
            fetch_wallet_position_maps=_fetch_wallet_position_maps,
            get_suspicious_pattern_cache=_get_suspicious_pattern_cache,
            set_suspicious_pattern_cache=_set_suspicious_pattern_cache,
            fetch_wallet_order_pattern_map=_fetch_wallet_order_pattern_map,
            resolve_followed_status=_resolve_followed_status,
            position_key=_position_key,
            bet_direction=_bet_direction,
            annotate_order_patterns=_annotate_order_patterns,
            trade_row_id=_trade_row_id,
            enqueue_background_sync=lambda job_key, fn, *args, **kwargs: _enqueue_background_sync(
                job_key, fn, *args, **kwargs
            ),
            background_sync_external_leaderboard=_background_sync_external_leaderboard,
        )
    )
)
# Intel paper book — reads SQLite only; marks are written by the */5 cron.
app.register_blueprint(create_api_intel_paper_blueprint({"conn": conn}))
app.register_blueprint(
    create_api_watchlist_blueprint(
        build_api_watchlist_context(
            conn=conn,
            MAX_WATCHLIST_UI_TRADES=MAX_WATCHLIST_UI_TRADES,
            MIN_UI_ORDER_NOTIONAL=MIN_UI_ORDER_NOTIONAL,
            normalize_wallet=_normalize_wallet,
            wallet_valid=_wallet_valid,
            ignored_wallets=_ignored_wallets,
            watchlist_wallets=watchlist_wallets,
            upsert_watchlist_wallet=upsert_watchlist_wallet,
            remove_watchlist_wallet=remove_watchlist_wallet,
            sync_watchlist_wallet=_sync_watchlist_wallet,
            sync_all_watchlist_wallets=_sync_all_watchlist_wallets,
            enqueue_background_sync=lambda job_key, fn, *args, **kwargs: _enqueue_background_sync(
                job_key, fn, *args, **kwargs
            ),
            background_sync_watchlist=_background_sync_watchlist,
            recent_watchlist_trades=recent_watchlist_trades,
            deleted_trade_hashes=deleted_trade_hashes,
            normalize_tx_hash=_normalize_tx_hash,
            format_unix=_format_unix,
            bet_direction=_bet_direction,
            annotate_order_patterns=_annotate_order_patterns,
            delete_trade_and_tombstone=_delete_trade_and_tombstone,
            position_snapshot_history=position_snapshot_history,
            followed_wallet_activity=followed_wallet_activity,
            research_position_building_clusters=research_position_building_clusters,
        )
    )
)
app.register_blueprint(
    create_api_followed_blueprint(
        build_api_followed_context(
            conn=conn,
            MAX_FOLLOWED_UI_TRADES=MAX_FOLLOWED_UI_TRADES,
            MAX_FOLLOWED_POSITION_UI_ROWS=MAX_FOLLOWED_POSITION_UI_ROWS,
            MIN_UI_ORDER_NOTIONAL=MIN_UI_ORDER_NOTIONAL,
            safe_float=_safe_float,
            safe_int=_safe_int,
            normalize_wallet=_normalize_wallet,
            normalize_tx_hash=_normalize_tx_hash,
            wallet_valid=_wallet_valid,
            ignored_wallets=_ignored_wallets,
            watchlist_wallets=watchlist_wallets,
            upsert_watchlist_wallet=upsert_watchlist_wallet,
            remove_watchlist_wallet=remove_watchlist_wallet,
            sync_watchlist_wallet=_sync_watchlist_wallet,
            followed_trades=followed_trades,
            followed_positions=followed_positions,
            real_trades_positions_summary=real_trades_positions_summary,
            wallet_profiles_map=wallet_profiles_map,
            is_trade_deleted=is_trade_deleted,
            upsert_followed_trade=upsert_followed_trade,
            remove_followed_trade=remove_followed_trade,
            upsert_followed_position=upsert_followed_position,
            remove_followed_position=remove_followed_position,
            deleted_trade_hashes=deleted_trade_hashes,
            followed_position_key=_followed_position_key,
            wallet_identity=_wallet_identity,
            format_unix=_format_unix,
            bet_direction=_bet_direction,
            sync_followed_wallet=_sync_followed_wallet,
            sync_followed_trades=_sync_followed_trades,
            sync_followed_positions_wallet=_sync_followed_positions_wallet,
            sync_followed_positions=_sync_followed_positions,
            enqueue_background_sync=lambda job_key, fn, *args, **kwargs: _enqueue_background_sync(
                job_key, fn, *args, **kwargs
            ),
            background_sync_followed_trades=_background_sync_followed_trades,
            background_sync_followed_positions=_background_sync_followed_positions,
            background_sync_external_leaderboard=_background_sync_external_leaderboard,
            delete_trade_and_tombstone=_delete_trade_and_tombstone,
        )
    )
)
app.register_blueprint(
    create_api_misc_blueprint(
        build_api_misc_context(
            conn=conn,
            normalize_wallet=_normalize_wallet,
            normalize_tx_hash=_normalize_tx_hash,
            wallet_valid=_wallet_valid,
            wallet_profiles_map=wallet_profiles_map,
            wallet_identity=_wallet_identity,
            format_unix=_format_unix,
            delete_trade_and_tombstone=_delete_trade_and_tombstone,
            undismiss_suspicious_trade=undismiss_suspicious_trade,
            clear_dismissed_suspicious_trades=clear_dismissed_suspicious_trades,
            ignored_wallets=ignored_wallets,
            ignore_wallet=ignore_wallet,
            unignore_wallet=unignore_wallet,
            clear_ignored_wallets=clear_ignored_wallets,
            block_wallet=block_wallet,
            blocked_wallets_list=blocked_wallets_list,
            is_wallet_blocked=is_wallet_blocked,
        )
    )
)
app.register_blueprint(
    create_api_strategies_blueprint(
        build_api_strategies_context(
            conn=conn,
            normalize_wallet=_normalize_wallet,
            wallet_valid=_wallet_valid,
            safe_int=_safe_int,
            optional_float=_optional_float,
        )
    )
)
from sniper.routes.api_ticker import bp as api_ticker_bp
app.register_blueprint(api_ticker_bp)
from sniper.routes.api_wallet_analyzer import create_api_wallet_analyzer_blueprint
app.register_blueprint(create_api_wallet_analyzer_blueprint({"conn": conn}))
from sniper.routes.api_live_signals import create_api_live_signals_blueprint
# Mutable ctx so the season set can be refreshed OFF the request path (see __main__). The board
# fail-opens (no season penalty) while active_leagues is absent.
_live_signals_ctx: Dict[str, Any] = {"conn": conn}
app.register_blueprint(create_api_live_signals_blueprint(_live_signals_ctx))
from sniper.routes.api_wallet_flags import create_api_wallet_flags_blueprint
app.register_blueprint(create_api_wallet_flags_blueprint({"conn": conn}))
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
# The operator's manual take-profit button (/positions/<id>/close on a REAL position) routes through
# the chain exit executor — bind it here. build_real_context returns None when disarmed / no creds, so
# real_exit_fn stays absent and a real close fails LOUD (503) instead of a modeled fake-close.
_memecoin_ctx: Dict[str, Any] = {"conn": conn}
try:
    from sniper.services.memecoin_real_runner import build_all_contexts, make_real_mark_fn
    from sniper.services.memecoin_manual_intents import make_enqueue_buy_fn
    from sniper.services.memecoin_manual_exit_intents import make_enqueue_exit_fn
    from sniper.util.clock import real_utc_now_iso
    _mc_contexts = build_all_contexts(own_sidecar=False, conn=conn)
    if _mc_contexts:
        # THE WEB NEVER HOLDS AN EXECUTOR (2026-09-05). Buys AND sells cross to the runner as
        # intents; the only funded-context function this process keeps is the read-only mark.
        _memecoin_ctx["real_buy_fn"] = make_enqueue_buy_fn(real_utc_now_iso)
        _memecoin_ctx["real_exit_intent_fn"] = make_enqueue_exit_fn(real_utc_now_iso)
        _memecoin_ctx["real_mark_fn"] = make_real_mark_fn(_mc_contexts)
except Exception:
    # degrade (no real fns -> real buy/close 503, never a fabricated fill) but LOUDLY —
    # validate_real_exit_policy() raising here means the armed exit policy is invalid
    logging.getLogger(__name__).exception(
        "memecoin real context construction failed — manual real buy/close disabled")
# A startup failure is no longer cached forever: the routes retry the build lazily (cooldown-
# bounded) before each manual action, so one bad boot moment cannot brick selling until a
# service restart (live 2026-08-14: four manual sells 503'd for exactly this).
try:
    from sniper.services.memecoin_real_runner import make_real_fns_installer
    _memecoin_ctx["ensure_real_fns"] = make_real_fns_installer(_memecoin_ctx)
except Exception:
    logging.getLogger(__name__).exception("real-fns installer unavailable")
app.register_blueprint(create_api_memecoin_blueprint(_memecoin_ctx))
from sniper.routes.api_memecoin_opportunity import create_api_memecoin_opportunity_blueprint
from sniper.research.memecoin_opportunity.store import opportunity_db_path

app.register_blueprint(create_api_memecoin_opportunity_blueprint({
    "db_path": os.getenv("MEMECOIN_OPPORTUNITY_DB") or str(opportunity_db_path()),
    "insight_db_path": os.getenv("MEMECOIN_OPPORTUNITY_INSIGHT_DB") or str(
        opportunity_db_path().with_name("memecoin_opportunity_insights.db")
    ),
    "stale_after_s": float(os.getenv("MEMECOIN_OPPORTUNITY_STALE_AFTER_S", "5")),
}))
try:
    from sniper.routes.api_memecoin_x import create_api_memecoin_x_blueprint
    app.register_blueprint(create_api_memecoin_x_blueprint({
        "db_path": os.getenv("MEMECOIN_X_DB_PATH", "data/memecoin_x.db"),
        "clock_offset_path": os.getenv("SNIPER_CLOCK_OFFSET_FILE"),
        "llm_enabled": os.getenv("MEMECOIN_X_LLM_ENABLED") == "1",
        "llm_provider": os.getenv("MEMECOIN_X_LLM_PROVIDER", ""),
        "llm_model": os.getenv("MEMECOIN_X_LLM_MODEL", ""),
        "llm_versions": {
            "prompt_version": os.getenv("MEMECOIN_X_LLM_PROMPT_VERSION", ""),
            "model_version": os.getenv("MEMECOIN_X_LLM_MODEL_VERSION", ""),
            "output_schema_version": os.getenv("MEMECOIN_X_LLM_OUTPUT_SCHEMA_VERSION", ""),
            "generation": {"temperature": 0},
        },
    }))
except Exception:
    logging.getLogger(__name__).exception("memecoin X source API registration failed")
try:
    # The Desk tab: read-only desk status (cohort caps, today's funnel, workers, reports).
    from sniper.persistence.db_core import get_db_path
    from sniper.routes.api_memecoin_desk import create_api_memecoin_desk_blueprint
    from sniper.util.state_paths import state_path
    app.register_blueprint(create_api_memecoin_desk_blueprint({
        "desk_dir": str(state_path("data", "desk")),
        "desk_db": str(state_path("data", "memecoin_desk.db")),
        "ledger_db": get_db_path(),
    }))
except Exception:
    logging.getLogger(__name__).exception("memecoin desk API registration failed")


try:
    from sniper.office.clock import corrected_now as _office_corrected_now
    from sniper.routes.api_office import create_api_office_blueprint
    from sniper.util.state_paths import state_path as _office_state_path
    app.register_blueprint(create_api_office_blueprint({
        "state_root": _office_state_path("data", "office"),
        "operator_authenticated": lambda: bool(
            _is_trusted_gateway_request() or
            (_DASH_USER and _DASH_PASS and (_dash_session_valid() or _basic_auth_credentials_valid()))),
        "clock": lambda: _office_corrected_now(
            os.getenv("SNIPER_CLOCK_OFFSET_FILE") or _office_state_path("data", "clock_offset.json"),
            host_now=time.time()),
    }))
except Exception:
    logging.getLogger(__name__).exception("Office read-only API registration failed")


def _refresh_live_signals_season(ctx: Dict[str, Any], *, interval_s: float = 6 * 3600.0) -> None:
    """Keep ctx['active_leagues'] current so the Live Wallet Signals board can apply the season gate
    WITHOUT calling active_leagues() per request (it hits the Gamma API). Runs in a daemon thread,
    started only when the server is actually launched (not on import — keeps tests network-free)."""
    import time as _time
    from sniper.analysis import market_taxonomy as _LG

    while True:
        try:
            ctx["active_leagues"] = _LG.active_leagues()
            logging.getLogger(__name__).info("live-signals season refresh: %d active leagues",
                                             len(ctx["active_leagues"]))
        except Exception:
            logging.getLogger(__name__).exception("live-signals season refresh failed")
        _time.sleep(interval_s)
from sniper.routes.api_btc5m import create_api_btc5m_blueprint
app.register_blueprint(create_api_btc5m_blueprint({"conn": conn}))
# crypto5m maker fleet ARCHIVED 2026-07-08 — retired (no realizable edge; naive-fill fantasy). Route
# unregistered so /api/crypto5m/* is gone from the live app + the "5m Binary" tab is removed. Blueprint
# code kept (import-only in tests). Revive = uncomment.
# from sniper.routes.api_crypto5m import create_api_crypto5m_blueprint
# app.register_blueprint(create_api_crypto5m_blueprint())
from sniper.routes.api_hl_microstructure import create_api_hl_microstructure_blueprint
app.register_blueprint(create_api_hl_microstructure_blueprint())
from sniper.routes.api_hyperlive import create_api_hyperlive_blueprint
app.register_blueprint(create_api_hyperlive_blueprint())
from sniper.routes.ws_stream import register_ws_stream
register_ws_stream(app, sock)

if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5000"))
    threaded = str(os.getenv("FLASK_THREADED", "true")).strip().lower() not in {"0", "false", "no"}
    
    # Security: require API key if binding to non-loopback interface
    if host not in {"127.0.0.1", "localhost"} and not SNIPER_API_KEY:
        raise RuntimeError(
            f"SNIPER_API_KEY required when binding to non-loopback host '{host}'. "
            "Set SNIPER_API_KEY environment variable or bind to 127.0.0.1/localhost only."
        )

    # Populate the live-signals season set off the request path (daemon; never blocks startup).
    # Disable for offline/dev with SNIPER_DISABLE_SEASON_REFRESH=1.
    if os.getenv("SNIPER_DISABLE_SEASON_REFRESH", "").strip().lower() not in {"1", "true", "yes"}:
        from threading import Thread
        Thread(target=_refresh_live_signals_season, args=(_live_signals_ctx,),
               name="live-signals-season", daemon=True).start()

    app.run(host=host, port=port, debug=False, threaded=threaded)
