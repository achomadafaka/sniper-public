"""Exit-only Uniswap V2 quotes for fee-on-transfer chain-4663 bags.

The normal direct venue remains Uniswap V3 for entries and token-safety admission. This module is a
strict fallback for an existing position whose token has no token/WETH V3 pool but does have a
verified V2 path through a configured immutable connector. It builds Router02's
``swapExactTokensForTokensSupportingFeeOnTransferTokens`` transaction and returns the same quote
shape consumed by the existing EVM exit executor.

The quote input is reduced by the existing maximum-tax policy before ``getAmountsOut``. The actual
transaction still sells the exact full raw balance; Router02 measures what the pair really receives.
This makes ``buyAmount`` and ``amountOutMin`` conservative without inventing a token-specific tax.
"""
from __future__ import annotations

from sniper.execution.evm_rpc_pool import RequestNotSent
from sniper.util.clock import real_utc_now

class V2ReadUnavailable(Exception):
    """We could not reach the chain. NOT the same as the chain saying there is no pair."""


class V2Reverted(Exception):
    """The chain answered: this path does not exist. A genuine no-route."""


def _classify_rpc_error(payload):
    """`revert`, `unavailable`, or None when the payload carries a result.

    `getAmountsOut` REVERTS when the pair is missing, so an error field alone cannot mean
    blindness — treating every error as unreadable would make a coin with no pool retry forever.
    """
    if not isinstance(payload, dict):
        return "unavailable"
    err = payload.get("error")
    if err is None:
        return None if "result" in payload else "unavailable"
    message = str((err or {}).get("message") if isinstance(err, dict) else err).lower()
    if "revert" in message or "execution reverted" in message:
        return "revert"
    return "unavailable"


GET_AMOUNTS_OUT = "d06ca61f"
SWAP_EXACT_TOKENS_FOT = "5c11d795"
_BPS = 10_000


def _w_uint(value):
    return format(int(value), "064x")


def _w_addr(address):
    return str(address).lower().replace("0x", "").rjust(64, "0")


def _make_call_fn(chain, http, rpc=None):
    """A pooled read on `rpc` (default: funded RPC), preserving typed chain outcomes."""
    from sniper.execution.evm_wallet import _rpc
    from sniper.settings import memecoin as cfg

    rpc = rpc or cfg.evm_rpc_for_chain(chain)
    from sniper.execution.evm_dex_quote import _make_call_fn as v3_call, V3ReadUnavailable
    transport = v3_call(chain, http, rpc=rpc)

    def call_fn(to, data):
        if not rpc:
            return None
        try:
            payload = _rpc(rpc, "eth_call", [{"to": to, "data": data}, "latest"], http=http)
        except RequestNotSent:
            raise                       # caller budget/deadline: stop, without a provider penalty
        except Exception as exc:
            status = getattr(exc, "http_status", None)
            detail = f"HTTP {status}: {exc}" if status is not None else str(exc)
            raise V2ReadUnavailable(detail[:120]) from exc
        kind = _classify_rpc_error(payload)
        if kind == "revert":
            raise V2Reverted(str((payload.get("error") or {}))[:120])
        if kind == "unavailable":
            detail = (payload.get("error") or payload) if isinstance(payload, dict) else payload
            raise V2ReadUnavailable(str(detail)[:120])
        return payload.get("result")

    def batch(calls):
        try:
            replies = transport.batch_responses(calls)
        except V3ReadUnavailable as exc:
            raise V2ReadUnavailable(str(exc)) from exc
        if replies is None:
            return None
        results = []
        for reply in replies:
            kind = _classify_rpc_error(reply)
            if kind == "revert":
                results.append(V2Reverted(str(reply.get("error"))[:120]))
            elif kind == "unavailable":
                results.append(V2ReadUnavailable(str(reply.get("error") or reply)[:120]))
            else:
                results.append(reply.get("result"))
        return results

    call_fn.batch = batch
    return call_fn


def _encode_get_amounts_out(amount_in, path):
    return ("0x" + GET_AMOUNTS_OUT + _w_uint(amount_in) + _w_uint(64)
            + _w_uint(len(path)) + "".join(_w_addr(token) for token in path))


def _parse_amounts_out(raw, expected_count):
    if not raw or raw == "0x":
        return None
    try:
        body = raw[2:]
        offset_words = int(body[:64], 16) // 32
        count = int(body[offset_words * 64:(offset_words + 1) * 64], 16)
        if count != expected_count:
            return None
        amounts = [int(body[(offset_words + 1 + i) * 64:
                            (offset_words + 2 + i) * 64], 16) for i in range(count)]
    except (TypeError, ValueError):
        return None
    return amounts if amounts and all(amount >= 0 for amount in amounts) else None


def _quote_path(call_fn, router, amount_in, path):
    # The typed read errors PROPAGATE: the caller decides whether a failure to reach the chain was
    # blindness. A bare `except Exception: return None` here is what made an unreadable path
    # indistinguishable from an empty one.
    try:
        raw = call_fn(router, _encode_get_amounts_out(amount_in, path))
    except (RequestNotSent, V2ReadUnavailable, V2Reverted):
        raise
    except Exception as exc:
        raise V2ReadUnavailable(str(exc)[:120]) from exc
    return _path_amount_out(raw, amount_in, path)


def _path_amount_out(raw, amount_in, path):
    if isinstance(raw, Exception):
        raise raw
    amounts = _parse_amounts_out(raw, len(path))
    if not amounts or amounts[0] != amount_in or amounts[-1] <= 0:
        return None
    return amounts[-1]


def _encode_swap(amount_in, amount_out_min, path, recipient, deadline):
    # Static args: amountIn, amountOutMin, path offset, recipient, deadline. Dynamic path follows.
    return ("0x" + SWAP_EXACT_TOKENS_FOT
            + _w_uint(amount_in) + _w_uint(amount_out_min) + _w_uint(5 * 32)
            + _w_addr(recipient) + _w_uint(deadline)
            + _w_uint(len(path)) + "".join(_w_addr(token) for token in path))


_PAIR_CACHE = {}
_PAIR_CACHE_LOCK = __import__("threading").Lock()
_PAIR_TTL_S, _PAIR_MISS_TTL_S = 600.0, 120.0
# THE SOURCE'S OWN PAIR (2026-10-15, operator "fill fastest"; evm_source_route.seed_pairs). The
# counter-token of the pool the source bought through, read from its receipt by the watcher. An
# ENTRY's path search tries it before DexScreener and asks DexScreener once only if it routes
# nothing. Apart from _PAIR_CACHE: a seed is never served to another caller as a provider answer.
_PAIR_SEED = {}
_PAIR_SEED_TTL_S = 600.0


def seed_pair_tokens(chain, mint, tokens, *, clock=None):
    """Remember the source receipt's counter-token(s) for `mint` on `chain` (lowercase addresses)."""
    import time as _time
    try:
        clean = tuple(dict.fromkeys(str(t).lower() for t in tokens
                                    if str(t).lower().startswith("0x") and len(str(t)) == 42))
    except TypeError:
        return
    now = (clock or _time.monotonic)()
    with _PAIR_CACHE_LOCK:
        _PAIR_SEED[(str(chain), str(mint).lower())] = (now + _PAIR_SEED_TTL_S, clean)


def seeded_pair_tokens(chain, mint, *, clock=None):
    """The live seed for `mint` on `chain`, or None (never seeded or expired)."""
    import time as _time
    now = (clock or _time.monotonic)()
    with _PAIR_CACHE_LOCK:
        hit = _PAIR_SEED.get((str(chain), str(mint).lower()))
    return hit[1] if hit and hit[0] > now else None


def pair_quote_tokens(chain, mint, *, fetch=None, clock=None):
    """Tokens this coin is actually paired against on `chain`, deepest pool first (max 2).

    WHY (2026-09-11): flap.sh launches pair a coin with any token -- a tokenized stock (SKHYB,
    QQQB, BIDUon), ZEC, BNC4 -- not with WBNB. The V2 path search only knew WBNB and USDT, so
    0x7e15…7777 refused `no_route` against a $49,647 PancakeSwap V2 pool (43 BSC no_route aborts
    in 14 days, 9 coins, all with a Pancake pool). Read from the token's own pair list, cached;
    any failure returns () and the path search is exactly what it was.
    """
    import time as _time
    key = (str(chain), str(mint).lower())
    now = (clock or _time.monotonic)()
    with _PAIR_CACHE_LOCK:
        hit = _PAIR_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    tokens = ()
    try:
        if fetch is None:
            from sniper.execution.solana_quote import (
                DEXSCREENER, FOMO_TO_DEX, _http, _DS_TIMEOUT_S, dexscreener_cooldown_active,
                ds_tokens_url)
            if dexscreener_cooldown_active(funded=True):
                return ()
            slug = FOMO_TO_DEX.get(str(chain))
            _s, j, _d = _http("GET", ds_tokens_url(mint), None, None, timeout=_DS_TIMEOUT_S)
            pairs = [p for p in (j or {}).get("pairs") or [] if not slug or p.get("chainId") == slug]
        else:
            pairs = fetch(chain, mint) or []
        ranked = []
        for p in pairs:
            base = str(((p.get("baseToken") or {}).get("address")) or "").lower()
            quote = str(((p.get("quoteToken") or {}).get("address")) or "").lower()
            # THE COIN CAN SIT ON EITHER SIDE (2026-09-22, 龙虾 0xf72c2c31 d3d7ed87). DexScreener
            # listed its only pair as oANTHROPIC / 龙虾, coin on the QUOTE side, and the old
            # `base != mint` filter dropped it, so the search never tried the pair token at all.
            # Every pair in this list involves the coin; the counter-token is whichever side is
            # not the coin.
            other = quote if base == key[1] else base if quote == key[1] else ""
            if not other.startswith("0x") or len(other) != 42:
                continue
            ranked.append((float(((p.get("liquidity") or {}).get("usd")) or 0.0), other))
        seen, out = set(), []
        for _liq, quote in sorted(ranked, reverse=True):
            if quote not in seen:
                seen.add(quote)
                out.append(quote)
        tokens = tuple(out[:2])
    except Exception:
        tokens = ()
    with _PAIR_CACHE_LOCK:
        _PAIR_CACHE[key] = (now + (_PAIR_TTL_S if tokens else _PAIR_MISS_TTL_S), tokens)
    return tokens


def _route_gas():
    """MEMECOIN_EVM_DEFAULT_ROUTE_GAS: this builder's transaction carries its own limit. A knob at 0
    or below would sign gas 0 ("0" is truthy to the signer): the default applies instead."""
    from sniper.settings import memecoin as _cfg
    return int(_cfg.MEMECOIN_EVM_DEFAULT_ROUTE_GAS) if int(_cfg.MEMECOIN_EVM_DEFAULT_ROUTE_GAS) > 0 \
        else 900_000


def dex_v2_exit_quote(chain, sell_token, buy_token, sell_amount_atomic, taker, *,
                      http=None, slippage_bps=None, cfg_router=None,
                      connector_tokens=None, tax_buffer_bps=None,
                      call_fn=None, now_fn=None, blind_out=None, pair_tokens_fn=None):
    """Return a conservative executable V2 fee-on-transfer exit quote, or ``None``."""
    cfg = None
    if cfg_router is None or connector_tokens is None or slippage_bps is None \
            or tax_buffer_bps is None or call_fn is None:
        from sniper.settings import memecoin as cfg
    configured = cfg.memecoin_evm_v2_exit_config(chain) if cfg is not None else None
    router = cfg_router or ((configured or {}).get("router"))
    connectors = (tuple(connector_tokens) if connector_tokens is not None
                  else tuple((configured or {}).get("connectors") or ()))
    if not router:
        return None
    try:
        sell_amount_atomic = int(sell_amount_atomic)
        slippage_bps = (int(slippage_bps) if slippage_bps is not None
                        else int(round(cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER[-1] * 100)))
        tax_buffer_bps = (int(tax_buffer_bps) if tax_buffer_bps is not None
                          else int(round(cfg.MEMECOIN_EVM_MAX_TAX_PCT * 100)))
    except (TypeError, ValueError, IndexError):
        return None
    if (sell_amount_atomic <= 0 or slippage_bps < 0 or slippage_bps >= _BPS
            or tax_buffer_bps < 0 or tax_buffer_bps >= _BPS):
        return None
    conservative_in = sell_amount_atomic * (_BPS - tax_buffer_bps) // _BPS
    if conservative_in <= 0:
        return None

    sell = str(sell_token).lower()
    buy = str(buy_token).lower()
    paths = [(sell, buy)]
    seen = {paths[0]}
    for connector in connectors:
        connector = str(connector).lower()
        path = (sell, connector, buy)
        if connector in (sell, buy) or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    # PATHS THROUGH THE COIN'S OWN PAIR TOKEN (2026-09-11, BSC flap.sh launches). The coin is the
    # side that is not a connector: the sell side on an exit, the buy side on an entry.
    lookup = pair_tokens_fn
    if lookup is None and cfg is not None and str(chain) in getattr(
            cfg, "MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS", ()):
        lookup = pair_quote_tokens
    known = {str(c).lower() for c in connectors}
    coin = sell if buy in known or sell not in known else buy
    # A seed answers only an ENTRY (the coin is bought) that would otherwise ask DexScreener.
    seeded = (seeded_pair_tokens(chain, coin)
              if lookup is pair_quote_tokens and coin == buy else None)

    def _add_pair_paths(tokens):
        added = []
        for x in tokens:
            if x in known or x in (sell, buy):
                continue
            candidates = [(sell, x, buy)]
            for c in known:
                if c in (sell, buy):
                    continue
                candidates.append((sell, x, c, buy) if coin == sell else (sell, c, x, buy))
            for path in candidates:
                if path not in seen:
                    seen.add(path)
                    paths.append(path)
                    added.append(path)
        return added

    def _lookup_tokens():
        try:
            return [str(x).lower() for x in (lookup(chain, coin) or ())]
        except Exception:
            return []
    if seeded is not None:
        _add_pair_paths(seeded)
    elif lookup is not None:
        _add_pair_paths(_lookup_tokens())

    call_fn = call_fn or _make_call_fn(chain, http)
    def _search(paths, best_path=None, best_out=0, saw_answer=False, unreadable=None):
        # A FAILED READ IS NOT AN EMPTY BOOK (2026-09-08). Every path used to collapse to None, so a
        # transport failure and a missing pair were the same answer and the ladder wrote a hard
        # `no_route` the re-quote lane treats as terminal. 0x7997a69c/56 was refused that way against a
        # $30,852 pool that had existed for twenty hours. Blindness is reported only when NO path got a
        # real answer: one revert proves we could see.
        values = None
        batch = getattr(call_fn, "batch", None)
        if len(paths) > 1 and callable(batch):
            try:
                values = batch([(router, _encode_get_amounts_out(conservative_in, path)) for path in paths])
                if values is not None and (not isinstance(values, (list, tuple)) or len(values) != len(paths)):
                    raise V2ReadUnavailable("partial batch replies")
            except RequestNotSent:
                raise
            except Exception as exc:
                values = [V2ReadUnavailable(str(exc)[:120])] * len(paths)
        for index, path in enumerate(paths):
            try:
                amount_out = (_quote_path(call_fn, router, conservative_in, path) if values is None
                              else _path_amount_out(values[index], conservative_in, path))
                saw_answer = True
            except V2Reverted:
                saw_answer = True          # the chain answered: this path does not exist
                continue
            except V2ReadUnavailable as exc:
                unreadable = unreadable or str(exc)
                continue
            if amount_out is not None and amount_out > best_out:
                best_path = path
                best_out = amount_out
        return best_path, best_out, saw_answer, unreadable

    best_path, best_out, saw_answer, unreadable = _search(paths)
    if (best_path is None or best_out <= 0) and seeded is not None and lookup is not None:
        # THE SEED ROUTED NOTHING (it quoted 0, reverted, or could not be read): ask DexScreener
        # exactly once, as before the seed, and search only the paths that adds.
        fallback = _add_pair_paths(_lookup_tokens())
        if fallback:
            best_path, best_out, saw_answer, unreadable = _search(
                fallback, best_path, best_out, saw_answer, unreadable)
    if best_path is None or best_out <= 0:
        if blind_out is not None and not saw_answer and unreadable is not None:
            # A 429 IS THE RPC'S QUOTA, NOT A TRANSPORT FAULT (2026-09-14, PERPS, PLCANARY). All 16
            # "v2 could not read: rpc_transport" aborts on 4663 that day were `Too Many Requests`
            # from our own retry bursts; as rpc_transport they re-quoted at once, into the limit.
            limited = "429" in unreadable or any(word in unreadable.lower() for word in (
                "too many requests", "rate limit", "rpc_rate_limit"))
            blind_out["reason"] = "rpc_rate_limit" if limited else "rpc_transport"
            blind_out["detail"] = unreadable
        return None

    amount_out_min = best_out * (_BPS - slippage_bps) // _BPS
    if amount_out_min <= 0:
        return None
    now_fn = now_fn or real_utc_now
    try:
        from sniper.settings.memecoin import memecoin_evm_swap_deadline_s
        deadline = int(now_fn()) + memecoin_evm_swap_deadline_s(chain, 300)
        data = _encode_swap(sell_amount_atomic, amount_out_min, best_path, taker, deadline)
    except (TypeError, ValueError):
        return None
    return {
        "sellAmount": str(sell_amount_atomic),
        "buyAmount": str(best_out),
        "minBuyAmount": str(amount_out_min),
        "liquidityAvailable": True,
        "transaction": {"to": router, "data": data, "value": "0x0", "gas": str(_route_gas())},
        "issues": {"allowance": {"spender": router}},
        "route": {"protocol": "uniswap_v2", "router": str(router).lower(),
                  "tokens": [str(token).lower() for token in best_path],
                  "path": list(best_path)},
        # What exit_value_amount needs to re-price this route on the whole bag. Plain data only.
        "_fot_exit": {"chain": str(chain), "router": str(router).lower(),
                      "path": list(best_path), "cut_bps": tax_buffer_bps},
    }


def with_lower_min_out(quote, new_min):
    """This builder's quote with its signed amountOutMin lowered to `new_min`, or None. Only the
    one calldata word changes (amountIn, path, recipient and deadline are the bytes already built);
    no read, no re-quote. A quote this module did not build, or a minimum that is not lower and
    positive, returns None."""
    try:
        if not isinstance(quote.get("_fot_exit"), dict):
            return None
        data = str(quote["transaction"]["data"])
        old = int(quote["minBuyAmount"])
        new_min = int(new_min)
        if (not data.startswith("0x" + SWAP_EXACT_TOKENS_FOT) or not 0 < new_min < old
                or int(data[10 + 64:10 + 128], 16) != old):
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    patched = data[:10 + 64] + _w_uint(new_min) + data[10 + 128:]
    return {**quote, "minBuyAmount": str(new_min),
            "transaction": {**quote["transaction"], "data": patched}}


def exit_value_amount(quote, *, http=None, value_tax_bps=None, call_fn=None):
    """What the pair pays for the WHOLE bag less a realistic transfer tax: the exit's VALUE.

    `buyAmount` above prices the bag cut by the max-tax policy (15%), which is right for the signed
    minimum and wrong as a valuation: every V2 exit decision read ~15% low (2026-09-27, 65763: cut
    0.0742 WBNB, paid 0.0856). One getAmountsOut on the chosen path, only for a quote this module
    built. None means "keep the cut": not a V2 exit quote, the knob at or above the cut (rollback),
    or the read failed. The caller never signs on this number.
    """
    fot = (quote or {}).get("_fot_exit")
    if not isinstance(fot, dict):
        return None
    try:
        if value_tax_bps is None:
            from sniper.settings import memecoin as cfg
            value_tax_bps = int(round(float(cfg.MEMECOIN_EVM_EXIT_VALUE_TAX_PCT) * 100))
        value_tax_bps, cut_bps = int(value_tax_bps), int(fot["cut_bps"])
        value_in = int(quote["sellAmount"]) * (_BPS - value_tax_bps) // _BPS
        router, path = fot["router"], tuple(fot["path"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= value_tax_bps < cut_bps or value_in <= 0:
        return None
    try:
        return _quote_path(call_fn or _make_call_fn(fot["chain"], http), router, value_in, path)
    except (RequestNotSent, V2ReadUnavailable, V2Reverted):
        return None
