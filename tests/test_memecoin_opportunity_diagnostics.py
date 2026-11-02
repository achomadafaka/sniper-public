from sniper.research.memecoin_opportunity.diagnostics import (
    DIAGNOSTIC_EVALUATOR_VERSION,
    ExecutablePoint,
    evaluate_target_ladder,
    near_miss,
)


def _points():
    return (
        ExecutablePoint(0, "executable", -150, True, False),
        ExecutablePoint(4_000, "executable", 250, True, False),
        ExecutablePoint(9_000, "executable", 650, True, False),
        ExecutablePoint(18_000, "executable", 950, True, False),
        ExecutablePoint(40_000, "executable", 1_300, True, False),
    )


def test_target_ladder_uses_first_passage_and_each_horizon():
    ladder = evaluate_target_ladder(_points())
    by_key = {(row["target_bps"], row["horizon_ms"]): row for row in ladder}
    assert by_key[(200, 5_000)]["result"] == "hit"
    assert by_key[(400, 5_000)]["result"] == "timeout"
    assert by_key[(600, 10_000)]["result"] == "hit"
    assert by_key[(800, 10_000)]["result"] == "timeout"
    assert by_key[(1_200, 60_000)]["result"] == "hit"
    assert by_key[(1_200, 60_000)]["time_to_target_ms"] == 40_000
    assert by_key[(800, 60_000)]["evaluator_version"] == DIAGNOSTIC_EVALUATOR_VERSION


def test_unreached_horizon_is_pending_not_a_manufactured_timeout():
    row = next(
        item
        for item in evaluate_target_ladder((
            ExecutablePoint(60_000, "executable", 100, True, False),
        ))
        if item["target_bps"] == 1_200 and item["horizon_ms"] == 300_000
    )

    assert row["result"] == "pending"
    assert row["terminal_ms"] == 60_000


def test_loss_and_exit_failure_preempt_later_target():
    loss = (
        ExecutablePoint(1_000, "executable", -900, True, False),
        ExecutablePoint(2_000, "executable", 1_500, True, False),
    )
    failed = (
        ExecutablePoint(1_000, "transient_failure", None, True, False),
        ExecutablePoint(2_000, "deterministic_failure", None, True, False),
    )
    assert evaluate_target_ladder(loss)[0]["result"] == "loss"
    assert evaluate_target_ladder(failed)[0]["result"] == "exit_failure"


def test_near_miss_preserves_best_executable_excursion():
    result = near_miss(_points()[:-1], target_bps=1_200)
    assert result == {"best_return_bps": 950, "time_to_best_ms": 18_000}
    assert near_miss(_points(), target_bps=1_200) is None


def test_target_ladder_distinguishes_120_and_300_second_continuation():
    points = (
        ExecutablePoint(60_000, "executable", 100, True, False),
        ExecutablePoint(250_000, "executable", 1_300, True, False),
    )

    ladder = evaluate_target_ladder(points)
    by_key = {(row["target_bps"], row["horizon_ms"]): row for row in ladder}

    assert by_key[(1_200, 120_000)]["result"] == "timeout"
    assert by_key[(1_200, 300_000)]["result"] == "hit"
    assert by_key[(1_200, 300_000)]["time_to_target_ms"] == 250_000


def test_extended_target_does_not_hide_earlier_drawdown():
    points = (
        ExecutablePoint(100_000, "executable", -900, True, False),
        ExecutablePoint(200_000, "executable", 1_300, True, False),
    )

    row = next(
        item
        for item in evaluate_target_ladder(points)
        if item["target_bps"] == 1_200 and item["horizon_ms"] == 300_000
    )

    assert row["result"] == "loss"
    assert row["worst_return_bps"] == -900


def test_ladder_v3_extends_above_the_v2_corner_and_bumps_the_version():
    """v2's EV was monotone to its top cell (TP=1200) in every cut; winners' MFE median
    +4,007 bps says the optimum sits above the grid. v3 adds 2000/3000/5000 and bumps
    the evaluator version so v3 cells can never pool with v2 cells."""
    from sniper.research.memecoin_opportunity.diagnostics import TARGET_BPS

    assert TARGET_BPS == (200, 400, 600, 800, 1_200, 2_000, 3_000, 5_000)
    assert DIAGNOSTIC_EVALUATOR_VERSION == "target-ladder-v3"
