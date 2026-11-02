#!/usr/bin/env python3
"""Convert USDbC (Base's older bridged USDC, 0xd9aaec86) in the custody wallet into the WETH
bankroll the Base funded lane spends (CHAIN_QUOTE["8453"]), through a LI.FI same-chain swap.

WHY THIS EXISTS. The ARC -> Base bridge (scripts/memecoin_bridge_arc_to_base.py) quoted native USDC
but Across settled in USDbC on 2026-09-22 (LI.FI status DONE / PARTIAL, "some of the received
tokens are not the requested destination tokens"): 79.670516 USDbC arrived, native USDC did not.
The Base lane quotes in WETH, so the honest one-shot conversion is USDbC -> WETH.

SAFETY MODEL (same as its siblings)
  - DRY-RUN by default: live quote, balances, the two transactions it WOULD send; signs nothing.
    --confirm is required to move real money.
  - Recipient is ALWAYS our own custody wallet; the quote's echoed toAddress is checked.
  - Exact-amount approve to LI.FI's approvalAddress, never max-uint; the swap is sent only after
    the approve is confirmed; each broadcast is written to the receipt before it is sent.
  - Refuses while a funded reservation is in flight on Base, if native ETH is under the gas floor,
    or if the quote's minimum output is under --min-received-pct of the quoted output.

  python3 scripts/memecoin_base_usdbc_to_weth.py                 # dry-run, whole USDbC balance
  python3 scripts/memecoin_base_usdbc_to_weth.py --confirm
"""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
CHAIN = 8453
USDBC = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
LIFI = "https://li.quest/v1"
MIN_GAS_ETH = 0.0005
GAS_FALLBACK = {"approve": 90_000, "swap": 900_000}
SEL_APPROVE = "095ea7b3"
SEL_BALANCE_OF = "70a08231"


def _get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Sniper/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.load(response)


def _pad(addr):
    return addr.lower().replace("0x", "").zfill(64)


def erc20_balance_atoms(rpc, token, owner):
    from sniper.execution.evm_wallet import _rpc
    answer = _rpc(rpc, "eth_call", [{"to": token, "data": "0x" + SEL_BALANCE_OF + _pad(owner)}, "latest"])
    result = (answer or {}).get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise ValueError("balance read unavailable")
    return int(result, 16)


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w") as handle:
        json.dump(value, handle, indent=2)
        handle.flush(); os.fsync(handle.fileno())
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--amount-usdbc", help="USDbC to convert (default: the whole balance)")
    parser.add_argument("--min-received-pct", default="99.0", help="minimum output as %% of the quoted output")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--state-root", default="/home/bigpapi/Desktop/Sniper")
    args = parser.parse_args()

    root = Path(args.state_root)
    from sniper.util.envfiles import load_env_files
    load_env_files(str(root))
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import CHAIN_QUOTE, load_keypair, _rpc, native_balance_raw_or_none
    from sniper.execution.evm_real_seams import eth_nonce, eth_gas_price, sign_tx, eth_send_raw, eth_confirm
    from sniper.util.clock import real_utc_now_iso

    rpc = (os.environ.get("MEMECOIN_EVM_RPC_URL_8453") or "").strip().strip("'\"")
    if not rpc:
        raise ValueError("MEMECOIN_EVM_RPC_URL_8453 must be set")
    if int(_rpc(rpc, "eth_chainId", [])["result"], 16) != CHAIN:
        raise ValueError("RPC does not serve chain %d" % CHAIN)
    weth = CHAIN_QUOTE[str(CHAIN)]["token"].lower()
    acct = load_keypair(cfg.MEMECOIN_EVM_KEYPAIR)
    wallet = acct.address

    balance = erc20_balance_atoms(rpc, USDBC, wallet)
    atoms = int(Decimal(str(args.amount_usdbc)) * 1_000_000) if args.amount_usdbc else balance
    if atoms <= 0 or atoms > balance:
        raise ValueError("USDbC balance %.6f cannot cover %.6f" % (balance / 1e6, atoms / 1e6))
    native = native_balance_raw_or_none(wallet, rpc)
    if native is None or native / 1e18 < MIN_GAS_ETH:
        raise ValueError("Base ETH for gas is below %.4f" % MIN_GAS_ETH)

    quote = _get(LIFI + "/quote?" + urllib.parse.urlencode({
        "fromChain": CHAIN, "toChain": CHAIN, "fromToken": USDBC, "toToken": weth,
        "fromAddress": wallet, "toAddress": wallet, "fromAmount": atoms, "slippage": "0.005"}))
    a, est, tr = quote["action"], quote["estimate"], quote.get("transactionRequest") or {}
    if (a["fromToken"]["address"].lower() != USDBC or a["toToken"]["address"].lower() != weth
            or int(a["fromChainId"]) != CHAIN or int(a["toChainId"]) != CHAIN
            or a["fromAddress"].lower() != wallet.lower() or a["toAddress"].lower() != wallet.lower()
            or int(a["fromAmount"]) != atoms or not tr.get("data") or not tr.get("to")
            or int(tr.get("chainId", CHAIN)) != CHAIN or not est.get("approvalAddress")):
        raise ValueError("route identity mismatch")
    pct = Decimal(str(args.min_received_pct))
    if Decimal(est["toAmountMin"]) < Decimal(est["toAmount"]) * pct / 100:
        raise ValueError("minimum output is below %s%% of the quoted output" % pct)
    spender = est["approvalAddress"]
    weth_before = erc20_balance_atoms(rpc, weth, wallet)
    fees = sum(Decimal(str(f.get("amountUSD") or 0)) for f in est.get("feeCosts", []))
    print("Custody %s on Base. USDbC %.6f, WETH %.6f, ETH %.6f." % (wallet, balance / 1e6, weth_before / 1e18, native / 1e18))
    print("Swap %.6f USDbC -> WETH via %s: %.6f WETH (~$%s), minimum %.6f; fees ~$%s." % (
        atoms / 1e6, quote.get("tool"), int(est["toAmount"]) / 1e18, est.get("toAmountUSD"),
        int(est["toAmountMin"]) / 1e18, fees))
    print("tx1 approve: to %s spender %s amount %d" % (USDBC, spender, atoms))
    print("tx2 swap:    to %s value %s data %d bytes" % (tr["to"], tr.get("value", "0x0"), len(tr["data"]) // 2))
    if not args.confirm:
        print("Quote only. Nothing signed or sent. Add --confirm to convert.")
        return

    from sniper.execution.exec_lock import wallet_exec_lock
    from sniper.persistence.db_core import get_readonly_connection
    request_id = str(uuid.uuid4())
    path = root / "data" / ("base-usdbc-to-weth-" + request_id + ".json")
    receipt = {"request_id": request_id, "wallet": wallet, "usdbc_atoms": atoms,
               "created_at": real_utc_now_iso(), "state": "prepared", "tool": quote.get("tool")}
    # NEVER HANG SILENTLY ON A HELD WALLET LOCK (2026-09-22): the first run sat behind a stale
    # bridge poller for minutes with no output. Try non-blocking for 60 s, naming the wait.
    import time as _time
    _lock = wallet_exec_lock("evm-8453-" + wallet, lock_dir=str(root / "data"), blocking=False)
    _deadline = _time.time() + 60
    while True:
        held = _lock.__enter__()
        if held:
            break
        _lock.__exit__(None, None, None)
        if _time.time() > _deadline:
            raise ValueError("the Base wallet lock is held by another process (see lsof data/real_exec_evm-8453-*.lock); nothing sent")
        print("waiting for the Base wallet lock ...", flush=True); _time.sleep(5)
        _lock = wallet_exec_lock("evm-8453-" + wallet, lock_dir=str(root / "data"), blocking=False)
    try:
        conn = get_readonly_connection(str(root / "sniper_tracker.db"))
        try:
            pending = conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved'").fetchone()[0]
            if pending:
                raise ValueError("a funded reservation is in flight; conversion refused")
        finally:
            conn.close()
        gp = eth_gas_price(rpc)
        nonce = eth_nonce(wallet, rpc)

        def send(label, to, data, value, gas_fallback, use_nonce):
            try:
                estg = _rpc(rpc, "eth_estimateGas", [{"from": wallet, "to": to, "data": data, "value": hex(value)}])
                gas_limit = int(int(estg["result"], 16) * 1.3)
            except Exception:
                gas_limit = gas_fallback
            signed = sign_tx({"to": to, "data": data, "value": value, "gas": gas_limit, "gasPrice": gp,
                              "nonce": use_nonce, "chainId": CHAIN}, acct)
            if not signed:
                raise ValueError(label + " signing failed")
            raw, tx_hash = signed
            receipt.update({label + "_sig": tx_hash, "state": label + "_submitted"}); _save(path, receipt)
            sent = eth_send_raw(raw, rpc)
            print("%s sent: %s (rpc echo %s)" % (label, tx_hash, sent), flush=True)
            status = eth_confirm(tx_hash, rpc, max_attempts=90, poll_s=2.0)
            receipt.update({label + "_status": status}); _save(path, receipt)
            if status != "confirmed":
                raise ValueError("%s not confirmed (%s); see %s" % (label, status, path))

        print("Receipt:", path, flush=True)
        send("approve", USDBC, "0x" + SEL_APPROVE + _pad(spender) + hex(atoms)[2:].zfill(64), 0,
             GAS_FALLBACK["approve"], nonce)
        send("swap", tr["to"], tr["data"], int(str(tr.get("value", "0x0")), 16), GAS_FALLBACK["swap"], nonce + 1)
        weth_after = erc20_balance_atoms(rpc, weth, wallet)
        receipt.update(state="done", weth_before_atoms=weth_before, weth_after_atoms=weth_after); _save(path, receipt)
        print("WETH after: %.6f (was %.6f). USDbC left: %.6f" % (
            weth_after / 1e18, weth_before / 1e18, erc20_balance_atoms(rpc, USDBC, wallet) / 1e6))
    finally:
        _lock.__exit__(None, None, None)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        raise SystemExit("REFUSE: " + str(exc))
