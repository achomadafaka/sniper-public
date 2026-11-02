"""Direct Pons V2 bonding-curve quotes for Robinhood chain 4663."""
from __future__ import annotations

from eth_abi import encode
from eth_utils import keccak
from sniper.services.memecoin_evm_gas_topup import WETH_CALL_GAS_LIMIT


GMGN_ROUTER = "0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc"
_WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
_ZERO = "0x" + "00" * 20
_BPS = 10_000
_GMGN_FEE_BPS = 100  # GMGN documents a 1% fee. Live min-out checks matched this value exactly.
_PONS_ROUTE_TYPE = 27
_NATIVE_INPUT_FLAG = (1).to_bytes(32, "big")
_SWAP_SELECTOR = "4d819a2a"
_WITHDRAW_SELECTOR = "2e1a7d4d"


def _selector(signature):
    return keccak(text=signature)[:4].hex()


_CURVE = _selector("curve()")
_GETTERS = tuple(map(_selector, (
    "token()", "pairToken()", "getReserves()", "feeBps()",
    "creatorTaxBps()", "graduated()", "sellableTokens()",
)))


def _word(value):
    return int(value).to_bytes(32, "big").hex()


def _address(raw):
    try:
        body = str(raw)[2:]
        if not str(raw).startswith('0x') or len(body) != 64 or int(body[:24], 16) != 0:
            return None
        int(body, 16)
        value = "0x" + body[-40:].lower()
        return None if value == _ZERO else value
    except Exception:
        return None


def _uint(raw, index=0):
    try:
        body = str(raw)[2:]
        return int(body[index * 64:(index + 1) * 64], 16) if len(body) >= (index + 1) * 64 else None
    except (TypeError, ValueError):
        return None


class PonsReadUnavailable(Exception):
    """The Pons reader could not be READ. Never "this coin has no Pons route".

    Same contract as `RelayQuoteError` and `V3ReadUnavailable`: the ladder's `_run` reads
    `failure_type` off a raised exception to set `blind_reason`.
    """

    def __init__(self, failure_type, detail=""):
        super().__init__(f"{failure_type}: {detail}" if detail else failure_type)
        self.failure_type = failure_type


def _make_call_fn(rpc_url, http=None):
    """A hex result, or None for a call that REVERTED.

    `evm_wallet._rpc` raises on transport, so that half was already reported as blind by accident of
    the helper this borrows. A JSON-RPC ERROR BODY was not: a 429 returned as a 200, a
    `limit exceeded`, a node error — all became None, and the ladder published a terminal
    `no_route` for a read we never made. A revert still returns None, because a revert is the node
    answering and a coin with no pool must not re-quote forever.
    """
    from sniper.execution.evm_dex_quote import _classify_rpc_error
    from sniper.execution.evm_wallet import _rpc

    def call(to, data):
        result = _rpc(rpc_url, "eth_call", [{"to": to, "data": data}, "latest"], http=http)
        kind = _classify_rpc_error(result)
        if kind == "unavailable":
            raise PonsReadUnavailable(
                "rpc_transport", str((result or {}).get("error") if isinstance(result, dict) else result)[:120])
        return None if kind == "revert" else result.get("result")

    def batch(calls):
        """The seven curve getters in parallel (2026-09-23). Every graduated Pons coin -- most
        4663 memecoins -- paid 1 + 7 SERIAL reads here before the rung answered None, and on a
        loaded custody RPC that is the 8 s ladder deadline: the SELL ladder waits for this rung
        before it may take v4, so ROOM 64049's manual close spent 15.9 s in two quotes. Same
        calls, same order, one round trip; an unavailable read still raises."""
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, len(calls)),
                                thread_name_prefix="pons-read") as pool:
            return list(pool.map(lambda c: call(*c), calls))
    call.batch = batch
    from sniper.execution.evm_relay_quote import warm_relay_code_hashes
    call.verify_relay = lambda: warm_relay_code_hashes(rpc_url, http=http)
    return call


def _curve_state(mint, call_fn, miss_out=None, counter_out=None):
    """The curve's state, or None with a TYPED reason in `miss_out` where one is warranted.

    WHY THE REASON EXISTS (2026-09-24). This rung reported `hit=False blind_reason=None reason=None`
    on 14 of 18 runs on 4663 since 09-23, so no reader could tell "not a Pons token" from "the coin
    left the curve". Classified live over the 7 distinct mints behind those misses: `curve()`
    answered on 7 of 7 and every one read `graduated=1`, `sellableTokens()=0`. The decline was
    right; the silence was not.

    A REASON IS NEVER A BLIND REASON. A graduated curve is a decided fact about the venue, and
    reporting it as blindness would turn a correct refusal into `provider_*` and change how the
    runner gates. A read that RAISES still raises, above this function.

    Silent (no reason) on purpose, because 4663 is full of these and a word for each would be
    noise, not signal: a token with no `curve()` at all, a curve whose `token()` is some other
    mint, and a confiscatory fee+tax. Those keep exactly today's behaviour.
    """
    def _miss(reason):
        if miss_out is not None and reason:
            miss_out["reason"] = reason
        return None

    curve = _address(call_fn(mint, "0x" + _CURVE))
    if curve is None:
        return None                              # not a Pons token: silent, as before
    calls = [(curve, "0x" + selector) for selector in _GETTERS]
    batch = getattr(call_fn, "batch", None)
    values = batch(calls) if callable(batch) else [call_fn(*call) for call in calls]
    if len(values) != len(calls):
        return _miss("curve_state_unreadable")
    token = _address(values[0])
    pair = _address(values[1])
    if pair is None:
        if values[1] != '0x' + '0' * 64:
            return _miss('curve_state_unreadable')
        pair = _ZERO
    quote_reserve, token_reserve = _uint(values[2], 0), _uint(values[2], 1)
    fee, tax = _uint(values[3]), _uint(values[4])
    graduated, sellable = _uint(values[5]), _uint(values[6])
    if None in (quote_reserve, token_reserve, fee, tax, sellable, graduated):
        # THE CURVE ANSWERED ITS OWN ADDRESS AND THEN A GETTER SAID NOTHING. That is a state we
        # could not read, which is a third thing from graduated and from not-Pons.
        return _miss("curve_state_unreadable")
    if graduated != 0:
        return _miss("curve_graduated")
    if pair != _ZERO:
        if counter_out is None:
            return _miss('curve_counter_not_prepared')
        counter_out['counter'] = pair
    if token != mint or quote_reserve <= 0 or token_reserve <= 0 or fee + tax >= _BPS:
        return None                              # unverified curve / confiscatory fee: as before
    return curve, quote_reserve, token_reserve, fee, tax, sellable, graduated


def _swap_data(route, amount_in, min_out, deadline):
    types = [
        "(uint8,address,address,address,uint24,int24,address,bytes,address,bytes32)[]",
        "address", "uint256", "uint256", "uint256",
    ]
    return "0x" + _SWAP_SELECTOR + encode(
        types, [(route,), _ZERO, int(amount_in), int(min_out), int(deadline)],
    ).hex()


def pons_round_trip(amount, quote_reserve, token_reserve, fee_bps, tax_bps,
                    router_fee_bps=_GMGN_FEE_BPS):
    """(round_trip_pct, tokens_bought) for buying `amount` and selling it straight back, or None.

    A PURE FUNCTION OF STATE THE QUOTE ALREADY READ. It costs no RPC: the curve is a constant
    product, so the sell side is computable from the reserves the buy leaves behind. That is what
    makes this affordable on every decision, which is the only way a cost gate is worth having.

    The sell is priced on the POST-BUY reserves -- `q1 = quote_reserve + net`,
    `t1 = token_reserve - tokens` -- because that is the curve the exit would actually meet. Using
    the pre-buy reserves would flatter every clip by exactly the amount our own buy moved the
    price, and the bigger the clip the bigger the flattery.

    Integer arithmetic throughout, with the same truncation as the quote, so the number reported is
    the number the swap would produce and not a float approximation of it.
    """
    amount = int(amount)
    if amount <= 0 or int(quote_reserve) <= 0 or int(token_reserve) <= 0:
        return None
    if int(fee_bps) + int(tax_bps) >= _BPS or int(router_fee_bps) >= _BPS:
        return None
    router_net = amount - amount * int(router_fee_bps) // _BPS
    net = router_net - router_net * int(fee_bps) // _BPS - router_net * int(tax_bps) // _BPS
    if net <= 0:
        return None
    tokens = net * int(token_reserve) // (int(quote_reserve) + net)
    if tokens <= 0:
        return None
    q1 = int(quote_reserve) + net
    t1 = int(token_reserve) - tokens
    if t1 <= 0:
        return None                        # the clip would take the whole curve
    gross = tokens * q1 // (t1 + tokens)
    curve_net = gross - gross * int(fee_bps) // _BPS - gross * int(tax_bps) // _BPS
    back = curve_net - curve_net * int(router_fee_bps) // _BPS
    return 100.0 * (back / amount - 1.0), tokens


def dex_pons_v2_quote(chain, sell_token, buy_token, amount_atomic, taker, *, http=None,
                      slippage_bps=None, rpc_url=None, call_fn=None, now_fn=None,
                      miss_out=None):
    """Return one executable Pons V2 quote, or ``None`` for a non-Pons token.

    `miss_out` is an optional dict the caller owns; a decline that has a NAMED cause writes
    `{"reason": ...}` into it (see `_curve_state`). It never changes what this function returns.
    """
    if str(chain) != "4663":
        return None
    sell, buy = str(sell_token).lower(), str(buy_token).lower()
    is_buy = sell == _WETH and buy != _WETH
    is_sell = buy == _WETH and sell != _WETH
    if not (is_buy or is_sell):
        return None
    mint = buy if is_buy else sell
    try:
        amount = int(amount_atomic)
        if slippage_bps is None:
            from sniper.settings import memecoin as cfg
            slippage_bps = int(round((cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT if is_buy
                                      else cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER[-1]) * 100))
        slippage_bps = int(slippage_bps)
    except (TypeError, ValueError, IndexError):
        return None
    if amount <= 0 or not 0 <= slippage_bps < _BPS:
        return None
    from sniper.execution import evm_pons_counter_quote as counter_routes
    if is_buy:
        prepared = counter_routes.CACHE.quote(sell, buy, amount, taker, slippage_bps)
        if prepared is not None:
            return prepared
    if call_fn is None:
        if rpc_url is None:
            from sniper.settings import memecoin as cfg
            rpc_url = cfg.evm_rpc_for_chain(chain)
        if not rpc_url:
            return None
        call_fn = _make_call_fn(rpc_url, http=http)
    counter = {} if is_sell else None
    state = _curve_state(mint, call_fn, miss_out, counter_out=counter)
    if state is None:
        return None
    curve, quote_reserve, token_reserve, fee_bps, tax_bps, sellable, graduated = state
    if counter:
        return counter_routes.exit_quote(dict(mint=mint, curve=curve, counter=counter['counter'],
            quote_reserve=quote_reserve, token_reserve=token_reserve, fee_bps=fee_bps,
            tax_bps=tax_bps, sellable=sellable, graduated=graduated),
            amount, str(taker).lower(), slippage_bps, call_fn)

    if is_buy:
        router_net = amount - amount * _GMGN_FEE_BPS // _BPS
        net = router_net - router_net * fee_bps // _BPS - router_net * tax_bps // _BPS
        amount_out = net * token_reserve // (quote_reserve + net)
        if amount_out <= 0 or amount_out > sellable:
            # A LIVE CURVE WHOSE REMAINDER IS UNDER OUR CLIP. Both numbers travel with the reason,
            # in the mint's own atomic units, so a reader can size the gap rather than re-derive it.
            if miss_out is not None and amount_out > 0:
                miss_out["reason"] = ("curve_remainder_below_clip:sellable=%d,amount_out=%d"
                                      % (sellable, amount_out))
                miss_out["sellable"] = sellable
                miss_out["amount_out"] = amount_out
            return None
        route = (_PONS_ROUTE_TYPE, _ZERO, mint, curve, 0, 0, _ZERO, _NATIVE_INPUT_FLAG,
                 _ZERO, bytes(32))
    else:
        gross = amount * quote_reserve // (token_reserve + amount)
        curve_net = gross - gross * fee_bps // _BPS - gross * tax_bps // _BPS
        amount_out = curve_net - curve_net * _GMGN_FEE_BPS // _BPS
        if amount_out <= 0:
            return None
        route = (_PONS_ROUTE_TYPE, mint, _WETH, curve, 0, 0, _ZERO, bytes(32),
                 _ZERO, bytes(32))

    min_out = amount_out * (_BPS - slippage_bps) // _BPS
    if min_out <= 0:
        return None
    if now_fn is None:
        from sniper.util.clock import real_utc_now
        now_fn = real_utc_now
    deadline = int(now_fn()) + 600
    quote = {
        "sellAmount": str(amount), "buyAmount": str(amount_out),
        "minBuyAmount": str(min_out), "liquidityAvailable": True,
        "transaction": {"to": GMGN_ROUTER,
                        "data": _swap_data(route, amount, min_out, deadline),
                        "value": hex(amount) if is_buy else "0x0", "gas": "1000000"},
        "issues": {"allowance": None if is_buy else {"spender": GMGN_ROUTER}},
        "route": {"protocol": "pons_v2", "curve": curve,
                  "router_fee_bps": _GMGN_FEE_BPS,
                  "curve_fee_bps": fee_bps, "creator_tax_bps": tax_bps},
    }
    if is_buy:
        # THE COST OF THE ROUND TRIP, AND THE STATE IT WAS COMPUTED FROM. Recorded on the BUY only:
        # an exit is not a decision we can decline, so pricing its round trip would be telemetry
        # with no reader. SOLANO (52064) lost $120.60 in 59 seconds and nothing in the ledger
        # recorded the curve's reserves, fee or tax at entry, so the stop cannot be explained now.
        # These fields are what make that explainable next time, whatever the gate decides.
        measured = pons_round_trip(amount, quote_reserve, token_reserve, fee_bps, tax_bps)
        quote["route"].update({
            "quote_reserve": str(quote_reserve), "token_reserve": str(token_reserve),
            "sellable": str(sellable), "graduated": int(graduated),
            "clip_share_pct": round(100.0 * net / quote_reserve, 6),
            "round_trip_pct": (None if measured is None else round(measured[0], 6)),
        })
        quote["pre_transactions"] = [{
            "to": _WETH, "data": "0x" + _WITHDRAW_SELECTOR + _word(amount),
            "value": "0x0", "gas": str(WETH_CALL_GAS_LIMIT),
        }]
    return quote


# PONS CREATOR-TAX TEST (2026-09-12). The hook decides eligibility BEFORE the reservation, because the
# clip is frozen into the funded contract there, and arms `(chain, mint)` for that one entry. The pons
# rung reads it from a worker thread, so this is a plain dict, not a thread-local.
# ponytail: keyed by coin, not by wallet. A second wallet entering the same coin during the same
# entry would see the widened budget; the hook disarms in `finally` to keep that window short.
TEST_ALLOW = {}


def test_allows(chain, mint, round_trip_pct):
    """True when an armed test entry may accept this measured round trip."""
    cap = TEST_ALLOW.get((str(chain), str(mint).lower()))
    try:
        return cap is not None and abs(float(round_trip_pct)) <= cap
    except (TypeError, ValueError):
        return False


def test_round_trip_pct(chain, mint, *, call_fn=None, rpc_url=None, http=None):
    """The fee-dominated round trip of a small clip on this coin's Pons curve, or None.

    None for a non-Pons coin, a graduated curve, or a read that failed. A failed read never arms
    the test: the normal budget then applies, which is the state before the test existed.
    """
    if str(chain) != "4663":
        return None
    try:
        if call_fn is None:
            if rpc_url is None:
                from sniper.settings import memecoin as cfg
                rpc_url = cfg.evm_rpc_for_chain(chain)
            if not rpc_url:
                return None
            call_fn = _make_call_fn(rpc_url, http=http)
        state = _curve_state(str(mint).lower(), call_fn)
    except PonsReadUnavailable:
        return None
    if state is None:
        return None
    _curve, quote_reserve, token_reserve, fee_bps, tax_bps, _sellable, _grad = state
    # A clip of 0.4% of the curve's quote reserve: a $50 clip on a ~$14k curve.
    measured = pons_round_trip(max(1, quote_reserve // 250), quote_reserve, token_reserve,
                               fee_bps, tax_bps)
    return None if measured is None else measured[0]
