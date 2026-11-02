import pytest
from office.test_outcome_table_script import W,T0,_pumpswap

def test_disjoint_keep_windows_do_not_bridge_a_capture_hole():
    ps=_pumpswap(now=T0+20000,keep={'p':[(T0-60,T0+20),(T0+40,T0+100)]},live_end=T0+20000)
    assert ps.coverage_end('p',T0,T0+60)<=T0+20

def test_a_keep_window_starting_after_the_requested_start_is_not_coverage():
    ps=_pumpswap(now=T0+20000,keep={'p':[(T0+40,T0+100)]},live_end=T0+20000)
    assert ps.coverage_end('p',T0,T0+60) is None

def test_no_selection_is_unknown_for_a_pool_without_keep():
    ps=_pumpswap(now=T0+200,keep={},live_end=T0+200)
    assert ps.coverage_end('p',T0,T0+60) is None

def test_selection_start_after_the_requested_start_is_not_coverage():
    ps=_pumpswap(now=T0+200,keep={},live_end=T0+200,selection={'p':[(T0+40,None)]})
    assert ps.coverage_end('p',T0,T0+60) is None

def test_unreadable_loss_source_is_not_an_empty_loss_list(tmp_path):
    from sniper.services import memecoin_desk_ps_tape as T
    ps=W.PumpSwap.__new__(W.PumpSwap);ps.T=T;ps.dir=tmp_path
    with pytest.raises(T.TapeUnreadable):ps._pool_losses('p',T0,T0+60)
