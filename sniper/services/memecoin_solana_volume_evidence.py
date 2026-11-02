"""Exact event-time volume proof from transactions the pull tape already fetched.

Turnover sums each event's pool quote movement, including round trips: curve sol_amount,
PumpSwap quote_amount_in_with_lp_fee / quote_amount_out_without_lp_fee. External fees are excluded.
Net token balances bind identity only; they never supply volume. Any uncertain pass restarts
the full 300-second warmup. Unsupported venues and incomplete logs remain unknown.
"""
from __future__ import annotations

import base64
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sniper.execution import pump_curve_buy as pump
from sniper.execution import pumpswap_buy as amm
from sniper.execution import pumpswap_sell as sell
from sniper.persistence import db_sol_volume as db
from sniper.services.memecoin_solana_amounts import NATIVE_SOL

_INVOKE = re.compile(r"Program (\S+) invoke \[(\d+)\]$")
_DONE = re.compile(r"Program (\S+) success$")
_SUPPORTED = {pump.PUMP_PROGRAM, amm.AMM_PROGRAM}
_DISCRIMINATORS = {pump.TRADE_EVENT_DISCRIMINATOR, amm.BUY_EVENT_DISCRIMINATOR,
                   sell.SELL_EVENT_DISCRIMINATOR}
_CLEAN = {"ok", "warming_up"}


@dataclass(frozen=True)
class _Pass:
    vault: str
    mint: str
    now: float
    binding: tuple
    prior: dict
    pass_id: str
    reset: bool


def _metadata(conn, mint, vault):
    row = conn.execute("SELECT mint,coin_vault,quote_vault,quote_mint,program "
                       "FROM sol_pool_vaults WHERE pool=?", (vault,)).fetchone()
    if not row or row[0] != mint or row[1] != vault:
        raise ValueError("missing_pool_identity")
    return tuple(row)


def _identity(binding):
    mint, vault, quote_vault, quote_mint, owner = binding
    # Derivation binds the native curve and both AMM vaults without another account read.
    if quote_mint == NATIVE_SOL:
        curve = str(pump._pda([b"bonding-curve", bytes(pump._pk(mint))], pump.PUMP_PROGRAM))
        if quote_vault != curve or owner != curve:
            raise ValueError("wrong_curve")
        if vault not in {str(pump.ata(curve, mint, tp))
                         for tp in (pump.TOKEN_PROGRAM, pump.TOKEN_2022_PROGRAM)}:
            raise ValueError("wrong_curve_vault")
        return "pump_curve", mint, NATIVE_SOL
    if quote_mint not in {pump.WSOL_MINT, pump.USDC_MINT} or not owner:
        raise ValueError("unsupported_quote")
    if vault not in {str(pump.ata(owner, mint, tp))
                     for tp in (pump.TOKEN_PROGRAM, pump.TOKEN_2022_PROGRAM)}:
        raise ValueError("wrong_amm_base_vault")
    if quote_vault not in {str(pump.ata(owner, quote_mint, tp))
                           for tp in (pump.TOKEN_PROGRAM, pump.TOKEN_2022_PROGRAM)}:
        raise ValueError("wrong_amm_quote_vault")
    return "pump_amm", owner, quote_mint


def begin_pass(conn, mint, vault, now_iso, *, reset=False):
    """Invalidate before enumeration; only finish_pass can restore eligibility."""
    db.ensure_tables(conn)
    cursor = conn.execute("SELECT * FROM sol_volume_coverage_v1 WHERE pool=?", (vault,))
    row = cursor.fetchone()
    prior = dict(zip((c[0] for c in cursor.description), row)) if row else {}
    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    if now.tzinfo is None:
        raise ValueError("naive_poll_time")
    pass_id = uuid.uuid4().hex
    # Metadata validation happens after the durable invalidation, even for a damaged binding.
    conn.execute("INSERT INTO sol_volume_coverage_v1 "
                 "(pool,mint,venue,market_pool,quote_mint,complete,reason,pass_id) "
                 "VALUES(?,?,'','','',0,'in_progress',?) ON CONFLICT(pool) DO UPDATE SET "
                 "complete=0,reason='in_progress',pass_id=excluded.pass_id",
                 (vault, mint, pass_id))
    conn.commit()
    binding = _metadata(conn, mint, vault)
    return _Pass(vault, mint, now.timestamp(), binding, prior, pass_id, reset)


def _events(tx):
    logs = (tx.get("meta") or {}).get("logMessages")
    if not isinstance(logs, list) or not logs or len(logs) > 4096:
        raise ValueError("missing_logs")
    stack, result = [], []
    for index, line in enumerate(logs):
        if not isinstance(line, str) or "truncated" in line.lower() or " failed:" in line:
            raise ValueError("incomplete_logs")
        invoke = _INVOKE.fullmatch(line)
        if invoke:
            pid, depth = invoke.group(1), int(invoke.group(2))
            if depth != len(stack) + 1:
                raise ValueError("broken_program_stack")
            stack.append({"pid": pid, "instruction": None, "events": 0})
            continue
        done = _DONE.fullmatch(line)
        if done:
            if not stack or stack[-1]["pid"] != done.group(1):
                raise ValueError("broken_program_stack")
            frame = stack.pop()
            if frame["pid"] in _SUPPORTED and not frame["events"]:
                # Pump's event-authority self CPI has no instruction or separate event.
                helper = (stack and stack[-1]["pid"] == frame["pid"]
                          and stack[-1]["events"] and frame["instruction"] is None)
                if not helper:
                    raise ValueError("unsupported_program_invocation")
            continue
        if line.startswith("Program log: Instruction: ") and stack:
            stack[-1]["instruction"] = line.removeprefix("Program log: Instruction: ")
        if not line.startswith("Program data: "):
            continue
        raw = base64.b64decode(line.removeprefix("Program data: "), validate=True)
        discriminator = raw[:8]
        pid = stack[-1]["pid"] if stack else None
        if discriminator in _DISCRIMINATORS and pid not in _SUPPORTED:
            raise ValueError("wrong_event_emitter")
        if pid not in _SUPPORTED:
            continue
        if pid == pump.PUMP_PROGRAM and discriminator == pump.TRADE_EVENT_DISCRIMINATOR:
            event = pump.parse_trade_event(line)
            kind = "pump_curve"
        elif pid == amm.AMM_PROGRAM and discriminator == amm.BUY_EVENT_DISCRIMINATOR:
            event = amm.parse_buy_event(line)
            kind = "buy"
        elif pid == amm.AMM_PROGRAM and discriminator == sell.SELL_EVENT_DISCRIMINATOR:
            event = sell.parse_sell_event(line.removeprefix("Program data: "))
            if event is None:
                raise ValueError("malformed_sell_event")
            # The existing sell parser exposes the numeric head; the next seven fields are keys.
            from sniper.research.memecoin_opportunity.decoders import BorshReader
            reader = BorshReader(raw[120:])
            event.update({name: reader.pubkey() for name in (
                "pool", "user", "user_base_token_account", "user_quote_token_account",
                "protocol_fee_recipient", "protocol_fee_recipient_token_account", "coin_creator")})
            kind = "sell"
        else:
            raise ValueError("unsupported_event")
        stack[-1]["events"] += 1
        result.append((index, kind, event))
    if stack:
        raise ValueError("unfinished_program_stack")
    return result


def _bound_balances(tx, binding):
    mint, vault, quote_vault, quote_mint, owner = binding
    keys = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys"))
    if not isinstance(keys, list):
        raise ValueError("missing_account_keys")
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
    entries = (tx.get("meta") or {}).get("preTokenBalances")
    after = (tx.get("meta") or {}).get("postTokenBalances")
    if not isinstance(entries, list) or not isinstance(after, list):
        raise ValueError("missing_token_balances")
    expected = {vault: (mint, owner)}
    if quote_mint != NATIVE_SOL:
        expected[quote_vault] = (quote_mint, owner)
    found = set()
    for item in entries + after:
        index = item["accountIndex"]
        if type(index) is not int or not 0 <= index < len(keys):
            raise ValueError("wrong_account_index")
        address = keys[index]
        if address in expected:
            if (item.get("mint"), item.get("owner")) != expected[address]:
                raise ValueError("wrong_vault_binding")
            found.add(address)
    if found != expected.keys():
        raise ValueError("unbound_vault")


def _receipts(token, transactions, identity):
    if len(transactions) > 4096:
        raise ValueError("transaction_bound")
    receipts = []
    for signature, tx in transactions:
        if not isinstance(signature, str) or not signature or not isinstance(tx, dict):
            raise ValueError("missing_transaction")
        meta = tx.get("meta")
        if not isinstance(meta, dict) or "err" not in meta:
            raise ValueError("unknown_transaction_status")
        if meta["err"] is not None:
            continue
        block_ts = tx.get("blockTime")
        if isinstance(block_ts, bool) or not isinstance(block_ts, (float, int)) \
                or not math.isfinite(block_ts) or block_ts <= 0:
            raise ValueError("missing_block_time")
        tx_signatures = (tx.get("transaction") or {}).get("signatures")
        if not tx_signatures or tx_signatures[0] != signature:
            raise ValueError("wrong_signature")
        keys = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys"))
        payer = keys[0] if keys else None
        payer = payer.get("pubkey") if isinstance(payer, dict) else payer
        if not isinstance(payer, str) or not payer:
            raise ValueError("missing_payer")
        _bound_balances(tx, token.binding)
        matched = 0
        for index, kind, event in _events(tx):
            if kind == "pump_curve":
                if event["mint"] != token.mint:
                    continue
                if identity[0] != "pump_curve" or event["quote_mint"] not in pump.SOL_QUOTE_MINTS:
                    raise ValueError("wrong_curve_quote")
                amount = event["sol_amount"]
            else:
                if event["pool"] != identity[1]:
                    continue
                if identity[0] != "pump_amm":
                    raise ValueError("wrong_venue")
                amount = event["quote_amount_in_with_lp_fee"] if kind == "buy" else \
                    event["quote_amount_out_without_lp_fee"]
            if not event.get("user") or type(amount) is not int or amount <= 0:
                raise ValueError("malformed_swap")
            receipts.append((token.vault, signature, index, block_ts, str(amount), event["user"], payer))
            matched += 1
        if not matched:
            raise ValueError("no_bound_swap_event")
    return receipts


def finish_pass(conn, token, *, transactions, complete):
    """No network; a partial, malformed or interrupted pass cannot restore eligibility."""
    try:
        identity = _identity(token.binding)
        if not complete or _metadata(conn, token.mint, token.vault) != token.binding:
            raise ValueError("incomplete_pass")
        rows = _receipts(token, transactions, identity)
        binding = json.dumps(token.binding, separators=(",", ":"))
        prior = token.prior
        same = not token.reset and prior.get("binding") == binding \
            and prior.get("reason") in _CLEAN \
            and prior.get("through", float("inf")) <= token.now
        since = prior["verified_since"] if same else token.now
        # An AMM requires a program-emitted event before a later empty head can extend its proof.
        if identity[0] == "pump_amm" and not rows and not same:
            raise ValueError("unproven_amm_program")
        is_complete = int(token.now - since >= 300)
        reason = "ok" if is_complete else "warming_up"
        current = conn.execute("SELECT pass_id FROM sol_volume_coverage_v1 WHERE pool=?",
                               (token.vault,)).fetchone()
        if not current or current[0] != token.pass_id:
            return
        if not same:
            conn.execute("DELETE FROM sol_volume_swaps_v1 WHERE pool=?", (token.vault,))
        for receipt in rows:
            old = conn.execute("SELECT block_ts,quote_atomic,maker,payer FROM sol_volume_swaps_v1 "
                               "WHERE pool=? AND signature=? AND event_index=?", receipt[:3]).fetchone()
            if old is not None and tuple(old) != receipt[3:]:
                raise ValueError("conflicting_receipt")
            conn.execute("INSERT OR IGNORE INTO sol_volume_swaps_v1 "
                         "(pool,signature,event_index,block_ts,quote_atomic,maker,payer) "
                         "VALUES(?,?,?,?,?,?,?)", receipt)
        conn.execute("DELETE FROM sol_volume_swaps_v1 WHERE pool=? AND block_ts<=?",
                     (token.vault, token.now - 600))
        conn.execute("UPDATE sol_volume_coverage_v1 SET mint=?,venue=?,market_pool=?,quote_mint=?,"
                     "verified_since=?,through=?,complete=?,reason=?,binding=? "
                     "WHERE pool=? AND pass_id=?",
                     (token.mint, *identity, since, token.now, is_complete, reason, binding,
                      token.vault, token.pass_id))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.execute("UPDATE sol_volume_coverage_v1 SET complete=0,verified_since=NULL,reason=? "
                     "WHERE pool=? AND pass_id=?",
                     (str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                      token.vault, token.pass_id))
        conn.commit()
