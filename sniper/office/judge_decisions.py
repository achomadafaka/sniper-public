"""office-paper-eval-1 Judge decisions (request tool judge.decide). Validation against the Judge's own frame, an
append-only receipt per Judge (cache_key 'judge.decide:<decision_id>', indexed), and publication: an entry-type
decision (ENTER/SKIP/WAIT) publishes when both Judges committed the same decision_id; an EXIT publishes on one
(Alpha: any single Judge EXIT closes). Unpublished rows stay invisible to other seats and the floor (Judge blindness)."""
import hashlib
import json
import math
import re

from . import store as S

KIND = 'judge.decide'
DECISIONS = ('ENTER', 'SKIP', 'WAIT', 'EXIT')
JUDGE_SLOTS = ('judge_claude', 'judge_gpt')
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
_DISPROOF = re.compile(r'^\s*Disproof:.*$', re.M)


def decision_id(epoch_id, args):
    target = ['exit', args['order_id']] if args['decision'] == 'EXIT' else ['candidate', args['candidate']]
    return hashlib.sha256(S.canonical_json([epoch_id] + target)).hexdigest()


def validate(args, *, candidates, open_orders):
    """candidates: receipt_row_hashes in this Judge's frame; open_orders: order_ids in its book view."""
    if not isinstance(args, dict) or set(args) != {'decision', 'candidate', 'order_id', 'public_reason'}:
        raise ValueError('judge_decide_shape')
    d, c, o, r = args['decision'], args['candidate'], args['order_id'], args['public_reason']
    if d not in DECISIONS: raise ValueError('judge_decide_decision')
    if not isinstance(r, str) or not 1 <= len(r.strip()) <= 160: raise ValueError('judge_decide_public_reason')
    if d == 'EXIT':
        if c is not None or not isinstance(o, str) or o not in open_orders: raise ValueError('judge_decide_exit_target')
    elif o is not None or not isinstance(c, str) or not _HEX64.match(c) or c not in candidates:
        raise ValueError('judge_decide_candidate_target')


def record(conn, *, context, slot, args, now, equity_snapshot_id, lesson_ids):
    did = decision_id(context.epoch_id, args)
    row = {'kind': KIND, 'cache_key': KIND + ':' + did, 'decision_id': did, 'epoch_id': context.epoch_id,
           'actor_id': context.actor_id, 'generation': context.generation, 'slot': slot, 'turn_decision_id': context.decision_id,
           'decision': args['decision'], 'candidate': args['candidate'], 'order_id': args['order_id'],
           'public_reason': args['public_reason'].strip(), 'equity_snapshot_id': equity_snapshot_id,
           'lesson_ids': list(lesson_ids), 'view_cut': context.cut['knowledge_cut'], 'known_at': now}
    return S.append(conn, 'receipts', row)


def disproof(memo):
    """The entry call's Disproof line (Epsilon prompt 17f673691), or None."""
    m = _DISPROOF.search(memo or '')
    return m.group(0).strip()[:400] if m else None


def rows_for(conn, did, *, before=None):
    out = []
    for payload, row_hash in conn.execute('SELECT payload_json,row_hash FROM receipts WHERE cache_key=? ORDER BY seq LIMIT 8',
                                          (KIND + ':' + did,)):
        r = json.loads(payload)
        if before is None or r['known_at'] <= before:
            out.append(dict(r, row_hash=row_hash))
    return out


def published(rows):
    """None until publishable; else {decision, published_at, rows}. First commit per slot counts; later ones are ignored."""
    first = {}
    for r in rows:
        first.setdefault(r['slot'], r)
    if any(r['decision'] == 'EXIT' for r in first.values()):
        exits = sorted((r for r in first.values() if r['decision'] == 'EXIT'), key=lambda r: r['known_at'])
        return {'decision': 'EXIT', 'published_at': exits[0]['known_at'], 'rows': exits}   # each asking Judge (Gamma d9da66b0b)
    if set(first) != set(JUDGE_SLOTS): return None
    decisions = {r['decision'] for r in first.values()}
    label = decisions.pop() if len(decisions) == 1 else 'split'
    return {'decision': label, 'published_at': max(r['known_at'] for r in first.values()), 'rows': [first[s] for s in JUDGE_SLOTS]}


def recent_published(conn, *, now, limit=50):
    """One bounded indexed query: newest400 Judge receipts, first8 commits per selected target.

    The first commits may predate the newest window. Preserve them rather than treating a
    later duplicate as a new verdict. Missing index/invalid selected proof refuses; no GET DDL.
    """
    if type(now) not in (int,float) or not math.isfinite(now) or now<0:
        raise ValueError('judge_read_clock_unknown')
    if type(limit) is not int or limit<0:raise ValueError('judge_read_limit')
    rows=conn.execute("""WITH targets AS MATERIALIZED (
      SELECT DISTINCT cache_key FROM (
        SELECT cache_key FROM receipts INDEXED BY receipts_judge_decide_seq
        WHERE json_extract(payload_json,'$.kind')='judge.decide' ORDER BY seq DESC LIMIT 400))
      SELECT r.seq,r.payload_json,r.prev_hash,r.row_hash,p.row_hash
      FROM targets t CROSS JOIN receipts r
      LEFT JOIN receipts p ON p.seq=r.seq-1
      WHERE r.seq IN (SELECT seq FROM receipts INDEXED BY receipts_cache_key_seq
                      WHERE cache_key=t.cache_key ORDER BY seq LIMIT 8)
      ORDER BY r.seq""").fetchall()
    grouped={}
    for seq,payload,prev,digest,prior in rows:
        try:
            row=json.loads(payload)
            canonical=S.canonical_json(row)
            if (canonical.decode()!=payload or hashlib.sha256(canonical+prev.encode('ascii')).hexdigest()!=digest
                    or prev!=('' if seq==1 else prior)):
                raise ValueError('judge_receipt_integrity')
            args={k:row[k] for k in ('decision','candidate','order_id','public_reason')}
            validate(args,candidates={row['candidate']},open_orders={row['order_id']})
            did=decision_id(row['epoch_id'],args)
            at=row['known_at']
            if (row['kind']!=KIND or row['cache_key']!=KIND+':'+did or row['decision_id']!=did
                    or row['slot'] not in JUDGE_SLOTS or type(at) not in (int,float)
                    or not math.isfinite(at) or at<0):
                raise ValueError('judge_receipt_shape')
        except (KeyError,TypeError,UnicodeError,RecursionError) as exc:
            raise ValueError('judge_receipt_integrity') from exc
        if at<=now:grouped.setdefault(did,[]).append(dict(row,row_hash=digest))
    out=[]
    for did,selected in grouped.items():
        pub=published(selected)
        if pub:out.append(dict(pub,decision_id=did))
    return sorted(out,key=lambda p:-p['published_at'])[:limit]
