from sniper.research.memecoin_opportunity.decoders import DecodedVenueEvent
from sniper.research.memecoin_opportunity.state import (
    add_feature_derivatives,
    LaunchState,
    MAYHEM_AGENT_IDS,
    PUMP_SOL_QUOTE_MINT,
    PUMP_USDC_QUOTE_MINT,
    apply_venue_event,
    launch_state,
    launch_state_from_json,
    point_in_time_features,
)
from sniper.research.memecoin_opportunity.types import EventKind, LifecycleState, VenueId


def _launch(**overrides):
    values = dict(
        event_kind=EventKind.LAUNCH,
        lifecycle=LifecycleState.PRE_MIGRATION,
        token_id="Mint1",
        curve_id="Curve1",
        creator_id="Creator1",
        virtual_token_reserves=1_000_000,
        virtual_quote_reserves=500_000,
        real_token_reserves=800_000,
        real_quote_reserves=0,
        decoder_version="fixture-v1",
    )
    values.update(overrides)
    return DecodedVenueEvent(**values)


def _trade(kind, actor, quote, token, **overrides):
    values = dict(
        event_kind=kind,
        lifecycle=LifecycleState.PRE_MIGRATION,
        token_id="Mint1",
        curve_id="Curve1",
        creator_id="Creator1",
        actor_id=actor,
        quote_delta_atomic=quote,
        token_delta_atomic=token,
        virtual_token_reserves=900_000,
        virtual_quote_reserves=550_000,
        real_token_reserves=700_000,
        real_quote_reserves=50_000,
        decoder_version="fixture-v1",
    )
    values.update(overrides)
    return DecodedVenueEvent(**values)


def test_reducer_tracks_effective_demand_concentration_and_creator_sale():
    state = launch_state(
        "launch-1", VenueId.PUMPFUN_SOLANA, _launch(), "2026-07-26T12:00:00Z"
    )
    state = apply_venue_event(
        state,
        _trade(EventKind.BUY, "Buyer1", 25_000, -100_000),
        "2026-07-26T12:00:01Z",
    )
    state = apply_venue_event(
        state,
        _trade(EventKind.BUY, "Router1", 100_000, -400_000),
        "2026-07-26T12:00:02Z",
        router_ids={"Router1"},
    )
    state = apply_venue_event(
        state,
        _trade(EventKind.BUY, "Buyer2", 25_000, -100_000),
        "2026-07-26T12:00:03Z",
    )
    state = apply_venue_event(
        state,
        _trade(EventKind.SELL, "Creator1", -10_000, 20_000),
        "2026-07-26T12:00:04Z",
    )

    assert isinstance(state, LaunchState)
    assert state.unique_buyers == ("Buyer1", "Buyer2")
    assert state.effective_buyer_hhi_bps == 5_000
    assert state.creator_sale_seen is True
    assert state.buy_count == 3
    assert state.capital_in_atomic == 150_000
    assert state.progress_bps == 1_250


def test_point_in_time_features_use_only_prior_snapshot_interval():
    initial = launch_state(
        "launch-1", VenueId.PUMPFUN_SOLANA, _launch(), "2026-07-26T12:00:00Z"
    )
    prior = apply_venue_event(
        initial,
        _trade(EventKind.BUY, "Buyer1", 10_000, -40_000),
        "2026-07-26T12:00:02Z",
    )
    current = apply_venue_event(
        prior,
        _trade(EventKind.BUY, "Buyer2", 30_000, -120_000),
        "2026-07-26T12:00:04Z",
    )

    features = point_in_time_features(current, prior=prior)
    assert features["capital_velocity_atomic_per_s"] == 15_000
    assert features["buyer_velocity_per_s_micros"] == 500_000
    assert features["interval_elapsed_ms"] == 2_000
    assert features["sell_pressure_bps"] == 0
    assert features["effective_buyer_hhi_change_bps"] == -3_750
    assert features["creator_sale_since_prior"] is False

    derivatives = add_feature_derivatives(
        features,
        prior={
            "capital_velocity_atomic_per_s": 5_000,
            "buyer_velocity_per_s_micros": 250_000,
        },
    )
    assert derivatives["capital_acceleration_atomic_per_s2"] == 5_000
    assert derivatives["buyer_acceleration_per_s2_micros"] == 125_000


def test_missing_curve_state_or_source_gap_blocks_eligibility():
    missing = launch_state(
        "launch-2",
        VenueId.BAGS_SOLANA,
        _launch(virtual_token_reserves=None, virtual_quote_reserves=None),
        "2026-07-26T12:00:00Z",
    )
    assert missing.eligible is False
    assert "UNSUPPORTED_CURVE_MATH" in missing.blocker_codes

    gapped = apply_venue_event(
        missing,
        _trade(EventKind.BUY, "Buyer1", 10, -10),
        "2026-07-26T12:00:01Z",
        blocker_codes={"EVENT_GAP"},
    )
    assert "EVENT_GAP" in gapped.blocker_codes
    assert gapped.eligible is False


def test_bags_4663_missing_block_state_persists_until_exact_lens_success():
    state = launch_state(
        "bags-4663",
        VenueId.BAGS_ROBINHOOD_4663,
        _launch(facts={"state_enrichment_status": "unavailable"}),
        "2026-07-26T12:00:00Z",
    )

    assert "MISSING_BLOCK_SCOPED_STATE" in state.blocker_codes
    still_missing = apply_venue_event(
        state,
        _trade(EventKind.BUY, "Buyer1", 10_000, -20_000),
        "2026-07-26T12:00:01Z",
    )
    assert "MISSING_BLOCK_SCOPED_STATE" in still_missing.blocker_codes

    recovered = apply_venue_event(
        still_missing,
        _trade(
            EventKind.BUY,
            "Buyer2",
            10_000,
            -20_000,
            facts={"state_enrichment_status": "available"},
        ),
        "2026-07-26T12:00:02Z",
    )
    assert "MISSING_BLOCK_SCOPED_STATE" not in recovered.blocker_codes


def test_zero_reserve_does_not_reuse_stale_state_and_missing_trade_amounts_block():
    initial = launch_state(
        "launch-3", VenueId.PUMPFUN_SOLANA, _launch(), "2026-07-26T12:00:00Z"
    )
    zeroed = apply_venue_event(
        initial,
        _trade(
            EventKind.BUY,
            "Buyer1",
            10,
            -10,
            virtual_token_reserves=0,
            virtual_quote_reserves=550_000,
        ),
        "2026-07-26T12:00:01Z",
    )
    assert zeroed.virtual_token_reserves == 0
    assert "UNSUPPORTED_CURVE_MATH" in zeroed.blocker_codes

    missing = apply_venue_event(
        initial,
        _trade(
            EventKind.BUY,
            "Buyer1",
            None,
            None,
        ),
        "2026-07-26T12:00:01Z",
    )
    assert "DECODER_FAILURE" in missing.blocker_codes


def test_pump_launch_mechanics_and_effective_fee_survive_state_round_trip():
    state = launch_state(
        "launch-usdc",
        VenueId.PUMPFUN_SOLANA,
        _launch(
            facts={
                "quote_mint": PUMP_USDC_QUOTE_MINT,
                "mayhem_mode": True,
                "cashback_enabled": True,
            }
        ),
        "2026-07-26T12:00:00Z",
    )
    state = apply_venue_event(
        state,
        _trade(
            EventKind.BUY,
            "Buyer1",
            25_000,
            -100_000,
            facts={"fee_basis_points": 95, "creator_fee_basis_points": 30},
        ),
        "2026-07-26T12:00:01Z",
    )

    restored = launch_state_from_json(__import__("json").dumps(state, default=lambda value: value.value if hasattr(value, "value") else value.__dict__))

    assert restored.quote_mint == PUMP_USDC_QUOTE_MINT
    assert restored.mayhem_mode is True
    assert restored.cashback_enabled is True
    assert restored.effective_fee_bps == 125


def test_fee_free_mayhem_agent_trade_does_not_replace_market_fee():
    state = launch_state(
        "launch-mayhem",
        VenueId.PUMPFUN_SOLANA,
        _launch(facts={"quote_mint": PUMP_SOL_QUOTE_MINT, "mayhem_mode": True}),
        "2026-07-26T12:00:00Z",
    )
    state = apply_venue_event(
        state,
        _trade(
            EventKind.BUY,
            "Buyer1",
            10_000,
            -40_000,
            facts={"fee_basis_points": 95, "creator_fee_basis_points": 30},
        ),
        "2026-07-26T12:00:01Z",
    )
    state = apply_venue_event(
        state,
        _trade(
            EventKind.BUY,
            next(iter(MAYHEM_AGENT_IDS)),
            5_000,
            -20_000,
            facts={"fee_basis_points": 0, "creator_fee_basis_points": 0},
        ),
        "2026-07-26T12:00:02Z",
    )

    assert state.effective_fee_bps == 125


def test_independent_demand_excludes_router_creator_and_mayhem_agent():
    agent = next(iter(MAYHEM_AGENT_IDS))
    initial = launch_state(
        "launch-independent",
        VenueId.PUMPFUN_SOLANA,
        _launch(facts={"quote_mint": PUMP_SOL_QUOTE_MINT, "mayhem_mode": True}),
        "2026-07-26T12:00:00Z",
    )
    prior = apply_venue_event(
        initial,
        _trade(EventKind.BUY, "Buyer1", 10_000, -40_000),
        "2026-07-26T12:00:01Z",
    )
    current = apply_venue_event(
        prior,
        _trade(EventKind.BUY, "Router1", 20_000, -80_000),
        "2026-07-26T12:00:02Z",
        router_ids={"Router1"},
    )
    current = apply_venue_event(
        current,
        _trade(EventKind.BUY, "Creator1", 30_000, -120_000),
        "2026-07-26T12:00:03Z",
    )
    current = apply_venue_event(
        current,
        _trade(EventKind.BUY, agent, 40_000, -160_000),
        "2026-07-26T12:00:04Z",
    )
    current = apply_venue_event(
        current,
        _trade(EventKind.BUY, "Buyer2", 50_000, -200_000),
        "2026-07-26T12:00:05Z",
    )

    features = point_in_time_features(current, prior=prior)

    assert current.buy_count == 5
    assert current.capital_in_atomic == 150_000
    assert current.unique_buyers == ("Buyer1", "Buyer2")
    assert current.independent_buy_count == 2
    assert current.independent_capital_in_atomic == 60_000
    assert current.protocol_agent_flow_atomic == 40_000
    assert features["effective_buyer_count"] == 2
    assert features["new_eligible_buyers"] == 1
    assert features["eligible_capital_velocity_atomic_per_s"] == 12_500
    assert features["protocol_agent_flow_interval_atomic"] == 40_000
    assert features["buyer_retention_bps"] == 10_000


def test_state_features_do_not_claim_uncomputed_buyer_independence():
    """`op_entity_edges` is empty, so no feature may be named `independent_*`."""
    state = apply_venue_event(
        launch_state("launch-1", VenueId.PUMPFUN_SOLANA, _launch(), "2026-07-26T12:00:00Z"),
        _trade(EventKind.BUY, "Buyer1", 25_000, -100_000),
        "2026-07-26T12:00:01Z",
    )
    features = point_in_time_features(state, prior=None)

    assert not [key for key in features if key.startswith("independent_")]
    assert "new_eligible_buyers" in features
    assert features["effective_buyer_count"] == len(state.unique_buyers)
