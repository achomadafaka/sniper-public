"""Live narrative data: disposable first-sighting/PS schemas, no providers or real state."""
import importlib.util
import json
import sqlite3
from pathlib import Path
import pytest
from sniper.services import memecoin_desk_narrative_guess_store as G
from sniper.services.memecoin_desk_ps_tape import WSOL_MINT

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('narrative_worker_live_test',ROOT/'scripts/memecoin_desk_narrative.py')
N=importlib.util.module_from_spec(spec);spec.loader.exec_module(N)
NOW=1793397600.


def source(tmp_path):
    d=tmp_path/'desk';(d/'narrative_guess').mkdir(parents=True);(d/'pumpswap').mkdir()
    c=G.connect(str(d/'narrative_guess/narrative_guess.db'))
    c.executemany('INSERT INTO launches VALUES(?,?,?,?,?,?,?)',[
        ('solana','cat','Cat Cat','CAT',NOW-100,None,'pump_curve'),
        ('56','evm','Cat','CAT',NOW-90,None,'fomo'),
        ('solana','future','Future','CAT',NOW+10,None,'pumpswap')]);c.commit();c.close()
    c=sqlite3.connect(d/'pumpswap/tape.db')
    c.execute('CREATE TABLE swaps_ps(signature TEXT,ordinal INTEGER,mint TEXT,is_buy INTEGER,quote_atomic TEXT,actor TEXT,received_at REAL,quote_mint TEXT,block_time INTEGER)')
    c.execute('CREATE INDEX swaps_ps_received ON swaps_ps(received_at)')
    c.executemany('INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?,?,?)',[
        ('buy',0,'cat',1,'1000000000','wallet',NOW-30,WSOL_MINT,int(NOW-30)),
        ('sell',0,'cat',0,'9000000000','wallet',NOW-20,WSOL_MINT,int(NOW-20)),
        ('future',0,'cat',1,'9000000000','wallet2',NOW+5,WSOL_MINT,int(NOW+5)),
        ('usdc',0,'cat',1,'9000000','wallet3',NOW-10,'other',int(NOW-10))]);c.commit();c.close()
    (d/'universe.heartbeat').write_text(json.dumps({'ts':NOW,'pumpswap_capture':True,'sol_usd':100.,'sol_age_s':0}))
    (d/'narrative_guess/worker.heartbeat.json').write_text(json.dumps({'ts':NOW,'error':None}))
    return d


def test_live_pass_is_causal_solana_only_and_keeps_v1_separate(tmp_path):
    d=source(tmp_path);legacy=d/'narrative/2026-10-30.db';legacy.parent.mkdir();legacy.write_bytes(b'legacy preserved')
    out=d/'narrative/live-v2';out.mkdir();store=N.Store(out,ddl=N.LIVE.DDL)
    try:
        result=N.LIVE.run_pass(store,d,NOW,{},host_now=NOW)
        assert result['tagged']==1 and result['protocol']=='narrative-live-v2'
        db=store.db(NOW)
        assert db.execute('SELECT mint,theme,launch_received_at,known_at FROM tags').fetchall()==[('cat','animal_cat',NOW-100,NOW)]
        assert db.execute("SELECT launches,band_entries,buy_usd,buyers,coverage FROM theme_agg WHERE theme='animal_cat' AND window_s=900").fetchone()==(1,None,100.,1,'observed_first_sightings_ps_lower_bound')
        assert legacy.read_bytes()==b'legacy preserved'
        assert N.LIVE.run_pass(store,d,NOW,{},host_now=NOW)['tagged']==0
        (d/'universe.heartbeat').write_text(json.dumps({'ts':NOW+11,'pumpswap_capture':True,'sol_usd':100.,'sol_age_s':0}))
        assert N.LIVE.run_pass(store,d,NOW+11,{},host_now=NOW+11)['tagged']==1
        assert db.execute("SELECT COUNT(*) FROM tags WHERE mint='future'").fetchone()[0]==1
    finally:store.close()


@pytest.mark.parametrize('failure',['missing','invalid','stale'])
def test_flow_failure_is_null_and_source_state_is_typed(tmp_path,failure):
    d=source(tmp_path);out=d/'narrative/live-v2';out.mkdir(parents=True);store=N.Store(out,ddl=N.LIVE.DDL)
    if failure=='missing':(d/'pumpswap/tape.db').unlink()
    elif failure=='invalid':
        c=sqlite3.connect(d/'pumpswap/tape.db');c.execute("UPDATE swaps_ps SET quote_atomic='bad' WHERE signature='buy'");c.commit();c.close()
    else:(d/'universe.heartbeat').write_text(json.dumps({'ts':NOW-20,'pumpswap_capture':True,'sol_usd':100.,'sol_age_s':0}))
    try:
        N.LIVE.run_pass(store,d,NOW,{},host_now=NOW)
        assert store.db(NOW).execute("SELECT buy_usd,buyers FROM theme_agg WHERE theme='animal_cat' AND window_s=900").fetchone()==(None,None)
        assert store.db(NOW).execute('SELECT flow_state FROM source_state').fetchone()[0]!='ok'
    finally:store.close()


def test_new_tokens_have_unknown_creators_and_bounded_cursor_bootstrap(tmp_path):
    d=source(tmp_path);c=sqlite3.connect(d/'narrative_guess/narrative_guess.db')
    c.execute('DELETE FROM launches');c.executemany('INSERT INTO launches VALUES(?,?,?,?,?,?,?)',[
        ('solana',str(i),'Kimchi','KIM',int(NOW//3600)*3600-100,None,'fomo') for i in range(8)]);c.commit();c.close()
    out=d/'narrative/live-v2';out.mkdir(parents=True);store=N.Store(out,ddl=N.LIVE.DDL)
    try:
        N.LIVE.run_pass(store,d,NOW,{},batch_size=3,host_now=NOW)
        assert store.db(NOW).execute('SELECT COUNT(*) FROM tags').fetchone()[0]==3
        row=store.db(NOW).execute("SELECT launches,creators FROM token_hourly WHERE token='kimchi' AND kind='name_token'").fetchone()
        assert row==(3,None)
        assert store.db(NOW).execute('SELECT bootstrap_limit FROM source_state').fetchone()[0]==3
    finally:store.close()


def test_readers_prefer_versioned_observations_and_preserve_judge_gate(tmp_path):
    from sniper.routes import desk_feeds_view as V
    from sniper.services import memecoin_desk_context_facts as C
    d=source(tmp_path);out=d/'narrative/live-v2';out.mkdir(parents=True)
    store=N.Store(out,ddl=N.LIVE.DDL)
    try:
        N.LIVE.run_pass(store,d,NOW,{},host_now=NOW)
        view=V.narrative(d)
        assert view['version']=='narrative-live-v2'
        assert view['source_state']['flow_state']=='ok'
        assert 'lower bound' in view['scope']
        part=C.narrative('cat',d,NOW+1)
        assert part['version']=='narrative-live-v2' and part['theme']=='animal_cat'
        assert part['theme_15m']=={'state':'unknown','reason':'curve_retired'}
        ctx=C.unknown_block('no_source');ctx['narrative']=part
        assert C.validate(ctx)==ctx
    finally:store.close()
