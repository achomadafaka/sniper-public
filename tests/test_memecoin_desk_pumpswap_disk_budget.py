"""A PS research disk budget cannot consume the curve tape's size budget."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_memecoin_desk_pumpswap_worker import W, worker, frame


def sizes(base, extra=(), exclude=()):
    if Path(base).name=='pumpswap': return 2e9
    assert Path(base)/'pumpswap' in exclude
    return .1e9


def test_ps_size_cap_disables_only_ps_and_preserves_curve_queue(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch)
    w.push(b'curve',received_at=1)
    w.push(b'ps',received_at=1,pumpswap=True)
    monkeypatch.setattr(W,'desk_bytes',sizes)
    monkeypatch.setattr(W.shutil,'disk_usage',lambda _:SimpleNamespace(free=100e9))
    w.check_disk()
    assert not w.stop and not w.ps_capture and w.ps_book is None
    assert w.ring and not w.ps_ring and w.ps_ring_bytes==0
    assert w.stats['ps_capture_stopped']==1
    assert w.files.conn('gaps_ps',W.time.time()).execute('SELECT cause FROM gaps_ps').fetchall()==[('disk_cap',)]
    w.check_disk()
    assert w.stats['ps_capture_stopped']==1


@pytest.mark.parametrize('alert_raises',[False,True])
def test_ps_disk_stop_attempts_one_alert_without_stopping_curve(tmp_path,monkeypatch,alert_raises):
    w=worker(tmp_path,monkeypatch)
    alerts=[]
    def alert(kind,text):
        alerts.append((kind,text))
        if alert_raises: raise RuntimeError('alert unavailable')
    w.alert_fn=alert
    w.push(b'curve',received_at=1)
    monkeypatch.setattr(W,'desk_bytes',sizes)
    monkeypatch.setattr(W.shutil,'disk_usage',lambda _:SimpleNamespace(free=100e9))
    w.check_disk()
    w.check_disk()
    assert len(alerts)==1 and alerts[0][0]=='desk-universe-ps-disk'
    assert 'restart' in alerts[0][1]
    assert not w.stop and not w.ps_capture and w.ring
    assert w.stats['ps_alert_errors']==int(alert_raises)


def test_ps_stop_keeps_dropping_subscribed_ps_frames_without_reconnect(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch)
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_CENSUS','0')
    w.census=False
    monkeypatch.setattr(W,'desk_bytes',sizes)
    monkeypatch.setattr(W.shutil,'disk_usage',lambda _:SimpleNamespace(free=100e9))
    received=[]
    def push(raw,**kwargs):
        received.append(raw)
        if len(received)==1: w.check_disk()
        else: w.stop=True
    monkeypatch.setattr(w,'push',push)
    monkeypatch.setattr(w,'heartbeat',lambda _:None)
    pump, ps=frame(names=('pump',)),frame(names=('pumpswap',))
    async def responses(*args,**kwargs):
        for raw in (pump,ps,pump): yield raw
    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        def stream_stream(self,*args,**kwargs): return responses
    monkeypatch.setitem(sys.modules,'grpc',SimpleNamespace(aio=SimpleNamespace(secure_channel=lambda *a,**k:Channel()),ssl_channel_credentials=lambda:None))
    asyncio.run(W.run(w,'offline.invalid','fake'))
    assert received==[pump,pump] and w.stats['ps_tx']==1


@pytest.mark.parametrize('cap',['0','-1','nan','inf','bad'])
def test_enabled_ps_capture_requires_a_finite_positive_byte_budget(tmp_path,monkeypatch,cap):
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_CAPTURE','1')
    cfg=W.knobs({});cfg['ps_hard_gb']=cap
    with pytest.raises(ValueError,match='disk cap'):
        W.Worker(cfg,base=tmp_path,sol_price_fn=lambda:None,alert_fn=lambda *a:None)


@pytest.mark.parametrize('free_gb,stopped',[(20.5,False),(19.5,True)])
def test_ps_yields_free_space_headroom_without_weakening_absolute_free_floor(tmp_path,monkeypatch,free_gb,stopped):
    w=worker(tmp_path,monkeypatch)
    monkeypatch.setattr(W,'desk_bytes',lambda *a:.1e9)
    monkeypatch.setattr(W.shutil,'disk_usage',lambda _:SimpleNamespace(free=free_gb*1e9))
    w.check_disk()
    assert not w.ps_capture and w.stop is stopped


def test_ps_artifacts_are_excluded_from_the_curve_size_sum(tmp_path):
    ps=tmp_path/'pumpswap';ps.mkdir()
    (ps/'tape.db').write_bytes(b'x'*1000)
    curve=tmp_path/'swaps';curve.mkdir()
    (curve/'day.db').write_bytes(b'x'*7)
    assert W.desk_bytes(tmp_path)==1007
    assert W.desk_bytes(tmp_path,exclude=(ps,))==7


def test_absolute_disk_stop_precedes_any_ps_cap_commit_failure(tmp_path,monkeypatch):
    import sqlite3
    w=worker(tmp_path,monkeypatch)
    monkeypatch.setattr(W,'desk_bytes',lambda *a:21e9)
    monkeypatch.setattr(W.shutil,'disk_usage',lambda _:SimpleNamespace(free=100e9))
    def fail(): raise sqlite3.OperationalError('curve commit unavailable')
    monkeypatch.setattr(w,'commit_files',fail)
    with pytest.raises(sqlite3.OperationalError): w.check_disk()
    assert w.stop is True
