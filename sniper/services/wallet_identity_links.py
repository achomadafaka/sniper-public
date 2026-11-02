"""Saved cross-chain research evidence. No network or funded state writes."""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import re
import statistics

from sniper.services.memecoin_chains import canonical_chain_id
from sniper.services.memecoin_links import _EXPLORERS
from sniper.services.wallet_identity import _address, _at, _account, RELATION_LIMIT, EVIDENCE_LIMIT, STALE_S

METHOD='armed-wallet-links-v1'
MAX_BYTES=4*1024*1024
BUY_METHODS={'direct-wallet-paid-v1','solana_venue_signer','solana_route_payment_signer'}
FUND_METHODS={'confirmed_seeded_wsol_close','confirmed_system_transfer','confirmed_native_transfer','confirmed_erc20_transfer'}
STAT_FIELDS=('n_coins','before_coins','after_coins','days','lead_p50_s','lead_p90_s')


def _count(value):
    if type(value) is not int or value<0:raise ValueError('invalid_count')
    return value


def _order(value):
    if not isinstance(value,list) or len(value)!=2:raise ValueError('invalid_order')
    return [_count(v) for v in value]


def _signature(chain,value):
    pattern=r'[1-9A-HJ-NP-Za-km-z]{64,88}' if chain=='solana' else r'0x[0-9a-fA-F]{64}'
    if not isinstance(value,str) or not re.fullmatch(pattern,value):raise ValueError('invalid_signature')


def _valid_address(chain,value):
    if not _address(chain,value):raise ValueError('invalid_address')


def _validate(raw,now):
    if raw.get('version')!=1 or raw.get('method')!=METHOD:raise ValueError('invalid_method')
    generated=_at(raw['generated_at']).timestamp()
    if generated>now:raise ValueError('future_artifact')
    for field,limit in (('pairs',1000),('buys',12000),('funding',3000)):
        if not isinstance(raw.get(field),list) or len(raw[field])>limit:raise ValueError('invalid_'+field)
    pairs={}
    for pair in raw['pairs']:
        chain=pair['chain'];uid=pair['uid'];key=(chain,uid)
        if canonical_chain_id(chain)!=chain or not isinstance(uid,str) or not uid or key in pairs:
            raise ValueError('invalid_pair')
        addresses=pair['main_addresses']
        if not isinstance(addresses,list) or len(addresses)>20:raise ValueError('invalid_main_addresses')
        for a in addresses:_valid_address(chain,a)
        coverage=pair['coverage']
        if not isinstance(coverage,dict) or coverage.get('state') not in ('ok','partial','unavailable','address_unproven'):
            raise ValueError('invalid_coverage')
        for k in ('source_units','sampled_coins','complete_coins','unknown_coins'):_count(coverage.get(k,0))
        if coverage.get('complete_coins',0)>coverage.get('sampled_coins',0):raise ValueError('invalid_denominator')
        if not isinstance(coverage.get('capped',[]),list) or not all(isinstance(k,str) for k in coverage.get('capped',[])):
            raise ValueError('invalid_cap')
        for k in ('relation_omitted','relations_omitted','evidence_omitted'):_count(coverage.get(k,0))
        stats=pair.get('statistics',[])
        if not isinstance(stats,list) or len(stats)>RELATION_LIMIT:raise ValueError('invalid_statistics')
        seen=set()
        for s in stats:
            _valid_address(chain,s['address'])
            if s['address'] in seen:raise ValueError('duplicate_statistics')
            seen.add(s['address'])
            n,b,a,d=(_count(s[k]) for k in ('n_coins','before_coins','after_coins','days'))
            if not max(b,a)<=n<=b+a or d>n or n>coverage.get('sampled_coins',0):raise ValueError('invalid_statistics')
            for k in ('lead_p50_s','lead_p90_s'):
                v=s.get(k)
                if v is not None and (type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=60):
                    raise ValueError('invalid_lead')
        pairs[key]=pair
    identities={}
    for buy in raw['buys']:
        chain=buy['chain'];pair=pairs.get((chain,buy['uid']))
        if not pair or buy['main_address'] not in pair['main_addresses']:raise ValueError('unbound_buy')
        for k in ('main_address','address','mint'):_valid_address(chain,buy[k])
        for k in ('source_signature','buy_signature'):_signature(chain,buy[k])
        for k in ('source_order','order'):_order(buy[k])
        t0,source_t0=(_count(buy[k]) for k in ('t0','source_t0'))
        if not 0<t0<=generated or not 0<source_t0<=generated or abs(t0-source_t0)>60:
            raise ValueError('invalid_clock')
        if buy.get('proof_method') not in BUY_METHODS:raise ValueError('buyer_unproven')
        for sig,order,at in ((buy['source_signature'],buy['source_order'],source_t0),(buy['buy_signature'],buy['order'],t0)):
            key=(chain,sig);identity=(tuple(order),at)
            if key in identities and identities[key]!=identity:raise ValueError('transaction_identity_changed')
            identities[key]=identity
        if (tuple(buy['order'])<tuple(buy['source_order']) and t0>source_t0 or
                tuple(buy['order'])>tuple(buy['source_order']) and t0<source_t0):raise ValueError('order_clock_mismatch')
    for f in raw['funding']:
        chain=f['chain']
        if canonical_chain_id(chain)!=chain:raise ValueError('invalid_funding_chain')
        for k in ('funder','recipient'):_valid_address(chain,f[k])
        _signature(chain,f['funding_signature'])
        atomic=f['amount_atomic'];decimals=_count(f['decimals'])
        if (f.get('kind')!='verified_funding' or f.get('verified') is not True or
            f.get('ownership_proven') is not False or f.get('method') not in FUND_METHODS or
            not isinstance(atomic,str) or len(atomic)>78 or not re.fullmatch(r'[1-9][0-9]*',atomic) or decimals>36 or
            not 0<_count(f['funded_at'])<=generated or f['funder']==f['recipient']):raise ValueError('invalid_funding')
        if Decimal(f['amount'])!=Decimal((0,tuple(int(c) for c in atomic),-decimals)):raise ValueError('funding_scale_mismatch')
        if f['asset']!='native':_valid_address(chain,f['asset'])
        if not isinstance(f['symbol'],str) or not 0<len(f['symbol'])<=30:raise ValueError('invalid_asset')
        if type(f.get('hub',False)) is not bool:raise ValueError('invalid_hub')
    return pairs


def attach_links(result,path,groups,*,chain,now_iso):
    """Rebind persisted research to current account claims on every web read."""
    try:
        with Path(path).open('rb') as f:data=f.read(MAX_BYTES+1)
        if len(data)>MAX_BYTES:raise ValueError('oversize')
        raw=json.loads(data);pairs=_validate(raw,_at(now_iso).timestamp())
    except FileNotFoundError:return
    except (OSError,ValueError,TypeError,KeyError,AttributeError,ArithmeticError):
        result.update(links_state='unavailable',links_reason='artifact_unreadable');return
    stale=(_at(now_iso)-_at(raw['generated_at'])).total_seconds()>STALE_S
    result.update(links_state='stale' if stale else 'ok',links_generated_at=raw['generated_at'])
    buys=defaultdict(list)
    for b in raw['buys']:
        if b['chain']==chain:buys[b['uid']].append(b)
    funding=[f for f in raw['funding'] if f['chain']==chain]
    funder_degree=defaultdict(set)
    for f in funding:funder_degree[f['funder']].add(f['recipient'])
    for (net,uid),pair in pairs.items():
        if net!=chain:continue
        verified,conflicts=_account(groups,uid,chain);own={a['address'] for a in verified}
        row=result['wallets'].setdefault(uid,dict(verified_addresses=verified,address_conflicts=conflicts))
        coverage=dict(pair['coverage'],window_s=60,known_roster_peers_included=True)
        binding=own==set(pair['main_addresses']) and bool(own)
        if not binding:coverage.update(state='unavailable',reason='account_mapping_changed' if own else 'address_unproven')
        relations={r['address']:dict(r) for r in row.get('related_wallets') or []} if binding else {}
        if not binding:
            for old in row.get('related_wallets') or []:
                clues=[c for c in old.get('funding_clues',[]) if c.get('verified') is True and
                       (c.get('funder') in own or c.get('recipient') in own)]
                if clues:relations[old['address']]=dict(old,n_coins=0,before_coins=0,after_coins=0,days=0,
                    lead_p50_s=None,lead_p90_s=None,evidence=[],evidence_omitted=0,funding_clues=clues)
        observed=defaultdict(list)
        funded_peers={f['recipient'] if f['funder'] in own else f['funder'] for f in funding
                      if own.intersection((f['funder'],f['recipient']))}
        for b in buys[uid] if binding else []:
            if b['address'] not in own and b['order']!=b['source_order']:observed[b['address']].append(b)
        stats={s['address']:s for s in pair.get('statistics',[])} if binding else {}
        for address in set(observed)|set(stats):
            evidence=observed[address];before={b['mint'] for b in evidence if b['order']<b['source_order']}
            after={b['mint'] for b in evidence if b['order']>b['source_order']}
            coins=before|after
            leads=[b['source_t0']-b['t0'] for b in evidence if b['order']<b['source_order']]
            full=stats.get(address,dict(n_coins=len(coins),before_coins=len(before),after_coins=len(after),
                days=len({datetime.fromtimestamp(b['source_t0'],timezone.utc).date() for b in evidence}),
                lead_p50_s=statistics.median(leads) if leads else None,
                lead_p90_s=sorted(leads)[max(0,math.ceil(len(leads)*.9)-1)] if leads else None))
            full={k:full.get(k) for k in STAT_FIELDS}
            if full['n_coins']<2 and address not in funded_peers:continue
            r=relations.setdefault(address,dict(address=address,chain=chain,funding_clues=[]))
            r.update(full,eligible_coins=coverage.get('sampled_coins',0),unknown_coins=coverage.get('unknown_coins',0),
                missed_coins=None,ownership_proven=False,status='related_trader',
                evidence=[dict(b,direction='before' if b['order']<b['source_order'] else 'after',
                    lead_s=b['source_t0']-b['t0'],slot=b['order'][0],tx_index=b['order'][1]) for b in evidence[:EVIDENCE_LIMIT]],
                evidence_omitted=max(0,full['before_coins']+full['after_coins']-len(evidence[:EVIDENCE_LIMIT])))
        for f in funding:
            address=f['recipient'] if f['funder'] in own else f['funder'] if f['recipient'] in own else None
            if not address or address in own:continue
            r=relations.setdefault(address,dict(address=address,chain=chain,n_coins=0,before_coins=0,after_coins=0,days=0,
                lead_p50_s=None,lead_p90_s=None,eligible_coins=coverage.get('sampled_coins',0),
                unknown_coins=coverage.get('unknown_coins',0),missed_coins=None,evidence=[],evidence_omitted=0,funding_clues=[]))
            key=(f['funding_signature'],f['funder'],f['recipient'],f['asset'])
            old=r.get('funding_clues',[])
            r.update(funding_verified=True,ownership_proven=False,status='funding_verified_link')
            r['funding_clues']=[c for c in old if (c.get('funding_signature'),c['funder'],c['recipient'],c.get('asset','native'))!=key]
            r['funding_clues'].append(dict(f,hub=f.get('hub',False) or len(funder_degree[f['funder']])>3))
        for address,r in relations.items():
            claimed=groups.get((chain,address),{})
            r['known_owner_uids']=sorted(claimed)
            if len(claimed)>1 or any(v.get('conflict') for v in claimed.values()):r['status']='ownership_conflict'
            r['ownership_proven']=False
            for k in ('armed','effective','funded','capital','real_approved'):r.pop(k,None)
        ordered=sorted(relations.values(),key=lambda r:(not r.get('funding_verified',False),-r.get('before_coins',r['n_coins']),-r['n_coins'],r['address']))
        coverage['relation_omitted']=max(0,len(ordered)-RELATION_LIMIT)+pair['coverage'].get('relations_omitted',pair['coverage'].get('relation_omitted',0))
        row.update(state='stale' if stale and binding else coverage['state'],generated_at=raw['generated_at'],coverage=coverage,
            related_wallets=ordered[:RELATION_LIMIT] if ordered else ([] if binding and coverage['state']=='ok' else None),
            omitted=coverage['relation_omitted'],links_method=METHOD,
            explorer_base=_EXPLORERS.get(chain,'').removesuffix('tx/'))
