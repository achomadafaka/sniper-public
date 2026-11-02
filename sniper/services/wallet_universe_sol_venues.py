"""Pure Solana venue quote legs from authenticated swaps and actual vault cash.

Basis: actual signed SPL quote into the instructed pool vault, including LP fees.
Every executed target swap must bind its program, discriminator, mint, vault,
decimals and child transfers. Signer profit, routed cash and USD are never used.
Wrong/missing vault data, incomplete invocation trees, duplicate Pump events,
observed failed CPIs and unclaimed target transfers fail closed. LP instructions and
untouched router accounts are nontrades. Unsupported programs, native curves,
unknown Token-2022 transfer extensions and unauditable vault changes stay unknown.
No RPC, DB, funded imports, process state, signing or mutable module state.

Layout sources (checked 2026-10-04; discriminators bind the supported methods):
https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump_amm.json
https://github.com/MeteoraAg/dlmm-sdk/blob/main/idls/dlmm.json (0.12.0)
https://github.com/MeteoraAg/cp-amm-sdk/blob/main/src/idl/cp_amm.json (0.2.5)
https://github.com/MeteoraAg/dynamic-bonding-curve-sdk/blob/main/packages/dynamic-bonding-curve/src/idl/dynamic-bonding-curve/idl.json (0.2.1)
https://github.com/MeteoraAg/dynamic-bonding-curve/blob/main/programs/dynamic-bonding-curve/src/state/fee.rs
https://github.com/MeteoraAg/dynamic-bonding-curve/blob/main/programs/dynamic-bonding-curve/src/state/virtual_pool.rs
https://github.com/anza-xyz/agave/blob/v2.3.13/svm/src/transaction_processor.rs
https://github.com/anza-xyz/agave/blob/v2.3.13/transaction-context/src/lib.rs
https://github.com/anza-xyz/agave/blob/v2.3.13/log-collector/src/lib.rs
https://www.anchor-lang.com/docs/features/events

DBC uses the canonical self-CPI event, derived pool/vault addresses and actual
SPL transfers. Hook callbacks must bind the checked transfer and have no extra
token movement. Referral swaps and transfer-fee extensions remain unsupported.
An optional routed payment proof requires one signer funding the entire closed
route, unchanged escrow inventories, paired conversions within their immediate
CPI, and one exact final delivery. Nonzero old escrow inventory is permitted
only when it is unchanged. The protocol payer remains a PDA, never a signer.

An exact runtime "Log truncated" marker permits the independently recorded full
compiled instruction trace. The complete log prefix must match; later short
logs can be discontinuous and supply no ancestry or event evidence. A visible
runtime failure anywhere still censors independently. This does not assert every child CPI
succeeded: canonical signed event CPIs and actual vault/transfer reconciliation
still prove each selected leg. Missing trace fields, contradictions, unresolved
movements, unknown target venues and unsupported token extensions stay unknown.
"""
from __future__ import annotations

import base64
from collections import defaultdict
from dataclasses import dataclass, field
import hashlib
import re
import struct

import base58
from solders.pubkey import Pubkey

VERSION = "solana-authenticated-venue-legs-v4"
BASIS = "signed_pool_vault_quote_v1"
PUMP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_EVENT_AUTHORITY = "GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR"
DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
DAMM = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
DAMM_AUTHORITY = "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC"
DBC = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
DBC_AUTHORITY = "FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM"
DBC_EVENT_AUTHORITY = "8Ks12pbrD6PXxfty1hVQiE9sc289zgU1zHkvXhrSdriF"
DBC_SWAP_EVENT = bytes.fromhex("bd4233a826507599")
DBC_HOOK_EVENT = bytes.fromhex("863ba8785e3372e7")
TRANSFER_HOOK_EXECUTE = bytes.fromhex("692565c54bfb661a")
TOKEN_PROGRAMS = frozenset(("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"))
EVENT_IX = bytes.fromhex("e445a52e51cb9a1d")
PUMP_BUY_EVENT = bytes.fromhex("67f4521f2cf57777")
PUMP_SELL_EVENT = bytes.fromhex("3e2f370aa503dc2a")
_INVOKE = re.compile(r"Program (\S+) invoke \[(\d+)\]\Z")
_DONE = re.compile(r"Program (\S+) (success|failed:.*)\Z")


def _disc(name):
    return hashlib.sha256(("global:" + name).encode()).digest()[:8]


_SWAPS = {
    PUMP: {_disc(n): n for n in ("buy", "buy_exact_quote_in", "sell")},
    DLMM: {_disc(n): n for n in ("swap", "swap2", "swap_exact_out", "swap_exact_out2",
                                "swap_with_price_impact", "swap_with_price_impact2")},
    DAMM: {_disc(n): n for n in ("swap", "swap2")},
    DBC: {_disc(n): n for n in ("swap", "swap2", "swap2_with_transfer_hook")},
}
# Each name is an official nonswap method. Nested swap CPIs are still decoded.
_NONTRADES = {
    PUMP: frozenset(_disc(n) for n in ("deposit", "withdraw", "create_pool", "claim_coin_creator_fee")),
    DLMM: frozenset(_disc(n) for n in ("add_liquidity", "add_liquidity2", "add_liquidity_by_strategy",
        "add_liquidity_by_strategy2", "add_liquidity_by_weight", "add_liquidity_by_weight2",
        "add_liquidity_one_side", "add_liquidity_one_side_precise", "remove_liquidity",
        "remove_liquidity2", "remove_all_liquidity", "remove_liquidity_by_range",
        "remove_liquidity_by_range2", "rebalance_liquidity", "claim_fee", "claim_fee2")),
    DAMM: frozenset(_disc(n) for n in ("add_liquidity", "remove_liquidity", "initialize_pool",
        "initialize_customizable_pool", "claim_position_fee", "claim_protocol_fee2")),
    DBC: frozenset(),
}
_SYMBOLS = {
    "So11111111111111111111111111111111111111112": ("SOL", 9),
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": ("USDC", 6),
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": ("USDT", 6),
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": ("WETH", 8),
}


class SolVenueDecodeError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _fail(reason):
    raise SolVenueDecodeError(reason)


def _int(value, reason):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"0|[1-9]\d*", value):
        return int(value)
    _fail(reason)


@dataclass
class _Call:
    program: str
    accounts: list
    data: str
    height: int
    ordinal: int
    parent: object = None
    children: list = field(default_factory=list)
    events: list = field(default_factory=list)
    trace_recovered: bool = False

    def raw(self):
        try:
            if not isinstance(self.data, str):
                _fail("missing_instruction_data")
            return base58.b58decode(self.data)
        except ValueError:
            _fail("malformed_instruction_data")


def _keys_and_states(tx):
    meta = tx["meta"]
    if meta.get("err", "missing") is not None:
        _fail("failed_or_unanchored_transaction")
    message = tx["transaction"]["message"]
    keys = message["accountKeys"]
    if not isinstance(keys, list) or not keys:
        _fail("missing_account_keys")
    if isinstance(keys[0], dict):
        signers = {k["pubkey"] for k in keys if k.get("signer") is True}
        keys = [k["pubkey"] for k in keys]
    else:
        count = _int(message["header"]["numRequiredSignatures"], "invalid_signer_count")
        if not 0 < count <= len(keys):
            _fail("invalid_signer_count")
        signers = set(keys[:count])
    loaded = meta.get("loadedAddresses") or {}
    keys = keys + loaded.get("writable", []) + loaded.get("readonly", [])
    if any(not isinstance(k, str) or not k for k in keys):
        _fail("invalid_account_key")
    states, decimals = {}, {}
    for field_name, side in (("preTokenBalances", "pre"), ("postTokenBalances", "post")):
        balances = meta[field_name]
        if not isinstance(balances, list):
            _fail("missing_token_balances")
        seen = set()
        for entry in balances:
            i = _int(entry["accountIndex"], "invalid_token_account_index")
            if i >= len(keys) or i in seen:
                _fail("duplicate_or_invalid_token_account")
            seen.add(i)
            asset, owner = entry["mint"], entry["owner"]
            if not isinstance(asset, str) or not asset or not isinstance(owner, str) or not owner:
                _fail("missing_token_identity")
            dec = _int(entry["uiTokenAmount"]["decimals"], "invalid_token_decimals")
            amount = _int(entry["uiTokenAmount"]["amount"], "invalid_atomic_balance")
            if dec > 255 or decimals.get(asset, dec) != dec:
                _fail("inconsistent_token_decimals")
            decimals[asset] = dec
            identity = (asset, owner, dec, entry.get("programId"))
            state = states.setdefault(keys[i], dict(identity=identity, pre=None, post=None))
            if state["identity"] != identity:
                _fail("token_account_identity_changed")
            state[side] = amount
    return keys, signers, states


def _calls(tx, keys):
    logs = tx["meta"]["logMessages"]
    if not isinstance(logs, list) or any(not isinstance(line, str) for line in logs):
        _fail("missing_runtime_logs")
    if any((hit := _DONE.fullmatch(line)) and hit[2] != "success" for line in logs):
        _fail("observed_runtime_cpi_failure")
    markers = [i for i, line in enumerate(logs) if line == "Log truncated"]
    if len(markers) > 1:
        _fail("duplicate_truncation_marker")
    recovered = bool(markers)
    prefix = logs[:markers[0]] if recovered else logs
    if recovered:
        loaded = tx["meta"].get("loadedAddresses")
        if (not isinstance(loaded, dict) or any(not isinstance(loaded.get(k), list) for k in ("writable", "readonly"))
            or len(set(keys)) != len(keys)):
            _fail("incomplete_compiled_trace_metadata")
    def index(value, reason):
        if type(value) is not int or value < 0:
            _fail(reason)
        return value
    top = tx["transaction"]["message"]["instructions"]
    groups = tx["meta"]["innerInstructions"]
    if not isinstance(top, list) or not isinstance(groups, list):
        _fail("missing_compiled_cpis")
    inner = {}
    for group in groups:
        i = index(group["index"], "invalid_cpi_parent")
        if i >= len(top) or i in inner or not isinstance(group["instructions"], list):
            _fail("duplicate_or_invalid_cpi_parent")
        inner[i] = group["instructions"]
    calls = []
    def add(ix, height, parent):
        p = index(ix["programIdIndex"], "invalid_instruction_program")
        refs = ix["accounts"]
        if p >= len(keys) or not isinstance(refs, list):
            _fail("invalid_compiled_instruction")
        refs = [index(i, "invalid_instruction_account") for i in refs]
        if any(i >= len(keys) for i in refs):
            _fail("invalid_instruction_account")
        node = _Call(keys[p], [keys[i] for i in refs], ix["data"], height, len(calls), parent,
                     trace_recovered=recovered)
        node.raw()  # All recorded data, including unselected programs, must exist.
        calls.append(node)
        if parent is not None:
            parent.children.append(node)
        return node
    for i, ix in enumerate(top):
        stack = [add(ix, 1, None)]
        for child in inner.get(i, []):
            height = index(child["stackHeight"], "missing_cpi_stack_height")
            while stack and stack[-1].height >= height:
                stack.pop()
            if not stack or height != stack[-1].height + 1 or height > 255:
                _fail("incoherent_cpi_stack_height")
            stack.append(add(child, height, stack[-1]))
    stack, cursor = [], 0
    for line in prefix:
        if "log truncated" in line.lower():
            _fail("missing_or_truncated_runtime_logs")
        hit = _INVOKE.fullmatch(line)
        if hit:
            if cursor >= len(calls):
                _fail("runtime_cpi_count_mismatch")
            node = calls[cursor]
            if (node.program != hit[1] or node.height != int(hit[2])
                or node.parent is not (stack[-1] if stack else None)):
                _fail("runtime_cpi_identity_mismatch")
            stack.append(node)
            cursor += 1
        elif (hit := _DONE.fullmatch(line)):
            if not stack or stack[-1].program != hit[1] or hit[2] != "success":
                _fail("failed_or_incoherent_runtime_cpi")
            stack.pop()
        elif line.startswith("Program data: ") and stack and stack[-1].program in (PUMP, DBC):
            try:
                stack[-1].events.append(base64.b64decode(line[14:], validate=True))
            except ValueError:
                _fail("malformed_pump_event")
    if not recovered and (stack or cursor != len(calls)):
        _fail("runtime_cpi_count_mismatch")
    return calls


def _transfer(node, states):
    raw, a = node.raw(), node.accounts
    if not raw:
        _fail("empty_token_instruction")
    if raw[0] not in (3, 12):
        return None
    if raw[0] == 12:
        if len(raw) != 10 or len(a) < 4:
            _fail("malformed_checked_transfer")
        source, asset, destination, dec = a[0], a[1], a[2], raw[9]
    else:
        if len(raw) != 9 or len(a) < 3:
            _fail("malformed_token_transfer")
        source, destination = a[:2]
        known = states.get(source) or states.get(destination)
        if not known:
            _fail("unbound_unchecked_transfer")
        asset, _, dec, _ = known["identity"]
    for address in (source, destination):
        if address in states:
            identity = states[address]["identity"]
            if identity[0] != asset or identity[2] != dec or identity[3] not in (None, node.program):
                _fail("transfer_token_identity_mismatch")
    authority = a[3] if raw[0] == 12 else a[2]
    return dict(node=node, source=source, destination=destination, asset=asset, authority=authority,
                decimals=dec, amount=int.from_bytes(raw[1:9], "little"))


def _layout(node, name):
    a = node.accounts
    if node.program == PUMP:
        size = 21 if name == "sell" else 23
        indices = (0, (3, 4), (7, 8), 1, (5, 6), (11, 12), 16)
    elif node.program == DLMM:
        v2 = name.endswith("2")
        size = 16 if v2 else 15
        indices = (0, (6, 7), (2, 3), 10, (4, 5), (11, 12), 15 if v2 else 14)
    elif node.program == DBC:
        size = 15
        # DBC input/output accounts are ordered by direction, vaults by mint.
        indices = (2, (7, 8), (5, 6), 9, (3, 4), (10, 11), 14)
    else:
        size = 14
        indices = (1, (6, 7), (4, 5), 8, (2, 3), (9, 10), 13)
    if len(a) < size or len(node.raw()) < 24:
        _fail("unsupported_swap_layout")
    pool, m, v, actor, users, programs, self_program = indices
    if a[self_program] != node.program or any(a[i] not in TOKEN_PROGRAMS for i in programs):
        _fail("swap_program_binding_mismatch")
    if node.program == DAMM and a[0] != DAMM_AUTHORITY:
        _fail("damm_authority_mismatch")
    if node.program == DBC:
        if a[0] != DBC_AUTHORITY or a[13] != DBC_EVENT_AUTHORITY:
            _fail("dbc_authority_mismatch")
        try:
            mints = sorted((bytes(Pubkey.from_string(a[7])), bytes(Pubkey.from_string(a[8]))))
            program = Pubkey.from_string(DBC)
            pool_key = Pubkey.find_program_address([b"pool", bytes(Pubkey.from_string(a[1])),
                                                   mints[1], mints[0]], program)[0]
            vaults = [str(Pubkey.find_program_address([b"token_vault", bytes(Pubkey.from_string(a[i])),
                                                      bytes(pool_key)], program)[0]) for i in (7, 8)]
        except ValueError:
            _fail("dbc_invalid_pubkey")
        if str(pool_key) != a[2] or vaults != [a[5], a[6]]:
            _fail("dbc_pool_or_vault_binding_mismatch")
    return dict(pool=a[pool], mints=[a[i] for i in m], vaults=[a[i] for i in v],
                actor=a[actor], users=[a[i] for i in users], programs=[a[i] for i in programs])


def _pump_event(node, name, layout, amounts):
    expected = PUMP_SELL_EVENT if name == "sell" else PUMP_BUY_EVENT
    logs = [raw for raw in node.events if raw[:8] == expected]
    children = [child for child in node.children if child.program == PUMP
                and child.raw()[:8] == EVENT_IX and child.raw()[8:16] == expected]
    if (node.accounts[15] != PUMP_EVENT_AUTHORITY or len(children) != 1
        or children[0].accounts != [PUMP_EVENT_AUTHORITY]):
        _fail("pump_event_authority_binding_mismatch")
    echoes = [child.raw()[8:] for child in children]
    if (len(echoes) != 1 or len(logs) > 1 or (logs and logs[0] != echoes[0])
        or (not node.trace_recovered and len(logs) != 1)):
        _fail("missing_duplicate_or_incoherent_pump_event")
    raw = echoes[0]
    if len(raw) < 360:
        _fail("truncated_pump_swap_event")
    fields = struct.unpack_from("<13Q", raw, 16)
    identities = [base58.b58encode(raw[off:off + 32]).decode() for off in (120, 152, 184, 216)]
    if identities != [layout["pool"], layout["actor"], *layout["users"]]:
        _fail("pump_event_account_binding_mismatch")
    sign = 1 if name == "sell" else -1
    if amounts != [sign * fields[0], -sign * fields[11]] or not fields[0] or not fields[11]:
        _fail("pump_event_vault_amount_mismatch")


def _dbc_event(node, name, layout, amounts, own, timestamp):
    """Official v0.2.1 event-CPI and executed SPL legs; curve input is not cash."""
    raw = node.raw()
    hook = name == "swap2_with_transfer_hook"
    if name == "swap":
        if len(raw) != 24:
            _fail("unsupported_dbc_swap_layout")
        params = raw[8:24] + b"\x00"
    else:
        if len(raw) < 25:
            _fail("unsupported_dbc_swap_layout")
        params = raw[8:25]
        if not hook and len(raw) != 25:
            _fail("unsupported_dbc_swap_layout")
    expected = DBC_HOOK_EVENT if hook else DBC_SWAP_EVENT
    echoes = [child for child in node.children if child.program == DBC
              and child.raw()[:8] == EVENT_IX]
    if len(echoes) != 1 or echoes[0].accounts != [DBC_EVENT_AUTHORITY]:
        _fail("missing_duplicate_or_unbound_dbc_event")
    event = echoes[0].raw()[8:]
    if len(event) != 187 or event[:8] != expected or (node.events and node.events != [event]):
        _fail("unsupported_or_incoherent_dbc_event")
    if [base58.b58encode(event[o:o+32]).decode() for o in (8, 40)] != [layout["pool"], node.accounts[1]]:
        _fail("dbc_event_account_binding_mismatch")
    direction, referral = event[72:74]
    if direction not in (0, 1) or referral not in (0, 1) or event[74:91] != params:
        _fail("dbc_event_parameter_mismatch")
    included, excluded, left, output = struct.unpack_from("<4Q", event, 91)
    trading, protocol, referral_fee = struct.unpack_from("<3Q", event, 139)
    if referral or referral_fee or node.accounts[12] != DBC:
        _fail("unsupported_dbc_referral")
    # Official FeeMode: base->quote always charges output; quote->base may
    # charge input (QuoteToken) or output (OutputToken). The canonical event
    # binds either legal mode without a current-state config read. Output fees
    # stay in the output vault; output is already the actual user transfer.
    fees = trading + protocol
    input_fees = direction == 1 and included == excluded + fees
    output_fees = included == excluded and output + fees <= (1 << 64) - 1
    if not (input_fees or output_fees) or not excluded or not output:
        _fail("dbc_event_fee_reconciliation_mismatch")
    amount0, amount1, mode = struct.unpack("<QQB", params)
    if mode == 0:
        valid = included == amount0 and left == 0 and output >= amount1
    elif mode == 1:
        valid = included + left == amount0 and output >= amount1
    elif mode == 2:
        valid = output == amount0 and included <= amount1 and left == 0
    else:
        valid = False
    if not valid or struct.unpack_from("<Q", event, 179)[0] != timestamp:
        _fail("dbc_event_amount_or_clock_mismatch")
    expected_amounts = [-output, included] if direction == 1 else [included, -output]
    if amounts != expected_amounts or len(own) != 2:
        _fail("dbc_event_vault_amount_mismatch")
    input_side, output_side = (1, 0) if direction else (0, 1)
    incoming = next((t for t in own if t["destination"] == layout["vaults"][input_side]), None)
    outgoing = next((t for t in own if t["source"] == layout["vaults"][output_side]), None)
    if (not incoming or not outgoing or incoming["source"] != layout["users"][0]
        or outgoing["destination"] != layout["users"][1]
        or incoming["authority"] != layout["actor"] or outgoing["authority"] != DBC_AUTHORITY
        or incoming["amount"] != included or outgoing["amount"] != output):
        _fail("dbc_transfer_account_binding_mismatch")
    # The SPL program invokes the exact supplied hook Execute instruction. No
    # extra movement is accepted merely because the overall vault delta fits.
    hook_children = [child for t in own for child in t["node"].children]
    if hook:
        if len(raw) < 29:
            _fail("unsupported_dbc_hook_accounts")
        count = int.from_bytes(raw[25:29], "little")
        if len(raw) != 29 + 2 * count or count != 1 or raw[29] != 0:
            _fail("unsupported_dbc_hook_accounts")
        size = raw[30]
        remaining = node.accounts[15:]
        if remaining[:1] == ["Sysvar1nstructions1111111111111111111111111"]:
            remaining = remaining[1:]
        if size != 4 or len(remaining) != size or len(hook_children) != 1:
            _fail("unsupported_dbc_hook_accounts")
        callback = hook_children[0]
        transfer = outgoing if callback.parent is outgoing["node"] else incoming
        tnode = transfer["node"]
        if (tnode.raw()[0] != 12 or len(set(remaining)) != 4 or set(tnode.accounts[4:]) != set(remaining)
            or callback.program != tnode.accounts[-1] or callback.program not in remaining
            or callback.raw() != TRANSFER_HOOK_EXECUTE + struct.pack("<Q", transfer["amount"])
            or callback.accounts[:4] != tnode.accounts[:4]
            or set(callback.accounts[4:]) != set(remaining) - {callback.program} or callback.children):
            _fail("dbc_hook_transfer_binding_mismatch")
    elif hook_children:
        _fail("unsupported_dbc_transfer_hook")
    return direction


def _descends(node, ancestor):
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


def _closed_route_payment(tx, node, layout, own, transfers, calls, states, signers, keys):
    """Observed closed payment path, distinct from a protocol signer identity.

    Unknown conversion programs supply no venue identity or price. Their actual
    payment and output must be the only two relevant token transfers in the same
    immediate CPI, with persistent vaults owned by one actual counterparty.
    All inventories reconcile across the entire transaction, so a separate debit
    plus gift, mixed funding, fees in the received token or depleted old escrow
    cannot become a source proof. This grants no initiating-wallet authority.
    """
    def require(ok, reason):
        if not ok:
            _fail("routed_payment_" + reason)
    incoming = next(t for t in own if t["destination"] == layout["vaults"][1])
    delivery = next(t for t in own if t["source"] == layout["vaults"][0])
    receiver = states.get(delivery["destination"])
    buyer = receiver["identity"][1] if receiver else None
    require(buyer in signers and buyer != layout["actor"], "recipient_not_signer")
    ancestor = node.parent
    require(ancestor is not None, "missing_ancestor")
    while ancestor.parent is not None:
        ancestor = ancestor.parent
    quote_escrow = incoming["source"]
    require(states.get(quote_escrow, {}).get("identity", (None,None))[1] == layout["actor"], "quote_owner")
    target = layout["mints"][0]
    target_transfers = [t for t in transfers if t["asset"] == target and t["amount"]]
    require(target_transfers == [delivery], "extra_target_movement")
    require(sum((s["post"] or 0) - (s["pre"] or 0) for s in states.values()
                if s["identity"][0:2] == (target, buyer)) == delivery["amount"], "target_credit")
    if receiver["pre"] is None:
        # Absence is zero only after a real initialized account binding.
        initializers = [n for n in calls if n.program in TOKEN_PROGRAMS and n.raw()[:1] == b"\x12"
            and n.accounts[:2] == [delivery["destination"], target] and len(n.raw()) == 33
            and base58.b58encode(n.raw()[1:]).decode() == buyer and n.ordinal < ancestor.ordinal]
        require(len(initializers) == 1, "unproved_new_output_account")
    funders = [t for t in transfers if t["amount"] and _descends(t["node"], ancestor)
        and t["authority"] == buyer and states.get(t["source"], {}).get("identity", (None,None))[1] == buyer
        and states.get(t["destination"], {}).get("identity", (None,None))[1] == layout["actor"]]
    require(len(funders) == 1, "ambiguous_funder")
    funding = funders[0]
    payment_escrow, payment_asset = funding["destination"], funding["asset"]
    require(payment_asset not in (target, incoming["asset"]) and payment_escrow != quote_escrow,
            "unsupported_same_asset_route")
    require(funding["node"].parent is ancestor, "unbound_signer_debit")
    require(all(_descends(t["node"], ancestor) and t["authority"] == buyer for t in transfers
        if t["asset"] == payment_asset and states.get(t["source"], {}).get("identity", (None,None))[1] == buyer),
        "extra_signer_debit")
    require(sum((s["post"] or 0) - (s["pre"] or 0) for s in states.values()
                if s["identity"][0:2] == (payment_asset, buyer)) == -funding["amount"], "signer_cash_reconciliation")
    pin = [t for t in transfers if t["destination"] == payment_escrow and t["amount"]]
    pout = [t for t in transfers if t["source"] == payment_escrow and t["amount"]]
    qin = [t for t in transfers if t["destination"] == quote_escrow and t["amount"]]
    qout = [t for t in transfers if t["source"] == quote_escrow and t["amount"]]
    require(pin == [funding] and qout == [incoming] and bool(qin) and bool(pout), "foreign_funding_or_quote_use")
    require(all(t["authority"] == layout["actor"] and _descends(t["node"], ancestor) for t in pout),
            "payment_authority_or_ancestry")
    conversions, consumed, audited = [], set(), {payment_escrow, quote_escrow, funding["source"], delivery["destination"]}
    for quote in qin:
        exchange = quote["node"].parent
        require(exchange is not None and exchange is not ancestor and _descends(exchange, ancestor)
                and not _descends(exchange, node), "unpaired_quote_credit")
        outputs = states.get(quote["source"])
        counterparty = outputs["identity"][1] if outputs else None
        require(counterparty not in (None, buyer, layout["actor"]) and counterparty == quote["authority"]
                and counterparty in exchange.accounts, "conversion_output_authority")
        paired = [t for t in pout if t["node"].parent is exchange
            and states.get(t["destination"], {}).get("identity", (None,None))[1] == counterparty]
        require(len(paired) == 1, "unpaired_payment")
        pay = paired[0]
        relevant = [t for t in transfers if t["asset"] in (payment_asset, incoming["asset"])
                    and t["amount"] and _descends(t["node"], exchange)]
        require(len(relevant) == 2 and pay in relevant and quote in relevant, "ambiguous_conversion")
        require(quote["asset"] == incoming["asset"] and pay["asset"] == payment_asset,
                "conversion_units")
        consumed.add(pay["node"].ordinal)
        audited.update((quote["source"], pay["destination"]))
        conversions.append(dict(program=exchange.program, invocation=exchange.ordinal,
            counterparty=counterparty, payment_atomic=str(pay["amount"]), quote_atomic=str(quote["amount"])))
    remaining = [t for t in pout if t["node"].ordinal not in consumed]
    require(all(t["node"].parent is ancestor and states.get(t["destination"]) for t in remaining),
            "unbound_remaining_payment")
    audited.update(t["destination"] for t in remaining)
    require(sum(t["amount"] for t in pout) == funding["amount"]
            and sum(t["amount"] for t in qin) == incoming["amount"], "route_cash_reconciliation")
    for account in audited:
        s = states[account]
        require(s["post"] is not None and (s["pre"] is not None or account == delivery["destination"]),
                "missing_account_inventory")
        movements = [t for t in transfers if account in (t["source"], t["destination"]) and t["amount"]]
        require(all(_descends(t["node"], ancestor) for t in movements), "movement_outside_ancestor")
        delta = sum(t["amount"] * ((t["destination"] == account) - (t["source"] == account)) for t in movements)
        require(s["post"] - (s["pre"] or 0) == delta, "account_inventory_reconciliation")
        if account in (payment_escrow, quote_escrow):
            require(s["pre"] == s["post"], "old_escrow_depleted")
        # SPL transfer/explicit new output initialization are the whole evidence.
        allowed = {t["node"].ordinal for t in movements}
        for c in calls:
            if c.program not in TOKEN_PROGRAMS or account not in c.accounts or c.ordinal in allowed:
                continue
            require(account == delivery["destination"] and c.ordinal < ancestor.ordinal
                    and c.raw()[:1] in (b"\x12", b"\x15", b"\x16"), "unknown_token_instruction")
        if s["identity"][0] == "So11111111111111111111111111111111111111112":
            i = keys.index(account)
            require(tx["meta"]["postBalances"][i] - tx["meta"]["preBalances"][i] == delta,
                    "native_backing_reconciliation")
    # Every participating counterparty vault must change only by its paired leg.
    for q in qin:
        require(states[q["source"]]["post"] - states[q["source"]]["pre"] == -q["amount"],
                "conversion_quote_inventory")
    for p in pout:
        require(states[p["destination"]]["post"] - states[p["destination"]]["pre"] == p["amount"],
                "conversion_payment_inventory")
    return dict(version="solana-closed-route-payment-v1", signer=buyer, actor_verified=False,
        protocol_payer=layout["actor"], ancestor_program=ancestor.program, ancestor_invocation=ancestor.ordinal,
        ancestor_data_sha256=hashlib.sha256(ancestor.raw()).hexdigest(), received_mint=target,
        received_atomic=str(delivery["amount"]), payment=dict(asset=payment_asset,
            decimals=funding["decimals"], amount=str(funding["amount"])),
        quote=dict(asset=incoming["asset"], decimals=incoming["decimals"], amount=str(incoming["amount"])),
        conversions=conversions, other_payment_atomic=str(sum(t["amount"] for t in remaining)),
        escrow_inventory="unchanged", initiating_wallet_verified=False)


def decode_transaction(tx, mint):
    """Return all authenticated target swaps, or raise a typed evidence gap.

    Actor verification means an actual transaction signer owns both instructed
    user accounts. It is used only for proved-source-address exclusions; a PDA or
    opaque routed recipient never becomes an asserted wallet owner.
    """
    try:
        return _decode_transaction(tx, mint)
    except SolVenueDecodeError:
        raise
    except (KeyError, TypeError, IndexError, AttributeError, struct.error) as exc:
        raise SolVenueDecodeError("malformed_venue_transaction") from exc


def _decode_transaction(tx, mint):
    keys, signers, states = _keys_and_states(tx)
    calls = _calls(tx, keys)
    provenance = ("compiled_instruction_trace_v1" if any(n.trace_recovered for n in calls)
                  else "runtime_and_compiled_instruction_trace_v1")
    target_accounts = {a for a, s in states.items() if s["identity"][0] == mint}
    target_refs = target_accounts | {mint}
    target_calls = [n for n in calls if n.program in TOKEN_PROGRAMS and target_refs.intersection(n.accounts)]
    if not target_calls:
        if all(s["pre"] == s["post"] for a, s in states.items() if a in target_accounts):
            return dict(version=VERSION, basis=BASIS, status="nontrade", swaps=[], evidence_provenance=provenance)
        _fail("unresolved_target_balance_change")
    operations = {}
    for node in calls:
        if node.program not in _SWAPS:
            continue
        raw = node.raw()
        if raw[:8] in _SWAPS[node.program]:
            operations[node.ordinal] = ("swap", _SWAPS[node.program][raw[:8]])
        elif raw[:8] in _NONTRADES[node.program]:
            operations[node.ordinal] = ("nontrade", None)
    def operation_for(node):
        parent = node.parent
        while parent is not None:
            if parent.ordinal in operations:
                return parent
            parent = parent.parent
        return None
    transfers = []
    for node in calls:
        if node.program not in TOKEN_PROGRAMS:
            continue
        transfer = _transfer(node, states)
        if transfer:
            transfers.append(transfer)
            if transfer["asset"] == mint and transfer["amount"] and node.parent is not None and operation_for(node) is None:
                _fail("unsupported_target_venue")
        elif target_refs.intersection(node.accounts) and node.raw()[0] in (7, 8, 14, 15, 26):
            parent = operation_for(node)
            if node.parent is not None and (parent is None or operations[parent.ordinal][0] != "nontrade"):
                _fail("unsupported_target_token_extension")
    swaps, audit_vaults = [], set()
    for node in calls:
        operation = operations.get(node.ordinal)
        if not operation or operation[0] != "swap":
            continue
        layout = _layout(node, operation[1])
        if mint not in layout["mints"]:
            continue
        if layout["mints"][0] == layout["mints"][1] or len(set(layout["vaults"])) != 2:
            _fail("self_valued_or_duplicate_vault")
        decimals = []
        authority = {DAMM:DAMM_AUTHORITY, DBC:DBC_AUTHORITY}.get(node.program, layout["pool"])
        for asset, vault, program in zip(layout["mints"], layout["vaults"], layout["programs"]):
            state = states.get(vault)
            if not state or state["pre"] is None or state["post"] is None:
                _fail("missing_persistent_vault_balance")
            identity = state["identity"]
            if identity[0] != asset or identity[1] != authority or identity[3] not in (None, program):
                _fail("venue_vault_identity_mismatch")
            decimals.append(identity[2])
        own = [t for t in transfers if operation_for(t["node"]) is node]
        amounts = [sum(t["amount"] * ((t["destination"] == v) - (t["source"] == v))
                       for t in own if t["asset"] == asset) for asset, v in zip(layout["mints"], layout["vaults"])]
        if not amounts[0] or not amounts[1] or amounts[0] * amounts[1] >= 0:
            _fail("zero_or_same_direction_venue_legs")
        if node.program == PUMP:
            _pump_event(node, operation[1], layout, amounts)
        direction = None
        if node.program == DBC:
            direction = _dbc_event(node, operation[1], layout, amounts, own, tx["blockTime"])
        side = layout["mints"].index(mint)
        asset, dec = layout["mints"][1 - side], decimals[1 - side]
        symbol, trusted_decimals = _SYMBOLS.get(asset, (None, dec))
        if dec != trusted_decimals:
            _fail("wrong_quote_decimals")
        actor_verified = layout["actor"] in signers and all(
            states.get(a, {}).get("identity", (None, None))[1] == layout["actor"] for a in layout["users"])
        swap = dict(protocol={PUMP:"pumpswap", DLMM:"meteora_dlmm", DAMM:"meteora_damm_v2", DBC:"meteora_dbc"}[node.program],
            program=node.program, pool=layout["pool"], actor=layout["actor"], actor_verified=actor_verified,
            invocation=node.ordinal, mint_delta_atomic=str(amounts[side]),
            quote=dict(asset=asset, symbol=symbol, decimals=dec, amount=str(amounts[1 - side])),
            basis=BASIS, evidence_provenance=provenance)
        if node.program == DBC and direction == 1 and side == 0 and not actor_verified:
            try:
                swap["economic_source_proof"] = _closed_route_payment(
                    tx, node, layout, own, transfers, calls, states, signers, keys)
            except SolVenueDecodeError as exc:
                swap["economic_source_gap"] = exc.reason
            except (KeyError, TypeError, IndexError, ValueError) as exc:
                swap["economic_source_gap"] = "routed_payment_missing_or_malformed_inventory"
        swaps.append(swap)
        audit_vaults.update(layout["vaults"])
    for vault in audit_vaults:
        actual = states[vault]["post"] - states[vault]["pre"]
        accounted = sum(t["amount"] * ((t["destination"] == vault) - (t["source"] == vault)) for t in transfers)
        if actual != accounted:
            _fail("unresolved_vault_balance_change")
    return dict(version=VERSION, basis=BASIS, status="trade" if swaps else "nontrade", swaps=swaps,
                evidence_provenance=provenance)
