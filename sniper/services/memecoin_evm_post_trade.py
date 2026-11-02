"""The source's POST-trade price on EVM, for the chase gates (spec
docs/superpowers/specs/2026-09-28-memecoin-evm-chase-post-trade-reference-design.md).

WHAT IT MEASURES. A large source buy moves a thin pool by itself, and the chase gates used to charge
that move to us: they compared our quote with the source's AVERAGE fill. The reference here is the
pool's price after the source's transaction, as the ratio

    ratio = post-trade coin price / the buy leg's own average coin price

taken from ONE venue event in the source receipt, both sides in that pool's raw units, so token
order and decimals cancel and no USD conversion happens. The caller multiplies the average fill it
already uses (USD) by the ratio.

    V4  PoolManager Swap (pool = topic1 PoolId), amounts from the SWAPPER's side (positive =
        received); the coin side comes from the pool's stored PoolKey. Post = the sqrtPrice of the
        LAST swap on that pool in the transaction: a launchpad hook that sells its coin fee right
        after the buy lowers the price the next buyer faces, and that is the price that counts.
    V3  Swap from the pool, amounts from the POOL's side (negative = paid out); the coin side is the
        side the pool paid, proven by the pool's own mint Transfers summing to it. Post as V4.
    V2  Swap + the pair's last Sync; the coin side as V3. Post = the Sync reserves.

A leg is a swap that paid the coin OUT to the buyer's side. Exactly one leg or no ratio: two legs
(a split route, two pools of the coin, a batched bot transaction) have no single price. One 4663
exception (MEMECOIN_4663_SELF_SWAP_HOOKS, default off): on a listed hook's pool, swaps after the buy
that all come from ONE other sender are the hook's own fee swaps, not a second leg. With the
owner known, the leg's out must satisfy delta <= out <= delta / 0.9: the owner's coin delta, with
10% slack for a transfer tax or a hook fee taken from the output.

BINDING (resolve): the reference applies only when OUR quote trades the SAME pool. A price from a
pool we will not trade is never used; the gate keeps the average fill and says why.

Never imported on a path that can raise into a funded decision without a guard: every function
here returns a reason instead of raising."""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor

from eth_utils import keccak
from sniper.services.memecoin_evm_v3_events import V3_SWAP, PANCAKE_V3_SWAP, valid_pancake_v3_data

from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP, v4_pool_id
from sniper.services.memecoin_tick_stream import PONS_V2_CURVE_BUY

logger = logging.getLogger(__name__)

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
V2_SWAP = "0x" + keccak(b"Swap(address,uint256,uint256,uint256,uint256,address)").hex()
V2_SYNC = "0x" + keccak(b"Sync(uint112,uint112)").hex()
RATIO_MIN, RATIO_MAX = 0.98, 50.0
OUT_SLACK = 0.9                     # delta <= out <= delta / 0.9, as the Solana routed leg
_Q192 = float(2 ** 192)
_UINT256, _SIGN = 1 << 256, 1 << 255

# (chain, router) -> (factory, pair init-code hash). Only entries PROVEN against our own filled
# receipts (tests/test_memecoin_evm_post_trade.py, positions 66521/66594/66596): a wrong constant
# would name a pair that does not exist and bind nothing, never a wrong pool, but it proves nothing.
V2_FACTORIES = {
    ("56", "0x10ed43c718714eb63d5aa57b78b54704e256024e"): (       # PancakeSwap V2 router
        "0xca143ce32fe78f1f7019d7d551a6402fc5350c73",
        "0x00fb7f630766e6a796048ea87d01acd3068e8ff67d078148a3fa3f4a84f69bd5"),
}


def _signed(word):
    value = int(word, 16)
    return value - _UINT256 if value >= _SIGN else value


def _words(log):
    data = str(log.get("data") or "0x")[2:]
    return [data[i:i + 64] for i in range(0, len(data), 64)]


def _topic_address(topic):
    return "0x" + str(topic)[-40:].lower()


# ---------------------------------------------------------------------------------- V4 PoolKeys

_KEY_CACHE = {}                       # (chain, pool) -> key; PoolKeys are immutable, misses are not kept
_KEY_LOCK = threading.Lock()


def stored_v4_key(chain, pool):
    """The PoolManager-verified PoolKey from memecoin_evm_v4_pool_keys, or None. Read-only."""
    chain, pool = str(chain), str(pool).lower()
    with _KEY_LOCK:
        hit = _KEY_CACHE.get((chain, pool))
    if hit is not None:
        return hit
    manager = V4_POOL_MANAGERS.get(chain)
    if not manager:
        return None
    try:
        from sniper.persistence import db_memecoin as m
        from sniper.persistence.db_core import get_readonly_connection
        from sniper.util.state_paths import state_path
        with contextlib.closing(get_readonly_connection(str(state_path("sniper_tracker.db")))) as conn:
            key = m.get_evm_v4_pool_key(conn, chain_id=chain, pool_id=pool, pool_manager=manager)
    except Exception:
        logger.exception("post-trade: PoolKey read failed chain=%s pool=%s", chain, pool)
        return None
    if key is not None:
        with _KEY_LOCK:
            _KEY_CACHE[(chain, pool)] = key
    return key


def conn_v4_key_fn(conn):
    """A pool_key_fn reading through an OPEN connection (the watcher's own ledger handle): a new
    read-only open cost 3.1 ms p50 per pool on the detection path; this read is a single indexed
    SELECT. Hits go to the shared immutable-key cache."""
    def read(chain, pool):
        chain, pool = str(chain), str(pool).lower()
        with _KEY_LOCK:
            hit = _KEY_CACHE.get((chain, pool))
        if hit is not None:
            return hit
        manager = V4_POOL_MANAGERS.get(chain)
        if not manager:
            return None
        try:
            from sniper.persistence import db_memecoin as m
            key = m.get_evm_v4_pool_key(conn, chain_id=chain, pool_id=pool, pool_manager=manager)
        except Exception:
            logger.exception("post-trade: PoolKey read failed chain=%s pool=%s", chain, pool)
            return None
        if key is not None:
            with _KEY_LOCK:
                _KEY_CACHE[(chain, pool)] = key
        return key
    return read


def warm_v4_keys(chain, receipt, mint, *, offpath_fn=None):
    """Warm the PoolKey of every V4 pool this receipt swapped on whose key no cache holds. The key
    is rebuilt from the receipt's own logs and verified by hash (derive_pool_key_from_receipt),
    then published to this module's cache, the V4 rung's cache and (on the side thread, so a busy
    ledger never holds this worker) the durable table. A pool whose hook emitted nothing here goes
    to the off-path Initialize read instead. Called on the receipt worker only, never on the entry
    path. Returns how many keys were rebuilt; never raises."""
    try:
        from sniper.execution import evm_v4_quote as v4q
        chain = str(chain)
        manager = (V4_POOL_MANAGERS.get(chain) or "").lower()
        logs = (receipt or {}).get("logs") or []
        derived, seen = 0, set()
        for log in logs:
            topics = log.get("topics") or ()
            if (not manager or len(topics) < 2 or str(topics[0]).lower() != V4_SWAP
                    or str(log.get("address") or "").lower() != manager):
                continue
            pool = str(topics[1]).lower()
            if pool in seen:
                continue
            seen.add(pool)
            with _KEY_LOCK:
                known = (chain, pool) in _KEY_CACHE
            if known or (chain, pool) in v4q._key_cache:
                continue
            words = _words(log)
            key = v4q.derive_pool_key_from_receipt(
                chain, pool, logs, mint, swap_fee=int(words[5], 16) if len(words) >= 6 else None)
            if key is None:
                (offpath_fn or v4q.warm_pool_key_offpath)(chain, pool)
                continue
            with _KEY_LOCK:
                _KEY_CACHE[(chain, pool)] = key
            v4q._key_cache[(chain, pool)] = key
            v4q.store_pool_key_offpath(chain, pool, key)
            derived += 1
        return derived
    except Exception:
        logger.exception("post-trade: V4 key warm failed chain=%s mint=%s", chain, mint)
        return 0


# ---------------------------------------------------------------------------------- source side

def source_post_trade(chain, receipt, mint, *, owner=None, pool_key_fn=None, infinity_read_fn=None):
    """{"ratio", "pool", "venue"} for the one leg that bought `mint`, else {"reason"}. Never raises."""
    if not isinstance(receipt, dict):
        return {"reason": "no_receipt"}
    if str(receipt.get("status") or "").lower() not in ("0x1", "1", "true"):
        return {"reason": "reverted"}
    # All source receipt consumers share this seam (J7, on-chain and fast lane). Queue key
    # discovery without holding their price decode; no funded RPC or synchronous discovery.
    if str(chain) == "56" and (infinity_read_fn is not None or read_host(chain)):
        try:
            from sniper.execution.evm_pancake_infinity import warm_receipt_offpath
            warm_receipt_offpath(chain, receipt, mint,
                                 read_fn=infinity_read_fn or default_read_fn(chain))
        except Exception:
            logger.warning("Infinity source-key warm not queued mint=%s", mint)
    try:
        return _decode(str(chain), receipt.get("logs") or [], str(mint or "").lower(),
                       str(owner).lower() if owner else None, pool_key_fn or stored_v4_key)
    except Exception:
        logger.exception("post-trade decode failed chain=%s mint=%s", chain, mint)
        return {"reason": "decode_error"}


def _decode(chain, logs, mint, owner, pool_key_fn):
    manager = (V4_POOL_MANAGERS.get(chain) or "").lower()
    paid = {}                             # address -> total mint it sent in this transaction
    sent = {}                             # address -> [(receiver, amount)] of the mint it sent
    delta = 0                             # the owner's net mint
    for log in logs:
        topics = log.get("topics") or ()
        if (len(topics) == 3 and str(topics[0]).lower() == TRANSFER
                and str(log.get("address") or "").lower() == mint):
            amount = int(str(log.get("data") or "0x0"), 16)
            sender, receiver = _topic_address(topics[1]), _topic_address(topics[2])
            paid[sender] = paid.get(sender, 0) + amount
            sent.setdefault(sender, []).append((receiver, amount))
            delta += (amount if receiver == owner else 0) - (amount if sender == owner else 0)
    legs, last, sync, v4_unknown = [], {}, {}, False
    entry_hint_complete = True
    v4_rows = {}                          # pool -> [(sender, leg or None)] in log order (4663 hooks)
    v2_swaps = {}                         # pair -> Swap events it emitted in this transaction
    pons_buys = {}                        # pool -> total input/output across sequential buys
    for log in logs:
        topics = log.get("topics") or ()
        if not topics:
            continue
        topic0, address, words = str(topics[0]).lower(), str(log.get("address") or "").lower(), _words(log)
        if topic0 == V4_SWAP and manager and address == manager:
            try:
                entry_hint_complete &= (len(topics) == 3 and len(words) == 6
                    and len(bytes.fromhex(''.join(words))) == 192
                    and all(str(t).startswith('0x') and len(str(t)) == 66
                            and len(bytes.fromhex(t[2:])) == 32 for t in topics[1:]))
            except (TypeError, ValueError):
                entry_hint_complete = False
        if topic0 == V4_SWAP and manager and address == manager and len(topics) >= 2 and len(words) >= 6:
            pool = str(topics[1]).lower()
            key = pool_key_fn(chain, pool)
            if not key:
                v4_unknown = True
                continue
            currencies = (str(key.get("currency0") or "").lower(), str(key.get("currency1") or "").lower())
            if mint not in currencies:
                continue                                     # a bridge hop, not the coin's pool
            coin1 = currencies[1] == mint
            a0, a1 = _signed(words[0]), _signed(words[1])
            last[pool] = ("sqrt", int(words[2], 16), coin1, int(words[3], 16))
            coin, other = (a1, a0) if coin1 else (a0, a1)
            leg = ("v4", pool, -other, coin, coin1) if coin > 0 and other < 0 else None
            if leg is not None:                              # the swapper received the coin
                legs.append(leg)
            v4_rows.setdefault(pool, []).append(
                (_topic_address(topics[2]) if len(topics) >= 3 else None, leg, key))
        elif (topic0 in (V3_SWAP, PANCAKE_V3_SWAP) and len(topics) == 3
              and len(words) >= 5
              and (topic0 != PANCAKE_V3_SWAP or valid_pancake_v3_data(log.get("data")))):
            a0, a1 = _signed(words[0]), _signed(words[1])
            if address in last and last[address][0] == "sqrt":
                last[address] = ("sqrt", int(words[2], 16), last[address][2])
            for out, into, coin1 in ((-a0, a1, False), (-a1, a0, True)):
                if out > 0 and into > 0 and paid.get(address) == out:
                    last[address] = ("sqrt", int(words[2], 16), coin1)
                    legs.append(("v3", address, into, out, coin1))
        elif (chain == '4663' and topic0 == PONS_V2_CURVE_BUY and len(topics) == 3
              and len(words) == 4 and all(len(w) == 64 for w in words)):
            into, out = int(words[0], 16), int(words[1], 16)
            if into > 0 and out > 0:
                previous_in, previous_out = pons_buys.get(address, (0, 0))
                pons_buys[address] = (previous_in + into, previous_out + out)
        elif topic0 == V2_SWAP and len(words) >= 4:
            v2_swaps[address] = v2_swaps.get(address, 0) + 1
            in0, in1, out0, out1 = (int(w, 16) for w in words[:4])
            for out, into, coin1 in ((out0, in1, False), (out1, in0, True)):
                if out > 0 and into > 0 and paid.get(address) == out:
                    last[address] = ("reserves", None, coin1)
                    legs.append(("v2", address, into, out, coin1))
        elif topic0 == V2_SYNC and len(words) >= 2:
            sync[address] = (int(words[0], 16), int(words[1], 16))
    # Relay/Kyber can split one buy into repeated calls to the SAME Pons pool. Bind their total
    # output to that pool's complete mint transfers; different pools remain separate route legs.
    for pool, (into, out) in pons_buys.items():
        if paid.get(pool) == out:
            legs.append(('pons_v2', pool, into, out, True))
    if chain == "4663":
        legs = _drop_hook_self_swaps(legs, v4_rows)
    if not legs:
        return {"reason": "v4_key_unknown" if v4_unknown else "no_venue_leg"}
    if len(legs) > 1:
        # Complete split economics can reach the shared quoter; no leg prices the whole buy.
        pools = {leg[1] for leg in legs}
        total = sum(leg[3] for leg in legs)
        if (chain == '4663' and owner is not None and not v4_unknown
                and all(leg[0] == 'v4' for leg in legs) and len(pools) == len(legs) <= 8
                and all(leg is not None for rows in v4_rows.values() for _, leg, _ in rows)
                and delta > 0 and delta <= total and total * 9 <= delta * 10):
            return dict(reason='split_route', venue='v4', pools=sorted(pools),
                        pool_output_atomic=str(total), receipt_routes_complete=True)
        return {"reason": "split_route"}
    venue, pool, into, out, coin1 = legs[0]
    if owner is not None:
        if delta <= 0:
            return {"reason": "owner_received_nothing"}
        if not delta <= out <= delta / OUT_SLACK:
            # A known coin pool can guide fresh discovery without proving the source price.
            if chain == '4663' and venue == 'v4' and not v4_unknown and entry_hint_complete:
                return {"reason": "leg_does_not_bind", "entry_hint_pool": pool}
            return {"reason": "leg_does_not_bind"}
    if venue == 'pons_v2':
        return dict(reason='pons_post_state_unavailable', venue=venue, pool=pool,
                    pool_input_atomic=str(into), pool_output_atomic=str(out),
                    receipt_routes_complete=not v4_unknown)
    if venue == "v2":
        if pool not in sync:
            return {"reason": "no_sync"}
        r0, r1 = sync[pool]
        post = (r0 / r1) if coin1 else (r1 / r0)             # other per coin, raw units
    else:
        sqrt = float(last[pool][1])
        price1_per_0 = sqrt * sqrt / _Q192
        post = (1.0 / price1_per_0) if coin1 else price1_per_0
    ratio = post / (into / out)
    if not (math.isfinite(ratio) and RATIO_MIN <= ratio <= RATIO_MAX):
        return {"reason": "ratio_out_of_range"}
    result = {"ratio": ratio, "pool": pool, "venue": venue, "mint_is_c1": coin1,
              "pool_input_atomic": str(into), "pool_output_atomic": str(out),
              "receipt_routes_complete": not v4_unknown}
    if venue == "v2":
        result.update(reserve0_atomic=str(r0), reserve1_atomic=str(r1))
        tax = _v2_buy_tax(sent.get(pool) or (), mint, v2_swaps.get(pool, 0))
        if tax is None and chain == '56' and v2_swaps.get(pool, 0) > 1:
            bound = v2_buy_with_sell_tax(logs, mint, pool)
            tax = bound[1] if bound is not None else None
        if tax is not None:
            result["buy_tax"] = tax
    else:
        result["sqrt_price_x96"] = str(last[pool][1])
        if venue == "v4":
            liquidity = last[pool][3]
            if not 0 <= liquidity < 2**128:
                return {"reason": "liquidity_out_of_range"}
            if liquidity:
                result["liquidity_atomic"] = str(liquidity)
    counter = _counter_token(logs, pool, into, mint)
    if counter is not None:
        # Additive (2026-10-15): the token the source paid INTO this leg's pool, for the entry's
        # path search (evm_v2_exit_quote.seed_pair_tokens). A reader that does not know the key
        # ignores it; nothing here decides a gate.
        result["counter_token"] = counter
    return result


def _counter_token(logs, pool, into, mint):
    """The one token Transferred INTO `pool` in this transaction for exactly `into`, other than the
    coin; None when there is none or more than one. Bound to the leg's own pool and its exact
    input: a transfer into another pool, or of another amount, never names it."""
    found = set()
    for log in logs or ():
        topics = log.get("topics") or ()
        if len(topics) != 3 or str(topics[0]).lower() != TRANSFER:
            continue
        token = str(log.get("address") or "").lower()
        if token == mint or _topic_address(topics[2]) != pool:
            continue
        try:
            amount = int(str(log.get("data") or "0x0"), 16)
        except ValueError:
            continue
        if amount == into:
            found.add(token)
    return found.pop() if len(found) == 1 else None


def _drop_hook_self_swaps(legs, v4_rows):
    """`legs` without the later legs of a listed hook's self-swap shape (MEMECOIN_4663_SELF_SWAP_HOOKS):
    on a pool whose hook is listed, when the FIRST swap is a coin buy and every later swap comes from
    ONE sender that is not the first swap's, those later swaps are the hook's own (a fee processor's
    tax sells and small buys), not a second route leg. The post state stays the pool's last swap."""
    from sniper.settings import memecoin as cfg
    hooks = cfg.MEMECOIN_4663_SELF_SWAP_HOOKS
    drop = set()
    for rows in v4_rows.values():
        (first, first_leg, key), later = rows[0], rows[1:]
        senders = {sender for sender, _leg, _key in later}
        if (later and first_leg is not None and first and None not in senders and len(senders) == 1
                and first not in senders and str(key.get("hooks") or "").lower() in hooks):
            drop.update(id(leg) for _sender, leg, _key in later if leg is not None)
    return [leg for leg in legs if id(leg) not in drop]


def _v2_buy_tax(transfers, mint, swaps):
    """The coin's transfer tax on the buy leg, from the pair's own Transfers of the coin, or None.

    A taxed coin splits the pair's payout: the fee to the token contract, the rest to the swap's
    next hop (BINF 2026-09-30: 626,252 to the token, 20,248,822 to the router: 3.00%). Unknown
    (None) unless it is that shape exactly: one Swap on the pair in this transaction (Alpha: more
    than one reads unknown), and every Transfer besides the largest goes to the token contract.
    One Transfer and nothing else is a decoded ZERO, not an unknown."""
    if swaps != 1 or not transfers:
        return None
    total = sum(amount for _receiver, amount in transfers)
    main = max(transfers, key=lambda item: item[1])
    rest = [item for item in transfers if item is not main]
    if total <= 0 or any(receiver != mint for receiver, _amount in rest):
        return None
    tax = (total - main[1]) / total
    return tax if math.isfinite(tax) and 0.0 <= tax < 1.0 else None


def v2_buy_with_sell_tax(logs, mint, pool):
    """One exact V2 coin buy plus sell-direction swaps, or None. Receipt-only.

    All coin payouts must sum to that buy's gross output. The main payout must match its
    recipient; every smaller payout must be self-collected token tax. Extra buys, mixed
    directions, malformed swaps, and foreign fee recipients stay unknown.
    """
    try:
        transfers, swaps = [], []
        for log in logs:
            topics = log.get('topics') or ()
            emitter = str(log.get('address') or '').lower()
            if (emitter == mint and len(topics) == 3 and str(topics[0]).lower() == TRANSFER
                    and _topic_address(topics[1]) == pool):
                transfers.append((_topic_address(topics[2]), int(log['data'], 16)))
            if emitter == pool and topics and str(topics[0]).lower() == V2_SWAP:
                words = _words(log)
                if len(topics) != 3 or len(words) != 4 or any(len(w) != 64 for w in words):
                    return None
                swaps.append((log, tuple(int(w, 16) for w in words)))
        total = sum(amount for _, amount in transfers)
        candidates = []
        for log, (in0, in1, out0, out1) in swaps:
            if in1 > 0 and in0 == out1 == 0 and out0 == total > 0:
                candidates.append((log, False))
            elif in0 > 0 and in1 == out0 == 0 and out1 == total > 0:
                candidates.append((log, True))
        if len(candidates) != 1:
            return None
        buy, coin1 = candidates[0]
        for log, (in0, in1, out0, out1) in swaps:
            if log is buy:
                continue
            if not ((in1 > 0 and in0 == out1 == 0 and out0 > 0) if coin1
                    else (in0 > 0 and in1 == out0 == 0 and out1 > 0)):
                return None
        tax = _v2_buy_tax(transfers, mint, 1)
        main = max(transfers, key=lambda item: item[1])
        if tax is None or main[0] != _topic_address(buy['topics'][2]):
            return None
        return buy, tax
    except (TypeError, ValueError, KeyError):
        return None


# ---------------------------------------------------------------------------------- our side

def route_pool(chain, route):
    """The pool our quote's coin leg trades, or None when no single pool is known before the fill
    (V3 and curve rungs, Relay, Kyber, LI.FI: memecoin_executed_pool.py says the same)."""
    if not isinstance(route, dict):
        return None
    protocol = route.get("protocol")
    try:
        if protocol == "uniswap_v4":
            pool = str(route.get("pool_id") or "").lower()
            return pool if re.fullmatch(r"0x[0-9a-f]{64}", pool) else None
        if protocol == "uniswap_v4_multihop":
            return v4_pool_id(route.get("coin_key"))
        if protocol == "uniswap_v2":
            entry = V2_FACTORIES.get((str(chain), str(route.get("router") or "").lower()))
            tokens = [str(t).lower() for t in route.get("tokens") or ()]
            if entry is None or len(tokens) < 2:
                return None
            return _v2_pair(entry[0], entry[1], tokens[-2], tokens[-1])
    except (TypeError, ValueError):
        return None
    return None


def _v2_pair(factory, init_hash, token_a, token_b):
    t0, t1 = sorted((token_a, token_b), key=lambda t: int(t, 16))
    salt = keccak(bytes.fromhex(t0[2:]) + bytes.fromhex(t1[2:]))
    raw = keccak(b"\xff" + bytes.fromhex(factory[2:]) + salt + bytes.fromhex(init_hash[2:]))
    return "0x" + raw[12:].hex()


# ---------------------------------------------------------------------------------- the reference

def enabled(chain):
    from sniper.services.memecoin_chains import canonical_chain_id
    from sniper.settings import memecoin as cfg
    return (str(getattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "avg_fill")) == "post_trade"
            and canonical_chain_id(chain) in cfg.memecoin_evm_post_trade_chains())


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def resolve(chain, *, avg_price, post, our_pool):
    """(price, basis, note) for a chase gate. basis: avg_fill | post_trade | post_trade_clamped.
    The note says why the average was kept, or which pool the post-trade price came from."""
    avg = _positive(avg_price)
    if avg is None or not enabled(chain):
        return avg_price if avg is None else avg, "avg_fill", None
    if not isinstance(post, dict):
        return avg, "avg_fill", "no_ratio none"
    if "ratio" not in post:
        return avg, "avg_fill", "no_ratio %s" % post.get("reason", "none")
    ratio = _positive(post.get("ratio"))
    if ratio is None or not RATIO_MIN <= ratio <= RATIO_MAX:
        return avg, "avg_fill", "no_ratio ratio_out_of_range"
    source = "%s pool %s" % (post.get("venue"), str(post.get("pool") or "").lower())
    if not our_pool:
        return avg, "avg_fill", "route_pool_unknown; source %s" % source
    if str(our_pool).lower() != str(post.get("pool") or "").lower():
        return avg, "avg_fill", "pool_mismatch ours %s; source %s" % (str(our_pool).lower(), source)
    from sniper.settings import memecoin as cfg
    ceiling = cfg.chase_post_trade_max_ratio()
    note = "%s ratio %.4f" % (source, ratio)
    if ratio > ceiling:
        return avg * ceiling, "post_trade_clamped", "%s; post_trade_clamped at %sx the average fill" % (
            note, ceiling)
    return avg * ratio, "post_trade", note


def pre_quote_reference(ev, stats):
    """(price, basis, note) for the runner's pre-quote chase leg: that leg compares the DISPLAY
    price, so the pool to bind is the display pair (a V4 PoolId or a pair address)."""
    ref = getattr(ev, "evm_chase_reference", None)
    avg = getattr(ev, "their_price", None)
    if ref is None:
        return avg, "avg_fill", None
    return ref.resolve(avg, (stats or {}).get("pair_address"))


_PENDING = object()


class EvmChaseReference:
    """The source's post-trade facts for one event: a decoded dict (the on-chain lane, from the
    watcher's receipt) or a Future (the J7 lane's own receipt read). resolve() PEEKS and never
    waits: a read that has not finished is `receipt_pending`, and the gate keeps the average."""

    def __init__(self, chain, post=None, future=None):
        self.chain, self._post, self._future = str(chain), post, future

    def post(self):
        if self._future is None:
            return self._post
        if not self._future.done():
            return _PENDING
        try:
            return self._future.result(timeout=0)
        except Exception:
            return {"reason": "receipt_read_failed"}

    def resolve(self, avg_price, our_pool):
        post = self.post()
        if post is _PENDING:
            avg = _positive(avg_price)
            if avg is None or not enabled(self.chain):
                return avg_price if avg is None else avg, "avg_fill", None
            return avg, "avg_fill", "receipt_pending"
        return resolve(self.chain, avg_price=avg_price, post=post, our_pool=our_pool)

    def payload(self):
        """The decoded facts for a retry payload, or None while pending."""
        post = self.post()
        return None if post is _PENDING else post


# ---------------------------------------------------------------------------------- the J7 read

_RAYBOT = re.compile(r"^swap:raybot:(0x[0-9a-f]{64}):(0x[0-9a-f]{40}):(0x[0-9a-f]{40})$")
_POOL = None
_POOL_LOCK = threading.Lock()
_COOL = {}                            # url -> monotonic time the host may be asked again
_COOL_LOCK = threading.Lock()
COOLDOWN_S = 60.0
READ_TIMEOUT_S = 1.0


def raybot_origin(origin):
    """(tx_hash, trader, mint) from a J7 EVM origin id, or None."""
    m = _RAYBOT.match(str(origin or "").lower())
    return m.groups() if m else None


def _positive_receipt_integer(value):
    """An RPC integer that fits the observational SQLite ledger, never bool or fractional."""
    if type(value) is int:
        number = value
    elif (isinstance(value, str) and len(value) <= 19
          and re.fullmatch(r"(?:[0-9]+|0x[0-9a-fA-F]+)", value)):
        number = int(value, 16 if value.startswith("0x") else 10)
    else:
        return None
    return number if 0 < number < 2 ** 63 else None


def receipt_observation(chain, tx_hash, receipt, *, require_success=True):
    """Measurement-only block facts from an exact, coherent receipt. No wallet/venue authority.

    Source receipts must succeed. The timing resolver may also measure where OUR reverted
    transaction landed; that is the only consumer of require_success=False.
    """
    chain, expected = str(chain), str(tx_hash or "").lower()
    network = _positive_receipt_integer(chain)
    if (not chain.isdigit() or network is None or not re.fullmatch(r"0x[0-9a-f]{64}", expected)
            or not isinstance(receipt, dict)
            or str(receipt.get("transactionHash") or "").lower() != expected):
        return None
    status = receipt.get("status")
    if isinstance(status, bool) or str(status).lower() not in (
            ("0x1", "1") if require_success else ("0x0", "0", "0x1", "1")):
        return None
    block = _positive_receipt_integer(receipt.get("blockNumber"))
    block_hash = receipt.get("blockHash")
    if (block is None or (block_hash is not None
                         and not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(block_hash)))):
        return None
    if ("removed" in receipt and receipt["removed"] is not False
            or "chainId" in receipt and _positive_receipt_integer(receipt["chainId"]) != network):
        return None
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        return None
    for log in logs:
        if (not isinstance(log, dict) or "removed" in log and log["removed"] is not False
                or "transactionHash" in log and str(log["transactionHash"]).lower() != expected
                or "blockNumber" in log and _positive_receipt_integer(log["blockNumber"]) != block
                or "blockHash" in log and (block_hash is None
                                           or str(log["blockHash"]).lower() != str(block_hash).lower())):
            return None
    observation = {"network_id": chain, "source_tx": expected, "source_block_number": block}
    if block_hash is not None:
        observation["source_block_hash"] = str(block_hash).lower()
    return observation


def _pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="evm-post-trade")
        return _POOL


def submit_receipt_reference(chain, tx_hash, mint, owner, *, read_fn=None, pool_key_fn=None):
    """Start the source receipt read and decode on the 2-worker pool; returns its Future."""
    read_fn = read_fn or default_read_fn(chain)

    def work():
        started = time.perf_counter()
        try:
            receipt = read_fn("eth_getTransactionReceipt", [tx_hash])
        except Exception:
            return {"reason": "receipt_read_failed",
                    "receipt_ms": round((time.perf_counter() - started) * 1000.0, 1)}
        ms = round((time.perf_counter() - started) * 1000.0, 1)
        if receipt is None:
            return {"reason": "receipt_null", "receipt_ms": ms}
        out = source_post_trade(chain, receipt, mint, owner=owner, pool_key_fn=pool_key_fn,
                                infinity_read_fn=read_fn)
        # HOOKERS (2026-09-30): an unknown PoolKey here is the same key our V4 rung is about to
        # scan for. Warm it on THIS worker, off the entry path, and decode again when it was found.
        if out.get("reason") == "v4_key_unknown" and warm_v4_keys(chain, receipt, mint):
            out = source_post_trade(chain, receipt, mint, owner=owner, pool_key_fn=pool_key_fn)
        observation = receipt_observation(chain, tx_hash, receipt)
        return dict(out, receipt_ms=ms,
                    **({"source_receipt_observation": observation} if observation is not None else {}))
    return _pool().submit(work)


def read_host(chain):
    """MEMECOIN_ONCHAIN_EVM_TX_READ_URL_<chain> (the host the watcher reads a receipt from), or None.
    Never the funded RPC (Beta F2): no dedicated host means no J7 read and the average fill."""
    return (os.getenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_%s" % chain) or "").strip() or None


def default_read_fn(chain):
    """A reader on read_host(chain): a curl User-Agent (publicnode 403s Python's), a 1 s SOCKET
    timeout (it does not bound DNS; nothing waits on this read, the gates peek), no retry, and a 60 s
    process-local cooldown after a 403 or 429."""
    url = read_host(chain) or ""

    def read(method, params):
        if not url:
            raise RuntimeError("no read host")
        with _COOL_LOCK:
            if time.monotonic() < _COOL.get(url, 0.0):
                raise RuntimeError("read host cooling")
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "curl/8.5.0"})
        try:
            with urllib.request.urlopen(req, timeout=READ_TIMEOUT_S) as resp:
                answer = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                with _COOL_LOCK:
                    _COOL[url] = time.monotonic() + COOLDOWN_S
            raise
        if not isinstance(answer, dict) or answer.get("error") is not None:
            raise RuntimeError("rpc error")              # an error is never read as a null receipt
        return answer.get("result")
    return read
