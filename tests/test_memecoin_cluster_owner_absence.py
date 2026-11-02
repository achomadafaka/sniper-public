"""Unknown provider rows must not remove an owner from the funded denominator."""
import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from sniper.services import memecoin_cluster_rpc as R

PDA=str(Pubkey.find_program_address([b'vault'],Pubkey.from_string('11111111111111111111111111111112'))[0])
WALLET=str(Keypair.from_seed(bytes(range(32))).pubkey())

@pytest.mark.parametrize('owner',[PDA,WALLET])
@pytest.mark.parametrize('result',[None,{}, {'value':[]},{'value':[{}]},{'value':[{'owner':None}]},{'value':[{'owner':''}]},{'value':[{'owner':123}]}])
def test_unknown_owner_row_cannot_be_inferred_absent(monkeypatch,owner,result):
 rpc=R.ResearchClusterRpc('https://fixture.invalid/?api-key=fixture',http=lambda *a,**k:None)
 monkeypatch.setattr(rpc,'holder_accounts',lambda mint:[('ata',100)])
 monkeypatch.setattr(rpc,'token_account_owners',lambda accounts:{'ata':owner})
 monkeypatch.setattr(rpc,'call',lambda *a:result)
 h=rpc.eligible_holders('mint')[0]
 assert h.exclusion is None and h.program_unknown is True

@pytest.mark.parametrize('owner,unknown',[(PDA,True),(WALLET,False)])
def test_explicit_null_account_keeps_reviewed_curve_classification(monkeypatch,owner,unknown):
 rpc=R.ResearchClusterRpc('https://fixture.invalid/?api-key=fixture',http=lambda *a,**k:None)
 monkeypatch.setattr(rpc,'holder_accounts',lambda mint:[('ata',100)])
 monkeypatch.setattr(rpc,'token_account_owners',lambda accounts:{'ata':owner})
 monkeypatch.setattr(rpc,'call',lambda *a:{'value':[None]})
 h=rpc.eligible_holders('mint')[0]
 assert h.exclusion is None and h.program_unknown is unknown


def test_truncated_batch_distinguishes_present_null_from_missing_owner(monkeypatch):
 rpc=R.ResearchClusterRpc('https://fixture.invalid/?api-key=fixture',http=lambda *a,**k:None)
 monkeypatch.setattr(rpc,'holder_accounts',lambda mint:[('one',100),('two',200)])
 monkeypatch.setattr(rpc,'token_account_owners',lambda accounts:{'one':WALLET,'two':PDA})
 monkeypatch.setattr(rpc,'call',lambda *a:{'value':[None]})
 # owner_programs requests sorted owners, so bind the null to the actual first requested key.
 first=sorted([WALLET,PDA])[0]
 holders=rpc.eligible_holders('mint')
 assert all(h.exclusion is None for h in holders)
 assert sum(h.balance_atomic for h in holders)==300
 assert {h.owner:h.program_unknown for h in holders}=={WALLET:WALLET!=first,PDA:True}
