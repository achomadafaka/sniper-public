"""Frozen paper/HF prices stop dependency losses at the quote cut; health stays current."""
import json
import sqlite3

import pytest

from sniper.services import memecoin_desk_capture as C
from sniper.services import memecoin_desk_ps_paper as P
from test_desk_ps_asof import NOW, MINT, POOL
from test_memecoin_desk_ps_evidence import fixture, evidence
from test_desk_ps_loss_integration import provision
from test_desk_ps_loss_scope import receipt, batch
from test_memecoin_desk_intake_assembler_runner import run  # noqa: F401

CUT=NOW-.5
EVALUATED=NOW+.1


def invoke(adapter,desk,cand):
    if adapter=='evidence':
        return evidence(desk,cand,integrity=True)['checks']['sell_probe']['status']=='pass'
    got,_=getattr(P,adapter)(desk,MINT,250_000_000 if adapter=='buy' else 1000,
                            CUT,evaluated_at=EVALUATED,integrity=True)
    return got is not None


@pytest.mark.parametrize('adapter',['evidence','buy','sell'])
@pytest.mark.parametrize('kind',['individual','pending','global','legacy_span'])
@pytest.mark.parametrize('offset,allowed',[(-.2,False),(0.,False),(.2,True)])
def test_dependency_loss_before_equal_after_frozen_cut(tmp_path,adapter,kind,offset,allowed):
    desk,cand=fixture(tmp_path);provision(desk)
    at=CUT+offset
    if kind=='pending':
        path=desk/'universe.heartbeat';beat=json.loads(path.read_text())
        beat.update(ps_gap_pending=True,ps_pending_gaps={'decode_loss':at});path.write_text(json.dumps(beat))
    else:
        with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
            if kind=='individual':receipt(c,at=at,pools=[POOL]);batch(c,at=at)
            elif kind=='global':c.execute("INSERT INTO gaps_ps VALUES(?,'disconnect',18)",(at,))
            else:c.execute('INSERT INTO ps_gap_spans VALUES(?,NULL)',(at,))
    assert invoke(adapter,desk,cand)==allowed


@pytest.mark.parametrize('adapter',['evidence','buy','sell'])
@pytest.mark.parametrize('broken',['capture_missing','selection_missing','old_protocol','partial','missing_index'])
def test_post_cut_reconnect_needs_real_historical_capture_proof(tmp_path,adapter,broken):
    desk,cand=fixture(tmp_path);provision(desk)
    # Current stream recovered after the frozen quote. The old committed interval remains independently stored.
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        c.execute("INSERT INTO gaps_ps VALUES(?,'disconnect',18)",(CUT+.1,))
        c.execute('INSERT INTO ps_capture_intervals VALUES(?,?,?,?)',('old',NOW-600,CUT,1))
        c.execute('INSERT INTO ps_pool_selection VALUES(?,?,?,?,?)',(POOL,'old',NOW-600,CUT,1))
    path=desk/'universe.heartbeat';beat=json.loads(path.read_text())
    beat.update(ts=NOW,ps_capture_from=NOW-.1,ps_committed_through=NOW,ps_gap_pending=False)
    path.write_text(json.dumps(beat))
    assert invoke(adapter,desk,cand)
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        if broken=='capture_missing':c.execute('DELETE FROM ps_capture_intervals')
        elif broken=='selection_missing':c.execute('DELETE FROM ps_pool_selection')
        elif broken=='old_protocol':c.execute('UPDATE ps_capture_intervals SET protocol=0')
        elif broken=='partial':c.execute('UPDATE ps_capture_intervals SET through_at=?',(CUT-.01,))
        else:c.execute('DROP INDEX ps_pool_selection_pool_start')
    assert not invoke(adapter,desk,cand)


@pytest.mark.parametrize('adapter',['evidence','buy','sell'])
@pytest.mark.parametrize('change',[{'connected':False},{'ps_committed_through':NOW-10},{'ts':NOW+1}])
def test_later_loss_exception_never_waives_current_source_health(tmp_path,adapter,change):
    desk,cand=fixture(tmp_path);provision(desk)
    path=desk/'universe.heartbeat';beat=json.loads(path.read_text());beat.update(change);path.write_text(json.dumps(beat))
    assert not invoke(adapter,desk,cand)


@pytest.mark.parametrize('broken',['missing','partial','end_before','old_protocol','same_point_loss','missing_index','selection_future'])
def test_raw_producer_print_point_coverage_survives_later_reconnect(run,broken):
    import math
    from sniper.services import memecoin_desk_ps_evidence as E
    from test_memecoin_desk_ps_evidence import mint
    desk=provision(run['desk']);cand=run['cand']
    target=cand['snapshot']['emission_at']
    state=cand['snapshot']['fields']['pool_state']['value']
    assert state['reserve_known_at']<target<=state['reserve_known_at']+1e-6 and 'cut_slot' not in state   # Am36 batch end
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        c.execute("INSERT INTO gaps_ps VALUES(?,'disconnect',18)",(target+.1,))
        c.execute('INSERT INTO ps_capture_intervals VALUES(?,?,?,?)',('original',target-30,target,1))
        c.execute('INSERT INTO ps_pool_selection VALUES(?,?,?,?,?)',(POOL,'original',target-30,target,1))
    path=desk/'universe.heartbeat';beat=json.loads(path.read_text())
    beat.update(ts=NOW,ps_capture_from=target+.2,ps_committed_through=NOW,ps_gap_pending=False)
    path.write_text(json.dumps(beat))
    def current():
        return E.pumpswap_evidence(cand,desk_dir=desk,as_of_host=target,clip_usd=25,sol_usd=100,
                                  sol_at=target-state['sol_age_s'],mint_account=mint(),
                                  mint_known_at=run['clock'](),evaluated_at_host=run['clock'](),integrity=True)
    out=current()
    assert out['checks']['sell_probe']['status']=='pass',out
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        if broken=='missing':c.execute('DELETE FROM ps_capture_intervals')
        elif broken=='partial':c.execute('UPDATE ps_pool_selection SET end_at=?',(math.nextafter(target,-math.inf),))
        elif broken=='end_before':c.execute('UPDATE ps_capture_intervals SET through_at=?',(math.nextafter(target,-math.inf),))
        elif broken=='old_protocol':c.execute('UPDATE ps_capture_intervals SET protocol=0')
        elif broken=='missing_index':c.execute('DROP INDEX ps_pool_selection_pool_start')
        elif broken=='selection_future':c.execute('UPDATE ps_pool_selection SET start_at=?,end_at=NULL',(math.nextafter(target,math.inf),))
        else:receipt(c,at=target,pools=[POOL]);batch(c,at=target)
    assert current()['checks']['sell_probe']['status']=='unknown'
