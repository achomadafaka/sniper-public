"""Pure confirmed-receipt valuation. No RPC, wallet balance debit or inferred SOL price."""
import math

from sniper.execution.pump_curve_buy import parse_trade_event, SOL_QUOTE_MINTS, trade_event_gross_lamports
from sniper.execution.pumpswap_buy import parse_buy_event, AMM_PROGRAM, buy_event_gross_quote
from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes
from sniper.services.memecoin_opportunity_live import program_data_logs
from sniper.services.memecoin_slot_signal import (IMPACT_RATIO_MAX, IMPACT_RATIO_MIN, PUMP_PROGRAM,
                                                  routed_post_facts)

WSOL = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}
SOL_USD_MAX_AGE_S = 30.0


def _account_keys(tx):
    keys = tx["transaction"]["message"]["accountKeys"]
    # jsonParsed already expands lookup keys; raw json keeps loadedAddresses separately.
    result = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    if not any(isinstance(k, dict) and k.get("source") == "lookupTable" for k in keys):
        loaded = tx["meta"].get("loadedAddresses") or {}
        result += loaded.get("writable", []) + loaded.get("readonly", [])
    return result


def source_pre_atomic(tx, owner, mint):
    """Exact pre tokens in receipt-touched owner/mint accounts, or None.

    This is inventory evidence, not signer/payment authority or an owner-wide snapshot. A missing
    pre row is zero only for a receipt-initialized account with zero prior lamports. No I/O.
    """
    import base58
    try:
        def address(value):
            if not isinstance(value, str) or len(base58.b58decode(value)) != 32:
                raise ValueError('source_pre_address')
            return value
        address(owner)
        address(mint)
        meta, keys = tx['meta'], _account_keys(tx)
        if (meta['err'] is not None or not 1 <= len(keys) <= 256
                or len(set(keys)) != len(keys)):
            return None
        for key in keys:
            address(key)
        for side in ('preBalances', 'postBalances'):
            if (not isinstance(meta[side], list) or len(meta[side]) != len(keys)
                    or any(type(amount) is not int or not 0 <= amount < 2**64 for amount in meta[side])):
                return None
        identities, decimals, snapshots = {}, {}, []
        for side in ('preTokenBalances', 'postTokenBalances'):
            rows, amounts = meta[side], {}
            if not isinstance(rows, list):
                return None
            for row in rows:
                index, ui = row['accountIndex'], row['uiTokenAmount']
                amount, places = ui['amount'], ui['decimals']
                identity = (address(row['owner']), address(row['mint']), row['programId'], places)
                if (type(index) is not int or not 0 <= index < len(keys) or index in amounts
                        or identity[2] not in TOKEN_PROGRAMS or type(places) is not int or not 0 <= places <= 18
                        or not isinstance(amount, str) or not amount or len(amount) > 20
                        or not amount.isascii() or not amount.isdigit() or int(amount) >= 2**64
                        or identities.setdefault(index, identity) != identity
                        or decimals.setdefault(identity[1], places) != places):
                    return None
                amounts[index] = int(amount)
            snapshots.append(amounts)
        pre, post = snapshots
        touched = {i for i, identity in identities.items() if identity[:2] == (owner, mint)}
        if not touched:
            return None
        top, groups = tx['transaction']['message']['instructions'], meta['innerInstructions']
        if not isinstance(top, list) or not isinstance(groups, list):
            return None
        instructions = [_route_instruction(ix, keys) for ix in top]
        seen = set()
        for group in groups:
            index = group['index']
            if (type(index) is not int or not 0 <= index < len(top) or index in seen
                    or not isinstance(group['instructions'], list)):
                return None
            seen.add(index)
            instructions.extend(_route_instruction(ix, keys) for ix in group['instructions'])
        initialized, closed, transfers = {}, set(), []
        for ix in instructions:
            if ix.get('programId') not in TOKEN_PROGRAMS:
                continue
            parsed = ix.get('parsed') or {}
            kind, info = parsed.get('type'), parsed.get('info') or {}
            if not isinstance(info, dict):
                return None
            program = ix['programId']
            if kind in ('initializeAccount', 'initializeAccount2', 'initializeAccount3'):
                identity = (address(info['owner']), address(info['mint']), program,
                            decimals.get(info['mint']))
                account = address(info['account'])
                if account not in keys or initialized.setdefault(account, identity) != identity:
                    return None
            elif kind == 'closeAccount':
                closed.add((info['account'], program))
            elif kind in ('transfer', 'transferChecked', 'transferCheckedWithFee'):
                transfers.append((program, kind, info))
        for i in post.keys() - pre.keys():
            if meta['preBalances'][i] != 0 or initialized.get(keys[i], ())[:3] != identities[i][:3]:
                return None
        for i in pre.keys() - post.keys():
            if meta['postBalances'][i] != 0 or (keys[i], identities[i][2]) not in closed:
                return None
        bindings = {keys[i]: identity for i, identity in identities.items()}
        for account, identity in initialized.items():
            i = keys.index(account)
            if (account not in bindings and meta['preBalances'][i] == meta['postBalances'][i] == 0
                    and (account, identity[2]) in closed):
                bindings[account] = identity
        for program, kind, info in transfers:
            endpoints = [bindings.get(info[key]) for key in ('source', 'destination')]
            if (any(identity is None or identity[2] != program for identity in endpoints)
                    or endpoints[0][1] != endpoints[1][1] or not 0 <= _route_atoms(info) < 2**64):
                return None
            if kind != 'transfer' and any(identity[1] != info['mint'] or identity[3] not in
                    (None, info['tokenAmount']['decimals']) for identity in endpoints):
                return None
        return sum(pre.get(i, 0) for i in touched)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError):
        return None


def _route_instruction(ix, keys):
    """Decode compiled receipt instructions with the existing confirmed-stream parser."""
    if not isinstance(ix, dict):
        raise ValueError("instruction_metadata_invalid")
    if "parsed" in ix:
        if not isinstance(ix["parsed"], dict):
            raise ValueError("instruction_metadata_invalid")
        return ix
    import base58
    from types import SimpleNamespace
    from sniper.services.memecoin_shyft_confirmed import _instruction, SYSTEM, ATA
    if "programIdIndex" not in ix and ix.get("programId") not in TOKEN_PROGRAMS | {SYSTEM, ATA}:
        return ix
    if not isinstance(ix.get("accounts"), list) or not isinstance(ix.get("data"), str):
        raise ValueError("instruction_metadata_invalid")
    program = ix["programIdIndex"] if "programIdIndex" in ix else keys.index(ix["programId"])
    accounts = [a if type(a) is int else keys.index(a) for a in ix.get("accounts", [])]
    if any(type(a) is not int or not 0 <= a < len(keys) for a in [program] + accounts):
        raise ValueError("instruction_index_invalid")
    data = base58.b58decode(ix["data"])
    if keys[program] in TOKEN_PROGRAMS and data[:1] in (b"\x04", b"\x0d"):
        checked = data[0] == 13
        if (len(data) != (10 if checked else 9) or len(accounts) < (4 if checked else 3)
                or checked and data[9] > 18):
            raise ValueError("token_approval_invalid")
        result = dict(programId=keys[program], accounts=[keys[a] for a in accounts], data=ix["data"])
    else:
        result = _instruction(SimpleNamespace(program_id_index=program, accounts=accounts, data=data), keys)
    if "stackHeight" in ix:
        result["stackHeight"] = ix["stackHeight"]
    return result


def _owner_usdc_out(tx, owner):
    """Only a candidate for route valuation; a balance debit never establishes price."""
    snapshots = [tx["meta"].get(side) for side in ("preTokenBalances", "postTokenBalances")]
    if any(not isinstance(rows, list) or any(not isinstance(b, dict) for b in rows)
           for rows in snapshots):
        raise ValueError("source_balance_metadata_invalid")
    owned = {b.get("accountIndex") for rows in snapshots for b in rows
             if b.get("owner") == owner and b.get("mint") == USDC}
    keys, debit = _account_keys(tx), 0
    for index in owned:
        if type(index) is not int or not 0 <= index < len(keys):
            raise ValueError("route_usdc_payment_identity")
        for sign, rows in zip((1, -1), snapshots):
            matches = [b for b in rows if b.get("accountIndex") == index]
            if len(matches) > 1:
                raise ValueError("route_usdc_payment_identity")
            if not matches:
                continue  # new or receipt-closed account
            b, ui = matches[0], matches[0].get("uiTokenAmount")
            if (b.get("owner") != owner or b.get("mint") != USDC
                    or b.get("programId") != "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
                    or not isinstance(ui, dict) or type(ui.get("decimals")) is not int
                    or ui["decimals"] != 6):
                raise ValueError("route_usdc_payment_identity")
            debit += sign * _route_atoms({"amount": ui.get("amount")})
    owned_accounts = {keys[index] for index in owned}
    mint_of = {keys[b["accountIndex"]]: b.get("mint") for rows in snapshots for b in rows
               if type(b.get("accountIndex")) is int and 0 <= b["accountIndex"] < len(keys)}
    instructions = [_route_instruction(ix, keys) for ix in
                    list(tx["transaction"]["message"].get("instructions", [])) +
                    [ix for g in tx["meta"].get("innerInstructions") or [] for ix in g["instructions"]]]
    created = set()
    for ix in instructions:
        parsed = ix.get("parsed") or {}
        info, kind = parsed.get("info") or {}, parsed.get("type", "")
        if not isinstance(info, dict):
            raise ValueError("route_usdc_payment_identity")
        if (info.get("mint") == USDC and owner in (info.get("owner"), info.get("wallet"))
                and (kind.startswith("initializeAccount") and ix.get("programId") ==
                     "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
                     or kind in ("create", "createIdempotent") and
                     ix.get("program") == "spl-associated-token-account" and info.get("tokenProgram") ==
                     "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")):
            created.add(info.get("account"))
    for ix in instructions:
        parsed = ix.get("parsed") or {}
        info = parsed.get("info") or {}
        if (ix.get("program") in _TOKEN_PARSERS and parsed.get("type") in ("transfer", "transferChecked")
                and (info.get("authority") or info.get("multisigAuthority")) == owner
                and (info.get("mint") or mint_of.get(info.get("source")) or
                     mint_of.get(info.get("destination"))) == USDC):
            token_amount = info.get("tokenAmount")
            if (info.get("source") not in owned_accounts | created or ix.get("programId") !=
                    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA" or
                    parsed["type"] == "transferChecked" and (not isinstance(token_amount, dict) or
                    type(token_amount.get("decimals")) is not int or token_amount["decimals"] != 6)):
                raise ValueError("route_usdc_payment_identity")
    return debit


def _output(tx, owner, mint, amount, account=None):
    """One owner/mint token account, matching atomic increase and stable decimals/program."""
    keys = _account_keys(tx)
    pre = {b["accountIndex"]: b for b in tx["meta"].get("preTokenBalances", [])}
    candidates = [b for b in tx["meta"].get("postTokenBalances", [])
                  if b.get("owner") == owner and (mint is None or b.get("mint") == mint)
                  and (account is None or keys[b["accountIndex"]] == account)]
    matches = []
    for b in candidates:
        ui = b["uiTokenAmount"]
        old = pre.get(b["accountIndex"])
        decimals = ui["decimals"]
        if (type(decimals) is not int or not 0 <= decimals <= 18
                or b.get("programId") not in TOKEN_PROGRAMS):
            continue
        if old and any(old.get(k) != b.get(k) for k in ("owner", "mint", "programId")):
            continue
        if old and old["uiTokenAmount"]["decimals"] != decimals:
            continue
        delta = int(ui["amount"]) - (int(old["uiTokenAmount"]["amount"]) if old else 0)
        if delta == amount and delta > 0:
            matches.append((b["mint"], decimals, b["programId"]))
    if len(matches) != 1:
        raise ValueError("output_identity_or_quantity")
    return matches[0]


# The instructions whose BuyEvent this values, and where each puts the base/quote token programs.
# Both v2 names have 17 accounts; buy_v2 is proved by retained 100xbroker receipt 2vcqP5A3.
AMM_TOKEN_PROGRAMS_AT = {"buy": (11, 12), "buy_exact_quote_in": (11, 12), "buy_exact_quote_in_v2": (9, 10), "buy_v2": (9, 10)}


def _amm_accounts(tx, event, mint, token_program):
    """Bind the event's pool/user/accounts to a successful PumpSwap buy instruction.

    Account order: 0 pool, 1 user, 3 base mint, 4 quote mint, 5/6 user base/quote accounts,
    8 pool quote vault, then the base/quote token programs at AMM_TOKEN_PROGRAMS_AT (11/12 on v1,
    9/10 on the 17-account buy_exact_quote_in_v2). A WSOL/classic-token quote returns; a
    VERIFIED other quote raises non_sol_quote_amm for the route valuation (alert c3843ec8, a pool
    quoted in a Token-2022 pump token); anything else is quote_or_instruction_identity."""
    import base58
    import hashlib
    keys = _account_keys(tx)
    instructions = list(tx["transaction"]["message"].get("instructions", []))
    for group in tx["meta"].get("innerInstructions", []):
        instructions.extend(group["instructions"])
    found = []
    discriminator = hashlib.sha256(("global:" + event["ix_name"]).encode()).digest()[:8]
    base_program_at, quote_program_at = AMM_TOKEN_PROGRAMS_AT[event["ix_name"]]
    for ix in instructions:
        program = ix.get("programId") or keys[ix["programIdIndex"]]
        if program != AMM_PROGRAM:
            continue
        accounts = [keys[a] if isinstance(a, int) else a for a in ix.get("accounts", [])]
        if (len(accounts) >= 13 and accounts[0:2] == [event["pool"], event["user"]]
                and accounts[3] == mint
                and accounts[5:7] == [event["user_base_token_account"], event["user_quote_token_account"]]
                and accounts[base_program_at] == token_program
                and base58.b58decode(ix.get("data", ""))[:8] == discriminator):
            found.append(accounts)
    if len(found) != 1:
        raise ValueError("quote_or_instruction_identity")
    accounts = found[0]
    quote_mint, quote_program = accounts[4], accounts[quote_program_at]
    if quote_mint == WSOL and quote_program == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA":
        return
    # The quote asset is verified from the receipt, never from the instruction alone: the pool's
    # quote vault must hold that mint under that program, owned by the pool, and the user's quote
    # account, when it has a balance row, must agree.
    rows = {}
    for bal in tx["meta"].get("preTokenBalances", []) + tx["meta"].get("postTokenBalances", []):
        i = bal.get("accountIndex")
        if isinstance(i, int) and 0 <= i < len(keys):
            rows.setdefault(keys[i], []).append(bal)
    vault, user_quote = rows.get(accounts[8], []), rows.get(accounts[6], [])
    if (quote_mint != WSOL and quote_program in TOKEN_PROGRAMS and vault
            and all((b.get("owner"), b.get("mint"), b.get("programId")) == (accounts[0], quote_mint, quote_program)
                    for b in vault)
            and all((b.get("owner"), b.get("mint"), b.get("programId")) == (accounts[1], quote_mint, quote_program)
                    for b in user_quote)):
        raise ValueError("non_sol_quote_amm")
    raise ValueError("quote_or_instruction_identity")


def value_source_buy(tx, owner, *, sol_usd, now):
    """USD uses net pool input, matching the slot lane; gross spend is retained separately.

    A missing price or unsupported/ambiguous receipt leaves size/price unknown. Facts describe
    this confirmed transaction only; source block time and local USD observation stay separate.
    """
    facts = dict(source_size_usd=None, source_price=None, source_pre_atomic=None, basis="unknown",
                 source_slot=tx.get("slot"), source_block_time=tx.get("blockTime"),
                 source_clock_provenance="confirmed_getTransaction", owner=owner)
    try:
        if tx["meta"].get("err") is not None:
            raise ValueError("failed_transaction")
        usdc_out = _owner_usdc_out(tx, owner)
        found = []
        for program, decoder, disc in ((PUMP_PROGRAM, parse_trade_event, bytes.fromhex("bddb7fd34ee661ee")),
                                      (AMM_PROGRAM, parse_buy_event, bytes.fromhex("67f4521f2cf57777"))):
            for log_index, line in program_data_logs(tx["meta"].get("logMessages", []), program):
                if anchor_event_bytes(line)[:8] != disc:
                    continue
                event = decoder(line)  # A malformed matching event makes the transaction unknown.
                if event["user"] == owner and event.get("is_buy", True):
                    found.append((program, log_index, event, line))
        if len(found) > 1:
            # DISTINCT own buy events are legs of one route (notdecu 823NjegkV3, 2026-09-27: a
            # PumpSwap leg buying the quote token, then the curve buy); the route valuation decides.
            # The same event line twice is a receipt contradicting itself (Codex's guard): unknown.
            lines = [f[3] for f in found]
            if len(set(lines)) == len(lines):
                chain = _amm_chain(tx, found)
                if chain is not None:
                    facts.update(route_intermediate_mint=chain[0], route_expected_mint=chain[1])
            raise ValueError("ambiguous_buy_event" if len(set(lines)) < len(lines)
                             else "multiple_buy_events")
        if not found:
            raise ValueError("missing_buy_event")
        program, log_index, event, _line = found[0]
        if event["cashback"]:
            raise ValueError("unsupported_cashback_semantics")
        if program == PUMP_PROGRAM:
            if event["quote_mint"] not in SOL_QUOTE_MINTS:
                from sniper.services.memecoin_slot_graduation import quote_curves_on
                if quote_curves_on():
                    # The route must end in this coin, and the curve's own quote is a hop of it, never
                    # a target (HI EtVFMDoB: a 7.04 WLDTE73P residue made route_target_ambiguous).
                    facts["route_expected_mint"] = event["mint"]
                    facts["route_intermediate_mint"] = event["quote_mint"]
                raise ValueError("non_sol_quote")
            mint, decimals, token_program = _output(tx, owner, event["mint"], event["token_amount"])
            amount, net = event["token_amount"], event["sol_amount"]
            gross = trade_event_gross_lamports(event)
            if gross is None:
                raise ValueError("invalid_quote_amount")
            quote_mint = event["quote_mint"]
        else:
            amount = event["base_amount_out"]
            mint, decimals, token_program = _output(tx, owner, None, amount, event["user_base_token_account"])
            if event["ix_name"] not in AMM_TOKEN_PROGRAMS_AT:
                # Keep public decoder evidence; an unrecognized layout remains unauthorized.
                import base58
                keys = _account_keys(tx)
                instructions = list(tx["transaction"]["message"].get("instructions", []))
                instructions += [ix for group in tx["meta"].get("innerInstructions", [])
                                 for ix in group["instructions"]]
                facts.update(instruction_name=event["ix_name"], program_id=program,
                             pool=event["pool"], event_log_index=log_index)
                for ix in instructions:
                    program_id = ix.get("programId") or keys[ix["programIdIndex"]]
                    accounts = [keys[a] if isinstance(a, int) else a
                                for a in ix.get("accounts", [])]
                    if program_id == program and accounts[:2] == [event["pool"], owner]:
                        facts["unsupported_instruction_evidence"] = {
                            "account_positions": accounts[:64],
                            "instruction_discriminator": base58.b58decode(ix.get("data", ""))[:8].hex(),
                            "event_log_index": log_index}
                        break
                raise ValueError("unsupported_buy_instruction")
            try:
                _amm_accounts(tx, event, mint, token_program)
            except ValueError as exc:
                if str(exc) == "non_sol_quote_amm":
                    facts["route_expected_mint"] = mint   # the route must end in this base mint
                raise
            net = event["quote_amount_in_with_lp_fee"] - event["lp_fee"]
            gross = buy_event_gross_quote(event)
            if gross is None:
                raise ValueError("inconsistent_quote_fees")
            quote_mint = WSOL
        if net <= 0 or gross < net:
            raise ValueError("invalid_quote_amount")
        facts.update(basis="pump_event_net_sol", mint=mint, token_decimals=decimals,
                     token_program=token_program, token_amount_atomic=str(amount),
                     token_delta=amount / 10**decimals, quote_mint=quote_mint,
                     gross_quote_atomic=str(gross), net_pool_quote_atomic=str(net),
                     program_id=program, event_log_index=log_index, instruction_name=event["ix_name"])
        facts['source_pre_atomic'] = source_pre_atomic(tx, owner, mint)
        if (program == AMM_PROGRAM and facts['source_pre_atomic'] == 0
                and event['user_base_token_reserves'] > 0):
            facts['source_pre_atomic'] = event['user_base_token_reserves']
        if program == AMM_PROGRAM:
            # The pool proves a pump mint without a read: pump's canonical migration pool is derived
            # from the mint (memecoin_onchain_lane.pump_mint_proven, 523c9968).
            facts["pool"] = event["pool"]
        facts.update(source_impact_ratio=_impact_ratio(program, event, net, amount), source_post_price=None)
        # A valid final Pump event can still be a WSOL hop of a USDC-funded route (RICK
        # 456550933). Its pool input is not the owner's payment. Balance metadata selects
        # the route; only the linked swap transfers below may establish its USD price.
        if usdc_out > 0:
            facts.update(route_expected_mint=mint, route_owner_usdc_out_atomic=str(usdc_out))
            raise ValueError("owner_usdc_funding")
        price = _fresh_sol_usd(sol_usd, now, facts)
        facts.update(gross_swap_size_usd=gross / 1e9 * price,
                     source_size_usd=net / 1e9 * price,
                     source_price=net / 1e9 * price / facts["token_delta"])
        if facts["source_impact_ratio"] is not None:
            facts["source_post_price"] = facts["source_price"] * facts["source_impact_ratio"]
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
        facts["unknown_reason"] = str(exc)
        if str(exc) in ROUTE_FALLBACK_REASONS:
            return _value_route(tx, owner, facts, sol_usd, now)
    return facts


def _impact_ratio(program, event, net, amount):
    """The source's POST-trade price over its own AVERAGE fill price, unit-free, from this receipt.
    None when an input is unreadable or the ratio is implausible.

    pump curve: the TradeEvent's virtual reserves are post-trade (the slot lane's curve_price_usd);
    the ratio is that marginal price over net lamports per atomic token.
    PumpSwap: for a constant-product pool, post / average = B_pre / (B_pre - base_out). The BuyEvent
    carries the PRE-trade base reserve, and the base side holds no virtual reserve, so this needs
    neither the pool's virtual quote reserve (absent from the event) nor any USD price. A vault-only
    quote-side price is wrong on pump-migrated pools: it read 0.78 on the real untaxxable receipt."""
    try:
        if program == PUMP_PROGRAM:
            post = int(event["virtual_sol_reserves"]) / int(event["virtual_token_reserves"])
            ratio = post / (net / amount)
        else:
            base_pre = int(event["pool_base_token_reserves"])
            ratio = base_pre / (base_pre - int(event["base_amount_out"]))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return ratio if math.isfinite(ratio) and IMPACT_RATIO_MIN <= ratio <= IMPACT_RATIO_MAX else None


def _fresh_sol_usd(sol_usd, now, facts):
    """The SOL/USD observation, recorded on `facts`, or ValueError when missing or stale."""
    if sol_usd is None:
        raise ValueError("sol_usd_unavailable")
    price, observed = sol_usd
    age = now - observed
    if not all(math.isfinite(v) for v in (price, observed, age)) or price <= 0:
        raise ValueError("sol_usd_stale_or_invalid")
    facts.update(sol_usd=price, sol_usd_observed_at=observed, sol_usd_age_s=age,
                 sol_usd_source="capture_sol_usdc_quote")
    if not 0 <= age <= SOL_USD_MAX_AGE_S:
        raise ValueError("sol_usd_stale_or_invalid")
    return price


# ROUTED BUYS (2026-09-26, alert 753be289). A router bought the target through 2 SOL -> PumpSwap
# SOL/USDC -> USDC -> BPxx -> target; the one Pump event is the FIRST leg (buying USDC into a
# temporary account closed in the same transaction), so the event binding above fails. The receipt
# holds every leg: value the route by what the owner's accounts, temporary ones included, sent out
# in quote and what the owner's balance of the target gained. GROSS of pool fees on every hop,
# unlike the event path's net pool input; the basis says which. Anything the rule cannot attribute
# stays unknown with a typed reason rather than borrowing another leg's amount.
# NOT ambiguous_buy_event: the same owner buy event twice is a receipt that contradicts itself, and
# it stays unknown (distinct events are multiple_buy_events, a route). non_sol_quote (2026-09-27): a pump curve quoted in
# another token, bought from wSOL through a router, left 12 of notdecu's buys 'unknown size'; the
# route counts only wSOL/USDC the owner sent out, so a buy paid in the quote token stays unknown.
# non_sol_quote_amm (alert c3843ec8, 2026-09-27) is the same shape on a PumpSwap pool quoted in a
# pump token, with the quote verified from the receipt; its route must end in the pool's base mint.
ROUTE_FALLBACK_REASONS = {"output_identity_or_quantity", "missing_buy_event", "non_sol_quote",
                          "multiple_buy_events", "non_sol_quote_amm", "owner_usdc_funding"}
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ROUTE_QUOTE_DECIMALS = {WSOL: 9, USDC: 6}
_TOKEN_PARSERS = {"spl-token", "spl-token-2022"}


def _route_facts(tx, owner, intermediate=None, hop_out=None):
    """(mint, decimals, token_program, net_receipt_atomic, {quote_mint: out_atomic}) or ValueError.

    `intermediate` (a quote-token curve's quote, value_source_buy) is a hop, not a target: a positive
    residue of it left with the owner is excluded from the targets, and the quote out is pro-rated to
    the share of it that went on into the coin, spent / bought, bought being every transfer of it into
    the owner's accounts. Only when the owner's balance of it moved by exactly bought - sent (no
    transfer fee, no pre-held quote spent); else route_intermediate_unbound. `hop_out` (a dict) gets
    {"mint", "inflow_atomic", "residue_atomic"} when it applied."""
    keys = _account_keys(tx)
    parsed = []
    for ix in list(tx["transaction"]["message"].get("instructions", [])) + [
            ix for group in tx["meta"].get("innerInstructions") or [] for ix in group["instructions"]]:
        ix = _route_instruction(ix, keys)
        if isinstance(ix.get("parsed"), dict):
            parsed.append((ix.get("program"), ix["parsed"].get("type") or "", ix["parsed"].get("info") or {}))
    transfers = [info for program, kind, info in parsed
                 if program in _TOKEN_PARSERS and kind in ("transfer", "transferChecked")]
    if not transfers:
        raise ValueError("route_unparsed")
    # The owner's token accounts: its balance rows, plus accounts created for it in this
    # transaction (temporary ones never reach a balance row because they are closed before the end).
    mint_of, mine = {}, set()
    for bal in tx["meta"].get("preTokenBalances", []) + tx["meta"].get("postTokenBalances", []):
        i = bal.get("accountIndex")
        if isinstance(i, int) and 0 <= i < len(keys):
            mint_of[keys[i]] = bal.get("mint")
            if bal.get("owner") == owner:
                mine.add(keys[i])
    for program, kind, info in parsed:
        if kind.startswith("initializeAccount") or kind in ("create", "createIdempotent"):
            if info.get("account"):
                mint_of[info["account"]] = info.get("mint")
                if owner in (info.get("owner"), info.get("wallet")):
                    mine.add(info["account"])
    flow, account_flow = {}, {}                  # mint net and each owned account's net
    inflow, outflow = {}, {}                     # per mint, across the owner boundary only
    for info in transfers:
        src, dst = info.get("source") in mine, info.get("destination") in mine
        if not src and not dst:
            continue
        mint = info.get("mint") or mint_of.get(info.get("source")) or mint_of.get(info.get("destination"))
        amount = info.get("amount") or (info.get("tokenAmount") or {}).get("amount")
        if mint is None or amount is None:
            raise ValueError("route_unparsed")
        for account, sign in ((info.get("source"), -1), (info.get("destination"), 1)):
            if account in mine:
                if mint_of.get(account) != mint:
                    raise ValueError("route_transfer_identity")
                account_flow[account] = account_flow.get(account, 0) + sign * int(amount)
        if src != dst:
            flow[mint] = flow.get(mint, 0) + (int(amount) if dst else -int(amount))
            side = inflow if dst else outflow
            side[mint] = side.get(mint, 0) + int(amount)
    # PLT 2ebfcf66: USDC -> temporary WSOL -> token; the WSOL residue was unwrapped
    # to the router's fee account. CloseAccount forwards ALL lamports, but is not an
    # SPL transfer. Only a new, receipt-closed native account can explain this outflow.
    created = {info.get("newAccount") for program, kind, info in parsed
               if program == "system" and kind in ("createAccount", "createAccountWithSeed")}
    closes = [info for program, kind, info in parsed
              if program in _TOKEN_PARSERS and kind == "closeAccount"]
    initialized = {info.get("account") for program, kind, info in parsed
                   if program in _TOKEN_PARSERS and kind.startswith("initializeAccount")
                   and info.get("owner") == owner and info.get("mint") == WSOL}
    for info in closes:
        account = info.get("account")
        if (account not in created or account not in initialized or account not in mine
                or mint_of.get(account) != WSOL or info.get("owner") != owner
                or not info.get("destination") or info["destination"] == owner
                or info["destination"] in mine
                or sum(c.get("account") == account for c in closes) != 1):
            continue
        i = keys.index(account)
        if (tx["meta"]["preBalances"][i] != 0 or tx["meta"]["postBalances"][i] != 0
                or any(b.get("accountIndex") == i for b in tx["meta"].get("preTokenBalances", []))):
            continue
        flow[WSOL] = flow.get(WSOL, 0) - max(0, account_flow.get(account, 0))
    # The target: the ONE non-quote mint whose owner balance rose. Balance, not transfer, because
    # a Token-2022 transfer fee makes the owner receive less than the transfer names (3% here).
    rows, seen = {}, {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for bal in tx["meta"].get(side, []):
            if bal.get("owner") == owner:
                ident = (bal.get("mint"), bal["uiTokenAmount"]["decimals"], bal.get("programId"))
                if seen.setdefault(bal.get("accountIndex"), ident) != ident:
                    raise ValueError("route_target_identity")   # one account changed identity
                row = rows.setdefault(bal.get("mint"), [0, None, None])
                row[0] += sign * int(bal["uiTokenAmount"]["amount"])
                if sign > 0:
                    row[1], row[2] = ident[1], ident[2]
    residue = rows[intermediate][0] if intermediate is not None and intermediate in rows else 0
    targets = [m for m, (d, _dec, _p) in rows.items() if m not in ROUTE_QUOTE_DECIMALS and d > 0
               and not (residue > 0 and m == intermediate)]
    if len(targets) != 1:
        raise ValueError("route_target_ambiguous" if targets else "route_no_target")
    mint = targets[0]
    amount, decimals, token_program = rows[mint]
    if type(decimals) is not int or not 0 <= decimals <= 18 or token_program not in TOKEN_PROGRAMS:
        raise ValueError("route_target_identity")
    if any(m not in ROUTE_QUOTE_DECIMALS and m != mint and f < 0 for m, f in flow.items()) or any(
            m not in ROUTE_QUOTE_DECIMALS and m != mint and d < 0 for m, (d, _x, _y) in rows.items()):
        raise ValueError("route_non_quote_funding")
    quote_out = {q: -flow.get(q, 0) for q in ROUTE_QUOTE_DECIMALS}
    if any(v < 0 for v in quote_out.values()):
        raise ValueError("route_quote_inflow")
    if not any(quote_out.values()):
        raise ValueError("route_no_quote_out")
    if residue > 0:
        bought, sent = inflow.get(intermediate, 0), outflow.get(intermediate, 0)
        if bought <= residue or bought - sent != residue:
            raise ValueError("route_intermediate_unbound")
        quote_out = {q: v * (bought - residue) // bought for q, v in quote_out.items()}
        if hop_out is not None:          # not `info`: that name is the transfer loop's variable
            hop_out.update(mint=intermediate, inflow_atomic=str(bought), residue_atomic=str(residue))
    return mint, decimals, token_program, amount, quote_out


class AmbiguousRoutePayment(Exception):
    """A swap's own invoker also moved a second payment out of the route (EVM-03, Codex review
    2026-10-07): a platform fee beside the purchase, a venue's host/referral fee, or two swaps under
    one instruction. Mint and destination cannot tell which outflow paid the swap."""

    def __init__(self, depth, program=None, share_pct=None):
        super().__init__(depth)
        self.depth, self.program, self.share_pct = depth, program, share_pct

    def why(self):
        """'fee_sibling:d=<hops>:p=<invoker prefix>:s=<other outflows / this payment, %>' (Gamma on
        8c5562ef8): ~0.1-2% reads as a fee, >5% as a second swap; p names the venue. <= 48 chars."""
        share = "?" if self.share_pct is None else "%.1f%%" % min(self.share_pct, 99999.9)
        return "fee_sibling:d=%d:p=%s:s=%s" % (self.depth, str(self.program or "?")[:6], share)


def route_payment_evidence(tx, owner, net_paid_atomic):
    """(USDC atoms the owner paid INTO THE ROUTE's swaps, None), or (None, why) when none can be
    bound: why is "no_call_tree", "fee_sibling:depth=<n>" (n = router hops from the owner) or
    "no_swap_leg" (Gamma's N1 residual 1 and EVM-03, 2026-10-07). A platform/referral fee or an
    unrelated outflow is excluded, so it can no longer raise the source's average and loosen the
    hard copy-premium cap. Capped at the owner's net USDC out (a refund only lowers what the route
    kept). See route_usdc_legs."""
    try:
        legs = route_usdc_legs(tx, owner)
    except AmbiguousRoutePayment as exc:
        return None, exc.why()
    except ValueError as exc:
        return None, str(exc)
    if legs is None:
        return None, "no_call_tree"
    paid = sum(routed for _amount, _dst, routed in legs)
    if paid <= 0:
        return None, "no_swap_leg"
    return (min(paid, int(net_paid_atomic)) if net_paid_atomic else paid), None


def route_payment_atomic(tx, owner, net_paid_atomic):
    """route_payment_evidence's amount alone: None when no payment can be bound."""
    return route_payment_evidence(tx, owner, net_paid_atomic)[0]


ROUTE_HOP_DEPTH = 4


def _route_atoms(info):
    if not isinstance(info, dict):
        raise ValueError("pumpswap_payment_unbound")
    token_amount = info.get("tokenAmount", {})
    if not isinstance(token_amount, dict):
        raise ValueError("pumpswap_payment_unbound")
    amount = info["amount"] if "amount" in info else token_amount.get("amount")
    values = (amount, token_amount['amount']) if 'amount' in token_amount else (amount,)
    for value in values:
        if not (type(value) is int and value >= 0 or
                isinstance(value, str) and value.isascii() and value.isdecimal()):
            raise ValueError("pumpswap_payment_unbound")
        if int(value) != int(amount):
            raise ValueError("pumpswap_payment_unbound")
    return int(amount)


def _pumpswap_v2_payments(tx, keys, invoker, children):
    """Bind v2 pool/buyback transfers to the successful buy's own emitted event."""
    import base58
    import base64
    import hashlib
    from sniper.execution.pump_native_exit import ata, _pk

    try:
        if "err" not in tx["meta"] or tx["meta"]["err"] is not None:
            raise ValueError("pumpswap_payment_unbound")
        accounts = [keys[a] if isinstance(a, int) else a for a in invoker["accounts"]]
        data = base58.b58decode(invoker["data"])
        # shortcut: only the verified 17-account buy_v2 layout; add other layouts after receipt proof.
        if (len(accounts) != 17 or data[:8] != hashlib.sha256(b"global:buy_v2").digest()[:8]
                or len(data) != 24 or accounts[16] != AMM_PROGRAM
                or accounts[9] not in TOKEN_PROGRAMS or accounts[10] not in TOKEN_PROGRAMS):
            raise ValueError("pumpswap_payment_unbound")
        emitted = [base58.b58decode(ix.get("data", ""))[8:] for ix in children
                   if ix.get("programId") == AMM_PROGRAM
                   and ix.get("accounts") == [accounts[15]]
                   and base58.b58decode(ix.get("data", ""))[:8] == bytes.fromhex("e445a52e51cb9a1d")]
        logs = [anchor_event_bytes(line) for _i, line in
                program_data_logs(tx["meta"].get("logMessages", []), AMM_PROGRAM)]
        if len(emitted) != 1 or logs.count(emitted[0]) != 1:
            raise ValueError("pumpswap_payment_unbound")
        event = parse_buy_event("Program data: " + base64.b64encode(emitted[0]).decode())
        gross = buy_event_gross_quote(event)
        fee = event["buyback_fee"]
        if (event["ix_name"] != "buy_v2" or event["cashback"] or gross is None
                or not 0 <= fee <= event["protocol_fee"]
                or not 0 <= event["buyback_fee_basis_points"] <= 10000
                or fee != event["protocol_fee"] * event["buyback_fee_basis_points"] // 10000
                or (event["pool"], event["user"], event["user_base_token_account"],
                    event["user_quote_token_account"]) != (accounts[0], accounts[1], accounts[5], accounts[6])
                or int.from_bytes(data[8:16], "little") != event["base_amount_out"]
                or int.from_bytes(data[16:24], "little") != event["max_quote_amount_in"]):
            raise ValueError("pumpswap_payment_unbound")
        rows = {}
        for bal in tx["meta"].get("preTokenBalances", []) + tx["meta"].get("postTokenBalances", []):
            rows.setdefault(keys[bal["accountIndex"]], []).append(bal)
        decimals = {}
        for account, actor, mint, program in (
                (accounts[5], accounts[1], accounts[3], accounts[9]),
                (accounts[6], accounts[1], accounts[4], accounts[10]),
                (accounts[7], accounts[0], accounts[3], accounts[9]),
                (accounts[8], accounts[0], accounts[4], accounts[10])):
            if not rows.get(account) or any((r.get("owner"), r.get("mint"), r.get("programId"))
                                           != (actor, mint, program) for r in rows[account]):
                raise ValueError("pumpswap_payment_unbound")
            d = rows[account][0]["uiTokenAmount"]["decimals"]
            if (type(d) is not int or not 0 <= d <= 18
                    or any(r["uiTokenAmount"]["decimals"] != d for r in rows[account])):
                raise ValueError("pumpswap_payment_unbound")
            decimals[mint] = d
        if decimals.get(USDC) != 6:
            raise ValueError("pumpswap_payment_unbound")
        fee_rows = rows.get(accounts[14], [])
        if not fee_rows or any((r.get("owner"), r.get("mint"), r.get("programId")) !=
                               (fee_rows[0].get("owner"), accounts[4], accounts[10])
                               or r["uiTokenAmount"]["decimals"] != decimals[accounts[4]] for r in fee_rows):
            raise ValueError("pumpswap_payment_unbound")
        if str(ata(_pk(fee_rows[0]["owner"]), _pk(accounts[4]), _pk(accounts[10]))) != accounts[14]:
            raise ValueError("pumpswap_payment_unbound")
        expected = {(accounts[7], accounts[5], accounts[0], accounts[3], accounts[9]): event["base_amount_out"],
                    (accounts[6], accounts[8], accounts[1], accounts[4], accounts[10]): gross - fee}
        if fee:
            expected[(accounts[6], accounts[14], accounts[1], accounts[4], accounts[10])] = fee
        paid = {}
        for ix in children:
            parsed = ix.get("parsed")
            if not isinstance(parsed, dict):
                continue
            info = parsed.get("info")
            amount = _route_atoms(info)
            identity = (info.get("source"), info.get("destination"), info.get("authority"),
                        info.get("mint"), ix.get("programId"))
            if (parsed.get("type") != "transferChecked" or ix.get("program") not in _TOKEN_PARSERS
                    or (info.get("tokenAmount") or {}).get("decimals") != decimals.get(info.get("mint"))
                    or identity not in expected or amount != expected.pop(identity) or amount <= 0):
                raise ValueError("pumpswap_payment_unbound")
            if info.get("source") == accounts[6]:
                paid[id(info)] = amount
        if expected or len(children) != len(paid) + 2:
            raise ValueError("pumpswap_payment_unbound")
        return paid
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        raise ValueError("pumpswap_payment_unbound") from exc


def route_usdc_legs(tx, owner):
    """[(atoms, destination, routed atoms)] for every USDC transfer the owner authorized out of its
    own accounts, or None without a call tree (an inner instruction without stackHeight).

    A SWAP: a transfer whose invoking instruction also moved tokens, among its own direct children,
    from outside the route into a route account (the swap's other side). ROUTE ACCOUNTS are the
    owner's accounts plus every account that both receives and sends in the transaction (a router's
    or the owner's temporary hop). A swap counts whole. A transfer into a route hop counts the share
    of that hop's outflows that reach swaps, followed up to ROUTE_HOP_DEPTH hops; anything else
    counts zero. Real shapes, 2026-10-07: DFlow pays a prop AMM directly (4rTe5hhHyX: the quote vault
    and the PDA paying the other side differ, so destination owners are not used); proVF4 takes the
    whole amount into its own account and pays the AMMs from there (27MRyWXYrE); DFlow's 0.3% fee
    passes through 7Rb2u9 into HrTf9C, which never sends, so it counts zero.

    A swap whose invoker ALSO moves another transfer out of a route account (a fee beside the
    payment) raises AmbiguousRoutePayment (EVM-03): its payout cannot say which outflow it paid for.

    A NESTED PAYOUT (2026-10-07, 4EFmaKjg: DFlow into a prop AMM whose instruction pays USDT back
    from a call one level down, height 4 beside a height-3 payment). When the direct children show
    no swap, the leg still counts whole if the invoker's WHOLE call tree moves tokens from outside
    the route into a route account and moves nothing else out of a route account. Anything else
    under that tree (a fee, a second payment) leaves the leg as before: never a guess."""
    keys = _account_keys(tx)
    mint_of, mine = {}, set()
    balances = []
    for side in ("preTokenBalances", "postTokenBalances"):
        rows = tx["meta"].get(side, [])
        if (not isinstance(rows, list) or any(not isinstance(b, dict)
                or not isinstance(b.get("uiTokenAmount"), dict) for b in rows)):
            raise ValueError("pumpswap_payment_unbound")
        balances.extend(rows)
    for bal in balances:
        i = bal.get("accountIndex")
        if isinstance(i, int) and 0 <= i < len(keys):
            mint_of[keys[i]] = bal.get("mint")
            if bal.get("owner") == owner:
                mine.add(keys[i])
    top = [_route_instruction(ix, keys) for ix in tx["transaction"]["message"].get("instructions", [])]
    inner_groups = tx["meta"].get("innerInstructions") or []
    indices = [g["index"] for g in inner_groups if isinstance(g.get("index"), int)]
    if len(indices) != len(set(indices)):
        raise ValueError("route_duplicate_inner_index")
    inner = {g["index"]: [_route_instruction(ix, keys) for ix in g["instructions"]] for g in inner_groups
             if isinstance(g.get("index"), int)}
    groups = [[(1, ix)] + [(c.get("stackHeight"), c) for c in inner.get(i, ())] for i, ix in enumerate(top)]
    if any(type(h) is not int for group in groups for h, _ix in group):
        return None                              # no call tree: the legs cannot be bound
    for group in groups:                         # accounts created for the owner (temporary wSOL)
        for _h, ix in group:
            info = (ix.get("parsed") or {}).get("info") if isinstance(ix.get("parsed"), dict) else None
            if info is not None and not isinstance(info, dict):
                raise ValueError("pumpswap_payment_unbound")
            kind = ix["parsed"].get("type", "") if info is not None else ""
            if info and (kind.startswith("initializeAccount") or kind in ("create", "createIdempotent")):
                if info.get("account") and owner in (info.get("owner"), info.get("wallet")):
                    mine.add(info["account"])
                    mint_of.setdefault(info["account"], info.get("mint"))

    def transfer(ix):
        parsed = ix.get("parsed")
        if (isinstance(parsed, dict) and ix.get("program") in _TOKEN_PARSERS
                and parsed.get("type") in ("transfer", "transferChecked")):
            return parsed.get("info") or {}
        return None
    def atoms(info):
        return _route_atoms(info)
    moves = []                                   # (info, amount, its invoker's direct children, invoker)
    calls = {}
    trees = {}                                   # id(info) -> every instruction under its invoker but it
    for group in groups:
        for n, (height, ix) in enumerate(group):
            info = transfer(ix)
            if info is None:
                continue
            amount = atoms(info)
            # The invoker: the nearest earlier instruction one level up; its direct children are the
            # instructions after it at this height, until the stack returns to its level.
            parent = next((p for p in range(n - 1, -1, -1) if group[p][0] == height - 1), None)
            siblings, tree = [], []
            if parent is not None:
                for h2, ix2 in group[parent + 1:]:
                    if h2 <= height - 1:
                        break
                    if ix2 is not ix:
                        tree.append(ix2)
                        if h2 == height:
                            siblings.append(ix2)
            trees[id(info)] = tree
            invoker = group[parent][1] if parent is not None else None
            calls[id(invoker)] = tree + [ix]
            moves.append((info, amount, siblings, invoker))
    sources = {info.get("source") for info, _a, _s, _p in moves}
    route = mine | (sources & {info.get("destination") for info, _a, _s, _p in moves})

    # Unwrapping sends no SPL transfer: bind the closed WSOL hop to its later exact native buy.
    if tx["meta"].get("err", True) is None:
        parsed = [(group, n, height, ix["parsed"]) for group in groups
                  for n, (height, ix) in enumerate(group) if isinstance(ix.get("parsed"), dict)]
        for group, n, height, instruction in parsed:
            if instruction.get("type") != "closeAccount" or group[n][1].get("program") not in _TOKEN_PARSERS:
                continue
            close = instruction.get("info") or {}
            account, actor = close.get("account"), close.get("owner")
            if (account not in keys or not actor or close.get("destination") != actor
                    or account in route or actor in mine):
                continue
            initial = [(g, j, p.get("info") or {}) for g, j, _h, p in parsed
                       if p.get("type", "").startswith("initializeAccount")
                       and (p.get("info") or {}).get("account") == account]
            closes = [p for _g, _j, _h, p in parsed if p.get("type") == "closeAccount"
                      and (p.get("info") or {}).get("account") == account]
            index = keys.index(account)
            pre, post = tx["meta"].get("preBalances"), tx["meta"].get("postBalances")
            if (not isinstance(pre, list) or not isinstance(post, list)
                    or index >= len(pre) or index >= len(post)
                    or type(pre[index]) is not int or type(post[index]) is not int):
                continue
            if (len(initial) != 1 or len(closes) != 1
                    or initial[0][0][initial[0][1]][1].get("program") not in _TOKEN_PARSERS
                    or initial[0][2].get("mint") != WSOL or initial[0][2].get("owner") != actor
                    or groups.index(initial[0][0]) > groups.index(group)
                    or initial[0][0] is group and initial[0][1] >= n
                    or pre[index] != 0 or post[index] != 0
                    or any(b.get("accountIndex") == index for b in balances)):
                continue
            initialized_at = (groups.index(initial[0][0]), initial[0][1])
            received, valid = 0, True
            for group_index, g in enumerate(groups):
                for j, (_h, ix) in enumerate(g):
                    t = transfer(ix)
                    if t is None or account not in (t.get("source"), t.get("destination")):
                        continue
                    if (g is not group or j >= n or (group_index, j) <= initialized_at
                            or t.get("source") in route
                            or t.get("source") == account
                            or (t.get("mint") or mint_of.get(t.get("source"))) != WSOL):
                        valid = False
                    received += atoms(t)
            if not valid or received <= 0:
                continue
            parent = next((p for p in range(n - 1, -1, -1) if group[p][0] == height - 1), None)
            if parent is None:
                continue
            candidates = []
            for j in range(n + 1, len(group)):
                h, ix = group[j]
                if h < height:
                    break
                if h != height:
                    continue
                children = []
                for h2, child in group[j + 1:]:
                    if h2 <= height:
                        break
                    children.append(child)
                native = [p.get("info") or {} for child in children
                          if child.get("program") == "system" and isinstance(p := child.get("parsed"), dict)
                          and p.get("type") == "transfer"]
                payout = [t for child in children if (t := transfer(child)) is not None
                          and t.get("destination") in route and t.get("source") not in route
                          and (t.get("mint") or mint_of.get(t.get("source"))) not in (None, WSOL, USDC)]
                if (payout and native and all(t.get("source") == actor and type(t.get("lamports")) is int
                                              and t["lamports"] > 0 for t in native)
                        and sum(t["lamports"] for t in native) == received
                        and not any(child.get("program") == "system"
                                    and isinstance(p := child.get("parsed"), dict)
                                    and p.get("type") == "transfer"
                                    and actor in ((p.get("info") or {}).get("source"),
                                                  (p.get("info") or {}).get("destination"))
                                    for _h, child in group[n + 1:j])):
                    candidates.append(j)
            if len(candidates) == 1:
                route.add(account)

    def swap(siblings):
        return any((t := transfer(s)) is not None and t.get("destination") in route
                   and t.get("source") not in route for s in siblings)

    def nested_swap(info):
        moved = [t for x in trees.get(id(info), ()) if (t := transfer(x)) is not None]
        return (any(t.get("destination") in route and t.get("source") not in route for t in moved)
                and not any(t.get("source") in route for t in moved))
    out_of = {}
    for move in moves:
        out_of.setdefault(move[0].get("source"), []).append(move)
    memo = {}
    bound_payments = {}

    def share(account, depth):
        """The fraction of `account`'s outflows that reach swaps."""
        if (account, depth) not in memo:
            memo[(account, depth)] = 0.0             # a cycle counts nothing
            outs = out_of.get(account, [])
            total = sum(a for _i, a, _s, _p in outs)
            memo[(account, depth)] = (sum(routed(m, depth + 1) for m in outs) / total) if total > 0 else 0.0
        return memo[(account, depth)]

    def routed(move, depth):
        info, amount, siblings, invoker = move
        if (invoker or {}).get("programId") == AMM_PROGRAM and len(invoker.get("accounts", [])) == 17:
            if id(invoker) not in bound_payments:
                bound_payments[id(invoker)] = _pumpswap_v2_payments(tx, keys, invoker, calls[id(invoker)])
            return bound_payments[id(invoker)].get(id(info), 0)
        if swap(siblings):
            others = [atoms(t) for s in siblings if (t := transfer(s)) is not None and t.get("source") in route]
            if others:
                raise AmbiguousRoutePayment(depth, (invoker or {}).get("programId"),
                                            100.0 * sum(others) / amount if amount > 0 else None)
            return amount
        dst = info.get("destination")
        if dst not in route and nested_swap(info):
            return amount                  # paid straight to a pool that pays back one call down
        if dst in route and dst not in mine and depth < ROUTE_HOP_DEPTH:
            return amount * share(dst, depth)
        return 0
    legs = []
    for move in moves:
        info, amount, _siblings, _invoker = move
        if ((info.get("authority") or info.get("multisigAuthority")) != owner
                or info.get("source") not in mine or info.get("destination") in mine
                or (info.get("mint") or mint_of.get(info.get("source"))) != USDC):
            continue
        legs.append((amount, info.get("destination"), int(routed(move, 0))))
    return legs


def _amm_chain(tx, found):
    """(intermediate, target) when the owner's own buy events are exactly two PumpSwap legs of ONE
    chain, in log order: leg 1 buys A, leg 2 spends A on the coin (leg 2's quote account holds the
    same mint as leg 1's base account), and A is no route quote. None otherwise.

    The apiANK chains (bigwarzeth, 2026-10-14, 8 frames): USDC -> SOL -> A -> coin, with a residue of A
    left with the owner. Without naming A as the hop, that residue was a second target
    (route_target_ambiguous), so the watcher valued nothing, proved no initiation and wrote no row:
    no lane ever judged those buys."""
    legs = sorted((f for f in found if f[0] == AMM_PROGRAM), key=lambda f: f[1])
    if len(legs) != 2 or len(found) != 2:
        return None
    keys = _account_keys(tx)
    mint_of = {}
    for bal in (tx.get("meta") or {}).get("preTokenBalances", []) + (tx.get("meta") or {}).get("postTokenBalances", []):
        i = bal.get("accountIndex")
        if isinstance(i, int) and 0 <= i < len(keys):
            mint_of[keys[i]] = bal.get("mint")
    first, second = legs[0][2], legs[1][2]
    hop = mint_of.get(first.get("user_base_token_account"))
    if (not hop or hop in ROUTE_QUOTE_DECIMALS or first.get("pool") == second.get("pool")
            or mint_of.get(second.get("user_quote_token_account")) != hop):
        return None
    target = mint_of.get(second.get("user_base_token_account"))
    return (hop, target) if target and target != hop and target not in ROUTE_QUOTE_DECIMALS else None


def _value_route(tx, owner, event_facts, sol_usd, now):
    facts = {k: event_facts[k] for k in ("source_size_usd", "source_price", "source_slot",
                                         "source_block_time", "source_clock_provenance", "owner")}
    facts.update(basis="unknown", source_pre_atomic=None, event_unknown_reason=event_facts["unknown_reason"])
    try:
        hop = {}
        intermediate = (event_facts.get("route_intermediate_mint")
                        if event_facts["unknown_reason"] in ("non_sol_quote", "multiple_buy_events") else None)
        mint, decimals, token_program, amount, quote_out = _route_facts(tx, owner, intermediate, hop)
        # Verified final events name the target; the intermediate-leg fallback (753be289) moves
        # from the event's mint to a DIFFERENT final target by design. A quote-token curve names its
        # coin (only with MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES=live, value_source_buy).
        expected = event_facts.get("route_expected_mint")
        if (event_facts["unknown_reason"] in ("non_sol_quote_amm", "non_sol_quote", "owner_usdc_funding",
                                              "multiple_buy_events")
                and expected is not None and mint != expected):
            raise ValueError("route_target_mismatch")
        if event_facts["unknown_reason"] == "owner_usdc_funding":
            facts.update({key: event_facts[key] for key in
                          ("program_id", "pool", "event_log_index", "instruction_name") if key in event_facts})
        if hop:
            facts["route_intermediate"] = hop
        facts.update(mint=mint, token_decimals=decimals, token_program=token_program,
                     token_amount_atomic=str(amount), token_delta=amount / 10**decimals,
                     source_pre_atomic=source_pre_atomic(tx, owner, mint),
                     quote_out_atomic={q: str(v) for q, v in quote_out.items()})
        usdc_paid = quote_out[USDC]
        if event_facts["unknown_reason"] == "owner_usdc_funding":
            if quote_out[WSOL]:
                raise ValueError("route_mixed_quote_funding")
            if usdc_paid != int(event_facts["route_owner_usdc_out_atomic"]):
                raise ValueError("route_usdc_payment_mismatch")
            # Fees/unrelated transfers cannot raise the price reference and loosen a signed
            # minimum. Reuse the same call-tree payment binding as receipt initiation.
            usdc_paid, why = route_payment_evidence(tx, owner, usdc_paid)
            if usdc_paid is None:
                raise ValueError(why or "route_usdc_payment_unknown")
            facts["route_payment_atomic"] = str(usdc_paid)
        usd = usdc_paid / 1e6     # USDC counted at $1.00, as the watcher's USDC fallback does
        if quote_out[WSOL]:
            usd += quote_out[WSOL] / 1e9 * _fresh_sol_usd(sol_usd, now, facts)
        facts.update(basis="route_owner_quote_out", source_size_usd=usd,
                     source_price=usd / facts["token_delta"])
        # The post-trade price, when ONE venue leg bought the whole target amount (spec amendment
        # "routed USDC buys", 2026-09-28); a split route keeps the average and says why.
        facts.update(routed_post_facts(lambda: receipt_venue_events(tx["meta"].get("logMessages") or []),
                                       mint, amount, facts["source_price"],
                                       bound=lambda out: _coin_left_a_non_owner_account(tx, owner, mint, out)))
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as exc:
        facts["unknown_reason"] = str(exc)
    return facts


def _coin_left_a_non_owner_account(tx, owner, mint, out):
    """True when some token account NOT owned by `owner` lost exactly `out` of `mint` in this receipt:
    the pool's vault paying the bound leg (Lambda's review 3). A PumpSwap or LaunchLab event names its
    pool, not its coin, so the amount alone could bind another token's leg."""
    moved = {}
    for side, sign in (("preTokenBalances", 1), ("postTokenBalances", -1)):
        for bal in tx["meta"].get(side) or []:
            if bal.get("mint") == mint and bal.get("owner") != owner:
                i = bal.get("accountIndex")
                moved[i] = moved.get(i, 0) + sign * int(bal["uiTokenAmount"]["amount"])
    return out in moved.values()


def receipt_venue_events(logs):
    """The venue events the routed pairer decodes, from a receipt's logMessages, by the same decoders:
    CPMM swaps, LaunchLab trades and pump buy events of any actor. RAISES on a malformed event."""
    from sniper.execution.raydium_cpmm_buy import swap_events
    from sniper.execution.raydium_launchlab_buy import trade_events
    from sniper.services.memecoin_slot_signal import routed_pump_buys
    frame = {"params": {"result": {"value": {"logs": logs}}}}      # routed_pump_buys reads a logs frame
    return tuple(swap_events(logs)) + tuple(trade_events(logs)) + routed_pump_buys(frame, True)
