"""Crew requests stay durable, idempotent and explicitly untrusted."""
import json
import pytest
from sniper.office import store,tickets as T


def fence():pass


@pytest.fixture
def db(tmp_path):
    con=store.open_store(tmp_path/'office.db')
    yield con
    con.close()


def actor(name='head',role='head',source='office',guard=fence,reply_to=None):
    return T.TicketActor(name,role,source,guard,reply_to=reply_to)


def file(db,**changes):
    return T.file_ticket(db,**({'author':'scout','problem':'Reader is stale','expected_impact':None,
        'evidence_refs':['receipt:1'],'done_when':'A fresh source frontier is observed','guard':fence}|changes))


def ready(db):
    tid=file(db)
    for state in ('head_approved','queued'):T.advance(db,tid,state,actor=actor())
    return tid


def test_unknown_impact_allowed_never_invented(db):
    tid=file(db);row=T.get_ticket(db,tid)
    assert row['expected_impact'] is None and row['known_at'] is None
    assert row['untrusted_office_request'] is True and row['state']=='proposed'
    assert tid==file(db)
    assert db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]==1


def test_crash_after_send_before_mark_no_duplicate_work(db):
    tid=ready(db);seen={};attempts=[]
    class Crash(BaseException):pass
    def send(*,message_id,body):
        assert not db.in_transaction
        attempts.append((message_id,body))
        if message_id not in seen:
            seen[message_id]=body
            raise Crash
        assert seen[message_id]==body
        return {'id':message_id,'delivery_status':'transport_sent'}
    with pytest.raises(Crash):T.drain_outbox(db,send,guard=fence)
    T.drain_outbox(db,send,guard=fence)
    assert len(seen)==1 and len(attempts)==2 and attempts[0]==attempts[1]
    assert attempts[0][0]==tid and T.get_ticket(db,tid)['state']=='queued'


def test_duplicate_ack_advances_once_and_sender_text_is_not_authority(db):
    tid=ready(db);crew=actor('Alpha','crew','crew',reply_to=tid)
    T.advance(db,tid,'crew_acknowledged',actor=crew)
    before=T.get_ticket(db,tid)
    T.advance(db,tid,'crew_acknowledged',actor=crew)
    assert T.get_ticket(db,tid)==before
    other=file(db,problem='different')
    with pytest.raises((PermissionError,ValueError)):
        T.advance(db,other,'head_approved',actor=actor('scout','scout'))
    with pytest.raises((TypeError,PermissionError)):
        T.advance(db,other,'head_approved',actor={'actor_id':'head','role':'head','source':'office'})


def test_transport_sent_is_not_acknowledged(db):
    tid=ready(db)
    T.drain_outbox(db,lambda **kw:{'id':kw['message_id'],'delivery_status':'transport_sent'},guard=fence)
    assert T.get_ticket(db,tid)['state']=='queued'
    with pytest.raises(PermissionError):T.advance(db,tid,'crew_acknowledged',actor=actor())


def test_reopen_has_new_effect_id_and_original_bytes_never_change(db):
    tid=ready(db);sent=[]
    def send(**kw):
        sent.append(kw);return {'id':kw['message_id'],'delivery_status':'transport_sent'}
    T.drain_outbox(db,send,guard=fence)
    for state in ('crew_acknowledged','assigned','resolved'):
        T.advance(db,tid,state,actor=actor('Alpha','crew','crew',reply_to=tid))
    T.advance(db,tid,'reopened',actor=actor())
    T.drain_outbox(db,send,guard=fence)
    assert sent[0]['message_id']==tid and sent[1]['message_id'].startswith(tid+':')
    assert json.loads(sent[1]['body'])['ticket_id']==tid
    assert json.loads(sent[0]['body'])['state']=='queued'
    assert json.loads(sent[1]['body'])['state']=='reopened'


def test_ack_during_send_is_not_overwritten_by_transport_mark(db):
    tid=ready(db)
    def send(**kw):
        T.advance(db,tid,'crew_acknowledged',actor=actor('Alpha','crew','crew',reply_to=tid))
        return {'id':kw['message_id'],'delivery_status':'transport_sent'}
    T.drain_outbox(db,send,guard=fence)
    assert T.get_ticket(db,tid)['state']=='crew_acknowledged'


def test_stale_generation_cannot_create_advance_or_mark(db):
    def stale():raise PermissionError('stale_generation')
    with pytest.raises(PermissionError):file(db,guard=stale)
    assert db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]==0
    tid=ready(db)
    with pytest.raises(PermissionError):T.advance(db,tid,'crew_acknowledged',actor=actor('Alpha','crew','crew',stale,reply_to=tid))
    assert T.get_ticket(db,tid)['state']=='queued'


@pytest.mark.parametrize('impact',[float('nan'),float('inf'),True,'123'])
def test_invalid_impact_is_refused(db,impact):
    with pytest.raises((TypeError,ValueError)):file(db,expected_impact=impact)


def test_fence_lost_after_delivery_cannot_mark_effect(db):
    tid=ready(db);valid=[True];seen={}
    def guard():
        if not valid[0]:raise PermissionError('stale_generation')
    def send(**kw):
        seen.setdefault(kw['message_id'],kw['body'])
        valid[0]=False
        return {'id':kw['message_id'],'delivery_status':'transport_sent'}
    with pytest.raises(PermissionError):T.drain_outbox(db,send,guard=guard)
    assert T.get_ticket(db,tid)['effects'][0]['sent'] is False
    valid[0]=True
    T.drain_outbox(db,lambda **kw:{'id':kw['message_id'],'delivery_status':'transport_sent'},guard=guard)
    assert len(seen)==1 and T.get_ticket(db,tid)['state']=='queued'


def test_fake_transport_ack_is_not_crew_authority(db):
    tid=ready(db)
    result=T.drain_outbox(db,lambda **kw:{'id':kw['message_id'],'delivery_status':'crew_acknowledged'},guard=fence)
    assert result[0]['error']=='invalid_transport_reply'
    assert T.get_ticket(db,tid)['state']=='queued' and T.get_ticket(db,tid)['outbox_pending'] is True


def test_delayed_duplicate_ack_never_rolls_back_assignment_or_acknowledges_reopen(db):
    tid=ready(db);crew=actor('Alpha','crew','crew',reply_to=tid)
    for state in ('crew_acknowledged','assigned'):T.advance(db,tid,state,actor=crew)
    T.advance(db,tid,'crew_acknowledged',actor=crew)
    assert T.get_ticket(db,tid)['state']=='assigned'
    T.advance(db,tid,'resolved',actor=crew)
    for state in ('reopened','head_approved','queued'):T.advance(db,tid,state,actor=actor())
    T.advance(db,tid,'crew_acknowledged',actor=crew)
    assert T.get_ticket(db,tid)['state']=='queued'
    fresh=T.get_ticket(db,tid)['effects'][-1]['message_id']
    T.advance(db,tid,'crew_acknowledged',actor=actor('Alpha','crew','crew',reply_to=fresh))
    assert T.get_ticket(db,tid)['state']=='crew_acknowledged'
