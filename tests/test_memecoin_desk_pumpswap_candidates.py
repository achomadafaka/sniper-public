"""Default-unavailable PumpSwap pricing facts never become an invented emission."""
import pytest

from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_pumpswap_candidates as P
from sniper.services import memecoin_desk_safety as S
from sniper.services import memecoin_desk_universe as U

MINT = U.b58encode(bytes([1]) * 32)
POOL = S.pumpswap_pool_address(MINT)
SIG = U.b58encode(bytes([3]) * 64)
NOW = 1_800_000_000.0


def facts():
    mapping = {'pool': POOL, 'mint': MINT, 'quote_mint': K.WSOL_MINT,
               'known_at': NOW - 100, 'slot': 1, 'canonical': True}
    metadata = {'pool': POOL, 'mint': MINT, 'quote_mint': K.WSOL_MINT, 'base_decimals': 6,
                'supply_atomic': '1000000000000000', 'virtual_quote_atomic': '0',
                'known_at': NOW - 80, 'slot': 2, 'source': 'fixture:independent_pool_facts'}
    return mapping, metadata


def event(quote='20000000000'):
    return {'kind': 'buy', 'pool': POOL, 'post_base_reserve_atomic': '100000000000000',
            'post_quote_reserve_atomic': quote, 'lp_fee_bps': 20,
            'protocol_fee_bps': 5, 'creator_fee_bps': 5, 'cashback_fee_bps': 0,
            'virtual_quote_atomic': '0', 'virtual_quote_source': 'pumpswap_grpc:buy_event_virtual_quote'}


def feed(book, *, quote='20000000000', slot=10, now=NOW, mapping=None, metadata=None, **extra):
    m, meta = facts()
    return book.on_swap(event=event(quote), mapping=m if mapping is None else mapping,
        metadata=meta if metadata is None else metadata, slot=slot, signature=SIG,
        ordinal=0, tx_index=None, now=now, received_at=now, sol_usd=100, sol_usd_at=now - 1, **extra)


def test_witnessed_crossing_builds_valid_venue_bound_candidate_and_original_provenance():
    book = P.PoolBook(band=(30_000, 100_000), cooldown_s=300, sol_max_age_s=120)
    assert feed(book) is None
    c = feed(book, quote='40000000000', slot=11, now=NOW + 1)
    assert c['schema'] == K.PUMPSWAP_SCHEMA and c['venue'] == 'pumpswap'
    assert K.validate_candidate(c)['snapshot'] == c['snapshot']
    assert S.verify_u_emission(c, now=NOW + 2, max_age_s=60)['ticket_id'] == c['ticket_id']
    state = c['snapshot']['fields']['pool_state']['value']
    assert state['base_reserve_atomic'] == '100000000000000'
    assert state['metadata_known_at'] == NOW - 80 and state['metadata_slot'] == 2
    assert state['metadata_source'] == 'fixture:independent_pool_facts'
    assert c['snapshot']['fields']['mcap_usd']['value'] == 40000
    assert c['snapshot']['fields']['liq_usd']['value'] == 4000
    assert c['snapshot']['fields']['curve_pct']['value'] is None
    assert c['snapshot']['fields']['curve_pct']['completeness'] == 'absent'


def test_missing_metadata_and_unknown_post_reserves_reset_crossing():
    book = P.PoolBook(band=(30_000, 100_000), cooldown_s=300, sol_max_age_s=120)
    assert feed(book) is None
    mapping, _ = facts()
    assert book.on_swap(event=event('40000000000'), mapping=mapping, metadata=None,
        slot=11, signature=SIG, ordinal=0, tx_index=None, now=NOW + 1, received_at=NOW + 1,
        sol_usd=100, sol_usd_at=NOW) is None
    assert feed(book, quote='40000000000', slot=12, now=NOW + 2) is None
    assert feed(book, slot=13, now=NOW + 3) is None
    e = event('40000000000'); e['post_quote_reserve_atomic'] = None
    _, meta = facts()
    assert book.on_swap(event=e, mapping=mapping, metadata=meta, slot=14, signature=SIG,
        ordinal=0, tx_index=None, now=NOW+4, received_at=NOW+4, sol_usd=100, sol_usd_at=NOW) is None
    assert feed(book, quote='40000000000', slot=15, now=NOW + 5) is None


@pytest.mark.parametrize('kind,key,value', [
    ('metadata', 'base_decimals', True), ('metadata', 'supply_atomic', None),
    ('event', 'virtual_quote_atomic', None), ('event', 'virtual_quote_atomic', '00'),
    ('metadata', 'supply_atomic', str(2**64)), ('metadata', 'known_at', NOW + 50),
    ('metadata', 'slot', 12), ('metadata', 'source', 'untrusted free text'),
    ('mapping', 'canonical', False), ('mapping', 'quote_mint', MINT),
    ('mapping', 'known_at', NOW + 50), ('mapping', 'slot', 12), ('mapping', 'mint', POOL),
    ('event', 'cashback_fee_bps', None), ('event', 'cashback_fee_bps', 1),
    ('event', 'lp_fee_bps', None), ('event', 'creator_fee_bps', 10000),
    ('event', 'post_base_reserve_atomic', '0')])
def test_missing_future_wrong_or_malformed_facts_refuse_and_reseed(kind, key, value):
    b = P.PoolBook(band=(30_000, 100_000), cooldown_s=300, sol_max_age_s=120)
    assert feed(b) is None
    m, meta = facts(); e = event('40000000000')
    {'mapping': m, 'metadata': meta, 'event': e}[kind][key] = value
    assert b.on_swap(event=e, mapping=m, metadata=meta, slot=11, signature=SIG,
        ordinal=0, tx_index=None, now=NOW+1, received_at=NOW+1, sol_usd=100, sol_usd_at=NOW) is None
    assert feed(b, quote='40000000000', slot=12, now=NOW+2) is None


def test_stale_or_future_sol_gaps_and_cooldown_do_not_create_crossings():
    b = P.PoolBook(band=(30_000, 100_000), cooldown_s=300, sol_max_age_s=120)
    assert feed(b) is None
    m, meta = facts()
    for age in (121, -1):
        assert b.on_swap(event=event('40000000000'), mapping=m, metadata=meta, slot=11,
            signature=SIG, ordinal=0, tx_index=None, now=NOW+1, received_at=NOW+1,
            sol_usd=100, sol_usd_at=NOW+1-age) is None
    assert feed(b, quote='40000000000', slot=12, now=NOW+2) is None
    assert feed(b, slot=13, now=NOW+3) is None
    assert feed(b, quote='40000000000', slot=14, now=NOW+4)
    assert feed(b, slot=15, now=NOW+5) is None
    assert feed(b, quote='40000000000', slot=16, now=NOW+6) is None
    b.mark_unknown('restart')
    assert feed(b, quote='40000000000', slot=17, now=NOW+400) is None


def test_unknown_same_slot_order_cannot_claim_a_source_crossing():
    b = P.PoolBook(band=(30_000, 100_000), cooldown_s=300, sol_max_age_s=120)
    assert feed(b) is None
    assert feed(b, quote='40000000000', slot=10, now=NOW+1) is None


def test_unrepresentable_wall_clock_refuses_instead_of_crashing():
    b = P.PoolBook(band=(30_000, 100_000), cooldown_s=300, sol_max_age_s=120)
    m, meta = facts()
    assert b.on_swap(event=event(), mapping=m, metadata=meta, slot=10, signature=SIG,
        ordinal=0, tx_index=None, now=1e100, received_at=1e100, sol_usd=100, sol_usd_at=1e100) is None
