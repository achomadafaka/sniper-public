"""Uniswap V4 quote + Universal Router execution for DexScreener-blind chains (2026-07-17).

Chain 4663 launches most fresh micros on V4 singleton pools AGAINST NATIVE ETH (verified live:
SPACEHOOD PoolKey currency0=0x0). Addresses from developers.uniswap.org/contracts/v4/deployments,
cross-validated on-chain (PoolManager matched the Swap-event emitter of a live trade; V4Quoter
returned a sane quote for the resolved PoolKey).

Shapes returned match the 0x/zerox contract ({sellAmount, buyAmount, transaction, issues}) so the
existing executors orchestrate them unchanged, with one addition: `pre_transactions` (BUYS unwrap
WETH bankroll to native first — the pools want native, our bankroll is WETH ERC-20).

BUY  (WETH bankroll -> token): pre-tx WETH.withdraw(amount); UR [V4_SWAP] with tx value=amount.
     No approvals, no Permit2 — native input has no allowance concept.
SELL (token -> WETH): UR [PERMIT2_PERMIT, V4_SWAP, WRAP_ETH]: permit the token to UR via a signed
     EIP-712 Permit2 message (the ERC20->Permit2 exact approve is the executor's existing allowance
     flow via issues.allowance.spender), swap token->native taken to the router, wrap the router's
     native balance back to WETH at us. Min-out is enforced INSIDE the swap params.

Fail-closed everywhere: no pool / no quote / no signer -> None. A wrong encoding reverts on-chain
(bounded gas cost), never a blind spend past amountOutMinimum."""
from __future__ import annotations

import os
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from eth_utils import keccak

from sniper.execution.evm_read_rpc import _rpc, eth_call_raw
from sniper.execution.evm_rpc_pool import RequestNotSent
from sniper.execution import evm_rpc_pool
from sniper.services.memecoin_evm_gas_topup import WETH_CALL_GAS_LIMIT

# Base (8453) addresses read 2026-09-06 from the Uniswap v4 deployments page
# (https://developers.uniswap.org/contracts/v4/deployments) and each confirmed by a non-empty
# `eth_getCode` on the chain owner's public endpoint https://mainnet.base.org: pool manager 24,009
# bytes, quoter 5,820, universal router 19,499, permit2 9,152. Permit2 is the same singleton
# address as on 4663, which is expected -- it is deployed deterministically on every chain.
V4 = {
    "4663": {
        "pool_manager": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
        "quoter": "0x8dc178efb8111bb0973dd9d722ebeff267c98f94",
        "universal_router": "0x8876789976decbfcbbbe364623c63652db8c0904",
        "permit2": "0x000000000022d473030f116ddee9f6b43ac78ba3",
        "gt_network": "robinhood",
    },
    # Ethereum mainnet (2026-09-30, eth-mainnet spec). Proved on chain, publicnode: pool manager
    # 24,009 bytes and == V4Quoter.poolManager(); quoter 5,820; universal router 19,499; permit2
    # 9,152 (the canonical Permit2, same as 8453). Cited from developers.uniswap.org v4 deployments.
    "1": {
        "pool_manager": "0x000000000004444c5dc75cb358380d2e3de08a90",
        "quoter": "0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203",
        "universal_router": "0x66a9893cc07d91d95644aedd05d03f95e1dba8af",
        "permit2": "0x000000000022d473030f116ddee9f6b43ac78ba3",
        "gt_network": "eth",
    },
    "8453": {
        "pool_manager": "0x498581ff718922c3f8e6a244956af099b2652b2b",
        "quoter": "0x0d5e0f971ed27fbff6c2837bf31316121532048d",
        "universal_router": "0x6ff5693b99212da76ad316178a184ab56d299b43",
        "permit2": "0x000000000022d473030f116ddee9f6b43ac78ba3",
        "gt_network": "base",
    },
    # Arc (5042, 2026-09-16). Read from developers.uniswap.org/contracts/v4/deployments, which
    # lists Arc explicitly. pool_manager and quoter are BYTE-IDENTICAL to the 4663/8453 entries
    # above (Uniswap's V4 core deploys deterministically); universal_router and permit2 verified
    # separately. Every address confirmed by a non-empty eth_getCode on the operator's dedicated
    # Arc RPC (QuickNode): pool manager 48,020 bytes, quoter 12,238, router 49,094, permit2
    # 18,306. No "gt_network": GeckoTerminal does not list Arc, and Arc is not DexScreener-blind
    # so it needs no fallback source (see _gt_pools_ranked's .get() below).
    "5042": {
        "pool_manager": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
        "quoter": "0x8dc178efb8111bb0973dd9d722ebeff267c98f94",
        "universal_router": "0x4fca4a51ab4f23a7447b3284fbd7d73289a89fb1",
        "permit2": "0x000000000022d473030f116ddee9f6b43ac78ba3",
    },
}

_NATIVE = "0x" + "00" * 20
# V4 fee units are hundredths of a bip: 1_000_000 == 100%. 0x800000 is the DYNAMIC-FEE sentinel,
# where a hook prices each swap, and it must stay allowed — the real Pons pools use fee=0 with a
# hook, which is the same arrangement expressed the other way.
_V4_DYNAMIC_FEE_FLAG = 0x800000
# POLICY CEILING, not a protocol one. V4 lets a pool set any fee up to 100%, and chain 4663 has
# pools that do: alongside the genuine pool for 0xb2a02fc6… sit three more at 89.00%, 93.33% and
# 92.06%, measured 2026-08-30. Their round trips are -99.58%, -99.88% and -99.92% against the real
# pool's -6.28%. They are not pools, they are traps, and the router's "pick the largest
# amount_out" rule is no defence: it protects the BUY only while the honest pool happens to quote
# best, and on one live exit run the sell fell through to a trap and returned 0.6% of the bag.
# Our own V3 tiers top out at 10000 (1%), so 10% is already an order of magnitude of headroom.
_V4_MAX_FEE = 100_000
_CMD_PERMIT2_PERMIT, _CMD_WRAP_ETH, _CMD_V4_SWAP = 0x0A, 0x0B, 0x10
_CMD_PERMIT2_TRANSFER_FROM, _CMD_UNWRAP_WETH = 0x02, 0x0C
_ACT_SWAP_IN_SINGLE, _ACT_SETTLE_ALL, _ACT_TAKE, _ACT_TAKE_ALL = 0x06, 0x0C, 0x0E, 0x0F
_ACT_SWAP_IN, _ACT_SETTLE = 0x07, 0x0B
# A HOOKED POOL SWAP COSTS MORE THAN 700k (2026-09-04). Every successful buy through the
# 0x4e3468951d launchpad hook used 752k-834k on chain and quoted 874k in eth_estimateGas;
# FROGE 50786 burned 899,687 of a 900,000 limit and reverted. 700k is a guaranteed out-of-gas.
_HOOKED_POOL_GAS = "1200000"
_ZERO_ADDRESS = "0x" + "00" * 20
_MSG_SENDER, _ADDRESS_THIS = 1, 2
_CONTRACT_BALANCE = 1 << 255
_SEL_UR_EXECUTE = "3593564c"                                   # execute(bytes,bytes[],uint256)
_SEL_WETH_WITHDRAW = "2e1a7d4d"
_SEL_P2_ALLOWANCE = keccak(b"allowance(address,address,address)")[:4].hex()
_SEL_QUOTE_SINGLE = keccak(
    b"quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))")[:4].hex()
_INIT_TOPIC = "0x" + keccak(
    b"Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)").hex()
_V4_DISCOVERY_LOOKBACK_BLOCKS = 8192
# Backward scan when the first read misses. Page width 100,000 blocks: Alpha measured this node
# serving a 100k range in 773ms with 2,428 logs, so it is a width the endpoint demonstrably
# handles. Six pages reaches ~608k blocks, past the 558k that covers the p90 coin age (15.67h) of
# the population we actually quote. Both env-settable so they can be tuned without a code change;
# a page width of 0 disables the scan entirely and restores the previous behaviour.
_V4_KEY_SCAN_PAGE_BLOCKS = int(os.getenv("MEMECOIN_V4_KEY_SCAN_PAGE_BLOCKS", "100000"))
_V4_KEY_SCAN_MAX_PAGES = int(os.getenv("MEMECOIN_V4_KEY_SCAN_MAX_PAGES", "6"))
# THE PAGE WIDTH IS A PROPERTY OF THE ENDPOINT, NOT OF THIS CODE (2026-09-06). Base's chain-owner
# endpoints refuse any `eth_getLogs` range over 10,000 blocks -- HTTP 413, `-32614 "eth_getLogs is
# limited to a 10,000 range"` -- so a 100,000-block page is refused on the FIRST page, the scan
# `break`s (a refused page says nothing about the range below it), and the unbounded `0x0`->`latest`
# fallback is refused for the same reason. Measured: the V4 rung returned `poolkey_read_failed` on
# 10 of 10 recent Base coins, every one of them older than the 8,192-block first read.
#
# 10,000 Base blocks is ~5.5 HOURS at 2s blocks, so six pages reach ~33h -- MORE calendar coverage
# than 4663's 608k blocks buy at 0.10s (~17h), and each page is cheaper: 10k measured at 1.3s and
# 1k at 221ms on mainnet.base.org.
# ARC accepts 10,000 blocks inclusive. The scan includes both ends of each span.
#
# ⚠️ BASE TIGHTENED AGAIN, 10,000 -> 2,000 (measured 2026-09-23 on https://mainnet.base.org: 2,000
# blocks answers in 305 ms with 245 logs, 2,001 answers HTTP 413 `-32614 "eth_getLogs is limited to
# a 2,000 range"`). That is the SECOND tightening this month, so `_shrink_page_blocks` below now
# believes the number the endpoint names instead of waiting for someone to notice. At 2 s blocks
# six pages reach ~6.7 h of Base chain rather than the ~33 h the 10,000 width claimed -- a claim
# that had been false since the provider moved, because every page was being refused outright.
_V4_KEY_SCAN_PAGE_BLOCKS_BY_CHAIN = {"1": 10000, "8453": 2000, "5042": 9999}
# The endpoint's own words, e.g. `eth_getLogs is limited to a 2,000 range`. Commas optional.
# Infura: `range 100000 exceeds limit of 10000` (HALCYON ref 5006a440).
_RANGE_LIMIT_RE = re.compile(
    r"(?:limited to a ([\d,]+) range|range [\d,]+ exceeds limit of ([\d,]+))", re.IGNORECASE)
_MIN_LEARNED_PAGE_BLOCKS = 100


def _learn_range_limit(chain, response, current):
    """Shrink this chain's page width to the limit a refusal NAMES, or None when it names none.

    The provider tells us the answer in the error body; not reading it cost a day of blind Base
    discovery on 2026-09-23 and would cost another on the next tightening. Bounded on purpose: a
    limit is believed only as an int in [100, current), so a malformed or hostile message cannot
    shrink the scan to a handful of blocks, and a WIDER claim is ignored because the refusal we are
    holding proves the wider range does not work.
    """
    error = (response or {}).get("error") if isinstance(response, dict) else None
    if not isinstance(error, dict):
        return None
    match = _RANGE_LIMIT_RE.search(str(error.get("message") or ""))
    if not match:
        return None
    try:
        learned = int((match.group(1) or match.group(2)).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not (_MIN_LEARNED_PAGE_BLOCKS <= learned < int(current)):
        return None
    _V4_KEY_SCAN_PAGE_BLOCKS_BY_CHAIN[str(chain)] = learned
    import logging                          # local: this module has no logging surface of its own
    logging.getLogger("memecoin").warning(
        "v4 pool-key scan: chain %s narrowed the getLogs range to %d blocks", chain, learned)
    return learned


def _key_scan_page_blocks(chain):
    """The `eth_getLogs` page width this chain's endpoints will actually serve."""
    raw = os.getenv("MEMECOIN_V4_KEY_SCAN_PAGE_BLOCKS_%s" % chain)
    if raw and str(raw).strip():
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return _V4_KEY_SCAN_PAGE_BLOCKS_BY_CHAIN.get(str(chain), _V4_KEY_SCAN_PAGE_BLOCKS)

# PROVIDER BLINDNESS (2026-08-13). Every read below fail-closes to None, which makes an RPC 429
# indistinguishable from "this token has no V4 pool" — and the entry path then reports `no_route`,
# i.e. proof about the coin, from what was only an infrastructure outage. `classify_quote_miss`
# already prefers a blind rung over no_route, but it could only SEE the GeckoTerminal cooldown;
# a chain-RPC 429 was swallowed here. Thread-local because the fast lane quotes on its own thread.
_blind = threading.local()


def _mark_blind(reason):
    _blind.reason = getattr(_blind, "reason", None) or reason


def begin_quote():
    """Clear the blindness flag at the start of one quote attempt."""
    _blind.reason = None
    _blind.discovery_observed = False
    _blind.v4_ranking = None


def last_provider_blind():
    """The provider failure that made the most recent quote attempt blind, or None if it saw
    everything it asked for. A None return means a no-pool answer can be trusted."""
    return getattr(_blind, "reason", None)


def last_discovery_observed():
    """True when a V4 pool source answered during the current quote attempt."""
    return bool(getattr(_blind, "discovery_observed", False))


def take_v4_ranking(mint):
    """The ranking record behind this thread's last discovery of `mint`, consumed once; None when
    there is none or it belongs to another mint (a stale record is never handed on).

    E10 phase 1 (2026-09-29, observation only): an uncached discovery builds one record with every
    candidate's GeckoTerminal, DexScreener and own-tape depth, where its id came from, and the cut;
    a cache hit hands on the record that built the cached list, marked `cached`. `dex_v4_quote`
    attaches it to the quote it returns, so the entry trace and a full exit's timing can say whether
    the tape's top pool is the pool the quote was won on. It changes no pick."""
    rec = getattr(_blind, "v4_ranking", None)
    if rec is None or rec.get("mint") != str(mint).lower():
        return None
    _blind.v4_ranking = None
    return rec


_pool_cache = {}      # (chain, mint) -> (expires, [pool_id, ...])
_ranking_cache = {}   # (chain, mint) -> the E10 ranking record that built the cached list
_key_cache = {}       # (chain, pool_id) -> poolkey dict
_reserve_cache = {}   # (chain, mint) -> (deepest-pool reserve_in_usd, monotonic ts)


def top_pool_reserve_usd(chain, mint, *, max_age_s=1800.0):
    """The deepest pool's reserve_in_usd (LIQUIDITY) for a mint, captured for FREE from the same GT
    discovery call the router already makes (_gt_pools_for_mint reads reserve_in_usd to rank pools,
    then discarded it). None if never discovered or older than max_age_s. This is the only reliable
    4663 liquidity source that costs no extra GT call — the web can't win GT's rate limit and V4's
    concentrated liquidity isn't summable on-chain. Fresh whenever the runner has routed the token."""
    hit = _reserve_cache.get((str(chain), str(mint).lower()))
    if not hit:
        return None
    reserve, ts = hit
    if reserve is None or reserve <= 0:
        return None
    if time.monotonic() - ts > float(max_age_s):
        return None
    return reserve


def _w(x):
    return format(int(x), "064x")


def _wa(a):
    return str(a).lower().replace("0x", "").rjust(64, "0")


def _default_http(url, *, http=None):
    """GT GET through the SHARED GeckoTerminal rate guard (cooldown + single-flight gate) —
    raw urllib here bypassed the guard, so the marks path's 429 cooldowns intermittently starved
    pool discovery while these calls never registered with the guard (2026-07-17). Returns the
    parsed JSON dict or None (the caller treats None as 'no pools this attempt', uncached)."""
    from sniper.execution.geckoterminal_quote import gt_get_json
    return gt_get_json(url, http=http)


def _is_v4_pool_id(pid):
    """A V4 pool id is the keccak of its PoolKey: 32 bytes, 66 chars with the 0x.

    THIS REPLACED A DEX-NAME MATCH (2026-08-30) and the distinction is the whole fix. Discovery
    used to keep a pool only when "v4" appeared in the provider's `dex` attribution string. On
    chain 4663 GeckoTerminal attributes pools on the PoolManager WE ARE CONFIGURED FOR to
    `pons-v2-dex` / `pons-v2`, so every one of them was discarded and the mint resolved to zero
    pools -> `no_route` on coins that were tradeable. Verified end to end: pool
    0x08c72ca0…df9725c (labelled `pons-v2-dex`, $26.7k reserve) resolves its Initialize event on
    our own PoolManager 0x8366a39c… and the V4Quoter quotes it BOTH ways — 0.02 ETH in for
    570,280 tokens, 1,000,000 tokens out for 0.03267 ETH.

    A name is a provider's opinion and can only recognise venues someone thought to name. The id
    SHAPE is structural, and `_pool_key` resolving the Initialize event against our CONFIGURED
    PoolManager is the real cross-validation: a pool on a foreign manager emits no such event
    there, returns None, and fails closed with no string for anyone to get wrong.

    A 42-char id is a plain pair address, not a V4 pool — Pons runs a V2-style lane too, and its
    pairs carry a NON-STANDARD ABI our V2 rung cannot read (2-word `getReserves()` instead of the
    canonical `(uint112,uint112,uint32)`; `token0()`/`token1()`/`fee()` revert). Those stay
    refused here on purpose; routing them is a separate decision with its own exit evidence.
    """
    return isinstance(pid, str) and pid.startswith("0x") and len(pid) == 66


def _provider_pair_possible(chain, mint, pair_token, currencies):
    """Prune only a known wrong pair; unknown provider metadata still needs PoolKey proof."""
    if pair_token is None:
        return True
    values = [str(value or "").lower() for value in (*currencies, mint, pair_token)]
    if any(not re.fullmatch(r"0x[0-9a-f]{40}", value) for value in values):
        return True
    from sniper.execution.evm_wallet import CHAIN_QUOTE, native_is_quote
    if not native_is_quote(chain):
        wrapped = str((CHAIN_QUOTE.get(str(chain)) or {}).get("token") or "").lower()
        # Indexers call native V4 currency WETH; the on-chain pair check remains exact.
        values = [_NATIVE if value == wrapped else value for value in values]
    return sorted(values[:2]) == sorted(values[2:])


def _gt_pools_ranked(chain, mint, gt_http=None, *, request_http=None, pair_token=None):
    """GeckoTerminal discovery -> (list of (reserve_usd, pool_id), provider_failed).

    A chain with no "gt_network" entry (Arc: GT lists no such network) is not a FAILED read --
    the source simply does not apply here, the same as an empty answer from a source that DID
    run. `_pools_for_mint` only calls a chain "blind" when every source failed; a chain that
    never had GT coverage must not read as blind on that account.
    """
    net = V4[str(chain)].get("gt_network")
    if not net:
        return [], False
    try:
        url = f"https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{mint}/pools"
        if gt_http is not None:
            j = gt_http(url)
        elif request_http is not None:
            j = _default_http(url, http=request_http)
        else:
            j = _default_http(url)
        if j is None:
            return [], True                # rate-guarded / failed — say so, do not call it empty
        out = []
        for p in j.get("data", []):
            pid = str(p.get("id", "")).split("_")[-1]
            if not _is_v4_pool_id(pid):
                continue
            # THE PAIR COMES FREE IN THIS RESPONSE and is what makes the key derivable without a
            # log read. `relationships.{base,quote}_token.data.id` is "<network>_<address>".
            rel = p.get("relationships") or {}
            currencies = []
            for side in ("base_token", "quote_token"):
                try:
                    ident = str(((rel.get(side) or {}).get("data") or {}).get("id") or "")
                except (AttributeError, TypeError):
                    ident = ""
                currencies.append(ident.rsplit("_", 1)[-1].lower())
            if not _provider_pair_possible(chain, mint, pair_token, currencies):
                continue
            try:
                reserve = float(p.get("attributes", {}).get("reserve_in_usd") or 0)
            except (TypeError, ValueError):
                reserve = 0.0
            for other in currencies:
                if other and other != str(mint).lower():
                    _note_pair(chain, pid, other)
            out.append((reserve, pid))
        return out, False
    except RequestNotSent:
        raise
    except Exception:
        return [], True


def _ds_pools_ranked(chain, mint, ds_http=None, pair_token=None, *, request_http=None, funded=True):
    """DexScreener discovery -> (list of (reserve_usd, pool_id), provider_failed).

    A SECOND, INDEPENDENT source, unioned with GeckoTerminal rather than replacing it, because
    each has a blind spot the other covers (measured 2026-08-30 on the two coins that stranded the
    4663 bankroll): DexScreener returned four correctly-labelled V4 pools for
    0xb2a02fc6… while GeckoTerminal mis-attributed them, and GeckoTerminal indexed
    0xd0f62b2d…'s pool at $20,385 while DexScreener returned nothing for it at all. Sourcing from
    either one alone loses coverage.

    It also spreads load: GeckoTerminal is the SHARED rate-guarded provider and threw
    `provider_rate_limit` three times in the same 17-minute window these entries failed in.

    Filtered on id shape exactly like the GT half — never on `labels`, which is the same
    name-matching mistake one provider along.
    """
    from sniper.execution.solana_quote import (
        _DS_TIMEOUT_S, _ds_start_cooldown, _http as _ds_default_http,
        dexscreener_cooldown_active, FOMO_TO_DEX, norm_mint)
    want_chain = FOMO_TO_DEX.get(str(chain))
    if not want_chain:
        return [], False                   # chain not indexed by DexScreener: absence, not failure
    # THROUGH THE SHARED BREAKER, never raw. Discovery runs on the funded route path, so it reads
    # and trips the FUNDED cell. Calling urllib directly here would reproduce the 2026-07-17 bug
    # the GeckoTerminal half already carries a comment about: a provider guard that the busiest
    # caller silently bypasses protects nothing, and the bypassing caller never learns it is down.
    # funded=False (the bridge memo's background refresh, Codex F6): the SHARED cell is read and
    # booked, so a background failure never suppresses funded discovery.
    if ds_http is None and dexscreener_cooldown_active(funded=funded):
        return [], True                    # provider suppressed -> blindness, not "no pools"
    try:
        http = ds_http or (
            lambda url: (request_http or _ds_default_http)("GET", url, timeout=_DS_TIMEOUT_S)[1])
        j = http("https://api.dexscreener.com/latest/dex/tokens/" + str(mint))
        if not isinstance(j, dict):
            return [], True
        wanted = norm_mint(str(mint))
        out = []
        for pair in (j.get("pairs") or []):
            if not isinstance(pair, dict) or pair.get("chainId") != want_chain:
                continue
            currencies = [str(value.get("address") or "") if isinstance(value, dict) else ""
                          for value in (pair.get("baseToken"), pair.get("quoteToken"))]
            if pair_token is None and norm_mint(currencies[0]) != wanted:
                continue
            if not _provider_pair_possible(chain, mint, pair_token, currencies):
                continue
            pid = str(pair.get("pairAddress") or "").lower()
            if not _is_v4_pool_id(pid):
                continue
            try:
                reserve = float(((pair.get("liquidity") or {}).get("usd")) or 0)
            except (TypeError, ValueError):
                reserve = 0.0
            for other in currencies:
                if other.lower() != wanted:
                    _note_pair(chain, pid, other)
            out.append((reserve, pid))
        return sorted(out, reverse=True), False
    except RequestNotSent:
        raise
    except Exception as exc:
        if ds_http is None:
            _ds_start_cooldown(funded=funded, reason=type(exc).__name__)
        return [], True


def _onchain_pools_for_mint(chain, mint, rpc_url, http=None):
    """Return V4 pool ids from indexed Initialize currencies, or mark an RPC failure."""
    conf = V4.get(str(chain))
    raw_mint = str(mint).lower().removeprefix("0x")
    if not conf or not rpc_url or len(raw_mint) != 40:
        return [], False
    try:
        int(raw_mint, 16)
    except ValueError:
        return [], False
    mint_topic = "0x" + raw_mint.rjust(64, "0")

    def _scan(url, from_block, to_block, scan_http=http):
        found = set()
        for topics in ((_INIT_TOPIC, None, mint_topic),
                       (_INIT_TOPIC, None, None, mint_topic)):
            logs = _rpc(url, "eth_getLogs", [{
                "address": conf["pool_manager"], "topics": list(topics),
                "fromBlock": from_block, "toBlock": to_block,
            }], http=scan_http)["result"]
            for log in logs:
                event_topics = log.get("topics") or []
                pool_id = str(event_topics[1]).lower() if len(event_topics) > 1 else ""
                if _is_v4_pool_id(pool_id):
                    found.add(pool_id)
        return found

    def _head(url):
        return int(str(_rpc(url, "eth_blockNumber", [], http=http)["result"]), 0)

    # PRIMARY, THEN THE IDENTITY-CHECKED READ FALLBACKS (2026-09-03). This scan is the ONLY source
    # that can see a pool younger than the public indexers' lag, so its failure is the whole
    # difference between "no route" and "could not look". Same helper `_pool_key` already uses,
    # same eth_chainId proof before any read; never a quote, sign or broadcast path.
    #
    # EVIDENCE, CORRECTED. The commit that added this cited "the funded endpoint threw HTTPError
    # three times running" from a live probe. That probe issued ~1,216 eth_getLogs against the
    # funded key in four minutes (~5/s), so those errors were almost certainly its own rate
    # limiting, not an organic endpoint fault; the same burst likely 429'd the next deploy gate's
    # custody probe. What stands without that: ANY (attempt 7d4f6f4f) was refused `no_route` at
    # 14:54:21Z on a pool initialised at 14:54:03Z, before any probe ran, and the index-lag
    # numbers (Initialize visible 38-61s after the block on most launches) come from the public
    # endpoint's own detection timing. A read fallback is still right; the funded key was not
    # shown to be broken.
    last_exc = None
    verified = []
    # NARROW WINDOW FIRST: a pool minted seconds ago answers here without a wide read.
    #
    # IT MAY USE THE WHOLE READ POOL, AND THE FULL RANGE BELOW MAY NOT (2026-09-08). This scan ran
    # single-homed: `_archive_rpc_urls` dedupes (primary, *archive) and chain 4663 configures ONE
    # archive endpoint, the same host as the primary — so the one source that can see a pool
    # younger than the indexer lag had no fallback at all, while a three-endpoint read pool sat
    # configured and healthy beside it. On 2026-09-08 that host failed for about fifty seconds and
    # took SIX consecutive attempts on 0x894917b3 with it (17:37:23 through 17:38:13, every one
    # `discovery_rpc_failed`, no position opened).
    #
    # The window is `_V4_DISCOVERY_LOOKBACK_BLOCKS` = 8192 blocks, THIRTEEN MINUTES on 4663, which
    # any healthy full node serves. The full-range `0x0`->`latest` scan below is different and
    # stays archive-only: a pruned node would answer it with a SHORT list rather than an error, and
    # a short list reads as proof of absence. `verified` therefore collects archive candidates
    # only, and the extra read endpoints are used for the window and then dropped.
    from sniper.execution.evm_rpc_pool import pool_for as _rpc_pool_for
    _archive = _archive_rpc_urls(chain, rpc_url)
    try:
        _window_urls = tuple(dict.fromkeys((*_archive, *(_rpc_pool_for(rpc_url) or ()))))
    except Exception:
        _window_urls = _archive          # a pool lookup failure must not cost us the primary
    for url in _window_urls:
        if url != rpc_url and not _archive_is_chain(url, chain, http=http):
            continue
        if url in _archive:
            verified.append(url)         # only these may serve the full-range read
        try:
            head = _head(url)
            span = _V4_DISCOVERY_LOOKBACK_BLOCKS
            page_width = _key_scan_page_blocks(chain)
            if page_width > 0:
                span = min(span, page_width)
            found = _scan(url, hex(max(0, head - span)), hex(head))
        except Exception as exc:
            last_exc = exc
            continue
        if found:
            return sorted(found), False
    # FULL RANGE WHEN THE WINDOW FOUND NOTHING (2026-09-03). The window is
    # `_V4_DISCOVERY_LOOKBACK_BLOCKS` = 8192 blocks, which on 4663 is THIRTEEN MINUTES, and unlike
    # `_pool_key` this scan had no wider fallback. An empty window was therefore reported as "no
    # pool" for every coin older than that whose mint the public indexers had not listed yet, and
    # the entry was refused as unroutable.
    #
    # MEASURED 2026-09-03 on four coins refused that day: a full-range read on this same
    # PoolManager found 7, 4, 7 and 17 pools where the window found ZERO, in about 160 ms. Both
    # currencies are INDEXED event topics, so this is a topic lookup and not a chain walk, and
    # `_pool_key` has relied on the identical `0x0`->`latest` read for months.
    #
    # It runs ONLY after the window found nothing, which is exactly the path that used to end in a
    # refusal, so no filling entry pays for it. An empty full-range answer is then real evidence of
    # absence, and a raising one still reports blind.
    for url in verified:
        # THE FULL-RANGE REFUSAL MEMORY APPLIES HERE TOO (2026-10-07): chain 1 re-asked Infura's
        # refused 0x0->latest on every ladder pass. Through _KeyScanHttp a refusal is remembered and
        # a remembered member is never sent the shape; with none left the read raises, so the
        # result stays blind (rpc_failed), never absent.
        try:
            return sorted(_scan(url, "0x0", "latest", scan_http=_KeyScanHttp(http, True))), False
        except Exception as exc:
            last_exc = exc
            continue
    return [], last_exc is not None


def _pools_for_mint(chain, mint, gt_http=None, ds_http=None, *, publication_fn=None):
    """Top V4 pool ids for a mint, unioned across GeckoTerminal and DexScreener (10-min cache).

    BLINDNESS IS ONLY WHEN BOTH SOURCES FAILED. One provider answering is enough to trust a
    no-pool result, which is the property `last_provider_blind` exists to report: a guard refusal
    or transport failure is never cached, because the next attempt may succeed.
    """
    key = (str(chain), mint.lower())
    now = time.time()
    hit = _pool_cache.get(key)
    if hit and hit[0] > now:
        _blind.discovery_observed = True
        # The list was built by another discovery (a prewarm, an earlier signal, the exit sweep's
        # warm-up), maybe on another thread: hand on ITS ranking, which is what this quote uses.
        rec = _ranking_cache.get(key)
        _blind.v4_ranking = dict(rec, cached=True) if rec is not None else None
        return hit[1]

    gt_pools, gt_failed = _gt_pools_ranked(chain, mint, gt_http=gt_http)
    # E10 (2026-09-29, DexScreener out, per chain): single-hop discovery only. The multihop's legs
    # keep asking until phase 1 records them (Alpha: v4_multihop won 23 quotes on 4663). Switched
    # off reads exactly like the open breaker: a source that did not answer, never "no pools".
    from sniper.settings.memecoin import dexscreener_off
    e10_off = dexscreener_off("E10", chain)
    if e10_off:
        from sniper.execution.solana_quote import _CONSUMER_SUPPRESSED, _count_consumer
        _count_consumer(_CONSUMER_SUPPRESSED, "E10:%s" % chain)
        ds_pools, ds_failed = [], True
    else:
        ds_pools, ds_failed = _ds_pools_ranked(chain, mint, ds_http=ds_http)
    merged = {}                            # pool_id -> deepest reserve either source reported
    for reserve, pid in list(gt_pools) + list(ds_pools):
        if pid not in merged or reserve > merged[pid]:
            merged[pid] = reserve
    # PER SOURCE, for the E10 ranking record and the E10 cut (2026-09-29). `merged` above keeps
    # today's union untouched; these only remember who said what.
    gt_depth, ds_depth, src = {}, {}, {}
    for depths, pools, name in ((gt_depth, gt_pools, "gt"), (ds_depth, ds_pools, "ds")):
        for reserve, pid in pools:
            depths[pid] = max(reserve, depths.get(pid, reserve))
            src.setdefault(pid, name)
    # THE LAUNCHPAD ANSWERS BEFORE ANY INDEX CAN (2026-09-05). A coin created by a registered
    # launchpad has its real PoolKey readable from that contract with one `eth_call`, so this is
    # the only source that can name a pool the log index will not surface for another 38-61
    # seconds -- which is exactly the window the first feed signal arrives in. The derived key is
    # confirmed by a probe quote before it is trusted, and priming `_key_cache` here means the
    # ranking below sees a KNOWN sane fee rather than an unknown one.
    derived_pending, confirmed_proofs = False, []
    _derived_rpc = getattr(_blind, "discovery_rpc_url", None)
    if _derived_rpc:
        try:
            from sniper.execution.evm_v4_launchpad import derived_pool_keys
            # NOTE the loop names: `key` above is this function's CACHE key and must not be
            # rebound here -- doing so replaced a tuple with a dict and raised `unhashable type`
            # at the reserve-cache write further down.
            for pid, derived_key, confirmed in derived_pool_keys(
                    chain, mint, _derived_rpc, http=getattr(_blind, "discovery_http", None)):
                if not confirmed:
                    derived_pending = True  # not initialised yet: the requote covers those seconds
                    continue
                if publication_fn is not None:
                    # Background readiness requires the exact hash/currency identity too. The
                    # default discovery path keeps its existing proof and provider behavior.
                    expected = "0x" + keccak(bytes.fromhex(_encode_pool_key(derived_key))).hex()
                    if (expected != str(pid).lower()
                            or str(mint).lower() not in (
                                str(derived_key.get("currency0", "")).lower(),
                                str(derived_key.get("currency1", "")).lower())):
                        raise RequestNotSent("background derived pool identity mismatch")
                    confirmed_proofs.append((pid, dict(derived_key)))
                _key_cache[(str(chain), pid.lower())] = derived_key
                if pid not in merged:
                    merged[pid] = 0.0
                    src.setdefault(pid, "derived")
        except RequestNotSent:
            if publication_fn is not None:
                raise
        except Exception:
            import logging                  # local: this module has no logging surface of its own
            logging.getLogger("memecoin").debug(
                "launchpad discovery unavailable for %s", mint, exc_info=True)
    # Fresh 4663 pools emit Initialize before public indexers list the mint. Both currencies are
    # indexed event topics, so this exact PoolManager query finds the pool without a chain scan.
    rpc_url = getattr(_blind, "discovery_rpc_url", None)
    http = getattr(_blind, "discovery_http", None)
    rpc_failed = False
    cache_added = False
    depth_known = bool(merged)          # a reserve came from GeckoTerminal or DexScreener
    if not merged:
        # THE DURABLE CACHE ANSWERS BEFORE ANY PROVIDER (2026-09-03). Free, offline, ~2 ms, and it
        # already holds every pool this system has ever quoted.
        #
        # IT CARRIES NO DEPTH, and that has to be said out loud rather than encoded as 0.0. See
        # `depth_known` below: pretending a cached pool has zero reserve made the "deepest first"
        # sort fall through to comparing POOL IDS, so the four pools handed to the quoter became an
        # arbitrary four. DUMBMONEY on 2026-09-04 had nine pools, one with $28.5k and several under
        # $200; the buy quote came back 15.8% off the market price and the sell simulation reverted,
        # which the safety gate correctly refused -- on a coin that was being sold twelve times a
        # minute elsewhere.
        for pid in _stored_pools_for_mint(chain, mint):
            merged[pid] = 0.0
            src.setdefault(pid, "cache")
    else:
        # THE CACHE ALSO ANSWERS WHEN A PROVIDER ANSWERED BADLY (2026-09-04). Gating it on
        # `not merged` assumed a non-empty provider answer is a COMPLETE one. It is not: an indexer
        # can list a mint's trap pools and omit its only tradeable one, and then this function
        # returns a list on which every quote fails while the coin trades fine elsewhere.
        #
        # LARP (position 50682, $139.12 funded) is the case. GeckoTerminal returned its 81% and 80%
        # fee pools, so `merged` was non-empty, the cache was skipped, and the one sane pool --
        # 0x00e547781c06ab, native-paired at 10%, quoting the whole 1,377,521-token bag for 0.1508
        # ETH -- was never a candidate. The position logged UNSELLABLE for 60 consecutive sweeps
        # and its take-profit could not fire, while a standalone quote succeeded for the single
        # reason that a GeckoTerminal rate limit had emptied `merged` and let the cache through.
        #
        # Additive only, and it cannot displace a measured pool: cached ids enter at reserve 0.0,
        # so they rank BEHIND everything a provider actually priced, and `depth_known` is left
        # alone so the ranking and the candidate limit keep the meaning they already had. The read
        # is a two-index UNION on the local SQLite cache, ~0.06 ms.
        for pid in _stored_pools_for_mint(chain, mint):
            if pid not in merged:
                merged[pid] = 0.0
                cache_added = True
                src.setdefault(pid, "cache")
    if not merged:
        rpc_pools, rpc_failed = _onchain_pools_for_mint(chain, mint, rpc_url, http=http)
        for pid in rpc_pools:
            merged[pid] = 0.0
            src.setdefault(pid, "scan")
    elif rpc_url and not _has_sane_eth_pool(chain, merged):
        # YIELDPAD 2026-09-05 12:53: the indexers listed five fee-trap/dust pairs, so `merged` was
        # non-empty and this scan -- the only source that sees a pool younger than the indexer lag
        # -- was skipped. The real hooked ETH pool ($20k, fee 0) was indexed at 12:54:04, eleven
        # seconds after the refusal, and stored by us 68s later. A list with no known sane ETH pool
        # is as blind as an empty one: scan, and union what the chain knows.
        extra, _extra_failed = _onchain_pools_for_mint(chain, mint, rpc_url, http=http)
        for pid in extra:
            if pid not in merged:
                merged[pid] = 0.0
                cache_added = True
                src.setdefault(pid, "scan")
    if not merged and gt_failed and ds_failed and (rpc_failed or not rpc_url):
        # DISCOVERY blindness: GeckoTerminal, DexScreener and the on-chain scan all failed, so we
        # have no pool ids at all. Named distinctly from the PoolKey sites below — until 2026-09-02
        # all three wrote "rpc_transport" and the tape could not say which function produced a mark,
        # which is what stalled the 4663 route investigation.
        _mark_blind("discovery_all_blind" if rpc_failed else "rate_limit")
        return []
    if not merged and rpc_failed:
        # THE CHAIN SCAN FAILED AND NOTHING ELSE FOUND A POOL (2026-09-03). An indexer answering
        # "no pairs" is not proof for a pool younger than its lag, and the chain scan is the only
        # source that can see one. Until today this branch fell through: one live provider was
        # taken as proof of absence, the RPC failure was swallowed, and the miss went out as
        # `no_route` — terminal, un-retried, and handed to LI.FI. ANY (0xd7eec4ab, DegenCapitalLLC,
        # attempt 7d4f6f4f): pool 18s old, funded getLogs failing, refused as unroutable, quotable
        # at fee 0 minutes later. Blind, not absent; the provider requote re-enters on this code.
        _mark_blind("discovery_rpc_failed")
        return []
    if not merged:
        # EVERY SOURCE ANSWERED EMPTY. Still not proof for a fresh pool: a live probe of
        # PoolManager Initialize events today saw the log index surface pools bimodally — 2.1s,
        # 2.7s, 2.3s ... and 61.0s, 58.6s after the block, on BOTH endpoints. A wallet sniping at
        # +3s can be unfindable by any eth_getLogs for a minute. "Unproven" costs a genuinely
        # unroutable coin one extra ladder pass via the requote; "no_route" cost the entry.
        _mark_blind("discovery_unproven")
        return []
    _blind.discovery_observed = True
    # DERIVE EVERY UNKNOWN KEY BEFORE RANKING, from the pair the provider just gave us. This is
    # zero RPC and it pays twice: `_fee_rank` below can then tell an honest pool from a trap
    # instead of ranking it "unknown", and `_pool_key` finds the key in cache later without a log
    # read. A miss changes nothing — the pool keeps its unknown rank and the scan still runs.
    for pid in list(merged):
        if _pool_key_cached(chain, pid) is None:
            _derive_and_cache_pool_key(chain, pid, mint)
    own = _own_depths(chain, merged)

    # ONE LOOKUP PER POOL (Gamma 2026-09-29). A key not in memory costs `_stored_pool_key`, a new
    # read-only ledger connection (p50 2.06 ms) that is not memoized on not-found; today's sort, the
    # phase 2 cut and the ranking record share this per-call memo instead of paying it three times.
    fee_ranks = {}

    def _fee_rank(pid):
        if pid not in fee_ranks:
            key = _pool_key_cached(chain, pid)
            # unknown fee: behind proven-sane, ahead of proven-trap
            fee_ranks[pid] = 1 if key is None else (0 if _fee_is_sane(key.get("fee")) else 2)
        return fee_ranks[pid]
    # THE PHASE 2 CUT, computed on every discovery: it IS the cut with E10 off, and with E10 on it
    # is recorded beside today's so the read never has to replay the rule.
    e10_cut, e10_cap = _e10_cut(merged, own, gt_depth, _fee_rank)
    reserve_record = None
    if e10_off:
        pools, _cap = e10_cut, e10_cap
        if gt_depth:                       # only GeckoTerminal's TVL means what readers expect
            reserve_record = (max(gt_depth.values()), time.monotonic())
    else:
        ranked = sorted(((r, pid) for pid, r in merged.items()), reverse=True)
        if depth_known:
            # Only a MEASURED reserve may be published as this mint's liquidity. Writing a synthetic
            # 0.0 here also overwrote a real reading taken minutes earlier, turning a known
            # liquidity into an unknown one for everything downstream.
            reserve_record = (ranked[0][0], time.monotonic())   # deepest pool's reserve
        # WITHOUT DEPTH, TAKE MORE POOLS RATHER THAN GUESS FOUR. The quoter quotes each candidate
        # and keeps the best output, so a wider list costs quote calls and never a worse fill,
        # while a narrow arbitrary list can miss the only pool worth trading. Depth-ranked stays at
        # four. The cap widens when the cache contributed: cached ids rank behind every measured
        # pool, so a 4-deep cut would drop exactly the candidates that were added to be tried.
        # A POOL THAT CANNOT BE USED MUST NOT CONSUME A CANDIDATE SLOT (2026-09-04). The cap used
        # to be applied to the raw ranking, so fee-traps ate the budget and honest pools were
        # truncated away before the quoter ever saw them. SEND (0x6909eb6f) had 20 known pools
        # including native-paired ones at 0.9% and 10%; the 8 candidates handed to the quoter
        # contained FIVE traps at 14.8-20% and ZERO sane native pools, and the entry refused
        # `no_route_all_venues` while a 0.9% pool sat in our own cache.
        #
        # `_fee_is_sane` is the same screen the quoter applies a moment later, so this reorders
        # work we were already going to do -- it can only change WHICH pools fit in the budget,
        # never widen what is accepted. A pool whose key is not cached yet sorts with the unknowns
        # rather than being dropped, so nothing is lost when the cache is cold; the fee screen
        # still runs in the quoter for every one of them.
        _cap = 4 if (depth_known and not cache_added) else 8
        ranked = sorted(ranked, key=lambda rp: (_fee_rank(rp[1]), -rp[0]))
        pools = [pid for _, pid in ranked[:_cap]]
    # CAPPED (Alpha 2026-09-29): the record rides in every entry trace and exit_timing_json, and a
    # mint can have thousands of stored pools. Keep both cuts and the top 8 of each source.
    keep = set(pools) | set(e10_cut)
    for depths in (gt_depth, ds_depth, {p: v for p, v in own.items() if v is not None}):
        keep.update(sorted(depths, key=lambda p: -depths[p])[:_RANKING_TOP_N])
    _blind.v4_ranking = {
        "chain": str(chain), "mint": mint.lower(), "e10_off": e10_off, "cap": _cap, "cached": False,
        "cut": list(pools), "e10_cut": list(e10_cut), "n_candidates": len(merged),
        "pools": {pid: {"gt": gt_depth.get(pid), "ds": ds_depth.get(pid), "own": own.get(pid),
                        "src": src.get(pid), "fee_rank": _fee_rank(pid)}
                  for pid in merged if pid in keep}}
    # ONLY A FOUND POOL IS CACHED. An empty answer used to be cached for 600s, which froze a
    # fresh coin as "no pool" for exactly the ten minutes in which its wallet was buying it.
    # A list still without a known sane ETH pool is provisional: keep it 30s, not 10 minutes, so
    # the next signal on the coin re-discovers instead of inheriting a trap-only list (PH 12:21 and
    # 12:28 shared one list).
    ttl = 600 if _has_sane_eth_pool(chain, pools) else 30
    if derived_pending:
        # The launchpad named a pool the chain has not initialised yet (YIELDPAD's signal came 12s
        # before its Initialize block). The requote runs within seconds; a 30s cache would hand it
        # this trap-only list until the pool was long open. Re-derive on the next look instead.
        ttl = min(ttl, 3)
    if publication_fn is not None:
        publication_fn(pools, now + ttl, confirmed_proofs)
    if reserve_record is not None:
        _reserve_cache[key] = reserve_record
    _ranking_cache[key] = _blind.v4_ranking
    _pool_cache[key] = (now + ttl, pools)             # publish the completed list last
    return pools


_OWN_DEPTH_MAX_POOLS = 64   # ponytail: ~0.03 ms a pool; a mint with more leaves the rest unknown
_RANKING_TOP_N = 8          # per source, in the E10 ranking record


def _e10_tier(pid, own, gt_depth):
    """E10's rank of one pool: tape depth > 0 (tier 0), else GeckoTerminal's reserve (1), else a tape
    ZERO (L = 0, no in-range liquidity: a real zero, 2), else unknown (3), as unknown ranks today.
    The tiers never compare an active-liquidity figure with a TVL figure."""
    if own.get(pid) is not None and own[pid] > 0:
        return 0, own[pid]
    if pid in gt_depth:
        return 1, gt_depth[pid]
    return (2, 0.0) if own.get(pid) is not None else (3, 0.0)


def _e10_cut(pids, own, gt_depth, fee_rank):
    """(cut, cap): the candidates `_pools_for_mint` hands the quoter with E10 off (2026-09-29): fee rank first
    (the quoter's own screen), then `_e10_tier`, deepest first within a tier. The cap keeps its
    rule: 4 only when every candidate's depth is known, so a stale tape widens it to 8."""
    tiers = {pid: _e10_tier(pid, own, gt_depth) for pid in pids}
    cap = 4 if all(t < 3 for t, _v in tiers.values()) else 8
    ranked = sorted(sorted(((v, pid) for pid, (_t, v) in tiers.items()), reverse=True),
                    key=lambda vp: (fee_rank(vp[1]), tiers[vp[1]][0], -vp[0]))
    return [pid for _v, pid in ranked[:cap]], cap


def _own_depths(chain, pool_ids):
    """{pool: own-tape USD virtual reserve or None}; every pool None when the tape cannot answer.
    Measured 2026-09-29 on the live tape: p50 0.20 / p90 0.31 ms per call (n=80 mints, <=8 pools)."""
    pools = list(pool_ids)
    try:
        from sniper.services import memecoin_onchain_volume as _ov
        out = _ov.own_v4_depth_usd(chain, pools[:_OWN_DEPTH_MAX_POOLS])
    except Exception:
        out = {}
    return {pid: (round(out[pid.lower()], 2) if out.get(pid.lower()) is not None else None)
            for pid in pools}


def _has_sane_eth_pool(chain, pool_ids):
    """True when one of `pool_ids` has a KNOWN PoolKey that is fee-sane and native/WETH-paired.

    Unknown keys do not count: they would cost a read the entry path must not pay here, and an
    unknown key cannot prove a route. False therefore means "nothing here is proven quotable",
    which is the condition under which the chain scan is worth its one indexed getLogs.
    """
    try:
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        weth = ((CHAIN_QUOTE.get(str(chain)) or {}).get("token") or "").lower()
    except Exception:
        weth = ""
    for pid in pool_ids:
        key = _pool_key_cached(chain, pid)
        if not key or not _fee_is_sane(key.get("fee")):
            continue
        currencies = {str(key.get("currency0", "")).lower(), str(key.get("currency1", "")).lower()}
        if _NATIVE in currencies or (weth and weth in currencies):
            return True
    return False


def _quote_pools_for_mint(chain, mint, *, gt_http=None, rpc_url=None, http=None,
                          ds_http=None, publication_fn=None):
    """Find quote pools with the RPC context, without changing the discovery seam."""
    old_rpc_url = getattr(_blind, "discovery_rpc_url", None)
    old_http = getattr(_blind, "discovery_http", None)
    _blind.discovery_rpc_url = rpc_url
    _blind.discovery_http = http
    try:
        if ds_http is None and publication_fn is None:
            return _pools_for_mint(chain, mint, gt_http=gt_http)
        return _pools_for_mint(chain, mint, gt_http=gt_http, ds_http=ds_http,
                               publication_fn=publication_fn)
    finally:
        _blind.discovery_rpc_url = old_rpc_url
        _blind.discovery_http = old_http


# Back-compat seam. The funded exit sweep and three test files address discovery by this name;
# it is one implementation, so the alias cannot drift from it.
_gt_pools_for_mint = _pools_for_mint


def _pool_key_cached(chain, pool_id):
    """The PoolKey for `pool_id` if we ALREADY know it, else None. Never makes an RPC call.

    Used to keep fee-traps from eating candidate slots before the quoter sees the honest pools.
    A miss must not cost a round trip here: this runs on the entry path, once per candidate, and
    an unknown fee is simply ranked with the unknowns rather than guessed at.
    """
    ck = (str(chain), str(pool_id).lower())
    cached = _key_cache.get(ck)
    if isinstance(cached, dict):
        return cached
    try:
        # The DURABLE cache is the one that matters here: on a fresh process the in-memory dict is
        # empty while SQLite already holds every key this system has ever seen. SEND had 20 of its
        # pools there while the ranking could not tell a 0.9% pool from a 20% trap.
        stored = _stored_pool_key(chain, pool_id)
    except Exception:
        return None
    if isinstance(stored, dict):
        _key_cache[ck] = stored
        return stored
    return None


def _fee_is_sane(fee):
    """Reject a pool whose fee alone guarantees we cannot exit.

    This exists because widening discovery (2026-08-30) made these pools REACHABLE for the first
    time. While the dex-name filter discarded everything on 4663 there was no trap to hit; now
    that we discover by pool-id shape, the honest pool arrives alongside them and something has to
    tell them apart. Fee is the cheapest true discriminator and costs no RPC.

    Fail-closed on a fee we cannot parse: an unreadable fee is not a small one.

    PASSING THIS SCREEN DOES NOT MEAN THE POOL IS TRADEABLE, and the inference is tempting enough
    that it needs saying here. Measured 2026-08-30: a 4663 pool with $3,003 of reported depth and
    a sane 4.99% fee returned NO QUOTE at $32.81, $10, $2 and $0.50 — not a capacity limit, simply
    unquotable. Only an actual quote attempt reveals that. Fee sanity is a necessary condition and
    nothing more.

    Reported DEPTH is also perishable: the same day, one pool thinned from $563 to $85 within
    hours. Any admission decision resting on depth has to be re-measured, never cached as a fact.
    """
    try:
        fee = int(fee)
    except (TypeError, ValueError):
        return False
    if fee == _V4_DYNAMIC_FEE_FLAG:
        return True
    return 0 <= fee <= _V4_MAX_FEE


def _now():
    return time.monotonic()


# A REFUSED UNBOUNDED SCAN IS NOT RE-ASKED (2026-10-07, HALCYON). After every page misses,
# `_pool_key` asks each endpoint for Initialize over `0x0`->`latest`. Infura refuses that range
# (-32602) on every call, so each chain-1 no-quote ladder sent 16-20 of them (v4 + v4_multihop),
# ~255 credits each into the custody key's per-second budget. Serial within a rung: tapped chain-1
# v4_multihop rungs spent p50 0.75 s / p90 0.93 s on them (n=9), v4 rungs 0.22 s (n=4).
# Remembered per answering endpoint and this one
# request shape, never per chain: an endpoint that serves the range (4663's primary, an archive
# spare) never refuses it, so it is never remembered. Only the structural refusals recognized by
# `evm_rpc_pool.is_full_range_refusal` count; a 5xx, 429, timeout or -32603 remains transient.
# The TTL lets a plan upgrade be noticed.
_FULL_RANGE_REFUSAL_TTL_S = float(os.getenv("MEMECOIN_V4_FULL_RANGE_REFUSAL_TTL_S", "3600"))
_full_range_refused = {}                  # answering url -> _now() when it refused the shape
_full_range_lock = threading.Lock()


def _full_range_refused_recently(url):
    with _full_range_lock:
        at = _full_range_refused.get(url)
    return (_FULL_RANGE_REFUSAL_TTL_S > 0 and at is not None
            and _now() - at < _FULL_RANGE_REFUSAL_TTL_S) or evm_rpc_pool.refuses(
                url, "eth_getLogs:full_range", ttl_s=_FULL_RANGE_REFUSAL_TTL_S)


def _full_range_pool_refused_recently(url):
    return all(_full_range_refused_recently(member) for member in evm_rpc_pool.pool_for(url))


# AN OLD POOL IS NOT RE-PAGED AND A 429 PARKS THE SCAN (2026-10-07, BUNKER 15:30Z). The chain-1
# multihop bridge leg asked the keys of two USDC/WETH pools initialised 1.8-3.2M blocks back: the
# pages cannot reach them, Infura refuses 0x0->latest, nothing was stored, and every attempt re-sent
# ~8 getLogs (255 credits each) per pool into the custody key, 429ing the next balance reads.
# A CLEAN miss (window and every page answered [], the full range refused or cooled down) keeps
# only the window read for MISS_TTL; a 429 during a key scan stops key-scan reads on THAT URL for
# 429_COOLDOWN. Custody reads never consult either. 0 disables each.
_KEY_SCAN_MISS_TTL_S = float(os.getenv("MEMECOIN_V4_KEY_SCAN_MISS_TTL_S", "3600"))
_KEY_SCAN_429_COOLDOWN_S = float(os.getenv("MEMECOIN_V4_KEY_SCAN_429_COOLDOWN_S", "60"))
_scan_miss = {}                            # (chain, pool_id) -> _now() of a clean miss
_scan_429 = {}                             # url -> _now() of a 429 during a key scan


# A RETRIED WALK RESUMES WHERE THE LAST ONE STOPPED (2026-10-07, chain 1, 0xsneakky 0x24c2c6f7).
# A walk is remembered above only on a CLEAN miss, and a 429 anywhere in a ladder burst made none
# clean, so 5 attempts in 45 s re-read the same pool ids' window and six pages each time; that burst
# 429'd Infura and both pool members went quarantined (NoEligibleReadEndpoint). Kept here: the
# contiguous range a walk ANSWERED empty, (chain, pool_id) -> (head, first_span, bottom, pages, at).
# A retry trusts only blocks at least first_span/2 below that head (log-index lag is seconds; half a
# window is minutes on every chain), re-reads the top window as before, and pages on below `bottom`
# with what is left of the same page cap. An error or refusal never extends it. 0 disables it.
_KEY_SCAN_DEPTH_TTL_S = float(os.getenv("MEMECOIN_V4_KEY_SCAN_DEPTH_TTL_S", "600"))
_KEY_SCAN_DEPTH_MAX = 4096
_scan_depth = {}


def _depth_memo(ck, head, first_span):
    """(bottom, pages) a walk from `head` may resume from, or None (absent, expired, or its trusted
    range no longer meets this walk's first window)."""
    with _full_range_lock:
        memo = _scan_depth.get(ck)
    if memo is None or _KEY_SCAN_DEPTH_TTL_S <= 0 or _now() - memo[4] >= _KEY_SCAN_DEPTH_TTL_S:
        return None
    old_head, old_span, bottom, pages, _at = memo
    if head - first_span > old_head - old_span // 2 + 1:
        return None
    return bottom, pages


def _note_depth(ck, head, first_span, bottom, pages):
    if _KEY_SCAN_DEPTH_TTL_S <= 0:
        return
    with _full_range_lock:
        if len(_scan_depth) >= _KEY_SCAN_DEPTH_MAX:
            for key in [k for k, v in _scan_depth.items() if _now() - v[4] >= _KEY_SCAN_DEPTH_TTL_S]:
                del _scan_depth[key]
        _scan_depth[ck] = (head, first_span, bottom, pages, _now())


# A POOL OLDER THAN THE WALK CAN REACH (2026-10-07, Alpha GO). On 4663 two bridge pools, 0x6444a8e0
# and 0x8674c1c5, were initialized at blocks 18.6M and 37.3M. Every page of the walk answered empty,
# and the 0x0->latest read was unavailable: NoEligibleReadEndpoint, and the one archive member caps a
# read at 10M blocks. So no clean miss was noted and every quote walked again: 47 eth_getLogs and
# ~5.6 s on 4663 since 10-01, 2.4 s of COMPANY's 2.76 s attempt. For MEMECOIN_V4_KEY_SCAN_EXHAUSTED_TTL_S
# after such a walk (anchored at that walk, never extended), one read covers every block since its
# head plus half a window, so a pool that appears later is still found; the walk and the full range
# are skipped. A stored key always wins: _stored_pool_key is read first. The id is appended to the
# chain's wanted list (wanted_pool_keys_path), which scripts/memecoin_v4_poolkey_index.py resolves on
# its archive host in topic-filtered windows. 0 disables.
_KEY_SCAN_EXHAUSTED_TTL_S = float(os.getenv("MEMECOIN_V4_KEY_SCAN_EXHAUSTED_TTL_S", "1800"))
_scan_exhausted = {}                       # (chain, pool_id) -> (head of the walk, _now())


def _exhausted_head(ck):
    """The head of a recent walk that ran out of pages for this pool, or None."""
    with _full_range_lock:
        memo = _scan_exhausted.get(ck)
    if memo is None or _KEY_SCAN_EXHAUSTED_TTL_S <= 0 or _now() - memo[1] >= _KEY_SCAN_EXHAUSTED_TTL_S:
        return None
    return memo[0]


def _note_exhausted(ck, head):
    if _KEY_SCAN_EXHAUSTED_TTL_S <= 0:
        return
    with _full_range_lock:
        if len(_scan_exhausted) >= _KEY_SCAN_DEPTH_MAX:
            for key in [k for k, v in _scan_exhausted.items() if _now() - v[1] >= _KEY_SCAN_EXHAUSTED_TTL_S]:
                del _scan_exhausted[key]
        _scan_exhausted[ck] = (int(head), _now())


def wanted_pool_keys_path(chain):
    """The chain's wanted list: one '<pool_id> <unix seconds>' line per walk that ran out of pages."""
    from sniper.util.state_paths import state_path
    return state_path("data", "v4_poolkey_wanted_%s.txt" % chain)


def _want_pool_key(chain, pool_id):
    """Append the id to the wanted list on the store side thread, never on the quote's. Never raises."""
    def write():
        import logging
        try:
            path = wanted_pool_keys_path(chain)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write("%s %d\n" % (pool_id, int(time.time())))
        except Exception:
            logging.getLogger(__name__).warning("v4 wanted pool key not written", exc_info=True)
    try:
        return _offpath_executor("store").submit(write)
    except Exception:
        return None


def _scan_recent(table, key, ttl):
    with _full_range_lock:
        at = table.get(key)
    return ttl > 0 and at is not None and _now() - at < ttl


def _scan_note(table, key):
    with _full_range_lock:
        table[key] = _now()


def _note_full_range_answer(url, response):
    """`url` is the physical answering member, never the requested pool alias."""
    if not evm_rpc_pool.is_full_range_refusal(response):
        return
    with _full_range_lock:
        _full_range_refused[url] = _now()


def _note_scan_rate_limit(url, exc):
    target = getattr(exc, "rpc_url", None)
    if target is None and len(evm_rpc_pool.pool_for(url)) == 1:
        target = url                       # legacy/single-endpoint transport
    if target is not None:
        _scan_note(_scan_429, target)


class _KeyScanHttp:
    """Per-request transport: observe physical replies and exclude scan-cooled members only."""
    def __init__(self, http, full_range):
        from sniper.execution.evm_wallet import _http
        self.http = http or _http
        self.full_range = full_range
        self.read_capability_ttl_s = _FULL_RANGE_REFUSAL_TTL_S

    def __getattr__(self, name):
        return getattr(self.http, name)     # preserve the route budget/deadline protocol

    def read_candidate_allowed(self, target):
        return (evm_rpc_pool.available(target)
                and not _scan_recent(_scan_429, target, _KEY_SCAN_429_COOLDOWN_S)
                and not (self.full_range and _full_range_refused_recently(target)))

    def __call__(self, method, target, *args, **kwargs):
        try:
            response = self.http(method, target, *args, **kwargs)
        except Exception as exc:
            if getattr(exc, "http_status", getattr(exc, "code", None)) == 429:
                _scan_note(_scan_429, target)
            raise
        status, answer, _headers = response
        if status == 429:
            _scan_note(_scan_429, target)
        if (self.full_range and json.loads(kwargs.get("body") or "{}").get("method")
                == "eth_getLogs"):
            _note_full_range_answer(target, answer)
        return response


def _archive_rpc_urls(chain, primary_rpc_url):
    """Primary first, then identity-checked archive reads for an immutable Initialize event."""
    from sniper.settings.memecoin import evm_archive_rpc_urls_for_chain
    return tuple(dict.fromkeys((primary_rpc_url, *evm_archive_rpc_urls_for_chain(chain))))


def _archive_is_chain(url, chain, http=None, state=None):
    """Use an archive endpoint only after it proves the configured chain id. `state` (a dict) gets
    unreadable=True when the read itself failed: unread is not a wrong chain (Gamma LOW A)."""
    try:
        response = _rpc(url, "eth_chainId", [], http=http)
        result = response.get("result") if isinstance(response, dict) else None
        if not (isinstance(result, str) and re.fullmatch(r"0x[0-9a-fA-F]+", result)):
            # An error body at HTTP 200 (-32005 "rate limited"), a missing or malformed result:
            # the endpoint said nothing about its chain (Gamma on 1d1a3449c).
            if state is not None:
                state["unreadable"] = True
            return False
        return int(result, 16) == int(chain)   # only a parsed numeric mismatch is a wrong chain
    except Exception:
        if state is not None:
            state["unreadable"] = True
        return False


def _stored_pool_key(chain, pool_id):
    """Read the durable key cache. A cache failure must never block chain verification."""
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_core import get_readonly_connection
    conn = None
    try:
        conn = get_readonly_connection()
        # FAIL FAST (2026-09-16 13:02Z). The default reader waits 30s on a busy database, and this
        # read runs on the main loop once per pool: the runner was killed as sweep_stale inside it.
        # A miss here only costs the chain read below.
        conn.execute("PRAGMA busy_timeout=250;")
        return m.get_evm_v4_pool_key(
            conn, chain_id=chain, pool_id=pool_id,
            pool_manager=V4[str(chain)]["pool_manager"])
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()


# --- PoolKey derivation: turn a provider's PAIR into a KEY, with no log read -------------------
#
# THE POOL ID *IS* THE KEY. A V4 pool id is `keccak(abi.encode(PoolKey))`, so when a provider hands
# us a pool id AND the two tokens in it, the only unknowns are (fee, tickSpacing, hooks). Try the
# combinations this chain has actually shown us, hash each one, and keep the first that reproduces
# the provider's id EXACTLY. A hit is a proof, a miss is silence, and neither costs a request.
#
# WHY IT EXISTS. The log scan cannot reach the pools we need. On 4663 the CryoPalmar/R miss
# (2026-09-06 12:44) had the real R/USDG pool listed by GeckoTerminal with $15,982 of liquidity and
# the only key we held was a 91.06%-fee trap with $1.23; the pool had existed for twenty minutes and
# no reader could turn the provider's pair into a key. On Base it is worse and structural: the pools
# we would trade are a MEDIAN 308 HOURS old against a scan that reaches about 33, and the endpoints
# refuse any `eth_getLogs` range over 10,000 blocks.
#
# THE CANDIDATE SET IS LEARNED, NOT CURATED, and that is the load-bearing decision. Measured over
# the 165,142 keys we already hold: 66.4% of all pools have a zero hook, but among SANE-FEE pools
# only 37.6% do, across 2,467 distinct hook addresses. The hook is the discriminator, not the fee.
# A hand-written list of "the launchpad family, the common fees, and the zero hook" would have
# missed CryoPalmar, whose winning family was `fee=0, ts=200, hooks=0xe5e702…` — hooked, and only
# the 4th most common. Ranking by observed frequency needs no maintenance and follows the chain.
#
# SAFETY, AND IT IS STRONGER THAN WHAT IT REPLACES. Only fees that pass `_fee_is_sane` enter the
# candidate set, so DERIVATION CANNOT PRODUCE A TRAP POOL. The 91.06% R trap is structurally
# underivable: no sane candidate hashes to it. If a provider hands us a trap's pool id, nothing
# matches, this returns None, and the caller falls back to the log scan exactly as before. The
# equality is also the STORE GATE — a key that does not hash to the provider's id is never cached
# and never persisted.
#
# COST. 44.7 us per candidate, so the 512-candidate default is 23 ms. One `eth_getLogs` page costs
# 220 ms to 2.4 s and the scan runs up to six of them.
#
# RECOVERY, on an honest time split of the 4663 table (trained on the 132,100 keys verified before
# 2026-09-04T10:15, tested on the 33,025 verified after, no leakage): N=32 recovers 68.7% of unseen
# sane pools, N=128 73.9%, N=512 80.5%, N=2048 83.9%.
_FAMILY_CANDIDATES = int(os.getenv("MEMECOIN_V4_KEY_FAMILY_CANDIDATES", "512"))
# The ranking is a full GROUP BY over the key table (338 ms at 165k rows), so it is paid once per
# chain per TTL, never per lookup.
_FAMILY_TTL_S = float(os.getenv("MEMECOIN_V4_KEY_FAMILY_TTL_S", "600"))
_family_cache = {}                          # chain -> (expires_at, ((fee, tick_spacing, hooks), ...))
# The provider knows the pair; `_pool_key` is only given an id. This carries the counter-currency
# between them. Bounded, because a long-running process discovers a great many pools.
_PAIR_HINT_MAX = 4096
_pair_hint = {}                             # (chain, pool_id) -> counter-currency address


def _note_pair(chain, pool_id, counter):
    """Record the counter-currency a provider reported for a pool id."""
    pid = str(pool_id or "").lower()
    other = str(counter or "").lower()
    if not pid or not other.startswith("0x") or len(other) != 42:
        return
    if len(_pair_hint) >= _PAIR_HINT_MAX:
        _pair_hint.clear()                  # bounded and cheap; a lost hint costs one derivation
    _pair_hint[(str(chain), pid)] = other


def pool_key_families(chain, *, now_fn=None):
    """The ranked families for this chain IF THEY ARE ALREADY RANKED, else empty. Never builds.

    THE ENTRY PATH ONLY EVER READS. Building the ranking is a full GROUP BY over the key table
    (340 ms at 165k rows) and the funded entry quote runs at a p50 of 545 ms on 4663, so a cold
    build there would be a 62% add on exactly the path that races other buyers and is judged by a
    chase cap. The build lives on the tick stream instead (`warm_pool_key_families`), which already
    walks these logs every pass and is nowhere near the money. A cold read here simply derives
    nothing and the caller falls back to the log scan, which is what it did before this existed.
    """
    now = (now_fn or time.time)()
    hit = _family_cache.get(str(chain))
    return hit[1] if (hit and hit[0] > now) else ()


def warm_pool_key_families(chain, *, now_fn=None, conn_fn=None):
    """Rank `(fee, tick_spacing, hooks)` for this chain and cache it. Called OFF the entry path.

    Sane fees only, so a trap fee can never become a candidate. Empty when the chain has no keys
    yet — Base today, and any new chain — so derivation is simply a no-op there. Never raises: a
    cache failure must not break the tape or the quote path.
    """
    now = (now_fn or time.time)()
    hit = _family_cache.get(str(chain))
    if hit and hit[0] > now:
        return hit[1]
    families = ()
    conn = None
    owned = conn_fn is None                 # only close what we opened; an injected handle is the
    try:                                    # caller's to manage, and closing it surprised a test
        if conn_fn is None:
            from sniper.persistence.db_core import get_readonly_connection as conn_fn
        conn = conn_fn()
        rows = conn.execute(
            "SELECT json_extract(key_json,'$.fee'), json_extract(key_json,'$.tick_spacing'),"
            "       lower(json_extract(key_json,'$.hooks')), COUNT(*) n"
            "  FROM memecoin_evm_v4_pool_keys WHERE network_id = ?"
            "  GROUP BY 1,2,3 ORDER BY n DESC", (str(chain),)).fetchall()
        ranked = []
        for fee, tick_spacing, hooks, _n in rows:
            if fee is None or tick_spacing is None or not hooks:
                continue
            if not _fee_is_sane(fee):
                continue                    # a trap fee never becomes a candidate
            ranked.append((int(fee), int(tick_spacing), str(hooks)))
            if len(ranked) >= _FAMILY_CANDIDATES:
                break
        families = tuple(ranked)
    except Exception:
        import logging                      # local: this module has no logging surface of its own
        logging.getLogger("memecoin").debug(
            "v4: pool-key family ranking unavailable", exc_info=True)
        families = ()
    finally:
        if conn is not None and owned:
            try:
                conn.close()
            except Exception:
                pass
    _family_cache[str(chain)] = (now + _FAMILY_TTL_S, families)
    return families


def derive_pool_key(chain, pool_id, token_a, token_b, *, families=None):
    """The PoolKey for `pool_id`, derived from its pair and verified by hash, or None.

    `token_a`/`token_b` are the provider's two tokens in either order; V4 requires
    `currency0 < currency1`, so they are sorted here. The returned key is the one whose
    `keccak(abi.encode(PoolKey))` EQUALS `pool_id`. There is no partial match and no ranking among
    hits: either a candidate reproduces the id or it does not.
    """
    from sniper.execution.evm_v4_launchpad import pool_id_for_key
    want = str(pool_id or "").lower()
    if not _is_v4_pool_id(want):
        return None
    try:
        a, b = str(token_a).lower(), str(token_b).lower()
        if not (a.startswith("0x") and b.startswith("0x") and len(a) == 42 and len(b) == 42):
            return None
        currency0, currency1 = (a, b) if int(a, 16) < int(b, 16) else (b, a)
    except (TypeError, ValueError, AttributeError):
        return None
    if currency0 == currency1:
        return None
    for fee, tick_spacing, hooks in (pool_key_families(chain) if families is None else families):
        try:
            key = {"currency0": currency0, "currency1": currency1,
                   "fee": int(fee), "tick_spacing": int(tick_spacing), "hooks": hooks}
            if pool_id_for_key(key).lower() == want:
                return key
        except Exception:
            continue                        # a malformed family is skipped, never fatal
    return None


# ---- HOOKERS (2026-09-30, attempt 92da4a9a): a 54-day-old hooked pool had no stored key, the rung's
# Initialize scan failed after 29 calls and 2.8 s, the ladder cut it at grace and an aggregator's stale
# price refused the entry. Two warms, both OFF the entry path and both hash-verified, so the rung
# finds the key in the cache instead of scanning for it.
_DYNAMIC_FEE = 0x800000                 # the V4 dynamic-fee flag a hooked pool carries in its key
_RECEIPT_TICK_SPACINGS = (1, 10, 60, 200)
_ZERO_ADDRESS = "0x" + "0" * 40
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_OFFPATH_LOCK = threading.Lock()
_OFFPATH_SEEN = set()                   # (chain, pool) handed to the side thread in this process
_OFFPATH_POOLS = {}                     # "read" and "store": one worker each, made on first use
# The off-path read shares a HOST with funded reads (see read_pool_key_offpath), so it waits until the
# entry that triggered it is over. Reserved attempts 2026-09-23..30: 4663 p50 4.9 s, p90 12.3 s, max
# 18.3 s (n=80); 8453 p50 3.2 s, p90 8.2 s, max 16.3 s (n=48).
# ponytail: a fixed wait covers the triggering entry only, not an unrelated one that starts later;
# gate on "no unresolved reservation on this chain" if the read ever shows up beside a 429.
_OFFPATH_DELAY_S = 20.0


def derive_pool_key_from_receipt(chain, pool_id, logs, mint, swap_fee=None):
    """The PoolKey of `pool_id` rebuilt from ONE transaction's own logs and verified by hash, or None.

    Candidates, all read off the receipt: the coin paired with native ETH (the zero address) or a
    token that moved; the hook as an address that emitted a log naming the pool id, then no hook,
    then any other emitter; the fee as the dynamic flag or the Swap's own fee; tickSpacing from the
    common ones, then this chain's ranked families. The first candidate whose
    keccak(abi.encode(key)) EQUALS the pool id is returned. There is no partial match: a hook that
    emitted nothing in this transaction yields None, and the off-path Initialize read covers it.
    Pure and bounded (at most 6 x 16 x 2 x 20 hashes); never raises."""
    want, coin = str(pool_id or "").lower(), str(mint or "").lower()
    if not _is_v4_pool_id(want) or len(coin) != 42:
        return None
    try:
        from sniper.execution.evm_v4_launchpad import pool_id_for_key
        manager = str((V4.get(str(chain)) or {}).get("pool_manager") or "").lower()
        tokens, named, emitters = [_ZERO_ADDRESS], [], []
        for log in logs or ():
            address = str(log.get("address") or "").lower()
            topics = [str(t).lower() for t in (log.get("topics") or ())]
            if len(address) != 42 or address == manager or not topics:
                continue
            if topics[0] == _TRANSFER_TOPIC:                  # a token, never the hook
                if address != coin and address not in tokens:
                    tokens.append(address)
            elif want in topics[1:]:
                if address not in named:
                    named.append(address)
            elif address not in emitters:
                emitters.append(address)
        hooks = (named + [_ZERO_ADDRESS] + [a for a in emitters if a not in named])[:16]
        fees = [_DYNAMIC_FEE]
        if swap_fee is not None and int(swap_fee) != _DYNAMIC_FEE:
            fees.append(int(swap_fee))
        spacings = list(_RECEIPT_TICK_SPACINGS)
        for _fee, spacing, _hooks in pool_key_families(chain):
            if spacing not in spacings and len(spacings) < 20:
                spacings.append(spacing)
        for other in tokens[:6]:
            currency0, currency1 = (coin, other) if int(coin, 16) < int(other, 16) else (other, coin)
            for hook in hooks:
                for fee in fees:
                    for spacing in spacings:
                        key = {"currency0": currency0, "currency1": currency1, "fee": fee,
                               "tick_spacing": int(spacing), "hooks": hook}
                        if pool_id_for_key(key).lower() == want:
                            return key
    except Exception:
        return None
    return None


def _offpath_post(url, method, params, timeout=10.0):
    import json
    from sniper.execution import evm_wallet as W
    status, payload, _headers = W._http(
        "POST", url, body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}),
        timeout=timeout)
    if not isinstance(payload, dict) or "result" not in payload:
        raise RuntimeError("rpc %s http=%s: %s" % (method, status, str(payload)[:160]))
    return payload["result"]


def read_pool_key_offpath(chain, pool_id, *, post_fn=None):
    """ONE full-range Initialize read for ONE pool id, on the archive / on-chain reader URLs the
    offline indexer uses. The pool id is an indexed topic, so the read is targeted, not a block
    sweep. OFF THE ENTRY PATH ONLY: it can take seconds.

    THESE ARE FUNDED HOSTS, NOT A SEPARATE ONE (resolved with .env loaded, 2026-09-30). On 4663 both
    the archive list and MEMECOIN_ONCHAIN_EVM_RPC_URL_4663 are rpc.mainnet.chain.robinhood.com: the
    custody endpoint, and the only pool member that serves a full-range eth_getLogs. On 8453 it is
    base-rpc.publicnode.com, and on 5042 both archive URLs: funded pool members. The request goes
    to the URL as given (`_http`, no pool rotation, no penalty), but it spends that host's rate
    budget. So the caller delays it past the entry burst (`_OFFPATH_DELAY_S`), it runs once per
    pool per process, and a refused read logs the host and the HTTP status.

    The key is cached and stored only when it hashes back to the pool id. None on any failure,
    which leaves the entry path exactly where it was."""
    chain, pool_id = str(chain), str(pool_id or "").lower()
    conf = V4.get(chain)
    if not conf or not _is_v4_pool_id(pool_id):
        return None
    post_fn = post_fn or _offpath_post
    urls = [url for url in _archive_rpc_urls(chain, None) if url]
    urls.append((os.getenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_%s" % chain) or "").strip())
    for url in dict.fromkeys(url for url in urls if url):
        try:
            logs = post_fn(url, "eth_getLogs", [{
                "address": conf["pool_manager"], "topics": [_INIT_TOPIC, pool_id],
                "fromBlock": "0x0", "toBlock": "latest"}])
        except Exception as exc:            # a refused or timed-out read is not an absent pool
            import logging
            from urllib.parse import urlsplit
            logging.getLogger("memecoin").warning(
                "v4 key off-path read failed chain=%s pool=%s host=%s error=%s",
                chain, pool_id, urlsplit(url).hostname, str(exc)[:200])   # host only: a URL can carry a key
            continue
        for log in logs or ():
            try:
                key = decode_initialize_log(log)
                if key is None or "0x" + keccak(bytes.fromhex(_encode_pool_key(key))).hex() != pool_id:
                    continue
            except Exception:
                continue
            _key_cache[(chain, pool_id)] = key
            _store_pool_key(chain, pool_id, key)
            return key
    return None


def _offpath_executor(name):
    """The one-worker side executor for reads or for stores. They are separate so a durable store
    never queues behind a read that is waiting out `_OFFPATH_DELAY_S`: `_stored_pools_for_mint`
    lists a pool only once its key row exists."""
    with _OFFPATH_LOCK:
        pool = _OFFPATH_POOLS.get(name)
        if pool is None:
            pool = _OFFPATH_POOLS[name] = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="v4-key-offpath-" + name)
        return pool


def _offpath_job(read_fn, chain, pool_id, due):
    import logging
    wait = due - time.monotonic()
    if wait > 0:
        time.sleep(wait)                    # on the side thread only; see _OFFPATH_DELAY_S
    # Memory first, then the durable table: the rung's own scan or a receipt may have answered
    # meanwhile, and after a restart the table holds keys the empty in-memory cache does not.
    key = _pool_key_cached(chain, pool_id)
    if key is not None:
        return key
    started = time.monotonic()
    try:
        key = read_fn(chain, pool_id)
        return key
    finally:
        logging.getLogger("memecoin").warning(
            "v4 key off-path read chain=%s pool=%s outcome=%s ms=%d", chain, pool_id,
            "stored" if key else "none", (time.monotonic() - started) * 1000.0)


def warm_pool_key_offpath(chain, pool_id, *, read_fn=None):
    """Hand one pool id to the side thread's Initialize read, once per pool per process. The read
    starts `_OFFPATH_DELAY_S` after this call and is skipped when the key is cached by then. Returns
    the Future, or None when the pool was already handed over or its key is already cached. Never
    blocks and never raises into the caller (the source receipt worker).
    # ponytail: one try per pool per process; add a timed retry if `outcome=none` lines show a
    # transient read failure losing a key until the next restart."""
    ck = (str(chain), str(pool_id or "").lower())
    try:
        with _OFFPATH_LOCK:
            if ck in _OFFPATH_SEEN or ck in _key_cache:
                return None
            _OFFPATH_SEEN.add(ck)
        return _offpath_executor("read").submit(
            _offpath_job, read_fn or read_pool_key_offpath, ck[0], ck[1],
            time.monotonic() + _OFFPATH_DELAY_S)
    except Exception:
        return None


def store_pool_key_offpath(chain, pool_id, key):
    """The durable write of an already hash-verified key, on the store side thread. The caller is
    the source receipt worker, whose answer an entry waits on, and a ledger write can wait on a busy
    database. The in-process caches are the caller's to set, at once. Returns the Future; never
    raises."""
    try:
        return _offpath_executor("store").submit(_store_pool_key, chain, pool_id, key)
    except Exception:
        return None


def _derive_and_cache_pool_key(chain, pool_id, mint):
    """Derive a provider-listed pool's key from the pair we recorded for it. True when stored.

    Called from discovery, where the pair is known. The key lands in the in-process cache AND the
    durable one, so `_pool_key` finds it without ever reading a log.
    """
    counter = _pair_hint.get((str(chain), str(pool_id).lower()))
    if not counter:
        return False
    key = derive_pool_key(chain, pool_id, mint, counter)
    if key is None:
        return False
    _key_cache[(str(chain), str(pool_id).lower())] = key
    _store_pool_key(chain, str(pool_id).lower(), key)
    return True


def _stored_pools_for_mint(chain, mint, *, require_complete=False):
    """Pool ids for a mint out of the durable PoolKey cache. No network, no provider, no quota.

    The cache is keyed by pool id, but every stored key CONTAINS both currencies, so the table we
    already fill on every successful read doubles as a mint -> pools index. A pool is paid for once,
    ever, and every later entry on that coin skips discovery entirely.

    This matters because the wide chain read it replaces is expensive and unreliable: measured
    2026-09-03 over 20 identical full-range reads against the only endpoint that serves them, 17
    answered, 2 returned an error body, 1 was rate limited, p50 665 ms and p90 2157 ms. A cache hit
    is about 2 ms. Read-only and fail-quiet: any cache trouble returns nothing and the callers fall
    through to the live sources exactly as before.
    """
    conf = V4.get(str(chain))
    if not conf or not mint:
        if require_complete:
            raise ValueError("unknown V4 chain or mint")
        return []
    from sniper.persistence.db_core import get_readonly_connection
    conn = None
    try:
        conn = get_readonly_connection()
        target = str(mint).lower()
        # UNION, NEVER `OR` (2026-09-03). Two expression indexes cover the two currency sides, but
        # an OR across them makes SQLite fall back to the leading equality columns alone, and every
        # row shares those, so the "indexed" query scanned the whole table and evaluated
        # json_extract on each row. Measured at 124,245 rows: OR 168 ms, UNION 0.06 ms. A UNION
        # gives each branch its own index seek. `test_the_cache_lookup_seeks_both_indexes` asserts
        # the plan so this cannot silently regress as the table grows.
        one = ("SELECT pool_id FROM memecoin_evm_v4_pool_keys "
               "WHERE network_id=? AND pool_manager=? "
               "AND lower(json_extract(key_json,'$.%s'))=?")
        rows = conn.execute(
            one % "currency0" + " UNION " + one % "currency1",
            (str(chain), conf["pool_manager"], target,
             str(chain), conf["pool_manager"], target)).fetchall()
        if require_complete:
            for row in rows:
                pid = str(row[0]).lower()
                if not _is_v4_pool_id(pid):
                    raise ValueError("malformed cached V4 pool id")
                int(pid[2:], 16)
        return sorted({str(r[0]).lower() for r in rows if _is_v4_pool_id(str(r[0]).lower())})
    except Exception:
        if require_complete:
            raise
        return []
    finally:
        if conn is not None:
            conn.close()


def _store_pool_key(chain, pool_id, key):
    """Persist only a key that this process read from the configured PoolManager event."""
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_core import get_connection
    conn = None
    try:
        conn = get_connection()
        m.put_evm_v4_pool_key(
            conn, chain_id=chain, pool_id=pool_id,
            pool_manager=V4[str(chain)]["pool_manager"], key=key)
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()


def store_pool_keys_from_logs(chain, logs):
    """Persist every verified PoolKey in a batch of PoolManager `Initialize` logs. Returns the count.

    THE TAPE SCAN ALREADY RECEIVES THESE BYTES. `INGEST_TOPICS` carries V4 `Initialize`, so fee,
    tickSpacing and hooks arrive on the live tick scan for every new pool -- and `decode_v4_initialize`
    keeps only the two currencies, so all three were parsed straight past on every row. Filling the
    durable cache here costs NO request, and it removes the wide chain read from the entry path:
    measured 2026-09-03 over 20 identical full-range reads against the one 4663 endpoint that serves
    them, 17 answered, 2 returned an error body, 1 was rate limited, p50 665 ms and p90 2157 ms. One
    transient failure there loses an entry. A cache hit is about 2 ms and has no failure mode.

    HASH BACK, ALWAYS. A V4 pool id IS the keccak of its encoded PoolKey, so re-deriving it proves
    the decode and the log agree about the same pool. A wrong cached key would make a funded entry
    quote a pool that does not exist, and this writer runs on the research tape lane, so it is never
    trusted on shape alone. Same check the offline indexer applies, against the same shared decode.

    FAIL-QUIET BY CONTRACT. The caller is tick ingest. A cache write must never cost a tick, so a
    bad log is skipped and a store failure returns the count so far.
    """
    conf = V4.get(str(chain))
    if not conf or not logs:
        return 0
    manager = str(conf["pool_manager"]).lower()
    # FILTER BEFORE TOUCHING THE LEDGER. The caller is tick ingest, which runs continuously, and
    # `Initialize` is rare on that tape -- about 12 a minute against a pass every few seconds. The
    # scan's logs are overwhelmingly swaps. Opening a WRITE connection to the FUNDED ledger on
    # every pass to store nothing is churn the money path should never pay for, and this database
    # has a documented history of WAL pain. No candidates, no connection.
    candidates = [log for log in logs
                  if len(log.get("topics") or ()) >= 4
                  and str((log.get("topics") or ())[0]).lower() == _INIT_TOPIC
                  and str(log.get("address") or "").lower() == manager]
    if not candidates:
        return 0
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_core import get_connection
    conn = None
    stored = 0
    try:
        conn = get_connection()
        for log in candidates:
            try:
                topics = log.get("topics") or ()
                pool_id = str(topics[1]).lower()
                if not _is_v4_pool_id(pool_id):
                    continue
                key = decode_initialize_log(log)
                if key is None:
                    continue
                if "0x" + keccak(bytes.fromhex(_encode_pool_key(key))).hex() != pool_id:
                    continue
                m.put_evm_v4_pool_key(conn, chain_id=str(chain), pool_id=pool_id,
                                      pool_manager=manager, key=key)
                stored += 1
            except Exception:
                continue
    except Exception:
        return stored
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return stored


def decode_initialize_log(log):
    """PoolKey out of one PoolManager `Initialize` event, or None when the shape is wrong.

    Both currencies are INDEXED, so they arrive as topics; fee, tickSpacing and hooks are packed
    words in `data`. tickSpacing is an int24 and arrives two's-complement in a 256-bit word, so a
    high bit means a negative spacing and must be sign-extended, never read as a huge positive.

    Shared on purpose with the offline indexer: a key it stores must be byte-identical to one the
    quote path would have read itself, or a cache hit would quote a pool that hashes to a different
    id.
    """
    try:
        data = log["data"][2:]
        tick = int(data[64:128], 16)
        return {"currency0": "0x" + log["topics"][2][-40:],
                "currency1": "0x" + log["topics"][3][-40:],
                "fee": int(data[0:64], 16),
                "tick_spacing": tick - (1 << 256) if tick >= (1 << 255) else tick,
                "hooks": "0x" + data[128 + 24:192]}
    except Exception:
        return None


def _pool_key(chain, pool_id, rpc_url, http=None, *, pair=None):
    """PoolKey from the PoolManager's Initialize event (immutable -> cached forever)."""
    ck = (str(chain), pool_id.lower())
    if ck in _key_cache:
        return _key_cache[ck]
    cached = _stored_pool_key(chain, pool_id)
    # Multihop knows both currencies. An exact hash match saves the old pool's log scan.
    if cached is None and pair is not None:
        cached = derive_pool_key(chain, pool_id, *pair)
    if cached is not None:
        _key_cache[ck] = cached
        return cached
    if all(_scan_recent(_scan_429, member, _KEY_SCAN_429_COOLDOWN_S)
           for member in evm_rpc_pool.pool_for(rpc_url)):
        _mark_blind("rpc_rate_limit")      # parked: see _KEY_SCAN_429_COOLDOWN_S, no read sent
        return None

    def scan_rpc(url, method, params, http=None):
        full_range = (method == "eth_getLogs" and params
                      and params[0].get("fromBlock") == "0x0"
                      and params[0].get("toBlock") == "latest")
        return _rpc(url, method, params, http=_KeyScanHttp(http, full_range))

    remembered_miss = _scan_recent(_scan_miss, ck, _KEY_SCAN_MISS_TTL_S)
    exhausted_head = None if remembered_miss else _exhausted_head(ck)
    window_clean = walk_complete = False
    first_span = resume = None
    full_open = False                      # some endpoint may still serve the full range
    logs = None
    head = None
    scanned_lower = None
    # A 429 ANYWHERE IN THIS READ IS THE ENDPOINT'S QUOTA (2026-10-07, HALCYON). It was typed
    # `poolkey_read_failed`, which the entry path re-quotes at once and the retry lane re-asks every
    # 6 s, into the same limit. `rpc_rate_limit` waits MEMECOIN_ENTRY_RETRY_RPC_429_S instead.
    rate_limited = False
    try:
        head = int(str(scan_rpc(rpc_url, "eth_blockNumber", [], http=http)["result"]), 0)
        # CAPPED BY WHAT THIS CHAIN WILL ACTUALLY SERVE (2026-09-23). The lookback is a hardcoded
        # 8,192 and Base now refuses anything over 2,000, so the first read was refused before the
        # paged scan even began -- and since every page was 10,000 it was refused too. A read the
        # endpoint will not serve is not a lookback, it is a wasted round trip.
        first_span = _V4_DISCOVERY_LOOKBACK_BLOCKS
        page_width = _key_scan_page_blocks(chain)
        if page_width > 0:
            first_span = min(first_span, page_width)
        if exhausted_head is not None:
            # A WALK RAN OUT FOR THIS POOL (_KEY_SCAN_EXHAUSTED_TTL_S): read every block since its head,
            # plus half a window for log-index lag, in ONE read when that fits a page; else as before.
            since = head - exhausted_head + first_span // 2
            if 0 <= since <= (page_width if page_width > 0 else first_span):
                first_span = max(first_span, since)
                remembered_miss = True
        resume = _depth_memo(ck, head, first_span)
        response = scan_rpc(rpc_url, "eth_getLogs", [{
            "address": V4[str(chain)]["pool_manager"],
            "topics": [_INIT_TOPIC, pool_id],
            "fromBlock": hex(max(0, head - first_span)),
            "toBlock": hex(head)}], http=http)
        result = response.get("result") if isinstance(response, dict) else None
        if isinstance(result, list):
            scanned_lower = max(0, head - first_span)
            window_clean = True
            if result:
                logs = result
        elif result is None:
            _learn_range_limit(chain, response, first_span)
    except evm_rpc_pool.NoEligibleReadEndpoint:
        limited = any(_scan_recent(_scan_429, member, _KEY_SCAN_429_COOLDOWN_S)
                      for member in evm_rpc_pool.pool_for(rpc_url))
        _mark_blind("rpc_rate_limit" if limited else "poolkey_read_failed")
        return None
    except RequestNotSent:
        return None                        # the caller's limit, not the chain: never mark blind
    except Exception as exc:
        rate_limited = getattr(exc, "http_status", None) == 429
        if rate_limited:
            _note_scan_rate_limit(rpc_url, exc)
    timed_out = False
    # BOUNDED BACKWARD SCAN (2026-09-02). The first read covers only
    # `_V4_DISCOVERY_LOOKBACK_BLOCKS`, which Alpha measured at 829 SECONDS on 4663 -- 13.8
    # minutes. The coins we actually quote are a median 1.41h old and a p90 of 15.67h, so the
    # first read misses by 6x at the median and 68x at p90, and everything then depended on an
    # unbounded `0x0`->`latest` fallback that fails far more often than it works.
    #
    # WHY THIS IS THE FIX THAT MATTERS: of 475 funded 4663 attempts that passed every selection
    # gate and RESERVED capital over 48h, only 64 filled. 153 died on "no quote" -- 110 no_route
    # and 43 `provider_rpc_transport` where a rung could not READ. One in three funded attempts,
    # and the largest single cause of not filling by a factor of three.
    #
    # Walks backward in bounded pages instead. Stops on the first hit, so a young pool costs one
    # extra page and an old one costs at most `_V4_KEY_SCAN_MAX_PAGES`. The result is persisted by
    # `_store_pool_key`, so a pool is paid for ONCE, ever.
    #
    # An empty page is evidence of absence FOR THAT RANGE ONLY and must never mark blind; only a
    # refusal may. Getting that backwards turns "no pool here" into "provider blind" and breaks
    # the NO_ROUTE gating.
    page_blocks = _key_scan_page_blocks(chain)
    answered_bottom, answered_pages = None, 0
    caller_stopped = False
    if not logs and head is not None and page_blocks > 0 and not remembered_miss and not rate_limited:
        # Only a successful read advances the cursor. A capped/rejected first range
        # otherwise leaves a gap directly below the head that no later page can find.
        upper = head if scanned_lower is None else scanned_lower
        pages_left = _V4_KEY_SCAN_MAX_PAGES
        if resume is not None:              # a retry: the range down to `bottom` is answered
            upper, pages_left = resume[0], max(0, _V4_KEY_SCAN_MAX_PAGES - resume[1])
        for _page in range(pages_left):
            if upper <= 0:
                walk_complete = True
                break
            lower = max(0, upper - page_blocks)
            try:
                response = scan_rpc(rpc_url, "eth_getLogs", [{
                    "address": V4[str(chain)]["pool_manager"],
                    "topics": [_INIT_TOPIC, pool_id],
                    "fromBlock": hex(lower), "toBlock": hex(upper)}], http=http)
                result = response.get("result") if isinstance(response, dict) else None
            except RequestNotSent:
                caller_stopped = True
                break                      # keep answered coverage before returning; never mark blind
            except Exception as exc:
                if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
                    timed_out = True
                if getattr(exc, "http_status", None) == 429:
                    rate_limited = True
                    _note_scan_rate_limit(rpc_url, exc)
                break                      # a refused page says nothing about the range below it
            if not isinstance(result, list):
                # THE REFUSAL NAMES THE LIMIT, SO BELIEVE IT AND RETRY THIS PAGE ONCE
                # (2026-09-23). Before this, a narrowed provider meant every page was refused and
                # the scan broke on the first one -- `poolkey_read_failed` on every Base coin.
                learned = _learn_range_limit(chain, response, page_blocks)
                if learned is None:
                    break                  # A JSON-RPC error is not an empty page.
                page_blocks = learned
                lower = max(0, upper - page_blocks)
                try:
                    response = scan_rpc(rpc_url, "eth_getLogs", [{
                        "address": V4[str(chain)]["pool_manager"],
                        "topics": [_INIT_TOPIC, pool_id],
                        "fromBlock": hex(lower), "toBlock": hex(upper)}], http=http)
                    result = response.get("result") if isinstance(response, dict) else None
                except RequestNotSent:
                    caller_stopped = True
                    break
                except Exception as exc:
                    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
                        timed_out = True
                    if getattr(exc, "http_status", None) == 429:
                        rate_limited = True
                        _note_scan_rate_limit(rpc_url, exc)
                    break
                if not isinstance(result, list):
                    break                  # still refused at its own stated limit: stop asking
            if result:
                logs = result
                break
            upper = lower                  # empty page: absence HERE, keep walking back
            answered_bottom, answered_pages = lower, answered_pages + 1
        else:
            walk_complete = True           # every page answered, all empty
    if not logs and head is not None and first_span is not None:
        # Only a clean, empty first window anchors a new memory at this head; without one, a
        # resumed walk's pages still extend the old memory below its own trusted top.
        if window_clean:
            base = resume if resume is not None else (scanned_lower, 0)
            _note_depth(ck, head, first_span, answered_bottom if answered_pages else base[0],
                        base[1] + answered_pages)
        elif resume is not None and answered_pages:
            with _full_range_lock:
                old = _scan_depth.get(ck)
            if old is not None:
                _note_depth(ck, old[0], old[1], answered_bottom, resume[1] + answered_pages)
    if caller_stopped:
        return None                        # no archive fallback after the caller stopped discovery
    if not logs and not remembered_miss:
        for read_rpc_url in _archive_rpc_urls(chain, rpc_url):
            if _full_range_pool_refused_recently(read_rpc_url):
                continue
            if all(_scan_recent(_scan_429, member, _KEY_SCAN_429_COOLDOWN_S)
                   for member in evm_rpc_pool.pool_for(read_rpc_url)):
                full_open = True           # parked by a 429: not asked, and proves nothing
                continue
            state = {}
            if read_rpc_url != rpc_url and not _archive_is_chain(
                    read_rpc_url, chain, http=_KeyScanHttp(http, False), state=state):
                full_open = full_open or state.get("unreadable", False)   # unread is not wrong
                continue
            try:
                response = scan_rpc(read_rpc_url, "eth_getLogs", [{
                "address": V4[str(chain)]["pool_manager"],
                "topics": [_INIT_TOPIC, pool_id],
                "fromBlock": "0x0", "toBlock": "latest"}], http=http)
                result = response.get("result") if isinstance(response, dict) else None
            except evm_rpc_pool.NoEligibleReadEndpoint:
                full_open = True
                continue
            except RequestNotSent:
                return None                # the caller's limit, not the chain: never mark blind
            except Exception as exc:
                # A timeout and a refusal are different faults with different fixes. The full-range
                # read was measured at 1.8-2.4s against a 3.0s read budget already partly consumed,
                # so a timeout here is plausible and must not be reported as a refusal.
                if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
                    timed_out = True
                if getattr(exc, "http_status", None) == 429:
                    rate_limited = True
                    _note_scan_rate_limit(read_rpc_url, exc)
                full_open = True
                continue
            if not isinstance(result, list):
                from sniper.execution.evm_wallet import answer_target
                target = answer_target(response)
                if target is None and len(evm_rpc_pool.pool_for(read_rpc_url)) == 1:
                    target = read_rpc_url
                if target is not None:
                    _note_full_range_answer(target, response)
                full_open = full_open or not _full_range_pool_refused_recently(read_rpc_url)
            if isinstance(result, list):
                # NOTE (2026-09-02, NOT changed here). An empty answer returns from the WHOLE
                # function, so the first endpoint to answer empty prevents any configured archive
                # from being consulted — and `_archive_rpc_urls` puts the PRIMARY first. Gamma
                # flagged this as a bug; `test_empty_primary_log_response_is_a_clean_no_pool`
                # asserts it as the contract, so somebody chose it. It is only a defect if the
                # primary can answer empty for a range it did not truly serve, and Alpha measured
                # this primary serving `0x0`->`latest` correctly (found the log, 2.4s). Left alone
                # deliberately: changing it is a behaviour decision, and it costs nothing today
                # because zero archives are configured on any chain.
                if not result:
                    return None
                logs = result
                break
    if not logs:
        rate_limited = rate_limited or any(
            _scan_recent(_scan_429, member, _KEY_SCAN_429_COOLDOWN_S)
            for url in _archive_rpc_urls(chain, rpc_url)
            for member in evm_rpc_pool.pool_for(url))
        if (window_clean and walk_complete and not full_open and not rate_limited
                and not timed_out and _KEY_SCAN_MISS_TTL_S > 0):
            _scan_note(_scan_miss, ck)     # a CLEAN miss only: see _KEY_SCAN_MISS_TTL_S
        elif window_clean and walk_complete and full_open and not rate_limited and not timed_out:
            _note_exhausted(ck, head)      # deeper than the walk: see _KEY_SCAN_EXHAUSTED_TTL_S
            _want_pool_key(chain, pool_id)
        _mark_blind("rpc_rate_limit" if rate_limited
                    else "poolkey_read_timeout" if timed_out else "poolkey_read_failed")
        return None
    with _full_range_lock:
        _scan_depth.pop(ck, None)          # found: the walk's memory has nothing left to save
    key = decode_initialize_log(logs[0])
    if key is None:
        _mark_blind("poolkey_malformed")
        return None
    _key_cache[ck] = key
    _store_pool_key(chain, pool_id, key)
    return key


def _encode_pool_key(key):
    ts = key["tick_spacing"]
    return (_wa(key["currency0"]) + _wa(key["currency1"]) + _w(key["fee"])
            + format(ts % (1 << 256), "064x") + _wa(key["hooks"]))


class PinnedQuoteBlind(Exception):
    """A pinned quoter call that could not be READ (transport, timeout, rate limit, 5xx). It is
    not "this pool does not quote": the dip watch pauses its chain at once on it, because the
    funded read pool it shares with exits is in trouble."""

    def __init__(self, failure_type):
        super().__init__(failure_type)
        self.failure_type = failure_type


def pinned_blind(exc):
    """The typed blind for a failed pinned read. A rate limit is the read layer's HTTP 429
    (`http_status` on its error) or an error that says so; everything else is transport."""
    text = str(exc).lower()
    if isinstance(exc, TimeoutError):
        kind = "timeout"
    elif (getattr(exc, "http_status", None) == 429 or "429" in text or "rate limit" in text
          or "too many requests" in text):
        kind = "rpc_rate_limit"
    else:
        kind = getattr(exc, "failure_type", None) or "rpc_transport"
    return PinnedQuoteBlind(str(kind))


def swap_native_in(sqrt_price, liquidity, amount_in):
    """(sqrt price after, token out) of an exact-in swap of `amount_in` native (currency0) inside
    one liquidity range. `amount_in` is what the POOL sees: every fee (LP, protocol, hook, Relay,
    Kyber) already taken off. Rounds as the pool does. None for an empty pool or a zero input.
    Reproduces real 4663 tape swaps exactly (tests); a swap that crosses a tick is not modelled."""
    sqrt_price, liquidity, amount_in = int(sqrt_price), int(liquidity), int(amount_in)
    if sqrt_price <= 0 or liquidity <= 0 or amount_in <= 0:
        return None
    num = liquidity << 96
    after = -(-num * sqrt_price // (num + amount_in * sqrt_price))        # rounds up
    return after, liquidity * (sqrt_price - after) >> 96


def swap_one_for_zero(sqrt_price, liquidity, amount_in):
    """(sqrt price after, token0 out) of an exact-in swap of `amount_in` currency1 inside one
    liquidity range, rounded as the pool rounds (getNextSqrtPriceFromAmount1RoundingDown, then
    getAmount0Delta rounding down). `amount_in` is what the POOL sees. None for an empty pool or a
    zero input; a swap that crosses a tick is not modelled (as swap_native_in)."""
    sqrt_price, liquidity, amount_in = int(sqrt_price), int(liquidity), int(amount_in)
    if sqrt_price <= 0 or liquidity <= 0 or amount_in <= 0:
        return None
    after = sqrt_price + (amount_in << 96) // liquidity
    return after, ((liquidity << 96) * (after - sqrt_price) // after) // sqrt_price


def _lp_pips(key):
    # The pool keeps the input net of its LP fee (Gamma LOW). A dynamic-fee pool (flag 0x800000) is
    # priced by its hook, so nothing is known to deduct; a protocol fee is left inside the haircut.
    fee = int(key["fee"])
    return fee if fee < _V4_DYNAMIC_FEE_FLAG else 0


def _leg_out(row, key, amount_in, in_is_c0):
    """What `amount_in` buys in one V4 pool at `row`'s state, net of its LP fee; None for an empty
    pool. One range, as `swap_native_in`: a swap that crosses a tick is not modelled."""
    net = int(amount_in) * (10 ** 6 - _lp_pips(key)) // 10 ** 6
    got = (swap_native_in if in_is_c0 else swap_one_for_zero)(row["sqrt"], row["liquidity"], net)
    return None if got is None else got[1]


def source_pool_est_out(clip_wei, b_row, b_key, b_in_is_c0, a_row=None, a_key=None):
    """Coin atomic the clip buys through the source's pool at its post-swap state (`b_row`), after
    hop A at its pre-trade state on a bridge (native is A's currency0), net of each LP fee and the B
    hook's take of the output (MEMECOIN_RH_FAST_HOOK_OUTPUT_TAKE_BPS, estimate only, Gamma F2). None
    when a leg has no liquidity. One range only, known hooks only: tick crossing is not
    modeled and this is never an executable quote."""
    from sniper.settings import memecoin as cfg
    amount = clip_wei
    if a_row is not None:
        amount = _leg_out(a_row, a_key, clip_wei, True)
        if not amount:
            return None
    out = _leg_out(b_row, b_key, amount, b_in_is_c0)
    if out is None:
        return None
    take = int(cfg.MEMECOIN_RH_FAST_HOOK_OUTPUT_TAKE_BPS.get(str(b_key.get("hooks") or "").lower(), 0))
    return out * (10_000 - take) // 10_000


def _quote_single(chain, key, zero_for_one, amount_in, rpc_url, http=None, *, raise_blind=False):
    """amountOut via the official V4Quoter (eth_call). None = no quote (fail-closed).

    `raise_blind` (the dip watch's pinned tick only) turns a failed READ into `PinnedQuoteBlind`
    instead of None. Every funded caller leaves it off and gets None, as before."""
    data = ("0x" + _SEL_QUOTE_SINGLE + _w(0x20) + _encode_pool_key(key)
            + _w(1 if zero_for_one else 0) + _w(amount_in) + _w(0x100) + _w(0))
    try:
        from sniper.execution.evm_read_rpc import _freshest_read_member
        r = _rpc(_freshest_read_member(rpc_url), "eth_call",
                 [{"to": V4[str(chain)]["quoter"], "data": data}, "latest"], http=http)
    except Exception as exc:
        if raise_blind:
            raise pinned_blind(exc) from exc
        return None
    try:
        if "result" not in r or r["result"] in (None, "0x"):
            return None
        out = int(r["result"][2:66], 16)
        return out if out > 0 else None
    except Exception:
        return None


def _encode_bytes(b):
    return _w(len(b)) + (b.hex() + "0" * 64)[:max(64, (len(b) + 31) // 32 * 64)] if b else _w(0)


def _v4_swap_input(key, zero_for_one, amount_in, min_out, *, take_all_to_sender):
    """abi.encode(bytes actions, bytes[] params) for the V4_SWAP command."""
    if not (0 <= int(amount_in) < (1 << 128) and 0 <= int(min_out) < (1 << 128)):
        raise ValueError("V4 exact-input amounts must fit uint128")
    actions = bytes([_ACT_SWAP_IN_SINGLE, _ACT_SETTLE_ALL,
                     _ACT_TAKE_ALL if take_all_to_sender else _ACT_TAKE])
    cin = key["currency0"] if zero_for_one else key["currency1"]
    cout = key["currency1"] if zero_for_one else key["currency0"]
    # ExactInputSingleParams{poolKey, zeroForOne, uint128 amountIn, uint128 amountOutMinimum, bytes hookData}
    #
    # THIS STRUCT HAS NO `minHopPriceX36`, AND ON 4663/56/5042 THE MULTI-HOP ONE DOES. Do not "fix"
    # this encoder to match `evm_v4_multihop_tx._encode_exact_input`; the two genuinely differ in the
    # DEPLOYED contract. Verified by read-only eth_call on 2026-08-30 (4663): this 5-field single
    # struct (offset 0x120) simulates OK, while the multi-hop `ExactInputParams` REVERTS at four
    # fields and is OK at five. On 1 and 8453 the multi-hop struct is upstream's FOUR fields instead
    # (`_EXACT_INPUT_FOUR_FIELD_CHAINS`, proved on Base state 2026-10-01). Inference from this
    # encoder's layout says the router is an older version; the measurement says otherwise, and the
    # measurement wins.
    # The action decoder consumes abi.encode(ExactInputSingleParams). Because the struct contains
    # dynamic hookData, the first word is the relative offset to the struct body. Omitting it makes
    # CalldataDecoder interpret currency0 as an offset and every V4 swap revert.
    p0 = (_w(0x20) + _encode_pool_key(key) + _w(1 if zero_for_one else 0)
          + _w(amount_in) + _w(min_out) + _w(0x120) + _w(0))
    p1 = _wa(cin) + _w(amount_in)                                   # SETTLE_ALL(currency, max)
    if take_all_to_sender:
        p2 = _wa(cout) + _w(min_out)                                # TAKE_ALL(currency, min)
    else:
        p2 = _wa(cout) + _w(_ADDRESS_THIS) + _w(0)                  # TAKE(currency, router, OPEN_DELTA)
    params = [p0, p1, p2]
    # outer: (bytes actions, bytes[] params)
    head = _w(0x40) + _w(0x40 + 32 + ((len(actions) + 31) // 32) * 32)
    actions_enc = _w(len(actions)) + (actions.hex() + "0" * 64)[:64]
    arr = _w(len(params))
    offs, tails = "", ""
    off = 32 * len(params)
    for p in params:
        blob = _w(len(p) // 2) + p
        offs += _w(off)
        off += len(blob) // 2
        tails += blob
    return head + actions_enc + arr + offs + tails


def _pool_is_hooked(key):
    hooks = str((key or {}).get("hooks") or "").lower()
    return bool(hooks) and hooks != _ZERO_ADDRESS


def _hooked_pool_gas(chain, key):
    # V4RC de6ba7c1: archive replay needs >1.5M (estimate 1,538,862); 2M clears the exact floor.
    if str(chain) == "4663" and str((key or {}).get("hooks") or "").lower() == \
            "0x919eb369e51840f78b9f78e59c47620038bafacc":
        return "2000000"
    return _HOOKED_POOL_GAS


def _pack_v4_swap(actions: bytes, params):
    """abi.encode(bytes actions, bytes[] params) -- the outer envelope every V4_SWAP shares."""
    head = _w(0x40) + _w(0x40 + 32 + ((len(actions) + 31) // 32) * 32)
    actions_enc = _w(len(actions)) + (actions.hex() + "0" * 64)[:64]
    arr = _w(len(params))
    offs, tails = "", ""
    off = 32 * len(params)
    for p in params:
        blob = _w(len(p) // 2) + p
        offs += _w(off)
        off += len(blob) // 2
        tails += blob
    return head + actions_enc + arr + offs + tails


# ExactInputParams HAS A DIFFERENT LAYOUT PER DEPLOYED ROUTER (2026-09-30, chain-1 step 7).
# Ethereum mainnet's Universal Router (0x66a9893c) takes upstream v4-periphery's FOUR fields,
# (currencyIn, PathKey[] path, uint128 amountIn, uint128 amountOutMinimum). Fed the five-field
# form it reads amountIn from the slot holding the bytes offset (0x1a0 = 416 wei) and our amountIn
# as the minimum: V4TooLittleReceived(8e15, ~4.8e8). eth_call on mainnet state: 6 of 6 hooked pools
# over 5 hooks revert on five fields and execute on four. Every other chain keeps five.
# BASE TOO (2026-10-01, OPH 67763): every exit of a hooked Base pool reverted
# V4TooLittleReceived(<our whole balance>, 0) at gasUsed 479,240 -- the same misread. The same
# signed sell re-encoded with four fields executes by eth_call on Base state (estimateGas 538,959)
# and the five-field bytes revert with the identical error. Base's Universal Router is 0x6ff5693b.
_EXACT_INPUT_FOUR_FIELD_CHAINS = frozenset({"1", "8453"})


def _v4_swap_input_path(key, zero_for_one, amount_in, min_out, *, payer_is_user, take_to, chain):
    """V4_SWAP input in the PATH form: [SWAP_EXACT_IN, SETTLE(open), TAKE(open)].

    `chain` is required: the struct layout belongs to the chain's deployed router (above).

    THIS IS THE ONLY SHAPE A HOOKED POOL ON THIS CHAIN ACCEPTS (measured 2026-09-04, HOVER
    0xf7d2a651 on hook 0x4e3468951d). `SWAP_EXACT_IN_SINGLE` reverts with EMPTY data on every
    hooked pool -- from our address, a reference buyer's address and a fresh one, in every
    settle/take combination, at the very block where a real buyer's bytes succeed from our own
    address. Those working bytes decode as this exact layout, and the eth_abi encoding of it is
    byte-equal to them (480 bytes).

    The ExactInputParams struct here has FIVE fields -- (currencyIn, PathKey[] path, bytes,
    uint128 amountIn, uint128 amountOutMinimum) -- with an extra empty `bytes` between the path
    and the amounts. That is the deployed router's layout, not upstream v4-periphery's; see the
    2026-08-30 note on the single-hop encoder for the same finding measured the other way round.

    SETTLE(currency, 0, payerIsUser) and TAKE(currency, to, 0) use the OPEN delta so the hook's
    after-swap delta (it takes its fee as a returns-delta) settles whatever the pool actually
    owes, instead of the pre-hook amount a SETTLE_ALL/TAKE_ALL pair would assert.
    """
    if not (0 <= int(amount_in) < (1 << 128) and 0 <= int(min_out) < (1 << 128)):
        raise ValueError("V4 exact-input amounts must fit uint128")
    cin = key["currency0"] if zero_for_one else key["currency1"]
    cout = key["currency1"] if zero_for_one else key["currency0"]
    tick_spacing = int(key.get("tick_spacing", key.get("tickSpacing")))
    fee = int(key["fee"])
    # struct body: head(5 words) | path array | empty bytes
    #   head: currencyIn, off(path)=0xa0, off(bytes)=0x1a0, amountIn, amountOutMinimum
    #   path: len=1, off(elem0)=0x20, PathKey{cout, fee, tickSpacing, hooks, off(hookData)=0xa0, len=0}
    #   bytes: len=0
    path_key = (_wa(cout) + _w(fee) + _w(tick_spacing & ((1 << 256) - 1)) + _wa(key["hooks"])
                + _w(0xa0) + _w(0))
    if str(chain) in _EXACT_INPUT_FOUR_FIELD_CHAINS:
        #   head: currencyIn, off(path)=0x80, amountIn, amountOutMinimum | path array
        body = _wa(cin) + _w(0x80) + _w(amount_in) + _w(min_out) + _w(1) + _w(0x20) + path_key
    else:
        body = (_wa(cin) + _w(0xa0) + _w(0x1a0) + _w(amount_in) + _w(min_out)
                + _w(1) + _w(0x20) + path_key
                + _w(0))
    p0 = _w(0x20) + body
    p1 = _wa(cin) + _w(0) + _w(1 if payer_is_user else 0)          # SETTLE(currency, OPEN, payerIsUser)
    p2 = _wa(cout) + (_w(take_to) if isinstance(take_to, int) else _wa(take_to)) + _w(0)  # TAKE(currency, to, OPEN)
    return _pack_v4_swap(bytes([_ACT_SWAP_IN, _ACT_SETTLE, _ACT_TAKE]), [p0, p1, p2])


def _ur_execute_data(commands: bytes, inputs, deadline):
    head = _w(0x60) + _w(0x60 + 32 + ((len(commands) + 31) // 32) * 32) + _w(deadline)
    cmd_enc = _w(len(commands)) + (commands.hex() + "0" * 64)[:max(64, (len(commands) + 31) // 32 * 64)]
    arr = _w(len(inputs))
    offs, tails = "", ""
    off = 32 * len(inputs)
    for p in inputs:
        blob = _w(len(p) // 2) + p
        offs += _w(off)
        off += len(blob) // 2
        tails += blob
    return "0x" + _SEL_UR_EXECUTE + head + cmd_enc + arr + offs + tails


def _permit2_nonce(chain, owner, token, spender, rpc_url, http=None, *, permit2=None):
    data = "0x" + _SEL_P2_ALLOWANCE + _wa(owner) + _wa(token) + _wa(spender)
    try:
        r = _rpc(rpc_url, "eth_call", [{"to": permit2 or V4[str(chain)]["permit2"], "data": data}, "latest"],
                 http=http)
        blob = r["result"][2:]
        return int(blob[128:192], 16)
    except Exception:
        return None


def _permit2_input(chain, acct, token, spender, amount, rpc_url, http=None, now_fn=None, nonce=None,
                   *, permit2=None):
    """PERMIT2_PERMIT input: abi.encode(PermitSingle, bytes signature). None on failure. `nonce`
    skips the read when the caller already has it (the in-swap unwrap reads it beside the quote)."""
    if not (0 <= int(amount) < (1 << 160)):
        return None
    if nonce is None:
        nonce = _permit2_nonce(chain, acct.address, token, spender, rpc_url, http=http,
                               **({"permit2": permit2} if permit2 else {}))
    if nonce is None:
        return None
    if now_fn is None:
        from sniper.util.clock import real_utc_now
        now_fn = real_utc_now
    deadline = int(now_fn()) + 1800
    permit = {
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"},
                             {"name": "chainId", "type": "uint256"},
                             {"name": "verifyingContract", "type": "address"}],
            "PermitSingle": [{"name": "details", "type": "PermitDetails"},
                             {"name": "spender", "type": "address"},
                             {"name": "sigDeadline", "type": "uint256"}],
            "PermitDetails": [{"name": "token", "type": "address"},
                              {"name": "amount", "type": "uint160"},
                              {"name": "expiration", "type": "uint48"},
                              {"name": "nonce", "type": "uint48"}],
        },
        "primaryType": "PermitSingle",
        "domain": {"name": "Permit2", "chainId": int(chain),
                   "verifyingContract": permit2 or V4[str(chain)]["permit2"]},
        "message": {"details": {"token": token, "amount": int(amount),
                                "expiration": deadline, "nonce": nonce},
                    "spender": spender, "sigDeadline": deadline},
    }
    try:
        from eth_account import Account
        sig = Account.sign_typed_data(acct.key, full_message=permit).signature
    except Exception:
        return None
    sig_hex = sig.hex() if not isinstance(sig, str) else sig.replace("0x", "")
    body = (_wa(token) + _w(int(amount)) + _w(deadline) + _w(nonce)
            + _wa(spender) + _w(deadline) + _w(0xE0)
            + _w(len(sig_hex) // 2) + (sig_hex + "0" * 64)[:((len(sig_hex) // 2 + 31) // 32) * 64])
    return body


def _quote_side(key, mint, weth, weth_currency_enabled):
    """(quote_currency, mint_is_c1) when `key` pairs `mint` with our bankroll currency at a sane
    fee, else None. V4 PoolKeys are ADDRESS-SORTED: native (0x00..0) is always currency0, but the
    WETH token can land on either side depending on the mint's address -- so both orderings are
    handled, and `zero_for_one` follows the ordering rather than the direction."""
    c0, c1 = key["currency0"].lower(), key["currency1"].lower()
    if c0 == _NATIVE and c1 == mint:
        side = (_NATIVE, True)
    elif weth_currency_enabled and c0 == weth and c1 == mint:
        side = (weth, True)
    elif weth_currency_enabled and c1 == weth and c0 == mint:
        side = (weth, False)
    else:
        return None                        # foreign quote currency (USDG, SPY, ...)
    if not _fee_is_sane(key["fee"]):
        return None                        # extortionate-fee trap: buyable, never sellable
    return side


def v4_pinned_pool_quote(chain, pool_id, key, sell_token, buy_token, amount_atomic, *, rpc_url,
                         http=None):
    """`v4_quote_amount_out`'s answer for ONE pool whose key the caller already holds, or None
    when the pool does not pair the coin with our bankroll currency or does not quote. No
    discovery: one V4Quoter eth_call (the source-route hint, 2026-10-15). The side and fee rules
    are `v4_quote_amount_out`'s own (`_quote_side`). Raises `PinnedQuoteBlind` when the quoter READ
    fails, so a transport fault is never read as "this pool does not quote"."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE, native_is_quote
    from sniper.settings import memecoin as _cfg
    chain = str(chain)
    quote_meta = CHAIN_QUOTE.get(chain) or {}
    weth = (quote_meta.get("token") or "").lower()
    if chain not in V4 or not rpc_url or not weth or not isinstance(key, dict):
        return None
    amount_atomic = int(amount_atomic)
    if not 0 < amount_atomic < (1 << 128):
        return None
    native_quote = native_is_quote(chain)
    native_scale = 10 ** (18 - int(quote_meta["decimals"])) if native_quote else 1
    weth_currency_enabled = (bool(getattr(_cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY", False))
                             or chain in getattr(_cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY_CHAINS", ())
                             or native_quote)
    is_buy = str(sell_token).lower() == weth
    mint = (str(buy_token) if is_buy else str(sell_token)).lower()
    side = _quote_side(key, mint, weth, weth_currency_enabled)
    if side is None:
        return None
    quote_currency, mint_is_c1 = side
    zf1 = (is_buy == mint_is_c1)
    pool_input = amount_atomic * native_scale if is_buy and quote_currency == _NATIVE else amount_atomic
    if pool_input >= (1 << 128):
        return None
    out = _quote_single(chain, key, zf1, pool_input, rpc_url, http=http, raise_blind=True)
    if out and not is_buy and quote_currency == _NATIVE:
        out //= native_scale
    if not out:
        return None
    return {"amount_out": out, "pool_id": str(pool_id).lower(), "pool_key": key,
            "zero_for_one": zf1, "pool_fee": key["fee"], "quote_currency": quote_currency}


def v4_quote_amount_out(chain, sell_token, buy_token, amount_atomic, *,
                        rpc_url=None, http=None, gt_http=None):
    """Pure V4 amountOut evidence via the official V4Quoter eth_call. Never requires an account,
    permit, or signature — quote facts only. Returns
    ``{'amount_out': int, 'pool_key': dict, 'zero_for_one': bool, 'pool_fee': int}`` for the deepest
    native-paired single-hop pool, or ``None`` (fail-closed: no pool / no quote / bad amount)."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE, native_is_quote
    chain = str(chain)
    begin_quote()
    conf = V4.get(chain)
    quote_meta = CHAIN_QUOTE.get(chain) or {}
    weth = (quote_meta.get("token") or "").lower()
    if not conf or not rpc_url or not weth:
        return None
    amount_atomic = int(amount_atomic)
    if amount_atomic <= 0 or amount_atomic >= (1 << 128):
        return None
    from sniper.settings import memecoin as _cfg
    # STEP 0 (2026-08-30, default OFF). Also quote pools whose currency is the WETH ERC-20, not
    # only native. We unwrap WETH to native and quoted native ONLY, so a WETH-token pool was
    # invisible. Measured on armed-wallet 4663 coins (n=22): 55% have a sane native pool, 36% have
    # no sane ETH-side pool, and 9% have ONLY a sane WETH-token one. Mint 0x94d84dac ("JOE") is in
    # that 9% -- its single honest ETH-side pool is WETH-token at 7% while all eight of its NATIVE
    # pools charge 15% to 93%, so we refused it as no_route while looking straight at a route.
    weth_currency_enabled = (bool(getattr(_cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY", False))
                             or str(chain) in getattr(_cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY_CHAINS", ()))
    native_quote = native_is_quote(chain)
    native_scale = 10 ** (18 - int(quote_meta["decimals"])) if native_quote else 1
    weth_currency_enabled = weth_currency_enabled or native_quote
    is_buy = str(sell_token).lower() == weth
    mint = (str(buy_token) if is_buy else str(sell_token)).lower()
    best = None   # (amount_out, pool_id, key, zero_for_one, quote_currency)
    found = []    # every candidate that quoted: the ladder's over-budget fallback (KLN, 2026-10-08)
    for pid in _quote_pools_for_mint(chain, mint, gt_http=gt_http, rpc_url=rpc_url, http=http):
        key = _pool_key(chain, pid, rpc_url, http=http)
        if not key:
            continue
        side = _quote_side(key, mint, weth, weth_currency_enabled)
        if side is None:
            continue                       # foreign quote currency or a fee trap -> B and C
        quote_currency, mint_is_c1 = side
        # Buying the coin moves quote_currency -> mint. That is currency0 -> currency1 (True) only
        # when the MINT is currency1. Selling inverts it. Getting this backwards still returns a
        # number, which is why every ordering is pinned by test.
        zf1 = (is_buy == mint_is_c1)
        pool_input = amount_atomic * native_scale if is_buy and quote_currency == _NATIVE else amount_atomic
        if pool_input >= (1 << 128):
            continue
        out = _quote_single(chain, key, zf1, pool_input, rpc_url, http=http)
        if out and not is_buy and quote_currency == _NATIVE:
            out //= native_scale
        if out:
            found.append((out, pid, key, zf1, quote_currency))
        if out and (best is None or out > best[0]):
            best = (out, pid, key, zf1, quote_currency)
    if best is None:
        return None
    amount_out, pool_id, key, zf1, quote_currency = best
    # THE OTHER CANDIDATES, already quoted, in descending output. The max output can be a stale thin
    # decoy right after a move in the real pool (KLN 0x2cadc4f2: the 8% decoy 241.7 vs the hooked
    # pool 204.6, round trips -33.5% vs -2.2%). The ladder measures them only when the winner fails
    # its round-trip budget. No new call.
    alternates = [{"amount_out": o, "pool_id": p, "pool_key": k, "zero_for_one": z, "pool_fee": k["fee"],
                   "quote_currency": c}
                  for o, p, k, z, c in sorted(found, key=lambda f: -f[0]) if p != pool_id]
    # `quote_currency` is REQUIRED downstream: the exit must traverse the currency the entry used.
    # A coin with an honest WETH pool and trap native pools would otherwise exit through the trap
    # it avoided on entry.
    return {"amount_out": amount_out, "pool_id": pool_id, "pool_key": key,
            "zero_for_one": zf1, "pool_fee": key["fee"], "quote_currency": quote_currency,
            "alternates": alternates}


def v4_direct_quote(chain, key, pool_id, sell_token, buy_token, amount_atomic, *, rpc_url, http=None):
    """`v4_quote_amount_out`'s answer for ONE native-ETH pool whose key the caller already holds,
    or None. No discovery: one V4Quoter eth_call on `rpc_url` (the 4663 fast lane's safety cache
    passes the research host; the key comes from the detector's tape). The entry's side and fee
    rules: native currency0, the mint as currency1, a sane fee."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    chain = str(chain)
    weth = ((CHAIN_QUOTE.get(chain) or {}).get("token") or "").lower()
    sell, buy = str(sell_token).lower(), str(buy_token).lower()
    is_buy = sell == weth
    mint = buy if is_buy else sell
    if (str(key.get("currency0")).lower() != _NATIVE or str(key.get("currency1")).lower() != mint
            or (buy if not is_buy else sell) != weth or not _fee_is_sane(key.get("fee"))):
        return None
    amount_atomic = int(amount_atomic)
    if not 0 < amount_atomic < (1 << 128):
        return None
    out = _quote_single(chain, key, is_buy, amount_atomic, rpc_url, http=http)
    if not out:
        return None
    return {"amount_out": out, "pool_id": pool_id, "pool_key": key, "zero_for_one": is_buy,
            "pool_fee": key["fee"], "quote_currency": _NATIVE}


def pinned_buy_out(chain, mint, amount_atomic, rpc_url, *, pool_ids=None, http=None, max_pools=8):
    """(tokens out, pool id) of the best BUY through pools whose key we ALREADY hold, else
    (None, None). The dip watch's on-chain tick (2026-09-30, operator order 86d053ce).

    NO DISCOVERY AND NO PROVIDER: the pool ids are the caller's (`pool_ids`: the refusal's own
    pool) or the durable key table's rows for this mint, and every key comes from the cache. So
    no GeckoTerminal or DexScreener pool list is ever read here, which `_pools_for_mint` does.
    One quoter `eth_call` per pool. The side and fee rules are the entry's own
    (`v4_quote_amount_out`): native or the chain's WETH on the other side, a sane fee.

    Raises `PinnedQuoteBlind` when a quoter READ fails (transport, timeout, rate limit): a pool
    that cannot be read is not a pool that does not quote.
    """
    from sniper.execution.evm_wallet import CHAIN_QUOTE, native_is_quote
    from sniper.settings import memecoin as _cfg
    chain, mint = str(chain), str(mint).lower()
    quote_meta = CHAIN_QUOTE.get(chain) or {}
    weth = (quote_meta.get("token") or "").lower()
    try:
        amount = int(amount_atomic)
    except (TypeError, ValueError):
        return None, None
    if chain not in V4 or not rpc_url or not weth or amount <= 0:
        return None, None
    native_quote = native_is_quote(chain)
    scale = 10 ** (18 - int(quote_meta["decimals"])) if native_quote else 1
    weth_ok = (native_quote or bool(getattr(_cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY", False))
               or chain in getattr(_cfg, "MEMECOIN_EVM_V4_WETH_CURRENCY_CHAINS", ()))
    pinned = pool_ids is not None
    candidates = list(pool_ids if pinned else _stored_pools_for_mint(chain, mint))
    if len(candidates) > max_pools:
        # MORE HELD POOLS THAN WE WILL QUOTE: take the deepest on our OWN tape (a local read, no
        # provider), known depths before unknown. By pool id the real pool could sit past the cut.
        depth = _own_depths(chain, candidates)
        candidates.sort(key=lambda pid: (depth.get(pid) is None, -(depth.get(pid) or 0.0)))
    best = (None, None)
    for pid in candidates[:max_pools]:
        key = _pool_key_cached(chain, pid)
        if not key:
            continue
        c0, c1 = key["currency0"].lower(), key["currency1"].lower()
        other = c0 if c1 == mint else c1 if c0 == mint else None
        # The refusal's own pool was admitted by the entry; a stored pool must pass its rules.
        if other is None or not _fee_is_sane(key["fee"]) or not (
                other == _NATIVE or (other == weth and (weth_ok or pinned))):
            continue
        pool_input = amount * scale if other == _NATIVE else amount
        if pool_input >= (1 << 128):
            continue
        out = _quote_single(chain, key, c1 == mint, pool_input, rpc_url, http=http,
                            raise_blind=True)
        if out and (best[0] is None or out > best[0]):
            best = (out, str(pid).lower())
    return best


# THE IN-SWAP UNWRAP READS ITS PERMIT2 NONCE BESIDE THE QUOTE (2026-09-28, spec 4663 V4 in-swap
# unwrap). A new serial provider read on the entry path is refused by default; this one runs while
# the quoter answers, so its serial cost is whatever the quote does not already cover. A shared pool,
# because a thread per quote is the same work with less bound; the wait is bounded too.
_PERMIT_NONCE_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="v4-permit-nonce")
_PERMIT_NONCE_WAIT_S = 2.0


def _in_swap_unwrap_start(chain, acct, weth, router, rpc_url, http):
    """(nonce future, None) when this buy may unwrap inside the swap; (None, why) when the knob names
    the chain but the buy cannot; (None, None) when the knob is off for it."""
    from sniper.settings import memecoin as cfg
    if str(chain) not in cfg.memecoin_evm_v4_in_swap_unwrap_chains():
        return None, None
    if acct is None:
        return None, "no_signer"
    # A receipt-first approval (~2.4 s on BSC) costs MORE than the unwrap it replaces.
    if not cfg.memecoin_evm_approval_pipeline(chain):
        return None, "approval_not_pipelined"
    return _PERMIT_NONCE_POOL.submit(_permit2_nonce, chain, acct.address, weth, router, rpc_url,
                                     http=http), None


def dex_v4_quote(chain, sell_token, buy_token, amount_atomic, taker, **kwargs):
    """`_dex_v4_quote`, with this thread's E10 ranking record of the coin attached to the quote it
    won as `_sniper_v4_ranking` (phase 1, observation only). The executor copies it and the won
    pool onto the entry trace; nothing reads it to decide."""
    _blind.v4_alternates = None
    out = _dex_v4_quote(chain, sell_token, buy_token, amount_atomic, taker, **kwargs)
    ranking = take_v4_ranking(buy_token) or take_v4_ranking(sell_token)
    alternates, _blind.v4_alternates = getattr(_blind, "v4_alternates", None), None
    if (ranking is not None or alternates) and isinstance(out, dict):
        out = dict(out)
        if ranking is not None:
            out["_sniper_v4_ranking"] = ranking
        if alternates:
            # Private: quote_fields never persists it. The ladder's over-budget fallback reads it.
            out["_v4_alternates"] = alternates
    return out


def _dex_v4_quote(chain, sell_token, buy_token, amount_atomic, taker, *, http=None,
                  slippage_bps=None, acct=None, rpc_url=None, gt_http=None, now_fn=None,
                  quote_override=None):
    """0x-shaped V4 quote+tx via the Universal Router. None = fail-closed (no pool/quote/signer).

    The quote-token side maps to NATIVE (V4 pools here are native-paired; our bankroll token is
    the chain WETH — buys carry `pre_transactions` unwrapping it first)."""
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import CHAIN_QUOTE, native_is_quote
    chain = str(chain)
    conf = V4.get(chain)
    quote_meta = CHAIN_QUOTE.get(chain) or {}
    weth = (quote_meta.get("token") or "").lower()
    if not conf or not rpc_url or not weth:
        return None
    amount_atomic = int(amount_atomic)
    if amount_atomic <= 0 or amount_atomic >= (1 << 128):
        return None
    if slippage_bps is None:
        slippage_bps = int(round(cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT * 100))
    is_buy = str(sell_token).lower() == weth
    nonce_job, in_swap_why = ((None, None) if not is_buy or native_is_quote(chain) else
                              _in_swap_unwrap_start(chain, acct, weth, conf["universal_router"],
                                                    rpc_url, http))
    # `quote_override` (the 4663 fast lane's safety cache): a `v4_direct_quote` answer for a key
    # the caller already holds, so no pool discovery runs. None (every other caller) is unchanged.
    quote = quote_override if quote_override is not None else v4_quote_amount_out(
        chain, sell_token, buy_token, amount_atomic, rpc_url=rpc_url, http=http, gt_http=gt_http)
    if quote is None:
        return None
    if quote_override is None and is_buy:
        # BUYS ONLY: the ladder's over-budget fallback reads them; an exit quote stays byte-identical.
        _blind.v4_alternates = quote.get("alternates") or None    # dex_v4_quote attaches it
    amount_out, key, zf1 = quote["amount_out"], quote["pool_key"], quote["zero_for_one"]
    # STEP 0: which currency the quote actually picked. The BUY transaction shape depends on it,
    # and getting that wrong is not a missed fill, it is a malformed funded transaction --
    # unwrapping WETH to native and then paying native into a pool whose currency is the WETH
    # ERC-20. Older callers/tests that predate the field default to native, the previous behaviour.
    quote_currency = str(quote.get("quote_currency") or _NATIVE).lower()
    weth_route = quote_currency == weth
    native_quote = native_is_quote(chain)
    native_scale = 10 ** (18 - int(quote_meta["decimals"])) if native_quote else 1
    pool_input = amount_atomic * native_scale if is_buy and not weth_route else amount_atomic
    # Return quote-token units to the caller, but encode the pool's native units.
    output_scale = native_scale if not is_buy and not weth_route else 1
    min_out = amount_out * (10_000 - int(slippage_bps)) // 10_000
    if min_out <= 0 or min_out * output_scale >= (1 << 128) or pool_input >= (1 << 128):
        return None
    if now_fn is None:
        from sniper.util.clock import real_utc_now
        now_fn = real_utc_now
    from sniper.settings import memecoin as _dl_cfg
    deadline = int(now_fn()) + _dl_cfg.memecoin_evm_swap_deadline_s(chain, 600)
    ur = conf["universal_router"]

    if is_buy and _pool_is_hooked(key):
        # HOOKED POOL BUY (2026-09-04): fund the router with NATIVE and let it settle from its own
        # balance. We hold WETH, so the native branch's WETH.withdraw pre-transaction runs first;
        # on a WETH-currency pool the router then re-wraps inside the same call (WRAP_ETH to
        # ADDRESS_THIS) and pays the pool in WETH. One unwrap and one wrap per entry is the price
        # of the only shape this hook executes -- the Permit2-funded WETH branch below has never
        # produced a fill (0 of 59 V4 fills on record), and on a hooked pool it reverts bare.
        swap_input = _v4_swap_input_path(key, zf1, pool_input, min_out * output_scale,
                                         payer_is_user=False, take_to=taker, chain=chain)
        if nonce_job is not None:
            # IN-SWAP UNWRAP (2026-09-28, proven by eth_call on live hooked pools of both
            # currencies: scripts/research/v4_in_swap_unwrap_proof.py). Pull the WETH into the
            # router through Permit2, unwrap it there for a native pool, and let the unchanged
            # V4_SWAP settle from the router's own balance. One transaction, value 0; the
            # executor's exact WETH->Permit2 approval (pipelined) replaces the receipt-first unwrap.
            from sniper.execution.evm_wallet import http_tap
            try:
                # THE WAIT IS TIMED ON THE RUNG'S OWN TAP (Gamma, 2026-10-08): the read runs on the
                # nonce pool's thread, so the rung's RPC tap never saw it. Instrumentation only.
                with http_tap("v4.permit2_nonce_wait"):
                    nonce = nonce_job.result(timeout=_PERMIT_NONCE_WAIT_S)
            except Exception:
                nonce = None
            permit = None if nonce is None else _permit2_input(
                chain, acct, weth, ur, amount_atomic, rpc_url, http=http,
                now_fn=now_fn, nonce=nonce)
            if permit is None:
                in_swap_why = "permit_unavailable"
            else:
                pull = _wa(weth) + _w(_ADDRESS_THIS) + _w(amount_atomic)
                commands, inputs = [_CMD_PERMIT2_PERMIT, _CMD_PERMIT2_TRANSFER_FROM], [permit, pull]
                if not weth_route:
                    commands.append(_CMD_UNWRAP_WETH)
                    inputs.append(_w(_ADDRESS_THIS) + _w(amount_atomic))
                data = _ur_execute_data(bytes(commands + [_CMD_V4_SWAP]), inputs + [swap_input],
                                        deadline)
                return {
                    "sellAmount": str(amount_atomic), "buyAmount": str(amount_out),
                    "minBuyAmount": str(min_out),
                    "liquidityAvailable": True,
                    "pre_transactions": [],
                    "transaction": {"to": ur, "data": data, "value": "0x0", "gas": _hooked_pool_gas(chain, key)},
                    "issues": {"allowance": {"spender": conf["permit2"]}},
                    "route": {"protocol": "uniswap_v4", "pool_id": quote["pool_id"],
                              "pool_fee": key["fee"], "hooked": True,
                              "quote_currency": quote_currency, "in_swap_unwrap": True},
                }
        if weth_route and not native_quote:
            wrap_input = _w(_ADDRESS_THIS) + _w(amount_atomic)
            data = _ur_execute_data(bytes([_CMD_WRAP_ETH, _CMD_V4_SWAP]),
                                    [wrap_input, swap_input], deadline)
        else:
            data = _ur_execute_data(bytes([_CMD_V4_SWAP]), [swap_input], deadline)
        return {
            "sellAmount": str(amount_atomic), "buyAmount": str(amount_out),
            "minBuyAmount": str(min_out),
            "liquidityAvailable": True,
            "pre_transactions": [] if native_quote else [{"to": quote_meta["token"],
                                  "data": "0x" + _SEL_WETH_WITHDRAW + _w(amount_atomic),
                                  "value": "0x0", "gas": str(WETH_CALL_GAS_LIMIT)}],
            "transaction": {"to": ur, "data": data, "value": hex(amount_atomic * native_scale),
                            "gas": _hooked_pool_gas(chain, key)},
            "issues": {"allowance": None},
            "route": {"protocol": "uniswap_v4", "pool_id": quote["pool_id"],
                      "pool_fee": key["fee"], "hooked": True,
                      "quote_currency": quote_currency,
                      **({"in_swap_unwrap_fallback": in_swap_why} if in_swap_why else {})},
        }

    if is_buy and weth_route:
        # WETH-CURRENCY BUY: pay the ERC-20 directly. No WETH.withdraw, and value=0 -- the pool's
        # currency IS the token, so unwrapping would leave us paying the wrong asset. Costs one
        # fewer pre-transaction than the native path; needs a permit2 because an ERC-20 cannot be
        # sent as tx value. Fail-closed without a signer, exactly as the SELL path does.
        if acct is None:
            return None
        permit_input = _permit2_input(
            chain, acct, quote_meta["token"], ur, amount_atomic, rpc_url, http=http, now_fn=now_fn,
        )
        if permit_input is None:
            return None
        swap_input = _v4_swap_input(key, zf1, pool_input, min_out * output_scale, take_all_to_sender=True)
        data = _ur_execute_data(bytes([_CMD_PERMIT2_PERMIT, _CMD_V4_SWAP]),
                                [permit_input, swap_input], deadline)
        return {
            "sellAmount": str(amount_atomic), "buyAmount": str(amount_out),
            "minBuyAmount": str(min_out),
            "liquidityAvailable": True,
            "pre_transactions": [],
            "transaction": {"to": ur, "data": data, "value": "0x0", "gas": "700000"},
            # THE ERC-20 MUST APPROVE PERMIT2 FIRST. A Permit2 permit only works once the token
            # has approved Permit2 as spender, and the executor sets that approval ONLY when a
            # quote declares one. Declaring None here (copied from the NATIVE buy, where it is
            # correct because native has no allowance concept) asked the router to run a permit
            # that could not succeed, and the swap would have reverted on-chain. Same declaration
            # the SELL branch already makes, for the same reason.
            "issues": {"allowance": {"spender": conf["permit2"]}},
            "route": {"protocol": "uniswap_v4", "pool_id": quote["pool_id"],
                      "pool_fee": key["fee"],
                      "quote_currency": quote_currency},
        }

    if is_buy:
        swap_input = _v4_swap_input(key, zf1, pool_input, min_out * output_scale, take_all_to_sender=True)
        data = _ur_execute_data(bytes([_CMD_V4_SWAP]), [swap_input], deadline)
        return {
            "sellAmount": str(amount_atomic), "buyAmount": str(amount_out),
            "minBuyAmount": str(min_out),
            "liquidityAvailable": True,
            "pre_transactions": [] if native_quote else [{"to": quote_meta["token"],
                                  "data": "0x" + _SEL_WETH_WITHDRAW + _w(amount_atomic),
                                  "value": "0x0", "gas": str(WETH_CALL_GAS_LIMIT)}],
            "transaction": {"to": ur, "data": data, "value": hex(amount_atomic * native_scale), "gas": "700000"},
            "issues": {"allowance": None},
            "route": {"protocol": "uniswap_v4", "pool_id": quote["pool_id"],
                      "pool_fee": key["fee"],
                      "quote_currency": quote_currency},
        }
    # SELL: permit2-permit + swap(token->native taken to router) + wrap router balance to us
    if acct is None:
        return None
    permit_input = _permit2_input(
        chain, acct, sell_token, ur, amount_atomic, rpc_url, http=http, now_fn=now_fn,
    )
    if permit_input is None:
        return None
    # THE EXIT IS DELIBERATELY NOT BOUND TO THE ENTRY'S CURRENCY (ruling 2026-08-30). It resolves
    # its own pool through the SAME `_fee_is_sane` screen, so it cannot reach a fee trap whatever
    # the entry did — the screen refuses one on its own merits, which is a stronger guarantee than
    # remembering a decision. Binding the exit to the entry currency would be WORSE: it would force
    # an exit through that currency even when a better sane pool exists at exit time, turning a
    # safety rule into a self-inflicted slippage cost. Best executable output at exit is the rule.
    # `route.quote_currency` is therefore EVIDENCE, not a constraint. Do not "fix" this into a
    # binding.
    #
    # DIRECTION COMES FROM THE QUOTE, never a constant. This was hardcoded False, which is right
    # only when the mint is currency1 — always true for NATIVE pools (native sorts first) and NOT
    # true for a WETH-currency pool whose mint sorts below the WETH address. A hardcoded direction
    # quotes one side of the pool and swaps the other.
    if _pool_is_hooked(key):
        # HOOKED POOL EXIT (2026-09-04): the same path form, paid by us through the permit
        # (payerIsUser). A WETH-currency pool yields the ERC-20 straight to the sender; a native
        # pool takes to the router and wraps, exactly as the plain branches below. This mirrors
        # the reference sell that landed on chain through this hook (actions 07 0b 0e).
        if weth_route or native_quote:
            swap_input = _v4_swap_input_path(key, zf1, pool_input, min_out * output_scale,
                                             payer_is_user=True, take_to=taker, chain=chain)
            data = _ur_execute_data(bytes([_CMD_PERMIT2_PERMIT, _CMD_V4_SWAP]),
                                    [permit_input, swap_input], deadline)
        else:
            swap_input = _v4_swap_input_path(key, zf1, pool_input, min_out * output_scale,
                                             payer_is_user=True, take_to=_ADDRESS_THIS, chain=chain)
            wrap_input = _w(_MSG_SENDER) + _w(_CONTRACT_BALANCE)
            data = _ur_execute_data(bytes([_CMD_PERMIT2_PERMIT, _CMD_V4_SWAP, _CMD_WRAP_ETH]),
                                    [permit_input, swap_input, wrap_input], deadline)
        return {
            "sellAmount": str(amount_atomic), "buyAmount": str(amount_out),
            "minBuyAmount": str(min_out),
            "liquidityAvailable": True,
            "transaction": {"to": ur, "data": data, "value": "0x0", "gas": _hooked_pool_gas(chain, key)},
            "issues": {"allowance": {"spender": conf["permit2"]}},
            "route": {"protocol": "uniswap_v4", "pool_id": quote["pool_id"],
                      "pool_fee": key["fee"], "hooked": True,
                      "quote_currency": quote_currency},
        }
    if weth_route or native_quote:
        # WETH-CURRENCY EXIT: the swap already yields the WETH ERC-20, so there is no native
        # balance to wrap. Emitting WRAP_ETH here would wrap nothing and the proceeds would be
        # left sitting in the router. Take them straight to the sender instead.
        swap_input = _v4_swap_input(key, zf1, pool_input, min_out * output_scale, take_all_to_sender=True)
        data = _ur_execute_data(bytes([_CMD_PERMIT2_PERMIT, _CMD_V4_SWAP]),
                                [permit_input, swap_input], deadline)
    else:
        swap_input = _v4_swap_input(key, zf1, pool_input, min_out * output_scale, take_all_to_sender=False)
        wrap_input = _w(_MSG_SENDER) + _w(_CONTRACT_BALANCE)
        data = _ur_execute_data(bytes([_CMD_PERMIT2_PERMIT, _CMD_V4_SWAP, _CMD_WRAP_ETH]),
                                [permit_input, swap_input, wrap_input], deadline)
    return {
        "sellAmount": str(amount_atomic), "buyAmount": str(amount_out),
        "minBuyAmount": str(min_out),
        "liquidityAvailable": True,
        "transaction": {"to": ur, "data": data, "value": "0x0", "gas": "700000"},
        "issues": {"allowance": {"spender": conf["permit2"]}},
        "route": {"protocol": "uniswap_v4", "pool_id": quote["pool_id"],
                  "pool_fee": key["fee"],
                  "quote_currency": quote_currency},
    }


def weth_buy_tx(key, zero_for_one, amount_wei, min_out, deadline, *, taker, chain="4663"):
    """The unsigned UR [WRAP_ETH, V4_SWAP] buy of a WETH-quoted pool's coin, paid by value: the router
    wraps `amount_wei` to WETH it holds itself and settles the pool from its own balance, path form
    (4663 fast lane routes R3). The same shape today's hooked-pool buy sends on a WETH-currency pool.
    No approval, no Permit2. Raises ValueError unless the input side is the chain's WETH and min_out > 0."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    weth = str((CHAIN_QUOTE.get(str(chain)) or {}).get("token") or "").lower()
    cin = str(key["currency0"] if zero_for_one else key["currency1"]).lower()
    amount_wei, min_out = int(amount_wei), int(min_out)
    if not weth or cin != weth:
        raise ValueError("not a WETH-quoted buy")
    if not (0 < amount_wei < (1 << 128) and 0 < min_out < (1 << 128)):
        raise ValueError("amount and min_out must be positive uint128")
    swap = _v4_swap_input_path(key, zero_for_one, amount_wei, min_out, payer_is_user=False,
                               take_to=taker, chain=chain)
    wrap = _w(_ADDRESS_THIS) + _w(amount_wei)
    return {"to": V4[str(chain)]["universal_router"], "value": hex(amount_wei), "gas": _hooked_pool_gas(chain, key),
            "data": _ur_execute_data(bytes([_CMD_WRAP_ETH, _CMD_V4_SWAP]), [wrap, swap], int(deadline))}


def native_buy_tx(key, amount_wei, min_out, deadline, *, taker, chain="4663"):
    """The unsigned UR [V4_SWAP] buy of `key`'s token for `amount_wei` native, paid by value: the
    4663 fast lane's buy (spec 2026-10-01 fast lane §2.3; eth_call-proven at block 77690971 by
    scripts/research/rh_fast_template_proof.py). No approval, no Permit2, no WETH unwrap.

    A hooked pool takes only the path form (SETTLE/TAKE on the open delta; trap: SWAP_EXACT_IN_SINGLE
    reverts bare on hooked pools). Raises ValueError for anything but a capped native buy: the pool's
    currency0 must be native (native sorts first, so the buy is zeroForOne), and min_out > 0."""
    amount_wei, min_out = int(amount_wei), int(min_out)
    if str(key["currency0"]).lower() != _NATIVE:
        raise ValueError("not a native-ETH pool")
    if not (0 < amount_wei < (1 << 128) and 0 < min_out < (1 << 128)):
        raise ValueError("amount and min_out must be positive uint128")
    if _pool_is_hooked(key):
        swap = _v4_swap_input_path(key, True, amount_wei, min_out, payer_is_user=False,
                                   take_to=taker, chain=chain)
        gas = _hooked_pool_gas(chain, key)
    else:
        swap = _v4_swap_input(key, True, amount_wei, min_out, take_all_to_sender=True)
        gas = "700000"
    return {"to": V4[str(chain)]["universal_router"], "value": hex(amount_wei), "gas": gas,
            "data": _ur_execute_data(bytes([_CMD_V4_SWAP]), [swap], int(deadline))}
