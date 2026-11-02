from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from db import init_db  # noqa: E402
from sniper.strategies.base import StrategyManageState, StrategySignal  # noqa: E402
from sniper.strategies.registry import get_strategy_plugin  # noqa: E402
from sniper.strategies.source_position_copy_v1 import (  # noqa: E402
    PLUGIN_ID,
    SWISSMISS_WALLET,
    SourceObservation,
    SourcePositionState,
    SourcePositionCopyV1Strategy,
    _already_entered,
    _build_source_positions,
    _detect_threshold_crossing,
    _has_opposite_side_conflict,
    _matches_target_filter,
    _parse_params,
    ensure_source_position_copy_tables,
    insert_source_observation,
    record_copy_state,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db(c)
    ensure_source_position_copy_tables(c)
    yield c
    c.close()


def _obs(**overrides):
    base = {
        "activity_key": "tx-1:0",
        "wallet": SWISSMISS_WALLET,
        "condition_id": "cond-sports-1",
        "asset_id": "asset-yes",
        "outcome": "YES",
        "side": "BUY",
        "price": 0.55,
        "size": 10000.0,
        "usdc_size": 5500.0,
        "observed_ts": 1_800_000_000,
        "title": "UFC fighter A vs fighter B",
        "event_slug": "ufc-fight-night-1",
        "event_id": "event-1",
        "tags": ["sports", "ufc"],
        "source_endpoint": "data-api-user-activity",
        "raw_json": {"id": "tx-1"},
    }
    base.update(overrides)
    return base


def _source_observation(**overrides) -> SourceObservation:
    data = _obs()
    data.update(overrides)
    return SourceObservation(
        activity_key=data["activity_key"],
        wallet=data["wallet"],
        condition_id=data["condition_id"],
        asset_id=data["asset_id"],
        outcome=data["outcome"],
        side=data["side"],
        price=data["price"],
        size=data["size"],
        usdc_size=data["usdc_size"],
        observed_ts=data["observed_ts"],
        title=data["title"],
        event_slug=data["event_slug"],
        event_id=data["event_id"],
        tags=data["tags"],
        source_endpoint=data["source_endpoint"],
        raw_json=data["raw_json"],
    )


def _state_from(observations: list[SourceObservation]) -> SourcePositionState:
    return next(iter(_build_source_positions(observations).values()))


def _strategy(params=None, strategy_id=101):
    return {
        "id": strategy_id,
        "wallet": "paper-wallet",
        "label": "SwissMiss Copy - Filtered",
        "strategy_type": PLUGIN_ID,
        "params": params or {},
        "status": "active",
    }


def _insert_source_obs(conn, **kwargs):
    return insert_source_observation(conn, **_obs(**kwargs))


def test_source_observation_table_is_idempotent(conn):
    ensure_source_position_copy_tables(conn)
    ensure_source_position_copy_tables(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='source_position_observations'"
    ).fetchone()
    assert row is not None


def test_insert_source_observation_dedups_by_activity_key(conn):
    first = insert_source_observation(conn, **_obs())
    second = insert_source_observation(conn, **_obs(price=0.56))

    assert first is not None
    assert second is None

    row = conn.execute(
        "SELECT price, tags_json, raw_json FROM source_position_observations WHERE activity_key='tx-1:0'"
    ).fetchone()
    assert row["price"] == 0.55
    assert json.loads(row["tags_json"]) == ["sports", "ufc"]
    assert json.loads(row["raw_json"]) == {"id": "tx-1"}


def test_target_filter_accepts_sports_and_rejects_geopolitics():
    params = _parse_params({"params": {}})
    sports = _source_observation(tags=["sports", "ufc"], price=0.44)
    geopolitics = _source_observation(tags=["sports", "iran"], price=0.44)

    assert _matches_target_filter(sports, params) == (True, "")
    assert _matches_target_filter(geopolitics, params) == (False, "rejected_tag:iran")


def test_rejected_tag_reason_is_stable_for_tag_order():
    params = _parse_params({"params": {}})
    first = _source_observation(tags=["sports", "geopolitics", "iran"], price=0.44)
    second = _source_observation(tags=["iran", "sports", "geopolitics"], price=0.44)

    assert _matches_target_filter(first, params) == _matches_target_filter(second, params)
    assert _matches_target_filter(first, params) == (False, "rejected_tag:iran")


def test_target_filter_rejects_missing_tags_and_low_price():
    params = _parse_params({"params": {}})

    missing_tags = _source_observation(tags=[], price=0.44)
    low_price = _source_observation(tags=["sports"], price=0.20)

    assert _matches_target_filter(missing_tags, params) == (False, "missing_tags")
    assert _matches_target_filter(low_price, params) == (False, "entry_price_below_min:0.2")


def test_build_source_position_aggregates_adds_sells_and_vwap():
    observations = [
        _source_observation(activity_key="a", price=0.50, size=10000, usdc_size=5000, observed_ts=100),
        _source_observation(activity_key="b", price=0.60, size=20000, usdc_size=12000, observed_ts=200),
        _source_observation(
            activity_key="c",
            side="SELL",
            price=0.70,
            size=5000,
            usdc_size=3500,
            observed_ts=300,
        ),
    ]

    positions = _build_source_positions(observations)

    key = (SWISSMISS_WALLET, "event-1", "cond-sports-1", "asset-yes")
    state = positions[key]
    assert state.buy_cost == 17000
    assert state.sell_proceeds == 3500
    assert state.net_buy_cost == 13500
    assert state.buy_shares == 30000
    assert state.sell_shares == 5000
    assert state.net_shares == 25000
    assert round(state.buy_vwap, 4) == 0.5667
    assert state.build_style == "averaged_up"


def test_threshold_does_not_fire_on_probe_buy():
    params = _parse_params({"params": {"source_commitment_threshold_usdc": 25000}})
    state = _state_from(
        [
            _source_observation(
                activity_key="probe",
                price=0.50,
                size=5000,
                usdc_size=2500,
                observed_ts=100,
            ),
        ]
    )

    trigger = _detect_threshold_crossing(state, params, now_ts=200)

    assert trigger.should_enter is False
    assert trigger.reason == "threshold_not_crossed"


def test_threshold_fires_after_cumulative_adds():
    params = _parse_params(
        {"params": {"source_commitment_threshold_usdc": 25000, "source_commitment_window_hours": 6}}
    )
    state = _state_from(
        [
            _source_observation(activity_key="a", price=0.50, size=20000, usdc_size=10000, observed_ts=100),
            _source_observation(activity_key="b", price=0.60, size=25000, usdc_size=15000, observed_ts=200),
        ]
    )

    trigger = _detect_threshold_crossing(state, params, now_ts=250)

    assert trigger.should_enter is True
    assert trigger.reason == "threshold_crossed"
    assert trigger.cross_ts == 200
    assert trigger.net_buy_cost == 25000
    assert round(trigger.source_vwap, 4) == 0.5556


def test_sells_before_threshold_reduce_net_commitment():
    params = _parse_params({"params": {"source_commitment_threshold_usdc": 25000}})
    state = _state_from(
        [
            _source_observation(activity_key="a", price=0.50, size=30000, usdc_size=15000, observed_ts=100),
            _source_observation(
                activity_key="b",
                side="SELL",
                price=0.55,
                size=10000,
                usdc_size=5500,
                observed_ts=150,
            ),
            _source_observation(activity_key="c", price=0.60, size=25000, usdc_size=15000, observed_ts=200),
        ]
    )

    trigger = _detect_threshold_crossing(state, params, now_ts=250)

    assert trigger.should_enter is False
    assert trigger.reason == "threshold_not_crossed"


def test_stale_threshold_crossing_rejects():
    params = _parse_params(
        {"params": {"source_commitment_threshold_usdc": 25000, "max_threshold_age_minutes": 20}}
    )
    state = _state_from(
        [
            _source_observation(activity_key="a", price=0.50, size=50000, usdc_size=25000, observed_ts=100),
        ]
    )

    trigger = _detect_threshold_crossing(state, params, now_ts=100 + 21 * 60)

    assert trigger.should_enter is False
    assert trigger.reason.startswith("threshold_crossing_stale:")


def test_price_drift_rejects():
    params = _parse_params(
        {"params": {"source_commitment_threshold_usdc": 25000, "max_price_drift_from_source_vwap_pct": 10}}
    )
    state = _state_from(
        [
            _source_observation(activity_key="a", price=0.50, size=50000, usdc_size=25000, observed_ts=100),
        ]
    )

    trigger = _detect_threshold_crossing(state, params, now_ts=110, copy_price=0.60)

    assert trigger.should_enter is False
    assert trigger.reason == "price_drift_exceeded:20.00%"


def test_price_drift_uses_crossing_observation_price_when_copy_price_missing():
    params = _parse_params(
        {"params": {"source_commitment_threshold_usdc": 25000, "max_price_drift_from_source_vwap_pct": 10}}
    )
    state = _state_from(
        [
            _source_observation(activity_key="a", price=0.50, size=40000, usdc_size=20000, observed_ts=100),
            _source_observation(activity_key="b", price=0.70, size=10000, usdc_size=7000, observed_ts=200),
        ]
    )

    trigger = _detect_threshold_crossing(state, params, now_ts=210)

    assert trigger.should_enter is False
    assert trigger.reason == "price_drift_exceeded:29.63%"


def test_threshold_cross_price_uses_copy_price_when_provided():
    params = _parse_params(
        {"params": {"source_commitment_threshold_usdc": 25000, "max_price_drift_from_source_vwap_pct": 50}}
    )
    state = _state_from(
        [
            _source_observation(activity_key="a", price=0.50, size=50000, usdc_size=25000, observed_ts=100),
        ]
    )

    trigger = _detect_threshold_crossing(state, params, now_ts=110, copy_price=0.54)

    assert trigger.should_enter is True
    assert trigger.cross_price == 0.54


def test_already_entered_state_blocks_duplicate_entry(conn):
    record_copy_state(
        conn,
        strategy_id=7,
        wallet=SWISSMISS_WALLET,
        event_id="event-1",
        condition_id="cond-sports-1",
        asset_id="asset-yes",
        trigger_version="swissmiss_filtered_v1",
        state="entered",
        reason="threshold_crossed",
    )

    assert _already_entered(
        conn,
        strategy_id=7,
        wallet=SWISSMISS_WALLET,
        condition_id="cond-sports-1",
        asset_id="asset-yes",
        trigger_version="swissmiss_filtered_v1",
    )


def test_record_copy_state_rejects_unknown_state(conn):
    with pytest.raises(ValueError, match="invalid source-position copy state"):
        record_copy_state(
            conn,
            strategy_id=7,
            wallet=SWISSMISS_WALLET,
            event_id="event-1",
            condition_id="cond-sports-1",
            asset_id="asset-yes",
            trigger_version="swissmiss_filtered_v1",
            state="enterd",
            reason="typo",
        )


def test_record_copy_state_invalid_update_does_not_unblock_entered(conn):
    record_copy_state(
        conn,
        strategy_id=7,
        wallet=SWISSMISS_WALLET,
        event_id="event-1",
        condition_id="cond-sports-1",
        asset_id="asset-yes",
        trigger_version="swissmiss_filtered_v1",
        state="entered",
        reason="threshold_crossed",
        entered_position_id="paper-1",
    )

    with pytest.raises(ValueError):
        record_copy_state(
            conn,
            strategy_id=7,
            wallet=SWISSMISS_WALLET,
            event_id="event-1",
            condition_id="cond-sports-1",
            asset_id="asset-yes",
            trigger_version="swissmiss_filtered_v1",
            state="enterd",
            reason="typo",
        )

    row = conn.execute(
        "SELECT state, entered_position_id FROM source_position_copy_state WHERE strategy_id=7"
    ).fetchone()
    assert row["state"] == "entered"
    assert row["entered_position_id"] == "paper-1"
    assert _already_entered(
        conn,
        strategy_id=7,
        wallet=SWISSMISS_WALLET,
        condition_id="cond-sports-1",
        asset_id="asset-yes",
        trigger_version="swissmiss_filtered_v1",
    )


def test_source_flip_to_opposite_outcome_rejects_for_v1():
    observations = [
        _source_observation(activity_key="a", asset_id="asset-yes", outcome="YES", side="BUY", usdc_size=30000),
        _source_observation(activity_key="b", asset_id="asset-no", outcome="NO", side="BUY", usdc_size=30000),
    ]

    assert _has_opposite_side_conflict(observations) == (True, "opposite_side_conflict")


def test_discover_emits_signal_after_source_threshold(conn):
    _insert_source_obs(conn, activity_key="a", price=0.50, size=20000, usdc_size=10000, observed_ts=int(time.time()) - 60)
    _insert_source_obs(conn, activity_key="b", price=0.60, size=25000, usdc_size=15000, observed_ts=int(time.time()) - 30)

    plugin = SourcePositionCopyV1Strategy()
    signals = plugin.discover(conn, _strategy())

    assert len(signals) == 1
    assert signals[0].wallet == SWISSMISS_WALLET
    assert signals[0].condition_id == "cond-sports-1"
    assert signals[0].current_price == 0.60
    assert signals[0].position_value == 25000
    assert signals[0].metadata["source_position_mode"] == "net_buy_cost"


def test_discover_signal_marks_source_price_proxy(conn):
    now = int(time.time())
    _insert_source_obs(conn, activity_key="a", price=0.50, size=20000, usdc_size=10000, observed_ts=now - 60)
    _insert_source_obs(conn, activity_key="b", price=0.60, size=25000, usdc_size=15000, observed_ts=now - 30)

    signal = SourcePositionCopyV1Strategy().discover(conn, _strategy())[0]

    assert signal.metadata["price_source"] == "source_price_proxy"


def test_discover_counts_sells_even_when_sell_row_fails_entry_filter(conn):
    now = int(time.time())
    _insert_source_obs(conn, activity_key="a", price=0.50, size=40000, usdc_size=20000, observed_ts=now - 90)
    _insert_source_obs(conn, activity_key="b", side="SELL", price=0.20, size=100000, usdc_size=20000, observed_ts=now - 60)
    _insert_source_obs(conn, activity_key="c", price=0.50, size=20000, usdc_size=10000, observed_ts=now - 30)

    plugin = SourcePositionCopyV1Strategy()
    signals = plugin.discover(conn, _strategy({"source_commitment_threshold_usdc": 25000, "entry_price_min": 0.30}))

    assert signals == []
    row = conn.execute("SELECT state, reason FROM source_position_copy_state WHERE strategy_id=101").fetchone()
    assert row["state"] == "tracking"
    assert row["reason"] == "threshold_not_crossed"


def test_discover_records_rejected_state_for_target_filter_failure(conn):
    _insert_source_obs(conn, activity_key="geo", tags=["geopolitics", "iran"], price=0.50, size=60000, usdc_size=30000)

    plugin = SourcePositionCopyV1Strategy()
    assert plugin.discover(conn, _strategy()) == []

    row = conn.execute("SELECT state, reason FROM source_position_copy_state WHERE strategy_id=101").fetchone()
    assert row["state"] == "rejected"
    assert row["reason"] == "rejected_tag:iran"


def test_discover_records_rejected_state_for_opposite_side_conflict(conn):
    now = int(time.time())
    _insert_source_obs(
        conn,
        activity_key="yes",
        asset_id="asset-yes",
        outcome="YES",
        price=0.50,
        size=60000,
        usdc_size=30000,
        observed_ts=now - 60,
    )
    _insert_source_obs(
        conn,
        activity_key="no",
        asset_id="asset-no",
        outcome="NO",
        price=0.50,
        size=60000,
        usdc_size=30000,
        observed_ts=now - 30,
    )

    plugin = SourcePositionCopyV1Strategy()
    assert plugin.discover(conn, _strategy()) == []

    rows = conn.execute("SELECT DISTINCT state, reason FROM source_position_copy_state WHERE strategy_id=101").fetchall()
    assert {(r["state"], r["reason"]) for r in rows} == {("rejected", "opposite_side_conflict")}


def test_discover_records_rejected_state_for_threshold_drift(conn):
    now = int(time.time())
    _insert_source_obs(conn, activity_key="a", price=0.50, size=40000, usdc_size=20000, observed_ts=now - 60)
    _insert_source_obs(conn, activity_key="b", price=0.70, size=10000, usdc_size=7000, observed_ts=now - 30)

    plugin = SourcePositionCopyV1Strategy()
    params = {"source_commitment_threshold_usdc": 25000, "max_price_drift_from_source_vwap_pct": 10}
    assert plugin.discover(conn, _strategy(params)) == []

    row = conn.execute("SELECT state, reason FROM source_position_copy_state WHERE strategy_id=101").fetchone()
    assert row["state"] == "rejected"
    assert row["reason"] == "price_drift_exceeded:29.63%"


def test_plugin_metadata_matches_contract():
    plugin = SourcePositionCopyV1Strategy()
    assert plugin.description == "Paper-only source-position copy after source commitment threshold crossing."
    assert plugin.strategy_family == "paper_copy"


def test_report_includes_state_counts(conn):
    plugin = SourcePositionCopyV1Strategy()
    record_copy_state(
        conn,
        strategy_id=101,
        wallet=SWISSMISS_WALLET,
        event_id="event-1",
        condition_id="cond-sports-1",
        asset_id="asset-yes",
        trigger_version="swissmiss_filtered_v1",
        state="tracking",
        reason="threshold_not_crossed",
    )
    report = plugin.report(conn, _strategy(), [])
    assert report.summary["state_counts"] == {"tracking:threshold_not_crossed": 1}
    assert report.summary["source_positions"] == 1


def test_enter_rejects_non_paper_execution_mode(conn):
    plugin = SourcePositionCopyV1Strategy()
    signal = StrategySignal(
        wallet=SWISSMISS_WALLET,
        condition_id="cond-sports-1",
        outcome="YES",
        title="UFC",
        current_price=0.60,
        position_value=25000,
        metadata={"asset_id": "asset-yes"},
    )

    decision = plugin.enter(conn, _strategy({"execution_mode": "live"}), signal, [])

    assert decision.should_enter is False
    assert decision.reason == "non_paper_execution_rejected"


def test_enter_returns_fixed_paper_size_and_marks_entered(conn):
    plugin = SourcePositionCopyV1Strategy()
    signal = StrategySignal(
        wallet=SWISSMISS_WALLET,
        condition_id="cond-sports-1",
        outcome="YES",
        title="UFC",
        current_price=0.60,
        position_value=25000,
        metadata={
            "asset_id": "asset-yes",
            "event_id": "event-1",
            "trigger_version": "swissmiss_filtered_v1",
            "threshold_cross_ts": 100,
            "source_vwap": 0.5555,
            "source_net_cost": 25000,
            "source_net_shares": 45000,
        },
    )

    decision = plugin.enter(conn, _strategy({"paper_size_usdc": 25}), signal, [])

    assert decision.should_enter is True
    assert decision.execution_mode == "paper"
    assert decision.size_usdc == 25
    assert decision.reason == "source_position_threshold_crossed"
    assert _already_entered(
        conn,
        strategy_id=101,
        wallet=SWISSMISS_WALLET,
        condition_id="cond-sports-1",
        asset_id="asset-yes",
        trigger_version="swissmiss_filtered_v1",
    )


def test_hold_to_resolution_exit_stays_open_until_resolution(conn):
    plugin = SourcePositionCopyV1Strategy()
    strategy = _strategy({"exit_policy": "hold_to_resolution"})

    assert plugin.exit(conn, strategy, {"current_price": 0.55}, StrategyManageState()).should_exit is False
    assert plugin.exit(conn, strategy, {"current_price": 0.98}, StrategyManageState()).reason == "resolution_win"
    assert plugin.exit(conn, strategy, {"current_price": 0.02}, StrategyManageState()).reason == "resolution_loss"


def test_source_exit_policy_closes_on_material_source_reduction(conn):
    plugin = SourcePositionCopyV1Strategy()
    strategy = _strategy({"exit_policy": "source_exit", "material_source_exit_share": 0.50})
    state = StrategyManageState(current_price=0.54, metadata={"source_exit_share": 0.55})

    decision = plugin.exit(conn, strategy, {"current_price": 0.54}, state)

    assert decision.should_exit is True
    assert decision.reason == "source_material_exit"


def test_registry_exposes_source_position_copy_v1():
    plugin = get_strategy_plugin(PLUGIN_ID)
    assert isinstance(plugin, SourcePositionCopyV1Strategy)
