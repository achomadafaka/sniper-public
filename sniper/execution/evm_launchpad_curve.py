"""Buy and sell a chain-4663 launchpad coin on its own bonding curve, before graduation.

Spec: docs/superpowers/specs/2026-09-17-memecoin-4663-launchpad-curve-venue-design.md.

The curve exposes `buy(uint256 amountIn, uint256 minAmountOut, address recipient)` (selector
`0x59a87bc1`) and `sell(uint256 amountIn, uint256 minAmountOut, address recipient)` (selector
`0xd04c6983`). Both are atomic: a native-counter coin pays through `msg.value`, an ERC-20-counter
coin pulls `amountIn` through `transferFrom` after a prior exact-amount `approve`. There is no
two-step path here on purpose -- a pay-first-then-call design would leave a window where anyone
else could claim a payment we sent ahead of the call, and the spec's evidence shows the direct
interface does not need it.

The quote is a closed-form constant-product formula read from the curve's own `getReserves()`
and `feeBps()`. The formula, and the rule that the fee is taken OUT of `amountIn` before it
changes the reserves, were both verified against real `eth_simulateV1` calls at 3 sizes each
direction (buy and sell), all within 0.1% -- see the spec's phase-3 evidence. This module never
guesses a number the chain itself can answer.
"""
from __future__ import annotations

import logging
import math
from sniper.services.memecoin_evm_gas_topup import WETH_CALL_GAS_LIMIT

logger = logging.getLogger("memecoin")

_NATIVE = "0x" + "00" * 20
_WORD = 64                                  # one 32-byte ABI word, in hex characters
_BPS = 10_000

_SEL_RESERVES = "0902f1ac"                  # getReserves()
_SEL_FEE_BPS = "24a9d853"                   # feeBps()
_SEL_BUY = "59a87bc1"                       # buy(uint256,uint256,address)
_SEL_SELL = "d04c6983"                      # sell(uint256,uint256,address)
_SEL_APPROVE = "095ea7b3"                   # approve(address,uint256)
_SEL_WETH_WITHDRAW = "2e1a7d4d"             # withdraw(uint256) -- unwrap WETH to native


def _chain_weth(chain):
    """This chain's bankroll token, lowercased, or "" when unconfigured.

    The entry/exit ladder is always called with THIS token as the cash side (`quote_token` in
    `evm_real_seams.py`), never with the literal native address -- our wallet holds WETH, not
    native, on chain 4663 (`CHAIN_QUOTE["4663"]["token"]`). A native-counter curve must therefore
    treat this token as interchangeable with native for MATCHING purposes, the same way
    `dex_v4_quote` does (`is_buy = str(sell_token).lower() == weth`), and must unwrap it to real
    native before paying the curve, via a `pre_transactions` entry the executor already runs
    generically ahead of the main swap (the same mechanism `dex_v4_quote`'s native buy path uses).
    """
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    return (CHAIN_QUOTE.get(str(chain)) or {}).get("token", "").lower()


def _counter_matches(counter, token, weth):
    """Is `token` an acceptable way to pay/receive `counter`? Exact match, or WETH standing in for
    a native counter (see `_chain_weth`)."""
    token = str(token).lower()
    if token == counter:
        return True
    return counter == _NATIVE and weth and token == weth

_COIN_WORD, _CURVE_WORD, _COUNTER_WORD, _STATE_WORD = 0, 1, 4, 10


def _words(raw):
    """Split an `eth_call` result into 32-byte words, or [] when there is nothing to read."""
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


def _call(to, data, rpc_url, *, http=None):
    """One read-only `eth_call` through the shared read layer. None on any failure."""
    from sniper.execution.evm_read_rpc import eth_call_raw
    try:
        return eth_call_raw(to, data, rpc_url, http=http)
    except Exception:
        logger.exception("memecoin: launchpad curve eth_call failed")
        return None


def _launchpad_entry(chain):
    """This chain's registered launchpad row, reusing the existing V4-derivation registry
    (`MEMECOIN_EVM_V4_LAUNCHPADS`) rather than a second, separately-maintained address."""
    from sniper.settings import memecoin as cfg
    entries = (getattr(cfg, "MEMECOIN_EVM_V4_LAUNCHPADS", {}) or {}).get(str(chain), ())
    return entries[0] if entries else None


def curve_state(chain, mint, rpc_url, *, http=None):
    """{"coin", "curve", "counter", "fee_bps", "state"} for `mint`, or None when unreadable.

    `state` is 0 while the coin trades on its own curve and 2 once it has graduated. The caller
    must re-read this before every quote and every sell -- the curve's own reserves are drained to
    0 at graduation and a curve `sell` built after that point cannot pay out.
    """
    entry = _launchpad_entry(chain)
    if entry is None or not rpc_url or not mint:
        return None
    arg = str(mint)[2:].lower().rjust(_WORD, "0")
    raw = _call(entry["address"], "0x" + entry["info_selector"] + arg, rpc_url, http=http)
    words = _words(raw)
    coin = _address_at(words, _COIN_WORD)
    if coin is None or coin != str(mint).lower():
        return None
    curve = _address_at(words, _CURVE_WORD)
    counter = _address_at(words, _COUNTER_WORD)
    state = _int_at(words, _STATE_WORD)
    if curve is None or counter is None or state is None:
        return None
    fee_bps = _int_at(_words(_call(curve, "0x" + _SEL_FEE_BPS, rpc_url, http=http)), 0)
    if fee_bps is None:
        return None
    return {"coin": coin, "curve": curve, "counter": counter,
            "fee_bps": int(fee_bps), "state": int(state)}


def curve_reserves(curve, rpc_url, *, http=None):
    """(reserve_counter, reserve_coin) from the curve's own `getReserves()`, or None."""
    words = _words(_call(curve, "0x" + _SEL_RESERVES, rpc_url, http=http))
    reserve_counter = _int_at(words, 0)
    reserve_coin = _int_at(words, 1)
    if reserve_counter is None or reserve_coin is None:
        return None
    return reserve_counter, reserve_coin


def cpamm_out(reserve_in, reserve_out, amount_in, fee_bps):
    """Constant-product-with-fee output. Verified against real `eth_simulateV1` buys and sells at
    3 sizes each direction, within 0.1% (spec section 9 and the phase-3 evidence). The fee comes
    OUT of `amount_in` before it changes the reserves -- confirmed by reading `getReserves()`
    immediately before and after a real simulated buy: the counter reserve grows by
    `amount_in * (10000 - fee_bps) / 10000`, never by the full `amount_in`.
    """
    amount_in = int(amount_in)
    reserve_in = int(reserve_in)
    reserve_out = int(reserve_out)
    if (amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0
            or not 0 <= int(fee_bps) < _BPS):
        return 0
    amount_in_with_fee = amount_in * (_BPS - int(fee_bps))
    return (amount_in_with_fee * reserve_out) // (reserve_in * _BPS + amount_in_with_fee)


def encode_buy(amount_in, min_amount_out, recipient):
    """`buy(uint256 amountIn, uint256 minAmountOut, address recipient)`, selector `0x59a87bc1`."""
    return ("0x" + _SEL_BUY + f"{int(amount_in):064x}" + f"{int(min_amount_out):064x}"
            + str(recipient)[2:].lower().rjust(_WORD, "0"))


def encode_sell(amount_in, min_amount_out, recipient):
    """`sell(uint256 amountIn, uint256 minAmountOut, address recipient)`, selector `0xd04c6983`."""
    return ("0x" + _SEL_SELL + f"{int(amount_in):064x}" + f"{int(min_amount_out):064x}"
            + str(recipient)[2:].lower().rjust(_WORD, "0"))


def encode_approve(spender, amount):
    """Exact-amount ERC-20 `approve(spender, amount)` -- never an unlimited approval."""
    return ("0x" + _SEL_APPROVE + str(spender)[2:].lower().rjust(_WORD, "0")
            + f"{int(amount):064x}")


def _min_out(amount_out, slippage_bps):
    if not 0 <= int(slippage_bps) < _BPS:
        return 0
    return (int(amount_out) * (_BPS - int(slippage_bps))) // _BPS


def _route_gas():
    """MEMECOIN_EVM_DEFAULT_ROUTE_GAS: this builder's transaction carries its own limit. A knob at 0
    or below would sign gas 0 ("0" is truthy to the signer): the default applies instead."""
    from sniper.settings import memecoin as _cfg
    return int(_cfg.MEMECOIN_EVM_DEFAULT_ROUTE_GAS) if int(_cfg.MEMECOIN_EVM_DEFAULT_ROUTE_GAS) > 0 \
        else 900_000


def dex_launchpad_curve_quote(chain, sell_token, buy_token, amount_atomic, taker, *, http=None,
                              slippage_bps=None, rpc_url=None, state_fn=None, reserves_fn=None):
    """Executable curve-buy quote for `amount_atomic` of `sell_token` -> `buy_token` (the coin),
    shaped like `dex_v3_quote`/`dex_v4_quote` so it can sit as a rung. None (fail closed) when the
    coin is not on this launchpad, is not on its curve any more, or `sell_token` is not the
    curve's own counter currency.
    """
    from sniper.settings import memecoin as cfg
    if slippage_bps is None:
        slippage_bps = int(round(cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT * 100))
    try:
        amount_atomic = int(amount_atomic)
    except (TypeError, ValueError):
        return None
    if amount_atomic <= 0 or not rpc_url:
        return None
    state_fn = state_fn or curve_state
    reserves_fn = reserves_fn or curve_reserves
    state = state_fn(chain, buy_token, rpc_url, http=http)
    if state is None or state["state"] != 0:
        return None
    weth = _chain_weth(chain)
    if not _counter_matches(state["counter"], sell_token, weth):
        return None                          # this rung only prices the curve's own counter currency
    reserves = reserves_fn(state["curve"], rpc_url, http=http)
    if reserves is None:
        return None
    reserve_counter, reserve_coin = reserves
    buy_amount = cpamm_out(reserve_counter, reserve_coin, amount_atomic, state["fee_bps"])
    if buy_amount <= 0:
        return None
    min_out = _min_out(buy_amount, slippage_bps)
    if min_out <= 0:
        return None
    is_native = state["counter"] == _NATIVE
    paying_with_weth = is_native and str(sell_token).lower() == weth
    data = encode_buy(amount_atomic, min_out, taker)
    tx = {"to": state["curve"], "data": data, "value": hex(amount_atomic) if is_native else "0x0",
          "gas": str(_route_gas())}
    issues = {} if is_native else {"allowance": {"spender": state["curve"]}}
    pre_transactions = (
        [{"to": weth, "data": "0x" + _SEL_WETH_WITHDRAW + f"{amount_atomic:064x}",
          "value": "0x0", "gas": str(WETH_CALL_GAS_LIMIT)}]
        if paying_with_weth else [])
    return {
        "sellAmount": str(amount_atomic),
        "buyAmount": str(buy_amount),
        "minBuyAmount": str(min_out),
        "liquidityAvailable": True,
        "pre_transactions": pre_transactions,
        "transaction": tx,
        "issues": issues,
        "route": {"protocol": "launchpad_curve",
                  "tokens": [str(sell_token).lower(), str(buy_token).lower()],
                  "curve": state["curve"]},
    }


def dex_launchpad_curve_exit_quote(chain, sell_token, buy_token, amount_atomic, taker, *,
                                   http=None, slippage_bps=None, rpc_url=None, state_fn=None,
                                   reserves_fn=None):
    """Executable curve-sell quote for `amount_atomic` of `sell_token` (the coin) -> `buy_token`
    (the counter currency). None (fail closed) once the coin has graduated -- the caller must
    switch to the derived V4 pool key path at that point, never build a curve sell.
    """
    from sniper.settings import memecoin as cfg
    if slippage_bps is None:
        slippage_bps = int(round(cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT * 100))
    try:
        amount_atomic = int(amount_atomic)
    except (TypeError, ValueError):
        return None
    if amount_atomic <= 0 or not rpc_url:
        return None
    state_fn = state_fn or curve_state
    reserves_fn = reserves_fn or curve_reserves
    state = state_fn(chain, sell_token, rpc_url, http=http)
    if state is None or state["state"] != 0:
        return None
    if not _counter_matches(state["counter"], buy_token, _chain_weth(chain)):
        return None
    # NO unwrap/wrap step is needed on the sell side. A native payout lands as a plain balance
    # change, not an ERC-20 Transfer, and this codebase's own exit accounting already has a
    # fallback for exactly that: `_receipt_quote_usd` (evm_real_seams.py) tries an ERC-20 Transfer
    # of the quote token first, and when the chain's quote `kind` is "eth" and that finds nothing,
    # falls back to `native_received_in_receipt` -- a before/after native balance read at the
    # sell's own block, gas-corrected. A curve sell (msg.sender pays no value, receives native
    # back) is exactly that shape, so realized proceeds are measured correctly with no new code
    # on the accounting side. Verified by reading that function, not assumed.
    reserves = reserves_fn(state["curve"], rpc_url, http=http)
    if reserves is None:
        return None
    reserve_counter, reserve_coin = reserves
    payout = cpamm_out(reserve_coin, reserve_counter, amount_atomic, state["fee_bps"])
    if payout <= 0:
        return None
    min_out = _min_out(payout, slippage_bps)
    if min_out <= 0:
        return None
    data = encode_sell(amount_atomic, min_out, taker)
    return {
        "sellAmount": str(amount_atomic),
        "buyAmount": str(payout),
        "minBuyAmount": str(min_out),
        "liquidityAvailable": True,
        "transaction": {"to": state["curve"], "data": data, "value": "0x0", "gas": str(_route_gas())},
        "issues": {"allowance": {"spender": state["curve"]}},
        "route": {"protocol": "launchpad_curve",
                  "tokens": [str(sell_token).lower(), str(buy_token).lower()],
                  "curve": state["curve"]},
    }


EXIT_UNQUOTABLE = "launchpad_curve_exit_unquotable_at_clip"
STATE_UNREADABLE = "launchpad_curve_state_unreadable"
GRADUATED_NO_CONFIRMED_KEY = "launchpad_curve_graduated_no_confirmed_v4_key"
GRADUATED_NO_QUOTE_FN = "launchpad_curve_graduated_no_v4_quote_fn"


def whole_balance_exit_quote_or_refuse(chain, mint, amount_atomic, rpc_url, *, taker, http=None,
                                       slippage_bps=None, state_fn=None, reserves_fn=None):
    """This rung's own clip-size safety check: an exact whole-balance quote from the curve
    itself, or an outright refusal -- never a fallback to a smaller probe. A dust-sized probe can
    pass while a real clip-sized position cannot exit (the same failure the Arc-only
    `MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS` gate exists to catch); this function is the equivalent
    behaviour for this rung, implemented entirely in this module so it can never touch that shared
    per-chain gate or change V3/V4 behaviour on any chain.

    Returns `(quote_dict, None)` on a priceable exit, or `(None, reason)` on refusal.
    """
    state_fn = state_fn or curve_state
    state = state_fn(chain, mint, rpc_url, http=http)
    if state is None:
        return None, STATE_UNREADABLE
    if state["state"] != 0:
        return None, GRADUATED_NO_CONFIRMED_KEY            # not this rung's job past graduation
    quote = dex_launchpad_curve_exit_quote(
        chain, mint, state["counter"], amount_atomic, taker, http=http,
        slippage_bps=slippage_bps, rpc_url=rpc_url, state_fn=state_fn, reserves_fn=reserves_fn)
    if quote is None:
        return None, EXIT_UNQUOTABLE
    return quote, None


def _default_derived_pool_keys_fn(chain, mint, rpc_url, *, http=None):
    from sniper.execution.evm_v4_launchpad import derived_pool_keys
    return derived_pool_keys(chain, mint, rpc_url, http=http)


def exit_quote(chain, mint, amount_atomic, rpc_url, *, taker, http=None, slippage_bps=None,
               state_fn=None, reserves_fn=None, derived_pool_keys_fn=None, v4_quote_fn=None):
    """The exit path's graduation switch. Re-reads launchpad state on every call -- never a cached
    value -- so a position that graduates mid-hold moves to the derived V4 pool on its very next
    quote, and a curve `sell` is never built once the launchpad reports state != 0 (the curve's
    own reserves are drained at graduation and cannot pay one out).

    Returns `(quote_dict, None)` or `(None, reason)`. Past graduation this module does not price
    the V4 pool itself -- it reuses `derived_pool_keys()` unchanged and hands the confirmed key(s)
    to the caller's own V4 quote function (`v4_quote_fn`), so this rung never duplicates that
    derivation or its quoting.
    """
    state_fn = state_fn or curve_state
    state = state_fn(chain, mint, rpc_url, http=http)
    if state is None:
        return None, STATE_UNREADABLE
    if state["state"] == 0:
        return whole_balance_exit_quote_or_refuse(
            chain, mint, amount_atomic, rpc_url, taker=taker, http=http,
            slippage_bps=slippage_bps, state_fn=state_fn, reserves_fn=reserves_fn)
    derived_pool_keys_fn = derived_pool_keys_fn or _default_derived_pool_keys_fn
    keys = derived_pool_keys_fn(chain, mint, rpc_url, http=http)
    confirmed = [(pool_id, key) for pool_id, key, ok in keys if ok]
    if not confirmed:
        return None, GRADUATED_NO_CONFIRMED_KEY
    if v4_quote_fn is None:
        return None, GRADUATED_NO_QUOTE_FN
    return v4_quote_fn(chain, mint, amount_atomic, taker, confirmed,
                       http=http, slippage_bps=slippage_bps, rpc_url=rpc_url), None


def mark_price_entry(chain, mint, rpc_url, *, http=None, state_fn=None, reserves_fn=None,
                     observed_at=None):
    """One `memecoin_position_marks`-shaped price entry for a curve-phase position, so the exit
    rails and the position card are not blind pre-graduation. Writes to the SAME
    `memecoin_position_marks` table every other rung's marks go to
    (`sniper/services/memecoin_exits.py:write_marks`), tagged `source="launchpad_curve"`,
    `quality="live"` -- a direct on-chain `getReserves()` mid read, not an estimate.

    None once the coin has graduated or the curve cannot be read: the caller must not write a
    curve mark for a position that has already moved to its V4 pool, and must not write a mark it
    cannot back with a real read.
    """
    state_fn = state_fn or curve_state
    reserves_fn = reserves_fn or curve_reserves
    state = state_fn(chain, mint, rpc_url, http=http)
    if state is None or state["state"] != 0:
        return None
    reserves = reserves_fn(state["curve"], rpc_url, http=http)
    if reserves is None:
        return None
    reserve_counter, reserve_coin = reserves
    if reserve_coin <= 0 or reserve_counter <= 0:
        return None
    if state["counter"] not in (_NATIVE, _chain_weth(chain)):
        return None
    from sniper.execution.evm_wallet import erc20_decimals, native_usd_price
    decimals = erc20_decimals(mint, rpc_url, http=http)
    counter_usd = native_usd_price(chain, http=http)
    if (decimals is None or not 0 <= decimals <= 255 or counter_usd is None
            or not math.isfinite(counter_usd) or counter_usd <= 0):
        return None
    # Marks use USD per whole coin. Native and WETH both use 18 decimals.
    mid = reserve_counter / reserve_coin * 10 ** (decimals - 18) * counter_usd
    if not math.isfinite(mid) or mid <= 0:
        return None
    return {"price": mid, "observed_at": observed_at, "source": "launchpad_curve",
            "quality": "live", "liquidity": None, "mcap": None}


def recording_http(http, reads):
    """`http` with every request it carries noted on `reads` (operator 2026-10-14 via Alpha: explain
    OUTPOST's 615 ms curve_state): the provider asked (scheme://host[:port], never a key), the
    JSON-RPC method (or the HTTP verb), an eth_call's 4-byte selector, wall ms and the outcome. It
    sees the read layer's own identity probes and hedged duplicates too. DISPLAY ONLY: the answer
    and any exception pass through untouched, recording never raises, and the attributes the read
    layer probes with getattr are carried over (functools.wraps). `http` None is the read layer's
    default transport, resolved per call."""
    import functools
    import json
    import time
    from sniper.execution import evm_wallet

    def send(method, url, *args, **kwargs):
        return (http or evm_wallet._http)(method, url, *args, **kwargs)

    def recorded(method, url, *args, **kwargs):
        rpc = sel = None
        try:
            body = kwargs.get("body", args[1] if len(args) > 1 else None)
            payload = json.loads(body) if body else None
            if isinstance(payload, dict):
                rpc = payload.get("method")
                if rpc == "eth_call":
                    sel = str(payload["params"][0]["data"])[2:10] or None
        except Exception:
            pass
        started, outcome = time.perf_counter(), "error"
        try:
            answer = send(method, url, *args, **kwargs)
            outcome = "http_%s" % (answer[0] if isinstance(answer, tuple) and answer else "?")
            return answer
        except BaseException as exc:
            status = getattr(exc, "http_status", None)
            outcome = ("timeout" if isinstance(exc, TimeoutError)
                       else "http_%d" % status if type(status) is int
                       else "raise:%s" % type(exc).__name__)
            raise
        finally:
            try:
                reads.append({"host": evm_wallet._tap_endpoint(url), "rpc": rpc or method,
                              "sel": sel, "ms": round((time.perf_counter() - started) * 1000.0, 1),
                              "outcome": outcome})
            except Exception:
                pass
    return functools.wraps(http)(recorded) if http is not None else recorded


def wrap_token_safety_fn(chain, rpc_url, *, http=None, taker, fallback_fn, native_usd_price_fn=None,
                         weth=None, state_fn=None, reserves_fn=None,
                         safety_deadline_scope=None, safety_deadline_s=8.0):
    """Wrap the chain's standard token-safety function so a live curve-phase mint (still on its
    curve, native or WETH counter) is checked with this rung's own exact whole-balance quote
    instead of the standard dust-probe cascade -- the dust probe never applies here in the first
    place, since this rung was never wired into it (see the spec's phase-3 evidence: this check is
    implemented entirely in this module so it can never touch
    `MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS` or change any other chain's/rung's behaviour).

    BOUND IDENTITY (2026-09-17, matching `54ffd4ec` "Bind ARC exit safety to each actual entry
    quote"): when the caller already knows the exact amount its own entry quote will buy
    (`bought_amount_atomic`), this checks sellability of THAT amount, never a second,
    independently-derived estimate that could disagree with what actually fills. `usd`-based
    sizing (a fresh probe quote) is the fallback only when the real amount is not yet known --
    unchanged from before, and still what chain 4663 uses today (it is not a member of
    `MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS`, so the executor's post-quote `bought_amount_atomic`
    path is not wired to this chain; this still keeps the contract correct if that ever changes).

    `bought_amount_atomic` is always forwarded to `fallback_fn` unchanged (`None` when the caller
    did not supply it, identical to omitting it). Every mint this does not recognise -- graduated,
    a different launchpad, a non-native/WETH counter, or neither `usd` nor `bought_amount_atomic`
    supplied -- falls straight through to `fallback_fn` unchanged.

    Price callbacks that declare `http` receive this check's recording/deadline transport;
    chain-only callbacks retain their existing call shape.
    """
    state_fn = state_fn or curve_state
    weth = weth or _chain_weth(chain)

    from sniper.execution.evm_token_safety import names_timings, timed
    fallback_times = names_timings(fallback_fn)
    import inspect
    try:
        price_http = inspect.signature(native_usd_price_fn).parameters.get("http")
        price_http = price_http is not None and price_http.kind != inspect.Parameter.POSITIONAL_ONLY
    except (TypeError, ValueError):
        price_http = False

    def check(mint, *, deadline=None, usd=None, bought_amount_atomic=None, timings=None,
              **kwargs):
        # SUB-MARKS, DISPLAY ONLY (2026-09-28, Alpha): the curve's own reads, then the
        # standard check's code/sell-quote/slot/simulation split when it runs. Every curve read is
        # also marked one by one on timings["curve_reads"] (2026-10-14).
        reads = [] if timings is not None else None
        try:
            return curve_check(mint, deadline=deadline, usd=usd,
                               bought_amount_atomic=bought_amount_atomic, timings=timings,
                               read_http=recording_http(http, reads) if reads is not None else http,
                               **kwargs)
        finally:
            if reads:
                timings.setdefault("curve_reads", []).extend(reads)

    def curve_check(mint, *, deadline, usd, bought_amount_atomic, timings, read_http, **kwargs):
        from sniper.settings import memecoin as cfg
        bound_amount = None
        if bought_amount_atomic is not None:
            try:
                bound_amount = max(0, int(bought_amount_atomic))
            except (TypeError, ValueError, OverflowError):
                bound_amount = 0
        if bound_amount or usd:
            state = timed(timings, "curve_state_ms", state_fn, chain, mint, rpc_url, http=read_http)
            if (state is not None and state["state"] == 0
                    and state["counter"] in (_NATIVE, weth)):
                quote_state_fn, quote_reserves_fn = state_fn, reserves_fn
                if getattr(cfg, "MEMECOIN_EVM_CURVE_SAFETY_ONE_READ", False):
                    # ONE READ PER CHECK (B+C, operator 2026-10-14): this state and one getReserves
                    # answer the entry and exit quotes, which re-read both (8 state calls, 2 reserve
                    # calls on OUTPOST). An absent read stays absent: the quotes refuse on it.
                    reserves = timed(timings, "curve_reserves_ms", reserves_fn or curve_reserves,
                                     state["curve"], rpc_url, http=read_http)
                    quote_state_fn = lambda *a, **k: state
                    quote_reserves_fn = lambda *a, **k: reserves
                if bound_amount:
                    buy_amount = bound_amount
                else:
                    if native_usd_price_fn is None:
                        from sniper.execution.evm_wallet import native_usd_price
                        px = timed(timings, "curve_price_ms", native_usd_price, chain, http=read_http)
                    else:
                        px = timed(timings, "curve_price_ms", native_usd_price_fn, chain,
                                   **({"http": read_http} if price_http else {}))
                    if not px:
                        return False, "launchpad_curve_price_unavailable"
                    try:
                        amount_in = int(float(usd) / float(px) * 10 ** 18)
                    except (TypeError, ValueError, ZeroDivisionError):
                        amount_in = 0
                    if amount_in <= 0:
                        return False, "launchpad_curve_amount_unavailable"
                    buy = timed(timings, "curve_entry_quote_ms", dex_launchpad_curve_quote,
                        chain, weth, mint, amount_in, taker, http=read_http, rpc_url=rpc_url,
                        state_fn=quote_state_fn, reserves_fn=quote_reserves_fn)
                    if buy is None:
                        return False, "launchpad_curve_entry_unquotable"
                    buy_amount = int(buy["buyAmount"])
                quote, reason = timed(
                    timings, "curve_exit_quote_ms", whole_balance_exit_quote_or_refuse,
                    chain, mint, buy_amount, rpc_url, taker=taker, http=read_http,
                    state_fn=quote_state_fn, reserves_fn=quote_reserves_fn)
                if quote is None:
                    return False, reason or EXIT_UNQUOTABLE
                return True, "launchpad_curve_exit_quotable_at_clip"
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
