"""Bounded read and causal clock witnesses; local SQLite only."""
from contextlib import contextmanager
from datetime import datetime,timezone
import sqlite3

import pytest
from sniper.office import desk_funnel as F
from office.test_desk_funnel import desk,ro

CUT=datetime(2026,10,29,12,tzinfo=timezone.utc).timestamp()


def read(root,now=CUT,open_ro=ro):
    return F.read_funnel(root,now=now,host_now=CUT,open_ro=open_ro,charge=lambda n=0:None)


def test_no_history_scan_past_newest_all_stage_window(tmp_path):
    root=desk(tmp_path,[('PASS','pass',{})])
    with sqlite3.connect(root.parent/'memecoin_desk.db') as conn:
        conn.executemany("INSERT INTO desk_tickets(ticket_id,stage,verdict,reason,inputs_json,ts) VALUES(?,'vet','PASS','pass','{}','2026-10-29T11:00:00+00:00')",[(str(i),) for i in range(10000)])
    steps=[0]
    @contextmanager
    def bounded(path):
        conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
        def progress():
            steps[0]+=1000
            return int(steps[0]>10000)
        conn.set_progress_handler(progress,1000)
        try:yield conn
        finally:conn.close()
    result=read(root,open_ro=bounded)
    assert result['state']=='known' and result['limited'] is True
    assert result['tickets']==[] and sum(i['count'] for i in result['items'])==0
    assert steps[0]<=10000
    assert 'all stages' in result['coverage']


@pytest.mark.parametrize('stamp',['2026-10-29T13:00:00+00:00','2026-10-29T10:00:00','invalid'])
def test_future_naive_or_invalid_time_cannot_be_counted_known(tmp_path,stamp):
    root=desk(tmp_path,[('PASS','pass',{})])
    with sqlite3.connect(root.parent/'memecoin_desk.db') as conn:
        conn.execute("UPDATE desk_tickets SET ts=? WHERE stage='hard_filter'",(stamp,))
    result=read(root)
    assert result['state']=='unknown' and result['tickets']==[]
    assert result['items'][0]['count'] is None


def test_mixed_valid_and_future_rows_are_partial_not_complete(tmp_path):
    root=desk(tmp_path,[('PASS','pass',{}),('SKIP','unknown:chart:missing',{})])
    with sqlite3.connect(root.parent/'memecoin_desk.db') as conn:
        conn.execute("UPDATE desk_tickets SET ts='2026-10-29T13:00:00+00:00' WHERE ticket_id='t1'")
    result=read(root)
    assert result['state']=='partial' and result['omitted']==1
    assert sum(i['count'] for i in result['items'])==1 and [t['ticket_id'] for t in result['tickets']]==['t0']


def test_offset_hour_is_bucketed_after_utc_conversion(tmp_path):
    root=desk(tmp_path,[('PASS','pass',{})])
    with sqlite3.connect(root.parent/'memecoin_desk.db') as conn:
        conn.execute("UPDATE desk_tickets SET ts='2026-10-29T10:47:00+05:30' WHERE stage='hard_filter'")
    result=read(root)
    assert result['items'][0]['hour_start']=='2026-10-29T05:00:00+00:00'


@pytest.mark.parametrize('now',[None,False,float('nan')])
def test_unknown_clock_is_unknown_before_database_read(tmp_path,now):
    calls=[]
    def no_read(*a):
        calls.append(1)
        raise AssertionError('clock unknown must not read')
    result=read(tmp_path,now=now,open_ro=no_read)
    assert result['state']=='unknown' and result['items'][0]['count'] is None
    assert calls==[]
