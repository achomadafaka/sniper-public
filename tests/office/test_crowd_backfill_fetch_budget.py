"""Bound cooperative SQL materialization without changing the legacy page contract."""
import sqlite3
from contextlib import closing
from sniper.office import large_buyer_arm as A


def test_cooperative_fetch_is_bounded_and_resumes_without_dropping_rows(tmp_path):
    at=A.START_AT+100
    tape=sqlite3.connect(':memory:')
    tape.execute('CREATE TABLE swaps_ps(signature,ordinal,pool,mint,actor,is_buy,quote_atomic,quote_mint,received_at,slot,tx_index,pre_quote_reserve_atomic,post_quote_reserve_atomic,post_base_reserve_atomic)')
    tape.executemany('INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        [(f's{i:05}',0,'P','M','W',1,'1','other',at,i+1,1,'1','1','1') for i in range(2501)])
    tape.commit()
    with closing(A.open_db(tmp_path/'cooperative.db')) as c:
        first=A.ingest(c,tape,at+1,should_yield=lambda:True)
        assert first['source_rows']<=2000  # a 50k fetch consumed the entire live soft budget
        assert first['processed']==1 and first['partial']
        for _ in range(3):
            result=A.ingest(c,tape,at+1,should_yield=lambda:False)
            assert result['source_rows']<=2000
            if not result['partial']:break
        assert not result['partial']
        assert c.execute('SELECT count(*) FROM seen').fetchone()[0]==2501
    with closing(A.open_db(tmp_path/'legacy.db')) as c:
        assert A.ingest(c,tape,at+1)['source_rows']==2501
    tape.close()
