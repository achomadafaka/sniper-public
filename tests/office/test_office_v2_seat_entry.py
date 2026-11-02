import importlib.util

import pytest

spec=importlib.util.spec_from_file_location('office_seat_host','scripts/office_seat_host.py')
M=importlib.util.module_from_spec(spec);spec.loader.exec_module(M)


class Px:
    deny=frozenset({'old.test'})


def _refresher(monkeypatch,clock):
    p=Px();r=M.DenyRefresher({},[p],clock=lambda:clock[0]);return r,p


def test_new_funded_host_reaches_the_proxy_and_set_only_grows(monkeypatch):
    r,p=_refresher(monkeypatch,[0.])
    monkeypatch.setattr(M,'live_hosts',lambda cfg:frozenset({'new.test'}))
    r.refresh();assert p.deny=={'old.test','new.test'}


def test_unreadable_environment_parks_after_grace_and_recovers(monkeypatch):
    clock=[100.];r,p=_refresher(monkeypatch,clock)
    def boom(cfg):raise OSError()
    monkeypatch.setattr(M,'live_hosts',boom)
    r.refresh();assert r.park_reason() is None
    clock[0]+=r.GRACE_S;r.refresh();assert r.park_reason()=='funded_hosts_unknown' and p.deny=={'old.test'}
    monkeypatch.setattr(M,'live_hosts',lambda cfg:frozenset());r.refresh();assert r.park_reason() is None


@pytest.mark.parametrize('content',['abc','999999999'])
def test_runner_pid_must_be_the_runner(tmp_path,content):
    f=tmp_path/'pid';f.write_text(content)
    with pytest.raises((ValueError,OSError)):M.runner_environ(f)


def test_a_changed_book_policy_file_refuses_to_start():
    import json,sqlite3
    from sniper.office import eval_transition as X
    policy=dict(fill_model_version=X.FILL_MODEL,max_hold_s=3600.)
    conn=sqlite3.connect(':memory:');conn.execute('CREATE TABLE epochs(id TEXT,payload_json TEXT)')
    conn.execute('CREATE TABLE office_v2_changes(epoch_id TEXT,kind TEXT,content_hash TEXT,note TEXT)')
    conn.execute('INSERT INTO epochs VALUES(?,?)',('E',json.dumps(dict(book_policy=X.book_policy_identity(policy)))))
    M.check_policy(conn,'E',policy)
    with pytest.raises(SystemExit,match='explore_book_policy_changed'):M.check_policy(conn,'E',dict(policy,max_hold_s=60.))
    with pytest.raises(SystemExit,match='explore_book_policy_unknown'):M.check_policy(conn,'missing',policy)


def test_policy_edit_parks_and_an_override_with_reference_is_the_way_through(tmp_path):
    import json,sqlite3
    from sniper.office import eval_transition as X
    policy=dict(fill_model_version=X.FILL_MODEL,max_hold_s=3600.);changed=dict(policy,max_hold_s=60.)
    conn=sqlite3.connect(':memory:');conn.execute('CREATE TABLE epochs(id TEXT,payload_json TEXT)')
    conn.execute('CREATE TABLE office_v2_changes(epoch_id TEXT,kind TEXT,content_hash TEXT,note TEXT)')
    conn.execute('INSERT INTO epochs VALUES(?,?)',('E',json.dumps(dict(book_policy=X.book_policy_identity(policy)))))
    file=tmp_path/'p.json';file.write_text(json.dumps(dict(policy=policy)))
    watch=M.policy_watch(dict(epoch='E',book_policy=str(file)),conn)
    assert watch() is None
    file.write_text(json.dumps(dict(policy=changed)));assert watch()=='book_policy_changed'
    conn.execute("INSERT INTO office_v2_changes VALUES('E','override',?,'')",(X.book_policy_identity(changed)['sha256'],));assert watch()=='book_policy_changed'
    conn.execute("INSERT INTO office_v2_changes VALUES('E','override',?,'Alpha 10-30')",(X.book_policy_identity(changed)['sha256'],));assert watch() is None
    file.write_text('{');assert watch()=='book_policy_unknown'


def test_an_override_with_the_old_service_still_running_parks_until_rebuilt(tmp_path):
    import json,sqlite3
    from types import SimpleNamespace
    from sniper.office import eval_transition as X
    policy=dict(fill_model_version=X.FILL_MODEL,max_hold_s=3600.);changed=dict(policy,max_hold_s=60.)
    conn=sqlite3.connect(':memory:');conn.execute('CREATE TABLE epochs(id TEXT,payload_json TEXT)');conn.execute('CREATE TABLE office_v2_changes(epoch_id TEXT,kind TEXT,content_hash TEXT,note TEXT)')
    conn.execute('INSERT INTO epochs VALUES(?,?)',('E',json.dumps(dict(book_policy=X.book_policy_identity(policy)))))
    conn.execute("INSERT INTO office_v2_changes VALUES('E','override',?,'Alpha ref')",(X.book_policy_identity(changed)['sha256'],))
    file=tmp_path/'p.json';file.write_text(json.dumps(dict(policy=changed)))
    cfg=dict(epoch='E',book_policy=str(file),paper=SimpleNamespace(policy_sha256=X.book_policy_identity(policy)['sha256']))
    assert M.policy_watch(cfg,conn)()=='book_policy_restart_needed'
    cfg['paper'].policy_sha256=X.book_policy_identity(changed)['sha256']
    assert M.policy_watch(cfg,conn)() is None


def test_policy_watch_works_off_the_main_thread(tmp_path):
    import json,sqlite3,threading
    from scripts import office_seat_host as H
    db=tmp_path/'o.db';main=sqlite3.connect(db);main.execute('CREATE TABLE t(x)');main.commit()
    policy=tmp_path/'p.json';policy.write_text(json.dumps({'policy':{}}))
    cfg=dict(book_policy=str(policy),office_db=str(db),epoch='e')
    seen={}
    original=H.policy_problem
    H.policy_problem=lambda conn,epoch,p:conn.execute('SELECT count(*) FROM t').fetchone() and None
    try:
        th=threading.Thread(target=lambda:seen.update(r=H.policy_watch(cfg,main)()));th.start();th.join()
    finally:H.policy_problem=original
    assert seen['r'] is None


def test_seats_share_one_account_poll(monkeypatch):
    import sqlite3,time
    from sniper.office import quota_measurement as Q
    calls=[];monkeypatch.setattr(M.subprocess,'run',lambda *a,**k:(calls.append(1),M.subprocess.CompletedProcess(a[0],0,stdout='{"state":"unknown","reason":"unavailable"}\n',stderr=''))[1])
    conn=sqlite3.connect(':memory:');Q.ensure_schema(conn)
    cfg=dict(account_alias='a',account_poll_s=120,quota_db='x',claude_credentials='y',account_poller=True)
    M.poll_account(cfg,conn);assert len(calls)==1
    Q.record_account_snapshot(conn,snapshot_id='s',provider='claude',account_alias='a',observed_at=time.time(),raw={},error='rate_limited')
    M.poll_account(cfg,conn);assert len(calls)==1


def test_only_the_head_seat_polls_and_a_reader_never_does(monkeypatch):
    import json
    calls=[];monkeypatch.setattr(M.subprocess,'run',lambda *a,**k:calls.append(1))
    M.poll_account(dict(account_alias='a',account_poll_s=300,account_poller=False));assert calls==[]
    flags={r:json.load(open('config/office/seat_%s_v2.json'%r)) for r in ('head','judge','scout')}
    assert [r for r,c in flags.items() if c['account_poller']]==['judge']
    assert {(c['account_poll_s'],c['account_max_age_s']) for c in flags.values()}=={(300,900)}


def test_changing_a_guard_setting_alone_changes_the_logged_config():
    base=dict(account_poller=True,account_max_age_s=900,account_poll_s=300)
    for key,value in (('account_poller',False),('account_max_age_s',300),('account_poll_s',120)):
        assert M.logged_settings(base)!=M.logged_settings(dict(base,**{key:value}))


def test_every_seat_reads_the_buyer_ledger_by_absolute_path(monkeypatch):
    import json,os
    for role in ('head','judge','scout'):
        cfg=json.load(open('config/office/seat_%s_v2.json'%role))
        assert os.path.isabs(cfg['funded_db'])
        monkeypatch.delenv('DB_PATH',raising=False);M.set_funded_db(cfg)
        assert os.environ['DB_PATH']==cfg['funded_db']


def test_the_poll_survives_a_failure_and_runs_off_the_main_thread(monkeypatch,tmp_path):
    calls=[]
    def poll(cfg,quota):
        calls.append(1)
        if len(calls)==1:raise OSError('x')
    class Stop(Exception):pass
    def wait(s):
        if len(calls)>=2:raise Stop
    monkeypatch.setattr(M,'poll_account',poll)
    with __import__('pytest').raises(Stop):M.poll_loop(dict(quota_db=str(tmp_path/'q.db')),wait=wait)
    assert len(calls)==2


def test_ticket_source_reads_only_passes_and_survives_none(tmp_path):
    import sqlite3
    db=tmp_path/'d.db';c=sqlite3.connect(db);c.execute("CREATE TABLE desk_tickets(stage TEXT,verdict TEXT,ticket_id TEXT DEFAULT 't',reason TEXT DEFAULT 'ok')");c.commit()
    latest=M.ticket_source(str(db));assert latest() is None
    c.execute("INSERT INTO desk_tickets(stage,verdict) VALUES('hard_filter','SKIP')");c.commit();assert latest() is None
    c.execute("INSERT INTO desk_tickets(stage,verdict) VALUES('hard_filter','PASS')");c.execute("INSERT INTO desk_tickets(stage,verdict) VALUES('vet','PASS')");c.commit();assert latest()['cursor']==2


def test_a_stuck_exit_pages_once_and_a_fresh_one_not_at_all(tmp_path):
    import sqlite3
    db=tmp_path/'l.db';c=sqlite3.connect(db);c.execute('CREATE TABLE orders(order_id TEXT,state TEXT,exit_requested_at REAL,exit_frontier_retries INTEGER)')
    c.execute("INSERT INTO orders VALUES('old','open',100,3)");c.execute("INSERT INTO orders VALUES('new','open',900,0)");c.execute("INSERT INTO orders VALUES('done','closed',100,0)");c.commit()
    pages=[];paged=set();cfg=dict(ledger_db=str(db),research_env_file='x')
    for _ in range(2):M.page_stuck_exits(cfg,paged,now=1000.,page=pages.append)
    assert len(pages)==1 and 'old' in pages[0]
    c.execute("UPDATE orders SET state='closed' WHERE order_id='old'");c.commit()
    for _ in range(2):M.page_stuck_exits(cfg,paged,now=1000.,page=pages.append)
    assert len(pages)==2 and 'no longer stuck' in pages[1] and paged==set()


def test_a_failing_exit_page_leaves_the_account_poller_running(monkeypatch,tmp_path):
    class Lost(Exception):pass
    polls=[]
    monkeypatch.setattr(M,'poll_account',lambda cfg,quota:polls.append(1))
    def boom(cfg,paged):raise Lost()
    monkeypatch.setattr(M,'page_stuck_exits',boom)
    class Stop(BaseException):pass
    def wait(s):
        if len(polls)>=2:raise Stop
    with __import__('pytest').raises(Stop):M.poll_loop(dict(quota_db=str(tmp_path/'q.db'),account_poller=True,desk_watcher=True,ledger_db='x'),wait=wait)
    assert len(polls)==2


def test_one_failed_exit_page_does_not_cost_the_other_order(tmp_path):
    import sqlite3
    db=tmp_path/'l.db';c=sqlite3.connect(db);c.execute('CREATE TABLE orders(order_id TEXT,state TEXT,exit_requested_at REAL,exit_frontier_retries INTEGER)')
    c.execute("INSERT INTO orders VALUES('aaa','open',100,0)");c.execute("INSERT INTO orders VALUES('bbb','open',100,0)");c.commit()
    sent=[]
    def page(text):
        if 'aaa' in text:raise RuntimeError('lost')
        sent.append(text)
    paged=set();M.page_stuck_exits(dict(ledger_db=str(db),research_env_file='x'),paged,now=1000.,page=page)
    assert paged=={'bbb'} and len(sent)==1


def test_the_watch_chain_has_the_page_wired():
    from pathlib import Path
    assert "C.research_url_of(cfg['research_env_file']),page=C.operator_page(cfg['research_env_file']))\n        threading.Thread(target=WA.loop" in Path(M.__file__).read_text()


def test_every_startup_change_note_fits_the_change_log():
    names=[n for n in vars(M) if n.endswith('_CHANGE')]
    assert len(names)>=5
    for name in names:assert len(getattr(M,name))<=500,name


def _poll_rig(monkeypatch,windows,age,error=None):
    import sqlite3,time,json
    from sniper.office import quota_measurement as Q
    calls=[];monkeypatch.setattr(M.subprocess,'run',lambda *a,**k:(calls.append(1),M.subprocess.CompletedProcess(a[0],0,stdout='{"state":"unknown","reason":"unavailable"}\n',stderr=''))[1])
    conn=sqlite3.connect(':memory:');Q.ensure_schema(conn);now=time.time()
    raw={k:dict(utilization=9,resets_at=now+v) for k,v in windows.items()}
    Q.record_account_snapshot(conn,snapshot_id='s',provider='claude',account_alias='a',observed_at=now-age,raw=raw,error=error)
    M.poll_account(dict(account_alias='a',account_poll_s=300,quota_db='x',claude_credentials='y',account_poller=True),conn)
    return calls


def test_a_reset_window_polls_at_once_and_otherwise_the_pace_holds(monkeypatch):
    assert len(_poll_rig(monkeypatch,dict(five_hour=-10,seven_day=9000),60))==1
    assert _poll_rig(monkeypatch,dict(five_hour=-2,seven_day=9000),60)==[]      # inside the grace
    assert _poll_rig(monkeypatch,dict(five_hour=500,seven_day=9000),60)==[]     # nothing reset


def test_a_failed_poll_after_the_reset_holds_the_pace(monkeypatch):
    assert _poll_rig(monkeypatch,{},60,error='rate_limited')==[]


def test_stopping_a_seat_host_stops_its_child_units(monkeypatch,tmp_path):
    import json
    from pathlib import Path
    unit=Path('deploy/office/office-seat@.service').read_text()
    stop=[l for l in unit.splitlines() if l.startswith('ExecStopPost=')]
    assert len(stop)==1 and stop[0].endswith('scripts/office_seat_host.py --config config/office/seat_%i_v2.json --stop-children')
    cfg=tmp_path/'c.json';cfg.write_text(json.dumps(dict(actor='ag_x')))
    seen=[]
    class Done:returncode=0;stderr=b''
    monkeypatch.setattr(M.subprocess,'run',lambda argv,**k:(seen.append(argv),Done())[1]);monkeypatch.setattr(M.B,'office_uid',lambda:995)
    monkeypatch.setattr(M.sys,'argv',['x','--config',str(cfg),'--stop-children'])
    M.main()     # returns before any proxy or database starts
    assert len(seen)==1 and seen[0][-1]=='office-seat-ag_x-*' and seen[0][-2]=='stop'
    class Bad:returncode=1;stderr=b'no'
    monkeypatch.setattr(M.subprocess,'run',lambda argv,**k:Bad())
    with __import__('pytest').raises(SystemExit):M.main()    # a failed stop is a failed unit stop, not silence


def test_the_partial_sheet_change_is_entered_in_the_change_log_and_names_its_rollback():
    from pathlib import Path
    assert 'content_hash=hashlib.sha256(PARTIAL_SHEET_CHANGE.encode()).hexdigest(),kind=\'config\',note=PARTIAL_SHEET_CHANGE' in Path(M.__file__).read_text()
    assert 'terminal policy change' in M.PARTIAL_SHEET_CHANGE and 'OFFICE_PARTIAL_SHEET_ORDERS=0' in M.PARTIAL_SHEET_CHANGE


def test_ticket_delta_counts_only_pass_rows_and_identifies_changes(tmp_path):
    import sqlite3
    db=tmp_path/'delta.db';c=sqlite3.connect(db)
    c.execute('CREATE TABLE desk_tickets(ticket_id TEXT,stage TEXT,verdict TEXT,reason TEXT)')
    c.executemany('INSERT INTO desk_tickets VALUES(?,?,?,?)',[
        ('old','hard_filter','PASS','old'),('skip','hard_filter','SKIP','no'),
        ('new','hard_filter','PASS','passed'),('vet','vet','PASS','not hard filter')])
    c.commit();latest=M.ticket_source(str(db))
    delta=latest(1)
    assert delta['cursor']==3 and delta['count']==1 and delta['truncated'] is False
    assert delta['tickets']==[dict(rowid=3,ticket_id='new',stage='hard_filter',verdict='PASS',reason='passed')]
    assert latest(3) is None
    first=latest(None)
    assert first['count'] is None and first['baseline'] is True
    c.close()
    assert M.ticket_source(str(tmp_path/'missing.db'))(3) is None


def test_ticket_delta_labels_capped_rows_without_an_exact_count(tmp_path):
    import sqlite3
    db=tmp_path/'delta.db';c=sqlite3.connect(db)
    c.execute('CREATE TABLE desk_tickets(ticket_id TEXT,stage TEXT,verdict TEXT,reason TEXT)')
    c.executemany("INSERT INTO desk_tickets VALUES(?,'hard_filter','PASS','ok')",[(str(i),) for i in range(40)])
    c.commit();delta=M.ticket_source(str(db))(1)
    assert delta['cursor']==40 and len(delta['tickets'])==32
    assert delta['truncated'] is True and delta['count'] is None and delta['observed_count']==32
