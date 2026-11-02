import time
from dataclasses import replace

import pytest

from sniper.research.memecoin_opportunity.curve_math import constant_product_clip_quote
from sniper.research.memecoin_opportunity.engine import RemoteQuoteInput, RemoteRoundTripProbe
from sniper.research.memecoin_opportunity.solana_probe import (
    NATIVE_SOL_MINT,
    OpportunitySolanaProbe,
    jupiter_quote,
    parse_jupiter_quote,
    parse_jupiter_quote_receipt,
)
from sniper.research.memecoin_opportunity.state import (
    LaunchState,
    PUMP_SOL_QUOTE_MINT,
    PUMP_USDC_QUOTE_MINT,
)
from sniper.research.memecoin_opportunity.types import LifecycleState, VenueId


def _state(venue=VenueId.PUMPFUN_SOLANA):
    return LaunchState(
        launch_id="launch-1",
        venue_id=venue,
        token_id="TokenMint11111111111111111111111111111111",
        curve_id="Curve111111111111111111111111111111111",
        creator_id="Creator1111111111111111111111111111111",
        launched_at="2026-07-27T00:00:00Z",
        observed_at="2026-07-27T00:00:02Z",
        lifecycle=LifecycleState.PRE_MIGRATION,
        event_count=3,
        buy_count=2,
        sell_count=0,
        capital_in_atomic=1_000_000,
        capital_out_atomic=0,
        unique_buyers=("a", "b"),
        buyer_token_balances=(("a", 10), ("b", 10)),
        creator_sale_count=0,
        virtual_token_reserves=1_000_000_000,
        virtual_quote_reserves=500_000_000,
        real_token_reserves=800_000_000,
        real_quote_reserves=20_000_000,
        initial_real_token_reserves=800_000_000,
        progress_bps=500,
        blocker_codes=(),
        quote_mint=PUMP_SOL_QUOTE_MINT,
    )


def _quote():
    return constant_product_clip_quote(
        virtual_token_reserves=1_000_000_000,
        virtual_quote_reserves=500_000_000,
        quote_in_atomic=1_000_000,
        fee_bps=100,
    )


def test_parse_jupiter_quote_requires_positive_exact_output_and_route():
    assert parse_jupiter_quote({"outAmount": "123", "routePlan": [{"swapInfo": {"label": "Pump.fun"}}]}) == 123
    for payload in ({}, {"outAmount": "0", "routePlan": [{}]}, {"outAmount": "12", "routePlan": []}):
        try:
            parse_jupiter_quote(payload)
        except ValueError:
            pass
        else:
            raise AssertionError("malformed route was accepted")


def test_parse_jupiter_quote_receipt_keeps_expected_and_slippage_floor():
    receipt = parse_jupiter_quote_receipt(
        {
            "outAmount": "123",
            "otherAmountThreshold": "119",
            "routePlan": [{"swapInfo": {"label": "Pump.fun"}}],
        }
    )

    assert receipt.expected_out_atomic == 123
    assert receipt.minimum_out_atomic == 119


def test_parse_jupiter_quote_receipt_requires_a_valid_floor():
    with pytest.raises(ValueError, match="JUPITER_MALFORMED_QUOTE"):
        parse_jupiter_quote_receipt(
            {"outAmount": "123", "routePlan": [{"swapInfo": {"label": "Pump.fun"}}]}
        )


def test_probe_maps_provider_errors_but_does_not_swallow_process_interrupts():
    def provider_error(*_args):
        raise RuntimeError("credential-like provider detail")

    probe = OpportunitySolanaProbe(
        quote_fn=provider_error, start_worker=False, min_call_interval_s=0
    )
    probe(_state(), _quote())
    assert probe.drain_once() is True
    assert probe.drain_completions()[0].blocker_code == "REMOTE_QUOTE_PROVIDER_ERROR"
    probe.close()

    def interrupt(*_args):
        raise KeyboardInterrupt

    interrupted = OpportunitySolanaProbe(
        quote_fn=interrupt, start_worker=False, min_call_interval_s=0
    )
    interrupted(_state(), _quote())
    with pytest.raises(KeyboardInterrupt):
        interrupted.drain_once()
    interrupted.close()


def test_jupiter_quote_uses_current_keyless_gateway(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"outAmount":"123","routePlan":[{}]}'

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(
        "sniper.research.memecoin_opportunity.solana_probe.urllib.request.urlopen",
        fake_urlopen,
    )

    payload = jupiter_quote(NATIVE_SOL_MINT, _state().token_id, 1_000_000)

    assert payload["outAmount"] == "123"
    assert captured["url"].startswith("https://api.jup.ag/swap/v1/quote?")
    assert captured["timeout"] == 3.0


def test_probe_is_nonblocking_deduped_and_sells_exact_acquired_quantity():
    calls = []

    def quote_fn(input_mint, output_mint, amount):
        calls.append((input_mint, output_mint, amount))
        if input_mint == NATIVE_SOL_MINT:
            return {"outAmount": "9000", "otherAmountThreshold": "8800", "routePlan": [{"swapInfo": {"label": "Pump.fun"}}]}
        return {"outAmount": "970000", "otherAmountThreshold": "960000", "routePlan": [{"swapInfo": {"label": "Pump.fun"}}]}

    probe = OpportunitySolanaProbe(
        quote_fn=quote_fn, start_worker=False, cache_ttl_s=10, min_call_interval_s=0
    )
    pending = probe(_state(), _quote())
    duplicate = probe(_state(), _quote())
    assert pending.blocker_code == "REMOTE_QUOTE_PENDING"
    assert duplicate.blocker_code == "REMOTE_QUOTE_PENDING"
    assert probe.metrics()["queued"] == 1

    assert probe.drain_once() is True
    result = probe(_state(), _quote())
    assert isinstance(result, RemoteRoundTripProbe)
    assert result.entry_quote_ok is True
    assert result.acquired_quantity_sell_ok is True
    assert result.tokens_out_atomic == 9000
    assert result.immediate_exit_quote_atomic == 970000
    assert result.round_trip_return_bps == -300
    assert calls == [
        (NATIVE_SOL_MINT, _state().token_id, 1_000_000),
        (_state().token_id, NATIVE_SOL_MINT, 9000),
    ]
    probe.close()


def test_probe_carries_usdc_quote_mint_through_both_jupiter_legs():
    calls = []

    def quote_fn(input_mint, output_mint, amount):
        calls.append((input_mint, output_mint, amount))
        return {
            "outAmount": "9000" if input_mint == PUMP_USDC_QUOTE_MINT else "24_000_000".replace("_", ""),
            "otherAmountThreshold": "8900" if input_mint == PUMP_USDC_QUOTE_MINT else "23_000_000".replace("_", ""),
            "routePlan": [{"swapInfo": {"label": "Pump.fun"}}],
        }

    state = replace(_state(), quote_mint=PUMP_USDC_QUOTE_MINT)
    request = RemoteQuoteInput(
        25_000_000,
        quote_mint=PUMP_USDC_QUOTE_MINT,
        launch_id=state.launch_id,
        state_fingerprint="usdc-state",
    )
    probe = OpportunitySolanaProbe(
        quote_fn=quote_fn, start_worker=False, cache_ttl_s=10, min_call_interval_s=0
    )

    assert probe(state, request).blocker_code == "REMOTE_QUOTE_PENDING"
    assert probe.drain_once() is True
    result = probe(state, request)

    assert result.provider == "jupiter_quote_v3"
    assert result.quote_mint == PUMP_USDC_QUOTE_MINT
    assert calls == [
        (PUMP_USDC_QUOTE_MINT, state.token_id, 25_000_000),
        (state.token_id, PUMP_USDC_QUOTE_MINT, 9_000),
    ]
    probe.close()


def test_probe_rejects_unsupported_quote_asset_before_queueing():
    state = replace(_state(), quote_mint="UnsupportedMint")
    probe = OpportunitySolanaProbe(start_worker=False)

    result = probe(
        state,
        RemoteQuoteInput(1_000_000, quote_mint="UnsupportedMint"),
    )

    assert result.blocker_code == "REMOTE_QUOTE_ASSET_UNSUPPORTED"
    assert probe.metrics()["queued"] == 0
    probe.close()


def test_probe_rejects_historical_only_bags_solana_before_queueing():
    probe = OpportunitySolanaProbe(start_worker=False)

    result = probe(_state(VenueId.BAGS_SOLANA), _quote())

    assert result.blocker_code == "REMOTE_SIMULATION_UNAVAILABLE"
    assert probe.metrics()["queued"] == 0
    probe.close()


def test_paper_exit_buckets_queue_and_complete_with_separate_ids():
    times = iter((
        "2026-08-11T12:00:00Z",
        "2026-08-11T12:00:01Z",
        "2026-08-11T12:00:02Z",
        "2026-08-11T12:00:03Z",
        "2026-08-11T12:00:04Z",
        "2026-08-11T12:00:05Z",
    ))

    def quote_fn(_input_mint, _output_mint, amount):
        return {
            "inAmount": str(amount),
            "outAmount": "25000000",
            "otherAmountThreshold": "24000000",
            "slippageBps": 300,
            "routePlan": [{}],
        }

    probe = OpportunitySolanaProbe(
        quote_fn=quote_fn,
        start_worker=False,
        min_call_interval_s=0,
        paper_queue_max=2,
        utc_now_fn=lambda: next(times),
    )
    request = {
        "input_mint": _state().token_id,
        "output_mint": PUMP_SOL_QUOTE_MINT,
        "amount_atomic": 1_000,
        "input_decimals": 6,
        "output_decimals": 9,
    }

    assert probe.paper_exit_quote(request_id="obs-5000", **request) == {
        "status": "pending"
    }
    assert probe.paper_exit_quote(request_id="obs-10000", **request) == {
        "status": "pending"
    }
    assert probe.metrics()["paper_queued"] == 2
    assert probe.paper_request_pending("obs-5000") is True
    assert probe.paper_request_pending("obs-10000") is True

    assert probe.drain_once() is True
    assert probe.paper_request_pending("obs-5000") is False
    assert probe.drain_once() is True
    completions = probe.drain_paper_completions(limit=2)

    assert [item.observation_id for item in completions] == [
        "obs-5000",
        "obs-10000",
    ]
    assert [item.completed_at for item in completions] == [
        "2026-08-11T12:00:03Z",
        "2026-08-11T12:00:05Z",
    ]
    probe.close()


def test_paper_entry_completion_stays_out_of_the_exit_completion_lane():
    probe = OpportunitySolanaProbe(
        quote_fn=lambda _input, _output, amount: {
            "inAmount": str(amount),
            "outAmount": "1000",
            "otherAmountThreshold": "970",
            "slippageBps": 300,
            "routePlan": [{}],
        },
        start_worker=False,
        min_call_interval_s=0,
    )

    assert probe.paper_quote(
        request_id="entry-1",
        input_mint=PUMP_SOL_QUOTE_MINT,
        output_mint=_state().token_id,
        amount_atomic=25_000_000,
        input_decimals=9,
        output_decimals=6,
    ) == {"status": "pending"}
    assert probe.drain_once() is True

    assert probe.drain_paper_completions() == ()
    probe.close()


def test_paper_exit_completion_lane_counts_oldest_drop():
    probe = OpportunitySolanaProbe(
        quote_fn=lambda _input, _output, amount: {
            "inAmount": str(amount),
            "outAmount": "1000",
            "otherAmountThreshold": "970",
            "slippageBps": 300,
            "routePlan": [{}],
        },
        start_worker=False,
        min_call_interval_s=0,
        paper_queue_max=2,
        completion_max=1,
    )
    request = {
        "input_mint": _state().token_id,
        "output_mint": PUMP_SOL_QUOTE_MINT,
        "amount_atomic": 1_000,
        "input_decimals": 6,
        "output_decimals": 9,
    }
    probe.paper_exit_quote(request_id="obs-1", **request)
    probe.paper_exit_quote(request_id="obs-2", **request)
    probe.drain_once()
    probe.drain_once()

    assert [item.observation_id for item in probe.drain_paper_completions()] == [
        "obs-2"
    ]
    assert probe.metrics()["paper_completion_drops"] == 1
    probe.close()


def test_probe_expires_cache_and_bounds_queue():
    now = [10.0]

    def quote_fn(input_mint, output_mint, amount):
        return {"outAmount": "100", "otherAmountThreshold": "99", "routePlan": [{"swapInfo": {"label": "route"}}]}

    probe = OpportunitySolanaProbe(
        quote_fn=quote_fn,
        now_fn=lambda: now[0],
        start_worker=False,
        cache_ttl_s=2,
        queue_max=1,
        min_call_interval_s=0,
    )
    assert probe(_state(), _quote()).blocker_code == "REMOTE_QUOTE_PENDING"
    second_state = replace(
        _state(),
        token_id="SecondToken111111111111111111111111111111",
    )
    second = probe(second_state, _quote())
    assert second.blocker_code == "REMOTE_QUOTE_QUEUE_FULL"
    probe.drain_once()
    assert probe(_state(), _quote()).entry_quote_ok is True
    now[0] = 13.0
    assert probe(_state(), _quote()).blocker_code == "REMOTE_QUOTE_PENDING"
    probe.close()


def test_background_worker_respects_minimum_call_interval():
    call_times = []

    def quote_fn(input_mint, output_mint, amount):
        call_times.append(time.monotonic())
        return {"outAmount": "100", "otherAmountThreshold": "99", "routePlan": [{"swapInfo": {"label": "route"}}]}

    probe = OpportunitySolanaProbe(
        quote_fn=quote_fn,
        min_call_interval_s=0.02,
        cache_ttl_s=10,
    )
    probe(_state(), _quote())
    deadline = time.monotonic() + 1
    while probe.metrics()["completed"] < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    probe.close()
    assert len(call_times) == 2
    assert call_times[1] - call_times[0] >= 0.018


def test_probe_prioritizes_inward_candidate_over_fifo_order():
    calls = []

    def quote_fn(input_mint, output_mint, amount):
        calls.append((input_mint, output_mint))
        return {"outAmount": "100", "otherAmountThreshold": "99", "routePlan": [{"swapInfo": {"label": "route"}}]}

    probe = OpportunitySolanaProbe(
        quote_fn=quote_fn,
        start_worker=False,
        min_call_interval_s=0,
    )
    weak = replace(_state(), launch_id="weak", token_id="WeakMint")
    inward = replace(_state(), launch_id="inward", token_id="InwardMint")
    probe(
        weak,
        RemoteQuoteInput(
            1_000_000,
            launch_id="weak",
            state_fingerprint="weak-state",
            priority_distance_bps=7_500,
            inward_velocity_bps_per_s=0,
        ),
    )
    probe(
        inward,
        RemoteQuoteInput(
            1_000_000,
            launch_id="inward",
            state_fingerprint="inward-state",
            priority_distance_bps=2_500,
            inward_velocity_bps_per_s=2_500,
        ),
    )

    probe.drain_once()

    assert calls[0] == (NATIVE_SOL_MINT, "InwardMint")
    probe.close()


def test_probe_discards_stale_queue_work_before_provider_call():
    now = [10.0]
    calls = []
    probe = OpportunitySolanaProbe(
        quote_fn=lambda *args: calls.append(args) or {
            "outAmount": "100",
            "otherAmountThreshold": "99",
            "routePlan": [{"swapInfo": {"label": "route"}}],
        },
        now_fn=lambda: now[0],
        start_worker=False,
        min_call_interval_s=0,
        max_queue_age_ms=2_000,
    )
    probe(_state(), _quote())
    now[0] = 12.001

    probe.drain_once()
    completion = probe.drain_completions()[0]

    assert calls == []
    assert completion.blocker_code == "REMOTE_QUOTE_QUEUE_STALE"
    assert probe.metrics()["stale_queue_discards"] == 1
    probe.close()


def test_probe_bounds_cache_and_completion_receipts():
    probe = OpportunitySolanaProbe(
        quote_fn=lambda *_: {
            "outAmount": "100",
            "otherAmountThreshold": "99",
            "routePlan": [{"swapInfo": {"label": "route"}}],
        },
        start_worker=False,
        min_call_interval_s=0,
        cache_max=1,
        completion_max=1,
    )
    for suffix in ("One", "Two"):
        state = replace(_state(), launch_id=suffix, token_id=f"{suffix}Mint")
        probe(state, _quote())
        probe.drain_once()

    metrics = probe.metrics()

    assert metrics["cached"] == 1
    assert metrics["cache_evictions"] == 1
    assert metrics["completion_drops"] == 1
    assert len(probe.drain_completions()) == 1
    probe.close()


def test_paper_quote_uses_one_low_priority_buy_and_keeps_full_floor_evidence():
    calls = []
    payload = {
        "inAmount": "1000000",
        "outAmount": "9000",
        "otherAmountThreshold": "8800",
        "slippageBps": 300,
        "routePlan": [{"swapInfo": {"ammKey": "Amm1", "label": "Pump.fun"}}],
    }
    probe = OpportunitySolanaProbe(
        quote_fn=lambda *args: calls.append(args) or payload,
        start_worker=False,
        min_call_interval_s=0,
        utc_now_fn=iter([
            "2026-08-05T00:00:01Z",
            "2026-08-05T00:00:02Z",
            "2026-08-05T00:00:03Z",
        ]).__next__,
    )

    pending = probe.paper_quote(
        request_id="r1",
        input_mint=PUMP_SOL_QUOTE_MINT,
        output_mint=_state().token_id,
        amount_atomic=1_000_000,
        input_decimals=9,
        output_decimals=6,
    )
    assert pending == {"status": "pending"}
    assert probe.drain_once() is True
    result = probe.paper_quote(
        request_id="r1",
        input_mint=PUMP_SOL_QUOTE_MINT,
        output_mint=_state().token_id,
        amount_atomic=1_000_000,
        input_decimals=9,
        output_decimals=6,
    )

    assert result["status"] == "filled"
    assert result["economics"]["expected_out_atomic"] == 9_000
    assert result["economics"]["floor_out_atomic"] == 8_800
    assert result["economics"]["quote_provider"] == "jupiter_quote_v3"
    assert result["economics"]["route_binding"]["route_plan"] == payload["routePlan"]
    assert result["economics"]["quote_payload"] == payload
    assert calls == [(NATIVE_SOL_MINT, _state().token_id, 1_000_000)]
    probe.close()


def test_paper_exit_quote_sells_exact_floor_quantity_to_the_entry_asset():
    calls = []
    payload = {
        "inAmount": "8800",
        "outAmount": "26000000",
        "otherAmountThreshold": "25000000",
        "slippageBps": 300,
        "routePlan": [{"swapInfo": {"label": "Pump.fun"}}],
    }
    probe = OpportunitySolanaProbe(
        quote_fn=lambda *args: calls.append(args) or payload,
        start_worker=False,
        min_call_interval_s=0,
    )
    kwargs = {
        "request_id": "exit-r1",
        "input_mint": _state().token_id,
        "output_mint": PUMP_SOL_QUOTE_MINT,
        "amount_atomic": 8_800,
        "input_decimals": 6,
        "output_decimals": 9,
    }

    assert probe.paper_exit_quote(**kwargs) == {"status": "pending"}
    assert probe.drain_once() is True
    result = probe.paper_exit_quote(**kwargs)

    assert result["status"] == "filled"
    assert result["economics"]["floor_out_atomic"] == 25_000_000
    assert calls == [(_state().token_id, NATIVE_SOL_MINT, 8_800)]
    probe.close()



def test_capture_quote_runs_before_a_queued_paper_quote():
    calls = []
    probe = OpportunitySolanaProbe(
        quote_fn=lambda *args: calls.append(args) or {
            "outAmount": "100", "otherAmountThreshold": "99",
            "slippageBps": 300, "routePlan": [{"swapInfo": {"label": "route"}}],
        },
        start_worker=False,
        queue_max=2,
        min_call_interval_s=0,
    )
    probe.paper_quote(
        request_id="r1", input_mint=PUMP_SOL_QUOTE_MINT,
        output_mint="PaperMint", amount_atomic=1_000_000,
        input_decimals=9, output_decimals=6,
    )
    probe(_state(), _quote())

    probe.drain_once()

    assert calls[0] == (NATIVE_SOL_MINT, _state().token_id, 1_000_000)
    probe.close()


def test_paper_quote_maps_provider_failure_to_a_typed_terminal_reason():
    probe = OpportunitySolanaProbe(
        quote_fn=lambda *_: (_ for _ in ()).throw(RuntimeError("provider detail")),
        start_worker=False,
        min_call_interval_s=0,
    )
    kwargs = {
        "request_id": "r1", "input_mint": PUMP_SOL_QUOTE_MINT,
        "output_mint": "PaperMint", "amount_atomic": 1_000_000,
        "input_decimals": 9, "output_decimals": 6,
    }
    assert probe.paper_quote(**kwargs) == {"status": "pending"}
    probe.drain_once()
    assert probe.paper_quote(**kwargs) == {
        "status": "rejected", "reason": "quote_unavailable"
    }
    probe.close()


# ---- paper lane: worker-thread dispatch + bounded anti-starvation (2026-08-06) ----
#
# Two production defects found after arming the radar paper adapter (0-for-2 filled):
#   1. `_run` (the real worker thread) called `_finish` for EVERY result. A processed paper
#      request returns a dict; `_finish` reads RemoteRoundTripProbe attributes and would
#      raise, killing the probe thread. Only `drain_once` (the test path) dispatched by kind.
#   2. Paper requests entered the ONE-SLOT priority queue at priority 20_000, where the
#      capture replacement path evicts them on the next capture quote — under a busy tape
#      (exactly when v5 alerts fire) they starved to their 30s `capacity_expired` receipt.

def _paper_payload():
    return {
        "inAmount": "1000000",
        "outAmount": "9000",
        "otherAmountThreshold": "8800",
        "slippageBps": 300,
        "routePlan": [{"swapInfo": {"ammKey": "Amm1", "label": "Pump.fun"}}],
    }


def _paper_kwargs():
    return dict(
        request_id="r-starve",
        input_mint=PUMP_SOL_QUOTE_MINT,
        output_mint=_state().token_id,
        amount_atomic=1_000_000,
        input_decimals=9,
        output_decimals=6,
    )


def test_worker_service_path_dispatches_paper_results():
    probe = OpportunitySolanaProbe(
        quote_fn=lambda *a: _paper_payload(), start_worker=False, min_call_interval_s=0)
    assert probe.paper_quote(**_paper_kwargs()) == {"status": "pending"}
    request = probe._next_request()
    assert request is not None and request.kind == "paper"
    probe._service(request)          # the SAME body _run executes — must not raise
    done = probe.paper_quote(**_paper_kwargs())
    assert done["status"] == "filled"
    assert done["economics"]["expected_out_atomic"] == 9000


def test_aged_paper_request_takes_the_next_slot_over_capture():
    clock = {"t": 100.0}
    probe = OpportunitySolanaProbe(
        quote_fn=lambda *a: _paper_payload(), start_worker=False, min_call_interval_s=0,
        now_fn=lambda: clock["t"], paper_starvation_bound_s=5.0)
    probe.paper_quote(**_paper_kwargs())
    probe(_state(), _quote())            # capture occupies the priority queue
    clock["t"] = 106.0                          # paper is past the bound
    request = probe._next_request()
    assert request is not None and request.kind == "paper"
    probe._service(request)
    assert probe.paper_quote(**_paper_kwargs())["status"] == "filled"


def test_capture_keeps_strict_priority_while_paper_is_young():
    clock = {"t": 100.0}
    probe = OpportunitySolanaProbe(
        quote_fn=lambda *a: _paper_payload(), start_worker=False, min_call_interval_s=0,
        now_fn=lambda: clock["t"], paper_starvation_bound_s=5.0)
    probe.paper_quote(**_paper_kwargs())
    probe(_state(), _quote())
    request = probe._next_request()
    assert request is not None and request.kind != "paper"   # capture first inside the bound


def test_paper_serviced_when_capture_is_idle():
    probe = OpportunitySolanaProbe(
        quote_fn=lambda *a: _paper_payload(), start_worker=False, min_call_interval_s=0)
    probe.paper_quote(**_paper_kwargs())
    request = probe._next_request()             # young, but nothing else to do
    assert request is not None and request.kind == "paper"


def test_capture_replacement_cannot_evict_a_queued_paper_request():
    clock = {"t": 100.0}
    probe = OpportunitySolanaProbe(
        quote_fn=lambda *a: _paper_payload(), start_worker=False, min_call_interval_s=0,
        now_fn=lambda: clock["t"], paper_starvation_bound_s=5.0)
    probe.paper_quote(**_paper_kwargs())
    for _ in range(3):                          # sustained capture pressure
        probe(_state(), _quote())
    assert probe.metrics()["paper_queued"] == 1  # still queued, never evicted
    clock["t"] = 106.0
    request = probe._next_request()
    assert request is not None and request.kind == "paper"


def test_solana_probe_reports_direct_request_and_failure_progress():
    times = iter((
        "2026-08-11T12:00:00Z",
        "2026-08-11T12:00:01Z",
        "2026-08-11T12:00:02Z",
    ))
    probe = OpportunitySolanaProbe(
        quote_fn=lambda *_args: (_ for _ in ()).throw(RuntimeError("provider down")),
        start_worker=False,
        min_call_interval_s=0,
        utc_now_fn=lambda: next(times),
    )

    assert probe.paper_quote(**_paper_kwargs()) == {"status": "pending"}
    assert probe.drain_once() is True

    metrics = probe.metrics()
    assert metrics["requested"] == 1
    assert metrics["last_request_at"] == "2026-08-11T12:00:00Z"
    assert metrics["last_success_at"] is None
    assert metrics["last_failure_at"] == "2026-08-11T12:00:02Z"
    assert metrics["last_failure"] == "quote_unavailable"
