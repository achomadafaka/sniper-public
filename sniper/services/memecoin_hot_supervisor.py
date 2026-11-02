"""WO-1 Phase B integration — tie the existing Solana WS hot tape to the existing hot decision engine.

The hot tape (`memecoin_solana_ws.run_vault_stream` / `VaultTape`) and the event-driven decision
engine (`exit_engine_hot.HotEngineSet`) already exist and are tested. This is the SUPERVISOR that
was missing: it bridges a WS vault-delta row into a decision, keeps the pull lane as verifier and
fail-closed fallback, pre-warms exits so a CUT is sign+broadcast only, and measures the actual
notification->decision latency. It reimplements NEITHER lane (one-code-path refusal).

Fail-closed guarantees:
- A WS gap can never leave a position undecided beyond `max_decision_gap_s` (default 60s): the pull
  lane's 30s pass produces a decision that resets the gap well inside the bound.
- The pull lane is the VERIFIER: any slot the pull tape recorded that the WS never delivered is a
  dropped WS event, surfaced as a divergence.
- A stale pre-warmed preview is never used — the CUT path re-quotes exact whole-bag if it aged out.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

logger = logging.getLogger("memecoin")

# EARLY EPOCH REBUILD (2026-09-02). The hot lane's position book is fixed for a 30-second epoch, so
# a position that opens just after a rebuild is INVISIBLE to it until the next one. Measured over 3
# days: open to first price mark ran p50 11.4s and p90 28.0s on 4663, with only 12% inside 5
# seconds -- the signature of a uniform wait for a 30s cycle. For that whole window no rail can
# fire on the position, because there is no price.
#
# Shortening an epoch is already routine: the stream loop ends one on any socket gap and the lane
# rebuilds from the pull-complete tape. This reuses that exact path rather than mutating a live
# book, so a new position costs one early rebuild and nothing else changes.
_REBUILD_REQUEST = threading.Event()


def request_rebuild():
    """Ask the hot lane to end its epoch early. Safe from any thread.

    Cheap and non-blocking: it is called from the funded entry path after a position opens and must
    never delay it. A request arriving with no lane running is consumed by the next one."""
    _REBUILD_REQUEST.set()


def rebuild_requested():
    """True (and CLEARS) when an early rebuild has been asked for."""
    if _REBUILD_REQUEST.is_set():
        _REBUILD_REQUEST.clear()
        return True
    return False

# WO-2 — transaction-grounded sale authority. A sale proposed on a vault-DELTA row (tx_sig=None) is
# not authorized until the triggering transaction is decoded and it re-proposes the sale.
_SALE_ACTIONS = ("CUT", "DERISK", "TAKE_PROFIT")
# reasons whose evidence is NOT a vault delta -> no decode corroboration (route / position age).
_NO_DECODE_REASONS = frozenset({"unsellable", "edge_duration_elapsed"})


@dataclass(frozen=True)
class SolanaDecodeContext:
    """Per-pool decode primitives (WO-2): resolve the triggering transaction(s) on the coin vault so
    a vault-delta sale can be confirmed against tx-grounded rows before it authorizes execution."""
    coin_mint: str
    coin_vault: str
    get_sigs_fn: object     # (vault, params) -> [sig rows newest-first] | None
    get_tx_fn: object       # (signature) -> tx dict | None
    # THE BOUND QUOTE MINT, carried so the fast lane can decode a pool quoted in anything
    # (2026-09-23). Defaulted to "" so a caller that has not resolved one keeps the old behaviour
    # rather than silently claiming a quote asset it never looked up.
    quote_mint: str = ""
    # The pool's BOUND quote-vault ADDRESS. Defaulted "" for the same reason as the mint: a caller
    # without a resolved binding must fall back to the owner match, not claim an address.
    quote_vault: str = ""


def _is_sale(decision):
    return getattr(decision, "action", None) in _SALE_ACTIONS


def _sale_needs_decode(decision):
    """A sale evidenced by a vault delta must be decode-corroborated; unsellable (route) and
    time-derisk (position age) are not vault-delta evidence and pass through as before."""
    if not _is_sale(decision):
        return False
    return not (set(getattr(decision, "reason_codes", ()) or ()) & _NO_DECODE_REASONS)


def _structure_pending(decision):
    return "structure_hysteresis_pending" in set(
        getattr(decision, "reason_codes", ()) or ())


def _decision_needs_decode(decision):
    return _sale_needs_decode(decision) or _structure_pending(decision)


def row_is_transaction_grounded(row):
    """WO-9: is this triggering row already transaction-grounded, so a sale may authorize WITHOUT the
    Solana vault-delta corroboration round-trip?

    True ONLY with EXPLICIT provenance — `transaction_decoded` set by the feeder, a tx hash, complete
    block/log identity, a block hash, and a true block time. Never inferred from the chain: a row that arrived from the
    mempool or a header-less receive (transaction_decoded absent / block_time_true False) is NOT
    grounded and can wake+prewarm but never authorize a sale. A Solana WS vault-delta row (tx_sig
    None) is not grounded either -> it keeps taking the WO-2 corroboration path. This is what stops an
    EVM sale being (wrongly) routed through the Solana corroboration path and stops a header-less EVM
    row from authorizing on receive time alone."""
    return bool(row.get("transaction_decoded") is True and row.get("tx_sig")
                and row.get("block_hash") and row.get("block_time_true") is True
                and row.get("block_no") is not None and row.get("log_index") is not None)


def build_decode_ctx_fn(entries, *, get_sigs_fn, get_tx_fn):
    """WO-2 wiring: resolve a hot-book key -> SolanaDecodeContext from the per-pool vault identity
    (`coin_mint`, `coin_vault`) that build_hot_book stored, backed by the shared read-RPC signature
    and transaction fns. Returns None (so every vault-delta sale fails closed to the pull lane) when
    the read RPC is unavailable — `get_signatures_fn()`/`get_transaction_fn()` return None then."""
    if get_sigs_fn is None or get_tx_fn is None:
        return None

    def resolve(key):
        e = entries.get(key)
        if not e or not e.get("coin_vault") or not e.get("coin_mint"):
            return None
        return SolanaDecodeContext(coin_mint=e["coin_mint"], coin_vault=e["coin_vault"],
                                   get_sigs_fn=get_sigs_fn, get_tx_fn=get_tx_fn,
                                   quote_mint=str(e.get("quote_mint") or ""),
                                   quote_vault=str(e.get("quote_vault") or ""))

    return resolve


class DecisionGapTracker:
    """Time since the last decision (from EITHER lane). Fail-closed: never decided, or older than the
    bound, means the pull lane must cover it — the hot lane is cold."""

    def __init__(self, max_decision_gap_s=60.0):
        self.max_gap_s = float(max_decision_gap_s)
        self.last_decision_ts = None

    def record_decision(self, now_ts):
        self.last_decision_ts = float(now_ts)

    def invalidate(self):
        """Force the authoritative pull lane on its next housekeeping check."""
        self.last_decision_ts = None

    def gap_s(self, now_ts):
        return None if self.last_decision_ts is None else (float(now_ts) - self.last_decision_ts)

    def needs_pull_fallback(self, now_ts):
        g = self.gap_s(now_ts)
        return g is None or g > self.max_gap_s


class PullVerifier:
    """The always-on 30s pull lane cross-checks the hot lane. A slot the pull tape saw for a held pool
    that the WS never delivered is a dropped WS event — a divergence, surfaced not hidden."""

    def __init__(self):
        self.divergences = 0
        self.dropped_slots = []

    def check(self, ws_seen_slots, pull_seen_slots):
        dropped = sorted(set(pull_seen_slots) - set(ws_seen_slots))
        self.divergences += len(dropped)
        self.dropped_slots.extend(dropped)
        return dropped


class PrewarmedExits:
    """Exact whole-bag exit previews per position, refreshed every N s, so a CUT is sign+broadcast
    only. A preview older than `max_age_s` is treated as absent (fail-closed: re-quote at CUT time)."""

    def __init__(self, *, max_age_s=5.0):
        self.max_age_s = float(max_age_s)
        self._cache = {}                                  # key -> (preview, refreshed_ts)

    def refresh(self, key, preview, now_ts):
        self._cache[key] = (preview, float(now_ts))

    def get_fresh(self, key, now_ts):
        entry = self._cache.get(key)
        if entry is None:
            return None
        preview, ts = entry
        return preview if (float(now_ts) - ts) <= self.max_age_s else None


class PrewarmWorkerPool:
    """Process-lifetime, queue-free worker capacity for speculative previews.

    The hot lane rebuilds its asyncio loop every 30 seconds. A per-epoch executor lets timed-out
    threads from old epochs overlap with new executors, defeating the worker bound. This pool is
    shared across epochs and admits work only when a real worker slot and the key are both free.
    """

    def __init__(self, *, max_workers=2):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.max_workers = max(1, int(max_workers))
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="memecoin-prewarm")
        self._lock = threading.Lock()
        self._active_keys = set()
        self._closed = False

    def try_submit(self, key, fn, *args):
        """Submit without queueing, or return None while capacity/the same key is occupied."""
        with self._lock:
            if self._closed or len(self._active_keys) >= self.max_workers or key in self._active_keys:
                return None
            self._active_keys.add(key)

        def run():
            try:
                return fn(*args)
            finally:
                with self._lock:
                    self._active_keys.discard(key)

        try:
            return self._executor.submit(run)
        except Exception:
            with self._lock:
                self._active_keys.discard(key)
            raise

    def shutdown(self):
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


class PrewarmScheduler:
    """WO-12A — run speculative exit previews OFF the event loop so a slow quote can never block WS
    frame processing or the pull-fallback trigger (the proven `preview_fn`-in-loop defect).

    Contract:
    - Bounded concurrency + a hard per-call deadline: previews run in a DEDICATED small executor
      (spare capacity), never the decode/sale path's threads, so sale-shaped work is never delayed.
    - Per-position/route coalescing: at most ONE in-flight + ONE newest-pending request per key.
    - No accumulating backlog: a slot is submitted only after acquiring the concurrency semaphore, and
      the semaphore is released only when the worker thread TRULY finishes (not on deadline) — a hung
      preview holds its slot, so further requests coalesce into the single pending rather than piling
      unbounded work onto the executor.
    - Results are applied via `apply_fn` ON the owning loop (this coroutine, post-await); execution,
      signing, SQLite ownership and state mutation never move into the worker thread.
    """

    def __init__(self, preview_fn, apply_fn, *, now_fn, max_workers=2, deadline_s=3.0,
                 worker_pool=None):
        self._preview_fn = preview_fn
        self._apply_fn = apply_fn
        self._now_fn = now_fn
        self._deadline_s = float(deadline_s)
        self._pool = worker_pool or PrewarmWorkerPool(max_workers=max_workers)
        self._owns_pool = worker_pool is None
        self._inflight = set()
        self._pending = {}
        self._tasks = set()                   # live _run tasks, so shutdown can cancel cleanly
        self._closed = False

    def inflight_keys(self):
        return set(self._inflight)

    def pending_keys(self):
        return set(self._pending)

    def schedule(self, key, pos):
        """Non-blocking. Coalesce onto an in-flight key (newest pending wins) or launch an off-loop
        preview task. Safe to call every housekeeping tick from the loop."""
        import asyncio
        if self._closed:
            return
        if key in self._inflight:
            self._pending[key] = pos          # coalesce: newest request replaces any older pending
            return
        self._inflight.add(key)
        task = asyncio.ensure_future(self._run(key, pos))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, key, pos):
        import asyncio
        try:
            worker_future = None
            while worker_future is None and not self._closed:
                worker_future = self._pool.try_submit(key, self._preview_fn, key, pos)
                if worker_future is None:
                    await asyncio.sleep(0.01)  # no executor queue; coalescing continues while waiting
            if worker_future is None:
                return
            future = asyncio.wrap_future(worker_future)
            try:
                preview = await asyncio.wait_for(
                    asyncio.shield(future), timeout=self._deadline_s)
            except (asyncio.TimeoutError, Exception):
                preview = None
            # A newer coalesced request supersedes this result. Never briefly publish the older quote.
            if (preview is not None and key not in self._pending
                    and getattr(preview, "failure", None) is None):
                try:
                    self._apply_fn(key, preview, float(self._now_fn()))       # ON the loop thread
                except Exception:
                    logger.exception("prewarm scheduler: apply failed key=%s", key)
        except Exception:
            logger.exception("prewarm scheduler: run failed key=%s", key)
        finally:
            self._inflight.discard(key)
            nxt = self._pending.pop(key, None)
            if nxt is not None and not self._closed:
                self.schedule(key, nxt)       # the newest coalesced request runs next

    def shutdown(self):
        self._closed = True
        for task in list(self._tasks):
            task.cancel()                     # a _run blocked on the concurrency gate is cancelled cleanly
        self._tasks.clear()
        if self._owns_pool:
            try:
                self._pool.shutdown()
            except Exception:
                logger.exception("prewarm scheduler: worker-pool shutdown failed")


class HotSupervisor:
    """Bridges WS rows -> the existing hot engine -> the existing execution adapter, with the pull
    lane as verifier + fallback. `now_fn` supplies the CORRECTED clock (this host runs ~+7h fast)."""

    def __init__(self, engine_set, *, now_fn, on_decision=None, max_decision_gap_s=60.0,
                 prewarm_max_age_s=5.0, decision_cfg=None, decode_ctx_fn=None, decode_max_sigs=10,
                 decode_page_limit=1000, decode_max_pages=4):
        self.engines = engine_set
        self.now_fn = now_fn
        self.on_decision = on_decision                    # wired to the runner's engine adapter
        self.decision_cfg = decision_cfg
        self.gap = DecisionGapTracker(max_decision_gap_s)
        self.verifier = PullVerifier()
        self.prewarm = PrewarmedExits(max_age_s=prewarm_max_age_s)
        self.ws_seen_slots = set()
        self.decision_latencies_ms = []
        self.decode_ctx_fn = decode_ctx_fn                # WO-2: key -> SolanaDecodeContext | None
        self.decode_max_sigs = int(decode_max_sigs)
        self.decode_page_limit = max(1, min(1000, int(decode_page_limit)))
        self.decode_max_pages = max(1, min(100, int(decode_max_pages)))

    def _prepare_row(self, row):
        if row is None:
            return None
        now = float(self.now_fn())
        row = dict(row)
        if row.get("notified_ts") is None:
            row["notified_ts"] = now
        if row.get("block_ts") is None:
            row["block_ts"] = now
        slot = row.get("block_no")
        if slot is not None:
            self.ws_seen_slots.add(slot)
        key = (row.get("chain_id"), row.get("pool"))
        decision = self.engines.on_tick(key, row, now_ts=now, cfg=self.decision_cfg)
        if decision is not None and row.get("notified_ts") is not None:
            self.decision_latencies_ms.append((now - float(row["notified_ts"])) * 1000.0)
        return now, row, key, decision

    def _engine_history_incomplete(self, key):
        """WO-11: the engine holding this pool lost sale authority because its retained tape overran
        the memory backstop (or a truncated rebuild) — evidence is incomplete until a pull-complete
        rebuild restores it. Stubs without an `engine()` accessor are treated as complete."""
        eng = self.engines.engine(key) if hasattr(self.engines, "engine") else None
        return bool(getattr(eng, "history_incomplete", False))

    def _finish_decision(self, key, row, decision, now, emit, resolved):
        # WO-11 completeness gate: no hot decision derived from truncated history can resolve the gap.
        # This also suppresses structure-hysteresis persistence; otherwise incomplete pivots could enter
        # the durable controller and help authorize a later sale after rebuild. Force pull immediately.
        if self._engine_history_incomplete(key):
            emit, resolved = None, False
            self.gap.invalidate()
        if resolved:
            self.gap.record_decision(now)
        if emit is not None and self.on_decision is not None:
            try:
                preview = (self.prewarm.get_fresh(key, now)
                           if getattr(emit, "action", emit) == "CUT" else None)
                self.on_decision(key, emit, row, preview)
            except Exception:
                logger.exception("hot supervisor: on_decision failed key=%s", key)

    def on_ws_row(self, row):
        """The `on_row` callback for run_vault_stream. Stamp block_ts (Solana slot time ~ notification
        time on the corrected clock), route to the hot engine, record latency + slot + decision."""
        prepared = self._prepare_row(row)
        if prepared is None:
            return None
        now, row, key, decision = prepared
        if decision is not None:
            # WO-2: a sale evidenced by a vault delta (this provisional row) is not authorized until
            # the triggering tx decodes and re-proposes the sale. Decode failure leaves the gap OPEN
            # so the pull lane covers the rug (accepted RPC tradeoff).
            if _decision_needs_decode(decision) and not row_is_transaction_grounded(row):
                emit, resolved = self._corroborate(key, row, decision, now)
            else:
                # Not a vault-delta sale, OR an already transaction-grounded row (EVM): authorize
                # directly. EVM logs are natively tx-grounded (tx hash + log index), so WO-2's Solana
                # corroboration invariant does not apply — but ONLY because the row carries explicit
                # transaction_decoded provenance, never because it is "an EVM chain".
                emit, resolved = decision, True
            self._finish_decision(key, row, decision, now, emit, resolved)
        return decision

    def on_evm_block(self, rows):
        """Load one pool's complete decoded EVM block, then decide once at block grain."""
        rows = [dict(row) for row in (rows or ()) if row is not None]
        if not rows:
            return None
        now = float(self.now_fn())
        key = (rows[0].get("chain_id"), rows[0].get("pool"))
        if any((row.get("chain_id"), row.get("pool")) != key for row in rows):
            raise ValueError("EVM block batch must contain exactly one pool")
        for row in rows:
            if row.get("notified_ts") is None:
                row["notified_ts"] = now
            if row.get("block_ts") is None:
                row["block_ts"] = now
            if row.get("block_no") is not None:
                self.ws_seen_slots.add(row["block_no"])
        decision = self.engines.on_ticks(
            key, rows, now_ts=now, cfg=self.decision_cfg)
        if decision is not None:
            notified = min(float(row["notified_ts"]) for row in rows)
            self.decision_latencies_ms.append((now - notified) * 1000.0)
            representative = rows[-1]
            block_grounded = all(row_is_transaction_grounded(row) for row in rows)
            if _decision_needs_decode(decision) and not block_grounded:
                emit, resolved = self._corroborate(key, representative, decision, now)
            else:
                emit, resolved = decision, True
            self._finish_decision(key, representative, decision, now, emit, resolved)
        return decision

    async def on_ws_row_async(self, row):
        """Production callback: keep the asyncio loop available to the enforced pull fallback while
        bounded read-RPC transaction corroboration runs in a worker thread. Execution callbacks resume
        on the hot-lane thread, which owns their SQLite connection."""
        import asyncio
        prepared = self._prepare_row(row)
        if prepared is None:
            return None
        now, row, key, decision = prepared
        if decision is not None:
            if _decision_needs_decode(decision) and not row_is_transaction_grounded(row):
                decoded = await asyncio.to_thread(self._decode_corroboration, key)
                emit, resolved = self._apply_corroboration(key, decoded, now)
            else:
                emit, resolved = decision, True     # WO-9: tx-grounded EVM row authorizes directly
            self._finish_decision(key, row, decision, now, emit, resolved)
        return decision

    def _complete_signatures(self, ctx, slot_ts):
        """Enumerate until the scan crosses below the oldest required slot (or exhausts history).

        Slot presence alone is not completeness: a page boundary may split one slot. Returning None
        keeps the sale held for the pull lane on RPC failure, malformed pagination, or decode-cap hit.
        """
        target = set(slot_ts)
        if not target:
            return None
        oldest = min(target)
        before = None
        seen_sigs = set()
        scoped = []
        for _page_no in range(self.decode_max_pages):
            params = {"limit": self.decode_page_limit}
            if before is not None:
                params["before"] = before
            page = ctx.get_sigs_fn(ctx.coin_vault, params)
            if page is None or not isinstance(page, (list, tuple)):
                return None
            for item in page:
                if not isinstance(item, dict):
                    return None
                sig = item.get("signature")
                slot = item.get("slot")
                if not sig or slot is None:
                    return None
                if sig in seen_sigs:
                    continue
                seen_sigs.add(sig)
                if slot in target:
                    scoped.append(item)
                    if len(scoped) > self.decode_max_sigs:
                        return None
            covered = {item.get("slot") for item in scoped} == target
            crossed = any(item.get("slot") < oldest for item in page)
            exhausted = len(page) < self.decode_page_limit
            if crossed or exhausted:
                return scoped if covered else None
            if not page:
                return scoped if covered else None
            next_before = page[-1].get("signature")
            if not next_before or next_before == before:
                return None
            before = next_before
        return None

    def _corroborate(self, key, row, decision, now):
        """Decode the triggering transaction(s) and re-propose on tx-grounded rows. Returns
        (emit, resolved): `emit` is the sale to authorize or None; `resolved` is True when the hot
        lane decided this (acted, or affirmatively no-sale after a clean decode) and False when it
        fell through to the pull lane (decode failure) so the decision gap stays open."""
        return self._apply_corroboration(key, self._decode_corroboration(key), now)

    def _decode_corroboration(self, key):
        """Read-RPC phase only. Safe in a worker thread; never touches the engine or SQLite."""
        if self.decode_ctx_fn is None:
            return None
        ctx = self.decode_ctx_fn(key)
        if ctx is None:
            return None
        try:
            # All sale-authorizing price/flow/vol/arrival/depth inputs must be transaction-grounded.
            # Pull-warmed history already carries tx_sig; enumerate every remaining provisional slot.
            eng = getattr(self.engines, "engine", lambda _k: None)(key)
            slot_ts = eng.provisional_slots() if eng is not None else None
            if not slot_ts:
                return None                                # feature scope unknowable -> pull covers
            in_scope = self._complete_signatures(ctx, slot_ts)
            if in_scope is None:
                return None                                # incomplete/bounded RPC -> pull covers
            from sniper.services.memecoin_solana_amounts import (
                NATIVE_SOL, QUOTE_MINTS, vault_rows)
            decoded = []
            for s in in_scope:
                sig = s.get("signature")
                tx = ctx.get_tx_fn(sig) if sig else None
                if tx is None:
                    return None                            # decode failure -> pull covers
                # PASS THE BOUND QUOTE MINT, NOT THE STATIC PAIR (2026-09-23). The pull lane
                # fixed this on 2026-09-22 (memecoin_solana_tape.py, QQQ position 63509) and the
                # fast lane was left on the default: `vault_rows` matches the quote leg BY MINT
                # against QUOTE_MINTS=(WSOL, USDC), so a pool quoted in anything else finds no
                # quote leg, emits no `sol_swap`, and yields a `sol_sync` whose amount1 is None.
                # Measured 2026-09-23: 295 of 1,094 bound pools in `sol_pool_vaults` (27.0%) carry
                # a quote mint that is neither WSOL, USDC nor native-sol.
                # NATIVE_SOL is deliberately NOT passed through: a pump curve holds lamports, not
                # an SPL quote vault, and the pull lane decodes it with `pump_curve_rows` instead.
                # Passing "native-sol" as a mint would match nothing; the default keeps today's
                # behaviour for those 96 pools, and they stay a NAMED gap rather than a silent one.
                _qm = ((ctx.quote_mint,) if ctx.quote_mint and ctx.quote_mint != NATIVE_SOL
                       else QUOTE_MINTS)
                for r in vault_rows(tx, ctx.coin_mint, ctx.coin_vault,
                                    slot=s["slot"], signature=sig, quote_mints=_qm,
                                    quote_vault=(ctx.quote_vault or None)):
                    # keep the slot's ORIGINAL event time — stamping `now` would time-travel older
                    # slots into the arrival/flow windows and distort rate/vol geometry
                    decoded.append({**r, "block_ts": slot_ts[s["slot"]]})
        except Exception:
            logger.exception("hot supervisor: decode corroboration failed key=%s", key)
            return None                                    # decode failure -> pull covers
        return set(slot_ts), decoded

    def _apply_corroboration(self, key, decoded, now):
        """Owner-thread phase. Engine state may close over a thread-bound SQLite connection."""
        if decoded is None:
            return None, False
        slots, decoded_rows = decoded
        re_decision = self.engines.corroborate(
            key, slots, decoded_rows, now_ts=now, cfg=self.decision_cfg)
        if re_decision is None:
            return None, False                             # recompute failure -> pull covers
        if re_decision is not None and _is_sale(re_decision):
            return re_decision, True                       # tx-grounded sale confirmed
        if re_decision is not None and _structure_pending(re_decision):
            # First decoded break: no sale authority yet, but the funded callback must persist the
            # one-event controller state so the second decoded event can CUT immediately.
            return re_decision, True
        return None, True                                  # decoded, no sale -> fabricated; hot resolved

    def record_pull_decision(self, now_ts):
        """The pull lane produced a decision — it resets the gap, proving the fallback covered it."""
        self.gap.record_decision(now_ts)

    def run_pull_fallback(self, pull_fn, *, now_ts=None):
        """Execute the verifier/pull lane when the decision gap crosses its bound."""
        now = float(self.now_fn() if now_ts is None else now_ts)
        if not self.gap.needs_pull_fallback(now):
            return None
        result = pull_fn()
        self.record_pull_decision(now)
        return result

    def verify_against_pull(self, pull_seen_slots):
        return self.verifier.check(self.ws_seen_slots, pull_seen_slots)

    def decision_latency_p50_ms(self):
        xs = sorted(self.decision_latencies_ms)
        return xs[len(xs) // 2] if xs else None


def hot_state_from_feats(feats, *, now_ts, tape_complete, stale_s, min_points,
                         mint, pool, position_id, chain_id="solana", pool_kind=None,
                         v3_depth_verified=frozenset()):
    """Pure hot-lane MarketState builder (WO-1). Per-family trust is carried in provenance and the
    label reflects market STRUCTURE — it no longer collapses to DATA_INSUFFICIENT just because one
    family is stale. Two consequences of the missed-rug fix:
      - a partially-stale window (fresh depth/burn, stale swaps) stays actionable; per-rule
        candidate_capable() in the engine then lets only the fresh-family rules act;
      - WARMUP describes structure only; a decoded LP pull with trusted depth remains eligible.
    Hard per-family freshness caps still apply (no decay): a family older than `stale_s` is untrusted.
    """
    from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel
    from sniper.services.memecoin_exit_market_state import ENGINE_VERSION, FEATURE_VERSION, _iso

    latest = feats.get("latest_event_ts")
    event_age = (float(now_ts) - float(latest)) if latest is not None else None
    swap_ts = feats.get("latest_swap_ts")
    depth_ts = feats.get("latest_depth_ts")
    swap_age = (float(now_ts) - float(swap_ts)) if swap_ts is not None else None
    depth_age = (float(now_ts) - float(depth_ts)) if depth_ts is not None else None
    is_v3 = str(pool_kind or "").lower() in ("v3", "v3_swap")
    depth_value = feats.get("active_liquidity") if is_v3 else feats.get("reserve_base")
    depth_supported = not is_v3 or str(chain_id) in {
        str(v) for v in (v3_depth_verified or ())}
    family_present = {
        "price": feats.get("nominal_price") is not None,
        "flow": feats.get("net_flow_quote") is not None,
        "vol": feats.get("realized_vol") is not None,
        "depth": depth_value is not None and depth_supported,
    }
    family_age = {"price": swap_age, "flow": swap_age, "vol": swap_age, "depth": depth_age}
    family_ready = {
        family: bool(present and tape_complete and family_age[family] is not None
                     and 0.0 <= family_age[family] <= stale_s)
        for family, present in family_present.items()
    }
    if latest is None or event_age is None or event_age < 0:
        label = StateLabel.DATA_INSUFFICIENT
    elif event_age > stale_s:
        label = StateLabel.DATA_STALE            # nothing fresh at all -> nothing to act on
    elif int(feats.get("structure_points") or 0) < min_points:
        label = StateLabel.WARMUP                # too little structure to trust ANY rule yet
    elif feats.get("depth_pull_flag") is True:
        label = StateLabel.EXIT_PROPOSED         # a decoded LP pull on a warmed pool
    elif (feats.get("nominal_price") is not None and feats.get("prior_low") is not None
          and feats["nominal_price"] < feats["prior_low"]):
        label = StateLabel.BREAK_CONFIRMED
    elif feats.get("lower_high_state") is True:
        label = StateLabel.WEAKENING
    else:
        label = StateLabel.TRENDING
    prov = {family: Provenance(
        ("evm_confirmed_log" if str(chain_id) not in ("solana", "sol")
         else "solana_ws_vault"), freshness_s=family_age[family],
        coverage=1.0 if family_ready[family] else 0.0,
        quality="trusted" if family_ready[family] else "untrusted",
        feature_version=FEATURE_VERSION,
        price_kind="block_vwap_exec_atomic" if family == "price" else None)
        for family in ("price", "flow", "vol", "depth")}
    return MarketState(
        chain_id=str(chain_id), mint=str(mint or ""), pool=pool,
        position_id=int(position_id), engine_version=ENGINE_VERSION,
        feature_version=FEATURE_VERSION, state=label,
        required_families=("price", "flow", "vol", "depth"),
        source_event_time=_iso(latest), observed_time=_iso(now_ts),
        latest_event_id=feats.get("latest_event_id"),
        latest_event_time=_iso(feats.get("latest_swap_ts")),
        latest_event_block_no=feats.get("latest_event_block_no"),
        latest_event_log_index=feats.get("latest_event_log_index"),
        computed_time=_iso(now_ts), age_s=event_age,
        nominal_price=feats.get("nominal_price"), prior_low=feats.get("prior_low"),
        prior_high=feats.get("prior_high"), higher_low_state=feats.get("higher_low_state"),
        lower_high_state=feats.get("lower_high_state"),
        confirmed_pivots=feats.get("confirmed_pivots") or (),
        realized_vol=feats.get("realized_vol"), net_flow_quote=feats.get("net_flow_quote"),
        buy_n=feats.get("buy_n"), sell_n=feats.get("sell_n"),
        arrival_rate=feats.get("arrival_rate"), arrival_accel=feats.get("arrival_accel"),
        reserve_base=feats.get("reserve_base"), reserve_quote=depth_value,
        liquidity_removed_fraction=feats.get("liquidity_removed_fraction"),
        depth_pull_flag=feats.get("depth_pull_flag"), provenance=prov)


def build_hot_book(positions, sol_conn, *, now_fn):
    """Build and history-warm the held Solana engine/vault book from the worker-owned tape."""
    from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel
    from sniper.services.exit_engine_hot import _MAX_EVENTS, HotEngineSet, HotPositionEngine
    from sniper.services.memecoin_exit_market_state import ENGINE_VERSION, FEATURE_VERSION, _epoch, _iso
    from sniper.services.memecoin_exit_materializer import resolve_pool_for_position
    from sniper.services.memecoin_exit_brain import exit_engine
    from sniper.services.memecoin_funded_policy import (
        ENGINE_ENVELOPE_POLICY_ID, band_anchor_trail_pct, frozen_policy_from_position)
    from sniper.services.memecoin_solana_ws import VaultTape
    from sniper.settings import memecoin as cfg

    engines, vaults, entries = HotEngineSet(), {}, {}
    for pos in positions:
        if str(pos.get("network_id") or "") != "solana":
            continue
        try:
            policy = frozen_policy_from_position(pos)
        except (TypeError, ValueError):
            continue
        if policy is None or policy.policy_id != ENGINE_ENVELOPE_POLICY_ID:
            continue
        decision_engine_version, decision_fn = exit_engine(
            getattr(policy, "decision_engine_version", "v5"))
        pool = resolve_pool_for_position(
            sol_conn, "solana", pos.get("mint"), position_id=pos.get("id"))
        row = (sol_conn.execute(
            "SELECT pool,coin_vault,quote_vault,quote_mint FROM sol_pool_vaults WHERE pool=?",
            (pool,)
        ).fetchone() if pool else None)
        if not row:
            continue
        pool, coin_vault, quote_vault = (str(row[0]), str(row[1]), str(row[2]))
        quote_mint = str(row[3] or "")
        cursor = sol_conn.execute(
            "SELECT frontier_before FROM sol_sig_cursor WHERE vault=?", (pool,)).fetchone()
        if not cursor or cursor[0]:
            logger.warning("hot supervisor: incomplete Solana tape pool=%s; not tracked", pool)
            continue

        def state_fn(feats, *, now_ts, pos=pos, pool=pool):
            try:
                live_cursor = sol_conn.execute(
                    "SELECT frontier_before FROM sol_sig_cursor WHERE vault=?", (pool,)).fetchone()
                tape_complete = bool(live_cursor and not live_cursor[0])
            except Exception:
                tape_complete = False
            return hot_state_from_feats(
                feats, now_ts=now_ts, tape_complete=tape_complete,
                stale_s=float(getattr(cfg, "MEMECOIN_EXIT_STALE_S", 180.0)),
                min_points=int(getattr(cfg, "MEMECOIN_EXIT_MIN_STRUCTURE_POINTS", 5)),
                mint=pos.get("mint"), pool=pool, position_id=pos["id"])

        opened_ts = _epoch(pos.get("opened_at"))
        held_s = (max(0.0, float(now_fn()) - opened_ts) if opened_ts is not None else None)
        from sniper.analysis.exit_controller import controller_state_from_row
        contract = {"trail_pct": band_anchor_trail_pct(policy.coin_band), "held_s": held_s,
                    "chain_id": "solana", "pool": pool,
                    "opened_ts": opened_ts, "coin_band": policy.coin_band,
                    "decision_engine_version": getattr(
                        policy, "decision_engine_version", "v5"),
                    "derisked_at": pos.get("derisked_at"),
                    "exact_quote_misses": pos.get("exact_quote_misses"),
                    # WO-5B route/no-progress feed input; NULL/0 until reducer wired live (conservative)
                    "controller_state": controller_state_from_row(pos)}
        engine = HotPositionEngine(
            pos["id"], contract, quote_side=1, state_fn=state_fn,
            decision_fn=decision_fn, engine_version=decision_engine_version,
            max_events=_MAX_EVENTS,
            structure_window_s=getattr(cfg, "MEMECOIN_HOT_STRUCTURE_WINDOW_S", None))
        # WO-11: fetch cap+1 so a warm read that TRUNCATED the tape is detected (the cap+1'th row
        # overruns the engine's memory backstop -> history_incomplete -> no hot sale authority). A
        # LIMIT of exactly cap would silently look complete.
        for tick in sol_conn.execute(
            "SELECT p.kind,p.block_no,p.tx_sig,p.log_index,p.amount0,p.amount1,b.block_ts "
            "FROM pool_ticks p "
                "JOIN block_times b ON b.chain_id=p.chain_id AND b.block_no=p.block_no "
                "WHERE p.chain_id='solana' AND p.pool=? ORDER BY p.block_no DESC LIMIT ?",
                (pool, _MAX_EVENTS + 1)).fetchall()[::-1]:
            ts = _epoch(tick["block_ts"])
            if ts is not None:
                engine.add_tick({**dict(tick), "block_ts": ts})
        engine.warm(now_ts=float(now_fn()), cfg=cfg)
        key = ("solana", pool)
        engines.track(key, engine)
        tape = VaultTape(coin_vault, quote_vault, mint=pool)
        vaults[coin_vault] = tape
        vaults[quote_vault] = tape
        entries[key] = {"position": pos, "engine": engine,
                        # WO-2: vault identity for tx-grounded decode corroboration
                        "coin_vault": coin_vault, "coin_mint": str(pos.get("mint") or ""),
                        # WO-2: the quote mint the binding resolved, so the fast lane decodes the
                        # same quote leg the pull lane does.
                        "quote_mint": quote_mint, "quote_vault": quote_vault}
    return engines, vaults, entries


def build_evm_hot_book(positions, ticks_conn, *, now_fn):
    """WO-9 production reachability — build+history-warm the held EVM engine/pool book for the SHARED
    hot supervisor. Mirrors build_hot_book for the funded EVM chains (4663, 8453): each held
    engine-envelope EVM position gets a HotPositionEngine (the SAME class as Solana) warmed from the
    confirmed-block tape and tracked under (chain_id, pool). No EVM chain can run on the 30s pull
    cadence — an on-chain rug resolves in seconds — so this is what makes an EVM position reachable
    by the push lane at all.

    Held pools ONLY: pool identity is the position's own `quote_route_target` (its entry pool) — a
    position with none is skipped and the pull lane still covers it. Returns
    (engines, entries, held_by_chain) where held_by_chain maps chain_id -> the sorted held pools that
    scope each chain's eth_getLogs. `entries` carries NO Solana vault identity (EVM logs are natively
    transaction-grounded, so the WO-2 vault-delta corroboration does not apply)."""
    from sniper.services.memecoin_evm_hot import HOT_CHAINS
    from sniper.services.exit_engine_hot import _MAX_EVENTS, HotEngineSet, HotPositionEngine
    from sniper.services.memecoin_exit_market_state import _epoch
    from sniper.services.memecoin_exit_brain import exit_engine
    from sniper.services.memecoin_funded_policy import (
        ENGINE_ENVELOPE_POLICY_ID, band_anchor_trail_pct, frozen_policy_from_position)
    from sniper.analysis.exit_controller import controller_state_from_row
    from sniper.services.memecoin_exit_materializer import (
        _pool_kind, _quote_side, resolve_pool_for_position)
    from sniper.settings import memecoin as cfg

    engines, entries, held_by_chain = HotEngineSet(), {}, {}
    for pos in positions:
        chain = str(pos.get("network_id") or "")
        if chain not in HOT_CHAINS:
            continue
        try:
            policy = frozen_policy_from_position(pos)
        except (TypeError, ValueError):
            continue
        if policy is None or policy.policy_id != ENGINE_ENVELOPE_POLICY_ID:
            continue
        # The ONE canonical position->pool resolver (pool_meta), NEVER quote_route_target — that is the
        # router/settler and has NO ticks by construction, so it would warm an empty tape and scan the
        # wrong address. This is the SAME resolver the pull materializer + exit-state worker use, so the
        # hot lane and its pull fallback resolve the identical pool (and the identical tape) by
        # construction. Ambiguity (>1 usable pool) fails closed to the pull lane.
        pool = resolve_pool_for_position(ticks_conn, chain, pos.get("mint"),
                                        route_target=pos.get("quote_route_target"))
        if not pool:
            logger.warning("evm hot book: position %s pool unresolved; not tracked (pull covers)",
                           pos.get("id"))
            continue
        pool = str(pool).lower()
        quote_side = _quote_side(ticks_conn, chain, pool)
        if quote_side not in (0, 1):
            logger.warning("evm hot book: position %s quote side unresolved; not tracked (pull covers)",
                           pos.get("id"))
            continue
        quote_side = int(quote_side)
        decision_engine_version, decision_fn = exit_engine(
            getattr(policy, "decision_engine_version", "v5"))
        # V3 vs V2 depth family is decided by the pool's recorded tick kinds (canonical _pool_kind).
        # A V3 pool on an UNVERIFIED chain (4663 is never added to MEMECOIN_V3_DEPTH_VERIFIED_CHAINS)
        # simply has untrusted depth — its LP-CUT cannot fire, but price/flow rules still do. V2 depth
        # (reserve_base) needs no verified claim. Unknown kind (no swaps yet) -> untrusted depth.
        pool_kind = _pool_kind(ticks_conn, chain, pool)

        def state_fn(feats, *, now_ts, pos=pos, pool=pool, chain=chain, pool_kind=pool_kind):
            # The EVM tape is a forward-only getLogs scan — complete by construction up to its cursor
            # (no async backfill frontier like Solana's), so freshness caps, not a frontier flag, gate
            # trust. A family older than stale_s is untrusted regardless.
            return hot_state_from_feats(
                feats, now_ts=now_ts, tape_complete=True,
                stale_s=float(getattr(cfg, "MEMECOIN_EXIT_STALE_S", 180.0)),
                min_points=int(getattr(cfg, "MEMECOIN_EXIT_MIN_STRUCTURE_POINTS", 5)),
                mint=pos.get("mint"), pool=pool, position_id=pos["id"],
                chain_id=chain, pool_kind=pool_kind,
                v3_depth_verified=cfg.MEMECOIN_V3_DEPTH_VERIFIED_CHAINS)

        opened_ts = _epoch(pos.get("opened_at"))
        held_s = (max(0.0, float(now_fn()) - opened_ts) if opened_ts is not None else None)
        contract = {"trail_pct": band_anchor_trail_pct(policy.coin_band), "held_s": held_s,
                    "chain_id": chain, "pool": pool, "quote_side": quote_side,
                    "opened_ts": opened_ts,
                    "coin_band": policy.coin_band,
                    "decision_engine_version": getattr(policy, "decision_engine_version", "v5"),
                    "derisked_at": pos.get("derisked_at"),
                    "exact_quote_misses": pos.get("exact_quote_misses"),
                    "controller_state": controller_state_from_row(pos)}
        engine = HotPositionEngine(
            pos["id"], contract, quote_side=quote_side, state_fn=state_fn,
            decision_fn=decision_fn, engine_version=decision_engine_version,
            max_events=_MAX_EVENTS,
            structure_window_s=getattr(cfg, "MEMECOIN_HOT_STRUCTURE_WINDOW_S", None))
        # WO-11: cap+1 so a truncated warm read is detected (overruns the backstop -> history_incomplete).
        for tick in ticks_conn.execute(
            "SELECT p.kind,p.block_no,p.tx_sig,p.log_index,p.amount0,p.amount1,p.liquidity,"
            "p.tick,p.sqrt_price_x96,b.block_ts "
            "FROM pool_ticks p JOIN block_times b "
                "ON b.chain_id=p.chain_id AND b.block_no=p.block_no "
                "WHERE p.chain_id=? AND p.pool=? ORDER BY p.block_no DESC LIMIT ?",
                (chain, pool, _MAX_EVENTS + 1)).fetchall()[::-1]:
            ts_val = _epoch(tick["block_ts"])
            if ts_val is not None:
                engine.add_tick({**dict(tick), "block_ts": ts_val})
        engine.warm(now_ts=float(now_fn()), cfg=cfg)
        key = (chain, pool)
        engines.track(key, engine)
        entries[key] = {"position": pos, "engine": engine}
        held_by_chain.setdefault(chain, []).append(pool)
    for chain in held_by_chain:
        held_by_chain[chain] = sorted(set(held_by_chain[chain]))
    return engines, entries, held_by_chain


async def run_hot_exit_lane(engines, vaults, entries, *, now_fn, on_decision, pull_fn,
                            preview_fn, ws_url=None, stop=None, max_decision_gap_s=60.0,
                            prewarm_period_s=3.0, duration_s=None, decision_cfg=None,
                            evm_lanes=None, now_iso_fn=None, evm_poll_s=2.0,
                            prewarm_workers=2, prewarm_deadline_s=3.0, prewarm_pool=None):
    """Run push decisions plus enforced pull fallback. Caller owns thread/process isolation."""
    import asyncio
    from sniper.services.memecoin_solana_ws import run_vault_stream

    from sniper.services.memecoin_solana_amounts import get_transaction_fn
    from sniper.services.memecoin_solana_tape import get_signatures_fn
    from sniper.settings import memecoin as cfg

    stop = stop or (lambda: False)
    # WO-2: activate tx-grounded sale authority via the read-RPC getSignaturesForAddress+getTransaction
    # path. No read RPC -> build_decode_ctx_fn returns None -> every vault-delta sale fails closed.
    decode_ctx_fn = build_decode_ctx_fn(
        entries, get_sigs_fn=get_signatures_fn(), get_tx_fn=get_transaction_fn())
    supervisor = HotSupervisor(
        engines, now_fn=now_fn, on_decision=on_decision,
        max_decision_gap_s=max_decision_gap_s, decision_cfg=decision_cfg,
        decode_ctx_fn=decode_ctx_fn, decode_max_sigs=cfg.MEMECOIN_HOT_DECODE_MAX_SIGS,
        decode_max_pages=cfg.MEMECOIN_HOT_DECODE_MAX_PAGES)
    rebuild = asyncio.Event()

    # WO-12A: previews run OFF the loop (own bounded executor, hard deadline, per-key coalescing) so a
    # slow quote never blocks WS frame processing or the pull-fallback trigger. Results apply ON the
    # loop via prewarm.refresh (execution/SQLite stay on the loop thread). Spare capacity only.
    def _apply_prewarm(key, preview, now_ts):
        supervisor.prewarm.refresh(key, preview, now_ts)

    prewarm_scheduler = PrewarmScheduler(
        preview_fn, _apply_prewarm, now_fn=now_fn,
        max_workers=int(prewarm_workers), deadline_s=float(prewarm_deadline_s),
        worker_pool=prewarm_pool)

    async def stream_loop():
        while not stop() and not rebuild.is_set():
            try:
                await run_vault_stream(vaults, supervisor.on_ws_row_async, ws_url=ws_url, stop=stop)
            except Exception:
                logger.exception("hot supervisor: vault stream failed; pull lane remains active")
            if not stop():
                try:
                    supervisor.run_pull_fallback(pull_fn, now_ts=float(now_fn()))
                finally:
                    # Never reuse balances/ring history across a socket gap. Rebuild from the
                    # pull-complete tape before accepting another push decision.
                    rebuild.set()

    async def housekeeping():
        last_warm = 0.0
        while not stop() and not rebuild.is_set():
            now = float(now_fn())
            if now - last_warm >= float(prewarm_period_s):
                for key, entry in entries.items():
                    prewarm_scheduler.schedule(key, entry["position"])   # non-blocking, off-loop
                last_warm = now
            try:
                supervisor.run_pull_fallback(pull_fn, now_ts=now)
            except Exception:
                logger.exception("hot supervisor: pull fallback failed")
            if rebuild_requested():
                # A funded position opened. End the epoch so the next book includes it, rather
                # than leaving it unpriced for up to 30s with no rail able to fire on it.
                logger.warning("hot lane: early rebuild requested (new funded position)")
                rebuild.set()
                break
            await asyncio.sleep(1.0)

    async def evm_lane(lane):
        """WO-9: one shared-supervisor EVM push lane per chain. Held pools only; the RPC round-trip
        runs OFF the event loop (never starving the enforced pull fallback) and feeding — which may
        authorize+execute — runs ON this loop thread, which owns the execution SQLite connection.
        A grounded EVM log authorizes a sale directly via explicit provenance; anything else fails
        closed to the pull lane. Rebuilds each epoch like every other lane."""
        from sniper.services.memecoin_evm_hot import feed_evm_blocks, fetch_evm_hot_logs
        cursor = None
        iso_fn = now_iso_fn or (lambda: None)
        while not stop() and not rebuild.is_set():
            try:
                new_cursor, blocks = await asyncio.to_thread(
                    fetch_evm_hot_logs, lane["chain_id"], lane["held_pools"], lane["rpc_call"],
                    cursor=cursor)
                feed_evm_blocks(supervisor, lane["chain_id"], blocks, observed_at=iso_fn())
                cursor = new_cursor
            except Exception:
                logger.exception("hot supervisor: evm lane failed chain=%s", lane.get("chain_id"))
            await asyncio.sleep(float(evm_poll_s))

    # An EVM-only book has no Solana vaults -> skip the WS stream (nothing to subscribe to) rather
    # than spinning a socket at DEFAULT_WS. The pull fallback + prewarm still run via housekeeping.
    tasks = [asyncio.create_task(housekeeping())]
    if vaults:
        tasks.append(asyncio.create_task(stream_loop()))
    for lane in (evm_lanes or ()):
        if lane.get("held_pools"):
            tasks.append(asyncio.create_task(evm_lane(lane)))
    try:
        if duration_s is None:
            await asyncio.gather(*tasks)
        else:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=float(duration_s))
    except asyncio.TimeoutError:
        pass
    finally:
        prewarm_scheduler.shutdown()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
