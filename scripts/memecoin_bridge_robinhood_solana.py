#!/usr/bin/env python3
"""Move ETH/WETH from the funded Robinhood (4663) wallet to the funded Solana wallet via Relay.link.

The mirror of scripts/memecoin_bridge_base_solana.py for a Robinhood ORIGIN. The origin is EVM, so
Relay returns `to`/`data`/`value` steps we sign on 4663 with the existing seams — no new signing
code. The recipient is ALWAYS our own Solana pubkey derived from our own keypair, never an argument.
The destination currency is native SOL (gas + trading bankroll on Solana).

SAFETY MODEL (same as its siblings)
  - DRY-RUN by default; --confirm required to move real money.
  - Recipient is our own Solana address, derived, never typed.
  - The quote's echoed recipient is compared against our address before anything is signed.
  - One asset per run. --amount all sweeps the balance (native keeps a computed gas reserve).
  - Every step must target 4663; a step on any other chain refuses the whole run.

    python3 scripts/memecoin_bridge_robinhood_solana.py --token weth --amount all --dry-run
    python3 scripts/memecoin_bridge_robinhood_solana.py --token weth --amount all --confirm
    python3 scripts/memecoin_bridge_robinhood_solana.py --token eth  --amount all --confirm
"""
import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.util.envfiles import load_env_files

ROBINHOOD_CHAIN = 4663
SOLANA_RELAY_CHAIN = 792703809
ROBINHOOD_WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
NATIVE = "0x0000000000000000000000000000000000000000"
SOLANA_NATIVE_SOL = "11111111111111111111111111111111"
RELAY_QUOTE_URL = "https://api.relay.link/quote"
GAS_FALLBACK = {"approve": 120_000, "deposit": 600_000}
# ponytail: flat native-sweep reserve; Orbit gas is ~0, $1 headroom covers 2 txs with margin
NATIVE_GAS_RESERVE_ETH = 0.0003


def _http_post_json(url, body, timeout=40):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def relay_quote(evm_addr, sol_pubkey, amount_wei, origin_currency):
    body = {
        "user": evm_addr,
        "recipient": sol_pubkey,
        "originChainId": ROBINHOOD_CHAIN, "destinationChainId": SOLANA_RELAY_CHAIN,
        "originCurrency": origin_currency, "destinationCurrency": SOLANA_NATIVE_SOL,
        "amount": str(amount_wei), "tradeType": "EXACT_INPUT",
    }
    return _http_post_json(RELAY_QUOTE_URL, body)


def _steps_as_txs(quote):
    from eth_utils import to_checksum_address
    out = []
    for step in quote.get("steps", []):
        for item in step.get("items", []):
            data = item.get("data") or {}
            if data.get("chainId") not in (ROBINHOOD_CHAIN, str(ROBINHOOD_CHAIN), None):
                raise RuntimeError(f"unexpected step chainId {data.get('chainId')} (want 4663)")
            out.append({
                "label": step.get("id") or "step",
                "to": to_checksum_address(data.get("to")),
                "data": data.get("data") or "0x",
                "value": int(data.get("value") or 0),
                "gas": int(data.get("gas") or 0) or None,
            })
    return out


def _fmt(quote):
    det = quote.get("details", {})
    ci, co = det.get("currencyIn", {}), det.get("currencyOut", {})
    return (f"  IN : {ci.get('amountFormatted')} {ci.get('currency', {}).get('symbol')} on Robinhood\n"
            f"  OUT: {co.get('amountFormatted')} {co.get('currency', {}).get('symbol')} on Solana\n"
            f"  rate={det.get('rate')}  impact={det.get('totalImpact', {}).get('percent')}%  "
            f"~{det.get('timeEstimate')}s")


def main():
    ap = argparse.ArgumentParser(description="Bridge Robinhood (4663) ETH/WETH -> native SOL via Relay.")
    ap.add_argument("--token", choices=("eth", "weth"), required=True)
    ap.add_argument("--amount", required=True, help="amount in ETH units, or 'all'")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True, help="inspect only (default)")
    g.add_argument("--confirm", action="store_true", help="SIGN + broadcast real money")
    ap.add_argument("--state-root", default="/home/bigpapi/Desktop/Sniper")
    args = ap.parse_args()

    load_env_files(args.state_root)
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import load_keypair, native_balance, erc20_balance, _rpc
    from sniper.execution.evm_real_seams import (eth_nonce, eth_gas_price, sign_tx,
                                                 eth_send_raw, eth_confirm)
    from sniper.execution.solana_wallet import load_keypair as sol_load, pubkey_str

    acct = load_keypair(cfg.MEMECOIN_EVM_KEYPAIR)
    addr = acct.address
    sol_pubkey = pubkey_str(sol_load(cfg.MEMECOIN_SOLANA_KEYPAIR))
    rpc = cfg.evm_rpc_for_chain(str(ROBINHOOD_CHAIN))

    gas_bal = native_balance(addr, rpc)
    weth_bal = erc20_balance(addr, ROBINHOOD_WETH, rpc)
    print(f"wallet {addr} -> {sol_pubkey}")
    print(f"  Robinhood(4663): native ETH {gas_bal:.8f} | WETH {weth_bal:.8f}")

    from_native = args.token == "eth"
    bal = gas_bal if from_native else weth_bal
    if args.amount == "all":
        amount = bal - NATIVE_GAS_RESERVE_ETH if from_native else bal
    else:
        amount = float(args.amount)
    origin_currency = NATIVE if from_native else ROBINHOOD_WETH
    print(f"  plan: bridge {amount:.8f} {args.token.upper()} (4663) -> SOL (Solana)\n")

    if amount <= 0:
        sys.exit("REFUSE: amount must be positive")
    if from_native:
        if amount > gas_bal - NATIVE_GAS_RESERVE_ETH + 1e-12:
            sys.exit(f"REFUSE: {amount:.8f} leaves under the {NATIVE_GAS_RESERVE_ETH} gas reserve")
    else:
        if amount > weth_bal + 1e-12:
            sys.exit(f"REFUSE: {amount:.8f} exceeds WETH {weth_bal:.8f}")
        if gas_bal < NATIVE_GAS_RESERVE_ETH:
            sys.exit(f"REFUSE: native gas {gas_bal:.8f} ETH < {NATIVE_GAS_RESERVE_ETH} for approve+deposit")

    amount_wei = int(round(amount * 1e18))
    quote = relay_quote(addr, sol_pubkey, amount_wei, origin_currency)
    if quote.get("message") or quote.get("errCode"):
        sys.exit(f"REFUSE: Relay error: {str(quote)[:300]}")
    echoed = (((quote.get("details") or {}).get("recipient")) or "")
    if echoed and echoed != sol_pubkey:
        sys.exit(f"REFUSE: quote recipient {echoed!r} is not our Solana wallet")
    txs = _steps_as_txs(quote)
    if not txs:
        sys.exit(f"REFUSE: quote returned no executable steps: {str(quote)[:300]}")
    print("Relay quote:")
    print(_fmt(quote))
    print(f"\n{len(txs)} transaction(s) on 4663 — every `to`/`data` is from Relay, not hardcoded:")
    for i, t in enumerate(txs, 1):
        print(f"  [{i}] {t['label']:10s} to={t['to']}  value={t['value']}  data={t['data'][:18]}… ({len(t['data'])//2}B)")

    if not args.confirm:
        print("\nDRY-RUN — nothing signed. Re-run with --confirm to broadcast real money.")
        return

    print("\n--confirm: broadcasting real money.")
    gp = eth_gas_price(rpc)
    if gp is None:
        sys.exit("REFUSE: could not read 4663 gas price")
    gp = int(gp * 1.25)
    for i, t in enumerate(txs, 1):
        nonce = eth_nonce(addr, rpc)
        if nonce is None:
            sys.exit(f"REFUSE: could not read nonce before step {i}")
        gas = t["gas"]
        if not gas:
            try:
                est = _rpc(rpc, "eth_estimateGas",
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
        sent = eth_send_raw(raw, rpc)
        if sent is None:
            sys.exit(f"STOP: broadcast failed at step {i} ({t['label']}) — nothing further sent")
        status = eth_confirm(sent, rpc, max_attempts=90, poll_s=2.0)
        print(f"      {sent} -> {status}")
        if status != "confirmed":
            sys.exit(f"STOP: step {i} did not confirm ({status}) — nothing further sent")
    print("\nDone on 4663. Solana arrival is Relay-side (usually <1 min); check the Solana SOL "
          "balance to verify.")


if __name__ == "__main__":
    main()
