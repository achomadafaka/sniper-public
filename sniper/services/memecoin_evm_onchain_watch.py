"""Pure receipt classification for direct Robinhood wallet detection."""
from __future__ import annotations

import re
import threading
import time

from sniper.services.memecoin_source_initiation import direct_receipt_initiation, relay_receipt_initiation


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def profile_evm_address(profile):
    """Return the profile's direct EVM address, or None when the profile cannot prove one."""
    value = profile.get("evmAddress") if isinstance(profile, dict) else None
    return value.lower() if isinstance(value, str) and _ADDRESS.fullmatch(value) else None


def _topic_address(value):
    value = str(value or "")
    return "0x" + value[-40:].lower() if len(value) >= 42 else None


def _integer(value):
    try:
        return int(str(value or "0"), 0)
    except (TypeError, ValueError):
        return 0


def wallet_paid_token_flows(receipt, wallet, quote, transaction=None, *, payment_details=None):
    """Return net acquisition and payment between the wallet and the same counterparty."""
    if str((receipt or {}).get("status") or "").lower() not in {"1", "0x1", "true"}:
        return {}, 0
    wallet, quote = str(wallet).lower(), str(quote).lower()
    net, counterparties = {}, {}
    for log in receipt.get("logs") or []:
        topics = log.get("topics") or []
        if len(topics) != 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
            continue
        token = str(log.get("address") or "").lower()
        sender, receiver = _topic_address(topics[1]), _topic_address(topics[2])
        amount = _integer(log.get("data"))
        if not _ADDRESS.fullmatch(token) or amount <= 0 or sender == receiver:
            continue
        if wallet not in (sender, receiver):
            continue
        party, delta = (sender, amount) if receiver == wallet else (receiver, -amount)
        net[token] = net.get(token, 0) + delta
        key = (token, party)
        counterparties[key] = counterparties.get(key, 0) + delta
    payees = {party for (token, party), delta in counterparties.items() if token == quote and delta < 0}
    native = 0
    transaction = transaction or {}
    if str(transaction.get("from") or "").lower() == wallet:
        native = max(0, _integer(transaction.get("value")))
        native_to = str(transaction.get("to") or "").lower()
        if native and _ADDRESS.fullmatch(native_to):
            payees.add(native_to)
        else:
            native = 0
    acquired, linked_payees = {}, set()
    for token, total in net.items():
        if token == quote or total <= 0:
            continue
        sources = {party for (mint, party), delta in counterparties.items()
                   if mint == token and delta > 0 and party in payees}
        supported = sum(counterparties[(token, party)] for party in sources)
        if supported:
            acquired[token] = min(total, supported)
            linked_payees.update(sources)
    quote_paid = min(max(0, -net.get(quote, 0)),
                     sum(max(0, -counterparties.get((quote, party), 0)) for party in linked_payees))
    native_paid = native if native and native_to in linked_payees else 0
    spent = max(native_paid, quote_paid)
    if payment_details is not None:
        payment_details.update(native_paid=native_paid, quote_paid=quote_paid,
            unlinked_quote_outflow=sum(max(0, -delta) for (token, party), delta in counterparties.items()
                                      if token == quote and party not in linked_payees))
    return (acquired, spent) if spent > 0 else ({}, 0)


def direct_buy_from_receipt(receipt, transaction, wallet, *, weth, quote_usd, decimals_fn, chain=None,
                            quote_decimals=18):
    """Return one wallet-paid ERC-20 buy from a confirmed receipt, else None."""
    wallet = str(wallet or "").lower()
    weth = str(weth or "").lower()
    if not _ADDRESS.fullmatch(wallet) or not _ADDRESS.fullmatch(weth):
        return None
    try:
        quote_usd = float(quote_usd)
    except (TypeError, ValueError):
        return None
    if quote_usd <= 0:
        return None
    payment = {}
    received, spent = wallet_paid_token_flows(receipt, wallet, weth, transaction, payment_details=payment)
    if spent <= 0 or len(received) != 1:
        return None
    if payment.get("native_paid") and payment.get("quote_paid"):
        return None  # A single-payment price cannot authenticate two charged currencies.
    mint, raw_amount = next(iter(received.items()))
    try:
        decimals = int(decimals_fn(mint))
    except (TypeError, ValueError):
        return None
    if decimals < 0:
        return None
    amount = raw_amount / (10 ** decimals)
    if amount <= 0:
        return None
    if type(quote_decimals) is not int or not 0 <= quote_decimals <= 36:
        return None
    payment_decimals = 18 if payment.get("native_paid") else quote_decimals
    source_size_usd = spent / 10 ** payment_decimals * quote_usd
    if source_size_usd <= 0:
        return None
    initiation = direct_receipt_initiation(receipt, transaction, wallet, mint, chain=chain)
    result = {"mint": mint, "token_delta": amount, "source_size_usd": source_size_usd,
              "source_price": source_size_usd / amount, "source_initiation": initiation}
    if payment.get("unlinked_quote_outflow"):
        result["source_fill_reason"] = "unexplained_quote_outflow"
    elif initiation:
        result.update(payment_token="0x" + "00" * 20 if payment.get("native_paid") else weth,
                      payment_atomic=str(spent), acquired_atomic=str(raw_amount),
                      payment_decimals=payment_decimals, token_decimals=decimals,
                      payment_basis="transaction_value_upper_bound" if payment.get("native_paid")
                                    else "wallet_quote_outflow",
                      receipt_proof_version=initiation["proof_version"])
    return result


RELAY_KYBER_PROOF_VERSION = 'relay-kyber-linked-v1'
# ROUTE-AGNOSTIC RELAY PROOF (2026-09-28, operator order: the RH fast lane). The same Relay Permit2
# envelope carries every J7-proved Axiom buy on 4663 and 56, but only a Kyber-routed swap emits the
# `Swapped` event the kyber proof requires: 13/25 real 4663 buys and 19/30 real 56 buys carry none.
# The envelope proof keeps every other check and drops only that one. Measured 2026-09-28: the Relay
# proxy and router on 56 and 8453 carry the same code hashes as on 4663; the Kyber router on 56 does
# not, which is why the envelope proof never asks for Kyber at all.
RELAY_ENVELOPE_PROOF_VERSION = 'relay-envelope-linked-v1'
# A swap call aimed somewhere other than the adapter (0x Settler, AllowanceHolder, the Kyber router
# called directly): 4 of 51 J7 4663 fills since 09-21 (2026-09-28, operator order).
RELAY_OTHER_TARGET_PROOF_VERSION = 'relay-envelope-other-target-v1'
KYBER_ROUTER_4663 = '0x6131b5fae19ea4f9d964eac0408e4408b66337b5'
KYBER_CODE_HASH_4663 = '0xdc6eb20a6d4701d8f0f04f9a3342d254eb2698bbad281d8578d6efba21865867'
RELAY_ADAPTER_4663 = '0x039ec98a76f111092d4751365ff09dd2aec301e8'
PERMIT2_TYPES = ['address', '((address,uint256)[],uint256,uint256)',
                 '(address,bool,uint256,bytes)[]', 'address', 'address', 'bytes', 'bytes']


def relay_envelope_chains():
    """The chains whose route-agnostic Relay envelope decode runs (MEMECOIN_EVM_RELAY_ENVELOPE_CHAINS,
    call-time: 4663, 56, 8453 and, since 2026-10-17, 1)."""
    from sniper.settings import memecoin as cfg
    return cfg.memecoin_evm_relay_envelope_chains()


def relayer_purchase_from_receipt(receipt, transaction, wallet, *, chain, contracts_verified=False,
                                  any_route=False, preconfirmed=False):
    """Decode the supported Relay Permit2 and Kyber route. Unknown evidence returns None.

    `any_route` accepts any swap step behind the adapter (RELAY_ENVELOPE_PROOF_VERSION). Default off:
    the address verifier keeps the kyber-only proof it was calibrated on.

    `preconfirmed` (Base 8453 only, spec 2026-10-09-memecoin-base-flashblocks-detection-design.md): a
    Flashblocks preconfirmed read, whose tx block hash is null and whose receipt and logs carry 0x00..0
    until the block seals. Then the tx and the receipt bind by block number and transaction index; two
    real hashes must still be equal, and every other check is unchanged.
    """
    from eth_abi import decode, encode
    from eth_abi.exceptions import DecodingError
    from eth_utils import keccak
    from sniper.execution.evm_relay_quote import RELAY_APPROVAL_PROXY, RELAY_ROUTER

    def topic(signature):
        return '0x' + keccak(text=signature).hex()

    def same_block(txn, rcpt):
        if txn['blockNumber'] != rcpt['blockNumber']:
            return False
        if not (preconfirmed and str(chain) == '8453'):
            return txn['blockHash'] == rcpt['blockHash']
        from sniper.services.memecoin_base_preconf import real_hash
        mined, sealed = real_hash(txn['blockHash']), real_hash(rcpt['blockHash'])
        if mined is None and sealed is None:
            return txn['transactionIndex'] == rcpt['transactionIndex']
        return mined is not None and mined == sealed

    def address(value):
        value = str(value).lower()
        if not _ADDRESS.fullmatch(value) or int(value, 16) == 0:
            raise ValueError('invalid address')
        return value

    def indexed(value):
        if len(value) != 66 or not value.startswith('0x') or int(value[2:26], 16):
            raise ValueError('invalid indexed address')
        return '0x' + value[-40:].lower()

    def abi(types, value, *, trailer=False):
        raw = bytes.fromhex(value[2:]) if isinstance(value, str) else value
        values = decode(types, raw)
        canonical = encode(types, values)
        if raw != canonical and not (trailer and len(raw) == len(canonical) + 32 and raw.startswith(canonical)):
            raise ValueError('noncanonical ABI')
        return values

    try:
        chains = relay_envelope_chains() if any_route else ('4663',)
        if str(chain) not in chains or not contracts_verified or _integer(receipt['status']) != 1:
            return None
        wallet = address(wallet)
        tx_hash = transaction['hash'].lower()
        if (not re.fullmatch(r'0x[0-9a-f]{64}', tx_hash)
                or tx_hash != receipt['transactionHash'].lower()
                or not same_block(transaction, receipt)
                or address(transaction['to']) != RELAY_APPROVAL_PROXY
                or address(receipt['to']) != RELAY_APPROVAL_PROXY
                or address(transaction['from']) != address(receipt['from'])
                or _integer(transaction.get('value')) != 0):
            return None
        raw = transaction['input']
        if not raw.startswith('0x0a2b8f36'):
            return None
        payer, permit, calls, refund, nft, metadata, signature = abi(PERMIT2_TYPES, bytes.fromhex(raw[10:]), trailer=True)
        permitted, _nonce, deadline = permit
        if (refund != wallet or nft != wallet or len(signature) not in (64, 65)
                or len(permitted) != 1 or deadline <= 0 or len(calls) != 3
                or any(fail or value for _target, fail, value, _data in calls)):
            return None
        payment, paid = permitted[0]
        payer, payment = address(payer), address(payment)
        if paid <= 0 or payer == wallet:
            return None
        approve, swap, cleanup = calls
        adapter = swap[0] == RELAY_ADAPTER_4663 and swap[3][:4].hex() == '998b5942'
        # OTHER SWAP TARGETS (2026-09-28): only the watcher's route-agnostic decode, and only
        # proven by the token flows below, never by the target's address.
        other = any_route and not adapter
        if (approve[0] != payment or approve[3][:4].hex() != '095ea7b3'
                or not (adapter or other)
                or cleanup[0] != RELAY_ROUTER or cleanup[3][:4].hex() != '9bb43718'):
            return None
        spender, allowance = abi(['address', 'uint256'], approve[3][4:])
        tokens, recipients, minima, cleanup_meta = abi(
            ['address[]', 'address[]', 'uint256[]', 'bytes'], cleanup[3][4:])
        if (spender != swap[0] or allowance != paid or len(tokens) != 1
                or recipients != (wallet,) or len(minima) != 1 or cleanup_meta != metadata):
            return None
        mint = address(tokens[0])
        if mint == payment:
            return None
        swap_topic = topic('Swapped(address,address,address,address,uint256,uint256)')
        funds_topic = topic('FundsMovement(address,address,address,uint256,bytes)')
        net, legs, swaps, movements = {}, {}, [], []
        seen_indexes = set()
        for log in receipt['logs']:
            if (log.get('removed') or log['transactionHash'].lower() != tx_hash
                    or log['blockHash'] != receipt['blockHash']):
                return None
            index = _integer(log['logIndex'])
            if index in seen_indexes:
                return None
            seen_indexes.add(index)
            emitter = address(log['address'])
            topics = log['topics']
            if not topics:
                continue
            if emitter == KYBER_ROUTER_4663 and topics[0] == swap_topic:
                if len(topics) != 1:
                    return None
                swaps.append(abi(['address'] * 4 + ['uint256'] * 2, log['data']))
            elif emitter in (RELAY_APPROVAL_PROXY, RELAY_ROUTER) and topics[0] == funds_topic:
                if len(topics) != 1:
                    return None
                movements.append((emitter, *abi(['address'] * 3 + ['uint256', 'bytes'], log['data'])))
            elif topics[0] == TRANSFER_TOPIC:
                if len(topics) != 3:
                    return None
                sender, receiver = indexed(topics[1]), indexed(topics[2])
                amount, = abi(['uint256'], log['data'])
                if not amount or sender == receiver:
                    continue
                for party, delta in ((sender, -amount), (receiver, amount)):
                    key = (emitter, party)
                    net[key] = net.get(key, 0) + delta
                key = (emitter, sender, receiver)
                legs[key] = legs.get(key, 0) + amount
        if any_route:
            # The output is what the router paid the wallet; the `required` legs below tie it to the
            # adapter, and the net checks tie the payment to the payer.
            amount_out = legs.get((mint, RELAY_ROUTER, wallet), 0)
            if amount_out <= 0 or amount_out < minima[0]:
                return None
        else:
            if len(swaps) != 1:
                return None
            sender, token_in, token_out, recipient, amount_in, amount_out = swaps[0]
            if (sender != swap[0] or recipient != swap[0] or token_in != payment
                    or token_out != mint or amount_in != paid or amount_out <= 0
                    or amount_out < minima[0]):
                return None
        if other:
            # The tokens do not pass through an other target (Kyber pays through an executor, the
            # Settler and AllowanceHolder through pools), so there are no fixed target legs. What
            # must hold instead: the wallet's OWN delta is the router's cleanup leg and nothing
            # else, the wallet never touches the payment token, and the router is a pure
            # pass-through of both tokens (in == out == the declared amounts).
            into_router = sum(v for (token, _src, dst), v in legs.items()
                              if token == mint and dst == RELAY_ROUTER)
            router_paid_out = sum(v for (token, src, _dst), v in legs.items()
                                  if token == payment and src == RELAY_ROUTER)
            if (legs.get((payment, payer, RELAY_ROUTER)) != paid or router_paid_out != paid
                    or into_router != amount_out or net.get((mint, RELAY_ROUTER), 0) != 0
                    or any(token == payment and wallet in (src, dst) for (token, src, dst) in legs)):
                return None
        else:
            required = [(payment, payer, RELAY_ROUTER, paid),
                        (payment, RELAY_ROUTER, swap[0], paid),
                        (mint, swap[0], RELAY_ROUTER, amount_out),
                        (mint, RELAY_ROUTER, wallet, amount_out)]
            if any(legs.get((token, source, target)) != value for token, source, target, value in required):
                return None
        gainers = {party for (token, party), delta in net.items() if token == mint and delta > 0}
        if any_route and wallet in gainers:
            # FEE AND TAX GAINERS (2026-09-28, Alpha GO). A V4 hook fee or a token tax pays the mint
            # to a second party FROM THE POOL: 13/25 real 4663 buys, 12/30 real 56 buys. Such a
            # gainer is allowed only when everything it received came from the pool side, the
            # parties upstream of the swap target in the mint flow, or from a pass-through they
            # paid (a Base V4 hook forwards its fee on); never the router or the target.
            # The walk starts where the coin entered the relay chain: the adapter, or for an other
            # target whoever paid the router the coin (the Settler itself, a Kyber executor, a
            # pool). Like the adapter, an entry point is relay chain, never pool side.
            seeds = ({swap[0]} if not other else
                     {src for (token, src, dst) in legs if token == mint and dst == RELAY_ROUTER})
            relay = {RELAY_ROUTER, swap[0], wallet} | seeds
            pool_side, frontier = set(), set(seeds)
            while frontier:
                frontier = ({source for (token, source, target) in legs
                             if token == mint and target in frontier} - pool_side - relay)
                pool_side |= frontier
            frontier = set(pool_side)
            while frontier:
                frontier = ({target for (token, source, target) in legs
                             if token == mint and source in frontier} - pool_side - relay)
                pool_side |= frontier
            for gainer in gainers - {wallet}:
                senders = {source for (token, source, target) in legs
                           if token == mint and target == gainer}
                # A direct V2 pool pays the router itself, so it is a seed rather than an
                # upstream node. Accept its token's self-collected tax only when the same
                # receipt accounts for the exact gross Swap output. No arbitrary recipients.
                taxed_pools = set()
                if other and gainer == mint:
                    v2_topic = topic('Swap(address,uint256,uint256,uint256,uint256,address)')
                    for log in receipt['logs']:
                        ts = log['topics']
                        emitter = address(log['address'])
                        if (emitter not in seeds or emitter in {RELAY_ROUTER, swap[0], wallet}
                                or len(ts) != 3 or ts[0] != v2_topic
                                or indexed(ts[2]) != RELAY_ROUTER):
                            continue
                        a_in, b_in, a_out, b_out = abi(['uint256'] * 4, log['data'])
                        gross = sum(v for (token, src, _dst), v in legs.items()
                                    if token == mint and src == emitter)
                        if ((a_in > 0 and b_in == a_out == 0 and b_out == gross > 0)
                                or (b_in > 0 and a_in == b_out == 0 and a_out == gross > 0)):
                            taxed_pools.add(emitter)
                if other and str(chain) == '56':
                    # PCS V3 can pay its output fee directly to a separate fee wallet.
                    # A seed is a pool only for this exact, recipient-bound gross payout.
                    from sniper.services.memecoin_evm_v3_events import PANCAKE_V3_SWAP, valid_pancake_v3_data
                    for emitter in senders & seeds:
                        matches = [log for log in receipt['logs']
                                   if address(log['address']) == emitter
                                   and log['topics'] and log['topics'][0] == PANCAKE_V3_SWAP]
                        if len(matches) != 1 or emitter in {RELAY_ROUTER, swap[0], wallet}:
                            continue
                        log = matches[0]
                        if (len(log['topics']) != 3 or indexed(log['topics'][2]) != RELAY_ROUTER
                                or not valid_pancake_v3_data(log['data'])):
                            continue
                        a0, a1 = abi(['int256', 'int256'], log['data'][:130])
                        gross = sum(v for (token, src, _dst), v in legs.items()
                                    if token == mint and src == emitter)
                        delivered = legs.get((mint, emitter, RELAY_ROUTER), 0)
                        if (gross > 0 and delivered * 10 >= gross * 9
                                and ((a0 < 0 < a1 and -a0 == gross)
                                     or (a1 < 0 < a0 and -a1 == gross))):
                            taxed_pools.add(emitter)
                if not senders or not senders <= (pool_side | taxed_pools):
                    return None
            gainers = {wallet}
        if (gainers != {wallet} or net.get((mint, wallet)) != amount_out
                or net.get((payment, payer)) != -paid
                or net.get((payment, RELAY_ROUTER), 0) != 0
                or net.get((payment, swap[0]), 0) != 0):
            return None
        expected = [(RELAY_APPROVAL_PROXY, payer, RELAY_ROUTER, payment, paid, metadata),
                    (RELAY_ROUTER, RELAY_ROUTER, wallet, mint, amount_out, metadata)]
        if movements != expected:
            return None
        if other:
            return {'proof_version': RELAY_OTHER_TARGET_PROOF_VERSION, 'network_id': chain, 'transaction_hash': tx_hash,
                    'block_hash': receipt['blockHash'], 'block_number': receipt['blockNumber'],
                    'beneficiary': wallet, 'payer': payer, 'relayer': address(transaction['from']),
                    'mint': mint, 'output_atomic': amount_out, 'payment_token': payment,
                    'payment_atomic': paid, 'router': None,
                    'swap_target': address(swap[0]), 'swap_selector': swap[3][:4].hex()}
        return {'proof_version': RELAY_ENVELOPE_PROOF_VERSION if any_route else RELAY_KYBER_PROOF_VERSION,
                'network_id': chain, 'transaction_hash': tx_hash,
                'block_hash': receipt['blockHash'], 'block_number': receipt['blockNumber'],
                'beneficiary': wallet, 'payer': payer, 'relayer': address(transaction['from']),
                'mint': mint, 'output_atomic': amount_out, 'payment_token': payment,
                'payment_atomic': paid, 'router': None if any_route else KYBER_ROUTER_4663}
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError, DecodingError):
        return None


def _empty_code(code):
    """True when an `eth_getCode` answer carries no bytecode.

    A pruned node returns '0x' for a block it no longer holds, with no error, so this reads
    "unknown at that block", never "not a contract". `None` and '' come back from a transport that
    answered nothing and are treated the same way.
    """
    return str(code or '0x').strip().lower() in ('0x', '')


# PINNED CODE, VERIFIED ONCE PER TTL (2026-09-28, Alpha GO). {(chain, contract): monotonic expiry}.
# The first live RH detection spent 263 ms on five sequential reads, three of them this check, and
# landed ~117 ms after J7 although its socket log beat J7. Only a caller that passes `code_ttl_s`
# (the watcher, one process per chain) reads or writes this; the address verifier passes none.
_RELAY_CODE_CACHE = {}
# WARM BEFORE THE BUY (2026-09-28, Alpha GO). DegenCapitalLLC's relay buy reached the watcher 37 s
# after a restart and was decoded 1.167 s after arrival while J7 decided at +0.30 s: a cold cache
# puts the chain-id read and every pinned getCode on the detection path. The watcher now pins its
# chain identity once per process and verifies the pinned code at start and in the background (a
# second thread), so the cache has two writers and a lock.
_RELAY_CODE_LOCK = threading.Lock()
_CHAIN_ID_PINNED = set()


def _pinned_code(proof_version=None, chain=None):
    """[(contract, expected hash)]: Relay proxy and router always; Kyber unless the proof is the
    envelope or other-target version (None = every pinned contract, for the warm-up). Kyber's pin is
    4663's alone: a warm on another chain (`chain`) never reads it (56 and 8453 hash differently, so
    every warm there logged a mismatch that hid a real one; Alpha 2026-10-02)."""
    from sniper.execution.evm_relay_quote import (
        RELAY_APPROVAL_PROXY, RELAY_ROUTER, RELAY_APPROVAL_PROXY_CODE_HASH, RELAY_ROUTER_CODE_HASH,
    )
    pinned = [(RELAY_APPROVAL_PROXY, RELAY_APPROVAL_PROXY_CODE_HASH),
              (RELAY_ROUTER, RELAY_ROUTER_CODE_HASH)]
    if (proof_version not in (RELAY_ENVELOPE_PROOF_VERSION, RELAY_OTHER_TARGET_PROOF_VERSION)
            and chain in (None, '4663')):
        pinned.append((KYBER_ROUTER_4663, KYBER_CODE_HASH_4663))
    return pinned


def pin_chain_identity(chain, read_fn):
    """Read eth_chainId once; a match pins it for this process, so no detection reads it again. A
    mismatch or a failed read pins nothing and every detection keeps checking inline."""
    try:
        ok = _integer(read_fn('eth_chainId', [])) == int(chain)
    except Exception:
        ok = False
    if ok:
        with _RELAY_CODE_LOCK:
            _CHAIN_ID_PINNED.add(str(chain))
    return ok


def warm_relay_code(chain, read_fn, *, ttl_s, clock=time.monotonic):
    """Verify every pinned contract at head. {contract: 'verified' | 'mismatch' | 'unavailable'}.

    A matching hash extends the entry by `ttl_s`. A WRONG hash revokes it at once: the contract
    changed, and nothing may keep trusting the old verification. A failed or empty read leaves the
    entry alone, so it expires on its own TTL and the detection path then verifies inline.
    """
    from eth_utils import keccak
    out = {}
    for contract, expected in _pinned_code(chain=str(chain)):
        key = (str(chain), contract)
        try:
            code = read_fn('eth_getCode', [contract, 'latest'])
        except Exception:
            code = None
        try:
            digest = None if _empty_code(code) else '0x' + keccak(bytes.fromhex(code[2:])).hex()
        except (TypeError, ValueError, KeyError):      # a malformed answer proves nothing either way
            digest = None
        if digest is None:
            out[contract] = 'unavailable'
            continue
        matched = digest == expected
        with _RELAY_CODE_LOCK:
            if matched:
                _RELAY_CODE_CACHE[key] = clock() + float(ttl_s)
            else:
                _RELAY_CODE_CACHE.pop(key, None)
        out[contract] = 'verified' if matched else 'mismatch'
    return out


def read_relayer_purchase(receipt, transaction, wallet, *, chain, read_fn, evidence=None, any_route=False,
                          code_ttl_s=None, clock=time.monotonic, live=False, preconfirmed=False):
    """Require the pinned Relay code at the receipt block before accepting its authorization.

    With `code_ttl_s`, a verification is reused for that many seconds (code_basis 'cached'). Once it
    expires the code is read again, and a hash that is wrong then still refuses.
    """
    from eth_utils import keccak
    evidence = evidence if evidence is not None else {}
    proof = relayer_purchase_from_receipt(receipt, transaction, wallet, chain=chain,
                                          contracts_verified=True, any_route=any_route,
                                          preconfirmed=preconfirmed)
    if proof is None:
        # WHICH REFUSAL (2026-09-28): a transaction that is not the Relay Permit2 call at all is not
        # a relay-decoder miss. Three 56 refusals labelled unsupported_relayer_swap were third-party
        # batch token distributions to a watched wallet (299, 22 and 4 recipients), and a census
        # reading the label counted them as relay shapes the decoder could not read.
        from sniper.execution.evm_relay_quote import RELAY_APPROVAL_PROXY
        relay_call = (str((transaction or {}).get('to') or '').lower() == RELAY_APPROVAL_PROXY
                      and str((transaction or {}).get('input') or '').startswith('0x0a2b8f36'))
        evidence['reason'] = 'unsupported_relayer_swap' if relay_call else 'not_relay_envelope'
        return None
    # CODE BASIS (2026-09-23, operator decision "no waiting"). The custody node keeps state for
    # 1.7-16.8 minutes and then answers `0x` for an older block WITHOUT an error, so a receipt-block
    # read of a contract that plainly exists today cannot pass on any historical event. An empty
    # answer is therefore UNKNOWN, not "no contract", and it may be re-asked at head.
    #
    # An empty read retries once at 'latest'. A NON-EMPTY read that hashes wrong never retries: that
    # is a real statement about that block, and re-asking at head would hide a contract that was
    # genuinely different when the swap happened.
    #
    # What the two bases prove is not the same thing, which is why the basis is recorded rather than
    # smoothed away. For a LIVE event head and the receipt block are the same block or seconds
    # apart, so a head basis is exact. For a BACKFILLED event it proves only that the contract is
    # correct NOW, so an address that was an EOA then and a contract today would read as verified.
    # Any consumer that treats a stored classification as evidence must read `code_basis` first.
    code_basis = 'receipt_block'
    pinned = _pinned_code(proof.get('proof_version'))              # Kyber unless proven otherwise
    keys = [(str(chain), contract) for contract, _expected in pinned]
    now = clock()
    with _RELAY_CODE_LOCK:
        cached = bool(code_ttl_s) and all(_RELAY_CODE_CACHE.get(key, -1.0) > now for key in keys)
        chain_pinned = bool(code_ttl_s) and str(chain) in _CHAIN_ID_PINNED
    if cached and chain_pinned:
        evidence['code_basis'] = proof['code_basis'] = 'cached'
        return proof
    started = time.perf_counter()
    try:
        if not chain_pinned and _integer(read_fn('eth_chainId', [])) != int(chain):
            evidence['reason'] = 'chain_mismatch'
            return None
        if not chain_pinned and code_ttl_s:
            with _RELAY_CODE_LOCK:
                _CHAIN_ID_PINNED.add(str(chain))
        evidence['chain_id_ms'] = (time.perf_counter() - started) * 1000.0
        if cached:
            with _RELAY_CODE_LOCK:
                cached = all(_RELAY_CODE_CACHE.get(key, -1.0) > clock() for key in keys)
            if cached:
                evidence['code_basis'] = proof['code_basis'] = 'cached'
                return proof
        for contract, expected in pinned:
            # A LIVE EVENT'S ERRORED RECEIPT-BLOCK READ IS UNKNOWN TOO (2026-09-28). A read node
            # that has not reached the block yet answers an error, not '0x': the watcher's tx-read
            # host hands the receipt over in ~50 ms, before the read host has the block
            # (notanicecat69 0x29225b3297, decided no_buy at 11:56:31Z with a cold code cache). For
            # a live event head is that block or seconds past it, so it re-asks head like an empty
            # answer, and the hash must still match. A HISTORICAL caller (address verification)
            # keeps the 2026-09-16 rule: a failed read never falls back to current code.
            try:
                code = read_fn('eth_getCode', [contract, receipt['blockNumber']])
            except Exception:
                if not live:
                    raise
                code = None
            if _empty_code(code):
                code = read_fn('eth_getCode', [contract, 'latest'])
                if _empty_code(code):
                    evidence['reason'] = 'contract_code_unavailable'
                    return None
                code_basis = 'head'
            if '0x' + keccak(bytes.fromhex(code[2:])).hex() != expected:
                evidence['reason'] = 'contract_code_mismatch'
                return None
    except Exception:
        evidence['reason'] = 'contract_code_unavailable'
        return None
    evidence['code_ms'] = (time.perf_counter() - started) * 1000.0 - evidence.get('chain_id_ms', 0.0)
    # One basis for the whole proof: if ANY pinned contract needed head, the proof is head.
    if code_ttl_s:
        with _RELAY_CODE_LOCK:
            for key in keys:
                _RELAY_CODE_CACHE[key] = now + float(code_ttl_s)
    evidence['code_basis'] = code_basis
    proof['code_basis'] = code_basis
    return proof


def price_relayer_purchase(proof, *, decimals_fn, price_fn):
    """Use the actual payment token and its price. Missing prices prevent a detection."""
    import math
    try:
        if not proof:
            return None
        payment_decimals = decimals_fn(proof['payment_token'])
        token_decimals = decimals_fn(proof['mint'])
        if any(not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 36
               for d in (payment_decimals, token_decimals)):
            return None
        price = float(price_fn(proof['payment_token']))
        amount = int(proof['output_atomic']) / 10 ** token_decimals
        dollars = int(proof['payment_atomic']) / 10 ** payment_decimals * price
        unit_price = dollars / amount
        if not all(math.isfinite(v) and v > 0 for v in (price, amount, dollars, unit_price)):
            return None
        return {'mint': proof['mint'], 'token_delta': amount,
                'source_size_usd': dollars, 'source_price': unit_price,
                'payment_token': proof['payment_token'], 'payment_atomic': str(proof['payment_atomic']),
                'acquired_atomic': str(proof['output_atomic']), 'payment_decimals': payment_decimals,
                'token_decimals': token_decimals, 'receipt_proof_version': proof.get('proof_version'),
                'source_initiation': relay_receipt_initiation(proof)}
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None


# --- EVM SELL PATH, SHADOW (2026-09-29, Alpha GO on the spec's aef3d5719) ------------------------
# Spec docs/superpowers/specs/2026-09-29-memecoin-evm-onchain-sell-path-design.md. The watcher sees
# Transfers FROM watched wallets and records a source's own sale with the proof that it IS a sale.
# Nothing here signs, and the mirror does not read these rows.

# What a sale is PAID in, per chain: the chain's wrapped native (CHAIN_QUOTE), the Relay payment
# token of the watcher's buy decode, and USDT on 56 (the real Relay sale routed through it). A
# proceeds token LEAVING a wallet is a buy's pay leg, so it is never a sale candidate.
SELL_PROCEEDS_TOKENS = {
    '4663': frozenset({'0x0bd7d308f8e1639fab988df18a8011f41eacad73',     # WETH
                       '0x5fc5360d0400a0fd4f2af552add042d716f1d168'}),   # USDG
    '56': frozenset({'0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c',       # WBNB
                     '0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d',       # Binance-Peg USDC
                     '0x55d398326f99059ff775485246999027b3197955'}),     # USDT
    '8453': frozenset({'0x4200000000000000000000000000000000000006',     # WETH
                       '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'}),   # USDC
    # Chain 1 (2026-10-17): 61 of 62 J7-proved chain-1 sales by watched wallets are Relay sales
    # (b_relay, USDC on to Relay) once these exist; without them every chain-1 sale decoded to [].
    '1': frozenset({'0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2',          # WETH
                    '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48'}),        # USDC
    '57073': frozenset({'0x4200000000000000000000000000000000000006',
                        '0x2d270e6886d130d724215a266106e6832161eaed',
                        '0xf1815bd50389c46847f0bda824ec8da914045d14'}),
}


def sell_candidate(log, watched, chain):
    """True when a delivered Transfer is worth a receipt read: FROM a watched wallet, not reorged
    out, and not a proceeds token (a buy's pay leg). Decided at arrival, with no read."""
    topics = log.get('topics') or ()
    if log.get('removed') or len(topics) != 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
        return False
    if _topic_address(topics[1]) not in watched:
        return False
    return str(log.get('address') or '').lower() not in SELL_PROCEEDS_TOKENS.get(str(chain), ())


def relay_code_verified(chain, clock=time.monotonic):
    """True while the pinned Relay router or approval proxy code is verified for this chain (the
    watcher's warm-up cache, `warm_relay_code`). A cold or revoked cache proves nothing."""
    from sniper.execution.evm_relay_quote import RELAY_APPROVAL_PROXY, RELAY_ROUTER
    now = clock()
    with _RELAY_CODE_LOCK:
        return any(_RELAY_CODE_CACHE.get((str(chain), c), 0) > now
                   for c in (RELAY_ROUTER, RELAY_APPROVAL_PROXY))


def wallet_sales_from_receipt(receipt, wallet, *, chain, relay_verified, native_delta=None):
    """[{mint, sold_atomic, proof, proceeds_token, proceeds_atomic}] for the coins `wallet` SOLD in
    this successful transaction, or [].

    A coin is sold when the wallet's net outflow of it is positive AND one proof holds:
      a_erc20   the wallet's net inflow of a proceeds token is positive (the largest one is named);
      a_native  else the caller's `native_delta` (the wallet's native balance change across the
                block, gas added back when it sent the tx) is positive;
      b_relay   else the coin went to the pinned Relay router or approval proxy, their code is
                verified (`relay_verified`), and a proceeds token was transferred TO a Relay
                contract in the same transaction. On this route the proceeds leave toward Relay
                (a fill elsewhere), so (a) cannot hold: the real 56 badabeepp sale is the fixture.
    A plain transfer to another wallet has none of these and is never a sale. When several coins
    are sold in one transaction the proceeds cannot be attributed: `proceeds_atomic` is None.
    """
    from sniper.execution.evm_relay_quote import RELAY_APPROVAL_PROXY, RELAY_ROUTER
    if str((receipt or {}).get('status') or '').lower() not in {'1', '0x1', 'true'}:
        return []
    wallet = str(wallet or '').lower()
    proceeds = SELL_PROCEEDS_TOKENS.get(str(chain), frozenset())
    relay = {RELAY_ROUTER, RELAY_APPROVAL_PROXY, RELAY_ADAPTER_4663}
    coin_net, proceeds_net, to_relay = {}, {}, set()
    proceeds_to_relay = False
    for log in receipt.get('logs') or []:
        topics = log.get('topics') or []
        if len(topics) != 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
            continue
        token = str(log.get('address') or '').lower()
        sender, receiver = _topic_address(topics[1]), _topic_address(topics[2])
        amount = _integer(log.get('data'))
        if not _ADDRESS.fullmatch(token) or amount <= 0 or sender == receiver:
            continue
        if token in proceeds:
            if receiver in relay:
                proceeds_to_relay = True
            if wallet in (sender, receiver):
                proceeds_net[token] = proceeds_net.get(token, 0) + (amount if receiver == wallet else -amount)
            continue
        if sender == wallet:
            coin_net[token] = coin_net.get(token, 0) + amount
            if receiver in (RELAY_ROUTER, RELAY_APPROVAL_PROXY):
                to_relay.add(token)
        elif receiver == wallet:
            coin_net[token] = coin_net.get(token, 0) - amount
    sold = {token: net for token, net in coin_net.items() if net > 0}
    if not sold:
        return []
    paid = {token: net for token, net in proceeds_net.items() if net > 0}
    if paid:
        proof, token = 'a_erc20', max(paid, key=paid.get)
        proceeds_atomic = paid[token]
    elif native_delta is not None and native_delta > 0:
        proof, token, proceeds_atomic = 'a_native', 'native', int(native_delta)
    elif relay_verified and proceeds_to_relay and any(t in to_relay for t in sold):
        proof, token, proceeds_atomic = 'b_relay', None, None
        sold = {t: n for t, n in sold.items() if t in to_relay}
    else:
        return []
    single = len(sold) == 1
    return [{'mint': mint, 'sold_atomic': net, 'proof': proof, 'proceeds_token': token,
             'proceeds_atomic': proceeds_atomic if single else None}
            for mint, net in sorted(sold.items())]


def source_events_from_receipt(receipt, transaction, *, wallet, chain, quote_usd,
                               decimals_fn, prebalance_atomic, source_timestamp):
    """Ink canonical owner flows, aggregated by economic transaction before origin dedup.

    `prebalance_atomic` must come from transaction-ordered state. A later balance read is not a
    source prebalance and remains unknown here.
    """
    chain = str(chain)
    wallet = str(wallet or "").lower()
    tx_hash = str((transaction or {}).get("hash") or "").lower()
    block_hash = str((receipt or {}).get("blockHash") or "").lower()
    if (chain != "57073" or not _ADDRESS.fullmatch(wallet)
            or not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash)
            or tx_hash != str((receipt or {}).get("transactionHash") or "").lower()
            or not re.fullmatch(r"0x[0-9a-f]{64}", block_hash)
            or int(block_hash, 16) == 0
            or str(transaction.get("blockHash") or "").lower() != block_hash
            or transaction.get("blockNumber") != receipt.get("blockNumber")
            or _integer((receipt or {}).get("status")) != 1
            or not isinstance(source_timestamp, int) or source_timestamp <= 0):
        return []
    logs = receipt.get("logs")
    if not isinstance(logs, list) or any(not isinstance(log, dict) or log.get("logIndex") is None
                                        for log in logs):
        return []
    events = []
    for sale in wallet_sales_from_receipt(receipt, wallet, chain=chain, relay_verified=False):
        mint = sale["mint"].lower()
        pre = (prebalance_atomic or {}).get(mint) if isinstance(prebalance_atomic, dict) else None
        pre = int(pre) if isinstance(pre, int) and pre >= int(sale["sold_atomic"]) else None
        events.append({"chain_id": chain, "origin_key": f"{chain}:{tx_hash}:{wallet}:{mint}:sell",
                       "tx_hash": tx_hash, "owner": wallet, "mint": mint, "side": "sell",
                       "token_amount_atomic": str(sale["sold_atomic"]),
                       "source_prebalance_atomic": str(pre) if pre is not None else None,
                       "proof": sale["proof"],
                       "proceeds_token": sale["proceeds_token"],
                       "proceeds_atomic": str(sale["proceeds_atomic"])
                       if sale["proceeds_atomic"] is not None else None,
                       "source_timestamp": source_timestamp, "coverage_complete": pre is not None})
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    quote = CHAIN_QUOTE[chain]["token"].lower()
    buy = direct_buy_from_receipt(receipt, transaction, wallet, weth=quote,
                                  quote_usd=quote_usd, decimals_fn=decimals_fn)
    if buy:
        received, spent = wallet_paid_token_flows(receipt, wallet, quote, transaction)
        mint = buy["mint"].lower()
        events.append({"chain_id": chain, "origin_key": f"{chain}:{tx_hash}:{wallet}:{mint}:buy",
                       "tx_hash": tx_hash, "owner": wallet, "mint": mint, "side": "buy",
                       "token_amount_atomic": str(received[mint]),
                       "quote_amount_atomic": str(spent), "source_size_usd": buy["source_size_usd"],
                       "source_price": buy["source_price"],
                       "source_timestamp": source_timestamp, "coverage_complete": True})
    return events
