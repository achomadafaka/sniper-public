from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

import sniper.research.memecoin_opportunity.engine as engine_module
from sniper.research.memecoin_opportunity.decoders import DecodedVenueEvent
from sniper.research.memecoin_opportunity.directional import DirectionalVerdict, RadarZone
from sniper.research.memecoin_opportunity.engine import OpportunityEngine, RemoteRoundTripProbe
from sniper.research.memecoin_opportunity.landmarks import OutOfOrderObservation
from sniper.research.memecoin_opportunity.outcome_tracker import record_launch_outcomes
from sniper.research.memecoin_opportunity.insights import connect_insights
from sniper.research.memecoin_opportunity.outcomes import (
    CounterfactualEntry,
    append_counterfactual_entry,
)
from sniper.research.memecoin_opportunity.projection import (
    append_forward_alert_entry,
    append_forward_alert_receipt,
)
from sniper.research.memecoin_opportunity.reconciliation import reconcile_confirmation
from sniper.research.memecoin_opportunity.retention import cleanup_hot_ledger
from sniper.research.memecoin_opportunity.state import (
    PUMP_USDC_QUOTE_MINT,
    launch_state,
    launch_state_from_json,
)
import sniper.research.memecoin_opportunity.reconciliation as reconciliation_module
from sniper.research.memecoin_opportunity.store import (
    ImmutableConflict,
    LaunchIdentity,
    append_canonical_event,
    append_raw_event,
    connect,
    credit_creator_outcome,
    network_for_venue,
    retention_delete_authority,
    upsert_launch_identity,
)
from sniper.research.memecoin_opportunity.types import (
    CanonicalEvent,
    ChainOrder,
    ConfirmationState,
    EventKind,
    LifecycleState,
    NetworkId,
    RawEvent,
    VenueId,
)
from sniper.services.memecoin_opportunity_sources import VenueIdentityMap
from scripts.memecoin_opportunity_worker import load_pending_confirmations, restore_pipeline


def test_engine_uses_the_typed_out_of_order_observation():
    assert engine_module.OutOfOrderObservation is OutOfOrderObservation


def _persist_event(
    conn, *, seq, observed_at, kind, actor=None, quote=None, token=None,
    confirmation=ConfirmationState.CONFIRMED,
    chain_at=None,
    network=NetworkId.ROBINHOOD_4663,
    venue=VenueId.BAGS_ROBINHOOD_4663,
    token_id="0xabc",
    creator_id="0xcreator",
    launch_id="launch-1",
    lifecycle=LifecycleState.PRE_MIGRATION,
):
    raw_id, event_id = f"raw-{seq}", f"event-{seq}"
    payload = f'{{"seq":{seq}}}'
    raw = RawEvent(
        raw_id, network, venue,
        "source", "provider", f"{seq}:0:0", token_id, f"0xtx{seq}",
        ChainOrder(seq, 0, 0, f"0xblock{seq}"), chain_at or observed_at, observed_at,
        confirmation, payload, hashlib.sha256(payload.encode()).hexdigest(),
    )
    event = CanonicalEvent(
        event_id, raw_id, launch_id, network,
        venue, token_id, lifecycle,
        kind, f"0xtx{seq}", ChainOrder(seq, 0, 0, f"0xblock{seq}"), chain_at or observed_at,
        observed_at, observed_at, observed_at, confirmation,
        actor, quote, token, 0, actor == creator_id if actor else None, "fixture-v1",
    )
    append_raw_event(conn, raw)
    append_canonical_event(conn, event)
    if kind is EventKind.LAUNCH:
        upsert_launch_identity(conn, LaunchIdentity(
            launch_id, network, venue,
            token_id, creator_id, event_id, observed_at,
        ))
    return event_id


def _decoded(
    kind,
    *,
    actor=None,
    quote=None,
    token=None,
    vtoken=1_000_000,
    vquote=500_000,
    rtoken=800_000,
):
    return DecodedVenueEvent(
        event_kind=kind,
        lifecycle=LifecycleState.PRE_MIGRATION,
        token_id="0xabc",
        curve_id="0xcurve",
        creator_id="0xcreator",
        actor_id=actor,
        quote_delta_atomic=quote,
        token_delta_atomic=token,
        virtual_token_reserves=vtoken,
        virtual_quote_reserves=vquote,
        real_token_reserves=rtoken,
        real_quote_reserves=max(0, (vquote - 500_000)),
        decoder_version="fixture-v1",
    )


def test_pump_usdc_curve_uses_usdc_atomic_input_and_dynamic_fee(tmp_path):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.PUMPFUN_SOLANA
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 150_000_000},
        quote_decimals={venue: 9},
        fee_bps={venue: 125},
        network_cost_quote_atomic={venue: 5_000},
        clip_ladder_usd_micros=(25_000_000,),
    )
    state = launch_state(
        "pump-usdc",
        venue,
        DecodedVenueEvent(
            event_kind=EventKind.LAUNCH,
            lifecycle=LifecycleState.PRE_MIGRATION,
            token_id="MintUsdc",
            curve_id="CurveUsdc",
            creator_id="CreatorUsdc",
            virtual_token_reserves=1_000_000_000_000,
            virtual_quote_reserves=100_000_000,
            real_token_reserves=800_000_000_000,
            facts={"quote_mint": PUMP_USDC_QUOTE_MINT},
            decoder_version="fixture-v1",
        ),
        "2026-07-26T12:00:00Z",
    )

    quote = engine._quotes(state)[25_000_000]

    assert quote.quote_in_atomic == 25_000_000
    assert quote.network_cost_quote_atomic == 750


def test_unknown_pump_quote_asset_blocks_local_quote_math(tmp_path):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.PUMPFUN_SOLANA
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 150_000_000},
        quote_decimals={venue: 9},
        fee_bps={venue: 125},
    )
    state = launch_state(
        "pump-unknown",
        venue,
        DecodedVenueEvent(
            event_kind=EventKind.LAUNCH,
            lifecycle=LifecycleState.PRE_MIGRATION,
            token_id="MintUnknown",
            curve_id="CurveUnknown",
            creator_id="CreatorUnknown",
            virtual_token_reserves=1_000_000,
            virtual_quote_reserves=500_000,
            real_token_reserves=800_000,
            facts={"quote_mint": "UnknownQuoteMint"},
            decoder_version="fixture-v1",
        ),
        "2026-07-26T12:00:00Z",
    )

    assert "UNSUPPORTED_QUOTE_ASSET" in state.blocker_codes
    assert engine._quotes(state) == {}


def test_later_chain_event_clamps_small_source_timestamp_regression(tmp_path):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.BAGS_ROBINHOOD_4663
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 1_000_000},
        quote_decimals={venue: 3},
        fee_bps={venue: 200},
    )
    launch = _persist_event(
        conn,
        seq=1,
        observed_at="2026-07-26T12:00:00Z",
        chain_at="2026-07-26T12:00:02Z",
        kind=EventKind.LAUNCH,
    )
    engine.observe_decoded(
        decoded=_decoded(EventKind.LAUNCH),
        canonical_event_id=launch,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:00Z",
    )
    buy = _persist_event(
        conn,
        seq=2,
        observed_at="2026-07-26T12:00:03Z",
        chain_at="2026-07-26T12:00:01Z",
        kind=EventKind.BUY,
        actor="0xbuyer1",
        quote=10_000,
        token=-20_000,
    )

    engine.observe_decoded(
        decoded=_decoded(
            EventKind.BUY,
            actor="0xbuyer1",
            quote=10_000,
            token=-20_000,
            vtoken=980_000,
            vquote=510_000,
        ),
        canonical_event_id=buy,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:03Z",
    )

    assert engine.states["launch-1"].observed_at == "2026-07-26T12:00:02Z"


def test_non_pump_remote_route_never_becomes_an_actionable_center(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={VenueId.BAGS_ROBINHOOD_4663: 1_000_000},
        quote_decimals={VenueId.BAGS_ROBINHOOD_4663: 3},
        fee_bps={VenueId.BAGS_ROBINHOOD_4663: 200},
        entry_latency_ms={VenueId.BAGS_ROBINHOOD_4663: 100},
        remote_probe=lambda state, quote: RemoteRoundTripProbe(True, True, "fixture", 12),
    )

    event = _persist_event(conn, seq=1, observed_at="2026-07-26T12:00:00Z", kind=EventKind.LAUNCH)
    outer = engine.observe_decoded(
        decoded=_decoded(EventKind.LAUNCH), canonical_event_id=event,
        confirmation=ConfirmationState.CONFIRMED, observed_at="2026-07-26T12:00:00Z",
    )
    assert outer.zone is RadarZone.OUTER

    event = _persist_event(
        conn, seq=2, observed_at="2026-07-26T12:00:02Z", kind=EventKind.BUY,
        actor="0xbuyer1", quote=10_000, token=-20_000,
    )
    tracking = engine.observe_decoded(
        decoded=_decoded(EventKind.BUY, actor="0xbuyer1", quote=10_000, token=-20_000, vtoken=980_000, vquote=510_000),
        canonical_event_id=event, confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:02Z",
    )
    assert tracking.zone is RadarZone.TRACKING

    for seq, second, actor in ((3, 3, "0xbuyer2"), (4, 5, "0xbuyer3")):
        event = _persist_event(
            conn, seq=seq, observed_at=f"2026-07-26T12:00:0{second}Z", kind=EventKind.BUY,
            actor=actor, quote=20_000, token=-40_000,
        )
        latest = engine.observe_decoded(
            decoded=_decoded(EventKind.BUY, actor=actor, quote=20_000, token=-40_000, vtoken=940_000, vquote=550_000),
            canonical_event_id=event, confirmation=ConfirmationState.CONFIRMED,
            observed_at=f"2026-07-26T12:00:0{second}Z",
        )

    assert latest.zone is RadarZone.INNER
    assert latest.would_fire is False
    # The final landmark has no post-latency state yet, so only one entry exists.
    assert conn.execute("SELECT count(*) FROM op_counterfactual_entries").fetchone()[0] == 1
    latest_prediction = conn.execute(
        "SELECT zone,would_fire FROM op_predictions ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()
    assert tuple(latest_prediction) == ("INNER", 0)
    remote_quote = conn.execute(
        "SELECT status,provider,latency_ms,is_remote FROM op_quote_observations "
        "WHERE is_remote=1 ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()
    assert tuple(remote_quote) == ("executable", "fixture", 12, 1)
    assert conn.execute("SELECT COUNT(*) FROM op_forward_alerts").fetchone()[0] == 0

    engine.finalize_due_outcomes("2026-07-26T12:01:05Z")

    assert conn.execute("SELECT count(*) FROM op_executable_observations").fetchone()[0] >= 2
    outcomes = conn.execute("SELECT outcome_kind FROM op_competing_outcomes").fetchall()
    assert outcomes
    assert {row[0] for row in outcomes} == {"timeout"}


def test_bags_solana_remote_route_is_observed_without_inheriting_pump_arm(tmp_path):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.BAGS_SOLANA
    token_id = "TokenMint11111111111111111111111111111111"
    creator = "Creator1111111111111111111111111111111"
    results = []

    def remote_probe(state, quote):
        results.append(quote.quote_in_atomic)
        return RemoteRoundTripProbe(
            True, True, "jupiter_quote_v1", 40, None,
            quote_in_atomic=quote.quote_in_atomic,
            tokens_out_atomic=90_000,
            immediate_exit_quote_atomic=990_000,
            round_trip_return_bps=-100,
        )

    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 25_000_000},
        quote_decimals={venue: 9},
        fee_bps={venue: 100},
        remote_probe=remote_probe,
    )

    for seq, second, kind, actor, amount in (
        (1, 0, EventKind.LAUNCH, None, None),
        (2, 2, EventKind.BUY, "Buyer1", 10_000),
        (3, 3, EventKind.BUY, "Buyer2", 20_000),
        (4, 5, EventKind.BUY, "Buyer3", 40_000),
    ):
        observed = f"2026-07-26T12:00:0{second}Z"
        event_id = _persist_event(
            conn,
            seq=seq,
            observed_at=observed,
            kind=kind,
            actor=actor,
            quote=amount,
            token=-amount if amount else None,
            network=NetworkId.SOLANA,
            venue=venue,
            token_id=token_id,
            creator_id=creator,
        )
        decoded = DecodedVenueEvent(
            event_kind=kind,
            lifecycle=LifecycleState.PRE_MIGRATION,
            token_id=token_id,
            curve_id="Pool1111111111111111111111111111111111",
            creator_id=creator,
            actor_id=actor,
            quote_delta_atomic=amount,
            token_delta_atomic=-amount if amount else None,
            decoder_version="fixture-v1",
        )
        latest = engine.observe_decoded(
            decoded=decoded,
            canonical_event_id=event_id,
            confirmation=ConfirmationState.CONFIRMED,
            observed_at=observed,
        )

    assert latest.zone is RadarZone.INNER
    assert latest.would_fire is False
    assert results[-1] == 1_000_000_000
    remote = conn.execute(
        "SELECT quote_in_atomic,tokens_out_atomic,immediate_exit_quote_atomic,round_trip_return_bps "
        "FROM op_quote_observations WHERE is_remote=1 ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()
    assert tuple(remote) == ("1000000000", "90000", "990000", -100)
    assert conn.execute("SELECT count(*) FROM op_counterfactual_entries").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM op_forward_alerts").fetchone()[0] == 0
    latest_state = conn.execute(
        "SELECT state_json FROM op_state_snapshots ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()[0]
    assert '"zone_transition":"TRACKING->INNER"' in latest_state


def test_forward_alert_entry_link_is_exact_idempotent_and_immutable(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={VenueId.BAGS_ROBINHOOD_4663: 1_000_000},
        quote_decimals={VenueId.BAGS_ROBINHOOD_4663: 3},
        fee_bps={VenueId.BAGS_ROBINHOOD_4663: 200},
        remote_probe=lambda state, quote: RemoteRoundTripProbe(
            True,
            True,
            "fixture",
            12,
            quote_in_atomic=quote.quote_in_atomic,
            tokens_out_atomic=40_000,
            immediate_exit_quote_atomic=24_500,
            round_trip_return_bps=-200,
        ),
    )
    latest = None
    for seq, second, kind, actor, amount in (
        (1, 0, EventKind.LAUNCH, None, None),
        (2, 2, EventKind.BUY, "0xbuyer1", 10_000),
        (3, 3, EventKind.BUY, "0xbuyer2", 20_000),
        (4, 5, EventKind.BUY, "0xbuyer3", 40_000),
    ):
        observed = f"2026-07-26T12:00:0{second}Z"
        event_id = _persist_event(
            conn,
            seq=seq,
            observed_at=observed,
            kind=kind,
            actor=actor,
            quote=amount,
            token=-amount if amount else None,
        )
        latest = engine.observe_decoded(
            decoded=_decoded(
                kind,
                actor=actor,
                quote=amount,
                token=-amount if amount else None,
                vtoken=940_000,
                vquote=550_000,
            ),
            canonical_event_id=event_id,
            confirmation=ConfirmationState.CONFIRMED,
            observed_at=observed,
        )
    assert latest is not None and latest.landmark_id is not None
    entry = CounterfactualEntry(
        entry_id="alert-entry",
        landmark_id=latest.landmark_id,
        clip_usd_atomic=25_000_000,
        entry_latency_ms=500,
        entry_observed_at="2026-07-26T12:00:05Z",
        entry_effective_at="2026-07-26T12:00:05.500000Z",
        acquired_token_atomic=40_000,
        entry_cost_usd_atomic=25_000_000,
        entry_price_impact_bps=0,
        quote_provider="fixture",
        quote_version="remote-v1",
    )
    append_counterfactual_entry(conn, entry)
    alert_id = append_forward_alert_receipt(
        conn,
        state=engine.states[latest.launch_id],
        snapshot_id=latest.snapshot_id,
        observed_at="2026-07-26T12:00:05Z",
        clip_usd_micros=25_000_000,
        rule_version="route-control-v1",
        quote_provider="fixture",
        round_trip_return_bps=-200,
        truth_label="ROUTE CONTROL — PAPER ONLY",
    )
    alert = conn.execute(
        "SELECT alert_id,snapshot_id FROM op_forward_alerts WHERE alert_id=?",
        (alert_id,),
    ).fetchone()
    values = dict(
        alert_id=alert["alert_id"],
        entry_id=entry.entry_id,
        snapshot_id=alert["snapshot_id"],
        quote_observed_at="2026-07-26T12:00:05Z",
        quote_completed_at="2026-07-26T12:00:05Z",
        quote_age_ms=0,
        entry_effective_at=entry.entry_effective_at,
        provider="fixture",
        evaluator_version="alert-outcome-v3",
    )

    assert append_forward_alert_entry(conn, **values) is True
    assert append_forward_alert_entry(conn, **values) is False
    with pytest.raises(ImmutableConflict):
        append_forward_alert_entry(conn, **{**values, "provider": "changed"})
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE op_forward_alert_entries SET provider='changed' WHERE alert_id=?",
            (alert["alert_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "DELETE FROM op_forward_alert_entries WHERE alert_id=?",
            (alert["alert_id"],),
        )


def _pending_inner_pump(
    conn,
    *,
    forward_arm_min_launched_at=None,
    confirmation=ConfirmationState.CONFIRMED,
    paper_adapter_enabled=False,
    adaptive_v3_enabled=False,
):
    venue = VenueId.PUMPFUN_SOLANA
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 100_000_000_000},
        quote_decimals={venue: 9},
        fee_bps={venue: 100},
        entry_latency_ms={venue: 500},
        remote_probe=lambda *_: RemoteRoundTripProbe(
            False, False, "jupiter_quote_v1", 0, "REMOTE_QUOTE_PENDING"
        ),
        forward_arm_min_launched_at=forward_arm_min_launched_at,
        paper_adapter_enabled=paper_adapter_enabled,
        **(
            {"adaptive_v3_enabled": True}
            if adaptive_v3_enabled
            else {}
        ),
    )
    for seq, second, kind, actor, quote, token, vtoken, vquote, rtoken in (
        (1, 0, EventKind.LAUNCH, None, None, None, 1_000_000, 500_000, 1_000_000),
        (2, 2, EventKind.BUY, "Buyer1", 10_000, -20_000, 980_000, 510_000, 950_000),
        (3, 4, EventKind.BUY, "Buyer2", 20_000, -40_000, 940_000, 550_000, 850_000),
        (4, 5, EventKind.BUY, "Buyer3", 600_000_000, -40_000, 900_000, 600_000, 700_000),
    ):
        observed = f"2026-07-26T12:00:0{second}Z"
        event_id = _persist_event(
            conn,
            seq=seq,
            observed_at=observed,
            kind=kind,
            actor=actor,
            quote=quote,
            token=token,
            network=NetworkId.SOLANA,
            venue=venue,
            token_id="TokenMint11111111111111111111111111111111",
            creator_id="Creator1111111111111111111111111111111",
            confirmation=confirmation,
        )
        latest = engine.observe_decoded(
            decoded=_decoded(
                kind,
                actor=actor,
                quote=quote,
                token=token,
                vtoken=vtoken,
                vquote=vquote,
                rtoken=rtoken,
            ),
            canonical_event_id=event_id,
            confirmation=confirmation,
            observed_at=observed,
        )
    assert latest.zone is RadarZone.INNER
    return engine, latest, venue


@pytest.mark.parametrize("adaptive_v3_enabled", [False, True])
def test_pons_exact_completion_creates_only_the_separate_pons_arm(
    tmp_path,
    adaptive_v3_enabled,
):
    from sniper.research.memecoin_opportunity.pons_4663 import PONS_WETH

    conn = connect(tmp_path / "op.db")
    venue = VenueId.PONS_V1_ROBINHOOD_4663
    token_id = "0x1111111111111111111111111111111111111111"
    pool_id = "0x2222222222222222222222222222222222222222"
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 2_500_000_000},
        quote_decimals={venue: 18},
        fee_bps={venue: 100},
        remote_probe=lambda *_: RemoteRoundTripProbe(
            False,
            False,
            "evm_quote_queue_v1",
            0,
            "EVM_QUOTE_PENDING",
        ),
        paper_adapter_enabled=True,
        adaptive_v3_enabled=adaptive_v3_enabled,
    )

    events = [(1, 0, EventKind.LAUNCH, None, 0)]
    events.extend(
        (seq + 1, second, EventKind.BUY, f"0xbuyer{seq:02d}", quote)
        for seq, (second, quote) in enumerate(
            ((1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (11, 1),
             (12, 100), (13, 100), (14, 100), (20, 100)),
            start=1,
        )
    )
    for seq, second, kind, actor, quote in events:
        observed = f"2026-08-07T12:00:{second:02d}Z"
        event_id = _persist_event(
            conn,
            seq=seq,
            observed_at=observed,
            kind=kind,
            actor=actor,
            quote=quote if kind is EventKind.BUY else None,
            token=-1_000 if kind is EventKind.BUY else None,
            network=NetworkId.ROBINHOOD_4663,
            venue=venue,
            token_id=token_id,
            creator_id="0x3333333333333333333333333333333333333333",
            launch_id="pons-launch",
            lifecycle=LifecycleState.ACTIVE_POOL,
        )
        decoded = DecodedVenueEvent(
            event_kind=kind,
            lifecycle=LifecycleState.ACTIVE_POOL,
            token_id=token_id,
            creator_id=(
                "0x3333333333333333333333333333333333333333"
                if kind is EventKind.LAUNCH else None
            ),
            actor_id=actor,
            recipient_id=actor,
            pool_id=pool_id,
            quote_delta_atomic=quote if kind is EventKind.BUY else None,
            token_delta_atomic=-1_000 if kind is EventKind.BUY else None,
            facts={
                "quote_mint": PONS_WETH,
                "token_is_token0": True,
                "sqrt_price_x96": 2**96,
                "liquidity": 1_000_000,
                "tick": 0,
            },
            decoder_version="pons-v1-fixture",
        )
        latest = engine.observe_decoded(
            decoded=decoded,
            canonical_event_id=event_id,
            confirmation=ConfirmationState.CONFIRMED,
            observed_at=observed,
        )

    assert latest.zone is RadarZone.INNER
    point = engine.last_observations["pons-launch"]
    stale = RemoteRoundTripProbe(
        True,
        True,
        "pons_v3_quoter_v1",
        5,
        quote_in_atomic=10**16,
        quote_mint=PONS_WETH,
        tokens_out_atomic=1_000_000,
        immediate_exit_quote_atomic=9_600_000_000_000_000,
        round_trip_return_bps=-400,
        launch_id="pons-launch",
        venue_id=venue.value,
        token_id=token_id,
        state_fingerprint="superseded-state",
        completed_at="2026-08-07T12:00:20.050000Z",
        block_identity="0x122",
        route_binding={"pool_id": pool_id},
    )
    assert engine.observe_remote_completion(
        stale,
        observed_at="2026-08-07T12:00:20.050000Z",
    ) is None
    normal_at = "2026-08-07T12:00:20.060000Z"
    normal_event_id = _persist_event(
        conn,
        seq=99,
        observed_at=normal_at,
        kind=EventKind.BUY,
        actor="0xnormalbuyer",
        quote=1,
        token=-1_000,
        network=NetworkId.ROBINHOOD_4663,
        venue=venue,
        token_id=token_id,
        creator_id="0x3333333333333333333333333333333333333333",
        launch_id="pons-launch",
        lifecycle=LifecycleState.ACTIVE_POOL,
    )
    engine.observe_decoded(
        decoded=DecodedVenueEvent(
            event_kind=EventKind.BUY,
            lifecycle=LifecycleState.ACTIVE_POOL,
            token_id=token_id,
            actor_id="0xnormalbuyer",
            recipient_id="0xnormalbuyer",
            pool_id=pool_id,
            quote_delta_atomic=1,
            token_delta_atomic=-1_000,
            facts={
                "quote_mint": PONS_WETH,
                "token_is_token0": True,
                "sqrt_price_x96": 2**96,
                "liquidity": 1_000_000,
                "tick": 0,
            },
            decoder_version="pons-v1-fixture",
        ),
        canonical_event_id=normal_event_id,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at=normal_at,
    )
    assert engine.last_observations["pons-launch"].state_fingerprint \
        != point.state_fingerprint
    result = engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "pons_v3_quoter_v1",
            5,
            quote_in_atomic=10**16,
            quote_mint=PONS_WETH,
            tokens_out_atomic=1_000_000,
            immediate_exit_quote_atomic=9_600_000_000_000_000,
            round_trip_return_bps=-400,
            launch_id="pons-launch",
            venue_id=venue.value,
            token_id=token_id,
            state_fingerprint=point.state_fingerprint,
            requested_state_fingerprint=point.state_fingerprint,
            completed_at="2026-08-07T12:00:20.100000Z",
            block_identity="0x123",
            route_binding={"pool_id": pool_id},
        ),
        observed_at="2026-08-07T12:00:20.100000Z",
    )

    assert result is not None
    assert result.would_fire is True
    alerts = conn.execute(
        "SELECT rule_version FROM op_forward_alerts ORDER BY rule_version"
    ).fetchall()
    assert [row["rule_version"] for row in alerts] == ["pons-confirmation-v1"]
    request = conn.execute(
        "SELECT venue_id,rule_version FROM op_paper_fill_requests"
    ).fetchone()
    assert tuple(request) == (venue.value, "pons-confirmation-v1")


def test_retired_bags_venue_produces_no_alert_at_all(tmp_path):
    """`bags_robinhood_4663` is HISTORICAL_ONLY since 2026-08-19.

    This test used to assert that a bags exact completion emitted
    `bags-confirmation-v1`. The venue is retired, so the correct assertion is the
    opposite: the same completion must now produce no receipt at all. It captured
    nothing in its whole life -- 0 alerts and 0 paper outcomes -- while its chain-4663
    poller failed every call against an unroutable backend.
    """
    from sniper.research.memecoin_opportunity.pons_4663 import PONS_WETH

    conn = connect(tmp_path / "op.db")
    venue = VenueId.BAGS_ROBINHOOD_4663
    token_id = "0x4444444444444444444444444444444444444444"
    curve_id = "0x5555555555555555555555555555555555555555"
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 2_500_000_000},
        quote_decimals={venue: 18},
        fee_bps={venue: 100},
        remote_probe=lambda *_: RemoteRoundTripProbe(
            False, False, "evm_quote_queue_v1", 0, "EVM_QUOTE_PENDING"
        ),
    )
    events = [(1, 0, EventKind.LAUNCH, None, 0)]
    events.extend(
        (seq + 1, second, EventKind.BUY, f"0xbuyer{seq:02d}", quote)
        for seq, (second, quote) in enumerate(
            ((1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (11, 1),
             (12, 100), (13, 100), (14, 100), (20, 100)),
            start=1,
        )
    )
    for seq, second, kind, actor, quote in events:
        observed = f"2026-08-07T12:01:{second:02d}Z"
        event_id = _persist_event(
            conn,
            seq=seq,
            observed_at=observed,
            kind=kind,
            actor=actor,
            quote=quote if kind is EventKind.BUY else None,
            token=-1_000 if kind is EventKind.BUY else None,
            venue=venue,
            token_id=token_id,
            creator_id="0x6666666666666666666666666666666666666666",
            launch_id="bags-launch",
        )
        decoded = DecodedVenueEvent(
            event_kind=kind,
            lifecycle=LifecycleState.PRE_MIGRATION,
            token_id=token_id,
            curve_id=curve_id,
            creator_id=(
                "0x6666666666666666666666666666666666666666"
                if kind is EventKind.LAUNCH else None
            ),
            actor_id=actor,
            recipient_id=actor,
            quote_delta_atomic=quote if kind is EventKind.BUY else None,
            token_delta_atomic=-1_000 if kind is EventKind.BUY else None,
            virtual_token_reserves=1_000_000_000_000,
            virtual_quote_reserves=500_000_000_000_000_000_000,
            real_token_reserves=600_000_000_000,
            real_quote_reserves=100_000_000_000_000_000_000,
            progress_bps=2_500,
            facts={"quote_mint": PONS_WETH},
            decoder_version="bags-v1-fixture",
        )
        latest = engine.observe_decoded(
            decoded=decoded,
            canonical_event_id=event_id,
            confirmation=ConfirmationState.CONFIRMED,
            observed_at=observed,
        )

    assert latest.zone is RadarZone.INNER
    state = engine.states["bags-launch"]
    point = engine.last_observations["bags-launch"]
    assert state.quote_mint == PONS_WETH
    result = engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "bags_curve_eth_call_v2",
            5,
            quote_in_atomic=10**16,
            quote_mint=PONS_WETH,
            tokens_out_atomic=1_000_000,
            immediate_exit_quote_atomic=9_600_000_000_000_000,
            round_trip_return_bps=-400,
            launch_id="bags-launch",
            venue_id=venue.value,
            token_id=token_id,
            state_fingerprint=point.state_fingerprint,
            completed_at="2026-08-07T12:01:20.100000Z",
            block_identity="0x123",
            route_binding={"curve_id": curve_id},
        ),
        observed_at="2026-08-07T12:01:20.100000Z",
    )

    # retired venue: no verdict, and above all no receipt
    assert result is None
    assert conn.execute("SELECT COUNT(*) FROM op_forward_alerts").fetchone()[0] == 0


def test_quote_completion_with_bad_cost_stays_inner_and_records_only_control(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(conn)
    canonical_before = conn.execute("SELECT COUNT(*) FROM op_canonical_events").fetchone()[0]
    point = engine.last_observations[latest.launch_id]
    result = RemoteRoundTripProbe(
        True,
        True,
        "jupiter_quote_v1",
        200,
        quote_in_atomic=1_000_000_000,
        tokens_out_atomic=123_456,
        immediate_exit_quote_atomic=990_000_000,
        round_trip_return_bps=-1_300,
        launch_id=latest.launch_id,
        venue_id=venue.value,
        token_id="0xabc",
        state_fingerprint=point.state_fingerprint,
        queued_at="2026-07-26T12:00:05Z",
        entry_quoted_at="2026-07-26T12:00:05.050000Z",
        exit_quoted_at="2026-07-26T12:00:05.200000Z",
        completed_at="2026-07-26T12:00:05.200000Z",
        consumed_at="2026-07-26T12:00:05.200000Z",
        queue_delay_ms=0,
        pair_age_ms=150,
        result_age_ms=0,
    )

    refreshed = engine.observe_remote_completion(
        result,
        observed_at="2026-07-26T12:00:05.200000Z",
    )

    assert refreshed is not None
    assert refreshed.zone is RadarZone.INNER
    assert refreshed.would_fire is False
    assert conn.execute("SELECT COUNT(*) FROM op_canonical_events").fetchone()[0] == canonical_before
    assert conn.execute(
        "SELECT GROUP_CONCAT(rule_version) FROM op_forward_alerts"
    ).fetchone()[0] == "route-control-v1"
    linked = conn.execute(
        "SELECT a.quote_completed_at,a.quote_age_ms,e.acquired_token_atomic,e.entry_effective_at "
        "FROM op_forward_alert_entries a JOIN op_counterfactual_entries e "
        "ON e.entry_id=a.entry_id"
    ).fetchone()
    assert tuple(linked) == (
        "2026-07-26T12:00:05.200000Z",
        0,
        "123456",
        "2026-07-26T12:00:05.700000Z",
    )


def test_qualifying_quote_completion_records_control_and_ignition_once(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(conn)
    point = engine.last_observations[latest.launch_id]
    result = RemoteRoundTripProbe(
        True,
        True,
        "jupiter_quote_v1",
        200,
        quote_in_atomic=1_000_000_000,
        tokens_out_atomic=123_456,
        immediate_exit_quote_atomic=900_000_000,
        round_trip_return_bps=-1_000,
        launch_id=latest.launch_id,
        venue_id=venue.value,
        token_id="0xabc",
        state_fingerprint=point.state_fingerprint,
        completed_at="2026-07-26T12:00:05.200000Z",
    )

    refreshed = engine.observe_remote_completion(
        result,
        observed_at="2026-07-26T12:00:05.200000Z",
    )

    assert refreshed is not None
    assert refreshed.zone is RadarZone.CENTER
    assert refreshed.would_fire is True
    assert conn.execute(
        "SELECT GROUP_CONCAT(rule_version, ',') FROM "
        "(SELECT rule_version FROM op_forward_alerts ORDER BY rule_version)"
    ).fetchone()[0] == "pump-ignition-v1,route-control-v1"
    assert conn.execute("SELECT COUNT(*) FROM op_forward_alert_entries").fetchone()[0] == 2
    feature_rows = conn.execute(
        "SELECT feature_version,feature_json FROM op_forward_alert_features ORDER BY alert_id"
    ).fetchall()
    assert len(feature_rows) == 2
    assert {row["feature_version"] for row in feature_rows} == {"confirmation-features-v1"}
    features = json.loads(feature_rows[0]["feature_json"])
    assert features["effective_fee_bps"] == 100
    assert features["quote_mint"]
    assert features["launch_age_ms"] == 5_200
    assert "eligible_buyer_count" in features
    assert not [key for key in features if key.startswith("independent_")]


def test_confirmation_completion_records_one_v3_receipt_with_condition_labels(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(conn)
    engine.states[latest.launch_id] = replace(
        engine.states[latest.launch_id],
        launched_at="2026-07-26T11:59:50Z",
    )
    engine.prior_interval_features[latest.launch_id] = {
        "capital_velocity_atomic_per_s": 500_000_000,
        "buyer_velocity_per_s_micros": 400_000,
        "effective_buyer_hhi_bps": 1_800,
    }
    engine.last_interval_features[latest.launch_id].update(
        capital_velocity_atomic_per_s=600_000_000,
        buyer_velocity_per_s_micros=600_000,
        effective_buyer_hhi_bps=1_500,
        effective_buyer_count=10,
        new_eligible_buyers=2,
        sell_pressure_bps=2_000,
        creator_sale_since_prior=False,
    )
    point = engine.last_observations[latest.launch_id]

    refreshed = engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "jupiter_quote_v2",
            200,
            quote_in_atomic=1_000_000_000,
            quote_mint=engine.states[latest.launch_id].quote_mint,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=900_000_000,
            round_trip_return_bps=-750,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id="0xabc",
            state_fingerprint=point.state_fingerprint,
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.200000Z",
    )

    assert refreshed is not None and refreshed.would_fire is True
    rules = {
        row[0]
        for row in conn.execute("SELECT rule_version FROM op_forward_alerts").fetchall()
    }
    assert rules == {"pump-confirmation-truth-v3"}
    assert "pump-confirmation-v1" not in rules
    assert conn.execute("SELECT COUNT(*) FROM op_forward_alert_entries").fetchone()[0] == 1
    feature = conn.execute(
        "SELECT feature_version,feature_json FROM op_forward_alert_features"
    ).fetchone()
    assert feature["feature_version"] == "confirmation-features-v2"
    feature_json = json.loads(feature["feature_json"])
    assert feature_json["confirmation_independent_pass"] is True
    assert feature_json["confirmation_clean_pass"] is True
    assert feature_json["confirmation_low_friction_pass"] is True
    assert feature_json["launch_mode"] == "standard"

    event_id = _persist_event(
        conn,
        seq=5,
        observed_at="2026-07-26T12:00:06Z",
        kind=EventKind.BUY,
        actor="Buyer4",
        quote=50_000_000,
        token=-20_000,
        network=NetworkId.SOLANA,
        venue=venue,
        token_id="TokenMint11111111111111111111111111111111",
        creator_id="Creator1111111111111111111111111111111",
    )
    engine.observe_decoded(
        decoded=_decoded(
            EventKind.BUY,
            actor="Buyer4",
            quote=50_000_000,
            token=-20_000,
            vtoken=880_000,
            vquote=650_000,
            rtoken=650_000,
        ),
        canonical_event_id=event_id,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:06Z",
    )
    engine.last_verdicts[latest.launch_id] = DirectionalVerdict(
        RadarZone.INNER, False, ("REMOTE_QUOTE_PENDING",)
    )
    next_point = engine.last_observations[latest.launch_id]
    delayed = engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "jupiter_quote_v1",
            200,
            quote_in_atomic=1_000_000_000,
            tokens_out_atomic=100_000,
            immediate_exit_quote_atomic=910_000_000,
            round_trip_return_bps=-650,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id="0xabc",
            state_fingerprint=next_point.state_fingerprint,
            completed_at="2026-07-26T12:00:06.200000Z",
        ),
        observed_at="2026-07-26T12:00:06.200000Z",
    )

    assert delayed is not None and delayed.would_fire is False
    delayed_row = conn.execute(
        "SELECT f.feature_json FROM op_forward_alerts a "
        "JOIN op_forward_alert_features f USING(alert_id) "
        "WHERE a.rule_version='pump-confirmation-delayed-v2'"
    ).fetchone()
    assert delayed_row is not None
    assert json.loads(delayed_row[0])["paired_truth_alert_id"]


def test_delayed_control_survives_a_non_landmark_event_moving_the_state(tmp_path):
    # A buy that crosses no time/progress landmark advances the launch state but
    # leaves last_interval_features on the prior landmark. The delayed control arm
    # then persists the newer state alongside those older features — the two must
    # not disagree inside one snapshot.
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(conn)
    engine.states[latest.launch_id] = replace(
        engine.states[latest.launch_id],
        launched_at="2026-07-26T11:59:50Z",
    )
    engine.prior_interval_features[latest.launch_id] = {
        "capital_velocity_atomic_per_s": 500_000_000,
        "buyer_velocity_per_s_micros": 400_000,
        "effective_buyer_hhi_bps": 1_800,
    }
    engine.last_interval_features[latest.launch_id].update(
        capital_velocity_atomic_per_s=600_000_000,
        buyer_velocity_per_s_micros=600_000,
        effective_buyer_hhi_bps=1_500,
        effective_buyer_count=10,
        new_eligible_buyers=2,
        sell_pressure_bps=2_000,
        creator_sale_since_prior=False,
    )
    point = engine.last_observations[latest.launch_id]
    engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "jupiter_quote_v1",
            200,
            quote_in_atomic=1_000_000_000,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=900_000_000,
            round_trip_return_bps=-750,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id="0xabc",
            state_fingerprint=point.state_fingerprint,
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.200000Z",
    )
    for seq, second, actor in ((5, "05.300000", "Buyer4"), (6, "05.400000", "Buyer5")):
        event_id = _persist_event(
            conn,
            seq=seq,
            observed_at=f"2026-07-26T12:00:{second}Z",
            kind=EventKind.BUY,
            actor=actor,
            quote=10_000,
            token=-20_000,
            network=NetworkId.SOLANA,
            venue=venue,
            token_id="TokenMint11111111111111111111111111111111",
            creator_id="Creator1111111111111111111111111111111",
        )
        engine.observe_decoded(
            decoded=_decoded(
                EventKind.BUY,
                actor=actor,
                quote=10_000,
                token=-20_000,
                vtoken=899_000,
                vquote=601_000,
                rtoken=699_000,
            ),
            canonical_event_id=event_id,
            confirmation=ConfirmationState.CONFIRMED,
            observed_at=f"2026-07-26T12:00:{second}Z",
        )
    # Buyer5 crossed no landmark, so the features still describe the Buyer4 state
    # while the launch state has gained a buyer.
    assert engine.last_landmark_states[latest.launch_id] is not engine.states[latest.launch_id]
    assert len(engine.states[latest.launch_id].unique_buyers) > len(
        engine.last_landmark_states[latest.launch_id].unique_buyers
    )

    engine.last_verdicts[latest.launch_id] = DirectionalVerdict(
        RadarZone.INNER, False, ("REMOTE_QUOTE_PENDING",)
    )
    next_point = engine.last_observations[latest.launch_id]
    delayed = engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "jupiter_quote_v1",
            200,
            quote_in_atomic=1_000_000_000,
            tokens_out_atomic=100_000,
            immediate_exit_quote_atomic=910_000_000,
            round_trip_return_bps=-650,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id="0xabc",
            state_fingerprint=next_point.state_fingerprint,
            completed_at="2026-07-26T12:00:05.400000Z",
        ),
        observed_at="2026-07-26T12:00:05.400000Z",
    )

    assert delayed is not None
    snapshot = json.loads(
        conn.execute(
            "SELECT state_json FROM op_state_snapshots WHERE snapshot_id=?",
            (delayed.snapshot_id,),
        ).fetchone()[0]
    )
    state = engine.states[latest.launch_id]
    assert snapshot["effective_buyer_count"] == len(state.unique_buyers)
    assert snapshot["progress_bps"] == state.progress_bps


def test_fresh_remote_quote_pairs_with_latest_eligible_state_after_market_moves(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(conn)

    refreshed = engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "jupiter_quote_v1",
            200,
            quote_in_atomic=1_000_000_000,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=900_000_000,
            round_trip_return_bps=-1_000,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id="0xabc",
            state_fingerprint="state-before-the-quote-pair-completed",
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.200000Z",
    )

    assert refreshed is not None
    assert refreshed.zone is RadarZone.CENTER
    assert conn.execute(
        "SELECT GROUP_CONCAT(rule_version, ',') FROM "
        "(SELECT rule_version FROM op_forward_alerts ORDER BY rule_version)"
    ).fetchone()[0] == "pump-ignition-v1,route-control-v1"


def test_remote_quote_older_than_latest_state_cannot_seed_forward_receipts(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(conn)

    refreshed = engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "jupiter_quote_v1",
            200,
            quote_in_atomic=1_000_000_000,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=900_000_000,
            round_trip_return_bps=-1_000,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id="0xabc",
            state_fingerprint="state-before-latest-observation",
            completed_at="2026-07-26T12:00:04.900000Z",
        ),
        observed_at="2026-07-26T12:00:05.200000Z",
    )

    assert refreshed is None
    assert conn.execute("SELECT COUNT(*) FROM op_forward_alerts").fetchone()[0] == 0


def test_jupiter_v2_quote_must_match_launch_quote_asset(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(conn)

    refreshed = engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "jupiter_quote_v2",
            200,
            quote_in_atomic=1_000_000_000,
            quote_mint=PUMP_USDC_QUOTE_MINT,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=900_000_000,
            round_trip_return_bps=-1_000,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id="0xabc",
            state_fingerprint=engine.last_observations[latest.launch_id].state_fingerprint,
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.200000Z",
    )

    assert refreshed is None
    assert conn.execute("SELECT COUNT(*) FROM op_forward_alerts").fetchone()[0] == 0


def test_finalized_snapshots_backfill_the_path_before_the_next_live_event(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(
        conn,
        confirmation=ConfirmationState.TENTATIVE,
    )
    point = engine.last_observations[latest.launch_id]
    engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "jupiter_quote_v1",
            200,
            quote_in_atomic=1_000_000_000,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=900_000_000,
            round_trip_return_bps=-1_000,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id="0xabc",
            state_fingerprint=point.state_fingerprint,
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.200000Z",
    )
    event_id = _persist_event(
        conn,
        seq=5,
        observed_at="2026-07-26T12:00:06Z",
        kind=EventKind.BUY,
        actor="Buyer4",
        quote=30_000,
        token=-50_000,
        confirmation=ConfirmationState.TENTATIVE,
        network=NetworkId.SOLANA,
        venue=venue,
        token_id="TokenMint11111111111111111111111111111111",
        creator_id="Creator1111111111111111111111111111111",
    )
    engine.observe_decoded(
        decoded=_decoded(
            EventKind.BUY,
            actor="Buyer4",
            quote=30_000,
            token=-50_000,
            vtoken=850_000,
            vquote=630_000,
            rtoken=650_000,
        ),
        canonical_event_id=event_id,
        confirmation=ConfirmationState.TENTATIVE,
        observed_at="2026-07-26T12:00:06Z",
    )
    assert conn.execute("SELECT COUNT(*) FROM op_executable_observations").fetchone()[0] == 0
    reconcile_confirmation(
        conn,
        canonical_event_id="event-4",
        confirmation=ConfirmationState.FINALIZED,
        reconciled_at="2026-07-26T12:00:07Z",
    )
    reconcile_confirmation(
        conn,
        canonical_event_id=event_id,
        confirmation=ConfirmationState.FINALIZED,
        reconciled_at="2026-07-26T12:00:07.100000Z",
    )

    engine.finalize_due_outcomes("2026-07-26T12:00:08Z")

    observations = conn.execute(
        "SELECT observed_at,elapsed_ms FROM op_executable_observations "
        "ORDER BY observed_at,entry_id"
    ).fetchall()
    assert observations
    assert {row["observed_at"] for row in observations} == {"2026-07-26T12:00:06Z"}
    assert {row["elapsed_ms"] for row in observations} == {300}

    snapshot = conn.execute(
        "SELECT snapshot_id,observed_at,state_json FROM op_state_snapshots "
        "WHERE observed_at='2026-07-26T12:00:06Z'"
    ).fetchone()
    record_launch_outcomes(
        conn,
        state=launch_state_from_json(snapshot["state_json"]),
        canonical_state_id=snapshot["snapshot_id"],
        observed_at=snapshot["observed_at"],
        quote_usd_price_micros=90_000_000_000,
        quote_decimals=9,
        fee_bps=100,
    )
    assert conn.execute("SELECT COUNT(*) FROM op_executable_observations").fetchone()[0] == len(observations)


def test_pre_activation_launch_cannot_seed_forward_receipts(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(
        conn,
        forward_arm_min_launched_at="2026-07-26T12:00:06Z",
    )
    point = engine.last_observations[latest.launch_id]

    result = engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "jupiter_quote_v1",
            200,
            quote_in_atomic=1_000_000_000,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=900_000_000,
            round_trip_return_bps=-1_000,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id="0xabc",
            state_fingerprint=point.state_fingerprint,
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.200000Z",
    )

    assert result is None
    assert conn.execute("SELECT COUNT(*) FROM op_forward_alerts").fetchone()[0] == 0


def test_capture_only_venue_cannot_seed_forward_receipts(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine, latest, _venue = _pending_inner_pump(conn)
    state = replace(
        engine.states[latest.launch_id],
        venue_id=VenueId.PONS_V1_ROBINHOOD_4663,
    )
    engine.states[latest.launch_id] = state
    point = engine.last_observations[latest.launch_id]

    result = engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "fixture",
            200,
            quote_in_atomic=1_000_000_000,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=900_000_000,
            round_trip_return_bps=-1_000,
            launch_id=latest.launch_id,
            venue_id=state.venue_id.value,
            token_id=state.token_id,
            state_fingerprint=point.state_fingerprint,
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.200000Z",
    )

    assert result is None
    assert conn.execute("SELECT COUNT(*) FROM op_forward_alerts").fetchone()[0] == 0


def test_candidate_entry_keeps_300_second_path_after_primary_outcome(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(conn)
    point = engine.last_observations[latest.launch_id]
    engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "jupiter_quote_v1",
            200,
            quote_in_atomic=1_000_000_000,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=900_000_000,
            round_trip_return_bps=-1_000,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id="0xabc",
            state_fingerprint=point.state_fingerprint,
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.200000Z",
    )
    engine.finalize_due_outcomes("2026-07-26T12:01:06Z")
    assert conn.execute(
        "SELECT COUNT(*) FROM op_competing_outcomes outcome "
        "JOIN op_forward_alert_entries link USING(entry_id)"
    ).fetchone()[0] == 2
    due = conn.execute(
        "SELECT observation.elapsed_ms,observation.net_return_bps,"
        "outcome.terminal_net_return_bps,observation.provider "
        "FROM op_forward_alerts alert "
        "JOIN op_forward_alert_entries link USING(alert_id) "
        "JOIN op_competing_outcomes outcome USING(entry_id) "
        "JOIN op_executable_observations observation "
        "ON observation.observation_id=outcome.terminal_observation_id "
        "WHERE alert.rule_version='pump-ignition-v1'"
    ).fetchone()
    assert due["elapsed_ms"] == 60_000
    assert due["terminal_net_return_bps"] == due["net_return_bps"]
    assert due["provider"] == "opportunity-path-v4"

    engine.finalize_due_outcomes("2026-07-26T12:05:06Z")

    counts = dict(
        conn.execute(
            "SELECT a.rule_version,COUNT(o.observation_id) AS n "
            "FROM op_forward_alerts a "
            "JOIN op_forward_alert_entries link USING(alert_id) "
            "JOIN op_executable_observations o USING(entry_id) "
            "GROUP BY a.rule_version"
        ).fetchall()
    )
    assert counts == {"pump-ignition-v1": 2, "route-control-v1": 1}


def test_emergency_retention_keeps_candidate_until_300_second_evidence(tmp_path):
    conn = connect(tmp_path / "op.db")
    insights = connect_insights(tmp_path / "insights.db")
    engine, latest, venue = _pending_inner_pump(conn)
    point = engine.last_observations[latest.launch_id]
    engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "jupiter_quote_v1",
            200,
            quote_in_atomic=1_000_000_000,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=950_000_000,
            round_trip_return_bps=-500,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id="0xabc",
            state_fingerprint=point.state_fingerprint,
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.200000Z",
    )
    engine.finalize_due_outcomes("2026-07-26T12:01:06Z")
    forward_outcomes = conn.execute(
        "SELECT o.* FROM op_competing_outcomes o "
        "JOIN op_forward_alert_entries link USING(entry_id)"
    ).fetchall()
    with retention_delete_authority(conn):
        conn.execute(
            "DELETE FROM op_competing_outcomes WHERE entry_id IN ("
            "SELECT entry_id FROM op_forward_alert_entries)"
        )
    for row in forward_outcomes:
        values = list(row)
        values[2] = "timeout"
        conn.execute(
            f"INSERT INTO op_competing_outcomes VALUES({','.join('?' for _ in values)})",
            values,
        )

    early = cleanup_hot_ledger(
        conn,
        insights,
        now_iso="2026-07-26T12:03:00Z",
        retention_s=120,
        max_launches=25,
        max_rows=2_500,
    )

    assert early.deleted_launches == 0
    assert conn.execute(
        "SELECT 1 FROM op_launches WHERE launch_id=?", (latest.launch_id,)
    ).fetchone() is not None

    engine.finalize_due_outcomes("2026-07-26T12:05:06Z")
    extended = [tuple(row) for row in conn.execute(
        "SELECT link.entry_id,x.elapsed_ms,x.source_healthy FROM op_forward_alert_entries link "
        "LEFT JOIN op_executable_observations x USING(entry_id) ORDER BY link.entry_id,x.elapsed_ms"
    )]
    assert any(elapsed_ms == 300_000 and healthy for _, elapsed_ms, healthy in extended)
    mature = cleanup_hot_ledger(
        conn,
        insights,
        now_iso="2026-07-26T12:06:00Z",
        retention_s=120,
        max_launches=25,
        max_rows=2_500,
    )

    assert mature.deleted_launches == 1


def test_existing_exact_alert_makes_replayed_quote_completion_a_noop(tmp_path):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.PUMPFUN_SOLANA
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 100_000_000_000},
        quote_decimals={venue: 9},
        fee_bps={venue: 100},
        entry_latency_ms={venue: 500},
        remote_probe=lambda *_: RemoteRoundTripProbe(
            False, False, "jupiter_quote_v1", 0, "REMOTE_QUOTE_PENDING"
        ),
    )
    for seq, second, kind, actor, quote, token, vtoken, vquote in (
        (1, 0, EventKind.LAUNCH, None, None, None, 1_000_000, 500_000),
        (2, 2, EventKind.BUY, "Buyer1", 10_000, -20_000, 980_000, 510_000),
        (3, 3, EventKind.BUY, "Buyer2", 20_000, -40_000, 940_000, 550_000),
        (4, 5, EventKind.BUY, "Buyer3", 20_000, -40_000, 940_000, 550_000),
    ):
        observed = f"2026-07-26T12:00:0{second}Z"
        event_id = _persist_event(
            conn,
            seq=seq,
            observed_at=observed,
            kind=kind,
            actor=actor,
            quote=quote,
            token=token,
            network=NetworkId.SOLANA,
            venue=venue,
            token_id="TokenMint11111111111111111111111111111111",
            creator_id="Creator1111111111111111111111111111111",
        )
        latest = engine.observe_decoded(
            decoded=_decoded(
                kind, actor=actor, quote=quote, token=token,
                vtoken=vtoken, vquote=vquote,
            ),
            canonical_event_id=event_id,
            confirmation=ConfirmationState.CONFIRMED,
            observed_at=observed,
        )
    point = engine.last_observations[latest.launch_id]
    completion = RemoteRoundTripProbe(
        True, True, "jupiter_quote_v1", 200,
        quote_in_atomic=1_000_000_000,
        tokens_out_atomic=123_456,
        immediate_exit_quote_atomic=990_000_000,
        round_trip_return_bps=-100,
        launch_id=latest.launch_id,
        venue_id=venue.value,
        token_id="0xabc",
        state_fingerprint=point.state_fingerprint,
        completed_at="2026-07-26T12:00:05.200000Z",
    )
    assert engine.observe_remote_completion(
        completion, observed_at="2026-07-26T12:00:05.200000Z"
    ) is not None
    before = tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "op_forward_alerts", "op_forward_alert_entries",
            "op_counterfactual_entries", "op_state_snapshots",
        )
    )
    engine.last_verdicts[latest.launch_id] = DirectionalVerdict(
        RadarZone.INNER, False, ("REMOTE_QUOTE_PENDING",)
    )

    replayed = engine.observe_remote_completion(
        completion, observed_at="2026-07-26T12:00:05.300000Z"
    )

    assert replayed is None
    assert tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "op_forward_alerts", "op_forward_alert_entries",
            "op_counterfactual_entries", "op_state_snapshots",
        )
    ) == before


def test_quote_completion_for_superseded_state_cannot_fire(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={VenueId.BAGS_ROBINHOOD_4663: 1_000_000},
        quote_decimals={VenueId.BAGS_ROBINHOOD_4663: 3},
        fee_bps={VenueId.BAGS_ROBINHOOD_4663: 200},
    )

    assert engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "fixture",
            1,
            launch_id="missing",
            state_fingerprint="superseded",
            completed_at="2026-07-26T12:00:00Z",
        ),
        observed_at="2026-07-26T12:00:00Z",
    ) is None
    assert conn.execute("SELECT COUNT(*) FROM op_forward_alerts").fetchone()[0] == 0


def test_non_landmark_event_preserves_latest_directional_zone(tmp_path):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.BAGS_ROBINHOOD_4663
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 1_000_000},
        quote_decimals={venue: 3},
        fee_bps={venue: 200},
        remote_probe=lambda state, quote: RemoteRoundTripProbe(True, True, "fixture", 12),
    )
    events = (
        (1, "2026-07-26T12:00:00Z", EventKind.LAUNCH, None, None, None, 1_000_000, 500_000),
        (2, "2026-07-26T12:00:02Z", EventKind.BUY, "0xbuyer1", 10_000, -20_000, 980_000, 510_000),
        (3, "2026-07-26T12:00:03Z", EventKind.BUY, "0xbuyer2", 20_000, -40_000, 960_000, 530_000),
        (4, "2026-07-26T12:00:05Z", EventKind.BUY, "0xbuyer3", 20_000, -40_000, 940_000, 550_000),
    )
    for seq, observed_at, kind, actor, quote, token, vtoken, vquote in events:
        event_id = _persist_event(
            conn, seq=seq, observed_at=observed_at, kind=kind,
            actor=actor, quote=quote, token=token,
        )
        latest = engine.observe_decoded(
            decoded=_decoded(
                kind, actor=actor, quote=quote, token=token,
                vtoken=vtoken, vquote=vquote,
            ),
            canonical_event_id=event_id,
            confirmation=ConfirmationState.CONFIRMED,
            observed_at=observed_at,
        )
    assert latest.zone is RadarZone.INNER

    event_id = _persist_event(
        conn, seq=5, observed_at="2026-07-26T12:00:05.100000Z",
        kind=EventKind.BUY, actor="0xbuyer4", quote=1_000, token=-2_000,
    )
    between = engine.observe_decoded(
        decoded=_decoded(
            EventKind.BUY, actor="0xbuyer4", quote=1_000, token=-2_000,
            vtoken=938_000, vquote=551_000,
        ),
        canonical_event_id=event_id,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:05.100000Z",
    )

    assert between.zone is RadarZone.INNER
    assert between.would_fire is False


def test_non_landmark_creator_sell_is_immediate_outer_interrupt(tmp_path):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.BAGS_ROBINHOOD_4663
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 1_000_000},
        quote_decimals={venue: 3},
        fee_bps={venue: 200},
    )
    launch = _persist_event(
        conn, seq=1, observed_at="2026-07-26T12:00:00Z", kind=EventKind.LAUNCH
    )
    engine.observe_decoded(
        decoded=_decoded(EventKind.LAUNCH), canonical_event_id=launch,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:00Z",
    )
    sell = _persist_event(
        conn, seq=2, observed_at="2026-07-26T12:00:00.100000Z",
        kind=EventKind.SELL, actor="0xcreator", quote=-1_000, token=2_000,
    )
    interrupted = engine.observe_decoded(
        decoded=_decoded(
            EventKind.SELL, actor="0xcreator", quote=-1_000, token=2_000,
            vtoken=1_002_000, vquote=499_000,
        ),
        canonical_event_id=sell, confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:00.100000Z",
    )
    assert interrupted.zone is RadarZone.OUTER
    assert interrupted.would_fire is False


def test_recovered_launch_age_uses_chain_time_and_remains_ineligible(tmp_path):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.BAGS_ROBINHOOD_4663
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 1_000_000},
        quote_decimals={venue: 3},
        fee_bps={venue: 200},
    )
    launch = _persist_event(
        conn,
        seq=1,
        observed_at="2026-07-26T12:00:10Z",
        chain_at="2026-07-26T12:00:00Z",
        kind=EventKind.LAUNCH,
    )
    result = engine.observe_decoded(
        decoded=_decoded(EventKind.LAUNCH),
        canonical_event_id=launch,
        confirmation=ConfirmationState.FINALIZED,
        observed_at="2026-07-26T12:00:10Z",
        source_gap_open=True,
    )
    assert engine.states["launch-1"].launched_at == "2026-07-26T12:00:00Z"
    assert conn.execute(
        "SELECT 1 FROM op_landmark_states WHERE observed_event_id=?", (launch,)
    ).fetchone() is None
    assert result.zone is RadarZone.OUTER


def test_replacing_market_inputs_removes_stale_quote_material(tmp_path):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.BAGS_ROBINHOOD_4663
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 1_000_000},
        quote_decimals={venue: 3},
        fee_bps={venue: 200},
        network_cost_quote_atomic={venue: 5_000},
        quote_input_as_of={venue: "2026-07-26T12:00:00Z"},
    )

    engine.replace_market_inputs({})

    assert engine.quote_usd_price_micros == {}
    assert engine.quote_decimals == {}
    assert engine.fee_bps == {}
    assert engine.network_cost_quote_atomic == {}
    assert engine.quote_input_as_of == {}


def test_entry_uses_first_observed_market_state_after_latency(tmp_path):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.BAGS_ROBINHOOD_4663
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 1_000_000},
        quote_decimals={venue: 3},
        fee_bps={venue: 200},
        entry_latency_ms={venue: 100},
    )
    launch = _persist_event(
        conn, seq=1, observed_at="2026-07-26T12:00:00Z", kind=EventKind.LAUNCH
    )
    engine.observe_decoded(
        decoded=_decoded(EventKind.LAUNCH), canonical_event_id=launch,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:00Z",
    )
    buy = _persist_event(
        conn, seq=2, observed_at="2026-07-26T12:00:02Z", kind=EventKind.BUY,
        actor="0xbuyer1", quote=10_000, token=-20_000,
    )
    engine.observe_decoded(
        decoded=_decoded(
            EventKind.BUY, actor="0xbuyer1", quote=10_000, token=-20_000,
            vtoken=980_000, vquote=510_000,
        ),
        canonical_event_id=buy, confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:02Z",
    )
    landmark = conn.execute(
        "SELECT landmark_id FROM op_landmark_states WHERE observed_event_id=?", (buy,)
    ).fetchone()[0]
    assert conn.execute(
        "SELECT 1 FROM op_counterfactual_entries WHERE landmark_id=?", (landmark,)
    ).fetchone() is None

    middle = _persist_event(
        conn, seq=3, observed_at="2026-07-26T12:00:02.050000Z", kind=EventKind.BUY,
        actor="0xbuyer2", quote=40_000, token=-80_000,
    )
    engine.observe_decoded(
        decoded=_decoded(
            EventKind.BUY, actor="0xbuyer2", quote=40_000, token=-80_000,
            vtoken=900_000, vquote=550_000,
        ),
        canonical_event_id=middle, confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:02.050000Z",
    )
    inclusion_quote = engine._quotes(engine.states["launch-1"])[25_000_000]
    later = _persist_event(
        conn, seq=4, observed_at="2026-07-26T12:00:02.200000Z", kind=EventKind.BUY,
        actor="0xbuyer3", quote=20_000, token=-20_000,
    )
    engine.observe_decoded(
        decoded=_decoded(
            EventKind.BUY, actor="0xbuyer3", quote=20_000, token=-20_000,
            vtoken=880_000, vquote=570_000,
        ),
        canonical_event_id=later, confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:02.200000Z",
    )
    entry = conn.execute(
        "SELECT * FROM op_counterfactual_entries WHERE landmark_id=?", (landmark,)
    ).fetchone()
    assert entry["entry_observed_at"] == "2026-07-26T12:00:02Z"
    assert entry["entry_effective_at"] == "2026-07-26T12:00:02.100000Z"
    assert int(entry["acquired_token_atomic"]) == inclusion_quote.tokens_out_atomic


def test_restart_restores_memory_without_replaying_quotes_or_writing(tmp_path):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.PONS_V1_ROBINHOOD_4663
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 1_000_000},
        quote_decimals={venue: 3},
        fee_bps={venue: 200},
    )
    launch = _persist_event(
        conn, seq=1, observed_at="2026-07-26T12:00:00Z", kind=EventKind.LAUNCH , venue=venue
    )
    engine.observe_decoded(
        decoded=_decoded(EventKind.LAUNCH), canonical_event_id=launch,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:00Z",
    )
    before = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("op_state_snapshots", "op_predictions", "op_quote_observations")
    }
    restored = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 1_000_000},
        quote_decimals={venue: 3},
        fee_bps={venue: 200},
        remote_probe=lambda *_: (_ for _ in ()).throw(AssertionError("remote probe during restore")),
    )
    restored.observe_decoded = lambda **_: (_ for _ in ()).throw(
        AssertionError("event replay during restore")
    )

    restore_pipeline(conn, restored, VenueIdentityMap())

    after = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert after == before
    assert restored.states["launch-1"].token_id == "0xabc"
    assert restored.last_observations["launch-1"].observed_event_id == launch


def test_restore_pipeline_excludes_historical_only_state(tmp_path):
    conn = connect(tmp_path / "op.db")
    inputs = {
        VenueId.PONS_V1_ROBINHOOD_4663: 1_000_000,
        VenueId.BAGS_SOLANA: 1_000_000,
    }
    source = OpportunityEngine(
        conn,
        quote_usd_price_micros=inputs,
        quote_decimals={venue: 3 for venue in inputs},
        fee_bps={venue: 200 for venue in inputs},
    )
    active_event = _persist_event(
        conn,
        venue=VenueId.PONS_V1_ROBINHOOD_4663,
        seq=1,
        observed_at="2026-07-26T12:00:00Z",
        kind=EventKind.LAUNCH,
        launch_id="active-launch",
    )
    source.observe_decoded(
        decoded=_decoded(EventKind.LAUNCH),
        canonical_event_id=active_event,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:00Z",
    )
    historical_event = _persist_event(
        conn,
        seq=2,
        observed_at="2026-07-26T12:00:01Z",
        kind=EventKind.LAUNCH,
        network=NetworkId.SOLANA,
        venue=VenueId.BAGS_SOLANA,
        token_id="BagsMint",
        creator_id="BagsCreator",
        launch_id="historical-launch",
    )
    source.observe_decoded(
        decoded=replace(
            _decoded(EventKind.LAUNCH),
            token_id="BagsMint",
            curve_id="BagsPool",
            creator_id="BagsCreator",
        ),
        canonical_event_id=historical_event,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:01Z",
    )
    restored = OpportunityEngine(
        conn,
        quote_usd_price_micros=inputs,
        quote_decimals={venue: 3 for venue in inputs},
        fee_bps={venue: 200 for venue in inputs},
    )

    restore_pipeline(conn, restored, VenueIdentityMap())

    assert set(restored.states) == {"active-launch"}


def test_pending_confirmations_exclude_historical_only_venues(tmp_path):
    conn = connect(tmp_path / "op.db")
    active_event = _persist_event(
        conn,
        venue=VenueId.PONS_V1_ROBINHOOD_4663,
        seq=1,
        observed_at="2026-07-26T12:00:00Z",
        kind=EventKind.LAUNCH,
        confirmation=ConfirmationState.TENTATIVE,
        launch_id="active-launch",
    )
    _persist_event(
        conn,
        seq=2,
        observed_at="2026-07-26T12:00:01Z",
        kind=EventKind.LAUNCH,
        confirmation=ConfirmationState.TENTATIVE,
        network=NetworkId.SOLANA,
        venue=VenueId.BAGS_SOLANA,
        token_id="BagsMint",
        creator_id="BagsCreator",
        launch_id="historical-launch",
    )

    pending = load_pending_confirmations(conn)

    assert pending == {"0xtx1": {active_event}}


def test_restart_restores_chain_age_not_delayed_receive_age(tmp_path):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.PONS_V1_ROBINHOOD_4663
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 1_000_000},
        quote_decimals={venue: 3},
        fee_bps={venue: 200},
    )
    launch = _persist_event(
        conn,
        venue=VenueId.PONS_V1_ROBINHOOD_4663,
        seq=1,
        observed_at="2026-07-26T12:00:10Z",
        chain_at="2026-07-26T12:00:00Z",
        kind=EventKind.LAUNCH,
    )
    engine.observe_decoded(
        decoded=_decoded(EventKind.LAUNCH),
        canonical_event_id=launch,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:10Z",
    )

    restored = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 1_000_000},
        quote_decimals={venue: 3},
        fee_bps={venue: 200},
    )
    restore_pipeline(conn, restored, VenueIdentityMap())

    assert restored.last_observations["launch-1"].age_ms == 0
    buy = _persist_event(
        conn,
        venue=VenueId.PONS_V1_ROBINHOOD_4663,
        seq=2,
        observed_at="2026-07-26T12:00:12Z",
        chain_at="2026-07-26T12:00:02Z",
        kind=EventKind.BUY,
        actor="0xbuyer1",
        quote=10_000,
        token=-20_000,
    )
    restored.observe_decoded(
        decoded=_decoded(
            EventKind.BUY,
            actor="0xbuyer1",
            quote=10_000,
            token=-20_000,
            vtoken=980_000,
            vquote=510_000,
        ),
        canonical_event_id=buy,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:12Z",
    )


def test_tentative_landmark_outcome_waits_for_append_only_confirmation(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={VenueId.BAGS_ROBINHOOD_4663: 1_000_000},
        quote_decimals={VenueId.BAGS_ROBINHOOD_4663: 3},
        fee_bps={VenueId.BAGS_ROBINHOOD_4663: 200},
    )
    launch = _persist_event(conn, seq=1, observed_at="2026-07-26T12:00:00Z", kind=EventKind.LAUNCH)
    engine.observe_decoded(
        decoded=_decoded(EventKind.LAUNCH), canonical_event_id=launch,
        confirmation=ConfirmationState.CONFIRMED, observed_at="2026-07-26T12:00:00Z",
    )
    buy = _persist_event(
        conn, seq=2, observed_at="2026-07-26T12:00:02Z", kind=EventKind.BUY,
        actor="0xbuyer1", quote=10_000, token=-20_000,
        confirmation=ConfirmationState.TENTATIVE,
    )
    engine.observe_decoded(
        decoded=_decoded(EventKind.BUY, actor="0xbuyer1", quote=10_000, token=-20_000, vtoken=980_000, vquote=510_000),
        canonical_event_id=buy, confirmation=ConfirmationState.TENTATIVE,
        observed_at="2026-07-26T12:00:02Z",
    )
    assert conn.execute("SELECT count(*) FROM op_counterfactual_entries").fetchone()[0] == 1
    engine.finalize_due_outcomes("2026-07-26T12:01:05Z")
    assert conn.execute("SELECT count(*) FROM op_competing_outcomes").fetchone()[0] == 0

    reconcile_confirmation(
        conn,
        canonical_event_id=buy,
        confirmation=ConfirmationState.CONFIRMED,
        reconciled_at="2026-07-26T12:01:06Z",
    )
    engine.finalize_due_outcomes("2026-07-26T12:01:06Z")
    assert conn.execute("SELECT outcome_kind FROM op_competing_outcomes").fetchone()[0] == "timeout"


def test_orphan_reconciliation_immediately_records_chain_reorg_outcome(tmp_path):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.BAGS_ROBINHOOD_4663
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 1_000_000},
        quote_decimals={venue: 3},
        fee_bps={venue: 200},
    )
    launch = _persist_event(
        conn, seq=1, observed_at="2026-07-26T12:00:00Z", kind=EventKind.LAUNCH
    )
    engine.observe_decoded(
        decoded=_decoded(EventKind.LAUNCH), canonical_event_id=launch,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:00Z",
    )
    buy = _persist_event(
        conn, seq=2, observed_at="2026-07-26T12:00:02Z", kind=EventKind.BUY,
        actor="0xbuyer1", quote=10_000, token=-20_000,
        confirmation=ConfirmationState.TENTATIVE,
    )
    engine.observe_decoded(
        decoded=_decoded(
            EventKind.BUY, actor="0xbuyer1", quote=10_000, token=-20_000,
            vtoken=980_000, vquote=510_000,
        ),
        canonical_event_id=buy, confirmation=ConfirmationState.TENTATIVE,
        observed_at="2026-07-26T12:00:02Z",
    )

    reconcile_confirmation(
        conn, canonical_event_id=buy, confirmation=ConfirmationState.ORPHANED,
        reconciled_at="2026-07-26T12:00:03Z",
    )

    assert conn.execute(
        "SELECT outcome_kind FROM op_competing_outcomes"
    ).fetchone()[0] == "chain_reorg"


def test_confirmation_reconciliation_recovers_after_correction_before_successor_crash(
    tmp_path, monkeypatch,
):
    conn = connect(tmp_path / "op.db")
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={VenueId.BAGS_ROBINHOOD_4663: 1_000_000},
        quote_decimals={VenueId.BAGS_ROBINHOOD_4663: 3},
        fee_bps={VenueId.BAGS_ROBINHOOD_4663: 200},
    )
    launch = _persist_event(conn, seq=1, observed_at="2026-07-26T12:00:00Z", kind=EventKind.LAUNCH)
    engine.observe_decoded(
        decoded=_decoded(EventKind.LAUNCH), canonical_event_id=launch,
        confirmation=ConfirmationState.CONFIRMED, observed_at="2026-07-26T12:00:00Z",
    )
    buy = _persist_event(
        conn, seq=2, observed_at="2026-07-26T12:00:02Z", kind=EventKind.BUY,
        actor="0xbuyer1", quote=10_000, token=-20_000,
        confirmation=ConfirmationState.TENTATIVE,
    )
    engine.observe_decoded(
        decoded=_decoded(EventKind.BUY, actor="0xbuyer1", quote=10_000, token=-20_000, vtoken=980_000, vquote=510_000),
        canonical_event_id=buy, confirmation=ConfirmationState.TENTATIVE,
        observed_at="2026-07-26T12:00:02Z",
    )
    real_append = reconciliation_module.append_landmark
    monkeypatch.setattr(
        reconciliation_module,
        "append_landmark",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError, match="crash"):
        reconcile_confirmation(
            conn, canonical_event_id=buy, confirmation=ConfirmationState.CONFIRMED,
            reconciled_at="2026-07-26T12:00:03Z",
        )
    monkeypatch.setattr(reconciliation_module, "append_landmark", real_append)

    result = reconcile_confirmation(
        conn, canonical_event_id=buy, confirmation=ConfirmationState.CONFIRMED,
        reconciled_at="2026-07-26T12:00:03Z",
    )
    assert len(result.successor_landmark_ids) == 1


def test_confirmation_reconciliation_can_progress_confirmed_to_finalized(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={VenueId.BAGS_ROBINHOOD_4663: 1_000_000},
        quote_decimals={VenueId.BAGS_ROBINHOOD_4663: 3},
        fee_bps={VenueId.BAGS_ROBINHOOD_4663: 200},
    )
    launch = _persist_event(
        conn, seq=1, observed_at="2026-07-26T12:00:00Z", kind=EventKind.LAUNCH
    )
    engine.observe_decoded(
        decoded=_decoded(EventKind.LAUNCH), canonical_event_id=launch,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:00Z",
    )
    buy = _persist_event(
        conn, seq=2, observed_at="2026-07-26T12:00:02Z", kind=EventKind.BUY,
        actor="0xbuyer1", quote=10_000, token=-20_000,
        confirmation=ConfirmationState.TENTATIVE,
    )
    engine.observe_decoded(
        decoded=_decoded(
            EventKind.BUY, actor="0xbuyer1", quote=10_000, token=-20_000,
            vtoken=980_000, vquote=510_000,
        ),
        canonical_event_id=buy, confirmation=ConfirmationState.TENTATIVE,
        observed_at="2026-07-26T12:00:02Z",
    )
    confirmed = reconcile_confirmation(
        conn, canonical_event_id=buy, confirmation=ConfirmationState.CONFIRMED,
        reconciled_at="2026-07-26T12:00:03Z",
    )
    finalized = reconcile_confirmation(
        conn, canonical_event_id=buy, confirmation=ConfirmationState.FINALIZED,
        reconciled_at="2026-07-26T12:00:04Z",
    )
    assert confirmed.correction_event_id != finalized.correction_event_id
    assert {
        row[0] for row in conn.execute(
            "SELECT confirmation FROM op_canonical_events WHERE correction_of=?", (buy,)
        )
    } == {"confirmed", "finalized"}


def test_source_gap_censors_open_counterfactual_at_gap_time(tmp_path):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.BAGS_ROBINHOOD_4663
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 1_000_000},
        quote_decimals={venue: 3},
        fee_bps={venue: 200},
    )
    launch = _persist_event(conn, seq=1, observed_at="2026-07-26T12:00:00Z", kind=EventKind.LAUNCH)
    engine.observe_decoded(
        decoded=_decoded(EventKind.LAUNCH), canonical_event_id=launch,
        confirmation=ConfirmationState.CONFIRMED, observed_at="2026-07-26T12:00:00Z",
    )
    buy = _persist_event(
        conn, seq=2, observed_at="2026-07-26T12:00:02Z", kind=EventKind.BUY,
        actor="0xbuyer1", quote=10_000, token=-20_000,
    )
    engine.observe_decoded(
        decoded=_decoded(EventKind.BUY, actor="0xbuyer1", quote=10_000, token=-20_000, vtoken=980_000, vquote=510_000),
        canonical_event_id=buy, confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:02Z",
    )

    engine.censor_gapped_outcomes(
        "2026-07-26T12:00:03Z", source_healthy_by_venue={venue: False}
    )

    row = conn.execute(
        "SELECT outcome_kind,elapsed_ms FROM op_competing_outcomes"
    ).fetchone()
    assert tuple(row) == ("censored_data_gap", 1_000)


def test_quarantine_venue_censors_and_forgets_pre_recovery_launches(tmp_path, monkeypatch):
    conn = connect(tmp_path / "op.db")
    venue = VenueId.BAGS_ROBINHOOD_4663
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: 1_000_000},
        quote_decimals={venue: 3},
        fee_bps={venue: 200},
    )
    launch = _persist_event(
        conn, seq=1, observed_at="2026-07-26T12:00:00Z",
        kind=EventKind.LAUNCH,
    )
    engine.observe_decoded(
        decoded=_decoded(EventKind.LAUNCH), canonical_event_id=launch,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:00Z",
    )
    buy = _persist_event(
        conn, seq=2, observed_at="2026-07-26T12:00:02Z",
        kind=EventKind.BUY, actor="0xbuyer1", quote=10_000, token=-20_000,
    )
    engine.observe_decoded(
        decoded=_decoded(
            EventKind.BUY, actor="0xbuyer1", quote=10_000, token=-20_000,
            vtoken=980_000, vquote=510_000,
        ),
        canonical_event_id=buy,
        confirmation=ConfirmationState.CONFIRMED,
        observed_at="2026-07-26T12:00:02Z",
    )

    def stale_state_censor(*_args, **_kwargs):
        raise ValueError("fixture reconstructed state failure")

    monkeypatch.setattr(engine, "censor_gapped_outcomes", stale_state_censor)

    quarantined = engine.quarantine_venue(
        venue, observed_at="2026-07-26T12:00:03Z"
    )

    assert quarantined == ("launch-1",)
    assert "launch-1" not in engine.states
    assert "launch-1" not in engine.last_observations
    assert {
        row[0] for row in conn.execute(
            "SELECT outcome_kind FROM op_competing_outcomes"
        ).fetchall()
    } == {"censored_data_gap"}


def test_v4_confirmation_uses_local_decision_state_and_records_zero_time_baseline(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(conn)
    engine.states[latest.launch_id] = replace(
        engine.states[latest.launch_id], launched_at="2026-07-26T11:59:50Z"
    )
    engine.prior_interval_features[latest.launch_id] = {
        "capital_velocity_atomic_per_s": 500_000_000,
        "buyer_velocity_per_s_micros": 400_000,
        "effective_buyer_hhi_bps": 1_800,
    }
    engine.last_interval_features[latest.launch_id].update(
        capital_velocity_atomic_per_s=600_000_000,
        buyer_velocity_per_s_micros=600_000,
        effective_buyer_hhi_bps=1_500,
        effective_buyer_count=10,
        new_eligible_buyers=2,
        sell_pressure_bps=2_000,
        creator_sale_since_prior=False,
    )
    state = engine.states[latest.launch_id]
    point = engine.last_observations[latest.launch_id]

    refreshed = engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "jupiter_quote_v3",
            2_000,
            quote_in_atomic=1_000_000_000,
            quote_mint=state.quote_mint,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=900_000_000,
            entry_min_out_atomic=120_000,
            immediate_exit_min_quote_atomic=880_000_000,
            round_trip_return_bps=-400,
            minimum_round_trip_return_bps=-700,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id=state.token_id,
            state_fingerprint=point.state_fingerprint,
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.300000Z",
    )

    assert refreshed is not None and refreshed.would_fire is True
    row = conn.execute(
        "SELECT a.rule_version,f.feature_version,f.feature_json,ce.quote_provider,"
        "ce.quote_version,ce.entry_effective_at "
        "FROM op_forward_alerts a JOIN op_forward_alert_features f USING(alert_id) "
        "JOIN op_forward_alert_entries link USING(alert_id) "
        "JOIN op_counterfactual_entries ce USING(entry_id)"
    ).fetchone()
    assert row["rule_version"] == "pump-confirmation-truth-v6"
    assert row["feature_version"] == "confirmation-features-v3"
    assert row["quote_provider"] == "local_curve_math_at_decision"
    assert row["quote_version"] == "constant-product-v2"
    assert row["entry_effective_at"] == "2026-07-26T12:00:05.300000Z"
    assert conn.execute(
        "SELECT quote_version FROM op_quote_observations "
        "WHERE is_remote=1 AND provider='jupiter_quote_v3' LIMIT 1"
    ).fetchone()[0] == "remote-acquired-quantity-probe-v3"
    features = json.loads(row["feature_json"])
    assert features["modeled_round_trip_cost_bps"] < 0
    assert features["observed_pair_return_bps"] == -400
    assert features["observed_pair_move_bps"] == (
        -400 - features["modeled_round_trip_cost_bps"]
    )
    assert features["eligible_buyer_count"] == 10
    baseline = conn.execute(
        "SELECT o.elapsed_ms,o.net_return_bps,o.provider "
        "FROM op_executable_observations o "
        "JOIN op_forward_alert_entries link USING(entry_id) "
        "JOIN op_forward_alerts a USING(alert_id) "
        "WHERE a.rule_version='pump-confirmation-truth-v6'"
    ).fetchone()
    assert tuple(baseline) == (
        0,
        features["modeled_round_trip_cost_bps"],
        "opportunity-path-v4",
    )


def test_v4_confirmation_uses_current_local_entry_when_route_receipt_state_is_older(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(conn)
    state = engine.states[latest.launch_id]

    result = engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "jupiter_quote_v3",
            2_000,
            quote_in_atomic=1_000_000_000,
            quote_mint=state.quote_mint,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=900_000_000,
            entry_min_out_atomic=120_000,
            immediate_exit_min_quote_atomic=880_000_000,
            round_trip_return_bps=-400,
            minimum_round_trip_return_bps=-700,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id=state.token_id,
            state_fingerprint="stale-state",
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.300000Z",
    )

    assert result is not None
    row = conn.execute(
        "SELECT f.feature_json,ce.quote_provider FROM op_forward_alerts a "
        "JOIN op_forward_alert_features f USING(alert_id) "
        "JOIN op_forward_alert_entries link USING(alert_id) "
        "JOIN op_counterfactual_entries ce USING(entry_id) "
        "WHERE a.rule_version IN ('pump-confirmation-truth-v6','route-control-v2')"
    ).fetchone()
    assert json.loads(row["feature_json"])["route_receipt_state_match"] is False
    assert row["quote_provider"] == "local_curve_math_at_decision"


def test_v4_alert_receipt_records_modeled_cost_not_the_paced_pair(tmp_path):
    """`quote_drag_bps` must be the modeled cost for the arm, as it is for the control."""
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(conn)
    engine.states[latest.launch_id] = replace(
        engine.states[latest.launch_id], launched_at="2026-07-26T11:59:50Z"
    )
    engine.prior_interval_features[latest.launch_id] = {
        "capital_velocity_atomic_per_s": 500_000_000,
        "buyer_velocity_per_s_micros": 400_000,
        "effective_buyer_hhi_bps": 1_800,
    }
    engine.last_interval_features[latest.launch_id].update(
        capital_velocity_atomic_per_s=600_000_000,
        buyer_velocity_per_s_micros=600_000,
        effective_buyer_hhi_bps=1_500,
        effective_buyer_count=10,
        new_eligible_buyers=2,
        sell_pressure_bps=2_000,
        creator_sale_since_prior=False,
    )
    state = engine.states[latest.launch_id]
    point = engine.last_observations[latest.launch_id]

    assert engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True, True, "jupiter_quote_v3", 2_000,
            quote_in_atomic=1_000_000_000,
            quote_mint=state.quote_mint,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=900_000_000,
            entry_min_out_atomic=120_000,
            immediate_exit_min_quote_atomic=880_000_000,
            round_trip_return_bps=-400,
            minimum_round_trip_return_bps=-700,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id=state.token_id,
            state_fingerprint=point.state_fingerprint,
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.300000Z",
    ) is not None

    row = conn.execute(
        "SELECT a.round_trip_return_bps,f.feature_json FROM op_forward_alerts a "
        "JOIN op_forward_alert_features f USING(alert_id) "
        "WHERE a.rule_version='pump-confirmation-truth-v6'"
    ).fetchone()
    modeled = json.loads(row["feature_json"])["modeled_round_trip_cost_bps"]
    assert row["round_trip_return_bps"] == modeled
    assert row["round_trip_return_bps"] != -400


def test_v4_zero_time_baseline_is_not_duplicated_by_outcome_recording(tmp_path):
    """The decision baseline and the same snapshot's exit must be one observation."""
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(conn)
    state = engine.states[latest.launch_id]
    point = engine.last_observations[latest.launch_id]

    assert engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True, True, "jupiter_quote_v3", 2_000,
            quote_in_atomic=1_000_000_000,
            quote_mint=state.quote_mint,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=900_000_000,
            entry_min_out_atomic=120_000,
            immediate_exit_min_quote_atomic=880_000_000,
            round_trip_return_bps=-400,
            minimum_round_trip_return_bps=-700,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id=state.token_id,
            state_fingerprint=point.state_fingerprint,
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.300000Z",
    ) is not None

    snapshot_id = conn.execute(
        "SELECT snapshot_id FROM op_forward_alert_entries LIMIT 1"
    ).fetchone()[0]
    record_launch_outcomes(
        conn,
        state=engine.states[latest.launch_id],
        canonical_state_id=snapshot_id,
        observed_at="2026-07-26T12:00:05.300000Z",
        quote_usd_price_micros=1_000_000,
        quote_decimals=3,
        fee_bps=200,
    )

    per_entry = conn.execute(
        "SELECT COUNT(*) FROM op_executable_observations WHERE elapsed_ms=0 "
        "GROUP BY entry_id ORDER BY 1 DESC LIMIT 1"
    ).fetchone()
    assert per_entry[0] == 1


def test_creator_prior_arm_is_retired_and_the_slot_returns_to_the_control(tmp_path):
    """Retirement recorded 2026-08-07 (operator approved). The arm's evidence basis was
    retracted for target leakage the day it shipped and the clean-universe rescan
    confirms the null (t=0.5); meanwhile the slot diversion consumed ~half of the
    control supply and structurally excluded positive-prior creators from the control
    cohort. A positive creator record must now emit route-control-v2 -- one receipt,
    never two -- while the creator_prior_* FEATURES keep accruing as decision-time
    facts."""
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(conn)
    state = engine.states[latest.launch_id]
    point = engine.last_observations[latest.launch_id]
    credit_creator_outcome(
        conn,
        network_id=network_for_venue(venue),
        venue_id=venue.value,
        creator_id=state.creator_id,
        outcome_kind="profit_first",
        terminal_net_return_bps=1_400,
        observed_at="2026-07-26T11:00:00Z",
    )

    assert engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True, True, "jupiter_quote_v3", 2_000,
            quote_in_atomic=1_000_000_000, quote_mint=state.quote_mint,
            tokens_out_atomic=123_456, immediate_exit_quote_atomic=900_000_000,
            entry_min_out_atomic=120_000, immediate_exit_min_quote_atomic=880_000_000,
            round_trip_return_bps=-1_000, minimum_round_trip_return_bps=-1_200,
            launch_id=latest.launch_id, venue_id=venue.value, token_id=state.token_id,
            state_fingerprint=point.state_fingerprint,
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.300000Z",
    ) is not None

    rules = [
        row[0] for row in conn.execute("SELECT rule_version FROM op_forward_alerts")
    ]
    assert "pump-creator-prior-v1" not in rules
    assert rules.count("route-control-v2") == 1
    features = json.loads(
        conn.execute(
            "SELECT f.feature_json FROM op_forward_alert_features f "
            "JOIN op_forward_alerts a USING(alert_id) "
            "WHERE a.rule_version='route-control-v2'"
        ).fetchone()[0]
    )
    assert features["creator_prior_samples"] == 1
    assert features["creator_prior_mean_bps"] == 1_400


def test_a_creator_with_no_record_still_gets_the_plain_control(tmp_path):
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(conn)
    state = engine.states[latest.launch_id]
    point = engine.last_observations[latest.launch_id]

    engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True, True, "jupiter_quote_v3", 2_000,
            quote_in_atomic=1_000_000_000, quote_mint=state.quote_mint,
            tokens_out_atomic=123_456, immediate_exit_quote_atomic=900_000_000,
            entry_min_out_atomic=120_000, immediate_exit_min_quote_atomic=880_000_000,
            round_trip_return_bps=-1_000, minimum_round_trip_return_bps=-1_200,
            launch_id=latest.launch_id, venue_id=venue.value, token_id=state.token_id,
            state_fingerprint=point.state_fingerprint,
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.300000Z",
    )

    rules = {
        row[0] for row in conn.execute("SELECT rule_version FROM op_forward_alerts")
    }
    assert "route-control-v2" in rules
    assert "pump-creator-prior-v1" not in rules


def _drive_to_v6_alert(
    tmp_path,
    *,
    paper_adapter_enabled,
    adaptive_v3_enabled=False,
):
    conn = connect(tmp_path / "op.db")
    engine, latest, venue = _pending_inner_pump(
        conn,
        paper_adapter_enabled=paper_adapter_enabled,
        adaptive_v3_enabled=adaptive_v3_enabled,
    )
    engine.states[latest.launch_id] = replace(
        engine.states[latest.launch_id], launched_at="2026-07-26T11:59:50Z"
    )
    engine.prior_interval_features[latest.launch_id] = {
        "capital_velocity_atomic_per_s": 500_000_000,
        "buyer_velocity_per_s_micros": 400_000,
        "effective_buyer_hhi_bps": 1_800,
    }
    engine.last_interval_features[latest.launch_id].update(
        capital_velocity_atomic_per_s=600_000_000,
        buyer_velocity_per_s_micros=600_000,
        effective_buyer_hhi_bps=1_500,
        effective_buyer_count=10,
        new_eligible_buyers=2,
        sell_pressure_bps=2_000,
        creator_sale_since_prior=False,
    )
    state = engine.states[latest.launch_id]
    point = engine.last_observations[latest.launch_id]

    refreshed = engine.observe_remote_completion(
        RemoteRoundTripProbe(
            True,
            True,
            "jupiter_quote_v3",
            2_000,
            quote_in_atomic=1_000_000_000,
            quote_mint=state.quote_mint,
            tokens_out_atomic=123_456,
            immediate_exit_quote_atomic=900_000_000,
            entry_min_out_atomic=120_000,
            immediate_exit_min_quote_atomic=880_000_000,
            round_trip_return_bps=-400,
            minimum_round_trip_return_bps=-700,
            launch_id=latest.launch_id,
            venue_id=venue.value,
            token_id=state.token_id,
            state_fingerprint=point.state_fingerprint,
            completed_at="2026-07-26T12:00:05.200000Z",
        ),
        observed_at="2026-07-26T12:00:05.300000Z",
    )

    assert refreshed is not None and refreshed.would_fire is True
    return conn


# --- paper adapter request ----------------------------------------------------
# The request must ride the alert transaction: written afterwards it could be lost to
# a crash while the alert survived, and a qualifying signal with no request cannot be
# told apart from one the adapter declined to quote.

def test_v6_alert_appends_one_paper_fill_request_when_enabled(tmp_path):
    conn = _drive_to_v6_alert(tmp_path, paper_adapter_enabled=True)
    rows = conn.execute("SELECT * FROM op_paper_fill_requests").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["rule_version"] == "pump-confirmation-truth-v6"
    assert row["clip_usd_micros"] == 25_000_000
    assert row["adapter_version"] == "radar-paper-adapter-v1"
    assert row["fill_model_version"] == "route-floor-v1"
    assert row["launch_mode"] in {"standard", "cashback", "mayhem"}
    assert int(row["local_qty_atomic"]) > 0
    assert row["expires_at"] > row["intent_at"]
    # v6's own modeled decision clock is carried, not replaced.
    assert row["decision_age_ms"] > 0
    # and no receipt yet: the request is `pending`, a derived state.
    assert conn.execute("SELECT COUNT(*) FROM op_paper_fills").fetchone()[0] == 0


def test_default_engine_skips_adaptive_v3_evaluation(tmp_path, monkeypatch):
    def unexpected_adaptive_evaluation(*_args, **_kwargs):
        raise AssertionError("adaptive v3 evaluation was not gated")

    monkeypatch.setattr(
        engine_module,
        "evaluate_adaptive_v3",
        unexpected_adaptive_evaluation,
    )

    conn = _drive_to_v6_alert(tmp_path, paper_adapter_enabled=True)

    adaptive_alerts = conn.execute(
        "SELECT COUNT(*) FROM op_forward_alerts "
        "WHERE rule_version='pump-adaptive-v3'"
    ).fetchone()[0]
    requested_rules = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT rule_version FROM op_paper_fill_requests"
        )
    }
    assert adaptive_alerts == 0
    assert requested_rules == {"pump-confirmation-truth-v6"}


def test_enabled_engine_can_store_an_adaptive_v3_request(tmp_path, monkeypatch):
    monkeypatch.setattr(
        engine_module,
        "evaluate_adaptive_v3",
        lambda *_args, **_kwargs: DirectionalVerdict(
            RadarZone.CENTER,
            True,
            ("ADAPTIVE_V3",),
            rule_version="pump-adaptive-v3",
            truth_label="ADAPTIVE V3 - DIAGNOSTIC PAPER",
        ),
    )

    conn = _drive_to_v6_alert(
        tmp_path,
        paper_adapter_enabled=True,
        adaptive_v3_enabled=True,
    )

    requested_rules = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT rule_version FROM op_paper_fill_requests"
        )
    }
    assert requested_rules == {
        "pump-adaptive-v3",
        "pump-confirmation-truth-v6",
    }


def test_no_paper_fill_request_when_the_adapter_is_off(tmp_path):
    conn = _drive_to_v6_alert(tmp_path, paper_adapter_enabled=False)
    assert conn.execute("SELECT COUNT(*) FROM op_paper_fill_requests").fetchone()[0] == 0


def test_paper_fill_request_is_only_for_the_v6_rule_and_its_matched_control(tmp_path):
    conn = _drive_to_v6_alert(tmp_path, paper_adapter_enabled=True)
    arms = {
        row[0]
        for row in conn.execute("SELECT DISTINCT rule_version FROM op_forward_alerts")
    }
    requested = {
        row[0]
        for row in conn.execute("SELECT DISTINCT rule_version FROM op_paper_fill_requests")
    }
    assert requested == {"pump-confirmation-truth-v6"}
    # One request per v5 alert, and nothing for any other arm that fired.
    v5_alerts = conn.execute(
        "SELECT COUNT(*) FROM op_forward_alerts WHERE rule_version='pump-confirmation-truth-v6'"
    ).fetchone()[0]
    total_requests = conn.execute("SELECT COUNT(*) FROM op_paper_fill_requests").fetchone()[0]
    assert total_requests == v5_alerts
    assert requested <= arms
