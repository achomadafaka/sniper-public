import pytest
from office.test_outcome_table_script import W,Curve,PS,T0,NOW


def test_later_unreadable_interval_cannot_rewrite_an_earlier_horizon():
    class LaterUnreadable(PS):
        def prints(self,mint,lo,hi,start=None):
            if hi>T0+1000:raise W.TapeUnreadable('later corrupt row')
            return super().prints(mint,lo,hi,start)
    source=LaterUnreadable([(T0-1,1.),(T0+10,.9)],NOW)
    _,early=W.process({'m':T0},Curve(),source,set(),now=T0+121)
    _,late=W.process({'m':T0},Curve(),source,set(),now=NOW)
    assert early[0][2]['status']=='ok'
    assert late[0][2]==early[0][2]


def test_failure_before_archive_work_cannot_remove_preexisting_scratch_contents(tmp_path,monkeypatch):
    scratch=tmp_path/'scratch';scratch.mkdir()
    sentinel=scratch/'foreign.txt';sentinel.write_text('keep')
    monkeypatch.setattr(W,'universe',lambda *a:(_ for _ in ()).throw(ValueError('source failed')))
    with pytest.raises(ValueError,match='source failed'):
        W.main(['--out',str(tmp_path/'out.db'),'--scratch',str(scratch),'--dry'])
    assert sentinel.read_text()=='keep'


def test_later_curve_print_cannot_remove_an_earlier_pumpswap_gap():
    curve=Curve([(T0+1000,2.)],NOW)
    ps=PS([(T0-1,1.),(T0+10,.9)],NOW,gaps=[T0+5],check='checked')
    _,early=W.process({'m':T0},curve,ps,set(),now=T0+121)
    _,late=W.process({'m':T0},curve,ps,set(),now=NOW)
    assert early[0][2]['status']=='censored_gap'
    assert late[0][2]==early[0][2]


def test_half_second_after_horizon_cannot_change_its_gap_provenance():
    ps=PS([(T0-1,1.),(T0+10,.9)],NOW,gaps=[T0+5],check='checked')
    _,cut=W.process({'m':T0},Curve([],NOW),ps,set(),now=T0+121)
    _,full=W.process({'m':T0},Curve([(T0+60.5,2.)],NOW),ps,set(),now=T0+121)
    assert cut[0][2]['status']=='censored_gap'
    assert full[0][2]==cut[0][2]
