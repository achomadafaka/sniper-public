#!/usr/bin/env python3
"""Fund the Robinhood (4663) trading wallet by bridging Base (8453) USDC via Relay.link.

WHY a script (not hardcoded bridge calls): Robinhood Chain is 11 days old and there is no verified
canonical Base->Robinhood bridge contract to trust. Relay.link supports 4663 (depositEnabled=true,
tokenSupport=All) and its API returns the EXACT approve + deposit transaction calldata for our own
wallet, so we NEVER hardcode/guess a bridge address — every `to`/`data` comes from Relay's quote and
is printed for the operator to eyeball before signing. Signing uses the SAME primitives the live
memecoin executor uses (load_keypair / eth_nonce / eth_gas_price / sign_tx / eth_send_raw /
eth_confirm), so this adds no new signing surface.

SAFETY MODEL
  - DRY-RUN by default: fetches the live quote + balances, prints the exact txs it WOULD send, signs
    nothing. --confirm is required to broadcast real money.
  - Small by default. Refuses if amount > available Base USDC or if Base gas ETH is too low to pay for
    the 2 txs. Recipient is ALWAYS our own wallet (no third-party recipient).
  - approve is for the EXACT bridged amount (Relay encodes it), never max-uint.
  - Confirms tx 1 (approve) before sending tx 2 (deposit); stops on any failure/unknown receipt.

Robinhood needs BOTH: native ETH (gas for every swap) and WETH (trading bankroll). Bridge is one dest
token per run, so fund gas first, then bankroll:
    python3 scripts/memecoin_fund_robinhood.py --usd 10 --dest eth  --dry-run   # inspect
    python3 scripts/memecoin_fund_robinhood.py --usd 10 --dest eth  --confirm   # gas
    python3 scripts/memecoin_fund_robinhood.py --usd 40 --dest weth --confirm   # bankroll
Then gate live trading on: scripts/memecoin_evm_smoke_test.py --chain 4663 --confirm
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.util.envfiles import load_env_files

BASE_CHAIN = 8453
ROBINHOOD_CHAIN = 4663
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
NATIVE = "0x0000000000000000000000000000000000000000"
ROBINHOOD_WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
RELAY_QUOTE_URL = "https://api.relay.link/quote"
MIN_BASE_GAS_ETH = 0.0015          # ~enough for approve + deposit on Base
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


def relay_quote(user, amount_human, dest_token, origin_currency=BASE_USDC, origin_decimals=6):
    """Live Relay quote Base (USDC|native ETH) -> (ETH|WETH) on Robinhood, for our own wallet.

    Raises on no route. `origin_currency` is a parameter rather than a constant because Base holds
    BOTH the bridgeable USDC and the native ETH that pays for the bridge; a native origin quotes as
    ONE transaction (no ERC-20 approve) and at lower impact than routing through USDC.
    """
    amount = str(int(round(amount_human * 10 ** origin_decimals)))
    body = {
        "user": user, "recipient": user,                      # recipient == our own wallet, always
        "originChainId": BASE_CHAIN, "destinationChainId": ROBINHOOD_CHAIN,
        "originCurrency": origin_currency, "destinationCurrency": dest_token,
        "amount": amount, "tradeType": "EXACT_INPUT",
    }
    d = _http_post_json(RELAY_QUOTE_URL, body)
    if "steps" not in d:
        raise RuntimeError(f"Relay: no route ({d.get('message') or d.get('errorCode') or d})")
    return d


def _steps_as_txs(quote):
    """Flatten Relay steps -> ordered list of {label, to, data, value, chainId} on the origin chain."""
    from eth_utils import to_checksum_address
    txs = []
    for step in quote.get("steps", []):
        label = step.get("id") or step.get("action") or "step"
        for it in step.get("items", []):
            data = it.get("data") or {}
            if data.get("chainId") not in (BASE_CHAIN, str(BASE_CHAIN), None):
                raise RuntimeError(f"unexpected step chainId {data.get('chainId')} (want Base {BASE_CHAIN})")
            txs.append({
                "label": label,
                # eth_account rejects a non-checksummed `to` ('invalid fields'). Relay returns some
                # contract addresses lowercase (e.g. the deposit router), so normalise every address.
                "to": to_checksum_address(data.get("to")),
                "data": data.get("data") or "0x",
                "value": _to_int(data.get("value"), 0),
                "gas": _to_int(data.get("gas"), 0) or None,
            })
    return txs


def _fmt(quote):
    det = quote.get("details", {})
    ci, co = det.get("currencyIn", {}), det.get("currencyOut", {})
    return (f"  IN : {ci.get('amountFormatted')} {ci.get('currency', {}).get('symbol')}\n"
            f"  OUT: {co.get('amountFormatted')} {co.get('currency', {}).get('symbol')} on Robinhood\n"
            f"  rate={det.get('rate')}  impact={det.get('totalImpact', {}).get('percent')}%  "
            f"~{det.get('timeEstimate')}s")


def main():
    ap = argparse.ArgumentParser(description="Bridge Base USDC or native ETH -> Robinhood (4663) via Relay.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--usd", type=float, help="USDC amount to bridge from Base")
    src.add_argument("--eth", type=float,
                     help="native ETH amount to bridge from Base (1 tx, no approve). The gas "
                          "reserve is withheld on top of this amount, never out of it.")
    ap.add_argument("--dest", choices=("eth", "weth"), required=True,
                    help="eth = native gas on Robinhood; weth = trading bankroll")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True, help="inspect only (default)")
    g.add_argument("--confirm", action="store_true", help="SIGN + broadcast real money")
    ap.add_argument("--state-root", default="/home/bigpapi/Desktop/Sniper")
    args = ap.parse_args()

    load_env_files(args.state_root)
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import (load_keypair, native_balance, erc20_balance, _rpc)
    from sniper.execution.evm_real_seams import (eth_nonce, eth_gas_price, sign_tx,
                                                 eth_send_raw, eth_confirm)

    kp = cfg.MEMECOIN_EVM_KEYPAIR
    acct = load_keypair(kp)
    addr = acct.address
    base_rpc = cfg.evm_rpc_for_chain(str(BASE_CHAIN))
    robin_rpc = cfg.evm_rpc_for_chain(str(ROBINHOOD_CHAIN))
    dest_token = NATIVE if args.dest == "eth" else ROBINHOOD_WETH

    # --- balances (verify preconditions) ---
    base_gas = native_balance(addr, base_rpc)
    base_usdc = erc20_balance(addr, BASE_USDC, base_rpc)
    robin_gas = native_balance(addr, robin_rpc)
    robin_weth = erc20_balance(addr, ROBINHOOD_WETH, robin_rpc)
    from_eth = args.eth is not None
    amount = args.eth if from_eth else args.usd
    origin_currency, origin_decimals = (NATIVE, 18) if from_eth else (BASE_USDC, 6)
    unit = "ETH" if from_eth else "USDC"
    print(f"wallet {addr}")
    print(f"  Base(8453):      gas {base_gas:.6f} ETH | USDC {base_usdc:.2f}")
    print(f"  Robinhood(4663): gas {robin_gas:.6f} ETH | WETH {robin_weth:.8f}")
    print(f"  plan: bridge {amount:.6f} {unit} (Base) -> {args.dest.upper()} on Robinhood\n")

    if amount <= 0:
        sys.exit(f"REFUSE: amount must be positive")
    if from_eth:
        # The bridged ETH and the gas that pays for the bridge come out of the SAME balance, so the
        # reserve is checked against what is left AFTER the transfer. Checking only `base_gas` would
        # happily sign a transfer that strands the wallet with no gas to ever move the rest.
        if amount > base_gas - MIN_BASE_GAS_ETH:
            sys.exit(f"REFUSE: --eth {amount:.6f} leaves {base_gas - amount:.6f} ETH, "
                     f"under the {MIN_BASE_GAS_ETH} gas reserve (Base gas {base_gas:.6f})")
    else:
        if amount > base_usdc + 1e-9:
            sys.exit(f"REFUSE: --usd {amount} exceeds Base USDC {base_usdc:.2f}")
        if base_gas < MIN_BASE_GAS_ETH:
            sys.exit(f"REFUSE: Base gas {base_gas:.6f} ETH < {MIN_BASE_GAS_ETH} needed for approve+deposit")

    # --- live quote ---
    quote = relay_quote(addr, amount, dest_token, origin_currency, origin_decimals)
    txs = _steps_as_txs(quote)
    print("Relay quote:")
    print(_fmt(quote))
    print(f"\n{len(txs)} transaction(s) on Base — every `to`/`data` is from Relay, not hardcoded:")
    for i, t in enumerate(txs, 1):
        print(f"  [{i}] {t['label']:8s} to={t['to']}  value={t['value']}  data={t['data'][:18]}… ({len(t['data'])//2}B)")

    if not args.confirm:
        print("\nDRY-RUN — nothing signed. Re-run with --confirm to broadcast real money.")
        return

    # --- CONFIRM: sign + broadcast, in order, confirming each before the next ---
    print("\n--confirm: broadcasting real money.")
    chain_id = BASE_CHAIN
    gp = eth_gas_price(base_rpc)
    if gp is None:
        sys.exit("REFUSE: could not read Base gas price")
    gp = int(gp * 1.25)                                        # small buffer
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
              "gas": int(gas), "gasPrice": gp, "nonce": nonce, "chainId": chain_id}
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
            sys.exit(f"STOP: step {i} ({t['label']}) status={status} — NOT resubmitting (may still land)")

    # --- wait for delivery on Robinhood ---
    print("\nBridged. Waiting for delivery on Robinhood (Relay ~seconds, allow up to 5m)…")
    target = ROBINHOOD_WETH if args.dest == "weth" else None
    before = robin_weth if args.dest == "weth" else robin_gas
    for _ in range(60):
        time.sleep(5)
        now = (erc20_balance(addr, target, robin_rpc) if target
               else native_balance(addr, robin_rpc))
        if now > before + 1e-12:
            print(f"  DELIVERED: Robinhood {args.dest.upper()} {before:.8f} -> {now:.8f}")
            break
    else:
        print("  not seen yet — check the explorer; Relay may still be settling.")

    print(f"\nfinal Robinhood(4663): gas {native_balance(addr, robin_rpc):.6f} ETH | "
          f"WETH {erc20_balance(addr, ROBINHOOD_WETH, robin_rpc):.8f}")
    print("next: fund the other leg (weth/eth), then "
          "scripts/memecoin_evm_smoke_test.py --chain 4663 --confirm")


if __name__ == "__main__":
    main()
