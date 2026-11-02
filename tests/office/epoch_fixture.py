"""Isolated, explicitly synthetic evaluated-nominee inputs; no production evidence."""
import hashlib
import json
from pathlib import Path

from sniper.office import memory_store as MS, roster as R, store as S
from sniper.office.epoch_transition import tool_hash
from sniper.office.readiness import verdict as V


def build(conn, root, *, k=1, now=1000.):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)
    refs = {}

    def put(key, name, doc):
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.chmod(0o644)
        raw = doc.encode() if isinstance(doc, str) else S.canonical_json(doc)
        path.write_bytes(raw)
        path.chmod(0o444)
        ref = dict(path=name, sha256=hashlib.sha256(raw).hexdigest())
        refs[key] = ref
        return ref['sha256']

    if not conn.execute('SELECT 1 FROM agents').fetchone():
        a = dict(agent_id='a', role='Scout', state='active', model='fixture-model', provider='fixture', family='gpt', prompt_hash='pending')
        conn.execute('INSERT INTO agents VALUES(?,?)', ('a', S.canonical_json(a).decode()))
        conn.commit()
    prompts, models = {}, {}
    for aid, payload in conn.execute('SELECT id,payload_json FROM agents').fetchall():
        a = json.loads(payload)
        if a['state'] != 'active':
            continue
        prompts[aid] = put('prompt:'+aid, 'prompts/'+aid+'.txt', 'fixture prompt '+aid)
        models[aid] = {key: a[key] for key in ('model', 'provider', 'family')}
        a['prompt_hash'] = prompts[aid]
        conn.execute('UPDATE agents SET payload_json=? WHERE id=?', (S.canonical_json(a).decode(), aid))
    conn.commit()
    snapshot = MS.seal(MS.OfficeMemory(conn), epoch_id='fixture-memory', cut=now-1)
    source = Path(__file__).resolve().parents[2]/'config'/'office'
    rule = json.loads((source/'cost_allocation_rule.json').read_bytes())
    put('cost_rule', 'cost_allocation_rule.json', rule)
    cost_id = hashlib.sha256(S.canonical_json(rule)).hexdigest()
    pods = put('pods_rule', 'pods.json', json.loads((source/'pods.json').read_bytes()))
    book = json.loads((source/'book_policy_pilot.json').read_bytes())
    put('book_policy', 'book_policy.json', dict(label='fixture-evaluated', evaluation=True, policy=book['policy']))
    learning = put('learning_rule', 'learning_rule.json', {'id': 'fixture-learning', 'rule': 'fixed'})
    metric = put('metric_registry', 'metric_registry.json', {'economics': dict(clip=25., bound_usd_per_fill=1., exec_bound_evidence_id='fixture-bound', horizon_s=900., venue='ps', clip_band='fixed', latency_regime='null')})
    latency = put('l_null', 'l_null.json', {'id': 'fixture-null', 'shifts': [{'shift_id': 'us', 'l_null_s': 10.}]})
    put('execution_bound', 'proofs/bound.json', dict(id='fixture-bound', bound_usd_per_fill=1., venue='ps', clip_band='fixed', latency_regime='null', created_at=now-1, sample_n=30, source_refs=['fixture-only']))
    guard = put('host_guard', 'host_guard.json', dict(mode='full', provisional=False, evaluation_allowed=True, source='synthetic-test-baseline'))
    params = dict(n_coins=240, days=30, alpha_scale=.1, block_days=5, delta0=1., delta1=2.5, sigma=3., day_method='t_block', alpha_k=V.alpha_k(k), draws=V.draws_for(V.alpha_k(k)))
    calibration = dict(schema='office-calibration-proof-v1', k=k, supported_for_k=True, params=params,
                       generators=[dict(name=name, supported=True) for name in ('iid','heavy_tail','day_cluster','serial','adaptive_entry','pods_shifts')])
    calibration['proof_id'] = hashlib.sha256(S.canonical_json(calibration)).hexdigest()
    put('calibration', 'proofs/calibration.json', calibration)
    nominee = dict(memory_snapshot_id=snapshot, learning_rule=dict(id='fixture-learning', sha256=learning),
                   prompt_hashes=prompts, model_ids=models, pods_rule_sha256=pods, cost_rule_id=cost_id,
                   metric_registry_hash=metric, calibration_proof_id=calibration['proof_id'], delta0=1., delta1=2.5,
                   paper_drawdown=50., clip_usd=25., min_epoch_days=30, l_null_file_hash=latency,
                   exec_bound_evidence_id='fixture-bound', host_guard_manifest_hash=guard,
                   stopping_rule='single-look-at-n-plan-and-min-days', n_plan=240,
                   alpha_scale=.1, block_days=5, sigma=3., day_method='t_block',
                   tool_contract_hash=tool_hash())
    return nominee, refs


def open_epoch(conn, lim, clock, root, *, k=1, **kwargs):
    nominee, refs = build(conn, root, k=k, now=clock())
    from .launcher_fixture import LauncherFixture
    launcher = LauncherFixture(root, clock)
    try:
        return R._open_evaluated(conn, lim, clock=clock, hypothesis_id='H1', by='crew', reason='fixture freeze',
                                 nominee=nominee, nominee_refs=refs, config_root=root,
                                 launcher_policy=launcher.policy, probe_write=launcher.probe, **kwargs)
    finally:
        launcher.close()
