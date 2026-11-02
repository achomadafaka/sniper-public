"""Frozen-horizon economics never substitute a later mutable order or mark."""
import hashlib
import json

import pytest

from sniper.office import grader as G, store as S


def proof(**overrides):
    return dict(state='open', execution_receipt_hash='a'*64, horizon_at=100.,
                as_of_cut=100., known_at=110., entry_filled_at=80., **overrides)


def outcome(receipt):
    return G.economic_outcome(receipt, bound_usd_per_fill=1., horizon_at=100., look_at=120.)


def test_immutable_open_at_horizon_does_not_use_later_closed_order():
    frozen_bytes = S.canonical_json(proof())
    current_order = dict(state='closed', closed_at=105., net_cashflow_usd=999., last_mark_usd=888.)
    row = outcome(json.loads(frozen_bytes))
    assert current_order['state'] == 'closed'
    assert row['entered'] is True and row['y'] is None and row['y_censored'] is True
    assert frozen_bytes == S.canonical_json(proof())


def test_later_close_is_censored_even_with_large_positive_cashflow():
    receipt = proof()
    receipt.update(state='closed', closed_at=101., net_cashflow_usd=999., fill_count=2, partial=False)
    row = outcome(receipt)
    assert row['entered'] is True and row['y'] is None and row['y_censored'] is True


@pytest.mark.parametrize('field,value', [('horizon_at', None), ('as_of_cut', None),
                                      ('as_of_cut', 101.), ('known_at', 121.), ('known_at', 99.),
                                      ('entry_filled_at', None), ('entry_filled_at', 101.)])
def test_missing_wrong_or_future_cut_proof_is_unknown(field, value):
    receipt = proof()
    receipt.update(state='closed', closed_at=99., net_cashflow_usd=999., fill_count=2, partial=False)
    receipt[field] = value
    row = outcome(receipt)
    assert row['y'] is None and row['y_censored'] is True


def test_unknown_exit_ignores_cashflows_and_last_mark():
    receipt = proof()
    receipt.update(state='exit_unknown', reason='unpriced_exit', net_cashflow_usd=999., last_mark_usd=999.)
    row = outcome(receipt)
    assert row['entered'] is True and row['y'] is None and row['reason'] == 'unpriced_exit'


def test_closed_inside_horizon_single_bound_and_proven_no_entry_only_zero():
    receipt = proof()
    receipt.update(state='closed', closed_at=99., net_cashflow_usd=8., fill_count=2, partial=True)
    assert outcome(receipt)['y'] == 6.
    receipt = dict(state='no_entry', execution_receipt_hash='a'*64, horizon_at=100., as_of_cut=100., known_at=110.)
    assert outcome(receipt)['y'] == 0.
    receipt.pop('as_of_cut')
    assert outcome(receipt)['y'] is None


@pytest.mark.parametrize('history', [{'closed_at': 99.}, {'fill_count': 2},
                                    {'net_cashflow_usd': -999.}, {'partial': True},
                                    {'entry_filled_at': 80.}])
def test_no_entry_with_contradictory_history_is_not_zero(history):
    receipt = dict(state='no_entry', execution_receipt_hash='a'*64, horizon_at=100., as_of_cut=100., known_at=110., **history)
    row = outcome(receipt)
    assert row['y'] is None and row['y_censored'] is True


@pytest.mark.parametrize('state,closed_at,n,expected', [
    ('closed', 101., -999., None), ('open', None, -999., None),
    ('exit_unknown', None, 999., None), ('closed', 99., 6., 6.),
    ('closed', 99., -999., None), ('no_entry', None, 0., 0.),
    ('capacity_refused', None, 0., 0.), ('capacity_refused', None, 999., None),
    ('unknown', None, 0., None),
])
def test_null_requires_symmetric_lifecycle(tmp_path, state, closed_at, n, expected):
    public, private, path = fixture(tmp_path)
    null = dict(state=state, horizon_at=100., as_of_cut=100., known_at=110.,
                execution_receipt_hash='b'*64, bound_usd_per_fill=1., n=n)
    if state in {'closed', 'open', 'exit_unknown'}:
        null.update(entry_filled_at=80., closed_at=closed_at, net_cashflow_usd=8., fill_count=2, partial=False)
    rows = G.grade_epoch(public, private, 'e', execution_lookup=lambda op, decision: proof(),
                         null_lookup=lambda op: null, bound_usd_per_fill=1., look_id='look', registry_path=path)
    assert rows[0]['n'] == expected
    assert rows[0]['n_censored'] is (expected is None)
    public.close()
    private.close()


@pytest.mark.parametrize('state', ['no_entry', 'capacity_refused'])
def test_null_no_entry_cannot_hide_execution_history(tmp_path, state):
    public, private, path = fixture(tmp_path)
    null = dict(state=state, horizon_at=100., as_of_cut=100., known_at=110.,
                execution_receipt_hash='b'*64, bound_usd_per_fill=1., n=0.,
                closed_at=99., fill_count=2, net_cashflow_usd=-999.)
    rows = G.grade_epoch(public, private, 'e', execution_lookup=lambda op, decision: proof(),
                         null_lookup=lambda op: null, bound_usd_per_fill=1., look_id='look', registry_path=path)
    assert rows[0]['n'] is None and rows[0]['n_censored'] is True
    public.close()
    private.close()


def fixture(tmp_path):
    public = S.open_store(tmp_path/'office.db')
    private = S.open_store(tmp_path/'evaluation.db', evaluation=True)
    path = tmp_path/'registry.json'
    path.write_bytes(S.canonical_json({'economics': {'horizon_s': 20.}}))
    epoch = dict(metric_registry_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
                 registered_look_id='look', registered_look_at=120.)
    public.execute('INSERT INTO epochs VALUES(?,?)', ('e', S.canonical_json(epoch).decode()))
    public.commit()
    S.append(public, 'opportunity_log', dict(epoch_id='e', network_id='solana', mint='m', first_eligible_at=80., shift_id='us'))
    return public, private, path


def test_grade_epoch_uses_pinned_registry_and_frozen_look(tmp_path):
    public, private, path = fixture(tmp_path)
    args = dict(execution_lookup=lambda op, decision: proof(), null_lookup=lambda op: None,
                bound_usd_per_fill=1., look_id='look', registry_path=path)
    rows = G.grade_epoch(public, private, 'e', **args)
    assert len(rows) == 1 and rows[0]['y_censored'] and rows[0]['entered'] is True
    assert rows[0]['horizon_at'] == 100. and rows[0]['look_at'] == 120.
    with pytest.raises(ValueError, match='unregistered_look'):
        G.grade_epoch(public, private, 'e', **dict(args, look_id='other'))
    path.write_text('{"economics":{"horizon_s":1}}')
    with pytest.raises(G.RegistryMismatch):
        G.grade_epoch(public, private, 'e', **args)
    public.close()
    private.close()


def test_frozen_grade_retains_open_receipt_and_censors_unproved_null(tmp_path):
    public, private, path = fixture(tmp_path)
    immutable = S.append(public, 'receipts', {'kind': 'execution_snapshot', 'snapshot': proof()})

    def resolver(op, decision):
        raw = public.execute('SELECT payload_json FROM receipts WHERE row_hash=?', (immutable,)).fetchone()[0]
        return dict(json.loads(raw)['snapshot'], execution_receipt_hash=immutable)

    args = dict(execution_lookup=resolver,
                null_lookup=lambda op: {'n': 999., 'execution_receipt_hash': 'b'*64, 'bound_usd_per_fill': 1.},
                bound_usd_per_fill=1., look_id='look', registry_path=path)
    first = G.grade_epoch(public, private, 'e', **args)
    S.append(public, 'receipts', {'kind': 'later_order_close', 'closed_at': 125., 'net_cashflow_usd': 999.})
    assert G.grade_epoch(public, private, 'e', **args) == first
    assert first[0]['entered'] is True and first[0]['y'] is None and first[0]['y_censored']
    assert first[0]['n'] is None and first[0]['n_censored']
    late = proof()
    late.update(state='closed', closed_at=125., net_cashflow_usd=999., fill_count=2, partial=False)
    with pytest.raises(ValueError, match='frozen_look_inputs_changed'):
        G.grade_epoch(public, private, 'e', **dict(args, execution_lookup=lambda op, decision: late))
    assert private.execute('SELECT COUNT(*) FROM grades').fetchone()[0] == 1
    public.close()
    private.close()
