"""Chain 1 (Ethereum) through Flashbots Protect: the post-send status watch and the fee headroom.

2026-10-08 (Alpha GO on Beta's diagnosis): both funded chain-1 swaps (pending 4945/4947) expired
unmined. Flashbots Protect's status API named them FAILED with simError MaxFeePerGasTooLow and
isRevert false, while the same wallet's WETH unwraps on the same endpoint, at a LOWER maxFee and the
same 0.05 gwei tip, were INCLUDED, and the base fee never reached the first swap's maxFee. The cause
is not proven.

P0, STATUS WATCH, always on: every chain-1 transaction the private relay accepts is asked
https://protect.flashbots.net/tx/<hash> once a block on a daemon thread, off the fill path, until a
terminal status. Each change is logged and appended to data/chain1_protect_status.jsonl; the first
simError on a pending transaction pages its current warning; final inclusion remains unknown.
Read-only, keyless.

P1, FEE HEADROOM, behind MEMECOIN_CHAIN1_FEE_HEADROOM (default off): a warm eth_feeHistory read
every 12 s gives the next block's base fee and the p75 of the last 5 blocks' median tips. Signing on
chain 1 then uses tip = clamp(max(MEMECOIN_EVM_TIP_GWEI_1, p75), MEMECOIN_EVM_TIP_CAP_GWEI_1) and
maxFee = max(today's, min(2 x base + tip, per-tx gas reserve / gas limit)). The maxFee never falls
below today's; gas limit x maxFee never exceeds the native gas reserve the entry gate already held
back (min of MEMECOIN_EVM_GAS_ENTRY_NATIVE_1 and ..._EXIT_NATIVE_1); data older than 30 s is not
used (today's fees).
"""
from __future__ import annotations

import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

STATUS_URL = "https://protect.flashbots.net/tx/%s"
POLL_S = 12.0
MAX_POLLS = 32                       # Protect keeps a transaction 25 blocks, plus a margin
MAX_WATCHERS = 16
TERMINAL = frozenset({"INCLUDED", "FAILED", "CANCELLED"})
FEE_PERIOD_S = 12.0
FEE_STALE_S = 30.0
_FEE_BLOCKS = 5


# ---- P0: the status watch ----

_watch_lock = threading.Lock()
_watching = set()
watch_counts = {"started": 0, "dropped": 0}

# P2, SAME-NONCE REPLACEMENT (operator via Alpha, 2026-10-08; MEMECOIN_CHAIN1_PROTECT_REPLACE, default
# off). BUNKER 72163: Protect simulated our exit as a revert 3 s after the send and held it while we
# waited 64 s for a receipt, then the deadline. A simError is NOT terminal: the same position's final
# close showed one at poll 1 and landed at poll 3. So this thread only FLAGS a hash after REVERT_POLLS
# consecutive PENDING polls with simError and isRevert; it never signs. The exit lane reads the flag
# (`replace_requested`) and signs the replacement itself, at the same nonce.
REVERT_POLLS = 2
REPLACEMENT_BUMP = (9, 8)            # new fee >= old x 9/8 + 1 wei: past the usual 10% replace rule
_reverting = {}                      # tx hash -> consecutive revert polls (under _watch_lock)


def reverting(tx_hash):
    """True while the watcher has seen REVERT_POLLS consecutive reverting polls on this hash."""
    with _watch_lock:
        return _reverting.get(str(tx_hash or "").lower(), 0) >= REVERT_POLLS


def replace_requested(chain, tx_hash):
    """The exit lane may sign a same-nonce replacement of this held chain-1 send."""
    from sniper.settings import memecoin as cfg
    return str(chain) == "1" and cfg.memecoin_chain1_protect_replace() and reverting(tx_hash)


def consume_revert(tx_hash):
    """The exit lane takes the flag for ONE replacement attempt. A retry then needs REVERT_POLLS
    fresh reverting polls (~24 s), so a refused or skipped attempt never repeats every sweep
    (Gamma MEDIUM-2 on 657e12117: the ethereum worker sweeps every 2 s)."""
    with _watch_lock:
        _reverting.pop(str(tx_hash or "").lower(), None)


_attempts = {}                       # close claim id -> replacement attempts (this process)


def take_attempt(claim_id, limit):
    """One replacement attempt for this close claim, at most `limit` per claim in this process,
    whatever each attempt's outcome (Gamma MEDIUM-2; the operator wants an attempt cap). Bounded
    memory: a claim is a few dozen bytes, and the table is cleared past 1,024 claims."""
    key = str(claim_id or "")
    with _watch_lock:
        if len(_attempts) > 1024:
            _attempts.clear()
        if not key or _attempts.get(key, 0) >= int(limit):
            return False
        _attempts[key] = _attempts.get(key, 0) + 1
        return True


def replacement_affordable(raw_hex, native_wei):
    """Can native cover the SIGNED replacement's worst case (gas x signed maxFee + value)? sign_tx
    may sign a maxFee above the floor (the headroom cap), and a node refuses on that number (Gamma
    LOW-4). An unreadable payload or balance is False: the held sell stands."""
    try:
        import base64
        from sniper.execution.evm_real_seams import decode_signed_tx_cost
        cost = decode_signed_tx_cost(base64.b64encode(str(raw_hex).encode()).decode())
        return cost is not None and native_wei is not None and int(native_wei) >= int(
            cost["worst_case_wei"])
    except Exception:
        return False


def _note_revert(tx_hash, status, got):
    with _watch_lock:
        if status == "PENDING" and got.get("simError") and got.get("isRevert") is True:
            _reverting[tx_hash] = _reverting.get(tx_hash, 0) + 1
        else:
            _reverting.pop(tx_hash, None)


def replacement_fee_floor(old_raw):
    """(maxFeePerGas, maxPriorityFeePerGas) a same-nonce replacement must reach, or None when the
    held bytes cannot be read (then the replacement is not signed)."""
    try:
        from eth_account.typed_transactions import TypedTransaction
        from hexbytes import HexBytes
        tx = TypedTransaction.from_bytes(HexBytes(str(old_raw))).as_dict()
        num, den = REPLACEMENT_BUMP
        return (int(tx["maxFeePerGas"]) * num // den + 1,
                int(tx["maxPriorityFeePerGas"]) * num // den + 1)
    except Exception:
        return None


def _fetch_status(tx_hash, timeout=5.0):
    import urllib.request
    req = urllib.request.Request(STATUS_URL % tx_hash, headers={"user-agent": "Mozilla/5.0 Sniper/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _default_sink(row):
    from sniper.util.state_paths import state_path
    with open(state_path("data", "chain1_protect_status.jsonl"), "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _default_alert(text):
    from sniper.services.memecoin_alerts import send_alert
    send_alert("CHAIN1-PROTECT", text)


def watch_status(tx_hash, *, chain, action, fetch=None, sink=None, alert_fn=None, sleep=time.sleep,
                 clock=time.time, start=True):
    """Start one daemon watch for an accepted private send; a hash already watched, or more than
    MAX_WATCHERS at once, starts none. Never raises. Returns the thread or None."""
    try:
        tx_hash = str(tx_hash or "").lower()
        if not tx_hash.startswith("0x"):
            return None
        with _watch_lock:
            if tx_hash in _watching:
                return None
            if len(_watching) >= MAX_WATCHERS:
                watch_counts["dropped"] += 1
                return None
            _watching.add(tx_hash)
            watch_counts["started"] += 1
        thread = threading.Thread(
            target=_watch, args=(tx_hash, str(chain), action, fetch or _fetch_status,
                                 sink or _default_sink, alert_fn or _default_alert, sleep, clock),
            name="chain1-protect-%s" % tx_hash[:10], daemon=True)
        if start:
            thread.start()
        return thread
    except Exception:
        logger.exception("chain1 protect: status watch not started for %s", tx_hash)
        return None


def _watch(tx_hash, chain, action, fetch, sink, alert_fn, sleep, clock):
    last, paged = None, False
    try:
        for poll in range(MAX_POLLS):
            try:
                got = fetch(tx_hash) or {}
            except Exception as exc:              # noqa: BLE001 - a missed poll, never a verdict
                got = {"status": "unreadable", "error": type(exc).__name__}
            status = str(got.get("status") or "unreadable")
            _note_revert(tx_hash, status, got)
            key = (status, got.get("simError"), got.get("isRevert"))
            if key != last:
                last = key
                row = {"at": clock(), "chain": chain, "action": action, "tx_hash": tx_hash,
                       "poll": poll, "status": status, "sim_error": got.get("simError"),
                       "is_revert": got.get("isRevert"), "max_block": got.get("maxBlockNumber"),
                       "seen_in_mempool": got.get("seenInMempool")}
                if got.get("simError"):
                    row["protect"] = got        # the whole answer: the cause is unproven (Gamma)
                try:
                    sink(row)
                except Exception:
                    logger.exception("chain1 protect: status row not written for %s", tx_hash)
                print("[chain1-protect] %s %s %s simError=%s isRevert=%s" % (
                    action, tx_hash[:12], status, got.get("simError"), got.get("isRevert")), flush=True)
            if got.get("simError") and status not in ("INCLUDED",) and not paged:
                paged = True
                try:
                    alert_fn("chain %s %s %s: Flashbots Protect reports %s with simError=%s "
                             "(isRevert=%s); final inclusion remains unknown."
                             % (chain, action, tx_hash, status, got.get("simError"),
                                got.get("isRevert")))
                except Exception:
                    logger.exception("chain1 protect: page failed for %s", tx_hash)
            if status in TERMINAL:
                return
            sleep(POLL_S)
    finally:
        with _watch_lock:
            _watching.discard(tx_hash)
            _reverting.pop(tx_hash, None)


# ---- P1: the fee headroom ----

class FeeHistoryError(RuntimeError):
    """eth_feeHistory gave no result: an RPC error body, or an answer with no result."""


def fee_history_result(answer):
    """eth_feeHistory's result, or FeeHistoryError naming the RPC error. The chain-1 read rotates its
    pool on provider failures but returns a chain-level JSON-RPC error body unchanged, and indexing
    ["result"] on it raised 166 KeyError tracebacks on 2026-10-08 with the error itself unlogged."""
    if isinstance(answer, dict) and isinstance(answer.get("result"), dict):
        return answer["result"]
    error = answer.get("error") if isinstance(answer, dict) else None
    if isinstance(error, dict):
        raise FeeHistoryError("rpc error code=%s message=%s" % (
            error.get("code"), str(error.get("message"))[:160]))
    raise FeeHistoryError("no result: %s" % str(answer)[:160])


class FeeHeadroom:
    """The next block's base fee and the p75 of recent median tips, from eth_feeHistory, refreshed
    by one daemon thread. Readers take a snapshot under the lock; stale data reads as None."""

    def __init__(self, read_fn, *, clock=time.monotonic, stale_s=FEE_STALE_S):
        self._read_fn, self._clock, self._stale_s = read_fn, clock, stale_s
        self._lock = threading.Lock()
        self._state = None                 # (at, base_next_wei, tip_p75_wei)
        # Written by the refresher thread only; the heartbeat reads a copy (status()).
        self.counts = {"reads": 0, "errors": 0, "streak": 0, "last_error": None}

    def refresh(self):
        self.counts["reads"] += 1
        try:
            hist = self._read_fn()
            bases = [int(x, 16) for x in hist["baseFeePerGas"]]
            tips = sorted(int(row[0], 16) for row in hist["reward"] if row)
            if not bases or not tips or bases[-1] <= 0:
                raise ValueError("empty fee history")
            p75 = tips[min(len(tips) - 1, (3 * len(tips)) // 4)]
            with self._lock:
                self._state = (self._clock(), bases[-1], p75)
        except Exception as exc:                   # noqa: BLE001 - stale data ages out
            # ONE LINE PER STREAK, no traceback: the last data stays until it goes stale, then
            # signing takes today's fees. The heartbeat carries the counts (status()).
            self.counts["errors"] += 1
            self.counts["streak"] += 1
            self.counts["last_error"] = ("%s: %s" % (type(exc).__name__, exc))[:200]
            if self.counts["streak"] == 1:
                logger.warning("chain1 fees: fee history read failed (%s); last data kept up to "
                               "%ss, then today's fees", self.counts["last_error"], self._stale_s)
            return
        if self.counts["streak"]:
            logger.warning("chain1 fees: fee history read recovered after %d failures",
                           self.counts["streak"])
            self.counts["streak"] = 0

    def status(self):
        """The heartbeat's view: the counts, the snapshot's age (None before any) and whether it is
        stale (signing then takes today's fees)."""
        with self._lock:
            state = self._state
        age = None if state is None else round(self._clock() - state[0], 1)
        return {**self.counts, "age_s": age, "stale": age is None or age > self._stale_s}

    def snapshot(self):
        with self._lock:
            state = self._state
        if state is None or self._clock() - state[0] > self._stale_s:
            return None
        return state[1], state[2]


_HEADROOM = {}


def register(chain, headroom):
    _HEADROOM[str(chain)] = headroom


def status():
    """The chain-1 fee reader's status for the runner heartbeat, or None when none runs."""
    headroom = _HEADROOM.get("1")
    return None if headroom is None else headroom.status()


def start_refresher(headroom, *, period_s=FEE_PERIOD_S, sleep=time.sleep, enabled=None):
    """One daemon reading fee history every `period_s` while MEMECOIN_CHAIN1_FEE_HEADROOM is on;
    turned off at run time it stops reading (Gamma: no idle polling of the custody RPC)."""
    if enabled is None:
        from sniper.settings import memecoin as cfg
        enabled = cfg.memecoin_chain1_fee_headroom

    def loop():
        while True:
            if enabled():
                headroom.refresh()
            sleep(period_s)
    thread = threading.Thread(target=loop, name="chain1-fee-headroom", daemon=True)
    thread.start()
    return thread


def _active(chain):
    from sniper.settings import memecoin as cfg
    if str(chain) != "1" or not cfg.memecoin_chain1_fee_headroom():
        return None
    headroom = _HEADROOM.get("1")
    return headroom.snapshot() if headroom is not None else None


def dynamic_tip_wei(chain, floor_wei):
    """The chain-1 tip with headroom on and fresh fee data: max(floor, p75) capped at
    MEMECOIN_EVM_TIP_CAP_GWEI_1. None otherwise (today's tip)."""
    from sniper.settings import memecoin as cfg
    snap = _active(chain)
    if snap is None or floor_wei is None:
        return None
    cap = int(cfg.evm_tip_cap_gwei_for(chain) * 1e9)
    return max(1, min(max(int(floor_wei), snap[1]), cap))


def headroom_max_fee_wei(chain, given_wei, gas_limit, tip_wei):
    """The signed maxFee: today's `given_wei`, raised toward 2 x next base + tip but never past the
    per-tx native gas reserve over `gas_limit`, and never below `given_wei`."""
    from sniper.settings import memecoin as cfg
    snap = _active(chain)
    given = int(given_wei)
    if snap is None or not gas_limit or int(gas_limit) <= 0:
        return given
    reserve_wei = int(min(cfg.gas_entry_native_for(chain), cfg.gas_exit_native_for(chain)) * 1e18)
    target = 2 * snap[0] + int(tip_wei or 0)
    return max(given, min(target, reserve_wei // int(gas_limit)))
