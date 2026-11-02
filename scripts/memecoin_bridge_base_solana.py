#!/usr/bin/env python3
"""Move USDC from the funded Base (8453) wallet back to the funded Solana wallet via Relay.link.

The mirror of scripts/memecoin_fund_robinhood.py for a Solana DESTINATION: the origin is EVM, so
Relay returns `to`/`data`/`value` steps we sign on Base with the existing seams — no new signing
code. The only cross-VM difference is that the recipient must be given explicitly (address formats
differ), so it is ALWAYS our own Solana pubkey derived from our own keypair, never an argument.

SAFETY MODEL (same as its siblings)
  - DRY-RUN by default; --confirm required to move real money.
  - Recipient is our own Solana address, derived, never typed.
  - The quote's echoed recipient is compared against our address before anything is signed.
  - Refuses if amount exceeds Base USDC or Base gas is under the approve+deposit floor.
  - Every step must target Base; a step on any other chain refuses the whole run.

    python3 scripts/memecoin_bridge_base_solana.py --usd 99 --dry-run   # inspect
    python3 scripts/memecoin_bridge_base_solana.py --usd 99 --confirm   # move real money
"""
import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.util.envfiles import load_env_files

BASE_CHAIN = 8453
SOLANA_RELAY_CHAIN = 792703809
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RELAY_QUOTE_URL = "https://api.relay.link/quote"
MIN_BASE_GAS_ETH = 0.0006          # approve + deposit at Base gas prices, with headroom
GAS_FALLBACK = {"approve": 120_000, "deposit": 500_000}


def _http_post_json(url, body, timeout=40):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def relay_quote(evm_addr, sol_pubkey, amount_human):
    body = {
        "user": evm_addr,
        "recipient": sol_pubkey,
        "originChainId": BASE_CHAIN, "destinationChainId": SOLANA_RELAY_CHAIN,
        "originCurrency": BASE_USDC, "destinationCurrency": SOLANA_USDC,
        "amount": str(int(round(amount_human * 1e6))), "tradeType": "EXACT_INPUT",
    }
    return _http_post_json(RELAY_QUOTE_URL, body)


def _steps_as_txs(quote):
    from eth_utils import to_checksum_address
    out = []
    for step in quote.get("steps", []):
        for item in step.get("items", []):
            data = item.get("data") or {}
            if data.get("chainId") not in (BASE_CHAIN, str(BASE_CHAIN), None):
                raise RuntimeError(f"unexpected step chainId {data.get('chainId')} (want Base)")
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
    return (f"  IN : {ci.get('amountFormatted')} {ci.get('currency', {}).get('symbol')} on Base\n"
            f"  OUT: {co.get('amountFormatted')} {co.get('currency', {}).get('symbol')} on Solana\n"
            f"  rate={det.get('rate')}  impact={det.get('totalImpact', {}).get('percent')}%  "
            f"~{det.get('timeEstimate')}s")


def main():
    ap = argparse.ArgumentParser(description="Bridge Base USDC -> Solana USDC via Relay.")
    ap.add_argument("--usd", type=float, required=True, help="USDC amount to bridge from Base")
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
    base_rpc = cfg.evm_rpc_for_chain(str(BASE_CHAIN))

    base_gas = native_balance(addr, base_rpc)
    base_usdc = erc20_balance(addr, BASE_USDC, base_rpc)
    print(f"wallet {addr} -> {sol_pubkey}")
    print(f"  Base(8453): gas {base_gas:.6f} ETH | USDC {base_usdc:.2f}")
    print(f"  plan: bridge {args.usd:.2f} USDC (Base) -> USDC (Solana)\n")

    if args.usd <= 0:
        sys.exit("REFUSE: amount must be positive")
    if args.usd > base_usdc + 1e-9:
        sys.exit(f"REFUSE: --usd {args.usd} exceeds Base USDC {base_usdc:.2f}")
    if base_gas < MIN_BASE_GAS_ETH:
        sys.exit(f"REFUSE: Base gas {base_gas:.6f} ETH < {MIN_BASE_GAS_ETH} for approve+deposit")

    quote = relay_quote(addr, sol_pubkey, args.usd)
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
    print(f"\n{len(txs)} transaction(s) on Base — every `to`/`data` is from Relay, not hardcoded:")
    for i, t in enumerate(txs, 1):
        print(f"  [{i}] {t['label']:10s} to={t['to']}  value={t['value']}  data={t['data'][:18]}… ({len(t['data'])//2}B)")

    if not args.confirm:
        print("\nDRY-RUN — nothing signed. Re-run with --confirm to broadcast real money.")
        return

    print("\n--confirm: broadcasting real money.")
    gp = eth_gas_price(base_rpc)
    if gp is None:
        sys.exit("REFUSE: could not read Base gas price")
    gp = int(gp * 1.25)
    for i, t in enumerate(txs, 1):
        nonce = eth_nonce(addr, base_rpc)
        if nonce is None:
            sys.exit(f"REFUSE: could not read nonce before step {i}")
        gas = t["gas"]
        if not gas:
            try:
                est = _rpc(base_rpc, "eth_estimateGas",
                           [{"from": addr, "to": t["to"], "value": hex(t["value"]), "data": t["data"]}])
                gas = int(int(est["result"], 16) * 1.3)
            except Exception:
                gas = GAS_FALLBACK.get(t["label"], 500_000)
        tx = {"to": t["to"], "data": t["data"], "value": t["value"],
              "gas": int(gas), "gasPrice": gp, "nonce": nonce, "chainId": BASE_CHAIN}
        signed = sign_tx(tx, acct)
        if not signed:
            sys.exit(f"REFUSE: signing failed at step {i} ({t['label']})")
        raw, h = signed
        print(f"  [{i}] {t['label']}: sending {h} (gas={gas})…")
        sent = eth_send_raw(raw, base_rpc)
        if sent is None:
            sys.exit(f"STOP: broadcast failed at step {i} ({t['label']}) — nothing further sent")
        status = eth_confirm(sent, base_rpc, max_attempts=90, poll_s=2.0)
        print(f"      {sent} -> {status}")
        if status != "confirmed":
            sys.exit(f"STOP: step {i} did not confirm ({status}) — nothing further sent")
    print("\nDone on Base. Solana arrival is Relay-side (usually <1 min); check the Solana USDC "
          "balance to verify.")


if __name__ == "__main__":
    main()
