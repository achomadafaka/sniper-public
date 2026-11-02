"""Entry mcap FLOOR (2026-08-25, operator).

The floor is a COST decision, not a performance one. Realized PnL cannot support a floor: sub-15k
reads -16.1% cutting at 08-19 and +1.7% cutting at 08-21, nine trades apart. Entry cost is what is
stable -- median route/display +36.7% under 30k against +9.8% over 250k.

The cases that matter most are the ones where the floor must NOT fire: an unresolved mcap has to
stay `mcap_unresolved`, because turning an unknown into a floor refusal would report a reason we
did not measure."""
from sniper.services.memecoin_real_runner import mcap_entry_decision as d


def test_floor_off_by_default_leaves_behaviour_unchanged():
    assert d(50_000, None, 500_000) == (True, 50_000, "ok")
    assert d(0, None, 0) == (True, None, "no_ceiling")


def test_floor_refuses_under_and_admits_at_the_boundary():
    assert d(74_999, None, 500_000, 75_000)[0] is False
    assert d(74_999, None, 500_000, 75_000)[2] == "under_floor"
    assert d(75_000, None, 500_000, 75_000) == (True, 75_000, "ok")
    assert d(200_000, None, 500_000, 75_000) == (True, 200_000, "ok")


def test_ceiling_is_checked_before_the_floor():
    assert d(900_000, None, 500_000, 75_000)[2] == "over_ceiling"


def test_unresolved_mcap_never_becomes_a_floor_refusal():
    assert d(None, None, 500_000, 75_000) == (False, None, "mcap_unresolved")
    assert d(0, 0, 500_000, 75_000) == (False, None, "mcap_unresolved")


def test_floor_applies_to_a_resolved_value_like_the_ceiling_does():
    assert d(None, 10_000, 500_000, 75_000)[2] == "under_floor"
    assert d(None, 120_000, 500_000, 75_000) == (True, 120_000, "ok")


def test_floor_works_with_no_ceiling():
    assert d(10_000, None, 0, 75_000)[2] == "under_floor"
    assert d(80_000, None, 0, 75_000) == (True, 80_000, "ok")


def test_absurd_mcap_is_unresolvable_not_a_ceiling_breach():
    """A garbage reading must fail closed as UNRESOLVABLE even with the ceiling OFF, because
    downstream banding and policy selection read this value. The over-ceiling population's max is
    $1.53 TRILLION."""
    import sniper.settings.memecoin as cfg
    prev = getattr(cfg, "MEMECOIN_REAL_ABSURD_MCAP_USD", 0)
    try:
        cfg.MEMECOIN_REAL_ABSURD_MCAP_USD = 10_000_000_000.0
        # ceiling OFF and floor OFF — the guard must still fire
        assert d(1.53e12, None, 0, 0) == (False, None, "mcap_unresolvable")
        # applies to a RESOLVED value too
        assert d(None, 5e10, 0, 0)[2] == "mcap_unresolvable"
        # a real coin is untouched (both bounds off -> the existing no_ceiling reason)
        assert d(9_000_000, None, 0, 0) == (True, 9_000_000, "no_ceiling")
        assert d(9_000_000, None, 500_000_000, 0) == (True, 9_000_000, "ok")
        # and it is not a ceiling breach: the reason must be distinguishable
        assert d(1.53e12, None, 500_000, 0)[2] == "mcap_unresolvable"
    finally:
        cfg.MEMECOIN_REAL_ABSURD_MCAP_USD = prev


def test_guard_off_by_default_changes_nothing():
    import sniper.settings.memecoin as cfg
    prev = getattr(cfg, "MEMECOIN_REAL_ABSURD_MCAP_USD", 0)
    try:
        cfg.MEMECOIN_REAL_ABSURD_MCAP_USD = 0
        assert d(1.53e12, None, 0, 0) == (True, 1.53e12, "no_ceiling")
    finally:
        cfg.MEMECOIN_REAL_ABSURD_MCAP_USD = prev
