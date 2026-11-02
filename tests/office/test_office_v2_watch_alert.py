import json,sqlite3
from sniper.office import chain_data as C,seat_events as E,store as S,watch_alert as W,work as K

MINT='So11111111111111111111111111111111111111112';POOL='P'*44
A,B,D,T=['1'*43+x for x in 'ABCD']
SIG='5'+'A'*87


def bal(owner,amount):return dict(owner=owner,mint=MINT,uiTokenAmount=dict(amount=str(amount)))


def tx(pre,post,err=None):return dict(blockTime=100,meta=dict(err=err,preTokenBalances=pre,postTokenBalances=post))


def test_three_owners_to_one_wallet_alerts_and_two_do_not():
    three=tx([bal(A,5),bal(B,5),bal(D,5)],[bal(T,15)])
    assert [a['kind'] for a in W.detect(three,MINT,10**9)]==['consolidation']
    assert W.detect(tx([bal(A,5),bal(B,5)],[bal(T,10)]),MINT,10**9)==[]
    assert W.detect(tx([bal(A,5),bal(B,5),bal(D,5)],[bal(T,15)],err={'x':1}),MINT,10**9)==[]


def test_holder_jump_excludes_the_pool_and_unknown_supply_never_fires():
    jump=tx([bal(POOL,100)],[bal(POOL,60),bal(T,40)])
    out=W.detect(jump,MINT,1000,pools=frozenset({POOL}))
    assert out==[dict(kind='holder_jump',wallet=T,pct_of_supply=4.)]
    assert W.detect(jump,MINT,None,pools=frozenset({POOL}))==[]
    assert W.detect(tx([],[bal(POOL,40)]),MINT,1000,pools=frozenset({POOL}))==[]
    assert W.detect(tx([],[bal(T,29)]),MINT,1000)==[]


def _rig(tmp_path):
    ledger=tmp_path/'l.db';c=sqlite3.connect(ledger);c.execute('CREATE TABLE orders(mint TEXT,pool TEXT,state TEXT,network_id TEXT)');c.execute("INSERT INTO orders VALUES(?,?,'open','solana')",(MINT,POOL));c.commit();c.close()
    office=S.open_store(tmp_path/'o.db');E.ensure_schema(office)
    office.execute("INSERT INTO office_v2_roster VALUES('j','ep','judge',1,1,0,1.)");office.commit()
    return ledger,office


def test_one_pass_alerts_once_mails_seats_as_watch_and_a_repeat_is_silent(tmp_path):
    ledger,office=_rig(tmp_path)
    clock=[2000.]
    def rpc(url,method,params):
        clock[0]+=2
        return {'getSignaturesForAddress':(200,dict(result=[dict(signature=SIG)])),'getTokenSupply':(200,dict(result=dict(value=dict(amount='1000')))),
                'getTransaction':(200,dict(result=tx([bal(POOL,100)],[bal(POOL,60),bal(T,40)])))}[method]
    chain=C.ChainData(tmp_path/'c.db',lambda:'u',clock=lambda:clock[0],rpc=rpc,sleep=lambda s:clock.__setitem__(0,clock[0]+s),monotonic=lambda:clock[0])
    W.run_pass(chain,str(ledger));W.deliver(chain,office)
    row=office.execute("SELECT from_actor_id,to_actor_id,text FROM office_dm_events").fetchone()
    assert row[0]=='watch' and row[1]=='j' and 'holder_jump' in row[2]
    W.run_pass(chain,str(ledger));W.deliver(chain,office)
    assert office.execute('SELECT count(*) FROM office_dm_events').fetchone()[0]==1 and 'candidate' in row[2]
    assert W.latency(chain,0,1e9)['n']==1
    assert sqlite3.connect(tmp_path/'c.db').execute('SELECT mailed_at IS NOT NULL FROM watch_alerts').fetchone()==(1,)


def test_a_quota_refusal_ends_the_pass_with_a_record(tmp_path):
    ledger,office=_rig(tmp_path)
    chain=C.ChainData(tmp_path/'c.db',lambda:'u',clock=lambda:2000.,rpc=lambda *a:(429,dict(quota=True)),monotonic=lambda:0.)
    W.run_pass(chain,str(ledger))
    assert sqlite3.connect(tmp_path/'c.db').execute('SELECT refused FROM watch_passes').fetchone()==('provider_quota_exhausted',)


def test_no_open_order_means_no_calls(tmp_path):
    ledger,office=_rig(tmp_path)
    c=sqlite3.connect(ledger);c.execute("UPDATE orders SET state='closed'");c.commit();c.close()
    chain=C.ChainData(tmp_path/'c.db',lambda:'u',clock=lambda:2000.,rpc=lambda *a:1/0,monotonic=lambda:0.)
    W.run_pass(chain,str(ledger))


def test_a_failed_mail_retries_from_the_table(tmp_path,monkeypatch):
    ledger,office=_rig(tmp_path)
    clock=[2000.]
    def rpc(url,method,params):
        clock[0]+=2
        return {'getSignaturesForAddress':(200,dict(result=[dict(signature=SIG)])),'getTokenSupply':(200,dict(result=dict(value=dict(amount='1000')))),'getTransaction':(200,dict(result=tx([bal(POOL,100)],[bal(POOL,60),bal(T,40)])))}[method]
    chain=C.ChainData(tmp_path/'c.db',lambda:'u',clock=lambda:clock[0],rpc=rpc,sleep=lambda s:clock.__setitem__(0,clock[0]+s),monotonic=lambda:clock[0])
    W.run_pass(chain,str(ledger))
    real=E.seat_dm
    monkeypatch.setattr(E,'seat_dm',lambda *a,**k:1/0)
    try:W.deliver(chain,office)
    except ZeroDivisionError:pass
    monkeypatch.setattr(E,'seat_dm',real)
    W.deliver(chain,office)
    assert office.execute('SELECT count(*) FROM office_dm_events').fetchone()[0]==1
