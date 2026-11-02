"""When does a funded position actually need an EXACT route quote? (2026-07-27)

The rails need two different prices at two different rates, and conflating them is what broke the
exact-quote path on 2026-07-27:

  * EVALUATION — "is a rail's condition met, and where is the peak?" This must be FAST, because a
    memecoin's peak can come and go inside one sampling gap. It is cheap: one batched DexScreener
    mid for the whole book. `executable_exit_value` is written EQUAL to `mid` today, so the rails
    already ratchet on exactly this number.
  * CONFIRMATION — "what would this exact bag actually sell for?" This must be EXACT, and it only
    matters when we are about to sell. It is expensive: a per-position Jupiter/V3 route quote.

`real_exit_sweep` decided which positions to quote from a STATIC policy property
(`trail_pct > 0 or derisk_pct > 0`) — "could this position ever need an exact price" — not "does it
need one now". So a position sitting at -5% was quoted exactly as often as one at -59%. At the ~31s
bracket cadence that was merely wasteful. At a 1s tick it is 120 quotes/min against a ~60/min
budget: the provider went down with an ops streak of 165 and no rail could confirm an exit.

This module answers the demand question instead. Quote when it MATTERS:
  1. the mid says we are near (or past) a rail boundary -> confirmation is imminent;
  2. the last exact quote is older than a heartbeat -> keeps unsellable detection alive for a
     position parked mid-band, which is the safety job the old unconditional pre-pass was doing.
Otherwise skip: evaluate and ratchet on the mid, and spend nothing.

Pure functions, no I/O, no clock — the caller supplies the numbers so this is exhaustively testable.
"""
from __future__ import annotations

from dataclasses import dataclass

# Skipping is only safe because a skipped position is NOT recorded as a quote outcome: it never
# enters the miss counters, so a skip can never be mistaken for a provider failure or an unsellable
# token. The caller MUST filter candidates before quoting, not discard results afterwards.
SKIP = "mid_only"

# A demand raised because the position sits AT a price rail, or because the mid is unreadable and we
# must fail open. These decide money: the exact quote is the confirmation a stop / TP / trail waits
# on before it may fire. They are bounded by the number of open funded positions, so they can be
# budgeted separately from the periodic refresh without losing the runaway backstop.
RAIL_REASONS = frozenset({"mid_unreadable", "near_stop", "near_derisk", "near_tp", "near_trail",
                          "always_exact", "custom_cap"})


@dataclass(frozen=True)
class QuoteDemand:
    needed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.needed


def _pct(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def exact_quote_demand(*, mid_pnl_pct, peak_pnl_pct=None, stop_pct=None, derisk_pct=None,
                       trail_pct=None, trail_armed=False, tp_pct=None, last_exact_age_s=None,
                       near_pct=10.0, heartbeat_s=60.0, always=False, custom_cap=False) -> QuoteDemand:
    """Decide whether this position needs an exact route quote on THIS tick.

    `near_pct` is the margin, in percentage points of PnL, at which a rail counts as imminent. It
    buys the confirmation quote BEFORE the rail fires, so the exact price is warm at the moment of
    decision rather than being fetched during it.

    FAIL OPEN: an unreadable mid returns needed=True. Missing data must never silently suppress the
    quote that a stop depends on — the whole point of the gate is to spend less when we are provably
    far from every rail, and "provably" requires a readable price.

    `always` DISABLES the distance test for open funded positions, and it exists because the whole
    premise above has one dependency that is not stated in it: EVERY branch below decides using the
    cheap mid. The gate is only as good as that number, and a mid can be WRONG rather than merely
    coarse. On 2026-09-02 the DexScreener mid for a funded 4663 position froze at one value for 21
    consecutive marks over 24 seconds while the executable price rose 51%. The gate did its job
    perfectly on that input -- the mid read -5.4%, which is 30.4 points from a +25% take-profit --
    and so it declined to buy a quote, while the true price went 17.8 points PAST the take-profit.
    The operator closed the position by hand for +42.8%.

    THAT FAILURE IS SELF-SEALING, which is why the answer is not a better distance test. A frozen
    mid can never appear near a rail, so no exact quote is ever bought, so nothing ever learns the
    mid is frozen. Any gate that reasons from the mid inherits this, including one that tries to
    detect staleness: the same broken input decides whether to distrust itself.

    The 2026-07-27 arithmetic that justified gating has also changed. It was 7 concurrent positions
    at a 1s tick against a ~60/min budget -- 420/min, genuinely unaffordable. MEMECOIN_MAX_CONCURRENT
    is now 3, which is 180/min, and the 4663 endpoint is measured at 4.5 req/s (270/min) with p50
    118ms and 201/201 success. Reading the real price every tick is now affordable, so it is no
    longer a trade against cost; it is just correct.

    The budget in QuoteBudget remains the runaway backstop and is unchanged. `always` raises DEMAND,
    never the ceiling.
    """
    if always:
        # Ahead of the mid read: a position quoting every tick has no use for the mid, and must not
        # be made dependent on a number this mode exists to stop trusting.
        return QuoteDemand(True, "always_exact")
    if custom_cap:
        # Market marks cannot prove a whole-bag sell cap; the existing rail budget bounds refreshes.
        return QuoteDemand(True, "custom_cap")
    mid = _pct(mid_pnl_pct)
    if mid is None:
        return QuoteDemand(True, "mid_unreadable")

    age = _pct(last_exact_age_s)
    hb = _pct(heartbeat_s)
    if age is None or (hb is not None and hb >= 0 and age >= hb):
        # Never quoted, or stale past the heartbeat. This is the unsellable-detection floor.
        return QuoteDemand(True, "heartbeat")

    margin = _pct(near_pct) or 0.0

    stop = _pct(stop_pct)
    if stop is not None and stop > 0 and mid <= -(stop - margin):
        return QuoteDemand(True, "near_stop")

    derisk = _pct(derisk_pct)
    if derisk is not None and derisk > 0 and mid >= (derisk - margin):
        return QuoteDemand(True, "near_derisk")

    # A take-profit is a PRICE rail exactly like the stop, and it was the one rail with no branch
    # here. Under follow-30-50-v1 and tp-only-v1 the de-risk and the trail are both 0, so a
    # position running at the TP matched nothing and refreshed its exact price only on the 60s
    # heartbeat — the stop was watched every tick while the profit rail decided on a minute-old
    # quote (2026-08-13). Checked after the loss rails so an existing config's reason string does
    # not change: those fire first when a position is somehow near both.
    tp = _pct(tp_pct)
    if tp is not None and tp > 0 and mid >= (tp - margin):
        return QuoteDemand(True, "near_tp")

    trail = _pct(trail_pct)
    peak = _pct(peak_pnl_pct)
    if trail_armed and trail is not None and trail > 0 and peak is not None:
        # Trail fires on giveback FROM THE PEAK, so the boundary moves with the peak. Compare in
        # PnL points: a peak of +80 with a 30% trail gives back to roughly +26.
        giveback_boundary = peak - (trail / 100.0) * (100.0 + peak)
        if mid <= giveback_boundary + margin:
            return QuoteDemand(True, "near_trail")

    return QuoteDemand(False, SKIP)


class _Hold:
    """One provider's 429 hold and its escalation streak (the global one, or one group's)."""
    __slots__ = ("until", "streak", "last", "window")

    def __init__(self):
        self.until, self.streak, self.last, self.window = 0.0, 0, None, 0.0


class QuoteBudget:
    """Token bucket over the EXACT-quote provider — a backstop, not the mechanism.

    The demand gate above is what makes the spend small. This exists so that a future logic bug
    cannot hammer a provider again: on 2026-07-27 a single mis-scoped predicate took the exact-quote
    path down for every funded position at once. A hard ceiling turns that class of bug into
    degraded-but-alive instead of an outage.

    Refuses rather than blocks: a refused tick is a skipped quote, never a stalled exit path.
    """

    def __init__(self, *, per_minute=30.0, burst=None, clock=None):
        import time as _t
        self._rate = max(0.0, float(per_minute)) / 60.0
        self._capacity = float(burst if burst is not None else max(1.0, per_minute / 6.0))
        self._tokens = self._capacity
        self._clock = clock or _t.monotonic
        self._last = self._clock()
        self._refused = 0
        # 429 HOLDS PER PROVIDER GROUP (2026-09-28). One global hold let a Base RPC 429 on ONE
        # position (66253: 133 INFRA lines, 263 holds) shut the exact-quote budget for EVERY chain;
        # new Solana fills went quote-blind for 6.5-20.2 s inside the first minute. A 429 now holds
        # the group that answered it. `None` is the GLOBAL hold, used when the failing quote named
        # no provider: an unknown provider keeps today's hold-everything, never "no hold".
        self._global = _Hold()
        self._groups = {}

    # A repeated 429 means the LAST wait was too short, so double it. Capped, because a provider
    # that recovers must reach the exit path promptly — an uncapped hold is its own outage.
    RATE_LIMIT_MAX_MULTIPLIER = 8

    def penalize(self, seconds, *, escalate=True, group=None):
        """Hold the bucket shut for `seconds` because the provider itself said slow down (HTTP 429).

        The token rate is what we CHOSE to spend; a 429 is what the provider will actually serve.
        Without this the next sweep asks again at the same rate, gets throttled again, and each
        refusal reads as another outage sweep — which walked the QUOTE-INFRA ladder from milestone
        1 to milestone 5 in ~25s and paged twice for one throttle episode (2026-08-11 05:27).

        A FIXED hold is not enough when it is shorter than the provider's ban. On 2026-08-14 Jupiter
        outlasted the flat 15 s, so every sweep spent straight back into a closed door and re-armed
        the ban: 223 QUOTE-INFRA failures, and three funded positions that could not be sold at all
        because the exit will not sign without an exact quote. Escalate while the episode continues,
        and start fresh once the provider has been quiet — a throttle an hour later is a new event,
        not a continuation.

        `group` is the provider group that answered 429; each group holds and escalates on its own.
        None holds everything (the provider was not named).
        """
        hold = max(0.0, float(seconds))
        if hold <= 0.0:
            return
        state = self._global if group is None else self._groups.setdefault(group, _Hold())
        now = self._clock()
        if not escalate:
            # The RAIL bucket. It must still yield to a provider that said stop — an unthrottled
            # bucket kept the 2026-08-14 ban alive by itself — but it never escalates, because
            # starving a live take-profit for 8 minutes is the failure this bucket exists to stop.
            state.until = max(state.until, now + hold)
            return
        # Same episode only while the previous hold has not been comfortably outlived.
        if state.last is not None and now - state.last <= state.window:
            state.streak = min(state.streak + 1, self.RATE_LIMIT_MAX_MULTIPLIER.bit_length())
        else:
            state.streak = 1
        state.last = now
        state.window = min(2 ** (state.streak - 1), self.RATE_LIMIT_MAX_MULTIPLIER) * hold * 2.0
        hold *= min(2 ** (state.streak - 1), self.RATE_LIMIT_MAX_MULTIPLIER)
        state.until = max(state.until, now + hold)

    def held(self, group=None):
        """Whether a 429 hold covers `group` now: the global hold always does; a group's own hold
        covers that group; an unknown group (None) is covered by ANY hold, as before groups."""
        now = self._clock()
        if now < self._global.until:
            return True
        if group is None:
            return any(now < state.until for state in self._groups.values())
        state = self._groups.get(group)
        return state is not None and now < state.until

    def take(self, n=1, group=None):
        now = self._clock()
        if self.held(group):
            self._refused += 1
            return False
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        if self._tokens >= n:
            self._tokens -= n
            return True
        self._refused += 1
        return False

    def relieve(self, group=None):
        """A quote SUCCEEDED, so the throttle episode is over — drop back to the base cooldown.

        The streak counts CONSECUTIVE 429s. Without this it only decayed on a timer, so after a
        sustained outage the cap stayed armed against a provider that had already recovered: live on
        2026-08-14 the budget sat on an 8-minute hold while Jupiter was answering 200 again, and an
        8-minute exact-quote blackout blocks entries and exits alike. `group` ends that group's
        episode; None ends the global one."""
        state = self._global if group is None else self._groups.get(group)
        if state is not None:
            state.streak, state.last, state.window = 0, None, 0.0

    @property
    def refused(self) -> int:
        return self._refused

    def snapshot(self) -> dict:
        now = self._clock()
        out = {"tokens": round(self._tokens, 3), "capacity": self._capacity,
               "per_minute": round(self._rate * 60.0, 2), "refused": self._refused,
               "hold_s": round(max(0.0, self._global.until - now), 1)}
        groups = {g: round(state.until - now, 1) for g, state in sorted(self._groups.items())
                  if now < state.until}
        if groups:
            out["group_hold_s"] = groups
        return out
