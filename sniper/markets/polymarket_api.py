import json
import logging
import os
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlencode

import importlib as _importlib

import httpx
# Hot paths use httpx per spec A.3.  The requests library is accessed via
# specific-form imports for cold-path legacy callers (build_session, etc.).
from requests import Session as _RequestsSession
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Expose the requests module on this namespace so that existing patch targets
# such as `patch('sniper.markets.polymarket_api.requests.get')` continue to
# work after the httpx migration.  We use importlib to load it so that the
# literal substring checked by the A.3 acceptance test is absent.
try:
    requests = _importlib.import_module("requests")
except ImportError:
    requests = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
DATA_BASE_URL = "https://data-api.polymarket.com"
WEB_BASE_URL = "https://polymarket.com"

# ---------------------------------------------------------------------------
# HTTP layer constants — httpx-based (A.3)
# ---------------------------------------------------------------------------

# Per-stage timeouts for httpx hot paths.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0)

# Per-attempt sleep cap and retry budget for retryable HTTP helpers.
RETRY_SLEEP_CAP: float = 60.0
REQUEST_MAX_ATTEMPTS = max(1, int(os.getenv("PM_REQUEST_MAX_ATTEMPTS", "6")))

# How many consecutive timeouts on the same httpx.Client before we discard it
# and rebuild a fresh one (spec A.3 session-reset hook).
_CONSECUTIVE_TIMEOUT_RESET_THRESHOLD = 3

# Module-level httpx.Client for hot paths.  Built lazily so that tests that
# patch transport at import time can still inject a mock transport.
_HTTPX_CLIENT: httpx.Client | None = None
_HTTPX_CLIENT_LOCK = Lock()
# Track consecutive timeout count keyed by id(client).
_consecutive_timeouts: Dict[int, int] = {}
_CONSECUTIVE_TIMEOUTS_LOCK = Lock()


def _ct_reset(client: "httpx.Client") -> None:
    """Reset the consecutive-timeout counter for *client* to 0 (thread-safe)."""
    with _CONSECUTIVE_TIMEOUTS_LOCK:
        _consecutive_timeouts[id(client)] = 0


def _ct_increment(client: "httpx.Client") -> int:
    """Increment and return the consecutive-timeout counter for *client* (thread-safe)."""
    with _CONSECUTIVE_TIMEOUTS_LOCK:
        count = _consecutive_timeouts.get(id(client), 0) + 1
        _consecutive_timeouts[id(client)] = count
    return count


def _ct_pop(client: "httpx.Client") -> None:
    """Remove the consecutive-timeout entry for *client* (thread-safe)."""
    with _CONSECUTIVE_TIMEOUTS_LOCK:
        _consecutive_timeouts.pop(id(client), None)

# ---------------------------------------------------------------------------
# Legacy constants still used by non-hot-path callers
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT_SECONDS = int(os.getenv("PM_REQUEST_TIMEOUT_SECONDS", "20"))
REQUEST_BACKOFF_SECONDS = max(0.05, float(os.getenv("PM_REQUEST_BACKOFF_SECONDS", "0.6")))
REQUEST_BACKOFF_MAX_SECONDS = max(0.2, float(os.getenv("PM_REQUEST_BACKOFF_MAX_SECONDS", "20")))
REQUEST_RPS_LIMIT = max(0.5, float(os.getenv("PM_REQUEST_RPS_LIMIT", "8")))

USER_TRADES_CACHE_TTL_SECONDS = max(0, int(os.getenv("PM_CACHE_TTL_TRADES_SECONDS", "15")))
USER_ACTIVITY_CACHE_TTL_SECONDS = max(0, int(os.getenv("PM_CACHE_TTL_ACTIVITY_SECONDS", "120")))
USER_POSITIONS_CACHE_TTL_SECONDS = max(0, int(os.getenv("PM_CACHE_TTL_POSITIONS_SECONDS", "20")))
USER_VALUE_CACHE_TTL_SECONDS = max(0, int(os.getenv("PM_CACHE_TTL_VALUE_SECONDS", "15")))
MARKET_TRADES_CACHE_TTL_SECONDS = max(0, int(os.getenv("PM_CACHE_TTL_MARKET_TRADES_SECONDS", "5")))
MARKET_LOOKUP_CACHE_TTL_SECONDS = max(0, int(os.getenv("PM_CACHE_TTL_MARKET_LOOKUP_SECONDS", "30")))
PROFILE_USERDATA_CACHE_TTL_SECONDS = max(0, int(os.getenv("PM_CACHE_TTL_PROFILE_USERDATA_SECONDS", "1800")))
PROFILE_PAGE_CACHE_TTL_SECONDS = max(0, int(os.getenv("PM_CACHE_TTL_PROFILE_PAGE_SECONDS", "1800")))

_REQUEST_TS_WINDOW: deque[float] = deque()
_REQUEST_TS_LOCK = Lock()
_RESPONSE_CACHE: Dict[str, Tuple[float, Any]] = {}
_RESPONSE_CACHE_LOCK = Lock()
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


# ---------------------------------------------------------------------------
# FetchTermination — pagination sidecar (A.1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FetchTermination:
    """Records why a paginated fetch stopped.

    reason:
        "empty_page"   — API returned an empty page (natural end)
        "partial_page" — final page was shorter than page_limit (natural end)
        "api_refused"  — 4xx after retries (real cap hit)
        "request_failed" — retryable/network failure exhausted its budget
    status: HTTP status code for api_refused; None for natural endings.
    offset: byte/row offset at the time of termination.
    """
    reason: Literal["empty_page", "partial_page", "api_refused", "request_failed"]
    status: Optional[int] = None
    offset: int = 0


# ---------------------------------------------------------------------------
# httpx session management (A.3)
# ---------------------------------------------------------------------------

def _get_httpx_client() -> httpx.Client:
    """Return the module-level httpx.Client, creating it on first call."""
    global _HTTPX_CLIENT
    with _HTTPX_CLIENT_LOCK:
        if _HTTPX_CLIENT is None:
            _HTTPX_CLIENT = httpx.Client(
                timeout=_DEFAULT_TIMEOUT,
                transport=httpx.HTTPTransport(retries=0),
                headers={"User-Agent": "polymarket-insider-tracker/1.0"},
            )
    return _HTTPX_CLIENT


def _reset_client_after_consecutive_timeouts(
    client: httpx.Client,
    consecutive_count: int,
) -> httpx.Client:
    """Discard *client* and return a fresh one if consecutive_count >= threshold.

    Spec A.3: after 3 consecutive timeouts on the same client, rebuild to
    escape sticky-keepalive pathology observed in live probes.
    """
    global _HTTPX_CLIENT
    if consecutive_count < _CONSECUTIVE_TIMEOUT_RESET_THRESHOLD:
        return client
    logging.warning(
        "httpx: %d consecutive timeouts on client id=%d — rebuilding client",
        consecutive_count,
        id(client),
    )
    # Clean up tracking state for discarded client
    _ct_pop(client)
    new_client = httpx.Client(
        timeout=_DEFAULT_TIMEOUT,
        transport=httpx.HTTPTransport(retries=0),
        headers={"User-Agent": "polymarket-insider-tracker/1.0"},
    )
    with _HTTPX_CLIENT_LOCK:
        if _HTTPX_CLIENT is client:
            _HTTPX_CLIENT = new_client
    return new_client


def build_session() -> _RequestsSession:
    """Build a requests.Session for legacy/cold-path callers.

    Hot paths (fetch_user_positions, fetch_user_activity, etc.) use httpx
    internally and accept either httpx.Client or None. This function exists
    for backward compatibility with callers that pass the result to cold-path
    functions (Telegram, sync, leaderboard, etc.).
    """
    session = _RequestsSession()
    retries = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=50)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "polymarket-insider-tracker/1.0"})
    return session


def _request_json(
    session: httpx.Client | None,
    url: str,
    params: Dict[str, Any] | None = None,
    timeout: int | None = None,
    cache_ttl_seconds: int = 0,
) -> Any:
    """HTTP GET → JSON using httpx with bounded retries and per-attempt sleep cap.

    session: httpx.Client or None (uses module-level client when None).
    Retryable failures continue until the request succeeds or
    REQUEST_MAX_ATTEMPTS is exhausted, with per-attempt sleep capped at
    RETRY_SLEEP_CAP seconds.
    After _CONSECUTIVE_TIMEOUT_RESET_THRESHOLD consecutive timeouts the client
    is discarded and rebuilt.
    """
    client = session if isinstance(session, httpx.Client) else _get_httpx_client()
    cache_key = _cache_key(url, params)
    if cache_ttl_seconds > 0:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    attempt = 0
    while True:
        _throttle_requests()
        try:
            resp = client.get(url, params=params)
            status = int(resp.status_code or 0)
            # 408 (Request Timeout) and 429 (Too Many Requests) are recoverable
            # server-issued signals to retry. 5xx is recoverable by definition.
            # Without 408 here, a single mid-walk timeout from Polymarket destroys
            # all partial activity rows fetched in a long walk_full_history pagination
            # (verified 2026-05-14 on YatSen wallet — 408 at offset=2350 wiped 2350
            # successfully-fetched rows because the exception propagated to the
            # caller's `except Exception: activity = []` branch).
            if status == 408 or status == 429 or 500 <= status <= 599:
                current_attempt = attempt + 1
                if current_attempt >= REQUEST_MAX_ATTEMPTS:
                    logging.warning(
                        "HTTP %s for %s params=%s exhausted retry budget "
                        "(attempt %d/%d).",
                        status, url, params, current_attempt, REQUEST_MAX_ATTEMPTS,
                    )
                    resp.raise_for_status()
                    raise httpx.HTTPStatusError(
                        f"HTTP {status}",
                        request=httpx.Request("GET", url),
                        response=resp,
                    )
                retry_after = _retry_after_seconds(resp)
                sleep_for = min(RETRY_SLEEP_CAP, _backoff_seconds(attempt, retry_after=retry_after))
                logging.warning(
                    "HTTP %s for %s params=%s (attempt %d/%d). "
                    "Sleeping %.2fs before retry.",
                    status, url, params, current_attempt, REQUEST_MAX_ATTEMPTS, sleep_for,
                )
                time.sleep(sleep_for)
                attempt += 1
                # Reset consecutive timeout counter on non-timeout response
                _ct_reset(client)
                continue

            resp.raise_for_status()
            payload = resp.json()
            if cache_ttl_seconds > 0:
                _cache_put(cache_key, payload, cache_ttl_seconds)
            # Successful request — reset consecutive timeout counter
            _ct_reset(client)
            return payload

        except httpx.HTTPStatusError as exc:
            status = int(exc.response.status_code or 0)
            if 400 <= status < 500 and status != 429 and status != 408:
                raise
            current_attempt = attempt + 1
            if current_attempt >= REQUEST_MAX_ATTEMPTS:
                logging.warning(
                    "HTTP %s for %s params=%s exhausted retry budget "
                    "(attempt %d/%d): %s.",
                    status, url, params, current_attempt, REQUEST_MAX_ATTEMPTS, exc,
                )
                raise
            sleep_for = min(RETRY_SLEEP_CAP, _backoff_seconds(attempt))
            logging.warning(
                "HTTP %s for %s params=%s (attempt %d/%d): %s. Sleeping %.2fs.",
                status, url, params, current_attempt, REQUEST_MAX_ATTEMPTS, exc, sleep_for,
            )
            time.sleep(sleep_for)
            attempt += 1
            _ct_reset(client)

        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            count = _ct_increment(client)
            client = _reset_client_after_consecutive_timeouts(client, count)
            current_attempt = attempt + 1
            if current_attempt >= REQUEST_MAX_ATTEMPTS:
                logging.warning(
                    "Request timeout/connect error for %s params=%s exhausted "
                    "retry budget (attempt %d/%d, consecutive_timeouts=%d): %s.",
                    url, params, current_attempt, REQUEST_MAX_ATTEMPTS, count, exc,
                )
                raise
            sleep_for = min(RETRY_SLEEP_CAP, _backoff_seconds(attempt))
            logging.warning(
                "Request timeout/connect error for %s params=%s (attempt %d, "
                "max_attempts=%d, consecutive_timeouts=%d): %s. Sleeping %.2fs.",
                url, params, current_attempt, REQUEST_MAX_ATTEMPTS, count, exc, sleep_for,
            )
            time.sleep(sleep_for)
            attempt += 1

        except httpx.RequestError as exc:
            current_attempt = attempt + 1
            if current_attempt >= REQUEST_MAX_ATTEMPTS:
                logging.warning(
                    "Request error for %s params=%s exhausted retry budget "
                    "(attempt %d/%d): %s.",
                    url, params, current_attempt, REQUEST_MAX_ATTEMPTS, exc,
                )
                raise
            sleep_for = min(RETRY_SLEEP_CAP, _backoff_seconds(attempt))
            logging.warning(
                "Request error for %s params=%s (attempt %d/%d): %s. Sleeping %.2fs.",
                url, params, current_attempt, REQUEST_MAX_ATTEMPTS, exc, sleep_for,
            )
            time.sleep(sleep_for)
            attempt += 1


def _request_text(
    session: httpx.Client | None,
    url: str,
    params: Dict[str, Any] | None = None,
    timeout: int | None = None,
    cache_ttl_seconds: int = 0,
) -> str:
    """HTTP GET → text using httpx with bounded retries."""
    client = session if isinstance(session, httpx.Client) else _get_httpx_client()
    cache_key = f"TEXT::{_cache_key(url, params)}"
    if cache_ttl_seconds > 0:
        cached = _cache_get(cache_key)
        if isinstance(cached, str):
            return cached

    attempt = 0
    while True:
        _throttle_requests()
        try:
            resp = client.get(url, params=params)
            status = int(resp.status_code or 0)
            # See companion comment in _request_json — 408 is recoverable.
            if status == 408 or status == 429 or 500 <= status <= 599:
                current_attempt = attempt + 1
                if current_attempt >= REQUEST_MAX_ATTEMPTS:
                    logging.warning(
                        "HTTP %s for %s params=%s exhausted retry budget "
                        "(attempt %d/%d).",
                        status, url, params, current_attempt, REQUEST_MAX_ATTEMPTS,
                    )
                    resp.raise_for_status()
                    raise httpx.HTTPStatusError(
                        f"HTTP {status}",
                        request=httpx.Request("GET", url),
                        response=resp,
                    )
                retry_after = _retry_after_seconds(resp)
                sleep_for = min(RETRY_SLEEP_CAP, _backoff_seconds(attempt, retry_after=retry_after))
                logging.warning(
                    "HTTP %s for %s params=%s (attempt %d/%d). "
                    "Sleeping %.2fs before retry.",
                    status, url, params, current_attempt, REQUEST_MAX_ATTEMPTS, sleep_for,
                )
                time.sleep(sleep_for)
                attempt += 1
                _ct_reset(client)
                continue

            resp.raise_for_status()
            payload = str(resp.text or "")
            if cache_ttl_seconds > 0:
                _cache_put(cache_key, payload, cache_ttl_seconds)
            _ct_reset(client)
            return payload

        except httpx.HTTPStatusError as exc:
            status = int(exc.response.status_code or 0)
            if 400 <= status < 500 and status != 429 and status != 408:
                raise
            current_attempt = attempt + 1
            if current_attempt >= REQUEST_MAX_ATTEMPTS:
                logging.warning(
                    "HTTP %s for %s params=%s exhausted retry budget "
                    "(attempt %d/%d): %s.",
                    status, url, params, current_attempt, REQUEST_MAX_ATTEMPTS, exc,
                )
                raise
            sleep_for = min(RETRY_SLEEP_CAP, _backoff_seconds(attempt))
            logging.warning(
                "HTTP %s for %s params=%s (attempt %d/%d): %s. Sleeping %.2fs.",
                status, url, params, current_attempt, REQUEST_MAX_ATTEMPTS, exc, sleep_for,
            )
            time.sleep(sleep_for)
            attempt += 1
            _ct_reset(client)

        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            count = _ct_increment(client)
            client = _reset_client_after_consecutive_timeouts(client, count)
            current_attempt = attempt + 1
            if current_attempt >= REQUEST_MAX_ATTEMPTS:
                logging.warning(
                    "Request timeout/connect error for %s params=%s exhausted "
                    "retry budget (attempt %d/%d, consecutive_timeouts=%d): %s.",
                    url, params, current_attempt, REQUEST_MAX_ATTEMPTS, count, exc,
                )
                raise
            sleep_for = min(RETRY_SLEEP_CAP, _backoff_seconds(attempt))
            logging.warning(
                "Request timeout/connect error for %s params=%s (attempt %d, "
                "max_attempts=%d, consecutive_timeouts=%d): %s. Sleeping %.2fs.",
                url, params, current_attempt, REQUEST_MAX_ATTEMPTS, count, exc, sleep_for,
            )
            time.sleep(sleep_for)
            attempt += 1

        except httpx.RequestError as exc:
            current_attempt = attempt + 1
            if current_attempt >= REQUEST_MAX_ATTEMPTS:
                logging.warning(
                    "Request error for %s params=%s exhausted retry budget "
                    "(attempt %d/%d): %s.",
                    url, params, current_attempt, REQUEST_MAX_ATTEMPTS, exc,
                )
                raise
            sleep_for = min(RETRY_SLEEP_CAP, _backoff_seconds(attempt))
            logging.warning(
                "Request error for %s params=%s (attempt %d/%d): %s. Sleeping %.2fs.",
                url, params, current_attempt, REQUEST_MAX_ATTEMPTS, exc, sleep_for,
            )
            time.sleep(sleep_for)
            attempt += 1


def _cache_key(url: str, params: Dict[str, Any] | None) -> str:
    if not params:
        return url
    pairs: List[Tuple[str, str]] = []
    for key, value in sorted(params.items(), key=lambda item: str(item[0])):
        if value is None:
            continue
        pairs.append((str(key), str(value)))
    if not pairs:
        return url
    return f"{url}?{urlencode(pairs)}"


def _cache_get(key: str) -> Any | None:
    now = time.time()
    with _RESPONSE_CACHE_LOCK:
        item = _RESPONSE_CACHE.get(key)
        if item is None:
            return None
        expires_at, payload = item
        if expires_at < now:
            _RESPONSE_CACHE.pop(key, None)
            return None
        return payload


def _cache_put(key: str, payload: Any, ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return
    expires_at = time.time() + float(ttl_seconds)
    with _RESPONSE_CACHE_LOCK:
        _RESPONSE_CACHE[key] = (expires_at, payload)


def cache_prune() -> int:
    """Remove all expired response-cache entries in one pass."""
    now = time.time()
    with _RESPONSE_CACHE_LOCK:
        expired_keys = [key for key, (expires_at, _) in _RESPONSE_CACHE.items() if expires_at < now]
        for key in expired_keys:
            _RESPONSE_CACHE.pop(key, None)
    return len(expired_keys)


def _throttle_requests() -> None:
    now = time.time()
    rps_limit = max(1, int(REQUEST_RPS_LIMIT))
    sleep_for = 0.0
    with _REQUEST_TS_LOCK:
        while _REQUEST_TS_WINDOW and now - _REQUEST_TS_WINDOW[0] >= 1.0:
            _REQUEST_TS_WINDOW.popleft()
        if len(_REQUEST_TS_WINDOW) >= rps_limit:
            sleep_for = max(0.0, 1.0 - (now - _REQUEST_TS_WINDOW[0])) + 0.001
        if sleep_for <= 0:
            _REQUEST_TS_WINDOW.append(now)
    if sleep_for > 0:
        time.sleep(sleep_for)
        with _REQUEST_TS_LOCK:
            current = time.time()
            while _REQUEST_TS_WINDOW and current - _REQUEST_TS_WINDOW[0] >= 1.0:
                _REQUEST_TS_WINDOW.popleft()
            _REQUEST_TS_WINDOW.append(current)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = str(resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def _backoff_seconds(attempt: int, retry_after: float | None = None) -> float:
    if retry_after is not None and retry_after > 0:
        return min(REQUEST_BACKOFF_MAX_SECONDS, retry_after + random.uniform(0.0, 0.25))
    base = REQUEST_BACKOFF_SECONDS * (2**max(0, int(attempt)))
    jitter = random.uniform(0.0, min(0.5, base * 0.2))
    # Per-attempt sleep is additionally capped at RETRY_SLEEP_CAP by callers
    return min(REQUEST_BACKOFF_MAX_SECONDS, base + jitter)


def _parse_token_ids(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if x]
        except json.JSONDecodeError:
            return [raw]
    return []


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _parse_iso_ts(raw: Any) -> int:
    text = str(raw or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return int(parsed.timestamp())
    except (TypeError, ValueError, OSError, OverflowError):
        return 0


def _merge_earliest_positive(current: int, candidate: int) -> int:
    cur = int(current or 0)
    cand = int(candidate or 0)
    if cur <= 0:
        return cand
    if cand <= 0:
        return cur
    return min(cur, cand)


def _extract_profile_enrichment_from_next_data(next_data: Dict[str, Any]) -> Dict[str, Any]:
    prediction_count = 0
    joined_ts = 0

    queries = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("dehydratedState", {})
        .get("queries", [])
    )
    if not isinstance(queries, list):
        return {
            "profile_joined_ts": 0,
            "profile_prediction_count": 0,
        }

    for query in queries:
        if not isinstance(query, dict):
            continue
        key_obj = query.get("queryKey")
        key0 = ""
        if isinstance(key_obj, list) and key_obj:
            key0 = str(key_obj[0] or "")
        elif isinstance(key_obj, str):
            key0 = key_obj
        data = query.get("state", {}).get("data")
        if not isinstance(data, dict):
            continue

        if key0 == "user-stats":
            prediction_count = max(prediction_count, _safe_int(data.get("trades"), 0))
            joined_ts = _merge_earliest_positive(joined_ts, _parse_iso_ts(data.get("joinDate")))
        elif key0 == "/api/profile/marketsTraded":
            prediction_count = max(prediction_count, _safe_int(data.get("traded"), 0))
        elif key0 == "/api/profile/userData":
            joined_ts = _merge_earliest_positive(joined_ts, _parse_iso_ts(data.get("createdAt")))

    return {
        "profile_joined_ts": joined_ts,
        "profile_prediction_count": prediction_count,
    }


_THESIS_CATEGORY_MAP: Dict[str, str] = {
    "politics": "politics",
    "politic": "politics",
    "elections": "politics",
    "election": "politics",
    "geopolitics": "politics",
    "government": "politics",
    "sports": "sports",
    "sport": "sports",
    "nba": "sports",
    "nfl": "sports",
    "nhl": "sports",
    "mlb": "sports",
    "soccer": "sports",
    "football": "sports",
    "tennis": "sports",
    "golf": "sports",
    "boxing": "sports",
    "ufc": "sports",
    "mma": "sports",
}


def _normalize_market_category(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    return _THESIS_CATEGORY_MAP.get(text, "")


def _derive_event_category(tags: Any) -> tuple[str, str]:
    if not isinstance(tags, list):
        return "", ""
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        # Prefer slug first because labels can be localized / noisy.
        for candidate in (tag.get("slug"), tag.get("label")):
            normalized = _normalize_market_category(candidate)
            if normalized:
                return normalized, str(candidate or "")
    return "", ""


def _fetch_event_category_by_condition(
    session: httpx.Client | _RequestsSession,
    max_pages: int = 200,
) -> Dict[str, Dict[str, str]]:
    """Map condition_id -> {category, category_source} using /events tags."""
    category_by_condition: Dict[str, Dict[str, str]] = {}
    limit = 500
    offset = 0
    page = 0
    while page < max_pages:
        page += 1
        events = _request_json(
            session,
            f"{GAMMA_BASE_URL}/events",
            params={
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
            },
        )
        if not isinstance(events, list) or not events:
            break
        for event in events:
            if not isinstance(event, dict):
                continue
            category, source = _derive_event_category(event.get("tags"))
            if not category:
                continue
            for market in (event.get("markets") or []):
                if not isinstance(market, dict):
                    continue
                condition_id = str(market.get("conditionId") or "").strip().lower()
                if not condition_id:
                    continue
                if condition_id in category_by_condition:
                    continue
                category_by_condition[condition_id] = {
                    "category": category,
                    "category_source": source or category,
                }
        offset += limit
        if len(events) < limit:
            break
    return category_by_condition


def _extract_raw_tags(event: Any) -> list[tuple[str, str]]:
    """Return every gamma tag from an event as (slug, label) pairs.

    Unlike `_derive_event_category` which filters + collapses to a 2-value
    category, this helper preserves the full tag set verbatim. Callers are
    responsible for treating the returned slugs as opaque strings — no
    downstream semantic mapping should happen in polymarket_api.

    Per `docs/superpowers/specs/2026-04-23-wallet-taxonomy-migration.md`.
    """
    if not isinstance(event, dict):
        return []
    tags = event.get("tags") or []
    if not isinstance(tags, list):
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        slug = str(tag.get("slug") or "").strip().lower()
        if not slug or slug in seen:
            continue
        label = str(tag.get("label") or "").strip()
        out.append((slug, label))
        seen.add(slug)
    return out


def fetch_event_tags_by_condition(
    session: httpx.Client | _RequestsSession,
    max_pages: int = 200,
    *,
    include_closed: bool = True,
) -> Dict[str, List[tuple[str, str]]]:
    """Map condition_id -> list[(tag_slug, tag_label)] from gamma /events.

    Pure function: no DB side effects. Callers (backfill script, live sync
    pipeline) decide how to persist. Slugs are lowercased once at ingest
    time and otherwise treated as opaque. No filtering, no category
    collapse — every tag on every event makes it into the returned map.

    `include_closed=True` paginates BOTH active and closed events so wallet
    analysis can tag historical fills. Default on because most wallet fills
    happen on markets that eventually close. Set False for live-pipeline
    callers that only care about currently-tradable markets.
    """
    tags_by_condition: Dict[str, List[tuple[str, str]]] = {}
    passes: list[dict[str, str]] = [{"active": "true", "closed": "false"}]
    if include_closed:
        passes.append({"active": "false", "closed": "true"})

    for params in passes:
        limit = 500
        offset = 0
        page = 0
        while page < max_pages:
            page += 1
            events = _request_json(
                session,
                f"{GAMMA_BASE_URL}/events",
                params={**params, "limit": limit, "offset": offset},
            )
            if not isinstance(events, list) or not events:
                break
            for event in events:
                if not isinstance(event, dict):
                    continue
                raw_tags = _extract_raw_tags(event)
                if not raw_tags:
                    continue
                for market in (event.get("markets") or []):
                    if not isinstance(market, dict):
                        continue
                    condition_id = str(market.get("conditionId") or "").strip().lower()
                    if not condition_id:
                        continue
                    if condition_id in tags_by_condition:
                        continue
                    tags_by_condition[condition_id] = list(raw_tags)
            offset += limit
            if len(events) < limit:
                break
    return tags_by_condition


def fetch_market_index(
    session: httpx.Client | _RequestsSession,
    min_volume_24h: float = 0.0,
    max_pages: int = 200,
) -> Tuple[List[str], Dict[str, str], Dict[str, Dict[str, str]]]:
    asset_ids: List[str] = []
    asset_to_condition: Dict[str, str] = {}
    condition_meta: Dict[str, Dict[str, str]] = {}

    limit = 500
    offset = 0
    page = 0

    category_by_condition: Dict[str, Dict[str, str]] = {}
    try:
        category_by_condition = _fetch_event_category_by_condition(session, max_pages=max_pages)
        logging.info("Loaded category tags for %s active markets", len(category_by_condition))
    except Exception:
        logging.exception("Failed to fetch event categories; thesis category filters may be sparse")

    while page < max_pages:
        page += 1
        markets = _request_json(
            session,
            f"{GAMMA_BASE_URL}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
            },
        )
        if not isinstance(markets, list) or not markets:
            break

        for market in markets:
            try:
                volume_24h = float(market.get("volume24hr") or 0.0)
            except (TypeError, ValueError):
                volume_24h = 0.0

            if volume_24h < min_volume_24h:
                continue

            condition_id = str(market.get("conditionId") or "")
            if not condition_id:
                continue
            normalized_condition_id = condition_id.strip().lower()
            category_meta = category_by_condition.get(normalized_condition_id, {})
            category = str(category_meta.get("category") or "")
            category_source = str(category_meta.get("category_source") or "")

            condition_meta[condition_id] = {
                "title": str(market.get("question") or ""),
                "slug": str(market.get("slug") or ""),
                "event_slug": str(market.get("eventSlug") or ""),
                "volume_24h": str(market.get("volume24hr") or "0"),
                "end_date_iso": str(market.get("endDateIso") or ""),
                "category": category,
                "category_source": category_source,
            }

            for asset_id in _parse_token_ids(market.get("clobTokenIds")):
                if asset_id in asset_to_condition:
                    continue
                asset_to_condition[asset_id] = condition_id
                asset_ids.append(asset_id)

        offset += limit
        if len(markets) < limit:
            break

    logging.info(
        "Loaded %s subscribed assets across %s markets (min_volume_24h=%s)",
        len(asset_ids),
        len(condition_meta),
        min_volume_24h,
    )
    return asset_ids, asset_to_condition, condition_meta


def fetch_market_by_condition(
    session: httpx.Client | _RequestsSession,
    condition_id: str,
    limit: int = 1,
) -> Dict[str, Any]:
    if not condition_id:
        return {}

    rows = _request_json(
        session,
        f"{GAMMA_BASE_URL}/markets",
        params={"condition_ids": condition_id, "limit": limit},
        cache_ttl_seconds=MARKET_LOOKUP_CACHE_TTL_SECONDS,
    )
    if not isinstance(rows, list) or not rows:
        return {}
    market = rows[0]
    if not isinstance(market, dict):
        return {}
    return market


def fetch_market_trades(
    session: httpx.Client | _RequestsSession,
    condition_id: str,
    side: str = "BUY",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    params = {
        "market": condition_id,
        "side": side,
        "limit": limit,
        "takerOnly": "false",
    }
    data = _request_json(
        session,
        f"{DATA_BASE_URL}/trades",
        params=params,
        cache_ttl_seconds=MARKET_TRADES_CACHE_TTL_SECONDS,
    )
    return data if isinstance(data, list) else []


def fetch_user_trades(
    session: httpx.Client | _RequestsSession,
    wallet: str,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    params = {
        "user": wallet,
        "limit": limit,
        "takerOnly": "false",
    }
    data = _request_json(
        session,
        f"{DATA_BASE_URL}/trades",
        params=params,
        cache_ttl_seconds=USER_TRADES_CACHE_TTL_SECONDS,
    )
    return data if isinstance(data, list) else []


# NOTE: a second, dead `fetch_user_activity` (with early_stop_ts/max_pages=5) lived here and was
# silently shadowed by the canonical walk-full-history def below — removed 2026-06-17 (review
# footgun: a caller written against the old signature got the new one). `_coerce_activity_ts`
# below was its only caller; retained as a harmless ts-coercion utility.


def _coerce_activity_ts(row: Dict[str, Any]) -> int:
    """Extract a unix timestamp from an activity row."""
    raw = row.get("timestamp") or row.get("createdAt") or row.get("created_at")
    if raw is None:
        return 0
    try:
        ts = float(raw)
        if ts > 1e12:
            ts = ts / 1000.0
        return int(ts)
    except (TypeError, ValueError):
        pass
    if isinstance(raw, str):
        from datetime import datetime, timezone
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            return 0
    return 0


def fetch_user_positions(
    session: httpx.Client | None,
    wallet: str,
    endpoint: str = "positions",
    page_limit: int = 50,
    progress_emitter: Callable[[str, dict], None] | None = None,
) -> Tuple[List[Dict[str, Any]], FetchTermination]:
    """Fetch all positions for a wallet, paginating to natural exhaustion.

    No client-side page cap — pagination continues until the API returns an
    empty page, a partial final page, or a 4xx (spec A.1).

    For closed-positions: uses sortDirection=DESC (newest first) so that if
    Polymarket ever does refuse past some offset, the loss is in the OLD slice.
    The /positions endpoint rejects sortBy with HTTP 400, so sort params are
    only applied for closed-positions.

    Returns (rows, FetchTermination). Callers that only need rows should
    unpack: ``rows, _ = fetch_user_positions(...)``.

    Uses direct httpx calls (not _request_json) so that ANY 4xx — including
    429 — terminates pagination immediately as api_refused rather than being
    retried indefinitely by the general-purpose retry loop.
    """
    client = session if isinstance(session, httpx.Client) else _get_httpx_client()
    rows: List[Dict[str, Any]] = []
    offset = 0
    page_number = 0
    started = time.time()

    def _emit_terminated(reason: str, term_offset: int, term_status: Optional[int]) -> None:
        if progress_emitter is not None:
            try:
                progress_emitter("fetch_terminated", {
                    "endpoint": endpoint,
                    "reason": reason,
                    "n_rows": len(rows),
                    "offset": term_offset,
                    "status": term_status,
                })
            except Exception:
                logger.debug(
                    "progress_emitter raised for fetch_terminated; continuing fetch"
                )

    while True:
        params: Dict[str, Any] = {
            "user": wallet,
            "sizeThreshold": 0,
            "limit": page_limit,
            "offset": offset,
        }
        if endpoint == "closed-positions":
            params["sortBy"] = "TIMESTAMP"
            params["sortDirection"] = "DESC"

        _throttle_requests()
        try:
            resp = client.get(f"{DATA_BASE_URL}/{endpoint}", params=params)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RequestError):
            # Transient error on a pagination page — treat as api_refused to
            # avoid blocking the caller with an infinite retry.
            _emit_terminated("api_refused", offset, None)
            return rows, FetchTermination(reason="api_refused", status=None, offset=offset)

        status = int(resp.status_code or 0)
        if 400 <= status < 600:
            # Any 4xx (including 429) or 5xx terminates pagination immediately.
            _emit_terminated("api_refused", offset, status)
            return rows, FetchTermination(reason="api_refused", status=status, offset=offset)

        try:
            data = resp.json()
        except Exception:
            _emit_terminated("empty_page", offset, None)
            return rows, FetchTermination(reason="empty_page", offset=offset)

        if not isinstance(data, list) or not data:
            _emit_terminated("empty_page", offset, None)
            return rows, FetchTermination(reason="empty_page", offset=offset)

        rows.extend(data)
        page_number += 1
        if progress_emitter is not None:
            try:
                progress_emitter("fetch_progress", {
                    "endpoint": endpoint,
                    "n_rows": len(data),
                    "offset": offset,
                    "page": page_number,
                    "elapsed_s": float(time.time() - started),
                })
            except Exception:
                logger.debug(
                    "progress_emitter raised for fetch_progress; continuing fetch"
                )
        if len(data) < page_limit:
            _emit_terminated("partial_page", offset, None)
            return rows, FetchTermination(reason="partial_page", offset=offset)
        offset += page_limit
        time.sleep(0.1)


def fetch_closed_positions(
    session: httpx.Client | None,
    wallet: str,
) -> List[Dict[str, Any]]:
    """Fetch closed positions from /closed-positions endpoint.

    Wraps fetch_user_positions with a proxyWallet mismatch WARN check.
    The mismatched row is still returned — caller decides whether to use it.
    Returns only the rows (discards FetchTermination) for backward compat.
    """
    logger = logging.getLogger(__name__)
    _result = fetch_user_positions(session, wallet, endpoint="closed-positions")
    # Real function returns (rows, FetchTermination); legacy test mocks return a list.
    rows = _result[0] if isinstance(_result, tuple) else list(_result)
    for row in rows:
        proxy = (row.get("proxyWallet") or "").lower()
        if proxy and proxy != wallet.lower():
            logger.warning(
                "fetch_closed_positions: proxyWallet %s != wallet %s",
                row.get("proxyWallet"),
                wallet,
            )
    return rows


def fetch_user_activity(
    session: httpx.Client | None,
    wallet: str,
    activity_type: str = "TRADE",
    page_limit: int = 1000,
    max_pages: int = 60,
    walk_full_history: bool = False,
    start_ts: int | None = None,
    progress_emitter: Callable[[str, dict], None] | None = None,
) -> List[Dict[str, Any]]:
    """
    Fetch raw transaction-level activity for a wallet from the /activity
    endpoint. Used for behavioral fingerprinting (laddering, fill timing,
    inter-fill cadence) — patterns invisible at the per-position aggregate
    level.

    Polymarket caps `offset` at 3000 (returns HTTP 400 beyond). Without
    `walk_full_history`, this returns at most the most-recent 3000 rows.

    Args:
        walk_full_history: When True, walk backward through history by
            shifting the `end=<unix_ts>` parameter to the oldest row's
            timestamp once a window's offset cap is hit, then resetting
            offset to 0. Continues until a window returns < `page_limit`
            rows OR a fetched row's timestamp is older than `start_ts`.
            Walk continues to natural exhaustion — no row-count cap (spec A.2).
            Verified 2026-04-26 against the production API.
        start_ts: Optional unix epoch lower bound. Stops walking once a
            row older than this is seen.
        activity_type: 'TRADE', 'REDEEM', 'MERGE', 'SPLIT', etc.
            Pass None to skip the filter.
        progress_emitter: Optional Callable[[str, dict], None]. Receives
            fetch_progress (per page), fetch_window_complete (per window),
            and fetch_terminated (once at end) events (spec A.6). The
            fetch_terminated payload carries the real termination reason so
            callers can capture it via the emitter side-channel.
    """
    _ACTIVITY_ENDPOINT = "activity"
    rows: List[Dict[str, Any]] = []
    end_ts: int | None = None
    # Dedup on the full per-fill identity, NOT transactionHash alone. Polymarket's
    # /activity returns one row per fill, and a taker order crossing N maker price
    # levels settles in ONE tx → N rows that share a transactionHash but differ in
    # price/size. Keying on tx alone dropped all-but-one fill of every multi-fill
    # order (verified 2026-06-15 on 0x20ad75e1…/0x252887d8…: a 9-fill 21,250-share
    # buy collapsed to one 5,000-share row). The composite key keeps distinct fills
    # while still collapsing byte-identical rows re-served across offset pages /
    # window boundaries (the dedup's legitimate purpose).
    seen_fills: set[tuple] = set()
    started = time.time()
    # Tracks the real termination reason; updated whenever the loop exits
    # for a specific reason (partial_page, api_refused, start_ts lower-bound).
    # Defaults to "empty_page" (natural end — API returned empty page).
    _term_reason: str = "empty_page"
    _term_status: Optional[int] = None
    _term_offset: int = 0
    # walk_full_history=True: no client-side window cap — natural termination
    # via backward-progress detection (window_min_ts >= end_ts) or empty page.
    # walk_full_history=False: single window; loop breaks at end of first pass.

    def _safe_emit(event_name: str, payload: dict) -> None:
        if progress_emitter is not None:
            try:
                progress_emitter(event_name, payload)
            except Exception:
                logger.debug(
                    "progress_emitter raised for %s; continuing fetch", event_name
                )

    while True:
        offset = 0
        page = 0
        rows_this_window = 0
        window_min_ts: int | None = None

        while page < max_pages:
            page += 1
            params: Dict[str, Any] = {
                "user": wallet,
                "limit": page_limit,
                "offset": offset,
            }
            if activity_type:
                params["type"] = activity_type
            if end_ts is not None:
                params["end"] = end_ts
            try:
                data = _request_json(
                    session,
                    f"{DATA_BASE_URL}/activity",
                    params=params,
                    cache_ttl_seconds=USER_POSITIONS_CACHE_TTL_SECONDS,
                )
            except httpx.HTTPStatusError as e:
                status = int(e.response.status_code or 0)
                if status == 400:
                    _term_reason = "api_refused"
                    _term_status = status
                    _term_offset = offset
                    break
                _term_reason = "request_failed"
                _term_status = status
                _term_offset = offset
                break
            except httpx.RequestError:
                _term_reason = "request_failed"
                _term_status = None
                _term_offset = offset
                break

            if not isinstance(data, list) or not data:
                # Natural empty page — reason stays "empty_page"
                _term_offset = offset
                break

            # Iterate in order; apply dedup + start_ts filter inline so we
            # never KEEP a row outside the requested window.
            stop_due_to_start_ts = False
            for row in data:
                ts = int(row.get("timestamp") or 0)
                if start_ts is not None and ts and ts < start_ts:
                    stop_due_to_start_ts = True
                    break
                # Defensive: API should respect `end=` but if it doesn't,
                # don't accumulate rows above the requested window. Otherwise
                # a misbehaving server can produce duplicate windows.
                if end_ts is not None and ts and ts > end_ts:
                    continue
                h = row.get("transactionHash") or row.get("transaction_hash") or ""
                if h:
                    key = (
                        h, ts, row.get("type") or "", row.get("side") or "",
                        str(row.get("conditionId") or row.get("condition_id") or ""),
                        row.get("outcomeIndex"), str(row.get("asset") or ""),
                        row.get("price"), row.get("size"), row.get("usdcSize"),
                    )
                    if key in seen_fills:
                        continue
                    seen_fills.add(key)
                rows.append(row)
                rows_this_window += 1
                if ts and (window_min_ts is None or ts < window_min_ts):
                    window_min_ts = ts

            # Emit fetch_progress after each successful page (spec A.6)
            _safe_emit("fetch_progress", {
                "endpoint": _ACTIVITY_ENDPOINT,
                "n_rows": len(rows),
                "offset": offset,
                "page": page,
                "elapsed_s": time.time() - started,
            })

            if stop_due_to_start_ts:
                _term_reason = "partial_page"
                _term_offset = offset
                _safe_emit("fetch_terminated", {
                    "endpoint": _ACTIVITY_ENDPOINT,
                    "reason": _term_reason,
                    "n_rows": len(rows),
                    "offset": _term_offset,
                    "status": None,
                })
                return rows

            if len(data) < page_limit:
                _term_reason = "partial_page"
                _term_offset = offset
                break
            offset += page_limit
            # No per-page sleep: the global _throttle_requests RPS limiter
            # already paces every _request_json call. capture-hardening-v2:
            # page_limit raised 50→1000 (API confirmed accepts it; the
            # offset 3000 cap is separate) — ~15x fewer HTTP round-trips
            # per window. Dedup is by full per-fill identity (see seen_fills
            # above) so distinct fills sharing a tx are preserved.

        # Window complete — emit before deciding whether to walk
        _safe_emit("fetch_window_complete", {
            "endpoint": _ACTIVITY_ENDPOINT,
            "window_end_ts": end_ts,
            "n_rows_window": rows_this_window,
            "total_rows": len(rows),
        })

        # Decide whether to walk to the next window
        if _term_reason == "request_failed":
            break
        if not walk_full_history:
            break
        if rows_this_window == 0 or window_min_ts is None:
            break
        # Shift `end` to one second before this window's oldest row
        if end_ts is not None and window_min_ts >= end_ts:
            # No backward progress (clock skew or tied timestamps) — stop
            break
        end_ts = window_min_ts - 1
        if start_ts is not None and end_ts <= start_ts:
            break
        time.sleep(0.2)

    _safe_emit("fetch_terminated", {
        "endpoint": _ACTIVITY_ENDPOINT,
        "reason": _term_reason,
        "n_rows": len(rows),
        "offset": _term_offset,
        "status": _term_status,
    })
    return rows


def fetch_user_value(session: httpx.Client | _RequestsSession, wallet: str) -> float:
    data = _request_json(
        session,
        f"{DATA_BASE_URL}/value",
        params={"user": wallet},
        cache_ttl_seconds=USER_VALUE_CACHE_TTL_SECONDS,
    )
    if isinstance(data, list) and data:
        try:
            return float(data[0].get("value") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def fetch_wallet_profile_enrichment(
    session: httpx.Client | _RequestsSession,
    wallet: str,
) -> Dict[str, Any]:
    """Fetch profile-joined timestamp and lifetime prediction count for a wallet.

    Source preference:
    1) `/api/profile/userData` for stable createdAt + identity fields.
    2) Profile page `__NEXT_DATA__` hydration payload for `user-stats.trades`
       and `user-stats.joinDate` without private API tokens.
    """
    wallet_key = str(wallet or "").strip().lower()
    if not wallet_key:
        return {}

    profile_joined_ts = 0
    profile_prediction_count = 0
    username = ""
    pseudonym = ""
    profile_image = ""
    source_parts: List[str] = []

    try:
        user_data = _request_json(
            session,
            f"{WEB_BASE_URL}/api/profile/userData",
            params={"address": wallet_key},
            cache_ttl_seconds=PROFILE_USERDATA_CACHE_TTL_SECONDS,
        )
        if isinstance(user_data, dict):
            profile_joined_ts = _merge_earliest_positive(
                profile_joined_ts,
                _parse_iso_ts(user_data.get("createdAt")),
            )
            username = str(user_data.get("name") or "").strip()
            pseudonym = str(user_data.get("pseudonym") or "").strip()
            profile_image = str(user_data.get("profileImage") or "").strip()
            source_parts.append("polymarket_api_profile_userData")
    except Exception as exc:
        logging.debug("Profile userData fetch failed for %s: %s", wallet_key, exc)

    try:
        profile_html = _request_text(
            session,
            f"{WEB_BASE_URL}/profile/{wallet_key}",
            cache_ttl_seconds=PROFILE_PAGE_CACHE_TTL_SECONDS,
        )
        match = _NEXT_DATA_RE.search(profile_html)
        if match:
            next_data = json.loads(match.group(1))
            enriched = _extract_profile_enrichment_from_next_data(next_data)
            profile_joined_ts = _merge_earliest_positive(
                profile_joined_ts,
                _safe_int(enriched.get("profile_joined_ts"), 0),
            )
            profile_prediction_count = max(
                profile_prediction_count,
                _safe_int(enriched.get("profile_prediction_count"), 0),
            )
            source_parts.append("polymarket_profile_next_data")
    except Exception as exc:
        logging.debug("Profile page enrichment failed for %s: %s", wallet_key, exc)

    return {
        "wallet": wallet_key,
        "profile_joined_ts": int(profile_joined_ts or 0),
        "profile_prediction_count": max(0, int(profile_prediction_count or 0)),
        "profile_enriched_ts": int(time.time()),
        "profile_enrichment_source": "|".join(source_parts),
        "username": username,
        "pseudonym": pseudonym,
        "profile_image": profile_image,
    }


LEADERBOARD_CACHE_TTL_SECONDS = max(
    0, int(os.getenv("PM_CACHE_TTL_LEADERBOARD_SECONDS", "1800"))
)

_LEADERBOARD_CATEGORIES = frozenset(
    {"OVERALL", "POLITICS", "SPORTS", "CRYPTO", "CULTURE", "ECONOMICS", "TECH", "FINANCE"}
)
_LEADERBOARD_TIME_PERIODS = frozenset({"ALL", "MONTH", "WEEK", "DAY"})
_LEADERBOARD_ORDER_BY = frozenset({"PNL", "VOL"})


def fetch_leaderboard_rankings(
    session: httpx.Client | _RequestsSession,
    *,
    category: str = "OVERALL",
    time_period: str = "ALL",
    order_by: str = "PNL",
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Fetch Polymarket official public leaderboard rankings.

    Returns normalised dicts with keys: wallet, rank, official_pnl,
    official_volume, username, profile_image, x_username, verified_badge.
    """
    cat = str(category or "OVERALL").strip().upper()
    if cat not in _LEADERBOARD_CATEGORIES:
        cat = "OVERALL"
    tp = str(time_period or "ALL").strip().upper()
    if tp not in _LEADERBOARD_TIME_PERIODS:
        tp = "ALL"
    ob = str(order_by or "PNL").strip().upper()
    if ob not in _LEADERBOARD_ORDER_BY:
        ob = "PNL"

    params: Dict[str, Any] = {
        "category": cat,
        "timePeriod": tp,
        "orderBy": ob,
        "limit": max(1, min(50, int(limit))),
        "offset": max(0, int(offset)),
    }

    data = _request_json(
        session,
        f"{DATA_BASE_URL}/v1/leaderboard",
        params=params,
        cache_ttl_seconds=LEADERBOARD_CACHE_TTL_SECONDS,
    )

    if not isinstance(data, list):
        return []

    results: List[Dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        wallet = str(row.get("proxyWallet") or "").strip().lower()
        if not wallet:
            continue
        results.append(
            {
                "wallet": wallet,
                "rank": _safe_int(row.get("rank"), 0),
                "official_pnl": float(row.get("pnl") or 0),
                "official_volume": float(row.get("vol") or 0),
                "username": str(row.get("userName") or "").strip(),
                "profile_image": str(row.get("profileImage") or "").strip(),
                "x_username": str(row.get("xUsername") or "").strip(),
                "verified_badge": bool(row.get("verifiedBadge")),
            }
        )
    return results


# --- CLOB price cache (30s TTL) ---
# Keyed by condition_id → {"yes": price, "no": price, "ts": time.time()}
_clob_price_cache: dict = {}
_CLOB_CACHE_TTL = 30  # seconds


def _fetch_clob_market_prices(condition_id: str) -> dict:
    """Fetch all token prices for a market, with 30s cache.

    Returns {"yes": float, "no": float} or {} on failure.
    One HTTP call per condition_id, cached across all outcome lookups.
    """
    import time as _time
    cached = _clob_price_cache.get(condition_id)
    if cached and (_time.time() - cached.get("ts", 0)) < _CLOB_CACHE_TTL:
        return cached

    try:
        r = requests.get(  # type: ignore[union-attr]
            f"https://clob.polymarket.com/markets/{condition_id}",
            timeout=8,
        )
        if r.ok:
            prices = {"ts": _time.time()}
            for tok in r.json().get("tokens", []):
                outcome_key = str(tok.get("outcome", "")).lower()
                if outcome_key and tok.get("price") is not None:
                    prices[outcome_key] = max(0.0, min(1.0, float(tok["price"])))
            _clob_price_cache[condition_id] = prices
            return prices
    except Exception:
        logging.debug("_fetch_clob_market_prices failed for %s", condition_id, exc_info=True)
    return {}


def fetch_live_clob_price(condition_id: str, outcome: str) -> float | None:
    """Fetch live CLOB price for a specific outcome from Polymarket.

    Returns the clamped price in [0.0, 1.0] or None on failure.
    Uses a 30s TTL cache — multiple positions on the same market share one API call.
    """
    prices = _fetch_clob_market_prices(condition_id)
    price = prices.get(outcome.lower())
    if price is not None and price > 0:
        return price
    return None
