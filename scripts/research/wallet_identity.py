#!/usr/bin/env python3
"""Publish account/address research from existing local evidence; zero RPC calls."""
import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))


def refresh_identity(ledger_path,graph_path,output_path,*,now_iso=None,own_addresses=()):
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.persistence.db_wallet_identity import load_identity_inputs
    from sniper.services.wallet_identity import build_identity_report,publish_identity,_at
    from sniper.util.clock import real_utc_now_iso
    if Path(output_path).suffix!='.json': raise ValueError('identity output must be a JSON artifact')
    now_iso=now_iso or real_utc_now_iso(path=Path(ledger_path).parent/'data/clock_offset.json')
    ledger=get_readonly_connection(str(Path(ledger_path).resolve())); graph=None
    try:
        if Path(graph_path).exists(): graph=get_readonly_connection(str(Path(graph_path).resolve()))
        data=load_identity_inputs(ledger,graph,since_iso=(_at(now_iso)-timedelta(days=14)).isoformat(),now_iso=now_iso)
    finally:
        ledger.close()
        if graph is not None: graph.close()
    report=build_identity_report(data,now_iso=now_iso,own_addresses=own_addresses)
    publish_identity(output_path,report)
    return dict(users=len(report['users']),related_wallets=sum(len(c['related_wallets'] or []) for u in report['users'].values()
        for c in u['chains'].values()),coverage=report['coverage'],rpc_calls=0,output=str(output_path))


def main(argv=None):
    from sniper.util.state_paths import state_root
    state=state_root()
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ledger',type=Path,default=state/'sniper_tracker.db')
    p.add_argument('--graph',type=Path,default=state/'data/memecoin_wallet_graph.db')
    p.add_argument('--out',type=Path,default=state/'data/wallet_identity.json')
    args=p.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    load_env_files(str(state),names=('.env',))
    from sniper.services.memecoin_wallet_map import own_address_set
    from sniper.settings.memecoin import MEMECOIN_OWN_ADDRESSES
    print(json.dumps(refresh_identity(args.ledger,args.graph,args.out,
                                     own_addresses=own_address_set(MEMECOIN_OWN_ADDRESSES))))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
