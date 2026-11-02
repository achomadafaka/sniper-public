#!/usr/bin/env python3
"""Wrap native gas above the reserve into the chain's wrapped cash token: ETH->WETH on Robinhood
(4663, the default) or BNB->WBNB on BSC (--chain 56). The wrapped token is the chain's cash.

BSC (2026-09-28, operator via Alpha): the funded runner sizes BSC entries from WBNB
(CHAIN_QUOTE["56"]), and the custody wallet held 0.1912 BNB native against 0.0000045 WBNB, so every
BSC clip was $0. On 56 the reserve is the runner's OWN native gas need, not only the top-up target:
the largest of the top-up target, the chain's MEMECOIN_EVM_GAS_RESERVE_MIN_NATIVE floor, and the
gate's `_gas_reserve` for the last entry that fits the chain book (max_concurrent - 1 open) plus
HEADROOM_ROUND_TRIPS full round trips of entry, approval and exit gas, all times the retry factor.
The dry run prints every term.

The runner's gas top-up (memecoin_evm_gas_topup.py) already does the opposite: it UNWRAPS WETH to
native when native falls under MEMECOIN_EVM_GAS_TOPUP_LOW_NATIVE and refills to
MEMECOIN_EVM_GAS_TOPUP_TARGET_NATIVE (settings: MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI). So the reserve this script keeps defaults to that TARGET:
wrapping below it would only make the runner unwrap it back on the next pass. WETH.deposit() is
1:1 and cannot lose money except the transaction's own gas.

SAFETY MODEL (same as its siblings)
  - DRY-RUN by default: prints balances and the exact transaction it WOULD send, signs nothing.
    --confirm is required to move real money.
  - Wraps only what sits ABOVE the reserve plus the transaction's own gas; refuses when that is
    under --min-wrap-eth (default 0.001), so a rounding sliver never costs a transaction.
  - The WETH contract is CHAIN_QUOTE["4663"]["token"], the address the funded executor already
    trades through; nothing is hardcoded here.
  - Signing uses the executor's own primitives (load_keypair / eth_nonce / eth_gas_price /
    sign_tx / eth_send_raw / eth_confirm).
  - Shares the funded wallet's coordination (Codex review 2026-09-23, A5): the per-(wallet, chain)
    exec lock is taken non-blocking with a bounded wait and the balances and nonce are re-read
    under it, so a funded 4663 entry and this wrap never interleave nonces or corrupt the
    executor's whole-wallet balance deltas. A durable receipt data/robinhood-wrap-<id>.json is
    written BEFORE the broadcast and updated with the confirmation status, so an unknown
    confirmation is on disk rather than only on the terminal. It is not resumed automatically.

  - No reservation in flight: a funded entry on the chain that has reserved but not settled
    blocks --confirm (read from the ledger, read-only; an unreadable ledger also blocks).

  python3 scripts/memecoin_robinhood_wrap_excess_gas.py                 # dry-run, 4663
  python3 scripts/memecoin_robinhood_wrap_excess_gas.py --confirm       # wrap the excess
  python3 scripts/memecoin_robinhood_wrap_excess_gas.py --reserve-eth 0.008 --confirm
  python3 scripts/memecoin_robinhood_wrap_excess_gas.py --chain 56      # dry-run, BSC
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time as _time
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
SEL_DEPOSIT = "0xd0e30db0"          # WETH.deposit() / WBNB.deposit(), same selector
GAS_LIMIT_FALLBACK = 60_000
# Per chain: labels, the receipt file prefix, and where the reserve comes from. 4663 keeps its
# reserve (the top-up target) and receipt names byte for byte.
CHAINS = {
    8453: {"label": "Base", "native": "ETH", "wrapped": "WETH",
           "receipt_prefix": "base-wrap-", "reserve": "gas_need"},
    4663: {"label": "Robinhood", "native": "ETH", "wrapped": "WETH",
           "receipt_prefix": "robinhood-wrap-", "reserve": "topup_target"},
    56: {"label": "BSC", "native": "BNB", "wrapped": "WBNB",
         "receipt_prefix": "bsc-wrap-", "reserve": "gas_need"},
}
GAS_TOPUP_CHAINS = frozenset({"4663"})   # where the pass takes the global TARGET (4663 only)


def gas_need_reserve(chain, cfg):
    """(reserve in native units, {term: value}) from the runner's own gas settings for `chain`.

    The gate's `_gas_reserve` at the fullest book an entry can still open into (max_concurrent - 1
    open), plus HEADROOM_ROUND_TRIPS round trips (entry + approval + exit + approval) at the retry
    factor, never below the top-up target (wrapping below it makes the runner unwrap it back) or
    the chain's configured minimum. The book and headroom are the runner gas pass's own numbers
    (memecoin_evm_gas_topup.chain_gas_need_native), so this reserve and the pass's 56 target agree."""
    from sniper.services.memecoin_evm_gas_topup import chain_gas_need_native
    key = str(chain)
    book, headroom, terms = chain_gas_need_native(key, cfg)
    # The global top-up target is 4663's ETH number. Raising it for the 4663 native float
    # (2026-09-28, 0.145) must not become a BSC reserve; the pass gives 56 its own bounds.
    target = (int(cfg.MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI) / 1e18) if key in GAS_TOPUP_CHAINS else 0.0
    floor = float(cfg.gas_reserve_min_native_for(key))
    terms = {**terms, "topup_target": target, "configured_floor": floor}
    return max(target, floor, book + headroom), terms


def default_reserve(chain, cfg):
    """(reserve, terms): the top-up target on 4663 (unchanged), the runner's gas need on 56."""
    if CHAINS[chain]["reserve"] == "gas_need":
        return gas_need_reserve(chain, cfg)
    target = int(cfg.MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI) / 1e18
    return target, {"topup_target": target}


def reservations_in_flight(root, chain):
    """Unsettled funded reservations on `chain`, from the ledger read-only. None = unreadable,
    which the caller treats as blocking: 'we could not look' is not 'nothing is in flight'."""
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.persistence import db_memecoin as m
    db = os.environ.get("DB_PATH") or "sniper_tracker.db"
    db = db if os.path.isabs(db) else str(root / db)
    if not os.path.exists(db):
        return None
    conn = None
    try:
        conn = get_readonly_connection(db)
        return int(m.pending_totals(conn, network=str(chain))[0])
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w") as handle:
        json.dump(value, handle, indent=2)
        handle.flush(); os.fsync(handle.fileno())
    temp.replace(path)


# A RECEIPT IS RESOLVED ONLY WHEN THE CHAIN HAS ANSWERED (A5 recheck, 2026-09-23). `done` means the
# wrap confirmed; `wrap_failed` means the chain says it reverted, so the nonce is consumed and
# nothing is outstanding. Everything else -- `wrap_submitted` (a crash between the broadcast and the
# confirmation), `wrap_unknown` (no receipt inside the poll), a file this script cannot parse, a
# receipt with no hash -- is a transaction that MAY STILL LAND, and signing a second wrap at the
# next nonce past one of those is how a maintenance script doubles a real transfer.
_RESOLVED_STATES = frozenset({"done", "wrap_failed"})


def _receipts(root):
    """(path, parsed-or-None) for every wrap receipt on disk, every chain. An unreadable one yields
    None, and the caller must treat that as unresolved rather than as absent -- fail closed."""
    paths = set()
    for spec in CHAINS.values():
        paths.update((root / "data").glob(spec["receipt_prefix"] + "*.json"))
    for path in sorted(paths):
        try:
            yield path, json.loads(path.read_text())
        except (OSError, ValueError):
            yield path, None


def _unresolved(root, wallet, chain):
    """Receipts for THIS wallet and chain that the chain has not settled. Matched on the receipt's
    own `wallet`/`chain` fields, never on the filename, so another wallet's receipt cannot block
    this one and this wallet's cannot be missed."""
    out = []
    for path, data in _receipts(root):
        if data is None:
            out.append((path, None))                      # unparseable: unknown, so it blocks
            continue
        same = (str(data.get("wallet") or "").lower() == str(wallet).lower()
                and str(data.get("chain") or "") == str(chain))
        if same and str(data.get("state") or "") not in _RESOLVED_STATES:
            out.append((path, data))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chain", type=int, default=4663, choices=sorted(CHAINS),
                        help="4663 (Robinhood, default), 56 (BSC) or 8453 (Base)")
    parser.add_argument("--reserve-native", "--reserve-eth", dest="reserve_native",
                        help="native to keep (default: the top-up target on 4663, the runner's gas "
                             "need on 56); never below that default")
    parser.add_argument("--min-wrap-native", "--min-wrap-eth", dest="min_wrap_native",
                        default="0.001", help="refuse to wrap less than this (default 0.001)")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--state-root", default="/home/bigpapi/Desktop/Sniper")
    parser.add_argument("--lock-wait-s", default="60", help="seconds to wait for the wallet lock before refusing")
    parser.add_argument("--reconcile", action="store_true",
                        help="re-poll unresolved receipts against the chain and exit; signs nothing")
    args = parser.parse_args()

    root = Path(args.state_root).resolve()
    # This CLI owns its process. Bind every executor generation/key path to custody state.
    os.environ['SNIPER_STATE_ROOT'] = str(root)
    from sniper.util.envfiles import load_env_files
    load_env_files(str(root))
    from sniper.util.state_paths import configure_memecoin_state_env
    configure_memecoin_state_env()
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import (CHAIN_QUOTE, load_keypair, _rpc, native_balance_raw_or_none,
                                             erc20_balance_raw, native_quote_chains)
    from sniper.execution.evm_real_seams import eth_nonce, eth_gas_price, sign_tx, eth_send_raw, eth_confirm

    CHAIN = int(args.chain)
    spec = CHAINS[CHAIN]
    if str(CHAIN) not in native_quote_chains():
        raise ValueError("chain %d's cash is not a wrapped native token; nothing to wrap" % CHAIN)
    rpc_env = "MEMECOIN_EVM_RPC_URL_%d" % CHAIN
    rpc = (os.environ.get(rpc_env) or "").strip().strip("'\"")
    if not rpc:
        raise ValueError(rpc_env + " must be set")
    if int(_rpc(rpc, "eth_chainId", [])["result"], 16) != CHAIN:
        raise ValueError("RPC does not serve chain %d" % CHAIN)
    weth = CHAIN_QUOTE[str(CHAIN)]["token"]
    acct = load_keypair(cfg.MEMECOIN_EVM_KEYPAIR)
    wallet = acct.address

    floor_reserve, reserve_terms = default_reserve(CHAIN, cfg)
    reserve = float(args.reserve_native) if args.reserve_native else floor_reserve
    min_wrap = float(args.min_wrap_native)
    if reserve < floor_reserve:
        what = ("the runner's top-up target" if spec["reserve"] == "topup_target"
                else "the runner's own gas need")
        raise ValueError("reserve %.4f is under %s %.4f; it would unwrap it back or starve entries"
                         % (reserve, what, floor_reserve))
    in_flight = reservations_in_flight(root, CHAIN)
    outstanding = _unresolved(root, wallet, CHAIN)
    if args.reconcile:
        # READ-ONLY RESUME. One receipt poll per unresolved receipt, on the hash that receipt
        # already holds, written back through the same file. Nothing is signed and nothing is
        # broadcast, so this needs no --confirm; a read that fails leaves the receipt exactly as
        # it was, because "we could not look" is not "it did not land".
        if not outstanding:
            print("No unresolved wrap receipts for %s on chain %d." % (wallet, CHAIN))
            return
        for path, data in outstanding:
            if data is None:
                print("%s: unreadable, cannot be reconciled automatically -- needs a human." % path)
                continue
            sig = str(data.get("wrap_sig") or "")
            if not sig:
                print("%s: no transaction hash recorded, cannot be reconciled -- needs a human." % path)
                continue
            status = eth_confirm(sig, rpc, max_attempts=1, poll_s=0.0)
            if status in ("confirmed", "failed"):
                data.update({"wrap_status": status,
                             "state": "done" if status == "confirmed" else "wrap_" + status})
                if status == "confirmed":
                    after = erc20_balance_raw(wallet, weth, rpc)
                    if after is not None:
                        data["weth_after_wei"] = int(after)
                _save(path, data)
            print("%s: %s (%s)" % (path, status, data.get("state")))
        return
    if in_flight != 0:
        why = ("the ledger could not be read" if in_flight is None
               else "%d funded reservation(s) in flight on chain %d" % (in_flight, CHAIN))
        if args.confirm:
            raise ValueError(why + "; a wrap must not move the wallet under an entry. Nothing sent")
        print("BLOCKING: %s. A --confirm run would refuse." % why)
    if outstanding:
        lines = "; ".join("%s state=%s" % (path.name, (data or {}).get("state") or "unreadable")
                          for path, data in outstanding)
        if args.confirm:
            raise ValueError(
                "%d unresolved wrap receipt(s) for this wallet: %s. A wrap that may still land "
                "must settle before another is signed -- run with --reconcile (it polls the chain "
                "and signs nothing), then re-run" % (len(outstanding), lines))
        # Dry run stays useful: say what blocks, then still show the quote below.
        print("BLOCKING: %d unresolved wrap receipt(s): %s" % (len(outstanding), lines))
        print("A --confirm run would refuse. Run --reconcile first.")
    native_wei = native_balance_raw_or_none(wallet, rpc)
    weth_wei = erc20_balance_raw(wallet, weth, rpc)
    if native_wei is None or weth_wei is None:
        raise ValueError("balance read unavailable")
    gas_price = eth_gas_price(rpc)
    tx_gas_wei = GAS_LIMIT_FALLBACK * int(gas_price)
    excess_wei = native_wei - int(reserve * 1e18) - tx_gas_wei
    print("Custody %s on %s" % (wallet, spec["label"]))
    print("native %s %.6f, %s %.6f (cash). Reserve %.4f %s + tx gas ~%.6f." % (
        spec["native"], native_wei / 1e18, spec["wrapped"], weth_wei / 1e18, reserve,
        spec["native"], tx_gas_wei / 1e18))
    if spec["reserve"] == "gas_need":
        print("Reserve terms: " + ", ".join("%s=%s" % (k, ("%.6f" % v) if isinstance(v, float) else v)
                                            for k, v in reserve_terms.items()))
    if excess_wei < int(min_wrap * 1e18):
        print("Excess above the reserve is %.6f %s, under the %.4f minimum. Nothing to wrap." % (
            max(0, excess_wei) / 1e18, spec["native"], min_wrap))
        return
    print("Would wrap %.6f %s -> %s via %s deposit()." % (
        excess_wei / 1e18, spec["native"], spec["wrapped"], weth))
    if not args.confirm:
        print("Quote only. Nothing signed or sent. Add --confirm to wrap.")
        return
    # THE FUNDED WALLET'S OWN LOCK, the same key the executor serializes on (evm-<chain>-<wallet>).
    # Non-blocking with a bounded wait: maintenance must never make a funded entry queue behind
    # it silently, and a wrap that cannot get its turn is refused, not delayed.
    from sniper.execution.exec_lock import wallet_exec_lock
    from sniper.util.clock import real_utc_now_iso
    lock_key = "evm-%d-%s" % (CHAIN, wallet)
    deadline = _time.time() + float(args.lock_wait_s)
    lock = wallet_exec_lock(lock_key, lock_dir=str(root / "data"), blocking=False)
    while not lock.__enter__():
        lock.__exit__(None, None, None)
        if _time.time() >= deadline:
            raise ValueError("the %s wallet lock is held by another process "
                             "(see lsof data/real_exec_evm-%d-*.lock); nothing sent"
                             % (spec["label"], CHAIN))
        print("waiting for the %s wallet lock ..." % spec["label"], flush=True); _time.sleep(5)
        lock = wallet_exec_lock(lock_key, lock_dir=str(root / "data"), blocking=False)
    try:
        # Re-read under the lock: the numbers above were taken while another actor could still
        # have been spending, and the amount signed must be the amount that is really there.
        # AND THE RECEIPT CHECK AGAIN, UNDER THE LOCK. Two invocations racing between the glob
        # above and this lock would otherwise both pass it and both sign -- the same reason the
        # balances are re-read here rather than trusted from before the wait.
        outstanding = _unresolved(root, wallet, CHAIN)
        if outstanding:
            raise ValueError("an unresolved wrap receipt appeared while waiting for the lock (%s); "
                             "nothing sent" % ", ".join(path.name for path, _d in outstanding))
        in_flight = reservations_in_flight(root, CHAIN)
        if in_flight != 0:
            raise ValueError("a funded reservation is in flight on chain %d (or the ledger could "
                             "not be read) under the lock; nothing sent" % CHAIN)
        native_wei = native_balance_raw_or_none(wallet, rpc)
        weth_before = erc20_balance_raw(wallet, weth, rpc)
        if native_wei is None or weth_before is None:
            raise ValueError("balance read unavailable under the lock")
        gas_price = eth_gas_price(rpc)
        tx_gas_wei = GAS_LIMIT_FALLBACK * int(gas_price)
        excess_wei = native_wei - int(reserve * 1e18) - tx_gas_wei
        if excess_wei < int(min_wrap * 1e18):
            raise ValueError("excess fell under the minimum once re-read under the lock; nothing sent")
        nonce = eth_nonce(wallet, rpc)
        try:
            est = _rpc(rpc, "eth_estimateGas", [{"from": wallet, "to": weth, "data": SEL_DEPOSIT, "value": hex(excess_wei)}])
            gas_limit = int(int(est["result"], 16) * 1.3)
        except Exception:
            gas_limit = GAS_LIMIT_FALLBACK
        tx = {"to": weth, "data": SEL_DEPOSIT, "value": int(excess_wei), "gas": gas_limit,
              "gasPrice": int(gas_price), "nonce": nonce, "chainId": CHAIN}
        signed = sign_tx(tx, acct)
        if not signed:
            raise ValueError("signing failed")
        raw, tx_hash = signed
        request_id = str(uuid.uuid4())
        path = root / "data" / (spec["receipt_prefix"] + request_id + ".json")
        receipt = {"request_id": request_id, "wallet": wallet, "chain": CHAIN, "weth": weth,
                   "wrap_wei": int(excess_wei), "nonce": nonce, "gas": gas_limit,
                   "gas_price": int(gas_price), "weth_before_wei": int(weth_before),
                   "created_at": real_utc_now_iso(), "wrap_sig": tx_hash, "state": "wrap_submitted"}
        _save(path, receipt)                      # identity on disk BEFORE any network I/O
        print("Receipt:", path, flush=True)
        sent = eth_send_raw(raw, rpc)
        print("sent %s (rpc echo %s)" % (tx_hash, sent), flush=True)
        status = eth_confirm(tx_hash, rpc, max_attempts=90, poll_s=2.0)
        receipt.update({"wrap_status": status, "state": "done" if status == "confirmed" else "wrap_" + str(status)})
        if status == "confirmed":
            receipt["weth_after_wei"] = int(erc20_balance_raw(wallet, weth, rpc))
        _save(path, receipt)
        print("status:", status)
        if status == "confirmed":
            print("%s after: %.6f" % (spec["wrapped"], receipt["weth_after_wei"] / 1e18))
        else:
            print("NOT CONFIRMED (%s): the receipt at %s holds the signed hash; check the chain "
                  "before running again" % (status, path))
    finally:
        lock.__exit__(None, None, None)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        raise SystemExit("REFUSE: " + str(exc))
