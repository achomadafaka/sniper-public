"""Office v2 exploratory epoch (record docs/audits/2026-10-30-office-v2-explore-1-epoch-record.md): office-v2-proof-1 -> office-v2-explore-1.
Office stage 1 seals the proof epoch (own transaction, org_changes receipt). Stage 2 opens the explore epoch, seats Head, one Judge and
Scout (same actor-id scheme, incarnation 1), creates the v2 order tables and the append-only change log, and enters segment 1 before the
control row moves. Ledger stage (separate DB, idempotent): the evaluated paper book, one initial fund, the kill-rule note. No model calls,
no service control, no funded DB. A sealed epoch never reopens."""
import hashlib
import json
import re

from . import epoch_transition as E, eval_orders as EO, eval_transition as X, ledger as L, seat_events as SE, store as S, v2_orders as VO
from . import v2_transition as V

PRED = V.NEW                                         # office-v2-proof-1
NEW = 'office-v2-explore-1'
BOOK = NEW
RECORD = 'docs/audits/2026-10-30-office-v2-explore-1-epoch-record.md'
RECORD_SHA256 = '65675eae02fb80c28de8a081a86ed9575b6e09e15c6d5ba0ad323943a5759038'
ROLES = ('head', 'judge', 'scout')
SEAL_KIND, OPEN_KIND = 'office_v2_proof_sealed', 'office_v2_explore_open'
CAPITAL_USD, CLIP_USD, MAX_CONCURRENT, DAILY_LOSS_USD = 1000., 25., 3, 100.     # eval-1's, record sec 5
LEDGER_NOTE = {'paper_eval': {'version': NEW, 'drawdown_usd': 200., 'unknown_max_s': 86400., 'mark_max_age_s': 60.,
                              'clock_skew_tolerance_s': 0.125, 'leg_stale_demote_s': 1800.}}     # eval-1's, record sec 5
CHANGE_KINDS = ('epoch_open', 'release', 'tool_set', 'prompt', 'seed_memory', 'model', 'roster', 'learning_rule', 'ticketless_sheets', 'judge_exit',
                'harness', 'config', 'override')
_HEX40, _HEX64 = re.compile('[0-9a-f]{40}'), re.compile('[0-9a-f]{64}')


def _hash(doc): return hashlib.sha256(S.canonical_json(doc)).hexdigest()


ADDED_ROLES = ('researcher', 'forensics', 'narrative', 'judge_gpt')   # only judge_gpt has order authority, on its own book; the others' configs carry no ledger, so the paper tools don't exist for them


def add_seat(conn, *, role, release_sha, now):
    """A seat added to the open epoch: one roster row, logged first. Idempotent."""
    if role not in ADDED_ROLES: raise ValueError('role_refused')
    actor = actor_id(role)
    record_change(conn, epoch_id=NEW, at=now, release_sha=release_sha, content_hash=hashlib.sha256(actor.encode()).hexdigest(), kind='roster', note=actor)
    conn.execute('INSERT OR IGNORE INTO office_v2_roster(actor_id,epoch_id,role,generation,incarnation,sealed,updated_at) VALUES(?,?,?,?,?,0,?)', (actor, NEW, role, 1, 1, now))
    conn.commit()
    return actor


def actor_id(role): return 'ag_' + hashlib.sha256((NEW + ':' + role).encode()).hexdigest()[:16]


def _actors(seats):
    """seats: {role: {'family','model'}} from the per-seat config; exactly the three roles, a Claude Judge."""
    if not isinstance(seats, dict) or set(seats) != set(ROLES): raise ValueError('explore_seats_invalid')
    out = {}
    for role in ROLES:
        s = seats[role]
        if (not isinstance(s, dict) or not all(isinstance(s.get(k), str) and s[k].strip() for k in ('family', 'model'))):
            raise ValueError('explore_seats_invalid')
        out[role] = {'actor_id': actor_id(role), 'role': role, 'family': s['family'], 'model': s['model']}
    if out['judge']['family'] != 'claude': raise ValueError('explore_seats_invalid')
    return out


def _book_terms():
    return {'book_id': BOOK, 'kind': 'evaluated', 'capital_usd': CAPITAL_USD, 'clip_usd': CLIP_USD,
            'max_concurrent': MAX_CONCURRENT, 'daily_loss_usd': DAILY_LOSS_USD}


def _open_hash(actors, book_policy):
    return _hash({'actors': actors, 'book': _book_terms(), 'book_policy': X.book_policy_identity(book_policy), 'record_sha256': RECORD_SHA256})


def _approval(actors, book_policy, release_sha):
    return _hash({'epoch': NEW, 'predecessor': PRED, 'open_hash': _open_hash(actors, book_policy), 'release_sha': release_sha})


# ---- change log (record sec 6): append-only, entered before the change takes effect -------------------------------------------------

def ensure_schema(conn):
    VO.ensure_schema(conn)
    conn.execute('CREATE TABLE IF NOT EXISTS office_v2_changes(epoch_id TEXT NOT NULL,segment INTEGER NOT NULL,at REAL NOT NULL,release_sha TEXT NOT NULL,'
                 'content_hash TEXT NOT NULL,kind TEXT NOT NULL,note TEXT NOT NULL DEFAULT \'\',PRIMARY KEY(epoch_id,segment),'
                 'CHECK(segment>=1),CHECK(length(release_sha)=40),CHECK(length(content_hash)=64))')
    for verb in ('UPDATE', 'DELETE'):
        conn.execute('CREATE TRIGGER IF NOT EXISTS office_v2_changes_no_%s BEFORE %s ON office_v2_changes BEGIN SELECT RAISE(ABORT,\'change_log_append_only\'); END'
                     % (verb.lower(), verb))


def segment_id(conn, epoch_id):
    """The current segment: the highest change-log entry. An order carries it; no entry means no order (segment_unknown)."""
    row = conn.execute('SELECT MAX(segment) FROM office_v2_changes WHERE epoch_id=?', (epoch_id,)).fetchone() if _has_log(conn) else None
    if row is None or row[0] is None: raise ValueError('segment_unknown')
    return row[0]


def _has_log(conn): return conn.execute("SELECT 1 FROM sqlite_master WHERE name='office_v2_changes'").fetchone() is not None


def record_change(conn, *, epoch_id, at, release_sha, content_hash, kind, note=''):
    """Enter a change; returns its new segment id. Call BEFORE the change takes effect. The same kind+hash+release as the latest entry is a retry and returns that segment."""
    if (kind not in CHANGE_KINDS or kind == 'epoch_open' or not E.O._number(at) or not isinstance(release_sha, str) or not _HEX40.fullmatch(release_sha)
            or not isinstance(content_hash, str) or not _HEX64.fullmatch(content_hash) or not isinstance(note, str) or len(note) > 500):
        raise ValueError('change_invalid')
    if kind == 'override' and not note.strip(): raise ValueError('override_needs_reference')   # Alpha's written reference; the kind itself flags measurement_definition_changed
    with S.write_transaction(conn):
        control = E._control(conn)
        if control is None or control.get('active_epoch') != epoch_id or epoch_id != NEW: raise ValueError('change_epoch_not_open')
        last = conn.execute('SELECT segment,at,release_sha,content_hash,kind FROM office_v2_changes WHERE epoch_id=? ORDER BY segment DESC LIMIT 1',
                            (epoch_id,)).fetchone()
        if last is None: raise ValueError('segment_unknown')
        # A non-change opens nothing. Compared per kind and scope (the note names the seat): start order must not make an unchanged tool set look new. A new release is its own 'release' entry.
        same = conn.execute('SELECT content_hash FROM office_v2_changes WHERE epoch_id=? AND kind=? AND note=? ORDER BY segment DESC LIMIT 1', (epoch_id, kind, note)).fetchone()
        if same is not None and same[0] == content_hash: return last[0]
        if at < last[1]: raise ValueError('change_clock_regressed')
        conn.execute('INSERT INTO office_v2_changes VALUES(?,?,?,?,?,?,?)', (epoch_id, last[0] + 1, at, release_sha, content_hash, kind, note))
        return last[0] + 1


def require_logged(conn, *, epoch_id, kind, content_hash):
    """Host guard: the latest entry of this kind must carry this hash, else the change was never entered (change_unlogged)."""
    row = conn.execute('SELECT content_hash FROM office_v2_changes WHERE epoch_id=? AND kind=? ORDER BY segment DESC LIMIT 1', (epoch_id, kind)).fetchone() \
        if _has_log(conn) else None
    if row is None or row[0] != content_hash: raise ValueError('change_unlogged')


# ---- Office stage -------------------------------------------------------------------------------------------------------------------

def _epoch(conn, epoch_id): return V._epoch(conn, epoch_id)


def _roster(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='office_v2_roster'").fetchone(): return {}
    return {r[0]: tuple(r[1:]) for r in conn.execute('SELECT actor_id,epoch_id,role,generation,incarnation,sealed FROM office_v2_roster')}


def _check(conn, now, actors, book_policy, release_sha):
    """Read-only. Returns state and the stages left; raises a typed explore_/v2_/epoch_ reason on any doubt."""
    if not E.O._number(now): raise ValueError('explore_clock_unknown')
    if not isinstance(release_sha, str) or not _HEX40.fullmatch(release_sha): raise ValueError('explore_release_invalid')
    if conn.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()[0] != 'office': raise ValueError('explore_office_store_required')
    if len(conn.execute('SELECT seq FROM org_changes LIMIT 5001').fetchall()) > 5000 or not S.verify_chain(conn, 'org_changes')[0]:
        raise ValueError('explore_org_chain_unknown')
    control = E._control(conn)
    if control is None: raise ValueError('explore_control_unknown')
    approval = _approval(actors, book_policy, release_sha)
    if control['active_epoch'] == NEW:
        frozen = V._event(conn, control['receipt_id'])
        if frozen.get('kind') != OPEN_KIND or frozen['approval_sha256'] != approval: raise ValueError('explore_approval_changed')
        want = {a['actor_id']: (NEW, a['role'], 1, 1, 0) for a in actors.values()}
        if _epoch(conn, NEW) != frozen['new_epoch'] or {k: v for k, v in _roster(conn).items() if v[0] == NEW} != want:
            raise ValueError('explore_record_changed')
        first = conn.execute('SELECT kind,release_sha FROM office_v2_changes WHERE epoch_id=? AND segment=1', (NEW,)).fetchone() if _has_log(conn) else None
        if first is None or tuple(first) != ('epoch_open', release_sha): raise ValueError('explore_record_changed')
        return {'state': 'already_applied', 'seal': False, 'open': False, 'approval_sha256': approval}
    if control['active_epoch'] != PRED: raise ValueError('explore_proof_not_active')
    E._lease(conn, now)                                  # the v1 supervisor must be off
    old = _epoch(conn, PRED)
    if old is None or old.get('kind') != 'v2_proof' or old.get('order_capable') is not False: raise ValueError('explore_proof_missing')
    seal = old.get('closed_at') is None
    if not seal:
        receipt = old.get('seal_receipt_id')
        if old.get('status') != 'SEALED' or not isinstance(receipt, str) or V._event(conn, receipt).get('kind') != SEAL_KIND:
            raise ValueError('explore_proof_closed_unexpectedly')
    elif not (E.O._number(old.get('opened_at')) and old['opened_at'] <= now): raise ValueError('explore_clock_unknown')
    if _epoch(conn, NEW) is not None: raise ValueError('explore_epoch_exists')
    seated = {k: v for k, v in _roster(conn).items() if k != V.PROOF_ACTOR_ID}
    if seated and seated != {a['actor_id']: (NEW, a['role'], 1, 1, 0) for a in actors.values()}: raise ValueError('explore_roster_conflict')
    if _has_log(conn) and conn.execute('SELECT 1 FROM office_v2_changes WHERE epoch_id=?', (NEW,)).fetchone(): raise ValueError('explore_record_changed')
    return {'state': 'pending', 'seal': seal, 'open': True, 'approval_sha256': approval}


def _seal_proof(conn, now):
    old = _epoch(conn, PRED)
    receipt = S.append(conn, 'org_changes', {'kind': SEAL_KIND, 'by': 'crew', 'recorded_by': 'crew', 'at': now, 'epoch_id': PRED, 'prior_epoch': old})
    conn.execute('UPDATE epochs SET payload_json=? WHERE id=?',
                 (S.canonical_json(dict(old, closed_at=now, status='SEALED', seal_receipt_id=receipt)).decode(), PRED))
    conn.execute('UPDATE office_v2_roster SET sealed=1,updated_at=? WHERE epoch_id=?', (now, PRED))
    return receipt


def _open_explore(conn, now, actors, book_policy, release_sha, approval):
    control = E._control(conn)
    old = _epoch(conn, PRED)
    SE.ensure_schema(conn)
    ensure_schema(conn)
    for a in actors.values():
        conn.execute('INSERT OR IGNORE INTO office_v2_roster(actor_id,epoch_id,role,generation,incarnation,sealed,updated_at) VALUES(?,?,?,?,?,0,?)',
                     (a['actor_id'], NEW, a['role'], 1, 1, now))
    epoch = dict(epoch_id=NEW, opened_at=now, closed_at=None, first_order_at=None, kind='v2_explore', pilot=True, exploratory=True,
                 evaluation_allowed=False, order_capable=True, book_id=BOOK, ledger_epoch_id=NEW, lessons_live=False, predecessor=PRED,
                 predecessor_seal_receipt_id=old['seal_receipt_id'], record=RECORD, record_sha256=RECORD_SHA256, transition_id=approval,
                 book=_book_terms(), book_policy=X.book_policy_identity(book_policy), actors=actors, release_sha=release_sha)
    receipt = S.append(conn, 'org_changes', {'kind': OPEN_KIND, 'by': 'crew', 'at': now, 'epoch_id': NEW, 'approval_sha256': approval,
                                              'new_epoch': epoch, 'prior_control': control})
    conn.execute('INSERT INTO office_v2_changes VALUES(?,?,?,?,?,?,?)',            # segment 1 lands in the same commit as the control move
                 (NEW, 1, now, release_sha, _open_hash(actors, book_policy), 'epoch_open', 'epoch open'))
    conn.execute('INSERT INTO epochs VALUES(?,?)', (NEW, S.canonical_json(epoch).decode()))
    history = list(control.get('history', [])) + [control['receipt_id']]
    conn.execute('UPDATE office_epoch_control SET payload_json=? WHERE id=1',
                 (S.canonical_json({'active_epoch': NEW, 'receipt_id': receipt, 'history': history}).decode(),))
    return receipt


def transition(conn, *, now, seats, book_policy, release_sha, apply=False):
    actors = _actors(seats)
    plan = _check(conn, now, actors, book_policy, release_sha)
    if plan['state'] == 'already_applied': return plan
    if not apply: return plan | {'state': 'dry_run'}
    if plan['seal']:
        with S.write_transaction(conn):
            if not _check(conn, now, actors, book_policy, release_sha)['seal']: raise ValueError('explore_proof_changed')
            _seal_proof(conn, now)
    with S.write_transaction(conn):                      # a separate commit: the proof epoch is durably sealed before any explore write
        again = _check(conn, now, actors, book_policy, release_sha)
        if again['state'] == 'already_applied': return again
        if again['seal']: raise ValueError('explore_proof_not_sealed')
        receipt = _open_explore(conn, now, actors, book_policy, release_sha, plan['approval_sha256'])
    return {'state': 'transitioned', 'receipt_id': receipt, 'approval_sha256': plan['approval_sha256']}


# ---- ledger stage -------------------------------------------------------------------------------------------------------------------

GPT_EPOCH = 'office-v2-explore-gpt-1'   # ledger epoch only: its own file, so coins, kill and pod totals are not shared with the Opus Judge's book
GPT_BOOK = GPT_EPOCH


def provision_gpt_ledger(ledger, *, now):
    """Idempotent. Same terms as the main book, in their own ledger. Returns a typed reason when the file does not match."""
    note = S.canonical_json({'paper_eval': dict(LEDGER_NOTE['paper_eval'], version=GPT_EPOCH)}).decode()
    if not ledger.execute('SELECT 1 FROM ledger_epochs WHERE epoch_id=?', (GPT_EPOCH,)).fetchone():
        L.open_epoch(ledger, epoch_id=GPT_EPOCH, clip_usd=CLIP_USD, rule_version=EO.PODS_RULE['version'], actor='crew', note=note, now=now, pods_rule=EO.PODS_RULE)
    if not ledger.execute('SELECT 1 FROM books WHERE book_id=?', (GPT_BOOK,)).fetchone():
        L.open_book(ledger, book_id=GPT_BOOK, epoch_id=GPT_EPOCH, kind='evaluated', actor='crew', note='GPT Judge book', now=now)
    if not ledger.execute("SELECT 1 FROM capital_flows WHERE book_id=? AND kind='initial'", (GPT_BOOK,)).fetchone():
        L.fund(ledger, book_id=GPT_BOOK, usd=CAPITAL_USD, kind='initial', actor='crew', note='GPT Judge initial paper', now=now)
    epoch = ledger.execute('SELECT clip_usd,note FROM ledger_epochs WHERE epoch_id=?', (GPT_EPOCH,)).fetchone()
    flows = ledger.execute('SELECT kind,usd FROM capital_flows WHERE book_id=? LIMIT 3', (GPT_BOOK,)).fetchall()
    book = ledger.execute('SELECT epoch_id,kind FROM books WHERE book_id=?', (GPT_BOOK,)).fetchone()
    rule = ledger.execute('SELECT rule_json FROM epoch_pods_rules WHERE epoch_id=?', (GPT_EPOCH,)).fetchone()
    ledger.commit()
    if (epoch[0] != CLIP_USD or epoch[1] != note or [tuple(f) for f in flows] != [('initial', CAPITAL_USD)] or tuple(book) != (GPT_EPOCH, 'evaluated')
            or rule is None or json.loads(rule[0]) != EO.PODS_RULE): return 'gpt_book_mismatch'
    return None


def book_bound(ledger):
    """None when the evaluated book exists in its own epoch with the frozen note and pod rule and exactly one initial flow; else a typed reason."""
    epoch = ledger.execute('SELECT clip_usd,note FROM ledger_epochs WHERE epoch_id=?', (NEW,)).fetchone()
    book = ledger.execute('SELECT epoch_id,kind FROM books WHERE book_id=?', (BOOK,)).fetchone()
    flows = ledger.execute('SELECT kind,usd FROM capital_flows WHERE book_id=? LIMIT 3', (BOOK,)).fetchall()
    if epoch is None or book is None or not flows: return 'explore_book_unbound'
    rule = ledger.execute('SELECT rule_json FROM epoch_pods_rules WHERE epoch_id=?', (NEW,)).fetchone()
    if (epoch[1] != S.canonical_json(LEDGER_NOTE).decode() or rule is None or json.loads(rule[0]) != EO.PODS_RULE or epoch[0] != CLIP_USD
            or tuple(book) != (NEW, 'evaluated') or [tuple(f) for f in flows] != [('initial', CAPITAL_USD)]):
        return 'explore_book_mismatch'
    return None


def provision_ledger(ledger, *, now):
    """Idempotent by ids; never a second initial fund. A half-provisioned book stays unbound."""
    if not ledger.execute('SELECT 1 FROM ledger_epochs WHERE epoch_id=?', (NEW,)).fetchone():
        L.open_epoch(ledger, epoch_id=NEW, clip_usd=CLIP_USD, rule_version=EO.PODS_RULE['version'], actor='crew',
                     note=S.canonical_json(LEDGER_NOTE).decode(), now=now, pods_rule=EO.PODS_RULE)
    if not ledger.execute('SELECT 1 FROM books WHERE book_id=?', (BOOK,)).fetchone():
        L.open_book(ledger, book_id=BOOK, epoch_id=NEW, kind='evaluated', actor='crew', note='office-v2-explore-1 book', now=now)
    if not ledger.execute("SELECT 1 FROM capital_flows WHERE book_id=? AND kind='initial'", (BOOK,)).fetchone():
        L.fund(ledger, book_id=BOOK, usd=CAPITAL_USD, kind='initial', actor='crew', note='office-v2-explore-1 initial paper', now=now)
    return book_bound(ledger)


def ready(conn, ledger):
    """None when the host may place orders: explore epoch active with segment 1 and the ledger book bound. Else a typed reason (a half-applied ledger stage never trades)."""
    return office_ready(conn) or book_bound(ledger)


def office_ready(conn):
    """The office side of ready, for any book: the explore epoch is active with segment 1."""
    control = E._control(conn)
    if control is None or control.get('active_epoch') != NEW: return 'explore_epoch_not_active'
    if not _has_log(conn) or conn.execute('SELECT 1 FROM office_v2_changes WHERE epoch_id=? AND segment=1', (NEW,)).fetchone() is None: return 'segment_unknown'
    return None
