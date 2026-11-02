"""Item 10 — EVM real-entry leg (DISARMED). The EVM analog of memecoin_real_leg.real_entry:

    arm gate (MEMECOIN_REAL_ENABLED + kill switch)
      -> S1-EVM risk gate + pessimistic reservation (reserve_entry_evm, tagged with the chain)
      -> pre-entry EVM token-safety gate (code / sell-route / sell-sim / tax)
      -> EVM entry executor (direct quote -> exact-amount approve -> nonce write-ahead -> send -> confirm)

This is the `evm_entry_fn` the network router dispatches allowlisted EVM tokens to. Tier-0 per-stage
latencies (gate/safety/exec) are stamped on every EVM order — the same record_exec_latency table the
Solana leg uses — so speed decisions have data. Injected seams; DISARMED unless armed. A rejection or
unsafe token never reaches the executor and frees the reservation."""
from __future__ import annotations

import logging
import functools
import math
import os
import threading
import time
from contextlib import contextmanager

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_arming import real_entries_allowed, real_entry_block_reason
from sniper.services import memecoin_arming as _arming
from sniper.services.memecoin_evm_risk import reserve_entry_evm
from sniper.services.memecoin_real_leg import (
    _entry_provenance, _executor_supports_kwarg, _executor_supports_trace,
)
from sniper.settings import memecoin as cfg


logger = logging.getLogger(__name__)

_FUNDED_EVM_READ_TIMEOUT_S = 3.0
_FUNDED_EVM_SAFETY_WAIT_S = float(os.getenv("MEMECOIN_FUNDED_EVM_SAFETY_WAIT_S", "12"))
# 4663 WAS 30.0 AND THAT IS THE FIRST DOMINO IN THE TITAN LOSS (2026-09-02).
#
# The safety worker runs inside the main loop, and the EXIT SWEEP lives in that same loop. So a
# slow entry does not just delay itself, it stops every rail on every open position for the
# duration. On 2026-09-02 the loop went 143 seconds without a cycle, the watchdog killed the runner
# for `sweep_stale`, and position 48565 — which had a CONFIRMED, APPROVED stop at -41.5% since
# 18:51:19 — did not sell until 18:55:27 at -81.3%. $27.80 of a $58.08 loss happened after we had
# decided to sell.
#
# Measured over 7 days of 4663 safety runs, 140 that ANSWERED and 24 that TIMED OUT:
#
#     answered    MED 2,096ms   p90 7,085ms   p99 18,102ms
#     timed out   burned up to 45,492ms and produced NOTHING
#
#     budget   answers lost   loop time saved per stalled entry
#        5s        21.4%              up to 25s
#        8s         7.9%              up to 22s
#       12s         5.0%              up to 18s     <- chosen
#       20s         0.7%              up to 10s
#
# 12s is not a fitted number: it is what every other funded EVM chain already uses, so this makes
# 4663 consistent rather than special. The cost is 5% of safety answers becoming
# `safety_deadline_exceeded`, which is FAIL-CLOSED — a refused entry, never a bad fill. Both values
# are env-settable so this can be tuned without a code change.
_FUNDED_EVM_4663_MULTIHOP_SAFETY_WAIT_S = float(
    os.getenv("MEMECOIN_FUNDED_EVM_4663_SAFETY_WAIT_S", "12"))
_RECENT_CONFIRMED_EXIT_OVERRIDE_S = 30 * 60


def _funded_evm_read_http(http):
    """Return the funded EVM read transport with a bounded route-read deadline."""
    if http is None:
        from sniper.execution.fast_http import PooledHttpClient
        from sniper.execution.submission_backends import register_client

        client = register_client(PooledHttpClient(read_timeout_s=_FUNDED_EVM_READ_TIMEOUT_S))

        def base_request(method, url, *, headers=None, body=None, timeout=None):
            return client.request(method, url, headers=headers, body=body, timeout=timeout)
        base_response_headers = client.request_with_headers
        base_metadata_headers = functools.partial(client.request_with_headers, strict_json=True)
    else:
        base_request = http
        base_response_headers = http
    state = threading.local()

    @contextmanager
    def safety_deadline(deadline):
        prior = getattr(state, "deadline", None)
        state.deadline = deadline
        try:
            yield lambda: time.monotonic() >= deadline
        finally:
            state.deadline = prior

    def bounded_request(base, method, url, *, headers=None, body=None, timeout=None):
        try:
            deadline = min(float(timeout), _FUNDED_EVM_READ_TIMEOUT_S)
        except (TypeError, ValueError):
            deadline = _FUNDED_EVM_READ_TIMEOUT_S
        safety_limit = getattr(state, "deadline", None)
        if safety_limit is not None:
            remaining = safety_limit - time.monotonic()
            if remaining <= 0:
                from sniper.execution.evm_token_safety import EvmSafetyDeadlineExceeded
                raise EvmSafetyDeadlineExceeded()
            deadline = min(deadline, remaining)
        return base(method, url, headers=headers, body=body, timeout=deadline)

    def request(method, url, *, headers=None, body=None, timeout=None):
        return bounded_request(
            base_request, method, url, headers=headers, body=body, timeout=timeout)

    def response_headers(method, url, *, headers=None, body=None, timeout=None):
        return bounded_request(
            base_response_headers, method, url, headers=headers, body=body,
            timeout=timeout)

    request.safety_deadline = safety_deadline
    request.safety_deadline_active = lambda: getattr(state, "deadline", None) is not None
    request.response_headers = response_headers
    if http is None:
        def owned_metadata_headers(method, url, *, headers=None, body=None, timeout=None):
            import httpx
            status, payload, response_meta = bounded_request(
                base_metadata_headers, method, url, headers=headers, body=body, timeout=timeout)
            # HTTPX lowercases header keys; provider cooldowns use Retry-After.
            return status, payload, httpx.Headers(response_meta or {})

        request.owned_metadata_headers = owned_metadata_headers
    return request


def _chain_of(route):
    return route.split(":", 1)[1] if ":" in route else route


# How long the Ink safety check waits for the head read that started beside the first quote.
# Measured beside a QuoterV2 quote on the public RPCs (see the Task 5a commit); a miss refuses.
_INK_HEAD_WAIT_S = 2.0


def _reconcile_op_fees(conn, chain, fee_fn):
    from sniper.execution.evm_op_fees import reconcile_fees
    return reconcile_fees(conn, chain, fee_fn)


def _v4_direct_quote_fn(chain, acct, address):
    """quote_fn(sell, buy, amount, *, key, pool_id, rpc_url, http, slippage_bps) -> a 0x-shaped V4
    quote and transaction for a pool key the caller holds, or None. One quoter call plus, for a
    sell, the Permit2 nonce read, both on `rpc_url`."""
    def quote(sell_token, buy_token, amount_atomic, *, key, pool_id, rpc_url, http=None,
              slippage_bps=None, quote_override=None):
        # `quote_override`: the 4663 fast lane's own quote for a WETH-quoted or bridged coin's
        # pool (routes rev 3, 6.5), read on the lane's host; the tx is built over it here, with
        # this leg's signer for the permit. None keeps the native-pool quote below.
        from sniper.execution.evm_v4_quote import dex_v4_quote, v4_direct_quote
        override = quote_override if quote_override is not None else v4_direct_quote(
            chain, key, pool_id, sell_token, buy_token, amount_atomic, rpc_url=rpc_url, http=http)
        if override is None:
            return None
        return dex_v4_quote(chain, sell_token, buy_token, amount_atomic, address, acct=acct,
                            rpc_url=rpc_url, http=http, quote_override=override,
                            slippage_bps=slippage_bps)
    return quote


def build_real_evm_context(
        chain_id, *, http=None, conn=None, lifi_entry_fn=None,
        lifi_runtime_factory=None, lifi_canary_activation=None, for_recovery=False,
        start_standing=False):
    """Assemble the EVM leg for one chain, or None if disarmed / creds missing (RPC + funded keypair).
    Mirrors the Solana build_real_context. The runner constructs one per allowlisted+configured chain;
    the evm_entry_fn dispatch routes a buy to the context for its chain."""
    if not cfg.MEMECOIN_REAL_ENABLED and not for_recovery:
        return None
    chain = str(chain_id)
    if cfg.memecoin_evm_exec_venue(chain) != "direct" and not for_recovery:
        return None
    if cfg.memecoin_evm_v3_addresses(chain) is None:
        return None
    rpc = cfg.evm_rpc_for_chain(chain)
    keypair_path = cfg.MEMECOIN_EVM_KEYPAIR
    if not rpc or not (keypair_path and os.path.exists(keypair_path)):
        return None
    from sniper.execution.evm_wallet import (
        CHAIN_QUOTE, erc20_balance_raw, erc20_decimals, eth_get_code, load_keypair,
        native_balance_or_none, native_balance_raw_or_none, quote_usd_price,
    )
    from sniper.execution.metadata_cache import BoundedLru
    from sniper.execution.evm_real_seams import (
        build_real_evm_executor, erc20_received_in_receipt, native_received_in_receipt,
        sign_tx as _sign_tx,
    )
    from sniper.execution.evm_token_safety import build_evm_token_safety

    acct = load_keypair(keypair_path)
    address = acct.address
    read_http = _funded_evm_read_http(http)
    decimals_cache = BoundedLru(256)
    quote = CHAIN_QUOTE.get(chain) or {}
    quote_token = quote.get("token")
    if quote_token is not None:
        decimals_cache.get_or_load(
            (str(chain), quote_token.lower()), lambda: int(quote["decimals"]))

    def decimals_fn(token):
        def load():
            value = erc20_decimals(token, rpc, http=read_http)
            return value if isinstance(value, int) and value >= 0 else None

        # chain in the key: same 0x address can carry different decimals on different
        # chains (18-dec Binance-Peg USDC footgun) — never rely on instance isolation alone.
        return decimals_cache.get_or_load((str(chain), token.lower()), load)

    def cached_decimals_fn(token):
        return decimals_cache.get_or_load((str(chain), token.lower()), lambda: None)

    def put_token_decimals_fn(token, decimals):
        if (type(decimals) is not int or not 0 <= decimals <= 255
                or not isinstance(token, str) or len(token) != 42 or token[:2].lower() != "0x"):
            return
        try:
            if len(bytes.fromhex(token[2:])) != 20:
                return
        except ValueError:
            return
        decimals_cache.get_or_load((str(chain), token.lower()), lambda: decimals)

    def put_token_facts_fn(token, facts):
        decimals, supply = facts
        if type(decimals) is int and 0 <= decimals <= 255 and type(supply) is int and supply > 0:
            decimals_cache.get_or_load((str(chain), token.lower()), lambda: decimals)
            # The funded ceiling guard prices the same coin from this read (4663 misses C).
            from sniper.services.memecoin_pons_metadata import remember_live_supply
            remember_live_supply(chain, token, decimals, supply)

    # Free-direct submission (auto_free) is the default: custody RPC + chain-owner official endpoints,
    # health-gated. pooled_rpc is the custody-only rollback; explicit rpc selects legacy eth_send_raw.
    from sniper.execution.submission_backends import build_evm_backend
    from sniper.settings.memecoin_submission import evm_submit_config
    _submit_cfg = evm_submit_config()
    _submit_transport = build_evm_backend(chain, custody_rpc_url=rpc, http=http)
    lifi_runtime = None
    canary = lifi_canary_activation is not None
    if chain == "4663" and (canary or cfg.memecoin_lifi_mode() == "authenticated_funded"):
        # LI.FI IS A FALLBACK, SO ITS FAILURE MUST NOT TAKE THE CHAIN DOWN (2026-09-04).
        # `validate_lifi_config` raises on a bad config and the runtime build raises when
        # authentication fails. Neither was caught, so the WHOLE 4663 executor failed to build --
        # and LI.FI is only ever consulted after every direct rung has already missed. An
        # aggregator outage was therefore taking v3, v2, pons_v2, v4 and v4_multihop down with it,
        # which is the entire funded leg for a chain whose direct routes were working fine.
        # Live 2026-09-04: LI.FI lost authentication overnight and the funded leg went with it.
        #
        # A CANARY STILL RAISES. That path exists because an operator deliberately activated it,
        # and degrading it silently would report a canary run that never had LI.FI in it.
        try:
            activation = (lifi_canary_activation if canary else
                          cfg.validate_lifi_config(conn=conn, custody_wallet=address))
            if lifi_runtime_factory is None:
                from sniper.execution.lifi_runtime import build_lifi_runtime
                lifi_runtime_factory = build_lifi_runtime
            manifest, manifest_hash = cfg.load_lifi_manifest()
            lifi_runtime = lifi_runtime_factory(
                conn=conn, chain_id=chain, custody=address, rpc_url=rpc,
                proof_rpc_url=os.getenv("MEMECOIN_LIFI_PROOF_RPC_URL_4663", "").strip(),
                http=read_http.response_headers, acct=acct, manifest=manifest,
                manifest_hash=manifest_hash,
                release_sha=os.getenv("MEMECOIN_LIFI_ACTIVE_RELEASE_SHA", "").strip(),
                activation=activation, submission_transport=_submit_transport,
                decimals_fn=decimals_fn, canary=canary,
            )
        except Exception:
            if canary:
                raise
            logger.exception(
                "LI.FI runtime unavailable on chain %s — direct routes continue WITHOUT the "
                "aggregator fallback; coins with no direct route will refuse as no_route", chain)
            lifi_runtime = None
    runtime_entry = (lifi_runtime or {}).get("entry_fn") or lifi_entry_fn
    runtime_preview = (lifi_runtime or {}).get("preview_fn")
    runtime_exit = (lifi_runtime or {}).get("exit_fn")
    from sniper.execution.evm_token_safety import build_lifi_token_safety
    lifi_code_safety_fn = build_lifi_token_safety(
        code_fn=lambda mint: eth_get_code(mint, rpc, http=read_http))

    def lifi_safety_fn(mint, **kwargs):
        safe, reason = token_safety_fn(mint, **kwargs)
        if not safe:
            return safe, reason
        return lifi_code_safety_fn(mint, **kwargs)
    ex = build_real_evm_executor(acct=acct, chain_id=chain, rpc_url=rpc, http=read_http,
                                 gasprice_buffer=cfg.MEMECOIN_EVM_GASPRICE_BUFFER,
                                 decimals_fn=decimals_fn, cached_decimals_fn=cached_decimals_fn,
                                 submission_transport=_submit_transport,
                                 backend_label=_submit_cfg.mode, lifi_entry_fn=runtime_entry,
                                 lifi_preview_fn=runtime_preview, lifi_exit_fn=runtime_exit,
                                 lifi_safety_fn=lifi_safety_fn, force_lifi=canary)
    # Code default "56" (today); 4663 at deploy keeps the early quote's 1 s price window (2026-10-15).
    if chain in cfg.memecoin_evm_quote_price_warmer_chains() and http is None:
        ex["start_quote_price_warmer_fn"]()
    if lifi_runtime is not None:
        recovery = lifi_runtime["recovery_context"]
        recovery.update({
            name: recovery.get(name) or ex["lifi_local"].get(name) or ex.get(name) for name in (
                "nonce_fn", "gas_price_fn", "sign_fn", "send_fn", "receipt_fn",
                "allowance_fn", "approve_gas", "recover_confirmed_fn",
                "reconcile_drop_fn",
            )
        })
    sell_routes = ["direct_v3_single_hop"]
    if cfg.MEMECOIN_EVM_V2_ENTRY_ENABLED and chain in cfg.MEMECOIN_DEX_BLIND_CHAINS:
        sell_routes.extend(("v2_fallback", "pons_v2"))
        if cfg.MEMECOIN_EVM_V4_ENABLED:
            sell_routes.append("v4")
            if cfg.MEMECOIN_EVM_V4_MULTIHOP_ENABLED:
                sell_routes.append("v4_multihop")
    sell_route_label = "+".join(sell_routes)
    from sniper.persistence.db_core import connection_scope, get_db_path, get_readonly_connection
    proof_db_path = get_db_path()

    def load_slot_proof(key):
        try:
            network_id, mint, spender = key
            proof_conn = get_readonly_connection(proof_db_path)
            try:
                return m.load_evm_slot_proof(
                    proof_conn, network_id=network_id, mint=mint, spender=spender)
            finally:
                proof_conn.close()
        except Exception:
            return None

    def store_slot_proof(key, proof):
        try:
            network_id, mint, spender = key
            with connection_scope(proof_db_path) as proof_conn:
                m.store_evm_slot_proof(
                    proof_conn, network_id=network_id, mint=mint, spender=spender, proof=proof)
        except Exception:
            pass

    token_safety_fn = build_evm_token_safety(chain_id=chain, rpc_url=rpc, taker=address, http=read_http,
                                             decimals_fn=decimals_fn,
                                             quote_fn=ex["venue_quote_fn"],
                                             probe_tokens_fn=ex.get("entry_probe_tokens_fn"),
                                             quote_with_miss_fn=ex.get("venue_quote_with_miss_fn"),
                                             entry_quote_probe_fn=ex.get("entry_quote_probe_fn"),
                                             sell_slippage_bps=int(round(
                                                 cfg.MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT * 100)),
                                             sell_route_label=sell_route_label,
                                             quote_rungs_fn=ex.get("take_quote_rungs_fn"),
                                             permit2_slot_prewarm=True,
                                             slot_proof_load=load_slot_proof,
                                             slot_proof_store=store_slot_proof,
                                             safety_deadline_scope=read_http.safety_deadline,
                                             safety_deadline_s=_FUNDED_EVM_SAFETY_WAIT_S,
                                             # 8453 since 2026-10-07 (KARP 8b609206): the
                                             # priority probe held kyber's hit 3.5 s behind
                                             # multihop's search. Chain 1 since 2026-10-08
                                             # (a1e93afb): its largest-output probe held v4's
                                             # 376 ms hit behind a 7.3 s bridge search. The
                                             # round trip and the post-revert requote still
                                             # use quote_fn, and exits are unchanged.
                                             fast_quote_fn=(functools.partial(
                                                 ex["venue_quote_fn"], first_available=True)
                                                 if chain in {"1", "56", "4663", "8453"}
                                                 else None))
    # Freeze which read members answer eth_simulateV1 now, off every path, so a round trip or a
    # buy-tax shadow never discovers it mid-entry (Alpha 2026-10-07; evm_token_safety).
    from sniper.execution.evm_token_safety import warm_simulate_members
    threading.Thread(target=warm_simulate_members, args=(rpc,), kwargs={"http": read_http},
                     name="evm-simulate-warm-%s" % chain, daemon=True).start()
    # THE MIXED-HOP RUNG'S EXIT PROOF (spec 2026-09-25) simulates selling a coin we do not hold, so
    # it needs this safety's balance/allowance override builder. Bound onto the ladder here, before
    # any wrapper below replaces the function; the rung refuses when it is absent.
    try:
        ex["venue_quote_fn"].fund_overrides_fn = token_safety_fn.fund_overrides
    except AttributeError:
        pass                        # an injected quote seam that cannot carry it: the rung refuses
    # LAUNCHPAD CURVE (spec 2026-09-17), default OFF. A coin still on its own bonding curve has no
    # dust-probe cascade to run -- the standard cascade would just miss it the same way every
    # other rung does pre-graduation. This wraps the safety function (not the shared per-chain
    # clip-check gate) so a live curve-phase mint gets this rung's own exact whole-balance quote
    # instead; every other mint falls straight through unchanged.
    if bool(getattr(cfg, "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", False)) and str(chain) == "4663":
        from sniper.execution.evm_launchpad_curve import _chain_weth as _curve_weth, wrap_token_safety_fn
        from sniper.execution.evm_wallet import native_usd_price
        # The executor's ETH mark (cut A): a WETH-quoted 4663 executor only, as 56 flap's guard.
        curve_price_peek = (ex.get("quote_price_peek_fn")
                            if quote.get("kind") == "eth" and quote.get("usd_symbol") == "ETH"
                            and quote.get("decimals") == 18
                            and str(ex.get("quote_token") or "").lower() == _curve_weth(chain)
                            and str(quote_token or "").lower() == _curve_weth(chain) else None)
        token_safety_fn = wrap_token_safety_fn(
            chain, rpc, http=read_http, taker=address, fallback_fn=token_safety_fn,
            native_usd_price_fn=curve_native_usd_price(
                chain, curve_price_peek, lambda c, *, http=read_http: native_usd_price(c, http=http)),
            safety_deadline_scope=read_http.safety_deadline,
            safety_deadline_s=_FUNDED_EVM_SAFETY_WAIT_S)
    # FLAP.SH CURVE (2026-09-20), default OFF. Same shape as launchpad above -- a coin still on
    # its own bonding curve has no dust-probe cascade to run. Not buildable until the allowance
    # override existed (2026-09-22): before that, an exact flap exit quote needed a real balance
    # and allowance, neither of which exists pre-entry, so this rung ran with NO exit-side clip
    # check at all (Gamma's/Alpha's review of 07d8db29/4dea7ba9). The override fabricates both for
    # a simulation, so the same "if I bought this, could I exit it" check chain 4663 has always
    # had is exact for flap.sh now too.
    if bool(getattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", False)) and str(chain) == "56":
        from sniper.execution.evm_flap_curve import (
            _chain_weth, wrap_token_safety_fn as _wrap_flap_safety_fn,
        )
        # The executor already fetched this entry's BNB mark. Peek only: joining its pending
        # Future could outlive this safety worker's deadline. Keep the existing live fallback
        # and 1 s freshness window; a stablecoin or foreign quote mark is never a BNB price.
        native_price_peek = (ex.get("quote_price_peek_fn")
                             if quote.get("kind") == "eth" and quote.get("usd_symbol") == "BNB"
                             and quote.get("decimals") == 18
                             and str(ex.get("quote_token") or "").lower() == _chain_weth(chain)
                             and str(quote_token or "").lower() == _chain_weth(chain) else None)

        def flap_native_usd_price(native_chain):
            if str(native_chain) == chain and callable(native_price_peek):
                try:
                    memo = native_price_peek()
                    px, age = memo["px"], memo["age_ms"]
                    if (type(px) in (int, float) and math.isfinite(px) and px > 0
                            and type(age) in (int, float) and math.isfinite(age)
                            and 0 <= age <= 1000):
                        return px
                except Exception:
                    pass  # Optional memo only; the same bounded live price read still decides.
            from sniper.execution.evm_wallet import native_usd_price
            return native_usd_price(native_chain, http=read_http)

        token_safety_fn = _wrap_flap_safety_fn(
            chain, rpc, http=read_http, taker=address, fallback_fn=token_safety_fn,
            native_usd_price_fn=flap_native_usd_price,
            safety_deadline_scope=read_http.safety_deadline,
            safety_deadline_s=_FUNDED_EVM_SAFETY_WAIT_S)
    # Serialize real execution per (wallet, CHAIN): Base (8453) and Robinhood (4663) share this EVM
    # address but have INDEPENDENT balances, so they key separately and never block each other, while
    # two trades on the SAME chain serialize (whole-wallet delta pricing — 2026-07-15 review CRIT-1).
    from sniper.execution.exec_lock import evm_exec_lock_key, serialize_executor
    _k = evm_exec_lock_key(chain, address)
    # THE FUNDED RUNNER ONLY (2026-10-15): the web and the deploy verify build contexts too, and the
    # web signed its own top-ups on every start (Base and 56, 20 s after the runner's), so the last
    # writer set the chain allowance under the runner's book. Only memecoin_runner_loop opts in.
    if (start_standing and str(chain) in cfg.memecoin_evm_standing_allowance_chains()
            and ex.get("standing_start_fn")):
        ex["standing_start_fn"]()            # top-ups for the pinned routers, on their own thread
    ex = {**ex,
          "execute_fn": serialize_executor(ex["execute_fn"], _k, holding_entry_chain=str(chain),
                                           maintenance_chain=str(chain)),
          "exit_fn": serialize_executor(
              ex["exit_fn"], _k, release_unsigned_close_on_error=True,
              maintenance_chain=str(chain)),
          "derisk_fn": (serialize_executor(ex["derisk_fn"], _k, maintenance_chain=str(chain))
                        if ex.get("derisk_fn") else None),
          "source_partial_fn": (serialize_executor(ex["source_partial_fn"], _k, maintenance_chain=str(chain))
                                if ex.get("source_partial_fn") else None),
          # Chain-1 Protect same-nonce replacement: under the same wallet lock as the exit.
          "replace_close_fn": (serialize_executor(ex["replace_close_fn"], _k,
                                                  maintenance_chain=str(chain))
                               if ex.get("replace_close_fn") else None)}

    def receipt_proceeds_fn(tx_hash, *, quote_asset_usd=None):
        # The same rule as the live exit (Alpha ruling 2026-10-06): the sell's canonical receipt
        # net, else a tx-bound venue payout event (Pons CurveSell), else unknown; never a balance
        # window or a block delta (Codex N2).
        from sniper.execution.evm_real_seams import exit_proceeds_atomic
        atomic, _source = exit_proceeds_atomic(
            tx_hash, rpc, owner=address, quote_token=quote_token, http=read_http,
            native=quote.get("kind") == "eth" and chain not in {"1", "57073"})
        price = quote_asset_usd or quote_usd_price(chain, http=read_http)
        if atomic is None or not price:
            return None
        return atomic / (10 ** int(quote["decimals"])) * float(price)

    from sniper.execution.evm_real_seams import (
        eth_receipt_state as _eth_receipt_state, eth_send_raw as _eth_send_raw,
        take_send_rejection as _take_send_rejection)
    from sniper.services.memecoin_exit_rebroadcast import (
        RECEIPT_TIMEOUT_S as _RECEIPT_TIMEOUT_S, short_timeout_http as _short_timeout_http,
        stored_payload_sender as _stored_payload_sender)
    _topup_local = ex.get("lifi_local") or {}
    from sniper.services.memecoin_native_float import remember_raw as _float_remember
    _gas_topup = ({
        "weth_token": quote_token,
        "rpc_url": rpc,
        # NATIVE FLOAT DRIFT (display, 2026-10-08): each read is also copied into the dashboard's
        # recorder (memecoin_native_float); remember_raw returns the value unchanged and never raises.
        "native_balance_raw_fn": lambda: _float_remember(
            chain, "native", native_balance_raw_or_none(address, rpc, http=read_http), 18),
        "weth_balance_raw_fn": lambda: _float_remember(
            chain, "wrapped", erc20_balance_raw(address, quote_token, rpc, http=read_http),
            int(quote.get("decimals", 18))),
        "nonce_fn": _topup_local["nonce_fn"],
        "claim_nonce_fn": ex.get("gas_claim_nonce_fn"),
        "release_nonce_fn": ex.get("gas_release_nonce_fn"),
        "gas_price_fn": _topup_local["gas_price_fn"],
        "sign_fn": _topup_local["sign_fn"],
        "send_fn": _topup_local["send_fn"],
        "confirm_once_fn": ex["_confirm_once"],
        # Optional on purpose: without them `_topup_provably_dead` proves nothing and the row
        # stays unresolved, exactly as it did before.
        "confirmed_nonce_fn": ex.get("confirmed_nonce_fn"),
        "pending_nonce_fn": ex.get("pending_nonce_fn"),
    } if all(callable(_topup_local.get(name)) for name in (
            "nonce_fn", "gas_price_fn", "sign_fn", "send_fn"))
          and callable(ex.get("_confirm_once")) else {})
    return {"execute_fn": ex["execute_fn"], "exit_fn": ex["exit_fn"],
            "bsc_execution_observer": ex.get("bsc_execution_observer"),
            # 2026-07-17: the executor now provides derisk_fn but this context previously
            # dropped it — the runner's ctx.get("derisk_fn") saw None and skipped every
            # EVM derisk ("venue for pos=… has no derisk_fn").
            "derisk_fn": ex.get("derisk_fn"),
            "source_partial_fn": ex.get("source_partial_fn"),
            "source_partial_receipt_fn": ex.get("source_partial_receipt_fn"),
            "source_partial_snapshot_fn": ex.get("source_partial_snapshot_fn"),
            "source_partial_proof_snapshot_fn": ex.get("source_partial_proof_snapshot_fn"),
            # The 4663 fast lane books its own fills and starts this after booking (2026-10-08):
            # the same executor's pre-approval the ordinary V4 entry starts through booked_fn.
            "preapprove_position_fn": ex.get("preapprove_position_fn"),
            "preview_exit_fn": ex.get("preview_exit_fn"),
            "replace_close_fn": ex.get("replace_close_fn"),
            # The leg's cached token decimals, read only to value a bag whose ledger has an exit of
            # unknown quantity (memecoin_real_runner._executable_unit_price).
            "decimals_fn": decimals_fn,
            "put_token_decimals_fn": put_token_decimals_fn,
            "put_token_facts_fn": put_token_facts_fn,
            "warm_quote_price_fn": ex.get("warm_quote_price_fn"),
            # The gate's own ETH/USD (P1), read from the price memo without fetching.
            "quote_price_peek_fn": ex.get("quote_price_peek_fn"),
            "entry_quote_probe_fn": ex.get("entry_quote_probe_fn"),
            "prepare_entry_quote_fn": ex.get("prepare_entry_quote_fn"),
            "confirm_fn": ex["_confirm"],
            "confirm_once_fn": ex["_confirm_once"],   # single-shot check for reservation recovery
            "confirmed_nonce_fn": ex.get("confirmed_nonce_fn"),
            # pending-tag nonce: with latest it tells the dropped-close recovery whether the
            # node is holding anything of ours at all (see evm_close_provably_dead).
            "pending_nonce_fn": ex.get("pending_nonce_fn"),
            # One block's view for the protocol-expiry dead proof (chain 1). The runner reads it
            # from THIS dict; without it every expired entry and sell is held forever.
            "proof_snapshot_fn": ex.get("proof_snapshot_fn"),
            "recover_confirmed_entry_fn": ex.get("recover_confirmed_entry_fn"),
            "receipt_proceeds_fn": receipt_proceeds_fn,
            "head_fn": ex.get("head_fn"),                 # Ink coherent safety state (Task 5a)
            # The runner's once-a-minute fee pass (own thread, own connection). OP-stack only.
            "fee_reconcile_fn": ((lambda fee_conn: _reconcile_op_fees(
                fee_conn, chain, ex["op_fee_fn"])) if ex.get("op_fee_fn") else None),
            "token_safety_fn": token_safety_fn,
            # THE 4663 FAST LANE'S TWO KEYED SEAMS (spec 2026-10-01 fast lane M2). The account stays
            # in this closure: the lane's safety cache quotes a pool key it already holds on the
            # host it names (the research QuickNode, Alpha option 1; no discovery), and the lane
            # signs its own buy with the same account and the same sign_tx every signer uses.
            "v4_direct_quote_fn": _v4_direct_quote_fn(chain, acct, address),
            "sign_tx_fn": (lambda tx_dict: _sign_tx(tx_dict, acct)),
            # .get: an injected executor (tests, older builders) may not carry them; the lane
            # then has no nonce and falls back on every signal.
            "lane_claim_nonce_fn": ex.get("lane_claim_nonce_fn"),
            "lane_release_nonce_fn": ex.get("lane_release_nonce_fn"),
            "gasprice_buffer": ex.get("gasprice_buffer"),
            "usdc_fn": ex["usdc_fn"], "token_fn": ex["token_fn"],
            "token_checked_fn": ex.get("token_checked_fn"),
            # retry-once read; None (read failed) refuses at the gate with an honest reason
            # instead of masquerading as a zero balance ("insufficient gas", 2026-07-13/14)
            "native_fn": (lambda: native_balance_or_none(address, rpc, http=read_http)),
            "gas_topup": _gas_topup,
            "address": address, "chain": chain, "kill_path": None,
            # THE HOLDINGS SELL VERIFIER READS THIS KEY (2026-09-22, operator queued sells).
            # holding_routes.verify_holding_quote proves the LI.FI quote against ctx["rpc_url"];
            # this context only carried the url inside gas_topup, so every 4663 holding sell
            # died with KeyError -> "internal error; check the runner log" (USDG 0x5fc5360d).
            "rpc_url": rpc,
            # THE HOLDING SELL'S FALLBACK VENUE (2026-09-22). LI.FI is one venue and on 4663 it is
            # the thin one, so a dust sell it refuses now re-asks the ladder that already prices
            # funded exits on this chain. Passing the executor's own callable keeps one ladder.
            "venue_quote_fn": ex.get("venue_quote_fn"),
            # THE DIP WATCH SUBSCRIPTS THESE TWO (2026-09-30, Gamma MEDIUM on 62fe9b21b). Its
            # ladder tick and every kyber, v3, v2, curve and mixed winner tick read them, and this
            # dict carried neither: each tick raised KeyError and the row expired unwatched.
            "venue_quote_with_miss_fn": ex.get("venue_quote_with_miss_fn"),
            "quote_token": ex.get("quote_token"),
            # The close recovery's pinned re-offer (memecoin_exit_rebroadcast): the executor's own
            # send seam, fed a sell's stored bytes. Unused unless the chain is in
            # MEMECOIN_PINNED_CLOSE_REOFFER_CHAINS.
            # The executor's baseline send (`eth_send_raw` to this chain's RPC), with the
            # re-offer's own short timeout and the node's words for a refused copy.
            "reoffer_send_fn": _stored_payload_sender(
                lambda raw: _eth_send_raw(raw, rpc, http=_short_timeout_http(read_http)),
                reason_fn=_take_send_rejection),
            # The copy's own receipt read, capped like its send.
            "reoffer_confirm_once_fn": lambda tx_hash: _eth_receipt_state(
                tx_hash, rpc, require_canonical=chain == "57073",
                http=_short_timeout_http(read_http, _RECEIPT_TIMEOUT_S)),
            "submission_backend": _submit_cfg.mode,
            "submission_transport": _submit_transport,
            "approval_pipeline": cfg.memecoin_evm_approval_pipeline(chain),
            "lifi_local": ex.get("lifi_local"),
            "lifi_exit_fn": runtime_exit,
            "lifi_enabled": runtime_entry is not None,
            "lifi_recovery_context": (lifi_runtime or {}).get("recovery_context")}


def curve_native_usd_price(chain, peek, live_fn):
    """The 4663 curve safety's ETH price (cut A, operator 2026-10-14 via Alpha): with
    MEMECOIN_EVM_CURVE_SAFETY_ONE_READ, the executor's memoised quote price when it is a finite
    positive number 0..1000 ms old; otherwise, and for any other chain, today's live read
    `live_fn` (a Binance GET, ~230-310 ms). The memo is peeked, never awaited (56 flap's rule)."""
    live_http = _names_kwarg(live_fn, "http")
    def price(native_chain, *, http=None):
        if (getattr(cfg, "MEMECOIN_EVM_CURVE_SAFETY_ONE_READ", False)
                and str(native_chain) == str(chain) and callable(peek)):
            try:
                memo = peek()
                px, age = memo["px"], memo["age_ms"]
                if (type(px) in (int, float) and math.isfinite(px) and px > 0
                        and type(age) in (int, float) and math.isfinite(age) and 0 <= age <= 1000):
                    return px
            except Exception:
                pass                     # an optional memo only; the live read still decides
        return (live_fn(native_chain, http=http) if http is not None and live_http
                else live_fn(native_chain))
    return price


def _safety_marks(timings):
    """The safety timings for the evm.token_safety substage: numbers rounded, anything else (the
    sell-probe record) as it is. Rounding a list raised inside the marker's bare except and lost
    the whole substage (2026-10-07)."""
    return {k: round(v, 1) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
            for k, v in timings.items()}


def _names_kwarg(fn, name):
    """True only when `fn`'s signature NAMES `name`; a bare **kwargs does not count."""
    import inspect
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def safety_exit_hint_kwargs(token_safety_fn, chain, reference):
    """{"exit_hint": pool} for one safety call: the V4 pool on the source's own receipt, when
    MEMECOIN_EVM_SAFETY_EXIT_HINT_CHAINS lists the chain and the function takes the keyword (the
    4663 curve wrapper forwards it through **kwargs); else {}. Peeks the reference, never waits."""
    from sniper.execution.evm_source_route import safety_exit_hint
    hint = safety_exit_hint(chain, reference)
    if hint is None or not _executor_supports_kwarg(token_safety_fn, "exit_hint"):
        return {}
    return {"exit_hint": hint}


def evm_admission_refusal(chain):
    """An ENTRY on an EVM chain out of MEMECOIN_EVM_ALLOWLIST refuses (disarmed), or None.

    A context can exist for a chain that is out of the allowlist: every chain with open inventory
    keeps an exit-and-recovery context (Gamma H3, 2026-09-30), so "a context exists" must never mean
    "entries are open". This was Ink-only. Routing already drops a non-allowlisted chain
    (memecoin_real_leg.route_real_entry) and the runner's context gate drops a recovery-only
    context, so this is defense in depth. Exits never come through here."""
    chain = str(chain)
    if chain not in cfg.MEMECOIN_EVM_ALLOWLIST:
        return {"status": "disarmed",
                "reason": "ink_admission_disabled" if chain == "57073" else "chain_not_allowlisted"}
    return None


def chain_capability_refusal(chain):
    """A chain-1 ENTRY refuses on its pinned coverage manifest (eth-mainnet spec decision 5), so
    allowlisting "1" alone never trades. Exits never come through here. None on every other chain;
    Ink keeps its own gate above. The allowlist check for every EVM chain is the separate H3 change
    (beta/evm-recovery-contexts)."""
    if str(chain) == "1":
        reason = cfg.eth_capability_reason()
        if reason:
            return {"status": "rejected", "reason": reason}
    return None


def evm_real_entry(conn, ev, *, route, day, usdc_balance, native_balance, now_iso, trade_size,
                   token_safety_fn, execute_fn, kill_path=None, clock=None, alert_fn=None,
                   trace=None, classification=None,
                   impact_cap_pct=None, chase_max_pct=None, wallet_chase_cap_pct=None,
                   wallet_chase_cap_expires_at=None, capacity_clip=None, trace_attempt_base=1,
                   warm_price_fn=None, size_cap_usd=None, head_fn=None, trial=None,
                   native_quote_price_usd=None, early_quote=None, entry_signing_snapshot_fn=None,
                   native_funding_precheck_fn=None):
    """Attempt one real EVM entry for a curated buy event. Returns the executor result (+ 'timing'),
    or {status: disarmed|rejected|route_failed|unsafe}. DISARMED unless MEMECOIN_REAL_ENABLED and
    no kill switch."""
    from sniper.settings.memecoin import MEMECOIN_REAL_POLICY
    clock = clock or time.perf_counter
    t0 = clock()

    if not real_entries_allowed(kill_path):
        # `real_entries_allowed` REMAINS the gate -- it is the seam the funded-leg tests
        # monkeypatch, and replacing it here silently bypassed their arrangement in 7 of
        # them. `real_entry_block_reason` is consulted only for the LABEL, which reaches
        # terminal_reason via _trace_terminal so the tape says which stand-down this was.
        return {"status": "disarmed",
                "reason": real_entry_block_reason(kill_path) or "unknown"}
    # QUIET WINDOW (2026-09-11): automatic entries stand down inside the operator's UTC window.
    # A manual buy is the operator's own hand and passes. Exits never come through here.
    from sniper.services.memecoin_origin import bypasses_selection, enforces_capacity
    _apply_quiet_hours = enforces_capacity(getattr(ev, "signal_source", None))
    if _apply_quiet_hours and _arming.in_quiet_hours():
        return {"status": "disarmed", "reason": "quiet_hours"}

    chain = _chain_of(route)
    refusal = evm_admission_refusal(chain)
    if refusal is not None:
        return refusal
    if chain == "57073":
        reason = cfg.ink_capability_reason()
        if reason:
            return {"status": "rejected", "reason": reason}
    refusal = chain_capability_refusal(chain)
    if refusal is not None:
        return refusal
    quote_bound_safety = chain in getattr(cfg, "MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS", ())
    if quote_bound_safety and not (
            _executor_supports_kwarg(execute_fn, "quote_safety_fn")
            and _executor_supports_kwarg(token_safety_fn, "bought_amount_atomic")):
        return {"status": "unsafe", "reason": "safety_exception: exact quote check unavailable"}
    if chain == "57073" and not _executor_supports_kwarg(token_safety_fn, "block_tag"):
        return {"status": "unsafe", "reason": "state_not_ready: the safety check cannot pin a block"}
    parallel = False
    try:
        parallel = not quote_bound_safety and _executor_supports_kwarg(execute_fn, "safety_gate_fn")
    except Exception:
        parallel = False

    # WARM THE QUOTE-TOKEN PRICE while the reservation runs. `execute_fn` opens with one live
    # Binance ETH/USD read that the entry quote cannot start without, because it sizes the input
    # amount: 411ms p50 / 7350ms p90 on 63 filled 4663 entries, 27.4% of the serial path, and 66 of
    # 66 missed the executor's 1.0s memo. Nothing earlier in the attempt fetches it — the runner's
    # balance poll ran seconds ago. Firing it here puts the SAME read beside the reservation
    # (532ms p50) instead of ahead of the quote. Read-only, daemon, never joined: a failure or a
    # slow read costs nothing because `execute_fn` still does its own live read on a memo miss.
    if warm_price_fn is not None:
        def _warm_price():
            try:
                warm_price_fn()
            except Exception:
                logger.debug("quote-price warm failed; the executor will read it live",
                             exc_info=True)
        threading.Thread(target=_warm_price, daemon=True,
                         name=f"evm-warm-price-{ev.mint[:10]}").start()

    safety_box = {}
    _chase_ref = getattr(ev, "evm_chase_reference", None)    # the sell probe's exit hint, peeked
    # SAFETY SUB-MARKS, DISPLAY ONLY (2026-09-28). Handed only to a safety function that NAMES
    # `timings`: a **kwargs wrapper would forward it to an inner function that raises, and a raised
    # safety check fails closed -- an entry refused for a display field.
    safety_timings = {}
    timings_kw = ({"timings": safety_timings}
                  if _names_kwarg(token_safety_fn, "timings") else {})
    safety_worker = None
    safety_deadline = None
    safety_started = None
    # THE EARLY PASS AT THE RESERVED CLIP (f31eeb917 on 56; item 2(A) on every listed chain). 56
    # defers whenever an early worker exists; another chain only once its early safety has
    # started, so a missing early check never delays this leg's own (7e8b1870: a 541 ms re-run
    # beside an early pass at the exact final clip).
    defer_early_safety = (parallel and early_quote is not None
                          and chain in cfg.memecoin_evm_defer_early_safety_chains()
                          and (chain == "56" or getattr(early_quote, "safety", None) is not None))
    if parallel:
        # This work only reads chain state. Start it before the serialized reservation and join it
        # before the executor's first wallet mutation. A refused reservation spends no custody funds.
        safety_wait_s = (_FUNDED_EVM_4663_MULTIHOP_SAFETY_WAIT_S
                         if chain == "4663" and cfg.MEMECOIN_EVM_V4_MULTIHOP_ENABLED
                         else _FUNDED_EVM_SAFETY_WAIT_S)
        safety_deadline = time.monotonic() + safety_wait_s
        safety_started = clock()

        def _safety_worker(checked_usd, box, timings):
            started = clock()
            try:
                # EARLY SAFETY (HUSH, MEMECOIN_EVM_EARLY_CACHED_SIZING): the early worker ran this
                # check beside its quote. Only a PASS for exactly this mint and usd, finished by its
                # own deadline, is reused; a refusal, an error or no verdict re-runs fresh here.
                early_safety = None
                try:
                    if early_quote is not None and hasattr(early_quote, "safety_for"):
                        early_safety = early_quote.safety_for(ev.mint, checked_usd)
                except Exception:
                    logger.warning("early safety lookup failed mint=%s", ev.mint, exc_info=True)
                if early_safety is not None:
                    verdict = early_safety.wait()
                    if isinstance(verdict, tuple) and verdict and verdict[0] is True:
                        box["result"], box["early"] = verdict, True
                        box["duration_ms"] = early_safety.duration_ms
                        timings.update(early_safety.timings or {})
                        return
                kwargs = ({"deadline": safety_deadline}
                          if _executor_supports_kwarg(token_safety_fn, "deadline") else {})
                if _executor_supports_kwarg(token_safety_fn, "usd"):
                    kwargs["usd"] = checked_usd
                if timings_kw:
                    kwargs["timings"] = timings
                kwargs.update(safety_exit_hint_kwargs(token_safety_fn, chain, _chase_ref))
                box["result"] = token_safety_fn(ev.mint, **kwargs)
            except Exception as exc:               # fail closed, never trade on a crashed check
                box["result"] = (False, f"safety_exception: {exc}")
            finally:
                if not box.get("early"):
                    box["duration_ms"] = max(0.0, clock() - started) * 1000.0
                box["finished_at"] = time.monotonic()

        def _start_safety_worker(usd):
            nonlocal safety_box, safety_timings
            box, timings = {"usd": usd}, {}
            safety_box, safety_timings = box, timings
            worker = threading.Thread(target=lambda: _safety_worker(usd, box, timings), daemon=True,
                                      name=f"evm-safety-{ev.mint[:10]}")
            worker.start()
            return worker

        if not defer_early_safety:
            safety_worker = _start_safety_worker(trade_size)

    # Manual entries skip the re-entry POLICY set (see the Solana leg); safety unaffected.
    # A desk entry keeps every capacity gate (spec desk-execution-plumbing §1.5).
    enforce_concurrency = enforces_capacity(getattr(ev, "signal_source", None))
    # OPERATOR OVERRIDE (2026-09-22, operator order): "Enter anyway" on a refused/failed card.
    # Named separately from the manual switch above -- override is stronger than plain manual, so
    # it must never be inferred from signal_source alone. See memecoin_risk.py's
    # override_risk_caps for the exact list of what this does and does not skip.
    override_risk = bool(getattr(ev, "override", False))
    funded_policy = None
    if not enforce_concurrency and classification is not None:
        from sniper.services.memecoin_funded_policy import _manual_unmanaged_policy
        funded_policy = _manual_unmanaged_policy(coin_band=classification.coin_band)
    # CROSS-WALLET RE-ENTRY (operator 2026-10-08): a full-cap wallet on this chain is cooled only
    # by its OWN closes; see memecoin_real_leg. The risk builder receives the scope, never the list.
    from sniper.services.memecoin_wallet_caps import cross_wallet_reentry_closes_by
    reentry_closes_by = cross_wallet_reentry_closes_by(ev.source_user_id, chain)
    # THE EARLY CLIP SNAP (Alpha GO 2026-10-17): only an automatic first attempt with no capacity
    # clip offers the early quote's clip; the reservation decides (memecoin_evm_risk._early_clip_snap).
    snap_facts, early_clip = None, None
    if early_quote is not None and trace_attempt_base == 1:
        snap_facts = {}
        eligible = (enforce_concurrency and not override_risk and capacity_clip is None
                    and str(getattr(early_quote, "mint", "") or "").lower() == str(ev.mint).lower())
        early_clip = getattr(early_quote, "clip", None) if eligible else None
        if not eligible:
            snap_facts.update(snapped=False, reason="not_eligible")
        elif early_clip is None:
            snap_facts.update(snapped=False, reason="early_clip_unset")
    t_reserve_started = clock()
    decision, pid = reserve_entry_evm(
        conn, mint=ev.mint, source_user_id=ev.source_user_id, strategy=MEMECOIN_REAL_POLICY,
        network_id=chain, day=day, usdc_balance=usdc_balance, native_balance=native_balance,
        native_quote_price_usd=native_quote_price_usd,
        now_iso=now_iso, classification=classification,
        include_reentry_policy=enforce_concurrency, reentry_closes_by=reentry_closes_by,
        enforce_concurrency=enforce_concurrency,
        # A desk entry, like a manual one, sizes at the clip it was given, never the bankroll clip.
        size_override=(capacity_clip if enforce_concurrency and capacity_clip is not None
                       else trade_size if bypasses_selection(getattr(ev, "signal_source", None))
                       else None),
        funded_policy=funded_policy, size_cap_usd=size_cap_usd,
        override_risk_caps=override_risk, trial=trial, desk=getattr(ev, "desk", None),
        early_clip_usd=early_clip, snap_facts=snap_facts if early_clip is not None else None,
        **_entry_provenance(ev, trace))
    t_gate = clock()
    try:
        if trace is not None:
            trace.mark("reservation", duration_ms=(t_gate - t_reserve_started) * 1000.0)
    except Exception:
        pass
    if snap_facts is not None and trace is not None:
        done_at, quote_clock = getattr(early_quote, "done_at", None), getattr(early_quote, "clock", None)
        try:
            age_ms = (None if done_at is None or not callable(quote_clock)
                      else (quote_clock() - done_at) * 1000.0)
            trace.mark_substage("evm.clip_snap", early_done=done_at is not None,
                                early_age_ms=age_ms, **(snap_facts or {"snapped": False,
                                                                       "reason": "not_reserved"}))
        except Exception:
            pass
    if not decision.allowed:
        from sniper.services.memecoin_risk import risk_refusal_result
        return risk_refusal_result(decision)
    from sniper.services.memecoin_wallet_caps import mark_reentry_lift
    mark_reentry_lift(trace, decision, reentry_closes_by)
    # spend EXACTLY what was reserved (bankroll sizing computes the clip in the gate).
    trade_size = decision.approved_size_usdc or trade_size
    if defer_early_safety or (safety_worker is not None and safety_box.get("usd") != trade_size
                              and time.monotonic() < safety_deadline):
        # The reserved clip's check starts NOW, beside the executor's quote: the reservation owns
        # sizing, and only this clip's verdict may authorize (1b46ef794). Gamma MEDIUM on
        # 1b46ef794: started inside the gate it ran serially after the quote (4663 p50 541 /
        # p90 1002 ms on the 48 of 65 fractional reservations). The early worker, deferred above,
        # already overlaps the reservation; an exact mint+usd early pass is reused here. Past the
        # original deadline nothing starts: the gate refuses safety_deadline_exceeded anyway.
        safety_started = clock()
        safety_worker = _start_safety_worker(trade_size)
    # AND TELL THE CALLER WHAT THAT WAS (2026-09-24). The route-capacity retry in
    # `_make_evm_entry_fn` used to halve the CONFIGURED base, which on a wallet holding less than
    # the base asks for more than is spendable and the risk context then refuses its own retry
    # (attempt 2cf2b654: $49.79 spendable, base 100, retry $50). The approved size is known only
    # here, so it rides back on every result below.
    approved_clip_usd = float(trade_size)
    if native_funding_precheck_fn is not None:
        funding_reason = native_funding_precheck_fn(approved_clip_usd)
        if funding_reason == "float_short":
            if not m.release_unsigned_blocked_entry(conn, pid, "bsc_fast_float_short"):
                return {"status": "unknown", "reason": "funding_precheck_release_conflict",
                        "pending_id": pid, "approved_clip_usd": approved_clip_usd}
            return {"status": "aborted", "reason": "float_short",
                    "pending_id": pid, "approved_clip_usd": approved_clip_usd}

    def _mark_safety(safe, reason):
        try:
            if trace is not None:
                marker = getattr(trace, "mark_substage", None)
                # One measured value, used for BOTH the substage and the stage column. Passing it
                # only to the substage left `safety_checks_ms` as the gap since the previous mark:
                # p50 1,534 ms against this stage's real 2,835 ms on the same 73 attempts.
                safety_duration_ms = (safety_box.get("duration_ms")
                                      if safety_box.get("duration_ms") is not None
                                      else max(0.0, clock() - safety_started) * 1000.0
                                      if safety_started is not None else None)
                marks = safety_timings.copy()
                curve = marks.pop("flap_curve_state", None)
                if marker is not None:
                    marker("evm.token_safety", outcome="passed" if safe else "refused",
                           reason=reason,
                           duration_ms=safety_duration_ms, early=bool(safety_box.get("early")),
                           **_safety_marks(marks))
                trace.mark("safety_checks", duration_ms=safety_duration_ms)
                sink = getattr(ev, "decision_features_sink", None)
                if isinstance(curve, dict) and isinstance(sink, dict):
                    # Measurement only (decision features): the Flap curve the check read, stamped
                    # in the trace's own clock domain, after the marks it must never disturb.
                    observed = getattr(trace, "reconciled_now", None)
                    sink.setdefault("flap_curve_state", dict(
                        curve, observed_at=observed() if callable(observed) else None))
        except Exception:
            pass

    def _recent_confirmed_exit_override(safe, reason):
        # BSC requires the current sell check: an earlier sell cannot establish that today's
        # route or liquidity survived. Do not add an exit-proof lookup to this refusal path.
        if chain == "56" or safe or not str(reason).startswith("sell_sim_reverted_unknown:"):
            return safe, reason
        try:
            proof = m.recent_confirmed_real_exit_for_coin(
                conn, chain, ev.mint, now_iso, max_age_s=_RECENT_CONFIRMED_EXIT_OVERRIDE_S)
        except Exception:
            return safe, reason
        if proof is None:
            return safe, reason
        try:
            if trace is not None:
                trace.mark_substage("evm.safety_recent_confirmed_exit", outcome="accepted",
                                    prior_reason=str(reason), **proof)
        except Exception:
            pass
        return True, "recent_confirmed_real_exit"

    safety_warning_box = {}

    def _apply_operator_override_safety(safe, reason):
        """Operator override (2026-09-22): token safety / clip-exit still RUNS -- it is never
        skipped -- but an unsafe verdict no longer refuses. The real verdict is kept, not
        overwritten, so the intent's result can show it as a warning: "run the safety check
        anyway ... never as a block" (the operator's own words, relayed by Alpha)."""
        if safe or not override_risk:
            return safe, reason
        safety_warning_box["reason"] = reason
        return True, "override: entered despite token safety (%s)" % reason

    def _refusal(reason):
        from sniper.execution.evm_token_safety import EVM_ROUTE_UNAVAILABLE_PREFIX
        if alert_fn:
            alert_fn(ev.mint, reason)
        status = ("route_failed" if (
                      str(reason).startswith(EVM_ROUTE_UNAVAILABLE_PREFIX)
                      or str(reason).startswith("entry_simulation_"))
                  else "unsafe")
        return {"status": status, "reason": reason, "pending_id": pid,
                "approved_clip_usd": approved_clip_usd}

    execute_kwargs = {"trace": trace} if (
        trace is not None and _executor_supports_trace(execute_fn)) else {}
    if early_quote is not None and trace_attempt_base == 1 and _executor_supports_kwarg(execute_fn, "early_quote"):
        execute_kwargs["early_quote"] = early_quote
    if (chain == "56" and callable(entry_signing_snapshot_fn)
            and _names_kwarg(execute_fn, "entry_signing_snapshot_fn")):
        execute_kwargs["entry_signing_snapshot_fn"] = entry_signing_snapshot_fn
    for name, value in (
            ("impact_cap_pct", impact_cap_pct), ("chase_max_pct", chase_max_pct),
            ("wallet_chase_cap_pct", wallet_chase_cap_pct),
            ("wallet_chase_cap_expires_at", wallet_chase_cap_expires_at)):
        if _executor_supports_kwarg(execute_fn, name):
            execute_kwargs[name] = value
    if trace_attempt_base != 1 and _executor_supports_kwarg(execute_fn, "trace_attempt_base"):
        execute_kwargs["trace_attempt_base"] = trace_attempt_base
    # THE EVM CHASE REFERENCE (2026-09-28): the source's post-trade facts (the on-chain lane's
    # receipt decode, or the J7 lane's own receipt read, peeked). The executor binds them to the
    # pool its quote trades; absent, every gate keeps the average fill exactly as before.
    _post_ref = getattr(ev, "evm_chase_reference", None)
    if _post_ref is not None and _executor_supports_kwarg(execute_fn, "chase_reference"):
        execute_kwargs["chase_reference"] = _post_ref
    # THE SOURCE'S BLOCK (D-c, 2026-10-16): a winner priced on state older than it is re-asked
    # before the preflight. Only an executor that NAMES the keyword gets it; a J7 event has none,
    # and the executor then peeks the chase reference's receipt read instead.
    _source_block = getattr(ev, "source_block_number", None)
    if type(_source_block) is int and _source_block > 0 and _names_kwarg(execute_fn, "source_block"):
        execute_kwargs["source_block"] = _source_block
    # DIP MAX PREMIUM (2026-10-09): a dip decision carries its cap into the executor or is refused.
    from sniper.services.memecoin_real_leg import dip_cap_refusal
    _dip_refused = dip_cap_refusal(ev, execute_fn)
    if _dip_refused is not None:
        m.release_pending(conn, pid, _dip_refused)
        return {"status": "rejected", "reason": _dip_refused, "pending_id": pid,
                "approved_clip_usd": approved_clip_usd}
    if getattr(ev, "dip_cap", None) is not None:
        execute_kwargs["dip_cap"] = ev.dip_cap

    # Ink's unknown-age recheck rides into the executor, which runs it after approval and before
    # the swap is signed. It sits OUTSIDE the safety dispatch below: inside it, an `elif parallel`
    # / `else` chain lost its `if`, and every other quote-bound chain (5042) ran a second,
    # quantity-blind safety check before the executor (Beta, 2026-09-29).
    if chain == "57073":
        from sniper.services.memecoin_real_leg import _coin_age_presign_fn
        presign_check = _coin_age_presign_fn(ev, classification, trace)
        if presign_check is not None:
            if not _executor_supports_kwarg(execute_fn, "presign_check_fn"):
                m.release_pending(conn, pid, "coin_age_presign_unavailable")
                return {"status": "unsafe", "reason": "coin_age_presign_unavailable",
                        "pending_id": pid}
            execute_kwargs["presign_check_fn"] = presign_check

    # A BASE PRECONFIRMED SOURCE (spec 2026-10-09-memecoin-base-flashblocks-detection-design.md, D1(b)):
    # one local read of the detection's state just before signing; a source proved a phantom refuses.
    if chain == "8453":
        from sniper.services.memecoin_base_preconf import presign_check as _preconf_presign
        presign_check = _preconf_presign(getattr(ev, "fomo_event_id", None))
        if presign_check is not None:
            if not _executor_supports_kwarg(execute_fn, "presign_check_fn"):
                m.release_pending(conn, pid, "preconf_presign_unavailable")
                return {"status": "unsafe", "reason": "preconf_presign_unavailable",
                        "pending_id": pid}
            execute_kwargs["presign_check_fn"] = presign_check

    # COHERENT STATE ON INK (Task 5a, 2026-09-29, Alpha GO). The exact-clip safety reads (code,
    # sell simulation, token pull) run PINNED to one block B, the chain head read IN PARALLEL with
    # the executor's first quote, so no serial read is added in front of it. Refused as
    # state_not_ready, before any approval or signature: the head is unavailable; the source's own
    # block time is unknown; the head is older than the source block (a lagging endpoint would
    # simulate a state from before the buy we copy); the read endpoint cannot serve B.
    ink_head = None
    if chain == "57073":
        if head_fn is None:
            m.release_pending(conn, pid, "state_not_ready")
            return {"status": "unsafe", "reason": "state_not_ready: no chain head reader",
                    "pending_id": pid, "approved_clip_usd": approved_clip_usd}
        ink_head = {"done": threading.Event()}

        def _read_head():
            try:
                ink_head["head"] = head_fn()
            except Exception as exc:              # a failed read refuses; it never admits
                ink_head["error"] = type(exc).__name__
            finally:
                ink_head["done"].set()
        threading.Thread(target=_read_head, daemon=True,
                         name=f"ink-head-{ev.mint[:10]}").start()

    def _ink_state_reason():
        """None, or why the pinned state is not ready. Joins the parallel head read."""
        if not ink_head["done"].wait(_INK_HEAD_WAIT_S):
            return "state_not_ready: chain head read did not answer in %.1fs" % _INK_HEAD_WAIT_S
        head = ink_head.get("head")
        if not head:
            return "state_not_ready: chain head unavailable (%s)" % ink_head.get("error", "empty")
        number, timestamp = head
        source_time = getattr(ev, "source_block_time", None)
        if source_time is None:
            return "state_not_ready: the source block time is unknown"
        if int(timestamp) < int(source_time):
            return ("state_not_ready: read head %d (t=%d) is older than the source block (t=%d)"
                    % (number, timestamp, int(source_time)))
        return None

    if quote_bound_safety:
        def quote_safety_fn(quote):
            nonlocal safety_started
            safety_started = clock()
            try:
                amount = int(quote.get("buyAmount") or 0)
                if amount <= 0:
                    return False, "safety_blind_exit_quote: bought quantity unavailable"
                pinned = {}
                if ink_head is not None:
                    reason = _ink_state_reason()
                    if reason:
                        _mark_safety(False, reason)
                        return False, reason
                    pinned["block_tag"] = hex(int(ink_head["head"][0]))
                safe, reason = token_safety_fn(
                    ev.mint, usd=trade_size, bought_amount_atomic=amount, **timings_kw, **pinned,
                    **safety_exit_hint_kwargs(token_safety_fn, chain, _chase_ref))
                _mark_safety(safe, reason)
                return _apply_operator_override_safety(safe, reason)
            except Exception:
                return False, "safety_exception: exact quote check failed"

        execute_kwargs["quote_safety_fn"] = quote_safety_fn
        t_safety = clock()
    elif parallel:
        # token-safety is read-only RPC (p50 1.7s on 4663) — run it concurrently with the
        # executor's quote. The executor JOINS + CHECKS the verdict via safety_gate_fn before its
        # first wallet mutation (approval / pre-tx / signing); unsafe releases the reservation
        # there and surfaces as unsafe_gate, mapped below to the SAME statuses as sequential.
        def safety_gate_fn():
            nonlocal safety_worker
            safety_worker.join(timeout=max(0.0, safety_deadline - time.monotonic()))
            if (not safety_worker.is_alive() and safety_box.get("usd") != trade_size
                    and time.monotonic() < safety_deadline):
                # Reservation owns sizing; an earlier answer cannot authorize a different clip.
                safety_worker = _start_safety_worker(trade_size)
                safety_worker.join(timeout=max(0.0, safety_deadline - time.monotonic()))
            if (safety_worker.is_alive() or safety_box.get("usd") != trade_size
                    or safety_box.get("finished_at", float("inf")) > safety_deadline):
                result = (False, "safety_deadline_exceeded")
            else:
                result = safety_box.get("result") or (False, "safety_exception: no verdict")
            safe, reason = result
            safe, reason = _recent_confirmed_exit_override(safe, reason)
            _mark_safety(safe, reason)
            return _apply_operator_override_safety(safe, reason)

        execute_kwargs["safety_gate_fn"] = safety_gate_fn
        t_safety = clock()                          # absorbed into exec_ms; trace carries truth
    else:
        seq_kwargs = ({"usd": trade_size}
                      if _executor_supports_kwarg(token_safety_fn, "usd") else {})
        seq_kwargs.update(timings_kw)
        seq_kwargs.update(safety_exit_hint_kwargs(token_safety_fn, chain, _chase_ref))
        safe, reason = token_safety_fn(ev.mint, **seq_kwargs)
        t_safety = clock()
        safe, reason = _recent_confirmed_exit_override(safe, reason)
        _mark_safety(safe, reason)
        safe, reason = _apply_operator_override_safety(safe, reason)
        if not safe:
            m.release_pending(conn, pid, "token_safety")
            return _refusal(reason)

    result = execute_fn(conn, pid, ev.mint, trade_size, now_iso, **execute_kwargs)
    if result.get("status") == "unsafe_gate":       # executor already released the reservation
        result = _refusal(result.get("reason"))
    t_exec = clock()
    # entry alerts are centralized in real_entry_hook (fires once for either venue on the final
    # result) — no self-alert here, or an EVM entry would page twice.

    timing = {
        "gate_ms": (t_gate - t0) * 1000.0,
        "safety_ms": (t_safety - t_gate) * 1000.0,
        "exec_ms": (t_exec - t_safety) * 1000.0,
        "total_ms": (t_exec - t0) * 1000.0,
    }
    try:
        pending = m.pending_row(conn, pid) or {}
        m.record_exec_latency(conn, mint=ev.mint, pending_id=pid,
                              status=result.get("status"), sig=result.get("tx_hash"),
                              timing=timing, at=now_iso, network_id=chain,
                              attempt_id=pending.get("attempt_id"),
                              origin_event_id=pending.get("origin_event_id"),
                              signal_source=pending.get("signal_source"),
                              source_price=(result.get("source_price")
                                            if result.get("source_price") is not None
                                            else pending.get("source_price")),
                              price_attribution=result.get("price_attribution"),
                              quote_price=result.get("quote_price"),
                              quote_observed_at=result.get("quote_observed_at"),
                              quote_age_ms=result.get("quote_age_ms"),
                              quote_id=result.get("quote_id"),
                              quote_route_target=result.get("quote_route_target"),
                              fill_price=result.get("fill_price"),
                              source_drift_pct=result.get("source_drift_pct"),
                              execution_slippage_pct=result.get("execution_slippage_pct"),
                              total_copy_drift_pct=result.get("total_copy_drift_pct"))
    except Exception:
        pass   # instrumentation is best-effort; never break the trade path
    if result.get("status") == "filled" and result.get("position_id"):
        if safety_warning_box.get("reason"):
            result = {**result, "token_safety_warning": safety_warning_box["reason"]}
        if override_risk:
            try:
                m.set_experiment_status(conn, result["position_id"], "manual_override")
            except Exception:
                logger.exception("manual_override marker failed to write pos=%s",
                                 result.get("position_id"))
    return {**result, "pending_id": pid, "timing": timing,
            "approved_clip_usd": approved_clip_usd}
