"""Offline confirmed-provider metadata codec for research, with no stream or order authority."""
import struct
import base64
import json
import zlib

import base58
from solders.errors import BincodeError
from solders.instruction import AccountMeta,Instruction
from solders.pubkey import Pubkey
from solders.system_program import create_account_with_seed,decode_create_account_with_seed

from sniper.services.memecoin_shyft_confirmed import _instruction,_pubkey,SYSTEM,TOKEN_PROGRAMS


MAX_RAW_BYTES=128*1024


def encode_payload(raw):
    """Lossless local storage encoding, independent of chain/receipt validation."""
    if not isinstance(raw,bytes) or len(raw)>MAX_RAW_BYTES:raise ValueError('stream_payload_raw_bound')
    return json.dumps(dict(encoding='zlib-v1',data=base64.b64encode(zlib.compress(raw,1)).decode('ascii'),raw_size=len(raw)),separators=(',',':'))


def decode_payload(payload_json,*,archive_dir=None):
    """Read legacy or zlib-v1 bytes with the same128KiB bound; malformed input raises ValueError."""
    try:
        if not isinstance(payload_json,str) or len(payload_json.encode())>256*1024:raise ValueError('stream_payload_bound')
        value=json.loads(payload_json)
        if not isinstance(value,dict):raise ValueError('stream_payload_shape')
        if set(value)=={'archive_payload_v1'}:
            if archive_dir is None:raise ValueError('stream_payload_archive_unavailable')
            from sniper.office.wallet_flow_archive import read_payload
            packed_json=read_payload(value['archive_payload_v1'],archive_dir)
            return decode_payload(packed_json)  # No archive_dir: nested references fail closed before another read.
        legacy=set(value)=={'raw_update_b64'}
        if legacy:encoded=value['raw_update_b64']
        elif set(value)=={'encoding','data','raw_size'} and value['encoding']=='zlib-v1':
            size=value['raw_size']
            if type(size) is not int or not 0<=size<=MAX_RAW_BYTES:raise ValueError('stream_payload_raw_bound')
            encoded=value['data']
        else:raise ValueError('stream_payload_encoding')
        if not isinstance(encoded,str) or len(encoded)>175000:raise ValueError('stream_payload_base64_bound')
        packed=base64.b64decode(encoded,validate=True)
        if legacy:raw=packed
        else:
            decoder=zlib.decompressobj();raw=decoder.decompress(packed,MAX_RAW_BYTES+1)
            if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or len(raw)!=size:
                raise ValueError('stream_payload_compressed_invalid')
        if len(raw)>MAX_RAW_BYTES:raise ValueError('stream_payload_raw_bound')
        return raw
    except (TypeError,KeyError,ImportError,OSError,zlib.error) as exc:
        raise ValueError('stream_payload_invalid') from exc


def _decode(ix,keys,signers,writable):
    if ix.program_id_index>=len(keys) or any(a>=len(keys) for a in ix.accounts):
        raise ValueError('instruction_index_invalid')
    data=bytes(ix.data)
    if keys[ix.program_id_index] in TOKEN_PROGRAMS and data[:1]==b'\x0e':
        # Research metadata only: preserve MintToChecked without widening funded payment parsing.
        if (len(data)!=10 or len(ix.accounts)!=3 or data[9]>18
                or ix.accounts[0]==ix.accounts[1] or any(i not in writable for i in ix.accounts[:2])):
            raise ValueError('token_mint_to_checked_invalid')
        accounts=[keys[i] for i in ix.accounts]
        parsed=dict(programId=keys[ix.program_id_index],accounts=accounts,data=base58.b58encode(data).decode(),
            parsed=dict(type='mintToChecked',info=dict(mint=accounts[0],account=accounts[1],mintAuthority=accounts[2],
                tokenAmount=dict(amount=str(struct.unpack('<Q',data[1:9])[0]),decimals=data[9]))))
        if hasattr(ix,'stack_height') and ix.HasField('stack_height'):
            parsed['stackHeight']=ix.stack_height
    else:
        parsed=_instruction(ix,keys,inventory_only=True)
    if parsed['programId'] in TOKEN_PROGRAMS and 'parsed' in parsed:
        # The shared helper's suffix label is wrong for legacy SPL; bind the label to its exact id.
        parsed['program']='spl-token' if parsed['programId']=='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA' else 'spl-token-2022'
    if parsed['programId']!=SYSTEM or bytes(ix.data[:4])!=struct.pack('<I',3):return parsed
    accounts=[AccountMeta(Pubkey.from_string(keys[i]),i in signers,i in writable) for i in ix.accounts]
    instruction=Instruction(Pubkey.from_string(SYSTEM),bytes(ix.data),accounts)
    fields=decode_create_account_with_seed(instruction)
    canonical=create_account_with_seed(fields)
    if (bytes(canonical.data)!=bytes(ix.data) or len(fields['seed'].encode())>32
            or [m.pubkey for m in canonical.accounts]!=[m.pubkey for m in accounts]
            or any((expected.is_signer and not actual.is_signer) or (expected.is_writable and not actual.is_writable)
                   for expected,actual in zip(canonical.accounts,accounts))):
        raise ValueError('seeded_instruction_invalid')
    parsed.update(program='system',parsed=dict(type='createAccountWithSeed',info=dict(
        source=str(fields['from_pubkey']),newAccount=str(fields['to_pubkey']),base=str(fields['base']),
        seed=fields['seed'],lamports=fields['lamports'],space=fields['space'],owner=str(fields['owner']))))
    return parsed


def _tokens(rows,count):
    out=[];seen=set()
    for b in rows:
        if (b.account_index>=count or b.account_index in seen or not b.HasField('ui_token_amount')
                or b.program_id not in TOKEN_PROGRAMS):raise ValueError('token_balance_invalid')
        seen.add(b.account_index)
        mint=str(Pubkey.from_string(b.mint));owner=str(Pubkey.from_string(b.owner))
        amount=b.ui_token_amount.amount;decimals=b.ui_token_amount.decimals
        if (not amount or len(amount)>20 or not amount.isascii() or not amount.isdigit()
                or int(amount)>=2**64 or decimals>255):raise ValueError('token_amount_invalid')
        out.append(dict(accountIndex=b.account_index,mint=mint,owner=owner,programId=b.program_id,
                        uiTokenAmount=dict(amount=amount,decimals=decimals)))
    return out


def confirmed_receipt(update,*,commitment,block_time):
    """RPC-shaped metadata for existing research proofs, or ValueError on incomplete input.

    Caller must attest authenticated CONFIRMED/FINALIZED origin; protobuf has no commitment.
    Explicit block_time is a supplied block fact, never a host receipt or default zero.
    None retains balance observations with clock unknown; funding proof must wait for block time.
    This validates structure and signer bindings, not cryptographic signatures or execution authority.
    """
    if commitment not in ('confirmed','finalized') or (block_time is not None and (type(block_time) is not int or block_time<=0)):
        raise ValueError('confirmed_block_fact_required')
    try:
        if not update.HasField('transaction') or update.ByteSize()>1024*1024:raise ValueError('frame_invalid')
        envelope=update.transaction;info=envelope.transaction
        if (envelope.slot<=0 or not envelope.HasField('transaction') or info.is_vote
                or not info.HasField('transaction') or not info.HasField('meta') or info.meta.HasField('err')):
            raise ValueError('successful_metadata_required')
        tx=info.transaction;meta=info.meta;msg=tx.message
        if not tx.HasField('message') or not msg.HasField('header'):raise ValueError('message_header_required')
        n=msg.header.num_required_signatures;rs=msg.header.num_readonly_signed_accounts;ru=msg.header.num_readonly_unsigned_accounts
        if (not 1<=n<=len(msg.account_keys)<=256 or not 0<=rs<n or not 0<=ru<=len(msg.account_keys)-n
                or len(tx.signatures)!=n or len(info.signature)!=64 or tx.signatures[0]!=info.signature
                or any(len(s)!=64 or not any(s) for s in tx.signatures) or len(msg.recent_blockhash)!=32):
            raise ValueError('message_or_signature_invalid')
        static=[_pubkey(k) for k in msg.account_keys];loaded_w=[_pubkey(k) for k in meta.loaded_writable_addresses];loaded_r=[_pubkey(k) for k in meta.loaded_readonly_addresses]
        keys=static+loaded_w+loaded_r
        if (len(keys)>256 or len(set(keys))!=len(keys)
                or (not msg.versioned and (msg.address_table_lookups or loaded_w or loaded_r))
                or sum(len(v.writable_indexes) for v in msg.address_table_lookups)!=len(loaded_w)
                or sum(len(v.readonly_indexes) for v in msg.address_table_lookups)!=len(loaded_r)):
            raise ValueError('loaded_keys_invalid')
        for v in msg.address_table_lookups:
            _pubkey(v.account_key)
            indices=bytes(v.writable_indexes)+bytes(v.readonly_indexes)
            if len(set(indices))!=len(indices):raise ValueError('lookup_indices_invalid')
        if (len(meta.pre_balances)!=len(keys) or len(meta.post_balances)!=len(keys) or meta.fee<=0
                or meta.inner_instructions_none or meta.log_messages_none or not meta.log_messages
                or not 1<=len(msg.instructions)<=128 or sum(len(g.instructions) for g in meta.inner_instructions)>512):
            raise ValueError('complete_balances_and_execution_metadata_required')
        signers=set(range(n));writable=set(range(n-rs))|set(range(n,len(static)-ru))|set(range(len(static),len(static)+len(loaded_w)))
        top=[_decode(ix,keys,signers,writable) for ix in msg.instructions]
        inner=[];seen=set()
        for g in meta.inner_instructions:
            if g.index>=len(top) or g.index in seen:raise ValueError('inner_group_invalid')
            seen.add(g.index)
            if any(not ix.HasField('stack_height') or not 2<=ix.stack_height<=64 for ix in g.instructions):
                raise ValueError('inner_stack_unknown')
            inner.append(dict(index=g.index,instructions=[_decode(ix,keys,signers,writable) for ix in g.instructions]))
        pre=_tokens(meta.pre_token_balances,len(keys));post=_tokens(meta.post_token_balances,len(keys))
        identity=lambda b:(b['accountIndex'],b['mint'],b['owner'],b['programId'],b['uiTokenAmount']['decimals'])
        # No token row is not zero inventory; unmatched inventory sides remain unknown to movements().
        token_known=bool(pre) and {identity(b) for b in pre}=={identity(b) for b in post}
        return dict(slot=envelope.slot,blockTime=block_time,transactionIndex=info.index or None,
            clock_state='unknown' if block_time is None else 'known',
            clock_reason='block_time_pending' if block_time is None else None,
            commitment=commitment,transaction=dict(signatures=[base58.b58encode(s).decode() for s in tx.signatures],
                message=dict(accountKeys=[dict(pubkey=k,signer=i in signers,writable=i in writable,
                    source='transaction' if i<len(static) else 'lookupTable') for i,k in enumerate(keys)],
                    recentBlockhash=base58.b58encode(msg.recent_blockhash).decode(),instructions=top)),
            meta=dict(err=None,fee=meta.fee,preBalances=list(meta.pre_balances),postBalances=list(meta.post_balances),
                innerInstructions=inner,logMessages=list(meta.log_messages),
                preTokenBalances=pre if token_known else None,postTokenBalances=post if token_known else None,
                preTokenBalanceObservations=pre,postTokenBalanceObservations=post,
                token_balances_state='known' if token_known else 'unknown'))
    except (ValueError,TypeError,IndexError,OverflowError,AttributeError,BincodeError) as exc:
        raise ValueError('stream_receipt_unproven:'+str(exc)) from exc
