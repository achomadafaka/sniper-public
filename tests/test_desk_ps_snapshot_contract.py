"""Additive PS provenance is causal; legacy bytes stay readable and never gain a price."""
import copy
import pytest
from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk as desk
from sniper.services import memecoin_desk_pumpswap_candidates as P
from test_memecoin_desk_pumpswap_contract import ps_candidate, rehash, NOW
from test_memecoin_desk_pumpswap_candidates import facts, event, feed, SIG, NOW as BOOK_NOW


def enriched():
    c=ps_candidate()
    c['opportunity_id']=c['ticket_id']
    c['snapshot']['fields']['pool_state']['value'].update(
        reserve_known_at=NOW-2, sol_usd=100., sol_age_s=5.,
        virtual_quote_known_at=NOW-2, virtual_quote_slot=7,
        virtual_quote_source='pumpswap_grpc:buy_event_virtual_quote',cut_slot=8)
    return rehash(c)


def test_additive_state_preserves_legacy_envelope_and_hash():
    old=ps_candidate(); frozen=copy.deepcopy(old)
    assert K.validate_candidate(old)['snapshot']==frozen['snapshot'] and old==frozen
    assert K.validate_candidate(enriched())['snapshot']==enriched()['snapshot']


@pytest.mark.parametrize('key,value',[
    ('reserve_known_at',NOW+1),('reserve_known_at',True),('sol_usd',0),
    ('sol_age_s',-1),('sol_age_s',float('inf')),('virtual_quote_known_at',NOW+1),
    ('virtual_quote_slot',8),('virtual_quote_source','free text')])
def test_bad_optional_provenance_is_refused(key,value):
    c=enriched();c['snapshot']['fields']['pool_state']['value'][key]=value
    with pytest.raises(ValueError): K.validate_candidate(rehash(c))


@pytest.mark.parametrize('price',[None,{'mcap_usd':1234.,'liq_usd':56.,'ref_price_usd':.001}])
def test_ps_sheet_uses_the_same_cap_or_hides_unpriceable_values(monkeypatch,price):
    c=enriched();seen=[]
    def cap(candidate,*,clip_usd,basis):
        seen.append((candidate,clip_usd,basis));return price
    monkeypatch.setattr(desk,'priced_verdict',cap)
    sheet=K.render_judge_sheet(c)
    assert seen==[(c,25,'asof')]
    assert sheet['verdict_price']==price
    assert sheet['pool_state']==(c['snapshot']['fields']['pool_state']['value'] if price else None)
    assert sheet['mcap_usd']==(price['mcap_usd'] if price else None)
    assert sheet['liq_usd']==(price['liq_usd'] if price else None)
    assert sheet['supply_basis']=='supply_at_read'


def test_emission_uses_receipt_and_event_virtual_quote_not_cached_metadata():
    NOW=BOOK_NOW
    b=P.PoolBook(band=(30_000,100_000),cooldown_s=300,sol_max_age_s=120)
    assert feed(b) is None
    m,meta=facts();meta['virtual_quote_atomic']='99999999999999999'
    e=event('40000000000');e.update(virtual_quote_atomic='0',virtual_quote_source='pumpswap_grpc:buy_event_virtual_quote')
    c=b.on_swap(event=e,mapping=m,metadata=meta,slot=11,signature=SIG,ordinal=0,
        tx_index=None,now=NOW+3,received_at=NOW+1,sol_usd=100,sol_usd_at=NOW)
    assert c is not None
    state=c['snapshot']['fields']['pool_state']['value']
    assert state['reserve_known_at']==state['virtual_quote_known_at']==NOW+1
    assert state['virtual_quote_atomic']=='0' and state['sol_usd']==100 and state['sol_age_s']==3


def test_late_processing_cannot_import_metadata_received_after_the_swap():
    NOW=BOOK_NOW
    b=P.PoolBook(band=(30_000,100_000),cooldown_s=300,sol_max_age_s=120)
    assert feed(b) is None
    m,meta=facts();meta['known_at']=NOW+2
    e=event('40000000000');e.update(virtual_quote_atomic='0',virtual_quote_source='pumpswap_grpc:buy_event_virtual_quote')
    assert b.on_swap(event=e,mapping=m,metadata=meta,slot=11,signature=SIG,ordinal=0,
        tx_index=None,now=NOW+3,received_at=NOW+1,sol_usd=100,sol_usd_at=NOW) is None
    assert b.last_reason=='metadata_not_yet_known'


def test_ps_sheet_reference_is_the_actual_queue_cap_reference():
    c=enriched()
    price=desk.priced_verdict(c,clip_usd=25,basis='asof')
    assert price is not None
    sheet=K.render_judge_sheet(c)
    assert sheet['verdict_price']==price
    assert sheet['verdict_price']['ref_price_usd']==price['ref_price_usd']
    assert sheet['pool_state']['sol_usd']==c['snapshot']['fields']['pool_state']['value']['sol_usd']


def test_new_decision_cut_allows_a_later_mint_read_without_rewriting_emission():
    old=ps_candidate();frozen=copy.deepcopy(old)
    c=enriched();s=c['snapshot']['fields']['pool_state']['value']
    s.update(cut_slot=10,metadata_slot=8,metadata_known_at=NOW-1)
    assert K.validate_candidate(rehash(c))['snapshot']==c['snapshot']
    assert old==frozen
    s['metadata_slot']=10
    with pytest.raises(ValueError):K.validate_candidate(rehash(c))
    s.update(metadata_slot=8,cut_slot=7)
    with pytest.raises(ValueError):K.validate_candidate(rehash(c))


def test_ps_worker_uses_the_ps_sol_bound_when_the_curve_bound_is_looser(tmp_path,monkeypatch):
    from test_memecoin_desk_pumpswap_worker import W
    from sniper.settings import memecoin as cfg
    for key,value in (('CAPTURE','1'),('EMISSIONS','1'),('BAND_LO_USD','30000'),('BAND_HI_USD','100000')):
        monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_'+key,value)
    monkeypatch.setattr(cfg,'MEMECOIN_DESK_PS_SOL_MAX_AGE_S',120.)
    knobs=W.knobs({});knobs['sol_max_age_s']='300'
    w=W.Worker(knobs,base=tmp_path,sol_price_fn=lambda:None,alert_fn=lambda *_:None)
    assert feed(w.ps_book) is None
    m,meta=facts();e=event('40000000000')
    assert w.ps_book.on_swap(event=e,mapping=m,metadata=meta,slot=11,signature=SIG,ordinal=0,
        tx_index=None,now=BOOK_NOW+1,received_at=BOOK_NOW+1,sol_usd=100,sol_usd_at=BOOK_NOW-120) is None
    assert w.ps_book.last_reason=='sol_stale'


def test_source_metadata_at_exact_receipt_cut_is_not_yet_known():
    b=P.PoolBook(band=(30_000,100_000),cooldown_s=300,sol_max_age_s=120)
    assert feed(b) is None
    m,meta=facts();meta['known_at']=BOOK_NOW+1
    assert b.on_swap(event=event('40000000000'),mapping=m,metadata=meta,slot=11,signature=SIG,
        ordinal=0,tx_index=None,now=BOOK_NOW+2,received_at=BOOK_NOW+1,
        sol_usd=100,sol_usd_at=BOOK_NOW) is None
    assert b.last_reason=='metadata_not_yet_known'
