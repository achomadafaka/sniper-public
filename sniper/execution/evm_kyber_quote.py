"""One validated KyberSwap aggregator quote on Robinhood 4663, or None.

WHY THIS RUNG EXISTS. Twelve 4663 signals since 2026-09-15 refused `no_route_all_venues` (BILL 11,
DAM 1) on mints that HAVE a pool against a priced unit but no liquidity in any venue we reach.
BILL's own buyer went Relay -> KyberSwap, so the liquidity was always there; we simply could not
see it. Relay reaches Kyber today with an aggregator's spread on top; this removes the
intermediary. Verified 2026-09-22, both directions, $30-clip:

    BILL      buy 3 hops  sell 2 hops   round trip -4.8%   gas $0.13/$0.06
    GIGABYTE  buy 3 hops  sell 3 hops   round trip -3.3%   gas $0.14/$0.16
    DAM       buy 3 hops  sell 2 hops   round trip -2.5%   gas $0.08/$0.07

THE CALLDATA IS A THIRD PARTY'S AND IS TREATED AS ONE. Every field that decides where the money
goes is re-derived from OUR request and compared: the router and its code hash, the executor and
its code hash, the selector, both tokens, the input amount, the recipient and the nested minimum.
The descriptor layout and these invariants are not new -- `holding_routes._swap` has validated this
exact router and selector for manual sales since 2026-09-17, and this reuses that decoder rather
than writing a second one that could drift from it.

DEFAULT OFF, AND LAST IN LINE. `MEMECOIN_EVM_KYBER_AGG_ENABLED` defaults to 0. The rung is meant to
run only after every native venue has missed, so a coin our own pools can quote never pays an HTTP
round trip on the entry path.

A QUOTE IS NOT A FILL. Three coins at one point in time is the whole of the evidence, and an
aggregator route can vanish between the quote and the block.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

BASE = "https://aggregator-api.kyberswap.com/robinhood/api/v1"
CHAIN = "4663"
# One aggregator, one path per chain. A chain outside this map has no pin and gets no route
# (2026-09-23: BSC opened after 64 no_route attempts in 7 days on venues only Kyber reaches.
# 2026-09-25: Base opened after PEA's only pool was a Hydrex CLAMM against POD -- no native rung
# reaches it, and Kyber routes it.)
SLUGS = {"4663": "robinhood", "56": "bsc", "8453": "base"}


def _base(chain):
    slug = SLUGS.get(str(chain))
    return None if slug is None else "https://aggregator-api.kyberswap.com/%s/api/v1" % slug
# The aggregator answers a default python-urllib agent with HTTP 403.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")
_ZERO = "0x" + "0" * 40
# The router's own fee/permit flag word. `holding_routes._swap` pins it at 512 for every route it
# has validated; anything else is a shape we have not proven and is refused rather than guessed.
_EXPECTED_FLAGS = 512
# Rounding only. Same value and same reason as `holding_routes._swap`, which has validated this
# router since 2026-09-17.
_MIN_OUT_WEI_TOLERANCE = 2


def _http_json(url, body=None, timeout=12, label="kyber"):
    from sniper.execution.evm_wallet import http_tap
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    # Measurement only: the ladder's provider HTTP tap (host + label + RTT + status, no URL).
    with http_tap(label, url) as tap:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                tap["outcome"] = "http_%s" % getattr(resp, "status", "?")
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            tap["outcome"] = "http_%s" % exc.code
            # KEEP THE AGGREGATOR'S OWN WORDS (2026-09-29, pos 66990 on 56). A 400 carries
            # {"code":4011,"message":"token not found"}; without it ten exit ladders said only
            # rpc_transport. Bounded, and never the URL.
            try:
                answer = json.loads(exc.read(500) or b"{}")
                exc.kyber_error = "HTTP %s %s %s" % (exc.code, answer.get("code"),
                                                     str(answer.get("message") or "")[:80])
                exc.kyber_code = answer.get("code")
            except Exception:
                exc.kyber_error = "HTTP %s" % exc.code
            raise


def _pin(chain=CHAIN):
    """The chain's own pin, or None: the router address repeats across chains, the bytecode does not."""
    from sniper.execution.holding_routes import PINS
    key = "kyberswap" if str(chain) == CHAIN else "kyberswap_%s" % chain
    return PINS.get(key)


def _code_hash(address, rpc_url, http=None):
    from eth_utils import keccak
    from sniper.execution.evm_v4_quote import _rpc
    out = _rpc(rpc_url, "eth_getCode", [address, "latest"], http=http)
    code = (out or {}).get("result") or "0x"
    if len(code) <= 2:
        return None
    return "0x" + keccak(bytes.fromhex(code[2:])).hex()


def verify_build(built, *, sell_token, buy_token, amount_atomic, taker, min_out, rpc_url=None,
                 http=None, verify_code=True, chain=CHAIN):
    """Return the router transaction dict when EVERY pinned invariant holds, else None.

    Fail-closed: any decode failure, any mismatch, any missing field refuses. The caller gets None
    and the ladder simply records a miss -- an aggregator we cannot verify is not a venue.
    """
    from sniper.execution import lifi_route as r
    from sniper.execution.holding_routes import KYBER_TYPES
    pin = _pin(chain)
    if pin is None:
        return _miss("chain %s has no kyber pin" % chain)
    try:
        data = str((built or {}).get("data") or "")
        router = str((built or {}).get("routerAddress") or "").lower()
        if router != pin["target"]:
            return _miss("router %s is not the pinned target" % router[:12])
        if data[:10].lower() != pin["selector"]:
            return _miss("selector %s is not the pinned selector" % data[:10])
        if verify_code and rpc_url:
            for label, addr, want in (("router", pin["target"], pin["code_hash"]),
                                      ("executor", pin["executor"], pin["executor_code_hash"])):
                # A HASH WE COULD NOT READ IS NOT A HASH THAT MOVED (E5 recheck, 2026-09-23).
                # These reads sat inside the broad `except Exception` below, which exists for
                # calldata decoding, so a timeout or a 429 on `eth_getCode` returned "calldata did
                # not decode" -> None and the ladder published `no_route`: a verdict about the coin
                # from a read we never made. Same class as the HTTP legs above, left behind here.
                try:
                    got = _code_hash(addr, rpc_url, http=http)
                except Exception as exc:
                    raise KyberQuoteUnavailable(
                        _failure_type(exc), "%s code hash unread: %r" % (label, exc)) from exc
                if got is None:
                    # `0x` FROM A NODE IS SILENCE, NOT AN ANSWER. `_code_hash` returns None for
                    # empty code, and comparing that against the pin refused with "code hash
                    # moved: None" -- a false claim about a pinned invariant, indistinguishable in
                    # the log from a real compromise. Our own custody node produces exactly this:
                    # it keeps state for minutes, then answers `0x` for an older block with no
                    # error.
                    raise KyberQuoteUnavailable(
                        "rpc_transport", "%s code read empty" % label)
                if got != want:
                    return _miss("%s code hash moved: %s" % (label, got))

        raw = bytes.fromhex(data[10:])
        target, approve, target_data, desc, _client = r.decode_exact(KYBER_TYPES, raw)[0]
        (src, dst, receivers, amounts, fees, fee_amounts, receiver, qty, nested_min,
         flags, permit) = desc
        if target != pin["executor"] or approve != _ZERO:
            return _miss("executor %s is not the pinned executor" % str(target)[:12])
        if not target_data or receivers != (target,) or amounts != (int(amount_atomic),):
            return _miss("input recipients or amounts do not match our request")
        if fees or fee_amounts or permit or flags != _EXPECTED_FLAGS:
            return _miss("unsupported fees, permit or flags=%s" % (flags,))
        if str(src).lower() != str(sell_token).lower() or str(dst).lower() != str(buy_token).lower():
            return _miss("nested tokens are not the pair we asked for")
        if str(receiver).lower() != str(taker).lower():
            return _miss("nested recipient %s is not the taker" % str(receiver)[:12])
        if int(qty) != int(amount_atomic):
            return _miss("nested input amount does not match our request")
        # A FEW WEI OF ROUNDING, AND NOT ONE UNIT MORE. The aggregator recomputes the minimum from
        # its own output with its own rounding, so an exact comparison refuses every live route:
        # measured 2026-09-22, a genuine GIGABYTE build came back exactly ONE wei under our floor
        # and all three coins refused. `holding_routes._swap` carries the same band for the same
        # reason on this same router, and the 2026-09-11 LI.FI finding is the same shape -- a
        # one-sided band read every quote as malformed. The band is absolute and tiny: against a
        # 6.6e22 minimum it is a relative 3e-20, so it cannot hide a weakened floor.
        if int(nested_min) < max(1, int(min_out) - _MIN_OUT_WEI_TOLERANCE):
            return _miss("nested minimum %s is under our minimum %s" % (nested_min, min_out))
    except KyberQuoteUnavailable:
        # A TYPED PROVIDER FAILURE MUST NOT BE RE-SWALLOWED BY THE DECODE HANDLER, which is the
        # very swallow this boundary was moved out of.
        raise
    except Exception as exc:
        return _miss("calldata did not decode: %r" % (exc,))
    # A GAS LIMIT OF OUR OWN (2026-09-23, RATBRAIN 64081). This transaction carried no `gas`, so the
    # signer fell back to the 500,000 default and 15 sells ran out of gas at ~489,000 used; the
    # same calldata simulated at 611,000. Kyber's build reports its estimate; take 1.5x it with a
    # floor, the same shape relay uses (900,000). Unused gas is refunded, an underestimate reverts.
    try:
        estimate = int(str((built or {}).get("gas") or 0), 0)
    except (TypeError, ValueError):
        estimate = 0
    from sniper.settings import memecoin as _cfg
    # 2.0x since 2026-09-30 (MEMECOIN_EVM_KYBER_BUY_GAS_MULT); floor and cap unchanged.
    gas = min(3_000_000, max(900_000, int(estimate * float(_cfg.MEMECOIN_EVM_KYBER_BUY_GAS_MULT))))
    return {"to": pin["target"], "data": data, "value": "0x0", "gas": str(gas)}


def _miss(reason):
    logger.info("kyber rung refused: %s", reason)
    return None


class KyberQuoteUnavailable(Exception):
    """The aggregator could not be READ. Never "this coin has no Kyber route".

    Same contract as `PonsReadUnavailable` and `RelayQuoteError`: the ladder's `_run` reads
    `failure_type` off a raised exception to set `blind_reason`, and `classify_quote_miss` then
    publishes `provider_<failure_type>` instead of `no_route`. Every value raised here is already
    a key of `QUOTE_MISS_FAILURES`, so none can fall through its MALFORMED_RESPONSE default --
    which is exactly how a renamed label turns a transport fault into a claim about the token.
    """

    def __init__(self, failure_type, detail=""):
        super().__init__(f"{failure_type}: {detail}" if detail else failure_type)
        self.failure_type = failure_type


def _failure_type(exc):
    """Which kind of not-an-answer this is. Four different facts, none about the token.

    HTTPError is tested before URLError because it subclasses it, and `socket.timeout` has been
    an alias of `TimeoutError` since 3.10, so a bare read timeout lands on `timeout` whether
    urllib re-raises it or wraps it in a URLError.
    """
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):
        # 429 is the provider refusing US; every other status is the provider or the path
        # between us failing. A 403 here is the User-Agent trap `_UA` exists for.
        return "rate_limit" if exc.code == 429 else "rpc_transport"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        return "timeout" if isinstance(exc.reason, TimeoutError) else "rpc_transport"
    if isinstance(exc, (ValueError, AttributeError, TypeError)):
        # `json.JSONDecodeError` is a ValueError; a body that is not the shape we parse raises
        # AttributeError or TypeError off the `.get` chain. Either way the bytes arrived and
        # could not be read, which is not the same as the aggregator answering.
        return "malformed_response"
    return "rpc_transport"


def _route_not_found(exc):
    """True for Kyber's HTTP 400 answer with its own code 4008, "route not found"."""
    import urllib.error
    try:
        return (isinstance(exc, urllib.error.HTTPError) and exc.code == 400
                and int(getattr(exc, "kyber_code", None)) == 4008)
    except (TypeError, ValueError):
        return False


def _routes(sell_token, buy_token, amount_atomic, timeout=12, chain=CHAIN):
    query = urllib.parse.urlencode({"tokenIn": str(sell_token), "tokenOut": str(buy_token),
                                    "amountIn": str(int(amount_atomic)), "gasInclude": "true"})
    out = _http_json("%s/routes?%s" % (_base(chain), query), timeout=timeout,
                     label="kyber:routes")
    return ((out or {}).get("data") or {}).get("routeSummary") or None


def dex_kyber_quote(chain, sell_token, buy_token, amount_atomic, taker, *, http=None,
                    slippage_bps=None, rpc_url=None, verify_code=True, timeout=12):
    """A validated aggregator swap, shaped like every other rung's quote, or None."""
    from sniper.settings import memecoin as cfg
    if not bool(getattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", False)):
        return None
    chains = tuple(getattr(cfg, "MEMECOIN_EVM_KYBER_AGG_CHAINS", (CHAIN,)) or ())
    if str(chain) not in {str(c) for c in chains}:
        return None
    if _base(chain) is None or _pin(chain) is None:
        return None                       # allowlisted but unpinned: no route, never a guess
    try:
        amount = int(amount_atomic)
        if slippage_bps is None:
            slippage_bps = int(round(cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT * 100))
        slippage_bps = int(slippage_bps)
    except (TypeError, ValueError):
        return None
    if amount <= 0 or not 0 <= slippage_bps < 10_000:
        return None
    try:
        summary = _routes(sell_token, buy_token, amount, timeout=timeout, chain=chain)
        if not summary:
            return None
        out = int(summary.get("amountOut") or 0)
        if out <= 0:
            return None
        min_out = out * (10_000 - slippage_bps) // 10_000
        if min_out <= 0:
            return None
        built = (_http_json("%s/route/build" % _base(chain),
                            {"routeSummary": summary, "sender": str(taker),
                             "recipient": str(taker), "slippageTolerance": slippage_bps,
                             "source": "sniper"}, timeout=timeout,
                            label="kyber:build").get("data") or {})
    except Exception as exc:
        if _route_not_found(exc):
            # AN ANSWER, NOT BLINDNESS (2026-10-02, DAODUN 68544). HTTP 400 with Kyber's own code
            # 4008 "route not found" is the aggregator saying it has no route for this pair, the
            # same fact as a 200 with no routeSummary; it was published provider_rpc_transport on
            # every rung of a stuck bag's exit ladder. Every other code (4011 "token not found"
            # included: an indexing lag on a new token) stays a typed read failure.
            return _miss("route not found (Kyber 4008)")
        # BLINDNESS IS RAISED, NOT SWALLOWED (E5, 2026-09-23). This block returned a bare None and
        # logged the word "unavailable" while throwing the fact away, so the rung recorded
        # `blind_reason=None` and the ladder published `no_route` -- a verdict on the coin, for a
        # read we never made. The deterministic misses above still return None: a 200 with no
        # routeSummary is the aggregator ANSWERING that it found nothing.
        logger.info("kyber rung unavailable: %r", exc)
        raise KyberQuoteUnavailable(_failure_type(exc),
                                    getattr(exc, "kyber_error", None) or repr(exc)[:120]) from exc
    tx = verify_build(built, sell_token=sell_token, buy_token=buy_token, amount_atomic=amount,
                      taker=taker, min_out=min_out, rpc_url=rpc_url, http=http,
                      verify_code=verify_code, chain=chain)
    if not tx:
        return None
    return {
        "sellAmount": str(amount),
        "buyAmount": str(out),
        # THE EXIT INTENT BINDS ON THIS FIELD (2026-09-23, RATBRAIN 64081). `bind_quote` reads
        # `minBuyAmount`, and without it `quote_binding_complete` is False, the executor skips the
        # rung silently at every slippage tier and marks the bag stuck with no failure type. The
        # same minimum is already verified inside the calldata; here it is only surfaced.
        "minBuyAmount": str(min_out),
        "transaction": tx,
        # The APPROVAL IS EXACT and goes to the router, never to the executor: the executor is
        # reached by the router's own call and must never hold an allowance of ours.
        "issues": {"allowance": {"spender": _pin(chain)["target"], "amount": str(amount)}},
        "route": {"protocol": "kyberswap", "hops": len((summary.get("route") or [[]])[0] or ()),
                  "gas_usd": summary.get("gasUsd"), "min_out": str(min_out)},
    }


def dex_kyber_exit_quote(chain, sell_token, buy_token, amount_atomic, taker, **kwargs):
    """The sell direction, with gas room for Kyber's understated execution estimate."""
    quote = dex_kyber_quote(chain, sell_token, buy_token, amount_atomic, taker, **kwargs)
    if quote is None:
        return None
    # BLUB 65688 and INUTEL 65700: Kyber's sell estimate was too low. The existing
    # signer balance gate checks this ceiling; unused gas is refunded.
    tx = dict(quote["transaction"])
    from sniper.settings import memecoin as _cfg
    sell_gas = int(_cfg.MEMECOIN_EVM_KYBER_SELL_GAS)          # 3,000,000 until 2026-09-30 (INUTEL)
    tx["gas"] = str(sell_gas if sell_gas > 0 else 5_000_000)  # 0 would sign gas 0
    return {**quote, "transaction": tx}
