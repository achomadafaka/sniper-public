from office.test_outcome_table_script import W,Curve,PS,T0

def test_old_curve_reference_cannot_override_pumpswap_coverage_hole():
    ps=PS([(T0-1,1.),(T0+59,.5)],T0+20)
    _,base=W.process({'m':T0},Curve(),ps,set(),now=T0+121)
    assert base[0][2]['status']=='tape_ended_capture'
    _,mixed=W.process({'m':T0},Curve([(T0-2,1.)],T0+121),ps,set(),now=T0+121)
    assert mixed[0][2]['status']=='tape_ended_capture',mixed[0][2]

def test_old_curve_reference_cannot_erase_checked_pumpswap_loss():
    ps=PS([(T0-1,1.),(T0+59,.5)],T0+121,gaps=[T0+20],check='checked')
    _,base=W.process({'m':T0},Curve(),ps,set(),now=T0+121)
    assert base[0][2]['status']=='censored_gap'
    _,mixed=W.process({'m':T0},Curve([(T0-2,1.)],T0),ps,set(),now=T0+121)
    assert mixed[0][2]['status']=='censored_gap',mixed[0][2]
