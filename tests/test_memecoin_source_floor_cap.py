import pytest
from sniper.services import memecoin_real_runner as rr
from sniper.settings import memecoin as cfg

@pytest.mark.parametrize('chain,mcap,expected', [('4663',370625,1500),('4663',250000,1500),('4663',249999,1999.992),('56',370625,2965),('4663',None,0.008)])
def test_large_robinhood_floor_cap(monkeypatch,chain,mcap,expected):
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_SOURCE_BUY_FLOOR_CAP_USD_BY_CHAIN',{'4663':1500},raising=False)
    ratio=rr._band_source_buy_ratio({},chain,mcap,0.008)
    assert (ratio*mcap if mcap else ratio)==pytest.approx(expected)

@pytest.mark.parametrize('ratio',[0,0.001])
def test_cap_never_raises_existing_floor(monkeypatch,ratio):
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_SOURCE_BUY_FLOOR_CAP_USD_BY_CHAIN',{'4663':1500},raising=False)
    assert rr._band_source_buy_ratio({},'4663',370625,ratio)==ratio

def test_synths_accepts_and_small_buy_still_refuses(monkeypatch):
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_SOURCE_BUY_FLOOR_CAP_USD_BY_CHAIN',{'4663':1500},raising=False)
    ratio=rr._band_source_buy_ratio({},'4663',370625,0.008)
    def reason(usd):
        return rr.source_buy_family_reason(their_usd=usd,source_mcap=370625,prior_net_usd=None,ratio=ratio,unknown_mcap_floor=100,abs_min=200,abs_max=0,family_active=True)[0]
    assert reason(2156) is None
    assert reason(1499) is not None
