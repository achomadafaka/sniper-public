"""Causal prior-dump admission checks; synthetic prices, named incident mints."""
import importlib

import pytest


def api():
    return importlib.import_module('sniper.services.memecoin_desk_dump_group')


def swap(i, *, actor=None, slot=100, price=1., buy=False, t=None, mint='APE', **kw):
    return dict(venue='curve', pool=mint, mint=mint, signature=f'{mint}-{i}', ordinal=0,
                slot=slot, tx_index=i, pre_real_quote_sol=100., known_at=float(i if t is None else t), is_buy=buy,
                actor=actor or f'a{i}', quote_sol=1., price_sol=price, price_before_sol=price, **kw)


def dump(mint='APE', start=100.):
    return [swap(i, t=start+i/100, price=1-i/38, mint=mint) for i in range(20)]


def test_module_exists():
    assert importlib.util.find_spec('sniper.services.memecoin_desk_dump_group') is not None


def test_earliest_prefix_needs_both_sellers_and_price_and_deduplicates():
    rows=dump()
    assert api().detect_bursts(rows[:-1], asof=102)==[]
    events=api().detect_bursts(rows+rows, asof=102)
    assert len(events)==1
    assert events[0]['detected_at']==100.19
    assert len(events[0]['participants'])==20
    assert api().detect_bursts([dict(r, price_sol=1,price_before_sol=1) for r in rows], asof=102)==[]


def test_four_slots_not_five_and_reused_actor_not_distinct():
    rows=dump()
    assert api().detect_bursts([dict(r, slot=100+i//5) for i,r in enumerate(rows)], asof=102)
    assert not api().detect_bursts([dict(r, slot=100 if i<19 else 104) for i,r in enumerate(rows)], asof=102)
    assert not api().detect_bursts([dict(r, actor='same') for r in rows], asof=102)


def test_future_price_and_excluded_wallets_cannot_prove_dump():
    rows=dump()
    assert not api().detect_bursts(rows, asof=100.18)
    assert not api().detect_bursts(rows, asof=102, excluded={'a19'})
    rows[-1]['price_known_at']=110
    assert not api().detect_bursts(rows, asof=102)
    assert not api().detect_bursts([dict(r, price_sol=None,price_before_sol=None) for r in rows], asof=102)


def test_causal_group_shares_and_bot_expiry_exclusions():
    events=api().detect_bursts(dump(), asof=102)
    assert api().actor_flags(events, asof=99)=={}
    flags=api().actor_flags(events, asof=105)
    rows=[swap(0, actor='a0', buy=True,t=104),swap(1,actor='new',buy=True,t=104)]
    got=api().buyer_shares(rows,flags,asof=105)
    assert got['buyer_share']==.5 and got['buy_sol_share']==.5 and got['matched_buyers']==1
    assert got['status']=='kill' and got['reason']=='dump_group_buyers'
    assert api().actor_flags(events,asof=100.19+86401)=={}
    five=[dict(events[0],id=str(i)) for i in range(5)]
    flags=api().actor_flags(five,asof=105)
    assert flags['a0']['burst_count']==5
    got=api().buyer_shares(rows,flags,asof=105)
    assert got['buyer_share']==0 and got['frequent_bot_buyers']==1
    assert api().buyer_shares(rows,flags,asof=105,excluded={'new'})['buyers']==0


def test_denominator_zero_unknown_not_clean_and_sol_threshold_independent():
    assert api().buyer_shares([],{},asof=1)['status']=='unknown'
    flags={'a0':{'known_at':0.,'burst_count':1}}
    rows=[swap(i,actor=f'a{i}',buy=True,t=90,quote_sol_override=None) for i in range(5)]
    for r in rows:r.pop('quote_sol_override')
    rows[0]['quote_sol']=10.
    got=api().buyer_shares(rows,flags,asof=100)
    assert got['buyer_share']==.2 and got['buy_sol_share']> .4 and got['status']=='kill'
    rows[1]['quote_sol']=None
    assert api().buyer_shares(rows,flags,asof=100)['status']=='unknown'


def test_volume_and_reserve_conditions_exact_boundaries():
    rows=dump()
    assert not api().detect_bursts([dict(r,pre_real_quote_sol=None) for r in rows],asof=102)
    assert not api().detect_bursts([dict(r,pre_real_quote_sol=1000) for r in rows],asof=102)
    rows.append(swap(20,buy=True,t=101,price=.5,quote_sol_override=20/19))
    rows[-1]['quote_sol']=rows[-1].pop('quote_sol_override')
    # Exactly 5% buy share must not qualify in a window assembled before the sell threshold.
    rows[-1]['known_at']=99
    assert not api().detect_bursts(rows,asof=102)


def test_incremental_batch_identical_no_cross_pool_seller_merge():
    rows=dump()
    detector=api().BurstDetector()
    events={}
    for r in rows:
        e=detector.feed(r)
        if e:events[e['id']]=e
    assert list(events.values())==api().detect_bursts(rows,asof=102)
    rows=[dict(r,pool='other' if i<10 else 'APE') for i,r in enumerate(rows)]
    assert api().detect_bursts(rows,asof=102)==[]


def test_contract_rejects_prior_dump_group_at_both_gates(monkeypatch):
    from test_memecoin_desk_contract import candidate,evidence,policy,AT
    from sniper.services import memecoin_desk_contract as C
    c=candidate();e=evidence(c)
    e['checks']['dump_group']=dict(api().buyer_shares(
        [swap(0,actor='a0',buy=True,t=100)],{'a0':dict(known_at=99,burst_count=1)},asof=101),
        positive_proven=True,target_complete=True,exclusions_valid=True,version=api().VERSION,policy_hash=api().POLICY_HASH,known_at=101,cutoff=101,expires_at=104,pool=c['pool'],mint=c['mint'])
    # Bind to the fixture's real clock.
    from datetime import datetime
    now=datetime.fromisoformat(AT).timestamp()
    e['checks']['dump_group'].update(known_at=now,cutoff=now,expires_at=now+3)
    assert C.hard_filter(c,evidence=e,policy=policy(),evaluated_at=AT)['reason']=='dump_group_buyers'
    e['checks']['dump_group']['expires_at']=now-1
    r=C.hard_filter(c,evidence=e,policy=policy(),evaluated_at=AT)
    assert r['action']=='PASS' and r['gate_inputs']['dump_group']['status']=='unknown'


def test_partial_quote_does_not_hide_proven_address_threshold():
    rows=[swap(i,actor=f'a{i}',buy=True,t=90) for i in range(4)]
    rows[3]['quote_sol']=None
    flags={'a0':{'known_at':0.,'burst_count':1}}
    got=api().buyer_shares(rows,flags,asof=100)
    assert got['buyer_share']==.25 and got['buy_sol_share'] is None and got['status']=='kill'


def test_late_old_slot_cannot_invent_dump_and_new_episode_counts_once():
    d=api().BurstDetector()
    events=[]
    for r in dump():
        e=d.feed(r)
        if e:events.append(e)
    # A new quiet slot does not renew an old episode; a repeated actor is not 5 bursts.
    assert d.feed(swap(21,slot=110,t=103,buy=True,price=1.)) is None
    assert d.feed(swap(22,slot=100,t=104,price=.01)) is None
    assert len(api().actor_flags(events,asof=105))==20
    assert {f['burst_count'] for f in api().actor_flags(events,asof=105).values()}=={1}


def test_unknown_buyer_identity_cannot_inflate_matched_address_share():
    rows=[swap(0,actor='a0',buy=True,t=90)]
    rows += [dict(swap(i,buy=True,t=90),actor=None,quote_sol=10.) for i in range(1,5)]
    got=api().buyer_shares(rows,{'a0':{'known_at':0.,'burst_count':1}},asof=100)
    assert got['status']=='unknown' and got['buyer_share']<=.2


def test_incident_tape_ape_then_eh_proves_preentry_block():
    import json
    from pathlib import Path
    f=json.loads((Path(__file__).parent/'fixtures/desk_dump_group_incidents.json').read_text())
    cut=f['eh_cutoff']
    events=api().detect_bursts(f['ape'],asof=cut)
    assert len(events)==1 and events[0]['n_distinct_sellers']==133
    assert events[0]['detected_at']<cut-500
    # Independent finite-set oracle from this bounded APE sell window.
    actors={r['actor'] for r in f['ape'] if not r['is_buy']}
    target=[r for r in f['eh_predecision'] if r['is_buy']]
    buyers={r['actor'] for r in target}
    matched=buyers & actors
    assert len(matched)==43 and len(buyers)==78
    oracle_sol=sum(r['quote_sol'] for r in target if r['actor'] in actors)/sum(r['quote_sol'] for r in target)
    got=api().buyer_shares(f['eh_predecision'],api().actor_flags(events,asof=cut),asof=cut)
    assert got['status']=='kill' and got['buyer_share']==43/78
    assert got['buy_sol_share']==pytest.approx(oracle_sol)
    # EH's own future dump cannot contribute to pre-entry membership.
    assert api().detect_bursts(f['eh'],asof=cut)==[]
    assert api().detect_bursts(f['eh'],asof=cut+20)


def test_pump_and_return_is_not_a_dump_and_endpoint_is_pre_current():
    rows=dump()
    rows=[dict(r,price_before_sol=1.,price_sol=3. if 0<i<19 else 1.) for i,r in enumerate(rows)]
    assert api().detect_bursts(rows,asof=102)==[]
    rows=dump()
    # Current post-state can plunge; its PRE-state has not crossed the rule yet.
    rows=[dict(r,price_before_sol=1.,price_sol=.1 if i==19 else 1.) for i,r in enumerate(rows)]
    assert api().detect_bursts(rows,asof=102)==[]


def test_ps_pre_price_requires_causally_proven_virtual_quote():
    assert hasattr(api(),'pumpswap_pre_price')
    fn=api().pumpswap_pre_price
    assert fn('1000000','1000000000',virtual_quote_atomic=None,state_known_at=10,virtual_quote_known_at=9) is None
    assert fn('1000000','1000000000',virtual_quote_atomic='2000000000',state_known_at=10,virtual_quote_known_at=11) is None
    assert fn('0','1000000000',virtual_quote_atomic='2000000000',state_known_at=10,virtual_quote_known_at=9) is None
    assert fn('1000000','1000000000',virtual_quote_atomic='2000000000',state_known_at=10,virtual_quote_known_at=9)==3.
    assert fn('1000000','1000000000',virtual_quote_atomic=0,state_known_at=10,virtual_quote_known_at=9)==1.
