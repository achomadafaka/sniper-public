"""Ticketless protocol witnesses use private SQLite stores only."""
import json,sqlite3
from datetime import datetime,timezone
import pytest
from sniper.office import self_sheet as SS


def stamp(n):return datetime.fromtimestamp(n,timezone.utc).isoformat()


@pytest.fixture
def desk(tmp_path):
    from sniper.persistence import db_memecoin_desk as D
    path=tmp_path/'desk.db';c=sqlite3.connect(path);D.ensure_tables(c);c.close()
    return path


def stage(path,at,*,ticket='t',mint='M',stage_name='hard_filter'):
    c=sqlite3.connect(path)
    # Read projection fixture: only the indexed columns it reads; producer creates the actual schema above.
    c.execute('INSERT INTO desk_tickets(ticket_id,opportunity_id,cohort_id,producer,network_id,mint,pool,stage,seat,verdict,reason,inputs_hash,inputs_json,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (ticket,'opp-'+ticket,'cohort','U','solana',mint,'P',stage_name,'code','PASS','ok',ticket,'{}',at))
    c.commit();c.close()


def test_source_lookup_distinguishes_empty_known_feed_future_and_unknown(desk,tmp_path):
    assert SS.feed_lookup(desk,'solana','M',cut_host=100.)==dict(state='known',source='self',known_at_host=None,ticket_id=None)
    stage(desk,stamp(110.))
    assert SS.feed_lookup(desk,'solana','M',cut_host=100.)['source']=='self'
    assert SS.feed_lookup(desk,'solana','M',cut_host=120.)['source']=='feed'
    stage(desk,'not-a-time',ticket='bad',mint='N')
    assert SS.feed_lookup(desk,'solana','N',cut_host=120.)['state']=='unknown'
    assert SS.feed_lookup(tmp_path/'missing','solana','M',cut_host=100.)['state']=='unknown'
    assert not (tmp_path/'missing').exists()


def test_default_self_sheet_never_turns_missing_host_safety_into_a_pass():
    out=SS.build(network_id='solana',mint='M',pool='P',now=100.,registration=dict(known_at=90.,actor_id='scout'))
    assert out['state']=='unknown' and out['sheet']['safety']['authority_mint'] is None
    assert out['sheet']['hard_filter']['state']=='unknown' and out['sheet']['sheet_lenses_missing']
    assert 'ticket_id' not in out and 'cache_key' not in out


def test_any_causal_feed_stage_prevents_self_classification(desk):
    stage(desk,stamp(90.),stage_name='intake')
    out=SS.feed_lookup(desk,'solana','M',cut_host=100.)
    assert out['source']=='feed' and out['ticket_id']=='t'


def test_original_registration_clock_binding_survives_retries_and_unknown_cannot_rebind(tmp_path):
    from sniper.office import store as S,v2_orders as O
    conn=S.open_store(tmp_path/'office.db');O.ensure_schema(conn)
    try:
        first=O.register(conn,epoch_id='e',actor_id='scout',network_id='solana',mint='M',pool='P',now=100.,registration_clock=dict(offset_s=10.,sampled_at_host=95.))
        retry=O.register(conn,epoch_id='e',actor_id='judge',network_id='solana',mint='M',pool='X',now=200.,registration_clock=dict(offset_s=50.,sampled_at_host=195.))
        assert first==retry and first['corrected_known_at']==90. and len(first['registration_clock_receipt'])==64
        bad=O.register(conn,epoch_id='e',actor_id='scout',network_id='solana',mint='N',pool='P',now=100.,registration_clock=dict(offset_s=None,sampled_at_host=None))
        assert bad['clock_state']=='unknown'
        assert O.register(conn,epoch_id='e',actor_id='judge',network_id='solana',mint='N',pool='P',now=200.,registration_clock=dict(offset_s=10.,sampled_at_host=195.))==bad
        O.register(conn,epoch_id='e',actor_id='scout',network_id='solana',mint='legacy',pool='P',now=100.)
        row=conn.execute("SELECT known_at,actor_id,pool FROM office_v2_candidates WHERE mint='legacy'").fetchone()
        assert SS.registration_time(conn,'e','solana','legacy',row)['reason']=='registration_clock_unbound'
    finally:conn.close()
