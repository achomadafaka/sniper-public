#!/usr/bin/env python3
"""Move USDC from the funded Solana wallet to the funded Base (8453) wallet via Relay.link.

WHY a script: this is the mirror of scripts/memecoin_fund_robinhood.py (Base -> Robinhood), for the
one direction that script cannot do. Its origin chain, origin currency, and send path are EVM
constants, so a Solana origin needs its own entry point.

WHY it builds a transaction: for an EVM origin Relay returns `to`/`data`/`value` and the existing
script signs that. For a Solana origin Relay returns raw INSTRUCTIONS plus address lookup table
addresses, so this script compiles the v0 message itself. That compile step is the only new code —
the keypair load, the balance reads, the broadcast, and the confirm poll are the same functions the
live memecoin executor uses.

SAFETY MODEL
  - DRY-RUN by default: quotes live, prints what it WOULD send, signs nothing. --confirm is
    required to move real money.
  - The recipient is ALWAYS our own Base address, derived from our own EVM keypair. It is never a
    command-line argument, so a typo cannot send funds to a stranger.
  - Refuses if a funded Solana reservation is in flight. An in-flight reservation is sized against
    this same balance, so bridging under it can strand an entry the runner already committed to.
  - Refuses if the amount exceeds the spendable USDC (balance minus --reserve), or if native SOL is
    below the fee floor.
  - Every instruction is checked: the only account marked as a signer may be our own pubkey.
  - The quote's echoed recipient is compared against our own address before anything is signed.

    python3 scripts/memecoin_bridge_solana_base.py --usd 100 --dry-run   # inspect
    python3 scripts/memecoin_bridge_solana_base.py --usd 100 --confirm   # move real money
"""
import argparse
import base64
import json
import os
import sqlite3
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.util.envfiles import load_env_files

SOLANA_RELAY_CHAIN = 792703809          # Relay's chain id for Solana mainnet
BASE_CHAIN = 8453
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
RELAY_QUOTE_URL = "https://api.relay.link/quote"
MIN_SOL_FEE_FLOOR = 0.003               # native SOL kept back for the fee + rent
DEFAULT_RESERVE_USDC = 50.0             # trading bankroll left on Solana


def _http_post_json(url, body, timeout=40):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def relay_quote(sol_pubkey, base_address, amount_human):
    """Live Relay quote, Solana USDC -> Base USDC, from our wallet to our own Base address."""
    body = {
        "user": sol_pubkey,
        "recipient": base_address,                    # our own Base wallet, always
        "originChainId": SOLANA_RELAY_CHAIN, "destinationChainId": BASE_CHAIN,
        "originCurrency": SOLANA_USDC, "destinationCurrency": BASE_USDC,
        "amount": str(int(round(amount_human * 10 ** 6))), "tradeType": "EXACT_INPUT",
    }
    quote = _http_post_json(RELAY_QUOTE_URL, body)
    if "steps" not in quote:
        raise RuntimeError("Relay: no route (%s)" % (
            quote.get("message") or quote.get("errorCode") or quote))
    return quote


def relay_instructions(quote):
    """Flatten Relay steps -> (instructions, lookup table addresses, request id).

    Raises when a step carries EVM calldata: that means Relay routed the origin somewhere this
    script cannot sign, and signing the wrong shape must fail loudly rather than silently skip.
    """
    instructions, tables, request_id = [], [], None
    for step in quote.get("steps", []):
        for item in step.get("items", []):
            data = item.get("data") or {}
            if "instructions" not in data:
                raise RuntimeError("step %r is not a Solana instruction set (keys=%s)"
                                   % (step.get("id"), sorted(data.keys())))
            instructions.extend(data["instructions"])
            tables.extend(data.get("addressLookupTableAddresses") or [])
            check = item.get("check") or {}
            request_id = request_id or check.get("endpoint")
    if not instructions:
        raise RuntimeError("Relay returned no instructions")
    return instructions, tables, request_id


def build_instruction(raw, our_pubkey):
    """One Relay instruction -> a solders Instruction, refusing any signer that is not us."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey
    metas = []
    for key in raw["keys"]:
        pubkey = Pubkey.from_string(key["pubkey"])
        if key.get("isSigner") and str(pubkey) != str(our_pubkey):
            raise RuntimeError("instruction wants a foreign signer: %s" % pubkey)
        metas.append(AccountMeta(pubkey=pubkey, is_signer=bool(key.get("isSigner")),
                                 is_writable=bool(key.get("isWritable"))))
    return Instruction(program_id=Pubkey.from_string(raw["programId"]), accounts=metas,
                       data=bytes.fromhex(raw["data"]))


def fetch_lookup_tables(addresses, rpc_url):
    """Read each address lookup table account and decode it for MessageV0.try_compile."""
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.pubkey import Pubkey
    out = []
    for address in addresses:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                           "params": [address, {"encoding": "base64"}]})
        req = urllib.request.Request(rpc_url, data=body.encode(),
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode())
        value = (payload.get("result") or {}).get("value")
        if not value:
            raise RuntimeError("lookup table %s not found" % address)
        raw = base64.b64decode(value["data"][0])
        # An ALT account is a 56-byte header followed by a packed array of 32-byte pubkeys.
        addrs = [Pubkey.from_bytes(raw[i:i + 32]) for i in range(56, len(raw), 32)]
        out.append(AddressLookupTableAccount(key=Pubkey.from_string(address), addresses=addrs))
    return out


def pending_solana_reservation(db_path):
    """Count in-flight funded Solana reservations. They are sized against this same balance."""
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM memecoin_pending_entries "
            "WHERE status='pending' AND COALESCE(network_id,'solana')='solana'").fetchone()[0]
    finally:
        conn.close()


def describe(quote):
    details = quote.get("details", {})
    currency_in = details.get("currencyIn", {})
    currency_out = details.get("currencyOut", {})
    return ("  IN : %s %s on Solana\n"
            "  OUT: %s %s on Base\n"
            "  rate=%s  impact=%s%%  ~%ss" % (
                currency_in.get("amountFormatted"),
                currency_in.get("currency", {}).get("symbol"),
                currency_out.get("amountFormatted"),
                currency_out.get("currency", {}).get("symbol"),
                details.get("rate"),
                (details.get("totalImpact") or {}).get("percent"),
                details.get("timeEstimate")))


def main():
    parser = argparse.ArgumentParser(
        description="Bridge Solana USDC -> Base (8453) USDC via Relay, wallet to own wallet.")
    parser.add_argument("--usd", type=float, required=True, help="USDC amount to move")
    parser.add_argument("--reserve", type=float, default=DEFAULT_RESERVE_USDC,
                        help="USDC to leave on Solana as trading bankroll (default %.0f)"
                             % DEFAULT_RESERVE_USDC)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="inspect only (default)")
    mode.add_argument("--confirm", action="store_true", help="SIGN + broadcast real money")
    parser.add_argument("--state-root", default=os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    args = parser.parse_args()

    load_env_files(args.state_root)
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import load_keypair as load_evm_keypair
    from sniper.execution.solana_wallet import (load_keypair, pubkey_str, sol_balance,
                                                usdc_balance)
    from sniper.execution.solana_real_seams import rpc_confirm, rpc_send

    keypair = load_keypair(cfg.MEMECOIN_SOLANA_KEYPAIR)
    sol_pubkey = pubkey_str(keypair)
    base_address = load_evm_keypair(cfg.MEMECOIN_EVM_KEYPAIR).address
    # The funded runner broadcasts through solana_funded_rpc_urls() — the credited pool first, with
    # MEMECOIN_SOLANA_RPC_URL (public api.mainnet-beta) only as the LAST fallback. Reading that
    # fallback directly is what made the first send attempt bounce: the public endpoint rate-limits
    # sendTransaction, so a perfectly valid transaction came back with no result.
    rpc_urls = cfg.solana_funded_rpc_urls()
    if not rpc_urls:
        sys.exit("REFUSED: no Solana RPC configured")
    rpc_url = rpc_urls[0]

    print("from   %s (Solana)" % sol_pubkey)
    print("to     %s (Base %d)" % (base_address, BASE_CHAIN))

    in_flight = pending_solana_reservation(os.path.join(args.state_root, "sniper_tracker.db"))
    if in_flight:
        sys.exit("REFUSED: %d funded Solana reservation(s) in flight; that balance is committed."
                 % in_flight)

    native = sol_balance(sol_pubkey, rpc_url)
    usdc = usdc_balance(sol_pubkey, rpc_url)
    spendable = usdc - args.reserve
    print("balance  %.6f SOL   %.6f USDC   (spendable after $%.2f reserve: %.6f)"
          % (native, usdc, args.reserve, spendable))
    if native < MIN_SOL_FEE_FLOOR:
        sys.exit("REFUSED: native SOL %.6f below fee floor %.6f" % (native, MIN_SOL_FEE_FLOOR))
    if args.usd > spendable:
        sys.exit("REFUSED: $%.2f exceeds spendable $%.2f" % (args.usd, spendable))

    quote = relay_quote(sol_pubkey, base_address, args.usd)
    echoed = ((quote.get("details") or {}).get("recipient")
              or (quote.get("request") or {}).get("recipient") or base_address)
    if str(echoed).lower() != base_address.lower():
        sys.exit("REFUSED: quote recipient %s is not our Base address" % echoed)
    print(describe(quote))

    raw_instructions, table_addresses, request_id = relay_instructions(quote)
    print("  %d instruction(s), %d lookup table(s)" % (len(raw_instructions), len(table_addresses)))
    if request_id:
        print("  status: https://api.relay.link%s" % request_id)

    if not args.confirm:
        print("\nDRY RUN — nothing signed, nothing sent. Re-run with --confirm to move real money.")
        return

    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction

    instructions = [build_instruction(raw, sol_pubkey) for raw in raw_instructions]
    tables = fetch_lookup_tables(table_addresses, rpc_url)

    # FINALIZED, not confirmed. rpc_send keeps preflight on, and that preflight simulates at the
    # default commitment (finalized), where a confirmed-commitment blockhash does not yet exist —
    # isBlockhashValid returns False for it and the send bounces with BlockhashNotFound even though
    # the transaction is perfectly valid. A finalized blockhash is valid at BOTH commitments; it
    # costs ~31 slots of the validity window, which is irrelevant for a one-shot transfer.
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash",
                       "params": [{"commitment": "finalized"}]})
    req = urllib.request.Request(rpc_url, data=body.encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        blockhash = json.loads(r.read().decode())["result"]["value"]["blockhash"]

    message = MessageV0.try_compile(Pubkey.from_string(sol_pubkey), instructions, tables,
                                    Hash.from_string(blockhash))
    signed = VersionedTransaction(message, [keypair])
    signed_b64 = base64.b64encode(bytes(signed)).decode()
    signature = str(signed.signatures[0])
    print("\nsigned %s" % signature)

    # Try each pooled endpoint with the IDENTICAL signed bytes. Solana deduplicates by signature, so
    # the same transaction offered twice can land at most once — this is a retry, never a re-spend.
    # Re-signing here instead would produce a second signature and would NOT be safe.
    sent = None
    for candidate in rpc_urls:
        sent = rpc_send(signed_b64, candidate)
        if sent:
            break
        # rpc_send collapses every failure to None. Re-ask once and print the RPC's own words, so a
        # rejection names itself instead of forcing the operator to guess which layer refused.
        try:
            probe = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                                "params": [signed_b64, {"encoding": "base64",
                                                        "skipPreflight": False}]})
            request = urllib.request.Request(candidate, data=probe.encode(),
                                             headers={"content-type": "application/json"})
            with urllib.request.urlopen(request, timeout=25) as response:
                detail = response.read().decode()[:400]
        except Exception as exc:
            detail = "%s: %s" % (type(exc).__name__, str(exc)[:300])
        print("  rejected by %s\n    %s" % (candidate.split("?")[0], detail))
    if not sent:
        sys.exit("SEND FAILED: signature %s was signed but no endpoint accepted it. Check it on "
                 "chain before re-running — a NEW run signs a NEW transaction, which can double "
                 "spend if this one actually landed." % signature)
    state = rpc_confirm(sent, rpc_url)
    print("broadcast %s -> %s" % (sent, state))
    if state != "confirmed":
        sys.exit("NOT CONFIRMED (%s). Check the signature on chain before retrying." % state)
    print("Solana leg confirmed. Base credit follows the Relay status endpoint above.")


if __name__ == "__main__":
    main()
