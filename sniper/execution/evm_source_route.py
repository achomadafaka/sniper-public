"""The source's own pool as an entry-ladder hint (2026-10-15, operator: "whatever make us fill the
fastest"). 4663 quote_initial ran p50 876 ms, p90 1508 ms over 12 submits since 10-13, almost all of
it route discovery: the v4 rung's registry call and Initialize getLogs, the multihop rung's provider
pool lists (p50 252 ms, p90 1092 ms) before its path quotes. The pool the source bought through is
already on the event: the on-chain lane's receipt decode and the 4663 fast lane's native payload
both put it on `ev.evm_chase_reference` (`evm_post`), and the executor receives that object.

This module only answers three questions, all without a network read:
  * which V4 pool did the source use (`attempt_hint`), from that reference or, when the event has
    none, from a pool an earlier source hint proved on this coin (`remember`/`forget`);
  * what is its PoolKey (`local_key`), from our durable key store or the volume tape's pool_meta,
    hash-verified against the pool id and required to contain the coin.
The ladder decides what to do with it (`evm_real_seams`); a hint that does not resolve is a miss
there, never a refusal, because today's rungs run beside it."""
from __future__ import annotations

import threading
import time

# Every rung that quotes the source's own pool. Consumers derive their lists from this tuple
# (tests/test_memecoin_evm_bridge_prefix_cache.py checks each one), so a new rung cannot miss one.
SOURCE_RUNGS = ("v4_source", "v4_multihop_source", "v4_multihop_pinned")
# The family whose pin re-quotes a source winner (same pool or path, same builder).
SOURCE_FAMILY = {"v4_source": "v4", "v4_multihop_source": "v4_multihop",
                 "v4_multihop_pinned": "v4_multihop"}
CACHE_TTL_S = 600.0
CACHE_MAX_COINS = 512
_NATIVE = "0x" + "0" * 40

_lock = threading.Lock()
_pools = {}                     # (chain, mint) -> {pool_id: expires_monotonic}


def enabled(chain):
    from sniper.settings import memecoin as cfg
    return str(chain) in cfg.memecoin_evm_source_route_hint_chains()


def _pool_id(value):
    from sniper.execution.evm_v4_quote import _is_v4_pool_id
    pool = str(value or "").lower()
    try:
        return pool if _is_v4_pool_id(pool) and len(bytes.fromhex(pool[2:])) == 32 else None
    except ValueError:
        return None


def _pool_ids(value):
    if not isinstance(value, (list, tuple)) or not 2 <= len(value) <= 8:
        return None
    pools = tuple(_pool_id(pool) for pool in value)
    return pools if all(pools) and len(set(pools)) == len(pools) else None


def hint_from_reference(reference, *, allow_entry_hint=False):
    """The V4 pool id the source bought through, from the event's decoded receipt, else None.
    `payload()` peeks: a receipt read still in flight is no hint, never a wait. A post without a
    venue may carry `hint_pool` instead (the rh lane's v4_shared kind, whose post has no ratio).
    A split may carry bounded `hint_pools` for discovery, never a single source price.
    Only entry discovery may opt into an unbound-price `entry_hint_pool`."""
    try:
        post = reference.payload() if reference is not None else None
    except Exception:
        return None
    if not isinstance(post, dict):
        return None
    if post.get("venue") == "v4":
        return _pool_id(post.get("pool"))
    return (_pool_id(post.get("hint_pool"))
            or (_pool_id(post.get("entry_hint_pool")) if allow_entry_hint else None)
            or _pool_ids(post.get("hint_pools")))


def safety_exit_hint(chain, reference):
    """The pool for the safety's hinted sell probe (MEMECOIN_EVM_SAFETY_EXIT_HINT_CHAINS): the event's
    own receipt pool only, never a remembered one, because this hint feeds a verdict."""
    from sniper.settings import memecoin as cfg
    if str(chain) not in cfg.memecoin_evm_safety_exit_hint_chains():
        return None
    return hint_from_reference(reference)


def remembered(chain, mint, *, now=None):
    """The newest unexpired pool a source hint proved on this coin, else None."""
    now = time.monotonic() if now is None else now
    with _lock:
        pools = _pools.get((str(chain), str(mint).lower())) or {}
        live = [(exp, pool) for pool, exp in pools.items() if exp > now]
    return max(live)[1] if live else None


def remember(chain, mint, pool, *, now=None):
    """Record a pool whose source quote hit, keyed by coin AND pool."""
    pool = _pool_id(pool)
    if pool is None:
        return
    now = time.monotonic() if now is None else now
    key = (str(chain), str(mint).lower())
    with _lock:
        if key not in _pools and len(_pools) >= CACHE_MAX_COINS:
            # ponytail: drop the coin whose newest pool expires first; a dict scan of <=512.
            oldest = min(_pools, key=lambda k: max(_pools[k].values(), default=0.0))
            _pools.pop(oldest, None)
        _pools.setdefault(key, {})[pool] = now + CACHE_TTL_S


def forget(chain, mint, pool):
    """Drop one pool after its quote failed (not after a blind read)."""
    key = (str(chain), str(mint).lower())
    with _lock:
        pools = _pools.get(key)
        if pools is not None:
            pools.pop(str(pool or "").lower(), None)
            if not pools:
                _pools.pop(key, None)


# (chain, X) -> (expires_monotonic, pool_id, key, depth, prefix). X is the currency the coin's own
# pool pairs it with; prefix is the route from cash to X as ((currency, key), ...), 1 or 2 legs;
# pool_id/key/depth are its first leg's.
_bridges = {}


def bridge_cache_enabled(chain):
    from sniper.settings import memecoin as cfg
    return str(chain) in cfg.memecoin_evm_bridge_cache_chains()


def prefix_enabled(chain):
    """MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS: the full-prefix cache and its own pinned rung."""
    from sniper.settings import memecoin as cfg
    return str(chain) in cfg.memecoin_evm_bridge_prefix_chains()


def remember_bridge(chain, route, *, now=None):
    """Record the cash->X prefix of a multihop route that passed the round-trip budget, keyed by X,
    when its first leg's key hashes to the route's pool id. Two legs (cash -> X -> coin) always; three
    (cash -> middle -> X -> coin) only under MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS. The last budget-passing
    route wins: a pinned hit rewrites its own prefix, a discovery-decided route replaces it. Depth
    rides along as found: the store-side search orders by on-chain liquidity and carries no USD
    depth, the same as the search it shortcuts."""
    from sniper.execution.evm_v4_launchpad import pool_id_for_key
    try:
        legs = route.get("legs_path") or ()
        pid = str(route.get("bridge_pool_id") or "").lower()
        depth = route.get("bridge_depth_usd")
        if len(legs) not in (2, 3) or (len(legs) == 3 and not prefix_enabled(chain)):
            return
        prefix = tuple((str(leg["currency"]).lower(), dict(leg["key"])) for leg in legs[:-1])
        if pool_id_for_key(prefix[0][1]).lower() != pid:
            return
    except Exception:
        return
    x = prefix[-1][0]
    now = time.monotonic() if now is None else now
    with _lock:
        if (str(chain), x) not in _bridges and len(_bridges) >= CACHE_MAX_COINS:
            _bridges.pop(min(_bridges, key=lambda k: _bridges[k][0]), None)
        _bridges[(str(chain), x)] = (now + CACHE_TTL_S, pid, prefix[0][1],
                                     None if depth is None else float(depth), prefix)


def _live(chain, x, now):
    now = time.monotonic() if now is None else now
    with _lock:
        hit = _bridges.get((str(chain), str(x).lower()))
    return None if hit is None or hit[0] <= now else hit


def bridge_for(chain, bridge, *, now=None):
    """(pool_id, key, depth) of the unexpired cached ONE-leg prefix cash->bridge, else None. A
    two-leg prefix is not a cash->bridge pool and is never returned here."""
    hit = _live(chain, bridge, now)
    if hit is None or len(hit[4]) != 1:
        return None
    return hit[1], dict(hit[2]), hit[3]


def prefix_for(chain, x, *, now=None):
    """(prefix, first-leg pool_id, depth) of the unexpired cached route cash -> ... -> x, else None.
    Knob off: None, whatever the cache holds."""
    if not prefix_enabled(chain):
        return None
    hit = _live(chain, x, now)
    if hit is None:
        return None
    return tuple((cur, dict(key)) for cur, key in hit[4]), hit[1], hit[3]


def forget_bridge(chain, bridge):
    """Drop a cached bridge pool after its pinned path missed with an answer (not when blind)."""
    with _lock:
        _bridges.pop((str(chain), str(bridge).lower()), None)


def attempt_hint(chain, reference, mint):
    """This attempt's hint: the event's own source pool, else a remembered one, else None."""
    if not enabled(chain):
        return None
    return hint_from_reference(reference, allow_entry_hint=True) or remembered(chain, mint)


def _tape_key(chain, pool):
    """The PoolKey the volume tape resolved for `pool`, else None. Local SQLite, no network."""
    from sniper.services import memecoin_volume_writer as vw
    return vw.pool_meta_key(chain, pool)


def local_key(chain, pool, mint):
    """The hash-verified PoolKey of `pool` holding `mint`, from our own stores, else None.
    Never a network read: the in-process/durable key cache, then the volume tape."""
    from sniper.execution import evm_v4_quote as v4q
    from sniper.execution.evm_v4_launchpad import pool_id_for_key
    chain, pool, mint = str(chain), _pool_id(pool), str(mint).lower()
    if pool is None:
        return None
    for read in (lambda: v4q._pool_key_cached(chain, pool), lambda: _tape_key(chain, pool)):
        try:
            key = read()
        except Exception:
            key = None
        if not isinstance(key, dict):
            continue
        try:
            if pool_id_for_key(key).lower() != pool:
                continue
        except Exception:
            continue
        if mint not in (str(key.get("currency0")).lower(), str(key.get("currency1")).lower()):
            return None
        v4q._key_cache[(chain, pool)] = key
        return key
    return None


def hinted_pool_key(chain, hint, mint, cash_tokens):
    """Resolve on a quote worker. A split must prove every key and exactly one cash-paired pool."""
    if not isinstance(hint, (list, tuple)):
        return hint, local_key(chain, hint, mint)
    pools = _pool_ids(hint)
    if pools is None:
        return None, None
    cash_pools = []
    for pool in pools:
        key = local_key(chain, pool, mint)
        if key is None:
            return None, None
        if other_currency(key, mint) in cash_tokens:
            cash_pools.append((pool, key))
    return cash_pools[0] if len(cash_pools) == 1 else (None, None)


def other_currency(key, mint):
    """The currency paired with `mint` in `key`."""
    c0, c1 = str(key["currency0"]).lower(), str(key["currency1"]).lower()
    return c1 if c0 == str(mint).lower() else c0


def seed_pairs(chain, reference, mint):
    """Seed an entry's V2 path search with the source's own pair token (2026-10-15, operator "fill
    fastest"; MEMECOIN_EVM_SOURCE_PAIR_SEED_CHAINS). The watcher's receipt decode names the
    token the source paid into its V2/V3 leg's pool (`counter_token`); the path search tries it
    before DexScreener and asks DexScreener once only when it routes nothing. Peeks, never waits;
    True when a seed was written."""
    from sniper.settings import memecoin as cfg
    if str(chain) not in cfg.memecoin_evm_source_pair_seed_chains():
        return False
    try:
        post = reference.payload() if reference is not None else None
    except Exception:
        return False
    if not isinstance(post, dict) or post.get("venue") not in ("v2", "v3"):
        return False
    counter = str(post.get("counter_token") or "").lower()
    if not (counter.startswith("0x") and len(counter) == 42) or counter == str(mint).lower():
        return False
    from sniper.execution.evm_v2_exit_quote import seed_pair_tokens
    seed_pair_tokens(chain, mint, (counter,))
    return True
