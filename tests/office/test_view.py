import json
from flask import Flask
from sniper.office import store
from sniper.office.report import read_state
from sniper.routes.api_office import create_api_office_blueprint


def test_missing_store_is_unknown_and_get_creates_nothing(tmp_path):
    app = Flask(__name__)
    app.register_blueprint(create_api_office_blueprint(dict(state_root=tmp_path, clock=lambda: 200., stale_after_s=30.)))
    response = app.test_client().get('/api/office/state')
    assert response.status_code == 200
    assert response.json['office']['state'] == 'missing'
    assert not list(tmp_path.iterdir())
    assert 'no-store' in response.headers['Cache-Control']
    assert app.test_client().post('/api/office/state').status_code == 405
    assert app.test_client().get('/office').status_code == 200


def test_stale_agent_and_public_fields_only(tmp_path):
    conn = store.open_store(tmp_path / 'office.db')
    conn.execute('INSERT INTO agents VALUES(?,?)', ('agent', json.dumps(dict(name='<script>unsafe</script>', role='Scout', state='active', system_prompt='PRIVATE_PROMPT'))))
    conn.execute('INSERT INTO heartbeats VALUES(?,?)', ('agent', '{"at":100}'))
    conn.commit()
    conn.close()
    private = store.open_store(tmp_path / 'evaluation.db', evaluation=True)
    store.append(private, 'grades', {'secret': 'PRIVATE_GRADE'})
    private.close()
    state = read_state(tmp_path, now=200., stale_after_s=30.)
    agent = state['agents']['items'][0]
    assert agent['heartbeat_state'] == 'stale' and agent['heartbeat_age_s'] == 100.
    assert 'PRIVATE_' not in json.dumps(state)
    unknown_clock = read_state(tmp_path, now=None, stale_after_s=30.)
    assert unknown_clock['agents']['items'][0]['heartbeat_state'] == 'unknown'


def test_bad_database_is_read_error_not_empty(tmp_path):
    (tmp_path / 'office.db').write_bytes(b'not a database')
    state=read_state(tmp_path,now=200.)
    assert state['office']['state']=='read_error'
    assert state['agents']['state']!='known'


def test_unknown_waits_are_not_reported_as_zero(tmp_path):
    conn=store.open_store(tmp_path/'office.db')
    for identity,payload in [('one',dict(requested_provider='p',requested_model='m',waiting_s=5.,waiting_measured=True)),('two',dict(requested_provider='p',requested_model='m'))]:
        conn.execute('INSERT INTO spend VALUES(?,?)',(identity,json.dumps(payload)))
    conn.commit();conn.close()
    state=read_state(tmp_path,now=200.)
    row=state['models']['items'][0]
    assert row['calls']==2 and row['waits_s'] is None and row['fallbacks'] is None


from office.test_ledger import book, opened, policy, NOW, FX


def test_partial_equity_and_atomic_quantity_remain_honest(book):
    conn,quotes,root=book
    opened(conn,quotes,policy())
    opened(conn,quotes,policy(),order_id='o2',mint='Other111111111111111111111111111111111pump')
    conn.execute("INSERT INTO marks(order_id,at,lamports,source,reason) VALUES('o1',?,?,'ps',NULL)",(NOW+9,'200000000'))
    quantity='100000000000000000000001'
    conn.execute("UPDATE orders SET tokens_atomic=? WHERE order_id='o2'",(quantity,))
    conn.commit()
    state=read_state(root/'office',now=NOW+10,sol_usd=FX,sol_at=NOW)
    assert state['equity']['state']=='partial'
    assert state['equity']['books'][0]['unpriced_count']==1
    assert state['equity']['books'][0]['unpriced_qty']==quantity
    assert next(p for p in state['positions']['items'] if p['order_id']=='o2')['tokens_atomic']==quantity


def test_evaluation_derived_desk_and_messages_are_not_public(tmp_path):
    conn=store.open_store(tmp_path/'office.db')
    conn.execute('INSERT INTO agents VALUES(?,?)',('agent','{"name":"Postmortem","generation":1}'))
    conn.execute('INSERT INTO turn_runs VALUES(?,?)',('turn',json.dumps(dict(actor_id='agent',generation=1,evaluation_derived=True,value=dict(text='PRIVATE_EVAL_RESULT')))))
    conn.execute('INSERT INTO messages VALUES(?,?)',('message',json.dumps(dict(evaluation_derived=True,text='PRIVATE_EVAL_RESULT'))))
    conn.commit();conn.close()
    result=read_state(tmp_path,now=200.)
    assert 'PRIVATE_EVAL_RESULT' not in json.dumps(result)
    assert result['agents']['items'][0]['desk_screen_state']=='sealed'


def test_a_new_generation_does_not_inherit_an_old_heartbeat(tmp_path):
    conn=store.open_store(tmp_path/'office.db')
    conn.execute('INSERT INTO agents VALUES(?,?)',('agent','{"generation":2}'))
    conn.execute('INSERT INTO heartbeats VALUES(?,?)',('agent','{"at":199,"generation":1}'))
    conn.commit();conn.close()
    assert read_state(tmp_path,now=200.,stale_after_s=30.)['agents']['items'][0]['heartbeat_state']=='unknown'
