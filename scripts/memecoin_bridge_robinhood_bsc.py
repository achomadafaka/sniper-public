#!/usr/bin/env python3
"""Move value from the funded Robinhood (4663) wallet to USDC on BSC (56) via Relay.link.

Robinhood (4663) holds no USDC/USDG of its own — the custody wallet's only balances there are
native ETH (gas) and WETH (CHAIN_QUOTE["4663"], the trading bankroll). So this bridges FROM WETH,
and asks Relay for an EXACT destination amount of USDC on BSC (CHAIN_USDC["56"],
0x8AC76a51...) rather than an exact WETH input — `tradeType: EXACT_OUTPUT`, the mirror of every
sibling script in this family, which all specify the ORIGIN amount because their origin already
holds the asset being sent. Origin is EVM (4663), so Relay returns `to`/`data`/`value` steps we
sign with the existing seams — no new signing code, same as memecoin_bridge_robinhood_solana.py.

DECIMALS ARE READ FROM THE CHAIN, NEVER ASSUMED. BSC's Binance-Peg USDC is 18-decimal (verified
2026-09-06, CHAIN_USDC/CHAIN_QUOTE comments) where Base and Ethereum USDC are 6 — the exact trap
that held BSC out of trading for months. This script calls erc20_decimals() on the destination
token and refuses rather than guess if the chain does not answer.

SAFETY MODEL (same as its siblings)
  - DRY-RUN by default; --confirm required to move real money.
  - Recipient is our own BSC address (same private key, EVM-wide), never an argument.
  - The quote's echoed recipient is compared against our address before anything is signed.
  - Refuses if the quoted WETH input exceeds what 4663 actually holds, or if native ETH on 4663 is
    too low to pay for approve+deposit.
  - Every step must target 4663; a step on any other chain refuses the whole run.

    python3 scripts/memecoin_bridge_robinhood_bsc.py --usd 40 --dry-run
    python3 scripts/memecoin_bridge_robinhood_bsc.py --usd 40 --confirm
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.util.envfiles import load_env_files

ROBINHOOD_CHAIN = 4663
BSC_CHAIN = 56
ROBINHOOD_WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
BSC_USDC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"
RELAY_QUOTE_URL = "https://api.relay.link/quote"
# ponytail: flat native-gas floor for approve+deposit on 4663; measured cost is a few cents
MIN_ROBINHOOD_GAS_ETH = 0.0003
GAS_FALLBACK = {"approve": 120_000, "deposit": 600_000}


def _http_post_json(url, body, timeout=40):
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _to_int(v, default=0):
    if v is None:
        return default
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.startswith("0x") else int(s)


def relay_quote_exact_output(user, dest_amount_atomic):
    """Live Relay quote: EXACT `dest_amount_atomic` of USDC on BSC, sourced from 4663 WETH.

    Raises on no route. EXACT_OUTPUT means Relay decides how much WETH the swap needs — the
    caller checks that amount against the wallet's real balance AFTER this returns, not before.
    """
    body = {
        "user": user, "recipient": user,                      # recipient == our own wallet, always
        "originChainId": ROBINHOOD_CHAIN, "destinationChainId": BSC_CHAIN,
        "originCurrency": ROBINHOOD_WETH, "destinationCurrency": BSC_USDC,
        "amount": str(dest_amount_atomic), "tradeType": "EXACT_OUTPUT",
    }
    d = _http_post_json(RELAY_QUOTE_URL, body)
    if "steps" not in d:
        raise RuntimeError(f"Relay: no route ({d.get('message') or d.get('errorCode') or d})")
    return d


def _steps_as_txs(quote):
    """Flatten Relay steps -> ordered list of {label, to, data, value, gas} on the origin chain."""
    from eth_utils import to_checksum_address
    txs = []
    for step in quote.get("steps", []):
        label = step.get("id") or step.get("action") or "step"
        for it in step.get("items", []):
            data = it.get("data") or {}
            if data.get("chainId") not in (ROBINHOOD_CHAIN, str(ROBINHOOD_CHAIN), None):
                raise RuntimeError(
                    f"unexpected step chainId {data.get('chainId')} (want 4663)")
            txs.append({
                "label": label,
                # eth_account rejects a non-checksummed `to`; Relay returns some contract
                # addresses lowercase (e.g. the deposit router), so normalise every address.
                "to": to_checksum_address(data.get("to")),
                "data": data.get("data") or "0x",
                "value": _to_int(data.get("value"), 0),
                "gas": _to_int(data.get("gas"), 0) or None,
            })
    return txs


def _fmt(quote):
    det = quote.get("details", {})
    ci, co = det.get("currencyIn", {}), det.get("currencyOut", {})
    return (f"  IN : {ci.get('amountFormatted')} {ci.get('currency', {}).get('symbol')} on Robinhood\n"
            f"  OUT: {co.get('amountFormatted')} {co.get('currency', {}).get('symbol')} on BSC\n"
            f"  rate={det.get('rate')}  impact={det.get('totalImpact', {}).get('percent')}%  "
            f"~{det.get('timeEstimate')}s")


def main():
    ap = argparse.ArgumentParser(
        description="Bridge Robinhood (4663) WETH -> an exact USDC amount on BSC (56) via Relay.")
    ap.add_argument("--usd", type=float, required=True,
                    help="exact USDC amount to land on BSC (sourced from 4663 WETH)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True, help="inspect only (default)")
    g.add_argument("--confirm", action="store_true", help="SIGN + broadcast real money")
    ap.add_argument("--state-root", default="/home/bigpapi/Desktop/Sniper")
    args = ap.parse_args()

    if args.usd <= 0:
        sys.exit("REFUSE: --usd must be positive")

    load_env_files(args.state_root)
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import (
        load_keypair, native_balance, erc20_balance, erc20_decimals, _rpc)
    from sniper.execution.evm_real_seams import (eth_nonce, eth_gas_price, sign_tx,
                                                 eth_send_raw, eth_confirm)

    acct = load_keypair(cfg.MEMECOIN_EVM_KEYPAIR)
    addr = acct.address
    robin_rpc = cfg.evm_rpc_for_chain(str(ROBINHOOD_CHAIN))
    bsc_rpc = cfg.evm_rpc_for_chain(str(BSC_CHAIN))

    dest_decimals = erc20_decimals(BSC_USDC, bsc_rpc)
    if dest_decimals is None:
        sys.exit(f"REFUSE: could not read USDC decimals on BSC ({BSC_USDC}) — refusing to assume")
    dest_amount_atomic = int(round(args.usd * 10 ** dest_decimals))

    # --- balances (verify preconditions) ---
    robin_gas = native_balance(addr, robin_rpc)
    robin_weth = erc20_balance(addr, ROBINHOOD_WETH, robin_rpc)
    bsc_usdc = erc20_balance(addr, BSC_USDC, bsc_rpc)
    print(f"wallet {addr}")
    print(f"  Robinhood(4663): gas {robin_gas:.6f} ETH | WETH {robin_weth:.8f}")
    print(f"  BSC(56):         USDC {bsc_usdc:.2f}")
    print(f"  plan: bridge Robinhood WETH -> exactly {args.usd:.2f} USDC on BSC\n")

    if robin_gas < MIN_ROBINHOOD_GAS_ETH:
        sys.exit(f"REFUSE: Robinhood gas {robin_gas:.6f} ETH < {MIN_ROBINHOOD_GAS_ETH} "
                 "needed for approve+deposit")

    # --- live quote (EXACT_OUTPUT: Relay decides the WETH input) ---
    quote = relay_quote_exact_output(addr, dest_amount_atomic)
    echoed = ((quote.get("details") or {}).get("recipient") or "")
    if echoed and echoed.lower() != addr.lower():
        sys.exit(f"REFUSE: quote recipient {echoed!r} is not our wallet")
    required_weth_atomic = int((quote.get("details") or {}).get("currencyIn", {}).get("amount") or 0)
    required_weth = required_weth_atomic / 1e18
    if required_weth <= 0:
        sys.exit(f"REFUSE: quote did not name a WETH input amount: {str(quote)[:300]}")
    if required_weth > robin_weth + 1e-12:
        sys.exit(f"REFUSE: quote needs {required_weth:.8f} WETH, wallet holds only {robin_weth:.8f}")

    txs = _steps_as_txs(quote)
    if not txs:
        sys.exit(f"REFUSE: quote returned no executable steps: {str(quote)[:300]}")
    print("Relay quote:")
    print(_fmt(quote))
    print(f"\n{len(txs)} transaction(s) on 4663 — every `to`/`data` is from Relay, not hardcoded:")
    for i, t in enumerate(txs, 1):
        print(f"  [{i}] {t['label']:8s} to={t['to']}  value={t['value']}  data={t['data'][:18]}… ({len(t['data'])//2}B)")

    if not args.confirm:
        print("\nDRY-RUN — nothing signed. Re-run with --confirm to broadcast real money.")
        return

    # --- CONFIRM: sign + broadcast, in order, confirming each before the next ---
    print("\n--confirm: broadcasting real money.")
    gp = eth_gas_price(robin_rpc)
    if gp is None:
        sys.exit("REFUSE: could not read 4663 gas price")
    gp = int(gp * 1.25)                                        # small buffer
    for i, t in enumerate(txs, 1):
        nonce = eth_nonce(addr, robin_rpc)
        if nonce is None:
            sys.exit(f"REFUSE: could not read nonce before step {i}")
        gas = t["gas"]
        if not gas:
            try:
                est = _rpc(robin_rpc, "eth_estimateGas",
                           [{"from": addr, "to": t["to"], "value": hex(t["value"]), "data": t["data"]}])
                gas = int(int(est["result"], 16) * 1.3)
            except Exception:
                gas = GAS_FALLBACK.get(t["label"], 500_000)
        tx = {"to": t["to"], "data": t["data"], "value": t["value"],
              "gas": int(gas), "gasPrice": gp, "nonce": nonce, "chainId": ROBINHOOD_CHAIN}
        signed = sign_tx(tx, acct)
        if not signed:
            sys.exit(f"REFUSE: signing failed at step {i} ({t['label']})")
        raw, h = signed
        print(f"  [{i}] {t['label']}: sending {h} (gas={gas})…")
        sent = eth_send_raw(raw, robin_rpc)
        if sent is None:
            sys.exit(f"STOP: broadcast failed at step {i} ({t['label']}) — nothing further sent")
        status = eth_confirm(sent, robin_rpc, max_attempts=90, poll_s=2.0)
        print(f"      {sent} -> {status}")
        if status != "confirmed":
            sys.exit(f"STOP: step {i} ({t['label']}) status={status} — NOT resubmitting (may still land)")

    # --- wait for delivery on BSC ---
    print("\nBridged. Waiting for delivery on BSC (Relay ~seconds, allow up to 5m)…")
    for _ in range(60):
        time.sleep(5)
        now = erc20_balance(addr, BSC_USDC, bsc_rpc)
        if now > bsc_usdc + 1e-9:
            print(f"  DELIVERED: BSC USDC {bsc_usdc:.2f} -> {now:.2f}")
            break
    else:
        print("  not seen yet — check the explorer; Relay may still be settling.")

    print(f"\nfinal Robinhood(4663): WETH {erc20_balance(addr, ROBINHOOD_WETH, robin_rpc):.8f} | "
          f"BSC(56) USDC {erc20_balance(addr, BSC_USDC, bsc_rpc):.2f}")
    print("next: scripts/memecoin_bsc_stable_to_wbnb.py --amount-usd <n> --confirm "
          "to convert into the WBNB bankroll, or arm the wanted wallets for chain 56.")


if __name__ == "__main__":
    main()
