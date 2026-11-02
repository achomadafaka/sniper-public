#!/usr/bin/env python3
"""Report or close empty classic SPL token accounts for the custody wallet."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
CLOSE_ACCOUNT_IX = 9
MAX_PER_TX = 12


def _rpc(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def empty_accounts(url, owner):
    """Return each classic SPL token account with a zero atomic balance."""
    response = _rpc(
        url,
        "getTokenAccountsByOwner",
        [owner, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed"}],
    )
    accounts = []
    for account in (response.get("result") or {}).get("value") or []:
        info = account["account"]["data"]["parsed"]["info"]
        amount = int(info["tokenAmount"]["amount"] or 0)
        if amount == 0:
            accounts.append((account["pubkey"], int(account["account"]["lamports"] or 0)))
    return accounts


def submit_and_confirm(
    rpc_url,
    signed_b64,
    expected_signature,
    *,
    send_fn=None,
    confirm_fn=None,
):
    """Send one transaction and confirm its local transaction identity."""
    send_fn = send_fn or _rpc
    if confirm_fn is None:
        from sniper.execution.solana_real_seams import rpc_confirm

        confirm_fn = rpc_confirm

    try:
        response = send_fn(
            rpc_url,
            "sendTransaction",
            [signed_b64, {"encoding": "base64", "skipPreflight": False}],
        )
    except Exception:
        return confirm_fn(expected_signature, rpc_url)

    if not isinstance(response, dict):
        return confirm_fn(expected_signature, rpc_url)
    returned_signature = response.get("result")
    if returned_signature is not None and str(returned_signature) != str(expected_signature):
        return "identity_mismatch"
    if response.get("error") is not None:
        return "send_rejected"
    return confirm_fn(expected_signature, rpc_url)


def _close_instruction(account, owner, program):
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    return Instruction(
        program_id=program,
        accounts=[
            AccountMeta(Pubkey.from_string(account), False, True),
            AccountMeta(owner, False, True),
            AccountMeta(owner, True, False),
        ],
        data=bytes([CLOSE_ACCOUNT_IX]),
    )


def main():
    parser = argparse.ArgumentParser(
        description="Report or close empty classic SPL token accounts. Token-2022 is not included."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="sign, send, and confirm each transaction",
    )
    parser.add_argument("--rpc", default=None)
    args = parser.parse_args()

    from sniper.util.envfiles import load_env_files

    load_env_files(os.environ.get("SNIPER_STATE_ROOT", ROOT))
    from sniper.execution.solana_wallet import load_keypair
    from sniper.settings import memecoin as cfg

    keypair = load_keypair(cfg.MEMECOIN_SOLANA_KEYPAIR)
    owner_text = str(keypair.pubkey())

    # PICK A HEALTHY ENDPOINT, DO NOT ASSUME CUSTODY IS ONE.
    # This defaulted to MEMECOIN_SOLANA_RPC_URL alone. On 2026-08-26 that key was rate-limited
    # ("max usage reached") and the script died on an unhandled HTTPError 429 at the first
    # getBalance — the same single-endpoint defect memecoin_swap_sol_usdc.py was fixed for the day
    # before. Two of four paid keys were spent at the time and three healthy endpoints were sitting
    # in the pool unused.
    # ONE endpoint is chosen for the WHOLE run, never switched mid-flight: --execute signs and
    # sends, and moving endpoints between the balance read and the close would report a delta
    # across two different views of the chain.
    if cfg.MEMECOIN_SOLANA_INFRA_PROVIDER == 'shyft':
        if args.rpc:
            from sniper.services.memecoin_solana_provider import provider_urls
            candidates = provider_urls('funded', {'MEMECOIN_SOLANA_INFRA_PROVIDER': 'shyft',
                'MEMECOIN_SOLANA_RPC_URL': args.rpc})
        else:
            candidates = list(cfg.solana_funded_rpc_urls())
    else:
        candidates = [args.rpc] if args.rpc else [cfg.MEMECOIN_SOLANA_RPC_URL]
        if not args.rpc:
            try:
                candidates += list(cfg.solana_funded_rpc_urls())
            except Exception:
                pass
            candidates.append(os.environ.get("MEMECOIN_RESEARCH_HELIUS_RPC_URL"))
    rpc_url = None
    before = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = (_rpc(candidate, "getBalance", [owner_text]).get("result") or {}).get("value")
        except Exception:
            continue
        if value is None:
            continue
        rpc_url, before = candidate, int(value)
        break
    if rpc_url is None:
        # An unread balance is not a zero balance, and closing accounts against a guess is worse
        # than not closing them.
        return "REFUSE: no configured Solana RPC answered a balance read. Nothing was closed."
    if not args.rpc:
        print(f"rpc              : {rpc_url.split('//')[-1].split('/')[0]}")
    targets = empty_accounts(rpc_url, owner_text)
    reclaimable = sum(lamports for _, lamports in targets)
    print(f"owner            : {owner_text}")
    print(f"balance          : {before / 1e9:.6f} SOL")
    print(f"empty accounts   : {len(targets)}")
    print(f"reclaimable rent : {reclaimable / 1e9:.6f} SOL")
    if not targets:
        return 0
    if not args.execute:
        print("\n(report only; pass --execute to close the accounts)")
        return 0

    from solders.hash import Hash
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    program = Pubkey.from_string(TOKEN_PROGRAM)
    owner = Pubkey.from_string(owner_text)
    # TAKE THE WALLET EXECUTION LOCK. Every real execution on this wallet serializes behind
    # `sol-<pubkey>` (2026-07-15 review, CRITICAL-1) because the runner thread, the follow-activity
    # thread and the web process all sign against it. This script is a FOURTH signer, and since
    # 2026-08-26 the runner sweeps rent on its own schedule too — running this unlocked while the
    # runner is live can put two close transactions on the same accounts, and can move the SOL
    # balance under an in-flight entry's gas check. Blocking here is correct: an operator running
    # this by hand wants it to happen, not to be skipped.
    from sniper.execution.exec_lock import wallet_exec_lock

    closed = 0
    with wallet_exec_lock("sol-%s" % owner_text):
      for offset in range(0, len(targets), MAX_PER_TX):
          batch_number = offset // MAX_PER_TX + 1
          batch = targets[offset:offset + MAX_PER_TX]

          # The token program also rejects a nonzero account balance.
          current_empty = {pubkey for pubkey, _ in empty_accounts(rpc_url, owner_text)}
          batch = [(pubkey, lamports) for pubkey, lamports in batch if pubkey in current_empty]
          if not batch:
              continue

          instructions = [_close_instruction(pubkey, owner, program) for pubkey, _ in batch]
          response = _rpc(rpc_url, "getLatestBlockhash", [{"commitment": "finalized"}])
          blockhash_text = (response.get("result") or {}).get("value", {}).get("blockhash")
          if not blockhash_text:
              print(f"  batch {batch_number}: failed because the RPC returned no blockhash")
              continue
          blockhash = Hash.from_string(blockhash_text)
          message = Message.new_with_blockhash(instructions, owner, blockhash)
          transaction = Transaction([keypair], message, blockhash)
          expected_signature = str(transaction.signatures[0])
          signed_b64 = base64.b64encode(bytes(transaction)).decode()
          state = submit_and_confirm(rpc_url, signed_b64, expected_signature)
          if state != "confirmed":
              print(f"  batch {batch_number}: not confirmed state={state} sig={expected_signature}")
              continue
          print(f"  batch {batch_number}: closed {len(batch)} accounts sig={expected_signature}")
          closed += len(batch)

    after = int((_rpc(rpc_url, "getBalance", [owner_text]).get("result") or {}).get("value") or 0)
    print(f"\nclosed {closed} accounts")
    print(f"balance {before / 1e9:.6f} -> {after / 1e9:.6f} SOL  (+{(after - before) / 1e9:.6f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
