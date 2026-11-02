"""Default-off family study uses the existing quote owner, never a new client."""
import importlib.util
import json
import sqlite3
from pathlib import Path

S=importlib.util.spec_from_file_location('dex_family_owner',Path(__file__).resolve().parents[1]/'scripts/research/dex_boost_collector.py')
C=importlib.util.module_from_spec(S);S.loader.exec_module(C)
T0=1793536000.;DAY=C.utc_day(T0)
MINT='3'*40;POOL='4'*40;SIG='5'*88
USDC='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'


class World:
    def __init__(self,tmp,enabled=True):
        self.tmp=tmp;self.now=T0+2;self.calls=[];self.delay=0;self.bad=None
        self.db=tmp/'wallet_network.db'
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE wallet_family_candidates(pool TEXT,source_signature TEXT,emitted_at REAL,source_receipt REAL,due REAL,early INTEGER,evidence_known_at REAL,actors_json TEXT,evidence_json TEXT,coverage_json TEXT,source_slot INTEGER,source_tx_index INTEGER,actor_order_json TEXT,policy TEXT,PRIMARY KEY(pool,source_signature))')
            c.execute('INSERT INTO wallet_family_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(POOL,SIG,T0+1,T0,T0+10,1,T0-1,'[]','{}','{"tape":"observed_only"}',100,None,'[]','research_only'))
        (tmp/'desk/pumpswap').mkdir(parents=True)
        with sqlite3.connect(tmp/'desk/pumpswap/tape.db') as c:
            c.execute('CREATE TABLE ps_watch_events(pool TEXT,signature TEXT,mint TEXT)')
            c.execute('INSERT INTO ps_watch_events VALUES(?,?,?)',(POOL,SIG,MINT))
        self.col=self.make(enabled)
    def make(self,enabled=True):
        return C.Collector(intake_dir=self.tmp/'empty_intake',out_dir=self.tmp/'out',desk_dir=self.tmp/'desk',clock_fn=lambda:(0.,self.now),jupiter_fn=self.quote,ps_buy=None,ps_sell=None,paper_throttled_fn=lambda:False,now_fn=lambda:self.now,family_probe=enabled,family_db=self.db)
    def quote(self,i,o,n):
        self.calls.append((i,o,n,self.now));self.now+=self.delay
        qty=(2**70 if self.now<T0+10 else 2**70+123) if i==USDC else 30_000_000
        q=dict(inputMint=i,outputMint=o,inAmount=str(n),outAmount=str(qty),otherAmountThreshold=str(qty-1),contextSlot=100,routePlan=[dict(swapInfo=dict(ammKey=POOL))])
        if self.bad:q.update(self.bad)
        return q,None
    def at(self,t):self.now=t;return self.col.tick()
    def rows(self,table):
        with sqlite3.connect(self.tmp/'out'/f'{DAY}.db') as c:
            c.row_factory=sqlite3.Row
            return [dict(r) for r in c.execute('SELECT * FROM '+table)]


def test_default_off_leaves_legacy_tables_and_policy_untouched(tmp_path):
    w=World(tmp_path,False);w.col.tick()
    assert not w.calls and C.CLIP_LAMPORTS==250_000_000 and C.ENTRY_AT==30
    for path in (tmp_path/'out').glob('????-??-??.db'):
        with sqlite3.connect(path) as c:assert not c.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'family_%'").fetchone()


def test_two_arms_use_usdc25_and_their_own_exact_text_quantities(tmp_path):
    w=World(tmp_path);w.col.tick();w.at(T0+10);w.at(T0+15)
    quotes=w.rows('family_quotes')
    buys={r['arm']:r for r in quotes if r['offset_s']==0}
    assert set(buys)=={'early','baseline'}
    assert all(r['input_atomic']=='25000000' and r['input_mint']==USDC for r in buys.values())
    assert buys['early']['acquired_qty_atomic']==str(2**70)
    assert buys['baseline']['acquired_qty_atomic']==str(2**70+123)
    sell=next(r for r in quotes if r['arm']=='baseline' and r['offset_s']==5)
    assert sell['input_atomic']==buys['baseline']['acquired_qty_atomic']
    assert sell['output_mint']==USDC and sell['net_pct'] is None
    assert sell['expected_output_atomic']!=sell['minimum_output_atomic']


def test_response_after_early_deadline_is_censored_not_backdated(tmp_path):
    w=World(tmp_path);w.delay=2.;w.at(T0+9)
    r=next(r for r in w.rows('family_quotes') if r['arm']=='early' and r['offset_s']==0)
    assert r['state']=='censored' and r['reason']=='response_late'
    assert r['requested_host']==T0+9 and r['response_host']==T0+11
    assert r['acquired_qty_atomic'] is None


def test_restart_dedupes_origin_and_reopens_pending(tmp_path):
    w=World(tmp_path);w.col.tick()
    for c in w.col.days.values():c.close()
    w.col.seen.close();w.col=w.make();w.col.resume();w.at(T0+10)
    assert len(w.rows('family_pairs'))==1
    assert sum(r['offset_s']==0 for r in w.rows('family_quotes'))==2
    assert len(w.calls)==2


def test_future_family_evidence_is_censored_without_quote(tmp_path):
    w=World(tmp_path)
    with sqlite3.connect(w.db) as c:c.execute('UPDATE wallet_family_candidates SET evidence_known_at=?',(T0+3,))
    w.col.tick()
    assert not w.calls
    assert w.rows('family_pairs')[0]['reason']=='future_family_evidence'


def test_unknown_emission_is_revisited_after_phase_two_update(tmp_path):
    w=World(tmp_path)
    with sqlite3.connect(w.db) as c:c.execute('UPDATE wallet_family_candidates SET early=NULL')
    w.col.tick();assert not w.calls
    with sqlite3.connect(w.db) as c:c.execute('UPDATE wallet_family_candidates SET early=1')
    w.at(T0+3)
    assert len(w.calls)==1


def test_wrong_mint_or_atomic_quantity_is_censored(tmp_path):
    w=World(tmp_path);w.bad={'inputMint':MINT};w.col.tick()
    q=next(r for r in w.rows('family_quotes') if r['arm']=='early' and r['offset_s']==0)
    assert q['state']=='censored' and q['reason']=='quote_identity_mismatch'
    assert q['acquired_qty_atomic'] is None


def test_shared_daily_cap_stops_family_without_new_provider_call(tmp_path,monkeypatch):
    w=World(tmp_path);monkeypatch.setattr(C,'DAILY_CAP',0);w.col.tick()
    assert not w.calls
    assert any(r['reason']=='cap_reached' for r in w.rows('family_quotes'))

def test_host_windows_use_one_nonzero_clock_sample(tmp_path):
    w=World(tmp_path);w.col.clock_fn=lambda:(120.,T0)
    w.col.tick()
    q=next(r for r in w.rows('family_quotes') if r['arm']=='early' and r['offset_s']==0)
    assert q['state']=='known' and q['requested_host']==T0+2
    assert q['requested_utc']==T0+2-120 and q['response_utc']==q['response_host']-120
    assert q['clock_offset_s']==120 and q['clock_sampled_at']==T0


def test_unknown_clock_never_defaults_to_zero(tmp_path):
    w=World(tmp_path);w.col.clock_fn=lambda:None
    assert w.col.tick()=={'clock':'unknown'} and not w.calls


def test_backward_response_clock_is_censored(tmp_path):
    w=World(tmp_path);w.delay=-5;w.col.tick()
    q=next(r for r in w.rows('family_quotes') if r['arm']=='early' and r['offset_s']==0)
    assert q['state']=='censored' and q['reason']=='clock_regressed' and q['acquired_qty_atomic'] is None


def test_legacy_due_job_keeps_priority_over_family(tmp_path):
    w=World(tmp_path);c=w.col.day(DAY)
    with c:
        c.execute('INSERT INTO events(event_id,class,network_id,mint,first_seen_at,intake_day,intake_row_id,filter,version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',('legacy','BOOST','solana',MINT,T0-28,DAY,1,'boost_latest',C.VERSION,T0))
        c.execute('INSERT INTO pending VALUES(?,?,?)',('legacy',0,T0+2))
    w.col.tick()
    assert len(w.calls)==1 and w.calls[0][0]==C.WSOL and w.calls[0][2]==250_000_000
    assert not w.rows('family_quotes')


def test_persisted_family_attempt_counts_toward_shared_cap_after_restart(tmp_path,monkeypatch):
    w=World(tmp_path);w.col.tick()
    for c in w.col.days.values():c.close()
    w.col.seen.close();w.col=w.make();w.col.resume()
    monkeypatch.setattr(C,'DAILY_CAP',1)
    w.at(T0+10)
    assert len(w.calls)==1
    q=next(r for r in w.rows('family_quotes') if r['arm']=='baseline' and r['offset_s']==0)
    assert q['reason']=='cap_reached'

def test_request_clock_rollback_cannot_run_baseline_before_due(tmp_path):
    w=World(tmp_path);w.col.tick();w.now=T0+9
    conn=w.col.day(DAY)
    row=conn.execute("SELECT p.*,e.mint,e.baseline_due FROM family_pending p JOIN family_pairs e USING(pair_id) WHERE arm='baseline' AND offset_s=0").fetchone()
    before=len(w.calls)
    assert w.col._family_one(conn,row,(0,T0))==0
    assert len(w.calls)==before


def test_exit_horizons_are_each_arms_own_full_quantity(tmp_path):
    w=World(tmp_path);w.at(T0+1)
    for t in (6,11,16,31,41,301,311):w.at(T0+t)
    sells=[r for r in w.rows('family_quotes') if r['offset_s'] in (5,30,300) and r['state']=='known']
    assert len(sells)==6
    for r in sells:
        expected=str(2**70 if r['arm']=='early' else 2**70+123)
        assert r['input_atomic']==expected and r['input_mint']==MINT and r['output_mint']==USDC
        assert r['net_pct'] is None

def test_interrupted_quote_is_durable_censor_not_unaccounted_retry(tmp_path):
    import pytest
    w=World(tmp_path)
    def interrupted(*args):raise KeyboardInterrupt
    w.col.jupiter_fn=interrupted
    with pytest.raises(KeyboardInterrupt):w.col.tick()
    q=next(r for r in w.rows('family_quotes') if r['arm']=='early' and r['offset_s']==0)
    assert q['reason']=='quote_interrupted' and q['accounted']==1
    for c in w.col.days.values():c.close()
    w.col.seen.close();w.col=w.make();w.col.resume();w.at(T0+10)
    assert sum(r['arm']=='early' and r['offset_s']==0 for r in w.rows('family_quotes'))==1
    assert all(r['net_pct'] is None for r in w.rows('family_quotes'))


def test_offset_change_during_response_keeps_request_sample(tmp_path):
    w=World(tmp_path);w.col.clock_fn=lambda:(120.,T0)
    original=w.col.jupiter_fn
    def changing(*args):
        answer=original(*args);w.col.clock_fn=lambda:(180.,w.now)
        return answer
    w.col.jupiter_fn=changing;w.col.tick()
    q=next(r for r in w.rows('family_quotes') if r['arm']=='early' and r['offset_s']==0)
    assert q['clock_offset_s']==120 and q['response_utc']==q['response_host']-120

def test_nonfinite_family_clock_is_unknown_without_provider_call(tmp_path):
    w=World(tmp_path);w.col.clock_fn=lambda:(float('nan'),T0)
    assert w.col.tick()=={'clock':'unknown'} and not w.calls

def test_completed_day_admissions_count_before_restart_reopens_handles(tmp_path,monkeypatch):
    w=World(tmp_path);w.col.tick();w.at(T0+400)
    assert not w.rows('family_pending')
    for c in w.col.days.values():c.close()
    w.col.seen.close();w.col=w.make();w.col.resume()
    assert DAY not in w.col.days
    sig='6'*88
    with sqlite3.connect(w.db) as c:c.execute('INSERT INTO wallet_family_candidates SELECT pool,?,?,?,?,early,evidence_known_at,actors_json,evidence_json,coverage_json,source_slot,source_tx_index,actor_order_json,policy FROM wallet_family_candidates',(sig,T0+400,T0+399,T0+409))
    with sqlite3.connect(tmp_path/'desk/pumpswap/tape.db') as c:c.execute('INSERT INTO ps_watch_events VALUES(?,?,?)',(POOL,sig,MINT))
    monkeypatch.setattr(C,'DAILY_CAP',1);before=len(w.calls);w.at(T0+401)
    assert len(w.calls)==before


def test_closed_old_event_day_attempt_is_in_current_day_budget(tmp_path):
    w=World(tmp_path);old=w.col.day('2026-10-01')
    with old:old.execute("INSERT INTO family_quotes(pair_id,arm,offset_s,state,reason,due_host,requested_utc,accounted) VALUES('old','early',0,'censored','quote_interrupted',?,?,1)",(T0,T0))
    w.col._close('2026-10-01')
    assert w.col.quotes_today(T0+2)==1
    assert '2026-10-01' not in w.col.days

def test_legacy_admissions_in_closed_day_also_count(tmp_path):
    w=World(tmp_path,False);old=w.col.day('2026-10-01')
    with old:old.execute("INSERT INTO quotes(event_id,offset_s,side,state,due_at,taken_at) VALUES('legacy',0,'buy','known',?,?)",(T0,T0))
    w.col._close('2026-10-01')
    assert w.col.quotes_today(T0+2)==1
    assert '2026-10-01' not in w.col.days


def test_unreadable_closed_day_budget_is_unknown_not_zero(tmp_path):
    w=World(tmp_path);(tmp_path/'out/2026-10-01.db').write_bytes(b'not sqlite')
    result=w.col.tick()
    assert result['budget']=='unknown' and not w.calls

def test_disabling_probe_preserves_its_shared_daily_admissions(tmp_path):
    w=World(tmp_path);w.col.tick()
    for c in w.col.days.values():c.close()
    w.col.seen.close();w.col=w.make(False);w.col.resume()
    assert w.col.quotes_today(T0+2)==1

def pair_status(w):
    r=w.rows('family_pairs')[0]
    return r['state'],r['reason']


def seed_terminal_pair(w, *, known_buys=2, missing=None, exclusion=None):
    w.col.tick();conn=w.col.day(DAY);pid=w.rows('family_pairs')[0]['pair_id']
    with conn:
        conn.execute('DELETE FROM family_pending')
        conn.execute('DELETE FROM family_quotes')
        for index,arm in enumerate(('early','baseline')):
            for h in (0,5,30,300):
                if (arm,h)==missing:continue
                state='known' if index<known_buys else 'censored'
                conn.execute('INSERT INTO family_quotes(pair_id,arm,offset_s,state,reason,due_host) VALUES(?,?,?,?,?,?)',(pid,arm,h,state,None if state=='known' else 'no_entry',T0))
        if exclusion:conn.execute("UPDATE family_pairs SET state='censored',reason=?",(exclusion,))


def test_completed_full_pair_is_quoted(tmp_path):
    w=World(tmp_path);seed_terminal_pair(w)
    before=len(w.calls);w.col._family_due((0,T0))
    assert pair_status(w)==('quoted','all_horizons_observed') and len(w.calls)==before


def test_completed_partial_pair_reports_partial_coverage(tmp_path):
    w=World(tmp_path);seed_terminal_pair(w,known_buys=1)
    w.col._family_due((0,T0))
    assert pair_status(w)==('partial','quote_coverage_partial')


def test_completed_no_entry_pair_is_censored(tmp_path):
    w=World(tmp_path);seed_terminal_pair(w,known_buys=0)
    w.col._family_due((0,T0))
    assert pair_status(w)==('censored','entry_unavailable')


def test_missing_quote_rows_are_partial_even_with_no_known_buy(tmp_path):
    w=World(tmp_path);seed_terminal_pair(w,known_buys=0,missing=('early',0))
    w.col._family_due((0,T0))
    assert pair_status(w)==('partial','quote_coverage_partial')


def test_pending_pair_and_existing_exclusion_are_preserved(tmp_path):
    w=World(tmp_path);w.col.tick();w.col._family_due((0,T0))
    assert pair_status(w)==('pending',None)
    seed_terminal_pair(w,exclusion='future_family_evidence')
    w.col._family_due((0,T0))
    assert pair_status(w)==('censored','future_family_evidence')


def test_resume_finalizes_old_completed_day_before_closing_it(tmp_path):
    w=World(tmp_path);seed_terminal_pair(w,known_buys=0)
    with w.col.seen:w.col.seen.execute("DELETE FROM meta WHERE key='last_tick'")
    for c in w.col.days.values():c.close()
    w.col.seen.close();w.now=T0+4*86400;w.col=w.make();w.col.resume()
    assert pair_status(w)==('censored','entry_unavailable')
    assert DAY not in w.col.days

def test_live_job_completion_finalizes_full_and_missed_pairs(tmp_path):
    w=World(tmp_path);w.at(T0+1)
    for t in (6,11,16,31,41,301,311):w.at(T0+t)
    assert pair_status(w)==('quoted','all_horizons_observed')
    (tmp_path/'late').mkdir()
    late=World(tmp_path/'late');late.at(T0+20)
    assert not late.calls and len(late.rows('family_quotes'))==8
    assert pair_status(late)==('censored','entry_unavailable')
