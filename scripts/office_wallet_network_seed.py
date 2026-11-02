#!/usr/bin/env python3
"""One read-only seed of data/office/wallet_network.db (Alpha 2026-10-31): old wallet graph edges and funders, mint_creators, cluster_funder_cache. Rows are source import, stale where the source stopped.
Sources open mode=ro. A rerun adds nothing it already holds. Import rows skip the daily row cap but stop at the disk cap. No provider calls.

    python3 scripts/office_wallet_network_seed.py [--state-root DIR]"""
import argparse
from datetime import datetime
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sniper.office.wallet_network import ADDRESS, EDGE_TYPES, WalletNetwork

BATCH = 5000


def _at(text):
    try: return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError): return None


def _ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def rows_from(root):
    """Yields (src, dst, type, observed_at, proof_kind, proof_ref, method, stale). Proof state stays none: an imported row was not checked against a stored fetch."""
    g = _ro(root / 'data/memecoin_wallet_graph.db')
    for src, dst, kind, first, version in g.execute("SELECT src,dst,edge_type,first_seen,method_version FROM memecoin_wallet_edges WHERE network_id='solana'"):
        yield src, dst, kind, _at(first), 'wallet_graph_edge', version, 'import-wallet-graph', 1
    for wallet, funder, sig, at in g.execute("SELECT wallet,funder,funding_sig,funded_at FROM memecoin_wallet_funders WHERE funder IS NOT NULL AND network_id='solana'"):
        yield wallet, funder, 'funded_by', at, 'signature' if sig else None, sig, 'import-wallet-graph', 1
    g.close()
    c = _ro(root / 'data/desk/cluster.db')
    for owner, funder, sig, at in c.execute('SELECT owner,funder,source_signature,resolved_at FROM cluster_funder_cache WHERE funder IS NOT NULL'):
        yield owner, funder, 'funded_by', _at(at), 'signature' if sig else None, sig, 'import-cluster-cache', 0
    c.close()
    o = _ro(root / 'data/office/outcomes.db')
    for creator, mint, source, at in o.execute("SELECT creator,mint,source,source_at FROM mint_creators WHERE network_id='solana'"):
        yield creator, mint, 'created_mint', at, 'source', source, 'import-mint-creators', 0
    o.close()


def seed(root, net):
    now, added, bad = net.clock(), 0, 0
    conn = net._conn()
    try:
        batch = 0
        for src, dst, kind, at, proof_kind, ref, method, stale in rows_from(root):
            if kind not in EDGE_TYPES or not isinstance(src, str) or not isinstance(dst, str) or not ADDRESS.match(src) or not ADDRESS.match(dst) or src == dst:
                bad += 1
                continue
            added += net._insert(conn, src=src, dst=dst, edge_type=kind, source='import', actor=None, observed_at=at, known_at=now, proof_kind=proof_kind, proof_ref=ref, proof_state='none', method=method, stale=stale)
            batch += 1
            if batch >= BATCH:
                conn.commit(); batch = 0
                if net._size() >= net.max_bytes:
                    return dict(state='refused', reason='disk_cap', added=added, malformed=bad)
        conn.commit()
    finally:
        conn.close()
    return dict(state='done', added=added, malformed=bad)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--state-root', default=str(Path(__file__).resolve().parents[1]))
    root = Path(ap.parse_args().state_root)
    print(seed(root, WalletNetwork(root / 'data/office/wallet_network.db')))


if __name__ == '__main__':
    main()
