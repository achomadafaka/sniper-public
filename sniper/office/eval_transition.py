"""Crew-only office-paper-eval-1 open (Beta design a21119cdd, Gamma prereg 5273d4f7c): discovery-2 -> eval-1.
Office stage: seal discovery-2, rebind the 4 actors (generation+1, new prompts), hire the Postmortem seat, open the
epoch with its frozen record. Ledger stage (separate DB, idempotent): the evaluated paper book and its one initial fund.
No model calls, no service control, no funded DB."""
from copy import deepcopy
import hashlib
import json

from . import epoch_transition as E, ledger as L, memory_store as MS, store as S

PRED = E.NEW                                   # office-pilot-discovery-2
NEW = 'office-paper-eval-1'
BOOK = NEW
CAPITAL_USD, CLIP_USD, MAX_CONCURRENT, DAILY_LOSS_USD = 1000., 25., 3, 100.
KILL = {'peak_drawdown_usd': 200., 'unknown_max_s': 86400.}
# Delta's admission reads this exact note JSON (no constant fallback in the library).
# clock_skew_tolerance_s: >= observed offset envelope max 0.0919 s (Delta, 1075 samples; not paired dispatch jitter),
# leg_stale_demote_s: configured, unmeasured (prereg Am7/Am8 9cfc43e79).
LEDGER_NOTE = {'paper_eval': {'version': NEW, 'drawdown_usd': 200., 'unknown_max_s': 86400., 'mark_max_age_s': 60.,
                              'clock_skew_tolerance_s': 0.125, 'leg_stale_demote_s': 1800.}}
LEARNING_FILES = ('sniper/office/lessons.py', 'sniper/office/call_grader.py', 'sniper/office/memory.py', 'sniper/office/memory_store.py')
LEARNING_TOOLS = {'grade.read': 'office-grade-read-v1', 'lesson.propose': 'office-lesson-propose-v1'}
POSTMORTEM = {'role': 'Postmortem', 'family': 'claude', 'provider': 'claude', 'model': 'claude-sonnet-5-5'}
POSTMORTEM_ID = 'ag_' + hashlib.sha256((NEW + ':postmortem').encode()).hexdigest()[:16]
_FIELDS = {'epoch', 'predecessor', 'prompt_hashes', 'tool_contract_hash', 'prereg_sha256', 'book_policy', 'lessons_live',
           'operator_waived_gates', 'book', 'kill', 'postmortem', 'roster', 'learning_rule', 'near_pass_cohort',
           'cost_rule_sha256', 'approvals'}
_IGNORED = E._IGNORED | {'memory_snapshots'}


def _hash(doc): return hashlib.sha256(S.canonical_json(doc)).hexdigest()


def _control(conn):
    control = E._control(conn)
    if control is None: raise ValueError('eval_predecessor_unknown')
    return control


def _actors(conn):
    rows = conn.execute('SELECT id,payload_json FROM agents LIMIT 6').fetchall()
    actors = {r[0]: json.loads(r[1]) for r in rows}
    if any(a.get('state') != 'active' or type(a.get('generation')) is not int or a['generation'] < 1 for a in actors.values()):
        raise ValueError('eval_roster_unknown')
    return actors


def _seat(manifest, role, family):
    seats = [s for s in manifest['seats'] if s['role'] == role and s['family'] == family]
    if len(seats) != 1: raise ValueError('eval_manifest_seat')
    return seats[0]


def _book_terms():
    return {'book_id': BOOK, 'kind': 'evaluated', 'capital_usd': CAPITAL_USD, 'clip_usd': CLIP_USD,
            'max_concurrent': MAX_CONCURRENT, 'daily_loss_usd': DAILY_LOSS_USD}


def _roster(actors, manifest):
    out = {aid: {'generation': a['generation'] + 1, **{k: a[k] for k in ('role', 'model', 'provider', 'family')}} for aid, a in actors.items()}
    out[POSTMORTEM_ID] = {'generation': 1, **{k: POSTMORTEM[k] for k in ('role', 'model', 'provider', 'family')}}
    return out


PREREG = 'docs/audits/2026-10-29-office-paper-eval-1-preregistration.md'
COST_RULE = 'config/office/cost_allocation_rule.json'
FILL_MODEL = 'office-ps-paper-v1'
CORE_COLLECTED = ['chart', 'holders']


def _file_sha(rel):
    from pathlib import Path
    return hashlib.sha256((Path(__file__).resolve().parents[2] / rel).read_bytes()).hexdigest()


def book_policy_identity(policy):
    """The frozen book policy (all rails) and its fill model (prereg sec 6)."""
    if not isinstance(policy, dict) or policy.get('fill_model_version') != FILL_MODEL: raise ValueError('eval_book_policy_invalid')
    return {'sha256': _hash(policy), 'fill_model_version': FILL_MODEL}


def cohort_identity(desk_conn, cohort_id):
    """Read from the provisioned desk cohort row; refuses a four-list cohort or any other require_collected."""
    from sniper.persistence import db_memecoin_desk as D
    row = desk_conn.execute('SELECT producer,policy_json FROM desk_cohorts WHERE cohort_id=?', (cohort_id,)).fetchone()
    if row is None: raise ValueError('eval_cohort_unknown')
    if row[0] != 'U': raise ValueError('eval_cohort_producer')
    policy = json.loads(row[1])
    if ((policy.get('hard_filter') or {}).get('intake') or {}).get('require_collected') != CORE_COLLECTED:
        raise ValueError('eval_cohort_not_core_collected')
    # the desk's own hashes (what the provisioner and intake proofs print), not an Office re-encoding (Gamma)
    return {'id': cohort_id, 'policy_hash': D._hash(D._json(policy)), 'hard_policy_hash': D._hash(D._json(policy['hard_filter'])),
            'require_collected': list(CORE_COLLECTED)}


def learning_rule():
    """Gamma: the frozen learning rule = lessons + grader + memory core files and the two tool versions."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    files = {f: hashlib.sha256((root / f).read_bytes()).hexdigest() for f in LEARNING_FILES}
    body = {'id': 'office-learning-rule-1', 'contract': 'office-call-grade-v1', 'files': files, 'tools': dict(LEARNING_TOOLS)}
    return dict(body, sha256=_hash(body))


def frozen_manifest(conn):
    """The manifest frozen at open, once eval-1 is active (retries and rollback reuse it), else None."""
    control = E._control(conn)
    if control is None or control.get('active_epoch') != NEW: return None
    return _frozen_event(conn, control['receipt_id'])['manifest']


def build_manifest(conn, *, prompts, models, policy_hash):
    """Eval-1 seats from the live discovery-2 actors (same identities) plus the Postmortem seat; prompts/models pinned."""
    from .runtime_steps import slot_of
    seats = []
    for aid, a in sorted(_actors(conn).items()):
        slot = slot_of(a)
        seats.append(dict(slot=slot, role=a['role'], pod=a.get('pod'), family=a['family'], provider=a['provider'], model=a['model'],
                          prompt_hash=prompts[slot]['sha256'], policy_hash=a['policy_hash'], lane=a.get('lane'), lens=a.get('lens'),
                          persona=a['persona']))
    pm = models['seats']['postmortem']
    seats.append(dict(slot='postmortem', role='Postmortem', pod=None, family='claude', provider=pm['provider'], model=pm['model'],
                      prompt_hash=prompts['postmortem']['sha256'], policy_hash=policy_hash, lane=None, lens=None,
                      persona=dict(name='claude Postmortem', personality='Careful and direct.', backstory='Grades closed paper calls.')))
    return {'schema': 'office-paper-eval-1-manifest-v1', 'epoch_id': NEW, 'seats': sorted(seats, key=lambda s: s['slot'])}


def approval_draft(conn, manifest, *, book_policy, near_pass_cohort):
    """Read-only template; approvals and the cohort/learning identities are attested by the crew."""
    actors = _actors(conn)
    pm = _seat(manifest, 'Postmortem', 'claude')
    return dict(epoch={'id': NEW, 'mode': 'paper_eval'}, predecessor={'id': PRED},
                prompt_hashes={'old': {aid: a['prompt_hash'] for aid, a in actors.items()},
                               'new': {aid: _seat(manifest, a['role'], a['family'])['prompt_hash'] for aid, a in actors.items()}},
                tool_contract_hash=E.tool_hash(), prereg_sha256=_file_sha(PREREG), book_policy=book_policy_identity(book_policy),
                lessons_live=True, operator_waived_gates=True,
                book=_book_terms(), kill=dict(KILL), postmortem={**POSTMORTEM, 'agent_id': POSTMORTEM_ID, 'prompt_hash': pm['prompt_hash']},
                roster=_roster(actors, manifest),
                learning_rule=learning_rule(), near_pass_cohort=dict(near_pass_cohort),
                cost_rule_sha256=_file_sha(COST_RULE), approvals={'alpha': None, 'gamma': None})


def _validate(record, manifest, actors, *, book_policy, near_pass_cohort):
    try:
        expected = approval_draft_from(actors, manifest)
        if set(record) != _FIELDS: raise ValueError()
        for key in ('epoch', 'predecessor', 'prompt_hashes', 'book', 'kill', 'postmortem', 'roster'):
            if record[key] != expected[key]: raise ValueError(key)
        if record['tool_contract_hash'] != E.tool_hash() or record['lessons_live'] is not True or record['operator_waived_gates'] is not True:
            raise ValueError()
        if record['prereg_sha256'] != _file_sha(PREREG) or record['cost_rule_sha256'] != _file_sha(COST_RULE): raise ValueError('files')
        if record['book_policy'] != book_policy_identity(book_policy): raise ValueError('book_policy')
        if record['learning_rule'] != learning_rule(): raise ValueError('learning_rule')
        co = near_pass_cohort        # computed by the caller from the provisioned desk row (cohort_identity)
        if (record['near_pass_cohort'] != co or co.get('require_collected') != CORE_COLLECTED
                or not isinstance(co.get('id'), str) or not co['id'] or not E._hex(co.get('policy_hash'), 64)
                or not E._hex(co.get('hard_policy_hash'), 64)): raise ValueError('cohort')
        if any(not isinstance(v, str) or not v.strip() for v in record['approvals'].values()) or set(record['approvals']) != {'alpha', 'gamma'}:
            raise ValueError()
        if len(manifest['seats']) != 5 or manifest.get('epoch_id') != NEW: raise ValueError()
        pm = _seat(manifest, 'Postmortem', 'claude')
        if any(pm[k] != POSTMORTEM[k] for k in ('provider', 'model')): raise ValueError()
        for aid, a in actors.items():
            seat = _seat(manifest, a['role'], a['family'])
            if any(seat[k] != a[k] for k in ('provider', 'model', 'policy_hash')) or not E._hex(seat['prompt_hash'], 64): raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError('eval_approval_binding_invalid') from None


def approval_draft_from(actors, manifest):
    pm = _seat(manifest, 'Postmortem', 'claude')
    return dict(epoch={'id': NEW, 'mode': 'paper_eval'}, predecessor={'id': PRED},
                prompt_hashes={'old': {aid: a['prompt_hash'] for aid, a in actors.items()},
                               'new': {aid: _seat(manifest, a['role'], a['family'])['prompt_hash'] for aid, a in actors.items()}},
                book=_book_terms(), kill=dict(KILL), postmortem={**POSTMORTEM, 'agent_id': POSTMORTEM_ID, 'prompt_hash': pm['prompt_hash']},
                roster=_roster(actors, manifest))


def transition(conn, *, manifest, record, now, book_policy, near_pass_cohort, apply=False, rollback=False):
    if not E.O._number(now): raise ValueError('eval_clock_unknown')
    approval = _hash({'manifest': manifest, 'record': record})
    if apply:
        with S.write_transaction(conn): return _transition(conn, manifest, record, now, approval, rollback, True, book_policy, near_pass_cohort)
    owned = not conn.in_transaction
    if owned: conn.execute('BEGIN')
    try: return _transition(conn, manifest, record, now, approval, rollback, False, book_policy, near_pass_cohort)
    finally:
        if owned: conn.rollback()


def _frozen_event(conn, receipt_id):
    row = conn.execute('SELECT payload_json,prev_hash FROM org_changes WHERE row_hash=?', (receipt_id,)).fetchone()
    if row is None or hashlib.sha256(S.canonical_json(json.loads(row[0])) + row[1].encode()).hexdigest() != receipt_id:
        raise ValueError('eval_transition_corrupt')
    return json.loads(row[0])


def _transition(conn, manifest, record, now, approval, rollback, apply, book_policy, near_pass_cohort):
    if conn.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()[0] != 'office': raise ValueError('eval_office_store_required')
    if len(conn.execute('SELECT seq FROM org_changes LIMIT 5001').fetchall()) > 5000 or not S.verify_chain(conn, 'org_changes')[0]:
        raise ValueError('eval_org_chain_unknown')
    E._lease(conn, now)
    control = _control(conn)
    if control['active_epoch'] == NEW:
        frozen = _frozen_event(conn, control['receipt_id'])
        if frozen['approval_sha256'] != approval: raise ValueError('eval_approval_changed')
        return _rollback(conn, frozen, control, now, apply) if rollback else {'state': 'already_applied'}
    if rollback: return {'state': 'already_rolled_back'} if control['active_epoch'] == PRED and control.get('history') else _missing()
    if control['active_epoch'] != PRED: raise ValueError('eval_predecessor_not_active')
    actors = _actors(conn)
    if len(actors) != 4: raise ValueError('eval_roster_unknown')
    _validate(record, manifest, actors, book_policy=book_policy, near_pass_cohort=near_pass_cohort)
    old_row = conn.execute('SELECT payload_json FROM epochs WHERE id=?', (PRED,)).fetchone()
    old = None if old_row is None else json.loads(old_row[0])
    if old is None or old.get('closed_at') is not None or old.get('kind') != 'discovery': raise ValueError('eval_predecessor_not_open_discovery')
    digest = _hash(manifest)
    new_actors = {aid: dict(a, prompt_hash=record['prompt_hashes']['new'][aid], generation=a['generation'] + 1,
                            seed_manifest_sha256=digest, prompt_update_epoch=NEW) for aid, a in actors.items()}
    pm = _seat(manifest, 'Postmortem', 'claude')
    new_actors[POSTMORTEM_ID] = dict(agent_id=POSTMORTEM_ID, role='Postmortem', pod=pm.get('pod'), family='claude', provider='claude',
        model=POSTMORTEM['model'], prompt_hash=pm['prompt_hash'], policy_hash=pm['policy_hash'], lane=None, lens=None,
        persona=pm['persona'], persona_hash=_hash(pm['persona']), state='active', generation=1, hired_epoch=NEW, archived_epoch=None,
        created_at=now, idempotency_key=NEW + ':postmortem', seed_manifest_sha256=digest)
    snapshot_id = MS.seal(MS.OfficeMemory(conn), epoch_id=NEW, cut=now) if apply else None
    epoch = dict(epoch_id=NEW, opened_at=now, closed_at=None, first_order_at=None, kind='paper_eval', pilot=True,
                 evaluation_allowed=True, lessons_live=True, operator_waived_gates=True, book_id=BOOK, ledger_epoch_id=NEW,
                 seed_manifest_sha256=digest, transition_id=approval, memory_snapshot_id=snapshot_id, record=deepcopy(record))
    if not apply: return {'state': 'dry_run', 'approval_sha256': approval}
    event = dict(kind='paper_eval_epoch_open', by='crew', at=now, epoch_id=NEW, approval_sha256=approval, old_epoch=old,
                 new_epoch=epoch, old_actors=actors, new_actors=new_actors, manifest=manifest, approved_record=deepcopy(record),
                 prior_control=control, baseline_sha256=E._fingerprint(conn, ignored=_IGNORED),
                 post_org_seq=conn.execute('SELECT COALESCE(MAX(seq),0)+1 FROM org_changes').fetchone()[0])
    receipt = S.append(conn, 'org_changes', event)
    conn.execute('UPDATE epochs SET payload_json=? WHERE id=?', (S.canonical_json(dict(old, closed_at=now)).decode(), PRED))
    for aid, a in new_actors.items():
        if aid in actors: conn.execute('UPDATE agents SET payload_json=? WHERE id=?', (S.canonical_json(a).decode(), aid))
        else: conn.execute('INSERT INTO agents VALUES(?,?)', (aid, S.canonical_json(a).decode()))
    conn.execute('INSERT INTO epochs VALUES(?,?)', (NEW, S.canonical_json(epoch).decode()))
    history = list(control.get('history', [])) + [control['receipt_id']]
    conn.execute('UPDATE office_epoch_control SET payload_json=? WHERE id=1',
                 (S.canonical_json({'active_epoch': NEW, 'receipt_id': receipt, 'history': history}).decode(),))
    return {'state': 'transitioned', 'receipt_id': receipt, 'memory_snapshot_id': snapshot_id}


def _missing(): raise ValueError('eval_transition_missing')


def _rollback(conn, frozen, control, now, apply):
    epoch = conn.execute('SELECT payload_json FROM epochs WHERE id=?', (NEW,)).fetchone()
    if (epoch is None or json.loads(epoch[0]) != frozen['new_epoch'] or _actors(conn) != frozen['new_actors']
            or conn.execute('SELECT max(seq) FROM org_changes').fetchone()[0] != frozen['post_org_seq']
            or E._fingerprint(conn, ignored=_IGNORED) != frozen['baseline_sha256']):
        raise ValueError('eval_new_epoch_has_written_or_changed')
    if not apply: return {'state': 'dry_run_rollback'}
    S.append(conn, 'org_changes', {'kind': 'paper_eval_epoch_rollback', 'by': 'crew', 'at': now, 'epoch_id': PRED,
                                   'transition_receipt_id': control['receipt_id']})
    conn.execute('DELETE FROM epochs WHERE id=?', (NEW,))
    conn.execute('UPDATE epochs SET payload_json=? WHERE id=?', (S.canonical_json(frozen['old_epoch']).decode(), PRED))
    conn.execute('DELETE FROM agents WHERE id=?', (POSTMORTEM_ID,))
    for aid, a in frozen['old_actors'].items():
        conn.execute('UPDATE agents SET payload_json=? WHERE id=?', (S.canonical_json(a).decode(), aid))
    prior = dict(frozen['prior_control'], history=list(frozen['prior_control'].get('history', [])) + [control['receipt_id']])
    conn.execute('UPDATE office_epoch_control SET payload_json=? WHERE id=1', (S.canonical_json(prior).decode(),))
    return {'state': 'rolled_back'}


def adopted(conn, manifest):
    """Startup recognition of an applied eval-1 open; anything changed since is refused, never reseeded."""
    control = _control(conn)
    if control['active_epoch'] != NEW: return None
    frozen = _frozen_event(conn, control['receipt_id'])
    if _actors(conn) != frozen['new_actors']: raise ValueError('eval_roster_changed')
    if manifest != frozen['manifest']: raise ValueError('eval_prompt_manifest_changed')
    if frozen['new_epoch']['record']['tool_contract_hash'] != E.tool_hash(): raise ValueError('eval_tool_contract_changed')
    row = conn.execute('SELECT payload_json FROM epochs WHERE id=?', (NEW,)).fetchone()
    epoch = None if row is None else json.loads(row[0])
    if epoch is None or {k: v for k, v in epoch.items() if k != 'first_order_at'} != {k: v for k, v in frozen['new_epoch'].items() if k != 'first_order_at'}:
        raise ValueError('eval_record_changed')
    actors = frozen['new_actors']
    ids = {s['slot']: next(aid for aid, a in actors.items() if a['role'] == s['role'] and a['family'] == s['family']) for s in manifest['seats']}
    return {'epoch_id': NEW, 'book_id': BOOK, 'agent_ids': ids, 'seed_manifest_sha256': frozen['new_epoch']['seed_manifest_sha256']}


def adopted_runtime(conn, *, prompts, models, book_policy):
    """Runtime startup: the pinned prompts and models must equal the frozen eval-1 seats, slot by slot."""
    control = E._control(conn)
    if control is None or control['active_epoch'] != NEW: return None
    frozen = _frozen_event(conn, control['receipt_id'])
    manifest = frozen['manifest']
    if frozen['approved_record']['book_policy'] != book_policy_identity(book_policy): raise ValueError('eval_book_policy_changed')
    for seat in manifest['seats']:
        p, m = prompts.get(seat['slot']), models['seats'].get(seat['slot'])
        if (p is None or m is None or p.get('sha256') != seat['prompt_hash']
                or {'provider': m.get('provider'), 'model': m.get('model')} != {'provider': seat['provider'], 'model': seat['model']}):
            raise ValueError('eval_prompt_manifest_changed')
    if set(prompts) != {s['slot'] for s in manifest['seats']}: raise ValueError('eval_prompt_manifest_changed')
    return adopted(conn, manifest)


def mark_first_order(conn, *, at):
    """first_order_at is written once (the 14-day look clock starts at the first order, prereg sec 4)."""
    with S.write_transaction(conn):
        row = conn.execute('SELECT payload_json FROM epochs WHERE id=?', (NEW,)).fetchone()
        if row is None: raise ValueError('eval_epoch_missing')
        epoch = json.loads(row[0])
        if epoch.get('first_order_at') is not None: return epoch['first_order_at']
        conn.execute('UPDATE epochs SET payload_json=? WHERE id=?', (S.canonical_json(dict(epoch, first_order_at=at)).decode(), NEW))
        return at


def provision_ledger(ledger, *, now):
    """Idempotent by ids; never a second initial fund. A half-provisioned book stays unbound (book_bound)."""
    if not ledger.execute('SELECT 1 FROM ledger_epochs WHERE epoch_id=?', (NEW,)).fetchone():
        from .eval_orders import PODS_RULE                     # Delta: single-book rule frozen with the epoch
        L.open_epoch(ledger, epoch_id=NEW, clip_usd=CLIP_USD, rule_version=PODS_RULE['version'], actor='crew',
                     note=S.canonical_json(LEDGER_NOTE).decode(), now=now, pods_rule=PODS_RULE)
    if not ledger.execute('SELECT 1 FROM books WHERE book_id=?', (BOOK,)).fetchone():
        L.open_book(ledger, book_id=BOOK, epoch_id=NEW, kind='evaluated', actor='crew', note='single Office book', now=now)
    if not ledger.execute("SELECT 1 FROM capital_flows WHERE book_id=? AND kind='initial'", (BOOK,)).fetchone():
        L.fund(ledger, book_id=BOOK, usd=CAPITAL_USD, kind='initial', actor='crew', note='office-paper-eval-1 initial paper', now=now)
    return book_bound(ledger)


def book_bound(ledger):
    """None when the evaluated book exists in its own epoch with exactly one initial flow of CAPITAL_USD; else a typed reason."""
    epoch = ledger.execute('SELECT clip_usd FROM ledger_epochs WHERE epoch_id=?', (NEW,)).fetchone()
    book = ledger.execute('SELECT epoch_id,kind FROM books WHERE book_id=?', (BOOK,)).fetchone()
    flows = ledger.execute('SELECT kind,usd FROM capital_flows WHERE book_id=? LIMIT 3', (BOOK,)).fetchall()
    if epoch is None or book is None or not flows: return 'eval_book_unbound'
    note = ledger.execute('SELECT note FROM ledger_epochs WHERE epoch_id=?', (NEW,)).fetchone()[0]
    if note != S.canonical_json(LEDGER_NOTE).decode(): return 'eval_book_mismatch'
    from .eval_orders import PODS_RULE
    rule = ledger.execute('SELECT rule_json FROM epoch_pods_rules WHERE epoch_id=?', (NEW,)).fetchone()
    if rule is None or json.loads(rule[0]) != PODS_RULE: return 'eval_book_mismatch'
    if (epoch[0] != CLIP_USD or tuple(book) != (NEW, 'evaluated')
            or [tuple(f) for f in flows if f[0] == 'initial'] != [('initial', CAPITAL_USD)]):
        return 'eval_book_mismatch'
    return None
