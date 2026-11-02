"""Frozen D provider-claim contract and causal listing-row parsing."""
import hashlib
import importlib
import json
from pathlib import Path

import pytest

NOW = 1793187000.0
MINT = '35fWL6pdJqpconRZPrawUAQegw1QF3p2tgN7mzhTpump'


def listing(tmp_path, *, feed='fomo', raw=None, age=20, offset=.5):
    from sniper.services import memecoin_desk_intake as I
    at = NOW-age
    if raw is None:
        raw = {'transport': 'dom', 'coverage': 'partial', 'item': {'cells': [
            'PMUP\\nPMUP\\n$100', '$123.4K', '\\u25b2\\n265.16%', '$526.4K', '765\\n1.5K en total',
            'untrusted free text must not reach the sheet']}}
    conn = I.open_day(tmp_path/'intake', feed, at)
    I.record_poll(conn, feed, 'trending24h', at, [{'network_id': 'solana', 'chain_raw': 'solana',
        'mint': MINT, 'venue': None, 'raw': raw}], offset_s=offset)
    row = conn.execute('SELECT provider_rank,rank_kind,observed_at,host_received_at,clock_offset_s,source_ref '
                       'FROM intake_rows LIMIT 1').fetchone()
    claim = dict(zip(['provider_rank','rank_kind','observed_at','host_received_at','clock_offset_s','source_ref'],row))
    ref = claim.pop('source_ref')
    claim.update(feed=feed, filter='trending24h', intake_row_ref=I.utc_day(at)+':'+ref)
    conn.close()
    return {'mint': MINT, 'network_id': 'solana', 'intake': claim}


def read(candidate, tmp_path):
    from sniper.services import memecoin_desk_list_claims as L
    return L.list_claims_for(candidate, desk_dir=tmp_path, as_of=NOW)


@pytest.mark.parametrize('name,version', [('heat_v2', 'desk-heat-v2'),
    ('list_claims', 'desk-list-claims-v1'), ('dex_token', 'desk-dex-token-v1')])
def test_definition_binds_canonical_json_and_closed_shapes(name, version):
    m = importlib.import_module('sniper.services.memecoin_desk_' + name)
    path = Path(m.__file__).with_name('desk_context_definitions') / (name + '.json')
    d = json.loads(path.read_bytes())
    assert m.definition() == {'version': version,
        'definition_sha256': hashlib.sha256(json.dumps(d, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()}
    assert m.KNOWN_KEYS == frozenset(d['known_keys'])
    assert m.UNKNOWN_KEYS == {'state', 'reason', 'definition_sha256', 'as_of'}
    assert m.REASONS == frozenset(d['reasons'])
    assert d['freshness_s'] > 0 and d['clock'] and isinstance(d['nullable_fields'], list)


def test_heat_definition_freezes_gap_proof_and_empty_promoter_semantics():
    from sniper.services import memecoin_desk_heat_v2 as H
    assert H.DEFINITION['minimum_coverage_fraction'] == 0.9
    assert {'covered_s', 'gap_s', 'author_unknown_n', 'scan_loss'} <= H.WINDOW_KNOWN_KEYS
    root = Path(H.__file__).with_name('desk_context_definitions')
    for name, key in [('promoters', 'promoter_list_sha256'), ('common_words', 'common_word_list_sha256')]:
        assert hashlib.sha256(json.dumps(json.loads((root / (name + '.json')).read_bytes()), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest() == H.DEFINITION[key]
    assert json.loads((root / 'promoters.json').read_bytes())['handles'] == []
    assert 'promoter_list_empty' in H.FIELD_REASONS


def test_fomo_cells_are_provider_claims_with_exact_row_clock(tmp_path):
    r = read(listing(tmp_path), tmp_path)
    assert r['state'] == 'known' and r['reason'] is None and r['parse_ok'] is True
    assert (r['vol_24h_usd'], r['change_24h_pct'], r['holders'], r['holders_total'], r['mcap_claim_usd']) == (
        526400, 265.16, 765, 1500, 123400)
    assert (r['as_of'], r['row_age_s'], r['label'], r['source']) == (NOW-20.5, 20.5, 'provider_claim', 'fomo')
    assert r['field_unknown'] == {} and 'untrusted' not in json.dumps(r)


@pytest.mark.parametrize('cell,value,field,expected', [(2,'▼\n12.5%','change_24h_pct',-12.5),
    (1,'$0','mcap_claim_usd',0), (3,'$1,234.50','vol_24h_usd',1234.5),
    (4,'1.2K\n2M total','holders',1200), (3,'$2B','vol_24h_usd',2000000000)])
def test_human_numeric_units_and_signs(tmp_path, cell, value, field, expected):
    c = listing(tmp_path)
    from sniper.services import memecoin_desk_intake_proof as P
    path, ref, _ = P._source_path(tmp_path/'intake', c['intake'])
    import sqlite3
    conn = sqlite3.connect(path)
    raw = json.loads(conn.execute('SELECT raw_json FROM intake_rows WHERE source_ref=?',(ref,)).fetchone()[0])
    raw['item']['cells'][cell] = value
    conn.execute('UPDATE intake_rows SET raw_json=? WHERE source_ref=?',(json.dumps(raw),ref));conn.commit();conn.close()
    assert read(c,tmp_path)[field] == expected


@pytest.mark.parametrize('value', ['$NaN', '$inf', '$-1', '$1,2', '$3Q', ''])
def test_bad_cell_preserves_other_claims_and_never_becomes_zero(tmp_path,value):
    raw = {'item': {'cells': ['PMUP','$123K','+2%',value,'3\n5 total']}}
    r = read(listing(tmp_path,raw=raw),tmp_path)
    assert r['state'] == 'known' and r['parse_ok'] is False
    assert r['vol_24h_usd'] is None and r['field_unknown']['vol_24h_usd'] == 'parse_cell'
    assert r['mcap_claim_usd'] == 123000 and r['holders'] == 3


def test_pumpfun_usd_field_is_not_sol_market_cap_and_absence_is_explicit(tmp_path):
    r = read(listing(tmp_path,feed='pumpfun',raw={'symbol':'PMUP','market_cap':55,'usd_market_cap':12345}),tmp_path)
    assert r['state'] == 'known' and r['parse_ok'] is True and r['mcap_claim_usd'] == 12345
    assert r['vol_24h_usd'] is None and r['field_unknown']['vol_24h_usd'] == 'not_reported'


@pytest.mark.parametrize('age,offset,reason', [(301,0,'stale'),(-1,0,'future_source'),(1,None,'clock_unknown')])
def test_source_clock_and_freshness_fail_closed(tmp_path,age,offset,reason):
    r = read(listing(tmp_path,age=age,offset=offset),tmp_path)
    assert r['state'] == 'unknown' and r['reason'] == reason
    assert set(r) == {'state','reason','definition_sha256','as_of'}


def test_wrong_identity_missing_source_and_unsupported_feed_are_typed(tmp_path):
    c = listing(tmp_path);c['mint']='wrong'
    assert read(c,tmp_path)['reason'] == 'source_mismatch'
    assert read({},tmp_path)['reason'] == 'no_source'
    c=listing(tmp_path/'other',feed='dexscreener')
    assert read(c,tmp_path/'other')['reason'] == 'unsupported_source'


@pytest.mark.parametrize('raw', [{}, {'item':None}, {'item':{'cells':None}}, {'item':{'cells':[]}},
    {'item':{'cells':['PMUP','bad','bad','bad','bad']}}, {'symbol':'PMUP'}])
def test_unparsable_bound_row_is_whole_unknown(tmp_path, raw):
    r=read(listing(tmp_path,raw=raw),tmp_path)
    assert r['state']=='unknown' and r['reason']=='parse_failed'
