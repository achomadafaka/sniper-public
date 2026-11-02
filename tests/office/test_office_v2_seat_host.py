import sqlite3

import pytest

from sniper.office import quota_measurement as Q,seat_host as H

NOW=10000.
SCOPE=dict(actor_id='head',epoch_id='p',generation=1)
COUNTS=dict(input_tokens=10,output_tokens=2,cache_read_input_tokens=9,cache_creation_input_tokens=3)


@pytest.mark.parametrize('overflow',[False,True])
def test_wake_rejects_a_result_followed_by_output_overflow(db,tmp_path,monkeypatch,overflow):
    import json,sys
    from types import SimpleNamespace
    seat=H.Seat.__new__(H.Seat)
    seat.actor='head';seat.seat=tmp_path;seat.bridge=tmp_path;seat.quota=db;seat.clock=lambda:NOW
    seat.state=dict(incarnation=1,session_id=None,per_wake_max=100)
    prompt=tmp_path/'prompt';prompt.write_text('system')
    seat.cfg=dict(epoch='p',generation=1,workspace=str(tmp_path),office_db='unused',model='test',prompt_file=str(prompt),
                  wake_deadline_s=1,max_turns=1,state_dir=str(tmp_path),account_alias='a',absolute_ceiling=200000,
                  default_sleep_s=60,sdk_version='test',usage_scope='per_wake')
    seat.guard=SimpleNamespace(spacing_s=60)
    seat._snapshot_credential=lambda:None;seat._prompt=lambda *a:'hello';seat._event=lambda *a,**k:None;seat._save=lambda:None
    fake=SimpleNamespace(start=lambda:None,stop=lambda:None,sleep_until=None,tainted=False,operator_mail=False)
    monkeypatch.setattr(H.T,'SeatTools',lambda **k:fake)
    seat.state['operator_influence']=True   # read in the wake before: this wake is labelled, the next is not
    result=dict(kind='result',session_id='00000000-0000-0000-0000-000000000000',result_id='r',usage=COUNTS,sdk_version='test',is_error=False)
    marker=tmp_path/'finished'
    code='import sys,json,pathlib; assert json.load(sys.stdin)["prompt"]=="hello"; print(%r,flush=True); '%json.dumps(result)
    if overflow:code+='sys.stdout.buffer.write(b"x"*2000000); '
    code+='pathlib.Path(%r).touch()'%str(marker)
    monkeypatch.setattr(H.B,'unit_args',lambda *a,**k:[sys.executable,'-c',code])
    monkeypatch.setattr(H.B,'harness_args',lambda *a,**k:[])
    seat.wake('mail')
    row=json.loads(db.execute('SELECT payload_json FROM quota_wakes').fetchone()[0])
    assert row['outcome']==('failed' if overflow else 'ok')
    assert fake.operator_prior is True and seat.state['operator_influence'] is False
    if overflow:assert seat.state['failures']==1 and seat.state['not_before']==NOW+60   # the backoff
    else:assert seat.state['failures']==0 and seat.state['not_before']==0
    assert row['state']==('unknown' if overflow else 'known')
    assert marker.exists() is (not overflow)


@pytest.fixture
def db():
    conn=sqlite3.connect(':memory:');Q.ensure_schema(conn);H.ensure_schema(conn);yield conn;conn.close()


def wake(conn,wake_id,*,used=None,ended=NOW-1,calibration=False,**kw):
    counts=COUNTS if used is None else dict(COUNTS,input_tokens=used,output_tokens=0,cache_creation_input_tokens=0)
    args=dict(epoch_id='p',actor_id='head',generation=1,incarnation='1',session_id='s',wake_id=wake_id,account_alias='a',outcome='ok',
              counters=counts,result_id=wake_id,started_at=ended-1,ended_at=ended,per_wake_max=100,usage_scope='per_wake',
              absolute_ceiling=200000,calibration=calibration,sdk_version='0.2.118')
    args.update(kw)
    return Q.record_wake(conn,**args)


def snapshot(conn,five=9,seven=54,at=NOW-10,alias='a'):
    Q.record_account_snapshot(conn,snapshot_id='s%s%s%s'%(five,seven,at),provider='claude',account_alias=alias,observed_at=at,
        raw=dict(five_hour=dict(utilization=five,resets_at=NOW+900),seven_day=dict(utilization=seven,resets_at=NOW+900)))


def park(conn,**kw):
    return H.park_reason(conn,**{**dict(actor_id='head',family='claude',account_alias='a',now=NOW,max_age_s=300,wakes_per_hour=12),**kw})


DONE=['mail','ticket','standup','sleep_until']*3


def _calibrate(conn,top=100):
    ids=['c%02d'%i for i in range(len(DONE))]
    for i,wake_id in enumerate(ids):
        wake(conn,wake_id,used=top if i==0 else 50,calibration=True,per_wake_max=None,ended=NOW+i)
    return ids


def test_healthy_seat_is_admitted(db):
    snapshot(db);wake(db,'w1');assert park(db) is None


def test_over_ceiling_parks_even_in_calibration(db):
    snapshot(db);wake(db,'w1',used=200001,calibration=True);assert park(db)=='absolute_ceiling_breach'


def test_cap_breach_parks(db):
    snapshot(db);wake(db,'w1',used=101);assert park(db)=='wake_cap_breach'


def test_calibration_has_only_the_ceiling(db):
    snapshot(db);wake(db,'w1',used=150000,calibration=True,per_wake_max=None);assert park(db) is None


def test_unknown_usage_parks(db):
    snapshot(db);wake(db,'w1',counters=None,outcome='timeout');assert park(db)=='wake_usage_unknown'


@pytest.mark.parametrize('kw,reason',[(dict(five=50),'account_guard_reached'),(dict(seven=80),'account_guard_reached'),
                                      (dict(at=NOW-400),'account_snapshot_stale_or_absent')])
def test_account_guard_parks(db,kw,reason):
    snapshot(db,**kw);assert park(db)==reason


def test_no_account_read_parks(db):
    assert park(db)=='account_snapshot_stale_or_absent'


def test_hourly_limit(db):
    snapshot(db)
    for i in range(12):wake(db,'w%d'%i,ended=NOW-100-i)
    assert park(db)=='hourly_wake_limit'


def test_old_wakes_do_not_count_toward_the_hour(db):
    snapshot(db)
    for i in range(12):wake(db,'w%d'%i,ended=NOW-4000-i)
    assert park(db) is None


def _codex(conn,primary=9,secondary=54,at=NOW-10):
    Q.record_account_snapshot(conn,snapshot_id='c%s%s'%(primary,secondary),provider='codex',account_alias='a',observed_at=at,
        raw=dict(ordinaryUsageAllowed=True,rateLimits=dict(primary=dict(usedPercent=primary,resetsAt=NOW+900),secondary=dict(usedPercent=secondary,resetsAt=NOW+900))))


def test_gpt_seat_reads_its_own_windows(db):
    _codex(db);assert park(db,family='codex') is None
    assert park(db,family='other')=='account_guard_unavailable'


@pytest.mark.parametrize('kw',[dict(primary=50),dict(secondary=80)])
def test_gpt_seat_parks_at_its_limits(db,kw):
    _codex(db,**kw);assert park(db,family='codex')=='account_guard_reached'


def test_claude_snapshot_does_not_clear_a_gpt_seat(db):
    snapshot(db);assert park(db,family='codex')=='account_windows_unknown'


def test_daily_budget_is_report_only_unless_given(db):
    snapshot(db);wake(db,'w1')
    assert park(db) is None and park(db,daily_limit=1)=='budget_exhausted'


def test_calibration_end_needs_count_and_every_type():
    types=['mail','ticket','standup','sleep_until']
    assert H.calibration_done(types*2)=='running' and H.calibration_done(types*3)=='done'
    assert H.calibration_done(['mail']*19)=='running' and H.calibration_done(['mail']*20)=='failed'


def test_cap_value_formula_and_inputs(db):
    ids=_calibrate(db,top=101)
    wake(db,'later',used=500,ended=NOW+50)
    assert {k:v for k,v in H.calibrated_cap(db,**SCOPE,pinned_sdk='0.2.118',wake_types=DONE).items() if k!='scope_proof'}==dict(per_wake_max=151.5,inputs=ids)


def test_calibration_wake_with_unknown_usage_gives_no_cap(db):
    _calibrate(db);wake(db,'c-last',counters=None,calibration=True,per_wake_max=None,ended=NOW+40)
    assert H.calibrated_cap(db,**SCOPE,pinned_sdk='0.2.118',wake_types=DONE+['mail']) is None


def test_breach_rate_reads_post_calibration_only(db):
    wake(db,'c1',used=9999,calibration=True,per_wake_max=None)
    for i in range(49):wake(db,'w%d'%i,ended=NOW+i)
    wake(db,'b1',used=101,ended=NOW+60)
    report=H.proof_report(db,**SCOPE,window_start=NOW,window_end=NOW+86400)
    assert (report['cohort_wakes'],report['breaches'],report['breach_fail'])==(50,1,False) and report['breach_rate']==.02
    wake(db,'b2',used=101,ended=NOW+61)
    assert H.proof_report(db,**SCOPE,window_start=NOW,window_end=NOW+86400)['breach_fail'] is True


def test_result_id_none_is_reported(db):
    wake(db,'w1',result_id=None);assert H.proof_report(db,**SCOPE,window_start=NOW-10,window_end=NOW)['result_id_none']==1


def test_no_cap_before_calibration_is_done(db):
    _calibrate(db)
    assert H.calibrated_cap(db,**SCOPE,pinned_sdk='0.2.118',wake_types=['mail'])is None
    assert H.calibrated_cap(db,**SCOPE,pinned_sdk='0.2.118',wake_types=['mail']*12) is None


def test_any_unknown_wake_fails_the_proof(db):
    for i in range(60):wake(db,'w%d'%i,ended=NOW+i*1800)
    assert H.proof_report(db,**SCOPE,window_closed=True,window_start=NOW,window_end=NOW+86400)['fail'] is False
    wake(db,'u',counters=None,outcome='timeout',ended=NOW+70)
    report=H.proof_report(db,**SCOPE,window_closed=True,window_start=NOW,window_end=NOW+86400)
    assert report['fail'] is True and report['breach_fail'] is False and report['unknown']==1


def test_old_proof_rows_stay_out(db):
    wake(db,'old',used=9000,calibration=True,per_wake_max=None,epoch_id='earlier')
    wake(db,'gen',used=8000,calibration=True,per_wake_max=None,generation=2)
    ids=_calibrate(db)
    assert {k:v for k,v in H.calibrated_cap(db,**SCOPE,pinned_sdk='0.2.118',wake_types=DONE).items() if k!='scope_proof'}==dict(per_wake_max=150.,inputs=ids)


def test_capped_read_is_no_verdict(db,monkeypatch):
    class Many:
        def execute(self,*a):return self
        def fetchall(self):return [('{}',)]*10001
    assert H.proof_report(Many(),**SCOPE)['state']=='unknown' and H.calibrated_cap(Many(),**SCOPE,pinned_sdk='0.2.118',wake_types=DONE) is None


def test_an_empty_or_unfinished_proof_never_passes(db):
    assert H.proof_report(db,**SCOPE,window_closed=True)['fail'] is True
    for i in range(24):wake(db,'w%d'%i,ended=NOW+i*3600)
    assert H.proof_report(db,**SCOPE,window_start=NOW,window_end=NOW+86400)['fail'] is True
    assert H.proof_report(db,**SCOPE,window_closed=True,window_start=NOW,window_end=NOW+86400)['fail'] is False


@pytest.mark.parametrize('closed',['false',1,None])
def test_unverified_window_flag_cannot_complete_a_proof(db,closed):
    for i in range(24):wake(db,'w%d'%i,ended=NOW+i)
    result=H.proof_report(db,**SCOPE,window_closed=closed)
    assert result['complete'] is False and result['fail'] is True


def test_24_wakes_in_one_hour_is_not_a_24_hour_proof(db):
    for i in range(24):wake(db,'w%d'%i,ended=NOW+i)
    report=H.proof_report(db,**SCOPE,window_closed=True,window_start=NOW,window_end=NOW+86400)
    assert report['fail'] is True and report['reason']=='incomplete_window' and report['hours_occupied']==1


@pytest.mark.parametrize('hours,fail',[(17,True),(18,False)])
def test_occupancy_boundary(db,hours,fail):
    for i in range(24):wake(db,'w%d'%i,ended=NOW+(i%hours)*3600+i)
    assert H.proof_report(db,**SCOPE,window_closed=True,window_start=NOW,window_end=NOW+86400)['fail'] is fail


def test_missing_window_is_a_failure(db):
    for i in range(24):wake(db,'w%d'%i,ended=NOW+i*3600)
    assert H.proof_report(db,**SCOPE,window_closed=True)['fail'] is True


def test_wakes_outside_the_window_do_not_complete_it(db):
    for i in range(18):wake(db,'w%d'%i,ended=NOW+i*3600)
    for i in range(6):wake(db,'after%d'%i,ended=NOW+90000+i)
    report=H.proof_report(db,**SCOPE,window_closed=True,window_start=NOW,window_end=NOW+86400)
    assert (report['cohort_wakes'],report['outside_wakes'],report['complete'])==(18,6,False)
    for i in range(6):wake(db,'more%d'%i,ended=NOW+i*3600+1)
    assert H.proof_report(db,**SCOPE,window_closed=True,window_start=NOW,window_end=NOW+86400)['complete'] is True


def test_park_clear_is_logged_and_only_for_operator_or_head(db):
    snapshot(db);wake(db,'w1',used=101)
    assert park(db)=='wake_cap_breach'
    with pytest.raises(ValueError):H.clear_park(db,actor_id='head',cleared_by='judge',reason='x',now=NOW)
    H.clear_park(db,actor_id='head',cleared_by='operator',reason='read it',now=NOW)
    assert park(db) is None and db.execute('SELECT cleared_by FROM seat_park_clears').fetchone()==('operator',)
    wake(db,'w2',used=101,ended=NOW+1);assert park(db)=='wake_cap_breach'


def _beat(tmp_path,age,auth=True):
    import json,os,time
    p=tmp_path/'hb';p.write_text(json.dumps(dict(auth_ok=auth)));t=time.time()-age;os.utime(p,(t,t));return p,time.time()


def test_runner_guard_parks_only_when_sustained(tmp_path):
    p,now=_beat(tmp_path,20)
    g=H.RunnerGuard(p)
    assert g.check(now) is None and g.check(now+1) is None and g.check(now+10) is None
    assert g.check(now+20)=='runner_heartbeat_stale'


def test_runner_guard_recovers_and_resets(tmp_path):
    p,now=_beat(tmp_path,20);g=H.RunnerGuard(p);g.check(now);g.check(now+10)
    _beat(tmp_path,0);assert g.check(__import__('time').time()) is None and g.over==[]


def test_runner_guard_parks_at_once_on_auth_or_unreadable(tmp_path):
    p,now=_beat(tmp_path,0,auth=False);assert H.RunnerGuard(p).check(now)=='runner_auth_not_ok'
    assert H.RunnerGuard(tmp_path/'none').check(now)=='runner_heartbeat_unreadable'
    p.write_text('{');assert H.RunnerGuard(p).check(now)=='runner_heartbeat_unreadable'


def test_lifecycle_refuses_when_held(tmp_path):
    with H.lifecycle(tmp_path):
        with pytest.raises(ValueError,match='lifecycle_busy'):
            with H.lifecycle(tmp_path):pass


def test_child_output_is_untrusted():
    events,result=H.parse_child(['not json','[1]','{"kind":"thought","text":"t"}','{"kind":"thought","text":5}','{"kind":"result","usage":1}','{"kind":"result","usage":2}'])
    assert events==['t'] and result['usage']==1


@pytest.mark.parametrize('result,scope',[(dict(sdk_version='0.2.174'),'per_wake'),(dict(sdk_version='0.2.175'),'unknown'),(dict(),'unknown'),(None,'unknown')])
def test_scope_follows_the_running_version(result,scope):
    assert H.scope_for(result,dict(usage_scope='per_wake',sdk_version='0.2.174'))==scope


def _line(kind,**kw):return __import__('json').dumps(dict(kind=kind,**kw))


U=dict(input_tokens=1,output_tokens=1,cache_creation_input_tokens=2,cache_read_input_tokens=3)


def test_calibration_wake_must_match_its_request_sums():
    cfg=dict(usage_scope='per_wake',sdk_version='v')
    good=H.parse_child([_line('request_usage',message_id='a',usage=U),_line('request_usage',message_id='a',usage=U),_line('request_usage',message_id='b',usage=U),
                        _line('result',sdk_version='v',usage=dict(U,input_tokens=2,cache_creation_input_tokens=4,output_tokens=9))])[1]
    assert H.scope_for(good,cfg,calibration=True)=='per_wake'
    last_only=H.parse_child([_line('request_usage',message_id='a',usage=U),_line('request_usage',message_id='b',usage=U),_line('result',sdk_version='v',usage=U)])[1]
    assert H.scope_for(last_only,cfg,calibration=True)=='unknown' and H.scope_for(last_only,cfg)=='per_wake'
    assert H.scope_for(H.parse_child([_line('result',sdk_version='v',usage=U)])[1],cfg,calibration=True)=='unknown'


def test_unknown_cleanup_refuses_the_next_wake_until_a_stop_succeeds(monkeypatch,tmp_path):
    s=H.Seat.__new__(H.Seat);s.actor='head';s.state=dict(cleanup_unknown=True);s.parked=None;s.undrained=None
    s._save=lambda:None;s._event=lambda *a,**k:None
    monkeypatch.setattr(H.B,'office_uid',lambda:995)
    monkeypatch.setattr(H.T,'stop_tool_units',lambda a,u:False)
    s.gate=lambda:pytest.fail('gate reached with cleanup unknown')
    assert s.tick()=='parked:tool_cleanup_unknown'
    monkeypatch.setattr(H.T,'stop_tool_units',lambda a,u:True)
    s.gate=lambda:'test_stop'
    assert s.tick()=='parked:test_stop' and s.state['cleanup_unknown'] is False


def _seat(monkeypatch,stop=True):
    s=H.Seat.__new__(H.Seat);s.actor='head';s.state=dict(cleanup_unknown=True);s.parked=None;s.undrained=None
    s._save=lambda:None;s._event=lambda *a,**k:None
    monkeypatch.setattr(H.B,'office_uid',lambda:995);monkeypatch.setattr(H.T,'stop_tool_units',lambda a,u:stop)
    return s


def test_undrained_handler_blocks_clearing_even_when_stop_succeeds(monkeypatch):
    import threading
    s=_seat(monkeypatch);s.undrained=type('T',(),{'idle':threading.Event()})()
    s.gate=lambda:pytest.fail('gate reached')
    assert s.tick()=='parked:tool_cleanup_unknown' and s.state['cleanup_unknown'] is True
    s.undrained.idle.set();s.gate=lambda:'next'
    assert s.tick()=='parked:next' and s.state['cleanup_unknown'] is False and s.undrained is None


def test_wake_and_swap_refuse_unproven_cleanup_and_parks(monkeypatch):
    s=_seat(monkeypatch,stop=False)
    with pytest.raises(ValueError,match='tool_cleanup_unknown'):s.wake('mail')
    s.gate=lambda:'absolute_ceiling_breach'
    with pytest.raises(ValueError,match='swap_parked'):s.force_swap()


def test_a_surviving_harness_unit_blocks_the_next_wake(monkeypatch):
    s=_seat(monkeypatch);s.state['harness_unit']='office-seat-head-x'
    monkeypatch.setattr(H.T,'stop_unit',lambda u,uid:False)
    with pytest.raises(ValueError,match='tool_cleanup_unknown'):s.wake('mail')
    monkeypatch.setattr(H.T,'stop_unit',lambda u,uid:True)
    assert s._cleanup_proven() and s.state['harness_unit'] is None


def test_killed_wake_still_names_its_session():
    assert H.early_session(['x','{"kind":"session","session_id":"abc"}','{"kind":"thought","text":"t"}'])=='abc'
    assert H.early_session(['{"kind":"thought","text":"t"}']) is None


def test_result_above_the_visible_requests_passes_below_fails():
    usage=dict(input_tokens=30,cache_creation_input_tokens=46650)
    assert H._requests_match({'a':dict(input_tokens=28,cache_creation_input_tokens=45706)},usage) is True   # the real wake: +2 and +944
    assert H._requests_match({'a':dict(input_tokens=31,cache_creation_input_tokens=45706)},usage) is False


def test_calibration_restart_ignores_earlier_rows(db):
    wake(db,'bad',counters=None,calibration=True,per_wake_max=None,ended=NOW-100)
    ids=_calibrate(db)
    assert H.calibrated_cap(db,**SCOPE,pinned_sdk='0.2.118',wake_types=DONE) is None
    assert {k:v for k,v in H.calibrated_cap(db,**SCOPE,pinned_sdk='0.2.118',wake_types=DONE,since=NOW).items() if k!='scope_proof'}==dict(per_wake_max=150.,inputs=ids)


def test_a_cumulative_stream_fails_the_excess_bound():
    """Second wake of a session whose counter carries the first wake's 50000."""
    assert H._requests_match({'a':dict(input_tokens=10,cache_creation_input_tokens=1000)},dict(input_tokens=60,cache_creation_input_tokens=51000)) is False


def test_cumulative_rows_have_no_scope_proof_and_give_no_cap(db):
    total=0;ids=[]
    for i in range(len(DONE)):
        total+=100
        wake(db,'c%02d'%i,counters=dict(COUNTS,input_tokens=total,output_tokens=0,cache_creation_input_tokens=total),calibration=True,per_wake_max=None,ended=NOW+i)
    assert H.calibrated_cap(db,**SCOPE,pinned_sdk='0.2.118',wake_types=DONE) is None


def test_a_falling_wake_is_recorded_as_the_scope_proof(db):
    _calibrate(db)
    proof=H.calibrated_cap(db,**SCOPE,pinned_sdk='0.2.118',wake_types=DONE)['scope_proof']
    assert proof['wake_id']=='c01' and proof['counter']=='input_tokens' and proof['after']<proof['before']


def _swap_seat(tmp_path,roster_inc=2,state_inc=2):
    from sniper.office import seat_events as E,work as W
    conn=sqlite3.connect(':memory:',isolation_level=None);W.ensure_schema(conn);E.ensure_schema(conn)
    conn.execute("INSERT INTO office_v2_roster VALUES('head','ep','head',1,?,0,1.)",(roster_inc,))
    state=tmp_path/'s.json';state.write_text(__import__('json').dumps(dict(session_id='s1',incarnation=state_inc,sleep_until=0,wake_types=[],last_standup=0)))
    cfg=dict(actor='head',epoch='ep',generation=1,host_state=str(state),heartbeat='x')
    return H.Seat(cfg=cfg,office_conn=conn,quota_conn=None,clock=lambda:5.),conn


def test_new_incarnation_moves_the_roster_with_the_host(tmp_path):
    s,conn=_swap_seat(tmp_path);s._new_incarnation()
    assert conn.execute('SELECT incarnation FROM office_v2_roster').fetchone()==(3,) and s.state['incarnation']==3 and s.state['session_id'] is None


def test_startup_adopts_the_roster_after_a_crash_mid_swap(tmp_path):
    s,_=_swap_seat(tmp_path,roster_inc=3,state_inc=2)
    assert s.state['incarnation']==3 and s.state['session_id'] is None and s.state['note_pending'] is True and s.state['post_swap'] is True


def _due_seat(db,tickets,now):
    from sniper.office import seat_events as E,work as W
    office=sqlite3.connect(':memory:',isolation_level=None);W.ensure_schema(office);E.ensure_schema(office)
    s=H.Seat.__new__(H.Seat);s.actor='head';s.quota=db;s.office=office;s.clock=lambda:now[0]
    s.cfg=dict(generation=1,wakes_per_hour=12,ticket_source=lambda after:dict(cursor=tickets[0],count=tickets[0]-after))
    s.state=dict(sleep_until=now[0]+9999,last_standup=now[0],ticket_seen=100)
    return s


def test_tickets_coalesce_to_one_wake_per_ten_minutes(db):
    now=[NOW];tickets=[105];s=_due_seat(db,tickets,now)
    assert s.due()=='ticket' and s.state['tickets_new']==5
    tickets[0]=109;now[0]+=60;assert s.due() is None
    now[0]+=540;assert s.due()=='ticket' and s.state['tickets_new']==4


def test_last_wakes_of_the_hour_are_reserved_for_mail(db):
    from sniper.office import seat_events as E
    now=[NOW];s=_due_seat(db,[105],now)
    for i in range(10):wake(db,'w%d'%i,ended=NOW-10-i)
    assert s.due() is None and s.state['ticket_seen']==100   # the ticket is not consumed, it waits
    s.office.execute("INSERT INTO office_v2_roster VALUES('head','e','head',1,1,0,1.)")
    E.operator_dm(s.office,to_actor_id='head',client_id='client-0001',text='hi',now=NOW)
    assert s.due()=='mail'


def test_hourly_park_frees_itself_without_a_clear(db):
    snapshot(db)
    for i in range(12):wake(db,'w%d'%i,ended=NOW-100-i)
    assert park(db)=='hourly_wake_limit'
    assert park(db,now=NOW+3600,max_age_s=9999)!='hourly_wake_limit'   # the fixture's account window has expired by then, a separate park
    assert db.execute('SELECT count(*) FROM seat_park_clears').fetchone()==(0,)


def test_a_fall_across_sessions_is_not_a_scope_proof(db):
    for i,(session,total) in enumerate([('a',100),('a',200),('b',100),('b',200)]):
        wake(db,'c%d'%i,session_id=session,counters=dict(COUNTS,input_tokens=total,output_tokens=0,cache_creation_input_tokens=total),calibration=True,per_wake_max=None,ended=NOW+i)
    assert H.scope_proof(H._rows(db,'head','p',1)) is None


def test_mixed_sdk_calibration_gives_no_cap(db):
    for i in range(len(DONE)):
        wake(db,'c%02d'%i,used=200 if i==0 else 100*i,calibration=True,per_wake_max=None,ended=NOW+i,sdk_version='0.2.174' if i<6 else '0.2.175')
    assert H.calibrated_cap(db,**SCOPE,pinned_sdk='0.2.175',wake_types=DONE) is None


def test_single_version_must_be_the_pinned_one(db):
    _calibrate(db)
    assert H.calibrated_cap(db,**SCOPE,pinned_sdk='0.2.999',wake_types=DONE) is None
    assert H.calibrated_cap(db,**SCOPE,pinned_sdk='0.2.118',wake_types=DONE) is not None


def test_clean_wakes_after_the_window_cannot_reverse_a_failure(db):
    for i in range(23):wake(db,'w%d'%i,ended=NOW+i*3600)
    wake(db,'b',used=101,ended=NOW+23*3600)
    first=H.proof_report(db,**SCOPE,window_closed=True,window_start=NOW,window_end=NOW+86400)
    assert first['fail'] is True and first['known']==24
    for i in range(100):wake(db,'later%d'%i,ended=NOW+90000+i)
    again=H.proof_report(db,**SCOPE,window_closed=True,window_start=NOW,window_end=NOW+86400)
    assert again['fail'] is True and again['breach_rate']==first['breach_rate'] and again['outside_wakes']==100


def test_bounds_are_start_inclusive_end_exclusive(db):
    wake(db,'start',ended=NOW);wake(db,'end',ended=NOW+3600)
    report=H.proof_report(db,**SCOPE,window_start=NOW,window_end=NOW+3600)
    assert (report['cohort_wakes'],report['outside_wakes'])==(1,1)


@pytest.mark.parametrize('kw',[dict(),dict(window_start=NOW),dict(window_end=NOW),dict(window_start=NOW,window_end=NOW),dict(window_start=NOW,window_end=float('nan')),
                                dict(window_start=NOW,window_end=float('inf')),dict(window_start=float('-inf'),window_end=NOW),dict(window_start=0,window_end=1e300),dict(window_start=NOW,window_end=10**400)])
def test_report_without_both_bounds_is_unknown(db,kw):
    report=H.proof_report(db,**SCOPE,**kw)
    assert report['state']=='unknown' and report['fail'] is True


def test_unknown_or_ceiling_outside_the_window_still_fails(db):
    for i in range(24):wake(db,'w%d'%i,ended=NOW+i*3600)
    assert H.proof_report(db,**SCOPE,window_closed=True,window_start=NOW,window_end=NOW+86400)['fail'] is False
    wake(db,'late',counters=None,outcome='timeout',ended=NOW+90000)
    report=H.proof_report(db,**SCOPE,window_closed=True,window_start=NOW,window_end=NOW+86400)
    assert report['fail'] is True and report['outside_unknown']==1


def test_cap_breach_outside_is_shown_but_not_in_the_rate(db):
    for i in range(24):wake(db,'w%d'%i,ended=NOW+i*3600)
    wake(db,'late',used=101,ended=NOW+90000)
    report=H.proof_report(db,**SCOPE,window_closed=True,window_start=NOW,window_end=NOW+86400)
    assert report['outside_breaches']==['late'] and report['breach_rate']==0 and report['fail'] is False


@pytest.mark.parametrize('kw,field',[(dict(used=200001),'calibration_over_ceiling'),(dict(counters=None,outcome='timeout'),'calibration_unknown')])
def test_a_bad_calibration_wake_fails_the_report(db,kw,field):
    for i in range(24):wake(db,'w%d'%i,ended=NOW+i*3600)
    wake(db,'c',calibration=True,per_wake_max=None,ended=NOW-500,**kw)
    report=H.proof_report(db,**SCOPE,window_closed=True,window_start=NOW,window_end=NOW+86400)
    assert report['fail'] is True and report[field]==1 and report['cohort_wakes']==24


def test_window_span_limit_is_31_days(db):
    assert H.proof_report(db,**SCOPE,window_start=NOW,window_end=NOW+31*86400)['state']=='known'
    assert H.proof_report(db,**SCOPE,window_start=NOW,window_end=NOW+31*86400+1)['state']=='unknown'


def test_manifest_fits_the_event_and_marks_truncation():
    doc=H.workspace_manifest('12 NOTEBOOK.md\nbad\n7 tools/a.py\n','note')
    assert doc['files']==[dict(path='NOTEBOOK.md',size_bytes=12),dict(path='tools/a.py',size_bytes=7)] and doc['state']=='known' and doc['truncated'] is False
    big=H.workspace_manifest('\n'.join('1 f%05d.py'%i for i in range(5000)),'n'*5000)
    assert len(__import__('json').dumps(big).encode())<=4096 and big['state']=='partial' and big['truncated'] is True and len(big['notebook_text'])==1200


def test_manifest_budget_counts_escaping():
    doc=H.workspace_manifest('','\U0001F600'*1200)
    assert len(__import__('json').dumps(doc).encode())<=4096 and doc['truncated'] is True and doc['state']=='partial'


def test_seat_paths_are_never_read_outside_the_sandbox(monkeypatch):
    s=H.Seat.__new__(H.Seat);s.actor='head';s.cfg=dict(workspace='/w');s.bridge=H.B.BRIDGE
    seen=[];monkeypatch.setattr(H.subprocess,'run',lambda argv,**k:(seen.append(argv),type('R',(),{'returncode':0,'stdout':b'x'})())[1])
    assert s._read_note()=='x'
    assert '/usr/bin/bwrap' in seen[0] and '--unshare-net' in seen[0] and not any('credential' in a for a in seen[0])


@pytest.mark.parametrize('listing_rc,notebook_rc,state,truncated',[(0,0,'known',False),(141,0,'partial',True),(0,1,'partial',False),(2,0,'unknown',None)])
def test_publish_reports_a_failed_or_cut_read(monkeypatch,listing_rc,notebook_rc,state,truncated):
    s=H.Seat.__new__(H.Seat);seen=[];s.state={};s._save=lambda:None
    s._event=lambda kind,text,**k:seen.append(k)
    results=iter([(listing_rc,b'1 a.py\n'),(notebook_rc,b'n')])
    s._in_sandbox=lambda *a,**k:(lambda r:type('R',(),{'returncode':r[0],'stdout':r[1]})())(next(results))
    s._publish_workspace()
    assert seen[0]['state']==state
    if truncated is not None:assert seen[0]['refs']['truncated'] is truncated


def test_a_full_length_listing_reads_as_cut_even_with_status_zero(monkeypatch):
    monkeypatch.setattr(H,'LISTING_BYTES',8)
    s=H.Seat.__new__(H.Seat);seen=[];s.state={};s._save=lambda:None;s._event=lambda kind,text,**k:seen.append(k)
    results=iter([(0,b'1 a.py\n1'),(0,b'n')])
    s._in_sandbox=lambda *a,**k:(lambda r:type('R',(),{'returncode':r[0],'stdout':r[1]})())(next(results))
    s._publish_workspace()
    assert seen[0]['state']=='partial' and seen[0]['refs']['truncated'] is True


@pytest.mark.parametrize('kw',[dict(primary_max=float('nan')),dict(secondary_max=float('nan')),dict(primary_max=0),dict(secondary_max=101)])
def test_codex_guard_refuses_bad_limits(db,kw):
    _codex(db,primary=99,secondary=99)
    out=Q.codex_account_guard(db,account_alias='a',now=NOW,max_age_s=300,**kw)
    assert out['allowed'] is False and out['reason']=='account_guard_input_unknown'


def test_corrupt_host_state_reads_tainted_and_missing_reads_fresh(tmp_path):
    s=H.Seat.__new__(H.Seat);s.state_path=tmp_path/'s.json'
    assert 'tainted_by_web' not in s._load()
    s.state_path.write_text('{broken');assert s._load()['tainted_by_web'] is True


def test_seats_share_the_lifecycle_lock_and_publish_excludes_them(tmp_path):
    with H.lifecycle(tmp_path,shared=True):
        with H.lifecycle(tmp_path,shared=True):pass
        with pytest.raises(ValueError,match='lifecycle_busy'):
            with H.lifecycle(tmp_path):pass
    with H.lifecycle(tmp_path):
        with pytest.raises(ValueError,match='lifecycle_busy'):
            with H.lifecycle(tmp_path,shared=True):pass


def test_all_three_configs_name_the_transition_actors():
    import json
    from sniper.office import v2_explore as X
    for role in ('head','judge','scout'):
        cfg=json.load(open('config/office/seat_%s_v2.json'%role))
        assert cfg['actor']==X.actor_id(role) and cfg['epoch']==X.NEW and cfg['family']==dict(head='codex').get(role,'claude') and len({cfg['workspace'],cfg['state_dir']})==2 and role in cfg['workspace']


def test_a_swap_labels_only_the_next_wake(tmp_path):
    s,conn=_swap_seat(tmp_path);s._new_incarnation()
    assert s.state['post_swap'] is True


def test_a_failed_desk_read_durably_clears_the_old_hash(monkeypatch):
    s=H.Seat.__new__(H.Seat);s.state=dict(workspace_hash='old');saved=[];s._save=lambda:saved.append(dict(s.state));s._event=lambda *a,**k:None
    s._in_sandbox=lambda *a,**k:type('R',(),{'returncode':2,'stdout':b''})()
    s._publish_workspace()
    assert saved[-1]['workspace_hash']=='unknown'


def _failed_snapshot(conn,at):
    Q.record_account_snapshot(conn,snapshot_id='fail%s'%at,provider='claude',account_alias='a',observed_at=at,raw={},error='rate_limited')


def test_one_rate_limited_poll_does_not_park_while_a_fresh_reading_exists(db):
    snapshot(db,at=NOW-60);_failed_snapshot(db,NOW-5)
    assert park(db) is None


def test_sustained_failure_still_parks_and_a_high_reading_still_wins(db):
    snapshot(db,at=NOW-400);_failed_snapshot(db,NOW-5)
    assert park(db)=='account_windows_unknown'
    snapshot(db,five=60,at=NOW-60);_failed_snapshot(db,NOW-4)
    assert park(db)=='account_guard_reached'


def test_a_newer_partial_high_reading_is_never_passed_over(db):
    snapshot(db,five=20,seven=20,at=NOW-60)
    Q.record_account_snapshot(db,snapshot_id='partial',provider='claude',account_alias='a',observed_at=NOW-1,
        raw=dict(five_hour=dict(utilization=99,resets_at=NOW+900)))
    assert park(db)=='account_guard_reached'
    assert Q.account_guard(db,account_alias='a',now=NOW,max_age_s=300)['allowed'] is False


def test_a_newer_partial_low_reading_parks_unknown_and_a_newer_whole_one_allows(db):
    snapshot(db,five=20,seven=20,at=NOW-60)
    Q.record_account_snapshot(db,snapshot_id='partial',provider='claude',account_alias='a',observed_at=NOW-2,
        raw=dict(five_hour=dict(utilization=5,resets_at=NOW+900)))
    assert park(db)=='account_windows_unknown'
    snapshot(db,five=20,seven=20,at=NOW-1)
    assert park(db) is None


def test_a_failed_poll_falls_back_only_within_the_nine_hundred_second_bound(db):
    snapshot(db,at=NOW-800);_failed_snapshot(db,NOW-5)
    assert park(db,max_age_s=900) is None
    assert park(db,max_age_s=900,now=NOW+200)=='account_windows_unknown'


def test_uncapped_has_no_account_guard_and_no_wake_parks(db):
    snapshot(db)
    for i in range(50):wake(db,'w%d'%i,ended=NOW-100-i)
    assert park(db)=='hourly_wake_limit' and park(db,uncapped=True,wakes_per_hour=None) is None
    snapshot(db,five=60,at=NOW-1)
    assert park(db)=='account_guard_reached' or park(db) is not None
    assert park(db,uncapped=True,wakes_per_hour=None) is None   # operator order 2026-10-31


def test_a_failing_seat_backs_off_even_for_mail(db):
    from sniper.office import seat_events as E
    now=[NOW];s=_due_seat(db,[105],now)
    s.office.execute("INSERT INTO office_v2_roster VALUES('head','e','head',1,1,0,1.)")
    E.operator_dm(s.office,to_actor_id='head',client_id='client-0001',text='hi',now=NOW)
    s.state['not_before']=NOW+60
    assert s.due() is None
    now[0]=NOW+61;assert s.due()=='mail'


def test_every_seat_config_is_uncapped_with_the_guards_kept():
    import json
    for role in ('head','judge','scout'):
        cfg=json.load(open('config/office/seat_%s_v2.json'%role))
        assert cfg['uncapped'] is True and cfg['account_max_age_s']==900 and cfg['wake_deadline_s']==3600


def test_an_uncapped_wake_records_its_tokens_as_known(db):
    doc=Q.record_wake(db,epoch_id='p',actor_id='head',generation=1,incarnation='1',session_id='s',wake_id='w-unc',account_alias='a',outcome='ok',counters=COUNTS,result_id='r',started_at=NOW-5,ended_at=NOW,per_wake_max=None,usage_scope='per_wake',absolute_ceiling=200000,calibration=False,sdk_version='test',uncapped=True)
    assert doc['state']=='known' and doc['used'] is not None and doc['over_cap'] is False


def test_a_pass_row_during_a_wake_is_seen_by_the_next_check(db):
    now=[NOW];s=_due_seat(db,[105],now)
    s.state['ticket_seen']=100;s.state['sleep_until']=NOW+9999;s.state['last_standup']=NOW;s.cfg['uncapped']=True
    assert s.due()=='ticket' and s.state['ticket_seen']==105
    s.cfg['ticket_source']=lambda after:dict(cursor=106,count=106-after)
    assert s.due()=='ticket' and s.due() is None


def _rotating(tmp_path,notes):
    s,conn=_swap_seat(tmp_path)
    s.state.update(results_asof=7.,results_reads=2,rotate_due=True)
    s._read_note=lambda:notes.pop(0)
    s.wake=lambda *a,**k:None
    s._event=lambda *a,**k:None
    return s,conn


def test_rotation_keeps_label_state_and_moves_the_incarnation(tmp_path):
    from sniper.office import seat_events as E
    s,conn=_rotating(tmp_path,[None,'note'])
    E.operator_dm(conn,to_actor_id='head',client_id='client-0001',text='hi',now=1.)
    assert s.rotate()=='rotated'
    assert s.state['session_id'] is None and s.state['incarnation']==3 and s.state['note_pending'] is True and s.state['rotate_due'] is False
    assert s.state['results_asof']==7. and s.state['results_reads']==2
    assert E.pending_mail(conn,actor_id='head',generation=1)==1


def test_no_fresh_note_keeps_the_old_session(tmp_path):
    s,conn=_rotating(tmp_path,['same','same'])
    assert s.rotate()=='kept_old_session' and s.state['session_id']=='s1' and s.state['incarnation']==2
    s2,_=_rotating(tmp_path,[None,None])
    assert s2.rotate()=='kept_old_session'


def test_every_seat_rotates_at_the_same_size():
    import json
    assert {json.load(open('config/office/seat_%s_v2.json'%r))['rotate_cache_read'] for r in ('head','judge','scout','researcher','forensics','narrative')}=={800000}


def test_three_asks_without_a_note_back_off_an_hour(tmp_path):
    s,conn=_rotating(tmp_path,['same']*6)
    for _ in range(3):s.rotate()
    assert s.state['rotate_not_before']==5.+3600 and s.state['swap_asks_without_note']==0


def test_rotation_waits_for_a_pending_order_and_for_an_unreadable_ledger(tmp_path):
    s,conn=_rotating(tmp_path,[])
    db=tmp_path/'l.db';c=sqlite3.connect(db);c.execute('CREATE TABLE orders(state TEXT)');c.execute("INSERT INTO orders VALUES('pending')");c.commit()
    s.cfg['ledger_db']=str(db);assert s._order_pending() is True
    c.execute("UPDATE orders SET state='open'");c.commit();assert s._order_pending() is False
    s.cfg['ledger_db']=str(tmp_path/'none.db');assert s._order_pending() is True
    s.cfg.pop('ledger_db');assert s._order_pending() is False


import pytest as _pytest


@_pytest.mark.parametrize('after',[dict(failures=1),dict(cleanup_unknown=True)])
def test_a_failed_or_cut_off_swap_keeps_the_old_session_even_with_a_fresh_note(tmp_path,after):
    s,conn=_rotating(tmp_path,[None,'partial'])
    s.wake=lambda *a,**k:s.state.update(after)
    assert s.rotate()=='kept_old_session' and s.state['session_id']=='s1' and s.state['incarnation']==2


def test_a_clean_swap_with_a_fresh_note_rotates(tmp_path):
    s,conn=_rotating(tmp_path,[None,'note'])
    s.wake=lambda *a,**k:s.state.update(failures=0)
    assert s.rotate()=='rotated'


def test_a_failed_outcome_keeps_the_session_and_an_order_opened_during_the_swap_delays_the_bump(tmp_path):
    s,conn=_rotating(tmp_path,[None,'partial'])
    s.wake=lambda *a,**k:dict(outcome='timeout')
    assert s.rotate()=='kept_old_session' and s.state['incarnation']==2
    s,conn=_rotating(tmp_path,[None,'note'])
    s.wake=lambda *a,**k:dict(outcome='ok')
    pending=[True]
    s._order_pending=lambda:pending[0]
    assert s.rotate()=='waiting_for_order' and s.state['incarnation']==2 and s.state['rotate_due'] is True
    pending[0]=False
    s.wake=lambda *a,**k:1/0   # no second swap wake
    assert s.rotate()=='rotated' and s.state['incarnation']==3


def test_a_cleared_park_is_recorded_once_with_no_wake_due(tmp_path):
    s,conn=_swap_seat(tmp_path)
    s.state['parked_reason']='account_window_expired_or_unknown'
    events=[];refs=[];s._event=lambda *a,**k:(events.append(a),refs.append(k.get('refs')))
    s._cleanup_proven=lambda:True;s.parked=None;s.gate=lambda:None;s.due=lambda:None
    assert s.tick()=='idle' and s.state['parked_reason'] is None and events==[('lifecycle','unparked')] and refs[0]['was']=='account_window_expired_or_unknown'
    assert s.tick()=='idle' and len(events)==1


def test_codex_token_park_follows_the_file(tmp_path):
    import base64,json
    from sniper.office import seat_host as SH
    path=tmp_path/'auth.json'
    def write(exp):
        part=base64.urlsafe_b64encode(json.dumps(dict(exp=exp)).encode()).decode().rstrip('=')
        path.write_text(json.dumps(dict(tokens=dict(access_token='a.%s.c'%part,account_id='acct'))))
    write(1000+3599);assert SH.codex_token_state(path,1000)==('codex_token_near_expiry',4599)
    write(1000+3600);assert SH.codex_token_state(path,1000)==(None,4600)   # a newer expiry resumes
    path.write_text('{');assert SH.codex_token_state(path,1000)==('codex_token_unknown',None)


def test_ticket_wake_uses_pass_delta_not_rowid_distance(db):
    import json
    now=[NOW];s=_due_seat(db,[105],now);seen=[]
    delta=dict(cursor=105,count=1,observed_count=1,truncated=False,baseline=False,
               tickets=[dict(rowid=105,ticket_id='new-pass',stage='hard_filter',verdict='PASS',reason='ok')])
    s.cfg['ticket_source']=lambda after:seen.append(after) or delta
    assert s.due()=='ticket' and seen==[100] and s.state['tickets_new']==1
    frame=json.loads(s._prompt('ticket').split('Wake frame (host data): ')[1])
    assert frame['ticket_delta']==delta
    assert frame['new_tickets']==1
    assert json.loads(s._prompt('mail').split('Wake frame (host data): ')[1])['ticket_delta'] is None


def test_unreadable_ticket_delta_preserves_cursor_and_no_signal(db):
    s=_due_seat(db,[105],[NOW]);s.cfg['ticket_source']=lambda after:None
    assert s.due() is None and s.state['ticket_seen']==100
    assert 'ticket_delta' not in s.state


def test_postmortem_frame_delivers_once_and_lessons_load_as_data(monkeypatch):
    import types
    from sniper.office import seat_host as SH
    calls=[]
    host=SH.Seat.__new__(SH.Seat)
    host.cfg=dict(ticket_wakes=True,generation=1,paper=types.SimpleNamespace(postmortems=lambda **k:(calls.append(k),[dict(order_id='o')])[1]))
    host.state=dict();host.clock=lambda:100.;host.actor='a';host.office=None
    monkeypatch.setattr(SH.E,'pending_mail',lambda *a,**k:0)
    monkeypatch.setattr(host,'_in_sandbox',lambda *a,**k:types.SimpleNamespace(returncode=0,stdout=b'lesson one'),raising=False)
    assert host._postmortems(100.)==[dict(order_id='o')] and host._postmortems(101.) is None   # polled, not on every tick
    host.state['postmortem_due']=[dict(order_id='o')]
    prompt=host._prompt('postmortem')
    assert '"order_id": "o"' in prompt and '<lessons>\nlesson one\n</lessons>' in prompt and host.state['postmortem_seen']==['o'] and 'postmortem_due' not in host.state


def test_rotation_reads_session_size_not_the_wake_total():
    from sniper.office.seat_host import session_grown as G
    assert G(800000,8,800000,300000)==(False,False)      # 100k context, 8 requests: the old rule rotated this
    assert G(900000,3,800000,300000)==(True,False)
    assert G(900000,None,800000,300000)==(True,True) and G(900000,0,800000,300000)==(True,True)    # no count: old rule, logged
    assert G(None,3,800000,300000)==(False,False) and G(100,None,800000,300000)==(False,False)


def test_support_seats_have_a_standing_job_and_no_idle_instruction():
    import json
    from pathlib import Path
    for role in ('scout','researcher','narrative'):
        text=Path('config/office/prompts/office-v2/%s.md'%role).read_text()
        assert 'Standing job' in text and 'nothing-found' in text and 'a wake costs' not in text
        assert json.load(open('config/office/seat_%s_v2.json'%role))['max_sleep_s']>=300


def test_position_watch_fires_once_and_playbook_loads_flagged(monkeypatch):
    import types
    from sniper.office import seat_host as SH
    host=SH.Seat.__new__(SH.Seat)
    host.cfg=dict(ticket_wakes=True,generation=1,paper=types.SimpleNamespace(position_triggers=lambda **k:[dict(order_id='o',trigger='mark_fall')] if not k['seen'] else []))
    host.state=dict();host.clock=lambda:100.;host.actor='a';host.office=None
    monkeypatch.setattr(SH.E,'pending_mail',lambda *a,**k:0)
    book=b'- good rule | cases=3 | last_check=x\n- bare rule\n'
    monkeypatch.setattr(host,'_in_sandbox',lambda *a,**k:types.SimpleNamespace(returncode=0,stdout=book if 'PLAYBOOK' in a[-1] else b''),raising=False)
    assert host._watch(100.)==[dict(order_id='o',trigger='mark_fall')] and host._watch(101.) is None
    host.state['watch_due']=[dict(order_id='o',trigger='mark_fall')]
    prompt=host._prompt('watch')
    assert host.state['watch_seen']==['o:mark_fall'] and 'one-slot crash' in prompt and 'good rule' in prompt and '1 rules show no cases' in prompt


def test_an_oversized_handover_is_refused_then_the_old_session_stays():
    from sniper.office import seat_host as SH
    host=SH.Seat.__new__(SH.Seat);host.state=dict(rotate_due=True);host.actor='a';asks=[]
    host._read_note=lambda:'n'+str(len(asks));host._note_bytes=lambda:SH.HANDOVER_BYTES+1
    host.wake=lambda *a,**k:(asks.append(k.get('extra')),dict(outcome='ok'))[1]
    host._event=lambda *a,**k:None;host._save=lambda:None;host._order_pending=lambda:False;host.clock=lambda:1.
    assert host.rotate()=='kept_old_session' and len(asks)==2 and 'refused' in asks[1]


def test_force_swap_refuses_a_note_over_the_cap():
    from sniper.office import seat_host as SH
    host=SH.Seat.__new__(SH.Seat);host.gate=lambda:None;asks=[]
    host.wake=lambda *a,**k:asks.append(k);host._read_note=lambda:'n';host._note_bytes=lambda:SH.HANDOVER_BYTES+1;host._event=lambda *a,**k:None
    host._new_incarnation=lambda:asks.append('bumped')
    with pytest.raises(ValueError,match='swap_handover_over_cap'):host.force_swap()
    assert 'bumped' not in asks and len(asks)==2


def test_notebook_keeps_its_file_when_compression_fails(tmp_path):
    import subprocess,types,stat
    from sniper.office import seat_host as SH
    work=tmp_path/'w';work.mkdir();(work/'NOTEBOOK.md').write_bytes(b'x'*(SH.NOTEBOOK_BYTES+1))
    (work/'notebook-archive').mkdir();old=work/'notebook-archive/old.md.gz';old.write_bytes(b'o')
    bin_=tmp_path/'bin';bin_.mkdir();g=bin_/'gzip';g.write_text('#!/bin/sh\nexit 1\n');g.chmod(0o755)
    host=SH.Seat.__new__(SH.Seat)
    def run(*argv):
        return subprocess.run([argv[0],argv[1],argv[2].replace('/opt/workspace',str(work))],env=dict(PATH=str(bin_)+':/usr/bin:/bin'),capture_output=True)
    host._own_read=run
    host._rotate_notebook()
    assert (work/'NOTEBOOK.md').stat().st_size==SH.NOTEBOOK_BYTES+1 and old.exists() and not list((work/'notebook-archive').glob('*.tmp'))


def test_seat_start_creates_order_tables_for_an_epoch_already_open():
    from pathlib import Path
    source=Path('scripts/office_seat_host.py').read_text()
    assert source.index('VO.ensure_schema(office)')<source.index("if cfg.get('ledger_db'):cfg['paper']=build_paper")
    import sqlite3
    from sniper.office import v2_orders as VO
    c=sqlite3.connect(':memory:');VO.ensure_schema(c);VO.ensure_schema(c)
    assert {'office_v2_feed_filters','office_v2_feed_seen','office_v2_feed_hidden'}<={r[0] for r in c.execute("SELECT name FROM sqlite_master")}


def test_researcher_is_handed_tickets_and_postmortem_asks_what_was_knowable():
    import json
    from pathlib import Path
    cfg=json.load(open('config/office/seat_researcher_v2.json'))
    assert cfg['ticket_wakes'] is True and cfg['ticket_wake_min_s']==300 and cfg['max_sleep_s']==7200
    source=Path('sniper/office/seat_host.py').read_text()
    assert 'knowable at entry' in source and 'winners killed' in source
    assert 'verified mint link' in Path('config/office/prompts/office-v2/narrative.md').read_text()
