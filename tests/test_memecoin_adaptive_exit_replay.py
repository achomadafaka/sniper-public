"""Check chronological quote rules and the evidence boundary."""
import pytest

from sniper.analysis.memecoin_adaptive_exit_replay import replay, summarize


def case(values, times=None):
    """Use seconds from a confirmed entry for a fixed whole-position quote."""
    from datetime import datetime, timedelta, timezone
    import json
    start = datetime(2026, 9, 12, tzinfo=timezone.utc)
    stamp = lambda t: (start + timedelta(seconds=t)).isoformat()
    policy = dict(tp_pct=25, stop_pct=35, catastrophe_pct=80,
                  stop_debounce_sweeps=2, max_hold_s=14400,
                  trail_pct=0, derisk_pct=0, mirror_on_sell=False)
    return {
        'position': dict(id='1', opened_at=stamp(0), opened_recorded_at=stamp(0),
                         closed_at=stamp(100), exit_reason='b_stop', network_id='solana',
                         mint='test', entry_notional='100', funded_contract_json=json.dumps({'policy': policy})),
        'memecoin_fills': [dict(kind='entry', qty=10, notional=100)],
        'memecoin_exit_execution_attempts': [], 'memecoin_position_exit_overrides': [],
        'memecoin_exit_targets': [], 'memecoin_exit_trigger_audit': [],
        'memecoin_route_series': [dict(position_id=1, mint='test', network_id='solana',
                                     seam='exit_decision', observed_at=stamp(t),
                                     quoted_at=stamp(t), open_qty=10, expected_usdc=v,
                                     quote_evidence_json=None)
                                 for t, v in zip(times or range(1, len(values)+1), values)],
    }


def test_ratchet_uses_only_prior_quotes_and_actual_trigger_proceeds():
    data = case([100, 110, 74, 70, 125])
    assert replay(data, 'moving_stop')['quote_pnl'] == -30
    assert replay(data, 'current')['quote_pnl'] == 25
    assert replay(data, 'both')['quote_pnl'] == -30
    assert replay(data, 'moving_stop')['peak_surrendered'] == 40
    assert replay(data, 'moving_stop')['profit_surrendered'] == 10


def test_initial_gap_does_not_reduce_target():
    data = case([90, 100, 114, 125])
    result = replay(data, 'reduced_target')
    assert result['quote_pnl'] == 25
    assert result['initial_gap_pct'] == -10


def test_reduced_target_can_reach_zero_and_then_become_negative():
    assert replay(case([100, 75, 100]), 'reduced_target')['quote_pnl'] == 0
    assert replay(case([100, 70, 95]), 'reduced_target')['quote_pnl'] == -5


def test_recovery_does_not_restore_target_or_lower_stop():
    assert replay(case([100, 90, 100, 115]), 'reduced_target')['quote_pnl'] == 15
    assert replay(case([100, 120, 110, 84, 82]), 'moving_stop')['quote_pnl'] == -18


def test_stop_debounce_resets_after_recovery():
    result = replay(case([100, 64, 80, 63, 60]), 'current')
    assert result['quote_pnl'] == -40


def test_instant_grace_and_catastrophe_match_current_controls():
    assert replay(case([100, 40]), 'current')['status'] == 'path_ended'
    assert replay(case([100, 20]), 'current')['quote_pnl'] == -80
    assert replay(case([100, 40], [15, 21]), 'current')['status'] == 'late_initial_quote'
    assert replay(case([100, 100, 100, 40], [1, 9, 17, 21]), 'current')['quote_pnl'] == -60


@pytest.mark.parametrize('change, reason', [
    ({'expected_usdc': float('nan')}, 'invalid_quote'),
    ({'expected_usdc': 0}, 'invalid_quote'),
    ({'open_qty': 1}, 'quantity_changed'),
    ({'quoted_at': None}, 'invalid_timestamp'),
    ({'quoted_at': '2026-09-12T00:00:03+00:00'}, 'future_quote'),
    ({'quoted_at': '2026-09-11T23:59:55+00:00'}, 'stale_quote'),
])
def test_invalid_quotes_censor_instead_of_becoming_prices(change, reason):
    data = case([100, 130])
    data['memecoin_route_series'][1].update(change)
    assert replay(data, 'moving_stop')['status'] == reason


def test_missing_path_and_post_close_prices_stay_unknown():
    assert replay(case([100, 110, 130], [1, 2, 20]), 'current')['status'] == 'quote_gap'
    data = case([100, 110, 130], [1, 2, 3])
    data['position']['closed_at'] = '2026-09-12T00:00:02+00:00'
    assert replay(data, 'current')['status'] == 'path_ended'


def test_chart_and_duplicate_quotes_cannot_move_threshold_or_confirm_stop():
    data = case([100, 200, 64, 63])
    data['memecoin_route_series'][1]['seam'] = 'chart'
    data['memecoin_route_series'][3]['quoted_at'] = data['memecoin_route_series'][2]['quoted_at']
    assert replay(data, 'moving_stop')['status'] == 'path_ended'


def test_safety_boundary_and_execution_failure_stay_unknown():
    data = case([100, 130])
    data['memecoin_exit_trigger_audit'] = [dict(reason='vol_death', allowed=1,
        checked_at='2026-09-12T00:00:02+00:00')]
    assert replay(data, 'current')['status'] == 'observed_safety_exit'
    data['memecoin_exit_execution_attempts'] = [dict(status='unknown')]
    assert replay(data, 'current')['status'] == 'execution_complexity'


def test_quantity_changes_and_other_exit_policies_are_not_silently_removed():
    data = case([100, 130])
    data['memecoin_fills'].extend([dict(kind='derisk'), dict(kind='bracket_exit')])
    assert replay(data, 'current')['status'] == 'partial_or_multiple_exit'


def test_disabled_target_stays_disabled_and_timecap_remains_active():
    import json
    data = case([100, 140], [1, 2])
    policy = json.loads(data['position']['funded_contract_json'])
    policy['policy']['tp_pct'] = 0
    data['position']['funded_contract_json'] = json.dumps(policy)
    assert replay(data, 'reduced_target')['status'] == 'path_ended'
    policy['policy']['max_hold_s'] = 1
    data['position']['funded_contract_json'] = json.dumps(policy)
    assert replay(data, 'reduced_target')['reason'] == 'b_timecap'


def test_atomic_quote_evidence_must_match_quantity_and_cash():
    import json
    data = case([100, 130])
    evidence = dict(input_atomic='10000000', expected_output_atomic='130000000',
                    quote_asset_usd=1, quote_asset_decimals=6)
    data['memecoin_route_series'][1]['quote_evidence_json'] = json.dumps(evidence)
    assert replay(data, 'current')['quote_pnl'] == 30
    evidence['input_atomic'] = '12345'
    data['memecoin_route_series'][1]['quote_evidence_json'] = json.dumps(evidence)
    assert replay(data, 'current')['status'] == 'atomic_quantity_mismatch'
    evidence.update(input_atomic='10000000', expected_output_atomic='1')
    data['memecoin_route_series'][1]['quote_evidence_json'] = json.dumps(evidence)
    assert replay(data, 'current')['status'] == 'quote_value_mismatch'


def test_quote_from_another_position_is_not_a_whole_position_price():
    data = case([100, 130])
    data['memecoin_route_series'][1]['position_id'] = 2
    assert replay(data, 'current')['status'] == 'quote_identity_mismatch'


def test_pairing_never_fills_unknown_with_zero():
    outcomes = {name: replay(case([100, 110, 74, 70]), name)
                for name in ('current', 'moving_stop', 'reduced_target', 'both')}
    row = dict(id=1, mint='test', network_id='solana', split='earlier', outcomes=outcomes)
    result = summarize([row])
    assert result['complete_n'] == 0
    assert result['unknown']['current'] == 1
    assert result['unknown']['moving_stop'] == 0


import pytest as _pytest
from sniper.research.wallet_execution.publication import diagnostic_mode as _diagnostic_mode
@_pytest.fixture(autouse=True)
def _explicit_legacy_diagnostics():
    with _diagnostic_mode():
        yield
