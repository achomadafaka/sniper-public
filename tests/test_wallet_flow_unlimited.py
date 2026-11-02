"""Unlimited research universe, bounded frame memory and unchanged provider channel."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from sniper.office.wallet_stream_proof import decode_payload
from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_desk_universe as U

spec=importlib.util.spec_from_file_location('wallet_capture_helpers',Path(__file__).with_name('test_wallet_flow_capture.py'))
T=importlib.util.module_from_spec(spec);spec.loader.exec_module(T)
W=T.W


def test_selector_and_local_snapshot_do_not_stop_at_five_hundred_or_sixty_four_kib(tmp_path,monkeypatch):
    w=T.worker(tmp_path,monkeypatch);addresses=[U.b58encode(i.to_bytes(32,'little')) for i in range(2000,4000)]
    T.listed(w,addresses)
    assert len(Path(w.ps_ext_path).with_name('watch_wallets.json').read_bytes())>65536
    assert len(w.wallet_flow_selectors())==2000
    w.watch_tick(100.)
    assert len(w.ps_listed_watch)==2000
    assert len(W.build_request(pb,True,wallet_flow=addresses).transactions['wallet_flow'].account_include)==2000


def test_old_raw_is_not_deleted_and_new_raw_is_compressed_without_fixed_payload_stop(tmp_path,monkeypatch):
    w=T.worker(tmp_path,monkeypatch);raw=T.frame();w.wallet_flow_process(raw,received_at=100.)
    first=T.table(w,'wallet_flow_transactions')[0]
    assert json.loads(first[8])['encoding']=='zlib-v1' and decode_payload(first[8])==raw
    w.wallet_flow_maintenance(100.+31*86400)
    assert len(T.table(w,'wallet_flow_transactions'))==1
    w.wallet_flow_process(T.frame(4),received_at=101.)
    assert len(T.table(w,'wallet_flow_transactions'))==2
    assert not w.stats['wallet_flow_dropped']


def test_file_is_checked_each_quarter_second_requests_only_change_and_ping_each_ten(tmp_path,monkeypatch):
    w=T.worker(tmp_path,monkeypatch);T.listed(w,[T.A]);clock=[0.];sleeps=[]
    async def sleep(seconds):
        sleeps.append(seconds);clock[0]+=seconds
        if clock[0]>=.25:T.listed(w,[T.B])
    monkeypatch.setattr(W,'asyncio',SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(W,'time',SimpleNamespace(monotonic=lambda:clock[0],time=lambda:100.+clock[0]))
    async def run():
        gen=W.request_updates(w,pb,True)
        first=await anext(gen);assert first.transactions['wallet_flow'].account_include==[T.A]
        changed=await anext(gen);assert clock[0]==.25 and changed.transactions['wallet_flow'].account_include==[T.B]
        ping=await anext(gen);assert clock[0]==10. and ping.HasField('ping')
        ping2=await anext(gen);assert clock[0]==20. and ping2.HasField('ping')
        await gen.aclose()
    asyncio.run(run())
    assert set(sleeps)=={.25}


def test_program_local_copy_uses_current_alias_selectors_without_waiting_thirty_seconds(tmp_path,monkeypatch):
    w=T.worker(tmp_path,monkeypatch);w.ps_watch=frozenset();w.wallet_flow_desired=(T.B,)
    w.wallet_flow_local_copy(T.frame(),[bytes([2])*32],received_at=100.)
    assert len(w.wallet_flow_ring)==1
