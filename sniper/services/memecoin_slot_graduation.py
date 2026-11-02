"""GRADUATION DIRECT: buy a copied curve coin on its canonical PumpSwap pool the moment it exists.

Operator order via Alpha, 2026-10-07 13:4xZ ("it's a fact that it happens and we need to address it").
A copied pump curve buy cannot land once the curve completes: the slot-lane send reverts
BondingCurveComplete (6005), or the source's own TradeEvent already leaves no real tokens. pump.fun
then migrates the coin into ONE pool, PDA["pool", 0u16, PDA_pump["pool-authority", mint], mint, WSOL]
under pAMM, and Jupiter routes it seconds later. On the 5 graduations 2026-09-28..10-07 the pool
appeared 1-9 slots after our revert and Jupiter routed 2-9 s after that, at +39..130% over the source;
our clip's exact quote at the pool's first state was inside both caps on 2 of the 5, and only in the
pool's first 1-2 slots (BLASTAR +20.8%, C4SE +28.5%). Every BLASTAR-style case so far was completed by
OTHER buyers in the source's own slot, so the revert trigger is the one that fires; the source-completed
trigger skips a send that cannot land when the source itself completes the curve.

The watcher reads that one address (never a lookalike: owner, creator, mints and vaults are checked)
with `read_native_state` at processed commitment, every `poll_s` and at once when the migration
listener hears the pool, until it exists or `deadline_s` passes. It then hands the buy back to the
lane thread carrying the pool's state; the lane runs the ordinary live path (re-admission, reservation,
wallet lock, sizing, safety, one reserved entry per mint) and the PumpSwap builder prices our clip
from the pool and the caps from the SOURCE's curve trade, as the curve buy would have.

While it watches, GRADUATION_PENDING (memecoin_slot_lane) holds the signature, so the on-chain lane
defers instead of re-quoting Jupiter into NO_ROUTES; at the deadline the hold is released and today's
fallback runs. Shadow mode watches and logs the same way but never holds and never hands back.

QUOTE-TOKEN CURVES (operator "fix" via Alpha, 2026-10-07, HI EtVFMDoB 22:34Z): a pump curve quoted in
another token migrates into PumpSwap coin/<quote>, never coin/wSOL, and its TradeEvent carries every
SOL field zero, so neither trigger above could see it and the wSOL-only pool checks could never find
its pool. Behind MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES=live: the lane offers such a buy BEFORE it
spends an on-demand leg (quote_curve_completed), or when its build refuses 'curve is complete'; the
watch reads the coin/<quote> pool; the hand-back takes a fresh leg into the quote (the ordinary exotic
path) and builds on the pool. A wSOL curve is untouched by the knob.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time

logger = logging.getLogger(__name__)

GRADUATION_REASON = "chain_error:BondingCurveComplete"
PENDING_REASON = "slot_lane_graduation_pending"
AGED_OUT_REASON = "slot_lane_graduation_aged_out"
# The lane thread may still be finishing another buy when the pool turns up: the hold outlives the
# watch by this much so a handed-back buy can still be taken. The caps judge a late landing.
REQUEUE_GRACE_S = 1.0
MAX_JOBS = 8
# read_native_state answers that can change on the next read (the pool is not there yet, or a read of
# it is not yet visible at processed); anything else stops the watch.
TRANSIENT = frozenset({"no_curve_or_canonical_pool", "pool_or_fee_unreadable", "vault_unreadable",
                       "curve_unreadable", "curve_live", "fee_config_unreadable", "read_failed"})


def mode():
    """off | shadow | live from MEMECOIN_SLOT_LANE_GRADUATION_DIRECT; anything else is off."""
    from sniper.settings import memecoin as cfg
    value = str(getattr(cfg, "MEMECOIN_SLOT_LANE_GRADUATION_DIRECT", "off") or "off").strip().lower()
    return value if value in ("shadow", "live") else "off"


def quote_curves_on():
    """MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES: 'live' watches quote-token curves too; anything else
    leaves them as before (no trigger, no hold)."""
    from sniper.settings import memecoin as cfg
    return str(getattr(cfg, "MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES", "off") or "off").strip().lower() == "live"


def quote_curve_completed(buy):
    """The quote mint when the SOURCE's own buy completed a curve quoted in another token: a pump
    TradeEvent for this coin, a buy, quoted outside SOL, whose post-trade real token reserves are 0.
    None otherwise: a wSOL curve (curve_completed's), any token left, an unreadable event."""
    if getattr(buy, "venue", None) != "pump_curve":
        return None
    try:
        if int(getattr(buy, "virtual_sol_reserves", None) or 0) != 0:
            return None
        from sniper.execution.pump_curve_buy import SOL_QUOTE_MINTS, parse_trade_event
        event = parse_trade_event(getattr(buy, "trade_event_log", None) or "")
        quote = event.get("quote_mint")
        if (not quote or quote in SOL_QUOTE_MINTS or event.get("mint") != getattr(buy, "mint", None)
                or not event.get("is_buy", True) or "real_token_reserves" not in event
                or int(event["real_token_reserves"]) != 0):
            return None
        return str(quote)
    except (AttributeError, TypeError, ValueError, KeyError):
        return None


def curve_completed(buy):
    """True when the SOURCE's own curve buy completed the curve: a SOL curve TradeEvent whose post-trade
    real token reserves are 0 (SlotBuy.real_token_reserves is post-trade on the curve). A curve quoted
    in another token carries zero SOL fields and never counts; a near-complete curve (any token left)
    never counts."""
    if getattr(buy, "venue", None) != "pump_curve":
        return False
    try:
        return int(buy.real_token_reserves) == 0 and int(buy.virtual_sol_reserves or 0) > 0
    except (AttributeError, TypeError, ValueError):
        return False


def canonical_pool(mint, quote_mint=None):
    from sniper.execution.solana_native_entry_quote import accounts_for
    return accounts_for(mint, quote_mint)["pool"]


def pool_refusal(state, mint, quote_mint=None):
    """None when `state` (read_native_state) is the canonical, migrated PumpSwap pool of `mint` quoted
    in `quote_mint` (None: wSOL) with its own vaults; else the reason. The reasons in TRANSIENT keep
    the watch going. The quote vault must be the classic token program for any quote."""
    from sniper.execution.pump_native_exit import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, _pk, ata
    from sniper.execution.pumpswap_buy import WSOL_MINT, is_pump_migrated_pool
    if not isinstance(state, dict):
        return "read_failed"
    if state.get("venue") == "curve":
        return "curve_live"
    if state.get("venue") != "pumpswap":
        return str(state.get("reason") or "not_pumpswap")
    # A TAGGED READ ERROR KEEPS ITS OWN REASON (review SOL02, 2026-10-08): read_native_state names an
    # incomplete PumpSwap read ("pool_or_fee_unreadable", "vault_unreadable") with no account, and the
    # identity checks below read that absence as a wrong pool and ended the watch. The read is of the
    # canonical address already; fee_unmodelled keeps its own (terminal) reason too.
    if state.get("reason"):
        return str(state["reason"])
    pool = canonical_pool(mint, quote_mint)
    quote = quote_mint or WSOL_MINT
    facts = state.get("pool_facts") or {}
    base_tp, quote_tp = state.get("base_token_program"), state.get("quote_token_program")
    if state.get("account") != pool:
        return "not_canonical_pool"
    if facts.get("base_mint") != mint or facts.get("quote_mint") != quote:
        return "pool_mint_mismatch"
    if not is_pump_migrated_pool(facts):
        return "not_pump_migrated"
    if base_tp not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) or quote_tp != TOKEN_PROGRAM:
        return "token_program_unsupported"
    if (facts.get("pool_base_token_account") != str(ata(_pk(pool), _pk(mint), _pk(base_tp)))
            or facts.get("pool_quote_token_account") != str(ata(_pk(pool), _pk(quote), _pk(quote_tp)))):
        return "vault_mismatch"
    if int(state.get("base_reserve") or 0) <= 0 or int(state.get("quote_reserve") or 0) <= 0:
        return "pool_empty"
    return None


def payload(state, *, trigger, offered_mono, read_ms, now_mono):
    """The `graduation` field the lane and the PumpSwap builder read."""
    facts = dict(state["pool_facts"], base_token_program=state["base_token_program"],
                 quote_token_program=state["quote_token_program"], base_decimals=state["base_decimals"],
                 **({"quote_decimals": state["quote_decimals"]} if "quote_decimals" in state else {}))
    return {"pool": state["account"], "state": {k: state[k] for k in (
                "base_reserve", "quote_reserve", "virtual_quote", "fees", "slot", "vault_slot")},
            "pool_facts": facts, "trigger": trigger,
            "watch_ms": round((now_mono - offered_mono) * 1000.0, 1), "read_ms": round(read_ms, 1),
            "requeued_mono": now_mono}


class GraduationWatcher:
    """One thread. `offer` (lane thread) and `notify` (migration listener thread) only enqueue."""

    def __init__(self, *, read_fn, requeue_fn, wake_fn, pending, live, log, clock=time,
                 deadline_s=6.0, poll_s=0.4, max_jobs=MAX_JOBS):
        self.read_fn = read_fn          # mint -> read_native_state answer (may raise)
        self.requeue_fn = requeue_fn    # the lane queue's put
        self.wake_fn = wake_fn          # the on-chain lane's wake, after a release
        self.pending = pending          # memecoin_slot_lane.GRADUATION_PENDING
        self.live = bool(live)
        self.log = log
        self.clock = clock
        self.deadline_s, self.poll_s, self.max_jobs = float(deadline_s), float(poll_s), int(max_jobs)
        self._lock = threading.Lock()
        self._jobs = {}                 # signature -> job dict
        self._event = threading.Event()
        self.stats = {"offered": 0, "busy": 0, "requeued": 0, "expired": 0, "refused": 0, "reads": 0,
                      "read_errors": 0, "notified": 0, "found": 0, "last": None}

    def offer(self, buy, *, trigger, quote_mint=None):
        """Start watching the pool of this curve buy. True only when LIVE and now held (the lane then
        treats the buy as pending); shadow always answers False so the lane carries on as before.
        `quote_mint` (a quote-token curve) watches the coin/<quote> pool instead of coin/wSOL."""
        now = self.clock.monotonic()
        mint = getattr(buy, "mint", None)
        if not mint:
            return False
        with self._lock:
            if buy.signature in self._jobs or len(self._jobs) >= self.max_jobs:
                self.stats["busy"] += 1
                return False
            if self.live and not self.pending.add(buy, deadline=now + self.deadline_s + REQUEUE_GRACE_S):
                self.stats["busy"] += 1
                return False
            self._jobs[buy.signature] = {"buy": buy, "mint": mint, "pool": canonical_pool(mint, quote_mint),
                                         "quote": quote_mint, "trigger": trigger, "offered": now,
                                         "due": now, "reads": 0, "notifications": 0}
            self.stats["offered"] += 1
        self._event.set()
        return self.live

    def notify(self, pool):
        """The migration listener heard `pool`: read now any job waiting on it."""
        hit = False
        with self._lock:
            for job in self._jobs.values():
                if job["pool"] == pool:
                    job["due"], hit = 0.0, True
                    job["notifications"] += 1
        if hit:
            self.stats["notified"] += 1
            self._event.set()

    def run(self, stop=None):
        while stop is None or not stop.is_set():
            self._event.wait(timeout=self.poll_s)
            self._event.clear()
            try:
                self.run_once()
            except Exception:
                logger.exception("slot lane graduation watch pass failed")

    def run_once(self):
        with self._lock:                 # offer() inserts from the lane thread (Gamma LOW)
            sigs = list(self._jobs)
        for sig in sigs:
            # THE CLOCK PER JOB, AND AGAIN AFTER THE READ (SOL-3, Codex review 2026-10-07). One `now`
            # for the pass let a stalled read age the jobs behind it unseen: they read and requeued
            # after their deadline. An overdue job expires with no read; a late answer is never a
            # ready requeue. The lane's own hold check in _handle_graduation stays.
            now = self.clock.monotonic()
            with self._lock:
                job = self._jobs.get(sig)
                notifications = job["notifications"] if job is not None else 0
            if job is None or now < job["due"]:
                continue
            if now - job["offered"] >= self.deadline_s:
                self._finish(sig, job, "expired", None)
                self._sweep_holds()
                continue
            t0 = self.clock.monotonic()
            try:
                # A wSOL job calls read_fn(mint) exactly as before; only a quote job names its quote.
                state = (self.read_fn(job["mint"]) if job.get("quote") is None
                         else self.read_fn(job["mint"], quote_mint=job["quote"]))
            except Exception as exc:
                self.stats["read_errors"] += 1
                state = {"reason": "read_failed", "error": type(exc).__name__}
            read_ms = (self.clock.monotonic() - t0) * 1000.0
            job["reads"] += 1
            self.stats["reads"] += 1
            refusal = pool_refusal(state, job["mint"], job.get("quote"))
            now2 = self.clock.monotonic()
            if now2 - job["offered"] >= self.deadline_s:
                self._finish(sig, job, "expired_after_read", None)
            elif refusal is None:
                grad = payload(state, trigger=job["trigger"], offered_mono=job["offered"],
                               read_ms=read_ms, now_mono=now2)
                self._finish(sig, job, "found", grad)
            elif refusal in TRANSIENT:
                with self._lock:
                    # Preserve a migration wake received while this older read was in flight.
                    if job["notifications"] == notifications:
                        job["due"] = now2 + self.poll_s
            else:
                self._finish(sig, job, refusal, None)
            self._sweep_holds()          # between jobs, not only after the pass (SOL-3)
        self._sweep_holds()

    def _sweep_holds(self):
        # A handed-back buy the lane did not take before its hold ran out: wake the on-chain lane.
        if self.pending.expired(self.clock.monotonic()):
            self.wake_fn()

    def _finish(self, sig, job, outcome, grad):
        with self._lock:
            self._jobs.pop(sig, None)
        buy = job["buy"]
        line = "[memecoin] slot lane graduation %s %s %s %s -> %s reads=%d watch_ms=%.0f" % (
            "live" if self.live else "shadow", getattr(buy, "handle", None) or "?", str(job["mint"])[:8],
            job["trigger"], outcome, job["reads"], (self.clock.monotonic() - job["offered"]) * 1000.0)
        if job.get("quote") is not None:
            line += " quote=%s" % str(job["quote"])[:8]
        if grad is not None:
            st = grad["state"]
            line += " pool=%s slot=%s base=%s quote=%s vq=%s fees=%s read_ms=%.0f" % (
                grad["pool"][:8], st.get("slot"), st["base_reserve"], st["quote_reserve"],
                st["virtual_quote"], "/".join(str(f) for f in st["fees"]), grad["read_ms"])
        self.stats["last"] = outcome
        if grad is not None and self.live:
            self.stats["requeued"] += 1
            self.requeue_fn(dataclasses.replace(buy, graduation=grad))
        else:
            self.stats["expired" if outcome.startswith("expired") else "refused" if grad is None
                       else "found"] += 1
            if self.live and self.pending.claim(sig, self.clock.monotonic(), how="expired") is not None:
                self.wake_fn()
        self.log(line)
