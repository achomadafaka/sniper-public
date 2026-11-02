"""Tape reader first-touch labels: barrier-before-gap/completion is resolved, gap/completion-before-barrier is censored (amendment 1)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("trs", Path(__file__).resolve().parents[1] / "scripts" / "research" / "tape_reader_samples.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)
SL, T = [1, 2, 3, 4], [10, 11, 12, 13]


def test_priority():
    assert S.touch([1.0, 1.05, 1.25, 1.3], SL, 1.0, 0.2, 0.1, T, None, [5, 5, 5, 0]) == 1          # early winner kept despite later completion
    assert S.touch([1.0, 1.05, 1.1, 1.3], SL, 1.0, 0.2, 0.1, T, None, [5, 5, 0, 5]) == -2          # completion before any barrier
    assert S.touch([1.0, 1.05, 1.1, 1.3], SL, 1.0, 0.2, 0.1, T, 12.5, None) == -2                  # silence before the barrier
    assert S.touch([1.0, 1.05, 1.25, 1.3], SL, 1.0, 0.2, 0.1, T, 12.5, None) == 1                  # barrier before the silence
    assert S.touch([1.3, 0.8], [1, 1], 1.0, 0.2, 0.1, [1, 2], None, None) == -1                    # same slot both barriers: ambiguous
    assert S.touch([1.0, 1.05], [1, 2], 1.0, 0.2, 0.1, [1, 2], None, None) == 0                    # fully observed, neither
    assert S.touch([1.0, 1.05], [1, 2], 1.0, 0.2, 0.1, [1, 2], 12.5, None) == -2                   # silence began, no barrier seen


def test_three_way():
    assert S.touch([1.0, 1.05], [1, 2], 1.0, 0.2, 0.2, [1, 2], None, None, three=True) == 2        # neither is its own class
    assert S.touch([1.0, 0.75], [1, 2], 1.0, 0.2, 0.2, [1, 2], None, None, three=True) == 0        # down first
    assert S.touch([1.0, 1.25], [1, 2], 1.0, 0.2, 0.2, [1, 2], None, None, three=True) == 1        # up first
    assert S.touch([1.0, 1.05], [1, 2], 1.0, 0.2, 0.2, [1, 2], 12.5, None, three=True) == -2       # silence still censors


def test_censor_inside_a_slot_group_truncates_before_aggregation():                       # eCat witnesses
    assert S.touch([1.0, 1.3], [1, 1], 1.0, 0.2, 0.1, [1, 10], 5, None) == -2          # silence begins at 5, the winner row is at 10 in the same slot
    assert S.touch([1.0, 1.3], [1, 1], 1.0, 0.2, 0.1, [1, 2], None, [5, 0]) == -2      # completion row precedes the winner in the same slot
    assert S.touch([1.3, 1.0], [1, 1], 1.0, 0.2, 0.1, [1, 10], 5, None) == 1           # winner row before the silence in the same group stays resolved
    assert S.touch([1.0, 1.3], [1, 2], 1.0, 0.2, 0.1, [1, 10], 5, None) == -2          # later slot after the silence


def test_missing_fee_after_an_early_winner_keeps_the_winner():
    assert S.touch([1.25], [1], 1.0, 0.2, 0.1, [1], 7.0, None) == 1                         # the early winner precedes the censor time 7 (what labels() now passes)
