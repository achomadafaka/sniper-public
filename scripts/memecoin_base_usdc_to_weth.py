#!/usr/bin/env python3
"""Convert bridged Base USDC into the WETH bankroll the funded runner spends.

WHY THIS EXISTS. The Solana -> Base bridge delivers USDC, and the Base funded lane quotes in WETH
(`CHAIN_QUOTE["8453"]`, 2026-09-06): Base memecoin pools are native/WETH-paired, so a USDC bankroll
would need a USDC -> WETH leg on every route and a USDC -> ETH gas top-up that does not exist. This
script is the one-shot conversion between the two, run by the OPERATOR, never by the runner and
never by a deploy.

SAFETY MODEL
  - DRY-RUN by default. It quotes live, prints the exact route, and signs nothing. `--confirm` is
    required before any key is loaded for signing.
  - The recipient is ALWAYS our own custody address, derived from our own keypair. It is never an
    argument, so a typo cannot send funds to a stranger.
  - It refuses while a funded reservation is in flight. A reservation is sized against this same
    balance, so converting under it can strand an entry the runner has already committed to.
  - It refuses if the amount exceeds the spendable USDC, or if native ETH is below the gas floor.
  - `amountOutMinimum` comes from QuoterV2 through the shared `dex_v3_quote`, at the funded entry
    slippage setting. The router, the calldata and the min-out are all built by the same code the
    funded lane uses; nothing here is hand-encoded.

    python3 scripts/memecoin_base_usdc_to_weth.py --amount-usd 300            # inspect
    python3 scripts/memecoin_base_usdc_to_weth.py --amount-usd 300 --confirm  # move real money
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sniper.util.envfiles import load_env_files

BASE_CHAIN = 8453
# The gas floor is the approval plus the swap with headroom, not a bankroll reserve. Base fees are
# a fraction of a cent, so this is small on purpose; the operator's own 0.02 ETH working reserve is
# a separate number and this script does not touch it.
MIN_BASE_GAS_ETH = 0.0005
APPROVE_GAS = 80_000
SWAP_GAS_FALLBACK = 400_000


def pending_reservations(db_path):
    """Funded reservations still in flight, or None when the table does not exist."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memecoin_pending_entries'"
        ).fetchone()
        if not row:
            return None
        return int(conn.execute(
            "SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved'"
        ).fetchone()[0])
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(
        description="Swap Base USDC -> WETH through the configured Uniswap V3 router.")
    ap.add_argument("--amount-usd", type=float, required=True,
                    help="USDC amount to convert (exact input)")
    ap.add_argument("--confirm", action="store_true",
                    help="SIGN + broadcast real money. Without it this is a dry run.")
    ap.add_argument("--reserve-usd", type=float, default=0.0,
                    help="USDC to leave behind (default 0)")
    ap.add_argument("--state-root", default=str(REPO))
    args = ap.parse_args()

    load_env_files(args.state_root)
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import (
        CHAIN_QUOTE, CHAIN_USDC, load_keypair, native_balance, erc20_balance, erc20_decimals, _rpc)
    from sniper.execution.evm_dex_quote import dex_v3_quote

    usdc = CHAIN_USDC[str(BASE_CHAIN)]
    quote_meta = CHAIN_QUOTE[str(BASE_CHAIN)]
    weth = quote_meta["token"]
    if str(quote_meta.get("kind") or "").lower() != "eth":
        sys.exit("REFUSE: Base quote currency is not wrapped native; this script has no purpose")

    acct = load_keypair(cfg.MEMECOIN_EVM_KEYPAIR)
    addr = acct.address
    rpc = cfg.evm_rpc_for_chain(str(BASE_CHAIN))
    if not rpc:
        sys.exit("REFUSE: no Base RPC configured (MEMECOIN_EVM_RPC_URL_8453)")

    # FROM THE CHAIN, NOT FROM A CONSTANT (2026-09-06). This read `10 ** 6` below, which is correct
    # for Base USDC and correct only BY COINCIDENCE OF CHAIN. Mirroring this script onto BSC nearly
    # carried that constant across, where both USDT and Binance-Peg USDC are 18-DECIMAL — a $300
    # funding call would have asked to move 300 TRILLION base units. The same 6-decimal assumption
    # is why BSC sat out of trading for months, recorded in `CHAIN_QUOTE`'s own comments. The scale
    # is a property of the token, so ask the token.
    usdc_dec = erc20_decimals(usdc, rpc)
    if usdc_dec is None:
        sys.exit("REFUSE: could not read the stablecoin's decimals; refusing to assume")
    usdc_dec = int(usdc_dec)
    gas_eth = native_balance(addr, rpc)
    usdc_bal = erc20_balance(addr, usdc, rpc, decimals=usdc_dec)
    weth_bal = erc20_balance(addr, weth, rpc)
    print(f"wallet {addr} on Base ({BASE_CHAIN})")
    print(f"  gas {gas_eth:.6f} ETH | USDC {usdc_bal:.2f} ({usdc_dec} decimals) "
          f"| WETH {weth_bal:.6f}")
    print(f"  plan: swap {args.amount_usd:.2f} USDC -> WETH (exact input)\n")

    if args.amount_usd <= 0:
        sys.exit("REFUSE: --amount-usd must be positive")
    spendable = usdc_bal - max(0.0, args.reserve_usd)
    if args.amount_usd > spendable + 1e-9:
        sys.exit(f"REFUSE: --amount-usd {args.amount_usd} exceeds spendable USDC {spendable:.2f}")
    if gas_eth < MIN_BASE_GAS_ETH:
        sys.exit(f"REFUSE: Base gas {gas_eth:.6f} ETH < {MIN_BASE_GAS_ETH} for approve+swap")

    db_path = os.getenv("DB_PATH") or str(Path(args.state_root) / "sniper_tracker.db")
    reserved = pending_reservations(db_path)
    if reserved:
        sys.exit(f"REFUSE: {reserved} funded reservation(s) in flight; they are sized against "
                 "this balance. Wait for them to resolve.")

    decimals = int(CHAIN_QUOTE.get(str(BASE_CHAIN), {}).get("decimals") or 18)
    in_atomic = int(round(args.amount_usd * (10 ** usdc_dec)))
    quote = dex_v3_quote(BASE_CHAIN, usdc, weth, in_atomic, addr, http=None)
    if not quote or not quote.get("buyAmount"):
        sys.exit("REFUSE: no Uniswap V3 route quoted USDC -> WETH on Base")

    out_atomic = int(quote["buyAmount"])
    min_atomic = int(quote["minBuyAmount"])
    out_weth = out_atomic / (10 ** decimals)
    min_weth = min_atomic / (10 ** decimals)
    route = quote.get("route") or {}
    tx = quote["transaction"]
    spender = ((quote.get("issues") or {}).get("allowance") or {}).get("spender")
    print("Uniswap V3 quote:")
    print(f"  route      {route.get('protocol')} {' -> '.join(route.get('tokens') or [])} "
          f"fees={route.get('fees')}")
    print(f"  in         {args.amount_usd:.2f} USDC ({in_atomic} atomic)")
    print(f"  out        {out_weth:.8f} WETH ({out_atomic} atomic)")
    print(f"  min out    {min_weth:.8f} WETH ({min_atomic} atomic), "
          f"slippage {cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT}%")
    print(f"  implied    {args.amount_usd / out_weth:,.2f} USDC per WETH" if out_weth else "")
    print(f"  router     {tx['to']}  data={tx['data'][:18]}… ({len(tx['data']) // 2}B)")

    if not args.confirm:
        print("\nDRY-RUN — nothing signed. Re-run with --confirm to broadcast real money.")
        return

    from sniper.execution.evm_real_seams import (eth_nonce, eth_gas_price, sign_tx,
                                                 eth_send_raw, eth_confirm)
    from sniper.execution.evm_wallet import SEL_APPROVE, _pad_addr

    print("\n--confirm: broadcasting real money.")
    gas_price = eth_gas_price(rpc)
    if gas_price is None:
        sys.exit("REFUSE: could not read Base gas price")
    gas_price = int(gas_price * 1.25)

    steps = []
    if spender:
        approve_data = ("0x" + SEL_APPROVE + _pad_addr(spender)
                        + format(in_atomic, "064x"))
        steps.append(("approve", usdc, approve_data, APPROVE_GAS))
    steps.append(("swap", tx["to"], tx["data"], None))

    for label, to, data, gas in steps:
        nonce = eth_nonce(addr, rpc)
        if nonce is None:
            sys.exit(f"REFUSE: could not read nonce before {label}")
        if not gas:
            try:
                est = _rpc(rpc, "eth_estimateGas",
                           [{"from": addr, "to": to, "value": "0x0", "data": data}])
                gas = int(int(est["result"], 16) * 1.3)
            except Exception:
                gas = SWAP_GAS_FALLBACK
        signed = sign_tx({"to": to, "data": data, "value": 0, "gas": int(gas),
                          "gasPrice": gas_price, "nonce": nonce, "chainId": BASE_CHAIN}, acct)
        if not signed:
            sys.exit(f"REFUSE: signing failed at {label}")
        raw, tx_hash = signed
        print(f"  {label}: sending {tx_hash} (gas={gas})…")
        sent = eth_send_raw(raw, rpc)
        if sent is None:
            sys.exit(f"STOP: broadcast failed at {label} — nothing further sent")
        status = eth_confirm(sent, rpc, max_attempts=90, poll_s=2.0)
        print(f"      {sent} -> {status}")
        if status != "confirmed":
            sys.exit(f"STOP: {label} did not confirm ({status}) — nothing further sent")

    print(f"\nDone. WETH balance now "
          f"{erc20_balance(addr, weth, rpc):.6f} (was {weth_bal:.6f}).")


if __name__ == "__main__":
    main()
