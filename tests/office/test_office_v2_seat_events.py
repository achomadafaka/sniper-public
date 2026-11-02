import sqlite3

import pytest

from sniper.office import seat_events as E,store as S,work as W

INSERT='INSERT INTO office_v2_events(event_id,epoch_id,actor_id,generation,incarnation,corrected_at,kind,state,text) VALUES(?,?,?,?,?,?,?,?,?)'


def _conn():
    conn=sqlite3.connect(':memory:');E.ensure_schema(conn);E.ensure_schema(conn);return conn


def test_constants_match_the_checks():
    conn=_conn()
    for i,kind in enumerate(E.KINDS):conn.execute(INSERT,('e%d'%i,'ep','a',1,1,1.,kind,E.STATES[0],None))


@pytest.mark.parametrize('kind,state,text',[('bad','known','x'),('thought','bad','x'),('thought','known','x'*4097),('thought','known','é'*2049)])
def test_refuses_bad_rows(kind,state,text):
    with pytest.raises(sqlite3.IntegrityError):_conn().execute(INSERT,('e','ep','a',1,1,1.,kind,state,text))


def test_duplicate_event_id_refused():
    conn=_conn();row=('e','ep','a',1,1,None,'thought','unknown',None);conn.execute(INSERT,row)
    with pytest.raises(sqlite3.IntegrityError):conn.execute(INSERT,row)


def _dm_conn():
    conn=sqlite3.connect(':memory:',isolation_level=None);W.ensure_schema(conn);E.ensure_schema(conn)
    conn.execute("INSERT INTO office_v2_roster VALUES('head','ep','head',2,3,0,1.)")
    conn.execute("INSERT INTO office_v2_roster VALUES('gone','ep','judge',1,1,1,1.)")
    return conn


def _dm(conn,**kw):
    return E.operator_dm(conn,**{**dict(to_actor_id='head',client_id='client-0001',text='hello',now=5.),**kw})


def test_operator_dm_binds_sender_generation_and_incarnation():
    conn=_dm_conn();out=_dm(conn)
    assert out['state']=='sent'
    row=conn.execute('SELECT from_actor_id,from_generation,to_generation FROM office_dm_events').fetchone()
    assert row==('operator',0,2)
    assert conn.execute('SELECT epoch_id,from_incarnation,to_incarnation FROM office_v2_mail_bind').fetchone()==('ep',None,3)
    assert conn.execute('SELECT count(*) FROM office_dm_pending').fetchone()[0]==1


def test_operator_dm_retry_is_one_row_and_changed_body_conflicts():
    conn=_dm_conn();first=_dm(conn);again=_dm(conn)
    assert again==dict(state='duplicate',message_id=first['message_id'])
    assert conn.execute('SELECT count(*) FROM office_dm_events').fetchone()[0]==1
    with pytest.raises(E.OperatorDmRefused,match='client_id_conflict'):_dm(conn,text='other')


@pytest.mark.parametrize('kw,code',[(dict(to_actor_id='nobody'),'unknown_actor'),(dict(to_actor_id='gone'),'actor_sealed'),(dict(text=' '),'text_invalid'),
                                    (dict(text='x'*4097),'text_invalid'),(dict(client_id='a b'),'client_id_invalid')])
def test_operator_dm_refusals_leave_no_row(kw,code):
    conn=_dm_conn()
    with pytest.raises(E.OperatorDmRefused,match=code):_dm(conn,**kw)
    assert conn.execute('SELECT count(*) FROM office_dm_events').fetchone()[0]==0


def test_operator_dm_store_failure_is_typed():
    conn=_dm_conn();conn.execute('DROP TABLE office_v2_mail_bind')
    with pytest.raises(E.OperatorDmRefused,match='store_unavailable'):_dm(conn)
    assert conn.execute('SELECT count(*) FROM office_dm_events').fetchone()[0]==0


def test_crew_mail_reads_as_crew_never_as_the_operator(tmp_path):
    conn=S.open_store(tmp_path/'o.db');E.ensure_schema(conn)
    conn.execute("INSERT INTO office_v2_roster VALUES('j','ep','judge',1,1,0,1.)");conn.commit()
    E.crew_dm(conn,role='lambda',to_role='judge',text='hello',now=5.)
    mail=E.read_mail(conn,actor_id='j',generation=1,now=6.)
    assert [m['sender'] for m in mail]==['crew:lambda'] and mail[0]['text'].startswith('CREW lambda (crew, not the operator):')
    assert E.is_host_sender('crew:lambda') and not E.is_host_sender('crew:mallory')
    for kw,code in ((dict(role='operator',text='x'),'crew_role_unknown'),(dict(role='lambda',text='x'*4096),'text_invalid'),(dict(role='lambda',text='x',to_role='nobody'),'unknown_actor')):
        with pytest.raises(E.OperatorDmRefused) as refused:E.crew_dm(conn,**{**dict(to_role='judge',now=7.),**kw})
        assert refused.value.code==code
    assert conn.execute('SELECT count(*) FROM office_dm_events').fetchone()[0]==1
