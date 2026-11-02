"""Direct Uniswap-V3 quote and swap-tx builder for Robinhood chain 4663.

This module talks to the chain's Uniswap V3 deployment directly:
  * QuoterV2 `quoteExactInputSingle` (eth_call, free, no key) for an EXACT amountOut, scanning the
    configured fee tiers and taking the deepest (best amountOut);
  * SwapRouter02 `exactInputSingle` calldata for the swap tx.

It returns the executor's venue-quote contract — `sellAmount` / `buyAmount` /
`transaction{to,data,value}` / `issues.allowance.spender` — so the existing EVM executor orchestration
(`execute_entry_evm` / `execute_exit_evm`: approve -> sign -> send -> confirm) is reused VERBATIM.

Fail-closed: any RPC error / no pool on any tier / zero-out / missing
configured address -> None (the executor then aborts and leaves the reservation; never a fabricated
fill). `amountOutMinimum` is `amountOut * (1 - slippage)`, using the EXISTING slippage settings — no
new slippage policy.

Addresses + selectors verified on-chain 2026-07-16 (docs/research/2026-07-16-robinhood-4663-dex-venue.md):
  QuoterV2.quoteExactInputSingle((address,address,uint256,uint24,uint160))  = 0xc6a5026a
  SwapRouter02.exactInputSingle((address,address,uint24,address,uint256,uint256,uint160)) = 0x04e45aaf
"""
from __future__ import annotations

import json as _json
import logging

from sniper.execution.evm_wallet import CHAIN_QUOTE, _endpoint_state_error
from sniper.execution.evm_rpc_pool import RequestNotSent

logger = logging.getLogger(__name__)

QUOTE_EXACT_INPUT_SINGLE = "c6a5026a"    # QuoterV2.quoteExactInputSingle
EXACT_INPUT_SINGLE = "04e45aaf"          # SwapRouter02.exactInputSingle
QUOTE_EXACT_INPUT = "cdca1753"           # QuoterV2.quoteExactInput(bytes,uint256)
EXACT_INPUT = "b858183f"                 # SwapRouter02.exactInput((bytes,address,uint256,uint256))
# Ramses V3 keys pools by int24 tickSpacing and its router requires a deadline.
RAMSES_QUOTE_EXACT_INPUT_SINGLE = "9e7defe6"
RAMSES_EXACT_INPUT_SINGLE = "a026383e"
_RAMSES_TICK_SPACINGS = (1, 5, 10, 50, 100, 200)
_RAMSES_DEADLINE_S = 120
# PANCAKESWAP V3 (chain 56) ships the ORIGINAL SwapRouter, not SwapRouter02, so both entry points
# carry a `deadline` INSIDE the params struct. Its QuoterV2 is selector-identical to Uniswap's,
# which is what hid this: chain 56 quoted correctly and every swap then reverted with EMPTY data at
# ~25.6k gas — a router refusing an unknown selector, which reads like a failed swap, not a
# malformed one. Verified on-chain 2026-09-08 against 0x1b81D678…, the configured 56 router:
#   0x04e45aaf exactInputSingle (no deadline) -> revert 0x     0x414bf389 (deadline) -> ACCEPTED
#   0xb858183f exactInput       (no deadline) -> revert 0x     0xc04b8d59 (deadline) -> ACCEPTED
PANCAKE_EXACT_INPUT_SINGLE = "414bf389"
PANCAKE_EXACT_INPUT = "c04b8d59"
_PANCAKE_DEADLINE_S = 120
_BPS = 10_000


def _w_uint(x):
    return format(int(x), "064x")


def _w_addr(a):
    return str(a).lower().replace("0x", "").rjust(64, "0")


class V3ReadUnavailable(Exception):
    """The v3 quoter could not be READ. Never "this coin has no v3 route".

    Carries `failure_type` because that is what the route ladder's `_run` reads off a raised
    exception to set `blind_reason`; the same contract `RelayQuoteError` uses two files over.
    """

    def __init__(self, failure_type, detail=""):
        super().__init__(f"{failure_type}: {detail}" if detail else failure_type)
        self.failure_type = failure_type


def _classify_rpc_error(payload):
    """`revert`, `unavailable`, or None when the payload carries a result.

    A REVERT IS A REAL ANSWER. QuoterV2 reverts on a fee tier with no pool, so typing every error
    as unreadable would make a coin that genuinely has no v3 pool retry forever. Same split Alpha
    drew in `evm_v2_exit_quote._classify_rpc_error` for `getAmountsOut`, and for the same reason.
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


def _batch_unsupported(status, response):
    error = response.get("error") if isinstance(response, dict) else None
    message = str(error.get("message") or "").lower() if isinstance(error, dict) else ""
    return (status in (200, 400) and isinstance(error, dict)
            and error.get("code") in (-32600, -32601) and "batch" in message
            and any(word in message for word in ("not support", "unsupported", "disabled")))


def _make_call_fn(chain, http, *, rpc=None):
    """Default eth_call seam: browser-UA JSON-RPC against the chain RPC.

    A HEX RESULT, OR None FOR A TIER THAT REVERTED. Until 2026-09-08 this also returned None for a
    transport failure, a 429 and a JSON-RPC error, and its docstring called that fail-closed. It is
    not: the ladder turns a None with no blindness into a TERMINAL `no_route`, so an RPC outage on
    the primary rung was published as "this coin has no v3 route" and never re-quoted. Fail-closed
    would be "unknown"; `no_route` is a claim.

    The read outcome is recorded on `call_fn.read_state` rather than raised here, because the caller
    scans several fee tiers and ONE tier answering — even by reverting — proves we could see. The
    quote decides at the end. State lives in this closure, which `dex_v3_quote` builds per call, so
    it is not shared across the ladder's thread pool.
    """
    from sniper.execution.evm_wallet import (
        _http as _default_http, _http_failure, _state_error_reason)
    from sniper.execution import evm_rpc_pool as _pool
    from sniper.settings import memecoin as cfg
    http = http or _default_http
    rpc = rpc or cfg.evm_rpc_for_chain(chain)
    expected_chain = int(_pool.chain_of(rpc) or chain)

    def post(body, *, batch_ids=None):
        def verify(candidate):
            status, response, _detail = http("POST", candidate, body=_json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}))
            if status != 200:
                raise _http_failure(status)
            chain_id = response.get("result") if isinstance(response, dict) else None
            if (not isinstance(chain_id, str) or not chain_id.startswith("0x")
                    or len(chain_id) <= 2 or response.get("error") is not None
                    or any(c not in "0123456789abcdefABCDEF" for c in chain_id[2:])):
                # An unavailable probe is unknown, not proof of a different chain.
                raise RuntimeError("EVM RPC identity probe returned no valid chain id")
            return int(chain_id, 16) == expected_chain

        candidates = _pool.pool_for(rpc)
        if batch_ids is not None:
            candidates = [url for url in candidates if not _pool.refuses(url, "eth_call_batch")]
        last = (None, None, None)
        for _attempt in range(max(2, len(candidates))):
            target = _pool.choose(rpc, verify_fn=verify, candidates=candidates)
            # Filtering capability can leave one secondary; choose's single-member shortcut
            # must not turn that secondary into a trusted configured primary.
            if target != rpc and _pool.identity_state(target) is not True:
                try:
                    valid = verify(target)
                except _pool.RequestNotSent:
                    raise
                except Exception:
                    _pool.penalize(target, "identity_probe_failed")
                    continue
                _pool.mark_identity(target, valid)
                if not valid:
                    continue
            try:
                status, response, detail = http("POST", target, body=body)
            except _pool.RequestNotSent:
                raise
            except Exception as exc:
                _pool.penalize(target, type(exc).__name__)
                continue
            if status in (402, 429) or (isinstance(status, int) and status >= 500):
                _pool.penalize(target, "provider_http_failure")
                last = (status, response, detail)
                continue
            busy = response if status == 200 and _endpoint_state_error(response) else None
            if (status == 200 and busy is None and batch_ids is not None and isinstance(response, list)
                    and response and all(_endpoint_state_error(item) for item in response)):
                ids = [item.get("id") for item in response]
                if (len(ids) == len(batch_ids) and all(type(i) is int for i in ids)
                        and set(ids) == set(batch_ids)):
                    busy = response[0]
            if busy is not None:
                _pool.penalize(target, _state_error_reason(busy))
                last = (status, response, detail)
                continue
            if batch_ids is not None and _batch_unsupported(status, response):
                _pool.note_refusal(target, "batch_unsupported", "eth_call_batch")
            return status, response, detail
        return last

    read_state = {"answered": False, "unavailable": None}

    def _note(kind, detail=""):
        if kind is None or kind == "revert":
            read_state["answered"] = True        # the node spoke; we are not blind
        elif read_state["unavailable"] is None:
            # Pons also imports the classifier's three-value contract; refine only V3 state.
            if (kind == "unavailable" and isinstance(detail, dict)
                    and _endpoint_state_error({"error": detail})
                    and any(word in str(detail.get("message") or "").lower()
                            for word in ("too many requests", "rate limit"))):
                kind = "rpc_rate_limit"
            read_state["unavailable"] = (kind, str(detail)[:120])

    def call_fn(to, data):
        if not rpc:
            _note("no_rpc_configured", "evm_rpc_for_chain returned nothing")
            return None
        body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                            "params": [{"to": to, "data": data}, "latest"]})
        status, j, _d = post(body)
        if j is None:
            _note("rpc_transport", f"no response (status={status})")
            return None
        kind = _classify_rpc_error(j)
        _note(kind, (j.get("error") if isinstance(j, dict) else j))
        if kind is not None:
            return None                          # reverted tier, or a read we could not make
        return j.get("result")

    call_fn.read_state = read_state

    def batch_responses(calls):
        """Matched read replies; only explicit unsupported batching permits scalar fallback."""
        if not rpc:
            raise V3ReadUnavailable("no_rpc_configured")
        if all(_pool.refuses(url, "eth_call_batch") for url in _pool.pool_for(rpc)):
            return None
        payload = [
            {"jsonrpc": "2.0", "id": i, "method": "eth_call",
             "params": [{"to": to, "data": data}, "latest"]}
            for i, (to, data) in enumerate(calls, 1)
        ]
        status, response, _d = post(_json.dumps(payload), batch_ids=range(1, len(calls) + 1))
        if not isinstance(response, list):
            error = response.get("error") if isinstance(response, dict) else None
            message = str(error.get("message") or "").lower() if isinstance(error, dict) else ""
            if _batch_unsupported(status, response):
                return None
            limited = status == 429 or any(word in message for word in ("too many requests", "rate limit"))
            raise V3ReadUnavailable("rpc_rate_limit" if limited else "rpc_transport",
                                    f"batch unavailable (status={status}): {message[:100]}")
        ids = [item.get("id") if isinstance(item, dict) else None for item in response]
        if (status != 200 or len(ids) != len(calls)
                or any(type(i) is not int for i in ids)
                or set(ids) != set(range(1, len(calls) + 1))):
            raise V3ReadUnavailable("rpc_transport", "unmatched batch replies")
        by_id = {item["id"]: item for item in response}
        return [by_id[i] for i in range(1, len(calls) + 1)]

    def batch_fn(calls):
        """One JSON-RPC request for a route scan; preserves per-call failures as None."""
        try:
            response = batch_responses(calls)
        except V3ReadUnavailable as exc:
            _note(exc.failure_type, str(exc))
            return [None] * len(calls)
        if response is None:
            return None
        out = []
        for item in response:
            _note(_classify_rpc_error(item if isinstance(item, dict) else None),
                  (item or {}).get("error") if isinstance(item, dict) else item)
            out.append(item.get("result") if "error" not in item else None)
        return out

    call_fn.batch = batch_fn
    call_fn.batch_responses = batch_responses

    return call_fn


def _single_quote_data(sell_token, buy_token, sell_amount_atomic, fee):
    return ("0x" + QUOTE_EXACT_INPUT_SINGLE
            + _w_addr(sell_token) + _w_addr(buy_token)
            + _w_uint(sell_amount_atomic) + _w_uint(fee) + _w_uint(0))


def _ramses_single_quote_data(sell_token, buy_token, sell_amount_atomic, tick_spacing):
    return ("0x" + RAMSES_QUOTE_EXACT_INPUT_SINGLE
            + _w_addr(sell_token) + _w_addr(buy_token)
            + _w_uint(sell_amount_atomic) + _w_uint(tick_spacing) + _w_uint(0))


def _path_quote_data(path, sell_amount_atomic):
    from eth_abi import encode
    return "0x" + QUOTE_EXACT_INPUT + encode(
        ["bytes", "uint256"], [path, int(sell_amount_atomic)],
    ).hex()


def _decode_amount_out(raw):
    if not raw or raw == "0x":
        return None
    try:
        out = int(raw[2:66], 16)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _call_many(call_fn, calls):
    batch_fn = getattr(call_fn, "batch", None)
    if callable(batch_fn):
        try:
            results = batch_fn(calls)
        except RequestNotSent:
            raise
        except Exception:
            logger.exception("v3 batch quoter call failed")
            return [None] * len(calls)
        if isinstance(results, (list, tuple)) and len(results) == len(calls):
            return list(results)
        # Some JSON-RPC providers reject batch payloads. Preserve the pre-batch behavior.
    results = []
    for to, data in calls:
        try:
            results.append(call_fn(to, data))
        except RequestNotSent:
            raise
        except Exception:
            logger.exception("v3 quoter call failed")
            results.append(None)
    return results


def _quote_amount_out(call_fn, quoter, sell_token, buy_token, sell_amount_atomic, fee):
    """One QuoterV2.quoteExactInputSingle eth_call for one fee tier. amountOut (int) or None.

    A tier with no pool reverts -> call_fn returns None -> None here (that tier is simply skipped)."""
    data = _single_quote_data(sell_token, buy_token, sell_amount_atomic, fee)
    try:
        raw = call_fn(quoter, data)
    except Exception:
        logger.exception("v3 quoter call failed chain fee=%s", fee)
        return None
    return _decode_amount_out(raw)


def _encode_exact_input_single(sell_token, buy_token, fee, recipient, amount_in, amount_out_min):
    """SwapRouter02.exactInputSingle(ExactInputSingleParams) calldata. Params (SwapRouter02 has NO
    deadline field): (tokenIn, tokenOut, fee, recipient, amountIn, amountOutMinimum, sqrtPriceLimitX96)."""
    return ("0x" + EXACT_INPUT_SINGLE
            + _w_addr(sell_token) + _w_addr(buy_token) + _w_uint(fee)
            + _w_addr(recipient) + _w_uint(amount_in) + _w_uint(amount_out_min) + _w_uint(0))


def _encode_ramses_exact_input_single(sell_token, buy_token, tick_spacing, recipient,
                                      deadline, amount_in, amount_out_min):
    return ("0x" + RAMSES_EXACT_INPUT_SINGLE
            + _w_addr(sell_token) + _w_addr(buy_token) + _w_uint(tick_spacing)
            + _w_addr(recipient) + _w_uint(deadline) + _w_uint(amount_in)
            + _w_uint(amount_out_min) + _w_uint(0))


def _encode_pancake_exact_input_single(sell_token, buy_token, fee, recipient, deadline,
                                      amount_in, amount_out_min):
    """PancakeSwap V3 SwapRouter.exactInputSingle. Same field order as Uniswap's SwapRouter02
    variant with `deadline` inserted after `recipient`. The struct is all-static, so it encodes
    inline with no outer offset."""
    return ("0x" + PANCAKE_EXACT_INPUT_SINGLE
            + _w_addr(sell_token) + _w_addr(buy_token) + _w_uint(fee)
            + _w_addr(recipient) + _w_uint(deadline) + _w_uint(amount_in)
            + _w_uint(amount_out_min) + _w_uint(0))


def _encode_pancake_exact_input(path, recipient, deadline, amount_in, amount_out_min):
    """PancakeSwap V3 SwapRouter.exactInput. The struct holds `bytes path`, so it is DYNAMIC and
    needs an outer offset — eth_abi handles that; hand-rolling it is how the first probe of this
    selector produced a false 'router rejects it' reading."""
    from eth_abi import encode
    payload = encode(
        ["(bytes,address,uint256,uint256,uint256)"],
        [(path, recipient, int(deadline), int(amount_in), int(amount_out_min))],
    )
    return "0x" + PANCAKE_EXACT_INPUT + payload.hex()


def _encode_path(tokens, fees):
    """Uniswap V3 packed path: token(20) + fee(3) + token(20) [+ fee + token]."""
    if len(tokens) != len(fees) + 1:
        raise ValueError("path token/fee arity mismatch")
    raw = bytes.fromhex(str(tokens[0]).lower().removeprefix("0x"))
    if len(raw) != 20:
        raise ValueError("invalid path token")
    for fee, token in zip(fees, tokens[1:]):
        nxt = bytes.fromhex(str(token).lower().removeprefix("0x"))
        if len(nxt) != 20 or not 0 <= int(fee) < (1 << 24):
            raise ValueError("invalid path rung")
        raw += int(fee).to_bytes(3, "big") + nxt
    return raw


def _quote_amount_out_path(call_fn, quoter, path, sell_amount_atomic):
    """QuoterV2 exact-input path quote. The first return word is the exact amount out."""
    data = _path_quote_data(path, sell_amount_atomic)
    try:
        raw = call_fn(quoter, data)
    except Exception:
        logger.exception("v3 path quoter call failed")
        return None
    return _decode_amount_out(raw)


def _encode_exact_input(path, recipient, amount_in, amount_out_min):
    from eth_abi import encode
    payload = encode(
        ["(bytes,address,uint256,uint256)"],
        [(path, recipient, int(amount_in), int(amount_out_min))],
    )
    return "0x" + EXACT_INPUT + payload.hex()


def _route_gas():
    """MEMECOIN_EVM_DEFAULT_ROUTE_GAS: this builder's transaction carries its own limit. A knob at 0
    or below would sign gas 0 ("0" is truthy to the signer): the default applies instead."""
    from sniper.settings import memecoin as _cfg
    return int(_cfg.MEMECOIN_EVM_DEFAULT_ROUTE_GAS) if int(_cfg.MEMECOIN_EVM_DEFAULT_ROUTE_GAS) > 0 \
        else 900_000


def _with_expiry(chain, data, *, now_fn=None):
    """SwapRouter02 calldata unchanged, except on an expiry chain (spec v3 §3.7): there it is
    wrapped in `multicall(uint256 deadline, bytes[])` (0x5ae401dc, verified in the deployed
    bytecode, evm_mixed_hop_router) so the swap carries a deadline its dead proof can read."""
    from sniper.settings.memecoin import memecoin_evm_expiry_chains, memecoin_evm_swap_deadline_s
    if str(chain) not in memecoin_evm_expiry_chains():
        return data
    from sniper.execution.evm_mixed_hop_router import encode_multicall
    if now_fn is None:
        from sniper.util.clock import real_utc_now
        now_fn = real_utc_now
    return encode_multicall(int(now_fn()) + memecoin_evm_swap_deadline_s(chain, 0), [data])


def dex_v3_quote(chain, sell_token, buy_token, sell_amount_atomic, taker, *, http=None,
                 slippage_bps=None, cfg_addr=None, call_fn=None, fee_tiers=None,
                 connectors=None, now_fn=None, pair_tokens_fn=None, extra_paths=None,
                 paths_only=False):
    """Executable direct Uniswap-V3 quote for `sell_amount_atomic` of `sell_token` -> `buy_token`,
    shaped for the shared venue executor (or None, fail-closed).

    Scans `fee_tiers` via QuoterV2 and keeps the deepest amountOut; builds SwapRouter02
    `exactInputSingle` calldata with `amountOutMinimum = amountOut * (1 - slippage)`.

    `extra_paths` [(tokens, fees), ...] are explicit multi-pool paths quoted in the SAME batch as
    the standard scan; `paths_only` quotes them alone (the bridge rung and its pinned reverse,
    2026-10-02). Neither changes the quote when absent."""
    cfg = None
    if cfg_addr is None or slippage_bps is None or fee_tiers is None or connectors is None \
            or call_fn is None:
        from sniper.settings import memecoin as cfg
    # cfg_addr: one {'router','quoter'} dict (back-compat) or a list of them. Unset -> every
    # configured deployment for the chain (2026-08-17: Robinhood runs multiple V3 factories).
    if cfg_addr is not None:
        deployments = tuple(cfg_addr) if isinstance(cfg_addr, (list, tuple)) else (cfg_addr,)
    else:
        deployments = cfg.memecoin_evm_v3_deployments(chain)
    deployments = tuple(d for d in deployments
                        if d and d.get("router") and d.get("quoter"))
    if not deployments:
        return None                              # unconfigured chain -> never a direct quote
    addr = deployments[0]
    try:
        sell_amount_atomic = int(sell_amount_atomic)
    except (TypeError, ValueError):
        return None
    if sell_amount_atomic <= 0:
        return None
    if slippage_bps is None:
        slippage_bps = int(round(cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT * 100))
    # PER-CHAIN TIER ORDER when the caller does not name one: the right tiers are a property of the
    # chain's pools (56 clusters at 1%, where 100/500/2500 measured empty).
    tiers = (tuple(fee_tiers) if fee_tiers is not None
             else cfg.memecoin_evm_v3_fee_tiers(chain))
    call_fn = call_fn or _make_call_fn(chain, http)

    best_out = 0
    best_descriptor = None
    best_path = None
    standard_candidates = [] if paths_only else [
        (("single", int(fee)), None,
         _single_quote_data(sell_token, buy_token, sell_amount_atomic, fee))
        for fee in tiers
    ]

    # Base custody is USDC while most memecoin pools quote in WETH. Compare configured
    # immutable two-hop rungs with direct pools; a thin direct pool must not mask a deeper
    # executable connector route. Only one endpoint may be this chain's exact quote token.
    quote_token = ((CHAIN_QUOTE.get(str(chain)) or {}).get("token") or "").lower()
    configured = (() if paths_only else cfg.memecoin_evm_v3_connectors(chain)
                  if connectors is None else tuple(connectors))
    for connector, quote_fee in configured:
        connector = str(connector)
        if connector.lower() in {str(sell_token).lower(), str(buy_token).lower()}:
            continue
        for token_fee in tiers:
            if str(sell_token).lower() == quote_token:
                fees = (quote_fee, token_fee)
            elif str(buy_token).lower() == quote_token:
                fees = (token_fee, quote_fee)
            else:
                continue
            try:
                path = _encode_path((sell_token, connector, buy_token), fees)
            except (TypeError, ValueError):
                continue
            standard_candidates.append((
                ("path", connector, int(quote_fee), int(token_fee)),
                path,
                _path_quote_data(path, sell_amount_atomic),
            ))

    for tokens, fees in (extra_paths or ()):
        tokens = [str(t).lower() for t in tokens]
        try:
            path = _encode_path(tokens, fees)
        except (TypeError, ValueError):
            continue
        standard_candidates.append((("xpath", tuple(tokens), tuple(int(f) for f in fees)), path,
                                    _path_quote_data(path, sell_amount_atomic)))

    if paths_only and not standard_candidates:
        return None
    best_dep = None
    best_protocol = None

    def _scan(shared_candidates, with_ramses):
        nonlocal best_out, best_descriptor, best_path, best_dep, best_protocol
        for dep in deployments:
            protocol = str(dep.get("protocol") or "uniswap_v3")
            if protocol == "ramses_v3":
                if not with_ramses or paths_only:
                    continue
                candidates = [
                    (("ramses_single", tick_spacing), None,
                     _ramses_single_quote_data(
                         sell_token, buy_token, sell_amount_atomic, tick_spacing))
                    for tick_spacing in _RAMSES_TICK_SPACINGS
                ]
            elif protocol in ("uniswap_v3", "pancake_v3"):
                # PancakeSwap's QuoterV2 is selector-identical to Uniswap's, so the QUOTE scan is
                # shared. Only the router calldata differs, below.
                candidates = shared_candidates
            else:
                continue
            calls = [(dep["quoter"], data) for _descriptor, _path, data in candidates]
            for (descriptor, path, _data), raw in zip(candidates, _call_many(call_fn, calls)):
                out = _decode_amount_out(raw)
                if out and out > best_out:
                    best_out = out
                    best_descriptor = descriptor
                    best_path = path
                    best_dep = dep
                    best_protocol = protocol

    _scan(standard_candidates, True)

    # PATHS THROUGH THE COIN'S OWN PAIR TOKEN (2026-09-11). BSC coins in the BREW family trade only
    # in a PancakeSwap V3 pool quoted in BREW, and BREW has a $1.1M V3 pool against WBNB; the scan
    # above knew only WBNB and USDT, so 0x77e2515c and 0xedd35445 refused `no_route`. Only after a
    # full miss, so the normal path costs nothing extra. Both leg tiers are unknown, so both are
    # scanned. ponytail: a thin direct pool still hides a deeper pair-token path; score both if
    # that shows up in refusals.
    lookup = pair_tokens_fn
    if lookup is None and best_descriptor is None and cfg is not None and not paths_only \
            and str(chain) in getattr(
            cfg, "MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS", ()):
        from sniper.execution.evm_v2_exit_quote import pair_quote_tokens as lookup
    if lookup is not None and best_descriptor is None and quote_token and not paths_only:
        sell_l, buy_l = str(sell_token).lower(), str(buy_token).lower()
        coin = buy_l if sell_l == quote_token else sell_l if buy_l == quote_token else None
        try:
            extra = [str(x).lower() for x in (lookup(chain, coin) or ())] if coin else []
        except Exception:
            extra = []
        discovered = []
        for x in extra:
            if x in (sell_l, buy_l):
                continue
            for quote_fee in tiers:
                for token_fee in tiers:
                    fees = (quote_fee, token_fee) if sell_l == quote_token else (token_fee, quote_fee)
                    try:
                        path = _encode_path((sell_token, x, buy_token), fees)
                    except (TypeError, ValueError):
                        continue
                    discovered.append((("path", x, int(quote_fee), int(token_fee)), path,
                                       _path_quote_data(path, sell_amount_atomic)))
        if discovered:
            _scan(discovered, False)

    if best_descriptor is None or best_out <= 0 or best_dep is None or best_protocol is None:
        # NO TIER PRICED IT. Two different facts arrive here and only one is about the coin.
        #
        # Report blindness ONLY when nothing answered at all. One tier reverting proves the node
        # was reachable and the quoter was live, so a coin with genuinely no v3 pool still returns
        # None and stays a real `no_route` — otherwise it would re-quote forever. A read we could
        # not make raises instead, and the ladder's `_run` turns `failure_type` into `blind_reason`
        # so the entry re-quotes rather than being published as unroutable.
        state = getattr(call_fn, "read_state", None)
        if isinstance(state, dict) and not state.get("answered") and state.get("unavailable"):
            kind, detail = state["unavailable"]
            raise V3ReadUnavailable(kind, detail)
        return None
    addr = best_dep
    min_out = best_out * (_BPS - int(slippage_bps)) // _BPS
    if best_protocol == "ramses_v3":
        if now_fn is None:
            from sniper.util.clock import real_utc_now
            now_fn = real_utc_now
        try:
            from sniper.settings.memecoin import memecoin_evm_swap_deadline_s
            deadline = int(now_fn()) + memecoin_evm_swap_deadline_s(chain, _RAMSES_DEADLINE_S)
        except (TypeError, ValueError, OverflowError):
            return None
        data = _encode_ramses_exact_input_single(
            sell_token, buy_token, best_descriptor[1], taker, deadline,
            sell_amount_atomic, min_out,
        )
    elif best_protocol == "pancake_v3":
        if now_fn is None:
            from sniper.util.clock import real_utc_now
            now_fn = real_utc_now
        try:
            from sniper.settings.memecoin import memecoin_evm_swap_deadline_s
            deadline = int(now_fn()) + memecoin_evm_swap_deadline_s(chain, _PANCAKE_DEADLINE_S)
        except (TypeError, ValueError, OverflowError):
            return None
        if best_descriptor[0] == "single":
            data = _encode_pancake_exact_input_single(
                sell_token, buy_token, best_descriptor[1], taker, deadline,
                sell_amount_atomic, min_out,
            )
        else:
            if best_path is None:
                return None
            data = _encode_pancake_exact_input(
                best_path, taker, deadline, sell_amount_atomic, min_out)
    elif best_descriptor[0] == "single":
        data = _with_expiry(chain, _encode_exact_input_single(
            sell_token, buy_token, best_descriptor[1], taker, sell_amount_atomic, min_out,
        ), now_fn=now_fn)
    else:
        if best_path is None:
            return None
        data = _with_expiry(chain, _encode_exact_input(
            best_path, taker, sell_amount_atomic, min_out), now_fn=now_fn)
    if best_protocol == "ramses_v3":
        route = {"protocol": "ramses_v3",
                 "tokens": [str(sell_token).lower(), str(buy_token).lower()],
                 "tick_spacings": [int(best_descriptor[1])]}
    elif best_descriptor[0] == "xpath":
        route = {"protocol": best_protocol, "tokens": list(best_descriptor[1]),
                 "fees": list(best_descriptor[2])}
    elif best_descriptor[0] == "single":
        # Report the protocol whose calldata we ACTUALLY built. Labelling a PancakeSwap swap
        # "uniswap_v3" would put a wrong venue in every downstream record and in any post-mortem.
        route = {"protocol": best_protocol,
                 "tokens": [str(sell_token).lower(), str(buy_token).lower()],
                 "fees": [int(best_descriptor[1])]}
    else:
        connector, quote_fee, token_fee = best_descriptor[1:]
        fees = ([int(quote_fee), int(token_fee)] if str(sell_token).lower() == quote_token
                else [int(token_fee), int(quote_fee)])
        route = {"protocol": best_protocol,
                 "tokens": [str(sell_token).lower(), str(connector).lower(),
                            str(buy_token).lower()],
                 "fees": fees}
    return {
        "sellAmount": str(sell_amount_atomic),
        "buyAmount": str(best_out),
        "minBuyAmount": str(min_out),
        "liquidityAvailable": True,
        "transaction": {"to": addr["router"], "data": data, "value": "0x0", "gas": str(_route_gas())},
        "issues": {"allowance": {"spender": addr["router"]}},
        "route": route,
    }


_SETTINGS_ALLOWLIST = object()


def v3_bridge_paths(chain, sell_token, buy_token, *, pair_tokens_fn=None, tiers=None,
                    connectors=None, allowlist=_SETTINGS_ALLOWLIST, rejected=None):
    """[(tokens, fees)] through the coin's own pair tokens, every fee-tier pair, or [].

    PI 0xf8e9...1f74 (2026-10-02): its market is a PCS V3 1% pool against ZEC, and ZEC trades
    against WBNB on V3. Bridges are the coin's top pair tokens minus the chain's quote token and its
    configured connectors (the standard scan already prices those). One end must be the quote
    token; the quote-side pool's fee comes first on a buy, last on a sell. Any lookup failure is no
    bridge, never an error.

    VETTED BRIDGES ONLY (Gamma MED, 2026-10-02). X is a second counterparty: a pausable or
    blacklisting X can block the bridge exit, which then falls to the thin direct pool. `allowlist`
    defaults to MEMECOIN_EVM_V3_BRIDGE_TOKENS[chain]; a pair token off it is appended to `rejected`
    (named in the rung's reason so the list grows from data). None = no filter: the exit of a held
    position reads its STORED X and must never be stranded by a later de-list.
    """
    from sniper.settings import memecoin as cfg
    quote = ((CHAIN_QUOTE.get(str(chain)) or {}).get("token") or "").lower()
    sell, buy = str(sell_token).lower(), str(buy_token).lower()
    if not quote or quote not in (sell, buy):
        return []
    coin = buy if sell == quote else sell
    if pair_tokens_fn is None:
        from sniper.execution.evm_v2_exit_quote import pair_quote_tokens as pair_tokens_fn
    try:
        pairs = [str(x).lower() for x in (pair_tokens_fn(chain, coin) or ())]
    except Exception:
        logger.exception("bridge pair lookup failed chain=%s coin=%s", chain, coin)
        return []
    skip = {quote, coin} | {str(c).lower() for c, _fee in (
        cfg.memecoin_evm_v3_connectors(chain) if connectors is None else connectors)}
    tiers = tuple(cfg.memecoin_evm_v3_fee_tiers(chain) if tiers is None else tiers)
    if allowlist is _SETTINGS_ALLOWLIST:
        allowlist = cfg.memecoin_evm_v3_bridge_tokens(chain)
    out = []
    for x in dict.fromkeys(pairs):
        if x in skip or len(x) != 42 or not x.startswith("0x"):
            continue
        if allowlist is not None and x not in allowlist:
            if rejected is not None:
                rejected.append(x)
            continue
        for quote_fee in tiers:
            for token_fee in tiers:
                fees = [int(quote_fee), int(token_fee)] if sell == quote else [int(token_fee),
                                                                                int(quote_fee)]
                out.append(([sell, x, buy], fees))
    return out
