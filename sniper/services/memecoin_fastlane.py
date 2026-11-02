"""Phase 2 Unit A — bounded open-position quote fast lane (2026-07-22).

Why this exists: 4663 position marks arrive at p50 56.5s / p90 124.9s on open positions
because their only price source is GeckoTerminal, whose 15s min-interval, 300s TTL and
SHARED 429 cooldown are load-bearing for every other chain (Global Constraint 8). Adding GT
calls to speed up 4663 would degrade the whole tape. So this lane does not use GT at all.

It also does not invent its own idea of "executable". It calls the position's OWN funded
`preview_exit_fn` — the same seam the real exit sweep uses — and divides by the ledger's open
quantity, which is byte-for-byte the runner's `_executable_unit_price` convention. That
matters more than it sounds (2026-07-22 review):

  * `preview_exit_fn` sizes the sell from the EXACT RAW on-chain balance. An earlier draft of
    this module rebuilt an atomic amount from the ledger's human quantity, which
    `evm_real_seams` explicitly warns against: memecoin balances exceed 2^53, so the float
    round-trip mis-sizes the bag.
  * `preview_exit_fn` quotes the full V3 -> V2 -> V4 ladder that admitted the funded entry.
    A V3-only quote is not the price the position would actually exit at, and labelling it
    `exact_route` was false.

The price is EXECUTABLE, not a mid, and that is the whole point: position 11984's tape showed
a +50.489% mid tick while its executable peak was +48.296%, so its +50% de-risk rail was never
actually reachable and only the executable number says so.

It fails closed. No context, no preview, no quantity => no mark. A fabricated mark is worse
than a missing one (RUSSELL, +$785.66).

This module produces a price map shaped for `write_marks`. The runner can also use the same map
for a funded exit sweep when `MEMECOIN_FASTLANE_DECIDES` is true.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_funded_policy import (
    CONTRACT_UNPARSEABLE, frozen_policy_from_position)

from sniper.util.log_sample import log_sampled

logger = logging.getLogger(__name__)

SOURCE = "fastlane_rpc"
QUALITY = "exact_route"
# The cheap batched mid lane is a DISTINCT source so the tape stays auditable: an exact-route mark
# and a DexScreener mid are not the same evidence, and a post-mortem must be able to tell them apart.
# `quality` is NOT that distinction — it is the trustworthy/quarantined axis, so the mid lane writes
# the normal "live" and stays subject to the same spike quarantine as every other price.
MID_SOURCE = "fastlane_mid"
MID_QUALITY = "live"
PAPER_SWAP_SOURCES = frozenset({"fastlane_sol_swap", "fastlane_sol_state"})


def _frozen_or_unparseable(pos):
    try:
        return frozen_policy_from_position(pos)
    except ValueError:
        return CONTRACT_UNPARSEABLE


class RecentSwapMarks:
    """Keep recent Solana swap marks for the slower paper exit sweep."""

    def __init__(self):
        self._lock = threading.Lock()
        self._marks = {}

    def publish(self, price_map, *, now_ts):
        accepted = {
            key: (dict(stats), float(now_ts))
            for key, stats in (price_map or {}).items()
            if isinstance(stats, dict) and stats.get("source") in PAPER_SWAP_SOURCES
        }
        if not accepted:
            return
        with self._lock:
            self._marks.update(accepted)

    def snapshot(self, *, now_ts, max_age_s):
        cutoff = float(now_ts) - float(max_age_s)
        with self._lock:
            stale = [key for key, (_stats, stamp) in self._marks.items() if stamp < cutoff]
            for key in stale:
                self._marks.pop(key, None)
            return {key: dict(stats) for key, (stats, _stamp) in self._marks.items()}


class MidObservationState:
    """Keep the first source time until a Dex mark value changes.

    FETCH HEALTH BESIDE IT (2026-09-26, docs/audits/2026-09-26-price-freshness.md). The returned
    stamp is when the VALUE last changed, which is right for the mark tape and the rails, but the
    screen could not tell a quiet market from a provider nobody had read for minutes. Each
    successful priced fetch also records `fetched_at`; a failed batch never reaches observe(), so
    it moves neither. The heartbeat carries snapshot(); the returned stamp is unchanged.
    """

    FETCH_REPORT_S = 600.0     # a key not read for this long is not reported as checked
    FETCH_FORGET_S = 3600.0

    def __init__(self):
        self._last = {}
        self._fetched = {}     # key -> fetched_at ISO; written on the fastlane thread
        self._lock = threading.Lock()

    def observe(self, key, stats, observed_at):
        fingerprint = (
            stats.get("price"),
            stats.get("liquidity"),
            stats.get("mcap"),
        )
        stamp = str(observed_at)
        with self._lock:
            self._fetched[key] = stamp
            prior = self._last.get(key)
            if prior is not None and prior[0] == fingerprint:
                return prior[1]
            self._last[key] = (fingerprint, stamp)
        return stamp

    def snapshot(self, *, now_iso):
        """{'chain:mint': {'changed_at', 'fetched_at'}} for keys fetched in the last ten minutes."""
        from datetime import datetime

        def age_s(iso):
            try:
                return (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                        - datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).total_seconds()
            except (TypeError, ValueError):
                return None

        out = {}
        with self._lock:
            for key, fetched in list(self._fetched.items()):
                age = age_s(fetched)
                if age is not None and age > self.FETCH_FORGET_S:
                    self._fetched.pop(key, None)     # _last is left alone: the mark stamp
                elif age is not None and age <= self.FETCH_REPORT_S:
                    out[f"{key[0]}:{key[1]}"] = {"changed_at": (self._last.get(key) or (None, None))[1],
                                                 "fetched_at": fetched}
        return out


class FastlaneState:
    """Thread-safe liveness state for the funded exit fastlane."""

    def __init__(self, *, clock=None):
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._last_success = None
        self._last_failure = None
        self._failure = None
        self._fallback_active = False
        self._thread_alive = False
        self._restart_count = 0

    def mark_success(self):
        with self._lock:
            self._last_success = float(self._clock())
            self._failure = None
            self._fallback_active = False

    def mark_failure(self, exc):
        with self._lock:
            self._last_failure = float(self._clock())
            self._failure = type(exc).__name__
            self._fallback_active = True

    def mark_fallback(self, active):
        with self._lock:
            self._fallback_active = bool(active)

    def mark_thread(self, alive, *, restarted=False):
        with self._lock:
            self._thread_alive = bool(alive)
            if restarted:
                self._restart_count += 1

    def snapshot(self, *, stale_after_s):
        now = float(self._clock())
        with self._lock:
            success_age = (
                None if self._last_success is None else max(0.0, now - self._last_success))
            failure_age = (
                None if self._last_failure is None else max(0.0, now - self._last_failure))
            if self._failure is not None:
                status = "failed"
            elif success_age is None:
                status = "starting"
            elif success_age > float(stale_after_s):
                status = "stale"
            else:
                status = "healthy"
            return {
                "status": status,
                "thread_alive": self._thread_alive,
                "restart_count": self._restart_count,
                "last_success_age_s": success_age,
                "last_failure_age_s": failure_age,
                "failure": self._failure,
                "fallback_active": self._fallback_active,
            }

    def fallback_required(self, *, stale_after_s):
        return self.snapshot(stale_after_s=stale_after_s)["status"] != "healthy"


def fastlane_wait_s(tick_s, *, started_at, now, open_book, max_book):
    """Seconds to wait after a pass: a DEADLINE on a small book, the old sleep-after otherwise.

    The lane slept a full tick AFTER variable serial work, so its period was work + tick: measured
    on `memecoin_route_series` (seam `exit_decision`, 14 d to 2026-09-24) the gap between
    consecutive quotes of one position is p50 1.61 s against a 1.0 s tick, and every stop inherits
    that ~0.6 s. Sleeping to `started_at + tick` removes it.

    ⚠️ IT IS SCOPED BY SOLANA BOOK SIZE ON PURPOSE, because the cost is quote rate on ONE Jupiter key
    (1 RPS / 60 per minute). Funded quotes already run p50 32 and p99 72 per minute, over 60 in
    2.9% of minutes; at the ~1.6x this implies that becomes 22.9%. And the exposure is not spread
    evenly: exit quotes taken with two positions open sit in a minute that lost a quote to rate
    limiting 25.8% of the time, with three open 75.2%, against 0.8% with one open. So the deadline
    is allowed only while the open book is at or below `max_book`; above it the old floor stands.
    `max_book` 0 disables it everywhere, which is the code default and today's behaviour. A larger
    key budget (10 RPS) is what raises it, not a better result from this function.

    `open_book` None means the pass did not report a count (a failed cycle), which is treated as
    "not small" rather than as zero: an unknown book must not buy the faster period.
    """
    tick = float(tick_s)
    if not max_book or open_book is None or open_book < 1 or open_book > int(max_book):
        return max(1.0, tick)
    # Clamped at 0: a pass slower than the tick simply starts the next one immediately, which is
    # the saturated case and is bounded by the pass duration, never by a shorter sleep.
    return max(0.0, float(started_at) + tick - float(now))


def run_fastlane_worker_cycle(resource, *, setup_fn, pass_fn, state, close_fn=None,
                              error_fn=None):
    """Run one worker cycle. Return no resource after a failed setup or pass."""
    try:
        if resource is None:
            resource = setup_fn()
        pass_fn(resource)
    except Exception as exc:
        state.mark_failure(exc)
        if error_fn is not None:
            error_fn(exc)
        if resource is not None and close_fn is not None:
            try:
                close_fn(resource)
            except Exception:
                pass
        return None, False
    state.mark_success()
    return resource, True


class WatchedLock:
    """The funded exit lock, remembering who holds it (2026-09-27).

    At 12:57:17-12:57:56Z the fast lane held the exit lock ~39 s on 66015 while the funded Solana
    pool failed every endpoint, and no stack existed to say which call it waited on. This is a
    plain threading.Lock plus a record written by the holder after it acquires and cleared before
    it releases: (thread ident, thread name, monotonic acquire time, episode seq). Nothing else
    reads or changes the inner lock, so the semantics are exactly a Lock's.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._held = None
        self._seq = 0

    def acquire(self, blocking=True, timeout=-1):
        ok = self._lock.acquire(blocking, timeout)
        if ok:
            self._seq += 1                      # only the holder writes, so no race
            t = threading.current_thread()
            self._held = (t.ident, t.name, time.monotonic(), self._seq)
        return ok

    def release(self):
        self._held = None
        self._lock.release()

    def locked(self):
        return self._lock.locked()

    def holder(self):
        return self._held

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def check_exit_lock(lock, *, hold_s, log, state, clock=time.monotonic, frames=None):
    """Log the holder's stack ONCE per episode when it has held `lock` longer than `hold_s`.

    One `log` call per stack line: runner.log stamps each `_p` line, and a bare multi-line dump
    is dropped by a timestamp-window read. Never acquires the lock, so it cannot deadlock it."""
    import sys
    import traceback
    held = lock.holder()
    if held is None or held[3] == state.get("dumped") or clock() - held[2] <= hold_s:
        return False
    state["dumped"] = held[3]
    ident, name, since, seq = held
    frame = (frames or sys._current_frames)().get(ident)
    log("[memecoin] exit lock held %.1fs by %s (episode %d); its stack:" % (clock() - since, name, seq))
    for block in (traceback.format_stack(frame) if frame is not None else ["(no frame)\n"]):
        for line in block.rstrip("\n").split("\n"):
            log("[memecoin] exit lock stack | " + line)
    return True


def watch_exit_lock(lock, *, hold_s, log, poll_s=1.0, stop=None):
    """The watcher thread's loop: one check per `poll_s`, forever (daemon) or until `stop`."""
    state = {}
    while stop is None or not stop.is_set():
        try:
            check_exit_lock(lock, hold_s=hold_s, log=log, state=state)
        except Exception as exc:  # noqa: BLE001 - a watcher fault must never touch the exits
            if not state.get("failed"):
                state["failed"] = True
                log("[memecoin] exit lock watch failed once: %r" % (exc,))
        time.sleep(poll_s)


def run_fastlane_exit_sweep(*, state, exit_lock, sweep_fn):
    """Run one fast sweep under the process exit lock."""
    with exit_lock:
        result = sweep_fn()
        state.mark_success()
        return result


def run_slow_exit_sweep(*, state, exit_lock, sweep_fn, fastlane_enabled,
                        fastlane_decides, stale_after_s, lock_timeout_s=5.0,
                        on_contended=None):
    """Run the slow sweep when it owns decisions or the fastlane needs a fallback.

    THE ACQUIRE IS BOUNDED, and that is the whole point (2026-09-09). This was `with exit_lock:` —
    an unbounded blocking acquire, called from the MAIN LOOP, between the previous `bracket_ts` and
    `finish_bracket_cycle`. `run_fastlane_exit_sweep` holds the SAME lock for the entire duration of
    a real exit: quote, sign, submit, confirm, and a slippage ladder that walks 3 -> 10 -> 25%. So
    whenever the fast lane took a real exit, the main loop parked on this line, `bracket_ts` froze,
    and at 300s `scripts/watch_sweep.py` declared `exit_sweep_stale` and the watchdog SIGTERMed the
    runner — while the fast lane was, by construction, in the middle of selling.

    58 of those kills between 2026-08-22 and 2026-09-09, seven on 09-04 alone. They read as a dead
    sweep and were the opposite: the exit path was the one thing definitely working. The runner log
    shows it exactly — `cycles=` stops, background threads keep printing, no exception is ever
    raised, because waiting on a lock is not an error. The sweep's own try/except has fired zero
    times in the whole log.

    Contention is therefore LIVENESS, not staleness, and it returns False (no fallback sweep ran)
    rather than blocking. `finish_bracket_cycle` is reached, `bracket_ts` advances, and the watchdog
    stops killing a healthy process mid-sell.

    The lock itself stays: two lanes deciding the same position race on `stop_hits` and can
    double-submit an exit. Removing it would trade a false alarm for a double sale.
    """
    if not exit_lock.acquire(timeout=float(lock_timeout_s)):
        state.mark_fallback(False)
        if on_contended is not None:
            on_contended()
        return False
    try:
        fallback = (
            not fastlane_enabled
            or not fastlane_decides
            or state.fallback_required(stale_after_s=stale_after_s)
        )
        state.mark_fallback(bool(fastlane_enabled and fastlane_decides and fallback))
        if not fallback:
            return False
        sweep_fn()
        return True
    finally:
        exit_lock.release()


def ensure_fastlane_thread(thread, *, target, state, thread_factory=threading.Thread):
    """Start the worker once. Restart it when the prior thread stops."""
    if thread is not None and thread.is_alive():
        state.mark_thread(True)
        return thread
    restarted = thread is not None
    new_thread = thread_factory(
        target=target, name="memecoin-funded-fastlane", daemon=True)
    new_thread.start()
    state.mark_thread(new_thread.is_alive(), restarted=restarted)
    return new_thread


def in_scope(network_id):
    """Chains that get the EXACT per-position preview quote (Jupiter/V3->V2->V4 balance + route).

    EVM only, decided EXPLICITLY — never inferred from "is there a context?". Solana also exposes
    preview_exit_fn, so an absence-of-context heuristic would silently price it here: two round
    trips per open position per tick. At a 1s tick and 7 positions that is 14 calls/s, which the
    public quote API throttles. Solana instead rides the CHEAP batched mid lane below.
    """
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    return str(network_id) in CHAIN_QUOTE


def mid_scope(network_id):
    """Chains priced by the cheap BATCHED mid lane — one DexScreener call for every open funded
    mint, regardless of how many there are.

    Solana was excluded from this worker entirely (2026-07-23) because its mark gaps were read as
    "a 30s RECORDING floor with no freshness to buy". That premise is stale: open REAL positions
    have their own 5s floor (MEMECOIN_MARK_WRITE_MIN_INTERVAL_OPEN_S), and the measured gaps on
    2026-07-27 were 14.6s median / 79s max — far above the floor, so the freshness was real and
    was being lost. The exit rails read `mid` (executable_exit_value is written EQUAL to mid
    today), so this one call feeds the ticker, mcap, liquidity AND the trail ratchet.
    """
    return str(network_id or "solana") == "solana"


def open_real_positions(conn, *, limit=None):
    """Open REAL positions, oldest first — the fast lane's entire work set.

    Admission limits do not bound the held book: desk slots are separate, and existing positions
    survive cap reductions and slot aging. Only an explicit caller limit truncates coverage.
    """
    try:
        # A PAUSED POSITION IS NOT WORK (2026-09-04, operator). `tracking_paused_at` means the
        # operator is watching it by hand and will say when to sell, so it must stop costing quotes
        # -- and this lane is where the cost actually is: it wrote 4,605 marks in two minutes for a
        # single paused dust play, against 483 from DexScreener. Filtering only the exit sweep left
        # that untouched and the reads did not stop.
        #
        # It stays OPEN and every exposure, bankroll and slot calculation keeps counting it; this
        # lane just stops quoting it. Clearing the stamp puts it straight back.
        base = ("SELECT * FROM memecoin_positions WHERE is_open=1 AND execution_mode='real' ")
        tail = " ORDER BY id" + (" LIMIT ?" if limit is not None else "")
        params = (int(limit),) if limit is not None else ()
        # TOLERATE A TABLE WITHOUT THE COLUMN. `ensure_memecoin_tables` adds it, but a caller that
        # builds the table by hand has not got it, and an exception here is swallowed by the
        # handler below and returns an EMPTY work set -- the fast lane would silently stop marking
        # every position rather than one paused one. Degrade to the unfiltered query instead.
        has_pause = any(r[1] == "tracking_paused_at"
                        for r in conn.execute("PRAGMA table_info(memecoin_positions)"))
        clause = "AND COALESCE(tracking_paused_at,'')='' " if has_pause else ""
        return [dict(r) for r in conn.execute(base + clause + tail, params)]
    except Exception:
        logger.exception("fast lane: open-position lookup failed")
        return []


def has_priority_tp_confirmation(conn):
    """True while a 4663 take-profit confirmation is pending and needs the next exact quote.

    The stamp exists only while a confirmation window is actually running, so its presence IS the
    condition. It used to also require a follow-30-50-v3/v4 policy id, which was the gate for the
    per-policy TP-window override removed on 2026-09-03; with the window read from one env value
    that check could only ever agree with the stamp.
    """
    return any(str(pos.get("network_id") or "") == "4663" and pos.get("tp_breach_started_at")
               for pos in open_real_positions(conn))


def open_swap_mark_positions(conn, *, real_limit=None, paper_mint_limit=16):
    """Select open funded positions and recent paper mints with the same funded origin.

    POST-CLOSE MARKS ARE A SOLANA GUARANTEE ONLY (Codex review 2026-09-23, N13). This list feeds
    the Solana swap-mark lane's vault book, so a Solana position closed within
    MEMECOIN_POST_CLOSE_MARK_S keeps printing marks. An EVM position closed in that window is
    listed here too but gets NO new price observation from it: EVM marks come from the exact
    balance-based exit preview in `refresh_open_positions`, which selects `open_real_positions`
    only, and a preview after the balance was sold would price nothing real. The post-close path of
    a 4663/8453 coin lives on the on-chain swap tape (data/memecoin_onchain_volume.db pool_ticks,
    about one hour retained) and is not written to memecoin_position_marks; 56 and 5042 have no
    post-close instrument at all. The closed rows are bounded to the newest
    2 * MEMECOIN_MAX_CONCURRENT (`recently_closed_real_positions`), so a burst of closes can drop
    the oldest of them from the window.
    """
    real = open_real_positions(conn, limit=real_limit)
    seen = {m.coin_identity(pos.get("network_id"), pos.get("mint")) for pos in real}
    # POST-CLOSE WINDOW (2026-09-23, operator): a funded position closed within
    # MEMECOIN_POST_CLOSE_MARK_S keeps its pool on the tape so the minutes after a stop are
    # recorded. Marks only; this list feeds subscriptions and the mark writer, never a rail.
    from sniper.settings import memecoin as cfg
    for pos in m.recently_closed_real_positions(
            conn, within_s=getattr(cfg, "MEMECOIN_POST_CLOSE_MARK_S", 0.0)):
        key = m.coin_identity(pos.get("network_id"), pos.get("mint"))
        if key in seen:
            continue
        seen.add(key)
        real.append(pos)
    if paper_mint_limit < 1:
        return real
    try:
        paper_sources = [dict(row) for row in conn.execute(
            """
            WITH ranked_paper AS (
                SELECT origin_event_id, mint, opened_at AS paper_opened_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY mint ORDER BY opened_at DESC, id DESC
                       ) AS mint_rank
                FROM memecoin_positions
                WHERE is_open=1 AND execution_mode='paper'
                  AND COALESCE(origin_event_id,'')<>''
                  AND COALESCE(network_id,'solana') IN ('', 'solana', '1399811149')
            ), paper_targets AS (
                SELECT origin_event_id, mint, paper_opened_at
                FROM ranked_paper
                WHERE mint_rank=1
                ORDER BY paper_opened_at DESC
                LIMIT ?
            )
            SELECT r.*
            FROM paper_targets t
            JOIN memecoin_positions r ON r.id=(
                SELECT r2.id
                FROM memecoin_positions r2
                WHERE r2.execution_mode='real'
                  AND r2.origin_event_id=t.origin_event_id
                  AND r2.mint=t.mint
                  AND COALESCE(r2.network_id,'solana') IN ('', 'solana', '1399811149')
                ORDER BY r2.id DESC
                LIMIT 1
            )
            ORDER BY t.paper_opened_at DESC
            """,
            (int(paper_mint_limit),),
        )]
    except Exception:
        logger.exception("swap mark lane: paper target lookup failed")
        return real
    for pos in paper_sources:
        key = m.coin_identity(pos.get("network_id"), pos.get("mint"))
        if key in seen:
            continue
        seen.add(key)
        real.append(pos)
    return real


def _record_preview(preview_by_position, position_id, preview, price, *, cache_unpriced):
    """Hand this tick's preview to the runner for reuse — and say whether an UNPRICED one counts.

    The runner reads PRESENCE, not value (`memecoin_real_runner` 3199 / 3209 / 3241), so caching a
    `None` means "attempted, nothing came back, do not spend a second preview on it" while omitting
    the key entirely means "not attempted this tick". The two fast-lane callers legitimately want
    different answers and always have:

      cache_unpriced=True   the batch in `refresh_open_positions`. A miss is a fact about this tick
                            and the sweep must not re-preview it. This is what feeds the WO-4
                            exact-quote-miss counter, which exists so that a provider outage cannot
                            manufacture a false unsellable CUT.
      cache_unpriced=False  `executable_unit_price`. Records only a preview that produced a price,
                            so a later caller gets a fresh attempt rather than a cached nothing.

    Unifying the two would move a funded decision input, so they stay different ON PURPOSE. This
    exists so the divergence is stated in one place instead of being an accident of two code paths
    that happen to write the same dict differently. Do not "simplify" it without deciding which
    counter you are changing.
    """
    if preview_by_position is None:
        return
    if price is None and not cache_unpriced:
        return
    preview_by_position[position_id] = preview


def executable_unit_price(conn, pos, *, preview_fn, preview_by_position=None):
    """USD price of ONE token, as the whole remaining bag would actually sell.

    `preview_fn` is the position's funded `preview_exit_fn`: it reads the EXACT raw on-chain
    balance and quotes the full V3 -> V2 -> V4 ladder that admitted the entry. Dividing its
    `expected_usdc` by the ledger's open quantity is exactly `_executable_unit_price` in
    memecoin_real_runner — one definition of "executable", shared, so the tape can never drift
    from what the rails judge.

    Any missing input returns None; callers treat None as "no mark this tick".
    """
    try:
        qty = float(m.open_qty(conn, pos["id"]) or 0.0)
    except Exception:
        logger.exception("fast lane: open_qty failed pos=%s", pos.get("id"))
        return None
    if qty <= 0:
        return None                                  # nothing left to price

    try:
        preview = preview_fn(pos)
    except Exception:
        logger.exception("fast lane: exit preview failed pos=%s mint=%s", pos.get("id"),
                         pos.get("mint"))
        return None
    if preview is None:
        return None                                  # unquotable this tick -> missing, not zero

    price = _preview_unit_price(preview, qty)
    _record_preview(preview_by_position, pos["id"], preview, price, cache_unpriced=False)
    return price


def _preview_unit_price(preview, qty):
    """Return one-token value from an exact preview and an already-read ledger quantity."""
    expected = getattr(preview, "expected_usdc", None)
    if expected is None and isinstance(preview, dict):
        expected = preview.get("expected_usdc")
    try:
        expected = float(expected)
    except (TypeError, ValueError):
        return None
    if not (expected > 0):
        return None
    price = expected / qty
    return price if price > 0 else None


def _batched_mid_snapshot(
    positions,
    *,
    quote_mints=(),
    batch_fn=None,
    observation_state=None,
    now_iso_fn=None,
):
    """Fetch one Dex snapshot for the funded positions and quote references.

    Cost is one request per tick no matter how many positions are open (the cap is 7), which is why
    this can run at a 1s tick where the exact per-position preview cannot. Carries liquidity and
    mcap too, so the ticker's price/mcap/liq all advance together off a single fetch.

    Never raises: a failed batch means no mid marks this tick, never a substituted weaker price.
    An EVM position that also has an exact preview is overwritten by it below — exact wins.
    """
    position_keys = sorted(
        {
            (str(p.get("network_id") or "solana"), str(p.get("mint") or ""))
            for p in positions
            if mid_scope(p.get("network_id")) and p.get("mint")
        }
    )
    # X2 OFF (DexScreener out, phase A), PER CHAIN: a switched-off chain's positions get no provider
    # mid, and when Solana is off the swap marks' quote-asset USD comes from the live marks' own source
    # (SOL from the slot lane's prefetch, other quote assets from Jupiter on the capture key), read
    # from cache and failing closed per asset. Swap marks and exact previews carry those chains.
    from sniper.settings.memecoin import dexscreener_off
    position_keys = [k for k in position_keys if not dexscreener_off("X2", k[0])]
    own_usd = None
    if dexscreener_off("X2", "solana"):
        from sniper.services.memecoin_live_marks import fresh_quote_usd
        # The pass's own clock (Gamma LOW): without it the adapter reads the clock itself.
        own_usd = fresh_quote_usd(quote_mints, now_iso=now_iso_fn() if now_iso_fn else None)
        quote_mints = ()
    quote_keys = {("solana", str(mint)) for mint in quote_mints if mint}
    request_keys = sorted(set(position_keys) | quote_keys)
    if not request_keys:
        return {}, (own_usd or {})
    if batch_fn is None:
        from functools import partial
        from sniper.execution import solana_quote
        batch_fn = partial(solana_quote.dexscreener_stats_batch_keyed, funded=True, consumer="X2")
    try:
        stats = batch_fn(request_keys) or {}
    except Exception:
        logger.exception(
            "fast lane: batched mid fetch failed (%d mint(s))", len(request_keys)
        )
        return {}, {}
    now_iso_fn = now_iso_fn or (lambda: None)
    marks = {}
    for key in position_keys:
        raw = stats.get(key) or {}
        try:
            price = float(raw.get("price"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        mark = {
            "price": price,
            "liquidity": raw.get("liquidity"),
            "mcap": raw.get("market_cap"),
            "fdv_snapshot": {k: raw.get(k) for k in ("price", "fdv", "market_cap")},
            "source": MID_SOURCE,
            "quality": MID_QUALITY,
        }
        if observation_state is not None:
            fetched_at = now_iso_fn()
            mark["observed_at"] = observation_state.observe(key, mark, fetched_at)
            # When the provider was CHECKED, beside when the value last CHANGED (observed_at):
            # the exit audit labels a stale-looking mid with both (2026-09-27). Display only.
            mark["fetched_at"] = fetched_at
        marks[key] = mark
    quote_usd = {}
    for mint in quote_mints:
        try:
            price = float(
                (stats.get(("solana", str(mint))) or {}).get("price")
            )
        except (TypeError, ValueError):
            continue
        if price > 0:
            quote_usd[str(mint)] = price
    return marks, (own_usd if own_usd is not None else quote_usd)


def _batched_mid_marks(positions, *, batch_fn=None):
    """Return compatible Dex marks for existing callers."""
    marks, _quote_usd = _batched_mid_snapshot(positions, batch_fn=batch_fn)
    return marks


def rail_levels(pos, *, override, exact_price):
    """(down, up) executable-price levels of this position's price rails, from the sweep's own
    helpers so the wake and the decision read one policy. Down: the stop (the catastrophe floor
    when the stop is off) and an ARMED trail; up: the take-profit and a de-risk not yet taken.
    Market-cap targets are not here; the wake's big-move test stands in for them."""
    from sniper.services.memecoin_real_runner import (
        _effective_catastrophe_pct, _effective_derisk_pct, effective_real_thresholds,
        effective_trail)
    entry = float(pos.get("entry_price") or 0.0)
    if entry <= 0:
        return [], []
    tp_pct, _tp_mcap, stop_mag, _sl_mcap = effective_real_thresholds(pos, override)
    floor = stop_mag if stop_mag > 0 else _effective_catastrophe_pct(pos, override)
    down = [entry * (1.0 - floor / 100.0)] if floor > 0 else []
    trail_pct, armed = effective_trail(pos, override)
    if trail_pct > 0 and armed:
        peak = max(float(pos.get("executable_peak_price") or entry), float(exact_price))
        down.append(peak * (1.0 - trail_pct / 100.0))
    up = [entry * (1.0 + tp_pct / 100.0)] if tp_pct > 0 else []
    derisk_pct = _effective_derisk_pct(pos, override)
    if derisk_pct > 0 and not pos.get("derisked_at"):
        up.append(entry * (1.0 + derisk_pct / 100.0))
    return down, up


def rearm_rail_wake(conn, trigger):
    """Hand the wake every open Solana position's rails, after a sweep.

    The anchor price is the position's persisted last exact quote (`last_executable_price`, written
    by the sweep only for a real positive quote), and `last_executable_at` says whether it is new:
    the trigger re-reads its mark anchor only when that stamp moved. Levels are ratios to that
    price. A level already on the far side of it is left out: the sweep has seen it, and keeping it
    would wake on every mark while an exit is pending. Several positions on one mint merge to the
    nearest level each way.
    """
    rails, held = {}, set()
    for pos in open_real_positions(conn):
        if str(pos.get("network_id") or "") != "solana":
            continue
        mint = str(pos.get("mint") or "")
        held.add(mint)
        price = pos.get("last_executable_price")
        if (not price or float(price) <= 0 or not pos.get("last_executable_at")
                or _frozen_or_unparseable(pos) is CONTRACT_UNPARSEABLE):
            continue
        price = float(price)
        down, up = rail_levels(pos, override=m.effective_exit_override(conn, pos["id"]),
                               exact_price=price)
        down = max((lv / price for lv in down if lv < price), default=None)
        up = min((lv / price for lv in up if lv > price), default=None)
        stamp = (pos["id"], str(pos["last_executable_at"]))
        if mint in rails:
            d0, u0, ids, stamps = rails[mint]
            down = down if d0 is None else d0 if down is None else max(down, d0)
            up = up if u0 is None else u0 if up is None else min(up, u0)
            rails[mint] = (down, up, ids | {pos["id"]}, tuple(sorted(stamps + (stamp,))))
        else:
            rails[mint] = (down, up, {pos["id"]}, (stamp,))
    trigger.rearm(rails, keep=held)


def refresh_open_positions(
    conn,
    contexts,
    *,
    limit=None,
    context_fn=None,
    mid_batch_fn=None,
    preview_by_position=None,
    demand_fn=None,
    swap_mark_cache=None,
    mid_observation_state=None,
    now_ts_fn=None,
    now_iso_fn=None,
    swap_max_age_s=5.0,
    stats_out=None,
):
    """One pass: price every open real position that has a funded exit-preview seam.

    Returns a `write_marks`-shaped map `{(network_id, mint): {...}}`. One bad position never
    denies the tape to the others. Never raises.

    Scope is checked explicitly (`in_scope`) BEFORE any context lookup — a chain being
    contextful is not the same as it being worth quoting. A position with no context (real leg
    not armed for its chain) is then skipped rather than priced some other way; this lane has
    exactly one definition of executable and does not substitute a weaker one.
    """
    if context_fn is None:
        from sniper.services.memecoin_real_runner import context_for_position as context_fn
    now_ts_fn = now_ts_fn or time.monotonic
    out = {}
    positions = list(open_real_positions(conn, limit=limit))
    # The caller's pacing needs the open-book size and this read already has it; asking the ledger
    # a second time for a number we are holding would put a query on the lane's own period.
    if stats_out is not None:
        stats_out["open_positions"] = len(positions)
        # The deadline cap protects the Solana quote provider. EVM reads use their own chain
        # seams and must not make a single Solana bag pay a full extra sleep after every pass.
        # Unknown networks count conservatively with Solana; an empty book keeps the idle floor.
        stats_out["deadline_positions"] = (max(1, sum(
            not in_scope(str(pos.get("network_id") or "")) for pos in positions))
            if positions else 0)
    quote_mints = (
        swap_mark_cache.quote_mints() if swap_mark_cache is not None else set()
    )
    mid_marks, quote_usd = _batched_mid_snapshot(
        positions,
        quote_mints=quote_mints,
        batch_fn=mid_batch_fn,
        observation_state=mid_observation_state,
        now_iso_fn=now_iso_fn,
    )
    out.update(mid_marks)
    if stats_out is not None:
        # A rail wake's focus pass prices its sweep from the swap-mark cache with this pass's
        # quote-asset USD, instead of paying for its own DexScreener batch.
        stats_out["quote_usd"] = dict(quote_usd or {})
    if swap_mark_cache is not None:
        out.update(
            swap_mark_cache.fresh_price_map(
                quote_usd,
                now_ts=now_ts_fn(),
                max_age_s=swap_max_age_s,
            )
        )
    preview_jobs = []
    if demand_fn is not None:
        from sniper.services.memecoin_real_runner import _effective_derisk_pct, effective_trail
    for pos in positions:
        network_id = str(pos.get("network_id") or "")
        if not in_scope(network_id):
            continue                                 # explicit scope gate, checked FIRST
        try:
            ctx = context_fn(contexts, pos) or {}
        except Exception:
            logger.exception("fast lane: context lookup failed pos=%s", pos.get("id"))
            continue
        preview_fn = ctx.get("preview_exit_fn")
        if preview_fn is None:
            continue                                 # no funded seam -> not this lane's job
        try:
            qty = float(m.open_qty(conn, pos["id"]) or 0.0)
        except Exception:
            logger.exception("fast lane: open_qty failed pos=%s", pos.get("id"))
            continue
        if qty <= 0:
            continue
        try:
            override = m.effective_exit_override(conn, pos["id"])
            pos["_exit_custom_cap"] = any((override or {}).get(key) is not None
                                         for key in ("tp_target_mcap", "sl_target_mcap"))
            pos["_mcap_exit_target"] = m.active_exit_target(conn, pos["id"])
        except Exception:
            logger.exception("fast lane: exit target metadata unavailable pos=%s", pos["id"])
            override = None
            # Unknown policy cannot justify skipping its quote; the demand budget still applies.
            pos["_exit_custom_cap"] = True
        if demand_fn is not None:
            try:
                trail_pct, trail_armed = effective_trail(pos, override)
                requested = demand_fn(
                    pos,
                    display_mid=(pos.get("last_executable_price")
                                 or pos.get("last_quote_price")),
                    trail_pct=trail_pct,
                    derisk_pct=_effective_derisk_pct(pos, override),
                    trail_armed=trail_armed,
                )
            except Exception:
                logger.exception("fast lane: demand gate failed pos=%s — quoting", pos.get("id"))
                requested = True
            if not requested:
                continue
        preview_jobs.append((pos, network_id, qty, preview_fn))

    if not preview_jobs:
        for key, mark in mid_marks.items():
            out[key]["fdv_snapshot"] = mark["fdv_snapshot"]
        return out

    def _preview(pos, preview_fn):
        try:
            return preview_fn(pos)
        except Exception:
            logger.exception("fast lane: exit preview failed pos=%s mint=%s", pos.get("id"),
                             pos.get("mint"))
            return None

    with ThreadPoolExecutor(max_workers=min(3, len(preview_jobs))) as pool:
        futures = {
            pool.submit(_preview, pos, preview_fn): (pos, network_id, qty)
            for pos, network_id, qty, preview_fn in preview_jobs
        }
        for future in as_completed(futures):
            pos, network_id, qty = futures[future]
            try:
                preview = future.result()
            except Exception:
                logger.exception("fast lane: refresh failed pos=%s", pos.get("id"))
                continue
            price = _preview_unit_price(preview, qty) if preview is not None else None
            _record_preview(preview_by_position, pos["id"], preview, price, cache_unpriced=True)
            if price is None:
                continue
            out[(network_id, str(pos.get("mint") or ""))] = {
                "price": price,
                "liquidity": None,
                "mcap": None,
                "source": SOURCE,
                "quality": QUALITY,
            }
    for key, mark in mid_marks.items():
        out[key]["fdv_snapshot"] = mark["fdv_snapshot"]
    return out


def build_demand_fn(conn, *, near_pct=None, heartbeat_s=None, budget=None, rail_budget=None,
                    clock=None, always=None):
    """A `demand_fn` for real_exit_sweep: quote exactly only when the cheap mid says it matters.

    Closes over a per-position "last exact quote" clock so the heartbeat is real wall time, not a
    tick count — the same mistake that made stop_debounce silently 30x weaker when the cadence
    changed. Returns a callable; the runner passes it straight through to the WO-4 pre-pass.
    """
    import time as _t
    from sniper.services.memecoin_quote_demand import (
        RAIL_REASONS, QuoteBudget, exact_quote_demand)
    from sniper.settings import memecoin as cfg

    now = clock or _t.monotonic
    if near_pct is None:
        near_pct = float(getattr(cfg, "MEMECOIN_EXACT_QUOTE_NEAR_PCT", 10.0))
    if heartbeat_s is None:
        heartbeat_s = float(getattr(cfg, "MEMECOIN_EXACT_QUOTE_HEARTBEAT_S", 60.0))
    if always is None:
        always = bool(getattr(cfg, "MEMECOIN_EXACT_QUOTE_ALWAYS", False))
    total_per_minute = float(getattr(cfg, "MEMECOIN_EXACT_QUOTE_PER_MINUTE", 30.0))
    rail_per_minute = min(total_per_minute, max(1.0, total_per_minute * 0.75))
    if budget is None:
        budget = QuoteBudget(
            per_minute=max(0.0, total_per_minute - rail_per_minute), clock=now)
    if rail_budget is None:
        # The two buckets partition the configured provider ceiling. Rails receive 75 percent.
        rail_budget = QuoteBudget(
            per_minute=rail_per_minute,
            clock=now)
    last_exact = {}
    # THE PROVIDER GROUP EACH POSITION QUOTES THROUGH (2026-09-28), so a 429 held in one group does
    # not refuse another group's quote. Learned from the sweep's own previews (`note_group`); before
    # a position's first quote, its chain's exact-quote seam: the Solana seam names
    # "solana:exact_quote", an EVM seam "<chain>:direct". An unknown network is None, which any
    # hold refuses (today's behaviour). Touched only on the fast-lane worker thread.
    groups = {}

    def _group(pos):
        learned = groups.get(pos.get("id"))
        if learned:
            return learned
        network = str(pos.get("network_id") or "")
        return "solana:exact_quote" if network == "solana" else (
            "%s:direct" % network if network else None)

    def note_group(pid, group):
        if pid is not None and group:
            groups[pid] = str(group)

    def _pnl_pct(pos, price):
        entry = pos.get("entry_price") or 0.0
        try:
            entry, price = float(entry), float(price)
        except (TypeError, ValueError):
            return None
        return ((price / entry) - 1.0) * 100.0 if entry > 0 and price > 0 else None

    def demand_fn(pos, *, display_mid, trail_pct, derisk_pct, trail_armed=False):
        pid = pos.get("id")
        stop_pct = tp_pct = None
        frozen = _frozen_or_unparseable(pos)
        if frozen is CONTRACT_UNPARSEABLE:
            return False
        if frozen is not None:
            stop_pct = getattr(frozen, "stop_pct", None)
            tp_pct = getattr(frozen, "tp_pct", None)
        peak = pos.get("executable_peak_price") or pos.get("peak_price")
        age = None if pid not in last_exact else max(0.0, now() - last_exact[pid])
        verdict = exact_quote_demand(
            mid_pnl_pct=_pnl_pct(pos, display_mid),
            peak_pnl_pct=_pnl_pct(pos, peak),
            stop_pct=stop_pct, derisk_pct=derisk_pct, trail_pct=trail_pct,
            # The SWEEP's own armed flag, never a guess. A peak of +7.33% has NOT armed a trail
            # that arms at +15%, but `bool(peak)` says it has — which made an unarmed position
            # demand an exact quote every single tick (caught rehearsing against the live book).
            trail_armed=bool(trail_armed), tp_pct=tp_pct, last_exact_age_s=age,
            near_pct=near_pct, heartbeat_s=heartbeat_s, always=always,
            custom_cap=bool(pos.get("_exit_custom_cap") or pos.get("_mcap_exit_target")))
        if not verdict.needed:
            return False
        # The budget is a BACKSTOP, checked last: it must never be the reason a near-rail position
        # goes unquoted while a mid-band heartbeat burns the tokens. Ordering matters — and so does
        # WHICH bucket pays: a rail draws the rail ceiling, the periodic refresh draws the small
        # shared one. Sharing a single bucket is what let heartbeats starve a live take-profit.
        # `age is None` means this position has NEVER had a successful exact quote. That demand
        # returns reason="heartbeat", but it is not a periodic refresh — it is the bootstrap, and
        # until it lands the position has no exact price to evaluate ANY rail against. Starving it
        # is self-sealing: the quote never succeeds, so age stays None, so it is classified as a
        # refresh forever. That is exactly how pos 37273 sat above its take-profit for 5283
        # consecutive refused ticks. Only a position with a live exact price draws the small bucket.
        rail = verdict.reason in RAIL_REASONS or age is None
        bucket = rail_budget if rail else budget
        group = _group(pos)
        if not bucket.take(group=group):
            log_sampled(logger.warning, ("exact_quote_budget", pid),
                        "exact-quote budget exhausted (%s) pos=%s group=%s reason=%s rail=%s — "
                        "skipping this tick", bucket.snapshot(), pid, group, verdict.reason, rail)
            return False
        last_exact[pid] = now()
        return True

    demand_fn.budget = budget          # exposed for the heartbeat / tests
    demand_fn.rail_budget = rail_budget
    demand_fn.note_group = note_group
    return demand_fn
