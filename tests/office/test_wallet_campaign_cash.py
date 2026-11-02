import pytest
import base58
from solders.pubkey import Pubkey
from sniper.office import wallet_campaign_quotes as Q
OWNER=str(Pubkey.from_bytes(bytes([4])*32));ACCOUNT=str(Pubkey.from_bytes(bytes([5])*32))

def tx(sig,usdc,sol,*,failed=False):
    return dict(slot=123,blockTime=1000,transaction=dict(signatures=[base58.b58encode(bytes([int(sig)])*64).decode()],message=dict(accountKeys=[dict(pubkey=OWNER,signer=True),dict(pubkey=ACCOUNT,signer=False)])),meta=dict(err={} if failed else None,fee=5000,preBalances=[100_000_000,2039280],postBalances=[100_000_000+sol,2039280],preTokenBalances=[dict(accountIndex=1,mint=Q.USDC,owner=OWNER,programId='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',uiTokenAmount=dict(amount='100000000',decimals=6))],postTokenBalances=[dict(accountIndex=1,mint=Q.USDC,owner=OWNER,programId='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',uiTokenAmount=dict(amount=str(100000000+usdc),decimals=6))]))

def test_measured_native_cash_costs_and_dated_fx_do_not_double_subtract_network_fee():
    r=Q.reconcile_cash_flow([tx('2',-25_000_000,-3_797_763),tx('3',25_921_931,-105_000)],owner=OWNER,commitment='finalized',fx=dict(usdc_per_sol='100',at=1000,ref='dated-price'),as_of=1001)
    assert r['usdc_delta_atomic']=='921931' and r['native_delta_lamports']=='-3902763'
    assert r['net_cash_usdc']=='0.531654700' and r['network_fee_lamports']=='10000'
    assert r['round_trip_profit_state']=='unknown_inventory_and_attribution'

def test_missing_fx_keeps_native_cost_unconverted():
    r=Q.reconcile_cash_flow([tx('2',-25_000_000,-1000)],owner=OWNER,commitment='confirmed',as_of=1001)
    assert r['net_cash_usdc'] is None and r['usdc_delta_atomic']=='-25000000'

@pytest.mark.parametrize('bad',['unsigned','missing_side','wrong_decimals','duplicate','future_fx','processed'])
def test_unproved_cash_is_not_zero_or_net_profit(bad):
    t=tx('2',-25_000_000,-1000);items=[t];commitment='finalized';fx=None
    if bad=='unsigned':t['transaction']['message']['accountKeys'][0]['signer']=False
    if bad=='missing_side':t['meta']['preTokenBalances']=[]
    if bad=='wrong_decimals':t['meta']['postTokenBalances'][0]['uiTokenAmount']['decimals']=9
    if bad=='duplicate':items=[t,t]
    if bad=='future_fx':fx=dict(usdc_per_sol='100',at=1002,ref='future')
    if bad=='processed':commitment='processed'
    r=Q.reconcile_cash_flow(items,owner=OWNER,commitment=commitment,fx=fx,as_of=1001)
    assert r['state']=='unknown' and r['net_cash_usdc'] is None


def test_unbounded_fx_exponent_is_rejected_before_formatting():
    r=Q.reconcile_cash_flow([tx('2',-25_000_000,-1000)],owner=OWNER,commitment='finalized',as_of=1001,fx=dict(usdc_per_sol='1e999999',at=1000,ref='bad'))
    assert r['state']=='unknown'


def test_conflicting_foreign_owner_duplicate_account_index_invalidates_receipt():
    t=tx('2',-25_000_000,-1000)
    for side in ('preTokenBalances','postTokenBalances'):
        forged=dict(t['meta'][side][0],owner=str(Pubkey.from_bytes(bytes([9])*32)))
        t['meta'][side].append(forged)
    r=Q.reconcile_cash_flow([t],owner=OWNER,commitment='finalized',as_of=1001)
    assert r['state']=='unknown' and r['usdc_delta_atomic'] is None


def test_cash_cli_never_substitutes_host_clock_for_utc_without_a_sample(tmp_path,capsys):
    import importlib.util,json
    from pathlib import Path
    path=Path(__file__).resolve().parents[2]/'scripts/research/wallet_campaign_report.py'
    spec=importlib.util.spec_from_file_location('cash_report_clock_cli',path);cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    receipts=tmp_path/'receipts.json';receipts.write_text(json.dumps([tx('2',-25_000_000,-1000)]))
    assert cli.main(['--state-root',str(tmp_path),'--cash-receipts',str(receipts),'--owner',OWNER])==0
    out=json.loads(capsys.readouterr().out)
    assert out['measured_cash_flow']['state']=='unknown' and out['measured_cash_flow']['reason']=='cash_clock_unknown'


def test_missing_execution_status_does_not_become_a_successful_cash_receipt():
    t=tx('2',-25_000_000,-1000);t['meta'].pop('err')
    assert Q.reconcile_cash_flow([t],owner=OWNER,commitment='confirmed',as_of=1001)['state']=='unknown'


def test_cash_cli_records_the_frozen_host_to_utc_conversion(tmp_path,capsys):
    import importlib.util,json
    from pathlib import Path
    path=Path(__file__).resolve().parents[2]/'scripts/research/wallet_campaign_report.py'
    spec=importlib.util.spec_from_file_location('cash_report_clock_known_cli',path);cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    (tmp_path/'data').mkdir();(tmp_path/'data/clock_offset.json').write_text(json.dumps(dict(offset_s=10.,at=2000.)))
    receipts=tmp_path/'receipts.json';receipts.write_text(json.dumps([tx('2',-25_000_000,-1000)]))
    cli.main(['--state-root',str(tmp_path),'--as-of','2000','--cash-receipts',str(receipts),'--owner',OWNER])
    out=json.loads(capsys.readouterr().out)
    assert out['cash_clock']==dict(kind='corrected_host',as_of_utc=1990.,offset_s=10.,sampled_at=2000.)
