#!/usr/bin/env python3
"""Move SOL/USDC from the funded Solana wallet to native ETH (GAS) on Robinhood 4663, via Relay.

The mirror of scripts/memecoin_bridge_robinhood_solana.py with the direction reversed. Relay
returns INSTRUCTIONS for a Solana origin rather than a serialized transaction, so the v0 message is
compiled here from Relay's own instructions and lookup tables and signed with our Solana keypair.
Broadcast and confirmation reuse the live executor's seams (`rpc_send` / `rpc_confirm`).

WHY THIS EXISTS (2026-09-02). A node rejects a transaction when `gasLimit * gasPrice` exceeds the
sender's balance, whatever the swap actually consumes. The funded 4663 wallet held 0.00051674 ETH
against a sell needing 900,000 * 0.6628 gwei = 0.00059648 ETH, so EVERY exit was signed, broadcast
and refused before the mempool: no receipt, no revert, nothing on chain. Three funded bags stuck in
one evening, each freed by hand. The existing bridge only runs 4663 -> Solana, so there was no way
to refill gas from the Solana bankroll without leaving the system.

DESTINATION IS NATIVE ETH, NOT WETH. WETH cannot pay gas. `destinationCurrency` is the zero address
on purpose, and the script refuses if Relay echoes anything else.

SAFETY MODEL (identical to its sibling)
  - DRY-RUN by default; --confirm required to move real money.
  - Recipient is our own EVM address, derived from our own keypair, never an argument.
  - The quote's echoed recipient is compared against that address before anything is signed.
  - One asset per run. --amount all sweeps, keeping a SOL reserve for Solana's own fees.
  - Every step must target Solana; a step on any other chain refuses the whole run.

    python3 scripts/memecoin_bridge_solana_robinhood.py --token usdc --amount 25 --dry-run
    python3 scripts/memecoin_bridge_solana_robinhood.py --token usdc --amount 25 --confirm
    python3 scripts/memecoin_bridge_solana_robinhood.py --token sol  --amount 0.1 --confirm
"""
import argparse
import base64
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.util.envfiles import load_env_files

ROBINHOOD_CHAIN = 4663
SOLANA_RELAY_CHAIN = 792703809
NATIVE = "0x0000000000000000000000000000000000000000"          # native ETH on 4663 == GAS
SOLANA_NATIVE_SOL = "11111111111111111111111111111111"
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RELAY_QUOTE_URL = "https://api.relay.link/quote"
# Solana keeps its own fee + rent headroom; a sweep must never leave the bankroll unable to sign.
SOL_RESERVE = 0.02


def _http_post_json(url, body, timeout=40):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def relay_quote(sol_pubkey, evm_addr, amount_atomic, origin_currency, *, destination_chain=4663):
    if int(destination_chain) not in {1, 4663}:
        raise ValueError("destination chain must be Ethereum or Robinhood")
    body = {
        "user": sol_pubkey,
        "recipient": evm_addr,
        "originChainId": SOLANA_RELAY_CHAIN, "destinationChainId": int(destination_chain),
        "originCurrency": origin_currency, "destinationCurrency": NATIVE,
        "amount": str(amount_atomic), "tradeType": "EXACT_INPUT",
    }
    return _http_post_json(RELAY_QUOTE_URL, body)


def _decode_ix_data(value):
    """Instruction data as bytes. Relay sends HEX for Solana steps.

    DECODED AS BASE64 FIRST AND IT SILENTLY WORKED. A hex string is made of characters that are
    also valid base64, so `b64decode` returned 72 bytes of garbage for 48 bytes of real data and
    the program answered `InstructionFallbackNotFound` — a wrong answer that looked like a working
    decode. Hex is tried FIRST and the result is only accepted if it round-trips, so a future
    format change fails loudly here instead of producing a transaction that does something else.
    """
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, (list, tuple)):
        return bytes(value)
    if not isinstance(value, str):
        raise RuntimeError(f"instruction data has unexpected type {type(value).__name__}")
    raw = value[2:] if value.startswith("0x") else value
    try:
        decoded = bytes.fromhex(raw)
    except ValueError:
        pass
    else:
        if decoded.hex() == raw.lower():
            return decoded
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:
        raise RuntimeError(f"instruction data is neither hex nor base64: {value[:40]!r}")
    return decoded


def _fetch_lookup_tables(addresses, rpc_url):
    """Resolve each address-lookup-table account so a v0 message can compile against it.

    A missing or unparseable table is a HARD FAILURE. Compiling without it silently produces a
    different account list than Relay intended, which is a transaction that does something other
    than the quote we showed the operator.
    """
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.pubkey import Pubkey
    from sniper.execution.solana_wallet import _rpc
    tables = []
    for addr in addresses or ():
        resp = _rpc(rpc_url, "getAccountInfo", [addr, {"encoding": "base64"}])
        value = ((resp or {}).get("result") or {}).get("value")
        if not value:
            raise RuntimeError(f"address lookup table {addr} did not resolve")
        raw = base64.b64decode(value["data"][0])
        # Layout: 56-byte header, then a packed array of 32-byte pubkeys.
        body = raw[56:]
        if len(body) % 32:
            raise RuntimeError(f"address lookup table {addr} has a malformed body")
        keys = [Pubkey(body[i:i + 32]) for i in range(0, len(body), 32)]
        tables.append(AddressLookupTableAccount(key=Pubkey.from_string(addr), addresses=keys))
    return tables


def _steps_as_solana_txs(quote, payer, rpc_url):
    """Compile each Relay step into a signable v0 transaction, in order.

    Relay returns INSTRUCTIONS for a Solana origin, not a serialized transaction, so the message is
    compiled here. Every account, program id and data blob comes from Relay verbatim — nothing about
    the transfer is constructed locally. A step on any chain other than Solana, or one carrying no
    instructions, refuses the whole run: a skipped step is a bridge that takes funds and never
    delivers.
    """
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from sniper.execution.solana_wallet import _rpc

    blockhash_resp = _rpc(rpc_url, "getLatestBlockhash", [{"commitment": "finalized"}])
    blockhash = (((blockhash_resp or {}).get("result") or {}).get("value") or {}).get("blockhash")
    if not blockhash:
        raise RuntimeError("could not read a recent blockhash")
    from solders.hash import Hash
    recent = Hash.from_string(blockhash)

    out = []
    for step in quote.get("steps", []):
        for item in step.get("items", []):
            data = item.get("data") or {}
            chain = data.get("chainId")
            if chain not in (SOLANA_RELAY_CHAIN, str(SOLANA_RELAY_CHAIN), None):
                raise RuntimeError(f"unexpected step chainId {chain} (want Solana)")
            raw_ixs = data.get("instructions") or []
            if not raw_ixs:
                raise RuntimeError(
                    f"step {step.get('id')!r} carries no instructions: {str(data)[:200]}")
            ixs = []
            for ix in raw_ixs:
                metas = [AccountMeta(pubkey=Pubkey.from_string(k["pubkey"]),
                                     is_signer=bool(k.get("isSigner")),
                                     is_writable=bool(k.get("isWritable")))
                         for k in (ix.get("keys") or [])]
                ixs.append(Instruction(
                    program_id=Pubkey.from_string(ix["programId"]),
                    accounts=metas,
                    data=_decode_ix_data(ix.get("data")),
                ))
            tables = _fetch_lookup_tables(data.get("addressLookupTableAddresses"), rpc_url)
            msg = MessageV0.try_compile(payer, ixs, tables, recent)
            out.append({"label": step.get("id") or "step", "message": msg,
                        "n_ix": len(ixs), "n_alt": len(tables)})
    return out


def _fmt(quote):
    det = quote.get("details", {})
    ci, co = det.get("currencyIn", {}), det.get("currencyOut", {})
    return (f"  IN : {ci.get('amountFormatted')} {ci.get('currency', {}).get('symbol')} on Solana\n"
            f"  OUT: {co.get('amountFormatted')} {co.get('currency', {}).get('symbol')} on chain {co.get('currency', {}).get('chainId')}\n"
            f"  rate={det.get('rate')}  impact={det.get('totalImpact', {}).get('percent')}%  "
            f"~{det.get('timeEstimate')}s")


def main():
    ap = argparse.ArgumentParser(
        description="Bridge Solana SOL/USDC -> native ETH (gas) on Robinhood 4663 via Relay.")
    ap.add_argument("--token", choices=("sol", "usdc"), required=True)
    ap.add_argument("--amount", required=True, help="amount in token units, or 'all'")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True, help="inspect only (default)")
    g.add_argument("--confirm", action="store_true", help="SIGN + broadcast real money")
    ap.add_argument("--state-root", default="/home/bigpapi/Desktop/Sniper")
    ap.add_argument("--destination-chain", type=int, choices=(4663, 1), default=4663)
    args = ap.parse_args()
    destination = args.destination_chain

    from pathlib import Path
    root = Path(args.state_root).resolve()
    configured_root = os.environ.get("SNIPER_STATE_ROOT")
    if configured_root and Path(configured_root).resolve() != root:
        sys.exit("REFUSE: --state-root conflicts with SNIPER_STATE_ROOT")
    os.environ["SNIPER_STATE_ROOT"] = str(root)
    load_env_files(str(root))
    db_path = Path(os.getenv("DB_PATH", "sniper_tracker.db"))
    if not db_path.is_absolute():
        db_path = root / db_path
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import load_keypair as evm_load, native_balance
    from sniper.execution.solana_wallet import (load_keypair as sol_load, native_sol_balance_value,
                                                pubkey_str, sol_balance_read,
                                                token_balance_checked)
    from sniper.execution.solana_real_seams import rpc_send, rpc_confirm

    kp = sol_load(cfg.MEMECOIN_SOLANA_KEYPAIR)
    pk = pubkey_str(kp)
    evm_addr = evm_load(cfg.MEMECOIN_EVM_KEYPAIR).address
    sol_rpc = cfg.MEMECOIN_SOLANA_RPC_URL
    evm_rpc = cfg.evm_rpc_for_chain(str(destination))

    # A BALANCE THIS SCRIPT CANNOT READ IS A REASON TO STOP, NEVER A ZERO. Same rule as
    # memecoin_swap_sol_usdc: a rate-limited read once printed 0.000000 on a wallet holding
    # 0.69 SOL, and a partially-failed read could produce a POSITIVE but WRONG sweep amount.
    read = sol_balance_read(pk, sol_rpc)
    if not getattr(read, "ok", False):
        sys.exit(f"REFUSE: could not read the SOL balance ({getattr(read, 'failure', '?')}) — "
                 "stopping rather than assuming zero")
    sol_bal = float(native_sol_balance_value(read))
    # `token_balance_checked` returns a float, or None when the read FAILED — the whole point of
    # the "checked" variant is that it never collapses a failure to 0.0. None must stop the run.
    usdc = token_balance_checked(pk, SOLANA_USDC, sol_rpc)
    usdc_bal = None if usdc is None else float(usdc)
    if args.token == "usdc" and usdc_bal is None:
        sys.exit("REFUSE: could not read the USDC balance — stopping rather than assuming zero")

    gas_now = native_balance(evm_addr, evm_rpc)
    print(f"wallet {pk} -> {evm_addr}")
    print(f"  Solana:          SOL {sol_bal:.6f} | USDC {usdc_bal if usdc_bal is None else f'{usdc_bal:.2f}'}")
    print(f"  Chain {destination}: native ETH {gas_now:.8f}   <- this is the gas being topped up")

    if args.token == "sol":
        bal, decimals, origin_currency = sol_bal, 9, SOLANA_NATIVE_SOL
        amount = (bal - SOL_RESERVE) if args.amount == "all" else float(args.amount)
        if args.amount == "all":
            print(f"  (sweep keeps a {SOL_RESERVE} SOL reserve for Solana fees)")
    else:
        bal, decimals, origin_currency = usdc_bal, 6, SOLANA_USDC
        amount = bal if args.amount == "all" else float(args.amount)
        if sol_bal < 0.002:
            sys.exit(f"REFUSE: SOL {sol_bal:.6f} is too low to pay the Solana fee for this bridge")

    print(f"  plan: bridge {amount:.6f} {args.token.upper()} (Solana) -> native ETH ({destination})\n")
    if amount <= 0:
        sys.exit("REFUSE: amount must be positive")
    if amount > bal + 1e-12:
        sys.exit(f"REFUSE: {amount:.6f} exceeds the {args.token.upper()} balance {bal:.6f}")

    amount_atomic = int(round(amount * (10 ** decimals)))
    quote = relay_quote(pk, evm_addr, amount_atomic, origin_currency, destination_chain=destination)
    if quote.get("message") or quote.get("errCode"):
        sys.exit(f"REFUSE: Relay error: {str(quote)[:300]}")

    det = quote.get("details") or {}
    echoed = det.get("recipient") or ""
    if echoed and echoed.lower() != evm_addr.lower():
        sys.exit(f"REFUSE: quote recipient {echoed!r} is not our EVM wallet {evm_addr}")
    if destination == 1 and echoed.lower() != evm_addr.lower():
        sys.exit("REFUSE: Ethereum funding quote must name our recipient")
    # WETH CANNOT PAY GAS. If Relay resolved the destination to anything but the native asset the
    # bridge would "succeed" and leave the wallet exactly as unable to broadcast as before.
    out_currency = ((det.get("currencyOut") or {}).get("currency") or {})
    out_addr = str(out_currency.get("address") or "")
    if destination == 1 and (out_addr.lower() != NATIVE or int(out_currency.get("chainId") or 0) != 1):
        sys.exit("REFUSE: quote must deliver native ETH on chain 1")
    if out_addr and out_addr.lower() not in (NATIVE, "0x0", ""):
        sys.exit(f"REFUSE: destination resolved to {out_addr} ({out_currency.get('symbol')}), "
                 f"not native ETH — that cannot pay gas")

    from solders.pubkey import Pubkey as _Pk
    txs = _steps_as_solana_txs(quote, _Pk.from_string(pk), sol_rpc)
    if not txs:
        sys.exit(f"REFUSE: quote returned no executable steps: {str(quote)[:300]}")

    print("Relay quote:")
    print(_fmt(quote))
    print(f"\n{len(txs)} Solana transaction(s) — every instruction is from Relay, not built here:")
    for i, t in enumerate(txs, 1):
        print(f"  [{i}] {t['label']:14s} {t['n_ix']} instruction(s), {t['n_alt']} lookup table(s)")

    if not args.confirm:
        print("\nDRY-RUN — nothing signed. Re-run with --confirm to broadcast real money.")
        return

    print("\n--confirm: broadcasting real money.")
    from solders.transaction import VersionedTransaction
    from contextlib import nullcontext
    from pathlib import Path
    from sniper.execution.exec_lock import wallet_exec_lock
    lock = wallet_exec_lock("sol-%s" % pk, lock_dir=str(root / "data"), blocking=False) if destination == 1 else nullcontext(True)
    with lock as held:
        if not held:
            sys.exit("REFUSE: source wallet is busy; no transfer was signed")
        if destination == 1:
            from sniper.persistence.db_core import get_readonly_connection
            from sniper.persistence import db_memecoin as m
            check = get_readonly_connection(str(db_path))
            try:
                if m.pending_totals(check, network="solana")[0]:
                    sys.exit("REFUSE: Solana has unresolved funded reservations")
            finally:
                check.close()
            fresh = token_balance_checked(pk, SOLANA_USDC, sol_rpc)
            if args.token != "usdc" or fresh is None or float(fresh) < amount:
                sys.exit("REFUSE: Ethereum funding requires a known sufficient USDC balance")
            if len(txs) != 1:
                sys.exit("REFUSE: Ethereum funding requires one atomic source transaction")
        journal = root / "output" / "ethereum-funding-relay.json"
        if destination == 1 and journal.exists():
            sys.exit("REFUSE: funding journal exists; reconcile its signature before another transfer")
        for i, t in enumerate(txs, 1):
            try:
                vtx = VersionedTransaction(t["message"], [kp])
                signed_b64 = base64.b64encode(bytes(vtx)).decode()
                sig = str(vtx.signatures[0])
            except Exception as exc:
                sys.exit(f"REFUSE: signing failed at step {i} ({t['label']}): {exc}")
            if destination == 1:
                journal = Path(args.state_root) / "output" / "ethereum-funding-relay.json"
                journal.parent.mkdir(parents=True, exist_ok=True)
                with journal.open("x") as output:
                    os.chmod(journal, 0o600)
                    json.dump({"source": pk, "recipient": evm_addr, "destination_chain": 1,
                               "amount_atomic": amount_atomic, "signature": sig,
                               "signed_b64": signed_b64, "quote_details": det}, output)
                    output.flush()
                    os.fsync(output.fileno())
            print(f"  [{i}] {t['label']}: sending {sig}…")
            sent = rpc_send(signed_b64, sol_rpc)
            if not sent:
                sys.exit(f"STOP: step {i} ({t['label']}) was not accepted — nothing further submitted")
            state = rpc_confirm(sent, sol_rpc)
            print(f"      {sent} -> {state}")
            if state != "confirmed":
                # NEVER continue past an unconfirmed step. A later step that assumes an earlier one
                # landed can move funds into a state neither side accounts for.
                sys.exit(f"STOP: step {i} is {state}, not confirmed — resolve on chain before retrying")

    print("\nSolana side confirmed. Relay fills the destination side asynchronously "
          f"(~{(quote.get('details') or {}).get('timeEstimate')}s).")
    print(f"Watch it arrive:  native ETH at {evm_addr} on {destination} (was {gas_now:.8f})")
    print("The funded runner retries stuck exits every few seconds — a stuck bag sells itself "
          "once the gas lands. No further action needed.")


if __name__ == "__main__":
    main()
