"""Buy and sell a native-quote flap.sh launchpad coin on BSC, on its own bonding curve, before
graduation. Phase 1 of the flap.sh venue: NATIVE-QUOTE mints only (`quoteTokenAddress ==
address(0)`). A non-native-quote mint is refused explicitly, never mis-quoted -- ten of the
thirteen flap.sh coins found in the 2026-09-20 BSC no_route review quote against a non-native
ERC-20, each a different token, and unwinding that on exit is a separate, unbuilt scope.

flap.sh trades through ONE shared "Portal" contract for every token on the chain -- there is no
per-token curve contract the way evm_launchpad_curve.py's chain-4663 template assumes. Both
directions go through the same function:

    swapExactInput((address inputToken, address outputToken, uint256 inputAmount,
                    uint256 minOutputAmount, bytes permitData)) -> selector 0xef7ec2e7

Buy: inputToken=address(0), value=amountIn (native BNB). Sell: inputToken=mint,
outputToken=address(0), permitData left empty -- this module uses the existing generic
`issues.allowance` approval path, not an EIP-2612 self-permit.

State comes from `getTokenV6(address) -> selector 0xdbde08f0`, a 15-word struct: the first 12
words match the (undocumented-fee-wise) getTokenV5 shape -- status, reserve (quote-token reserve
held by the curve), circulatingSupply, price, tokenVersion, r, h, k, dexSupplyThresh,
quoteTokenAddress, nativeToQuoteSwapEnabled, extensionID -- plus creatorFeeBps at word[12] (words
13-14 are read but unused here). The Portal address (0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0)
and the swap selector were read from a real on-chain fill (YEE, tx 0x65ae17b0..., block
121921194) BEFORE they were cross-checked against flap.sh's own docs, which named the identical
Portal address independently; the Portal is itself a proxy (implementation
0xab8ec926b6e113c2212af152b086ea62d1fdced9).

THE FORMULA is the documented shifted constant product (x+h)(y+r)=k, where y is `reserve` (the
quote-token side, given) and x (the coin-token side, held by the Portal as a plain ERC-20
balance) is solved from the other three fields: x = k*1e18 // (y+r) - h. Verified against YEE
2026-09-20: the solved x (947,727,556.956972916...) matched the Portal's real on-chain YEE
balanceOf to 10 significant figures, and x + circulatingSupply landed on exactly
1,000,000,000 -- both independent confirmations this is the right pairing, not an assumption.

THE FEE IS NOT A FLAT RATE. Total take = a fixed 1% (100 bps) PLATFORM fee, PLUS a per-token
CREATOR fee in basis points at `getTokenV6(address)` word[12] (selector 0xdbde08f0 -- the same
12 words as getTokenV5, plus 3 more; words 13-14 are unused here). YEE's creator fee happens to
be 100 bps, for a 2% total -- this was measured directly against six live `eth_call` simulations
of the real `swapExactInput` function (3 buy sizes, 3 sell sizes) and matched to 1e-12%. But YEE
is the outlier: a live sample across 54 other flap.sh tokens (213 sell events) found the MODAL
total fee is 4% (creator fee 300 bps), with 2%/5%/3% also observed -- so `getTokenV6` word[12]
MUST be read per token, never assumed from YEE's number. This module reads it live every call.

An earlier draft of this module cited flap.sh's own docs (1%) as wrong because YEE measured 2%.
Both numbers were real: the Portal's own trade-event fee word always equals exactly 1% of the
curve output -- that is the platform half, genuine and correctly matching the docs -- but the
event never discloses the creator half at all. A wei-exact measurement of a real event can still
answer a smaller question than the one asked, same shape as reading a pool's existence without
its fee, or its fee without its liquidity. Fee is taken from the quote-token leg: OFF the
input before the curve runs it (buy, `net_in = amount_in * (10000 - fee_bps) // 10000`), OFF the
gross output after the curve computes it (sell) -- both directions confirmed independently
against live data, not assumed symmetric.

THE `100 + word[12]` RULE IS NOT UNIVERSAL EITHER, and this module does not trust it blindly. A
second live sample (5 native tokens, one pinned block, real `swapExactInput` `eth_call`
simulation at 0.01 BNB each) found the rule exact at word[12] = 100, 200 and 300, but wrong at
two others: a word[12]=0 token implied 125 bps, not 100, and a word[12]=899 token fit no integer
bps rule at all -- most likely a token-level transfer tax layered on top of the curve's own fee,
invisible to `getTokenV6` entirely. A formula that is right for 3 of 5 sampled tokens is not a
foundation to quote money on. So: `eth_call`-simulating the real `swapExactInput` function IS
the quote source for a buy, at the exact amount requested -- see `simulate_swap_exact_input`.
`curve_buy_out`/`curve_sell_out` (the local formula) are downgraded to a CROSS-CHECK for buys:
every buy compares the live simulated amount against the formula's own prediction and refuses
past `_FEE_MODEL_TOLERANCE` as `FEE_MODEL_UNVERIFIED` -- the venue does not guess when the chain
and the model disagree. Gamma's own 22-token sweep proves no formula over `getTokenV6` alone can
ever be exact regardless of tuning: two tokens (0xb4a8ea, 0x1ed3ba) agree on every field of the
struct -- word[12], word[13], status, quoteTokenAddress, tokenVersion -- and still imply different
fees. The live simulation is exact by construction (it IS the contract); the formula only has to
agree with it.

THE SELL SIDE USES A LIVE SIMULATION ALWAYS, never an estimate. `swapExactInput`'s sell path
pulls the coin via `transferFrom`, which reverts for any caller without a REAL, currently-granted
allowance -- confirmed by testing. `dex_flap_curve_exit_quote` tries the real simulation FIRST,
using `taker`'s own address: whenever a real allowance already exists (a second sell attempt, or
any caller with standing approval), this succeeds directly, `route.quote_basis =
"real_allowance"`. The common case -- a first-ever quote on a freshly-bought position -- has no
real allowance yet, so the module builds an `eth_call` state override instead: it fabricates
`taker`'s balance and allowance for the Portal in the simulation only, `route.quote_basis =
"allowance_override"`. Both paths set `route.quote_quality = "exact"`, because both ARE the real
contract running the real curve math on the real chain state -- an override changes only the
`transferFrom` precondition the simulation would otherwise fail on, never the swap's economics.
Verified directly, not assumed: the override-simulated payout for a real token matched
`curve_sell_out`'s independently-derived formula prediction to the last wei (2026-09-22).

AN EARLIER DRAFT OF THIS SECTION WAS WRONG TWICE. First, it claimed a production override "fakes
what the real submitted transaction cannot rely on" -- false: the override never leaves this
process, and the real transaction still gets its own precondition from the real `approve()` the
executor sends first; simulating past a precondition is not the same as removing it from the
real path. Second, before the override existed, this section described a probe-and-estimate
fallback ("fee off input on buy, off gross output on sell, same rate") that Gamma refuted:
0x1ed3bafef7ed1ccb464d949ea2b3ddba028c7777 and 0x9c5563972087358a7fbaf35015e7b0b5d4427777 both
take 100 bps on a buy and 200 bps on a sell, so a buy-side probe cannot prove a sell-side rate.
The override removes the need for that inference entirely: `discover_sell_overrides` finds the
mint's real balance/allowance storage slots (the same technique `evm_token_safety.py` already
runs in production for the chain-5042 clip-exit check) and the simulation reads the SAME curve
state and SAME fee word[12] every other path here reads live -- nothing is estimated.

SLOT DISCOVERY, VERIFIED LIVE ON 3 REAL TOKENS (2026-09-22): YEE and the two tokens Gamma's
asymmetric-fee finding named above all land balance at storage slot 51 and allowance at slot 52
-- the OpenZeppelin-Upgradeable clone layout (`Initializable` + `ContextUpgradeable.__gap[50]`)
`evm_token_safety.py` already documents for other chains. One factory template, so a found slot
pair is cached forever per mint (a bytecode property, not chain state) and a NEW mint costs one
probe wave, not a per-quote cost.

OVERRIDE SUPPORT ON BOTH CHAIN-56 POOL MEMBERS, VERIFIED LIVE (2026-09-22): a real `swapExactInput`
sell simulation on YEE succeeded with the override and reverted without it, on both
`bsc-dataseed.bnbchain.org` and `bsc-rpc.publicnode.com` independently -- neither member refuses
the `eth_call` `overrides` parameter. Measured warm-path latency (state read + one override
`eth_call`, slots already known), n=20 each: `bsc-dataseed.bnbchain.org` p50 372ms / p95 528ms;
`bsc-rpc.publicnode.com` p50 232ms / p95 299ms.

LATENCY, MEASURE BEFORE ARMING, DO NOT OPTIMISE YET. A buy quote costs 2 RPC round trips (state
read + simulate). A sell quote on a mint whose slots are already cached costs 3 (state read, the
real-allowance attempt that fails the common first time, one override simulate); the first sell
quote for a NEW mint costs those 3 plus slot discovery (2 serial probes + 1 concurrent wave, run
for balance and allowance together) -- paid once per mint, ever, not once per quote. All of this
is bounded by the same whole-ladder deadline every other rung already answers to -- no new
transport, no new blocking path, both verified by reading the dispatch code, not assumed. But
`finding-memecoin-money-is-made-in-the-first-60-seconds` shows entry speed is real edge on this
system. Before this venue is ever armed, record the added milliseconds a real flap_curve quote
costs against the ladder deadline and against the other rungs' own timings -- if the cost of the
extra round trip is material against that 60-second window, that is an arming input, not
something to guess at from this docstring.

BUY ROUTING CAVEAT: this module builds a direct `swapExactInput` call against the Portal, not
through any aggregator/router. The one historical buy this session traced went through a router
("relay-fomo", contract 0xccc88a9d...) whose own fee on top of the curve, if any, was never
measured -- irrelevant to this module's own transactions, which never route through it, but
worth knowing if a different caller ever compares this module's quotes to that historical fill.

GRADUATION. flap.sh's `TokenStatus` enum numeric values are not documented anywhere found
(2026-09-20) -- only the named values (Invalid, Tradable, InDuel-obsolete, Killed-obsolete,
DEX) are public. `status` is read and logged, but the PRIMARY graduation check here is
`circulatingSupply >= dexSupplyThresh`, a comparison this module can justify from the docs
without trusting an unconfirmed enum mapping.

GRADUATION IS ENTRY-ONLY, AT THE `refusal_reason` LAYER. A graduated coin's ENTRY is refused
outright: refusing it costs only a missed trade, and V2/V3 is the real market for a graduated
coin now, not this curve. `refusal_reason(..., allow_graduated=True)` does not refuse an EXIT on
graduation -- verified directly, not inferred, that graduation does not make the Portal revert: a
live `swapExactInput` BUY and a state-overridden SELL both succeeded on a real graduated native
token (Taraxacum, status=4, reserve=0, 2026-09-20).

BUT A GRADUATED EXIT STILL REFUSES IN PRACTICE (2026-09-22, Gamma, reproduced live by Alpha), one
layer up. `dex_flap_curve_exit_quote`'s fee-model cross-check on the override path compares the
simulated payout to `curve_sell_out`, which is UNDEFINED at `reserve == 0` (every real graduated
coin) and returns None -- so the cross-check reads as "unverified" and refuses, even though the
Portal simulation itself paid out a real number (Taraxacum 0.0673 BNB, DPAID the same shape,
both reproduced at runtime). LEFT AS-IS, DELIBERATELY (Alpha's call, 2026-09-22): there is ZERO
real post-graduation Portal sell to validate that number against, so trusting it as "exact" would
trade a known, safe refusal for an unvalidated one. A coin that graduates while held can still be
stranded if the V2/V3 rung cannot price its new pool -- the same failure mode this module has
otherwise been built to avoid, now documented as an accepted gap rather than assumed closed.
"""
from __future__ import annotations

import logging
from sniper.services.memecoin_evm_gas_topup import WETH_CALL_GAS_LIMIT

logger = logging.getLogger("memecoin")

_NATIVE = "0x" + "00" * 20
_WORD = 64
_BPS = 10_000

FLAP_PORTAL = {
    "56": "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0",
}
_SEL_GET_TOKEN_V6 = "dbde08f0"
_SEL_SWAP_EXACT_INPUT = "ef7ec2e7"
_SEL_APPROVE = "095ea7b3"                   # approve(address,uint256)
_SEL_WETH_WITHDRAW = "2e1a7d4d"             # withdraw(uint256) -- unwrap WETH to native

# Fixed protocol-wide platform fee, 1% -- confirmed as the Portal's own disclosed trade-event fee
# word, which is ALWAYS exactly 1% of the curve output. Never a per-token read; the per-token
# CREATOR fee (word[12] of getTokenV6) is added on top, always live, never cached. See module
# docstring -- YEE's 100 bps creator fee (2% total) is not representative of other tokens.
_PLATFORM_FEE_BPS = 100

# The 12 getTokenV5-shaped fields, plus creator_fee_bps at word[12]. Words 13-14 (getTokenV6
# only) are read but unused here.
_STATE_FIELDS = (
    "status", "reserve", "circulating_supply", "price", "token_version",
    "r", "h", "k", "dex_supply_thresh", "quote_token", "native_to_quote_swap_enabled",
    "extension_id", "creator_fee_bps",
)


def _chain_weth(chain):
    """This chain's bankroll token, lowercased, or "" when unconfigured. Mirrors
    evm_launchpad_curve.py's `_chain_weth` -- our wallet holds WBNB on chain 56, not native, so a
    native-counter buy must unwrap it first via a `pre_transactions` entry the executor already
    runs ahead of the main swap."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    return (CHAIN_QUOTE.get(str(chain)) or {}).get("token", "").lower()


def _words(raw):
    body = str(raw or "")[2:] if str(raw or "").startswith("0x") else str(raw or "")
    return [body[i:i + _WORD] for i in range(0, len(body) - len(body) % _WORD, _WORD)]


def _address_at(words, index):
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


def _call(to, data, rpc_url, *, http=None, from_addr=None, value_wei=None, overrides=None):
    """One read-only `eth_call` through the shared read layer. None on any failure (including the
    Portal's own "token not recognised" custom revert -- this token is not a flap.sh mint, or a
    sell simulation reverting on a real allowance check)."""
    from sniper.execution.evm_read_rpc import eth_call_raw
    try:
        return eth_call_raw(to, data, rpc_url, http=http, from_addr=from_addr,
                            value_wei=value_wei, overrides=overrides)
    except Exception:
        logger.exception("memecoin: flap.sh curve eth_call failed")
        return None


def simulate_swap_exact_input(portal, input_token, output_token, amount_atomic, taker, rpc_url,
                              *, http=None, overrides=None):
    """The REAL `swapExactInput` output for `amount_atomic`, from a live `eth_call` -- exact,
    including any mechanism this module's own curve formula does not model (a token-level
    transfer tax, an undocumented fee tier). `minOutputAmount=0` so the simulation itself never
    reverts on slippage; the caller applies its own slippage after seeing the true amount. None
    on any revert -- for a sell with no `overrides`, this reverts for any `taker` without a real,
    already-granted allowance (see module docstring), which is expected and not itself an error.

    `overrides` is an `eth_call` state-override map (see `discover_sell_overrides`): it fabricates
    `taker`'s balance/allowance for THIS SIMULATION ONLY, so a sell can be quoted before any real
    approval exists. It changes only whether `transferFrom` reverts, never the curve's own output
    -- verified live, not assumed (module docstring).

    `taker` MUST be a real, non-zero address: `swapExactInput` reverts with a distinct error
    (`0x90b8ec18`) for a missing or zero-address caller, which is not itself informative about
    the route -- Gamma's finding. Checked here so a caller bug never silently reads as "no
    route" on this rung specifically."""
    try:
        taker_is_zero = not taker or int(str(taker), 16) == 0
    except ValueError:
        taker_is_zero = True
    if taker_is_zero:
        logger.error("memecoin: flap.sh curve simulate called with no real taker address")
        return None
    is_native_in = str(input_token).lower() == _NATIVE
    data = encode_swap_exact_input(input_token, output_token, amount_atomic, 0)
    raw = _call(portal, data, rpc_url, http=http, from_addr=taker,
               value_wei=amount_atomic if is_native_in else None, overrides=overrides)
    if raw is None or raw == "0x":
        return None
    value = _int_at(_words(raw), 0)
    return value if value and value > 0 else None


_sell_override_slot_cache = {}
# The flap.sh token template (an OpenZeppelin-Upgradeable clone): `_balances` at slot 51 and
# `_allowances` at slot 52. 3 of 3 tokens on 2026-09-22, 30 of 30 on 2026-09-30.
_TEMPLATE_BALANCE_SLOT, _TEMPLATE_ALLOWANCE_SLOT = 51, 52


def _build_overrides(mint, proof, amount_atomic, *, taker):
    from sniper.execution.evm_token_safety import _GAS_HEX, _simulation_amount_word
    balance_slot, allowance_slot = proof
    word = _simulation_amount_word(amount_atomic)
    if word is None:
        return None
    return {mint: {"stateDiff": {balance_slot: word, allowance_slot: word}},
            taker: {"balance": _GAS_HEX}}


def discover_sell_overrides(mint, spender, amount_atomic, *, taker, rpc_url, http=None):
    """Build an `eth_call` state override that gives `taker` a balance of `mint` and an allowance
    for `spender`, so `simulate_swap_exact_input`'s sell path can run before any real approval
    exists. Lifted from `evm_token_safety.py`'s `_fund_overrides` -- the same technique the
    chain-5042 clip-exit check already runs in production -- because that one lives inside
    `build_evm_token_safety`'s closure, bound to a different chain's safety pipeline; this is a
    standalone copy for flap.sh's own exit-quote path.

    A found slot pair is CACHED FOREVER per mint/taker/spender: these hashed keys bind the
    balance owner and allowance spender, not mutable chain state. Verified live (2026-09-22)
    on 3 real flap.sh tokens -- all 3 land balance at slot 51,
    allowance at slot 52, the OpenZeppelin-Upgradeable clone layout `evm_token_safety.py` already
    documents (see module docstring). One factory template, so a NEW mint costs the two read-backs
    of that pair, and the probe wave only when one of them says "not this slot". Never cached on
    a miss (a transient RPC fault must not brand a token permanently unverifiable -- same
    reasoning `evm_token_safety.py` documents for its own cache).

    None when the layout cannot be found or amount is invalid -- the caller refuses rather than
    guesses.
    """
    mint = str(mint).lower()
    cache_key = (mint, str(taker).lower(), str(spender).lower())
    cached = _sell_override_slot_cache.get(cache_key)
    if cached is not None:
        return _build_overrides(mint, cached, amount_atomic, taker=taker)
    from concurrent.futures import ThreadPoolExecutor
    from sniper.execution.evm_read_rpc import eth_call_raw
    from sniper.execution.evm_token_safety import (
        _BIG, _BIG_HEX, _MalformedProbe, _access_list_slots, _find_slot, _map_slot, _nested_slot,
        _pad, _read_with_retry, _simulation_amount_word, _slot_probe_alternates,
        _solady_allowance_slot, _solady_balance_slot,
    )
    from sniper.execution.evm_wallet import SEL_ALLOWANCE, SEL_BALANCE_OF

    if _simulation_amount_word(amount_atomic) is None:
        return None                              # an invalid amount: nothing to build, no call
    alternates = _slot_probe_alternates(rpc_url)
    balance_data = "0x" + SEL_BALANCE_OF + _pad(taker)
    allowance_data = "0x" + SEL_ALLOWANCE + _pad(taker) + _pad(spender)

    def read_back(data):
        """Write `_BIG_HEX` at one storage key and read the getter back: the scan's own proof."""
        return lambda key, url=rpc_url: eth_call_raw(
            mint, data, url, http=http,
            overrides={mint: {"stateDiff": {key: _BIG_HEX}}}, raise_on_error=True)

    def is_slot(data, key):
        """True when writing `key` makes the getter read `_BIG` back. None from the node is a clean
        "not this slot". The body is parsed INSIDE the retried call, as `_find_slot._parsed` does:
        a member that answers a 200 that is not a word (a lagging node, garbage) is retried on the
        next member instead of costing the ask (the 2026-09-04 SUIT fix)."""
        ask = read_back(data)

        def parsed(k, **kw):
            got = ask(k, **kw)
            if got is None:
                return None
            try:
                return int(got, 16)
            except (TypeError, ValueError) as e:
                raise _MalformedProbe("malformed slot-probe result") from e
        return _read_with_retry(parsed, key, alternates=alternates) == _BIG

    def find_balance_slot():
        return _find_slot(
            read_back(balance_data),
            lambda i: _map_slot(taker, i),
            extra_slots=(_solady_balance_slot(taker),),
            late_slots=lambda: _access_list_slots(mint, balance_data, rpc_url, http=http, taker=taker),
            alternates=alternates)

    def find_allowance_slot():
        return _find_slot(
            read_back(allowance_data),
            lambda i: _nested_slot(spender, taker, i),
            extra_slots=(_solady_allowance_slot(spender, taker),),
            late_slots=lambda: _access_list_slots(mint, allowance_data, rpc_url, http=http, taker=taker),
            alternates=alternates)

    # THE FACTORY TEMPLATE FIRST (2026-09-30). The scan is about 30 `eth_call`s in one wave on the
    # first sight of every mint, inside the entry ladder's reverse leg: measured off the funded
    # host, 30 to 55 calls, and a public host answered HTTP 429 to the burst. All 30 flap tokens
    # checked that day share one template (balance mapping at slot 51, allowance at 52), so those
    # two keys are asked first, with the scan's own read-back proof: 2 calls in parallel.
    # A CLEAN "NOT THIS SLOT" falls back to the scan, so another layout gets the answer it got
    # before. A TRANSPORT FAILURE does not: an endpoint that is refusing calls must not be sent a
    # wave of them. It returns None, caches nothing, and the caller refuses this ask.
    # The proof is the read-back, NOT a sell simulation: 2 of the 10 on-curve non-native tokens
    # seen that week revert their sell with the right slots funded, and a sell-based check would
    # have sent exactly those tokens (the ones we refuse) to the wave.
    template = (_map_slot(taker, _TEMPLATE_BALANCE_SLOT),
                _nested_slot(spender, taker, _TEMPLATE_ALLOWANCE_SLOT))
    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="flap-slot-scan") as executor:
            bal_future = executor.submit(is_slot, balance_data, template[0])
            allow_future = executor.submit(is_slot, allowance_data, template[1])
            # BOTH results are read, so a transport failure on either probe raises here and can
            # never be taken for "not the template".
            is_template = all((bal_future.result(), allow_future.result()))
    except Exception:
        logger.exception(
            "memecoin: flap.sh curve template slot check failed on transport mint=%s", mint)
        return None
    if is_template:
        _sell_override_slot_cache[cache_key] = template
        return _build_overrides(mint, template, amount_atomic, taker=taker)

    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="flap-slot-scan") as executor:
            bal_future = executor.submit(find_balance_slot)
            allow_future = executor.submit(find_allowance_slot)
            bal_slot = bal_future.result()
            allow_slot = allow_future.result()
    except Exception:
        logger.exception(
            "memecoin: flap.sh curve override slot discovery failed mint=%s", mint)
        return None
    if bal_slot is None or allow_slot is None:
        return None
    _sell_override_slot_cache[cache_key] = (bal_slot, allow_slot)
    return _build_overrides(mint, (bal_slot, allow_slot), amount_atomic, taker=taker)


def curve_state(chain, mint, rpc_url, *, http=None):
    """`getTokenV6(mint)` decoded, plus the derived `coin_reserve` (x) and this token's own
    `fee_bps` (platform + creator), or None when this chain has no Portal, the mint is not a
    flap.sh token, or the read failed.

    `coin_reserve` is solved from the returned r/h/k/reserve via the documented (x+h)(y+r)=k
    curve -- see the module docstring for the two independent checks that confirmed this
    algebra against a real token (YEE, 2026-09-20)."""
    portal = FLAP_PORTAL.get(str(chain))
    if portal is None or not rpc_url or not mint:
        return None
    arg = str(mint)[2:].lower().rjust(_WORD, "0")
    raw = _call(portal, "0x" + _SEL_GET_TOKEN_V6 + arg, rpc_url, http=http)
    words = _words(raw)
    if len(words) < len(_STATE_FIELDS):
        return None
    state = {
        "status": _int_at(words, 0),
        "reserve": _int_at(words, 1),
        "circulating_supply": _int_at(words, 2),
        "price": _int_at(words, 3),
        "token_version": _int_at(words, 4),
        "r": _int_at(words, 5),
        "h": _int_at(words, 6),
        "k": _int_at(words, 7),
        "dex_supply_thresh": _int_at(words, 8),
        "quote_token": _address_at(words, 9),
        "native_to_quote_swap_enabled": _int_at(words, 10),
        "extension_id": words[11] if len(words) > 11 else None,
        "creator_fee_bps": _int_at(words, 12),
    }
    if any(state[f] is None for f in
           ("reserve", "r", "h", "k", "dex_supply_thresh", "circulating_supply",
            "creator_fee_bps")):
        return None
    if state["quote_token"] is None:
        return None
    # `reserve == 0` IS ALLOWED (not `<= 0`): a graduated coin's curve reserve reads exactly 0
    # once drained, and the exit path must still be able to build a state for it -- refusing
    # here would silently reject the exit-side graduation allowance below before it ever runs.
    if state["reserve"] < 0 or state["r"] < 0 or state["h"] < 0 or state["k"] <= 0:
        return None
    if not 0 <= state["creator_fee_bps"] < _BPS:
        return None
    denom = state["reserve"] + state["r"]
    if denom <= 0:
        return None
    coin_reserve = (state["k"] * 10 ** 18) // denom - state["h"]
    if coin_reserve <= 0:
        return None
    state["coin_reserve"] = coin_reserve
    state["fee_bps"] = _PLATFORM_FEE_BPS + state["creator_fee_bps"]
    state["portal"] = portal
    state["mint"] = str(mint).lower()
    return state


def is_native_quote(state):
    return state.get("quote_token") == _NATIVE


def _check_state_fn(chain, mint, rpc_url, state, state_fn):
    """Reuse one fresh state within this check; other identities still need a live read.

    The flat decoded state is copied both on capture and on return. No state survives the
    caller's check, and a custom state source cannot mutate the bound fee between simulations.
    """
    identity = str(chain), str(mint).lower(), rpc_url
    snapshot = dict(state)

    def read(request_chain, request_mint, request_rpc, *, http=None):
        if (str(request_chain), str(request_mint).lower(), request_rpc) == identity:
            return dict(snapshot)
        return state_fn(request_chain, request_mint, request_rpc, http=http)

    return read


def takes_native_payment(state):
    """True when the PORTAL ITSELF accepts native for this curve, so we never hold its quote token.

    A native-quote curve takes native by construction. A NON-native curve takes it only when
    `nativeToQuoteSwapEnabled` is set (word[10], decoded by `curve_state` and, until now, read by
    nothing), and the Portal then converts internally.

    VERIFIED LIVE ON CHAIN 56, never inferred from the field name. Four of four flag-set tokens
    simulated a real `swapExactInput` in BOTH directions -- native -> mint and mint -> native --
    while the one flag-clear non-native token still on its curve (OPAI 0xa980aea6, quoted in
    xOPAI) REVERTED both ways. Paying the quote token directly reverted on all four, so native
    stays the only input this rung ever sends.

    THE CLASS IS THE MAJORITY OF THE VENUE: of 102 flap tokens among 141 chain-56 mints seen in
    the 7 days to 2026-09-22, 61 are non-native with the flag set against 31 native.

    Behind `MEMECOIN_EVM_FLAP_NATIVE_SWAP_ENABLED`, default OFF: with the knob off this is exactly
    `is_native_quote`, so the venue keeps the behaviour it shipped with.
    """
    if is_native_quote(state):
        return True
    from sniper.settings import memecoin as cfg
    if not bool(getattr(cfg, "MEMECOIN_EVM_FLAP_NATIVE_SWAP_ENABLED", False)):
        return False
    return int(state.get("native_to_quote_swap_enabled") or 0) == 1


def is_graduated(state):
    """Primary check: `circulatingSupply >= dexSupplyThresh` -- justified directly from flap.sh's
    docs. `status` is corroboration only (logged, never gating): the numeric TokenStatus enum is
    not documented anywhere found, so a mismatch between this check and `status` is reported, not
    trusted either way. See the module docstring."""
    supply_graduated = state["circulating_supply"] >= state["dex_supply_thresh"]
    status = state.get("status")
    if status is not None:
        status_says_curve = status == 1          # inferred, not confirmed -- see docstring
        # `==` IS CORRECT HERE, not a swapped operator: the two flags carry OPPOSITE polarity
        # (one says "is graduated", the other says "is still curve"), so them being EQUAL is
        # what signals a DISAGREEMENT -- confirmed against all 4 cases of the truth table
        # (Gamma's review, 2026-09-20). Reads like a bug; is not one.
        if supply_graduated == status_says_curve:
            logger.warning(
                "memecoin: flap.sh curve state.status=%s disagrees with supply-threshold "
                "graduation=%s for %s -- using the supply threshold", status, supply_graduated,
                state.get("mint"))
    return supply_graduated


def curve_buy_out(state, amount_in_atomic):
    """Coin-token output for `amount_in_atomic` of the native quote token spent, net of THIS
    TOKEN's own total fee (`state["fee_bps"]` = 100 platform + its creator fee, never a hardcoded
    rate -- see module docstring) taken off the input before it changes the curve's reserve. None
    (fail closed) on any non-positive result."""
    amount_in_atomic = int(amount_in_atomic)
    if amount_in_atomic <= 0:
        return None
    fee_bps = state["fee_bps"]
    if not 0 <= fee_bps < _BPS:
        return None
    net_in = amount_in_atomic * (_BPS - fee_bps) // _BPS
    if net_in <= 0:
        return None
    new_reserve = state["reserve"] + net_in
    denom = new_reserve + state["r"]
    if denom <= 0:
        return None
    new_coin_reserve = (state["k"] * 10 ** 18) // denom - state["h"]
    out = state["coin_reserve"] - new_coin_reserve
    return out if out > 0 else None


def curve_sell_out(state, amount_in_atomic):
    """Native-quote-token output for `amount_in_atomic` of the coin sold, with THIS TOKEN's own
    total fee (`state["fee_bps"]`) taken off the curve's gross output. None (fail closed) on any
    non-positive result, including a sell that would drain more than the curve's real reserve
    (the curve cannot pay that out)."""
    amount_in_atomic = int(amount_in_atomic)
    if amount_in_atomic <= 0:
        return None
    fee_bps = state["fee_bps"]
    if not 0 <= fee_bps < _BPS:
        return None
    new_coin_reserve = state["coin_reserve"] + amount_in_atomic
    denom = new_coin_reserve + state["h"]
    if denom <= 0:
        return None
    new_reserve = (state["k"] * 10 ** 18) // denom - state["r"]
    if new_reserve < 0 or new_reserve > state["reserve"]:
        return None
    gross_out = state["reserve"] - new_reserve
    out = gross_out * (_BPS - fee_bps) // _BPS
    return out if out > 0 else None


def encode_swap_exact_input(input_token, output_token, amount_in, min_out, permit_bytes=b""):
    """`swapExactInput((address,address,uint256,uint256,bytes))`, selector `0xef7ec2e7`."""
    def enc_addr(a):
        return str(a)[2:].lower().rjust(_WORD, "0")

    def enc_uint(n):
        return f"{int(n):064x}"

    def enc_bytes(b):
        length = len(b)
        pad = (-length) % 32
        return enc_uint(length) + (b + b"\x00" * pad).hex()

    head = (enc_addr(input_token) + enc_addr(output_token) + enc_uint(amount_in)
            + enc_uint(min_out) + enc_uint(5 * 32))
    return "0x" + _SEL_SWAP_EXACT_INPUT + enc_uint(32) + head + enc_bytes(permit_bytes)


def encode_approve(spender, amount):
    return ("0x" + _SEL_APPROVE + str(spender)[2:].lower().rjust(_WORD, "0")
            + f"{int(amount):064x}")


def _min_out(amount_out, slippage_bps):
    if not 0 <= int(slippage_bps) < _BPS:
        return 0
    return (int(amount_out) * (_BPS - int(slippage_bps))) // _BPS


NOT_A_FLAP_TOKEN = "flap_curve_not_a_flap_token"
NON_NATIVE_QUOTE_REFUSED = "flap_curve_non_native_quote_refused"
GRADUATED_REFUSED = "flap_curve_graduated_refused"
EXIT_UNQUOTABLE = "flap_curve_exit_unquotable_at_clip"
FEE_MODEL_UNVERIFIED = "flap_curve_fee_model_unverified"

# Same bar the 4663 curve spec set (phase-3 evidence, live eth_simulateV1 within 0.1%). Gamma's
# 22-token sweep (the larger, later one -- supersedes an earlier 5-token pass this comment used
# to cite) found the formula-vs-live gap on the 17 well-behaved tokens was 0.00000% to 5e-8%, and
# on the 5 anomalous ones ranged -1.03% to +0.25%. The separation this tolerance relies on is
# real but is 2.5x, NOT the ~88x an earlier, smaller sample suggested.
_FEE_MODEL_TOLERANCE = 0.001


def _relative_diff(a, b):
    if not b:
        return float("inf")
    return abs(a - b) / b


def _implied_fee_bps(state, amount_in, live_out):
    """The total fee bps a live buy result actually implies, back-solved from the SAME
    (x+h)(y+r)=k curve this module trusts for everything else -- diagnostic only, never used to
    decide a quote. Exists so a `FEE_MODEL_UNVERIFIED` refusal is logged with BOTH rates (the
    modeled one from word[12], and the one the chain's own answer implies), per Alpha's "count
    the refusals, do not silence them": a fail-closed refusal is still the right call, but the
    false-refusal rate must be visible, not inferred. None when the back-solve is undefined."""
    try:
        amount_in = int(amount_in)
        if amount_in <= 0 or live_out is None or live_out <= 0:
            return None
        new_coin_reserve = state["coin_reserve"] - live_out
        denom = new_coin_reserve + state["h"]
        if denom <= 0:
            return None
        new_reserve = (state["k"] * 10 ** 18) // denom - state["r"]
        effective_net_in = new_reserve - state["reserve"]
        if effective_net_in <= 0:
            return None
        return round((1 - effective_net_in / amount_in) * _BPS, 4)
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def _fee_model_verified(mint, modeled, live, *, state=None, amount_in=None):
    """True when the local formula's prediction agrees with a live simulated result within
    `_FEE_MODEL_TOLERANCE` -- logs `FEE_MODEL_UNVERIFIED` with BOTH the modeled and (when
    `state`/`amount_in` are given) the live-implied fee bps, and returns False otherwise. The
    named, countable refusal for a token whose real economics are not word[12]-shaped -- logged
    so the false-refusal rate stays visible rather than inferred (Alpha, 2026-09-20)."""
    if modeled is None or live is None or _relative_diff(modeled, live) > _FEE_MODEL_TOLERANCE:
        implied_bps = (_implied_fee_bps(state, amount_in, live)
                      if state is not None and amount_in is not None else None)
        modeled_bps = state.get("fee_bps") if state is not None else None
        logger.warning(
            "memecoin: %s for %s -- modeled=%s live=%s modeled_fee_bps=%s implied_fee_bps=%s",
            FEE_MODEL_UNVERIFIED, mint, modeled, live, modeled_bps, implied_bps)
        return False
    return True


def refusal_reason(mint, state, *, allow_graduated=False):
    """Why this rung will not price `mint`, or None when it is eligible (a flap.sh token, native
    quote, and -- on entry only -- still on its curve). A NAMED, countable reason -- callers that
    want to log or measure an explicit refusal (never a silent miss dressed as one) call this
    directly; the quote functions below stay bare `None`-or-quote-dict, the same contract every
    other rung in the ladder uses, so a refusal here is an ordinary rung miss to
    `classify_quote_miss`, not a fabricated "provider could not read" blind spot -- this coin was
    read just fine; it simply does not qualify.

    GRADUATION IS ENTRY-ONLY (2026-09-20, Alpha's design change on Gamma's finding). Graduation
    does not make the Portal revert -- verified directly (not inferred): a live `swapExactInput`
    BUY and a state-overridden SELL both succeeded on a real graduated native token
    (Taraxacum, status=4, reserve=0). Refusing a graduated coin's ENTRY costs only a missed
    trade, and a graduated coin genuinely belongs to V2/V3 -- their pool is the real market now.
    But refusing its EXIT on the same rule risks a stuck bag: a coin that graduates WHILE we hold
    it, whose new pool the V2/V3 rung cannot price for some unrelated reason, would have a Portal
    that could still serve the exit -- refusing it anyway trades a real position for a scope
    rule. `allow_graduated=True` (used by `dex_flap_curve_exit_quote` and
    `whole_balance_exit_quote_or_refuse`) skips only this check; `is_graduated` still runs for
    its logging/corroboration side effect either way."""
    if state is None:
        return NOT_A_FLAP_TOKEN
    if not takes_native_payment(state):
        return NON_NATIVE_QUOTE_REFUSED
    graduated = is_graduated(state)                     # always runs, for its logging side effect
    if graduated and not allow_graduated:
        return GRADUATED_REFUSED
    return None


def dex_flap_curve_quote(chain, sell_token, buy_token, amount_atomic, taker, *, http=None,
                         slippage_bps=None, rpc_url=None, state_fn=None, simulate_fn=None):
    """Executable curve-buy quote: `amount_atomic` of `sell_token` (must be this chain's WETH
    standing in for native, or literal native) -> `buy_token` (the coin). None (fail closed) when
    the mint is not a flap.sh token, is not native-quote, or has already graduated -- phase 1 is
    native-quote-only by design (see module docstring); a non-native mint is a documented refusal
    (see `refusal_reason`), never a mis-quote.
    """
    from sniper.settings import memecoin as cfg
    if slippage_bps is None:
        slippage_bps = int(round(cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT * 100))
    if not 0 <= int(slippage_bps) < _BPS:
        return None
    try:
        amount_atomic = int(amount_atomic)
    except (TypeError, ValueError):
        return None
    if amount_atomic <= 0 or not rpc_url:
        return None
    state_fn = state_fn or curve_state
    simulate_fn = simulate_fn or simulate_swap_exact_input
    state = state_fn(chain, buy_token, rpc_url, http=http)
    reason = refusal_reason(buy_token, state)
    if reason is not None:
        logger.debug("memecoin: flap.sh curve refused %s: %s", buy_token, reason)
        return None
    weth = _chain_weth(chain)
    is_weth_pay = str(sell_token).lower() == weth
    if str(sell_token).lower() != _NATIVE and not is_weth_pay:
        return None                          # this rung only prices the native/WETH counter
    # THE LIVE eth_call IS THE QUOTE SOURCE, not the local curve formula -- the formula's
    # word[12]-derived fee was found wrong for 2 of 5 sampled tokens (module docstring). The
    # formula still runs, as a cross-check: a mismatch means this token's real economics are not
    # word[12]-shaped (a token transfer tax, most likely), and the venue refuses rather than
    # quote a number the chain itself disagrees with.
    # A WETH-PAID BUY UNWRAPS ITS OWN NATIVE FIRST (2026-09-23). `pre_transactions` below
    # withdraws exactly `amount_atomic` of WETH into native BEFORE the swap, so the wallet's
    # native balance at SIMULATION time is not the balance the swap spends from. Without this,
    # the `eth_call` reverts `insufficient funds for gas * price + value` and the rung misses a
    # route the wallet can pay for -- measured live on the funded 56 wallet on 2026-09-23:
    # 0.009789 BNB native against a 0.1 BNB clip, refused, and the identical call with a balance
    # override accepted. The clip itself is already capped by the spendable pay-token balance
    # before the ladder is asked (`sniper/services/memecoin_risk.py`), so this simulation was
    # never the gate that kept an unfundable WETH-paid entry out.
    #
    # ONLY when `is_weth_pay`: a NATIVE-paid buy has no unwrap to account for, so its real
    # balance must keep deciding -- an unconditional override would quote an entry the wallet
    # cannot pay for, which is the opposite defect. The fabricated balance is the clip PLUS a gas
    # allowance; `_GAS_HEX` alone is the sell path's gas figure and would under-fund any clip at
    # or above one native unit. An endpoint that rejects state overrides fails CLOSED here (the
    # call errors, `_call` returns None, the rung misses); the sell path already relies on
    # overrides against these endpoints, but that is proven for the sell shape, not asserted for
    # this one.
    overrides = None
    if is_weth_pay and taker:
        from sniper.execution.evm_token_safety import _GAS_HEX
        overrides = {taker: {"balance": hex(amount_atomic + int(_GAS_HEX, 16))}}
    buy_amount = simulate_fn(
        state["portal"], _NATIVE, state["mint"], amount_atomic, taker, rpc_url, http=http,
        overrides=overrides)
    if buy_amount is None:
        return None
    # THE CROSS-CHECK ONLY APPLIES WHEN THE FORMULA AND THE PAYMENT SHARE A UNIT (2026-09-22). On
    # an internally-converted curve the reserve is denominated in the QUOTE token while we pay
    # native, so `curve_buy_out` is not a wrong number -- it is a number about a different trade.
    # Measured against the live simulation on three such tokens: implied fee 7,283.6 bps,
    # 9,622.7 bps and -7,725,801.9 bps. Running it there would refuse the whole class for a unit
    # mismatch. What guards a transfer tax instead is the ladder's round-trip cap: this rung is
    # wrapped in `_measured` with `reverse_fns["flap_curve"]` (evm_real_seams.py), which prices
    # buy-then-sell IN NATIVE TERMS and refuses an unmeasurable rung outright.
    if is_native_quote(state) and not _fee_model_verified(
            state["mint"], curve_buy_out(state, amount_atomic), buy_amount,
            state=state, amount_in=amount_atomic):
        return None
    min_out = _min_out(buy_amount, slippage_bps)
    if min_out <= 0:
        return None
    data = encode_swap_exact_input(_NATIVE, state["mint"], amount_atomic, min_out)
    pre_transactions = (
        [{"to": weth, "data": "0x" + _SEL_WETH_WITHDRAW + f"{amount_atomic:064x}",
          "value": "0x0", "gas": str(WETH_CALL_GAS_LIMIT)}]
        if is_weth_pay else [])
    return {
        "sellAmount": str(amount_atomic),
        "buyAmount": str(buy_amount),
        "minBuyAmount": str(min_out),
        "liquidityAvailable": True,
        "pre_transactions": pre_transactions,
        "transaction": {"to": state["portal"], "data": data, "value": hex(amount_atomic),
                        "gas": str(portal_gas())},
        "issues": {},
        "route": {"protocol": "flap_curve",
                  "tokens": [str(sell_token).lower(), state["mint"]],
                  "curve": state["portal"]},
    }


def portal_gas():
    """The gas limit of a Portal `swapExactInput` (buy or sell): MEMECOIN_EVM_FLAP_PORTAL_GAS.
    See that setting: the executor's 500,000 default ran a graduated coin's sell out of gas."""
    from sniper.settings import memecoin as cfg
    gas = int(getattr(cfg, "MEMECOIN_EVM_FLAP_PORTAL_GAS", 1_500_000))
    return gas if gas > 0 else 1_500_000              # 0 would sign gas 0 ("0" is truthy)


def dex_flap_curve_exit_quote(chain, sell_token, buy_token, amount_atomic, taker, *, http=None,
                              slippage_bps=None, rpc_url=None, state_fn=None, simulate_fn=None,
                              overrides_fn=None, pre_entry=False):
    """Executable curve-sell quote: `amount_atomic` of `sell_token` (the coin) -> `buy_token`
    (native). None (fail closed) when the mint is not a flap.sh token or is not native-quote, or
    when no exact answer can be produced at all -- graduation does NOT refuse here (module
    docstring: the Portal still serves both directions post-graduation, verified live).
    Pre-entry may try an already-proven override first; actual exits remain real-first.
    """
    from sniper.settings import memecoin as cfg
    if slippage_bps is None:
        slippage_bps = int(round(cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT * 100))
    if not 0 <= int(slippage_bps) < _BPS:
        return None
    try:
        amount_atomic = int(amount_atomic)
    except (TypeError, ValueError):
        return None
    if amount_atomic <= 0 or not rpc_url:
        return None
    state_fn = state_fn or curve_state
    simulate_fn = simulate_fn or simulate_swap_exact_input
    overrides_fn = overrides_fn or discover_sell_overrides
    state = state_fn(chain, sell_token, rpc_url, http=http)
    reason = refusal_reason(sell_token, state, allow_graduated=True)
    if reason is not None:
        logger.debug("memecoin: flap.sh curve refused %s: %s", sell_token, reason)
        return None
    weth = _chain_weth(chain)
    if str(buy_token).lower() not in (_NATIVE, weth):
        return None
    # A warm pre-entry proof avoids the usual unfunded sell. Cold/custom/actual exits keep
    # real-first; failed overrides and model refusals still allow one exact real fallback.
    warm_pre_entry = (pre_entry and str(chain) == "56"
        and overrides_fn is discover_sell_overrides
        and (str(state["mint"]).lower(), str(taker).lower(),
             str(state["portal"]).lower()) in _sell_override_slot_cache)
    for use_override in ((True, False) if warm_pre_entry else (False, True)):
        overrides = None
        if use_override:
            overrides = overrides_fn(
                state["mint"], state["portal"], amount_atomic, taker=taker, rpc_url=rpc_url,
                http=http)
            if overrides is None:
                continue
        payout = simulate_fn(
            state["portal"], state["mint"], _NATIVE, amount_atomic, taker, rpc_url,
            http=http, **({"overrides": overrides} if use_override else {}))
        if payout is None:
            continue
        # Native override outputs still need the fresh fee model, including graduation's
        # undefined model. Converted quote-token amounts cannot be compared to native output.
        if use_override and is_native_quote(state) and not _fee_model_verified(
                state["mint"], curve_sell_out(state, amount_atomic), payout):
            if warm_pre_entry:
                continue
            return None
        quote_basis = "allowance_override" if use_override else "real_allowance"
        break
    else:
        return None
    min_out = _min_out(payout, slippage_bps)
    if min_out <= 0:
        return None
    data = encode_swap_exact_input(state["mint"], _NATIVE, amount_atomic, min_out)
    # NO wrap step on the sell side -- a native payout lands as a plain balance change, not an
    # ERC-20 Transfer, and `_receipt_quote_usd` (evm_real_seams.py) already falls back to
    # `native_received_in_receipt` for exactly that shape when the quote `kind` is "eth". Verified
    # by reading that function, not assumed -- same reasoning evm_launchpad_curve.py documents for
    # the identical situation on chain 4663.
    return {
        "sellAmount": str(amount_atomic),
        "buyAmount": str(payout),
        "minBuyAmount": str(min_out),
        "liquidityAvailable": True,
        "transaction": {"to": state["portal"], "data": data, "value": "0x0",
                        "gas": str(portal_gas())},
        "issues": {"allowance": {"spender": state["portal"]}},
        "route": {"protocol": "flap_curve", "quote_quality": "exact", "quote_basis": quote_basis,
                  "tokens": [state["mint"], str(buy_token).lower()],
                  "curve": state["portal"]},
    }


NOT_EXACT = "flap_curve_not_exact"


def whole_balance_exit_quote_or_refuse(chain, mint, amount_atomic, rpc_url, *, taker, http=None,
                                       slippage_bps=None, state_fn=None, simulate_fn=None,
                                       overrides_fn=None, pre_entry=False):
    """This rung's own clip-size safety check: an exact whole-balance quote from the curve
    itself, or an outright refusal -- never a fallback to a smaller probe. Mirrors
    evm_launchpad_curve.py's identical function for chain 4663: a dust-sized probe can pass while
    a real clip-sized position cannot exit (`finding-memecoin-evm-honeypot-false-positive`), so
    this checks the REAL amount, never an independently-derived estimate.

    ENFORCES `quote_quality == "exact"` (Gamma's review, 2026-09-20). `dex_flap_curve_exit_quote`
    can now ONLY return `"exact"` or `None` -- the independently-derived-estimate path it used to
    fall back to (`"estimated_pre_approval"`) was deleted once the allowance-override made a real
    simulation always reachable (2026-09-22, see module docstring). This check stays regardless:
    `route.quote_quality` had no reader anywhere in the funded path before Gamma's 2026-09-20
    review, so this is the one place that actually reads the callee's own contract rather than
    trusting it -- cheap, and the only thing standing between a future regression in the callee
    and a fabricated quote reaching a real exit.

    Returns `(quote_dict, None)` on a priceable EXACT exit, or `(None, reason)` on refusal.
    """
    state_fn = state_fn or curve_state
    state = state_fn(chain, mint, rpc_url, http=http)
    reason = refusal_reason(mint, state, allow_graduated=True)
    if reason is not None:
        return None, reason
    bound_state_fn = _check_state_fn(chain, mint, rpc_url, state, state_fn)
    result = dex_flap_curve_exit_quote(
        chain, mint, _NATIVE, amount_atomic, taker, http=http,
        slippage_bps=slippage_bps, rpc_url=rpc_url, state_fn=bound_state_fn, simulate_fn=simulate_fn,
        overrides_fn=overrides_fn, pre_entry=pre_entry)
    if result is None:
        return None, EXIT_UNQUOTABLE
    if (result.get("route") or {}).get("quote_quality") != "exact":
        return None, NOT_EXACT
    return result, None


# The curve facts the safety check's own state read hands to the lane's decision features.
CURVE_FACT_FIELDS = ("status", "reserve", "circulating_supply", "price", "r", "h",
                     "dex_supply_thresh", "quote_token", "coin_reserve", "fee_bps")


def wrap_token_safety_fn(chain, rpc_url, *, http=None, taker, fallback_fn,
                         native_usd_price_fn=None, weth=None, state_fn=None, simulate_fn=None,
                         overrides_fn=None, safety_deadline_scope=None, safety_deadline_s=8.0):
    """Wrap the chain's standard token-safety function so a live flap.sh curve mint is checked
    with this rung's own exact whole-balance quote instead of the standard dust-probe cascade --
    mirrors `evm_launchpad_curve.py`'s function of the same name, chain-4663's equivalent.

    THIS WAS NOT BUILDABLE BEFORE THE ALLOWANCE OVERRIDE (2026-09-22 addendum). An earlier review
    (Gamma, on 07d8db29/4dea7ba9) said this rung "cannot" run a pre-entry clip check because an
    exact flap exit quote needs a token balance and an allowance, neither of which exists before
    entry -- true of the real-simulation-or-estimate architecture that existed then. The
    allowance override fabricates both for a SIMULATION regardless of what is actually held, so a
    hypothetical "if I bought this amount, could I exit it" check is exact now, the same as it
    always was for chain 4663's view-call-based curve.

    Every mint this does not recognise -- not a flap.sh token, non-native quote, already
    graduated, or neither `usd` nor `bought_amount_atomic` supplied -- falls straight through to
    `fallback_fn` unchanged, identical in shape to the launchpad version."""
    state_fn = state_fn or curve_state
    weth = weth or _chain_weth(chain)

    from sniper.execution.evm_token_safety import names_timings, timed
    fallback_times = names_timings(fallback_fn)

    def check(mint, *, deadline=None, usd=None, bought_amount_atomic=None, timings=None,
              **kwargs):
        # SUB-MARKS, DISPLAY ONLY (2026-09-28, Alpha): the curve's own reads, then the
        # standard check's code/sell-quote/slot/simulation split when it runs.
        bound_amount = None
        if bought_amount_atomic is not None:
            try:
                bound_amount = max(0, int(bought_amount_atomic))
            except (TypeError, ValueError, OverflowError):
                bound_amount = 0
        if bound_amount or usd:
            state = timed(timings, "curve_state_ms", state_fn, chain, mint, rpc_url, http=http)
            if timings is not None and state:
                # MEASUREMENT ONLY (decision features, spec 2026-10-14 4.3): the read above, kept.
                timings["flap_curve_state"] = {k: state.get(k) for k in CURVE_FACT_FIELDS}
            if refusal_reason(mint, state) is None:            # entry-eligible: native, on-curve
                bound_state_fn = _check_state_fn(chain, mint, rpc_url, state, state_fn)
                if bound_amount:
                    buy_amount = bound_amount
                else:
                    if native_usd_price_fn is None:
                        from sniper.execution.evm_wallet import native_usd_price
                        px = timed(timings, "curve_price_ms", native_usd_price, chain, http=http)
                    else:
                        px = timed(timings, "curve_price_ms", native_usd_price_fn, chain)
                    if not px:
                        return False, "flap_curve_price_unavailable"
                    try:
                        amount_in = int(float(usd) / float(px) * 10 ** 18)
                    except (TypeError, ValueError, ZeroDivisionError):
                        amount_in = 0
                    if amount_in <= 0:
                        return False, "flap_curve_amount_unavailable"
                    buy = timed(timings, "curve_entry_quote_ms", dex_flap_curve_quote,
                        chain, weth, mint, amount_in, taker, http=http, rpc_url=rpc_url,
                        state_fn=bound_state_fn, simulate_fn=simulate_fn)
                    if buy is None:
                        return False, "flap_curve_entry_unquotable"
                    buy_amount = int(buy["buyAmount"])
                quote, reason = timed(
                    timings, "curve_exit_quote_ms", whole_balance_exit_quote_or_refuse,
                    chain, mint, buy_amount, rpc_url, taker=taker, http=http,
                    state_fn=bound_state_fn, simulate_fn=simulate_fn, overrides_fn=overrides_fn,
                    pre_entry=True)
                if quote is None:
                    return False, reason or EXIT_UNQUOTABLE
                return True, "flap_curve_exit_quotable_at_clip"
        if timings is not None and fallback_times:
            kwargs["timings"] = timings
        return fallback_fn(mint, deadline=deadline, usd=usd,
                           bought_amount_atomic=bought_amount_atomic, **kwargs)

    def wrapped(mint, *, deadline=None, usd=None, bought_amount_atomic=None, timings=None,
                **kwargs):
        kwargs["timings"] = timings
        if safety_deadline_scope is None:
            return check(mint, deadline=deadline, usd=usd,
                         bought_amount_atomic=bought_amount_atomic, **kwargs)
        if deadline is None:
            import time
            deadline = time.monotonic() + float(safety_deadline_s)
        with safety_deadline_scope(deadline) as expired:
            if expired():
                return False, "safety_deadline_exceeded"
            result = check(mint, deadline=deadline, usd=usd,
                           bought_amount_atomic=bought_amount_atomic, **kwargs)
            return (False, "safety_deadline_exceeded") if expired() else result

    return wrapped
