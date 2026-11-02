"""Attempt-local EVM entry reads; the original quote deadline applies to reuse too."""
from __future__ import annotations

import math
import threading
import time


class EarlySafety:
    """Token safety the early worker ran beside its quote, for ONE (mint, usd). `wait()` returns the
    verdict only if it finished by its own deadline, else None. The leg reuses only a PASS."""

    def __init__(self, mint, usd, deadline):
        self.mint, self.usd, self.deadline = mint, float(usd), deadline
        self.result = self.duration_ms = self.done_at = None
        self.timings = {}
        self.done = threading.Event()

    def wait(self):
        self.done.wait(max(0.0, self.deadline - time.monotonic()))
        return (self.result if self.done.is_set() and self.done_at is not None
                and self.done_at <= self.deadline else None)


class EarlyEvmQuote:
    def __init__(self, *, mint, quote_fn, sizing_fn, trace, clock=time.monotonic, safety_fn=None,
                 safety_wait_s=12.0):
        self.mint, self.trace, self.clock = str(mint).lower(), trace, clock
        self.result = self.clip = self.done_at = None
        self.error = None
        self.taken = False
        # THE WORKER'S OWN OUTCOME (HUSH, 2026-10-07): take() reported "not_started" whether the
        # sizing was still running, refused, or lost the launch race. state: sizing -> sized ->
        # launched -> done, or the refusal; with the sizing time and its balance source.
        self.state, self.sizing_ms, self.balance_source = "sizing", None, None
        self.sizing_facts, self.sizing_done_at = {}, None
        self.own_invalidation = None
        self.safety, self.safety_wait_s = None, float(safety_wait_s)
        self._done = threading.Event()
        # One-shot launch claim; neither caller ever blocks on it. The loser cancels unused work.
        self._launch_token = threading.Lock()
        self.deadline = self.pending = None

        def on_started(start, budget, envelope):
            self.pending = dict(envelope)
            self.deadline = start + budget
            launched = self._launch_token.acquire(blocking=False)
            if launched:
                self.state = "launched"
            return launched

        def run():
            try:
                began = self.clock()
                sized = sizing_fn()
                clip, refusal = sized[0], sized[1]
                facts = sized[2] if len(sized) > 2 and isinstance(sized[2], dict) else {}
                self.sizing_facts = dict(facts)
                self.sizing_done_at = self.clock()
                self.balance_source = facts.get("balance_source")
                self.sizing_ms = (self.sizing_done_at - began) * 1000.0
                if refusal or clip is None or not math.isfinite(float(clip)) or float(clip) <= 0:
                    self.error = self.state = str(refusal or "no_clip")
                    return
                self.clip = float(clip)
                self.state = "sized"
                if self.taken:
                    return
                if safety_fn is not None:
                    self._start_safety(safety_fn)
                if getattr(quote_fn, "early_deadline", False):
                    self.result = quote_fn(self.mint, self.clip, on_started=on_started)
                else:
                    if not on_started(self.clock(), 8.0, {}):
                        return
                    self.result = quote_fn(self.mint, self.clip)
                self.done_at = self.clock()
                self.state = "done"
            except Exception as exc:
                self.error = self.state = "error:" + type(exc).__name__
            finally:
                self._done.set()
        self.worker = threading.Thread(target=run, name="evm-early-entry-quote", daemon=True)
        self.worker.start()

    def note_invalidation(self, chain, generation, stamp):
        """Main caller's existing spend invalidation; never authorizes reuse of stale balances."""
        self.own_invalidation = (str(chain), generation, stamp)

    def _start_safety(self, safety_fn):
        """Token safety beside the quote, with this clip, on its own thread (HUSH: it started at
        ~el 327 in the leg and ran serial inside). Its deadline is its own, from now."""
        safety = EarlySafety(self.mint, self.clip, time.monotonic() + self.safety_wait_s)

        def go():
            began = time.monotonic()
            try:
                safety.result = safety_fn(self.mint, self.clip, safety.deadline, safety.timings)
            except Exception as exc:              # fail closed: the leg re-runs anything not a pass
                safety.result = (False, "safety_exception: %s" % type(exc).__name__)
            finally:
                safety.done_at = time.monotonic()
                safety.duration_ms = (safety.done_at - began) * 1000.0
                safety.done.set()
        self.safety = safety
        threading.Thread(target=go, name="evm-early-safety", daemon=True).start()

    def safety_for(self, mint, usd):
        """The early safety for exactly this mint and usd, or None."""
        safety = self.safety
        try:
            if (safety is None or str(mint).lower() != safety.mint
                    or abs(float(usd) - safety.usd) > 1e-9):
                return None
        except (TypeError, ValueError):
            return None
        return safety

    def take(self, mint, clip, *, max_age_s=3.0):
        # Only this attempt's executor calls take. The worker publishes through Event and never
        # touches taken. Matching reads finish against their original budget, not a new timeout.
        if self.taken:
            return None
        self.taken = True
        reason = "not_started" if self._launch_token.acquire(blocking=False) else None
        waited = self.clock()
        if reason is not None:
            pass
        elif str(mint).lower() != self.mint:
            reason = "mint_mismatch"
        elif self.clip is None or abs(float(clip) - self.clip) > 1e-9:
            reason = "clip_mismatch"
        elif not self._done.is_set() and self.deadline is not None:
            self._done.wait(max(0.0, self.deadline - self.clock()))
        elif not self._done.is_set():
            reason = "not_started"
        completed = self._done.is_set()
        result, done_at = (self.result, self.done_at) if completed else (None, None)
        if reason is None and not completed:
            # The original budget expired. Preserve unknown/provider truth without launching a
            # second full ladder. Late completion belongs only to this discarded object.
            if self.pending is not None:
                result = dict(self.pending, quote=None, observed_at=None,
                              rungs=({"rung": "early", "hit": False,
                                      "blind_reason": "ladder_deadline"},))
                done_at = self.clock()
            else:
                reason = "not_ready"
        if reason is not None:
            pass
        elif completed and self.error:
            reason = self.error
        elif self.clip is None or abs(float(clip) - self.clip) > 1e-9:
            reason = "clip_mismatch"
        elif done_at is None or not 0 <= self.clock() - done_at <= max_age_s:
            reason = "stale"
        elif not isinstance(result, dict):
            reason = "no_quote"
        marker = getattr(self.trace, "mark_substage", None)
        if marker is not None:
            try:
                marker("evm.early_quote", used=reason is None, reason=reason, clip=self.clip,
                       wait_ms=(self.clock() - waited) * 1000,
                       age_ms=(None if done_at is None else (self.clock() - done_at) * 1000),
                       worker=self.state, sizing_ms=self.sizing_ms,
                       balance_source=self.balance_source,
                       sizing_facts=dict(self.sizing_facts) if completed else None,
                       sizing_done_at=self.sizing_done_at if completed else None,
                       own_invalidation_chain=(self.own_invalidation[0] if self.own_invalidation else None),
                       own_invalidation_generation=(self.own_invalidation[1] if self.own_invalidation else None),
                       own_invalidation_at=(self.own_invalidation[2] if self.own_invalidation else None),
                       early_safety=self.safety is not None)
            except Exception:
                pass
        return result if reason is None else None
