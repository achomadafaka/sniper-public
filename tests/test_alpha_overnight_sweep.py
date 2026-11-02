"""Alpha's overnight sweep must not page on a slot-lane policy skip that ends as
native_fast_lane_unavailable under FAST_LANE_ONLY (Degen daff897f, 2026-10-07)."""
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "alpha_overnight_sweep", Path(__file__).resolve().parents[1] / "scripts" / "alpha_overnight_sweep.py")
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)


def _trace(reason):
    return json.dumps({"substage_events": [{"name": "slot_lane_skip", "reason": reason}]})


def test_policy_skip_is_not_reported():
    assert sweep._slot_lane_policy_skip(_trace("source buy $121 (0.135%) under mcap floor 0.250% ($224)"))


def test_mechanical_skip_is_still_reported():
    assert not sweep._slot_lane_policy_skip(_trace("missing_receipt_proof"))
    assert not sweep._slot_lane_policy_skip(None)
    assert not sweep._slot_lane_policy_skip("not json")


def test_state_watermark_lives_in_data():
    assert sweep.STATE.endswith("/data/alpha_overnight_sweep.state.json")
