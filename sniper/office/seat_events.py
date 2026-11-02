"""Office v2 feed and roster schema. Producers are host-only; every text field is hostile when displayed."""

import hashlib
import json
import re
import sqlite3
import uuid

from . import store as S

KINDS=('thought','tool_call','tool_result','browser_visit','lifecycle','request','standup','workspace')
STATES=('known','partial','unknown')


class OperatorDmRefused(ValueError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


def ensure_schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS office_v2_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,'
                 'event_id TEXT NOT NULL UNIQUE,epoch_id TEXT NOT NULL,actor_id TEXT NOT NULL,generation INTEGER NOT NULL,'
                 'incarnation INTEGER NOT NULL,corrected_at REAL,kind TEXT NOT NULL,state TEXT NOT NULL,text TEXT,'
                 'truncated INTEGER NOT NULL DEFAULT 0,refs_json TEXT,web_tainted INTEGER NOT NULL DEFAULT 0,'
                 "CHECK(kind IN ('thought','tool_call','tool_result','browser_visit','lifecycle','request','standup','workspace')),"
                 "CHECK(state IN ('known','partial','unknown')),CHECK(truncated IN (0,1)),CHECK(web_tainted IN (0,1)),"
                 'CHECK(text IS NULL OR length(CAST(text AS BLOB))<=4096),CHECK(refs_json IS NULL OR length(CAST(refs_json AS BLOB))<=4096))')
    conn.execute('CREATE INDEX IF NOT EXISTS office_v2_events_actor ON office_v2_events(actor_id,seq)')
    conn.execute('CREATE INDEX IF NOT EXISTS office_v2_events_at ON office_v2_events(corrected_at)')   # retention
    # One row per seat in the current epoch: the UI labels an event stale when its epoch, generation or incarnation differs.
    conn.execute('CREATE TABLE IF NOT EXISTS office_v2_roster(actor_id TEXT PRIMARY KEY,epoch_id TEXT NOT NULL,role TEXT NOT NULL,'
                 'generation INTEGER NOT NULL,incarnation INTEGER NOT NULL,sealed INTEGER NOT NULL DEFAULT 0,updated_at REAL NOT NULL,'
                 'CHECK(sealed IN (0,1)))')
    # office_dm_events has generation but no epoch or incarnation. Mail without a row here is unbound: withhold it, never guess.
    conn.execute('CREATE TABLE IF NOT EXISTS office_v2_mail_bind(message_id TEXT PRIMARY KEY,epoch_id TEXT NOT NULL,'
                 'from_incarnation INTEGER,to_incarnation INTEGER NOT NULL)')   # from_incarnation NULL: the operator


def operator_dm(conn,*,to_actor_id,client_id,text,now):
    """Sender and generation are set here, never by the caller. The caller has no open transaction; route auth is the caller's job."""
    if not isinstance(client_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',client_id):raise OperatorDmRefused('client_id_invalid')
    try:
        if not isinstance(text,str) or not text.strip() or len(text.encode('utf-8'))>4096:raise OperatorDmRefused('text_invalid')
    except UnicodeError:raise OperatorDmRefused('text_invalid') from None
    message_id='op_'+hashlib.sha256(client_id.encode()).hexdigest()
    try:
        with S.write_transaction(conn):
            prior=conn.execute('SELECT to_actor_id,text FROM office_dm_events WHERE message_id=?',(message_id,)).fetchone()
            if prior is not None:
                if (prior[0],prior[1])!=(to_actor_id,text):raise OperatorDmRefused('client_id_conflict')
                return dict(state='duplicate',message_id=message_id)
            seat=conn.execute('SELECT epoch_id,generation,incarnation,sealed FROM office_v2_roster WHERE actor_id=?',(to_actor_id,)).fetchone()
            if seat is None:raise OperatorDmRefused('unknown_actor')
            if seat[3]:raise OperatorDmRefused('actor_sealed')
            seq=conn.execute('INSERT INTO office_dm_events(message_id,from_actor_id,from_generation,to_actor_id,to_generation,created_at,text,refs_json,receipt_id)'
                             " VALUES(?,'operator',0,?,?,?,?,'[]',?)",(message_id,to_actor_id,seat[1],now,text,'operator:'+message_id)).lastrowid
            conn.execute('INSERT INTO office_dm_pending(message_id,to_actor_id,to_generation,created_at,event_seq) VALUES(?,?,?,?,?)',
                         (message_id,to_actor_id,seat[1],now,seq))
            conn.execute('INSERT INTO office_v2_mail_bind(message_id,epoch_id,from_incarnation,to_incarnation) VALUES(?,?,NULL,?)',(message_id,seat[0],seat[2]))
        return dict(state='sent',message_id=message_id)
    except sqlite3.Error as exc:
        raise OperatorDmRefused('store_unavailable') from exc


def record_event(conn,*,epoch_id,actor_id,generation,incarnation,at,kind,state='known',text=None,refs=None,web_tainted=0):
    """Hostile text is stored bounded and marked truncated, never rejected: a long tool output must not lose the event."""
    raw=None if text is None else text.encode('utf-8','replace')
    truncated=int(raw is not None and len(raw)>4096)
    if raw is not None:text=raw[:4096].decode('utf-8','ignore')
    blob=None if refs is None else json.dumps(refs)
    if blob is not None and len(blob.encode())>4096:blob,truncated=json.dumps(dict(omitted=True)),1
    event_id=uuid.uuid4().hex
    with S.write_transaction(conn):
        conn.execute('INSERT INTO office_v2_events(event_id,epoch_id,actor_id,generation,incarnation,corrected_at,kind,state,text,truncated,refs_json,web_tainted)'
                     ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(event_id,epoch_id,actor_id,generation,incarnation,at,kind,state,text,truncated,blob,int(web_tainted)))
    return event_id


def seat_dm(conn,*,from_actor,from_generation,from_incarnation,to_actor_id,text,now,sender_tainted=False):
    """Seat-to-seat mail. Sender identity is the caller's bound socket identity."""
    try:
        if not isinstance(text,str) or not text.strip() or len(text.encode('utf-8'))>4096:raise OperatorDmRefused('text_invalid')
    except UnicodeError:raise OperatorDmRefused('text_invalid') from None
    message_id='seat_'+uuid.uuid4().hex
    with S.write_transaction(conn):
        seat=conn.execute('SELECT epoch_id,generation,incarnation,sealed FROM office_v2_roster WHERE actor_id=?',(to_actor_id,)).fetchone()
        if seat is None:raise OperatorDmRefused('unknown_actor')
        if seat[3]:raise OperatorDmRefused('actor_sealed')
        seq=conn.execute('INSERT INTO office_dm_events(message_id,from_actor_id,from_generation,to_actor_id,to_generation,created_at,text,refs_json,receipt_id)'
                         " VALUES(?,?,?,?,?,?,?,?,?)",(message_id,from_actor,from_generation,to_actor_id,seat[1],now,text,'["sender_tainted_by_web"]' if sender_tainted else '[]','seat:'+message_id)).lastrowid
        conn.execute('INSERT INTO office_dm_pending(message_id,to_actor_id,to_generation,created_at,event_seq) VALUES(?,?,?,?,?)',(message_id,to_actor_id,seat[1],now,seq))
        conn.execute('INSERT INTO office_v2_mail_bind(message_id,epoch_id,from_incarnation,to_incarnation) VALUES(?,?,?,?)',(message_id,seat[0],from_incarnation,seat[2]))
    return dict(state='sent',message_id=message_id)


CREW_ROLES=frozenset({'alpha','beta','gamma','lambda','codex','delta','epsilon','zeta'})


def is_host_sender(actor):
    """Host-side mail senders that are not seats. Crew mail is never the operator."""
    return actor in ('operator','crew','watch') or (isinstance(actor,str) and actor.startswith('crew:') and actor[5:] in CREW_ROLES)


def crew_dm(conn,*,role,to_role,text,now):
    """Host-side crew mail to one role's unsealed seats. Sender and first line both name the crew role. All or nothing on the size; a rerun mails again."""
    if role not in CREW_ROLES:raise OperatorDmRefused('crew_role_unknown')
    body='CREW %s (crew, not the operator):\n%s'%(role,text)
    if not isinstance(text,str) or not text.strip() or len(body.encode('utf-8'))>4096:raise OperatorDmRefused('text_invalid')
    actors=[r[0] for r in conn.execute('SELECT actor_id FROM office_v2_roster WHERE sealed=0 AND role=?',(to_role,))]
    if not actors:raise OperatorDmRefused('unknown_actor')
    return [seat_dm(conn,from_actor='crew:'+role,from_generation=0,from_incarnation=0,to_actor_id=a,text=body,now=now)['message_id'] for a in actors]


def seat_to_operator(conn,*,from_actor,from_generation,from_incarnation,text,now,refs=()):
    """A seat's reply to the operator. Nothing is pending, so it wakes nobody; the sender is the caller's bound socket identity."""
    try:
        if not isinstance(text,str) or not text.strip() or len(text.encode('utf-8'))>4096:raise OperatorDmRefused('text_invalid')
    except UnicodeError:raise OperatorDmRefused('text_invalid') from None
    message_id='seat_'+uuid.uuid4().hex
    with S.write_transaction(conn):
        seat=conn.execute('SELECT epoch_id,sealed,generation,incarnation FROM office_v2_roster WHERE actor_id=?',(from_actor,)).fetchone()
        if seat is None:raise OperatorDmRefused('unknown_actor')
        if seat[1]:raise OperatorDmRefused('actor_sealed')
        if (seat[2],seat[3])!=(from_generation,from_incarnation):raise OperatorDmRefused('stale_identity')
        conn.execute('INSERT INTO office_dm_events(message_id,from_actor_id,from_generation,to_actor_id,to_generation,created_at,text,refs_json,receipt_id)'
                     " VALUES(?,?,?,'operator',?,?,?,?,?)",(message_id,from_actor,from_generation,from_generation,now,text,json.dumps(list(refs)),'seat:'+message_id))
        conn.execute('INSERT INTO office_v2_mail_bind(message_id,epoch_id,from_incarnation,to_incarnation) VALUES(?,?,?,?)',(message_id,seat[0],from_incarnation,from_incarnation))
    return dict(state='sent',message_id=message_id)


def _notifications(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS office_v2_notifications(page_id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,incarnation INTEGER,epoch_id TEXT,sent_at REAL NOT NULL,'
                 'state TEXT NOT NULL,case_sha256 TEXT NOT NULL,evidence_sha256 TEXT NOT NULL,evidence_ids_json TEXT NOT NULL,pages_this_hour INTEGER NOT NULL)')
    for column,kind in (('repeat_count','INTEGER NOT NULL DEFAULT 0'),('evidence_ids_dropped','INTEGER NOT NULL DEFAULT 0')):
        if column not in {r[1] for r in conn.execute('PRAGMA table_info(office_v2_notifications)')}:
            try:conn.execute('ALTER TABLE office_v2_notifications ADD COLUMN %s %s'%(column,kind))
            except sqlite3.OperationalError as exc:   # another seat's first use won the race
                if 'duplicate column' not in str(exc):raise


def notifications_paged(conn,*,actor_id,since):
    _notifications(conn)
    return conn.execute("SELECT count(*) FROM office_v2_notifications WHERE actor_id=? AND state='telegram_delivered' AND sent_at>?",(actor_id,since)).fetchone()[0]


def repeat_notification(conn,*,actor_id,case_sha256,since):
    """The same case from the same seat within the window is one row, counted: a looping seat can't grow the table or the inbox."""
    _notifications(conn)
    row=conn.execute('SELECT page_id FROM office_v2_notifications WHERE actor_id=? AND case_sha256=? AND sent_at>? ORDER BY sent_at DESC LIMIT 1',(actor_id,case_sha256,since)).fetchone()
    if row is None:return None
    conn.execute('UPDATE office_v2_notifications SET repeat_count=repeat_count+1 WHERE page_id=?',(row[0],));conn.commit()
    return row[0]


def record_notification(conn,*,page_id,actor_id,incarnation,epoch_id,sent_at,want_page,since,case_sha256,evidence_sha256,evidence_ids,evidence_ids_dropped=0):
    """Append-only: the inbox keeps the text, this says which claim a later reading answers. The hour's page slot is taken here, before any network send, and across processes: page_reserved counts as used, so a crash after the send can't allow a retry within the hour."""
    _notifications(conn)
    conn.commit()
    conn.execute('BEGIN IMMEDIATE')
    try:
        prior=conn.execute("SELECT count(*) FROM office_v2_notifications WHERE actor_id=? AND state IN ('telegram_delivered','telegram_unknown','page_reserved') AND sent_at>?",(actor_id,since)).fetchone()[0]
        state='page_reserved' if want_page and not prior else 'queued_in_inbox'
        conn.execute('INSERT INTO office_v2_notifications(page_id,actor_id,incarnation,epoch_id,sent_at,state,case_sha256,evidence_sha256,evidence_ids_json,pages_this_hour,evidence_ids_dropped) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(page_id,actor_id,incarnation,epoch_id,sent_at,state,case_sha256,evidence_sha256,json.dumps(evidence_ids),prior,evidence_ids_dropped))
        conn.commit()
    except BaseException:
        conn.rollback();raise
    return state


def settle_notification(conn,*,page_id,state):
    conn.execute('UPDATE office_v2_notifications SET state=? WHERE page_id=?',(state,page_id));conn.commit()


def pending_mail(conn,*,actor_id,generation):
    return conn.execute('SELECT count(*) FROM office_dm_pending WHERE to_actor_id=? AND to_generation=?',(actor_id,generation)).fetchone()[0]


def read_mail(conn,*,actor_id,generation,now,limit=32,on_operator=None):
    """Mail is data from another seat or the operator. Reading removes it from pending and records the read."""
    with S.write_transaction(conn):
        rows=conn.execute('SELECT p.message_id,e.from_actor_id,e.created_at,e.text,e.refs_json FROM office_dm_pending p JOIN office_dm_events e ON e.seq=p.event_seq '
                          'WHERE p.to_actor_id=? AND p.to_generation=? ORDER BY p.created_at,p.event_seq LIMIT ?',(actor_id,generation,limit)).fetchall()
        # Inside the transaction, on the exact batch: if the save raises, the mail stays pending.
        if on_operator is not None and any(r[1]=='operator' for r in rows):on_operator()
        for row in rows:
            conn.execute('DELETE FROM office_dm_pending WHERE message_id=?',(row[0],))
        roles=dict(conn.execute('SELECT actor_id,role FROM office_v2_roster WHERE actor_id IN (%s)'%','.join('?'*len(rows)),[r[1] for r in rows]).fetchall()) if rows else {}
    return [dict(message_id=r[0],sender=r[1],sender_role='operator' if r[1]=='operator' else roles.get(r[1]),at=r[2],text=r[3],sender_tainted_by_web='sender_tainted_by_web' in (r[4] or '')) for r in rows]


def roster_incarnation(conn,*,actor_id):
    row=conn.execute('SELECT incarnation FROM office_v2_roster WHERE actor_id=?',(actor_id,)).fetchone()
    return row[0] if row else None


def set_incarnation(conn,*,actor_id,incarnation,now):
    with S.write_transaction(conn):
        changed=conn.execute('UPDATE office_v2_roster SET incarnation=?,updated_at=? WHERE actor_id=?',(incarnation,now,actor_id)).rowcount
    if not changed:raise ValueError('roster_actor_unknown')
