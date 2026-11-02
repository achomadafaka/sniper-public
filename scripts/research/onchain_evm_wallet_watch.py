#!/usr/bin/env python3
"""Watch armed wallets on one EVM chain on-chain before the Fomo feed publishes a buy.

One process serves one chain. `--chain` selects it (default 4663) and every chain-scoped
name -- quote token, heartbeat file, RPC override, pool manager, socket -- follows from it,
so two chains can run side by side without either seeing the other's state.
"""
from __future__ import annotations

import argparse
import functools
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE_ROOT = Path(os.getenv("SNIPER_STATE_ROOT") or REPO)
sys.path.insert(0, str(REPO))
if __name__ == "__main__":
    # One write() per print() (sniper/util/line_print.py): before any other sniper import.
    from sniper.util.line_print import install
    install()

from sniper.util.envfiles import load_env_files
load_env_files(STATE_ROOT)

from sniper.execution.evm_wallet import (
    CHAIN_QUOTE, erc20_decimals, quote_usd_price, _rpc, start_rpc_tap, take_rpc_tap,
)
from sniper.persistence import db_memecoin as m
from sniper.persistence.db_core import get_connection, get_readonly_connection
from sniper.services.axiom_address_verify import evm_watched_owners
from sniper.services.memecoin_evm_onchain_watch import (
    TRANSFER_TOPIC, direct_buy_from_receipt, _topic_address, relay_code_verified,
    relay_envelope_chains, sell_candidate, wallet_sales_from_receipt,
)
from sniper.services.memecoin_onchain_wake import default_address, send_wake
from sniper.services import memecoin_base_preconf as P
from sniper.services.memecoin_source_fill import source_fill_from_buy
from sniper.settings.memecoin import evm_rpc_for_chain
from sniper.util.receipt_clock import ReceiptClock, elapsed_receipt_ms

CHAIN = "4663"
WETH = CHAIN_QUOTE[CHAIN]["token"].lower()
# The chains whose Relay envelope buys this watcher decodes (MEMECOIN_EVM_RELAY_ENVELOPE_CHAINS),
# read once at start; the decoder rechecks the same knob per call.
RELAY_ENVELOPE_CHAINS = relay_envelope_chains()


def _configure(chain):
    """Point this process at one chain. Called once, before any lane runs."""
    global CHAIN, WETH, HEARTBEAT
    chain = str(chain).strip()
    if chain not in CHAIN_QUOTE:
        raise SystemExit("unknown chain %r: no CHAIN_QUOTE entry" % chain)
    CHAIN = chain
    WETH = CHAIN_QUOTE[chain]["token"].lower()
    # PER CHAIN, and 4663 KEEPS ITS EXISTING PATH. A shared heartbeat would let a healthy chain
    # hide a dead one, which is the failure this file's own history is full of.
    HEARTBEAT = STATE_ROOT / "data" / (
        "onchain_evm_watch.heartbeat" if chain == "4663"
        else "onchain_evm_watch.%s.heartbeat" % chain)
DB = Path(os.getenv("DB_PATH") or STATE_ROOT / "sniper_tracker.db")
HEARTBEAT = STATE_ROOT / "data" / "onchain_evm_watch.heartbeat"
ADDRESS_REFRESH_S = 6 * 3600
ADDRESS_RESOLVE_PERIOD_S = 60.0
ADDRESS_FAILURE_RETRY_S = 15 * 60.0

# Alchemy's free tier caps eth_getLogs at 10 blocks INCLUSIVE. Measured 2026-09-03: a span of 9
# (10 blocks) returns in ~121ms; a span of 10 (11 blocks) returns HTTP 400 -32600. This script
# asked for 100 and had therefore NEVER completed a single sweep -- zero rows in
# onchain_wallet_detections, on a lane that trades Solana fine.
LOGS_MAX_SPAN = int(os.getenv("MEMECOIN_ONCHAIN_EVM_LOGS_SPAN", "9"))
# 4663 produces a block every ~0.10s, so one 10-block call covers ~1.0s of chain against a 1.0s
# poll: break-even with no margin, which is how it ended up 62,201 blocks behind. When behind,
# spend up to this many calls per tick to catch up; when current, one call per tick.
CATCHUP_CALLS_PER_TICK = int(os.getenv("MEMECOIN_ONCHAIN_EVM_CATCHUP_CALLS", "8"))
# Never backfill further than the consuming lane will accept. MEMECOIN_ONCHAIN_LANE_MAX_AGE_S is
# 45s and ~0.10s blocks make that ~450 blocks, so an older detection is rejected downstream
# anyway. Skipping is the CORRECT loss: a 1.7-hour-old buy is not tradeable, and grinding through
# it would keep the watcher permanently blind to the present.
MAX_BEHIND_BLOCKS = int(os.getenv("MEMECOIN_ONCHAIN_EVM_MAX_BEHIND_BLOCKS", "450"))


# RPC LOAD ON THE HTTP HOST, IN THE HEARTBEAT (2026-09-28). MEMECOIN_ONCHAIN_EVM_RPC_URL_4663 is the
# chain-owner endpoint the funded path also broadcasts through, and a dark socket moves this watcher
# onto range getLogs against it. Every JSON-RPC request this module sends goes through
# `_counted_rpc`, so Gamma can read from the beat whether a fallback ever loads that host.
# `erc20_decimals` (one read per new mint) is not counted.
_RPC_SENT = deque()
_RPC_TOTAL = [0]
_RPC_LOCK = threading.Lock()     # a detection's receipt read runs on its own thread (_read_pair)


def _rpc_load():
    now = time.monotonic()
    with _RPC_LOCK:
        while _RPC_SENT and now - _RPC_SENT[0] > 60.0:
            _RPC_SENT.popleft()
        return {"rpc_requests_60s": len(_RPC_SENT), "rpc_requests_total": _RPC_TOTAL[0]}


def _counted_rpc(url, method, params, *, read_transport=None):
    with _RPC_LOCK:
        _RPC_TOTAL[0] += 1
        _RPC_SENT.append(time.monotonic())
    call = read_transport.rpc if read_transport is not None else _rpc
    return call(url, method, params)


def _relay_warm_interval(chain, ttl_s):
    return 30. if chain == '56' or not math.isfinite(ttl_s) or ttl_s <= 0 else max(30., ttl_s / 3.)


class _WatcherReads:
    """One startup-owned HTTP pool for watcher reads; each request keeps its own headers/body."""

    def __init__(self, *, client=None):
        import inspect
        from sniper.execution.fast_http import PooledHttpClient, warm_keepalive_expiry
        from sniper.settings import memecoin as cfg
        try:
            ttl = float(getattr(cfg, 'MEMECOIN_ONCHAIN_RELAY_CODE_TTL_S', 0.) or 0.)
        except (TypeError, ValueError):
            ttl = 0.
        expiry = (warm_keepalive_expiry(_relay_warm_interval(CHAIN, ttl), 10., serial_calls=4)
                  if CHAIN in RELAY_ENVELOPE_CHAINS and math.isfinite(ttl) and ttl > 0 else 30.)
        self.client = client if client is not None else PooledHttpClient(
            max_connections=32, max_keepalive=16, connect_timeout_s=10., read_timeout_s=10.,
            write_timeout_s=10., pool_timeout_s=0., keepalive_expiry_s=expiry)
        def bind(fn, keyword, value):
            # Legacy injectable seams can take only positional arguments. Bind once at startup,
            # never construct a client or swap a module global around an individual request.
            parameters = inspect.signature(fn).parameters.values()
            supported = any(p.name == keyword and p.kind != p.POSITIONAL_ONLY
                            or p.kind == p.VAR_KEYWORD for p in parameters)
            return functools.partial(fn, **{keyword: value}) if supported else fn
        self.rpc = bind(_rpc, 'http', self.http)
        self.decimals = bind(erc20_decimals, 'http', self.http)
        self.counted_rpc = bind(_counted_rpc, 'read_transport', self)
        self.result = bind(_rpc_result, 'read_transport', self)
        self.tx_host = bind(tx_read_host_from_env, 'read_transport', self)

    def http(self, method, url, *, headers=None, body=None, timeout=None):
        from sniper.execution.evm_wallet import _UA
        from sniper.execution.fast_http import FastHttpError
        request_headers = {"user-agent": _UA, "accept": "application/json", "content-type": "application/json"}
        request_headers.update({key.lower(): value for key, value in (headers or {}).items()})
        try:
            status, answer, response_headers = self.client.request_with_headers(
                method, url, headers=request_headers, body=body, timeout=timeout)
        except FastHttpError as exc:
            from httpx import PoolTimeout
            if isinstance(exc.__context__, PoolTimeout):
                from sniper.execution.evm_rpc_pool import RequestNotSent
                raise RequestNotSent("watcher read pool capacity") from None
            raise
        if status >= 400 and not (isinstance(answer, dict) and 'error' in answer and 'jsonrpc' in answer):
            import io
            raw = answer.encode() if isinstance(answer, str) else json.dumps(answer).encode()
            raise urllib.error.HTTPError(url, status, "HTTP read refused", response_headers, io.BytesIO(raw))
        if isinstance(answer, str):
            answer = json.loads(answer) if answer else {}
        return status, answer, response_headers

    def post(self, url, body, timeout):
        """TxReadHost retains its curl agent, explicit timeout and HTTP-error classification."""
        from httpx import Timeout
        try:
            status, answer, _headers = self.http('POST', url, body=json.dumps(body), timeout=Timeout(timeout, pool=0.),
                headers={"Content-Type": "application/json", "User-Agent": "curl/8.5.0"})
            return status, answer
        except urllib.error.HTTPError as exc:
            return exc.code, None

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _pooled_watcher_reads(fn):
    """Own before startup work; nested poll fallback borrows the push owner's pool."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        if kwargs.get('read_transport') is not None:
            return fn(*args, **kwargs)
        with _WatcherReads() as reads:
            return fn(*args, **dict(kwargs, read_transport=reads))
    return run


class RpcErrorBody(RuntimeError):
    """A JSON-RPC reply that carries no result: a failed read, never a null and never KeyError."""


def _rpc_result(url, method, params, *, read_transport=None):
    """The result of one counted call, or RpcErrorBody naming the provider's error (2026-10-07,
    8453: four delivered transfers died in KeyError('result') on an error body, unnamed)."""
    call = read_transport.counted_rpc if read_transport is not None else _counted_rpc
    reply = call(url, method, params)
    if not isinstance(reply, dict) or reply.get("error") is not None or "result" not in reply:
        error = reply.get("error") if isinstance(reply, dict) else reply
        raise RpcErrorBody("%s: %s" % (method, json.dumps(error, default=str)[:200]))
    return reply["result"]


_RECEIPT_READER = []


def _reader():
    if not _RECEIPT_READER:
        from concurrent.futures import ThreadPoolExecutor
        _RECEIPT_READER.append(ThreadPoolExecutor(max_workers=2, thread_name_prefix="evm-receipt"))
    return _RECEIPT_READER[0]


# RELAY DECODE CAPTURE (2026-10-02, Alpha yes). 16 J7 BSC buys by watched wallets since 09-28 were
# delivered and refused unsupported_relayer_swap, and no allowed 56 host still serves their receipts
# (the research dRPC node keeps block bodies ~1.5-3 h). So a refused Relay envelope's tx and receipt,
# exactly as the detection already holds them, go to a per-chain JSONL for the decoder's fixtures.
# The detection thread only queues (put_nowait, never blocks, no read); ONE writer thread appends.
# Two parts a chain (the file and its .1), each rotated at RELAY_CAPTURE_PART_BYTES: at most 16 MB a
# chain plus one record, under 50 MB across 56, 4663 and 8453. Nothing reads these files.
RELAY_CAPTURE_PART_BYTES = 8 * 1024 * 1024
RELAY_CAPTURE_QUEUE_MAX = 64
_CAPTURE = []                    # [queue.Queue], started once
_CAPTURE_LOCK = threading.Lock()
_CAPTURE_SEEN = deque(maxlen=512)
_CAPTURE_STATS = {"queued": 0, "written": 0, "duplicate": 0, "dropped_full": 0, "write_failed": 0,
                  "rotated": 0}


def relay_capture_path(chain=None):
    return STATE_ROOT / "data" / ("onchain_evm_relay_unsupported.%s.jsonl" % (chain or CHAIN))


def _capture_count(key):
    with _CAPTURE_LOCK:
        _CAPTURE_STATS[key] += 1


def _capture_queue(start=True):
    import queue
    with _CAPTURE_LOCK:
        if not _CAPTURE:
            _CAPTURE.append(queue.Queue(maxsize=RELAY_CAPTURE_QUEUE_MAX))
            if start:
                threading.Thread(target=_capture_run, args=(_CAPTURE[0],), daemon=True,
                                 name="evm-relay-capture").start()
        return _CAPTURE[0]


def capture_unsupported_relay(tx, receipt, owners, *, arrival=None, chain=None):
    """Queue one refused Relay envelope for the capture writer. Never blocks, reads or raises."""
    import queue
    try:
        tx_hash = str((tx or {}).get("hash") or "").lower()
        with _CAPTURE_LOCK:
            if tx_hash in _CAPTURE_SEEN:
                _CAPTURE_STATS["duplicate"] += 1
                return False
            _CAPTURE_SEEN.append(tx_hash)
        record = {"chain": str(chain or CHAIN), "tx_hash": tx_hash, "watched": sorted(owners or ()),
                  "arrival": str(arrival) if arrival is not None else None,
                  "captured_at": datetime.now(timezone.utc).isoformat(),
                  "transaction": tx, "receipt": receipt}
        _capture_queue().put_nowait((relay_capture_path(chain), record))
        _capture_count("queued")
        return True
    except queue.Full:
        _capture_count("dropped_full")
    except Exception:
        _capture_count("write_failed")
    return False


def relay_origin_capture_path(chain=None):
    return STATE_ROOT / "data" / ("onchain_evm_relay_origin_malformed.%s.jsonl" % (chain or CHAIN))


def capture_relay_origin(record, chain=None):
    """Queue one final malformed Relay origin answer (dfeaef22 (b), 2026-10-07: 41 of 107 56 checks
    were malformed and nothing kept the response; the v3 bound path's tests are built from v2). Same
    bounded writer and file rotation as the envelope capture, a file of its own. Never blocks or raises."""
    import queue
    try:
        tx_hash = str(record.get("tx_hash") or "").lower()
        with _CAPTURE_LOCK:
            if "origin:" + tx_hash in _CAPTURE_SEEN:
                _CAPTURE_STATS["duplicate"] += 1
                return False
            _CAPTURE_SEEN.append("origin:" + tx_hash)
        _capture_queue().put_nowait((relay_origin_capture_path(chain), dict(
            record, chain=str(chain or CHAIN), tx_hash=tx_hash,
            captured_at=datetime.now(timezone.utc).isoformat())))
        _capture_count("queued")
        return True
    except queue.Full:
        _capture_count("dropped_full")
    except Exception:
        _capture_count("write_failed")
    return False


def _capture_write(path, record, part_bytes=None):
    """Append one record; first move a part that would pass `part_bytes` to <path>.1 (replacing it)."""
    part_bytes = RELAY_CAPTURE_PART_BYTES if part_bytes is None else part_bytes
    line = (json.dumps(record, separators=(",", ":"), default=str) + "\n").encode()
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    if size and size + len(line) > part_bytes:
        os.replace(path, str(path) + ".1")
        _capture_count("rotated")
    with open(path, "ab") as fh:
        fh.write(line)
    _capture_count("written")


def _capture_run(q):
    while True:
        path, record = q.get()
        try:
            _capture_write(path, record)
        except Exception as exc:
            _capture_count("write_failed")
            _say("[onchain-evm] relay capture write failed (%s: %s)" % (type(exc).__name__, exc))


# EVM SELL PATH, SHADOW (2026-09-29, Alpha GO; spec 2026-09-29-memecoin-evm-onchain-sell-path-design.md).
# Transfers FROM watched wallets, decoded on ONE worker of their own (never the buy path's
# `evm-receipt` pool, so a sale can never delay a buy decode), written to onchain_evm_sell_shadow.
# Nothing wakes the runner and no mirror reads the table: J7 stays the live sell source.
_SELL_POOL = []
_SELL_STATS = {}
_SELL_LOCK = threading.Lock()
_SELL_LOCAL = threading.local()
_BALANCE_OF = "0x70a08231"


def _sell_count(key, n=1):
    with _SELL_LOCK:
        _SELL_STATS[key] = _SELL_STATS.get(key, 0) + n


def _sell_pool():
    if not _SELL_POOL:
        from concurrent.futures import ThreadPoolExecutor
        _SELL_POOL.append(ThreadPoolExecutor(max_workers=1, thread_name_prefix="evm-sell"))
    return _SELL_POOL[0]


def sell_read_url(chain=None):
    """Where the sell shadow reads, or None. NEVER the funded custody RPC (Gamma MED, 2026-09-29): on
    4663 the watcher's own rpc_url IS the custody URL. The tx-read host first; else the watcher's
    rpc_url only when it is not custody; else nothing, and the sale is not decoded."""
    chain = str(chain or CHAIN)
    tx_read = (os.getenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_%s" % chain) or "").strip()
    if tx_read:
        return tx_read
    watcher = (os.getenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_%s" % chain) or "").strip()
    custody = (evm_rpc_for_chain(chain) or "").strip()
    if watcher and watcher.rstrip("/") != custody.rstrip("/"):
        return watcher
    return None


def sell_fallback_url(chain=None):
    """The sell shadow's SECOND host, or None (2026-10-07): the chain's backfill log-read host
    (_backfill_url: explicit, else its Alchemy pool member), read by its own direct POST like the
    first. Never custody and never the primary itself. On 8453 the primary (the watcher's
    publicnode HTTP URL) answers 403 to every receipt, and the Base mirror never decoded a sale.
    8453 ONLY: 56 and 4663 already decode sales on their primary (56: 86 not_a_sale outcomes), and
    their Alchemy member is shared with the funded pool, so no extra load there without evidence."""
    chain = str(chain or CHAIN)
    if chain != "8453":
        return None                 # 8453 only (Alpha, 2026-10-07): see the docstring
    url = (_backfill_url(chain) or "").strip()
    custody = (evm_rpc_for_chain(chain) or "").strip().rstrip("/")
    primary = (sell_read_url(chain) or "").strip().rstrip("/")
    if not url or url.rstrip("/") in (custody, primary):
        return None
    return url


def _token_balance_at(read_fn, token, wallet, block_hex):
    """balanceOf(wallet) at the sale's block, or None when unreadable."""
    try:
        raw = read_fn("eth_call", [{"to": token, "data": _BALANCE_OF + "0" * 24 + wallet[2:]},
                                   block_hex])
        return int(raw, 16)
    except Exception:
        return None


def decode_sell(log, *, watched, read_fn, conn, arrival, chain=None, sleep_fn=time.sleep):
    """Decode one delivered Transfer FROM a watched wallet. [sale dicts] written to the SHADOW
    table, or [] when the transaction is not a proved sale. Reads: the receipt (3 tries 0.3 s apart
    while it is null) and the tx; the wallet's native balance at N and N-1 only when neither an
    ERC-20 proceeds leg nor the Relay envelope proves the sale; balanceOf at N per coin sold."""
    chain = str(chain or CHAIN)
    topics = log.get("topics") or []
    wallet = _topic_address(topics[1])
    tx_hash = str(log.get("transactionHash") or "").lower()
    block_hex = log.get("blockNumber")
    receipt = None
    for attempt in range(3):
        receipt = read_fn("eth_getTransactionReceipt", [tx_hash])
        if receipt:
            break
        if attempt < 2:
            sleep_fn(0.3)
    if not receipt:
        _sell_count("read_failed")
        return []
    tx = read_fn("eth_getTransactionByHash", [tx_hash]) or {}
    verified = relay_code_verified(chain)
    sales = wallet_sales_from_receipt(receipt, wallet, chain=chain, relay_verified=verified)
    if not sales and block_hex:
        block = int(block_hex, 16)
        try:
            delta = (int(read_fn("eth_getBalance", [wallet, hex(block)]), 16)
                     - int(read_fn("eth_getBalance", [wallet, hex(block - 1)]), 16))
            if str(tx.get("from") or "").lower() == wallet:    # the wallet paid this tx's gas
                delta += (int(receipt.get("gasUsed") or "0x0", 16)
                          * int(receipt.get("effectiveGasPrice") or "0x0", 16))
        except Exception:
            delta = None
        sales = wallet_sales_from_receipt(receipt, wallet, chain=chain, relay_verified=verified,
                                          native_delta=delta)
    decoded_at = now_iso()
    if not sales:
        _sell_count("not_a_sale")
        print("[onchain-evm] sell %s arrival=%s decoded_at=%s outcome=not_a_sale"
              % (tx_hash, arrival, decoded_at), flush=True)
        return []
    source_user_id, handle = (watched.get(wallet) or (None, None))[:2]
    for sale in sales:
        post = _token_balance_at(read_fn, sale["mint"], wallet, block_hex)
        m.record_onchain_evm_sell_shadow(
            conn, network_id=chain, tx_hash=tx_hash, address=wallet, source_user_id=source_user_id,
            handle=handle, mint=sale["mint"], sold_atomic=sale["sold_atomic"],
            pre_atomic=None if post is None else post + sale["sold_atomic"], post_atomic=post,
            proof=sale["proof"], proceeds_token=sale["proceeds_token"],
            proceeds_atomic=sale["proceeds_atomic"],
            block_number=int(block_hex, 16) if block_hex else None,
            detected_at=arrival, decoded_at=decoded_at, created_at=decoded_at)
        _sell_count("proof:" + sale["proof"])
        print("[onchain-evm] sell %s arrival=%s decoded_at=%s proof=%s mint=%s"
              % (tx_hash, arrival, decoded_at, sale["proof"], sale["mint"][:12]), flush=True)
    # The runner's on-chain lane mirrors a listed wallet's sale (2026-10-07): wake it after the
    # write, as a detection does. A hint only (the lane's poll covers a lost one); never raises.
    send_wake(WAKE_ADDRESS)
    return sales


def _read_pair(read_fn, tx_hash):
    """(tx, receipt), read concurrently: the receipt on a worker thread, the tx here (2026-09-28)."""
    receipt = _reader().submit(read_fn, "eth_getTransactionReceipt", [tx_hash])
    tx = read_fn("eth_getTransactionByHash", [tx_hash])
    return tx, receipt.result()


def _receipt_stage(outcome, name, *, monotonic_ns=None):
    """Local memory only; old/non-BSC evidence retains its original clock contract."""
    anchor = outcome.get("_receipt_clock")
    if anchor is None:
        return
    mono = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
    outcome.setdefault("receipt_stages", []).append(dict(
        name=name, monotonic_ns=mono, boot_id=anchor["boot_id"],
        receipt_elapsed_ms=elapsed_receipt_ms(anchor, mono)))


class NativePrice:
    """The chain's quote-asset USD price, kept warm off the detection path (2026-09-28, Alpha GO).

    Every transfer used to fetch it from Binance BEFORE the detection was stamped (median 252 ms from
    this host). `start` refreshes it every `refresh_s` on a daemon thread; `get` returns the cached
    value when it is at most `max_age_s` old, and otherwise fetches synchronously as before and says
    so. Returns (price, age_s, source) with source 'cache' or 'sync'; a failed fetch is (None, None,
    'sync'), never zero. Only `run()` starts the thread; a watcher started any other way fetches on
    demand, which is today's behaviour."""

    def __init__(self, fetch_fn, *, clock=time.monotonic, log_fn=None, refresh_s=5.0, max_age_s=10.0):
        self._fetch, self._clock = fetch_fn, clock
        self._log = log_fn or _say
        self._refresh_s, self._max_age_s = float(refresh_s), float(max_age_s)
        self._lock = threading.Lock()
        self._value = self._at = None
        self._stop = threading.Event()

    def refresh(self):
        try:
            value = self._fetch()
        except Exception:
            value = None
        if value:
            with self._lock:
                self._value, self._at = float(value), self._clock()
        return float(value) if value else None

    def start(self):
        def loop():
            while not self._stop.is_set():
                self.refresh()
                self._stop.wait(self._refresh_s)
        threading.Thread(target=loop, daemon=True, name="native-price").start()

    def stop(self):
        self._stop.set()

    def peek(self):
        """(value, age_s) of the cached price, or (None, None). NEVER fetches: the Base fast lane's
        decide runs on the push socket's loop thread, where a fetch would hold back the next delivery,
        and a stale mark must read as stale (spec rev 3, Gamma MED)."""
        with self._lock:
            value, at = self._value, self._at
        if value is None or at is None:
            return None, None
        return value, self._clock() - at

    def get(self):
        with self._lock:
            value, at = self._value, self._at
        now = self._clock()
        if value is not None and at is not None and now - at <= self._max_age_s:
            return value, now - at, "cache"
        self._log("[onchain-evm] native price %s: fetched on the detection path"
                  % ("absent" if at is None else "stale (age %.1f s)" % (now - at)))
        value = self.refresh()
        return value, (0.0 if value else None), "sync"


def _atomic_write_text(path, text):
    """`path` written whole or not at all (2026-10-07, 8453 17:16Z restart): a kill mid write_text
    left torn JSON, the next process read no restart anchor, and its start backfill was skipped."""
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(text)
    os.replace(tmp, path)


def _write_beat(beat):
    # The pid (2026-10-02): the runner's BSC fast lane hears a datagram only from this process.
    _atomic_write_text(HEARTBEAT, json.dumps({**beat, **_rpc_load(), **_BASE_LANE_BEAT,
                                           "pid": os.getpid()}))


def _sweep_dead_tmp(path):
    """Remove `path`'s temp files left by a kill between write and rename (Gamma LOW). A live
    writer's temp is kept: an overlapping old process may still be about to rename it."""
    for tmp in path.parent.glob("%s.*.tmp" % path.name):
        try:
            pid = int(tmp.name[len(path.name) + 1:-len(".tmp")])
            os.kill(pid, 0)
            continue                       # alive (or a reused pid): leave it
        except ProcessLookupError:
            pass
        except (ValueError, OSError):
            continue
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass                           # a sweep never blocks a start (Gamma nit)


def _restart_anchor():
    """(the previous process's heartbeat last_block, None) or (None, why there is none)."""
    _sweep_dead_tmp(HEARTBEAT)
    try:
        text = HEARTBEAT.read_text()
    except FileNotFoundError:
        return None, "heartbeat missing"
    except OSError as exc:
        return None, "heartbeat unreadable (%s)" % type(exc).__name__
    try:
        block = json.loads(text).get("last_block")
    except (ValueError, AttributeError):
        return None, "heartbeat unreadable"
    if block is None:
        return None, "no last_block"
    try:
        return int(block), None
    except (TypeError, ValueError):
        return None, "last_block unreadable"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


_wall_clock = time.time


def _say(line):
    """One UTC-stamped line (2026-09-28): the unstamped log could not tell a delivery gap from a
    dropped read after the fact."""
    print("%s %s" % (now_iso(), line), flush=True)


def _parse_iso(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _topic(address):
    return "0x" + str(address).lower().removeprefix("0x").rjust(64, "0")


def _quantity(value):
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None


def _verified_addresses(conn):
    """{address: (source_user_id, handle)} for THIS chain: proven addresses of armed wallets.

    The list used to come from fomo profile pages (`memecoin_evm_wallet_addresses`). Measured
    2026-09-16 against our own funded entries, that address was contradicted on 45 of 50 checks on
    this chain and confirmed on none, and this lane has written zero 4663 detections in its life.
    The source is now the verified book: fomoscan-confirmed rows plus this lane's own
    `axiom_confirmed` rows. Nothing here asks fomo anything, and nothing here writes.

    ONE DEFINITION (2026-09-29): the J7 shadow gate covers EVM wallets by the same function, so what
    this watcher subscribes and what J7 stops buying cannot drift.
    """
    return evm_watched_owners(conn, CHAIN, include_operator=True)


def _roster_connection():
    """The head-path roster read's own read-only ledger handle (Gamma L2, 2026-09-29).

    The roster is re-read on the socket thread every few seconds. On the writable handle a checkpoint
    or long writer could hold it for the writer's 60 s busy wait, and heads, J7 checks and transfer
    delivery wait behind it. This handle waits 250 ms at most; a timeout keeps the subscribed set."""
    conn = get_readonly_connection(str(DB))
    conn.execute("PRAGMA busy_timeout=250")
    return conn


def _require_addresses(conn, *, sleep_fn=time.sleep, log_fn=None, retry_s=60.0):
    """Wait for a proven address instead of exiting. A watcher of an empty set sees nothing.

    Exiting made the watchdog relaunch once a minute until 9c20efd1 stopped launching it at all,
    and then only a deploy could start the lane again. Waiting here means an address proved later --
    a verifier run, an operator decision -- starts the lane on its own.
    """
    log_fn = log_fn or _say
    announced = False
    while True:
        watched = _verified_addresses(conn)
        if watched:
            return watched
        if not announced:
            announced = True
            log_fn("[onchain-evm] no verified address for chain %s: waiting for one. The lane "
                   "watches nobody until an address earns a current proof." % CHAIN)
        sleep_fn(retry_s)


def address_readiness(conn):
    """Read the same address and authority filter used by the watcher.

    The head count is visible here because a head-basis proof arms this watcher exactly like a
    receipt-block one (operator decision 2026-09-23, "no waiting"), and that is right for a LIVE
    event: head and the event block are the same block or seconds apart. What it cannot support is
    the historical claim, so the operator sees how many of the armed addresses rest on it.
    """
    from sniper.services.axiom_address_verify import head_basis_addresses
    watched = _verified_addresses(conn)
    count = len(watched)
    try:
        head = len({address for chain, address in head_basis_addresses(conn)
                    if str(chain) == CHAIN} & set(watched))
    except Exception:
        head = None          # a readiness probe must never fail on its own annotation
    reason = 'no verified addresses'
    if count:
        reason = ('verified addresses available (%d, %d at head)' % (count, head)
                  if head is not None else 'verified addresses available (%d)' % count)
    return {'status': 'eligible' if count else 'blocked',
            'reason': reason,
            'network_id': CHAIN, 'eligible_addresses': count, 'head_basis_addresses': head}


# The runner's on-chain lane listens here, as it does for the Solana watcher (2026-09-27). Without
# it the first live RH row (2865, 2026-09-28) waited 1.987 s for the lane's poll.
WAKE_ADDRESS = default_address()
# The push socket's state while push mode runs (run_push), for push_head_gap.
PUSH_STATE = None


def push_head_gap(log, arrival, state=None):
    """How far this detection's log push trailed its own block's head on the SAME socket (Alpha
    2026-10-14: armed 56 pushes ran p90 3.2 s after their block while publicnode heads ran p90 92 ms;
    is the tail in the log subscription?). Host wall clock on both sides; the header's ms when the head
    carried it. None (unknown, never 0) when that block's head was not seen here."""
    state = PUSH_STATE if state is None else state
    block, arrived = _quantity(log.get("blockNumber")), _parse_iso(arrival)
    seen = state.head_for(block) if state is not None and block is not None else None
    if seen is None or arrived is None:
        return None
    head_at, block_ms = seen
    out = {"head_at": head_at, "log_minus_head_ms": round((arrived.timestamp() - head_at) * 1000.0, 1)}
    if block_ms is not None:
        out["head_minus_block_ms"] = round(head_at * 1000.0 - block_ms, 1)
        out["log_minus_block_ms"] = round(arrived.timestamp() * 1000.0 - block_ms, 1)
    return out


def _commit_detections(conn, rows):
    """Record and commit the rows, THEN wake the runner's on-chain lane: one datagram per commit, and
    only when a row was written. The wake follows the commit so the sweep it triggers can read the
    row; it is a hint (the lane's poll covers a lost one) and send_wake never raises.

    STAGES (2026-09-28): `commit_at` is stamped into each row's valuation immediately before the
    write; `wake_sent_at` goes to one log line per row, not to the ledger, because a second write
    there would land exactly when the lane writes its attempt."""
    commit_at = now_iso()
    for row in rows:
        if row.get("valuation") is not None:
            row["valuation"]["commit_at"] = commit_at
            if not row["valuation"].get("backfill"):
                gap = push_head_gap(row.get("log") or {}, row.get("now"))
                if gap is not None:
                    row["valuation"]["push_head"] = gap
        _record(conn, **row)
    conn.commit()
    if rows:
        send_wake(WAKE_ADDRESS)
        wake_sent_at = now_iso()
        for row in rows:
            gap = (row.get("valuation") or {}).get("push_head") or {}
            print("[onchain-evm] detection %s arrival=%s decoded_at=%s commit_at=%s wake_sent_at=%s %s "
                  "head_gap_ms=%s"
                  % (str(row.get("tx_hash") or "")[:12], row.get("now"),
                     (row.get("valuation") or {}).get("decoded_at"), commit_at, wake_sent_at,
                     (row.get("valuation") or {}).get("reads") or "",
                     gap.get("log_minus_head_ms", "unknown")),
                  flush=True)


def _bsc_lane_signal(observe_only):
    """The BSC fast lane's hook (MEMECOIN_BSC_FAST_LANE runner_shadow|live, chain 56 only), or None.
    One start line says whether it is armed, so the hook is confirmed before the first detection."""
    if str(CHAIN) != "56":
        return None
    from sniper.services.memecoin_bsc_fast_lane import default_address, make_watcher_signal
    from sniper.settings import memecoin as cfg
    mode, address = cfg.memecoin_bsc_fast_lane_mode(), default_address()
    signal = None if observe_only else make_watcher_signal(mode, address, hop_check_fn=_bsc_hop_check)
    _say("[bsc-lane] hook %s: mode=%s%s socket=%s" % (
        "ARMED" if signal else "off", mode, " (observe-only watcher)" if observe_only else "",
        address.lstrip("\0")))
    return signal


def _bsc_hop_check(rec):
    _say("[bsc-lane] hop_check %s pair=%s used=%s seen=%s seq=%s late=%s" % (
        str(rec.get("tx_hash") or "")[:12], rec["pair"], rec["used"], rec["seen"], rec["seq"],
        rec["late"]))


_BASE_LANE_BEAT = {}              # the heartbeat's base_step0 state (chain 8453 only)


def _base_lane_hook(observe_only):
    """The Base 8453 step-0 hook (MEMECOIN_BASE_FAST_LANE=step0, chain 8453 only), or None. It
    decides from the receipt in hand and RECORDS; nothing is sent. Its PoolKey store reads go through
    their own read-only ledger handles (busy_timeout 250 ms, as the roster's; one per calling thread,
    see _thread_key_fn), and only when the source's calldata holds no key. One start line says
    whether it is armed.

    NOTHING HERE MAY STOP FUNDED 8453 DETECTION (Alpha, Gamma LOW 1 on fba091be5): the module is
    imported HERE, inside the guard, never at the watcher's import; any fault building the hook,
    knob on or off, is one log line, base_step0=step0_unavailable in the heartbeat, and no hook."""
    if str(CHAIN) != "8453":
        return None
    try:
        from sniper.services import memecoin_base_fast_decide as base
        lane_mode = base.mode()
        hook = None
        if lane_mode != "off" and not observe_only:
            from sniper.util.state_paths import state_path
            writer = base.DecisionWriter(state_path("data", "base_fast_decisions.db"))
            send_fn = None
            if lane_mode in ("runner_shadow", "live"):
                # Step 1 (plan 2026-10-14 part C): one datagram to the runner's Base lane, never
                # raising, never blocking; the record keeps being written either way.
                from sniper.services.memecoin_base_fast_lane import default_address
                from sniper.services.memecoin_rh_fast_lane import send_signal
                address = default_address()
                send_fn = lambda payload: send_signal(address, payload)  # noqa: E731
            hook = base.make_hook(writer, key_fn=_thread_key_fn(), send_fn=send_fn)
    except Exception as exc:
        _BASE_LANE_BEAT["base_step0"] = "step0_unavailable"
        _say("[base-lane] step0_unavailable: %s: %s (funded detection unaffected)"
             % (type(exc).__name__, exc))
        return None
    _BASE_LANE_BEAT["base_step0"] = "armed" if hook else lane_mode
    _say("[base-lane] hook %s: mode=%s%s" % (
        ("ARMED (record only)" if lane_mode == "step0" else "ARMED (record + datagram to the runner)")
        if hook else "off", lane_mode, " (observe-only watcher)" if observe_only else ""))
    return hook


def _thread_key_fn():
    """conn_v4_key_fn on a read-only ledger handle OF THE CALLING THREAD; a sqlite handle belongs to
    the thread that opened it. The step-0 decide now runs on the base-decide thread (_BASE_DECIDE),
    which opens its own handle on its first stored-key read; the per-thread rule keeps any other
    caller safe."""
    from sniper.services.memecoin_evm_post_trade import conn_v4_key_fn
    local = threading.local()
    local.read = conn_v4_key_fn(_roster_connection())

    def key_fn(chain, pool):
        if not hasattr(local, "read"):
            local.read = conn_v4_key_fn(_roster_connection())
        return local.read(chain, pool)
    return key_fn


# THE DECIDE RUNS ON ITS OWN THREAD (Gamma LOW on 3fa583899): a stored-key read can wait up to the
# 250 ms busy_timeout, and inline it held the push socket's next delivery or the origin worker's next
# funded publication. One worker, FIFO, so records keep arrival order; its threads start on the first
# submit, so a non-8453 watcher never starts one.
# ponytail: unbounded queue; Base step-0 buys are tens a day. Bound it if the lane ever widens.
from concurrent.futures import ThreadPoolExecutor as _Executor  # noqa: E402
_BASE_DECIDE = _Executor(max_workers=1, thread_name_prefix="base-decide")


def _signal_base_lane(hook, row, outcome, price, price_source, *, path="direct", verify_ms=None):
    """Hand one step-0 decision to the decide thread, AFTER today's hand-off: from the socket thread
    after commit and wake (path 'direct'), or from a Relay origin worker after verification and
    publication (path 'relay_verified'). Returns at once; never raises."""
    try:
        _BASE_DECIDE.submit(_decide_base_lane, hook, row, outcome, price, price_source, path, verify_ms)
    except Exception as exc:              # a record only; today's path already has the row
        _say("[base-lane] decide not queued: %s: %s" % (type(exc).__name__, exc))


def _decide_base_lane(hook, row, outcome, price, price_source, path, verify_ms):
    """The decide itself, on the decide thread: the ETH/USD mark by peek() only, never a fetch."""
    try:
        eth_usd, age = price.peek()
        rec = hook(row, outcome.get("_transaction"), outcome.get("_receipt"), eth_usd, age, price_source,
                   path=path, verify_ms=verify_ms)
        since = rec.get("since_arrival_ms")
        lane = ("sent" if rec.get("lane_sent") else "unsent") if "lane_sent" in rec else rec.get("lane_skip")
        _say("[base-lane] %s reason=%s kind=%s key=%s decide_ms=%.2f path=%s since_arrival_ms=%s%s" % (
            str(row.get("tx_hash") or "")[:12], rec.get("reason"), rec.get("route_kind"),
            rec.get("key_source"), rec.get("decide_ms") or 0.0, path,
            "%.0f" % since if since is not None else "-", " lane=%s" % lane if lane else ""))
    except Exception as exc:              # a record only; today's path already has the row
        _say("[base-lane] decide failed: %s: %s" % (type(exc).__name__, exc))


# The live 56 push watcher installs memecoin_bsc_fast_lane.want_hop_quote here (run_push); a
# library call (a test, a replay) records no wanted hop in a shared state root.
_HOP_WANTED_SINK = None


def _signal_bsc_lane(signal, row, outcome, quote_usd, price_age):
    """Send the receipt-bound 56 hint and return its delivery result. Never raises. The row carries its
    block's head as this push socket saw it (first arrival, header ms): the lane's crowded gas rule
    times the send from it. A dict lookup; absent outside push mode or when that head was missed."""
    try:
        state = PUSH_STATE
        block = _quantity((row.get("log") or {}).get("blockNumber"))
        seen = state.head_for(block) if state is not None and block is not None else None
        row = dict(row, source_head=seen) if seen is not None else row
        out = signal(row, outcome.get("_receipt"), outcome.get("evm_post"),
                     decimals=outcome.get("decimals"), bnb_usd=quote_usd, bnb_usd_age_s=price_age,
                     transaction=outcome.get("_transaction"))
        block = (outcome.get('_receipt') or {}).get('blockNumber')
        # The existing log write carries full identity; the timing cron persists it off path.
        _say('[bsc-lane-note] ' + json.dumps(dict(
            chain='56', tx_hash=str(row.get('tx_hash') or '').lower(),
            mint=str((row.get('buy') or {}).get('mint') or '').lower(),
            source_block=int(block, 16) if isinstance(block, str) else int(block or 0),
            reason=out.get('reason'), sent=out.get('sent'), decide_ms=out.get('decide_ms'),
            split=out.get('split', False), **({'quote': out['quote']} if out.get('quote') else {})),
            separators=(',', ':')))
        from sniper.services.memecoin_bsc_fast_lane import _HOP_WANTED_REASONS
        if _HOP_WANTED_SINK is not None and out.get('reason') in _HOP_WANTED_REASONS and out.get('quote'):
            _HOP_WANTED_SINK(out['quote'])        # after the lane's decision; the tape pins it next
        return out
    except Exception as exc:              # the signal is a hint; today's path already has the row
        _say("[bsc-lane] signal failed: %s: %s" % (type(exc).__name__, exc))
        return None


def _publish_detection(conn, row, *, bsc_signal=None, outcome=None, quote_usd=None, price_age=None):
    """Offer BSC's lane first; persist the bounded priority with the ordinary fallback row."""
    from sniper.settings import memecoin as cfg
    live = bsc_signal is not None and cfg.memecoin_bsc_fast_lane_mode() == 'live'
    if live:
        out = _signal_bsc_lane(bsc_signal, row, outcome, quote_usd, price_age)
        if isinstance(out, dict) and out.get('sent') is True:
            row['valuation'] = dict(row.get('valuation') or {}, bsc_fast_lane_sent=True)
    _commit_detections(conn, [row])
    if bsc_signal is not None and not live:
        _signal_bsc_lane(bsc_signal, row, outcome, quote_usd, price_age)


def _origin_identities(uid):
    """Verifier thread's short-lived read-only connection; never the socket's handle."""
    conn = get_readonly_connection(str(DB))
    try:
        conn.execute('PRAGMA busy_timeout=250')
        cursor = conn.execute('SELECT source_user_id, network_id, address, status, status_at, conflict_at '
                              'FROM memecoin_address_registry WHERE source_user_id=?', (uid,))
        return [dict(zip((column[0] for column in cursor.description), row)) for row in cursor]
    finally:
        conn.close()


_DEPOSIT_STORE = []
_DEPOSIT_STORE_LOCK = threading.Lock()


def _deposit_store():
    """Relay option A (spec 2026-10-09): the shared deposit store when this watcher's chain is listed in
    MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS, else None. Created once, under its lock."""
    from sniper.settings import memecoin as cfg
    if CHAIN not in cfg.MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS:
        return None
    with _DEPOSIT_STORE_LOCK:
        if not _DEPOSIT_STORE:
            from sniper.services.memecoin_alerts import send_alert
            from sniper.services.memecoin_relay_deposit_store import DepositStore
            _DEPOSIT_STORE.append(DepositStore(cfg.MEMECOIN_RELAY_DEPOSIT_STORE_PATH,
                                               page_fn=lambda text: send_alert("relay_origin", text)))
        return _DEPOSIT_STORE[0]


def _deposit_registry_ok(depositor, uid):
    from sniper.services.memecoin_relay_deposit_store import registry_reader
    return registry_reader(DB)(depositor, uid)


def _prove_from_deposit(row, transaction):
    """The deposit check at detection: (True, ms) when the row is now proved from the user's own stored
    deposit (its source_initiation and valuation are set in place), else (False, status)."""
    store = _deposit_store()
    if store is None:
        return False, "off"
    from sniper.services.memecoin_relay_deposit_store import prove_from_deposit
    started = time.perf_counter()
    try:
        proved, status = prove_from_deposit(row, transaction, chain=CHAIN, store=store,
                                            registry_ok=_deposit_registry_ok, now=time.time())
    except Exception as exc:                     # noqa: BLE001 - no proof; the Relay path runs
        _say("[onchain-evm] relay deposit check %s failed %s: %s" % (
            str(row.get("tx_hash"))[:12], type(exc).__name__, str(exc)[:120]))
        return False, "error"
    if proved is None:
        return False, status
    ms = (time.perf_counter() - started) * 1000.0
    row["buy"] = proved["buy"]
    row["valuation"] = dict(row.get("valuation") or {}, relay_origin_ms=ms, relay_origin_path="deposit",
                            relay_origin_error=None)
    return True, ms


def _relay_origin_worker(signal, base_hook=None, price=None, publication_fence=None, authority_fn=None):
    """Relay verification has its own bounded threads and dedicated/public read RPC. `base_hook`
    (Base step 0, 8453 only) decides a VERIFIED buy after its publication; a refused one is skipped
    with a line, because a live lane could not act on it (Alpha ruling b, 2026-10-07)."""
    from sniper.services.memecoin_relay_initiation import OriginWorker, WatcherVerifier, read_json
    from sniper.services.memecoin_research_pacer import drpc_paced_json
    from sniper.settings import memecoin as cfg
    if cfg.MEMECOIN_SOLANA_INFRA_PROVIDER == 'shyft':
        from sniper.services.memecoin_solana_provider import provider_urls
        solana_rpc = provider_urls('read')[0]
    else:
        solana_rpc = (os.getenv('MEMECOIN_RESEARCH_HELIUS_RPC_URL') or
                      'https://api.mainnet-beta.solana.com').strip()
        custody = {os.getenv('MEMECOIN_SOLANA_RPC_URL', '').rstrip('/'),
                   *[v.strip().rstrip('/') for v in os.getenv('MEMECOIN_SOLANA_RPC_POOL_URLS', '').split(',')]}
        if solana_rpc.rstrip('/') in custody:
            solana_rpc = 'https://api.mainnet-beta.solana.com'
    evm_rpcs = {}
    funded_pools = getattr(cfg, 'MEMECOIN_EVM_RPC_POOL_URLS', {}) or {}
    for chain in ('1', '56', '8453', '4663', '5042'):
        funded = {str(url).strip().rstrip('/') for url in funded_pools.get(chain, [])}
        funded.add((evm_rpc_for_chain(chain) or '').strip().rstrip('/'))
        url = os.getenv('MEMECOIN_RESEARCH_DRPC_RPC_URL_' + chain, '').strip()
        if url and url.rstrip('/') not in funded:
            evm_rpcs[chain] = url
    store = _deposit_store()
    deposit = {}
    if store is not None:                # Relay option A: the deposit first on every poll, and the
        from sniper.services.memecoin_relay_deposit_store import deposit_check   # shared rule
        deposit = dict(deposit_fn=deposit_check(store, chain=CHAIN, registry_ok=_deposit_registry_ok),
                       depositor_fn=store)
    verifier = WatcherVerifier(_origin_identities, solana_rpc=solana_rpc,
                              evm_rpcs=evm_rpcs,
                              fetch=drpc_paced_json(read_json),
                              api_key=os.getenv('MEMECOIN_RELAY_API_KEY', ''),
                              requests_version=cfg.MEMECOIN_RELAY_REQUESTS_VERSION,
                              capture_fn=capture_relay_origin, **deposit)
    local = threading.local()
    def publish_now(item):
        row = item['row']
        if item['outcome'].get('_receipt_clock') is not None:
            row['receipt_stages'] = tuple(item['outcome'].get('receipt_stages') or ())
            row['valuation'] = dict(row.get('valuation') or {}, receipt_stages=row['receipt_stages'])
        if authority_fn is not None and not authority_fn(row):
            _say('[onchain-evm] relay origin %s source_authority_revoked' % row['tx_hash'][:12])
            return
        if item.get('pregate_published'):
            # THE PROOF STILL RECORDS (Alpha condition 2): the row went out labelled; what Relay or a
            # deposit said afterwards is kept on it, so the label can be graded against the proof.
            _record_late_origin(item)
            _say('[onchain-evm] ' + _relay_origin_line(item) + ' late_after_pregate')
            return
        row['valuation'] = dict(row.get('valuation') or {},
                                relay_origin_ms=item['origin_verification_ms'],
                                relay_origin_error=item.get('origin_verification_error'))
        if item.get('pregate'):
            row['valuation']['relay_pregate'] = item['pregate']
        # Preserve source arrival. Existing entry age/chase/authority checks run after publication.
        conn = get_connection(str(DB))
        try:
            local_signal = None
            age = item['price_age']
            if signal is not None and not item.get('backfill'):
                if not hasattr(local, 'signal'):
                    local.signal = _bsc_lane_signal(False)
                local_signal = local.signal
                if age is not None:
                    age += max(0., time.monotonic() - item['submitted_mono'])
            _publish_detection(conn, row, bsc_signal=local_signal, outcome=item['outcome'],
                               quote_usd=item['quote_usd'], price_age=age)
        finally:
            conn.close()
        if base_hook is not None and not item.get('backfill'):    # AFTER publication, record only
            error = item.get('origin_verification_error')
            if error:
                _say('[base-lane] %s skipped origin_refused=%s' % (row['tx_hash'][:12], error))
            else:
                _signal_base_lane(base_hook, row, item['outcome'], price,
                                  row['valuation'].get('quote_price_source'), path='relay_verified',
                                  verify_ms=item['origin_verification_ms'])
        _say('[onchain-evm] ' + _relay_origin_line(item))

    def publish(item):
        if publication_fence is None:
            return publish_now(item)
        return publication_fence.call(publish_now, item)

    def verify(item):
        _receipt_stage(item['outcome'], 'relay_origin_start')
        # SECTION 5, LABEL ONLY (MEMECOIN_RELAY_PREGATE): on this worker thread, never the detection's.
        # shadow: the verdict rides on the row, which waits as today. on: a label publishes the
        # unproved row at once with the hook's own reason; the verification then runs to its end as
        # before and records late (publish above).
        mode = cfg.MEMECOIN_RELAY_PREGATE
        if mode in ('shadow', 'on') and not item.get('backfill'):
            started = time.perf_counter()
            label = _pregate_label(item['row'])
            ms = round((time.perf_counter() - started) * 1000, 3)
            if label is not None:
                cls, text, inputs = label
                item['pregate'] = dict(inputs or {}, mode=mode, cls=cls, text=text, ms=ms)
                if mode == 'on' and cls:
                    publish(dict(item, origin_verification_error='pregate:' + text, origin_verification_ms=ms))
                    item['pregate_published'] = True
        try:
            return verifier(item)
        finally:
            _receipt_stage(item['outcome'], 'relay_origin_end')
    def close_signal():
        getattr(getattr(local, 'signal', None), 'close', lambda: None)()

    return OriginWorker(verify, publish, on_stop=close_signal)


_PROCESS_STARTED_AT = time.time()      # the env files this process loaded are at most this old


def _pregate_label(row):
    """(class or None, text or the silence reason, inputs) from memecoin_relay_pregate.for_row on a
    short read-only connection, judged with the runner's effective knobs and silent on an env file
    newer than this process; None on any failure (no label is ever a guess)."""
    from sniper.services import memecoin_relay_pregate as pregate
    try:
        conn = get_readonly_connection(str(DB))
        try:
            conn.execute('PRAGMA busy_timeout=250')
            return pregate.for_row(conn, row, chain=CHAIN, now_iso=datetime.now(timezone.utc).isoformat(),
                                   env_paths=(STATE_ROOT / '.env', STATE_ROOT / '.fomo.env'),
                                   started_at=_PROCESS_STARTED_AT,
                                   runner_heartbeat=(os.getenv('MEMECOIN_HEARTBEAT_PATH')
                                                     or STATE_ROOT / 'data' / 'memecoin_runner.heartbeat'))
        finally:
            conn.close()
    except Exception as exc:                     # noqa: BLE001 - silent; the wait runs as today
        _say('[onchain-evm] relay pregate %s failed %s: %s' % (
            str(row.get('tx_hash'))[:12], type(exc).__name__, str(exc)[:120]))
        return None


def _record_late_origin(item):
    """The verification's own outcome on the already-published row: relay_origin_late in its
    source_valuation_json (error, proof_version, ms). One UPDATE by the row's own writer."""
    row = item['row']
    proof = (row.get('buy') or {}).get('source_initiation') or {}
    mint = proof.get('mint') or (row.get('buy') or {}).get('mint')
    late = dict(error=item.get('origin_verification_error'),
                proof_version=None if item.get('origin_verification_error') else proof.get('proof_version'),
                ms=item.get('origin_verification_ms'))
    conn = get_connection(str(DB))
    try:
        conn.execute("UPDATE onchain_wallet_detections SET source_valuation_json = json_set("
                     "coalesce(source_valuation_json, '{}'), '$.relay_origin_late', json(?)) WHERE signature=?",
                     (json.dumps(late), "%s:%s:%s" % (CHAIN, str(row.get('tx_hash')).lower(), mint)))
        conn.commit()
    finally:
        conn.close()


def _relay_origin_line(item):
    """`relay origin <tx> reason=<r> verify_ms=<ms>`, then `attempts=` (one note per verifier
    attempt), plus `detail=` when the refusal named what failed (a malformed answer's class, key and
    line; an unknown v3 status)."""
    line = 'relay origin %s reason=%s verify_ms=%.3f' % (
        item['row']['tx_hash'][:12], item.get('origin_verification_error') or 'verified',
        item['origin_verification_ms'])
    if item.get('origin_attempts'):        # (i) each attempt: error/ms/Relay status/deposit tx
        line += ' attempts=%s' % ','.join(item['origin_attempts'])
    detail = item.get('origin_verification_detail')
    return line + (' detail=%s' % detail if detail else '')


def _defer_relay_origin(worker, row, outcome, quote_usd, price_age, *, backfill=False):
    proof = (row.get('buy') or {}).get('source_initiation')
    if (not isinstance(proof, dict)
            or not str(proof.get('proof_version') or '').startswith('relay-')):
        return False
    # RELAY OPTION A: a matching stored deposit proves the row now; it is published at once and
    # never waits on Relay (spec 2026-10-09 section 3.2).
    proved, info = _prove_from_deposit(row, outcome.get('_transaction'))
    if proved:
        _say('[onchain-evm] relay origin %s reason=verified path=deposit verify_ms=%.3f'
             % (row['tx_hash'][:12], info))
        return False
    if worker is None:
        return False
    item = dict(row=row, outcome=outcome, transaction=outcome.get('_transaction'),
                quote_usd=quote_usd, price_age=price_age, backfill=backfill,
                submitted_mono=time.monotonic())
    if worker.submit(item):
        return True
    row['valuation']['relay_origin_error'] = 'origin_queue_full'
    _say('[onchain-evm] relay origin %s reason=origin_queue_full' % row['tx_hash'][:12])
    return False                         # original unknown proof is recorded and safely refused


def _record(conn, *, tx_hash, wallet, source_user_id, handle, buy, log, now, valuation=None,
            observation_key=None, observation_state=None, receipt_clock=None, receipt_stages=()):
    block_time = _quantity(log.get("blockTimestamp"))
    detected = _parse_iso(now)
    latency = ((detected.timestamp() - block_time) if detected and block_time else None)
    columns = ("signature,source_user_id,handle,address,network_id,mint,token_delta,source_size_usd,"
               "source_price,block_time,detected_at,detect_latency_s,created_at")
    values = [f"{CHAIN}:{str(tx_hash).lower()}:{buy['mint']}", source_user_id, handle, wallet, CHAIN,
              buy["mint"], buy["token_delta"], buy["source_size_usd"], buy["source_price"],
              block_time, now, latency, now]
    if isinstance(buy.get("source_initiation"), dict):
        valuation = dict(valuation or {}, source_initiation=buy["source_initiation"])
    if receipt_clock is not None:
        valuation = dict(valuation or {}, receipt_clock=dict(receipt_clock), receipt_stages=receipt_stages)
    if valuation is not None:
        valuation = dict(valuation)
        valuation.pop("source_fill", None)
        valuation.pop("source_fill_reason", None)
    if buy.get("source_fill_reason") == "unexplained_quote_outflow":
        valuation = dict(valuation or {}, source_fill_reason=buy["source_fill_reason"])
    # A preconfirmed log's 0x00..0 block hash is not a block (2026-10-09): no source fill until sealed.
    if log.get("transactionHash") == str(tx_hash).lower() and P.real_hash(log.get("blockHash")):
        fill = source_fill_from_buy(buy, dict(chain=CHAIN, tx_hash=str(tx_hash).lower(), wallet=wallet,
            source_user_id=source_user_id, mint=buy["mint"], seq=_quantity(log.get("blockNumber")),
            block_hash=log["blockHash"]))
        if fill is not None:
            valuation = dict(valuation or {}, source_fill=fill)
    if valuation is not None:              # the stage stamps (2026-09-28); absent = the old row
        columns += ",source_valuation_json"
        values.append(json.dumps(valuation, separators=(",", ":")))
    if observation_state is not None:          # a Base preconfirmed row (2026-10-09): its own state
        columns += ",source_observation_key,source_observation_state"
        values += [observation_key, observation_state]
    columns += ",source_slot"                  # fill timing: the source block, last (2026-10-02)
    values.append(_quantity(log.get("blockNumber")))
    try:
        conn.execute("INSERT OR IGNORE INTO onchain_wallet_detections (%s) VALUES (%s)"
                     % (columns, ",".join("?" * len(values))), values)
    except sqlite3.OperationalError as exc:
        # A table that predates source_slot still records the buy, without the slot: the detection is
        # the signal itself (Gamma M2, 2026-10-02).
        if "source_slot" not in str(exc):
            raise
        cols = columns.split(",")
        i = cols.index("source_slot")
        conn.execute("INSERT OR IGNORE INTO onchain_wallet_detections (%s) VALUES (%s)"
                     % (",".join(cols[:i] + cols[i + 1:]), ",".join("?" * (len(values) - 1))),
                     values[:i] + values[i + 1:])


_PAYMENT_PRICES = {}
# The Relay payment tokens the 2026-09-28 sample of real J7-proved buys carried: USDG on 4663,
# Binance-Peg USDC on 56, USDC on 8453; USDC on chain 1 (69 of 69 decoded chain-1 envelopes,
# 2026-10-17). Any other payment token stays unknown, so its buy is not detected.
_PAYMENT_TICKERS = {("4663", "0x5fc5360d0400a0fd4f2af552add042d716f1d168"): "USDGUSD",
                    ("56", "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"): "USDCUSD",
                    ("8453", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"): "USDCUSD",
                    ("1", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"): "USDCUSD"}


_payment_clock = time.monotonic
_PAYMENT_REFRESH_S = 10.0          # well inside the 30 s a detection accepts


def _fetch_payment_price(pair):
    """One Kraken bid for `pair`, or None. Never raises."""
    from sniper.execution import evm_wallet
    try:
        status, data, _ = evm_wallet._http('GET', 'https://api.kraken.com/0/public/Ticker?pair=' + pair,
                                           timeout=3)
        if status == 200 and isinstance(data, dict) and not data.get('error'):
            return float(data['result'][pair]['b'][0])
    except (OSError, KeyError, IndexError, TypeError, ValueError):
        pass
    return None


def _payment_usd_price(token, native_price):
    """Cache a measured payment-token price for at most 30 seconds. Missing prices remain unknown."""
    if token == WETH:
        return native_price
    pair = _PAYMENT_TICKERS.get((CHAIN, token))
    if pair is None:
        return None
    at, value = _PAYMENT_PRICES.get(token, (-float('inf'), None))
    if _payment_clock() - at >= 30:
        value = _fetch_payment_price(pair)
        _PAYMENT_PRICES[token] = (_payment_clock(), value)
    return value


def _refresh_payment_prices():
    """Re-read this chain's payment tickers off the detection path (2026-09-28): row 2931 spent 87.5
    of its 89.5 ms decode on a Kraken GET. Only a SUCCESS is stored, so a failed refresh never
    replaces a good price with None; the old entry ages out and the detection fetches inline. One
    tuple assignment per token, atomic under the GIL, so the detection thread needs no lock."""
    for (chain, token), pair in _PAYMENT_TICKERS.items():
        if chain == CHAIN:
            value = _fetch_payment_price(pair)
            if value is not None:
                _PAYMENT_PRICES[token] = (_payment_clock(), value)


def _payment_price_loop(stop):
    """Refresh at start, then every _PAYMENT_REFRESH_S until `stop` is set."""
    while True:
        try:
            _refresh_payment_prices()
        except Exception as exc:
            _say("[onchain-evm] payment price refresh failed (%s: %s)" % (type(exc).__name__, exc))
        if stop.wait(_PAYMENT_REFRESH_S):
            return


_TX_READS = {"eth_getTransactionByHash": "tx", "eth_getTransactionReceipt": "receipt"}


def _http_post(url, body, timeout):
    """(HTTP status, JSON body or None). A curl User-Agent: publicnode answers Python's default
    urllib agent with a bare 403."""
    req = urllib.request.Request(url, json.dumps(body).encode(), {
        "Content-Type": "application/json", "User-Agent": "curl/8.5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None


class TxReadHost:
    """The WS provider's own HTTP endpoint, asked first for a just-delivered tx and receipt
    (2026-09-28). The read host (4663: the custody endpoint) returned a NULL tx at arrival on every
    transfer, while this endpoint served a tx from its newest block on the first read 10 of 10 times.

    `read` returns (result, None) or (None, why): cooling, http_<status>, transport, rpc_error, null.
    A 403 or 429 cools the host for `cooldown_s`; nothing here is ever the final word on a transfer,
    the caller falls back to the read host. Safe across the receipt worker and the caller thread."""

    def __init__(self, url, *, cooldown_s, timeout_s, post_fn=None, clock=time.monotonic,
                 name="buy", meanwhile=None):
        self.url, self.name = url, name     # who is cooled, in the log (Gamma LOW, 2026-09-29)
        self._meanwhile = meanwhile         # what reads instead while this host cools
        self._cooldown_s, self._timeout_s = float(cooldown_s), float(timeout_s)
        self._post, self._clock = post_fn or _http_post, clock
        self._lock = threading.Lock()
        self._cool_until = None

    def read(self, method, params):
        with self._lock:
            if self._cool_until is not None and self._clock() < self._cool_until:
                return None, "cooling"
        try:
            status, answer = self._post(
                self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                self._timeout_s)
        except Exception:
            return None, "transport"
        if status in (403, 429):
            with self._lock:
                self._cool_until = self._clock() + self._cooldown_s
            _say("[onchain-evm] %s tx-read host answered HTTP %s: cooling it %.0f s, %s"
                 % (self.name, status, self._cooldown_s,
                    self._meanwhile or ("no fallback: sells go unread meanwhile"
                                        if self.name == "sell"
                                        else "reading from the read host meanwhile")))
            return None, "http_%s" % status
        if status != 200 or not isinstance(answer, dict):
            return None, "http_%s" % status
        if "error" in answer:
            return None, "rpc_error"
        if answer.get("result") is None:
            return None, "null"
        return answer["result"], None


def _ws_connect(url, timeout):
    from websockets.sync.client import connect
    return connect(url, open_timeout=timeout, close_timeout=0.2, max_size=2 ** 24)


class WsTxReadHost:
    """C4(b) (2026-10-01): the tx and receipt over the push provider's WEBSOCKET. publicnode's BSC HTTP
    endpoint refuses every eth_getTransactionReceipt (403 "Archive requests require a personal
    token", any User-Agent); its wss endpoint served head-block receipts 3 of 3 in 18-48 ms.

    Same contract as TxReadHost.read. One kept connection, one request at a time; the factory gives
    the tx and the receipt one each (WsTxReadPair, 2026-10-14: host_ms showed the turns). Any
    transport failure or timeout CLOSES the connection, so a late reply is never read as the next
    request's answer, and cools the host: the caller reads from the read host meanwhile."""

    def __init__(self, url, *, cooldown_s, timeout_s, read_cooldown_s=10.0, null_retry_s=0.1,
                 connect_fn=None, clock=time.monotonic, sleep_fn=time.sleep, name="buy", keepalive_s=10.0,
                 max_age_s=40.0):
        self.url, self.name = url, name
        self._cooldown_s, self._timeout_s = float(cooldown_s), float(timeout_s)
        self._read_cooldown_s, self._null_retry_s = float(read_cooldown_s), float(null_retry_s)
        self._connect, self._clock, self._sleep = connect_fn or _ws_connect, clock, sleep_fn
        self._keepalive_s, self._max_age_s = float(keepalive_s), float(max_age_s)
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._ws, self._cool_until, self._id, self._born = None, None, 0, None

    def keepalive_once(self):
        """One keepalive tick, OFF the read path (2026-10-02). publicnode drops a kept socket that has
        carried no DATA for about 60 s; pings do not count (measured: pinged every 15 s, dropped at
        60, 135 and 211 s; with a read every 30 s one socket lived 180 s), and an unpinged one died
        after 2 and 19 idle minutes live. So the tick replaces a
        socket older than `max_age_s` BEFORE the server closes it, pings a younger one, and reopens at
        once when the ping fails. A read never waits on this: a held lock skips the tick, and the
        connect and the pong wait run OUTSIDE the lock (a socket that loses a race with a read's own
        connect is closed). Returns connected / rotated / raced / ok / dropped+connected / failed /
        busy / cooling / closed."""
        if self._closed.is_set():
            return "closed"
        if not self._lock.acquire(blocking=False):
            return "busy"
        try:
            if self._cool_until is not None and self._clock() < self._cool_until:
                return "cooling"
            ws, pong = self._ws, None
            old = ws is not None and self._born is not None and self._clock() - self._born >= self._max_age_s
            if ws is not None and not old:
                try:
                    pong = ws.ping()
                except Exception:
                    pong = None
        finally:
            self._lock.release()
        if ws is not None and not old:
            if pong is not None and pong.wait(self._timeout_s):
                return "ok"
            self._retire(ws)
            return "dropped+" + self._open(replacing=None)
        return self._open(replacing=ws) if old else self._open(replacing=None)

    def _open(self, *, replacing):
        """Open a socket outside the lock and install it if the slot still holds `replacing`."""
        try:
            fresh = self._connect(self.url, self._timeout_s)
        except Exception as exc:
            with self._lock:
                self._cool_until = self._clock() + self._cooldown_s
            _say("[onchain-evm] %s tx-read websocket keepalive connect failed (%s): cooling it %.0f s"
                 % (self.name, type(exc).__name__, self._cooldown_s))
            return "failed"
        with self._lock:
            if self._ws is replacing and not self._closed.is_set():
                self._ws, self._born = fresh, self._clock()
                installed = True
            else:
                installed = False
        if not installed:
            self._close_quietly(fresh)
            return "raced"
        if replacing is not None:
            self._close_quietly(replacing)
            return "rotated"
        return "connected"

    def _retire(self, ws):
        with self._lock:
            if self._ws is ws:
                self._ws = None
        self._close_quietly(ws)

    @staticmethod
    def _close_quietly(ws):
        try:
            ws.close()
        except Exception:
            pass

    def start_keepalive(self):
        """The keepalive on its own DAEMON thread (a socket it opens gets a daemon receive thread too, so
        it never holds the process at exit); close() ends it."""
        def run():
            while True:                   # the first tick opens the socket at once, before any read
                try:
                    self.keepalive_once()
                except Exception as exc:
                    _say("[onchain-evm] tx-read websocket keepalive tick failed (%s: %s)"
                         % (type(exc).__name__, exc))
                if self._closed.wait(self._keepalive_s):
                    return
        thread = threading.Thread(target=run, name="evm-tx-read-ws-keepalive", daemon=True)
        thread.start()
        return thread

    def _exchange(self, method, params):
        if self._ws is None:
            self._ws, self._born = self._connect(self.url, self._timeout_s), self._clock()
        self._id += 1
        rid = self._id
        self._ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        end = time.monotonic() + self._timeout_s
        while True:
            answer = json.loads(self._ws.recv(timeout=max(0.0, end - time.monotonic())))
            if isinstance(answer, dict) and answer.get("id") == rid:
                return answer

    def read(self, method, params, _retry=True):
        """A failed connect cools the host `cooldown_s`; a failed or slow read on an open connection
        only `read_cooldown_s` (Gamma LOW). A null is asked once more after `null_retry_s`: the
        pushing node usually has a head-block receipt a moment later, and the read host is null-first
        too (Gamma LOW)."""
        if not self._lock.acquire(timeout=self._timeout_s):
            return None, "busy"
        try:
            if self._cool_until is not None and self._clock() < self._cool_until:
                return None, "cooling"
            try:
                answer = self._exchange(method, params)
            except Exception as exc:
                ws, self._ws = self._ws, None
                cool = self._cooldown_s if ws is None else self._read_cooldown_s
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self._cool_until = self._clock() + cool
                _say("[onchain-evm] %s tx-read websocket %s failed (%s): cooling it %.0f s, reading "
                     "from the read host meanwhile" % (self.name, "connect" if ws is None else "read",
                                                       type(exc).__name__, cool))
                return None, "transport"
        finally:
            self._lock.release()
        if "error" in answer:
            return None, "rpc_error"
        if answer.get("result") is None:
            if _retry and self._null_retry_s > 0:
                self._sleep(self._null_retry_s)
                return self.read(method, params, _retry=False)
            return None, "null"
        return answer["result"], None

    def close(self):
        """End the connection and its receive thread: websockets' sync client runs a NON-daemon thread
        when opened from the main thread, and an open one holds the process at exit."""
        self._closed.set()
        with self._lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


class WsTxReadPair:
    """The tx and the receipt each on its own kept socket (2026-10-14). One WsTxReadHost served them
    in turn, and host_ms showed it: over 401 reads on 56 (10-13/14) the receipt trailed the tx by
    p50 36 ms, and the pair took p50 63 / p90 119 ms. Same two calls on the same host; each socket
    keeps its own lock, cooling, null retry and keepalive, and a miss on either kind still falls
    back to the read host (_host_first)."""

    def __init__(self, tx, receipt):
        self.url, self.hosts = tx.url, {"tx": tx, "receipt": receipt}

    def read(self, method, params):
        return self.hosts["receipt" if _TX_READS.get(method) == "receipt" else "tx"].read(method, params)

    def start_keepalive(self):
        return [host.start_keepalive() for host in self.hosts.values()]

    def close(self):
        for host in self.hosts.values():
            host.close()


_SELL_HOSTS = {}


def _sell_host(url, clock, name, meanwhile=None):
    key = (url, name, meanwhile)
    host = _SELL_HOSTS.get(key)
    if host is None:
        from sniper.settings import memecoin as cfg
        host = _SELL_HOSTS.setdefault(key, TxReadHost(
            url, cooldown_s=getattr(cfg, "MEMECOIN_ONCHAIN_EVM_TX_READ_COOLDOWN_S", 60.0),
            timeout_s=getattr(cfg, "MEMECOIN_ONCHAIN_EVM_TX_READ_TIMEOUT_S", 1.5), clock=clock,
            name=name, meanwhile=meanwhile))
    return host


def _sell_read_fn(url, clock=time.monotonic, fallback=None):
    """The sell shadow's read function on its OWN direct POST to `url` (Gamma blocker, 2026-09-29).

    Never evm_wallet._rpc: publicnode is an exact member of the funded pool on 4663 and 8453, and
    _rpc expanded a pooled URL to custody/preconf/Alchemy, hedged, and let a sell-side 429 penalize
    and quarantine in the process-wide pool. This is a TxReadHost of its own per URL: a 403/429 cools
    only it, and any failure returns None, which decode_sell treats as unread.

    `fallback` (sell_fallback_url) is a second TxReadHost, asked on any miss of the first (2026-10-07,
    8453: publicnode 403s every receipt read)."""
    host = _sell_host(url, clock, "sell",
                      "reading from the fallback host meanwhile" if fallback else None)
    backup = _sell_host(fallback, clock, "sell_fallback") if fallback else None

    def read(method, params):
        result, _why = host.read(method, params)
        if result is None and backup is not None:
            result, why = backup.read(method, params)
            if result is not None:
                _sell_count("fallback_reads")
            elif why == "http_429":           # its key is the funded pool's (Gamma LOW)
                _sell_count("fallback_429")
        return result
    return read


def tx_read_host_from_env(*, read_transport=None):
    """The configured TxReadHost for this chain, or None (MEMECOIN_ONCHAIN_EVM_TX_READ_URL_<chain>
    empty: the read host only, exactly as before). MEMECOIN_ONCHAIN_EVM_TX_READ_WS_URL_<chain> wins when
    set: the same reads over the provider's websocket (C4(b), 2026-10-01). The sell shadow reads
    neither through here (sell_read_url takes only the HTTP knob)."""
    from sniper.settings import memecoin as cfg
    ws = (os.getenv("MEMECOIN_ONCHAIN_EVM_TX_READ_WS_URL_%s" % CHAIN) or "").strip()
    if ws:
        host = WsTxReadPair(*(WsTxReadHost(
            ws, cooldown_s=getattr(cfg, "MEMECOIN_ONCHAIN_EVM_TX_READ_COOLDOWN_S", 60.0),
            timeout_s=getattr(cfg, "MEMECOIN_ONCHAIN_EVM_TX_READ_TIMEOUT_S", 1.5),
            read_cooldown_s=getattr(cfg, "MEMECOIN_ONCHAIN_EVM_TX_READ_WS_READ_COOLDOWN_S", 10.0),
            name=name) for name in ("buy", "buy_receipt")))
        host.start_keepalive()
        return host
    url = (os.getenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_%s" % CHAIN) or "").strip()
    if not url:
        return None
    return TxReadHost(url, cooldown_s=getattr(cfg, "MEMECOIN_ONCHAIN_EVM_TX_READ_COOLDOWN_S", 60.0),
                      timeout_s=getattr(cfg, "MEMECOIN_ONCHAIN_EVM_TX_READ_TIMEOUT_S", 1.5),
                      **({"post_fn": read_transport.post} if read_transport is not None else {}))


def _host_first(base_read, host, outcome):
    """A read_fn: the tx and receipt from `host` first, then from the read host on any miss. Every
    other method goes to the read host unchanged. Records which host answered and how long it took."""
    def read(method, params):
        kind = _TX_READS.get(method)
        if kind is None:
            return base_read(method, params)
        if host is not None:
            started = time.perf_counter()
            result, miss = host.read(method, params)
            if result is not None:
                outcome["hosts"][kind] = "tx_read"
                outcome["host_ms"][kind] = round((time.perf_counter() - started) * 1000.0, 1)
                return result
            outcome.setdefault("tx_read_miss", {})[kind] = miss
        started = time.perf_counter()
        try:
            result = base_read(method, params)
            outcome.get("read_errors", {}).pop(kind, None)      # the reason follows the last read
        except Exception as exc:
            # A FAILED READ IS NOT A CRASH AND NOT A NULL (2026-10-07). It raised out of the push
            # handler, which dropped the transfer unnamed. Now it is re-read like a null within the
            # same budget and, if it never answers, the outcome is read_error with its words.
            outcome.setdefault("read_errors", {})[kind] = ("%s: %s" % (type(exc).__name__, exc))[:200]
            result = None
        outcome["hosts"][kind] = "read_host"
        outcome["host_ms"][kind] = round((time.perf_counter() - started) * 1000.0, 1)
        return result
    return read


def _hosts_note(outcome):
    """`tx_host=tx_read/52ms receipt_host=read_host/310ms [tx_read_miss=receipt:http_403]`."""
    ms = outcome.get("host_ms") or {}
    parts = ["%s_host=%s/%.0fms" % (kind, host, ms.get(kind) or 0.0)
             for kind, host in sorted((outcome.get("hosts") or {}).items())]
    misses = outcome.get("tx_read_miss") or {}
    if misses:
        parts.append("tx_read_miss=%s" % ",".join("%s:%s" % kv for kv in sorted(misses.items())))
    return " ".join(parts)


def push_detection(rpc_url, log, watched, *, decimals_cache, quote_usd, now, read_fn=None, payment_price_fn=None,
                   outcome=None, sleep_fn=time.sleep, clock=time.monotonic, tx_read_host=None,
                   pool_key_fn=None, preconf=False, read_transport=None):
    """Fetch a delivered Transfer's transaction and decode the buy, or None. NO WRITE HAPPENS HERE.

    FETCH FIRST, WRITE LAST -- the same rule the poll loop follows and for the same reason: a row
    written before these reads holds the FUNDED ledger's write lock across every RPC round trip,
    and the runner's own writes then fail with `database is locked` (measured about once every two
    hours before 2026-09-05). The caller writes the returned row and commits, once.

    `outcome` (a dict, filled in place) says why: detected, tx_null, receipt_null or no_buy, plus
    `retries`, so a delivered Transfer is never dropped without a reason (2026-09-28).

    `preconf` (Base, spec 2026-10-09-memecoin-base-flashblocks-detection-design.md): a Flashblocks
    pending log, read from the preconf host. A null tx block hash is that read's normal shape, so it
    is not re-read as a mempool copy, and the Relay decoder binds tx and receipt by block number and
    index instead of hash.
    """
    outcome = outcome if outcome is not None else {}
    outcome.update(reason="no_buy", retries=0, hosts={}, host_ms={})
    result_read = read_transport.result if read_transport is not None else _rpc_result
    read_fn = _host_first(read_fn or (lambda method, params: result_read(rpc_url, method, params)),
                          tx_read_host, outcome)
    tx_hash = str(log.get("transactionHash") or "").lower()
    if not tx_hash:
        return None
    # The mint is the Transfer log's own address, known at arrival: read its decimals beside the
    # transaction instead of after it (2026-09-28). Written to the cache on this thread only.
    mint_hint = str(log.get("address") or "").lower()
    decimals_read = read_transport.decimals if read_transport is not None else erc20_decimals
    def metadata():
        start = time.monotonic_ns()
        tap, evidence = None, None
        try:
            tap = start_rpc_tap()
        except Exception:
            pass
        try:
            value = decimals_read(mint_hint, rpc_url)
        except Exception:
            value = None                 # preserve the existing unknown/fallback behavior
        finally:
            end = time.monotonic_ns()
        try:
            calls = take_rpc_tap()  # Helper attempts; internal pool rotations are opaque.
            if tap is not None:
                if type(calls) is not list or not 1 <= len(calls) <= 2:
                    raise ValueError('prefetch RPC evidence unavailable')
                outcomes, rpc_ms = [], 0.
                for call in calls:
                    rtt, result = call['rtt_ms'], call['outcome']
                    if type(rtt) not in (int, float) or not math.isfinite(rtt) or rtt < 0:
                        raise ValueError('prefetch RPC duration unavailable')
                    if type(result) is not str:
                        raise ValueError('prefetch RPC outcome unavailable')
                    if result in ('ok', 'timeout') or re.fullmatch(r'http_[1-5][0-9]{2}', result):
                        outcomes.append(result)
                    elif result == 'rpc_error' or result.startswith('rpc_error:'):
                        outcomes.append('rpc_error')
                    elif result.startswith('raise:') and result[6:].isidentifier():
                        outcomes.append('raise')
                    else:
                        raise ValueError('prefetch RPC outcome unavailable')
                    rpc_ms += rtt
                wall_ms = (end - start) / 1_000_000.
                if not math.isfinite(rpc_ms) or not math.isfinite(wall_ms) or wall_ms < 0:
                    raise ValueError('prefetch RPC duration unavailable')
                evidence = dict(count=len(calls), resolved=value is not None,
                                outcomes=outcomes, rpc_ms=rpc_ms, wall_ms=wall_ms)
        except Exception:
            pass                         # Diagnostics cannot invalidate metadata or cause a new read.
        return value, start, end, evidence
    prefetch = (_reader().submit(metadata)
                if mint_hint and mint_hint not in decimals_cache else None)
    _receipt_stage(outcome, "proof_read_start")
    tx, receipt = _read_pair(read_fn, tx_hash)
    if prefetch is not None:
        _receipt_stage(outcome, "metadata_wait_start")
        try:
            value, metadata_start, metadata_end, evidence = prefetch.result()
            outcome['metadata_prefetch_rpc_attempts'] = evidence
            _receipt_stage(outcome, "metadata_read_start", monotonic_ns=metadata_start)
            _receipt_stage(outcome, "metadata_read_end", monotonic_ns=metadata_end)
        except Exception:
            value = None
        _receipt_stage(outcome, "metadata_wait_end")
        if value is not None:
            decimals_cache[mint_hint] = value
    tx, receipt = _retry_null_reads(read_fn, tx_hash, tx, receipt, outcome, sleep_fn=sleep_fn, clock=clock,
                                    preconf=preconf)
    _receipt_stage(outcome, "proof_read_end")
    # THE DECODE, SPLIT (2026-09-28): DegenCapitalLLC's relay buy spent 1.1 s here after 45 ms of
    # reads, and nothing said where. Milliseconds per step go into the row's source_valuation_json.
    decode_started = time.perf_counter()
    spent = {"chain_id": 0.0, "code": 0.0, "decimals": 0.0, "price": 0.0}
    owners = {str((tx or {}).get("from") or "").lower()}
    for transfer in (receipt or {}).get("logs") or []:
        topics = transfer.get("topics") or []
        if len(topics) == 3 and topics[0] == TRANSFER_TOPIC:
            owners.add(_topic_address(topics[2]))

    def decimals(mint):
        if mint not in decimals_cache:
            started = time.perf_counter()
            value = decimals_read(mint, rpc_url)
            spent["decimals"] += (time.perf_counter() - started) * 1000.0
            if value is None:
                raise RuntimeError("token decimals unavailable")
            decimals_cache[mint] = value
        return decimals_cache[mint]

    rows = []
    for owner in sorted(owners & watched.keys()):
        buy = direct_buy_from_receipt(
            receipt, tx, owner, weth=WETH, quote_usd=quote_usd, decimals_fn=decimals, chain=CHAIN,
            quote_decimals=CHAIN_QUOTE[CHAIN]["decimals"])
        if buy:
            outcome["payment_token"] = WETH
        if buy is None and CHAIN in RELAY_ENVELOPE_CHAINS:
            from sniper.services.memecoin_evm_onchain_watch import read_relayer_purchase, price_relayer_purchase
            from sniper.settings import memecoin as cfg
            evidence = {}
            _receipt_stage(outcome, "relay_proof_start")
            proof = read_relayer_purchase(receipt, tx, owner, chain=CHAIN, read_fn=read_fn, any_route=True,
                                          evidence=evidence, live=True,
                                          code_ttl_s=cfg.MEMECOIN_ONCHAIN_RELAY_CODE_TTL_S,
                                          preconfirmed=preconf)
            _receipt_stage(outcome, "relay_proof_end")
            spent["chain_id"] += evidence.get("chain_id_ms", 0.0)
            spent["code"] += evidence.get("code_ms", 0.0)
            if evidence.get("code_basis"):
                outcome["code_basis"] = evidence["code_basis"]
            if proof:
                outcome["payment_token"] = proof.get("payment_token")
                base_price = payment_price_fn or (lambda token: _payment_usd_price(token, quote_usd))

                def timed_price(token):
                    started = time.perf_counter()
                    try:
                        return base_price(token)
                    finally:
                        spent["price"] += (time.perf_counter() - started) * 1000.0
                buy = price_relayer_purchase(proof, decimals_fn=decimals, price_fn=timed_price)
            # WHICH STEP REFUSED goes on the no_buy line (2026-09-28): the 11:56:31Z miss printed a
            # bare no_buy and its cause had to be rebuilt from a replay.
            if not buy:
                outcome["decode"] = evidence.get("reason") or ("relay_price_unavailable" if proof else None)
        if buy:
            identity = watched[owner]
            if isinstance(buy.get("source_initiation"), dict):
                buy["source_initiation"] = dict(buy["source_initiation"], source_user_id=identity[0])
            rows.append(dict(tx_hash=tx_hash, wallet=owner, source_user_id=identity[0],
                             handle=identity[1], buy=buy, log=log, now=now))
    total = (time.perf_counter() - decode_started) * 1000.0
    outcome["decode_ms"] = dict({k: round(v, 1) for k, v in spent.items()},
                                rest=round(max(0.0, total - sum(spent.values())), 1))
    if not rows and outcome.get("decode") == "unsupported_relayer_swap":
        # Fixtures for the 56 Relay decoder (2026-10-02): queued, never written on this thread.
        capture_unsupported_relay(tx, receipt, owners & watched.keys(), arrival=now)
    if len(rows) == 1:
        outcome["reason"] = "detected"
        outcome["_receipt"] = receipt          # memory only: the BSC fast lane decides from it
        outcome["_transaction"] = tx           # memory only: off-path Relay origin verifier, and the
                                               # Base lane's PoolKeys sit in its calldata
        outcome["decimals"] = decimals_cache.get(rows[0]["buy"]["mint"])
        # THE SOURCE'S POST-TRADE FACTS (2026-09-28), from the receipt already in hand: pure decode
        # plus a cached read-only PoolKey lookup, never a network call. The runner binds them to the
        # pool its quote trades (memecoin_evm_post_trade); source_post_trade never raises.
        from sniper.services.memecoin_evm_post_trade import source_post_trade
        started = time.perf_counter()
        outcome["evm_post"] = dict(
            source_post_trade(CHAIN, receipt, rows[0]["buy"]["mint"], owner=rows[0]["wallet"],
                              pool_key_fn=pool_key_fn),
            ms=round((time.perf_counter() - started) * 1000.0, 2))
        rows[0]["buy"]["source_post"] = outcome["evm_post"]
        return rows[0]
    errors = outcome.get("read_errors") or {}
    outcome["reason"] = ("read_error" if (tx is None and "tx" in errors)
                         or (tx is not None and receipt is None and "receipt" in errors)
                         else "tx_null" if tx is None else "receipt_null" if receipt is None
                         else "ambiguous" if rows else "no_buy")
    return None


def _detection_complete(outcome):
    """Unknown RPC/valuation results are retryable, never a negative buy decision."""
    return (outcome.get("reason") in ("detected", "no_buy", "ambiguous")
            and outcome.get("decode") not in ("contract_code_unavailable", "relay_price_unavailable"))


def _retry_null_reads(read_fn, tx_hash, tx, receipt, outcome, *, sleep_fn, clock, preconf=False):
    """Re-read a NULL tx or receipt (2026-09-28): the WS provider can deliver a Transfer before the
    read node has indexed its block. Bounded by MEMECOIN_ONCHAIN_EVM_NULL_READ_RETRY_S AND
    _MAX_RETRIES; only a null is re-read, and every retry says what it got. 0 = one read."""
    from sniper.settings import memecoin as cfg
    budget = float(getattr(cfg, "MEMECOIN_ONCHAIN_EVM_NULL_READ_RETRY_S", 0.0) or 0.0)
    attempts = int(getattr(cfg, "MEMECOIN_ONCHAIN_EVM_NULL_READ_MAX_RETRIES", 0) or 0)
    if budget <= 0 or attempts <= 0:
        return tx, receipt
    # A PRECONFIRMED READ IS NOT A MEMPOOL COPY (2026-10-09): its tx block hash is null until the block
    # seals, by design, so only a null tx or receipt is re-read.
    unmined = (lambda _tx, _receipt: False) if preconf else _tx_unmined
    if not preconf:
        tx = _mined_copy(read_fn, tx_hash, tx, receipt, outcome)
    deadline = clock() + budget
    step = budget / (attempts + 1)
    # A CHAIN'S OWN STEP (2026-09-29, spec 2026-09-29-memecoin-evm-null-read-step-design.md). On 56
    # the read host shows a block p50 0.28 s after it, and the push socket delivers at p50 0.13 s, so
    # the fixed 0.4 s step overslept every buy. The attempts run to the SAME budget, so no transfer
    # is given up on sooner than before (the epsilon keeps 2.0 / 0.1 at 20, not 19).
    chain_step = cfg.memecoin_onchain_evm_null_read_step_s(CHAIN)
    if chain_step:
        step, attempts = chain_step, max(attempts, int(budget / chain_step + 1e-9))
    while ((tx is None or receipt is None or unmined(tx, receipt))
           and outcome["retries"] < attempts and clock() < deadline):
        sleep_fn(step)
        outcome["retries"] += 1
        need_tx = tx is None or unmined(tx, receipt)
        if need_tx and receipt is None:
            # BOTH AT ONCE (D2): one round trip instead of two, the same two reads.
            tx, receipt = _read_pair(read_fn, tx_hash)
        elif need_tx:
            tx = read_fn("eth_getTransactionByHash", [tx_hash])
        elif receipt is None:
            receipt = read_fn("eth_getTransactionReceipt", [tx_hash])
        if not preconf:
            tx = _mined_copy(read_fn, tx_hash, tx, receipt, outcome)
        _say("[onchain-evm] null read retry %d tx=%s tx_read=%s receipt_read=%s"
             % (outcome["retries"], tx_hash[:12],
                "null" if tx is None else ("pending" if _tx_unmined(tx, receipt) else "ok"),
                "ok" if receipt is not None else "null"))
    return tx, receipt


def _mined_copy(read_fn, tx_hash, tx, receipt, outcome):
    """The MINED tx once the receipt is mined, read by (blockHash, transactionIndex) (2026-09-28).

    On 56 the read host kept answering getTransactionByHash from its mempool for the whole retry
    budget while the receipt was already mined: 18 of 21 relay buys after c38b768e read
    tx_read=pending on every retry and were refused. A block-and-index read is always the mined
    copy, so it is asked once, only when the receipt is mined and the tx is not. The answer is used
    only if it carries the same hash; anything else keeps the tx we had."""
    if receipt is None or not _tx_unmined(tx, receipt):
        return tx
    try:
        mined = read_fn("eth_getTransactionByBlockHashAndIndex",
                        [receipt["blockHash"], receipt["transactionIndex"]])
    except Exception as exc:
        outcome["mined_copy"] = "error:%s" % type(exc).__name__
        return tx
    if isinstance(mined, dict) and str(mined.get("hash") or "").lower() == tx_hash:
        outcome["mined_copy"] = "ok"
        return mined
    outcome["mined_copy"] = "mismatch" if mined else "null"
    return tx


def _tx_unmined(tx, receipt):
    """The tx as a read node's MEMPOOL returns it (2026-09-28): `blockHash` present and null, or a
    block other than the receipt's. On 56 the read host answers the tx from its mempool while the
    receipt is still null; the null-read retry re-read only the receipt, so the tx stayed pending,
    the Relay proof's tx-block == receipt-block check refused it, and 279 of 305 decode refusals on
    56 carried retries=1 (47 J7-proved buys that arrived ~1.2 s before J7). A tx dict with no
    `blockHash` key at all (a partial fixture) is not judged."""
    if not isinstance(tx, dict) or "blockHash" not in tx:
        return False
    if not tx["blockHash"]:
        return True
    mined = (receipt or {}).get("blockHash")
    return bool(mined) and str(mined).lower() != str(tx["blockHash"]).lower()


J7_XCHECK_PERIOD_S = 5.0
_J7_DELIVERED_MAX = 4096


class J7CrossCheck:
    """J7 as the liveness reference for the socket's topic-only subscriptions (2026-09-29).

    Spec 2026-09-29-memecoin-evm-watch-dead-subscription-design.md. After the 05:30:42Z socket-error
    reconnect the 4663 transfers and sells filters were acked and delivered nothing for 22+ minutes
    while heads and Initialize flowed, and nothing could tell: the filters are sparse by design. J7
    reports the same wallets' trades about a second after their blocks (the socket delivered 19 of 19
    J7 watched buys on healthy connections, 0 of 14 on the two dead ones). A J7 event by an acked
    address on this chain, received on the current connection, whose tx the socket did not deliver
    within `grace_s`, is a miss; a miss sets `force` (the push thread takes it and reconnects), at
    most once per `min_gap_s`. Silence proves nothing and a failed ledger read is `unknown`."""

    def __init__(self, chain, *, grace_s, min_gap_s):
        self.chain, self.grace_s, self.min_gap_s = str(chain), float(grace_s), float(min_gap_s)
        self.last_id = None
        self.pending = {}                    # tx -> (J7 received ts, side, trader)
        self.delivered = {}                  # tx -> socket arrival ts, bounded
        self.lock = threading.Lock()
        self.force = threading.Event()
        self.stop = threading.Event()
        self.last_forced = None
        self.stats = {"checked": 0, "missed": 0, "forced": 0, "suppressed": 0, "unknown": 0,
                      "unparsable": 0, "known_prior": 0, "last_miss": None}

    def delivered_tx(self, tx_hash):
        """The socket passed a log of this tx to a handler (any outcome). Socket thread."""
        with self.lock:
            self.delivered[str(tx_hash).lower()] = _wall_clock()
            while len(self.delivered) > _J7_DELIVERED_MAX:
                self.delivered.pop(next(iter(self.delivered)))

    def ingest(self, rows, *, watched, sells_live, connected_at):
        for rid, fid, received, side, network_id, lane in rows:
            self.last_id = rid if self.last_id is None else max(self.last_id, rid)
            if fid is None or str(network_id) != self.chain or lane != "j7_wallets":
                continue
            parts = str(fid).split(":")
            try:
                if len(parts) < 5 or parts[0] != "swap":
                    raise ValueError(fid)
                tx, trader = parts[2].lower(), parts[3].lower()
                at = datetime.fromisoformat(str(received).replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                self.stats["unparsable"] += 1
                continue
            if trader not in watched or side not in ("buy", "sell"):
                continue
            if side == "sell" and not sells_live:
                continue
            if connected_at is None or at < connected_at:
                continue                     # asked of an earlier socket
            self.pending[tx] = (at, side, trader)

    def due(self, now, known_fn=None):
        """The misses whose grace ran out now; sets `force` for them unless inside the min gap.

        `known_fn(tx)` looks the tx up among the watcher's own detections in the ledger (D2 read
        2026-09-30: J7 reported a 56 buy 32 min after the watcher had detected it, the bounded
        in-memory set had rolled over, and a needless reconnect was forced). A detected tx is
        delivered, never a miss. An unreadable lookup falls back to the in-memory verdict: a
        needless reconnect is cheap, a dead socket missed is not."""
        with self.lock:
            delivered = set(self.delivered)
        misses = []
        for tx, (at, side, trader) in list(self.pending.items()):
            if tx in delivered:
                self.stats["checked"] += 1
                del self.pending[tx]
            elif now - at >= self.grace_s:
                del self.pending[tx]
                try:
                    known = bool(known_fn(tx)) if known_fn is not None else False
                except Exception:
                    known = False
                if known:
                    self.stats["known_prior"] += 1
                    _say("[onchain-evm] j7 xcheck known_prior tx=%s side=%s age_s=%.1f total=%d"
                         % (tx[:12], side, now - at, self.stats["known_prior"]))
                    continue
                self.stats["missed"] += 1
                self.stats["last_miss"] = tx[:12]
                misses.append({"tx": tx, "side": side, "trader": trader, "age_s": round(now - at, 1)})
        if misses:
            if self.last_forced is None or now - self.last_forced >= self.min_gap_s:
                self.last_forced = now
                self.stats["forced"] += 1
                self.force.set()
            else:
                self.stats["suppressed"] += 1
        return misses


def j7_xcheck_tick(xc, read_fn, *, watched, sells_live, connected_at, now, known_fn=None):
    """One cross-check pass: read J7 events after the cursor, ingest, and return the misses due now.
    A failed read is `unknown`: no verdict, nothing forced."""
    try:
        rows = read_fn(xc.last_id)
    except Exception:
        xc.stats["unknown"] += 1
        return []
    xc.ingest(rows, watched=watched, sells_live=sells_live, connected_at=connected_at)
    return xc.due(now, known_fn=known_fn)


def _detected_before(conn, chain, tx):
    """The watcher already detected this tx on this chain: a row in onchain_wallet_detections, whose
    signature is '<chain>:<tx>:<mint>'. Buys only (sells have no detection row)."""
    # A range seek on UNIQUE(signature), not LIKE: case-insensitive LIKE cannot use the index (Gamma
    # LOW, 2026-09-30). The writer lowercases the tx (_record); ';' sorts right after ':'.
    prefix = "%s:%s:" % (chain, str(tx).lower())
    return conn.execute("SELECT 1 FROM onchain_wallet_detections WHERE signature >= ? AND signature < ? "
                        "LIMIT 1", (prefix, prefix[:-1] + ";")).fetchone() is not None


def _start_j7_xcheck(xc, tick):
    """The cross-check's own daemon thread: off the socket thread, every J7_XCHECK_PERIOD_S."""
    def loop():
        while not xc.stop.wait(J7_XCHECK_PERIOD_S):
            try:
                tick()
            except Exception as exc:
                _say("[onchain-evm] j7 cross-check tick failed: %s" % type(exc).__name__)
    threading.Thread(target=loop, name="evm-j7-xcheck", daemon=True).start()


class _Decided:
    """Tx hashes the push watcher decided recently (2026-09-28). push_detection decides a tx for
    EVERY watched owner in it, so another Transfer log of the same tx can only repeat the answer:
    one 4663 tx arrived as 11 logs in 0.85 s and cost 22 reads, the burst that earns a publicnode
    403. Oldest-first pruning keeps it bounded by the window AND `cap`."""

    def __init__(self, clock=time.monotonic, cap=4096):
        self._at, self._clock, self._cap = {}, clock, cap

    def seen(self, tx_hash, window_s):
        now = self._clock()
        while self._at:
            oldest = next(iter(self._at))
            if now - self._at[oldest] <= window_s and len(self._at) <= self._cap:
                break
            del self._at[oldest]
        return tx_hash in self._at

    def add(self, tx_hash):
        self._at.pop(tx_hash, None)        # re-insert at the end so the dict stays oldest-first
        self._at[tx_hash] = self._clock()


class _PublicationFence:
    """Close admission before shutdown; only already-claimed local publications may finish."""

    def __init__(self):
        self.closed = threading.Event()
        self._condition, self._active = threading.Condition(), 0

    def call(self, fn, *args, **kwargs):
        with self._condition:
            if self.closed.is_set():
                raise RuntimeError("watcher_stopped")
            self._active += 1
        try:
            return fn(*args, **kwargs)
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def close(self, *, timeout_s=5.0):
        with self._condition:
            self.closed.set()
            return self._condition.wait_for(lambda: not self._active, timeout=timeout_s)


class _CanonicalDecodeWork:
    """Existing bounded decoder with an unfinished source-block restart frontier."""

    def __init__(self, decode, *, max_age_s, workers=2, cap=128, now_fn=None, on_stop=None, on_closed=None,
                 receipt_clock=None):
        self._lock, self._pending, self._failed = threading.RLock(), {}, None
        self._cap, self._full, self._stops, self._duplicates = cap, 0, 0, 0
        self._receipt_clock = receipt_clock

        def stopped():
            try:
                if on_stop is not None:
                    on_stop()
            finally:
                with self._lock:
                    self._stops += 1
                    last = self._stops == workers
                if last and on_closed is not None:
                    on_closed()

        self._work = P.DecodeWork(decode, workers=workers, cap=cap, max_age_s=max_age_s,
                                  now_fn=now_fn, on_stop=stopped)

    def _unread(self, block):
        self._failed = ((min(block, self._failed[0]), max(block, self._failed[1]))
                        if self._failed else (block, block))

    def submit(self, log, arrival, roster):
        from concurrent.futures import Future
        tx = str(log.get("transactionHash") or "").lower()
        block = _quantity(log.get("blockNumber"))
        block = block if block is not None and block >= 0 else 0
        with self._lock:
            anchor = log.get("_receipt_clock")
            if anchor is not None and self._receipt_clock is not None:
                if tx:
                    anchor = self._receipt_clock.remember(("source_transaction", tx), anchor)
                stages = {"_receipt_clock": anchor}
                _receipt_stage(stages, "decode_queue")
                log = dict(log, _receipt_clock=anchor, _receipt_stages=tuple(stages["receipt_stages"]))
            if tx in self._pending:
                self._duplicates += 1
                return self._pending[tx][1]
            if len(self._pending) >= self._cap:
                self._full += 1
                self._unread(block)
                future = Future()
                future.set_result(None)
                return future
            # Hold before admission. Future completion may race the socket's next head.
            self._pending[tx] = (block, None)
            # In-flight tx dedupe is ours; the canonical path retains its configured decided TTL.
            future = self._work.submit((tx, object()), (dict(log), dict(roster)), arrival)
            self._pending[tx] = (block, future)

            def done(completed):
                try:
                    complete = completed.result() is True
                except Exception:
                    complete = False
                with self._lock:
                    if not complete:
                        self._unread(block)
                    self._pending.pop(tx, None)
            future.add_done_callback(done)
            return future

    def frontier(self, block):
        with self._lock:
            floors = [number - 1 for number, _ in self._pending.values()]
            if self._failed is not None:
                floors.append(self._failed[0] - 1)
            if block is not None:
                floors.append(block)
            return min(floors) if floors else None

    def drain_failed(self):
        with self._lock:
            failed, self._failed = self._failed, None
            return failed

    def snapshot(self):
        with self._lock:
            return dict(self._work.snapshot(), full=self._full, duplicate=self._duplicates,
                        unfinished=len(self._pending), unread_range=self._failed,
                        stopped_workers=self._stops,
                        receipt_anchor_evicted=self._receipt_clock.evicted if self._receipt_clock else 0)

    def close(self, *, wait=False):
        self._work.close(wait=wait)


def _warm_relay(read_fn, ttl_s):
    """Pin the chain identity and verify the pinned Relay/Kyber code before a buy arrives (2026-09-28).
    A failure here changes nothing: the detection path verifies inline, as it always has."""
    from sniper.services.memecoin_evm_onchain_watch import pin_chain_identity, warm_relay_code
    pinned = pin_chain_identity(CHAIN, read_fn)
    verdicts = warm_relay_code(CHAIN, read_fn, ttl_s=ttl_s)
    _say("[onchain-evm] relay code warm chain_id=%s %s"
         % ("pinned" if pinned else "unpinned",
            " ".join("%s=%s" % (c[:10], v) for c, v in sorted(verdicts.items()))))


def _relay_warm_loop(warm, *, ttl_s, stop):
    """Re-verify at TTL/3; BSC uses 30s because its public connection survived 35s idle but closed
    after 201s. Proof TTL is unchanged. Stops when `stop` is set."""
    interval = _relay_warm_interval(CHAIN, float(ttl_s))
    while not stop.wait(interval):
        try:
            warm()
        except Exception as exc:
            _say("[onchain-evm] relay code warm failed (%s: %s)" % (type(exc).__name__, exc))


_BACKFILL_PAGE_BLOCKS = 10          # Alchemy's free tier refuses a wider eth_getLogs range (HTTP 400)


def _backfill_post(url, method, params):
    """One JSON-RPC call to the backfill endpoint: the result, or an exception naming the failure.
    The URL carries the API key, so nothing here may print or raise it."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                          "User-Agent": "curl/8.5.0"})
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            answer = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError("HTTP %s" % exc.code) from None
    except Exception as exc:
        raise RuntimeError(type(exc).__name__) from None
    if answer.get("error"):
        raise RuntimeError("JSON-RPC %s" % (answer["error"].get("code"),))
    return answer.get("result") or []


def _backfill_thread(target):
    threading.Thread(target=target, name="evm-gap-backfill", daemon=True).start()


def _backfill_url(chain):
    """Explicit log-read host, otherwise the chain's Alchemy pool member."""
    explicit = os.environ.get("MEMECOIN_ONCHAIN_EVM_BACKFILL_RPC_URL_%s" % chain, "").strip()
    if explicit:
        return explicit
    from sniper.settings import memecoin as cfg
    for url in (getattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {}) or {}).get(str(chain), []) or []:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        if host.endswith(".g.alchemy.com"):
            return url
    return None


def _chain_blocks_per_s(state):
    """The chain's own block rate from the delivered heads' numbers and timestamps, or None."""
    window = getattr(state, "window", None) or []
    if len(window) < 2:
        return None
    (b0, t0, _w0), (b1, t1, _w1) = window[0], window[-1]
    return (b1 - b0) / (t1 - t0) if t1 > t0 and b1 > b0 else None


def _valuation(quote_usd, age_s, source):
    return {"basis": "evm_onchain_watch", "quote_usd": quote_usd, "quote_price_age_s": age_s,
            "quote_price_source": source, "decoded_at": now_iso()}


def _poll_backoff(period_s, streak):
    """The wait after `streak` consecutive poll failures: the period, doubling, capped at 30 s."""
    # The exponent is capped: an uncapped one overflows float after ~1,024 failures, and a long
    # provider outage would then crash the poll loop it is meant to slow down.
    return min(30.0, max(float(period_s or 0.0), 0.25) * 2 ** min(max(0, streak - 1), 16))


LANE_WINDOW_COOLDOWN_S = 60.0   # per chain: one lane-window backfill per minute (Alpha, 2026-09-29)
_DARK_LOGS_MIN_GAP_S = 1.0   # ponytail: one Alchemy getLogs a second while dark; a knob if it binds
_DARK_LOGS_LAST = [None]
# DARK-POLL COVERAGE (2026-10-17, Alpha/Gamma): 56's push was refused 38 times that day and each
# dark window's poll left no line, so a buy inside one could not be checked. Per process: windows
# polled, blocks read, logs found, detections committed; the push heartbeat carries a copy.
_DARK_POLL_STATS = {"windows": 0, "blocks": 0, "logs": 0, "detections": 0}


def _dark_logs(url, from_block, to_block, topics):
    """The dark poll's eth_getLogs on the chain's BACKFILL host (2026-09-29, Alpha GO). The read host
    refused every topic getLogs on 56 (-32005), so a dark socket read nothing. Paced to one call a
    second: the key is shared with the funded pool on 56. Raises like `_backfill_post`."""
    last = _DARK_LOGS_LAST[0]
    if last is not None:
        wait = _DARK_LOGS_MIN_GAP_S - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _DARK_LOGS_LAST[0] = time.monotonic()
    logs = _backfill_post(url, "eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(to_block),
                                               "topics": topics}])
    if not isinstance(logs, list):
        raise ValueError("non-list eth_getLogs result")
    return logs


@_pooled_watcher_reads
def run_poll(*, duration_s=None, period_s=1.0, price=None, logs_url=None, start_block=None,
             max_behind=None, read_transport=None):
    """Poll armed-wallet transfers; returns the last block whose logs were read (None: none).

    `logs_url` / `start_block` (2026-09-29): the push stream's dark fallback reads eth_getLogs on the
    chain's backfill host from the last block the push delivered, never further back than
    `max_behind` blocks (the lane's age window at the chain's measured rate). Pure poll mode passes
    none of them."""
    behind_cap = MAX_BEHIND_BLOCKS if max_behind is None else min(MAX_BEHIND_BLOCKS, int(max_behind))
    # A RESEARCH watcher must not share the FUNDED custody endpoint. `evm_rpc_for_chain` resolves
    # MEMECOIN_EVM_RPC_URL_4663, which is the Alchemy key the funded quote and exit path uses. At a
    # 10-block getLogs cap and one 4663 block per ~0.10s this poller needs ~1 req/s sustained just
    # to keep up, against a path where a throttled exit quote is money. Measured 2026-09-03: the
    # funded lane is currently clean (0 rate-limits in 1,320 attempts), and both chain-owner public
    # endpoints answer eth_chainId with HTTP 200 -- so the contention is avoidable, not a cost of
    # doing business. Point this at a public endpoint and leave custody alone.
    rpc_url = os.getenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_%s" % CHAIN) or evm_rpc_for_chain(CHAIN)
    if not rpc_url:
        raise SystemExit(
            "set MEMECOIN_ONCHAIN_EVM_RPC_URL_%s (preferred: a public endpoint) " % CHAIN +
            "or MEMECOIN_EVM_RPC_URL_%s" % CHAIN)
    wconn = get_connection(str(DB))
    m.ensure_memecoin_tables(wconn)
    decimals_cache = {}
    price = price or NativePrice(lambda: quote_usd_price(CHAIN))   # on demand unless run() warmed it
    cursor = None
    watched = _require_addresses(wconn)
    tx_read_host = read_transport.tx_host() if read_transport is not None else tx_read_host_from_env()
    origin_worker = _relay_origin_worker(None)
    refreshed_at = 0.0
    spent = 0
    head = None
    started = time.monotonic()
    # THE POLL SAYS IT IS POLLING, AND WHY IT FAILS (2026-09-29, Alpha). The 56 fallback asked a
    # provider that answered `-32005 limit exceeded` 24 times in two minutes at the fixed period, and
    # its beat never said which mode it was in. Every beat here carries mode 'poll'; a failure
    # streak doubles the wait from the period, up to 30 s, and one success resets it.
    streak, last_error = 0, None
    dark = {"lo": None, "hi": None, "logs": 0, "detections": 0} if logs_url else None
    if dark is not None:
        _DARK_POLL_STATS["windows"] += 1

    def beat(now, **fields):
        # `last_block` is the last block this poll READ (2026-09-29, Gamma): a push restart anchors
        # its gap backfill on it, and a poll beat without it left a dark window read by nobody.
        _write_beat({"ts": now, "wallets": len(watched), "cursor": cursor, "mode": "poll",
                     "last_block": origin_worker.frontier(cursor if cursor is not None else start_block)
                                   if origin_worker is not None else cursor if cursor is not None else start_block,
                     "poll_error_streak": streak, "last_poll_error": last_error, **fields})
    try:
        while duration_s is None or time.monotonic() - started < duration_s:
            now = now_iso()
            if time.monotonic() >= refreshed_at:
                watched = _verified_addresses(wconn)
                refreshed_at = time.monotonic() + ADDRESS_REFRESH_S
            if not watched:
                beat(now)
                time.sleep(period_s)
                continue
            # Re-read the head only when starting a fresh tick. While catching up, `head` from
            # this tick is a valid LOWER bound -- the chain only moves forward -- so the extra
            # eth_blockNumber per 10 blocks bought nothing. It cost half the throughput: two calls
            # per 10 blocks against a chain making 10 blocks/s is break-even, measured at 9.8
            # blocks/s covered vs 9.6 produced, pinning the cursor ~19s behind head, which is no
            # better than the fomo lane this exists to beat. Dropping it also LOWERS the request
            # rate on a key shared with funded quoting.
            try:
                if spent == 0 or head is None:
                    head = int(_rpc_result(rpc_url, "eth_blockNumber", []), 16)
            except Exception as exc:
                # Heartbeat and SAY SO. Both were missing: the write sits at the end of the loop
                # body, so every `continue` above it left the heartbeat frozen at the last good
                # sweep -- a permanently failing watcher and a wedged process looked identical.
                _say("[onchain-evm] eth_blockNumber failed: %r" % (exc,))
                streak, last_error = streak + 1, repr(exc)[:200]
                beat(now, error="head_read")
                time.sleep(_poll_backoff(period_s, streak))
                continue
            if cursor is None and start_block is not None and logs_url:
                cursor = min(int(start_block), head)
            if cursor is None or cursor > head:
                cursor = max(0, head - 1)
            if head - cursor > behind_cap:
                _say("[onchain-evm] %d blocks behind — skipping to %d blocks back from head; "
                     "the skipped range is older than the lane accepts"
                     % (head - cursor, behind_cap))
                cursor = head - behind_cap
            to_block = min(head, cursor + LOGS_MAX_SPAN)
            if to_block <= cursor:
                beat(now)
                time.sleep(period_s)
                continue
            topics = [TRANSFER_TOPIC, None, [_topic(address) for address in watched]]
            try:
                if logs_url:
                    logs = _dark_logs(logs_url, cursor + 1, to_block, topics)
                else:
                    reply = _counted_rpc(rpc_url, "eth_getLogs", [{
                        "fromBlock": hex(cursor + 1), "toBlock": hex(to_block), "topics": topics}])
                    if "error" in reply:       # the provider's words, not KeyError('result')
                        raise RuntimeError("JSON-RPC error %s" % json.dumps(reply["error"])[:200])
                    logs = reply.get("result")
                if not isinstance(logs, list):
                    raise ValueError("non-list eth_getLogs result")
            except Exception as exc:
                # THE bug this script died of, made loud. A provider that rejects the span answers
                # every request identically forever, so a silent retry is an infinite silent retry.
                _say("[onchain-evm] eth_getLogs %d..%d failed: %r" % (cursor + 1, to_block, exc))
                streak, last_error = streak + 1, repr(exc)[:200]
                beat(now, error="get_logs")
                time.sleep(_poll_backoff(period_s, streak))
                continue
            streak = 0
            if dark is not None:
                dark["lo"] = cursor + 1 if dark["lo"] is None else dark["lo"]
                dark["hi"] = to_block
                dark["logs"] += len(logs)
                _DARK_POLL_STATS["blocks"] += to_block - cursor
                _DARK_POLL_STATS["logs"] += len(logs)
            tx_logs = {}
            for log in logs:
                tx_hash = str(log.get("transactionHash") or "").lower()
                if tx_hash:
                    tx_logs.setdefault(tx_hash, log)
            complete = True
            quote_usd, price_age, price_source = price.get() if tx_logs else (None, None, None)
            if tx_logs and not quote_usd and CHAIN != '4663':
                time.sleep(period_s)
                continue
            # Decode first, write last. A row written inside this loop opened a write transaction
            # on the FUNDED ledger that then sat open across three RPC round-trips per transaction
            # (30s timeout each) and, on `break`, across the next sleep and the next getLogs -- the
            # runner's own writes hit `database is locked` about once every two hours (2026-09-05).
            pending = []
            block_ts = {}
            for tx_hash, log in tx_logs.items():
                try:
                    if logs_url:
                        number = log.get("blockNumber")
                        if number not in block_ts:
                            block_ts[number] = (_backfill_post(
                                logs_url, "eth_getBlockByNumber", [number, False]) or {}).get("timestamp")
                        if not block_ts[number]:
                            raise ValueError("source block time unavailable")
                        log = dict(log, blockTimestamp=block_ts[number])
                    outcome = {}
                    row = push_detection(rpc_url, log, watched, decimals_cache=decimals_cache,
                                         quote_usd=quote_usd, now=now, tx_read_host=tx_read_host,
                                         outcome=outcome, read_transport=read_transport)
                    if not row and not _detection_complete(outcome):
                        complete = False
                        break
                    if row:
                        row["valuation"] = _valuation(quote_usd, price_age, price_source)
                        if 'metadata_prefetch_rpc_attempts' in outcome:
                            row['valuation']['metadata_prefetch_rpc_attempts'] = outcome['metadata_prefetch_rpc_attempts']
                        if logs_url:
                            row["valuation"]["backfill"] = True
                        if _defer_relay_origin(origin_worker, row, outcome, quote_usd, price_age,
                                              backfill=bool(logs_url)):
                            continue
                        pending.append(row)
                except Exception as exc:
                    _say("[onchain-evm] dark poll %s held: %s" % (tx_hash[:12], type(exc).__name__))
                    complete = False
                    break
            if complete:
                _commit_detections(wconn, pending)
                cursor = to_block
                if dark is not None:
                    dark["detections"] += len(pending)
                    _DARK_POLL_STATS["detections"] += len(pending)
            beat(now)
            # One 10-block call covers ~1.0s of a 0.10s-block chain, so a fixed one-call-per-second
            # tick can only ever break even and never recover from a stall. Keep spending calls
            # while behind, up to the budget, then yield.
            spent += 1
            if cursor >= head or spent >= CATCHUP_CALLS_PER_TICK:
                spent = 0
                time.sleep(period_s)
    finally:
        if dark is not None:
            if dark["lo"] is None:
                _say("[onchain-evm] dark poll read no range")
            else:
                n_logs, n_det = dark["logs"], dark["detections"]
                _say("[onchain-evm] dark poll read %d..%d (%d blocks): %d log%s, %d detection%s"
                     % (dark["lo"], dark["hi"], dark["hi"] - dark["lo"] + 1, n_logs,
                        "" if n_logs == 1 else "s", n_det, "" if n_det == 1 else "s"))
        if origin_worker is not None:
            origin_worker.close()
        wconn.close()
        getattr(tx_read_host, "close", lambda: None)()   # a websocket host's thread ends here
    return cursor if (start_block is None or cursor is None or cursor >= int(start_block)) else None



def source_receipt_producer(chain):
    """Immutable original-ingress clock for the measured EVM source chains, startup only."""
    chain = str(chain)
    return ReceiptClock('evm:' + chain) if chain in {'56', '4663'} else None


@_pooled_watcher_reads
def run_push(ws_url, *, duration_s=None, period_s=1.0, observe_only=False, price=None, read_transport=None):
    """Receive armed-wallet buys and new pools by subscription. Falls back to polling when dark.

    HEARTBEAT ON A DELIVERED HEAD ONLY. A subscription reply is not delivery (publicnode,
    2026-08-07), so a heartbeat written on connect would report a socket that never carries a
    single event as healthy for the life of the process.

    `observe_only` (2026-09-16, Arc build): this chain has no armed wallets and none are expected
    yet -- pool-key observation is deliberately independent of wallet arming, a separate and later
    decision. `_require_addresses` blocks forever waiting for one, which would hang this chain's
    process for good. Skip straight to an empty watched set; `run_push_stream` already handles it
    (no transfer filter is subscribed when `addresses_fn()` returns nothing), so the Initialize
    subscription still runs. Never signs, executes, or reaches a funded hook either way -- this
    lane only decodes logs and writes observation rows.
    """
    from sniper.settings import memecoin as cfg
    from sniper.services import memecoin_onchain_push as push
    from sniper.execution.evm_v4_quote import V4, _INIT_TOPIC
    rpc_url = os.getenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_%s" % CHAIN) or evm_rpc_for_chain(CHAIN)
    wconn = get_connection(str(DB))
    m.ensure_memecoin_tables(wconn)
    decimals_cache = {}
    price = price or NativePrice(lambda: quote_usd_price(CHAIN))   # on demand unless run() warmed it
    bsc_signal = _bsc_lane_signal(observe_only)
    if bsc_signal is not None:
        global _HOP_WANTED_SINK
        from sniper.services.memecoin_bsc_fast_lane import want_hop_quote
        _HOP_WANTED_SINK = want_hop_quote
    receipt_producer = source_receipt_producer(CHAIN)
    publication_fence = _PublicationFence() if CHAIN == "56" and not observe_only else None
    canonical_work = None
    base_hook = _base_lane_hook(observe_only)
    # SEED FROM WHAT WE ALREADY PROVED, BEFORE ASKING ANYONE (2026-09-06). `watched` used to start
    # empty, so any failure in the first sync left the armed lane watching NOBODY for as long as the
    # failure lasted. The stored addresses are addresses we resolved and persisted earlier; starting
    # from them means the worst a broken sync can do is leave us slightly stale, never blind.
    #
    # OBSERVE-ONLY CHAINS SKIP THE WAIT (2026-09-16). `_require_addresses` blocks forever for a
    # chain with no armed wallets yet, by design -- correct for 4663, where the wait means "an
    # address will earn a proof soon", wrong here, where NO wallet is expected until a separate,
    # later arming decision. `addresses_fn` below still re-checks every reconnect, so an address
    # armed later on this chain is picked up without a restart.
    watched = {} if observe_only else _require_addresses(wconn)

    def _authority_current(row):
        identity = watched.get(str(row.get('wallet') or '').lower())
        return identity is not None and identity[0] == row.get('source_user_id')

    origin_options = (dict(publication_fence=publication_fence, authority_fn=_authority_current)
                      if publication_fence is not None else {})
    origin_worker = (_relay_origin_worker(bsc_signal, base_hook=base_hook, price=price, **origin_options)
                     if not observe_only else None)
    # The SELL shadow on this chain (default off): a third filter on this socket, rows in the shadow
    # table only. Read once per process, like the other per-chain knobs here.
    sell_shadow = (not observe_only and CHAIN in cfg.memecoin_onchain_evm_sell_shadow_chains())
    tx_read_host = read_transport.tx_host() if read_transport is not None else tx_read_host_from_env()
    # The previous process's newest head, before we overwrite it, named either way (2026-10-07: a
    # skipped start gap printed nothing, so a torn heartbeat and a one-block step looked the same).
    prev_last_block, no_anchor = _restart_anchor()
    _say("[onchain-evm] restart anchor last_block=%d" % prev_last_block if prev_last_block is not None
         else "[onchain-evm] restart anchor: none (%s); no start backfill" % no_anchor)
    warm_stop = threading.Event()
    relay_ttl = float(getattr(cfg, "MEMECOIN_ONCHAIN_RELAY_CODE_TTL_S", 0.0) or 0.0)
    if not observe_only and CHAIN in RELAY_ENVELOPE_CHAINS and relay_ttl > 0:
        result_read = read_transport.result if read_transport is not None else _rpc_result
        relay_read = lambda method, params: result_read(rpc_url, method, params)  # noqa: E731
        try:
            _warm_relay(relay_read, relay_ttl)
        except Exception as exc:          # never fatal: the detection path verifies inline
            _say("[onchain-evm] relay code warm failed (%s: %s)" % (type(exc).__name__, exc))
        threading.Thread(target=_relay_warm_loop, name="relay-code-warm", daemon=True,
                         args=(lambda: _warm_relay(relay_read, relay_ttl),),
                         kwargs={"ttl_s": relay_ttl, "stop": warm_stop}).start()
    # Decimals never change: the quote token and the known payment tokens are read once, here.
    for token in [WETH] + [t for (c, t) in _PAYMENT_TICKERS if c == CHAIN]:
        if not observe_only and token not in decimals_cache:
            try:
                decimals_read = read_transport.decimals if read_transport is not None else erc20_decimals
                value = decimals_read(token, rpc_url)
            except Exception:
                value = None
            if value is not None:
                decimals_cache[token] = value
    _say("[onchain-evm] watching %d verified address(es)%s"
         % (len(watched), " (observe-only)" if observe_only else ""))
    started = time.monotonic()
    state = push.PushState()
    global PUSH_STATE
    PUSH_STATE = state

    roster_conn = [None]

    def addresses_fn():
        nonlocal watched
        # A successful empty read revokes every address. Only a read error (a 250 ms busy timeout
        # included) preserves the old set.
        try:
            roster_conn[0] = roster_conn[0] or _roster_connection()
            watched = _verified_addresses(roster_conn[0])
        except Exception as exc:
            _say("[onchain-evm] address read failed (%s: %s); keeping %d"
                 % (type(exc).__name__, exc, len(watched)))
        return sorted(watched)

    low_since = [None]
    # LOG SILENCE ON A LIVE SOCKET (2026-09-28, observe-only). 4663's log was silent 12:10-12:24Z
    # and a watched buy (0x2330b335a6) was never delivered; nothing could tell a dead log
    # subscription from a quiet chain. Heads advancing while no log on ANY subscription arrives for
    # MEMECOIN_EVM_WS_LOG_SILENCE_S is `suspect`. The busy reference is the V4 Initialize
    # subscription on the same socket (4663 ~6/min); without one (56) the verdict is `no_reference`.
    # Counted from the existing subscriptions: no RPC. Read in the head callback, i.e. after the
    # head is counted. Heads that stop too are the existing stale-head reconnect's job.
    has_reference = bool(V4.get(CHAIN, {}).get("pool_manager"))
    silence = {"logs": 0, "heads_at": 0, "since": _wall_clock(), "flagged": False}
    # LOG SILENCE ACTS (2026-10-16, Alpha). 4663 16:37-17:53Z: heads flowed, the log subscriptions
    # delivered nothing, and the verdict below only said so. On MEMECOIN_EVM_WS_LOG_SILENCE_ACT_CHAINS
    # (Initialize reference required) "all": the aggregate suspect resubscribes every log filter;
    # "transfers": Initialize delivering while transfers are silent MEMECOIN_EVM_WS_TRANSFER_SILENCE_S
    # resubscribes the transfer filter alone (today's shape: transfers dead at 16:34:45, Initialize
    # alive to ~16:47). Each escalates to a reconnect after another window, then 900 s of cooldown.
    act_chains = {c.strip() for c in str(getattr(cfg, "MEMECOIN_EVM_WS_LOG_SILENCE_ACT_CHAINS", "") or "")
                  .split(",") if c.strip()}
    transfer_limit = float(getattr(cfg, "MEMECOIN_EVM_WS_TRANSFER_SILENCE_S", 900.0) or 900.0)
    acts = ({"all": push.LogSilenceAction(limit_s=float(getattr(cfg, "MEMECOIN_EVM_WS_LOG_SILENCE_S", 300.0)
                                                        or 300.0), cooldown_s=900.0),
             "transfers": push.LogSilenceAction(limit_s=transfer_limit, cooldown_s=900.0)}
            if has_reference and not observe_only and str(CHAIN) in act_chains else None)

    def _act(verdict, subs, heads_since, silent_s):
        now = _wall_clock()
        if acts["all"].note(verdict, now=now, marker=state.logs) == "resubscribe":
            _say("[onchain-evm] log silence: resubscribing every log filter (%d heads and %.0fs with no log)"
                 % (heads_since, silent_s))
        init, transfers = subs.get("initialize") or {}, subs.get("transfers") or {}
        quiet = init.get("verdict") == "ok" and (transfers.get("silence_s") or 0.0) >= transfer_limit
        if acts["transfers"].note("suspect" if quiet else "ok", now=now,
                                  marker=(state.last_log_at or {}).get("transfers")) == "resubscribe":
            _say("[onchain-evm] transfer silence: resubscribing the transfer filter (%.0fs with no "
                 "transfer while Initialize delivers)" % transfers.get("silence_s"))

    def _log_silence():
        if state.logs != silence["logs"]:
            # This callback runs for a NEW head, so every log counted so far came before it: the
            # head that triggered this beat is the first one after the log. The log's own arrival
            # stamp (state.last_log_at, host wall clock) starts the silence.
            silence.update(logs=state.logs, heads_at=state.heads - 1, flagged=False,
                           since=max((state.last_log_at or {}).values(), default=_wall_clock()))
        heads_since = state.heads - silence["heads_at"]
        silent_s = _wall_clock() - silence["since"]
        limit = float(getattr(cfg, "MEMECOIN_EVM_WS_LOG_SILENCE_S", 0.0) or 0.0)
        # No explicit "heads advanced" test: this runs ONLY on a delivered head, so a written verdict
        # already means the socket is alive; when heads stop, the heartbeat itself goes stale.
        verdict = ("no_reference" if not has_reference else
                   "suspect" if limit > 0 and silent_s >= limit else "ok")
        if verdict == "suspect" and not silence["flagged"]:
            silence["flagged"] = True
            _say("[onchain-evm] log silence suspect: %d heads and %.0fs with no log on any subscription"
                 % (heads_since, silent_s))
        subs = _subscription_silence(limit)
        if acts is not None:
            _act(verdict, subs, heads_since, silent_s)
        return {"log_silence": verdict, "heads_since_log": heads_since,
                "log_silence_s": round(silent_s, 1), "subscriptions": subs,
                **({"log_silence_actions": {name: dict(a.stats) for name, a in acts.items()}}
                   if acts is not None else {})}

    # PER SUBSCRIPTION (2026-09-28, Alpha). The aggregate above counts ANY log, so a dead transfer
    # filter behind a live Initialize subscription reads "ok" -- and whether 12:10-12:24Z was the
    # whole socket or the filter alone cannot be told from what was kept. Each subscription now
    # reports its own silence. Initialize is busy enough to judge itself; the transfer filter is
    # sparse by design (47 addresses), so it reports its silence and `no_reference` until a canary
    # recipient gives it a baseline.
    expected_subs = (["transfers"] if not observe_only else []) + (["initialize"] if has_reference else [])
    started_wall = _wall_clock()
    per_sub = {name: {"at": None, "flagged": False} for name in expected_subs}

    def _subscription_silence(limit):
        out, now = {}, _wall_clock()
        for name in expected_subs:
            at, info = (state.last_log_at or {}).get(name), per_sub[name]
            if at != info["at"]:
                info.update(at=at, flagged=False)
            silent_s = now - (at if at is not None else started_wall)
            verdict = "no_reference"
            if name == "initialize":
                verdict = "suspect" if limit > 0 and silent_s >= limit else "ok"
                if verdict == "suspect" and not info["flagged"]:
                    info["flagged"] = True
                    _say("[onchain-evm] initialize subscription silent: %.0fs while heads arrive" % silent_s)
            out[name] = {"silence_s": round(silent_s, 1), "verdict": verdict}
        return out

    # ROLLING WINDOW, NOT A LIFETIME AVERAGE (2026-09-16, Arc build). `logs_per_min` above is
    # count/elapsed-since-start, which is exactly the shape that hid the 2026-09-06 coverage
    # degradation: a rate computed since start cannot show a RECENT stall, only dilute it into a
    # number that still looks fine for hours. `events_60s` is a true trailing count.
    _init_events = deque()
    # THE ACKED SET, LOGGED ON CHANGE (2026-09-29, Alpha). The beat holds only the current set, and
    # the sell-shadow read had no way to tell which addresses were subscribed when a sell happened.
    # One line per change (a reconnect re-acking the same set logs nothing); a process start line
    # ("watching N verified address(es)") means UNKNOWN until the first one.
    logged_set = [None]

    def _beat():
        _drain_backfill()
        acked = state.transfer_addresses
        if acked is not None and acked != logged_set[0]:
            logged_set[0] = acked
            _say("[onchain-evm] acked transfer set (%d): %s" % (len(acked), ",".join(acked)))
        rate_window = max(1e-6, (time.monotonic() - started) / 60.0)
        now_mono = time.monotonic()
        while _init_events and now_mono - _init_events[0] > 60.0:
            _init_events.popleft()
        # COVERAGE IS THE ONLY NUMBER HERE THAT NOTICED THE LAST DEGRADATION. On 2026-09-06 this
        # socket ran at 46% and then 30% of the chain's blocks with RECONNECTS AT ZERO and a
        # perfectly healthy-looking connection; heads_per_min fell too, but nothing knew what to
        # compare it against. Coverage compares delivered heads to the block span they cover, so the
        # chain is its own reference and no baseline has to survive a restart.
        cov = state.coverage()
        low_since[0] = push.coverage_low_since(state, cov, time.time(), low_since[0])
        _write_beat({
            "ts": now_iso(), "wallets": len(watched), "mode": "push",
            "heads": state.heads, "logs": state.logs, "reconnects": state.reconnects,
            # DELIVERED-MESSAGE RATE, so the provider's bill can be read from our side rather
            # than from their dashboard. Filter B alone measured ~187 Initialize/hour on 09-05.
            "heads_per_min": round(state.heads / rate_window, 2),
            "logs_per_min": round(state.logs / rate_window, 2),
            "events_60s": len(_init_events),
            "coverage": None if cov is None else round(cov, 4),
            "coverage_low_since": low_since[0],
            "last_log_at": state.last_log_at,
            "last_event_at": state.last_log_at.get("initialize"),
            **_log_silence(),
            # A GAP STILL WAITING FOR ITS BACKFILL HOLDS last_block at the gap (2026-09-29, Gamma): a
            # restart anchors its own backfill here, and a head past the gap would skip it forever.
            "last_block": _frontier(),
            # THIS CONNECTION ONLY (2026-09-29). The addresses whose transfer filter the socket
            # ACKED (the J7 shadow gate covers a wallet only when all of its addresses are here;
            # None until the reply), and deliveries per subscription since the connect, so a dead
            # transfer filter behind a live socket shows as heads climbing over transfers at 0.
            "transfer_addresses": (list(state.transfer_addresses)
                                   if state.transfer_addresses is not None else None),
            "delivered_since_connect": dict(state.delivered),
            "connected_at": state.connected_at,
            "ws_pool": state.ws_pool,
            "push_host": (state.ws_pool or {}).get("push_host"),
            "failovers": (state.ws_pool or {}).get("failovers", 0),
            "dark_poll": dict(_DARK_POLL_STATS),
            # THE CANARY (2026-10-17): logs and the last full minute's count per push host.
            "canary": ({"silent": state.canary["silent"],
                        "per_host": {host: {"logs": c["logs"], "canary_logs_per_min": c["logs_per_min"]}
                                     for host, c in state.canary["per_host"].items()}}
                       if state.canary else None),
            **({"sell_shadow": dict(_SELL_STATS)} if sell_shadow else {}),
            "relay_capture": dict(_CAPTURE_STATS),
            **({"relay_origin": dict(origin_worker.stats)} if origin_worker is not None else {}),
            **({"canonical_decode": canonical_work.snapshot()} if canonical_work is not None else {}),
            **({"receipt_clock": {"producer_id": receipt_producer.producer_id,
                                  "anchor_evicted": receipt_producer.evicted}}
               if receipt_producer is not None else {}),
            **({"j7_xcheck": dict(xc.stats)} if xc is not None else {}),
            **({"base_preconf_decode": preconf_jobs[1].snapshot(),
                "base_preconf_resolver": preconf_jobs[0].snapshot(),
                "base_preconf_invalid_logs": preconf_live["invalid_logs"]}
               if preconf_on and preconf_jobs else {}),
        })

    def _drop_acked():
        # A NEW CONNECTION ATTEMPT UNPUBLISHES THE ACKED SET (Gamma, 2026-09-29). The beat is written
        # on a delivered head only, so after a socket error the last beat kept the OLD socket's
        # transfer_addresses until the new socket's first head, and the J7 shadow gate covered
        # wallets nothing was subscribed for. Rewrite the beat with the set cleared and its `ts`
        # UNCHANGED: a fresh ts here would make a watcher stuck reconnecting look alive to the
        # watchdog's beat_stale rule. Unreadable or already clear: nothing to do.
        try:
            beat = json.loads(HEARTBEAT.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(beat, dict) or beat.get("transfer_addresses") is None:
            return
        beat.update(transfer_addresses=None, delivered_since_connect={}, connected_at=None)
        _atomic_write_text(HEARTBEAT, json.dumps(beat))

    decided = _Decided()
    dedupe_s = float(getattr(cfg, "MEMECOIN_ONCHAIN_EVM_TX_DEDUPE_S", 0.0) or 0.0)
    # THE LOCK GUARDS THE CLAIM, NOT THE READS (2026-09-28, Alpha). The gap backfill runs on its own
    # thread and hands its logs to this same path. Holding one lock over a whole decode let a slow
    # backfill read (tx-read timeout, fallback, null retries: about 3.7 s worst case) hold a LIVE
    # transfer back. Now only the dedupe claim and its release are serial; an in-flight set stops
    # the same tx being decoded twice at once, and each thread writes on its own connection.
    transfer_lock = threading.Lock()
    inflight = set()
    # J7 CROSS-CHECK (2026-09-29, spec 2026-09-29-memecoin-evm-watch-dead-subscription-design.md).
    xc = (J7CrossCheck(CHAIN, grace_s=cfg.MEMECOIN_ONCHAIN_EVM_J7_XCHECK_GRACE_S,
                       min_gap_s=cfg.MEMECOIN_ONCHAIN_EVM_J7_XCHECK_MIN_GAP_S)
          if not observe_only and str(CHAIN) in cfg.memecoin_onchain_evm_j7_xcheck_chains() else None)

    # BASE PRECONF DETECTION (2026-10-09, spec 2026-10-09-memecoin-base-flashblocks-detection-design.md).
    # A second socket's pending logs go through on_transfer(preconf=True). A row is published from a
    # preconfirmation only when the Relay buy is proved from the user's own stored deposit (the Relay
    # API cannot have indexed an unsealed fill, and a direct buy's proof binds a block hash): anything
    # else is left to its sealed log, exactly as today.
    preconf_on = not observe_only and CHAIN in cfg.memecoin_onchain_evm_preconf_chains()
    tracker = P.Tracker() if preconf_on else None
    preconf_seen = deque(maxlen=4096)            # every tx the preconf socket delivered
    preconf_inflight = set()                     # txs a preconf decode holds (under transfer_lock)
    preconf_parked = {}                          # tx -> (sealed log, backfill, socket arrival)
    preconf_live = {"head_at": None, "missed": 0, "invalid_logs": 0}
    force_preconf = threading.Event()

    def _publish_preconf(row, log, tx_hash, arrival, outcome, conn, price, price_source):
        block = _quantity(log.get("blockNumber"))
        proved = block is not None and _prove_from_deposit(row, outcome.get("_transaction"))[0]
        if not proved:
            outcome["reason"] = "preconf_needs_seal"
            _say("[onchain-evm] transfer %s arrival=%s outcome=preconf_needs_seal" % (tx_hash, arrival))
            return
        first_at = _parse_iso(arrival).timestamp()
        row["valuation"]["preconf"] = P.valuation_note(log, first_at)
        row["observation_key"], row["observation_state"] = tx_hash, "preconfirmed"
        _commit_detections(conn, [row])
        tracker.add(tx_hash, block=block, first_at=first_at)
        outcome["preconf_recorded"] = True
        if base_hook is not None:                     # AFTER today's hand-off, record only
            _signal_base_lane(base_hook, row, outcome, price, price_source)

    def _confirm_preconf(log, tx_hash, entry, conn, arrival):
        sealed_at = _parse_iso(arrival).timestamp()
        n = P.confirm(conn, tx_hash, block=log.get("blockNumber"), block_hash=log.get("blockHash"),
                      sealed_at=sealed_at)
        preconf_live["missed"] = 0
        _say("[onchain-evm] preconf %s confirmed block=%s lead=%.2fs rows=%d"
             % (tx_hash[:12], _quantity(log.get("blockNumber")), sealed_at - entry["first_at"], n))

    def _count_preconf_miss(tx_hash):
        """A sealed armed transfer the live preconf socket never delivered; MISSED_RECONNECT in a row
        replace that socket (a confirmed subscription that went deaf, trap a66c5223)."""
        head_at = preconf_live["head_at"]
        if not tx_hash or head_at is None or time.monotonic() - head_at > P.HEAD_STALE_S:
            return
        if tx_hash in preconf_seen:
            preconf_live["missed"] = 0
            return
        preconf_live["missed"] += 1
        if preconf_live["missed"] >= P.MISSED_RECONNECT:
            preconf_live["missed"] = 0
            force_preconf.set()

    def on_transfer(log, backfill=False, conn=None, preconf=False, arrival=None, *,
                    roster=None, cache=None, lane_signal=None, delivered=False,
                    receipt_clock=None, receipt_stages=()):
        if receipt_producer is not None and receipt_clock is None and not backfill:
            receipt_clock = receipt_producer.capture()
        arrival = arrival if arrival is not None else now_iso()   # retain pre-queue socket time
        tx_hash = str(log.get("transactionHash") or "").lower()
        if not preconf and tracker is not None and tx_hash and not backfill:
            # Removed/partial notifications cannot consume the canonical receipt resolver's work.
            block, topics = _quantity(log.get("blockNumber")), log.get("topics") or []
            if (log.get("removed", False) is not False or P.real_hash(tx_hash) is None
                    or P.real_hash(log.get("blockHash")) is None or block is None or block < 0
                    or not isinstance(topics, list) or len(topics) != 3
                    or str(topics[0]).lower() != TRANSFER_TOPIC
                    or _topic_address(topics[2]) not in watched):
                return False
            address, data = str(log.get("address") or ""), str(log.get("data") or "")
            contract, amount = _quantity(address), _quantity(data)
            indices = (_quantity(log.get("logIndex")), _quantity(log.get("transactionIndex")))
            if (len(address) != 42 or not address.startswith("0x") or contract is None or contract <= 0
                    or len(data) != 66 or not data.startswith("0x") or amount is None or amount < 0
                    or any(i is None or i < 0 for i in indices)
                    or _quantity(topics[1]) is None
                    or any(str(t).lower() != _topic(_topic_address(t)) for t in topics[1:])):
                return False
            # THE SEALED LOG OF A PRECONFIRMED DETECTION IS ITS CONFIRMATION, never a second detection.
            entry = tracker.sealed(tx_hash)
            if entry is not None:
                _confirm_preconf(log, tx_hash, entry, conn or wconn, arrival)
                return True
            _count_preconf_miss(tx_hash)
        if xc is not None and tx_hash and not backfill and not preconf and not delivered:
            xc.delivered_tx(tx_hash)       # the SOCKET delivered it; a backfilled log is not proof
        with transfer_lock:
            if receipt_clock is not None and tx_hash and not delivered:
                receipt_clock = receipt_producer.remember(("source_transaction", tx_hash), receipt_clock)
            dup = bool(tx_hash) and (tx_hash in inflight
                                     or (dedupe_s > 0 and decided.seen(tx_hash, dedupe_s)))
            # A SEALED LOG NEVER WAITS ON A PRECONF DECODE (Gamma MEDIUM on b91757822): parked here, it
            # gets its own detection when that decode ends without a row, or confirms the row it wrote.
            parked_now = dup and not preconf and tx_hash in preconf_inflight
            if parked_now:
                preconf_parked[tx_hash] = (log, backfill, arrival)
            if not dup and tx_hash:
                inflight.add(tx_hash)
                if preconf:
                    preconf_inflight.add(tx_hash)
            decided_dup = dup and tx_hash not in inflight
        if parked_now:
            _say("[onchain-evm] transfer %s arrival=%s outcome=parked_behind_preconf" % (tx_hash, arrival))
            return True
        if dup:
            _say("[onchain-evm] transfer %s arrival=%s outcome=duplicate_tx" % (tx_hash, arrival))
            return decided_dup
        outcome = ({"_receipt_clock": receipt_clock, "receipt_stages": list(receipt_stages)}
                   if receipt_clock is not None else {})
        complete = False
        try:
            _on_transfer(log, backfill, conn or wconn, arrival, outcome, preconf=preconf,
                         roster=roster, cache=cache, lane_signal=lane_signal)
            # A preconf sighting decides the tx only when it RECORDED a row: anything else (no buy yet,
            # needs the seal, a read error) leaves the tx to its sealed log.
            complete = (bool(outcome.get("preconf_recorded")) if preconf
                        else _detection_complete(outcome))
            return complete
        except Exception as exc:
            # NAMED BEFORE IT PROPAGATES (2026-10-07): the push lane's traceback names no tx.
            _say("[onchain-evm] transfer %s arrival=%s outcome=handler_error %s: %s"
                 % (tx_hash, arrival, type(exc).__name__, str(exc)[:200]))
            raise
        finally:
            with transfer_lock:
                inflight.discard(tx_hash)
                # Only a DECISION is remembered: a null read (tx_null / receipt_null) or a raise
                # leaves the tx open, so the next log of it reads again.
                if dedupe_s > 0 and tx_hash and complete:
                    decided.add(tx_hash)
                held = None
                if preconf:
                    preconf_inflight.discard(tx_hash)
                    held = preconf_parked.pop(tx_hash, None)
            if held is not None:
                sealed_log, sealed_backfill, sealed_arrival = held
                entry = tracker.sealed(tx_hash)        # set only when this decode recorded a row
                if entry is not None:
                    _confirm_preconf(sealed_log, tx_hash, entry, conn or wconn, sealed_arrival)
                else:
                    on_transfer(sealed_log, backfill=sealed_backfill, conn=conn or wconn, arrival=sealed_arrival)

    def _on_transfer(log, backfill, conn, arrival, outcome, preconf=False, *,
                     roster=None, cache=None, lane_signal=None):
        if publication_fence is not None and publication_fence.closed.is_set():
            raise RuntimeError("watcher_stopped")
        tx_hash = str(log.get("transactionHash") or "").lower()
        tx12 = tx_hash        # the FULL hash (2026-10-17, Gamma): a [:12] prefix cannot be read by receipt
        # C1 (2026-10-16): tell the 56 fast lane now, before the receipt decode, so the coin's
        # market stats, facts and safety read start ~70 ms earlier. Best-effort; the lane decides
        # nothing on it (MEMECOIN_EVM_LANE_INTENT_WARM_CHAINS).
        intent = getattr(lane_signal if roster is not None else bsc_signal, "intent", None)
        if intent is not None and not backfill and not preconf:
            topics = log.get("topics") or []
            intent(log.get("address"), tx_hash,
                   pair=_topic_address(topics[1]) if len(topics) > 1 else None)
        quote_usd, price_age, price_source = price.get()
        _receipt_stage(outcome, "source_valuation_ready")
        if not quote_usd and CHAIN != '4663':
            _say("[onchain-evm] transfer %s arrival=%s outcome=price_missing" % (tx12, arrival))
            return
        from sniper.services.memecoin_evm_post_trade import conn_v4_key_fn
        row = push_detection(P.PRECONF_HTTP if preconf else rpc_url, log,
                             watched if roster is None else roster,
                             decimals_cache=decimals_cache if cache is None else cache,
                             quote_usd=quote_usd, now=arrival,
                             outcome=outcome, tx_read_host=None if preconf else tx_read_host,
                             pool_key_fn=conn_v4_key_fn(conn), preconf=preconf, read_transport=read_transport)
        _receipt_stage(outcome, "decode_end")
        if row:                            # fetch first, write last: one short write, then commit
            row["valuation"] = _valuation(quote_usd, price_age, price_source)
            row["valuation"]["reads"] = _hosts_note(outcome)
            row["valuation"]["code_basis"] = outcome.get("code_basis")
            row["valuation"]["decode_ms"] = outcome.get("decode_ms")
            row["valuation"]["payment_token"] = outcome.get("payment_token")
            row["valuation"]["evm_post"] = outcome.get("evm_post")
            if 'metadata_prefetch_rpc_attempts' in outcome:
                row['valuation']['metadata_prefetch_rpc_attempts'] = outcome['metadata_prefetch_rpc_attempts']
            if outcome.get("_receipt_clock") is not None:
                row["receipt_clock"] = outcome["_receipt_clock"]
                row["receipt_stages"] = tuple(outcome["receipt_stages"])
                row["valuation"].update(receipt_clock=dict(row["receipt_clock"]),
                                        receipt_stages=row["receipt_stages"])
            if backfill:
                row["valuation"]["backfill"] = True
            if preconf:
                _publish_preconf(row, log, tx_hash, arrival, outcome, conn, price, price_source)
                return
            def publish():
                if publication_fence is not None and not _authority_current(row):
                    outcome.update(reason='no_buy', decode='source_authority_revoked')
                    _say('[onchain-evm] transfer %s arrival=%s outcome=source_authority_revoked'
                         % (tx12, arrival))
                    return
                if _defer_relay_origin(origin_worker, row, outcome, quote_usd, price_age, backfill=backfill):
                    return
                _publish_detection(conn, row,
                                   bsc_signal=(lane_signal if roster is not None else bsc_signal)
                                   if not backfill else None,
                                   outcome=outcome, quote_usd=quote_usd, price_age=price_age)
            if publication_fence is None:
                publish()
            else:
                publication_fence.call(publish)
            if base_hook is not None and not backfill:    # AFTER today's hand-off, record only
                _signal_base_lane(base_hook, row, outcome, price, price_source)
        else:                              # EVERY delivered transfer is accounted for (2026-09-28)
            _say("[onchain-evm] transfer %s arrival=%s outcome=%s retries=%s %s%s%s"
                 % (tx12, arrival, outcome.get("reason"), outcome.get("retries"),
                    _hosts_note(outcome),
                    " decode=%s" % outcome["decode"] if outcome.get("decode") else "",
                    "".join(" read_error=%s:%s" % kv
                            for kv in sorted((outcome.get("read_errors") or {}).items()))))

    def on_initialize(log):
        _init_events.append(time.monotonic())
        push.store_initialize(CHAIN, log)

    # GAP BACKFILL (2026-09-28, operator YES; spec 2026-09-28-memecoin-evm-watcher-gap-backfill).
    # The gap is (last head before, first head after]; at start the "before" is the previous
    # process's heartbeat last_block. It is read once, newest page first, clipped to the lane's age
    # window at the chain's measured block rate and to MEMECOIN_ONCHAIN_EVM_BACKFILL_MAX_CALLS, on
    # its own thread through the same transfer path. It waits for a measured rate before reading.
    backfill_url = (_backfill_url(CHAIN) if not observe_only
                    and CHAIN in getattr(cfg, "MEMECOIN_ONCHAIN_EVM_BACKFILL_CHAINS", frozenset()) else None)
    if not observe_only and CHAIN in getattr(cfg, "MEMECOIN_ONCHAIN_EVM_BACKFILL_CHAINS", frozenset()):
        _say("[onchain-evm] backfill %s" % ("on via %s" % backfill_url.split("//", 1)[-1].split("/", 1)[0]
                                            if backfill_url else "off: no log-read host for chain %s" % CHAIN))
    gap = {"pending": None, "last_block": prev_last_block, "reconnects": state.reconnects}

    def _frontier():
        floors = [floor[0] - 1 for floor in (gap.get("pending"), gap.get("inflight"))
                  if floor is not None]
        current = min(floors) if floors else state.window[-1][0] if state.window else prev_last_block
        if canonical_work is not None:
            current = canonical_work.frontier(current)
        return origin_worker.frontier(current) if origin_worker is not None else current

    def _drain_decode():
        unread = canonical_work.drain_failed() if canonical_work is not None else None
        if unread is not None:
            pending = gap.get("pending")
            gap["pending"] = ((min(unread[0], pending[0]), max(unread[1], pending[1]))
                              if pending else unread)
            _say("[onchain-evm] canonical decode incomplete: recovery %d-%d" % unread)

    # Only the WS owner mutates gap. The worker publishes completion without holding
    # any lock over RPC or a database operation; a reconnect cannot overwrite its requeue.
    backfill_done = deque()

    def _drain_backfill():
        _drain_decode()
        while backfill_done:
            lo, hi, complete = backfill_done.popleft()
            # The dark poll may have covered this worker's range while it was still running.
            lo = max(lo, int(gap.get("polled_hi") or -1) + 1)
            complete = complete or lo > hi
            if complete:
                gap["backfill_hi"] = max(int(gap.get("backfill_hi") or 0), hi)
                gap["failures"] = 0
            else:
                gap["lane_at"] = None
                pending = gap.get("pending")
                gap["pending"] = (min(lo, pending[0]), max(hi, pending[1])) if pending else (lo, hi)
                gap["failures"] = gap.get("failures", 0) + 1
                gap["retry_at"] = time.monotonic() + 5.0
            gap["inflight"] = None

    def _run_backfill(lo, hi):
        complete, bconn = False, None
        try:
            bconn = get_connection(str(DB))
            complete = _backfill_range(lo, hi, bconn)
        finally:
            try:
                if bconn is not None:
                    bconn.close()
            finally:
                backfill_done.append((lo, hi, complete))

    def _backfill_range(lo, hi, bconn):
        if not watched:
            # NEVER AN EMPTY ADDRESS LIST (2026-09-29): [Transfer, null, []] can read as a wildcard
            # on a node, i.e. every Transfer in the range. A revoked roster reads nothing.
            _say("[onchain-evm] backfill %d-%d skipped: no watched address" % (lo, hi))
            return False
        watched_topics = ["0x" + "0" * 24 + a[2:] for a in sorted(watched)]
        top, calls, found, lost = hi, 0, 0, 0
        block_ts = {}
        max_calls = int(getattr(cfg, "MEMECOIN_ONCHAIN_EVM_BACKFILL_MAX_CALLS", 0) or 0)
        while top >= lo and calls < max_calls:
            bottom = max(lo, top - _BACKFILL_PAGE_BLOCKS + 1)
            calls += 1
            try:
                logs = _backfill_post(backfill_url, "eth_getLogs", [{
                    "fromBlock": hex(bottom), "toBlock": hex(top),
                    "topics": [TRANSFER_TOPIC, None, watched_topics]}])
            except Exception as exc:
                _say("[onchain-evm] backfill page %d-%d failed: %s" % (bottom, top, exc))
                logs, lost = [], lost + 1
            for log in logs:
                found += 1
                # THE BUY'S OWN TIME (2026-09-28, Alpha). The lane ages a backfilled row by its
                # source block, never by when we read it; eth_getLogs carries no timestamp, so read
                # the block once. Unknown time means unknown age: the log is skipped, never acted on.
                number = log.get("blockNumber")
                if not log.get("blockTimestamp"):
                    if number not in block_ts:
                        try:
                            block_ts[number] = (_backfill_post(backfill_url, "eth_getBlockByNumber",
                                                               [number, False]) or {}).get("timestamp")
                        except Exception as exc:
                            block_ts[number] = None
                            _say("[onchain-evm] backfill block %s read failed: %s" % (number, exc))
                    if not block_ts[number]:
                        _say("[onchain-evm] backfill log %s skipped: block time unavailable"
                             % str(log.get("transactionHash") or "")[:12])
                        lost += 1
                        continue
                    log = dict(log, blockTimestamp=block_ts[number])
                if not on_transfer(log, backfill=True, conn=bconn):
                    lost += 1
            top = bottom - 1
        _say("[onchain-evm] backfill %d-%d read %d page(s), %d log(s)" % (max(lo, top + 1), hi, calls, found))
        return top < lo and not lost

    def _gap_check():
        _drain_backfill()
        cur = state.window[-1][0] if state.window else None
        if cur is None:
            return
        if state.reconnects != gap["reconnects"]:
            prior_pending = gap.get("pending")
            gap["reconnects"] = state.reconnects
            gap["failures"] = 0
            # STICKY (2026-09-29): an armed-set resubscribe owes the lane window even when another
            # reconnect (a silent new socket, 56 08:06:52Z) lands before the first head.
            armed_n = state.reconnect_causes.get("armed_set", 0)
            lane = gap.pop("force_window", False) or armed_n != gap.get("armed_n", 0)
            gap["armed_n"] = armed_n
            # (armed_set, 2026-09-29, Alpha: a newly armed wallet was unsubscribed from its arming
            # until this resubscribe, so its buys predate the blip; read the lane window with the
            # NEW set.) AT MOST ONE LANE WINDOW PER LANE_WINDOW_COOLDOWN_S per chain: the backfill
            # host is a member of custody's pool (shared key and rate limit), so a flapping roster
            # must not re-read 45 pages every 5 s. Inside the cooldown, read only from the end of the
            # last backfill, which is at least the blip. The claim is taken here, so a roster that
            # flaps while a lane read is in flight cannot stack a second one; `_run_backfill`
            # releases it when the read is incomplete.
            # ponytail: L1 ceiling (Gamma, accepted by Alpha 2026-09-29): a wallet armed after the
            # previous connect's address read but before that connect's backfill_hi (its first head,
            # ~0.3-1.5 s later) has a buy in that gap read with the OLD set only, and a clipped read
            # starts after it. Start a clipped armed_set read at the previous connect's address-read
            # head instead if a bulk arming ever misses a buy there; it costs a few blocks.
            now_m = time.monotonic()
            if (lane and gap.get("lane_at") is not None
                    and now_m - gap["lane_at"] < LANE_WINDOW_COOLDOWN_S):
                lo = max(int(gap.get("backfill_hi") or 0) + 1,
                         (gap["last_block"] + 1) if gap["last_block"] is not None else 0)
                _say("[onchain-evm] lane-window backfill clipped: the last one ran %.0fs ago; "
                     "reading %d-%d" % (now_m - gap["lane_at"], lo, cur))
                gap["pending"] = (lo, cur) if lo <= cur else None
            elif lane:
                gap["lane_at"] = now_m
                # A FORCED reconnect: heads kept flowing, so the blip is tiny, but the deaf socket
                # missed everything since its connect. Read the lane window; the clip below bounds it.
                gap["pending"] = (0, cur)
            elif gap["last_block"] is not None and cur > gap["last_block"] + 1:
                gap["pending"] = (gap["last_block"] + 1, cur)
            if prior_pending:
                pending = gap.get("pending")
                gap["pending"] = (min(prior_pending[0], pending[0]),
                                  max(prior_pending[1], pending[1])) if pending else prior_pending
            if lane:
                gap["retry_at"] = 0        # a new lane event may retry its incomplete read now
        elif gap["pending"] is None and gap["last_block"] is not None and gap.get("start", True):
            # THE ANCHOR BLOCK ITSELF, AND A ONE-BLOCK STEP (2026-10-07, 8453 17:16Z). The previous
            # process may have seen head N and died before N's logs were handled, and a first head
            # at N+1 can still have missed N+1's logs before the subscription took. Re-reads are
            # idempotent (INSERT OR IGNORE on the signature).
            gap["pending"] = (min(gap["last_block"], cur), cur)
        gap["start"] = False
        gap["last_block"] = cur
        if gap["pending"] is not None and backfill_url and gap.get("inflight") is None:
            rate = _chain_blocks_per_s(state)
            if rate is None:
                return                     # clip needs the chain's own rate: wait for another head
            lo, hi = gap["pending"]
            window_s = float(getattr(cfg, "MEMECOIN_ONCHAIN_LANE_MAX_AGE_S", 45.0) or 45.0)
            lo = max(lo, cur - int(window_s * rate + 0.999))
            if lo > hi:
                _say("[onchain-evm] backfill %d-%d expired unread: outside lane window"
                     % gap["pending"])
                gap["pending"] = None
                gap["failures"] = 0
                return
            # One bounded retry while actionable. Repeated provider failures retain the
            # frontier until expiry; never turn the shared funded key into a retry loop.
            if gap.get("failures", 0) >= 2 or time.monotonic() < gap.get("retry_at", 0):
                return
            gap["pending"] = None
            gap["inflight"] = (lo, hi)
            _backfill_thread(lambda: _run_backfill(lo, hi))
        elif gap["pending"] is not None and not backfill_url:
            if canonical_work is None:
                gap["pending"] = None
            else:
                # No read host is a hole, not coverage; retire only past the existing lane window.
                rate = _chain_blocks_per_s(state)
                window_s = float(getattr(cfg, "MEMECOIN_ONCHAIN_LANE_MAX_AGE_S", 45.0) or 45.0)
                if rate is not None:
                    lo, hi = gap["pending"]
                    lo = max(lo, cur - int(window_s * rate + .999))
                    if lo > hi:
                        _say("[onchain-evm] canonical decode recovery %d-%d expired unread: no read host"
                             % gap["pending"])
                        gap["pending"] = None
                    else:
                        gap["pending"] = (lo, hi)

    def on_head():
        _gap_check()
        _beat()
        if tracker is not None and state.window:
            _preconf_settle(state.window[-1][0])

    preconf_jobs = []                       # separate canonical resolver and bounded decode admission
    preconf_stop = threading.Event()
    preconf_local = threading.local()       # each preconf thread writes on its own ledger handle

    def _preconf_conn():
        if getattr(preconf_local, "conn", None) is None:
            preconf_local.conn = get_connection(str(DB))
        return preconf_local.conn

    def _preconf_close():
        conn = getattr(preconf_local, "conn", None)
        if conn is not None:
            conn.close()
            preconf_local.conn = None

    def _preconf_work():
        # Initialized on the owning thread before either stream starts delivering callbacks.
        if not preconf_jobs:
            preconf_jobs.append(P.HeadWork(_preconf_settle_now, on_stop=_preconf_close))
            preconf_jobs.append(P.DecodeWork(_preconf_decode, on_stop=_preconf_close))
        return preconf_jobs

    def _preconf_page(kind, tx_hash):
        text = "8453 preconfirmed source buy %s %s" % (tx_hash, kind)
        _say("[onchain-evm] preconf %s %s" % (tx_hash[:12], kind))
        from sniper.services.memecoin_alerts import send_alert
        send_alert("onchain_preconf", text)

    def _preconf_settle_now(head):
        conn = _preconf_conn()
        try:
            P.settle(tracker, head,
                     resolve=lambda tx: P.resolve(conn, tx, read_fn=lambda m, p: _rpc_result(P.SEALED_HTTP, m, p),
                                                  now=time.time()),
                     retract=lambda tx: P.retract(conn, tx), unresolved=lambda tx: P.mark_unresolved(conn, tx),
                     page=_preconf_page,
                     confirmed=lambda tx: _say("[onchain-evm] preconf %s confirmed by receipt" % tx[:12]))
        except Exception as exc:                      # noqa: BLE001 - named; the next head asks again
            _say("[onchain-evm] preconf settle failed: %s: %s" % (type(exc).__name__, str(exc)[:160]))

    def _preconf_settle(head):
        if head is not None and tracker.due(head):
            preconf_jobs[0].submit(head)

    def _preconf_head(number):
        preconf_live["head_at"] = time.monotonic()
        # A preconfirmed head cannot prove a later canonical null observation. Sealed
        # on_head owns receipt resolution; this callback reports stream liveness only.

    def _preconf_decode(log, arrival):
        return on_transfer(log, conn=_preconf_conn(), preconf=True, arrival=arrival)

    def _preconf_admit(log):
        from concurrent.futures import Future
        arrival = now_iso()
        tx = str((log or {}).get("transactionHash") or "").lower() if isinstance(log, dict) else ""
        try:
            topics = log["topics"]
            mint = str(log["address"]).lower()
            beneficiary = _topic_address(topics[2])
            valid = (P.real_hash(tx) is not None and isinstance(topics, list)
                     and str(topics[0]).lower() == TRANSFER_TOPIC and P.real_hash(topics[2]) is not None
                     and len(mint) == 42 and mint.startswith("0x") and int(mint, 16) >= 0
                     and beneficiary in watched)
        except (KeyError, IndexError, TypeError, ValueError):
            valid = False
        if not valid:
            preconf_live["invalid_logs"] += 1
            future = Future()
            future.set_result(None)
            return future
        preconf_seen.append(tx)              # socket delivery remains distinct from admitted decode
        return preconf_jobs[1].submit((CHAIN, tx, mint, beneficiary), log, arrival)

    def _start_preconf():
        from websockets.sync.client import connect
        _preconf_work()
        _say("[onchain-evm] preconf detection on %s" % P.PRECONF_WS)
        threading.Thread(target=P.run_preconf_stream, name="base-preconf", daemon=True, kwargs=dict(
            on_log=_preconf_admit, on_head=_preconf_head,
            addresses_fn=lambda: sorted(watched),
            connect_fn=lambda url: connect(url, max_size=2 ** 24, open_timeout=10),
            should_stop=should_stop, now_fn=time.monotonic, sleep_fn=preconf_stop.wait, say=_say,
            force_reconnect=force_preconf)).start()

    sell_seen = deque(maxlen=512)            # one decode per tx: a multi-coin sale emits several logs

    def on_sell_transfer(log):
        arrival = now_iso()
        if xc is not None and log.get("transactionHash"):
            xc.delivered_tx(log["transactionHash"])
        if not sell_candidate(log, set(watched), CHAIN):
            _sell_count("prefiltered")
            return
        tx_hash = str(log.get("transactionHash") or "").lower()
        with _SELL_LOCK:
            if tx_hash in sell_seen:
                return
            sell_seen.append(tx_hash)
        _sell_count("candidates")
        read_url = sell_read_url()
        if read_url is None:
            _sell_count("no_read_host")
            return

        def work():
            try:
                if getattr(_SELL_LOCAL, "conn", None) is None:
                    _SELL_LOCAL.conn = get_connection(str(DB))
                    m.ensure_onchain_evm_sell_shadow_table(_SELL_LOCAL.conn)
                decode_sell(log, watched=dict(watched), conn=_SELL_LOCAL.conn, arrival=arrival,
                            read_fn=_sell_read_fn(read_url, fallback=sell_fallback_url()))
            except Exception as exc:
                _sell_count("errors")
                _say("[onchain-evm] sell decode failed tx=%s (%s: %s)"
                     % (tx_hash[:12], type(exc).__name__, exc))
        _sell_pool().submit(work)

    def resubscribe_logs():
        """Asked by the push stream on its own thread: a log-silence verdict resubscribes once. The
        aggregate one also reads the lane window the silence may have missed. Trigger B does NOT
        (Gamma MEDIUM, Alpha 2026-10-16): the sparse 4663 transfer filter goes quiet >= 900 s ~29
        times a day on quiet markets, and each lane-window read is up to 45 getLogs on the backfill
        host that shares custody's pool key; B's escalated reconnect still reads it."""
        if acts["all"].take_resubscribe():
            gap["force_window"] = True
            return push.ALL_LOGS
        if acts["transfers"].take_resubscribe():
            return ("transfers",)
        return ()

    def force_reconnect():
        """Asked by the push stream on its own thread: a cross-check verdict, or a log silence the
        resubscribe did not end, drops the socket once."""
        if acts is not None and any([a.take_reconnect() for a in acts.values()]):
            gap["force_window"] = True
            _say("[onchain-evm] log silence: forcing a reconnect (no log came back after the resubscribe)")
            return True
        if xc is None or not xc.force.is_set():
            return False
        xc.force.clear()
        gap["force_window"] = True
        _say("[onchain-evm] j7 cross-check: forcing a reconnect (a live socket not delivering; last miss %s)"
             % xc.stats.get("last_miss"))
        return True

    if xc is not None:
        _j7_ro = []

        def _j7_read(last_id):
            if not _j7_ro:
                _j7_ro.append(get_readonly_connection(str(DB)))
            return m.memecoin_events_after(_j7_ro[0], last_id)

        def _j7_tick():
            for miss in j7_xcheck_tick(xc, _j7_read, watched=set(state.transfer_addresses or ()),
                                       sells_live=bool(sell_shadow), connected_at=state.connected_at,
                                       now=_wall_clock(),
                                       known_fn=lambda tx: _detected_before(_j7_ro[0], xc.chain, tx)):
                _say("[onchain-evm] j7 cross-check: J7 %s by %s (tx %s) not delivered by the socket "
                     "in %.0fs" % (miss["side"], miss["trader"][:10], miss["tx"][:12], miss["age_s"]))
        _start_j7_xcheck(xc, _j7_tick)

    # 4663 RECEIPT HINT (Alpha GO 2026-10-16, memecoin_receipt_hint): a fast-lane re-ask still waiting
    # for this watcher's row asks for its receipt. The hint's own worker reads it on the same hosts as a
    # pushed log and hands the receipt's Transfer log to on_transfer; never on the push thread.
    if not observe_only and CHAIN == "4663" and cfg.MEMECOIN_RH_FAST_RECEIPT_FETCH:
        from sniper.services import memecoin_receipt_hint as receipt_hint
        _hint_ro = []

        def _hint_known(tx):
            with transfer_lock:
                if tx in inflight or (dedupe_s > 0 and decided.seen(tx, dedupe_s)):
                    return True
            if not _hint_ro:
                _hint_ro.append(get_readonly_connection(str(DB)))
            return _detected_before(_hint_ro[0], CHAIN, tx)

        def _hint_read(method, params):
            base = read_transport.result if read_transport is not None else _rpc_result
            return _host_first(lambda m, p: base(rpc_url, m, p), tx_read_host,
                               {"hosts": {}, "host_ms": {}})(method, params)
        hint_local = {}       # the hint worker's own; only that one thread calls _hint_log

        def _hint_log(log):
            # ITS OWN WRITE CONNECTION AND DECIMALS CACHE (Gamma B1 on e4d9325e7): wconn is the push
            # thread's, and two threads committing on one sqlite3 connection can commit or roll back
            # each other's half-written rows. As 56's _canonical_decode and the backfill do.
            if "conn" not in hint_local:
                hint_local["conn"] = get_connection(str(DB))
                hint_local["cache"] = dict(decimals_cache)
            try:
                return on_transfer(log, conn=hint_local["conn"], cache=hint_local["cache"],
                                   arrival=now_iso(), delivered=True)
            except Exception:
                conn = hint_local.pop("conn", None)      # a close rolls back any partial publication
                if conn is not None:
                    conn.close()
                raise
        try:
            receipt_hint.start(receipt_hint.socket_path(CHAIN), read_fn=_hint_read, known_fn=_hint_known,
                               watched_fn=lambda: watched, log=_say, on_log=_hint_log)
            _say("[onchain-evm] receipt hint listener started")
        except OSError as exc:               # a taken name or a bind error: no hints, today's wait
            _say("[onchain-evm] receipt hint listener not started: %s: %s" % (type(exc).__name__, exc))

    def on_fallback():
        if observe_only:
            # The poll loop only ever detects armed-wallet transfers -- it has no Initialize
            # handling at all -- so it would do nothing for this lane and cost real_getLogs
            # requests against an endpoint already known to cap that call at a few blocks.
            _say("[onchain-evm] socket dark (observe-only: no poll fallback)")
            return
        _say("[onchain-evm] socket dark; polling until it returns%s"
             % (" (getLogs on the backfill host)" if backfill_url else ""))
        _drain_backfill()
        unresolved = [floor[0] - 1 for floor in (gap.get("pending"), gap.get("inflight"))
                      if floor is not None]
        start_block = min(unresolved) if unresolved else gap["last_block"]
        if canonical_work is not None:
            start_block = _frontier()
        rate = _chain_blocks_per_s(state)
        window_s = float(getattr(cfg, "MEMECOIN_ONCHAIN_LANE_MAX_AGE_S", 45.0) or 45.0)
        read = run_poll(duration_s=min(30.0, period_s * 30 or 30.0), period_s=max(0.25, period_s),
                        price=price, logs_url=backfill_url, start_block=start_block,
                        max_behind=int(window_s * rate + 0.999) if rate else None, read_transport=read_transport)
        # What the dark poll read is covered: the reconnect backfill starts after it, never re-reads.
        if backfill_url and read is not None and gap["last_block"] is not None:
            gap["last_block"] = max(gap["last_block"], read)
            gap["polled_hi"] = max(int(gap.get("polled_hi") or -1), read)
            _drain_backfill()
            pending = gap.get("pending")
            if pending is not None:
                gap["pending"] = (max(pending[0], read + 1), pending[1]) if pending[1] > read else None

    def should_stop():
        if preconf_stop.is_set():
            return True
        if duration_s is None:
            return False
        return (time.monotonic() - started) >= float(duration_s)

    canonical_local = threading.local()

    def _canonical_decode(item, arrival):
        log, roster = item
        log = dict(log)
        receipt_clock = log.pop("_receipt_clock", None)
        receipt_stages = log.pop("_receipt_stages", ())
        if receipt_clock is not None:
            stages = {"_receipt_clock": receipt_clock, "receipt_stages": list(receipt_stages)}
            _receipt_stage(stages, "decode_start")
            receipt_stages = tuple(stages["receipt_stages"])
        if publication_fence.closed.is_set():
            raise RuntimeError("watcher_stopped")
        try:
            if not hasattr(canonical_local, "conn"):
                canonical_local.conn = get_connection(str(DB))
                canonical_local.cache = dict(decimals_cache)
                canonical_local.signal = _bsc_lane_signal(False) if bsc_signal is not None else None
            return on_transfer(log, conn=canonical_local.conn, arrival=arrival, roster=roster,
                               cache=canonical_local.cache, lane_signal=canonical_local.signal,
                               delivered=True, receipt_clock=receipt_clock, receipt_stages=receipt_stages)
        except Exception:
            _canonical_close()                  # SQLite close rolls back any partial publication
            raise

    def _canonical_close():
        conn = getattr(canonical_local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                del canonical_local.conn
                getattr(getattr(canonical_local, 'signal', None), 'close', lambda: None)()

    def _persist_frontier():
        frontier = _frontier()
        if frontier is None:
            return
        try:
            beat = json.loads(HEARTBEAT.read_text())
        except (OSError, ValueError):
            beat = {}
        if isinstance(beat, dict):
            beat['last_block'] = frontier
            beat['canonical_decode'] = canonical_work.snapshot()
            _atomic_write_text(HEARTBEAT, json.dumps(beat))   # preserve ts: shutdown is not delivery

    def _canonical_admit(log):
        receipt_clock = receipt_producer.capture() if receipt_producer is not None else None
        arrival = now_iso()
        if receipt_clock is not None:
            log = dict(log, _receipt_clock=receipt_clock)
        tx_hash = str(log.get("transactionHash") or "").lower()
        if xc is not None and tx_hash:
            xc.delivered_tx(tx_hash)             # socket receipt, before queue admission
        return canonical_work.submit(log, arrival, watched)

    _say("[onchain-evm] push mode on %s" % ws_url.split("//", 1)[-1].split("/", 1)[0])
    if publication_fence is not None:
        _reader()                               # initialize the existing read executor on its owner
        canonical_work = _CanonicalDecodeWork(
            _canonical_decode, max_age_s=float(cfg.MEMECOIN_ONCHAIN_LANE_MAX_AGE_S),
            on_stop=_canonical_close, on_closed=getattr(tx_read_host, 'close', lambda: None),
            receipt_clock=receipt_producer)
    if preconf_on:
        _start_preconf()
    try:
        push.run_push_stream(
            ws_url, on_transfer=_canonical_admit if canonical_work is not None else on_transfer,
            ws_urls=cfg.memecoin_evm_ws_pool(CHAIN), chain_id=CHAIN, **cfg.memecoin_evm_ws_failover(CHAIN),
            **cfg.memecoin_evm_ws_canary(CHAIN),
            on_initialize=on_initialize,
            addresses_fn=addresses_fn,
            # BSC has no V4 PoolManager: no Initialize subscription, transfers only.
            pool_manager=V4.get(CHAIN, {}).get("pool_manager"),
            init_topic=_INIT_TOPIC, state=state,
            # Ethereum's 12s slots can be empty: tolerate two missed slots plus delivery slack.
            stale_after_s=(max(getattr(cfg, "MEMECOIN_EVM_WS_STALE_S", 15.0), 45.0) if CHAIN == "1"
                           else getattr(cfg, "MEMECOIN_EVM_WS_STALE_S", 15.0)),
            fallback_after_s=getattr(cfg, "MEMECOIN_EVM_WS_FALLBACK_S", 120.0),
            on_fallback=on_fallback, on_head=on_head, should_stop=should_stop,
            on_connect_attempt=_drop_acked,
            on_sell_transfer=on_sell_transfer if sell_shadow else None,
            force_reconnect=force_reconnect if xc is not None or acts is not None else None,
            resubscribe_logs=resubscribe_logs if acts is not None else None)
    finally:
        drained = True
        if publication_fence is not None:
            _persist_frontier()                 # a kill during drain must retain unfinished work
            drained = publication_fence.close()
        if canonical_work is not None:
            canonical_work.close()
        warm_stop.set()
        preconf_stop.set()
        for work in preconf_jobs:
            work.close()                     # no join/wait on source delivery or order handling
        if origin_worker is not None:
            origin_worker.close()
        if canonical_work is not None:
            _drain_backfill()
            _persist_frontier()
        getattr(bsc_signal, 'close', lambda: None)()
        if canonical_work is None:
            getattr(tx_read_host, "close", lambda: None)()
        if xc is not None:
            xc.stop.set()
        if roster_conn[0] is not None:
            roster_conn[0].close()
        wconn.close()
        if not drained:
            raise RuntimeError('watcher_publication_drain_timeout')


def run(*, duration_s=None, period_s=1.0, chain=None, observe_only=False):
    """Push when a custody socket resolves and push is enabled; poll otherwise.

    `MEMECOIN_EVM_WS_URL_4663` is None whenever no custody endpoint is configured -- the settings
    derivation refuses to build a socket on the chain-owner host -- so an unconfigured or
    misconfigured deployment keeps the polling behaviour it has today rather than subscribing
    somewhere it should not.

    OBSERVE-ONLY CHAINS REFUSE RATHER THAN DEGRADE (2026-09-16). No wallets are armed here, so the
    poll loop's transfer-detection has nothing to do, and its `eth_getLogs` is already known to be
    range-capped on chains added for observation only -- a poll fallback would not fail loudly, it
    would just burn requests and see nothing. Refusing at startup is the loud version of that.
    """
    from sniper.settings import memecoin as cfg
    if chain is not None:
        _configure(chain)
    ws_url = cfg.memecoin_evm_ws_url(CHAIN)
    if observe_only and not ws_url:
        raise SystemExit(
            "observe-only chain %s has no MEMECOIN_EVM_WS_URL_%s configured -- refusing to start "
            "rather than poll an endpoint known not to serve eth_getLogs at range" % (CHAIN, CHAIN))
    price = NativePrice(lambda: quote_usd_price(CHAIN))
    payment_stop = threading.Event()
    if not observe_only:
        price.start()                      # warm before the first transfer (2026-09-28)
        if any(c == CHAIN for c, _token in _PAYMENT_TICKERS):   # the relay payment token too
            threading.Thread(target=_payment_price_loop, name="payment-price-warm", daemon=True,
                             args=(payment_stop,)).start()
    # THE WARMERS END WITH THE WATCH (2026-09-29). They were never stopped: harmless in the process,
    # which exits with run(), but every test calling run() left both alive in its xdist worker.
    try:
        if getattr(cfg, "MEMECOIN_ONCHAIN_PUSH_ENABLED", False) and ws_url:
            return run_push(ws_url, duration_s=duration_s, period_s=period_s,
                            observe_only=observe_only, price=price)
        return run_poll(duration_s=duration_s, period_s=period_s, price=price)
    finally:
        price.stop()
        payment_stop.set()

if __name__ == "__main__":
    import logging
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=logging.WARNING, format="%(asctime)sZ %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float)
    parser.add_argument("--period", type=float, default=1.0)
    parser.add_argument("--chain", default="4663",
                        help="EVM chain id to watch (default 4663)")
    parser.add_argument("--observe-only", action="store_true",
                        help="pool-key discovery only: no wallet wait, no poll fallback, "
                             "refuses to start without a WS URL")
    parser.add_argument("--check-addresses", action="store_true",
                        help="report local eligibility without RPC or a watcher launch")
    args = parser.parse_args()
    if args.check_addresses:
        _configure(args.chain)
        try:
            read = get_readonly_connection(str(DB))
            try:
                status = address_readiness(read)
            finally:
                read.close()
        except Exception:
            print(json.dumps({'status': 'blocked', 'reason': 'address eligibility unavailable',
                              'network_id': CHAIN, 'eligible_addresses': None}))
            raise SystemExit(2)
        print(json.dumps(status, sort_keys=True))
        raise SystemExit(0 if status['status'] == 'eligible' else 3)
    run(duration_s=args.duration, period_s=max(0.25, args.period), chain=args.chain,
       observe_only=args.observe_only)
