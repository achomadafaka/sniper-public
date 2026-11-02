#!/usr/bin/env python3
"""Default-OFF operator worker for WO-14 token-authority observations.

This process only reads held funded Solana identities/RPC and writes the observation tables. It has
no import path into the exit brain or executor. Run explicitly; no service or runner starts it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sniper.execution.solana_wallet import _http  # noqa: E402
from sniper.persistence.db_core import get_connection  # noqa: E402
from sniper.services.memecoin_token_authority import collect_held_token_authority  # noqa: E402
from sniper.util.clock import real_utc_now_iso  # noqa: E402
from sniper.util.envfiles import load_env_files  # noqa: E402
from sniper.util.state_paths import state_root  # noqa: E402


def _fetcher(rpc_url):
    def fetch(mint, commitment):
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [mint, {"encoding": "jsonParsed", "commitment": commitment}],
        })
        _status, response, _date = _http("POST", rpc_url, body=body, timeout=10)
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict):
            return None, None
        return result, (result.get("context") or {}).get("slot")
    return fetch


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="collect one pass and exit")
    parser.add_argument("--interval-s", type=float, default=60.0)
    args = parser.parse_args(argv)
    if args.interval_s <= 0:
        parser.error("--interval-s must be positive")

    load_env_files(str(state_root()))
    from sniper.settings.memecoin import solana_read_rpc
    rpc_url = solana_read_rpc()
    if not rpc_url:
        raise SystemExit("MEMECOIN_SOLANA_READ_RPC_URL is unset")

    conn = get_connection()
    fetch = _fetcher(rpc_url)
    while True:
        result = collect_held_token_authority(
            conn, fetch, now_iso=real_utc_now_iso(), provider_label="solana_read_rpc",
            commitment="confirmed")
        print(json.dumps({"observed": len(result), "results": result}, sort_keys=True), flush=True)
        if args.once:
            break
        time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
