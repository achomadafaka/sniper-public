"""Unknown infrastructure never erases strict direct payments or grants traversal."""
import pytest
from solders.pubkey import Pubkey
from sniper.office import wallet_watch as W
from sniper.office.wallet_network import WalletNetwork

@pytest.mark.parametrize('fanout,source,want',[(2,'host',True),(1,'host',False),(2,'seat',False)])
def test_unknown_catalogue_preserves_only_strict_direct_hub_facts(tmp_path,monkeypatch,fanout,source,want):
    monkeypatch.setattr(W,'_known_infrastructure',lambda:None)
    monkeypatch.setattr(W,'FANOUT_HUB',2)
    addr=lambda n:str(Pubkey.from_bytes(n.to_bytes(32,'big')))
    net=WalletNetwork(tmp_path/'network.db',clock=lambda:1000.)
    c=net._conn()
    for n in range(1,fanout+1):
        net._insert(c,src=addr(n),dst=addr(9),edge_type='funded_by',source=source,actor='fixture',observed_at=985.,known_at=990.,proof_kind='research_stream',proof_ref=str(n),proof_state='verified',method='wallet-watch-stream-v1')
    c.commit();c.close()
    result=W.funding_links(net,address=addr(1),hops=3)
    assert bool(result['hub_funding']) is want
    assert result['state']=='partial'
    assert not result['funding_chain'] and not result['siblings'] and not result['hub_campaigns']
    assert result['excluded_funders'][0]['excluded']=='infrastructure_catalogue_unknown'
    if want:
        fact,=result['hub_funding']
        assert fact['infrastructure_state']=='unknown' and fact['ownership_proven'] is False
        assert fact['control_link_eligible'] is False and fact['hop']==1
