"""A fresh heartbeat cannot certify rows that the collector has not committed."""
import json
import sqlite3

import pytest
from sniper.util import clock
from test_memecoin_desk_universe_worker import frame_meta
from test_memecoin_desk_pumpswap_worker import W, worker


@pytest.fixture(autouse=True)
def corrected_clock(monkeypatch):
    monkeypatch.setattr(clock, 'offset_s', lambda: 0.)


def source_block(w, at):
    w.process(frame_meta(int(at), int(at)), received_at=at)


def test_individual_decode_loss_window_uses_an_indexed_range(tmp_path):
    files = W.DayFiles(tmp_path)
    conn = files.conn("ps_decode_losses", 100.)
    try:
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT received_at FROM ps_decode_losses "
                            "WHERE received_at>=? AND received_at<? LIMIT ?", (50., 100., 20001)).fetchall()
        assert any("SEARCH" in row[-1] for row in plan), plan
    finally:
        conn.close()


def test_startup_does_not_build_a_loss_index_on_an_existing_source_table(tmp_path):
    path = tmp_path / "pumpswap" / "tape.db"
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE ps_decode_losses(signature TEXT,cause TEXT,slot INTEGER,received_at REAL, "
                     "PRIMARY KEY(signature,cause)) WITHOUT ROWID")
        conn.execute("INSERT INTO ps_decode_losses VALUES('old','decode_loss',1,50.)")
    files = W.DayFiles(tmp_path)
    conn = files.conn("ps_decode_losses", 100.)
    try:
        assert not any(row[1] == "ps_decode_losses_received" for row in conn.execute("PRAGMA index_list(ps_decode_losses)"))
        assert conn.execute("SELECT received_at FROM ps_decode_losses").fetchall() == [(50.,)]
    finally:
        conn.close()


def heartbeat(w):
    w.heartbeat(True)
    return json.loads((w.base/'universe.heartbeat').read_text())


def test_watermark_is_unknown_until_commit_and_uses_the_precommit_clock(tmp_path,monkeypatch):
    clock=[100.]
    monkeypatch.setattr(W.time,'time',lambda:clock[0])
    w=worker(tmp_path,monkeypatch)
    assert heartbeat(w).get('ps_committed_through') is None
    source_block(w,100.)
    def commit(**kwargs):
        assert w.ps_committed_through is None
        clock[0]=101.
    monkeypatch.setattr(w.files,'commit',commit)
    w.commit_files()
    beat=heartbeat(w)
    assert beat['ps_committed_through']==100. and beat['ts']==101.


@pytest.mark.parametrize('ps',[False,True])
def test_queued_receipt_prevents_watermark_advance(tmp_path,monkeypatch,ps):
    clock=[100.];monkeypatch.setattr(W.time,'time',lambda:clock[0])
    w=worker(tmp_path,monkeypatch);source_block(w,100.);w.commit_files()
    clock[0]=102.;w.push(b'pending',received_at=101.,pumpswap=ps)
    w.commit_files()
    assert heartbeat(w)['ps_committed_through']==100.
    w.ring.clear();w.ps_ring.clear()
    clock[0]=103.;w.commit_files()
    assert heartbeat(w)['ps_committed_through']==100.                 # draining alone is not new source progress
    source_block(w,103.);w.commit_files()
    assert heartbeat(w)['ps_committed_through']==103.


def test_ps_commit_failure_never_advances_the_watermark(tmp_path,monkeypatch):
    clock=[100.];monkeypatch.setattr(W.time,'time',lambda:clock[0])
    w=worker(tmp_path,monkeypatch);source_block(w,100.);w.commit_files();clock[0]=102.
    monkeypatch.setattr(w.files,'commit',lambda **kw:kw['ps_error'](sqlite3.OperationalError('disk')))
    w.commit_files()
    beat=heartbeat(w)
    assert beat['ps_committed_through'] is None and beat['ps_gap_pending'] is True


def test_capture_off_never_claims_a_ps_commit(tmp_path,monkeypatch):
    monkeypatch.setattr(W.time,'time',lambda:100.)
    w=worker(tmp_path,monkeypatch,enabled=False);source_block(w,100.);w.commit_files()
    assert heartbeat(w).get('ps_committed_through') is None


def _assert_receive_stamp_to_enqueue_and_commit_are_synchronous():
    import ast
    import inspect
    import textwrap
    tree=ast.parse(inspect.getsource(W.run))
    loop=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFor))
    start=next(i for i,n in enumerate(loop.body) if isinstance(n,ast.Assign)
               and any(isinstance(t,ast.Name) and t.id=='received_at' for t in n.targets))
    end=next(i for i,n in enumerate(loop.body) if isinstance(n,ast.Expr)
             and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Attribute)
             and n.value.func.attr=='queue_frame')
    handoffs={'to_thread','run_in_executor','Thread','start_new_thread','create_task','call_soon_threadsafe'}
    segments=[ast.Module(body=loop.body[start:end+1],type_ignores=[])]
    for name in ('queue_frame','push','push_wallet_flow','census_frame','wallet_flow_local_copy','commit_files'):
        segments.append(ast.parse(textwrap.dedent(inspect.getsource(getattr(W.Worker,name)))))
    for segment in segments:
        assert not any(isinstance(n,ast.Await) for n in ast.walk(segment))
        assert not any(isinstance(n,ast.Call) and
            (getattr(n.func,'attr',None) in handoffs or getattr(n.func,'id',None) in handoffs)
            for n in ast.walk(segment))



def test_receive_stamp_to_enqueue_and_commit_are_synchronous():
    _assert_receive_stamp_to_enqueue_and_commit_are_synchronous()


@pytest.mark.parametrize('name',['queue_frame','push','push_wallet_flow','commit_files'])
@pytest.mark.parametrize('handoff',['await','task'])
def test_synchronous_contract_rejects_handoffs_inside_queue_helpers(monkeypatch,name,handoff):
    async def yielding(*args,**kwargs):
        await W.asyncio.sleep(0)
    def scheduled(*args,**kwargs):
        W.asyncio.create_task(None)
    monkeypatch.setattr(W.Worker,name,yielding if handoff=='await' else scheduled)
    with pytest.raises(AssertionError):
        _assert_receive_stamp_to_enqueue_and_commit_are_synchronous()


def test_receiver_cannot_publish_a_cut_between_receipt_and_enqueue(tmp_path,monkeypatch):
    import asyncio
    import sys
    from types import SimpleNamespace
    w=worker(tmp_path,monkeypatch)
    monkeypatch.setattr(w,'heartbeat',lambda *_:None)
    monkeypatch.setattr(w,'census_frame',lambda _:False)
    original=w.push
    def push(raw,**kwargs):
        original(raw,**kwargs);w.stop=True
    monkeypatch.setattr(w,'push',push)
    async def responses(*args,**kwargs):
        yield b'offline-frame'
    class Channel:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def stream_stream(self,*args,**kwargs):return responses
    monkeypatch.setitem(sys.modules,'grpc',SimpleNamespace(
        aio=SimpleNamespace(secure_channel=lambda *a,**k:Channel()),ssl_channel_credentials=lambda:None))
    async def scenario():
        stamped=asyncio.Event();seen=[]
        def clock():
            stamped.set();return 101.
        monkeypatch.setattr(W.time,'time',clock)
        async def competing_commit():
            await stamped.wait()
            w.commit_files()
            seen.append((bool(w.ring),w.ps_committed_through))
        task=asyncio.create_task(competing_commit())
        await asyncio.sleep(0)
        await W.run(w,'offline.invalid','fake')
        await task
        assert seen==[(True,None)]
    asyncio.run(scenario())


def test_new_write_error_blocks_publication_even_if_the_gap_flag_was_cleared(tmp_path,monkeypatch):
    clock=[100.];monkeypatch.setattr(W.time,'time',lambda:clock[0])
    w=worker(tmp_path,monkeypatch);source_block(w,100.);w.commit_files();clock[0]=102.
    source_block(w,102.)
    def commit(**kwargs):
        w.stats['ps_write_errors']+=1
        w.ps_pending_gaps.clear()
    monkeypatch.setattr(w.files,'commit',commit)
    w.commit_files()
    assert heartbeat(w)['ps_committed_through'] is None
