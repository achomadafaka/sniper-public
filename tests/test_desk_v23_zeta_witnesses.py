"""Zeta review witnesses on 027132ff5 (2026-10-26), adopted: gate ceiling, missing basis, noise re-judge, LIMIT dispatch, budget no-call."""
from contextlib import contextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timedelta
import json
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_memecoin_desk_pipeline import databases, api, decide, RESPONSE
from test_desk_v23_pipeline import ticket_in, vet, GATE, ASOF, PLAN, F, BASIS, vol_structure, verdict
from test_desk_watch_v22_pipeline import T_RESP, iso, fire
from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_judge as J
from sniper.services.memecoin_desk_universe import StandingBook
from sniper.settings import memecoin as cfg

@contextmanager
def fixture():
    with TemporaryDirectory(prefix='zeta-v23-fixture-') as td, pytest.MonkeyPatch.context() as mp:
        mp.setattr(cfg, 'MEMECOIN_DESK_ENTRY_GATE', True)
        mp.setattr(cfg, 'MEMECOIN_DESK_PX_HOLD_WATCH', True)
        gen = databases.__wrapped__(Path(td), mp)
        db = next(gen)
        try:
            yield db, mp
        finally:
            gen.close()

def test_gate_rebound_keeps_original_verdict_ceiling():
    with fixture() as (db, mp):
        t = ticket_in(db, mp, pol={**GATE, 'verdict_tolerance_pct': 0.0})
        out = decide(db, t, {**verdict(), 'p': PLAN, 'f': F}, asof=ASOF, structure=vol_structure(4.0, 30))
        assert out['action'] == 'WAIT'
        b = StandingBook()
        b.load(db[0].execute(StandingBook.SQL).fetchall(), T_RESP)
        mint = store.get_ticket(db[0], t)['mint']
        fires = []
        for dt, multiple in ((1, .90), (2, 1.50), (7, 1.50)):
            fires = b.on_trade(mint=mint, price_sol=BASIS*multiple, block_time=T_RESP+dt,
                               received_at=T_RESP+dt+.1, gap_since_load=False, net_flow_usd=1.0)
        assert not fires        # Beta: since the e49d1b0f9 re-review the producer refuses an above-ceiling fire outright
        row = api().record_fire(db[0], t, fire(price_sol=BASIS*1.5, block_time=T_RESP+7,
                                received_at=T_RESP+7.1, sol_at=T_RESP+7), now_iso=iso(T_RESP+8))
        vp = row['output']['verdict_price']        # the writer still caps a fire that reaches it
        # Beta (Zeta final on 1e0c21c4e): the ceiling is the sheet's own USD ref (priced_verdict), not BASIS x 180; this
        # fixture's snapshot mcap and BASIS are not one price identity, so compare with the sheet ref
        sheet = api().priced_verdict(ASOF['candidate'], clip_usd=25, basis='asof')['ref_price_usd']
        assert vp['ref_price_usd'] <= sheet, ('sheet ref', sheet, 'accepted ref', vp['ref_price_usd'])

def test_young_noise_watch_gets_one_real_rejudge():
    with fixture() as (db, mp):
        t = ticket_in(db, mp, pol={**GATE, 'judge_p_admit': 0.28})   # Beta: run_once refuses a cohort without p_admit
        out = decide(db, t, {**verdict(), 'p': PLAN, 'f': F}, asof=ASOF, structure=vol_structure(4.0, 5))
        assert out['reason'] == 'px_hold_noise_watch'
        calls = []
        from test_memecoin_desk_judge import model_returning
        fake = model_returning(lambda sheet: json.dumps({**verdict(v='S', r=None, k=sheet['opportunity_id']), 'p':None, 'f':F}), calls=calls)
        J.run_once(db[0], lambda: nullcontext(db[1]), call_model=fake, system_prompt='offline',
                   worker='zeta-test', now_fn=lambda: datetime.fromisoformat(RESPONSE)+timedelta(seconds=31),   # Beta: past the verdict's next_check_s 30
                   calls_per_hour=40, structure_fn=lambda *a: vol_structure(4.0, 30))
        assert len(calls) == 1, ('calls', calls, 'awaiting', store.list_awaiting_judge(db[0]),
                                  'state', store.get_ticket(db[0], t)['state'])

def test_fired_limit_dispatches_one_intent():
    from test_desk_limit_arm_watch import entered, refuse, LATER, t_of, fire as lfire, iso as liso
    with fixture() as (db, mp):
        t, intent = entered(db, mp)
        refuse(db, intent)
        api().reconcile_intents(*db, now_iso=LATER)
        at = t_of(LATER, 10)
        api().record_fire(db[0], t+':limit', lfire(BASIS, at), now_iso=liso(at+1))
        result = api().dispatch_outbox(*db, now_iso=liso(at+1))
        assert [(r.get('state'),r.get('reason')) for r in result] == [('sent',None)]

def test_no_call_budget_mismatch_has_known_zero_cost():
    from test_memecoin_desk_judge import ready_ttl, Clock
    from sniper.persistence import db_memecoin as ledger
    with fixture() as (db, mp):
        t = ready_ttl(db)
        try:
            out = J.run_once(db[0], lambda: nullcontext(db[1]), call_model=lambda *a, **kw: {
                'text':None, 'is_error':False, 'call_error':'answer_budget_mismatch', 'usage':None,
                'max_output_tokens':600}, system_prompt='offline', worker='zeta-test', now_fn=Clock(), calls_per_hour=40)
        except ValueError as exc:
            head=store.get_ticket(db[0], t)
            cost=ledger.get_desk_cohort_cost(db[1], head['cohort_id'], 'desk-judge:'+t)
            raise AssertionError(('unexpected receipt refusal', str(exc), 'vet', vet(db,t)['reason'],
                                  'model_cost_usd', cost['usd'])) from exc
        assert out[0]['reason'] == 'judge_answer_budget_mismatch'

def test_entry_gate_handles_asof_without_rank_price():
    with fixture() as (db, mp):
        t=ticket_in(db,mp,pol=GATE)
        asof={**ASOF, 'meta':{k:v for k,v in ASOF['meta'].items() if k!='rank'}}
        out=decide(db,t,{**verdict(),'p':PLAN,'f':F},asof=asof,structure=vol_structure(4.0,30))
        assert out['action'] in ('SKIP','WATCH'), out
        assert 'trigger' not in vet(db,t)['inputs']

@pytest.mark.parametrize('noise,bars',[(None,30),(0,30),(4.0,None),(4.0,0),(4.0,11)])
def test_unknown_or_zero_noise_never_creates_a_trigger(noise,bars):
    with fixture() as (db,mp):
        t=ticket_in(db,mp,pol=GATE)
        out=decide(db,t,{**verdict(),'p':PLAN,'f':F},asof=ASOF,structure=vol_structure(noise,bars))
        assert out['action']=='WATCH' and out['reason']=='px_hold_noise_watch'
        assert 'trigger' not in vet(db,t)['inputs']
