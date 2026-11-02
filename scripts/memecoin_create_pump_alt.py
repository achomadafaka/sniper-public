#!/usr/bin/env python3
"""Create the funded wallet's pump address lookup table for quote-curve slot-lane buys.

WHY (2026-10-07, Alpha, after the non-SOL pump curve build 3581d7c62). A curve quoted in another
token (WBTC, LINK, PUMP, xStocks...) is bought as a Jupiter USDC -> quote leg plus pump's
27-account buy_exact_quote_in_v2 in one transaction. A 1-hop leg built 1,111-1,317 bytes and a
2-hop leg 1,383-1,446, against Solana's 1,232-byte packet; the worker refuses an oversized one
before the lane reserves. Twelve addresses every such buy names and no leg brings
(pump_curve_buy.pump_alt_keys) save 152 bytes in a lookup table.

WHAT IT DOES. Dry run (the default): prints the 12 keys, the payer and authority (the funded
wallet), the table's rent and the fee; it signs and sends nothing. --confirm: under the funded
wallet's execution lock (the runner prices fills by whole-wallet balance delta under the same
lock), ONE transaction creates the table and extends it with the 12 keys; the script waits for
confirmation and one more slot (a table is usable the slot after it is extended), reads the table
back, checks it holds exactly the 12 keys, and only then marks the state file the curve builder
reads (data/slot_lane_pump_alt.json) active. The runner picks it up within a minute, no restart.

ONE TABLE, EVER. Before sending, the signed transaction's signature and table address are written
to the state file as PENDING. A rerun with a pending state looks that signature up first: landed,
it verifies the table and marks it active (no new transaction); reverted, or never seen with its
blockhash expired, a new attempt is safe; anything else refuses and says to rerun in a minute. An
ACTIVE state refuses. The builder uses only an active table whose 12 keys and authority (this
wallet) verify.

OPERATOR STEPS.
  1. Quiet book: no open or pending Solana entry. --confirm holds the wallet's execution lock up
     to ~40 s, and the slot lane skips buys while it does.
  2. python3 scripts/memecoin_create_pump_alt.py              (dry run: keys, rent, fee)
  3. python3 scripts/memecoin_create_pump_alt.py --confirm
  4. If it ends NOT CONFIRMED, rerun step 3: it resolves the pending signature first.
ROLLBACK, IN THIS ORDER. (a) Delete data/slot_lane_pump_alt.json and RESTART the runner: the
builder holds the verified table until a restart. (b) Only then deactivate the table, and close it
~513 slots later to get the rent back. Deactivating first fails every quote-curve send until the
restart.
The operator runs --confirm; nobody else.
"""
import argparse
import base64
import json
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FEE_LAMPORTS = 5_000                      # one signature, no priority fee
TABLE_META_BYTES = 56                     # LookupTableMeta header; each address adds 32


def create_and_extend_instructions(authority, recent_slot, keys):
    """([create, extend], table address) for a table owned by `authority` (also the payer)."""
    from solders.instruction import AccountMeta, Instruction
    from sniper.execution.pump_curve_buy import ALT_PROGRAM, SYSTEM_PROGRAM, _pk
    program, auth = _pk(ALT_PROGRAM), _pk(authority)
    table, bump = type(program).find_program_address(
        [bytes(auth), struct.pack("<Q", int(recent_slot))], program)
    metas = [AccountMeta(table, False, True), AccountMeta(auth, True, False),
             AccountMeta(auth, True, True), AccountMeta(_pk(SYSTEM_PROGRAM), False, False)]
    create = Instruction(program, struct.pack("<IQB", 0, int(recent_slot), bump), metas)
    extend = Instruction(program, struct.pack("<IQ", 2, len(keys)) + b"".join(bytes(_pk(k)) for k in keys),
                         metas)
    return [create, extend], str(table)


def _rpc(url, method, params):
    import urllib.request
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        out = json.loads(resp.read())
    if "error" in out:
        raise RuntimeError("%s: %s" % (method, out["error"]))
    return out["result"]


def _finish(url, table, authority, keys, landed, state, base):
    """Wait one slot past `landed`, read the table back, verify it, write the ACTIVE state."""
    from sniper.execution.pump_curve_buy import verified_pump_alt
    from sniper.util.atomic import atomic_write_json
    from datetime import datetime, timezone
    while int(_rpc(url, "getSlot", [{"commitment": "confirmed"}])) <= int(landed):
        time.sleep(0.4)                   # usable the slot after it was extended
    account = _rpc(url, "getAccountInfo", [table, {"encoding": "base64", "commitment": "confirmed"}])["value"]
    held, why = verified_pump_alt(table, account, authority)
    if held is None:
        print("TABLE DOES NOT VERIFY (%s): state stays pending. Table %s." % (why, table))
        return 1
    atomic_write_json(str(state), {**base, "status": "active", "slot": landed,
                                   "activated_at": datetime.now(timezone.utc).isoformat()})
    print("DONE: %s holds the 12 keys; wrote %s (active). The runner uses it within a minute."
          % (table, state))
    return 0


def _resume(url, state, pending, confirm):
    """A pending table from an earlier run. Returns an exit code, or None when a new attempt is
    safe: its transaction reverted, or its blockhash expired unseen (it can never land)."""
    from sniper.execution.solana_real_seams import rpc_confirm
    sig, table = pending["signature"], pending["address"]
    verdict = rpc_confirm(sig, url, max_attempts=3, poll_s=1.0)
    print("pending table %s, signature %s: %s" % (table, sig, verdict))
    if verdict == "confirmed":
        if not confirm:
            print("Dry run: it landed. Add --confirm to verify it and mark it active (no new table).")
            return 0
        return _finish(url, table, pending["authority"], pending["keys"],
                       getattr(verdict, "landing_slot", None) or pending["recent_slot"], state, pending)
    if verdict == "failed":
        print("It reverted on chain: no table exists. A new attempt is safe.")
        return None
    if verdict == "not_found":
        height = int(_rpc(url, "getBlockHeight", [{"commitment": "confirmed"}]))
        if height > int(pending["last_valid_block_height"]):
            print("Never seen and its blockhash expired (height %d > %d): it cannot land. "
                  "A new attempt is safe." % (height, int(pending["last_valid_block_height"])))
            return None
    print("STILL UNRESOLVED: it may yet land. Nothing done; rerun in a minute.")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--confirm", action="store_true", help="create the table (the operator only)")
    ap.add_argument("--keypair", help="funded Solana keypair (default MEMECOIN_SOLANA_KEYPAIR)")
    ap.add_argument("--state", help="state file (default data/slot_lane_pump_alt.json)")
    args = ap.parse_args(argv)
    from sniper.execution.pump_curve_buy import PUMP_ALT_STATE
    from sniper.util.state_paths import state_path
    state = Path(args.state or state_path(*PUMP_ALT_STATE))
    if not args.confirm:
        return _run(args, state)
    # THE DECISION AND THE CREATION SHARE ONE LOCK (Codex F-SOL-2 / N12, 2026-10-07): two --confirm
    # runs that both read an absent (or expired pending) state before either locked each sent a
    # creation. The state is read inside this lock, keyed on the state file (one table per wallet);
    # the wallet's execution lock still wraps only the signing and sending, inside it. Only this
    # script takes this lock, so the order (state, then wallet) cannot deadlock the runner.
    import hashlib
    from sniper.execution.exec_lock import wallet_exec_lock
    with wallet_exec_lock("pump-alt-%s" % hashlib.sha256(str(state.resolve()).encode()).hexdigest()[:16]):
        return _run(args, state)


def _run(args, state):
    from sniper.execution.pump_curve_buy import pump_alt_keys
    from sniper.execution.solana_wallet import load_keypair
    from sniper.settings import memecoin as cfg
    from sniper.util.atomic import atomic_write_json
    prior = json.loads(state.read_text()) if state.exists() else None
    if prior is not None and prior.get("status") != "pending":
        print("REFUSED: %s exists (table %s, %s). One table per wallet; nothing done."
              % (state, prior.get("address"), prior.get("status")))
        return 2
    url = cfg.MEMECOIN_SOLANA_RPC_URL
    if not url:
        print("REFUSED: MEMECOIN_SOLANA_RPC_URL is unset")
        return 2
    if prior is not None:
        code = _resume(url, state, prior, args.confirm)
        if code is not None:
            return code
    kp = load_keypair(args.keypair or cfg.MEMECOIN_SOLANA_KEYPAIR)
    authority = str(kp.pubkey())
    keys = pump_alt_keys(authority)
    rent = int(_rpc(url, "getMinimumBalanceForRentExemption", [TABLE_META_BYTES + 32 * len(keys)]))
    print("payer and authority: %s" % authority)
    for i, key in enumerate(keys):
        print("  key %2d  %s" % (i, key))
    print("rent %d lamports (%.6f SOL, returned when the table is closed) + fee %d lamports"
          % (rent, rent / 1e9, FEE_LAMPORTS))
    if not args.confirm:
        print("Dry run. Nothing signed or sent. Add --confirm to create the table.")
        return 0
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    from sniper.execution.exec_lock import wallet_exec_lock
    from sniper.execution.solana_real_seams import rpc_confirm, rpc_send
    with wallet_exec_lock("sol-%s" % authority):
        recent_slot = int(_rpc(url, "getSlot", [{"commitment": "finalized"}]))
        ixs, table = create_and_extend_instructions(authority, recent_slot, keys)
        latest = _rpc(url, "getLatestBlockhash", [{"commitment": "confirmed"}])["value"]
        message = MessageV0.try_compile(kp.pubkey(), ixs, [], Hash.from_string(latest["blockhash"]))
        tx = VersionedTransaction(message, [kp])
        sig = str(tx.signatures[0])
        base = {"status": "pending", "address": table, "authority": authority, "keys": keys,
                "signature": sig, "recent_slot": recent_slot,
                "last_valid_block_height": int(latest["lastValidBlockHeight"])}
        atomic_write_json(str(state), base)          # identity first: a rerun resumes, never repeats
        print("table %s  signature %s (pending state written)" % (table, sig))
        sent = rpc_send(base64.b64encode(bytes(tx)).decode(), url)
        confirmed = rpc_confirm(sig, url) if sent else "unsent"
    if confirmed != "confirmed":
        print("NOT CONFIRMED (%s). The state stays pending: a rerun looks this signature up first "
              "and creates a new table only if it reverted or can no longer land." % confirmed)
        return 1
    return _finish(url, table, authority, keys,
                   getattr(confirmed, "landing_slot", None) or recent_slot, state, base)


if __name__ == "__main__":
    sys.exit(main())
