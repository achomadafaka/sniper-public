"""Selective Pump launch facts from caller-attested confirmed research receipts."""
import base58
from solders.pubkey import Pubkey
from sniper.services.memecoin_desk_backfill_history import PUMP, _borsh_creator

LAYOUTS={bytes.fromhex('181ec828051c0777'):(7,14),bytes.fromhex('d6904cec5f8b31b4'):(5,16)}
NOTE='Top-level Pump instruction evidence only; empty items are not launch absence or ownership proof.'


def launch_facts(tx,*,commitment):
    """Bind signing developer and creator argument separately; no RPC, control or safety claim.

    Caller attests confirmed/finalized source. CPI/PDA creates and other programs are unhandled.
    Existing decoder reads the stable creator prefix; trailing policy arguments are not safety evidence.
    """
    try:
        if commitment not in ('confirmed','finalized'):raise ValueError('commitment')
        if type(tx['slot']) is not int or not 0<tx['slot']<2**63 or type(tx['blockTime']) is not int or tx['blockTime']<=0:raise ValueError('clock')
        if 'err' not in tx['meta'] or tx['meta']['err'] is not None:raise ValueError('success')
        transaction=tx['transaction'];msg=transaction['message'];rows=msg['accountKeys'];ixs=msg['instructions']
        if not isinstance(rows,list) or not 1<=len(rows)<=256 or not isinstance(ixs,list) or len(ixs)>128:raise ValueError('message')
        keys={}
        for row in rows:
            address=row['pubkey']
            if str(Pubkey.from_string(address))!=address or address in keys or type(row['signer']) is not bool or type(row['writable']) is not bool:raise ValueError('keys')
            keys[address]=row
        static=[];loaded=[]
        for row in rows:
            source=row.get('source','transaction')
            if source not in ('transaction','lookupTable') or (source=='transaction' and loaded):raise ValueError('key_source')
            (loaded if source=='lookupTable' else static).append(row)
        n=sum(k['signer'] for k in static)
        if any(k['signer'] for k in loaded) or [k['signer'] for k in static]!=[True]*n+[False]*(len(static)-n):raise ValueError('signer_prefix')
        for section in (static[:n],static[n:],loaded):
            writable=sum(k['writable'] for k in section)
            if [k['writable'] for k in section]!=[True]*writable+[False]*(len(section)-writable):raise ValueError('writable_prefix')
        if not static or not static[0]['signer'] or not static[0]['writable']:raise ValueError('payer')
        if 'header' in msg:
            header=msg['header']
            expected=dict(numRequiredSignatures=n,numReadonlySignedAccounts=sum(not k['writable'] for k in static[:n]),
                          numReadonlyUnsignedAccounts=sum(not k['writable'] for k in static[n:]))
            if not isinstance(header,dict) or any(type(header.get(k)) is not int or header[k]!=v for k,v in expected.items()):raise ValueError('header')
        signatures=transaction['signatures']
        if not isinstance(signatures,list) or len(signatures)!=sum(k['signer'] for k in rows) or not signatures:raise ValueError('signatures')
        for signature in signatures:
            if not isinstance(signature,str) or not 64<=len(signature)<=88:raise ValueError('signature')
            raw=base58.b58decode(signature)
            if len(raw)!=64 or not any(raw):raise ValueError('signature')
        items=[];seen=set()
        for ordinal,ix in enumerate(ixs):
            if ix.get('programId')!=PUMP:continue
            if not isinstance(ix.get('data'),str) or len(ix['data'])>6000:raise ValueError('data')
            raw=base58.b58decode(ix['data'])
            if len(raw)>4096:raise ValueError('data')
            layout=LAYOUTS.get(raw[:8])
            if layout is None:continue
            accounts=[a if isinstance(a,str) else a['pubkey'] for a in ix['accounts']]
            creator=_borsh_creator(raw)
            user_index,minimum=layout
            if not minimum<=len(accounts)<=64 or any(a not in keys for a in accounts) or PUMP not in keys:raise ValueError('accounts')
            mint,developer,curve=accounts[0],accounts[user_index],accounts[2]
            if mint==developer or any(not keys[a]['signer'] or not keys[a]['writable'] for a in (mint,developer)):raise ValueError('signers')
            if str(Pubkey.from_string(creator))!=creator:raise ValueError('creator')
            expected=Pubkey.find_program_address([b'bonding-curve',bytes(Pubkey.from_string(mint))],Pubkey.from_string(PUMP))[0]
            if curve!=str(expected) or mint in seen:raise ValueError('mint_binding')
            seen.add(mint)
            items.append(dict(mint=mint,developer=developer,creator_argument=creator,curve=curve,
                signature=signatures[0],slot=tx['slot'],block_time=tx['blockTime'],outer_ix=ordinal,
                commitment=commitment,method='confirmed_pump_create_v1',ownership_proven=False))
        return dict(state='known',items=items,coverage='top_level_pump_only',note=NOTE)
    except (KeyError,TypeError,ValueError,IndexError,AttributeError,OverflowError):
        return dict(state='unknown',reason='launch_evidence_unproven',items=[],coverage='top_level_pump_only',note=NOTE)
