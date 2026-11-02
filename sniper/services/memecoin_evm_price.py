"""GeckoTerminal pricing for DexScreener-blind EVM chains such as Robinhood 4663.

DexScreener indexes no pair for 4663: 323,801 of 323,801 capture observations came back
`missing_pair` (100%). So every 4663 paper position closes `unquotable` at exactly $0.00, we hold
ZERO price history for the chain, and no price rail (TP/stop/trail) can ever nominate on it.
GeckoTerminal supplies a keyless indexed price to the paper and real sweeps, so marks accumulate and
the chain becomes measurable without coupling observation to an execution provider.

Design notes:
  * `price_for(chain, mint) -> usd | None` is injected, so the batch logic unit-tests without HTTP.
  * A mint we cannot price is OMITTED from the map — never emitted with price 0.0. A zero would
    read as a -100% tick and fire every stop in the book.
  * Liquidity is unknown from this price endpoint -> None. `slippage_frac` treats unknown liquidity
    as the conservative slippage cap, so the paper fill degrades PESSIMISTICALLY. Never invent a
    liquidity number: that would fabricate an optimistic fill.
  * Bounded by `cap` probes per sweep, and the runner calls it on a slower cadence than the ~15s
    DexScreener sweep — the point is a usable price series, not a real-time tape.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Per-mint probes and bracket_pass run SYNCHRONOUSLY on the poll loop, so an unbounded
# pass (34 EVM mints open today) would stall the copy path for ~34s. Cap it and rotate internally on
# least-recently-attempted mints, so every coin is still covered, just over several passes. A
# coarse-but-honest 4663 series beats the current situation (no price at all, $0 fictional closes).
DEFAULT_CAP = 8


# Rotation state, keyed by (network_id, mint). Rotation MUST be driven by last ATTEMPT, not last
# success: 20 open `evm-unknown` mints are not on the allowlist and never resolve, so they never
# price. Rotating on last success left their timestamp empty forever -> they sorted first on every
# pass and ate the whole cap, starving the 13 real 4663 coins (measured: 1 mark in 90s).
_last_attempt: dict = {}
_fail_streak: dict = {}
_FAIL_BACKOFF_S = 900.0     # a mint that failed FAIL_TO_BACKOFF times in a row is parked this long
_FAIL_TO_BACKOFF = 3


def reset_rotation_state():
    """Test seam — the rotation caches are process-lifetime by design."""
    _last_attempt.clear()
    _fail_streak.clear()


def evm_price_batch(keys, price_for, *, mcap_for=None, cap=DEFAULT_CAP, clock=None):
    """(network_id, mint) -> {price, liquidity, mcap, source} for EVM mints only.

    Solana is skipped outright: DexScreener prices it fine, so probing it here
    would be a wasted call against the rate limit.

    LEAST-RECENTLY-ATTEMPTED first, so a bounded cap still covers the whole book over a few passes,
    and a mint that keeps failing (unresolvable chain, no indexed price) is parked so it cannot
    crowd out coins we CAN price."""
    if clock is None:
        import time
        clock = time.monotonic
    now = clock()

    candidates = []
    for key in keys or []:
        network_id, mint = key if isinstance(key, tuple) else (None, key)
        if not str(mint or "").startswith("0x"):
            continue                       # solana / malformed -> not ours
        k = (network_id, mint)
        if _fail_streak.get(k, 0) >= _FAIL_TO_BACKOFF and \
                now - _last_attempt.get(k, 0.0) < _FAIL_BACKOFF_S:
            continue                       # parked: repeatedly unpriceable, don't burn the cap
        candidates.append(k)
    candidates.sort(key=lambda k: _last_attempt.get(k, 0.0))

    out = {}
    for k in candidates[:cap]:
        network_id, mint = k
        _last_attempt[k] = now             # stamp the ATTEMPT, success or not
        try:
            result = price_for(network_id, mint)
        except Exception:
            # one dead mint must never take down pricing for the rest of the book
            logger.exception("evm price probe failed chain=%s mint=%s", network_id, mint)
            result = None
        # `price_for` may return a bare price from an injected probe OR a (price, source) tuple.
        # Provider-aware paths must return the tuple so persisted provenance stays exact.
        if isinstance(result, tuple):
            px, source = result
        else:
            px, source = result, "probe"
        if not px or float(px) <= 0:
            _fail_streak[k] = _fail_streak.get(k, 0) + 1
            continue                       # unpriceable -> ABSENT, never a 0.0 tick
        _fail_streak.pop(k, None)
        # MARKET CAP is derivable honestly (price x on-chain totalSupply) and the operator's
        # real-positions table + the mcap exit-bands both need it — DexScreener gives us neither on
        # a blind chain. LIQUIDITY and 24h VOLUME are not supplied by the per-mint seam, so they stay
        # None: an unknown must never be dressed up as a number.
        mcap = None
        if mcap_for is not None:
            try:
                mcap = mcap_for(network_id, mint, float(px))
            except Exception:
                logger.exception("evm mcap probe failed chain=%s mint=%s", network_id, mint)
                mcap = None                # a bad supply read must never cost us the PRICE
        out[k] = {"price": float(px), "liquidity": None,
                  "mcap": (float(mcap) if mcap else None), "source": source}
    return out


def build_evm_price_for(*, blind_chains=None, http=None, resolve_fn=None, _gt_fn=None):
    """Bind `price_for(chain, mint)` to the GeckoTerminal price seam.

    Returns `(price, "geckoterminal")` or None. A missing indexed price remains missing; this path
    has no execution-provider or RPC fallback. `_gt_fn` is the test seam.

    Deliberately independent of the real-leg contexts: those exist only when MEMECOIN_REAL_ENABLED
    is armed, but PAPER needs 4663 prices regardless — that paper tape is the only way the chain
    ever becomes measurable or gets an exit policy fitted.

    `resolve_fn` recovers the chain for a mint the feed tagged 'evm-unknown' (78 open positions
    today); an unresolvable mint stays unpriceable rather than being quoted against a guessed chain.
    """
    from sniper.settings import memecoin as cfg

    # ONLY chains DexScreener is BLIND to. On Base/BSC/Ethereum DexScreener works, so a transient
    # miss must be SKIPPED, not back-filled: no mark beats a wrong mark.
    allowed = set(blind_chains or cfg.MEMECOIN_DEX_BLIND_CHAINS or ())
    if resolve_fn is None:
        try:
            from sniper.execution.solana_quote import resolve_evm_chain as resolve_fn
        except Exception:
            resolve_fn = None

    # `_gt_fn` overrides for tests; otherwise bind the live client unless disabled by config.
    if _gt_fn is not None:
        gt_fn = _gt_fn
    elif cfg.MEMECOIN_GT_ENABLED:
        from sniper.execution.geckoterminal_quote import gt_price_for as _gt_price_for
        def gt_fn(chain, mint):
            return _gt_price_for(chain, mint, http=http)
    else:
        gt_fn = None

    def price_for(chain, mint):
        chain = str(chain or "").strip()
        if chain in ("", "evm-unknown") and resolve_fn is not None:
            chain = resolve_fn(mint) or ""   # unresolvable -> stays unpriceable, never guessed
        if chain not in allowed:
            return None                      # DexScreener covers this chain -> NEVER price it from a
                                             # secondary source (RUSSELL +$785.66 lesson; GT included)
        # GeckoTerminal first. Only reached AFTER the blind-chain gate, so GT never touches a
        # DexScreener-covered chain. An index USD price -> tagged 'geckoterminal'.
        if gt_fn is not None:
            g = gt_fn(chain, mint)
            if g and float(g) > 0:
                return (float(g), "geckoterminal")
        return None

    return price_for


_SEL_TOTAL_SUPPLY = "18160ddd"     # keccak("totalSupply()")[:4]
_supply_cache: dict = {}


def evm_total_supply(chain, mint, *, http=None):
    """(supply, cache_hit, read_ms): the coin's on-chain totalSupply in whole tokens, cached per
    (chain, mint) on SUCCESS only; (None, False, ms) on any failure, which caches nothing, so a
    failed read is never answered from a stale value. A miss reads decimals and totalSupply on the
    chain's custody RPC (two overlapping eth_calls); a hit reads nothing and reports 0 ms."""
    import time as _time
    from concurrent.futures import ThreadPoolExecutor
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import erc20_decimals
    from sniper.execution.evm_real_seams import eth_call_raw
    key = (str(chain), mint)
    supply = _supply_cache.get(key)
    if supply is not None:
        return supply, True, 0.0
    t0 = _time.monotonic()

    def done(value):
        return value, False, round((_time.monotonic() - t0) * 1000.0, 1)
    rpc = cfg.evm_rpc_for_chain(str(chain))
    # Independent reads must fit the existing bounded entry wait, not pay two RPC round trips.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="evm-supply-raw") as pool:
        raw_future = pool.submit(eth_call_raw, mint, "0x" + _SEL_TOTAL_SUPPLY, rpc, http=http)
        dec = erc20_decimals(mint, rpc, http=http)
        raw = raw_future.result()
    if dec is None or not 0 <= int(dec) <= 36:
        return done(None)
    if not raw:
        return done(None)
    try:
        supply = int(raw, 16) / (10 ** int(dec))
    except (TypeError, ValueError):
        return done(None)
    if not supply > 0:
        return done(None)
    _supply_cache[key] = supply
    return done(supply)


def cached_total_supply(chain, mint):
    """The supply evm_total_supply already holds for (chain, mint), or None. Reads nothing."""
    return _supply_cache.get((str(chain), mint))


def build_evm_mcap_for(*, chains=None, http=None, resolve_fn=None):
    """Bind `mcap_for(chain, mint, price) -> usd | None`: price x on-chain totalSupply.

    DexScreener has no pair on 4663, so market cap is otherwise simply unknown — which is why every
    non-Solana row in the real-positions table shows a blank. totalSupply is a plain ERC-20 read and
    is effectively constant, so it is cached per mint: at most ONE extra eth_call per new coin.

    A rebasing/mintable token would drift, which is exactly the kind of coin the token-safety gate
    already refuses (live mint authority), so a cached supply is honest for anything we would trade.
    """
    from sniper.settings import memecoin as cfg

    allowed = set(chains or cfg.MEMECOIN_EVM_ALLOWLIST or ())
    if resolve_fn is None:
        try:
            from sniper.execution.solana_quote import resolve_evm_chain as resolve_fn
        except Exception:
            resolve_fn = None

    def mcap_for(chain, mint, price):
        chain = str(chain or "").strip()
        if chain in ("", "evm-unknown") and resolve_fn is not None:
            chain = resolve_fn(mint) or ""
        if chain not in allowed or not price:
            return None
        supply, _hit, _ms = evm_total_supply(chain, mint, http=http)
        if supply is None:
            return None
        return float(price) * supply

    return mcap_for
