"""Independent real-helper integration checks; every ledger/tape is under tmp_path."""
import json
import sqlite3

import pytest
from sniper.services import memecoin_desk_paper as P
from sniper.services import memecoin_desk_ps_tape as PT
from sniper.services import memecoin_desk_lessons as L
from test_memecoin_desk_pipeline import databases
from test_desk_paper import paper_mode, _routed, tape, candidate, VQ, VT
from test_desk_lessons_v3 import ps_tape


def beat(root, now, curve, ps=None, start=None):
    start=now-300 if start is None else start
    (root/'universe.heartbeat').write_text(json.dumps({
        'ts':now,'connected':True,'sol_usd':180.,'sol_age_s':0.,
        'curve_capture_from':start,'curve_committed_through':curve,
        'pumpswap_capture':True,'ps_capture_from':start if ps is not None else None,
        'ps_committed_through':ps}))


def opened(databases, tmp_path):
    ticket,t0=_routed(databases)
    root=tmp_path/'desk'
    tape(root,candidate()['mint'],[(t0-5,VQ,VT)]).close()
    beat(root,t0+2,t0+2,start=t0-5)
    [out]=P.fill_pending(databases[0],desk_dir=root,now=t0+2,sol_usd=180.,sol_at=t0+1)
    assert out['state']=='open'
    row=dict(databases[0].execute('SELECT * FROM desk_paper_positions').fetchone())
    assert row['state']=='open' and row['qty_atomic'] and row['last_mark_at']
    return ticket,t0,root


def complete(root,t0):
    path=root/'swaps'/(P._iso(t0)[:10]+'.db')
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO swaps(slot,signature,ordinal,mint,is_buy,virtual_quote,virtual_token,received_at,real_token,fee_bps,creator_fee_bps,real_quote,quote_mint,cashback_fee_bps) VALUES(99,'complete',0,?,1,?,?,?,0,95,5,?,?,0)",
                  (candidate()['mint'],VQ,VT,t0+4,VQ*3//4,PT.WSOL_MINT))


def pool(root, rows):
    ps_tape(root,rows)
    with sqlite3.connect(PT.tape_path(root)) as c:
        c.execute('UPDATE swaps_ps SET mint=?,pool=?,quote_mint=?',
                  (candidate()['mint'],PT.canonical_pool(candidate()['mint']),PT.WSOL_MINT))
        c.execute('CREATE TABLE ps_gap_spans(start_at REAL,end_at REAL)')


def test_real_frontier_mark_ignores_later_row(databases,paper_mode,tmp_path):
    _,t0,root=opened(databases,tmp_path)
    conn=databases[0]
    beat(root,t0+3,t0+3,start=t0-5)
    P.tick(conn,desk_dir=root,now=t0+3,sol_usd=180.,sol_at=t0+2)
    before=dict(conn.execute('SELECT * FROM desk_paper_positions').fetchone())
    assert before['state']=='open' and before['last_mark_at']==P._iso(t0+3)
    with sqlite3.connect(root/'swaps'/(P._iso(t0)[:10]+'.db')) as c:
        c.execute("INSERT INTO swaps(slot,signature,ordinal,mint,is_buy,virtual_quote,virtual_token,received_at,real_token,fee_bps,creator_fee_bps,real_quote,quote_mint,cashback_fee_bps) VALUES(99,'past',0,?,0,?,?,?,?,95,5,?,?,0)",
                  (candidate()['mint'],VQ//2,VT*2,t0+4,VT*3//2,VQ*3//8,PT.WSOL_MINT))
    beat(root,t0+4.5,t0+3,start=t0-5)
    P.tick(conn,desk_dir=root,now=t0+4.5,sol_usd=180.,sol_at=t0+2)
    after=dict(conn.execute('SELECT * FROM desk_paper_positions').fetchone())
    assert after['last_mark_at']==before['last_mark_at']
    assert after['last_mark_usd']==before['last_mark_usd'] and after['exit_requested_at'] is None


def test_real_curve_exit_waits_without_attempt_then_closes_original_time(databases,paper_mode,tmp_path):
    _,t0,root=opened(databases,tmp_path)
    conn=databases[0]
    conn.execute("UPDATE desk_paper_positions SET exit_reason='b_stop',exit_requested_at=?",(P._iso(t0+4),));conn.commit()
    beat(root,t0+6,t0+4.5,start=t0-5)
    assert P.tick(conn,desk_dir=root,now=t0+6,sol_usd=180.,sol_at=t0+4)==[]
    assert conn.execute('SELECT exit_json FROM desk_paper_positions').fetchone()[0] is None
    beat(root,t0+6.5,t0+6,start=t0-5)
    [out]=P.tick(conn,desk_dir=root,now=t0+6.5,sol_usd=180.,sol_at=t0+4)
    assert out['state']=='closed'
    ex=json.loads(conn.execute('SELECT exit_json FROM desk_paper_positions').fetchone()[0])
    assert ex['at']==t0+5 and ex['attempts']==[]


def test_real_pool_control_has_a_positive_causal_quote(databases,paper_mode,tmp_path):
    _,t0,root=opened(databases,tmp_path)
    complete(root,t0);pool(root,[(t0+4.5,1,1,'0')])
    amount,venue=P.sell_proceeds(root,candidate()['mint'],1000,t0+5)
    assert amount>0 and venue=='pumpswap',(amount,venue)
    beat(root,t0+6,t0+6,ps=t0+6,start=t0-5)
    assert P._sell_on_committed_tape(root,candidate()['mint'],1000,t0+6)[0]>0


def test_real_ps_frontier_wait_spends_no_exit_attempt(databases,paper_mode,tmp_path):
    _,t0,root=opened(databases,tmp_path)
    complete(root,t0);pool(root,[(t0+4.5,1,1,'0')])
    conn=databases[0]
    conn.execute("UPDATE desk_paper_positions SET exit_reason='b_stop',exit_requested_at=?",(P._iso(t0+4),));conn.commit()
    beat(root,t0+6,t0+6,ps=t0+4.5,start=t0-5)
    before=dict(conn.execute('SELECT * FROM desk_paper_positions').fetchone())
    out=P.tick(conn,desk_dir=root,now=t0+6,sol_usd=180.,sol_at=t0+4)
    after=dict(conn.execute('SELECT * FROM desk_paper_positions').fetchone())
    assert after['last_mark_at']==before['last_mark_at']
    assert out==[] and after['exit_json'] is None,(out,after['exit_json'])


def test_known_invalid_ps_row_at_exact_quote_time_blocks_older_state(databases,paper_mode,tmp_path):
    _,t0,root=opened(databases,tmp_path)
    complete(root,t0);pool(root,[(t0+4.5,1,1,'0'),(t0+5,1,1,None)])
    amount,venue=P.sell_proceeds(root,candidate()['mint'],1000,t0+5)
    assert amount is None,(amount,venue)


def test_proven_zero_exit_mark_does_not_advance_past_cut(databases,paper_mode,tmp_path):
    _,t0,root=opened(databases,tmp_path)
    complete(root,t0);pool(root,[(t0+4.5,1,1,'0')])
    with sqlite3.connect(PT.tape_path(root)) as c:
        c.execute("UPDATE swaps_ps SET post_quote_reserve_atomic='1'")
    amount,venue=P.sell_proceeds(root,candidate()['mint'],1000,t0+5)
    assert amount is None and venue==P.PROVEN_ZERO
    conn=databases[0]
    conn.execute("UPDATE desk_paper_positions SET exit_reason='b_stop',exit_requested_at=?",(P._iso(t0+4),));conn.commit()
    beat(root,t0+6,t0+5,ps=t0+5,start=t0-5)
    P.tick(conn,desk_dir=root,now=t0+6,sol_usd=180.,sol_at=t0+4)
    row=dict(conn.execute('SELECT * FROM desk_paper_positions').fetchone())
    assert row['last_mark_usd']==0
    assert P._ts(row['last_mark_at'])<=t0+5,row['last_mark_at']


@pytest.mark.parametrize('problem',['stale','new_null_fee','new_zero_liquidity','post_state_gap','non_sol','unknown_quote'])
def test_real_ps_bad_evidence_is_unknown_not_old_liquidity(databases,paper_mode,tmp_path,problem):
    _,t0,root=opened(databases,tmp_path)
    complete(root,t0)
    end=t0+130 if problem=='stale' else t0+10
    rows=[(t0+4.5,1,1,'0')]
    if problem in ('new_null_fee','new_zero_liquidity'):
        rows.append((t0+8,1,1,None if problem=='new_null_fee' else '0'))
    pool(root,rows)
    with sqlite3.connect(PT.tape_path(root)) as c:
        if problem=='new_zero_liquidity':
            c.execute("UPDATE swaps_ps SET post_quote_reserve_atomic='0' WHERE received_at=?",(t0+8,))
        if problem=='post_state_gap':
            c.execute("INSERT INTO gaps_ps VALUES(?,'ws_gap',0)",(t0+8,))
        if problem in ('unknown_quote','non_sol'):
            c.execute('UPDATE swaps_ps SET quote_mint=?',(None if problem=='unknown_quote' else 'USDC',))
    amount,venue=P.sell_proceeds(root,candidate()['mint'],1000,end)
    assert amount is None,(problem,amount,venue)


def test_real_ps_frontier_consumed_attempt_moves_landing_time(databases,paper_mode,tmp_path):
    _,t0,root=opened(databases,tmp_path)
    complete(root,t0);pool(root,[(t0+4.5,1,1,'0')])
    conn=databases[0]
    conn.execute("UPDATE desk_paper_positions SET exit_reason='b_stop',exit_requested_at=?",(P._iso(t0+4),));conn.commit()
    beat(root,t0+6,t0+6,ps=t0+4.5,start=t0-5)
    P.tick(conn,desk_dir=root,now=t0+6,sol_usd=180.,sol_at=t0+4)
    beat(root,t0+7,t0+7,ps=t0+7,start=t0-5)
    [out]=P.tick(conn,desk_dir=root,now=t0+7,sol_usd=180.,sol_at=t0+4)
    assert out['state']=='closed'
    ex=json.loads(conn.execute('SELECT exit_json FROM desk_paper_positions').fetchone()[0])
    assert ex['at']==t0+5 and ex['attempts']==[],ex
