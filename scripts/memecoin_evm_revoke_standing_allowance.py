#!/usr/bin/env python3
"""Revoke the standing quote-token allowance of a chain's pinned spenders (rollback, 2026-10-07;
--chain 8453 for Base's Permit2 since 2026-10-15).

Spec docs/superpowers/specs/2026-10-07-bsc-standing-allowance-design.md. Reads the quote token's
allowance(wallet, spender) (WBNB on 56, WETH on 8453) for each pinned spender and, with --confirm,
sends approve(spender, 0) for every one above zero, one at a time, each confirmed before the next.

Dry run by default: nothing is signed or sent without --confirm, which the operator runs.
Refused while MEMECOIN_EVM_STANDING_ALLOWANCE_CHAINS still names the chain (the runner would top
the allowance back up), while the chain's wallet lock is held, or while a funded reservation is open.

  python3 scripts/memecoin_evm_revoke_standing_allowance.py [--chain 56|8453]
  python3 scripts/memecoin_evm_revoke_standing_allowance.py [--chain 56|8453] --confirm
"""
import argparse
import os
from pathlib import Path

SEL_ALLOWANCE = "dd62ed3e"


def _pad(addr):
    return str(addr).lower().replace("0x", "").rjust(64, "0")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--chain", default="56", choices=("56", "8453", "4663"))
    parser.add_argument("--state-root", default="/home/bigpapi/Desktop/Sniper")
    args = parser.parse_args()
    chain = args.chain

    root = Path(args.state_root)
    from sniper.util.envfiles import load_env_files
    load_env_files(str(root))
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_standing_allowance import PINNED_SPENDERS
    from sniper.execution.evm_wallet import CHAIN_QUOTE, load_keypair, _rpc
    from sniper.execution.evm_real_seams import (encode_approve, eth_confirm, eth_gas_price, eth_nonce,
                                                 eth_send_raw, sign_tx)

    rpc = (os.environ.get("MEMECOIN_EVM_RPC_URL_%s" % chain) or "").strip().strip("'\"")
    if not rpc:
        raise ValueError("MEMECOIN_EVM_RPC_URL_%s must be set" % chain)
    if int(_rpc(rpc, "eth_chainId", [])["result"], 16) != int(chain):
        raise ValueError("RPC does not serve chain %s" % chain)
    token = CHAIN_QUOTE[chain]["token"].lower()
    acct = load_keypair(cfg.MEMECOIN_EVM_KEYPAIR)
    wallet = acct.address

    live = []
    for spender in sorted(PINNED_SPENDERS[chain]):
        answer = _rpc(rpc, "eth_call", [{"to": token, "data": "0x" + SEL_ALLOWANCE + _pad(wallet) + _pad(spender)},
                                        "latest"])
        allowance = int(answer["result"], 16)
        print("spender %s allowance %d atoms (%.6f quote token)" % (spender, allowance, allowance / 1e18))
        if allowance > 0:
            live.append(spender)
    if not live:
        print("Nothing to revoke.")
        return
    print("Would send approve(spender, 0) for: %s" % ", ".join(live))
    if not args.confirm:
        print("Dry run. Nothing signed or sent. Add --confirm to revoke.")
        return
    if chain in cfg.memecoin_evm_standing_allowance_chains():
        raise ValueError("MEMECOIN_EVM_STANDING_ALLOWANCE_CHAINS still names %s: the runner would top "
                         "the allowance back up. Turn it off and restart first; nothing sent." % chain)

    from sniper.execution.exec_lock import evm_exec_lock_key, wallet_exec_lock
    from sniper.persistence.db_core import get_readonly_connection
    with wallet_exec_lock(evm_exec_lock_key(chain, wallet), lock_dir=str(root / "data"),
                          blocking=False) as held:
        if not held:
            raise ValueError("the chain-%s wallet lock is held (an execution is in flight); nothing sent" % chain)
        conn = get_readonly_connection(str(root / "sniper_tracker.db"))
        try:
            pending = conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved'"
                                   " AND network_id=?", (chain,)).fetchone()[0]
        finally:
            conn.close()
        if pending:
            raise ValueError("a funded %s reservation is in flight; nothing sent" % chain)
        for spender in live:
            nonce, gas_price = eth_nonce(wallet, rpc), eth_gas_price(rpc)
            if nonce is None or gas_price is None:
                raise ValueError("nonce or gas price unreadable; stopped before %s" % spender)
            signed = sign_tx({"to": token, "data": encode_approve(spender, 0), "value": 0, "gas": 60000,
                              "gasPrice": int(gas_price), "nonce": nonce, "chainId": int(chain)}, acct)
            if not signed:
                raise ValueError("signing failed; stopped before %s" % spender)
            raw, tx_hash = signed
            print("revoke %s: sending %s (nonce %d)" % (spender, tx_hash, nonce), flush=True)
            eth_send_raw(raw, rpc)
            status = eth_confirm(tx_hash, rpc, max_attempts=60, poll_s=1.0)
            print("revoke %s: %s" % (spender, status), flush=True)
            if status != "confirmed":
                raise ValueError("revoke %s not confirmed (%s); stopped" % (spender, status))


if __name__ == "__main__":
    main()
