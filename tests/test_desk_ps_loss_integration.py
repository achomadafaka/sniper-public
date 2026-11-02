"""New policy loss proof reaches reserve refresh and hard-filter evidence; legacy stays frozen."""
import json
import sqlite3

import pytest

from sniper.services import memecoin_desk_asof as AS
from sniper.services import memecoin_desk_ps_asof as A
from sniper.services import memecoin_desk_ps_evidence as E
from sniper.services import memecoin_desk_ps_losses as L
from sniper.services.memecoin_desk_asof import StaleSnapshot
from test_desk_ps_asof import build, candidate, NOW, POOL, MINT
from test_desk_ps_loss_scope import receipt, batch
from test_memecoin_desk_intake_assembler_runner import run  # noqa: F401


def provision(desk):
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        c.execute('CREATE TABLE IF NOT EXISTS ps_decode_losses(signature TEXT,cause TEXT,slot INTEGER,received_at REAL, '
                  'PRIMARY KEY(signature,cause)) WITHOUT ROWID')
        c.execute('CREATE INDEX IF NOT EXISTS ps_decode_losses_received ON ps_decode_losses(received_at)')
        c.execute('CREATE INDEX IF NOT EXISTS gaps_ps_at ON gaps_ps(at)')
        for ddl in L.SCHEMAS.values(): c.execute(ddl)
        for ddls in L.INDEXES.values():
            for ddl in ddls:c.execute(ddl)
    return desk


def add(desk, *, pools, at=NOW-5):
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        receipt(c, at=at, pools=pools); batch(c, at=at)


def test_new_asof_honors_proven_other_pool_but_legacy_stays_conservative(tmp_path):
    desk=provision(build(tmp_path)); add(desk,pools=[MINT])
    with pytest.raises(StaleSnapshot, match='tape_gap_since_cut'):
        A.refresh(candidate(),desk_dir=desk,now=NOW)
    out,meta=A.refresh(candidate(),desk_dir=desk,now=NOW,integrity=True)
    assert out['pool']==POOL and meta['version']=='psasof-v2'
    out2,_=AS.refresh(candidate(),desk_dir=desk,now=NOW,integrity=True)
    assert out2['snapshot']['hash']==out['snapshot']['hash']


@pytest.mark.parametrize('scope', [[POOL], [POOL,MINT], None])
def test_new_asof_refuses_every_same_or_unknown_individual_loss(tmp_path,scope):
    desk=provision(build(tmp_path)); add(desk,pools=scope or [])
    if scope is None:
        with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:c.execute("UPDATE ps_loss_attribution SET scope='unknown'")
    with pytest.raises(StaleSnapshot, match='tape_gap_since_cut'):
        A.refresh(candidate(),desk_dir=desk,now=NOW,integrity=True)


def test_new_asof_catches_second_loss_after_earlier_coalesced_marker(tmp_path):
    desk=provision(build(tmp_path))
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        receipt(c,at=NOW-11,pools=[MINT]);receipt(c,'two',NOW-5,[POOL]);batch(c,('one','two'),at=NOW-11)
    with pytest.raises(StaleSnapshot,match='tape_gap_since_cut'):
        A.refresh(candidate(),desk_dir=desk,now=NOW,integrity=True)


def test_new_asof_missing_loss_index_is_typed_refusal(tmp_path):
    desk=provision(build(tmp_path))
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:c.execute('DROP INDEX ps_decode_losses_received')
    with pytest.raises(StaleSnapshot,match='ps_loss_unreadable'):
        A.refresh(candidate(),desk_dir=desk,now=NOW,integrity=True)


def test_new_asof_pending_after_claim_does_not_rewrite_the_past(tmp_path):
    desk=provision(build(tmp_path,heartbeat={'ps_gap_pending':True,'ps_pending_gaps':{'decode_loss':NOW+1}}))
    A.refresh(candidate(),desk_dir=desk,now=NOW,integrity=True)
    beat=json.loads((desk/'universe.heartbeat').read_text());beat['ps_pending_gaps']['decode_loss']=NOW-1
    (desk/'universe.heartbeat').write_text(json.dumps(beat))
    with pytest.raises(StaleSnapshot,match='tape_gap_since_cut'):
        A.refresh(candidate(),desk_dir=desk,now=NOW,integrity=True)


def test_strict_hard_filter_uses_same_pool_loss_proof(tmp_path):
    from test_memecoin_desk_ps_evidence import fixture,evidence
    desk,cand=fixture(tmp_path);provision(desk)
    add(desk,pools=[MINT])
    good=evidence(desk,cand,integrity=True)['checks']
    assert good['market_data']['status']=='pass' and good['sell_probe']['status']=='pass'
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        c.execute('UPDATE ps_loss_attribution SET pools_json=?',(json.dumps([POOL]),))
    bad=evidence(desk,cand,integrity=True)['checks']
    assert bad['market_data']['status']=='unknown' and bad['sell_probe']['reason']=='pool_tape_gap'


def test_strict_hard_filter_cannot_borrow_a_future_slot_state(tmp_path):
    from test_memecoin_desk_ps_evidence import fixture,evidence
    desk,cand=fixture(tmp_path);provision(desk)
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        c.execute('UPDATE swaps_ps SET slot=999')
    bad=evidence(desk,cand,integrity=True)['checks']
    assert bad['market_data']['status']=='unknown'


@pytest.mark.parametrize('adapter',['capture','buy','sell','risk'])
@pytest.mark.parametrize('problem',['other','same','global','missing_index','second','malformed'])
def test_strict_paper_capture_and_risk_adapters_share_loss_rules(tmp_path,adapter,problem):
    from test_memecoin_desk_ps_evidence import fixture
    from sniper.services import memecoin_desk_capture as C
    from sniper.services import memecoin_desk_ps_paper as P
    from sniper.services import memecoin_desk_risk_tape as R
    desk,_=fixture(tmp_path);provision(desk)
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        if problem=='global':c.execute("INSERT INTO gaps_ps VALUES(?,'disconnect',10)",(NOW-5,))
        elif problem=='missing_index':c.execute('DROP INDEX ps_decode_losses_received')
        elif problem=='malformed':
            receipt(c,at=NOW-5,pools=[MINT]);batch(c,at=NOW-5)
            c.execute("UPDATE ps_loss_attribution SET pools_json='broken'")
        elif problem=='second':
            receipt(c,at=NOW-12,pools=[MINT]);receipt(c,'two',NOW-5,[POOL]);batch(c,('one','two'),at=NOW-12)
        else:
            receipt(c,at=NOW-5,pools=[MINT] if problem=='other' else [POOL]);batch(c,at=NOW-5)
    allowed=problem=='other'
    if adapter=='capture':
        assert (C.capture_proof(desk,NOW+.1,venues=('ps',),window_s=20,pool=POOL,integrity=True) is not None)==allowed
    elif adapter in ('buy','sell'):
        got,why=getattr(P,adapter)(desk,MINT,1000 if adapter=='sell' else 250_000_000,NOW-.5,evaluated_at=NOW+.1,integrity=True)
        assert (got is not None)==allowed,(got,why)
    elif allowed:
        rows,gaps=R._ps(desk,MINT,POOL,NOW-20,NOW-.5,100,base_decimals=6,integrity=True)
        assert rows and not gaps
    elif problem in ('missing_index','malformed'):
        with pytest.raises(R.TapeUnreadable):R._ps(desk,MINT,POOL,NOW-20,NOW-.5,100,base_decimals=6,integrity=True)
    else:
        rows,gaps=R._ps(desk,MINT,POOL,NOW-20,NOW-.5,100,base_decimals=6,integrity=True)
        assert gaps


def test_risk_new_policy_uses_explicit_decimals_and_pool_capture_flag(tmp_path,monkeypatch):
    from test_memecoin_desk_ps_evidence import fixture
    from sniper.services import memecoin_desk_risk_tape as R
    from sniper.services import memecoin_desk_ps_tape as PT
    from datetime import datetime,timezone
    from sniper.util import clock
    desk,_=fixture(tmp_path);provision(desk)
    monkeypatch.setattr(clock,'offset_s',lambda:0.)
    with sqlite3.connect(desk/'blocks'/'2027-01-15.db') as c:
        c.execute('UPDATE blocks SET received_at=?',(NOW-3,))
    observed=[];load=PT.load_ps_swaps
    def reader(*a,**kw):observed.append(kw);return load(*a,**kw)
    monkeypatch.setattr(PT,'load_ps_swaps',reader)
    proofs=[]
    def proof(*a,**kw):proofs.append(kw);return {'from':NOW-600,'through':NOW-.5}
    pos={'id':1,'mint':MINT,'opened_at':datetime.fromtimestamp(NOW-100,timezone.utc).isoformat(),
         'entry_price':1,'desk_ps_inputs':'pumpswap-facts-v1','desk_base_decimals':None}
    R.evidence_of(pos,desk_dir=desk,now=NOW,sol_usd=100,fill_sigs_of=lambda _:('ours',),sb=0,proof_fn=proof)
    assert any(p.get('integrity') is True and p.get('pool')==POOL and p.get('window_s')==0 for p in proofs)
    assert observed and observed[0]['integrity'] is True and observed[0]['base_decimals'] is None
    assert observed[0]['cut_slot']==21


def test_strict_reserve_and_paper_prices_refuse_missing_swap_index(tmp_path):
    from test_memecoin_desk_ps_evidence import fixture,evidence
    from sniper.services import memecoin_desk_ps_paper as P
    desk,cand=fixture(tmp_path);provision(desk)
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:c.execute('DROP INDEX swaps_ps_pool_slot')
    with pytest.raises(StaleSnapshot,match='ps_tape_index_unknown'):
        A.refresh(candidate(),desk_dir=desk,now=NOW,integrity=True)
    assert P.buy(desk,MINT,250_000_000,NOW-.5,evaluated_at=NOW+.1,integrity=True)[0] is None
    assert evidence(desk,cand,integrity=True)['checks']['market_data']['status']=='unknown'


def test_original_producer_print_uses_actual_late_block_context(run):
    from test_memecoin_desk_ps_evidence import mint
    desk=provision(run['desk']);cand=run['cand']
    state=cand['snapshot']['fields']['pool_state']['value']
    at=cand['snapshot']['emission_at'];evaluated=run['clock']()   # Am36: the cut is the batch end, after the receipt
    assert 'cut_slot' not in state
    result=E.pumpswap_evidence(cand,desk_dir=desk,as_of_host=at,clip_usd=25,sol_usd=100,
                               sol_at=at-state['sol_age_s'],mint_account=mint(),mint_known_at=evaluated,
                               evaluated_at_host=evaluated,integrity=True)
    assert result['checks']['market_data']['status']=='pass',result
    assert result['checks']['sell_probe']['status']=='pass'
    assert result['market_cut']['receipt_cut']==at and result['market_cut']['cut_slot']==21
    assert at<result['market_cut']['block_known_at']<=evaluated
    assert E.K._utc(result['known_at'])>=result['market_cut']['block_known_at']
    # Later context validates the old print; later swaps never become part of that frozen price.
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        c.execute('CREATE TEMP TABLE future AS SELECT * FROM swaps_ps')
        c.execute("UPDATE future SET signature='later',slot=19,received_at=?,post_quote_reserve_atomic='999'",(at+.5,))
        c.execute('INSERT INTO swaps_ps SELECT * FROM future')
    later=E.pumpswap_evidence(cand,desk_dir=desk,as_of_host=at,clip_usd=25,sol_usd=100,
                              sol_at=at-state['sol_age_s'],mint_account=mint(),mint_known_at=evaluated,
                              evaluated_at_host=evaluated,integrity=True)
    assert later['checks']['sell_probe']==result['checks']['sell_probe']


@pytest.mark.parametrize('problem',['future','missing'])
def test_initial_print_missing_or_future_block_context_refuses(run,problem):
    from test_memecoin_desk_ps_evidence import mint
    desk=provision(run['desk']);cand=run['cand'];state=cand['snapshot']['fields']['pool_state']['value']
    with sqlite3.connect(desk/'blocks'/'2027-01-15.db') as c:
        if problem=='missing':c.execute('DELETE FROM blocks')
        else:c.execute('UPDATE blocks SET received_at=?',(run['clock']()+1,))
    result=E.pumpswap_evidence(cand,desk_dir=desk,as_of_host=state['reserve_known_at'],clip_usd=25,sol_usd=100,
                               sol_at=state['reserve_known_at']-state['sol_age_s'],mint_account=mint(),
                               mint_known_at=run['clock'](),evaluated_at_host=run['clock'](),integrity=True)
    assert result['checks']['market_data']['status']=='unknown'
