#!/usr/bin/env python3
"""Default-off confirmed CPMM research capture. No funded lane or ledger access."""
import asyncio
import json
import os
import signal
import sqlite3
import sys
import time
import urllib.request
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sniper.services import memecoin_desk_cpmm as C
from sniper.services import memecoin_desk_cpmm_capture as S


def config(env):
    if env.get('MEMECOIN_DESK_CPMM_ENABLED','0')!='1':return None
    names=('MEMECOIN_DESK_CPMM_GRPC_HOST','MEMECOIN_DESK_CPMM_GRPC_TOKEN','MEMECOIN_DESK_CPMM_READ_RPC_URL')
    if any(not env.get(n) for n in names):raise ValueError('explicit_research_credentials_required')
    host,token,rpc=(env[n] for n in names)
    if '://' in host or '/' in host or not rpc.startswith('https://'):raise ValueError('research_endpoint_shape')
    # Same credential/host identity as funded_overlap, without importing execution modules.
    def identity(url):
        parts=urlsplit(url.strip());params=parse_qs(parts.query)
        key=(params.get('api-key') or params.get('api_key') or [''])[0]
        return ('key',key) if key else ('host',parts.hostname)
    funded=[env.get(n,'') for n in ('MEMECOIN_SOLANA_RPC_URL','SOLANA_RPC_URL','MEMECOIN_RH_FAST_DEPOSIT_HELIUS_URL')]
    funded+=env.get('MEMECOIN_SOLANA_RPC_POOL_URLS','').split(',')
    if (host==env.get('MEMECOIN_SHYFT_GRPC_HOST') or token==env.get('MEMECOIN_SHYFT_GRPC_TOKEN')
            or identity(rpc) in {identity(u) for u in funded if u.strip()}):
        raise ValueError('funded_endpoint_forbidden')
    cap=int(env.get('MEMECOIN_DESK_CPMM_RPC_DAILY_CAP','120'))
    if not 1<=cap<=1000:raise ValueError('research_budget')
    return {'host':host,'token':token,'rpc':rpc,'daily_cap':cap}


def lookup(url,pool,mints,now):
    body={'jsonrpc':'2.0','id':1,'method':'getMultipleAccounts','params':[[pool,*mints],{'encoding':'base64','commitment':'confirmed'}]}
    req=urllib.request.Request(url,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=2) as response:
        raw=response.read(131073)
    if len(raw)>131072:raise C.DecodeLoss('rpc_response_size')
    payload=json.loads(raw)
    if not isinstance(payload,dict) or payload.get('error') or not isinstance(payload.get('result'),dict):
        raise C.DecodeLoss('rpc_response_error')
    result=payload['result'];values=result.get('value')
    if not isinstance(values,list) or len(values)!=3:raise C.DecodeLoss('rpc_partial')
    return C.pool_binding(pool,dict(zip([pool,*mints],values)),time.time(),result['context']['slot'])


def read_offset(path,now):
    try:
        data=json.loads(Path(path).read_text())
        value,at=data['offset_s'],data['at']
        if S.finite(value) and S.finite(at) and 0<=now-at<=86400:return value
    except (OSError,ValueError,TypeError,KeyError):pass
    return None


def build_request(pb):
    return pb.SubscribeRequest(transactions={'cpmm':pb.SubscribeRequestFilterTransactions(vote=False,failed=False,account_include=[C.PROGRAM])},
                               blocks_meta={'meta':pb.SubscribeRequestFilterBlocksMeta()},commitment=pb.CommitmentLevel.CONFIRMED)


async def run(cfg,desk_dir):
    import grpc
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    stop=asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,stop.set)
    with S.Store(desk_dir) as store:
        capture=S.Capture(store)
        process=S.Processor(store,lookup=lambda pool,mints,now:lookup(cfg['rpc'],pool,mints,now),daily_cap=cfg['daily_cap'])
        status={'connected':False,'error':None}
        # Only this processor touches SQLite. A separate receiver can keep draining during lookup.
        async def receiver():
            backoff=1
            while not stop.is_set():
                try:
                    async def requests():
                        yield build_request(pb)
                        while not stop.is_set():
                            await asyncio.sleep(10)
                            yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=1))
                    async with grpc.aio.secure_channel(cfg['host']+':443',grpc.ssl_channel_credentials(),options=[('grpc.max_receive_message_length',4_000_000)]) as channel:
                        call=channel.stream_stream('/geyser.Geyser/Subscribe',request_serializer=pb.SubscribeRequest.SerializeToString,response_deserializer=lambda b:b)
                        capture.offer(b'',time.time()) # ordered reconnect barrier; a new coverage interval
                        status['connected']=True
                        async for raw in call(requests(),metadata=(('x-token',cfg['token']),)):
                            capture.offer(raw,time.time());backoff=1
                            if stop.is_set():break
                    if not stop.is_set():raise OSError('stream_closed')
                except Exception:
                    status.update(connected=False,error='stream_disconnected')
                    capture.pending_gap=capture.pending_gap or time.time()
                    try:await asyncio.wait_for(stop.wait(),timeout=backoff)
                    except TimeoutError:pass
                    backoff=min(60,backoff*2)

        async def processor():
            beat_at=0
            while not stop.is_set():
                item=capture.pop();now=time.time()
                try:
                    if item:
                        raw,at=item
                        if not raw:
                            old=store.head()['through']
                            if old is not None:store.gap('reconnect',old,at)
                            store.begin(at)
                        else:
                            update=pb.SubscribeUpdate.FromString(raw);kind=update.WhichOneof('update_oneof')
                            if kind=='transaction':
                                frame=S.transaction_frame(update,received_at=at,clock_offset_s=status.get('clock_offset_s'))
                                result=process.decode(frame)
                                for pool,mints in process.requests(result,now):
                                    try:
                                        binding=await asyncio.to_thread(process.lookup,pool,mints,now)
                                        if binding['known_at']<now:raise C.DecodeLoss('binding_backdated')
                                        process.lookup_succeeded(binding)
                                    except (OSError,ValueError,KeyError,TypeError) as exc:
                                        process.lookup_failed(exc,at,time.time(),pool)
                            elif kind=='block_meta':
                                capture.flush_loss(max(at,time.time()))
                                store.fence(update.block_meta.slot,at)
                    else:await asyncio.sleep(.005)
                    if now-beat_at>=1:
                        beat_at=now
                        status['clock_offset_s']=read_offset(Path(desk_dir).parent/'clock_offset.json',now)
                        payload={'version':C.VERSION,'ts':now,**status,**store.head(),'queue_bytes':capture.bytes,'gap_pending':capture.pending_gap is not None}
                        target=store.path.parent/'heartbeat.json';temp=target.with_suffix('.tmp')
                        temp.write_text(S.dump(payload));temp.replace(target)
                except (S.StorageFull,sqlite3.Error,OSError):
                    status.update(connected=False,error='storage_failed');stop.set();raise
                except Exception:
                    store.gap('process_error',item[1] if item else now,time.time())
            store.conn.commit()
        tasks=[asyncio.create_task(receiver()),asyncio.create_task(processor())]
        try:
            done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done:task.result()
        finally:
            stop.set()
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)


def main():
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root,require_state_root_outside_release
    load_env_files(str(state_root()))
    cfg=config(os.environ)
    if cfg is None:return 0
    require_state_root_outside_release()
    asyncio.run(run(cfg,Path(state_root())/'data/desk'))
    return 0


if __name__=='__main__':raise SystemExit(main())
