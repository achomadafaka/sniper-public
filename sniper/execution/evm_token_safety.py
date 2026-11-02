"""EVM pre-entry safety checks a contract, an executable sell route, and a funded sell simulation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
import logging
import os
import threading
import time

from sniper.execution.evm_rpc_pool import RequestNotSent

_now = time.monotonic          # seam: monotonic so a host clock step cannot extend a refusal

# --- storage-slot discovery for the funded sell simulation -------------------------------------
# We must simulate selling a token we do not own. eth_call state overrides let us fabricate the
# balance + allowance, but only if we know WHICH storage slot holds them, and that is not
# introspectable — so we probe. Bounded scan: standard ERC-20s (OpenZeppelin, solmate) land at slot
# 0/1; a couple of dozen probes covers the long tail without turning entry into an RPC storm.
# OpenZeppelin-UPGRADEABLE ERC20s (Initializable at 0 + ContextUpgradeable __gap[50]) put _balances
# at slot 51 and _allowances at slot 52 — past any small linear scan. Seen live 2026-07-17 on 4663
# mint 0xe013…: its funded sell simulation read 'unverifiable'.
_SLOT_SCAN = 12
_SLOT_CANDIDATES = tuple(range(_SLOT_SCAN)) + (51, 52)   # linear scan + OZ-Upgradeable layout
# Width of the ONE concurrent wave the tail is probed in. Bounded so a scan cannot fan out past the
# candidate list; the prefix above never uses it.
_SLOT_TAIL_WORKERS = int(os.getenv("MEMECOIN_SLOT_SCAN_TAIL_WORKERS", "12"))
# PER-CHAIN LEAD LAYOUT (2026-10-14, 73851). A layout a chain's launchpads stamp out is probed FIRST.
# On 56, 185 of 209 stored proofs (memecoin_evm_slot_proofs) are OZ-Upgradeable (balances 51,
# allowances 52; all 184 '...7777' mints); the serial prefix walked 0, 1 and Solady before the tail
# reached 51: 298 ms of a 317 ms inline safety check. The lead is verified by the same readback probe
# as every candidate; a token with another layout pays one extra probe. Other chains: unchanged
# (4663 is 449/565 slot 0, already first).
_CHAIN_SLOT_LEADS = {"56": (51, 52)}            # chain -> (balance mapping index, allowance index)


def _lead_slots(chain, mint, slot_for_index, *, b20_index, which):
    """Keys probed before slot 0: a B20 mint's known slot, then the chain's measured layout."""
    leads = []
    if _is_b20_address(mint):
        leads.append(slot_for_index(_B20_STORAGE_BASE + b20_index))
    index = _CHAIN_SLOT_LEADS.get(str(chain))
    if index:
        leads.append(slot_for_index(index[0 if which == "balance" else 1]))
    return tuple(leads)
_SOLADY_BALANCE_SLOT_SEED = bytes.fromhex("87a211a2")
_SOLADY_ALLOWANCE_SLOT_SEED = bytes.fromhex("7f5e9f20")
_BIG = 10 ** 24                                     # fabricated token balance / allowance
# STORAGE values are 32-byte words; an ACCOUNT balance is a hexutil.Big *quantity* and must be
# compact — a zero-padded balance is rejected as -32602 ("hex number with leading zero digits"),
# which then reads as a revert and gets reported as a honeypot. Two encodings, do not mix them.
_BIG_HEX = "0x" + _BIG.to_bytes(32, "big").hex()
_GAS_HEX = hex(10 ** 18)                            # 1 native unit, so gas checks can't revert
_UINT256_MAX = 2 ** 256 - 1
# A not-found layout EXPIRES. It describes on-chain state at one instant, not a property of the
# token, and 4663 entries are fresh minimal-proxy clones whose state moves under us. 120 s is long
# enough that a genuinely exotic layout is not re-scanned once per event, and short enough that a
# coin does not stay refused for the life of the runner process.
_NO_SLOT_TTL_S = 120.0
_NO_SLOT_MAX = 512                                  # bound the expiry map independently of the LRU
EVM_ROUTE_UNAVAILABLE_PREFIX = "configured sell venue returned no executable quote"
_SIM_REVERTED_UNKNOWN = "reverted_unknown"
_SIM_MIN_OUT_REPEATED = "min_out_repeated"
_SIM_REQUOTE_UNAVAILABLE = "requote_unavailable"
_SIM_RPC_UNKNOWN = "rpc_unknown"
_SIM_NO_SPENDER = "no_spender"
_PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
EVM_EXIT_UNQUOTABLE_AT_CLIP = "exit_unquotable_at_clip"


def _quote_miss_is_blind(miss_code):
    """A rung that could not READ (rpc_transport, rate limit, timeout, ...) says nothing about
    whether the coin can be sold -- see `classify_quote_miss` in evm_real_seams.py, whose blind
    codes are all named `provider_<reason>`. `quote_unattributed` (the ladder recorded no rung at
    all) fails toward blind too. `None` alone -- no `quote_with_miss_fn` wired -- is NOT treated as
    blind: it preserves the plain `quote_fn` miss this codebase has always read as a real answer,
    so a caller that has not opted into the finer classifier keeps its existing behaviour."""
    if miss_code is None:
        return False
    return miss_code == "quote_unattributed" or str(miss_code).startswith("provider_")


logger = logging.getLogger(__name__)


class EvmSafetyDeadlineExceeded(TimeoutError):
    """The funded pre-entry safety deadline expired."""


class _MalformedProbe(Exception):
    """One node answered a slot probe with something that is not hex. That is an answer about the
    NODE, not about the layout, so it is retried (and rotated) like a transport fault."""


class _Permit2Decided(Exception):
    """One Permit2 simulation's overrides, decided on THIS call's live allowance read: balance-only when
    that allowance covers the amount word (allowance_slot None), else both slots. Never a cached proof
    (Gamma (d), 2026-10-16; Alpha 2026-10-17: warm agrees with cold). Raised through BoundedLru, which
    never stores a loader failure."""

    def __init__(self, allowance, balance_slot, allowance_slot=None):
        super().__init__("permit2 overrides decided on a live allowance read")
        self.allowance, self.balance_slot, self.allowance_slot = allowance, balance_slot, allowance_slot


class _SlotScanTransportError(Exception):
    """The slot scan could not COMPLETE because an RPC read failed transiently (after retries).

    This is categorically different from a clean 'scanned every candidate, the override never
    landed' negative. Conflating the two (2026-07-15) let a single RPC blip during the balanceOf
    probe get negative-cached as _NO_SLOT for the whole runner process, permanently branding a
    normal, sellable token 'storage layout not discoverable' — chain 4663 lost ~half its real
    entries this way. A transient failure must fail-closed for THAT attempt only and NEVER be
    cached, so the next event (RPC recovered) can pass."""


def _pad(x):
    """32-byte left-pad, as ABI-encoded mapping keys are."""
    if isinstance(x, int):
        return x.to_bytes(32, "big").hex()
    return x[2:].lower().rjust(64, "0")


def _map_slot(key_addr, index):
    """Solidity mapping(address => uint) at slot `index`: keccak(pad(key) . pad(index))."""
    from eth_utils import keccak
    return "0x" + keccak(bytes.fromhex(_pad(key_addr) + _pad(index))).hex()


def _nested_slot(spender, owner, index):
    """mapping(address => mapping(address => uint)): keccak(pad(spender) . keccak(pad(owner) . pad(i)))."""
    from eth_utils import keccak
    inner = keccak(bytes.fromhex(_pad(owner) + _pad(index)))
    return "0x" + keccak(bytes.fromhex(_pad(spender)) + inner).hex()


# B20, BASE'S NATIVE TOKEN STANDARD (2026-10-08, Jez 0xb2000…da01; design f026987d1). A B20 token is a
# Rust precompile (code 0xef) at 0xB2 + 9 zero bytes + derived bytes; its balances and allowances live
# in the token account's own storage, ERC-7201 namespace "base.b20", fields 4 and 5, as Solidity
# mappings (read back on chain 2026-10-08). Discovery cannot find them: the precompile's reads are not
# in eth_createAccessList, and the base is a hash. They are LEAD candidates for a prefixed mint only,
# verified by the same override-and-read-back probe as every other.
_B20_STORAGE_BASE = 0xC78B71FEE795DDD74AFF64EA9B2474194C938C3196430E10BB5F01ED48434000
_B20_PREFIX = bytes([0xB2]) + bytes(9)


def _is_b20_address(mint):
    try:
        raw = bytes.fromhex(str(mint)[2:]) if str(mint).lower().startswith("0x") else b""
    except ValueError:
        return False
    return len(raw) == 20 and raw[:10] == _B20_PREFIX


def _solady_balance_slot(owner):
    """Solady ERC20 balance key: keccak(owner[20] || zero[8] || balance seed[4])."""
    from eth_utils import keccak
    return "0x" + keccak(bytes.fromhex(_pad(owner)[24:]) + bytes(8)
                          + _SOLADY_BALANCE_SLOT_SEED).hex()


def _solady_allowance_slot(spender, owner):
    """Solady ERC20 allowance key: owner[20] || zero[8] || seed[4] || spender[20]."""
    from eth_utils import keccak
    return "0x" + keccak(bytes.fromhex(_pad(owner)[24:]) + bytes(8)
                          + _SOLADY_ALLOWANCE_SLOT_SEED
                          + bytes.fromhex(_pad(spender)[24:])).hex()


def _simulation_amount_word(value):
    """Return one funded token balance and allowance word for a sell quote.

    ALWAYS above the amount being sold, never equal to it. `max(_BIG, amount)` funded the balance to
    EXACTLY the sell amount whenever the amount exceeded `_BIG`, leaving zero headroom — and a
    fee-on-transfer or reflection token whose `transferFrom` needs more than it moves then reverts
    on its own accounting, which the caller reads as `TRANSFER_FROM_FAILED` and reports as unsellable.
    Measured on stored exit intents: 31 of 1153 sell amounts (2.7%) exceed `_BIG`, the largest by
    8.7x, so the zero-headroom case is real rather than theoretical.

    Extra simulated balance cannot manufacture a false PASS: the balance is fabricated either way and
    a token's transfer restrictions still apply to it. What it removes is a false REFUSAL.
    """
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0 or amount > _UINT256_MAX:
        return None
    return "0x" + min(_UINT256_MAX, amount + _BIG).to_bytes(32, "big").hex()


def _validated_access_list_slots(result, mint):
    """Bounded exact-token hints; every returned key still needs override readback proof."""
    import re
    entries = result.get("accessList") if isinstance(result, dict) else None
    if not isinstance(entries, list) or len(entries) > 64:
        raise ValueError("malformed access-list result")
    keys = []
    for entry in entries:
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("address"), str)
                or re.fullmatch(r"0x[0-9a-fA-F]{40}", entry["address"]) is None):
            raise ValueError("malformed access-list address")
        storage = entry.get("storageKeys")
        if not isinstance(storage, list) or len(storage) > 64:
            raise ValueError("malformed access-list storage keys")
        if entry["address"].lower() != str(mint).lower():
            continue
        for key in storage:
            if not isinstance(key, str) or re.fullmatch(r"0x[0-9a-fA-F]{64}", key) is None:
                raise ValueError("malformed access-list storage key")
            key = key.lower()
            if key not in keys:
                keys.append(key)
            if len(keys) > 16:
                raise ValueError("access-list storage hint limit exceeded")
    return tuple(keys)


def _access_list_slots(mint, data, rpc_url, http=None, taker=None, *, strict=False):
    """Storage keys the node says this call actually READS, or () when it cannot say.

    ASK, DO NOT GUESS. `_SLOT_CANDIDATES` enumerates the layouts we know about, so a token using a
    namespaced layout (ERC-7201 and friends, where the base slot is a hash rather than a small
    integer) is invisible to it and the entry refuses as `storage layout not discoverable`.
    `eth_createAccessList` executes the getter and reports the exact slots it touched, which is the
    answer the scan is trying to reconstruct.

    OURO (`0x8ea0eb35`, chain 4663, 2026-09-04) is the case: a 45-byte EIP-1167 minimal proxy whose
    balance slot is `0x8263e588…`, found by no linear scan of 0..63 and hit by this on the first
    call.

    A key from here is still VERIFIED by the same override-and-read-back probe as every scanned
    candidate, so this widens what we can discover without weakening what we accept. The method is
    not universally served -- on the 4663 pool only Alchemy answers it -- and every failure returns
    () so the caller keeps its existing not-found behaviour.

    `gas` is REQUIRED in the call. Without it the node applies the transaction against the sender's
    real balance and returns `insufficient funds` instead of a list.
    """
    if not rpc_url:
        if strict:
            raise ValueError("access-list RPC unavailable")
        return ()
    call = {"to": mint, "data": data, "gas": "0x100000"}
    if taker:
        call["from"] = taker
    try:
        from sniper.execution.evm_wallet import _rpc as _wallet_rpc
        payload = _wallet_rpc(rpc_url, "eth_createAccessList", [call, "latest"], http=http)
    except RequestNotSent:
        raise
    except Exception:
        if strict:
            raise
        return ()
    if strict:
        if (not isinstance(payload, dict) or type(payload.get("id")) is not int
                or payload["id"] != 1):
            raise ValueError("unmatched access-list response")
        error = payload.get("error")
        if error is not None:
            if (isinstance(error, dict) and type(error.get("code")) is int
                    and error["code"] == -32601 and "result" not in payload):
                return ()  # this exact optional method is explicitly unsupported
            raise ValueError("access-list RPC error")
        return _validated_access_list_slots(payload.get("result"), mint)
    entries = ((payload or {}).get("result") or {}).get("accessList") or []
    if not isinstance(entries, list):
        return ()
    keys = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("address") or "").lower() != str(mint).lower():
            continue                       # the implementation's own slots are not the token's
        for key in entry.get("storageKeys") or ():
            if isinstance(key, str) and key.startswith("0x") and key not in keys:
                keys.append(key)
    return tuple(keys)


def _find_slot(read_back, slot_for_index, *, extra_slots=(), late_slots=None, alternates=(),
               probe_scope=None, lead_slots=(), early_slots=None):
    """Return the storage key whose override actually lands, or None. `read_back(key)` overrides that
    key and re-reads the getter; the slot is right when our fabricated value comes back out.

    Standard ERC-20s (OpenZeppelin, solmate) hit on the first probe — measured on live chain-4663
    tokens (TENDIES, JUGGERNAUT): slot 0, one call, ~0.15s. The scan only runs long on exotic
    layouts, which we then refuse as unverifiable rather than guess at.

    A clean scan that finds nothing returns None (genuinely undiscoverable — safe to negative-cache).
    An RPC transport failure that survives retries raises _SlotScanTransportError instead: the scan
    is INCONCLUSIVE, not a proven negative, and the caller must never cache it (2026-07-15)."""
    # Keep the common Solidity 0/1 happy path unchanged, then try known hashed layouts before the
    # long tail. Solady launch clones otherwise pay the entire bounded scan twice during Fomo entry.
    # `lead_slots` (a B20 mint's known slot) go before even slot 0; empty for every other token, so
    # its probe order is unchanged.
    candidates = (*lead_slots, *_SLOT_CANDIDATES[:2], *extra_slots, *_SLOT_CANDIDATES[2:])
    prefix_len = len(tuple(lead_slots)) + 2 + len(tuple(extra_slots))
    keys = []
    for candidate in candidates:
        try:
            keys.append(slot_for_index(candidate) if isinstance(candidate, int) else candidate)
        except Exception:
            return None                             # deterministic bad input -> clean not-found

    def _parsed(got):
        """None on a clean revert, the integer value otherwise. A non-hex body raises so the
        retry loop treats it like a blip: the SAME node answering the SAME garbage four times is
        what rotation exists for (live 2026-09-04: "malformed slot-probe result" refused SUIT twice
        with no retry and no rotation, because the parse ran outside the retried unit)."""
        if got is None:
            return None                      # clean EVM revert: this candidate is not the slot
        try:
            return int(got, 16)
        except (TypeError, ValueError) as e:
            raise _MalformedProbe("malformed slot-probe result") from e

    def _hit(key, value):
        """None when this candidate is not the slot; the key when it is."""
        return key if value == _BIG else None

    def _probe(key):
        try:
            # The tail uses another executor; thread-local deadlines do not inherit.
            with probe_scope() if probe_scope is not None else nullcontext():
                return _read_with_retry(lambda k, **kw: _parsed(read_back(k, **kw)), key,
                                        alternates=alternates)
        except (EvmSafetyDeadlineExceeded, RequestNotSent):
            # The deadline names itself. Wrapping it here turned "safety_deadline_exceeded" into
            # "sell simulation failed (fail-closed)" for every deadline hit inside a slot probe --
            # the BLAD misattribution of 2026-09-02, at the one site the earlier fix did not reach.
            raise
        except Exception as e:
            # RPC failed AFTER retries — says nothing about the layout. Do NOT return the not-found
            # sentinel (that gets negative-cached forever); signal a transient so the caller retries.
            raise _SlotScanTransportError(str(e)) from e

    # The cheap prefix stays SERIAL and therefore costs exactly what it costs today. Standard
    # ERC-20s hit slot 0 or 1 on the first probe, and firing the whole fan-out speculatively would
    # multiply funded RPC calls on the common case to save nothing.
    for key in keys[:prefix_len]:
        hit = _hit(key, _probe(key))
        if hit is not None:
            return hit

    # Job-bound BSC preparation alone asks for exact getter hints before the paced tail.
    # Errors remain inconclusive; a hint is accepted only by the SAME readback probe.
    hinted = set()
    if early_slots is not None:
        for key in early_slots():
            if key in keys[:prefix_len] or key in hinted:
                continue
            hinted.add(key)
            hit = _hit(key, _probe(key))
            if hit is not None:
                return hit

    # The tail is reached ONLY when the cheap prefix missed, and it is where the cost was: 12 more
    # candidates walked one RPC round trip at a time. An OpenZeppelin-upgradeable token (balances at
    # slot 51) paid twelve failed probes before its hit — ~2.4s at 4663's ~200ms/trip, which is the
    # bulk of `evm.token_safety`'s measured p50 2,835 ms. One concurrent wave instead of a walk.
    #
    # Resolution stays IN CANDIDATE ORDER, not in response order: an earlier candidate's hit beats a
    # later one, and an earlier transport failure still raises before a later hit can paper over it.
    # Otherwise the verdict would depend on which probe answered first.
    tail = [key for key in keys[prefix_len:] if key not in hinted]
    if not tail:
        return None
    executor = ThreadPoolExecutor(
        max_workers=max(1, min(_SLOT_TAIL_WORKERS, len(tail))),
        thread_name_prefix="evm-slot-scan")
    try:
        futures = [executor.submit(_probe, key) for key in tail]
        for key, future in zip(tail, futures):
            hit = _hit(key, future.result())
            if hit is not None:
                return hit
    finally:
        # NOT a context manager: `with` joins every future on exit, so an early hit would still wait
        # out the slowest probe in the wave. Every probe is already in flight; the losers are dropped.
        executor.shutdown(wait=False)

    # EVERY LAYOUT WE KNOW OF MISSED. Before reporting the layout undiscoverable, ask the node which
    # slots the getter actually reads. Deliberately last: it costs one more round trip and only a
    # namespaced-storage token ever reaches here. The answer is verified by the same probe as any
    # other candidate, so an endpoint that cannot answer changes nothing.
    # STRICTLY A BONUS, SO IT MUST NEVER WORSEN THE ANSWER. Reaching this line means the scan of
    # every known layout already COMPLETED CLEANLY, which is a negative the caller may cache. If
    # this extra probe fails on transport it would raise `_SlotScanTransportError` and convert that
    # clean negative into an inconclusive one -- re-scanning the token on every alert and reporting
    # a token verdict as `sell simulation failed`. Swallow it and keep the clean not-found.
    try:
        for key in (late_slots() if early_slots is None and callable(late_slots) else ()):
            if key in keys:
                continue                    # already probed above; do not pay for it twice
            hit = _hit(key, _probe(key))
            if hit is not None:
                return hit
    except RequestNotSent:
        raise
    except Exception:
        logger.debug("access-list slot probe failed; keeping the clean not-found", exc_info=True)
    return None


def _read_with_retry(read_back, key, attempts=4, backoff_s=0.2, *, alternates=()):
    """Retry on a TRANSPORT failure with exponential backoff. Without this a single RPC blip on the
    correct slot makes the scan walk past it and declare the token unverifiable (seen live on 4663:
    TENDIES flipped between 'ok' and 'unverifiable' run to run). A revert returns None and is NOT
    retried — that just means this candidate slot is the wrong one.

    Backoff is exponential (0.2, 0.4, 0.8s) to ride through a real rate-limit window: the correct
    slot is almost always the first probe (slot 0/1), so the happy path pays ZERO retry latency;
    only a genuinely-blipping RPC pays, and only there does riding-through save a lost entry.

    EVERY RETRY CHANGES ENDPOINT when `alternates` is given (2026-09-04). Inside the safety
    deadline the hedge is off and `_rpc` prefers the configured primary, so four retries against
    a node that answers HTTP 200 with garbage were four identical answers: a 200 is recorded as a
    success and nothing ever quarantined it. Attempt N>0 asks `alternates[N-1]` instead; `_rpc`
    still owns identity proof and quarantine for whichever member it is handed, so a quarantined
    or wrong-chain alternate is skipped there, never trusted here. With no alternates (a chain
    with no pool, every test) the behaviour is byte-for-byte the old one."""
    import time
    alternates = _usable_alternates(alternates)
    for attempt in range(attempts):
        try:
            if attempt and alternates:
                return read_back(key, url=alternates[(attempt - 1) % len(alternates)])
            return read_back(key)
        except (EvmSafetyDeadlineExceeded, RequestNotSent):
            # A local pre-dispatch refusal cannot recover through sleeps or another endpoint.
            raise
        except Exception:
            if attempt + 1 >= attempts:
                raise
            time.sleep(backoff_s * (2 ** attempt))


def _usable_alternates(alternates):
    """Alternates that are not quarantined RIGHT NOW, configured order; all of them when every one
    is. Live 2026-09-04 the first alternate was QuickNode, 429 on its daily cap all day: `_rpc`
    recovers inside the same call, but only after paying that 429 once per quarantine expiry."""
    alternates = tuple(alternates or ())
    if not alternates:
        return alternates
    from sniper.execution import evm_rpc_pool as _pool
    live = tuple(u for u in alternates if _pool.available(u))
    return live or alternates


def _slot_probe_alternates(rpc_url):
    """The other members of `rpc_url`'s pool, configured order. Empty when there is no pool.
    Availability and chain identity are NOT checked here: `_rpc` proves both on use."""
    from sniper.execution import evm_rpc_pool as _pool
    return tuple(u for u in _pool.pool_for(rpc_url) if u != rpc_url)


from sniper.settings import memecoin as cfg  # noqa: E402


def timed(timings, key, fn, *args, **kwargs):
    """DISPLAY ONLY: run `fn` and add its wall time to `timings[key]` in ms. The result and any
    exception pass through untouched; no timings dict, no timing."""
    if timings is None:
        return fn(*args, **kwargs)
    import time
    started = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    finally:
        timings[key] = timings.get(key, 0.0) + (time.perf_counter() - started) * 1000.0


def names_timings(fn):
    """True when `fn` declares a `timings` parameter. A **kwargs catch-all does not count: it would
    forward the dict to an inner function that raises, and a raised safety check fails closed."""
    import inspect
    try:
        return "timings" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


# Token-level transfer failures that arrive as custom errors (Solady/Solmate SafeTransferLib,
# OpenZeppelin SafeERC20 and ERC-6093). They are the coin refusing to move, never the route.
def _selectors(*signatures):
    from eth_utils import keccak
    return frozenset(keccak(text=sig)[:4].hex() for sig in signatures)


_TOKEN_TRANSFER_ERRORS = _selectors(
    "TransferFromFailed()", "TransferFailed()", "SafeERC20FailedOperation(address)",
    "ERC20InsufficientBalance(address,uint256,uint256)",
    "ERC20InsufficientAllowance(address,uint256,uint256)",
    "ERC20InvalidSender(address)", "ERC20InvalidReceiver(address)")


def _route_attributable(detail):
    """True only for an UNNAMED custom error that is not a token transfer failure: the pool or hook
    reverting (STOCK's hook, 2026-09-04). A string revert is the token's or router's own words, and
    `TransferHelper: TRANSFER_FROM_FAILED` is the token refusing the move into its pair (SICAT
    70895, 2026-10-06, a honeypot the isolated taker -> router pull could not see)."""
    head = str(detail or "")
    if not head.startswith("custom_error 0x"):
        return False
    return head[len("custom_error 0x"):len("custom_error 0x") + 8].lower() not in _TOKEN_TRANSFER_ERRORS


# WHICH POOL MEMBERS ANSWER eth_simulateV1 (Alpha 2026-10-07). Support varies by member (Base's
# mainnet.base.org rejects it, evm_real_seams.py:1847), so a simulation routed to whichever member
# answers would make the same coin admit on one try and refuse on the next. Each member's verdict is
# learned once and frozen; only a DEFINITE answer freezes: "supported" (a result list) or "refused"
# (a method-not-supported error). A transport failure, 429, 5xx or any other error freezes nothing
# and is re-probed with backoff (Gamma MEDIUM on 064b1f042: a deploy-time hiccup must not empty
# the set for the process lifetime). The simulation itself is posted to the chosen member
# DIRECTLY, never through the pool's rotation (Gamma MEDIUM: _rpc re-chose among all members).
# Set-once per member under a lock: the funded runner is threaded.
_SIMULATE_LOCK = threading.Lock()
_SIMULATE_MEMBERS = {}          # rpc_url -> {member_url: "supported" | "refused"}
_SIMULATE_RETRYING = set()      # rpc_urls whose pending members a background thread re-probes
_SIMULATE_BACKOFF_S = (2, 4, 8, 16, 32, 64, 128)
_SIMULATE_REPROBE_S = 128       # then every 128 s while any member is still unknown (Gamma LOW)
_METHOD_REFUSED = ("not supported", "does not exist", "not found", "unsupported", "not available")
_TRANSIENT = ("temporar", "try again", "limit", "rate", "timeout", "busy", "header not")


def _definite_method_refusal(error):
    """True only when the answer refuses the METHOD: code -32601, or a message that names the
    method (or "method") together with a not-supported phrase. A transient answer (-32005, or
    "temporarily", "try again", "limit", ...) never freezes a member out (Gamma MEDIUM)."""
    if not isinstance(error, dict):
        return False
    code, text = error.get("code"), str(error.get("message") or "").lower()
    if code == -32005 or any(word in text for word in _TRANSIENT):
        return False
    if code == -32601:
        return True
    return (("method" in text or "simulatev1" in text)
            and any(phrase in text for phrase in _METHOD_REFUSED))


def _pool_members(rpc_url):
    from sniper.execution import evm_rpc_pool
    return evm_rpc_pool.pool_for(rpc_url)


def _post(url, method, params, http):
    """One JSON-RPC POST to exactly `url` (no pool rotation). Raises on a provider failure."""
    from sniper.execution.evm_wallet import _http, _provider_http_failure
    status, answer, _detail = (http or _http)("POST", url, body=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))
    if _provider_http_failure(status):
        raise RuntimeError("http %s" % status)
    return answer


def _probe_member(url, http):
    """'supported', 'refused' (definite method refusal) or None (unknown: probe again later)."""
    try:
        answer = _post(url, "eth_simulateV1", [{"blockStateCalls": [{"calls": []}]}, "latest"], http)
    except Exception:
        return None, "transport"
    if isinstance(answer, dict) and isinstance(answer.get("result"), list):
        return "supported", "ok"
    error = (answer or {}).get("error") if isinstance(answer, dict) else None
    if _definite_method_refusal(error):
        return "refused", str(error)[:120]
    return None, str(error or answer)[:120]


def _probe_pending(rpc_url, http):
    """Probe every member without a frozen verdict once. Returns how many are still unknown."""
    from urllib.parse import urlsplit
    pending = 0
    for url in _pool_members(rpc_url):
        with _SIMULATE_LOCK:
            known = _SIMULATE_MEMBERS.setdefault(rpc_url, {}).get(url)
        if known is not None:
            continue
        verdict, why = _probe_member(url, http)
        if verdict is None:
            pending += 1
            continue
        with _SIMULATE_LOCK:
            _SIMULATE_MEMBERS[rpc_url].setdefault(url, verdict)
        if verdict == "refused":
            logger.warning("evm.simulate_member_refused host=%s why=%s", urlsplit(url).hostname, why)
    return pending


def warm_simulate_members(rpc_url, *, http=None, sleep=time.sleep, max_reprobes=None):
    """Startup (the leg's daemon thread): probe, re-probe unknown members with backoff, then every
    _SIMULATE_REPROBE_S while any is still unknown (one call per unknown member per 128 s), so an
    outage at startup never leaves the set empty until a restart. `max_reprobes` bounds tests."""
    pending = _probe_pending(rpc_url, http)
    for delay in _SIMULATE_BACKOFF_S:
        if not pending:
            break
        sleep(delay)
        pending = _probe_pending(rpc_url, http)
    rounds = 0
    while pending and (max_reprobes is None or rounds < max_reprobes):
        sleep(_SIMULATE_REPROBE_S)
        pending = _probe_pending(rpc_url, http)
        rounds += 1
    with _SIMULATE_LOCK:
        _SIMULATE_RETRYING.discard(rpc_url)
    return simulate_supported(rpc_url)


def simulate_supported(rpc_url):
    """The frozen supporting members, in pool order."""
    with _SIMULATE_LOCK:
        verdicts = dict(_SIMULATE_MEMBERS.get(rpc_url) or {})
    return tuple(url for url in _pool_members(rpc_url) if verdicts.get(url) == "supported")


def simulate_member(rpc_url, *, http=None):
    """The first frozen supporting member that is not quarantined, else the first supporting one;
    None when none is known (the simulation is then unknown and the coin refuses). An unwarmed
    pool is probed once here, and its unknown members are re-probed in the background."""
    from sniper.execution import evm_rpc_pool
    with _SIMULATE_LOCK:
        warmed = rpc_url in _SIMULATE_MEMBERS
    if not warmed and _probe_pending(rpc_url, http):
        with _SIMULATE_LOCK:
            start = rpc_url not in _SIMULATE_RETRYING
            _SIMULATE_RETRYING.add(rpc_url)
        if start:
            threading.Thread(target=warm_simulate_members, args=(rpc_url,), kwargs={"http": http},
                             name="evm-simulate-warm", daemon=True).start()
    members = simulate_supported(rpc_url)
    for url in members:
        if evm_rpc_pool.available(url):
            return url
    return members[0] if members else None


def _rung_note(rung):
    """`<rung>:hit|miss|blind:<why>[:<reason>]:<ms>ms` for one sell-probe rung record."""
    blind = rung.get("blind_reason")
    state = "hit" if rung.get("hit") else ("blind:%s" % blind if blind else "miss")
    reason = "" if rung.get("hit") or not rung.get("reason") else ":%s" % str(rung["reason"])[:60]
    try:
        ms = "%.0fms" % float(rung.get("duration_ms") or 0.0)
    except (TypeError, ValueError):
        ms = "?ms"
    return "%s:%s%s:%s" % (rung.get("rung"), state, reason, ms)


def _maybe_schedule_buy_tax_shadow(chain, sell_quote):
    """A PASS on a uniswap_v2 sell route starts the V2 buy-tax shadow: a thread start, no I/O
    here; the measurement runs later on a research host (memecoin_v2_buy_tax_shadow)."""
    route = (sell_quote or {}).get("route") or {}
    if (str(getattr(cfg, "MEMECOIN_EVM_V2_BUY_TAX_GATE", "off")) != "shadow"
            or route.get("protocol") != "uniswap_v2" or not route.get("tokens")):
        return
    try:
        from sniper.services import memecoin_v2_buy_tax_shadow as shadow
        shadow.schedule(chain, list(route["tokens"]), router=route.get("router"))
    except Exception:                               # a shadow never touches the verdict
        logger.exception("buy tax shadow did not start chain=%s", chain)


def token_safety_ok_evm(mint, *, code_fn, sell_route_fn, sell_sim_fn, sell_route_label=None,
                        allow_pull_only=None, round_trip_fn=None):
    """Return (ok: bool, reason: str). Seams:
      code_fn(mint) -> hex bytecode ('0x' = not a contract)
      sell_route_fn(mint) -> bool  (a mint->USDC route exists)
      sell_sim_fn(mint) -> bool | str | None (True = clean; False = legacy proven-token failure;
                                              str = classified fail-closed outcome;
                                              None = could not fund the sim)
    All checks fail-closed."""
    try:
        code = code_fn(mint)
    except EvmSafetyDeadlineExceeded:
        logger.warning("funded token safety: code read hit the deadline mint=%s", mint)
        return False, "safety_deadline_exceeded"
    except RequestNotSent:
        logger.warning("funded token safety: code read not dispatched mint=%s", mint)
        return False, "code read failed (fail-closed)"
    except Exception:
        # The REASON STRING IS LOAD-BEARING and must not carry the cause: it is matched exactly by
        # `_RETRY_TRANSIENT` in memecoin_real_runner, and appending an exception name would drop
        # this outcome out of that set and silently STOP the retry. The cause goes to the log.
        logger.exception("funded token safety: code read raised mint=%s", mint)
        return False, "code read failed (fail-closed)"
    if code is None:
        return False, "code read failed (fail-closed)"
    if not code or code in ("0x", "0x0", "0X"):
        return False, "not a contract (no code at address)"
    try:
        if not sell_route_fn(mint):
            label = (sell_route_label() if callable(sell_route_label) else sell_route_label) \
                or "configured"
            return False, (f"{EVM_ROUTE_UNAVAILABLE_PREFIX} "
                           f"(route={label}; other venues not tested)")
    except EvmSafetyDeadlineExceeded:
        logger.warning("funded token safety: sell-route probe hit the deadline mint=%s", mint)
        return False, "safety_deadline_exceeded"
    except RequestNotSent:
        logger.warning("funded token safety: sell-route probe not dispatched mint=%s", mint)
        return False, "sell-route probe failed (fail-closed)"
    except Exception:
        logger.exception("funded token safety: sell-route probe raised mint=%s", mint)
        return False, "sell-route probe failed (fail-closed)"
    try:
        sim = sell_sim_fn(mint)
    except EvmSafetyDeadlineExceeded:
        # THE DEADLINE NAMES ITSELF NOW. It is a TimeoutError, so the generic handler below used to
        # swallow it and report "sell simulation failed (fail-closed)" -- a token verdict for an
        # infrastructure fault. BLAD 0xaa2d398d on 2026-09-02 refused that way after a 7,502ms
        # safety run against a 3.0s per-read budget, and cleared on the retry 21 seconds later.
        # Measured over 7 days on 4663, 24 of ~60 safety refusals are deadline or timeout, so this
        # is the largest single safety-refusal class and it was partly reported as something else.
        # Both reasons are already in `_RETRY_TRANSIENT`, so retry behaviour is UNCHANGED; what
        # changes is that a slow node stops being counted as an unsellable token.
        logger.warning("funded token safety: sell simulation hit the deadline mint=%s", mint)
        return False, "safety_deadline_exceeded"
    except RequestNotSent:
        logger.warning("funded token safety: sell simulation read not dispatched mint=%s", mint)
        return False, "sell simulation failed (fail-closed)"
    except Exception:
        # BLAD 0xaa2d398d, 2026-09-02: refused as "sell simulation failed (fail-closed)" after
        # `evm.token_safety` ran 7,502ms. The cause was destroyed here, so the refusal named the
        # boundary and not the fault -- the same defect as the shared `rpc_transport` label and the
        # discarded revert selector. Individual reads are bounded at
        # `_FUNDED_EVM_READ_TIMEOUT_S` = 3.0s, so a 7.5s safety run is consistent with reads timing
        # out, but nothing recorded which. `EvmSafetyDeadlineExceeded` is a TimeoutError and is
        # handled at two inner sites; anything that escapes those lands HERE and is reported as a
        # generic simulation failure rather than as a deadline.
        logger.exception("funded token safety: sell simulation raised mint=%s", mint)
        return False, "sell simulation failed (fail-closed)"
    if sim is None:
        # We could not FUND the simulation (exotic/proxy storage layout), so a revert would tell us
        # nothing. Stay fail-closed, but never report this as a honeypot: conflating "unverifiable"
        # with "proven unsellable" is exactly what blocked the whole EVM leg. Distinct + countable.
        return False, "sell simulation unverifiable (storage layout not discoverable) — fail-closed"
    if sim == _SIM_MIN_OUT_REPEATED:
        return False, ("sell_sim_min_out_repeated: fresh funded sell simulation still hit "
                       "router min-out — fail-closed")
    if sim == _SIM_REQUOTE_UNAVAILABLE:
        return False, ("sell_sim_requote_unavailable: fresh sell quote unavailable after "
                       "router min-out — fail-closed")
    if sim == _SIM_NO_SPENDER:
        return False, ("sell_sim_no_spender: sell quote named no approval spender, so the storage "
                       "layout was never probed — fail-closed")
    if sim == _SIM_RPC_UNKNOWN:
        return False, "sell_sim_rpc_unknown: funded sell simulation RPC result unavailable — fail-closed"
    if isinstance(sim, str) and sim.startswith(_SIM_REVERTED_UNKNOWN):
        detail = sim.partition("|")[2]
        suffix = f"; detail={detail}" if detail else ""
        # THE TOKEN PULL SUCCEEDING IS THE ANSWER TO THE QUESTION THIS GATE ASKS (2026-09-04).
        # `_attributed` already says it in as many words: "token_pull_ok=true means the ERC-20 layer
        # is fine and the revert came from the router path — the refusal is about our route, not the
        # coin." This gate exists to catch a coin we can buy and cannot move. A successful
        # `transferFrom` under the same overrides proves we can move it, so an unclassified revert
        # AFTER that point is a statement about the pool we quoted, not about the token.
        #
        # STOCK (0x609bc137, 2026-09-04) is the case: refused with `custom_error 0x90bfb865` whose
        # payload decodes to 0x4e346895…, the HOOK on one of its fifteen pools -- the only hooked
        # one. Fourteen others carry no hook at all. The market showed $92,332 of liquidity with 101
        # buys and 85 SELLS in five minutes, and the operator was watching the followed wallets sell
        # it. The hook rejects our synthetic state-override seller; it does not stop real ones.
        #
        # A pull that FAILS still refuses, which is the actual honeypot signature, and so does an
        # unprobeable pull (None -> no suffix). Route quality remains the quote ladder's job, and
        # the execution-premium cap and exit rails sit downstream of this.
        # NARROWED 2026-10-07 (SICAT 70895, -$50 honeypot): the pull probe moves taker -> router,
        # a V2 sell moves taker -> pair, so a token blocking its pair passes the probe. Only an
        # unnamed custom error that is not a token transfer failure keeps the admit.
        pull_admits = ((getattr(cfg, "MEMECOIN_EVM_SAFETY_PULL_OK_ADMITS", True)
                        if allow_pull_only is None else allow_pull_only)
                       and "token_pull_ok=true" in detail)
        # A REAL BUYER'S ROUND TRIP (2026-10-07): the synthetic seller was refused after the coin
        # moved into the pool, so ask the question a real buyer faces. Only a completed round trip
        # admits; unknown or failed refuses, whatever the revert said.
        if pull_admits and round_trip_fn is not None and getattr(
                cfg, "MEMECOIN_EVM_SAFETY_ROUND_TRIP", True):
            try:
                rt_ok, rt_why = round_trip_fn(mint)
            except Exception as exc:                  # a crash is unknown, never an admit
                rt_ok, rt_why = None, "round_trip_error:%s" % type(exc).__name__
            if rt_ok is True:
                return True, ("ok: the synthetic sell reverted but a real buyer's round trip "
                              f"completed ({rt_why}){suffix}")
            return False, ("sell_sim_reverted_unknown: funded sell simulation reverted with an "
                           f"unclassified cause — fail-closed; round_trip={rt_why}{suffix}")
        if pull_admits and _route_attributable(detail):
            return True, ("ok: sell route reverted but the token pull succeeded — "
                          f"route-attributed, not a honeypot{suffix}")
        return False, ("sell_sim_reverted_unknown: funded sell simulation reverted with an "
                       f"unclassified cause — fail-closed{suffix}")
    if not sim:
        # Machine-greppable token first (memecoin_entry_attempts.terminal_reason groups on it),
        # human detail after. The real bound factory returns a classified string; this legacy bool
        # branch remains only for injected seams that explicitly assert a token-caused failure.
        return False, "honeypot_sim_reverted: funded sell simulation reverted (honeypot)"
    return True, "ok"


def build_lifi_token_safety(*, code_fn):
    """Keep the contract-code gate when LI.FI supplies the route and simulation gates."""
    def check(mint):
        try:
            code = code_fn(mint)
        except Exception:
            return False, "code read failed (fail-closed)"
        if code is None:
            return False, "code read failed (fail-closed)"
        if not code or code in ("0x", "0x0", "0X"):
            return False, "not a contract (no code at address)"
        return True, "ok"

    return check


def build_evm_token_safety(*, chain_id, rpc_url, taker, quote_fn, http=None, probe_usd=None,
                           decimals_fn=None, slot_cache_size=256,
                           sell_route_label=None, slot_proof_load=None, slot_proof_store=None,
                           safety_deadline_scope=None, safety_deadline_s=8.0,
                           probe_tokens_fn=None, sell_slippage_bps=None,
                           quote_with_miss_fn=None, entry_quote_probe_fn=None, fast_quote_fn=None,
                           quote_rungs_fn=None, early_slot_hints=False, sell_spender_hint=None,
                           decimals_hint_fn=None, slot_prewarm_gate=None, permit2_slot_prewarm=False):
    """Bind the safety seams to the selected execution venue/RPC and return
    token_safety_fn(mint) -> (ok, reason).

    - code_fn: eth_getCode.
    - sell_route_fn: a nominal mint->quote-token executable quote exists.
    - sell_sim_fn: eth_call the sell quote's transaction from `taker` (classified fail-closed).
    `quote_fn` is the executor's required bound direct-venue selector.

    A funded clip check requires `bought_amount_atomic` from the actual executor quote.
    A shared mint hint cannot prove the current input size or quote identity.
    Other chains retain their existing probe sequence. Calls without a funded size remain diagnostic.
    `quote_with_miss_fn` separates provider failures from a measured route refusal.
    `entry_quote_probe_fn` remains an accepted argument for older context builders.
    `sell_spender_hint`: the spender the sell quote is known to name (a fixed router). Its slot probes
    then run beside the code read and the quote; a quote naming another spender is probed as before.
    `slot_prewarm_gate`: an optional lane-owned lock shared by job-bound safety closures, so unused
    speculative discovery stays bounded even when each job binds a new provider seam.
    `permit2_slot_prewarm`: opt in (the funded EVM leg) to finding the Permit2 slot layouts beside
    the quote on MEMECOIN_EVM_PERMIT2_SLOT_PREWARM_CHAINS; other builders keep today's reads.
    `decimals_hint_fn(mint)`: (source, value) pairs in priority order that size the one-token sell
    probe before any decimals read; the first int in 0..36 wins, anything else falls through to the
    read. The source goes to timings["decimals_source"].
    """
    from sniper.execution.evm_wallet import (
        CHAIN_QUOTE, SEL_ALLOWANCE, SEL_BALANCE_OF, SEL_TRANSFER_FROM, eth_get_code,
        erc20_decimals,
    )
    from sniper.execution.evm_real_seams import (
        _rpc_eth_call_result, eth_call_raw, usd_to_quote_atomic,
    )

    from sniper.execution.metadata_cache import BoundedLru

    chain = str(chain_id)
    _alternates = _slot_probe_alternates(rpc_url)
    _slot_cache = BoundedLru(slot_cache_size)
    _no_slot_until = {}                 # key -> monotonic expiry of a NOT-FOUND verdict
    # COVERED ALLOWANCES (Gamma (d), 2026-10-16): key -> (the live allowance a check read, the proven
    # balance slot). A balance-only override is valid only for an amount word that allowance covers; a
    # larger amount is decided again (live read, then the allowance slot). It used to be cached in
    # _slot_cache with no amount and reused for a larger sell, which then reverted: a false refusal.
    # Not handled: an allowance that shrank after it was read (exact Permit2 approvals fall per sell).
    _covered, _covered_lock = {}, threading.Lock()
    _prewarm_gate = slot_prewarm_gate if slot_prewarm_gate is not None else threading.Lock()
    # FOUND SLOT LAYOUTS (2026-10-16, Alpha GO): (chain, mint, "balance") or (chain, mint, spender)
    # -> the slot a probe proved. A layout is immutable; a not-found or a failed scan is never stored
    # (BoundedLru caches neither). A slot, never an override: the amount word and the live allowance
    # decision stay with each simulation, so nothing amount-shaped can be pinned here.
    _layout_cache = BoundedLru(slot_cache_size)

    if sell_route_label is None:
        sell_route_label = "direct_configured"

    def _proof_overrides(mint, proof, amount):
        try:
            balance_slot, allowance_slot = proof
        except (TypeError, ValueError):
            return None
        if not all(isinstance(slot, str) and slot.startswith("0x") and len(slot) == 66
                   for slot in (balance_slot, allowance_slot)):
            return None
        word = _simulation_amount_word(amount)
        if word is None:
            return None
        return {mint: {"stateDiff": {balance_slot: word, allowance_slot: word}},
                taker: {"balance": _GAS_HEX}}

    def _seed_layouts(mint, spender, proof):
        """A persisted Permit2 proof as slot LAYOUTS only (balance, allowance): the live allowance read
        still decides each override."""
        try:
            balance_slot, allowance_slot = proof
        except (TypeError, ValueError):
            return
        base = (str(chain_id), str(mint).lower())
        for which, slot in (("balance", balance_slot), (str(spender).lower(), allowance_slot)):
            if isinstance(slot, str) and slot.startswith("0x") and len(slot) == 66:
                _layout_cache.get_or_load(base + (which,), lambda slot=slot: slot)

    def _deadline_call(deadline, fn, *args):
        if safety_deadline_scope is None or deadline is None:
            return fn(*args)
        with safety_deadline_scope(deadline):
            return fn(*args)

    def _slot_finders(mint, spender, deadline):
        """(find_balance_slot, find_allowance_slot) for `mint`, `spender`: each returns the proven
        slot, from the layout cache or one scan, else None (a clean not-found)."""
        probe_scope = (lambda: safety_deadline_scope(deadline)) if (
            safety_deadline_scope is not None and deadline is not None) else None

        def scan_balance_slot():
            data = "0x" + SEL_BALANCE_OF + _pad(taker)
            return _find_slot(
                lambda key, url=rpc_url: eth_call_raw(
                    mint, data, url, http=http,
                    overrides={mint: {"stateDiff": {key: _BIG_HEX}}}, raise_on_error=True),
                lambda i: _map_slot(taker, i),
                lead_slots=_lead_slots(chain_id, mint, lambda i: _map_slot(taker, i),
                                       b20_index=4, which="balance"),
                extra_slots=(_solady_balance_slot(taker),),
                late_slots=lambda: _access_list_slots(
                    mint, data, rpc_url, http=http, taker=taker),
                early_slots=(lambda: _access_list_slots(
                    mint, data, rpc_url, http=http, taker=taker, strict=True))
                    if early_slot_hints else None,
                alternates=_alternates, probe_scope=probe_scope)

        def scan_allowance_slot():
            data = "0x" + SEL_ALLOWANCE + _pad(taker) + _pad(spender)
            return _find_slot(
                lambda key, url=rpc_url: eth_call_raw(
                    mint, data, url, http=http,
                    overrides={mint: {"stateDiff": {key: _BIG_HEX}}},
                    raise_on_error=True),
                lambda i: _nested_slot(spender, taker, i),
                lead_slots=_lead_slots(chain_id, mint, lambda i: _nested_slot(spender, taker, i),
                                       b20_index=5, which="allowance"),
                extra_slots=(_solady_allowance_slot(spender, taker),),
                late_slots=lambda: _access_list_slots(
                    mint, data, rpc_url, http=http, taker=taker),
                early_slots=(lambda: _access_list_slots(
                    mint, data, rpc_url, http=http, taker=taker, strict=True))
                    if early_slot_hints else None,
                alternates=_alternates, probe_scope=probe_scope)

        base = (str(chain_id), str(mint).lower())
        return (lambda: _layout_cache.get_or_load(base + ("balance",), scan_balance_slot),
                lambda: _layout_cache.get_or_load(base + (str(spender).lower(),),
                                                  scan_allowance_slot))

    def _fund_overrides(mint, spender, amount, *, deadline=None, before_allowance_scan=None):
        """Build an eth_call state-override that gives `taker` a balance of `mint` and an allowance
        for `spender`. Solidity lays a mapping out at keccak(pad(key) . pad(slotIndex)); allowance is
        the nested keccak(pad(spender) . keccak(pad(owner) . pad(slotIndex))). The slot INDEX is not
        introspectable, so we discover it: override a candidate slot, then read balanceOf/allowance
        back and see if our value comes out. Standard ERC-20s land at 0/1 within a few probes; proxy
        and Vyper layouts may not be found at all -> None (unverifiable, never 'honeypot')."""
        word = _simulation_amount_word(amount)
        if word is None:
            return None

        def discover():
            # _find_slot raises on transport or malformed responses. BoundedLru never catches or
            # stores loader failures, and it never caches None, so only a clean completed not-found
            # reaches the TTL below — and it EXPIRES rather than branding the token for the process.
            find_balance_slot, find_allowance_slot = _slot_finders(mint, spender, deadline)
            existing_allowance = None
            if before_allowance_scan is not None:
                # Codex F4: the prewarmed allowance layout is waited on only when a scan is needed.
                _scan_allowance = find_allowance_slot

                def find_allowance_slot():
                    before_allowance_scan()
                    return _scan_allowance()

            if permit2:
                def _allowance_word(_key, url=rpc_url):
                    # THE PARSE LIVES INSIDE THE RETRIED UNIT (2026-09-05). LIGER 0x... was
                    # refused "sell simulation failed" at 23:14:51 after endpoint rotation
                    # shipped, because this read parsed OUTSIDE the retry: one node answered
                    # `0x`, int() raised, and the ValueError became a transport error with no
                    # second endpoint asked. Same defect as the slot probe, one site over.
                    got = eth_call_raw(
                        mint, "0x" + SEL_ALLOWANCE + _pad(taker) + _pad(spender),
                        url, http=http, raise_on_error=True)
                    try:
                        return int(got, 16)
                    except (TypeError, ValueError) as e:
                        raise _MalformedProbe("malformed allowance result") from e

                def allowance_proof():
                    try:
                        existing = _read_with_retry(
                            _allowance_word, None, alternates=_alternates)
                    except (EvmSafetyDeadlineExceeded, RequestNotSent):
                        raise
                    except Exception as exc:
                        raise _SlotScanTransportError(str(exc)) from exc
                    return existing, (None if existing >= int(word, 16) else find_allowance_slot())

                executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="evm-safety-slot")
                try:
                    bal_future = executor.submit(_deadline_call, deadline, find_balance_slot)
                    allow_future = executor.submit(_deadline_call, deadline, allowance_proof)
                    bal_slot = bal_future.result()  # Balance failures retain their original priority.
                    existing_allowance, allow_slot = allow_future.result()
                    if existing_allowance >= int(word, 16):
                        if bal_slot is not None:
                            raise _Permit2Decided(existing_allowance, bal_slot)
                        allow_slot = _deadline_call(deadline, find_allowance_slot)
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
            else:
                with ThreadPoolExecutor(
                        max_workers=2, thread_name_prefix="evm-safety-slot") as executor:
                    bal_future = executor.submit(
                        _deadline_call, deadline, find_balance_slot)
                    allow_future = executor.submit(
                        _deadline_call, deadline, find_allowance_slot)
                    bal_slot = bal_future.result()
                    allow_slot = allow_future.result()
            if bal_slot is None or allow_slot is None:
                return None                 # BoundedLru never caches None -> the TTL below owns it
            proof = (bal_slot, allow_slot)
            if slot_proof_store is not None:
                try:
                    slot_proof_store(key, proof)
                except Exception:
                    pass                      # persistence can never block a safety decision
            if permit2:
                raise _Permit2Decided(existing_allowance, bal_slot, allow_slot)
            return _proof_overrides(mint, proof, amount)

        # chain_id in the key: the cache instance is per-chain today, but the same 0x address
        # can have different storage layouts on different chains — enforce it structurally.
        key = (str(chain_id), mint.lower(), spender.lower())
        # PERMIT2: THE LIVE ALLOWANCE DECIDES EVERY OVERRIDE (Gamma (d) 2026-10-16; Alpha 2026-10-17,
        # refining a1c6db310's "never widen": never widen WITHOUT a read, so warm agrees with cold).
        # An amount word inside the allowance this key last read is balance-only with no read; any
        # larger word makes one live allowance read, then balance-only if it now covers, else both
        # slots. No Permit2 override ever comes from a cached or persisted proof alone: those seed the
        # slot LAYOUTS only. Not handled: an allowance that shrank after it was read (exact Permit2
        # approvals fall with each sell); the chain still decides that simulation.
        permit2 = str(spender).lower() == _PERMIT2
        covered = _covered.get(key) if permit2 else None
        if covered is not None and covered[0] >= int(word, 16):
            return {mint: {"stateDiff": {covered[1]: word}}, taker: {"balance": _GAS_HEX}}
        now = _now()
        # A FOUND layout is immutable and stays cached forever. A NOT-FOUND verdict is a statement
        # about on-chain state at one instant, and it used to be permanent for the runner process:
        # one clean miss branded the token unverifiable until restart, so a coin seen again minutes
        # later was refused instantly WITHOUT re-probing. On 4663 the entries are fresh minimal-proxy
        # clones, so "not discoverable yet" and "not discoverable" are not the same claim.
        # Verified 2026-08-12 on 0xa3b6aee9…: refused twice, yet `_find_slot` resolves BOTH slots
        # against the real taker and both routers minutes later.
        expiry = _no_slot_until.get(key)
        if expiry is not None:
            if now < expiry:
                return None
            _no_slot_until.pop(key, None)
        def load_or_discover():
            if slot_proof_load is not None:
                if permit2:
                    try:
                        _seed_layouts(mint, spender, slot_proof_load(key))
                    except Exception:
                        pass                  # a layout seed can never block a safety decision
                else:
                    try:
                        persisted = _proof_overrides(mint, slot_proof_load(key), amount)
                    except Exception:
                        persisted = None
                    if persisted is not None:
                        return persisted
            return discover()

        try:
            overrides = _slot_cache.get_or_load(key, load_or_discover)
        except _Permit2Decided as hit:
            with _covered_lock:
                if key not in _covered and len(_covered) >= _NO_SLOT_MAX:
                    _covered.pop(next(iter(_covered)), None)   # bound it; oldest first
                _covered[key] = (hit.allowance, hit.balance_slot)
            diff = {hit.balance_slot: word}
            if hit.allowance_slot is not None:
                diff[hit.allowance_slot] = word
            return {mint: {"stateDiff": diff}, taker: {"balance": _GAS_HEX}}
        if overrides is None:
            for stale in [k for k, until in _no_slot_until.items() if until <= now]:
                _no_slot_until.pop(stale, None)          # opportunistic prune; keeps the map small
            if len(_no_slot_until) < _NO_SLOT_MAX:
                _no_slot_until[key] = now + _NO_SLOT_TTL_S
            return None
        # Cache proven slots, never a prior probe's quantity or a mutable caller result.
        return {address: ({"stateDiff": {slot: word for slot in state["stateDiff"]}}
                          if "stateDiff" in state else dict(state))
                for address, state in overrides.items()}
    q = CHAIN_QUOTE.get(chain) or {}
    quote_token = q.get("token")
    if probe_usd is None:
        probe_usd = 5.0

    def code_fn(mint):
        return eth_get_code(mint, rpc_url, http=http)

    def _token_safety_fn(mint, deadline_expired=None, deadline=None, usd=None, bought_amount_atomic=None,
                         timings=None, block_tag=None, probe_out=None, exit_hint=None):
        # Executable venue quotes never survive an attempt. The route and simulation share one
        # sell quote. A later event must fetch a fresh transaction and route.
        quote_cache = {}
        prewarm = {}                    # the hinted spender's slot discovery, when started
        # COHERENT STATE (Ink Task 5a, 2026-09-29). With `block_tag`, the code read and every
        # simulation run AT that block, so the verdict describes one state. A read endpoint that
        # cannot serve the block is "state not ready", never a token verdict. Slot LAYOUT
        # discovery stays unpinned: a layout does not change between blocks, and it is cached.
        at = block_tag or "latest"

        def code_read(mint):
            if block_tag is None:
                return code_fn(mint)
            from sniper.execution.evm_real_seams import _block_unavailable
            from sniper.execution.evm_wallet import _rpc
            try:
                answer = _rpc(rpc_url, "eth_getCode", [mint, at], http=http)
            except Exception:
                return None
            error = answer.get("error")
            if error:
                message = error.get("message") if isinstance(error, dict) else str(error)
                if _block_unavailable(message):
                    quote_cache["block_unavailable"] = "code: %s" % str(message)[:80]
                return None
            return answer.get("result")

        def _timed(key, fn, *args, **kwargs):
            """DISPLAY ONLY (2026-09-28): add this call's wall time to `timings[key]` in ms. The
            result and any exception pass through untouched; no timings dict, no timing."""
            if timings is None:
                return fn(*args, **kwargs)
            started = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                timings[key] = timings.get(key, 0.0) + (time.perf_counter() - started) * 1000.0

        def quote_sell(amount, *, fast=True):
            """One sell-probe quote. With `quote_rungs_fn`, its amount, time, hit and the rungs that
            ran (with each miss reason) go to timings["sell_probes"] (2026-10-07: chain 1 refused
            five entries and nothing said what the probe tried). Display only."""
            if quote_rungs_fn is None:
                return _probed(amount, _quote_sell(amount, fast=fast))
            quote_rungs_fn()                     # this thread's earlier rungs are not this probe's
            started = time.perf_counter()
            quote = _quote_sell(amount, fast=fast)
            try:
                ran = [r for r in quote_rungs_fn() or ()
                       if isinstance(r, dict) and not r.get("skipped") and not r.get("unavailable")]
                quote_cache["ran_rungs"] = [str(r.get("rung")) for r in ran]
                if timings is not None:
                    timings.setdefault("sell_probes", []).append({
                        "amount": str(amount), "hit": quote is not None,
                        "ms": round((time.perf_counter() - started) * 1000.0, 1),
                        "rungs": [_rung_note(r) for r in ran]})
            except Exception:
                logger.exception("safety sell probe record failed mint=%s", mint)
            return _probed(amount, quote)

        def _probed(amount, quote):
            """THE CALLER'S OWN AMOUNT (review EVM06, 2026-10-08): a hit's sell amount goes into this
            call's `probe_out`, so a caller that checks the route further (the 4663 fast lane's
            bridge leg) binds the amount THIS verdict simulated. The last hit wins, which is the
            refresh requote's when the first sell reverted."""
            if probe_out is not None and quote is not None:
                probe_out["amount"] = int(amount)
            return quote

        def _quote_sell(amount, *, fast=True):
            kwargs = ({"slippage_bps": int(sell_slippage_bps)}
                      if sell_slippage_bps is not None else {})
            if exit_hint is not None:
                kwargs["exit_hint"] = exit_hint      # the receipt's pool, first in the probe ladder
            if quote_with_miss_fn is not None and clip_check_enabled():
                quote, miss_code = quote_with_miss_fn(mint, quote_token, amount, **kwargs)
                quote_cache["last_miss_code"] = miss_code
                return quote
            # A successful speculative route still needs the same funded simulation.
            # Its existing revert recheck uses the ordinary priority ladder below.
            fn = fast_quote_fn if fast and fast_quote_fn is not None else quote_fn
            return fn(mint, quote_token, amount, **kwargs)

        def clip_check_enabled():
            # Per-chain allowlist (default {"5042"}): a chain NOT listed keeps the pre-existing
            # shape byte-for-byte -- no warm-up quote, and a clip-known miss still widens to the
            # smaller probes below, exactly as before this check existed. No hook-cap evidence was
            # found on 4663 (2026-09-16); widen this set only on a chain's own live evidence.
            return chain in getattr(cfg, "MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS", ())

        def known_bought_amount():
            if bought_amount_atomic is not None:
                try:
                    return max(0, int(bought_amount_atomic))
                except (TypeError, ValueError, OverflowError):
                    return 0
            if usd is not None and clip_check_enabled():
                return 0
            try:
                return int(probe_tokens_fn(mint) or 0) if probe_tokens_fn else 0
            except (TypeError, ValueError, OverflowError):
                return 0

        def sell_quote(*, refresh=False):
            if refresh:
                quote_cache.pop("sell", None)
                quote_cache.pop("clip_probe_tokens", None)
                quote_cache.pop("clip_miss_code", None)
            if "sell" in quote_cache:
                return quote_cache["sell"]
            probe_tokens = known_bought_amount()
            if probe_tokens > 0 and clip_check_enabled():
                # THE EXPECTED BOUGHT AMOUNT IS KNOWN, on a chain this check covers: quote its exit
                # at that exact size and stop there. A miss here is a fact about the position we
                # would actually hold -- it must not be widened away by falling back to a 1-token
                # or $5 probe whose dust output can pass while a real clip-sized exit cannot (live
                # false-negative, 2026-09-16).
                quote = quote_sell(probe_tokens)
                quote_cache["sell"] = quote
                quote_cache["clip_probe_tokens"] = probe_tokens
                if quote is None:
                    quote_cache["clip_miss_code"] = quote_cache.get("last_miss_code")
                return quote
            if clip_check_enabled() and (usd is not None or bought_amount_atomic is not None):
                quote_cache["sell"] = None
                quote_cache["clip_probe_tokens"] = 0
                quote_cache["clip_miss_code"] = "provider_quote_quantity_unavailable"
                return None
            # Other chains and diagnostics retain their existing probe sequence.
            quote = quote_sell(probe_tokens, fast=not refresh) if probe_tokens > 0 else None
            if quote is None:
                if "decimals" not in quote_cache:
                    dec, source = _hinted_decimals()
                    if source is None:
                        source = "host" if decimals_fn is None else "decimals_fn"
                        try:
                            dec = (decimals_fn(mint) if decimals_fn is not None
                                   else erc20_decimals(mint, rpc_url, http=http))
                            dec = int(dec) if dec is not None else None
                            if dec is not None and dec < 0:
                                dec = None
                        except (TypeError, ValueError):
                            dec = None
                    quote_cache["decimals"] = dec
                    if timings is not None:
                        timings["decimals_source"] = source
                dec = quote_cache["decimals"]
                quote = None if dec is None else quote_sell(10 ** dec, fast=not refresh)
            if quote is None:
                probe_atomic = usd_to_quote_atomic(chain, probe_usd, http=http)
                buy = quote_fn(quote_token, mint, probe_atomic) if probe_atomic else None
                try:
                    probe_tokens = int((buy or {}).get("buyAmount") or 0)
                except (TypeError, ValueError):
                    probe_tokens = 0
                if probe_tokens > 0:
                    quote = quote_sell(probe_tokens, fast=not refresh)
            quote_cache["sell"] = quote
            return quote

        def _hinted_decimals():
            """(decimals, source) from the hint's first int in 0..36, else (None, None): read."""
            if decimals_hint_fn is None:
                return None, None
            try:
                candidates = list(decimals_hint_fn(mint) or ())
            except Exception:
                logger.exception("safety decimals hint failed mint=%s", mint)
                return None, None
            for source, value in candidates:
                if type(value) is int and 0 <= value <= 36:
                    return value, source
            return None, None

        def sell_route_fn(_mint):
            return sell_quote() is not None

        def _round_trip(_mint):
            """(True, why) when one eth_simulateV1 from the taker -- buy through the entry
            quote, approve, sell the received amount through the exit quote -- completes and
            returns at least 1 - MEMECOIN_EVM_MAX_TAX_PCT of what it paid; (False, why) when
            it ran and failed; (None, why) when it could not run (unknown: refuses).
            FAILURE PATH ONLY: called after the synthetic sell reverted and the coin moved
            into the sale's destination (spec 2026-10-07 EVM entry safety, section 1)."""
            from sniper.execution.evm_wallet import is_native_quote_token, native_is_quote
            if native_is_quote(chain):
                return None, "native_quote_unmeasured"     # proceeds are native: no balanceOf
            big = "f" * 64

            def call(to, data, value=0):
                return {"from": taker, "to": to, "input": data, "value": hex(int(value))}

            def balance(token):
                return call(token, "0x" + SEL_BALANCE_OF + _pad(taker))

            def quote_calls(q, sell_token):
                tx = (q or {}).get("transaction") or {}
                out = []
                spender = (((q or {}).get("issues") or {}).get("allowance") or {}).get("spender")
                if spender:
                    out.append(call(sell_token, "0x095ea7b3" + _pad(spender) + big))
                for pre in (q or {}).get("pre_transactions") or ():
                    out.append(call(pre["to"], pre.get("data") or "0x",
                                    int(str(pre.get("value") or "0"), 0)))
                out.append(call(tx["to"], tx.get("data") or "0x",
                                int(str(tx.get("value") or "0"), 0)))
                return out

            def simulate(overrides, calls):
                member = simulate_member(rpc_url, http=http)
                if member is None:
                    return None
                try:
                    answer = _post(member, "eth_simulateV1", [{"blockStateCalls": [
                        {"stateOverrides": overrides, "calls": calls}], "validation": False}, at],
                        http)
                except Exception:
                    return None
                rows = ((answer or {}).get("result") or [{}])[0].get("calls")
                return rows if isinstance(rows, list) and len(rows) == len(calls) else None

            def word(row):
                data = str((row or {}).get("returnData") or "")
                return int(data, 16) if data.startswith("0x") and len(data) >= 66 else None

            # THE CLIP, NOT THE PROBE (Gamma MEDIUM on 49580fc8a): a max-sell or anti-whale limit
            # in the swap path passes a $5 round trip and blocks our clip-size exit.
            paid = usd_to_quote_atomic(chain, usd or probe_usd, http=http) if quote_token else None
            if not paid or paid <= 0:
                return None, "unpriced"
            buy = quote_fn(quote_token, mint, paid)
            if not ((buy or {}).get("transaction") or {}).get("to"):
                return None, "no_buy_route"
            overrides = {taker: {"balance": hex(10 ** 22)}}
            if is_native_quote_token(quote_token):
                funding = [call(quote_token, "0xd0e30db0", paid)]     # WETH.deposit()
            else:
                spender = ((buy.get("issues") or {}).get("allowance") or {}).get("spender") \
                    or buy["transaction"]["to"]
                funded = _fund_overrides(quote_token, spender, paid, deadline=deadline)
                if funded is None:
                    return None, "unfundable"
                overrides.update(funded)
                funding = []
            buy_calls = funding + quote_calls(buy, quote_token)
            first = simulate(overrides, [balance(mint)] + buy_calls + [balance(mint)])
            if first is None:
                return None, "rpc_unknown"
            if any(int(str(r.get("status") or "0"), 16) != 1 for r in first):
                return False, "buy_reverted"
            start, end = word(first[0]), word(first[-1])
            got = None if start is None or end is None else end - start
            if not got or got <= 0:
                return False, "bought_nothing"
            sell = quote_fn(mint, quote_token, got)
            if not ((sell or {}).get("transaction") or {}).get("to"):
                return None, "no_sell_route"
            second = simulate(overrides, buy_calls + [balance(quote_token)]
                              + quote_calls(sell, mint) + [balance(quote_token)])
            if second is None:
                return None, "rpc_unknown"
            if any(int(str(r.get("status") or "0"), 16) != 1 for r in second):
                return False, "sell_reverted_for_a_real_buyer"
            before, after = word(second[len(buy_calls)]), word(second[-1])
            if before is None or after is None:
                return None, "unreadable_balance"
            back = after - before
            floor = 1.0 - float(getattr(cfg, "MEMECOIN_EVM_MAX_TAX_PCT", 15.0)) / 100.0
            if back < paid * floor:
                return False, "returned %.1f%% of paid" % (100.0 * back / paid)
            if probe_out is not None:
                # THE ROUND TRIP DECIDED THIS PASS, so its clip-size sell is the amount the verdict
                # simulated (Gamma on 9b3223d0c; what the old shared `sold` held on this path).
                probe_out["amount"] = int(got)
            return True, "returned %.1f%% of paid" % (100.0 * back / paid)

        def sell_sim_fn(_mint):
            def simulate(quote):
                tx = (quote or {}).get("transaction")
                if not tx:
                    return _SIM_REVERTED_UNKNOWN, None
                allowance = ((quote or {}).get("issues") or {}).get("allowance") or {}
                spender = allowance.get("spender") or tx.get("to")
                if not spender:
                    # DISTINCT from an undiscoverable layout: the quote named no spender, so the
                    # layout was never probed at all. Both used to return the same `None` and print
                    # the same reason, which is why the 2026-08-12 0xa3b6aee9… refusals could not be
                    # attributed from the stored row.
                    return _SIM_NO_SPENDER, None
                future = prewarm.get("slots")
                if future is not None and str(spender).lower() == prewarm["spender"]:
                    # The same discovery, started beside the quote. A deadline raises here, where
                    # today's discovery would have raised it. A transport failure (after its own
                    # retries) does not refuse: today's discovery below asks once more (Gamma LOW).
                    try:
                        _timed("slot_wait_ms", future.result)
                    except _SlotScanTransportError:
                        pass
                permit2_layouts = str(spender).lower() == _PERMIT2

                def await_layout(name, timed=True):
                    # A Permit2 layout found beside the quote. A deadline raises here as the
                    # discovery below would; any other failure is asked again below.
                    future = prewarm.get(name) if permit2_layouts else None
                    if future is None:
                        return
                    try:
                        _timed("slot_wait_ms", future.result) if timed else future.result()
                    except (EvmSafetyDeadlineExceeded, RequestNotSent):
                        raise
                    except Exception:
                        pass
                # THE LIVE ALLOWANCE DECIDES WHICH LAYOUT IS AWAITED (Codex F4, 2026-10-17): the
                # balance layout always; the allowance layout only when the live allowance does not
                # cover the amount (inside _fund_overrides, before its own scan). A covering
                # allowance never waits on, or fails with, a scan the cold path would not run, also
                # when the balance layout failed and discovery below scans it again (Codex F4
                # verification: joining the allowance layout first ate the deadline before any live
                # allowance read). That re-scan may overlap the still-running allowance prewarm.
                await_layout("layout_balance")
                overrides = _timed("slot_discovery_ms", _fund_overrides,
                                   mint, spender, (quote or {}).get("sellAmount"), deadline=deadline,
                                   before_allowance_scan=(
                                       (lambda: await_layout("layout_allowance", timed=False))
                                       if prewarm.get("layout_allowance") is not None else None))
                if overrides is None:
                    return None, None
                status, kind, detail = _timed(
                    "simulation_ms", _rpc_eth_call_result,
                    tx, taker, rpc_url, http=http, overrides=overrides, block_tag=at,
                )
                if status is True:
                    return True, None
                if status is None:
                    if kind == "block_unavailable":
                        quote_cache["block_unavailable"] = "simulation: %s" % detail
                    return _SIM_RPC_UNKNOWN, None
                detail = " ".join(str(detail or "").split())[:120]
                return (f"{_SIM_REVERTED_UNKNOWN}|{detail}"
                        if detail else _SIM_REVERTED_UNKNOWN), kind

            def _sale_destination(quote):
                """Where the SALE moves the token: a V2 pair, a V3 pool, the V4 PoolManager, a
                curve. None when it cannot be named, so the probe makes no move at all (2026-10-07,
                Alpha/operator: SICAT's pull to the router passed while its pair refused).
                Failure path only: the success path never reads a factory."""
                route = (quote or {}).get("route")
                if not isinstance(route, dict):
                    return None
                protocol = str(route.get("protocol") or "")
                if protocol.startswith("uniswap_v4"):
                    from sniper.execution.evm_v4_quote import V4
                    return (V4.get(chain) or {}).get("pool_manager")
                if protocol in ("pons_v2", "launchpad_curve"):
                    return route.get("curve")
                tokens = route.get("tokens") or []
                if protocol == "uniswap_v2":
                    lookup = "e6a43905" + _pad(tokens[0]) + _pad(tokens[1]) if len(tokens) >= 2 else None
                elif protocol in ("uniswap_v3", "pancake_v3"):
                    fees = route.get("fees") or []
                    lookup = ("1698ee82" + _pad(tokens[0]) + _pad(tokens[1]) + "%064x" % int(fees[0])
                              if len(tokens) >= 2 and fees else None)
                else:
                    lookup = None                      # ramses (tick spacing), mixed, unknown
                router = route.get("router") or ((quote or {}).get("transaction") or {}).get("to")
                if lookup is None or not router:
                    return None
                factory = eth_call_raw(router, "0xc45a0155", rpc_url, http=http, block_tag=at)
                if not factory or len(factory) < 66 or int(factory, 16) == 0:
                    return None
                found = eth_call_raw("0x" + factory[-40:], "0x" + lookup, rpc_url, http=http,
                                     block_tag=at)
                if not found or len(found) < 66 or int(found, 16) == 0:
                    return None
                return "0x" + found[-40:]

            def _token_moves(quote):
                """FAILURE PATH ONLY. Can the token be pulled at all, under the SAME overrides?

                `TRANSFER_FROM_FAILED` out of a router simulation reads as "unsellable" and refuses
                the entry, but it cannot distinguish the TOKEN rejecting the pull from something
                later in the router's own execution. Measured by hand on 2026-09-02 against the
                three refused mints holding stored slot proofs: the overrides landed, a plain
                `transfer` succeeded, and `transferFrom` by the named spender succeeded at 1e18,
                1e23 and 1e24 — the tokens were sellable and the refusals were false.

                One extra `eth_call`, ONLY when we are already refusing (20 refusals over 15 mints
                since 2026-09-01). It never runs on the success path, never gates, and returns None
                on any doubt so a probe failure can only ever leave the verdict where it was.
                """
                try:
                    tx = (quote or {}).get("transaction") or {}
                    allowance = ((quote or {}).get("issues") or {}).get("allowance") or {}
                    spender = allowance.get("spender") or tx.get("to")
                    amount = int((quote or {}).get("sellAmount") or 0)
                    if not spender or amount <= 0:
                        return None
                    destination = _timed("destination_ms", _sale_destination, quote)
                    if not destination:
                        return None
                    overrides = _fund_overrides(mint, spender, amount, deadline=deadline)
                    if overrides is None:
                        return None
                    # taker -> the sale's destination, by the spender: the move the sale makes. A
                    # move to the router passed SICAT, whose pair refused every real sale.
                    data = ("0x" + SEL_TRANSFER_FROM + _pad(taker) + _pad(destination)
                            + "%064x" % amount)
                    status, _kind, _detail = _rpc_eth_call_result(
                        {"to": mint, "data": data, "value": 0}, spender,
                        rpc_url, http=http, overrides=overrides, block_tag=at)
                    return status if status in (True, False) else None
                except Exception:
                    return None

            def _attributed(reason, quote):
                moves = _token_moves(quote)
                if moves is None:
                    return reason
                # `token_pull_ok=true` means the ERC-20 layer is fine and the revert came from the
                # router path — the refusal is about our route, not the coin.
                return "%s (token_pull_ok=%s)" % (reason, "true" if moves else "false")

            first_quote = sell_quote()
            result, kind = simulate(first_quote)
            if not isinstance(result, str) or not result.startswith(_SIM_REVERTED_UNKNOWN):
                return result
            first_kind = kind
            fresh = _timed("sell_quote_ms", sell_quote, refresh=True)
            if not fresh:
                return (_SIM_REQUOTE_UNAVAILABLE if first_kind == "min_out"
                        else _attributed(result, first_quote))
            result, kind = simulate(fresh)
            if (isinstance(result, str) and result.startswith(_SIM_REVERTED_UNKNOWN)
                    and kind == "min_out"):
                return _SIM_MIN_OUT_REPEATED
            if isinstance(result, str) and result.startswith(_SIM_REVERTED_UNKNOWN):
                return _attributed(result, fresh)
            return result

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="evm-safety-read") as executor:
            code_future = executor.submit(_timed, "code_ms", _deadline_call, deadline, code_read, mint)
            route_future = executor.submit(_timed, "sell_quote_ms", _deadline_call, deadline,
                                           sell_quote)
            # Never for Permit2: its branch caches a balance-only override when an existing
            # allowance covers the probe amount, which an amount-1 prewarm would pin (Gamma LOW).
            if (sell_spender_hint and str(sell_spender_hint).lower() != _PERMIT2
                    and _prewarm_gate.acquire(blocking=False)):
                # BESIDE THE QUOTE (BSC cut 3, 2026-10-14): the hinted spender's balance and
                # allowance slots. Layout discovery only (the amount word is re-made per call), into
                # the same cache the simulation reads; it never decides anything by itself.
                prewarm["spender"] = str(sell_spender_hint).lower()
                def discover_hinted_slots():
                    started = time.perf_counter()
                    try:
                        return _deadline_call(deadline, lambda: _fund_overrides(
                            mint, prewarm["spender"], 1, deadline=deadline))
                    finally:
                        # Private until complete: an unused future cannot mutate published timings.
                        prewarm["slot_discovery_ms"] = (time.perf_counter() - started) * 1000.0
                warm_executor = None
                try:
                    warm_executor = ThreadPoolExecutor(max_workers=1,
                                                       thread_name_prefix="evm-safety-prewarm")
                    prewarm["slots"] = warm_executor.submit(discover_hinted_slots)
                except Exception:
                    _prewarm_gate.release()
                    raise
                finally:
                    if warm_executor is not None:
                        warm_executor.shutdown(wait=False)
                prewarm["slots"].add_done_callback(lambda _future: _prewarm_gate.release())
                # A known refusal does not need these bounded provider reads. A simulation still
                # waits for its own future above; the gate prevents refusal bursts piling up jobs.
            elif (permit2_slot_prewarm and chain in cfg.memecoin_evm_permit2_slot_prewarm_chains()
                  and _prewarm_gate.acquire(blocking=False)):
                # PERMIT2 LAYOUTS BESIDE THE QUOTE (2026-10-16, Alpha GO;
                # MEMECOIN_EVM_PERMIT2_SLOT_PREWARM_CHAINS). 4663 V4 sells name Permit2, so the slot
                # step ran serially after the quote (0078cc00: 103 ms). Only the balance slot and the
                # Permit2 allowance slot LAYOUTS are found here, into the layout cache. The live
                # allowance read, the amount word and the override stay with the simulation, so no
                # amount can be pinned (Gamma LOW on c693e19fd).
                finders = _slot_finders(mint, _PERMIT2, deadline)
                started, left, left_lock = time.perf_counter(), [2], threading.Lock()

                def layout_done(_future):
                    with left_lock:
                        left[0] -= 1
                        last = left[0] == 0
                    if last:
                        prewarm["layout_ms"] = (time.perf_counter() - started) * 1000.0
                        _prewarm_gate.release()
                warm_executor = None
                try:
                    warm_executor = ThreadPoolExecutor(max_workers=2,
                                                       thread_name_prefix="evm-safety-layout")
                    jobs = [warm_executor.submit(_deadline_call, deadline, find) for find in finders]
                except Exception:
                    _prewarm_gate.release()
                    raise
                finally:
                    if warm_executor is not None:
                        warm_executor.shutdown(wait=False)
                prewarm["layout_balance"], prewarm["layout_allowance"] = jobs
                for job in jobs:
                    job.add_done_callback(layout_done)
            result = token_safety_ok_evm(
                mint, code_fn=lambda _mint: code_future.result(),
                sell_route_fn=lambda _mint: route_future.result() is not None,
                sell_sim_fn=sell_sim_fn,
                sell_route_label=lambda: "+".join(quote_cache.get("ran_rungs") or ()) or sell_route_label,
                # BSC and Ink require a completed sell transaction. A transferable token can
                # still have no executable swap; an unknown router failure is not admission.
                allow_pull_only=False if chain in {"56", "57073"} else None,
                round_trip_fn=lambda _mint: _timed("round_trip_ms", _round_trip, _mint),
            )
        if timings is not None and prewarm.get("slots") is not None and prewarm["slots"].done():
            timings["slot_discovery_ms"] = prewarm["slot_discovery_ms"]
        if timings is not None and "layout_ms" in prewarm:
            timings["slot_layout_ms"] = prewarm["layout_ms"]
        ok, reason = result
        if block_tag is not None and quote_cache.get("block_unavailable"):
            return False, "state_not_ready: block %s is unavailable on the read endpoint (%s)" % (
                block_tag, quote_cache["block_unavailable"])
        if (not ok and "clip_miss_code" in quote_cache
                and str(reason).startswith(EVM_ROUTE_UNAVAILABLE_PREFIX)):
            miss_code = quote_cache["clip_miss_code"]
            probe_tokens = quote_cache.get("clip_probe_tokens")
            if _quote_miss_is_blind(miss_code):
                result = (False, "safety_blind_exit_quote: could not verify the sell at the "
                          "expected bought amount (%s atomic); %s — fail-closed, unverified"
                          % (probe_tokens, miss_code or "no classifier"))
            else:
                result = (False, "%s: no sell route at the expected bought amount "
                          "(%s atomic); %s" % (EVM_EXIT_UNQUOTABLE_AT_CLIP, probe_tokens,
                                               miss_code or "no route"))
        if deadline_expired is not None and deadline_expired():
            return False, "safety_deadline_exceeded"
        if result[0]:
            _maybe_schedule_buy_tax_shadow(chain, quote_cache.get("sell"))
        return result

    def token_safety_fn(mint, *, deadline=None, usd=None, bought_amount_atomic=None, timings=None,
                        block_tag=None, probe_out=None, exit_hint=None):
        """`timings`: an optional dict that receives code_ms, sell_quote_ms, slot_discovery_ms and
        simulation_ms for the `evm.token_safety` trace substage. Display only; the verdict is the
        same with or without it. `block_tag`: pin the code read and simulations to one block
        (Ink's coherent state); None keeps every read at "latest", as before. `probe_out`: an
        optional dict that receives this call's sell-probe amount ("amount", atomic) on a hit.
        `exit_hint`: the V4 pool on the source's own receipt (evm_source_route.safety_exit_hint),
        quoted first by the sell probe; None is today's probe."""
        if safety_deadline_scope is None:
            return _token_safety_fn(mint, usd=usd, bought_amount_atomic=bought_amount_atomic,
                                    timings=timings, block_tag=block_tag, probe_out=probe_out,
                                    exit_hint=exit_hint)
        if deadline is None:
            deadline = _now() + float(safety_deadline_s)
        with safety_deadline_scope(deadline) as deadline_expired:
            return _token_safety_fn(mint, deadline_expired, deadline, usd=usd,
                                    bought_amount_atomic=bought_amount_atomic, timings=timings,
                                    block_tag=block_tag, probe_out=probe_out, exit_hint=exit_hint)

    # The mixed-hop exit proof simulates a sell of a coin we do not hold yet, so it needs the SAME
    # balance/allowance state override the sell simulation above builds. Exposed, not duplicated:
    # a second slot-discovery implementation is a second place for a layout to be read wrong.
    token_safety_fn.fund_overrides = _fund_overrides
    return token_safety_fn
