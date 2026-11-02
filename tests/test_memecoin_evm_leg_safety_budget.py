"""The funded EVM safety budget, and why 4663's was the first domino in the TITAN loss.

The safety worker runs inside the main loop and the EXIT SWEEP lives in that same loop, so a slow
entry stops every rail on every open position. On 2026-09-02 the loop went 143s without a cycle,
the watchdog killed the runner for `sweep_stale`, and position 48565 — holding a CONFIRMED stop at
-41.5% since 18:51:19 — did not sell until 18:55:27 at -81.3%.
"""
import importlib
import os

import pytest


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import sniper.services.memecoin_evm_leg as L
    return importlib.reload(L)


def test_4663_no_longer_gets_a_budget_two_and_a_half_times_every_other_chain():
    import sniper.services.memecoin_evm_leg as L
    assert L._FUNDED_EVM_4663_MULTIHOP_SAFETY_WAIT_S <= L._FUNDED_EVM_SAFETY_WAIT_S, (
        "4663 blocking the loop longer than any other chain is what stalled the exit sweep")


def test_the_budget_covers_the_p90_answered_safety_run():
    """Measured over 7d on 4663: answered runs are MED 2,096ms, p90 7,085ms. A budget under the
    p90 turns ordinary successes into refusals."""
    import sniper.services.memecoin_evm_leg as L
    assert L._FUNDED_EVM_4663_MULTIHOP_SAFETY_WAIT_S >= 7.1, "would refuse a p90 real answer"


def test_the_budget_is_well_under_the_measured_timeout_burn():
    """Timed-out runs burned up to 45,492ms and produced nothing. The budget exists to stop
    paying for that, so it must be far below it."""
    import sniper.services.memecoin_evm_leg as L
    assert L._FUNDED_EVM_4663_MULTIHOP_SAFETY_WAIT_S <= 20.0


def test_both_budgets_are_env_settable(monkeypatch):
    """Hardcoded, they could only be tuned by a deploy — during an incident, that is the wrong
    control surface."""
    L = _reload(monkeypatch, MEMECOIN_FUNDED_EVM_SAFETY_WAIT_S="9",
                MEMECOIN_FUNDED_EVM_4663_SAFETY_WAIT_S="11")
    assert L._FUNDED_EVM_SAFETY_WAIT_S == 9.0
    assert L._FUNDED_EVM_4663_MULTIHOP_SAFETY_WAIT_S == 11.0
    for k in ("MEMECOIN_FUNDED_EVM_SAFETY_WAIT_S", "MEMECOIN_FUNDED_EVM_4663_SAFETY_WAIT_S"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(L)


def test_the_default_is_restored_after_the_env_test(monkeypatch):
    import sniper.services.memecoin_evm_leg as L
    importlib.reload(L)
    assert L._FUNDED_EVM_4663_MULTIHOP_SAFETY_WAIT_S == 12.0
