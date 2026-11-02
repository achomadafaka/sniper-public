#!/usr/bin/env python3
"""Withdraw USDC from the funded Solana wallet to an address the operator names.

WHY THIS EXISTS (2026-09-13, operator: "cash out 300 usdc to a wallet of mine"). Nothing in the
repo moved USDC out of the funded wallet except the Relay bridges, which only pay our own EVM
wallet. The operator runs this script; no agent broadcasts it.

SAFETY MODEL
  - DRY-RUN by default. It builds, signs in memory and SIMULATES; nothing is broadcast.
    --confirm is required to send real money.
  - The runner prices every fill from the whole-wallet balance change, so a withdrawal that lands
    during an entry corrupts that fill. This script takes the same `sol-<pubkey>` execution lock
    every real execution takes, and it refuses while any funded Solana position is open or any
    reservation is unresolved.
  - An unreadable balance stops the run; it never reads as zero.
  - The recipient must be a wallet address (32 bytes) and not our own. Its USDC account is
    derived, and created in the same transaction when missing (we pay about 0.00204 SOL rent).
  - The transfer is TransferChecked, so the mint and 6 decimals are verified on chain.

    python3 scripts/memecoin_withdraw_solana_usdc.py --to <WALLET> --amount 300
    python3 scripts/memecoin_withdraw_solana_usdc.py --to <WALLET> --amount 300 --confirm
"""
import argparse
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.util.envfiles import load_env_files

from sniper.execution.solana_usdc_transfer import (  # noqa: E402
    USDC_DECIMALS, USDC_MINT, associated_token_account, build_signed_transfer, valid_wallet,
    withdraw_instructions)

MIN_SOL = 0.003            # fee plus the rent of a new recipient account


def open_book(db_path):
    """(open funded Solana positions, unresolved Solana reservations). Read-only."""
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=10)
    try:
        positions = conn.execute(
            "SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' AND is_open=1 "
            "AND network_id='solana'").fetchone()[0]
        reserved = conn.execute(
            "SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved' "
            "AND COALESCE(network_id,'solana')='solana'").fetchone()[0]
        return int(positions), int(reserved)
    finally:
        conn.close()


def submit_transfer(conn, *, to, amount, request_id, now_iso, signed_b64, sig, last_valid,
                    rpc, send_fn, confirm_fn):
    """Persist the identity before send. The caller must hold the wallet execution lock."""
    from sniper.persistence import db_memecoin as m
    iid = m.enqueue_withdraw(conn, "solana", to, amount, now_iso=now_iso, request_id=request_id)
    intent = m.withdraw_intent(conn, iid)
    if intent["state"] != "queued":
        return intent
    claimed = m.claim_withdraw(conn, "solana", now_iso=now_iso)
    if not claimed or claimed["intent_id"] != iid:
        raise RuntimeError("withdrawal claim changed before send")
    m.resolve_withdraw(conn, iid, "submitted", now_iso=now_iso, sig=sig,
                       last_valid_height=last_valid)
    try:
        send_fn(signed_b64, rpc)
    except Exception as exc:
        print(f"send response unavailable: {exc}")
    try:
        status = confirm_fn(sig, rpc)
    except Exception:
        status = "unavailable"
    if status in ("confirmed", "failed"):
        m.resolve_withdraw(conn, iid, "sent" if status == "confirmed" else "failed",
                           now_iso=now_iso, reason=None if status == "confirmed" else
                           "the transfer landed and failed on chain")
    return m.withdraw_intent(conn, iid)


def main():
    ap = argparse.ArgumentParser(description="Withdraw USDC from the funded Solana wallet.")
    ap.add_argument("--to", required=True, help="recipient Solana WALLET address (not a token account)")
    ap.add_argument("--amount", required=True, type=float, help="USDC to send, e.g. 300")
    ap.add_argument("--request-id", help="UUID to reuse after a lost response")
    ap.add_argument("--confirm", action="store_true", help="SIGN + broadcast real money")
    ap.add_argument("--state-root", default="/home/bigpapi/Desktop/Sniper")
    args = ap.parse_args()

    load_env_files(args.state_root)
    from sniper.settings import memecoin as cfg
    from sniper.execution.exec_lock import wallet_exec_lock
    from sniper.execution.solana_wallet import (load_keypair, native_sol_balance_value, pubkey_str,
                                                sol_balance_read, token_balance_checked)
    from sniper.services.memecoin_rent_sweep import rpc_call
    from sniper.execution.solana_real_seams import rpc_confirm

    kp = load_keypair(cfg.MEMECOIN_SOLANA_KEYPAIR)
    pk = pubkey_str(kp)
    rpc = cfg.MEMECOIN_SOLANA_RPC_URL
    db_path = os.path.join(args.state_root, "sniper_tracker.db")

    if not valid_wallet(args.to):
        sys.exit("REFUSE: --to is not a Solana wallet address (a token account address is refused)")
    if args.to == pk:
        sys.exit("REFUSE: --to is the funded wallet itself")
    if not math.isfinite(args.amount) or not (args.amount > 0):
        sys.exit("REFUSE: --amount must be positive")
    amount_atomic = int(round(args.amount * 10 ** USDC_DECIMALS))
    if args.request_id is not None:
        import uuid
        try:
            if str(uuid.UUID(args.request_id)) != args.request_id:
                raise ValueError
        except ValueError:
            sys.exit("REFUSE: --request-id must be a canonical UUID")

    with wallet_exec_lock("sol-%s" % pk, blocking=True,
                          lock_dir=os.path.join(args.state_root, "data")):
        if args.request_id is not None:
            from sniper.persistence import db_memecoin as m
            conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=10)
            try:
                result = m.withdraw_intent(
                    conn, args.request_id, expected_payload=("solana", args.to, args.amount))
            except ValueError as exc:
                sys.exit("REFUSE: %s" % exc)
            finally:
                conn.close()
            if result and result["state"] != "queued":
                print(f"request_id    {args.request_id}")
                print(f"result        {result['state']}   https://solscan.io/tx/{result.get('sig')}")
                if result["state"] != "sent":
                    sys.exit("STOP: stored withdrawal state is %s; no new transfer was sent."
                             % result["state"])
                return
        positions, reserved = open_book(db_path)
        if positions or reserved:
            sys.exit(f"REFUSE: {positions} open Solana position(s), {reserved} reservation(s). "
                     "Withdraw when the book is flat.")
        sol_read = sol_balance_read(pk, rpc)
        if not getattr(sol_read, "ok", False):
            sys.exit("REFUSE: could not read the SOL balance — stopping rather than assuming zero")
        sol = float(native_sol_balance_value(sol_read))
        usdc = token_balance_checked(pk, USDC_MINT, rpc)
        if usdc is None:
            sys.exit("REFUSE: could not read the USDC balance — stopping rather than assuming zero")
        if args.amount > float(usdc) + 1e-9:
            sys.exit(f"REFUSE: {args.amount:.2f} USDC exceeds the balance {float(usdc):.2f}")
        if sol < MIN_SOL:
            sys.exit(f"REFUSE: SOL {sol:.6f} is under the {MIN_SOL} needed for the fee and rent")

        dest = associated_token_account(args.to)
        try:
            signed_b64, sig, last_valid, create = build_signed_transfer(
                kp, pk, args.to, amount_atomic, rpc_url=rpc, call_fn=rpc_call)
        except Exception as exc:
            sys.exit("REFUSE: could not build the transfer: %s" % exc)

        print(f"from wallet   {pk}")
        print(f"to wallet     {args.to}")
        print(f"to USDC acct  {dest}  ({'CREATED in this transaction' if create else 'exists'})")
        print(f"amount        {args.amount:.6f} USDC")
        print(f"balances      USDC {float(usdc):.2f} -> {float(usdc) - args.amount:.2f} | SOL {sol:.6f}")

        sim = rpc_call(rpc, "simulateTransaction", [signed_b64, {
            "encoding": "base64", "commitment": "confirmed"}])
        err = ((sim.get("result") or {}).get("value") or {}).get("err") if "result" in sim else sim
        if err is not None:
            sys.exit(f"REFUSE: simulation failed: {err}")
        print("simulation    ok")

        if not args.confirm:
            print("\nDRY-RUN — nothing broadcast. Re-run with --confirm to send real money.")
            return

        import uuid
        from sniper.util.clock import real_utc_now_iso
        request_id = args.request_id or str(uuid.uuid4())
        print(f"request_id    {request_id}", flush=True)
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            result = submit_transfer(
                conn, to=args.to, amount=args.amount, request_id=request_id,
                now_iso=real_utc_now_iso(), signed_b64=signed_b64, sig=sig,
                last_valid=last_valid, rpc=rpc,
                send_fn=lambda signed, url: rpc_call(url, "sendTransaction", [
                    signed, {"encoding": "base64", "skipPreflight": False,
                             "preflightCommitment": "confirmed"}]),
                confirm_fn=lambda signature, url: rpc_confirm(signature, url, max_attempts=1))
        finally:
            conn.close()
        print(f"result        {result['state']}   https://solscan.io/tx/{result.get('sig')}")
        if result["state"] != "sent":
            sys.exit("STOP: the runner will resolve this request. Reuse --request-id for any retry.")



if __name__ == "__main__":
    main()
