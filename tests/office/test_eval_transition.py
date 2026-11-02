"""office-paper-eval-1 open on synthetic Office/ledger DBs (discovery-2 -> eval-1); never runs against live state."""
import copy
import hashlib
import json

import pytest

from sniper.office import eval_transition as X, ledger as L, store as S
from sniper.office.supervisor import Lease
from .test_epoch_transition import prepared, run as run_discovery  # noqa: F401  (fixture reuse)
from .test_pilot_seed import env  # noqa: F401


def _manifest(old):
    m = copy.deepcopy(old)
    for seat in m['seats']:
        seat['prompt_hash'] = hashlib.sha256(('eval1-' + seat['slot']).encode()).hexdigest()
    m['seats'].append(dict(slot='postmortem', role='Postmortem', pod=None, family='claude', provider='claude',
                           model='claude-sonnet-5-5', prompt_hash=hashlib.sha256(b'eval1-postmortem').hexdigest(),
                           policy_hash=m['seats'][0]['policy_hash'], lane=None, lens=None,
                           persona=dict(name='claude Postmortem', personality='Careful and direct.', backstory='Grades closed calls.')))
    m['epoch_id'] = X.NEW
    return m


BP = json.loads(open('config/office/book_policy_pilot.json').read())['policy']
CO = {'id': 'fixture-cohort', 'policy_hash': 'e' * 64, 'hard_policy_hash': 'd' * 64, 'require_collected': ['chart', 'holders']}


@pytest.fixture
def disc2(prepared, monkeypatch):
    monkeypatch.setattr(X, 'PREREG', 'docs/superpowers/specs/2026-10-29-office-paper-eval-1-epoch.md')
    conn, ledger, manifest, record, _ = prepared
    run_discovery(prepared, apply=True)
    m = _manifest(manifest)
    rec = X.approval_draft(conn, m, book_policy=BP, near_pass_cohort=CO)
    rec['approvals'] = {'alpha': 'fixture', 'gamma': 'fixture'}
    yield conn, ledger, m, rec


def go(disc2, **kw):
    conn, ledger, m, rec = disc2
    kw.setdefault('book_policy', BP); kw.setdefault('near_pass_cohort', CO)
    return X.transition(conn, manifest=m, record=rec, now=1200., **kw)


def test_dry_run_is_nonmutating_then_apply_hires_postmortem_and_rebinds_four(disc2):
    conn, ledger, m, rec = disc2
    before = {r[0]: json.loads(r[1]) for r in conn.execute('SELECT id,payload_json FROM agents')}
    changes = conn.total_changes
    assert go(disc2)['state'] == 'dry_run' and conn.total_changes == changes
    assert go(disc2, apply=True)['state'] == 'transitioned'
    assert go(disc2, apply=True)['state'] == 'already_applied'
    after = {r[0]: json.loads(r[1]) for r in conn.execute('SELECT id,payload_json FROM agents')}
    assert len(after) == 5 and after[X.POSTMORTEM_ID]['role'] == 'Postmortem' and after[X.POSTMORTEM_ID]['generation'] == 1
    for aid, a in before.items():
        assert after[aid]['generation'] == a['generation'] + 1 and after[aid]['prompt_hash'] == rec['prompt_hashes']['new'][aid]
    epoch = json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?', (X.NEW,)).fetchone()[0])
    assert epoch['lessons_live'] is True and epoch['evaluation_allowed'] is True and epoch['operator_waived_gates'] is True
    assert epoch['book_id'] == X.BOOK and epoch['memory_snapshot_id']
    pred = json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?', (X.PRED,)).fetchone()[0])
    assert pred['closed_at'] == 1200.
    control = json.loads(conn.execute('SELECT payload_json FROM office_epoch_control').fetchone()[0])
    assert control['active_epoch'] == X.NEW and len(control['history']) == 1
    assert S.verify_chain(conn, 'org_changes')[0]


@pytest.mark.parametrize('bad', ['lease', 'prompt', 'tool', 'lessons', 'capital', 'postmortem_model', 'not_disc2'])
def test_bad_binding_refuses_without_partial_change(disc2, bad):
    conn, ledger, m, rec = disc2
    if bad == 'lease':
        Lease(conn, clock=lambda: 1200., ttl_s=20).acquire('live')
    elif bad == 'prompt':
        rec['prompt_hashes']['new'][next(iter(rec['prompt_hashes']['new']))] = '0' * 64
    elif bad == 'tool':
        rec['tool_contract_hash'] = '0' * 64
    elif bad == 'lessons':
        rec['lessons_live'] = 'true'
    elif bad == 'capital':
        rec['book']['capital_usd'] = 2000.
    elif bad == 'postmortem_model':
        m['seats'][-1]['model'] = 'claude-opus-5-5'
    else:
        conn.execute("UPDATE office_epoch_control SET payload_json=json_set(payload_json,'$.active_epoch','office-pilot-discovery-1')")
        conn.commit()
    epochs = conn.execute('SELECT count(*) FROM epochs').fetchone()[0]
    with pytest.raises(ValueError):
        go(disc2, apply=True)
    assert conn.execute('SELECT count(*) FROM epochs').fetchone()[0] == epochs
    assert conn.execute('SELECT count(*) FROM agents').fetchone()[0] == 4


def test_rollback_only_while_untouched(disc2):
    conn, *_ = disc2
    before = {r[0]: r[1] for r in conn.execute('SELECT id,payload_json FROM agents')}
    go(disc2, apply=True)
    assert go(disc2, apply=True, rollback=True)['state'] == 'rolled_back'
    assert {r[0]: r[1] for r in conn.execute('SELECT id,payload_json FROM agents')} == before
    assert json.loads(conn.execute('SELECT payload_json FROM office_epoch_control').fetchone()[0])['active_epoch'] == X.PRED
    go(disc2, apply=True)
    conn.execute("INSERT INTO turn_runs VALUES('eval-work','{}')"); conn.commit()
    with pytest.raises(ValueError, match='written'):
        go(disc2, apply=True, rollback=True)


def test_ledger_stage_is_idempotent_and_never_refunds(disc2, tmp_path):
    ledger = L.open_ledger(tmp_path / 'eval-ledger.db')
    assert X.book_bound(ledger) == 'eval_book_unbound'
    X.provision_ledger(ledger, now=1300.)
    X.provision_ledger(ledger, now=1400.)
    assert X.book_bound(ledger) is None
    assert tuple(ledger.execute("SELECT count(*), sum(usd) FROM capital_flows WHERE book_id=?", (X.BOOK,)).fetchone()) == (1, 1000.)
    assert ledger.execute("SELECT kind FROM books WHERE book_id=?", (X.BOOK,)).fetchone()[0] == 'evaluated'
    assert ledger.execute("SELECT clip_usd FROM ledger_epochs WHERE epoch_id=?", (X.NEW,)).fetchone()[0] == 25.


def test_adopted_recognizes_eval1_and_refuses_a_changed_manifest(disc2):
    conn, ledger, m, rec = disc2
    go(disc2, apply=True)
    out = X.adopted(conn, m)
    assert out['epoch_id'] == X.NEW and out['agent_ids']['postmortem'] == X.POSTMORTEM_ID and set(out['agent_ids']) == {s['slot'] for s in m['seats']}
    changed = copy.deepcopy(m); changed['seats'][0]['prompt_hash'] = '1' * 64
    with pytest.raises(ValueError, match='manifest'):
        X.adopted(conn, changed)


def test_learning_rule_freezes_lessons_grader_and_memory_core_files():
    rule = X.learning_rule()
    assert rule['id'] == 'office-learning-rule-1' and rule['contract'] == 'office-call-grade-v1'
    assert set(rule['files']) == {'sniper/office/lessons.py', 'sniper/office/call_grader.py', 'sniper/office/memory.py', 'sniper/office/memory_store.py'}
    assert rule['tools'] == {'grade.read': 'office-grade-read-v1', 'lesson.propose': 'office-lesson-propose-v1'}
    assert len(rule['sha256']) == 64


def test_placeholder_cohort_refuses(disc2):
    conn, ledger, m, rec = disc2
    with pytest.raises(ValueError):
        go(disc2, apply=True, near_pass_cohort=dict(CO, policy_hash='0' * 64))     # record != the provisioned row
    with pytest.raises(ValueError):
        go(disc2, apply=True, book_policy=dict(BP, stop_loss_pct=1))                 # rails edited after approval
    rec['cost_rule_sha256'] = 'f' * 64
    with pytest.raises(ValueError):
        go(disc2, apply=True)


def test_runtime_adoption_requires_the_frozen_prompts_and_models_slot_by_slot(disc2):
    conn, ledger, m, rec = disc2
    prompts = {s['slot']: {'text': 'x', 'sha256': s['prompt_hash']} for s in m['seats']}
    models = {'seats': {s['slot']: {'provider': s['provider'], 'model': s['model']} for s in m['seats']}}
    assert X.adopted_runtime(conn, prompts=prompts, models=models, book_policy=BP) is None          # not opened yet
    go(disc2, apply=True)
    assert X.adopted_runtime(conn, prompts=prompts, models=models, book_policy=BP)['agent_ids']['postmortem'] == X.POSTMORTEM_ID
    bad = copy.deepcopy(models); bad['seats']['postmortem']['model'] = 'claude-opus-5-5'
    with pytest.raises(ValueError, match='manifest'):
        X.adopted_runtime(conn, prompts=prompts, models=bad, book_policy=BP)
    with pytest.raises(ValueError, match='manifest'):
        X.adopted_runtime(conn, prompts={k: v for k, v in prompts.items() if k != 'postmortem'}, models=models, book_policy=BP)


def test_model_manifest_accepts_the_five_seat_eval1_set_only(tmp_path):
    from sniper.office import pilot_models as PM
    seats = {'head': 'claude-opus-5-5', 'scout': 'claude-opus-5-5', 'judge_claude': 'claude-opus-5-5', 'judge_gpt': 'gpt-6-luna'}
    def load(extra):
        doc = {'version': 'office-pilot-models-v1', 'seats': {k: {'provider': PM.EVAL1_PROVIDERS[k], 'model': v} for k, v in {**seats, **extra}.items()}}
        raw = json.dumps(doc).encode(); path = tmp_path / 'm.json'; path.write_bytes(raw)
        return PM.load(path, hashlib.sha256(raw).hexdigest())
    assert set(load({})['seats']) == set(PM.PROVIDERS)
    assert set(load({'postmortem': 'claude-sonnet-5-5'})['seats']) == set(PM.EVAL1_PROVIDERS)
    with pytest.raises(ValueError):
        load({'postmortem': 'gpt-6-luna'})


def test_first_order_at_is_written_once_and_adoption_still_holds(disc2):
    conn, ledger, m, rec = disc2
    go(disc2, apply=True)
    assert X.mark_first_order(conn, at=1500.) == 1500. and X.mark_first_order(conn, at=1600.) == 1500.
    assert X.adopted(conn, m)['epoch_id'] == X.NEW


def test_ledger_stage_freezes_delta_single_book_rule(tmp_path):
    from sniper.office.eval_orders import PODS_RULE
    ledger = L.open_ledger(tmp_path / 'eval-ledger.db')
    X.provision_ledger(ledger, now=1300.)
    assert json.loads(ledger.execute("SELECT rule_json FROM epoch_pods_rules WHERE epoch_id=?", (X.NEW,)).fetchone()[0]) == PODS_RULE


def test_runtime_refuses_a_book_policy_edited_after_open(disc2):
    conn, ledger, m, rec = disc2
    go(disc2, apply=True)
    prompts = {s['slot']: {'text': 'x', 'sha256': s['prompt_hash']} for s in m['seats']}
    models = {'seats': {s['slot']: {'provider': s['provider'], 'model': s['model']} for s in m['seats']}}
    with pytest.raises(ValueError, match='book_policy'):
        X.adopted_runtime(conn, prompts=prompts, models=models, book_policy=dict(BP, max_hold_s=1))


def test_cohort_identity_reads_the_desk_row_and_refuses_a_four_list(tmp_path):
    import sqlite3
    desk = sqlite3.connect(tmp_path / 'desk.db')
    from sniper.persistence import db_memecoin_desk as D
    desk.execute('CREATE TABLE desk_cohorts(cohort_id TEXT PRIMARY KEY, producer TEXT, policy_json TEXT)')
    core = {'hard_filter': {'intake': {'require_collected': ['chart', 'holders']}}}
    four = {'hard_filter': {'intake': {'require_collected': ['chart', 'cluster', 'dump_group', 'holders']}}}
    desk.executemany('INSERT INTO desk_cohorts VALUES(?,?,?)', [('core', 'U', json.dumps(core)), ('four', 'U', json.dumps(four)),
                                                                 ('held', 'H', json.dumps(core))])
    assert X.cohort_identity(desk, 'core') == {'id': 'core', 'policy_hash': D._hash(D._json(core)),
                                               'hard_policy_hash': D._hash(D._json(core['hard_filter'])), 'require_collected': ['chart', 'holders']}
    for bad in ('four', 'missing', 'held'):
        with pytest.raises(ValueError):
            X.cohort_identity(desk, bad)
