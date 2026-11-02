import hashlib
from sniper.office import grader as G, store


HORIZON = 1800000900.
LOOK = 1800002000.


def public_store(tmp_path):
    conn = store.open_store(tmp_path / 'office.db')
    path = tmp_path / 'metric_registry.json'
    path.write_bytes(store.canonical_json({'economics': {'horizon_s': 900.}}))
    conn.execute('INSERT INTO epochs VALUES(?,?)', ('e', store.canonical_json(dict(
        metric_registry_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        registered_look_id='look', registered_look_at=LOOK)).decode()))
    conn.commit()
    return conn


def receipt(state, **extra):
    return dict(state=state, execution_receipt_hash='a'*64, horizon_at=HORIZON,
                as_of_cut=HORIZON, known_at=LOOK, **extra)


def opportunity(conn, mint):
    store.append(conn, 'opportunity_log', dict(epoch_id='e', network_id='solana', mint=mint, first_eligible_at=1800000000., shift_id='us'))


def test_all_opportunities_preserved_and_missing_telemetry_censored(tmp_path):
    public = public_store(tmp_path)
    private = store.open_store(tmp_path / 'evaluation.db', evaluation=True)
    for mint in ('handover', 'unprocessed', 'skip'):
        opportunity(public, mint)
    def execution(op, decision):
        return receipt('no_entry') if op['mint'] == 'skip' else None
    rows = G.grade_epoch(public, private, 'e', execution_lookup=execution, null_lookup=lambda op: None, bound_usd_per_fill=1., look_id='look', registry_path=tmp_path/'metric_registry.json')
    assert len(rows) == 3
    assert rows[0]['decision_id'] is None and rows[0]['y'] is None and rows[0]['y_censored']
    assert rows[1]['y'] is None and rows[2]['y'] == 0.
    assert private.execute('SELECT COUNT(*) FROM grades').fetchone()[0] == 3
    assert public.execute("SELECT 1 FROM sqlite_master WHERE name='grades'").fetchone() is None
    assert G.grade_epoch(public, private, 'e', execution_lookup=execution, null_lookup=lambda op: None, bound_usd_per_fill=1., look_id='look', registry_path=tmp_path/'metric_registry.json') == rows
    assert private.execute('SELECT COUNT(*) FROM grades').fetchone()[0] == 3


def test_entered_partial_uses_actual_cashflow_and_single_bound_debit():
    row = G.economic_outcome(receipt('closed', entry_filled_at=1800000001., closed_at=1800000500., net_cashflow_usd=8., fill_count=2, partial=True), bound_usd_per_fill=1.5, horizon_at=HORIZON, look_at=LOOK)
    assert row == {'entered': True, 'y': 5., 'y_censored': False, 'partial': True, 'reason': None}
    assert G.economic_outcome({'state': 'open', 'execution_receipt_hash': 'b'*64}, bound_usd_per_fill=1., horizon_at=HORIZON, look_at=LOOK)['y'] is None
    assert G.economic_outcome({'state': 'no_entry'}, bound_usd_per_fill=1., horizon_at=HORIZON, look_at=LOOK)['y'] is None


def test_frozen_look_refuses_changed_inputs_and_public_store(tmp_path):
    import pytest
    public = public_store(tmp_path)
    private = store.open_store(tmp_path / 'evaluation.db', evaluation=True)
    opportunity(public, 'm')
    args = dict(execution_lookup=lambda op, decision: None, null_lookup=lambda op: None, bound_usd_per_fill=1., look_id='look', registry_path=tmp_path/'metric_registry.json')
    G.grade_epoch(public, private, 'e', **args)
    with pytest.raises(ValueError, match='frozen_look_inputs_changed'):
        G.grade_epoch(public, private, 'e', **dict(args, bound_usd_per_fill=2.))
    with pytest.raises(ValueError, match='private_evaluation_store_required'):
        G.grade_epoch(public, public, 'e', **args)
    assert private.execute('SELECT COUNT(*) FROM grades').fetchone()[0] == 1


def test_invalid_bound_and_cashflow_never_become_economic_zero():
    import pytest
    for bound in (None, True, -1., float('nan')):
        with pytest.raises(ValueError, match='invalid_execution_bound'):
            G.economic_outcome(None, bound_usd_per_fill=bound, horizon_at=HORIZON, look_at=LOOK)
    assert G.economic_outcome(dict(state='closed', net_cashflow_usd=float('nan'), fill_count=2, partial=False, execution_receipt_hash='a'*64), bound_usd_per_fill=1., horizon_at=HORIZON, look_at=LOOK)['y'] is None


def test_released_aggregates_reject_raw_rows_and_require_host_authority(tmp_path):
    import pytest
    conn = store.open_store(tmp_path / 'office.db')
    args = dict(epoch_id='e', kind='epoch_economics', released_at=100., aggregates=dict(opportunity_count=1, net_pnl_usd=None), source_heads={'grades': 'a'*64})
    with pytest.raises(ValueError, match='release_not_authorized'):
        G.release_aggregate(conn, **args, authorize_release=lambda *args: False)
    with pytest.raises(ValueError, match='raw_or_unknown_aggregate_field'):
        G.release_aggregate(conn, **dict(args, aggregates={'rows': [{'coin': 'private'}]}), authorize_release=lambda *args: True)
    first = G.release_aggregate(conn, **args, authorize_release=lambda *args: True)
    assert G.release_aggregate(conn, **args, authorize_release=lambda *args: True) == first
    assert conn.execute('SELECT COUNT(*) FROM sealed_views').fetchone()[0] == 1


def test_fired_agent_is_not_removed_from_opportunity_grades(tmp_path):
    public = public_store(tmp_path)
    private = store.open_store(tmp_path / 'evaluation.db', evaluation=True)
    opportunity(public, 'm')
    store.append(public, 'claims', dict(claim_hash='claim'))
    store.append(public, 'decisions', dict(candidate_seq=1, decision_id='d', actor_id='fired', claim_hash='claim'))
    public.execute('INSERT INTO agents VALUES(?,?)', ('fired', '{"state":"fired"}'))
    public.commit()
    rows = G.grade_epoch(public, private, 'e', execution_lookup=lambda op, decision: receipt('no_entry'), null_lookup=lambda op: None, bound_usd_per_fill=1., look_id='look', registry_path=tmp_path/'metric_registry.json')
    assert rows[0]['actor_id'] == 'fired' and rows[0]['y'] == 0.
