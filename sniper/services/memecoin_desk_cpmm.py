"""Pure confirmed CPMM research facts. No execution imports or provider calls.

The 170-byte event layout is pinned by the repo's frozen CPMM census. Event reserves
are effective PRE state; protocol/fund accrual is not in the event, so POST stays unknown.
"""
from __future__ import annotations

import base64
import hashlib
import math
import re
import struct

from solders.pubkey import Pubkey

PROGRAM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
WSOL = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}
SWAP_INPUT = bytes.fromhex("8fbe5adac41e33de")
SWAP_OUTPUT = hashlib.sha256(b"global:swap_base_output").digest()[:8]
EVENT_DISC = bytes.fromhex("40c6cde8260871e2")
POOL_DISC = bytes.fromhex("f7ede3f5d7c3de46")
VERSION = "desk-cpmm/v1"
_INVOKE = re.compile(r"^Program (\S+) invoke \[(\d+)\]$")
_DONE = re.compile(r"^Program (\S+) (success|failed.*)$")


class DecodeLoss(ValueError):
    """A fact cannot establish complete, pool-bound research evidence."""


def key(raw):
    return str(Pubkey.from_bytes(bytes(raw)))


def _raw(account):
    if not isinstance(account, dict) or account.get("executable") is not False:
        raise DecodeLoss("account_unknown")
    data = account.get("data")
    if not isinstance(data, list) or len(data) != 2 or data[1] != "base64":
        raise DecodeLoss("account_encoding")
    try:
        return base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError) as exc:
        raise DecodeLoss("account_encoding") from exc


def pool_binding(pool, accounts, known_at, context_slot):
    """Bind verified program/pool/mints/decimals; only extension-free SPL mints initially."""
    if type(known_at) not in (int, float) or not math.isfinite(known_at) or known_at < 0:
        raise DecodeLoss("binding_clock_unknown")
    if type(context_slot) is not int or context_slot <= 0:
        raise DecodeLoss("binding_slot_unknown")
    account = accounts.get(pool)
    raw = _raw(account)
    if account.get("owner") != PROGRAM or len(raw) != 637 or raw[:8] != POOL_DISC:
        raise DecodeLoss("pool_identity")
    mints = [key(raw[o:o + 32]) for o in (168, 200)]
    programs = [key(raw[o:o + 32]) for o in (232, 264)]
    vaults = [key(raw[o:o + 32]) for o in (72, 104)]
    decimals = list(raw[331:333])
    if mints.count(WSOL) != 1 or len(set(mints)) != 2 or len(set(vaults)) != 2:
        raise DecodeLoss("pool_quote_or_mint")
    for mint, program, decimal in zip(mints, programs, decimals):
        meta = accounts.get(mint)
        mint_raw = _raw(meta)
        if program not in TOKEN_PROGRAMS or meta.get("owner") != program:
            raise DecodeLoss("mint_owner")
        if len(mint_raw) != 82:
            raise DecodeLoss("token_extensions_unsupported")
        if mint_raw[45] != 1 or mint_raw[44] != decimal or decimal > 18 or (mint == WSOL and decimal != 9):
            raise DecodeLoss("mint_decimals")
    base = 1 - mints.index(WSOL)
    return {"pool": pool, "mint": mints[base], "quote_mint": WSOL, "mints": mints,
            "vaults": vaults, "programs": programs, "decimals": decimals,
            "base_decimals": decimals[base], "quote_decimals": 9, "known_at": float(known_at),
            "context_slot": context_slot, "pool_sha256": hashlib.sha256(raw).hexdigest(),
            "mint_sha256": {m: hashlib.sha256(_raw(accounts[m])).hexdigest() for m in mints},
            "source": "research_rpc:confirmed_pool_and_mints", "version": VERSION}


def parse_event(raw):
    if len(raw) != 170 or raw[:8] != EVENT_DISC or raw[88] not in (0, 1) or raw[169] not in (0, 1):
        raise DecodeLoss("event_shape")
    names = ("input_vault_before", "output_vault_before", "input_amount", "output_amount", "input_transfer_fee", "output_transfer_fee")
    out = dict(zip(names, struct.unpack_from("<6Q", raw, 40)))
    trade, creator = struct.unpack_from("<2Q", raw, 153)
    out.update(pool=key(raw[8:40]), input_mint=key(raw[89:121]), output_mint=key(raw[121:153]),
               base_input=bool(raw[88]), trade_fee=trade, creator_fee=creator, creator_fee_on_input=bool(raw[169]))
    if (not all(out[k] > 0 for k in names[:4]) or out["output_transfer_fee"] > out["output_amount"]
            or trade + (creator if out["creator_fee_on_input"] else 0) >= out["input_amount"]):
        raise DecodeLoss("event_amount")
    return out


def _invocations(frame):
    keys = frame["account_keys"]
    inner = {}
    for group in frame["inner_instructions"]:
        if type(group["index"]) is not int or not 0 <= group["index"] < len(frame["instructions"]) or group["index"] in inner:
            raise DecodeLoss("instruction_order")
        inner[group["index"]] = group["instructions"]
    result = []
    for outer, ix in enumerate(frame["instructions"]):
        candidates = [(None, ix)] + list(enumerate(inner.get(outer, [])))
        for inner_ix, instruction in candidates:
            if any(type(i) is not int or not 0 <= i < len(keys)
                   for i in [instruction["program_id_index"], *instruction["accounts"]]):
                raise DecodeLoss("instruction_index")
            if keys[instruction["program_id_index"]] == PROGRAM:
                result.append((outer, inner_ix, instruction))
    return result


def _logs(logs):
    stack, calls = [], []
    for ordinal, line in enumerate(logs):
        if not isinstance(line, str) or "log truncated" in line.lower():
            raise DecodeLoss("incomplete_logs")
        invoke, done = _INVOKE.match(line), _DONE.match(line)
        if invoke:
            if int(invoke[2]) != len(stack) + 1:
                raise DecodeLoss("incomplete_logs")
            index = len(calls) if invoke[1] == PROGRAM else None
            if index is not None:
                calls.append([])
            stack.append((invoke[1], index))
        elif done:
            if not stack or stack.pop()[0] != done[1] or done[2] != "success":
                raise DecodeLoss("incomplete_logs")
        elif line.startswith("Program data: ") and stack and stack[-1][0] == PROGRAM:
            try:
                raw = base64.b64decode(line[14:], validate=True)
            except ValueError as exc:
                raise DecodeLoss("event_encoding") from exc
            calls[stack[-1][1]].append((ordinal, raw))
    if stack:
        raise DecodeLoss("incomplete_logs")
    return calls


def _actor(frame, indices, event, binding):
    keys = frame["account_keys"]
    authority = keys[indices[0]]
    if authority not in keys[:frame["signer_count"]]:
        return None
    for index, mint in ((indices[4], event["input_mint"]), (indices[5], event["output_mint"])):
        rows = [r for r in frame["pre_token_balances"] + frame["post_token_balances"] if r["account_index"] == index]
        if not rows or any(r.get("owner") != authority or r.get("mint") != mint
                           or r.get("program_id") != binding["programs"][binding["mints"].index(mint)]
                           or r.get("decimals") != binding["decimals"][binding["mints"].index(mint)] for r in rows):
            return None
    return authority


def decode_transaction(frame, bindings):
    """Return raw swap facts and typed gaps; never turn missing proof into a clean window."""
    result = {"rows": [], "gaps": [], "unresolved_pools": [], "status": "ok", "reason": None}
    pool = None
    def gap(cause, pool=None):
        result["gaps"].append({"cause": cause, "pool": pool})
        result["status"] = "partial"
    try:
        if (type(frame.get("slot")) is not int or frame["slot"] <= 0
                or not isinstance(frame.get("signature"), str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{64,88}", frame["signature"])
                or type(frame.get("received_at")) not in (int, float) or not math.isfinite(frame["received_at"]) or frame["received_at"] < 0
                or type(frame.get("signer_count")) is not int or not 0 <= frame["signer_count"] <= len(frame["account_keys"])):
            raise DecodeLoss("frame_identity_or_clock")
        observed, offset = frame.get("observed_at"), frame.get("clock_offset_s")
        if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (observed, offset))
                or abs(frame["received_at"] - offset - observed) > 0.001):
            observed = offset = None
        if frame.get("commitment") != "confirmed":
            raise DecodeLoss("not_confirmed")
        if "err" not in frame:
            raise DecodeLoss("success_metadata_unknown")
        if frame.get("err") is not None:
            result.update(status="rejected", reason="failed_transaction")
            return result
        calls, logs = _invocations(frame), _logs(frame["logs"])
        if len(calls) != len(logs):
            raise DecodeLoss("invocation_mismatch")
        keys = frame["account_keys"]
        for (outer, inner, ix), events in zip(calls, logs):
            data = base64.b64decode(ix["data_b64"], validate=True)
            indices = ix["accounts"]
            pool = None
            if len(data) != 24 or data[:8] not in (SWAP_INPUT, SWAP_OUTPUT):
                gap("pool_state_change")
                continue
            if len(indices) != 13 or len(events) != 1:
                raise DecodeLoss("swap_instruction_shape")
            pool = keys[indices[3]]
            ordinal, raw = events[0]
            event = parse_event(raw)
            if (pool != event["pool"] or keys[indices[10]] != event["input_mint"]
                    or keys[indices[11]] != event["output_mint"]):
                raise DecodeLoss("event_instruction_mismatch")
            amount, limit = struct.unpack_from("<2Q", data, 8)
            gross_in = event["input_amount"] + event["input_transfer_fee"]
            net_out = event["output_amount"] - event["output_transfer_fee"]
            if ((data[:8] == SWAP_INPUT and (amount != gross_in or limit > net_out))
                    or (data[:8] == SWAP_OUTPUT and (limit != net_out or amount < gross_in))):
                raise DecodeLoss("event_instruction_amount")
            if (event["input_mint"] == WSOL) == (event["output_mint"] == WSOL):
                gap("quote_unsupported", pool)
                continue
            buy = event["input_mint"] == WSOL
            binding = bindings.get(pool)
            bound = (isinstance(binding, dict) and binding.get("known_at", math.inf) <= frame["received_at"]
                     and binding.get("context_slot", math.inf) <= frame["slot"])
            actor, price, decimals = None, None, None
            if bound:
                mint_index = binding["mints"].index(event["input_mint"])
                expected = (binding["vaults"][mint_index], binding["vaults"][1 - mint_index],
                            binding["programs"][mint_index], binding["programs"][1 - mint_index])
                if tuple(keys[indices[i]] for i in (6, 7, 8, 9)) != expected or binding["pool"] != pool:
                    raise DecodeLoss("pool_account_mismatch")
                decimals = binding["base_decimals"]
                actor = _actor(frame, indices, event, binding)
            else:
                gap("pool_unbound", pool)
                result["unresolved_pools"].append(pool)
            pre_quote, pre_base = ((event["input_vault_before"], event["output_vault_before"]) if buy else
                                   (event["output_vault_before"], event["input_vault_before"]))
            if bound:
                price = pre_quote / pre_base * 10**decimals / 10**9
                if actor is None:
                    gap("actor_unknown", pool)
            tx_index = frame.get("tx_index")
            known_order = type(tx_index) is int and tx_index >= 0
            if not known_order:
                gap("order_unknown", pool)
            identity = f"{frame['signature']}:{outer}:{inner}:{ordinal}"
            result["rows"].append({"version": VERSION, "event_id": identity, "signature": frame["signature"],
                "slot": frame["slot"], "tx_index": tx_index if known_order else None,
                "outer_ix": outer, "inner_ix": inner, "ordinal": ordinal, "pool": pool,
                "mint": event["output_mint"] if buy else event["input_mint"], "quote_mint": WSOL,
                "is_buy": buy, "actor": actor, "actor_status": "proven" if actor else "unknown",
                "binding_status": "proven" if bound else "unknown", "order_status": "proven" if known_order else "unknown",
                "base_decimals": decimals, "quote_decimals": 9, "received_at": frame["received_at"],
                "observed_at": observed, "clock_offset_s": offset,
                "block_time": frame.get("block_time"), "reserve_basis": "event_effective_pre",
                "pre_quote_atomic": str(pre_quote), "pre_base_atomic": str(pre_base),
                "post_quote_atomic": None, "post_base_atomic": None, "post_price_sol": None,
                "price_before_sol": price, "quote_atomic": str(event["input_amount"] if buy else event["output_amount"]),
                "token_atomic": str(net_out if buy else gross_in),
                "event": {k: str(v) if type(v) is int else v for k, v in event.items()},
                "event_b64": base64.b64encode(raw).decode(), "binding_known_at": binding["known_at"] if bound else None,
                "instruction_accounts": [keys[i] for i in indices]})
    except (DecodeLoss, KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
        result["rows"] = []
        result.update(status="unknown", reason=str(exc) if isinstance(exc, DecodeLoss) else "malformed_frame")
        gap(result["reason"])
        result["status"] = "unknown"
    return result
