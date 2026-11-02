"""Prior-dump actor evidence. Pure causal arithmetic: no I/O, clocks or executor imports.

Four consecutive slots = slot spread <=3. One episode is overlapping qualifying
windows of one venue/pool. A price must already be observable; unpriced sells
count as flow, never as price proof. Exclusions apply to actors and signatures.
"""
from __future__ import annotations

import copy
import hashlib
import math
import json
from collections import defaultdict

VERSION = 'dump-group-v2-first-pre'
DEFINITION = dict(version=VERSION,sellers=20,slot_spread=3,drop_pct=50,sell_reserve_fraction=.03,
                  buy_fraction_lt=.05,price_endpoint='first_pre_to_current_pre',lookback_s=86400,buyer_share=.25,buy_sol_share=.4,bot_bursts=5)
POLICY_HASH = hashlib.sha256(json.dumps(DEFINITION,sort_keys=True,separators=(',',':')).encode()).hexdigest()
DETECTOR_HASH = POLICY_HASH  # closed definition hash; source identity is the accompanying Git SHA
LOOKBACK_S = 86400.
AGENTS = frozenset({'BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s'})


def _positive(x):
    return type(x) in (float, int) and math.isfinite(x) and x > 0


def _key(row):
    return (row['slot'], row.get('tx_index', -1) or -1, row['ordinal'], row['signature'])


class BurstDetector:
    """Feed ONE globally arrival-ordered stream, across pools. No history scan per feed.

    Rows: venue,pool,mint,signature,ordinal,slot,known_at,is_buy,actor,
    quote_sol,price_sol,pre_real_quote_sol; optional price_before_sol,price_known_at,price_only.
    None price is unknown. known_at is max(received_at,processed_at) in UTC
    seconds. feed returns a complete episode update or None. close evicts a pool.
    """
    def __init__(self, *, excluded=()):
        self.excluded = frozenset(excluded) | AGENTS
        self.pools = {}
        self.last_at = -math.inf
        self.fed = 0

    def close(self, venue, pool):
        self.pools.pop((venue, pool), None)

    def feed(self, row):
        at = row['known_at']
        if not _positive(at) or at < self.last_at:
            raise ValueError('dump_group_arrival_order')
        self.last_at = at
        self.fed += 1
        if self.fed % 10000 == 0:
            self.pools = {k:v for k,v in self.pools.items() if v['at'] >= at-120}
        if row['actor'] in self.excluded or row['signature'] in self.excluded:
            return None
        key=(row['venue'],row['pool'])
        state=self.pools.setdefault(key,dict(rows={},slot=row['slot'],at=at,episode=None))
        state['at']=at
        state['slot']=max(state['slot'],row['slot'])
        if row['slot'] < state['slot']-3:
            return None                        # late chain history cannot rewrite a prior decision
        state['rows']={k:r for k,r in state['rows'].items() if r['slot'] >= state['slot']-3}
        ident=(row['signature'],row['ordinal'])
        if ident in state['rows']:
            return None
        state['rows'][ident]=dict(row)
        rows=sorted(state['rows'].values(),key=_key)
        if len(rows)>50000:
            raise ValueError('dump_group_window_overflow')
        sellers={r['actor'] for r in rows if not r['is_buy'] and not r.get('price_only') and r['actor']}
        if len(sellers)<20:
            return None
        reserve=rows[0].get('pre_real_quote_sol')
        quotes=[r.get('quote_sol') for r in rows if not r.get('price_only')]
        if not _positive(reserve) or any(type(q) not in (int,float) or not math.isfinite(q) or q<0 for q in quotes):
            return None
        sold=sum(r['quote_sol'] for r in rows if not r['is_buy'] and not r.get('price_only'))
        bought=sum(r['quote_sol'] for r in rows if r['is_buy'] and not r.get('price_only'))
        if sold<reserve*.03 or sold+bought<=0 or bought*19>=sold:
            return None
        end=rows[-1]
        first_row=rows[0]
        before=first_row.get('price_before_sol')
        current=end.get('price_before_sol')
        def pre_known(r):
            return r.get('price_before_known_at',r.get('price_known_at',r['known_at']))<=at
        if not (_positive(before) and _positive(current) and pre_known(first_row) and pre_known(end)):
            return None
        # Frozen by Alpha: first PRE to current PRE, never an intra-window peak or current POST.
        if current>before*.5:
            return None
        sells=[r for r in rows if not r['is_buy'] and not r.get('price_only') and r['actor']]
        if len({r['actor'] for r in sells})<20:
            return None
        first=min(r['slot'] for r in rows)
        episode=state['episode']
        if episode is None or first>episode['last_slot']:
            eid=hashlib.sha256(f"{row['venue']}:{row['pool']}:{row['signature']}:{row['ordinal']}".encode()).hexdigest()
            episode=dict(id=eid,venue=row['venue'],pool=row['pool'],mint=row['mint'],detected_at=at,
                         proven_at=at,first_slot=first,last_slot=state['slot'],
                         first_sell_known_at=min(r['known_at'] for r in sells),participants={})
            state['episode']=episode
        episode['last_slot']=state['slot']
        episode['known_at']=at
        episode['drop_pct']=(1-current/before)*100
        for r in sells:
            episode['participants'].setdefault(r['actor'],dict(known_at=at,slot=r['slot']))
        episode['n_distinct_sellers']=len(episode['participants'])
        episode['same_slot_dump']=len({p['slot'] for p in episode['participants'].values()})==1
        return copy.deepcopy(episode)


def detect_bursts(rows, *, asof, excluded=()):
    """Batch convenience; output identical to folding feed updates by episode id."""
    detector=BurstDetector(excluded=excluded)
    out={}
    for row in sorted((r for r in rows if r['known_at']<=asof),key=lambda r:(r['known_at'],*_key(r))):
        event=detector.feed(row)
        if event is not None:
            out[event['id']]=event
    return list(out.values())


def actor_flags(events, *, asof, excluded=()):
    """Distinct episodes per actor in (asof-24h,asof]; no participant before proof."""
    seen=defaultdict(dict)
    excluded=frozenset(excluded)|AGENTS
    for e in events:
        if not asof-LOOKBACK_S<e['detected_at']<=asof:
            continue
        for actor,p in e['participants'].items():
            at=p['known_at'] if isinstance(p,dict) else p
            if actor not in excluded and at<=asof:
                seen[actor][e['id']]=at
    return {a:dict(burst_count=len(es),known_at=min(es.values())) for a,es in sorted(seen.items())}


def buyer_shares(rows, flags, *, asof, excluded=()):
    """Only the coin's trailing 60s rows are needed. Bots >=5 bursts are excluded
    from BOTH denominators; shares are fractions, quote is SOL, unknown != zero.
    """
    excluded=frozenset(excluded)|AGENTS
    flow={}
    bots=set()
    unknown_actors={}
    for r in rows:
        if not asof-60<r['known_at']<=asof or not r['is_buy'] or r.get('price_only'):
            continue
        actor=r['actor']
        if actor in excluded or r['signature'] in excluded:
            continue
        if not actor:
            unknown_actors[(r['signature'],r['ordinal'])]=r
            continue
        f=flags.get(actor,{})
        if f.get('known_at',math.inf)<=asof and f.get('burst_count',0)>=5:
            bots.add(actor)
            continue
        flow[(r['signature'],r['ordinal'])]=r
    buyers={r['actor'] for r in flow.values()}
    matched={a for a in buyers if flags.get(a,{}).get('known_at',math.inf)<=asof}
    quotes=[r.get('quote_sol') for r in (*flow.values(),*unknown_actors.values())]
    known=all(type(q) in (float,int) and math.isfinite(q) and q>=0 for q in quotes)
    total=sum(quotes) if known else None
    matched_sol=sum(r['quote_sol'] for r in flow.values() if r['actor'] in matched) if known else None
    denominator=len(buyers)+len(unknown_actors)  # conservative: every unknown may be a distinct buyer
    share=len(matched)/denominator if denominator else None
    sol_share=matched_sol/total if total else None
    status=('kill' if (share is not None and share>=.25) or (sol_share is not None and sol_share>=.4) else
            'unknown' if share is None or sol_share is None or unknown_actors else 'pass')
    return dict(status=status,reason='dump_group_buyers' if status=='kill' else ('no_buy_flow' if not buyers else
                'buy_quote_unknown' if sol_share is None else 'below_threshold'),buyer_share=share,
                buy_sol_share=sol_share,matched_buyers=len(matched),buyers=len(buyers),
                buy_sol=total,matched_buy_sol=matched_sol,frequent_bot_buyers=len(bots),unknown_actor_swaps=len(unknown_actors))


def checked_fact(fact, *, mint, pool, now):
    """Validate cached identity, definition, finite numbers and expiry. Malformed
    cache is typed unknown; its claimed kill never becomes admission authority.
    """
    unknown=lambda reason: dict(status='unknown',reason=reason,version=VERSION,policy_hash=POLICY_HASH)
    if not isinstance(fact,dict):
        return unknown('dump_group_unknown')
    if (fact.get('version')!=VERSION or fact.get('policy_hash')!=POLICY_HASH
            or fact.get('mint')!=mint or fact.get('pool')!=pool):
        return unknown('cache_identity')
    times=[fact.get(k) for k in ('known_at','cutoff','expires_at')]
    if any(not _positive(x) for x in times):
        return unknown('cache_timestamp')
    known,cut,expires=times
    if not cut<=known<=now<=expires or now-cut>5 or expires-known>5:
        return unknown('cache_stale')
    if fact.get('status') not in ('pass','kill','unknown'):
        return unknown('cache_shape')
    for key in ('buyer_share','buy_sol_share'):
        x=fact.get(key)
        if x is not None and (type(x) not in (int,float) or not math.isfinite(x) or not 0<=x<=1):
            return unknown('cache_shape')
    shares=[fact.get(k) for k in ('buyer_share','buy_sol_share')]
    hit=any(x is not None and x>=threshold for x,threshold in zip(shares,(.25,.4)))
    # Stale/disabled snapshots cannot assert a kill even if old diagnostic shares are high.
    if fact['status']=='kill' and (not hit or fact.get('reason')!='dump_group_buyers'
            or any(fact.get(k) is not True for k in ('positive_proven','target_complete','exclusions_valid'))):
        return unknown('cache_shape')
    if fact['status']=='pass' and (hit or any(x is None for x in shares)):
        return unknown('cache_shape')
    # Fixed-size copy: an untrusted extra cache field cannot add work at admission.
    keys=('status','reason','detail','version','policy_hash','mint','pool','venue','known_at','cutoff','expires_at',
          'buyer_share','buy_sol_share','matched_buyers','buyers','buy_sol','matched_buy_sol',
          'frequent_bot_buyers','unknown_actor_swaps','positive_proven','target_complete','exclusions_valid','would_block',
          'dump_group_coverage','ps_loss_fraction_24h','ps_window_holes','ps_window_blocking_holes','curve_venue')
    out={k:fact[k] for k in keys if k in fact and (fact[k] is None or type(fact[k]) in (bool,int,float,str))}
    coverage=fact.get('coverage')
    if isinstance(coverage,dict):
        out['coverage']={v:{k:c[k] for k in ('complete','history_start','holes') if k in c and
                          (c[k] is None or type(c[k]) in (bool,int,float,str))}
                         for v in ('curve','ps','pumpswap') if isinstance((c:=coverage.get(v)),dict)}
    return out


def pumpswap_pre_price(pre_base_atomic, pre_quote_atomic, *, virtual_quote_atomic,
                       state_known_at, virtual_quote_known_at):
    """SOL per whole 6-decimal base token at an OBSERVED canonical wSOL pool pre-state.

    Caller must bind pool/mint/wSOL and obtain virtual_quote_atomic ONLY from an
    authenticated prior legacy-buy proof (memecoin_desk_ps_tape._proof). This is
    arithmetic, not provenance discovery. Unproven virtual quote is None; explicit
    proven zero is valid. The observed pre-state belongs to THIS row's known_at,
    including processing time, never the previous sell's time. No network or I/O.
    """
    try:
        def u64(v):
            if type(v) not in (str,int) or (isinstance(v,str) and (not v.isascii() or not v.isdecimal())):
                raise ValueError
            n=int(v)
            if not 0<=n<2**64:
                raise ValueError
            return n
        base,quote,vq=map(u64,(pre_base_atomic,pre_quote_atomic,virtual_quote_atomic))
        if (base==0 or quote+vq<=0 or not _positive(state_known_at) or not _positive(virtual_quote_known_at)
                or virtual_quote_known_at>state_known_at):
            return None
        return (quote+vq)/base*.001
    except (TypeError,ValueError,OverflowError):
        return None
