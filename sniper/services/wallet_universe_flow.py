"""Research-only ±60-second quote flow around an exact source transaction.

``call(method, params)`` returns a JSON-RPC result and owns transport, budgets and
provider isolation. These collectors perform no network, database or execution
operations. State is serializable and belongs to one observation and exclusions.
Positive quote flow means quote paid INTO a venue; sells are negative. Raw atomic
units are authoritative. No price conversion, trader impact or causation is inferred.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import re

from sniper.services.memecoin_evm_v3_events import V3_SWAP, PANCAKE_V3_SWAP, valid_pancake_v3_data
from sniper.services.memecoin_evm_v4_events import V4_SWAP, decode_v4_log
from sniper.services.wallet_universe_sol_venues import (
    BASIS as SOL_FLOW_BASIS, VERSION as SOL_VENUE_VERSION,
    SolVenueDecodeError, decode_transaction as decode_sol_venues,
)
from sniper.util.clock import real_utc_now_iso
from sniper.util.state_paths import state_path

METHOD = "wallet-universe-flow-v7"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
WETH = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"
SOL_QUOTES = {USDC: ("USDC", 6), USDT: ("USDT", 6), WETH: ("WETH", 8), WSOL: ("SOL", 9)}
SPL_TOKEN_PROGRAMS = frozenset(("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                                "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"))
V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
SOLIDLY_SWAP = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"
PONS_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
PONS_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
SWAP_TOPICS = (V2_SWAP, SOLIDLY_SWAP, V3_SWAP, PANCAKE_V3_SWAP, V4_SWAP, PONS_BUY, PONS_SELL)
NATIVE_EVM = "0x" + "00" * 20
# Named persisted watcher protocol data. Importing the funded watcher/relay
# executor would bring live machinery into this research-only module.
RELAY_KYBER_PROOF_VERSION = "relay-kyber-linked-v1"
RELAY_ENVELOPE_PROOF_VERSION = "relay-envelope-linked-v1"
RELAY_OTHER_TARGET_PROOF_VERSION = "relay-envelope-other-target-v1"
RELAY_ORIGIN_PROOF_VERSION = "relay-origin-paid-v1"
RELAY_BOUND_PROOF_VERSION = "relay-origin-bound-v1"
RELAY_ROUTER = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"
RELAY_APPROVAL_PROXY = "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_HEX = re.compile(r"0x[0-9a-fA-F]+\Z")


class _Invalid(ValueError):
    pass


class _Stopped(Exception):
    pass


def _now():
    # Resolve state-root after the CLI has configured it, never at module import.
    return real_utc_now_iso(path=state_path("data", "clock_offset.json"))


def _integer(value, name, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _Invalid(name)
    try:
        parsed = int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)
    except ValueError as exc:
        raise _Invalid(name) from exc
    if parsed < minimum or (isinstance(value, str) and str(parsed) != value and not _HEX.fullmatch(value)):
        raise _Invalid(name)
    return parsed


def _hex(value, size, name):
    if not isinstance(value, str) or len(value) != 2 + size * 2 or not _HEX.fullmatch(value):
        raise _Invalid(name)
    return value.lower()


def _unit(unit, solana=False):
    if not isinstance(unit, dict):
        raise _Invalid("invalid_unit")
    needed = ("chain", "uid", "address", "mint", "signature")
    if any(not isinstance(unit.get(k), str) or not unit[k] for k in needed):
        raise _Invalid("missing_identity")
    identity = {k: unit[k] for k in needed}
    identity["t0"] = _integer(unit.get("t0"), "invalid_t0")
    if solana:
        if identity["chain"] != "solana":
            raise _Invalid("wrong_chain")
    else:
        _integer(identity["chain"], "invalid_chain", minimum=1)
        for k in ("address", "mint"):
            identity[k] = _hex(identity[k], 20, "invalid_" + k)
        identity["signature"] = _hex(identity["signature"], 32, "invalid_signature")
    return identity


def _start(identity, state, exclusions, own, **extra):
    binding = dict(identity=identity, exclusions=sorted(set(exclusions)), own=sorted(set(own)), **extra)
    if state is not None:
        if not isinstance(state, dict) or state.get("binding") != binding or state.get("version") != METHOD:
            raise _Invalid("state_identity_mismatch")
        return copy.deepcopy(state)
    return dict(version=METHOD, binding=binding, cursor=None, pages=0, records=[], seen={},
                source=None, complete=False, nontrades=0, undecodable=0, errors=[], issues=[],
                before_complete=True, before_detail=None, last_collect=None)


def _read(call, state, method, params):
    state["calls"] = state.get("calls", 0) + 1
    try:
        return call(method, params)
    except Exception as exc:
        # The injected adapter supplies typed, sanitized budget/provider reasons.
        detail = getattr(exc, "code", None) or getattr(exc, "reason", None) or str(exc) or type(exc).__name__
        raise _Stopped(str(detail)[:160]) from exc


def _page(value):
    if not isinstance(value, dict) or not isinstance(value.get("data"), list) or "paginationToken" not in value:
        raise _Stopped("malformed_page")
    token = value["paginationToken"]
    if token is not None and (not isinstance(token, str) or not token):
        raise _Stopped("malformed_pagination")
    return value["data"], token


def _decode_issue(state, order, detail, signature=None):
    state["undecodable"] += 1
    state.setdefault("issues", []).append(dict(kind="decode", order=list(order), detail=detail,
                                               signature=signature))
    state["errors"].append(detail)


def _completion(state):
    source = state.get("source")
    if not source:
        return "censored", "source_not_verified"
    issues = state.get("issues", [])
    # Older/unanchored failures cannot acquire an order retrospectively.
    if (state["undecodable"] != len(issues) or len(state["errors"]) != len(issues)
            or any(issue["kind"] != "decode" for issue in issues)):
        return "censored", "unanchored_or_source_evidence_gap"
    prior = [issue for issue in issues if tuple(issue["order"]) < tuple(source["order"])]
    if prior:
        state.update(before_complete=False, before_detail="pre_window_undecodable")
    if len(prior) != len(issues):
        return "censored", "post_window_undecodable"
    return "complete", "descriptive_quote_flow"


def _result(state, status, detail, *, scope, owner_exclusion, calls):
    quotes = {}
    source = state.get("source")
    if source:
        for rec in state["records"]:
            side = "before" if tuple(rec["order"]) < tuple(source["order"]) else "after"
            for leg in rec["legs"]:
                asset, decimals = leg["asset"], leg["decimals"]
                if asset in quotes and quotes[asset]["decimals"] != decimals:
                    status, detail = "censored", "quote_decimals_changed"
                    continue
                q = quotes.setdefault(asset, dict(asset=asset, symbol=leg["symbol"], decimals=decimals,
                                                  before=0, after=0, swaps=0))
                q[side] += int(leg["amount"])
                q["swaps"] += 1
    output = []
    for q in sorted(quotes.values(), key=lambda q: q["asset"]):
        before = q.pop("before")
        after = q.pop("after")
        q.update(net_before_atomic=str(before) if state["before_complete"] else None,
                 net_after_atomic=str(after),
                 delta_atomic=str(after - before) if state["before_complete"] else None)
        output.append(q)
    result = dict(method=METHOD, state=state, status=status, detail=detail, quotes=output,
                source_found=bool(source), source=source, source_proof=state.get("source_proof"),
                research_source_proof=state.get("research_source_proof"),
                cursor=state.get("cursor"),
                pages=state["pages"], calls=state.get("calls", 0) - calls,
                total_calls=state.get("calls", 0), last_collect=state["last_collect"],
                computed_at=_now(),
                nontrades=state["nontrades"], undecodable=state["undecodable"],
                scope=scope, owner_exclusion=owner_exclusion,
                owner_exclusion_detail=("Only proved transaction signers or closed-route payment signers are excluded; "
                                        "unproved routed payers remain unattributed."
                                        if scope == "mint_transactions" else "Only source and own transaction identities are excluded."),
                flow_basis=(SOL_FLOW_BASIS if scope == "mint_transactions" else "signed_registered_venue_quote_v1"),
                quote_accounting=("Signed quote into authenticated pool vaults, including LP fees. "
                                  "Each mint retains its exact atomic units; unrelated tips and routed cash are excluded."
                                  if scope == "mint_transactions" else
                                  "Signed quote into registered venues. Event maker is not asserted to be the transaction owner."),
                before_complete=state["before_complete"], before_detail=state["before_detail"],
                after_complete=state["complete"] and status == "complete" and bool(source),
                errors=list(state["errors"]))
    # Publish compact windows while the single selected decoder history is resident.
    result.update(horizons=flow_horizons(result), source_position=(source or {}).get('position_evidence'))
    return result


def flow_horizons(result):
    """Project archived quote flow; clocks/units stay exact, unknown stays unknown."""
    windows = [dict(seconds=n, state="unknown" if n <= 60 else "not_captured",
                    quotes=None, verified_buyer_count=None, buyers_complete=False)
               for n in (5, 10, 15, 30, 60, 120, 300)]
    output = dict(method="wallet-flow-horizons-v1", windows=windows,
                  detail="incomplete_window")
    state = result.get("state") or {}
    if (result.get("method") != METHOD or result.get("status") != "complete"
            or result.get("after_complete") is not True or state.get("complete") is not True):
        return output
    try:
        source = state["source"]
        t0 = _integer(source["t0"], "source_clock_missing")
        records = []
        solana = result["scope"] == "mint_transactions"
        for rec in state["records"]:
            if tuple(rec["order"]) <= tuple(source["order"]):
                continue
            if solana:
                anchor = state["seen"][rec["signature"]]
                if anchor[1:] != rec["order"]:
                    raise _Invalid("record_clock_identity_mismatch")
                timestamp = anchor[0]
            else:
                timestamp = state["headers"][str(rec["order"][0])]["timestamp"]
            delay = _integer(timestamp, "record_clock_missing") - t0
            if not 0 <= delay <= 60:
                raise _Invalid("record_clock_outside_window")
            for leg in rec["legs"]:
                if not isinstance(leg["amount"], str) or str(int(leg["amount"])) != leg["amount"]:
                    raise _Invalid("invalid_quote_quantity")
                _integer(leg["decimals"], "invalid_quote_decimals")
            records.append((delay, rec))
        for window in windows[:5]:
            quotes, buyers, unattributed = {}, set(), False
            for delay, rec in records:
                if delay > window["seconds"]:
                    continue
                for leg in rec["legs"]:
                    q = quotes.setdefault(leg["asset"], dict(asset=leg["asset"], symbol=leg["symbol"],
                        decimals=leg["decimals"], buy=0, sell=0, swaps=0))
                    if q["decimals"] != leg["decimals"]:
                        raise _Invalid("quote_decimals_changed")
                    amount = int(leg["amount"])
                    q["buy" if amount > 0 else "sell"] += abs(amount)
                    q["swaps"] += 1
                    if amount > 0:
                        buyer = rec.get("payment_signer") or (rec.get("actor") if rec.get("actor_verified") is True else None)
                        if solana and buyer:
                            buyers.add(buyer)
                        else:
                            unattributed = True
            rows = []
            for q in sorted(quotes.values(), key=lambda q: q["asset"]):
                buy, sell = q.pop("buy"), q.pop("sell")
                rows.append(dict(q, buy_atomic=str(buy), sell_atomic=str(sell), net_after_atomic=str(buy-sell)))
            window.update(state="complete", quotes=rows, verified_buyer_count=len(buyers) if solana else None,
                          buyers_complete=solana and not unattributed)
        output["detail"] = "descriptive_quote_flow"
    except (KeyError, TypeError, ValueError):
        for window in windows[:5]:
            window.update(state="unknown", quotes=None, verified_buyer_count=None, buyers_complete=False)
        output["detail"] = "record_clock_or_units_unavailable"
    return output


def _signers_keys(tx):
    message = tx["transaction"]["message"]
    keys = message["accountKeys"]
    if not isinstance(keys, list) or not keys:
        raise _Invalid("missing_account_keys")
    if isinstance(keys[0], dict):
        if any(not isinstance(k, dict) for k in keys):
            raise _Invalid("invalid_parsed_account_keys")
        signers = [k["pubkey"] for k in keys if k.get("signer") is True]
        keys = [k["pubkey"] for k in keys]
    else:
        count = _integer(message["header"]["numRequiredSignatures"], "invalid_signer_count", minimum=1)
        if count > len(keys):
            raise _Invalid("invalid_signer_count")
        signers = keys[:count]
    loaded = tx["meta"].get("loadedAddresses") or {}
    if not isinstance(loaded, dict):
        raise _Invalid("invalid_loaded_addresses")
    if not isinstance(loaded.get("writable", []), list) or not isinstance(loaded.get("readonly", []), list):
        raise _Invalid("invalid_loaded_addresses")
    keys = keys + loaded.get("writable", []) + loaded.get("readonly", [])
    if any(not isinstance(k, str) or not k for k in keys):
        raise _Invalid("invalid_account_key")
    return signers, keys


def _sol_mint_untouched(tx, mint, keys, accounts):
    """Prove a router merely carried unused mint accounts, despite other swaps.

    Unchanged balances alone cannot exclude an arbitrage round trip. Every
    executed SPL instruction must also omit the mint and its known accounts,
    with a complete compiled CPI list corroborated by runtime invocation logs.
    """
    target = {i for i, (_, asset, before, after) in accounts.items() if asset == mint}
    if not target or any(accounts[i][2] != accounts[i][3] for i in target):
        return False
    target.update(i for i, key in enumerate(keys) if key == mint)
    try:
        top = tx["transaction"]["message"]["instructions"]
        inner = tx["meta"]["innerInstructions"]
        logs = tx["meta"]["logMessages"]
        if (not isinstance(top, list) or not top or not isinstance(inner, list)
                or not isinstance(logs, list) or any(not isinstance(line, str) or
                "log truncated" in line.lower() for line in logs)):
            return False
        instructions = list(top)
        groups = set()
        for group in inner:
            index = _integer(group["index"], "invalid_cpi_parent")
            if index >= len(top) or index in groups or not isinstance(group["instructions"], list):
                return False
            groups.add(index)
            instructions.extend(group["instructions"])
        token_calls = 0
        for instruction in instructions:
            program_index = _integer(instruction["programIdIndex"], "invalid_instruction_program")
            refs = instruction["accounts"]
            if program_index >= len(keys) or not isinstance(refs, list):
                return False
            refs = [_integer(i, "invalid_instruction_account") for i in refs]
            if any(i >= len(keys) for i in refs):
                return False
            if keys[program_index] in SPL_TOKEN_PROGRAMS:
                token_calls += 1
                if target.intersection(refs):
                    return False
        invoked = sum(bool(re.fullmatch(r"Program (\S+) invoke \[\d+\]", line))
                      and line.split()[1] in SPL_TOKEN_PROGRAMS for line in logs)
        succeeded = sum(line in {"Program " + p + " success" for p in SPL_TOKEN_PROGRAMS} for line in logs)
        return token_calls > 0 and invoked == succeeded == token_calls
    except (KeyError, TypeError, _Invalid):
        return False


def _sol_trade(tx, mint, *, diagnostics=None):
    """Signer quote deltas, with the established fee/rent/WSOL corrections.

    No script import: dump_forensics changes a shared provider's pacing on import
    and converts quotes to SOL using a fixed ETH price. Only its pure accounting
    rule is retained here, with integer amounts and explicit decode blockers.
    """
    def unsupported(detail):
        if diagnostics is not None:
            diagnostics["detail"] = detail
        return "undecodable", None

    meta = tx["meta"]
    signers, keys = _signers_keys(tx)
    deltas, decimals, accounts = {}, {}, {}
    for field, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        balances = meta.get(field)
        if not isinstance(balances, list):
            raise _Invalid("missing_token_balances")
        for b in balances:
            if not isinstance(b, dict):
                raise _Invalid("invalid_token_balance")
            owner, asset = b.get("owner"), b.get("mint")
            if not isinstance(owner, str) or not owner or not isinstance(asset, str) or not asset:
                raise _Invalid("missing_token_owner")
            ui = b["uiTokenAmount"]
            amount = _integer(ui["amount"], "invalid_atomic_balance")
            dec = _integer(ui["decimals"], "invalid_decimals")
            if dec > 255 or (asset in decimals and decimals[asset] != dec):
                raise _Invalid("inconsistent_decimals")
            decimals[asset] = dec
            key = (owner, asset)
            deltas[key] = deltas.get(key, 0) + sign * amount
            index = _integer(b["accountIndex"], "missing_account_index")
            if index >= len(keys):
                raise _Invalid("invalid_account_index")
            account = accounts.setdefault(index, [owner, asset, 0, 0])
            if account[:2] != [owner, asset]:
                raise _Invalid("token_account_identity_changed")
            account[2 if sign < 0 else 3] = amount
    swap_log = any(re.search(r"\b(?:Swap\w*|Buy\w*|Sell\w*)\b", str(line), re.I)
                   for line in meta.get("logMessages", []))
    movers = [s for s in signers if deltas.get((s, mint), 0)]
    if not movers:
        if not swap_log or _sol_mint_untouched(tx, mint, keys, accounts):
            return "nontrade", None
        return unsupported("no_signer_mint_delta")
    if len(movers) != 1:
        return unsupported("ambiguous_signer_mint_delta")
    trader = movers[0]
    coin = deltas[(trader, mint)]
    legs = []
    for asset in (USDC, USDT, WETH):
        amount = deltas.get((trader, asset), 0)
        if asset != mint and amount * coin < 0:
            symbol, dec = SOL_QUOTES[asset]
            if decimals[asset] != dec:
                raise _Invalid("wrong_quote_decimals")
            legs.append(dict(asset=asset, symbol=symbol, decimals=dec, amount=str(-amount)))
    if not legs and mint != WSOL:
        if any(owner == trader and asset not in SOL_QUOTES and asset != mint and amount * coin < 0
               for (owner, asset), amount in deltas.items()):
            return unsupported("unsupported_quote_asset")
        others = sum(amount for (owner, asset), amount in deltas.items() if asset == WSOL and owner != trader)
        asset = WSOL if WSOL in decimals else "native"
        if others:
            amount = -others
        else:
            pre, post = meta.get("preBalances"), meta.get("postBalances")
            if not isinstance(pre, list) or not isinstance(post, list) or len(pre) != len(keys) or len(post) != len(keys):
                raise _Invalid("missing_native_balances")
            pre = [_integer(v, "invalid_native_balance") for v in pre]
            post = [_integer(v, "invalid_native_balance") for v in post]
            i = keys.index(trader)
            amount = deltas.get((trader, WSOL), 0) + post[i] - pre[i]
            if keys[0] == trader:
                amount += _integer(meta.get("fee"), "missing_fee")
            for j, (owner, token, pre_amount, post_amount) in accounts.items():
                if owner != trader or token not in (mint, WSOL):
                    continue
                if pre[j] == 0 < post[j]:
                    amount += post[j] - (post_amount if token == WSOL else 0)
                elif post[j] == 0 < pre[j]:
                    amount -= pre[j] - (pre_amount if token == WSOL else 0)
        if amount * coin < 0:
            if WSOL in decimals and decimals[WSOL] != 9:
                raise _Invalid("wrong_quote_decimals")
            legs.append(dict(asset=asset, symbol="SOL", decimals=9, amount=str(-amount)))
    if not legs:
        unknown_quote = any(owner == trader and asset != mint and amount * coin < 0
                            for (owner, asset), amount in deltas.items())
        if swap_log or unknown_quote:
            return unsupported("unsupported_quote_asset" if unknown_quote else "swap_quote_unresolved")
        return "nontrade", None
    return "trade", dict(trader=trader, buy=coin > 0, legs=legs)


def _sol_source_receipt(tx, mint, address):
    """Exact signer-owned net acquisition; quote pricing never gates identity.

    The venue decoder separately authenticates the actual actor and pool buy.
    A routed PDA, transfer-only airdrop or buy/sell net-zero transaction cannot
    acquire source-wallet attribution from this balance check.
    """
    signers, _ = _signers_keys(tx)
    if address not in signers:
        return False
    net = 0
    for field, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for balance in tx["meta"][field]:
            if balance.get("owner") == address and balance.get("mint") == mint:
                net += sign * _integer(balance["uiTokenAmount"]["amount"], "invalid_source_mint_balance")
    return net > 0


def _sol_position_evidence(tx, mint, address):
    # Touched accounts can prove holdings, never wallet-wide absence.
    amounts, decimals = {}, set()
    for field in ("preTokenBalances", "postTokenBalances"):
        relevant = [b for b in tx["meta"][field] if b.get("owner") == address and b.get("mint") == mint]
        indices = [_integer(b["accountIndex"], "invalid_source_account_index") for b in relevant]
        if len(set(indices)) != len(indices):
            raise _Invalid("duplicate_source_balance")
        amounts[field] = sum(_integer(b["uiTokenAmount"]["amount"], "invalid_source_balance") for b in relevant)
        decimals.update(_integer(b["uiTokenAmount"]["decimals"], "invalid_source_decimals") for b in relevant)
    if len(decimals) != 1 or max(decimals) > 255:
        raise _Invalid("source_decimals_changed")
    pre, post = amounts["preTokenBalances"], amounts["postTokenBalances"]
    evidence = dict(signature=tx["transaction"]["signatures"][0], mint=mint, address=address,
                    pre=tx["meta"]["preTokenBalances"], post=tx["meta"]["postTokenBalances"])
    return dict(state="add_on" if pre > 0 else "unknown", scope="transaction_accounts",
                visible_pre_balance_atomic=str(pre), net_received_atomic=str(post-pre), decimals=next(iter(decimals)),
                evidence_digest=hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest())


def collect_solana(unit, call, *, state=None, excluded_addresses=(), own_signatures=(), max_pages=10):
    """Collect dense full mint transactions; resume only after completed pages.

    The source must be a successful buy by its proved address at exactly ``t0``.
    Same-second flow uses (slot, transactionIndex); no slot or source RPC is needed.
    A first-history probe distinguishes a launch with no full pre-window. Set
    ``unit['probe_age']=False`` only when coin age is already known or deliberately
    unknown; the result labels that age unknown rather than inferring launch age.
    """
    identity = _unit(unit, True)
    excluded = set(excluded_addresses) | {identity["address"]}
    own = set(own_signatures)
    state = _start(identity, state, excluded, own, probe_age=unit.get("probe_age", True),
                   venue_version=SOL_VENUE_VERSION, flow_basis=SOL_FLOW_BASIS)
    start_calls = state.get("calls", 0)
    scope = dict(scope="mint_transactions", owner_exclusion="proved_source_addresses_and_own_transactions")
    finish = lambda status, detail: _result(state, status, detail, calls=start_calls, **scope)
    limit = min(_integer(max_pages, "invalid_page_limit", minimum=1), 10)
    t0 = identity["t0"]
    try:
        try:
            started = datetime.fromisoformat(_now())
            if started.tzinfo is None:
                raise ValueError("naive clock")
        except (TypeError, ValueError):
            raise _Stopped("collection_clock_unavailable")
        # The inclusive integer blockTime endpoint is an entire second.
        if started.timestamp() < t0 + 61:
            return finish("partial", "waiting_after_window")
        if unit.get("probe_age", True) and not state.get("age_probed"):
            data, _ = _page(_read(call, state, "getTransactionsForAddress", [identity["mint"],
                            dict(transactionDetails="signatures", sortOrder="asc", limit=1,
                                 commitment="confirmed", filters=dict(status="succeeded"))]))
            if not data or not isinstance(data[0], dict):
                raise _Stopped("coin_age_unavailable")
            birth = _integer(data[0].get("blockTime"), "invalid_coin_birth")
            if birth > t0:
                raise _Invalid("coin_birth_after_source")
            state.update(age_probed=True, birth_ts=birth)
            if birth > t0 - 60:
                state.update(before_complete=False, before_detail="no_pre_window")
        elif not unit.get("probe_age", True):
            state["age_detail"] = "coin_age_unknown"
        for _ in range(limit):
            if state["complete"]:
                break
            opts = dict(transactionDetails="full", sortOrder="asc", limit=1000, encoding="json",
                        maxSupportedTransactionVersion=1, commitment="confirmed",
                        filters=dict(status="succeeded", blockTime=dict(gte=t0 - 60, lte=t0 + 60)))
            if state["cursor"]:
                opts["paginationToken"] = state["cursor"]
            rows, token = _page(_read(call, state, "getTransactionsForAddress", [identity["mint"], opts]))
            if token and (token == state["cursor"] or not rows):
                raise _Stopped("pagination_did_not_advance")
            # Commit the page only after all envelopes validate. A provider interruption
            # or malformed envelope cannot move past evidence we have not accounted for.
            pending = copy.deepcopy(state)
            for tx in rows:
                if not isinstance(tx, dict) or not isinstance(tx.get("meta"), dict) or "err" not in tx["meta"]:
                    raise _Stopped("malformed_transaction_envelope")
                if tx["meta"]["err"] is not None:
                    raise _Stopped("provider_returned_failed_transaction")
                try:
                    signatures = tx["transaction"]["signatures"]
                    if not isinstance(signatures, list) or not signatures or any(not isinstance(x, str) or not x for x in signatures):
                        raise _Invalid("missing_transaction_signature")
                    timestamp = _integer(tx.get("blockTime"), "missing_block_time")
                    order = [_integer(tx.get("slot"), "missing_slot"),
                             _integer(tx.get("transactionIndex"), "missing_transaction_index")]
                    if not t0 - 60 <= timestamp <= t0 + 60:
                        raise _Invalid("outside_requested_window")
                    sig = signatures[0]
                    anchor = [timestamp, *order]
                    if sig in pending["seen"]:
                        if pending["seen"][sig] != anchor:
                            raise _Invalid("transaction_identity_changed")
                        continue
                    if pending.get("last_order") and tuple(anchor) <= tuple(pending["last_order"]):
                        raise _Invalid("nonascending_transaction_order")
                    pending["seen"][sig] = anchor
                    pending["last_order"] = anchor
                    is_source = identity["signature"] in signatures
                    if not is_source and (own.intersection(signatures)):
                        continue
                    try:
                        decoded = decode_sol_venues(tx, identity["mint"])
                        if is_source:
                            source_received = _sol_source_receipt(tx, identity["mint"], identity["address"])
                    except (KeyError, TypeError, _Invalid, SolVenueDecodeError) as exc:
                        if is_source:
                            raise _Invalid("source_transaction_undecodable") from exc
                        _decode_issue(pending, order, str(exc) or "malformed_transaction", sig)
                        continue
                    if is_source:
                        source_buy = any(s["actor_verified"] and s["actor"] == identity["address"]
                                         and int(s["mint_delta_atomic"]) < 0 for s in decoded["swaps"])
                        routed = [s["economic_source_proof"] for s in decoded["swaps"]
                            if int(s["mint_delta_atomic"]) < 0 and s.get("economic_source_proof", {}).get("signer") == identity["address"]
                            and s["economic_source_proof"].get("received_mint") == identity["mint"]]
                        if not source_buy and len(routed) == 1:
                            source_buy = True
                            pending["source_proof"] = dict(routed[0], kind="solana_routed_economic_payment")
                        if timestamp != t0 or not source_received or not source_buy:
                            raise _Invalid("source_clock_or_buy_identity_mismatch")
                        pending["source"] = dict(signature=identity["signature"], t0=t0, order=order,
                                                 evidence_provenance=decoded["evidence_provenance"],
                                                 position_evidence=_sol_position_evidence(tx, identity["mint"], identity["address"]))
                        continue
                    if decoded["status"] == "nontrade":
                        pending["nontrades"] += 1
                    else:
                        for swap in decoded["swaps"]:
                            if swap["actor_verified"] and swap["actor"] in excluded:
                                continue
                            payment = swap.get("economic_source_proof", {})
                            payment_signer = (payment.get("signer") if int(swap["mint_delta_atomic"]) < 0
                                and payment.get("received_mint") == identity["mint"] else None)
                            if payment_signer in excluded:
                                continue
                            pending["records"].append(dict(signature=sig, order=order,
                                invocation=swap["invocation"], protocol=swap["protocol"],
                                pool=swap["pool"], actor=swap["actor"], actor_verified=swap["actor_verified"],
                                payment_signer=payment_signer, evidence_provenance=swap["evidence_provenance"],
                                legs=[swap["quote"]]))
                except (KeyError, TypeError, _Invalid) as exc:
                    pending["undecodable"] += 1
                    pending["errors"].append(str(exc) or "malformed_transaction")
                    pending.setdefault("issues", []).append(dict(kind="anchor", order=None,
                                                                 detail=str(exc) or "malformed_transaction"))
            pending["pages"] += 1
            pending["last_collect"] = _now()
            pending["cursor"] = token
            pending["complete"] = token is None
            state = pending
        if not state["complete"]:
            return finish("partial", "page_limit")
        return finish(*_completion(state))
    except _Stopped as exc:
        return finish("partial", str(exc))
    except _Invalid as exc:
        state["errors"].append(str(exc))
        return finish("censored", str(exc))


def _pool_metadata(pools, mint):
    result = []
    for raw in pools:
        p = dict(raw)
        protocol = str(p.get("protocol", "")).lower()
        p["protocol"] = protocol
        p["pool"] = _hex(p.get("pool"), 32 if protocol == "v4" else 20, "invalid_pool")
        for side in (0, 1):
            p["token" + str(side)] = _hex(p.get("token" + str(side)), 20, "invalid_pool_token")
            p["token" + str(side) + "_decimals"] = _integer(p.get("token" + str(side) + "_decimals"), "invalid_pool_decimals")
            if p["token" + str(side) + "_decimals"] > 255:
                raise _Invalid("invalid_pool_decimals")
        q = p.get("quote_side")
        if q in ("token0", "token1"):
            q = int(q[-1])
        q = _integer(q, "invalid_quote_side")
        if q not in (0, 1) or p["token" + str(1 - q)] != mint or p["token" + str(q)] == mint:
            raise _Invalid("pool_coin_identity_mismatch")
        p["quote_side"] = q
        if protocol == "v4":
            p["manager"] = _hex(p.get("manager"), 20, "missing_v4_manager")
        if protocol not in ("v2", "v3", "v4", "solidly", "aerodrome", "pancake_v3", "pons", "pons_v2", "pons_v1"):
            raise _Invalid("unsupported_pool_protocol")
        result.append(p)
    if not result:
        raise _Invalid("no_verified_pools")
    if len({p["pool"] for p in result}) != len(result):
        raise _Invalid("duplicate_pool_metadata")
    return sorted(result, key=lambda p: p["pool"])


def _header(call, state, number, *, refresh=False):
    key = str(number)
    if not refresh and key in state["headers"]:
        return state["headers"][key]
    raw = _read(call, state, "eth_getBlockByNumber", [hex(number), False])
    if not isinstance(raw, dict):
        raise _Stopped("block_header_unavailable")
    if _integer(raw.get("number"), "missing_header_number") != number:
        raise _Invalid("wrong_block_header")
    header = dict(number=number, timestamp=_integer(raw.get("timestamp"), "missing_header_time"),
                  hash=_hex(raw.get("hash"), 32, "missing_header_hash"))
    previous = state["headers"].get(key)
    if previous and previous != header:
        raise _Invalid("block_reorg")
    state["headers"][key] = header
    return header


def _boundary(call, state, timestamp, low, high, *, inclusive):
    # First block >= lower endpoint, and first block > upper endpoint.
    while low < high:
        mid = (low + high) // 2
        ts = _header(call, state, mid)["timestamp"]
        if ts < timestamp or (not inclusive and ts == timestamp):
            low = mid + 1
        else:
            high = mid
    return low


def _near_boundary(call, state, timestamp, source_block, head, *, inclusive):
    """Bracket from the source, doubling 128-block steps before exact search.

    No block-rate estimate: dense and sparse chains retain the same timestamp
    predicate, including repeated timestamps at both window endpoints.
    """
    step = 128
    if inclusive:
        right = source_block
        while True:
            left = max(0, source_block - step)
            if _header(call, state, left)["timestamp"] < timestamp:
                return _boundary(call, state, timestamp, left + 1, right, inclusive=True)
            if left == 0:
                return 0
            right = left
            step *= 2
    left = source_block
    while True:
        right = min(head, source_block + step)
        if _header(call, state, right)["timestamp"] > timestamp:
            return _boundary(call, state, timestamp, left + 1, right, inclusive=False)
        if right == head:
            return head + 1
        left = right
        step *= 2


def _words(log, count):
    raw = log.get("data")
    if not isinstance(raw, str) or not re.fullmatch("0x[0-9a-fA-F]{" + str(64 * count) + "}", raw):
        raise _Invalid("malformed_swap_data")
    return [int(raw[i:i + 64], 16) for i in range(2, len(raw), 64)]


def _signed(value):
    return value - (1 << 256) if value >= (1 << 255) else value


def _receipt_log_anchor(log, identity, block, index, block_hash):
    if (log.get("removed") is not False
            or _hex(log.get("transactionHash"), 32, "missing_receipt_log_hash") != identity["signature"]
            or _hex(log.get("blockHash"), 32, "missing_receipt_log_block_hash") != block_hash
            or _integer(log.get("blockNumber"), "missing_receipt_log_block") != block
            or _integer(log.get("transactionIndex"), "missing_receipt_log_order") != index):
        raise _Invalid("source_receipt_log_anchor_mismatch")
    _integer(log.get("logIndex"), "missing_receipt_log_index")


def _indexed_address(topic):
    word = _hex(topic, 32, "invalid_transfer_address")
    if int(word[2:26], 16):
        raise _Invalid("invalid_transfer_address")
    return "0x" + word[-40:]


def _mint_delivery(identity, receipt, block, index, block_hash):
    net, router_in, seen = 0, 0, set()
    for log in receipt["logs"]:
        if str(log.get("address") or "").lower() != identity["mint"]:
            continue
        topics = log.get("topics")
        if not isinstance(topics, list) or not topics:
            raise _Invalid("malformed_mint_transfer_log")
        if _hex(topics[0], 32, "invalid_mint_transfer_topic") != TRANSFER_TOPIC:
            continue
        if len(topics) != 3:
            raise _Invalid("malformed_mint_transfer_log")
        _receipt_log_anchor(log, identity, block, index, block_hash)
        log_index = _integer(log["logIndex"], "invalid_mint_transfer_index")
        if log_index in seen:
            raise _Invalid("duplicate_mint_transfer_log")
        seen.add(log_index)
        source, recipient = _indexed_address(topics[1]), _indexed_address(topics[2])
        amount = _words(log, 1)[0]
        if recipient == identity["address"]:
            net += amount
            if source == RELAY_ROUTER:
                router_in += amount
        if source == identity["address"]:
            net -= amount
    return net, router_in


def _source_attribution(unit, identity, receipt, block, index, block_hash):
    """Bind beneficiary-only watcher evidence; never infer a relay initiator.

    The persisted watcher already checked the Relay envelope, payment and code
    identity. Its stripped proof is accepted only with the exact observation
    binding plus current canonical logs showing the router delivered the mint.
    The caller additionally requires a real registered-pool quote-in buy.
    """
    sender = _hex(receipt.get("from"), 20, "missing_source_sender")
    if unit.get("source_slot") is not None and _integer(unit["source_slot"], "invalid_source_slot") != block:
        raise _Invalid("source_slot_identity_mismatch")
    if sender == identity["address"]:
        net, _ = _mint_delivery(identity, receipt, block, index, block_hash)
        if net <= 0:
            raise _Invalid("source_direct_mint_delivery_unproved")
        return dict(kind="direct_transaction_sender", protocol="direct_evm_transaction",
                    transaction_sender=sender, beneficiary_net_atomic=str(net))
    proof = unit.get("source_initiation")
    if not isinstance(proof, dict):
        raise _Invalid("source_sender_identity_mismatch")
    version = proof.get("proof_version")
    versions = (RELAY_KYBER_PROOF_VERSION, RELAY_ENVELOPE_PROOF_VERSION,
                RELAY_OTHER_TARGET_PROOF_VERSION, RELAY_ORIGIN_PROOF_VERSION, RELAY_BOUND_PROOF_VERSION)
    if (type(proof.get("schema_version")) is not int or proof["schema_version"] != 1
            or version not in versions or identity["chain"] not in ("4663", "56", "8453")
            or version == RELAY_KYBER_PROOF_VERSION and identity["chain"] != "4663"
            or proof.get("network_id") != identity["chain"]
            or _hex(proof.get("tx_hash"), 32, "invalid_relay_tx_hash") != identity["signature"]
            or _hex(proof.get("block_hash"), 32, "invalid_relay_block_hash") != block_hash
            or _integer(proof.get("block_number"), "invalid_relay_block_number") != block
            or _hex(proof.get("mint"), 20, "invalid_relay_mint") != identity["mint"]
            or _hex(proof.get("beneficiary"), 20, "invalid_relay_beneficiary") != identity["address"]
            or proof.get("source_user_id") != identity["uid"]):
        raise _Invalid("source_relay_proof_identity_mismatch")
    payer = _hex(proof.get("payer"), 20, "invalid_relay_payer")
    if payer == NATIVE_EVM or _hex(receipt.get("to"), 20, "missing_relay_proxy") != RELAY_APPROVAL_PROXY:
        raise _Invalid("source_relay_envelope_mismatch")
    net, router_in = _mint_delivery(identity, receipt, block, index, block_hash)
    if net <= 0 or router_in <= 0:
        raise _Invalid("source_relay_beneficiary_transfer_unproved")
    return dict(kind="relay_beneficiary_purchase", protocol="relay_permit2_envelope",
                proof_version=version, transaction_sender=sender, beneficiary=identity["address"],
                payer=payer, mint=identity["mint"], beneficiary_net_atomic=str(net),
                initiating_wallet_verified=False,
                evidence="persisted_watcher_proof_and_canonical_router_transfer_and_registered_pool_buy")


def _evm_amounts(chain, log, pool):
    topics = log.get("topics")
    if not isinstance(topics, list) or not topics:
        raise _Invalid("missing_swap_topics")
    topics = [_hex(topic, 32, "invalid_swap_topic") for topic in topics]
    head = topics[0]
    if head not in SWAP_TOPICS:
        return None
    protocol = pool["protocol"]
    if protocol == "v4":
        if head != V4_SWAP:
            raise _Invalid("pool_protocol_mismatch")
        _words(log, 6)
        row = decode_v4_log(chain, log, None)
        if row is None or row["pool"] != pool["pool"]:
            raise _Invalid("v4_manager_or_pool_mismatch")
        amounts = int(row["amount0"]), int(row["amount1"])
    elif head in (V2_SWAP, SOLIDLY_SWAP) and protocol in ("v2", "solidly", "aerodrome"):
        w = _words(log, 4)
        amounts = w[0] - w[2], w[1] - w[3]
    elif head in (V3_SWAP, PANCAKE_V3_SWAP) and protocol in ("v3", "pancake_v3", "pons_v1"):
        w = _words(log, 7 if head == PANCAKE_V3_SWAP else 5)
        if head == PANCAKE_V3_SWAP and not valid_pancake_v3_data(log["data"]):
            raise _Invalid("malformed_pancake_swap")
        amounts = _signed(w[0]), _signed(w[1])
    elif head in (PONS_BUY, PONS_SELL) and protocol in ("pons", "pons_v2"):
        w = _words(log, 4)
        if pool["quote_side"] != 0:
            raise _Invalid("pons_quote_identity_mismatch")
        amounts = (w[0], -w[1]) if head == PONS_BUY else (-w[1], w[0])
    else:
        raise _Invalid("pool_protocol_mismatch")
    if amounts[0] * amounts[1] >= 0:
        raise _Invalid("swap_legs_not_opposite")
    return amounts


def _upper_bracket_proven(state, identity):
    """True when a completed EVM range stores the header of the block right after its upper bound,
    stamped strictly after t0+60: the proof that no later block belongs to the window."""
    try:
        nxt = (state.get("headers") or {}).get(str(int(state["to_block"]) + 1))
        return bool(nxt) and int(nxt["timestamp"]) > int(identity["t0"]) + 60
    except (KeyError, TypeError, ValueError):
        return False


def collect_evm(unit, call, *, pools, state=None, excluded_addresses=(), own_signatures=()):
    """Whole ±60-second registered-pool flow, with canonical source order.

    Pool metadata must already be verified by the caller. The EVM event maker is
    commonly a router, so address aliases are deliberately NOT used as owners.
    Source and known own transaction hashes are excluded across every pool. Logs
    are fetched in bounded 400-block pages; headers and completed pages survive
    injected budget stops. Incomplete provider reads never become quiet-market zero.
    """
    identity = _unit(unit)
    pool_meta = _pool_metadata(pools, identity["mint"])
    own = {_hex(sig, 32, "invalid_own_signature") for sig in own_signatures}
    if isinstance(state, dict) and state.get("complete") and not _upper_bracket_proven(state, identity):
        # A range completed before the strict-maturity rule (Codex night review W6) may have sealed at
        # a head stamped exactly t0+60. Without the next block's header proving it is past the window,
        # the completed range is not trusted: collect afresh. Valid completed caches are untouched.
        state = None
    state = _start(identity, state, (), own, pools=pool_meta,
                   source_initiation=copy.deepcopy(unit.get("source_initiation")),
                   source_slot=unit.get("source_slot"))
    start_calls = state.get("calls", 0)
    state.setdefault("headers", {})
    state.setdefault("pool_index", 0)
    state.setdefault("transaction_order", {})
    previous_source = state["source"]
    state["source"] = None
    state["source_proof"] = None
    scope = dict(scope="registered_pools", owner_exclusion="source_transaction_only")
    finish = lambda status, detail: _result(state, status, detail, calls=start_calls, **scope)
    try:
        receipt = _read(call, state, "eth_getTransactionReceipt", [identity["signature"]])
        if not isinstance(receipt, dict):
            raise _Stopped("source_receipt_unavailable")
        if _hex(receipt.get("transactionHash"), 32, "missing_source_hash") != identity["signature"]:
            raise _Invalid("source_transaction_hash_mismatch")
        _hex(receipt.get("from"), 20, "missing_source_sender")
        if _integer(receipt.get("status"), "missing_source_status") != 1:
            raise _Invalid("source_transaction_failed")
        block = _integer(receipt.get("blockNumber"), "missing_source_block")
        index = _integer(receipt.get("transactionIndex"), "missing_source_index")
        h = _header(call, state, block, refresh=True)
        if h["timestamp"] != identity["t0"] or h["hash"] != _hex(receipt.get("blockHash"), 32, "missing_source_block_hash"):
            raise _Invalid("source_clock_or_block_hash_mismatch")
        verified = dict(signature=identity["signature"], t0=identity["t0"], order=[block, index])
        if previous_source and previous_source != verified:
            raise _Invalid("source_order_changed")
        receipt_logs = receipt.get("logs")
        if not isinstance(receipt_logs, list):
            raise _Invalid("missing_source_receipt_logs")
        source_pool_seen = False
        for p in pool_meta:
            emitter = p.get("manager", p["pool"])
            for row in receipt_logs:
                if not isinstance(row, dict):
                    raise _Invalid("malformed_source_receipt_log")
                if str(row.get("address", "")).lower() != emitter:
                    continue
                if p["protocol"] == "v4" and (row.get("topics") or [None, None])[1:2] != [p["pool"]]:
                    continue
                amounts = _evm_amounts(identity["chain"], row, p)
                if amounts is not None:
                    _receipt_log_anchor(row, identity, block, index, h["hash"])
                    if amounts[p["quote_side"]] <= 0:
                        continue
                    source_pool_seen = True
        if not source_pool_seen:
            raise _Invalid("source_buy_not_in_registered_pools")
        effective_unit=unit
        research=None
        if (unit.get("source_initiation") is None
                and str(receipt.get("from","")).lower()!=identity["address"]
                and str(receipt.get("to","")).lower()==RELAY_APPROVAL_PROXY):
            from sniper.services.wallet_universe_relay import recover_beneficiary, RelayResearchError
            try:
                research=recover_beneficiary(unit,receipt,
                    lambda method,params:_read(call,state,method,params),
                    state=state.setdefault("research_relay_state",{}))
            except RelayResearchError as exc:
                if exc.retryable:
                    raise _Stopped(exc.reason) from exc
                raise _Invalid(exc.reason) from exc
            state["research_source_proof"]=research
            # The original observation/proof-None state binding remains immutable.
            effective_unit={**unit,"source_initiation":research["source_initiation"]}
        state["source_proof"] = _source_attribution(effective_unit, identity, receipt, block, index, h["hash"])
        if research:
            state["source_proof"].update(code_basis="receipt_block",beneficiary_only=True,
                third_party_payment_possible=True,research_version=research["version"],
                evidence="research_exact_block_code_and_envelope_and_canonical_router_transfer_and_registered_pool_buy")
        state["source"] = verified
        if state["complete"]:
            if not state.get("source_log_seen"):
                return finish("censored", "source_missing_from_log_window")
            return finish(*_completion(state))
        head = _integer(_read(call, state, "eth_blockNumber", []), "invalid_chain_head")
        # STRICTLY after the window (Codex W6): a head stamped exactly t0+60 can still be followed by
        # another block with that same timestamp, which belongs to the window but would be cut off by a
        # completed range forever.
        if head < block or _header(call, state, head, refresh=True)["timestamp"] <= identity["t0"] + 60:
            return finish("partial", "waiting_after_window")
        if "from_block" not in state:
            state["from_block"] = _near_boundary(call, state, identity["t0"] - 60, block, head, inclusive=True)
        if "to_block" not in state:
            upper = _near_boundary(call, state, identity["t0"] + 60, block, head, inclusive=False) - 1
            state["to_block"] = upper
        while state["pool_index"] < len(pool_meta):
            p = pool_meta[state["pool_index"]]
            lo = state["cursor"] if state["cursor"] is not None else state["from_block"]
            hi = min(lo + 399, state["to_block"])
            opts = dict(fromBlock=hex(lo), toBlock=hex(hi), address=p.get("manager", p["pool"]),
                        topics=[[V4_SWAP], p["pool"]] if p["protocol"] == "v4" else [list(SWAP_TOPICS)])
            rows = _read(call, state, "eth_getLogs", [opts])
            if not isinstance(rows, list):
                raise _Stopped("malformed_log_page")
            pending = copy.deepcopy(state)
            for row in rows:
                if not isinstance(row, dict):
                    raise _Stopped("malformed_log_envelope")
                try:
                    if row.get("removed") is not False:
                        raise _Invalid("removed_or_unanchored_log")
                    b = _integer(row.get("blockNumber"), "missing_log_block")
                    i = _integer(row.get("transactionIndex"), "missing_log_transaction_index")
                    li = _integer(row.get("logIndex"), "missing_log_index")
                    sig = _hex(row.get("transactionHash"), 32, "missing_log_transaction_hash")
                    bh = _hex(row.get("blockHash"), 32, "missing_log_block_hash")
                    if not lo <= b <= hi or _hex(row.get("address"), 20, "missing_log_emitter") != opts["address"]:
                        raise _Invalid("foreign_log_in_filtered_page")
                    # Completed header reads remain durable even if a later read stops.
                    header = _header(call, state, b)
                    pending["headers"] = copy.deepcopy(state["headers"])
                    pending["calls"] = state.get("calls", 0)
                    if header["hash"] != bh or not identity["t0"] - 60 <= header["timestamp"] <= identity["t0"] + 60:
                        raise _Invalid("log_block_hash_or_time_mismatch")
                    if p["protocol"] == "v4" and (row.get("topics") or [None, None])[1:2] != [p["pool"]]:
                        raise _Invalid("foreign_v4_pool")
                    transaction_order = str(b) + ":" + str(i)
                    if pending["transaction_order"].get(transaction_order, sig) != sig:
                        raise _Invalid("conflicting_transaction_order")
                    pending["transaction_order"][transaction_order] = sig
                    key = sig + ":" + str(li)
                    anchor = [b, i, bh, row.get("data"), row.get("topics")]
                    if key in pending["seen"]:
                        if pending["seen"][key] != anchor:
                            raise _Invalid("log_identity_changed")
                        continue
                    pending["seen"][key] = anchor
                    if sig == identity["signature"]:
                        if [b, i] != verified["order"]:
                            raise _Invalid("source_log_order_mismatch")
                        if _evm_amounts(identity["chain"], row, p) is not None:
                            pending["source_log_seen"] = True
                        continue
                    if sig in own:
                        continue
                    try:
                        amounts = _evm_amounts(identity["chain"], row, p)
                    except (KeyError, TypeError, _Invalid) as exc:
                        _decode_issue(pending, [b, i], str(exc) or "malformed_swap", sig)
                        continue
                    if amounts is None:
                        pending["nontrades"] += 1
                        continue
                    q = p["quote_side"]
                    asset = p["token" + str(q)]
                    pending["records"].append(dict(signature=sig, order=[b, i],
                        legs=[dict(asset="native" if asset == NATIVE_EVM else asset,
                                   symbol=p.get("quote_symbol") or p.get("token" + str(q) + "_symbol"),
                                   decimals=p["token" + str(q) + "_decimals"], amount=str(amounts[q]))]))
                except (KeyError, TypeError, _Invalid) as exc:
                    pending["undecodable"] += 1
                    pending["errors"].append(str(exc) or "malformed_log")
                    pending.setdefault("issues", []).append(dict(kind="anchor", order=None,
                                                                 detail=str(exc) or "malformed_log"))
            pending["pages"] += 1
            pending["last_collect"] = _now()
            if hi == state["to_block"]:
                pending["pool_index"] += 1
                pending["cursor"] = None
            else:
                pending["cursor"] = hi + 1
            state = pending
        state["complete"] = True
        if not state.get("source_log_seen"):
            return finish("censored", "source_missing_from_log_window")
        return finish(*_completion(state))
    except _Stopped as exc:
        return finish("partial", str(exc))
    except (KeyError, TypeError, _Invalid) as exc:
        state["errors"].append(str(exc) or "malformed_evm_data")
        return finish("censored", str(exc) or "malformed_evm_data")
