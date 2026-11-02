import json
import os
import socket
import sqlite3

import pytest

from sniper.office import seat_events as E,seat_tools as T,work as W


@pytest.fixture
def tools(tmp_path,monkeypatch):
    monkeypatch.setattr(T.SeatTools,'_stop_units',lambda self:(self.__dict__.setdefault('stops',[]).append(1),getattr(self,'stop_ok',True))[1])
    db=tmp_path/'o.db'
    conn=sqlite3.connect(db,isolation_level=None);W.ensure_schema(conn);E.ensure_schema(conn)
    conn.execute("INSERT INTO office_v2_roster VALUES('head','ep','head',1,2,0,1.)");conn.execute("INSERT INTO office_v2_roster VALUES('judge','ep','judge',1,1,0,1.)");conn.close()
    t=T.SeatTools(actor='head',epoch='ep',generation=1,incarnation=2,workspace=tmp_path,path=str(tmp_path/'h.sock'),db_path=str(db),clock=lambda:5.,office_uid=os.getuid())
    policy=tmp_path/'search.json';policy.write_text('{"daily_budget":30}')
    t.search_broker=T.S.Broker(tmp_path/'search.db',policy,clock=lambda:5.)
    t.start();yield t;t.stop()


def _rpc(t,lines):
    c=socket.socket(socket.AF_UNIX);c.settimeout(5);c.connect(t.path);f=c.makefile('rwb')
    out=[]
    for m in lines:
        f.write(json.dumps(m).encode()+b'\n');f.flush();out.append(json.loads(f.readline()))
    c.close();return out


def _call(t,name,**args):
    return _rpc(t,[dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name=name,arguments=args))])[0]['result']


def _db(t):return sqlite3.connect(t.db_path)


def test_list_tools(tools):
    out=_rpc(tools,[dict(jsonrpc='2.0',id=1,method='initialize',params={}),dict(jsonrpc='2.0',id=2,method='tools/list')])
    assert {x['name'] for x in out[1]['result']['tools']}=={'wallet_activity','shell','browse','web_search','market_data','chain_wallet_history','chain_transaction','chain_token_holders','chain_funding_trace','notify_operator','db_read','brain_read','brain_note','mail_send','mail_read','sleep','request'}


def test_mail_sender_comes_from_the_socket_not_the_model(tools):
    assert not _call(tools,'mail_send',to='judge',text='hi')['isError']
    assert _call(tools,'mail_send',to='judge',text='hi',from_actor='operator')['isError']
    row=_db(tools).execute('SELECT from_actor_id,from_generation FROM office_dm_events').fetchone()
    assert row==('head',1)
    assert _db(tools).execute('SELECT epoch_id,from_incarnation,to_incarnation FROM office_v2_mail_bind').fetchone()==('ep',2,1)


def test_read_mail_is_once(tools):
    conn=_db(tools);E.operator_dm(conn,to_actor_id='head',client_id='client-0001',text='hello',now=1.);conn.close()
    first=json.loads(_call(tools,'mail_read')['content'][0]['text'])
    assert [(m['sender'],m['text']) for m in first]==[('operator','hello')]
    assert json.loads(_call(tools,'mail_read')['content'][0]['text'])==[]


def test_unknown_tool_and_bad_args_are_typed(tools):
    assert _call(tools,'read_env')['content'][0]['text']=='unknown_tool'
    assert _call(tools,'sleep',until_s='x',reason='r')['isError']


def test_wrong_peer_gets_nothing(tools):
    tools.office_uid=os.getuid()+1
    c=socket.socket(socket.AF_UNIX);c.settimeout(5);c.connect(tools.path);c.sendall(b'{"id":1,"method":"tools/list"}\n')
    try:assert c.recv(10)==b''
    except ConnectionError:pass


def test_stop_ends_the_incarnation(tools):
    open_client=socket.socket(socket.AF_UNIX);open_client.settimeout(5);open_client.connect(tools.path)
    open_client.sendall(json.dumps(dict(jsonrpc='2.0',id=1,method='tools/list')).encode()+b'\n');assert open_client.recv(4096)
    path=tools.path;tools.stop()
    late=socket.socket(socket.AF_UNIX)
    with pytest.raises(OSError):late.connect(path)
    try:
        open_client.sendall(json.dumps(dict(jsonrpc='2.0',id=2,method='tools/list')).encode()+b'\n')
        assert open_client.recv(4096)==b''
    except ConnectionError:pass   # cut before the send or during the read: both are the refusal


def test_long_output_is_stored_truncated(tools):
    conn=_db(tools);E.record_event(conn,epoch_id='e',actor_id='a',generation=1,incarnation=1,at=1.,kind='tool_result',text='é'*5000)
    assert conn.execute('SELECT truncated FROM office_v2_events').fetchone()==(1,)


def test_a_closed_incarnation_refuses_calls_and_stops_units_twice(tools):
    tools.stop()
    assert tools.call('mail_read',{})['content'][0]['text']=='incarnation_closed'
    assert len(tools.stops)==2


def test_stop_waits_for_a_call_in_flight(tools):
    import threading,time
    release=threading.Event();seen=[]
    tools._mail_read=lambda:(release.wait(5),T._result('x'))[1]
    worker=threading.Thread(target=lambda:seen.append(tools.call('mail_read',{})));worker.start()
    time.sleep(.1)
    stopper=threading.Thread(target=tools.stop);stopper.start();time.sleep(.1)
    assert stopper.is_alive() and len(tools.stops)==1
    release.set();stopper.join(5);worker.join(5)
    assert len(tools.stops)==2 and seen


def test_failed_cleanup_reads_not_clean(tools):
    tools.stop_ok=False
    assert tools.stop() is False and tools.clean is False


def test_stop_timeout_is_not_clean(monkeypatch):
    def boom(*a,**k):raise T.subprocess.TimeoutExpired('x',1)
    monkeypatch.setattr(T.subprocess,'run',boom)
    assert T.stop_tool_units('head',995) is False


@pytest.mark.parametrize('fd',[1,2])
def test_shell_stops_output_before_the_producer_finishes(tools,monkeypatch,tmp_path,fd):
    import sys
    marker=tmp_path/'finished'
    code='import os,pathlib; os.write(%d,b"x"*1000000); pathlib.Path(%r).touch()'%(fd,str(marker))
    monkeypatch.setattr(T.B,'unit_args',lambda *a,**k:[sys.executable,'-c',code])
    monkeypatch.setattr(T.B,'tool_args',lambda *a,**k:[])
    out=tools._shell('ignored')
    assert out['isError'] and out['content'][0]['text']=='output_limit'
    assert not marker.exists()


@pytest.mark.parametrize('state,rc,ok',[('inactive',0,True),('failed',0,True),('active',0,False),('deactivating',0,False),('',1,False)])
def test_stop_unit_needs_proof(monkeypatch,state,rc,ok):
    monkeypatch.setattr(T.subprocess,'run',lambda argv,**k:type('R',(),{'returncode':rc if 'show' in argv else 5,'stdout':state})())
    assert T.stop_unit('u',995) is ok


@pytest.mark.parametrize('complete',[False,True])
def test_oversized_rpc_line_closes_before_dispatch(tools,complete):
    msg=json.dumps(dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name='request',arguments=dict(kind='x',detail='x'*70000,why='x')))).encode()
    client=socket.socket(socket.AF_UNIX);client.settimeout(1);client.connect(tools.path)
    try:
        try:client.sendall(msg+(b'\n' if complete else b''))
        except ConnectionError:pass
        try:assert client.recv(4096)==b''
        except ConnectionError:pass
    finally:client.close()
    assert _db(tools).execute('SELECT count(*) FROM office_v2_events').fetchone()==(0,)


def test_listener_loop_starting_after_stop_exits_cleanly(tools):
    tools.stop()
    tools._serve()


@pytest.mark.parametrize('url',['http://a.test/','file:///etc/passwd','notaurl','chrome://settings'])
def test_browse_refuses_anything_but_https(tools,url):
    assert _call(tools,'browse',url=url)['isError']


def test_browse_records_host_and_path_only_and_quotes_the_url(tools,monkeypatch):
    seen=[];monkeypatch.setattr(tools,'_shell',lambda c,timeout_s:(seen.append(c),T._result('{}'))[1])
    _call(tools,'browse',url="https://a.test/p?secret=1';id;'")
    row=_db(tools).execute("SELECT text,web_tainted FROM office_v2_events WHERE kind='browser_visit'").fetchone()
    assert row==('a.test/p',1) and "'\"'\"'" in seen[0]


def test_web_search_quotes_and_bounds_the_query(tools,monkeypatch):
    seen=[];monkeypatch.setattr(tools,'_shell',lambda c,timeout_s:(seen.append(c),T._result('{}'))[1])
    _call(tools,'web_search',query="a';id;'")
    assert "'\"'\"'" in seen[0]
    assert _call(tools,'web_search',query='x'*401)['isError'] and _call(tools,'web_search',query=' ')['isError']


def test_search_page_shapes():
    from sniper.office import seat_search as S
    page='<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.test%2Fp&rut=x">Ti<b>tle</b></a><a class="result__snippet" href="x">Sni  ppet</a>'
    assert S.parse(page)==dict(state='known',results=[dict(title='Title',url='https://a.test/p',snippet='Sni ppet')])
    assert S.parse('<div class="no-results">No results.</div>')==dict(state='known',results=[])
    assert S.parse('<html>please verify you are human</html>')['state']=='unknown'


def test_brain_tools_refuse_without_config_and_on_bad_text(tools):
    assert _call(tools,'brain_read',query='x')['isError']
    tools.brain_env='/nonexistent'
    assert _call(tools,'brain_note',text=' ')['isError']


def test_bing_page_shape_and_link_unwrap():
    import base64
    from sniper.office import seat_search as S
    wrapped=base64.urlsafe_b64encode(b'https://a.test/p').decode().rstrip('=')
    page='<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&&p=x&u=a1%s&ntb=1">Ti<b>tle</b></a></h2><p>Sni  ppet</p></li>'%wrapped
    assert S.parse_bing(page)==dict(state='known',results=[dict(title='Title',url='https://a.test/p',snippet='Sni ppet')])
    assert S.parse_bing('<html>challenge</html>')['state']=='unknown'


def test_search_falls_through_a_challenged_engine_and_says_so(monkeypatch):
    from sniper.office import seat_search as S
    pages={S.TAVILY:'not json',S.URL:'<html>Select all squares containing a duck</html>','https://www.bing.com/search?q=':'<li class="b_algo"><h2><a href="https://a.test/">T</a></h2></li>'}
    monkeypatch.setattr(S,'_fetch',lambda prefix,q:pages[prefix])
    out=S.search('q')
    assert out['state']=='known' and out['engine']=='bing' and out['engine_prior']=='unrelated_on_two_controls' and out['failed']=={'tavily':'result_shape_unknown','duckduckgo':'result_markup_absent'}
    pages['https://www.bing.com/search?q=']='<html>no</html>'
    assert S.search('q')['state']=='unknown'


def test_tavily_shape_and_empty_is_known():
    from sniper.office import seat_search as S
    assert S.parse_tavily('{"results":[{"title":"T","url":"https://a.test/","content":"C  D"},{"url":"ftp://x"}]}')==dict(state='known',results=[dict(title='T',url='https://a.test/',snippet='C D')],dropped_malformed=1)
    assert S.parse_tavily('{"results":[]}')==dict(state='known',results=[])
    assert S.parse_tavily('{"error":"limit"}')['state']=='unknown'


@pytest.mark.parametrize('body',['{"results":[{}]}','{"results":[null,{},{"url":null}]}','{"results":[{"url":"http-not-a-url"}]}','{"results":[{"url":"https://"}]}'])
def test_tavily_unusable_rows_are_not_no_hits(body):
    from sniper.office import seat_search as S
    assert S.parse_tavily(body)['state']=='unknown'


def test_tavily_mixed_rows_keep_the_good_and_count_the_bad():
    from sniper.office import seat_search as S
    out=S.parse_tavily('{"results":[{"url":"https://a.test/"},{}]}')
    assert out['state']=='known' and len(out['results'])==1 and out['dropped_malformed']==1


def test_no_paper_service_means_no_order_tools(tools):
    names={x['name'] for x in _rpc(tools,[dict(jsonrpc='2.0',id=1,method='tools/list')])[0]['result']['tools']}
    assert not names&T.PAPER_TOOLS and {'feed_list','sheet_read'}<=T.PAPER_TOOLS
    assert _call(tools,'paper_order',ticket_id='t',reason='r',thesis='t',disproof='d',exit_plan='e')['content'][0]['text']=='unknown_tool'


def test_paper_tools_pass_host_identity_and_sticky_labels(tools,monkeypatch):
    seen={}
    class Paper:
        def enter(self,**k):seen.update(k);return dict(state='submitted')
    tools.paper=Paper();monkeypatch.setattr(tools,'_shell',lambda c,timeout_s:T._result('{}'))
    tools._browse('https://a.test/')
    conn=_db(tools);E.operator_dm(conn,to_actor_id='head',client_id='client-0009',text='hi',now=1.);conn.close();_call(tools,'mail_read')
    out=_call(tools,'paper_order',ticket_id='t',reason='r',thesis='t',disproof='d',exit_plan='e')
    assert not out['isError'] and seen['actor_id']=='head' and seen['incarnation']==2 and seen['labels']['tainted_by_web'] is True and seen['labels']['operator_influenced'] is True


def test_forged_identity_argument_is_refused(tools):
    class Paper:
        def enter(self,**k):raise AssertionError('must not be reached')
    tools.paper=Paper()
    assert _call(tools,'paper_order',ticket_id='t',reason='r',thesis='t',disproof='d',exit_plan='e',actor_id='forged')['isError']


def test_taint_is_saved_before_the_web_tool_runs(tools,monkeypatch):
    order=[];tools.on_taint=lambda:order.append('saved')
    monkeypatch.setattr(tools,'_shell',lambda c,timeout_s:(order.append('ran'),T._result('{"state":"known","results":[]}'))[1])
    tools._web_search('q')
    assert order==['saved','ran'] and tools.tainted is True
    tools._web_search('q');assert order==['saved','ran']


def test_a_failed_taint_save_fences_the_tool_and_retries(tools,monkeypatch):
    ran=[];monkeypatch.setattr(tools,'_shell',lambda c,timeout_s:(ran.append(1),T._result('{"state":"known","results":[]}'))[1])
    calls=[]
    def save():
        calls.append(1)
        if len(calls)==1:raise OSError()
    tools.on_taint=save
    assert _call(tools,'web_search',query='q')['content'][0]['text']=='taint_save_failed' and not ran and tools.tainted is False
    assert not _call(tools,'web_search',query='q')['isError'] and ran and tools.tainted is True and len(calls)==2


def test_a_long_feed_says_what_it_dropped(tools):
    class Paper:
        def curated_feed(self,**k):return dict(state='known',items=[dict(id=i,pad='x'*500) for i in range(100)])
    tools.paper=Paper()
    out=json.loads(_call(tools,'feed_list')['content'][0]['text'])
    assert out['state']=='partial' and out['dropped']+len(out['items'])==100 and out['dropped']>0


def test_order_labels_carry_what_the_wake_read(tools,monkeypatch):
    seen={}
    class Paper:
        def enter(self,**k):seen.update(k);return dict(state='submitted')
    tools.paper=Paper();tools.wake_labels=dict(post_swap_first_wake=True,workspace_manifest_hash='w')
    tools.desk_db=None;tools.db_hashes.append('h');tools.brain_fact_ids.append('f')
    _call(tools,'paper_order',ticket_id='t',reason='r',thesis='t',disproof='d',exit_plan='e')
    l=seen['labels']
    assert l['post_swap_first_wake'] is True and l['workspace_manifest_hash']=='w' and l['db_read_hashes']==['h'] and l['brain_fact_ids']==['f'] and l['equity_snapshot_id']=='absent'


def test_book_view_records_the_snapshot_on_the_next_order(tools):
    seen={}
    class Paper:
        def book_view(self,**k):return dict(equity=dict(state='known'),equity_snapshot_id='snap1')
        def enter(self,**k):seen.update(k);return dict(state='submitted')
    tools.paper=Paper()
    assert not _call(tools,'book_view')['isError']
    _call(tools,'paper_order',ticket_id='t',reason='r',thesis='t',disproof='d',exit_plan='e')
    assert seen['labels']['equity_snapshot_id']=='snap1'


def test_search_broker_is_used_for_real_tool_calls(tools,monkeypatch):
    calls=[]
    monkeypatch.setattr(tools,'_shell',lambda *a,**k:(calls.append(a),T._result('{"state":"known","results":[]}'))[1])
    first=json.loads(_call(tools,'web_search',query='exact')['content'][0]['text'])
    second=json.loads(_call(tools,'web_search',query='exact')['content'][0]['text'])
    assert not first['cached'] and second['cached'] and len(calls)==1 and second['age_s']==0
    monkeypatch.setattr(tools,'_shell',lambda *a,**k:T._result('{"state":"unknown","reason":"budget_exhausted"}'))
    out=_call(tools,'web_search',query='different')
    assert out['isError'] and json.loads(out['content'][0]['text'])['reason']=='budget_exhausted'


def test_cached_search_taints_before_lookup_without_running_shell(tools,tmp_path,monkeypatch):
    tools.search_broker.search('cached',lambda:dict(state='known',results=[]))
    durable=tmp_path/'durable-taint.json'
    tools.on_taint=lambda:durable.write_text('{"tainted_by_web":true}')
    lookup=tools.search_broker.search
    def checked(query,fetch):
        assert json.loads(durable.read_text())['tainted_by_web'] and tools.tainted
        return lookup(query,fetch)
    monkeypatch.setattr(tools.search_broker,'search',checked)
    monkeypatch.setattr(tools,'_shell',lambda *a,**k:pytest.fail('cache hit ran shell'))
    out=tools._web_search('cached')
    assert not out['isError'] and json.loads(out['content'][0]['text'])['cached']
    assert tools._labels()['tainted_by_web']


def test_failed_durable_taint_fences_even_a_cache_hit(tools,monkeypatch):
    tools.search_broker.search('cached',lambda:dict(state='known',results=[]))
    def fail():raise OSError('disk unavailable')
    tools.on_taint=fail
    monkeypatch.setattr(tools.search_broker,'search',lambda *a:pytest.fail('lookup before durable taint'))
    monkeypatch.setattr(tools,'_shell',lambda *a,**k:pytest.fail('shell before durable taint'))
    with pytest.raises(ValueError,match='taint_save_failed'):tools._web_search('cached')
    assert not tools.tainted


def test_a_real_sized_sheet_reaches_the_seat_and_a_huge_one_says_so(tools):
    sizes=[22000]
    class Paper:
        def read_sheet(self,**k):return dict(state='known',sheet=dict(pad='x'*sizes[0]),lenses={})
    tools.paper=Paper()
    assert json.loads(_call(tools,'sheet_read',ticket_id='t')['content'][0]['text'])['state']=='known'
    sizes[0]=60000
    assert json.loads(_call(tools,'sheet_read',ticket_id='t')['content'][0]['text'])['reason']=='sheet_too_large'


def test_mail_reaches_a_seat_by_role_and_an_ambiguous_role_refuses(tools):
    conn=_db(tools);conn.execute("INSERT INTO office_v2_roster VALUES('ag_s','ep','scout',1,1,0,1.)");conn.commit()
    assert not _call(tools,'mail_send',to='Scout',text='hi')['isError']
    assert _db(tools).execute('SELECT to_actor_id FROM office_dm_events').fetchone()==('ag_s',)
    conn.execute("INSERT INTO office_v2_roster VALUES('ag_t','ep','scout',1,1,0,1.)");conn.commit()
    assert _call(tools,'mail_send',to='scout',text='hi')['isError']
    assert not _call(tools,'mail_send',to='ag_t',text='hi')['isError']


def test_a_seat_replies_to_the_operator_without_waking_anyone(tools):
    assert not _call(tools,'mail_send',to='Operator',text='ack')['isError']
    conn=_db(tools)
    assert conn.execute("SELECT from_actor_id,to_actor_id,text FROM office_dm_events").fetchone()==('head','operator','ack')
    assert conn.execute('SELECT count(*) FROM office_dm_pending').fetchone()==(0,)
    assert _call(tools,'mail_send',to='operator',text='x',from_actor='operator')['isError']


def test_operator_influence_carries_one_wake_and_crew_mail_never_sets_it(tools):
    saved=[];tools.on_operator_read=lambda:saved.append(1)
    conn=_db(tools);E.seat_dm(conn,from_actor='crew',from_generation=0,from_incarnation=0,to_actor_id='head',text='note',now=1.);conn.close()
    _call(tools,'mail_read')
    assert tools._labels()['operator_influenced'] is False and saved==[]
    tools.operator_prior=True
    assert tools._labels()['operator_influenced'] is True
    tools.operator_prior=False
    conn=_db(tools);E.operator_dm(conn,to_actor_id='head',client_id='client-0011',text='hi',now=1.);conn.close()
    _call(tools,'mail_read')
    assert saved==[1] and tools._labels()['operator_influenced'] is True


def test_a_stale_seat_identity_cannot_reply_to_the_operator(tools):
    conn=_db(tools)
    with pytest.raises(E.OperatorDmRefused):
        E.seat_to_operator(conn,from_actor='head',from_generation=1,from_incarnation=1,text='old',now=1.)
    assert conn.execute('SELECT count(*) FROM office_dm_events').fetchone()==(0,)


def test_the_operator_read_saves_on_the_batch_read_and_a_failed_save_keeps_the_mail(tools):
    conn=_db(tools);E.operator_dm(conn,to_actor_id='head',client_id='client-0021',text='hi',now=1.);conn.close()
    def fail():raise OSError('disk')
    tools.on_operator_read=fail
    assert _call(tools,'mail_read')['isError'] and tools.operator_mail is False
    assert _db(tools).execute('SELECT count(*) FROM office_dm_pending').fetchone()==(1,)
    saved=[];tools.on_operator_read=lambda:saved.append(_db(tools).execute('SELECT count(*) FROM office_dm_pending').fetchone()[0])
    assert 'hi' in _call(tools,'mail_read')['content'][0]['text'] and saved==[1]


def test_notify_pages_once_an_hour_and_always_reaches_the_inbox(tools):
    pages=[];tools.page_operator=pages.append
    cases=iter('ab')
    def notify():return json.loads(_call(tools,'notify_operator',case=next(cases),evidence='e\x07',risks='r')['content'][0]['text'])
    assert notify()['paged'] is True and notify()['delivery']=='queued_in_inbox' and len(pages)==1
    rows=_db(tools).execute("SELECT to_actor_id,refs_json,text FROM office_dm_events ORDER BY seq").fetchall()
    assert [r[0] for r in rows]==['operator','operator'] and 'notify_operator' in rows[0][1] and '\x07' not in rows[0][2]
    log=_db(tools).execute('SELECT state,pages_this_hour,length(case_sha256) FROM office_v2_notifications ORDER BY sent_at,rowid').fetchall()
    assert log==[('telegram_delivered',0,64),('queued_in_inbox',1,64)]
    assert 'unverified' in pages[0]


def test_a_failed_page_keeps_the_claim_and_can_page_again(tools):
    def fail(t):raise ValueError('x')
    tools.page_operator=fail
    assert json.loads(_call(tools,'notify_operator',case='c',evidence='e',risks='r')['content'][0]['text'])['delivery']=='telegram_failed'
    tools.page_operator=lambda t:None
    assert json.loads(_call(tools,'notify_operator',case='d',evidence='e',risks='r')['content'][0]['text'])['paged'] is True


def test_the_same_case_within_a_day_is_one_row_counted_and_short_ids_are_counted_dropped(tools):
    tools.page_operator=lambda t:None
    first=json.loads(_call(tools,'notify_operator',case='c',evidence='order 39e0d65b '+'a'*64,risks='r')['content'][0]['text'])
    again=json.loads(_call(tools,'notify_operator',case='c',evidence='x',risks='r')['content'][0]['text'])
    assert again['message_id']==first['message_id'] and again['paged'] is False
    conn=_db(tools);assert conn.execute("SELECT count(*) FROM office_dm_events WHERE to_actor_id='operator'").fetchone()==(1,)
    assert conn.execute('SELECT repeat_count,evidence_ids_dropped,evidence_ids_json FROM office_v2_notifications').fetchone()==(1,1,json.dumps(['a'*64]))


def test_two_threads_take_one_page_slot_and_a_crash_after_reserving_holds_it(tools):
    import threading
    pages=[];start=threading.Barrier(2)
    tools.page_operator=pages.append
    def go(case):
        start.wait();_call(tools,'notify_operator',case=case,evidence='e',risks='r')
    threads=[threading.Thread(target=go,args=(c,)) for c in 'xy']
    [t.start() for t in threads];[t.join() for t in threads]
    assert len(pages)==1
    conn=_db(tools);conn.execute("UPDATE office_v2_notifications SET state='page_reserved'");conn.commit()
    assert json.loads(_call(tools,'notify_operator',case='z',evidence='e',risks='r')['content'][0]['text'])['delivery']=='queued_in_inbox' and len(pages)==1


def test_multibyte_text_stays_inside_the_mail_limit(tools):
    tools.page_operator=lambda t:None
    assert not _call(tools,'notify_operator',case='é'*1200,evidence='é'*1200,risks='é'*1200)['isError']


def test_added_seats_have_no_order_tools_and_their_denials_hold(tools):
    assert 'paper_order' not in {x['name'] for x in _rpc(tools,[dict(jsonrpc='2.0',id=2,method='tools/list')])[0]['result']['tools']}
    assert _call(tools,'paper_order',ticket_id='t',reason='r',thesis='t',disproof='d',exit_plan='e')['isError']
    tools.denied=frozenset({'chain_funding_trace'})
    assert _call(tools,'chain_funding_trace',wallet='w')['content'][0]['text']=='unknown_tool'


def test_added_seat_configs_cannot_reach_a_book():
    import json
    for role in ('researcher','forensics'):
        cfg=json.load(open('config/office/seat_%s_v2.json'%role))
        assert 'ledger_db' not in cfg and cfg['model']==dict(researcher='claude-haiku-5-5',forensics='gpt-6.1-sol')[role] and cfg['role']==role and not cfg['account_poller'] and role in cfg['workspace']
    assert json.load(open('config/office/seat_researcher_v2.json'))['denied_tools']==[]   # operator order 2026-10-30: no seat has tools disabled


def test_a_lost_page_response_keeps_the_slot(tools):
    sends=[]
    def lost(text):
        sends.append(1);raise OSError('response lost')
    tools.page_operator=lost
    for case in 'ab':_call(tools,'notify_operator',case=case,evidence='e',risks='r')
    assert len(sends)==1
    assert [r[0] for r in _db(tools).execute('SELECT state FROM office_v2_notifications ORDER BY rowid')]==['telegram_unknown','queued_in_inbox']


def test_mail_from_a_tainted_seat_labels_the_reader(tools):
    conn=_db(tools)
    E.seat_dm(conn,from_actor='judge',from_generation=1,from_incarnation=1,to_actor_id='head',text='clean',now=1.,sender_tainted=False)
    conn.commit();_call(tools,'mail_read');assert tools._labels()['peer_tainted_read'] is False
    E.seat_dm(conn,from_actor='judge',from_generation=1,from_incarnation=1,to_actor_id='head',text='web',now=2.,sender_tainted=True)
    conn.commit();_call(tools,'mail_read')
    assert tools._labels()['peer_tainted_read'] is True and tools._labels()['tainted_by_web'] is False


def test_results_view_needs_the_paper_service_and_labels_the_read(tools):
    assert _call(tools,'results_view')['content'][0]['text']=='unknown_tool'
    class Paper:
        def results_view(self,**k):return dict(state='known',results_asof=7.,items=[])
    tools.paper=Paper()
    assert tools._labels()['results_visible_asof']=='none_read' and tools._labels()['results_reads_n']==0
    assert not _call(tools,'results_view')['isError']
    assert tools._labels()['results_visible_asof']==7. and tools._labels()['results_reads_n']==1


def test_a_results_read_is_saved_for_later_wakes_and_a_failed_save_withholds_it(tools):
    class Paper:
        def results_view(self,**k):return dict(state='known',results_asof=7.,items=[])
    tools.paper=Paper();saved=[]
    tools.on_results_read=lambda a,n:saved.append((a,n))
    _call(tools,'results_view');assert saved==[(7.,1)]
    def fail(a,n):raise OSError('disk')
    tools.on_results_read=fail
    assert _call(tools,'results_view')['isError'] and tools.results_reads==1
    tools.results_asof,tools.results_reads=7.,1   # a new wake's tools, from the host state
    assert tools._labels()['results_visible_asof']==7.


def test_ticketless_is_read_only_and_socket_bound_until_host_evidence_exists(tools):
    seen=[]
    class Paper:
        def read_self_sheet(self,**kw):seen.append(kw);return dict(state='unknown',reason='self_host_evidence_unknown')
    tools.paper=Paper()
    assert _call(tools,'self_sheet_read',network_id='solana',mint='M',pool='P')['isError']
    assert seen==[dict(actor_id='head',incarnation=2,network_id='solana',mint='M',pool='P')]
    assert not hasattr(tools,'_self_paper_order')
    assert 'self_paper_order' not in T.TOOLS
    refused=_call(tools,'self_paper_order',network_id='solana',mint='M',reason='x')
    assert refused['isError'] and refused['content'][0]['text']=='unknown_tool'
    assert len(seen)==1


def test_any_seat_can_notify_the_operator():
    import json
    for role in ('researcher','forensics'):assert 'notify_operator' not in json.load(open('config/office/seat_%s_v2.json'%role))['denied_tools']


def test_a_request_pings_alpha_rate_limited_with_a_count_and_no_seat_text(monkeypatch):
    seen=[];clock=[1000.];seat='ag_'+'a'*16
    monkeypatch.setattr(T,'_ping_state',dict(last=None,count=0));monkeypatch.setattr(T.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(T.subprocess,'run',lambda argv,**k:seen.append((argv,open(argv[-1]).read())))
    T._ping_alpha('ag_1;rm -rf');assert seen==[]                       # an odd id cannot shape the message
    T._ping_alpha(seat);assert seen[0][0][1:6]==['send','--to','Codex','--from','Alpha'] and '1 new' in seen[0][1]
    T._ping_alpha(seat);T._ping_alpha(seat);assert len(seen)==1
    clock[0]+=T.REQUEST_PING_MIN_S+1;T._ping_alpha(seat);assert len(seen)==2 and '3 new' in seen[1][1]
    monkeypatch.setenv('OFFICE_REQUEST_NOTIFY','0');clock[0]+=1000;T._ping_alpha(seat);assert len(seen)==2
    def boom(*a,**k):raise OSError('no bridge')
    monkeypatch.delenv('OFFICE_REQUEST_NOTIFY');monkeypatch.setattr(T.subprocess,'run',boom);T._ping_alpha(seat)   # a dead bridge never reaches the tool


def test_order_rails_and_the_exit_tool_reach_the_paper_service(tools):
    seen=[]
    class Paper:
        def enter(self,**k):seen.append(k);return dict(state='submitted')
        def request_exit(self,**k):seen.append(k);return dict(state='submitted')
    tools.paper=Paper()
    assert not _call(tools,'paper_order',ticket_id='t',reason='r',thesis='t',disproof='d',exit_plan='e',trail_pct=15)['isError'] and seen[0]['rails']==dict(trail_pct=15)
    assert 'rails' not in (_call(tools,'paper_order',ticket_id='t',reason='r',thesis='t',disproof='d',exit_plan='e'),seen[-1])[1]
    assert not _call(tools,'paper_exit',order_id='o',reason='why')['isError'] and seen[-1]=={'actor_id':'head','incarnation':2,'order_id':'o','reason':'why'}


def test_db_read_description_names_every_readable_table():
    from sniper.office import seat_db as D
    text=T.TOOLS['db_read'][0]
    missing=[t for t in (*D.ALLOWED,*D.EXTERNAL) if t not in text]
    assert not missing,missing           # a seat can only use tables it is told about


def test_the_claude_harness_stays_without_native_tools():
    """Alpha 2026-10-30: the harness holds a refresh token beside any native tool, so "all tools on" never reaches it."""
    import ast
    tree=ast.parse(open('sniper/office/seat_child.py').read())
    call=next(n for n in ast.walk(tree) if isinstance(n,ast.Call) and getattr(n.func,'id',None)=='ClaudeAgentOptions')
    args={k.arg:ast.literal_eval(k.value) for k in call.keywords if k.arg in ('tools','allowed_tools','setting_sources','strict_mcp_config')}
    assert args=={'tools':[],'allowed_tools':['mcp__office__*'],'setting_sources':[],'strict_mcp_config':True}


def test_a_db_read_that_touches_pass_replay_is_logged_with_its_query_hash(tools,monkeypatch):
    import hashlib
    tools.desk_db=None
    sql='SELECT status, count(*) FROM pass_replay GROUP BY 1'
    _call(tools,'db_read',sql=sql)
    conn=sqlite3.connect(tools.db_path)
    rows=conn.execute("SELECT refs_json FROM office_v2_events WHERE kind='tool_call' AND text='db_read_pass_replay'").fetchall()
    assert len(rows)==0                                                    # no desk db: refused before any query is made, so nothing is counted
    tools.desk_db='/nonexistent/desk.db'
    _call(tools,'db_read',sql=sql);_call(tools,'db_read',sql='SELECT 1 FROM desk_tickets')
    rows=[json.loads(r[0]) for r in conn.execute("SELECT refs_json FROM office_v2_events WHERE kind='tool_call' AND text='db_read_pass_replay'")]
    assert rows==[dict(tool='db_read',table='pass_replay',sql_sha256=hashlib.sha256(sql.encode()).hexdigest())]     # only the replay query, with its hash


def test_a_db_read_naming_web_text_taints_the_wake_and_every_intel_read_is_logged(tools):
    import hashlib
    tools.desk_db=None
    assert tools.tainted is False
    _call(tools,'db_read',sql='SELECT 1 FROM trending_boards')                       # numeric intel table: logged, no taint
    assert tools.tainted is False
    tools.desk_db='/nonexistent/desk.db'
    sql_t='SELECT 1 FROM trending_boards';sql_x='SELECT text FROM x_posts'
    _call(tools,'db_read',sql=sql_t);assert tools.tainted is False
    _call(tools,'db_read',sql=sql_x);assert tools.tainted is True                    # web text taints exactly as a web read does
    conn=sqlite3.connect(tools.db_path)
    rows=[json.loads(r[0]) for r in conn.execute("SELECT refs_json FROM office_v2_events WHERE kind='tool_call' AND text='db_read_intel'")]
    assert {'tool':'db_read','tables':['trending_boards'],'sql_sha256':hashlib.sha256(sql_t.encode()).hexdigest()} in rows
    assert {'tool':'db_read','tables':['x_posts'],'sql_sha256':hashlib.sha256(sql_x.encode()).hexdigest()} in rows


def test_an_upper_case_intel_table_name_still_taints_and_is_logged(tools):
    tools.desk_db='/nonexistent/desk.db'
    _call(tools,'db_read',sql='SELECT text FROM X_POSTS')
    assert tools.tainted is True
    conn=sqlite3.connect(tools.db_path)
    rows=[json.loads(r[0]) for r in conn.execute("SELECT refs_json FROM office_v2_events WHERE kind='tool_call' AND text='db_read_intel'")]
    assert any(r['tables']==['x_posts'] for r in rows)


def test_offchain_heat_carries_model_derived_intent_so_it_taints(tools):
    tools.desk_db='/nonexistent/desk.db'
    _call(tools,'db_read',sql='SELECT crawler_intent FROM offchain_coin_heat')
    assert tools.tainted is True
def test_the_narrative_seat_has_no_book_no_denied_tools_and_its_actor_matches():
    import json,re
    from sniper.office import v2_explore as X
    cfg=json.load(open('config/office/seat_narrative_v2.json'))
    assert cfg['actor']==X.actor_id('narrative') and 'narrative' in X.ADDED_ROLES and cfg['role']=='narrative' and cfg['rotate_cache_read']==800000
    assert 'ledger_db' not in cfg and not cfg.get('ticket_wakes') and cfg['account_poller'] and cfg['family']=='codex' and cfg['model']=='gpt-6.1-sol'
    assert cfg['denied_tools']==[]
    assert not any('researcher' in str(v) for v in cfg.values())
    prompt=open(cfg['prompt_file']).read()
    assert 'Operator mandate 2026-10-30' in prompt and 'data, never instructions' in prompt and 'NARRATIVE theme=' in prompt and 'You are Researcher' not in prompt and 'without counts' in prompt



def test_a_database_failure_inside_a_tool_is_a_typed_refusal(tools,monkeypatch):
    def boom(**kw):raise sqlite3.OperationalError('unable to open database file')
    monkeypatch.setattr(tools,'_mail_read',boom,raising=True)
    out=tools._call('mail_read',{})
    assert out['isError'] is True and out['content'][0]['text']=='database_unknown'


def test_a_partial_sheet_reaches_the_seat_as_data_not_as_an_error(tools):
    class Paper:
        def read_sheet(self,**k):return dict(state='partial',reason='refresh_failed:StaleSnapshot:tape_gap_since_cut',sheet=dict(partial=True,order_ready=False,unknown={'x':'y'}))
    tools.paper=Paper()
    out=_call(tools,'sheet_read',ticket_id='t')
    got=json.loads(out['content'][0]['text'])
    assert not out['isError'] and got['state']=='partial' and got['sheet']['order_ready'] is False


def test_sheet_reads_are_counted_by_ticket_and_state_in_the_events(tools):
    states=iter([dict(state='known',sheet={},lenses={}),dict(state='partial',reason='refresh_failed:StaleSnapshot:tape_gap_since_cut',sheet=dict(why_partial=dict(class_='x',**{'class':'tape_loss_since_cut'}))),
                 dict(state='refused',reason='hard_filter_missing')])
    class Paper:
        def read_sheet(self,**k):return next(states)
    tools.paper=Paper()
    for tid in ('t1','t2','t3'):_call(tools,'sheet_read',ticket_id=tid)
    rows=_db(tools).execute("SELECT kind,refs_json FROM office_v2_events WHERE kind IN ('tool_call','tool_result') ORDER BY seq").fetchall()
    calls=[json.loads(r[1]) for r in rows if r[0]=='tool_call'];results=[json.loads(r[1]) for r in rows if r[0]=='tool_result']
    assert [c['ticket_id'] for c in calls]==['t1','t2','t3']
    assert [r['sheet_state'] for r in results]==['known','partial','refused'] and results[1]['sheet_class']=='tape_loss_since_cut' and results[2]['sheet_reason']=='hard_filter_missing'


@pytest.mark.parametrize('text', ['x'*5000, 'é'*3000, 'x'*4096, 'é'*2048], ids=['ascii-over', 'utf8-over', 'ascii-boundary', 'utf8-boundary'])
def test_tool_call_preserves_response_and_marks_audit_byte_truncation(tools, text):
    tools._mail_read=lambda:T._result(text)
    response=tools.call('mail_read', {})
    assert response['content'][0]['text']==text
    with _db(tools) as conn:
        stored,truncated=conn.execute("SELECT text,truncated FROM office_v2_events WHERE kind='tool_result'").fetchone()
    assert stored==text.encode('utf-8')[:4096].decode('utf-8', 'ignore')
    assert truncated==int(len(text.encode('utf-8'))>4096)


def test_paced_and_bad_arguments_tell_the_seat_what_to_do_and_refusals_are_recorded(tools):
    class Market:
        def __init__(self,out):self.out=out
        def read(self,**k):return dict(self.out)
    tools.market=Market(dict(state='unknown',reason='paced'))
    assert json.loads(_call(tools,'market_data',mint='m')['content'][0]['text'])['retry_after_s']==T.market_data.PACE
    tools.market=Market(dict(state='unknown',reason='bad_arguments'))
    assert 'exactly one of mint or pool' in json.loads(_call(tools,'market_data')['content'][0]['text'])['hint']
    class Chain:
        def transaction(self,signature):return dict(state='unknown',reason='paced',tool='chain_transaction')
    tools.chain=Chain()
    assert json.loads(_call(tools,'chain_transaction',signature='s')['content'][0]['text'])['retry_after_s']==T.chain_data.PACE
    out=_call(tools,'sleep',until_s=0,reason='r')
    assert out['isError']
    row=_db(tools).execute("SELECT text,refs_json FROM office_v2_events WHERE kind='tool_result' ORDER BY seq DESC LIMIT 1").fetchone()
    assert row[0]=='bad_arguments' and json.loads(row[1])=={'tool':'sleep','is_error':True}


def test_sleep_cap_is_enforced_and_said(tools):
    tools.max_sleep_s=300
    assert 'host cap' in _call(tools,'sleep',until_s=14400,reason='r')['content'][0]['text']
    assert tools.sleep_until-tools.clock()<=300
    refs=json.loads(_db(tools).execute("SELECT refs_json FROM office_v2_events WHERE text='sleep' ORDER BY seq DESC LIMIT 1").fetchone()[0])
    assert refs['until_s']==300 and refs['asked_s']==14400
    assert json.load(open('config/office/seat_narrative_v2.json'))['max_sleep_s']==300


def test_mail_read_gives_the_host_role_of_a_roster_sender(tools):
    c=_db(tools)
    role=c.execute("SELECT role FROM office_v2_roster WHERE actor_id='head'").fetchone()[0]
    assert not _call(tools,'mail_send',to='head',text='hi')['isError']
    mail=json.loads(_call(tools,'mail_read')['content'][0]['text'])
    assert mail and mail[0]['sender']=='head' and mail[0]['sender_role']==role
