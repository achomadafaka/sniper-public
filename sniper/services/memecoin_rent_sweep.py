"""Reclaim the SOL rent locked in empty SPL token accounts left behind by closed positions.

WHY THIS IS A SYSTEM TASK AND NOT A COMMAND (operator, 2026-08-26). Every funded entry creates the
token accounts its swap route needs, and each one holds Solana's rent-exempt minimum
(~0.00203928 SOL). Selling the position empties the account but does NOT close it, so the deposit
stays locked. Measured on 2026-08-26: 52 empty accounts holding 0.106043 SOL — twice the wallet's
whole balance at the time, and twice what the operator had just bought with $5 of trading bankroll
to unblock entries. At roughly 25 closes a day the lock rate is ~0.05 SOL/day, which is the slope
that drained the wallet from healthy to 0.002196 with no fee anomaly anywhere.

Rent is a DEPOSIT, not a cost. Closing an empty account returns it in full.

TWO THINGS THIS MUST NEVER DO, and they are the whole design:

1. RUN OUTSIDE THE WALLET EXECUTION LOCK. `build_real_solana_executor` serializes every real
   execution behind `sol-<pubkey>` because the runner thread, the follow-activity thread, and the
   web process all sign against this one wallet and price fills by whole-wallet balance delta
   (2026-07-15 review, CRITICAL-1). A cron running the CLI with --execute would be a second signer
   outside that lock. This sweep takes the same lock, which is also why it belongs in the runner
   rather than in crontab.

2. CLOSE AN ACCOUNT THE TRADER STILL NEEDS. An account is only swept when its token balance is
   EXACTLY zero AND no open real position and no unresolved reservation names its mint. A held
   position has a non-zero balance so it is already excluded, but a reservation that has not filled
   yet can name a mint whose account is still empty, and closing that mid-entry is a live-money
   fault. Fail closed on every gap: an unreadable balance, an unreadable roster, or an RPC that
   will not answer all leave the account alone.

Token-2022 accounts are NOT included. The classic SPL program is the only one enumerated here, so a
Token-2022 memecoin's rent stays locked; that is a known gap, not an oversight.
"""
from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
CLOSE_ACCOUNT_IX = 9
MAX_PER_TX = 12                 # close instructions that fit one transaction comfortably


def rpc_call(url, method, params, *, timeout=30):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def pick_live_rpc(candidates, owner, *, call_fn=None):
    """First endpoint that answers a balance read, plus that balance. (None, None) if none do.

    The endpoint is chosen ONCE and used for the whole sweep: the balance read and the closes must
    see the same view of the chain, or the reclaimed delta is measured across two of them.
    """
    call_fn = call_fn or rpc_call
    for url in candidates:
        if not url:
            continue
        try:
            value = (call_fn(url, "getBalance", [owner]).get("result") or {}).get("value")
        except Exception:
            continue
        if value is not None:
            return url, int(value)
    return None, None


def empty_token_accounts(url, owner, *, call_fn=None):
    """(pubkey, mint, lamports) for every classic SPL account of `owner` holding zero tokens."""
    call_fn = call_fn or rpc_call
    response = call_fn(
        url, "getTokenAccountsByOwner",
        [owner, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed"}])
    out = []
    for account in (response.get("result") or {}).get("value") or []:
        try:
            info = account["account"]["data"]["parsed"]["info"]
            amount = int(info["tokenAmount"]["amount"] or 0)
        except (KeyError, TypeError, ValueError):
            continue                      # unparseable -> leave it alone, never guess it is empty
        if amount == 0:
            out.append((account["pubkey"], info.get("mint"), int(account["account"]["lamports"] or 0)))
    return out


def protected_mints(conn):
    """Mints the sweep must not touch: an open real position, or a reservation not yet resolved.

    Returns None when the roster cannot be read. None means UNKNOWN and the caller must sweep
    nothing — an empty set would read as "nothing is protected", which is the opposite claim.
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT mint FROM memecoin_positions "
            "WHERE execution_mode='real' AND is_open=1 AND mint IS NOT NULL "
            "UNION "
            "SELECT DISTINCT mint FROM memecoin_pending_entries "
            "WHERE status='reserved' AND mint IS NOT NULL").fetchall()
    except Exception:
        logger.exception("rent sweep could not read the protected-mint set; sweeping nothing")
        return None
    return {r[0] for r in rows if r[0]}


def sweepable(accounts, protected):
    """Empty accounts whose mint is not protected. An account with an UNKNOWN mint is kept."""
    if protected is None:
        return []
    return [(pk, mint, lamports) for pk, mint, lamports in accounts
            if mint is not None and mint not in protected]


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


def close_batch(rpc_url, keypair, owner_text, batch, *, call_fn=None, confirm_fn=None):
    """Sign and send one close transaction. Returns the confirm state string."""
    from solders.hash import Hash
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    call_fn = call_fn or rpc_call
    if confirm_fn is None:
        from sniper.execution.solana_real_seams import rpc_confirm
        # BOUNDED. The default poll is 40 x 1s and this runs holding the wallet lock; a chore must
        # not own the wallet for 40 seconds. Unconfirmed here means unknown, and the next pass
        # retries — a close of an already-closed account fails harmlessly.
        def confirm_fn(signature, url):
            return rpc_confirm(signature, url, max_attempts=8, poll_s=0.5)

    program = Pubkey.from_string(TOKEN_PROGRAM)
    owner = Pubkey.from_string(owner_text)
    # FINALIZED, not confirmed: a `confirmed` blockhash is routinely rejected at send while
    # simulation returns err:null (finding-solana-preflight-commitment-blockhash-2026-08-14).
    blockhash_text = ((call_fn(rpc_url, "getLatestBlockhash", [{"commitment": "finalized"}])
                       .get("result") or {}).get("value") or {}).get("blockhash")
    if not blockhash_text:
        return "no_blockhash"
    message = Message.new_with_blockhash(
        [_close_instruction(pk, owner, program) for pk, _mint, _lam in batch],
        owner, Hash.from_string(blockhash_text))
    tx = Transaction([keypair], message, Hash.from_string(blockhash_text))
    import base64
    signed_b64 = base64.b64encode(bytes(tx)).decode()
    signature = str(tx.signatures[0])
    try:
        response = call_fn(rpc_url, "sendTransaction",
                           [signed_b64, {"encoding": "base64", "skipPreflight": False}])
    except Exception:
        # Ambiguous, never "did not send": confirm the local identity rather than resend.
        return confirm_fn(signature, rpc_url)
    if not isinstance(response, dict):
        return confirm_fn(signature, rpc_url)
    returned = response.get("result")
    if returned is not None and str(returned) != signature:
        return "identity_mismatch"
    if response.get("error") is not None:
        return "send_rejected"
    return confirm_fn(signature, rpc_url)


def rent_sweep_once(conn, *, rpc_candidates, keypair, owner_text, min_accounts=8,
                    max_accounts=MAX_PER_TX, execute=True, call_fn=None, confirm_fn=None,
                    lock_dir=None, already_locked=False):
    """One sweep pass. Returns a tally dict; never raises.

    `min_accounts` keeps the sweep from spending a transaction fee to reclaim one account's rent.
    `max_accounts` bounds a pass to a single transaction so it can never monopolise the wallet lock
    while the entry path wants it.
    """
    tally = {"found": 0, "protected": 0, "closed": 0, "reclaimed_sol": 0.0, "state": None}
    try:
        rpc_url, _balance = pick_live_rpc(rpc_candidates, owner_text, call_fn=call_fn)
        if rpc_url is None:
            tally["state"] = "no_rpc"
            return tally
        accounts = empty_token_accounts(rpc_url, owner_text, call_fn=call_fn)
        tally["found"] = len(accounts)
        protected = protected_mints(conn)
        targets = sweepable(accounts, protected)
        tally["protected"] = len(accounts) - len(targets)
        if len(targets) < int(min_accounts):
            tally["state"] = "below_min"
            return tally
        batch = targets[:int(max_accounts)]
        tally["reclaimed_sol"] = sum(lam for _pk, _m, lam in batch) / 1e9
        if not execute:
            tally["state"] = "report_only"
            return tally

        # NON-BLOCKING wallet lock. Every real execution on this wallet serializes behind
        # `sol-<pubkey>`; taking it blocking would make a latency-critical entry queue behind a
        # chore. A skipped pass costs nothing — the rent is still there in 30 minutes.
        # `already_locked` is for a CALLER THAT ALREADY HOLDS THIS LOCK — today the gas top-up,
        # which reclaims before it buys. Without it the non-blocking acquire below would fail
        # against the caller's own lock, return `wallet_busy`, and the reclaim would silently never
        # happen: a feature that looks wired and does nothing. Everything else, including the
        # in-lock protected-mint recheck, is unchanged.
        import contextlib as _ctx
        from sniper.execution.exec_lock import wallet_exec_lock
        _guard = (_ctx.nullcontext(True) if already_locked
                  else wallet_exec_lock("sol-%s" % owner_text, blocking=False, lock_dir=lock_dir))
        with _guard as held:
            if not held:
                tally["state"] = "wallet_busy"
                tally["reclaimed_sol"] = 0.0
                return tally
            # RE-READ the protected set inside the lock. Between the read above and this point a
            # reservation can be created for a mint whose account is still empty, and closing that
            # account mid-entry is a live-money fault.
            fresh = protected_mints(conn)
            if fresh is None:
                tally["state"] = "roster_unreadable"
                tally["reclaimed_sol"] = 0.0
                return tally
            batch = [row for row in batch if row[1] not in fresh]
            if not batch:
                tally["state"] = "all_protected_on_recheck"
                tally["reclaimed_sol"] = 0.0
                return tally
            tally["reclaimed_sol"] = sum(lam for _pk, _m, lam in batch) / 1e9
            state = close_batch(rpc_url, keypair, owner_text, batch,
                                call_fn=call_fn, confirm_fn=confirm_fn)
        tally["state"] = state
        if state == "confirmed":
            tally["closed"] = len(batch)
        else:
            # Unconfirmed is UNKNOWN, not failed. The close may still land; re-closing an account
            # that is already gone fails harmlessly, so the next pass simply retries.
            tally["reclaimed_sol"] = 0.0
        return tally
    except Exception:
        logger.exception("rent sweep pass failed")
        tally["state"] = "error"
        return tally
