"""Process-local funded Solana RPC rotation on explicit provider credit exhaustion only."""
from __future__ import annotations

import collections
import contextlib
import functools
import hashlib
import json
import logging
import math
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS


_LOG = logging.getLogger(__name__)
# A re-admit probe claim older than this is abandoned: DNS is not bounded by any socket timeout.
PROBE_CLAIM_STALE_S = 30.0
_CREDIT_EXHAUSTED = -32429


# A provider stating the PLAN is spent, in the words it actually uses. Helius answers a spent plan
# with a plain HTTP 429 and this body -- not the JSON-RPC -32429 the code below looks for, and not
# JSON at all. Measured 2026-08-25: keys d046dcf1 and a56bd061 returned `HTTP 429 max usage reached`
# to a SINGLE getHealth, three times over 40s, while two other keys answered normally. A per-second
# rate limit cannot do that; one request is not a burst.
#
# This matters because of what the reason selects. `http_429` takes the 60s transient bench, so a
# key that is out of credit until the plan resets gets probed every minute for the rest of the
# period -- about 1,440 pointless probes per key per day, and `degraded` flapping in the heartbeat
# so an operator cannot tell a burst from a spent plan. Matching the provider's own words puts it on
# the hour bench, where credit exhaustion belongs.
_CREDIT_EXHAUSTED_BODIES = ("max usage reached", "credit limit", "insufficient credits")


def _credit_exhausted_body(response) -> bool:
    """True when the response BODY says the plan is spent, whatever the status or content type."""
    if not isinstance(response, tuple) or len(response) < 2:
        return False
    for part in response[1:3]:
        if isinstance(part, (bytes, bytearray)):
            try:
                part = part.decode("utf-8", "replace")
            except Exception:
                continue
        if isinstance(part, str) and any(m in part.lower() for m in _CREDIT_EXHAUSTED_BODIES):
            return True
    return False


def _credit_exhausted(response, *, allow_body=False, request_body=None) -> bool:
    if not isinstance(response, tuple) or len(response) < 2:
        return False
    if allow_body and _credit_exhausted_body(response):
        return True
    status, payload = response[0], response[1]
    if status != 200 or not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if ("result" in payload or not isinstance(error, dict)
            or type(error.get("code")) is not int or error["code"] != _CREDIT_EXHAUSTED
            or payload.get("jsonrpc", "2.0") != "2.0"):
        return False
    if "id" in payload:
        # Only candidate rejections need request parsing; ordinary success never enters this path.
        try:
            request = (json.loads(request_body) if isinstance(request_body, (str, bytes, bytearray))
                       else request_body)
        except (TypeError, ValueError):
            return False
        if (not isinstance(request, dict) or "id" not in request
                or type(payload["id"]) is bool or type(request["id"]) is bool
                or payload["id"] != request["id"]):
            return False
    return True


# Statuses that mean THIS ENDPOINT will not serve us right now.
#
# WHY THIS EXISTS (2026-08-25): rotation fired ONLY on the JSON-RPC -32429 body, which requires an
# HTTP 200. Real rate limiting does not arrive that way. The funded runner logged 1,812 HTTP 429s
# and ZERO -32429, and the pool still reported active_index=0 exhausted_count=0 — it had never
# rotated in its life. Worse, urllib RAISES on a 4xx, so the 429 propagated as an exception and
# never reached _credit_exhausted at all. The rotation was not mis-tuned, it was unreachable.
#
# 403 is included because a throttled or revoked key commonly answers 403 rather than 429.
# 5xx and 408 are provider-side faults where another endpoint is strictly better.
# NOT included: 400/401/404/422 — those say the REQUEST is wrong, and rotating would just repeat
# a bad request against every endpoint in turn and blame the last one.
_UNAVAILABLE_STATUSES = frozenset({403, 408, 429, 500, 502, 503, 504})

# ONLY THESE METHODS MAY ROTATE ON AN UNAVAILABLE ENDPOINT, AND THE ALLOWLIST IS THE SAFETY
# PROPERTY. A timeout or a 429 on `sendTransaction` is AMBIGUOUS, never a failure: the bytes may
# already be in flight, so re-sending them to a second endpoint can double-submit a funded trade.
# That is why the original seam refused to rotate on anything but -32429, and that refusal was
# correct for writes even though it left reads hammering a dead key.
#
# Every method here is idempotent — asking twice costs a round trip and nothing else. Anything
# NOT listed keeps the old behaviour exactly: -32429 rotates, everything else propagates to the
# caller, which already treats transport loss on a submit as `submitted_unknown`.
# Do NOT add sendTransaction, simulateTransaction, or any signing/broadcast method.
_ROTATABLE_METHODS = frozenset({
    "getAccountInfo", "getBalance", "getBlockHeight", "getGenesisHash", "getLatestBlockhash",
    "getMultipleAccounts", "getProgramAccounts", "getSignatureStatuses",
    "getSignaturesForAddress", "getSlot", "getTokenAccountBalance", "getTokenAccountsByOwner",
    "getTokenLargestAccounts", "getTokenSupply", "getTransaction", "getHealth", "getVersion",
})


def _body_methods(body):
    """The JSON-RPC method of each request in `body` (a batch has several), or None when the body is
    not a request at all. A batch member that is not an object reads as None. Parsed once per call:
    the rotation rule and the call counter both use it."""
    if not body:
        return None
    try:
        payload = json.loads(body) if isinstance(body, (str, bytes, bytearray)) else body
    except (ValueError, TypeError):
        return None
    if isinstance(payload, list):
        return tuple(p.get("method") if isinstance(p, dict) else None for p in payload)
    return (payload.get("method"),) if isinstance(payload, dict) else None


def _rotatable(methods) -> bool:
    return bool(methods) and all(m in _ROTATABLE_METHODS for m in methods)


def _rotatable_method(body) -> bool:
    """True only for an idempotent read. Unparseable body -> False (treat as a write); a JSON-RPC
    batch rotates only when every member is safe."""
    return _rotatable(_body_methods(body))


# THE CALL COUNTER'S LABEL (spec 2026-10-01 burst read coalescing, §5). Per THREAD, never a module
# attribute shared across threads: `rpc_label` sets and restores it on the calling thread only, so
# a concurrent decision can never see another's label (the gmgn_stats capture-then-restore defect
# was a shared attribute). An unlabelled thread is named by its thread's class.
_LABEL = threading.local()
_THREAD_SUFFIX = re.compile(r"\s*\(.*\)$|(?:[-_]\d+)+$")


@contextlib.contextmanager
def rpc_label(label):
    """Label every funded-pool request this thread makes inside the block. The lane class is worked
    out here, once per block, so the call path only reads it (measured: per call it cost ~0.25 us)."""
    previous = getattr(_LABEL, "value", None), getattr(_LABEL, "lane", None)
    _LABEL.value = str(label)
    _LABEL.lane = _lane_class(_LABEL.value)
    try:
        yield
    finally:
        _LABEL.value, _LABEL.lane = previous


@functools.lru_cache(maxsize=256)
def _thread_class(name):
    return _THREAD_SUFFIX.sub("", _THREAD_SUFFIX.sub("", str(name))) or "thread"


def current_label():
    value = getattr(_LABEL, "value", None)
    return value if value else "thread:" + _thread_class(threading.current_thread().name)


COUNTER_MINUTES = 60
COUNTER_MAX_KEYS = 1000          # ponytail: a runaway label set collapses into "other", never grows
# The minute ring is keyed by lane CLASS only, never the full label (Alpha, 2026-10-01: the full-label
# ring doubled the heartbeat and its JSON time is GIL time on the runner's main loop).
_LANE_CLASSES = frozenset({"entry", "entry_preread", "exit"})


def _lane_class(label):
    kind = label.partition(":")[0]
    return kind if kind in _LANE_CLASSES else "other"


def _status_unavailable(response) -> bool:
    if not isinstance(response, tuple) or len(response) < 2:
        return False
    try:
        return int(response[0]) in _UNAVAILABLE_STATUSES
    except (TypeError, ValueError):
        return False


def _exception_body(exc):
    """Body carried by a raised transport error, read at most once and cached on the exception.

    urllib RAISES HTTPError on 4xx, so a spent-plan 429 never reaches the response-path body check —
    it is the EXCEPTION that carries the provider's words. Reading consumes the stream, so the text
    is stashed back on the exception: the pool re-raises `last_exc` when every endpoint is out, and a
    caller inspecting it must not find an empty body because we looked first."""
    cached = getattr(exc, "_pool_body_peek", None)
    if cached is not None:
        return cached
    text = ""
    read = getattr(exc, "read", None)
    if callable(read):
        try:
            raw = read()
            text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except Exception:
            text = ""
    try:
        exc._pool_body_peek = text
    except Exception:
        pass
    return text


def _exception_status(exc):
    """HTTP status carried by a raised transport error, or None."""
    for attr in ("code", "status", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _nest(flat):
    out = {}
    for (label, method), n in flat.items():
        out.setdefault(label, {})[method] = n
    return out


def key_id(url) -> str:
    """Operator-safe name for one endpoint: sha256 prefix of its API credential, else its host.

    Never the URL itself -- a Helius URL IS the credential. The 8-hex prefix matches the inventory
    the crew already uses, so a page names the key the operator will look up.
    ponytail: a key embedded in the PATH (QuickNode/Alchemy style) shows as its host, so two such
    keys on one host read the same; hash the path too if one ever joins a pool.
    """
    parts = urlsplit(str(url).strip())
    params = parse_qs(parts.query)
    key = (params.get("api-key") or params.get("api_key") or [""])[0]
    if key:
        return hashlib.sha256(key.encode()).hexdigest()[:8]
    return parts.hostname or "?"


class RotatingSolanaRpcHttp:
    """Ordered RPC seam: WRITEs rotate only on explicit ``-32429``; idempotent READs may fail over.

    Requests for every other URL (Jupiter and official fanout endpoints) pass through unchanged.
    Exhausted endpoints are reconsidered with a read-only genesis probe after ``retry_s``.
    """

    def __init__(self, http, urls, *, retry_s=3600.0, transient_retry_s=60.0, clock=None,
                 on_credit_exhausted=None, name="funded", wall=None, env=None):
        urls = tuple(str(url).strip() for url in urls if str(url).strip())
        if not urls:
            raise ValueError("Solana funded RPC pool requires at least one endpoint")
        from sniper.services.memecoin_solana_provider import infra_provider
        self._legacy_credit_body = infra_provider(env) == "legacy"
        self._http = http
        # Log label only. The research lanes use this class too, and "funded" in their logs read
        # as a funded RPC outage.
        self._name = str(name)
        self._urls = urls
        self._anchor = urls[0]
        self._retry_s = max(1.0, float(retry_s))
        self._transient_retry_s = max(1.0, float(transient_retry_s))
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        # ONE re-admit probe at a time, as (generation, claimed_at); see _claim_probe.
        self._probe_claim = None
        self._probe_gen = 0
        self._probe_thread = None
        self._active = 0
        self._blocked_until = [None] * len(urls)
        # WHY each endpoint is benched, per index. `_last_reason` is the pool's most recent rotation
        # and is display state; it is NOT this endpoint's reason once a second endpoint rotates for a
        # different cause, so the re-bench below must not read it.
        self._blocked_reason = [None] * len(urls)
        self._last_reason = None
        self._last_rotation_at = None
        # Per-endpoint usage for key_status(). Counted under the lock the call loop already takes,
        # so it adds no lock round trip to a funded read.
        self._calls = [0] * len(urls)
        # Requests by (label, method) and a per-minute ring by label, under the same lock: the
        # spec's instrument. `wall` stamps the minutes; telemetry only, never read by a decision.
        self._wall = wall or time.time
        self._by_label = {}
        self._count_failures = 0
        self._minutes = collections.deque(maxlen=COUNTER_MINUTES)
        # `on_credit_exhausted(index, key_id)` fires ONCE per endpoint per process, outside the
        # lock. A caller on a latency-sensitive path must make it non-blocking; a raise is logged
        # and swallowed so a page can never fail the read that discovered the exhaustion.
        self._on_credit_exhausted = on_credit_exhausted
        self._paged = set()

    @staticmethod
    def _kwargs(headers, body, timeout):
        kwargs = {"headers": headers, "body": body}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return kwargs

    def _request(self, method, url, headers, body, timeout):
        return self._http(method, url, **self._kwargs(headers, body, timeout))

    def _claim_probe(self):
        """(endpoint index, generation) for the one re-admit probe now due, or None.

        A claim older than PROBE_CLAIM_STALE_S is abandoned (2026-09-27): a probe stuck in DNS,
        which no socket timeout bounds, would otherwise hold the only slot and no benched endpoint
        could be re-admitted until it returned. Its late result is ignored by generation."""
        now = self._clock()
        with self._lock:
            candidates = [
                idx for idx, until in enumerate(self._blocked_until)
                if until is not None and until <= now
            ]
            if not candidates:
                return None
            if self._probe_claim is not None and now - self._probe_claim[1] < PROBE_CLAIM_STALE_S:
                return None
            self._probe_gen += 1
            self._probe_claim = (self._probe_gen, now)
            return candidates[0], self._probe_gen

    def _refresh_one(self, *, headers=None, timeout=None):
        """Claim and run the due probe on THIS thread (direct callers and tests)."""
        claim = self._claim_probe()
        if claim is not None:
            self._probe(*claim, headers=headers, timeout=timeout)

    def _refresh_background(self, *, headers=None, timeout=None):
        """Claim the due probe and run it on its own daemon thread (2026-09-27).

        Every custody call used to run the probe INLINE first, sends and exit previews included,
        so whenever a benched endpoint's window expired the caller paid a request to an endpoint
        that was usually still dead. A re-admitted endpoint now serves the NEXT call."""
        claim = self._claim_probe()
        if claim is None:
            return
        try:
            thread = threading.Thread(target=self._probe, args=claim, daemon=True,
                                      kwargs={"headers": headers, "timeout": timeout},
                                      name="rpc-pool-probe")
            thread.start()
            self._probe_thread = thread
        except Exception:
            with self._lock:
                if self._probe_claim is not None and self._probe_claim[0] == claim[1]:
                    self._probe_claim = None
            _LOG.exception("Solana %s RPC re-admit probe could not start", self._name)

    def _probe(self, idx, gen, *, headers=None, timeout=None):
        try:
            probe = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "getGenesisHash", "params": [],
            })
            try:
                response = self._request("POST", self._urls[idx], headers, probe, timeout)
            except Exception:
                response = None
            restored = (
                isinstance(response, tuple) and len(response) >= 2
                and response[0] == 200 and isinstance(response[1], dict)
                and response[1].get("result") == SOLANA_MAINNET_GENESIS
            )
            with self._lock:
                if self._probe_claim is None or self._probe_claim[0] != gen:
                    return                      # an abandoned claim: a newer probe owns the state
                if restored:
                    self._blocked_until[idx] = None
                    self._blocked_reason[idx] = None
                    if idx == 0:
                        self._last_reason = None
                    if idx < self._active or self._blocked_until[self._active] is not None:
                        self._active = idx
                else:
                    # Still refusing. Re-bench on the class that benched THIS endpoint, so a
                    # transient 429 cannot be promoted to an hour just by failing one probe.
                    # Reading `_last_reason` here would take whichever endpoint rotated most
                    # recently: bench 0 on credit_exhausted, bench 1 on http_429, and a failed probe
                    # of 0 would then get the 60s window that belongs to 1.
                    self._blocked_until[idx] = self._clock() + self._bench_s(
                        self._blocked_reason[idx])
            if restored:
                _LOG.info("Solana %s RPC credit restored index=%d endpoint_count=%d",
                          self._name, idx, len(self._urls))
        finally:
            with self._lock:
                if self._probe_claim is not None and self._probe_claim[0] == gen:
                    self._probe_claim = None

    def _bench_s(self, reason):
        """How long this endpoint stays out, chosen by WHY it failed.

        `credit_exhausted` (-32429) is a provider stating the ACCOUNT is out of credit. That is a
        daily/monthly cap and an hour is the right bench.

        An HTTP 429 is a different animal: it is almost always a per-SECOND rate limit that clears
        in seconds. When rotation was widened to HTTP statuses (2026-08-25) this reused the
        credit-exhaustion window, so one burst of requests benched a paid endpoint for a full hour.
        Live that evening: indexes 0 and 1 both out on `http_429` with three endpoints left, none of
        them recoverable early because `_refresh_one` only probes AFTER the window elapses.
        Transient statuses get the short bench; only an explicit credit statement gets the long one.
        """
        return self._retry_s if reason == "credit_exhausted" else self._transient_retry_s

    def _advance(self, exhausted_idx, reason="credit_exhausted"):
        now = self._clock()
        with self._lock:
            self._blocked_until[exhausted_idx] = now + self._bench_s(reason)
            self._blocked_reason[exhausted_idx] = reason
            self._last_reason = reason
            self._last_rotation_at = now
            page = (reason == "credit_exhausted" and self._on_credit_exhausted is not None
                    and exhausted_idx not in self._paged)
            if page:
                self._paged.add(exhausted_idx)
            old_active = self._active
            if self._active == exhausted_idx:
                available = [
                    idx for idx in range(exhausted_idx + 1, len(self._urls))
                    if self._blocked_until[idx] is None
                ]
                if available:
                    self._active = available[0]
            new_active = self._active
        if new_active != old_active:
            fallback = new_active == len(self._urls) - 1
            log = _LOG.critical if fallback else _LOG.warning
            log("Solana %s RPC rotated reason=%s from_index=%d "
                "to_index=%d endpoint_count=%d fallback_active=%s",
                self._name, reason, exhausted_idx, new_active, len(self._urls), fallback)
        else:
            _LOG.warning("Solana %s RPC endpoint unavailable reason=%s index=%d "
                         "and NO healthy alternative remains", self._name, reason, exhausted_idx)
        if page:
            try:
                self._on_credit_exhausted(exhausted_idx, key_id(self._urls[exhausted_idx]))
            except Exception:
                _LOG.exception("credit-exhausted page failed index=%d", exhausted_idx)
        return new_active if new_active != exhausted_idx else None

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        if str(url) != self._anchor:
            return self._request(method, url, headers, body, timeout)
        self._refresh_background(headers=headers, timeout=timeout)
        # A WRITE MAY ONLY ROTATE ON -32429, EXACTLY AS BEFORE. A timeout or a 429 on
        # sendTransaction is AMBIGUOUS, not failed: the bytes may already be in flight, and
        # re-sending them to a second endpoint can double-submit a funded trade. -32429 is safe
        # because the provider is stating it did not serve the request at all.
        methods = _body_methods(body)
        rotate_on_unavailable = _rotatable(methods)
        try:
            label = current_label()
            lane = getattr(_LABEL, "lane", None) or "other"
        except Exception:                       # the counter never reaches the call
            label, lane = "?", "other"
        count_exc = None
        attempted = set()
        response = None
        last_exc = None
        while True:
            with self._lock:
                idx = self._active
                if idx not in attempted:
                    self._calls[idx] += 1
                    try:
                        self._count(label, lane, methods)
                    except Exception as exc:    # telemetry: drop the count, never the request
                        self._count_failures += 1
                        count_exc, failures = exc, self._count_failures
            if count_exc is not None:
                # Logged OUTSIDE the lock (no I/O under it), and only at the 1st, 10th, 100th...
                # failure: a broken counter must not turn every funded call into a log write.
                if math.log10(failures) % 1 == 0:
                    _LOG.warning("Solana %s RPC call counter failed; count dropped (failures=%d)",
                                 self._name, failures, exc_info=count_exc)
                count_exc = None
            if idx in attempted:
                # Every endpoint has been tried. Surface the real failure rather than a rotation
                # artefact: if the last attempt RAISED, the caller must see that exception, not a
                # None it would read as a valid empty answer.
                if response is None and last_exc is not None:
                    raise last_exc
                return response
            attempted.add(idx)
            reason = None
            try:
                response = self._request(method, self._urls[idx], headers, body, timeout)
                last_exc = None
            except Exception as exc:
                # urllib RAISES on 4xx/5xx, so a rate limit never reaches the body check below.
                # This is why the pool never rotated: the 429 never got that far.
                if not rotate_on_unavailable:
                    raise                       # write path: unchanged, caller handles ambiguity
                status = _exception_status(exc)
                if status is not None and status not in _UNAVAILABLE_STATUSES:
                    raise                       # a real request error; rotating cannot fix it
                response, last_exc = None, exc
                # A spent plan and a per-second rate limit both arrive as HTTP 429, and they need
                # opposite bench windows. Only the body separates them, so ask it before defaulting
                # to the transient class. See _CREDIT_EXHAUSTED_BODIES.
                if (self._legacy_credit_body and status is not None
                        and _credit_exhausted_body((status, _exception_body(exc)))):
                    reason = "credit_exhausted"
                else:
                    reason = ("http_%d" % status) if status is not None else "transport_error"
            if reason is None:
                # Text from an HTTP error cannot establish pre-admission rejection of signed bytes.
                # Helius plan text is a READ-only legacy hint; Shyft errors retain their own status.
                if _credit_exhausted(response, allow_body=(
                        rotate_on_unavailable and self._legacy_credit_body), request_body=body):
                    reason = "credit_exhausted"
                elif rotate_on_unavailable and _status_unavailable(response):
                    reason = "http_%s" % response[0]
                else:
                    return response
            next_idx = self._advance(idx, reason)
            if next_idx is None:
                if response is None and last_exc is not None:
                    raise last_exc
                return response

    def _count(self, label, lane, methods):
        """One request: each member method under `label` (so a batch counts once per member), and
        one request in the minute ring under `lane`. Caller holds the lock."""
        for method in (methods or ("?",)):
            key = (label, method or "?")
            if key not in self._by_label and len(self._by_label) >= COUNTER_MAX_KEYS:
                key = ("other", "other")
            self._by_label[key] = self._by_label.get(key, 0) + 1
        minute = int(self._wall() // 60) * 60
        if not self._minutes or self._minutes[-1][0] != minute:
            self._minutes.append((minute, {}))
        bucket = self._minutes[-1][1]
        bucket[lane] = bucket.get(lane, 0) + 1

    def snapshot(self):
        """Pool state for the heartbeat.

        `degraded` and `last_reason` exist because the old snapshot could not answer "are we on
        the fastest path". It reported active_index=0 exhausted_count=0 all day while the runner
        logged 1,812 HTTP 429s — true, and useless, because rotation was unreachable so the pool
        genuinely never left index 0. Now that it can move, the heartbeat has to SAY so: degraded
        means we are not on the primary endpoint, which is the question an operator is actually
        asking.
        """
        with self._lock:
            blocked = [i for i, until in enumerate(self._blocked_until) if until is not None]
            return {
                "active_index": self._active,
                "endpoint_count": len(self._urls),
                "exhausted_count": len(blocked),
                "fallback_active": self._active == len(self._urls) - 1,
                "degraded": self._active != 0,
                "blocked_indexes": blocked,
                "last_reason": self._last_reason,
                "last_rotation_at": self._last_rotation_at,
                "calls": _nest(self._by_label),
                "minutes": [[minute, dict(counts)] for minute, counts in self._minutes],
                "count_failures": self._count_failures,
            }

    def key_status(self):
        """Per-endpoint state for a heartbeat: key id (never the URL), state, bench left, reason,
        calls since start. `unknown` = never called; nothing here asks the provider for usage."""
        now = self._clock()
        with self._lock:
            rows = []
            for idx, url in enumerate(self._urls):
                until, reason = self._blocked_until[idx], self._blocked_reason[idx]
                if until is not None:
                    state = {"credit_exhausted": "credit_exhausted",
                             "http_429": "rate_limited"}.get(reason, "unavailable")
                else:
                    state = "healthy" if self._calls[idx] else "unknown"
                rows.append({
                    "key": key_id(url), "state": state,
                    "bench_left_s": (None if until is None else round(max(0.0, until - now), 1)),
                    "last_reason": reason, "calls": self._calls[idx],
                })
            return rows
