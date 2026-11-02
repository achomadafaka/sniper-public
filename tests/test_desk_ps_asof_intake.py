"""Intake refresh reuses proven mint receipts without weakening the band path."""
import copy
import json
import sqlite3

import pytest

from sniper.services import memecoin_desk_ps_asof as A
from sniper.services.memecoin_desk_asof import StaleSnapshot
from test_memecoin_desk_ps_evidence import partial_intake as _partial_intake, fixture, evidence, NOW
from test_memecoin_desk_contract import rehash
from test_desk_ps_asof import MINT, POOL


def partial_intake(tmp_path, **kw):
    desk,c=_partial_intake(tmp_path,absent=False,**kw)
    at=c["snapshot"]["fields"]["pool_state"]["value"]["reserve_known_at"]
    c["emitted_at"]=A.datetime.fromtimestamp(at,A.timezone.utc).isoformat()
    c["snapshot"]["emission_at"]=at
    for row in c["snapshot"]["fields"].values():
        if row["known_at"] is not None: row["known_at"]=min(row["known_at"],at)
    c["snapshot"]["missing"]=sorted(k for k,v in c["snapshot"]["fields"].items() if v["value"] is None or v["completeness"]!="complete")
    rehash(c)
    return desk,c


def receipt(**changes):
    return dict(supply_atomic='1000000000000000',decimals=6,known_at=NOW-1,context_slot=21,
                authority_mint='closed',**changes)


def refresh(c,desk,r):
    return A.refresh(c,desk_dir=desk,now=NOW-.5,intake_mint=r)


def test_later_immutable_mint_receipt_completes_unmapped_intake_without_metadata_db(tmp_path):
    desk,c=partial_intake(tmp_path,derived=True);before=copy.deepcopy(c)
    out,meta=refresh(c,desk,receipt())
    state=out['snapshot']['fields']['pool_state']
    assert c==before and state['completeness']=='complete'
    assert state['value']['metadata_source']=='intake_mint_read'
    assert state['value']['metadata_known_at']==NOW-1 and state['value']['metadata_slot']==21
    assert state['value']['supply_atomic']=='1000000000000000'
    assert state['value']['base_decimals']==6
    assert state['value']['map_source']=='intake_canonical_pda'
    assert out['snapshot']['fields']['mcap_usd']['value']>0
    assert meta['supply_label']=='supply_at_read'


@pytest.mark.parametrize('authority',[None,'open','unknown'])
def test_mutable_or_unknown_authority_cannot_backfill_future_supply(tmp_path,authority):
    desk,c=partial_intake(tmp_path,derived=True);r=receipt();r['authority_mint']=authority
    with pytest.raises(StaleSnapshot,match='intake_mint_mutable'):
        refresh(c,desk,r)


@pytest.mark.parametrize('change',[{'decimals':True},{'decimals':19},{'supply_atomic':'01'},
                                  {'supply_atomic':'0'},{'context_slot':True},{'context_slot':1},
                                  {'known_at':float('nan')},{'known_at':NOW-200}])
def test_unproven_mint_units_context_or_clock_refuse(tmp_path,change):
    desk,c=partial_intake(tmp_path,derived=True);r=receipt();r.update(change)
    with pytest.raises(StaleSnapshot):
        refresh(c,desk,r)


def test_intake_receipt_required_and_existing_supply_conflicts_refuse(tmp_path):
    desk,c=partial_intake(tmp_path)
    with pytest.raises(StaleSnapshot,match='intake_mint_missing'):
        refresh(c,desk,None)
    c['snapshot']['fields']['pool_state']['value']['supply_atomic']='1'
    rehash(c)
    with pytest.raises(StaleSnapshot):
        refresh(c,desk,receipt())


def test_band_path_does_not_accept_intake_metadata_substitute(tmp_path):
    desk,c=fixture(tmp_path)
    (desk/'pumpswap/metadata.db').unlink()
    with pytest.raises(StaleSnapshot,match='metadata_not_yet_known'):
        refresh(c,desk,receipt())


def test_raw_mint_context_is_carried_for_judge_receipt_binding(tmp_path):
    desk,c=fixture(tmp_path)
    assert evidence(desk,c)['checks']['token_safety']['mint_context_slot']==21


@pytest.mark.parametrize('change',[
    'cashback_fee_bps=NULL','cashback_fee_bps=1','tx_index=NULL',
    'is_buy=0','post_quote_reserve_atomic=NULL', "payload_json='{}'",
    "quote_mint='wrong'",
])
def test_intake_mode_never_relaxes_native_tape_proof(tmp_path,change):
    desk,c=partial_intake(tmp_path,derived=True)
    conn=sqlite3.connect(desk/'pumpswap/tape.db')
    conn.execute('UPDATE swaps_ps SET '+change);conn.commit();conn.close()
    with pytest.raises(StaleSnapshot):
        refresh(c,desk,receipt())


def test_derived_binding_label_cannot_override_a_captured_pool(tmp_path):
    desk,c=partial_intake(tmp_path,derived=True)
    from test_desk_ps_asof import mapping
    m=mapping()
    conn=sqlite3.connect(desk/'pumpswap/tape.db')
    conn.execute('INSERT INTO pools VALUES('+','.join('?'*19)+')',(m['pool'],m['mint'],m['quote_mint'],m['creator'],m['canonical'],m['slot'],m['signature'],m['ordinal'],m['tx_index'],0,0,m['received_at'],m['known_at'],m['known_at'],'{}','','solana','pumpswap','p'))
    conn.commit();conn.close()
    with pytest.raises(StaleSnapshot):
        refresh(c,desk,receipt())


@pytest.mark.parametrize("known",[NOW-.5,NOW])
def test_mint_read_after_claim_is_not_available_at_claim(tmp_path,known):
    desk,c=partial_intake(tmp_path,derived=True);r=receipt();r["known_at"]=known
    with pytest.raises(StaleSnapshot,match="intake_mint_future"):
        refresh(c,desk,r)


def test_receipt_freshness_uses_the_configured_bound(tmp_path):
    desk,c=partial_intake(tmp_path,derived=True);r=receipt();r["known_at"]=NOW-2
    p=desk/"universe.heartbeat";beat=json.loads(p.read_text());beat["sol_age_s"]=0;p.write_text(json.dumps(beat))
    with pytest.raises(StaleSnapshot,match="intake_mint_unusable"):
        A.refresh(c,desk_dir=desk,now=NOW-.5,intake_mint=r,sol_max_age_s=1)


def test_receipt_before_the_original_print_is_not_its_mint_proof(tmp_path):
    desk,c=partial_intake(tmp_path,derived=True);r=receipt();r["known_at"]=NOW-11
    with pytest.raises(StaleSnapshot):
        refresh(c,desk,r)
