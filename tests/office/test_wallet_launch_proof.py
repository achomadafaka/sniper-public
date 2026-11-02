import copy
import hashlib
import struct

import base58
import pytest
from solders.pubkey import Pubkey
from sniper.office.wallet_launch_proof import launch_facts

PUMP='6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'


def receipt(v2=True):
    dev,mint,creator=[str(Pubkey.from_bytes(bytes([n])*32)) for n in (1,2,3)]
    curve=str(Pubkey.find_program_address([b'bonding-curve',bytes(Pubkey.from_string(mint))],Pubkey.from_string(PUMP))[0])
    accounts=[str(Pubkey.from_bytes(bytes([n])*32)) for n in range(10,26 if v2 else 24)]
    accounts[0]=mint;accounts[2]=curve;accounts[5 if v2 else 7]=dev;accounts[-1]=PUMP
    name='create_v2' if v2 else 'create'
    data=hashlib.sha256(('global:'+name).encode()).digest()[:8]+b''.join(struct.pack('<I',len(s))+s for s in (b'n',b's',b'u'))+bytes(Pubkey.from_string(creator))+(b'\0\0' if v2 else b'')
    keys=[dev,mint]+[k for k in accounts if k not in (dev,mint)]
    tx=dict(slot=10,blockTime=100,meta=dict(err=None),transaction=dict(signatures=[base58.b58encode(bytes([n])*64).decode() for n in (7,8)],message=dict(accountKeys=[dict(pubkey=k,signer=k in (dev,mint),writable=k!=PUMP) for k in keys],instructions=[dict(programId=PUMP,accounts=accounts,data=base58.b58encode(data).decode())])))
    return tx,dev,mint,creator


@pytest.mark.parametrize('v2',[False,True])
def test_signed_developer_is_distinct_from_creator_argument(v2):
    tx,dev,mint,creator=receipt(v2)
    before=copy.deepcopy(tx);out=launch_facts(tx,commitment='confirmed')
    assert out['state']=='known' and out['coverage']=='top_level_pump_only'
    fact,=out['items']
    assert (fact['developer'],fact['mint'],fact['creator_argument'])==(dev,mint,creator)
    assert fact['ownership_proven'] is False and fact['outer_ix']==0
    assert tx==before


@pytest.mark.parametrize('bad',['failed','missing_meta','clock','signature','unsigned_dev','readonly_dev','unsigned_mint','missing_account','curve','truncated','duplicate'])
def test_incomplete_launch_proof_is_unknown(bad):
    tx,dev,mint,_=receipt();msg=tx['transaction']['message'];ix=msg['instructions'][0]
    if bad=='failed':tx['meta']['err']={}
    elif bad=='missing_meta':tx['meta']={}
    elif bad=='clock':tx['blockTime']=None
    elif bad=='signature':tx['transaction']['signatures'][0]='1'
    elif bad in ('unsigned_dev','readonly_dev','unsigned_mint'):
        key=next(k for k in msg['accountKeys'] if k['pubkey']==(mint if bad=='unsigned_mint' else dev))
        key['writable' if bad=='readonly_dev' else 'signer']=False
    elif bad=='missing_account':msg['accountKeys'].pop(3)
    elif bad=='curve':ix['accounts'][2]=dev
    elif bad=='truncated':ix['data']=base58.b58encode(base58.b58decode(ix['data'])[:15]).decode()
    elif bad=='duplicate':msg['instructions'].append(copy.deepcopy(ix))
    out=launch_facts(tx,commitment='confirmed')
    assert out['state']=='unknown' and out['items']==[]


def test_other_program_and_inner_creation_do_not_claim_launch_absence():
    tx,*_=receipt();ix=tx['transaction']['message']['instructions'].pop()
    tx['meta']['innerInstructions']=[dict(index=0,instructions=[ix])]
    tx['transaction']['message']['instructions']=[dict(programId='11111111111111111111111111111111',data='1',accounts=[])]
    out=launch_facts(tx,commitment='confirmed')
    assert out['items']==[] and out['coverage']=='top_level_pump_only'
    assert 'absence' in out['note']


@pytest.mark.parametrize('bad',['processed','duplicate_keys','oversize_data','oversize_signature'])
def test_source_and_size_boundaries(bad):
    tx,*_=receipt();commitment='confirmed'
    if bad=='processed':commitment='processed'
    elif bad=='duplicate_keys':tx['transaction']['message']['accountKeys'].append(copy.deepcopy(tx['transaction']['message']['accountKeys'][0]))
    elif bad=='oversize_data':tx['transaction']['message']['instructions'][0]['data']='1'*6001
    elif bad=='oversize_signature':tx['transaction']['signatures'][0]='1'*10000
    assert launch_facts(tx,commitment=commitment)['state']=='unknown'


@pytest.mark.parametrize('bad',['lookup_signer','header_signatures','signer_prefix','header_readonly','lookup_order'])
def test_impossible_message_signer_bindings_are_refused(bad):
    tx,*_=receipt();msg=tx['transaction']['message']
    if bad=='lookup_signer':msg['accountKeys'][1]['source']='lookupTable'
    elif bad=='header_signatures':msg['header']=dict(numRequiredSignatures=1,numReadonlySignedAccounts=0,numReadonlyUnsignedAccounts=1)
    elif bad=='header_readonly':msg['header']=dict(numRequiredSignatures=2,numReadonlySignedAccounts=1,numReadonlyUnsignedAccounts=1)
    elif bad=='signer_prefix':msg['accountKeys'][1],msg['accountKeys'][2]=msg['accountKeys'][2],msg['accountKeys'][1]
    elif bad=='lookup_order':msg['accountKeys'][2]['source']='lookupTable'
    assert launch_facts(tx,commitment='confirmed')['state']=='unknown'


def test_consistent_optional_header_and_loaded_readonly_program_are_valid():
    tx,*_=receipt();msg=tx['transaction']['message']
    msg['header']=dict(numRequiredSignatures=2,numReadonlySignedAccounts=0,numReadonlyUnsignedAccounts=1)
    assert launch_facts(tx,commitment='confirmed')['state']=='known'
    msg['accountKeys'][-1]['source']='lookupTable';msg['header']['numReadonlyUnsignedAccounts']=0
    assert launch_facts(tx,commitment='confirmed')['state']=='known'
