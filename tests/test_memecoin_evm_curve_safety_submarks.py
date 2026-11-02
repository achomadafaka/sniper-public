"""The curve safety wrappers carry the token-safety sub-marks (2026-09-28, Alpha).

4663 token safety had no split at all: the EVM leg hands `timings` only to a safety function that
NAMES it, and the launchpad wrapper (and flap on 56) took it through **kwargs, so the 133 4663
attempts that reached the safety join since 09-14 carried no code/quote/slot/simulation times and
nothing for the curve's own state read. The wrappers now name it, time their own reads and hand it
on only to a fallback that names it. Display only: the verdict is identical with or without it.
"""
import pytest

from sniper.execution import evm_flap_curve as flap
from sniper.execution import evm_launchpad_curve as launchpad
from sniper.services.memecoin_evm_leg import _names_kwarg

MINT = "0x5e9ca04c6fe6397f287091130a63072a91afd14f"
GRADUATED = {"coin": MINT, "curve": "0x" + "d1" * 20, "counter": "0x" + "00" * 20,
             "fee_bps": 100, "state": 2}


def _wrap(module, fallback, state=GRADUATED):
    chain = "4663" if module is launchpad else "56"
    return module.wrap_token_safety_fn(
        chain, "http://rpc", taker="0x" + "ab" * 20, fallback_fn=fallback,
        state_fn=lambda *a, **k: state)


def _named_fallback(seen):
    def fallback(mint, *, deadline=None, usd=None, bought_amount_atomic=None, timings=None):
        seen.append(timings)
        if timings is not None:
            timings["code_ms"] = 12.0
        return True, "ok"
    return fallback


@pytest.mark.parametrize("module", [launchpad, flap])
def test_the_leg_hands_the_wrapper_its_timings(module):
    assert _names_kwarg(_wrap(module, _named_fallback([])), "timings")


@pytest.mark.parametrize("module", [launchpad, flap])
def test_a_graduated_coin_times_the_curve_read_and_the_standard_check(module):
    seen, timings = [], {}
    assert _wrap(module, _named_fallback(seen))(MINT, usd=100.0, timings=timings) == (True, "ok")
    assert seen == [timings] and timings["code_ms"] == 12.0
    assert timings["curve_state_ms"] >= 0.0


@pytest.mark.parametrize("module", [launchpad, flap])
def test_a_fallback_that_does_not_name_timings_never_receives_it(module):
    calls = []

    def fallback(mint, **kwargs):          # a catch-all is not a named parameter
        calls.append(kwargs)
        return False, "refused"
    assert _wrap(module, fallback)(MINT, usd=100.0, timings={}) == (False, "refused")
    assert "timings" not in calls[0]


@pytest.mark.parametrize("module", [launchpad, flap])
def test_the_verdict_is_the_same_without_timings(module):
    wrapped = _wrap(module, _named_fallback([]))
    assert wrapped(MINT, usd=100.0) == wrapped(MINT, usd=100.0, timings={})
