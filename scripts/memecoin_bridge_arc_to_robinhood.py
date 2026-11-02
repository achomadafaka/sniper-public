#!/usr/bin/env python3
"""Move USDC from the ARC (5042) custody wallet to the Robinhood (4663) custody wallet AS WETH, via LI.FI.

Derived from scripts/memecoin_bridge_arc_to_base.py (2026-09-25, operator order: straight to 4663,
not through Base). ARC's USDC is the chain's gas asset AND an ERC-20 at 0x3600...0000 (6 decimals);
LI.FI (tool at capture: relaydepository) swaps and bridges it to WETH (18 decimals) on 4663 in one
call to its ARC diamond, after an EXACT-amount ERC-20 approve. Every `to`/`data` comes from the
live quote. Signing uses the SAME primitives the funded executor uses.

SAFETY MODEL (the sibling's, plus the destination-specific checks marked NEW)
  - DRY-RUN by default: prints balances, the live quote, fees and the two transactions it WOULD
    send, and signs nothing. --confirm is required to move real money.
  - Recipient is ALWAYS our own custody wallet (same key on every EVM chain), never an argument;
    the quote's echoed toAddress is compared against it before anything is signed.
  - Refuses unless the ARC and Robinhood funded books are flat (no open real position, no reserved
    entry) and no other entry pause exists; pauses entries for the transfer and clears only its
    own pause. An uncertain transfer retains the pause; --resume RECEIPT checks it without sending.
  - approve is for the EXACT bridged amount, never max-uint; the bridge tx is sent only after the
    approve is confirmed. Each broadcast is saved to the receipt BEFORE it is sent.
  - Refuses if the amount would leave less than --reserve-usdc on ARC (default 80, one entry).
  - NEW: the output floor is in DOLLARS. toAmountMin is WETH wei and fromAmount is USDC atoms, so the
    sibling's direct comparison would be a 10^12 unit error here. The wei are valued with OUR live
    ETH/USD (evm_wallet.native_usd_price(4663)), never LI.FI's own priceUSD, which would only echo
    the quote back. An unknown price refuses.
  - NEW: the spender must be LI.FI's published ARC diamond (/v1/chains diamondAddress), and the
    bridge tx must go to that same address.
  - NEW: the bridge tx must carry value 0. ARC's native asset IS USDC, so a nonzero value would
    spend on top of the approved ERC-20.
  - NEW: settlement needs the 4663 WETH balance up by at least toAmountMin. If the route delivered
    native ETH instead, that fails closed (pause retained); the receipt records the native balance
    before and after so the operator can tell. A failed balance read during settle counts as "not
    yet", never as a zero and never as a crash that strands the receipt.

  python3 scripts/memecoin_bridge_arc_to_robinhood.py --amount-usdc 30
  python3 scripts/memecoin_bridge_arc_to_robinhood.py --amount-usdc 30 --confirm
  python3 scripts/memecoin_bridge_arc_to_robinhood.py --resume data/arc-to-robinhood-<id>.json
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
ROBINHOOD_CHAIN = 4663
ARC_USDC = "0x3600000000000000000000000000000000000000"
ROBINHOOD_WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
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


def lifi_diamond(chains, chain_id):
    """LI.FI's published diamond for `chain_id` from a /v1/chains answer, lowercased."""
    for chain in chains.get("chains") or []:
        if int(chain.get("id") or 0) == chain_id and chain.get("diamondAddress"):
            return chain["diamondAddress"].lower()
    raise ValueError("LI.FI publishes no diamond for chain %d" % chain_id)


def validate_quote(quote, wallet, atoms, minimum_pct, eth_usd, diamond):
    """Bind the route to our wallet on both chains, both tokens, the amount, the spender and a
    dollar floor. Returns toAmountMin in WETH wei."""
    a, estimate = quote["action"], quote["estimate"]
    for field, chain, token, decimals in [("fromToken", ARC_CHAIN, ARC_USDC, 6),
                                          ("toToken", ROBINHOOD_CHAIN, ROBINHOOD_WETH, 18)]:
        item = a[field]
        if (item["address"].lower() != token or int(item["decimals"]) != decimals
                or int(item["chainId"]) != chain):
            raise ValueError("route token identity mismatch")
    if (int(a["fromChainId"]) != ARC_CHAIN or int(a["toChainId"]) != ROBINHOOD_CHAIN
            or a["fromAddress"].lower() != wallet.lower() or a["toAddress"].lower() != wallet.lower()
            or int(a["fromAmount"]) != atoms):
        raise ValueError("route transfer identity mismatch")
    pct = Decimal(str(minimum_pct))
    if not pct.is_finite() or not 0 < pct <= 100:
        raise ValueError("minimum received percent must be above zero and at most 100")
    price = Decimal(str(eth_usd)) if eth_usd else Decimal(0)
    if not price.is_finite() or price <= 0:
        raise ValueError("no live ETH/USD price; the WETH floor cannot be valued")
    minimum = int(estimate["toAmountMin"])
    floor_usd = Decimal(atoms) / Decimal(10**6) * pct / 100
    if Decimal(minimum) / Decimal(10**18) * price < floor_usd or int(estimate["toAmount"]) < minimum:
        raise ValueError("minimum route output is below the requested dollar floor")
    tr = quote.get("transactionRequest") or {}
    if not tr.get("data") or not tr.get("to") or int(tr.get("chainId", ARC_CHAIN)) != ARC_CHAIN:
        raise ValueError("route has no ARC transaction")
    if int(str(tr.get("value") or "0x0"), 16) != 0:
        raise ValueError("route transaction carries native value on a chain whose native asset is USDC")
    spender = str(estimate.get("approvalAddress") or "").lower()
    if not spender or spender != diamond or tr["to"].lower() != diamond:
        raise ValueError("spender or bridge target is not LI.FI's published ARC diamond")
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
    token = trip_kill_switch(str(path), source="arc-to-robinhood:" + request_id, only_if_clear=True)
    if token is None:
        raise FileExistsError("an entry pause already exists")
    return token


def resume_entries(path, request_id, token):
    from sniper.services.memecoin_arming import clear_kill_switch
    return clear_kill_switch(str(path), source="arc-to-robinhood:" + request_id, expected_token=token)


def lifi_status(tx_hash):
    return _get(LIFI + "/status?" + urllib.parse.urlencode({
        "txHash": tx_hash, "fromChain": ARC_CHAIN, "toChain": ROBINHOOD_CHAIN}))


def settlement_complete(receipt, result, weth_wei):
    """`weth_wei` None = the balance read failed: not settled, whatever LI.FI says."""
    return (weth_wei is not None
            and result.get("status") == "DONE" and result.get("substatus") == "COMPLETED"
            and (result.get("sending") or {}).get("txHash", "").lower() == receipt["bridge_sig"].lower()
            and int((result.get("receiving") or {}).get("chainId") or 0) == ROBINHOOD_CHAIN
            and bool((result.get("receiving") or {}).get("txHash"))
            and weth_wei >= receipt["weth_before_wei"] + receipt["minimum_wei"])


def _read_or_none(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:
        print("balance read unavailable:", type(exc).__name__, flush=True)
        return None


def settle(path, receipt, rh_rpc, kill, wait_s=600):
    """Poll LI.FI and the 4663 WETH balance; clear the pause only on a complete settlement."""
    from sniper.execution.evm_wallet import native_balance_raw_or_none
    deadline = time.time() + wait_s
    while True:
        result = {}
        try:
            result = lifi_status(receipt["bridge_sig"])
        except Exception as exc:
            print("status unavailable:", type(exc).__name__, flush=True)
        weth_wei = _read_or_none(erc20_balance_atoms, rh_rpc, ROBINHOOD_WETH, receipt["wallet"])
        if settlement_complete(receipt, result, weth_wei):
            receipt.update(state="settled", weth_after_wei=weth_wei,
                           native_after_wei=native_balance_raw_or_none(receipt["wallet"], rh_rpc),
                           receiving_tx=(result.get("receiving") or {}).get("txHash"))
            _save(path, receipt)
            cleared = resume_entries(kill, receipt["request_id"], receipt["kill_token"])
            print("4663 credit confirmed. WETH: %.9f. Entry pause cleared: %s" % (weth_wei / 1e18, cleared))
            return True
        if result.get("status") == "FAILED":
            receipt.update(state="bridge_failed", lifi=result); _save(path, receipt)
            print("LI.FI reports FAILED. The entry pause stays until an operator resolves it:", path)
            return False
        if time.time() > deadline:
            receipt.update(state="submitted_unknown", lifi=result, weth_last_wei=weth_wei,
                           native_last_wei=native_balance_raw_or_none(receipt["wallet"], rh_rpc))
            _save(path, receipt)
            print("Not settled within %ds; pause retained. If LI.FI says DONE but WETH did not rise, "
                  "compare native_before_wei/native_last_wei in the receipt. Re-run with --resume %s"
                  % (wait_s, path))
            return False
        print("waiting: LI.FI %s/%s, 4663 WETH %s" % (
            result.get("status"), result.get("substatus"),
            "unavailable" if weth_wei is None else "%.9f" % (weth_wei / 1e18)), flush=True)
        time.sleep(10)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--amount-usdc", help="exact USDC amount to debit on ARC (required unless --resume)")
    parser.add_argument("--reserve-usdc", default="80", help="USDC to retain on ARC (default: 80)")
    parser.add_argument("--min-received-pct", default="98.5",
                        help="minimum route output, in dollars at our live ETH/USD, as a percent of the amount")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--resume", type=Path, help="check a stored transfer; never send again")
    parser.add_argument("--state-root", default="/home/bigpapi/Desktop/Sniper")
    args = parser.parse_args()

    root = Path(args.state_root)
    from sniper.util.envfiles import load_env_files
    load_env_files(str(root))
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import load_keypair, _rpc, native_usd_price, native_balance_raw_or_none
    from sniper.execution.evm_real_seams import eth_nonce, eth_gas_price, sign_tx, eth_send_raw, eth_confirm
    from sniper.util.clock import real_utc_now_iso

    arc_rpc = (os.environ.get("MEMECOIN_EVM_RPC_URL_5042") or "").strip().strip("'\"")
    rh_rpc = (os.environ.get("MEMECOIN_EVM_RPC_URL_4663") or "").strip().strip("'\"")
    if not arc_rpc or not rh_rpc:
        raise ValueError("MEMECOIN_EVM_RPC_URL_5042 and MEMECOIN_EVM_RPC_URL_4663 must be set")
    for rpc, chain in ((arc_rpc, ARC_CHAIN), (rh_rpc, ROBINHOOD_CHAIN)):
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
        settle(args.resume, receipt, rh_rpc, kill)
        return

    if not args.amount_usdc:
        raise ValueError("--amount-usdc is required")
    atoms, reserve = usdc_atoms(args.amount_usdc), usdc_atoms(args.reserve_usdc)
    quote = _get(LIFI + "/quote?" + urllib.parse.urlencode({
        "fromChain": ARC_CHAIN, "toChain": ROBINHOOD_CHAIN, "fromToken": ARC_USDC, "toToken": ROBINHOOD_WETH,
        "fromAddress": wallet, "toAddress": wallet, "fromAmount": atoms, "slippage": "0.005"}))
    diamond = lifi_diamond(_get(LIFI + "/chains?chainTypes=EVM"), ARC_CHAIN)
    eth_usd = native_usd_price(ROBINHOOD_CHAIN)
    minimum = validate_quote(quote, wallet, atoms, args.min_received_pct, eth_usd, diamond)
    estimate, tr = quote["estimate"], quote["transactionRequest"]
    spender = estimate["approvalAddress"]

    arc_atoms = erc20_balance_atoms(arc_rpc, ARC_USDC, wallet)
    weth_before = erc20_balance_atoms(rh_rpc, ROBINHOOD_WETH, wallet)
    native_before = native_balance_raw_or_none(wallet, rh_rpc)
    if arc_atoms < atoms + reserve:
        raise ValueError("ARC USDC %.6f cannot cover %.6f plus the %.6f reserve"
                         % (arc_atoms / 1e6, atoms / 1e6, reserve / 1e6))
    fees = sum(Decimal(str(f.get("amountUSD") or 0)) for f in estimate.get("feeCosts", []))
    gas = sum(Decimal(str(g.get("amountUSD") or 0)) for g in estimate.get("gasCosts", []))
    print("Custody %s" % wallet)
    print("ARC USDC %.6f (keeps %.6f). 4663 WETH before %.9f; native ETH before %s."
          % (arc_atoms / 1e6, (arc_atoms - atoms) / 1e6, weth_before / 1e18,
             "unavailable" if native_before is None else "%.9f" % (native_before / 1e18)))
    print("Send %.6f USDC via %s; estimated 4663 WETH %.9f; minimum %.9f (~$%.2f at our ETH/USD %.2f); "
          "fees ~$%s + gas ~$%s; ~%ss."
          % (atoms / 1e6, quote.get("tool"), int(estimate["toAmount"]) / 1e18, minimum / 1e18,
             minimum / 1e18 * float(eth_usd), float(eth_usd), fees, gas, estimate.get("executionDuration")))
    print("tx1 approve: to %s spender %s (LI.FI ARC diamond) amount %d" % (ARC_USDC, spender, atoms))
    print("tx2 bridge:  to %s value %s data %d bytes chainId %s" % (tr["to"], tr.get("value", "0x0"), len(tr["data"]) // 2, tr.get("chainId")))
    if not args.confirm:
        print("Quote only. Nothing signed or sent. Add --confirm to pause entries and transfer.")
        return

    request_id = str(uuid.uuid4())
    path = root / "data" / ("arc-to-robinhood-" + request_id + ".json")
    kill_token = pause_entries(kill, request_id)
    receipt = {"request_id": request_id, "kill_token": kill_token, "wallet": wallet,
               "source_atoms": atoms, "minimum_wei": minimum, "weth_before_wei": weth_before,
               "native_before_wei": native_before, "eth_usd": eth_usd,
               "created_at": real_utc_now_iso(), "state": "prepared", "quote_tool": quote.get("tool")}
    try:
        from sniper.execution.exec_lock import wallet_exec_lock
        from sniper.persistence.db_core import get_readonly_connection
        with ExitStack() as stack:
            for lock in ["evm-5042-" + wallet, "evm-4663-" + wallet]:
                stack.enter_context(wallet_exec_lock(lock, lock_dir=str(root / "data")))
            conn = get_readonly_connection(str(root / "sniper_tracker.db"))
            try:
                positions = conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                                         "AND is_open=1 AND network_id IN ('5042','4663')").fetchone()[0]
                pending = conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved'").fetchone()[0]
                if positions or pending:
                    raise ValueError("affected book is not flat; transfer refused")
            finally:
                conn.close()
            if erc20_balance_atoms(arc_rpc, ARC_USDC, wallet) < atoms + reserve:
                raise ValueError("ARC balance moved; transfer refused")
            # The settle baseline is re-read under the 4663 lock, after the flat-book check.
            receipt["weth_before_wei"] = erc20_balance_atoms(rh_rpc, ROBINHOOD_WETH, wallet)
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
            send("bridge", tr["to"], tr["data"], 0, GAS_FALLBACK["bridge"], nonce + 1)
            settle(path, receipt, rh_rpc, kill)
    finally:
        if not path.exists() or not json.loads(path.read_text()).get("bridge_sig"):
            resume_entries(kill, request_id, kill_token)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        raise SystemExit("REFUSE: " + str(exc))
