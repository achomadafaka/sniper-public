import importlib.util
import math
import sqlite3
from pathlib import Path


spec=importlib.util.spec_from_file_location('research_quote_capacity',Path(__file__).parents[1]/'scripts/research/dex_boost_collector.py')
C=importlib.util.module_from_spec(spec);spec.loader.exec_module(C)


def test_default_daily_research_quote_ceiling_is_removed():
    assert math.isinf(C.DAILY_CAP)


def test_capture_quotes_use_documented_keyed_free_rate_without_burst():
    assert C.JUP_RATE==C.GLOBAL_RATE==1.
    b=C.Bucket(C.JUP_RATE)
    assert b.take(0.) and not b.take(.5) and b.take(1.)
    b.cut(1.)
    assert not b.take(2.) and b.take(3.)


def test_resource_epoch_tags_censored_quotes_and_keeps_old_origins_unknown(tmp_path):
    out=tmp_path/'out';out.mkdir();stamp=1793500000.;day=C.utc_day(stamp)
    with sqlite3.connect(out/(day+'.db')) as c:
        c.executescript(C.SCHEMA+C.FAMILY_SCHEMA)
        c.execute('INSERT INTO family_pairs VALUES(?,?,?,?,?,?,?,?,?,?,?)',('old','pool','sig','mint',1.,2.,11.,'pending',None,'v','{}'))
    owner=C.Collector(intake_dir=tmp_path/'intake',out_dir=out,desk_dir=tmp_path/'desk',clock_fn=lambda:(0.,stamp),jupiter_fn=lambda *a:None,ps_buy=lambda *a:None,ps_sell=lambda *a:None,paper_throttled_fn=lambda:False,now_fn=lambda:stamp,family_probe=True,family_db=tmp_path/'graph.db')
    c=owner.day(day)
    assert c.execute("SELECT 1 FROM resource_record_epochs WHERE table_name='family_pairs'").fetchone() is None
    owner._family_record(c,'old','early',0.,'censored','source_unknown',stamp)
    row=c.execute("SELECT origin_epoch,first_write_epoch,last_write_epoch FROM resource_record_epochs WHERE table_name='family_quotes'").fetchone()
    assert row['origin_epoch']=='legacy_unattributed'
    assert row['first_write_epoch']==row['last_write_epoch']!='legacy_unattributed'
    policy=c.execute('SELECT policy_json FROM resource_epochs').fetchone()[0]
    assert 'Infinity' not in policy and '"daily_quote_cap": null' in policy
    assert c.execute('SELECT count(*) FROM family_pairs').fetchone()[0]==1
    c.execute("UPDATE family_pairs SET state='censored' WHERE pair_id='old'")
    updated=c.execute("SELECT origin_epoch,last_write_epoch FROM resource_record_epochs WHERE table_name='family_pairs'").fetchone()
    assert updated is not None and updated['origin_epoch']=='legacy_unattributed'
    assert updated['last_write_epoch']==row['last_write_epoch']


def test_quote_rate_and_observed_429_slowdown_survive_owner_restart(tmp_path):
    calls=[]
    def provider(*a):calls.append(a);return (None,'rate_limited') if len(calls)==1 else ({'outAmount':'10','routePlan':[]},None)
    def owner():return C.Collector(intake_dir=tmp_path/'intake',out_dir=tmp_path/'out',desk_dir=tmp_path/'desk',clock_fn=lambda:(0.,100.),jupiter_fn=provider,ps_buy=lambda *a:None,ps_sell=lambda *a:None,paper_throttled_fn=lambda:False,now_fn=lambda:100.)
    first=owner();assert first._jupiter('a','b',25,100.)[1]=='rate_limited'
    second=owner()
    assert second._jupiter('a','b',25,101.1)[1]=='rate_limited' and len(calls)==1
    assert second._jupiter('a','b',25,102.)[1] is None and len(calls)==2
    third=owner()
    assert third._jupiter('a','b',25,102.5)[1]=='rate_limited' and len(calls)==2
