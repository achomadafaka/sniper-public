"""The round-trip cap refusal is observed afterwards, like every other funded entry gate.

MOAN (Fsx9PN13, solana, mcap $11.2k) was refused at 2026-09-08T21:48 with

    round trip 12.21% over 10.0% cap (sell back $153.63 of $175.00)

and the operator asked whether that was a good refusal. It could not be answered: this gate has
decided 22 funded entries across 19 mints since 2026-08-13 and not one was ever observed
afterwards. Thirteen refusal families are captured; `roundtrip_cap` was not among them, and the
word "roundtrip" did not appear anywhere in the capturer.

That is the wrong combination with what else happened that day: the same gate's RULE changed from a
flat 10% to 0.4 x the position's take-profit. A rule that can move, with no feedback on where it
should sit, is a knob nobody can ever grade.

Cost was measured before adding rather than assumed — 14 distinct mints in 7 days, 4 in the last
24h, so 64-128 extra Jupiter quotes a day against ~3,065 funded quotes/day (a 2-4% rise, with the
current loss rate at 1.4%).
"""
from sniper.services.memecoin_refusal_capture import GATES, should_capture

MOAN = "round trip 12.21% over 10.0% cap (sell back $153.63 of $175.00)"


def family(reason):
    return next((g for g, match in GATES if match(reason)), None)


def test_the_moan_refusal_is_classified_and_captured():
    assert family(MOAN) == "roundtrip_cap"
    assert should_capture("roundtrip_cap") is True


def test_an_unreadable_round_trip_is_a_different_decision_and_is_not_pooled():
    """`round trip unavailable for impact sentinel` is a failure to MEASURE, not a cost over the
    cap. Pooling them would put "we could not see" and "we saw and refused" in one bucket."""
    assert family("round trip unavailable for impact sentinel") != "roundtrip_cap"


def test_the_matcher_reads_the_shape_not_just_the_first_two_words():
    assert family("round trip 9.80% over 10.0% cap (sell back $180.36 of $200.00)") == "roundtrip_cap"
    for near_miss in (
        "round trip measurement skipped",
        "round trip cost recorded for telemetry",
        "the round trip 12.21% over 10.0% cap (sell back $1 of $2)",   # must START with it
    ):
        assert family(near_miss) != "roundtrip_cap", near_miss


def test_it_does_not_collide_with_any_other_family():
    others = {
        "m5_turnover_over_ceiling":
            "m5 volume $110754 OVER ceiling $27161 (m5_turnover_over_ceiling, revival guard)",
        "m5_turnover_under_floor":
            "m5 turnover 0.004 under floor 0.02 (m5_turnover_under_floor)",
        "route_over_mid": "route quote is 3.1x the chart mid (route_over_mid)",
    }
    for expected, reason in others.items():
        assert family(reason) == expected, reason
    assert family(MOAN) not in others


def test_every_family_name_is_unique():
    """Two entries sharing a name would silently merge two gates' evidence."""
    names = [g for g, _ in GATES]
    assert len(names) == len(set(names)), sorted(n for n in names if names.count(n) > 1)


def test_the_evm_twin_is_deliberately_absent():
    """`round_trip_over_budget` has never fired on a funded attempt. An unused matcher is untested
    code; it gets a family when it gets a row."""
    assert family("round_trip_over_budget:-24.8") is None
