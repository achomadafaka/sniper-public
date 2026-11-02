from sniper.office import pass_replay as R
import importlib.util as _u, pathlib as _p
_spec=_u.spec_from_file_location("office_pass_replay_w2", _p.Path(__file__).resolve().parents[2]/"scripts/office_pass_replay.py"); S=_u.module_from_spec(_spec); _spec.loader.exec_module(S)
T0=1_000_000.
BASE=dict(sl=30.,tp=100.,trail=20.,cat=60.)

def test_a_blind_tick_after_asof_is_not_a_capture_stop():
    row=R.simulate([(T0-1,1.),(T0+5,.3)],T0,BASE,tape_end=T0+10,asof=T0+10,blind_s=15.)
    assert row['status']=='immature',row


def test_a_read_never_reaches_past_asof_even_when_an_exit_is_already_known():
    now=T0+30
    class Reader:
        def prints(self,mint,lo,hi,start=None):
            if hi>now+1e-6:raise S.OT.TapeUnreadable('only after run asof')
            return [(T0-1,1.),(T0+5,3.)],now,None,[],'checked'
        def coverage_end(self,*a):return now
    rows=S.replay_ticket(Reader(),('t',T0,'solana','m','p','{}',None),[(0,'v')],None,now=now)
    assert next(r for r in rows if r['variant']=='base_d1')['status']=='ok'
