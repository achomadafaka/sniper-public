"""Root-approved research-only MintToChecked native receipt acceptance."""
import struct

import base58
import pytest

import test_known_group_canary as C
from test_known_group_canary_worker import compile_native
from sniper.office.wallet_stream_proof import confirmed_receipt
from sniper.services.wallet_identity import verified_sol_funding

TOKEN_2022='TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'


def native_mint(*,program=C.TOKEN,decimals=6,amount=600_000_000_000_000):
    update=compile_native(C.creation());msg=update.transaction.transaction.transaction.message
    del msg.instructions[0]
    program_index=next(i for i,k in enumerate(msg.account_keys) if base58.b58encode(k).decode()==C.TOKEN)
    msg.account_keys[program_index]=base58.b58decode(program)
    msg.instructions[0].data=struct.pack('<BQB',14,amount,decimals)
    return update


def decoded(update):
    return confirmed_receipt(update,commitment='confirmed',block_time=int(C.R)-1)


def assert_native_mint(update,*,program=C.TOKEN,decimals=6,amount=600_000_000_000_000):
    tx=decoded(update);instruction,=tx['transaction']['message']['instructions']
    raw=update.transaction.transaction.transaction.message.instructions[0]
    assert instruction['programId']==program
    assert instruction['program']==('spl-token' if program==C.TOKEN else 'spl-token-2022')
    assert instruction['data']==base58.b58encode(raw.data).decode()
    assert instruction['accounts']==[C.MINT,C.MINT_ACCOUNT,C.DEV]
    assert instruction['parsed']==dict(type='mintToChecked',info=dict(mint=C.MINT,account=C.MINT_ACCOUNT,mintAuthority=C.DEV,
        tokenAmount=dict(amount=str(amount),decimals=decimals)))
    assert verified_sol_funding(tx,C.iso(C.R))==[],'minting must never become economic funding proof'
    return tx


@pytest.mark.parametrize('program',[C.TOKEN,TOKEN_2022])
@pytest.mark.parametrize('decimals',[0,6,18])
def test_native_checked_mint_preserves_exact_program_accounts_data_and_amount_without_funding(program,decimals):
    amount=0 if decimals==0 else 600_000_000_000_000
    update=native_mint(program=program,decimals=decimals,amount=amount)
    assert_native_mint(update,program=program,decimals=decimals,amount=amount)


@pytest.mark.parametrize('bad',['short_data','long_data','two_accounts','four_accounts','decimals_19','same_mint_recipient',
    'unbound_authority','readonly_recipient','readonly_mint','failed_receipt'])
def test_supported_checked_mint_refuses_malformed_or_unsuccessful_native_evidence(bad):
    # The supported instruction must decode before the malformed form is meaningful.
    assert_native_mint(native_mint())
    update=native_mint();msg=update.transaction.transaction.transaction.message;ix=msg.instructions[0]
    if bad=='short_data':ix.data=bytes(ix.data[:-1])
    elif bad=='long_data':ix.data=bytes(ix.data)+b'\0'
    elif bad=='two_accounts':ix.accounts=bytes(ix.accounts[:2])
    elif bad=='four_accounts':ix.accounts=bytes(ix.accounts)+bytes([ix.accounts[-1]])
    elif bad=='decimals_19':ix.data=bytes(ix.data[:-1])+b'\x13'
    elif bad=='same_mint_recipient':ix.accounts=bytes([ix.accounts[0],ix.accounts[0],ix.accounts[2]])
    elif bad=='unbound_authority':ix.accounts=bytes([ix.accounts[0],ix.accounts[1],255])
    elif bad=='readonly_recipient':msg.header.num_readonly_unsigned_accounts+=1
    elif bad=='readonly_mint':msg.header.num_readonly_signed_accounts=1
    elif bad=='failed_receipt':update.transaction.transaction.meta.err.err=b'failed'
    with pytest.raises(ValueError):decoded(update)
