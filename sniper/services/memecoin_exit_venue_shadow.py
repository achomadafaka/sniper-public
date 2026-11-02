"""The EVM exit-venue shadow (spec 2026-09-30-memecoin-evm-exit-venue-selection-design, §5.6).

The EVM sell ladder takes the first venue that hits in a fixed order, and the mark the exit rails
read comes from the same ladder. A position bought on one pool can be marked, stopped and sold
through a drained side pool. Before any selection changes, this measures the gap: for each exit
quote the runner stores, what would the position's ENTRY venue have paid?

WHERE IT RUNS. In the capture process, never in the funded runner. The runner already stores every
exit-decision quote with its venue (`memecoin_route_series`); this reads those rows from a
read-only ledger handle and writes to the captures store. So it adds no wait to a sell or to a
preview by construction, and nothing on the exit path imports it.

IT CANNOT SIGN. It is given the wallet ADDRESS. The whole-bag sell it simulates carries no
Permit2 permit (a permit is a signature): the Permit2 allowance is set with a state override on
the `eth_call` instead.

WHAT AN ANSWER IS. A quoter number moves no tokens and is blind to what the coin does on transfer
(a sell tax raised after entry, a blocked seller). So each tick takes the entry pool's quote AND
simulates the real sell transaction for the whole bag at the executor's own minimum (the quote
less the slippage the runner stored with its quote). A V4 simulation cannot return its output:
`sim_ok` says the bag sells for at least `sim_min_atomic`. After a pass a second simulation asks
for 99% of the quote: a pass at the minimum and a revert at 99% is a transfer tax in between.

AT WHICH BLOCK. The shadow reaches a tick about a second after the runner quoted it. On the tick
that fires a rail the runner's sell has landed by then: at "latest" the wallet has no bag and the
entry pool is the pool after our own sell. So every read of a tick is pinned to a block before
the runner's quote time, on the chain's own clock.

Which block the runner's quote saw is not stored, and these pools move every few blocks (10 to
30% inside a second on a stop tick). Measured on 13 stored quotes of three 4663 positions whose
winner WAS the entry pool (the block where the pool's quote equals the stored output): the state
the runner read is 4 to 9 blocks, 0.4 to 0.9 s, older than `quoted_at`. So a tick is read at TWO
blocks that bracket that: `early_block` (quoted_at - 1.0 s) and `pin_block` (quoted_at - 0.3 s).
The two quotes equal: the pool did not move across the window and the answer is exact. They
differ: the row says so and is a bound, not an answer. The allowance and the simulations use
`pin_block`. The sub-second position of a block comes from the chain: the first block of the
quote's own second is found by a search on block timestamps, never from the host's clock.

ITS OWN HOST, ITS OWN BUDGET. Every request goes to the one URL the operator named for the chain,
as given, with no pool rotation. That host must serve historical state (every read is at a past
block) and must not be one the funded runner uses: `build_worker` refuses a host whose NAME is
the chain's custody RPC or a member of its funded or archive pool. A chain whose native coin is
its quote token (Arc 5042) is not shadowed: its native pool quotes are in another unit. A ceiling bounds its calls per minute: a tick whose worst case
does not fit is a row that says so, and the ceiling is enforced again on every single call. Its
calls are also SPACED: never two inside CALL_GAP_S, so a tick's 10 to 11 calls are no burst (the
research host answered one 429 to about 10 calls inside 1.5 s, and none to calls 0.4 s apart).
The first 429 pauses it for 60 s.

THE LAST QUOTE IS ALWAYS READ. The quote that matters most is the one a rail fired on, and a
position is sampled once per interval, so sampling alone would catch about one fire tick in
twelve. Reads are pinned to past blocks, so they do not expire: when a position closes, its last
stored quote is read, ahead of the sampling, and waits for room instead of being skipped.

Slice 1: pinned Uniswap V4 entry pools. V2/V3 entry venues, aggregator entries and the 60 s
follow-up after a loss-side close are later slices.
"""
from __future__ import annotations

import http.client
import inspect
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from datetime import datetime, timezone
from urllib.parse import urlsplit

from eth_utils import keccak

from sniper.execution import evm_v4_quote as Q
from sniper.util.clock import real_utc_now_iso

logger = logging.getLogger("memecoin")

MAX_CALLS_PER_MIN = 30            # the ceiling on one chain's read host
CALL_GAP_S = 0.4                  # and the least time between two of its calls: 2.5 a second
READS_PER_TICK = 6                # head, 2 quotes, token approval, 2 simulations
SAMPLE_EVERY_S = 20.0             # one tick per position per interval: one position fits the ceiling
HOLD_S = 60.0                     # the pause after a 429
TIMEOUT_S = 3.0
SIM_TIGHT_BPS = 9_900             # the second simulation: 99% of the entry pool's quote
PIN_EARLY_S = 1.0                 # the bracket around the state the runner's quote read:
PIN_LATE_S = 0.3                  # measured 0.4 to 0.9 s before quoted_at (4663, 13 quotes)
PIN_SLACK_BLOCKS = 2              # slack of the timestamp search window, plus 1% of its distance
BLOCK_TIME_SPAN = 10_000          # blocks the chain's block time is measured over
BLOCK_TIME_TTL_S = 600.0          # and how long that measurement is used
_SEL_ALLOWANCE = "dd62ed3e"       # ERC-20 allowance(owner, spender)
_MAX_160, _MAX_48 = (1 << 160) - 1, (1 << 48) - 1
# The path encoder takes the chain on the branch where Ethereum mainnet has its own struct layout.
_PATH_TAKES_CHAIN = "chain" in inspect.signature(Q._v4_swap_input_path).parameters


def _word(address):
    return bytes.fromhex(str(address).lower().removeprefix("0x").rjust(64, "0"))


def permit2_allowance_slot(owner, token, spender):
    """Storage slot of Permit2's `allowance[owner][token][spender]` (the mapping is slot 1)."""
    slot = keccak(_word(owner) + (1).to_bytes(32, "big"))
    slot = keccak(_word(token) + slot)
    return "0x" + keccak(_word(spender) + slot).hex()


def packed_allowance(amount, expiration, nonce):
    """Permit2's PackedAllowance word: uint160 amount, uint48 expiration, uint48 nonce."""
    return int(amount) | (int(expiration) << 160) | (int(nonce) << 208)


def _sells_zero_for_one(key, mint):
    """Selling the coin moves coin -> the other currency: zeroForOne only when the coin is currency0."""
    return str(key["currency0"]).lower() == str(mint).lower()


def quote_call(chain, key, mint, amount_atomic):
    """The V4Quoter `eth_call` for selling `amount_atomic` of the coin into this one pool."""
    data = ("0x" + Q._SEL_QUOTE_SINGLE + Q._w(0x20) + Q._encode_pool_key(key)
            + Q._w(1 if _sells_zero_for_one(key, mint) else 0) + Q._w(int(amount_atomic))
            + Q._w(0x100) + Q._w(0))
    return {"to": Q.V4[str(chain)]["quoter"], "data": data}


def sell_simulation(chain, key, mint, amount_atomic, min_out, owner, *, now=None):
    """(call, state overrides) for the whole-bag sell on this pool, as the executor builds it minus
    the permit. `owner` is an address: nothing here can sign."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE, native_is_quote
    chain = str(chain)
    conf = Q.V4[chain]
    weth = str((CHAIN_QUOTE.get(chain) or {}).get("token") or "").lower()
    zf1 = _sells_zero_for_one(key, mint)
    other = str(key["currency1"] if zf1 else key["currency0"]).lower()
    to_sender = other == weth or native_is_quote(chain)       # the pool pays the quote token itself
    if Q._pool_is_hooked(key):
        extra = {"chain": chain} if _PATH_TAKES_CHAIN else {}
        swap = Q._v4_swap_input_path(key, zf1, int(amount_atomic), int(min_out), payer_is_user=True,
                                     take_to=owner if to_sender else Q._ADDRESS_THIS, **extra)
    else:
        swap = Q._v4_swap_input(key, zf1, int(amount_atomic), int(min_out),
                                take_all_to_sender=to_sender)
    commands, inputs = [Q._CMD_V4_SWAP], [swap]
    if not to_sender:                                           # native out: wrap it to the owner
        commands.append(Q._CMD_WRAP_ETH)
        inputs.append(Q._w(Q._MSG_SENDER) + Q._w(Q._CONTRACT_BALANCE))
    deadline = int(time.time() if now is None else now) + 600
    slot = permit2_allowance_slot(owner, mint, conf["universal_router"])
    overrides = {conf["permit2"]: {"stateDiff": {
        slot: "0x" + format(packed_allowance(_MAX_160, _MAX_48, 0), "064x")}}}
    call = {"from": owner, "to": conf["universal_router"], "value": "0x0",
            "data": Q._ur_execute_data(bytes(commands), inputs, deadline)}
    return call, overrides


def _post(url, body, timeout):
    """One JSON-RPC request to the URL as given. (status, json)."""
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "user-agent": "sniper-exit-venue-shadow"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def pin_blocks(second_block, second_time, quoted_epoch, block_time):
    """(early, late): the blocks current PIN_EARLY_S and PIN_LATE_S before the runner's quote.
    `second_block` is the first block of the quote's own second and `second_time` its timestamp,
    so a block's position inside the second comes from the chain's block count."""
    def at(age):
        return int(second_block) + math.floor(
            (float(quoted_epoch) - age - float(second_time)) / float(block_time))
    return at(PIN_EARLY_S), at(PIN_LATE_S)


def _epoch(iso):
    stamp = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("a quote time without a zone")
    return stamp.timestamp()


def _answer(response):
    """(result, revert). A node error that is not a revert (state it does not hold, a bad
    request) RAISES: it must never read as "the pool reverted"."""
    error = response.get("error")
    if not error:
        return response.get("result"), None
    message = str(error.get("message") if isinstance(error, dict) else error)
    data = error.get("data") if isinstance(error, dict) else None
    if "revert" not in message.lower():
        raise RuntimeError("rpc_%s" % message[:60])
    return None, str(data or message)[:138]


NOT_READ = ("held", "held_429", "over_ceiling")    # states that end by themselves: read later
LAST_TICK_RETRIES = 3             # a last tick cut by the transport is read at most this many times
LAST_TICK_RETRY_AFTER_S = (60.0, 300.0)   # ... the 2nd attempt 60 s after the 1st, the 3rd 300 s later


class _Held(Exception):
    pass


class _OverCeiling(Exception):
    pass


class Shadow:
    """One chain's shadow. Single-threaded: the worker owns it."""

    def __init__(self, chain, url, *, owner, post_fn=None, clock=time.monotonic, sleep=time.sleep,
                 now_iso_fn=None, max_calls_per_min=MAX_CALLS_PER_MIN,
                 sample_every_s=SAMPLE_EVERY_S, hold_s=HOLD_S, timeout_s=TIMEOUT_S):
        self.chain, self.url, self.owner = str(chain), url, str(owner).lower()
        self.post_fn, self.clock, self.sleep = post_fn or _post, clock, sleep
        self.now_iso_fn = now_iso_fn or real_utc_now_iso      # the runner's clock: quoted_at is on it
        self.max_calls, self.sample_every_s = int(max_calls_per_min), float(sample_every_s)
        self.hold_s, self.timeout_s = float(hold_s), float(timeout_s)
        self._window = deque()                 # clock stamps of the calls in the last minute
        self._held_until = 0.0
        self._last_call = None                 # clock of the latest call, for the spacing
        self._sampled = {}                     # position id -> clock of its last tick
        self._block_time = None                # (seconds per block, clock when measured)
        self.last_tick = {}                    # position id -> its newest pinnable tick
        self.last_tick_failures = Counter()    # position id -> transport failures of its last tick
        self.last_tick_retry_at = {}           # position id -> clock before which it is not retried
        self.transient = False                 # the latest observation failed on the transport
        self.read = {}                         # position id -> series id of its last tick READ
        self._unpinnable = set()               # positions already recorded as having no entry pool
        self.counts = Counter()
        self.last_row_at = None

    def held(self):
        return self.clock() < self._held_until

    def due(self, position_id):
        last = self._sampled.get(position_id)
        return last is None or self.clock() - last >= self.sample_every_s

    def seen_unpinnable(self, position_id):
        return position_id in self._unpinnable

    def _block_time_due(self):
        return (self._block_time is None
                or self.clock() - self._block_time[1] >= BLOCK_TIME_TTL_S)

    def need(self, tick):
        """The most calls this tick can take. The timestamp search is wider for an older tick."""
        block_time = self._block_time[0] if self._block_time else 0.1
        try:
            age = max(0.0, _epoch(self.now_iso_fn()) - _epoch(tick.get("quoted_at"))) + 1.0
        except (TypeError, ValueError):
            age = 0.0
        width = (math.ceil(1.0 / block_time) + 1
                 + 2 * (PIN_SLACK_BLOCKS + math.ceil(0.01 * (age + 1.0) / block_time)))
        return (READS_PER_TICK + math.ceil(math.log2(width)) + 1
                + (1 if self._block_time_due() else 0))

    def room(self, need):
        now = self.clock()
        while self._window and now - self._window[0] >= 60.0:
            self._window.popleft()
        return len(self._window) + int(need) <= self.max_calls

    def _block(self, tag, row):
        block = self._call("eth_getBlockByNumber", [tag, False], row).get("result")
        if not isinstance(block, dict):
            raise RuntimeError("rpc_no_block")
        return int(block["number"], 16), int(block["timestamp"], 16)

    def _second_start(self, second, head, head_time, row):
        """(block, its timestamp): the first block whose timestamp is `second` or later. A binary
        search under the head; the result is CHECKED against the blocks on both sides of it."""
        if head_time < second:
            raise RuntimeError("rpc_head_behind")
        block_time, seen = self._block_time[0], {head: head_time}

        def stamp(number):
            if number not in seen:
                seen[number] = self._block(hex(number), row)[1]
            return seen[number]
        back = (head_time + 1 - second) / block_time
        slack = PIN_SLACK_BLOCKS + math.ceil(0.01 * back)
        low = head - math.ceil(back) - slack
        high = min(head, head - math.floor((head_time - second) / block_time) + slack)
        while high - low > 1:
            middle = (low + high) // 2
            if stamp(middle) >= second:
                high = middle
            else:
                low = middle
        if not stamp(low) < second <= stamp(high):
            raise RuntimeError("rpc_block_time")     # the blocks are not where the block time says
        return high, stamp(high)

    def _pin(self, tick, row):
        """The two block tags this tick is read at: (early, late)."""
        quoted_epoch = _epoch(tick["quoted_at"])
        head, head_time = self._block("latest", row)
        row["head_block"], row["head_time"] = head, head_time
        if self._block_time_due():
            _number, earlier = self._block(hex(head - BLOCK_TIME_SPAN), row)
            if head_time <= earlier:
                raise RuntimeError("rpc_block_time")
            self._block_time = ((head_time - earlier) / BLOCK_TIME_SPAN, self.clock())
        row["block_time_s"] = self._block_time[0]
        row["second_block"], second_time = self._second_start(
            int(quoted_epoch), head, head_time, row)
        row["early_block"], row["pin_block"] = pin_blocks(
            row["second_block"], second_time, quoted_epoch, self._block_time[0])
        return hex(row["early_block"]), hex(row["pin_block"])

    def _call(self, method, params, row):
        if not self.room(1):                   # the ceiling itself, whatever a tick was admitted on
            raise _OverCeiling()
        if self._last_call is not None:        # no burst: this thread waits, nothing else does
            wait = self._last_call + CALL_GAP_S - self.clock()
            if wait > 0:
                self.sleep(wait)
        self._last_call = self.clock()
        self._window.append(self._last_call)
        self.counts["calls"] += 1
        row["calls"] += 1
        status, answer = self.post_fn(
            self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            self.timeout_s)
        if status == 429:
            self._held_until = self.clock() + self.hold_s
            self.counts["http_429"] += 1
            logger.warning("exit venue shadow %s: HTTP 429, paused %.0f s; ticks in the pause are "
                           "rows that say so", self.chain, self.hold_s)
            raise _Held()
        if status != 200 or not isinstance(answer, dict):
            raise RuntimeError("http_%s" % status)
        return answer

    def observe(self, tick):
        """One tick -> one row (always). Never raises."""
        route = tick.get("entry_route") if isinstance(tick.get("entry_route"), dict) else None
        key = tick.get("pool_key")
        row = {name: tick.get(name) for name in (
            "series_id", "network_id", "position_id", "mint", "quoted_at", "winner_venue",
            "winner_pool_id", "winner_route_target", "input_atomic", "winner_output_atomic",
            "winner_min_output_atomic", "winner_slippage_bps", "quote_asset_usd")}
        row.update(entry_protocol=(route or {}).get("protocol"),
                   entry_pool_id=(route or {}).get("pool_id"),
                   entry_hooked=None if key is None else int(Q._pool_is_hooked(key)),
                   observed_at=self.now_iso_fn(), lag_s=None, head_block=None, head_time=None,
                   block_time_s=None, second_block=None, early_block=None, pin_block=None,
                   entry_quote_early_atomic=None, entry_quote_atomic=None, entry_quote_ms=None,
                   allowance_standing=None,
                   sim_min_atomic=None, sim_ok=None, sim_revert=None, sim_ms=None,
                   sim99_ok=None, sim99_revert=None, calls=0)
        try:
            row["lag_s"] = (datetime.fromisoformat(str(row["observed_at"]).replace("Z", "+00:00"))
                            - datetime.fromisoformat(str(tick["quoted_at"]).replace("Z", "+00:00"))
                            ).total_seconds()
        except (KeyError, TypeError, ValueError):
            pass
        self.transient = False
        row["outcome"] = self._observe(tick, route, key, row)
        if key and row["outcome"] not in NOT_READ and not self.transient:
            self.read[tick.get("position_id")] = tick.get("series_id")
        self.counts["rows"] += 1
        self.counts[row["outcome"].split(":")[0]] += 1
        self.last_row_at = row["observed_at"]
        return row

    def _observe(self, tick, route, key, row):
        if not route or route.get("protocol") != "uniswap_v4" or not route.get("pool_id"):
            self._unpinnable.add(tick.get("position_id"))
            return "unpinnable:%s" % ((route or {}).get("protocol") or "none")
        # Sampled from here on, whatever the outcome: a position with no stored key, a pause or a
        # full ceiling writes one row per interval, not one per pass.
        self._sampled[tick.get("position_id")] = self.clock()
        if not key:
            return "no_pool_key"
        if self.held():
            self.counts["held_ticks"] += 1
            return "held"
        if not self.room(self.need(tick)):
            return "over_ceiling"
        try:
            amount, mint = int(tick["input_atomic"]), str(tick["mint"]).lower()
            early, block = self._pin(tick, row)
            started = self.clock()
            out = self._quote(key, mint, amount, block, row)
            row["entry_quote_ms"] = (self.clock() - started) * 1000.0
            if out <= 0:
                return "entry_quote_reverted"
            row["entry_quote_atomic"] = str(out)
            before = out if early == block else self._quote(key, mint, amount, early, row)
            row["entry_quote_early_atomic"] = str(before) if before > 0 else None
            allowance, _revert = _answer(self._call("eth_call", [{
                "to": mint, "data": "0x" + _SEL_ALLOWANCE + _word(self.owner).hex()
                + _word(Q.V4[self.chain]["permit2"]).hex()}, block], row))
            standing = int(str(allowance or "0x0"), 16) >= amount
            row["allowance_standing"] = int(standing)
            if not standing:
                return "no_token_approval"
            slippage = tick.get("winner_slippage_bps")
            if slippage is None or not 0 <= int(slippage) < 10_000:
                return "no_slippage"              # the executor's minimum is not known: no guess
            minimum = out * (10_000 - int(slippage)) // 10_000
            row["sim_min_atomic"] = str(minimum)
            started = self.clock()
            row["sim_ok"], row["sim_revert"] = self._simulate(key, mint, amount, minimum, block, row)
            row["sim_ms"] = (self.clock() - started) * 1000.0
            if row["sim_ok"]:
                row["sim99_ok"], row["sim99_revert"] = self._simulate(
                    key, mint, amount, out * SIM_TIGHT_BPS // 10_000, block, row)
            return "ok"
        except _Held:
            return "held_429"
        except _OverCeiling:
            return "over_ceiling"
        except Exception as exc:              # noqa: BLE001 - a shadow row names its failure
            self.counts["errors"] += 1
            words = str(exc)
            # The host did not answer (a 5xx, a refused or dropped connection, a timeout): says
            # nothing about the pool, so a closed position's last tick is read again.
            self.transient = (words.startswith("http_")
                              or isinstance(exc, (OSError, urllib.error.URLError,
                                                  http.client.HTTPException)))
            return "error:%s" % (words if words.startswith(("http_", "rpc_")) else type(exc).__name__)

    def _quote(self, key, mint, amount, block, row):
        """The entry pool's quote for the whole bag at `block`, atomic; 0 when it reverts. An
        answer that is neither a revert nor a quote raises: it is not "the pool reverted"."""
        result, revert = _answer(self._call(
            "eth_call", [quote_call(self.chain, key, mint, amount), block], row))
        if revert is not None:
            return 0
        if not isinstance(result, str) or not result.startswith("0x") or len(result) < 66:
            raise RuntimeError("rpc_malformed_quote")
        return int(result[2:66], 16)

    def _simulate(self, key, mint, amount, minimum, block, row):
        """(1, None) the whole bag sells for at least `minimum` at `block`; (0, revert data)."""
        call, overrides = sell_simulation(self.chain, key, mint, amount, minimum, self.owner)
        _result, revert = _answer(self._call("eth_call", [call, block, overrides], row))
        return (0, revert) if revert is not None else (1, None)

    def stats(self):
        return {"chain": self.chain, "held": self.held(), "last_row_at": self.last_row_at,
                "positions_tracked": len(self.last_tick),
                "calls_last_min": len(self._window), "max_calls_per_min": self.max_calls,
                **{name: self.counts[name] for name in (
                    "rows", "ok", "calls", "http_429", "held_ticks", "over_ceiling", "errors",
                    "unpinnable", "no_pool_key", "no_token_approval", "no_slippage",
                    "entry_quote_reverted")}}


def newest_series_id(ledger):
    from sniper.persistence import db_memecoin as m
    return m.max_route_series_id(ledger)


def new_ticks(ledger, chain, *, after_id):
    """(ticks, cursor): the NEWEST stored exit-decision quote per real position on `chain` since
    `after_id`, each with its entry route and the entry pool's stored key."""
    from sniper.persistence import db_memecoin as m
    rows = m.exit_decision_series_after(ledger, chain, after_id)
    cursor = max([int(after_id)] + [int(r["series_id"]) for r in rows])
    newest = {}
    for r in rows:
        newest[r["position_id"]] = r
    ticks = []
    for r in sorted(newest.values(), key=lambda x: x["series_id"]):
        try:
            evidence = json.loads(r.get("quote_evidence_json") or "{}")
        except (TypeError, ValueError):
            evidence = {}
        try:
            route = json.loads(r.get("evm_route_json") or "null")
        except (TypeError, ValueError):
            route = None
        route = route if isinstance(route, dict) else None
        key = None
        if route and route.get("protocol") == "uniswap_v4" and route.get("pool_id"):
            key = m.get_evm_v4_pool_key(ledger, chain_id=chain, pool_id=route["pool_id"],
                                        pool_manager=Q.V4[str(chain)]["pool_manager"])
        ticks.append({
            "series_id": r["series_id"], "position_id": r["position_id"], "network_id": str(chain),
            "mint": r["mint"], "quoted_at": r.get("quoted_at") or r.get("observed_at"),
            "winner_venue": evidence.get("venue") or (evidence.get("route") or {}).get("protocol"),
            "winner_pool_id": (evidence.get("route") or {}).get("pool_id"),
            "winner_route_target": evidence.get("route_target"),
            "input_atomic": evidence.get("input_atomic"),
            "winner_output_atomic": evidence.get("expected_output_atomic"),
            "winner_min_output_atomic": evidence.get("minimum_output_atomic"),
            "winner_slippage_bps": evidence.get("slippage_bps"),
            "quote_asset_usd": evidence.get("quote_asset_usd"),
            "entry_route": route, "pool_key": key})
    return ticks, cursor


def read_last_ticks(shadow, ledger, store_conn):
    """The last stored quote of every position that has closed: read it if no read of it exists.
    It waits for a pause to end and for room under the ceiling; it is never skipped."""
    from sniper.persistence import db_memecoin as m
    from sniper.persistence import db_memecoin_captures as store
    for position_id in m.closed_position_ids(ledger, list(shadow.last_tick)):
        tick = shadow.last_tick[position_id]
        if shadow.read.get(position_id) != tick["series_id"]:
            if shadow.clock() < shadow.last_tick_retry_at.get(position_id, 0.0):
                continue                           # spaced: a host outage outlasts a few passes
            if shadow.held() or not shadow.room(shadow.need(tick)):
                continue
            row = shadow.observe(tick)
            if row["outcome"] in NOT_READ:
                continue
            if shadow.transient:
                failures = shadow.last_tick_failures[position_id] = (
                    shadow.last_tick_failures[position_id] + 1)
                if failures < LAST_TICK_RETRIES:     # a pinned read does not expire: try again
                    shadow.last_tick_retry_at[position_id] = (
                        shadow.clock() + LAST_TICK_RETRY_AFTER_S[failures - 1])
                    continue
            store.record_exit_venue_shadow(store_conn, row, replace=True)
        del shadow.last_tick[position_id]
        shadow.read.pop(position_id, None)
        shadow.last_tick_failures.pop(position_id, None)
        shadow.last_tick_retry_at.pop(position_id, None)


def run_pass(shadow, ledger, store_conn, cursor):
    """One pass for one chain. Returns the new cursor. A closed position's last quote first;
    then each position is sampled once per interval, and a position with no pinnable entry
    venue is recorded once."""
    from sniper.persistence import db_memecoin_captures as store
    ticks, cursor = new_ticks(ledger, shadow.chain, after_id=cursor)
    for tick in ticks:
        if tick["pool_key"]:
            shadow.last_tick[tick["position_id"]] = tick
    read_last_ticks(shadow, ledger, store_conn)
    for tick in ticks:
        position_id = tick["position_id"]
        if shadow.seen_unpinnable(position_id) or not shadow.due(position_id):
            continue
        store.record_exit_venue_shadow(store_conn, shadow.observe(tick))
    return cursor


class Worker(threading.Thread):
    """All shadowed chains on ONE thread in the capture process, each with its own host and
    ceiling. It starts at the ledger's newest quote: the shadow never backfills."""

    def __init__(self, shadows, *, ledger_path, store_path, poll_s=1.0):
        super().__init__(name="exit-venue-shadow", daemon=True)
        self.shadows, self.ledger_path, self.store_path = list(shadows), str(ledger_path), store_path
        self.poll_s, self.errors, self.last_pass = float(poll_s), 0, None
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def stats(self):
        return {"alive": self.is_alive(), "errors": self.errors, "last_pass": self.last_pass,
                "chains": [shadow.stats() for shadow in self.shadows]}

    def run(self):
        from sniper.persistence import db_memecoin_captures as store
        from sniper.persistence.db_core import get_readonly_connection
        ledger = get_readonly_connection(self.ledger_path)
        out = store.connect(self.store_path)
        cursors = {shadow.chain: newest_series_id(ledger) for shadow in self.shadows}
        while not self._stop_event.is_set():
            for shadow in self.shadows:
                try:
                    cursors[shadow.chain] = run_pass(shadow, ledger, out, cursors[shadow.chain])
                except Exception:
                    self.errors += 1
                    logger.exception("exit venue shadow %s: pass failed; the thread continues",
                                     shadow.chain)
            self.last_pass = datetime.now(timezone.utc).isoformat()
            self._stop_event.wait(self.poll_s)


def _funded_hosts(env, cfg, chain):
    """Hostnames the funded runner reads and sends through on `chain`: its RPC, its pool and its
    archive pool. Compared by NAME: another key on the same host spends the same provider."""
    urls = [str(env.get("MEMECOIN_EVM_RPC_URL_%s" % chain) or ""),
            str(env.get("MEMECOIN_EVM_RPC_URL") or "")]
    for pools in (getattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {}) or {},
                  getattr(cfg, "MEMECOIN_EVM_ARCHIVE_RPC_POOL_URLS", {}) or {}):
        urls.extend(pools.get(chain) or pools.get(int(chain)) or ())
    return {urlsplit(str(url).strip()).hostname for url in urls if str(url).strip()}


def build_worker(env, cfg, *, ledger_path, store_path):
    """The worker for the chains the operator gave a read host, or (None, reason)."""
    hosts = cfg.memecoin_exit_venue_shadow_hosts(env)
    if not hosts:
        return None, "no MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_<chain> is set"
    owner = str(env.get("MEMECOIN_EXIT_VENUE_SHADOW_OWNER") or "").strip().lower()
    if len(owner) != 42 or not owner.startswith("0x"):
        return None, "MEMECOIN_EXIT_VENUE_SHADOW_OWNER (the funded wallet's address) is not set"
    from sniper.execution.evm_wallet import native_is_quote
    # A chain whose native coin IS the quote token (Arc 5042) quotes a native pool in the native
    # unit, not in the quote token's units the runner stores: not comparable, so not shadowed.
    chains = sorted(chain for chain in hosts if chain in Q.V4 and not native_is_quote(chain))
    if not chains:
        return None, ("no shadowed chain has a Uniswap V4 deployment on record and a quote token "
                      "apart from its native coin")
    funded = {chain: _funded_hosts(env, cfg, chain) for chain in chains}
    refused = [chain for chain in chains if urlsplit(hosts[chain]).hostname in funded[chain]]
    if refused:
        return None, ("the read host for chain %s is a funded RPC host; the shadow needs its own "
                      "(one that serves historical state)" % ",".join(refused))
    shadows = [Shadow(chain, hosts[chain], owner=owner) for chain in chains]
    return Worker(shadows, ledger_path=ledger_path, store_path=store_path), None
