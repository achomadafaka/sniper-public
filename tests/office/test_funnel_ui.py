"""Read-only API integration: hard-filter evidence remains a separate bounded universe."""
from datetime import datetime,timezone
import hashlib
import json
import sqlite3

from sniper.office.report import read_state

CUT=datetime(2026,10,29,12,tzinfo=timezone.utc).timestamp()


def fixture(tmp_path):
    root=tmp_path/'data'/'office';root.mkdir(parents=True)
    db=root.parent/'memecoin_desk.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE desk_tickets(id INTEGER PRIMARY KEY,ticket_id TEXT,stage TEXT,seat TEXT,verdict TEXT,reason TEXT,inputs_json TEXT,ts TEXT)')
        payload={'PRIVATE_UNSELECTED_PAYLOAD':'not for UI','evidence':{'checks':{'dump_group':{'status':'unknown','reason':'partial_history'},'chart':{'status':'pass','reason':'complete'}}}}
        conn.execute('INSERT INTO desk_tickets VALUES(1,?,?,?,?,?,?,?)',('ticket','hard_filter','CHECKER','SKIP','unknown:dump_group:partial_history',json.dumps(payload),'2026-10-29T11:59:00+00:00'))
    return root,db


def test_api_wires_first_blocker_checks_without_raw_payload_or_writes(tmp_path):
    root,db=fixture(tmp_path);before=hashlib.sha256(db.read_bytes()).hexdigest()
    state=read_state(root,now=CUT,host_now=CUT);f=state['funnel']
    assert f['hard_filter_state']=='known' and f['hard_filter_count']==1
    assert f['blockers']==[{'check':'dump_group','count':1}]
    assert f['tickets'][0]['checks']['dump_group']=={'status':'unknown','reason':'partial_history'}
    assert f['tickets'][0]['checks']['holders']['status']=='not_observed'
    assert 'PRIVATE_UNSELECTED_PAYLOAD' not in json.dumps(state)
    assert hashlib.sha256(db.read_bytes()).hexdigest()==before
    assert state['read_budget']['queries']<=512


def test_no_filter_in_all_stage_window_is_zero_not_no_candidates(tmp_path):
    root,db=fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.executemany("INSERT INTO desk_tickets(ticket_id,stage,seat,verdict,reason,inputs_json,ts) VALUES(?,'vet','CHECKER','PASS','pass','{}','2026-10-29T11:59:00+00:00')",[(str(i),) for i in range(205)])
    f=read_state(root,now=CUT,host_now=CUT)['funnel']
    assert f['hard_filter_count']==0 and f['hard_filter_state']=='known'
    assert f['tickets']==[] and f['limited'] is True
    assert 'all stages' in f['coverage']


def test_missing_or_future_filter_data_is_unknown_not_zero(tmp_path):
    root,db=fixture(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE desk_tickets SET ts='2026-10-29T13:00:00+00:00'")
    f=read_state(root,now=CUT,host_now=CUT)['funnel']
    assert f['hard_filter_count'] is None and f['hard_filter_state']=='unknown'
    assert not f['tickets']
