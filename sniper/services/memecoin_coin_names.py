"""A readable name for a coin whose row has no ticker (2026-09-28, operator order).

The slot lane and the on-chain watcher open positions with no ticker (26 of 281 funded positions
from 09-20 to 09-28: 21 slot lane, 5 on-chain), so the dashboard and the Telegram cards showed a
mint code. The ledger names most of them (a J7 event on the same mint: 11 of 26); DexScreener named
13 of the other 15.

Order: this process's cache, then the ledger (memecoin_events, indexed on mint), then ONE
DexScreener token batch for NAMES ONLY (the research breaker cell, never the funded one). A name is
kept for the life of the process; a miss is asked again at most every 10 minutes.

`wait=False` (the web's 4 s polls) hands the batch to a background thread, one at a time, so a poll
never waits on an external call. `wait=True` (the notification worker's own thread, never a trading
thread) runs it inline under DexScreener's 6 s deadline, so the first card already carries the name.
`cached()` never does I/O: the runner's text alerts format on the trading thread and may only read
what the worker has already resolved.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

RETRY_S = 600.0
BATCH_MAX = 30
_LOCK = threading.Lock()
_NAMES = {}
_ASKED = {}
_BUSY = [False]


def _key(network_id, mint):
    return (str(network_id or "solana"), str(mint))


def cached(network_id, mint):
    """The name this process already knows, or None. No I/O."""
    with _LOCK:
        return _NAMES.get(_key(network_id, mint))


def cached_for_mint(mint):
    """The same, for a formatter that carries no chain. No I/O."""
    if not mint:
        return None
    with _LOCK:
        for (_net, known), name in _NAMES.items():
            if known == mint:
                return name
    return None


def _default_batch(keys):
    from sniper.execution.solana_quote import dexscreener_stats_batch_keyed
    return dexscreener_stats_batch_keyed(keys)


def _lookup(keys):
    try:
        from sniper.execution.solana_quote import norm_mint
        stats = _default_batch(keys) or {}
        found = {}
        for key in keys:
            symbol = (stats.get((key[0], norm_mint(key[1]))) or {}).get("symbol")
            if isinstance(symbol, str) and symbol.strip():
                found[key] = symbol.strip()
        with _LOCK:
            _NAMES.update(found)
    except Exception:
        logger.exception("coin name lookup failed for %d coins", len(keys))


def names(conn, keys, *, wait=False, now=None):
    """{(network_id, mint): name} for the keys it can name now. See the module doc."""
    from sniper.persistence import db_memecoin as m
    now = time.monotonic() if now is None else now
    out, misses = {}, []
    for key in dict.fromkeys(_key(n, mt) for n, mt in keys if mt):
        name = cached(*key)
        if not name:
            name = m.resolve_mint_ticker(conn, key[1], network_id=key[0], include_feed=False)
            if name:
                with _LOCK:
                    _NAMES[key] = name
        if name:
            out[key] = name
        else:
            misses.append(key)
    with _LOCK:
        due = [k for k in misses if now - _ASKED.get(k, float("-inf")) >= RETRY_S][:BATCH_MAX]
        if not due or (not wait and _BUSY[0]):
            return out
        for key in due:
            _ASKED[key] = now
        if not wait:
            _BUSY[0] = True
    if wait:
        _lookup(due)
        with _LOCK:
            out.update({k: _NAMES[k] for k in due if k in _NAMES})
        return out

    def run():
        try:
            _lookup(due)
        finally:
            with _LOCK:
                _BUSY[0] = False
    threading.Thread(target=run, daemon=True, name="coin-name-lookup").start()
    return out


def name_for(conn, network_id, mint, *, wait=False):
    """One coin's name, or None."""
    return names(conn, [(network_id, mint)], wait=wait).get(_key(network_id, mint))
