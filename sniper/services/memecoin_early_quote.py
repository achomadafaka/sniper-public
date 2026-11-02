"""The J7 Solana early entry quote (design section 12, option 4; Alpha GO 2026-09-28).

WHY. On J7 Solana fills, decision->quote is about 643 ms p50: market_data 234 ms (the DexScreener
GET), the reservation stage 288 ms, safety 30 ms, then the Jupiter quote 91 ms (p90 502). The quote
needs the clip, the clip needs the reservation, so today it runs last. This starts the SAME quote
call at decision start, for the clip the reservation is expected to approve, so it runs beside the
GET and the reservation stage and is usually done when the executor reaches its quote step.

HOW IT STAYS HARMLESS.
- A cheap pre-screen runs on the decision thread before anything starts (manual, quiet hours, the J7
  event's mcap under the wallet's ceiling). The thread then runs the SAME risk envelope
  reserve_entry runs (build_memecoin_risk_context + memecoin_entry_allowed, on its own read-only
  connection) at the market regime scale. It quotes only when that would allow the entry, for
  exactly the clip that would be approved.
- The executor uses the early quote only when the reservation approved the SAME clip, the quote is
  younger than MEMECOIN_ENTRY_EARLY_QUOTE_MAX_AGE_S, and it came back. Every other case discards it
  and quotes exactly as before. Every gate after the quote runs unchanged on whichever quote it
  gets.
- The retry path (max_accounts) never uses it. Nothing here ever raises into the entry.
- A REFUSAL IS CONFIRMED FRESH (Alpha doctrine, 2026-09-28). When the early quote would refuse the
  entry at the chase or the post-peak check, the executor re-runs the entry once on a fresh quote,
  and the refusal stands only if the fresh quote confirms it. A pass uses the early quote (the
  signed min_out still bounds the drift).

COST. Alpha accepted about +87% entry quotes (about 1.3 an hour): the pre-screen passes coins that
a later gate refuses, and a discarded quote is spent for nothing. The heartbeat's `early_quote`
section counts skipped, started, quoted, sizing_refused, used and discarded by reason, and
wasted_quotes = quoted - used.

THE NATIVE SHADOW. The shadow compares a native read with the Jupiter quote taken beside it. When
the early quote runs, the shadow starts beside IT on this thread, and the executor starts none if
it uses that quote; a discarded early quote leaves the executor's own shadow in place.

MEMECOIN_ENTRY_EARLY_QUOTE: 1 (default) on, 0 = today's path.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

_STATS_LOCK = threading.Lock()
# skipped: the cheap pre-screen refused (no thread, no quote). started: threads. quoted: quote calls
# made (the added provider load). sizing_refused: the risk envelope refused on the thread (no
# quote). used / discarded: what the executor did with it. A started quote whose entry stopped
# before the executor is neither used nor discarded ("untaken" in stats()).
# refusal_requoted / refusal_agreed: a used early quote that the chase or post-peak check would
# have refused, re-run on a fresh quote, and how often the fresh quote confirmed the refusal.
_STATS = {"skipped": {}, "started": 0, "quoted": 0, "sizing_refused": {}, "used": 0,
          "discarded": {}, "refusal_requoted": {}, "refusal_agreed": {}}


def _count(bucket, reason=None):
    with _STATS_LOCK:
        if reason is None:
            _STATS[bucket] += 1
        else:
            _STATS[bucket][reason] = _STATS[bucket].get(reason, 0) + 1


def note_skip(reason):
    _count("skipped", str(reason))


def note_refusal_requote(kind, agreed):
    _count("refusal_requoted", str(kind))
    if agreed:
        _count("refusal_agreed", str(kind))


def stats():
    """A copy of the process counters, for the heartbeat. wasted_quotes = quoted - used."""
    with _STATS_LOCK:
        out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _STATS.items()}
    out["wasted_quotes"] = out["quoted"] - out["used"]
    out["untaken"] = (out["started"] - out["used"] - sum(out["discarded"].values()))
    return out


def enabled():
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_ENTRY_EARLY_QUOTE", True))


def configured_max_age_s():
    from sniper.settings import memecoin as cfg
    return float(getattr(cfg, "MEMECOIN_ENTRY_EARLY_QUOTE_MAX_AGE_S", 1.5) or 0.0)


def onchain_unknown_mcap_allowed(ev):
    """C1 (2026-10-01): an on-chain lane event never carries a market cap, so with
    MEMECOIN_ENTRY_EARLY_QUOTE_ONCHAIN on its unknown mcap passes the pre-screen. The scope is the
    signal source, in code: a J7 event with an unknown mcap still skips."""
    from sniper.settings import memecoin as cfg
    return (getattr(ev, "signal_source", None) == "onchain_watch"
            and bool(getattr(cfg, "MEMECOIN_ENTRY_EARLY_QUOTE_ONCHAIN", False)))


def cheap_prescreen(ev, *, quiet_hours, mcap_ceiling):
    """A refusal reason from the event alone (no I/O), or None. Unknown mcap refuses the EARLY
    quote only: the entry itself proceeds exactly as before. An on-chain lane event's unknown mcap
    passes when C1 is on (onchain_unknown_mcap_allowed); the mcap ceiling still refuses its entry
    after the GET."""
    from sniper.services.memecoin_origin import bypasses_selection
    if bypasses_selection(getattr(ev, "signal_source", None)) or getattr(ev, "override", False):
        return "manual"
    if quiet_hours:
        return "quiet_hours"
    try:
        mcap = float(getattr(ev, "their_market_cap", None))
    except (TypeError, ValueError):
        return None if onchain_unknown_mcap_allowed(ev) else "mcap_unknown"
    if not mcap > 0:
        return None if onchain_unknown_mcap_allowed(ev) else "mcap_unknown"
    if mcap_ceiling and mcap > float(mcap_ceiling):
        return "mcap_over_ceiling"
    return None


class EarlyEntryQuote:
    """One decision's early quote, on its own daemon thread.

    `sizing_fn()` -> (clip_usd, None) when the risk envelope would allow the entry, else (None,
    reason); it runs on this object's thread. `quote_fn(mint, clip)` is the executor's own entry
    quote seam. `shadow_fn(mint, clip)`, when given, starts the native quote shadow beside THIS
    quote (the executor then starts none), so the shadow still compares two reads taken together.
    `take(clip)` hands the quote to the executor or discards it (counted). Never raises.

    C1 CLIP_MISMATCH (Alpha 2026-10-07): `sizing_fn` may return a third element {"alt_clips_fn": fn}
    (or {"alt_clips": [...]}), the clips a coin-level regime scale known only after the GET would
    reserve instead (15 of 17 discards were exactly 0.75 x). One more thread sizes and quotes them
    beside the first quote, which never waits for it; `take` hands over whichever clip the
    reservation approved, under the same freshness rules."""

    def __init__(self, *, mint, quote_fn, sizing_fn, trace=None, shadow_fn=None,
                 clock=time.monotonic, start=True):
        self.mint, self.quote_fn, self.sizing_fn, self.trace = mint, quote_fn, sizing_fn, trace
        self.shadow_fn = shadow_fn
        self.clock = clock
        self.clip = self.quote = self.done_at = self.observed_at = self.refusal = None
        self._alts, self._alt_thread = {}, None  # alt clip -> its result; the thread sizing+quoting them
        # take() waits on the worker that owns the requested clip, never on the other (review C07):
        # the primary's sizing decision, the alternates' publication, and each alternate's quote.
        self._sized, self._alts_published = threading.Event(), threading.Event()
        self._taken = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self.run, name="early-entry-quote", daemon=True)
        _count("started")
        if start:
            self._thread.start()

    def run(self):
        try:
            sized = self.sizing_fn()
            clip, refusal = sized[0], sized[1]
            facts = sized[2] if len(sized) > 2 and isinstance(sized[2], dict) else {}
            if refusal is not None or clip is None or not clip > 0:
                self.refusal = str(refusal or "no_clip")
                _count("sizing_refused", self.refusal)
                self._sized.set()
                return
            alt_fn = facts.get("alt_clips_fn")
            if alt_fn is None and facts.get("alt_clips"):
                listed = list(facts["alt_clips"])
                alt_fn = lambda: listed  # noqa: E731
            # The alternates' thread exists BEFORE self.clip is set: take() treats a known clip with no
            # alternates pending as final, so it must never see the clip first.
            if alt_fn is not None:
                self._alt_thread = threading.Thread(target=self._run_alts, args=(alt_fn, float(clip)),
                                                    name="early-entry-quote-alt", daemon=True)
                self._alt_thread.start()
            self.clip = float(clip)
            self._sized.set()
            shadow = None
            if self.shadow_fn is not None:
                try:
                    shadow = self.shadow_fn(self.mint, self.clip)
                except Exception:
                    logger.warning("early quote: native shadow did not start mint=%s", self.mint,
                                   exc_info=True)
            _count("quoted")
            quote = self.quote_fn(self.mint, self.clip)
            self.done_at = self.clock()
            from sniper.execution.entry_attribution import trace_now
            self.observed_at = trace_now(self.trace)
            self.quote = quote
            if shadow is not None:
                try:
                    shadow.offer_jupiter(quote)
                except Exception:
                    logger.warning("early quote: native shadow offer failed mint=%s", self.mint,
                                   exc_info=True)
        except Exception as exc:
            self.refusal = "error:%s" % type(exc).__name__
            logger.warning("early entry quote failed mint=%s", self.mint, exc_info=True)
        finally:
            self._sized.set()

    def _run_alts(self, alt_fn, clip):
        """The alternates' own thread: size them, then quote each. Never raises; a failed sizing
        leaves no alternate (the reserved clip then discards as clip_mismatch, as before)."""
        try:
            alts = [float(a) for a in (alt_fn() or ()) if a and abs(float(a) - clip) > 1e-9]
        except Exception:
            logger.warning("early entry quote: scaled clip sizing failed mint=%s", self.mint, exc_info=True)
            self._alts_published.set()
            return
        # Published whole, once (Gamma NIT): take() may iterate it while this thread still quotes.
        self._alts = {alt: {"quote": None, "done_at": None, "observed_at": None, "error": None,
                            "done": threading.Event()}
                      for alt in alts[:2]}
        self._alts_published.set()

        def quote_one(alt, result):
            _count("quoted")
            try:
                quote = self.quote_fn(self.mint, alt)
                result["done_at"] = self.clock()
                from sniper.execution.entry_attribution import trace_now
                result["observed_at"] = trace_now(self.trace)
                result["quote"] = quote
            except Exception as exc:
                result["error"] = "error:%s" % type(exc).__name__
                logger.warning("early entry quote (scaled clip) failed mint=%s", self.mint, exc_info=True)
            finally:
                result["done"].set()
        # CONCURRENTLY (2026-10-08): with two alternates (the above-nano clip and the regime clip),
        # one after another would add a quote round-trip to the second. This thread joins both, so
        # take()'s join on it still covers every alternate.
        workers = [threading.Thread(target=quote_one, args=item, name="early-entry-quote-alt",
                                    daemon=True) for item in list(self._alts.items())]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

    def _alt_for(self, clip):
        return next((alt for alt in self._alts if abs(float(clip) - alt) <= 1e-9), None)

    def take(self, clip, *, max_wait_s=2.0, max_age_s=None):
        """The quote for `clip`, or None (discarded, counted). Waits up to `max_wait_s` IN TOTAL for
        the worker that owns `clip`: the primary for the primary clip, that alternate for an
        alternate (review C07, 2026-10-08: a ready alternate used to wait on, and die with, a
        stalled primary). It is the call the executor would otherwise start now, begun earlier."""
        self._wait_ms = None
        try:
            with self._lock:
                if self._taken:
                    return None
                self._taken = True
            deadline = time.monotonic() + max(0.0, float(max_wait_s))

            def left():
                return max(0.0, deadline - time.monotonic())
            waited = self.clock()
            if self.clip is None and self.refusal is None and self._thread.ident is not None:
                self._sized.wait(left())                     # the primary's sizing decides the clips
            max_age_s = max_age_s if max_age_s is not None else configured_max_age_s()
            reason = alt = None
            quote, done_at = self.quote, self.done_at
            if self.refusal is not None and self.clip is None:
                reason = "error" if self.refusal.startswith("error:") else "sizing_refused"
            elif self.clip is None:
                reason = "not_ready"                         # still sizing when the budget ran out
            elif abs(float(clip) - self.clip) <= 1e-9:
                if self._thread.ident is not None:
                    self._thread.join(left())
                quote, done_at = self.quote, self.done_at
                if self._thread.is_alive():
                    reason = "not_ready"
                elif self.refusal is not None:
                    reason = "error" if self.refusal.startswith("error:") else "sizing_refused"
            elif self._alt_thread is None:
                reason = "clip_mismatch"                     # no alternates: nothing else to wait for
            else:
                self._alts_published.wait(left())
                alt = self._alt_for(clip)
                result = self._alts.get(alt) if alt is not None else None
                if result is None:
                    reason = "clip_mismatch" if self._alts_published.is_set() else "not_ready"
                else:
                    result["done"].wait(left())
                    quote, done_at = result["quote"], result["done_at"]
                    if result["error"] is not None:
                        reason = "error"
                    elif done_at is None:
                        reason = "not_ready"
            self._wait_ms = (self.clock() - waited) * 1000.0
            if reason is not None:
                pass
            elif not isinstance(quote, dict) or not quote:
                reason = "no_quote"
            elif self.clip is None:
                reason = "clip_mismatch"
            elif done_at is None or self.clock() - done_at > max_age_s:
                reason = "stale"
        except Exception:
            logger.warning("early quote take failed mint=%s", self.mint, exc_info=True)
            reason = "error"
        if reason is not None:
            _count("discarded", reason)
            self._mark(False, reason, requested_clip=clip)
            return None
        _count("used")
        self._mark(True, None, alt=alt, done_at=done_at, requested_clip=clip)
        return quote

    def _mark(self, used, reason, alt=None, done_at=None, requested_clip=None):
        marker = getattr(self.trace, "mark_substage", None) if self.trace is not None else None
        if marker is None:
            return
        try:
            # wait_ms: how long the executor blocked here for the quote (C1's quote_wait_ms).
            done_at = self.done_at if done_at is None else done_at
            marker("early_quote", used=used, reason=reason, clip=self.clip if alt is None else alt,
                   alt=alt is not None, alt_clips=sorted(self._alts) or None,
                   requested_clip=requested_clip,
                   candidate_clips=sorted(set(self._alts) | ({self.clip} if self.clip is not None else set())),
                   size_miss=reason == "clip_mismatch",
                   age_ms=((self.clock() - done_at) * 1000.0 if done_at else None),
                   wait_ms=getattr(self, "_wait_ms", None))
        except Exception:
            pass
