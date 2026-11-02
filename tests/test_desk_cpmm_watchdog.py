import importlib.util
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def worker():
    spec=importlib.util.spec_from_file_location('desk_cpmm_worker',ROOT/'scripts/memecoin_desk_cpmm_worker.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def test_disabled_worker_never_opens_storage_or_network(tmp_path,monkeypatch):
    w=worker();monkeypatch.setenv('MEMECOIN_DESK_CPMM_ENABLED','0')
    assert w.main()==0
    assert not (tmp_path/'cpmm').exists()


def test_explicit_research_credentials_required_and_default_off():
    w=worker()
    assert w.config({}) is None
    for env in ({'MEMECOIN_DESK_CPMM_ENABLED':'1'},
                {'MEMECOIN_DESK_CPMM_ENABLED':'1','MEMECOIN_SHYFT_GRPC_TOKEN':'funded'}):
        try:w.config(env)
        except ValueError:pass
        else:raise AssertionError('funded credentials must not be fallback')


def test_watchdog_uses_own_identity_lock_and_pinned_code():
    s=(ROOT/'scripts/memecoin_desk_cpmm_watchdog.sh').read_text()
    assert 'memecoin_desk_cpmm_worker.py' in s and 'flock' in s
    assert '.runtime' in s and 'SNIPER_STATE_ROOT' in s and 'CODE_ROOT' in s
    assert 'pkill' not in s and '--confirm' not in s


def test_missing_or_nonfinite_clock_file_does_not_claim_zero_offset(tmp_path):
    import json
    w=worker();p=tmp_path/'clock.json'
    assert w.read_offset(p,1000) is None
    for data in ({'offset_s':0,'at':999},{'offset_s':float('nan'),'at':999},{'offset_s':7,'at':1001}):
        p.write_text(json.dumps(data))
        assert w.read_offset(p,1000)==(0 if data['offset_s']==0 else None)


def test_watchdog_current_identity_and_superseded_release_do_not_start(tmp_path):
    import os
    import subprocess
    proc=tmp_path/'proc';p=proc/'99999999';p.mkdir(parents=True)
    (p/'environ').write_bytes(('SNIPER_STATE_ROOT='+str(tmp_path)+'\0').encode())
    env={**os.environ,'SNIPER_STATE_ROOT':str(tmp_path),'DESK_CPMM_PROC_ROOT':str(proc),'SNIPER_PYTHON':'/bin/false'}
    script=ROOT/'scripts/memecoin_desk_cpmm_watchdog.sh'
    for code,workerpath in [(0,str(ROOT/'scripts/memecoin_desk_cpmm_worker.py')),(1,'/old/release/scripts/memecoin_desk_cpmm_worker.py')]:
        (p/'cmdline').write_bytes(('python3\0'+workerpath+'\0').encode())
        result=subprocess.run(['bash',str(script)],env=env,capture_output=True)
        assert result.returncode==code,result.stderr
        assert not (tmp_path/'data/desk/cpmm').exists()


def test_confirmed_subscription_contains_only_cpmm_program():
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    w=worker();req=w.build_request(pb)
    assert req.commitment==pb.CommitmentLevel.CONFIRMED
    assert list(req.transactions)==['cpmm']
    assert list(req.transactions['cpmm'].account_include)==[w.C.PROGRAM]
    assert req.transactions['cpmm'].HasField('failed') and not req.transactions['cpmm'].failed


def test_new_capture_has_no_funded_imports():
    import ast
    paths=[ROOT/'sniper/services'/('memecoin_desk_cpmm'+suffix+'.py') for suffix in ('','_capture','_tape')]
    paths.append(ROOT/'scripts/memecoin_desk_cpmm_worker.py')
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            names=[node.module or ''] if isinstance(node,ast.ImportFrom) else [a.name for a in node.names] if isinstance(node,ast.Import) else []
            assert not any(n.startswith('sniper.execution') or 'memecoin_real' in n or 'slot_lane' in n for n in names)


def test_async_receiver_processor_commits_a_confirmed_fence_without_network(tmp_path,monkeypatch):
    import asyncio
    import base64
    import json
    import sqlite3
    import sys
    from types import SimpleNamespace
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    w=worker();f=json.loads((ROOT/'tests/fixtures/desk_cpmm_transaction.json').read_text())['frame']
    tx=pb.SubscribeUpdate();info=tx.transaction.transaction;tx.transaction.slot=f['slot']
    info.signature=bytes(Signature.from_string(f['signature']));info.index=7
    msg=info.transaction.message;msg.header.num_required_signatures=f['signer_count']
    msg.account_keys.extend(bytes(Pubkey.from_string(k)) for k in f['account_keys'])
    for ix in f['instructions']:
        p=msg.instructions.add();p.program_id_index=ix['program_id_index'];p.accounts=bytes(ix['accounts']);p.data=base64.b64decode(ix['data_b64'])
    info.meta.log_messages.extend(f['logs'])
    for name in ('pre_token_balances','post_token_balances'):
        for row in f[name]:
            b=getattr(info.meta,name).add();b.account_index=row['account_index'];b.mint=row['mint'];b.owner=row['owner'];b.program_id=row['program_id'];b.ui_token_amount.decimals=row['decimals']
    meta=pb.SubscribeUpdate();meta.block_meta.slot=f['slot'];meta.block_meta.block_time.timestamp=100
    async def frames():
        yield tx.SerializeToString();yield meta.SerializeToString()
        await asyncio.sleep(10)
    class Channel:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def stream_stream(self,*args,**kwargs):return lambda *a,**k:frames()
    monkeypatch.setitem(sys.modules,'grpc',SimpleNamespace(aio=SimpleNamespace(secure_channel=lambda *a,**k:Channel()),ssl_channel_credentials=lambda:None))
    monkeypatch.setattr(w,'lookup',lambda *args:(_ for _ in ()).throw(TimeoutError()))
    async def exercise():
        try:await asyncio.wait_for(w.run({'host':'research.invalid','token':'test','rpc':'https://research.invalid','daily_cap':1},tmp_path),timeout=.2)
        except TimeoutError:pass
    asyncio.run(exercise())
    conn=sqlite3.connect(tmp_path/'cpmm/tape.db')
    try:
        assert conn.execute('SELECT count(*) FROM swaps').fetchone()[0]==1
        assert conn.execute('SELECT slot,through FROM head').fetchone()[0]==f['slot']
        assert conn.execute("SELECT cause FROM gaps WHERE cause='rpc_timeout'").fetchone()
    finally:conn.close()


def test_research_rpc_rejects_all_funded_pool_endpoints_and_key_aliases():
    import pytest
    w=worker()
    cfg={'MEMECOIN_DESK_CPMM_ENABLED':'1','MEMECOIN_DESK_CPMM_GRPC_HOST':'research.invalid',
         'MEMECOIN_DESK_CPMM_GRPC_TOKEN':'separate','MEMECOIN_DESK_CPMM_READ_RPC_URL':'https://research.invalid/?api-key=shared',
         'MEMECOIN_SOLANA_RPC_POOL_URLS':'https://other.invalid/?api_key=shared, https://funded-pool.invalid'}
    with pytest.raises(ValueError,match='funded_endpoint_forbidden'):w.config(cfg)
    cfg['MEMECOIN_DESK_CPMM_READ_RPC_URL']='https://funded-pool.invalid'
    with pytest.raises(ValueError,match='funded_endpoint_forbidden'):w.config(cfg)
