"""Robinhood 4663 fast lane, M0: the sequencer-feed shadow (spec
docs/superpowers/specs/2026-10-01-memecoin-4663-fast-lane-design.md §2.1 and §6).

READ-ONLY, DEFAULT OFF, ITS OWN PROCESS (`scripts/memecoin_rh_feed_shadow_loop.py`, which exits
unless `MEMECOIN_RH_FAST_LANE=shadow`). It never runs inside the funded runner, so it cannot take the
GIL from the Solana slot lane or the EVM decision thread (Gamma MEDIUM). It reads the public sequencer feed,
decodes armed wallets' Relay fills from calldata and records what the fast lane would have done. It
holds no key, signs nothing, sends nothing, reserves nothing and never writes the funded ledger: its
rows go to their own SQLite file. A test pins that no signing or sending name appears here.

What one feed message is: one SEALED L2 block (contiguous `sequenceNumber`, `blockHash`). Its
`l2Msg` is an Arbitrum batch (kind 3) of signed transactions (kind 4). Every armed-wallet 4663 buy
seen since 09-14 is a Relay fill: `to` = the Relay approval proxy, selector `0x0a2b8f36`, sent by a
Relay solver. The Permit2 envelope names the wallet (`refund` = `nft`), the payment and, in the
cleanup call, the bought mint.

AUTHENTICITY (Gamma HIGH, spec §2.1). The feed has no receipts, and a sealed block includes reverted
transactions, so calldata alone is not a buy. A candidate is a SIGNAL only when its recovered sender
is a known solver, its Permit2 payer is a known Relay payer (Gamma MEDIUM; never the wallet: Relay pays
from its own funds), its permit deadline is not past the block's timestamp, and its (wallet, permit
nonce) and tx hash were not seen before. The solver seed is the senders of watcher-proven fills
(memecoin_rh_relay_seed_4663); M0 records every sender so the grader can test the allowlist.

COST. Only `to` and the selector are read for every transaction (one RLP decode, ~0.02 ms). The ABI
decode (~0.14 ms) and the sender recovery (~4.6 ms, pure-Python ECC) run only for Relay-shaped
transactions naming a watched wallet. The feed thread does no I/O besides the socket; the writer
thread does the tape read and the SQLite write.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import queue
import sqlite3
import threading
import time
from sniper.util.receipt_clock import ReceiptClock, freeze_receipt_clock

logger = logging.getLogger(__name__)

FEED_URL = "wss://feed.mainnet.chain.robinhood.com"
RELAY_SELECTOR = bytes.fromhex("0a2b8f36")
CLEANUP_SELECTOR = bytes.fromhex("9bb43718")
_CLEANUP_TYPES = ["address[]", "address[]", "uint256[]", "bytes"]
_DEDUPE_MAX = 4096

from sniper.execution.evm_relay_quote import RELAY_APPROVAL_PROXY, RELAY_ROUTER  # noqa: E402
# Imported at load, not on the first signal: a lazy import made the first recovery 44 ms (Gamma).
from eth_account import Account  # noqa: E402
from eth_utils import keccak  # noqa: E402
_RELAY_TO = bytes.fromhex(RELAY_APPROVAL_PROXY[2:])


def l2_txs(buf):
    """Signed-tx bytes in an Arbitrum L2 message: kind 4 = one tx, kind 3 = a batch of 8-byte
    big-endian length-prefixed sub-messages. Other kinds carry no user transaction."""
    if not buf:
        return
    kind, body = buf[0], buf[1:]
    if kind == 4:
        yield body
    elif kind == 3:
        i = 0
        while i + 8 <= len(body):
            n = int.from_bytes(body[i:i + 8], "big")
            i += 8
            yield from l2_txs(body[i:i + n])
            i += n


def tx_to_input(raw):
    """(lowercase `to` or None, input bytes) of a legacy, type-1 or type-2 tx; (None, b"") for any
    other type (Arbitrum system types, blobs) or bytes that do not decode."""
    import rlp
    try:
        if raw and raw[0] >= 0xC0:                      # legacy
            fields, to_i, data_i = rlp.decode(raw), 3, 5
        elif raw and raw[0] == 1:
            fields, to_i, data_i = rlp.decode(raw[1:]), 4, 6
        elif raw and raw[0] == 2:
            fields, to_i, data_i = rlp.decode(raw[1:]), 5, 7
        else:
            return None, b""
        to = bytes(fields[to_i])
        return ("0x" + to.hex() if len(to) == 20 else None), bytes(fields[data_i])
    except Exception:
        return None, b""


def _abi(types, raw, *, trailer=False):
    """Strict ABI decode: the bytes must be the canonical encoding (or it plus one 32-byte trailer,
    as `relayer_purchase_from_receipt` allows), else ValueError."""
    from eth_abi import decode, encode
    values = decode(types, raw)
    canonical = encode(types, values)
    if raw != canonical and not (trailer and len(raw) == len(canonical) + 32
                                 and raw.startswith(canonical)):
        raise ValueError("noncanonical ABI")
    return values


def decode_relay_fill(data, watched, quote_assets):
    """The Relay Permit2 fill in `data` (selector included) naming a watched wallet, or None.

    Returns wallet, mint, payment_token, paid, permit_nonce, deadline, min_out, swap_target and
    side: "buy" when the payment is a quote asset and the received token is not, else "other"."""
    from sniper.services.memecoin_evm_onchain_watch import PERMIT2_TYPES
    if bytes(data[:4]) != RELAY_SELECTOR:
        return None
    try:
        payer, permit, calls, refund, nft, _meta, signature = _abi(
            PERMIT2_TYPES, bytes(data[4:]), trailer=True)
        refund, nft = str(refund).lower(), str(nft).lower()
        if refund != nft or refund not in watched:
            return None
        permitted, nonce, deadline = permit
        if (len(permitted) != 1 or len(calls) != 3 or len(signature) not in (64, 65)
                or any(fail or value for _target, fail, value, _data in calls)):
            return None
        _approve, swap, cleanup = calls
        if (str(cleanup[0]).lower() != RELAY_ROUTER
                or bytes(cleanup[3][:4]) != CLEANUP_SELECTOR):
            return None
        tokens, recipients, minima, _cmeta = _abi(_CLEANUP_TYPES, bytes(cleanup[3][4:]))
        if len(tokens) != 1 or tuple(str(r).lower() for r in recipients) != (refund,):
            return None
        payment, paid = str(permitted[0][0]).lower(), int(permitted[0][1])
        mint = str(tokens[0]).lower()
        if paid <= 0 or mint == payment:
            return None
    except Exception:
        return None
    side = "buy" if payment in quote_assets and mint not in quote_assets else "other"
    # order_id: Relay's order commitment, the calldata's last 32 bytes (_verify_relay's binding);
    # option C matches it to the user's Solana deposit (memecoin_rh_deposit_proof).
    return {"wallet": refund, "mint": mint, "payment_token": payment, "paid": paid,
            "order_id": "0x" + bytes(data[-32:]).hex(),
            "permit_nonce": int(nonce), "deadline": int(deadline),
            "min_out": int(minima[0]) if minima else None,
            "swap_target": str(swap[0]).lower(), "payer": str(payer).lower(), "side": side}


def recover_sender(raw):
    """The lowercase sender of signed tx bytes, or None."""
    try:
        return str(Account.recover_transaction(bytes(raw))).lower()
    except Exception:
        return None


class FeedShadow:
    """Per-frame decode and verdict. `handle_frame` is pure apart from its dedupe memory and
    counters; `sink` receives each record (the writer queue in production)."""

    def __init__(self, *, watched_fn, solvers, quote_assets, sink, payers=()):
        self.watched_fn = watched_fn
        self.solvers = frozenset(s.lower() for s in solvers)
        self.payers = frozenset(p.lower() for p in payers)
        self.quote_assets = frozenset(q.lower() for q in quote_assets)
        self.sink = sink
        self._seen = collections.OrderedDict()
        self.counters = collections.Counter()
        self.last_seq = None
        # Startup-only boot/correction reads. The socket handler captures before any decoding.
        self.receipt_producer = ReceiptClock('rh-sequencer:4663')

    def _first_time(self, key):
        if key in self._seen:
            return False
        self._seen[key] = True
        if len(self._seen) > _DEDUPE_MAX:
            self._seen.popitem(last=False)
        return True

    def handle_frame(self, text, *, arrival_utc, arrival_mono, receipt_clock=None):
        records = []
        try:
            messages = json.loads(text).get("messages") or []
        except (ValueError, AttributeError):
            self.counters["bad_frames"] += 1
            return records
        watched = self.watched_fn() or {}
        for m in messages:
            seq = m.get("sequenceNumber")
            if isinstance(seq, int):
                if self.last_seq is not None and seq != self.last_seq + 1:
                    self.counters["seq_gaps"] += 1
                self.last_seq = seq
            msg = (m.get("message") or {}).get("message") or {}
            header_ts = (msg.get("header") or {}).get("timestamp")
            try:
                l2 = base64.b64decode(msg.get("l2Msg") or "")
            except ValueError:
                self.counters["bad_frames"] += 1
                continue
            self.counters["blocks"] += 1
            for raw in l2_txs(l2):
                self.counters["txs"] += 1
                to, data = tx_to_input(raw)
                if to != RELAY_APPROVAL_PROXY or bytes(data[:4]) != RELAY_SELECTOR:
                    continue
                self.counters["relay_txs"] += 1
                t0 = time.perf_counter()
                fill = decode_relay_fill(data, watched, self.quote_assets)
                decode_ms = (time.perf_counter() - t0) * 1000.0
                if fill is None:
                    continue
                t1 = time.perf_counter()
                sender = recover_sender(raw)
                recover_ms = (time.perf_counter() - t1) * 1000.0
                tx_hash = "0x" + keccak(bytes(raw)).hex()
                anchor = (self.receipt_producer.remember(('source_transaction', tx_hash), receipt_clock)
                          if receipt_clock is not None else None)
                deadline_ok = isinstance(header_ts, int) and fill["deadline"] >= header_ts
                fresh = (self._first_time(("tx", tx_hash))
                         & self._first_time(("permit", fill["wallet"], fill["permit_nonce"])))
                reason = ("not_a_buy" if fill["side"] != "buy" else
                          "unknown_sender" if sender not in self.solvers else
                          "unknown_payer" if fill["payer"] not in self.payers else
                          "permit_expired" if not deadline_ok else
                          "duplicate" if not fresh else "signal")
                uid, handle = watched.get(fill["wallet"], (None, None))
                rec = {**fill, "tx_hash": tx_hash, "seq": seq, "header_ts": header_ts,
                       "arrival_utc": arrival_utc, "arrival_mono": arrival_mono,
                       "sender": sender, "solver_known": sender in self.solvers,
                       "payer_known": fill["payer"] in self.payers,
                       "deadline_ok": deadline_ok, "duplicate": not fresh,
                       "signal": reason == "signal", "reason": reason,
                       "decode_ms": decode_ms, "recover_ms": recover_ms,
                       "source_user_id": uid, "handle": handle}
                rec['receipt_clock'] = freeze_receipt_clock(anchor)
                self.counters["candidates"] += 1
                self.counters["signals"] += rec["signal"]
                records.append(rec)
                self.sink(rec)
        return records


_SCHEMA = """
CREATE TABLE IF NOT EXISTS rh_feed_signals (
    id INTEGER PRIMARY KEY, tx_hash TEXT NOT NULL, seq INTEGER, header_ts INTEGER,
    arrival_utc TEXT, wallet TEXT, source_user_id TEXT, handle TEXT, mint TEXT,
    payment_token TEXT, paid TEXT, permit_nonce TEXT, deadline INTEGER, min_out TEXT,
    swap_target TEXT, payer TEXT, side TEXT, sender TEXT, solver_known INTEGER, payer_known INTEGER,
    deadline_ok INTEGER, duplicate INTEGER, signal INTEGER, reason TEXT,
    decode_ms REAL, recover_ms REAL, pre_pool TEXT, pre_block INTEGER, pre_observed_at TEXT,
    pre_tick INTEGER, pre_liquidity TEXT, pre_sqrt_price_x96 TEXT, written_at TEXT,
    post_checked INTEGER NOT NULL DEFAULT 0, post_pool TEXT, post_block INTEGER, post_tick INTEGER,
    post_liquidity TEXT, post_sqrt_price_x96 TEXT);
CREATE INDEX IF NOT EXISTS idx_rh_feed_signals_tx ON rh_feed_signals(tx_hash);
CREATE INDEX IF NOT EXISTS idx_rh_feed_signals_arrival ON rh_feed_signals(arrival_utc,id);
CREATE TABLE IF NOT EXISTS rh_feed_heartbeat (
    at TEXT NOT NULL, frames INTEGER, blocks INTEGER, txs INTEGER, relay_txs INTEGER,
    candidates INTEGER, signals INTEGER, seq_gaps INTEGER, bad_frames INTEGER,
    reconnects INTEGER, queue_drops INTEGER, cpu_ms REAL, frame_ms_max REAL, frame_ms_sum REAL,
    frame_errors INTEGER);
"""


def _tape_prestate(tape, mint, before_iso, *, source_block=None, source_tx=None):
    """The would-be pre-trade state (spec §2.4): the pool's last swap in a block BEFORE the
    source's block, never the source tx itself (Gamma MEDIUM: the tape sees a tx ~25 ms before the
    feed, so "last swap before arrival" was the source's own swap and pre equalled post).

    The pool is the one the source swapped in when the tape already has its row, else the mint's
    busiest pool in the 10 minutes before `before_iso`. `source_block` is the frame's
    `sequenceNumber` (block number, offset 0). None when unknown."""
    pool = None
    if source_tx:
        hit = tape.execute("SELECT pool FROM pool_ticks WHERE chain_id='4663' AND tx_sig=? "
                           "AND kind='v3_swap' LIMIT 1", (source_tx,)).fetchone()
        pool = hit[0] if hit else None
    if pool is None:
        pools = [r[0] for r in tape.execute(
            "SELECT pool FROM onchain_pool_protocols WHERE chain_id='4663' AND lower(mint)=?",
            (mint,))]
        from datetime import datetime, timedelta
        # The bound in the tape's own ISO form: SQLite's datetime() writes a space, not a 'T', and
        # a string comparison against it would admit every row of the day.
        since = (datetime.fromisoformat(before_iso) - timedelta(minutes=10)).isoformat()
        best = None
        for candidate in pools:
            row = tape.execute(
                "SELECT count(*) FROM pool_ticks WHERE chain_id='4663' AND pool=? "
                "AND kind='v3_swap' AND observed_at < ? AND observed_at >= ?",
                (candidate, before_iso, since)).fetchone()
            if row and row[0] and (best is None or row[0] > best[1]):
                best = (candidate, row[0])
        if best is None:
            return None
        pool = best[0]
    if isinstance(source_block, int):
        return tape.execute(
            "SELECT pool, block_no, observed_at, tick, liquidity, sqrt_price_x96 FROM pool_ticks "
            "WHERE chain_id='4663' AND pool=? AND kind='v3_swap' AND block_no < ? "
            "AND tx_sig IS NOT ? ORDER BY block_no DESC, log_index DESC LIMIT 1",
            (pool, source_block, source_tx)).fetchone()
    return tape.execute(
        "SELECT pool, block_no, observed_at, tick, liquidity, sqrt_price_x96 FROM pool_ticks "
        "WHERE chain_id='4663' AND pool=? AND kind='v3_swap' AND observed_at < ? "
        "AND tx_sig IS NOT ? ORDER BY observed_at DESC LIMIT 1",
        (pool, before_iso, source_tx)).fetchone()


def write_records(records, *, db_path, tape_path):
    """Writer-thread work: enrich each record with the tape pre-state and store it."""
    from sniper.util.clock import real_utc_now_iso
    out = sqlite3.connect(db_path, timeout=5)
    out.executescript(_SCHEMA)
    columns = {row[1] for row in out.execute('PRAGMA table_info(rh_feed_signals)')}
    if 'receipt_clock_json' not in columns:
        out.execute('ALTER TABLE rh_feed_signals ADD COLUMN receipt_clock_json TEXT')
    tape = None
    try:
        tape = sqlite3.connect("file:%s?mode=ro" % tape_path, uri=True, timeout=1)
    except sqlite3.Error:
        tape = None
    try:
        for r in records:
            pre = None
            if tape is not None and r.get("arrival_utc"):
                try:
                    pre = _tape_prestate(tape, r["mint"], r["arrival_utc"],
                                         source_block=r.get("seq"), source_tx=r.get("tx_hash"))
                except sqlite3.Error:
                    pre = None
            pre = pre or (None,) * 6
            out.execute(
                "INSERT INTO rh_feed_signals (tx_hash, seq, header_ts, arrival_utc, wallet, "
                "source_user_id, handle, mint, payment_token, paid, permit_nonce, deadline, "
                "min_out, swap_target, payer, side, sender, solver_known, payer_known, deadline_ok, "
                "duplicate, "
                "signal, reason, decode_ms, recover_ms, pre_pool, pre_block, pre_observed_at, "
                "pre_tick, pre_liquidity, pre_sqrt_price_x96, written_at, receipt_clock_json) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["tx_hash"], r["seq"], r["header_ts"], r["arrival_utc"], r["wallet"],
                 r["source_user_id"], r["handle"], r["mint"], r["payment_token"], str(r["paid"]),
                 str(r["permit_nonce"]), r["deadline"],
                 None if r["min_out"] is None else str(r["min_out"]), r["swap_target"], r["payer"],
                 r["side"], r["sender"], int(r["solver_known"]), int(r["payer_known"]),
                 int(r["deadline_ok"]),
                 int(r["duplicate"]), int(r["signal"]), r["reason"], r["decode_ms"],
                 r["recover_ms"], *pre, real_utc_now_iso(),
                 json.dumps(freeze_receipt_clock(r.get('receipt_clock')))))
        out.commit()
    finally:
        out.close()
        if tape is not None:
            tape.close()


def enrich_post(*, db_path, tape_path, now_epoch, settle_s=3.0, give_up_s=120.0):
    """The source tx's own post-swap state from the tape, written onto its signal row: the exact
    reference the estimate is graded against (spec §2.4/§2.8). The tape keeps about an hour, so this
    runs live. A row is tried once it is `settle_s` old and given up (post_checked=2) at `give_up_s`.
    Returns the number of rows filled."""
    from datetime import datetime
    out = sqlite3.connect(db_path, timeout=5)
    filled = 0
    try:
        out.executescript(_SCHEMA)
        rows = out.execute("SELECT id, tx_hash, written_at FROM rh_feed_signals "
                           "WHERE post_checked=0").fetchall()
        if not rows:
            return 0
        tape = sqlite3.connect("file:%s?mode=ro" % tape_path, uri=True, timeout=1)
        try:
            for row_id, tx_hash, written_at in rows:
                try:
                    age = now_epoch - datetime.fromisoformat(written_at).timestamp()
                except (TypeError, ValueError):
                    age = give_up_s
                if age < settle_s:
                    continue
                hit = tape.execute(
                    "SELECT pool, block_no, tick, liquidity, sqrt_price_x96 FROM pool_ticks "
                    "WHERE chain_id='4663' AND tx_sig=? AND kind='v3_swap' "
                    "ORDER BY log_index DESC LIMIT 1", (tx_hash,)).fetchone()
                if hit:
                    out.execute("UPDATE rh_feed_signals SET post_checked=1, post_pool=?, "
                                "post_block=?, post_tick=?, post_liquidity=?, post_sqrt_price_x96=? "
                                "WHERE id=?", (*hit, row_id))
                    filled += 1
                elif age >= give_up_s:
                    out.execute("UPDATE rh_feed_signals SET post_checked=2 WHERE id=?", (row_id,))
        finally:
            tape.close()
        out.commit()
    finally:
        out.close()
    return filled


def write_heartbeat(db_path, row):
    out = sqlite3.connect(db_path, timeout=5)
    try:
        out.executescript(_SCHEMA)
        out.execute("INSERT INTO rh_feed_heartbeat VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        out.commit()
    finally:
        out.close()


def quote_assets_4663():
    """The 4663 quote assets a Relay buy pays with: the chain's WETH and its USD stables (USDG).
    A payment in anything else reads side "other"."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    from sniper.settings import memecoin as cfg
    return frozenset({CHAIN_QUOTE["4663"]["token"].lower(),
                      *(t.lower() for t in cfg.MEMECOIN_ONCHAIN_M5_USD_STABLES.get("4663", ()))})


def start_shadow(*, ledger_path, log=print):
    """Start the feed thread and the writer thread in THIS process (the shadow's own process).
    Returns the FeedShadow. No signing path."""
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.services.axiom_address_verify import evm_watched_owners
    from sniper.util.clock import real_utc_now, real_utc_now_iso
    from sniper.util.state_paths import state_path

    db_path = str(state_path("data", "memecoin_rh_feed_shadow.db"))
    tape_path = str(state_path("data", "memecoin_onchain_volume.db"))
    quotes = quote_assets_4663()
    from sniper.services.memecoin_rh_relay_seed_4663 import PAYERS as payers, SOLVERS as solvers
    watched = {"value": {}, "by_chain": {}, "at": 0.0}
    lock = threading.Lock()

    def refresh_watched():
        # Off the feed thread: the writer refreshes the roster every 10 s on a read-only handle; the
        # Relay poller's per-chain rosters (4663, and 56 with its pre-buy on) on the same handle.
        from sniper.services.memecoin_rh_relay_prebuy import relay_rosters
        conn = get_readonly_connection(str(ledger_path))
        try:
            conn.execute("PRAGMA busy_timeout=250")
            value = evm_watched_owners(conn, "4663", include_operator=True)
            by_chain = relay_rosters(lambda chain: value if chain == "4663" else
                                     evm_watched_owners(conn, chain, include_operator=True))
        finally:
            conn.close()
        with lock:
            watched["value"], watched["by_chain"], watched["at"] = value, by_chain, time.monotonic()

    def watched_fn():
        with lock:
            return watched["value"]

    def relay_watched_fn():
        with lock:
            return watched["by_chain"]

    work = queue.Queue(maxsize=1000)
    decide_offer, decide_threads = None, []
    from sniper.settings import memecoin as cfg
    if cfg.memecoin_rh_fast_decide_shadow():
        # M1: signals also go to the sign-less decide thread; the feed thread never waits on it.
        from sniper.services.memecoin_rh_fast_decide import start_decide
        # The Relay poller (P2/P3) reads the roster and the newest sealed block (the chain head).
        decide_offer, decide_threads = start_decide(db_path=db_path, tape_path=tape_path, log=log,
                                                    ledger_path=ledger_path, watched_fn=relay_watched_fn,
                                                    head_fn=lambda: shadow.last_seq)
    shadow = FeedShadow(watched_fn=watched_fn, solvers=solvers, payers=payers, quote_assets=quotes,
                        sink=make_sink(lambda rec: _offer(work, rec, shadow), decide_offer))
    stats = {"frames": 0, "reconnects": 0, "cpu_ms": 0.0, "frame_ms_max": 0.0, "frame_ms_sum": 0.0}

    def writer():
        last_beat = last_post = time.monotonic()
        while True:
            try:
                if time.monotonic() - watched["at"] > 10:
                    refresh_watched()
            except Exception:
                logger.exception("rh feed shadow: roster refresh failed (keeping the last set)")
            batch = []
            try:
                batch.append(work.get(timeout=1.0))
                while len(batch) < 100:
                    batch.append(work.get_nowait())
            except queue.Empty:
                pass
            if batch:
                try:
                    write_records(batch, db_path=db_path, tape_path=tape_path)
                except Exception:
                    shadow.counters['queue_drops'] += len(batch)
                    logger.exception("rh feed shadow: write failed (%d records lost)", len(batch))
                pipeline = getattr(getattr(decide_offer, "relay", None), "pipeline", None)
                if pipeline is not None:                    # the Relay pre-buy's source outcome
                    try:
                        pipeline.note_fills(batch)
                    except Exception:
                        logger.exception("rh feed shadow: relay fill note failed")
            if time.monotonic() - last_post >= 5:
                last_post = time.monotonic()
                try:
                    # The corrected clock, as written_at is: never diff host time against it.
                    enrich_post(db_path=db_path, tape_path=tape_path, now_epoch=real_utc_now())
                except Exception:
                    logger.exception("rh feed shadow: post-state enrichment failed")
            if time.monotonic() - last_beat >= 60:
                last_beat = time.monotonic()
                c = shadow.counters
                try:
                    write_heartbeat(db_path, (
                        real_utc_now_iso(), stats["frames"], c["blocks"], c["txs"], c["relay_txs"],
                        c["candidates"], c["signals"], c["seq_gaps"], c["bad_frames"],
                        stats["reconnects"], c["queue_drops"], round(stats["cpu_ms"], 3),
                        round(stats["frame_ms_max"], 3), round(stats["frame_ms_sum"], 3),
                        c["frame_errors"]))
                except Exception:
                    logger.exception("rh feed shadow: heartbeat write failed")
                stats["frame_ms_max"] = 0.0

    async def read_feed():
        import websockets
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(FEED_URL, max_size=2 ** 24, ping_interval=20) as ws:
                    backoff = 1.0
                    async for text in ws:
                        process_frame(shadow, stats, text, now_utc=real_utc_now_iso)
            except Exception as exc:
                stats["reconnects"] += 1
                log("[rh-feed-shadow] feed disconnected (%s: %s); reconnecting in %.0fs"
                    % (type(exc).__name__, exc, backoff))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    shadow.threads = [
        threading.Thread(target=writer, name="rh-feed-shadow-writer", daemon=True),
        threading.Thread(target=lambda: asyncio.run(read_feed()), name="rh-feed-shadow",
                         daemon=True)]
    for thread in shadow.threads:
        thread.start()
    # M1's threads stay OUT of the liveness set M0's loop exits on (Gamma LOW: M0 is under grade);
    # their death shows as rh_fast_heartbeat.threads_alive < 3, or no heartbeat at all.
    shadow.decide_threads = decide_threads
    deposit_stream = getattr(decide_offer, 'deposit_stream_status', None)
    if isinstance(deposit_stream, dict) and deposit_stream.get('provider') == 'shyft':
        shadow.deposit_stream_status = deposit_stream
    shadow.stats = stats
    log("[rh-feed-shadow] started: read-only, no signing; rows -> %s" % db_path)
    return shadow


def process_frame(shadow, stats, text, *, now_utc):
    """One frame on the feed thread: stamp, handle, account CPU and wall. A frame that raises is
    counted in `frame_errors` and never reaches the connection's except, so it is not a reconnect
    with backoff (Gamma LOW 3)."""
    arrival_mono = time.perf_counter()
    receipt_clock = shadow.receipt_producer.capture()
    arrival_utc = now_utc()
    cpu0 = time.thread_time()
    try:
        shadow.handle_frame(text, arrival_utc=arrival_utc, arrival_mono=arrival_mono,
                            receipt_clock=receipt_clock)
    except Exception:
        shadow.counters["frame_errors"] += 1
        logger.exception("rh feed shadow: frame handling failed")
    cpu_ms = (time.thread_time() - cpu0) * 1000.0
    wall_ms = (time.perf_counter() - arrival_mono) * 1000.0
    stats["frames"] += 1
    stats["cpu_ms"] += cpu_ms
    stats["frame_ms_sum"] += wall_ms
    stats["frame_ms_max"] = max(stats["frame_ms_max"], wall_ms)


def make_sink(offer_work, decide_offer):
    """The feed thread's sink: every record to the writer; signals also to M1's decide thread."""
    def sink(rec):
        offer_work(rec)
        if decide_offer is not None and rec["signal"]:
            decide_offer(rec)
    return sink


def _offer(work, rec, shadow):
    try:
        work.put_nowait(rec)
    except queue.Full:
        shadow.counters["queue_drops"] += 1
