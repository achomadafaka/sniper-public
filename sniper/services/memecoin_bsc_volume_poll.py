"""BSC (56) volume-death feed: poll the operator's archive endpoint for Swap logs.

Spec docs/superpowers/specs/2026-10-01-memecoin-bsc-swap-feed-design.md. Default off
(MEMECOIN_VOLDEATH_POLL_CHAINS). One daemon thread in the runner process, never on the fill path.

Every tick: rebind the pools of open funded 56 positions, then one eth_blockNumber and one eth_getLogs
over [last + 1, head] for those pools plus an always-busy CANARY pool. A good range covers every bound
pool and records their swaps; any failure, or a catch-up over MAX_RANGE blocks, disconnects the state
(blind) and coverage restarts at the head. A gap is never back-filled into coverage.

A position binds EVERY known pool of its mint (the 49013 stranger's quiet in reverse): the entry pair,
the route's pool, the pool our own entry receipt touched, and the V2/V3 factory pools against WBNB,
USDT, BUSD and USDC, read in ONE Multicall3 eth_call. A mint whose set cannot be read binds nothing,
which the rail reads as `blind_frozen:pool_uncovered`. Flap curves bind token-specific Portal
trades; Infinity CL binds verified cached keys and the pool our receipt actually executed.

Host: MEMECOIN_EVM_ARCHIVE_RPC_URLS_56 only, refused when it shares a host with a funded 56 URL.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from eth_abi import decode, encode
from eth_utils import keccak

from sniper.execution.evm_flap_curve import FLAP_PORTAL
from sniper.execution.evm_pancake_infinity import MANAGER as INFINITY_MANAGER, SWAP_TOPIC as INFINITY_SWAP
from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP
from sniper.services.memecoin_executed_pool import (
    PANCAKE_V3_SWAP, V2_SWAP, V3_SWAP, executed_pool_from_receipt)

logger = logging.getLogger(__name__)

SWAP_TOPICS = (V2_SWAP, V3_SWAP, PANCAKE_V3_SWAP)
# https://github.com/flap-sh/FlapVaultExample/blob/main/src/flap/IPortal.sol
# TokenBought/TokenSold carry seven non-indexed words, including token in word 1.
FLAP_TOPICS = tuple("0x" + keccak(text=f"{name}(uint256,address,address,uint256,uint256,uint256,uint256)").hex()
                    for name in ("TokenBought", "TokenSold"))
MAX_RANGE = 40                 # blocks; BSC measured 0.45 s/block on 2026-10-01, so ~18 s
POLL_S = 1.5                   # ~3 blocks; ~1.3 requests/s (block number + logs)
RESOLVE_PER_TICK = 1           # initial resolutions + refreshes per tick (one multicall + optional receipt)
REDISCOVER_EVERY_S = 20.0      # each bound mint's pool set is re-read this often, on this thread
POLL_TIMEOUT_S = 10.0
CONFIRM_TIMEOUT_S = 2.0        # the confirm runs on the funded exit sweep: a hard short bound

MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT = "0x55d398326f99059ff775485246999027b3197955"
BUSD = "0xe9e7cea3dedca5984780bafc599bd69add087d56"
USDC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
QUOTES = (WBNB, USDT, BUSD, USDC)
V2_FACTORIES = ("0xca143ce32fe78f1f7019d7d551a6402fc5350c73",     # PancakeSwap V2
                "0x8909dc15e40173ff4699343b6eb8132c65e18ec6")     # Uniswap V2 (BSC)
V3_FACTORIES = (("0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865", (100, 500, 2500, 10000)),   # Pancake V3
                ("0xdb1d10011ad0ff90774d0c6bb92e5c5c8b4461f7", (100, 500, 3000, 10000)))   # Uniswap V3
GET_PAIR, GET_POOL = bytes.fromhex("e6a43905"), bytes.fromhex("1698ee82")
ZERO = "0x" + "0" * 40


def archive_url(chain="56"):
    from sniper.settings import memecoin as cfg
    urls = cfg.evm_archive_rpc_urls_for_chain(chain)
    return urls[0] if urls else None


# CHAIN 1 (spec 2026-10-08-memecoin-voldeath-chain1-arc-witness-design.md, Alpha approved): Uniswap V2,
# Sushi V2 and Uniswap V3 against WETH/USDC/USDT, plus V4 by pool id on the PoolManager. The canary is
# Uniswap V3 WETH/USDC 0.05%, read from the factory. A 24 s tick spans two 12 s blocks.
CHAIN1 = {
    "v2_factories": ("0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f",      # Uniswap V2
                     "0xc0aee478e3658e2610c5f7a4a2e1777ce9e4f2ac"),     # SushiSwap V2
    "v3_factories": (("0x1f98431c8ad98523631ae4a59f267346ea31f984", (100, 500, 3000, 10000)),),
    "quotes": ("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",            # WETH
               "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",            # USDC
               "0xdac17f958d2ee523a2206206994597c13d831ec7"),           # USDT
    "poll_s": 24.0, "block_s": 12.0, "max_range": 10,
}


# The keyed dRPC research account is saturated (2026-10-08: its shared pacer queued ~7 slots, acquire
# 4.9-6.0 s against a 54 ms HTTP call), so chain 1 uses the KEYLESS public host, used by no other role,
# with its own pacer. MEMECOIN_VOLDEATH_POLL_URL_1 overrides it.
CHAIN1_DEFAULT_URL = "https://eth.drpc.org"
PACER_GAP_S = 2.0              # chain-1 ticks: 3 calls per 24 s fit with room
PACER_PAUSE_S = 60.0


# ARC (5042), Alpha 2026-10-08: vol_death only. Most of Arc's flow is V4 (10 swaps in 12 blocks
# measured) and no V2 factory is known, so the canary is the PoolManager itself (`canary_v4`): any V4
# swap on the chain vouches for the host. The V3 factory was read from the configured quoter's
# factory() live; the quote is Arc's native USDC. 0.5 s blocks; a 6 s tick reads 12.
ARC = {
    "v2_factories": (),
    "v3_factories": (("0xf0db7b58379503491d857db50ac9ece64c653918", (100, 500, 3000, 10000)),),
    "quotes": ("0x3600000000000000000000000000000000000000",),         # native USDC
    "v4_manager": V4_POOL_MANAGERS["5042"],
    "poll_s": 6.0, "block_s": 0.5, "max_range": 60, "pacer_gap_s": 1.0,
}
ARC_DEFAULT_URL = "https://arc.drpc.org"


def poll_url(chain):
    """The witness host for a poll chain. 56: the operator's archive endpoint. 1 and 5042: the keyless
    eth.drpc.org / arc.drpc.org (or MEMECOIN_VOLDEATH_POLL_URL_<chain>). Never a funded host
    (`archive_host_refusal` still checks)."""
    chain = str(chain)
    if chain == "56":
        return archive_url("56")
    if chain == "1":
        return os.environ.get("MEMECOIN_VOLDEATH_POLL_URL_1", "").strip() or CHAIN1_DEFAULT_URL
    if chain == "5042":
        return os.environ.get("MEMECOIN_VOLDEATH_POLL_URL_5042", "").strip() or ARC_DEFAULT_URL
    return None


def archive_host_refusal(chain, url):
    """A reason when the archive URL shares a host with any URL the funded runner uses on the chain,
    else None. The set is DERIVED (Gamma L2): every MEMECOIN_*URL*_<chain> name except the archive
    and research ones (custody RPC, pool, WS, on-chain watcher, capture read)."""
    host = (urlparse(str(url or "")).hostname or "").lower()
    suffix = "_%s" % chain
    names = [k for k in os.environ if k.startswith("MEMECOIN_") and k.endswith(suffix)
             and "URL" in k and "ARCHIVE" not in k and "RESEARCH" not in k]
    funded = {(urlparse(u.strip()).hostname or "").lower()
              for key in names for u in os.environ.get(key, "").split(",") if u.strip()}
    if not host:
        return "archive url unparseable"
    if host in funded:
        return "archive host %s is a funded %s host" % (host, chain)
    return None


def make_call(url, *, timeout_s=POLL_TIMEOUT_S, pacer=None, stats=None):
    """A JSON-RPC caller on one URL with a hard timeout: the result, or raise (HTTP error, timeout,
    error body). `pacer` (a SharedPacer) spaces the calls across processes and pauses on a 429, for
    a public host with no per-key budget (chain 1's keyless eth.drpc.org). `stats` (a deque) collects
    each call's wall ms, pacer wait included, for the utilization line."""
    def call(method, params):
        started = time.monotonic()
        try:
            if pacer is not None:
                pacer.acquire()
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
            req = urllib.request.Request(url, body, {"Content-Type": "application/json",
                                                     "User-Agent": "sniper-voldeath/1"})
            try:
                payload = json.loads(urllib.request.urlopen(req, timeout=timeout_s).read())
            except urllib.error.HTTPError as exc:
                if pacer is not None and exc.code == 429:
                    pacer.pause(PACER_PAUSE_S)
                raise
            if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
                raise RuntimeError("rpc %s: %s" % (method, str(payload)[:160]))
            return payload["result"]
        finally:
            if stats is not None:
                stats.append((time.monotonic() - started) * 1000.0)
    return call


def _addr(word):
    a = "0x" + bytes(word)[-20:].hex()
    return None if a == ZERO else a


def route_quote_tokens(pos):
    """Every token the position's route names, the coin excepted. 56 routes hop through other base
    tokens (position 67623: WBNB -> 0x4902c5eb... -> coin), so the coin's pools can pair with a
    token outside QUOTES."""
    mint = str(pos.get("mint") or "").lower()
    try:
        route = json.loads(pos.get("evm_route_json") or "{}")
    except (TypeError, ValueError):
        return ()
    names = [route.get("base_token")]
    for key in ("path", "tokens", "v3_tokens", "exit_v3_tokens"):
        names.extend(route.get(key) or ())
    out = []
    for t in names:
        t = str(t or "").lower()
        if t.startswith("0x") and len(t) == 42 and t != mint and t not in out:
            out.append(t)
    return tuple(out)


def discover_pools(call, mint, quotes=(), *, v2_factories=None, v3_factories=None,
                   base_quotes=None):
    """Every V2/V3 factory pool of `mint` against the chain's base quotes plus `quotes`, in one
    Multicall3 eth_call (56's factories and quotes unless named). None when the read failed or any
    sub-call failed: an unknown set is not an empty one."""
    mint = str(mint).lower()
    v2_factories = V2_FACTORIES if v2_factories is None else v2_factories
    v3_factories = V3_FACTORIES if v3_factories is None else v3_factories
    base_quotes = QUOTES if base_quotes is None else base_quotes
    all_quotes = tuple(dict.fromkeys(tuple(base_quotes) + tuple(q for q in quotes if q != mint)))
    calls = []
    for factory in v2_factories:
        for q in all_quotes:
            calls.append((factory, True, GET_PAIR + encode(["address", "address"], [mint, q])))
    for factory, fees in v3_factories:
        for q in all_quotes:
            for fee in fees:
                calls.append((factory, True,
                              GET_POOL + encode(["address", "address", "uint24"], [mint, q, fee])))
    data = bytes.fromhex("82ad56cb") + encode(["(address,bool,bytes)[]"], [calls])   # aggregate3
    try:
        raw = call("eth_call", [{"to": MULTICALL3, "data": "0x" + data.hex()}, "latest"])
        (results,) = decode(["(bool,bytes)[]"], bytes.fromhex(str(raw)[2:]))
    except Exception:
        logger.warning("bsc poll: pool discovery failed mint=%s", mint, exc_info=True)
        return None
    if len(results) != len(calls) or not all(ok and len(ret) >= 32 for ok, ret in results):
        return None
    return frozenset(a for _ok, ret in results if (a := _addr(ret[-32:])))


def canary_pool(call, chain="56"):
    """56: PancakeSwap V2 WBNB/USDT. 1: Uniswap V3 WETH/USDC 0.05%. Read from the factory, never
    typed in. None when unreadable."""
    try:
        if str(chain) == "1":
            factory, _fees = CHAIN1["v3_factories"][0]
            weth, usdc, _usdt = CHAIN1["quotes"]
            data = GET_POOL + encode(["address", "address", "uint24"], [weth, usdc, 500])
        else:
            factory, data = V2_FACTORIES[0], GET_PAIR + encode(["address", "address"], [WBNB, USDT])
        raw = call("eth_call", [{"to": factory, "data": "0x" + data.hex()}, "latest"])
        return _addr(bytes.fromhex(str(raw)[2:])[-32:])
    except Exception:
        logger.warning("chain %s poll: canary lookup failed", chain, exc_info=True)
        return None


def contract_pools(pos):
    """(pools named by the position's own records, is_curve)."""
    try:
        contract = json.loads(pos.get("funded_contract_json") or "{}")
        route = json.loads(pos.get("evm_route_json") or "{}")
    except (TypeError, ValueError):
        return set(), False
    facts = (contract.get("classification") or {}).get("facts") or {}
    named = {str(p).lower() for p in (facts.get("liquidity_pair_address"), route.get("pool_id")) if p}
    # 66-char V4 pool ids too; a poller without a V4 manager (56) drops them (`BscVolumePoller`).
    return {p for p in named if p.startswith("0x") and len(p) in (42, 66)}, bool(route.get("curve"))


def executed_pool_fn_for(call, entry_tx_fn, chain="56"):
    """The entry's last supported swap, or None for a valid receipt without one; unknown raises."""
    def executed(pos):
        tx = entry_tx_fn(pos)
        if not tx:
            raise RuntimeError("entry transaction unavailable")
        receipt = call("eth_getTransactionReceipt", [tx])
        if (not isinstance(receipt, dict) or not isinstance(receipt.get("logs"), list)
                or any(not isinstance(log, dict) for log in receipt["logs"])):
            raise RuntimeError("entry receipt unavailable")
        pool, _source, _n = executed_pool_from_receipt(str(chain), receipt, strict=True)
        return pool if pool and len(pool) in (42, 66) else None
    return executed


def _flap_key(pos):
    try:
        route = json.loads(pos.get("evm_route_json") or "{}")
        mint = str(pos.get("mint") or "").lower()
        if (str(pos.get("network_id")) == "56" and route.get("protocol") == "flap_curve"
                and str(route.get("curve") or "").lower() == FLAP_PORTAL["56"]
                and mint.startswith("0x") and len(mint) == 42 and int(mint[2:], 16)):
            return "flap:" + mint
    except (TypeError, ValueError, AttributeError):
        pass
    return None


def _address_filter(pools, canary, *, infinity=False):
    addresses = [p for p in pools if p.startswith("0x") and len(p) == 42] + [canary]
    topics = list(SWAP_TOPICS)
    if any(p.startswith("flap:") for p in pools):
        addresses.append(FLAP_PORTAL["56"])
        topics.extend(FLAP_TOPICS)
    if infinity and any(len(p) == 66 for p in pools):
        addresses.append(INFINITY_MANAGER)
        topics.append(INFINITY_SWAP)
    return {"address": list(dict.fromkeys(addresses)), "topics": [topics]}


def _bound_logs(logs, pools, *, infinity=False):
    """Singleton events become exact bound keys; malformed singleton data is an unknown range."""
    if not isinstance(logs, list):
        raise ValueError("logs is not a list")
    out = []
    for log in logs:
        address = str(log.get("address") or "").lower()
        topics = log.get("topics") or ()
        if address == FLAP_PORTAL["56"]:
            if len(topics) != 1 or str(topics[0]).lower() not in FLAP_TOPICS:
                raise ValueError("unknown Portal trade")
            raw = str(log.get("data") or "")
            if not raw.startswith("0x") or len(raw) != 2 + 7 * 64:
                raise ValueError("malformed Portal trade")
            _ts, mint, _actor, amount, _quote, _fee, _price = decode(
                ["uint256", "address", "address", "uint256", "uint256", "uint256", "uint256"],
                bytes.fromhex(raw[2:]))
            key = "flap:" + mint.lower()
            if key in pools and amount > 0:
                out.append(dict(log, address=key))
        elif infinity and address == INFINITY_MANAGER:
            if len(topics) != 3 or str(topics[0]).lower() != INFINITY_SWAP:
                raise ValueError("unknown Infinity swap")
            key = str(topics[1]).lower()
            if len(key) != 66 or not key.startswith("0x"):
                raise ValueError("malformed Infinity pool")
            int(key[2:], 16)
            raw = str(log.get("data") or "")
            if not raw.startswith("0x") or len(raw) != 2 + 7 * 64:
                raise ValueError("malformed Infinity swap")
            decode(["int128", "int128", "uint160", "uint128", "int24", "uint24", "uint16"],
                   bytes.fromhex(raw[2:]))
            if key in pools:
                out.append(dict(log, address=key))
        else:
            out.append(log)
    return out


class BscVolumePoller:
    """One chain's bound-pool poller. 56 uses its archive host and tick-time stamps.
    Chain 1 (spec 2026-10-08-memecoin-voldeath-chain1-arc-witness-design.md) adds V4 pools by id on
    `v4_manager` (`v4_ids_fn(mint)` from the PoolKey cache) and stamps activity at its BLOCK's time
    (`block_s`), since a 24 s tick spans two blocks.

    RESIDUAL (Gamma on 93eb73537): only the V4 pools in the PoolKey cache are bound, plus our own
    executed pool (always). A coin trading flat on an uncached V4 pool reads silent here; only the
    executable-price-moved veto guards that. Confirmation starts after the latest activity block
    when its cursor is known; otherwise it uses the recorded head timestamps."""

    def __init__(self, state, *, call, positions_fn, discover_fn, executed_pool_fn, canary,
                 max_range=MAX_RANGE, chain="56", v4_manager=None, v4_ids_fn=None, block_s=None,
                 canary_v4=False, v4_swap_topic=V4_SWAP):
        self.state, self.call, self.positions_fn = state, call, positions_fn
        self.discover_fn, self.executed_pool_fn = discover_fn, executed_pool_fn
        self.canary, self.max_range = str(canary).lower(), int(max_range)
        self.chain, self.v4_ids_fn = str(chain), v4_ids_fn
        self.v4_manager = str(v4_manager).lower() if v4_manager else None
        self.v4_swap_topic = v4_swap_topic
        self.block_s = float(block_s) if block_s else None
        # Arc: the canary is the PoolManager's chain-wide V4 flow, so the V4 read asks for every Swap
        # and keeps our ids client-side, and the V2/V3 read runs only when a V3 pool is bound.
        self.canary_v4 = bool(canary_v4 and self.v4_manager)
        self.last = None
        self._pools = {}               # position id -> frozenset of pools (resolved once, then grown)
        self.quotes = {}               # mint -> route quote tokens
        self._rediscovered_at = {}     # position id -> last good pool-set read
        self._resolve_order = []       # eligible position ids, rotated on every attempt, even failures

    def bindings(self, now_ts=None):
        """{pool: mint} for open 56 positions whose pool set is resolved. Resolution and the periodic
        re-discovery are bounded per tick and run HERE, never on the exit sweep; an unresolved or
        failed mint binds nothing (uncovered, never a partial set)."""
        now_ts = time.time() if now_ts is None else now_ts
        out = {}
        positions = [p for p in (self.positions_fn() or []) if str(p.get("network_id")) == self.chain]
        live = {p.get("id") for p in positions}
        self._pools = {pid: pools for pid, pools in self._pools.items() if pid in live}
        self._rediscovered_at = {pid: t for pid, t in self._rediscovered_at.items() if pid in live}
        pending = {}
        for pos in positions:
            pid = pos.get("id")
            if pid not in self._pools:
                _named, curve = contract_pools(pos)
                if curve and not _flap_key(pos):
                    self._pools[pid] = frozenset()
                else:
                    pending[pid] = pos
            elif (pid in self._rediscovered_at
                  and now_ts - self._rediscovered_at[pid] >= REDISCOVER_EVERY_S):
                pending[pid] = pos
        # One shared queue: a failed initial read or stale refresh cannot monopolize the budget.
        order = [pid for pid in self._resolve_order if pid in pending]
        queued = set(order)
        order.extend(pid for pid in pending if pid not in queued)
        selected = order[:RESOLVE_PER_TICK]
        self._resolve_order = order[len(selected):] + selected
        for pid in selected:
            pos = pending[pid]
            mint = str(pos.get("mint") or "").lower()
            initial = pid not in self._pools
            if initial:
                self.quotes[mint] = route_quote_tokens(pos)
            try:
                found = self._found(mint, self.discover_fn(mint, self.quotes.get(mint, ())))
            except Exception:
                logger.warning("chain %s poll: pool discovery failed position=%s", self.chain, pid,
                               exc_info=True)
                continue
            if found is None:
                continue                      # retry on its next turn; never refresh a failed stamp
            if initial:
                try:
                    executed = self.executed_pool_fn(pos)
                except Exception:
                    logger.warning("chain %s poll: entry receipt unknown position=%s", self.chain, pid)
                    continue
                named, _curve = contract_pools(pos)
                named = {p for p in named if len(p) == 42 or self.v4_manager}
                if executed and len(executed) == 66 and not self.v4_manager:
                    executed = None
                self._pools[pid] = frozenset(found) | named | ({executed} if executed else set())
                if flap := _flap_key(pos):
                    self._pools[pid] |= {flap}
            else:
                self._pools[pid] |= frozenset(found)
            self._rediscovered_at[pid] = now_ts
            self.state.record_rediscovery(mint, self._pools[pid], now_ts=now_ts)
        for pos in positions:
            mint = str(pos.get("mint") or "").lower()
            for pool in self._pools.get(pos.get("id"), ()):
                out[pool] = mint
        return out

    def _found(self, mint, found):
        """The discovered V2/V3 set plus the mint's V4 pool ids where this chain reads V4."""
        if found is None or not (self.v4_manager and self.v4_ids_fn):
            return found
        try:
            ids = {str(i).lower() for i in (self.v4_ids_fn(mint) or ())
                   if str(i).startswith("0x") and len(str(i)) == 66}
        except Exception:
            logger.warning("chain %s poll: V4 pool ids unreadable mint=%s", self.chain, mint,
                           exc_info=True)
            return None                        # an unknown set is not an empty one
        return frozenset(found) | ids

    def _stamp(self, log, head, now_ts):
        """The activity time: its block's (head minus the blocks behind it x block_s) when this chain
        names a block time, else the tick's."""
        if not self.block_s:
            return now_ts
        try:
            behind = head - int(str(log.get("blockNumber")), 16)
        except (TypeError, ValueError):
            return now_ts
        return now_ts - max(0, behind) * self.block_s

    def tick(self, *, now_ts):
        bindings = self.bindings(now_ts)
        self.state.replace_bindings(bindings, now_ts=now_ts)
        if not bindings:
            # Nothing to watch: no request (the free tier's quota is unknown). Blind until a position
            # binds; its coverage then starts at that tick's head, after its open.
            self.state.disconnect()
            self.last = None
            return "idle"
        try:
            head = int(self.call("eth_blockNumber", []), 16)
        except Exception:
            logger.warning("bsc poll: block number failed", exc_info=True)
            return self._blind()
        if self.last is None:
            self.last = head - 1
        if head - self.last > self.max_range:
            logger.warning("bsc poll: catch-up %d blocks over %d: blind, restarting at the head",
                           head - self.last, self.max_range)
            self._blind()
            self.last = head - 1
            return "over_range"
        if head <= self.last:
            return "no_new_block"
        pools = sorted(bindings)
        addresses = [p for p in pools if p.startswith("0x") and len(p) == 42]
        ids = [p for p in pools if len(p) == 66] if self.v4_manager else []
        infinity = self.chain == "56" and self.v4_manager == INFINITY_MANAGER
        try:
            logs = []
            if addresses or not self.canary_v4:
                flt = (_address_filter(pools, self.canary, infinity=infinity) if not self.canary_v4
                       else {"address": addresses, "topics": [list(SWAP_TOPICS)]})
                logs = self.call("eth_getLogs", [{**flt,
                    "fromBlock": hex(self.last + 1), "toBlock": hex(head),
                    }])
                logs = _bound_logs(logs, pools, infinity=infinity)
            if (ids or self.canary_v4) and not infinity:
                v4 = self.call("eth_getLogs", [{"fromBlock": hex(self.last + 1), "toBlock": hex(head),
                                                "address": self.v4_manager,
                                                "topics": ([self.v4_swap_topic] if self.canary_v4
                                                           else [self.v4_swap_topic, ids])}])
                if not isinstance(v4, list):
                    raise RuntimeError("v4 logs is %s" % type(v4).__name__)
                logs = list(logs) + list(v4)
        except Exception:
            logger.warning("chain %s poll: logs %d..%d failed", self.chain, self.last + 1, head,
                           exc_info=True)
            return self._blind()
        for log in logs:
            address = str(log.get("address") or "").lower()
            seen = self._stamp(log, head, now_ts)
            if self.v4_manager and address == self.v4_manager:
                if self.canary_v4:
                    self.state.record_canary(now_ts=seen)
                topics = log.get("topics") or ()
                if len(topics) > 1:          # a pool we do not bind is ignored by the state
                    self.state.record_pool_activity(str(topics[1]).lower(), now_ts=seen,
                                                    block_number=log.get("blockNumber"))
            elif address == self.canary:
                self.state.record_canary(now_ts=seen)
            else:
                self.state.record_pool_activity(address, now_ts=seen,
                                                block_number=log.get("blockNumber"))
        for pool in pools:
            self.state.record_coverage(pool, now_ts=now_ts)
        if self.block_s:
            # EVERY block of the range, at its own estimated time, a millisecond BEFORE a swap in it.
            # The threshold confirm reads from the first head seen at or after the last activity, so
            # with block-time stamps it starts at the block AFTER the last swap; one head per tick
            # would start it at that tick's head, which may hold the last swap itself, and a coin
            # quiet for exit_s would read moving and lose a whole window.
            for block in range(self.last + 1, head + 1):
                self.state.record_head(
                    block, now_ts=now_ts - (head - block) * self.block_s - 0.001)
        else:
            self.state.record_head(head, now_ts=now_ts)
        self.last = head
        return "ok"

    def _blind(self):
        self.state.disconnect()
        self.last = None
        return "blind"

    def run_forever(self, *, now_fn=time.time, sleep=time.sleep, poll_s=POLL_S):
        while True:
            try:
                self.tick(now_ts=now_fn())
            except Exception:
                logger.exception("bsc poll: tick failed")
                self._blind()
            sleep(poll_s)


CONFIRM_COOLDOWN_S = 15.0     # chain 1: after a failed confirm, no new ask for this long


def confirm_logs_fn(call, canary, *, v4_manager=None, to_latest=False, cooldown_s=0.0,
                    canary_v4=False, v4_swap_topic=V4_SWAP):
    """The threshold confirm: one eth_getLogs over the pools plus the canary, and on a V4 chain one
    over the PoolManager by pool id. The pools' logs, or None when unreadable OR when the canary
    shows none (a host answering [] for everything).

    BSC reads V2/V3, Portal and Infinity in the same filter, then keeps only bound identities.
    `to_latest` (chain 1): the range ends at the chain's CURRENT head, not at the last polled one. A
    24 s tick leaves up to two blocks after the last poll that no tick has read, and a swap there
    must hold the fire; ending at the last polled head would call that coin still."""
    canary = str(canary).lower()
    manager = str(v4_manager).lower() if v4_manager else None
    infinity = manager == INFINITY_MANAGER
    blocked = [0.0]                 # monotonic time before which a failed host is not asked again

    def confirm(pools, first_block, last_block):
        # A FAILED CONFIRM IS NOT REPEATED EVERY SWEEP (Gamma on 93eb73537). The decision asks on every
        # pass once silence >= exit_s, and chain 1's sweep runs every 2 s on the thread its entries
        # share: one timeout per cooldown, never one per sweep. The answer meanwhile is None, so
        # state_reader_dark, never silence.
        if cooldown_s and time.monotonic() < blocked[0]:
            return None
        out = _confirm(pools, first_block, last_block)
        if out is None and cooldown_s:
            blocked[0] = time.monotonic() + float(cooldown_s)
        return out

    def _confirm(pools, first_block, last_block):
        keys = sorted({str(p).lower() for p in pools})
        ids = [p for p in keys if len(p) == 66] if manager else []
        addresses = [p for p in keys if p.startswith("0x") and len(p) == 42]
        to = "latest" if to_latest else hex(int(last_block))
        v4_wide = bool(canary_v4 and manager)
        try:
            flt = (_address_filter(keys, canary, infinity=infinity) if not v4_wide
                   else {"address": addresses, "topics": [list(SWAP_TOPICS)]})
            logs = (call("eth_getLogs", [{**flt, "fromBlock": hex(int(first_block)), "toBlock": to}])
                    if addresses or not v4_wide else [])
            logs = _bound_logs(logs, keys, infinity=infinity)
            v4 = (call("eth_getLogs", [{"fromBlock": hex(int(first_block)), "toBlock": to,
                                        "address": manager,
                                        "topics": [v4_swap_topic] if v4_wide else [v4_swap_topic, ids]}])
                  if (ids or v4_wide) and not infinity else [])
        except Exception:
            return None
        if not isinstance(logs, list) or not isinstance(v4, list):
            return None
        if v4_wide:
            # The manager's chain-wide flow vouches for the host; only our ids are returned.
            if not v4:
                return None
            ours = [l for l in v4 if len(l.get("topics") or ()) > 1
                    and str(l["topics"][1]).lower() in set(ids)]
            return list(logs) + ours
        if not any(str(l.get("address") or "").lower() == canary for l in logs):
            return None
        return [l for l in logs if str(l.get("address") or "").lower() != canary] + list(v4)
    return confirm


_RUNTIME = {}                  # chain -> {"call", "canary"}, set by start_bsc_volume_poller
# A canary unreadable at start (one HTTP 408 on chain 1, 2026-10-14 09:19:19Z) used to leave the
# chain's vol_death blind for the runner's whole life. Retried in its own daemon thread, never on a
# fill or exit path; one retry thread per chain; backoff 30 s doubling to 10 min, until it starts.
_CANARY_RETRY = set()
CANARY_RETRY_FIRST_S, CANARY_RETRY_MAX_S = 30.0, 600.0
_retry_sleep = time.sleep


def _retry_start_later(chain, start_fn, kwargs, say):
    if chain in _CANARY_RETRY:
        return
    _CANARY_RETRY.add(chain)

    def loop():
        delay = CANARY_RETRY_FIRST_S
        try:
            while True:
                _retry_sleep(delay)
                try:
                    if start_fn(**kwargs):
                        say("[memecoin] chain-%s volume poll started on canary retry" % chain)
                        return
                except Exception:  # noqa: BLE001 - a retry failure waits for the next one
                    logger.exception("chain-%s volume poll canary retry failed", chain)
                delay = min(delay * 2, CANARY_RETRY_MAX_S)
        finally:
            _CANARY_RETRY.discard(chain)

    kwargs["thread_factory"](target=loop, name="memecoin-volpoll-canary-retry-%s" % chain,
                             daemon=True).start()


def confirm_fn_for(chain):
    """The threshold confirm, on its own caller with a CONFIRM_TIMEOUT_S hard timeout (it runs on the
    funded exit sweep). A timeout is None: held as state_reader_dark, never silence. A chain whose
    starter built one stateful confirm (chain 1, with its cooldown) gets that same function."""
    rt = _RUNTIME.get(str(chain))
    if rt and rt.get("confirm"):
        return rt["confirm"]
    return (confirm_logs_fn(rt["confirm_call"], rt["canary"], v4_manager=rt.get("v4_manager"),
                            to_latest=bool(rt.get("confirm_to_latest")))
            if rt else None)


def start_bsc_volume_poller(*, positions_fn, entry_tx_fn, thread_factory, log=None):
    """Start the 56 poller thread when 56 is listed and its archive URL is usable. Returns True when
    started. Not started: the rail reads `blind_frozen:poll_stale`, audited, never silence."""
    from sniper.services.memecoin_volume_death import EVM_POLL_STATE
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_pancake_infinity import stored_keys
    say = log or logger.warning
    if "56" not in cfg.memecoin_voldeath_poll_chains():
        return False
    url = archive_url("56")
    refusal = "no MEMECOIN_EVM_ARCHIVE_RPC_URLS_56" if not url else archive_host_refusal("56", url)
    if refusal:
        say("[memecoin] BSC volume poll NOT started: %s; 56 vol_death reads blind" % refusal)
        return False
    call = make_call(url)
    canary = canary_pool(call)
    if not canary:
        say("[memecoin] BSC volume poll NOT started: canary pool unreadable; 56 vol_death reads blind")
        _retry_start_later("56", start_bsc_volume_poller, dict(
            positions_fn=positions_fn, entry_tx_fn=entry_tx_fn, thread_factory=thread_factory,
            log=log), say)
        return False
    poller = BscVolumePoller(EVM_POLL_STATE["56"], call=call, positions_fn=positions_fn,
                             discover_fn=lambda mint, quotes=(): discover_pools(call, mint, quotes),
                             executed_pool_fn=executed_pool_fn_for(call, entry_tx_fn), canary=canary,
                             v4_manager=INFINITY_MANAGER, v4_swap_topic=INFINITY_SWAP,
                             v4_ids_fn=lambda mint: [pid for pid, _key in stored_keys(mint, require_complete=True)])
    confirm_call = make_call(url, timeout_s=CONFIRM_TIMEOUT_S)
    _RUNTIME["56"] = {"call": call, "canary": canary, "poller": poller,
                      "confirm_call": confirm_call,
                      "confirm": confirm_logs_fn(confirm_call, canary, v4_manager=INFINITY_MANAGER,
                                                 v4_swap_topic=INFINITY_SWAP)}
    thread_factory(target=poller.run_forever, name="memecoin-bsc-volume-poll", daemon=True).start()
    say("[memecoin] BSC volume poll: started (canary %s, poll %.1fs)" % (canary, POLL_S))
    return True


def start_chain1_volume_poller(*, positions_fn, entry_tx_fn, thread_factory, log=None):
    """Start the chain-1 poller thread when 1 is listed in MEMECOIN_VOLDEATH_POLL_CHAINS (spec
    2026-10-08-memecoin-voldeath-chain1-arc-witness-design.md). Ticks go through this host's own
    pacer ("voldeath-poll-1"); a utilization line (calls and wall ms, pacer wait included) is logged
    once a minute. Not started: 1 reads `blind_frozen:poll_stale`, audited."""
    return _start_poll_chain("1", dict(CHAIN1, v4_manager=V4_POOL_MANAGERS["1"],
                                       pacer_gap_s=PACER_GAP_S), canary_v4=False,
                             positions_fn=positions_fn, entry_tx_fn=entry_tx_fn,
                             thread_factory=thread_factory, log=log)


def start_arc_volume_poller(*, positions_fn, entry_tx_fn, thread_factory, log=None):
    """Start the Arc (5042) poller when 5042 is listed: keyless arc.drpc.org on its own pacer, a 6 s
    tick, the PoolManager's chain-wide V4 flow as the canary. vol_death only (Alpha 2026-10-08)."""
    return _start_poll_chain("5042", ARC, canary_v4=True, positions_fn=positions_fn,
                             entry_tx_fn=entry_tx_fn, thread_factory=thread_factory, log=log)


def _start_poll_chain(chain, spec, *, canary_v4, positions_fn, entry_tx_fn, thread_factory, log=None):
    import collections
    from sniper.execution.evm_v4_quote import _stored_pools_for_mint
    from sniper.services.memecoin_research_pacer import SharedPacer
    from sniper.services.memecoin_volume_death import EVM_POLL_STATE
    from sniper.settings import memecoin as cfg
    say = log or logger.warning
    if chain not in cfg.memecoin_voldeath_poll_chains():
        return False
    url = poll_url(chain)
    refusal = archive_host_refusal(chain, url)
    if refusal:
        say("[memecoin] chain-%s volume poll NOT started: %s; vol_death reads blind" % (chain, refusal))
        return False
    stats = collections.deque(maxlen=4096)
    pacer = SharedPacer.named("voldeath-poll-%s" % chain, gap_s=float(spec["pacer_gap_s"]))
    call = make_call(url, pacer=pacer, stats=stats)
    manager = spec["v4_manager"]
    canary = manager if canary_v4 else canary_pool(call, chain)
    if not canary:
        say("[memecoin] chain-%s volume poll NOT started: canary unreadable; vol_death reads blind"
            % chain)
        _retry_start_later(chain, _start_poll_chain, dict(
            chain=chain, spec=spec, canary_v4=canary_v4, positions_fn=positions_fn,
            entry_tx_fn=entry_tx_fn, thread_factory=thread_factory, log=log), say)
        return False
    poller = BscVolumePoller(
        EVM_POLL_STATE[chain], call=call, positions_fn=positions_fn,
        discover_fn=lambda mint, quotes=(): discover_pools(
            call, mint, quotes, v2_factories=spec["v2_factories"],
            v3_factories=spec["v3_factories"], base_quotes=spec["quotes"]),
        executed_pool_fn=executed_pool_fn_for(call, entry_tx_fn, chain=chain), canary=canary,
        max_range=spec["max_range"], chain=chain, v4_manager=manager,
        v4_ids_fn=lambda mint: _stored_pools_for_mint(chain, mint, require_complete=True), block_s=spec["block_s"],
        canary_v4=canary_v4)
    # Unpaced on purpose: it runs on the exit sweep, so it may not wait in a queue. One call per fire
    # attempt; a 429 or timeout is None, then CONFIRM_COOLDOWN_S before the next ask. Built whole
    # before it is published, so no sweep can see a runtime without its stateful confirm.
    confirm_call = make_call(url, timeout_s=CONFIRM_TIMEOUT_S)
    _RUNTIME[chain] = {
        "call": call, "canary": canary, "poller": poller, "v4_manager": manager,
        "confirm_to_latest": True, "confirm_call": confirm_call,
        "confirm": confirm_logs_fn(confirm_call, canary, v4_manager=manager, to_latest=True,
                                   cooldown_s=CONFIRM_COOLDOWN_S, canary_v4=canary_v4)}

    def run():
        logged = time.monotonic()
        while True:
            try:
                poller.tick(now_ts=time.time())
            except Exception:
                logger.exception("chain-%s poll: tick failed", chain)
                poller._blind()
            if time.monotonic() - logged >= 60.0 and stats:
                ms = sorted(stats)
                stats.clear()
                logger.warning("chain-%s volume poll: %d calls in the last minute, wall ms p50 %.0f "
                               "p90 %.0f (pacer wait included)", chain, len(ms), ms[len(ms) // 2],
                               ms[min(len(ms) - 1, int(len(ms) * 0.9))])
                logged = time.monotonic()
            time.sleep(float(spec["poll_s"]))
    thread_factory(target=run, name="memecoin-chain%s-volume-poll" % chain, daemon=True).start()
    say("[memecoin] chain-%s volume poll: started on %s (canary %s, poll %.0fs)"
        % (chain, urlparse(url).hostname, canary, float(spec["poll_s"])))
    return True
