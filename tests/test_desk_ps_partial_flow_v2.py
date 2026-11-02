"""v28 sees observations without mistaking incomplete capture for complete flow."""
import json
import sqlite3

import pytest

from sniper.services import memecoin_desk_ps_facts as F
from sniper.services import memecoin_desk_ps_losses as L
from test_desk_ps_model_facts import tape,facts,NOW,CUT


def test_partial_flow_and_chart_keep_seen_trades_and_coverage(tmp_path):
    c,path=tape(tmp_path)
    with sqlite3.connect(path) as db:
        for at in (NOW-50,NOW-30,NOW-7):
            db.execute("INSERT INTO gaps_ps VALUES(?,'disconnect',0)",(at,))
    out=facts(c,tmp_path)
    flow=out['flow_60s']
    assert out['version']=='pumpswap-facts-v2'
    assert flow is not None and flow['buy_usd']==80 and flow['sell_usd']==20
    assert flow['buyers']==2 and flow['sellers']==1 and flow['n_swaps']==3
    assert flow['partial'] is True and flow['covered_s']==pytest.approx(7)
    assert flow['loss_classes_exclusive'] is True
    assert (flow['unknown_loss_receipts'],flow['global_loss_receipts'],flow['own_pool_losses'])==(0,3,0)
    structure=out['structure']
    assert structure['windows']['60s']['n_swaps']==3
    assert structure['windows']['60s']['partial'] is True
    assert len(structure['chart'])==len(structure['chart_coverage'])==24
    assert structure['chart'][0][5:]==[None,None,None]
    assert structure['chart_coverage'][-1]=={'covered_s':5.0,'partial':False}
    assert out['price_vs_60s_low_pct'] is None


def test_loss_details_count_receipt_identity_not_distinct_time_and_reuse(tmp_path):
    c,path=tape(tmp_path)
    with sqlite3.connect(path) as db:
        for sig in ('unknown-a','unknown-b'):
            db.execute("INSERT INTO ps_decode_losses VALUES(?,'decode_loss',?,?)",(sig,NOW-7,CUT-1))
    details=[];memo={}
    L.losses(tmp_path,c['pool'],NOW-60,NOW,cut_slot=CUT,loss_reads=memo,receipt_details=details)
    again=[]
    L.losses(tmp_path,c['pool'],NOW-60,NOW,cut_slot=CUT,loss_reads=memo,receipt_details=again)
    assert again==details and len(details)==2
    flow=facts(c,tmp_path)['flow_60s']
    assert flow['unknown_loss_receipts']==2 and flow['global_loss_receipts']==0


@pytest.mark.parametrize('coverage',[0,None])
def test_zero_covered_seconds_preserves_seen_totals(tmp_path,coverage):
    c,_=tape(tmp_path,coverage=coverage)
    flow=facts(c,tmp_path)['flow_60s']
    assert flow is not None and flow['covered_s']==0 and flow['lower_bound'] is True
    assert flow['buy_usd']==80 and flow['partial'] is True


def test_no_seen_swaps_never_zero_fills_even_with_positive_coverage(tmp_path):
    c,_=tape(tmp_path,rows=[],coverage=10)
    assert facts(c,tmp_path)['flow_60s'] is None


def test_classes_are_exclusive_and_attested_wrappers_never_double_count(tmp_path):
    from test_desk_ps_loss_pool_budget import add_losses
    c,path=tape(tmp_path)
    add_losses(path,[c['pool']],count=1)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO ps_decode_losses VALUES('u','decode_loss',?,50)",(NOW-8,))
        db.execute("INSERT INTO gaps_ps VALUES(?,'disconnect',0)",(NOW-9,))
    flow=facts(c,tmp_path)['flow_60s']
    assert (flow['own_pool_losses'],flow['unknown_loss_receipts'],flow['global_loss_receipts'])==(1,1,1)
    assert flow['loss_classes_exclusive'] is True


@pytest.mark.parametrize('field,value',[
    ('partial',False),('covered_s',61),('unknown_loss_receipts',True),
    ('loss_classes_exclusive',False),('lower_bound',False),('n_swaps',999)])
def test_v2_flow_validator_rejects_false_coverage_or_counter_claims(tmp_path,field,value):
    c,_=tape(tmp_path,coverage=10)
    out=facts(c,tmp_path)
    out['flow_60s'][field]=value
    with pytest.raises(ValueError,match='ps_facts'):
        F.validate(out)


def test_bar_coverage_validator_rejects_inconsistent_partial_flag(tmp_path):
    c,_=tape(tmp_path,coverage=10)
    out=facts(c,tmp_path)
    out['structure']['chart_coverage'][0]['partial']=False
    with pytest.raises(ValueError,match='ps_facts'):
        F.validate(out)

def test_inferred_pool_discontinuity_also_shortens_complete_suffix(tmp_path):
    c,path=tape(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE swaps_ps SET pre_quote_reserve_atomic='25000000000' WHERE signature='fixture5'")
    flow=facts(c,tmp_path)['flow_60s']
    assert flow is not None and flow['partial'] is True
    assert flow['covered_s']==pytest.approx(5)
    # The discontinuity is not fabricated into a retained decoder/source receipt.
    assert flow['own_pool_losses']==flow['global_loss_receipts']==flow['unknown_loss_receipts']==0

def test_disjoint_capture_segments_do_not_sum_into_complete_suffix(tmp_path):
    c,path=tape(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM ps_capture_intervals')
        db.execute("INSERT INTO ps_capture_intervals VALUES('fixture',?,?,1)",(NOW-60,NOW-10))
        db.execute("INSERT INTO ps_capture_intervals VALUES('fixture',?,?,1)",(NOW-5,NOW))
    assert facts(c,tmp_path)['flow_60s']['covered_s']==5

def test_seen_zero_coverage_rejects_boolean_covered_seconds(tmp_path):
    c,_=tape(tmp_path,coverage=None)
    out=facts(c,tmp_path)
    out['flow_60s']['covered_s']=False
    with pytest.raises(ValueError,match='ps_facts'):
        F.validate(out)

def test_chart_no_seen_swap_is_null_even_when_capture_is_complete(tmp_path):
    c,_=tape(tmp_path)
    out=facts(c,tmp_path)
    assert out['structure']['chart_coverage'][0]=={'covered_s':5.0,'partial':False}
    assert out['structure']['chart'][0][5:]==[None,None,None]

@pytest.mark.parametrize('explicit',[False,True])
def test_v1_default_preserves_original_null_and_chart_shape(tmp_path,explicit):
    from test_desk_ps_model_facts import NOW,CUT
    c,_=tape(tmp_path,coverage=10)
    kw={'version':'pumpswap-facts-v1'} if explicit else {}
    out=F.facts_for(c,desk_dir=tmp_path,as_of=NOW,receipt_cut=NOW,cut_slot=CUT,ours=frozenset(('ours',)),**kw)
    assert out['version']=='pumpswap-facts-v1' and out['flow_60s'] is None
    assert 'chart_coverage' not in out['structure']


def test_unknown_facts_version_refuses_before_reading(tmp_path):
    c,_=tape(tmp_path)
    with pytest.raises(ValueError,match='ps_facts_candidate'):
        F.facts_for(c,desk_dir=tmp_path,as_of=NOW,receipt_cut=NOW,cut_slot=CUT,version='guessed')
