"""Desk pre-quote while JUDGE thinks (Alpha GO 2026-10-23, option A).

WHY. A desk ENTER spends 4.8-7.1 s in the JUDGE call and then 124-346 ms quoting after the verdict.
The mint is known when the call starts, so the runner keeps a fresh Jupiter entry quote for it at the
desk clip during the call, and the executor takes it like any early quote (memecoin_early_quote).

LIMITS. The judge worker writes a hint row (desk_prequote_hints) at call start that expires at the
call deadline. This thread holds at most MEMECOIN_DESK_MAX_CONCURRENT hints, re-quotes each when its
quote is older than REFRESH_SHARE x MEMECOIN_ENTRY_EARLY_QUOTE_MAX_AGE_S, and drops expired ones. A
hint never reserves, signs or authorizes: only the desk intent does. `take(clip)` hands over the
quote only for the same mint, the hinted clip and while it is younger than the early-quote max age;
otherwise None and the executor quotes as before (every gate, the desk cap included, judges it).
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

REFRESH_SHARE = 0.7
ERROR_BACKOFF_S = 2.0       # after a failed quote: never the 0.25 s poll rate while Jupiter is failing
LOG_EVERY_S = 60.0


def enabled():
    """MEMECOIN_DESK_PREQUOTE, default on (improvements ship with a kill knob)."""
    import os
    return os.getenv("MEMECOIN_DESK_PREQUOTE", "1").strip().lower() in ("1", "true", "yes", "on")
POLL_S = 0.25


class HeldQuote:
    """The executor-facing view of one hint's latest quote (take / observed_at, like EarlyEntryQuote)."""

    def __init__(self, mint, clip, quote, done_at, observed_at, max_age_s, clock):
        self.mint, self.clip, self.quote = mint, clip, quote
        self.done_at, self.observed_at, self.max_age_s, self.clock = done_at, observed_at, max_age_s, clock
        self.trace = None

    def take(self, clip):
        if (self.quote is None or abs(float(clip) - self.clip) > 1e-6
                or self.clock() - self.done_at > self.max_age_s):
            return None
        return self.quote


class PrequoteBook:
    def __init__(self, *, quote_fn, clip_usd, max_hints, max_age_s, clock=time.monotonic,
                 wall=time.time, observed_fn=None):
        self.quote_fn, self.clip, self.max_hints = quote_fn, float(clip_usd), int(max_hints)
        self.max_age_s, self.clock, self.wall = float(max_age_s), clock, wall
        self.observed_fn = observed_fn or (lambda: None)
        self._held = {}                    # ticket_id -> (mint, expires_at, HeldQuote | None)
        # STOP REFRESHING != STOP TAKING (Codex/Gamma on 47841126b): a hint cleared when the JUDGE call
        # returns is never re-quoted, but its last quote stays takeable here until it is older than the
        # max age from its OWN completion time (never reset), so a verdict that reaches the executor a
        # few hundred ms later still finds it. Bounded by max_hints.
        self._retained = {}                # ticket_id -> (mint, HeldQuote)
        self._lock = threading.Lock()
        self.stats = {"quotes": 0, "quote_errors": 0, "taken": 0, "missed": 0}

    def sync(self, hints):
        """Keep only live hints (solana, newest max_hints); never more than max_hints held."""
        now = self.wall()
        keep = {}
        for ticket_id, network_id, mint, expires_at in hints[: self.max_hints]:
            if network_id != "solana" or expires_at < now:
                continue
            with self._lock:
                old = self._held.get(ticket_id)
            keep[ticket_id] = (mint, expires_at, old[2] if old and old[0] == mint else None)
        with self._lock:
            for ticket_id, (mint, _exp, held) in self._held.items():
                if ticket_id not in keep and held is not None and held.quote is not None:
                    self._retained[ticket_id] = (mint, held)
            self._retained = dict(sorted(
                ((t, v) for t, v in self._retained.items()
                 if t not in keep and self.clock() - v[1].done_at <= self.max_age_s),
                key=lambda kv: kv[1][1].done_at, reverse=True)[: self.max_hints])
            self._held = keep

    def refresh(self):
        """Re-quote each held hint whose quote is missing or older than REFRESH_SHARE x max age."""
        with self._lock:
            todo = [(t, v) for t, v in self._held.items()
                    if v[2] is None or self.clock() - v[2].done_at >= REFRESH_SHARE * self.max_age_s]
        for ticket_id, (mint, expires_at, _old) in todo:
            try:
                quote = self.quote_fn(mint, self.clip)
                self.stats["quotes"] += 1
                stamp = self.clock()
            except Exception:
                self.stats["quote_errors"] += 1
                logger.warning("desk prequote failed mint=%s", mint, exc_info=True)
                # An error stamp in the future: the hint is not re-quoted for ERROR_BACKOFF_S plus the
                # normal cadence, and take() never returns it (no quote).
                quote, stamp = None, self.clock() + ERROR_BACKOFF_S
            held = HeldQuote(mint, self.clip, quote or None, stamp, self.observed_fn(),
                             self.max_age_s, self.clock)
            with self._lock:
                if ticket_id in self._held and self._held[ticket_id][0] == mint:
                    self._held[ticket_id] = (mint, expires_at, held)

    def for_ticket(self, ticket_id, mint):
        """The held quote for this ticket and mint, or None (counted)."""
        with self._lock:
            got = self._held.get(ticket_id)
            if got is None and ticket_id in self._retained:
                r_mint, r_held = self._retained[ticket_id]
                if self.clock() - r_held.done_at > self.max_age_s:
                    del self._retained[ticket_id]
                else:
                    got = (r_mint, None, r_held)
        if got is None or got[0] != mint or got[2] is None or got[2].quote is None:
            self.stats["missed"] += 1
            return None
        self.stats["taken"] += 1
        return got[2]


_BOOK = {"book": None}


def install(book):
    _BOOK["book"] = book


def stats():
    """The installed book's counters for the heartbeat, or None when no book runs in this process."""
    book = _BOOK["book"]
    return None if book is None else dict(book.stats)


def held_for(ticket_id, mint):
    book = _BOOK["book"]
    return None if book is None or not ticket_id else book.for_ticket(ticket_id, mint)


def run_forever(book, read_hints, stop, *, poll_s=POLL_S, clock=time.monotonic):
    """The runner's pre-quote thread: read live hints, re-quote, sleep. Never raises. A failing pass is
    logged once per distinct error or once per LOG_EVERY_S, not every poll."""
    last = (None, None)
    while not stop.is_set():
        try:
            book.sync(read_hints())
            book.refresh()
        except Exception as exc:
            key = "%s: %s" % (type(exc).__name__, exc)
            book.stats["pass_errors"] = book.stats.get("pass_errors", 0) + 1
            if key != last[0] or clock() - last[1] >= LOG_EVERY_S:
                logger.exception("desk prequote pass failed")
                last = (key, clock())
        stop.wait(poll_s)
