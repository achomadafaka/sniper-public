"""Derive a launchpad coin's real V4 pool from the launchpad contract, with no log read.

Spec: docs/superpowers/specs/2026-09-05-memecoin-v4-launchpad-derived-discovery-design.md.

WHY A DERIVATION AND NOT A SCAN. At the first feed signal a launchpad coin's pool is 0-30 seconds
old. Measured on chain 4663 (2026-09-05): CHILL Initialize to first signal 27 s, NOTFLIX 225 s,
YIELDPAD -12 s, n00b -6 s -- the wallet bought before the Initialize block was even visible. The
chain's log index surfaces Initialize 38-61 s after the block, and one endpoint alone serves a
range `eth_getLogs`. The public indexers list the coin's fee-trap pools first and its real pool
minutes later. So log-based and indexer-based discovery miss the first signal BY CONSTRUCTION, and
every requote inside the 90-second window lands before the index catches up.

The launchpad already knows the pool it created. One `eth_call` asks it, one keccak turns the
PoolKey into the pool id, and one V4Quoter `eth_call` proves the pool answers.

WHAT THIS IS NOT. It is not the fixed-shape guess that was measured and rejected on 2026-09-03:
assuming every pool is `(fee 0, tickSpacing 200, hook 0xe5e7...)` reproduced only 8.3% of 1,500
stored pools, because the hook is an arbitrary address. Here the shape is READ from the launchpad
rather than assumed, and the derived id is confirmed by a quote before it is trusted.
"""
from __future__ import annotations

import logging
import threading

from eth_utils import keccak

logger = logging.getLogger("memecoin")

_WORD = 64                                  # one 32-byte ABI word, in hex characters
_POOL_NOT_INITIALIZED = "486aa307"          # V4 `PoolNotInitialized()` selector
_hook_cache = {}                            # (chain, launchpad) -> hook address, process lifetime
_hook_lock = threading.Lock()


def _words(raw):
    """Split an `eth_call` result into 32-byte words, or [] when there is nothing to read."""
    body = str(raw or "")[2:] if str(raw or "").startswith("0x") else str(raw or "")
    return [body[i:i + _WORD] for i in range(0, len(body) - len(body) % _WORD, _WORD)]


def _address_at(words, index):
    """The low 20 bytes of one word as a 0x address, or None when the word is absent."""
    if index >= len(words):
        return None
    return "0x" + words[index][-40:].lower()


def _int_at(words, index):
    if index >= len(words):
        return None
    try:
        return int(words[index], 16)
    except ValueError:
        return None


def _call(chain, to, data, rpc_url, *, http=None):
    """One read-only `eth_call` through the shared pooled/hedged read layer. None on any failure."""
    from sniper.execution.evm_read_rpc import _rpc
    try:
        response = _rpc(rpc_url, "eth_call", [{"to": to, "data": data}, "latest"], http=http)
    except Exception:
        _background_admit(http)
        return None
    _background_admit(http)
    if not isinstance(response, dict):
        return None
    return response.get("result")


def _background_admit(http):
    """An optional job-owned fence; ordinary inline discovery has no additional work."""
    admit = getattr(http, "raise_if_cancelled", None)
    if admit is not None:
        admit()


def _hook_for(chain, entry, rpc_url, *, http=None):
    """The launchpad's hook address, cached for the process. A launchpad's hook is immutable."""
    ck = (str(chain), entry["address"])
    with _hook_lock:
        if ck in _hook_cache:
            return _hook_cache[ck]
    raw = _call(chain, entry["address"], "0x" + entry["hook_selector"], rpc_url, http=http)
    hook = _address_at(_words(raw), 0)
    if hook is None:
        return None
    with _hook_lock:
        _hook_cache[ck] = hook
    return hook


def pool_id_for_key(key):
    """`keccak(abi.encode(PoolKey))`, which IS the V4 pool id. Shared with the quote path."""
    from sniper.execution.evm_v4_quote import _encode_pool_key
    return "0x" + keccak(bytes.fromhex(_encode_pool_key(key))).hex()


def key_from_struct(words, entry, mint):
    """The PoolKey a launchpad's coin-info struct describes, or None when it is not our coin.

    THE COIN WORD IS THE OWNERSHIP TEST. A launchpad answers for tokens it did not create with a
    struct whose coin word is some other address (FRIES and KPOP belong to a second launchpad and
    are out of scope for v1), so a struct that does not name the mint back is not evidence about
    this coin and must not become a candidate pool.
    """
    coin = _address_at(words, entry["coin_word"])
    if coin is None or coin != str(mint).lower():
        return None
    counter = _address_at(words, entry["counter_word"])
    fee = _int_at(words, entry["fee_word"])
    tick_spacing = _int_at(words, entry["tick_spacing_word"])
    if counter is None or fee is None or tick_spacing is None:
        return None
    # V4 PoolKeys are ADDRESS-SORTED, so the pair order is decided by the addresses and never by
    # which side we are trading. Native (0x00..0) sorts first against every real token.
    currency0, currency1 = sorted((counter, str(mint).lower()), key=lambda a: int(a, 16))
    return {"currency0": currency0, "currency1": currency1,
            "fee": int(fee), "tick_spacing": int(tick_spacing), "hooks": None}


def derived_pool_keys(chain, mint, rpc_url, *, http=None, note_fn=None):
    """[(pool_id, key, confirmed)] for `mint` from every registered launchpad on `chain`.

    `confirmed` is True when the V4Quoter answered a probe quote. A pool that exists in the
    launchpad's records but is NOT YET INITIALISED answers `PoolNotInitialized`; that key is
    returned unconfirmed and is NOT stored, because the caller's transient-miss requote covers the
    seconds until the pool opens and a stored key must mean a pool we have actually quoted.
    """
    from sniper.settings import memecoin as cfg
    entries = (getattr(cfg, "MEMECOIN_EVM_V4_LAUNCHPADS", {}) or {}).get(str(chain), ())
    if not entries or not rpc_url or not mint:
        return []

    def _note(reason):
        if note_fn is not None:
            try:
                note_fn(reason)
            except Exception:
                pass

    out = []
    for entry in entries:
        data = "0x" + entry["info_selector"] + str(mint).lower()[2:].rjust(64, "0")
        raw = _call(chain, entry["address"], data, rpc_url, http=http)
        if raw is None:
            _note("launchpad_read_failed")
            logger.debug("launchpad %s: info read failed for %s", entry["address"][:10], mint)
            continue
        key = key_from_struct(_words(raw), entry, mint)
        if key is None:
            _note("launchpad_not_ours")
            logger.debug("launchpad %s: %s is not its coin", entry["address"][:10], mint)
            continue
        hook = _hook_for(chain, entry, rpc_url, http=http)
        if hook is None:
            _note("launchpad_read_failed")
            continue
        key["hooks"] = hook
        pool_id = pool_id_for_key(key)
        buyable, sellable = _confirm(chain, key, mint, rpc_url, http=http)
        _background_admit(http)
        if buyable and sellable:
            verify = getattr(http, "verify_pool_key", None)
            if verify is not None:
                verify(chain, mint, pool_id, key)
            _store(chain, pool_id, key)
            out.append((pool_id, key, True))
            continue
        if buyable:
            # BUYS, DOES NOT SELL. Never stored and never confirmed: a cached key is treated as a
            # proven pool everywhere downstream, and this one would strand a position.
            _note("launchpad_pool_unsellable")
            logger.debug("launchpad pool %s for %s quotes a buy but not a sell", pool_id[:14], mint)
        else:
            _note("launchpad_pool_pending")
        out.append((pool_id, key, False))
    return out


def _confirm(chain, key, mint, rpc_url, *, http=None):
    """(buyable, sellable) for this key, each proven by its own V4Quoter probe.

    THE PROBE PROVES THE POOL, IT DOES NOT PRICE THE CLIP. The entry path quotes the real amount
    afterwards through its own ladder. A derived id that no quoter will answer must never reach the
    candidate list: a wrong key hashes to a pool that does not exist and would refuse the coin while
    looking correct.

    BOTH DIRECTIONS, BECAUSE A BUY QUOTE IS NOT EVIDENCE OF A SELL QUOTE. Measured 2026-09-05 on
    12 native-paired pools per hook family, publicnode, buy and sell at the probe size:

        0xe5e702 (shipped)   buy 12/12   sell 12/12
        0x0310cfeb           buy 12/12   sell  8/12   (4 revert)
        0x4e346895           buy 12/12   sell  8/12   (4 revert)
        0x75a54357           buy 12/12   sell  2/12   (10 revert)

    Every reverting pool priced its BUY perfectly. A one-direction probe would confirm and STORE
    them, and discovery would then hand the entry path a coin it cannot sell -- the one outcome
    this stack never accepts. The sell probe is what makes a family safe to register at all, and it
    judges the POOL rather than the family average, so the good pools inside a bad family are still
    reachable.

    The sell probe uses the buy's own `amount_out`, so it asks the exact question that matters:
    can we sell back what this pool would actually have given us.
    """
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_v4_quote import _quote_single
    probe = int(getattr(cfg, "MEMECOIN_EVM_V4_LAUNCHPAD_PROBE_ATOMIC", 10 ** 15) or 10 ** 15)
    coin_is_currency1 = str(key["currency1"]).lower() == str(mint).lower()
    try:
        bought = _quote_single(chain, key, coin_is_currency1, probe, rpc_url, http=http)
    except Exception:
        _background_admit(http)
        bought = None
    _background_admit(http)
    if not bought:
        return False, False                 # not initialised yet, or no such pool
    try:
        returned = _quote_single(
            chain, key, not coin_is_currency1, int(bought), rpc_url, http=http)
    except Exception:
        _background_admit(http)
        returned = None
    _background_admit(http)
    return True, bool(returned)


def _store(chain, pool_id, key):
    """Persist a CONFIRMED key through the existing durable cache, so later reads are free."""
    try:
        from sniper.execution.evm_v4_quote import _store_pool_key
        _store_pool_key(chain, pool_id, key)
    except Exception:
        pass                                # a cache write must never cost a discovery
