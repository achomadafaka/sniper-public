import re
import sqlite3

import pytest

from sniper.office import seat_db as D

NOW=2000000000.


def _externals(tmp_path,*,skip=()):
    """The other databases beside the desk db, with the real table shapes (plus a column the allowlist must leave out)."""
    import os
    for name,rel in D.SOURCES.items():
        if name in skip:continue
        os.makedirs(os.path.dirname(tmp_path/rel),exist_ok=True)
        c=sqlite3.connect(str(tmp_path/rel).replace('????-??-??','2026-10-30'))        # a day-partitioned source: one day file
        for table,(src,col,_,cols,_p,_a) in D.EXTERNAL.items():
            if src!=name:continue
            if table in D.SRC_TABLE:        # an intel table: the SOURCE table has the raw columns its expressions read
                raw=[x for x in cols if x not in D.EXPR[table] and not x.endswith('_truncated')]
                raw+=sorted({m for e in D.EXPR[table].values() for m in re.findall(r'[a-z_]*json',e)})
                c.execute('CREATE TABLE %s(%s)'%(D.SRC_TABLE[table],','.join(dict.fromkeys(raw))))
            else:c.execute('CREATE TABLE %s(%s)'%(table,','.join(cols)+(',golden_buyers' if table=='runner_signals' else '')))
        c.commit();c.close()


@pytest.fixture
def desk(tmp_path):
    path=tmp_path/'d.db';c=sqlite3.connect(path)
    for table,(_,_,cols) in D.ALLOWED.items():c.execute('CREATE TABLE %s(%s)'%(table,','.join(cols)+(',inputs_json,position_id' if table=='desk_tickets' else '')))
    c.execute('CREATE TABLE desk_paper_positions(pnl REAL)');c.execute('INSERT INTO desk_paper_positions VALUES(5)')
    for ts,mint in (('2020-01-01T00:00:00+00:00','past'),('2099-01-01T00:00:00+00:00','future'),(None,'null')):c.execute('INSERT INTO desk_tickets(ts,mint,inputs_json,position_id) VALUES(?,?,?,?)',(ts,mint,'{"secret":1}','pos1'))
    c.commit();c.close();_externals(tmp_path);return str(path)


def test_rows_are_bounded_to_the_cut_and_null_time_is_excluded(desk):
    out=D.read(desk,'SELECT mint FROM desk_tickets',now=NOW)
    assert out['state']=='known' and out['rows']==[['past']]


@pytest.mark.parametrize('sql',['SELECT * FROM desk_paper_positions','SELECT * FROM main.desk_paper_positions',"ATTACH '/etc/passwd' AS x",
    'DELETE FROM desk_tickets','PRAGMA table_info(desk_tickets)','SELECT 1; SELECT 2','SELECT inputs_json FROM desk_tickets','SELECT position_id FROM desk_tickets',"SELECT * FROM desk_tickets UNION SELECT * FROM desk_paper_positions","SELECT load_extension('x')",'CREATE TABLE t(a)','WITH x AS (SELECT * FROM main.desk_paper_positions) SELECT * FROM x','WITH desk_tickets AS (SELECT * FROM main.desk_paper_positions) SELECT * FROM desk_tickets'])
def test_everything_else_is_refused(desk,sql):
    out=D.read(desk,sql,now=NOW)
    assert out['state']=='unknown' and not out.get('rows')


def test_deadline_is_partial_not_empty(desk,monkeypatch):
    monkeypatch.setattr(D,'DEADLINE_S',0.)
    out=D.read(desk,'WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) SELECT count(*) FROM n',now=NOW)
    assert out['state']=='partial'


def test_row_cap_is_partial(desk):
    out=D.read(desk,'WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<500) SELECT i FROM n',now=NOW)
    assert out['state']=='partial' and out['reason']=='row_limit' and len(out['rows'])==D.MAX_ROWS


def test_missing_database_is_unknown(tmp_path):
    assert D.read(str(tmp_path/'none.db'),'SELECT 1',now=NOW)['state']=='unknown'


def test_large_json_column_is_capped_by_bytes_with_valid_rows(desk):
    c=sqlite3.connect(desk)
    for i in range(50):c.execute('INSERT INTO desk_tickets(ts,mint) VALUES(?,?)',('2020-01-01T00:00:00+00:00','x'*2000))
    c.commit();c.close()
    out=D.read(desk,'SELECT mint FROM desk_tickets',now=NOW)
    assert out['state']=='partial' and out['reason']=='output_bytes' and out['limited'] is True and 0<len(out['rows'])<50
    import json;assert len(json.dumps(out))<16384


def test_value_size_is_limited(desk):
    assert D.read(desk,'SELECT zeroblob(10000000)',now=NOW)['state']=='unknown'


@pytest.mark.parametrize('sql',['WITH x AS (SELECT * FROM main.desk_tickets) SELECT mint FROM x','SELECT mint FROM main.desk_tickets','SELECT mint FROM desk_tickets'])
def test_no_spelling_reaches_a_row_past_the_cut(desk,sql):
    out=D.read(desk,sql,now=NOW)
    assert out['rows']==[['past']]


def test_a_table_cut_to_newest_rows_says_so(desk,monkeypatch):
    monkeypatch.setattr(D,'COPY_ROWS',0)
    out=D.read(desk,'SELECT count(*) FROM desk_tickets',now=NOW)
    assert out['coverage']['desk_tickets']['state']=='newest_only'


def test_nothing_in_the_query_database_names_the_excluded_table(desk):
    out=D.read(desk,'SELECT name FROM sqlite_master',now=NOW)
    assert 'desk_paper_positions' not in str(out['rows'])


def test_real_desk_columns_all_exist():
    """Real shape: a renamed column must fail here, not at a seat's first query."""
    import os
    path='/home/bigpapi/Desktop/Sniper/data/memecoin_desk.db'
    if not os.path.exists(path):pytest.skip('no desk database')
    out=D.read(path,'SELECT 1',now=NOW)
    assert out['state']=='known' and all(isinstance(v,dict) for v in out['coverage'].values())


def test_whole_answer_is_capped_even_with_a_huge_column_name(desk):
    import json
    c=sqlite3.connect(desk);c.execute('INSERT INTO desk_tickets(ts,mint) VALUES(?,?)',('2020-01-01T00:00:00+00:00','m'*11800));c.commit();c.close()
    out=D.read(desk,'SELECT mint AS "%s" FROM desk_tickets'%('\\\\'*1900),now=NOW)
    assert len(json.dumps(out))<=D.MAX_OUTPUT and out['state']=='unknown' and out['reason']=='response_too_large'
    out=D.read(desk,'SELECT mint FROM desk_tickets',now=NOW)
    assert len(json.dumps(out))<=D.MAX_OUTPUT and out['state']=='partial' and out['reason']=='output_bytes'


def test_copy_timeout_reads_partial(desk,monkeypatch):
    monkeypatch.setattr(D,'COPY_DEADLINE_S',-1.)
    c=sqlite3.connect(desk)
    for i in range(5000):c.execute('INSERT INTO desk_tickets(ts,mint) VALUES(?,?)',('2020-01-01T00:00:00+00:00','x'))
    c.commit();c.close()
    assert D.read(desk,'SELECT 1',now=NOW)['state']=='partial'


def test_whole_table_flag(desk,monkeypatch):
    assert D.read(desk,'SELECT 1',now=NOW)['whole_table'] is True
    monkeypatch.setattr(D,'COPY_ROWS',0)
    assert D.read(desk,'SELECT count(*) FROM desk_tickets',now=NOW)['whole_table'] is False


def test_too_many_columns_refused(desk):
    sql='SELECT '+','.join('1 AS c%d'%i for i in range(65))
    assert D.read(desk,sql,now=NOW)['reason']=='response_too_large'


def test_copied_tables_hold_no_excluded_column(desk):
    out=D.read(desk,"SELECT name FROM pragma_table_info('desk_tickets')",now=NOW)
    assert 'inputs_json' not in str(out) and 'position_id' not in str(out)


def test_memory_hungry_recursion_is_bounded_in_the_child(desk):
    import json,subprocess,sys,time
    start=time.monotonic()
    sql='WITH RECURSIVE n(i,b) AS (SELECT 1,zeroblob(100000) UNION ALL SELECT i+1,zeroblob(100000) FROM n) SELECT count(*) FROM (SELECT i,b FROM n ORDER BY i DESC)'
    done=subprocess.run([sys.executable,D.__file__,desk,sql,str(NOW)],capture_output=True,text=True,timeout=30,env={'PYTHONPATH':'.'})
    assert json.loads(done.stdout)['state'] in ('unknown','partial') and time.monotonic()-start<15


def test_a_small_memory_limit_really_binds_the_sorter_and_leaves_no_temp_file(desk,tmp_path):
    import json,os,subprocess,sys
    temp=tmp_path/'t';temp.mkdir()
    code=("import importlib.util as u,sys;s=u.spec_from_file_location('d',%r);m=u.module_from_spec(s);s.loader.exec_module(m);"
          "m.CHILD_MEMORY=96*1024**2;m.CHILD_FILE=8*1024**3;m.DEADLINE_S=20.;m.main(['x',%r,sys.argv[1],'2000000000'])")%(D.__file__,desk)   # a large file limit, so only temp_store can stop the spill
    # max(length(b)) needs b from the sorter, so the sort cannot be optimised away.
    sql='WITH RECURSIVE n(i,b) AS (SELECT 1,zeroblob(500000) UNION ALL SELECT i+1,zeroblob(500000) FROM n WHERE i<2000) SELECT max(length(b)) FROM (SELECT i,b FROM n ORDER BY i DESC LIMIT 2000)'
    done=subprocess.run([sys.executable,'-c',code,sql],capture_output=True,text=True,timeout=30,env=dict(TMPDIR=str(temp),SQLITE_TMPDIR=str(temp)))
    out=json.loads(done.stdout)
    assert out['state']=='unknown' and not out['rows'] and os.listdir(temp)==[],(out,os.listdir(temp))


def test_the_file_limit_stops_a_spill_on_its_own(tmp_path):
    """The second belt, tested alone: a statement that spills under temp_store=FILE dies at RLIMIT_FSIZE."""
    import subprocess,sys
    code=("import resource,sqlite3;resource.setrlimit(resource.RLIMIT_FSIZE,(%d,%d));c=sqlite3.connect(':memory:');c.execute('PRAGMA temp_store=FILE');"
          "c.execute('WITH RECURSIVE n(i,b) AS (SELECT 1,zeroblob(500000) UNION ALL SELECT i+1,zeroblob(500000) FROM n WHERE i<2000) SELECT max(length(b)) FROM (SELECT i,b FROM n ORDER BY i DESC LIMIT 2000)').fetchone();print('finished')")%(D.CHILD_FILE,D.CHILD_FILE)
    done=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=60,env=dict(SQLITE_TMPDIR=str(tmp_path)))
    assert 'finished' not in done.stdout


def _put(tmp_path,name,table,**row):
    c=sqlite3.connect(tmp_path/D.SOURCES[name]);c.execute('INSERT INTO %s(%s) VALUES(%s)'%(table,','.join(row),','.join('?'*len(row))),tuple(row.values()));c.commit();c.close()


def test_other_databases_are_read_with_last_write_and_asof_now(desk,tmp_path):
    _put(tmp_path,'runner','runner_signals',mint='m1',creator='c1',created_at='2020-01-01T00:00:00+00:00',golden_buyers='x')
    out=D.read(desk,'SELECT creator FROM runner_signals',now=NOW)
    assert out['rows']==[['c1']]
    cov=out['coverage']['runner_signals']
    assert cov['asof']=='now' and cov['last_write']=='2020-01-01T00:00:00+00:00' and cov['stale'] is True and cov['source']=='runner'
    assert 'golden_buyers' not in str(D.read(desk,"SELECT name FROM pragma_table_info('runner_signals')",now=NOW))     # our own decision column stays out


def test_a_fresh_table_is_not_stale_and_future_rows_are_cut(desk,tmp_path):
    fresh='2033-05-17T00:00:00+00:00'                     # NOW is 2033-05-18
    _put(tmp_path,'cluster','cluster_funder_cache',owner='o1',funder='f1',status='resolved',resolved_at=fresh)
    _put(tmp_path,'cluster','cluster_funder_cache',owner='o2',funder='f2',status='resolved',resolved_at='2099-01-01T00:00:00+00:00')
    out=D.read(desk,'SELECT owner FROM cluster_funder_cache',now=NOW)
    assert out['rows']==[['o1']] and out['coverage']['cluster_funder_cache']['stale'] is False


def test_holders_follow_their_snapshot_time(desk,tmp_path):
    _put(tmp_path,'cluster','cluster_snapshots',snapshot_id='old',mint='m',snapshot_at='2020-01-01T00:00:00+00:00')
    _put(tmp_path,'cluster','cluster_snapshots',snapshot_id='new',mint='m',snapshot_at='2099-01-01T00:00:00+00:00')
    _put(tmp_path,'cluster','cluster_snapshot_holders',snapshot_id='old',owner='a',balance_atomic='1')
    _put(tmp_path,'cluster','cluster_snapshot_holders',snapshot_id='new',owner='b',balance_atomic='1')
    assert D.read(desk,'SELECT owner FROM cluster_snapshot_holders',now=NOW)['rows']==[['a']]


def test_a_missing_source_reads_unavailable_not_empty_and_is_not_whole(tmp_path):
    path=tmp_path/'d.db';c=sqlite3.connect(path)
    for table,(_,_,cols) in D.ALLOWED.items():c.execute('CREATE TABLE %s(%s)'%(table,','.join(cols)))
    c.commit();c.close();_externals(tmp_path,skip=('runner',))
    out=D.read(str(path),'SELECT count(*) FROM runner_signals',now=NOW)
    assert out['coverage']['runner_signals']['state']=='unavailable' and out['whole_table'] is False
    assert out['coverage']['op_creator_outcomes']['state']=='complete'          # the others still read


def test_external_columns_all_exist_on_the_live_files():
    """A renamed column in a live file must fail here, not at a seat's first query."""
    import os
    path='/home/bigpapi/Desktop/Sniper/data/memecoin_desk.db'
    if not os.path.exists(path):pytest.skip('no desk database')
    out=D.read(path,'SELECT 1',now=NOW)
    assert out['state']=='known' and all(out['coverage'][t]['state']!='unavailable' for t,v in D.EXTERNAL.items() if v[0]!='outcomes')   # the outcome file appears when its writer first runs


def test_outcome_rows_are_dated_by_their_own_time_so_an_immature_horizon_is_cut(desk,tmp_path):
    _put(tmp_path,'outcomes','outcome_horizons',network_id='solana',mint='m1',horizon_s=900,matured_at=NOW-100,status='ok',ret=-.5,n_prints=3)
    _put(tmp_path,'outcomes','outcome_horizons',network_id='solana',mint='m1',horizon_s=1800,matured_at=NOW+100,status='ok',ret=2.,n_prints=3)   # matures after the cut
    out=D.read(desk,'SELECT horizon_s,ret FROM outcome_horizons',now=NOW)
    assert out['rows']==[[900,-.5]]
    cov=out['coverage']['outcome_horizons']
    assert cov['asof']=='row' and cov['source']=='outcomes' and cov['stale'] is False
    assert 'ticker' not in str(D.read(desk,"SELECT name FROM pragma_table_info('outcome_coins')",now=NOW))


def test_outcome_loss_and_bracket_columns_are_readable_and_ok_bracketed_is_a_status_of_its_own(desk,tmp_path):
    _put(tmp_path,'outcomes','outcome_horizons',network_id='solana',mint='m1',horizon_s=60,matured_at=NOW-100,status='ok_bracketed',ret=.01,n_prints=3,gap_check='checked',tape_decoder_version='ps-decode-v1',
         loss_complete_n=5,loss_unknown_n=2,loss_share_complete=.5,loss_share_all=.6,bracket_n=5,bracket_endpoint_displacement_max=.02)
    out=D.read(desk,'SELECT status,loss_complete_n,loss_unknown_n,loss_share_complete,loss_share_all,bracket_n,bracket_endpoint_displacement_max FROM outcome_horizons',now=NOW)
    assert out['rows']==[['ok_bracketed',5,2,.5,.6,5,.02]]


def test_outcome_columns_are_numeric_or_ids_only():
    for t in ('outcome_coins','outcome_horizons','mint_creators'):
        assert not {'ticker','name','symbol','note','evidence_json'} & set(D.EXTERNAL[t][3])


def test_creator_by_mint_is_readable_and_conflicts_stay_visible(desk,tmp_path):
    _put(tmp_path,'outcomes','mint_creators',network_id='solana',mint='m1',creator='c1',source='launches',source_at=NOW-10)
    _put(tmp_path,'outcomes','mint_creators',network_id='solana',mint='m1',creator='c2',source='ps_live',source_at=NOW-5)
    _put(tmp_path,'outcomes','mint_creators',network_id='solana',mint='m1',creator='c3',source='ps_live',source_at=NOW+50)      # seen after the cut
    out=D.read(desk,"SELECT creator,source FROM mint_creators WHERE mint='m1' ORDER BY source_at",now=NOW)
    assert out['rows']==[['c1','launches'],['c2','ps_live']] and out['coverage']['mint_creators']['asof']=='row'


def test_a_source_file_without_its_table_reads_unavailable_and_the_rest_still_read(desk,tmp_path):
    # the outcome writer creates the file before its tables: a half-built or drifted source must not take every table down
    import os
    os.remove(tmp_path/D.SOURCES['outcomes']);sqlite3.connect(tmp_path/D.SOURCES['outcomes']).close()
    out=D.read(desk,'SELECT count(*) FROM desk_tickets',now=NOW)
    assert out['state']=='known' and out['rows']==[[1]]
    assert out['coverage']['outcome_coins']['state']=='unavailable' and out['whole_table'] is False
    assert out['coverage']['cluster_funder_cache']['state']=='complete'


def test_pass_replay_rows_are_dated_by_maturity_and_replay_meta_is_readable(desk,tmp_path):
    _put(tmp_path,'replay','pass_replay',ticket_id='old',variant='base_d1',matured_at=NOW-10,status='ok',pass_at=NOW-4000)
    _put(tmp_path,'replay','pass_replay',ticket_id='new',variant='base_d1',matured_at=NOW+10,status='ok',pass_at=NOW-3000)     # its hold has not elapsed at the cut
    _put(tmp_path,'replay','replay_meta',built_at=NOW-5,version='office-pass-replay-v2',rule='r',since=0,variants='{}')
    out=D.read(desk,'SELECT ticket_id FROM pass_replay',now=NOW)
    assert out['rows']==[['old']]
    assert D.read(desk,'SELECT version FROM replay_meta',now=NOW)['rows']==[['office-pass-replay-v2']]
    assert 'features_json' not in str(D.read(desk,"SELECT name FROM pragma_table_info('pass_replay')",now=NOW))


def _intel_rows(tmp_path,n_old=3):
    """A real-shaped X day file: posts with payload_json, dated by receipt."""
    import json,os
    f=tmp_path/D.SOURCES['x'].replace('????-??-??','2026-10-30')
    os.makedirs(os.path.dirname(f),exist_ok=True)
    c=sqlite3.connect(f)
    c.execute('DROP TABLE IF EXISTS posts');c.execute('DROP TABLE IF EXISTS ai_suggestion')
    c.execute('CREATE TABLE posts(id INTEGER PRIMARY KEY,event TEXT,tweet_id TEXT,received_at REAL,created_at_ms INTEGER,handle TEXT,author_id TEXT,post_type TEXT,payload_json TEXT)')
    c.execute('CREATE TABLE ai_suggestion(id INTEGER PRIMARY KEY,event TEXT,tweet_id TEXT,ticker TEXT,ai_ticker TEXT,received_at REAL,payload_json TEXT)')
    for i in range(n_old):
        c.execute('INSERT INTO posts VALUES(NULL,"tweet_update",?,?,?,?,?,?,?)',('t%d'%i,NOW-3600-i,0,'h','a','TWEET',json.dumps({'text':'x'*(1500 if i==0 else 10),'author':{'followersCount':5},'metrics':{'views':7,'likes':1,'retweets':2}})))
    c.execute('INSERT INTO posts VALUES(NULL,"tweet_update","future",?,0,"h","a","TWEET",?)',(NOW+50,json.dumps({'text':'later'})))     # after the cut
    c.commit();c.close()


def test_intel_text_is_cut_marked_dated_by_receipt_and_the_note_carries_retention(desk,tmp_path):
    _intel_rows(tmp_path)
    out=D.read(desk,'SELECT tweet_id,length(text) n,text_truncated t,followers,views FROM x_posts ORDER BY received_at DESC',now=NOW)
    assert out['state']=='known'
    rows={r[0]:r for r in out['rows']}
    assert set(rows)=={'t0','t1','t2'}                                             # the row received after the cut is not served
    assert rows['t0'][1]==1000 and rows['t0'][2]==1 and rows['t1'][1]==10 and rows['t1'][2]==0      # cut to 1000 bytes and marked; short text unmarked
    assert rows['t0'][3]==5 and rows['t0'][4]==7
    cov=out['coverage']['x_posts']
    assert cov['state']=='complete' and cov['max_receipt']==NOW-3600 and cov['age_s']==3600 and cov['stale'] is True      # 900 s line: served with its age, flagged stale
    assert cov['min_receipt']==NOW-3602 and sum(cov['rows_per_day'].values())==3 and 'No predictive value' in cov['note'] and 'Retention' in cov['note']
    assert out['coverage']['news_items']['state']=='not_requested' and 'note' not in out['coverage']['news_items']       # unnamed intel tables are neither copied nor described


def test_an_intel_source_that_is_missing_reads_unavailable_with_its_note_never_empty(desk,tmp_path):
    out=D.read(desk,'SELECT count(*) FROM crawler_crawls',now=NOW)
    cov=out['coverage']['crawler_crawls']
    assert cov['state'] in ('unavailable','complete') and 'note' in cov
    import os
    os.remove(tmp_path/D.SOURCES['crawler']) if os.path.exists(tmp_path/D.SOURCES['crawler']) else None
    out=D.read(desk,'SELECT count(*) FROM crawler_crawls',now=NOW)
    assert out['coverage']['crawler_crawls']['state']=='unavailable' and out['whole_table'] is False


def test_intel_columns_all_exist_on_the_live_files():
    """A renamed column or an expression that no longer parses in a live intel file must fail here, not at a seat first query."""
    import os,time
    path='/home/bigpapi/Desktop/Sniper/data/memecoin_desk.db'
    if not os.path.exists(path) or not os.path.isdir(os.path.dirname(path)+'/desk/x'):pytest.skip('no live intel files')
    names=sorted(D.NOTES)
    cov={}
    for k in range(0,len(names),6):      # six tables a statement: the answer is capped at 12 KB
        out=D.read(path,' UNION ALL '.join("SELECT '%s' t, count(*) n FROM %s"%(t,t) for t in names[k:k+6]),now=time.time())
        assert out['state']=='known',out
        cov.update(out['coverage'])
    out={'coverage':cov}
    for t in names:
        src=os.path.join(os.path.dirname(path),D.SOURCES[D.EXTERNAL[t][0]])
        if any(ch in src for ch in '?*'):
            import glob
            if not glob.glob(src):continue
        elif not os.path.exists(src):continue
        assert out['coverage'][t]['state']!='unavailable',(t,out['coverage'][t])


def test_intel_table_names_match_case_insensitively_like_sqlite(desk,tmp_path):
    _intel_rows(tmp_path)
    lower=D.read(desk,'SELECT count(*) FROM x_posts',now=NOW)['rows']
    upper=D.read(desk,'SELECT count(*) FROM X_POSTS',now=NOW)
    assert lower==[[3]] and upper['rows']==[[3]] and upper['coverage']['x_posts']['state']!='not_requested'        # SQLite folds the name; the wanted set must too


def test_every_string_column_of_an_intel_table_is_cut_and_listed_not_only_the_declared_text_field(desk,tmp_path):
    import json
    _intel_rows(tmp_path)
    c=sqlite3.connect(str(tmp_path/D.SOURCES['x']).replace('????-??-??','2026-10-30'))
    c.execute('INSERT INTO posts VALUES(NULL,"tweet_update","long",?,0,?,"a","TWEET",?)',(NOW-10,'h'*1500,json.dumps({'text':'ok'})));c.commit();c.close()
    out=D.read(desk,"SELECT length(handle),truncated_fields,text_truncated FROM x_posts WHERE tweet_id='long'",now=NOW)
    assert out['rows']==[[1000,'handle',0]]                      # the handle is cut to 1000 bytes and named; the declared text field was short


def test_web_or_model_derived_intel_tables_are_all_in_the_taint_set():
    from sniper.office import seat_intel as I
    assert {'x_posts','x_ai_suggestions','news_items','narrative_guesses','narrative_token_hourly','crawler_crawls','offchain_coin_heat','narrative_tags','narrative_theme_agg'}<=I.WEB_TEXT_TABLES
    assert not ({'trending_boards','attention_events','dex_boost_seen'}&I.WEB_TEXT_TABLES)


def test_retention_names_the_real_prune_rule_and_the_reader_horizon_not_the_stored_minimum(desk,tmp_path):
    _intel_rows(tmp_path)
    cov=D.read(desk,'SELECT count(*) FROM x_posts',now=NOW)['coverage']['x_posts']
    assert cov['reader_horizon_start']=='2026-10-30' and cov['files_opened']>=1
    assert 'MEMECOIN_DESK_X_RETENTION_DAYS' in cov['note'] and 'reader_horizon_start' in cov['note'] and 'no pruning rule known' not in cov['note']


def test_a_partition_that_cannot_be_read_makes_the_table_partial_source_naming_the_file(desk,tmp_path):
    _intel_rows(tmp_path)
    broken=tmp_path/D.SOURCES['x'].replace('????-??-??','2026-10-29')
    c=sqlite3.connect(broken);c.execute('CREATE TABLE unrelated(a)');c.commit();c.close()      # a day file without the posts table
    out=D.read(desk,'SELECT count(*) FROM x_posts',now=NOW)
    cov=out['coverage']['x_posts']
    assert out['rows']==[[3]] and cov['state']=='partial_source' and cov['failed_files']==['2026-10-29.db'] and out['whole_table'] is False      # never the surviving subset as the table


def test_narrative_v2_tables_are_read_from_the_live_v2_files_with_their_lower_bound_note(desk,tmp_path):
    import os
    day=tmp_path/D.SOURCES['narrative_v2'].replace('????-??-??','2026-10-30')
    os.makedirs(os.path.dirname(day),exist_ok=True)
    c=sqlite3.connect(day)
    c.execute('DROP TABLE IF EXISTS tags');c.execute('CREATE TABLE tags(mint,theme,matched,rules_version,launch_received_at,known_at,name,symbol,source_rowid)')
    c.execute('INSERT INTO tags VALUES("m","pepe",1,"v",?,?,?,?,7)',(NOW-60,NOW-59,'n'*1200,'SYM'))
    c.execute('INSERT INTO tags VALUES("later","x",1,"v",?,?,"n","S",8)',(NOW+5,NOW+6))               # received after the cut
    c.commit();c.close()
    out=D.read(desk,'SELECT mint,length(name),name_truncated,symbol FROM narrative_v2_tags',now=NOW)
    assert out['rows']==[['m',1000,1,'SYM']]
    note=out['coverage']['narrative_v2_tags']['note']
    assert 'LOWER BOUND' in note and 'not chain creations' in note and 'NULL' in note and 'v1 is stale' in note
    assert out['coverage']['narrative_tags']['state']=='not_requested'


def test_narrative_v2_text_tables_taint_and_the_source_state_does_not():
    from sniper.office import seat_intel as I
    assert {'narrative_v2_tags','narrative_v2_theme_agg','narrative_v2_token_hourly'}<=I.WEB_TEXT_TABLES and 'narrative_v2_source_state' not in I.WEB_TEXT_TABLES


def test_naming_every_intel_table_still_fits_the_answer_and_keeps_failure_flags(desk,tmp_path):
    import json
    _intel_rows(tmp_path)
    broken=tmp_path/D.SOURCES['x'].replace('????-??-??','2026-10-29');c=sqlite3.connect(broken);c.execute('CREATE TABLE unrelated(a)');c.commit();c.close()
    sql=' UNION ALL '.join("SELECT '%s' t, count(*) n FROM %s"%(t,t) for t in sorted(D.NOTES))
    out=D.read(desk,sql,now=NOW)
    assert out['state']=='known' and len(json.dumps(out,default=str))<12000
    assert out['coverage']['x_posts']['state']=='partial_source' and out['coverage']['x_posts']['failed_files']==['2026-10-29.db']      # shrinking drops notes, never a failure flag
