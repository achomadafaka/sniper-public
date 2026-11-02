"""4663 receipt hint: the fast lane asks the watcher to read a source receipt now (Alpha GO 2026-10-16,
option B; operator standing fill-speed order).

WHY. A deposit-matched 4663 re-ask that needs the source's receipt proof (a shared route) waits up to
2 s for the watcher's detection row. Seven such signals since 10-08 expired proof_absent although the
source block came 0.2-1.2 s BEFORE the signal: the watcher's log push arrived 2.8-23.7 s after the
block (provider lag), or its log subscription was dead (10-16 16:37-17:52Z). The receipt existed.

WHAT. 0.4 s after such a re-ask is queued, still unresolved (MEMECOIN_RH_FAST_RECEIPT_FETCH_AFTER_S),
the feed process sends one datagram {tx_hash, wallet, mint, source_user_id} to the 4663 watcher. The
watcher's own bounded worker (never the push thread) reads that receipt, binds it to ALL FOUR fields
(the tx, a sealed successful block, the wallet's watched source user, a Transfer of the mint to the
wallet) and hands the receipt's own Transfer log to the watcher's on_transfer: the same decode, the
same deposit proof and the same detection row as a pushed log, so nothing new decides a buy and no
proof function changes. The fast lane learns the row's source fill on its usual 50 ms poll.

ONE DECISION. A tx the watcher already has in flight, decided, or recorded is a counted no-op, checked
before the read and again after it; a push that slips in between meets on_transfer's own in-flight
claim. A null receipt is read once more, then left to today's wait; a read error, a refused binding or
a failed known-check hands nothing.

NOT HANDLED, named: a dead watcher process (the datagram goes nowhere, the sender counts it, today's
wait stands, the watchdog relaunches the watcher); a hint for a buy whose proof is not yet in the
deposit store (the watcher's row then waits on Relay as a pushed log would).
"""
from __future__ import annotations

import errno
import json
import os
import queue
import re
import socket
import threading
import time

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
FIELDS = ("tx_hash", "wallet", "mint", "source_user_id")
_TX = re.compile(r"0x[0-9a-f]{64}\Z")
_ADDRESS = re.compile(r"0x[0-9a-f]{40}\Z")
_MAX_DATAGRAM = 1024


def socket_path(chain):
    """A Linux ABSTRACT socket name (leading NUL): no file to leave stale, and a second binder (a test,
    a second watcher) gets EADDRINUSE instead of unlinking the live watcher's socket. Any local process
    may send to it; a hint only asks for a receipt read the watcher's own checks then bind."""
    return "\0sniper-onchain-evm-receipt-hint-%s" % chain


def _hint(fields):
    """The four fields, lowercased where they are hex, or None when any is absent or malformed."""
    try:
        hint = {k: fields[k] for k in FIELDS}
    except (KeyError, TypeError):
        return None
    if not all(isinstance(v, str) for v in hint.values()):
        return None
    hint.update({k: hint[k].lower() for k in ("tx_hash", "wallet", "mint")})
    if (not _TX.fullmatch(hint["tx_hash"]) or not _ADDRESS.fullmatch(hint["wallet"])
            or not _ADDRESS.fullmatch(hint["mint"]) or not 0 < len(hint["source_user_id"]) <= 128):
        return None
    return hint


def encode(rec):
    hint = _hint(rec)
    return None if hint is None else json.dumps(hint, separators=(",", ":")).encode()


def decode(payload):
    try:
        return _hint(json.loads(payload))
    except (ValueError, TypeError):
        return None


def _topic_address(topic):
    return "0x" + str(topic).lower()[-40:]


def receipt_log(receipt, hint, watched):
    """(the receipt's own Transfer log of `hint`, None), or (None, why): the receipt must be the hint's
    tx, sealed and successful, the wallet must be watched for the hint's source user, and the receipt
    must carry a live Transfer of the hint's mint to that wallet."""
    if not isinstance(receipt, dict):
        return None, "malformed"
    if str(receipt.get("transactionHash") or "").lower() != hint["tx_hash"]:
        return None, "tx_mismatch"
    if receipt.get("status") != "0x1":
        return None, "reverted"
    block_hash = str(receipt.get("blockHash") or "").lower()
    if not _TX.fullmatch(block_hash) or block_hash == "0x" + "0" * 64:
        return None, "unsealed"
    identity = (watched or {}).get(hint["wallet"])
    if not identity or identity[0] != hint["source_user_id"]:
        return None, "wallet_mismatch"
    for log in receipt.get("logs") or ():
        topics = log.get("topics") if isinstance(log, dict) else None
        if (isinstance(topics, list) and len(topics) == 3 and str(topics[0]).lower() == TRANSFER_TOPIC
                and str(log.get("address") or "").lower() == hint["mint"]
                and _topic_address(topics[2]) == hint["wallet"] and log.get("removed", False) is False
                and str(log.get("transactionHash") or "").lower() == hint["tx_hash"]
                and str(log.get("blockHash") or "").lower() == block_hash):
            return dict(log), None
    return None, "no_transfer"


class Sender:
    """The feed side: one non-blocking datagram per hint. Never raises; a dead watcher is a count."""

    def __init__(self, path):
        self.path = path
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.stats = dict(sent=0, failed=0, invalid=0, last_error=None)

    def send(self, rec):
        payload = encode(rec)
        if payload is None:
            self.stats["invalid"] += 1
            return False
        try:
            self.sock.sendto(payload, self.path)
        except OSError as exc:
            self.stats["failed"] += 1
            self.stats["last_error"] = errno.errorcode.get(exc.errno, type(exc).__name__)
            return False
        self.stats["sent"] += 1
        return True


class ReceiptHints:
    """The watcher side: a bounded queue, one worker. `read_fn(method, params)` reads the receipt (the
    watcher's own tx-read host, then its read host); `known_fn(tx)` is True when the watcher already has
    the tx in flight, decided or recorded; `on_log(log)` is the watcher's on_transfer."""

    def __init__(self, *, read_fn, known_fn, watched_fn, on_log, log=print, cap=16, null_retry_s=0.15):
        self.read_fn, self.known_fn, self.watched_fn, self.on_log = read_fn, known_fn, watched_fn, on_log
        self.log, self.null_retry_s = log, float(null_retry_s)
        self.queue = queue.Queue(maxsize=cap)
        self.stats = dict(received=0, malformed=0, queue_full=0, outcomes={})
        self._stop = threading.Event()
        self._sock = None
        self._threads = []

    def offer(self, payload):
        hint = decode(payload)
        if hint is None:
            self.stats["malformed"] += 1
            return False
        try:
            self.queue.put_nowait(hint)
        except queue.Full:
            self.stats["queue_full"] += 1
            return False
        self.stats["received"] += 1
        return True

    def _known(self, tx):
        try:
            return "known" if self.known_fn(tx) else None
        except Exception:                        # noqa: BLE001 - unsure is never a second decode
            return "known_check_error"

    def _read(self, tx):
        try:
            receipt = self.read_fn("eth_getTransactionReceipt", [tx])
            if receipt is None and self.null_retry_s >= 0:
                time.sleep(self.null_retry_s)
                receipt = self.read_fn("eth_getTransactionReceipt", [tx])
        except Exception:                        # noqa: BLE001 - the wait stands
            return None, "read_error"
        return receipt, None if receipt is not None else "receipt_null"

    def handle(self, hint):
        """One hint to an outcome: handed, known, known_check_error, receipt_null, read_error or a
        receipt_log refusal. Only `handed` calls the watcher."""
        tx, started = hint["tx_hash"], time.perf_counter()
        outcome = self._known(tx)
        log = None
        if outcome is None:
            receipt, outcome = self._read(tx)
            if outcome is None:
                log, outcome = receipt_log(receipt, hint, self.watched_fn())
            if outcome is None:
                outcome = self._known(tx)        # the push thread may have taken it during the read
        read_ms = (time.perf_counter() - started) * 1000.0
        if outcome is None:
            self.on_log(log)
            outcome = "handed"
        counts = self.stats["outcomes"]
        counts[outcome] = counts.get(outcome, 0) + 1
        self.log("[onchain-evm] receipt hint %s outcome=%s read_ms=%.1f handled_ms=%.1f"
                 % (tx[:12], outcome, read_ms, (time.perf_counter() - started) * 1000.0))
        return outcome

    def _work(self):
        while not self._stop.is_set():
            try:
                hint = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.handle(hint)
            except Exception as exc:             # noqa: BLE001 - one bad hint never stops the worker
                self.log("[onchain-evm] receipt hint %s failed %s: %s"
                         % (hint["tx_hash"][:12], type(exc).__name__, str(exc)[:160]))

    def _listen(self):
        while not self._stop.is_set():
            try:
                payload = self._sock.recv(_MAX_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.2)
                continue
            self.offer(payload)

    def listen(self, path):
        """Bind `path` and start the listener and the worker. A filesystem path (tests) replaces a stale
        file; an abstract name (live) never unlinks anything, so a taken name raises EADDRINUSE."""
        abstract = path.startswith("\0")
        if not abstract and os.path.exists(path):
            os.unlink(path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.bind(path)
        if not abstract:
            os.chmod(path, 0o600)
        self._sock.settimeout(0.2)
        self._threads = [threading.Thread(target=fn, name=name, daemon=True)
                         for fn, name in ((self._listen, "receipt-hint-listen"), (self._work, "receipt-hint-work"))]
        for thread in self._threads:
            thread.start()
        return self

    def close(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=1.0)
        if self._sock is not None:
            self._sock.close()


def start(path, **kwargs):
    return ReceiptHints(**kwargs).listen(path)
