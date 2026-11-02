import pytest
from sniper.office import pass_replay as R
import importlib.util as _u, pathlib as _p
_spec=_u.spec_from_file_location("office_pass_replay_w", _p.Path(__file__).resolve().parents[2]/"scripts/office_pass_replay.py"); S=_u.module_from_spec(_spec); _spec.loader.exec_module(S)
T0=1_000_000.
BASE=dict(sl=30.,tp=100.,trail=20.,cat=60.)
def _script():return S
class FakePS:
    def __init__(self,prints,cover,gaps=(),gap_check='checked'):
        self.p,self.cover,self.g,self.gc=prints,cover,list(gaps),gap_check
    def prints(self,mint,lo,hi,start=None):
        return [x for x in self.p if lo<=x[0]<=hi],self.cover,None,self.g,self.gc
    def coverage_end(self,pool,start,hi):return self.cover

@pytest.mark.parametrize('coverage',[None,T0+5])
def test_a_rail_beyond_or_without_proven_coverage_is_not_scored(coverage):
    row=R.simulate([(T0-1,1.),(T0+10,.3)],T0,BASE,tape_end=coverage)
    assert row['status']!='ok',row
    assert row['gross_ret'] is None

def test_a_future_print_after_run_asof_cannot_supply_an_exit():
    S=_script();now=T0+10
    rows=S.replay_ticket(FakePS([(T0-1,1.),(T0+20,3.)],T0+4000),('t',T0,'solana','m','p','{}',None),[(0,'v')],None,now=now)
    row=next(r for r in rows if r['variant']=='base_d1')
    assert row['exit_at'] is None or row['exit_at']<=now,row
    assert row['gross_ret'] is None

def test_later_variant_read_failure_does_not_erase_an_earlier_variant():
    S=_script()
    class LateBad(FakePS):
        def prints(self,mint,lo,hi,start=None):
            if hi>T0+3602:raise S.OT.TapeUnreadable('later corrupt interval')
            return super().prints(mint,lo,hi,start)
    ps=LateBad([(T0-1,1.),(T0+10,3.)],T0+4000)
    rows=S.replay_ticket(ps,('t',T0,'solana','m','p','{}',None),[(0,'v')],None,now=T0+9999)
    assert next(r for r in rows if r['variant']=='base_d1')['status']=='ok'
