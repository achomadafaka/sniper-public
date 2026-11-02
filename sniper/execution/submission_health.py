"""Process-local health + quarantine state for the free official endpoints.

Health is OBSERVABILITY, never execution authority. This registry:

- never signs or submits, and never performs a network call from ``should_attempt`` (an action reads
  the last recorded snapshot; it never waits for a check);
- is fully rebuilt after a process restart (all state is in-memory);
- quarantines a free endpoint only after ``FREE_HEALTH_FAIL_THRESHOLD`` consecutive failures, for
  ``FREE_HEALTH_QUARANTINE_S`` seconds; one success ends quarantine and clears the streak;
- treats a wrong-chain identity as a HARD, restart-scoped exclusion (high severity — the caller
  alerts): a later success can never un-exclude it;
- leaves submission-only endpoints (health_method='none', e.g. the Robinhood sequencer) permanently
  attemptable — they receive no health records and are validated only by the chain receipt.

``sanitized_snapshot`` exposes labels + states + counts + last-health age only — never a URL,
identity value, or raw error string.
"""
from __future__ import annotations

import json as _json
import threading as _threading
import time as _time

from sniper.execution.submission_transport import AttemptState, SubmissionAttempt
from sniper.settings import memecoin_submission as _cfg


class HealthRegistry:
    def __init__(self, provider_ids=(), *, health_interval_s=None, fail_threshold=None,
                 quarantine_s=None, clock=None):
        self._clock = clock or _time.monotonic
        self.health_interval_s = float(
            _cfg.FREE_HEALTH_INTERVAL_S if health_interval_s is None else health_interval_s)
        self._fail_threshold = int(
            _cfg.FREE_HEALTH_FAIL_THRESHOLD if fail_threshold is None else fail_threshold)
        self._quarantine_s = float(
            _cfg.FREE_HEALTH_QUARANTINE_S if quarantine_s is None else quarantine_s)
        self._lock = _threading.RLock()
        self._p = {}
        for pid in provider_ids or ():
            self._ensure(pid)

    def _ensure(self, provider_id):
        st = self._p.get(provider_id)
        if st is None:
            st = {"state": "unverified", "ever_verified": False,
                  "consecutive_failures": 0, "quarantine_until": 0.0,
                  "last_health": None, "ok": 0, "fail": 0, "wrong_chain": 0}
            self._p[provider_id] = st
        return st

    # --- pure reads (no network, no clock advance) --------------------------------------------
    def should_attempt(self, provider_id, now=None) -> bool:
        with self._lock:
            st = self._p.get(provider_id)
            if st is None:
                return True                   # health_method='none' / unregistered -> attemptable
            if st["state"] in ("unverified", "excluded"):
                return False
            if st["state"] == "quarantined":
                now = self._clock() if now is None else now
                # A once-verified endpoint may be retried after its bounded quarantine. A provider
                # that has NEVER proved its identity remains gated until a successful health probe.
                return bool(st["ever_verified"] and now >= st["quarantine_until"])
            return True

    # --- health records (fed by the separate, never-signing checker) --------------------------
    def record_success(self, provider_id, identity, now=None):
        with self._lock:
            st = self._ensure(provider_id)
            if st["state"] == "excluded":
                # a success can never un-exclude a wrong-chain endpoint; still note it was reached.
                st["last_health"] = self._clock() if now is None else now
                return
            st["state"] = "active"
            st["ever_verified"] = True
            st["consecutive_failures"] = 0
            st["quarantine_until"] = 0.0
            st["ok"] += 1
            st["last_health"] = self._clock() if now is None else now

    def record_failure(self, provider_id, reason, now=None):
        with self._lock:
            st = self._ensure(provider_id)
            st["fail"] += 1
            st["last_health"] = self._clock() if now is None else now
            if st["state"] == "excluded":
                return
            st["consecutive_failures"] += 1
            if st["consecutive_failures"] >= self._fail_threshold:
                st["state"] = "quarantined"
                st["quarantine_until"] = (
                    self._clock() if now is None else now) + self._quarantine_s

    def record_wrong_chain(self, provider_id, received, now=None):
        with self._lock:
            st = self._ensure(provider_id)
            st["state"] = "excluded"
            st["wrong_chain"] += 1
            st["last_health"] = self._clock() if now is None else now

    def sanitized_snapshot(self, now=None) -> dict:
        with self._lock:
            now = self._clock() if now is None else now
            out = {}
            for pid, st in self._p.items():
                last = st["last_health"]
                out[pid] = {
                    "state": st["state"],
                    "consecutive_failures": st["consecutive_failures"],
                    "ok": st["ok"],
                    "fail": st["fail"],
                    "wrong_chain": st["wrong_chain"],
                    "last_health_age_s": (None if last is None else round(now - last, 3)),
                }
            return out


class HealthGatedProvider:
    """Wrap a delivery provider so an excluded/quarantined endpoint returns ``NOT_SENT`` WITHOUT the
    inner provider ever seeing the request — zero bytes cross a quarantined boundary. A healthy
    endpoint delegates unchanged; the inner provider still owns the accepted/ambiguous/explicit
    outcome. Never signs or mutates the request."""

    def __init__(self, inner, health, *, provider_id=None):
        self._inner = inner
        self._health = health
        self.provider_id = provider_id or inner.provider_id

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def submit(self, request):
        if not self._health.should_attempt(self.provider_id):
            return SubmissionAttempt(
                self.provider_id, AttemptState.NOT_SENT, None, 0.0, None, "health_gated")
        return self._inner.submit(request)


def probe_evm_identity(http, endpoint, *, timeout_s=None):
    """Read-only ``eth_chainId`` identity probe. Never signs or submits. Returns one of:
    ``("ok", chain_id)`` / ``("wrong_chain", received)`` / ``("fail", reason)`` /
    ``("skip", "")`` (endpoints with ``health_method='none'`` — submission-only, no read identity)."""
    if endpoint.health_method == "none":
        return ("skip", "")
    body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []})
    kwargs = {"body": body}
    if timeout_s is not None:
        kwargs["timeout"] = timeout_s
    try:
        status, payload, _date = http("POST", endpoint.url, **kwargs)
    except Exception:
        return ("fail", "transport")
    if status != 200 or not isinstance(payload, dict):
        return ("fail", "http_%s" % status)
    result = payload.get("result")
    if not result:
        return ("fail", "no_result")
    if str(result).lower() == str(endpoint.expected_identity).lower():
        return ("ok", str(result))
    return ("wrong_chain", str(result))


def probe_solana_identity(http, endpoint, *, timeout_s=None):
    """Read-only Solana identity probe: ``getHealth`` must answer and ``getGenesisHash`` must equal the
    pinned mainnet-beta genesis. Never signs or submits. Same tri/quad verdict shape as the EVM probe;
    a genesis mismatch is ``wrong_chain`` (hard exclude), any transport/parse issue is ``fail``."""
    if endpoint.health_method == "none":
        return ("skip", "")
    kwargs_common = {} if timeout_s is None else {"timeout": timeout_s}
    try:
        hs, hp, _ = http("POST", endpoint.url, body=_json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "getHealth"}), **kwargs_common)
        if hs != 200 or not isinstance(hp, dict) or hp.get("result") != "ok":
            return ("fail", "unhealthy")
        gs, gp, _ = http("POST", endpoint.url, body=_json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "getGenesisHash"}), **kwargs_common)
    except Exception:
        return ("fail", "transport")
    if gs != 200 or not isinstance(gp, dict):
        return ("fail", "http_%s" % gs)
    genesis = gp.get("result")
    if not genesis:
        return ("fail", "no_genesis")
    if str(genesis) == str(endpoint.expected_identity):
        return ("ok", str(genesis))
    return ("wrong_chain", str(genesis))


def apply_probe(health, endpoint, verdict, *, now=None):
    """Feed a probe verdict into the registry. ``wrong_chain`` -> hard, restart-scoped exclusion (high
    severity — the caller alerts); ``ok`` -> success; ``fail`` -> a single failure (the runtime loop
    re-checks and quarantines only on a persistent streak); ``skip`` -> no-op (always attemptable)."""
    kind, value = verdict
    if kind == "ok":
        health.record_success(endpoint.provider_id, value, now=now)
    elif kind == "wrong_chain":
        health.record_wrong_chain(endpoint.provider_id, value, now=now)
    elif kind == "fail":
        health.record_failure(endpoint.provider_id, value, now=now)
    return kind
