"""Exact-route EVM refusal capture: our own V4/V3/V2 quoters, read on a research host.

V4 MULTIHOP, STORE ONLY (2026-09-30, Alpha). A coin paired only with a bridge token (USDG, a
tokenized stock) has no single-hop route, and 15 of 41 refusals with an exact row on 4663 were
no_route. When the single-hop scan prices nothing, the lane runs the funded multihop search on OUR
PoolKey store alone (`multihop_quote(store_only=True)`): no GeckoTerminal or DexScreener pool list,
every request through this lane's own seam (`_rpc_http`), which refuses any URL but its read host.
Chains: MEMECOIN_CAPTURE_EVM_MULTIHOP_CHAINS (default 4663). There is no launchpad-curve venue here.

Spec docs/superpowers/specs/2026-09-28-memecoin-evm-exact-refusal-capture-design.md. Imported by
scripts/memecoin_refusal_capture_loop.py only. The dip retry's EVM watch used it for a few hours
on 2026-09-30 and no longer does: it quotes through the funded chain context. Importing the runner
or the dip module never loads this one (tests pin both).

The first observation of a capture BUYS the clip: every stored ETH-side V4 key of the mint, the
funded V3 scan and the funded V2 scan, each through this lane's `call_fn`, and the largest amountOut
wins. That route is FIXED and every later observation SELLS the bag on it. Rows have the Solana
exact shape (`memecoin_refusal_capture.quote_once`) with USD per token in `quote_price`; a sell's
`quote_output_atomic` is quote-token atoms (WETH/WBNB, 18 decimals), priced with `quote_asset_usd`.

NO ADMISSION AUTHORITY, AND NO BUDGET SHARED WITH A MONEY ROLE:
  * every eth_call goes to MEMECOIN_CAPTURE_EVM_READ_URL_<chain>, whose host `usable_read_urls`
    proves no other variable, resolved read pool or free-submission endpoint uses;
  * pair tokens come from the flap Portal on that same host, never from `pair_quote_tokens`, which
    reads DexScreener through the funded seam;
  * the native USD rate is a plain Binance GET on this lane's own cache, never `native_usd_price`,
    whose FMP fallback spends the funded sizing fallback's quota.
A limit or an unreadable answer holds the host and writes nothing: the reaper sees a gap.
"""
from __future__ import annotations

import json
import logging
import math
import queue
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

READ_URL_VAR = "MEMECOIN_CAPTURE_EVM_READ_URL_%s"
PROVIDER = {"v4": "evm_v4_quoter", "v3": "evm_v3_quoter", "v2": "evm_v2_router",
            "v4_multihop": "evm_v4_multihop_quoter"}
_PREFER = {"v4": 0, "v3": 1, "v2": 2, "v4_multihop": 3}   # tie order: the venue most funded fills used
# The store-only multihop search behind this lane's bucket: (pools per step, complete paths,
# deadline s, requests, seconds kept for the path quotes). The funded 5 s deadline would cut a
# search that waits for 4 requests/s; the request cap is what bounds the load, per first buy.
MULTIHOP_LIMITS = (3, 12, 45.0, 40, 12.0)
LIMIT_CODES = frozenset({-32005, -32003})
# A provider that signals its limit in words (drpc: HTTP 429 with code 15 "You reached Public
# endpoint rate limit"). Checked AFTER the revert test: a revert reason that says "max wallet limit"
# is the pool answering, not the provider refusing.
LIMIT_WORDS = re.compile(r"limit|too many|exceed|quota|rate", re.I)
V4_MAX_POOLS = 8
ROUTE_HIT_TTL_S, ROUTE_MISS_TTL_S = 60.0, 120.0
PRICE_TTL_S = 60.0
BINANCE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=%sUSDT"
_TAKER = "0x000000000000000000000000000000000000dEaD"   # quote-only: the calldata is never sent
_SEL_DECIMALS = "0x313ce567"
_TIMEOUT_S = 3.0
_COUNTS = ("calls", "ok", "reverted", "failed", "held")
# A read host that did not answer eth_chainId is asked again this long after each failure; the
# last step repeats. blastapi answered 429 at two relaunches (2026-09-29 17:57, 2026-09-30 15:01Z).
IDENTITY_BACKOFF_S = (30.0, 60.0, 120.0, 300.0)


def _default_http():
    from sniper.execution.evm_wallet import _http
    return _http


def _status_of(exc):
    from sniper.execution.solana_rpc_pool import _exception_status
    return _exception_status(exc) or type(exc).__name__


def _urls(value):
    return [part for part in str(value or "").replace(",", " ").split()
            if "://" in part and urlsplit(part).hostname]


def taken_budgets(env):
    """The rate budget (`budget`) of every URL another role can reach: all environment values except
    this lane's own read URLs, the resolved EVM read pools, and the free-submission registry. (The
    BSC tape's hop seed was a code-set host here until it moved to the keyed dRPC 56 research
    variable, an env var, on 2026-10-02.)

    DERIVED, never listed. The Solana capture guard's first version named eight variables and five
    of them did not exist; a derived set also covers a role added tomorrow."""
    urls = []
    for name, value in env.items():
        if not str(name).startswith("MEMECOIN_CAPTURE_EVM_READ_URL_"):
            urls += _urls(value)
    from sniper.settings import memecoin as cfg
    for pools in (getattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {}) or {},
                  getattr(cfg, "MEMECOIN_EVM_ARCHIVE_RPC_POOL_URLS", {}) or {}):
        for members in pools.values():
            for url in members:
                urls += _urls(url)
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    from sniper.execution.free_endpoint_registry import free_endpoints
    for chain in ("solana", *CHAIN_QUOTE):
        for endpoint in free_endpoints(chain):
            urls += _urls(endpoint.url)
    return {budget(url) for url in urls}


def _domain(host):
    """The registrable domain a provider rate-limits under: publicnode, Alchemy and drpc budget per
    IP or account across their chain hostnames, so `eth-rpc.publicnode.com` shares the budget of
    `robinhood-rpc.publicnode.com`. An IP or a bare name is its own domain.
    ponytail: last two labels; no public-suffix list, since no EVM provider here sits on co.uk."""
    host = str(host or "").lower().strip(".")
    labels = host.split(".")
    if len(labels) <= 2 or all(label.isdigit() for label in labels):
        return host
    return ".".join(labels[-2:])


def budget(url):
    """The rate budget `url` spends: its host's `_domain`. dRPC alone splits (Gamma probe
    2026-10-02): a URL with a `dkey` query spends the keyed account's budget, apart from the keyless
    per-IP hosts (`robinhood.drpc.org`). Every key reads as one budget, and a blank key or a key in
    the path reads as keyless: each refuses more, never less. Never contains the key.
    ponytail: one keyed account; split by a hash of the key if a second dRPC account appears."""
    parts = urlsplit(str(url or "").strip())
    domain = _domain(parts.hostname)
    if domain == "drpc.org" and parse_qs(parts.query).get("dkey"):
        return "drpc.org (keyed)"
    return domain


def _post(http, url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return http("POST", url, body=body, timeout=_TIMEOUT_S)


def _identity(http, url, chain):
    """`(verdict, got, status)` of one eth_chainId read. True: the host answered `chain`. False: it
    answered ANOTHER chain id. None: it did not answer (limit, timeout, error reply, no result),
    so which chain it serves is unknown."""
    status, got = None, None
    try:
        status, reply, _h = _post(http, url, "eth_chainId", [])
        got = int(str(reply.get("result")), 16) if status == 200 else None
    except Exception as exc:
        status, got = status or _status_of(exc), None
    return (None if got is None else got == int(chain)), got, status


def read_urls(env, chains, *, http=None):
    """`(usable, pending)`, each `{chain: url}`. Usable: the read host is set, used by no other
    role, and answered eth_chainId with that chain. Pending: set and unshared, but the identity
    read did not answer; the lane retries it (`ExactLane.retry_pending`). A chain in neither is
    refused for good and logged loudly: no host, a shared host, or a host on another chain."""
    http = http or _default_http()
    taken = taken_budgets(env)
    usable, pending = {}, {}
    for chain in sorted(str(c) for c in chains):
        url = str(env.get(READ_URL_VAR % chain) or "").strip()
        host = (urlsplit(url).hostname or "").lower() if url else ""
        why = None
        if not host:
            why = "%s is not set" % (READ_URL_VAR % chain)
        elif budget(url) in taken:
            why = ("domain %s is used by another role; research load must not share its budget"
                   % budget(url))
        else:
            verdict, got, status = _identity(http, url, chain)
            if verdict is None:
                # Host only, never the URL: a keyed endpoint carries its key in the path.
                logger.warning("evm exact capture: chain %s PENDING: host %s did not answer "
                               "eth_chainId (status %s); retried on a backoff", chain, host, status)
                pending[chain] = url
                continue
            if not verdict:
                why = "host %s answered chain %s (status %s), not %s" % (host, got, status, chain)
        if why:
            logger.error("evm exact capture: chain %s REFUSED: %s", chain, why)
            continue
        usable[chain] = url
    return usable, pending


def usable_read_urls(env, chains, *, http=None):
    """The usable half of `read_urls`, for a one-shot caller that cannot wait for a retry."""
    return read_urls(env, chains, http=http)[0]


def v4_quote_data(key, zero_for_one, amount_in):
    """V4Quoter.quoteExactInputSingle calldata, byte-identical to `evm_v4_quote._quote_single`'s."""
    from sniper.execution.evm_v4_quote import _SEL_QUOTE_SINGLE, _encode_pool_key, _w
    return ("0x" + _SEL_QUOTE_SINGLE + _w(0x20) + _encode_pool_key(key)
            + _w(1 if zero_for_one else 0) + _w(amount_in) + _w(0x100) + _w(0))


def stored_v4_keys(chain, mint):
    """`[(pool_id, key)]` from the ledger's PoolManager-verified key cache. No network."""
    from sniper.execution.evm_v4_quote import _stored_pool_key, _stored_pools_for_mint
    out = []
    for pool_id in _stored_pools_for_mint(chain, mint):
        key = _stored_pool_key(chain, pool_id)
        if key:
            out.append((pool_id, key))
    return out


def _key_pool_id(key):
    """A V4 pool id from its PoolKey: keccak(abi.encode(key)), as the PoolManager derives it."""
    from eth_utils import keccak
    from sniper.execution.evm_v4_quote import _encode_pool_key
    return "0x" + keccak(bytes.fromhex(_encode_pool_key(key))).hex()


def route_label(route):
    kind = route["kind"]
    if kind == "v4":
        return "v4:%s" % route["pool"]
    if kind == "v4_multihop":
        return "v4_multihop:%s" % ">".join(str(p) for p in route.get("pools") or ())
    if kind == "v2":
        return "v2:%s:%s" % (route["router"], ">".join(route["path"]))
    spacing = route.get("fees") or route.get("tick_spacings") or ()
    return "v3:%s:%s:%s" % (route.get("protocol"), ">".join(route["tokens"]),
                            ",".join(str(x) for x in spacing))


def _amount(raw):
    from sniper.execution.evm_dex_quote import _decode_amount_out
    return _decode_amount_out(raw)


def _wall():
    epoch = time.time()
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(), epoch


class _Host:
    """One read host: a token bucket and a hold. Capacity is one second of rate, so no one-second
    window ever sees more than twice the rate."""

    def __init__(self, url, rps, yield_s, clock, sleep):
        self.url = url
        self.name = (urlsplit(url).hostname or "").lower()
        self.rate = max(0.1, float(rps))
        self.tokens, self.stamp = self.rate, clock()
        self.yield_s, self.until = float(yield_s), 0.0
        self.clock, self.sleep = clock, sleep
        self.counts = Counter()

    def held(self):
        return self.clock() < self.until

    def hold(self, why):
        self.until = self.clock() + self.yield_s
        self.counts["held"] += 1
        logger.warning("evm exact capture: %s held %.0fs (%s); missed observations are gaps",
                       self.name, self.yield_s, why)

    def take(self):
        while True:
            now = self.clock()
            self.tokens = min(self.rate, self.tokens + (now - self.stamp) * self.rate)
            self.stamp = now
            if self.tokens >= 1.0 - 1e-9:
                self.tokens -= 1.0
                return
            self.sleep((1.0 - self.tokens) / self.rate)


class ExactLane:
    """The exact EVM capture lane. Single-threaded, owned by the capture worker: every cache, bucket
    and hold lives on this object, never in a module global."""

    def __init__(self, urls, *, rps, yield_s, clip_usd, http=None, keys_fn=None,
                 clock=time.monotonic, sleep=time.sleep, wall=None, pending=None):
        self.http = http or _default_http()
        # `urls` are the proven hosts and `pending` (`{chain: url}`, read_urls' second half) the ones
        # that have not proven their chain id yet. The lane builds its hosts from BOTH, so a caller
        # cannot hand it a pending chain without a host. A chain in both is pending.
        self.hosts = {str(c): _Host(u, rps, yield_s, clock, sleep)
                      for c, u in {**urls, **(pending or {})}.items()}
        # `{chain: (failures, due)}`. No read reaches such a host but the identity check itself.
        # `refused` is for good.
        self.pending = {str(c): (0, clock() + IDENTITY_BACKOFF_S[0]) for c in (pending or {})}
        self.refused = set()
        self.clip_usd, self.yield_s = float(clip_usd), float(yield_s)
        self.keys_fn = keys_fn or stored_v4_keys
        self.clock, self.wall = clock, wall or _wall
        # ponytail: unbounded dicts; a few dozen mints a day over a worker's life. Evict if it grows.
        self._routes, self._decimals, self._pairs = {}, {}, {}
        self._seam_lock = threading.Lock()         # the multihop search quotes paths on threads
        self._pool_warned = set()
        self._price, self._price_until = {}, 0.0
        self.price_counts = Counter()

    def stats(self):
        out = {h.name: {k: h.counts[k] for k in _COUNTS} for h in self.hosts.values()}
        out["binance"] = {k: self.price_counts[k] for k in ("reads", "held")}
        return out

    def _ready(self, chain):
        return chain in self.hosts and chain not in self.pending and chain not in self.refused

    def retry_pending(self):
        """Ask each pending host for its chain id again once its backoff is due. The right id
        admits the chain, another id refuses it for good, no answer waits for the next step."""
        for chain, (failures, due) in list(self.pending.items()):
            if self.clock() < due:
                continue
            host = self.hosts[chain]
            host.take()
            verdict, got, status = _identity(self.http, host.url, chain)
            if verdict is None:
                wait = IDENTITY_BACKOFF_S[min(failures + 1, len(IDENTITY_BACKOFF_S) - 1)]
                self.pending[chain] = (failures + 1, self.clock() + wait)
                logger.warning("evm exact capture: chain %s still PENDING: host %s did not answer "
                               "eth_chainId (status %s); next check in %.0fs",
                               chain, host.name, status, wait)
                continue
            if verdict:
                logger.info("evm exact capture: chain %s admitted: host %s answered its chain id "
                            "after %d failed checks", chain, host.name, failures + 1)
            else:
                self.refused.add(chain)
                logger.error("evm exact capture: chain %s REFUSED: host %s answered chain %s, not %s",
                             chain, host.name, got, chain)
            del self.pending[chain]

    # --- reads -----------------------------------------------------------------------------------

    def _call(self, chain, state):
        """`call_fn(to, data) -> hex or None` for the funded quoters. A revert is an answer (None).
        A limit, a transport error or an unreadable reply marks `state['failed']`, holds the host on
        a limit, and turns every later call of this observation into a no-op."""
        host = self.hosts[chain]

        def call_fn(to, data):
            if state.get("failed"):
                return None
            if host.held():
                state["failed"] = "held"
                return None
            host.take()
            started = self.clock()
            status, reply = None, None
            try:
                status, reply, _h = _post(self.http, host.url, "eth_call", [{"to": to, "data": data}, "latest"])
            except Exception as exc:
                status = _status_of(exc)
            host.counts["calls"] += 1
            state["status"] = status
            state["ms"] = state.get("ms", 0.0) + (self.clock() - started) * 1000.0
            err = reply.get("error") if isinstance(reply, dict) else None
            code = err.get("code") if isinstance(err, dict) else None
            message = str(err.get("message") or "").lower() if isinstance(err, dict) else ""
            if status == 200 and isinstance(reply, dict) and "result" in reply and err is None:
                host.counts["ok"] += 1
                return reply["result"]
            if status == 200 and code not in LIMIT_CODES and (code == 3 or "revert" in message):
                host.counts["reverted"] += 1
                return None
            why = "status=%s code=%s" % (status, code)
            if (status in (403, 429) or code in LIMIT_CODES or LIMIT_WORDS.search(message)
                    or not isinstance(status, int) or status >= 500):
                host.hold(why)
            else:
                host.counts["failed"] += 1
            state["failed"] = why
            return None
        return call_fn

    def _rpc_http(self, chain, state):
        """An HTTP seam for funded READ helpers that speak `http(method, url, body=...)`, with the
        same host, bucket, hold and counters as `_call`. IT REACHES THIS LANE'S READ HOST AND
        NOTHING ELSE: any other URL (a funded pool member, an archive host, a provider) raises
        `RequestNotSent` before a byte is sent. A limit holds the host and fails the observation."""
        from sniper.execution.evm_rpc_pool import RequestNotSent
        host = self.hosts[chain]

        def http(method, url, headers=None, body=None, timeout=None):
            if method != "POST" or url != host.url:
                raise RequestNotSent("evm exact capture: only its own read host")
            with self._seam_lock:
                if state.get("failed"):
                    raise RequestNotSent("evm exact capture: observation already failed")
                if host.held():
                    state["failed"] = "held"
                    raise RequestNotSent("evm exact capture: read host held")
                host.take()
            started = self.clock()
            status, reply, headers_out = None, None, {}
            try:
                status, reply, headers_out = self.http("POST", url, body=body, timeout=_TIMEOUT_S)
            except Exception as exc:
                status = _status_of(exc)
            err = reply.get("error") if isinstance(reply, dict) else None
            code = err.get("code") if isinstance(err, dict) else None
            message = str(err.get("message") or "").lower() if isinstance(err, dict) else ""
            reverted = (status == 200 and code not in LIMIT_CODES
                        and (code == 3 or "revert" in message))
            with self._seam_lock:
                host.counts["calls"] += 1
                state["status"] = status
                state["ms"] = state.get("ms", 0.0) + (self.clock() - started) * 1000.0
                if status == 200 and isinstance(reply, (dict, list)) and err is None:
                    host.counts["ok"] += 1
                    return status, reply, headers_out
                if reverted:
                    host.counts["reverted"] += 1
                    return status, reply, headers_out
                why = "status=%s code=%s" % (status, code)
                if (status in (403, 429) or code in LIMIT_CODES or LIMIT_WORDS.search(message)
                        or not isinstance(status, int) or status >= 500):
                    host.hold(why)
                else:
                    host.counts["failed"] += 1
                state["failed"] = why
            raise RuntimeError("evm exact capture: read failed (%s)" % why)
        return http

    def _multihop(self, chain, mint, amount_in, state):
        """`(amount_out, "v4_multihop", route)` from the funded search on our store alone, or None."""
        from sniper.execution import evm_rpc_pool
        from sniper.execution import evm_v4_multihop as mh
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        from sniper.settings import memecoin as cfg
        if chain not in {str(c) for c in getattr(cfg, "MEMECOIN_CAPTURE_EVM_MULTIHOP_CHAINS", ())}:
            return None
        host = self.hosts[chain]
        if list(evm_rpc_pool.pool_for(host.url)) != [host.url]:
            # `_rpc` would expand the URL into its pool members. The seam refuses them anyway;
            # this says why the lane has no multihop here instead of failing request by request.
            if chain not in self._pool_warned:
                self._pool_warned.add(chain)
                logger.error("evm exact capture: %s read host is a pool member; multihop search "
                             "is off for it", chain)
            return None
        native = self.native_usd(chain)
        if native is None:
            return None
        clip = amount_in / 10 ** int(CHAIN_QUOTE[chain]["decimals"]) * native
        cash = mh._multihop_cash(chain)
        route = mh.multihop_quote(
            chain, cash, mint, amount_in, rpc_url=host.url,
            bridge_currencies=tuple(getattr(cfg, "MEMECOIN_EVM_MULTIHOP_BRIDGES", {}).get(chain, ()) or ()),
            clip_usd=clip, http=self._rpc_http(chain, state), store_only=True, limits=MULTIHOP_LIMITS)
        if not route or state.get("failed"):
            return None
        return (int(route["amount_out"]), "v4_multihop", {
            "kind": "v4_multihop", "cash": cash, "legs_path": route["legs_path"],
            # One pool per LEG, in path order, each id derived from that leg's own key. The
            # search's result names only the first and the last pool, so a 3-leg label lost the
            # middle one.
            "pools": [_key_pool_id(leg["key"]) for leg in route["legs_path"]],
            "search_requests": route.get("search_requests")})

    def native_usd(self, chain):
        """USD per chain quote token: a plain Binance GET on this lane's cache. None when unknown."""
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        quote = CHAIN_QUOTE.get(str(chain)) or {}
        if quote.get("kind") == "usd":
            return 1.0
        symbol = str(quote.get("usd_symbol") or "").upper()
        if not symbol:
            return None
        now = self.clock()
        hit = self._price.get(symbol)
        if hit and hit[0] > now:
            return hit[1]
        if now < self._price_until:
            return None
        self.price_counts["reads"] += 1
        status, reply = None, None
        try:
            status, reply, _h = self.http("GET", BINANCE_URL % symbol, timeout=_TIMEOUT_S)
        except Exception as exc:
            status = _status_of(exc)
        try:
            price = float((reply or {}).get("price")) if status == 200 else None
        except (TypeError, ValueError, AttributeError):
            price = None
        if price is not None and math.isfinite(price) and price > 0:
            self._price[symbol] = (now + PRICE_TTL_S, price)
            return price
        self._price_until = now + self.yield_s
        self.price_counts["held"] += 1
        logger.warning("evm exact capture: Binance %s price unreadable (status %s); held %.0fs",
                       symbol, status, self.yield_s)
        return None

    def _pair_tokens(self, chain, coin, call, state):
        """The flap Portal's quote token for a flap.sh coin (getTokenV6 word 9), else (). The funded
        path asks DexScreener here; 34 of 37 refused 56 mints in a week were flap-style coins."""
        from sniper.execution.evm_flap_curve import (
            FLAP_PORTAL, _NATIVE, _SEL_GET_TOKEN_V6, _address_at, _words)
        portal = FLAP_PORTAL.get(str(chain))
        if not portal or not coin:
            return ()
        key = (str(chain), str(coin).lower())
        if key in self._pairs:
            return self._pairs[key]
        words = _words(call(portal, "0x" + _SEL_GET_TOKEN_V6 + str(coin)[2:].lower().rjust(64, "0")))
        token = _address_at(words, 9) if len(words) > 9 else None
        out = (token,) if token and token != _NATIVE else ()
        if not state.get("failed"):
            self._pairs[key] = out
        return out

    def _scan(self, chain, mint, amount_in, state, kinds=None):
        """`[(amount_out, kind, route)]` across our venues for the clip; [] when nothing prices it.
        `kinds` limits the scan to those venues (the dip watch's hint from the refused route)."""
        from sniper.execution.evm_dex_quote import dex_v3_quote
        from sniper.execution.evm_v2_exit_quote import dex_v2_exit_quote
        from sniper.execution.evm_v4_quote import _NATIVE, V4, _fee_is_sane
        from sniper.execution.evm_wallet import CHAIN_QUOTE, native_is_quote
        from sniper.settings import memecoin as cfg
        call = self._call(chain, state)
        quote = CHAIN_QUOTE[chain]["token"].lower()
        found = []
        conf = V4.get(chain) if kinds is None or "v4" in kinds else None
        if conf:
            # THE FUNDED POOL RULE, mirrored from `evm_v4_quote.v4_quote_amount_out`: a native pool,
            # or a WETH-token pool where funded enables it, and never a fee trap. Without the fee
            # screen the largest buy on 8453 came from an 87% pool and its round trip read -98.7%.
            native_quote = native_is_quote(chain)
            weth_ok = (bool(getattr(cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY", False)) or native_quote
                       or chain in getattr(cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY_CHAINS", ()))
            scale = 10 ** (18 - int(CHAIN_QUOTE[chain]["decimals"])) if native_quote else 1
            usable = []
            for pool_id, key in self.keys_fn(chain, mint):
                c0, c1 = str(key["currency0"]).lower(), str(key["currency1"]).lower()
                if not _fee_is_sane(key.get("fee")):
                    continue
                if c0 == _NATIVE and c1 == mint:
                    usable.append((pool_id, key, True, scale))
                elif weth_ok and quote in (c0, c1) and mint in (c0, c1):
                    usable.append((pool_id, key, c0 == quote, 1))
            for pool_id, key, zero_for_one, pool_scale in sorted(usable, key=lambda u: u[0])[:V4_MAX_POOLS]:
                if amount_in * pool_scale >= 1 << 128:      # uint128 amountIn, as the funded rule
                    continue
                out = _amount(call(conf["quoter"], v4_quote_data(key, zero_for_one, amount_in * pool_scale)))
                if out:
                    found.append((out, "v4", {"kind": "v4", "pool": pool_id, "key": key,
                                              "zero_for_one": zero_for_one, "scale": pool_scale}))

        def pairs(_chain, coin):
            return self._pair_tokens(chain, coin, call, state)
        v3 = (dex_v3_quote(chain, quote, mint, amount_in, _TAKER, call_fn=call, pair_tokens_fn=pairs)
              if kinds is None or "v3" in kinds else None)
        if v3 and int(v3.get("buyAmount") or 0) > 0:
            found.append((int(v3["buyAmount"]), "v3", dict(v3["route"], kind="v3")))
        if kinds is not None and "v2" not in kinds:
            return found
        v2 = dex_v2_exit_quote(chain, quote, mint, amount_in, _TAKER, call_fn=call,
                               tax_buffer_bps=0, pair_tokens_fn=pairs)
        if v2 and int(v2.get("buyAmount") or 0) > 0:
            found.append((int(v2["buyAmount"]), "v2", {"kind": "v2", "router": v2["route"]["router"],
                                                        "path": list(v2["route"]["path"])}))
        if not found and kinds is None and not state.get("failed"):
            # ONLY WHEN NO SINGLE-HOP VENUE PRICES THE COIN: the search costs up to
            # MULTIHOP_LIMITS[3] requests, and a coin with a direct pool does not need it.
            hop = self._multihop(chain, mint, amount_in, state)
            if hop:
                found.append(hop)
        return found

    def _quote(self, chain, route, amount, side, state):
        """One quote on a FIXED route: the buy direction, or reversed for a sell."""
        call = self._call(chain, state)
        kind = route["kind"]
        if kind == "v4":
            from sniper.execution.evm_v4_quote import V4
            scale = int(route.get("scale") or 1)
            if side == "buy":
                return _amount(call(V4[chain]["quoter"], v4_quote_data(
                    route["key"], route["zero_for_one"], amount * scale)))
            out = _amount(call(V4[chain]["quoter"], v4_quote_data(
                route["key"], not route["zero_for_one"], amount)))
            return out // scale if out else None
        if kind == "v4_multihop":
            # The FIXED path, one quoter call: forward for a buy, the reverse path for a sell
            # (the order `evm_v4_multihop._measure_paths` quotes it in).
            from sniper.execution import evm_v4_multihop as mh
            legs, cash = route["legs_path"], route["cash"]
            if side == "buy":
                currency_in = cash
                path = tuple((leg["currency"], leg["key"]) for leg in legs)
            else:
                currency_in = legs[-1]["currency"]
                path = tuple((legs[i - 1]["currency"] if i else cash, legs[i]["key"])
                             for i in range(len(legs) - 1, -1, -1))
            try:
                return mh._quote_path(chain, currency_in, path, int(amount), self.hosts[chain].url,
                                      http=self._rpc_http(chain, state))
            except Exception:
                return None
        if kind == "v2":
            from sniper.execution.evm_v2_exit_quote import _quote_path
            path = route["path"] if side == "buy" else list(reversed(route["path"]))
            try:
                return _quote_path(call, route["router"], int(amount), path)
            except Exception:
                return None
        from sniper.execution.evm_dex_quote import (
            _decode_amount_out, _encode_path, _quote_amount_out, _quote_amount_out_path,
            _ramses_single_quote_data)
        from sniper.settings import memecoin as cfg
        tokens, fees = list(route["tokens"]), list(route.get("fees") or [])
        if side == "sell":
            tokens.reverse()
            fees.reverse()
        best = 0
        # 4663 runs more than one uniswap_v3 factory and the route does not say which one priced
        # it, so every quoter of that protocol is asked and the best answer kept.
        for dep in cfg.memecoin_evm_v3_deployments(chain):
            if str(dep.get("protocol") or "uniswap_v3") != route.get("protocol"):
                continue
            if route.get("protocol") == "ramses_v3":
                spacing = (route.get("tick_spacings") or [0])[0]
                out = _decode_amount_out(call(dep["quoter"], _ramses_single_quote_data(
                    tokens[0], tokens[1], amount, spacing)))
            elif len(tokens) == 2:
                out = _quote_amount_out(call, dep["quoter"], tokens[0], tokens[1], amount, fees[0])
            else:
                out = _quote_amount_out_path(call, dep["quoter"], _encode_path(tokens, fees), amount)
            best = max(best, out or 0)
        return best or None

    def _buy_route(self, chain, mint, amount_in, state, kinds=None):
        """`(route, amount_out)` for the clip. A cached route is re-quoted at this clip, a cached miss
        answers without a call, and a read failure caches nothing."""
        key = (chain, mint) if kinds is None else (chain, mint, tuple(kinds))
        now = self.clock()
        hit = self._routes.get(key)
        if hit and hit[0] > now:
            if hit[1] is None:
                return None, None
            return hit[1], self._quote(chain, hit[1], amount_in, "buy", state)
        found = self._scan(chain, mint, amount_in, state, kinds)
        if state.get("failed"):
            return None, None
        if not found:
            self._routes[key] = (now + ROUTE_MISS_TTL_S, None)
            return None, None
        out, _kind, route = min(found, key=lambda f: (-f[0], _PREFER[f[1]]))
        self._routes[key] = (now + ROUTE_HIT_TTL_S, route)
        return route, out

    def _decimals_of(self, chain, mint, state):
        key = (chain, mint)
        if key not in self._decimals:
            raw = self._call(chain, state)(mint, _SEL_DECIMALS)
            try:
                value = int(str(raw)[2:66], 16) if raw and len(str(raw)) >= 66 else None
            except ValueError:
                value = None
            if value is None or not 0 <= value <= 36:
                return None
            self._decimals[key] = value
        return self._decimals[key]

    def buy_price_usd(self, chain, mint, clip_usd, kinds=None):
        """USD per token for buying `clip_usd` of `mint` on this lane's host, or None when unknown
        (held host, no route, unreadable decimals, no quote-token USD). The same calls and price as
        observe()'s buy. NO PRODUCTION CALLER since the dip watch rebuild of 2026-09-30 (that
        watch now quotes through the funded chain context); kept as the scan's price in one call.
        The quote-token USD is this lane's Binance rate, so its error moves the price 1:1."""
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        chain, mint = str(chain), str(mint).lower()
        if not self._ready(chain) or chain not in CHAIN_QUOTE:
            return None
        native = self.native_usd(chain)
        if native is None:
            return None
        clip = float(clip_usd)
        amount_in = int(clip / native * 10 ** int(CHAIN_QUOTE[chain]["decimals"]))
        state = {}
        route, out = self._buy_route(chain, mint, amount_in, state, kinds)
        decimals = self._decimals_of(chain, mint, state) if route and out else None
        if state.get("failed") or not out or decimals is None:
            return None
        return clip / (int(out) / 10 ** decimals)

    # --- one observation -------------------------------------------------------------------------

    def observe(self, cap):
        """One immutable row for one exact capture, or None when its read host is held, could not
        be read, or has not proven its chain id: a gap, never a retry."""
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        from sniper.persistence.db_memecoin_captures import atomic_text
        chain = str(cap.network_id)
        if not self._ready(chain) or chain not in CHAIN_QUOTE:
            return None
        mint = str(cap.mint).lower()
        clip = float(cap.quote_input_usd) if cap.quote_input_usd is not None else self.clip_usd
        native = self.native_usd(chain)
        now_iso, now_epoch = self.wall()
        side = "buy" if cap.tokens_atomic is None else "sell"
        row = {
            "capture_id": cap.capture_id, "origin_event_id": cap.origin_event_id,
            "network_id": cap.network_id, "mint": cap.mint, "gate": cap.gate,
            "refused_at": cap.refused_at, "observed_at": now_iso, "age_s": cap.age(now_epoch),
            "cadence_s": cap.cadence_s, "horizon_s": cap.horizon_s, "quote_input_usd": clip,
            "provider": PROVIDER.get((cap.route or {}).get("kind")), "expected_obs": cap.expected_obs,
            "endpoint_index": 0, "quote_price": None, "quote_id": None,
            "quote_route_target": route_label(cap.route) if cap.route else None, "quality": None,
            "token_qty_atomic": atomic_text(cap.tokens_atomic), "token_decimals": cap.decimals,
            "quote_output_atomic": None, "quote_min_output_atomic": None,
            "quote_price_basis": "exact_route", "quote_side": side, "http_status": None,
            "latency_ms": None, "quote_asset_usd": native,
            "slippage_available_over_min": cap.slippage_available_over_min,
        }
        state = {}
        if side == "buy":
            if native is None:
                row["quality"] = "price_unknown"         # the clip cannot be sized; no quoter asked
                return row
            # Clip in quote-token atoms. The fixture recorder computes it the same way; keep them equal.
            amount_in = int(clip / native * 10 ** int(CHAIN_QUOTE[chain]["decimals"]))
            route, out = self._buy_route(chain, mint, amount_in, state)
            decimals = self._decimals_of(chain, mint, state) if route and out else None
        else:
            out = self._quote(chain, cap.route, int(cap.tokens_atomic), "sell", state)
        if state.get("failed"):
            return None
        row["http_status"], row["latency_ms"] = state.get("status"), state.get("ms")
        if side == "buy":
            if not route or not out:
                row["quality"] = "no_route"
                return row
            if decimals is None:
                row["quality"] = "provider_error"
                return row
            cap.route, cap.tokens_atomic, cap.decimals = route, int(out), decimals
            row.update(provider=PROVIDER[route["kind"]], quote_route_target=route_label(route),
                       token_qty_atomic=atomic_text(out), token_decimals=decimals,
                       quote_output_atomic=atomic_text(out),
                       quote_price=clip / (int(out) / 10 ** decimals), quality="ok")
            return row
        if not out:
            row["quality"] = "no_route"
            return row
        row["quote_output_atomic"] = atomic_text(out)
        if native is None:
            row["quality"] = "price_unknown"
            return row
        tokens = int(cap.tokens_atomic) / 10 ** int(cap.decimals)
        value = int(out) / 10 ** int(CHAIN_QUOTE[chain]["decimals"]) * native
        row["quote_price"] = value / tokens if tokens > 0 else None
        row["quality"] = "ok" if row["quote_price"] else "no_route"
        return row


def build_worker(env, cfg, *, ledger_path, store_path, horizon_s, cadence_s, handed_off_fn, http=None,
                 clock=time.monotonic, sleep=time.sleep):
    """`(worker, networks)` for the capture loop: the exact lane's worker, not started, and every
    chain it holds, PENDING ONES INCLUDED, so the loop hands it their refusals and the captures are
    gaps until the host proves its chain. `(None, ())` when every wanted chain is refused for good.
    The loop calls this and nothing else: the usable and pending halves are merged in one place."""
    wanted = cfg.memecoin_capture_evm_exact_chains(env)
    usable, pending = read_urls(env, wanted, http=http)
    networks = tuple(sorted({**usable, **pending}))
    logger.info("EVM exact lane: chains=%s pending=%s refused=%s max=%d rps=%.1f/host",
                ",".join(sorted(usable)) or "off", ",".join(sorted(pending)) or "none",
                ",".join(sorted(set(str(c) for c in wanted) - set(networks))) or "none",
                cfg.MEMECOIN_CAPTURE_EVM_EXACT_MAX_CONCURRENT, cfg.MEMECOIN_CAPTURE_EVM_EXACT_MAX_RPS)
    if not networks:
        return None, ()
    lane = ExactLane(usable, pending=pending, rps=cfg.MEMECOIN_CAPTURE_EVM_EXACT_MAX_RPS,
                     yield_s=cfg.MEMECOIN_CAPTURE_EVM_EXACT_YIELD_S, clip_usd=cfg.MEMECOIN_CAPTURE_CLIP_USD,
                     http=http, clock=clock, sleep=sleep)
    worker = ExactWorker(lane, ledger_path=ledger_path, store_path=store_path, horizon_s=horizon_s,
                         cadence_s=cadence_s, max_concurrent=cfg.MEMECOIN_CAPTURE_EVM_EXACT_MAX_CONCURRENT,
                         poll_s=float(cfg.MEMECOIN_CAPTURE_POLL_S), handed_off_fn=handed_off_fn, clock=clock)
    return worker, networks


class ExactWorker:
    """The exact lane on its OWN THREAD (Alpha, 2026-09-28, from Beta's C6). A first-buy scan is 15
    to 30 eth_calls at 4 calls/s, several seconds, and the Solana exact captures are the instrument
    for every cap study: they must never wait for it. A time budget per pump cannot finish a scan,
    so the lane runs apart. This thread owns its capture dict, its read-only ledger handle and its
    store handle; the capture loop only hands it refusals through `submit`, a thread-safe queue.
    An exception in a pass is logged and counted, never fatal, and `stats()` carries the thread's
    own last pass so a silent death is visible in the loop's heartbeat."""

    def __init__(self, lane, *, ledger_path, store_path, horizon_s, cadence_s, max_concurrent,
                 poll_s, handed_off_fn, clock=time.monotonic):
        self.lane = lane
        self.ledger_path, self.store_path = str(ledger_path), store_path
        self.horizon_s, self.cadence_s = float(horizon_s), float(cadence_s)
        self.max_concurrent, self.poll_s = int(max_concurrent), float(poll_s)
        self.handed_off_fn, self.clock = handed_off_fn, clock
        self.inbox = queue.SimpleQueue()
        self.captures = {}
        self.last_pass, self.pass_ms, self.errors = None, None, 0
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="evm-exact-capture", daemon=True)

    def submit(self, refusal):
        self.inbox.put(dict(refusal))

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()

    def stats(self):
        return {"alive": self.thread.is_alive(), "active": len(self.captures),
                "last_pass": self.last_pass, "pass_ms": self.pass_ms, "errors": self.errors,
                "pending": sorted(list(self.lane.pending)), "refused": sorted(list(self.lane.refused)),
                "hosts": self.lane.stats()}

    def run_once(self, conn, ledger):
        """One pass: retry the pending hosts, take new refusals, retire handed-off coins, serve
        the due captures."""
        from sniper.persistence import db_memecoin_captures as store
        from sniper.services import memecoin_refusal_capture as cap
        self.lane.retry_pending()
        started = self.clock()
        now_iso, now_epoch = _wall()

        def on_refuse(cid, outcome, mint, gate, refused_at):
            store.terminal_row(conn, cid, outcome, mint=mint, gate=gate, refused_at=refused_at,
                               horizon_s=self.horizon_s,
                               expected_obs=int(self.horizon_s // self.cadence_s))
        while True:
            try:
                r = self.inbox.get_nowait()
            except queue.Empty:
                break
            if str(r["network_id"]) in self.lane.refused:      # its host answered another chain
                continue
            cap.start(self.captures, mint=r["mint"], origin_event_id=r["origin_event_id"],
                      gate=r["gate"], refused_at=r["refused_at"], network_id=r["network_id"],
                      horizon_s=self.horizon_s, cadence_s=self.cadence_s,
                      max_concurrent=self.max_concurrent, now=started, on_refuse=on_refuse,
                      now_epoch=now_epoch, quote_input_usd=r.get("quote_input_usd"), exact=True)
        for c in list(self.captures.values()):
            if c.done:
                continue
            pid = self.handed_off_fn(ledger, c)
            if pid is not None:
                c.done = True
                store.terminal_row(conn, c.capture_id, "handed_off_to_position", mint=c.mint,
                                   gate=c.gate, refused_at=c.refused_at, horizon_s=self.horizon_s,
                                   expected_obs=c.expected_obs, quote_id=str(pid))
        written = cap.pump(self.captures, now_mono=started, now_iso=now_iso, http=None, rpc_url=None,
                           clip_usd=self.lane.clip_usd, now_epoch=now_epoch, exact_lane=self.lane,
                           record_fn=lambda row: store.record_observation(conn, row))
        for key in [k for k, c in self.captures.items() if c.done]:
            self.captures.pop(key, None)
        self.pass_ms = (self.clock() - started) * 1000.0
        self.last_pass = now_iso
        return written

    def _run(self):
        from sniper.persistence import db_memecoin_captures as store
        from sniper.persistence.db_core import get_readonly_connection
        conn = store.connect(self.store_path)
        ledger = get_readonly_connection(self.ledger_path)
        while not self._stop.is_set():
            try:
                self.run_once(conn, ledger)
            except Exception:
                self.errors += 1
                logger.exception("evm exact capture: pass failed; the thread continues")
            self._stop.wait(self.poll_s)
