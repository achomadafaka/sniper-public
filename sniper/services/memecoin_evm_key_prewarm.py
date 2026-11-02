"""Warm the V4 PoolKey for a signal's mint BEFORE we need it to quote.

THE GAP THIS CLOSES. `_pool_key` resolves a pool's immutable PoolKey from the PoolManager's
Initialize event. A hit in the durable store costs ~10ms; a live resolve costs ~840ms and can fail
outright under load. Measured 2026-09-02 on 4663:

    funded attempts that RESERVED capital, 48h            475
    of those, FILLED                                       64   (13%)
    died on "no quote" (110 no_route + 43 could-not-read) 153   <- one in three, the top cause
    v4 rung hit rate                                    11.3%
    PoolKeys stored against pools known                216 of 30,433

MUSTANG, 2026-09-02: every direct rung missed, v4 read failed, relay was the only answer and
wanted 227% over market on a $10k pool. Thirty seconds later the same pool keys resolved by hand
off the critical path — one from the store in 10ms, one live in 839ms. The key was resolvable; we
just asked for it at the worst possible moment.

WHY THIS IS NOT THE BACKFILL THAT WAS DROPPED. A retrospective warm over coins we already quoted is
worthless here: 58% of quoted mints are quoted exactly ONCE, and the median repeat arrives 0.1h
later, inside one process lifetime where the in-memory cache already answers. This is the opposite
shape — it warms the coin arriving NOW, keyed on the signal, ahead of the decision that needs it.
The sweep warms what we already HOLD, which is precisely the wrong set for an entry.

BOUNDS, because a signal burst must never become an RPC burst. A rate cap per minute, an overflow
that drops the OLDEST pending mint with a counter, and a per-mint TTL so a hot mint enqueued on
every poll is resolved once. Deliberately a SEPARATE queue from the Solana signal pool resolver:
one shared rate budget across two chains would let a Solana burst starve 4663, and the two have
different failure domains.

IT NEVER TOUCHES THE ENTRY PATH. Enqueue is cheap and synchronous — a dict lookup and a list
append, no I/O beyond an optional indexed store check. Resolution happens on its own thread with
its own connections. It runs AHEAD of the decision or it does not run: nothing here can delay,
block or fail an entry.
"""
from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

COUNTS = {"enqueued": 0, "resolved": 0, "unresolved": 0, "failed": 0,
          "dropped_full": 0, "dropped_rate": 0, "deduped": 0}

_LOCK = threading.Lock()
_PENDING = []          # FIFO of (chain_id, mint)
# Chain 1 is enqueued BEFORE the gates for every J7 buy and would resolve pools on the metered Infura
# custody key (Gamma M-A, 2026-09-30). Requires an explicit separate read host; custody is never used.
PREWARM_EXCLUDED_CHAINS = frozenset({"1"})
_SEEN = {}             # (chain, mint) -> monotonic stamp, the TTL dedup
_RECENT = []           # monotonic stamps of completed resolutions, for the rate bound


def reset_state():
    """Test hook. Production never calls this."""
    with _LOCK:
        _PENDING.clear()
        _SEEN.clear()
        _RECENT.clear()
        for k in COUNTS:
            COUNTS[k] = 0


def enqueue(chain_id, mint, *, now, ttl_s=900.0, max_pending=64, has_key_fn=None):
    """Queue one (chain, mint) for PoolKey warming. True when newly queued.

    Called from the observation path, so it must stay cheap: a dict lookup, an optional indexed
    store check, a list append. No network, no blocking, and every failure returns rather than
    raises — a warm that cannot be scheduled must never affect the entry it was scheduled for.
    """
    if not mint or not chain_id:
        return False
    if (str(chain_id) in PREWARM_EXCLUDED_CHAINS
            and not os.getenv("MEMECOIN_EVM_KEY_PREWARM_RPC_URL_%s" % chain_id, "").strip()):
        return False
    key = (str(chain_id), str(mint).lower())
    with _LOCK:
        stamp = _SEEN.get(key)
        if stamp is not None and (float(now) - float(stamp)) < float(ttl_s):
            COUNTS["deduped"] += 1
            return False
    if has_key_fn is not None:
        try:
            if has_key_fn(key[0], key[1]):
                return False                 # already stored; nothing to warm
        except Exception:
            pass                             # an unreadable check is not a reason to skip
    with _LOCK:
        _SEEN[key] = float(now)
        for k, when in list(_SEEN.items()):
            if float(now) - float(when) > float(ttl_s) * 4:
                _SEEN.pop(k, None)           # bounded: the dedup memory cannot grow forever
        if key in _PENDING:
            return False
        if len(_PENDING) >= int(max_pending):
            dropped = _PENDING.pop(0)        # oldest first: it has waited longest and is stalest
            COUNTS["dropped_full"] += 1
            logger.warning("evm key prewarm queue full (%d); dropped %s/%s",
                           int(max_pending), dropped[0], dropped[1])
        _PENDING.append(key)
        COUNTS["enqueued"] += 1
        return True


def _rate_ok(now, *, max_per_min):
    """True when another resolution fits inside the rate bound. Prunes as it goes."""
    cutoff = float(now) - 60.0
    while _RECENT and _RECENT[0] < cutoff:
        _RECENT.pop(0)
    return len(_RECENT) < int(max_per_min)


def take_next(now, *, max_per_min=10):
    """The next (chain, mint) to warm, or None when empty or rate-bound."""
    with _LOCK:
        if not _PENDING:
            return None
        if not _rate_ok(now, max_per_min=max_per_min):
            COUNTS["dropped_rate"] += 1
            return None
        _RECENT.append(float(now))
        return _PENDING.pop(0)


def resolve(chain, mint):
    """Resolve at most four pools in the worker process. Keep this outside the entry path."""
    from sniper.execution.evm_v4_quote import _pool_key, _pools_for_mint, warm_pool_key_families
    from sniper.settings.memecoin import evm_rpc_for_chain
    http = None
    if str(chain) == "1":
        rpc, http = _ethereum_reader()
    else:
        rpc = evm_rpc_for_chain(chain)
    if not rpc:
        return 0
    warm_pool_key_families(chain)
    return sum(bool(_pool_key(chain, pid, rpc, **({"http": http} if http else {})))
               for pid in (_pools_for_mint(chain, mint) or [])[:4])


def _ethereum_reader():
    """One separate host, sixteen reads / five seconds per job; never custody fallback."""
    import json
    from urllib.parse import urlsplit
    from sniper.execution.evm_wallet import _http
    from sniper.execution.evm_rpc_pool import RequestNotSent
    from sniper.execution.evm_v4_multihop import _SearchBudget, _BudgetedHttp
    from sniper.services.memecoin_evm_exact_capture import budget, taken_budgets
    name = "MEMECOIN_EVM_KEY_PREWARM_RPC_URL_1"
    env = dict(os.environ)
    rpc = env.pop(name, "").strip()
    if not rpc:
        return None, None
    parts = urlsplit(rpc)
    if parts.scheme != "https" or not parts.hostname or budget(rpc) in taken_budgets(env):
        raise ValueError("Ethereum key prewarm requires a separate HTTPS read budget")
    limits = _SearchBudget(deadline_s=5.0, max_requests=16)

    def read(method, url, **kwargs):
        request = json.loads(kwargs.get("body") or "{}")
        if (method != "POST" or url != rpc
                or request.get("method") not in {"eth_chainId", "eth_blockNumber", "eth_getLogs"}):
            raise RequestNotSent("Ethereum key prewarm endpoint or method excluded")
        kwargs["timeout"] = min(float(kwargs.get("timeout", 3.0)), limits.remaining_s())
        return _http(method, url, **kwargs)

    http = _BudgetedHttp(limits, read, discovery=True)
    status, answer, _headers = http("POST", rpc, body=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}))
    try:
        valid = status == 200 and int(str(answer.get("result")), 16) == 1
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Ethereum key prewarm chain identity unavailable or wrong")
    return rpc, http


def record_outcome(stored):
    """Fold one warm attempt into the counters. `stored` is the count of keys persisted, or None
    for a failure — the two are different and must not both read as 'unresolved'."""
    with _LOCK:
        if stored is None:
            COUNTS["failed"] += 1
        elif stored:
            COUNTS["resolved"] += 1
        else:
            COUNTS["unresolved"] += 1


def snapshot():
    """Heartbeat projection. Pending depth plus the counters, so a queue that is silently full or
    permanently rate-bound is visible rather than inferred from an absence of effect."""
    with _LOCK:
        return {"pending": len(_PENDING), **dict(COUNTS)}
