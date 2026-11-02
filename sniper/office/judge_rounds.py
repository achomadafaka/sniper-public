"""office-paper-eval-1 Judge rounds (prereg Am11 sec 19, fresh-sheet rule Am14): between turns the host closes the open
round and opens the next one by RE-SHEETING the newest unassigned admitted ticket of the frozen cohort (pass feed first,
then near_pass), assigning the new sheet only if it is at most JUDGE_FRESH_S old. Both Judges' frames carry that one
item while it is still fresh at their own cut. A round ends (append-only judge.round.end) when both Judges committed
judge.decide on it, or when it is older than ROUND_MAX_S (round_incomplete, counted by which slots committed). Each
ticket and each sheet receipt is assigned at most once. Closing reads only whether a slot committed, never its decision."""
import hashlib
import json
import math

from . import judge_decisions as J, store as S

JUDGE_FRESH_S = 120.        # Am14 SHEET_FRESH_S: a sheet is assigned / shown only while at most this old
SHEET_BUDGET_S = 30.        # wall time for one host pass's sheet builds (the RPC fallback alone can take ~16 s)
RULE = 'am14'
ROUND_MAX_S = 120.          # a round never outlives its sheet's freshness
PICK_TRIES = 3              # Am14 SHEET_TRIES: tickets re-sheeted per host pass (bounds tape/RPC cost)
FEEDS = ('pass', 'near_pass')


def _open(conn):
    row = conn.execute("SELECT payload_json FROM receipts INDEXED BY receipts_cache_key_seq WHERE cache_key='judge.round:latest' "
                       "ORDER BY seq DESC LIMIT 1").fetchone()
    latest = None if row is None else json.loads(row[0])
    if latest is None or S.latest(conn, 'receipts', 'cache_key', 'judge.round.end:' + latest['round_id']) is not None:
        return None
    return latest


def _slots(conn, epoch_id, row_hash):
    did = J.decision_id(epoch_id, {'decision': 'ENTER', 'candidate': row_hash, 'order_id': None, 'public_reason': 'x'})
    return sorted({r['slot'] for r in J.rows_for(conn, did)})


def frame_round(conn, *, cut):
    """The open round's item for a Judge frame at `cut`, only while causal and fresh; else no candidate."""
    rnd = _open(conn)
    if rnd is None:
        return {'round_id': None, 'item': None, 'reason': 'no_open_round'}
    age = cut - rnd['item']['known_at']
    if not 0 <= age <= JUDGE_FRESH_S:
        return {'round_id': rnd['round_id'], 'item': None, 'reason': 'sheet_not_fresh_at_cut'}
    return {'round_id': rnd['round_id'], 'item': rnd['item'], 'reason': None, 'sheet_age_s': age}


def advance(conn, *, epoch_id, now, judges, tickets, sheet, wall=None, clock=None):
    """Host pass. tickets(now) -> listed tickets newest first ({ticket_id, feed}); sheet(ticket_id) -> a freshly made,
    verified candidate item or None. Returns the open round after the pass (or None) and the sheet attempts made."""
    if type(now) not in (int,float) or not math.isfinite(now) or now<0:
        raise ValueError('round_clock_unknown')
    assignment_clock=clock if clock is not None else lambda:now
    last_at=now
    attempts = []
    rnd = _open(conn)
    if rnd is not None:
        slots = _slots(conn, epoch_id, rnd['candidate'])
        done = set(slots) == set(J.JUDGE_SLOTS)
        if done or now - rnd['round_cut'] > ROUND_MAX_S:
            S.append(conn, 'receipts', {'kind': 'judge.round.end', 'cache_key': 'judge.round.end:' + rnd['round_id'],
                                        'epoch_id': epoch_id, 'round_id': rnd['round_id'], 'committed_slots': slots,
                                        'outcome': 'complete' if done else 'round_incomplete',
                                        'reason': None if done else ('no_commit' if not slots else 'one_judge:' + ','.join(slots)),
                                        'known_at': now})
        else:
            return rnd, attempts
    import time as _t
    wall = wall or _t.monotonic
    started = wall()
    unused = lambda t: (S.latest(conn, 'receipts', 'cache_key', 'judge.round.ticket:' + t['ticket_id']) is None
                        and S.latest(conn, 'receipts', 'cache_key', 'judge.round.sheet_failed:' + t['ticket_id']) is None)
    listed = [t for t in tickets(now) if t.get('feed') in FEEDS and unused(t)]
    passes = [t for t in listed if t['feed'] == 'pass']
    listed = passes or [t for t in listed if t['feed'] == 'near_pass']      # Am14: near_pass only when no PASS ticket exists
    for t in listed:
        if len(attempts) >= PICK_TRIES or wall() - started > SHEET_BUDGET_S:
            break
        got = sheet(t['ticket_id'])
        # sheet() returns a verified item, or {'unavailable': True, 'state', 'reason'} from the sheet tool itself
        detail = {'state': got.get('state'), 'reason': str(got.get('reason'))[:200]} if isinstance(got, dict) and got.get('unavailable') else None
        item = None if detail is not None else got
        # The sheet is created after pass start. Assign at a fresh trusted cut, never backdate or clamp its age.
        assigned_at=assignment_clock()
        if type(assigned_at) not in (int,float) or not math.isfinite(assigned_at) or assigned_at<last_at:
            raise ValueError('round_clock_unknown_or_regressed')
        last_at=assigned_at
        stamp=item.get('known_at') if isinstance(item,dict) else None
        age=assigned_at-stamp if type(stamp) in (int,float) and math.isfinite(stamp) else None
        state='fresh' if age is not None and 0<=age<=JUDGE_FRESH_S else 'unavailable' if age is None or age<0 else 'too_old'
        attempts.append({'ticket_id': t['ticket_id'], 'feed': t['feed'], 'state': state})
        if state != 'fresh' or S.latest(conn, 'receipts', 'cache_key', 'judge.round.assigned:' + item['receipt_row_hash']) is not None:
            S.append(conn, 'receipts', {'kind': 'judge.round.sheet_failed', 'cache_key': 'judge.round.sheet_failed:' + t['ticket_id'],
                                        'epoch_id': epoch_id, 'ticket_id': t['ticket_id'], 'feed': t['feed'], 'reason': 'sheet_failed:' + state, 'reason_detail': detail,
                                        'sheet_age_s': age, 'known_at': assigned_at})
            continue
        rid = hashlib.sha256(S.canonical_json([epoch_id, item['receipt_row_hash']])).hexdigest()
        rnd = {'kind': 'judge.round', 'cache_key': 'judge.round:latest', 'rule': RULE, 'round_id': rid, 'epoch_id': epoch_id,
               'candidate': item['receipt_row_hash'], 'ticket_id': t['ticket_id'], 'feed': item['feed'], 'judges': sorted(judges),
               'round_cut': assigned_at, 'sheet_age_s': age, 'item': item, 'known_at': assigned_at}
        with S.write_transaction(conn):           # round + its two markers land together or not at all
            S.append(conn, 'receipts', rnd)
            S.append(conn, 'receipts', {'kind': 'judge.round.assigned', 'cache_key': 'judge.round.assigned:' + item['receipt_row_hash'],
                                        'epoch_id': epoch_id, 'round_id': rid, 'known_at': assigned_at})
            S.append(conn, 'receipts', {'kind': 'judge.round.ticket', 'cache_key': 'judge.round.ticket:' + t['ticket_id'],
                                        'epoch_id': epoch_id, 'round_id': rid, 'known_at': assigned_at})
        return rnd, attempts
    return None, attempts
