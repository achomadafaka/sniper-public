"""Contract counterexamples: dense labels cannot replace executable provenance."""
from copy import deepcopy
import pytest
from sniper.research.wallet_execution.evidence import episode_gate, study_gate, digest, PROTOCOL


def episode():
    frames = []
    for second in range(-120, 1803):
        raw = {'chain': '56', 'block_hash': f'block-{second}', 'quantity': '1000000'}
        frames.append({'at': float(second), 'available_at': float(second), 'sequence': second + 120,
                       'chain': '56', 'raw': raw, 'raw_digest': digest(raw), 'quality': 'ok',
                       'buy': {'spend_usd': 100., 'quantity_atomic': '1000000', 'decimals': 6,
                               'floor_atomic': '1000000', 'fee_usd': 1., 'route': 'v2:pool'},
                       'sells': {'1000000': {'floor_usd': 110., 'expected_usd': 111.,
                                            'fee_usd': 1., 'route': 'v2:pool'}}})
    return {'protocol': PROTOCOL, 'origin_id': 'source-1', 'wallet': 'alice', 'chain': '56',
            'mint': 'token', 'source': {'kind': 'paid_swap', 'identity_status': 'verified',
            'address': 'owner', 'transaction': 'source-tx', 'paid_atomic': '100000000',
            'acquired_atomic': '1000000', 'decimals': 6, 'spent_usd': 100.,
            'source_at': 0., 'available_at': 0., 'received_at': 0., 'order': [10, 2, 0],
            'proof': {'transaction_digest': 'a'*64, 'identity_digest': 'b'*64}},
            'window': {'start': -120., 'end': 1802., 'entry_wait_s': 0.,
                       'max_hold_s': 1800., 'settlement_margin_s': 2.},
            'execution': {'entry_delay_s': 1., 'exit_delay_s': 1., 'settlement_s': 1., 'max_quote_age_s': 1.,
                          'calibration_before': -121., 'calibration_digest': 'c'*64,
                          'cost_components': ['gas', 'tax', 'impact', 'slippage', 'approval'],
                          'cost_model_digest': 'd'*64, 'state_verified': True},
            'features': {}, 'frames': frames}


def test_positive_episode_is_not_a_supported_strategy():
    e = episode()
    assert episode_gate(e).eligible
    assert not study_gate({'protocol': PROTOCOL}, [e]).eligible


@pytest.mark.parametrize('mutate,code', [
    (lambda e: e.pop('protocol'), 'protocol_missing_or_legacy'),
    (lambda e: e.update(frames=[]), 'path_resolution_or_gap'),
    (lambda e: e['frames'].pop(124), 'path_resolution_or_gap'),
    (lambda e: e['source'].update(kind='airdrop'), 'identity_unproven'),
    (lambda e: e['source'].update(identity_status='claimed'), 'identity_unproven'),
    (lambda e: e['frames'][120].update(chain='8453'), 'execution_evidence_missing'),
    (lambda e: e['frames'][120]['buy'].update(quantity_atomic=1000000), 'execution_evidence_missing'),
    (lambda e: e['frames'][120]['buy'].update(fee_usd=None), 'cost_or_latency_unknown'),
    (lambda e: e['execution'].update(entry_delay_s=None), 'cost_or_latency_unknown'),
    (lambda e: e['execution'].pop('max_quote_age_s'), 'cost_or_latency_unknown'),
    (lambda e: e['execution'].update(calibration_before=1), 'ordering_or_lookahead'),
    (lambda e: e['frames'][121].update(available_at=0), 'ordering_or_lookahead'),
    (lambda e: e['frames'][121].update(at=-1), 'ordering_or_lookahead'),
    (lambda e: e['frames'][121].update(raw_digest='0'*64), 'execution_evidence_missing'),
    (lambda e: e['frames'][121].update(quality='provider_timeout'), 'outcome_censored'),
    (lambda e: e['window'].update(end=1799), 'path_resolution_or_gap'),
    (lambda e: e['frames'][121].update(high=200., low=10.), 'ordering_or_lookahead'),
])
def test_gate_rejects_each_counterexample(mutate, code):
    e = episode(); mutate(e)
    assert code in episode_gate(e).codes


def test_no_fake_sorting_repairs_regressed_path():
    e = episode(); e['frames'][123], e['frames'][124] = e['frames'][124], e['frames'][123]
    assert not episode_gate(e).eligible


def test_complete_flag_and_nominal_cadence_do_not_qualify():
    e = episode(); e['frames'] = e['frames'][::60]; e['complete'] = True; e['cadence_s'] = 1
    assert 'path_resolution_or_gap' in episode_gate(e).codes


def test_feature_availability_is_prior_only():
    e = episode(); e['features'] = {'mcap': {'value': 10, 'available_at': 3}}
    assert 'ordering_or_lookahead' in episode_gate(e, decision_at=1).codes


def test_study_rejects_same_data_winner_and_mixed_wallets():
    e = episode()
    m = {'protocol': PROTOCOL, 'wallet': 'bob', 'chain': '56', 'sealed_at': 2,
         'validation_start': 1, 'test_start': 1, 'trials': list(range(9))}
    g = study_gate(m, [e]); assert not g.eligible
    assert 'cohort_invalid' in g.codes and 'validation_missing' in g.codes


def test_digest_is_canonical_and_refuses_nonfinite():
    assert digest({'a': 1, 'b': 2}) == digest({'b': 2, 'a': 1})
    with pytest.raises(ValueError): digest({'a': float('nan')})
