#!/usr/bin/env python3
"""Price evidenced desk costs off-path. Default: one bounded pass; --loop is explicit."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.persistence.db_core import get_connection, get_readonly_connection
from sniper.util.clock import real_utc_now_iso
from sniper.services.memecoin_desk_costs import price_execution_batch, read_solana_receipt, read_sol_usd_quote


def validate_read_rpc_url(url, env):
    """No defaults: reject known funded hosts and credentials without network/config mutation."""
    def host(value):
        try:
            parts = urlsplit(value)
            if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                    or parts.fragment or (parts.port is not None and parts.port < 1)):
                raise ValueError
            query = parse_qs(parts.query, keep_blank_values=True)
            credentials = query.get('api-key', []) + query.get('api_key', [])
            if len(credentials) > 1 or any(not key.strip() for key in credentials):
                raise ValueError
            return parts.hostname.rstrip('.').encode('idna').decode('ascii').lower()
        except (TypeError, ValueError, UnicodeError):
            raise ValueError('RPC configuration must use unambiguous HTTPS observation endpoints') from None

    candidate_host = host(url)
    funded = [u.strip() for u in (env.get('MEMECOIN_SOLANA_RPC_POOL_URLS') or '').split(',') if u.strip()]
    custody = (env.get('MEMECOIN_SOLANA_RPC_URL') or '').strip()
    if custody:
        funded.append(custody)
    if not funded:
        raise ValueError('funded RPC configuration is required to verify read isolation')
    for lane in ('MEMECOIN_SOLANA_READ_RPC_URL', 'MEMECOIN_SOL_TAPE_RPC_URL'):
        critical_read = (env.get(lane) or '').strip()
        if critical_read:
            funded.append(critical_read)
    # Funded pool, custody, read and tape hosts refuse by HOST. The watcher lane sits on a SHARED provider host
    # (mainnet.helius-rpc.com) that also serves separate research keys, so it refuses by CREDENTIAL only
    # (Alpha 2026-10-22: the host rule refused every research Helius key).
    watcher = (env.get('ONCHAIN_WATCH_HELIUS_RPC_URL') or '').strip()
    blocked_hosts = {'mainnet-beta.solana.com', 'api.mainnet-beta.solana.com'} | {host(u) for u in funded}
    if watcher:
        host(watcher)
    from sniper.services.memecoin_research_rpc import funded_overlap
    overlap = funded_overlap([url], {'MEMECOIN_SOLANA_RPC_POOL_URLS': ','.join(funded + ([watcher] if watcher else []))})
    if candidate_host in blocked_hosts or overlap:
        raise ValueError('read RPC overlaps a funded host or credential; use an isolated research endpoint')
    return url


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger-db', required=True)
    parser.add_argument('--owner', help='Public custody address; never a signer secret')
    parser.add_argument('--read-rpc-url', help='Explicit isolated research endpoint; no funded fallback')
    parser.add_argument('--from-env', action='store_true',
                        help="watchdog mode: load the state root's .env; owner = MEMECOIN_DESK_COST_OWNER, read RPC = "
                             "the first MEMECOIN_RESEARCH_HELIUS_RPC_URLS entry (still checked for funded overlap)")
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--loop', action='store_true')
    parser.add_argument('--interval-s', type=float, default=30)
    args = parser.parse_args(argv)
    if args.from_env:
        from sniper.util.envfiles import load_env_files
        from sniper.util.state_paths import state_root
        load_env_files(str(state_root()))
        args.owner = args.owner or (os.environ.get('MEMECOIN_DESK_COST_OWNER') or '').strip() or None
        urls = [u.strip() for u in (os.environ.get('MEMECOIN_RESEARCH_HELIUS_RPC_URLS') or '').split(',') if u.strip()]
        args.read_rpc_url = args.read_rpc_url or (urls[0] if urls else None)
    if not args.owner or not args.read_rpc_url:
        parser.error('--owner and --read-rpc-url are required (or --from-env with MEMECOIN_DESK_COST_OWNER and '
                     'MEMECOIN_RESEARCH_HELIUS_RPC_URLS set)')
    if not Path(args.ledger_db).is_file():
        parser.error('--ledger-db must exist; provision the reviewed schema separately')
    if not 1 <= args.limit <= 100 or not 5 <= args.interval_s <= 60:
        parser.error('--limit must be 1..100 and --interval-s must be 5..60')
    try:
        from solders.pubkey import Pubkey
        Pubkey.from_string(args.owner)
    except ValueError:
        parser.error('--owner must be a public Solana address')
    try:
        args.read_rpc_url = validate_read_rpc_url(args.read_rpc_url, dict(os.environ))
    except ValueError as exc:
        parser.error(str(exc))

    @lru_cache(maxsize=128)
    def quote_minute(minute):
        return read_sol_usd_quote(minute * 60, now_iso_fn=real_utc_now_iso)

    reader = get_readonly_connection(args.ledger_db)
    writer = None
    try:
        writer = get_connection(args.ledger_db)
        after = None
        while True:
            result = price_execution_batch(reader, writer, owner=args.owner,
                receipt_fn=lambda sig: read_solana_receipt(sig, rpc_url=args.read_rpc_url),
                usd_quote_fn=lambda block: quote_minute(block // 60), now_iso_fn=real_utc_now_iso,
                after=after, limit=args.limit)
            print(json.dumps(result, allow_nan=False), flush=True)
            if not args.loop:
                return 1 if any(row['state'] == 'deferred' for row in result['results']) else 0
            after = None if result['drained'] else result['next_cursor']
            time.sleep(args.interval_s)
    except KeyboardInterrupt:
        return 0
    finally:
        if writer is not None:
            writer.close()
        reader.close()


if __name__ == '__main__':
    raise SystemExit(main())
