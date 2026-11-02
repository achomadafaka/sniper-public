"""An Office entry on a sell tip carries the quote from the exact proving buy; nothing else relaxes."""
import sqlite3

import pytest

from sniper.office import ledger as L
from sniper.services import memecoin_desk_ps_paper as P
from test_desk_ps_asof import NOW, MINT
from test_desk_ps_loss_integration import provision
from test_memecoin_desk_ps_evidence import fixture

SPEND=250_000_000


@pytest.fixture
def desk(tmp_path,monkeypatch):
    monkeypatch.delenv('OFFICE_ENTRY_CARRIED',raising=False)
    d,_=fixture(tmp_path);return provision(d)


def sell_tip(desk,at=NOW-1,proved_age=None,mint=None):
    """Copies the newest proving buy as a later sell; optionally ages or rebinds that buy."""
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:
        c.row_factory=sqlite3.Row
        row=dict(c.execute('SELECT * FROM swaps_ps ORDER BY slot DESC,tx_index DESC,ordinal DESC LIMIT 1').fetchone())
        buy=(row['signature'],row['ordinal'])
        if proved_age is not None:c.execute('UPDATE swaps_ps SET received_at=? WHERE is_buy=1',(NOW-proved_age,))
        if mint:c.execute('UPDATE swaps_ps SET mint=? WHERE signature=? AND ordinal=?',(mint,*buy))
        row.update(signature='sell-tip',ordinal=row['ordinal']+1,tx_index=row['tx_index']+1,is_buy=0,payload_json='{}',received_at=at)
        c.execute('INSERT INTO swaps_ps(%s) VALUES(%s)'%(','.join(row),','.join('?'*len(row))),tuple(row.values()))
    return buy


def entry(desk):
    return P.entry_buy(desk,MINT,SPEND,NOW-.5,evaluated_at=NOW+.1,integrity=True)


def test_a_buy_tip_stays_proven_and_labelled_buy(desk):
    out,why=entry(desk)
    assert why is None and out['binding_kind']!='carried' and out['proof']['tip_side']=='buy'
    assert 'quote_source_signature' not in out['proof']


def test_a_sell_tip_fills_carried_with_source_row_and_both_ages(desk):
    sig,ordinal=sell_tip(desk)
    out,why=entry(desk)
    assert why is None and out['binding_kind']=='carried' and int(out['tokens_atomic'])>0
    p=out['proof']
    assert p['tip_side']=='sell' and p['signature']=='sell-tip'
    assert (p['quote_source_signature'],p['quote_source_ordinal'])==(sig,ordinal) and p['quote_source_slot']==p['slot']
    assert p['carried_age_at_observation_s']>=p['carried_age_s']>0 and p['newest_swap_age_at_observation_s']>=p['newest_swap_age_s']>0


def test_other_callers_and_the_kill_switch_stay_strict(desk,monkeypatch):
    sell_tip(desk)
    assert P.buy(desk,MINT,SPEND,NOW-.5,evaluated_at=NOW+.1,integrity=True)==(None,'pool_tip_unproven')
    monkeypatch.setenv('OFFICE_ENTRY_CARRIED','0')
    assert entry(desk)==(None,'pool_tip_unproven')


def test_a_quote_past_the_entry_bound_is_refused_typed(desk):
    sell_tip(desk,proved_age=120.2)
    assert entry(desk)==(None,'entry_carry_stale')


def test_the_carried_row_must_be_this_coin(desk):
    sell_tip(desk,mint='another')
    assert entry(desk)==(None,'pool_binding_unknown')


def test_a_capture_gap_inside_the_carried_span_is_refused(desk):
    sell_tip(desk,proved_age=30)
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:c.execute("INSERT INTO gaps_ps VALUES(?,'disconnect',10)",(NOW-20,))
    assert entry(desk)==(None,'entry_carry_span_gap')


def test_a_gap_covering_the_tip_keeps_the_transient_reason(desk):
    sell_tip(desk,at=NOW-5,proved_age=30)
    with sqlite3.connect(desk/'pumpswap'/'tape.db') as c:c.execute("INSERT INTO gaps_ps VALUES(?,'disconnect',10)",(NOW-2,))
    why=entry(desk)[1]
    assert why in ('ps_capture_unproven','capture_gap') and why in L.TRANSIENT_FILL


def test_exit_sells_never_carry(desk):
    sell_tip(desk)
    assert P.sell(desk,MINT,1000,NOW-.5,evaluated_at=NOW+.1,integrity=True)==(None,'pool_tip_unproven')
