"""Read-only EVM JSON-RPC primitives with no signing or submission imports."""
import logging
import os

from sniper.execution.evm_wallet import _rpc

logger = logging.getLogger(__name__)


def _fresh_read_enabled():
    return os.getenv("MEMECOIN_EVM_FRESH_READ_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on"}


def _freshest_read_member(rpc_url, method="eth_call"):
    """The least stale healthy pool member for a READ, or `rpc_url` unchanged.

    A SIMULATION ON STATE THE CHAIN HAS LEFT BEHIND COSTS GAS (2026-09-22). The custody endpoint on
    4663 runs 6-23 blocks behind the public member, median 8 -- about 0.6 s at 12-13 blocks a
    second -- and it does not merely lag, it STOPS: in 13 consecutive 1.5 s samples it failed to
    advance once, and that is the sample where the gap reached 23 blocks and 1.8 s. The `row`
    entry (0xb30fcc1d) is what that costs: its preflight PASSED at 21:40:21.35 on custody state and
    the chain REVERTED the transaction two seconds later, status 0x0, 115,763 gas, no logs.

    On a bonding curve with no depth, 1.8 s is many buys, and a minimum computed from state that
    old is unreachable by the time the transaction lands.

    THIS CANNOT TOUCH SIGNING, BY CONSTRUCTION RATHER THAN BY DISCIPLINE. Only `eth_call` reads
    pass through here; nonces, gas, signing and broadcast never call it, so custody keeps
    everything that spends. That is why the resolver lives in this one function instead of in a
    dozen call sites where one could be missed.

    NOTHING IS CACHED. Health, quarantine and per-method refusals all move on their own, and a
    remembered "freshest" member is exactly how a lane keeps talking to an endpoint that has
    started refusing. Every call re-asks, which costs a dict lookup.
    """
    if not _fresh_read_enabled():
        return rpc_url
    try:
        from sniper.execution import evm_rpc_pool as pool
        best, best_lag = rpc_url, pool.lag_ms(rpc_url)
        for url in pool.pool_for(rpc_url):
            if not pool.available(url) or pool.refuses(url, method):
                continue
            if url != rpc_url and pool.identity_state(url) is not True:
                continue                      # freshness cannot establish chain identity
            lag = pool.lag_ms(url)
            if lag is None:
                continue                      # unmeasured is not known-fresh
            if best_lag is None or lag < best_lag:
                best, best_lag = url, lag
        # A member that refuses this method or sits in quarantine must not be returned even when
        # it IS the configured url -- but we have nothing better to offer, so the caller's own url
        # stands and the pool's normal failure handling deals with it.
        return best
    except Exception:
        logger.exception("fresh read member lookup failed; using the configured url")
        return rpc_url


def eth_call_raw(to, data, rpc_url, *, http=None, overrides=None, from_addr=None,
                 value_wei=None, raise_on_error=False):
    call = {"to": to, "data": data}
    if from_addr:
        call["from"] = from_addr
    if value_wei:
        call["value"] = hex(int(value_wei))
    params = [call, "latest"] + ([overrides] if overrides else [])
    response = _rpc(_freshest_read_member(rpc_url), "eth_call", params, http=http)
    if "error" in response:
        if raise_on_error:
            raise RuntimeError("eth_call RPC error")
        return None
    return response.get("result")
