"""D attention uses witnessed time, versioned text and explicit missing evidence."""
import json
import sqlite3

import pytest

from test_desk_list_claims import NOW, MINT, listing
from test_memecoin_desk_x_producer import X


@pytest.fixture
def tape(tmp_path):
    from sniper.services import memecoin_desk_intake as I
    c=listing(tmp_path)
    for feed in ('pumpfun','dexscreener'):
        db=I.open_day(tmp_path/'intake',feed,NOW);db.close()
    out=tmp_path/'x';out.mkdir();store=X.DayStore(out)
    session='a'*32
    def cover(segments=None, *, epoch=NOW-30000, offset=0):
        conn=store._for(NOW);conn.execute('DELETE FROM connection');conn.commit()
        rows=[('connection',dict(at=epoch,kind='epoch',detail='desk-x-coverage-v2',
                                clock_offset_s=offset,session_id=session))]
        for start,end in (segments or [(epoch,NOW)]):
            rows.append(('connection',dict(at=start,kind='connect',detail=None,clock_offset_s=offset,session_id=session)))
            t=start+10
            while t<end:
                rows.append(('connection',dict(at=t,kind='heartbeat',detail='{"connected":true}',
                                              clock_offset_s=offset,session_id=session)));t+=10
            rows.append(('connection',dict(at=end,kind='heartbeat',detail='{"connected":true}',
                                          clock_offset_s=offset,session_id=session)))
            if end<NOW:
                rows.append(('connection',dict(at=end,kind='disconnect',detail=None,clock_offset_s=offset,session_id=session)))
        store.write(rows)
        (out/'producer.heartbeat').write_text(json.dumps(dict(ts=NOW,connected=True,session_id=session,
                 epoch_at=epoch,clock_offset_s=offset,committed_through=NOW)))
    def post(tid,text,at,*,event='tweet',author='account',followers=100,offset=0):
        payload={'id':tid,'text':text,'author':{'id':author,'followersCount':followers,'handle':'someone'}}
        payload=[payload] if event=='initialTweets' else payload
        store.write(X.rows_for(event,payload,at,clock_offset_s=offset))
    cover()
    yield tmp_path,c,post,cover,store
    store.close()


def read(tape, **kw):
    from sniper.services import memecoin_desk_heat_v2 as H
    root,c,*_=tape
    return H.heat_for(c,desk_dir=root,as_of=kw.pop('as_of',NOW),**kw)


def test_ticker_union_first_matching_version_and_per_window_counts(tape):
    _,_,post,_,_=tape
    post('one','no match yet',NOW-1000)
    post('one','The $pmup launch',NOW-30,event='tweet_update')
    post('one','PMUP again',NOW-5,event='tweet_update')
    post('two',MINT,NOW-2000,author='other',followers=200)
    post('bad','PMUPPER _PMUP other',NOW-10)
    post('future','PMUP',NOW+1)
    r=read(tape)
    assert r['state']=='known' and r['match_basis']=='both' and r['ticker_ambiguous'] is False
    assert [r['windows'][w]['mentions'] for w in ('15m','1h','6h')]==[1,2,2]
    assert r['windows']['1h']['accounts']==2 and r['first_mention_age_s']==2000
    assert r['windows']['1h']['top_author_followers']==200
    assert r['windows']['15m']['promoter_mentions'] is None
    assert r['windows']['15m']['field_unknown']['promoter_mentions']=='promoter_list_empty'


def test_initial_snapshot_delete_and_missing_author_are_not_false_zero(tape):
    _,_,post,_,_=tape
    post('snapshot','PMUP',NOW-20,event='initialTweets')
    post('live','PMUP',NOW-10,author=None,followers=None)
    post('live','PMUP',NOW-5,event='tweet_deleted')
    r=read(tape);w=r['windows']['15m']
    assert r['backfill_n']==1 and w['mentions']==1 and w['accounts']==0
    assert w['author_unknown_n']==1 and w['lower_bound'] is True and w['top_author_followers'] is None


def test_partial_90_percent_coverage_is_known_lower_bound_and_not_zero_proof(tape):
    _,_,_,cover,_=tape
    cover([(NOW-30000,NOW-180),(NOW-90,NOW)])
    w=read(tape)['windows']['15m']
    assert w['state']=='known' and w['mentions']==0
    assert (w['covered_s'],w['gap_s'],w['lower_bound'],w['scan_loss'])==(810,90,True,False)
    cover([(NOW-30000,NOW-181),(NOW-90,NOW)])
    assert read(tape)['windows']['15m']=={'state':'unknown','reason':'no_coverage_proof'}


def test_short_epoch_missing_offset_and_stale_tape_are_typed(tape):
    _,_,_,cover,store=tape
    cover(epoch=NOW-1000)
    r=read(tape)
    assert r['windows']['15m']['state']=='known'
    assert r['windows']['1h']['reason']=='no_coverage_proof'
    cover(offset=None)
    assert read(tape)['reason']=='clock_unknown'
    cover([(NOW-30000,NOW-61)])
    assert read(tape)['reason']=='tape_stale'


def test_received_offset_and_no_future_version_leakage(tape):
    _,_,post,_,_=tape
    post('a','PMUP',NOW+1,offset=2)
    post('b','nothing yet',NOW-10)
    post('b','PMUP',NOW+1,event='tweet_update')
    assert read(tape)['windows']['15m']['mentions']==1


def test_missing_symbol_map_is_null_not_false(tape):
    root,_,post,_,_=tape
    post('a','PMUP',NOW-10)
    (root/'intake'/'dexscreener'/'first_sightings.db').unlink()
    r=read(tape)
    assert r['ticker_ambiguous'] is None
    assert r['field_unknown']['ticker_ambiguous']=='symbol_map_incomplete'


def test_unknown_source_and_read_budget_never_return_healthy_empty(tmp_path,tape,monkeypatch):
    from sniper.services import memecoin_desk_heat_v2 as H
    assert H.heat_for({},desk_dir=tmp_path/'absent',as_of=NOW)['state']=='unknown'
    monkeypatch.setattr(H,'READ_BUDGET_S',0)
    assert read(tape)['reason']=='read_budget'


def test_pre_epoch_time_is_not_proven_by_ninety_percent_coverage(tape):
    _,_,_,cover,_=tape
    cover(epoch=NOW-850)
    assert read(tape)['reason']=='no_coverage_proof'


def test_late_mint_link_never_backdates_an_unrelated_post(tape):
    _,_,post,_,store=tape
    post('late','unrelated text',NOW-1000)
    post('late','unrelated text',NOW-5,event='tweet_update')
    db=store._for(NOW)
    db.execute('INSERT INTO token_meta(tweet_id,mint,chain,received_at,clock_offset_s,payload_json) VALUES(?,?,?,?,?,?)',
               ('late',MINT,'solana',NOW-5,0,'{}'));db.commit()
    r=read(tape)
    assert r['windows']['15m']['mentions']==1 and r['first_mention_age_s']==5

def test_missing_clock_matching_post_cannot_be_proven_zero(tape):
    _,_,post,_,_=tape
    post("unknown-time","PMUP",NOW-10,offset=None)
    w=read(tape)["windows"]["15m"]
    assert w["state"]=="unknown" or (w["scan_loss"] is True and w["lower_bound"] is True)


def test_unknown_mint_link_time_does_not_date_unrelated_text(tape):
    _,_,post,_,store=tape
    post('unknown-link','unrelated text',NOW-5)
    db=store._for(NOW)
    db.execute('INSERT INTO token_meta(tweet_id,mint,chain,received_at,clock_offset_s,payload_json) VALUES(?,?,?,?,?,?)',
               ('unknown-link',MINT,'solana',NOW-5,None,'{}'));db.commit()
    w=read(tape)['windows']['15m']
    assert w['mentions']==0 and w['scan_loss'] is True and w['lower_bound'] is True
