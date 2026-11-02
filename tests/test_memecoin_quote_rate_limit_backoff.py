"""A fixed 429 cooldown shorter than the provider's ban window cannot escape it.

2026-08-14. Jupiter rate-limited this host on EVERY exact quote, including a trivial SOL/USDC probe.
The funded exit will not sign without an exact whole-balance quote, so three positions could not be
sold at all — 223 QUOTE-INFRA failures, `refused=77`.

We were sustaining it ourselves. `penalize()` held the bucket a flat 15 s, the ban outlasted that,
so the next sweep spent straight back into a closed door and re-armed the ban. The loop only broke
when the request rate dropped and the host went quiet; the provider answered again within about a
minute.

A repeated 429 is the provider saying the last wait was too short. Back off geometrically, and cap
it so a real outage still reaches the exit path promptly once it clears.
"""
from sniper.services.memecoin_quote_demand import QuoteBudget


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


def _budget(clock, per_minute=8.0):
    return QuoteBudget(per_minute=per_minute, clock=clock)


def test_first_penalty_is_the_configured_cooldown():
    clock = Clock()
    b = _budget(clock)
    b.penalize(15.0)
    clock.advance(14.0)
    assert b.take() is False, "still inside the first hold"
    clock.advance(2.0)
    assert b.take() is True


def test_repeated_penalty_escalates():
    """The second 429 arrives BECAUSE the first wait was too short. Wait longer."""
    clock = Clock()
    b = _budget(clock)
    b.penalize(15.0)
    clock.advance(16.0)
    b.penalize(15.0)                     # throttled again right after the hold expired
    clock.advance(16.0)
    assert b.take() is False, "second hold must outlast the first"
    clock.advance(20.0)
    assert b.take() is True


def test_escalation_is_capped():
    """A capped hold keeps a cleared outage from waiting hours to notice."""
    clock = Clock()
    b = _budget(clock)
    for _ in range(12):
        b.penalize(15.0)
        clock.advance(1.0)
    clock.advance(15.0 * 8 + 5.0)
    assert b.take() is True, "hold must not grow without bound"


def test_a_clean_run_resets_the_escalation():
    """A throttle episode an hour later is a NEW episode, not a continuation of the old one."""
    clock = Clock()
    b = _budget(clock)
    b.penalize(15.0)
    clock.advance(16.0)
    b.penalize(15.0)                     # escalated
    clock.advance(3600.0)                # long quiet period; provider healthy again
    b.penalize(15.0)                     # a fresh, unrelated episode
    clock.advance(16.0)
    assert b.take() is True, "a new episode must start at the base cooldown"


def test_a_successful_quote_ends_the_episode():
    """The streak counts CONSECUTIVE 429s. A provider that answers has recovered — stop punishing it.

    Live on 2026-08-14: the cap held the budget 480 s while Jupiter was already returning 200, and
    an exact-quote blackout blocks entries as well as exits.
    """
    clock = Clock()
    b = _budget(clock)
    for _ in range(6):                   # drive the escalation to its cap
        b.penalize(15.0)
        clock.advance(1.0)
    b.relieve()                          # a quote came back
    clock.advance(200.0)
    b.penalize(15.0)                     # a fresh throttle, later
    clock.advance(16.0)
    assert b.take() is True, "after a success the next episode restarts at the base cooldown"


def test_relieve_is_safe_before_any_penalty():
    clock = Clock()
    b = _budget(clock)
    b.relieve()
    assert b.take() is True


def test_the_rail_bucket_yields_but_never_escalates():
    """The rail bucket was exempt from the 429 penalty, and that exemption sustained the ban.

    A rails-blind position draws the RAIL bucket, so the blinder it got the harder it asked — 50
    requests/min at a provider already refusing, which held the ban open, so no quote could land, so
    it stayed blind. Position 37108 rode that loop for over an hour on 2026-08-14.

    It must yield, or the loop never breaks. It must not ESCALATE, or an 8-minute hold starves the
    live take-profit this bucket exists to protect.
    """
    clock = Clock()
    rail = _budget(clock)
    rail.penalize(15.0, escalate=False)
    clock.advance(14.0)
    assert rail.take() is False, "the rail bucket must yield to a provider that said stop"
    clock.advance(2.0)
    assert rail.take() is True, "and it must come back at the BASE cooldown"

    for _ in range(6):                   # a sustained outage must not compound the rail hold
        rail.penalize(15.0, escalate=False)
        clock.advance(1.0)
    clock.advance(16.0)
    assert rail.take() is True, "the rail hold must never escalate"


def test_penalize_zero_is_a_noop():
    clock = Clock()
    b = _budget(clock)
    b.penalize(0.0)
    assert b.take() is True
