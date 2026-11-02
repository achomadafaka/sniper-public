from sniper.office.market_data import MarketData

MINT='So11111111111111111111111111111111111111112'
POOL='7qbRF6YsyGuLUVs6Y1q64bdVrfe4ZcUUz1JRdoVNUJnm'
PAIR=dict(chainId='solana',pairAddress=POOL,dexId='pumpswap',baseToken=dict(address=MINT),priceUsd='0.5',liquidity=dict(usd=100),volume=dict(h24=0),pairCreatedAt=1000_000)


def market(tmp_path,answers):
    clock=[2000.];calls=[]
    def fetch(url):
        calls.append(url);return answers.pop(0)
    return MarketData(tmp_path/'m.db',clock=lambda:clock[0],fetch=fetch),clock,calls


def test_units_zero_unknown_provenance_and_cache(tmp_path):
    m,clock,calls=market(tmp_path,[(200,dict(pairs=[PAIR]))])
    out=m.read(mint=MINT)
    assert out['price_usd']==.5 and out['volume_24h_usd']==0. and out['pair_age_s']==1000. and out['market_cap_usd'] is None
    assert out['field_unknown']==['market_cap_usd'] and out['provenance']=='api.dexscreener.com' and out['fetched_at']==2000.
    clock[0]+=59;assert m.read(mint=MINT)['cached'] is True and len(calls)==1


def test_pace_is_shared_across_instances_and_a_429_stops_at_once(tmp_path):
    m,clock,calls=market(tmp_path,[(429,None)])
    assert m.read(mint=MINT)['reason']=='provider_rate_limited'
    clock[0]+=11;assert m.read(pool=POOL)['reason']=='provider_backoff'
    other=MarketData(tmp_path/'m.db',clock=lambda:clock[0],fetch=lambda u:1/0)
    assert other.read(pool=POOL)['reason']=='provider_backoff'
    clock[0]+=900;assert len(calls)==1


def test_failure_is_unknown_and_wrong_identity_never_answers(tmp_path):
    m,clock,calls=market(tmp_path,[(None,None),(200,dict(pairs=[dict(PAIR,baseToken=dict(address='x'))]))])
    assert m.read(mint=MINT)['reason']=='provider_unavailable'
    clock[0]+=10;assert m.read(mint=MINT)['reason']=='no_matching_pair'
    assert m.read(mint='bad')['reason']=='bad_arguments' and m.read(mint=MINT,pool=POOL)['reason']=='bad_arguments'


def test_counts_block_for_the_report(tmp_path):
    m,clock,_=market(tmp_path,[(200,dict(pairs=[PAIR])),(429,None)])
    m.read(mint=MINT);m.read(mint=MINT);m.read(pool=POOL)
    clock[0]+=10;m.read(pool=POOL)
    out=m.counts(0,1e6)
    assert out['dials']==2 and out['dials_by_outcome']=={'fetched':1,'provider_rate_limited':1} and out['other_by_outcome']=={'cache_hit':1,'paced':1}
    assert out['stop_until']==[2910.]


def test_a_delayed_caller_cannot_reserve_with_its_old_clock(tmp_path):
    times=[2000.,2010.,2010.,2010.,2010.];dials=[]
    # The first reading is the caller's, taken before the lock; the old code reserved with it.
    m=MarketData(tmp_path/'m.db',clock=lambda:times.pop(0) if len(times)>1 else times[0],fetch=lambda u:(dials.append(u),(200,dict(pairs=[PAIR])))[1])
    assert m.read(mint=MINT)['state']=='known'
    other=MarketData(tmp_path/'m.db',clock=lambda:2010.,fetch=lambda u:(dials.append(u),(200,dict(pairs=[PAIR])))[1])
    assert other.read(pool=POOL)['reason']=='paced' and len(dials)==1


def test_the_429_stop_runs_from_the_response_time(tmp_path):
    clock=[2000.]
    def fetch(url):
        clock[0]=2100.;return 429,None
    m=MarketData(tmp_path/'m.db',clock=lambda:clock[0],fetch=fetch)
    m.read(mint=MINT)
    clock[0]=2100.+899;assert m.read(pool=POOL)['reason']=='provider_backoff'


def test_malformed_provider_shapes_stay_typed_unknown(tmp_path):
    bad=[dict(pairs='x'),dict(pairs=[dict(chainId='solana',baseToken=[1])]),dict(pairs=[dict(PAIR,liquidity=[1],volume='x',dexId=[1])]),dict(pairs=[dict(PAIR,priceUsd=[1],pairCreatedAt={})])]
    m,clock,_=market(tmp_path,[(200,d) for d in bad])
    out=[]
    for _d in bad:
        clock[0]+=61;out.append(m.read(mint=MINT))
    assert out[0]['reason']=='provider_malformed' and out[1]['reason']=='no_matching_pair'
    assert out[2]['liquidity_usd'] is None and out[2]['dex'] is None and out[3]['price_usd'] is None and out[3]['pair_age_s'] is None


def test_real_threads_never_both_dial_inside_the_pace(tmp_path):
    import threading,time
    dials=[];start=threading.Barrier(4)
    def worker(i):
        m=MarketData(tmp_path/'m.db',fetch=lambda u:(dials.append(u),(200,dict(pairs=[PAIR])))[1])
        start.wait();m.read(pool=POOL) if i%2 else m.read(mint=MINT)
    threads=[threading.Thread(target=worker,args=(i,)) for i in range(4)]
    [t.start() for t in threads];[t.join() for t in threads]
    assert len(dials)==1


def test_each_answer_shown_has_a_row_written_first(tmp_path):
    import sqlite3
    m,clock,_=market(tmp_path,[(200,dict(pairs=[PAIR]))])
    first=m.read(mint=MINT);second=m.read(mint=MINT)
    rows=sqlite3.connect(tmp_path/'m.db').execute('SELECT fetch_id,asked,cached,price_usd,liquidity_usd,volume_24h_usd FROM fetches ORDER BY rowid').fetchall()
    assert rows==[(first['fetch_id'],'mint:'+MINT,0,.5,100.,0.),(second['fetch_id'],'mint:'+MINT,1,.5,100.,0.)]
    assert 'fetch_id' not in m.read(mint='bad')


def test_a_lost_outcome_keeps_the_reservation_and_counts_show_it(tmp_path):
    import sqlite3
    m,clock,_=market(tmp_path,[(200,dict(pairs=[PAIR]))])
    conn=m._conn();conn.execute("CREATE TRIGGER no_outcome BEFORE UPDATE ON calls BEGIN SELECT RAISE(ABORT,'x'); END");conn.close()
    assert m.read(mint=MINT)['state']=='known'
    out=m.counts(0,1e6)
    assert out['dials_by_outcome']=={'outcome_unknown':1} and out['dials']==1


def test_no_reservation_means_no_dial_and_counts_never_create_the_store(tmp_path):
    m,clock,calls=market(tmp_path,[])
    assert m.counts(0,1)['reason']=='market_db_missing' and not (tmp_path/'m.db').exists()
    conn=m._conn();conn.execute("CREATE TRIGGER no_row BEFORE INSERT ON calls BEGIN SELECT RAISE(ABORT,'x'); END");conn.close()
    assert m.read(mint=MINT)['reason']=='market_store_unknown' and calls==[]


def test_the_recorded_stop_is_the_response_time_one(tmp_path):
    clock=[2000.]
    def fetch(url):
        clock[0]=2005.;return 429,None
    m=MarketData(tmp_path/'m.db',clock=lambda:clock[0],fetch=fetch)
    m.read(mint=MINT);assert m.counts(0,1e6)['stop_until']==[2905.]
