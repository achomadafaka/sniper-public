"""GeckoTerminal price client for DexScreener-blind chains (Robinhood 4663).

GT's public API indexes Robinhood chain as network slug ``robinhood``, free and keyless. This client
is the sole live price source for blind chains. It returns the exact stats shape
``sniper.services.memecoin_evm_price.evm_price_batch`` emits — ``{"price", "liquidity", "mcap",
"source"}`` keyed by ``(network_id, mint)`` — so it drops into every existing seam unchanged.

Guards (all protect a free public endpoint and the money-adjacent mark tape):
  * batched ≤30 addresses per HTTP call (GT's documented multi-token cap);
  * a shared TTL cache (default ``MEMECOIN_GT_TTL_S``≈15s) so the blind worker's batch warms the
    per-mint exit-sweep lookups;
  * a min-interval rate guard so the per-mint ``gt_price_for`` path cannot burst GT HTTP;
  * one nonblocking process-wide request gate; HTTP 429 starts a shared bounded cooldown and stops
    later chunks, so independent capture/mark threads cannot amplify a provider refusal;
  * NEVER a 0.0 tick — an unpriceable / non-finite / absent mint is OMITTED (a 0 reads as a -100%
    tick and fires every stop); same contract as ``memecoin_evm_price``;
  * every HTTP failure → a missing key, never an exception to the caller;
  * ONLY the live-verified slug is mapped. Other chains are skipped, never guessed (RUSSELL lesson:
    a secondary price source must never touch a chain DexScreener already covers).

Liquidity is not derivable from this endpoint → ``None`` always (the slippage model treats unknown
liquidity as the conservative cap; never fabricate a number). Market cap comes back as ``"0.0"`` /
``null`` for young coins → ``None``; the RPC ``totalSupply`` path stays the real mcap source.

Live-verified 2026-07-16:
  GET https://api.geckoterminal.com/api/v2/simple/networks/robinhood/token_price/<a1,a2,...>
      ?include_market_cap=true&include_24hr_vol=true
  → {"data":{"attributes":{"token_prices":{"0x39db…":"0.008658…"},
                           "market_cap_usd":{"0x39db…":"0.0"},
                           "h24_volume_usd":{"0x39db…":"17644511.59…"}}}}
"""
from __future__ import annotations

import json as _json
import logging
import math
import threading
import urllib.error
import urllib.request

from sniper.execution.solana_quote import norm_mint

from sniper.util.log_sample import log_sampled

logger = logging.getLogger(__name__)


class GtRateLimited(Exception):
    """HTTP 429 from GeckoTerminal. Raised, never swallowed: the caller must STOP its cycle.

    GT issues no API key and limits by IP, and the funded mark lane shares that IP. Retrying into a
    limit is how a research process spends a funded quota — the 2026-08-21 Helius incident burned
    ~7,000 requests in four hours on a 2s retry loop against a usage cap.
    """

# ONLY live-verified slugs. Extending this map is a one-line change once a chain is verified live —
# a guessed slug would price a coin against the wrong chain and fabricate a mark.
GT_NETWORK = {"4663": "robinhood"}

# ONE MAP PER PURPOSE. `GT_NETWORK` governs which chains GT may PRICE, and solana is deliberately
# absent there because DexScreener is solana's price source — widening it for the chart-truth job
# silently routed solana price lookups to GeckoTerminal too, which an existing test caught
# (2026-08-28). Candle history is a different question from live pricing and gets its own allowlist.
# Both stay live-verified: the solana entry below rests on a 2026-08-27 probe of
# `networks/solana/pools/{pool}/ohlcv/minute` returning HTTP 200 in 0.43 s with 1,000 candles over
# 17.3 hours, re-confirmed against four live pools during the build.
# 8453 added 2026-09-06 after a live probe of the `base` slug: token_price returned WETH 2502.04
# and USDC 1.005, and `networks/base/pools/{pool}/ohlcv/minute` returned HTTP 200 with 100 candles.
# It is in the CANDLE map only. `GT_NETWORK` above stays without Base on purpose -- DexScreener
# indexes Base, and pricing a DexScreener-covered chain from a secondary source is the RUSSELL
# lesson (`memecoin_evm_price.price_for`, which refuses it anyway behind the blind-chain gate).
# Ethereum (1) is in NEITHER map (2026-09-30): DexScreener indexes it, so GT must not price it,
# and its candle endpoint was not probed clean (one 429 on the shared IP; the `eth` slug itself
# answered token_price WETH 2693.96, USDC 1.0006).
GT_OHLCV_NETWORK = {"4663": "robinhood", "solana": "solana", "8453": "base"}

GT_BASE = "https://api.geckoterminal.com/api/v2/simple/networks"
MAX_ADDRS = 30       # GT multi-token cap

# GT 403s the default Python user-agent (same as DexScreener / Jupiter). Send a browser UA.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

# Shared module state: TTL price cache + a single last-HTTP stamp for the rate guard. Keyed by
# (network_id, normalized mint). The blind worker's batch warms it; the per-mint exit lookups read it.
_cache: dict = {}          # (network, mint) -> (stats_dict, ts_monotonic)
_last_http = [0.0]         # monotonic ts of the most recent GT HTTP call (list = mutable cell)
_cooldown_until = [0.0]    # provider 429 cooldown, shared by batch + single-key callers
# Funded callers get their OWN cooldown cell. The blind-capture batch prices dozens of research
# mints per minute and trips 429s constantly; because the cooldown was a single shared cell, every
# one of those research refusals also gagged the money path for up to 300s. On 2026-08-14 that
# refused live entries outright (`provider_rate_limit: v4 could not read: rate_limit`) while the
# funded lane itself had made no failing request. Research 429s now suppress research only; a
# funded 429 still sets BOTH, because that one is the provider actually refusing us.
_funded_cooldown_until = [0.0]
_http_gate = threading.Lock()
# WHAT THIS THREAD'S LAST gt_get_json DID (2026-10-17, Gamma MED-2): {"outcome": ok | gate_busy |
# cooldown | 429 | error | disabled, "gate_wait_ms": float}. Read with `last_get()`, never shared.
_LAST_GET = threading.local()
_RATE_LIMITED = object()
_BUSY = object()

# POOL stats (liquidity / volume / price-change) — the simple price endpoint can't give liquidity, so
# this is a SEPARATE, slow, rate-guarded path. Pool address is immutable per token -> cached forever.
_pool_addr: dict = {}      # (slug, mint) -> pool address string
_pool_stats: dict = {}     # (slug, mint) -> (stats_dict, ts_monotonic)
_pool_last_http = [0.0]    # own min-interval stamp: keep pool calls off the funded pricing cadence
_GT_V2 = "https://api.geckoterminal.com/api/v2"


def reset_gt_state():
    """Test seam — cache, rate stamp, and cooldown are process-lifetime by design."""
    _cache.clear()
    _pool_addr.clear()
    _pool_stats.clear()
    _last_http[0] = 0.0
    _pool_last_http[0] = 0.0
    _cooldown_until[0] = 0.0
    _funded_cooldown_until[0] = 0.0


def _signed(value):
    """Finite float allowing negatives (price-change %), or None. (`_num` rejects <=0.)"""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _http(method, url, headers=None, body=None, timeout=10):
    h = {"user-agent": _UA, "accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return r.status, (_json.loads(raw) if raw else {}), None


def _cfg_ttl(ttl_s):
    if ttl_s is not None:
        return float(ttl_s)
    from sniper.settings import memecoin as cfg
    return float(cfg.MEMECOIN_GT_TTL_S)


def _cfg_min_interval(min_interval_s):
    if min_interval_s is not None:
        return float(min_interval_s)
    from sniper.settings import memecoin as cfg
    return float(cfg.MEMECOIN_GT_MIN_INTERVAL_S)


def _cfg_cooldown(cooldown_s):
    if cooldown_s is not None:
        return float(cooldown_s)
    from sniper.settings import memecoin as cfg
    return float(cfg.MEMECOIN_GT_429_COOLDOWN_S)


def _num(value):
    """Positive finite float, or None. NEVER returns 0.0 — an omitted mint beats a phantom -100% tick."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 and math.isfinite(value) else None


def _stats_from_attributes(attrs, mint):
    """Build the evm_price_batch-shaped stats for one mint from a GT token_price payload, or None.

    GT lowercases addresses in the response; `norm_mint` lowercases EVM addresses too, so the keys
    line up directly. Liquidity is never available here → None. mcap <=0/null → None."""
    prices = (attrs or {}).get("token_prices") or {}
    price = _num(prices.get(mint))
    if price is None:
        return None                        # unpriced / 0 / non-finite -> OMIT, never a 0.0 tick
    mcaps = (attrs or {}).get("market_cap_usd") or {}
    # 24h volume ships in the SAME response — the fetch already sets include_24hr_vol=true — but was
    # discarded, so Robinhood cards showed no volume. Surface it (zero GT cost). Liquidity is still not
    # in this endpoint (needs the pool endpoint) -> None. (2026-07-18)
    vols = (attrs or {}).get("h24_volume_usd") or {}
    return {"price": price, "liquidity": None,
            "mcap": _num(mcaps.get(mint)), "volume_24h": _num(vols.get(mint)),
            "source": "geckoterminal"}


def _retry_after_seconds(headers, default_s):
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(default_s)
    return min(300.0, max(3.0, value))


def _start_cooldown(now, headers, default_s, *, funded=False):
    delay = _retry_after_seconds(headers, default_s)
    _cooldown_until[0] = max(_cooldown_until[0], now + delay)
    if funded:
        _funded_cooldown_until[0] = max(_funded_cooldown_until[0], now + delay)
    log_sampled(logger.warning, ("gt_429", bool(funded)),
                "geckoterminal rate limited; cooling down for %.0fs (funded=%s)", delay, funded)


def cooldown_active(clock=None, *, funded=False):
    """True while a 429 cooldown suppresses GT reads.

    In this state `gt_get_json` returns None WITHOUT an HTTP call, so a caller that watches only
    HTTP sees a clean empty result and cannot tell a provider rate limit from "this token has no
    pool". Ask this instead before you record a discovery miss as a token fact.

    Pass `funded=True` from the money path. `gt_get_json` gates on the funded cell, so asking the
    shared cell there reports blindness the funded call never actually suffered — which attributed
    a research throttle to the coin and refused live entries as `rate_limit` (2026-08-14)."""
    if clock is None:
        import time
        clock = time.monotonic
    cell = _funded_cooldown_until if funded else _cooldown_until
    return clock() < cell[0]


def _fetch(slug, mints, http, now, cooldown_s):
    """One GT multi-token HTTP call for up to MAX_ADDRS mints on `slug`. Returns the attributes dict
    or a private guard sentinel on rate-limit/busy (fail-closed)."""
    if now < _cooldown_until[0]:
        return _RATE_LIMITED
    if not _http_gate.acquire(blocking=False):
        return _BUSY
    url = (f"{GT_BASE}/{slug}/token_price/{','.join(mints)}"
           "?include_market_cap=true&include_24hr_vol=true")
    try:
        if now < _cooldown_until[0]:
            return _RATE_LIMITED
        _last_http[0] = now
        try:
            status, payload, detail = http("GET", url)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                _start_cooldown(now, exc.headers, cooldown_s)
                return _RATE_LIMITED
            logger.exception("geckoterminal fetch failed slug=%s n=%d", slug, len(mints))
            return None
        except Exception:
            logger.exception("geckoterminal fetch failed slug=%s n=%d", slug, len(mints))
            return None
        if status == 429:
            _start_cooldown(now, detail, cooldown_s)
            return _RATE_LIMITED
        if status != 200 or not isinstance(payload, dict):
            return None
        return ((payload.get("data") or {}).get("attributes")) or {}
    finally:
        _http_gate.release()


def _gt_disabled():
    """True when GeckoTerminal is switched off for the whole process.

    ENFORCED AT THE CLIENT, NOT AT EACH CALLER (2026-09-04, operator). `MEMECOIN_GT_ENABLED` was
    honoured in exactly three places -- the price seam, the runner and capture v2 -- while V4 pool
    DISCOVERY called this module directly and ignored it. That is the path that cost a funded
    position: GeckoTerminal returned LARP's 81% and 80% fee pools, the sane 10% pool was not among
    them, and every exit quote failed for 60 sweeps. A switch that leaves the failing path live is
    not a switch, so the gate belongs here where every caller must pass through it.

    Read at CALL time, never captured at import, so flipping the env and restarting is enough and
    no caller can hold a stale answer.
    """
    from sniper.settings import memecoin as _cfg
    return not getattr(_cfg, "MEMECOIN_GT_ENABLED", True)


def gt_stats_batch_keyed(keys, *, http=None, clock=None, ttl_s=None, min_interval_s=None,
                         cooldown_s=None):
    """`(network_id, mint)` → GT stats, for chains in GT_NETWORK only. Cache-first; misses are
    fetched in ≤30-address chunks. The bulk warmer for the per-mint `gt_price_for` cache.

    Networks without a verified slug (Solana, Base, …) are skipped outright — never HTTP'd, never
    guessed. Absent / unpriceable mints are omitted from the result (never a 0.0 tick)."""
    if _gt_disabled():
        return {}
    http = http or _http
    if clock is None:
        import time
        clock = time.monotonic
    now = clock()
    ttl = _cfg_ttl(ttl_s)
    cooldown = _cfg_cooldown(cooldown_s)

    out = {}
    fetch_by_slug: dict = {}
    for key in keys or []:
        network_id, mint = key if isinstance(key, tuple) else (None, key)
        slug = GT_NETWORK.get(str(network_id or ""))
        if not slug:
            continue                       # DexScreener covers this chain -> never GT it
        k = (str(network_id), norm_mint(mint))
        cached = _cache.get(k)
        if cached is not None and (now - cached[1]) < ttl:
            out[k] = cached[0]             # fresh cache hit
            continue
        fetch_by_slug.setdefault(slug, {})[k[1]] = k

    for slug, addr_to_key in fetch_by_slug.items():
        addrs = list(addr_to_key)
        for i in range(0, len(addrs), MAX_ADDRS):
            chunk = addrs[i:i + MAX_ADDRS]
            attrs = _fetch(slug, chunk, http, now, cooldown)
            if attrs is _RATE_LIMITED or attrs is _BUSY:
                return out                   # retain earlier chunks; never amplify a refusal
            if not attrs:
                continue                   # whole chunk missing -> those keys stay absent (fail-closed)
            for addr in chunk:
                stats = _stats_from_attributes(attrs, addr)
                if stats is None:
                    continue               # unpriceable -> omitted
                k = addr_to_key[addr]
                _cache[k] = (stats, now)
                out[k] = stats
    return out


def last_get():
    """This thread's last gt_get_json outcome and gate wait, or {} (see _LAST_GET)."""
    return dict(getattr(_LAST_GET, "value", None) or {})


def _note_get(outcome, gate_wait_ms=None):
    _LAST_GET.value = {"outcome": outcome, "gate_wait_ms": gate_wait_ms}


def gt_get_json(url, *, http=None, clock=None, cooldown_s=None, gate_timeout_s=2.0, funded=True):
    """Shared-guarded GET for NON-price GT endpoints (e.g. V4 pool discovery). Honors the
    process-wide 429 cooldown, waits (bounded) on the single-flight gate, stamps the shared
    last-HTTP mark, and registers any 429 with the shared cooldown — so pool lookups and the
    price batch can never independently amplify a provider refusal (2026-07-17: raw-urllib V4
    pool lookups were starved by marks-path 429s AND invisible to the guard).

    Deliberately NOT min-interval-guarded: callers are low-volume and money-adjacent (entry
    quotes); refusing them behind the marks cadence would silently drop the V4 rung.
    Returns the parsed JSON dict, or None (fail-closed — callers must never cache a refusal).
    `funded=False` (background traffic, e.g. the multihop bridge-memo refresh): honors the SHARED
    cooldown and books its 429 there only, so it never closes the funded cell (Gamma MED-1)."""
    from sniper.execution.evm_rpc_pool import RequestNotSent
    if _gt_disabled():
        _note_get("disabled")
        return None
    http = http or _http
    if clock is None:
        import time
        clock = time.monotonic
    import time as _time
    now = clock()
    # The FUNDED cell, not the shared one: a research-batch 429 must not decide whether an entry
    # quote may be attempted. See `_funded_cooldown_until`. Background callers read the shared cell,
    # which every 429 (funded or not) sets, so they also stand down for a funded refusal.
    cell = _funded_cooldown_until if funded else _cooldown_until
    if now < cell[0]:
        _note_get("cooldown", 0.0)
        return None
    remaining = getattr(http, "request_remaining_s", None)
    if callable(remaining):
        gate_timeout_s = min(gate_timeout_s, remaining())
    waited = _time.monotonic()
    if not _http_gate.acquire(timeout=gate_timeout_s):
        _note_get("gate_busy", (_time.monotonic() - waited) * 1000.0)
        if callable(remaining) and remaining() <= 0:
            raise RequestNotSent("provider discovery deadline")
        return None
    gate_wait_ms = (_time.monotonic() - waited) * 1000.0
    try:
        if now < cell[0]:
            _note_get("cooldown", gate_wait_ms)
            return None
        _last_http[0] = now
        try:
            status, payload, detail = http("GET", url)
        except RequestNotSent:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                _start_cooldown(now, exc.headers, _cfg_cooldown(cooldown_s), funded=funded)
                _note_get("429", gate_wait_ms)
                return None
            logger.exception("geckoterminal GET failed url=%s", url)
            _note_get("error", gate_wait_ms)
            return None
        except Exception:
            logger.exception("geckoterminal GET failed url=%s", url)
            _note_get("error", gate_wait_ms)
            return None
        if status == 429:
            _start_cooldown(now, detail, _cfg_cooldown(cooldown_s), funded=funded)
            _note_get("429", gate_wait_ms)
            return None
        if status != 200 or not isinstance(payload, dict):
            _note_get("error", gate_wait_ms)
            return None
        _note_get("ok", gate_wait_ms)
        return payload
    finally:
        _http_gate.release()


def gt_price_for(network_id, mint, *, http=None, clock=None, ttl_s=None, min_interval_s=None,
                 cooldown_s=None):
    """Single-key price convenience over the shared cache — the `price_for(chain, mint)` seam.

    Cache-first (warmed by the batch). On a miss it honors the min-interval rate guard: if GT was
    called too recently it returns None (fail-closed) rather than bursting the endpoint. None means
    "no GT price this call"; the caller leaves the key unpriced."""
    if _gt_disabled():
        return None
    slug = GT_NETWORK.get(str(network_id or ""))
    if not slug:
        return None
    http = http or _http
    if clock is None:
        import time
        clock = time.monotonic
    now = clock()
    ttl = _cfg_ttl(ttl_s)
    k = (str(network_id), norm_mint(mint))
    cached = _cache.get(k)
    if cached is not None and (now - cached[1]) < ttl:
        return cached[0]["price"]
    if now < _cooldown_until[0]:
        return None
    if (now - _last_http[0]) < _cfg_min_interval(min_interval_s):
        return None                        # rate-guarded: don't burst GT from the per-mint path
    attrs = _fetch(slug, [k[1]], http, now, _cfg_cooldown(cooldown_s))
    if attrs is _RATE_LIMITED or attrs is _BUSY:
        return None
    stats = _stats_from_attributes(attrs, k[1]) if attrs else None
    if stats is None:
        return None
    _cache[k] = (stats, now)
    return stats["price"]


def gt_pool_stats_for(network_id, mint, *, http=None, clock=None, ttl_s=300.0, min_interval_s=15.0):
    """LIVE pool stats — liquidity (reserve_in_usd), 24h volume, mcap, 1h/24h price-change — from GT's
    ``/networks/{slug}/tokens/{mint}/pools`` endpoint. This is the ONLY liquidity source for
    DexScreener-blind chains (Robinhood 4663), and it is the SAME call the V4 pool discovery already
    makes for routing (evm_v4_quote._gt_pools_for_mint), whose response carries reserve_in_usd per pool
    — so it is a proven-reachable endpoint, and one call gets everything (no separate pool fetch).

    ONE GT call, cache-first (ttl_s, default 300s — liquidity moves slowly), behind the SHARED 429
    cooldown, with its own min-interval so it stays off the funded pricing cadence. Picks the DEEPEST
    (max-reserve) pool. On any miss/throttle returns the last cached stats (stale-OK) or None — never
    bursts GT. Returns {liquidity, volume_24h, mcap, change_h1, change_h24} or None."""
    if _gt_disabled():
        return None
    slug = GT_NETWORK.get(str(network_id or ""))
    if not slug:
        return None
    http = http or _http
    if clock is None:
        import time
        clock = time.monotonic
    now = clock()
    k = (slug, norm_mint(mint))
    cached = _pool_stats.get(k)
    if cached is not None and (now - cached[1]) < float(ttl_s):
        return cached[0]
    stale = cached[0] if cached else None
    if now < _cooldown_until[0]:
        return stale                              # provider cooling down -> don't burst
    if (now - _pool_last_http[0]) < float(min_interval_s):
        return stale                              # keep pool calls off the pricing cadence
    _pool_last_http[0] = now
    payload = gt_get_json(f"{_GT_V2}/networks/{slug}/tokens/{k[1]}/pools", http=http, clock=clock)
    pools = (payload or {}).get("data") or []
    best, best_liq = None, -1.0
    for p in pools:                               # the deepest pool = the tradeable liquidity
        a = p.get("attributes") or {}
        liq = _num(a.get("reserve_in_usd"))
        if liq is not None and liq > best_liq:
            best, best_liq = a, liq
    if best is None:
        return stale
    pc = best.get("price_change_percentage") or {}
    vol = best.get("volume_usd") or {}
    stats = {
        "liquidity": _num(best.get("reserve_in_usd")),
        "volume_24h": _num(vol.get("h24")),
        "mcap": _num(best.get("market_cap_usd")) or _num(best.get("fdv_usd")),
        "change_h1": _signed(pc.get("h1")),
        "change_h24": _signed(pc.get("h24")),
    }
    _pool_stats[k] = (stats, now)
    return stats


GT_OHLCV_LIMIT = 1000       # one page; 1,000 minutes is ~16.7 hours, NOT the full history


def gt_pool_ohlcv_minute(network_id, pool_address, http=None, *, limit=GT_OHLCV_LIMIT):
    """One page of minute candles for a pool, newest first, as {epoch_seconds: (o,h,l,c,v)}.

    Returns None on any failure — an unmapped chain, a transport error, a malformed body. The
    caller decides what a None means; this never guesses a slug, because a guessed slug prices a
    coin against the wrong chain and fabricates a mark (the reason `GT_NETWORK` is an allowlist).

    Raises `GtRateLimited` on HTTP 429 so a caller can stop its cycle rather than retry into a
    limit. GeckoTerminal has no key and limits by IP, and the funded mark lane shares that IP.
    """
    slug = GT_OHLCV_NETWORK.get(str(network_id))
    if not slug or not pool_address:
        return None
    url = f"{_GT_V2}/networks/{slug}/pools/{pool_address}/ohlcv/minute?limit={int(limit)}"
    if http is None:
        def http(method, u, headers=None, body=None):
            return _http(method, u, headers, body)
    try:
        status, body, _ = http("GET", url, {"Accept": "application/json", "User-Agent": _UA})
    except GtRateLimited:
        raise
    except urllib.error.HTTPError as exc:
        # A 429 ARRIVES AS AN EXCEPTION, NOT A STATUS. `_http` uses `urlopen`, which RAISES on any
        # 4xx/5xx, so a `status == 429` branch below would be unreachable dead code and the caller's
        # stop-on-first-429 rule would never fire — it would treat the limit as an ordinary provider
        # error and walk straight on to the next pool, retrying into the limit. That is the Jupiter
        # trap with extra steps. Found by running the real client against the live API, where a 404
        # surfaced the same way (2026-08-28).
        if exc.code == 429:
            raise GtRateLimited(url) from exc
        if exc.code != 404:
            logger.warning("geckoterminal ohlcv HTTP %s url=%s", exc.code, url)
        return None
    except Exception:
        logger.exception("geckoterminal ohlcv GET failed url=%s", url)
        return None
    if status == 429:                       # kept for an injected http() that RETURNS a status
        raise GtRateLimited(url)
    if status != 200 or not isinstance(body, dict):
        return None
    rows = (((body.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
    out = {}
    for row in rows:
        try:
            ts, o, h, low, close, vol = row[:6]
            out[int(ts)] = (float(o), float(h), float(low), float(close), float(vol))
        except (TypeError, ValueError, IndexError):
            continue
    return out or None
