"""Honest paper-fill pricing + token stats for Solana memecoins.

Fill price chain: DexScreener mid + modeled slippage -> fomo event price. Every fill records
which source was used so paper PnL stays honest. `http` is injectable for tests:
(method, url, headers, body) -> (status, json, date).

(The Jupiter quote leg was removed 2026-07-08: `quote-api.jup.ag` is dead — unreachable, 0/73
fills ever used it — and its `lite-api` replacement omits out/inDecimals, which the old code
defaulted to 6dp, mispricing 9-decimal tokens 1000x. Reintroducing real-impact quotes needs a
per-token decimals lookup and should start a fresh fill cohort, not ride this one.)
"""
from __future__ import annotations

import json as _json
import logging
import math
import threading
import time
import urllib.request
from functools import partial
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"


def ds_tokens_url(ids):
    """The DexScreener tokens URL for `ids` (one mint or a comma list).

    CACHE BUST (2026-09-12, operator "Yes do both"). Cloudflare serves the bare URL with
    max-age=30: 92% of 90 samples were cache HITs, age p50 16 s, max 29 s, and in 37% the origin
    already had newer trades (Gamma's measurement). A unique query string reaches the origin, so
    every gate reads the index up to 30 s fresher. Request count is unchanged.
    MEMECOIN_DEXSCREENER_CACHE_BUST=0 is the rollback.
    """
    import os
    import time
    url = DEXSCREENER + str(ids)
    if os.getenv("MEMECOIN_DEXSCREENER_CACHE_BUST", "0").strip().lower() in {"1", "true", "yes"}:
        url += "?cb=%d" % time.time_ns()
    return url

# DEXSCREENER CIRCUIT BREAKER (2026-08-24). The provider stopped answering: TLS completes in 16ms
# and then nothing comes back for the full `_http` timeout -- an edge block, not a 429, so there is
# no status to key on. Every caller re-paid that timeout on every poll, and the batch pays it PER
# 30-MINT CHUNK, so the cost was ceil(N/30) x timeout. Measured: /api/memecoin/real/positions took
# 10.03s on ONE chunk while every non-DexScreener endpoint served in under 10ms.
#
# This changes LATENCY, NOT SEMANTICS. The batch already wrapped each chunk in `except: continue`,
# so a dead provider already returned {}. The breaker returns the same {} sooner. No caller sees a
# value it could not have seen before, which is why this is safe on the funded exit path --
# `real_exit_sweep` calls the batch with no `price_fn` from all three of its call sites, including
# the hot exit lane, and today would hang there the moment a position is open.
#
# TWO CELLS, copied from geckoterminal_quote deliberately. A single shared cell let a research
# throttle gag the money path and refuse live entries on 2026-08-14. DexScreener has the same
# caller mix -- research (capture/discovery) and funded (real_exit_sweep, fastlane) -- so one cell
# reproduces that bug exactly. A FUNDED failure sets both (the provider is refusing the money path
# itself); a research failure sets only the shared cell.
_DS_COOLDOWN_S = 60.0
_DS_TIMEOUT_S = 3.0          # explicit: without it the first call after every expiry re-pays 10s
_ds_cooldown_until = [0.0]
_ds_funded_cooldown_until = [0.0]


def dexscreener_cooldown_active(clock=None, *, funded=False):
    """True while the breaker suppresses DexScreener reads. In this state the batch returns {}
    with NO http call, so a caller that watches only HTTP cannot tell a provider outage from
    "this token has no pair". Ask this before recording a miss as a fact about the coin."""
    if clock is None:
        import time as _t
        clock = _t.monotonic
    cell = _ds_funded_cooldown_until if funded else _ds_cooldown_until
    return clock() < cell[0]


_ds_capacity_refusals = {False: 0, True: 0}


def _note_capacity_refusal(funded):
    from sniper.util.log_sample import log_sampled
    _ds_capacity_refusals[bool(funded)] += 1     # ponytail: GIL-racy counter, observability only
    log_sampled(logger.warning, ("ds_capacity", bool(funded)),
                "dexscreener worker slots full (funded=%s); call refused, no cooldown (total %d)",
                funded, _ds_capacity_refusals[bool(funded)])


def _ds_start_cooldown(*, funded=False, reason="", clock=None):
    import time as _t
    from sniper.util.log_sample import log_sampled
    now = (clock or _t.monotonic)()
    until = now + _DS_COOLDOWN_S
    _ds_cooldown_until[0] = max(_ds_cooldown_until[0], until)
    if funded:
        _ds_funded_cooldown_until[0] = max(_ds_funded_cooldown_until[0], until)
    log_sampled(logger.warning, ("ds_down", bool(funded)),
                "dexscreener unreachable (%s); cooling down %.0fs (funded=%s)",
                reason or "no response", _DS_COOLDOWN_S, funded)

# Exact chain identity. A 0x address can exist on several EVM chains, so the mint alone is not a
# price key. Solana base58 addresses infer Solana; EVM calls must provide a network except in the
# dedicated chain-resolution helper.
# 4663 (Robinhood) added 2026-08-30. Without it `_expected_chain` returned None, so
# `select_dex_pair` -> None -> `dexscreener_stats` -> None, and `real_entry_hook` ran with
# `st = {}` on every 4663 signal. Six funded entry gates then admitted unconditionally: the
# m5/h1 volume revival guard, the execution premium cap, the chase gate and its impact
# allowance, the sell-share cap, the coin-age ceiling and the winner-re-entry price gate.
# Measured over 30 days, the volume guard fired 348 times on solana and 0 times on 4663.
# DexScreener serves these tokens under chainId 'robinhood' (6/6 tokens, 92/92 pairs).
# "5042": "arc" verified live 2026-09-16 against api.dexscreener.com/latest/dex/tokens/
# 0x3600000000000000000000000000000000000000 (Arc's native/USDC address) — real indexed pairs,
# real volume, "chainId":"arc". DexScreener already covers Arc at mainnet launch; it is not
# DexScreener-blind, so it does not belong in MEMECOIN_DEX_BLIND_CHAINS.
FOMO_TO_DEX = {"solana": "solana", "8453": "base", "56": "bsc", "1": "ethereum",
               "4663": "robinhood", "5042": "arc"}
DEX_CHAIN_TO_FOMO = {v: k for k, v in FOMO_TO_DEX.items()}


# DexScreener / Jupiter 403 the default Python user-agent.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


def _http(method, url, headers=None, body=None, timeout=10):
    h = {"user-agent": _UA, "accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return r.status, (_json.loads(raw) if raw else {}), None


# THE WHOLE REQUEST, NOT JUST THE SOCKET (2026-09-25). urllib's timeout bounds connect and read,
# but not `getaddrinfo`: at 18:30Z a host DNS stall held the funded main loop ~100 s inside
# create_connection -> getaddrinfo on this fetch, the watchdog killed the runner for sweep_stale,
# and the entry-retry window of a missed funded buy died with it. Past this deadline the caller
# gets TimeoutError, the breaker trips as for any transport failure, and the stuck worker thread
# finishes on its own. A permanently stuck resolver retains a slot across breaker windows.
_DS_DEADLINE_S = 6.0
# ponytail: fixed per-process bounds, research 8 / funded 4; tune only after measuring healthy
# overlapping demand. Batches fetch chunks serially; research concurrency never consumes the funded
# slots. Research is wider because funded ENTRY mcap reads ride the research role
# (real_runner stats_fn -> dexscreener_stats) beside capture, tape and paper reads.
_ds_http_slots = {False: threading.BoundedSemaphore(8), True: threading.BoundedSemaphore(4)}


class DexscreenerCapacityError(TimeoutError):
    """Every worker slot of this role is busy: THIS call is refused locally. It says nothing about
    the provider, so it never trips the role's 60 s cooldown (Alpha, 2026-09-26): a healthy burst
    must not blank entry mcap for a minute. Stuck workers still trip it through the deadline."""
    failure_type = "capacity"


def _ds_default_http(method, url, headers=None, body=None, *, funded=False, transport=None):
    """`transport` (default urllib `_http`) sends the request inside this deadline and slot bound;
    the fast lanes pass their kept-alive client (memecoin_rh_fast_lane.WarmDexHttp)."""
    box = {}
    send = transport or _http
    slots = _ds_http_slots[bool(funded)]

    def run():
        held = False
        try:
            held = slots.acquire(blocking=False)
            if not held:
                raise DexscreenerCapacityError("dexscreener worker capacity exhausted")
            box["result"] = send(method, url, headers, body, timeout=_DS_TIMEOUT_S)
        except BaseException as exc:          # handed to the caller below, never swallowed
            box["error"] = exc
        finally:
            if held:
                slots.release()              # caller timeout cannot release an active request

    worker = threading.Thread(target=run, name="dexscreener-http", daemon=True)
    worker.start()
    worker.join(_DS_DEADLINE_S)
    if worker.is_alive():
        raise TimeoutError("dexscreener request exceeded %.1fs (DNS or connect stall)"
                           % _DS_DEADLINE_S)
    if "error" in box:
        raise box["error"]
    return box["result"]


def norm_mint(a):
    """Normalize EVM addresses only. Solana base58 remains case-sensitive."""
    return a.lower() if isinstance(a, str) and a[:2].lower() == "0x" else a


def _expected_chain(mint, network_id):
    if network_id:
        return FOMO_TO_DEX.get(str(network_id))
    return None if str(mint).lower().startswith("0x") else "solana"


def _positive_finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 and math.isfinite(value) else None


def _finite(value):
    """SIGNED finite number, or None if unknown/unparseable.

    Distinct from _positive_finite: a signal feature can legitimately be negative (priceChange on a
    dumping coin) or zero (no txns in the window), and both are INFORMATION. None means 'we do not
    know', 0.0 means 'we know it is zero' — the policy engine must be able to tell them apart.
    Collapsing unknown into a number is the exact bug class that made the mcap gate drop sub-$1M
    winners on feed gaps."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _nonnegative_finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if value >= 0 and math.isfinite(value) else 0.0


def _nested(pair, *path):
    cur = pair
    for key in path:
        cur = cur.get(key) if isinstance(cur, dict) else None
    return cur


def _quote_side_usd(pair):
    """USD depth on the QUOTE side of the pool — the part a pool creator cannot fake.

    DexScreener's liquidity.usd counts the base-token reserve valued at the pool's own
    price. A pool seeded with worthless tokens at a wash ratio therefore prints millions
    of fake depth with no real capital (PURR copycat, position 40687, 2026-08-19:
    $92.8M claimed, quote side near zero, 4,560x wrong unit on every mid lane for 25
    minutes). usd - base*priceUsd isolates the quote side, which requires a real
    deposit of quote tokens. When the base reserve is not in the response, fall back
    to liquidity.usd unchanged."""
    usd = _nonnegative_finite(_nested(pair, "liquidity", "usd"))
    base = _nested(pair, "liquidity", "base")
    price = _positive_finite(pair.get("priceUsd"))
    if base is None or price is None:
        return usd
    return max(0.0, usd - _nonnegative_finite(base) * price)


PAIR_SELECTION_RULE = "activity_fallback_v1"


def _valid_dex_pairs(pairs, mint, network_id=None):
    """The listed pairs that prove chain + base-token identity and carry a usable price."""
    chain = _expected_chain(mint, network_id)
    if not chain:
        return []
    wanted = norm_mint(mint)
    valid = []
    for pair in pairs or []:
        if not isinstance(pair, dict) or pair.get("chainId") != chain:
            continue
        base = (pair.get("baseToken") or {}).get("address")
        if not base or norm_mint(base) != wanted:
            continue
        if not pair.get("pairAddress") or _positive_finite(pair.get("priceUsd")) is None:
            continue
        valid.append(pair)
    return valid


def _txns_h1(pair):
    return int(_nonnegative_finite(_nested(pair, "txns", "h1", "buys"))
               + _nonnegative_finite(_nested(pair, "txns", "h1", "sells")))


def _dex_pair_picks(valid):
    """(today's pick, the activity-fallback pick, triggered) for already-valid pairs.

    Today: the deepest by quote-side USD, then h24 volume, then pair address. The fallback applies
    only when that pair holds under MEMECOIN_DEX_PAIR_DUST_USD on the quote side or reports no
    liquidity: the most h1 transactions, then h1 volume. The sort is stable over today's ranking, so a
    remaining tie falls to quote-side liquidity (then h24 volume, then address). A pair with no h1
    transaction therefore never wins over a pair with one.
    """
    from sniper.settings import memecoin as cfg
    if not valid:
        return None, None, False
    ranked = sorted(
        valid,
        key=lambda p: (
            -_quote_side_usd(p),
            -_nonnegative_finite(_nested(p, "volume", "h24")),
            str(p.get("pairAddress")),
        ),
    )
    today = ranked[0]
    dust_usd = float(getattr(cfg, "MEMECOIN_DEX_PAIR_DUST_USD", 100.0))
    triggered = _nested(today, "liquidity", "usd") is None or _quote_side_usd(today) < dust_usd
    if not triggered:
        return today, today, False
    activity = sorted(ranked, key=lambda p: (
        -_txns_h1(p), -_nonnegative_finite(_nested(p, "volume", "h1"))))[0]
    return today, activity, True


def _activity_fallback_on():
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_DEX_PAIR_ACTIVITY_FALLBACK", False))


def select_dex_pair(pairs, mint, network_id=None):
    """Select the deepest-by-quote-side pair after proving chain + base-token identity.

    With MEMECOIN_DEX_PAIR_ACTIVITY_FALLBACK on, a dust top pair yields to the most active listed
    pair (`_dex_pair_picks`). Off, the result is today's pick, unchanged.
    """
    today, activity, _triggered = _dex_pair_picks(_valid_dex_pairs(pairs, mint, network_id))
    return activity if _activity_fallback_on() else today


def _pair_brief(pair):
    return {
        "pair": pair.get("pairAddress"), "dex": pair.get("dexId"),
        "quote_side_usd": round(_quote_side_usd(pair), 2),
        "liquidity_usd": _finite(_nested(pair, "liquidity", "usd")),
        "txns_h1": _txns_h1(pair), "volume_h1": _finite(_nested(pair, "volume", "h1")),
        "price": _positive_finite(pair.get("priceUsd")), "mcap": _positive_finite(pair.get("marketCap")),
    }


def quote_key(network_id, mint):
    network = str(network_id or ("solana" if not str(mint).lower().startswith("0x") else ""))
    return network, norm_mint(mint)


def quote_for(price_map, mint, network_id=None):
    """Read a network-keyed quote, falling back only to the legacy mint key for old fixtures."""
    price_map = price_map or {}
    return price_map.get(quote_key(network_id, mint)) or price_map.get(norm_mint(mint))


def dexscreener_price(mint, http=None, network_id=None):
    st = dexscreener_stats(mint, http=http, network_id=network_id)
    return (st or {}).get("price")


def slippage_frac(notional, liquidity, k=None, cap=None):
    """Modeled price impact of a paper fill: fraction of mid the fill moves against
    us. Constant-product-ish FLOOR = k*notional/liquidity, capped. Unknown/zero
    liquidity -> cap (fail conservative). Real impact is usually >= this."""
    from sniper.settings.memecoin import MEMECOIN_SLIPPAGE_K, MEMECOIN_SLIPPAGE_CAP
    k = MEMECOIN_SLIPPAGE_K if k is None else k
    cap = MEMECOIN_SLIPPAGE_CAP if cap is None else cap
    if not liquidity or liquidity <= 0:
        return cap
    return min(cap, k * float(notional) / float(liquidity))


def modeled_fill(mid, notional, liquidity, side):
    """Apply modeled slippage to a mid price. buy -> pay up; sell -> receive less."""
    slip = slippage_frac(notional, liquidity)
    return mid * (1.0 + slip) if side == "buy" else mid * (1.0 - slip)


def honest_fill(mint, usd_amount, side, fallback_price, http=None, stats_fn=None, network_id=None):
    """Honest paper fill (both sides): modeled DexScreener mid +/- slippage -> fomo price only
    if >0. buy pays up, sell receives less (see modeled_fill). Returns (price, source); price is
    None when nothing can price the fill, so the caller must skip rather than book a fabricated
    fill. (Jupiter real-impact leg removed 2026-07-08 — see module docstring.)"""
    if stats_fn is None:
        st = dexscreener_stats(mint, http=http, network_id=network_id) or {}
    else:
        st = stats_fn(mint, http=http) or {}
    mid = st.get("price")
    if mid and mid > 0:
        return modeled_fill(mid, usd_amount, st.get("liquidity"), side), "dexscreener"
    if fallback_price and float(fallback_price) > 0:
        return float(fallback_price), "fomo"      # no-impact last resort
    return None, "none"


def _sum_reported(pools, *path, positive_only=False):
    """Sum a numeric field across pools that REPORT it. A pool with no such field is
    excluded from the sum, not counted as a 0 -- a missing field is not a known zero
    (see _finite). None if no pool in the set reports it at all, so the caller's None
    branch (unknown) stays distinct from a real 0.

    positive_only mirrors the single-pool convention already used for volume fields,
    where _positive_finite treats 0/negative as "unknown" -- keeping that rule means a
    coin whose every pool shows literal 0 volume still comes back None here, matching
    what a single-pool read already returned before this function existed."""
    parser = _positive_finite if positive_only else _finite
    reported = [v for v in (parser(_nested(p, *path)) for p in pools) if v is not None]
    return sum(reported) if reported else None


def _measured_h1_volume(pools, created_ms, *, now_ms=None):
    """Token-level h1 volume. A reported literal 0 is a MEASURED zero on a pair old enough
    to own an hour of history (RAYGUN 64145, 2026-09-23: a 42-day-old pool listed with
    volume.h1 = 0 and txns.h1 = 0 read as "unknown", the volume gate failed OPEN, and $175
    went into a coin with 3 trades an hour). Zero stays unknown when the pair is younger than
    the window (a fresh pool the provider has not indexed yet) or when the provider counts
    h1 trades it did not price (a partial read, not a zero)."""
    positive = _sum_reported(pools, "volume", "h1", positive_only=True)
    if positive is not None:
        return positive
    reported = [v for v in (_finite(_nested(p, "volume", "h1")) for p in pools) if v is not None]
    if not reported or any(v != 0 for v in reported):
        return None
    txns = [v for v in (_txns_h1(p) for p in pools) if v]
    if txns:
        return None
    if not created_ms:
        return None
    age_s = ((now_ms if now_ms is not None else time.time() * 1000.0) - float(created_ms)) / 1000.0
    return 0.0 if age_s >= 3600.0 else None


def _stats_from_pairs(pairs, mint, network_id):
    from sniper.settings import memecoin as cfg
    valid = _valid_dex_pairs(pairs, mint, network_id)
    today, activity, triggered = _dex_pair_picks(valid)
    fallback_on = _activity_fallback_on()
    pair = activity if fallback_on else today
    if pair is None:
        return None
    chain = _expected_chain(mint, network_id)
    wanted = norm_mint(mint)
    same_identity = [
        p for p in pairs or []
        if isinstance(p, dict)
        and p.get("chainId") == chain
        and norm_mint((p.get("baseToken") or {}).get("address") or "") == wanted
    ]
    created = [
        float(p["pairCreatedAt"]) for p in same_identity
        if _positive_finite(p.get("pairCreatedAt")) is not None
    ]
    return {
        "price": _positive_finite(pair.get("priceUsd")),
        "market_cap": _positive_finite(pair.get("marketCap")),
        "fdv": _positive_finite(pair.get("fdv")),
        "liquidity": _positive_finite(_nested(pair, "liquidity", "usd")),
        # TOKEN-LEVEL VOLUME (2026-09-12). A memecoin routinely lists on several pools per
        # chain (bonding-curve migration, a copy pool, a second DEX), and select_dex_pair
        # picks exactly one -- the deepest by quote-side USD -- for price and routing.
        # Reading volume/txns from that one pool undercounts a coin whose trading is split
        # across pools: a live sample of 85 decision coins found 9/43 on 4663 and 5/38 on
        # Solana with over 20% of h1 volume outside the chosen pool, worst case m5 1,431 vs
        # 49,887 summed across 30 pools. The turnover gates divide this by TOKEN-level
        # market_cap, so a one-pool numerator was comparing a slice of the coin's trading to
        # the whole coin. Sum across same_identity instead; keep the old single-pool number
        # as *_top for audit. price/market_cap/fdv/liquidity/pair_address/dex_id stay on the
        # chosen pair -- routing and impact read the pool that actually fills.
        "volume_24h": _sum_reported(same_identity, "volume", "h24", positive_only=True),
        "volume_m5": _sum_reported(same_identity, "volume", "m5", positive_only=True),
        "volume_m5_top": _positive_finite(_nested(pair, "volume", "m5")),
        "volume_h1": _measured_h1_volume(same_identity, min(created) if created else None),
        "volume_h1_top": _positive_finite(_nested(pair, "volume", "h1")),
        "txns_m5_buys": _sum_reported(same_identity, "txns", "m5", "buys"),
        "txns_m5_sells": _sum_reported(same_identity, "txns", "m5", "sells"),
        "txns_m5_top": {
            "buys": _finite(_nested(pair, "txns", "m5", "buys")),
            "sells": _finite(_nested(pair, "txns", "m5", "sells")),
        },
        # --- signal-time features for the adaptive policy engine (2026-07-15). Same HTTP response
        # the fill already fetches, so these are FREE: volatility, short-horizon volume and buy/sell
        # pressure at the moment of the signal. Previously parsed and discarded.
        # priceChange is SIGNED (a dumping coin is the point) -> _finite, NOT _positive_finite.
        "price_change_m5": _finite(_nested(pair, "priceChange", "m5")),
        "price_change_h1": _finite(_nested(pair, "priceChange", "h1")),
        "price_change_h6": _finite(_nested(pair, "priceChange", "h6")),
        "price_change_h24": _finite(_nested(pair, "priceChange", "h24")),
        "symbol": _nested(pair, "baseToken", "symbol") or None,
        "base_address": (_nested(pair, "baseToken", "address") or None),
        "quote_address": (_nested(pair, "quoteToken", "address") or None),
        "chain": pair.get("chainId") or None,
        "pair_address": pair.get("pairAddress") or None,
        "dex_id": pair.get("dexId") or None,
        "pair_created_at": min(created) if created else None,
        "n_pairs": len(same_identity),
        # BOTH PICKS FROM THIS RESPONSE (2026-09-17, operator order B). Observation for the operator's
        # ON decision: 0 extra requests, and it gates nothing while the switch is off.
        "pair_selection": {
            "rule": PAIR_SELECTION_RULE, "fallback_on": fallback_on,
            "dust_usd": float(getattr(cfg, "MEMECOIN_DEX_PAIR_DUST_USD", 100.0)),
            "listed_pair_count": len(valid), "response_pair_count": len(pairs or []),
            "triggered": triggered, "differs": today.get("pairAddress") != activity.get("pairAddress"),
            "liquidity_pick": _pair_brief(today), "activity_pick": _pair_brief(activity),
        },
    }


def dexscreener_stats(mint, http=None, network_id=None, *, funded=False) -> Optional[dict]:
    """Single-mint stats. Shares the batch's breaker: this is the mark-lane and runner path, and
    without it a dead provider costs the full `_http` timeout on every single call too."""
    if http is None:
        http = partial(_ds_default_http, funded=funded)
    if dexscreener_cooldown_active(funded=funded):
        return None
    try:
        _s, j, _d = http("GET", ds_tokens_url(mint))
        pairs = j.get("pairs") or []
        if not pairs:
            return None
        return _stats_from_pairs(pairs, mint, network_id)
    except DexscreenerCapacityError:
        _note_capacity_refusal(funded)
        return None
    except Exception as exc:
        _ds_start_cooldown(funded=funded, reason=type(exc).__name__)
        return None


def _resolvable_evm_networks():
    """Chains `resolve_evm_chain` may GUESS for a buy that arrived with no networkId.

    Deliberately NOT `FOMO_TO_DEX`. Adding a chain to that map restores its market data, which
    is a SAFETY change: the gates that were admitting unconditionally start refusing. Adding a
    chain HERE changes which chain a buy ROUTES to, which is a money-flow change in the opposite
    risk direction, and the two must not ride on one approval.

    4663 is gated behind MEMECOIN_RESOLVE_UNKNOWN_EVM_4663 because enabling it does TWO things,
    not one:
      1. a robinhood-only token that previously produced no candidate and resolved to None (so
         the buy was dropped) becomes a funded entry on a funded chain; and
      2. a token listed on BOTH base and robinhood gains a second candidate, so the
         liquidity tie-break below can rank 4663 first, or TIE and return None where it
         previously resolved. That is a REROUTE of existing resolution, not only new flow.

    Inert as of 2026-08-30: the feed has carried a networkId on every EVM buy since 2026-07-15
    (330 `evm-unknown` rows exist, none newer than that date), so this path has had no traffic
    for six weeks. Default off costs nothing today and keeps the decision explicit.
    """
    from sniper.settings import memecoin as cfg
    gated = {"4663"} if not getattr(
        cfg, "MEMECOIN_RESOLVE_UNKNOWN_EVM_4663", False) else set()
    # Arc (5042, 2026-09-16 build): FOMO_TO_DEX carries it for market data ONLY. This function
    # derives its list FROM FOMO_TO_DEX, so that entry alone would also open Arc as a ROUTE
    # target for an unknown-chain buy -- a separate, still-pending approval per the docstring
    # above. Held out unconditionally until that approval is explicit.
    gated |= {"5042"}
    return [n for n in FOMO_TO_DEX if n != "solana" and n not in gated]


def resolve_evm_chain(mint, http=None):
    """fomo numeric chain id for an 0x mint via ONE DexScreener lookup ('base' -> '8453'), or
    None when unlisted/unmapped. Used when the feed delivered an EVM buy WITHOUT a networkId
    (~27% of curated EVM buys measured 2026-07-10) — without this, every one of those was
    dropped even when the token actually lives on an allowlisted chain.

    E9 (2026-09-29, DexScreener out): a dead path -- 0 of 79,250 EVM events since 09-01 lacked a
    network, the last `evm-unknown` was 07-15. With E9 off this asks nobody and returns None, which
    every caller already treats as today's miss (drop / unresolved / refused / unpriceable). E9 has
    no chain dimension: the chain is exactly what is unknown, so only a bare `E9` matches."""
    from sniper.settings import memecoin as _cfg
    if _cfg.dexscreener_off("E9"):
        _count_consumer(_CONSUMER_SUPPRESSED, "E9")
        return None
    http = http or _http
    try:
        status, payload, _date = http("GET", ds_tokens_url(mint))
        if status != 200 or not isinstance(payload, dict):
            return None
        candidates = []
        for network in _resolvable_evm_networks():
            stats = _stats_from_pairs(payload.get("pairs") or [], mint, network)
            if stats:
                candidates.append((float(stats.get("liquidity") or 0.0), network))
        if len(candidates) == 1:
            return candidates[0][1]
        ranked = sorted(candidates, reverse=True)
        if not ranked or ranked[0][0] <= 0 or ranked[0][0] == ranked[1][0]:
            return None
        return ranked[0][1]
    except Exception:
        return None


# DexScreener returns at most this many PAIRS per /latest/dex/tokens response, even for one token
# (WETH on Base and wSOL returned exactly 30 each, 2026-09-17). A chunk of 30 MINTS can therefore
# push listed mints out of the answer with no error: capture-v2 sweep 699a2d4c read 26 Solana
# mints as missing_pair, and 12 of 12 re-asked alone priced.
DS_PAIR_CAP = 30


# CONSUMER TAGS (2026-09-28, DexScreener out phase A). Every batch call counts under the id of the
# consumer that made it ("untagged" when it names none), and a consumer switched off in
# MEMECOIN_DEXSCREENER_OFF gets {} with no request and counts as suppressed. A call site the removal
# missed therefore shows up in the heartbeat as a count, never as a silent read. Lock-guarded: the
# fast lane, the sweeps and the entry threads all call this.
_CONSUMER_LOCK = threading.Lock()
_CONSUMER_CALLS = {}
_CONSUMER_SUPPRESSED = {}


def _count_consumer(table, consumer):
    with _CONSUMER_LOCK:
        table[consumer] = table.get(consumer, 0) + 1


def dexscreener_consumer_counts():
    """{"calls": {consumer: n}, "suppressed": {"consumer:chain": keys}} since the process started.

    Covers `dexscreener_stats_batch_keyed` only: the exit sweep (X1), the fast lane (X2) and any other
    batch caller ("untagged"). The entry's single-mint GET (`dexscreener_stats`), the web, the paper
    lab and the capture reads go through other functions and are NOT counted here."""
    with _CONSUMER_LOCK:
        return {"calls": dict(_CONSUMER_CALLS), "suppressed": dict(_CONSUMER_SUPPRESSED)}


def dexscreener_stats_batch_keyed(keys, http=None, *, funded=False, consumer=None):
    """Batch prices by `(network_id, mint)`, preserving cross-chain address collisions.

    `funded=True` from the money path (real_exit_sweep, fastlane): it reads the funded breaker cell
    and a failure there trips BOTH cells. Research callers read and trip only the shared cell, so a
    discovery throttle can never blind the funded lane -- the 2026-08-14 GeckoTerminal bug.
    Returns {} while the breaker is open, which is the same value a dead provider already produced.
    """
    if consumer is not None:
        # Per chain: the switched-off chains' keys are never asked for, the rest go out as today.
        from sniper.settings.memecoin import dexscreener_off
        kept = []
        for network, mint in keys:
            chain = str(network or "solana")
            if dexscreener_off(consumer, chain):
                _count_consumer(_CONSUMER_SUPPRESSED, "%s:%s" % (consumer, chain))
            else:
                kept.append((network, mint))
        keys = kept
        if not keys:
            return {}
    _count_consumer(_CONSUMER_CALLS, str(consumer) if consumer else "untagged")
    # Bind the short timeout and the whole-request deadline only to the DEFAULT seam: an injected
    # `http` keeps the exact signature it had before this change.
    if http is None:
        http = partial(_ds_default_http, funded=funded)
    if dexscreener_cooldown_active(funded=funded):
        return {}
    normalized = list(dict.fromkeys((str(network or ""), norm_mint(mint)) for network, mint in keys))
    by_mint = {}
    for network, mint in normalized:
        by_mint.setdefault(mint, []).append(network)
    out = {}

    def collect(pairs, asked):
        """Fill `out` from one answer. Return the asked mints that got no stats on some chain."""
        missing = []
        for mint in asked:
            for network in by_mint[mint]:
                stats = _stats_from_pairs(pairs, mint, network or None)
                if stats:
                    out[(network or "solana", mint)] = stats
                elif mint not in missing:
                    missing.append(mint)
        return missing

    mints = list(by_mint)
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        try:
            status, payload, _date = http("GET", ds_tokens_url(",".join(chunk)))
            if status != 200 or not isinstance(payload, dict):
                # A non-200 is the provider refusing us, not a missing pair. Trip and stop the
                # remaining chunks: they would each re-pay the timeout against the same refusal.
                _ds_start_cooldown(funded=funded, reason="status %s" % status)
                break
            pairs = payload.get("pairs") or []
            missing = collect(pairs, chunk)
            if len(pairs) >= DS_PAIR_CAP and missing:
                # A CAPPED answer: its missing mints are unknown, not unlisted. Re-ask only those,
                # once. The heavy mints that filled the cap are not in the re-ask. A mint still
                # missing after it stays unknown -- never a loop. A refusal here trips the breaker
                # exactly as on the first read, and the stats already collected are kept.
                status, payload, _date = http("GET", ds_tokens_url(",".join(missing)))
                if status != 200 or not isinstance(payload, dict):
                    _ds_start_cooldown(funded=funded, reason="status %s" % status)
                    break
                collect(payload.get("pairs") or [], missing)
        except DexscreenerCapacityError:
            _note_capacity_refusal(funded)       # local, not the provider: no cooldown
            break
        except Exception as exc:
            # Timeout / transport failure. Trip the breaker and abandon the remaining chunks --
            # the old `continue` re-paid the full timeout on each one (ceil(N/30) x timeout).
            _ds_start_cooldown(funded=funded, reason=type(exc).__name__)
            break
    return out


DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q="


def dexscreener_search(query, http=None, limit=25):
    """Free-text coin lookup via DexScreener search — the in-app replacement for the Fomo coin lookup.
    Returns normalized universe rows [{network_id, mint, ticker, price, market_cap, liquidity,
    volume_24h, source:'live'}], deepest pair per (chain, token), for chains we map (DEX_CHAIN_TO_FOMO).
    4663/Robinhood is DexScreener-blind and simply won't appear here (local search still covers it).
    Best-effort; returns [] on any failure."""
    http = http or _http
    q = (query or "").strip()
    if not q:
        return []
    try:
        import urllib.parse
        status, payload, _date = http("GET", DEXSCREENER_SEARCH + urllib.parse.quote(q))
        if status != 200 or not isinstance(payload, dict):
            return []
    except Exception:
        return []
    best = {}                                  # (network_id, mint) -> (liq, row)
    for pair in (payload.get("pairs") or []):
        if not isinstance(pair, dict):
            continue
        network = DEX_CHAIN_TO_FOMO.get(pair.get("chainId"))
        base = pair.get("baseToken") or {}
        mint = base.get("address")
        price = _positive_finite(pair.get("priceUsd"))
        if not network or not mint or price is None:
            continue
        liq = _nonnegative_finite(_nested(pair, "liquidity", "usd"))
        key = (network, norm_mint(mint))
        if key in best and best[key][0] >= liq:
            continue
        best[key] = (liq, {
            # Emit the NORMALIZED mint (EVM lowercased) — DexScreener returns checksummed addresses, and
            # a mixed-case mint here silently missed every downstream keyed lookup (dedup vs the lowercase
            # feed, quote cache, socials, triage, charting). Lowercase EVM addresses are on-chain-valid.
            "network_id": network, "mint": norm_mint(mint),
            "ticker": base.get("symbol") or base.get("name"),
            "venue": "EVM" if str(mint).startswith("0x") else "SOL", "chain": network,
            "price": price, "market_cap": pair.get("marketCap") or pair.get("fdv"),
            "liquidity": liq or None, "volume_24h": _nested(pair, "volume", "h24"),
            "buyers": 0, "armed_buyers": 0, "source": "live",
        })
    rows = [r for _liq, r in sorted(best.values(), key=lambda x: -x[0])]
    return rows[:int(max(1, limit))]


def dexscreener_socials_batch(keys, http=None):
    """`(network_id, mint)` -> {twitter, telegram, website, boosts} from DexScreener token pages — 'is
    anyone talking about this?' as data. Same endpoint as the stats batch. Solana/Base/mapped chains
    only (4663 is DexScreener-blind). Best-effort; coins with no socials/boosts are omitted."""
    http = http or _http
    normalized = list(dict.fromkeys((str(net or ""), norm_mint(mint)) for net, mint in keys))
    by_mint = {}
    for net, mint in normalized:
        by_mint.setdefault(mint, []).append(net)
    out = {}

    def collect(pairs, asked):
        """Fill `out` from one answer. Return the asked mints with no pair on some chain."""
        missing = []
        for mint in asked:
            for net in by_mint[mint]:
                pair = select_dex_pair(pairs, mint, net or None)
                if not pair:
                    if mint not in missing:
                        missing.append(mint)
                    continue
                info = pair.get("info") or {}
                socials = info.get("socials") or []
                tw = next((s.get("url") for s in socials if s.get("type") == "twitter"), None)
                tg = next((s.get("url") for s in socials if s.get("type") == "telegram"), None)
                web = next((w.get("url") for w in (info.get("websites") or []) if w.get("url")), None)
                boosts = int((pair.get("boosts") or {}).get("active") or 0)
                if tw or tg or web or boosts:
                    out[(net or "solana", mint)] = {"twitter": tw, "telegram": tg,
                                                    "website": web, "boosts": boosts}
        return missing

    mints = list(by_mint)
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        try:
            status, payload, _date = http("GET", ds_tokens_url(",".join(chunk)))
            if status != 200 or not isinstance(payload, dict):
                continue
            pairs = payload.get("pairs") or []
            missing = collect(pairs, chunk)
            if len(pairs) >= DS_PAIR_CAP and missing:
                # Same rule as the stats batch: one re-ask of a capped chunk's missing mints.
                status, payload, _date = http("GET", ds_tokens_url(",".join(missing)))
                if status == 200 and isinstance(payload, dict):
                    collect(payload.get("pairs") or [], missing)
        except Exception:
            continue
    return out


def dexscreener_stats_batch(mints, http=None):
    """Legacy mint-keyed wrapper. It is safe for Solana only; EVM callers must use the keyed API."""
    keys = [("solana", mint) for mint in dict.fromkeys(mints)
            if not str(mint).lower().startswith("0x")]
    keyed = dexscreener_stats_batch_keyed(keys, http=http)
    return {mint: stats for (_network, mint), stats in keyed.items()}
