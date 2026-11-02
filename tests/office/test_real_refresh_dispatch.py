from test_desk_ps_asof import build,candidate,swap,NOW,SIG
from sniper.services import memecoin_desk_asof as D
from sniper.office import desk_near_pass as N

def test_real_sheet_dispatcher_reaches_real_pumpswap_fee_resolution(tmp_path):
    swaps=[swap(slot=18,sig='old'+SIG[3:],tx_index=1,received_at=NOW-3.),swap(slot=19,lp='0',prot='0',crt='0',received_at=NOW-2.)]
    desk=build(tmp_path,swaps=swaps)
    resolved,_=D.refresh(candidate(),desk_dir=desk,now=NOW,resolve_zero_fees=True)
    normal,_=D.refresh(candidate(),desk_dir=desk,now=NOW)
    def state(c):return c['snapshot']['fields']['pool_state']['value']
    assert tuple(state(resolved)[k] for k in ('lp_fee_bps','protocol_fee_bps','creator_fee_bps'))==(20,5,5)
    assert state(resolved)['fee_basis']=='newest_nonzero'
    assert tuple(state(normal)[k] for k in ('lp_fee_bps','protocol_fee_bps','creator_fee_bps'))==(0,0,0)
