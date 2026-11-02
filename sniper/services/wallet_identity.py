"""Descriptive account/address links. No network, funded authority, or signing."""
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
import json
import math
from pathlib import Path
import re
import statistics
from decimal import Decimal

from sniper.services.memecoin_chains import canonical_chain_id

METHOD = 'wallet-identity-v1:pre60:c3:d2:funding-clue'
MAX_BYTES = 4 * 1024 * 1024
STALE_S = 2 * 3600
RELATION_LIMIT = 20
EVIDENCE_LIMIT = 10


def _at(value):
    dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if dt.tzinfo is None:
        raise ValueError('aware timestamp required')
    return dt


def _address(chain, value):
    if not isinstance(value,str): return None
    if chain=='solana':
        return value if re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}',value) else None
    return value.lower() if re.fullmatch(r'0x[0-9a-fA-F]{40}',value) else None


def _claims(raw):
    if isinstance(raw,dict):
        raw=[dict(chain=c,address=a,uid=v[0]) for (c,a),v in raw.items()]
    groups=defaultdict(dict)
    for r in raw:
        chain=canonical_chain_id(r.get('chain')); address=_address(chain,r.get('address'))
        uid=r.get('uid')
        if chain and address and isinstance(uid,str) and uid:
            old=groups[(chain,address)].get(uid,{})
            groups[(chain,address)][uid]=dict(r,chain=chain,address=address,conflict=bool(r.get('conflict') or old.get('conflict')))
    return groups


def _account(groups,uid,chain):
    verified=[]; conflicts=[]
    for (net,address),owners in sorted(groups.items()):
        if net!=chain or uid not in owners: continue
        if len(owners)>1 or any(r.get('conflict') for r in owners.values()):
            conflicts.append(dict(chain=net,address=address,owner_uids=sorted(owners)))
        else:
            r=owners[uid]
            verified.append(dict(chain=net,address=address,status='verified_account_address',
                                 evidence_kind=r.get('evidence_kind','current_verified_registry')))
    return verified,conflicts


def build_identity_report(inputs, *, now_iso, own_addresses=()):
    now=_at(now_iso); start=now-timedelta(days=14)
    groups=_claims(inputs.get('claims',[])); users={}
    def account(uid,chain):
        if chain not in users.setdefault(uid,dict(chains={}))['chains']:
            verified,conflicts=_account(groups,uid,chain)
            coverage=dict(inputs.get('coverage',{}),pre_window_s=60) if chain=='solana' else dict(state='not_measured')
            if chain=='solana':coverage.update(state='partial' if coverage.get('state')=='partial' else 'unavailable',reason='no_source_windows')
            users[uid]['chains'][chain]=dict(verified_addresses=verified,address_conflicts=conflicts,
                related_wallets=None if chain=='solana' else [],omitted=0,coverage=coverage)
        return users[uid]['chains'][chain]
    for (chain,_addr),owners in groups.items():
        for uid in owners: account(uid,chain)
    first={}
    for r in inputs.get('windows',[]):
        uid,mint=r.get('source_user_id'),r.get('mint')
        if not uid or not mint: continue
        t=r.get('t0')
        if type(t) not in (int,float) or not math.isfinite(t):
            t=None
        elif not start.timestamp()<=t<=now.timestamp(): continue
        # An unknown clock cannot prove this was later than a decoded first buy.
        key=(uid,mint); order=(t is not None,t or 0,str(r.get('signature','')))
        if key not in first or order<first[key][0]: first[key]=(order,dict(r,t0=t))
    by_uid=defaultdict(list); eligible={}
    for (uid,_),(_,r) in first.items():
        by_uid[uid].append(r)
        if r.get('status') in ('ok','no_label','our_fill') and r.get('control_named')==1 and r['t0'] is not None:
            eligible[r['signature']]=r
    hits=defaultdict(dict)
    uncertain_order={r['signature'] for r in eligible.values() if (r.get('n_order_unknown') or 0)>0}
    for b in inputs.get('early_buys',[]):
        r=eligible.get(b.get('signature')); address=_address('solana',b.get('trader')); lead=b.get('lead_s')
        if not r or not address or address in own_addresses or type(lead) not in (int,float) or not math.isfinite(lead) or not 0<=lead<=60: continue
        if lead==0 and (b.get('order_proven')!=1 or type(b.get('slot')) is not int):
            uncertain_order.add(r['signature']);continue
        hits[(r['source_user_id'],address)].setdefault(r['mint'],dict(mint=r['mint'],t0=r['t0'],
            source_signature=r['signature'],buy_signature=b.get('buy_signature'),lead_s=lead,
            slot=b.get('slot'),tx_index=b.get('tx_index'),order_proven=b.get('order_proven')))
    funders={r['wallet']:r for r in inputs.get('funders',[]) if r.get('status')=='resolved' and r.get('funder')}
    degree=Counter(r['funder'] for r in funders.values())
    funding_available=inputs.get('coverage',{}).get('funding_state')!='unavailable' and 'funders' not in inputs.get('coverage',{}).get('capped',[])
    for uid,windows in by_uid.items():
        row=account(uid,'solana'); complete=sum(r.get('signature') in eligible for r in windows)
        row['related_wallets']=[] if complete else None
        row['coverage'].update(state=inputs.get('coverage',{}).get('state','ok') if complete else 'unavailable',
                               reason=None if complete else 'no_complete_windows')
        row['coverage'].update(eligible_coins=complete,unknown_coins=len(windows)-complete,
            source_coins=len(windows),funding_state=inputs.get('coverage',{}).get('funding_state','cached_heuristic'),
            capped=inputs.get('coverage',{}).get('capped',[]),
            ordering_unknown_coins=sum(r.get('signature') in uncertain_order for r in windows),
            statuses=dict(Counter(r.get('status','unknown') for r in windows)),
            origins=dict(Counter(r.get('origin','unknown') for r in windows)))
    for (uid,address),coins in hits.items():
        row=account(uid,'solana'); evidence=sorted(coins.values(),key=lambda c:(c['t0'],c['mint']))
        own={a['address'] for a in row['verified_addresses']}; claimed=groups.get(('solana',address),{})
        conflict=len(claimed)>1 or any(r.get('conflict') for r in claimed.values())
        clues=[]
        for main in sorted(own):
            candidate,source=funders.get(address),funders.get(main)
            direct=(candidate if candidate and candidate['funder']==main else
                    source if source and source['funder']==address else None)
            if direct:
                clues.append(dict(kind='initial_funding_clue',funder=direct['funder'],recipient=direct['wallet'],
                    funding_signature=direct.get('funding_sig'),funded_at=direct.get('funded_at'),
                    hub=degree[direct['funder']]>3,method='cached_first_credit_balance_attribution',ownership_proven=False))
            elif candidate and source and candidate['funder']==source['funder']:
                clues.append(dict(kind='shared_funder',funder=candidate['funder'],recipient=address,
                    funding_signature=candidate.get('funding_sig'),hub=degree[candidate['funder']]>3,
                    method='cached_first_credit_balance_attribution',ownership_proven=False))
        days=len({datetime.fromtimestamp(c['t0'],timezone.utc).date() for c in evidence})
        status=('ownership_conflict' if conflict else 'verified_account_address' if uid in claimed else
                'suspected_alternate' if not claimed and len(coins)>=3 and days>=2 and funding_available and
                any(c['kind']=='initial_funding_clue' and c['funding_signature'] and not c['hub'] for c in clues) else 'related_trader')
        leads=sorted(c['lead_s'] for c in evidence)
        row['related_wallets'].append(dict(chain='solana',address=address,status=status,ownership_proven=False,
            known_owner_uids=sorted(claimed),n_coins=len(coins),eligible_coins=row['coverage']['eligible_coins'],
            missed_coins=None if 'early_buys' in row['coverage']['capped'] or row['coverage']['ordering_unknown_coins'] else row['coverage']['eligible_coins']-len(coins),
            unknown_coins=row['coverage']['unknown_coins'],
            days=days,lead_p50_s=statistics.median(leads),lead_p90_s=leads[min(len(leads)-1,int((len(leads)-1)*.9+.5))],
            first_seen_at=datetime.fromtimestamp(evidence[0]['t0'],timezone.utc).isoformat(),
            last_seen_at=datetime.fromtimestamp(evidence[-1]['t0'],timezone.utc).isoformat(),
            funding_clues=clues,evidence=evidence[:EVIDENCE_LIMIT],evidence_omitted=max(0,len(evidence)-EVIDENCE_LIMIT)))
    for user in users.values():
        for row in user['chains'].values():
            if row['related_wallets'] is not None:
                rows=sorted(row['related_wallets'],key=lambda r:(r['status']!='suspected_alternate',-r['n_coins'],r['address']))
                row.update(related_wallets=None if not rows and 'early_buys' in row['coverage'].get('capped',[]) else rows[:RELATION_LIMIT],
                           omitted=max(0,len(rows)-RELATION_LIMIT))
    return dict(version=1,method=METHOD,generated_at=now_iso,coverage=inputs.get('coverage',{}),users=users)


def publish_identity(path, report):
    encoded=json.dumps(report,allow_nan=False).encode()
    if len(encoded)>MAX_BYTES: raise ValueError('identity artifact exceeds 4 MiB')
    from scripts.research.wallet_follow_funnel import publish_universe
    publish_universe(path,report)


def _verified_funding(path, now_iso):
    """Saved confirmed native-SOL receipts, independent of the rolling activity artifact."""
    with Path(path).open('rb') as f: data=f.read(MAX_BYTES+1)
    if len(data)>MAX_BYTES: raise ValueError('oversize_funding_receipts')
    raw=json.loads(data); now=_at(now_iso).timestamp()
    if (raw.get('version')!=1 or raw.get('chain')!='solana' or raw.get('commitment') not in ('confirmed','finalized')
            or _at(raw['observed_at']).timestamp()>now or not isinstance(raw.get('transactions'),list)
            or len(raw['transactions'])>100): raise ValueError('invalid_funding_receipts')
    return [clue for tx in raw['transactions'] for clue in verified_sol_funding(tx,now_iso)]


def verified_sol_funding(tx, now_iso):
    """Pure saved confirmed receipt validation for the off-path research collector."""
    now=_at(now_iso).timestamp()
    clues=[]
    if type(tx['slot']) is not int or tx['slot']<=0 or type(tx['blockTime']) is not int or not 0<tx['blockTime']<=now:
        raise ValueError('invalid_funding_clock')
    meta=tx['meta']; msg=tx['transaction']['message']; signatures=tx['transaction']['signatures']
    if meta['err'] is not None or not isinstance(signatures,list) or not signatures or not all(
            isinstance(sig,str) and re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{64,88}',sig) for sig in signatures):
        raise ValueError('failed_or_unsigned_funding')
    keys=[k['pubkey'] for k in msg['accountKeys']]
    signers={k['pubkey'] for k in msg['accountKeys'] if k.get('signer') is True}
    if len(signatures)!=len(signers): raise ValueError('invalid_funding_signers')
    amounts=Counter()
    for ix in msg['instructions']:
        if ix.get('program')!='system' or ix.get('parsed',{}).get('type')!='transfer': continue
        info=ix['parsed']['info']; src,dst=info['source'],info['destination']; amount=info['lamports']
        if (ix.get('programId')!='11111111111111111111111111111111' or src not in signers or
                not _address('solana',src) or not _address('solana',dst) or type(amount) is not int or not 0<amount<2**64):
            raise ValueError('invalid_funding_transfer')
        if src!=dst: amounts[(src,dst)]+=amount
    for (src,dst),amount in amounts.items():
        index=keys.index(dst); before,after=meta['preBalances'][index],meta['postBalances'][index]
        if type(before) is not int or type(after) is not int or after-before<amount:
            raise ValueError('funding_credit_unproven')
        clues.append(dict(kind='verified_funding',verified=True,funder=src,recipient=dst,
            funding_signature=signatures[0],funded_at=tx['blockTime'],amount_atomic=str(amount),
            amount_sol=format(Decimal(amount).scaleb(-9),'f').rstrip('0').rstrip('.') if amount%10**9 else str(amount//10**9),
            method='confirmed_system_transfer',ownership_proven=False))
    # Only the observed closed, single-path WSOL receipt is supported; custom/CPI flows stay unverified.
    instructions=msg['instructions']
    if len(instructions)==3 and not meta.get('innerInstructions'):
        create,initialize,close=instructions
        c=create.get('parsed',{});a=initialize.get('parsed',{});z=close.get('parsed',{})
        if create.get('program')=='system' and c.get('type')=='createAccountWithSeed':
            from solders.pubkey import Pubkey
            token='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA';wsol='So11111111111111111111111111111111111111112'
            info=c['info'];initial=a.get('info',{});closed=z.get('info',{})
            src,base,temp,dst=info.get('source'),info.get('base'),info.get('newAccount'),closed.get('destination')
            amount,seed,fee=info.get('lamports'),info.get('seed'),meta.get('fee')
            if (create.get('programId')!='11111111111111111111111111111111' or info.get('owner')!=token or info.get('space')!=165
                    or src not in signers or base not in signers or not keys or keys[0] not in signers
                    or any(not _address('solana',v) for v in (src,base,temp,dst)) or len(set(keys))!=len(keys)
                    or src==dst or temp in (src,base,dst) or type(amount) is not int or not 0<amount<2**64
                    or type(fee) is not int or not 0<=fee<2**64 or not isinstance(seed,str) or len(seed.encode())>32
                    or initialize.get('program')!='spl-token' or initialize.get('programId')!=token or a.get('type')!='initializeAccount'
                    or initial.get('account')!=temp or initial.get('owner')!=base or initial.get('mint')!=wsol
                    or close.get('program')!='spl-token' or close.get('programId')!=token or z.get('type')!='closeAccount'
                    or closed.get('account')!=temp or closed.get('owner')!=base):
                raise ValueError('invalid_seeded_wsol_funding')
            if str(Pubkey.create_with_seed(Pubkey.from_string(base),seed,Pubkey.from_string(token)))!=temp:
                raise ValueError('seeded_funding_address_unproven')
            before,after=meta['preBalances'],meta['postBalances']
            if len(before)!=len(keys) or len(after)!=len(keys) or any(type(v) is not int or not 0<=v<2**64 for v in before+after):
                raise ValueError('seeded_funding_balances_unknown')
            deltas=Counter({src:-amount,dst:amount});deltas[keys[0]]-=fee
            if any(k not in keys for k in (src,base,temp,dst)) or before[keys.index(temp)]!=0 or after[keys.index(temp)]!=0 or any(
                    after[n]-before[n]!=deltas[k] for n,k in enumerate(keys)):
                raise ValueError('seeded_funding_flow_unproven')
            clues.append(dict(kind='verified_funding',verified=True,funder=src,recipient=dst,
                funding_signature=signatures[0],funded_at=tx['blockTime'],amount_atomic=str(amount),
                amount_sol=format(Decimal(amount).scaleb(-9),'f').rstrip('0').rstrip('.') if amount%10**9 else str(amount//10**9),
                method='confirmed_seeded_wsol_close',funding_account=temp,close_authority=base,ownership_proven=False))
    return clues

def _attach_funding(result, path, groups, now_iso):
    try:
        clues=_verified_funding(path,now_iso); result['funding_state']='ok'
    except (OSError,ValueError,TypeError,KeyError,AttributeError,IndexError):
        result['funding_state']='unavailable'; return
    for uid,row in result['wallets'].items():
        own={a['address'] for a in row['verified_addresses']}
        related=row['related_wallets']
        for clue in clues:
            peer=(clue['funder'] if clue['recipient'] in own else clue['recipient'] if clue['funder'] in own else None)
            if not peer or peer in own: continue
            if related is None: related=[]
            r=next((r for r in related if r['address']==peer),None)
            if r is None:
                r=dict(chain='solana',address=peer,status='related_trader',ownership_proven=False,
                    n_coins=0,eligible_coins=row['coverage'].get('eligible_coins',0),missed_coins=None,
                    unknown_coins=row['coverage'].get('unknown_coins',0),days=0,lead_p50_s=None,lead_p90_s=None,
                    funding_clues=[],evidence=[],evidence_omitted=0)
                related.append(r)
            claimed=groups.get(('solana',peer),{})
            conflict=len(claimed)>1 or any(v.get('conflict') for v in claimed.values())
            r.update(funding_verified=True,ownership_proven=False,known_owner_uids=sorted(claimed),
                status='ownership_conflict' if conflict else 'verified_account_address' if uid in claimed else 'funding_verified_link')
            key=(clue['funding_signature'],clue['funder'],clue['recipient'])
            r['funding_clues']=[c for c in r['funding_clues'] if
                (c.get('funding_signature'),c['funder'],c['recipient'])!=key]+[dict(clue)]
        if related is not None:
            related.sort(key=lambda r:(not r.get('funding_verified',False),r['status']!='suspected_alternate',-r['n_coins'],r['address']))
            row['omitted']+=max(0,len(related)-RELATION_LIMIT)
            row['related_wallets']=related[:RELATION_LIMIT]


def read_identity(path, owners, *, chain, now_iso):
    chain=canonical_chain_id(chain)
    if not chain: raise ValueError('unknown chain')
    groups=_claims(owners); state='unavailable'; report={}; reason='artifact_absent'
    try:
        with Path(path).open('rb') as f: data=f.read(MAX_BYTES+1)
        if len(data)>MAX_BYTES: raise ValueError('oversize')
        report=json.loads(data)
        if report.get('version')!=1 or report.get('method')!=METHOD or not isinstance(report.get('users'),dict): raise ValueError('invalid_identity_artifact')
        if not isinstance(report.get('coverage'),dict): raise ValueError('invalid_coverage')
        for uid,user in report['users'].items():
            if not uid or not isinstance(user,dict) or not isinstance(user.get('chains'),dict): raise ValueError('invalid_user')
            for net,row in user['chains'].items():
                if not canonical_chain_id(net) or not isinstance(row,dict) or not isinstance(row.get('coverage'),dict): raise ValueError('invalid_chain')
                if type(row.get('omitted',0)) is not int or row.get('omitted',0)<0:raise ValueError('invalid_omitted_count')
                if not isinstance(row['coverage'].get('capped',[]),list) or not all(isinstance(c,str) for c in row['coverage'].get('capped',[])):raise ValueError('invalid_cap')
                related=row.get('related_wallets')
                if related is None and row['coverage'].get('state')!='ok':continue
                if not isinstance(related,list) or len(related)>RELATION_LIMIT: raise ValueError('invalid_relations')
                for r in related:
                    if not isinstance(r,dict) or not _address(net,r.get('address')) or r.get('chain')!=net: raise ValueError('invalid_address')
                    if not isinstance(r.get('evidence'),list) or len(r['evidence'])>EVIDENCE_LIMIT or not all(isinstance(e,dict) for e in r['evidence']): raise ValueError('invalid_evidence')
                    for e in r['evidence']:
                        if not isinstance(e.get('mint'),str) or any(type(e.get(k)) not in (int,float) or not math.isfinite(e[k]) for k in ('t0','lead_s')): raise ValueError('invalid_evidence_clock')
                        if not 0<=e['lead_s']<=60 or not 0<=e['t0']<=_at(report['generated_at']).timestamp(): raise ValueError('invalid_evidence_clock')
                    if not isinstance(r.get('funding_clues'),list) or not all(isinstance(c,dict) for c in r['funding_clues']): raise ValueError('invalid_clues')
                    for c in r['funding_clues']:
                        if not _address(net,c.get('funder')) or not _address(net,c.get('recipient')) or type(c.get('hub')) is not bool:raise ValueError('invalid_funding_address')
                    if any(type(r.get(k)) is not int or r[k]<0 for k in ('n_coins','eligible_coins','unknown_coins','days')): raise ValueError('invalid_counts')
                    if r['n_coins']>r['eligible_coins'] or r['days']>r['n_coins']: raise ValueError('invalid_counts')
                    if any(type(r.get(k)) not in (int,float) or not math.isfinite(r[k]) or not 0<=r[k]<=60 for k in ('lead_p50_s','lead_p90_s')): raise ValueError('invalid_leads')
        age=(_at(now_iso)-_at(report['generated_at'])).total_seconds()
        if age<0: raise ValueError('future_artifact')
        state='stale' if age>STALE_S else report.get('coverage',{}).get('state','ok')
        if state not in ('ok','partial','stale','unavailable'): raise ValueError('invalid_identity_state')
        reason=report.get('coverage',{}).get('reason')
    except (OSError,ValueError,TypeError,KeyError,AttributeError) as exc:
        state='unavailable';report={}; reason='artifact_absent' if isinstance(exc,FileNotFoundError) else 'artifact_unreadable'
    uids={u for owners in groups.values() for u in owners}|set(report.get('users',{})); wallets={}
    for uid in sorted(uids):
        verified,conflicts=_account(groups,uid,chain)
        saved=report.get('users',{}).get(uid,{}).get('chains',{}).get(chain,{})
        relations=[] if report and state!='unavailable' and isinstance(saved.get('related_wallets'),list) else None
        own={a['address'] for a in verified}
        for original in (saved.get('related_wallets',[]) if relations is not None else []):
            address=_address(chain,original.get('address'))
            if original.get('chain')!=chain or not address: continue
            r=dict(original,address=address); claimed=groups.get((chain,address),{})
            conflict=len(claimed)>1 or any(v.get('conflict') for v in claimed.values())
            r['known_owner_uids']=sorted(claimed)
            r['funding_clues']=[c for c in r.get('funding_clues',[]) if
                (c.get('kind')=='initial_funding_clue' and (c.get('funder') in own or c.get('recipient') in own)) or
                (c.get('kind')=='shared_funder' and c.get('recipient')==address and bool(own))]
            r['status']=('ownership_conflict' if conflict else 'verified_account_address' if uid in claimed else
                'suspected_alternate' if not claimed and r.get('status')=='suspected_alternate' and
                r['n_coins']>=3 and r['days']>=2 and saved.get('coverage',{}).get('funding_state')!='unavailable' and
                any(c.get('kind')=='initial_funding_clue' and c.get('funding_signature') and c.get('hub') is False for c in r['funding_clues']) else 'related_trader')
            r['ownership_proven']=False
            for forbidden in ('armed','effective','funded','capital','real_approved','funding_verified'): r.pop(forbidden,None)
            relations.append(r)
        local_state=saved.get('coverage',{}).get('state','unavailable')
        wallets[uid]=dict(state=(state if relations is not None else local_state) if chain=='solana' else 'not_measured',generated_at=report.get('generated_at'),
            verified_addresses=verified,address_conflicts=conflicts,related_wallets=relations,
            omitted=saved.get('omitted',0),coverage=saved.get('coverage',dict(state='unavailable',pre_window_s=60)))
    result=dict(state=state,reason=reason,generated_at=report.get('generated_at'),wallets=wallets)
    if chain=='solana': _attach_funding(result,Path(path).with_name('wallet_funding_receipts.json'),groups,now_iso)
    from sniper.services.wallet_identity_links import attach_links
    attach_links(result,Path(path).with_name('wallet_identity_links.json'),groups,chain=chain,now_iso=now_iso)
    return result
