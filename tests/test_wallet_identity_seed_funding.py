"""Seeded WSOL funding is verified economic flow, never inferred beneficial control."""
import copy
import pytest
from solders.pubkey import Pubkey
from sniper.services.wallet_identity import verified_sol_funding

SYSTEM='11111111111111111111111111111111'
TOKEN='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
WSOL='So11111111111111111111111111111111111111112'
SRC=str(Pubkey.from_bytes(bytes([1])*32));BASE=str(Pubkey.from_bytes(bytes([2])*32));DST=str(Pubkey.from_bytes(bytes([3])*32))
SEED='receipt-proof';TEMP=str(Pubkey.create_with_seed(Pubkey.from_string(BASE),SEED,Pubkey.from_string(TOKEN)))
NOW='2026-11-01T12:20:00+00:00';N=29_452_139_280


def receipt():
    keys=[dict(pubkey=k,signer=k in (SRC,BASE)) for k in (SRC,BASE,TEMP,DST,SYSTEM,TOKEN,WSOL)]
    return dict(slot=464084492,blockTime=1793534467,meta=dict(err=None,fee=10000,preBalances=[100_000_000_000,0,0,0,0,0,0],postBalances=[100_000_000_000-N-10000,0,0,N,0,0,0],innerInstructions=[]),transaction=dict(signatures=['a'*64,'b'*64],message=dict(accountKeys=keys,instructions=[
        dict(program='system',programId=SYSTEM,parsed=dict(type='createAccountWithSeed',info=dict(source=SRC,base=BASE,seed=SEED,newAccount=TEMP,owner=TOKEN,space=165,lamports=N))),
        dict(program='spl-token',programId=TOKEN,parsed=dict(type='initializeAccount',info=dict(account=TEMP,mint=WSOL,owner=BASE))),
        dict(program='spl-token',programId=TOKEN,parsed=dict(type='closeAccount',info=dict(account=TEMP,owner=BASE,destination=DST))) ])))


def test_seeded_wsol_close_proves_actual_source_not_only_fee_payer():
    clue,=verified_sol_funding(receipt(),NOW)
    assert (clue['funder'],clue['recipient'],clue['amount_atomic'])==(SRC,DST,str(N))
    assert clue['method']=='confirmed_seeded_wsol_close' and clue['verified'] is True
    assert clue['ownership_proven'] is False


@pytest.mark.parametrize('bad',['source_unsigned','owner_unsigned','wrong_owner','wrong_mint','wrong_program','wrong_seed','wrong_space','wrong_order','missing_close','wrong_close_owner','source_debit','recipient_credit','temporary_balance','custom_inner','custom_top','amount_float','amount_zero'])
def test_incomplete_ambiguous_or_custom_seed_flows_never_verify(bad):
    tx=receipt();ix=tx['transaction']['message']['instructions'];keys=tx['transaction']['message']['accountKeys'];meta=tx['meta']
    if bad=='source_unsigned':keys[0]['signer']=False;tx['transaction']['signatures'].pop()
    elif bad=='owner_unsigned':keys[1]['signer']=False;tx['transaction']['signatures'].pop()
    elif bad=='wrong_owner':ix[0]['parsed']['info']['owner']=SYSTEM
    elif bad=='wrong_mint':ix[1]['parsed']['info']['mint']=DST
    elif bad=='wrong_program':ix[1]['programId']=SYSTEM
    elif bad=='wrong_seed':ix[0]['parsed']['info']['seed']='different'
    elif bad=='wrong_space':ix[0]['parsed']['info']['space']=164
    elif bad=='wrong_order':ix[1],ix[2]=ix[2],ix[1]
    elif bad=='missing_close':ix.pop()
    elif bad=='wrong_close_owner':ix[2]['parsed']['info']['owner']=SRC
    elif bad=='source_debit':meta['postBalances'][0]+=1
    elif bad=='recipient_credit':meta['postBalances'][3]-=1
    elif bad=='temporary_balance':meta['postBalances'][2]=1
    elif bad=='custom_inner':meta['innerInstructions']=[dict(index=1,instructions=[dict(programId=DST,accounts=[SRC,TEMP])])]
    elif bad=='custom_top':ix.append(dict(programId=DST,accounts=[SRC,TEMP]))
    elif bad=='amount_float':ix[0]['parsed']['info']['lamports']=float(N)
    elif bad=='amount_zero':ix[0]['parsed']['info']['lamports']=0
    try:clues=verified_sol_funding(tx,NOW)
    except (ValueError,KeyError,TypeError,IndexError):clues=[]
    assert not any(c.get('method')=='confirmed_seeded_wsol_close' and c.get('verified') for c in clues)


def test_fee_payer_is_not_mistaken_for_the_actual_seed_funding_source():
    tx=receipt();payer=str(Pubkey.from_bytes(bytes([4])*32))
    tx['transaction']['message']['accountKeys'].insert(0,dict(pubkey=payer,signer=True))
    tx['transaction']['signatures'].append('c'*64)
    tx['meta']['preBalances'].insert(0,50000);tx['meta']['postBalances'].insert(0,40000)
    tx['meta']['postBalances'][1]+=10000
    clue,=verified_sol_funding(tx,NOW)
    assert clue['funder']==SRC and clue['funder']!=payer


def test_missing_destination_key_cannot_prove_recipient_credit():
    tx=receipt()
    tx['transaction']['message']['accountKeys'].pop(3)
    tx['meta']['preBalances'].pop(3)
    tx['meta']['postBalances'].pop(3)
    with pytest.raises(ValueError,match='seeded_funding_flow_unproven'):
        verified_sol_funding(tx,NOW)


def test_seeded_proof_reaches_existing_links_artifact_reader(tmp_path):
    import json
    from scripts.research.armed_wallet_links import _sol_facts
    from sniper.services.wallet_identity import read_identity
    funding=_sol_facts(receipt(),NOW)
    raw=dict(version=1,method='armed-wallet-links-v1',generated_at=NOW,
             pairs=[dict(uid='u',chain='solana',main_addresses=[DST],coverage=dict(state='partial'))],
             buys=[],funding=funding)
    (tmp_path/'wallet_identity_links.json').write_text(json.dumps(raw))
    out=read_identity(tmp_path/'wallet_identity.json',
                      [dict(uid='u',chain='solana',address=DST,evidence_kind='test')],chain='solana',now_iso=NOW)
    assert out['links_state']=='ok'
    relation,=out['wallets']['u']['related_wallets']
    assert relation['status']=='funding_verified_link'
    assert relation['ownership_proven'] is False
    clue,=relation['funding_clues']
    assert clue['method']=='confirmed_seeded_wsol_close'
    assert (clue['funder'],clue['recipient'],clue['amount_atomic'])==(SRC,DST,str(N))
