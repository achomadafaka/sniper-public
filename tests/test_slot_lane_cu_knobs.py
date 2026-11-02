"""The slot lane's compute budget is an env knob; the defaults are the values it always signed.

Leaders order by fee per REQUESTED compute unit, so a tight limit with a higher price buys the
same priority for less (Alpha, 2026-09-27; Beta measures our real use and the copiers' prices).
Both lane venues read the same two names.
"""
import os
import subprocess
import sys

import pytest

from sniper.settings import memecoin as cfg

_PROBE = ("from sniper.execution import pump_curve_buy as a, pumpswap_buy as b;"
          "print(a.DEFAULT_COMPUTE_UNIT_LIMIT, a.DEFAULT_PRIORITY_FEE_MICROLAMPORTS,"
          " b.DEFAULT_COMPUTE_UNIT_LIMIT, b.DEFAULT_PRIORITY_FEE_MICROLAMPORTS)")


def _probe(**env):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("MEMECOIN_SLOT_LANE_CU")}
    out = subprocess.run([sys.executable, "-c", _PROBE], env={**clean, **env}, text=True,
                         capture_output=True, check=True, timeout=60)
    return out.stdout.split()


def test_unset_knobs_sign_exactly_what_the_lane_always_signed():
    assert _probe() == ["400000", "200000", "400000", "200000"]


def test_both_venues_take_the_configured_budget():
    assert _probe(MEMECOIN_SLOT_LANE_CU_LIMIT="160000",
                  MEMECOIN_SLOT_LANE_CU_PRICE_MICROLAMPORTS="900000") == [
        "160000", "900000", "160000", "900000"]


@pytest.mark.parametrize("name,value", [("MEMECOIN_SLOT_LANE_CU_LIMIT", 0),
                                        ("MEMECOIN_SLOT_LANE_CU_LIMIT", 1_400_001),
                                        ("MEMECOIN_SLOT_LANE_CU_PRICE_MICROLAMPORTS", 0)])
def test_an_out_of_range_budget_fails_the_armed_config_check(monkeypatch, name, value):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, name, value)
    with pytest.raises(Exception, match=name):
        cfg.validate_real_exit_policy()
