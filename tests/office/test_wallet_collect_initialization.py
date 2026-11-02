"""Public collection entry points survive busy initialization without moving durable cursors."""
import sqlite3

import pytest

from .test_wallet_flow_consumer import source,NOW
from sniper.office.wallet_watch_collect import Collector


@pytest.mark.parametrize('method,reason',[('consume_flows','flow_store_unknown'),('consume_setups','setup_store_unknown'),('tick','collect_store_unknown')])
@pytest.mark.parametrize('error',[sqlite3.OperationalError('database is locked'),OSError('store unavailable')])
def test_initialization_failure_is_typed_and_does_not_advance_cursors(source,monkeypatch,method,reason,error):
    net,chain,clock,desk=source;collector=Collector(net,desk)
    c=collector._open()
    with c:c.execute("INSERT INTO watch_collect_sources VALUES('setup',123,?,'partial','fixture')",(NOW,))
    before=c.execute('SELECT * FROM watch_collect_sources ORDER BY source').fetchall();c.close()
    def fail():raise error
    monkeypatch.setattr(collector,'_open',fail)
    out=getattr(collector,method)()
    assert out['state']=='unknown' and out['reason']==reason
    if method=='consume_flows':assert out['rows']==0
    elif method=='consume_setups':assert out['added']==0
    else:assert out['calls_attempted']==0
    with sqlite3.connect(net.path) as c:
        assert c.execute('SELECT * FROM watch_collect_sources ORDER BY source').fetchall()==[tuple(r) for r in before]


def test_partially_initialized_connection_is_closed(source,monkeypatch):
    net,chain,clock,desk=source;collector=Collector(net,desk);real=net._conn();closed=[]
    class Connection:
        def executescript(self,*args):raise sqlite3.OperationalError('database locked during schema initialization')
        def close(self):closed.append(True);real.close()
    monkeypatch.setattr(net,'_conn',lambda:Connection())
    out=collector.consume_flows()
    assert out['reason']=='flow_store_unknown' and closed==[True]


@pytest.mark.parametrize('method,reason',[('consume_setups','setup_store_unknown'),('tick','collect_store_unknown')])
def test_post_open_lock_is_typed_and_keeps_source_cursor(source,monkeypatch,method,reason):
    net,chain,clock,desk=source;collector=Collector(net,desk);real=collector._open()
    with real:real.execute("INSERT INTO watch_collect_sources VALUES('setup',123,?,'partial','fixture')",(NOW,))
    class Connection:
        def __getattr__(self,name):return getattr(real,name)
        def __enter__(self):real.__enter__();return self
        def __exit__(self,*args):return real.__exit__(*args)
        def execute(self,*args):raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(collector,'_open',lambda:Connection())
    out=getattr(collector,method)()
    assert out['state']=='unknown' and out['reason']==reason
    with sqlite3.connect(net.path) as c:assert c.execute("SELECT cursor FROM watch_collect_sources WHERE source='setup'").fetchone()==(123,)
