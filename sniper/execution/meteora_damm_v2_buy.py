"""Meteora DAMM v2 (cp-amm) buys on the slot lane, B2 (spec 2026-10-01-memecoin-slot-lane-routed-
venues-design.md; Alpha GO). SOL-quoted pools only; another quote waits for decision D1.

WHAT THE LANE CAN SEE. DAMM v2 emits its swap event by self-CPI (`emit_cpi!`), not in the logs: the
processed logs socket shows only `Program cpamdp... invoke`, never the pool or its amounts
(confirmed on LILMET's source transaction, 2026-10-01). So:
- the routed frame carries a `DammV2Seen` marker when cp-amm ran;
- the coin's pool comes from a coin -> pool cache, learned OFF the fill path from the confirmed
  creation transaction when the prewarmer sees initialization, otherwise from the confirmed
  source transaction of a first sighting. The pool and both mints are verified off path.
  A buy before verification completes still hands off;
- the price is the source's own average, USDC spent / tokens received (the routed valuation), and
  the signed minimum out is the lane's price floor at source price x (1 + chase cap). No provider
  quote, no reserve read on the fill path.

Pool layout and instruction verified on chain 2026-10-01: Pool discriminator sha256("account:Pool"),
1112 bytes, token_a_mint at 168, token_b_mint 200, token_a_vault 232, token_b_vault 264; swap
discriminator sha256("global:swap"), args (amount_in u64, minimum_amount_out u64), 14 accounts.

MEMECOIN_SLOT_LANE_DAMM_V2: off (default) | shadow (build + simulate once, never send) | live.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import math
import struct
import threading
import time
from dataclasses import dataclass

PROGRAM = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
POOL_AUTHORITY = "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC"     # PDA ["pool_authority"]
EVENT_AUTHORITY = "3rmHSu74h1ZcmAisVcWerTCiRDQbUrBKmcwptYGjHfet"    # PDA ["__event_authority"]
POOL_DISCRIMINATOR = hashlib.sha256(b"account:Pool").digest()[:8]
SWAP_DISCRIMINATOR = hashlib.sha256(b"global:swap").digest()[:8]
# Routers (DFlow, 2026-10-02) CPI into `swap2`: same 14 accounts, pool at [1], mints at [6:8].
SWAP2_DISCRIMINATOR = hashlib.sha256(b"global:swap2").digest()[:8]
_INITIALIZERS = {name: index for name, index in (
    ("initialize_customizable_pool", 5), ("initialize_pool", 6),
    ("initialize_pool_with_dynamic_config", 7))}
_CREATE_DISCRIMINATORS = {hashlib.sha256(("global:" + name).encode()).digest()[:8]: index
                         for name, index in _INITIALIZERS.items()}
_CREATE_LOGS = {"Program log: Instruction: " + "".join(w.title() for w in name.split("_"))
                for name in _INITIALIZERS}
POOL_LEN = 1112
OFF_MINT_A, OFF_MINT_B, OFF_VAULT_A, OFF_VAULT_B = 168, 200, 232, 264
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
REASON_PREFIX = "slot_lane_damm_v2"
MAX_PENDING = 64
LEARN_TRIES = 8                 # a first sighting's transaction is read until it is confirmed
_INVOKE = "Program %s invoke" % PROGRAM


def lane_mode():
    """off | shadow | live. Anything unrecognised is off."""
    from sniper.settings import memecoin as cfg
    mode = str(getattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", "off") or "off").strip().lower()
    return mode if mode in ("shadow", "live") else "off"


def damm_live():
    return lane_mode() == "live"


def non_sol_listed():
    """E3 (spec 2026-10-02 exotic quotes): a pool quoted in anything but SOL builds only with
    "meteora_damm_v2" on MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES; it then follows DAMM's own mode."""
    from sniper.settings import memecoin as cfg
    return "meteora_damm_v2" in (getattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", None) or ())


@dataclass(frozen=True)
class DammV2Seen:
    """A routed frame in which cp-amm ran. It names nothing: the event is not in the logs."""


@dataclass(frozen=True)
class DammV2ReadHint:
    """Unsigned source instruction identities, usable only as canonical account read targets."""
    owner: str
    coin: str
    pool: str
    mint_a: str
    mint_b: str
    vault_a: str
    vault_b: str
    program_a: str
    program_b: str


def pool_read_hint(tx, coin, owner):
    """One signer/mint-bound swap CPI target, never payment or successful acquisition proof."""
    import base58
    from sniper.execution.pumpswap_buy import TOKEN_PROGRAM, TOKEN_2022_PROGRAM
    try:
        msg, meta = tx['transaction']['message'], tx['meta']
        if meta.get('err', 'unknown') is not None:
            return None
        keys = _account_keys(tx)
        n = msg['header']['numRequiredSignatures']
        if (type(n) is not int or not 0 < n <= len(msg['accountKeys'])
                or owner not in keys[:n] or len(keys) > 256 or len(set(keys)) != len(keys)):
            return None
        instructions = list(msg.get('instructions') or ())
        for group in meta.get('innerInstructions') or ():
            instructions.extend(group.get('instructions') or ())
        if len(instructions) > 512:
            return None
        found = []
        for ix in instructions:
            if 'programIdIndex' in ix:
                index = ix['programIdIndex']
                if type(index) is not int or not 0 <= index < len(keys):
                    return None
                program = keys[index]
            else:
                program = ix.get('programId')
            if program != PROGRAM:
                continue
            data = base58.b58decode(ix.get('data', ''))
            if data[:8] not in (SWAP_DISCRIMINATOR, SWAP2_DISCRIMINATOR):
                continue  # emit_cpi event bytes carry no swap read target.
            accounts = []
            for key in ix.get('accounts', ()):
                if type(key) is int:
                    if not 0 <= key < len(keys):
                        return None
                    key = keys[key]
                if key not in keys:
                    return None
                accounts.append(key)
            if len(accounts) != 14 or coin not in accounts[6:8] or accounts[8] != owner:
                return None
            if (len(data) != (24 if data[:8] == SWAP_DISCRIMINATOR else 25)
                    or data[:8] == SWAP2_DISCRIMINATOR and data[-1:] != b'\x01'
                    or accounts[0] != POOL_AUTHORITY or accounts[12] != EVENT_AUTHORITY
                    or accounts[11] != PROGRAM or accounts[13] != PROGRAM
                    or any(accounts[i] not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) for i in (9, 10))
                    or len(set(accounts[i] for i in (1, 4, 5, 6, 7))) != 5
                    or any(not _is_address(accounts[i]) for i in (1, 4, 5, 6, 7))):
                return None
            found.append(DammV2ReadHint(owner, coin, accounts[1], accounts[6], accounts[7],
                accounts[4], accounts[5], accounts[9], accounts[10]))
        return found[0] if len(found) == 1 else None
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None


def damm_v2_invoked(logs):
    return any(isinstance(line, str) and line.startswith(_INVOKE) for line in logs or ())


def parse_pool(raw):
    """{mint_a, mint_b, vault_a, vault_b} from a cp-amm Pool account, or ValueError."""
    from solders.pubkey import Pubkey
    if len(raw) < POOL_LEN or raw[:8] != POOL_DISCRIMINATOR:
        raise ValueError("not a DAMM v2 pool account")
    key = lambda off: str(Pubkey.from_bytes(raw[off:off + 32]))      # noqa: E731
    return {"mint_a": key(OFF_MINT_A), "mint_b": key(OFF_MINT_B),
            "vault_a": key(OFF_VAULT_A), "vault_b": key(OFF_VAULT_B)}


def _account_keys(tx):
    msg = tx["transaction"]["message"]
    keys = [k if isinstance(k, str) else k.get("pubkey") for k in msg["accountKeys"]]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(loaded.get("writable") or ()) + list(loaded.get("readonly") or ())


def pool_from_transaction(tx, coin):
    """The DAMM v2 pool a confirmed transaction swapped `coin` on, or None: the cp-amm swap or swap2
    instruction (top level or inner) whose two mints include the coin."""
    import base58
    keys = _account_keys(tx)
    msg = tx["transaction"]["message"]
    ixs = list(msg.get("instructions") or ())
    for inner in (tx.get("meta") or {}).get("innerInstructions") or ():
        ixs.extend(inner.get("instructions") or ())
    for ix in ixs:
        try:
            if keys[ix["programIdIndex"]] != PROGRAM:
                continue
            data = base58.b58decode(ix["data"])
            accounts = [keys[i] for i in ix["accounts"]]
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        if data[:8] in (SWAP_DISCRIMINATOR, SWAP2_DISCRIMINATOR) and len(accounts) >= 14 and coin in accounts[6:8]:
            return accounts[1]
    return None


def creation_signature(value):
    """A successful initialization notification's read target, never trusted pool facts.
    Scope the instruction log to cp-amm: another program can print the same words."""
    logs = value.get("logs") or ()
    if value.get("err") is not None or not any(line in _CREATE_LOGS for line in logs):
        return None
    stack = []
    for line in logs:
        if line.startswith("Program ") and " invoke [" in line:
            stack.append(line.split()[1])
        elif line in _CREATE_LOGS and stack and stack[-1] == PROGRAM:
            return value.get("signature")
        elif line.startswith("Program ") and (line.endswith(" success") or " failed:" in line):
            if stack and stack[-1] == line.split()[1]:
                stack.pop()
    return None


def created_pools_from_transaction(tx):
    """Verified program instruction identities (pool, mint A, mint B), including router CPIs.
    IDL pool indices are 5/6/7; both mints follow pool + position. Account facts are re-read."""
    import base58
    if not isinstance(tx.get("meta"), dict) or tx["meta"].get("err", "unknown") is not None:
        return []
    if type(tx.get("slot")) is not int or tx["slot"] <= 0:
        return []
    keys = _account_keys(tx)
    ixs = list(tx["transaction"]["message"].get("instructions") or ())
    for inner in tx["meta"].get("innerInstructions") or ():
        ixs.extend(inner.get("instructions") or ())
    pools = []
    for ix in ixs:
        try:
            if keys[ix["programIdIndex"]] != PROGRAM:
                continue
            index = _CREATE_DISCRIMINATORS.get(base58.b58decode(ix["data"])[:8])
            if index is None or len(ix["accounts"]) < index + 14:
                continue
            accounts = [keys[i] for i in ix["accounts"]]
            if accounts[index - 1] != POOL_AUTHORITY:
                continue
            target = (accounts[index], accounts[index + 2], accounts[index + 3])
            if all(_is_address(key) for key in target) and target not in pools:
                pools.append(target)
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    return pools


# THE COIN -> POOL STORE (operator yes 2026-10-02 14:2xZ, Alpha GO): 33 runner starts on 2026-10-02
# wiped the in-memory cache (B3A4k63n learned 09:56Z, pool_unknown again 12:49Z and 13:55Z). Rows are
# {coin, pool, slot}: READ TARGETS plus the learn slot (the source transaction's slot), never facts.
STORE_CAP = 4096       # bounded discovery cache; whole-program creation volume exceeds copied buys
WARM_TRIES = 3         # learner passes a stored batch may go unanswered before it waits for the next start
_MULTIPLE_MAX = 100    # getMultipleAccounts accounts per call


def _is_address(value):
    from solders.pubkey import Pubkey
    try:
        return isinstance(value, str) and str(Pubkey.from_string(value)) == value
    except ValueError:
        return False


def load_store(path, cap=STORE_CAP):
    """The stored rows [{coin, pool, slot}], oldest learn slot first and the newest `cap` kept, or []
    when the file is absent or unreadable. A row whose coin or pool is not an address, or whose slot
    is not a whole number, is skipped alone; two rows for one coin keep the newer learn slot. The
    file names what to re-read; it never supplies a fact."""
    import logging
    try:
        with open(path) as fh:
            rows = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logging.getLogger(__name__).warning("damm v2 pool store unreadable path=%s; warming nothing",
                                            path, exc_info=True)
        return []
    if not isinstance(rows, list):
        logging.getLogger(__name__).warning("damm v2 pool store is not a list path=%s; warming nothing", path)
        return []
    best = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        coin, pool, slot = row.get("coin"), row.get("pool"), row.get("slot")
        if (not _is_address(coin) or not _is_address(pool) or isinstance(slot, bool)
                or not isinstance(slot, int) or slot < 0):
            continue
        if coin not in best or slot > best[coin]["slot"]:
            best[coin] = {"coin": coin, "pool": pool, "slot": slot}
    return sorted(best.values(), key=lambda r: r["slot"])[-int(cap):]


class _Unread(Exception):
    """The chain did not answer (a raised read, a short or malformed value list): unknown, not gone."""


class DammV2PoolRefusal(ValueError):
    """A complete confirmed account read disproves this exact pool/mint identity."""


class DammV2PoolCache:
    """coin mint -> pool facts, learned OFF the signal path. `get` on the lane thread never reads
    the network; `queue(signature, coin)` records a first sighting and `learn_pending` reads its
    confirmed transaction for the pool, then the pool and both mints. With `start`, learning runs on
    this cache's OWN thread, never the prefetch thread: a slow read there would age the funded legs
    and balances it also refreshes. One writer (the learner) of the cache, the store and the stats;
    readers see whole dicts.

    ACROSS RESTARTS. With `store_path`, each learn writes {coin, pool, slot} to a small JSON file
    (tmp + os.replace) on the learner thread. At start the learner re-reads every stored pool and its
    mints from chain in bulk (`_warm_batch`), through the same checks and the same facts as
    `learn_pool`: a stored row the chain denies (no account, not a cp-amm Pool, the coin not in it, a
    mint gone) is dropped; one it does not answer is kept in the file, unloaded, for the next start.

    REPOINT. A later cp-amm routed buy of a known coin queues its signature (`recheck`); the learner
    reads that confirmed transaction and, when the coin swapped on another pool, re-learns that pool
    through `learn_pool` (never a raw swap of the key). An older transaction never undoes a newer
    learn. One recheck per learner pass, on the same RPC as the first learn."""

    LEARN_INTERVAL_S = 0.5
    # A recheck waits this long after its buy: the source transaction confirms in ~1 s, and the
    # lane's own send and confirm for that buy run on the same custody RPC meanwhile.
    RECHECK_AFTER_S = 2.0
    ROUTE_SCAN_INTERVAL_S = 60.0
    ROUTE_RETRY_AFTER_S = 15.0
    ROUTE_COOLDOWN_S = 60.0
    HOT_RETENTION_S = 86400.0

    def __init__(self, *, rpc, start=False, store_path=None, store_cap=STORE_CAP,
                 route_targets_fn=None):
        self.rpc = rpc
        self._by_coin = {}
        self._pending = collections.OrderedDict()   # coin -> [signature, tries]
        self._recheck = collections.OrderedDict()   # known coin -> [signature, tries, due monotonic]
        self._creates = collections.OrderedDict()   # initialization signature -> tries
        self._targets = collections.OrderedDict()   # ledger coin -> pool, active monotonic, tries, due
        self._route_failures = collections.OrderedDict()  # (pool, mint) -> due monotonic; None = refused
        self._hot = {}                              # learner's recent source/ledger targets
        self._used = {}                             # lock-free source lookups; ledger never overwrites them
        self.route_targets_fn, self._next_route_scan = route_targets_fn, 0.0
        self._lock = threading.Lock()
        self._facts_lock = threading.RLock()
        self._wake = threading.Event()
        self.store_path, self.store_cap = store_path, int(store_cap)
        self._warm = load_store(store_path, self.store_cap) if store_path else []
        self._warm_tries = 0
        self._unread = {}                            # coin -> stored row the chain never answered for
        self._saved = None
        self.stats = {"queued": 0, "learned": 0, "learn_failed": 0, "not_confirmed": 0, "not_learnable": 0,
                      "warm_rows": len(self._warm), "warm_loaded": 0, "warm_dropped": 0,
                      "warm_unread": 0, "warm_calls": 0, "warm_ms": 0.0, "rechecked": 0,
                      "recheck_same": 0, "repointed": 0, "recheck_failed": 0, "store_writes": 0,
                      "store_write_failed": 0}
        self.stats.update(creation_queued=0, creation_learned=0, creation_read_failed=0,
                          creation_dropped=0, cache_evicted=0, route_rows=None, route_scan_ok=None,
                          route_scan_failed=0, route_queued=0, route_loaded=0,
                          route_read_failed=0, route_dropped=0, route_refused=0,
                          route_suppressed=0, route_last_failure=None)
        if start:
            threading.Thread(target=self._run, name="slot-lane-damm-v2-learn", daemon=True).start()

    def _run(self):
        while True:
            self._wake.wait(self.LEARN_INTERVAL_S)
            self._wake.clear()
            try:
                if (self.route_targets_fn is not None or self._warm or self._pending
                        or self._recheck or self._creates or self._targets):
                    self.learn_pending()
            except Exception:
                import logging
                logging.getLogger(__name__).warning("damm v2 learn pass failed", exc_info=True)

    def get(self, coin):
        facts = self._by_coin.get(coin)
        if facts is not None:
            self._used[coin] = time.monotonic()  # one lock-free source lookup; no new entry lock or I/O
        return facts

    def offer_route_target(self, coin, pool, *, age_s=0.0):
        """Learner-only enqueue of an untrusted ledger hint. Never replace a source-bound pool."""
        if (not _is_address(coin) or not _is_address(pool) or isinstance(age_s, bool)
                or not isinstance(age_s, (int, float)) or not math.isfinite(age_s) or age_s < 0):
            return "invalid"
        if age_s > self.HOT_RETENTION_S:
            return "expired"
        active = time.monotonic() - age_s
        with self._facts_lock:  # background-only: serialize retention with MissLearner.adopt
            known = self._by_coin.get(coin)
            if known is not None:
                if known["pool"] == pool:
                    self._hot[coin] = max(active, self._hot.get(coin, active))
                return "known"
        blocked = self._route_blocked(pool, coin)
        if blocked:
            self.stats["route_suppressed"] += 1
            return blocked
        if coin in self._targets:
            return "pending"
        if len(self._targets) >= MAX_PENDING:
            self.stats["route_dropped"] += 1
            return "full"
        self._targets[coin] = [pool, active, 0, 0.0]
        self.stats["route_queued"] += 1
        return "queued"

    def _route_blocked(self, pool, coin):
        """Learner-only refusal/cooldown lookup; source learning never consults this map."""
        key = (pool, coin)
        if key not in self._route_failures:
            return None
        due = self._route_failures[key]
        if due is None:
            return "refused"
        if time.monotonic() < due:
            return "cooldown"
        del self._route_failures[key]
        return None

    def _route_scan(self):
        if self.route_targets_fn is None or time.monotonic() < self._next_route_scan:
            return
        self._next_route_scan = time.monotonic() + self.ROUTE_SCAN_INTERVAL_S
        try:
            rows = self.route_targets_fn()
            for row in rows[:MAX_PENDING]:
                self.offer_route_target(row["coin"], row["pool"], age_s=row["age_s"])
            self.stats["route_rows"], self.stats["route_scan_ok"] = len(rows), True
        except Exception:
            self.stats["route_scan_ok"] = False
            self.stats["route_scan_failed"] += 1
            import logging
            logging.getLogger(__name__).warning("damm v2 route hydration ledger read failed; source learning continues",
                                                exc_info=True)

    def _target_one(self):
        if not self._targets:
            return False
        coin, (pool, active, tries, due) = self._targets.popitem(last=False)
        if coin in self._by_coin or time.monotonic() - active > self.HOT_RETENTION_S:
            return False
        if time.monotonic() < due or self._route_blocked(pool, coin):
            self._targets[coin] = [pool, active, tries, due]
            return False
        try:
            facts = self.learn_pool(pool, coin, only_if_missing=True, active_at=active)
            if facts["pool"] == pool:
                self.stats["route_loaded"] += 1
                return True
        except Exception as exc:
            self.stats["route_read_failed"] += 1
            refused = isinstance(exc, DammV2PoolRefusal)
            retry = not refused and tries + 1 < WARM_TRIES
            self.stats["route_refused"] += int(refused)
            self.stats["route_last_failure"] = {
                "kind": "permanent" if refused else "unread",
                "reason": str(exc) if isinstance(exc, (DammV2PoolRefusal, _Unread)) else type(exc).__name__}
            key = (pool, coin)
            self._route_failures[key] = (None if refused else time.monotonic() +
                (self.ROUTE_RETRY_AFTER_S if retry else self.ROUTE_COOLDOWN_S))
            self._route_failures.move_to_end(key)
            while len(self._route_failures) > self.store_cap:
                self._route_failures.popitem(last=False)
            if retry:
                self._targets[coin] = [pool, active, tries + 1, self._route_failures.get(key, 0.0)]
            else:
                self.stats["route_dropped"] += 1
        return False

    def offer_creation(self, signature):
        """Enqueue only; the stream never reads chain or touches the funded ledger."""
        import base58
        try:
            if (not isinstance(signature, str) or not 64 <= len(signature) <= 88
                    or len(base58.b58decode(signature)) != 64):
                return "invalid"
        except ValueError:
            return "invalid"
        with self._lock:
            if signature in self._creates:
                return "pending"
            if len(self._creates) >= MAX_PENDING:
                self.stats["creation_dropped"] += 1
                return "full"
            self._creates[signature] = 0
            self.stats["creation_queued"] += 1
        self._wake.set()
        return "queued"

    def _rank(self, coin, slot, now):
        active, used = self._hot.get(coin), self._used.get(coin)
        if used is not None:
            active = used if active is None else max(active, used)
        hot = active is not None and now - active <= self.HOT_RETENTION_S
        return (hot, active if hot else 0, slot or 0)

    def _remember(self, coin, facts, *, only_if_missing=False, active_at=None):
        # Two writers since the first-sight learner (adopt, on the MissLearner thread): the eviction
        # scan iterates the dict, so every write holds the facts lock. `get` stays lock-free.
        with self._facts_lock:
            if only_if_missing and coin in self._by_coin:
                return False
            if active_at is not None:
                self._hot[coin] = max(active_at, self._hot.get(coin, active_at))
            self._by_coin[coin] = facts
            if len(self._by_coin) > self.store_cap:
                now = time.monotonic()
                oldest = min(self._by_coin, key=lambda c: self._rank(c, self._by_coin[c].get("learn_slot"), now))
                del self._by_coin[oldest]
                self.stats["cache_evicted"] += 1
            for stamps in (self._hot, self._used):
                for old in list(stamps):
                    if old not in self._by_coin:
                        stamps.pop(old, None)
            return True

    def adopt(self, coin, facts):
        """A pool found BY MINT on a first sighting (DammV2MintLearner, MissLearner thread). It goes in
        as the newest entry (its learn_slot ties the newest), so the next eviction never takes it. The
        learner's own queue still reads the source transaction and stores the row as usual."""
        with self._facts_lock:              # one acquire (RLock): nothing can write in between
            newest = max((f.get("learn_slot") or 0 for f in self._by_coin.values()), default=0)
            self._remember(coin, {**facts, "learn_slot": facts.get("learn_slot") or newest},
                           active_at=time.monotonic())

    def _creation_one(self):
        """At most one creation per learner turn. Unknown reads retry within a bounded queue.
        A creation never replaces a pool already selected by a copied source swap."""
        with self._lock:
            if not self._creates:
                return False
            signature, tries = next(iter(self._creates.items()))
        changed, retry = False, False
        try:
            tx = self._transaction(signature)
            if tx is None:
                retry = True
            else:
                for pool, mint_a, mint_b in created_pools_from_transaction(tx):
                    # Prefer the coin side of SOL/USDC; for exotic pairs warm both possible sides.
                    coins = [m for m in (mint_a, mint_b) if m not in (WSOL_MINT, USDC_MINT)]
                    missing = [coin for coin in coins if coin not in self._by_coin]
                    if not missing:
                        continue
                    facts = self.learn_pool(pool, missing[0], slot=tx["slot"],
                                            expected_mints=(mint_a, mint_b))
                    for coin in missing[1:]:
                        # The same two verified mint accounts; invert their coin/quote roles.
                        other = dict(facts, coin=coin, quote=missing[0],
                                     coin_facts=facts["other_coin_facts"],
                                     quote_facts=facts["other_quote_facts"],
                                     coin_refusal=facts["quote_refusal"],
                                     quote_refusal=facts["coin_refusal"],
                                     other_coin_facts=facts["coin_facts"],
                                     other_quote_facts=facts["quote_facts"],
                                     quote_program=(facts["program_a"] if mint_a == missing[0]
                                                    else facts["program_b"]))
                        self._remember(coin, other)
                    self.stats["creation_learned"] += 1
                    changed = True
        except Exception:
            self.stats["creation_read_failed"] += 1
            retry = True
        with self._lock:
            if retry and tries + 1 < LEARN_TRIES:
                self._creates[signature] = tries + 1
                self._creates.move_to_end(signature)
            else:
                self._creates.pop(signature, None)
                if retry:
                    self.stats["creation_dropped"] += 1
        return changed

    def queue(self, signature, coin):
        """'queued', or why not: 'known', 'pending' (a learn is in flight) or 'full'."""
        with self._lock:
            if coin in self._by_coin:
                return "known"
            if coin in self._pending:
                return "pending"
            if len(self._pending) >= MAX_PENDING:
                return "full"
            self._pending[coin] = [signature, 0]
            self.stats["queued"] += 1
        self._wake.set()
        return "queued"

    def recheck(self, signature, coin):
        """A later cp-amm routed buy of a KNOWN coin: queue its signature for the learner to check
        which pool it swapped on, RECHECK_AFTER_S from now (the learner is not woken). No I/O; one in
        flight per coin; any thread."""
        import time
        with self._lock:
            if coin in self._recheck or len(self._recheck) >= MAX_PENDING:
                return
            self._recheck[coin] = [signature, 0, time.monotonic() + self.RECHECK_AFTER_S]

    def learn_pending(self):
        """One learner pass: one recent ledger target, one creation, one stored batch, queued first
        sightings and one recheck; then the store if its rows changed. Ledger scans run once/minute."""
        self._route_scan()
        changed = self._target_one()
        changed = self._creation_one() or changed
        changed = (self._warm_batch() if self._warm else False) or changed
        with self._lock:
            todo = list(self._pending.items())
        for coin, (signature, tries) in todo:
            if coin in self._by_coin:                 # warmed while it waited
                with self._lock:
                    self._pending.pop(coin, None)
                continue
            try:
                learned = self.learn_from_signature(signature, coin)
            except Exception:
                learned = False
                self.stats["learn_failed"] += 1
                import logging
                logging.getLogger(__name__).warning("damm v2 pool learn failed coin=%s", coin,
                                                    exc_info=True)
            changed = changed or learned is True
            with self._lock:
                if learned is None and tries + 1 < LEARN_TRIES:
                    self._pending[coin] = [signature, tries + 1]     # not confirmed yet: retry
                else:
                    self._pending.pop(coin, None)
        changed = self._recheck_one() or changed
        if changed:
            self._save()

    def learn_from_signature(self, signature, coin):
        """True learned, False not a learnable DAMM v2 buy, None not confirmed yet."""
        tx = self._transaction(signature)
        if not tx:
            self.stats["not_confirmed"] += 1
            return None
        pool = pool_from_transaction(tx, coin)
        if pool is None:
            self.stats["not_learnable"] += 1
            return False
        return self.learn_pool(pool, coin, slot=tx.get("slot"), active_at=time.monotonic()) is not None

    def _transaction(self, signature):
        from sniper.execution.pump_native_exit import _rpc_result
        return _rpc_result(self.rpc, "getTransaction", [signature, {
            "encoding": "json", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}])

    def learn_pool(self, pool, coin, slot=None, expected_mints=None, *, only_if_missing=False,
                   active_at=None):
        """Read and verify the pool and both mints. Complete identity mismatches raise
        DammV2PoolRefusal; missing, short or malformed answers remain _Unread."""
        from sniper.execution.pump_native_exit import _rpc_result
        try:
            answer = _rpc_result(self.rpc, "getAccountInfo", [pool, {"encoding": "base64", "commitment": "confirmed"}])
        except Exception as exc:
            raise _Unread(type(exc).__name__) from exc
        if not isinstance(answer, dict) or "value" not in answer:
            raise _Unread("pool_reply_unreadable")
        value = answer["value"]
        if value is None:
            raise _Unread("pool_missing")
        if not isinstance(value, dict) or not _is_address(value.get("owner")):
            raise _Unread("pool_owner_unreadable")
        if value["owner"] != PROGRAM:
            raise DammV2PoolRefusal("wrong_program")
        try:
            data = value["data"]
            if data[1] != "base64":
                raise ValueError("not base64")
            raw = base64.b64decode(data[0], validate=True)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise _Unread("pool_data_unreadable") from exc
        if len(raw) < POOL_LEN:
            raise _Unread("pool_data_short")
        if raw[:8] != POOL_DISCRIMINATOR:
            raise DammV2PoolRefusal("wrong_layout")
        state = parse_pool(raw)
        if expected_mints is not None and (state["mint_a"], state["mint_b"]) != expected_mints:
            raise DammV2PoolRefusal("creation_mints_mismatch")
        if coin not in (state["mint_a"], state["mint_b"]):
            raise DammV2PoolRefusal("coin_not_in_pool")
        mints = self._multiple([state["mint_a"], state["mint_b"]], "jsonParsed")
        if not all(isinstance(mint, dict) and mint for mint in mints):
            raise _Unread("mints_unreadable")
        try:
            facts = self._facts(pool, coin, state, mints[0], mints[1], slot)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise _Unread("mints_unreadable") from exc
        if not self._remember(coin, facts, only_if_missing=only_if_missing, active_at=active_at):
            return self._by_coin[coin]
        self.stats["learned"] += 1
        return facts

    @staticmethod
    def _facts(pool, coin, state, mint_a_value, mint_b_value, slot, read_at=None):
        """The facts a build signs with, from the pool's state and both mint accounts as read now.
        `read_at` (monotonic, the mints' read START) also hands the quote mint's value to the live leg
        worker (ondemand_facts), which then re-reads it only past QUOTE_RECHECK_S."""
        from sniper.execution.raydium_cpmm_buy import coin_facts, coin_refusal, quote_facts
        coin_value, quote_value = ((mint_a_value, mint_b_value) if state["mint_a"] == coin
                                   else (mint_b_value, mint_a_value))
        quote = state["mint_b"] if state["mint_a"] == coin else state["mint_a"]
        stamp = {} if read_at is None else {"quote_read_at": read_at, "mint_values": {quote: quote_value}}
        return {**state, "pool": pool, "coin": coin, "quote": quote, **stamp,
                "program_a": mint_a_value.get("owner"), "program_b": mint_b_value.get("owner"),
                "coin_facts": coin_facts(coin_value), "coin_refusal": coin_refusal(coin_value),
                "other_coin_facts": coin_facts(quote_value), "other_quote_facts": quote_facts(coin_value),
                "quote_refusal": coin_refusal(quote_value),
                # E3: the quote side, for a pool quoted in anything but SOL (None = unmovable).
                "quote_facts": quote_facts(quote_value), "quote_program": quote_value.get("owner"),
                "learn_slot": slot}

    def _multiple(self, addresses, encoding):
        """getMultipleAccounts values, one per address, or _Unread."""
        from sniper.execution.pump_native_exit import _rpc_result
        try:
            values = (_rpc_result(self.rpc, "getMultipleAccounts", [list(addresses), {
                "encoding": encoding, "commitment": "confirmed"}]) or {}).get("value")
        except Exception as exc:
            raise _Unread(type(exc).__name__) from exc
        if not isinstance(values, list) or len(values) != len(addresses):
            raise _Unread("short answer")
        return values

    def _warm_batch(self):
        """Re-read the newest stored batch (<= 100 pools, then their mints) and load each row that
        passes learn_pool's checks. Returns whether the store's rows changed (a row dropped)."""
        import time
        batch = self._warm[-_MULTIPLE_MAX:]
        started, calls, dropped, loaded = time.monotonic(), 0, 0, 0
        try:
            calls += 1
            accounts = self._multiple([r["pool"] for r in batch], "base64")
            states = {}
            for row, value in zip(batch, accounts):
                try:
                    if not value or value.get("owner") != PROGRAM:
                        raise ValueError("not a DAMM v2 pool")
                    state = parse_pool(base64.b64decode(value["data"][0]))
                    if row["coin"] not in (state["mint_a"], state["mint_b"]):
                        raise ValueError("pool does not hold the coin")
                    states[row["coin"]] = state
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    dropped += 1
            wanted = list(dict.fromkeys(m for st in states.values() for m in (st["mint_a"], st["mint_b"])))
            mints = {}
            for i in range(0, len(wanted), _MULTIPLE_MAX):
                calls += 1
                chunk = wanted[i:i + _MULTIPLE_MAX]
                mints.update(zip(chunk, self._multiple(chunk, "jsonParsed")))
        except _Unread:
            self.stats["warm_calls"] += calls
            self._warm_tries += 1
            if self._warm_tries >= WARM_TRIES:       # wait for the next start; the row stays stored
                for row in batch:
                    self._unread[row["coin"]] = row
                del self._warm[-len(batch):]
                self._warm_tries = 0
                self.stats["warm_unread"] += len(batch)
            return False
        for row in batch:
            state = states.get(row["coin"])
            if state is None:
                continue
            known = self._by_coin.get(row["coin"])
            if known is not None and (known.get("learn_slot") or 0) >= row["slot"]:
                continue                              # learned live since the start, and newer
            try:
                a, b = mints.get(state["mint_a"]), mints.get(state["mint_b"])
                if not a or not b:
                    raise ValueError("mint gone")
                self._remember(row["coin"], self._facts(row["pool"], row["coin"], state, a, b, row["slot"]))
                loaded += 1
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                dropped += 1
        del self._warm[-len(batch):]
        self._warm_tries = 0
        self.stats["warm_calls"] += calls
        self.stats["warm_loaded"] += loaded
        self.stats["warm_dropped"] += dropped
        self.stats["warm_ms"] += (time.monotonic() - started) * 1000.0
        return dropped > 0

    def _recheck_one(self):
        """Check ONE queued later buy of a known coin; re-learn its pool when it moved. Returns whether
        the cache changed."""
        import logging
        import time
        with self._lock:
            due = [c for c, entry in self._recheck.items() if entry[2] <= time.monotonic()]
            if not due:
                return False
            coin = due[0]
            signature, tries, _due_at = self._recheck.pop(coin)
        known = self._by_coin.get(coin)
        try:
            tx = self._transaction(signature)
            if not tx:
                if tries + 1 < LEARN_TRIES:
                    with self._lock:                  # not confirmed yet: the next pass reads again
                        self._recheck.setdefault(coin, [signature, tries + 1, time.monotonic()])
                return False
            self.stats["rechecked"] += 1
            pool, slot = pool_from_transaction(tx, coin), tx.get("slot")
            if pool is None or known is None:
                return False
            if pool == known["pool"]:
                self.stats["recheck_same"] += 1
                return False
            if not isinstance(slot, int) or slot < (known.get("learn_slot") or 0):
                return False                          # an older transaction never undoes a newer learn
            facts = self.learn_pool(pool, coin, slot=slot)
        except Exception:
            self.stats["recheck_failed"] += 1
            logging.getLogger(__name__).warning("damm v2 pool recheck failed coin=%s sig=%s", coin,
                                                signature, exc_info=True)
            return False
        self.stats["repointed"] += 1
        logging.getLogger(__name__).warning(
            "[memecoin] slot lane damm_v2 pool repointed %s %s -> %s slot=%s sig=%s", coin[:8],
            known["pool"][:8], facts["pool"][:8], slot, signature)
        return True

    def _save(self):
        """Write the store's rows (learned, still to warm, never answered), newest `store_cap` by learn
        slot, when they changed. tmp + os.replace; a failed write keeps the cache and is counted."""
        import logging
        import os
        if not self.store_path:
            return
        best = {}
        learned = [{"coin": coin, "pool": f["pool"], "slot": f.get("learn_slot")}
                   for coin, f in list(self._by_coin.items())]
        for row in list(self._warm) + list(self._unread.values()) + learned:
            if isinstance(row["slot"], bool) or not isinstance(row["slot"], int):
                continue                              # a learn with no slot is not stored
            if row["coin"] not in best or row["slot"] > best[row["coin"]]["slot"]:
                best[row["coin"]] = row
        now = time.monotonic()
        rows = sorted(best.values(), key=lambda r: self._rank(r["coin"], r["slot"], now))[-self.store_cap:]
        rows.sort(key=lambda r: r["slot"])
        if rows == self._saved:
            return
        tmp = "%s.tmp" % self.store_path
        try:
            with open(tmp, "w") as fh:
                json.dump(rows, fh)
            os.replace(tmp, self.store_path)
        except OSError:
            self.stats["store_write_failed"] += 1
            logging.getLogger(__name__).warning("damm v2 pool store write failed path=%s",
                                                self.store_path, exc_info=True)
            return
        self._saved = rows
        self.stats["store_writes"] += 1


MINT_SLICE = (OFF_MINT_A, 128)       # mint_a, mint_b, vault_a, vault_b: contiguous in the Pool layout


def mint_learner_rpc(url, *, timeout_s=0.5, funded_purpose=False, env=None):
    """The first-sight learner's OWN client (never the learner's sleeping pool_rpc closure, never the
    custody RPC). Refuses a URL that carries the funded custody key."""
    from sniper.execution.raydium_launchlab_buy import http_rpc
    from sniper.services.memecoin_slot_signal import _shares_funded_key
    if funded_purpose:
        from sniper.services.memecoin_research_rpc import funded_dependency_url
        if url != funded_dependency_url("pool_first_sight", env):
            raise ValueError("funded first-sight read must match the explicit neutral endpoint")
    if not url or (_shares_funded_key(url) and not funded_purpose):
        raise ValueError("the DAMM v2 first-sight read needs its own non-funded endpoint")
    return http_rpc(url, timeout_s=timeout_s)


def find_pool_by_mint(rpc, coin, *, sol_usd, min_depth_usd, pool_map=map):
    """(facts, None) for the deepest SOL pool holding `coin` at or above `min_depth_usd` (USDC pools
    too when non-SOL DAMM is listed), or (None, reason): no_pool, no_sol_pool, shallow,
    exotic_ambiguous, sol_usd_unknown, unread (a raised, error or short answer: unknown, never
    "no pool").
    EXOTIC QUOTES (operator order 2026-10-08, LILMET D75KtdgF: its pool was MET-quoted, its SOL pool
    $1 deep). While non-SOL DAMM is listed, a pool quoted in any other mint the on-demand leg can move
    (quote_facts, no transfer fee: the worker's own test) is a candidate too. A priced pool (SOL, USDC)
    at or above the floor still wins. Otherwise the deepest pool of the ONE exotic quote mint is taken
    UNPRICED (depth_usd None, depth_quote_atomic): the learner fetches no price, so market_facts judges
    the floor once the leg prices the quote. Two exotic quote mints cannot be compared: exotic_ambiguous.
    Reads: getProgramAccounts memcmp on mint_a and on mint_b (`pool_map` runs them, in parallel on the
    learner), then ONE getMultipleAccounts of the candidate pools, their quote vaults and both mints.
    The facts are learn_pool's (_facts, with the quote mint's value and read start), plus depth_usd."""
    import base64
    from sniper.execution.pump_native_exit import _rpc_result
    from sniper.execution.raydium_cpmm_buy import epoch_now, quote_facts
    from solders.pubkey import Pubkey
    exotic_ok = non_sol_listed()
    quotes = (WSOL_MINT, USDC_MINT) if exotic_ok else (WSOL_MINT,)

    def scan(offset):
        return _rpc_result(rpc, "getProgramAccounts", [PROGRAM, {
            "encoding": "base64", "commitment": "confirmed",
            "dataSlice": {"offset": MINT_SLICE[0], "length": MINT_SLICE[1]},
            "filters": [{"dataSize": POOL_LEN}, {"memcmp": {"offset": offset, "bytes": coin}}]}])
    try:
        answers = list(pool_map(scan, (OFF_MINT_A, OFF_MINT_B)))
    except Exception:
        return None, "unread"
    if any(not isinstance(a, list) for a in answers):
        return None, "unread"
    seen, candidates = set(), []
    for answer in answers:
        for row in answer:
            try:
                raw = base64.b64decode(row["account"]["data"][0])
                key = lambda o: str(Pubkey.from_bytes(raw[o:o + 32]))      # noqa: E731
                mint_a, mint_b, vault_a, vault_b = key(0), key(32), key(64), key(96)
            except (KeyError, IndexError, TypeError, ValueError):
                return None, "unread"
            if row.get("pubkey") in seen or coin not in (mint_a, mint_b):
                continue
            seen.add(row.get("pubkey"))
            quote, vault = (mint_b, vault_b) if mint_a == coin else (mint_a, vault_a)
            if quote in quotes or exotic_ok:
                candidates.append((row["pubkey"], quote, vault))
    if not seen:
        return None, "no_pool"
    if not candidates:
        return None, "no_sol_pool"
    if sol_usd is None or not sol_usd > 0:
        return None, "sol_usd_unknown"
    # Priced (SOL, USDC) first, then the cap: spam exotic pools never cut a priced one (Gamma MEDIUM).
    # 2 x 30 + the coin + at most 30 quote mints: one getMultipleAccounts (100 max).
    candidates = sorted(candidates, key=lambda c: c[1] not in (WSOL_MINT, USDC_MINT))[:30]
    addresses = ([c[0] for c in candidates] + [c[2] for c in candidates]
                 + [coin] + sorted({c[1] for c in candidates}))
    read_at = time.monotonic()                 # read start: a slow answer never extends freshness
    try:
        values = (_rpc_result(rpc, "getMultipleAccounts", [addresses, {
            "encoding": "jsonParsed", "commitment": "confirmed"}]) or {}).get("value")
    except Exception:
        return None, "unread"
    if not isinstance(values, list) or len(values) != len(addresses):
        return None, "unread"
    by = dict(zip(addresses, values))
    best, exotic = None, {}                    # exotic: quote mint -> (pool, quote atomic)
    for pool, quote, vault in candidates:
        try:
            amount = int(by[vault]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        except (KeyError, TypeError, ValueError):
            continue
        if quote not in (WSOL_MINT, USDC_MINT):
            moved = quote_facts(by.get(quote), epoch=epoch_now())
            if moved is not None and not moved[1] and not moved[2] and amount > exotic.get(quote, (None, -1))[1]:
                exotic[quote] = (pool, amount)
            continue
        depth = amount / 1e9 * float(sol_usd) if quote == WSOL_MINT else amount / 1e6
        if best is None or depth > best[1]:
            best = ((pool, quote), depth)
    depth_quote = None
    if best is None or best[1] < float(min_depth_usd):
        if not exotic:
            return None, "shallow"
        if len(exotic) > 1:
            return None, "exotic_ambiguous"
        ((quote, (pool, depth_quote)),) = exotic.items()
        best = ((pool, quote), None)
    (pool, quote), depth = best
    value = by.get(pool)
    try:
        if not value or value.get("owner") != PROGRAM:
            return None, "unread"
        state = parse_pool(base64.b64decode(value["data"][0]))
    except (KeyError, IndexError, TypeError, ValueError):
        return None, "unread"
    if coin not in (state["mint_a"], state["mint_b"]) or not by.get(coin) or not by.get(quote):
        return None, "unread"
    mint_a_value, mint_b_value = ((by[coin], by[quote]) if state["mint_a"] == coin
                                  else (by[quote], by[coin]))
    facts = DammV2PoolCache._facts(pool, coin, state, mint_a_value, mint_b_value, None, read_at=read_at)
    if depth_quote is not None:
        facts["depth_quote_atomic"] = depth_quote
    return {**facts, "depth_usd": depth}, None


class DammV2MintLearner:
    """The MissLearner's venue cache for a DAMM v2 first sighting: its key is the COIN (the frame names
    no pool). `learn(coin)` finds the pool by mint and adopts it into the lane's DammV2PoolCache.
    Runs on the MissLearner's own venue thread; two reads in parallel on a two-worker pool."""

    def __init__(self, pools, rpc, *, sol_usd_fn, min_depth_usd, log=None):
        from concurrent.futures import ThreadPoolExecutor
        self.pools, self.rpc, self.sol_usd_fn = pools, rpc, sol_usd_fn
        self.min_depth_usd, self.log = float(min_depth_usd), log
        self._exec = ThreadPoolExecutor(max_workers=2, thread_name_prefix="damm-first-sight")
        self.stats = {"found": 0, "refused": {}}

    def learn(self, coin, read_hint=None):
        if (isinstance(read_hint, DammV2ReadHint) and read_hint.coin == coin
                and coin in (read_hint.mint_a, read_hint.mint_b)
                and (read_hint.mint_b if coin == read_hint.mint_a else read_hint.mint_a)
                    in ((WSOL_MINT, USDC_MINT) if non_sol_listed() else (WSOL_MINT,))):
            facts, why = find_pool_by_hint(self.rpc, read_hint, sol_usd=self.sol_usd_fn(),
                                          min_depth_usd=self.min_depth_usd)
        else:
            facts, why = find_pool_by_mint(self.rpc, coin, sol_usd=self.sol_usd_fn(),
                                           min_depth_usd=self.min_depth_usd, pool_map=self._exec.map)
        if facts is None:
            self.stats["refused"][why] = self.stats["refused"].get(why, 0) + 1
            if self.log is not None:
                self.log("[memecoin] slot lane damm_v2 first sight %s refused %s" % (str(coin)[:8], why))
            return False
        self.pools.adopt(coin, facts)
        self.stats["found"] += 1
        if self.log is not None:
            depth = ("depth_usd=%.0f" % facts["depth_usd"] if facts["depth_usd"] is not None
                     else "depth=unpriced quote_atomic=%d" % facts["depth_quote_atomic"])
            self.log("[memecoin] slot lane damm_v2 first sight %s pool=%s quote=%s %s" % (
                str(coin)[:8], str(facts["pool"])[:8], str(facts["quote"])[:8], depth))
        return True


def find_pool_by_hint(rpc, hint, *, sol_usd, min_depth_usd):
    """One background GMA; source addresses must match canonical pool, mints and both vaults."""
    from sniper.execution.pump_native_exit import _rpc_result
    addresses = [hint.pool, hint.mint_a, hint.mint_b, hint.vault_a, hint.vault_b]
    read_at = time.monotonic()
    try:
        answer = _rpc_result(rpc, 'getMultipleAccounts', [addresses, {
            'encoding': 'jsonParsed', 'commitment': 'confirmed'}])
        values = answer.get('value')
        if not isinstance(values, list) or len(values) != 5 or not all(isinstance(v, dict) for v in values):
            return None, 'unread'
        pool, mint_a, mint_b, vault_a, vault_b = values
        if pool.get('owner') != PROGRAM:
            return None, 'wrong_program'
        if pool['data'][1] != 'base64':
            return None, 'unread'
        state = parse_pool(base64.b64decode(pool['data'][0], validate=True))
        if any(state[key] != getattr(hint, key) for key in ('mint_a', 'mint_b', 'vault_a', 'vault_b')):
            return None, 'hint_identity_mismatch'
        if mint_a.get('owner') != hint.program_a or mint_b.get('owner') != hint.program_b:
            return None, 'mint_program_mismatch'
        amounts = []
        for value, mint, program in ((vault_a, hint.mint_a, hint.program_a),
                                      (vault_b, hint.mint_b, hint.program_b)):
            parsed = value['data']['parsed']
            info = parsed['info']
            if (value.get('owner') != program or parsed.get('type') != 'account'
                    or info.get('mint') != mint or info.get('owner') != POOL_AUTHORITY):
                return None, 'vault_identity_mismatch'
            amount = info['tokenAmount']['amount']
            if (not isinstance(amount, str) or not amount or not amount.isascii()
                    or not amount.isdigit() or len(amount) > 20 or int(amount) >= 2**64):
                return None, 'unread'
            amounts.append(int(amount))
        quote = hint.mint_b if hint.coin == hint.mint_a else hint.mint_a
        quote_amount = amounts[1 if hint.coin == hint.mint_a else 0]
        facts = DammV2PoolCache._facts(hint.pool, hint.coin, state, mint_a, mint_b, None, read_at=read_at)
        if facts['quote_facts'] != ((9 if quote == WSOL_MINT else 6), 0, 0):
            return None, 'quote_unreadable'
        if sol_usd is None or not sol_usd > 0 or not math.isfinite(sol_usd):
            return None, 'sol_usd_unknown'
        depth = quote_amount / 1e9 * float(sol_usd) if quote == WSOL_MINT else quote_amount / 1e6
        if depth < float(min_depth_usd):
            return None, 'shallow'
        return {**facts, 'depth_usd': depth}, None
    except Exception:
        return None, 'unread'


def damm_slot_buy(routed, pools):
    """(SlotBuy, None) for a routed buy whose frame ran cp-amm and whose coin's pool is cached;
    (None, "pool_unknown:<queue state>") when it ran cp-amm but the pool is not known yet (the first
    sighting is queued for learning); (None, None) when cp-amm did not run. No I/O."""
    from sniper.services.memecoin_slot_signal import SlotBuy
    if pools is None or not any(isinstance(e, DammV2Seen) for e in getattr(routed, "venue_events", ()) or ()):
        return None, None
    facts = pools.get(routed.mint)
    if facts is None:
        return None, "pool_unknown:%s" % pools.queue(routed.signature, routed.mint)
    recheck = getattr(pools, "recheck", None)
    if recheck is not None:
        recheck(routed.signature, routed.mint)    # the learner checks it swapped on this pool (no I/O)
    if facts.get("coin_refusal"):
        # THE ONLY TOKEN-2022 GUARD ON THIS PATH (Gamma MEDIUM on 87a4a017c): the slot lane runs no
        # token-safety simulation, so a coin with an extension the builds do not cover (permanent
        # delegate: seizable; transfer hook: gated; pausable) is handed off, never built.
        return None, "coin_" + str(facts["coin_refusal"])
    return SlotBuy(venue="meteora_damm_v2", source_user_id=routed.source_user_id,
                   handle=routed.handle, owner=routed.owner, signature=routed.signature,
                   slot=routed.slot, mint=routed.mint, pool=facts["pool"],
                   sol_amount=int(routed.usdc_spent_atomic), token_amount=int(routed.token_delta_atomic),
                   virtual_sol_reserves=None, virtual_token_reserves=None, real_sol_reserves=0,
                   real_token_reserves=0, token_program=None, trade_event_log="",
                   recv_monotonic=routed.recv_monotonic, recv_utc=routed.recv_utc,
                   signal_feed=getattr(routed, "signal_feed", "helius"), routed=routed,
                   quote_mint=facts["quote"]), None


def source_price_usd(routed, decimals):
    """The source's own average fill, USD per whole token (USDC spent / tokens received), or None."""
    try:
        usd = int(routed.usdc_spent_atomic) / 1e6
        tokens = int(routed.token_delta_atomic) / 10 ** int(decimals)
        price = usd / tokens
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None
    return price if price > 0 else None


def market_facts(buy, pools, sol_usd, quote=None):
    """(reason, facts) for a DAMM v2 slot buy from the cache and the routed buy only (no network).
    The price is the source's routed average whatever the pool's quote. A non-SOL pool needs E3's
    listing; USDC then needs nothing more, any other quote needs the on-demand leg's `quote`
    (without it: REASON_PREFIX_quote_not_supported, the worker's cue)."""
    facts = pools.get(buy.mint) if pools is not None else None
    if facts is None:
        return "pool_unknown", {}
    if facts["quote"] != WSOL_MINT and (
            not non_sol_listed() or (facts["quote"] != USDC_MINT and quote is None)):
        return REASON_PREFIX + "_quote_not_supported", {}
    if facts.get("coin_refusal"):
        return "%s_coin_%s" % (REASON_PREFIX, facts["coin_refusal"]), {}
    if facts.get("depth_quote_atomic") is not None and quote is not None:
        # A pool first sight took UNPRICED: its depth floor, now that the leg prices the quote.
        from sniper.settings import memecoin as cfg
        try:
            depth_usd = int(facts["depth_quote_atomic"]) / 10 ** int(quote[0]) * float(quote[1])
        except (TypeError, ValueError, OverflowError):
            depth_usd = None
        if (depth_usd is None or not math.isfinite(depth_usd)
                or not depth_usd >= float(cfg.MEMECOIN_SLOT_LANE_DAMM_V2_MISS_MIN_DEPTH_USD)):
            return REASON_PREFIX + "_shallow", {}
    coin = facts.get("coin_facts")
    routed = getattr(buy, "routed", None)
    if coin is None or routed is None:
        return "supply_unknown", {}
    supply, decimals, _bps, _max = coin
    price = source_price_usd(routed, decimals)
    if price is None:
        return "slot_lane_price_unreadable", {}
    return None, {"price_usd": price, "mcap_usd": price * supply / 10 ** decimals,
                  "their_usd": int(routed.usdc_spent_atomic) / 1e6, "mint": buy.mint,
                  "pool": facts["pool"], "their_usd_basis": "routed_usdc_spent"}


def swap_instruction(*, user, facts, input_account, output_account, amount_in, minimum_out):
    """The cp-amm swap instruction: (amount_in, minimum_amount_out) in the 14 accounts of the
    verified layout. No referral: the optional account is the program id."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey
    pk = Pubkey.from_string
    metas = [
        AccountMeta(pk(POOL_AUTHORITY), False, False),
        AccountMeta(pk(facts["pool"]), False, True),
        AccountMeta(pk(str(input_account)), False, True),
        AccountMeta(pk(str(output_account)), False, True),
        AccountMeta(pk(facts["vault_a"]), False, True),
        AccountMeta(pk(facts["vault_b"]), False, True),
        AccountMeta(pk(facts["mint_a"]), False, False),
        AccountMeta(pk(facts["mint_b"]), False, False),
        AccountMeta(pk(str(user)), True, False),
        AccountMeta(pk(facts["program_a"]), False, False),
        AccountMeta(pk(facts["program_b"]), False, False),
        AccountMeta(pk(PROGRAM), False, False),
        AccountMeta(pk(EVENT_AUTHORITY), False, False),
        AccountMeta(pk(PROGRAM), False, False),
    ]
    data = SWAP_DISCRIMINATOR + struct.pack("<QQ", int(amount_in), int(minimum_out))
    return Instruction(pk(PROGRAM), data, metas)


class SlotLaneDammV2Builder:
    """The slot lane's DAMM v2 builder; the same interface as the other venue builders. A SOL pool
    spends the PumpSwap builder's prefetched USDC -> SOL leg (output left in the wSOL ATA, as the CPMM
    builder does). `build` makes no network call; `refresh` (prefetch thread) learns queued coins."""

    REASON_PREFIX = REASON_PREFIX

    def __init__(self, *, user, rpc, leg_fn, max_leg_age_s=None, compute_unit_limit=None,
                 priority_fee_microlamports=None, clock=None, start_learner=True, pool_store_path=None,
                 pool_rpc=None, route_targets_fn=None):
        import time
        from sniper.execution.pump_curve_buy import DEFAULT_COMPUTE_UNIT_LIMIT, MAX_LEG_AGE_S
        self.user = str(user)
        self.rpc = rpc
        self.pools = DammV2PoolCache(rpc=pool_rpc or rpc, start=start_learner, store_path=pool_store_path,
                                   route_targets_fn=route_targets_fn)
        self.leg_fn = leg_fn
        self.max_leg_age_s = float(MAX_LEG_AGE_S if max_leg_age_s is None else max_leg_age_s)
        self.compute_unit_limit = int(compute_unit_limit or DEFAULT_COMPUTE_UNIT_LIMIT)
        self.priority_fee_microlamports = priority_fee_microlamports
        self.clock = clock or time.monotonic
        self.last_build = None
        self._alt_cache = {}                   # shared on-demand leg worker, per builder

    def refresh(self, clip_usd, snapshot=None):
        return True                           # the pool cache learns on its own thread

    @property
    def read_rpc(self):
        """The on-demand worker's reads (quote mint re-check, leg lookup tables): `rpc`, the custody
        RPC, as for every other exotic venue. Never the pool cache's rpc: in the lane that is the
        learner's own closure, paced 0.5 s per call (LILMET 2026-10-08: a cold MET leg 544 ms)."""
        return self.rpc

    def ondemand_facts(self, buy):
        """The coin's facts: their quote mint value and read start, when the learn stamped them."""
        return self.pools.get(getattr(buy, "mint", None)) or {}

    def ondemand_quote(self, buy):
        """(quote mint, quote facts or None, refusal or None) for the on-demand leg worker. No read:
        a DAMM pool is learned from a first sighting only, and the live worker re-reads the mint."""
        facts = self.pools.get(getattr(buy, "mint", None))
        if facts is None:
            return None, None, "pool_unknown"
        return facts["quote"], facts.get("quote_facts"), None

    def _non_sol_input(self, pool, clip_usd, leg, quote):
        """(leg-shaped funding, input account) for a non-SOL pool, or the refusal. USDC: the clip from
        the USDC account (no leg). Any other quote: the on-demand leg's minimum from its own account."""
        from sniper.execution.pump_curve_buy import clip_atomic, leg_clip_refusal
        from sniper.execution.pump_native_exit import TOKEN_PROGRAM, _pk, ata
        user = _pk(self.user)
        if pool["quote"] == USDC_MINT:
            spend = clip_atomic(clip_usd)
            return ({"ixs": [], "tables": [], "usdc_atomic": spend, "min_out_lamports": spend,
                     "fetched_at": None}, ata(user, _pk(USDC_MINT), _pk(TOKEN_PROGRAM)))
        if leg.get("output_mint") != pool["quote"]:
            return "leg_wrong_quote"
        if self.clock() - leg["fetched_at"] > self.max_leg_age_s:
            return "leg_stale"
        refused = leg_clip_refusal(leg, clip_usd)
        if refused:
            return refused
        if not pool.get("quote_program"):
            return "quote token program unknown"
        return leg, ata(user, _pk(pool["quote"]), _pk(pool["quote_program"]))

    accepts_priority_lamports = True     # memecoin_slot_lane.dispatch_build passes the crowded fee

    def build(self, buy, clip_usd, snapshot, *, funding_lamports=None, leg=None, quote=None, tip=None,
              priority_lamports=None):
        """(unsigned MessageV0, validity) or (None, reason). `leg` and `quote` come from the on-demand
        worker for an exotic quote (E3); a USDC pool needs neither."""
        from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import MessageV0
        from solders.null_signer import NullSigner
        from solders.transaction import VersionedTransaction
        from sniper.execution.pump_curve_buy import (
            DEFAULT_PRIORITY_FEE_MICROLAMPORTS, SOLANA_PACKET_LIMIT, leg_clip_refusal,
            leg_route_keys, source_price_floor_atomic)
        from sniper.execution.pump_native_exit import ATA_PROGRAM, SYSTEM_PROGRAM, TOKEN_PROGRAM, _pk, ata
        from sniper.settings import memecoin as cfg
        if funding_lamports is not None:
            return None, "native_funding_not_supported_on_damm_v2"
        if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
            # No independent output quote is cached here. Keep the unsigned mechanical backup
            # path rather than fabricate expected output from the source's average fill.
            return None, "output_quote_unavailable"
        pool = self.pools.get(buy.mint)
        non_sol = pool is not None and pool["quote"] != WSOL_MINT and non_sol_listed()
        if non_sol and pool["quote"] != USDC_MINT and (leg is None or quote is None):
            return None, "exotic_leg_missing"
        reason, facts = market_facts(buy, self.pools, getattr(snapshot, "sol_usd", None),
                                     quote=tuple(quote[:2]) if quote else None)
        if reason:
            return None, reason
        if not getattr(snapshot, "blockhash", None):
            return None, "no_blockhash"
        if non_sol:
            got = self._non_sol_input(pool, clip_usd, leg, quote)
            if isinstance(got, str):
                return None, got
            leg, input_account = got
        else:
            if leg is None:
                leg = self.leg_fn() if self.leg_fn else None
            if leg is not None and leg.get("output_mint", WSOL_MINT) != WSOL_MINT:
                return None, "leg_wrong_quote"
            if leg is None or self.clock() - leg["fetched_at"] > self.max_leg_age_s:
                return None, "leg_stale"
            refused = leg_clip_refusal(leg, clip_usd)
            if refused:
                return None, refused
            input_account = None
        amount_in, cost_usdc = int(leg["min_out_lamports"]), int(leg["usdc_atomic"])
        _supply, decimals, _bps, _max = pool["coin_facts"]
        cap_pct = cfg.solana_wallet_chase_cap_pct(
            getattr(buy, "source_user_id", None), cfg.MEMECOIN_REAL_CHASE_MAX_PCT)
        try:
            min_out = source_price_floor_atomic(cost_usdc, facts["price_usd"], cap_pct,
                                                base_decimals=decimals)
        except ValueError as exc:
            return None, str(exc)[:120]
        if not 0 <= int(min_out) <= 2 ** 64 - 1:
            # An absurd source average must refuse, never raise out of the build (E3 chain sim).
            return None, "price floor outside u64"
        user = _pk(self.user)
        coin = _pk(buy.mint)
        coin_program = pool["program_a"] if pool["mint_a"] == buy.mint else pool["program_b"]
        out_ata = ata(user, coin, _pk(coin_program))
        wsol_ata = ata(user, _pk(WSOL_MINT), _pk(TOKEN_PROGRAM))
        input_account = wsol_ata if input_account is None else input_account
        ixs = list(leg["ixs"])
        ixs.append(Instruction(_pk(ATA_PROGRAM), bytes([1]), [      # 1 = CreateIdempotent
            AccountMeta(user, True, True), AccountMeta(out_ata, False, True),
            AccountMeta(user, False, False), AccountMeta(coin, False, False),
            AccountMeta(_pk(SYSTEM_PROGRAM), False, False), AccountMeta(_pk(coin_program), False, False)]))
        ixs.append(swap_instruction(user=self.user, facts=pool, input_account=input_account,
                                    output_account=out_ata, amount_in=amount_in, minimum_out=min_out))
        if not non_sol:                       # the SOL leg's wSOL account is closed; a quote ATA stays
            ixs.append(Instruction(_pk(TOKEN_PROGRAM), bytes([9]), [      # 9 = CloseAccount
                AccountMeta(wsol_ata, False, True), AccountMeta(user, False, True),
                AccountMeta(user, True, False)]))
        cu_limit, leg_hops, cu_estimate = (
            cfg.slot_lane_exotic_cu(leg, "meteora_damm_v2", self.compute_unit_limit) if quote is not None
            else (self.compute_unit_limit, None, None))       # an exotic leg may need more (2026-10-07)
        # A treated crowded send's priority from THIS build's CU limit (pump_curve_buy.static_priority).
        from sniper.execution.pump_curve_buy import static_priority
        price, priority_bid = static_priority(self, cu_limit, tip, priority_lamports,
                                              DEFAULT_PRIORITY_FEE_MICROLAMPORTS)
        head = [set_compute_unit_limit(cu_limit), set_compute_unit_price(price)]
        from sniper.execution.solana_sender_ab import tip_instructions
        ixs = list(ixs) + list(tip_instructions(user, tip))     # Sender tip, last (SENDER_ALL)
        message = MessageV0.try_compile(user, head + ixs, list(leg["tables"]),
                                        Hash.from_string(snapshot.blockhash))
        wire = bytes(VersionedTransaction(message, [NullSigner(user)]))
        if len(wire) > SOLANA_PACKET_LIMIT:
            return None, "transaction is %d bytes, over the %d-byte packet" % (len(wire), SOLANA_PACKET_LIMIT)
        self.last_build = {"bytes": len(wire), "pool": pool["pool"], "min_tokens_out": min_out,
                           "leg_age_s": (self.clock() - leg["fetched_at"]
                                         if leg.get("fetched_at") is not None else None)}
        return message, {**({"priority_bid": priority_bid} if priority_bid else {}),
                         "blockhash": snapshot.blockhash, "cu_limit": cu_limit, "leg_hops": leg_hops,
                         "cu_estimate": cu_estimate,
                         "last_valid_height": getattr(snapshot, "last_valid_height", None),
                         "target_mint": buy.mint, "min_tokens_out": min_out, "pool": pool["pool"],
                         "unsigned_b64": base64.b64encode(wire).decode(),
                         "route_target": ",".join(leg_route_keys(leg) + [pool["pool"]]),
                         # The chase cap the minimum out was built with (Gamma's pre-registration).
                         "pump_price_limit": {
                             "source_price_usd": facts["price_usd"], "cap_pct": cap_pct,
                             "cost_usdc_atomic": cost_usdc, "base_decimals": int(decimals),
                             "spend_lamports": amount_in, "expected_tokens_atomic": None,
                             "output_floor_atomic": None, "price_floor_atomic": min_out,
                             "min_tokens_atomic": min_out, "price_basis": "source_average"}}


def shadow_line(buy, result, sim=None):
    ok, err, units, sslot = sim if sim else (None, None, None, None)
    return ("[memecoin] slot lane damm_v2 %s %s %s pool=%s S=%s -> %s age_ms=%s build_ms=%s "
            "min=%s sim_ok=%s sim_err=%s sim_units=%s sim_slot=%s sig=%s recv=%s" % (
                result.get("lane_mode") or "shadow",
                getattr(buy, "handle", "?"), str(buy.mint)[:8], str(getattr(buy, "pool", None))[:8],
                buy.slot, result.get("reason"),
                "%.1f" % result["signal_age_ms"] if result.get("signal_age_ms") is not None else None,
                "%.1f" % result["build_ms"] if result.get("build_ms") is not None else None,
                result.get("min_tokens_out"), ok,
                json.dumps(err, separators=(",", ":")) if err is not None else None, units, sslot,
                getattr(buy, "signature", None), getattr(buy, "recv_utc", None)))


class ShadowVerifier:
    """Simulates each shadow build once, off the lane thread, on the lane's own watch key (never the
    custody RPC), and logs whether it would have landed. Never starts while the lane thread handles a
    buy (`idle`). A full queue drops and counts; nothing here can refuse, delay or send an entry."""

    def __init__(self, *, log, sim_rpc=None, idle=None, maxsize=64, start=True):
        import queue
        self.log, self.sim_rpc, self.idle = log, sim_rpc, idle
        self._q = queue.Queue(maxsize=maxsize)
        self.stats = {"logged": 0, "dropped": 0, "sim_ok": 0, "sim_err": 0, "sim_failed": 0,
                      "sim_skipped": 0}
        if start:
            threading.Thread(target=self._run, name="slot-lane-damm-v2-verify", daemon=True).start()

    def offer(self, buy, result):
        import queue
        try:
            self._q.put_nowait((buy, result))
        except queue.Full:
            self.stats["dropped"] += 1

    def _simulate(self, result):
        from sniper.execution.pump_native_exit import _rpc_result
        from sniper.execution.raydium_launchlab_buy import SIM_IDLE_WAIT_S
        if not result.get("unsigned_b64") or self.sim_rpc is None:
            return None
        if self.idle is not None and not self.idle.wait(timeout=SIM_IDLE_WAIT_S):
            self.stats["sim_skipped"] += 1
            return None
        try:
            res = _rpc_result(self.sim_rpc, "simulateTransaction", [result["unsigned_b64"], {
                "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
                "commitment": "processed"}]) or {}
        except Exception:
            self.stats["sim_failed"] += 1
            import logging
            logging.getLogger(__name__).warning("damm v2 shadow simulation failed", exc_info=True)
            return None
        value = res.get("value") if isinstance(res, dict) else None
        if not isinstance(value, dict) or "err" not in value:
            # No answer (a null result or value, an error envelope, no err key) is unknown, never
            # a landing: it counted sim_ok (SOL-2, Codex review 2026-10-06).
            self.stats["sim_failed"] += 1
            return None
        ok = value["err"] is None
        self.stats["sim_ok" if ok else "sim_err"] += 1
        return ok, value.get("err"), value.get("unitsConsumed"), (res.get("context") or {}).get("slot")

    def verify_once(self, buy, result):
        self.log(shadow_line(buy, result, self._simulate(result)))
        self.stats["logged"] += 1

    def _run(self):
        while True:
            buy, result = self._q.get()
            try:
                self.verify_once(buy, result)
            except Exception:
                import logging
                logging.getLogger(__name__).warning("damm v2 shadow line failed", exc_info=True)
