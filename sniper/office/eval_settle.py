"""Host settlement of published office-paper-eval-1 Judge decisions into Delta's eval_orders (paper ledger only).
Runs after turns (runtime flush), never inside a model call. One settle receipt per decision_id; an unknown result is
retried until the decision's window closes, then settled as refused so nothing is retried forever."""
from types import SimpleNamespace

from . import eval_orders as EO, eval_transition as X, judge_decisions as J, store as S
from .supervisor import _get, action_key

SETTLE_WINDOW_S = 120.
# Delta's reversible entry pauses: retried inside the window, never settled as a lost ENTER.
REVERSIBLE = frozenset({'epoch_clock_regressed', 'epoch_equity_unknown', 'epoch_mark_stale', 'fx_unknown'})
STATUS = {'config_drift': 'BROKEN'}          # every other latch kind ends the epoch FAILED (Delta d89667265)
HOST_TRADER = 'office-host-trader'


def _memo(conn, row):
    """(memo, served_model) of the same Judge turn's committed memo, or None while it is not committed yet."""
    msg = _get(conn, 'messages', action_key(row['turn_decision_id'], 'memo.commit'))
    if msg is None or msg.get('actor_id') != row['actor_id']: return None
    req = msg.get('request') or {}
    return req.get('memo'), req.get('served_model')


def _ctx(did, published_at):
    return SimpleNamespace(role='trader', actor_id=HOST_TRADER, decision_id=did, book_id=X.BOOK, epoch_id=X.NEW,
                           deadline_at=published_at + SETTLE_WINDOW_S, cut={'knowledge_cut': published_at})


def _settle_exits(conn, ledger, pub, *, now):
    """Every asking Judge is settled (recorded) once; the ledger keeps the first exit request."""
    did, out = pub['decision_id'], []
    answers = {s: None for s in J.JUDGE_SLOTS}
    answers.update({r['slot']: 'EXIT' for r in pub['rows']})
    for row in pub['rows']:
        key = 'judge.settle:' + did + ':' + row['slot']
        if S.latest(conn, 'receipts', 'cache_key', key) is not None: continue
        ctx = _ctx(did, row['known_at'])
        ctx.cut = {'knowledge_cut': row['view_cut']}          # what the Judge saw, not its commit time (Gamma)
        r = EO.exit(ledger, context=ctx, order_id=row['order_id'], reason=row['public_reason'], now=now,
                    asker={'slot': row['slot'], 'decision_id': did, 'answers': dict(answers)})
        if r['state'] != 'unknown' or now >= ctx.deadline_at:
            S.append(conn, 'receipts', {'kind': 'judge.settle', 'cache_key': key, 'decision_id': did, 'label': 'EXIT',
                                        'slot': row['slot'], 'result': r, 'known_at': now})
        out.append(dict(r, decision_id=did, label='EXIT', slot=row['slot']))
    return out


def record_epoch_end(conn, ledger, *, now):
    """Persisted ledger halt latch -> one epoch.end receipt (FAILED, or BROKEN for config drift). The epoch row itself
    is never rewritten, so startup adoption keeps verifying the frozen open record."""
    if S.latest(conn, 'receipts', 'cache_key', 'epoch.end:' + X.NEW) is not None: return None
    from . import ledger as L
    latch = L.evaluation_safety_state(ledger, epoch_id=X.NEW)
    if latch.get('halted_at') is None: return None
    status = STATUS.get(latch['halt_kind'], 'FAILED')
    S.append(conn, 'receipts', {'kind': 'epoch.end', 'cache_key': 'epoch.end:' + X.NEW, 'epoch_id': X.NEW, 'status': status,
                                'halt_kind': latch['halt_kind'], 'halt_reason': latch['halt_reason'], 'halted_at': latch['halted_at'],
                                'halt_actor': latch['halt_actor'], 'known_at': now})
    return status


def settle(conn, ledger, *, policy, now, sol_usd, sol_at, candidate_of, limit=20):
    out = []
    for pub in J.recent_published(conn, now=now, limit=limit):
        did = pub['decision_id']
        if pub['decision'] == 'EXIT':
            out += _settle_exits(conn, ledger, pub, now=now)
            continue
        if S.latest(conn, 'receipts', 'cache_key', 'judge.settle:' + did) is not None: continue
        ctx = _ctx(did, pub['published_at'])
        rows = pub['rows']
        memos = {row['slot']: _memo(conn, row) for row in rows}
        candidate = candidate_of(rows[0]['candidate'])
        if any(m is None for m in memos.values()) or candidate is None:
            r = {'state': 'unknown', 'reason': 'judge_memo_or_candidate_pending', 'order_id': None, 'bundle_hash': None, 'outcome': 'unknown'}
        else:
            judgements = {row['slot']: {'decision': row['decision'], 'memo': memos[row['slot']][0],
                                        'served_model': memos[row['slot']][1], 'receipt_row_hash': row['candidate']} for row in rows}
            r = EO.enter(conn, ledger, policy, context=ctx, candidate=candidate, judgements=judgements, now=now,
                         sol_usd=sol_usd, sol_at=sol_at)
            if r['state'] == 'refused' and r.get('reason') in REVERSIBLE and now < ctx.deadline_at:
                r = dict(r, state='unknown')
        if r['state'] == 'submitted' and r.get('order_id'):
            X.mark_first_order(conn, at=now)                   # write-once, before the settle receipt (crash-safe)
        if r['state'] != 'unknown' or now >= ctx.deadline_at:
            S.append(conn, 'receipts', {'kind': 'judge.settle', 'cache_key': 'judge.settle:' + did, 'decision_id': did,
                                        'label': pub['decision'], 'result': r, 'known_at': now})
        out.append(dict(r, decision_id=did, label=pub['decision']))
    return out
