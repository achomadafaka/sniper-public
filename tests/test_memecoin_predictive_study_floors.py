"""The predictive study's two admission floors (Codex 2026-07-24).

`MIN_SWAP_BLOCKS` was chosen as a sample-size floor but, under the 23x-too-slow cadence, silently
doubled as a ~8-minute lifetime floor. Correcting the cadence collapsed that to ~20 s. The two
meanings are now separate constants and both must bind.
"""
import importlib.util
import os
import sys

import pandas as pd
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "scripts", "research", "memecoin_tape_predictive_study.py")


@pytest.fixture(scope="module")
def study():
    spec = importlib.util.spec_from_file_location("predictive_study", _PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["predictive_study"] = m
    spec.loader.exec_module(m)
    return m


def _swaps(n_blocks, block_step):
    """n_blocks distinct swap-blocks, `block_step` apart. quote=token0, base=token1.

    Amounts are float64, matching `load_swaps` (it casts the signed TEXT amounts) — not the raw
    strings, which would only prove the fixture agrees with itself."""
    return pd.DataFrame({
        "block_no": [1000 + i * block_step for i in range(n_blocks)],
        "amount0": [100.0] * n_blocks,
        "amount1": [-50.0] * n_blocks,
    })


def test_flash_pool_is_rejected_by_the_lifetime_floor(study):
    """Dense enough (200 swap-blocks) but only ~20 s of trading — exactly the case the corrected
    cadence would otherwise have admitted."""
    span_s = 200 * 1 * study.BLOCK_SECONDS
    assert span_s < study.MIN_ACTIVE_SPAN_S, "fixture must be shorter than the floor"
    out = study.build_pool_block_features(_swaps(200, 1), True, None)
    assert out.empty


def test_sparse_pool_is_rejected_by_the_density_floor(study):
    """Long-lived (hours) but only 50 swap-blocks — the sample-size floor still binds."""
    out = study.build_pool_block_features(_swaps(50, 5000), True, None)
    assert out.empty


def test_pool_meeting_both_floors_is_admitted(study):
    """200 swap-blocks spread over more than 30 minutes of active trading."""
    import math
    need_blocks = study.MIN_ACTIVE_SPAN_S / study.BLOCK_SECONDS
    step = math.ceil(need_blocks / 199) + 1          # span is (n-1)*step, not n*step
    assert step < study.GAP_BLOCKS, "spacing must stay inside one active group"
    assert 199 * step * study.BLOCK_SECONDS > study.MIN_ACTIVE_SPAN_S
    out = study.build_pool_block_features(_swaps(200, step), True, None)
    assert not out.empty


def test_dead_gaps_do_not_count_toward_the_active_span(study):
    """Two short bursts far apart must not pass a 30-minute floor on first-to-last span alone."""
    burst = 100
    a = [1000 + i for i in range(burst)]
    b = [1000 + 10 * study.GAP_BLOCKS + i for i in range(burst)]   # a long dead gap away
    blocks = a + b
    df = pd.DataFrame({"block_no": blocks, "amount0": [100.0] * len(blocks),
                       "amount1": [-50.0] * len(blocks)})
    assert (blocks[-1] - blocks[0]) * study.BLOCK_SECONDS > study.MIN_ACTIVE_SPAN_S
    assert study.build_pool_block_features(df, True, None).empty


def test_underfloor_pool_is_retained_in_without_floor_arm(study):
    out = study.build_pool_block_features(
        _swaps(200, 1), True, None, apply_span_floor=False
    )
    assert not out.empty
    assert out.attrs["clears_span_floor"] is False


def test_sensitivity_run_registers_both_labeled_arms(study):
    assert study._study_arms() == (
        ("with_span_floor", True),
        ("without_span_floor", False),
    )


def test_each_sensitivity_arm_has_a_distinct_output_path(study):
    with_floor = study._arm_output_path("results", "with_span_floor")
    without_floor = study._arm_output_path("results", "without_span_floor")
    assert with_floor.name == "memecoin_tape_predictive_results_with_span_floor_2026-07-24.csv"
    assert without_floor.name == "memecoin_tape_predictive_results_without_span_floor_2026-07-24.csv"
    assert with_floor != without_floor


def test_arm_builder_uses_the_same_source_but_changes_only_span_admission(study):
    import math
    short = _swaps(200, 1).assign(pool="SHORT")
    step = math.ceil((study.MIN_ACTIVE_SPAN_S / study.BLOCK_SECONDS) / 199) + 1
    long = _swaps(200, step).assign(pool="LONG")
    swaps = pd.concat([short, long], ignore_index=True)

    frames = dict(study._iter_arm_frames(
        swaps, pd.DataFrame(), {"SHORT": True, "LONG": True}, ["SHORT", "LONG"]
    ))

    assert tuple(frames) == ("with_span_floor", "without_span_floor")
    assert set(frames["with_span_floor"]["pool"]) == {"LONG"}
    assert set(frames["without_span_floor"]["pool"]) == {"SHORT", "LONG"}
