#!/usr/bin/env python3
"""Journal official Ink Subblocks without touching the funded ledger."""

import argparse
import json
import os
import queue
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sniper.persistence import db_evm_stream as journal
from sniper.services.memecoin_ink_subblocks import InkSubblockState

DEFAULT_STREAM = "wss://subblocks.inkonchain.com/ws"
HEARTBEAT_NAME = "onchain_ink_watch.heartbeat"
BEAT_MAX_AGE_S = 180.0      # the EVM watchers' MEMECOIN_WATCHER_HEARTBEAT_MAX_AGE_S default


SERIES_NAME = "onchain_ink_watch.minutes.jsonl"
SERIES_MAX_BYTES = 5_000_000          # ~23 days of minutes; one rotated backup, never more


class MinuteSeries:
    """One JSONL line per completed UTC minute, from the heartbeat thread (G2 R1, 2026-09-30).

    The heartbeat keeps only the latest cumulative counters, so the shadow's busiest minute could
    not be found afterwards. Each line: the minute, cumulative frames and their delta over the
    minute, sessions, overflows, queue depth and the last block the journal parsed. The first line
    after a start is `partial`, and so is the first minute after a heartbeat stall: its delta
    covers every beatless minute before it (Gamma LOW2, 2026-09-30). `span_s` is the time between
    the two beats the delta is measured over. Bounded: past `max_bytes` the file rotates to `.1`
    (one backup). A write failure is printed and never raised: evidence, not a dependency."""

    def __init__(self, path, *, max_bytes=SERIES_MAX_BYTES):
        self.path, self.max_bytes = Path(path), int(max_bytes)
        self.minute = None
        self.frames_at_start = self.start_at = None
        self.partial = True
        self.last = self.last_at = None

    def observe(self, now, stats, qsize):
        minute = now.replace(second=0, microsecond=0)
        snapshot = {"frames": int(stats.get("frames", 0)), "sessions": stats.get("sessions", 0),
                    "overflows": stats.get("overflows", 0), "queue_depth": qsize,
                    "last_block": stats.get("last_block")}
        if self.minute is None:
            self.minute, self.frames_at_start, self.start_at = minute, snapshot["frames"], now
        elif minute != self.minute:
            line = {"minute": self.minute.strftime("%Y-%m-%dT%H:%MZ"), "partial": self.partial,
                    "frames_delta": self.last["frames"] - self.frames_at_start,
                    "span_s": round((self.last_at - self.start_at).total_seconds(), 3), **self.last}
            self._write(line)
            self.partial = (minute - self.minute).total_seconds() != 60
            self.minute, self.frames_at_start, self.start_at = minute, self.last["frames"], self.last_at
        self.last, self.last_at = snapshot, now

    def _write(self, line):
        try:
            text = json.dumps(line, sort_keys=True) + "\n"
            if self.path.exists() and self.path.stat().st_size + len(text) > self.max_bytes:
                self.path.replace(self.path.with_name(self.path.name + ".1"))
            with self.path.open("a") as fh:
                fh.write(text)
        except OSError as exc:
            print(f"[ink-watch] minute series unwritable: {type(exc).__name__}", file=sys.stderr,
                  flush=True)


def heartbeat_loop(stop, path, stats, threads, *, period_s=5.0, series=None, qsize_fn=None):
    """Write the reader's liveness from its own thread (Task 11, 2026-09-29): write time, frame
    counters and whether each worker thread is alive. The receive loop only bumps counters.
    `series` (G2 R1) gets every beat; it writes one line per completed minute."""
    path = Path(path)
    while True:
        if series is not None:
            try:
                series.observe(datetime.now(timezone.utc), stats,
                               qsize_fn() if qsize_fn is not None else None)
            except Exception as exc:        # noqa: BLE001 - evidence must never stop the beat
                print(f"[ink-watch] minute series failed: {type(exc).__name__}", file=sys.stderr,
                      flush=True)
        beat = {"ts": _utc_now(), "frames": stats.get("frames", 0),
                "overflows": stats.get("overflows", 0), "sessions": stats.get("sessions", 0),
                "last_frame_utc": stats.get("last_frame_utc"),
                "rpc": dict(stats.get("rpc") or {}),
                "threads": {name: t.is_alive() for name, t in threads.items() if t is not None}}
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(beat, sort_keys=True))
            tmp.replace(path)
        except OSError as exc:
            print(f"[ink-watch] heartbeat unwritable: {type(exc).__name__}", file=sys.stderr, flush=True)
        if stop.wait(period_s):
            return


def beat_verdict(beat, now, max_age_s=BEAT_MAX_AGE_S):
    """'' when healthy or unknowable (absent/garbled: the launcher owns that), else the watchdog's
    kill reason: `beat_stale:<age s>` for a hung process, `thread_dead:<names>` for a dead worker."""
    if not isinstance(beat, dict):
        return ""
    try:
        age = now - datetime.fromisoformat(str(beat["ts"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError):
        return ""
    if age >= max_age_s:
        return "beat_stale:%d" % age
    dead = sorted(name for name, alive in (beat.get("threads") or {}).items() if alive is False)
    return "thread_dead:%s" % ",".join(dead) if dead else ""


def enqueue_frame(frames, item):
    try:
        frames.put_nowait(item)
        return True
    except queue.Full:
        return False


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def reconcile_once(conn, rpc, *, limit=100):
    """Canonical receipt reconciliation; missing provider evidence leaves state provisional."""
    changed = 0
    for key, tx_hash in journal.provisional_for_reconcile(conn, limit):
        try:
            receipt = rpc("eth_getTransactionReceipt", [tx_hash])
            if not isinstance(receipt, dict) or not receipt.get("blockNumber"):
                continue
            block = rpc("eth_getBlockByNumber", [receipt["blockNumber"], False])
            if not isinstance(block, dict) or not block.get("hash"):
                continue
            before = journal.observation(conn, key)["state"]
            after = journal.reconcile_canonical(conn, key, receipt, block_hash=block["hash"])
            changed += after != before
        except (OSError, ValueError, KeyError, TypeError):
            continue
        finally:
            journal.mark_receipt_checked(conn, key, recheck=False, at_ns=time.time_ns())
    for key, receipt in journal.canonical_for_recheck(conn, limit):
        try:
            block = rpc("eth_getBlockByNumber", [receipt["blockNumber"], False])
            if isinstance(block, dict) and block.get("hash"):
                if journal.reconcile_canonical(conn, key, receipt,
                                                block_hash=block["hash"]) == "retracted":
                    changed += 1
        except (OSError, ValueError, KeyError, TypeError):
            continue
        finally:
            journal.mark_receipt_checked(conn, key, recheck=True, at_ns=time.time_ns())
    conn.commit()
    return changed


def promote_once(conn, rpc, *, limit=100):
    rows = journal.canonical_for_recheck(conn, limit)
    if not rows:
        return 0
    try:
        heads = {tag: rpc("eth_getBlockByNumber", [tag, False]) for tag in ("safe", "finalized")}
        heights = {tag: int(head["number"], 16) for tag, head in heads.items()
                   if isinstance(head, dict) and isinstance(head.get("number"), str)}
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    promoted = 0
    for key, receipt in rows:
        try:
            at = int(receipt["blockNumber"], 16)
            eligible = ("finalized" if heights.get("finalized", -1) >= at else
                        "safe" if heights.get("safe", -1) >= at else None)
            if eligible is None:
                continue
            block = rpc("eth_getBlockByNumber", [receipt["blockNumber"], False])
            if not isinstance(block, dict) or block.get("hash") != receipt["blockHash"]:
                continue
            promoted += journal.promote_finality(conn, key, eligible, block_hash=block["hash"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    conn.commit()
    return promoted


def project_canonical_once(stream, main, rpc, *, watched, quote_usd, decimals_fn,
                           detected_at, quote_price_age_s=None, wake=None, limit=100):
    """Commit canonical owner events before advancing the separate journal cursor."""
    from sniper.persistence import db_memecoin as funded
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    from sniper.services.memecoin_evm_onchain_watch import (
        source_events_from_receipt, wallet_paid_token_flows)

    projected = 0
    for change in journal.next_unprojected(stream, limit):
        key, state = change["observation_key"], change["state"]
        if state == "retracted":
            # Only a registry owner can have a detection to mark; any other retraction (a panel
            # owner, or any unwatched sender) must not take the funded ledger's write lock.
            record = journal.observation(stream, key)
            if record and str(record["owner"]).lower() in watched:
                main.execute("UPDATE onchain_wallet_detections SET "
                             "source_observation_state='retracted' "
                             "WHERE source_observation_key=?", (key,))
                main.commit()
        elif state in {"canonical_unsafe", "safe", "finalized"}:
            record = journal.observation(stream, key)
            receipt = json.loads(record["canonical_json"] or "null")
            frontier = journal.next_backfill_block(stream)
            if ((frontier is not None and frontier <= record["block_number"])
                    or not isinstance(receipt, dict)
                    or journal.canonical_block_hash(stream, record["block_number"])
                    != receipt.get("blockHash", "").lower()
                    or journal.has_open_gap_through(stream, record["block_number"])):
                break
            owner = record["owner"].lower()
            if owner in watched:
                if quote_usd is None:
                    # Unvalued: hold here, BEFORE the block and tx reads (Gamma LOW3, 2026-09-30).
                    # The loop retries the price every PRICE_EVERY_S; reading on every 1 s pass
                    # meanwhile turned a Binance outage into 2 research RPC/s.
                    break
                block = rpc("eth_getBlockByNumber", [receipt["blockNumber"], False]) if receipt else None
                tx = rpc("eth_getTransactionByHash", [record["tx_hash"]]) if block else None
                if (not isinstance(block, dict) or not isinstance(tx, dict)
                        or block.get("hash") != receipt["blockHash"]
                        or tx.get("hash", "").lower() != record["tx_hash"].lower()
                        or tx.get("blockHash") != block["hash"]
                        or tx.get("blockNumber") != receipt["blockNumber"]):
                    break
                try:
                    timestamp = int(block["timestamp"], 16)
                except (KeyError, TypeError, ValueError):
                    break
                if timestamp <= 0 or quote_usd is None:
                    break
                acquired, spent = wallet_paid_token_flows(
                    receipt, owner, CHAIN_QUOTE["57073"]["token"], tx)
                if acquired and spent:
                    try:
                        if any(int(decimals_fn(mint)) < 0 for mint in acquired):
                            break
                    except (TypeError, ValueError, OSError):
                        break
                events = source_events_from_receipt(
                    receipt, tx, wallet=owner, chain="57073", quote_usd=quote_usd,
                    decimals_fn=decimals_fn, prebalance_atomic=None,
                    source_timestamp=timestamp)
                user_id, handle = watched[owner]
                for event in events:
                    if event["side"] == "buy":
                        amount = int(event["token_amount_atomic"])
                        decimals = int(decimals_fn(event["mint"]))
                        inserted = funded.record_onchain_detection(
                            main, signature=event["origin_key"], source_user_id=user_id,
                            handle=handle, address=owner, mint=event["mint"],
                            network_id="57073", token_delta=amount / 10**decimals,
                            source_size_usd=event["source_size_usd"],
                            source_price=event["source_price"],
                            detected_at=detected_at, created_at=detected_at,
                            block_time=timestamp,
                            source_valuation_json=json.dumps({"backfill": True,
                                "ink_observation_key": key, "quote_amount_atomic":
                                event["quote_amount_atomic"], "quote_price_age_s":
                                quote_price_age_s}, separators=(",", ":")),
                            token_amount_atomic=event["token_amount_atomic"],
                            quote_amount_atomic=event["quote_amount_atomic"],
                            source_observation_key=key, source_observation_state=state,
                            commit=False)
                        main.commit()
                        projected += int(inserted)
                        if inserted and wake is not None:
                            wake()
                    else:
                        funded.record_onchain_evm_sell_shadow(
                            main, network_id="57073", tx_hash=event["tx_hash"],
                            address=owner, source_user_id=user_id, handle=handle,
                            mint=event["mint"], sold_atomic=event["token_amount_atomic"],
                            pre_atomic=None, post_atomic=None, proof=event["proof"],
                            proceeds_token=event["proceeds_token"],
                            proceeds_atomic=event["proceeds_atomic"],
                            block_number=receipt["blockNumber"], detected_at=detected_at,
                            decoded_at=detected_at, created_at=detected_at)
        journal.advance_cursor(stream, change["id"])
        stream.commit()
    return projected


def backfill_block(stream, rpc, number, *, watched, max_transactions=1000):
    """Complete one canonical block; a partial provider response leaves the gap open."""
    block = rpc("eth_getBlockByNumber", [hex(number), True])
    if not isinstance(block, dict) or block.get("number") != hex(number):
        return False
    block_hash = block.get("hash")
    if not isinstance(block_hash, str) or len(block_hash) != 66:
        return False
    transactions = block.get("transactions")
    if not isinstance(transactions, list) or len(transactions) > max_transactions:
        return False
    try:
        timestamp = int(block["timestamp"], 16)
    except (KeyError, TypeError, ValueError):
        return False
    if timestamp <= 0:
        return False
    records = []
    for tx in transactions:
        if (not isinstance(tx, dict) or tx.get("blockHash") != block_hash
                or tx.get("blockNumber") != hex(number)):
            return False
        tx_hash = tx.get("hash")
        if not isinstance(tx_hash, str) or len(tx_hash) != 66:
            return False
        # RECEIPTS ONLY FOR WATCHED SENDERS (2026-09-29, Alpha (C)). The owner rule is tx.from,
        # known from the block itself, so an unwatched tx's receipt can change nothing here. Reading
        # every receipt cost ~6.5 calls per 1 s Ink block (5.5 txs/block measured).
        if str(tx.get("from") or "").lower() not in watched:
            continue
        receipt = rpc("eth_getTransactionReceipt", [tx_hash])
        if (not isinstance(receipt, dict) or receipt.get("transactionHash") != tx_hash
                or receipt.get("blockHash") != block_hash
                or receipt.get("blockNumber") != hex(number)
                or receipt.get("status") not in ("0x0", "0x1", 0, 1)
                or not isinstance(receipt.get("logs"), list)):
            return False
        logs = receipt["logs"]
        if any(not isinstance(log, dict) or log.get("logIndex") is None
               or log.get("transactionHash") != tx_hash or log.get("blockHash") != block_hash
               for log in logs):
            return False
        owner = str(tx.get("from") or "").lower()
        if receipt["status"] not in ("0x1", 1):
            continue
        records.append({"observation_key": f"57073:canonical:{tx_hash.lower()}",
                        "payload_id": f"canonical:{block_hash.lower()}",
                        "block_number": number, "tx_hash": tx_hash.lower(), "owner": owner,
                        "state": "canonical_unsafe", "receipt": receipt,
                        "source_timestamp": timestamp, "source_time_basis": "block_timestamp",
                        "coverage_complete": True, "canonical_block_hash": block_hash})
    if not journal.record_canonical_block(stream, number, block_hash):
        return False
    for record in records:
        journal.insert_observation(stream, record)
    journal.resolve_covered_gaps(stream)
    stream.commit()
    return True


def backfill_once(stream, rpc, *, watched, limit=8):
    next_block = journal.next_backfill_block(stream)
    if next_block is None:
        return 0
    head = rpc("eth_blockNumber", [])
    try:
        latest = int(head, 16)
    except (TypeError, ValueError):
        return 0
    completed = 0
    for number in range(next_block, min(latest + 1, next_block + max(1, min(limit, 64)))):
        if not backfill_block(stream, rpc, number, watched=watched):
            break
        if not journal.advance_backfill(stream, number):
            break
        stream.commit()
        completed += 1
    return completed


PANEL_OWNER = (None, "g2-observation-panel")


def load_panel(path):
    """The frozen G2 observation panel: {"owners": frozenset(lowercase addresses), "source",
    "frozen_at"}. Raises ValueError on anything malformed; an empty panel is malformed."""
    import re
    data = json.loads(Path(path).read_text())
    owners = data.get("owners") if isinstance(data, dict) else None
    if not isinstance(owners, list) or not owners:
        raise ValueError("Ink observation panel has no owners list")
    out = set()
    for owner in owners:
        if not isinstance(owner, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", owner):
            raise ValueError("Ink observation panel has a malformed address")
        out.add(owner.lower())
    return {"owners": frozenset(out), "source": data.get("source"),
            "frozen_at": data.get("frozen_at")}


PRICE_EVERY_S = 10.0
BINANCE_ETH_URL = "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT"


def binance_eth_usd(http=None, timeout=3.0):
    """ETH/USD for valuing a projected detection: ONE plain Binance GET, no fallback (Alpha,
    2026-09-30, from Gamma's pre-read). The funded price seam falls back to FMP on the funded
    FMP_API_KEY, which this process holds because it loads .env; the shadow must never spend it.
    Any failure or unusable reply is None: the projection stays unvalued and censored, never
    zero. The caller's PRICE_EVERY_S cadence is its cache."""
    import math
    if http is None:
        from sniper.execution.evm_wallet import _http as http
    try:
        status, reply, _extra = http("GET", BINANCE_ETH_URL, timeout=timeout)
        price = float((reply or {}).get("price")) if status == 200 else None
    except Exception:           # noqa: BLE001 - unknown price, never a raise into the loop
        return None
    return price if price is not None and math.isfinite(price) and price > 0 else None


def price_due(registry, *, since_last_s):
    """Read ETH/USD only when a registry owner could be projected (Alpha, 2026-09-30). The read is
    Binance, the same host the funded runner prices ETH chains from; with no registry owner the
    shadow has nothing to value, so it adds no load there."""
    return bool(registry) and since_last_s >= PRICE_EVERY_S


class _LazyMain:
    """The funded-ledger connection, opened on the first write (Alpha, 2026-09-30). A pass with
    nothing to write never takes a writable handle on sniper_tracker.db."""

    def __init__(self, scope):
        self._scope, self._cm, self._conn = scope, None, None

    def _get(self):
        if self._conn is None:
            self._cm = self._scope()
            self._conn = self._cm.__enter__()
        return self._conn

    def __getattr__(self, name):
        return getattr(self._get(), name)

    def commit(self):
        if self._conn is not None:
            self._conn.commit()

    def close(self, exc_info=(None, None, None)):
        if self._cm is not None:
            self._cm.__exit__(*exc_info)


def projection_tick(stream, rpc, main_scope, *, registry, panel, quote_usd, quote_age_s,
                    decimals_fn, wake):
    """One backfill + projection pass. The canonical backfill reads receipts for the registry
    owners AND the G2 observation panel (ground truth for the stream); the projection into the
    funded ledger sees the registry only, so a panel owner is never projected. Projection always
    runs, so its cursor keeps moving; it opens the funded ledger only when it has a row to write."""
    backfill_watched = dict(registry)
    for address in panel:
        backfill_watched.setdefault(address, PANEL_OWNER)
    backfill_once(stream, rpc, watched=backfill_watched)
    main = _LazyMain(main_scope)
    try:
        project_canonical_once(
            stream, main, rpc, watched=dict(registry), quote_usd=quote_usd,
            decimals_fn=decimals_fn, detected_at=_utc_now(), quote_price_age_s=quote_age_s,
            wake=wake)
    except BaseException:
        main.close(sys.exc_info())
        raise
    main.close()


def _projection_loop(stop, journal_path, rpc_url, main_db_path, start_block, stats=None,
                     panel=frozenset()):
    from sniper.execution.evm_wallet import erc20_decimals
    from sniper.persistence import db_memecoin as funded
    from sniper.persistence.db_core import connection_scope, get_readonly_connection
    from sniper.services.axiom_address_verify import watcher_address_owners
    from sniper.services.memecoin_onchain_wake import default_address, send_wake

    if not Path(main_db_path).is_file():
        raise RuntimeError("Ink main ledger is absent")
    rpc = _rpc_reader(rpc_url, stats=stats)
    decimals_cache = {}

    def decimals(mint):
        mint = mint.lower()
        if mint not in decimals_cache:
            value = erc20_decimals(mint, rpc_url)
            if value is None:
                raise ValueError("Ink token decimals unavailable")
            decimals_cache[mint] = value
        return decimals_cache[mint]

    with connection_scope(main_db_path) as main:
        funded.ensure_memecoin_tables(main)
    with closing(sqlite3.connect(journal_path, timeout=5)) as stream, closing(
            get_readonly_connection(main_db_path)) as roster:
        journal.ensure_stream_tables(stream)
        journal.start_backfill(stream, start_block)
        stream.commit()
        price, price_read_at = None, 0.0
        while not stop.wait(1):
            try:
                if rpc("eth_chainId", []) != "0xdef1":
                    continue
                registry = {str(address).lower(): owner
                            for (chain, address), owner in watcher_address_owners(roster).items()
                            if str(chain) == "57073"}
                if price_due(registry, since_last_s=time.monotonic() - price_read_at):
                    price = binance_eth_usd()      # None on any failure: unvalued, never zero
                    price_read_at = time.monotonic()
                projection_tick(
                    stream, rpc, lambda: connection_scope(main_db_path), registry=registry,
                    panel=panel, quote_usd=price, quote_age_s=time.monotonic() - price_read_at,
                    decimals_fn=decimals, wake=lambda: send_wake(default_address()))
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                print(f"[ink-watch] projection unavailable: {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)


def _rpc_reader(url, *, stats=None, sleep=time.sleep, clock=time.monotonic):
    """JSON-RPC reader for ONE provider group. Every reader built on the same `stats` shares the
    group (Gamma LOW-2, 2026-09-29: reconcile and projection read one host): one lock for the counts
    in stats["rpc"] (the heartbeat publishes them) and one 429 hold. A 429 sets the hold to 1, 2,
    4 ... 30 s and propagates; every reader of the group waits the hold out before its next call."""
    stats = {} if stats is None else stats
    counts = stats.setdefault("rpc", {"calls": 0, "http_429": 0, "errors": 0})
    group = stats.setdefault("_rpc_group", {"lock": threading.Lock(), "streak": 0, "hold_until": 0.0})

    def read(method, params):
        wait = group["hold_until"] - clock()
        if wait > 0:
            sleep(wait)
        with group["lock"]:
            counts["calls"] += 1
        try:
            result = _read(method, params)
        except urllib.error.HTTPError as exc:
            with group["lock"]:
                counts["errors"] += 1
                if exc.code == 429:
                    counts["http_429"] += 1
                    group["streak"] += 1
                    group["hold_until"] = clock() + min(30.0, 2.0 ** (group["streak"] - 1))
            raise
        except Exception:
            with group["lock"]:
                counts["errors"] += 1
            raise
        with group["lock"]:
            group["streak"] = 0
        return result

    def _read(method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = urllib.request.Request(
            url, body, {"content-type": "application/json",
                        "user-agent": "Sniper-Ink-Reader/1"})
        with urllib.request.urlopen(request, timeout=5) as response:
            reply = json.load(response)
        if not isinstance(reply, dict) or reply.get("error") is not None:
            raise ValueError("Ink canonical RPC unavailable")
        return reply.get("result")
    return read


def read_host_refusal(url):
    """None when `url`'s domain is used by no other role, else the reason. Custody, the EVM read
    pools and the free-submission endpoints are all "another role": the Ink reader's canonical
    reads must never spend the budget Ink submission will need (Task 9 isolation)."""
    from urllib.parse import urlsplit
    from sniper.services.memecoin_evm_exact_capture import budget, taken_budgets
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return "no host in %r" % url
    if budget(url) in taken_budgets(os.environ):
        return "%s is used by another role (custody, a read pool or submission)" % host
    return None


def _reconcile_loop(stop, journal_path, rpc_url, stats=None):
    rpc = _rpc_reader(rpc_url, stats=stats)
    with sqlite3.connect(journal_path, timeout=5) as conn:
        journal.ensure_stream_tables(conn)
        last_finality = 0.0
        while not stop.wait(1.0):
            try:
                if rpc("eth_chainId", []) != "0xdef1":
                    continue
                reconcile_once(conn, rpc)
                if time.monotonic() - last_finality >= 10:
                    promote_once(conn, rpc)
                    last_finality = time.monotonic()
            except (OSError, ValueError):
                continue


def process_frames(frames, stop, overflow, journal_path, stats=None):
    """One journal owner; callback receives no RPC or main-ledger connection. `stats` gets the
    last parsed block number (G2 R1's series reads it; an int assignment, nothing more)."""
    conn = sqlite3.connect(journal_path, timeout=5)
    journal.ensure_stream_tables(conn)
    state = InkSubblockState()
    session = None
    pending_gaps = []
    try:
        while not stop.is_set() or not frames.empty():
            try:
                item_session, raw, received_mono_ns, received_utc = frames.get(timeout=0.2)
            except queue.Empty:
                continue
            frame = None
            try:
                frame = json.loads(raw) if isinstance(raw, str) else raw
                value = frame["metadata"]["block_number"]
                frame_block = int(value, 0) if isinstance(value, str) else int(value)
            except (ValueError, TypeError, KeyError):
                frame_block = state.block_number
            if stats is not None and frame_block is not None:
                stats["last_block"] = frame_block
            if overflow.is_set():
                start = state.block_number if state.block_number is not None else frame_block
                if start is not None:
                    journal.record_gap(conn, start, "queue_overflow")
                    pending_gaps.append((start, "queue_overflow"))
                state.gapped = True
                overflow.clear()
            if session != item_session:
                if session is not None and state.block_number is not None:
                    journal.record_gap(conn, state.block_number, "stream_restart")
                    pending_gaps.append((state.block_number, "stream_restart"))
                state = InkSubblockState()
                session = item_session
            result = state.accept(raw, received_mono_ns=received_mono_ns,
                                  received_utc=received_utc)
            if result["status"] in {"sequence_gap", "malformed"}:
                start = state.block_number if state.block_number is not None else frame_block
                if start is not None:
                    journal.record_gap(conn, start, result["status"])
                    pending_gaps.append((start, result["status"]))
            if (result["status"] in {"accepted", "payload_replaced"}
                    and frame_block is not None and isinstance(frame, dict)
                    and frame.get("index") == 0):
                for start, reason in pending_gaps:
                    journal.record_gap(conn, start, reason,
                                       end_block_number=max(start, frame_block))
                pending_gaps.clear()
            for payload in result["invalidated_payload_ids"]:
                journal.invalidate_payload(conn, payload)
            for observation in result["observations"]:
                journal.insert_observation(conn, observation)
            conn.commit()
            frames.task_done()
    finally:
        conn.close()


def run(*, stream_url, journal_path, rpc_url, main_db_path=None,
        start_block=None, queue_size=2048, heartbeat_path=None, panel=None):
    import websocket

    Path(journal_path).parent.mkdir(parents=True, exist_ok=True)
    frames = queue.Queue(maxsize=queue_size)
    stop = threading.Event()
    overflow = threading.Event()
    stats = {"frames": 0, "overflows": 0, "sessions": 0, "last_frame_utc": None}
    if panel:
        with closing(sqlite3.connect(journal_path, timeout=5)) as conn:
            journal.ensure_stream_tables(conn)
            journal.record_observation_panel(conn, panel["owners"], source=panel.get("source"),
                                             frozen_at=panel.get("frozen_at"))
    worker = threading.Thread(target=process_frames,
                              args=(frames, stop, overflow, journal_path, stats),
                              name="ink-stream-journal", daemon=True)
    worker.start()
    canonical = threading.Thread(target=_reconcile_loop,
                                 args=(stop, journal_path, rpc_url, stats),
                                 name="ink-canonical-reconcile", daemon=True)
    canonical.start()
    projection = None
    if start_block is not None:
        if not main_db_path or not Path(main_db_path).is_file():
            raise ValueError("Ink projection requires an existing main ledger")
        projection = threading.Thread(target=_projection_loop,
                                      args=(stop, journal_path, rpc_url, main_db_path, start_block,
                                            stats, (panel or {}).get("owners", frozenset())),
                                      name="ink-canonical-projection", daemon=True)
        projection.start()
    beat = threading.Thread(
        target=heartbeat_loop, name="ink-heartbeat", daemon=True,
        args=(stop, heartbeat_path or Path(journal_path).parent / HEARTBEAT_NAME, stats,
              {"journal": worker, "canonical": canonical, "projection": projection}),
        kwargs={"series": MinuteSeries(Path(journal_path).parent / SERIES_NAME),
                "qsize_fn": frames.qsize})
    beat.start()
    session = 0
    try:
        while True:
            session += 1
            stats["sessions"] = session
            ws = None
            try:
                ws = websocket.create_connection(stream_url, timeout=2)
                last_frame = time.monotonic()
                while True:
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        if time.monotonic() - last_frame > 5:
                            raise TimeoutError("Ink stream silent")
                        continue
                    if not raw:
                        raise ConnectionError("Ink stream closed")
                    last_frame = time.monotonic()
                    item = (session, raw, time.monotonic_ns(), _utc_now())
                    stats["frames"] += 1
                    stats["last_frame_utc"] = item[3]
                    if not enqueue_frame(frames, item):
                        stats["overflows"] += 1
                        overflow.set()
            except (OSError, TimeoutError, ConnectionError, websocket.WebSocketException):
                time.sleep(1)
            finally:
                if ws is not None:
                    ws.close()
    except KeyboardInterrupt:
        stop.set()
        worker.join(timeout=10)
        canonical.join(timeout=10)
        if projection is not None:
            projection.join(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream-url", default=DEFAULT_STREAM)
    state_root = Path(os.getenv("SNIPER_STATE_ROOT") or REPO)
    parser.add_argument("--journal", default=str(state_root / "data" / "ink_stream.sqlite"))
    # NOT rpc-gel/rpc-qnd (2026-09-29): both are 57073 free-SUBMISSION endpoints, and canonical reads
    # must not spend the budget Ink submission needs. thirdweb serves every read this uses (1000-block
    # getLogs cap, JSON-RPC batches); read_host_refusal enforces the rule for any --rpc-url.
    parser.add_argument("--rpc-url", default="https://57073.rpc.thirdweb.com")
    parser.add_argument("--start-block", type=int,
                        help="Declared canonical backfill start; omission keeps the reader observational")
    parser.add_argument("--main-db", default=str(state_root / "sniper_tracker.db"))
    parser.add_argument("--check-beat", metavar="PATH",
                        help="print the watchdog kill reason for this heartbeat ('' = none) and exit")
    args = parser.parse_args()
    if args.check_beat:
        try:
            beat = json.loads(Path(args.check_beat).read_text())
        except (OSError, ValueError):
            beat = None
        print(beat_verdict(beat, time.time()))
        return
    if args.stream_url != DEFAULT_STREAM:
        raise SystemExit("only the code-pinned Ink mainnet stream is supported")
    from sniper.util.envfiles import load_env_files
    load_env_files(str(state_root), names=(".env",))       # custody and pools resolve as the runner's
    why = read_host_refusal(args.rpc_url)
    if why:
        raise SystemExit("refusing Ink read host: %s" % why)
    # G2 OBSERVATION PANEL (2026-09-30): a frozen file of senders the canonical backfill reads for
    # ground truth only. Empty = none. A set but unreadable panel refuses to start, loudly.
    panel_path = os.getenv("MEMECOIN_INK_OBSERVATION_PANEL", "").strip()
    try:
        panel = load_panel(panel_path) if panel_path else None
    except (OSError, ValueError) as exc:
        raise SystemExit("refusing Ink observation panel %s: %s" % (panel_path, exc))
    run(stream_url=args.stream_url, journal_path=args.journal, rpc_url=args.rpc_url,
        main_db_path=args.main_db, start_block=args.start_block, panel=panel)


if __name__ == "__main__":
    main()
