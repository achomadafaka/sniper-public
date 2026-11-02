"""Offline intake evidence: exact atomic arithmetic, causal pool view and strict mint metadata."""
import base64
import copy
import json
import sqlite3
import struct

import pytest

from sniper.services import memecoin_desk_ps_evidence as E
from sniper.services import memecoin_desk_ps_asof as A
from test_desk_ps_asof import build, candidate, swap, NOW, POOL, MINT


def mint(*,decimals=6,supply=10**15,owner=None,extensions=b''):
    raw=bytearray(82);raw[44]=decimals;raw[45]=1;struct.pack_into('<Q',raw,36,supply)
    if extensions:raw+=bytes(83)+b'\x01'+extensions
    return {'owner':owner or E.TOKEN_PROGRAM,'executable':False,'data':[base64.b64encode(raw).decode(),'base64'],'context_slot':21}


def fixture(tmp_path,**kw):
    desk=build(tmp_path,**kw)
    beat=json.loads((desk/'universe.heartbeat').read_text());beat['ps_capture_from']=NOW-600
    (desk/'universe.heartbeat').write_text(json.dumps(beat))
    c=sqlite3.connect(desk/'pumpswap/tape.db');c.execute('CREATE TABLE ps_gap_spans(start_at REAL,end_at REAL)');c.commit();c.close()
    cand,_=A.refresh(candidate(),desk_dir=desk,now=NOW-.5)
    return desk,cand


def evidence(desk,cand,**kw):
    args=dict(desk_dir=desk,as_of_host=NOW-.5,clip_usd=25,sol_usd=100,sol_at=NOW-2,
              mint_account=mint(),mint_known_at=NOW,evaluated_at_host=NOW+.1)
    args.update(kw)
    return E.pumpswap_evidence(cand,**args)


def test_complete_ps_math_and_mint_supply_are_bound_without_a_curve_read(tmp_path):
    desk,c=fixture(tmp_path);before=copy.deepcopy(c)
    out=evidence(desk,c)
    assert c==before and out['snapshot_hash']==c['snapshot']['hash']
    checks=out['checks']
    assert all(checks[k]['status']=='pass' for k in ('market_data','token_safety','sell_probe'))
    assert checks['token_safety']['supply_atomic']==str(10**15) and checks['token_safety']['decimals']==6
    assert checks['sell_probe']['exact_quantity'] is True
    assert 0<checks['sell_probe']['round_trip_loss_pct']<1
    assert all(checks[k]['status']=='unknown' for k in ('holders','cluster','dump_group'))
    assert out['known_at']==E.iso(NOW)


@pytest.mark.parametrize('decimals,supply',[(0,12345),(9,2**63+7),(18,123456)])
def test_any_mint_supply_and_decimals_come_from_account_not_pump_defaults(tmp_path,decimals,supply):
    desk,c=fixture(tmp_path)
    c['snapshot']['fields']['pool_state']['value'].update(base_decimals=decimals,supply_atomic=str(supply))
    out=evidence(desk,c,mint_account=mint(decimals=decimals,supply=supply))
    assert out['checks']['token_safety']['supply_atomic']==str(supply)
    assert out['checks']['token_safety']['decimals']==decimals


@pytest.mark.parametrize('kw,reason',[
    ({'mint_account':None},'mint_unreadable'),({'mint_known_at':None},'mint_clock_unknown'),
    ({'mint_known_at':NOW+1},'mint_future'),({'mint_known_at':NOW-200},'mint_stale'),
    ({'sol_usd':None},'sol_usd_unknown'),({'sol_usd':float('nan')},'sol_usd_unknown'),
    ({'sol_at':NOW},'sol_usd_future'),({'sol_at':NOW-200},'sol_usd_stale'),
    ({'clip_usd':0},'clip_invalid'),({'clip_usd':True},'clip_invalid')])
def test_missing_stale_future_or_invalid_input_never_passes(tmp_path,kw,reason):
    desk,c=fixture(tmp_path);checks=evidence(desk,c,**kw)['checks']
    assert checks['sell_probe']['status']=='unknown'
    assert reason in {r.get('reason') for r in checks.values()}


def test_newer_invalid_state_is_not_hidden_by_older_good_state(tmp_path):
    desk,c=fixture(tmp_path)
    conn=sqlite3.connect(desk/'pumpswap/tape.db')
    conn.execute("UPDATE swaps_ps SET cashback_fee_bps=NULL");conn.commit();conn.close()
    assert evidence(desk,c)['checks']['sell_probe']['status']=='unknown'


def test_capture_gap_after_the_state_refuses_and_missing_tape_is_unknown(tmp_path):
    desk,c=fixture(tmp_path)
    conn=sqlite3.connect(desk/'pumpswap/tape.db');conn.execute('INSERT INTO gaps_ps VALUES(?,?,?)',(NOW-1,'disconnect',18));conn.commit();conn.close()
    assert evidence(desk,c)['checks']['market_data']['status']=='unknown'
    assert evidence(tmp_path/'missing',c)['checks']['sell_probe']['status']=='unknown'


@pytest.mark.parametrize('change',[{'context_slot':1},{'context_slot':True},{'owner':'wrong'},{'executable':True}])
def test_mint_owner_context_and_executable_are_not_guessed_safe(tmp_path,change):
    desk,c=fixture(tmp_path);acc=dict(mint(),**change)
    assert evidence(desk,c,mint_account=acc)['checks']['token_safety']['status']=='unknown'


@pytest.mark.parametrize('rawchange',[(0,2),(45,0),(44,255)])
def test_invalid_coption_initialization_and_decimals_fail_closed(tmp_path,rawchange):
    desk,c=fixture(tmp_path);acc=mint();raw=bytearray(base64.b64decode(acc['data'][0]));raw[rawchange[0]]=rawchange[1]
    acc['data'][0]=base64.b64encode(raw).decode()
    assert evidence(desk,c,mint_account=acc)['checks']['token_safety']['status']=='unknown'


def test_token2022_malformed_tlv_and_duplicate_hook_cannot_hide_a_hook(tmp_path):
    desk,c=fixture(tmp_path)
    for tlv in (struct.pack('<HH',18,64)+b'x', struct.pack('<HH',14,64)+bytes(64)+struct.pack('<HH',14,64)+bytes(64)):
        acc=mint(owner=E.TOKEN_2022_PROGRAM,extensions=tlv)
        assert evidence(desk,c,mint_account=acc)['checks']['token_safety']['status']=='unknown'


def test_round_trip_uses_lp_retained_post_buy_and_sells_exact_bought_quantity():
    result=E.round_trip(base=1000000,real_quote=1000000,virtual_quote=0,fees=(20,5,30,0),spend=100000)
    net=99452  # ceil fees:199+50+299; exactly100000 including net
    tokens=1000000*net//(1000000+net)
    net=(1000000*tokens+1000000-tokens-1)//(1000000-tokens)
    real_after=1000000+net+(net*20+9999)//10000
    gross=real_after*tokens//1000000
    back=gross-sum((gross*b+9999)//10000 for b in (20,5,30))
    assert result['tokens_atomic']==str(tokens) and result['quote_back_atomic']==str(back)
    assert result['post_buy_quote_atomic']==str(real_after)
    assert result['loss_pct']==pytest.approx((100000-back)*100/100000)


@pytest.mark.parametrize('kw',[{'fees':(20,None,30,0)},{'fees':(20,5,30,1)},{'base':True},
                              {'real_quote':0},{'virtual_quote':-1},{'spend':1},{'spend':2**64}])
def test_unproved_fees_dust_and_invalid_units_have_no_probe(kw):
    args=dict(base=1000000,real_quote=1000000,virtual_quote=0,fees=(20,5,30,0),spend=100000);args.update(kw)
    with pytest.raises(ValueError):E.round_trip(**args)


def test_pending_capture_gap_and_unproven_tip_virtual_quote_refuse(tmp_path):
    desk,c=fixture(tmp_path)
    beat=json.loads((desk/'universe.heartbeat').read_text());beat['ps_gap_pending']=True
    (desk/'universe.heartbeat').write_text(json.dumps(beat))
    assert evidence(desk,c)['checks']['market_data']['status']=='unknown'


def test_newer_unproven_buy_cannot_reuse_an_older_virtual_quote(tmp_path):
    desk,c=fixture(tmp_path)
    conn=sqlite3.connect(desk/'pumpswap/tape.db')
    conn.execute("UPDATE swaps_ps SET payload_json='{}'");conn.commit();conn.close()
    assert evidence(desk,c)['checks']['sell_probe']['status']=='unknown'


def test_exact_base_buy_never_overspends_rounded_fee_budget():
    result=E.round_trip(base=10**14,real_quote=40*10**9,virtual_quote=0,fees=(20,5,5,0),spend=250000000)
    assert int(result['quote_spent_atomic'])+int(result['cash_left_atomic'])==250000000
    assert int(result['quote_spent_atomic'])<=250000000


def test_missing_sql_column_is_unknown_not_an_uncaught_read_error(tmp_path):
    desk,c=fixture(tmp_path)
    conn=sqlite3.connect(desk/'pumpswap/tape.db');conn.execute('ALTER TABLE swaps_ps RENAME COLUMN payload_json TO unavailable_payload');conn.commit();conn.close()
    assert evidence(desk,c)['checks']['sell_probe']['status']=='unknown'


def test_malformed_heartbeat_shape_is_typed_unknown(tmp_path):
    desk,c=fixture(tmp_path);(desk/'universe.heartbeat').write_text('[]')
    assert evidence(desk,c)['checks']['market_data']['status']=='unknown'


def test_frozen_native_buy_is_not_overstated_by_the_budget_bound():
    from pathlib import Path
    from sniper.execution import pumpswap_buy as P
    fixture=json.loads((Path(__file__).parent/'fixtures/pumpswap_buy_frames.json').read_text())
    event=P.parse_buy_event(fixture['direct_buy_exact_quote_in']['buy_event_log'])
    vq=P.parse_pool_account(fixture['pool_account_b64'])['virtual_quote']
    fees=tuple(event[k] for k in ('lp_fee_basis_points','protocol_fee_basis_points','coin_creator_fee_basis_points'))+(0,)
    result=E.round_trip(base=event['pool_base_token_reserves'],real_quote=event['pool_quote_token_reserves'],virtual_quote=vq,fees=fees,spend=50_000_000)
    assert int(result['tokens_atomic'])<=event['base_amount_out']
    assert int(result['post_buy_quote_atomic'])<=P.post_trade_reserves(event)[1]
    assert int(result['quote_spent_atomic'])+int(result['cash_left_atomic'])==50_000_000


def test_unknown_same_slot_order_is_not_hidden_by_a_good_tip(tmp_path):
    desk,c=fixture(tmp_path)
    conn=sqlite3.connect(desk/'pumpswap/tape.db')
    conn.execute('CREATE TEMP TABLE clone AS SELECT * FROM swaps_ps')
    conn.execute("UPDATE clone SET signature='unknown_order',tx_index=NULL,ordinal=99")
    conn.execute('INSERT INTO swaps_ps SELECT * FROM clone');conn.commit();conn.close()
    assert evidence(desk,c)['checks']['sell_probe']['status']=='unknown'


def test_probe_and_candidate_cannot_use_different_fx(tmp_path):
    desk,c=fixture(tmp_path)
    assert evidence(desk,c,sol_usd=110)['checks']['sell_probe']['status']=='unknown'


def test_pool_map_and_source_fact_cannot_be_future_proof(tmp_path):
    desk,c=fixture(tmp_path)
    c['snapshot']['fields']['pool_state']['value']['map_known_at']=NOW-1
    assert evidence(desk,c)['checks']['market_data']['status']=='unknown'


def test_float_loss_never_rounds_below_the_exact_atomic_cost_bound():
    from fractions import Fraction
    for spend in range(100000,100010):
        r=E.round_trip(base=10**12,real_quote=10**10,virtual_quote=0,fees=(20,5,30,0),spend=spend)
        exact=Fraction((spend-int(r['quote_back_atomic']))*100,spend)
        assert Fraction.from_float(r['loss_pct'])>=exact


def test_zero_fees_are_known_and_token2022_metadata_is_benign(tmp_path):
    result=E.round_trip(base=10**8,real_quote=10**9,virtual_quote=0,fees=(0,0,0,0),spend=1000000)
    assert result['loss_pct']>=0
    desk,c=fixture(tmp_path)
    account=mint(owner=E.TOKEN_2022_PROGRAM,extensions=struct.pack('<HH',18,64)+bytes(64))
    assert evidence(desk,c,mint_account=account)['checks']['token_safety']['status']=='pass'


def test_intake_print_itself_is_usable_after_its_commit_boundary(tmp_path):
    desk,c=fixture(tmp_path)
    at=c['snapshot']['fields']['pool_state']['value']['reserve_known_at']
    c['snapshot']['fields']['pool_state']['known_at']=at
    out=evidence(desk,c,as_of_host=at,sol_at=at-2)
    assert out['checks']['sell_probe']['status']=='pass'


METADATA_KEYS=('supply_atomic','base_decimals','metadata_known_at','metadata_slot','metadata_source')


def partial_intake(tmp_path,*,derived=False,absent=True):
    desk,c=fixture(tmp_path)
    c['intake']={'feed':'pumpfun','filter':'active','provider_rank':1,'rank_kind':'list_position',
                 'observed_at':NOW-1,'host_received_at':NOW-1,'clock_offset_s':0,'intake_row_ref':'pumpfun:day:1'}
    row=c['snapshot']['fields']['pool_state'];row['completeness']='partial'
    for name in METADATA_KEYS:
        if absent:row['value'].pop(name)
        else:row['value'][name]=None
    (desk/'pumpswap/metadata.db').unlink()
    if derived:
        row['value']['map_source']='intake_canonical_pda'
        row['value']['map_known_at']=row['value']['reserve_known_at']
        conn=sqlite3.connect(desk/'pumpswap/tape.db')
        conn.execute('DELETE FROM pools')
        conn.execute("UPDATE swaps_ps SET map_status='unmapped',map_known_at=NULL,mint=NULL,quote_mint=NULL")
        conn.commit();conn.close()
    return desk,c


@pytest.mark.parametrize('absent',[True,False])
def test_f3_intake_without_metadata_db_uses_only_fresh_mint_proof(tmp_path,absent):
    desk,c=partial_intake(tmp_path,absent=absent)
    checks=evidence(desk,c)['checks']
    assert checks['market_data']['status']=='pass' and checks['sell_probe']['status']=='pass'
    assert checks['token_safety']['supply_atomic']==str(10**15)


def test_f3_does_not_relax_band_or_mandatory_mint_read(tmp_path):
    desk,c=partial_intake(tmp_path)
    assert evidence(desk,c,mint_account=None)['checks']['market_data']['status']=='unknown'
    del c['intake']
    assert evidence(desk,c)['checks']['market_data']['status']=='unknown'


@pytest.mark.parametrize('name',['base_reserve_atomic','quote_reserve_atomic','quote_mint','virtual_quote_atomic',
                                'lp_fee_bps','protocol_fee_bps','creator_fee_bps','slot','map_known_at',
                                'reserve_known_at','virtual_quote_known_at','virtual_quote_slot','virtual_quote_source'])
def test_f3_missing_nonmetadata_proof_still_refuses(tmp_path,name):
    desk,c=partial_intake(tmp_path);c['snapshot']['fields']['pool_state']['value'].pop(name)
    assert evidence(desk,c)['checks']['market_data']['status']=='unknown'


def test_f2_no_create_pool_row_can_use_only_the_labelled_exact_canonical_pda(tmp_path):
    desk,c=partial_intake(tmp_path,derived=True)
    assert evidence(desk,c)['checks']['sell_probe']['status']=='pass'
    c['snapshot']['fields']['pool_state']['value'].pop('map_source')
    assert evidence(desk,c)['checks']['market_data']['status']=='unknown'


def test_f2_derived_binding_never_overrides_a_captured_mapping(tmp_path):
    desk,c=partial_intake(tmp_path,derived=True)
    conn=sqlite3.connect(desk/'pumpswap/tape.db')
    from test_desk_ps_asof import mapping
    m=mapping()
    conn.execute('INSERT INTO pools VALUES('+','.join('?'*19)+')',(m['pool'],m['mint'],m['quote_mint'],m['creator'],m['canonical'],m['slot'],m['signature'],m['ordinal'],m['tx_index'],0,0,m['received_at'],m['known_at'],m['known_at'],'{}','','solana','pumpswap','p'))
    conn.commit();conn.close()
    assert evidence(desk,c)['checks']['market_data']['status']=='unknown'


def test_f3_partial_metadata_never_hides_conflicting_mint_facts_or_bad_intake(tmp_path):
    desk,c=partial_intake(tmp_path)
    state=c['snapshot']['fields']['pool_state']['value']
    state['supply_atomic']='1'
    assert evidence(desk,c)['checks']['market_data']['status']=='unknown'
    state.pop('supply_atomic')
    c['intake']['feed']=[]
    assert evidence(desk,c)['checks']['market_data']['status']=='unknown'


# ---- cohort D Amendment 23: curve-derived pool binding in the hard-filter tip proof
def curve_derived(tmp_path, *, keep_pools=False, grad_mint=None, tripwire=False):
    desk, c = fixture(tmp_path)
    conn = sqlite3.connect(desk / 'pumpswap/tape.db')
    if not keep_pools:
        conn.execute('DELETE FROM pools')
    conn.execute("UPDATE swaps_ps SET map_status='mapped_curve_derived'")
    conn.execute('CREATE TABLE IF NOT EXISTS ps_grads(pool TEXT, mint TEXT, quote_mint TEXT, kind TEXT, slot INTEGER, '
                 'signature TEXT, ordinal INTEGER, tx_index INTEGER, block_time REAL, received_at REAL, known_at REAL, '
                 'curve_quote_mint TEXT, curve_real_quote TEXT, grad_class TEXT)')
    slot, received = conn.execute('SELECT MIN(slot), MIN(received_at) FROM swaps_ps').fetchone()
    conn.execute('INSERT INTO ps_grads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (POOL, grad_mint or MINT, A.T.WSOL_MINT if hasattr(A, 'T') else E.PT.WSOL_MINT, 'complete', slot - 1,
                  'g', 0, 1, None, received - 5, received - 5, None, None, None))
    if tripwire:      # another pool whose create_pool disagrees with its curve-derived binding
        conn.execute('INSERT INTO ps_grads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     ('OtherPool111', 'MintA', E.PT.WSOL_MINT, 'complete', 1, 's', 0, 1, None, 1.0, 1.0, None, None, None))
        conn.execute('INSERT INTO pools(pool,mint,quote_mint,canonical,slot,signature,ordinal,tx_index,received_at,known_at,'
                     'network_id,venue) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     ('OtherPool111', 'MintB', E.PT.WSOL_MINT, 1, 1, 's2', 0, 1, 1.0, 1.0, 'solana', 'pumpswap'))
    conn.commit(); conn.close()
    return desk, c


def test_am23_a_curve_derived_tip_passes_labelled_unchecked(tmp_path):
    desk, c = curve_derived(tmp_path)
    md = evidence(desk, c)['checks']['market_data']
    assert md == {'status': 'pass', 'reason': 'ps_tape_curve_derived_unchecked'}


def test_am23_an_agreeing_create_pool_makes_it_checked(tmp_path):
    desk, c = curve_derived(tmp_path, keep_pools=True)
    assert evidence(desk, c)['checks']['market_data']['reason'] == 'ps_tape_curve_derived_checked'


def test_am23_a_grad_on_another_mint_never_binds(tmp_path):
    desk, c = curve_derived(tmp_path, grad_mint='OtherMint1111')
    assert evidence(desk, c)['checks']['market_data']['status'] == 'unknown'


def test_am23_tripwire_a_disagreeing_create_pool_anywhere_stops_unchecked(tmp_path):
    desk, c = curve_derived(tmp_path, tripwire=True)
    md = evidence(desk, c)['checks']['market_data']
    assert md == {'status': 'unknown', 'reason': 'curve_binding_tripwire'}


def test_am23_a_create_pool_tip_keeps_its_plain_reason(tmp_path):
    desk, c = fixture(tmp_path)
    assert evidence(desk, c)['checks']['market_data'] == {'status': 'pass', 'reason': 'ps_tape'}
