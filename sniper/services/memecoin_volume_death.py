"""Would a volume-death exit have helped? Measure it on the live book. Act on nothing.

PREMISE, measured by Gamma and then RECOMPUTED on slot time after a batch-stamp artifact was found
(see the instrument note below). Separation survives at roughly 10x:

    real_tp    n=36   max interior gap median  5.0s   p75 16.8s
    b_timecap  n=19   max interior gap median 51.0s   p75 79.4s
    cutoff 20s -> 14/19 timecaps caught, 8/36 winners touched
    cutoff 60s ->  7/19 caught,          1/36 touched

So the rule is promising and its cost is real — at 20s it cuts nearly a quarter of the winners it
would have to run alongside. 60s is the conservative arming candidate: it touches 3% of winners and
still catches a third of the timecaps. Adaptive per-coin forms measured WORSE than plain fixed 20s,
dominated on both axes, and the mcap-adaptive variant is uncalibratable because entry_mcap is
populated on 4 of 75 rows.

This evaluator writes down what it WOULD have done and touches nothing: no exit, no gate, no
timecap change, no inventory. Arming is a separate operator decision with its own cohort cutover.

THE INSTRUMENT IS EVENT-TIME SWAP EVIDENCE. Two ways to get this wrong, both measured:

  - A POLLED MID is not silence. DexScreener's m5 is an aggregate on a ~30s cadence; its gaps
    measure our polling, not the market's quiet.
  - A BATCH TIMESTAMP is not event time. `pool_ticks.observed_at` is when the POLLER wrote the row:
    6,412 rows share 245 distinct stamps since 08-23 (3.8% distinct), and the nine exactly-300.000s
    "silences" in the first pass were the fetch interval itself. A clock read from it measures our
    cadence and would fire the 20s rule constantly for non-market reasons. If the tape must ever be
    read here, join slot time through `block_times(chain_id, block_no, block_ts)` — never the raw
    column.

This uses `memecoin_position_marks` where source='fastlane_sol_swap', which is live WS arrival and
genuinely per-event: verified independently 2026-08-26 at 6,028 rows with 6,028 distinct timestamps,
100% distinct, against the tape's 3.8%. Coverage reaches 76 of 110 funded closes since 08-20 (69%).

SILENCE, NOT VOLUME-BELOW-FLOOR. A volume form would inherit a 15x unit mismatch — the alive floor
is fitted on FIVE-MINUTE volume and a 20s window is a fifteenth of it — plus DexScreener's ~30s
refresh floor underneath. If the operator wants that variant it needs a tape refit at the sustain
window, as its own design.

QUIET IS NOT BLIND, AND THIS IS THE WHOLE DESIGN. A dead stream reads as unbounded silence and
would fire on every position at once — the loudest possible false signal at the worst possible
moment. On 2026-08-24 the mark stream went dark for 926 seconds while a funded position was open;
a naive silence rule would have "fired" on it with total confidence and learned nothing true.

So the clock only accumulates while the stream is provably ALIVE: swap evidence must have landed
for SOME mint inside the health window. When it has not, the row is stamped `blind_frozen`, the
silence is recorded as unmeasurable, and no would-fire is produced. Absence of evidence about the
market is never evidence about the coin.

Thresholds are logged TOGETHER in one row (10/15/20/30/60s) so the cutoff is chosen from one live
dataset rather than from five reruns of the same window.
"""
from __future__ import annotations

from collections import deque
import contextlib
from datetime import datetime
import json
import logging
import math
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

SWAP_SOURCE = "fastlane_sol_swap"
# THE SECOND WITNESS (2026-08-28, operator, position 44813). Pool-STATE marks price the vault
# reserves the next fill executes against. Solana-only, exactly like SWAP_SOURCE, so the chain
# scope of this rail does not change.
STATE_SOURCE = "fastlane_sol_state"
# THE THIRD WITNESS (2026-09-03, operator, position 49013). The price the bag can actually sell
# into. Every other witness in this file asks a VENUE whether it saw a trade, so each one inherits
# the assumption that we bound the right venue. This one does not ask a venue anything.
EXECUTABLE_SOURCE = "fastlane_rpc"
DEFAULT_THRESHOLDS_S = (10, 15, 20, 30, 60)


def _epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _pool_key_ok(pool):
    key = str(pool).lower()
    if key.startswith("flap:"):
        mint = key[5:]
        return (mint.startswith("0x") and len(mint) == 42
                and all(c in "0123456789abcdef" for c in mint[2:]))
    return key.startswith("0x") and len(key) in {42, 66}


class RobinhoodVolumeState:
    """Keep live pool activity for the Robinhood volume-death exit."""

    def __init__(self, *, health_window_s=5.0):
        self.health_window_s = float(health_window_s)
        self._lock = threading.Lock()
        self._bindings = {}
        self._covered_at = {}
        self._activity_at = {}
        self._activity_blocks = {}
        self._heads = deque(maxlen=4096)

    def disconnect(self):
        with self._lock:
            self._covered_at.clear()
            self._heads.clear()

    def replace_bindings(self, bindings, *, now_ts, covered=False):
        clean = {
            str(pool).lower(): str(mint).lower()
            for pool, mint in dict(bindings or {}).items()
            if _pool_key_ok(pool) and mint
        }
        with self._lock:
            self._bindings = clean
            self._covered_at = {
                pool: seen for pool, seen in self._covered_at.items() if pool in clean
            }
            self._activity_at = {
                pool: seen for pool, seen in self._activity_at.items() if pool in clean
            }
            self._activity_blocks = {
                pool: block for pool, block in self._activity_blocks.items() if pool in clean
            }
            if covered:
                for pool in clean:
                    self._covered_at.setdefault(pool, float(now_ts))

    def record_coverage(self, pool, *, now_ts):
        key = str(pool or "").lower()
        with self._lock:
            if key in self._bindings:
                self._covered_at.setdefault(key, float(now_ts))

    def record_head(self, block_number, *, now_ts):
        try:
            block = int(block_number)
        except (TypeError, ValueError):
            return False
        with self._lock:
            self._heads.append((block, float(now_ts)))
        return True

    def record_pool_activity(self, pool, *, now_ts, block_number=None):
        key = str(pool or "").lower()
        block = None
        if block_number is not None:
            try:
                block = int(block_number, 16) if isinstance(block_number, str) else int(block_number)
            except (TypeError, ValueError, OverflowError):
                pass
        with self._lock:
            if key not in self._bindings:
                return False
            self._activity_at[key] = float(now_ts)
            if block is not None and block >= 0:
                self._activity_blocks[key] = block
            else:
                self._activity_blocks.pop(key, None)
        return True

    def head_age_s(self, now_ts):
        """Seconds since the last newHeads delivery, or None if none has ever arrived.

        This is the SAME staleness `decision()` freezes on — exposed so the stream loop can
        detect and reconnect on it, instead of only reacting to a hard connection exception. A
        subscription that stays open but stops being SERVICED (a common free-RPC failure mode)
        raises nothing, so nothing here was watching it before this method existed."""
        with self._lock:
            if not self._heads:
                return None
            return max(0.0, float(now_ts) - self._heads[-1][1])

    def coverage_snapshot(self, now_ts):
        """Read-only summary for the operator heartbeat log: bound pools, covered pools, and
        head age. Never used by any funded decision — decision() re-derives everything itself."""
        with self._lock:
            return {
                "pools_bound": len(self._bindings),
                "pools_covered": len(self._covered_at),
                "head_age_s": (None if not self._heads
                              else max(0.0, float(now_ts) - self._heads[-1][1])),
                "heads_seen": len(self._heads),
            }

    def decision(self, pos, *, now_iso, exit_s, confirm_fn, pre_confirm=None):
        now_ts = _epoch(now_iso)
        opened_ts = _epoch(pos.get("opened_at"))
        try:
            threshold = float(exit_s)
        except (TypeError, ValueError):
            return False, None, "misconfigured"
        if now_ts is None or opened_ts is None or threshold <= 0:
            return False, None, "unmeasurable"
        mint = str(pos.get("mint") or "").lower()
        with self._lock:
            heads = list(self._heads)
            if not heads or now_ts - heads[-1][1] > self.health_window_s:
                return False, None, "blind_frozen"
            pools = [pool for pool, bound in self._bindings.items() if bound == mint]
            if not pools or any(pool not in self._covered_at for pool in pools):
                return False, None, "swap_lane_uncovered"
            quiet_from = max(
                opened_ts,
                max(self._covered_at[pool] for pool in pools),
                max((self._activity_at.get(pool, opened_ts) for pool in pools),
                    default=opened_ts),
            )
            latest = [pool for pool in pools if self._activity_at.get(pool) == quiet_from]
            last_activity_block = (max(self._activity_blocks[pool] for pool in latest)
                                   if latest and all(pool in self._activity_blocks for pool in latest)
                                   else None)
        silence = max(0.0, now_ts - quiet_from)
        if silence < threshold:
            return False, silence, "measured"
        # A FIRE ATTEMPT ONLY: a subclass's checks before the confirm read (the 56 poll's pool
        # re-discovery and canary). A token holds the rail; the subclass names it. None for 4663.
        if pre_confirm is not None:
            token = pre_confirm(mint, pools, now_ts - silence, now_ts)
            if token:
                return False, silence, "pre:%s" % token
        # Poll arrival times can coincide across blocks; use the observed swap's block boundary.
        first = (last_activity_block + 1 if last_activity_block is not None
                 else next((block for block, seen_at in heads if seen_at >= quiet_from), None))
        last = heads[-1][0]
        # THE DARK LABEL NAMES THE READ (2026-09-16, position 58831). One undifferentiated
        # `state_reader_dark` held X for five minutes and nobody could tell which read failed.
        if first is None or first > last:
            return False, silence, "state_reader_dark:no_head"
        if confirm_fn is None:
            return False, silence, "state_reader_dark:no_confirm_fn"
        # ONE READ FOR EVERY BOUND POOL. This loop used to make one eth_getLogs per pool and hold on
        # the first failure, so a coin with 11 pools needed 11 good reads in a row: at the ~26%
        # failure rate of the only endpoint that serves the filter, that is about 4%.
        try:
            logs = confirm_fn(pools, first, last)
        except Exception:
            logs = None
        if logs is None:
            return False, silence, "state_reader_dark:getlogs_failed"
        if logs:
            moved = {_log_pool(log) for log in logs} & set(pools)
            for pool in (moved or pools[:1]):
                try:
                    block = max(int(log["blockNumber"], 16) for log in logs
                                if _log_pool(log) == pool)
                except (KeyError, TypeError, ValueError, OverflowError):
                    block = None
                self.record_pool_activity(pool, now_ts=now_ts, block_number=block)
            return False, silence, "state_moving"
        return True, silence, "measured_state_still"


ROBINHOOD_VOLUME_STATE = RobinhoodVolumeState()


# THE POLL RAIL (spec 2026-10-01-memecoin-bsc-swap-feed-design.md): BSC (56) fed by a thread polling
# the operator's archive endpoint (memecoin_bsc_volume_poll). The 4663 decision with three changes,
# each a named, audited `blind_frozen:*` label: a canary pool must have delivered through the silence
# window, a fire attempt re-discovers the mint's pools first, and blindness is audited.
CANARY_GAP_S = 10.0
# The sweep never discovers pools itself (Gamma MEDIUM on 7a93d73c9): the poller thread re-reads each
# bound mint's pool set every ~20 s and the decision reads that set. Older than this, it is unknown.
REDISCOVER_MAX_AGE_S = 60.0


class BscPollState(RobinhoodVolumeState):
    """One per poll chain, never the 4663 instance."""

    def __init__(self, *, health_window_s=5.0, canary_gap_s=CANARY_GAP_S):
        super().__init__(health_window_s=health_window_s)
        self.canary_gap_s = float(canary_gap_s)
        self._canary = deque(maxlen=20000)
        self._rediscovered = {}                 # mint -> (ts, frozenset of pools), from the poller

    def record_rediscovery(self, mint, pools, *, now_ts):
        with self._lock:
            self._rediscovered[str(mint).lower()] = (
                float(now_ts), frozenset(str(p).lower() for p in pools))

    def record_canary(self, *, now_ts):
        with self._lock:
            now_ts = float(now_ts)
            previous = (self._canary[-1] if self._canary
                        else min(self._covered_at.values(), default=now_ts))
            if now_ts - previous > self.canary_gap_s:
                # A recovered feed must earn a new full silence window after its gap.
                self._covered_at = {pool: max(seen, now_ts)
                                    for pool, seen in self._covered_at.items()}
            self._canary.append(now_ts)

    def canary_vouched(self, start_ts, now_ts):
        """True when canary swaps arrived through [start_ts, now_ts] with no gap over the bound,
        counting from the silence start itself (the first tick of coverage may hold no canary)."""
        with self._lock:
            seen = [t for t in self._canary if start_ts <= t <= now_ts]
        marks = [start_ts] + seen + [now_ts]
        return all(b - a <= self.canary_gap_s for a, b in zip(marks, marks[1:]))

    def bind_more(self, pools, mint):
        with self._lock:
            for pool in pools:
                key = str(pool).lower()
                if _pool_key_ok(key):
                    self._bindings.setdefault(key, str(mint).lower())

    def decision(self, pos, *, now_iso, exit_s, confirm_fn):
        def pre(mint, pools, start_ts, now_ts):
            with self._lock:
                got = self._rediscovered.get(mint)
            if got is None or now_ts - got[0] > REDISCOVER_MAX_AGE_S:
                return "rediscovery_failed"         # no fresh pool-set read: unknown, no network here
            new = set(got[1]) - set(pools)
            if new:
                self.bind_more(new, mint)       # uncovered until a poll covers it: the clock restarts
                return "pool_set_changed"
            return None if self.canary_vouched(start_ts, now_ts) else "unvouched"
        fire, silence, why = RobinhoodVolumeState.decision(
            self, pos, now_iso=now_iso, exit_s=exit_s, confirm_fn=confirm_fn, pre_confirm=pre)
        if why == "blind_frozen":
            return False, silence, "blind_frozen:poll_stale"
        if why in ("swap_lane_uncovered", "pre:rediscovery_failed"):
            return False, silence, "blind_frozen:pool_uncovered"
        if why == "pre:pool_set_changed":
            return False, silence, "blind_frozen:pool_set_changed"
        if why == "pre:unvouched":
            return False, silence, "blind_frozen:poll_unvouched"
        return fire, silence, why


# Per chain: the health window and the canary gap follow the chain's tick. Chain 1 polls every 24 s
# (two 12 s blocks), so a head is up to one tick old between polls: 60 s is two ticks plus a block
# (Alpha, after Beta found one slow 56 tick reading poll_stale at the 5 s window).
EVM_POLL_STATE = {"56": BscPollState(),
                  "1": BscPollState(health_window_s=60.0, canary_gap_s=60.0),
                  # Arc: 6 s ticks of 0.5 s blocks; two ticks plus margin.
                  "5042": BscPollState(health_window_s=15.0, canary_gap_s=15.0)}


# THE TAPE RAIL (operator order 2026-10-01): Base (8453) from our own swap tape. Measured on 8453
# over the hour to 08:20Z: V4 and V3 swaps land in every block (max gap 4 s), V2 nearly every block
# (max 6 s); the writer's last header is about 1-2 s old.
TAPE_FEED_MAX_AGE_S = 10.0
TAPE_DELIVERY_GAP_S = 10.0
_TAPE_SWAP_KINDS = ("v3_swap", "v4_swap", "pons_v2_swap", "v2_swap")
# One literal per subscription family, so the audit guard can see every label this rail returns.
TAPE_UNVOUCHED = {family: "tape_unvouched:" + family for family in ("v4", "v3", "v2", "pons_v2")}


def _tape_family(pool_len, kind):
    """The subscription that carries a tick. `v3_swap` labels both V3 and V4; the pool-id length
    tells them apart (66 = a V4 pool id)."""
    if kind in ("v2_swap", "pons_v2_swap"):
        return kind[:-5]
    return "v4" if pool_len == 66 else "v3"


def tape_exit_decision(pos, *, now_iso, exit_s, tape_path=None):
    """Volume-death for an EVM chain in MEMECOIN_VOLDEATH_TAPE_CHAINS, from the local swap tape.

    Mirrors `RobinhoodVolumeState.decision`: the clock runs from the newest of the open, the
    tape's covered-window start and the coin's newest swap on ANY of its pools. It reads one local
    SQLite file and calls no provider. Silence fires only when all of these hold:

      - the writer is alive (`volume_feed.received` within TAPE_FEED_MAX_AGE_S) and its covered
        window began before the silence did (a reset moves `since`, restarting the clock);
      - every pool the tape maps to this mint is on a subscribed protocol, and the entry pair is
        one of them. A pool the tape never mapped would be a stranger's quiet (position 49013);
      - each subscription the coin's pools ride delivered OTHER pools' swaps through the last
        exit_s seconds, with no gap over TAPE_DELIVERY_GAP_S. A silent subscription is re-opened
        WITHOUT resetting the window (8453 V4, 2026-09-29), and nothing re-opens a silent V2 one;
      - the exact whole-bag quotes did not move (the venue-free veto).

    NOT HANDLED: a venue the tape does not subscribe at all (another Swap topic) that the tape
    therefore never maps. Only the exact-quote veto and the wrapper's executable veto see it.

    Every non-measured answer is `blind_frozen:<cause>` or `tape_unvouched:<family>`, and both are
    in POST_THRESHOLD_REASONS, so each writes an audit row. A listed chain the tape writer does not
    run (no `volume_feed` row: 56 and 5042 today) is `blind_frozen:feed_absent` on every position,
    one audit row a minute each (Gamma LOW 2 on ef1bfa029)."""
    now, opened = _epoch(now_iso), _epoch(pos.get("opened_at"))
    if now is None or opened is None:
        return False, None, "unmeasurable"
    chain, mint = str(pos.get("network_id")), str(pos.get("mint") or "").lower()
    try:
        contract = json.loads(pos.get("funded_contract_json") or "{}")
        route = json.loads(pos.get("evm_route_json") or "{}")
        facts = (contract.get("classification") or {}).get("facts") or {}
        # THE POOLS WE HOLD, UNION (Alpha G2, 2026-10-08, on Gamma's LOW): the pool our entry receipt
        # touched, the route's pool id, and the provider's pair while it is known. A Kyber route has
        # no pool id and, with DexScreener leaving the money path, often no provider pair, so 71670
        # read entry_pool_unknown for its whole hold: the executed pool closes that. The provider
        # pair is NEVER dropped while known: if the tape did not map it (perhaps the coin's main pool
        # on an uncovered protocol), quiet on our pool proves nothing, and a false fire sells a live
        # coin where blind only holds.
        own = {str(p).lower() for p in (pos.get("executed_pool"), route.get("pool_id"),
                                        facts.get("liquidity_pair_address")) if p}
    except (TypeError, ValueError, AttributeError):
        return False, None, "blind_frozen:route_unreadable"
    if route.get("curve"):
        return False, None, "blind_frozen:curve_route"      # a curve is not a pool (49013)
    if not own:
        # No entry pair to check against the tape's map (Gamma LOW 1 on ef1bfa029): silence on the
        # mapped pools could be a stranger's quiet while we hold a pool the tape never mapped.
        return False, None, "blind_frozen:entry_pool_unknown"
    from sniper.services.memecoin_onchain_volume import covered_protocols
    from sniper.services.memecoin_volume_writer import db_path, iso
    try:
        tape = sqlite3.connect("file:%s?mode=ro" % (tape_path or db_path()), uri=True,
                               timeout=0.2, isolation_level=None)
    except sqlite3.Error:
        return False, None, "blind_frozen:store_unavailable"
    try:
        with contextlib.closing(tape):
            tape.execute("BEGIN")                           # one snapshot for every read below
            feed = tape.execute("SELECT since, received FROM volume_feed WHERE chain_id=?",
                                (chain,)).fetchone()
            if not feed or feed[0] is None or feed[1] is None:
                return False, None, "blind_frozen:feed_absent"
            if not -1.0 <= now - float(feed[1]) <= TAPE_FEED_MAX_AGE_S:
                return False, None, "blind_frozen:feed_stale"
            pools = {str(pool).lower(): str(protocol or "") for pool, protocol in tape.execute(
                "SELECT pool, protocol FROM onchain_pool_protocols WHERE chain_id=? AND mint=?",
                (chain, mint))}
            if not pools:
                return False, None, "blind_frozen:no_pool"
            if own - set(pools):
                return False, None, "blind_frozen:pool_unmapped"
            if not set(pools.values()) <= set(covered_protocols(chain, tape)):
                return False, None, "blind_frozen:protocol_uncovered"
            kinds = ",".join("?" * len(_TAPE_SWAP_KINDS))
            quiet_from = max(opened, float(feed[0]))
            for pool in pools:
                row = tape.execute(
                    "SELECT t.observed_at, b.block_ts FROM pool_ticks t LEFT JOIN block_times b "
                    "ON b.chain_id=t.chain_id AND b.block_no=t.block_no WHERE t.chain_id=? "
                    f"AND t.pool=? AND t.kind IN ({kinds}) ORDER BY t.block_no DESC LIMIT 1",
                    (chain, pool, *_TAPE_SWAP_KINDS)).fetchone()
                if row is None:
                    continue
                # Block time; a log that beat its header uses its arrival, which is later and so
                # can only shorten the silence.
                seen = _epoch(row[1]) if row[1] is not None else _epoch(row[0])
                if seen is None:
                    return False, None, "blind_frozen:tape_unreadable"
                quiet_from = max(quiet_from, seen)
            silence = max(0.0, now - quiet_from)
            if silence < float(exit_s):
                return False, silence, "measured"
            window_start = now - float(exit_s)
            delivered = {}
            for pool_len, kind, block_ts in tape.execute(
                    "SELECT DISTINCT length(t.pool), t.kind, b.block_ts FROM pool_ticks t "
                    "JOIN block_times b ON b.chain_id=t.chain_id AND b.block_no=t.block_no "
                    f"WHERE t.chain_id=? AND t.observed_at>=? AND t.kind IN ({kinds})",
                    (chain, iso(window_start), *_TAPE_SWAP_KINDS)):
                ts = _epoch(block_ts)
                if ts is not None and ts >= window_start:
                    delivered.setdefault(_tape_family(pool_len, kind), []).append(ts)
    except sqlite3.Error:
        logger.exception("vol_death tape read failed for %s", mint)
        return False, None, "blind_frozen:store_unavailable"
    for family in sorted(set(pools.values())):
        edges = [window_start] + sorted(delivered.get(family, ())) + [now]
        if max(b - a for a, b in zip(edges, edges[1:])) > TAPE_DELIVERY_GAP_S:
            return False, silence, TAPE_UNVOUCHED[family]
    from sniper.settings import memecoin as cfg
    if getattr(cfg, "MEMECOIN_VOLDEATH_EXACT_WITNESS", True) and exact_quote_witness(
            pos.get("id"), window_start_iso=window_start, now_iso=now,
            max_range_pct=getattr(cfg, "MEMECOIN_VOLDEATH_MOVE_VETO_PCT", 5.0)) == "moving":
        return False, silence, "exact_quote_moving"
    return True, silence, "measured_tape_still"


def _log_pool(log):
    """The pool a log belongs to: topics[1] under the V4 PoolManager, else the emitting address."""
    from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP
    try:
        address = str(log.get("address") or "").lower()
        topics = log.get("topics") or ()
        if (address in V4_POOL_MANAGERS.values() and len(topics) > 1
                and str(topics[0]).lower() == V4_SWAP):
            return str(topics[1]).lower()
        return address
    except AttributeError:
        return None


def robinhood_pool_bindings(positions):
    bindings = {}
    for pos in positions or ():
        if str(pos.get("network_id") or "") != "4663":
            continue
        try:
            contract = json.loads(pos.get("funded_contract_json") or "{}")
            route = json.loads(pos.get("evm_route_json") or "{}")
        except (TypeError, ValueError):
            continue
        if route.get("curve"):
            # A BONDING CURVE IS NOT A POOL, SO NO POOL CAN WITNESS IT. Position 49013
            # (2026-09-03) exited `vol_death` at 92.2s on `measured_state_still` while the coin
            # was alive: 25 distinct executable mids inside the same window, moving over a 20%
            # range. The exact quote prices this token from the curve's own `getReserves()`, so a
            # moving mid IS moving reserves — that is direct proof the pool the rail called still
            # was not the venue being traded.
            #
            # Its route is `pons_v2`, ungraduated, `pairToken() == 0`. The binding below took
            # `liquidity_pair_address` out of the classification facts, which is the V4 poolId
            # DexScreener reports for the token, and that pool held $5.01 and never traded.
            # `eth_getLogs` answered "no swaps here" correctly, about a pool nobody uses, and the
            # rail read a stranger's stillness as this coin's death.
            #
            # An unwatchable venue must freeze this rail, which is the same answer the module
            # already gives for a dark reader or an uncovered pool. Binding nothing reaches that
            # state through `swap_lane_uncovered`. This rail is the one exit that sells without a
            # percent recheck — `executable_exit_allowed` returns True for any reason it does not
            # name — so a wrong witness here sells directly, and nothing downstream catches it.
            continue
        mint = str(pos.get("mint") or "").lower()
        pool = (contract.get("classification", {}).get("facts", {}).get("liquidity_pair_address")
                or route.get("pool_id"))
        pool = str(pool or "").lower()
        if pool.startswith("0x") and len(pool) in {42, 66}:
            bindings[pool] = mint
        # BIND EVERY POOL THE COIN HAS, NOT ONE (2026-09-04). One pool was bound, chosen from
        # DexScreener's `liquidity_pair_address` or the route's own pool id, and that is wrong in
        # two directions at once.
        #
        # It watches too little: a MULTI-HOP entry has no single `pool_id`, so those positions
        # bound nothing at all and every one of them sat at `swap_lane_uncovered` with no rail --
        # and multi-hop is now a common route. Measured on this runner's decisions: 57
        # `blind_frozen` and 22 `state_reader_dark` against 4 `measured`.
        #
        # And it watches the wrong thing: the comment above records position 49013 exiting on a
        # pool holding $5.01 that never traded, while the coin moved 20% on the venue nobody was
        # watching. Silence on one pool was read as the coin's death.
        #
        # The decision layer has always handled a SET -- it requires every bound pool to be covered
        # and takes the NEWEST activity across all of them -- so more pools makes this rail strictly
        # more conservative, never more trigger-happy. The durable PoolKey cache already knows every
        # pool for a mint, offline and free, so this costs no provider call.
        if mint:
            try:
                from sniper.execution.evm_v4_quote import _stored_pools_for_mint
                for extra in _stored_pools_for_mint(pos.get("network_id"), mint):
                    key = str(extra or "").lower()
                    if key.startswith("0x") and len(key) in {42, 66}:
                        bindings.setdefault(key, mint)
            except Exception:
                logger.exception("volume death: cached pool bind failed mint=%s", mint)
    return bindings


def _robinhood_log_filter(pool):
    from sniper.services.memecoin_evm_v4_events import (
        V4_MODIFY_LIQUIDITY, V4_POOL_MANAGERS, V4_SWAP)
    from sniper.services.memecoin_tick_stream import (
        V2_BURN, V2_MINT, V2_SWAP, V2_SYNC, V3_BURN, V3_MINT, V3_SWAP)
    pool = str(pool).lower()
    if len(pool) == 66:
        return {
            "address": V4_POOL_MANAGERS["4663"],
            "topics": [[V4_SWAP, V4_MODIFY_LIQUIDITY], pool],
        }
    return {
        "address": pool,
        "topics": [[V2_SWAP, V2_SYNC, V2_MINT, V2_BURN, V3_SWAP, V3_MINT, V3_BURN]],
    }


def robinhood_log_subscription(request_id, pool):
    return {
        "jsonrpc": "2.0", "id": int(request_id), "method": "eth_subscribe",
        "params": ["logs", _robinhood_log_filter(pool)],
    }


_READER_DARK_LOGGED = {}          # cause -> last log time; one line per cause per minute


def _robinhood_batch_filters(pools):
    """At most two filters for any pool set: V4 pools share the PoolManager address and differ in
    topics[1]; V2/V3 pools are their own addresses, so they go in one address list."""
    pools = sorted({str(p).lower() for p in pools})
    v4 = [p for p in pools if len(p) == 66]
    pairs = [p for p in pools if len(p) == 42]
    filters = []
    if v4:
        v4_filter = _robinhood_log_filter(v4[0])
        filters.append({**v4_filter, "topics": [v4_filter["topics"][0], v4]})
    if pairs:
        filters.append({**_robinhood_log_filter(pairs[0]), "address": pairs})
    return filters


def robinhood_activity_logs(pool, first_block, last_block, *, rpc_url=None, http=None):
    """Logs for one pool or a list of pools in [first_block, last_block], or None when unreadable.

    One failed read is retried once after 200 ms. A read that fails twice returns None, which
    holds the rail, and its cause is logged so a dark hold can be traced to a status or a code.
    """
    from sniper.execution.evm_wallet import _http, _rpc
    from sniper.settings import memecoin as cfg
    rpc_url = rpc_url or cfg.evm_rpc_for_chain("4663")
    http = http or _http
    span = {"fromBlock": hex(max(0, int(first_block))), "toBlock": hex(max(0, int(last_block)))}
    filters = ([_robinhood_log_filter(pool)] if isinstance(pool, str)
               else _robinhood_batch_filters(pool))
    out = []
    for flt in filters:
        result = cause = None
        for attempt in range(2):
            if attempt:
                time.sleep(0.2)
            try:
                payload = _rpc(rpc_url, "eth_getLogs", [{**flt, **span}], http=http)
            except Exception as exc:
                cause = "%s: %s" % (type(exc).__name__, str(exc)[:120])
                continue
            if not isinstance(payload, dict) or payload.get("error"):
                error = payload.get("error") if isinstance(payload, dict) else None
                cause = "error body %s" % (str(error)[:160] if error else type(payload).__name__)
                continue
            if isinstance(payload.get("result"), list):
                result = payload["result"]
                break
            cause = "result is %s" % type(payload.get("result")).__name__
        if result is None:
            now = time.monotonic()
            kind = cause.split(":")[0].split(" ")[0] if cause else "unknown"
            if now - _READER_DARK_LOGGED.get(kind, -1e9) >= 60.0:
                _READER_DARK_LOGGED[kind] = now
                logger.warning("vol_death 4663 witness read failed twice: blocks %s..%s cause=%s",
                               first_block, last_block, cause)
            return None
        out.extend(result)
    return out


def _handle_robinhood_message(state, message, pending, *, now_ts):
    if not isinstance(message, dict):
        return False
    if "id" in message and message.get("result"):
        pool = pending.pop(message.get("id"), None)
        if pool:
            state.record_coverage(pool, now_ts=now_ts)
        return True
    result = ((message.get("params") or {}).get("result") or {})
    if not isinstance(result, dict):
        return False
    if result.get("number") is not None:
        try:
            return state.record_head(int(result["number"], 16), now_ts=now_ts)
        except (TypeError, ValueError):
            return False
    topics = result.get("topics") or ()
    address = str(result.get("address") or "").lower()
    from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS
    pool = str(topics[1]).lower() if (
        address == V4_POOL_MANAGERS["4663"] and len(topics) > 1) else address
    return state.record_pool_activity(pool, now_ts=now_ts)


def run_robinhood_volume_stream(*, ws_url, bindings_fn, now_fn, state=None,
                                connect_fn=None, sleep_fn=None, stale_after_s=15.0,
                                heartbeat_s=900.0):
    """Run the Robinhood pool event lane. This function does not submit a transaction.

    `stale_after_s` forces a reconnect when newHeads has gone quiet this long. A subscription
    that stays OPEN but stops being SERVICED (a real free-RPC failure mode) raises no exception,
    so before this nothing here would ever notice or recover — the armed rail would correctly
    freeze on the stale read (decision()'s own 5s health_window_s), but silently, forever, until
    the whole process restarted. 15s is 3x that window: wide enough that normal block-time
    jitter never trips it, close enough that a stalled feed does not stay dark for the rest of
    the process's life.

    `heartbeat_s` is a periodic operator-visible log of stream coverage (bound/covered pools,
    head age). This lane had NO liveness signal short of a hard connection exception — its
    Solana tickstream sibling already logs a delivery summary every 900s; this one logged only
    on failure.
    """
    state = state or ROBINHOOD_VOLUME_STATE
    sleep_fn = sleep_fn or time.sleep
    if connect_fn is None:
        from websockets.sync.client import connect as connect_fn
    delay = 1.0
    last_heartbeat = None
    while True:
        try:
            bindings = robinhood_pool_bindings(bindings_fn())
            state.replace_bindings(bindings, now_ts=_epoch(now_fn()) or time.time())
        except Exception:
            # Every OTHER seam in this loop already fails toward "reconnect and keep going" —
            # this was the one call outside all of them. Its current caller (open_real_positions)
            # already catches its own exceptions, so this is a second line of defense, not a
            # response to an observed failure.
            logger.exception("Robinhood volume stream: binding refresh failed")
            sleep_fn(delay)
            delay = min(30.0, delay * 2.0)
            continue
        if not bindings:
            sleep_fn(1.0)
            continue
        pending = {}
        subscribed = set()
        request_id = 1
        try:
            state.disconnect()
            with connect_fn(ws_url, open_timeout=10) as ws:
                ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": request_id,
                    "method": "eth_subscribe", "params": ["newHeads"],
                }))
                request_id += 1
                delay = 1.0
                # Baseline for a connection that never delivers a SINGLE head: head_age_s stays
                # None forever in that case (nothing to measure FROM), which would otherwise let
                # a subscription that never once answered sit blind for the rest of the process's
                # life exactly like the stalled-after-activity case this loop now also catches.
                connected_at = _epoch(now_fn()) or time.time()
                while True:
                    bindings = robinhood_pool_bindings(bindings_fn())
                    now_ts = _epoch(now_fn()) or time.time()
                    state.replace_bindings(bindings, now_ts=now_ts)
                    if not bindings:
                        break
                    for pool in sorted(set(bindings) - subscribed):
                        ws.send(json.dumps(robinhood_log_subscription(request_id, pool)))
                        pending[request_id] = pool
                        subscribed.add(pool)
                        request_id += 1
                    try:
                        raw = ws.recv(timeout=1.0)
                    except TimeoutError:
                        raw = None
                    if raw is not None:
                        _handle_robinhood_message(
                            state, json.loads(raw), pending,
                            now_ts=_epoch(now_fn()) or time.time())
                    now_ts = _epoch(now_fn()) or time.time()
                    if last_heartbeat is None or now_ts - last_heartbeat >= heartbeat_s:
                        last_heartbeat = now_ts
                        logger.info("Robinhood volume stream: %s", state.coverage_snapshot(now_ts))
                    stale = state.head_age_s(now_ts)
                    if stale is None:
                        stale = max(0.0, now_ts - connected_at)
                    if stale > stale_after_s:
                        logger.warning(
                            "Robinhood volume stream: newHeads stalled %.1fs (>%.1fs) — "
                            "reconnecting", stale, stale_after_s)
                        break
        except Exception:
            logger.exception("Robinhood volume stream failed")
        finally:
            state.disconnect()
        sleep_fn(delay)
        delay = min(30.0, delay * 2.0)

# position_id -> the highest threshold already recorded, so a position that stays quiet writes one
# row per ESCALATION instead of one per maintenance pass.
_RECORDED = {}


def stream_healthy(conn, *, now_iso, chain_id, health_window_s=60.0):
    """Has swap evidence landed recently ON THIS CHAIN, for any coin?

    This is the quiet-vs-blind discriminator. If swaps are arriving for other mints ON THE SAME
    CHAIN, silence on THIS mint is a real observation about this coin. If nothing is arriving on
    this chain, the silence is ours, not the market's.

    CHAIN-SCOPED (2026-08-26, Beta pre-deploy review). SWAP_SOURCE rows exist for solana ONLY
    (16,661 rows; zero on 4663/8453/56/1). Unscoped, a 4663 position BORROWED Solana's health,
    then found no swaps for its own mint, fell back to silence-since-entry, and would have exited
    every non-Solana position at exit_s regardless of liveness — the blind-freeze guard failing
    open on the axis it was built to close, one level up: a dead STREAM was guarded, a chain the
    stream never covered was not. Scoped, an uncovered chain reads blind -> the rail freezes there
    and self-arms per chain as coverage appears, with no cutoff to hand-write. An unknown/missing
    chain_id reads blind for the same reason. chain_id on marks matches network_id on positions
    exactly (no cross terms; verified 2026-08-26).
    """
    from datetime import datetime, timedelta
    if not chain_id:
        return False
    try:
        cutoff = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                  - timedelta(seconds=float(health_window_s))).isoformat()
    except (TypeError, ValueError):
        return False
    row = conn.execute(
        "SELECT 1 FROM memecoin_position_marks "
        "WHERE chain_id=? AND source=? AND sweep_ts>=? AND sweep_ts<=? LIMIT 1",
        (str(chain_id), SWAP_SOURCE, cutoff, str(now_iso))).fetchone()
    return bool(row)


def stream_delivered_other_mint(conn, *, chain_id, mint, since_iso, now_iso):
    """Did the swap stream deliver a swap for ANOTHER mint on this chain in (since, now]? That is
    the proof the stream was alive while `mint` was silent. A failed read proves nothing (False).
    Runs only when the rail would otherwise fire, so its chain-wide index range is affordable."""
    try:
        row = conn.execute(
            "SELECT 1 FROM memecoin_position_marks WHERE chain_id=? AND source=? AND sweep_ts>? "
            "AND sweep_ts<=? AND mint<>? LIMIT 1",
            (str(chain_id or ""), SWAP_SOURCE, str(since_iso), str(now_iso), str(mint or ""))
        ).fetchone()
    except Exception:
        logger.exception("stream vouch read failed for %s", mint)
        return False
    return bool(row)


def newest_swap_age_s(conn, *, mint, since_iso, now_iso, chain_id):
    """Seconds since the newest SWAP for `mint` within this hold, or None if none was ever seen.

    Bounded by `since_iso` (the open) so a swap from a previous hold of the same coin cannot make
    the current hold look alive.
    """
    from datetime import datetime
    # chain_id LEADS idx_mc_marks_uniq (chain_id, mint, sweep_ts): with it this is an index seek
    # (~0ms); without it a skip-scan measured at 376ms/call on the funded exit path (Beta,
    # 2026-08-26). The predicate must be in the SEARCH line, not just the semantics.
    # BOUNDED ABOVE AT now_iso (2026-08-28, Codex review). Production cannot read the future, so
    # live behaviour is unchanged — but a replay or a grader passes a PAST now_iso against a table
    # that has since grown, reads marks from after the moment being judged, and returns a negative
    # silence. A full-database replay of 44813 returned -2050.69s that way. Every reader of this
    # rail's history inherited the defect; the rail itself never did.
    row = conn.execute(
        "SELECT MAX(sweep_ts) FROM memecoin_position_marks "
        "WHERE chain_id=? AND mint=? AND source=? AND sweep_ts>=? AND sweep_ts<=?",
        (str(chain_id or ""), mint, SWAP_SOURCE, since_iso, str(now_iso))).fetchone()
    newest = row[0] if row else None
    if not newest:
        return None
    try:
        a = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (b - a).total_seconds()


def swap_lane_covers(conn, *, mint, chain_id):
    """Has the swap lane EVER recorded a swap for this pool?

    Coverage is a property of the pool, not of the hold, so this is deliberately unbounded in time.
    A pool we have seen trade before and that is quiet now is evidence of quiet. A pool we have
    never seen trade is evidence of nothing, and must not be read as silence.
    """
    row = conn.execute(
        "SELECT 1 FROM memecoin_position_marks WHERE chain_id=? AND mint=? AND source=? LIMIT 1",
        (str(chain_id or ""), mint, SWAP_SOURCE)).fetchone()
    return bool(row)


def window_observable(conn, *, mint, since_iso, chain_id=None):
    """Did ANY mark, from any source, exist for this coin during the hold?

    THE DENOMINATOR PROBLEM (Gamma, 2026-08-26). Swap evidence reaches ~69% of holds, and the
    missing 31% correlates with the stream being down — precisely the condition where the rule is
    dangerous. Without recording observability per window, the would-fire RATE reads biased low
    exactly in the blind case, because unobserved windows silently leave the denominator.

    So every window resolves to one of three states, and the reader can tell them apart:
        observed-quiet      swap evidence exists, silence measured, no threshold crossed
        observed-would-fire swap evidence exists, silence crossed a threshold
        NOT OBSERVABLE      nothing was watching; the window carries no information either way
    """
    row = conn.execute(
        # CHAIN FIRST, for the same reason the two reads above already do it: a mint-only match can
        # borrow another chain's tape and turn "we could not see" into a false "we could see",
        # which corrupts the shadow denominator this function exists to compute.
        "SELECT 1 FROM memecoin_position_marks WHERE mint=? AND sweep_ts>=?"
        + (" AND chain_id=?" if chain_id else "") + " LIMIT 1",
        ((mint, since_iso, str(chain_id)) if chain_id else (mint, since_iso))).fetchone()
    return bool(row)


def state_witness(conn, *, mint, chain_id, since_iso, window_start_iso, now_iso,
                  health_window_s=60.0):
    """Does the pool's own reserve tape say this coin is STILL, over the silence window?

    THE DEFECT THIS EXISTS FOR. Position 44813 exited `vol_death` on 2026-08-28 while the coin was
    trading both sides. The silence clock reads only SWAP_SOURCE, that stream ran gaps of 112s,
    45s and 61s across the hold, and in the final "silent" 60 seconds STATE_SOURCE logged twelve
    marks ranging 0.00346 to 0.00503 — a 45% swing. The rail measured our own packet loss as
    market death and sold a live position.

    A dead coin's reserves are still. A dropped websocket's are not.

    THE WITNESS IS VALUE VARIANCE, NOT MARK COUNT, and the choice is load-bearing in both
    directions. Counting marks would measure whether the READER is running, so a healthy reader on
    a genuinely dead coin would block this rail forever — the defect inverted. Measured on the
    persisted tape: 12.4% of consecutive state marks on the same mint carry an IDENTICAL mid (32 of
    259 pairs over 48h), so the source does emit without a price change and a count is not a proxy
    for movement.

    The design is robust to how the frame is emitted, which is why it does not depend on settling
    that question. If a sync frame arrives only when reserves change, a dead pool produces NO marks
    in the window. If it arrives regardless, a dead pool produces marks that are all EQUAL. Both
    land on `still`, so both fire the rail correctly.

    Returns one of:
        "moving"     two or more distinct reserve prices inside the window — the coin is alive
        "still"      the state lane covers this coin and shows no movement — corroborates death
        "uncovered_transient"   no state mark covers this hold
        "reader_dark" the state lane is silent CHAIN-WIDE, so its quiet is ours, not the coin's

    `uncovered` is a real and common state, not an edge case: 20 of 40 swap-covered funded Solana
    holds since 2026-08-30 carry zero state marks.

    CORRECTED 2026-09-02. This docstring used to claim that swaps without state marks PROVE the
    lane was not watching, "because a swap necessarily moves reserves and would have emitted".
    That is false. Position 45597 carries 185 swap marks, a `sol_position_pools` row and a
    `sol_pool_vaults` row, and zero state marks. The lane was watching.

    CORRECTED AGAIN 2026-09-22, and the 09-02 correction named the wrong cause. It said a pool
    answers only when its PROGRAM emits an update we decode. The program is not the gate.
    `SolanaSwapMarkCache` keeps ONE mark per mint, so a `sol_swap` mark replaces a `sol_sync` mark
    in the same slot and `refresh_open_positions` persists only the survivor. A STATE row reaches
    `memecoin_position_marks` only when the newest event was one-sided: about 1% of marks.
    Measured on 601 Solana mints, the SWAP-only share falls from 96% at one mark to 4% above 300,
    and every program shows both labels.

    So a missing state mark measures the SAMPLE, not the pool. The `structural` label claimed a
    pool could never answer and is withdrawn; an uncovered hold is one fact again. It freezes the
    rail, as it did before. The caller decides what to do with it; this function only reports.
    """
    try:
        # COUNT(DISTINCT mid), one row: the question is "did the price take more than one value",
        # and counting rows would answer "is the reader running" instead — see the note above.
        # chain_id LEADS idx_mc_marks_uniq (chain_id, mint, sweep_ts). Without it this is a
        # skip-scan measured at 376ms/call on the funded exit path, and this runs per position per
        # sweep.
        row = conn.execute(
            "SELECT COUNT(DISTINCT mid) FROM memecoin_position_marks "
            "WHERE chain_id=? AND mint=? AND source=? AND sweep_ts>=? AND sweep_ts<=? AND mid>0",
            (str(chain_id or ""), mint, STATE_SOURCE, str(window_start_iso),
             str(now_iso))).fetchone()
    except Exception:
        logger.exception("state witness read failed for %s", mint)
        return "reader_dark"          # a failed read is our blindness, never the coin's death
    if row and int(row[0] or 0) >= 2:
        return "moving"
    covered = conn.execute(
        "SELECT 1 FROM memecoin_position_marks "
        "WHERE chain_id=? AND mint=? AND source=? AND sweep_ts>=? AND sweep_ts<=? LIMIT 1",
        (str(chain_id or ""), mint, STATE_SOURCE, str(since_iso or ""), str(now_iso))).fetchone()
    if not covered:
        return "uncovered_transient"
    # The lane watched this pool but shows at most one price in the window. Distinguish a still
    # pool from a stopped reader the same way the swap side does: ask whether the lane is emitting
    # for ANY coin on this chain right now.
    from datetime import datetime, timedelta
    try:
        cutoff = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                  - timedelta(seconds=float(health_window_s))).isoformat()
    except (TypeError, ValueError):
        return "reader_dark"
    alive = conn.execute(
        "SELECT 1 FROM memecoin_position_marks "
        "WHERE chain_id=? AND source=? AND sweep_ts>=? AND sweep_ts<=? LIMIT 1",
        (str(chain_id or ""), STATE_SOURCE, cutoff, str(now_iso))).fetchone()
    return "still" if alive else "reader_dark"


def crossed(silence_s, thresholds=DEFAULT_THRESHOLDS_S):
    """Thresholds this silence has passed, ascending."""
    try:
        value = float(silence_s)
    except (TypeError, ValueError):
        return []
    return [t for t in sorted(thresholds) if value >= float(t)]


def evaluate(conn, *, now_iso, open_positions, has_loss_rail_fn, thresholds=DEFAULT_THRESHOLDS_S,
             health_window_s=60.0, hold_age_fn=None):
    """Would-fire rows for the current book. Returns (rows, tally); never raises.

    A row is produced only when the stream is healthy AND the coin's own silence has crossed the
    smallest threshold. A blind stream yields a `blind_frozen` row carrying no would-fire, because
    what it measures is our own outage.
    """
    rows, tally = [], {"checked": 0, "quiet": 0, "blind_frozen": 0, "no_swap_ever": 0}
    # Health is per CHAIN (2026-08-26): Solana streaming says nothing about a 4663 coin. Memoized
    # per pass — it is a global fact per chain, not per position.
    _health = {}

    def _chain_ok(chain):
        if chain not in _health:
            _health[chain] = stream_healthy(conn, now_iso=now_iso, chain_id=chain,
                                            health_window_s=health_window_s)
        return _health[chain]

    for index, pos in enumerate(open_positions or ()):
        try:
            if not has_loss_rail_fn(pos):
                continue
            tally["checked"] += 1
            if not _chain_ok(pos.get("network_id")):
                # FREEZE. The clock does not run while we cannot see the market.
                tally["blind_frozen"] += 1
                rows.append({"position_id": pos.get("id"), "mint": pos.get("mint"),
                             "silence_s": None, "thresholds": [], "blind_frozen": True,
                             "stream_healthy": False, "observable": False})
                continue
            silence = newest_swap_age_s(conn, mint=pos.get("mint"),
                                        since_iso=pos.get("opened_at") or "", now_iso=now_iso,
                                        chain_id=pos.get("network_id"))
            if silence is None:
                # No swap ever seen in this hold. That is the 31%-coverage case, and it is NOT a
                # silent market — it is an unobserved one. Judge by hold age only to record it, and
                # never produce a would-fire from it.
                tally["no_swap_ever"] += 1
                rows.append({"position_id": pos.get("id"), "mint": pos.get("mint"),
                             "silence_s": (hold_age_fn(pos) if hold_age_fn else None),
                             "thresholds": [], "blind_frozen": False, "stream_healthy": True,
                             "no_swap_ever": True,
                             "observable": window_observable(
                                 conn, mint=pos.get("mint"), chain_id=pos.get("network_id"),
                                 since_iso=pos.get("opened_at") or "")})
                continue
            hits = crossed(silence, thresholds)
            if not hits:
                continue
            tally["quiet"] += 1
            rows.append({"position_id": pos.get("id"), "mint": pos.get("mint"),
                         "silence_s": float(silence), "thresholds": hits,
                         "blind_frozen": False, "stream_healthy": True, "observable": True})
        except Exception:
            # By index: a handler that re-reads the row that just failed can raise from inside the
            # handler and take the maintenance pass with it.
            logger.exception("volume-death shadow could not evaluate open position #%d", index)
    return rows, tally


def record_new_escalations(conn, rows, *, now_iso, record_fn):
    """Persist one row per position per ESCALATION, not per pass. Returns the count written."""
    written = 0
    live = {r["position_id"] for r in rows}
    for key in list(_RECORDED):
        if key not in live:
            _RECORDED.pop(key, None)          # closed or recovered; a later quiet spell is new
    for row in rows:
        try:
            hits = row.get("thresholds") or []
            if not hits:
                continue
            top = max(hits)
            if _RECORDED.get(row["position_id"], -1) >= top:
                continue
            _RECORDED[row["position_id"]] = top
            record_fn(row, now_iso)
            written += 1
        except Exception:
            logger.exception("volume-death shadow could not record %s", row.get("position_id"))
    return written


def _is_solana_pubkey(text):
    """True for a base58 32-byte address; False for a label such as 'slot_lane'."""
    try:
        from solders.pubkey import Pubkey
        return str(Pubkey.from_string(str(text))) == str(text)
    except Exception:
        return False


# THE VERDICTS THAT REACH THE AUDIT, IN ONE PLACE. `exit_reason` is 'vol_death' for every one of
# these, so the audit's `detail` is the only column the operator's covered-vs-uncovered split can
# read. The runner used to carry this list inline in two places, and when `state_uncovered_frozen`
# replaced `measured_no_state_witness` on 2026-08-29 the decision stopped being recorded while
# still being made — caught by the guard that enumerates labels from THIS function's source and
# intersects them against the runner. A list that lives beside the returns cannot drift from them.
POST_THRESHOLD_REASONS = ("measured_state_still", "measured_onchain_still", "exact_quote_moving",
                          "stream_unvouched",
                          "route_changed",
                          "route_unknown_entry", "route_unknown",
                          "state_uncovered_frozen",
                          "state_witness_unknown",
                          "state_moving", "state_reader_dark", "executable_price_moved",
                          "onchain_pending", "onchain_moving", "onchain_unavailable",
                          "onchain_unavailable:binding_missing", "onchain_unavailable:route_unknown",
                          "onchain_unavailable:identity_mismatch", "onchain_unavailable:provider",
                          "onchain_unavailable:lookup_failed", "mint_moving",
                          "state_reader_dark:no_head", "state_reader_dark:no_confirm_fn",
                          "state_reader_dark:getlogs_failed",
                          # The tape rail audits its blindness too (operator order 2026-10-01):
                          # unlike the bare `blind_frozen`, each of these writes a row per minute.
                          "measured_tape_still", "blind_frozen:route_unreadable",
                          "blind_frozen:curve_route", "blind_frozen:entry_pool_unknown",
                          "blind_frozen:store_unavailable",
                          "blind_frozen:feed_absent", "blind_frozen:feed_stale",
                          "blind_frozen:no_pool", "blind_frozen:pool_unmapped",
                          "blind_frozen:protocol_uncovered", "blind_frozen:tape_unreadable",
                          *TAPE_UNVOUCHED.values(),
                          # The 56 poll rail audits its blindness the same way.
                          "blind_frozen:poll_stale", "blind_frozen:pool_uncovered",
                          "blind_frozen:pool_set_changed", "blind_frozen:poll_unvouched")
# Decided NOT to fire on positive counter-evidence or on missing evidence. Logged loudly: the rate
# of these is the entire justification for keeping a second witness.
HELD_WITNESS_REASONS = ("state_moving", "state_reader_dark", "state_uncovered_frozen",
                        "state_witness_unknown",
                        "route_changed",
                        "route_unknown_entry", "route_unknown",
                        "executable_price_moved", "exact_quote_moving", "stream_unvouched",
                        "onchain_pending", "onchain_moving", "onchain_unavailable",
                        "onchain_unavailable:binding_missing", "onchain_unavailable:route_unknown",
                        "onchain_unavailable:identity_mismatch", "onchain_unavailable:provider",
                        "onchain_unavailable:lookup_failed", "mint_moving",
                        "state_reader_dark:no_head", "state_reader_dark:no_confirm_fn",
                        "state_reader_dark:getlogs_failed", *TAPE_UNVOUCHED.values())


# Exact quote movement can veto a silence exit. Flat quotes cannot prove absent swaps.
# ZINU 59458 crossed the old rule with a 0.271% quote range and no pool-state witness.
# Keep the tape for the movement veto and stream check. Uncovered pools remain unknown.
EXACT_WITNESS_MIN_QUOTES = 3
EXACT_WITNESS_EDGE_S = 10.0
_EXACT_TAPE_KEEP_S = 300.0
_EXACT_TAPE = {}
_EXACT_TAPE_LOCK = threading.Lock()


def record_exact_quote(position_id, *, quoted_at, price):
    """Keep one exact whole-bag unit price for `position_id`. Never raises."""
    try:
        ts, px = _epoch(quoted_at), float(price)
        if ts is None or not (px > 0):
            return
        with _EXACT_TAPE_LOCK:
            tape = _EXACT_TAPE.setdefault(position_id, [])
            if tape and tape[-1][0] >= ts:
                return                             # a cached preview re-read, or out of order
            tape.append((ts, px))
            horizon = ts - _EXACT_TAPE_KEEP_S
            while tape and tape[0][0] < horizon:
                tape.pop(0)
            for pid in [k for k, v in _EXACT_TAPE.items() if not v or v[-1][0] < horizon]:
                _EXACT_TAPE.pop(pid, None)         # closed positions stop being quoted
    except Exception:
        logger.exception("exact-quote tape could not record pos=%s", position_id)


def exact_quote_witness(position_id, *, window_start_iso, now_iso, max_range_pct):
    """'still', 'moving', or 'sparse' for the exact quotes inside the silence window."""
    start, now = _epoch(window_start_iso), _epoch(now_iso)
    if start is None or now is None:
        return "sparse"
    with _EXACT_TAPE_LOCK:
        window = [px for ts, px in _EXACT_TAPE.get(position_id, ()) if start <= ts <= now]
        stamps = [ts for ts, _px in _EXACT_TAPE.get(position_id, ()) if start <= ts <= now]
    if len(window) >= 2 and ((max(window) / min(window)) - 1.0) * 100.0 >= float(max_range_pct):
        return "moving"
    if (len(window) < EXACT_WITNESS_MIN_QUOTES or stamps[0] - start > EXACT_WITNESS_EDGE_S
            or now - stamps[-1] > EXACT_WITNESS_EDGE_S):
        return "sparse"
    return "still"


def executable_price_moved(conn, *, mint, chain_id, window_start_iso, now_iso, min_range_pct):
    """Did the price this bag can actually sell into MOVE while the rail called the coin silent?

    WHY A THIRD WITNESS EXISTS. Both witnesses above ask a venue whether it saw a trade, so both
    inherit one assumption: that we bound the right venue. When that assumption fails they do not
    return "unknown" — they return `still`, confidently, about a pool nobody trades. Measured over
    all 7 funded 4663 `vol_death` closes: THREE sold a coin whose executable price moved inside the
    window the rail called silent — 48336 CARDPAD 38.9%, 49013 LOCK 16.3%, 47633 DUET 43.1%. Two
    were pons curves (no pool exists to watch) and one was a genuine uniswap_v4 pool, so binding
    the venue more carefully cannot close this on its own.

    This witness asks no venue. It reads the exact whole-bag quote tape we already persist, which
    is derived from the venue's own reserves, so movement in it is movement in the thing being
    traded. A dead coin's sell price does not move.

    A RANGE, NOT A COUNT, and the threshold is why. On EVM the quote is denominated through
    ETH/USD, so a static pool still produces marks that differ slightly — position 48590 GTAVI
    carries 23 distinct mids across a 1.7% range on a pool that really was dead. Counting distinct
    values would freeze the rail permanently on every EVM coin. The default cut separates 1.7% from
    16.3% with room on both sides; it is fitted to n=7 and should be revisited as fires accumulate.

    VETO ONLY. Returns True to FREEZE. It can never make this rail fire, so the worst case is that
    volume-death stops firing — the resting state this module already documents as correct for a
    rail that cannot tell a dead pool from an unwatched one. A failed read returns False and leaves
    the decision exactly as it was: our broken query is not evidence that the coin traded.

    SCOPE, STATED SO IT CANNOT BE MISREAD AS SAFETY. Only 4663 writes an executable tape. Measured
    2026-08-25 onward: `fastlane_rpc` carries 10,438 marks over 71 mints, ALL of them 4663. Solana
    prices its funded positions from `fastlane_mid`, which is the batched DexScreener lane — the
    same nominal price the exit decision stopped trusting — so it is not evidence about the venue
    and this witness must not read it. On a chain with no executable tape the veto returns False by
    ABSENCE, and every Solana vol_death close replays unchanged: 25 of 25 still fire. That is a
    blind spot, not a clean bill of health, and a fire with no tape behind it is logged as such.
    The same applies to a window holding a SINGLE mark — see the two-mark guard below.
    """
    try:
        row = conn.execute(
            # chain_id LEADS idx_mc_marks_uniq (chain_id, mint, sweep_ts), same reason as the
            # state witness above: without it this is a skip-scan on the funded exit path.
            "SELECT COUNT(*), MIN(mid), MAX(mid) FROM memecoin_position_marks "
            "WHERE chain_id=? AND mint=? AND source=? AND sweep_ts>=? AND sweep_ts<=? AND mid>0",
            (str(chain_id or ""), mint, EXECUTABLE_SOURCE, str(window_start_iso),
             str(now_iso))).fetchone()
    except Exception:
        logger.exception("executable-move veto read failed for %s", mint)
        return False
    marks, low, high = (row or (0, None, None))
    if (marks or 0) < 2:
        # TWO, NOT ONE (2026-09-03, Gamma, Protocol C review of the commit above). A range needs
        # two points. At exactly one mark MIN equals MAX, the range is 0.0%, and this function
        # returns False having proved nothing — indistinguishable in the audit from a coin it
        # checked and cleared. The first version of this guard tested `not marks`, so the
        # single-mark case skipped the warning too and reported as checked.
        #
        # It is not an edge case at the operating point: 3 of the 7 funded 4663 vol_death fires
        # carried exactly ONE mark in the window (47462 MARKETPLIER, 47961 BUCK, 48020 Liluni),
        # and a fourth (47633 DUET) carried exactly two. This rail fires on coins nobody is
        # quoting, which is the same condition that leaves the tape sparse, so the sparse case is
        # the NORMAL case here rather than the tail. A whole-tape rate does not describe it — any
        # such rate conditions on the coin being watched, which is precisely what is missing.
        logger.warning(
            "volume-death: %d executable mark(s) for %s on %s in the silence window, need 2 for a "
            "range — the executable-move veto could not check this fire", marks or 0, mint, chain_id)
        return False
    try:
        low, high = float(low), float(high)
    except (TypeError, ValueError):
        return False
    if low <= 0 or high <= 0:
        return False
    return ((high / low) - 1.0) * 100.0 >= float(min_range_pct)


def mint_audit_note(cache, position_id, why):
    """' mint=<outcome>' for the vol_death audit detail when the mint witness was the decision's
    last step (a fire, or its veto); '' otherwise, so a stale note never rides on another reason."""
    if why not in ("measured_state_still", "measured_onchain_still", "mint_moving"):
        return ""
    try:
        text = cache.mint_note(position_id) if hasattr(cache, "mint_note") else ""
    except Exception:
        return ""
    return (" " + text) if text else ""


def held_log_due(cache, position_id, why):
    """False for a repeat `mint_moving` HELD line inside one veto episode (Lambda F10): a cached veto
    returns every sweep, and a line per sweep would bury the log. Every other reason logs as before."""
    if why != "mint_moving":
        return True
    try:
        return cache.held_log_due(position_id)
    except Exception:
        return True


def armed_exit_decision(conn, pos, *, now_iso, held_s, exit_s, min_opened_at,
                        health_window_s=60.0, stream_ok=None, require_state_witness=True,
                        robinhood_state=None, robinhood_confirm_fn=None,
                        move_veto_pct=None, reserve_witness_cache=None,
                        require_onchain=True, exit_quote_evidence=None, tape_path=None):
    """Decide the funded volume-death exit, then refuse it if the coin visibly traded.

    The venue witnesses live in `_armed_exit_decision`. This wrapper applies the one witness that
    asks no venue — see `executable_price_moved`. It is applied HERE rather than at each fire site
    so that a witness added later inherits it by construction, which is how the `return True`
    fall-through in the state witness became a defect once before.
    """
    fire, silence, why = _armed_exit_decision(
        conn, pos, now_iso=now_iso, held_s=held_s, exit_s=exit_s, min_opened_at=min_opened_at,
        health_window_s=health_window_s, stream_ok=stream_ok,
        require_state_witness=require_state_witness, robinhood_state=robinhood_state,
        robinhood_confirm_fn=robinhood_confirm_fn,
        reserve_witness_cache=reserve_witness_cache, tape_path=tape_path)
    onchain_only = False
    if not fire:
        # A confirmed vault-history read can measure a bound Solana pool even when the WS never
        # delivered its swap or reserve frames, or delivered a trade late. The async proof binds
        # the confirmed head to local movement slots and expires on new movement; receipt time
        # alone cannot postpone the frozen silence window. Local positive movement still vetoes.
        if (str(pos.get("network_id")) != "solana" or not require_onchain
                or why not in ("measured", "blind_frozen", "swap_lane_uncovered", "stream_unvouched",
                               "state_reader_dark", "state_uncovered_frozen",
                               "state_witness_unknown")):
            return fire, silence, why
        try:
            if (not math.isfinite(float(held_s)) or not math.isfinite(float(exit_s))
                    or float(exit_s) <= 0 or float(held_s) < float(exit_s)):
                return fire, silence, why
        except (TypeError, ValueError, OverflowError):
            return fire, silence, why
        onchain_only = True
    from datetime import datetime, timedelta
    try:
        window_start = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                        - timedelta(seconds=float(exit_s))).isoformat()
    except (TypeError, ValueError, OverflowError):
        return (False, silence, "onchain_unavailable") if require_onchain else (fire, silence, why)
    if str(pos.get("network_id")) == "solana" and require_onchain:
        plan = (exit_quote_evidence.get("route_plan")
                if isinstance(exit_quote_evidence, dict) else None)
        # ONE KNOWN EXIT POOL OR NOTHING (Codex review, parked branch). Every token-facing leg must
        # name its pool (Jupiter `ammKey`, the Raydium fallback's `poolId`); a split sell or a leg
        # with no pool means quiet in one pool proves nothing about the route the sell would take.
        token_legs = [leg for leg in plan if isinstance(leg, dict)
                      and leg.get("inputMint") == pos.get("mint")] if isinstance(plan, list) else []
        leg_keys = [str(leg.get("ammKey") or leg.get("poolId") or "") for leg in token_legs]
        pool_keys = set(leg_keys) if leg_keys and all(leg_keys) else set()
        current_pool_key = next(iter(pool_keys)) if len(pool_keys) == 1 else None
        if current_pool_key is None:
            return False, silence, "route_unknown"
        entry_pool_keys = {key.strip() for key in str(pos.get("quote_route_target") or "").split(",")
                           if key.strip()}
        if current_pool_key and entry_pool_keys and not any(
                _is_solana_pubkey(key) for key in entry_pool_keys):
            # LABEL-ONLY ENTRY ROUTE (2026-09-26, position 65860). Every slot-lane fill stores the
            # venue label 'slot_lane' as its entry route, so no exit route could ever match and the
            # rail was held as route_changed for good. The tape binding, resolved from our own entry
            # transaction, names the curve for a native-SOL Pump curve; an AMM binding cannot name
            # its ammKey, so that case stays held (fail-closed) under its own reason.
            from sniper.services.memecoin_solana_swap_marks import bound_curve_for_position
            curve = bound_curve_for_position(pos.get("id"), pos.get("mint"))
            if not curve:
                return False, silence, "route_unknown_entry"
            entry_pool_keys = {curve}
        if current_pool_key and entry_pool_keys and current_pool_key not in entry_pool_keys:
            return False, silence, "route_changed"
        if onchain_only:
            from sniper.settings import memecoin as cfg
            if exact_quote_witness(
                    pos.get("id"), window_start_iso=window_start, now_iso=now_iso,
                    max_range_pct=getattr(cfg, "MEMECOIN_VOLDEATH_MOVE_VETO_PCT", 5.0)) == "moving":
                return False, silence, "exact_quote_moving"
        # THE MINT-WIDE WITNESS (2026-09-28): after a still vault, the background probe also reads
        # the mint's newest signatures; another pool trading inside the window vetoes. `exit_s`
        # None (knob off) skips the read and is exactly the old decision.
        from sniper.settings import memecoin as _cfg
        mint_exit_s = float(exit_s) if getattr(_cfg, "MEMECOIN_VOLDEATH_MINT_WITNESS", False) else None
        proof = (reserve_witness_cache.onchain_witness(
            position_id=pos.get("id"), mint=pos.get("mint"),
            window_start_iso=window_start, now_iso=now_iso,
            current_pool_key=current_pool_key, exit_s=mint_exit_s,
            veto_max_s=getattr(_cfg, "MEMECOIN_VOLDEATH_MINT_VETO_MAX_S", 120.0))
            if reserve_witness_cache is not None else "unavailable")
        if proof == "moving:mint":
            return False, silence, "mint_moving"
        if proof == "moving":
            return False, silence, "onchain_moving"
        if proof == "pending":
            return False, silence, "onchain_pending"
        # A TYPED CAUSE (2026-09-26, PAIDCATS 65854): a bare "unavailable" alternating with
        # "pending" hid that the curve identity check could never pass. Each cause is its own literal
        # so the reason guard and the audit see it.
        if proof == "unavailable:binding_missing":
            return False, silence, "onchain_unavailable:binding_missing"
        if proof == "unavailable:route_unknown":
            return False, silence, "onchain_unavailable:route_unknown"
        if proof == "unavailable:identity_mismatch":
            return False, silence, "onchain_unavailable:identity_mismatch"
        if proof == "unavailable:provider":
            return False, silence, "onchain_unavailable:provider"
        if proof == "unavailable:lookup_failed":
            return False, silence, "onchain_unavailable:lookup_failed"
        if proof != "still":
            return False, silence, "onchain_unavailable"
    if move_veto_pct is None:
        from sniper.settings import memecoin as cfg
        move_veto_pct = getattr(cfg, "MEMECOIN_VOLDEATH_MOVE_VETO_PCT", 5.0)
    if float(move_veto_pct) <= 0:
        if onchain_only:
            return True, float(exit_s), "measured_onchain_still"
        return fire, silence, why
    if executable_price_moved(conn, mint=pos.get("mint"), chain_id=pos.get("network_id"),
                              window_start_iso=window_start, now_iso=now_iso,
                              min_range_pct=move_veto_pct):
        return False, silence, "executable_price_moved"
    if onchain_only:
        return True, float(exit_s), "measured_onchain_still"
    return fire, silence, why


def _armed_exit_decision(conn, pos, *, now_iso, held_s, exit_s, min_opened_at,
                        health_window_s=60.0, stream_ok=None, require_state_witness=True,
                        robinhood_state=None, robinhood_confirm_fn=None,
                        reserve_witness_cache=None, tape_path=None):
    """Use confirmed pool silence for the funded volume-death exit.

    Returns (fire, silence_s, why). Fires ONLY when every one of these holds:
      - the position belongs to the armed cohort (opened_at >= min_opened_at; the cutoff is
        written at deploy, so pre-cutover positions keep their frozen contract untouched)
      - the stream is HEALTHY — a dead stream reads as unbounded silence, and exiting on our own
        blindness would sell healthy coins on every websocket hiccup. Blind tape FREEZES this
        rail; it never fires it. Same discriminator the shadow validated.
      - the coin's own tape has been silent for exit_s. No swap since entry counts as silence
        since entry: a coin nobody has traded for the whole hold IS the dead state this rail
        exists for, not missing data.

    The frozen funded contract supplies the threshold.
    """
    if not min_opened_at or (pos.get("opened_at") or "") < str(min_opened_at):
        return False, None, "cohort_excluded"
    try:
        exit_s = float(exit_s)
    except (TypeError, ValueError):
        return False, None, "misconfigured"
    if exit_s <= 0:
        return False, None, "disarmed"
    chain = pos.get("network_id")
    if str(chain) == "4663":
        state = robinhood_state or ROBINHOOD_VOLUME_STATE
        confirm = robinhood_confirm_fn or robinhood_activity_logs
        return state.decision(pos, now_iso=now_iso, exit_s=exit_s, confirm_fn=confirm)
    from sniper.settings import memecoin as _cfg
    if str(chain) in _cfg.memecoin_voldeath_poll_chains():
        state = EVM_POLL_STATE.get(str(chain))
        if state is None:
            return False, None, "blind_frozen:poll_stale"
        from sniper.services import memecoin_bsc_volume_poll as _poll
        return state.decision(pos, now_iso=now_iso, exit_s=exit_s,
                              confirm_fn=_poll.confirm_fn_for(str(chain)))
    if str(chain) in _cfg.memecoin_voldeath_tape_chains():
        return tape_exit_decision(pos, now_iso=now_iso, exit_s=exit_s, tape_path=tape_path)
    if stream_ok is None:
        stream_ok = stream_healthy(conn, now_iso=now_iso, chain_id=chain,
                                   health_window_s=health_window_s)
    if not stream_ok:
        # Covers three states with one honest answer: our stream is down, this CHAIN has no swap
        # coverage at all (4663/EVM today), or the chain is unknown. In every one of them silence
        # is a statement about US, so the rail freezes. It self-arms per chain as coverage appears.
        return False, None, "blind_frozen"
    silence = newest_swap_age_s(conn, mint=pos.get("mint"),
                                since_iso=pos.get("opened_at"), now_iso=now_iso, chain_id=chain)
    if silence is None:
        # ZERO SWAPS SINCE ENTRY IS TWO DIFFERENT FACTS AND THIS USED TO CONFLATE THEM.
        # "The pool did not trade" is the dead state this rail exists for. "The swap lane never
        # watched this pool" is our own blindness, and `silence = held_s` turned that unknown into
        # a measured silence equal to the position's whole life — so a 61-second-old position
        # tripped a 60-second threshold on its first sweep, every time.
        #
        # Measured 2026-08-29 on all 24 armed fires: 16 carried ZERO swap marks for the hold while
        # price marks existed, i.e. we were quoting the coin and simply never observing swaps. They
        # closed at 60-61s at 1.00x. That is a time-stop wearing a liquidity rail's name.
        #
        # A chain-level stream check cannot answer this: a healthy Solana stream says nothing about
        # whether THIS pool is in it. So require positive proof of per-pool coverage — at least one
        # swap mark for this mint at some point — before absence is allowed to mean anything.
        if not swap_lane_covers(conn, mint=pos.get("mint"), chain_id=chain):
            return False, None, "swap_lane_uncovered"
        silence = held_s          # covered pool, no swap since entry: genuinely silent
    if silence is None:
        return False, None, "unmeasurable"
    if float(silence) < exit_s:
        return False, float(silence), "measured"

    # SECOND WITNESS. Swap silence alone is absence of evidence — it cannot tell a dead coin from a
    # dropped websocket, and on 2026-08-28 it sold position 44813 while the coin swung 45% inside
    # the window this clock called silent. So a fire now needs the pool's own reserves to agree.
    if not require_state_witness:
        return True, float(silence), "measured"
    from datetime import datetime, timedelta
    try:
        window_start = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                        - timedelta(seconds=float(exit_s))).isoformat()
    except (TypeError, ValueError):
        return False, float(silence), "unmeasurable"
    witness_args = dict(mint=pos.get("mint"), since_iso=pos.get("opened_at") or "",
                        window_start_iso=window_start, now_iso=now_iso,
                        health_window_s=health_window_s)
    if reserve_witness_cache is not None and str(chain) == "solana":
        witness = reserve_witness_cache.reserve_witness(**witness_args)
    else:
        witness = state_witness(conn, chain_id=chain, **witness_args)

    # THE STREAM MUST BE SEEN ALIVE DURING THIS SILENCE, BY ANOTHER COIN (2026-09-13, operator,
    # PVE 57075). The chain-level health check asks for ANY swap in the last 60 s. With one
    # position open the stream watched one mint, so the only swaps it could see were PVE's own:
    # the stream stopped delivering at 21:44:04, its last PVE swap kept "healthy" true for 60 s,
    # and 30.8 s later the rail sold a coin that traded $21.9K that minute and ran +81% the next.
    # So: a swap from a DIFFERENT mint after this coin's last swap proves the stream was
    # delivering. Without that proof, only the exact whole-bag quotes (another channel) may
    # corroborate, and they must be STILL.
    from sniper.settings import memecoin as cfg
    # `window_start` (now - exit_s) was parsed above; the silence began at now - silence, which is
    # at or before it, so the window start is a safe (narrower) bound if the subtraction fails.
    try:
        _silence_start = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                          - timedelta(seconds=float(silence))).isoformat()
    except (TypeError, ValueError):
        _silence_start = window_start
    _exit_window_start = window_start
    _exact = "sparse"
    if getattr(cfg, "MEMECOIN_VOLDEATH_EXACT_WITNESS", True):
        _exact = exact_quote_witness(
            pos.get("id"), window_start_iso=_exit_window_start, now_iso=now_iso,
            max_range_pct=getattr(cfg, "MEMECOIN_VOLDEATH_MOVE_VETO_PCT", 5.0))
    # EXACT-QUOTE VETO ON EVERY SOLANA FIRE. The sellable price moving through the silence means
    # the coin traded, whatever the other witnesses say. Veto only: it never fires the rail.
    if _exact == "moving":
        return False, float(silence), "exact_quote_moving"
    if (not stream_delivered_other_mint(conn, chain_id=chain, mint=pos.get("mint"),
                                        since_iso=_silence_start, now_iso=now_iso)
            and _exact != "still"):
        return False, float(silence), "stream_unvouched"

    if witness == "moving":
        # The coin traded. The silence was ours.
        return False, float(silence), "state_moving"
    if witness == "reader_dark":
        # The reserve lane is quiet chain-wide, so its agreement would be our own blindness
        # agreeing with itself. Two witnesses that share a failure are one witness.
        return False, float(silence), "state_reader_dark"
    if witness in ("uncovered", "uncovered_transient"):
        # THIS BRANCH USED TO FIRE, WHICH MADE THE SECOND WITNESS DECORATION. All 24 armed fires
        # took this path: every audit row reads `measured_no_state_witness`, allowed=1. A guard
        # whose blocking branch never executes is not a guard, and the operator reported the rail
        # as not working on exactly these closes.
        #
        # The lane never watched this pool, so there is no second witness to consult, and the
        # honest answer to "did the pool go quiet" is that we do not know. This rail freezes on
        # not-knowing everywhere else — blind stream, uncovered chain, reader dark — and this is
        # the same condition wearing a different name.
        #
        # CONSEQUENCE, STATED RATHER THAN DISCOVERED: while the reserve lane covers few funded
        # pools, this rail will seldom fire. That is the correct resting state for a rail that
        # cannot yet tell a dead pool from an unwatched one. It self-arms per pool as coverage
        # appears, exactly like the chain-level freeze above.
        #
        # `uncovered_structural` WAS WITHDRAWN ON 2026-09-22 AND ITS CASES ARRIVE HERE. The split
        # claimed a pool "cannot ever answer". It measured how many marks the position got.
        # `SolanaSwapMarkCache` keeps one mark per mint, so a swap mark replaces a state mark before
        # the fast lane persists either, and a STATE row survives only when the newest event was
        # one-sided: about 1% of marks. The SWAP-only share falls from 96% at 1 mark to 4% above
        # 300, and every program shows both labels, so the label tracked the sample, not the pool.
        return False, float(silence), "state_uncovered_frozen"
    if witness == "still":
        return True, float(silence), "measured_state_still"
    # FAIL CLOSED BY CONSTRUCTION. This used to be a bare `return True` fall-through, so ANY new
    # witness value a later change introduced would have FIRED the rail rather than frozen it —
    # on the ~50% of funded Solana holds that reach this branch. The one firing value is named
    # explicitly above; everything else, known or not, freezes.
    logger.warning("volume-death: unknown state witness %r for %s", witness, pos.get("mint"))
    return False, float(silence), "state_witness_unknown"
