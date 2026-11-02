import pytest
from flask import Flask
from sniper.routes.api_office import create_api_office_blueprint


def app(tmp_path,*,auth=True):
    a=Flask(__name__);a.testing=True;calls=[]
    def send(**kw):calls.append(kw);return dict(state='sent',message_id='m')
    a.register_blueprint(create_api_office_blueprint(dict(state_root=tmp_path,clock=lambda:200.,operator_authenticated=lambda:auth,operator_dm=send)))
    return a.test_client(),calls


@pytest.mark.parametrize('auth,headers,status',[
    (False,{'Origin':'http://localhost'},401),
    (True,{'Origin':'https://evil.invalid'},403),
    (True,{},403),
    (True,{'Origin':'http://localhost','Content-Type':'text/plain'},415),
])
def test_dm_rejects_missing_auth_cross_origin_and_non_json(tmp_path,auth,headers,status):
    c,calls=app(tmp_path,auth=auth)
    r=c.post('/api/office/dm',data='{}',headers=headers)
    assert r.status_code==status and not calls


def test_dm_passes_only_host_owned_sender_and_stable_identity(tmp_path):
    c,calls=app(tmp_path)
    body=dict(to_actor_id='a',client_id='client',text='<script>x</script>')
    r=c.post('/api/office/dm',json=body,headers={'Origin':'http://localhost'})
    assert r.status_code==200 and r.json==dict(state='sent',message_id='m')
    assert calls==[dict(body,now=200.)]
    assert r.headers['Cache-Control']=='no-store'
    r=c.post('/api/office/dm',json=dict(body,sender='operator'),headers={'Origin':'http://localhost'})
    assert r.status_code==400 and len(calls)==1


def test_dm_without_auth_callback_fails_closed(tmp_path):
    a=Flask(__name__);a.register_blueprint(create_api_office_blueprint(dict(state_root=tmp_path,clock=lambda:200.)))
    assert a.test_client().post('/api/office/dm',json={},headers={'Origin':'http://localhost'}).status_code==401


def test_feed_invalid_cursor_and_absent_store(tmp_path):
    c,_=app(tmp_path)
    assert c.get('/api/office/feed?after=bad').status_code==400
    r=c.get('/api/office/feed');assert r.status_code==200 and r.json['state']=='unknown'
    assert r.headers['Cache-Control']=='no-store'


def test_dm_rejects_unpaired_unicode_without_crashing(tmp_path):
    c,calls=app(tmp_path)
    r=c.post('/api/office/dm',json=dict(to_actor_id='a',client_id='c',text='\ud800'),headers={'Origin':'http://localhost'})
    assert r.status_code==400 and not calls


def test_real_host_dm_is_idempotent_and_visible_only_in_bound_feed(tmp_path):
    from sniper.office import store,seat_events
    conn=store.open_store(tmp_path/'office.db');seat_events.ensure_schema(conn)
    conn.execute("INSERT INTO office_v2_roster VALUES('a','ep','Judge',1,1,0,100)")
    conn.commit();conn.close()
    a=Flask(__name__);a.testing=True
    a.register_blueprint(create_api_office_blueprint(dict(state_root=tmp_path,clock=lambda:200.,operator_authenticated=lambda:True)))
    c=a.test_client();body=dict(to_actor_id='a',client_id='client_123',text='Bound operator text')
    for expected in ('sent','duplicate'):
        r=c.post('/api/office/dm',json=body,headers={'Origin':'http://localhost'})
        assert r.status_code==200 and r.json['state']==expected
    feed=c.get('/api/office/feed').json
    assert len(feed['mail']['items'])==1 and feed['mail']['items'][0]['text']==body['text']
    r=c.post('/api/office/dm',json=dict(body,text='different'),headers={'Origin':'http://localhost'})
    assert r.status_code==409 and r.json['error']=='client_id_conflict'


def test_production_auth_callback_uses_real_dashboard_credentials_and_proxy_boundary():
    # Extract only auth code: importing server.py would start unrelated funded observers.
    import ast
    import base64
    import hmac
    import time
    from pathlib import Path
    from flask import request
    tree=ast.parse((Path(__file__).resolve().parents[2]/'sniper/web/server.py').read_text())
    names={'_is_trusted_gateway_request','_basic_auth_credentials_valid','_dash_session_token','_dash_session_valid'}
    module=ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[])
    env=dict(request=request,hmac=hmac,_hmac=hmac,time=time,_DASH_USER='',_DASH_PASS='',_DASH_SESSION_COOKIE='sniper_dash_session',_DASH_SESSION_TTL_S=43200,_TRUSTED_PROXY_AUTH_VALUE='proxy-marker',_TRUSTED_PROXY_AUTH_HEADER='X-Test-Gateway')
    exec(compile(module,'production-auth','exec'),env)
    expression=next(v for n in ast.walk(tree) if isinstance(n,ast.Dict) for k,v in zip(n.keys,n.values) if isinstance(k,ast.Constant) and k.value=='operator_authenticated')
    auth=eval(compile(ast.Expression(expression),'production-auth-callback','eval'),env)
    a=Flask(__name__)
    def basic(value):return {'Authorization':'Basic '+base64.b64encode(value.encode()).decode()}
    with a.test_request_context(headers=basic(':')):assert auth() is False
    env.update(_DASH_USER='desk',_DASH_PASS='test-password')
    with a.test_request_context(headers=basic('desk:test-password')):assert auth() is True
    with a.test_request_context(headers=basic('desk:wrong')):assert auth() is False
    with a.test_request_context(headers={'X-Test-Gateway':'proxy-marker'},environ_base={'REMOTE_ADDR':'10.0.0.1'}):assert auth() is False
    with a.test_request_context(headers={'X-Test-Gateway':'proxy-marker'},environ_base={'REMOTE_ADDR':'127.0.0.1'}):assert auth() is True


def _chat_client(tmp_path,*,parked='hourly_wake_limit'):
    import json,sqlite3
    from flask import Flask
    from sniper.office import seat_events as E,store as S,work as W
    conn=sqlite3.connect(tmp_path/'office.db',isolation_level=None);W.ensure_schema(conn);E.ensure_schema(conn)
    conn.execute("INSERT INTO office_v2_roster VALUES('h','ep','head',1,1,0,1.)");conn.execute("INSERT INTO office_v2_roster VALUES('j','ep','judge',1,1,0,1.)");conn.execute("INSERT INTO office_v2_roster VALUES('old','ep','scout',1,1,1,1.)")
    (tmp_path/'v2').mkdir();(tmp_path/'v2/explore-head.json').write_text(json.dumps(dict(parked_reason=parked)));(tmp_path/'v2/explore-judge.json').write_text(json.dumps(dict(parked_reason=None)))
    a=Flask(__name__);a.register_blueprint(create_api_office_blueprint(dict(state_root=tmp_path,clock=lambda:200.,operator_authenticated=lambda:True)))
    return a.test_client(),conn


def _post(c,**body):
    return c.post('/api/office/dm',json=body,headers=dict(Origin='http://localhost'))


def test_a_dm_says_in_words_whether_the_seat_can_wake(tmp_path):
    c,_=_chat_client(tmp_path)
    assert _post(c,to_actor_id='h',client_id='client-0001',text='hi').get_json()['delivery']=='delivered, seat parked: hourly_wake_limit'
    assert _post(c,to_actor_id='j',client_id='client-0002',text='hi').get_json()['delivery']=='delivered, seat waking'


def test_all_hands_reaches_each_live_seat_once_and_a_retry_adds_nothing(tmp_path):
    c,conn=_chat_client(tmp_path)
    for _ in range(2):
        out=_post(c,to_actor_id='all',client_id='client-0003',text='hi').get_json()
        assert set(out['seats'])=={'h','j'}
    assert conn.execute("SELECT to_actor_id,from_actor_id FROM office_dm_events ORDER BY to_actor_id").fetchall()==[('h','operator'),('j','operator')]
