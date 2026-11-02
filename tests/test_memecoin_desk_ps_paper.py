"""PumpSwap paper pricing uses native atomic math and a frozen, committed host cut."""
import importlib
import importlib.util
import json
import sqlite3

import pytest

from test_memecoin_desk_ps_evidence import fixture, partial_intake
from test_desk_ps_asof import NOW, MINT, POOL
from sniper.services import memecoin_desk_ps_evidence as E


def module():
    assert importlib.util.find_spec('sniper.services.memecoin_desk_ps_paper') is not None, 'PS paper pricer missing'
    return importlib.import_module('sniper.services.memecoin_desk_ps_paper')


def quote(desk, *, side='buy', amount=250_000_000, at=NOW-.5, evaluated_at=NOW+.1):
    return getattr(module(),side)(desk,MINT,amount,at,evaluated_at=evaluated_at)


def test_buy_and_full_sell_are_exact_atomic_and_do_not_need_curve_or_metadata(tmp_path):
    desk,c=partial_intake(tmp_path,derived=True)
    state=c['snapshot']['fields']['pool_state']['value']
    got,why=quote(desk)
    assert why is None and int(got['tokens_atomic'])>0
    assert got['pool']==POOL and got['state_at']==state['reserve_known_at']
    assert int(got['spend_lamports'])<=250_000_000
    # 249252241 net + separately rounded fees 498505/124627/124627 = clip.
    assert got['tokens_atomic']=='619271730832' and got['spend_lamports']=='250000000'
    sold,why=quote(desk,side='sell',amount=int(got['tokens_atomic']))
    assert why is None and sold['lamports']=='245445588'
    assert sold['proof']['as_of_host']==NOW-.5


def test_commit_barrier_waits_without_reading_later_state(tmp_path):
    desk,c=fixture(tmp_path)
    assert quote(desk,at=NOW)[0] is None
    got,why=quote(desk,at=NOW-1)
    assert why is None and got['state_at']<=NOW-1


@pytest.mark.parametrize('change',[
    'cashback_fee_bps=NULL','cashback_fee_bps=1','lp_fee_bps=NULL','lp_fee_bps=10000',
    'is_buy=0','post_base_reserve_atomic=NULL','post_quote_reserve_atomic=NULL',
    "quote_mint='wrong'","mint='wrong'",'tx_index=NULL',
    "payload_json='{}'", "post_base_reserve_atomic='0'",
])
def test_missing_or_conflicting_native_proof_is_unknown(tmp_path,change):
    desk,c=fixture(tmp_path)
    conn=sqlite3.connect(desk/'pumpswap/tape.db');conn.execute('UPDATE swaps_ps SET '+change);conn.commit();conn.close()
    assert quote(desk)[0] is None
    assert quote(desk,side='sell',amount=1000)[0] is None


def test_future_state_never_prices_a_frozen_fill(tmp_path):
    desk,c=fixture(tmp_path)
    conn=sqlite3.connect(desk/'pumpswap/tape.db');conn.execute('UPDATE swaps_ps SET received_at=?',(NOW,));conn.commit();conn.close()
    assert quote(desk)[0] is None


@pytest.mark.parametrize('change',[{'ps_gap_pending':True},{'ps_committed_through':NOW-2},{'connected':False}, {'ts':NOW+1}])
def test_uncommitted_or_unhealthy_capture_is_unknown(tmp_path,change):
    desk,c=fixture(tmp_path)
    p=desk/'universe.heartbeat';b=json.loads(p.read_text());b.update(change);p.write_text(json.dumps(b))
    assert quote(desk)[0] is None


def test_capture_gap_and_stale_state_are_unknown(tmp_path):
    desk,c=fixture(tmp_path)
    conn=sqlite3.connect(desk/'pumpswap/tape.db');conn.execute('INSERT INTO gaps_ps VALUES(?,?,?)',(NOW-1,'disconnect',18));conn.commit();conn.close()
    assert quote(desk)[0] is None


@pytest.mark.parametrize('amount',[0,-1,True,1.5,2**64])
def test_invalid_atomic_quantity_never_prices(tmp_path,amount):
    desk,c=fixture(tmp_path)
    assert quote(desk,amount=amount)[0] is None
    assert quote(desk,side='sell',amount=amount)[0] is None


def test_known_later_swap_cannot_hide_beyond_the_block_cut(tmp_path):
    desk,c=fixture(tmp_path)
    con=sqlite3.connect(desk/'pumpswap/tape.db')
    con.execute('CREATE TEMP TABLE later AS SELECT * FROM swaps_ps')
    con.execute("UPDATE later SET signature='later',slot=21,received_at=?,post_base_reserve_atomic=NULL",(NOW-1,))
    con.execute('INSERT INTO swaps_ps SELECT * FROM later');con.commit();con.close()
    assert quote(desk)[0] is None


def test_recovered_gap_before_the_exact_state_does_not_censor_its_price(tmp_path):
    desk,c=fixture(tmp_path)
    con=sqlite3.connect(desk/'pumpswap/tape.db')
    con.execute('INSERT INTO gaps_ps VALUES(?,?,?)',(NOW-20,'disconnect',1));con.commit();con.close()
    assert quote(desk)[0] is not None


def test_dust_sell_is_proven_zero_but_insufficient_real_vault_is_unknown(tmp_path):
    desk,c=fixture(tmp_path)
    out,why=quote(desk,side='sell',amount=1)
    assert why is None and out['lamports']=='0'
    con=sqlite3.connect(desk/'pumpswap/tape.db')
    payload={'ix_name':'buy','virtual_quote_reserves':10**12,'virtual_quote_atomic':str(10**12),'virtual_quote_source':E.VQ_SOURCE}
    con.execute('UPDATE swaps_ps SET payload_json=?',(json.dumps(payload),));con.commit();con.close()
    assert quote(desk,side='sell',amount=10**14)[0] is None


def test_unknown_captured_mapping_clock_is_not_absence(tmp_path):
    desk,c=partial_intake(tmp_path,derived=True)
    from test_desk_ps_asof import mapping
    m=mapping()
    con=sqlite3.connect(desk/'pumpswap/tape.db')
    con.execute('INSERT INTO pools VALUES('+','.join('?'*19)+')',(m['pool'],m['mint'],m['quote_mint'],m['creator'],m['canonical'],m['slot'],m['signature'],m['ordinal'],m['tx_index'],0,0,m['received_at'],m['known_at'],None,'{}','','solana','pumpswap','p'))
    con.commit();con.close()
    assert quote(desk)[0] is None


def _quiet(desk, silence):
    p=desk/'universe.heartbeat';b=json.loads(p.read_text())
    b.update(ts=NOW+silence+.2,ps_committed_through=NOW+silence+.1);p.write_text(json.dumps(b))
    return quote(desk,side='sell',amount=1_000_000,at=NOW+silence,evaluated_at=NOW+silence+.2)


def test_a_quiet_pool_with_proven_capture_still_prices_its_last_state(tmp_path):
    desk,c=fixture(tmp_path)
    got,why=_quiet(desk,1200)                                        # 2026-10-28 trade 2: 40 min of pool_state_stale on a silent pool
    assert why is None and int(got['lamports'])>0


def test_a_gap_inside_the_quiet_window_still_refuses(tmp_path):
    desk,c=fixture(tmp_path)
    conn=sqlite3.connect(desk/'pumpswap/tape.db');conn.execute('INSERT INTO gaps_ps VALUES(?,?,?)',(NOW+600,'disconnect',18));conn.commit();conn.close()
    assert _quiet(desk,1200)[0] is None


def test_one_tape_window_read_proves_the_block_cut_and_finds_the_tip(tmp_path, monkeypatch):
    """The block-cut proof and the tip share one bounded read: two reads doubled a 6 h quote to 7.5 s CPU."""
    from sniper.services import memecoin_desk_ps_tape as T
    desk,c=partial_intake(tmp_path,derived=True)
    real=T.window_rows; calls=[]
    def counted(conn,columns,pool,lo,hi,**kw):
        if kw.get('limit')==1 and pool==POOL: calls.append(kw)
        return real(conn,columns,pool,lo,hi,**kw)
    monkeypatch.setattr(T,'window_rows',counted)
    got,why=quote(desk)
    assert why is None and got['pool']==POOL
    assert len(calls)==1, calls


WSOL='So11111111111111111111111111111111111111112'


def _curve_derived(tmp_path,*,pools='none',grad_slot=10,grad_mint=None,grad_known=None,grad_quote=WSOL):
    """D Amendment 19: the swap row is mapped_curve_derived and the graduation (ps_grads) bound the pool."""
    desk,c=fixture(tmp_path)
    con=sqlite3.connect(desk/'pumpswap/tape.db')
    received=con.execute('SELECT received_at FROM swaps_ps').fetchone()[0]
    con.execute("UPDATE swaps_ps SET map_status='mapped_curve_derived'")
    con.execute('CREATE TABLE ps_grads(pool TEXT, mint TEXT, quote_mint TEXT, kind TEXT, slot INTEGER, signature TEXT,'
                ' ordinal INTEGER, tx_index INTEGER, block_time INTEGER, received_at REAL, known_at REAL,'
                ' curve_quote_mint TEXT, curve_real_quote INTEGER, grad_class TEXT, PRIMARY KEY(signature,ordinal))')
    if grad_slot is not None:
        con.execute("INSERT INTO ps_grads VALUES(?,?,?,'migration',?,'g',0,1,0,?,?,?,1,'sol85')",
                    (POOL,grad_mint or MINT,grad_quote,grad_slot,received-60,
                     received-60 if grad_known is None else grad_known,WSOL))
    if pools=='none':
        con.execute('DELETE FROM pools')
    elif pools=='conflict':
        con.execute("UPDATE pools SET mint='OtherMint111111111111111111111111111111111'")
    con.commit();con.close()
    return desk


def test_curve_derived_binding_prices_and_is_tagged_unchecked_without_create_pool(tmp_path):
    got,why=quote(_curve_derived(tmp_path),side='sell',amount=10**9)
    assert why is None and got['proof']['map_source']=='curve_derived'
    assert got['proof']['binding_check']=='unchecked' and got['binding_kind']=='curve_derived_unchecked'


def test_curve_derived_binding_agreeing_with_create_pool_is_checked(tmp_path):
    got,why=quote(_curve_derived(tmp_path,pools='agree'),side='sell',amount=10**9)
    assert why is None and got['proof']['map_source']=='curve_derived'
    assert got['proof']['binding_check']=='create_pool_agrees' and got['binding_kind']=='curve_derived'


@pytest.mark.parametrize('kw',[dict(pools='conflict'),dict(grad_slot=None),dict(grad_slot=18),dict(grad_slot=19),
                               dict(grad_mint='OtherMint111111111111111111111111111111111'),
                               dict(grad_quote='OtherQuote11111111111111111111111111111111'),
                               dict(grad_known=1799999995.0)],
                         ids=['create_pool_conflict','no_grad','grad_same_slot','grad_later_slot','grad_other_mint',
                              'grad_other_quote','grad_known_after_swap'])
def test_curve_derived_binding_refuses_without_a_strictly_earlier_agreeing_graduation(tmp_path,kw):
    assert quote(_curve_derived(tmp_path,**kw),side='sell',amount=10**9)==(None,'pool_binding_unknown')


def test_create_pool_binding_is_unchanged(tmp_path):
    desk,c=fixture(tmp_path)
    got,why=quote(desk,side='sell',amount=10**9)
    assert why is None and got['proof']['map_source']=='create_pool' and got['proof']['binding_check']=='create_pool'
    assert got['binding_kind']=='create_pool'


def test_a_cut_past_the_committed_frontier_is_typed_never_backdated(tmp_path):
    desk,c=fixture(tmp_path)
    beat=json.loads((desk/'universe.heartbeat').read_text())
    assert quote(desk,side='sell',amount=10**9,at=beat['ps_committed_through']+.01)==(None,'frontier_not_committed')
    assert quote(desk,side='sell',amount=10**9,at=beat['ps_committed_through'])[1] is None


def _zero_tip(desk,*,prior):
    conn=sqlite3.connect(desk/'pumpswap/tape.db')
    cols=[r[1] for r in conn.execute('PRAGMA table_info(swaps_ps)')]
    row=dict(zip(cols,conn.execute('SELECT * FROM swaps_ps ORDER BY slot DESC LIMIT 1').fetchone()))
    if prior:
        old=dict(row,signature='older',slot=row['slot']-1,tx_index=1,received_at=row['received_at']-1,lp_fee_bps='100',protocol_fee_bps='100',creator_fee_bps='100')
        conn.execute('INSERT INTO swaps_ps(%s) VALUES(%s)'%(','.join(cols),','.join('?'*len(cols))),[old[c] for c in cols])
    conn.execute("UPDATE swaps_ps SET lp_fee_bps='0',protocol_fee_bps='0',creator_fee_bps='0' WHERE signature=?",(row['signature'],));conn.commit();conn.close()


def test_an_exempt_tip_never_gives_a_cheaper_fill(tmp_path):
    desk,_=fixture(tmp_path/'a')
    normal,_=quote(desk,side='sell',amount=10**12)
    desk2,_=fixture(tmp_path/'b');_zero_tip(desk2,prior=True)
    got,why=quote(desk2,side='sell',amount=10**12)
    assert why is None and got['proof']['fee_basis']=='newest_nonzero' and int(got['lamports'])<int(normal['lamports'])
    bought,why=quote(desk2)
    assert why is None and bought['proof']['fee_basis']=='newest_nonzero'


def test_an_exempt_tip_with_no_prior_keeps_zero_and_says_so_and_a_normal_tip_has_no_label(tmp_path):
    desk,_=fixture(tmp_path/'a');assert 'fee_basis' not in quote(desk)[0]['proof']
    desk2,_=fixture(tmp_path/'b');_zero_tip(desk2,prior=False)
    assert quote(desk2)[0]['proof']['fee_basis']=='tip_zero_unresolved'


def test_a_prior_past_the_age_bound_or_with_cashback_is_not_used(tmp_path):
    from sniper.services import memecoin_desk_ps_asof as A
    conn=sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE swaps_ps(pool,slot,tx_index,ordinal,received_at,lp_fee_bps,protocol_fee_bps,creator_fee_bps,cashback_fee_bps)')
    conn.execute("INSERT INTO swaps_ps VALUES('P',5,1,0,50.,'20','5','95','0')");conn.execute("INSERT INTO swaps_ps VALUES('P',9,1,0,99.,'20','5','95','1')")
    assert A.resolve_fees(conn,'P',10,100.,'0','0','0',max_age_s=10)[3]=='tip_zero_unresolved'
    assert A.resolve_fees(conn,'P',10,100.,'0','0','0',max_age_s=60)[:3]==('20','5','95')
    assert A.resolve_fees(conn,'P',10,100.,'1','0','0')==('1','0','0','tip') and A.resolve_fees(conn,'P',10,100.,None,'0','0')[3]=='tip'
