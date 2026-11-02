from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as runner
from sniper.services.memecoin_entry_shadow import evaluate_entry_shadows
from sniper.services.memecoin_entry_trace import EntryAttemptTrace


DECISION_AT = "2026-08-31T12:00:00+00:00"
EARLIER = "2026-08-31T11:00:00+00:00"
LATER = "2026-08-31T13:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _attempt(conn, *, signal_source="follow_activity", band="small", network_id="solana"):
    attempt_id = "attempt-1"
    m.start_entry_attempt(
        conn,
        attempt_id=attempt_id,
        decision_started_at=DECISION_AT,
        signal_source=signal_source,
        network_id=network_id,
        mint="Mint",
        source_user_id="wallet-1",
    )
    if band is not None:
        payload, digest = m.canonical_funded_json({
            "classification_version": 1,
            "coin_band": band,
            "facts": {},
        })
        m.set_entry_attempt_classification(
            conn,
            attempt_id,
            classification_json=payload,
            classification_hash=digest,
            classification_version=1,
        )
    return attempt_id


def _position(
        conn, *, mode="real", network_id="solana", is_open=False, pnl=-10.0,
        notional=100.0, closed_recorded_at=EARLIER, signal_source="follow_activity"):
    position_id = m.open_position(
        conn,
        source_user_id="wallet-1",
        source_handle="wallet-1",
        mint=f"Mint-{mode}-{network_id}-{position_id_seed(conn)}",
        ticker="T",
        entry_price=1.0,
        entry_notional=notional,
        entry_fill_source="test",
        entry_lag_s=0.0,
        their_size_usd=100.0,
        opened_at="2026-08-31T10:00:00+00:00",
        opened_recorded_at="2026-08-31T10:00:00+00:00",
        execution_mode=mode,
        network_id=network_id,
        signal_source=signal_source,
        strategy="alpha_b",
    )
    if not is_open:
        conn.execute(
            "UPDATE memecoin_positions SET is_open=0, realized_pnl=?, closed_at=?, "
            "closed_recorded_at=? WHERE id=?",
            (pnl, closed_recorded_at, closed_recorded_at, position_id),
        )
        conn.commit()
    return position_id


def position_id_seed(conn):
    return conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0]


def test_small_band_shadow_blocks_the_frozen_small_classification():
    conn = _conn()

    shadows = evaluate_entry_shadows(conn, attempt_id=_attempt(conn), current_position_id=None)

    assert shadows["version"] == "funded_entry_shadow_v1"
    assert shadows["small_band_v1"] == {"state": "would_block", "coin_band": "small"}


@pytest.mark.parametrize(("signal_source", "band", "expected"), [
    ("manual", "small", "not_applicable"),
    ("follow_activity", None, "unknown"),
    ("follow_activity", "nano", "would_allow"),
])
def test_small_band_shadow_has_a_defined_state(signal_source, band, expected):
    conn = _conn()

    shadows = evaluate_entry_shadows(
        conn,
        attempt_id=_attempt(conn, signal_source=signal_source, band=band),
        current_position_id=None,
    )

    assert shadows["small_band_v1"]["state"] == expected


def test_prior_shadow_excludes_paper_open_invalid_future_other_chain_and_current_rows():
    conn = _conn()
    attempt_id = _attempt(conn)
    eligible = [_position(conn) for _ in range(5)]
    current_position_id = _position(conn, pnl=-100.0)
    _position(conn, signal_source="manual", pnl=-100.0)
    _position(conn, mode="paper", pnl=500.0)
    _position(conn, is_open=True, pnl=500.0)
    _position(conn, network_id="4663", pnl=500.0)
    _position(conn, closed_recorded_at=LATER, pnl=500.0)
    _position(conn, pnl=float("inf"))
    _position(conn, notional=0.0)

    shadows = evaluate_entry_shadows(
        conn,
        attempt_id=attempt_id,
        current_position_id=current_position_id,
    )

    prior = shadows["prior_5_negative_v1"]
    assert eligible
    assert prior["state"] == "would_block"
    assert prior["prior_closed_n"] == 5
    assert prior["prior_realized_pnl"] == pytest.approx(-50.0)
    assert prior["prior_entry_notional"] == pytest.approx(500.0)
    assert prior["prior_roi_pct"] == pytest.approx(-10.0)
    assert prior["as_of_decision_at"] == DECISION_AT


def test_prior_shadow_requires_five_eligible_closed_real_positions():
    conn = _conn()
    attempt_id = _attempt(conn)
    for _ in range(4):
        _position(conn)

    shadows = evaluate_entry_shadows(conn, attempt_id=attempt_id, current_position_id=None)

    assert shadows["prior_5_negative_v1"]["state"] == "insufficient_history"
    assert shadows["prior_5_negative_v1"]["prior_closed_n"] == 4


def test_strategy_shadows_store_raw_inputs_and_keep_the_gate_state_descriptive():
    conn = _conn()
    attempt_id = _attempt(conn, band="small")
    payload, digest = m.canonical_funded_json({
        "classification_version": 1,
        "coin_band": "small",
        "facts": {
            "effective_mcap": 300_000.0,
            "volume_m5": 1_400.0,
        },
    })
    conn.execute("UPDATE memecoin_entry_attempts SET classification_version=?, "
                 "classification_hash=?, classification_json=? WHERE attempt_id=?",
                 (1, digest, payload, attempt_id))
    conn.commit()

    shadows = evaluate_entry_shadows(
        conn,
        attempt_id=attempt_id,
        current_position_id=None,
        result={"status": "filled"},
        entry_context={
            "source_size_usd": 600.0,
            "candidate_clip_usd": 125.0,
            "selection_gates_complete": True,
        },
    )

    provenance = shadows["gate_provenance_v1"]
    assert provenance["inputs"] == {
        "coin_band": "small",
        "source_size_usd": 600.0,
        "candidate_clip_usd": 125.0,
        "effective_mcap_usd": 300_000.0,
        "raw_m5_volume_usd": 1_400.0,
        "organic_m5_volume_usd": 800.0,
    }
    assert set(provenance["selection_gate_states"].values()) == {"unknown"}
    assert shadows["source_size_ge_1000_v1"]["state"] == "would_block"
    assert shadows["solana_clip_ge_100_v1"]["state"] == "would_allow"
    assert shadows["robinhood_organic_m5_lt_1000_v1"]["state"] == "not_applicable"


def test_robinhood_volume_shadow_keeps_zero_volume_distinct_from_missing_volume():
    conn = _conn()
    attempt_id = _attempt(conn, network_id="4663")
    payload, digest = m.canonical_funded_json({
        "classification_version": 1,
        "coin_band": "nano",
        "facts": {"volume_m5": 0.0},
    })
    conn.execute("UPDATE memecoin_entry_attempts SET classification_version=?, "
                 "classification_hash=?, classification_json=? WHERE attempt_id=?",
                 (1, digest, payload, attempt_id))
    conn.commit()

    shadows = evaluate_entry_shadows(
        conn,
        attempt_id=attempt_id,
        current_position_id=None,
        entry_context={"source_size_usd": 300.0},
    )

    inputs = shadows["gate_provenance_v1"]["inputs"]
    assert inputs["raw_m5_volume_usd"] == 0.0
    assert inputs["organic_m5_volume_usd"] == 0.0
    assert shadows["robinhood_organic_m5_lt_1000_v1"]["state"] == "would_block"


class _Clock:
    def perf_counter(self):
        return 0.0


def _trace(conn):
    event = SimpleNamespace(
        fomo_event_id="event-1",
        signal_source="follow_activity",
        network_id="solana",
        mint="Mint",
        source_user_id="wallet-1",
        source_handle="wallet-1",
        ticker="T",
        side="buy",
        created_at=EARLIER,
        their_price=1.0,
        their_market_cap=10_000.0,
        received_at_normalized=EARLIER,
        clock_domain="observed_real_utc",
    )
    trace = EntryAttemptTrace.start(conn, event, DECISION_AT, _Clock())
    payload, digest = m.canonical_funded_json({
        "classification_version": 1,
        "coin_band": "small",
        "facts": {},
    })
    m.set_entry_attempt_classification(
        conn,
        trace.attempt_id,
        classification_json=payload,
        classification_hash=digest,
        classification_version=1,
    )
    return trace


def _shadow_event(conn, trace):
    payload = json.loads(conn.execute(
        "SELECT trace_json FROM memecoin_entry_attempts WHERE attempt_id=?",
        (trace.attempt_id,),
    ).fetchone()[0])
    return [event for event in payload["substage_events"]
            if event["name"] == "funded_entry_shadow_v1"]


def test_trace_finalization_appends_one_immutable_entry_shadow_event():
    conn = _conn()
    trace = _trace(conn)

    assert runner._finish_trace_for_result(trace, {"status": "unknown"}) is True

    events = _shadow_event(conn, trace)
    assert len(events) == 1
    assert events[0]["version"] == "funded_entry_shadow_v1"
    assert events[0]["small_band_v1"]["state"] == "would_block"


def test_trace_finalization_uses_the_local_entry_context_only_for_observation():
    conn = _conn()
    trace = _trace(conn)
    trace.set_shadow_context(
        source_size_usd=600.0,
        candidate_clip_usd=125.0,
        selection_gates_complete=True,
    )

    assert runner._finish_trace_for_result(trace, {"status": "filled"}) is True

    event = _shadow_event(conn, trace)[0]
    assert event["gate_provenance_v1"]["inputs"]["source_size_usd"] == 600.0
    assert event["solana_clip_ge_100_v1"]["state"] == "would_allow"


def test_shadow_evaluator_error_does_not_change_the_funded_result(monkeypatch):
    conn = _conn()
    trace = _trace(conn)
    result = {"status": "unknown", "reason": "confirmation unknown"}
    monkeypatch.setattr(
        "sniper.services.memecoin_entry_shadow.evaluate_entry_shadows",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("shadow unavailable")),
    )

    assert runner._finish_trace_for_result(trace, result) is True

    assert result == {"status": "unknown", "reason": "confirmation unknown"}
    events = _shadow_event(conn, trace)
    assert len(events) == 1
    assert events[0]["small_band_v1"]["state"] == "unknown"
    assert events[0]["prior_5_negative_v1"]["state"] == "unknown"


def test_completed_selection_does_not_prove_unrecorded_gates_passed():
    from sniper.services.memecoin_entry_shadow import _selection_gate_states
    states = _selection_gate_states({}, {"status": "filled"}, {"selection_gates_complete": True})
    assert set(states.values()) == {"unknown"}


def test_trace_gate_evidence_keeps_disabled_missing_and_bypassed_distinct():
    conn = _conn()
    trace = _trace(conn)
    trace.mark_substage("source_buy_floor", floor_usd=100.0, source_size_usd=150.0, rejected=False)
    trace.mark_substage("sell_share", allowed=True, share_pct=None, txns=0, reason="unknown")
    trace.mark_substage("execution_premium", allow=True, cap=0, reason="cap_off")
    trace.mark_substage("chase_quote_confirmation", cap_pct=0.0, gap_pct=600.0)
    trace.mark_substage("volume_gate", active=False, mcap=20_000, boundary=2_000_000,
                        allowed=True, reason="ok", configured_ceiling=1.5,
                        gate_evaluations={"m5_turnover_over_ceiling": {"state": "out_of_scope"}})
    assert runner._finish_trace_for_result(trace, {"status": "filled"})
    provenance = _shadow_event(conn, trace)[0]["gate_provenance_v1"]
    assert provenance["schema_version"] == 2
    states = provenance["selection_gate_states"]
    assert states["source_buy_floor"] == "pass"
    assert states["sell_share"] == "unknown"
    assert states["execution_premium"] == "disabled"
    assert states["chase_past_entry"] == "disabled"
    assert states["m5_turnover_over_ceiling"] == "out_of_scope"
    assert provenance["gate_evaluations"]["source_buy_floor"]["floor_usd"] == 100.0


def test_gate_exception_preserves_evidence_without_claiming_enforcement():
    from sniper.services.memecoin_entry_shadow import _selection_gate_states
    context = {"gate_events": [{"name": "source_buy_floor", "floor_usd": 100,
                                "source_size_usd": 50, "rejected": True}]}
    states = _selection_gate_states({"exemptions_applied": "source_buy_floor,chase_first_seen?"},
                                    {"status": "filled"}, context)
    assert states["source_buy_floor"] == "bypassed"
    assert states["chase_first_seen"] == "bypassed"
    states = _selection_gate_states({}, {"status": "rejected",
        "reason": "source buy $50 under unknown-mcap floor $100"}, {})
    assert states["source_buy_floor"] == "fail"


@pytest.mark.parametrize(('active', 'mcap', 'allowed', 'reason', 'volume', 'expected'), [
    (False, 20_000, True, 'ok', 60_000, 'out_of_scope'),
    (True, 3_000_000, True, 'ok', 6_000_000, 'pass'),
    (True, 3_000_000, False, 'm5_turnover_under_big_floor', 1, 'fail'),
    (True, 3_000_000, True, 'no_volume_data', None, 'unknown'),
])
def test_volume_evidence_separates_scope_from_the_actual_minimum(active, mcap, allowed, reason, volume, expected):
    from sniper.services.memecoin_entry_shadow import _gate_evaluations
    evidence = _gate_evaluations({'gate_events': [{'name': 'volume_gate',
        'active': active, 'mcap': mcap, 'boundary': 2_000_000, 'allowed': allowed,
        'reason': reason, 'volume_m5': volume, 'volume_h1': 5_000_000,
        'floor_over_band': .03, 'h1_floor': .01, 'txns': 10, 'txns_floor': 2,
        'configured_ceiling': 1.5}]})
    assert evidence['m5_turnover_under_big_floor']['state'] == expected
    assert evidence['m5_turnover_over_ceiling']['state'] == 'out_of_scope'
    assert evidence['m5_turnover_over_ceiling']['configured_ratio'] == 1.5
    assert evidence['m5_turnover_over_ceiling']['effective_ratio'] == 0.0


def test_frozen_research_shadows_use_raw_turnover_and_other_mint_prior():
    conn = _conn()
    aid = _attempt(conn, band='nano')
    conn.execute("UPDATE memecoin_entry_attempts SET classification_json=? WHERE attempt_id=?",
                 (json.dumps({'coin_band': 'nano', 'facts': {'effective_mcap': 20_000, 'volume_m5': 6_000}}), aid))
    for _ in range(5):
        _position(conn, pnl=5.0)
    same = _position(conn, pnl=-100.0)
    conn.execute("UPDATE memecoin_positions SET funded_contract_json=?",
                 (json.dumps({'source': {'signal_source': 'follow_activity'}}),))
    conn.execute("UPDATE memecoin_positions SET mint='Mint' WHERE id=?", (same,))
    conn.commit()
    shadows = evaluate_entry_shadows(conn, attempt_id=aid, current_position_id=None,
                                     entry_context={'source_size_usd': 6_000})
    assert shadows['turnover_10_50_v1']['state'] == 'would_allow'
    assert shadows['turnover_10_50_v1']['raw_turnover_ratio'] == .3
    nano = shadows['nano_prior_profitable_v1']
    assert nano['state'] == 'would_allow'
    assert nano['prior_closed_n'] == 5
    assert nano['prior_realized_pnl'] == 25.0
    assert shadows['prior_5_negative_v1']['state'] == 'would_block'


@pytest.mark.parametrize(('mcap', 'volume', 'state'), [(None, 1, 'unknown'), (1, None, 'unknown'), (1, 0, 'would_block'), (1, .5, 'would_block'), (1, .1, 'would_allow')])
def test_turnover_shadow_keeps_unknown_and_half_open_bounds(mcap, volume, state):
    conn = _conn(); aid = _attempt(conn)
    conn.execute("UPDATE memecoin_entry_attempts SET classification_json=? WHERE attempt_id=?",
                 (json.dumps({'facts': {'effective_mcap': mcap, 'volume_m5': volume}}), aid))
    conn.commit()
    assert evaluate_entry_shadows(conn, attempt_id=aid, current_position_id=None)['turnover_10_50_v1']['state'] == state


@pytest.mark.parametrize(('source_size', 'state'), [
    (None, 'unknown'), (999.99, 'would_block'), (1_000.0, 'would_allow'),
    (9_999.99, 'would_allow'), (10_000.0, 'would_block')])
def test_source_band_shadow_keeps_unknown_and_half_open_bounds(source_size, state):
    conn = _conn(); aid = _attempt(conn)
    conn.commit()
    entry_context = {} if source_size is None else {'source_size_usd': source_size}
    shadows = evaluate_entry_shadows(conn, attempt_id=aid, current_position_id=None,
                                     entry_context=entry_context)
    assert shadows['source_1k_10k_v1']['state'] == state


def test_source_band_shadow_is_not_applicable_off_the_studied_networks():
    conn = _conn(); aid = _attempt(conn, network_id='8453')
    conn.commit()
    shadows = evaluate_entry_shadows(conn, attempt_id=aid, current_position_id=None,
                                     entry_context={'source_size_usd': 5_000.0})
    assert shadows['source_1k_10k_v1']['state'] == 'not_applicable'


def test_turnover_source_conjunction_allows_only_when_both_bands_allow():
    conn = _conn(); aid = _attempt(conn)
    conn.execute("UPDATE memecoin_entry_attempts SET classification_json=? WHERE attempt_id=?",
                 (json.dumps({'facts': {'effective_mcap': 100_000.0, 'volume_m5': 30_000.0}}), aid))
    conn.commit()
    shadows = evaluate_entry_shadows(conn, attempt_id=aid, current_position_id=None,
                                     entry_context={'source_size_usd': 5_000.0})
    assert shadows['turnover_10_50_v1']['state'] == 'would_allow'
    assert shadows['source_1k_10k_v1']['state'] == 'would_allow'
    assert shadows['turnover_source_conjunction_v1'] == {'state': 'would_allow'}


def test_turnover_source_conjunction_blocks_when_only_turnover_allows():
    conn = _conn(); aid = _attempt(conn)
    conn.execute("UPDATE memecoin_entry_attempts SET classification_json=? WHERE attempt_id=?",
                 (json.dumps({'facts': {'effective_mcap': 100_000.0, 'volume_m5': 30_000.0}}), aid))
    conn.commit()
    shadows = evaluate_entry_shadows(conn, attempt_id=aid, current_position_id=None,
                                     entry_context={'source_size_usd': 200.0})
    assert shadows['turnover_10_50_v1']['state'] == 'would_allow'
    assert shadows['source_1k_10k_v1']['state'] == 'would_block'
    assert shadows['turnover_source_conjunction_v1']['state'] == 'would_block'


def test_turnover_source_conjunction_stays_unknown_rather_than_guessing_a_missing_side():
    conn = _conn(); aid = _attempt(conn)
    conn.execute("UPDATE memecoin_entry_attempts SET classification_json=? WHERE attempt_id=?",
                 (json.dumps({'facts': {'effective_mcap': None, 'volume_m5': 30_000.0}}), aid))
    conn.commit()
    shadows = evaluate_entry_shadows(conn, attempt_id=aid, current_position_id=None,
                                     entry_context={'source_size_usd': 5_000.0})
    assert shadows['turnover_10_50_v1']['state'] == 'unknown'
    assert shadows['source_1k_10k_v1']['state'] == 'would_allow'
    assert shadows['turnover_source_conjunction_v1']['state'] == 'unknown'


def test_turnover_source_conjunction_is_not_applicable_for_manual_signals():
    conn = _conn(); aid = _attempt(conn, signal_source='manual')
    conn.commit()
    shadows = evaluate_entry_shadows(conn, attempt_id=aid, current_position_id=None,
                                     entry_context={'source_size_usd': 5_000.0})
    assert shadows['turnover_source_conjunction_v1'] == {'state': 'not_applicable'}
    assert shadows['source_1k_10k_v1'] == {'state': 'not_applicable'}


def test_nano_prior_without_target_mint_remains_unknown():
    conn = _conn()
    attempt_id = _attempt(conn, band="nano")
    conn.execute("UPDATE memecoin_entry_attempts SET mint=NULL,classification_json=? WHERE attempt_id=?",
                 (json.dumps({"facts": {"effective_mcap": 20_000, "volume_m5": 6_000}}), attempt_id))
    for _ in range(5):
        _position(conn, pnl=5.0)
    shadow = evaluate_entry_shadows(conn, attempt_id=attempt_id, current_position_id=None)
    assert shadow["nano_prior_profitable_v1"]["state"] == "unknown"
    assert shadow["nano_prior_profitable_v1"].get("target_mint_excluded") is not True


def test_prior_compares_close_and_decision_as_instants():
    conn = _conn()
    attempt_id = _attempt(conn, band="nano")
    conn.execute("UPDATE memecoin_entry_attempts SET decision_started_at=?,classification_json=? WHERE attempt_id=?",
                 ("2026-08-31T12:00:00+02:00",
                  json.dumps({"facts": {"effective_mcap": 20_000, "volume_m5": 6_000}}), attempt_id))
    for _ in range(5):
        _position(conn, pnl=5.0, closed_recorded_at="2026-08-31T11:00:00+00:00")
    conn.execute("UPDATE memecoin_positions SET funded_contract_json=?",
                 (json.dumps({'source': {'signal_source': 'follow_activity'}}),))
    shadow = evaluate_entry_shadows(conn, attempt_id=attempt_id, current_position_id=None)
    assert shadow["nano_prior_profitable_v1"]["prior_closed_n"] == 0
    assert shadow["nano_prior_profitable_v1"]["state"] == "insufficient_history"


def test_nano_prior_requires_frozen_copy_identity():
    conn = _conn()
    aid = _attempt(conn, band='nano')
    conn.execute("UPDATE memecoin_entry_attempts SET classification_json=? WHERE attempt_id=?",
                 (json.dumps({'facts': {'effective_mcap': 20_000}}), aid))
    for _ in range(5):
        _position(conn, pnl=5)
    manual = _position(conn, pnl=100)
    conn.execute("UPDATE memecoin_positions SET funded_contract_json=? WHERE id=?",
                 (json.dumps({'source': {'signal_source': 'manual'}}), manual))
    result = evaluate_entry_shadows(conn, attempt_id=aid, current_position_id=None)
    assert result['nano_prior_profitable_v1']['prior_closed_n'] == 0
    assert result['prior_5_negative_v1']['prior_closed_n'] == 6


@pytest.mark.parametrize(('event', 'family', 'expected'), [
    ({'name': 'sell_share', 'allowed': True, 'share_pct': 100, 'txns': 2,
      'min_txns': 8, 'cap_pct': 80}, 'sell_share', 'unknown'),
    ({'name': 'sell_share', 'allowed': True, 'share_pct': 10, 'txns': 12,
      'min_txns': 8, 'cap_pct': 80}, 'sell_share', 'pass'),
    ({'name': 'sell_share', 'cap_pct': 0}, 'sell_share', 'disabled'),
    ({'name': 'chase_quote_skipped', 'reason': 'cap_off', 'cap_pct': 0},
     'chase_past_entry', 'disabled'),
    ({'name': 'chase_quote_skipped', 'reason': 'no_source_price', 'cap_pct': 30},
     'chase_past_entry', 'unknown'),
])
def test_gate_evidence_requires_an_actual_evaluation(event, family, expected):
    from sniper.services.memecoin_entry_shadow import _gate_evaluations
    assert _gate_evaluations({'gate_events': [event]})[family]['state'] == expected


@pytest.mark.parametrize('mcap, expected_ratio', [(54016.96232313582, 0.5), (None, None)])
def test_labrats_recorded_volume_refusal_overrides_old_scope(mcap, expected_ratio):
    from sniper.services.memecoin_entry_shadow import _gate_evaluations

    evidence = _gate_evaluations({'gate_events': [{
        'name': 'volume_gate', 'active': False, 'mcap': mcap,
        'boundary': 2_000_000, 'allowed': False,
        'reason': 'm5_turnover_over_ceiling', 'volume_m5': 33757.0,
        'required_usd': 27008.48116156791, 'configured_ceiling': 1.5,
    }]})['m5_turnover_over_ceiling']

    assert evidence['state'] == 'fail'
    assert evidence['input'] == 33757.0
    assert evidence['threshold'] == 27008.48116156791
    assert evidence['effective_ratio'] == expected_ratio
    assert evidence['configured_ratio'] == 1.5


@pytest.mark.parametrize('changes,state,threshold,value', [
    ({}, 'fail', 796.754399, 22.08),
    ({'volume_h1': 1000, 'source_volume_excluded_usd': 100,
      'allowed': True, 'reason': 'ok'}, 'pass', 796.754399, 900),
    ({'source_volume_excluded_usd': 100}, 'fail', 796.754399, 22.08),
    ({'h1_abs_floor': 5000}, 'fail', 796.754399, 22.08),
    ({'mcap': None, 'h1_abs_floor': 100, 'reason': 'under_abs_floor'}, 'fail', 100, 22.08),
    ({'h1_floor': 0, 'h1_abs_floor': 100, 'reason': 'under_abs_floor',
      'source_volume_excluded_usd': 100}, 'fail', 100, 0),
    ({'volume_h1': None, 'allowed': True, 'reason': 'no_volume_data'},
     'unknown', 796.754399, None),
    ({'mcap': None, 'allowed': True, 'reason': 'ok'}, 'unknown', None, 22.08),
    ({'h1_floor': 0, 'allowed': True, 'reason': 'ok'}, 'disabled', 0, 22.08),
    ({'h1_floor': 0, 'volume_h1': None, 'allowed': True,
      'reason': 'no_volume_data'}, 'disabled', 0, None),
    ({'reason': 'm5_turnover_under_floor'}, 'unknown', 796.754399, 22.08),
])
def test_all_cap_h1_evidence_has_actual_usd_threshold_and_input(changes, state, threshold, value):
    from sniper.services.memecoin_entry_shadow import _gate_evaluations
    event = {'name': 'volume_gate', 'active': False, 'boundary': 2_000_000,
             'mcap': 79675.4399, 'volume_h1': 22.08, 'h1_floor': .01,
             'h1_abs_floor': 0, 'h1_scope': 'all_market_caps',
             'allowed': False, 'reason': 'turnover_under_floor'}
    event.update(changes)
    evidence = _gate_evaluations({'gate_events': [event]})['turnover_under_floor']
    assert evidence['state'] == state
    if threshold is None:
        assert evidence['threshold'] is None
    else:
        assert evidence['threshold'] == pytest.approx(threshold)
    assert evidence['input'] == value
    assert evidence['units'] == 'USD'


def test_legacy_h1_trace_keeps_its_recorded_band_scope():
    from sniper.services.memecoin_entry_shadow import _gate_evaluations
    evidence = _gate_evaluations({'gate_events': [{
        'name': 'volume_gate', 'active': False, 'boundary': 2_000_000,
        'mcap': 79675.4399, 'volume_h1': 22.08, 'h1_floor': .01,
        'allowed': True, 'reason': 'ok',
    }]})['turnover_under_floor']
    assert evidence['state'] == 'out_of_scope'


def test_entry_view_preserves_hourly_volume_scope_and_floors():
    from sniper.services.memecoin_entry_view import _gate_evidence
    event = {'name': 'volume_gate', 'volume_h1': 22.08, 'h1_floor': .01,
             'h1_abs_floor': 0, 'h1_scope': 'all_market_caps',
             'waived_m5_reason': 'm5_volume_unreadable'}
    assert _gate_evidence({'trace_json': json.dumps({'substage_events': [event]})}) == [event]


def test_waived_m5_failure_does_not_claim_unreached_m5_legs_passed():
    from sniper.services.memecoin_entry_shadow import _gate_evaluations
    evidence = _gate_evaluations({'gate_events': [{
        'name': 'volume_gate', 'active': True, 'boundary': 2_000_000,
        'mcap': 3_000_000, 'volume_m5': 1, 'volume_h1': 40_000,
        'txns': 1, 'txns_floor': 2, 'floor_over_band': .03,
        'h1_scope': 'all_market_caps', 'h1_floor': .01, 'h1_abs_floor': 0,
        'waived_m5_reason': 'm5_txns_under_floor', 'reason': 'ok', 'allowed': True,
    }]})
    assert evidence['m5_txns_under_floor']['state'] == 'bypassed'
    assert evidence['m5_turnover_under_big_floor']['state'] == 'unknown'
    assert evidence['turnover_under_floor']['state'] == 'pass'
