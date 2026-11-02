#!/usr/bin/env python3
"""Move native ETH/WETH from the funded Robinhood (4663) wallet to gas or bankroll on BSC (56).

The mirror of scripts/memecoin_bridge_robinhood_solana.py for a BSC destination, and the EXACT_INPUT
sibling of memecoin_bridge_robinhood_bsc.py (which is EXACT_OUTPUT-to-USDC because Robinhood has no
stable of its own). This one exists because BSC's OWN gas can run out before there is enough of it
to pay for the on-chain swap that would convert existing BSC stablecoin into more gas -- the
chicken-and-egg the operator hit 2026-09-06: 0.00124 BNB on hand, memecoin_bsc_stable_to_wbnb.py's
own MIN_GAS_BNB floor is 0.002. Native ETH from Robinhood, which has real headroom over its own gas
floor, sidesteps that entirely. Origin is EVM, so Relay returns `to`/`data`/`value` steps we sign on
4663 with the existing seams — no new signing code.

SAFETY MODEL (same as its siblings)
  - DRY-RUN by default; --confirm required to move real money.
  - Recipient is our own BSC address (same private key, EVM-wide), never an argument.
  - The quote's echoed recipient is compared against our address before anything is signed.
  - Refuses if the amount leaves Robinhood's own gas reserve short, or if WETH is insufficient.
  - Every step must target 4663; a step on any other chain refuses the whole run.

    python3 scripts/memecoin_bridge_robinhood_bsc_gas.py --token eth --amount 0.003 --dest bnb --dry-run
    python3 scripts/memecoin_bridge_robinhood_bsc_gas.py --token eth --amount 0.003 --dest bnb --confirm
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.util.envfiles import load_env_files

ROBINHOOD_CHAIN = 4663
BSC_CHAIN = 56
ROBINHOOD_WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
BSC_WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
NATIVE = "0x0000000000000000000000000000000000000000"
RELAY_QUOTE_URL = "https://api.relay.link/quote"
GAS_FALLBACK = {"approve": 120_000, "deposit": 600_000}
# ponytail: flat native-gas floor for approve+deposit on 4663; measured cost is a few cents
MIN_ROBINHOOD_GAS_ETH = 0.0003


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


def relay_quote(user, amount_wei, origin_currency, dest_token):
    body = {
        "user": user, "recipient": user,
        "originChainId": ROBINHOOD_CHAIN, "destinationChainId": BSC_CHAIN,
        "originCurrency": origin_currency, "destinationCurrency": dest_token,
        "amount": str(amount_wei), "tradeType": "EXACT_INPUT",
    }
    d = _http_post_json(RELAY_QUOTE_URL, body)
    if "steps" not in d:
        raise RuntimeError(f"Relay: no route ({d.get('message') or d.get('errorCode') or d})")
    return d


def _steps_as_txs(quote):
    from eth_utils import to_checksum_address
    txs = []
    for step in quote.get("steps", []):
        label = step.get("id") or step.get("action") or "step"
        for it in step.get("items", []):
            data = it.get("data") or {}
            if data.get("chainId") not in (ROBINHOOD_CHAIN, str(ROBINHOOD_CHAIN), None):
                raise RuntimeError(f"unexpected step chainId {data.get('chainId')} (want 4663)")
            txs.append({
                "label": label,
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
        description="Bridge Robinhood (4663) native ETH/WETH -> BNB/WBNB on BSC (56) via Relay.")
    ap.add_argument("--token", choices=("eth", "weth"), required=True)
    ap.add_argument("--amount", type=float, required=True, help="amount in ETH units")
    ap.add_argument("--dest", choices=("bnb", "wbnb"), required=True,
                    help="bnb = native gas on BSC; wbnb = trading bankroll")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True, help="inspect only (default)")
    g.add_argument("--confirm", action="store_true", help="SIGN + broadcast real money")
    ap.add_argument("--state-root", default="/home/bigpapi/Desktop/Sniper")
    args = ap.parse_args()

    if args.amount <= 0:
        sys.exit("REFUSE: --amount must be positive")

    load_env_files(args.state_root)
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import load_keypair, native_balance, erc20_balance, _rpc
    from sniper.execution.evm_real_seams import (eth_nonce, eth_gas_price, sign_tx,
                                                 eth_send_raw, eth_confirm)

    acct = load_keypair(cfg.MEMECOIN_EVM_KEYPAIR)
    addr = acct.address
    robin_rpc = cfg.evm_rpc_for_chain(str(ROBINHOOD_CHAIN))
    bsc_rpc = cfg.evm_rpc_for_chain(str(BSC_CHAIN))
    dest_token = NATIVE if args.dest == "bnb" else BSC_WBNB

    robin_gas = native_balance(addr, robin_rpc)
    robin_weth = erc20_balance(addr, ROBINHOOD_WETH, robin_rpc)
    bsc_gas = native_balance(addr, bsc_rpc)
    bsc_wbnb = erc20_balance(addr, BSC_WBNB, bsc_rpc)
    from_native = args.token == "eth"
    origin_currency = NATIVE if from_native else ROBINHOOD_WETH
    print(f"wallet {addr}")
    print(f"  Robinhood(4663): gas {robin_gas:.6f} ETH | WETH {robin_weth:.8f}")
    print(f"  BSC(56):         gas {bsc_gas:.6f} BNB | WBNB {bsc_wbnb:.8f}")
    print(f"  plan: bridge {args.amount:.6f} {args.token.upper()} (4663) -> {args.dest.upper()} on BSC\n")

    if from_native:
        # The bridged ETH and the gas that pays for the bridge come out of the SAME balance, so the
        # reserve is checked against what is left AFTER the transfer.
        if args.amount > robin_gas - MIN_ROBINHOOD_GAS_ETH:
            sys.exit(f"REFUSE: --amount {args.amount:.6f} leaves {robin_gas - args.amount:.6f} ETH, "
                     f"under the {MIN_ROBINHOOD_GAS_ETH} gas reserve (Robinhood gas {robin_gas:.6f})")
    else:
        if args.amount > robin_weth + 1e-12:
            sys.exit(f"REFUSE: --amount {args.amount:.6f} exceeds WETH {robin_weth:.8f}")
        if robin_gas < MIN_ROBINHOOD_GAS_ETH:
            sys.exit(f"REFUSE: Robinhood gas {robin_gas:.6f} ETH < {MIN_ROBINHOOD_GAS_ETH} "
                     "for approve+deposit")

    amount_wei = int(round(args.amount * 1e18))
    quote = relay_quote(addr, amount_wei, origin_currency, dest_token)
    echoed = ((quote.get("details") or {}).get("recipient") or "")
    if echoed and echoed.lower() != addr.lower():
        sys.exit(f"REFUSE: quote recipient {echoed!r} is not our wallet")
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

    print("\n--confirm: broadcasting real money.")
    gp = eth_gas_price(robin_rpc)
    if gp is None:
        sys.exit("REFUSE: could not read 4663 gas price")
    gp = int(gp * 1.25)
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

    print("\nBridged. BSC delivery is Relay-side, usually seconds — check the balance to verify:")
    print(f"  BSC(56): gas {native_balance(addr, bsc_rpc):.6f} BNB | "
          f"WBNB {erc20_balance(addr, BSC_WBNB, bsc_rpc):.8f}")


if __name__ == "__main__":
    main()
