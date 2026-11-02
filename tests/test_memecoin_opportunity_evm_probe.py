from types import SimpleNamespace

from sniper.research.memecoin_opportunity.engine import RemoteRoundTripProbe
from sniper.research.memecoin_opportunity.evm_probe import OpportunityEvmProbe
from sniper.research.memecoin_opportunity.types import VenueId


def _state():
    return SimpleNamespace(
        launch_id="launch-1",
        venue_id=VenueId.PONS_V1_ROBINHOOD_4663,
        token_id="0x1111111111111111111111111111111111111111",
    )


def test_evm_probe_returns_pending_then_one_completion_without_blocking():
    work = []

    class Provider:
        def round_trip(self, state, quote):
            work.append((state.launch_id, quote.state_fingerprint))
            return RemoteRoundTripProbe(
                True, True, "pons_v3_quoter_v1", 4,
                quote_in_atomic=quote.quote_in_atomic,
                tokens_out_atomic=10,
                immediate_exit_quote_atomic=9,
                round_trip_return_bps=-1_000,
                launch_id=state.launch_id,
                venue_id=state.venue_id.value,
                token_id=state.token_id,
                state_fingerprint=quote.state_fingerprint,
                completed_at="2026-08-07T12:00:00Z",
            )

    probe = OpportunityEvmProbe(
        {VenueId.PONS_V1_ROBINHOOD_4663: Provider()},
        queue_max=2,
        completion_max=2,
        start_worker=False,
    )
    quote = SimpleNamespace(
        quote_in_atomic=25,
        state_fingerprint="state-1",
        requested_block_identity="block-1",
        priority_distance_bps=0,
        inward_velocity_bps_per_s=0,
    )

    pending = probe(_state(), quote)
    assert pending.blocker_code == "EVM_QUOTE_PENDING"
    assert work == []

    assert probe.run_one() is True
    assert work == [("launch-1", "state-1")]
    completions = probe.drain_completions(limit=2)
    assert len(completions) == 1
    assert completions[0].state_fingerprint == "state-1"
    assert completions[0].requested_state_fingerprint == "state-1"
    assert completions[0].requested_block_identity == "block-1"


def test_evm_probe_queue_is_bounded_and_reports_capacity():
    probe = OpportunityEvmProbe(
        {}, queue_max=1, completion_max=1, start_worker=False,
    )
    quote = SimpleNamespace(
        quote_in_atomic=25,
        state_fingerprint="state-1",
        priority_distance_bps=0,
        inward_velocity_bps_per_s=0,
    )

    first = probe(_state(), quote)
    second_state = SimpleNamespace(**{
        **_state().__dict__, "launch_id": "launch-2",
    })
    second_quote = SimpleNamespace(**{
        **quote.__dict__, "state_fingerprint": "state-2",
    })
    second = probe(second_state, second_quote)

    assert first.blocker_code == "EVM_QUOTE_PENDING"
    assert second.blocker_code == "EVM_QUOTE_CAPACITY"
    assert probe.metrics()["queue_depth"] == 1


def test_evm_probe_paper_entry_and_exit_return_pending_then_filled():
    economics = {
        "expected_out_atomic": 10,
        "floor_out_atomic": 9,
    }

    class Provider:
        def entry(self, state, quote_in_atomic, state_fingerprint):
            assert state.launch_id == "launch-1"
            assert quote_in_atomic == 25
            assert state_fingerprint == "state-1"
            return economics

        def exit(self, **request):
            assert request["quantity_atomic"] == 10
            return economics

    probe = OpportunityEvmProbe(
        {VenueId.PONS_V1_ROBINHOOD_4663: Provider()},
        queue_max=2,
        completion_max=2,
        start_worker=False,
    )

    pending = probe.paper_entry(
        request_id="entry-1",
        state=_state(),
        quote_in_atomic=25,
        state_fingerprint="state-1",
    )
    assert pending == {"status": "pending"}
    assert probe.run_one() is True
    assert probe.paper_entry(
        request_id="entry-1",
        state=_state(),
        quote_in_atomic=25,
        state_fingerprint="state-1",
    ) == {"status": "filled", "economics": economics}

    pending = probe.paper_exit(
        request_id="exit-1",
        venue_id=VenueId.PONS_V1_ROBINHOOD_4663,
        token_id=_state().token_id,
        quantity_atomic=10,
        token_decimals=18,
        quote_decimals=18,
        route_binding={"pool_id": "pool"},
    )
    assert pending == {"status": "pending"}
    assert probe.run_one() is True
    assert probe.paper_exit(
        request_id="exit-1",
        venue_id=VenueId.PONS_V1_ROBINHOOD_4663,
        token_id=_state().token_id,
        quantity_atomic=10,
        token_decimals=18,
        quote_decimals=18,
        route_binding={"pool_id": "pool"},
    ) == {"status": "filled", "economics": economics}


def test_evm_probe_bounds_unclaimed_paper_results():
    class Provider:
        def entry(self, state, quote_in_atomic, state_fingerprint):
            return {"expected_out_atomic": quote_in_atomic}

    probe = OpportunityEvmProbe(
        {VenueId.PONS_V1_ROBINHOOD_4663: Provider()},
        queue_max=2,
        completion_max=1,
        start_worker=False,
    )
    for request_id in ("entry-1", "entry-2"):
        assert probe.paper_entry(
            request_id=request_id,
            state=_state(),
            quote_in_atomic=25,
            state_fingerprint=request_id,
        ) == {"status": "pending"}
        assert probe.run_one() is True

    assert probe.metrics()["paper_results"] == 1


def test_evm_paper_exit_buckets_queue_and_complete_with_separate_ids():
    times = iter((
        "2026-08-11T12:00:00Z",
        "2026-08-11T12:00:01Z",
        "2026-08-11T12:00:02Z",
        "2026-08-11T12:00:03Z",
    ))

    class Provider:
        @staticmethod
        def exit(**_request):
            return {"expected_out_atomic": 10, "floor_out_atomic": 9}

    probe = OpportunityEvmProbe(
        {VenueId.PONS_V1_ROBINHOOD_4663: Provider()},
        queue_max=2,
        completion_max=2,
        start_worker=False,
        utc_now_fn=lambda: next(times),
    )
    request = {
        "venue_id": VenueId.PONS_V1_ROBINHOOD_4663,
        "token_id": _state().token_id,
        "quantity_atomic": 10,
        "token_decimals": 18,
        "quote_decimals": 18,
        "route_binding": {"pool_id": "pool"},
    }

    assert probe.paper_exit(request_id="obs-5000", **request) == {
        "status": "pending"
    }
    assert probe.paper_exit(request_id="obs-10000", **request) == {
        "status": "pending"
    }
    assert probe.metrics()["queue_depth"] == 2
    assert probe.paper_request_pending("obs-5000") is True
    assert probe.paper_request_pending("obs-10000") is True

    assert probe.run_one() is True
    assert probe.paper_request_pending("obs-5000") is False
    assert probe.run_one() is True
    completions = probe.drain_paper_completions(limit=2)

    assert [item.observation_id for item in completions] == [
        "obs-5000",
        "obs-10000",
    ]
    assert [item.completed_at for item in completions] == [
        "2026-08-11T12:00:02Z",
        "2026-08-11T12:00:03Z",
    ]


def test_evm_paper_entry_completion_stays_out_of_the_exit_completion_lane():
    class Provider:
        @staticmethod
        def entry(_state, quote_in_atomic, _state_fingerprint):
            return {"expected_out_atomic": quote_in_atomic}

    probe = OpportunityEvmProbe(
        {VenueId.PONS_V1_ROBINHOOD_4663: Provider()},
        start_worker=False,
    )

    assert probe.paper_entry(
        request_id="entry-1",
        state=_state(),
        quote_in_atomic=25,
        state_fingerprint="state-1",
    ) == {"status": "pending"}
    assert probe.run_one() is True

    assert probe.drain_paper_completions() == ()


def test_evm_paper_exit_completion_lane_counts_oldest_drop():
    class Provider:
        @staticmethod
        def exit(**_request):
            return {"expected_out_atomic": 10, "floor_out_atomic": 9}

    probe = OpportunityEvmProbe(
        {VenueId.PONS_V1_ROBINHOOD_4663: Provider()},
        queue_max=2,
        completion_max=1,
        start_worker=False,
    )
    request = {
        "venue_id": VenueId.PONS_V1_ROBINHOOD_4663,
        "token_id": _state().token_id,
        "quantity_atomic": 10,
        "token_decimals": 18,
        "quote_decimals": 18,
        "route_binding": {"pool_id": "pool"},
    }
    probe.paper_exit(request_id="obs-1", **request)
    probe.paper_exit(request_id="obs-2", **request)
    probe.run_one()
    probe.run_one()

    assert [item.observation_id for item in probe.drain_paper_completions()] == [
        "obs-2"
    ]
    assert probe.metrics()["paper_completion_drops"] == 1


def test_evm_probe_reports_direct_progress_for_each_venue():
    times = iter((
        "2026-08-11T12:00:00Z",
        "2026-08-11T12:00:01Z",
        "2026-08-11T12:00:02Z",
        "2026-08-11T12:00:03Z",
    ))

    class Provider:
        def round_trip(self, state, quote):
            if state.venue_id is VenueId.BAGS_ROBINHOOD_4663:
                raise RuntimeError("fixture failure")
            return RemoteRoundTripProbe(
                True,
                True,
                "fixture",
                1,
                launch_id=state.launch_id,
                venue_id=state.venue_id.value,
                token_id=state.token_id,
                state_fingerprint=quote.state_fingerprint,
                completed_at="2026-08-11T12:00:02Z",
            )

    probe = OpportunityEvmProbe(
        {
            VenueId.BAGS_ROBINHOOD_4663: Provider(),
            VenueId.PONS_V1_ROBINHOOD_4663: Provider(),
        },
        start_worker=False,
        utc_now_fn=lambda: next(times),
    )
    quote = SimpleNamespace(
        quote_in_atomic=25,
        state_fingerprint="state-1",
        priority_distance_bps=0,
        inward_velocity_bps_per_s=0,
    )
    bags = SimpleNamespace(
        **{
            **_state().__dict__,
            "venue_id": VenueId.BAGS_ROBINHOOD_4663,
        }
    )
    probe(bags, quote)
    assert probe.run_one() is True
    probe(_state(), quote)
    assert probe.run_one() is True

    metrics = probe.metrics()

    assert metrics["by_venue"]["bags_robinhood_4663"] == {
        "requested": 1,
        "completed": 0,
        "failed": 1,
        "pending": 0,
        "last_request_at": "2026-08-11T12:00:00Z",
        "last_success_at": None,
        "last_failure_at": "2026-08-11T12:00:01Z",
        "last_failure": "RuntimeError",
    }
    assert metrics["by_venue"]["pons_v1_4663"] == {
        "requested": 1,
        "completed": 1,
        "failed": 0,
        "pending": 0,
        "last_request_at": "2026-08-11T12:00:02Z",
        "last_success_at": "2026-08-11T12:00:03Z",
        "last_failure_at": None,
        "last_failure": None,
    }
