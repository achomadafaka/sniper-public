"""Untrusted Office requests: frozen outbox effects, authenticated workflow updates.

Only host adapters construct TicketActor and supply lease fences. Model arguments
must never supply identity, source, role or a fence. No transport is opened here.
"""
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Callable

from . import store


@dataclass(frozen=True)
class TicketActor:
    actor_id: str
    role: str
    source: str
    fence: Callable[[],None]
    known_at: float|None=None
    reply_to: str|None=None


_TRANSITIONS={
    'proposed':{'head_approved'},'head_approved':{'queued'},'queued':{'crew_acknowledged'},
    'crew_acknowledged':{'assigned'},'assigned':{'resolved'},
    'resolved':{'office_verified','reopened'},'office_verified':{'reopened'},
    'reopened':{'head_approved'},
}
_CREW=frozenset({'Alpha','Codex'})


def _guard(check):
    if not callable(check):raise TypeError('trusted_fence_required')
    if check() is False:raise PermissionError('stale_generation')


def _text(value):
    if not isinstance(value,str) or not value.strip():raise ValueError('nonempty_text_required')
    return value.strip()


def _clock(value):
    if value is not None and (type(value) not in (float,int) or not math.isfinite(value)):
        raise ValueError('clock_unknown')
    return value


def _ensure(conn):
    conn.execute("CREATE INDEX IF NOT EXISTS office_tickets_pending ON tickets(json_extract(payload_json,'$.outbox_pending'),id)")


def get_ticket(conn,ticket_id):
    row=conn.execute('SELECT payload_json FROM tickets WHERE id=?',(ticket_id,)).fetchone()
    return None if row is None else json.loads(row[0])


# shortcut: history lives in one ticket row; split into event rows if it reaches broker byte limits.
def _save(conn,ticket):
    conn.execute('UPDATE tickets SET payload_json=? WHERE id=?',
                 (store.canonical_json(ticket).decode('utf-8'),ticket['ticket_id']))


def file_ticket(conn,*,author,problem,expected_impact,evidence_refs,done_when,guard,known_at=None):
    author,problem,done_when=map(_text,(author,problem,done_when))
    if expected_impact is not None and (type(expected_impact) not in (float,int) or not math.isfinite(expected_impact)):
        raise ValueError('invalid_expected_impact')
    if not isinstance(evidence_refs,list) or not evidence_refs:
        raise ValueError('evidence_required')
    refs=sorted({_text(ref) for ref in evidence_refs})
    normalized=' '.join(problem.split()).casefold()
    ticket_id='office-ticket-'+hashlib.sha256(store.canonical_json({'problem':normalized,'evidence':refs})).hexdigest()
    ticket=dict(ticket_id=ticket_id,author=author,problem=problem,expected_impact=expected_impact,
                evidence_refs=refs,done_when=done_when,known_at=_clock(known_at),state='proposed',
                untrusted_office_request=True,history=[],effects=[],last_event_number=-1,outbox_pending=False)
    with store.write_transaction(conn):
        _guard(guard);_ensure(conn)
        if get_ticket(conn,ticket_id) is None:
            conn.execute('INSERT INTO tickets VALUES(?,?)',(ticket_id,store.canonical_json(ticket).decode('utf-8')))
    return ticket_id


def _authorize(actor,state):
    if not isinstance(actor,TicketActor):raise TypeError('trusted_actor_required')
    _text(actor.actor_id);_clock(actor.known_at)
    if state in ('crew_acknowledged','assigned','resolved'):
        if actor.source!='crew' or actor.role!='crew' or actor.actor_id not in _CREW:
            raise PermissionError('authenticated_crew_required')
        _text(actor.reply_to)
    elif state=='queued':
        if actor.source!='office' or actor.role not in ('head','supervisor'):
            raise PermissionError('head_approval_required')
    elif actor.source!='office' or actor.role!='head':
        raise PermissionError('head_required')


def _effect(ticket,state):
    number=ticket['last_event_number']+1
    message_id=ticket['ticket_id'] if number==0 else ticket['ticket_id']+':'+str(number)
    body={key:ticket[key] for key in ('ticket_id','author','problem','expected_impact','evidence_refs','done_when')}
    body.update(type='untrusted_office_request',state=state,event_number=number,message_id=message_id)
    ticket['effects'].append(dict(message_id=message_id,body=store.canonical_json(body).decode('utf-8'),
                                  sent=False,attempts=0,delivery_status='pending',error=None))
    ticket['last_event_number']=number;ticket['outbox_pending']=True


def advance(conn,ticket_id,to_state,*,actor):
    if to_state not in _TRANSITIONS:raise ValueError('invalid_ticket_state')
    _authorize(actor,to_state)
    with store.write_transaction(conn):
        _guard(actor.fence)
        ticket=get_ticket(conn,ticket_id)
        if ticket is None:raise KeyError('ticket_missing')
        if actor.source=='crew':
            if any(h['to_state']==to_state and h.get('reply_to')==actor.reply_to for h in ticket['history']):
                return ticket
            queues=[e['message_id'] for e in ticket['effects'] if json.loads(e['body'])['state']=='queued']
            if not queues or actor.reply_to!=queues[-1]:raise ValueError('stale_workflow_reply')
        if ticket['state']==to_state:return ticket
        if to_state not in _TRANSITIONS[ticket['state']]:raise ValueError('invalid_ticket_transition')
        ticket['history'].append(dict(from_state=ticket['state'],to_state=to_state,
            actor_id=actor.actor_id,source=actor.source,known_at=actor.known_at,reply_to=actor.reply_to))
        ticket['state']=to_state
        if to_state in ('queued','office_verified','reopened'):_effect(ticket,to_state)
        _save(conn,ticket)
        return ticket


def drain_outbox(conn,send_fn,*,guard,limit=50):
    """Retry the exact effect bytes. Transport completion never becomes a crew ACK."""
    if conn.in_transaction:raise ValueError('network_transaction_active')
    if type(limit) is not int or not 0<limit<=100:raise ValueError('invalid_outbox_limit')
    with store.write_transaction(conn):
        _guard(guard);_ensure(conn)
        rows=conn.execute("SELECT payload_json FROM tickets WHERE json_extract(payload_json,'$.outbox_pending')=1 ORDER BY id LIMIT ?",(limit,)).fetchall()
    results=[]
    for row in rows:
        ticket=json.loads(row[0])
        for effect in ticket['effects']:
            if effect['sent']:continue
            if len(results)>=limit:return results
            _guard(guard)
            status='delivery_failed';error=None
            try:
                reply=send_fn(message_id=effect['message_id'],body=effect['body'])
                if (not isinstance(reply,dict) or reply.get('id')!=effect['message_id'] or
                    reply.get('delivery_status') not in ('transport_sent','codex_queued','delivery_failed','delivery_unknown')):
                    error='invalid_transport_reply'
                else:status=reply['delivery_status']
            except Exception as exc:error=type(exc).__name__
            with store.write_transaction(conn):
                _guard(guard)
                fresh=get_ticket(conn,ticket['ticket_id'])
                current=next(e for e in fresh['effects'] if e['message_id']==effect['message_id'])
                current['attempts']+=1;current['delivery_status']=status;current['error']=error
                current['sent']=current['sent'] or status in ('transport_sent','codex_queued')
                fresh['outbox_pending']=any(not e['sent'] for e in fresh['effects'])
                _save(conn,fresh)
            results.append(dict(ticket_id=ticket['ticket_id'],message_id=effect['message_id'],delivery_status=status,error=error))
    return results
