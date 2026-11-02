"""Helius Preconfirmations as the slot lane's third signal feed (operator 2026-10-17 "turn preconf on";
Alpha GO on the map). Its frames reach the rabbit owner-frame path: the same candidate decode
(memecoin_shyft_prepare.preparation_candidate), ONE shared RabbitBuyer (one fire per signature across
rabbit and preconf), the same pre-execution proof and the same source_not_landed exit.

WHAT A FRAME CARRIES (wss://beta.helius-rpc.com, preconfSubscribe): version u8 | slot u64 LE |
transaction index u64 LE | status u8 | the transaction's wire bytes, signatures included. No logs, no
meta, no balances, and NO SOURCE OR SUBSCRIPTION FIELD.

WHAT IS ASKED FOR (Alpha GO 2026-10-17): signerInclude = the tracked owners (accountInclude matched any
mention: 21 of the day's first 24 billed slots carried no owner frame), one socket PER SOURCE in
MEMECOIN_SLOT_LANE_PRECONF_SOURCES (helius, bam), because a frame does not say which source sent it.
Each source is its own feed (preconf:helius, preconf:bam): its own frame line, arrival stamps and
lead pairs. The same signature from both sources fires once (the one shared buyer) and bills twice
(two subscriptions).

WHAT IT PROVES, and what is refused (frame log only, never a buy):
- the owner is a REQUIRED signer and its OWN ed25519 signature verifies here, over the raw message
  bytes (`ed25519_local`): v1 (SIMD-0385) by this module's parser, legacy/v0 by solders;
- refused: status Failed or Unknown; bytes that do not parse exactly (fail closed: any count, index,
  mask or length that does not add up); an owner signature that does not verify;
- v0 lookup addresses use an owner/layout/context/activation-validated bounded cache. Cold misses
  enqueue one read on the buyer's existing worker budget; expired, reconnected and processed sources
  never resume. Only loaded address vectors are populated, with the original frame's receipt clock;
- never that it lands: source_not_landed is the backstop, as for rabbit.

CREDITS (Helius: per subscription, max(300, 10 x messages) for each slot that delivers at least one
message): metered here per UTC day, one meter and one cap for every source's subscription, each
slot billed per (source, slot); at MEMECOIN_SLOT_LANE_PRECONF_DAILY_CREDITS the subscription
closes until 00:00Z, paged once. The day's count is persisted (data/slot_lane_preconf_credits.json), so a
restart inside the day keeps it (see CreditMeter). The key is the research Helius key (the one paid account), so the
cap also protects the relay-proof verifies and the exit tape that share it.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import struct
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

FEED = "preconf:helius"
SOURCES = ("helius", "bam")                      # includeSource values (preconfSubscribe API reference)


def feed_for(source):
    """The feed name of one source's subscription: preconf:helius, preconf:bam."""
    return "preconf:%s" % source
FRAME_VERSION = 1
HEADER_LEN = 18
STATUS_FAILED, STATUS_SUCCESS, STATUS_UNKNOWN = 0, 1, 2
V1_PREFIX = 0x81
V1_MAX_BYTES = 4096
V1_MAX_ADDRESSES = 64
V1_MAX_INSTRUCTIONS = 64
V1_MASK_BITS = 0b11111          # priority fee (bits 0+1), CU limit, loaded data, heap
CREDITS_PER_MESSAGE = 10
CREDITS_MIN_PER_SLOT = 300
CREDIT_FILE = ("data", "slot_lane_preconf_credits.json")   # the day's spend, across restarts


class Malformed(ValueError):
    """Bytes that do not parse exactly. Never a buy."""


V1 = collections.namedtuple("V1", "num_required num_readonly_signed num_readonly_unsigned blockhash "
                                  "addresses config instructions message_end signatures")


def parse_frame(data):
    """(slot, transaction_index, status, tx_bytes) of one binary preconf message, or Malformed."""
    data = bytes(data)
    if len(data) <= HEADER_LEN or data[0] != FRAME_VERSION:
        raise Malformed("frame_header")
    slot, index = struct.unpack_from("<QQ", data, 1)
    status = data[17]
    if status not in (STATUS_FAILED, STATUS_SUCCESS, STATUS_UNKNOWN) or slot <= 0:
        raise Malformed("frame_status")
    return slot, index, status, data[HEADER_LEN:]


def parse_v1(raw):
    """A SIMD-0385 transaction, parsed exactly (every count fixed-width, no trailing bytes), or
    Malformed. ConfigValues follow the mask in ascending bit order (the SIMD does not state it; it
    only feeds the recorded config, never a decision)."""
    raw = bytes(raw)
    if not 42 <= len(raw) <= V1_MAX_BYTES or raw[0] != V1_PREFIX:
        raise Malformed("v1_prefix")
    n_req, ro_signed, ro_unsigned = raw[1], raw[2], raw[3]
    mask = struct.unpack_from("<I", raw, 4)[0]
    blockhash = raw[8:40]
    n_ix, n_addr = raw[40], raw[41]
    if (n_req < 1 or ro_signed >= n_req or n_addr > V1_MAX_ADDRESSES or n_ix > V1_MAX_INSTRUCTIONS
            or n_addr < n_req + ro_unsigned or mask & ~V1_MASK_BITS or bool(mask & 1) != bool(mask & 2)):
        raise Malformed("v1_header")
    off = 42

    def take(n):
        nonlocal off
        if n < 0 or off + n > len(raw):
            raise Malformed("v1_short")
        chunk = raw[off:off + n]
        off += n
        return chunk
    addresses = tuple(take(32) for _ in range(n_addr))
    if len(set(addresses)) != n_addr:
        raise Malformed("v1_duplicate_address")
    config = {"priority_fee_lamports": None, "compute_unit_limit": None,
              "loaded_accounts_data_size_limit": None, "heap_size": None}
    if mask & 1:
        config["priority_fee_lamports"] = struct.unpack("<Q", take(8))[0]
    for bit, name in ((2, "compute_unit_limit"), (3, "loaded_accounts_data_size_limit"), (4, "heap_size")):
        if mask & (1 << bit):
            config[name] = struct.unpack("<I", take(4))[0]
    heap = config["heap_size"]
    if heap is not None and (heap % 1024 or not 32 * 1024 <= heap <= 256 * 1024):
        raise Malformed("v1_heap")
    headers = [struct.unpack("<BBH", take(4)) for _ in range(n_ix)]
    instructions = []
    for program, n_accounts, n_data in headers:
        accounts = take(n_accounts)
        if program >= n_addr or any(index >= n_addr for index in accounts):
            raise Malformed("v1_index")
        instructions.append((program, bytes(accounts), bytes(take(n_data))))
    message_end = off
    signatures = tuple(take(64) for _ in range(n_req))
    if off != len(raw):
        raise Malformed("v1_trailing")
    return V1(n_req, ro_signed, ro_unsigned, blockhash, addresses, config, tuple(instructions),
              message_end, signatures)


def _shortvec(raw, off):
    value = shift = 0
    for i in range(3):
        if off + i >= len(raw):
            raise Malformed("shortvec")
        byte = raw[off + i]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, off + i + 1
        shift += 7
    raise Malformed("shortvec")


def parse_legacy_v0(raw):
    """(VersionedTransaction, message_bytes) for a legacy or v0 wire transaction, or Malformed. The
    message bytes are the signed bytes exactly as received, after the signature list."""
    from solders.transaction import VersionedTransaction
    raw = bytes(raw)
    count, off = _shortvec(raw, 0)
    if count < 1 or off + 64 * count >= len(raw):
        raise Malformed("signatures")
    try:
        tx = VersionedTransaction.from_bytes(raw)
    except Exception as exc:
        raise Malformed("solders:%s" % type(exc).__name__) from exc
    if len(tx.signatures) != count or bytes(tx) != raw:
        raise Malformed("not_canonical")
    return tx, raw[off + 64 * count:]


def frame_update(slot, raw):
    """(update, verify) for one preconf transaction: a yellowstone-shaped SubscribeUpdate with no meta
    (what preparation_candidate and the rabbit path read) and verify(owner) -> 'ed25519_local' or None.
    Raises Malformed."""
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    raw = bytes(raw)
    update = pb.SubscribeUpdate()
    update.transaction.slot = int(slot)
    info = update.transaction.transaction
    info.is_vote = False
    msg = info.transaction.message
    if raw[:1] == bytes([V1_PREFIX]):
        tx = parse_v1(raw)
        signatures, keys, n_req = tx.signatures, tx.addresses, tx.num_required
        msg.header.num_required_signatures = n_req
        msg.header.num_readonly_signed_accounts = tx.num_readonly_signed
        msg.header.num_readonly_unsigned_accounts = tx.num_readonly_unsigned
        msg.recent_blockhash = tx.blockhash
        msg.versioned = True
        for name, value in (("priority_fee", tx.config["priority_fee_lamports"]),
                            ("compute_unit_limit", tx.config["compute_unit_limit"]),
                            ("loaded_accounts_data_size_limit", tx.config["loaded_accounts_data_size_limit"]),
                            ("heap_size", tx.config["heap_size"])):
            if value is not None:
                setattr(msg.config, name, value)
        msg.config.SetInParent()
        for program, accounts, data in tx.instructions:
            ix = msg.instructions.add()
            ix.program_id_index, ix.accounts, ix.data = program, accounts, data
        message_bytes = raw[:tx.message_end]
    else:
        tx, message_bytes = parse_legacy_v0(raw)
        m = tx.message
        signatures = tuple(bytes(sig) for sig in tx.signatures)
        keys = tuple(bytes(key) for key in m.account_keys)
        n_req = m.header.num_required_signatures
        msg.header.num_required_signatures = n_req
        msg.header.num_readonly_signed_accounts = m.header.num_readonly_signed_accounts
        msg.header.num_readonly_unsigned_accounts = m.header.num_readonly_unsigned_accounts
        msg.recent_blockhash = bytes(m.recent_blockhash)
        lookups = list(getattr(m, "address_table_lookups", None) or ())
        msg.versioned = raw[_shortvec(raw, 0)[1] + 64 * len(signatures)] & 0x80 != 0
        for ix in m.instructions:
            out = msg.instructions.add()
            out.program_id_index, out.accounts, out.data = ix.program_id_index, bytes(ix.accounts), bytes(ix.data)
        for table in lookups:
            out = msg.address_table_lookups.add()
            out.account_key = bytes(table.account_key)
            out.writable_indexes = bytes(table.writable_indexes)
            out.readonly_indexes = bytes(table.readonly_indexes)
    msg.account_keys.extend(keys)
    info.transaction.signatures.extend(signatures)
    info.signature = signatures[0]

    def verify(owner):
        """'ed25519_local' when `owner` is a required signer and ITS signature verifies over the
        message bytes as received, else None."""
        try:
            target = bytes(Pubkey.from_string(owner))
            index = next(i for i, key in enumerate(keys[:n_req]) if key == target)
            ok = Signature.from_bytes(signatures[index]).verify(Pubkey.from_bytes(target), message_bytes)
        except Exception:
            return None
        return "ed25519_local" if ok else None
    return update, verify


class LookupTables:
    """Event-loop-owned structural ALT cache; cold reads share the buyer's bounded worker budget."""

    def __init__(self, buyer):
        self.buyer = buyer
        self.tables, self.receipts = collections.OrderedDict(), collections.OrderedDict()
        self.pending, self.generations, self.feed_status = set(), {}, {}
        self.stats = buyer.stats.setdefault('preconf_alt', dict(
            hits=0, reads=0, refused=0, errors=0, reasons={}, last_reason=None))

    def _note(self, reason, *, error=False):
        self.stats['last_reason'] = reason
        reasons = self.stats['reasons']
        reasons[reason] = reasons.get(reason, 0) + 1
        self.stats['errors'] += int(error)
        return False

    def generation(self, feed, generation, live_status=None):
        if live_status is not None:
            self.feed_status[feed] = live_status
        previous = self.generations.setdefault(feed, generation)
        if previous != generation:
            self.tables.clear()
            self.generations[feed] = generation

    def receipt(self, signature, owner, mono):
        key = (signature, owner)
        if key not in self.receipts:
            self.receipts[key] = mono
            while len(self.receipts) > 4096:
                self.receipts.popitem(last=False)
        self.receipts[key] = min(mono, self.receipts[key])
        return self.receipts[key]

    @staticmethod
    def _lookups(update):
        rows = tuple(update.transaction.transaction.transaction.message.address_table_lookups)
        keys = tuple(bytes(row.account_key) for row in rows)
        if not 0 < len(rows) <= 8 or len(set(keys)) != len(keys) or any(len(key) != 32 for key in keys):
            raise ValueError('lookup_shape')
        for row in rows:
            indices = bytes(row.writable_indexes) + bytes(row.readonly_indexes)
            if len(indices) != len(set(indices)):
                raise ValueError('lookup_indices')
        return rows

    def resolve(self, update):
        """Populate only loaded address vectors. Neither success nor payment metadata is invented."""
        try:
            rows = self._lookups(update)
            slot, now = update.transaction.slot, self.buyer.clock.monotonic()
            writable, readonly = [], []
            for row in rows:
                stamp, extended, keys = self.tables[bytes(row.account_key)]
                if not 0 <= now - stamp < 60. or extended >= slot:
                    return False
                writable.extend(keys[i] for i in row.writable_indexes)
                readonly.extend(keys[i] for i in row.readonly_indexes)
            msg = update.transaction.transaction.transaction.message
            every = tuple(msg.account_keys) + tuple(writable) + tuple(readonly)
            if len(every) > 256 or len(every) != len(set(every)):
                return False
            meta = update.transaction.transaction.meta
            meta.loaded_writable_addresses[:] = writable
            meta.loaded_readonly_addresses[:] = readonly
            self.stats['hits'] += 1
            return True
        except (KeyError, IndexError, TypeError, ValueError):
            return False

    def _alive(self, signature, owner, mono, feed, generation):
        mono = min(mono, self.receipts.get((signature, owner), mono))
        if not 0 <= self.buyer.clock.monotonic() - mono < .5:
            return self._note('expired')
        if (signature, owner) in self.buyer.seen:
            return self._note('processed_first')
        if self.generations.get(feed) != generation:
            return self._note('reconnected')
        live = self.feed_status.get(feed, {})
        if live.get('connection_generation', generation) != generation or live.get('connected') is False:
            self.tables.clear()
            return self._note('reconnected')
        if not self.buyer.backstop.allowed(feed):
            return self._note('disabled')
        return True

    def offer(self, update, *, owner, mono, feed, on_ready, generation=0):
        import base58
        self.generation(feed, generation)
        signature = base58.b58encode(update.transaction.transaction.signature).decode()
        mono = self.receipt(signature, owner, mono)
        key = (signature, owner)
        if (key in self.pending or not self._alive(signature, owner, mono, feed, generation)
                or len(self.buyer._tasks) >= self.buyer.MAX_IN_FLIGHT):
            self.stats['refused'] += 1
            return False
        try:
            rows = self._lookups(update)
        except ValueError:
            self.stats['refused'] += 1
            return False
        self.pending.add(key)
        task = asyncio.get_running_loop().create_task(self._load(
            update, rows, signature, owner, mono, feed, on_ready, generation))
        self.buyer._tasks.add(task)
        task.add_done_callback(self.buyer._tasks.discard)
        return True

    async def _load(self, update, rows, signature, owner, mono, feed, on_ready, generation):
        import base64
        from solders.address_lookup_table_account import AddressLookupTable
        from solders.pubkey import Pubkey
        try:
            if not self._alive(signature, owner, mono, feed, generation):
                return
            self.stats['reads'] += 1
            result = await asyncio.to_thread(self.buyer.read, 'getMultipleAccounts', [
                [str(Pubkey.from_bytes(bytes(row.account_key))) for row in rows],
                {'encoding': 'base64', 'commitment': 'processed', 'minContextSlot': update.transaction.slot}])
            if not self._alive(signature, owner, mono, feed, generation):
                return
            if isinstance(result, dict) and 'error' in result:
                raise RuntimeError('lookup_rejected')
            context = result['context']['slot']
            if type(context) is not int or context < update.transaction.slot or len(result['value']) != len(rows):
                raise ValueError('lookup_context')
            verified = []
            for row, account in zip(rows, result['value']):
                if (account['owner'] != 'AddressLookupTab1e1111111111111111111111111'
                        or account.get('executable') is not False or account['data'][1] != 'base64'):
                    raise ValueError('lookup_owner')
                raw = base64.b64decode(account['data'][0], validate=True)
                if not 56 <= len(raw) <= 56 + 256 * 32 or (len(raw) - 56) % 32:
                    raise ValueError('lookup_layout')
                table = AddressLookupTable.deserialize(raw)
                if (table.meta.deactivation_slot != 2**64 - 1
                        or table.meta.last_extended_slot >= update.transaction.slot
                        or table.meta.last_extended_slot > context):
                    raise ValueError('lookup_activation')
                if any(i >= len(table.addresses) for i in bytes(row.writable_indexes) + bytes(row.readonly_indexes)):
                    raise ValueError('lookup_indices')
                verified.append((bytes(row.account_key), (
                    self.buyer.clock.monotonic(), table.meta.last_extended_slot,
                    tuple(bytes(address) for address in table.addresses))))
            for address, value in verified:
                self.tables.pop(address, None)
                self.tables[address] = value
                while len(self.tables) > 64:
                    self.tables.popitem(last=False)
            if self.resolve(update) and self._alive(signature, owner, mono, feed, generation):
                # The ALT read has finished; a handler's next read takes the same released slot.
                self.buyer._tasks.discard(asyncio.current_task())
                on_ready(update)
        except Exception as exc:
            name = type(exc).__name__
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if isinstance(exc, TimeoutError) or name in ('Timeout', 'ReadTimeout', 'ConnectTimeout'):
                reason = 'timeout'
            elif isinstance(exc, ConnectionError) or name in ('ConnectionError', 'ConnectError', 'NetworkError'):
                reason = 'transport'
            elif status in (401, 403, 429):
                reason = {401: 'unauthorized', 403: 'forbidden', 429: 'rate_limit'}[status]
            elif isinstance(exc, RuntimeError):
                reason = 'rpc_rejected'
            elif isinstance(exc, ValueError) and str(exc) in (
                    'lookup_context', 'lookup_owner', 'lookup_layout', 'lookup_activation', 'lookup_indices'):
                reason = str(exc)
            else:
                reason = 'malformed_response'
            self._note(reason, error=True)
        finally:
            self.pending.discard((signature, owner))


class FrameSink:
    """The preconf feed's work per frame, on the signal loop, memory only (no I/O): parse; exactly one
    tracked owner among the required signers; stamp its arrival beside the other feeds; refuse a
    Failed/Unknown status; decode the candidate (the rabbit owner-frame decode); offer it to the ONE
    shared buyer with this feed's own verified signature basis; queue cold ALT reads without inline
    I/O; hand it to the processed path's
    warm-up, as a rabbit frame is. `status`: refused (by reason), owner_frames, candidates, offered."""

    def __init__(self, *, feed, owners_fn, arrivals, frames, status, buyer=None, prep=None,
                 on_prepare=None, prep_status=None, clock=time.monotonic):
        self.feed, self.owners_fn, self.arrivals, self.frames = feed, owners_fn, arrivals, frames
        self.buyer, self.prep, self.on_prepare, self.prep_status = buyer, prep, on_prepare, prep_status
        self.clock = clock
        self.status = status
        for key in ("owner_frames", "candidates", "offered"):
            status.setdefault(key, 0)
        status.setdefault("refused", {})

    def _refused(self, reason):
        self.status["refused"][reason] = self.status["refused"].get(reason, 0) + 1

    def __call__(self, slot, state, tx, mono, generation):
        from sniper.services.memecoin_shyft_prepare import owner_signer, preparation_candidate, preparation_target
        tables = getattr(self.buyer, 'preconf_tables', None)
        if tables is not None:
            tables.generation(self.feed, generation, self.status.get('feed'))
        try:
            update, verify = frame_update(slot, tx)
        except Malformed as exc:
            return self._refused("malformed:" + str(exc).split(":")[0])
        owners = dict(self.owners_fn() or {})
        signed = owner_signer(update, owners)
        if signed is None:
            return self._refused("not_owner_signed")     # accountInclude also matches non-signers
        signature, owner = signed
        self.status["owner_frames"] += 1
        self.arrivals.stamp(signature, owner, self.feed, mono)
        if self.frames is not None:
            self.frames.frame(signature, owner, slot, mono)
        if state != STATUS_SUCCESS:
            verdict = "source_failed" if state == STATUS_FAILED else "status_unknown"
            if self.frames is not None:
                self.frames.note(signature, owner, verdict)
            return self._refused(verdict)
        if update.transaction.transaction.transaction.message.address_table_lookups:
            if verify(owner) != 'ed25519_local':
                return self._refused('unsigned')
            if self.buyer is None or not callable(getattr(self.buyer, 'read', None)):
                return self._deliver(update, verify, owners, signature, owner, mono, generation)
            tables = getattr(self.buyer, 'preconf_tables', None)
            if tables is None:
                tables = self.buyer.preconf_tables = LookupTables(self.buyer)
            tables.generation(self.feed, generation, self.status.get('feed'))
            mono = tables.receipt(signature, owner, mono)
            if not tables._alive(signature, owner, mono, self.feed, generation):
                return self._refused('alt_source_unavailable')
            if not tables.resolve(update):
                if self.frames is not None:
                    self.frames.note(signature, owner, 'alt_pending')
                tables.offer(update, owner=owner, mono=mono, feed=self.feed, generation=generation,
                    on_ready=lambda resolved: self._deliver(resolved, verify, dict(self.owners_fn() or {}),
                        signature, owner, tables.receipt(signature, owner, mono), generation))
                return None
        return self._deliver(update, verify, owners, signature, owner, mono, generation)

    def _deliver(self, update, verify, owners, signature, owner, mono, generation):
        from sniper.services.memecoin_shyft_prepare import preparation_candidate, preparation_target
        if owner not in owners:
            return self._refused('roster_refused')
        if self.buyer is not None and (signature, owner) in getattr(self.buyer, 'seen', {}):
            return self._refused('processed_first')
        candidate = preparation_candidate(update, owners, recv_monotonic=mono, generation=generation,
                                          on_owner_miss=self.frames.note if self.frames is not None else None)
        if candidate is None:
            return None                                       # its miss reason is on the frame line
        self.status["candidates"] += 1
        candidate["feed"] = self.feed
        target = preparation_target(candidate)
        if target is not None and target["venue"] != "raydium_cpmm":   # a CPMM swap names no side
            self.arrivals.mark_buy(signature, owner)
        if target is not None and self.frames is not None:
            self.frames.note(signature, owner, venue=target["venue"])
        if self.buyer is not None:
            self.status["offered"] += 1
            self.buyer.offer(candidate, update, basis=verify(owner))
        if self.prep is not None and self.on_prepare is not None:
            try:
                if self.prep.offer(candidate, now=self.clock()):
                    self.on_prepare(candidate)
            except Exception:
                if self.prep_status is not None:
                    self.prep_status["callback_errors"] += 1
        return candidate


def _utc_day(now):
    return now.strftime("%Y-%m-%d")


class CreditMeter:
    """Helius preconf credits per UTC day: each slot that delivered at least one message costs
    max(300, 10 x its messages) PER SUBSCRIPTION, so a source's slot is noted as (source, slot) and
    two sources in one slot bill twice; one cap covers every source. Every message counts, parsed or not. At `cap` the feed
    must close until the next UTC midnight (`paused_until`); the page goes out once per day.
    PERSISTED PER UTC DAY (Gamma LOW-2, Alpha 2026-10-17): with `path`, the day's spend and the page
    mark are read at feed start; after each billed message ONE background writer saves the latest
    snapshot (coalescing: while a write is in flight, later bills wait for the next one), and the
    receive loop never awaits it (Gamma MED-2: an fsynced write is ~9-10 ms, 42 ms max, and the next
    frame must not queue behind it). The cap close, the pause and the feed's exit await a final write,
    where no frame is waiting. A crash loses at most the bills since the last completed write
    (typically one covered slot, 300 credits). A restart inside the day keeps the spent count, so the
    cap stays one cap. A new subscription after a restart bills its slots again, as Helius does. An
    unreadable file starts the day at 0, logged."""

    def __init__(self, cap, *, now_fn=None, alert_fn=None, path=None):
        self.cap = int(cap)
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.alert_fn = alert_fn
        self.path = path
        self.day, self.slots, self.credits, self.messages = None, {}, 0, 0
        self.restored_slots, self.paged_day, self.dirty = 0, None, False
        self.status = dict(day=None, credits=0, messages=0, covered_slots=0, cap=self.cap,
                           capped=False, paused_until=None, pages=0, restored=False)
        self._roll()
        self._load()

    def _load(self):
        if self.path is None:
            return
        try:
            with open(self.path) as fh:
                saved = json.load(fh)
        except FileNotFoundError:
            return
        except Exception:
            logger.warning("[memecoin] slot lane preconf credit file unreadable (%s): the day starts at 0",
                           self.path, exc_info=True)
            return
        if not isinstance(saved, dict) or saved.get("day") != self.day:
            return                                          # another day: today starts at 0
        try:
            self.credits, self.messages = int(saved["credits"]), int(saved["messages"])
            self.restored_slots = int(saved.get("covered_slots") or 0)
        except (KeyError, TypeError, ValueError):
            logger.warning("[memecoin] slot lane preconf credit file malformed (%s): the day starts at 0",
                           self.path)
            self.credits = self.messages = self.restored_slots = 0
            return
        self.paged_day = saved.get("paged_day") if saved.get("paged_day") == self.day else None
        self.status.update(credits=self.credits, messages=self.messages, covered_slots=self.restored_slots,
                           restored=True)

    def _roll(self):
        day = _utc_day(self.now_fn())
        if day != self.day:
            if self.day is not None:
                self.dirty = True
            self.day, self.slots, self.credits, self.messages, self.restored_slots = day, {}, 0, 0, 0
            self.status.update(day=day, credits=0, messages=0, covered_slots=0, capped=False,
                               paused_until=None)

    def note(self, slot):
        """One delivered message in `slot`. Returns True when the cap is reached now."""
        self._roll()
        before = self.slots.get(slot, 0)
        self.slots[slot] = before + 1
        self.messages += 1
        added = (max(CREDITS_MIN_PER_SLOT, CREDITS_PER_MESSAGE * (before + 1))
                 - (max(CREDITS_MIN_PER_SLOT, CREDITS_PER_MESSAGE * before) if before else 0))
        self.credits += added
        self.dirty = self.dirty or added > 0
        self.status.update(credits=self.credits, messages=self.messages,
                           covered_slots=self.restored_slots + len(self.slots))
        return self.capped()

    def capped(self):
        self._roll()
        if self.cap > 0 and self.credits >= self.cap:
            tomorrow = (self.now_fn() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            self.status.update(capped=True, paused_until=tomorrow.isoformat())
            return True
        return False

    def pause_s(self):
        """Seconds until the next UTC midnight, while capped; 0 otherwise."""
        if not self.capped():
            return 0.0
        now = self.now_fn()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1.0, (tomorrow - now).total_seconds())

    def page_once(self):
        """The cap page, once per UTC day. Returns the text when it is due (the caller sends it off the loop)."""
        if not self.capped() or self.paged_day == self.day:
            return None
        self.paged_day = self.day
        self.dirty = True
        self.status["pages"] += 1
        return ("slot-lane preconf feed CLOSED until 00:00Z: %d credits today (cap %d, %d messages over "
                "%d covered slots). Rabbit and the processed feeds continue. Raise or clear "
                "MEMECOIN_SLOT_LANE_PRECONF_DAILY_CREDITS and restart to reopen today."
                % (self.credits, self.cap, self.messages, self.restored_slots + len(self.slots)))

    def snapshot(self):
        """What `save` writes; taken on the loop so the writer thread never reads live state."""
        self.dirty = False
        return {"day": self.day, "credits": self.credits, "messages": self.messages,
                "covered_slots": self.restored_slots + len(self.slots), "paged_day": self.paged_day,
                "cap": self.cap}

    def save(self, snap):
        """Write one snapshot atomically (any thread). A failed write is logged; billing goes on."""
        if self.path is None:
            return
        try:
            from sniper.util.atomic import atomic_write_json
            atomic_write_json(str(self.path), snap)
        except Exception:
            logger.warning("[memecoin] slot lane preconf credit file write failed (%s)", self.path,
                           exc_info=True)


def _kick(meter, writer):
    """Start ONE background write of the latest snapshot when the meter changed and no write is in
    flight. Never awaited by the receive loop; a bill during a write stays dirty for the next kick."""
    task = writer.get("task")
    if meter.dirty and meter.path is not None and (task is None or task.done()):
        writer["task"] = asyncio.get_running_loop().create_task(asyncio.to_thread(meter.save, meter.snapshot()))


async def _flush(meter, writer=None):
    """Wait for the in-flight write, then write the latest snapshot when it changed. Only where no frame
    is waiting: the cap close, the pause, the feed's exit."""
    task = (writer or {}).get("task")
    if task is not None and not task.done():
        await asyncio.gather(task, return_exceptions=True)
    if meter.dirty and meter.path is not None:
        await asyncio.to_thread(meter.save, meter.snapshot())


def subscribe_request(owners, *, source="helius", request_id=1, regions=()):
    """The preconfSubscribe call: transactions SIGNED by one of the owners (signerInclude), from one
    source, failed ones neither delivered nor billed (Helius; BAM may ignore `failed`, and the parser
    refuses a Failed status either way)."""
    if source not in SOURCES:
        raise ValueError("unknown preconf source %r" % (source,))
    params = {"failed": False, "signerInclude": sorted(owners), "includeSource": [source]}
    if regions:
        params["regionInclude"] = list(regions)
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "preconfSubscribe", "params": [params]})


def preconf_url(env=None):
    """wss://beta.helius-rpc.com/?api-key=<key> from MEMECOIN_SLOT_LANE_PRECONF_URL, else the research
    Helius key (the paid account), else None. The key is never logged."""
    import os
    from urllib.parse import parse_qs, urlsplit
    src = env if env is not None else os.environ
    explicit = str(src.get("MEMECOIN_SLOT_LANE_PRECONF_URL") or "").strip()
    if explicit:
        return explicit if explicit.startswith("wss://") else None
    research = str(src.get("MEMECOIN_RESEARCH_HELIUS_RPC_URL") or "").strip()
    key = (parse_qs(urlsplit(research).query).get("api-key") or [""])[0] if research else ""
    return "wss://beta.helius-rpc.com/?api-key=%s" % key if key else None


async def run_preconf_feed(url, owners_fn, status, stop, *, on_frame, meter, connect=None, alert_fn=None,
                           clock=time.monotonic, sleep=asyncio.sleep, reroster_s=5.0, source="helius",
                           writer=None):
    """One preconfSubscribe socket for one `source`. `on_frame(slot, status_byte, tx_bytes,
    recv_monotonic, generation)` runs on this loop for every parsed binary frame. A roster change
    re-subscribes by reconnecting (at most once per `reroster_s`); a capped meter closes the socket
    until the next UTC midnight. Sockets sharing one `meter` share one `writer` (the credit file's
    single background write)."""
    if connect is None:
        import websockets

        def connect(u):
            return websockets.connect(u, max_size=1 << 20, max_queue=256, ping_interval=20)
    status.update(connected=False, acknowledged=False, reconnects=0, frames=0, malformed=0,
                  binary_before_ack=0, owners=0, last_error=None, last_frame_mono=None, connection_generation=0,
                  credits=meter.status)
    generation, backoff = 0, 1.0
    writer = {} if writer is None else writer
    while not stop.is_set():
        wait = meter.pause_s()
        if wait:
            page = meter.page_once()
            await _flush(meter, writer)
            if page is not None and alert_fn is not None:
                await asyncio.to_thread(_page, alert_fn, page)
            status.update(connected=False, acknowledged=False)
            await _sleep_until(stop, min(wait, 60.0), sleep)
            continue
        owners = tuple(sorted(dict(owners_fn() or {})))
        if not owners:
            status.update(connected=False, acknowledged=False, owners=0)
            await _sleep_until(stop, 1.0, sleep)
            continue
        generation += 1
        status.update(connection_generation=generation, owners=len(owners))
        try:
            async with connect(url) as ws:
                await ws.send(subscribe_request(owners, source=source))
                status["connected"] = True
                subscribed_at = clock()
                backoff = 1.0
                while not stop.is_set():
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        message = None
                    if message is not None:
                        mono = clock()
                        if isinstance(message, (bytes, bytearray)):
                            if not status["acknowledged"]:
                                status["binary_before_ack"] += 1
                            _handle_binary(bytes(message), mono, generation, status, meter, on_frame,
                                           source=source)
                        else:
                            reply = json.loads(message)
                            if reply.get("error"):
                                raise RuntimeError("preconfSubscribe refused: %s"
                                                   % str(reply["error"].get("message", ""))[:80])
                            if reply.get("id") == 1 and "result" in reply:
                                status["acknowledged"] = True
                    _kick(meter, writer)                            # background; never awaited here
                    if meter.capped():
                        break                                       # closes the socket: no more billing
                    current = tuple(sorted(dict(owners_fn() or {})))
                    if current != owners and clock() - subscribed_at >= reroster_s:
                        break                                       # re-subscribe on the new roster
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status["last_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:80])
            status["reconnects"] += 1
            status.update(connected=False, acknowledged=False)
            await _sleep_until(stop, backoff, sleep)
            backoff = min(backoff * 2.0, 30.0)
            continue
        status.update(connected=False, acknowledged=False)
    await _flush(meter, writer)                                     # the exit: no frame is waiting


def _handle_binary(data, mono, generation, status, meter, on_frame, *, source=None):
    try:
        slot, _index, state, tx = parse_frame(data)
    except Malformed:
        status["malformed"] += 1
        meter.note(("unparsed", meter.messages))   # its slot is unknown: billed as its own slot (300)
        return
    meter.note(slot if source is None else (source, slot))    # each subscription bills its own slot
    status["frames"] += 1
    status["last_frame_mono"] = mono
    try:
        on_frame(slot, state, tx, mono, generation)
    except Exception:
        status["callback_errors"] = status.get("callback_errors", 0) + 1
        logger.exception("[memecoin] slot lane preconf frame callback failed slot=%s", slot)


def _page(alert_fn, text):
    try:
        alert_fn("SLOT-LANE PRECONF CREDIT CAP", text)
    except Exception:
        logger.exception("[memecoin] slot lane preconf cap page failed")


async def _sleep_until(stop, seconds, sleep):
    end = time.monotonic() + seconds
    while not stop.is_set() and time.monotonic() < end:
        await sleep(min(0.5, max(0.0, end - time.monotonic())))
