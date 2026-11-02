#!/usr/bin/env python3
"""Move USDC from the ARC (5042) custody wallet to the Base (8453) custody wallet via LI.FI.

The EVM-origin sibling of scripts/memecoin_bridge_solana_to_arc.py. ARC's USDC is the chain's gas
asset AND an ERC-20 at 0x3600...0000 (6 decimals); LI.FI (tool: across) bridges it to native USDC on
Base in one call to its diamond, after an EXACT-amount ERC-20 approve. Every `to`/`data` comes from
the live quote; nothing is hardcoded or guessed. Signing uses the SAME primitives the funded executor
uses (load_keypair / eth_nonce / eth_gas_price / sign_tx / eth_send_raw / eth_confirm).

SAFETY MODEL (same as its siblings)
  - DRY-RUN by default: prints balances, the live quote, fees and the two transactions it WOULD
    send, and signs nothing. --confirm is required to move real money.
  - Recipient is ALWAYS our own custody wallet (same key on every EVM chain), never an argument;
    the quote's echoed toAddress is compared against it before anything is signed.
  - Refuses unless the ARC and Base funded books are flat (no open real position, no reserved
    entry) and no other entry pause exists; pauses entries for the transfer and clears only its
    own pause. An uncertain transfer retains the pause; --resume RECEIPT checks it without sending.
  - approve is for the EXACT bridged amount, never max-uint; the bridge tx is sent only after the
    approve is confirmed. Each broadcast is saved to the receipt BEFORE it is sent, so a lost
    response can never cause a second send.
  - Refuses if the amount would leave less than --reserve-usdc on ARC (default 80, one entry).

  python3 scripts/memecoin_bridge_arc_to_base.py --amount-usdc 80
  python3 scripts/memecoin_bridge_arc_to_base.py --amount-usdc 80 --confirm
  python3 scripts/memecoin_bridge_arc_to_base.py --resume data/arc-to-base-<id>.json
"""
import argparse
from contextlib import ExitStack
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
ARC_CHAIN = 5042
BASE_CHAIN = 8453
ARC_USDC = "0x3600000000000000000000000000000000000000"
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
LIFI = "https://li.quest/v1"
GAS_FALLBACK = {"approve": 90_000, "bridge": 400_000}
SEL_APPROVE = "095ea7b3"
SEL_BALANCE_OF = "70a08231"


def _get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Sniper/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.load(response)


def usdc_atoms(value):
    """Reject rounding, nonfinite values, and nonpositive amounts."""
    try:
        amount = Decimal(str(value)) * 1_000_000
        if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
            raise ValueError("amount requires positive USDC with at most six decimals")
        return int(amount)
    except InvalidOperation as exc:
        raise ValueError("invalid USDC amount") from exc


def _pad(addr):
    return addr.lower().replace("0x", "").zfill(64)


def erc20_balance_atoms(rpc, token, owner):
    from sniper.execution.evm_wallet import _rpc
    answer = _rpc(rpc, "eth_call", [{"to": token, "data": "0x" + SEL_BALANCE_OF + _pad(owner)}, "latest"])
    result = (answer or {}).get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise ValueError("balance read unavailable")
    return int(result, 16)


def validate_quote(quote, wallet, atoms, minimum_pct):
    """Bind the route to our wallet on both chains, both tokens, and the requested amount."""
    a, estimate = quote["action"], quote["estimate"]
    for field, chain, token in [("fromToken", ARC_CHAIN, ARC_USDC), ("toToken", BASE_CHAIN, BASE_USDC)]:
        item = a[field]
        if (item["address"].lower() != token or int(item["decimals"]) != 6
                or int(item["chainId"]) != chain):
            raise ValueError("route token identity mismatch")
    if (int(a["fromChainId"]) != ARC_CHAIN or int(a["toChainId"]) != BASE_CHAIN
            or a["fromAddress"].lower() != wallet.lower() or a["toAddress"].lower() != wallet.lower()
            or int(a["fromAmount"]) != atoms):
        raise ValueError("route transfer identity mismatch")
    pct = Decimal(str(minimum_pct))
    if not pct.is_finite() or not 0 < pct <= 100:
        raise ValueError("minimum received percent must be above zero and at most 100")
    minimum = int(estimate["toAmountMin"])
    if minimum < Decimal(atoms) * pct / 100 or int(estimate["toAmount"]) < minimum:
        raise ValueError("minimum route output is below the requested floor")
    tr = quote.get("transactionRequest") or {}
    if not tr.get("data") or not tr.get("to") or int(tr.get("chainId", ARC_CHAIN)) != ARC_CHAIN:
        raise ValueError("route has no ARC transaction")
    if not estimate.get("approvalAddress"):
        raise ValueError("route names no approval address")
    return minimum


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w") as handle:
        json.dump(value, handle, indent=2)
        handle.flush(); os.fsync(handle.fileno())
    temp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def pause_entries(path, request_id):
    from sniper.services.memecoin_arming import trip_kill_switch
    token = trip_kill_switch(str(path), source="arc-to-base:" + request_id, only_if_clear=True)
    if token is None:
        raise FileExistsError("an entry pause already exists")
    return token


def resume_entries(path, request_id, token):
    from sniper.services.memecoin_arming import clear_kill_switch
    return clear_kill_switch(str(path), source="arc-to-base:" + request_id, expected_token=token)


def lifi_status(tx_hash):
    return _get(LIFI + "/status?" + urllib.parse.urlencode({
        "txHash": tx_hash, "fromChain": ARC_CHAIN, "toChain": BASE_CHAIN}))


def settlement_complete(receipt, result, base_atoms):
    return (result.get("status") == "DONE" and result.get("substatus") == "COMPLETED"
            and (result.get("sending") or {}).get("txHash", "").lower() == receipt["bridge_sig"].lower()
            and int((result.get("receiving") or {}).get("chainId") or 0) == BASE_CHAIN
            and bool((result.get("receiving") or {}).get("txHash"))
            and base_atoms >= receipt["base_before_atoms"] + receipt["minimum_atoms"])


def settle(path, receipt, base_rpc, kill, wait_s=600):
    """Poll LI.FI and the Base balance; clear the pause only on a complete settlement."""
    deadline = time.time() + wait_s
    while True:
        result = {}
        try:
            result = lifi_status(receipt["bridge_sig"])
        except Exception as exc:
            print("status unavailable:", type(exc).__name__, flush=True)
        base_atoms = erc20_balance_atoms(base_rpc, BASE_USDC, receipt["wallet"])
        if settlement_complete(receipt, result, base_atoms):
            receipt.update(state="settled", base_after_atoms=base_atoms,
                           receiving_tx=(result.get("receiving") or {}).get("txHash"))
            _save(path, receipt)
            cleared = resume_entries(kill, receipt["request_id"], receipt["kill_token"])
            print("Base credit confirmed. Base USDC: %.6f. Entry pause cleared: %s" % (base_atoms / 1e6, cleared))
            return True
        if result.get("status") == "FAILED":
            receipt.update(state="bridge_failed", lifi=result); _save(path, receipt)
            print("LI.FI reports FAILED. The entry pause stays until an operator resolves it:", path)
            return False
        if time.time() > deadline:
            receipt.update(state="submitted_unknown", lifi=result); _save(path, receipt)
            print("Not settled within %ds; pause retained. Re-run with --resume %s" % (wait_s, path))
            return False
        print("waiting: LI.FI %s/%s, Base USDC %.6f" % (result.get("status"), result.get("substatus"), base_atoms / 1e6), flush=True)
        time.sleep(10)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--amount-usdc", default="80", help="exact USDC amount to debit on ARC (default: 80)")
    parser.add_argument("--reserve-usdc", default="80", help="USDC to retain on ARC (default: 80)")
    parser.add_argument("--min-received-pct", default="98.5")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--resume", type=Path, help="check a stored transfer; never send again")
    parser.add_argument("--state-root", default="/home/bigpapi/Desktop/Sniper")
    args = parser.parse_args()

    root = Path(args.state_root)
    from sniper.util.envfiles import load_env_files
    load_env_files(str(root))
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import load_keypair, _rpc
    from sniper.execution.evm_real_seams import eth_nonce, eth_gas_price, sign_tx, eth_send_raw, eth_confirm
    from sniper.util.clock import real_utc_now_iso

    arc_rpc = (os.environ.get("MEMECOIN_EVM_RPC_URL_5042") or "").strip().strip("'\"")
    base_rpc = (os.environ.get("MEMECOIN_EVM_RPC_URL_8453") or "").strip().strip("'\"")
    if not arc_rpc or not base_rpc:
        raise ValueError("MEMECOIN_EVM_RPC_URL_5042 and MEMECOIN_EVM_RPC_URL_8453 must be set")
    for rpc, chain in ((arc_rpc, ARC_CHAIN), (base_rpc, BASE_CHAIN)):
        if int(_rpc(rpc, "eth_chainId", [])["result"], 16) != chain:
            raise ValueError("RPC does not serve chain %d" % chain)
    acct = load_keypair(cfg.MEMECOIN_EVM_KEYPAIR)
    wallet = acct.address
    kill = root / "data" / "memecoin_kill_switch"

    if args.resume:
        receipt = json.loads(args.resume.read_text())
        if not receipt.get("bridge_sig"):
            print("No bridge transaction was ever sent; clearing the pause.")
            resume_entries(kill, receipt["request_id"], receipt["kill_token"])
            return
        settle(args.resume, receipt, base_rpc, kill)
        return

    atoms, reserve = usdc_atoms(args.amount_usdc), usdc_atoms(args.reserve_usdc)
    quote = _get(LIFI + "/quote?" + urllib.parse.urlencode({
        "fromChain": ARC_CHAIN, "toChain": BASE_CHAIN, "fromToken": ARC_USDC, "toToken": BASE_USDC,
        "fromAddress": wallet, "toAddress": wallet, "fromAmount": atoms, "slippage": "0.005"}))
    minimum = validate_quote(quote, wallet, atoms, args.min_received_pct)
    estimate, tr = quote["estimate"], quote["transactionRequest"]
    spender = estimate["approvalAddress"]

    arc_atoms = erc20_balance_atoms(arc_rpc, ARC_USDC, wallet)
    base_before = erc20_balance_atoms(base_rpc, BASE_USDC, wallet)
    if arc_atoms < atoms + reserve:
        raise ValueError("ARC USDC %.6f cannot cover %.6f plus the %.6f reserve"
                         % (arc_atoms / 1e6, atoms / 1e6, reserve / 1e6))
    fees = sum(Decimal(str(f.get("amountUSD") or 0)) for f in estimate.get("feeCosts", []))
    gas = sum(Decimal(str(g.get("amountUSD") or 0)) for g in estimate.get("gasCosts", []))
    print("Custody %s" % wallet)
    print("ARC USDC %.6f (keeps %.6f). Base USDC before %.6f." % (arc_atoms / 1e6, (arc_atoms - atoms) / 1e6, base_before / 1e6))
    print("Send %.6f USDC via %s; estimated Base receipt %.6f; minimum %.6f; fees ~$%s + gas ~$%s; ~%ss."
          % (atoms / 1e6, quote.get("tool"), int(estimate["toAmount"]) / 1e6, minimum / 1e6, fees, gas,
             estimate.get("executionDuration")))
    print("tx1 approve: to %s spender %s amount %d" % (ARC_USDC, spender, atoms))
    print("tx2 bridge:  to %s value %s data %d bytes chainId %s" % (tr["to"], tr.get("value", "0x0"), len(tr["data"]) // 2, tr.get("chainId")))
    if not args.confirm:
        print("Quote only. Nothing signed or sent. Add --confirm to pause entries and transfer.")
        return

    request_id = str(uuid.uuid4())
    path = root / "data" / ("arc-to-base-" + request_id + ".json")
    kill_token = pause_entries(kill, request_id)
    receipt = {"request_id": request_id, "kill_token": kill_token, "wallet": wallet,
               "source_atoms": atoms, "minimum_atoms": minimum, "base_before_atoms": base_before,
               "created_at": real_utc_now_iso(), "state": "prepared", "quote_tool": quote.get("tool")}
    try:
        from sniper.execution.exec_lock import wallet_exec_lock
        from sniper.persistence.db_core import get_readonly_connection
        with ExitStack() as stack:
            for lock in ["evm-5042-" + wallet, "evm-8453-" + wallet]:
                stack.enter_context(wallet_exec_lock(lock, lock_dir=str(root / "data")))
            conn = get_readonly_connection(str(root / "sniper_tracker.db"))
            try:
                positions = conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                                         "AND is_open=1 AND network_id IN ('5042','8453')").fetchone()[0]
                pending = conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved'").fetchone()[0]
                if positions or pending:
                    raise ValueError("affected book is not flat; transfer refused")
            finally:
                conn.close()
            if erc20_balance_atoms(arc_rpc, ARC_USDC, wallet) < atoms + reserve:
                raise ValueError("ARC balance moved; transfer refused")
            gp = eth_gas_price(arc_rpc)
            nonce = eth_nonce(wallet, arc_rpc)

            def send(label, to, data, value, gas_fallback, use_nonce):
                try:
                    est = _rpc(arc_rpc, "eth_estimateGas", [{"from": wallet, "to": to, "data": data, "value": hex(value)}])
                    gas_limit = int(int(est["result"], 16) * 1.3)
                except Exception:
                    gas_limit = gas_fallback
                tx = {"to": to, "data": data, "value": value, "gas": gas_limit, "gasPrice": gp,
                      "nonce": use_nonce, "chainId": ARC_CHAIN}
                signed = sign_tx(tx, acct)
                if not signed:
                    raise ValueError(label + " signing failed")
                raw, tx_hash = signed
                if receipt.get(label + "_sig"):
                    raise ValueError("this request already sent " + label + "; use --resume")
                receipt.update({label + "_sig": tx_hash, "state": label + "_submitted"})
                _save(path, receipt)
                sent = eth_send_raw(raw, arc_rpc)
                print("%s sent: %s (rpc echo %s)" % (label, tx_hash, sent), flush=True)
                status = eth_confirm(tx_hash, arc_rpc, max_attempts=90, poll_s=2.0)
                receipt.update({label + "_status": status}); _save(path, receipt)
                if status != "confirmed":
                    raise ValueError("%s not confirmed (%s); pause retained, see %s" % (label, status, path))

            print("Receipt:", path, flush=True)
            send("approve", ARC_USDC, "0x" + SEL_APPROVE + _pad(spender) + hex(atoms)[2:].zfill(64), 0,
                 GAS_FALLBACK["approve"], nonce)
            send("bridge", tr["to"], tr["data"], int(str(tr.get("value", "0x0")), 16), GAS_FALLBACK["bridge"], nonce + 1)
            settle(path, receipt, base_rpc, kill)
    finally:
        if not path.exists() or not json.loads(path.read_text()).get("bridge_sig"):
            resume_entries(kill, request_id, kill_token)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        raise SystemExit("REFUSE: " + str(exc))
