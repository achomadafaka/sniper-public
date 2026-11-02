"""A sheet reuses its own fully validated loss interval; later calls read anew."""
import sqlite3

from sniper.services import memecoin_desk_ps_losses as L
from test_desk_ps_model_facts import tape, facts, NOW


def test_one_validated_loss_query_per_sheet_and_no_cross_call_cache(tmp_path,monkeypatch):
    cand,path=tape(tmp_path)
    statements=[]
    original=L._open
    def opened(root):
        conn=original(root);conn.set_trace_callback(statements.append);return conn
    monkeypatch.setattr(L,'_open',opened)
    out=facts(cand,tmp_path)
    assert out['flow_60s'] is not None
    query=lambda:sum(sql.startswith('SELECT l.signature,l.cause,l.received_at,l.slot FROM ps_decode_losses') for sql in statements)
    assert query()==1
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO gaps_ps VALUES(?,'disconnect',0)",(NOW-1,))
    assert facts(cand,tmp_path)['flow_60s']['global_loss_receipts']==1
    assert query()==2
