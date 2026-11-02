"""Offline provider-frame compilation: no connections, accounts or credentials."""
import struct

import base58
import pytest
from solders.pubkey import Pubkey
from solders.system_program import create_account_with_seed,decode_create_account_with_seed
from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.office.wallet_stream_proof import confirmed_receipt
from sniper.services.wallet_identity import verified_sol_funding
from sniper.office.wallet_watch_collect import movements

TOKEN='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
SYSTEM='11111111111111111111111111111111'
WSOL='So11111111111111111111111111111111111111112'
A,B,C=(str(Pubkey.from_bytes(bytes([n])*32)) for n in (1,2,3))
NOW='2026-11-01T14:00:00+00:00';BLOCK=1793534467


def compile_receipt(raw):
    """Compile a jsonParsed saved receipt into the provider's full protobuf offline."""
    u=pb.SubscribeUpdate();u.transaction.slot=raw['slot']
    info=u.transaction.transaction;tx=info.transaction;meta=info.meta
    signatures=[base58.b58decode(s) for s in raw['transaction']['signatures']]
    tx.signatures.extend(signatures);info.signature=signatures[0]
    info.index=raw.get('transactionIndex') or 0
    source=raw['transaction']['message'];keys=source['accountKeys']
    static=[k for k in keys if k.get('source','transaction')=='transaction']
    msg=tx.message;msg.header.num_required_signatures=sum(k['signer'] for k in static)
    msg.header.num_readonly_signed_accounts=sum(k['signer'] and not k['writable'] for k in static)
    msg.header.num_readonly_unsigned_accounts=sum(not k['signer'] and not k['writable'] for k in static)
    msg.account_keys.extend(base58.b58decode(k['pubkey']) for k in static)
    msg.recent_blockhash=base58.b58decode(source['recentBlockhash'])
    allkeys=[k['pubkey'] for k in keys]
    for ix in source['instructions']:
        target=msg.instructions.add();target.program_id_index=allkeys.index(ix['programId'])
        if 'parsed' not in ix:
            target.accounts=bytes(ix['accounts']);target.data=base58.b58decode(ix['data']);continue
        kind=ix['parsed']['type'];v=ix['parsed']['info']
        if kind=='createAccountWithSeed':
            built=create_account_with_seed(dict(from_pubkey=Pubkey.from_string(v['source']),to_pubkey=Pubkey.from_string(v['newAccount']),base=Pubkey.from_string(v['base']),seed=v['seed'],lamports=v['lamports'],space=v['space'],owner=Pubkey.from_string(v['owner'])))
            target.accounts=bytes(allkeys.index(str(a.pubkey)) for a in built.accounts);target.data=bytes(built.data)
        elif kind=='initializeAccount':
            target.accounts=bytes(allkeys.index(v[k]) for k in ('account','mint','owner'))
            target.data=b'\x01'
        elif kind=='closeAccount':
            target.accounts=bytes(allkeys.index(v[k]) for k in ('account','destination','owner'));target.data=b'\x09'
        elif kind=='transfer':
            target.accounts=bytes(allkeys.index(v[k]) for k in ('source','destination'));target.data=struct.pack('<IQ',2,v['lamports'])
        else:raise AssertionError(kind)
    meta.fee=raw['meta']['fee'];meta.pre_balances.extend(raw['meta']['preBalances']);meta.post_balances.extend(raw['meta']['postBalances'])
    meta.log_messages.extend(raw['meta']['logMessages'])
    meta.log_messages_none=raw['meta']['logMessages'] is None
    for side,field in (('preTokenBalances',meta.pre_token_balances),('postTokenBalances',meta.post_token_balances)):
        for r in raw['meta'].get(side) or []:
            b=field.add();b.account_index=r['accountIndex'];b.owner=r.get('owner','');b.mint=r['mint'];b.program_id=r.get('programId',TOKEN)
            b.ui_token_amount.amount=r['uiTokenAmount']['amount'];b.ui_token_amount.decimals=r['uiTokenAmount']['decimals']
    return u


def fixture():
    seed='proof';temp=str(Pubkey.create_with_seed(Pubkey.from_string(B),seed,Pubkey.from_string(TOKEN)))
    keys=[dict(pubkey=k,signer=k in (A,B),writable=k not in (SYSTEM,TOKEN,WSOL)) for k in (A,B,temp,C,SYSTEM,TOKEN,WSOL)]
    raw=dict(slot=464084492,transactionIndex=0,blockTime=BLOCK,transaction=dict(signatures=[base58.b58encode(bytes([n])*64).decode() for n in (4,5)],message=dict(accountKeys=keys,recentBlockhash=SYSTEM,instructions=[
        dict(programId=SYSTEM,parsed=dict(type='createAccountWithSeed',info=dict(source=A,base=B,newAccount=temp,seed=seed,owner=TOKEN,space=165,lamports=20000))),
        dict(programId=TOKEN,parsed=dict(type='initializeAccount',info=dict(account=temp,mint=WSOL,owner=B))),
        dict(programId=TOKEN,parsed=dict(type='closeAccount',info=dict(account=temp,destination=C,owner=B))) ])),
        meta=dict(err=None,fee=10000,preBalances=[100000,0,0,0,0,0,0],postBalances=[70000,0,0,20000,0,0,0],logMessages=['Program success'],innerInstructions=[],preTokenBalances=[],postTokenBalances=[]))
    for ix in raw['transaction']['message']['instructions']:
        ix['program']='system' if ix['programId']==SYSTEM else 'spl-token'
    return raw,compile_receipt(raw)


def test_seeded_frame_reuses_existing_funding_and_movement_proofs():
    raw,u=fixture();out=confirmed_receipt(u,commitment='confirmed',block_time=BLOCK)
    assert verified_sol_funding(out,NOW)==verified_sol_funding(raw,NOW)
    native=movements(out,C)[0]
    assert native['state']=='known' and native['delta_atomic']=='20000'
    assert out['transactionIndex'] is None
    assert out['meta']['preTokenBalances'] is None and out['meta']['postTokenBalances'] is None
    assert any(r['state']=='unknown' for r in movements(out,C))


@pytest.mark.parametrize('commitment',['processed','unknown',None,0])
def test_unproved_commitment_refused(commitment):
    _,u=fixture()
    with pytest.raises(ValueError):confirmed_receipt(u,commitment=commitment,block_time=BLOCK)


@pytest.mark.parametrize('block',[0,-1,True,float('nan')])
def test_explicit_block_time_required(block):
    _,u=fixture()
    with pytest.raises(ValueError):confirmed_receipt(u,commitment='confirmed',block_time=block)


@pytest.mark.parametrize('bad',['signature','signature_count','header','meta','failed','balance_count','loaded_keys','instruction_index','fee_unknown','inner_unknown','seed_trailing','seed_base','blockhash','logs_unknown','readonly_payer','unsigned_seed_base'])
def test_incomplete_or_inconsistent_frames_refused(bad):
    _,u=fixture();info=u.transaction.transaction;msg=info.transaction.message;meta=info.meta
    if bad=='signature':info.signature=b'x'
    elif bad=='signature_count':info.transaction.signatures.pop()
    elif bad=='header':msg.ClearField('header')
    elif bad=='meta':info.ClearField('meta')
    elif bad=='failed':meta.err.err=b'failed'
    elif bad=='balance_count':meta.post_balances.pop()
    elif bad=='loaded_keys':meta.loaded_writable_addresses.append(bytes([7])*32)
    elif bad=='instruction_index':msg.instructions[0].program_id_index=99
    elif bad=='fee_unknown':meta.fee=0
    elif bad=='inner_unknown':meta.inner_instructions_none=True
    elif bad=='seed_trailing':msg.instructions[0].data+=b'x'
    elif bad=='seed_base':msg.instructions[0].accounts=bytes([0,2,0])
    elif bad=='blockhash':msg.recent_blockhash=b''
    elif bad=='logs_unknown':meta.log_messages_none=True;del meta.log_messages[:]
    elif bad=='readonly_payer':msg.header.num_readonly_signed_accounts=2
    elif bad=='unsigned_seed_base':msg.header.num_required_signatures=1;info.transaction.signatures.pop()
    with pytest.raises(ValueError):confirmed_receipt(u,commitment='confirmed',block_time=BLOCK)


def test_direct_native_transfer_keeps_existing_proof():
    raw,_=fixture();raw['transaction']['message']['instructions']=[dict(programId=SYSTEM,parsed=dict(type='transfer',info=dict(source=A,destination=C,lamports=20000)))]
    out=confirmed_receipt(compile_receipt(raw),commitment='finalized',block_time=BLOCK)
    clue,=verified_sol_funding(out,NOW)
    assert clue['method']=='confirmed_system_transfer' and clue['recipient']==C


def test_valid_loaded_readonly_key_and_positive_transaction_index():
    raw,u=fixture();info=u.transaction.transaction;msg=info.transaction.message
    msg.versioned=True;loaded=msg.account_keys.pop();msg.header.num_readonly_unsigned_accounts-=1
    info.meta.loaded_readonly_addresses.append(loaded)
    lookup=msg.address_table_lookups.add();lookup.account_key=bytes([9])*32;lookup.readonly_indexes=b'\x00'
    info.index=7
    out=confirmed_receipt(u,commitment='confirmed',block_time=BLOCK)
    assert out['transactionIndex']==7
    assert out['transaction']['message']['accountKeys'][-1]==dict(pubkey=WSOL,signer=False,writable=False,source='lookupTable')
    assert verified_sol_funding(out,NOW)==verified_sol_funding(raw,NOW)


def test_matching_token_inventory_yields_exact_atomic_movements():
    _,u=fixture();meta=u.transaction.transaction.meta
    for side,amount in ((meta.pre_token_balances,'100'),(meta.post_token_balances,'30')):
        b=side.add();b.account_index=3;b.owner=C;b.mint=WSOL;b.program_id=TOKEN;b.ui_token_amount.amount=amount;b.ui_token_amount.decimals=9
    out=confirmed_receipt(u,commitment='confirmed',block_time=BLOCK)
    token,=[r for r in movements(out,C) if r['asset']==WSOL]
    assert token['delta_atomic']=='-70' and token['state']=='known'
    del meta.pre_token_balances[:]
    unknown=confirmed_receipt(u,commitment='confirmed',block_time=BLOCK)
    assert unknown['meta']['postTokenBalances'] is None
    assert any(r['state']=='unknown' for r in movements(unknown,C))


@pytest.mark.parametrize('change',['truncated','seed_oversize','duplicate_keys','duplicate_lookup','missing_ui','stack_unknown'])
def test_additional_metadata_boundaries(change):
    _,u=fixture();info=u.transaction.transaction;msg=info.transaction.message;meta=info.meta
    if change=='truncated':msg.instructions[0].data=msg.instructions[0].data[:20]
    elif change=='seed_oversize':
        ix=msg.instructions[0];ix.data=bytes(create_account_with_seed(dict(from_pubkey=Pubkey.from_string(A),to_pubkey=Pubkey.from_bytes(msg.account_keys[2]),base=Pubkey.from_string(B),seed='s'*33,lamports=20000,space=165,owner=Pubkey.from_string(TOKEN))).data)
    elif change=='duplicate_keys':msg.account_keys[3]=msg.account_keys[0]
    elif change=='duplicate_lookup':
        msg.versioned=True
        lookup=msg.address_table_lookups.add();lookup.account_key=bytes([9])*32;lookup.readonly_indexes=b'\x01\x01'
        meta.loaded_readonly_addresses.extend([bytes([7])*32,bytes([8])*32]);meta.pre_balances.extend([0,0]);meta.post_balances.extend([0,0])
    elif change=='missing_ui':meta.pre_token_balances.add().account_index=0
    elif change=='stack_unknown':
        group=meta.inner_instructions.add();group.index=0;ix=group.instructions.add();ix.program_id_index=4;ix.accounts=b'\x00\x03';ix.data=struct.pack('<IQ',2,1)
    with pytest.raises(ValueError):confirmed_receipt(u,commitment='confirmed',block_time=BLOCK)


def test_missing_block_time_keeps_balance_observation_but_cannot_prove_funding():
    _,u=fixture()
    out=confirmed_receipt(u,commitment='confirmed',block_time=None)
    assert out['blockTime'] is None and out['clock_state']=='unknown'
    assert out['clock_reason']=='block_time_pending'
    assert movements(out,C)[0]['delta_atomic']=='20000'
    with pytest.raises(ValueError):verified_sol_funding(out,NOW)
    known=confirmed_receipt(u,commitment='confirmed',block_time=BLOCK)
    assert known['clock_state']=='known' and known['clock_reason'] is None
    assert verified_sol_funding(known,NOW)


def mint_setup_frame():
    _,u=fixture();msg=u.transaction.transaction.transaction.message
    del msg.instructions[:]
    msg.instructions.add(program_id_index=5,accounts=bytes([2]),data=bytes([20,6])+bytes([1])*32+b'\0')
    msg.instructions.add(program_id_index=5,accounts=bytes([2,3,0]),data=b'\x07'+struct.pack('<Q',100))
    return u


def test_research_mint_inventory_is_readable_without_funding_authority():
    u=mint_setup_frame()
    out=confirmed_receipt(u,commitment='confirmed',block_time=BLOCK)
    assert [r['parsed']['type'] for r in out['transaction']['message']['instructions']]==['inventoryMintSetup','inventoryMintTo']
    assert verified_sol_funding(out,NOW)==[]
    assert movements(out,C)[-1]['state']=='unknown'
    from sniper.services.memecoin_shyft_confirmed import _instruction
    keys=[base58.b58encode(k).decode() for k in u.transaction.transaction.transaction.message.account_keys]
    with pytest.raises(ValueError,match='unsupported'):
        _instruction(u.transaction.transaction.transaction.message.instructions[0],keys)


@pytest.mark.parametrize('data',[b'\x14\x06',b'\x07',b'\xff'])
def test_research_mint_inventory_does_not_accept_malformed_or_unknown(data):
    u=mint_setup_frame();u.transaction.transaction.transaction.message.instructions[0].data=data
    with pytest.raises(ValueError):confirmed_receipt(u,commitment='confirmed',block_time=BLOCK)
