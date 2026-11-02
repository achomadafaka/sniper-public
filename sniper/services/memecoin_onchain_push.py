"""Push-fed on-chain lanes for chain 4663: armed-wallet transfers and new V4 pools.

Spec: docs/superpowers/specs/2026-09-05-memecoin-4663-push-fed-onchain-lanes-design.md.

WHY PUSH. Measured 2026-09-05: our decision timestamp equals our receipt timestamp (median 0.00s),
so the feed lag is entirely upstream -- Fomo's armed-wallet lane runs a 3.3s median, 19s p90, and
9% of buys arrive over 20s late. The one source that beats it is our own chain reader, 1.6s ahead,
but it saw 6 of 435 buys because it POLLS range `eth_getLogs`: only the public chain-owner endpoint
serves that call, and the watcher log holds 12,142 failed ones. Those polls are also part of the
load that rate-limits the endpoint the funded path needs for pool-key reads.

A subscription costs no range read at all. The same probe that measured the lag also showed
`eth_subscribe` on `Initialize` delivering every event in the SAME block as the concurrent
`newHeads` head.

A SUBSCRIPTION ID IS NOT DELIVERY. publicnode accepted a subscription and then delivered nothing
(2026-08-07), so this module measures liveness from delivered HEADS, never from a subscribe reply,
and the caller writes its heartbeat only when a head has actually arrived.

TWO HEALTH QUESTIONS, TWO CHECKS, AND NEITHER REPLACES THE OTHER. The failure mode of a health
metric is someone later trusting it outside its domain, so state the domains here:

  IS THE SOCKET ALIVE?  Answered by HEAD AGE — `stale_after_s` reconnects a socket that has stopped
                        being serviced, and the watchdog's absent-watcher check covers a process
                        that is not running at all.
  IS IT DELIVERING THE  Answered by COVERAGE — delivered heads over the block span they cover.
  WHOLE CHAIN?

A socket that goes ENTIRELY silent produces no window, so `coverage()` returns None — UNKNOWN, never
zero. It cannot detect that case and must not be asked to: a healthy-looking coverage field is not
evidence the lane is alive, and an absent one is not evidence it is sick. Conversely head age says
nothing about a socket that delivers punctually and partially, which is exactly what ran for most of
2026-09-06 at 46% and then 30% of the chain with reconnects reading zero.
"""
from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger("memecoin")

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_BACKOFF_CAP_S = 30.0
# A REFUSAL IS NOT AN OUTAGE, and reconnecting at outage speed against one is what broke this lane
# on 2026-09-05. The QuickNode custody key hit its DAILY request limit at ~15:03 UTC; the socket's
# first reply was {"error":{"code":-32003,"message":"daily request limit reached"}} followed by a
# 1001 close, and this loop read that as "dark", reconnected about once a second, and produced 26
# fallbacks in 30 seconds -- spending requests against the very quota that was exhausted.
#
# A quota or policy refusal is DETERMINISTIC: the next attempt gets the same answer, and on a daily
# limit it will keep getting it for hours. So a refusal backs off on its own much slower schedule,
# starting a full minute out and doubling to ten, while a transient socket error keeps the fast
# 1-30s reconnect that a real outage deserves. Same distinction the RPC pool already draws between
# a provider that is DOWN and one that WILL NOT SERVE THIS (2026-09-03).
_REFUSAL_BACKOFF_START_S = 60.0
_REFUSAL_BACKOFF_CAP_S = 600.0
# TWO KINDS OF NO, AND THEY WANT OPPOSITE LADDERS (2026-09-06). Do not merge these back together;
# a compromise number is wrong for both.
#
# A QUOTA OR AUTH REFUSAL IS DETERMINISTIC AND EXPENSIVE TO RETRY. The QuickNode daily limit that
# broke this lane on 2026-09-05 returns the same answer for HOURS, and every retry spends a request
# against the very quota it is waiting on. The slow 60-to-600 ladder above exists for that.
#
# A TRANSPORT-CLASS REFUSAL IS EPISODIC AND FREE TO RETRY. publicnode answers `-32603 Internal
# error` on 4663 intermittently and then accepts the very next subscribe — verified twice on
# 2026-09-06, once by a socket that recovered on its own and once by a manual subscribe that was
# accepted and delivered a head. On a keyless free endpoint a retry costs nothing, so ten minutes of
# backoff buys ten minutes of a dark lane and saves precisely nothing.
_TRANSPORT_BACKOFF_START_S = 5.0
_TRANSPORT_BACKOFF_CAP_S = 30.0
# JSON-RPC codes that mean "this endpoint is having a moment", not "you may not have this".
# -32603 is the spec's own Internal error; -32000 is the common catch-all server error.
_TRANSPORT_REFUSAL_CODES = ("-32603", "-32000")
# Words that mark a refusal as a POLICY one whatever its code, because providers are inconsistent
# about codes and consistent about wording.
_POLICY_REFUSAL_WORDS = ("limit", "quota", "unauthor", "forbidden", "api key", "plan",
                         "subscription", "credit", "billing", "exceeded")


def refusal_is_transport(text):
    """True when a provider's NO looks episodic rather than a policy decision.

    POLICY WINS ON A TIE. A message naming a limit or a key is treated as policy even if it arrives
    with a transport-shaped code, because retrying fast against a quota is the failure that costs
    the most — it spends the thing it is waiting for.
    """
    body = str(text or "").lower()
    if not body:
        return False
    if any(word in body for word in _POLICY_REFUSAL_WORDS):
        return False
    return any(code in body for code in _TRANSPORT_REFUSAL_CODES)
# A server that closes before it has delivered one head is refusing the session, not dropping it.
# 1001 is "going away" and 1012 is "service restart"; both are what a provider sends when it is
# shedding a client it has decided not to serve.
_REFUSAL_CLOSE_CODES = (1001, 1012)
# SILENCE IS CHECKED TOO (2026-09-29). The stale rule ran only after a message, so a socket that
# stopped delivering without closing blocked `for raw in ws` with no timeout: the 56 watcher went
# dark 02:49:56-02:52:4xZ with reconnects at 0 and a frozen heartbeat. A real client's receive now
# returns every tick, and a tick with no message runs the same rule.
_RECV_TICK_S = 1.0
_END = object()


def _silence_s(state, checked_at, connected_at):
    """Seconds THIS socket has gone without a head: from the later of its connect and its last head.

    NEVER the previous socket's last head (Gamma, 2026-09-29): after a silence reconnect that head
    is past the stale limit by construction, so a fresh socket that needed one empty tick for its
    first head was replaced at once, a reconnect storm (11 in 20 s in Gamma's probe)."""
    last = state.last_head_at if state.last_head_at is not None else connected_at
    return max(0.0, float(checked_at) - max(float(connected_at), float(last)))
# How many recent heads the coverage window keeps. On 4663 (0.095s blocks) 600 heads is about a
# minute of chain; on Base (2s) it is twenty minutes. Keeping a HEAD count rather than a duration
# means the window is self-scaling: it always holds the same amount of evidence.
_COVERAGE_WINDOW_HEADS = int(os.getenv("MEMECOIN_PUSH_COVERAGE_WINDOW_HEADS", "600"))
# Each recent head's FIRST arrival on this socket, by block (2026-10-14, Alpha): a detection names how
# far its log push trailed its own block's head. Bounded; a block older than this reads unknown.
_HEADS_SEEN_MAX = 256
# The smallest block span that may produce a coverage number at all. Below this the sample says
# nothing, and a metric that reports a confident percentage from four blocks is worse than silence.
_COVERAGE_MIN_SPAN = int(os.getenv("MEMECOIN_PUSH_COVERAGE_MIN_SPAN", "60"))
# The smallest span of BLOCK TIME that may produce a coverage number. Block timestamps are whole
# seconds and 4663 produces about ten blocks per second, so a short window has too few distinct
# timestamps to derive a chain rate from -- the divisor would be zero or noise.
_COVERAGE_MIN_SECONDS = float(os.getenv("MEMECOIN_PUSH_COVERAGE_MIN_SECONDS", "30"))
# THE FLOOR, DERIVED FROM MEASUREMENT RATHER THAN CHOSEN. On 4663, 2026-09-06: a fresh socket ran at
# 93% coverage (590 of 632 blocks/min), decayed to 46% in about 35 minutes, and after a restart to
# 30% in ten. 50% sits in the gap — every observed bad reading is below it and the good one is
# nearly double it.
#
# THE NUMBER A LATER READER NEEDS IS NOT THE CONSTANT. It is that RECONNECTS STAYED AT ZERO through
# all three readings: the connection reported itself perfectly healthy while under a third of the
# chain was arriving. That is why coverage exists and why no amount of connection-level health
# checking would have caught this.
#
# 0.6 RATHER THAN THE 0.5 THAT SITS IN THE OBSERVED GAP (operator, 2026-09-06). 0.5 clears the
# MILDER of the two degraded readings by only 3.6 points, so a socket at 55% would go uncaught — and
# looking healthy is the entire failure mode this exists to catch. The costs are asymmetric the
# other way too: a false kill here is cheap, because this is a discovery and detection lane, the
# relaunch is automatic and the cooldown bounds it, while the missed degradation cost most of a day
# at under half coverage. 0.6 keeps 33 points of margin below the healthy 93%.
_COVERAGE_FLOOR = float(os.getenv("MEMECOIN_PUSH_COVERAGE_FLOOR", "0.6"))


def coverage_low_since(state, coverage, now_ts, previous=None):
    """When coverage FIRST went below the floor and stayed there, or None while it is healthy.

    A TIMESTAMP RATHER THAN A COUNTER, so the consumer's rule is a pure function of one heartbeat
    read: "low continuously for longer than N" needs no state on the watchdog side and no agreement
    about what a window is. Unknown coverage (too small a sample) CLEARS it — an unknown must never
    accumulate toward a kill.
    """
    if coverage is None or float(coverage) >= _COVERAGE_FLOOR:
        return None
    return previous if previous is not None else float(now_ts)


def ws_url_for(http_url):
    """The `wss://` form of an https endpoint, or None. The custody pool URL is the default."""
    url = str(http_url or "").strip()
    if url.startswith("wss://") or url.startswith("ws://"):
        return url
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return None


def address_topic(address):
    """A 20-byte address as a 32-byte log topic, or None when it is not an address."""
    raw = str(address or "").strip().lower()
    if not raw.startswith("0x") or len(raw) != 42:
        return None
    try:
        int(raw, 16)
    except ValueError:
        return None
    return "0x" + raw[2:].rjust(64, "0")


def armed_transfer_filter(addresses):
    """`eth_subscribe` params matching ERC-20 Transfers TO any armed address, or None if none.

    The recipient is the THIRD topic of a Transfer, so the filter is
    `[Transfer, null, [addr...]]`: any sender, these recipients. Filtering server-side is the
    point -- the alternative is receiving every transfer on the chain and discarding almost all
    of it, which is the bandwidth equivalent of the poll this replaces.
    """
    topics = [t for t in (address_topic(a) for a in (addresses or ())) if t]
    if not topics:
        return None
    return ["logs", {"topics": [TRANSFER_TOPIC, None, sorted(set(topics))]}]


def sell_transfer_filter(addresses):
    """`eth_subscribe` params matching ERC-20 Transfers FROM any watched address, or None if none.

    The SELL path's counterpart of `armed_transfer_filter` (2026-09-29, shadow): the sender is the
    SECOND topic, so `[Transfer, [addr...], null]`. It rides the same socket as the buy filter.
    """
    topics = [t for t in (address_topic(a) for a in (addresses or ())) if t]
    if not topics:
        return None
    return ["logs", {"topics": [TRANSFER_TOPIC, sorted(set(topics)), None]}]


def initialize_filter(pool_manager, init_topic):
    """`eth_subscribe` params for new pools on OUR PoolManager only."""
    manager = str(pool_manager or "").strip().lower()
    if not manager.startswith("0x") or len(manager) != 42:
        return None
    return ["logs", {"address": manager, "topics": [str(init_topic).lower()]}]


def transfer_recipient(log):
    """The armed address a Transfer log credits, or None when the log is not a Transfer."""
    topics = log.get("topics") or ()
    if len(topics) < 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
        return None
    return "0x" + str(topics[2])[-40:].lower()


def store_initialize(chain, log):
    """Persist the PoolKey a delivered `Initialize` describes. Returns True when it was stored.

    An Initialize is the chain's OWN statement that the pool exists, so no confirming quote is
    needed -- unlike a launchpad-derived key, which is a claim about a pool that may not be open
    yet. The shared writer still hashes the decoded key back to the pool id before storing, so a
    malformed log cannot poison the cache.
    """
    try:
        from sniper.execution.evm_v4_quote import store_pool_keys_from_logs
        return bool(store_pool_keys_from_logs(chain, [log]))
    except Exception:
        logger.debug("push lane: Initialize store failed", exc_info=True)
        return False


class PushState:
    """Liveness measured from DELIVERED heads, never from a subscribe reply."""

    def __init__(self):
        self.last_head_at = None
        self.last_log_at = {}
        self.heads = 0
        self.logs = 0
        self.reconnects = 0
        # WHY the last counted reconnect happened (2026-09-29): armed_set | armed_set_removed |
        # socket_error | socket_silent | heads_stalled | forced. The watcher reads the window a reconnect backfills
        # from it: an armed-set resubscribe reads the whole lane window with the NEW set.
        self.last_reconnect_cause = None
        # EVERY counted reconnect by cause (2026-09-29). The last cause alone was not enough: on 56
        # an armed-set resubscribe got a silent socket and a socket_silent reconnect followed before
        # the first head, so the watcher saw only socket_silent and never read the lane window.
        self.reconnect_causes = {}
        self.limit_backoffs = 0
        self.last_error_reply = None
        self.window = []               # (block_number, block_ts, wall_ts) samples, for coverage
        # PER CONNECTION (2026-09-29, J7 shadow on EVM). `transfer_addresses`: the addresses whose
        # transfer filter the server ACKED on this socket (a reply is not delivery, but without it
        # nothing is subscribed at all); the J7 gate covers a wallet only when all its addresses are
        # here. `delivered`: deliveries per subscription since this connection, so a dead transfer
        # filter behind a live socket is visible afterwards (0 transfers under thousands of heads).
        self.transfer_addresses = None
        self.delivered = {}
        self.connected_at = None
        self.ws_pool = None
        # THE CANARY (2026-10-17): a counted-only logs filter on the same socket; None when off.
        self.canary = None
        self.heads_seen = {}           # block -> (first arrival wall ts, header ms or None)

    def new_connection(self, now_ts=None):
        """A connection attempt starts: nothing is acked or delivered on it yet."""
        self.transfer_addresses = None
        self.delivered = {}
        self.connected_at = now_ts
        # A log on this socket is measured against THIS socket's heads only (Gamma LOW 2).
        self.heads_seen = {}

    def head(self, now_ts, block_number=None, block_ts=None, block_ms=None):
        self.last_head_at = float(now_ts)
        self.heads += 1
        self.delivered["heads"] = self.delivered.get("heads", 0) + 1
        if block_number is not None:
            self.heads_seen.setdefault(int(block_number), (float(now_ts), block_ms))
            while len(self.heads_seen) > _HEADS_SEEN_MAX:
                self.heads_seen.pop(next(iter(self.heads_seen)), None)
        if block_number is not None and block_ts is not None:
            self.window.append((int(block_number), int(block_ts), float(now_ts)))
            if len(self.window) > _COVERAGE_WINDOW_HEADS:
                del self.window[:-_COVERAGE_WINDOW_HEADS]

    def head_for(self, block_number):
        """(first arrival wall ts, header ms or None) of that block's head on this socket, or None."""
        try:
            return self.heads_seen.get(int(block_number))
        except (TypeError, ValueError):
            return None

    def coverage(self, min_span=None, min_seconds=None):
        """What FRACTION OF THE CHAIN we are receiving, as our delivery rate over the chain's own.

            chain_rate = (last_block - first_block) / (last_block_ts - first_block_ts)
            our_rate   = (heads - 1)               / (last_wall - first_wall)
            coverage   = our_rate / chain_rate

        Both rates come from data already in hand: the block numbers and TIMESTAMPS in the
        `newHeads` payloads, and our own clock. No request, and the reference is the chain's own
        block spacing rather than anything we remember about ourselves — so our throttling cannot
        contaminate it the way a best-ever rate would.

        THIS REPLACED A BLOCK-SPAN RATIO THAT COULD NOT SEE THE FAILURE IT WAS BUILT FOR
        (2026-09-06). The old form divided delivered heads by `max(window) - min(window) + 1`, which
        measures between the first and last DELIVERED head — so blocks lost to a delivery PAUSE fell
        outside the window instead of counting as missed. It answered "were the blocks I received
        contiguous", not "what fraction of the chain did I receive". Those agree when an endpoint
        drops individual blocks and diverge completely when it pauses and resumes in sequence, which
        is what we were actually suffering. Simulated against the live degradations:

            scenario                     block-span    rate-based
            healthy 632/min                 1.00          1.00
            skipping 1 in 8                 0.13          0.12
            contiguous @293/min             1.00          0.46
            contiguous @191/min             1.00          0.30
            contiguous @78/min (live)       1.00          0.12

        The old metric read PERFECT HEALTH at 12% of the chain, and would never have fired on the
        2026-09-06 degradation that motivated it.

        TWO MINIMUMS, both returning None — UNKNOWN, never unhealthy. `min_span` guards a slow chain
        where a short window holds few blocks. `min_seconds` guards a FAST one: block timestamps are
        whole seconds and 4663 produces ~10 blocks per second, so over a short window
        `last_ts - first_ts` is zero or noise and the chain rate would be divided by nothing.
        """
        span_floor = _COVERAGE_MIN_SPAN if min_span is None else int(min_span)
        seconds_floor = _COVERAGE_MIN_SECONDS if min_seconds is None else float(min_seconds)
        if len(self.window) < 2:
            return None
        first, last = self.window[0], self.window[-1]
        blocks = last[0] - first[0]
        block_seconds = last[1] - first[1]
        wall_seconds = last[2] - first[2]
        if blocks < max(1, span_floor) or block_seconds < max(1.0, seconds_floor):
            return None
        if wall_seconds <= 0:
            return None
        chain_rate = blocks / float(block_seconds)
        our_rate = (len(self.window) - 1) / float(wall_seconds)
        if chain_rate <= 0:
            return None
        return min(1.0, our_rate / chain_rate)

    def log(self, name, now_ts):
        self.last_log_at[name] = float(now_ts)
        self.logs += 1
        self.delivered[name] = self.delivered.get(name, 0) + 1

    def head_age_s(self, now_ts):
        if self.last_head_at is None:
            return None
        return max(0.0, float(now_ts) - self.last_head_at)


def next_backoff(delay):
    """1, 2, 4, 8 ... capped. The same shape the volume stream already uses."""
    return min(_BACKOFF_CAP_S, max(1.0, float(delay)) * 2.0)


def next_refusal_backoff(delay, transport=False):
    """The wait after a refusal. 60/120/240...600 for a policy no, 5/10/20/30 for a transport one.

    See `_TRANSPORT_BACKOFF_START_S` for why these are separate schedules rather than one tuned
    number: a quota refusal is deterministic for hours and every retry spends the quota, while an
    episodic `-32603` clears on the next attempt and costs nothing on a keyless endpoint.
    """
    start = _TRANSPORT_BACKOFF_START_S if transport else _REFUSAL_BACKOFF_START_S
    cap = _TRANSPORT_BACKOFF_CAP_S if transport else _REFUSAL_BACKOFF_CAP_S
    if not delay or float(delay) < start:
        return start
    return min(cap, float(delay) * 2.0)


def error_reply_text(message):
    """The provider's refusal text from a JSON-RPC error reply, or None.

    ANY error reply to a subscribe request counts, not only -32003. A provider has many ways to
    say no -- quota, plan, method, auth -- and every one of them will answer the next attempt
    identically, so the response to all of them is the same: wait properly.
    """
    if not isinstance(message, dict) or "error" not in message:
        return None
    err = message.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        text = str(err.get("message") or "").strip() or "error"
        return "%s: %s" % (code, text) if code is not None else text
    return str(err)


def _hex_field(payload, name):
    if not isinstance(payload, dict):
        return None
    try:
        return int(str(payload.get(name)), 16)
    except (TypeError, ValueError):
        return None


def block_number_of(payload):
    """The block number in a `newHeads` payload, or None. A header without one is still a head."""
    return _hex_field(payload, "number")


def block_timestamp_of(payload):
    """The block's own timestamp (whole seconds), or None. Coverage needs it for the chain rate."""
    return _hex_field(payload, "timestamp")


def block_ms_of(payload):
    """The header's millisecond timestamp (BSC's milliTimestamp), or None when the head has none."""
    try:
        value = (payload or {}).get("milliTimestamp")
        return int(value, 16) if isinstance(value, str) else None
    except (TypeError, ValueError, AttributeError):
        return None


def close_code_of(exc):
    """The WebSocket close code carried by an exception, or None."""
    for attr in ("code", "rcvd_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    rcvd = getattr(exc, "rcvd", None)
    code = getattr(rcvd, "code", None)
    return code if isinstance(code, int) else None


ALL_LOGS = ("transfers", "initialize", "sells")     # every log filter a socket may carry


class LogSilenceAction:
    """ACT ON A SILENT LOG STREAM BEHIND LIVE HEADS (Alpha 2026-10-16). 4663, 16:37-17:53Z: after a
    socket-silent reconnect, heads kept arriving while the log subscriptions delivered nothing; three
    deposit-matched fast-lane buys never got a watcher receipt. The verdict was observe-only.

    `note(verdict, now, marker)` runs in the watcher's head callback (the push thread). On "suspect"
    it asks for a resubscribe once per silent episode; if `marker` (a log count or last-log stamp)
    has not moved `limit_s` later, a reconnect; then nothing for `cooldown_s`. A moved marker ends
    the episode. The push loop takes the requests (`take_resubscribe`, `take_reconnect`) on the same
    thread. ponytail: fixed cooldown, a knob if a provider ever needs a different one."""

    def __init__(self, *, limit_s, cooldown_s):
        self.limit_s, self.cooldown_s = float(limit_s), float(cooldown_s)
        self.stage, self.at, self._marker = None, None, None
        self._resubscribe = self._reconnect = False
        self.stats = {"resubscribes": 0, "reconnects": 0, "stage": None}

    def _set(self, stage, now):
        self.stage, self.at = stage, now
        self.stats["stage"] = stage

    def note(self, verdict, *, now, marker):
        if marker != self._marker:
            self._marker = marker
            self._set(None, None)
            return None
        if verdict != "suspect":
            return None
        if self.stage is None or (self.stage == "reconnected" and now - self.at >= self.cooldown_s):
            self._set("resubscribed", now)
            self._resubscribe = True
            self.stats["resubscribes"] += 1
            return "resubscribe"
        if self.stage == "resubscribed" and now - self.at >= self.limit_s:
            self._set("reconnected", now)
            self._reconnect = True
            self.stats["reconnects"] += 1
            return "reconnect"
        return None

    def take_resubscribe(self):
        taken, self._resubscribe = self._resubscribe, False
        return taken

    def take_reconnect(self):
        taken, self._reconnect = self._reconnect, False
        return taken


def run_push_stream(ws_url, *, on_transfer, on_initialize, addresses_fn, pool_manager,
                    init_topic, state=None, connect_fn=None, sleep_fn=None, now_fn=None,
                    stale_after_s=15.0, fallback_after_s=120.0, on_fallback=None,
                    on_head=None, should_stop=None, on_connect_attempt=None,
                    on_sell_transfer=None, force_reconnect=None, roster_check_s=5.0,
                    ws_urls=None, chain_id=None, resubscribe_logs=None, failover_after=1,
                    failover_window_s=600.0, primary_return_s=None, canary_filter=None,
                    canary_silence_s=None):
    """One WebSocket carrying newHeads plus the two log filters. Returns when `should_stop` says so.

    RECONNECT ON SILENT HEADS. A socket that stays OPEN but stops being serviced raises no
    exception; without a head-age check the lane goes dark for the life of the process. Reconnect
    at 1, 2, 4, 8s capped at 30s, the same rule as the volume stream.

    FALL BACK RATHER THAN GO BLIND. If no head has arrived for `fallback_after_s`, `on_fallback` is
    called so the caller can run its polling loop; push resumes the moment a head arrives again.

    `force_reconnect` (2026-09-29, spec 2026-09-29-memecoin-evm-watch-dead-subscription-design.md): a
    callable asked on every message and every quiet receive tick; True drops a socket that is alive
    but deaf (its acked topic-only filters delivering nothing) and counts a reconnect.

    `resubscribe_logs` (2026-10-16, Alpha): a callable asked on every message and quiet tick, like
    `force_reconnect`; the log filter names it returns ("transfers", or ALL_LOGS) are subscribed again
    on the LIVE socket. Their old ids are dropped from the map (a late delivery on one is ignored,
    never doubled; no eth_unsubscribe: its error reply would read as a refusal), and it counts as a
    reconnect cause so the watcher's gap backfill reads what the silence missed.

    `roster_check_s` (2026-09-29, Alpha): the armed set is re-read on heads too, at most once per
    this many seconds, so a roster change resubscribes within it rather than at the next delivery
    (~2.5 min on 56). 0 = only on deliveries, as before.

    HOST FAILOVER (`ws_urls`, 2026-10-17, Alpha; 56: MEMECOIN_EVM_WS_URLS_56). A host is left for the
    next one after `failover_after` refusals or failed sockets inside `failover_window_s` (a daily
    quota refusal at once); fewer keep today's single-host wait and poll fallback on it. A head
    clears its count. On a backup, after `primary_return_s` connected there (None = stay), the lane
    closes that socket and re-subscribes on the primary (cause `primary_return`). One socket at a
    time: every switch leaves the `with` block, which closes the old socket, before the next connect.

    THE CANARY (`canary_filter`, 2026-10-17, Alpha/Gamma; 56: one PancakeSwap V2 pool's Sync). A second logs filter on the
    SAME socket, subscribed after the armed ones, COUNTED ONLY: never decoded, never handed to a
    handler, never part of the lane's log liveness. With heads arriving and no canary log for
    `canary_silence_s` since the later of its ack and its last log, the socket is silent: cause
    `canary_silent`, and with a host list the lane moves to the next host at once (one strike is the
    judgment), else it reconnects. state.canary carries logs and the last full minute's count per host.
    """

    def _counted(cause):
        state.reconnects += 1
        state.last_reconnect_cause = cause
        state.reconnect_causes[cause] = state.reconnect_causes.get(cause, 0) + 1

    def _roster_changed(old, new, ws):
        old_set, new_set = set(old), set(new)
        logger.warning("push lane: armed set changed (%d -> %d addresses, +%d -%d) — resubscribing",
                       len(old_set), len(new_set), len(new_set - old_set), len(old_set - new_set))
        # An ADDED address was unsubscribed until now, so its buys predate the blip; a removal-only
        # change cannot hide a buy. The watcher reads the lane window only for `armed_set`.
        _counted("armed_set" if new_set - old_set else "armed_set_removed")
        # This socket is retired: don't wait for its peer before subscribing the new roster.
        close_socket = getattr(ws, "close_socket", None)
        if callable(close_socket):
            close_socket()

    def _resubscribe_names():
        if resubscribe_logs is None:
            return ()
        try:
            return tuple(resubscribe_logs() or ())
        except Exception:
            logger.exception("push lane: resubscribe check failed")
            return ()

    def _forced():
        if force_reconnect is None:
            return False
        try:
            return bool(force_reconnect())
        except Exception:
            logger.exception("push lane: force-reconnect check failed")
            return False
    state = state or PushState()
    sleep_fn = sleep_fn or time.sleep
    cooldown_clock = now_fn or time.monotonic
    now_fn = now_fn or time.time
    if connect_fn is None:
        from websockets.sync.client import connect as connect_fn
    delay = 1.0
    refusal_delay = 0.0
    last_refusal_transport = [None]     # which ladder the current `refusal_delay` belongs to
    dark_since = None
    urls = tuple(dict.fromkeys((ws_url, *(ws_urls or ()))))
    until = [0.0] * len(urls)
    failures = [0] * len(urls)
    strikes = [[] for _ in urls]        # refusal / failed-socket times per host, inside the window
    index = 0
    want_primary = False
    canary_rotate = False
    if canary_filter:
        state.canary = {"silent": 0, "per_host": {}}

    def _canary_host(i):
        host = state.canary["per_host"].setdefault(_host(i), {"logs": 0, "logs_per_min": None,
                                                             "minute_at": None, "minute_logs": 0})
        return host

    def _host(i):
        from urllib.parse import urlsplit
        return urlsplit(urls[i]).hostname or "?"

    def _strike(i):
        now = cooldown_clock()
        strikes[i][:] = [t for t in strikes[i] if now - t < float(failover_window_s)] + [now]
        if len(strikes[i]) < max(1, int(failover_after)):
            return False
        strikes[i].clear()
        return True
    if len(urls) > 1:
        state.ws_pool = {"active_index": 0, "endpoint_count": len(urls), "rotations": 0,
                         "attempts": [0] * len(urls), "heads": [0] * len(urls),
                         "logs": [0] * len(urls), "refusals": [0] * len(urls),
                         "push_host": _host(0), "failovers": 0}
    while not (should_stop and should_stop()):
        if len(urls) > 1:
            eligible = [i for i in range(len(urls)) if until[i] <= cooldown_clock()]
            if not eligible:
                if on_fallback is not None:
                    on_fallback()
                if should_stop and should_stop():
                    return state
                sleep_fn(min(30.0, max(0.0, min(until) - cooldown_clock())))
                continue
            chosen = 0 if want_primary and 0 in eligible else (
                index if index in eligible else eligible[0])
            want_primary = False
            if chosen != index:
                _counted("provider_rotation")  # the watcher backfills this reconnect's gap
                state.ws_pool["rotations"] += 1
                if chosen != 0:
                    state.ws_pool["failovers"] += 1
                logger.warning("push lane: subscribing on %s (was %s)", _host(chosen), _host(index))
            index = chosen
            state.ws_pool["active_index"] = index
            state.ws_pool["push_host"] = _host(index)
            state.ws_pool["attempts"][index] += 1
        refused = None                     # the provider's own words, when it declined the session
        reconnects_before = state.reconnects
        subscribed = {}                     # subscription id -> filter name
        armed = tuple(addresses_fn() or ())
        state.new_connection()
        if on_connect_attempt is not None:
            # The caller unpublishes what the previous socket had acked (Gamma, 2026-09-29).
            try:
                on_connect_attempt()
            except Exception:
                logger.exception("push lane: connect-attempt callback failed")
        try:
            with connect_fn(urls[index], open_timeout=10) as ws:
                if len(urls) > 1 and chain_id is not None:
                    ws.send(json.dumps({"jsonrpc": "2.0", "id": 0,
                                        "method": "eth_chainId", "params": []}))
                    proof = json.loads(ws.recv(timeout=10))
                    refused = error_reply_text(proof)
                    if refused is not None:
                        raise RuntimeError("WebSocket identity provider refused")
                    result = proof.get("result")
                    if proof.get("id") != 0 or not isinstance(result, str) \
                            or not result.startswith("0x"):
                        raise ValueError("WebSocket chain identity unavailable")
                    if int(result, 16) != int(chain_id):
                        until[index] = float("inf")
                        raise ValueError("WebSocket chain identity mismatched")
                request_id = 1

                def _send(method, params):
                    nonlocal request_id
                    ws.send(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                        "method": method, "params": params}))
                    request_id += 1
                    return request_id - 1

                pending = {_send("eth_subscribe", ["newHeads"]): "heads"}

                def _subscribe_logs(names):
                    filters = {
                        "transfers": armed_transfer_filter(armed),
                        "initialize": initialize_filter(pool_manager, init_topic),
                        # THE SELL FILTER (2026-09-29, shadow): only when the caller handles sells,
                        # so a chain off the sell shadow keeps exactly today's subscriptions.
                        "sells": sell_transfer_filter(armed) if on_sell_transfer is not None else None}
                    for name in ALL_LOGS:
                        if name in names and filters[name]:
                            pending[_send("eth_subscribe", filters[name])] = name
                _subscribe_logs(ALL_LOGS)
                canary_since = canary_id = None  # the canary's clock starts at its ack
                if canary_filter:
                    canary_id = _send("eth_subscribe", ["logs", canary_filter])
                    pending[canary_id] = "canary"
                connected_at = now_fn()
                state.connected_at = connected_at
                roster_checked_at = connected_at
                delay = 1.0
                recv = getattr(ws, "recv", None)
                scripted = None if callable(recv) else iter(ws)   # a replayed server (tests)
                try:
                    from websockets.exceptions import ConnectionClosedOK as _closed_ok
                except ImportError:                               # pragma: no cover
                    _closed_ok = ()
                while True:
                    # BEFORE the receive, so a delivered message is never read and then dropped;
                    # every message and every quiet tick passes here.
                    if _forced():
                        logger.warning("push lane: caller forced a reconnect (a live socket not "
                                       "delivering its subscriptions)")
                        _counted("forced")
                        break
                    if (canary_since is not None and canary_silence_s and state.last_head_at is not None
                            and now_fn() - state.last_head_at < float(stale_after_s)
                            and now_fn() - canary_since >= float(canary_silence_s)):
                        logger.warning("push lane: canary silent %.0fs on %s while heads arrive; the "
                                       "socket's log subscriptions are not delivering", now_fn() - canary_since,
                                       _host(index))
                        _counted("canary_silent")
                        state.canary["silent"] += 1
                        canary_rotate = True
                        break
                    if (primary_return_s is not None and index != 0 and until[0] <= cooldown_clock()
                            and now_fn() - connected_at >= float(primary_return_s)):
                        logger.warning("push lane: %.0fs healthy on %s; returning to the primary %s",
                                       now_fn() - connected_at, _host(index), _host(0))
                        _counted("primary_return")
                        want_primary = True
                        break
                    names = _resubscribe_names()
                    if names:
                        for sid in [sid for sid, name in subscribed.items() if name in names]:
                            del subscribed[sid]
                        _subscribe_logs(names)
                        _counted("transfer_silence_resubscribe" if names == ("transfers",)
                                 else "log_silence_resubscribe")
                        logger.warning("push lane: resubscribed %s on the live socket (no log while "
                                       "heads arrive)", "+".join(names))
                    if scripted is not None:
                        raw = next(scripted, _END)
                        if raw is _END:
                            break
                    else:
                        try:
                            raw = recv(timeout=_RECV_TICK_S)
                        except TimeoutError:
                            if should_stop and should_stop():
                                return state
                            checked_at = now_fn()
                            stale = _silence_s(state, checked_at, connected_at)
                            if stale > stale_after_s:
                                logger.warning("push lane: socket silent %.1fs (>%.1fs) with no "
                                               "message at all — reconnecting", stale, stale_after_s)
                                _counted("socket_silent")
                                break
                            continue
                        except _closed_ok:
                            break                                 # a normal close, as iteration ended
                    if should_stop and should_stop():
                        return state
                    now_ts = now_fn()
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if canary_id is not None and msg.get("id") == canary_id and msg.get("error"):
                        # A refused CANARY is not a refused socket: no canary on this one, no judgment.
                        logger.warning("push lane: %s refused the canary filter (%s); no canary on this "
                                       "socket", _host(index), error_reply_text(msg))
                        pending.pop(canary_id, None)
                        canary_id = None
                        continue
                    refusal = error_reply_text(msg)
                    if refusal is not None:
                        # The provider answered our subscribe with a NO. Stop, and wait properly.
                        refused = refusal
                        break
                    if "id" in msg and msg.get("result") and msg["id"] in pending:
                        subscribed[msg["result"]] = pending.pop(msg["id"])
                        if subscribed[msg["result"]] == "canary":
                            canary_since = now_ts
                            continue
                        if subscribed[msg["result"]] == "transfers":
                            # The filter's own addresses: the valid ones, lowercased.
                            state.transfer_addresses = tuple(sorted(
                                {str(a).strip().lower() for a in armed if address_topic(a)}))
                        continue           # a reply is not delivery: nothing is marked live here
                    params = msg.get("params") or {}
                    name = subscribed.get(params.get("subscription"))
                    payload = params.get("result")
                    if name == "canary":         # counted only: no handler, no lane liveness
                        canary_since = now_ts
                        host = _canary_host(index)
                        host["logs"] += 1
                        minute = int(now_ts // 60)
                        if host["minute_at"] != minute:
                            if host["minute_at"] is not None:
                                host["logs_per_min"] = host["minute_logs"] if minute == host["minute_at"] + 1 else 0
                            host["minute_at"], host["minute_logs"] = minute, 0
                        host["minute_logs"] += 1
                        continue
                    if name == "heads":
                        state.head(now_ts, block_number_of(payload),
                                   block_timestamp_of(payload), block_ms=block_ms_of(payload))
                        if state.ws_pool is not None:
                            state.ws_pool["heads"][index] += 1
                            failures[index] = 0
                            strikes[index].clear()
                        dark_since = None
                        if on_head is not None:
                            try:
                                on_head()
                            except Exception:
                                logger.exception("push lane: head callback failed")
                        # THE ROSTER ON A TIMER TOO (2026-09-29, Alpha): one read per roster_check_s
                        # of heads (~1 ms each, measured), so a newly armed wallet is subscribed
                        # within seconds even when nothing is being delivered.
                        if roster_check_s and now_ts - roster_checked_at >= roster_check_s:
                            roster_checked_at = now_ts
                            current = tuple(addresses_fn() or ())
                            if current != armed:
                                _roster_changed(armed, current, ws)
                                break
                        continue
                    if name and payload:
                        state.log(name, now_ts)
                        if state.ws_pool is not None:
                            state.ws_pool["logs"][index] += 1
                        try:
                            if name == "transfers":
                                on_transfer(payload)
                            elif name == "initialize":
                                on_initialize(payload)
                            elif name == "sells" and on_sell_transfer is not None:
                                on_sell_transfer(payload)
                        except Exception:
                            logger.exception("push lane: %s handler failed", name)
                    # RE-SUBSCRIBE ON AN ARMED-SET CHANGE. Checked on delivery rather than on a
                    # timer, so a quiet chain costs nothing and a busy one notices within a block.
                    current = tuple(addresses_fn() or ())
                    if current != armed:
                        _roster_changed(armed, current, ws)  # counted and logged (2026-09-29)
                        break              # reconnect rebuilds both filters from the new set
                    # THE CLOCK AFTER THE HANDLER (2026-09-28). `now_ts` was read before the handler
                    # ran, so time spent inside one (a null-read retry) was invisible here and a
                    # stalled socket could read as healthy.
                    checked_at = now_fn()
                    stale = _silence_s(state, checked_at, connected_at)
                    if stale > stale_after_s:
                        logger.warning(
                            "push lane: newHeads stalled %.1fs (>%.1fs) — reconnecting",
                            stale, stale_after_s)
                        _counted("heads_stalled")
                        break
        except Exception as exc:
            code = close_code_of(exc)
            if code in _REFUSAL_CLOSE_CODES and state.heads == 0:
                # Closed before a single head: the provider is shedding us, not dropping us.
                refused = "close %s before any head" % code
            else:
                # A SOCKET ERROR IS A RECONNECT (2026-09-29). Uncounted, the watcher's gap backfill
                # (keyed on this count) never ran after one: 0 backfills after the 05:30:42Z error.
                _counted("socket_error")
                logger.warning("push lane: socket error; reconnecting in %.0fs", delay)
        if should_stop and should_stop():
            return state
        now_ts = now_fn()
        if refused is not None:
            state.limit_backoffs += 1
            state.last_error_reply = refused
            transport = refusal_is_transport(refused)
            # A REFUSAL CLASS DOES NOT CARRY ACROSS A CHANGE OF CLASS. A transport blip after a
            # quota refusal must not inherit the quota's ten-minute wait, and a quota refusal after
            # a blip must not inherit the blip's five seconds.
            if transport != last_refusal_transport[0]:
                refusal_delay = 0.0
                last_refusal_transport[0] = transport
            refusal_delay = next_refusal_backoff(refusal_delay, transport=transport)
            daily = "daily" in refused.lower() and any(
                word in refused.lower() for word in ("limit", "credit", "quota"))
            if len(urls) > 1:
                state.ws_pool["refusals"][index] += 1
            if len(urls) > 1 and (daily or _strike(index)):
                wait = (86400.0 - now_fn() % 86400.0) if daily else refusal_delay
                until[index] = cooldown_clock() + wait
                logger.warning("push lane: endpoint slot %d (%s) refused (%s); rotating, cooldown %.0fs",
                               index, _host(index), refused, wait)
                continue
            # ONCE PER BACKOFF, not once per attempt: a daily limit would otherwise fill the log
            # with the same line every second for the rest of the day.
            logger.warning("push lane: provider refused the subscription (%s, %s); "
                           "waiting %.0fs before retrying", refused,
                           "transport" if transport else "policy", refusal_delay)
            if on_fallback is None:
                sleep_fn(refusal_delay)
            else:
                deadline = cooldown_clock() + refusal_delay
                while cooldown_clock() < deadline:
                    if should_stop and should_stop():
                        return state
                    next_poll = cooldown_clock() + 30.0
                    on_fallback()          # real polling, never a synthetic liveness heartbeat
                    if should_stop and should_stop():
                        return state
                    wait = min(deadline, next_poll) - cooldown_clock()
                    if wait > 0:
                        sleep_fn(wait)
            delay = 1.0                    # a later transient failure still gets the fast schedule
            continue
        refusal_delay = 0.0
        if want_primary:
            continue                       # the healthy backup's socket closed; next: the primary
        rotate_now, canary_rotate = canary_rotate, False
        if len(urls) > 1 and not (state.reconnects > reconnects_before and
                state.last_reconnect_cause in ("armed_set", "armed_set_removed")) and (
                rotate_now or _strike(index)):
            failures[index] += 1
            logger.warning("push lane: endpoint slot %d (%s) failed (%s); rotating", index, _host(index),
                           state.last_reconnect_cause if state.reconnects > reconnects_before
                           else "connect")
            until[index] = max(until[index], cooldown_clock() +
                               (5.0, 15.0, 60.0)[min(failures[index], 3) - 1])
            continue
        if state.last_head_at is None or (now_ts - state.last_head_at) > fallback_after_s:
            dark_since = dark_since or now_ts
            if on_fallback is not None and (now_ts - dark_since) >= 0:
                on_fallback()
        sleep_fn(delay)
        delay = next_backoff(delay)
    return state
