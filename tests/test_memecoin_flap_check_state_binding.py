"""A Flap check reads fresh state once; its exact simulations stay authoritative."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from sniper.execution import evm_flap_curve as flap
from test_memecoin_evm_flap_curve import _state, _matching_simulate_fn, _overrides_fn
from test_memecoin_flap_preentry_warm_simulation import (
    AMOUNT, MINT, PAYOUT, PORTAL, RPC, TAKER, reads, _warm,
)


def _check(**kwargs):
    return flap.wrap_token_safety_fn(
        "56", RPC, taker=TAKER,
        fallback_fn=lambda *a, **k: (False, "standard_fallback"), **kwargs)


def test_bound_clip_safety_reads_curve_state_once_and_keeps_exact_sell(reads):
    _warm(reads)
    assert _check()(MINT, bought_amount_atomic=AMOUNT) == (
        True, "flap_curve_exit_quotable_at_clip")
    assert [c[0] for c in reads.calls] == ["state", "override"]
    assert reads.calls[-1][1] == AMOUNT


def test_standalone_whole_balance_exit_reads_once_and_keeps_real_first(reads):
    _warm(reads)
    reads.real_payout = PAYOUT * 2
    quote, reason = flap.whole_balance_exit_quote_or_refuse(
        "56", MINT, AMOUNT, RPC, taker=TAKER)
    assert reason is None and quote["buyAmount"] == "119748894460844"
    assert quote["route"]["quote_basis"] == "real_allowance"
    assert [c[0] for c in reads.calls] == ["state", "real"]


def test_usd_safety_reuses_state_but_still_simulates_exact_buy_and_sell():
    state = _state()
    states, simulations = [], []

    def state_fn(chain, mint, rpc_url, *, http=None):
        states.append((chain, mint, rpc_url))
        return state

    live = _matching_simulate_fn(state, override_allowed=True)

    def simulate(*args, **kwargs):
        simulations.append((args[1], args[3], kwargs.get("overrides") is not None))
        return live(*args, **kwargs)

    check = _check(state_fn=state_fn, simulate_fn=simulate,
                   overrides_fn=_overrides_fn(), native_usd_price_fn=lambda chain: 500.0)
    assert check(MINT, usd=5.0) == (True, "flap_curve_exit_quotable_at_clip")
    assert states == [("56", MINT, RPC)]
    assert simulations[0] == (flap._NATIVE, 10**16, True)
    assert simulations[1][0] == MINT and simulations[1][2] is False
    assert simulations[2][0] == MINT and simulations[2][2] is True
    assert simulations[1][1] == simulations[2][1] > 0


def test_state_is_bound_for_one_check_and_reread_on_next_attempt():
    state = _state()
    calls = []

    def state_fn(*a, **k):
        calls.append(1)
        return state if len(calls) == 1 else _state(creator_fee_bps=300)

    check = _check(state_fn=state_fn,
                   simulate_fn=_matching_simulate_fn(state, override_allowed=True),
                   overrides_fn=_overrides_fn())
    assert check(MINT, bought_amount_atomic=AMOUNT)[0] is True
    assert check(MINT, bought_amount_atomic=AMOUNT) == (
        False, "flap_curve_exit_unquotable_at_clip")
    assert len(calls) == 2, "a later attempt must read today's curve fee"


def test_native_fee_change_during_exact_simulation_still_refuses():
    state = _state()
    calls = []

    def state_fn(*a, **k):
        calls.append(1)
        return state

    # The state read described 200 bps; the actual simulation now pays a different fee.
    moved = _state(creator_fee_bps=300)
    check = _check(state_fn=state_fn,
                   simulate_fn=_matching_simulate_fn(moved, override_allowed=True),
                   overrides_fn=_overrides_fn())
    assert check(MINT, bought_amount_atomic=AMOUNT) == (
        False, "flap_curve_exit_unquotable_at_clip")
    assert len(calls) == 1


def test_converted_quote_keeps_native_exact_output_without_native_fee_comparison(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_NATIVE_SWAP_ENABLED", True)
    state = _state(quote_token="0x" + "12" * 20, native_to_quote_swap_enabled=1)
    calls = []

    def state_fn(*a, **k):
        calls.append(1)
        return state

    check = _check(state_fn=state_fn, simulate_fn=lambda *a, **k: PAYOUT * 2,
                   overrides_fn=_overrides_fn())
    assert check(MINT, bought_amount_atomic=AMOUNT) == (
        True, "flap_curve_exit_quotable_at_clip")
    assert len(calls) == 1


@pytest.mark.parametrize("state", [None,
    _state(quote_token="0x" + "12" * 20, native_to_quote_swap_enabled=0),
    _state(circulating_supply=900000000000000000000000000)])
def test_unowned_missing_or_graduated_state_keeps_standard_safety(state):
    calls = []

    def state_fn(*a, **k):
        calls.append(1)
        return state

    check = _check(state_fn=state_fn,
                   simulate_fn=lambda *a, **k: pytest.fail("must use standard safety"))
    assert check(MINT, usd=50) == (False, "standard_fallback")
    assert len(calls) == 1


def test_zero_bound_quantity_without_usd_never_fetches_curve_state():
    check = _check(state_fn=lambda *a, **k: pytest.fail("zero is not a sized clip"))
    assert check(MINT, bought_amount_atomic=0) == (False, "standard_fallback")


def test_concurrent_checks_bind_separate_mint_rpc_and_taker():
    barrier = threading.Barrier(2)
    calls, simulations = [], []
    cases = [(MINT, RPC, TAKER), ("0x" + "ab" * 20, "https://other.example", "0x" + "cd" * 20)]

    def run(case):
        mint, rpc, taker = case
        state = _state(mint=mint)
        live = _matching_simulate_fn(state, override_allowed=True)

        def state_fn(chain, request_mint, request_rpc, *, http=None):
            calls.append((request_mint, request_rpc))
            if calls.count((request_mint, request_rpc)) == 1:
                barrier.wait(timeout=1)
            assert (request_mint, request_rpc) == (mint, rpc)
            return state

        def simulate(portal, input_token, output_token, amount, request_taker, request_rpc,
                     **kwargs):
            assert (request_taker, request_rpc) == (taker, rpc)
            assert input_token == mint
            simulations.append((mint, rpc, taker, amount))
            return live(portal, input_token, output_token, amount, request_taker, request_rpc,
                        **kwargs)

        check = flap.wrap_token_safety_fn("56", rpc, taker=taker,
            fallback_fn=lambda *a, **k: pytest.fail("owned curve"), state_fn=state_fn,
            simulate_fn=simulate, overrides_fn=_overrides_fn())
        return check(mint, bought_amount_atomic=AMOUNT)

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert list(workers.map(run, cases)) == [(True, "flap_curve_exit_quotable_at_clip")] * 2
    assert sorted(calls) == sorted((mint, rpc) for mint, rpc, _ in cases)
    assert all(row[3] == AMOUNT for row in simulations)


@pytest.mark.parametrize("identity", [
    ("1", MINT, RPC),
    ("56", "0x" + "ab" * 20, RPC),
    ("56", MINT, "https://other.example"),
])
def test_bound_state_does_not_answer_for_another_chain_mint_or_rpc(identity):
    calls = []

    def state_fn(chain, mint, rpc_url, *, http=None):
        calls.append((chain, mint, rpc_url))
        return None

    read = flap._check_state_fn("56", MINT, RPC, _state(), state_fn)
    assert read(*identity) is None
    assert calls == [identity]
    assert read("56", MINT.upper(), RPC)["creator_fee_bps"] == 100


def test_state_source_mutation_after_read_cannot_change_bound_fee():
    state = _state()
    live = _matching_simulate_fn(dict(state), override_allowed=True)
    calls = []

    def state_fn(*a, **k):
        calls.append(1)
        return state

    def simulate(*a, **k):
        state["creator_fee_bps"], state["fee_bps"] = 300, 400
        return live(*a, **k)

    check = _check(state_fn=state_fn, simulate_fn=simulate, overrides_fn=_overrides_fn())
    assert check(MINT, bought_amount_atomic=AMOUNT) == (
        True, "flap_curve_exit_quotable_at_clip")
    assert len(calls) == 1


def test_deadline_expired_before_check_does_not_read_state():
    from contextlib import contextmanager

    @contextmanager
    def scope(deadline):
        yield lambda: True

    check = _check(safety_deadline_scope=scope,
                   state_fn=lambda *a, **k: pytest.fail("expired check"))
    assert check(MINT, bought_amount_atomic=AMOUNT) == (False, "safety_deadline_exceeded")


def test_deadline_expired_during_exact_simulation_rejects_its_pass():
    from contextlib import contextmanager
    expired = [False]
    state = _state()
    live = _matching_simulate_fn(state, override_allowed=True)

    @contextmanager
    def scope(deadline):
        yield lambda: expired[0]

    def simulate(*a, **k):
        expired[0] = True
        return live(*a, **k)

    check = _check(safety_deadline_scope=scope, state_fn=lambda *a, **k: state,
                   simulate_fn=simulate, overrides_fn=_overrides_fn())
    assert check(MINT, bought_amount_atomic=AMOUNT) == (False, "safety_deadline_exceeded")
