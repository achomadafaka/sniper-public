"""Offline account observations and explicit token instructions, never wallet control or PnL."""
import json
import math

from solders.pubkey import Pubkey

TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN22 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
MAX_ROWS = 512
SCHEMA = '''
CREATE TABLE IF NOT EXISTS watch_token_accounts(signature TEXT NOT NULL,account TEXT NOT NULL,
 mint TEXT,owner TEXT,program TEXT,decimals INTEGER,pre_atomic TEXT,post_atomic TEXT,delta_atomic TEXT,
 state TEXT NOT NULL,reason TEXT,fetch_id TEXT NOT NULL,source TEXT,flow_id INTEGER,known_at REAL,PRIMARY KEY(signature,account));
CREATE INDEX IF NOT EXISTS watch_token_accounts_owner ON watch_token_accounts(owner,mint);
CREATE TABLE IF NOT EXISTS watch_token_transfers(signature TEXT NOT NULL,outer_ix INTEGER NOT NULL,inner_ix INTEGER NOT NULL,
 source_account TEXT,destination_account TEXT,source_owner TEXT,destination_owner TEXT,mint TEXT,program TEXT,
 amount_atomic TEXT,decimals INTEGER,fee_atomic TEXT,credit_atomic TEXT,authority TEXT,state TEXT NOT NULL,reason TEXT,
 instruction_json TEXT NOT NULL,fetch_id TEXT NOT NULL,source TEXT,flow_id INTEGER,known_at REAL,PRIMARY KEY(signature,outer_ix,inner_ix));
CREATE INDEX IF NOT EXISTS watch_token_transfers_source ON watch_token_transfers(source_owner,mint);
CREATE INDEX IF NOT EXISTS watch_token_transfers_destination ON watch_token_transfers(destination_owner,mint);
'''


def init_storage(conn):
    """Initialize research tables; old facts keep NULL causal context, never backfilled clocks."""
    conn.executescript(SCHEMA)
    for table in ('watch_token_accounts','watch_token_transfers'):
        columns={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
        for name,kind in [('source','TEXT'),('flow_id','INTEGER'),('known_at','REAL')]:
            if name not in columns:conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')


def _address(value):
    return str(Pubkey.from_string(value))


def _amount(value):
    if not isinstance(value,str) or not value.isascii() or not value.isdigit() or len(value)>20 or int(value)>=2**64:
        raise ValueError('token_amount_invalid')
    return value


def _decimals(value):
    if type(value) is not int or not 0<=value<=255:raise ValueError('token_decimals_invalid')
    return value


def token_flow(tx):
    """Explicit transfers and per-account pre/post inventory; missing sides stay NULL.

    Successful receipt structure is validated upstream. This independently refuses ambiguous
    account identities. Instruction amount is not a wallet's aggregate balance change.
    """
    out=dict(accounts=[],transfers=[],state='known',reason=None,
             coverage='explicit token instructions and observed accounts only; no absence or control proof')
    try:
        meta=tx['meta'];msg=tx['transaction']['message']
        if meta.get('err') is not None:raise ValueError('successful_metadata_required')
        keys=[_address(k['pubkey'] if isinstance(k,dict) else k) for k in msg['accountKeys']]
        sides=[]
        for raw,legacy in [('preTokenBalanceObservations','preTokenBalances'),('postTokenBalanceObservations','postTokenBalances')]:
            rows=meta.get(raw,meta.get(legacy))
            if not isinstance(rows,list) or len(rows)>256:raise ValueError('token_observations_unknown')
            side={}
            for row in rows:
                i=row['accountIndex']
                if type(i) is not int or not 0<=i<len(keys) or i in side:raise ValueError('token_account_index_invalid')
                program=row['programId']
                if program not in (TOKEN,TOKEN22):raise ValueError('token_program_unknown')
                side[i]=dict(mint=_address(row['mint']),owner=_address(row['owner']),program=program,
                             decimals=_decimals(row['uiTokenAmount']['decimals']),amount=_amount(row['uiTokenAmount']['amount']))
            sides.append(side)
        for i in sorted(set(sides[0])|set(sides[1])):
            pre,post=(s.get(i) for s in sides);identity=pre or post
            mismatch=pre and post and any(pre[k]!=post[k] for k in ('mint','owner','program','decimals'))
            reason='token_identity_changed' if mismatch else 'token_balance_side_missing' if pre is None or post is None else None
            item=dict(account=keys[i],**{k:None if mismatch else identity[k] for k in ('mint','owner','program','decimals')},
                      pre_atomic=pre['amount'] if pre else None,post_atomic=post['amount'] if post else None,
                      delta_atomic=str(int(post['amount'])-int(pre['amount'])) if not reason else None,
                      state='unknown' if reason else 'known',reason=reason,
                      observed_owners=sorted({v['owner'] for v in (pre,post) if v}))
            out['accounts'].append(item)
        accounts={a['account']:a for a in out['accounts']}
        instructions=[(i,-1,ix) for i,ix in enumerate(msg['instructions'])]
        for group in meta.get('innerInstructions') or []:
            if type(group['index']) is not int or not 0<=group['index']<len(msg['instructions']):raise ValueError('token_instruction_path_invalid')
            instructions.extend((group['index'],i,ix) for i,ix in enumerate(group['instructions']))
        if len(instructions)>640:raise ValueError('token_instruction_limit')
        for outer,inner,ix in instructions:
            program=ix.get('programId')
            if program not in (TOKEN,TOKEN22):continue
            parsed=ix.get('parsed') or {};kind=parsed.get('type')
            if kind not in ('transfer','transferChecked','transferCheckedWithFee'):continue
            info=parsed['info'];src=_address(info['source']);dst=_address(info['destination'])
            source,destination=accounts.get(src,{}),accounts.get(dst,{})
            if kind=='transfer':
                amount=_amount(info['amount']);identity=source if source.get('mint') else destination
                mint,decimals=identity.get('mint'),identity.get('decimals')
            else:
                amount=_amount(info['tokenAmount']['amount']);mint=_address(info['mint']);decimals=_decimals(info['tokenAmount']['decimals'])
            reason=None
            for account in (source,destination):
                if account.get('reason')=='token_identity_changed' or any(account.get(k) is not None and account[k]!=v for k,v in [('mint',mint),('decimals',decimals),('program',program)]):
                    reason='token_identity_mismatch'
            if not reason and (mint is None or decimals is None):reason='token_mint_unknown'
            if not reason and (not source.get('owner') or not destination.get('owner')):reason='token_owner_unknown'
            fee='0' if program==TOKEN else None
            if kind=='transferCheckedWithFee':
                if program!=TOKEN22:raise ValueError('token_fee_program_invalid')
                fee=_amount(info['feeAmount']['amount'])
                if int(fee)>int(amount) or _decimals(info['feeAmount']['decimals'])!=decimals:raise ValueError('token_fee_invalid')
            if not reason and fee is None:reason='token2022_fee_unknown'
            out['transfers'].append(dict(outer_ix=outer,inner_ix=inner,source_account=src,destination_account=dst,
                source_owner=source.get('owner'),destination_owner=destination.get('owner'),mint=mint,program=program,
                amount_atomic=amount,decimals=decimals,fee_atomic=fee,
                credit_atomic=str(int(amount)-int(fee)) if fee is not None and reason not in ('token_identity_mismatch','token_mint_unknown') else None,
                authority=_address(info['authority']),state='unknown' if reason else 'known',reason=reason,
                observed_owners=sorted(set(source.get('observed_owners',[])+destination.get('observed_owners',[]))),
                instruction_json=json.dumps(ix,sort_keys=True,allow_nan=False)))
        if len(out['accounts'])+len(out['transfers'])>MAX_ROWS:raise ValueError('token_evidence_row_limit')
        if not out['accounts'] and not out['transfers']:out.update(state='unknown',reason='no_token_observations')
        elif any(v['state']!='known' for v in out['accounts']+out['transfers']):out.update(state='unknown',reason='token_evidence_partial')
    except (KeyError,TypeError,ValueError,IndexError) as exc:
        out.update(accounts=[],transfers=[],state='unknown',reason=str(exc))
    return out


def scoped_rows(evidence,owners):
    """Only actual observed actors' accounts and instructions touching them."""
    owners=set(owners)
    return ([a for a in evidence['accounts'] if owners.intersection(a['observed_owners'])],
            [t for t in evidence['transfers'] if owners.intersection(t['observed_owners'])])


def persist(conn,*,signature,accounts,transfers,proof_refs,source,flow_id,known_at):
    """Caller owns transaction/caps. References are already committed observed-actor receipts."""
    if (not isinstance(source,str) or not source or type(flow_id) is not int or flow_id<=0
            or type(known_at) not in (int,float) or not math.isfinite(known_at) or known_at<=0):
        raise ValueError('token_causal_context_required')
    for table,items,columns,owner_keys in [
        ('watch_token_accounts',accounts,'account,mint,owner,program,decimals,pre_atomic,post_atomic,delta_atomic,state,reason',('owner',)),
        ('watch_token_transfers',transfers,'outer_ix,inner_ix,source_account,destination_account,source_owner,destination_owner,mint,program,amount_atomic,decimals,fee_atomic,credit_atomic,authority,state,reason,instruction_json',('source_owner','destination_owner'))]:
        columns=columns.split(',')
        for item in items:
            ref=next((proof_refs[owner] for owner in [item[k] for k in owner_keys]+item['observed_owners'] if owner in proof_refs),None)
            if not isinstance(ref,str) or not ref:raise ValueError('token_observed_proof_required')
            conn.execute(f"INSERT OR IGNORE INTO {table}(signature,{','.join(columns)},fetch_id,source,flow_id,known_at) VALUES({','.join('?' for _ in range(len(columns)+5))})",
                         (signature,*(item[k] for k in columns),ref,source,flow_id,known_at))
