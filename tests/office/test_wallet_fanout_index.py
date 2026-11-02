import sqlite3
import time

from sniper.office.wallet_network import SCHEMA
from sniper.office.wallet_watch import FANOUT_HUB

QUERY = "SELECT count(DISTINCT src) FROM edges WHERE network_id='solana' AND type='funded_by' AND dst=? AND proof_state='verified'"


def test_verified_fanout_uses_partial_covering_index_without_changing_count(tmp_path):
    conn = sqlite3.connect(tmp_path / 'fanout.db')
    conn.executescript(SCHEMA)
    insert = "INSERT INTO edges(network_id,src,dst,type,source,known_at,proof_state,method_version,proof_ref) VALUES(?,?,?,?,?,0,?,'test',?)"
    rows = [('solana', f'import-{i}', 'hub', 'funded_by', 'import', 'none', str(i)) for i in range(20000)]
    rows += [('solana', f'verified-{i}', 'hub', 'funded_by', 'host', 'verified', str(i)) for i in range(FANOUT_HUB)]
    rows += [('solana', 'verified-0', 'hub', 'funded_by', 'host', 'verified', 'duplicate')]
    rows += [('8453', 'other-chain', 'hub', 'funded_by', 'host', 'verified', 'chain'), ('solana', 'other-type', 'hub', 'created_mint', 'host', 'verified', 'type')]
    conn.executemany(insert, rows)
    conn.commit()
    plan = ' '.join(str(r) for r in conn.execute('EXPLAIN QUERY PLAN ' + QUERY, ('hub',)))
    assert 'edges_verified_fanout' in plan
    assert 'USE TEMP B-TREE' not in plan
    indexed = conn.execute(QUERY, ('hub',)).fetchone()[0]
    baseline = conn.execute(QUERY.replace('FROM edges', 'FROM edges NOT INDEXED'), ('hub',)).fetchone()[0]
    assert indexed == baseline == FANOUT_HUB
    assert conn.execute(QUERY, ('absent',)).fetchone()[0] == 0
    start = time.perf_counter()
    for _ in range(100):
        conn.execute(QUERY, ('hub',)).fetchone()
    print(f'indexed_100_queries_s={time.perf_counter() - start:.6f}')
    conn.close()
