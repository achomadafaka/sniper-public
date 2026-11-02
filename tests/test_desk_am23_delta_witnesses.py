"""Delta HOLD d2e26800a witness (verbatim from ~/.cache/codex/delta-am23-d2e26800a-review/test_witness.py)."""
import sqlite3
import pytest
from sniper.services import memecoin_desk_ps_evidence as E

def test_conflicting_create_pool_beyond_query_limit_refuses():
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE ps_grads(pool,mint,quote_mint,slot,known_at)')
    c.execute('CREATE TABLE pools(pool,mint,quote_mint,canonical,known_at,signature,ordinal,PRIMARY KEY(signature,ordinal)) WITHOUT ROWID')
    c.execute('CREATE INDEX pools_pool ON pools(pool)')
    c.execute('INSERT INTO ps_grads VALUES(?,?,?,?,?)',('p','m',E.PT.WSOL_MINT,1,1.))
    c.executemany('INSERT INTO pools VALUES(?,?,?,?,?,?,?)', [('p','m',E.PT.WSOL_MINT,1,1.,f'a{i:03}',0) for i in range(101)])
    c.execute('INSERT INTO pools VALUES(?,?,?,?,?,?,?)',('p','WRONG',E.PT.WSOL_MINT,1,1.,'zzz',0))
    with pytest.raises(ValueError):
        E._curve_binding(c,'p','m',2,2.)
