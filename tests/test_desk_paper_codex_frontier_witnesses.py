"""Offline paper receipt-frontier witness; temporary DB only."""
import json
from test_memecoin_desk_pipeline import databases
from test_desk_paper import paper_mode, _routed, tape, candidate, VQ, VT
from sniper.services import memecoin_desk_paper as paper


def test_entry_waits_until_landing_time_is_committed(databases,paper_mode,tmp_path):
    _,t0=_routed(databases)
    root=tmp_path/'desk'
    tape(root,candidate()['mint'],[(t0-5,VQ,VT)]).close()
    fill_at=t0+paper.ENTRY_DELAY_S
    (root/'universe.heartbeat').write_text(json.dumps({
        'ts':t0+2,'connected':True,'sol_usd':180.,'sol_age_s':1.,
        'curve_capture_from':t0-5,'curve_committed_through':fill_at-0.5,
        'ps_capture_from':None,'ps_committed_through':None}))
    paper.fill_pending(databases[0],desk_dir=root,now=t0+2,sol_usd=180.,sol_at=t0)
    row=dict(databases[0].execute('SELECT * FROM desk_paper_positions').fetchone())
    assert row['state']=='pending_fill', 'paper filled a time beyond the committed tape frontier: '+str(row['state'])


def test_tick_does_not_mark_or_exit_from_a_row_beyond_frontier(databases,paper_mode,tmp_path):
    import sqlite3
    _,t0=_routed(databases)
    root=tmp_path/'desk'
    conn=databases[0]
    tape(root,candidate()['mint'],[(t0-5,VQ,VT)]).close()
    def beat(ts, through):                                  # a REAL healthy heartbeat, read by capture_proof (Codex: no vacuous pass)
        (root/'universe.heartbeat').write_text(json.dumps({'ts':ts,'connected':True,'sol_usd':180.,'sol_age_s':1.,
            'curve_capture_from':t0-5,'curve_committed_through':through,'ps_capture_from':None,'ps_committed_through':None}))
    beat(t0+2, t0+2)
    paper.fill_pending(conn,desk_dir=root,now=t0+2,sol_usd=180.,sol_at=t0+1)
    beat(t0+3, t0+3)
    paper.tick(conn,desk_dir=root,now=t0+3,sol_usd=180.,sol_at=t0+2)
    before=dict(conn.execute('SELECT * FROM desk_paper_positions').fetchone())
    assert before['state']=='open' and before['last_mark_at'] is not None, before   # opened and marked on committed tape
    with sqlite3.connect(root/'swaps'/(paper._iso(t0)[:10]+'.db')) as db:
        db.execute("INSERT INTO swaps(slot, signature, ordinal, mint, is_buy, virtual_quote, virtual_token, received_at, real_token, fee_bps, creator_fee_bps, real_quote, quote_mint, cashback_fee_bps) VALUES (99,'past-frontier',0,?,0,?,?,?,?,95,5,?,'So11111111111111111111111111111111111111112',0)", (candidate()['mint'],VQ//2,VT*2,t0+4,VT*3//2,VQ*3//8))
    (root/'universe.heartbeat').write_text(json.dumps({'ts':t0+4.5,'connected':True,'sol_usd':180.,'sol_age_s':1.,'curve_capture_from':t0-5,'curve_committed_through':t0+3,'ps_capture_from':None,'ps_committed_through':None}))
    paper.tick(conn,desk_dir=root,now=t0+4.5,sol_usd=180.,sol_at=t0+3)
    after=dict(conn.execute('SELECT * FROM desk_paper_positions').fetchone())
    assert after['last_mark_at']==before['last_mark_at'] and after['exit_requested_at'] is None, 'tick used a reserve row beyond committed frontier'
