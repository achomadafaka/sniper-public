import sqlite3

from .test_wallet_flow_consumer import source,insert,rows,NOW
from .test_wallet_token_flow_consumer import token_wire
from .test_wallet_flow_archive import compact as archive_rows
from sniper.office.wallet_stream_proof import decode_payload
from sniper.office.wallet_watch_collect import Collector


def test_real_archived_protobuf_reaches_the_same_token_proofs_without_rpc(source):
    net,chain,clock,desk=source;raw=token_wire().SerializeToString();insert(source,raw=raw)
    path=desk/'pumpswap/tape.db';directory=path.parent/'wallet-flow-archive'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE wallet_flow_meta(id INTEGER PRIMARY KEY,payload_bytes INTEGER,pending_metadata INTEGER)')
        c.execute('INSERT INTO wallet_flow_meta SELECT 1,sum(length(payload_json)),0 FROM wallet_flow_transactions')
    assert archive_rows(path,directory,before=NOW+1.)['archived']==1
    with sqlite3.connect(path) as c:payload=c.execute('SELECT payload_json FROM wallet_flow_transactions').fetchone()[0]
    assert decode_payload(payload,archive_dir=directory)==raw
    assert Collector(net,desk).consume_flows()['rows']==1
    assert len(rows(net.path,'watch_token_transfers'))==1
    assert rows(net.path,'watch_flow_receipts')[0]['state']=='known'
    assert not rows(chain.path,'calls')
