"""B: V4 multi-hop through a bridge currency (spec 2026-08-30 Amendment 5). DECISION LAYER ONLY.

31% of armed-wallet 4663 coins have no sane ETH-side pool but do have a sane pool quoted in USDG
or a tokenized equity — that is where the chain's depth actually sits. Measured four-leg round
trips, both directions, ETH -> USDG -> coin and back: -6.34% on a $9,083 pool, -5.52% on $4,816,
and -15.51% on $368.

THE FINDING THAT MOTIVATES THIS: the single-hop 4663 baseline already on record is -6.3% / -6.9%,
so FOUR legs cost the SAME as the TWO we accept today, on coins we cannot trade at all. HOP COUNT
IS NOT WHAT SETS THE TAX. Clip-vs-depth is, and the bridge is cheap enough (0.046% on $2.5M) that
the coin leg dominates. The "two hops doubles the spread" intuition is measurably false here.

This module finds and quotes the route. The transaction module builds the entry and exit.

Bridge selection deliberately prefers a FIXED fee over depth — see `_select_bridge`.
"""
from __future__ import annotations
import math
import threading
import time
# Module level on purpose: a lazy import inside a deadline wait blocks on the import lock while a
# worker thread imports, and the first search then overran a 20 ms deadline by 100 ms.
from concurrent.futures import ThreadPoolExecutor, wait

from sniper.execution.evm_rpc_pool import RequestNotSent

import logging

logger = logging.getLogger(__name__)

# V4's dynamic-fee sentinel: a hook prices each swap. `_fee_is_sane` allows it on purpose (the
# real Pons coin pools use fee=0 with a hook), but it means the fee screen cannot BOUND the cost.
DYNAMIC_FEE_FLAG = 0x800000
_QUOTE_EXACT_INPUT = "ca253dc9"


def _pools_with_depth(chain, token, *, include_store=True, pair_token=None, memo=False, **kw):
    """[(pool_id, depth_usd)] for a token, unioned across both providers, deepest first.

    `include_store=False` leaves out our store's pools, which carry NO depth. A caller that admits
    only measured depth (_bridge_options' eligible) asks for that: for a popular bridge the store
    holds ~100k pools, a ~600 ms read that could not add one option (2026-09-27).

    ONE call returns the id AND its depth, deliberately. The first version of this module had a
    separate `_depth_usd_for(chain, pool_id)` reading `_reserve_cache` — which is keyed on the
    MINT and stores only the deepest pool's reserve, so the lookup missed every time, depth read
    as unknown, and the module fail-closed on 100% of routes. The tests monkeypatched the depth
    function, so they proved the LOGIC and never exercised the WIRING.

    Pairing the id with its depth at the source removes the mismatched pair rather than fixing the
    key, so there is no second lookup left to get wrong.
    """
    from sniper.execution.evm_v4_quote import (
        _gt_pools_ranked, _ds_pools_ranked, _stored_pools_for_mint)
    merged = {}
    pair_kw = {"pair_token": pair_token} if pair_token is not None else {}
    # BOTH PROVIDERS AT ONCE (Alpha GO 2026-10-17). DexScreener waited for GeckoTerminal although
    # neither reads the other: on the six 4663 sends v4_multihop won since 10-14 the serial pair cost
    # the shorter call again (median ~65 ms). Same calls, same results, same merge order.
    from sniper.settings import memecoin as cfg
    together = (_concurrently if getattr(cfg, "MEMECOIN_EVM_MULTIHOP_PROVIDERS_CONCURRENT", True)
                else lambda first, second: (first(), second()))
    (gt, gt_failed), (ds, ds_failed) = together(
        lambda: _provider_pools(
            _gt_pools_ranked, chain, token, budget=kw.get("provider_budget"), memo=memo,
            gt_http=kw.get("gt_http"), request_http=kw.get("gt_request_http"), **pair_kw),
        lambda: _provider_pools(
            _ds_pools_ranked, chain, token, budget=kw.get("provider_budget"),
            cache=kw.get("ds_pool_cache"), memo=memo,
            ds_http=kw.get("ds_http"), request_http=kw.get("ds_request_http"), **pair_kw))
    if gt_failed and ds_failed and not include_store:
        return []
    if gt_failed and ds_failed:
        # BOTH PROVIDERS DARK: their silence is not evidence, but OUR store is (2026-09-12). This
        # returned [] and so skipped the store, which is exactly when it is the only source left:
        # with GeckoTerminal and DexScreener forced blind, ANIME and four stock-paired coins
        # refused `no_bridge_candidates` although every pool on their route is in the store. The
        # store's pools carry no depth and stay behind any measured pool as below; the rung's
        # blindness attribution reads the provider state separately and is unchanged.
        return [(pid, None) for pid in _stored_pools_for_mint(chain, token)]
    for reserve, pid in list(gt) + list(ds):
        if pid not in merged or (reserve or 0) > merged[pid]:
            merged[pid] = reserve or 0.0
    known = sorted(((pid, d) for pid, d in merged.items()), key=lambda x: -x[1])
    # THE INDEXERS LAG THE COINS WE COPY (2026-09-04). Both providers need MINUTES to list a fresh
    # 4663 coin, and a copy signal arrives seconds after the wallet's buy, so this returned nothing
    # exactly when it mattered and the rung declined. Measured that day: GPU refused as
    # `no_route_all_venues` at 10:21, and the very same route quoted fine fourteen minutes later
    # once DexScreener had caught up -- WETH -> NVDA -> GPU through a $14,102 pool.
    #
    # Our own PoolKey cache knows those pools within seconds of the Initialize event. It carries NO
    # DEPTH, so these are appended with depth None and stay STRICTLY BEHIND every measured pool.
    # `_select_coin_leg` decides what an unknown depth is allowed to do; this function only stops
    # pretending the pool does not exist.
    if not include_store:
        return known
    seen = {pid for pid, _ in known}
    unknown = [(pid, None) for pid in _stored_pools_for_mint(chain, token) if pid not in seen]
    return known + unknown


def _pool_key_for(chain, pool_id, rpc_url, **kw):
    from sniper.execution.evm_v4_quote import _pool_key
    return _pool_key(chain, pool_id, rpc_url, http=kw.get("http"), pair=kw.get("pair"))


def _direct_bridge_pools(chain, cash, bridge, *, memo=False, **kw):
    """Return DexScreener V4 pools for one exact pair, or None for the full-provider fallback."""
    from sniper.execution.evm_v4_quote import _ds_pools_ranked

    ranked, failed = _provider_pools(
        _ds_pools_ranked, chain, bridge, budget=kw.get("provider_budget"),
        cache=kw.get("ds_pool_cache"), memo=memo,
        ds_http=kw.get("ds_http"), pair_token=cash, request_http=kw.get("ds_request_http"),
    )
    if failed or not ranked:
        return None
    return [(pid, depth) for depth, pid in ranked]


def _quote_leg(chain, key, zero_for_one, amount_in, rpc_url, **kw):
    from sniper.execution.evm_v4_quote import _quote_single
    return _quote_single(chain, key, zero_for_one, amount_in, rpc_url, **kw)


def quote_path_data(currency_in, path, amount_in):
    """The V4Quoter quoteExactInput calldata for `path` = [(currency out of the hop, PoolKey), ...]."""
    from eth_abi import encode
    params = (
        currency_in,
        [(currency, int(key["fee"]), int(key["tick_spacing"]), key["hooks"], b"")
         for currency, key in path],
        int(amount_in),
    )
    return "0x" + _QUOTE_EXACT_INPUT + encode(
        ["(address,(address,uint24,int24,address,bytes)[],uint128)"], [params],
    ).hex()


def _quote_path(chain, currency_in, path, amount_in, rpc_url, block="latest", **kw):
    """Quote one complete V4 exact-input path at `block` (hex number or "latest")."""
    from sniper.execution.evm_read_rpc import _rpc
    from sniper.execution.evm_v4_quote import V4

    data = quote_path_data(currency_in, path, amount_in)
    try:
        # FRESH ONLY AT "latest" (2026-09-22). A pinned historical block may not exist on another
        # pool member yet, and a search that pins a block on one node must quote on that node.
        at = block or "latest"
        from sniper.execution.evm_read_rpc import _freshest_read_member
        url = _freshest_read_member(rpc_url) if at == "latest" else rpc_url
        response = _rpc(
            url,
            "eth_call",
            [{"to": V4[str(chain)]["quoter"], "data": data}, at],
            http=kw.get("http"),
        )
        raw = response.get("result")
        out = int(raw[2:66], 16) if raw and raw != "0x" else 0
        return out or None
    except Exception as exc:
        if kw.get("raise_blind"):          # the dip watch's pinned tick: a failed READ is typed
            from sniper.execution.evm_v4_quote import pinned_blind
            raise pinned_blind(exc) from exc
        return None


def pinned_path_out(chain, legs_path, amount_atomic, rpc_url, **kw):
    """Tokens out of one exact-input quote through a KNOWN forward path, or None.

    The dip watch's tick on a multihop refusal (2026-09-30): the refusal's own `legs_path`, one
    quoter call, no pool discovery and so no DexScreener/GeckoTerminal read.
    """
    try:
        forward = tuple((leg["currency"], leg["key"]) for leg in legs_path)
    except (KeyError, TypeError):
        return None
    # Raises `evm_v4_quote.PinnedQuoteBlind` when the quoter READ fails.
    return _quote_path(chain, _multihop_cash(chain), forward, amount_atomic, rpc_url,
                       raise_blind=True, **kw)


def _fee_ok(fee):
    from sniper.execution.evm_v4_quote import _fee_is_sane
    return _fee_is_sane(fee)


def _pair_of(key, a, b):
    """(matches, a_is_currency0) for a pool joining exactly a and b, else (False, None)."""
    c0, c1 = str(key["currency0"]).lower(), str(key["currency1"]).lower()
    if {c0, c1} != {str(a).lower(), str(b).lower()}:
        return False, None
    return True, c0 == str(a).lower()


_BRIDGE_LAST_GOOD = {}
_BRIDGE_LAST_GOOD_TTL_S = 6 * 3600.0

# --- ON-CHAIN PAIR PICK: no provider in the loop (2026-09-12) ---------------------------------
#
# Bridge and middle legs were found only through GeckoTerminal and DexScreener. DexScreener never
# returns ETH/USDG (it lists USDG as the quote and discovery keeps base == USDG), and the shared
# GeckoTerminal guard is spent on busy hours, so stock- and fxJPY-paired 4663 coins read
# "bridge missing" while the route existed (ANIME 08:10Z). The runner already holds every V4 pool
# key on the chain (memecoin_evm_v4_pool_keys, verified against the PoolManager's Initialize
# event), so the pools joining two currencies are one indexed query, and the chain itself says
# which is deepest: in-range liquidity read from the PoolManager's own storage (extsload,
# StateLibrary layout: pools mapping at slot 6, liquidity at offset 3). Hookless pools only: a hook
# can change how a swap settles, and this is the leg that carries the volume. Measured on ETH/USDG:
# the most-liquid hookless pool also gave the best 0.01 ETH quote (25.262 vs 25.244 USDG through
# the pool the providers picked).
_EXTSLOAD = "1e2eaeaf"
# extsload(bytes32[]): every candidate pool of a pair in ONE eth_call (2026-09-28). At most
# _LIQUIDITY_BATCH_SLOTS slots per call; each call takes one from the decision's read allowance.
_EXTSLOAD_BATCH = "dbd035ff"
_LIQUIDITY_BATCH_SLOTS = 256
_POOLS_SLOT = 6
_LIQUIDITY_OFFSET = 3
_PAIR_PICK_TTL_S = 600.0
_POOL_LIQUIDITY = {}                        # (chain, pool_id) -> (monotonic time, liquidity)
_ZERO_HOOKS = "0x" + "00" * 20


def _stored_pair_pools(chain, a, b):
    """[(pool_id, key)] joining `a` and `b`, from the runner's own verified PoolKey store."""
    import json
    from sniper.execution.evm_v4_quote import V4
    from sniper.persistence.db_core import get_readonly_connection
    conf = V4.get(str(chain))
    if not conf or not a or not b:
        return []
    c0, c1 = sorted((str(a).lower(), str(b).lower()))
    conn = None
    try:
        conn = get_readonly_connection()
        rows = conn.execute(
            "SELECT pool_id, key_json FROM memecoin_evm_v4_pool_keys "
            "WHERE network_id=? AND pool_manager=? "
            "AND lower(json_extract(key_json,'$.currency0'))=? "
            "AND lower(json_extract(key_json,'$.currency1'))=?",
            (str(chain), conf["pool_manager"], c0, c1)).fetchall()
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()
    out = []
    for pid, raw in rows:
        try:
            key = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(key, dict):
            out.append((str(pid).lower(), key))
    return out


def _liquidity_slot(pool_id):
    """The PoolManager storage slot holding one pool's in-range liquidity, as 64 hex digits."""
    from eth_utils import keccak
    state = int.from_bytes(
        keccak(bytes.fromhex(str(pool_id)[2:]) + _POOLS_SLOT.to_bytes(32, "big")), "big")
    return (state + _LIQUIDITY_OFFSET).to_bytes(32, "big").hex()


def _onchain_liquidities(chain, pool_ids, rpc_url, **kw):
    """{pool_id: in-range liquidity} for up to _LIQUIDITY_BATCH_SLOTS pools in ONE eth_call, or None.

    ALL OR NOTHING. The answer is an ABI bytes32[]: offset 32, a length, then one word per slot.
    Anything else -- an error, a short or long answer, a length word that disagrees with the
    request -- is None, so no pool of the chunk gets a reading and none is remembered; the next
    decision reads them again. Partial decoding would pair words with the wrong pools.
    """
    from sniper.execution.evm_read_rpc import _freshest_read_member, _rpc
    from sniper.execution.evm_v4_quote import V4
    ids = [str(pid) for pid in pool_ids]
    if not ids or len(ids) > _LIQUIDITY_BATCH_SLOTS:
        return None
    try:
        data = ("0x" + _EXTSLOAD_BATCH + "%064x" % 32 + "%064x" % len(ids)
                + "".join(_liquidity_slot(pid) for pid in ids))
        response = _rpc(_freshest_read_member(rpc_url), "eth_call", [
            {"to": V4[str(chain)]["pool_manager"], "data": data}, "latest"], http=kw.get("http"))
        raw = response.get("result") if isinstance(response, dict) else None
        if not isinstance(raw, str) or not raw.startswith("0x"):
            return None
        body = bytes.fromhex(raw[2:])
        if (len(body) != 64 + 32 * len(ids) or int.from_bytes(body[:32], "big") != 32
                or int.from_bytes(body[32:64], "big") != len(ids)):
            return None
        mask = (1 << 128) - 1
        return {pid: int.from_bytes(body[64 + 32 * i:96 + 32 * i], "big") & mask
                for i, pid in enumerate(ids)}
    except Exception:
        return None


def _onchain_liquidity(chain, pool_id, rpc_url, **kw):
    """In-range liquidity (uint128) of one V4 pool from the PoolManager's storage, or None."""
    from sniper.execution.evm_read_rpc import _rpc
    from sniper.execution.evm_v4_quote import V4
    try:
        slot = _liquidity_slot(pool_id)
        from sniper.execution.evm_read_rpc import _freshest_read_member
        response = _rpc(_freshest_read_member(rpc_url), "eth_call", [
            {"to": V4[str(chain)]["pool_manager"], "data": "0x" + _EXTSLOAD + slot}, "latest"],
            http=kw.get("http"))
        raw = response.get("result") if isinstance(response, dict) else None
        return int(raw, 16) & ((1 << 128) - 1) if raw and raw != "0x" else None
    except Exception:
        return None


class _SearchBudget:
    """One deadline and one request count for a complete route search (2026-09-17).

    LuluDior (0x46f3c721, 4663) refused at -15.4% through a 5.01%-fee ETH/LULU pool, while a
    0.50% pool and a three-leg USDG path measured -6.2% and -4.7% on the same chain minutes later.
    Each selector returned one pool, so the search never measured the alternatives. Measuring
    alternatives costs requests, so the search takes a bounded set under this budget.

    THE COUNT IS OUTBOUND REQUESTS, TAKEN AT DISPATCH (review of b526531c). Every RPC request of
    the search passes through `_BudgetedHttp`, so nested key scans, hedged duplicates, rotation
    retries and identity probes are each counted, and none is sent after the deadline. A provider
    HTTP call is counted after its shared rate guard, at dispatch.
    A request already in progress is not cancelled.

    Discovery stops `quote_time_s` before the deadline and may not spend `quote_reserve` requests,
    so pool discovery cannot take the time or the requests that the path quotes need.
    """

    def __init__(self, deadline_s, max_requests, clock=time.monotonic, quote_reserve=0,
                 quote_time_s=0.0):
        self._clock = clock
        now = clock()
        self._end = now + max(0.0, float(deadline_s))
        self._discovery_end = self._end - max(0.0, min(float(quote_time_s), float(deadline_s)))
        self._left = max(0, int(max_requests))
        # DISCOVERY CANNOT SPEND THE QUOTES (2026-09-17). A cold store scan of USDG/LULU is 114
        # liquidity reads; without a reserve it spent the whole budget and no path was measured.
        self._reserve = max(0, min(int(quote_reserve), self._left))
        self._lock = threading.Lock()
        self.used = 0
        self.discovery_cut = False
        # THE LIQUIDITY CAP BELONGS TO THE DECISION (audit E5, 2026-09-18). It was applied inside
        # the per-PAIR scan, so a two-pair search spent it twice: one real multihop_quote issued 64
        # `extsload` reads against the endpoint the cap exists to spare, while the comment and the
        # commit message both said 32 per decision. The allowance is held here, where the decision
        # is, and every pair draws from the same pool.
        self._liquidity_left = None
        self.liquidity_reads_used = 0
        self.stop_reason = None

    def expired(self, discovery=False):
        with self._lock:
            end = self._discovery_end if discovery else self._end
            if self._clock() >= end:
                if discovery:
                    self.discovery_cut = True
                else:
                    self.stop_reason = self.stop_reason or "deadline"
                return True
            return False

    def take(self, n=1, discovery=False):
        """Reserve `n` requests. False = do not send.

        A refused discovery request sets `discovery_cut`; a refused quote request sets
        `stop_reason`.
        """
        with self._lock:
            now = self._clock()
            if discovery:
                if now >= self._discovery_end or self._left - n < self._reserve:
                    self.discovery_cut = True
                    return False
            else:
                if now >= self._end:
                    self.stop_reason = self.stop_reason or "deadline"
                    return False
                if self._left < n:
                    self.stop_reason = self.stop_reason or "request_budget"
                    return False
            self._left -= n
            self.used += n
            return True

    def remaining_s(self, discovery=False):
        end = self._discovery_end if discovery else self._end
        return max(0.0, end - self._clock())


    def take_liquidity_reads(self, wanted):
        """Hand out up to `wanted` pair-liquidity reads from the DECISION's allowance.

        A GRANT SHORT OF `wanted` IS THE SAME BLINDNESS `take()` ALREADY NAMES (EX2, 2026-09-20).
        `take()` sets `discovery_cut` the moment a discovery request is refused; this allowance is
        also a discovery limit, but it truncates the CANDIDATE LIST before dispatch instead of
        refusing a request, so nothing here was marking it. A cold first bridge could spend the
        whole shared cap, leave the second bridge zero reads, and the search's own bottom-of-
        function check (`if budget.discovery_cut: ...`) had nothing to see -- the unread bridge
        came out as `bridge_pool_unusable`, indistinguishable from a bridge with no pool at all.
        """
        cap, _workers = _liquidity_read_limits()
        wanted = max(0, int(wanted))
        with self._lock:
            if self._liquidity_left is None:
                self._liquidity_left = cap
            granted = min(wanted, self._liquidity_left)
            self._liquidity_left -= granted
            self.liquidity_reads_used += granted
            if granted < wanted:
                self.discovery_cut = True
        return granted


class _BudgetedHttp:
    """The search's RPC transport: counts each outbound request and refuses it after the limit.

    A refusal raises `RequestNotSent` before any bytes are sent. The RPC pool and the PoolKey scan
    treat that as the caller's limit, never as an endpoint failure or provider blindness.
    """

    def __init__(self, budget, base, *, discovery):
        self._budget = budget
        self._base = base
        self._discovery = discovery

    def __call__(self, *args, **kwargs):
        if not self._budget.take(discovery=self._discovery):
            raise RequestNotSent("multihop search limit")
        return self._base(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._base, name)

    def request_remaining_s(self):
        """Return the caller's remaining time for an RPC wait."""
        return self._budget.remaining_s(discovery=self._discovery)


def _metadata_requests(call, user_agent, remaining_s):
    """Owned pooled GETs through `call`: the shared CDN headers and a bounded redirect walk."""
    from urllib.error import HTTPError
    from urllib.parse import urljoin, urlsplit

    def request(method, url, headers=None, body=None, timeout=10):
        # Share the common CDN Accept variant; successful bodies still require strict JSON.
        h = {"user-agent": user_agent, "accept": "*/*"}
        h.update(headers or {})
        visited = {}
        for _ in range(10):
            status, payload, response_headers = call(
                method, url, headers=h, body=body, timeout=min(float(timeout), remaining_s()))
            if 200 <= status < 300:
                return status, payload, response_headers
            if status in (301, 302, 303, 307, 308):
                location = (response_headers or {}).get("Location")
                target = urljoin(url, location) if location else ""
                if urlsplit(target).scheme in {"http", "https"} and visited.get(target, 0) < 4:
                    visited[target] = visited.get(target, 0) + 1
                    url = target
                    continue
            raise HTTPError("", status, "metadata HTTP status %d" % status, response_headers, None)
        raise HTTPError("", status, "metadata redirect limit", response_headers, None)
    return request


# ONE UNBUDGETED ADAPTER PER (owned transport, user agent) (Codex F5, 2026-10-17): the bridge memo's
# transport identity and its refresh transport. A per-search closure as the key missed across
# searches and refreshed through that search's expired budget (RequestNotSent before dispatch).
_METADATA_REFRESH = {}


def _metadata_refresh_http(base, user_agent):
    key = (base, user_agent)
    try:
        hash(key)
    except TypeError:
        return None                      # an unhashable transport keeps today's per-search key
    with _BRIDGE_MEMO_LOCK:
        adapter = _METADATA_REFRESH.get(key)
        if adapter is None:
            if len(_METADATA_REFRESH) >= 32:      # ponytail: crude bound; owned transports are few
                _METADATA_REFRESH.clear()
            adapter = _METADATA_REFRESH[key] = _metadata_requests(base, user_agent, lambda: math.inf)
        return adapter


def _metadata_http(base, budget, user_agent):
    """Owned pooled GETs, with every redirect counted inside the discovery budget."""
    counted = _BudgetedHttp(budget, base, discovery=True)
    request = _metadata_requests(counted, user_agent, lambda: budget.remaining_s(discovery=True))
    request.request_remaining_s = counted.request_remaining_s
    refresh = _metadata_refresh_http(base, user_agent)
    if refresh is not None:
        request._memo_transport = refresh
    return request


def _search_limits():
    """(pools per step, maximum complete paths, deadline seconds, request budget, quote seconds)."""
    from sniper.settings import memecoin as cfg
    return (
        max(1, int(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_POOLS_PER_STEP", 3) or 1)),
        max(1, int(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_PATHS", 12) or 1)),
        max(0.01, float(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_SEARCH_DEADLINE_S", 5.0) or 5.0)),
        max(2, int(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_REQUEST_BUDGET", 100) or 2)),
        max(0.0, float(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_QUOTE_TIME_S", 1.5) or 0.0)),
    )


def _liquidity_read_limits():
    """(maximum pair-liquidity reads per decision, concurrent reads)."""
    from sniper.settings import memecoin as cfg
    return (
        max(1, int(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_LIQUIDITY_READS", 32) or 1)),
        max(1, int(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_LIQUIDITY_WORKERS", 4) or 1)),
    )


def _liquidity_reads_allowed(budget, wanted):
    """How many pair-liquidity reads this scan may issue now.

    Without a budget the cap is per call, which is the old behaviour and the right one for a caller
    that is not part of a bounded search.
    """
    cap, _workers = _liquidity_read_limits()
    wanted = max(0, int(wanted))
    if budget is None:
        return min(wanted, cap)
    return budget.take_liquidity_reads(wanted)


def _budget_stops(budget):
    return budget is not None and budget.expired(discovery=True)


def _concurrently(first, second):
    """(first(), second()): `second` on a helper thread that records into this thread's provider
    tap, `first` here. The wait is the longer call, never more than the two in turn; the helper's
    exception (a budget's RequestNotSent included) is raised here after `first` returns."""
    from sniper.execution import evm_wallet
    calls, box = getattr(evm_wallet._HTTP_TAP, "calls", None), {}

    def run():
        evm_wallet._HTTP_TAP.calls = calls
        try:
            box["value"] = second()
        except BaseException as exc:            # re-raised on the caller's thread below
            box["error"] = exc
    helper = threading.Thread(target=run, name="evm-multihop-provider-pair", daemon=True)
    helper.start()
    try:
        out = first()
    finally:
        helper.join()
    if "error" in box:
        raise box["error"]
    return out, box["value"]


# THE BRIDGE-LEG PROVIDER MEMO (Alpha GO 2026-10-17). A bridge leg's provider lookup is the same
# GeckoTerminal/DexScreener URL for every coin (tokens/{bridge}/pools, the pair filtered after), and
# on the six 4663 sends v4_multihop won since 10-14 it was the largest serial wait (GeckoTerminal
# median ~380 ms per send). An answer is served while younger than
# MEMECOIN_EVM_MULTIHOP_BRIDGE_MEMO_MAX_AGE_S (0 = off, today's live call), refreshed every
# MEMECOIN_EVM_MULTIHOP_BRIDGE_REFRESH_S off the entry path; a failed refresh keeps the last
# answer until the age bound, then the live call returns. A refused read (provider failed) or a cut
# read is never stored. Keyed by the read, chain, token, pair and the base transport, so an
# injected transport never shares an answer; a bridge nobody asked for in _BRIDGE_MEMO_IDLE_S is
# dropped and no longer refreshed.
_BRIDGE_MEMO = {}
_BRIDGE_MEMO_LOCK = threading.Lock()
_BRIDGE_MEMO_IDLE_S = 6 * 3600.0
_BRIDGE_REFRESHER = [None]
# For the heartbeat (bridge_memo_status): served/stored answers, refresh rounds and what each GT
# refresh met. A GT refresh is skipped while GeckoTerminal cools down or a search runs.
_BRIDGE_STATS = {"served": 0, "stored": 0, "rounds": 0, "refreshed": 0, "refresh_failed": 0,
                 "gt_skipped_busy": 0, "gt_skipped_cooldown": 0, "gt_skipped_search": 0,
                 "gt_429": 0, "dropped_idle": 0}


def bridge_memo_status():
    """{entries, refresher_alive, and the _BRIDGE_STATS counters}, JSON-safe, for the heartbeat."""
    with _BRIDGE_MEMO_LOCK:
        out = dict(_BRIDGE_STATS, entries=len(_BRIDGE_MEMO))
        thread = _BRIDGE_REFRESHER[0]
    out["refresher_alive"] = bool(thread is not None and thread.is_alive())
    return out


def _bridge_memo_max_age_s():
    from sniper.settings import memecoin as cfg
    return float(getattr(cfg, "MEMECOIN_EVM_MULTIHOP_BRIDGE_MEMO_MAX_AGE_S", 0.0) or 0.0)


def _bridge_memo_key(read, args, kwargs):
    if kwargs.get("gt_http") is not None or kwargs.get("ds_http") is not None:
        return None                     # an injected transport's answers are not the provider's
    if _bridge_memo_max_age_s() <= 0:
        return None
    base = kwargs.get("request_http")
    # The search's _BudgetedHttp wraps one shared transport; an owned metadata adapter names its
    # stable unbudgeted twin (F5).
    base = getattr(base, "_memo_transport", None) or getattr(base, "_base", base)
    return (read, str(args[0]), str(args[1]).lower(), str(kwargs.get("pair_token") or "").lower(), base)


def _bridge_memo_get(key, now=None):
    if key is None:
        return None
    now = time.monotonic() if now is None else now
    with _BRIDGE_MEMO_LOCK:
        entry = _BRIDGE_MEMO.get(key)
        if entry is None:
            return None
        entry["asked"] = now
        if now - entry["at"] > _bridge_memo_max_age_s():
            return None
        _BRIDGE_STATS["served"] += 1
        return list(entry["result"][0]), entry["result"][1]


def _bridge_memo_put(key, result, now=None):
    if key is None or not (isinstance(result, tuple) and len(result) == 2) or result[1]:
        return
    now = time.monotonic() if now is None else now
    with _BRIDGE_MEMO_LOCK:
        entry = _BRIDGE_MEMO.setdefault(key, {"asked": now})
        entry.update(at=now, result=(list(result[0]), False))
        _BRIDGE_STATS["stored"] += 1
    _ensure_bridge_refresher()


def _bridge_refresh_once(now_fn=time.monotonic):
    """Re-read every remembered bridge lookup once, outside any search budget. GeckoTerminal is
    background traffic here (Gamma MED-1): funded=False, so a 429 books the SHARED cooldown only and
    never closes the entries' funded cell; it never waits on the gate (a busy gate skips it), and
    it is skipped while either cooldown runs or a multihop search is in flight."""
    from sniper.execution.geckoterminal_quote import cooldown_active, gt_get_json, last_get
    with _BRIDGE_MEMO_LOCK:
        items = list(_BRIDGE_MEMO.items())
        _BRIDGE_STATS["rounds"] += 1
    for key, entry in items:
        read, chain, token, pair, base = key
        if now_fn() - entry["asked"] > _BRIDGE_MEMO_IDLE_S:
            with _BRIDGE_MEMO_LOCK:
                if _BRIDGE_MEMO.get(key) is entry:
                    _BRIDGE_MEMO.pop(key)
                    _BRIDGE_STATS["dropped_idle"] += 1
            continue
        kwargs = {"pair_token": pair or None}
        if base is not None:
            kwargs["request_http"] = base
        gt = getattr(read, "__name__", "") == "_gt_pools_ranked"
        if gt:
            skip = ("gt_skipped_cooldown" if cooldown_active() or cooldown_active(funded=True)
                    else "gt_skipped_search" if _SEARCHES_IN_FLIGHT[0] > 0 else None)
            if skip is not None:
                with _BRIDGE_MEMO_LOCK:
                    _BRIDGE_STATS[skip] += 1
                continue
            kwargs["gt_http"] = lambda url, _base=base: gt_get_json(
                url, http=_base, gate_timeout_s=0.0, funded=False)
        elif getattr(read, "__name__", "") == "_ds_pools_ranked":
            kwargs["funded"] = False      # F6: a background failure books the SHARED cell only
        try:
            result = read(chain, token, **kwargs)
        except Exception:
            logger.warning("multihop bridge memo refresh failed read=%s chain=%s token=%s",
                           getattr(read, "__name__", read), chain, token, exc_info=True)
            with _BRIDGE_MEMO_LOCK:
                _BRIDGE_STATS["refresh_failed"] += 1
            continue
        outcome = last_get().get("outcome") if gt else None
        with _BRIDGE_MEMO_LOCK:
            if outcome == "gate_busy":
                _BRIDGE_STATS["gt_skipped_busy"] += 1
            elif outcome == "429":
                _BRIDGE_STATS["gt_429"] += 1
            if isinstance(result, tuple) and len(result) == 2 and not result[1]:
                if _BRIDGE_MEMO.get(key) is entry:
                    entry.update(at=now_fn(), result=(list(result[0]), False))
                    _BRIDGE_STATS["refreshed"] += 1
            elif outcome not in ("gate_busy", "429"):
                _BRIDGE_STATS["refresh_failed"] += 1


def _bridge_refresh_loop():
    from sniper.settings import memecoin as cfg
    while True:
        period = float(getattr(cfg, "MEMECOIN_EVM_MULTIHOP_BRIDGE_REFRESH_S", 0.0) or 0.0)
        time.sleep(period if period > 0 else 30.0)
        if period <= 0 or _bridge_memo_max_age_s() <= 0:
            continue
        try:
            _bridge_refresh_once()
        except Exception:
            logger.exception("multihop bridge memo refresh round failed")


def _ensure_bridge_refresher():
    with _BRIDGE_MEMO_LOCK:
        thread = _BRIDGE_REFRESHER[0]
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(target=_bridge_refresh_loop, name="evm-multihop-bridge-memo",
                                  daemon=True)
        _BRIDGE_REFRESHER[0] = thread
        thread.start()      # F10: inside the lock, so a concurrent first write sees it alive


def reset_bridge_memo():
    """Forget every remembered bridge lookup (tests; the refresher keeps running)."""
    with _BRIDGE_MEMO_LOCK:
        _BRIDGE_MEMO.clear()
        for name in _BRIDGE_STATS:
            _BRIDGE_STATS[name] = 0


def _read_noting_gate(read, args, kwargs):
    """(read(...), {gate_wait_ms, gt_outcome}) on the thread that ran it: a GeckoTerminal read's
    wait on the shared single-flight gate, for the rung's tap (Gamma MED-2). {} for any other read
    or one that never reached gt_get_json."""
    if getattr(read, "__name__", "") != "_gt_pools_ranked":
        return read(*args, **kwargs), {}
    from sniper.execution import geckoterminal_quote as gtq
    gtq._LAST_GET.value = None
    result = read(*args, **kwargs)
    got = gtq.last_get()
    return result, ({"gate_wait_ms": got.get("gate_wait_ms"), "gt_outcome": got.get("outcome")}
                    if got else {})


def _provider_pools(read, *args, budget=None, cache=None, memo=False, **kwargs):
    """Bound the provider wait; local cancellation is not provider failure. Timed for the ladder's
    provider HTTP tap from THIS (the rung's) thread; the read itself may run on a worker.
    `memo=True` (a bridge leg's coin-independent lookup) serves the bridge memo above."""
    from sniper.execution.evm_wallet import http_tap
    if kwargs.get("request_http") is None:
        kwargs.pop("request_http", None)
    cache_key = (read, str(args[0]), str(args[1]).lower(),
                 str(kwargs.get("pair_token") or "").lower(),
                 id(kwargs.get("ds_http")), id(kwargs.get("request_http"))) if cache is not None else None
    label = "v4_multihop:provider:%s" % getattr(read, "__name__", "read")
    memo_key = _bridge_memo_key(read, args, kwargs) if memo else None
    hit = _bridge_memo_get(memo_key)
    if hit is not None:
        with http_tap(label, memo=True) as tap:
            tap["outcome"] = "memo"
        return hit
    if budget is None:
        with http_tap(label) as tap:
            result, gate = _read_noting_gate(read, args, kwargs)
            tap.update(gate)
        _bridge_memo_put(memo_key, result)
        return result
    if budget.expired(discovery=True):
        return [], False
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="evm-multihop-provider")
    future = executor.submit(_read_noting_gate, read, args, kwargs)
    with http_tap(label) as tap:
        try:
            result, gate = future.result(timeout=budget.remaining_s(discovery=True))
            tap.update(gate)
            if cache is not None and not result[1] and not budget.expired(discovery=True):
                cache[cache_key] = result
            if not budget.expired(discovery=True):
                _bridge_memo_put(memo_key, result)
            return result
        except (TimeoutError, RequestNotSent):
            tap["outcome"] = "cut"
            budget.discovery_cut = True
            return [], False
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


def _key_within_budget(chain, pool_id, rpc_url, budget, **kw):
    """The pool key. Its RPC requests are counted by the search transport in `kw`."""
    if _budget_stops(budget):
        return None
    return _pool_key_for(chain, pool_id, rpc_url, **kw)


def _onchain_pair_options(chain, a, b, rpc_url, *, budget=None, hooked=False, **kw):
    """[(pool_id, key, None)] hookless fixed-fee pools joining a and b, most liquid first.

    `hooked=True` admits hook-priced and dynamic-fee pools too, for the MIDDLE leg only (see
    `_middle_leg_options`). The cash leg keeps the hookless fixed-fee guard.

    Depth is None because in-range liquidity is not a dollar figure; the caller's forward and
    reverse path quote at our clip, and the round-trip budget, are what admit the route. The most
    liquid pool is not always quotable: USDG/LULU 0x989154f2 had the most liquidity and returned no
    quote, and the next pool measured -4.7% on the full path.

    ONE READING PER POOL IS REMEMBERED, NOT ONE RANKING PER PAIR (2026-09-17). The store holds 114
    USDG/LULU pools. A search that the budget cuts keeps the readings it made, so the next search
    reads only the rest. Ranking uses the pools with a reading; an unread pool is not an option yet.
    """
    candidates = [
        (pid, key) for pid, key in _stored_pair_pools(chain, a, b)
        if (hooked or (str(key.get("hooks") or "").lower() == _ZERO_HOOKS
                       and key.get("fee") != DYNAMIC_FEE_FLAG))
        and _fee_ok(key.get("fee")) and _pair_of(key, a, b)[0]]
    now = time.monotonic()
    readings = {}
    to_read = []
    for pid, key in candidates:
        hit = _POOL_LIQUIDITY.get((str(chain), pid))
        if hit is not None and now - hit[0] < _PAIR_PICK_TTL_S:
            readings[pid] = hit[1]
        else:
            to_read.append((pid, key))
    # THE BURST IS CAPPED PER DECISION (2026-09-17, SHIT 0x685dfd38). A cold pair is up to 114
    # `extsload` reads; run at eight at a time on the funded 4663 pool they 429'd the v2 rung on the
    # next coin. The readings cache per pool, so a capped scan still finishes across decisions.
    # ONE CALL PER CHUNK, NOT PER POOL (2026-09-28). Capped per pool, a cold search starved: coin
    # 0x8920d999 needed 96 readings over three pairs, the first bridge spent all 32, the pair with
    # the coin's deep pool was never read, and the route quoted -46.4% against -2.7% on the
    # provider path. The cap bounds REQUESTS on the endpoint, so a chunk of up to
    # _LIQUIDITY_BATCH_SLOTS pools is one extsload(bytes32[]) call and takes ONE from the allowance.
    # Cold on 8 recorded 4663 coins: within 0.24 pp of the provider route on every coin, in 3-4
    # calls. Batch values equal single reads on 96/96 pools.
    _reads_cap, workers = _liquidity_read_limits()
    chunks = [to_read[i:i + _LIQUIDITY_BATCH_SLOTS]
              for i in range(0, len(to_read), _LIQUIDITY_BATCH_SLOTS)]
    chunks = chunks[:_liquidity_reads_allowed(budget, len(chunks))]
    if chunks and not _budget_stops(budget):
        # NOT a context manager: `with` joins every queued call. The wait stops at the discovery
        # deadline exactly as it did for single reads, so a slow batch cuts the search and never
        # extends the rung; queued calls are cancelled and a call the transport refuses is never
        # sent.
        executor = ThreadPoolExecutor(max_workers=min(workers, len(chunks)),
                                      thread_name_prefix="evm-multihop-liquidity")
        futures = [executor.submit(_onchain_liquidities, chain, [pid for pid, _key in chunk],
                                   rpc_url, **kw) for chunk in chunks]
        from sniper.execution.evm_wallet import http_tap
        try:
            # The rung record gets the batch's wall time and size, so custody-RPC latency is
            # measured on organic ladders.
            with http_tap("v4_multihop:liquidity_batch", slots=sum(len(c) for c in chunks),
                          calls=len(chunks)) as box:
                wait(futures, timeout=(None if budget is None
                                       else budget.remaining_s(discovery=True)))
                if not all(f.done() and not f.cancelled() for f in futures):
                    box["outcome"] = "cut"
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        read_at = time.monotonic()
        if budget is not None and not all(f.done() and not f.cancelled() for f in futures):
            budget.discovery_cut = True
        for future in futures:
            if not future.done() or future.cancelled():
                continue
            try:
                got = future.result()
            except Exception:
                got = None
            for pid, liq in (got or {}).items():
                if liq is not None:        # an unreadable chain is not remembered
                    _POOL_LIQUIDITY[(str(chain), pid)] = (read_at, liq)
                    readings[pid] = liq
    return [(pid, key, None) for _liq, pid, key in sorted(
        ((readings[pid], pid, key) for pid, key in candidates if readings.get(pid)),
        key=lambda x: -x[0])]


def _onchain_pair_pick(chain, a, b, rpc_url, **kw):
    """(pool_id, key, None) for the most-liquid hookless fixed-fee pool joining a and b, or None."""
    ranked = _onchain_pair_options(chain, a, b, rpc_url, **kw)
    return ranked[0] if ranked else None


def _bridge_options(chain, cash, bridge, rpc_url, *, limit, budget=None, providers=True, **kw):
    """Up to `limit` FIXED-FEE pools joining `cash` and `bridge`, or [].

    FIXED FEE BEATS DEPTH, and that ordering is the guard. A dynamic-fee pool can be far deeper —
    99M against 2.5M in the live data — but a hook prices every swap through it, so the cost of
    the bridge leg becomes unbounded and unmeasured. The bridge carries EVERY entry and EVERY
    exit, so an unbounded cost there is charged on all volume, twice per position. A deeper pool
    is not a reason to accept an unmeasurable price.

    DEPTH ORDERS THE OPTIONS; IT DOES NOT CHOOSE THE ROUTE (2026-09-17). The deepest ETH/LULU pool
    charged a 5.01% fee. The caller measures every option on the complete path.
    """
    def eligible(pools, want):
        out = []
        for pid, depth in pools:
            if len(out) >= want or _budget_stops(budget):
                break
            # UNKNOWN DEPTH FIRST, before the key read (2026-09-12). An unmeasured pool is never a
            # bridge (below), and USDG's unmeasured list is every coin paired with it: 65,492
            # cached key reads took 122 s per decision, the 8 s ladder deadline cut the rung, and
            # the 2-leg WETH -> NVDA -> coin route found in 0.2 s was never measured.
            if depth is None:
                continue
            key = _key_within_budget(chain, pid, rpc_url, budget, pair=(cash, bridge), **kw)
            if not key or not _fee_ok(key.get("fee")):
                continue
            if key.get("fee") == DYNAMIC_FEE_FLAG:
                continue                   # hook-priced: unbounded cost on the leg that carries all volume
            ok, _ = _pair_of(key, cash, bridge)
            if not ok:
                continue
            out.append((pid, key, depth))   # the provider list is deepest first
        return out

    memo_key = (str(chain), str(cash).lower(), str(bridge).lower())
    selected = []
    # `providers=False` is the store-first search: no provider read here, the store pair below.
    direct = _direct_bridge_pools(chain, cash, bridge, memo=True, **kw) if providers else None
    if direct is not None:
        selected = eligible(direct, limit)
    if providers and not selected and not _budget_stops(budget):
        # MEASURED DEPTH ONLY: eligible() drops unknown depth, so the store's pools are not read.
        selected = eligible(_pools_with_depth(
            chain, bridge, include_store=False, pair_token=cash, memo=True, **kw), limit)
    if len(selected) < limit and not _budget_stops(budget):
        # The providers can list only the deepest pools of a pair. Our own store holds every pool,
        # so it fills the remaining options with pools the providers did not return.
        seen = {pid for pid, _key, _depth in selected}
        for option in _onchain_pair_options(chain, cash, bridge, rpc_url, budget=budget, **kw):
            if len(selected) >= limit:
                break
            if option[0] not in seen:
                selected.append(option)
    if selected:
        _BRIDGE_LAST_GOOD[memo_key] = (time.monotonic(), selected[0])
        return selected
    # LAST GOOD BRIDGE WHEN DISCOVERY COMES BACK EMPTY (2026-09-12). The ETH/USDG bridge is found
    # only through GeckoTerminal: DexScreener lists every USDG pair with USDG as the QUOTE, and the
    # discovery keeps base == USDG, so it never returns ETH/USDG. When the shared GeckoTerminal
    # guard is spent the bridge reads "missing": ANIME (fxJPY-paired, 08:10Z) refused
    # no_route_all_venues six times while the same search off the runner routed at -3.17%.
    # The remembered pool is re-read and re-screened here, and the caller still requires the full
    # forward AND reverse path to quote before it returns a route.
    hit = _BRIDGE_LAST_GOOD.get(memo_key)
    if hit is None or time.monotonic() - hit[0] > _BRIDGE_LAST_GOOD_TTL_S:
        return []
    pid, _old_key, depth = hit[1]
    key = _key_within_budget(chain, pid, rpc_url, budget, pair=(cash, bridge), **kw)
    if (not key or not _fee_ok(key.get("fee")) or key.get("fee") == DYNAMIC_FEE_FLAG
            or not _pair_of(key, cash, bridge)[0]):
        return []
    return [(pid, key, depth)]


def _select_bridge(chain, cash, bridge, rpc_url, **kw):
    """Deepest FIXED-FEE pool joining `cash` and `bridge`, or None. The exit search uses this."""
    options = _bridge_options(chain, cash, bridge, rpc_url, limit=1, **kw)
    return options[0] if options else None


def _allow_unmeasured_depth():
    """Whether a pool with no provider depth may be carried to the round-trip quote test.

    Default ON. The gate it replaces was not measuring depth, it was measuring whether a third
    party had got round to listing the coin, and on the coins this system copies the answer at
    decision time is usually no.
    """
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_EVM_MULTIHOP_ALLOW_UNMEASURED", True))


def _coin_leg_options(chain, bridge, mint, rpc_url, min_depth_usd, *, limit, pools=None,
                      budget=None, **kw):
    """Up to `limit` sane pools joining `bridge` and `mint` that clear the depth floor, or [].

    Fails CLOSED on unknown depth: absence of a disqualifying signal is not a qualifying one.
    Today's proof is that `_fee_is_sane` admitted a pool that could not be quoted at any size.
    """
    out = []
    for pid, depth in (pools if pools is not None else _pools_with_depth(chain, mint, **kw)):
        if len(out) >= limit or _budget_stops(budget):
            break
        key = _key_within_budget(chain, pid, rpc_url, budget, pair=(bridge, mint), **kw)
        if not key or not _fee_ok(key.get("fee")):
            continue
        ok, _ = _pair_of(key, bridge, mint)
        if not ok:
            continue
        if depth is None:
            # UNKNOWN DEPTH IS NOT A FREE PASS, it is a deferral to a better measurement. The
            # provider's reserve is a second-hand number about the pool; a round-trip quote at OUR
            # clip is a first-hand answer about OUR trade, and `multihop_quote` already requires all
            # four legs -- in AND back out -- to quote before it returns a route. So an unmeasured
            # pool is carried to that test instead of being dropped before it, and it is only ever
            # reached after every pool with a real depth has been tried and rejected.
            #
            # This is why the fee screen still runs above: `_fee_is_sane` once admitted a pool that
            # could not be quoted at any size, which is exactly the trap the quote catches.
            if not _allow_unmeasured_depth():
                continue
            out.append((pid, key, None))
            continue
        if depth < min_depth_usd:
            continue
        out.append((pid, key, depth))       # the provider list is deepest first
    return out


def _select_coin_leg(chain, bridge, mint, rpc_url, min_depth_usd, *, pools=None, **kw):
    """Deepest sane pool joining `bridge` and `mint` that clears the depth floor, or None."""
    options = _coin_leg_options(
        chain, bridge, mint, rpc_url, min_depth_usd, limit=1, pools=pools, **kw)
    return options[0] if options else None


def _middle_leg_options(chain, first, second, rpc_url, *, limit, budget=None, providers=True,
                        **kw):
    """Up to `limit` sane pools joining `first` and `second`, or []. A hook-priced pool is allowed.

    THE DYNAMIC-FEE EXCLUSION DOES NOT APPLY HERE, and the reason is the measurement, not the
    pool. `_bridge_options` refuses a hook-priced pool because it carries EVERY entry and EVERY
    exit, so an unbounded price there is charged on all volume twice per position. This middle
    leg is quoted forward and back on every decision, and re-quoted at exit, so its cost is a
    measured number rather than an unbounded one. The rule stays on the cash leg above.
    """
    # THE CHAIN FIRST (2026-09-12). The provider path below walks every pool of `second` -- for a
    # stock token that is every coin paired with it -- reading keys to find the one pair. Our own
    # store answers "which pools join these two" in one query.
    # HOOK-PRICED POOLS INCLUDED (2026-09-28). The chain read was hookless-only, so a middle leg the
    # chain answered never saw a hook-priced pool; the providers found one only when the capped read
    # starved. 0x250f720f routed at -3.47% through a dynamic-fee USDG/0xc72b96 pool, and fell to
    # -5.23% once batched reads always answered. This leg is quoted both ways, so a hook is measured.
    picked = _onchain_pair_options(chain, first, second, rpc_url, budget=budget, hooked=True, **kw)
    if picked:
        return picked[:limit]
    out = []
    if _budget_stops(budget) or not providers:
        return out
    for pid, depth in _pools_with_depth(chain, second, pair_token=first, memo=True, **kw):
        if len(out) >= limit or _budget_stops(budget):
            break
        key = _key_within_budget(chain, pid, rpc_url, budget, pair=(first, second), **kw)
        if not key or not _fee_ok(key.get("fee")):
            continue
        ok, _ = _pair_of(key, first, second)
        if not ok:
            continue
        out.append((pid, key, depth))       # the provider list is deepest first
    return out


def _select_middle_leg(chain, first, second, rpc_url, **kw):
    """Deepest sane pool joining `first` and `second`, or None."""
    options = _middle_leg_options(chain, first, second, rpc_url, limit=1, **kw)
    return options[0] if options else None


def _candidate_bridges(chain, cash, mint, rpc_url, configured, *, pools=None, **kw):
    """Return the configured bridges, or the coin pool counter-currencies."""
    if configured:
        yield from configured
        return
    bridges = set()
    for pool_id, _depth in (
            pools if pools is not None else _pools_with_depth(chain, mint, **kw)):
        key = _pool_key_for(chain, pool_id, rpc_url, **kw)
        if not key or not _fee_ok(key.get("fee")):
            continue
        currencies = (str(key.get("currency0") or ""), str(key.get("currency1") or ""))
        if str(mint).lower() not in {value.lower() for value in currencies}:
            continue
        bridge = currencies[1] if currencies[0].lower() == str(mint).lower() else currencies[0]
        if bridge.lower() in {str(cash).lower(), str(mint).lower()}:
            continue
        if bridge.lower() not in bridges:
            bridges.add(bridge.lower())
            yield bridge


# Fractions tried, largest first, ONLY when the whole bag fails to quote. Coarse on purpose: each
# rung costs one eth_call on a path that is already failing, and the goal is to get MOST of the
# position out, not to find the exact maximum. LARP 50682's ceiling measured about 84.4%.
_EXIT_SIZE_LADDER = (0.9, 0.75, 0.5, 0.25)


def round_trip_budget_pct(tp_pct, *, cfg=None, chain=None):
    """The admission budget `round_trip_within_budget` compares against (percent), or None when the
    gate is disabled. One definition, so the EVM resize targets the very number that refused."""
    if cfg is None:
        from sniper.settings import memecoin as cfg
    frac = float(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_TP_FRAC", 0.0) or 0.0)
    hard = float(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_PCT", 0.0) or 0.0)
    per_chain = getattr(cfg, "evm_round_trip_tp_frac_for", None)
    if chain is not None and per_chain is not None:
        override = per_chain(chain)
        if override is not None:
            frac = float(override)
    if frac <= 0 and hard <= 0:
        return None
    try:
        tp = float(tp_pct or 0.0)
    except (TypeError, ValueError):
        tp = 0.0
    budget = tp * frac if (tp > 0 and frac > 0) else hard
    return budget if budget > 0 else None


def round_trip_within_budget(round_trip_pct, tp_pct, *, cfg=None, chain=None, margin_pct=0.0):
    """True when a route's MEASURED round-trip cost leaves room for the take-profit it aims at.

    `round_trip_pct` is what a clip loses buying in and selling straight back out, negative for a
    loss, and `multihop_quote` has always computed it from all four legs. Nothing read it: depth
    stood in as a proxy, and on this chain depth is wrong in BOTH directions -- it admits a deep
    pool charging 90% and rejects a shallow honest one.

    THE CEILING IS RELATIVE TO THE POSITION'S OWN TAKE-PROFIT, because that is the comparison that
    makes a number a refusal. FRIES (0x798c78cf) quotes -24.8% against a 25% TP: the exit it is
    aiming at does not even cover getting in and out. A global percentage cannot express that -- the
    same -24.8% is fatal against a 25% TP and merely expensive against a 200% one.

    Fail-closed: an unreadable round trip is not a cheap one. A missing TP falls back to the
    absolute ceiling rather than passing everything, which is what a bare TP-relative test would do
    whenever the take-profit is 0.
    """
    if cfg is None:
        from sniper.settings import memecoin as cfg
    frac = float(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_TP_FRAC", 0.0) or 0.0)
    hard = float(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_PCT", 0.0) or 0.0)
    # PER CHAIN (2026-09-13, operator, PONZI 7ae4df5b). See evm_round_trip_tp_frac_for.
    per_chain = getattr(cfg, "evm_round_trip_tp_frac_for", None)
    if chain is not None and per_chain is not None:
        override = per_chain(chain)
        if override is not None:
            frac = float(override)
    if frac <= 0 and hard <= 0:
        return True                        # gate disabled -> depth-only screening, as before
    try:
        cost = abs(float(round_trip_pct))
    except (TypeError, ValueError):
        return False                       # unreadable cost is not a cheap one
    import math
    if not math.isfinite(cost):
        return False
    try:
        tp = float(tp_pct or 0.0)
    except (TypeError, ValueError):
        tp = 0.0
    budget = tp * frac if (tp > 0 and frac > 0) else hard
    if budget <= 0:
        return True
    # A MARGIN ONLY FOR THE RE-ASK (2026-09-17). `margin_pct` is a RELATIVE widening of the budget
    # and is 0 for the admission decision. SKIBIDI 0x38931deb priced -15.09% against the 15% budget
    # and -13.35% on the same route 13 minutes later; LuluDior read -15.40% and later -13.8%. The
    # cost moves with the pool, so one read decides a coin. OP829's -11.79% against a 10% budget
    # (17.9% over) stays outside a 10% margin and keeps its one-shot refusal.
    if margin_pct:
        budget = budget * (1.0 + max(0.0, float(margin_pct)) / 100.0)
    return cost <= budget


_refusal = threading.local()


def _note(reason):
    """Record why the CURRENT thread's multihop decision refused (None = it routed)."""
    _refusal.reason = reason


def last_multihop_refusal():
    """The branch the last `multihop_quote`/`dex_v4_multihop_quote` on this thread refused on, or
    None. Instrumentation only (2026-09-05): 46 of 46 USDG-reachable `no_route` refusals over three
    days carried a v4_multihop rung with no reason, so nothing could say whether depth, budget, the
    bridge or a leg quote was the gate. Per-thread because the ladder runs its rungs in parallel."""
    return getattr(_refusal, "reason", None)


_SEARCHES_IN_FLIGHT = [0]               # multihop searches running now (the refresher waits them out)


def multihop_quote(*args, **kwargs):
    """The route search (`_multihop_quote_search`), counted while it runs so the bridge-memo
    refresher never starts a GeckoTerminal read into a search (Gamma MED-2, 2026-10-17)."""
    with _BRIDGE_MEMO_LOCK:
        _SEARCHES_IN_FLIGHT[0] += 1
    try:
        return _multihop_quote_search(*args, **kwargs)
    finally:
        with _BRIDGE_MEMO_LOCK:
            _SEARCHES_IN_FLIGHT[0] -= 1


def last_pinned_refusal():
    """Why the last call's pinned-bridge attempt on this thread did not decide, or None (it decided
    or did not run). The bridge cache drops an entry only on an answered miss, never a blind one."""
    return getattr(_refusal, "pinned", None)


def _multihop_quote_search(chain, cash_token, mint, amount_atomic, *, rpc_url,
                   bridge_currencies=(), clip_usd=None, min_depth_clips=None,
                   http=None, gt_http=None, store_only=False, limits=None, metadata_http=None,
                   coin_pool_hint=None, bridge_hint=None, pinned_accept_fn=None,
                   prefix_hint=None, pinned_only=False):
    """Quote cash -> bridge -> mint AND the full reverse, or None (fail-closed).

    Returns `{amount_out, round_trip_pct, bridge, bridge_pool_id, coin_pool_id, coin_depth_usd,
    legs}`. ALL FOUR LEGS are quoted before a route is returned: the exit is two legs and both
    must be proven before an entry, because buyable-and-not-sellable is the one thing we never
    accept. A buy quote is not evidence of a sell quote.

    `store_only` and `limits` are for a caller that is NOT the funded rung (the exact capture
    lane, 2026-09-30). `store_only=True` searches our PoolKey store alone and never asks a
    provider, whatever MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST says. `limits` replaces
    `_search_limits()` for a caller behind its own rate bucket. Both default to the funded
    behaviour, unchanged.

    `coin_pool_hint` (the source-route rung, 2026-10-15): the coin pool the source itself bought
    through. The search then runs on that pool ALONE, with no provider pass; the bridge comes from
    the store or the chain exactly as in the store pass. No route there is None, and today's
    multihop rung, running beside it, decides.

    `bridge_hint` (the bridge cache, 2026-10-15, operator "fill fastest"): `(bridge, pool_id, key,
    depth)`, the cash->bridge pool that last won for that bridge currency. With a coin pool hint,
    one pinned path (that pool, then the coin pool) is measured first; the coin leg keeps its depth
    floor and `pinned_accept_fn(round_trip_pct)` is the caller's budget. A key that does not join
    cash and `bridge`, a dynamic or insane fee, no quote or a refused budget falls through to the
    full bridge search inside the same limits (`last_pinned_refusal` names why).

    `prefix_hint` (the full-prefix cache, MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS): `(x, prefix, pool_id,
    depth)`, the cached route cash -> ... -> x as ((currency, key), ...), 1 or 2 legs. The pinned
    attempt then measures exactly prefix + the coin leg, under the same screens as the search that
    found it and the same `pinned_accept_fn`. `pinned_only` returns after that one attempt: the
    v4_multihop_pinned rung runs it beside the discovery, never in front of it.
    """
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import http_tap
    _refusal.pinned = None
    if min_depth_clips is None:
        min_depth_clips = float(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MIN_DEPTH_CLIPS", 100.0))
    _note("no_clip_size")
    if clip_usd is None or float(clip_usd) <= 0:
        return None                        # cannot evaluate the floor without a clip: fail closed
    min_depth_usd = float(clip_usd) * float(min_depth_clips)
    amount_atomic = int(amount_atomic)
    _note("no_amount")
    if amount_atomic <= 0:
        return None
    _note("no_bridge_candidates")
    # THE LIMITS START BEFORE DISCOVERY and cover every outbound request of this search. The
    # reserve covers every path's two quotes and the block read.
    per_step, max_paths, deadline_s, max_requests, quote_time_s = limits or _search_limits()
    budget = _SearchBudget(deadline_s, max_requests, quote_reserve=2 * max_paths + 1,
                           quote_time_s=quote_time_s)
    from sniper.execution.evm_wallet import _http as _default_rpc_http
    base_http = http if http is not None else _default_rpc_http
    kw = {"http": _BudgetedHttp(budget, base_http, discovery=True)}
    quote_kw = {"http": _BudgetedHttp(budget, base_http, discovery=False)}
    from sniper.execution.geckoterminal_quote import _http as gt_request_http
    from sniper.execution.solana_quote import _http as ds_request_http
    kw.update(provider_budget=budget,
              gt_request_http=_BudgetedHttp(budget, gt_request_http, discovery=True),
              ds_request_http=_BudgetedHttp(budget, ds_request_http, discovery=True))
    if callable(metadata_http):
        from sniper.execution.geckoterminal_quote import _UA as gt_ua
        from sniper.execution.solana_quote import _UA as ds_ua
        kw.update(gt_request_http=_metadata_http(metadata_http, budget, gt_ua),
                  ds_request_http=_metadata_http(metadata_http, budget, ds_ua), ds_pool_cache={})
    if gt_http is not None:
        kw["gt_http"] = _BudgetedHttp(budget, gt_http, discovery=True)

    # STORE FIRST (2026-09-27, Alpha GO). Our PoolKey store knows a copied coin's pools within
    # seconds of their Initialize event; the providers need minutes to list them and cost 1-3 s to
    # ask. When the store holds the coin, the search runs on the store alone (no GeckoTerminal or
    # DexScreener read, for the coin or its bridges). No route there -- no pair, every quote
    # reverted, the budget cut -- falls back to the provider search, inside the same budget.
    # Replayed on the 7 V4-routed 4663 fills (tests/fixtures/memecoin/
    # v4_multihop_store_first_4663.json): the executed pool on 6 of 7 in 0.44-0.85 s.
    attempts = []
    if coin_pool_hint is not None:
        source_pools = [(str(coin_pool_hint).lower(), None)]
        if bridge_hint is not None or prefix_hint is not None:
            attempts.append(("pinned", source_pools))
        if not pinned_only:
            attempts.append(("source", source_pools))
    elif store_only or bool(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST", True)):
        from sniper.execution.evm_v4_quote import _stored_pools_for_mint
        stored = [(pid, None) for pid in _stored_pools_for_mint(chain, mint)]
        if stored:
            attempts.append(("store", stored))
        elif store_only:
            _note("no_stored_pool")
    if not store_only and coin_pool_hint is None:
        attempts.append(("provider", None))
    # WHY THE STORE PASS DID NOT DECIDE (2026-10-07, Alpha): 0 of 10 multihop hits on 4663 since 10-01
    # came from the store, and the trace said only `discovery:provider`. The store pass's own
    # refusal rides on a provider-decided route, and on the ladder trace.
    store_refusal = None
    pinned_refusal = None
    for discovery, coin_pools in attempts:
        providers = discovery == "provider"
        pinned = discovery == "pinned"
        if providers:
            coin_pools = _pools_with_depth(chain, mint, **kw)
        max_legs = max(2, int(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS", 2) or 2))
        second_bridges = tuple(
            getattr(cfg, "MEMECOIN_EVM_MULTIHOP_SECOND_BRIDGES", {}).get(str(chain), ()) or ())
        use_prefix = pinned and prefix_hint is not None
        if use_prefix or pinned:
            bridges = () if use_prefix else (bridge_hint[0],)
        else:
            # STEP MARKS (2026-10-16, Alpha): one http-tap record per search step, so a late first
            # call on the rung trace (v4_multihop_source p50 263 ms) names its step. Timing only.
            with http_tap("v4_multihop:candidate_bridges", discovery=discovery):
                bridges = _candidate_bridges(
                    chain, cash_token, mint, rpc_url, bridge_currencies, pools=coin_pools, **kw)

        # ENUMERATE A BOUNDED SET OF COMPLETE PATHS (2026-09-17). Every bridge currency is searched,
        # and each step offers up to `per_step` pools, so a pool that does not quote no longer hides
        # the pools behind it. LuluDior: the 5.01% ETH/LULU pool measured -13.8% while a 0.50% pool
        # measured -6.2% and ETH -> USDG -> LULU -> coin measured -4.7% on the same chain.
        paths = []                              # (order, legs_path, meta)
        saw_bridge = False
        for bridge in bridges:
            if budget.expired(discovery=True):
                break
            saw_bridge = True
            # THE CASH LEG IS TESTED FIRST, which is the original order and the one the refusal tape
            # reads: when both the bridge pool and the coin leg are missing, the bridge is the reason
            # reported. A three-leg candidate reaches the cash currency through a DIFFERENT pool, so it
            # is still searched when this one is unusable.
            if pinned:
                # THE CACHED POOL PASSES THE SAME KEY SCREEN as `_bridge_options`: it joins cash and
                # this bridge, its fee is sane and fixed. The coin leg below keeps its depth floor.
                _b_bridge, b_pid, b_key, b_depth = bridge_hint
                br_options = ([(b_pid, b_key, b_depth)] if (
                    _fee_ok(b_key.get("fee"))
                    and b_key.get("fee") != DYNAMIC_FEE_FLAG
                    and _pair_of(b_key, cash_token, bridge)[0]) else [])
            else:
                with http_tap("v4_multihop:bridge_options", discovery=discovery,
                              bridge=str(bridge).lower()):
                    br_options = _bridge_options(
                        chain, cash_token, bridge, rpc_url, limit=per_step, budget=budget,
                        providers=providers, **kw)
            if not br_options:
                _note("bridge_pool_unusable:%s" % str(bridge)[:10])
                if max_legs < 3 or pinned:
                    continue
            with http_tap("v4_multihop:coin_leg_options", discovery=discovery,
                          bridge=str(bridge).lower()):
                coin_options = _coin_leg_options(
                    chain, bridge, mint, rpc_url, min_depth_usd, limit=per_step, pools=coin_pools,
                    budget=budget, **kw)
            if not coin_options:
                _note("coin_leg_missing_or_under_floor:min_depth_usd=%d" % int(min_depth_usd))
                continue

            for c_pid, c_key, c_depth in coin_options:
                coin_meta = {"coin_pool_id": c_pid, "coin_depth_usd": c_depth, "coin_key": c_key}
                # CANDIDATE A, two legs: cash -> bridge -> coin. The cash leg stays fixed-fee.
                for b_pid, b_key, b_depth in br_options:
                    paths.append((
                        [{"currency": bridge, "key": b_key}, {"currency": mint, "key": c_key}],
                        dict(coin_meta, bridge=bridge, bridge_key=b_key, bridge_pool_id=b_pid,
                             bridge_depth_usd=b_depth)))

            # CANDIDATE B, three legs: cash -> middle -> bridge -> coin. Searched only when the
            # operator has raised the cap, so the default configuration never builds one.
            if max_legs >= 3 and not pinned:
                for middle in second_bridges:
                    if str(middle).lower() in {str(cash_token).lower(), str(bridge).lower(),
                                               str(mint).lower()}:
                        continue
                    mb_options = _bridge_options(
                        chain, cash_token, middle, rpc_url, limit=per_step, budget=budget,
                        providers=providers, **kw)
                    if not mb_options:
                        _note("second_bridge_pool_missing:%s" % str(middle)[:10])
                        continue
                    mid_options = _middle_leg_options(
                        chain, middle, bridge, rpc_url, limit=per_step, budget=budget,
                        providers=providers, **kw)
                    if not mid_options:
                        _note("middle_leg_missing:%s" % str(bridge)[:10])
                        continue
                    for c_pid, c_key, c_depth in coin_options:
                        for m_pid, m_key, m_depth in mb_options:
                            for _mid_pid, mid_key, _mid_depth in mid_options:
                                paths.append((
                                    [{"currency": middle, "key": m_key},
                                     {"currency": bridge, "key": mid_key},
                                     {"currency": mint, "key": c_key}],
                                    {"bridge": middle, "bridge_key": m_key, "bridge_pool_id": m_pid,
                                     "bridge_depth_usd": m_depth, "coin_pool_id": c_pid,
                                     "coin_depth_usd": c_depth, "coin_key": c_key}))

        if use_prefix and len(prefix_hint[1]) + 1 > max_legs:
            _note("prefix_over_max_legs:%d" % max_legs)    # MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS lowered
        elif use_prefix:
            saw_bridge = True
            paths = _prefix_paths(chain, cash_token, mint, rpc_url, prefix_hint, coin_pools,
                                  min_depth_usd, limit=per_step, budget=budget, **kw)

        # THE STATIC FEE ORDERS WHICH PATHS ARE MEASURED FIRST; THE MEASUREMENT CHOOSES. A hook-priced
        # leg adds nothing to the order because its fee is not known before the quote. The sort is
        # stable, so equal fees keep the discovery order.
        def _static_fee(item):
            return sum(int(leg["key"].get("fee") or 0) for leg in item[0]
                       if leg["key"].get("fee") != DYNAMIC_FEE_FLAG)
        considered = len(paths)
        ordered = sorted(paths, key=_static_fee)

        best = None                             # (ratio, out, back, legs_path, meta)
        measured_n = 0
        block = None
        # THE NEXT SLICE WHEN A WHOLE SLICE FAILS (2026-09-28). With every pair read in one batched
        # call, coin 0x941cf3f6 got 12 paths at static fee 0-100 through the WETH/0x4fc59c pair (never
        # read under the old 32-read cap) whose hooked coin-side pools do not quote, and the stable
        # sort cut the working 0xc0d6 path at max_paths: no route, with 73 requests and 2.75 s left.
        # A slice in which no path is measured is followed by the next one while the budget allows;
        # a slice with any measured path ends the search, so the best measured round trip still wins.
        for start in range(0, len(ordered), max_paths):
            if best is not None or (start and (budget.stop_reason or budget.expired())):
                break
            paths = ordered[start:start + max_paths]
            if block is None:
                # NO FIXED BLOCK, NO COMPARISON. Quotes at `latest` can each read a different block,
                # which mixes a market move into the route-cost difference, even between a buy and
                # its reverse.
                block = _decision_block(chain, rpc_url, **quote_kw)
                if block is None:
                    _note("decision_block_unavailable")
                    return None
            results = _measure_paths(chain, cash_token, mint, amount_atomic, rpc_url, paths,
                                     block, budget, **quote_kw)
            for (legs_path, meta), result in zip(paths, results):
                if result is None:
                    _note("search_stopped:%s" % (budget.stop_reason or "deadline"))
                    continue
                out, back_cash, reason = result
                if reason is not None:
                    _note(reason)
                    continue
                measured_n += 1
                ratio = float(back_cash) / float(amount_atomic)
                # BEST MEASURED ROUND TRIP WINS, never the first that quotes. A three-leg route is
                # admitted only by beating the two-leg one on the number the caller budgets against.
                if best is None or ratio > best[0]:
                    best = (ratio, out, back_cash, legs_path, meta)

        if best is None:
            # DO NOT OVERWRITE A SPECIFIC REASON. `_note` is last-write-wins and each gate PRE-SETS the
            # reason it would bail with, so naming the terminal gate unconditionally would erase the
            # bridge, coin-leg or path reason that actually refused the coin. This fires only when a
            # bridge WAS examined and nothing more specific was recorded; with no bridge at all the
            # pre-set `no_bridge_candidates` is already the true reason and is left alone.
            if saw_bridge and last_multihop_refusal() == "no_bridge_candidates":
                _note("no_candidate_within_legs:%d" % max_legs)
            # A CUT SEARCH IS NOT A MISSING ROUTE. When discovery ran out of time or requests, the
            # bridge or coin-leg reason above only says what the search reached, so the refusal names
            # the limit instead of reading as "no route".
            if budget.discovery_cut and not str(last_multihop_refusal() or "").startswith(
                    "search_stopped"):
                _note("search_stopped:discovery_limit")
            if discovery == "store":
                store_refusal = last_multihop_refusal()
            elif pinned:
                pinned_refusal = _refusal.pinned = last_multihop_refusal()
            continue
        ratio, out, back_cash, legs_path, meta = best
        if pinned and pinned_accept_fn is not None and not pinned_accept_fn(100.0 * (ratio - 1.0)):
            _note("pinned_over_budget:%.2f" % (100.0 * (ratio - 1.0)))
            pinned_refusal = _refusal.pinned = last_multihop_refusal()
            continue
        _note(None)
        return {
            "amount_out": out,
            # the tx builder needs the KEYS, not just the ids -- it encodes fee, tickSpacing and
            # hooks per hop into the PathKey[]. Returning ids alone would force a second lookup,
            # which is the mismatched-pair shape that made this module refuse everything once.
            "bridge_key": meta["bridge_key"],
            "coin_key": meta["coin_key"],
            "round_trip_pct": 100.0 * (ratio - 1.0),
            "bridge": meta["bridge"],
            "bridge_pool_id": meta["bridge_pool_id"],
            "bridge_depth_usd": meta["bridge_depth_usd"],
            "bridge_fee": meta["bridge_key"].get("fee"),
            "coin_pool_id": meta["coin_pool_id"],
            "coin_depth_usd": meta["coin_depth_usd"],
            "coin_fee": meta["coin_key"].get("fee"),
            "min_depth_usd": min_depth_usd,
            # `legs` COUNTS THE QUOTE CALLS and keeps its old meaning; `legs_path` is the ordered
            # route from cash to coin that the builders encode.
            "legs": 2 * len(legs_path),
            "legs_path": legs_path,
            # SEARCH EVIDENCE: the block every compared path was quoted at, and what the search spent.
            "quote_block": block,
            "paths_considered": considered,
            "paths_measured": measured_n,
            "search_requests": budget.used,
            "search_stop": budget.stop_reason,
            "search_discovery_cut": budget.discovery_cut,
            "executable": False,
            # WHICH DISCOVERY DECIDED (store or provider), carried to the ladder trace.
            "discovery": discovery,
            "store_refusal": store_refusal,
            "pinned_refusal": pinned_refusal,
        }
    return None


def _prefix_paths(chain, cash, mint, rpc_url, prefix_hint, coin_pools, min_depth_usd, *, limit,
                  budget=None, **kw):
    """[(legs_path, meta)] for a cached prefix plus each usable coin leg, or [] with the reason noted.

    THE SCREENS OF THE SEARCH THAT FOUND IT, re-applied to the cached keys: the first leg joins cash
    and its currency at a sane, FIXED fee (the `_bridge_options` rule: it carries every entry and
    exit); a second leg joins the first's currency and x at a sane fee, hooked or not (the
    `_middle_leg_options` rule); the coin leg goes through `_coin_leg_options` and its depth floor.
    """
    x, prefix, pid0, depth0 = prefix_hint
    if not prefix or len(prefix) > 2 or str(prefix[-1][0]).lower() != str(x).lower():
        _note("prefix_shape")
        return []
    prev = cash
    for i, (cur, key) in enumerate(prefix):
        if (not isinstance(key, dict) or not _fee_ok(key.get("fee"))
                or (i == 0 and key.get("fee") == DYNAMIC_FEE_FLAG) or not _pair_of(key, prev, cur)[0]):
            _note("bridge_pool_unusable:%s" % str(cur)[:10])
            return []
        prev = cur
    coin_options = _coin_leg_options(chain, x, mint, rpc_url, min_depth_usd, limit=limit,
                                     pools=coin_pools, budget=budget, **kw)
    if not coin_options:
        _note("coin_leg_missing_or_under_floor:min_depth_usd=%d" % int(min_depth_usd))
        return []
    head = [{"currency": cur, "key": key} for cur, key in prefix]
    return [(head + [{"currency": mint, "key": c_key}],
             {"bridge": prefix[0][0], "bridge_key": prefix[0][1], "bridge_pool_id": pid0,
              "bridge_depth_usd": depth0, "coin_pool_id": c_pid, "coin_depth_usd": c_depth,
              "coin_key": c_key})
            for c_pid, c_key, c_depth in coin_options]


def _decision_block(chain, rpc_url, **kw):
    """One block number for every path in a search, as hex, or None when it cannot be fixed.

    Paths quoted at different blocks compare two market states, not two routes.
    """
    from sniper.execution.evm_read_rpc import _rpc
    try:
        response = _rpc(rpc_url, "eth_blockNumber", [], http=kw.get("http"))
    except Exception:
        return None
    raw = response.get("result") if isinstance(response, dict) else None
    if not isinstance(raw, str) or not raw.startswith("0x"):
        return None
    try:
        number = int(raw, 16)
    except ValueError:
        return None
    return hex(number) if number > 0 else None


def _measure_paths(chain, cash_token, mint, amount_atomic, rpc_url, paths, block, budget, **kw):
    """[(out, back_cash, reason) | None] per path, in the order given.

    None = the path was not measured before the deadline. The reverse quote sells the quantity
    that path's forward quote acquired. Reasons return to the caller because `_note` is per thread.
    """
    def _one(legs_path):
        """Forward and reverse quote for one ordered candidate. Never partial evidence.

        BOTH DIRECTIONS ON EVERY CANDIDATE. A buy quote is not evidence of a sell quote, and
        buyable-and-not-sellable is the one outcome this module never accepts.
        """
        n = len(legs_path)
        if budget.expired():
            return None
        forward = tuple((leg["currency"], leg["key"]) for leg in legs_path)
        out = _quote_path(chain, cash_token, forward, amount_atomic, rpc_url, block=block, **kw)
        if not out and budget.stop_reason is not None:
            return None                    # the limit refused the request: unmeasured, not no-quote
        if not out:
            # THE TWO-LEG NAMES DO NOT CHANGE. A longer path gets the counted name from the
            # spec; the existing pair keeps the exact strings the tape and its tests already read.
            return (None, None,
                    "forward_path_no_quote" if n == 2 else "path_forward_no_quote:%d" % n)
        back = tuple(
            (legs_path[i - 1]["currency"] if i else cash_token, legs_path[i]["key"])
            for i in range(n - 1, -1, -1))
        back_cash = _quote_path(chain, mint, back, out, rpc_url, block=block, **kw)
        if not back_cash and budget.stop_reason is not None:
            return None
        if not back_cash:
            logger.info("multihop refused %s: complete reverse path does not quote", mint)
            return (None, None,
                    "reverse_path_no_quote" if n == 2 else "path_reverse_no_quote:%d" % n)
        return out, back_cash, None

    workers = max(1, min(4, len(paths)))
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="evm-multihop-path")
    futures = [executor.submit(_one, legs_path) for legs_path, _meta in paths]
    from sniper.execution.evm_wallet import http_tap
    try:
        with http_tap("v4_multihop:path_quotes", n=len(paths)):
            wait(futures, timeout=budget.remaining_s())
    finally:
        # A late answer must not change the decision, and paths not yet started never send.
        executor.shutdown(wait=False, cancel_futures=True)
    results = []
    for future in futures:
        if future.done() and not future.cancelled():
            try:
                results.append(future.result())
            except Exception:
                logger.exception("multihop path measurement failed for %s", mint)
                results.append(None)
        else:
            results.append(None)
    return results


def _multihop_cash(chain):
    """The currency the multihop's bridge search and settlement use as "cash" (2026-09-17).

    On 4663/8453/56, V4 pools price against wrapped native (or its unwrapped form the executor
    handles around it), so `_NATIVE` is exactly the right search currency and stays unchanged.
    On Arc (5042), V4 pools price against the real USDC ERC-20 (0x3600...0000) -- gas and quote
    share one BALANCE there (`native_is_quote`), but pool KEYS reference the ERC-20 address, never
    the native zero-address sentinel. Searching for a bridge against `_NATIVE` on Arc therefore
    found nothing even when a real, adequately-deep, fixed-fee USDC-paired bridge existed
    (XAUM/USDC, pool 0x5ca7a0c9..., fee 10000, unhooked, $5,232 depth) -- the cause of the
    AURGUS/AU no_route family. This is the one-line fix; nothing else in this module needed to
    change since `multihop_quote` already takes `cash_token` as a parameter.
    """
    from sniper.execution.evm_v4_quote import _NATIVE
    from sniper.execution.evm_wallet import CHAIN_QUOTE, native_is_quote
    if native_is_quote(chain):
        token = (CHAIN_QUOTE.get(str(chain)) or {}).get("token")
        if token:
            return token
    return _NATIVE


def dex_v4_multihop_quote(chain, sell_token, buy_token, amount_atomic, taker, *, http=None,
                          slippage_bps=None, acct=None, rpc_url=None, gt_http=None, now_fn=None,
                          clip_usd=None, coin_pool_hint=None, bridge_hint=None,
                          pinned_accept_fn=None, prefix_hint=None, pinned_only=False):
    """Venue-rung entry point: decide a two-hop route AND build its transaction, or None.

    Shaped like `dex_v4_quote` so it can sit as a rung in the venue ladder after single-hop V4.

    FAIL-CLOSED IN BOTH HALVES. The decision layer refuses on an unknown depth, a below-floor
    coin leg, a dynamic-fee bridge, or any of the four legs failing to quote; the builder refuses
    on an unsane fee, an amount that does not fit uint128, or a missing signer. A route that
    cannot be BUILT is never returned as quotable — that separation is what stopped step 0
    shipping a quote whose transaction was malformed.
    """
    from sniper.settings import memecoin as cfg
    _note("multihop_disabled")
    _refusal.pinned = None
    if not bool(getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False)):
        return None
    bridges = tuple(getattr(cfg, "MEMECOIN_EVM_MULTIHOP_BRIDGES", {}).get(str(chain), ()) or ())
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    cash = _multihop_cash(chain)
    weth = ((CHAIN_QUOTE.get(str(chain)) or {}).get("token") or "").lower()
    if not weth:
        return None
    is_buy = str(sell_token).lower() == weth
    mint = str(buy_token if is_buy else sell_token)
    if clip_usd is None:
        clip_usd = float(getattr(cfg, "MEMECOIN_COPY_SIZE_USD", 0) or 0)
    if not is_buy:
        return dex_v4_multihop_exit_quote(
            chain, mint, amount_atomic, taker, route=None, http=http,
            slippage_bps=slippage_bps, acct=acct, rpc_url=rpc_url,
            gt_http=gt_http, now_fn=now_fn, clip_usd=clip_usd)
    metadata_http = getattr(http, "owned_metadata_headers", None) if str(chain) == "4663" else None
    metadata_kw = {"metadata_http": metadata_http} if callable(metadata_http) else {}
    hint_kw = {"coin_pool_hint": coin_pool_hint} if coin_pool_hint is not None else {}
    if bridge_hint is not None:
        hint_kw.update(bridge_hint=bridge_hint, pinned_accept_fn=pinned_accept_fn)
    if prefix_hint is not None:
        hint_kw.update(prefix_hint=prefix_hint, pinned_accept_fn=pinned_accept_fn,
                       pinned_only=bool(pinned_only))
    route = multihop_quote(
        chain, cash, mint, amount_atomic, rpc_url=rpc_url,
        bridge_currencies=bridges, clip_usd=clip_usd, http=http, gt_http=gt_http, **metadata_kw,
        **hint_kw)
    if route is None:
        return None
    # THE BUILDER NEEDS rpc_url FOR AN ERC-20 CASH TOKEN'S PERMIT2 NONCE LOOKUP
    # (build_multihop_entry -> _permit2_input), and multihop_quote's own return dict never carried
    # one -- the exit path already adds it (see `selected = {**selected, "rpc_url": rpc_url}`
    # below), but nothing ever needed it on ENTRY before, because every existing multihop chain's
    # cash is native (no permit2 branch taken). Arc's ERC-20 cash entry is the first caller that
    # exercises this branch, and it silently built_failed without this line.
    route = {**route, "rpc_url": rpc_url}
    if slippage_bps is None:
        slippage_bps = int(round(cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT * 100))
    min_out = int(route["amount_out"]) * (10_000 - int(slippage_bps)) // 10_000
    if min_out <= 0:
        _note("min_out_zero")
        return None
    from sniper.execution.evm_v4_multihop_tx import as_zerox_quote
    built = as_zerox_quote(chain, cash, mint, route, amount_atomic, min_out,
                           out_amount=route["amount_out"], taker=taker, acct=acct, now_fn=now_fn)
    if built is None:
        _note("build_failed")
        return None
    built["route"]["coin_depth_usd"] = route.get("coin_depth_usd")
    built["route"]["round_trip_pct"] = route.get("round_trip_pct")
    for field in ("quote_block", "paths_considered", "paths_measured", "search_requests",
                  "search_stop", "search_discovery_cut", "discovery", "store_refusal",
                  "pinned_refusal", "bridge_pool_id", "bridge_depth_usd"):
        built["route"][field] = route.get(field)
    return built


def dex_v4_multihop_exit_quote(chain, mint, amount_atomic, taker, *, route=None, http=None,
                                slippage_bps=None, acct=None, rpc_url=None, gt_http=None,
                                now_fn=None, clip_usd=None):
    """Quote and build a coin to bridge to WETH exit.

    EVERY REFUSAL NAMES ITSELF (2026-09-06). The entry search notes each branch; this one noted
    NOTHING, so an exit that could not route returned a bare None and the caller could only say
    "configured sell venue returned no executable quote" with no cause. AIR (0x31dc304f, chain
    4663) is the case: its only pool pairs it with another memecoin, the BUY direction quoted, the
    sell did not, and finding out why meant re-running the search by hand — the reason channel
    still held a stale note from an earlier call. A silent None is not a diagnosis.
    """
    from sniper.settings import memecoin as cfg
    cash = _multihop_cash(chain)
    if clip_usd is None:
        clip_usd = float(getattr(cfg, "MEMECOIN_COPY_SIZE_USD", 0) or 0)
    min_depth = float(clip_usd) * float(
        getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MIN_DEPTH_CLIPS", 100.0))
    kw = {"http": http} if http is not None else {}
    if gt_http is not None:
        kw["gt_http"] = gt_http
    coin_pools = None
    if isinstance(route, dict) and route.get("bridge"):
        candidates = (route["bridge"],)
    else:
        coin_pools = _pools_with_depth(chain, mint, **kw)
        candidates = _candidate_bridges(
            chain, cash, mint, rpc_url, (), pools=coin_pools, **kw)
    selected = route
    if not isinstance(selected, dict):
        selected = None
        for bridge in candidates:
            bridge_leg = _select_bridge(chain, cash, bridge, rpc_url, **kw)
            coin_leg = _select_coin_leg(
                chain, bridge, mint, rpc_url, min_depth, pools=coin_pools, **kw)
            if bridge_leg and coin_leg:
                selected = {"bridge": bridge, "bridge_key": bridge_leg[1],
                            "coin_key": coin_leg[1], "rpc_url": rpc_url}
                break
    if not selected:
        # No (bridge leg, coin leg) pair survived: either the coin has no counter-currency we can
        # reach from cash, or the bridge leg has no fixed-fee pool deep enough for this clip.
        _note("exit_no_bridge_candidate:%s" % (str(mint)[:10],))
        return None
    selected = {**selected, "rpc_url": rpc_url}
    coin_key, bridge_key = selected["coin_key"], selected["bridge_key"]
    _, bridge_is_c0 = _pair_of(coin_key, selected["bridge"], mint)
    # SELL WHAT WE CAN, RATHER THAN NOTHING (2026-09-04, operator).
    #
    # This leg used to quote the WHOLE bag once and give up. A pool deep enough for most of a
    # position but not all of it therefore produced NO exit at all, and the position sat stuck
    # holding 100% of the risk -- the operator noticed this as a cause of stuck bags, and it was
    # measured independently on LARP 50682: leg 2 reverted at the full held amount and cleared at
    # about 84.4% in one hop. A partial exit is strictly better than a bag we cannot sell.
    #
    # The step-down runs ONLY after the full amount fails, so a healthy exit costs exactly one
    # quote as before. The built transaction carries the REDUCED amount, so the executor sells that
    # much and the remainder stays in the wallet for the next sweep to take -- the position is not
    # closed on our say-so, it is closed by what the balance actually shows.
    # A STORED ROUTE LONGER THAN TWO LEGS IS REPLAYED AS ONE REVERSED PATH. The two-leg pair below
    # is unchanged, so every position opened before `legs_path` existed exits exactly as it did.
    legs_path = selected.get("legs_path") or ()
    if len(legs_path) > 2:
        sell_path = tuple(
            (legs_path[i - 1]["currency"] if i else cash, legs_path[i]["key"])
            for i in range(len(legs_path) - 1, -1, -1))
        sell_atomic = int(amount_atomic)
        out = _quote_path(chain, mint, sell_path, sell_atomic, rpc_url, **kw)
        if not out:
            for frac in _EXIT_SIZE_LADDER:
                trial = int(int(amount_atomic) * frac)
                if trial <= 0:
                    continue
                out = _quote_path(chain, mint, sell_path, trial, rpc_url, **kw)
                if out:
                    sell_atomic = trial
                    logger.warning(
                        "v4 multihop exit: full bag did not quote on %s over %d legs; "
                        "selling %.0f%% (%d of %d)",
                        str(mint)[:12], len(legs_path), frac * 100.0, trial, int(amount_atomic))
                    break
        if not out:
            _note("exit_path_no_quote_at_any_size:%d_legs" % (len(legs_path),))
            return None
        if slippage_bps is None:
            slippage_bps = int(round(cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT * 100))
        min_out = int(out) * (10_000 - int(slippage_bps)) // 10_000
        from sniper.execution.evm_v4_multihop_tx import as_zerox_exit_quote
        built = as_zerox_exit_quote(
            chain, cash, mint, selected, sell_atomic, min_out,
            out_amount=out, taker=taker, acct=acct, now_fn=now_fn)
        if built is None:
            # THE QUOTE SUCCEEDED AND THE TRANSACTION DID NOT BUILD. The entry path already names
            # this (`build_failed`); the exit path returned the None unchecked, which is the branch
            # AIR actually took on 2026-09-06 and the reason its refusal had no cause.
            _note("exit_build_failed:%d_legs" % (len(legs_path),))
            return None
        if sell_atomic != int(amount_atomic):
            built["partial_exit"] = True
            built["requested_sell_amount"] = int(amount_atomic)
            built["sell_amount"] = sell_atomic
        return built

    sell_atomic = int(amount_atomic)
    bridge_amount = _quote_leg(chain, coin_key, not bridge_is_c0, sell_atomic, rpc_url, **kw)
    if not bridge_amount:
        for frac in _EXIT_SIZE_LADDER:
            trial = int(int(amount_atomic) * frac)
            if trial <= 0:
                continue
            bridge_amount = _quote_leg(
                chain, coin_key, not bridge_is_c0, trial, rpc_url, **kw)
            if bridge_amount:
                sell_atomic = trial
                logger.warning(
                    "v4 multihop exit: full bag did not quote on %s; selling %.0f%% (%d of %d)",
                    str(mint)[:12], frac * 100.0, trial, int(amount_atomic))
                break
    if not bridge_amount:
        _note("exit_coin_leg_no_quote_at_any_size")
        return None
    _, cash_is_c0 = _pair_of(bridge_key, cash, selected["bridge"])
    out = _quote_leg(chain, bridge_key, not cash_is_c0, bridge_amount, rpc_url, **kw)
    if not out:
        _note("exit_bridge_leg_no_quote")
        return None
    if slippage_bps is None:
        slippage_bps = int(round(cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT * 100))
    min_out = int(out) * (10_000 - int(slippage_bps)) // 10_000
    from sniper.execution.evm_v4_multihop_tx import as_zerox_exit_quote
    built = as_zerox_exit_quote(
        chain, cash, mint, selected, sell_atomic, min_out,
        out_amount=out, taker=taker, acct=acct, now_fn=now_fn)
    if built is None:
        _note("exit_build_failed:2_legs")
        return None
    if sell_atomic != int(amount_atomic):
        # Make the partial VISIBLE. A caller that assumes a full close would otherwise book one.
        built["partial_exit"] = True
        built["requested_sell_amount"] = int(amount_atomic)
        built["sell_amount"] = sell_atomic
    return built
