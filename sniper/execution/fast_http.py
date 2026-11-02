"""Persistent, pooled HTTP transport for the funded memecoin submission backend.

One long-lived ``httpx.Client`` per provider family — keep-alive connections, a bounded
connection pool, bounded connect/read/write/pool timeouts, and NO automatic retry of POST
requests — exposed through the same injectable seam the executors already use:

    request(method, url, *, headers=None, body=None, timeout=None)
        -> (status:int, json_or_text:object, date:str|None)

Any HTTP response (including 4xx/5xx) returns a status tuple — the caller classifies it.
A transport-level failure (connect/read/write/pool timeout, connection drop) raises
``FastHttpError`` with a REDACTED message: never the URL, query string, api-key, or
Authorization header. ``httpx.Client`` is safe for concurrent use from the bounded
submission workers.

Connection warming keeps keep-alive sockets hot without ever signing or submitting. Each
provider has one daemon worker so a blocked probe cannot delay its peers or process shutdown.
Nothing here constructs a client at import time.
"""
from __future__ import annotations

import math
import threading
import time as _time

import httpx


class FastHttpError(RuntimeError):
    """Redacted transport failure — carries the method + exception TYPE only, never a URL."""


# Baseline-safe defaults. These are generic transport bounds; provider-specific deadlines live
# in the submission transport, not here.
_DEFAULT_MAX_CONNECTIONS = 16
_DEFAULT_MAX_KEEPALIVE = 16


def warm_keepalive_expiry(interval_s, timeout_s, *, serial_calls=1):
    """Outlive the sleep plus serial warm calls. HTTPX bounds pool/connect/write/read separately;
    include each phase and the client's 3s defaults when a raw managed client is supplied."""
    return max(30.0, 2.0 * float(interval_s)
               + int(serial_calls) * 4.0 * max(3.0, float(timeout_s)))


class PooledHttpClient:
    """A reusable ``httpx.Client`` behind the (method,url,headers,body,timeout) seam.

    Construct once per process/provider family and reuse for the lifetime; call ``close()`` on
    shutdown. Pass ``transport=`` (e.g. ``httpx.MockTransport``) to test without a live endpoint.
    """

    def __init__(self, *, transport=None, max_connections=_DEFAULT_MAX_CONNECTIONS,
                 max_keepalive=_DEFAULT_MAX_KEEPALIVE, connect_timeout_s=3.0,
                 read_timeout_s=8.0, write_timeout_s=3.0, pool_timeout_s=3.0,
                 keepalive_expiry_s=30.0):
        limits = httpx.Limits(max_connections=max_connections,
                              max_keepalive_connections=max_keepalive,
                              keepalive_expiry=keepalive_expiry_s)
        timeout = httpx.Timeout(connect=connect_timeout_s, read=read_timeout_s,
                                write=write_timeout_s, pool=pool_timeout_s)
        # httpx.Client does NOT retry by default (no `retries` on the default pool) — a POST is
        # sent exactly once. We deliberately do not enable any retry transport.
        self._client = httpx.Client(transport=transport, limits=limits, timeout=timeout)
        self._closed = False

    def request(
            self, method, url, *, headers=None, body=None, timeout=None,
            _all_headers=False, _strict_json=False):
        """Send one request. Returns (status, parsed_json_or_text, date_header). Raises
        ``FastHttpError`` (redacted) on any transport-level failure — never retried."""
        try:
            kwargs = {}
            if timeout is not None:
                kwargs["timeout"] = timeout
            request_headers = dict(headers or {})
            if (body is not None
                    and not any(str(k).lower() == "content-type" for k in request_headers)):
                # Every production caller sends JSON-RPC. httpx's raw ``content=`` path does not
                # infer a media type; Robinhood correctly rejects the otherwise valid body with 415.
                request_headers["content-type"] = "application/json"
            resp = self._client.request(
                method, url, headers=request_headers, content=body, **kwargs)
        except httpx.HTTPError as exc:
            # NEVER include str(exc) / the URL: httpx embeds the full request URL (with api-key
            # query params) in its exception text. Redact to method + exception type.
            raise FastHttpError("%s request failed: %s" % (method, type(exc).__name__)) from None
        try:
            payload = resp.json()
        except (ValueError, UnicodeDecodeError):
            if _strict_json and 200 <= resp.status_code < 300:
                raise ValueError("metadata response is not JSON") from None
            payload = resp.text
        response_meta = dict(resp.headers) if _all_headers else resp.headers.get("date")
        return resp.status_code, payload, response_meta

    def request_with_headers(self, method, url, *, headers=None, body=None, timeout=None,
                             strict_json=False):
        """Send one request and return all response headers."""
        return self.request(
            method, url, headers=headers, body=body, timeout=timeout,
            _all_headers=True, _strict_json=strict_json,
        )

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._client.close()
        except Exception:
            pass


# --- connection warming --------------------------------------------------------------------------

def warm_once(providers, health=None):
    """Call each ``(label, warm_fn)`` once. ``warm_fn`` returns/raises — success -> healthy,
    any exception -> unhealthy. Returns (and optionally updates) a ``{label: bool}`` health map.
    Never raises; a warm failure only marks that provider unhealthy. This records callback
    completion, not the submission provider's separately validated health."""
    result = {} if health is None else health
    for label, warm_fn in providers:
        try:
            warm_fn()
            result[label] = True
        except Exception:
            result[label] = False
    return result


def _warming_inputs(providers, interval_s):
    interval_s = float(interval_s)
    if not math.isfinite(interval_s) or not 0 < interval_s <= threading.TIMEOUT_MAX:
        raise ValueError("warming interval must be positive, finite and bounded")
    providers = tuple((label, warm_fn) for label, warm_fn in providers)
    if any(not callable(warm_fn) for _label, warm_fn in providers):
        raise ValueError("warming providers require callable probes")
    return providers, interval_s


def run_warm_loop(providers, *, interval_s, stop_event, sleep=None, health=None):
    """Warm providers independently on a start-to-start cadence, without catch-up bursts.

    The frozen provider list bounds workers and in-flight calls; no work is queued. A stopped
    coordinator returns without waiting for a blocked daemon probe. ``sleep`` is injectable.
    """
    providers, interval_s = _warming_inputs(providers, interval_s)

    def run_provider(label, warm_fn):
        while not stop_event.is_set():
            started = _time.monotonic()
            result = warm_once(((label, warm_fn),))
            if stop_event.is_set():
                return
            if health is not None:
                health.update(result)
            delay = max(0.0, interval_s - (_time.monotonic() - started))
            if sleep is None:
                stop_event.wait(delay)
            else:
                sleep(delay)

    for index, (label, warm_fn) in enumerate(providers):
        if stop_event.is_set():
            break
        threading.Thread(
            target=run_provider, args=(label, warm_fn), daemon=True,
            name="memecoin-conn-warm-%d" % index,
        ).start()
    stop_event.wait()


def start_warming(providers, *, interval_s=5.0, stop_event=None, health=None):
    """Spawn a DAEMON warming thread (never keeps the process alive). Returns the thread; the
    caller sets ``stop_event`` and joins with a timeout on shutdown."""
    providers, interval_s = _warming_inputs(providers, interval_s)
    stop_event = stop_event or threading.Event()
    thread = threading.Thread(
        target=run_warm_loop, kwargs={
            "providers": providers, "interval_s": interval_s,
            "stop_event": stop_event, "health": health},
        name="memecoin-conn-warm", daemon=True)
    thread.start()
    return thread
