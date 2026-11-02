"""Office v2 transition (spec 2026-10-30-office-v2-persistent-seats-design.md section 8): eval-1 -> office-v2-proof-1.
Stage 1 closes the eval-1 row as STOPPED_BY_OPERATOR at the recorded stop time (its own transaction, org_changes receipt).
Stage 2 opens the proof epoch with one Head actor (no book, no order capability) and seeds the v2 roster. Stage 2 never starts
while eval-1 is open; each stage is idempotent, so a rerun after a crash between them completes stage 2.
No model calls, no service control, no funded DB, no ledger. Rollback is not a reversal: a sealed epoch never reopens (spec section 11)."""
from datetime import datetime, timezone
import hashlib
import json

from . import epoch_transition as E, seat_events as SE, store as S

PRED = 'office-paper-eval-1'
NEW = 'office-v2-proof-1'
STATUS = 'STOPPED_BY_OPERATOR'
STOP_AT_ISO = '2026-10-30T09:48:08Z'                  # operator order, Gamma Amendment 17
STOP_AT = datetime.strptime(STOP_AT_ISO, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
ROLE, FAMILY, MODEL = 'head', 'claude', 'claude-opus-5-5'
PROOF_ACTOR_ID = 'ag_' + hashlib.sha256((NEW + ':head').encode()).hexdigest()[:16]    # unique to the proof; not a v1 agent id
STOP_KIND, OPEN_KIND = 'office_eval1_stopped_by_operator', 'office_v2_epoch_open'


def _hash(doc): return hashlib.sha256(S.canonical_json(doc)).hexdigest()


def _actor(model): return {'actor_id': PROOF_ACTOR_ID, 'role': ROLE, 'family': FAMILY, 'model': model}


def _approval(model): return _hash({'epoch': NEW, 'predecessor': PRED, 'stop_at': STOP_AT, 'actor': _actor(model)})


def _epoch(conn, epoch_id):
    row = conn.execute('SELECT payload_json FROM epochs WHERE id=?', (epoch_id,)).fetchone()
    return None if row is None else json.loads(row[0])


def _event(conn, receipt_id):
    row = conn.execute('SELECT payload_json,prev_hash FROM org_changes WHERE row_hash=?', (receipt_id,)).fetchone()
    if row is None or hashlib.sha256(S.canonical_json(json.loads(row[0])) + row[1].encode()).hexdigest() != receipt_id:
        raise ValueError('v2_transition_corrupt')
    return json.loads(row[0])


def _roster(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='office_v2_roster'").fetchone(): return None
    return conn.execute('SELECT epoch_id,role,generation,incarnation,sealed FROM office_v2_roster WHERE actor_id=?', (PROOF_ACTOR_ID,)).fetchone()


def _check(conn, now, model):
    """Read-only. Returns the state and which stages remain; raises a typed v2_/epoch_ reason on any doubt."""
    if not isinstance(model, str) or not model.strip(): raise ValueError('v2_model_invalid')
    if not E.O._number(now): raise ValueError('v2_clock_unknown')
    if conn.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()[0] != 'office': raise ValueError('v2_office_store_required')
    if len(conn.execute('SELECT seq FROM org_changes LIMIT 5001').fetchall()) > 5000 or not S.verify_chain(conn, 'org_changes')[0]:
        raise ValueError('v2_org_chain_unknown')
    control = E._control(conn)
    if control is None: raise ValueError('v2_control_unknown')
    approval = _approval(model)
    if control['active_epoch'] == NEW:
        frozen = _event(conn, control['receipt_id'])
        if frozen.get('kind') != OPEN_KIND or frozen['approval_sha256'] != approval: raise ValueError('v2_approval_changed')
        if _epoch(conn, NEW) != frozen['new_epoch'] or _roster(conn) is None or tuple(_roster(conn)[:4]) != (NEW, ROLE, 1, 1):
            raise ValueError('v2_record_changed')
        return {'state': 'already_applied', 'stop': False, 'open': False, 'approval_sha256': approval}
    if control['active_epoch'] != PRED: raise ValueError('v2_eval1_not_active')
    E._lease(conn, now)                                  # the v1 supervisor must be off
    old = _epoch(conn, PRED)
    if old is None or old.get('kind') != 'paper_eval': raise ValueError('v2_eval1_missing')
    if old.get('first_order_at') is not None: raise ValueError('v2_eval1_has_orders')
    stop = old.get('closed_at') is None
    if stop:
        if not STOP_AT <= now: raise ValueError('v2_stop_in_future')
        if not (E.O._number(old.get('opened_at')) and old['opened_at'] <= STOP_AT): raise ValueError('v2_stop_before_open')
    else:
        receipt = old.get('stop_receipt_id')
        if (old['closed_at'] != STOP_AT or old.get('status') != STATUS or not isinstance(receipt, str)
                or _event(conn, receipt).get('kind') != STOP_KIND): raise ValueError('v2_eval1_closed_unexpectedly')
    if _epoch(conn, NEW) is not None: raise ValueError('v2_epoch_exists')
    seated = _roster(conn)
    if seated is not None and tuple(seated[:4]) != (NEW, ROLE, 1, 1): raise ValueError('v2_roster_conflict')
    return {'state': 'pending', 'stop': stop, 'open': True, 'approval_sha256': approval}


def _close_eval1(conn, now, model):
    old = _epoch(conn, PRED)
    receipt = S.append(conn, 'org_changes', {'kind': STOP_KIND, 'by': 'operator', 'recorded_by': 'crew', 'at': now, 'epoch_id': PRED,
                                              'stopped_at': STOP_AT, 'stopped_at_iso': STOP_AT_ISO, 'status': STATUS, 'prior_epoch': old})
    closed = dict(old, closed_at=STOP_AT, status=STATUS, stop_receipt_id=receipt)
    conn.execute('UPDATE epochs SET payload_json=? WHERE id=?', (S.canonical_json(closed).decode(), PRED))


def _open_v2(conn, now, model, approval):
    control = E._control(conn)
    old = _epoch(conn, PRED)
    SE.ensure_schema(conn)
    conn.execute('INSERT OR IGNORE INTO office_v2_roster(actor_id,epoch_id,role,generation,incarnation,sealed,updated_at) VALUES(?,?,?,?,?,0,?)',
                 (PROOF_ACTOR_ID, NEW, ROLE, 1, 1, now))
    epoch = dict(epoch_id=NEW, opened_at=now, closed_at=None, first_order_at=None, kind='v2_proof', pilot=True,
                 evaluation_allowed=False, order_capable=False, book_id=None, ledger_epoch_id=None, lessons_live=False,
                 predecessor=PRED, predecessor_stop_receipt_id=old['stop_receipt_id'], transition_id=approval, actor=_actor(model))
    receipt = S.append(conn, 'org_changes', {'kind': OPEN_KIND, 'by': 'crew', 'at': now, 'epoch_id': NEW, 'approval_sha256': approval,
                                              'new_epoch': epoch, 'prior_control': control})
    conn.execute('INSERT INTO epochs VALUES(?,?)', (NEW, S.canonical_json(epoch).decode()))
    history = list(control.get('history', [])) + [control['receipt_id']]
    conn.execute('UPDATE office_epoch_control SET payload_json=? WHERE id=1',
                 (S.canonical_json({'active_epoch': NEW, 'receipt_id': receipt, 'history': history}).decode(),))
    return receipt


def transition(conn, *, now, model=MODEL, apply=False):
    plan = _check(conn, now, model)
    if plan['state'] == 'already_applied': return plan
    if not apply: return plan | {'state': 'dry_run'}
    if plan['stop']:
        with S.write_transaction(conn):
            if not _check(conn, now, model)['stop']: raise ValueError('v2_eval1_changed')
            _close_eval1(conn, now, model)
    with S.write_transaction(conn):                      # a separate commit: eval-1 is durably closed before any v2 write
        again = _check(conn, now, model)
        if again['state'] == 'already_applied': return again
        if again['stop']: raise ValueError('v2_eval1_not_closed')
        receipt = _open_v2(conn, now, model, plan['approval_sha256'])
    return {'state': 'transitioned', 'receipt_id': receipt, 'approval_sha256': plan['approval_sha256']}
