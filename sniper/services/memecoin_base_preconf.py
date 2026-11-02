"""Base 8453: armed sources' buys from Flashblocks preconfirmations, live (spec
docs/superpowers/specs/2026-10-09-memecoin-base-flashblocks-detection-design.md).

The 8453 watcher's second socket subscribes `pendingLogs` on Base's preconfirmation endpoint with its own
Transfer-to-armed filter. A transfer seen there is decoded through the watcher's ordinary path from the
preconf host's reads and written as `source_observation_state='preconfirmed'`. This module holds the
pieces that are not the watcher's decode:

- the endpoint constants and the pending filter;
- `confirm`: the sealed log of the same transaction arrived, so the row is `canonical` with its block hash
  and the lead (sealed arrival minus preconf arrival);
- `retract`: the transaction never made a final block (a phantom source buy);
- `resolve`: the one canonical receipt read for a row whose block sealed without its log.

MEASURED 2026-10-09: a preconfirmed read differs from its sealed copy ONLY in block hashes (the tx's is
null, the receipt's and the logs' are 0x00..0 until the block seals). A zero hash is "not sealed yet"; it
is never stored as a block hash by `confirm`, which writes only a real one."""
from __future__ import annotations

import logging
import threading

from sniper.services.memecoin_onchain_push import armed_transfer_filter

logger = logging.getLogger(__name__)

CHAIN = "8453"
IDENTITY = "0x2105"
PRECONF_WS = "wss://mainnet-preconf.base.org"
PRECONF_HTTP = "https://mainnet-preconf.base.org"
SEALED_HTTP = "https://mainnet.base.org"
ZERO_HASH = "0x" + "0" * 64
HEAD_STALE_S = 10.0           # no newHeads frame for this long: reconnect (a deaf subscription)
MISSED_RECONNECT = 3          # sealed armed transfers in a row the preconf socket never delivered
RETRACT_NULLS = 2             # null canonical receipts, at two heads past the block, before retracting
RESOLVE_MAX_READS = 5         # failed reads before the row is left preconf_unresolved and paged


def pending_filter(addresses):
    """`eth_subscribe` params for pending Transfers TO the armed addresses, or None when there is none to
    watch (never a wildcard)."""
    sealed = armed_transfer_filter(addresses)
    return None if sealed is None else ["pendingLogs", sealed[1]]


def real_hash(value):
    """A sealed block hash, lowercased, or None for null, empty or the preconf zero hash."""
    text = str(value or "").lower()
    if len(text) != 66 or not text.startswith("0x") or text == ZERO_HASH:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return text


def _quantity(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return int(str(value), 16)
    except (TypeError, ValueError):
        return None


def _rows(tx_hash):
    tx = str(tx_hash).lower()
    return "signature >= ? AND signature < ? AND source_observation_state='preconfirmed'", (
        "%s:%s:" % (CHAIN, tx), "%s:%s;" % (CHAIN, tx))


def confirm(conn, tx_hash, *, block, block_hash, sealed_at):
    """The sealed log (or the canonical receipt) of a preconfirmed transaction: its rows become
    `canonical` with the real block hash, the block it sealed in and the lead. Rows updated (0 when the
    hash is not a real one or no preconfirmed row exists); commits its one write."""
    sealed, number = real_hash(block_hash), _quantity(block)
    if sealed is None or number is None or number < 0:
        return 0
    where, args = _rows(tx_hash)
    cur = conn.execute(
        "UPDATE onchain_wallet_detections SET source_observation_state='canonical', source_slot=?, "
        "source_valuation_json=json_set(coalesce(source_valuation_json, '{}'), "
        "'$.preconf.block_hash', ?, '$.preconf.sealed_at', ?, "
        "'$.preconf.lead_s', ? - json_extract(source_valuation_json, '$.preconf.first_at'), "
        "'$.preconf.moved', json(CASE WHEN json_extract(source_valuation_json, '$.preconf.block_number') = ? "
        "THEN 'false' ELSE 'true' END), '$.source_initiation.block_hash', ?, "
        "'$.source_initiation.block_number', ?) WHERE " + where,
        (number, sealed, float(sealed_at), float(sealed_at), number, sealed, number, *args))
    conn.commit()
    return cur.rowcount


def retract(conn, tx_hash):
    """A preconfirmed transaction that never made a final block: its rows become `retracted`, which the
    on-chain lane skips. Rows updated; commits its one write."""
    where, args = _rows(tx_hash)
    cur = conn.execute("UPDATE onchain_wallet_detections SET source_observation_state='retracted' WHERE "
                       + where, args)
    conn.commit()
    return cur.rowcount


def resolve(conn, tx_hash, *, read_fn, now):
    """One canonical receipt read for a preconfirmed transaction whose block sealed without its log.
    'canonical' (confirmed), 'retracted' (a canonical receipt that reverted), 'null' (no receipt: the
    caller retracts at RETRACT_NULLS), or None (a failed read or a node still preconfirmed: ask again)."""
    try:
        receipt = read_fn("eth_getTransactionReceipt", [str(tx_hash).lower()])
    except Exception:
        return None
    if receipt is None:
        return "null"
    if not isinstance(receipt, dict):
        return None
    status, block = _quantity(receipt.get("status")), _quantity(receipt.get("blockNumber"))
    requested = real_hash(tx_hash)
    # Partial/misrouted data is not execution failure. Standard receipts omit chainId;
    # a provider that supplies it must agree with the Base endpoint identity.
    if (requested is None or real_hash(receipt.get("transactionHash")) != requested
            or real_hash(receipt.get("blockHash")) is None
            or block is None or block < 0 or status not in (0, 1)
            or ("chainId" in receipt and _quantity(receipt["chainId"]) != int(CHAIN))):
        return None
    if status == 0:
        return "retracted" if retract(conn, tx_hash) else None
    return "canonical" if confirm(conn, tx_hash, block=receipt.get("blockNumber"),
                                  block_hash=receipt["blockHash"], sealed_at=now) else None


def valuation_note(log, first_at):
    """The `preconf` member a preconfirmed row's valuation carries from its first sighting."""
    return {"first_at": float(first_at), "block_number": _quantity((log or {}).get("blockNumber")),
            "tx_index": _quantity((log or {}).get("transactionIndex"))}


class Tracker:
    """The watcher's preconfirmed transactions still waiting for their sealed log (thread-safe, bounded).
    Each holds its block, its first sighting and its resolve history."""

    def __init__(self, cap=1024):
        import collections
        import threading
        self._lock, self._rows, self._cap = threading.Lock(), collections.OrderedDict(), cap

    def add(self, tx_hash, *, block, first_at):
        with self._lock:
            self._rows[str(tx_hash).lower()] = {"block": int(block), "first_at": float(first_at),
                                                "nulls": 0, "errors": 0, "last_resolve_head": None}
            while len(self._rows) > self._cap:
                self._rows.popitem(last=False)

    def sealed(self, tx_hash):
        """The entry of a transaction whose sealed log arrived (removed), or None."""
        with self._lock:
            return self._rows.pop(str(tx_hash).lower(), None)

    def due(self, head):
        """Transactions whose block has sealed (the head is past it) and that are still waiting."""
        with self._lock:
            return [tx for tx, row in self._rows.items() if head is not None and int(head) > row["block"]]

    def claim_due(self, head):
        """Claim one read per strictly later canonical head, even with concurrent head callbacks."""
        number = _quantity(head)
        if number is None or number < 0:
            return []
        with self._lock:
            due = []
            for tx, row in self._rows.items():
                previous = row["last_resolve_head"]
                if number > row["block"] and (previous is None or number > previous):
                    row["last_resolve_head"] = number
                    due.append(tx)
            return due

    def count(self, tx_hash, key):
        with self._lock:
            row = self._rows.get(str(tx_hash).lower())
            if row is None:
                return None
            row[key] += 1
            return row[key]

    def __len__(self):
        with self._lock:
            return len(self._rows)


def settle(tracker, head, *, resolve, retract, unresolved, page, confirmed):
    """For each transaction due at `head`, one canonical read (`resolve(tx)`, see `resolve`): confirmed,
    retracted (paged), a second null retracts (paged), and RESOLVE_MAX_READS failed reads leave the rows
    `preconf_unresolved` (paged). The resolver thread only; every write is the caller's own."""
    for tx in tracker.claim_due(head):
        answer = resolve(tx)
        if answer == "canonical":
            tracker.sealed(tx)
            confirmed(tx)
        elif answer == "retracted":
            tracker.sealed(tx)
            page("retracted", tx)
        elif answer == "null":
            if (tracker.count(tx, "nulls") or 0) >= RETRACT_NULLS:
                tracker.sealed(tx)
                retract(tx)
                page("retracted", tx)
        elif (tracker.count(tx, "errors") or 0) >= RESOLVE_MAX_READS:
            tracker.sealed(tx)
            unresolved(tx)
            page("preconf_unresolved", tx)


def mark_unresolved(conn, tx_hash):
    """Rows whose canonical state could not be read RESOLVE_MAX_READS times: `preconf_unresolved`, which
    the lane treats like `preconfirmed` (only `retracted` is skipped). Commits its one write."""
    where, args = _rows(tx_hash)
    cur = conn.execute("UPDATE onchain_wallet_detections SET source_observation_state='preconf_unresolved' "
                       "WHERE " + where, args)
    conn.commit()
    return cur.rowcount


class DecodeWork:
    """Bounded observation admission; dedupe/age checks precede decode, with no executor backlog."""

    def __init__(self, decode, *, workers=2, cap=128, max_age_s=2.0, now_fn=None, on_stop=None):
        from collections import deque, OrderedDict
        from time import monotonic
        self._decode, self._now, self._on_stop = decode, now_fn or monotonic, on_stop
        self._cap, self._age = cap, max_age_s
        self._condition, self._queue = threading.Condition(), deque()
        self._keys, self._recent, self._stopping, self._active = {}, OrderedDict(), False, 0
        self._counts = dict(accepted=0, duplicate=0, full=0, expired=0, stopped=0, completed=0, failed=0, cancelled=0)
        self._threads = [threading.Thread(target=self._run, daemon=True,
                                         name="base-preconf-decode-%d" % n) for n in range(workers)]
        for thread in self._threads:
            thread.start()

    def submit(self, key, log, arrival):
        from concurrent.futures import Future
        future = Future()
        with self._condition:
            now = self._now()
            while self._recent and now - next(iter(self._recent.values()))[0] > self._age:
                self._recent.popitem(last=False)
            existing = self._keys.get(key) or self._recent.get(key, (None, None))[1]
            if self._stopping:
                self._counts["stopped"] += 1
            elif existing is not None:
                self._counts["duplicate"] += 1
                return existing
            elif len(self._keys) >= self._cap:
                self._counts["full"] += 1
            else:
                self._keys[key] = future
                self._queue.append((key, log, arrival, now, future))
                self._counts["accepted"] += 1
                self._condition.notify()
                return future
        future.set_result(None)                     # typed refusal; sealed source path stays available
        return future

    def _run(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._stopping or self._queue)
                    if self._stopping:
                        return
                    key, log, arrival, queued_at, future = self._queue.popleft()
                if not future.set_running_or_notify_cancel():
                    with self._condition:
                        self._keys.pop(key, None)
                        self._counts["cancelled"] += 1
                    continue
                with self._condition:
                    expired = self._now() - queued_at > self._age
                    if expired:
                        self._keys.pop(key, None)
                        self._counts["expired"] += 1
                    else:
                        self._active += 1
                if expired:
                    future.set_result(None)
                    continue
                result, error = None, None
                try:
                    result = self._decode(log, arrival)
                except Exception as exc:
                    error = exc
                    logger.exception("Base preconf decode failed")
                with self._condition:
                    self._keys.pop(key, None)
                    self._active -= 1
                    self._counts["failed" if error else "completed"] += 1
                    if result is True:
                        self._recent[key] = (self._now(), future)
                        while len(self._recent) > self._cap:
                            self._recent.popitem(last=False)
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(result)
        finally:
            if self._on_stop is not None:
                self._on_stop()

    def snapshot(self):
        with self._condition:
            oldest = self._queue[0][3] if self._queue else None
            return dict(self._counts, queued=len(self._queue), active=self._active, capacity=self._cap,
                        oldest_age_s=None if oldest is None else max(0.0, self._now() - oldest))

    def close(self, *, wait=False):
        with self._condition:
            self._stopping = True
            queued = list(self._queue)
            self._queue.clear()
            for key, *_ in queued:
                self._keys.pop(key, None)
            self._counts["stopped"] += len(queued)
            self._condition.notify_all()
        for *_, future in queued:
            if future.set_running_or_notify_cancel():
                future.set_result(None)
            else:
                with self._condition:
                    self._counts["cancelled"] += 1
        if wait:
            for thread in self._threads:
                thread.join(timeout=2.0)


class HeadWork:
    """Canonical resolver owns one worker and one newest pending head, separate from decode load."""

    def __init__(self, resolve, *, on_stop=None):
        self._resolve, self._on_stop = resolve, on_stop
        self._condition, self._pending, self._latest = threading.Condition(), None, None
        self._stopping, self._active = False, None
        self._counts = dict(accepted=0, coalesced=0, failed=0, stopped=0)
        self._thread = threading.Thread(target=self._run, daemon=True, name="base-preconf-settle")
        self._thread.start()

    def submit(self, head):
        number = _quantity(head)
        if number is None or number < 0:
            return
        with self._condition:
            if self._stopping:
                self._counts["stopped"] += 1
            elif self._latest is not None and number <= self._latest:
                self._counts["coalesced"] += 1
            else:
                self._counts["coalesced"] += int(self._pending is not None)
                self._pending = self._latest = number
                self._counts["accepted"] += 1
                self._condition.notify()

    def _run(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._stopping or self._pending is not None)
                    if self._stopping:
                        return
                    head, self._pending = self._pending, None
                    self._active = head
                try:
                    self._resolve(head)
                except Exception:
                    with self._condition:
                        self._counts["failed"] += 1
                    logger.exception("Base canonical resolver failed")
                finally:
                    with self._condition:
                        self._active = None
        finally:
            if self._on_stop is not None:
                self._on_stop()

    def snapshot(self):
        with self._condition:
            return dict(self._counts, active_head=self._active, pending_head=self._pending)

    def close(self, *, wait=False):
        with self._condition:
            self._stopping = True
            self._counts["stopped"] += int(self._pending is not None)
            self._pending = None
            self._condition.notify_all()
        if wait:
            self._thread.join(timeout=2.0)


def run_preconf_stream(*, on_log, on_head, addresses_fn, connect_fn, should_stop, now_fn, sleep_fn, say,
                       force_reconnect=None, roster_check_s=5.0, recv_timeout_s=1.0):
    """The preconf socket's loop, on its own thread, until `should_stop()`: check the chain, subscribe
    `newHeads` and the pending Transfer filter, and hand each pending log to `on_log` and each head number
    to `on_head`. A wrong chain stops it for good. A refused subscription or a socket error waits
    next_refusal_backoff (60 s, doubling to 600). A deaf socket (no head for HEAD_STALE_S), a forced
    reconnect (the watcher's missed-delivery count) or a roster change opens a new one. An empty roster
    subscribes nothing."""
    import json
    from sniper.services.memecoin_onchain_push import next_refusal_backoff
    delay = 0.0
    while not should_stop():
        if delay:
            sleep_fn(delay)
            if should_stop():
                return
        roster = sorted(set(addresses_fn() or ()))
        flt = pending_filter(roster)
        if flt is None:
            delay = roster_check_s
            continue
        try:
            ws = connect_fn(PRECONF_WS)
        except Exception as exc:                                  # noqa: BLE001 - named, then waited
            say("[base-preconf] connect failed: %s: %s" % (type(exc).__name__, str(exc)[:120]))
            delay = next_refusal_backoff(delay)
            continue
        reason = None
        try:
            subs, pending = {}, []
            last_head = checked = now_fn()

            def dispatch(frame):
                nonlocal last_head
                if not isinstance(frame, dict) or frame.get("method") != "eth_subscription":
                    return True
                params = frame.get("params")
                if not isinstance(params, dict):
                    return True
                subscription, result = params.get("subscription"), params.get("result")
                if not isinstance(subscription, str) or not isinstance(result, dict):
                    return True
                kind = subs.get(subscription)
                if kind == "newHeads":
                    number = _quantity(result.get("number"))
                    if number is not None and number >= 0:
                        last_head = now_fn()
                        on_head(number)
                elif kind == "pendingLogs":
                    on_log(result)
                else:
                    return False                         # notification arrived before its reply
                return True

            def response(request_id):
                deadline = now_fn() + 10.0
                while True:
                    remaining = deadline - now_fn()
                    if remaining <= 0:
                        raise TimeoutError("Base subscription response deadline")
                    frame = json.loads(ws.recv(timeout=remaining))
                    if isinstance(frame, dict) and frame.get("id") == request_id:
                        return frame
                    if not dispatch(frame):
                        if len(pending) >= 64:
                            raise RuntimeError("Base subscription notification buffer full")
                        pending.append(frame)

            ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}))
            chain = response(1).get("result")
            if chain != IDENTITY:
                say("[base-preconf] wrong chain %r on %s: preconf detection stopped" % (chain, PRECONF_WS))
                return
            for n, params in ((2, ["newHeads"]), (3, flt)):
                ws.send(json.dumps({"jsonrpc": "2.0", "id": n, "method": "eth_subscribe", "params": params}))
                reply = response(n)
                subscription = reply.get("result")
                if reply.get("error") is not None or not isinstance(subscription, str) or not subscription:
                    raise RuntimeError("subscribe %s refused: %s" % (params[0], reply.get("error")))
                subs[subscription] = params[0]
                pending[:] = [frame for frame in pending if not dispatch(frame)]
            say("[base-preconf] subscribed: %d armed address(es)" % len(roster))
            delay = 0.0
            while not should_stop():
                now = now_fn()
                if now - last_head > HEAD_STALE_S:
                    reason = "deaf: no head for %.0f s" % (now - last_head)
                    break
                if force_reconnect is not None and force_reconnect.is_set():
                    force_reconnect.clear()
                    reason = "missed sealed transfers"
                    break
                if now - checked >= roster_check_s:
                    checked = now
                    if sorted(set(addresses_fn() or ())) != roster:
                        reason = "roster changed"
                        break
                try:
                    frame = json.loads(ws.recv(timeout=recv_timeout_s))
                except TimeoutError:
                    continue
                dispatch(frame)
        except Exception as exc:                                  # noqa: BLE001 - named, then waited
            say("[base-preconf] socket error: %s: %s" % (type(exc).__name__, str(exc)[:160]))
            delay = next_refusal_backoff(delay)
        finally:
            try:
                ws.close()
            except Exception:                                     # noqa: BLE001 - closing a dead socket
                pass
        if reason:
            say("[base-preconf] reconnect: %s" % reason)
            delay = max(delay, 5.0)


# ---- D1(b), Alpha 2026-10-09: one local read before signing ----

_PRESIGN = threading.local()          # each signing thread keeps its own read-only ledger handle


def _ledger_path():
    from sniper.util.state_paths import state_path
    return str(state_path("sniper_tracker.db"))


def _readonly(path):
    conns = getattr(_PRESIGN, "conns", None)
    if conns is None:
        conns = _PRESIGN.conns = {}
    if path not in conns:
        # Kept per thread: a primary-key read on it measured p50/p90 0.002/0.002 ms on the real ledger
        # (2026-10-09, 400 reads) against 1.9 ms for a fresh handle. WAL readers never wait on writers;
        # the busy wait is still bounded to 50 ms, not the helper's 30 s, because this is the fill path.
        from sniper.persistence.db_core import get_readonly_connection
        conn = get_readonly_connection(path)
        conn.execute("PRAGMA busy_timeout=50")
        conns[path] = conn
    return conns[path]


def presign_check(origin, *, ledger_path=None):
    """The executor's pre-sign check for a Base on-chain detection (origin `onchain:8453:<tx>:<mint>`),
    else None. Calling it reads the row's state by primary key on a read-only handle (no provider call):
    'source_retracted' when the source buy was proved a phantom, else None. Preconfirmed, canonical,
    unresolved, a row from before this change and an unreadable ledger all proceed (the last is named):
    only a proven phantom refuses."""
    origin = str(origin or "")
    # The Base fast lane books the same source as rhfast:8453:<tx>:<mint> (plan 2026-10-14).
    prefix = next((p for p in ("onchain:", "rhfast:") if origin.startswith(p + CHAIN + ":")), None)
    if prefix is None:
        return None
    signature = origin[len(prefix):]

    def check():
        try:
            row = _readonly(ledger_path or _ledger_path()).execute(
                "SELECT source_observation_state FROM onchain_wallet_detections WHERE signature=?",
                (signature,)).fetchone()
        except Exception:
            logger.exception("base preconf: presign read failed for %s", signature)
            return None
        return "source_retracted" if row is not None and row[0] == "retracted" else None
    return check
