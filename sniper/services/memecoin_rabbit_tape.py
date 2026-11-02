"""The rabbit pre-execution POOL TAPE: account state from one processed Yellowstone stream, so a
pre-execution fire prices the source from memory instead of an RPC read (task 5a7e1967, Alpha GO
2026-10-17, MEMECOIN_SLOT_LANE_RABBIT_TAPE; 0 = every read goes to RPC exactly as before).

WHY. A RabbitStream frame leads the processed feed by p50 4.6 ms (preconf 10-12 ms) and the funded
read role's getMultipleAccounts takes p50 13 ms: at the median every pre-execution read lost the
race (5WWNimzM: processed_first_after_read). A memory lookup does not.

WHAT IT HOLDS (one connection on the funded Shyft token, processed commitment):
  accounts owner=pump program      every bonding curve write;
  accounts owner=pAMM program      every PumpSwap pool account write (virtual quote, creator...);
  accounts by address              the two FeeConfigs, plus the vaults and base mints of the pools
                                   the lane's PumpSwapPoolCache knows (watch_fn), re-sent at most once
                                   a second and only when that set changed;
  slots, interslot updates         SLOT_CREATED_BANK(X, parent P): agave freezes P before it creates X.
Each account keeps its last RING writes as (slot, write_version, account), immutable tuples swapped
whole: readers (the event loop) take no lock; the stream thread and the seeders share one writer lock.

THE STATE FOR A FRAME IN SLOT S is the end of S's own parent P (from CREATED_BANK(S)): for each
account, its newest write in P or an ancestor of P (the ancestry walked through the slot map), never
a write in S or later and never one on a sibling fork. The handlers then see a read whose context
slot is P < S, so the source's own buy cannot be in it (pre by construction).

A MISS (the caller reads RPC exactly as before), by reason, counted and printed on the frame line:
  no_coverage       not connected, or no slot seen since the last (re)connect;
  tape_lag          the tape's newest created slot trails the lane head by more than LAG_SLOTS;
  parent_unknown    CREATED_BANK(S) not seen (the Rabbit shred can run ahead of the geyser node);
  ancestry_unknown  the slot map breaks between P and an account's last write;
  unseen            an account with no write since the last gap (quiet, new, or just added to the
                    address filter: a re-send is a gap for its NEW addresses only).
A reconnect or stream error resets everything: continuity is by construction, never assumed.

SEEDS. An RPC fallback read may seed the accounts it read (seed(), any thread, never the stream
thread, which issues no RPC at all) when its generation is still current. Inside one slot a stream
write always beats a seed (the seed's position in that slot is unknown); a seed never replaces a
newer write. The same owner loop also schedules one background snapshot of the two always-watched
FeeConfigs per 30 seconds, on a separate HTTP client and worker thread. No dynamic account is seeded
by that worker; missing or invalid state keeps the original fallback.
Exact duplicate writes are kept once; conflicting duplicates invalidate the bank. Stream account
signature/bank/startup metadata enables the fresh guard; frozen readers retain the parent-state rule.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import logging
import threading
import time

logger = logging.getLogger(__name__)

SLOT_CREATED_BANK = 5          # geyser.SlotStatus.SLOT_CREATED_BANK
SLOT_DEAD = 6
LAG_SLOTS = 2                  # = MEMECOIN_SLOT_LANE_RABBIT_READ_MAX_LAG_SLOTS's default; one bound
WATCH_RESEND_MIN_S = 10.0      # additions batch this long into one subscribe update (Gamma, 2026-10-17)
WATCH_SWEEP_S = 60.0           # removals alone wait for the next addition or this sweep
WATCH_POLL_S = 1.0             # how often the request loop reads the watch set
PING_S = 5.0
ANCESTRY_MAX = 512
SNAPSHOT_REFRESH_S = 30.0
SNAPSHOT_TIMEOUT_S = 1.0


def knob_on():
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_TAPE", False))


def tape_transport(url, connect_ip=None):
    """Resolve this tape's optional numeric route once at setup; keep its TLS identity."""
    import ipaddress
    import os
    from urllib.parse import urlsplit
    if connect_ip is None:
        connect_ip = os.getenv("MEMECOIN_SHYFT_TAPE_CONNECT_IP", "")
    if not isinstance(connect_ip, str):
        raise ValueError("shyft_tape_connect_ip_invalid")
    connect_ip = connect_ip.strip()
    if connect_ip:
        if url not in ("https://grpc.ams.shyft.to", "https://grpc.ams.shyft.to/",
                       "https://grpc.ams.shyft.to:443", "https://grpc.ams.shyft.to:443/"):
            raise ValueError("shyft_tape_url_invalid")
        try:
            connect_ip = str(ipaddress.IPv4Address(connect_ip))
        except ValueError:
            raise ValueError("shyft_tape_connect_ip_invalid") from None
    host = urlsplit(url).hostname
    return dict(host=host, dial_target="ipv4:" + connect_ip + ":443" if connect_ip else host + ":443",
                tls_authority=host, connect_ip=connect_ip)


class PoolTape:
    RING = 4
    MAX_KEYS = 200_000
    PARENTS_MAX = 2048
    GROUP_KEYS_MAX = 64
    _GROUP_DATA_MAX = 4096

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self._write_lock = threading.Lock()
        self._acc = {}                         # key -> ((slot, write_version, seeded, account|None), ...)
        self._parent = {}                      # slot -> parent, from CREATED_BANK
        self._order = collections.deque()      # created slots, oldest first (trim)
        self.head = None                       # newest CREATED_BANK slot this generation
        self.coverage_start = None             # first CREATED_BANK after the initial subscription
        self._has_child = set()                # slots some CREATED_BANK named as parent: frozen, by the rule
        self._tainted = set()                  # frozen slots that still got a write: the rule broke there
        self.generation = 0
        self._reset_provenance()
        self.connected = False
        self.stats = dict(resets=0, last_reset=None, writes=0, seeds=0, seeds_stale=0, evicted=0,
                          hits=0, misses={}, keys=0, late_parent_writes=0)
        self._snapshots = dict(attempts=0, successes=0, failures=0, discarded=0, last_error=None,
                               last_slot=None, last_generation=None, last_success_mono=None)

    # -- writers (stream thread; seed from any thread) -----------------------------------------
    def reset(self, reason):
        with self._write_lock:
            self.generation += 1
            self._acc, self._parent, self._order, self.head = {}, {}, collections.deque(), None
            self.coverage_start = None
            self._has_child, self._tainted = set(), set()
            self._reset_provenance()
            self.connected = reason == "connect"
            self.stats["resets"] += 1
            self.stats["last_reset"] = reason

    def disconnect(self, reason):
        self.reset(reason)
        self.connected = False

    def _reset_provenance(self):
        self._banks, self._account_banks, self._bank_tainted = {}, {}, set()
        self._bank_order, self._provenance_groups = collections.OrderedDict(), collections.OrderedDict()
        self._provenance_entries = {}  # (key, slot, write_version, seeded) -> retained full entry
        self._bank_conflict_samples = collections.deque(maxlen=8)
        self.provenance_revision = 0
        names = ("updates", "signature", "bank_id", "both", "startup", "malformed")
        self._provenance = dict.fromkeys(names, 0)
        self._provenance.update(groups_with_2_accounts=0, groups_with_3_accounts=0,
                                created_with_bank_id=0, dead=0, bank_conflicts=0, duplicate_conflicts=0,
                                retention_trims=0,
                                bank_conflicts_by_kind=dict.fromkeys(("account_vs_account", "account_vs_created",
                                    "created_vs_account", "created_vs_created", "parent"), 0),
                                by_filter={label: dict.fromkeys(names, 0)
                                           for label in ("curve", "amm", "watch", "other")})

    def _track_bank_slot(self, slot):
        self._bank_order[slot] = None
        while len(self._bank_order) > self.PARENTS_MAX:
            old, _ = self._bank_order.popitem(last=False)
            self._banks.pop(old, None)
            self._account_banks.pop(old, None)
            self._bank_tainted.discard(old)
            if old not in self._parent:
                self._tainted.discard(old)
            self.provenance_revision += 1

    def _taint_bank(self, slot, *, raw=False):
        self._bank_tainted.add(slot)
        if raw:
            self._tainted.add(slot)
        self.provenance_revision += 1

    def _record_bank_conflict(self, kind, slot, bank_id, *, label="slot", write_version=None, parent=None):
        # Several comparisons can disagree in one callback; bank_conflicts still counts that callback
        # once. Diagnostic samples contain no account keys, signatures, data or arbitrary labels.
        self._provenance["bank_conflicts_by_kind"][kind] += 1
        self._bank_conflict_samples.append(dict(
            kind=kind, slot=slot, incoming_bank_id=bank_id,
            created_bank_id=self._banks.get(slot), account_bank_id=self._account_banks.get(slot),
            label=label if label in ("curve", "amm", "watch", "slot") else "other",
            write_version=write_version if type(write_version) is int else None,
            parent=parent, previous_parent=self._parent.get(slot)))

    def on_slot(self, slot, parent, status, *, bank_id=None):
        if type(slot) is not int:
            return
        valid_bank = type(bank_id) is int and 0 <= bank_id < 2**64
        if status == SLOT_DEAD:
            with self._write_lock:
                self._track_bank_slot(slot)
                self._taint_bank(slot, raw=True)  # Dead is intrinsic invalidation, not optional ID ambiguity.
                self._provenance["dead"] += 1
            return
        if status != SLOT_CREATED_BANK or type(slot) is not int or type(parent) is not int or slot <= parent:
            return
        with self._write_lock:
            self._track_bank_slot(slot)
            parents = self._parent
            previous = self._banks.get(slot)
            created_conflict = valid_bank and previous is not None and previous != bank_id
            account_conflict = valid_bank and slot in self._account_banks and self._account_banks[slot] != bank_id
            parent_conflict = slot in parents and parents[slot] != parent
            if created_conflict or account_conflict or parent_conflict:
                self._taint_bank(slot, raw=parent_conflict)
                self._provenance["bank_conflicts"] += 1
                incoming = bank_id if valid_bank else None
                if created_conflict:
                    self._record_bank_conflict("created_vs_created", slot, incoming, parent=parent)
                if account_conflict:
                    self._record_bank_conflict("created_vs_account", slot, incoming, parent=parent)
                if parent_conflict:
                    self._record_bank_conflict("parent", slot, incoming, parent=parent)
            if valid_bank:
                self._provenance["created_with_bank_id"] += 1
                if previous != bank_id:
                    self._banks[slot] = bank_id
            if slot not in parents:
                self._order.append(slot)
            parents[slot] = parent
            self._has_child.add(parent)
            while len(self._order) > self.PARENTS_MAX:
                old = self._order.popleft()
                parents.pop(old, None)
                self._has_child.discard(old)
                self._tainted.discard(old)
            if self.head is None or slot > self.head:
                if self.coverage_start is None:
                    self.coverage_start = slot
                self.head = slot

    def on_account(self, key, owner, lamports, data, slot, write_version, *, deleted=False,
                   txn_signature=None, bank_id=None, is_startup=False, label="other"):
        signature_ok = type(txn_signature) is bytes and len(txn_signature) == 64
        bank_ok = type(bank_id) is int and 0 <= bank_id < 2**64
        malformed = ((txn_signature is not None and not signature_ok)
                     or (bank_id is not None and not bank_ok) or type(is_startup) is not bool)
        txn_signature = txn_signature if signature_ok else None
        bank_id = bank_id if bank_ok else None
        is_startup = is_startup if type(is_startup) is bool else True
        account = None if deleted else {"owner": owner, "lamports": int(lamports), "data": bytes(data),
                                        "txn_signature": txn_signature, "bank_id": bank_id,
                                        "is_startup": is_startup}
        with self._write_lock:
            slot = int(slot)
            self._track_bank_slot(slot)
            p = self._provenance
            if bank_ok:
                # Account callbacks may precede CREATED_BANK. Remember identity even for deleted
                # records (whose public account remains None), but only CREATED_BANK fills _banks.
                created_conflict = slot in self._banks and self._banks[slot] != bank_id
                account_conflict = slot in self._account_banks and self._account_banks[slot] != bank_id
                if created_conflict or account_conflict:
                    self._taint_bank(slot)
                    p["bank_conflicts"] += 1
                    if created_conflict:
                        self._record_bank_conflict("account_vs_created", slot, bank_id,
                                                   label=label, write_version=write_version)
                    if account_conflict:
                        self._record_bank_conflict("account_vs_account", slot, bank_id,
                                                   label=label, write_version=write_version)
                if slot not in self._account_banks:
                    self._account_banks[slot] = bank_id
            per_filter = p["by_filter"].get(label, p["by_filter"]["other"])
            for name, present in (("updates", True), ("signature", signature_ok), ("bank_id", bank_ok),
                                  ("both", signature_ok and bank_ok), ("startup", is_startup),
                                  ("malformed", malformed)):
                if present:
                    p[name] += 1
                    per_filter[name] += 1
            # DETECTED, NOT ASSUMED (Gamma BLOCK-1): the end-of-parent rule rests on geyser emitting a
            # parent's writes before CREATED_BANK of its child. A write into a slot that already has a
            # child taints that slot: every lookup whose ancestry crosses it misses.
            if slot in self._has_child:
                self.stats["late_parent_writes"] += 1
                self._tainted.add(int(slot))
            entry = (slot, int(write_version), False, account)
            accepted = self._put(key, entry)
            if accepted and account is not None and signature_ok and bank_ok and not is_startup and not malformed:
                self._retain_provenance(key, entry)
            self.stats["writes"] += 1

    def _retain_provenance(self, key, entry):
        # Immutable group snapshots retain source ordering after raw RING eviction. They never
        # supply the latest state. Full entries share the existing immutable account/bytes objects.
        account = entry[3]
        group = (entry[0], account["bank_id"], account["txn_signature"])
        records = self._provenance_groups.get(group)
        if records == () or len(account["data"]) > self._GROUP_DATA_MAX:
            # A bounded tombstone prevents an old replay rebuilding incomplete source proof.
            for old_key, previous in records or ():
                self._provenance_entries.pop((old_key, *previous[:3]), None)
            self._provenance["retention_trims"] += len(records or ()) + 1
            records = ()
        else:
            records = records or ()
            for index, (old_key, previous) in enumerate(records):
                if old_key == key:
                    if previous[1] >= entry[1]:
                        return
                    self._provenance_entries.pop((key, *previous[:3]), None)
                    records = records[:index] + ((key, entry),) + records[index + 1:]
                    break
            else:
                if len(records) >= self.GROUP_KEYS_MAX:
                    self._provenance["retention_trims"] += 1
                    return
                records += ((key, entry),)
                if len(records) in (2, 3):
                    self._provenance["groups_with_%d_accounts" % len(records)] += 1
            self._provenance_entries[(key, *entry[:3])] = entry
        self._provenance_groups[group] = records
        while len(self._provenance_groups) > self.PARENTS_MAX:
            _group, removed = self._provenance_groups.popitem(last=False)
            for old_key, previous in removed:
                self._provenance_entries.pop((old_key, *previous[:3]), None)
            self._provenance["retention_trims"] += len(removed)

    def seed(self, key, account_json, slot, generation):
        """One account an RPC fallback read returned (the JSON getMultipleAccounts shape, base64),
        at that read's context slot, for the generation it was read in."""
        if type(slot) is not int:
            return
        try:
            account = None if account_json is None else {
                "owner": account_json.get("owner"), "lamports": int(account_json.get("lamports") or 0),
                "data": base64.b64decode((account_json.get("data") or [""])[0] or "")}
        except Exception:
            return
        with self._write_lock:
            if generation != self.generation:
                self.stats["seeds_stale"] += 1
                return
            old = self._acc.get(key, ())
            if any(e[0] >= slot for e in old):     # a stream write in the same slot or later wins
                return
            self._put(key, (slot, -1, True, account))
            self.stats["seeds"] += 1

    def _put(self, key, entry):
        old = self._acc.get(key)
        if not entry[2]:
            retained = self._provenance_entries.get((key, *entry[:3]))
            for previous in (old or ()) + ((retained,) if retained is not None else ()):
                if previous[:3] == entry[:3] and previous[3] != entry[3]:
                    raw_conflict = (previous[3] is None or entry[3] is None or any(
                        previous[3][name] != entry[3][name] for name in ("owner", "lamports", "data")))
                    self._taint_bank(entry[0], raw=raw_conflict)
                    self._provenance["duplicate_conflicts"] += 1
                    return False
        if old is None and len(self._acc) >= self.MAX_KEYS:
            self._evict()
        unique = {(e[0], e[1], e[2]): e for e in (old or ()) + (entry,)}   # a replayed write is kept once
        merged = tuple(sorted(unique.values(), key=lambda e: (e[0], e[1])))[-self.RING:]
        acc = self._acc
        acc[key] = merged
        self.stats["keys"] = len(acc)
        # An unverifiable arrival below the raw ring cannot introduce new retained proof.
        return any(candidate is entry for candidate in merged)

    def _evict(self):
        """Drop the quarter of keys whose newest write is oldest (writer lock held)."""
        ranked = sorted(self._acc.items(), key=lambda kv: kv[1][-1][0])
        drop = max(1, len(ranked) // 4)
        fresh = dict(ranked[drop:])
        self._acc = fresh                       # readers keep their old reference: still consistent
        # A recreated key cannot use a source witness from before its coverage gap.
        self._provenance["retention_trims"] += len(self._provenance_entries)
        self._provenance_groups, self._provenance_entries = collections.OrderedDict(), {}
        self.stats["evicted"] += drop

    # -- reader (event loop, lock-free) ----------------------------------------------------------
    def lookup(self, keys, frame_slot, *, lane_head=None):
        """({key: account JSON | None}, parent_slot) or (None, reason). Memory only, no lock."""
        generation = self.generation               # first (Gamma LOW-1): a reset after it is caught below
        acc, parents, head, tainted = self._acc, self._parent, self.head, self._tainted
        raw_taints = frozenset(tainted)
        out = self._lookup(acc, parents, head, keys, frame_slot, lane_head, tainted)
        if out[0] is not None and (generation != self.generation or tainted != raw_taints):
            out = (None, "no_coverage")             # reset or intrinsic raw invalidation while reading
        if out[0] is None:
            misses = self.stats["misses"]
            misses[out[1]] = misses.get(out[1], 0) + 1
        else:
            self.stats["hits"] += 1
        return out

    def _lookup(self, acc, parents, head, keys, frame_slot, lane_head, tainted=frozenset(),
                extra_tainted=frozenset()):
        if not self.connected or head is None or type(frame_slot) is not int:
            return None, "no_coverage"
        if type(lane_head) is int and lane_head - head > LAG_SLOTS:
            return None, "tape_lag"
        parent = parents.get(frame_slot)
        if parent is None:
            return None, "parent_unknown"
        rings = []
        for key in keys:
            ring = acc.get(key)
            if not ring:
                return None, "unseen"
            rings.append((key, ring))
        floor = min(e[0] for _key, ring in rings for e in ring)
        chain, slot, steps = {parent}, parent, 0
        while slot > floor and steps < ANCESTRY_MAX:
            nxt = parents.get(slot)
            if nxt is None:
                break
            chain.add(nxt)
            slot, steps = nxt, steps + 1
        bottom = slot                               # the lowest slot the chain is proven down to
        if tainted and not chain.isdisjoint(tainted):
            return None, "late_parent_write"
        if extra_tainted and not chain.isdisjoint(extra_tainted):
            return None, "bank_provenance"
        out = {}
        for key, ring in rings:
            for s, _wv, _seeded, account in reversed(ring):
                if s > parent:
                    continue                        # S itself, or later
                if s in chain:
                    out[key] = _json(account)
                    break
                if s < bottom:
                    return None, "ancestry_unknown"  # the map broke above this write
                # s is between bottom and parent but not an ancestor: a sibling fork; keep looking
            else:
                return None, "unseen"
        return out, parent

    def status(self):
        snapshots = dict(self._snapshots)
        stamp = snapshots.pop("last_success_mono")
        snapshots["last_success_age_s"] = round(max(0.0, self.clock() - stamp), 3) if stamp is not None else None
        snapshots["current_generation"] = bool(self.connected and snapshots["last_generation"] == self.generation)
        provenance = dict(self._provenance, generation=self.generation,
                          banks=len(self._banks), tainted_slots=len(self._bank_tainted),
                          retained_groups=len(self._provenance_groups),
                          retained_entries=len(self._provenance_entries),
                          bank_conflicts_by_kind=dict(self._provenance["bank_conflicts_by_kind"]),
                          bank_conflict_samples=[dict(sample) for sample in tuple(self._bank_conflict_samples)],
                          by_filter={label: dict(counts) for label, counts in self._provenance["by_filter"].items()})
        return dict(self.stats, misses=dict(self.stats["misses"]), generation=self.generation,
                    connected=self.connected, head=self.head, snapshots=snapshots, provenance=provenance)


def _json(account):
    if account is None:
        return None
    return {"owner": account["owner"], "lamports": account["lamports"],
            "data": [base64.b64encode(account["data"]).decode(), "base64"]}


# -- the stream ----------------------------------------------------------------------------------

def subscription(addresses):
    from sniper.execution.pump_native_exit import PUMP_PROGRAM
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    from sniper.services.memecoin_rabbit_dflow_amm import AMM_PROGRAM
    accounts = {"curve": pb.SubscribeRequestFilterAccounts(owner=[PUMP_PROGRAM]),
                "amm": pb.SubscribeRequestFilterAccounts(owner=[AMM_PROGRAM])}
    if addresses:
        accounts["watch"] = pb.SubscribeRequestFilterAccounts(account=list(addresses))
    return pb.SubscribeRequest(
        accounts=accounts,
        slots={"head": pb.SubscribeRequestFilterSlots(filter_by_commitment=False, interslot_updates=True)},
        commitment=pb.CommitmentLevel.PROCESSED)


def fee_config_addresses():
    from sniper.execution.solana_native_entry_quote import accounts_for
    keys = accounts_for("So11111111111111111111111111111111111111112")
    return keys["curve_fee_config"], keys["amm_fee_config"]


def watch_addresses(pool_cache):
    """The FeeConfigs plus every known PumpSwap pool's vaults and base mint (no I/O)."""
    out = set(fee_config_addresses())
    for facts in tuple(getattr(pool_cache, "_pools", {}).values()) if pool_cache is not None else ():
        for name in ("pool_base_token_account", "pool_quote_token_account", "base_mint"):
            if facts.get(name):
                out.add(facts[name])
    return tuple(sorted(out))


async def warm_fee_configs(tape, snapshot, stop):
    """One paced, off-loop read of the two initially watched pricing configs; never dynamic keys.

    The RPC bank must be past the first covered slot: a snapshot from within that bootstrap slot
    could omit a write from before subscription. Preserve its actual context slot and generation;
    normal tape lookup still proves ancestry, parent freshness and stream-write precedence.
    """
    from sniper.execution.pump_fees import parse_fee_config
    from sniper.execution.pump_native_exit import FEE_PROGRAM
    keys, next_at = fee_config_addresses(), 0.0
    stats = tape._snapshots
    while not stop.is_set():
        generation, start, head = tape.generation, tape.coverage_start, tape.head
        if (not tape.connected or type(start) is not int or type(head) is not int
                or head <= start or tape.clock() < next_at):
            await asyncio.sleep(.25)
            continue
        stats["attempts"] += 1
        try:
            result = await asyncio.to_thread(snapshot, "getMultipleAccounts", [list(keys), {
                "encoding": "base64", "commitment": "processed", "minContextSlot": start + 1}],
                timeout=SNAPSHOT_TIMEOUT_S)
            slot = (result.get("context") or {}).get("slot")
            values = result.get("value")
            if (stop.is_set() or generation != tape.generation or not tape.connected
                    or tape.coverage_start != start):
                stats["discarded"] += 1
                continue
            if (type(slot) is not int or not start < slot <= tape.head
                    or not isinstance(values, list) or len(values) != len(keys)):
                raise ValueError("snapshot_shape")
            for account in values:
                if (not isinstance(account, dict) or account.get("owner") != FEE_PROGRAM
                        or account.get("executable") is not False
                        or type(account.get("lamports")) is not int or account["lamports"] <= 0):
                    raise ValueError("snapshot_owner")
                data = account.get("data")
                if (not isinstance(data, list) or len(data) != 2 or data[1] != "base64"
                        or not isinstance(data[0], str) or len(data[0]) > 128 * 1024):
                    raise ValueError("snapshot_encoding")
                base64.b64decode(data[0], validate=True)
                parse_fee_config(data[0])
            for key, account in zip(keys, values):
                tape.seed(key, account, slot, generation)
            if generation != tape.generation or not tape.connected:
                stats["discarded"] += 1
                continue
            stats.update(successes=stats["successes"] + 1, last_error=None, last_slot=slot,
                         last_generation=generation, last_success_mono=tape.clock())
        except Exception as exc:
            stats["failures"] += 1
            stats["last_error"] = type(exc).__name__
        finally:
            next_at = tape.clock() + SNAPSHOT_REFRESH_S
        await asyncio.sleep(.25)


async def run_tape(url, token, tape, watch_fn, status, stop, *, channel_fn=None):
    """Hold the one processed stream; never issues an RPC read. Reconnects after 1..30 s."""
    import base58
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    transport = tape_transport(url)
    status.update(connected=False, reconnects=0, frames=0, bytes=0, by_filter={}, watch=0,
                  resends=0, addresses_added=0, last_error=None, thread_cpu_s=None, **transport)
    backoff = 1.0
    while not stop.is_set():
        stream = None
        try:
            if channel_fn is None:
                import grpc
                options = [("grpc.max_receive_message_length", 16 * 1024 * 1024),
                           ("grpc.keepalive_time_ms", 10000)]
                if transport["connect_ip"]:
                    options += [("grpc.ssl_target_name_override", transport["tls_authority"]),
                                ("grpc.default_authority", transport["tls_authority"]),
                                ("grpc.enable_http_proxy", 0)]
                channel = grpc.aio.secure_channel(transport["dial_target"], grpc.ssl_channel_credentials(),
                                                  options=options)
            else:
                channel = channel_fn()
            async with channel:
                async def requests():
                    # THE FILTER RE-SEND (Gamma flag on 2490b5f66: 30 re-sends a minute of ~9k addresses,
                    # the pool cache's set changing almost every second). Re-sent only for ADDITIONS,
                    # all additions of a WATCH_RESEND_MIN_S window in one update; a set that only lost
                    # addresses waits for the next addition or the WATCH_SWEEP_S sweep. A new address
                    # is unseen (a miss) until its first write; covered ones are never gapped.
                    sent, sent_at, ping, polled = None, float("-inf"), 0, float("-inf")
                    last_ping = time.monotonic()
                    while not stop.is_set():
                        now = time.monotonic()
                        if now - polled >= WATCH_POLL_S:
                            polled = now
                            current = frozenset(watch_fn() or ())
                            added = current - sent if sent is not None else current
                            removed_only = sent is not None and not added and current != sent
                            if sent is None or (added and now - sent_at >= WATCH_RESEND_MIN_S) or (
                                    removed_only and now - sent_at >= WATCH_SWEEP_S):
                                if sent is not None:
                                    status["resends"] += 1
                                    status["addresses_added"] += len(added)
                                status["watch"] = len(current)
                                yield subscription(tuple(sorted(current)))
                                sent, sent_at = current, now
                        if now - last_ping >= PING_S:
                            ping += 1
                            last_ping = now
                            yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=ping))
                        await asyncio.sleep(0.25)
                call = channel.stream_stream("/geyser.Geyser/Subscribe",
                                             request_serializer=pb.SubscribeRequest.SerializeToString,
                                             response_deserializer=pb.SubscribeUpdate.FromString)
                stream = call(requests(), metadata=(("x-token", token),))
                tape.reset("connect")
                status.update(connected=True, connected_mono=time.monotonic(), frames=0, bytes=0, by_filter={},
                              resends=0, addresses_added=0)
                by = status["by_filter"]
                async for update in stream:
                    if stop.is_set():
                        break
                    size = update.ByteSize()
                    status["frames"] += 1
                    status["bytes"] += size
                    if update.HasField("account"):
                        acct = update.account.account
                        label = next(iter(update.filters), "?")
                        row = by.setdefault(label, [0, 0])
                        row[0] += 1
                        row[1] += size
                        tape.on_account(base58.b58encode(bytes(acct.pubkey)).decode(),
                                        base58.b58encode(bytes(acct.owner)).decode(), acct.lamports,
                                        bytes(acct.data), update.account.slot, acct.write_version,
                                        deleted=acct.lamports == 0,
                                        txn_signature=bytes(acct.txn_signature) if acct.HasField("txn_signature") else None,
                                        bank_id=update.account.bank_id if update.account.HasField("bank_id") else None,
                                        is_startup=update.account.is_startup, label=label)
                    elif update.HasField("slot"):
                        slot = update.slot
                        tape.on_slot(slot.slot, slot.parent if slot.HasField("parent") else None, slot.status,
                                     bank_id=slot.bank_id if slot.HasField("bank_id") else None)
                    status["thread_cpu_s"] = round(time.thread_time(), 2)
                backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status["last_error"] = type(exc).__name__
            status["reconnects"] += 1
            logger.warning("[memecoin] rabbit tape stream ended: %s (%s); reconnect in %.0f s",
                           type(exc).__name__, str(exc)[:160], backoff)
        finally:
            if stream is not None:
                stream.cancel()
            status["connected"] = False
            tape.disconnect("stream_end")
        deadline = time.monotonic() + backoff
        while not stop.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        backoff = min(30.0, backoff * 2)


def start_tape_thread(url, token, tape, watch_fn, status, stop, *, snapshot_factory=None):
    async def owned():
        # THE LOOP-LOCAL gRPC WAKEUP HANDLER, before the first channel, as every other channel owner
        # installs it (Codex speed audit 706f4245f, item 1: without it this loop logged ~345
        # PollerCompletionQueue BlockingIOError(EAGAIN) tracebacks a second).
        from sniper.services.memecoin_grpc_wakeup import install_grpc_wakeup_handler
        install_grpc_wakeup_handler()
        warmer = None
        if snapshot_factory is not None:
            try:
                warmer = asyncio.create_task(warm_fee_configs(tape, snapshot_factory(), stop))
            except Exception as exc:
                tape._snapshots["last_error"] = "init:" + type(exc).__name__
        try:
            await run_tape(url, token, tape, watch_fn, status, stop)
        finally:
            if warmer is not None:
                warmer.cancel()
                await asyncio.gather(warmer, return_exceptions=True)

    def main():
        try:
            asyncio.run(owned())
        except Exception:
            logger.exception("[memecoin] rabbit tape thread died")
    thread = threading.Thread(target=main, name="rabbit-pool-tape", daemon=True)
    thread.start()
    return thread
