"""Solana slot -> time backfill, on the FREE public endpoint (unlock step 1 of the brief §5).

The exit brain is forced to legacy on Solana because its slots are absent from the EVM-only
`block_times` map, so nothing can be windowed on true event time (ingest-time windows are banned by
the design spec §3.5). `getBlockTime(slot)` closes that gap and costs nothing.

STRICTLY READ-ONLY. It uses `solana_read_rpc()`, never `MEMECOIN_SOLANA_RPC_URL` — the funded
keypair already exists on this host, so touching that variable would arm the real Solana leg. This
module has no access to custody, signing or execution.

Bounded and cached exactly like the EVM block-time worker: newest slots first (the fresh window is
what the brain needs; an ancient slot the node no longer serves must never starve it), capped per
pass, and written once. Fail-closed: a slot the node cannot answer is left absent, never guessed.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request

from sniper.persistence import db_ticks as t

logger = logging.getLogger("memecoin")

CHAIN = "solana"


def _rpc_call(url, method, params, *, timeout=10):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 Sniper/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()).get("result")


def build_read_rpc_call(url=None, *, min_call_s=1.2, max_429_retries=1,
                        retry_backoff_s=3.0, clock=time.monotonic, sleep=time.sleep,
                        raw_call=None):
    """One paced/retrying JSON-RPC transport shared by every Solana read method.

    Pacing below the method adapters is load-bearing: those adapters intentionally translate
    transport errors to `None`, so wrapping them cannot observe a 429 and cannot retry it.
    """
    try:
        cadence = float(min_call_s)
        backoff = float(retry_backoff_s)
        retries = int(max_429_retries)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Solana read RPC pacing configuration") from exc
    if not math.isfinite(cadence) or cadence <= 0:
        raise ValueError("min_call_s must be finite and > 0")
    if not math.isfinite(backoff) or backoff < 0 or retries < 0:
        raise ValueError("retry pacing must be finite and non-negative")
    if raw_call is None:
        from sniper.settings.memecoin import solana_read_rpc
        url = url or solana_read_rpc()
        if not url:
            return None

        def raw_call(method, params):
            return _rpc_call(url, method, params, timeout=15)

    lock = threading.Lock()
    state = {"last": None, "blocked_until": 0.0}

    def call(method, params):
        for attempt in range(retries + 1):
            with lock:
                now = clock()
                target = state["blocked_until"]
                if state["last"] is not None:
                    target = max(target, state["last"] + cadence)
                if target > now:
                    sleep(target - now)
                state["last"] = clock()
            try:
                # The lock spaces request starts; it must not serialize network latency. This lets
                # the V6 recovery lane overlap bounded read-only requests without exceeding the
                # configured starts-per-second cadence.
                return raw_call(method, params)
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt >= retries:
                    raise
                with lock:
                    state["blocked_until"] = max(state["blocked_until"], clock() + backoff)
        raise AssertionError("unreachable")

    return call


def get_block_time_fn(url=None, *, call=None):
    """Build `slot -> unix_seconds | None`. `call` is injected in tests so no network is touched."""
    if call is None:
        from sniper.settings.memecoin import solana_read_rpc
        url = url or solana_read_rpc()
        if not url:
            return None

        def call(method, params):
            return _rpc_call(url, method, params)

    def get_block_time(slot):
        try:
            ts = call("getBlockTime", [int(slot)])
        except Exception:
            logger.exception("solana getBlockTime failed slot=%s", slot)
            return None
        try:
            return int(ts) if ts is not None else None
        except (TypeError, ValueError):
            return None

    return get_block_time


def missing_slots(tc, *, limit=64):
    """Slots present in the Solana tape with no `block_times` row yet, NEWEST FIRST and bounded.

    Newest-first is load-bearing for the same reason as the EVM worker: the fresh window is what the
    brain needs, and a pruned ancient slot the node no longer serves must never consume the budget
    every pass while newer, answerable slots wait behind it.
    """
    slots = [r[0] for r in tc.execute(
        "SELECT DISTINCT block_no FROM pool_ticks WHERE chain_id=? AND block_no IS NOT NULL "
        "ORDER BY block_no DESC LIMIT ?", (CHAIN, int(limit)))]
    if not slots:
        return []
    have = t.block_ts_map(tc, CHAIN, slots)
    return [s for s in slots if s not in have]


def backfill_solana_block_times(tc, now_iso, *, cap=64, get_block_time=None):
    """Fill up to `cap` missing slot timestamps. Returns rows written. Never raises."""
    try:
        t.ensure_block_times(tc)
        fn = get_block_time or get_block_time_fn()
        if fn is None:
            return 0                      # no read endpoint configured -> nothing, never a guess
        slots = missing_slots(tc, limit=cap)
        if not slots:
            return 0
        return t.backfill_block_times(tc, CHAIN, slots, fn, now_iso)
    except Exception:
        logger.exception("solana block-time backfill failed")
        return 0
