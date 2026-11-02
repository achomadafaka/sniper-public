"""The fomo rate guard's steady pacing floor.

Without a floor the guard has only two states, flat out and punished: `gap_s` starts at 0, fires
until fomo answers 429, escalates to a 16 s gap, decays back to 0, and hammers again. Measured
2026-09-06: 64 throttle episodes, 16,918 seconds elevated, 82 engagements. The fast lane polls all
64 armed wallets across 4 workers with a 1 s sleep, so at an escalated gap ONE sweep of the roster
takes seventeen minutes — which is what the armed-wallet REST lane's 21 s mean lag and 191 s worst
case actually were. A steady 0.25 s gap sweeps the same 64 wallets in 16 s, so pacing DOWN makes
the lane faster.
"""


def _guard():
    from sniper.feeds import fomo_feed

    return type(fomo_feed._rate_guard)()


def _age_the_ladder(guard):
    """The decay ladder waits 120 s from the last state change; age it so `on_success` may act."""
    guard._last_escalation = 0.0
    guard._last_decay = 0.0


def test_the_gap_never_decays_below_the_configured_floor(monkeypatch):
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_FOMO_BASE_GAP_S", 0.25, raising=False)
    guard = _guard()
    assert guard.gap_s == 0.25, "a fresh guard starts at the floor, not at zero"

    guard.on_rate_limited()
    assert guard.gap_s >= 1.0

    for _ in range(12):
        _age_the_ladder(guard)
        guard.on_success()
    assert guard.gap_s == 0.25, "decayed below the floor"


def test_a_zero_floor_reproduces_the_previous_behaviour(monkeypatch):
    """0 is the default and must be byte-identical to the pre-2026-09-06 guard."""
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_FOMO_BASE_GAP_S", 0.0, raising=False)
    guard = _guard()
    assert guard.gap_s == 0.0

    guard.on_rate_limited()
    for _ in range(12):
        _age_the_ladder(guard)
        guard.on_success()
    assert guard.gap_s == 0.0


def test_a_malformed_floor_falls_back_to_zero_rather_than_raising(monkeypatch):
    """A bad value must not take the feed down; unknown pacing is no pacing, not a crash."""
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_FOMO_BASE_GAP_S", "not-a-number", raising=False)
    assert _guard().gap_s == 0.0


def test_a_negative_floor_is_clamped_to_zero(monkeypatch):
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_FOMO_BASE_GAP_S", -5.0, raising=False)
    assert _guard().gap_s == 0.0


def test_a_guard_at_rest_never_decays_or_logs_a_recovery(monkeypatch, caplog):
    """With a floor configured, `gap_s > 0` is true at rest.

    The first cut of this change decayed a guard that had never escalated and logged
    "fomo throttle recovered ... 233563s elevated, peak gap 0.0s, 0 escalations" every 120 s —
    noise in the exact log used to measure throttling.
    """
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_FOMO_BASE_GAP_S", 0.25, raising=False)
    guard = _guard()
    with caplog.at_level("WARNING"):
        for _ in range(12):
            _age_the_ladder(guard)
            guard.on_success()
    assert guard.gap_s == 0.25
    assert guard.decays == 0, "decayed while at rest"
    assert not [r for r in caplog.records if "throttle recovered" in r.getMessage()]


def test_a_real_escalation_still_reports_its_recovery(monkeypatch, caplog):
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_FOMO_BASE_GAP_S", 0.25, raising=False)
    guard = _guard()
    guard.on_rate_limited()
    with caplog.at_level("WARNING"):
        for _ in range(12):
            _age_the_ladder(guard)
            guard.on_success()
    assert guard.gap_s == 0.25
    assert [r for r in caplog.records if "throttle recovered" in r.getMessage()]
