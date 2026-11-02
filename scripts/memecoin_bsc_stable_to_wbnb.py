#!/usr/bin/env python3
"""Convert bridged BSC stablecoin into the WBNB bankroll the funded runner spends.

WHY THIS EXISTS. Chain 56 quotes in WBNB (`CHAIN_QUOTE["56"]`, 2026-09-06): the census found WBNB
holding 67.9% of pool liquidity against USDT's 18.9%, and the wrapped-native shape is what every
existing funded path already speaks. A stablecoin bankroll would need a stable -> WBNB leg on every
route and a stable -> BNB gas top-up that does not exist. This script is the one-shot conversion,
run by the OPERATOR, never by the runner and never by a deploy.

DECIMALS ARE READ FROM THE CHAIN, NEVER ASSUMED, and that is not defensive padding. BSC's USDT
(BSC-USD, 0x55d3983...) and its Binance-Peg USDC are BOTH 18-DECIMAL, unlike the 6-decimal USDC on
Base and Ethereum. The sibling script `memecoin_base_usdc_to_weth.py` hardcodes `10 ** 6`, which is
correct there and would be a 10^12 sizing error here. `CHAIN_QUOTE` carries this same trap in its
comments: the 6-decimal assumption is why BSC was held out of trading for months.

SAFETY MODEL
  - DRY-RUN by default. It quotes live, prints the exact route, and signs nothing. `--confirm` is
    required before any key is loaded for signing.
  - The recipient is ALWAYS our own custody address, derived from our own keypair. It is never an
    argument, so a typo cannot send funds to a stranger.
  - It refuses while a funded reservation is in flight. A reservation is sized against this same
    balance, so converting under it can strand an entry the runner has already committed to.
  - It refuses if the amount exceeds the spendable balance, or if native BNB is below the gas floor.
  - `amountOutMinimum` comes from the PancakeSwap V3 quoter through the shared `dex_v3_quote`, at
    the funded entry slippage setting and this chain's measured fee-tier order (1% first). The
    router, the calldata and the min-out are all built by the code the funded lane uses.

    python3 scripts/memecoin_bsc_stable_to_wbnb.py --amount-usd 300            # inspect
    python3 scripts/memecoin_bsc_stable_to_wbnb.py --amount-usd 300 --confirm  # move real money
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

BSC_CHAIN = 56
# Approve plus swap with headroom. BSC gas is cheap; the operator's own working reserve is separate
# and this script does not touch it.
MIN_GAS_BNB = 0.002
APPROVE_GAS = 90_000
SWAP_GAS_FALLBACK = 500_000


def pending_reservations(db_path):
    """Funded reservations still in flight, or None when the table does not exist."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='memecoin_pending_entries'").fetchone():
            return None
        return int(conn.execute(
            "SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved'"
        ).fetchone()[0])
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(
        description="Swap a BSC stablecoin -> WBNB through the configured PancakeSwap V3 router.")
    ap.add_argument("--amount-usd", type=float, required=True,
                    help="stablecoin amount to convert (exact input)")
    ap.add_argument("--from", dest="source", choices=("usdt", "usdc"), default="usdt",
                    help="which stable to spend (default usdt, the chain's dominant pair)")
    ap.add_argument("--confirm", action="store_true",
                    help="SIGN + broadcast real money. Without it this is a dry run.")
    ap.add_argument("--reserve-usd", type=float, default=0.0,
                    help="stablecoin to leave behind (default 0)")
    ap.add_argument("--to", choices=("wbnb", "native"), default="wbnb",
                    help="wbnb (default) = leave as WBNB bankroll; native = unwrap to native BNB "
                         "gas right after the swap (WBNB.withdraw on the exact amount received)")
    ap.add_argument("--state-root", default=str(REPO))
    args = ap.parse_args()

    load_env_files(args.state_root)
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import (
        CHAIN_QUOTE, CHAIN_STABLE, CHAIN_USDC, load_keypair, native_balance,
        erc20_balance, erc20_decimals, _rpc)
    from sniper.execution.evm_dex_quote import dex_v3_quote

    chain = str(BSC_CHAIN)
    quote_meta = CHAIN_QUOTE[chain]
    wbnb = quote_meta["token"]
    if str(quote_meta.get("kind") or "").lower() != "eth":
        sys.exit("REFUSE: chain 56 quote currency is not wrapped native; this script has no purpose")
    stable = CHAIN_STABLE[chain] if args.source == "usdt" else CHAIN_USDC[chain]

    acct = load_keypair(cfg.MEMECOIN_EVM_KEYPAIR)
    addr = acct.address
    rpc = cfg.evm_rpc_for_chain(chain)
    if not rpc:
        sys.exit("REFUSE: no BSC RPC configured (MEMECOIN_EVM_RPC_URL_56)")

    # FROM THE CHAIN, NOT FROM A CONSTANT. BSC stables are 18-decimal where Base/Ethereum USDC is 6.
    stable_dec = erc20_decimals(stable, rpc)
    if stable_dec is None:
        sys.exit("REFUSE: could not read the stablecoin's decimals; refusing to assume")
    stable_dec = int(stable_dec)
    wbnb_dec = int(quote_meta["decimals"])

    gas_bnb = native_balance(addr, rpc)
    stable_bal = erc20_balance(addr, stable, rpc, decimals=stable_dec)
    wbnb_bal = erc20_balance(addr, wbnb, rpc, decimals=wbnb_dec)
    print(f"wallet {addr} on BSC ({BSC_CHAIN})")
    print(f"  gas {gas_bnb:.6f} BNB | {args.source.upper()} {stable_bal:.2f} "
          f"({stable_dec} decimals) | WBNB {wbnb_bal:.6f}")
    dest_label = "WBNB" if args.to == "wbnb" else "native BNB (unwrapped after the swap)"
    print(f"  plan: swap {args.amount_usd:.2f} {args.source.upper()} -> {dest_label} (exact input)\n")

    if args.amount_usd <= 0:
        sys.exit("REFUSE: --amount-usd must be positive")
    spendable = stable_bal - max(0.0, args.reserve_usd)
    if args.amount_usd > spendable + 1e-9:
        sys.exit(f"REFUSE: --amount-usd {args.amount_usd} exceeds spendable {spendable:.2f}")
    if gas_bnb < MIN_GAS_BNB:
        sys.exit(f"REFUSE: gas {gas_bnb:.6f} BNB < {MIN_GAS_BNB} for approve+swap")

    db_path = os.getenv("DB_PATH") or str(Path(args.state_root) / "sniper_tracker.db")
    reserved = pending_reservations(db_path)
    if reserved:
        sys.exit(f"REFUSE: {reserved} funded reservation(s) in flight; they are sized against "
                 "this balance. Wait for them to resolve.")

    in_atomic = int(round(args.amount_usd * (10 ** stable_dec)))
    quote = dex_v3_quote(BSC_CHAIN, stable, wbnb, in_atomic, addr, http=None)
    if not quote or not quote.get("buyAmount"):
        sys.exit(f"REFUSE: no PancakeSwap V3 route quoted {args.source.upper()} -> WBNB")

    out_atomic = int(quote["buyAmount"])
    min_atomic = int(quote["minBuyAmount"])
    out_wbnb = out_atomic / (10 ** wbnb_dec)
    route = quote.get("route") or {}
    tx = quote["transaction"]
    spender = ((quote.get("issues") or {}).get("allowance") or {}).get("spender")
    print("PancakeSwap V3 quote:")
    print(f"  route      {route.get('protocol')} {' -> '.join(route.get('tokens') or [])} "
          f"fees={route.get('fees')}  (tier order {cfg.memecoin_evm_v3_fee_tiers(chain)})")
    print(f"  in         {args.amount_usd:.2f} {args.source.upper()} ({in_atomic} atomic)")
    print(f"  out        {out_wbnb:.8f} WBNB ({out_atomic} atomic)")
    print(f"  min out    {min_atomic / (10 ** wbnb_dec):.8f} WBNB, "
          f"slippage {cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT}%")
    if out_wbnb:
        print(f"  implied    {args.amount_usd / out_wbnb:,.2f} {args.source.upper()} per WBNB")
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
        sys.exit("REFUSE: could not read BSC gas price")
    gas_price = int(gas_price * 1.25)

    def send_step(label, to, data, gas):
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
                          "gasPrice": gas_price, "nonce": nonce, "chainId": BSC_CHAIN}, acct)
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

    steps = []
    if spender:
        steps.append(("approve", stable,
                      "0x" + SEL_APPROVE + _pad_addr(spender) + format(in_atomic, "064x"),
                      APPROVE_GAS))
    steps.append(("swap", tx["to"], tx["data"], None))
    for label, to, data, gas in steps:
        send_step(label, to, data, gas)

    if args.to == "native":
        # Unwrap the EXACT amount actually received, not the pre-swap quote estimate -- slippage
        # can move the real fill away from `out_atomic`. WBNB is a standard WETH9-shape contract:
        # withdraw(uint256) selector 0x2e1a7d4d, verified this session (keccak256 computed, not
        # assumed), burns WBNB 1:1 for native BNB sent to msg.sender.
        received_atomic = int(round(
            erc20_balance(addr, wbnb, rpc, decimals=wbnb_dec) * (10 ** wbnb_dec)
        )) - int(round(wbnb_bal * (10 ** wbnb_dec)))
        if received_atomic <= 0:
            sys.exit("REFUSE: swap confirmed but WBNB balance did not increase -- refusing to "
                     "unwrap a non-positive amount")
        withdraw_data = "0x2e1a7d4d" + format(received_atomic, "064x")
        send_step("unwrap", wbnb, withdraw_data, None)

    print(f"\nDone. WBNB balance now "
          f"{erc20_balance(addr, wbnb, rpc, decimals=wbnb_dec):.6f} (was {wbnb_bal:.6f}). "
          f"Native gas now {native_balance(addr, rpc):.6f} BNB (was {gas_bnb:.6f}).")


if __name__ == "__main__":
    main()
