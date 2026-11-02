#!/usr/bin/env python3
"""One-shot: approve a dedicated HL API wallet (agent) from the master account and save it.

SDK 0.24.0 contract (verified): Exchange.approve_agent(name=None) -> (response, agent_key)
— the SDK generates the agent keypair; we persist it. The master key (MEMECOIN_EVM_KEYPAIR,
the funded 0x898B… account) is loaded ONLY in this one-shot script, never by the runner.
The runner signs with the saved API wallet; HL nonce state is per-signer.

  python3 scripts/hl_real_setup_agent.py            # mainnet
  python3 scripts/hl_real_setup_agent.py --testnet  # testnet approval

Writes data/hl_api_wallet.json {"address", "private_key"} chmod 0600 and prints the
.env lines to set. Refuses to overwrite an existing wallet file without --force.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MAINNET = "https://api.hyperliquid.xyz"
TESTNET = "https://api.hyperliquid-testnet.xyz"


def save_wallet_file(path: str, address: str, private_key: str, *, force: bool = False) -> None:
    """Persist {"address","private_key"} at `path` with 0600 perms."""
    if os.path.exists(path) and not force:
        raise FileExistsError(f"{path} exists — pass --force to overwrite (a live leg "
                              f"may be signing with it)")
    with open(path, "w") as f:
        json.dump({"address": address, "private_key": private_key}, f)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)        # 0600


def main(argv=None) -> int:
    from dotenv import load_dotenv
    load_dotenv("/home/bigpapi/Desktop/Sniper/.env")
    ap = argparse.ArgumentParser(description="Approve + save the HL API wallet")
    ap.add_argument("--testnet", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--out", default="data/hl_api_wallet.json")
    args = ap.parse_args(argv)

    if os.path.exists(args.out) and not args.force:
        print(f"{args.out} exists — pass --force to overwrite", file=sys.stderr)
        return 1

    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from sniper.settings import memecoin as mcfg
    with open(mcfg.MEMECOIN_EVM_KEYPAIR) as f:
        master_key = json.load(f)["private_key"]
    master = Account.from_key(master_key)
    base = TESTNET if args.testnet else MAINNET
    ex = Exchange(master, base)
    resp, agent_key = ex.approve_agent("sniper-hl-real")
    status = resp.get("status") if isinstance(resp, dict) else None
    if status != "ok":
        print(f"approve_agent FAILED: {resp}", file=sys.stderr)
        return 1
    agent = Account.from_key(agent_key)
    save_wallet_file(args.out, agent.address, agent_key, force=args.force)
    print(f"agent approved + saved: {agent.address} -> {args.out} (0600)")
    print("\nSet in .env:")
    print(f"HL_REAL_API_WALLET_FILE={args.out}")
    print(f"HL_REAL_ACCOUNT_ADDRESS={master.address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
