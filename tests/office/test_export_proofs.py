import hashlib
import json
import pytest
from sniper.office import grader as G, store, roster as R


def equity_ref(root, cut):
    from sniper.office import ledger
    folder = root/'equity'
    folder.mkdir()
    path = folder/'ledger.db'
    conn = ledger.open_ledger(path)
    ledger.open_epoch(conn, epoch_id='e', clip_usd=25., rule_version='fixture', actor='fixture', note='fixture', now=cut-10)
    ledger.open_book(conn, book_id='b', epoch_id='e', kind='evaluated', actor='fixture', note='fixture', now=cut-10)
    conn.close()
    path.chmod(0o444)
    doc = dict(schema='office-equity-snapshot-v1', epoch_id='e', as_of_cut=cut, taken_at=cut,
               clock_sample_id='fixture-clock', clock_offset_s=0., clock_sampled_at_host=cut,
               ledger=dict(path='equity/ledger.db', sha256=hashlib.sha256(path.read_bytes()).hexdigest(), user_version=0,
                           books=['b'], order_count=0, mark_count=0, cashflow_count=0,
                           max_order_time=None, max_cashflow_at=None, max_mark_at=None),
               fx=dict(sol_usd=150., sol_usd_at=cut, source='fixture', source_ref='fixture:fx', max_age_s=120.),
               receipt=dict(id='fixture-snapshot', written_by='fixture', written_by_identity='fixture-identity'))
    doc['receipt']['metadata_sha256'] = hashlib.sha256(store.canonical_json(doc)).hexdigest()
    sidecar = folder/'snapshot.json'
    sidecar.write_bytes(store.canonical_json(doc))
    sidecar.chmod(0o444)
    return dict(path='equity/snapshot.json', sha256=hashlib.sha256(sidecar.read_bytes()).hexdigest())


def test_proof_loader_rejects_modified_bytes_and_escape(tmp_path):
    raw = b'{"id":"fee","fee_usd":20}'
    (tmp_path / 'fee.json').write_bytes(raw)
    ref = dict(id='fee', path='fee.json', sha256=hashlib.sha256(raw).hexdigest())
    assert G.read_proof(tmp_path, ref)['fee_usd'] == 20
    (tmp_path / 'fee.json').write_bytes(b'{"id":"fee","fee_usd":0}')
    with pytest.raises(ValueError, match='proof_hash_mismatch'):
        G.read_proof(tmp_path, ref)
    with pytest.raises(ValueError, match='proof_path_escape'):
        G.read_proof(tmp_path, dict(ref, path='../outside.json'))


def test_calibration_cannot_be_supported_by_an_empty_or_wrong_design(tmp_path):
    from copy import deepcopy
    params = dict(k=1, n_plan=240, days=30, block_days=3, alpha_scale=.5, delta0=.1, delta1=.5, day_method='t_block', sigma=3., draws=10000, alpha_k=.025)
    body = dict(schema='office-calibration-proof-v1', k=1, params=dict(n_coins=240, days=30, block_days=3, alpha_scale=.5, delta0=.1, delta1=.5, day_method='t_block', sigma=3., draws=10000, alpha_k=.025), supported_for_k=True, generators=[])
    with pytest.raises(ValueError, match='calibration_generators'):
        G.validate_calibration(body, params)
    body['generators'] = [dict(name=name, supported=True) for name in ('iid', 'heavy_tail', 'day_cluster', 'serial', 'adaptive_entry', 'pods_shifts')]
    body['proof_id'] = hashlib.sha256(store.canonical_json(body)).hexdigest()
    G.validate_calibration(body, params)
    wrong = deepcopy(params)
    wrong['days'] = 14
    with pytest.raises(ValueError, match='calibration_design_mismatch'):
        G.validate_calibration(body, wrong)


def test_export_refuses_missing_epoch_manifest_binding(tmp_path):
    public = store.open_store(tmp_path / 'office.db')
    private = store.open_store(tmp_path / 'evaluation.db', evaluation=True)
    path = tmp_path / 'manifest.json'
    path.write_text('{}')
    with pytest.raises(ValueError, match='epoch_manifest_not_frozen'):
        G.export_epoch(public, private, 'e', manifest_path=path, manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), config_root=tmp_path, audit_snapshot={})


@pytest.mark.parametrize("coverage", [True, False, "gap", "missing_source", "legacy_grade", "legacy_horizon_grade"])
def test_verified_export_resolves_files_and_feeds_verdict(tmp_path, coverage, monkeypatch, request):
    import shutil
    from pathlib import Path
    from sniper.office import auditor
    from sniper.office.readiness import verdict as V
    root = tmp_path / 'config'
    (root / 'proofs').mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / 'config' / 'office'
    for name in ('cost_allocation_rule.json', 'proofs/calibration-k1.json'):
        shutil.copyfile(source / name, root / name)
    def write(name, doc):
        path = root / name
        path.write_bytes(store.canonical_json(doc))
        return hashlib.sha256(path.read_bytes()).hexdigest()
    def filehash(name):
        return hashlib.sha256((root / name).read_bytes()).hexdigest()
    calibration = json.loads((root / 'proofs/calibration-k1.json').read_bytes())
    params = {**calibration['params'], 'k': 1, 'n_plan': 240, 'clip': 25., 'loss_bound': 27., 'n_upper': None, 'seed': 1}
    start = 1800000000. // 86400 * 86400
    end = start + 30 * 86400
    economics = dict(clip=25., bound_usd_per_fill=1., loss_bound_components=dict(clip=25., B_entry=1., B_exit=1., max_network_rent=0.), horizon_s=900., venue='ps', clip_band='fixed', latency_regime='null', mark_max_age_s=60, exec_bound_evidence_id='bound')
    registry_hash = write('metric_registry.json', {'economics': economics})
    bound_hash = write('proofs/bound.json', dict(id='bound', venue='ps', clip_band='fixed', latency_regime='null', bound_usd_per_fill=1., sample_n=20, source_refs=['isolated-test-fixture'], method='fixture', created_at=start-1))
    shift_hash = write('shifts.json', dict(shifts=[dict(id='us', start_hour=0, end_hour=24)]))
    starts = [start - 86400 + 100 + i for i in range(30)]
    latency_hash = write('proofs/latency.json', dict(id='latency', shift_id='us', source_kind='discovery', known_through_at=start-1, latency_samples_s=[10.]*30, attempt_ids=[str(i) for i in range(30)], first_eligible_at=starts, filled_at=[t+10. for t in starts], submitted_count=40, filled_share=.75))
    delay_hash = write('l_null.json', dict(epoch_id='e', shifts=[dict(shift_id='us', utc_hours=[0, 24], l_null_s=10., sample_n=30, evidence_ref='latency')]))
    manifest = dict(epoch_id='e', look_id='look', opened_at=start, ended_at=end, look_at=end, params=params, eligible_sources=['feed', 'extra'] if coverage=='missing_source' else ['feed'], metric_registry_hash=registry_hash, cost_rule_hash=filehash('cost_allocation_rule.json'), cost_rule_id='aa0e9a583757cf929f3babbbb3b63f3c6bd67a49c3a4a63d3f9689a6eeb83290', l_null_hash=delay_hash, shifts_hash=shift_hash, l_null_evidence_refs={'latency': dict(id='latency', path='latency.json', sha256=latency_hash)}, calibration=dict(id=calibration['proof_id'], path='calibration-k1.json', sha256=filehash('proofs/calibration-k1.json')), execution_bound=dict(id='bound', path='bound.json', sha256=bound_hash), stopping_rule='fixed-30-days')
    manifest['equity_snapshot'] = equity_ref(root, end)
    manifest_hash = write('manifest.json', manifest)
    public = store.open_store(tmp_path / 'office.db')
    private = store.open_store(tmp_path / 'evaluation.db', evaluation=True)
    from .epoch_fixture import build
    nominee, refs = build(public, root/'seed', now=start)
    for ref in refs.values():
        target = root/ref['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(root/'seed'/ref['path'], target)
        ref['sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
    refs['calibration'] = dict(path='proofs/calibration-k1.json', sha256=filehash('proofs/calibration-k1.json'))
    for key,name in [('metric_registry','metric_registry.json'),('l_null','l_null.json'),('execution_bound','proofs/bound.json')]:
        refs[key] = dict(path=name, sha256=filehash(name))
    nominee.update(metric_registry_hash=registry_hash, l_null_file_hash=delay_hash,
                   exec_bound_evidence_id='bound', calibration_proof_id=calibration['proof_id'])
    for ref in refs.values():
        (root/ref['path']).chmod(0o444)
    frozen = dict(evaluation_manifest_sha256=manifest_hash, opened_at=start, evaluation_broken=False,
                  metric_registry_hash=registry_hash, registered_look_id='look', registered_look_at=end,
                  kind='evaluated', k=1, alpha_k=.025, hypothesis_id='fixture', epoch_id='e',
                  nominee=nominee, nominee_refs=refs,
                  nominee_config_hash=hashlib.sha256(store.canonical_json(nominee)).hexdigest())
    public.execute('INSERT INTO epochs VALUES(?,?)', ('e', store.canonical_json(frozen).decode()))
    public.commit()
    from .launcher_fixture import LauncherFixture
    from sniper.office import launcher_policy
    monkeypatch.setattr(launcher_policy, 'readonly_mount', lambda fd:True)
    launcher = LauncherFixture(root, lambda:end)
    request.addfinalizer(launcher.close)
    store.append(public, 'opportunity_log', dict(epoch_id='e', network_id='solana', mint='m', first_eligible_at=start+100, shift_id='us'))
    if coverage:
        from sniper.office.capture import record_coverage
        receipt = store.append(public, 'receipts', dict(kind='eligible_source_poll', source='feed', state='observed', from_at=start, to_at=end, known_at=end))
        if coverage == 'gap':
            record_coverage(public, epoch_id='e', source='feed', from_at=start, to_at=start+100, proof_hash=receipt)
            record_coverage(public, epoch_id='e', source='feed', from_at=start+101, to_at=end, proof_hash=receipt)
        else:
            record_coverage(public, epoch_id='e', source='feed', from_at=start, to_at=end, proof_hash=receipt)
    if coverage in {'legacy_grade', 'legacy_horizon_grade'}:
        op_hash = public.execute('SELECT row_hash FROM opportunity_log').fetchone()[0]
        legacy = dict(epoch_id='e', candidate_seq=1, kind='coin_economics_v1', look_id='look', entered=False, y=0., n=0., y_censored=False, n_censored=False, opportunity_hash=op_hash, bound_usd_per_fill=1., shift_id='us', coin_id='solana:m', day='2027-01-15')
        if coverage == 'legacy_horizon_grade':
            legacy.update(horizon_at=start+100+900., look_at=end, metric_registry_hash=registry_hash)
        store.append(private, 'grades', legacy)
    else:
        G.grade_epoch(public, private, 'e', execution_lookup=lambda op, decision: dict(state='no_entry', execution_receipt_hash='a'*64, horizon_at=op['first_eligible_at']+900., as_of_cut=op['first_eligible_at']+900., known_at=end), null_lookup=lambda op: dict(state='no_entry', n=0., execution_receipt_hash='b'*64, bound_usd_per_fill=1., horizon_at=op['first_eligible_at']+900., as_of_cut=op['first_eligible_at']+900., known_at=end), bound_usd_per_fill=1., look_id='look', registry_path=root/'metric_registry.json')
    snapshot = dict(state='clean', epoch_id='e', at=end, checks={key: dict(state='clean') for key in ('office', 'evaluation', 'safety', 'reconciliation', 'null_reconciliation')})
    for name, conn, tables in (('office', public, store.APPEND_ONLY_TABLES-store.PRIVATE_TABLES), ('evaluation', private, store.PRIVATE_TABLES)):
        snapshot['checks'][name]['heads'] = {table: list(row) if (row := conn.execute('SELECT seq,row_hash FROM '+table+' ORDER BY seq DESC LIMIT 1').fetchone()) else [0, ''] for table in tables}
    snapshot['checks']['safety']['counters'] = dict.fromkeys(auditor.COUNTERS, 0)
    if coverage in {'legacy_grade', 'legacy_horizon_grade'}:
        with pytest.raises(ValueError, match='grade_horizon_unresolved'):
            G.export_epoch(public, private, 'e', manifest_path=root/'manifest.json', manifest_sha256=manifest_hash, config_root=root, audit_snapshot=snapshot, clock=lambda: end, authenticate_snapshot=lambda receipt,digest:True, runtime_dir=tmp_path/'runtime', launcher_policy=launcher.policy, probe_write=launcher.probe)
        return
    if coverage is not True:
        with pytest.raises(ValueError, match='opportunity_coverage_unknown'):
            G.export_epoch(public, private, 'e', manifest_path=root/'manifest.json', manifest_sha256=manifest_hash, config_root=root, audit_snapshot=snapshot, clock=lambda: end, authenticate_snapshot=lambda receipt,digest:True, runtime_dir=tmp_path/'runtime', launcher_policy=launcher.policy, probe_write=launcher.probe)
        return
    export_args = dict(manifest_path=root/'manifest.json', manifest_sha256=manifest_hash,
        config_root=root, audit_snapshot=snapshot, clock=lambda:end,
        authenticate_snapshot=lambda receipt,digest:True, runtime_dir=tmp_path/'runtime',
        launcher_policy=launcher.policy, probe_write=launcher.probe)
    for missing in ('launcher_policy', 'probe_write', 'authenticate_snapshot'):
        args = dict(export_args)
        del args[missing]
        with pytest.raises((ValueError, R.OrgRefused), match='host_launcher_callbacks_required|auth'):
            G.export_epoch(public, private, 'e', **args)
    original_read_proof = G.read_proof
    injected = []
    def concurrent_append(root_path, ref):
        if not injected:
            injected.append(True)
            writer = store.open_store(tmp_path / 'office.db')
            evaluator = store.open_store(tmp_path / 'evaluation.db', evaluation=True)
            try:
                store.append(writer, 'opportunity_log', dict(epoch_id='e', network_id='solana', mint='late', first_eligible_at=start+200, shift_id='us'))
                G.grade_epoch(writer, evaluator, 'e', execution_lookup=lambda op, decision: dict(state='no_entry', execution_receipt_hash='a'*64, horizon_at=op['first_eligible_at']+900., as_of_cut=op['first_eligible_at']+900., known_at=end), null_lookup=lambda op: dict(state='no_entry', n=0., execution_receipt_hash='b'*64, bound_usd_per_fill=1., horizon_at=op['first_eligible_at']+900., as_of_cut=op['first_eligible_at']+900., known_at=end), bound_usd_per_fill=1., look_id='look', registry_path=root/'metric_registry.json')
            finally:
                writer.close()
                evaluator.close()
        return original_read_proof(root_path, ref)
    monkeypatch.setattr(G, 'read_proof', concurrent_append)
    result = G.export_epoch(public, private, 'e', manifest_path=root/'manifest.json', manifest_sha256=manifest_hash, config_root=root, audit_snapshot=snapshot, clock=lambda: end, authenticate_snapshot=lambda receipt,digest:True, runtime_dir=tmp_path/'runtime', launcher_policy=launcher.policy, probe_write=launcher.probe)
    assert injected and len(result['coins']) == 1
    assert result['strict_equity']['summary']['state'] == 'known'
    assert result['strict_equity']['summary']['binding_kind_counts'] == {}
    assert result['proofs']['strict_equity'] == result['strict_equity']['proofs']
    assert public.execute('SELECT COUNT(*) FROM opportunity_log').fetchone()[0] == 2
    assert result['proofs']['cut_heads']['office']['opportunity_log'][0] == 1
    assert result['proofs']['cut_counts']['eligible_coins'] == 1
    assert not public.in_transaction and not private.in_transaction
    public.execute('BEGIN')
    private.execute('BEGIN')
    try:
        with pytest.raises(R.OrgRefused, match='owned_transaction'):
            G.export_epoch(public, private, 'e', manifest_path=root/'manifest.json', manifest_sha256=manifest_hash, config_root=root, audit_snapshot=snapshot, clock=lambda: end, authenticate_snapshot=lambda receipt,digest:True, runtime_dir=tmp_path/'runtime', launcher_policy=launcher.policy, probe_write=launcher.probe)
        assert public.in_transaction and private.in_transaction
    finally:
        public.rollback()
        private.rollback()
    monkeypatch.setattr(G, 'read_proof', original_read_proof)
    assert result['cost'] == V.flat_office_cost(V.epoch_days(start, end))
    coin = V.Coin(**{k: v for k, v in result['coins'][0].items() if k != 'shift_id'})
    epoch = V.Epoch(coins=(coin,), k=1, delta0=params['delta0'], delta1=params['delta1'], clip=25., loss_bound=27., n_upper=None, cost=result['cost'], exec_bound_present=True, counters=result['counters'], calibrated_ks=frozenset([1]), seed=1, n_plan=240, block_days=params['block_days'], alpha_scale=params['alpha_scale'], day_method=params['day_method'], min_days=params['days'], proofs=result['proofs'])
    assert not V.proof_problems(epoch)
    assert V.evaluate(epoch).status != 'pass'
    tampered = []
    def mutate_during_export(root_path, ref):
        if not tampered:
            tampered.append(True)
            (root/'pods.json').chmod(0o644)
            (root/'pods.json').write_text('{}')
            (root/'pods.json').chmod(0o444)
        return original_read_proof(root_path, ref)
    monkeypatch.setattr(G, 'read_proof', mutate_during_export)
    with pytest.raises(R.OrgRefused, match='nominee_changed'):
        G.export_epoch(public, private, 'e', manifest_path=root/'manifest.json', manifest_sha256=manifest_hash, config_root=root, audit_snapshot=snapshot, clock=lambda: end, authenticate_snapshot=lambda receipt,digest:True, runtime_dir=tmp_path/'runtime', launcher_policy=launcher.policy, probe_write=launcher.probe)
    changed = json.loads(public.execute('SELECT payload_json FROM epochs WHERE id=?', ('e',)).fetchone()[0])
    assert changed['status'] == 'broken'


def test_withdrawn_calibration_is_explicitly_rejected():
    with pytest.raises(ValueError, match='withdrawn_calibration_proof'):
        G.validate_calibration({'proof_id': '1bd1d0507535effba801103929d5f6c0f4fc84aee43f3858ca7f3bfc431433c8'}, {})
