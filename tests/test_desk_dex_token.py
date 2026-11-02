"""Dex page claims are pool-bound, causal and collected off the sheet path."""
import pytest
from sniper.services import memecoin_desk_dex_token as D

NOW = 1793187000.
C = {'network_id':'solana','mint':'A'*32,'pool':'B'*32}
CHECKS = {k:{'status':'pass'} for k in ('market_data','token_safety','sell_probe')}
COLLECTED = {k:{'status':'pass'} for k in ('holders','cluster','dump_group','chart')}


def queued(tmp_path, c=C, at=NOW):
    assert D.enqueue(c,desk_dir=tmp_path,checks=CHECKS,collected=COLLECTED,as_of=at)


def result(c=C, **kw):
    return dict(state='known',reason=None,mint=c['mint'],pool=c['pool'],vol_24h=0,
                buys_24h=12,sells_24h=3,pair_age_s=None,**kw)


def test_missing_gate_and_future_capture_are_unknown(tmp_path):
    assert D.dex_token_for(C,desk_dir=tmp_path,as_of=NOW)['reason']=='no_capture'
    assert not D.enqueue(C,desk_dir=tmp_path,checks={},collected=COLLECTED,as_of=NOW)
    queued(tmp_path,at=NOW-1)
    with D.Worker(tmp_path) as w:
        assert w.step(now=NOW,offset_s=.5,fetch=lambda c:result())=='captured'
    assert D.dex_token_for(C,desk_dir=tmp_path,as_of=NOW-1)['reason']=='no_capture'
    r=D.dex_token_for(C,desk_dir=tmp_path,as_of=NOW+1)
    assert r['state']=='known' and r['vol_24h']==0 and r['as_of']==NOW-.5
    assert set(r)==D.KNOWN_KEYS and r['field_unknown']=={'pair_age_s':'not_reported'}
    assert D.dex_token_for(C,desk_dir=tmp_path,as_of=NOW+301)['reason']=='stale'


@pytest.mark.parametrize('mutate,expected', [({'pool':'Z'*32},'not_served'),
    ({'vol_24h':None},'not_served_unparsed'),({'buys_24h':-1},'not_served_unparsed'),
    ({'sells_24h':True},'not_served_unparsed')])
def test_wrong_pool_and_bad_mandatory_stats_never_become_zero(tmp_path,mutate,expected):
    queued(tmp_path)
    with D.Worker(tmp_path) as w:
        w.step(now=NOW,offset_s=0,fetch=lambda c:{**result(),**mutate})
    r=D.dex_token_for(C,desk_dir=tmp_path,as_of=NOW+1)
    assert r['state']==('not_served' if expected=='not_served' else 'unknown')
    assert r['reason']==(None if expected=='not_served' else expected)


def test_global_cooldown_three_challenges_disable_and_no_mint_retry(tmp_path):
    calls=[]
    def challenge(c):
        calls.append(c); return {'state':'unknown','reason':'challenged'}
    with D.Worker(tmp_path) as w:
        for i in range(3):
            at=NOW+i*3601
            queued(tmp_path,at=at)
            assert w.step(now=at,offset_s=0,fetch=challenge)=='captured'
            other={**C,'mint':str(i+1)*32};queued(tmp_path,c=other,at=at+1)
            assert w.step(now=at+1,offset_s=0,fetch=challenge)=='challenged'
            assert D.dex_token_for(other,desk_dir=tmp_path,as_of=at+2)['reason']=='challenged'
        queued(tmp_path,at=NOW+20000)
        assert w.step(now=NOW+20000,offset_s=0,fetch=challenge)=='challenged'
    assert len(calls)==3


def test_rate_daily_cap_and_unknown_clock_do_not_send(tmp_path):
    with D.Worker(tmp_path) as w:
        queued(tmp_path)
        assert w.step(now=NOW,offset_s=None,fetch=lambda c:pytest.fail('clock'))=='clock_unknown'
        assert w.step(now=NOW,offset_s=0,fetch=lambda c:result())=='captured'
        queued(tmp_path,c={**C,'mint':'C'*32},at=NOW+.1)
        assert w.step(now=NOW+.1,offset_s=0,fetch=lambda c:pytest.fail('rate'))=='paced'
        assert w.step(now=NOW+1,offset_s=0,fetch=lambda c:result(c))=='captured'
        queued(tmp_path,at=NOW+2)
        assert w.step(now=NOW+2,offset_s=0,fetch=lambda c:pytest.fail('mint retry'))=='paced'


def test_transport_error_is_recorded_and_reader_never_raises(tmp_path):
    queued(tmp_path)
    def fail(c): raise TimeoutError
    with D.Worker(tmp_path) as w:
        w.step(now=NOW,offset_s=0,fetch=fail)
    r=D.dex_token_for(C,desk_dir=tmp_path,as_of=NOW+1)
    assert r['reason']=='read_error' and set(r)==D.UNKNOWN_KEYS
    for candidate,at in [(None,NOW),({},NOW),([],None)]:
        assert D.dex_token_for(candidate,desk_dir=tmp_path,as_of=at)['state']=='unknown'


def test_public_page_parse_needs_exact_pool_mint_and_24h_period():
    url='https://dexscreener.com/solana/'+C['pool']
    text='Volume\n$1.2M\nBuys\n1,234\nSells\n345\nPair created\n2h ago'
    assert D.page_claims(C,url=url,text=text,mints=[C['mint']],period_24h=True)['vol_24h']==1200000
    assert D.page_claims(C,url=url,text=text,mints=[C['mint']],period_24h=False)['reason']=='not_served_unparsed'
    assert D.page_claims(C,url=url,text=text,mints=[],period_24h=True)['state']=='not_served'
    assert D.page_claims(C,url=url,text='Verify you are human',mints=[],period_24h=False)['reason']=='challenged'


def test_receipt_clock_crash_reservation_and_daily_cap(tmp_path):
    queued(tmp_path,at=NOW-1)
    with D.Worker(tmp_path) as w:
        w.step(now=NOW,offset_s=.5,fetch=lambda c:(result(),NOW+2))
        assert D.dex_token_for(C,desk_dir=tmp_path,as_of=NOW+1)['reason']=='read_error' # reserved before receipt
        assert D.dex_token_for(C,desk_dir=tmp_path,as_of=NOW+3)['as_of']==NOW+1.5
        queued(tmp_path,c={**C,'mint':'D'*32},at=NOW+3)
        w.db.execute('UPDATE worker_state SET requests_n=?',(D.DEFINITION['daily_request_cap'],));w.db.commit()
        assert w.step(now=NOW+3,offset_s=0,fetch=lambda c:pytest.fail('cap'))=='daily_cap'


def test_reader_refuses_default_separator_definition_hash(tmp_path):
    import hashlib,json
    queued(tmp_path)
    with D.Worker(tmp_path) as w:
        w.step(now=NOW,offset_s=0,fetch=lambda c:result())
        noncanonical=hashlib.sha256(json.dumps(D.DEFINITION,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()
        assert noncanonical!=D.definition()['definition_sha256']
        row=w.db.execute('SELECT id,payload FROM captures ORDER BY id DESC LIMIT 1').fetchone()
        payload=json.loads(row[1]);payload['definition_sha256']=noncanonical
        w.db.execute('UPDATE captures SET payload=? WHERE id=?',(json.dumps(payload),row[0]));w.db.commit()
        assert D.dex_token_for(C,desk_dir=tmp_path,as_of=NOW+1)['reason']=='read_error'


def test_browser_deadline_kills_process_group_without_retry(monkeypatch):
    import importlib.util
    from pathlib import Path
    path=Path(__file__).resolve().parents[1]/'scripts/memecoin_desk_dex_token_worker.py'
    spec=importlib.util.spec_from_file_location('dex_worker',path);worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
    events=[]
    class Process:
        pid=12345
        def communicate(self,timeout):
            events.append(('timeout',timeout));raise worker.subprocess.TimeoutExpired('browser',timeout)
        def wait(self): events.append('wait')
    monkeypatch.setattr(worker.subprocess,'Popen',lambda *a,**kw:events.append(kw['start_new_session']) or Process())
    monkeypatch.setattr(worker.os,'killpg',lambda pid,sig:events.append(('kill',pid)))
    payload,receipt=worker.fetch(C)
    assert payload=={'state':'unknown','reason':'read_error'} and isinstance(receipt,float)
    assert events==[True,('timeout',D.DEFINITION['page_deadline_s']),('kill',12345),'wait']

def test_unparsed_page_reason_survives_worker(tmp_path):
    queued(tmp_path)
    with D.Worker(tmp_path) as w:
        w.step(now=NOW,offset_s=0,fetch=lambda c:{"state":"unknown","reason":"not_served_unparsed"})
    assert D.dex_token_for(C,desk_dir=tmp_path,as_of=NOW+1)["reason"] == "not_served_unparsed"

def test_paced_mint_does_not_block_other_mint(tmp_path):
    queued(tmp_path)
    other={**C,"mint":"D"*32}
    with D.Worker(tmp_path) as w:
        assert w.step(now=NOW,offset_s=0,fetch=lambda c:result(c))=="captured"
        queued(tmp_path,at=NOW+2)
        queued(tmp_path,c=other,at=NOW+3)
        calls=[]
        w.step(now=NOW+4,offset_s=0,fetch=lambda c:calls.append(c) or result(c))
        assert [c["mint"] for c in calls]==[other["mint"]]
