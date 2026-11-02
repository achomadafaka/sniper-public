import sqlite3

import pytest

from sniper.office import seat_events as E,seat_retention as R

NOW=2000000000.


def test_events_older_than_14_days_go_and_unknown_time_stays():
    conn=sqlite3.connect(':memory:',isolation_level=None);E.ensure_schema(conn)
    for at,name in ((NOW-15*86400,'old'),(NOW-13*86400,'new'),(None,'unknown')):
        conn.execute("INSERT INTO office_v2_events(event_id,epoch_id,actor_id,generation,incarnation,corrected_at,kind,state) VALUES(?,'e','a',1,1,?,'thought','known')",(name,at))
    assert R.prune_events(conn,now=NOW)==1
    assert {r[0] for r in conn.execute('SELECT event_id FROM office_v2_events')}=={'new','unknown'}


def test_logs_drop_whole_old_days_only(tmp_path):
    for name in ('tool-20200101.jsonl','model-20330518.jsonl','other.jsonl','tool-bad.jsonl'):(tmp_path/name).write_text('x')
    assert R.prune_logs(tmp_path,now=NOW)==1
    assert sorted(p.name for p in tmp_path.iterdir())==['model-20330518.jsonl','other.jsonl','tool-bad.jsonl']


def test_invalid_numeric_log_date_is_left_untouched(tmp_path):
    path=tmp_path/'tool-20261340.jsonl';path.write_text('keep')
    assert R.prune_logs(tmp_path,now=NOW)==0 and path.read_text()=='keep'


@pytest.mark.parametrize('sizes,reason',[((10,10),None),((101,10),'workspace_over_cap'),((10,101),'workspace_over_cap'),((None,10),'workspace_size_unknown')])
def test_cap_parks_when_over_or_unmeasurable(monkeypatch,sizes,reason):
    table=dict(zip(('w','s'),sizes))
    monkeypatch.setattr(R,'workspace_bytes',lambda p:table[p])
    assert R.over_cap(dict(workspace='w',state_dir='s',workspace_cap_bytes=100))==reason


def test_du_is_parsed_and_failure_is_none(monkeypatch):
    monkeypatch.setattr(R,'_as_office',lambda *a,**k:type('D',(),{'returncode':0,'stdout':'123\t/x\n'})())
    assert R.workspace_bytes('/x')==123
    monkeypatch.setattr(R,'_as_office',lambda *a,**k:type('D',(),{'returncode':1,'stdout':''})())
    assert R.workspace_bytes('/x') is None


@pytest.mark.parametrize('failure',['timeout','missing','negative'])
def test_unmeasurable_du_parks_instead_of_raising_or_admitting(monkeypatch,failure):
    import subprocess
    def measure(*a,**k):
        if failure=='timeout':raise subprocess.TimeoutExpired('du',120)
        if failure=='missing':raise OSError('cannot start du')
        return type('D',(),{'returncode':0,'stdout':'-1\t/x\n'})()
    monkeypatch.setattr(R,'_as_office',measure)
    assert R.over_cap(dict(workspace='w',state_dir='s'))=='workspace_size_unknown'


def test_seat_samples_the_cap(monkeypatch):
    from sniper.office import seat_host as H
    calls=[];monkeypatch.setattr(H.R,'over_cap',lambda cfg:calls.append(1))
    now=[1000.];s=H.Seat.__new__(H.Seat);s.cfg={};s.clock=lambda:now[0];s.cap_state=None;s.cap_checked=float('-inf')
    s._cap_reason();s._cap_reason();assert len(calls)==1
    now[0]+=H.CAP_CHECK_S;s._cap_reason();assert len(calls)==2


def test_prune_keeps_the_current_session_and_reports_failure(monkeypatch):
    calls=[]
    def fake(*argv,**k):
        calls.append(argv);return type('D',(),{'returncode':0 if argv[0]=='sh' else rc[0],'stdout':'present\n'})()
    rc=[0];monkeypatch.setattr(R,'_as_office',fake)
    assert R.prune_sessions('/s',current='abc') is True and 'abc.jsonl' in calls[-1]
    rc[0]=1;assert R.prune_sessions('/s',current='abc') is False


def test_missing_session_starts_fresh_from_the_note(monkeypatch):
    from sniper.office import seat_host as H
    s=H.Seat.__new__(H.Seat);s.cfg=dict(state_dir='/s',workspace='/w');s.state=dict(session_id='gone',incarnation=3)
    events=[];s._save=lambda:None;s._event=lambda *a,**k:events.append((a,k));s._read_note=lambda:'my note'
    s.office=None;s.actor='head';s.clock=lambda:5.;moved=[];monkeypatch.setattr(H.E,'set_incarnation',lambda conn,**k:moved.append(k['incarnation']))
    monkeypatch.setattr(H.R,'session_exists',lambda d,i,**k:False)
    assert s._recover_session(None)=='my note' and s.state['session_id'] is None and s.state['incarnation']==4 and moved==[4]
    assert events[0][0][1]=='session_reset'


def test_failed_session_search_keeps_the_session(monkeypatch):
    from sniper.office import seat_host as H
    s=H.Seat.__new__(H.Seat);s.cfg=dict(state_dir='/s');s.state=dict(session_id='live',incarnation=1)
    def boom(d,i,**k):raise OSError('session_search_failed')
    monkeypatch.setattr(H.R,'session_exists',boom)
    with pytest.raises(OSError):s._recover_session(None)
    assert s.state['session_id']=='live'


@pytest.mark.parametrize('rc,out,ok',[(0,'absent\n',True),(1,'',False),(126,'',False),(0,'garbage',False)])
def test_only_a_printed_absent_counts_as_fine(monkeypatch,rc,out,ok):
    monkeypatch.setattr(R,'_as_office',lambda *a,**k:type('D',(),{'returncode':rc,'stdout':out})())
    assert R.prune_sessions('/s') is ok


def test_prune_timeout_is_reported(monkeypatch):
    def boom(*a,**k):raise R.subprocess.TimeoutExpired('x',1)
    monkeypatch.setattr(R,'_as_office',boom)
    assert R.prune_sessions('/s') is False


def test_market_rows_go_on_their_rules_and_an_open_epoch_keeps_its_fetches(tmp_path):
    import json,sqlite3
    from sniper.office.market_data import MarketData
    now=100*86400.
    path=tmp_path/'m.db';m=MarketData(path);c=m._conn()
    for at in (now-31*86400,now-2*86400):
        c.execute('INSERT INTO calls(at,outcome) VALUES(?,?)',(at,'fetched'));c.execute('INSERT INTO cache VALUES(?,?,?)',('k%s'%at,at,'{}'))
    for at,fid in ((now-60*86400,'sealed'),(now-5*86400,'open')):
        c.execute('INSERT INTO fetches(fetch_id,shown_at) VALUES(?,?)',(fid,at))
    o=sqlite3.connect(':memory:');o.execute('CREATE TABLE epochs(id TEXT,payload_json TEXT)');o.execute('CREATE TABLE office_v2_changes(epoch_id TEXT,at REAL)')
    o.execute('INSERT INTO epochs VALUES(?,?)',('open',json.dumps({})))
    assert R.prune_market(path,o,now=now)==(0,2,0)   # open epoch with no recorded start: neither calls nor fetches go
    o.execute('INSERT INTO epochs VALUES(?,?)',('old',json.dumps(dict(closed_at=now-40*86400))))
    assert R.prune_market(path,o,now=now)==(0,0,0)   # open epoch without a recorded start
    o.execute('INSERT INTO office_v2_changes VALUES(?,?)',('open',now-40*86400))
    assert R.prune_market(path,o,now=now)==(0,0,1)   # the 31-day call is inside the open epoch
    o.execute('UPDATE office_v2_changes SET at=?',(now-10*86400,))
    assert R.prune_market(path,o,now=now)==(1,0,0)
    assert [r[0] for r in c.execute('SELECT fetch_id FROM fetches')]==['open']


def test_a_recently_sealed_epoch_keeps_its_fetches_and_shown_time_decides(tmp_path):
    import json,sqlite3
    from sniper.office.market_data import MarketData
    d=86400.;now=100*d
    path=tmp_path/'m.db';c=MarketData(path)._conn()
    # Fetched before the recent epoch began, shown inside it: a cached quote crossing the boundary.
    c.execute('INSERT INTO fetches(fetch_id,fetched_at,shown_at) VALUES(?,?,?)',('crossed',45*d,55*d))
    c.execute('INSERT INTO fetches(fetch_id,fetched_at,shown_at) VALUES(?,?,?)',('old',40*d,45*d))
    o=sqlite3.connect(':memory:');o.execute('CREATE TABLE epochs(id TEXT,payload_json TEXT)');o.execute('CREATE TABLE office_v2_changes(epoch_id TEXT,at REAL)')
    o.execute('INSERT INTO epochs VALUES(?,?)',('old',json.dumps(dict(closed_at=60*d))))
    o.execute('INSERT INTO epochs VALUES(?,?)',('recent',json.dumps(dict(closed_at=90*d))))
    o.execute('INSERT INTO office_v2_changes VALUES(?,?)',('recent',50*d))
    assert R.prune_market(path,o,now=now)==(0,0,1)
    assert [r[0] for r in c.execute('SELECT fetch_id FROM fetches')]==['crossed']


def test_seat_logs_and_watch_tables_follow_the_epoch_horizon(tmp_path):
    import json,sqlite3
    from sniper.office.chain_data import ChainData
    from sniper.office import watch_alert as W
    d=86400.;now=100*d
    (tmp_path/'m.db').touch();c=ChainData(tmp_path/'chain.db',lambda:'u')._conn();W.ensure_schema(c)
    c.execute('INSERT INTO watch_seen VALUES(?,?,?)',('old','m',45*d));c.execute('INSERT INTO watch_seen VALUES(?,?,?)',('new','m',55*d))
    o=sqlite3.connect(':memory:');o.execute('CREATE TABLE epochs(id TEXT,payload_json TEXT)');o.execute('CREATE TABLE office_v2_changes(epoch_id TEXT,at REAL)')
    o.execute('CREATE TABLE office_v2_results_reads(last_at REAL)');o.execute('INSERT INTO office_v2_results_reads VALUES(?)',(45*d,));o.execute('INSERT INTO office_v2_results_reads VALUES(?)',(55*d,))
    o.execute('INSERT INTO epochs VALUES(?,?)',('old',json.dumps(dict(closed_at=60*d))));o.execute('INSERT INTO epochs VALUES(?,?)',('recent',json.dumps(dict(closed_at=90*d))));o.execute('INSERT INTO office_v2_changes VALUES(?,?)',('recent',50*d))
    from sniper.office.market_data import MarketData
    MarketData(tmp_path/'m.db')._conn().close()
    R.prune_market(tmp_path/'m.db',o,now=now)
    assert [r[0] for r in c.execute('SELECT signature FROM watch_seen')]==['new']
    assert [r[0] for r in o.execute('SELECT last_at FROM office_v2_results_reads')]==[55*d]


def test_an_alert_goes_by_mail_time_and_an_unmailed_one_stays(tmp_path):
    import json,sqlite3
    from sniper.office.chain_data import ChainData
    from sniper.office.market_data import MarketData
    from sniper.office import watch_alert as W
    d=86400.;now=100*d
    MarketData(tmp_path/'m.db')._conn().close()
    c=ChainData(tmp_path/'chain.db',lambda:'u')._conn();W.ensure_schema(c)
    for sig,mailed in (('in',55*d),('unmailed',None),('gone',45*d)):
        c.execute('INSERT INTO watch_alerts VALUES(?,?,?,?,?,?,?,?)',(sig,'m','k','w',1,45*d,'{}',mailed))
    o=sqlite3.connect(':memory:');o.execute('CREATE TABLE epochs(id TEXT,payload_json TEXT)');o.execute('CREATE TABLE office_v2_changes(epoch_id TEXT,at REAL)')
    o.execute('INSERT INTO epochs VALUES(?,?)',('old',json.dumps(dict(closed_at=60*d))));o.execute('INSERT INTO epochs VALUES(?,?)',('recent',json.dumps(dict(closed_at=90*d))));o.execute('INSERT INTO office_v2_changes VALUES(?,?)',('recent',50*d))
    R.prune_market(tmp_path/'m.db',o,now=now)
    assert sorted(r[0] for r in c.execute('SELECT signature FROM watch_alerts'))==['in','unmailed']
