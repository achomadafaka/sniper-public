"""PS dispatch precedes every curve read and uses the shared PS freshness limits."""
import pytest
from sniper.services import memecoin_desk_asof as A
from test_memecoin_desk_pumpswap_contract import ps_candidate, NOW


def test_pumpswap_dispatch_never_reads_curve_state(tmp_path,monkeypatch):
    from sniper.services import memecoin_desk_ps_asof as PS
    from sniper.settings import memecoin as cfg
    def forbidden(*args,**kwargs):
        raise AssertionError('PumpSwap reached a curve reader')
    monkeypatch.setattr(A,'_heartbeat',forbidden)
    monkeypatch.setattr(A,'_rows',forbidden)
    seen=[]
    def refresh(candidate,**kwargs):
        seen.append(kwargs);return candidate,{'fixture':'ps-only'}
    monkeypatch.setattr(PS,'refresh',refresh)
    c,meta=A.refresh(ps_candidate(),desk_dir=tmp_path,now=NOW+1)
    assert meta=={'fixture':'ps-only'} and c['venue']=='pumpswap'
    assert seen==[{'desk_dir':tmp_path,'now':NOW+1,
        'reserve_max_age_s':cfg.MEMECOIN_DESK_PS_RESERVE_MAX_AGE_S,
        'sol_max_age_s':cfg.MEMECOIN_DESK_PS_SOL_MAX_AGE_S,
        'resolve_zero_fees':False}]
    assert not list(tmp_path.iterdir())
