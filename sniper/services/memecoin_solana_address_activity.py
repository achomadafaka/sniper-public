"""Solana hourly floor, S2: one page of mint-address signatures as a capped ACTIVITY count.

Spec: docs/superpowers/specs/2026-09-17-solana-hourly-floor-activity-design.md (operator approved
2026-09-17). READ-ONLY provider access, separate from the pure volume evaluator.

The count is ADDRESS ACTIVITY, never dollars. Solana's signature method returns transactions that
reference the address, newest first, including failed ones, and one page holds at most 1,000. A page
can therefore prove `count >= K`; it can never prove the full hourly count, and it is never
multiplied into an estimate. It does not classify swaps, and a mint query does not cover every venue.

Controls: one call per probe, no pagination, no transaction fetch, a 600 ms monotonic TOTAL deadline
(worker admission, connection, read and parse), one request at a time, one request per second with
no wait for a token, a 120 s cooldown after a 429, no retry and no endpoint rotation. A response that
arrives after the deadline changes nothing: the caller already has `unknown`. A timed-out request
keeps the only slot until it ends. The endpoint is a dedicated research quota; invalid configuration
disables the probe and never stops the runner.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass

from sniper.persistence.db_memecoin import event_transaction_signature as source_signature

METHOD_VERSION = "mint_signatures_one_page_v1"
ADDRESS_TYPE = "mint"
PAGE_LIMIT = 1000
WINDOW_S = 3600
DEADLINE_S = 0.6
MIN_INTERVAL_S = 1.0
COOLDOWN_S = 120.0
PUBLIC_HOSTS = ("api.mainnet-beta.solana.com",)
URL_VAR = "MEMECOIN_SOL_H1_ACTIVITY_RPC_URL"

_SIGNATURE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,90}$")



def count_page(page, *, decision_ts, source_signature, window_s=WINDOW_S):
    """Count one returned page. PURE: the live probe and the calibration fixtures both use this.

    A signature counts once only when its signature and slot are valid, `err` exists and is null,
    the status is confirmed or finalized, `blockTime` is an integer in [T - window, T), and it is not
    the source. A failed transaction or a missing timestamp adds no count.
    """
    counts = {"returned_count": 0, "valid_signature_count": 0, "failed_count": 0,
              "missing_time_count": 0, "out_of_window_count": 0, "invalid_count": 0,
              "excluded_source_count": 0, "page_saturated": False, "oldest_block_time": None}
    rows = page if isinstance(page, list) else []
    counts["returned_count"] = len(rows)
    counts["page_saturated"] = len(rows) >= PAGE_LIMIT
    seen = set()
    start, end = int(decision_ts) - int(window_s), int(decision_ts)
    for row in rows:
        if not isinstance(row, dict):
            counts["invalid_count"] += 1
            continue
        signature, slot = row.get("signature"), row.get("slot")
        if (not isinstance(signature, str) or not _SIGNATURE.match(signature)
                or not isinstance(slot, int) or isinstance(slot, bool) or slot < 0
                or "err" not in row or signature in seen):
            counts["invalid_count"] += 1
            continue
        seen.add(signature)
        if row["err"] is not None:
            counts["failed_count"] += 1
            continue
        if row.get("confirmationStatus") not in ("confirmed", "finalized"):
            counts["invalid_count"] += 1
            continue
        block_time = row.get("blockTime")
        if not isinstance(block_time, int) or isinstance(block_time, bool):
            counts["missing_time_count"] += 1
            continue
        oldest = counts["oldest_block_time"]
        counts["oldest_block_time"] = block_time if oldest is None else min(oldest, block_time)
        if not start <= block_time < end:
            counts["out_of_window_count"] += 1
            continue
        if signature == source_signature:
            counts["excluded_source_count"] += 1
            continue
        counts["valid_signature_count"] += 1
    return counts


@dataclass(frozen=True)
class ProbeConfig:
    url: str | None
    cutoff: int | None
    disabled_reason: str | None

    @property
    def enabled(self):
        return self.disabled_reason is None


def load_config(url_raw, cutoff_raw, env):
    """Validate the endpoint and the cutoff. Never raises; a reason never contains a URL."""
    url = str(url_raw or "").strip()
    if not url:
        return ProbeConfig(None, None, "no_endpoint")
    if any(host in url for host in PUBLIC_HOSTS):
        return ProbeConfig(None, None, "public_endpoint")
    for name in sorted(env or {}):
        if name == URL_VAR or "RPC" not in str(name).upper():
            continue
        others = {u for u in str(env.get(name) or "").replace(",", " ").split() if u}
        if url in others:
            return ProbeConfig(None, None, "shared_endpoint:%s" % name)
    raw = str(cutoff_raw or "").strip()
    if not raw:
        return ProbeConfig(url, None, None)
    if not raw.isdigit() or not 1 <= int(raw) <= PAGE_LIMIT:
        return ProbeConfig(None, None, "invalid_cutoff")
    return ProbeConfig(url, int(raw), None)


class _RateLimited(Exception):
    pass


def _post_json(url, body, timeout):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json",
                                              "accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return exc.code, {}


class ActivityProbe:
    """One capped activity read per call, with the spec's budget controls."""

    def __init__(self, url, cutoff, *, post=None, clock=None, deadline_s=DEADLINE_S,
                 min_interval_s=MIN_INTERVAL_S, cooldown_s=COOLDOWN_S):
        self._url = url
        self._cutoff = cutoff
        self._post = post or _post_json
        self._clock = clock or time.monotonic
        self._deadline_s = float(deadline_s)
        self._min_interval_s = float(min_interval_s)
        self._cooldown_s = float(cooldown_s)
        self._lock = threading.Lock()
        self._inflight = False
        self._last_start = None
        self._cooldown_until = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sol-h1-activity")

    def _base(self, source_signature_value):
        return {"method_version": METHOD_VERSION, "address_type": ADDRESS_TYPE,
                "anchor": source_signature_value, "cutoff": self._cutoff, "call_count": 0,
                "elapsed_ms": 0.0, "result_type": "unknown", "exception_used": False,
                "budget_state": None, "valid_signature_count": None, "failed_count": None,
                "missing_time_count": None, "page_saturated": None, "oldest_block_time": None,
                "returned_count": None}

    def _release(self, _future):
        with self._lock:
            self._inflight = False

    def _fetch(self, mint, anchor, decision_ts):
        body = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                "params": [mint, {"limit": PAGE_LIMIT, "commitment": "confirmed", "before": anchor}]}
        status, payload = self._post(self._url, body, self._deadline_s)
        code = ((payload or {}).get("error") or {}).get("code") if isinstance(payload, dict) else None
        if status == 429 or code in (429, -32429):
            with self._lock:
                self._cooldown_until = self._clock() + self._cooldown_s
            raise _RateLimited()
        if status != 200 or not isinstance(payload, dict) or payload.get("error") is not None:
            raise ValueError("status %s" % status)
        page = payload.get("result")
        if not isinstance(page, list):
            raise ValueError("malformed page")
        return count_page(page, decision_ts=decision_ts, source_signature=anchor)

    def probe(self, mint, *, decision_ts, source_signature):
        started = self._clock()
        out = self._base(source_signature)

        def finish(**fields):
            out.update(fields)
            out["elapsed_ms"] = round((self._clock() - started) * 1000.0, 1)
            return out

        if not source_signature or not mint:
            return finish(budget_state="no_source_signature")
        with self._lock:
            now = self._clock()
            if self._cooldown_until is not None and now < self._cooldown_until:
                return finish(budget_state="cooldown")
            if self._inflight:
                return finish(budget_state="busy")
            if self._last_start is not None and now - self._last_start < self._min_interval_s:
                return finish(budget_state="rate_limited")
            self._inflight = True
            self._last_start = now
        out["call_count"] = 1
        try:
            future = self._executor.submit(self._fetch, mint, source_signature, decision_ts)
        except Exception:
            with self._lock:
                self._inflight = False
            return finish(budget_state="error")
        future.add_done_callback(self._release)
        remaining = self._deadline_s - (self._clock() - started)
        try:
            counts = future.result(timeout=max(0.0, remaining))
        except FutureTimeout:
            return finish(budget_state="timeout")
        except _RateLimited:
            return finish(budget_state="rate_limit_429")
        except Exception:
            return finish(budget_state="error")
        if self._clock() - started > self._deadline_s:
            return finish(budget_state="timeout")
        out.update(counts)
        if self._cutoff is None:
            return finish(budget_state="ok", result_type="no_cutoff")
        if counts["valid_signature_count"] >= self._cutoff:
            return finish(budget_state="ok", result_type="exception", exception_used=True)
        return finish(budget_state="ok", result_type="below_cutoff")


_PROBE = {"built": False, "probe": None, "disabled_reason": None}


def runtime_probe():
    """The process probe from settings, built once. (None, reason) when disabled."""
    if not _PROBE["built"]:
        import os
        from sniper.settings import memecoin as cfg
        cutoff = getattr(cfg, "MEMECOIN_SOL_H1_ACTIVE_SIGS", "")
        if cfg.MEMECOIN_FUNDED_ONLY and not str(cutoff or "").strip():
            _PROBE.update(built=True, probe=None, disabled_reason="funded_only_evidence_disabled")
            return None, _PROBE["disabled_reason"]
        if cfg.MEMECOIN_SOLANA_INFRA_PROVIDER == "shyft":
            # Explicit funded cutoff only; validate/freeze neutral read once at construction.
            url = cfg.solana_read_rpc()
            config = load_config(url, cutoff, {URL_VAR: url})
        else:
            config = load_config(getattr(cfg, "MEMECOIN_SOL_H1_ACTIVITY_RPC_URL", ""), cutoff, os.environ)
        _PROBE.update(built=True, disabled_reason=config.disabled_reason,
                      probe=ActivityProbe(config.url, config.cutoff) if config.enabled else None)
    return _PROBE["probe"], _PROBE["disabled_reason"]
