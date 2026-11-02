"""Actual recorded seat event metadata; no process probes, writes to live state or guessed activity."""
import json,os,time
from office.test_v2_feed import fixture,event,read
from sniper.office import floor_view


def test_latest_event_and_tool_result_are_not_the_old_wake_time(tmp_path,monkeypatch):
    c=fixture(tmp_path);event(c,1,kind='lifecycle',text='wake_start',corrected_at=100)
    event(c,2,kind='tool_call',text='chain_transaction',refs_json='{"tool":"chain_transaction"}',corrected_at=180)
    event(c,3,kind='tool_result',text='{}',refs_json='{"tool":"chain_transaction"}',corrected_at=190)
    c.commit();c.close()
    r=read(tmp_path);s=r['seats']['items'][0]
    assert s['last_event_id']=='e3' and s['last_event_at']==190 and s['last_event_kind']=='tool_result'
    assert s['last_tool']=='chain_transaction' and s['tool_event_state']=='returned' and s['tool_event_at']==190
    assert s['last_event_age_s']==10


def test_sleep_deadline_and_unknown_event_time_remain_distinct(tmp_path,monkeypatch):
    c=fixture(tmp_path);event(c,1,kind='tool_call',text='chain_receipt',refs_json='{}',corrected_at=170);event(c,2,kind='tool_result',text='{}',refs_json='{"tool":"chain_receipt"}',corrected_at=179);event(c,3,kind='lifecycle',text='sleep',corrected_at=180);c.commit();c.close()
    monkeypatch.setattr(floor_view,'_host_state',lambda *a:dict(sleep_until=250))
    s=read(tmp_path)['seats']['items'][0]
    assert s['activity']['kind']=='sleeping' and s['activity']['until']==250
    assert s['last_event_at']==180 and s['last_event_age_s']==20
    assert s['last_tool']=='chain_receipt' and s['tool_event_state']=='returned'


def test_host_file_mtime_uses_host_clock_not_corrected_event_clock(tmp_path):
    path=tmp_path/'v2';path.mkdir();(path/'explore-head.json').write_text(json.dumps(dict(incarnation=3,sleep_until=250)))
    seat=dict(epoch_id='office-v2-explore-1',role='head',incarnation=3)
    assert floor_view._host_state(tmp_path,seat,100)['sleep_until']==250
    os.utime(path/'explore-head.json',(time.time()+60,time.time()+60))
    assert floor_view._host_state(tmp_path,seat,100)=={}  # Future host files still fail closed.


def test_unknown_or_future_latest_event_time_is_not_an_old_age(tmp_path):
    c=fixture(tmp_path);event(c,1,kind='lifecycle',text='wake_start',corrected_at=100)
    event(c,2,kind='thought',text='future',corrected_at=300);c.commit();c.close()
    s=read(tmp_path)['seats']['items'][0]
    assert s['last_event_id']=='e2' and s['last_event_at'] is None and s['last_event_age_s'] is None


def test_busy_timeline_failure_does_not_hide_independent_seat_observations(tmp_path,monkeypatch):
    import sqlite3
    from contextlib import contextmanager
    from sniper.office import seat_feed
    c=fixture(tmp_path);event(c,1,kind='lifecycle',text='wake_start',corrected_at=190);c.commit();c.close()
    real=seat_feed._ro;opened=[0]
    class InterruptedTimeline:
        def __init__(self,c):self.conn=c
        def execute(self,sql,*args):
            if 'office_v2_events' in sql:raise sqlite3.OperationalError('interrupted')
            return self.conn.execute(sql,*args)
    @contextmanager
    def bounded(path):
        with real(path) as c:
            opened[0]+=1;yield InterruptedTimeline(c) if opened[0]==1 else c
    monkeypatch.setattr(seat_feed,'_ro',bounded)
    result=read(tmp_path)
    assert result['events']['state']=='unknown'
    assert result['seats']['state']=='known'
    assert result['seats']['items'][0]['last_event_id']=='e1'
