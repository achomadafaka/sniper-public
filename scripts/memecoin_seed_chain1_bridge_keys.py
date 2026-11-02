#!/usr/bin/env python3
"""Seed the two chain-1 USDC/WETH V4 bridge PoolKeys into the durable key store, once.

BUNKER / PAYZ (2026-10-07): v4_multihop's chain-1 bridge leg asks the keys of two USDC/WETH pools
initialised long before the pages our custody RPC serves (block 22,972,018 and 24,319,186 against a
head of ~26.1M). Without a stored key each runner restart re-walks them on Infura (the in-process
miss memo does not survive a restart), drawing 429s that then fail the balance reads. The keys
below were read from each pool's own PoolManager Initialize event on a full-range-serving host, and
each pool id is re-derived here from its key fields (keccak(abi.encode(PoolKey))) before anything
is written; a key that does not hash back to its id is refused.

    python3 scripts/memecoin_seed_chain1_bridge_keys.py            # dry run: verify, print
    python3 scripts/memecoin_seed_chain1_bridge_keys.py --confirm  # write the missing rows

The operator runs --confirm. Writes only memecoin_evm_v4_pool_keys rows (immutable keys; INSERT OR
REPLACE with identical content is a no-op in effect). ROLLBACK: delete the two rows by pool_id.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHAIN = "1"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
KEYS = {
    # Initialize at block 22,972,018: dynamic-fee (0x800000) hooked pool.
    "0xe500210c7ea6bfd9f69dce044b09ef384ec2b34832f132baec3b418208e3a657": {
        "currency0": USDC, "currency1": WETH, "fee": 8388608, "tick_spacing": 10,
        "hooks": "0x0000000aa232009084bd71a5797d089aa4edfad4"},
    # Initialize at block 24,319,186: fee 200, tick spacing 4, no hooks.
    "0x5a5c7cab5f55c7ea020e97d4fa6dd5d99270e56ce76afa61d8cbddec0af92060": {
        "currency0": USDC, "currency1": WETH, "fee": 200, "tick_spacing": 4,
        "hooks": "0x0000000000000000000000000000000000000000"},
}


def verified_keys():
    """[(pool_id, key)] whose key hashes back to its id; raises on any mismatch."""
    from sniper.execution.evm_v4_launchpad import pool_id_for_key
    out = []
    for pool_id, key in KEYS.items():
        derived = pool_id_for_key(key).lower()
        if derived != pool_id:
            raise SystemExit("REFUSE: %s does not hash back to its key (derived %s)" % (pool_id, derived))
        out.append((pool_id, dict(key)))
    return out


def run(conn, *, confirm):
    from sniper.execution.evm_v4_quote import V4
    from sniper.persistence import db_memecoin as m
    manager = V4[CHAIN]["pool_manager"]
    lines, written = [], 0
    for pool_id, key in verified_keys():
        stored = m.get_evm_v4_pool_key(conn, chain_id=CHAIN, pool_id=pool_id, pool_manager=manager)
        if stored is not None and {k: str(v).lower() for k, v in stored.items() if k in key} == {
                k: str(v).lower() for k, v in key.items()}:
            lines.append("%s: already stored" % pool_id[:12])
            continue
        if not confirm:
            lines.append("%s: would store fee=%s tick_spacing=%s hooks=%s" % (
                pool_id[:12], key["fee"], key["tick_spacing"], key["hooks"][:10]))
            continue
        m.put_evm_v4_pool_key(conn, chain_id=CHAIN, pool_id=pool_id, pool_manager=manager, key=key)
        back = m.get_evm_v4_pool_key(conn, chain_id=CHAIN, pool_id=pool_id, pool_manager=manager)
        if back is None:
            raise SystemExit("FAILED: %s not readable after the write" % pool_id)
        written += 1
        lines.append("%s: stored" % pool_id[:12])
    return written, lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--confirm", action="store_true")
    a = ap.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import configure_memecoin_state_env, state_root
    load_env_files(str(state_root()))
    configure_memecoin_state_env()
    from sniper.persistence.db_core import get_connection, get_readonly_connection
    conn = get_connection() if a.confirm else get_readonly_connection(str(state_root() / "sniper_tracker.db"))
    try:
        written, lines = run(conn, confirm=a.confirm)
    finally:
        conn.close()
    for line in lines:
        print(line)
    print("DRY RUN: nothing written. Re-run with --confirm." if not a.confirm else "wrote %d" % written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
