"""Persistent entry safety is separate from paper exit rails and evaluation efficacy."""
import json
from pathlib import Path
import sqlite3

import pytest

from sniper.office import ledger as L
from office.test_ledger import book, policy, NOW, FX, MINT, CLOCK, manage

CONFIG={'version':'E1','drawdown_usd':200.,'unknown_max_s':86400.,'mark_max_age_s':60.,
        'clock_skew_tolerance_s':.125,'leg_stale_demote_s':1800.}

@pytest.fixture(autouse=True)
def _synthetic_epoch_has_a_kill_rule(monkeypatch):
    # These stores use the synthetic epoch E1; the ledger enforces its kill rule only for listed epochs.
    from sniper.office import ledger as _L
    monkeypatch.setattr(_L,'EVAL_EPOCHS',_L.EVAL_EPOCHS+('E1',))



@pytest.fixture
def safety(book,monkeypatch):
    conn,quotes,path=book
    conn.execute('UPDATE ledger_epochs SET note=?',(json.dumps({'paper_eval':CONFIG}),))
    sample={'state':'known','equity':1000.}
    def equity(conn,**kw):
        assert kw['epoch_id']=='E1' and kw['kind']=='evaluated' and kw['require_proven'] is True
        assert kw['mark_max_age_s']==60.
        return L.Equity(sample['equity'] if sample['state']=='known' else 1000.,
            0. if sample['state']=='known' else None,0 if sample['state']=='known' else 1,
            0 if sample['state']=='known' else 100,sample['state'])
    monkeypatch.setattr(L,'equity',equity)
    return conn,quotes,path,sample


def observe(safety,now=NOW):
    return L.evaluation_safety(safety[0],epoch_id='E1',now=now,sol_usd=FX,sol_at=now)


def submit(conn,order='new',now=NOW):
    return L.submit(conn,policy(),book_id='A',order_id=order,network_id='solana',mint=MINT,pool='P',
                    decision_bundle_hash='b'*64,now=now,sol_usd=FX,sol_at=now)


def test_drawdown_exact_threshold_latches_and_survives_restart(safety):
    assert observe(safety)['state']=='known'
    safety[3]['equity']=801.
    assert observe(safety,NOW+1)['state']=='known'
    safety[3]['equity']=800.
    out=observe(safety,NOW+2)
    assert out['state']=='halted' and out['reason']=='epoch_drawdown_halt'
    safety[3]['equity']=1200.
    assert observe(safety,NOW+3)['state']=='halted'
    conn=L.open_ledger(safety[2]/'office'/'ledger.db')
    try:assert submit(conn,now=NOW+4).reason=='epoch_drawdown_halt'
    finally:conn.close()


@pytest.mark.parametrize('state',['partial','unknown'])
def test_unknown_equity_is_not_zero_and_timeout_is_strictly_more_than_day(safety,state):
    observe(safety)
    safety[3]['state']=state
    out=observe(safety,NOW+1)
    assert out['state']==state and out['peak_equity_usd']==1000. and out['equity_usd'] is None
    assert out['unknown_since']==NOW
    assert observe(safety,NOW+86400)['state']==state
    # Recovery at the next sample must not erase an already elapsed unknown interval.
    safety[3]['state']='known'
    out=observe(safety,NOW+86401)
    assert out['state']=='halted' and out['reason']=='epoch_equity_unknown_timeout'


def test_known_sample_resets_short_unknown_interval_without_lowering_peak(safety):
    observe(safety)
    safety[3]['state']='unknown';observe(safety,NOW+1)
    safety[3].update(state='known',equity=950.)
    out=observe(safety,NOW+10)
    assert out['unknown_since'] is None and out['peak_equity_usd']==1000.


def test_manual_halt_censors_pending_but_open_order_exit_still_works(safety):
    conn,quotes,_,sample=safety
    assert submit(conn).state=='pending'
    manage(conn,policy(),NOW+L.ENTRY_DELAY_S+1)
    assert conn.execute('SELECT state FROM orders').fetchone()[0]=='open'
    L.halt_evaluation_entries(conn,epoch_id='E1',reason='operator',actor='crew',now=NOW+10)
    row=conn.execute('SELECT post_halt,epoch_halted_at FROM orders').fetchone()
    assert tuple(row)==(1,NOW+10)
    assert conn.execute('SELECT state FROM orders').fetchone()[0]=='open'
    assert submit(conn,'blocked',NOW+11).reason=='epoch_manual_halt:operator'
    assert L.request_exit(conn,order_id='new',reason='exit',requested_by='judge',now=NOW+11,exit_by='judge').ok


def test_manage_rechecks_kill_before_pending_fill_and_preserves_no_fill_evidence(safety):
    conn,quotes,_,sample=safety
    assert submit(conn).state=='pending'
    sample['equity']=799.
    manage(conn,policy(),NOW+L.ENTRY_DELAY_S+1)
    row=conn.execute('SELECT state,reason,filled_at FROM orders').fetchone()
    assert tuple(row)==('censored','epoch_drawdown_halt',None) and quotes.calls==[]


@pytest.mark.parametrize('change',['changed','removed','bad'])
def test_frozen_kill_config_cannot_change_or_disappear(safety,change):
    observe(safety)
    cfg=dict(CONFIG,drawdown_usd=199.) if change=='changed' else None
    note=json.dumps({'paper_eval':cfg}) if change!='bad' else 'broken'
    safety[0].execute('UPDATE ledger_epochs SET note=?',(note,))
    assert observe(safety,NOW+1)['state']=='halted'
    assert submit(safety[0],now=NOW+2).state=='refused'


def test_regressed_clock_pauses_reversibly_and_unknown_clock_cannot_write_a_timestamp(safety):
    out=observe(safety)
    assert observe(safety,None)['state']=='unknown'
    row=safety[0].execute('SELECT last_at FROM epoch_entry_safety').fetchone()
    assert row[0]==NOW
    out=observe(safety,NOW-1)
    assert out['state']=='unknown' and out['reason']=='epoch_clock_regressed'
    assert observe(safety,NOW+1)['state']=='known'


def test_small_cross_process_sample_reordering_uses_monotone_effective_time(safety):
    """1075 last-day supervisor offsets vs ONE retained watcher offset: p99 .0168421s,
    max .0919318s. .125s >= that envelope; NOT paired dispatch jitter evidence.
    /home/bigpapi/.cache/delta-office-clock-offset-envelope-20261029.json.
    The 1800s stale-demotion window is configured, unmeasured (Gamma Am8).
    """
    observe(safety,NOW+.6)  # watcher won the write lock after runtime sampled .5
    assert observe(safety,NOW+.5)['state']=='known'
    assert L.evaluation_safety_state(safety[0],epoch_id='E1')['last_at']==NOW+.6


def test_first_peak_is_initial_capital_and_dead_watcher_timeout_cannot_be_reset(safety):
    safety[3]['equity']=799.
    assert observe(safety)['reason']=='epoch_drawdown_halt'


def test_dead_watcher_halts_before_accepting_a_new_known_sample(safety):
    out=observe(safety,NOW+86401)
    assert out['state']=='halted' and out['halt_kind']=='unknown_equity'


def test_submit_observes_safety_inside_its_ledger_write_transaction(safety,monkeypatch):
    equity=L.equity
    def checked(conn,**kw):
        assert conn.in_transaction
        return equity(conn,**kw)
    monkeypatch.setattr(L,'equity',checked)
    assert submit(safety[0]).state=='pending'
    safety[3]['equity']=799.
    assert submit(safety[0],'second',NOW+1).reason=='epoch_drawdown_halt'


@pytest.mark.parametrize('kind',['zero_tolerance','funded_boundary'])
def test_auditor_latch_is_typed_and_read_does_not_mutate(safety,kind):
    conn=safety[0]
    L.halt_evaluation_entries(conn,epoch_id='E1',kind=kind,reason='counter',actor='runtime',now=NOW)
    before=conn.total_changes
    row=L.evaluation_safety_state(conn,epoch_id='E1')
    assert row['halt_kind']==kind and row['halt_actor']=='runtime' and row['state']=='halted'
    assert conn.total_changes==before


def test_real_unproven_mark_pauses_and_fresh_proven_mark_recovers(book):
    conn,quotes,_=book
    conn.execute('UPDATE ledger_epochs SET note=?',(json.dumps({'paper_eval':CONFIG}),))
    assert submit(conn).state=='pending'
    manage(conn,policy(),NOW+L.ENTRY_DELAY_S+1)
    out=L.evaluation_safety(conn,epoch_id='E1',now=NOW+5,sol_usd=FX,sol_at=NOW)
    assert out['state']=='unknown' and out['equity_usd'] is None and out['peak_equity_usd']==1000.
    order=conn.execute('SELECT * FROM orders').fetchone()
    quote={'binding_kind':'intake_canonical_pda','lamports':order['spend_lamports'],'pool':'P',
           'clock':{'host_at':NOW+6,'offset_s':0.},'proof':{'committed_through':NOW+6}}
    conn.execute('INSERT INTO marks(order_id,at,lamports,source,binding_kind,quote_proof) VALUES(?,?,?,?,?,?)',
        ('new',NOW+6,order['spend_lamports'],'ps_paper.sell','intake_canonical_pda',json.dumps(quote)))
    out=L.evaluation_safety(conn,epoch_id='E1',now=NOW+6,sol_usd=FX,sol_at=NOW)
    assert out['state']=='known' and out['unknown_since'] is None
    out=L.evaluation_safety(conn,epoch_id='E1',now=NOW+67,sol_usd=FX,sol_at=NOW)
    assert out['state']=='unknown' and out['halted_at'] is None


def test_unknown_pause_does_not_extend_the_original_pending_entry_timeout(safety):
    conn=safety[0]
    assert submit(conn).state=='pending'
    safety[3]['state']='unknown'
    manage(conn,policy(),NOW+L.ENTRY_DELAY_S+1)
    manage(conn,policy(),NOW+policy().entry_timeout_s+1)
    assert conn.execute('SELECT state FROM orders').fetchone()[0]=='censored'


def test_existing_watcher_tick_drives_safety_with_corrected_time(safety,tmp_path):
    from office.test_pilot_seats import _watcher
    conn,quotes,_,sample=safety
    clock=tmp_path/'clock.json'
    clock.write_text(json.dumps({'offset_s':100.,'at':NOW+100}))
    desk=tmp_path/'desk';desk.mkdir()
    (desk/'universe.heartbeat').write_text(json.dumps({'ts':NOW+100,'connected':True,'sol_usd':FX,'sol_age_s':0.}))
    sample['equity']=799.
    result=_watcher().tick(conn,desk_dir=desk,clock_file=clock,now_host=NOW+100,manage=L.manage)
    assert result['state']=='managed' and result['through_utc']==NOW
    row=L.evaluation_safety_state(conn,epoch_id='E1')
    assert row['state']=='halted' and row['halted_at']==NOW


def test_removed_config_is_detected_by_watcher_even_without_new_submissions(safety):
    observe(safety)
    safety[0].execute("UPDATE ledger_epochs SET note='removed'")
    manage(safety[0],policy(),NOW+1)
    assert L.evaluation_safety_state(safety[0],epoch_id='E1')['halt_kind']=='config_drift'


def test_another_writer_cannot_submit_through_an_uncommitted_halt(safety):
    conn=safety[0]
    other=L.open_ledger(safety[2]/'office'/'ledger.db')
    other.execute('PRAGMA busy_timeout=0')
    try:
        with L._write(conn):
            L.halt_evaluation_entries(conn,epoch_id='E1',reason='operator',actor='crew',now=NOW)
            with pytest.raises(sqlite3.OperationalError,match='locked'):submit(other)
        assert submit(other).reason=='epoch_manual_halt:operator'
    finally:other.close()


def test_old_schema_readonly_safety_view_is_unknown_without_ddl():
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    try:
        before=conn.total_changes
        row=L.evaluation_safety_state(conn,epoch_id='E1')
        assert row=={'state':'unknown','reason':'epoch_safety_unavailable'}
        assert conn.total_changes==before
    finally:conn.close()


EXPLORE='office-v2-explore-1'


def _mark(kind='carried',swap_age=5.,carry_age=30.):
    quote=dict(binding_kind=kind,pool='P',lamports='200000000',proof=dict(newest_swap_age_s=swap_age,carried_age_s=carry_age))
    return dict(at=NOW,binding_kind=kind,lamports='200000000',quote_proof=json.dumps(quote))


def test_a_labelled_mark_passes_only_inside_its_bounds_and_only_when_asked():
    assert L._mark_accepted(_mark(),'P','labelled',NOW) and L._mark_accepted(_mark('estimated',carry_age=None),'P','labelled',NOW)
    assert not L._mark_accepted(_mark(),'P',True,NOW)                       # reports and frozen epochs stay proven-only
    assert not L._mark_accepted(_mark(swap_age=61.),'P','labelled',NOW)
    assert not L._mark_accepted(_mark(carry_age=301.),'P','labelled',NOW)
    assert not L._mark_accepted(_mark(),'OTHER','labelled',NOW)


@pytest.fixture
def labelled(book,monkeypatch):
    conn,quotes,path=book
    conn.execute('UPDATE ledger_epochs SET note=?',(json.dumps({'paper_eval':CONFIG}),))
    monkeypatch.setattr(L,'EVAL_EPOCHS',L.EVAL_EPOCHS+('E1',))
    monkeypatch.setattr(L,'LABELLED_MARK_EPOCHS',frozenset({'E1'}))
    monkeypatch.delenv('OFFICE_GATE_ESTIMATES',raising=False)
    pol=policy()
    assert submit(conn,order='held').state=='pending'
    manage(conn,pol,NOW+2)
    return conn,pol


def other(conn,order,now):
    return L.submit(conn,policy(),book_id='A',order_id=order,network_id='solana',mint='COIN'+order,pool='P',decision_bundle_hash='b'*64,now=now,sol_usd=FX,sol_at=now)


def _carry_newest(conn,at,**kw):
    mark=_mark(**kw)
    conn.execute("INSERT INTO marks(order_id,at,lamports,source,binding_kind,quote_proof) VALUES('held',?,?,'fixture',?,?)",(at,mark['lamports'],mark['binding_kind'],mark['quote_proof']));conn.commit()


def test_entry_admitted_with_a_carried_open_position_refused_past_the_bound(labelled):
    conn,_=labelled
    _carry_newest(conn,NOW+3)
    out=other(conn,'second',NOW+4);assert out.state=='pending',out
    _carry_newest(conn,NOW+5,carry_age=301.)
    assert other(conn,'third',NOW+6).reason=='epoch_equity_unknown'


def test_the_kill_switch_returns_the_gate_to_proven_only(labelled,monkeypatch):
    conn,_=labelled
    _carry_newest(conn,NOW+3)
    monkeypatch.setenv('OFFICE_GATE_ESTIMATES','0')
    assert other(conn,'second',NOW+4).reason=='epoch_equity_unknown'


def test_the_kill_rule_never_reads_an_estimate(labelled):
    conn,_=labelled
    before=dict(conn.execute("SELECT peak_equity_usd,last_equity_read_at FROM epoch_entry_safety WHERE epoch_id='E1'").fetchone())
    mark=_mark();quote=json.loads(mark['quote_proof']);quote['lamports']='0'
    conn.execute("INSERT INTO marks(order_id,at,lamports,source,binding_kind,quote_proof) VALUES('held',?,'0','fixture','carried',?)",(NOW+3,json.dumps(quote)));conn.commit()
    conn.execute("UPDATE epoch_entry_safety SET peak_equity_usd=peak_equity_usd+1000");conn.commit()
    out=L.evaluation_safety(conn,epoch_id='E1',now=NOW+4,sol_usd=FX,sol_at=NOW+4)
    assert out['state']!='halted' and out['reason']=='epoch_equity_unknown'
    after=dict(conn.execute("SELECT peak_equity_usd,last_equity_read_at FROM epoch_entry_safety WHERE epoch_id='E1'").fetchone())
    assert after['last_equity_read_at']==before['last_equity_read_at']


def test_an_admitted_order_records_the_basis_it_was_admitted_on(labelled):
    conn,_=labelled
    _carry_newest(conn,NOW+3)
    assert other(conn,'second',NOW+4).state=='pending'
    basis=json.loads(conn.execute("SELECT gate_basis FROM orders WHERE order_id='second'").fetchone()[0])
    assert basis==[dict(order_id='held',mark_kind='carried',mark_age_s=1.)]


def test_entry_refused_when_the_labelled_mark_is_past_the_age_bound(labelled):
    conn,_=labelled
    assert L.GATE_MARK_MAX_AGE_S==30.
    _carry_newest(conn,NOW+3)
    assert other(conn,'second',NOW+33).state=='pending'         # exactly 30 s old
    assert other(conn,'third',NOW+33.5).reason=='epoch_equity_unknown'


def test_the_observation_bounds_run_until_admission(labelled):
    conn,_=labelled
    _carry_newest(conn,NOW+3,carry_age=299.)
    assert other(conn,'second',NOW+5).reason=='epoch_equity_unknown'
    _carry_newest(conn,NOW+5,kind='estimated',swap_age=59.,carry_age=None)
    assert other(conn,'third',NOW+7).reason=='epoch_equity_unknown'
    _carry_newest(conn,NOW+7,carry_age=299.)
    assert other(conn,'fourth',NOW+8).state=='pending'          # exactly 300 s


def test_a_fill_records_the_basis_it_was_admitted_on(labelled):
    conn,pol=labelled
    _carry_newest(conn,NOW+3)
    assert other(conn,'second',NOW+4).state=='pending'
    L.manage(conn,pol,desk_dir=Path('/nonexistent'),clock_sample=CLOCK,now=NOW+6,sol_usd=FX,sol_at=NOW+4,mark_min_interval_s=3600.)
    row=conn.execute("SELECT state,gate_basis,fill_gate_basis FROM orders WHERE order_id='second'").fetchone()
    assert row['state']=='open',dict(conn.execute("SELECT state,reason,due_at FROM orders WHERE order_id='second'").fetchone())
    assert json.loads(row['gate_basis'])==[dict(order_id='held',mark_kind='carried',mark_age_s=1.)]
    assert json.loads(row['fill_gate_basis'])==[dict(order_id='held',mark_kind='carried',mark_age_s=3.)]


def test_commit_lag_is_not_counted_twice(labelled):
    conn,_=labelled
    _carry_newest(conn,NOW+3,swap_age=3.,carry_age=297.)       # producer: 299 at observation NOW+5, 297 at the cut
    assert other(conn,'second',NOW+5).state=='pending'


def test_both_explore_books_admit_on_a_labelled_mark():
    from sniper.office import v2_explore as X
    assert X.GPT_EPOCH in L.LABELLED_MARK_EPOCHS and 'office-v2-explore-1' in L.LABELLED_MARK_EPOCHS
