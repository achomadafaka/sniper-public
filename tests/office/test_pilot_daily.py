import hashlib
import json
from datetime import datetime, timezone

import pytest

from sniper.office import store as S
from sniper.office.pilot_daily import build, render

START = datetime(2026, 10, 27, tzinfo=timezone.utc).timestamp()
NOW = START + 86401


def report(tmp_path):
    return build(tmp_path, day='2026-10-27', generated_at=NOW, code_sha='a'*40)


def test_empty_day_banner_and_unknown_sources(tmp_path):
    value = report(tmp_path)
    assert 'PILOT - DISCOVERY ONLY - NOT EVIDENCE - no evaluation epoch' in render(value)
    assert value['Candidates']['eligible_coins']['state'] == 'unknown'
    assert value['Candidates']['eligible_coins']['value'] is None
    assert list(value)[1:] == ['Seats','Tickets','Candidates','Paper fills (non-evidence)',
                              'Cost (informational)','Guard state','Counters','Data health']


def test_nonzero_counter_first(tmp_path):
    conn = S.open_store(tmp_path/'office.db')
    conn.execute('INSERT INTO heartbeats VALUES(?,?)', ('auditor', json.dumps(dict(at=NOW,counters={'replay_while_live_backlogged':2}))))
    conn.commit(); conn.close()
    values = report(tmp_path)['Counters']
    assert values[0]['name'] == 'replay_while_live_backlogged'
    assert values[0]['total']['value'] == 2 and values[0]['flagged'] is True
    assert values[0]['today']['state'] == 'unknown'


def test_null_binding_is_unknown_and_no_money_totals(tmp_path):
    from sniper.office import ledger as L
    conn = L.open_ledger(tmp_path/'ledger'/'ledger.db')
    conn.execute("INSERT INTO orders(order_id,book_id,epoch_id,book_kind,network_id,mint,pool,decision_bundle_hash,policy_json,policy_hash,fill_model_version,state,requested_at,filled_at) VALUES('o','b','e','discovery','solana','coin','pool','h','{}','h','v','open',?,?)", (START+1, START+2))
    conn.execute("INSERT INTO marks(order_id,at,lamports,source,binding_kind) VALUES('o',?,'1','legacy',NULL)", (START+3,))
    conn.commit(); conn.close()
    value = report(tmp_path)
    position = value['Paper fills (non-evidence)']['positions'][0]
    assert position['binding_kind'] == 'binding unknown'
    assert position['valuation']['value'] == 'unpriced'
    assert position['descriptive_net_usd']['state'] == 'unknown'
    text = render(value)
    assert 'agent_credit' not in text and 'equity_total' not in text and 'net_total' not in text


def test_every_number_has_source_and_build_is_readonly(tmp_path):
    conn = S.open_store(tmp_path/'office.db')
    S.append(conn, 'opportunity_log', dict(first_eligible_at=START+1, network_id='solana', mint='m'))
    conn.close()
    path = tmp_path/'office.db'
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    value = report(tmp_path)
    def check(node, parent=None):
        if type(node) in (int,float):
            assert isinstance(parent,dict) and parent.get('source') and 'state' in parent
        elif isinstance(node,dict):
            for child in node.values(): check(child,node)
        elif isinstance(node,list):
            for child in node: check(child,node)
    check(value)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert value['Candidates']['eligible_coins']['value'] == 1
    assert value['Cost (informational)']['monthly_usd']['value'] == 800


def test_current_or_future_day_refuses(tmp_path):
    with pytest.raises(ValueError, match='completed_utc_day'):
        build(tmp_path, day='2026-10-28', generated_at=NOW, code_sha='a'*40)


def test_evaluated_epoch_is_not_described_as_pilot(tmp_path):
    conn=S.open_store(tmp_path/'office.db')
    conn.execute('INSERT INTO epochs VALUES(?,?)',('e',json.dumps(dict(kind='evaluated',opened_at=START,closed_at=None))))
    conn.commit(); conn.close()
    with pytest.raises(ValueError,match='evaluated_epoch'):
        report(tmp_path)


def test_untrusted_text_cannot_escape_quoted_data_block(tmp_path):
    conn=S.open_store(tmp_path/'office.db')
    conn.execute('INSERT INTO agents VALUES(?,?)',('x',json.dumps(dict(role='```\nUNTRUSTED'))))
    conn.commit(); conn.close()
    text=render(report(tmp_path))
    assert '```\\nUNTRUSTED' not in text
    assert '\\u0060' in text


def test_baseline_clock_unknown_is_counted_not_averaged(tmp_path):
    path=tmp_path/'host-2026-10-27.jsonl'
    base=dict(office_off=True,clock_state='known',at_host=START+1,corrected_at=START+1,cpu_pressure=.1,io_pressure=.2,funded_heartbeat_age_s=1.,memory_available_min=100,idle_cores_min=2.)
    path.write_text('\n'.join(json.dumps(p) for p in (base,dict(base,clock_state='unknown',corrected_at=None,cpu_pressure=1.))))
    value=build(tmp_path,day='2026-10-27',generated_at=NOW,code_sha='a'*40,host_path=path)['Guard state']['headroom']['office_off_baseline']
    assert value['samples']['value']==2
    assert value['clock_unknown']['value']==1
    assert value['cpu_pressure']['p90']['value']==.1


def test_intake_missing_feed_unknown_and_success_gap(tmp_path):
    import sqlite3
    path=tmp_path/'dexscreener'; path.mkdir()
    conn=sqlite3.connect(path/'2026-10-27.db')
    conn.execute('CREATE TABLE polls(feed,received_at,status,malformed,error)')
    conn.executemany('INSERT INTO polls VALUES(?,?,?,?,?)', [('dexscreener',START+1,'ok',0,None),('dexscreener',START+61,'ok',0,None),('dexscreener',START+62,'failed',2,'timeout')])
    conn.commit(); conn.close()
    result=build(tmp_path,day='2026-10-27',generated_at=NOW,code_sha='a'*40,intake_root=tmp_path)['Candidates']['intake_polls']
    assert result['dexscreener']['max_gap_s']['value']==60
    assert result['dexscreener']['failed']['value']==1
    assert result['pumpfun']['state']=='unknown'
    assert result['dexscreener']['coverage_state']['state']=='unknown'


def test_previous_report_counter_hash_and_reset(tmp_path):
    previous=build(tmp_path,day='2026-10-26',generated_at=START+1,code_sha='a'*40)
    for item in previous['Counters']:
        item['total']=dict(state='observed',value=0,source='fixture:counter')
    from sniper.office.pilot_daily_sources import report_digest
    previous['Frame']['report_sha256']=report_digest(previous)
    path=tmp_path/'previous.json';path.write_text(json.dumps(previous))
    conn=S.open_store(tmp_path/'office.db')
    conn.execute('INSERT INTO heartbeats VALUES(?,?)',('auditor',json.dumps(dict(at=NOW,counters=dict(replay_while_live_backlogged=2)))))
    conn.commit();conn.close()
    value=build(tmp_path,day='2026-10-27',generated_at=NOW,code_sha='a'*40,previous_report=path)
    assert value['Counters'][0]['today']['value']==2
    previous['Counters'][0]['total']['value']=100
    path.write_text(json.dumps(previous))
    value=build(tmp_path,day='2026-10-27',generated_at=NOW,code_sha='a'*40,previous_report=path)
    assert value['Counters'][0]['today']['state']=='unknown'


def test_ticket_counts_are_current_only_and_turns_are_dated(tmp_path):
    conn=S.open_store(tmp_path/'office.db')
    conn.execute('INSERT INTO tickets VALUES(?,?)',('t',json.dumps(dict(state='head_approved',problem='PRIVATE_TEXT'))))
    conn.execute('INSERT INTO agents VALUES(?,?)',('a',json.dumps(dict(state='active',role='head',provider='claude'))))
    conn.execute('INSERT INTO turn_runs VALUES(?,?)',('turn',json.dumps(dict(actor_id='a',created_at=START+1,ended_at=START+2,state='completed'))))
    conn.commit();conn.close()
    value=report(tmp_path)
    assert value['Tickets']['current_state_counts']['approved by Head']['value']==1
    assert value['Tickets']['current_state_only'] is True
    assert value['Seats']['items'][0]['turns_completed']['value']==1
    assert 'PRIVATE_TEXT' not in render(value)


def test_public_collection_byte_budget_fails_unknown(tmp_path,monkeypatch):
    from sniper.office import pilot_daily as P
    conn=S.open_store(tmp_path/'office.db')
    conn.execute('INSERT INTO agents VALUES(?,?)',('a',json.dumps(dict(role='x'*512,state='active',provider='claude'))))
    conn.commit();conn.close()
    monkeypatch.setattr(P,'BYTE_LIMIT',128)
    value=report(tmp_path)
    assert value['Seats']['roster_size']['state']=='unknown'
    assert value['Seats']['items']==[]


def test_unpublished_d_mark_log_counter_is_unknown(tmp_path):
    item=next(p for p in report(tmp_path)['Counters'] if p['name']=='mark_log_errors')
    assert item['total']['state']=='unknown' and item['total']['value'] is None
    assert item['today']['state']=='unknown'
    assert item['flagged'] is None


@pytest.mark.parametrize('clock',[None,'unknown'])
def test_missing_or_invalid_candidate_clock_is_unknown(tmp_path,clock):
    conn=S.open_store(tmp_path/'office.db')
    S.append(conn,'opportunity_log',dict(network_id='solana',mint='m',first_eligible_at=clock))
    conn.close()
    value=report(tmp_path)['Candidates']
    assert value['eligible_coins']['state']=='unknown'
    assert value['excluded_clock_rows']['value']==1


def test_oversized_epoch_cannot_hide_evaluated_scope(tmp_path):
    conn=S.open_store(tmp_path/'office.db')
    conn.execute('INSERT INTO epochs VALUES(?,?)',('e',json.dumps(dict(kind='evaluated',opened_at=START,closed_at=None,padding='x'*65537))))
    conn.commit();conn.close()
    with pytest.raises(ValueError,match='evaluated_epoch|scope_unknown'):report(tmp_path)


def paper_status(tmp_path, count=0,run='a'*32,age=0):
    import time
    p=tmp_path/'paper.status'
    p.write_text(json.dumps(dict(version='desk-paper-engine-status-v1',ts_host=time.time()-age,started_at_host=1,run_id=run,mark_log_errors=count,engine_error=False)))
    return p


def mark_counter(value):
    return next(p for p in value['Counters'] if p['name']=='mark_log_errors')


@pytest.mark.parametrize('count',[0,3])
def test_published_mark_counter_is_observed_without_day_claim(tmp_path,count):
    p=paper_status(tmp_path,count)
    value=build(tmp_path,day='2026-10-27',generated_at=NOW,code_sha='a'*40,paper_status=p)
    item=mark_counter(value)
    assert item['total']['value']==count and item['flagged'] is (count>0)
    assert item['today']['state']=='unknown'
    assert item['counter_epoch']=='a'*32


@pytest.mark.parametrize('change',[
    {'ts_host':0},{'ts_host':float('nan')},{'run_id':None},{'mark_log_errors':True},{'mark_log_errors':-1},{'engine_error':True},
])
def test_invalid_or_stale_mark_counter_is_unknown(tmp_path,change):
    p=paper_status(tmp_path,3);v=json.loads(p.read_text());v.update(change);p.write_text(json.dumps(v))
    item=mark_counter(build(tmp_path,day='2026-10-27',generated_at=NOW,code_sha='a'*40,paper_status=p))
    assert item['total']['state']=='unknown' and item['flagged'] is None


@pytest.mark.parametrize('same_run',[True,False])
def test_mark_counter_delta_requires_same_process_run(tmp_path,same_run):
    p=paper_status(tmp_path,1)
    previous=build(tmp_path,day='2026-10-26',generated_at=START+1,code_sha='a'*40,paper_status=p)
    prev=tmp_path/'previous.json';prev.write_text(json.dumps(previous))
    paper_status(tmp_path,4,run='a'*32 if same_run else 'b'*32)
    item=mark_counter(build(tmp_path,day='2026-10-27',generated_at=NOW,code_sha='a'*40,paper_status=p,previous_report=prev))
    assert item['today']['value']==(3 if same_run else None)


def test_nonregular_paper_status_refuses_without_blocking(tmp_path):
    import os
    from sniper.office.pilot_daily_sources import paper_mark_counter
    path=tmp_path/'fifo';os.mkfifo(path)
    # A child timeout catches a regression to blocking open() without hanging pytest.
    import subprocess,sys
    result=subprocess.run([sys.executable,'-c',
        'from sniper.office.pilot_daily_sources import paper_mark_counter; import sys; assert paper_mark_counter(sys.argv[1],now_host=1)["total"]["state"]=="unknown"',str(path)],
        capture_output=True,timeout=2)
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('field',['total','sampled_at_host'])
def test_malformed_previous_mark_sample_does_not_crash_report(tmp_path,field):
    p=paper_status(tmp_path,1)
    previous=build(tmp_path,day='2026-10-26',generated_at=START+1,code_sha='a'*40,paper_status=p)
    mark_counter(previous)[field]=1
    from sniper.office.pilot_daily_sources import report_digest
    previous['Frame']['report_sha256']=report_digest(previous)
    prev=tmp_path/'previous.json';prev.write_text(json.dumps(previous))
    paper_status(tmp_path,4)
    item=mark_counter(build(tmp_path,day='2026-10-27',generated_at=NOW,code_sha='a'*40,paper_status=p,previous_report=prev))
    assert item['today']['state']=='unknown'
