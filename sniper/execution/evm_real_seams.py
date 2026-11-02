"""Item 7b — real HTTP/RPC/signing bindings for the DISARMED EVM executor seams.

Supplies the real implementations the EVM executor (`evm_swap_executor.execute_entry_evm`) orchestrates:
  - direct on-chain DEX quote/transaction construction (ExactIn), injected or chain-configured;
  - EVM JSON-RPC: eth_getTransactionCount (nonce), eth_gasPrice, eth_sendRawTransaction,
    eth_getTransactionReceipt (confirm).
  - eth_account: local secp256k1 signing (raw tx + hash).

`build_real_evm_executor` wires these into execute_fn(conn, pid, mint, usd, now_iso) — recovering
source_user_id + strategy from the pending row and tagging the chain. Every function takes an
injectable http so the units test without a live endpoint. Fail-closed throughout: a bad response ->
None -> the executor aborts/leaves-reserved, never a fabricated fill. Approvals are EXACT-amount.

⚠ DISARMED. Requires a funded EVM keypair + an RPC + verified direct DEX configuration.
"""
from __future__ import annotations

import hashlib
import functools
import json
import logging
import math
import re
import threading
import time as _time
import urllib.parse
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

from sniper.execution.evm_wallet import (
    CHAIN_QUOTE, CHAIN_USDC, SEL_ALLOWANCE, SEL_APPROVE, _eth_call, _http, _pad_addr, _rpc,
    start_rpc_tap as _start_rpc_tap, take_rpc_tap as _take_rpc_tap,
    start_http_tap as _start_http_tap, take_http_tap as _take_http_tap,
    erc20_balance, erc20_balance_checked,
    erc20_balance_with_decimals, erc20_balance_raw_checked, erc20_decimals, native_usd_price,
    is_native_quote_token, native_quote_chains,
    quote_usd_price, quote_balance_usd_or_none, usdc_balance, native_balance_or_none,
)
from sniper.persistence import db_memecoin as m
from sniper.execution import evm_op_fees as _op_fees
from sniper.services.memecoin_evm_gas_topup import WETH_CALL_GAS_LIMIT
from sniper.util.receipt_clock import clock_domain

# Immutable startup metadata; confirmation never opens boot-id files on the entry path.
_RECEIPT_BOOT_ID = clock_domain()

logger = logging.getLogger(__name__)

def _evm_quantity(value):
    """Parse an EVM JSON-RPC quantity or a decimal integer without changing its value."""
    if isinstance(value, str) and value.lower().startswith("0x"):
        return int(value, 16)
    return int(value)


def pre_transaction_status(receipt_state):
    """The funded unwrap's outcome from ONE receipt answer (2026-09-28).

    Only a receipt with status 0x0 ('failed') is a REVERT: mined, nonce consumed, nothing unwrapped,
    and no swap was sent, so the reservation may be released at once. `not_found`, `unknown` and a
    missing answer prove nothing and stay ambiguous, exactly as before. Reservation 4158 held its
    slot for 11 minutes because 'failed' was folded into 'unknown'."""
    if receipt_state == "confirmed":
        return "confirmed"
    if receipt_state == "failed":
        return "reverted"
    return "unknown"


def signed_gas_facts(raw_hex):
    """{gas_price_wei, gas_limit} decoded from a signed legacy transaction's own bytes, or {}.

    CPU only, no network: the trace names the price and limit the unwrap was actually signed at."""
    import base64
    try:
        cost = decode_signed_tx_cost(base64.b64encode(str(raw_hex).encode("ascii")).decode("ascii"))
    except Exception:
        cost = None
    if not cost:
        return {}
    facts = {"gas_price_wei": cost.get("gas_price"), "gas_limit": cost.get("gas_limit")}
    if "max_priority_fee" in cost:                       # type 2: gas_price_wei is the cap
        facts["max_priority_fee_wei"] = cost["max_priority_fee"]
    return facts


def recover_confirmed_native_pre_transaction(
        conn, pending, *, address, quote_token, transaction_fn, sign_fn, send_fn, confirm_fn,
        now_iso):
    """Rewrap a confirmed WETH pre-transaction that never reached its swap.

    Scoped to WRAPPED-NATIVE chains by CONFIG, not by chain literal: only those unwrap before a
    swap, so only those can leave a confirmed unwrap with no swap behind it.
    """
    if (str(pending.get("network_id") or "") not in native_quote_chains()
            or pending.get("recovery_status") not in (
                "native_pre_transaction_ambiguous", "orphaned")):
        return None
    pre_hash = pending.get("tx_sig")
    if not pre_hash or pending.get("cleanup_tx_sig"):
        return None
    try:
        tx = transaction_fn(pre_hash) or {}
        data = str(tx.get("input") or "").lower()
        amount = int(data[10:], 16)
        valid = (
            str(tx.get("hash") or "").lower() == str(pre_hash).lower()
            and str(tx.get("from") or "").lower() == str(address).lower()
            and str(tx.get("to") or "").lower() == str(quote_token).lower()
            and _evm_quantity(tx.get("value") or 0) == 0
            and data.startswith("0x2e1a7d4d") and len(data) == 74 and amount > 0
        )
    except (TypeError, ValueError):
        return None
    if not valid:
        return None
    signed = sign_fn({"to": quote_token, "data": "0xd0e30db0", "value": amount,
                      "gas": WETH_CALL_GAS_LIMIT})
    if not signed:
        return None
    raw, cleanup_hash, nonce = signed
    import hashlib
    try:
        raw_bytes = bytes.fromhex(raw[2:] if str(raw).startswith("0x") else str(raw))
    except ValueError:
        return None
    if not m.bind_pending_cleanup(
            conn, pending["id"], tx_sig=cleanup_hash, nonce=nonce, amount_atomic=amount,
            payload_sha256=hashlib.sha256(raw_bytes).hexdigest(), signed_at=now_iso):
        return None
    sent = send_fn(raw)
    if sent is False:
        m.mark_pending_cleanup_status(conn, pending["id"], "explicit_rejected")
        return None
    status = confirm_fn(cleanup_hash)
    m.mark_pending_cleanup_status(conn, pending["id"], status)
    return {"status": "cleanup_confirmed"} if status == "confirmed" else None


def usd_to_quote_atomic(chain_id, usd, *, http=None, price_fn=None):
    """USD clip -> atomic amount of the chain's QUOTE token (USDC or WETH per CHAIN_QUOTE).
    None = unknown chain or unknown quote price (fail-closed: the entry quote aborts).
    On a WETH-quoted chain the ETH/USD mark is read per call — a price move between sizing and
    fill measurement adds ~bps of noise at $30 clips, which the honest chain-delta absorbs."""
    q = CHAIN_QUOTE.get(str(chain_id))
    if not q:
        return None
    px = quote_usd_price(chain_id, http=http, price_fn=price_fn)
    if not px:
        return None
    atomic = int(round(float(usd) / px * 10 ** q["decimals"]))
    return atomic if atomic > 0 else None


# --- direct quote with bounded V2 fallback ------------------------------------------------------

# Rung refusal reasons that mean THE ROUTE EXISTS AND WE TURNED IT DOWN, as opposed to the rung
# finding nothing. Kept as a tuple prefix match so `round_trip_over_budget:-16.18` matches without
# parsing the number.
_PRICED_AND_DECLINED = ("round_trip_over_budget",)


# A SEARCH WE STOPPED OURSELVES (audit E4, 2026-09-18; extended EX1, 2026-09-20). `evm_v4_multihop`
# notes `search_stopped:*` when its own deadline or request budget ended discovery, and
# `decision_block_unavailable` (exact, no suffix) when it could not obtain a valid fixed block to
# quote paths against at all -- `_measure_paths` never runs without one, so this is the same kind
# of blindness by the docstring's own test below ("nothing about the token was measured"), not a
# different one that needs its own bucket. Either note carried no `blind_reason` before EX1, so the
# ladder reported `no_route` — proved route absence — for a coin whose route was never looked for,
# and the caller may hand that verdict to a fallback venue (LI.FI) that bypasses this rung's own
# checks.
_LOCAL_LIMIT_NOTES = ("search_stopped", "decision_block_unavailable")

from sniper.execution.evm_source_route import SOURCE_RUNGS as _SOURCE_RUNGS  # noqa: E402
# The buy ladder's tie order (first measured hit wins on a 0-grace chain; the order ranks the
# runner-up hits). Every source-route rung must be in it: tests/test_memecoin_evm_bridge_prefix_cache.py.
# `v4_source_exit` (sells only, the safety probe's hinted exit) leads: a probe takes the first
# answered hit in this order.
LADDER_ORDER = ("v4_source_exit", "launchpad_curve", "flap_curve", "v3", "v3_bridge", "v2",
                "pons_v2", "v4", "v4_source", "v4_multihop", "v4_multihop_source",
                "v4_multihop_pinned", "mixed_v3_v2", "pancake_infinity", "kyber", "relay")

# OUR OWN GECKOTERMINAL COOLDOWN (2026-09-28, Alpha). While the funded GT cooldown runs,
# `gt_get_json` returns None WITHOUT an HTTP call, so a rung that found nothing during it was blind,
# but no provider refused it: it is a local limit, not a 429. It was typed `rate_limit`, which maps
# to QuoteFailure.RATE_LIMIT, and an exit preview with that failure holds the exact-quote budget of
# its provider group (all chains, before d1ce0ce7). It now has its own reason, typed like
# `search_limit`: nothing measured, transient, never a route verdict, never a provider 429.
DISCOVERY_COOLDOWN = "discovery_cooldown"


def multihop_blind_reason(note, *, gt_cold, discovery_observed):
    """The blind reason for a multihop miss, or None when the rung really answered.

    `discovery_cooldown`: our own GeckoTerminal cooldown ran and no discovery was seen (no HTTP
    call was made, so no provider refused us). `search_limit` is also OURS — our deadline, our
    request budget, or a fixed block we could not read. Both are blindness for the same reason:
    nothing about the token was measured.
    """
    if gt_cold and not discovery_observed:
        return DISCOVERY_COOLDOWN
    if str(note or "").startswith(_LOCAL_LIMIT_NOTES):
        return "search_limit"
    return None


def _quote_miss_failures():
    """`classify_quote_miss` code -> QuoteFailure. Module level so it can be read by a test.

    A code missing from this map silently becomes MALFORMED_RESPONSE, which is how a renamed label
    turns a transport fault into a claim about the token (Codex audit #12, and audit E4 again on
    2026-09-18 with `provider_search_limit`).
    """
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    return {
        "no_route": QuoteFailure.NO_ROUTE,
        "provider_rate_limit": QuoteFailure.RATE_LIMIT,
        "provider_rpc_transport": QuoteFailure.RPC_TRANSPORT,
        "provider_timeout": QuoteFailure.PROVIDER_TIMEOUT,
        # The whole ladder timed out waiting on every rung (evm_real_seams.py's own
        # `as_completed(..., timeout=ladder_deadline_s)`, blind_reason="ladder_deadline")
        # -- an infrastructure timeout exactly like `provider_timeout`, not garbage back
        # from a provider (Codex audit #12).
        "provider_ladder_deadline": QuoteFailure.PROVIDER_TIMEOUT,
        # A rung still unanswered when the post-hit grace ended (2026-09-23). It only exists
        # beside a measured hit, so a miss classified from it is a preflight story, not a
        # provider verdict; the same transient class as the deadline above.
        "provider_ladder_grace": QuoteFailure.PROVIDER_TIMEOUT,
        "provider_malformed_response": QuoteFailure.MALFORMED_RESPONSE,
        # The v4 blind sites split out of a shared "rpc_transport" on 2026-09-02. All
        # four map to RPC_TRANSPORT so the split is PURELY a rename in behaviour terms.
        # Without these the `.get(code, MALFORMED_RESPONSE)` default would silently
        # reclassify every v4 provider failure — a renamed label falling off the end of
        # a lookup is the same defect class as the retry set below.
        # `provider_poolkey_read_timeout` arguably belongs on PROVIDER_TIMEOUT; that is
        # a behaviour decision and is deliberately NOT taken here.
        "provider_discovery_all_blind": QuoteFailure.RPC_TRANSPORT,
        # 2026-09-03: the chain scan failed, or every source answered empty on what may
        # be a pool younger than the log index. Neither is "no route".
        "provider_discovery_rpc_failed": QuoteFailure.RPC_TRANSPORT,
        "provider_discovery_unproven": QuoteFailure.RPC_TRANSPORT,
        "provider_poolkey_read_failed": QuoteFailure.RPC_TRANSPORT,
        "provider_poolkey_read_timeout": QuoteFailure.RPC_TRANSPORT,
        "provider_poolkey_malformed": QuoteFailure.RPC_TRANSPORT,
        # OUR OWN deadline or request budget stopped the search (audit E4, 2026-09-18). Nothing
        # about the token was measured, so it is a timeout like the ladder deadline above — and it
        # must NOT be NO_ROUTE, which is the verdict that sends an exit to the LI.FI fallback.
        "provider_search_limit": QuoteFailure.PROVIDER_TIMEOUT,
        # Our own GeckoTerminal cooldown (2026-09-28): a local limit, the same class as the search
        # limit above. Never RATE_LIMIT, which holds an exact-quote provider group's budget.
        "provider_discovery_cooldown": QuoteFailure.PROVIDER_TIMEOUT,
        # An RPC 429 (v2 since 2026-09-14, v4's pool-key read since 2026-10-07). It fell through to
        # MALFORMED_RESPONSE; both are infra. RPC_TRANSPORT keeps v4's exit class what it was as
        # `provider_poolkey_read_failed`. RATE_LIMIT would hold the exit's exact-quote provider
        # group budget: a behaviour decision, deliberately not taken here.
        "provider_rpc_rate_limit": QuoteFailure.RPC_TRANSPORT,
    }


QUOTE_MISS_FAILURES = _quote_miss_failures()


def rung_error_text(exc):
    """`<Class>: <message>` of a rung that raised, for the ledger and the miss detail. A URL of any
    scheme is cut to its host (an RPC URL can carry a key), key/token query values are masked
    (Gamma LOW, 2026-09-29), and the text is cut to 200 characters."""
    import re
    from urllib.parse import urlparse
    message = re.sub(r"[a-z][a-z0-9+.-]*://[^\s'\"<>]+",
                     lambda m: urlparse(m.group()).hostname or "url", str(exc), flags=re.I)
    message = re.sub(r"(api[-_]?key|key|token)=[^&\s'\"]+", r"\1=***", message, flags=re.I)
    return ("%s: %s" % (type(exc).__name__, message))[:200]


def _blind_phrase(rung):
    """'<rung> could not read: <reason>', plus host, method and outcome of its last failed call, and
    the error text of a rung that raised (2026-09-29: a Kyber 400 read as bare rpc_transport)."""
    text = "%s could not read: %s" % (rung["rung"], rung["blind_reason"])
    if rung.get("reason"):
        text += " [%s]" % rung["reason"]
    failed = [c for c in (rung.get("calls") or [])
              if isinstance(c, dict) and c.get("outcome") not in (None, "ok")]
    if failed:
        from urllib.parse import urlparse
        call = failed[-1]
        host = urlparse(str(call.get("url_requested") or "")).hostname or "unknown host"
        text += " (%s %s %s)" % (host, call.get("method") or "call", call["outcome"])
    return text


def classify_quote_miss(rungs):
    """Map a rung ledger from `venue_quote_with_v2_fallback` to ``(code, detail)``.

    A rung that could not READ says nothing about the token. Report the blind rung first: an
    entry refused while V4 pool discovery was rate limited is an infrastructure refusal, not
    proof that the coin has no route (4663 HOOKR / 0xdd90bf, 2026-08-07). Only when every
    configured rung answered do we report `no_route`.

    `rungs` is the list of `{"rung", "hit", "skipped", ...}` dicts the ladder appends.
    """
    if not rungs:
        return "quote_unattributed", "the quote ladder recorded no rung"
    if any(r.get("hit") for r in rungs):
        return None, None
    blind = [r for r in rungs if r.get("blind_reason")]
    declined = [r for r in rungs
                if not r.get("skipped") and not r.get("hit") and not r.get("blind_reason")
                and str(r.get("reason") or "").startswith(_PRICED_AND_DECLINED)]
    declined_detail = "; ".join(
        "%s priced this route and declined it: %s" % (r["rung"], r["reason"])
        for r in declined)
    if blind:
        first = blind[0]
        # THE CODE STAYS BLIND, THE SENTENCE NAMES THE DECISION TOO (2026-09-12). Aircoin on 4663
        # paged "v2 could not read: rpc_transport" seven times while the pons_v2 rung had priced
        # the curve at -13.4% round trip and declined it on cost. The page named the one rung that
        # could not have decided and hid the rung that did.
        also = "; " + declined_detail if declined else ""
        if first["blind_reason"] == "route_capacity":
            return "route_capacity", "%s rejected this clip for route capacity%s" % (first["rung"], also)
        # EVERY BLIND RUNG, AND THE CALL THAT FAILED (2026-09-25). PEA on Base read "v3 could not
        # read: unavailable" while v4's discovery had failed too, on mainnet.base.org with -32602;
        # the reader chased v3. The CODE stays the first blind rung's -- the retry lane keys on it.
        return ("provider_%s" % first["blind_reason"],
                "; ".join(_blind_phrase(r) for r in blind) + also)
    # A ROUTE WE PRICED AND DECLINED IS NOT A MISSING ROUTE (2026-09-09). Hock 0x43330ef8 on chain
    # 56 was reported as "no route on v3/v2/pons_v2" while its PancakeSwap V2 WBNB pair held
    # $13,586 and quoted cleanly on demand. The v2 rung had found it and refused it:
    # `round_trip_over_budget:-16.188742` against the 10% budget. The gate was right; the SENTENCE
    # was wrong, and it sent the reader hunting for liquidity that was there all along.
    #
    # Only reasons that mean "priced, then declined" are promoted. `no_bridge_candidates` genuinely
    # means that rung found nothing and stays part of `no_route`.
    if declined:
        first = declined[0]
        # A MARGINAL MISS KEEPS ITS OWN CODE (2026-09-17) so the retry lane can re-ask that class
        # alone. The sentence is unchanged apart from the code.
        code = ("route_over_budget_marginal"
                if str(first.get("reason") or "").startswith("round_trip_over_budget_marginal")
                else "route_over_budget")
        return code, declined_detail
    # A FLAG AND A MISSING IMPLEMENTATION ARE NOT THE SAME SENTENCE (2026-09-20). Every skipped
    # rung printed as "off", so chain 56 reported "(launchpad_curve/v4/v4_multihop off)" for three
    # rungs that are NOT flags: 56 has no V4 deployment addresses and the launchpad curve is gated
    # to 4663 in code. A reader went looking for a config fix that does not exist. "off" now means
    # a switch someone can flip; "unavailable on this chain" means a build.
    ran = [r["rung"] for r in rungs if not r.get("skipped")]
    off = [r["rung"] for r in rungs if r.get("skipped") and not r.get("unavailable")]
    unavailable = [r["rung"] for r in rungs if r.get("unavailable")]
    detail = "no route on %s" % ("/".join(ran) or "no rung")
    if off:
        detail += " (%s off)" % "/".join(off)
    if unavailable:
        detail += " (%s unavailable on this chain)" % "/".join(unavailable)
    return "no_route", detail


def _rung_ladder_chains(cfg):
    """Chains that run the on-chain rung ladder (v2 / v4 / v4_multihop).

    Falls back to the pricing list only when the dedicated setting is absent, so an older
    deployment keeps its behaviour and a newer one can retire a pricing source without silently
    turning off the rungs that actually find routes.
    """
    chains = getattr(cfg, "MEMECOIN_EVM_RUNG_LADDER_CHAINS", None)
    if chains:
        return chains
    return getattr(cfg, "MEMECOIN_DEX_BLIND_CHAINS", set())


def _entry_tp_pct(chain):
    """The take-profit an entry on this chain would actually be frozen with.

    NOT `MEMECOIN_REAL_TP_PCT`. That global is 0.0 in production, so a take-profit-relative gate
    reading it would silently fall back to its absolute ceiling on every route and the relative
    half would never run -- which is exactly the state this function was written to fix.

    The real number lives in the funded policy: `resolve_funded_policy` returns tp_pct=25.0 for
    4663 under `follow-30-50-v4`, and it is UNIFORM across nano/micro/small/mid, so the coin band
    is not needed here and the venue seam does not have to be threaded with a classification it
    never sees. If that stops being true -- a band-dependent take-profit -- this must take the band
    rather than keep guessing, and the assertion to add is that the bands disagree.

    Returns 0.0 when the policy cannot be resolved, which routes the caller to its absolute
    ceiling rather than to a fabricated target.
    """
    try:
        from sniper.services.memecoin_funded_policy import resolve_funded_policy
        return float(resolve_funded_policy(
            coin_band="micro", network_id=str(chain)).tp_pct or 0.0)
    except Exception:
        return 0.0


UNMEASURED = "round_trip_unmeasured"

# HOW MUCH OF THE LADDER'S DEADLINE THE BOUNDED REVERSE GIVES BACK (E4, 2026-09-23). The reverse
# must fail before the ladder stops waiting, or bounding it buys nothing: the rung would still be
# recorded `ladder_deadline` and the forward quote still discarded. 50 ms is the room for the
# rung to return and the ladder's `_wait` to observe it.
_REVERSE_DEADLINE_MARGIN_S = 0.05


def _v4_same_pool_reverse(chain, quote, mint, amount_out, rpc_url, http=None):
    """Sell `amount_out` back through the EXACT pool the v4 hit used, or None.

    THE SAME POOL IS THE WHOLE POINT. Re-running discovery in the sell direction would pick the
    BEST pool to sell into, which is not the pool we would be holding a bag in. BUDDY (2026-09-06)
    is the case: the v4 rung hit through decoy pool 0x5bb3337725 (fee 52760, no hook) while the
    real hook-0xe5e702 pool also quoted, and a reverse quote that re-discovered would have priced
    the exit through the real pool and made the decoy look fine. First exact exit mark 4s after
    fill: -61%.
    """
    pool_id = (quote.get("route") or {}).get("pool_id")
    if not pool_id:
        return None
    try:
        from sniper.execution.evm_v4_quote import _pool_key, _quote_single
        key = _pool_key(str(chain), str(pool_id), rpc_url, http=http)
        if not key:
            return None
        coin_is_c1 = str(key["currency1"]).lower() == str(mint).lower()
        # entry bought the coin; the reverse sells it, so the direction flips
        return _quote_single(str(chain), key, not coin_is_c1, int(amount_out), rpc_url, http=http)
    except Exception:
        return None


def freshest_price_age_s(now, selected_at, prepared):
    """Seconds since the FRESHER of the memo's two prices -- the live selection (`selected_at`) and
    the warmer's preparation (`prepared[0]`) -- or None when it holds neither. The early quote may
    use either, so the newer one is the memo's age."""
    stamps = [t for t in (selected_at, prepared[0] if prepared is not None else None)
              if t is not None]
    return round(now - max(stamps), 3) if stamps else None


def _round_trip_for_hit(rung, quote, *, chain, sell_token, buy_token, amount_atomic,
                        taker, rpc_url, http, slippage_bps, reverse_fns):
    """(round_trip_pct, None) for one entry hit, or (None, reason) when it cannot be measured.

    ONE EXTRA QUOTER CALL PER HIT, and only on entry. `round_trip_pct` is what the clip loses
    buying in and selling straight back out, the same number the multihop and Pons rungs already
    report, so every rung is finally judged on one comparable quantity instead of on its position
    in a fixed priority list.

    Evidence (4663, 2026-08-23..09-06): of 123 funded fills with an exact exit quote inside 15s,
    12 were already at or below -30% on that first quote, for -$571. TEN of the twelve came from
    the rungs that measured nothing -- v4 single-hop 4 (-$267), v3 2 (-$93), relay 4 (-$45) --
    while the measured multihop rung contributed one, from before its budget refusal shipped.
    """
    try:
        amount_out = int(quote.get("buyAmount") or 0)
        amount_in = int(quote.get("sellAmount") or amount_atomic or 0)
    except (TypeError, ValueError):
        return None, "%s:%s" % (UNMEASURED, rung)
    if amount_out <= 0 or amount_in <= 0:
        return None, "%s:%s" % (UNMEASURED, rung)
    back = None
    if rung in ("v4", "v4_source"):
        back = _v4_same_pool_reverse(chain, quote, buy_token, amount_out, rpc_url, http=http)
    else:
        reverse = reverse_fns.get(rung)
        if reverse is not None:
            try:
                # the SAME rung, in the sell direction, on the bag the entry would have bought
                rq = reverse(buy_token, sell_token, amount_out)
                back = int((rq or {}).get("buyAmount") or 0) or None
            except Exception:
                back = None
    if not back:
        return None, "%s:%s" % (UNMEASURED, rung)
    return 100.0 * (float(back) / float(amount_in) - 1.0), None


def _http_summary(http, top=3):
    """`n=<calls> ms=<summed rtt> slowest=<label>:<ms>/<outcome>,...` for one rung's http tap."""
    rows = sorted((h for h in http or () if isinstance(h, dict)),
                  key=lambda h: -float(h.get("rtt_ms") or 0.0))
    return "n=%d ms=%.0f slowest=%s" % (
        len(rows), sum(float(h.get("rtt_ms") or 0.0) for h in rows),
        ",".join("%s:%.0f/%s" % (h.get("label"), float(h.get("rtt_ms") or 0.0), h.get("outcome"))
                 for h in rows[:top]) or "-")


def _log_cut_rung_when_done(future, *, chain, run_id, name, cut, winner=None):
    """ONE runner-log line when a rung the ladder already cut (`ladder_grace` or `ladder_deadline`)
    finally answers (2026-09-28, Alpha). The cut record carries only the cut time: the rung's real
    finish time and what it spent it on were lost. On 8453 v4_multihop was cut at the grace on 14 of
    15 ladders and nothing said why.

    Measurement only. The ladder has already decided and returned, and it never waits for this: the
    callback runs on the rung's own pool thread when the rung ends. (If the rung ended between the
    cut and the attach, it runs once on the ladder thread; that costs one log call.) It writes no
    database row and never touches the trace or `on_rung`: the attempt row may already be
    persisted. WARNING level, because INFO does not reach logs/memecoin_runner.log. The line carries
    `run_id`, which is stamped on this rung's cut record in the attempt's trace_json, so it joins to
    the attempt exactly. `finish_ms` is the rung's perf_counter time from its own start; all rungs
    start together with the ladder. It never raises into the rung's thread."""
    def done(f):
        try:
            if f.cancelled():
                finish, outcome, rpc_n, http = None, "cancelled", 0, ()
            else:
                result, blind_reason, finish, reason, calls, http = f.result()
                route = (result.get("route") or {}) if isinstance(result, dict) else {}
                pct = route.get("round_trip_pct")
                outcome = ("hit rt=%.2f" % float(pct) if result is not None and pct is not None
                           else "hit unmeasured" if result is not None
                           else "miss blind=%s reason=%s" % (blind_reason, reason))
                if winner is not None and isinstance(result, dict):
                    # A SOURCE-ROUTE WIN AGAINST THIS ROUTE (2026-10-15, Alpha): what this rung
                    # would have bought, and how far the winner's output sits from it.
                    w_name, w_out = winner
                    out = int(result.get("buyAmount") or 0)
                    delta = (100.0 * (float(w_out) / out - 1.0)) if out > 0 and w_out else None
                    outcome += " out=%d winner=%s winner_out=%s winner_vs_this_pct=%s" % (
                        out, w_name, w_out, None if delta is None else "%.3f" % delta)
                rpc_n = len(calls or ())
            from datetime import datetime, timezone
            logger.warning(
                "venue ladder cut rung finished chain=%s run_id=%s rung=%s cut=%s finish_ms=%s "
                "outcome=%s rpc=%d http=%s at=%s", chain, run_id, name, cut,
                "%.0f" % finish if finish is not None else None, outcome, rpc_n,
                _http_summary(http), datetime.now(timezone.utc).isoformat())
        except Exception:
            logger.warning("venue ladder cut rung log failed rung=%s run_id=%s", name, run_id,
                           exc_info=True)
    try:
        future.add_done_callback(done)
    except Exception:
        logger.warning("venue ladder cut rung callback not attached rung=%s", name, exc_info=True)


def take_rungs(local):
    """The rung records this thread's ladder calls left on `local` since the last take, cleared.
    The safety sell probe takes its own: the ladder records on the calling thread (2026-10-07)."""
    rungs = getattr(local, "rungs", None) or []
    local.rungs = []
    return rungs


def rung_record(name, hit, *, run_id, skipped=False, blind_reason=None, started_at=None,
                duration_ms=None, reason=None, round_trip_pct=None, unavailable=False,
                calls=None, http=None, clock=None, pools=None, buy_out=None):
    """The ladder's per-rung ledger record (extracted from `venue_quote_with_v2_fallback._rung`).

    `http` (2026-09-27, P4) is the rung's PROVIDER HTTP tap: label, host only, RTT, outcome, from
    `evm_wallet.http_tap`. Kept apart from `calls`, which stays JSON-RPC payloads only so the
    replay that reads it is unchanged; absent rather than empty when the rung made none."""
    clock = clock or _time.perf_counter
    record = {"rung": name, "hit": bool(hit), "skipped": bool(skipped),
              # `unavailable` is a SUBSET of `skipped`, never an alternative to it: an older
              # reader that only knows `skipped` keeps its exact meaning.
              "unavailable": bool(unavailable),
              "blind_reason": blind_reason,
              # WHY a rung that COULD read still refused (2026-09-05). Separate from
              # `blind_reason` on purpose: blindness re-enters the requote; a reason is
              # only the ledger's answer to "which gate". Present only when the route
              # returned a third tuple element.
              **({"reason": reason} if reason is not None else {}),
              # WHAT THE RUNG COST, so the trace explains WHICH won and why (2026-09-06).
              **({"round_trip_pct": round_trip_pct} if round_trip_pct is not None else {}),
              # EVERY POOL THE V4 RUNG MEASURED, when its winner failed the round-trip budget
              # (2026-10-08, KLN): pool_id, fee, hooks, buy_out, rt_pct, outcome.
              **({"pools": pools} if pools else {}),
              # WHAT A HIT WOULD HAVE BOUGHT, on a ladder that ran a source-route rung only
              # (2026-10-15): the trace then compares the source pool with the provider routes.
              **({"buy_out": str(buy_out)} if buy_out is not None else {}),
              # WHAT THE RUNG SENT (2026-09-24). Absent rather than empty when the rung
              # made no call at all, so "asked nothing" and "asked and got nothing" stay
              # different rows.
              **({"calls": calls} if calls else {}),
              # WHICH LADDER RUN produced this record (2026-09-02). `_record_rung` APPENDS
              # and only `quote_at_snapshot` resets the list, so a caller that runs the
              # ladder twice — the entry probe and the exit sweep both do — leaves one
              # ledger holding the UNION of several runs. Measured on the 14d 4663 tape:
              # 13 of 76 `evm.quote_initial` events carry more than one `hit`, which is
              # impossible in a single run because a hit at priority N stops the scan and
              # stamps everything below it `priority_resolved` at 0.0ms. Two events carry a
              # multi-second ledger against `quote_ms=1`: a stale ledger attached to an
              # attempt that never quoted.
              #
              # The durations were always real. What was missing is WHICH RUN they belong
              # to, and without it a per-rung latency figure cannot be attributed to the
              # entry it supposedly explains. Group by this before reading any rung timing.
              "run_id": run_id,
              "duration_ms": (duration_ms if duration_ms is not None else
                              (max(0.0, clock() - started_at) * 1000.0
                               if started_at is not None else 0.0))}
    if http:
        record["http"] = http
    return record


def venue_quote_with_v2_fallback(primary_fn, *, chain, quote_token, taker, http=None,
                                 v2_quote_fn=None, enabled=None, v4_quote_fn=None,
                                 pons_v2_quote_fn=None, relay_quote_fn=None, acct=None,
                                 rpc_url=None, on_rung=None, clock=None,
                                 relay_output_guard_verified=False):
    """Wrap a venue quote fn with the V2 Router02 fallback on DexScreener-blind chains.

    2026-07-16 (theveeman 0xf056…): fresh 4663 micros are born on V2-style pools (the VIM
    precedent) — the V3-only quote refused both the pre-entry sell-route check and the entry
    quote for coins the EXIT path could already sell via V2. Same verified Router02 + immutable
    connectors already trusted with real sells. BUYS (sell side == the chain quote token) zero
    the fee-on-transfer input buffer — the quote token does not tax; SELLS keep the default.
    Fires only when: the primary direct quote returned None, chain is DexScreener-blind, and
    MEMECOIN_EVM_V2_ENTRY_ENABLED. Fail-closed straight through (None stays None).

    `on_rung(record)` receives one dict per rung — `{"rung", "hit", "skipped", "blind_reason"}` —
    so the caller can tell "the token has no route" from "a rung could not read". Without it the
    three rungs collapse into one bare None and the refusal is unattributable forever. It is pure
    instrumentation: an on_rung that raises can never change a quote."""
    from sniper.settings import memecoin as cfg
    clock = clock or _time.perf_counter

    def _rung(name, hit, *, run_id, skipped=False, blind_reason=None, started_at=None,
              duration_ms=None, reason=None, round_trip_pct=None, unavailable=False,
              calls=None, http=None, pools=None, buy_out=None):
        if on_rung is None:
            return
        try:
            on_rung(rung_record(name, hit, run_id=run_id, skipped=skipped,
                                blind_reason=blind_reason, started_at=started_at,
                                duration_ms=duration_ms, reason=reason,
                                round_trip_pct=round_trip_pct, unavailable=unavailable,
                                calls=calls, http=http, clock=clock, pools=pools,
                                buy_out=buy_out))
        except Exception:
            pass

    def combined(sell_token, buy_token, amount_atomic, *, slippage_bps=None, sell_plausible_fn=None,
                 only_rung=None, exclude_rungs=(), first_available=False, ladder_started_fn=None,
                 route_hint=None, exit_hint=None):
        # One id per ladder run, stamped on every record this run emits. Pure instrumentation:
        # it cannot change a quote, and a consumer that ignores it reads exactly what it read
        # before.
        run_id = uuid.uuid4().hex[:12]
        on = cfg.MEMECOIN_EVM_V2_ENTRY_ENABLED if enabled is None else enabled
        measured_ladder = bool(getattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", False))
        reverse_fns = {}
        rung_pools = {}      # rung -> the pools its over-budget fallback measured (the trace)
        rebuild_fns = {}     # rung -> build the executable quote on one named pool

        def _v4_budget_fallback(name, q, pct, *, amount_in):
            """The executable quote on the next v4 pool whose OWN round trip is within budget, or None.

            KLN (chain 1, 2026-10-08, 0x2cadc4f2): max buy output picked a stale 8% decoy right after
            the source's buy moved the real hooked pool. The decoy's same-pool round trip read -33.5%
            and the entry refused, while the hooked pool (-2.2%) had quoted in the same discovery.
            The candidates are the ones discovery already quoted, in descending output; each costs one
            reverse quote, at most MEMECOIN_EVM_V4_BUDGET_FALLBACK_MAX of them (0 = off). The rebuild
            names that pool, so the executed swap, min_out and the route the copy-premium and gas
            checks read all come from the pool whose round trip passed. Only ever reached after the
            winner failed, so the normal path pays nothing.
            """
            from sniper.execution.evm_v4_multihop import round_trip_within_budget
            route = q.get("route") or {}
            pools = [{"pool_id": route.get("pool_id"), "fee": route.get("pool_fee"), "hooks": None,
                      "buy_out": q.get("buyAmount"), "rt_pct": round(pct, 4), "outcome": "over_budget"}]
            limit = int(getattr(cfg, "MEMECOIN_EVM_V4_BUDGET_FALLBACK_MAX", 0) or 0)
            alternates = list(q.get("_v4_alternates") or ())[:max(limit, 0)]
            rebuild = rebuild_fns.get(name)
            if not alternates or rebuild is None or amount_in <= 0:
                return None
            rung_pools[name] = pools
            for alt in alternates:
                key = alt.get("pool_key") or {}
                row = {"pool_id": alt.get("pool_id"), "fee": key.get("fee"), "hooks": key.get("hooks"),
                       "buy_out": str(alt.get("amount_out")), "rt_pct": None}
                pools.append(row)
                back = _v4_same_pool_reverse(chain, {"route": {"pool_id": alt.get("pool_id")}}, buy_token,
                                             int(alt.get("amount_out") or 0), rpc_url, http=http)
                if not back:
                    row["outcome"] = "unmeasured"
                    continue
                alt_pct = 100.0 * (float(back) / float(amount_in) - 1.0)
                row["rt_pct"] = round(alt_pct, 4)
                if not round_trip_within_budget(alt_pct, _entry_tp_pct(chain), chain=chain):
                    row["outcome"] = "over_budget"
                    continue
                try:
                    built = rebuild(alt)
                except Exception:
                    logger.exception("v4 budget fallback rebuild failed chain=%s pool=%s",
                                     chain, alt.get("pool_id"))
                    built = None
                built_route = (built.get("route") or {}) if isinstance(built, dict) else {}
                # THE BUILT QUOTE MUST BE THE MEASURED ONE: same pool, same output.
                if (not isinstance(built, dict)
                        or str(built_route.get("pool_id") or "").lower() != str(alt.get("pool_id") or "").lower()
                        or str(built.get("buyAmount")) != str(alt.get("amount_out"))):
                    row["outcome"] = "rebuild_failed"
                    continue
                row["outcome"] = "chosen"
                chosen = dict(built)
                chosen.pop("_v4_alternates", None)
                if q.get("_sniper_v4_ranking") is not None and "_sniper_v4_ranking" not in chosen:
                    chosen["_sniper_v4_ranking"] = q["_sniper_v4_ranking"]
                chosen["route"] = dict(built_route, round_trip_pct=alt_pct, round_trip_measured_by=name,
                                       budget_fallback_from=route.get("pool_id"))
                return chosen
            return None

        def _measured(name, quote_fn):
            """Wrap an unmeasured ENTRY rung so its hit carries a round trip and is budget-gated.

            Exits are never measured and never refused on cost: the alternative to an expensive
            exit is a bag we cannot sell at all.
            """
            def run():
                raw = quote_fn()
                # A RUNG MAY ANSWER WITH THE 3-TUPLE FORM `(result, blind_reason, reason)`, and v4
                # does. Measuring only plain dicts let exactly the rung this change exists for slip
                # through unmeasured -- unwrap, measure the result, and put the tuple back.
                # A RUNG ANSWERS AS A DICT, A 2-TUPLE `(result, blind_reason)` OR A 3-TUPLE
                # `(result, blind_reason, reason)`. v4 -- the rung this change exists for -- emits
                # the 2-TUPLE, so accepting only 3 let it through unmeasured while every test that
                # used a bare dict looked fine.
                tail = None
                q = raw
                if isinstance(raw, tuple):
                    if not raw or not isinstance(raw[0], dict):
                        return raw
                    q, tail = raw[0], raw[1:]
                if not (measured_ladder and is_buy) or not isinstance(q, dict):
                    return raw
                # COPY, NEVER MUTATE THE RUNG'S OWN OBJECT. The executor and the capacity-probe
                # cache consume what the LADDER returns, so the annotation still reaches
                # `evm_route_json`; only the rung's dict is left alone. A rung that hands back a
                # module-level object -- which several fixtures do -- would otherwise carry this
                # run's round trip into the next test in the same process.
                q = dict(q)
                q["route"] = dict(q.get("route") or {})

                def _wrap(value):
                    return value if tail is None or not isinstance(value, dict) else (value,) + tail
                from sniper.execution.evm_v4_multihop import round_trip_within_budget
                route = q.setdefault("route", {})
                pct, unmeasured = _round_trip_for_hit(
                    name, q, chain=chain, sell_token=sell_token, buy_token=buy_token,
                    amount_atomic=amount_atomic, taker=taker, rpc_url=rpc_url, http=http,
                    slippage_bps=slippage_bps, reverse_fns=reverse_fns)
                if unmeasured is not None:
                    # NEVER SELECTED WHILE A MEASURED HIT EXISTS, and refused when it is alone --
                    # the entry window requotes a transient already, so a miss costs a pass and
                    # not a blind fill.
                    route["round_trip_unmeasured"] = True
                    route["round_trip_measured_by"] = name
                    # REFUSED, NOT DEMOTED. Refusing satisfies both halves of the rule at once: it
                    # cannot outrank a measured hit, and alone it does not fill. The entry window
                    # requotes a transient already, so a reverse quote that failed once costs a
                    # pass rather than a blind fill. Relay uses its own bounded reverse below.
                    return (None, (tail[0] if tail else None), unmeasured)
                route["round_trip_pct"] = pct
                route["round_trip_measured_by"] = name
                if (name == "v4" and not round_trip_within_budget(pct, _entry_tp_pct(chain), chain=chain)):
                    chosen = _v4_budget_fallback(name, q, pct, amount_in=int(q.get("sellAmount") or amount_atomic))
                    if chosen is not None:
                        return _wrap(chosen)
                if not round_trip_within_budget(pct, _entry_tp_pct(chain), chain=chain):
                    # THE SAME MARGINAL CLASS AS THE MULTIHOP RUNG (2026-09-22, Rizo 8fd42cb4).
                    # v2 read -10.05% against the 10% budget and the miss was one-shot, because
                    # only the multihop block below named a marginal miss. The measured cost is
                    # two impacts read off one untouched pool, so it moves with the pool; within
                    # the margin the retry lane may re-ask, beyond it the refusal stays one-shot.
                    margin = float(getattr(
                        cfg, "MEMECOIN_EVM_V4_MULTIHOP_BUDGET_RETRY_MARGIN_PCT", 0.0) or 0.0)
                    marginal = margin > 0 and round_trip_within_budget(
                        pct, _entry_tp_pct(chain), chain=chain, margin_pct=margin)
                    return (None, (tail[0] if tail else None), "%s:%s" % (
                        "round_trip_over_budget_marginal" if marginal
                        else "round_trip_over_budget", pct))
                return _wrap(q)
            return run
        reverse_fns["v3"] = lambda s_t, b_t, amt: primary_fn(
            s_t, b_t, amt, slippage_bps=slippage_bps)
        routes = [("v3", _measured("v3", lambda: primary_fn(
            sell_token, buy_token, amount_atomic, slippage_bps=slippage_bps)))]
        skipped = []
        # NOT BUILT FOR THIS CHAIN, as opposed to switched off for it. Kept apart so the refusal
        # can say which, because only one of the two is something an operator can change.
        unavailable = []
        relay_enabled = bool(getattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False)) and str(chain) == "4663"
        # HOISTED out of the v2 branch below (2026-09-02). It used to be defined only inside the
        # `else`, so with v2 entry disabled or a non-blind chain it was UNDEFINED — and the relay
        # entry gate that reads it raised NameError on the funded quote path, which surfaced as a
        # typed `provider_rpc_transport` miss. A direction is a property of the call, not of one
        # branch's configuration.
        is_buy = (quote_token or "").lower() == str(sell_token).lower()
        # THE RUNG LADDER HAS ITS OWN SETTING (2026-09-04). This read
        # `MEMECOIN_DEX_BLIND_CHAINS`, which answers "which chains need a price source other than
        # DexScreener" -- a different question. Clearing that pricing list to retire GeckoTerminal
        # disabled the V4 rung on 4663, where every pool is V4, and 26 entries refused
        # `no_route_all_venues` in 84 minutes against pools that were quotable throughout.
        v2_v4_ladder_ok = bool(on) and str(chain) in _rung_ladder_chains(cfg)
        # V4 HAS ITS OWN INDEPENDENT SWITCH TOO (2026-09-16, Arc build). The ladder above gates v2
        # AND v4 as one pair -- correct for a chain that needs BOTH, wrong for one that needs
        # neither the rung ladder's pricing-source history nor its V2 pools. Membership here opens
        # v4 without opening v2; membership in the ladder above still opens both, unchanged.
        v4_rung_ok = v2_v4_ladder_ok or str(chain) in getattr(cfg, "MEMECOIN_EVM_V4_RUNG_CHAINS", ())
        if not v2_v4_ladder_ok:
            skipped.append("v2")
        else:
            v2 = v2_quote_fn
            if v2 is None:
                from sniper.execution.evm_v2_exit_quote import dex_v2_exit_quote
                v2 = dex_v2_exit_quote
            # THE COST MEASUREMENT IS NOT THE EXIT'S SAFETY FLOOR (2026-09-13, operator, 红尘飞
            # 7ba1dc76). tax_buffer_bps=None shaved the sell-back by MEMECOIN_EVM_MAX_TAX_PCT (15%),
            # the worst-case tax an EXIT must survive, and the round trip counted it as cost: every
            # V2 coin measured >= 15% against a 10% budget. All 20 BSC cost refusals since 09-06
            # read -16..-20% (15 + 1..5 real; the refused coins' real taxes were 0-2% a side), and
            # BSC filled 2 entries in that time. The measurement takes a realistic allowance
            # instead; the exit keeps the 15% floor.
            from sniper.settings.memecoin import MEMECOIN_EVM_ROUNDTRIP_TAX_ALLOWANCE_PCT as _rt_tax
            reverse_fns["v2"] = lambda s_t, b_t, amt: v2(
                chain, s_t, b_t, amt, taker, http=http, slippage_bps=slippage_bps,
                tax_buffer_bps=int(round(float(_rt_tax) * 100)))

            def _v2_with_blindness():
                """A V2 read that could not reach the chain must reach the ladder as BLINDNESS.

                Before 2026-09-08 this rung answered a bare dict-or-None, so `blind_reason` was
                always None and a transport failure was written as a hard `no_route` that the
                re-quote lane treats as terminal. 0x7997a69c on chain 56 was refused that way
                against a $30,852 pool twenty hours old. `blind_out` is a fresh dict per call
                because the rungs run in a ThreadPoolExecutor.
                """
                blind_out = {}
                # ARGUMENTS PRESERVED EXACTLY: a BUY sells the quote token, which carries no
                # transfer tax, so the input must NOT be reduced by the tax buffer (tax_buffer_bps=0).
                # An exit keeps None so the configured max-tax policy applies.
                kwargs = dict(http=http, slippage_bps=slippage_bps,
                              tax_buffer_bps=(0 if is_buy else None))
                try:
                    result = v2(chain, sell_token, buy_token, amount_atomic, taker,
                                blind_out=blind_out, **kwargs)
                except TypeError:
                    # an injected test seam may not accept blind_out; behaviour unchanged
                    result = v2(chain, sell_token, buy_token, amount_atomic, taker, **kwargs)
                blind = blind_out.get("reason") if result is None else None
                # The raw error rides in the rung ledger, so the next "v2 could not read" names its
                # cause. 19 of 108 failed 4663 ladders since 09-11 were v2-blind with no detail.
                return result, blind, (("read error: %s" % blind_out.get("detail"))[:200]
                                       if blind and blind_out.get("detail") else None)

            routes.append(("v2", _measured("v2", _v2_with_blindness)))
            if pons_v2_quote_fn is not None:
                def _quote_pons_v2():
                    from sniper.execution.evm_v4_multihop import round_trip_within_budget
                    # WHY THE RUNG DECLINED, WHEN IT HAS A NAME (2026-09-24). `pons_v2` reported
                    # hit=False with no blind reason and no reason on 14 of 18 runs on 4663 since
                    # 09-23; all 7 distinct mints behind those misses read `graduated=1` live. The
                    # third tuple element is the REASON channel, never `blind_reason`: a graduated
                    # curve is a decided fact about the venue, and calling it blindness would
                    # publish `provider_*` for a refusal that is correct.
                    miss = {}
                    try:
                        q = pons_v2_quote_fn(
                            chain, sell_token, buy_token, amount_atomic, taker, http=http,
                            slippage_bps=slippage_bps, rpc_url=rpc_url, miss_out=miss)
                    except TypeError:
                        # An injected quote_fn from before this keyword existed (tests, and any
                        # caller passing its own): keep the old contract rather than fail the rung.
                        q = pons_v2_quote_fn(
                            chain, sell_token, buy_token, amount_atomic, taker, http=http,
                            slippage_bps=slippage_bps, rpc_url=rpc_url)
                    # SAME GATE, SAME BUDGET, SAME HELPER AS THE MULTIHOP RUNG (2026-09-05). A Pons
                    # curve's round trip is a pure function of the state the quote already read, so
                    # this costs no extra call -- and until now the rung admitted a funded entry
                    # with no measure of what a clip costs to buy and sell back. Funded history to
                    # today: 13 positions, -$77.50 net, 4 stops for -$309.
                    #
                    # ENTRY ONLY. The sell quote carries no `round_trip_pct` by construction and
                    # must never be refused on cost: the alternative to an expensive exit is a bag
                    # we cannot sell at all. `is_buy` is what separates them here, because one
                    # closure serves both directions.
                    # The gate applies only to a quote that MEASURED its round trip (the key is
                    # present). A measured-but-unpriceable curve (None) fails closed as an
                    # unreadable cost; a quote with no measurement at all (a custom quote_fn, an
                    # older shape) is admitted exactly as before this gate existed.
                    route = (q.get("route") or {}) if isinstance(q, dict) else {}
                    if is_buy and q is not None and "round_trip_pct" in route \
                            and not round_trip_within_budget(route.get("round_trip_pct"),
                                                             _entry_tp_pct(chain), chain=chain):
                        from sniper.execution.evm_pons_v2_quote import test_allows
                        if not test_allows(chain, buy_token, route.get("round_trip_pct")):
                            return (None, None, "round_trip_over_budget:%s" % (
                                route.get("round_trip_pct"),))
                    if q is None and miss.get("reason"):
                        return (None, None, miss["reason"])
                    return q
                routes.append(("pons_v2", _quote_pons_v2))
        # V4 IS GATED INDEPENDENTLY OF v2 (see v4_rung_ok above), so this whole block sits outside
        # the v2 `if/else` -- a chain can try v4 with v2 skipped, same as it always could try v2
        # with v4 skipped.
        #
        # A CHAIN WITH NO V4 DEPLOYMENT SKIPS THE RUNG, it does not fail it (2026-09-06).
        # Chain 56 is on the rung ladder for v2 and v3 but has no V4 addresses, and without this
        # the v4 and v4_multihop rungs both attempted a read and reported `rpc_transport` —
        # a BLIND reason. `classify_quote_miss` reports the blind rung first, so a coin that
        # genuinely has no route would have been attributed to a provider fault instead, and a
        # real transport problem would have been indistinguishable from this noise. Observed on
        # all three coins of the first BSC canary.
        from sniper.execution.evm_v4_quote import V4 as _V4_DEPLOYMENTS
        if not v4_rung_ok:
            skipped.extend(("v4", "v4_multihop"))          # a chain list an operator can change
        elif str(chain) not in _V4_DEPLOYMENTS:
            unavailable.extend(("v4", "v4_multihop"))      # no addresses: a build, not a flag
        elif not cfg.MEMECOIN_EVM_V4_ENABLED:
            skipped.append("v4")
        else:
            v4 = v4_quote_fn
            default_v4 = v4 is None
            if default_v4:
                from sniper.execution.evm_v4_quote import dex_v4_quote
                v4 = dex_v4_quote
            from sniper.execution.geckoterminal_quote import cooldown_active
            from sniper.execution.evm_v4_quote import begin_quote as _v4_begin
            gt_cold = cooldown_active(funded=True)

            def _quote_v4():
                _v4_begin()
                kwargs = dict(http=http, slippage_bps=slippage_bps, acct=acct,
                              rpc_url=rpc_url)
                if default_v4:
                    kwargs["gt_http"] = http
                result = v4(chain, sell_token, buy_token, amount_atomic, taker, **kwargs)
                from sniper.execution.evm_v4_quote import (
                    last_discovery_observed, last_provider_blind)
                blind_reason = last_provider_blind()
                if result is None and blind_reason is None and gt_cold and not last_discovery_observed():
                    blind_reason = DISCOVERY_COOLDOWN
                return result, blind_reason

            routes.append(("v4", _measured("v4", _quote_v4)))
            # The over-budget fallback rebuilds the executable quote on ONE named pool: no discovery.
            rebuild_fns["v4"] = lambda alt: v4(
                chain, sell_token, buy_token, amount_atomic, taker, quote_override=alt, http=http,
                slippage_bps=slippage_bps, acct=acct, rpc_url=rpc_url)
            if getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False):
                def _quote_v4_multihop():
                    from sniper.execution.evm_v4_multihop import dex_v4_multihop_quote
                    from sniper.execution.evm_v4_multihop import (
                        round_trip_within_budget)
                    from sniper.execution.evm_v4_quote import last_discovery_observed
                    q = dex_v4_multihop_quote(
                        chain, sell_token, buy_token, amount_atomic, taker, http=http,
                        slippage_bps=slippage_bps, acct=acct, rpc_url=rpc_url)
                    # THE MEASURED COST DECIDES, NOT THE DEPTH PROXY (2026-09-04). The rung
                    # already quotes all four legs and reports what a clip loses buying in and
                    # selling straight back out; nothing read it, so depth stood in for a cost
                    # we measure exactly -- and depth is wrong in BOTH directions here, admitting
                    # a deep pool at 90% and rejecting a shallow honest one. Judged against the
                    # take-profit this coin would aim at, because that is what makes FRIES'
                    # -24.8% against a 25% TP a refusal rather than merely expensive.
                    #
                    # ENTRY ONLY, and deliberately at the CALLER: the quote builder stays a pure
                    # quote so its mechanics tests do not encode a cost policy, and the EXIT rung
                    # is a different function that must never be blocked by what it costs -- the
                    # alternative to an expensive exit is a bag we cannot sell at all.
                    # BUYS ONLY (2026-10-07, operator yes): a sell quote carries no round trip,
                    # round_trip_within_budget(None) is False, and this discarded every multihop
                    # SELL in the ladder -- the chain-1 safety probe refused every multihop coin.
                    if is_buy and q is not None and not round_trip_within_budget(
                            (q.get("route") or {}).get("round_trip_pct"),
                            _entry_tp_pct(chain), chain=chain):
                        measured = (q.get("route") or {}).get("round_trip_pct")
                        # MARGINAL MISS vs A PRICED REFUSAL (2026-09-17). The measured cost moves
                        # with the pool: SKIBIDI read -15.09% against the 15% budget and -13.35%
                        # thirteen minutes later, LuluDior -15.40% then -13.8%. Within the margin
                        # the runner may re-ask; beyond it the refusal stays one-shot.
                        margin = float(getattr(
                            cfg, "MEMECOIN_EVM_V4_MULTIHOP_BUDGET_RETRY_MARGIN_PCT", 0.0) or 0.0)
                        marginal = margin > 0 and round_trip_within_budget(
                            measured, _entry_tp_pct(chain), chain=chain, margin_pct=margin)
                        return (None, None, "%s:%s" % (
                            "round_trip_over_budget_marginal" if marginal
                            else "round_trip_over_budget", measured))
                    if q is None:
                        from sniper.execution.evm_v4_multihop import last_multihop_refusal
                        # BLINDNESS IS NOT ABSENCE, AND THIS RUNG COULD NOT SAY SO (2026-09-08).
                        # The v4 rung above already converts "GeckoTerminal was cold and we saw
                        # no discovery" into blind_reason='rate_limit'; this rung had no such
                        # branch, so its `blind_reason` was ALWAYS None. The ladder only reports
                        # `provider_*` when some rung claims blindness, so a multihop that failed
                        # because its depth providers were rate limited came out as
                        # `no_route_all_venues` — indistinguishable from a coin with no route.
                        # `_pools_with_depth` fails closed when both providers are dark, and
                        # depth is exactly what gates this rung, so it is the rung MOST exposed
                        # to provider cooldown and the one that was silent about it.
                        # qINU 2026-09-08T11:38:02Z aborted `no_route_all_venues` with
                        # GeckoTerminal cooldowns logged at 11:38:00 and 11:38:52, and the same
                        # route quoted cleanly minutes later.
                        note = last_multihop_refusal()
                        blind = multihop_blind_reason(
                            note, gt_cold=gt_cold, discovery_observed=last_discovery_observed())
                        return (None, blind, note)
                    if is_buy:
                        from sniper.execution import evm_source_route as _bridge_src
                        if (_bridge_src.bridge_cache_enabled(chain)
                                or _bridge_src.prefix_enabled(chain)):
                            _bridge_src.remember_bridge(chain, q.get("route") or {})
                    # WHICH DISCOVERY DECIDED rides on the rung's trace record (store | provider).
                    found_by = (q.get("route") or {}).get("discovery")
                    store_refusal = (q.get("route") or {}).get("store_refusal")
                    if found_by and store_refusal:
                        return (q, None, "discovery:%s store_refusal=%s" % (found_by, store_refusal))
                    return (q, None, "discovery:%s" % found_by) if found_by else q
                routes.append(("v4_multihop", _quote_v4_multihop))
            else:
                skipped.append("v4_multihop")
            # THE SOURCE'S OWN POOL (2026-10-15, operator "fill fastest"; evm_source_route). The pool
            # the source bought through, from the event's receipt decode, quoted BESIDE today's
            # rungs with no discovery. A key we cannot resolve locally, a pool without the coin, or
            # one that does not quote is a MISS and the other rungs decide; over budget is a miss
            # too (the measured gate below), so a worse price never wins on speed.
            from sniper.execution import evm_source_route as _src
            hint_pool = ((route_hint if isinstance(route_hint, (list, tuple)) else str(route_hint).lower())
                         if route_hint and is_buy and _src.enabled(chain) else None)
            if hint_pool is not None:
                hint_mint = str(buy_token).lower()
                cash_side = {"0x" + "0" * 40, str(quote_token or "").lower()}

                def _quote_v4_source():
                    source_pool, key = _src.hinted_pool_key(chain, hint_pool, hint_mint, cash_side)
                    if key is None:
                        return (None, None, "source_key_unknown")
                    if _src.other_currency(key, hint_mint) not in cash_side:
                        return (None, None, "source_pool_not_cash_paired")
                    from sniper.execution.evm_v4_quote import PinnedQuoteBlind, v4_pinned_pool_quote
                    try:
                        quote = v4_pinned_pool_quote(chain, source_pool, key, sell_token, buy_token,
                                                     amount_atomic, rpc_url=rpc_url, http=http)
                    except PinnedQuoteBlind as exc:
                        return (None, exc.failure_type, "source_quote_blind")
                    if quote is None:
                        _src.forget(chain, hint_mint, source_pool)
                        return (None, None, "source_pool_no_quote")
                    built = v4(chain, sell_token, buy_token, amount_atomic, taker,
                               quote_override=quote, http=http, slippage_bps=slippage_bps,
                               acct=acct, rpc_url=rpc_url)
                    if built is None:
                        return (None, None, "source_build_failed")
                    return (built, None, "source_pool")

                routes.append(("v4_source", _measured("v4_source", _quote_v4_source)))
                if (getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False)
                        and only_rung != "v4_source"
                        and not isinstance(hint_pool, (list, tuple))):
                    def _quote_v4_multihop_source():
                        from sniper.execution.evm_v4_multihop import (
                            dex_v4_multihop_quote, last_multihop_refusal, last_pinned_refusal,
                            round_trip_within_budget)
                        key = _src.local_key(chain, hint_pool, hint_mint)
                        if key is None:
                            return (None, None, "source_key_unknown")
                        bridge = _src.other_currency(key, hint_mint)
                        if bridge in cash_side:
                            return (None, None, "source_pool_cash_paired")
                        # THE BRIDGE CACHE (2026-10-15, MEMECOIN_EVM_BRIDGE_CACHE_CHAINS): the
                        # cash->bridge pool that last passed the budget for this bridge currency
                        # is measured first, under the same budget; a miss falls through to the
                        # full bridge search. An answered miss drops it, a blind one keeps it.
                        bridge_kw = {}
                        # Under MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS the pin is its own rung beside
                        # this one (v4_multihop_pinned), so this rung runs the discovery alone.
                        cached = (_src.bridge_for(chain, bridge)
                                  if _src.bridge_cache_enabled(chain)
                                  and not _src.prefix_enabled(chain) else None)
                        if cached is not None:
                            tp_pct = _entry_tp_pct(chain)
                            bridge_kw = {"bridge_hint": (bridge,) + cached,
                                         "pinned_accept_fn": lambda pct: round_trip_within_budget(
                                             pct, tp_pct, chain=chain)}
                        q = dex_v4_multihop_quote(
                            chain, sell_token, buy_token, amount_atomic, taker, http=http,
                            slippage_bps=slippage_bps, acct=acct, rpc_url=rpc_url,
                            coin_pool_hint=hint_pool, **bridge_kw)
                        pinned_note = last_pinned_refusal() if bridge_kw else None
                        if pinned_note is not None and multihop_blind_reason(
                                pinned_note, gt_cold=False, discovery_observed=True) is None:
                            _src.forget_bridge(chain, bridge)
                        if q is None:
                            note = last_multihop_refusal()
                            blind = multihop_blind_reason(note, gt_cold=False,
                                                          discovery_observed=True)
                            if blind is None:
                                _src.forget(chain, hint_mint, hint_pool)
                            return (None, blind, "source:%s" % note)
                        # The same budget as the multihop rung; no marginal retry class here,
                        # because today's multihop rung keeps its own.
                        measured = (q.get("route") or {}).get("round_trip_pct")
                        if not round_trip_within_budget(measured, _entry_tp_pct(chain), chain=chain):
                            return (None, None, "round_trip_over_budget:%s" % measured)
                        if _src.bridge_cache_enabled(chain) or _src.prefix_enabled(chain):
                            _src.remember_bridge(chain, q.get("route") or {})
                        found_by = (q.get("route") or {}).get("discovery")
                        return (q, None, "discovery:%s" % ("pinned" if found_by == "pinned"
                                                           else "source"))
                    routes.append(("v4_multihop_source", _quote_v4_multihop_source))
                    # THE FULL-PREFIX PIN, ITS OWN RUNG (2026-10-15, MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS).
                    # The cached route cash -> ... -> X (1 or 2 legs; X = what the source's pool pairs
                    # the coin with) plus the coin leg, measured as ONE path at this call's amount,
                    # under the same budget. It runs BESIDE the discovery above, never in front of
                    # it: a hit is the first measured answer on a 0-grace chain, a miss costs the
                    # discovery nothing. An answered miss drops the prefix; a blind one keeps it.
                    p_key = (_src.local_key(chain, hint_pool, hint_mint)
                             if _src.prefix_enabled(chain) else None)
                    p_x = _src.other_currency(p_key, hint_mint) if p_key is not None else None
                    p_cached = (_src.prefix_for(chain, p_x)
                                if p_x is not None and p_x not in cash_side else None)
                    if p_cached is not None:
                        def _quote_v4_multihop_pinned(_x=p_x, _cached=p_cached):
                            from sniper.execution.evm_v4_multihop import (
                                dex_v4_multihop_quote, last_multihop_refusal,
                                round_trip_within_budget)
                            tp_pct = _entry_tp_pct(chain)
                            prefix, pid0, depth0 = _cached
                            q = dex_v4_multihop_quote(
                                chain, sell_token, buy_token, amount_atomic, taker, http=http,
                                slippage_bps=slippage_bps, acct=acct, rpc_url=rpc_url,
                                coin_pool_hint=hint_pool, prefix_hint=(_x, prefix, pid0, depth0),
                                pinned_accept_fn=lambda pct: round_trip_within_budget(
                                    pct, tp_pct, chain=chain),
                                pinned_only=True)
                            if q is None:
                                note = last_multihop_refusal()
                                blind = multihop_blind_reason(note, gt_cold=False,
                                                              discovery_observed=True)
                                if blind is None:
                                    _src.forget_bridge(chain, _x)
                                return (None, blind, "pinned:%s" % note)
                            measured = (q.get("route") or {}).get("round_trip_pct")
                            if not round_trip_within_budget(measured, tp_pct, chain=chain):
                                _src.forget_bridge(chain, _x)
                                return (None, None, "round_trip_over_budget:%s" % measured)
                            _src.remember_bridge(chain, q.get("route") or {})
                            return (q, None, "discovery:pinned legs=%d"
                                    % len((q.get("route") or {}).get("legs_path") or ()))
                        routes.append(("v4_multihop_pinned", _quote_v4_multihop_pinned))
            # THE SOURCE'S OWN POOL ON THE SAFETY'S SELL PROBE (2026-10-16, Alpha GO;
            # MEMECOIN_EVM_SAFETY_EXIT_HINT_CHAINS, resolved by the caller from the receipt only).
            # 0078cc00: the buy found the coin through the source pool in 295 ms while the probe fell
            # to Relay at 637 ms. The exit is built from local caches only (the hash-verified key, and
            # the cached cash->bridge pool for a bridged coin); it is a hit only when the built route
            # names the hinted pool, at the full amount. Anything else is a miss and today's rungs
            # decide. The simulation and the verdict are not touched, and nothing is remembered.
            if exit_hint and not is_buy:
                exit_pool = (exit_hint if isinstance(exit_hint, (list, tuple)) else str(exit_hint).lower())
                exit_mint = str(sell_token).lower()
                exit_cash = {"0x" + "0" * 40, str(quote_token or "").lower()}

                def _quote_v4_source_exit():
                    from sniper.execution.evm_v4_launchpad import pool_id_for_key
                    source_pool, key = _src.hinted_pool_key(chain, exit_pool, exit_mint, exit_cash)
                    if key is None:
                        return (None, None, "source_key_unknown")
                    bridge = _src.other_currency(key, exit_mint)
                    if bridge in exit_cash:
                        from sniper.execution.evm_v4_quote import (
                            PinnedQuoteBlind, v4_pinned_pool_quote)
                        try:
                            quote = v4_pinned_pool_quote(chain, source_pool, key, sell_token, buy_token,
                                                         amount_atomic, rpc_url=rpc_url, http=http)
                        except PinnedQuoteBlind as exc:
                            return (None, exc.failure_type, "source_quote_blind")
                        if quote is None:
                            return (None, None, "source_pool_no_quote")
                        built = v4(chain, sell_token, buy_token, amount_atomic, taker,
                                   quote_override=quote, http=http, slippage_bps=slippage_bps,
                                   acct=acct, rpc_url=rpc_url)
                        pool = ((built or {}).get("route") or {}).get("pool_id")
                    else:
                        if not getattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False):
                            return (None, None, "source_multihop_disabled")
                        from sniper.execution.evm_v4_multihop import (
                            DYNAMIC_FEE_FLAG, _fee_ok, _multihop_cash, _pair_of,
                            dex_v4_multihop_exit_quote, last_multihop_refusal)
                        if _src.prefix_enabled(chain):
                            cached_prefix = _src.prefix_for(chain, bridge)
                            if cached_prefix is None:
                                return (None, None, "source_bridge_uncached")
                            prefix, first_pool, _depth = cached_prefix
                        else:
                            cached = _src.bridge_for(chain, bridge)
                            if cached is None:
                                return (None, None, "source_bridge_uncached")
                            first_pool = cached[0]
                            prefix = ((bridge, cached[1]),)
                        if len(prefix) == 1:
                            exit_route = {"bridge": bridge, "bridge_key": prefix[0][1], "coin_key": key}
                        else:
                            try:
                                if (len(prefix) != 2 or prefix[-1][0] != bridge
                                        or pool_id_for_key(prefix[0][1]).lower() != first_pool):
                                    return (None, None, "source_bridge_uncached")
                                previous = _multihop_cash(chain)
                                for i, (currency, prefix_key) in enumerate(prefix):
                                    if (not _fee_ok(prefix_key.get("fee"))
                                            or (i == 0 and prefix_key["fee"] == DYNAMIC_FEE_FLAG)
                                            or not _pair_of(prefix_key, previous, currency)[0]):
                                        return (None, None, "source_bridge_uncached")
                                    pool_id_for_key(prefix_key)
                                    previous = currency
                            except (KeyError, TypeError, ValueError):
                                return (None, None, "source_bridge_uncached")
                            # Replay the cached cash→X prefix plus this receipt's exact coin pool.
                            exit_route = {"bridge": prefix[0][0], "bridge_key": prefix[0][1],
                                          "coin_key": key,
                                          "legs_path": [{"currency": c, "key": k} for c, k in prefix]
                                          + [{"currency": exit_mint, "key": key}]}
                        built = dex_v4_multihop_exit_quote(
                            chain, sell_token, amount_atomic, taker,
                            route=exit_route,
                            http=http, slippage_bps=slippage_bps, acct=acct, rpc_url=rpc_url)
                        if built is None:
                            note = last_multihop_refusal()
                            return (None, multihop_blind_reason(
                                note, gt_cold=False, discovery_observed=True), "source:%s" % note)
                        if built.get("partial_exit"):
                            return (None, None, "source_exit_partial")
                        coin_key = (built.get("route") or {}).get("coin_key")
                        try:
                            pool = pool_id_for_key(coin_key)
                        except Exception:
                            pool = None
                    if built is None:
                        return (None, None, "source_build_failed")
                    if str(pool or "").lower() != source_pool:
                        return (None, None, "source_exit_pool_mismatch")
                    return (built, None, "source_pool")

                routes.append(("v4_source_exit", _quote_v4_source_exit))
        # LAUNCHPAD CURVE (spec 2026-09-17), default OFF, 4663 only. A coin still on its own
        # bonding curve has no V3/V2/V4/relay pool at all -- every other rung is structurally a
        # miss for it, so this rung's own gate is its ONLY gate; there is nothing to race against.
        # The mint-specific decision (is this coin on this launchpad, is it still on its curve, is
        # the counter currency one this call can pay/receive) all live inside
        # `dex_launchpad_curve_quote`/`_exit_quote` themselves and fail closed to None -- this
        # block only decides whether the rung is REACHABLE on this chain at all.
        if bool(getattr(cfg, "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", False)) and str(chain) == "4663":
            def _quote_launchpad_curve():
                from sniper.execution.evm_launchpad_curve import (
                    dex_launchpad_curve_quote, dex_launchpad_curve_exit_quote)
                fn = dex_launchpad_curve_quote if is_buy else dex_launchpad_curve_exit_quote
                return fn(chain, sell_token, buy_token, amount_atomic, taker,
                         http=http, slippage_bps=slippage_bps, rpc_url=rpc_url)
            # THE SAME COST POLICY AS EVERY OTHER VENUE (audit E2, 2026-09-18). This rung entered
            # the list UNWRAPPED, so its hit carried no `route.round_trip_pct`; the ladder accepts
            # an unmeasured hit when no measured one exists, and the curve's own safety wrapper
            # only asks for a positive whole-quantity sell quote. Neither path applied the budget
            # the venue spec requires (2026-09-17 curve design, Exit item 5), on a chain where this
            # rung is LIVE. `_measured` measures the round trip through the reverse leg below and
            # refuses an entry over budget; there is no curve-specific budget.
            #
            # EXITS ARE UNTOUCHED: `_measured` returns the rung's answer unchanged when `is_buy` is
            # false, which keeps the asymmetry every other venue has — the alternative to an
            # expensive exit is a bag we cannot sell at all.
            def _curve_reverse(s_t, b_t, amt):
                """The sell leg of the same curve, for the round-trip measurement only."""
                from sniper.execution.evm_launchpad_curve import dex_launchpad_curve_exit_quote
                return dex_launchpad_curve_exit_quote(
                    chain, s_t, b_t, amt, taker, http=http, slippage_bps=slippage_bps,
                    rpc_url=rpc_url)

            reverse_fns["launchpad_curve"] = _curve_reverse
            routes.append(("launchpad_curve",
                           _measured("launchpad_curve", _quote_launchpad_curve)))
        elif str(chain) != "4663":
            unavailable.append("launchpad_curve")          # gated to 4663 in code above
        else:
            skipped.append("launchpad_curve")
        # FLAP.SH CURVE (2026-09-20), default OFF, 56 only, NATIVE-QUOTE MINTS ONLY. Same shape
        # as launchpad_curve above -- a coin still on its own bonding curve has no V3/V2/V4/relay
        # pool, so this rung's own gate is its only gate. `dex_flap_curve_quote`/`_exit_quote`
        # return a bare quote-dict-or-None, the SAME contract every other rung here uses -- an
        # explicit refusal (not a flap.sh token, non-native quote, entry into an already-graduated
        # coin) is an ordinary miss to `classify_quote_miss`, never a fabricated blind_reason. An
        # earlier draft used a `(None, reason)` tuple on this exact seam and it was deliberately
        # removed (Gamma's review, 2026-09-20) -- do not restore it. The named, countable reason
        # for a refusal is `evm_flap_curve.refusal_reason()`, called separately by anything that
        # wants to log or measure it, not threaded through this dispatch.
        if bool(getattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", False)) and str(chain) == "56":
            flap_state_read = {}

            def _quote_flap_curve():
                from sniper.execution.evm_flap_curve import (
                    _check_state_fn, curve_state, dex_flap_curve_quote, dex_flap_curve_exit_quote)

                def capture_state(chain, mint, rpc_url, *, http=None):
                    state = curve_state(chain, mint, rpc_url, http=http)
                    if state is None:
                        return None
                    read = _check_state_fn(chain, mint, rpc_url, state, curve_state)
                    flap_state_read["read"] = read
                    return read(chain, mint, rpc_url, http=http)

                fn = dex_flap_curve_quote if is_buy else dex_flap_curve_exit_quote
                return fn(chain, sell_token, buy_token, amount_atomic, taker,
                         http=http, slippage_bps=slippage_bps, rpc_url=rpc_url,
                         **({"state_fn": capture_state} if is_buy and measured_ladder else {}))

            def _flap_curve_reverse(s_t, b_t, amt):
                """The sell leg of the same curve, for the round-trip measurement only."""
                from sniper.execution.evm_flap_curve import dex_flap_curve_exit_quote
                return dex_flap_curve_exit_quote(
                    chain, s_t, b_t, amt, taker, http=http, slippage_bps=slippage_bps,
                    rpc_url=rpc_url, pre_entry=True,
                    # One ladder's copied metadata only; both exact simulations stay live.
                    state_fn=flap_state_read.get("read"))

            reverse_fns["flap_curve"] = _flap_curve_reverse
            routes.append(("flap_curve", _measured("flap_curve", _quote_flap_curve)))
        elif str(chain) != "56":
            unavailable.append("flap_curve")               # gated to 56 in code above
        else:
            skipped.append("flap_curve")
        # Infinity has a different PoolKey ABI and Permit2; never send it through Uniswap V4.
        # Keys come from the existing source receipt worker, so discovery adds no entry RPC.
        if str(chain) == "56" and rpc_url:
            from sniper.execution import evm_pancake_infinity as infi
            infinity_hit = {}

            def _quote_infinity():
                q = infi.dex_infinity_quote(
                    chain, sell_token, buy_token, amount_atomic, taker, rpc_url=rpc_url,
                    http=http, slippage_bps=slippage_bps, acct=acct)
                if q:
                    infinity_hit["key"] = q["route"]["pool_key"]
                return q

            def _infinity_reverse(s_t, b_t, amt):
                key = infinity_hit.get("key")
                out = infi.quote_amount(key, False, amt, rpc_url=rpc_url, http=http) if key else None
                return {"buyAmount": str(out)} if out else None

            reverse_fns["pancake_infinity"] = _infinity_reverse
            routes.append(("pancake_infinity", _measured("pancake_infinity", _quote_infinity)))
        # RELAY IS EXIT-ONLY BY DEFAULT (2026-09-02). Relay is an aggregator, not a pool, and it
        # carries the aggregator's spread. It only wins when every direct rung has missed, which on
        # 4663 is most of the time, so it has become the de facto entry route on the chain where
        # routing is worst. Measured over 14 days of funded 4663 positions:
        #
        #     direct pool   n=45   MED ROI  +2.0%   win 53%   total  +$64.82
        #     relay         n=11   MED ROI -25.1%   win 27%   total -$214.73
        #
        # Eleven relay-routed entries lost more than forty-five direct ones made. A relay BUY is
        # paying a bridge price for a bag we then have to sell through the same spread.
        #
        # The sell side is the opposite: when every direct rung misses on an EXIT, relay is the only
        # way the position closes at all, and removing it would strand bags. So this refuses relay
        # for buys and keeps it for sells. `MEMECOIN_EVM_RELAY_ENTRY_ENABLED=1` restores the old
        # behaviour when the direct rungs can carry entries again.
        if relay_enabled and is_buy and not getattr(
                cfg, "MEMECOIN_EVM_RELAY_ENTRY_ENABLED", False):
            relay_enabled = False
            skipped.append("relay_entry_disabled")
        if relay_enabled:
            relay = relay_quote_fn
            if relay is None:
                from sniper.execution.evm_relay_quote import dex_relay_quote
                relay = dex_relay_quote
            def _relay_reverse(s_t, b_t, amt):
                """The SAME rung in the sell direction, on the bag the entry would have bought.

                Relay is an aggregator, so the reverse re-asks it for a route and the route it
                names may differ from the entry's. That is correct here and not the v4 decoy-pool
                hazard: we do not hold a pool, we hold a bag, and what the bag is worth is
                whatever this same aggregator will pay for it.
                """
                return relay(chain, s_t, b_t, amt, taker, http=http,
                             slippage_bps=slippage_bps, rpc_url=rpc_url)
            reverse_fns["relay"] = _relay_reverse

            def _quote_relay():
                if is_buy and not measured_ladder:
                    return (None, None, "%s:relay:measurement_disabled" % UNMEASURED)
                started = clock()
                # WALL TIME FOR THE DEADLINE ARITHMETIC BELOW, the same clock the ladder's own
                # `_wait` uses. `clock` is injected and driven by tests for rung timing, so a
                # budget read from it would not describe the deadline it is protecting.
                reverse_started_mono = _time.monotonic()
                q = relay(chain, sell_token, buy_token, amount_atomic, taker, http=http,
                          slippage_bps=slippage_bps, rpc_url=rpc_url)
                if not (is_buy and isinstance(q, dict)):
                    return q
                if cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT > 0:
                    from sniper.execution.evm_relay_quote import bind_relay_output_minimum
                    q = bind_relay_output_minimum(
                        q, guard_verified=relay_output_guard_verified) or q
                # RIZOWEEN 71056 (2026-10-06): a missing Relay reverse admitted a $50 bag after
                # V4/Kyber measured -99.99%/-92.55%. Every Relay buy must prove its reverse inside
                # the existing cost/deadline budgets, including priority mode and alternate quotes.
                route = q.setdefault("route", {})
                # Do not extend the entry deadline to obtain proof. Insufficient time is a refusal.
                budget_s = 0.5 * max(0.5, float(
                    getattr(cfg, "MEMECOIN_EVM_LADDER_WAIT_S", 8.0) or 8.0))
                if (clock() - started) >= budget_s:
                    return (None, None, "%s:relay:relay_reverse_no_deadline_budget" % UNMEASURED)
                # Bound the reverse by the remaining deadline, with room to return to the ladder.
                reverse_budget_s = (ladder_deadline_s
                                    - (_time.monotonic() - reverse_started_mono)
                                    - _REVERSE_DEADLINE_MARGIN_S)
                if reverse_budget_s <= 0:
                    return (None, None, "%s:relay:relay_reverse_no_deadline_budget" % UNMEASURED)
                # Socket timeouts bound each operation, not their sum. Keep the existing floor:
                # below it, refuse without starting a reverse that cannot fit the entry window.
                try:
                    min_budget_s = float(getattr(
                        cfg, "MEMECOIN_EVM_RELAY_REVERSE_MIN_BUDGET_S", 3.2))
                except (TypeError, ValueError):
                    min_budget_s = 3.2               # malformed reads as the default, never as 0
                if min_budget_s > 0 and reverse_budget_s < min_budget_s:
                    return (None, None, "%s:relay:relay_reverse_budget_below_expected" % UNMEASURED)
                route["round_trip_reverse_budget_s"] = reverse_budget_s

                def _bounded_relay_reverse(s_t, b_t, amt, _budget=reverse_budget_s):
                    return relay(chain, s_t, b_t, amt, taker, http=http,
                                 slippage_bps=slippage_bps, rpc_url=rpc_url, timeout=_budget)

                pct, unmeasured = _round_trip_for_hit(
                    "relay", q, chain=chain, sell_token=sell_token, buy_token=buy_token,
                    amount_atomic=amount_atomic, taker=taker, rpc_url=rpc_url, http=http,
                    slippage_bps=slippage_bps,
                    reverse_fns={**reverse_fns, "relay": _bounded_relay_reverse})
                route["round_trip_measured_by"] = "relay"
                if unmeasured is not None:
                    return (None, None, unmeasured)
                route["round_trip_pct"] = pct
                from sniper.execution.evm_v4_multihop import round_trip_within_budget
                if not round_trip_within_budget(pct, _entry_tp_pct(chain), chain=chain):
                    return (None, None, "round_trip_over_budget:%s" % pct)
                return q
            routes.append(("relay", _quote_relay))

        # KYBERSWAP AGGREGATOR RUNG (2026-09-22, Beta 02813ece; operator: "add the kyber rung so we
        # can fill these"). The aggregator finds paths through venues this ladder does not have
        # (PMM, Ekubo, Doppler, UP-v3: BILL, GIGABYTE, DAM on 4663) with one intermediary fewer
        # than Relay. Default OFF and chain-gated inside dex_kyber_quote; measured like every native
        # rung, with its own exit quote as the reverse leg, so the round-trip cap decides it. It
        # ranks before relay among unmeasured hits and after every native venue.
        kyber_chains = {str(c) for c in (getattr(cfg, "MEMECOIN_EVM_KYBER_AGG_CHAINS", ("4663",)) or ())}
        if bool(getattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", False)) and str(chain) in kyber_chains:
            from sniper.execution.evm_kyber_quote import dex_kyber_quote, dex_kyber_exit_quote

            def _quote_kyber():
                quote_fn = dex_kyber_quote if is_buy else dex_kyber_exit_quote
                return quote_fn(chain, sell_token, buy_token, amount_atomic, taker,
                                http=http, slippage_bps=slippage_bps, rpc_url=rpc_url)

            def _kyber_reverse(s_t, b_t, amt):
                return dex_kyber_exit_quote(chain, s_t, b_t, amt, taker, http=http,
                                            slippage_bps=slippage_bps, rpc_url=rpc_url)
            reverse_fns["kyber"] = _kyber_reverse
            routes.append(("kyber", _measured("kyber", _quote_kyber)))
        else:
            skipped.append("kyber")

        # V3 BRIDGE-TOKEN RUNG (2026-10-02, Alpha; PI 9493dd86). PI's market is a V3 1% pool
        # against ZEC; the v3 rung priced the thin PI/WBNB pool (-99.86%) and never asked ZEC,
        # because dex_v3_quote tries a pair token only after a full miss. This rung quotes
        # quote -> X -> coin for the coin's pair tokens in one batch and measures the round trip on
        # the SAME pools reversed (`bridge["route"]`, set on this rung's own thread before
        # _measured asks the reverse). Buys only: a held bridge position exits on its stored path.
        bridge_chains = {str(c) for c in (getattr(cfg, "MEMECOIN_EVM_V3_BRIDGE_CHAINS", ()) or ())}
        if is_buy and str(chain) in bridge_chains:
            from sniper.execution import evm_dex_quote as _dq
            bridge = {}

            def _quote_bridge():
                rejected = []
                paths = _dq.v3_bridge_paths(chain, sell_token, buy_token, rejected=rejected)
                if not paths:
                    # The trace row carries the coin; the reason names each unvetted X.
                    return (None, None, ("bridge_x_not_allowlisted:" + ",".join(rejected))
                            if rejected else "no_bridge_token")
                q = _dq.dex_v3_quote(chain, sell_token, buy_token, amount_atomic, taker, http=http,
                                     slippage_bps=slippage_bps, extra_paths=paths,
                                     paths_only=True)
                if not isinstance(q, dict):
                    return (None, None, "no_bridge_route")
                q = dict(q, route=dict(q.get("route") or {}, bridge=True))
                bridge["route"] = q["route"]
                return q

            def _bridge_reverse(s_t, b_t, amt):
                route = bridge.get("route") or {}
                tokens, fees = route.get("tokens"), route.get("fees")
                if not tokens or not fees:
                    return None
                return _dq.dex_v3_quote(chain, s_t, b_t, amt, taker, http=http,
                                        slippage_bps=slippage_bps, paths_only=True,
                                        extra_paths=[(list(reversed(tokens)), list(reversed(fees)))])
            reverse_fns["v3_bridge"] = _bridge_reverse
            routes.append(("v3_bridge", _measured("v3_bridge", _quote_bridge)))
        elif str(chain) in bridge_chains:
            skipped.append("v3_bridge")                    # a sell: the stored route exits it

        # PCS V3 -> V2 MIXED HOP (spec 2026-09-25, 7ef40f05). A coin whose only market is a V2 pair
        # in a NON-connector base token (螃蟹 / oANTHROPIC) is reachable only through a V3 bridge
        # into that base token; every single-venue rung prices a dust pool instead (螃蟹: v3 -86%,
        # v2 -99.7%, kyber -41.9% against a -2.28% round trip through the real pools). One
        # SmartRouter multicall, a measured round trip judged by the same budget as every rung,
        # and NO QUOTE AT ALL without a whole-balance exit simulated through the same router. The
        # simulation needs token safety's balance/allowance override builder, which the chain
        # context binds onto this ladder as `fund_overrides_fn`; without it the rung refuses.
        mixed_chains = {str(c) for c in (
            getattr(cfg, "MEMECOIN_EVM_MIXED_HOP_PROBE_CHAINS", ()) or ())}
        if (bool(getattr(cfg, "MEMECOIN_EVM_MIXED_HOP_EXECUTION_ENABLED", False))
                and str(chain) in mixed_chains):
            from sniper.execution import evm_mixed_hop_router as _mixed

            def _quote_mixed():
                if not is_buy:
                    # EXITS ARE NEVER REFUSED ON COST: the alternative is a bag we cannot sell.
                    q, why = _mixed.mixed_exit_quote(
                        chain, sell_token, buy_token, amount_atomic, taker, http=http,
                        slippage_bps=slippage_bps)
                    return q if q is not None else (None, None, why)
                q, why = _mixed.mixed_entry_quote(
                    chain, sell_token, buy_token, amount_atomic, taker, rpc_url=rpc_url,
                    fund_overrides_fn=getattr(combined, "fund_overrides_fn", None), http=http,
                    slippage_bps=slippage_bps, safety_deadline=mixed_deadline)
                if q is None:
                    proof_reason = str(why or "").removeprefix("exit_unproven:")
                    failure, _, kind = proof_reason.partition(":")
                    blind = (kind or "rpc_transport") if failure in (
                        "exit_overrides_unavailable", "exit_sim_error", "exit_sim_unknown") else None
                    return (None, blind, why)
                from sniper.execution.evm_v4_multihop import round_trip_within_budget
                pct = q["route"]["round_trip_pct"]
                if not round_trip_within_budget(pct, _entry_tp_pct(chain), chain=chain):
                    margin = float(getattr(
                        cfg, "MEMECOIN_EVM_V4_MULTIHOP_BUDGET_RETRY_MARGIN_PCT", 0.0) or 0.0)
                    marginal = margin > 0 and round_trip_within_budget(
                        pct, _entry_tp_pct(chain), chain=chain, margin_pct=margin)
                    return (None, None, "%s:%s" % (
                        "round_trip_over_budget_marginal" if marginal
                        else "round_trip_over_budget", pct))
                return q
            routes.append(("mixed_v3_v2", _quote_mixed))
        elif str(chain) in mixed_chains:
            skipped.append("mixed_v3_v2")                  # the operator's switch is off
        else:
            unavailable.append("mixed_v3_v2")              # no verified SmartRouter: a build

        def _run(route):
            started_at = clock()
            reason = None
            # PER FUTURE, NEVER A SHARED LIST. The rungs run in a ThreadPoolExecutor below, so the
            # tap is installed on THIS thread and taken back here; two rungs quoting at once
            # cannot see each other's calls, and no lock is needed because nothing is shared. The
            # pool reuses threads, so `start_rpc_tap` replaces rather than clears.
            _start_rpc_tap()
            _start_http_tap()                # provider HTTP calls and timed stages (P4)
            try:
                result = route()
                if isinstance(result, tuple) and len(result) == 3:
                    result, blind_reason, reason = result
                elif isinstance(result, tuple):
                    result, blind_reason = result
                else:
                    blind_reason = None
            except Exception as exc:
                result = None
                blind_reason = "timeout" if isinstance(exc, TimeoutError) else getattr(
                    exc, "failure_type", "rpc_transport")
                # WHAT RAISED, kept as the blind rung's reason (2026-09-29). `classify_quote_miss`
                # reads `reason` only on rungs that are NOT blind, so no verdict can change.
                reason = rung_error_text(exc)
            # TAKEN IN EVERY CASE, including the raising one: a rung that failed still made the
            # calls that failed, and those are the payloads most worth replaying.
            calls = _take_rpc_tap()
            http_calls = _take_http_tap()
            return (result, blind_reason, max(0.0, clock() - started_at) * 1000.0, reason,
                    calls or [], http_calls or [])

        # ONE RUNG, ON PURPOSE (2026-09-22). A winner refresh re-asks the rung that already won,
        # not the whole ladder: re-running every venue to refresh one quote costs the 1.8-3 s that
        # made the quote stale in the first place. Filtering HERE, after every rung has been
        # assembled, means the single rung is built and measured by exactly the same code that
        # built it the first time -- a second construction path is how a refresh quietly starts
        # quoting something else.
        if only_rung is not None:
            routes = [(name, route) for name, route in routes if name == only_rung]
            if not routes:
                return None                  # that rung is not configured on this chain now
        # RUNGS THE CALLER MUST NOT ASK (2026-09-30). The dip watch's fallback ladder drops
        # v4_multihop: its discovery reads DexScreener/GeckoTerminal pool lists, which operator
        # order 86d053ce keeps out of the watch. Dropped before submission, so it is never called.
        if exclude_rungs:
            routes = [(name, route) for name, route in routes if name not in exclude_rungs]
            if not routes:
                return None
        source_ladder = any(name in _SOURCE_RUNGS for name, _r in routes)
        # ONE DEADLINE FOR THE WHOLE LADDER (2026-09-11). "Each rung carries its own timeout" did
        # not hold: a 4663 buy ladder waited >85 s on one rung, the main loop froze, the exit
        # sweep went stale and the watchdog killed the runner mid-entry -- all three stale kills
        # that day (08:52, 12:31, 14:28Z) were a 4663 entry stuck here. After the deadline the
        # ladder decides from the rungs that answered; stragglers are recorded, never awaited.
        # ASSIGNED BEFORE THE RUNGS ARE SUBMITTED (2026-09-23): `_quote_relay` reads this free
        # variable on its own thread to bound the reverse quote (E4, c8b6a9b6). With the
        # assignment after `executor.submit`, a relay forward that answered before this thread
        # reached it raised NameError inside the rung, which `_run` recorded as a blind
        # `rpc_transport` and the ladder handed the win to the next measured rung. Reproduced 30
        # times in 300 with instant stubs; live relay answers in seconds, so it had not fired
        # there, but the race was real.
        from concurrent.futures import TimeoutError as _LadderTimeout
        ladder_deadline_s = max(0.5, float(getattr(cfg, "MEMECOIN_EVM_LADDER_WAIT_S", 8.0) or 8.0))
        mixed_deadline = _time.monotonic() + ladder_deadline_s
        if ladder_started_fn is not None:
            if ladder_started_fn(mixed_deadline - ladder_deadline_s, ladder_deadline_s) is False:
                return None  # attempt fell back before launch; submit no speculative provider work
        from concurrent.futures import ThreadPoolExecutor, as_completed
        executor = ThreadPoolExecutor(
            max_workers=len(routes), thread_name_prefix="evm-route-quote")
        futures = {executor.submit(_run, route): name for name, route in routes}
        ordered_names = list(LADDER_ORDER)
        configured = {name for name, _route in routes}
        completed = {}
        selected = None
        timed_out = set()
        late = set()                       # unanswered when the post-hit grace ended (buys)
        grace_ended_ms = 0.0
        try:
            if measured_ladder and is_buy:
                # WAIT FOR EVERY RUNG, THEN TAKE THE BEST MEASURED ROUND TRIP. The fixed priority
                # took the FIRST hit in ordered_names, so a cheap decoy that happened to sit
                # earlier in the list beat a better route that had already quoted. BUDDY
                # (2026-09-06): v4 hit through decoy pool 0x5bb3337725 and v4_multihop ALSO hit
                # through the real hook-0xe5e702 pool; v4 wins on order alone, and the first exact
                # exit mark 4s after fill was -61%. The rungs already run in parallel and each
                # carries its own timeout, so waiting costs the slowest rung, not their sum.
                # ...BUT NOT FOR LONGER THAN A BOUNDED GRACE AFTER THE FIRST MEASURED HIT
                # (2026-09-23, Ztach 435622f2). "Waiting costs the slowest rung" was the cost:
                # launchpad_curve answered in 3.06 s and the ladder then waited 7.37 s for kyber,
                # v4 and relay, and the route quote it finally compared was 1.65x the source price.
                # On 4663 since 09-20 (n=34 ladders) the wait after the first hit was p50 3.0 s
                # when v4 hit first and 2.1 s (p90 5.0 s) when the curve hit first. A hit on a
                # launchpad or flap CURVE ends the wait at once: before graduation the curve is
                # the only venue, so no other rung can outrank it. Any other measured hit opens a
                # grace of MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S for the slower rungs
                # (v4_multihop p50 2.0 s, relay p50 3.1 s), and rungs still unanswered when the
                # grace ends are recorded as `ladder_grace`, never awaited.
                # WALL TIME, NOT THE INJECTED `clock`: the tests drive `clock` for rung timing,
                # and a wait budget read from it would expire before the threads answer.
                from concurrent.futures import FIRST_COMPLETED, wait as _wait
                # Per chain since 2026-09-28: MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_<chain>,
                # else the global (unset on every chain = the global, as before).
                grace_s = max(0.0, float(cfg.evm_ladder_grace_after_hit_s_for(chain)))
                _mono = _time.monotonic
                ladder_started = _mono()
                deadline_at = ladder_started + ladder_deadline_s
                grace_at = None
                limit_was_grace = False
                pending = set(futures)
                while pending:
                    limit = deadline_at if grace_at is None else min(deadline_at, grace_at)
                    limit_was_grace = grace_at is not None and grace_at < deadline_at
                    done, pending = _wait(pending, timeout=max(0.0, limit - _mono()),
                                          return_when=FIRST_COMPLETED)
                    if not done:
                        break              # the limit passed with rungs still unanswered
                    for future in done:
                        name = futures[future]
                        entry = future.result()
                        completed[name] = entry
                        result = entry[0] if entry else None
                        route = (result.get("route") or {}) if isinstance(result, dict) else {}
                        if route.get("round_trip_pct") is None:
                            continue       # a miss or an unmeasured hit opens no grace
                        if name in ("launchpad_curve", "flap_curve"):
                            grace_at = _mono()
                        elif grace_at is None:
                            grace_at = _mono() + grace_s
                unanswered = {n for n in futures.values() if n not in completed}
                if unanswered and limit_was_grace:
                    late = unanswered
                    grace_ended_ms = max(0.0, _mono() - ladder_started) * 1000.0
                else:
                    timed_out = unanswered
                best = None                # (round_trip_pct, -priority, result)
                for rank, name in enumerate(ordered_names):
                    entry = completed.get(name)
                    if not entry or entry[0] is None:
                        continue
                    route = (entry[0].get("route") or {}) if isinstance(entry[0], dict) else {}
                    pct = route.get("round_trip_pct")
                    if pct is None:
                        continue           # unmeasured never outranks a measured hit
                    key = (float(pct), -rank)
                    if best is None or key > best[0]:
                        best = (key, entry[0])
                if best is not None:
                    selected = best[1]
                else:
                    # Only unmeasured hits came back. Priority decides among them, exactly as
                    # before, for rungs whose own admission rules permit an unmeasured hit.
                    for name in ordered_names:
                        entry = completed.get(name)
                        if entry and entry[0] is not None:
                            selected = entry[0]
                            break
            elif chain == "1" and not is_buy and not first_available:
                # Ethereum actual exits compare exact proceeds; safety still takes its first hit.
                try:
                    for future in as_completed(futures, timeout=ladder_deadline_s):
                        completed[futures[future]] = future.result()
                except _LadderTimeout:
                    timed_out = {n for n in futures.values() if n not in completed}
                best_output = 0
                for name in ordered_names:
                    entry = completed.get(name)
                    if not entry or not isinstance(entry[0], dict):
                        continue
                    try:
                        output = int(entry[0].get("buyAmount") or 0)
                    except (TypeError, ValueError):
                        continue
                    if output > best_output:
                        best_output, selected = output, entry[0]
            else:
                try:
                    for future in as_completed(futures, timeout=ladder_deadline_s):
                        completed[futures[future]] = future.result()
                        for name in ordered_names:
                            if name not in configured:
                                continue
                            if name not in completed:
                                # Safety needs one simulatable sell, not a priority winner.
                                # Entry and actual exit calls retain their existing selection.
                                if first_available and not is_buy:
                                    continue
                                break
                            if completed[name][0] is not None:
                                selected = completed[name][0]
                                break
                        if selected is not None:
                            break
                except _LadderTimeout:
                    # A hung higher-priority rung must not hold an answer that already arrived.
                    timed_out = {n for n in futures.values() if n not in completed}
                    for name in ordered_names:
                        entry = completed.get(name)
                        if entry and entry[0] is not None:
                            selected = entry[0]
                            break
                # DUST CHECK ON SELLS (2026-09-11, Gamma F1; SUIT 50977). The sell side takes the
                # first venue that answers, by priority, and a drained side pool always answers:
                # SUIT's previews read its bag at $0.97 (-99.3%) while Relay could sell it at -16%.
                # When the caller says the winner's output is implausible, wait a bounded time for
                # the other venues and take the largest output. Measured: fires on 0.05% of EVM
                # exit previews (all SUIT); Relay answers at p50 1.9 s / p99 3.4 s.
                if (selected is not None and not is_buy and sell_plausible_fn is not None
                        and isinstance(selected, dict)):
                    try:
                        plausible = bool(sell_plausible_fn(int(selected.get("buyAmount") or 0)))
                    except Exception:
                        plausible = True
                    if not plausible:
                        from concurrent.futures import TimeoutError as _FuturesTimeout
                        cap = float(getattr(cfg, "MEMECOIN_EVM_SELL_DUST_WAIT_S", 3.5) or 0.0)
                        pending = [f for f, n in futures.items() if n not in completed]
                        try:
                            for future in as_completed(pending, timeout=max(0.0, cap)):
                                completed[futures[future]] = future.result()
                        except _FuturesTimeout:
                            pass
                        outputs = {}
                        for name, entry in completed.items():
                            if entry and isinstance(entry[0], dict):
                                try:
                                    outputs[name] = int(entry[0].get("buyAmount") or 0)
                                except (TypeError, ValueError):
                                    continue
                        best_name = max(outputs, key=outputs.get) if outputs else None
                        if best_name and outputs[best_name] > int(selected.get("buyAmount") or 0):
                            selected = completed[best_name][0]
                        logger.warning("sell ladder dust check fired chain=%s token=%s outputs=%s "
                                       "chose=%s waited_cap=%.1fs", chain, sell_token,
                                       outputs, best_name, cap)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        # A CUT RUNG STILL SAYS WHEN IT FINISHED (2026-09-28): one log line from its own thread,
        # after the ladder has returned; see _log_cut_rung_when_done.
        source_won = None
        if isinstance(selected, dict):
            for name, entry in completed.items():
                if entry[0] is selected and name in _SOURCE_RUNGS:
                    try:
                        source_won = (name, int(selected.get("buyAmount") or 0))
                    except (TypeError, ValueError):
                        source_won = None
        for future, name in futures.items():
            if name in (late | timed_out) and name not in completed:
                _log_cut_rung_when_done(future, chain=str(chain), run_id=run_id, name=name,
                                        cut="ladder_grace" if name in late else "ladder_deadline",
                                        winner=source_won)
        # THE RUNNER-UP HITS RIDE WITH THE WINNER (2026-09-22, MUSEFUND e31953ff and AGRIPPA
        # e3fd121c). Both had two in-budget hits, v4_multihop and relay; v4_multihop won and its
        # exact-route preflight reverted on a hooked pool, and the attempt refused while relay's
        # quote sat unused -- the trader's own fill had gone through Relay. The executor can now
        # fall through to the next hit on a preflight revert instead of refusing. Buys only, and
        # only hits that passed their own rung (a measured hit is inside budget by construction;
        # Relay also requires a measured reverse). Stored under a private key that
        # quote_fields never persists, so the route JSON on a position stays the winner's.
        if isinstance(selected, dict):
            for name, entry in completed.items():
                if entry[0] is selected:
                    selected["_sniper_winner"] = name
                    break
            if selected.get("_sniper_winner") in _SOURCE_RUNGS and route_hint:
                from sniper.execution import evm_source_route as _src
                _src.remember(chain, buy_token, ((selected.get("route") or {}).get("pool_id")
                              if selected["_sniper_winner"] == "v4_source" else route_hint))
        if is_buy and isinstance(selected, dict):
            ranked = []
            for rank, name in enumerate(ordered_names):
                entry = completed.get(name)
                if not entry or entry[0] is None or entry[0] is selected \
                        or not isinstance(entry[0], dict):
                    continue
                pct = ((entry[0].get("route") or {}).get("round_trip_pct")
                       if isinstance(entry[0].get("route"), dict) else None)
                measured = pct is not None
                ranked.append(((0 if measured else 1, -(float(pct) if measured else 0.0), rank),
                               name, entry[0]))
            ranked.sort(key=lambda item: item[0])
            if ranked:
                selected["_alternates"] = [{"rung": name, "quote": q} for _k, name, q in ranked]
        for name in ordered_names:
            # A REFRESH REPORTS ONE RUNG, NOT A LADDER (2026-09-22). The other venues were never
            # asked on a `only_rung` pass, so recording them as skipped would write a full
            # ladder trace for a call that made one quote -- and `classify_quote_miss` reading
            # that trace would describe a no_route across venues nobody consulted.
            if (only_rung is not None and name != only_rung) or name in exclude_rungs:
                continue
            if name in completed:
                result, blind_reason, duration_ms, reason, rung_calls, rung_http = completed[name]
                # THE TRACE HAS TO EXPLAIN THE CHOICE. Without the round trip on every rung, a
                # reader can see which one won but not why it beat the others.
                route = (result.get("route") or {}) if isinstance(result, dict) else {}
                rt = route.get("round_trip_pct")
                if rt is None and route.get("round_trip_unmeasured"):
                    rt = UNMEASURED
                _rung(name, result is not None, run_id=run_id, blind_reason=blind_reason,
                      duration_ms=duration_ms, reason=reason, round_trip_pct=rt,
                      calls=rung_calls, http=rung_http, pools=rung_pools.get(name),
                      buy_out=(result.get("buyAmount") if source_ladder and isinstance(result, dict)
                               else None))
            elif name in unavailable:
                _rung(name, False, run_id=run_id, skipped=True, unavailable=True)
            elif name in skipped:
                _rung(name, False, run_id=run_id, skipped=True)
            elif name in timed_out:
                _rung(name, False, run_id=run_id, blind_reason="ladder_deadline",
                      duration_ms=ladder_deadline_s * 1000.0)
            elif name in late:
                _rung(name, False, run_id=run_id, blind_reason="ladder_grace",
                      duration_ms=grace_ended_ms)
            elif selected is not None and name in configured:
                _rung(name, False, run_id=run_id, skipped=True,
                      blind_reason="priority_resolved")
        if timed_out:
            logger.warning("venue ladder deadline %.1fs chain=%s buy=%s unanswered=%s selected=%s",
                           ladder_deadline_s, chain, is_buy, sorted(timed_out), selected is not None)
        if late:
            logger.info("venue ladder grace ended at %.2fs chain=%s late=%s selected=%s",
                        grace_ended_ms / 1000.0, chain, sorted(late), selected is not None)
        # MEASUREMENT ONLY, AND ONLY WHERE THE LADDER FOUND NOTHING. `selected` is already decided
        # above and is never reassigned here: this probe records what a PCS V3 -> V2 mixed hop
        # WOULD have quoted for a signal we are refusing anyway. Running it on the no-route path
        # alone means it can never delay a signal that could still become an entry, and returning
        # it could never be right — two legs are two transactions, so a fill on the first with a
        # failure on the second is an open position in a coin this ladder just refused.
        if selected is None and is_buy:
            try:
                from sniper.execution.evm_mixed_hop_probe import probe_v3_v2
                probe = probe_v3_v2(chain, sell_token, buy_token, amount_atomic, taker,
                                    http=http, slippage_bps=slippage_bps)
                if probe.get("reason") not in ("probe_disabled", "chain_not_probed"):
                    _rung("mixed_v3_v2_probe", False, run_id=run_id, skipped=True,
                          reason=probe.get("reason"))
                    logger.info("mixed-hop probe chain=%s coin=%s %s", chain, buy_token, probe)
            except Exception:
                logger.exception("mixed-hop probe failed (ladder unaffected)")
        # THE WINNER CARRIES ITS OWN REFRESH (2026-09-22). The `row` entry 0xb30fcc1d passed its
        # preflight on state the chain had already left and the transaction reverted two seconds
        # later, 115,763 gas for nothing. Part of that staleness is ours: this ladder takes 1.8-3 s,
        # and the minimum is computed at the top of it. A caller that is about to simulate can now
        # re-ask the rung that WON, for a few hundred milliseconds, instead of paying for the whole
        # ladder again -- and `only_rung` guarantees the refresh is built by the same code path.
        #
        # TWO QUESTIONS, TWO FIELDS, ONE REPRESENTATION EACH (E2, 2026-09-23). "How old is this
        # quote inside this process" and "at what instant was it taken" are not the same question,
        # and they were sharing `_sniper_quote_observed_at`. This block wrote a monotonic float
        # there; the capacity-reuse path at `quote_at_snapshot` writes an ISO string to the same
        # key. Three consumers parse it as ISO UTC -- `_effective_chase_cap`'s wallet window,
        # `quote_age_ms`, and everything persisted through `quote_fields` -- so all three failed
        # closed on every ladder quote, and the WALLET CHASE CAP silently fell back to the base cap
        # on all of them. The staleness check parses it as a float, so it failed on exactly the
        # reused quotes the other three could read.
        #
        # `_sniper_quote_monotonic_at` answers the age question and nothing else. It is deliberately
        # not a wall clock: an age must not move because the host's time did.
        # `_sniper_quote_observed_at` is now always an ISO UTC instant.
        #
        # Stamped in place, like the relay rung's own route annotation. `dex_*_quote` builds a
        # fresh dict per call in production, so nothing carries between calls.
        if selected is not None and isinstance(selected, dict):
            winner = selected.get("_sniper_winner")     # named by the fall-through block above
            if winner:
                winner_hint = ((selected.get("route") or {}).get("pool_id")
                               if winner == "v4_source" else route_hint)
                def _requote(slippage_bps=None, _s=sell_token, _b=buy_token, _a=amount_atomic,
                             _w=winner, _base=slippage_bps, _h=winner_hint, _e=exit_hint):
                    """Re-ask ONLY the winning rung. None when it no longer quotes."""
                    return combined(_s, _b, _a, only_rung=_w, route_hint=_h, exit_hint=_e,
                                    slippage_bps=_base if slippage_bps is None else slippage_bps)
                # ONLY THE MONOTONIC STAMP IS WRITTEN HERE. The ISO instant is NOT this block's
                # to produce: the corrected clock (`real_utc_now_iso`) reads an offset file on
                # every call, and the entry quote path does not take a file read. The executor
                # already supplies the instant from the trace it has open -- see the
                # `or recorded_quote_at` fallback where it binds the quote -- so leaving the key
                # ABSENT here is what makes that fallback produce an ISO string instead of the
                # float this block used to write.
                if is_buy:
                    selected.setdefault("_sniper_quote_monotonic_at", clock())
                selected["_requote"] = _requote
        return selected

    return combined
# --- EVM JSON-RPC ------------------------------------------------------------------------------

def eth_proof_snapshot(address, token, rpc_url, http=None, *, decimals_fn=None, include_atomic=False):
    """ONE block's view for the protocol-expiry dead proof: `{block, ts, nonce, balance}`, every
    number read AT THAT BLOCK, or None.

    The proof's facts must describe the same chain state. `eth_getBlockByNumber`,
    `eth_getTransactionCount` and `eth_call` are all hedged reads, so asked at "latest" each can be
    answered by a different pool member at a different height, and a load-balanced URL does the
    same behind one name (Gamma MEDIUM on 5b87e0b1e). A chain time 24 s fresher than the nonce and
    the bag released a transaction that had landed in between.

    So: the latest block ONCE, its number and its timestamp, then the account nonce and the token
    balance at that block NUMBER. A member that does not have the block yet answers an error, and
    any error, missing field or unreadable word is None: no proof, the row is held.

    `nonce` is the account's transaction count at that block (the mined nonce as of it). `balance`
    is in whole tokens; decimals are immutable, so the height they are read at does not matter.
    """
    from sniper.execution.evm_wallet import SEL_BALANCE_OF, _balance_word
    try:
        block = _rpc(rpc_url, "eth_getBlockByNumber", ["latest", False], http=http)["result"]
        tag = block["number"]
        number, stamp = int(tag, 16), int(block["timestamp"], 16)
        nonce = int(_rpc(rpc_url, "eth_getTransactionCount", [address, tag], http=http)["result"], 16)
        atomic = _balance_word(_rpc(
            rpc_url, "eth_call", [{"to": token, "data": "0x" + SEL_BALANCE_OF + _pad_addr(address)},
                                  tag], http=http)["result"], token)
        decimals = decimals_fn(token) if decimals_fn is not None else erc20_decimals(
            token, rpc_url, http=http)
        decimals = int(decimals)
        if decimals < 0:
            return None
    except Exception:
        return None
    result = {"block": number, "ts": stamp, "nonce": nonce, "balance": atomic / (10 ** decimals)}
    if include_atomic:
        result.update(balance_atomic=atomic, decimals=decimals)
    return result


def eth_nonce(address, rpc_url, http=None, block_tag="pending"):
    """Account nonce at the selected block tag. None on a failed read."""
    try:
        return int(_rpc(rpc_url, "eth_getTransactionCount", [address, block_tag], http=http)["result"], 16)
    except Exception:
        return None


def eth_gas_price(rpc_url, http=None):
    try:
        return int(_rpc(rpc_url, "eth_gasPrice", [], http=http)["result"], 16)
    except Exception:
        return None


def _is_revert_evidence(data, message, kind):
    """True iff this JSON-RPC error is evidence the EVM actually REVERTED.

    A node returns an error object for two very different things: the contract reverted, and the
    node could not run the call. Both arrive as HTTP 200 with an `error` field, so neither raises,
    and both used to return status=False -- "the token rejected the sell".

    Measured on 4663, all 32 `sell_sim_reverted_unknown` refusals:

        (no detail)             15     no payload, no message
        Call failed              7     the provider's generic message, no payload
        execution reverted       5     a real revert
        TRANSFER_FROM_FAILED     4     a real revert (decoded from the payload)
        insufficient balance     1     OUR state override failed to fund the wallet

    22 of 32 (69%) carried no revert evidence whatever, and one names our own override failing.
    Four of those tokens were later proven sellable -- one of them refused THIRTEEN MINUTES after
    we closed a real position on it for +$27.23.

    Evidence is: a hex payload (Error(string) or a custom-error selector), a classifier hit, or a
    message that says revert. Absent all three, the honest status is UNKNOWN.

    This does NOT loosen the gate. The caller fails closed on unknown exactly as it does on a
    revert -- no entry is admitted that was not admitted before. What changes is the label, and
    therefore whether an infrastructure failure advances a token-unsellable counter or is eligible
    for a provider retry.
    """
    if kind and kind != "unknown":                       # min_out / transfer_failed recognised
        return True
    blob = data.get("data") if isinstance(data, dict) else data
    if isinstance(blob, str) and blob.strip().lower().startswith("0x"):
        # A REVERT PAYLOAD IS AT LEAST A 4-BYTE SELECTOR, AND IT IS HEX (2026-09-22, MUSEFUND
        # e31953ff and AGRIPPA e3fd121c). `len > 2` accepted `0x0` as "a payload the contract
        # produced", so a node answering an infrastructure failure with a stub data field proved a
        # revert that never happened: both attempts refused `entry_simulation_reverted ...
        # detail=Call failed` while the route simulated True at 3% slippage, at every haircut down
        # to minOut=0, and at every size from 0.005 to 1.0 ETH. Reproduced exactly from data='0x0'.
        # This is the same hole the message branch below was written to close -- that fix plugged
        # the prose half and left the payload half open. Rows already carry the evidence:
        # `custom_error 0x0` and `custom_error 0x4` are stubs that were named rather than refused.
        body = blob.strip().lower()[2:]
        if len(body) >= 8 and not body.strip(_HEX_DIGITS):
            return True                                  # a payload the contract produced
    text = str(message or "").strip()
    if "revert" in text.lower():
        return True
    # A CONTRACT REVERT STRING carried in the message with no payload. `require(x, "TRANSFER_FROM_
    # FAILED")` and solady's short codes reach some nodes as the bare identifier, and the first cut
    # of this predicate flipped them to UNKNOWN — losing the token-vs-route attribution built for
    # exactly that class. Caught 2026-09-02 by tests/test_memecoin_evm_token_safety.py, which I had
    # not run because I ran only the revert suites.
    #
    # The discriminator is shape, not a keyword list: a Solidity revert identifier is a single
    # token with no spaces, while every node STATUS message observed in production is prose --
    # "Call failed", "insufficient balance". Checked against all five details seen on 4663:
    # (none) and "Call failed" and "insufficient balance" stay UNKNOWN, "execution reverted" and
    # "TRANSFER_FROM_FAILED" stay REVERTS.
    return bool(text) and " " not in text and text.upper() == text and len(text) <= 64


# A node that cannot serve the pinned block answers with one of these (geth / op-geth / reth /
# erigon wording). That is "this endpoint is behind or pruned", never a verdict on the token.
_BLOCK_UNAVAILABLE = ("header not found", "unknown block", "missing trie node",
                      "block not found", "state not available", "state is not available")


def _block_unavailable(message):
    text = str(message or "").lower()
    return any(marker in text for marker in _BLOCK_UNAVAILABLE)


def _rpc_eth_call_result(tx, from_addr, rpc_url, http=None, overrides=None, block_tag="latest",
                         *, result_out=None):
    """Return ``(status, revert_kind, detail)`` for a funded sell simulation.

    ``status`` is True on success, False on an EVM revert, and None when transport/RPC truth is
    unavailable. Revert detail must survive this seam so the safety layer can distinguish a proven
    router min-out from an unclassified revert without weakening its fail-closed decision.

    `overrides` is an eth_call state-override map. It is REQUIRED for an honest honeypot probe: we do
    not hold the token we are about to buy, so an un-overridden sell reverts on insufficient
    balance/allowance for EVERY token and reads as a honeypot (2026-07-14 regression — the EVM leg
    refused 27/28 real entries and never opened a position). Callers fund the balance + allowance
    slots so the revert, if any, is the TOKEN's doing and not ours."""
    # Optional caller-owned capture. Reuse this exact call's output without changing the
    # three-state verdict or issuing another read. Never retain a prior successful result.
    if result_out is not None:
        result_out.clear()
    val = tx.get("value") or 0
    if isinstance(val, str) and val.startswith("0x"):
        v = val
    else:
        try:
            v = hex(int(val))
        except (TypeError, ValueError):
            v = "0x0"
    call = {"from": from_addr, "to": tx.get("to"), "data": tx.get("data", "0x"), "value": v}
    try:
        gas = _evm_quantity(tx.get("gas"))
    except (TypeError, ValueError):
        gas = 0
    if gas > 0:
        call["gas"] = hex(gas)
    params = [call, block_tag] + ([overrides] if overrides else [])
    # THE READ THAT SPENT THE GAS (2026-09-22). `row` 0xb30fcc1d passed HERE at 21:40:21.35 on
    # custody state and the chain reverted the transaction two seconds later, 115,763 gas, no logs.
    # dc6848b7 routed reads that go through evm_read_rpc.eth_call_raw to the least stale pool
    # member; this simulation calls `_rpc` directly and was never covered, so the quote half was
    # fresh and the half that decides whether to sign was not.
    from sniper.execution.evm_read_rpc import _freshest_read_member
    try:
        j = _rpc(_freshest_read_member(rpc_url), "eth_call", params, http=http)
    except Exception as exc:
        # TRANSPORT failure (429 / timeout / 5xx) — says nothing about the token. Must NOT read as a
        # revert: Base's public RPC 429s after ~4 calls, and mapping that to False would resurrect
        # the "everything is a honeypot" bug. None = unknown; the caller fails closed with a reason.
        kind = "timeout" if isinstance(exc, TimeoutError) else getattr(exc, "failure_type", None)
        return None, kind, None
    err = j.get("error")
    if err:
        data = err.get("data") if isinstance(err, dict) else None
        message = err.get("message") if isinstance(err, dict) else str(err)
        if block_tag != "latest" and _block_unavailable(message):
            return None, "block_unavailable", str(message or "")[:120]
        kind = classify_evm_revert(data, message)
        if isinstance(data, dict):
            data = data.get("data")
        detail = _revert_detail(data, message)
        # KEEP THE RAW BYTES WHEN THEY NAME NOTHING (2026-09-22). MUSEFUND and AGRIPPA stored
        # `detail=Call failed` and the stub payload that actually drove the verdict was discarded,
        # so the cause had to be re-derived by reproducing the predicate rather than read off the
        # row. `_revert_detail` stays pure -- it must never promote junk to a selector -- so the
        # raw field is appended HERE, clearly labelled as raw and never as an identifier.
        raw = data if isinstance(data, str) else None
        # ONLY when `_revert_detail` decoded nothing and named nothing, i.e. it fell through to the
        # node's own message. A decoded Error(string) or a named selector already carries the
        # bytes, and repeating them there would be noise in the row that matters most.
        fell_through = detail == (str(message or "")[:120] or None)
        if fell_through and raw and raw.strip().lower() not in ("", "0x"):
            detail = ("%s; raw=%s" % (detail or "", raw.strip()[:48])).lstrip("; ")
        if not _is_revert_evidence(data, message, kind):
            # The node failed the call; the token did not refuse it. Fails closed identically, but
            # as UNKNOWN rather than as a token verdict.
            return None, None, detail
        return False, kind, detail
    if "result" in j:
        if result_out is not None:
            result_out["result"] = j["result"]
        return True, None, None
    return None, None, None


def _rpc_eth_call(tx, from_addr, rpc_url, http=None, overrides=None):
    """Backward-compatible boolean funded sell simulation seam."""
    status, _kind, _detail = _rpc_eth_call_result(
        tx, from_addr, rpc_url, http=http, overrides=overrides,
    )
    return status


def _alternate_read_member(rpc_url):
    """A healthy pool member that is NOT `rpc_url`, or None when the pool has no second opinion."""
    try:
        from sniper.execution import evm_rpc_pool as _pool
        for url in _pool.pool_for(rpc_url):
            if url != rpc_url and _pool.available(url):
                return url
    except Exception:                       # a pool lookup must never decide an entry
        logger.exception("alternate read member lookup failed rpc=%s", rpc_url)
    return None


def preflight_weth_entry_quote(quote, *, quote_token, taker, rpc_url, http=None):
    """Simulate one exact wrapped-native (WETH) entry before a wallet mutation."""
    weth = str(quote_token or "").strip().lower()
    if not is_native_quote_token(weth):
        return None
    tx = (quote or {}).get("transaction")
    if not isinstance(tx, dict) or not tx.get("to") or not tx.get("data"):
        return False, "entry_simulation_malformed: exact route transaction unavailable"
    overrides = {}
    for pre in (quote or {}).get("pre_transactions") or ():
        data = str(pre.get("data") or "").lower()
        if (str(pre.get("to") or "").lower() == str(weth).lower()
                and data.startswith("0x2e1a7d4d")):
            try:
                value = _evm_quantity(tx.get("value") or 0)
            except (TypeError, ValueError):
                return False, "entry_simulation_malformed: exact route value unavailable"
            overrides[taker] = {"balance": hex(max(10 ** 18, value + 10 ** 17))}
            break
    allowance = ((quote or {}).get("issues") or {}).get("allowance") or {}
    spender = allowance.get("spender") if isinstance(allowance, dict) else None
    if spender:
        from sniper.execution.evm_token_safety import _BIG_HEX, _SLOT_CANDIDATES, _nested_slot
        overrides[str(weth).lower()] = {
            "stateDiff": {
                _nested_slot(spender, taker, index): _BIG_HEX
                for index in _SLOT_CANDIDATES
            },
        }
    if not overrides:
        return None
    status, kind, detail = _rpc_eth_call_result(
        tx, taker, rpc_url, http=http, overrides=overrides,
    )
    if status is None:
        # A NODE FAILURE IS RETRIED, NEVER PROVEN (2026-09-22). `status is None` means the node
        # could not run the call, which says nothing about the route -- and this seam refuses an
        # entry on it. One read on a DIFFERENT healthy pool member separates "this node is having a
        # moment" from "the route really cannot execute", at the cost of a single eth_call on the
        # path that was about to refuse anyway. Only one retry: a second failure is a real signal,
        # and the entry window is measured in seconds.
        alternate = _alternate_read_member(rpc_url)
        if alternate:
            status, kind, detail = _rpc_eth_call_result(
                tx, taker, alternate, http=http, overrides=overrides,
            )
    if status is True:
        return True, "ok"
    if status is None:
        # The detail rides along now: an unknown that cannot name what the node said is the row
        # that cost an hour of inference on MUSEFUND.
        suffix = f"; detail={detail}" if detail else ""
        return False, ("entry_simulation_rpc_unknown: exact route simulation unavailable"
                       + suffix)
    # THE CLASSIFIED BRANCHES DROPPED THEIR DETAIL (2026-09-22, ALLOC 09b6cdb8). `min_out` and
    # `transfer_failed` returned a bare label, so the payload that named WHICH minimum failed never
    # reached the row -- and diagnosing ALLOC then needed a live reproduction instead of a query,
    # by which time the curve had moved and the revert could not be reproduced at all. Knowing the
    # class is not knowing the cause; the classified branches deserve their bytes as much as the
    # unclassified one, which has carried them since the first cut of this function.
    if kind == "min_out":
        return False, ("entry_simulation_min_out: exact route minimum output reverted"
                       + (f"; detail={detail}" if detail else ""))
    if kind == "transfer_failed":
        return False, ("entry_simulation_transfer_failed: exact route token transfer failed"
                       + (f"; detail={detail}" if detail else ""))
    suffix = f"; detail={detail}" if detail else ""
    return False, "entry_simulation_reverted: exact route reverted" + suffix


def eth_call_raw(to, data, rpc_url, *, http=None, overrides=None, from_addr=None,
                 raise_on_error=False, block_tag="latest"):
    """eth_call returning the raw hex result, or None if the CALL REVERTED. Used to probe an ERC-20's
    storage layout under a state override — the only portable way to fund a sell simulation
    (eth_simulateV1 is not available on every chain's RPC; Base's public endpoint rejects it).

    Transport failures RAISE — they are not None. A caller scanning candidate storage slots must be
    able to tell "this slot is wrong" (None) from "the RPC blipped" (raise); collapsing the two makes
    the scan skip the correct slot on a transient error and then report the token as unverifiable.
    ``raise_on_error`` keeps JSON-RPC error payloads inconclusive for slot discovery; other callers
    retain the historical None-on-revert contract."""
    call = {"to": to, "data": data}
    if from_addr:
        call["from"] = from_addr
    params = [call, block_tag] + ([overrides] if overrides else [])
    # THE SECOND eth_call_raw (2026-09-22). This module defines its own, distinct from
    # evm_read_rpc.eth_call_raw -- verified, they are not the same object. evm_token_safety and
    # memecoin_evm_price import THIS one, so dc6848b7's coverage was narrower than its message said.
    from sniper.execution.evm_read_rpc import _freshest_read_member
    j = _rpc(_freshest_read_member(rpc_url), "eth_call", params,
             http=http)                                    # transport error propagates
    if "error" in j:
        if raise_on_error:
            raise RuntimeError("eth_call RPC error")
        return None
    return j.get("result")


_last_send_rejection = threading.local()


def last_send_rejection():
    """The node's own words for the most recent POSITIVE broadcast rejection on this thread, or
    None. PAWSINU 0xbcc948e7 (2026-09-17 21:17:56Z) recorded a bare `broadcast_rejected`: the
    marker list below had already classified the message and then discarded it, so the card could
    not say whether the nonce, the gas or the balance was the problem.

    A PEEK. Consumers attributing a message to a send use `take_send_rejection`."""
    return getattr(_last_send_rejection, "message", None)


def take_send_rejection():
    """The words for the send that just happened, consumed so they cannot describe a later one.

    A THREAD-LOCAL IS ISOLATION BETWEEN THREADS, NOT BETWEEN SENDS (E8, 2026-09-23). This slot was
    written in one place -- the classified-marker branch of `eth_send_raw` -- and never cleared,
    while the funded runner reuses its worker threads. Worse, `eth_send_raw` is the executor's
    `send_fn` only when no submission transport is configured; with one (`auto_free`, the funded
    default on every chain) a positive rejection comes back through `aggregate_to_seam` as a bare
    False, having never touched this slot. So a rejected entry on the default path read whatever
    message the thread last set, from an earlier entry and possibly a different coin.

    Taking it, together with the clear at the top of `eth_send_raw`, means a message can describe
    only the send that produced it. On the transport path the answer is now None and the reason
    stays the bare class -- absent rather than wrong, which is the honest answer there: we do not
    have the node's words on that path at all.
    """
    message = getattr(_last_send_rejection, "message", None)
    _last_send_rejection.message = None
    return message


def record_send_rejection(message):
    """Publish the node's own words for the send that just happened on THIS thread (E9).

    `eth_send_raw` is the executor's `send_fn` only when no submission transport is configured.
    With one -- `auto_free`, the funded default on every chain -- a positive rejection comes back
    through `aggregate_to_seam` as a bare False, so the words the provider already gave us never
    reached the executor and the reason stayed the bare class. The transport adapter calls this on
    the CALLER thread, after its fanout has returned and before `send_fn` does, so the message
    lands in the slot `take_send_rejection` reads immediately afterwards.

    Only a POSITIVE rejection calls this. An accepted or ambiguous send writes nothing, leaving
    the slot exactly as the executor emptied it before the send -- absent rather than wrong.
    """
    _last_send_rejection.message = message or None


def eth_send_raw(raw_hex, rpc_url, http=None):
    """Broadcast a signed tx with tri-state acceptance truth.

    Hash/``already_known`` means accepted, ``False`` means the RPC positively rejected this raw
    transaction, and ``None`` means transport or unclassified ambiguity. Callers may release only
    on ``False``; an ambiguous response can still hide an accepted transaction.
    """
    # CLEAR BEFORE THE SEND, not after it: every return below -- accepted, already_known,
    # unclassified, and the transport failure in the `except` -- must leave no message from an
    # EARLIER send on this thread standing for a later reader to attribute to this one.
    _last_send_rejection.message = None
    from sniper.execution.free_endpoint_registry import private_only_refusal
    refusal = private_only_refusal(raw_hex, rpc_url)
    if refusal:
        # Nothing was sent, so False (release-safe) is the truth (spec v2 §3.6).
        _last_send_rejection.message = refusal
        return False
    from sniper.execution import exec_lock
    try:
        intent = exec_lock.intent_send(raw_hex, "rpc")
    except exec_lock.IntentUnwritable as exc:
        # EVM-08: no intent record, no send. Nothing left the host, so False is the truth.
        _last_send_rejection.message = "intent_unwritable: %s" % exc
        return False
    try:
        return _eth_send_raw(raw_hex, rpc_url, http)
    finally:
        # Every broadcast advances its chain's generation, whatever the outcome (Gamma MEDIUM):
        # the 4663 fast lane trusts a wallet snapshot only when it was read after this stamp.
        exec_lock.stamp_send(raw_hex, intent=intent)


# Refusals that do not prove the bytes can never be included, so after another member's 429 in the
# same send (that member may have accepted them) they are ambiguous, never a rejection (review R-01,
# 2026-10-08): our OWN copy mined (nonce too low), pending (replacement underpriced) or having spent
# the balance (insufficient funds), and this node's own view at this moment (its minimum price, the
# base fee it saw, the types it serves; Gamma on 9059898d2). What stays a rejection is what no node
# can ever include: bad RLP or sender, wrong chain id, intrinsic gas, block gas limit.
_STATE_DEPENDENT_REJECTIONS = ("nonce too low", "replacement transaction underpriced",
                               "insufficient funds", "transaction underpriced",
                               "max fee per gas less than block base fee",
                               "transaction type not supported")


def _eth_send_raw(raw_hex, rpc_url, http):
    try:
        j = _rpc(rpc_url, "eth_sendRawTransaction", [raw_hex], http=http)
        result = j.get("result")
        if result:
            return result
        error = j.get("error") or {}
        message = str(error.get("message") if isinstance(error, dict) else error).lower()
        if any(marker in message for marker in (
                "already known", "known transaction", "already imported")):
            return "already_known"
        if j.get("_sniper_send_after_429") and any(
                marker in message for marker in _STATE_DEPENDENT_REJECTIONS):
            return None             # after a 429 this may describe our own forwarded copy (R-01)
        if any(marker in message for marker in (
                "nonce too low", "replacement transaction underpriced", "transaction underpriced",
                "insufficient funds", "intrinsic gas too low",
                "max fee per gas less than block base fee", "invalid sender",
                "invalid transaction", "invalid chain id", "rlp", "exceeds block gas limit",
                "gas required exceeds allowance", "transaction type not supported")):
            _last_send_rejection.message = " ".join(str(
                error.get("message") if isinstance(error, dict) else error).split())[:160]
            return False
        return None
    except Exception:
        return None


def _canonical_receipt_on_rpc(receipt, tx_hash, rpc_url, *, http=None):
    """A nonzero published block must still name this exact receipt on the same endpoint."""
    if not isinstance(receipt, dict):
        return False
    block_hash = receipt.get("blockHash")
    number = receipt.get("blockNumber")
    if (str(receipt.get("transactionHash") or "").lower() != str(tx_hash).lower()
            or not isinstance(block_hash, str) or len(block_hash) != 66
            or not block_hash.startswith("0x") or not isinstance(number, str)
            or not isinstance(receipt.get("logs"), list)):
        return False
    try:
        if int(block_hash, 16) == 0 or int(number, 16) < 0:
            return False
        indexes = []
        for log in receipt["logs"]:
            if (not isinstance(log, dict) or log.get("transactionHash", "").lower()
                    != str(tx_hash).lower() or log.get("blockHash", "").lower()
                    != block_hash.lower()):
                return False
            indexes.append(int(log["logIndex"], 16))
        if len(indexes) != len(set(indexes)):
            return False
        block = _rpc(rpc_url, "eth_getBlockByNumber", [number, False], http=http).get("result")
    except Exception:
        return False
    return (isinstance(block, dict) and str(block.get("hash") or "").lower() == block_hash.lower()
            and block.get("number") == number)


def latest_head(rpc_url, *, http=None):
    """(block number, block timestamp) of the endpoint's latest block. Raises on any failure or a
    malformed answer: the caller refuses on it, so a guess is never returned."""
    block = _rpc(rpc_url, "eth_getBlockByNumber", ["latest", False], http=http).get("result")
    return int(block["number"], 16), int(block["timestamp"], 16)


class ReceiptState(str):
    """'confirmed' or 'failed' from a receipt, plus the receipt's block (fill timing, 2026-10-02).
    It compares, binds into sqlite and JSON-encodes as its plain string, so every caller that tests
    `status == "confirmed"` or `status in (...)` is unchanged; `block_number` is None when unknown."""

    __slots__ = ("block_number", "receipt", "observed_monotonic_ns", "observed_boot_id")

    def __new__(cls, value, *, block_number=None, receipt=None,
                observed_monotonic_ns=None, observed_boot_id=None):
        obj = super().__new__(cls, value)
        obj.block_number = block_number
        obj.receipt = receipt          # the receipt the confirmation read: booking reuses it
        obj.observed_monotonic_ns = observed_monotonic_ns
        obj.observed_boot_id = observed_boot_id
        return obj


def _receipt_block(receipt):
    try:
        return int(str(receipt.get("blockNumber")), 0)
    except (TypeError, ValueError, AttributeError):
        return None


def entry_preparation_at_ingress(trace):
    """Consume a local causal label, never read a generation file or confer funded authority."""
    from sniper.util.receipt_clock import freeze_receipt_clock
    proof = (getattr(trace, '_source_evidence', {}) or {}).get('entry_preparation')
    proof = dict(proof) if isinstance(proof, dict) else {'state': 'unknown', 'reason': 'unavailable'}
    context = trace.receipt_context() if trace is not None and hasattr(trace, 'receipt_context') else {}
    anchor = freeze_receipt_clock(context.get('receipt_clock'))
    ready = None
    if anchor is not None and proof.get('state') == 'cold':
        ready = False
    elif anchor is not None and proof.get('state') == 'warm':
        completed = proof.get('completed_monotonic_ns')
        if type(completed) is int and completed >= 0:
            ready = completed <= anchor['monotonic_ns']
    return proof, ready


def eth_confirm(tx_hash, rpc_url, *, http=None, sleep=None, max_attempts=60, poll_s=2.0,
                fast_poll_s=None, fast_attempts=0, resend_fn=None, resend_at_attempts=(),
                require_canonical=False, stop_fn=None):
    """Poll eth_getTransactionReceipt. 'confirmed' (status 0x1) | 'failed' (0x0, reverted) |
    'unknown' (no receipt within max_attempts -> the tx MAY still land; caller must NOT resubmit).
    ``fast_poll_s`` applies only to the bounded initial window; the long-tail cadence stays at
    ``poll_s`` so a missing receipt cannot create unbounded RPC load."""
    import time as _time
    sleep = sleep or _time.sleep
    first_success = {}
    for attempt in range(max_attempts):
        observed_ns = None
        try:
            rec = _rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash], http=http).get("result")
            if isinstance(rec, dict) and rec.get('status') in ('0x1', '0x01', 1):
                identity = tuple(str(rec.get(key)) for key in (
                    'transactionHash', 'blockNumber', 'blockHash', 'status', 'chainId', 'removed'))
                observed_ns = first_success.setdefault(identity, _time.monotonic_ns())
        except Exception:
            rec = None
        if rec:
            if require_canonical and not _canonical_receipt_on_rpc(rec, tx_hash, rpc_url, http=http):
                rec = None
        if rec:
            st = rec.get("status")
            if st in ("0x1", "0x01", 1):
                return ReceiptState("confirmed", block_number=_receipt_block(rec), receipt=rec,
                                    observed_monotonic_ns=observed_ns,
                                    observed_boot_id=_RECEIPT_BOOT_ID)
            if st in ("0x0", "0x00", 0):
                return ReceiptState("failed", block_number=_receipt_block(rec))
        if resend_fn is not None and attempt in resend_at_attempts:
            # RE-OFFER THE IDENTICAL BYTES (2026-09-16, Arc CIRCLE exit). An accepted transaction
            # that a node dropped waits out this whole poll. The same bytes are the same hash and
            # nonce, so the chain includes them at most once. A failed re-offer changes nothing.
            try:
                resend_fn()
            except Exception:
                logger.warning("receipt wait: identical-bytes re-offer raised tx=%s", tx_hash)
        try:
            # `unknown`, never `failed`: the send may still land (chain-1 Protect replacement).
            if stop_fn is not None and stop_fn():
                return "unknown"
        except Exception:
            logger.exception("receipt wait: stop check raised tx=%s (waiting on)", tx_hash)
        delay = (fast_poll_s if fast_poll_s is not None and attempt < int(fast_attempts)
                 else poll_s)
        sleep(delay)
    return "unknown"


def eth_receipt_state(tx_hash, rpc_url, *, http=None, require_canonical=False):
    """One receipt read, with FOUR answers — 'confirmed' | 'failed' | 'not_found' | 'unknown'.

    `eth_confirm` has only three, and it swallows the transport error (`except: rec = None`), so a
    node that cleanly answers "I have never seen this transaction" and a node that could not be
    reached both come back as `unknown`. Those are different facts with different repairs, and the
    close recovery needs them apart: it branches on `not_found` in four places — the two
    provably-dead proofs that RELEASE a held claim, and the identical-bytes re-offer. `eth_confirm`
    never produces that string, so on EVM all four were unreachable and every dropped sell parked
    in `submitted_unknown` — "never auto-released" — until a human freed it.

    Measured live: position 49252 (PRC, 4663) sat pinned at +23.6% for 7 minutes over ~30 recovery
    checks a minute, every one reporting `unknown`, while the same `eth_getTransactionReceipt`
    answered a clean null from all three configured endpoints. 37331 sat at +48% the same way on
    2026-08-14.

    Fail-closed stays fail-closed: an unreadable provider returns `unknown`, which releases nothing
    and re-offers nothing. Only a node that positively answered "no receipt" yields `not_found`.
    """
    try:
        response = _rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash], http=http)
    except Exception:
        return "unknown"                      # the read failed; we learned nothing about the chain
    if not isinstance(response, dict) or "result" not in response:
        return "unknown"                      # malformed answer is not evidence of absence
    receipt = response.get("result")
    if receipt is None:
        return "not_found"                    # the node answered, and it has no such transaction
    if not isinstance(receipt, dict):
        return "unknown"
    if require_canonical and not _canonical_receipt_on_rpc(receipt, tx_hash, rpc_url, http=http):
        return "unknown"
    status = receipt.get("status")
    if status in ("0x1", "0x01", 1):
        return ReceiptState("confirmed", block_number=_receipt_block(receipt))
    if status in ("0x0", "0x00", 0):
        return ReceiptState("failed", block_number=_receipt_block(receipt))
    return "unknown"                          # mined but statusless (pre-Byzantium shape)


def confirm_receipt_across_pool(tx_hash, rpc_url, *, http=None, attempts=12, poll_s=0.25,
                                sleep=None, candidates=None):
    """Bounded receipt wait that FAILS OVER and does not mistake a bad read for a missing tx.

    `eth_confirm` reads one endpoint and swallows the transport error into "no receipt"
    (`except: rec = None`), so a flaky or lagging node is indistinguishable from an unmined
    transaction. On a short window that difference decides an entry.

    Measured 2026-09-04: every one of the four `native pre-transaction ambiguous` entries this
    rail has ever produced was a false report -- all four unwraps were mined with status 1.
    Attempt 823225ab is the clearest: 0x64ff3b99 mined at 15:43:07 while the poll ran to
    15:43:10.9 and reported nothing. The same hour, the custody endpoint sat 4 blocks behind the
    pool's best and two of the four members answered HTTP errors outright.

    A secondary can only ADD a positive confirmation. `not_found` from a lagging node means "keep
    waiting", never proof of absence, so cross-reading cannot fabricate a miss. Fail-closed is
    unchanged: a window that never reads a receipt returns 'unknown'.
    """
    sleep = sleep or _time.sleep
    if candidates is None:
        from sniper.execution import evm_rpc_pool as _pool
        candidates = [u for u in _pool.pool_for(rpc_url) if _pool.available(u)]
    candidates = list(candidates) or [rpc_url]
    if rpc_url in candidates:                  # the configured primary keeps the first read
        candidates.insert(0, candidates.pop(candidates.index(rpc_url)))
    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        state = eth_receipt_state(tx_hash, candidates[attempt % len(candidates)], http=http)
        if state in ("confirmed", "failed"):
            return state
        if attempt + 1 < attempts:
            sleep(poll_s)
    return "unknown"


def decode_signed_tx_cost(signed_payload_b64):
    """Worst-case wei a signed exit must be able to pay: `gasLimit * gasPrice + value`.

    A node refuses a transaction when that total exceeds the sender balance, WHATEVER the swap
    itself consumes, and it refuses before the mempool -- no receipt, no hash on any node, and the
    account nonce never moves. Position 49252 (PRC, 4663) was signed at gasLimit 1,000,000 x 4.798
    gwei = 0.004798 ETH against 0.00276 ETH of native, and every symptom read as a dropped packet.

    Funded 4663 exits are LEGACY type-0 transactions -- rlp([nonce, gasPrice, gasLimit, to, value,
    data, v, r, s]) -- and the intent stores the `0x…` hex STRING under base64, not raw bytes. Both
    of those cost a reader an afternoon, so they are asserted here rather than assumed.

    Returns {nonce, gas_price, gas_limit, value, worst_case_wei} or None; a type-2 payload adds
    max_priority_fee and its gas_price is maxFeePerGas. Never raises: an
    undecodable payload must leave the caller fail-closed, not crash a recovery pass.
    """
    import base64 as _b64
    try:
        blob = _b64.b64decode(signed_payload_b64)
        text = blob.decode("ascii", "ignore").strip()
        raw = bytes.fromhex(text[2:]) if text[:2].lower() == "0x" else blob
        if raw[:1] == b"\x02":
            # TYPE 2: 0x02 || rlp([chainId, nonce, tip, maxFee, gasLimit, to, value, data,
            # accessList, yParity, r, s]). A node admits it when the balance covers
            # gasLimit x maxFee + value, so maxFee is the price of the bound.
            import rlp as _rlp
            items = _rlp.decode(raw[1:])
            if not isinstance(items, list) or len(items) != 12:
                return None
            _chain, nonce, tip, price, limit, _to, value = (int.from_bytes(x, "big") for x in items[:7])
            if limit <= 0 or price <= 0:
                return None
            return {"nonce": nonce, "gas_price": price, "gas_limit": limit, "value": value,
                    "max_priority_fee": tip, "worst_case_wei": limit * price + value}
        if not raw or raw[0] < 0xc0:
            return None                      # typed (EIP-1559) envelope or junk: not this shape
        i = 1 + (raw[0] - 0xf7) if raw[0] >= 0xf8 else 1

        def _item(j):
            head = raw[j]
            if head < 0x80:
                return raw[j:j + 1], j + 1
            if head < 0xb8:
                n = head - 0x80
                return raw[j + 1:j + 1 + n], j + 1 + n
            width = head - 0xb7
            n = int.from_bytes(raw[j + 1:j + 1 + width], "big")
            return raw[j + 1 + width:j + 1 + width + n], j + 1 + width + n

        out = []
        for _ in range(5):                   # nonce, gasPrice, gasLimit, to, value
            item, i = _item(i)
            out.append(item)
        nonce, price, limit, _to, value = (int.from_bytes(x, "big") if x else 0 for x in out)
    except Exception:
        return None
    if limit <= 0 or price <= 0:
        return None
    return {"nonce": nonce, "gas_price": price, "gas_limit": limit, "value": value,
            "worst_case_wei": limit * price + value}


# WHERE A SWAP CARRIES ITS DEADLINE (eth-mainnet spec v3 §3.7, step 3.2). selector -> the index of
# the uint256 deadline among the calldata's 32-byte head words. AN ALLOWLIST: the dead proof on a
# private-relay chain rests on this number, so a selector that is not here gives no proof and the
# row is held. Every rung a chain in `memecoin_evm_expiry_chains()` can return must build one of
# these (pinned by test).
_SWAP_DEADLINE_WORD = {
    "3593564c": 2,        # UniversalRouter.execute(bytes commands, bytes[] inputs, uint256 deadline)
    "5ae401dc": 0,        # SwapRouter02.multicall(uint256 deadline, bytes[] data)
    "5c11d795": 4,        # Router02.swapExactTokensForTokensSupportingFeeOnTransferTokens(..., deadline)
}
_DEADLINE_MIN, _DEADLINE_MAX = 1_500_000_000, 100_000_000_000      # a UNIX second, not a garbage word


def decode_signed_swap_deadline(signed_payload):
    """The UNIX deadline a signed swap commits to, read from its own bytes, or None.

    One decoder for both recovery paths. The entry row stores the raw `0x…` hex string; the exit
    intent stores that same string under base64 (see `decode_signed_tx_cost`). Legacy and typed
    (EIP-2718) envelopes are both read. None means NO PROOF, never "no deadline": an unknown
    selector, a short head, an implausible number or undecodable bytes all hold the row.
    Never raises.
    """
    import base64 as _b64
    try:
        import rlp
        text = str(signed_payload or "").strip()
        if text[:2].lower() != "0x":
            text = _b64.b64decode(text, validate=True).decode("ascii").strip()
        raw = bytes.fromhex(text[2:])
        if not raw:
            return None
        if raw[0] >= 0xc0:
            data = rlp.decode(raw)[5]                       # [nonce, gasPrice, gas, to, value, data, ...]
        elif raw[0] == 2:
            data = rlp.decode(raw[1:])[7]                   # [chainId, nonce, tip, fee, gas, to, value, data, ...]
        elif raw[0] == 1:
            data = rlp.decode(raw[1:])[6]                   # [chainId, nonce, gasPrice, gas, to, value, data, ...]
        else:
            return None
        index = _SWAP_DEADLINE_WORD.get(bytes(data[:4]).hex())
        if index is None:
            return None
        word = data[4 + 32 * index:4 + 32 * (index + 1)]
        if len(word) != 32:
            return None
        deadline = int.from_bytes(word, "big")
    except Exception:
        return None
    return deadline if _DEADLINE_MIN <= deadline <= _DEADLINE_MAX else None


_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def receipt_token_net(receipt, token, owner):
    """`owner`'s NET atomic change of `token` across one receipt's Transfer logs: in minus out, a
    self transfer moving nothing. Pure. The receipt names only its own transaction, so this is that
    transaction's movement, never a wallet balance window (Alpha 2026-10-06, after Codex EVM-03)."""
    token, owner = str(token).lower(), "0x" + str(owner).lower()[2:].rjust(64, "0")
    net = 0
    for log in (receipt or {}).get("logs") or ():
        topics = [str(t).lower() for t in log.get("topics") or ()]
        data = str(log.get("data") or "").lower()
        if (str(log.get("address") or "").lower() != token or len(topics) < 3
                or topics[0] != _TRANSFER_TOPIC or topics[1] == topics[2]
                or not data.startswith("0x") or len(data) != 66):
            continue
        amount = int(data, 16)
        if topics[2] == owner:
            net += amount
        elif topics[1] == owner:
            net -= amount
    return net


PONS_V2_CURVE_SELL_TOPIC = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"


def pons_curve_sell_received(receipt, owner):
    """Native wei a Pons V2 curve paid `owner` in this receipt, or None (Codex N2, 2026-10-07).

    The only tx-bound native proof we have: a block delta cannot see an outsider's INTERNAL native
    transfer to us, and neither 4663 nor 56 serves traces. CurveSell(maker, recipient; tokensIn,
    quoteOut, fee, fee) is counted only when the contract our transaction called (`receipt.to`)
    emitted it, because the coin's own contract can emit a log of the same shape mid-transaction,
    and only for recipient == owner. quoteOut is the recipient's net: position 70915 (tx
    0x3549b689...) read 23413013829531819 wei, its block balance delta plus gas to the wei."""
    target = str((receipt or {}).get("to") or "").lower()
    me = "0x" + "0" * 24 + str(owner).lower()[2:]
    total = 0
    for log in (receipt or {}).get("logs") or ():
        topics = log.get("topics") or ()
        data = str(log.get("data") or "")
        if (len(topics) < 3 or str(topics[0]).lower() != PONS_V2_CURVE_SELL_TOPIC
                or not target or str(log.get("address") or "").lower() != target
                or str(topics[2]).lower() != me or len(data) < 2 + 64 * 2):
            continue
        total += int(data[2 + 64:2 + 128], 16)
    return total if total > 0 else None


WRAPPED_NATIVE_WITHDRAWAL_TOPIC = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"


def flap_portal_unwrap_received(receipt, owner, wrapped_native):
    """Native wei the flap.sh Portal forwarded to `owner` in OUR sell, or None (2026-10-09).

    A graduated flap coin sold through the Portal routes to the wrapped native, the Portal unwraps
    it (Withdrawal, src = the Portal) and pays us by an internal call, so no log names us. Counted
    only when our own transaction (`receipt.from` == owner) called the Portal (`receipt.to`), and
    only one Withdrawal of the wrapped native whose src is that Portal; a generic router may keep a
    fee after unwrapping, so no other contract qualifies. Position 72574 (tx 0x23b3c527...): the
    unwrap equals our block balance delta plus the gas of the sell and its same-block approve, to
    the wei. A curve-phase Portal sell pays from the Portal's own balance with no unwrap: None."""
    from sniper.execution.evm_flap_curve import FLAP_PORTAL
    target = str((receipt or {}).get("to") or "").lower()
    if (not target or target not in {a.lower() for a in FLAP_PORTAL.values()}
            or str(receipt.get("from") or "").lower() != str(owner).lower()):
        return None
    src = "0x" + "0" * 24 + target[2:]
    received = None
    for log in receipt.get("logs") or ():
        topics = log.get("topics") or ()
        if (str(log.get("address") or "").lower() != str(wrapped_native).lower() or len(topics) < 2
                or str(topics[0]).lower() != WRAPPED_NATIVE_WITHDRAWAL_TOPIC
                or str(topics[1]).lower() != src):
            continue
        if received is not None:
            return None                    # more than one unwrap cannot identify our payout
        try:
            data = str(log.get("data") or "")
            if not re.fullmatch(r"0x[0-9a-fA-F]{64}", data):
                return None
            received = int(data, 16)
        except ValueError:
            return None
        if received <= 0:
            return None
    return received


FLAP_TOKEN_SOLD_TOPIC = "0x03a4693e592f5e75dc7c136acb39b146d2b4966c0e509c34f362dee02b3b861a"
V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"


def flap_portal_sell_received(receipt, owner, wrapped_native):
    """Native wei a CURVE-PHASE flap.sh Portal sell paid `owner`, or None (position 73815).

    The Portal pays from its own BNB balance by an internal call (no Withdrawal; its fee is a
    Deposit), so no Transfer names us. Counted only in our own Portal call, with no Portal
    Withdrawal (that shape is `flap_portal_unwrap_received`'s), exactly one TokenSold
    (ts, token, seller, amount, eth, fee, postPrice) from the Portal whose seller is us, and
    exactly one V2-shaped Swap with sender Portal and to us whose output equals that eth word.
    73815 (tx 0x9416514b...): eth 61000668703544502 wei, the Swap's amount1Out to the wei."""
    from sniper.execution.evm_flap_curve import FLAP_PORTAL
    target = str((receipt or {}).get("to") or "").lower()
    if (not target or target not in {a.lower() for a in FLAP_PORTAL.values()}
            or str(receipt.get("from") or "").lower() != str(owner).lower()):
        return None
    portal_topic = "0x" + "0" * 24 + target[2:]
    me = "0x" + "0" * 24 + str(owner).lower()[2:]
    sold, outs = [], []
    for log in receipt.get("logs") or ():
        address = str(log.get("address") or "").lower()
        topics = [str(t).lower() for t in (log.get("topics") or ())]
        data = str(log.get("data") or "")
        if not topics:
            continue
        if (address == str(wrapped_native).lower() and topics[0] == WRAPPED_NATIVE_WITHDRAWAL_TOPIC
                and len(topics) >= 2 and topics[1] == portal_topic):
            return None                    # an unwrap payout is the other proof's shape
        if address == target and topics[0] == FLAP_TOKEN_SOLD_TOPIC:
            if len(topics) != 1 or not re.fullmatch(r"0x[0-9a-fA-F]{448}", data):
                return None
            if "0x" + data[2 + 128 + 24:2 + 192].lower() == str(owner).lower():
                sold.append(int(data[2 + 256:2 + 320], 16))
        elif (topics[0] == V2_SWAP_TOPIC and len(topics) == 3 and topics[1] == portal_topic
              and topics[2] == me):
            if not re.fullmatch(r"0x[0-9a-fA-F]{256}", data):
                return None
            outs.append({int(data[2 + 128:2 + 192], 16), int(data[2 + 192:2 + 256], 16)} - {0})
    if len(sold) != 1 or sold[0] <= 0 or len(outs) != 1 or outs[0] != {sold[0]}:
        return None
    return sold[0]


def exit_proceeds_atomic(tx_hash, rpc_url, *, owner, quote_token, native, http=None, receipt=None):
    """(atomic, source) a confirmed sell paid us, from its own canonical receipt: the owner's NET
    quote-token inflow ('receipt_net'), else, for a chain whose payout can be native, a venue
    payout event bound to this transaction ('pons_curve_sell') or the flap Portal's unwrap in our
    own sell ('flap_portal_unwrap'), else (None, 'unknown'). Never a
    wallet balance window (Alpha ruling 2026-10-06) and never a block delta (Codex N2)."""
    try:
        if receipt is None:
            receipt = _rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash], http=http).get("result")
            if (not isinstance(receipt, dict)
                    or receipt.get("status") not in ("0x1", "0x01", 1)
                    or not _canonical_receipt_on_rpc(receipt, tx_hash, rpc_url, http=http)):
                return None, "unknown"
        net = receipt_token_net(receipt, quote_token, owner)
    except Exception:
        return None, "unknown"
    if net > 0:
        return net, "receipt_net"
    if native and net == 0:
        atomic = pons_curve_sell_received(receipt, owner)
        if atomic:
            return atomic, "pons_curve_sell"
        atomic = flap_portal_unwrap_received(receipt, owner, quote_token)
        if atomic:
            return atomic, "flap_portal_unwrap"
        atomic = flap_portal_sell_received(receipt, owner, quote_token)
        if atomic:
            return atomic, "flap_portal_sell"
    return None, "unknown"


def erc20_sent_in_receipt(tx_hash, token, sender, rpc_url, *, http=None,
                         require_canonical=False):
    """Exact atomic ERC-20 net spend by one address in a receipt, or None.

    The mirror of `erc20_received_in_receipt`, matching topics[1] (from) instead of topics[2] (to).
    It exists so a spend can be measured from the receipt we already hold rather than from a
    balance read: when the RPC degrades both balance reads fail together, which is how a CONFIRMED
    swap was reported as an anomaly and a real position dropped (WAAI, 2026-09-04).
    """
    try:
        receipt = _rpc(
            rpc_url, "eth_getTransactionReceipt", [tx_hash], http=http,
        ).get("result") or {}
        if receipt.get("status") not in ("0x1", "0x01", 1):
            return None
        if str(receipt.get("transactionHash") or "").lower() != str(tx_hash).lower():
            return None
        if require_canonical and not _canonical_receipt_on_rpc(
                receipt, tx_hash, rpc_url, http=http):
            return None
        token = str(token).lower()
        sender = str(sender).lower()
        if not (token.startswith("0x") and len(token) == 42
                and sender.startswith("0x") and len(sender) == 42):
            return None
        sender_topic = "0x" + sender[2:].rjust(64, "0")
        total = 0
        for log in receipt.get("logs") or ():
            topics = log.get("topics") or ()
            data = str(log.get("data") or "").lower()
            if (str(log.get("address") or "").lower() != token
                    or len(topics) < 3
                    or str(topics[0]).lower() != _TRANSFER_TOPIC
                    or not data.startswith("0x") or len(data) != 66):
                continue
            amount = int(data, 16)
            if str(topics[1]).lower() == sender_topic:
                total += amount
            if str(topics[2]).lower() == sender_topic:
                total -= amount
        return total if total > 0 else None
    except Exception:
        return None


def erc20_received_in_receipt(tx_hash, token, recipient, rpc_url, *, http=None,
                              require_canonical=False):
    """Return the exact atomic ERC-20 amount sent to one address, or None."""
    try:
        receipt = _rpc(
            rpc_url, "eth_getTransactionReceipt", [tx_hash], http=http,
        ).get("result") or {}
        if receipt.get("status") not in ("0x1", "0x01", 1):
            return None
        if str(receipt.get("transactionHash") or "").lower() != str(tx_hash).lower():
            return None
        if require_canonical and not _canonical_receipt_on_rpc(
                receipt, tx_hash, rpc_url, http=http):
            return None
        token = str(token).lower()
        recipient = str(recipient).lower()
        if not (token.startswith("0x") and len(token) == 42
                and recipient.startswith("0x") and len(recipient) == 42):
            return None
        recipient_topic = "0x" + recipient[2:].rjust(64, "0")
        total = 0
        for log in receipt.get("logs") or ():
            topics = log.get("topics") or ()
            data = str(log.get("data") or "").lower()
            if (str(log.get("address") or "").lower() != token
                    or len(topics) < 3
                    or str(topics[0]).lower() != _TRANSFER_TOPIC
                    or str(topics[2]).lower() != recipient_topic
                    or not data.startswith("0x") or len(data) != 66):
                continue
            total += int(data, 16)
        return total if total > 0 else None
    except Exception:
        return None


def native_received_in_receipt(tx_hash, recipient, rpc_url, *, http=None,
                               allow_block_delta=True):
    """Return native coin received by the sender in a confirmed zero-value transaction."""
    if not allow_block_delta:
        return None  # multiple custody transactions in one Ink block make this attribution false
    try:
        receipt = _rpc(
            rpc_url, "eth_getTransactionReceipt", [tx_hash], http=http,
        ).get("result") or {}
        if receipt.get("status") not in ("0x1", "0x01", 1):
            return None
        if str(receipt.get("transactionHash") or "").lower() != str(tx_hash).lower():
            return None
        recipient = str(recipient).lower()
        if not (recipient.startswith("0x") and len(recipient) == 42):
            return None
        tx = _rpc(rpc_url, "eth_getTransactionByHash", [tx_hash], http=http).get("result") or {}
        if (str(tx.get("hash") or "").lower() != str(tx_hash).lower()
                or str(tx.get("from") or "").lower() != recipient
                or _evm_quantity(tx.get("value") or 0) != 0):
            return None
        block = _evm_quantity(receipt.get("blockNumber"))
        if block <= 0:
            return None
        before = _evm_quantity(_rpc(
            rpc_url, "eth_getBalance", [recipient, hex(block - 1)], http=http,
        ).get("result"))
        after = _evm_quantity(_rpc(
            rpc_url, "eth_getBalance", [recipient, hex(block)], http=http,
        ).get("result"))
        gas = (_evm_quantity(receipt.get("gasUsed"))
               * _evm_quantity(receipt.get("effectiveGasPrice")))
        received = after - before + gas
        return received if received > 0 else None
    except (TypeError, ValueError):
        return None
    except Exception:
        return None


# --- revert-reason decoding (2026-07-17) ---------------------------------------------------------
# Three real 4663 entries reverted 'Too little received' during violent pumps: the pool moved
# 13.6%/3.5% between quote and landing. That is QUOTE MOVEMENT — categorically different from a
# honeypot or a generic failure — and the executor may retry it ONCE with a fresh quote. Anything
# we cannot positively identify as a min-out revert stays 'unknown' (fail-closed: no retry).

_ERROR_STRING_SELECTOR = "08c379a0"                    # Error(string)


def _min_out_marks():
    from eth_utils import keccak
    return (
        b"Too little received".hex(),                  # V3 SwapRouter / SwapRouter02 exactInput*
        b"Return amount is not enough".hex(),          # Relay route minimum return
        b"INSUFFICIENT_OUTPUT_AMOUNT".hex(),           # V2 Router02 min-out require
        keccak(b"V4TooLittleReceived(uint256,uint256)")[:4].hex(),   # V4 router custom error
        keccak(b"TooMuchSlippage(address,uint256,uint256)")[:4].hex(),  # Relay approval proxy
    )


_MIN_OUT_HEX_MARKS = _min_out_marks()
_MIN_OUT_TEXTS = ("too little received", "return amount is not enough",
                  "insufficient_output_amount", "v4toolittlereceived", "err_exceeded_slippage")


def _transfer_failed_mark():
    from eth_utils import keccak
    return keccak(b"ERC20TransferFailed()")[:4].hex()


_TRANSFER_FAILED_HEX_MARK = _transfer_failed_mark()


def _flap_slippage_amounts(data):
    """Flap IPortal's SlippageTooHigh(uint256,uint256), only when its minimum was missed."""
    if not isinstance(data, str):
        return None
    blob = data.lower()
    if len(blob) != 138 or not blob.startswith("0x76baadda") or blob[2:].strip(_HEX_DIGITS):
        return None
    actual, minimum = int(blob[10:74], 16), int(blob[74:], 16)
    return (actual, minimum) if actual < minimum else None


def classify_evm_revert(data, message=None):
    """'min_out' iff the revert payload/message carries a known router min-out signature, else
    'unknown'. Substring match on the raw hex blob deliberately survives nested wrappers (the
    Universal Router re-wraps sub-command reverts in ExecutionFailed(uint256,bytes))."""
    blob = ""
    if isinstance(data, dict):                          # some clients nest {'data': '0x…'}
        data = data.get("data")
    if isinstance(data, str):
        blob = data.lower().replace("0x", "")
    if blob.startswith("76baadda"):
        return "min_out" if _flap_slippage_amounts(data) is not None else "unknown"
    if _TRANSFER_FAILED_HEX_MARK in blob:
        return "transfer_failed"
    for mark in _MIN_OUT_HEX_MARKS:
        if mark in blob:
            return "min_out"
    msg = str(message or "").lower()
    if msg.strip() in {"tf", "stf"} or msg.strip().endswith((": tf", ": stf")):
        return "transfer_failed"
    for text in _MIN_OUT_TEXTS:
        if text in msg:
            return "min_out"
    return "unknown"


_PANIC_SELECTOR = "4e487b71"                            # Panic(uint256)
_HEX_DIGITS = "0123456789abcdef"

# Selectors whose NAME we have verified by keccak against the signature. A bare `custom_error
# 0x025ac17e` sent the operator to a 4-byte directory that 403s us and does not carry this chain's
# contracts anyway; the name is the whole difference between a refusal that explains itself and one
# that needs an hour of bytecode reading. Add an entry only with the keccak checked -- a guessed
# name is worse than the hex, because the hex at least announces that it is unidentified.
_KNOWN_ERROR_SELECTORS = {
    "486aa307": "PoolNotInitialized()",     # Uniswap V4 PoolManager
    "025ac17e": "CurveGraduated()",         # Pons bonding curve, chain 4663
}


def _revert_detail(data, message=None, *, _depth=0):
    """Human detail for a revert, KEEPING the selector when the payload is not ``Error(string)``.

    A custom error is four bytes of selector plus args and decodes to nothing here. Uniswap V3/V4
    and most modern tokens revert that way, so `_decode_revert_string(...) or message` collapsed
    every one of them to the node's generic "execution reverted" and threw away the only bytes that
    say WHICH failure it was. The stored row then could not attribute the refusal: 13 funded 4663
    entries in 14 days carry `sell_sim_reverted_unknown ... detail=execution reverted`, which names
    the boundary rather than the cause. Same defect as the custody probe in fdc9c8ee — the layer
    below knew and the boundary discarded it.

    Diagnostic only. The caller's status/kind and every fail-closed decision are unchanged.
    """
    if isinstance(data, dict):                          # some clients nest {'data': '0x…'}
        data = data.get("data")
    amounts = _flap_slippage_amounts(data)
    if amounts is not None:
        return "SlippageTooHigh(uint256,uint256): actual=%s minimum=%s 0x76baadda" % amounts
    text = _decode_revert_string(data)
    if text:
        return text
    blob = str(data or "").lower()
    if blob.startswith("0x"):
        blob = blob[2:]
    # The blob must be HEX before any of it is called a selector. Without this, a client that puts
    # prose in `data` gets it promoted to an authoritative-looking identifier: "reverted" became
    # `custom_error 0xreverted` and "out of gas" became `custom_error 0xout of g`. A fabricated
    # attribution is worse than an absent one -- "execution reverted" announces that it knows
    # nothing, while an invented selector sends the next reader to 4byte to search for something
    # that was never on chain. Not hex, or not whole bytes -> fall through to the node message.
    if len(blob) >= 8 and len(blob) % 2 == 0 and not blob.strip(_HEX_DIGITS):
        selector, payload = blob[:8], blob[8:72]
        if selector == "90bfb865":                 # WrappedError(address,bytes4,bytes,bytes)
            head = "custom_error 0x90bfb865 WrappedError"  # Keep the safety attribution prefix.
            raw = "%s payload=0x%s" % (head, payload) if payload else head
            if len(blob) > 32768:
                return raw + " undecoded=oversized"
            if _depth >= 2:
                return raw + " undecoded=depth_limit"
            try:
                from eth_abi import decode
                target, callback, reason, details = decode(
                    ["address", "bytes4", "bytes", "bytes"], bytes.fromhex(blob[8:]))
            except Exception:                     # Diagnostics must not obscure the refusal.
                return raw + " undecoded=malformed"

            def inner(value):
                encoded = "0x" + value.hex()
                text = _revert_detail(encoded, _depth=_depth + 1) or encoded
                return text if len(text) <= 384 else text[:381] + "..."

            detail = (f"{head} target={target} callback=0x{callback.hex()} "
                      f"reason=[{inner(reason)}] details=[{inner(details)}]")
            # The retry reader matches these outer-selector strings anywhere in the reason.
            for marker in ("0x486aa307", "0x025ac17e"):
                detail = detail.replace(marker, marker[2:])
            detail = detail.replace("route_over_budget_marginal", "route_over_budget:marginal")
            # Hook text cannot supply the safety layer's independent token-move proof marker.
            return detail.replace("token_pull_ok=", "token_pull_ok:")
        if selector == _PANIC_SELECTOR:
            try:
                return "Panic(0x%x)" % int(payload, 16)
            except ValueError:
                return "Panic(undecodable)"
        name = _KNOWN_ERROR_SELECTORS.get(selector)
        head = "%s 0x%s" % (name, selector) if name else "custom_error 0x%s" % selector
        return ("%s payload=0x%s" % (head, payload) if payload else head)
    return (str(message or "")[:120] or None)


def _decode_revert_string(data):
    """Best-effort Error(string) payload decode for human logging; None when not decodable."""
    try:
        blob = str(data or "").lower().replace("0x", "")
        if not blob.startswith(_ERROR_STRING_SELECTOR):
            return None
        body = bytes.fromhex(blob[8:])
        length = int.from_bytes(body[32:64], "big")
        return body[64:64 + length].decode("utf-8", "replace") or None
    except Exception:
        return None


def _pcs_v2_floor_probe(tx):
    """Relax only the final PCS mixed-hop V2 minimum in diagnostic calldata."""
    from eth_abi import decode, encode
    from sniper.execution.evm_mixed_hop_router import smart_router_for
    try:
        if _evm_quantity(tx.get("chainId")) != 56 or \
                str(tx.get("to")).lower() != smart_router_for("56"):
            return None
        raw = bytes.fromhex(tx["input"][2:])
        if raw[:4].hex() != "5ae401dc":
            return None
        deadline, legs = decode(["uint256", "bytes[]"], raw[4:])
        if len(legs) != 2 or legs[0][:4].hex() != "b858183f" or \
                legs[-1][:4].hex() != "472b43f3":
            return None
        args = list(decode(["uint256", "uint256", "address[]", "address"], legs[-1][4:]))
        minimum = args[1]
        if args[0] != 0 or minimum <= 1 or len(args[2]) != 2:
            return None
        # Reject noncanonical/trailing data; the probe must change exactly one ABI word.
        if raw[4:] != encode(["uint256", "bytes[]"], [deadline, legs]) or \
                legs[-1][4:] != encode(["uint256", "uint256", "address[]", "address"], args):
            return None
        args[1] = 1
        loose = legs[-1][:4] + encode(["uint256", "uint256", "address[]", "address"], args)
        return ("0x" + (raw[:4] + encode(["uint256", "bytes[]"],
                                        [deadline, [legs[0], loose]])).hex(), minimum)
    except Exception:
        return None


def _verified_prefix_revert(tx, rec, rpc_url, *, http=None, floor_probe=None):
    """Classify only a receipt-matched prefix; optionally prove an empty PCS V2 min-out."""
    block_hash = rec.get("blockHash") or tx.get("blockHash")
    index = _evm_quantity(tx["transactionIndex"])
    block = _rpc(rpc_url, "eth_getBlockByHash", [block_hash, True], http=http).get("result") or {}
    transactions = block.get("transactions") or []
    if index >= len(transactions) or str(transactions[index].get("hash")).lower() != \
            str(tx.get("hash")).lower():
        return None

    calls = []
    simulated_txs = []
    for item in transactions[:index + 1]:
        if _evm_quantity(item.get("gas") or 0) == 0:
            if str(item.get("type") or "").lower() != "0x6a":
                return None
            continue
        call = {key: item[key] for key in ("from", "to", "gas", "value", "input")
                if item.get(key) is not None}
        if item.get("gasPrice") is not None:
            call["gasPrice"] = item["gasPrice"]
        calls.append(call)
        simulated_txs.append(item)
    if not calls or simulated_txs[-1].get("hash", "").lower() != tx.get("hash", "").lower():
        return None

    receipts = _rpc(
        rpc_url, "eth_getBlockReceipts", [block.get("number")], http=http,
    ).get("result") or []
    receipts_by_hash = {
        str(item.get("transactionHash") or "").lower(): item for item in receipts
    }
    overrides = {
        "number": block.get("number"), "time": block.get("timestamp"),
        "gasLimit": block.get("gasLimit"), "baseFeePerGas": block.get("baseFeePerGas"),
    }
    if any(value is None for value in overrides.values()):
        return None
    if floor_probe:
        # The original failed call rolls back; the probe therefore sees the SAME prefix state.
        calls.append({**calls[-1], "input": floor_probe[0]})
    request = {
        "blockStateCalls": [{"blockOverrides": overrides, "calls": calls}],
        "validation": False, "traceTransfers": False,
    }
    parent = hex(_evm_quantity(block["number"]) - 1)
    rows = (_rpc(rpc_url, "eth_simulateV1", [request, parent], http=http).get("result")
            or [{}])[0].get("calls") or []
    if len(rows) != len(simulated_txs) + bool(floor_probe):
        return None

    def logs(items):
        return [(str(item.get("address") or "").lower(),
                 tuple(str(topic).lower() for topic in item.get("topics") or ()),
                 str(item.get("data") or "").lower()) for item in items or ()]

    for item, row in zip(simulated_txs, rows):
        actual = receipts_by_hash.get(str(item.get("hash") or "").lower())
        if not actual or _evm_quantity(row.get("status") or 0) != \
                _evm_quantity(actual.get("status") or 0):
            return None
        if logs(row.get("logs")) != logs(actual.get("logs")):
            return None
        if floor_probe and _evm_quantity(row.get("gasUsed")) != _evm_quantity(actual.get("gasUsed")):
            return None
    if floor_probe:
        from eth_abi import decode
        if _evm_quantity(rows[-2].get("status") or 0) != 0 or \
                _evm_quantity(rows[-2].get("gasUsed")) != _evm_quantity(rec.get("gasUsed")):
            return None
        if _evm_quantity(rows[-1].get("status") or 0) == 1:
            outputs, = decode(["bytes[]"], bytes.fromhex(rows[-1]["returnData"][2:]))
            if len(outputs) == 2 and all(len(value) == 32 for value in outputs):
                amount = int.from_bytes(outputs[-1], "big")
                if 0 < amount < floor_probe[1]:
                    return "min_out", ("PCS V2 minimum output: actual=%s minimum=%s"
                                       % (amount, floor_probe[1]))
    err = rows[len(simulated_txs) - 1].get("error") or {}
    if not err:
        return None
    return classify_evm_revert(err.get("data"), err.get("message")), \
        _revert_detail(err.get("data"), err.get("message"))


def evm_revert_reason(tx_hash, rpc_url, *, http=None):
    """Classify a mined transaction revert.

    Replay the block-end state first. Simulate the verified prefix when that succeeds or a
    recognized PCS mixed-hop route needs proof of its empty V2 minimum-output revert.
    Return ``unknown`` after a transport fault or a receipt mismatch.
    """
    def evidence(result, basis):
        kind, detail = result
        return kind, "replay_basis=%s; %s" % (basis, detail or "no revert detail")

    try:
        tx = _rpc(rpc_url, "eth_getTransactionByHash", [tx_hash], http=http).get("result") or {}
        rec = _rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash], http=http).get("result") or {}
        block = rec.get("blockNumber")
        if not tx.get("to") or not block:
            return "unknown", None
        call = {"from": tx.get("from"), "to": tx.get("to"), "data": tx.get("input") or "0x",
                "value": tx.get("value") or "0x0"}
        if tx.get("gas"):
            call["gas"] = tx["gas"]
        j = _rpc(rpc_url, "eth_call", [call, block], http=http)
        err = j.get("error")
        if not err:
            probe = _pcs_v2_floor_probe(tx) if _evm_quantity(rec.get("status")) == 0 else None
            prefix = _verified_prefix_revert(tx, rec, rpc_url, http=http, floor_probe=probe)
            return (evidence(prefix, "verified_transaction_prefix") if prefix
                    else evidence(("unknown", "replay did not revert"), "unknown"))
        data = err.get("data")
        if isinstance(data, dict):
            data = data.get("data")
        kind = classify_evm_revert(data, err.get("message"))
        detail = _revert_detail(data, err.get("message"))
        if kind == "unknown" and data in (None, "0x", "") and \
                _evm_quantity(rec.get("status")) == 0 and \
                _is_revert_evidence(data, err.get("message"), kind):
            probe = _pcs_v2_floor_probe(tx)
            if probe:
                prefix = _verified_prefix_revert(tx, rec, rpc_url, http=http, floor_probe=probe)
                if prefix:
                    return evidence(prefix, "verified_transaction_prefix")
        return evidence((kind, detail), "block_end" if _is_revert_evidence(data, err.get("message"), kind) else "unknown")
    except Exception:
        return "unknown", None


# --- signing + ERC-20 approve encoding ---------------------------------------------------------

def _type2_tip_wei(tx_dict):
    """The tip when `tx_dict`'s chain signs type 2 (listed in MEMECOIN_EVM_TYPE2_CHAINS with a
    positive tip), else None. Keyed on str(int(chainId)): 8453, "8453" and "0x2105" are one chain."""
    from sniper.settings import memecoin as cfg
    try:
        chain = str(int(str(tx_dict.get("chainId")), 0))
    except (TypeError, ValueError):
        return None
    if chain not in cfg.memecoin_evm_type2_chains():
        return None
    tip = cfg.evm_type2_tip_wei_for(chain)
    from sniper.execution import chain1_protect
    dynamic = chain1_protect.dynamic_tip_wei(chain, tip)      # MEMECOIN_CHAIN1_FEE_HEADROOM
    return tip if dynamic is None else dynamic


def sign_tx(tx_dict, acct, fee_floor=None, tip_floor_wei=None):
    """Sign a legacy tx dict with `acct`; return (raw_hex, tx_hash) or None (fail-closed). Normalises
    `to` to an EIP-55 checksum first: eth_account rejects a non-checksummed address ('Transaction had
    invalid fields'), and venue routers / Relay contracts are frequently returned lowercase. Single choke
    point for every EVM signer (_approve / _approve_token / _sign), so no caller can hit this again.

    TYPE 2 (spec 2026-09-30-memecoin-evm-type2-signing-design.md): on a chain that signs type 2 the
    caller's `gasPrice` becomes maxFeePerGas, unchanged, and the tip is capped at it. The transaction
    pays base fee + tip and keeps the rest. Every other chain signs the same legacy bytes as before.

    `fee_floor` = (maxFeePerGas, maxPriorityFeePerGas) a same-nonce replacement must reach (chain-1
    Protect, `chain1_protect.replacement_fee_floor`); every other caller passes None.

    `tip_floor_wei`: the caller's independent entry or exit tip floor. Type 2 only: tip = max(tip,
    floor), then the same maxFee cap, and the tip never above maxFee. A legacy chain ignores it."""
    from eth_utils import to_checksum_address
    try:
        if tx_dict.get("to"):
            tx_dict = {**tx_dict, "to": to_checksum_address(tx_dict["to"])}
        tip = _type2_tip_wei(tx_dict) if "gasPrice" in tx_dict else None
        if tip is not None:
            tip = max(tip, int(tip_floor_wei or 0))
            from sniper.execution import chain1_protect
            # Chain 1 with MEMECOIN_CHAIN1_FEE_HEADROOM: never below the caller's price, never past
            # the per-tx native gas reserve over this gas limit; every other chain is unchanged.
            cap = chain1_protect.headroom_max_fee_wei(
                str(int(str(tx_dict.get("chainId")), 0)), int(tx_dict["gasPrice"]),
                tx_dict.get("gas"), tip)
            tip = min(tip, cap)
            if fee_floor is not None:
                cap, tip = max(cap, int(fee_floor[0])), max(tip, int(fee_floor[1]))
                cap = max(cap, tip)
            tx_dict = {**{k: v for k, v in tx_dict.items() if k != "gasPrice"},
                       "type": 2, "maxFeePerGas": cap, "maxPriorityFeePerGas": tip}
        elif fee_floor is not None and "gasPrice" in tx_dict:
            tx_dict = {**tx_dict, "gasPrice": max(int(tx_dict["gasPrice"]), int(fee_floor[0]))}
        signed = acct.sign_transaction(tx_dict)
        raw = signed.raw_transaction.hex()
        h = signed.hash.hex()
        raw = raw if raw.startswith("0x") else "0x" + raw
        h = h if h.startswith("0x") else "0x" + h
        return raw, h
    except Exception:
        return None


def encode_approve(spender, amount):
    """ERC-20 approve(spender, amount) calldata — EXACT amount (never max-uint)."""
    return ("0x" + SEL_APPROVE + spender[2:].lower().rjust(64, "0")
            + format(int(amount), "x").rjust(64, "0"))


# --- factory -----------------------------------------------------------------------------------

def build_real_evm_executor(*, acct, chain_id, rpc_url, http=None, venue_quote_fn=None, usdc_fn=None,
                            token_fn=None, approve_gas=100_000, default_swap_gas=500_000,
                            price_fn=None, gasprice_buffer=1.5, decimals_fn=None, cached_decimals_fn=None,
                            submission_transport=None, backend_label="rpc", now_fn=None,
                            lifi_entry_fn=None, lifi_preview_fn=None, lifi_exit_fn=None,
                            lifi_safety_fn=None, force_lifi=False):
    """Return {execute_fn, address, chain_id, usdc_fn, token_fn, exit seams}. execute_fn matches
    real_entry's (conn, pid, mint, usd, now_iso) contract, recovering source_user_id + strategy from
    the pending row. The exit executor (item 9) reuses the same bound seams via the returned dict.
    The chain's QUOTE token (CHAIN_QUOTE: USDC, or WETH on 4663) is what entries spend and exits
    receive. Risk reads use the current USD value. Each execution freezes one quote-token USD price
    and applies it to the raw balance delta; subtracting two whole-wallet USD values at different ETH
    marks corrupts a small fill when the wallet holds materially more WETH than the trade."""
    from sniper.execution.evm_swap_executor import execute_entry_evm
    from sniper.settings import memecoin as cfg

    address = acct.address
    chain = str(chain_id)
    from sniper.services.memecoin_bsc_execution_observation import create_publisher
    bsc_execution_observer = create_publisher(chain)
    if chain in cfg.memecoin_evm_type2_chains() and cfg.evm_type2_tip_wei_for(chain) is None:
        logger.error("MEMECOIN_EVM_TYPE2_CHAINS lists chain %s but MEMECOIN_EVM_TIP_GWEI_%s is not a "
                     "positive number: chain %s signs LEGACY transactions", chain, chain, chain)
    if chain == "1" and cfg.memecoin_chain1_fee_headroom():
        # One warm fee-history reader per process (chain1_protect P1), never on the fill path.
        from sniper.execution import chain1_protect
        if "1" not in chain1_protect._HEADROOM:
            from sniper.execution.evm_wallet import _rpc as _fee_rpc
            headroom = chain1_protect.FeeHeadroom(lambda: chain1_protect.fee_history_result(_fee_rpc(
                rpc_url, "eth_feeHistory", [hex(chain1_protect._FEE_BLOCKS), "latest", [50]],
                http=http)))
            chain1_protect.register("1", headroom)
            chain1_protect.start_refresher(headroom)
    quote = CHAIN_QUOTE.get(chain) or {"token": CHAIN_USDC.get(chain), "decimals": 6, "kind": "usd"}
    quote_token = quote["token"]
    # Sizing and accounting share one native/USD mark for at most one second. BSC can prepare the
    # next mark off-path; selecting it preserves its original age and the current sizing window.
    _price_window_s = 1.0
    _price_memo = {"at": None, "px": None, "source": "native_price_live", "pending": None,
                   "prepared": None, "last_error": None, "warmer": None}
    _price_lock = threading.Lock()
    _base_price_fn = price_fn

    def _selected_price(now):
        # Called under the existing memo lock; preparation never resets the selected lifetime.
        at, px = _price_memo["at"], _price_memo["px"]
        prepared = _price_memo["prepared"]
        if (at is None or not 0 <= now - at < _price_window_s) and prepared is not None:
            prepared_at, prepared_px = prepared
            if 0 <= now - prepared_at < _price_window_s:
                _price_memo.update(at=prepared_at, px=prepared_px, source="native_price_prepared")
                at, px = prepared
        return px if at is not None and 0 <= now - at < _price_window_s else None

    def _preparation_http(method, url, **kwargs):
        # Background polling must not consume the existing live fallback's paid FMP quota.
        if urllib.parse.urlsplit(url).hostname == "financialmodelingprep.com":
            return 503, None, None
        status, payload, detail = (http or _http)(method, url, **kwargs)
        return status, payload if status == 200 else None, detail

    def _read_quote_price(preparing=False):
        with _price_lock:
            if not preparing:
                px = _selected_price(_time.monotonic())
                if px is not None:
                    return px
            pending = _price_memo["pending"]
            # Optional preparation cannot hold a stale funded read behind its provider wait.
            owner = pending is None or (not preparing and pending[1] and not pending[0].done())
            if owner:
                pending = _price_memo["pending"] = (Future(), preparing)
        if not owner:
            px, was_preparing = pending[0].result()
            # A failed Binance-only preparation does not replace the ordinary live/FMP fallback.
            return _live_quote_price() if not preparing and was_preparing else px
        try:
            px = (_base_price_fn() if _base_price_fn else native_usd_price(
                chain, http=_preparation_http if preparing else http))
            at = _time.monotonic()
            if preparing and (px is None or not math.isfinite(px) or px <= 0):
                px = None
            with _price_lock:
                if px:
                    _price_memo["prepared"] = (at, px)
                    if not preparing:
                        _price_memo.update(at=at, px=px, source="native_price_live")
                if preparing:
                    _price_memo["last_error"] = None if px else "unavailable"
                pending[0].set_result((px, preparing))
                if _price_memo["pending"] is pending:
                    _price_memo["pending"] = None
            return px
        except BaseException as exc:
            with _price_lock:
                if preparing:
                    _price_memo["last_error"] = type(exc).__name__
                    pending[0].set_result((None, True))
                else:
                    pending[0].set_exception(exc)
                if _price_memo["pending"] is pending:
                    _price_memo["pending"] = None
            if preparing and isinstance(exc, Exception):
                return None
            raise

    def _live_quote_price():
        return _read_quote_price()

    def _start_quote_price_warmer():
        from sniper.execution.submission_backends import _register_warmer

        def refresh(stop):
            backoff = .5
            while not stop.is_set():
                try:
                    px = _read_quote_price(preparing=True)
                except Exception as exc:
                    with _price_lock:
                        _price_memo["last_error"] = type(exc).__name__
                    px = None
                backoff = .5 if px else min(30.0, backoff * 2)
                stop.wait(backoff)

        with _price_lock:
            if _price_memo["warmer"] is None:
                stop = threading.Event()
                thread = threading.Thread(target=refresh, args=(stop,),
                                          name="bsc-quote-price", daemon=True)
                _price_memo["warmer"] = (stop, thread)
                _register_warmer(stop, thread)
                try:
                    thread.start()
                except (RuntimeError, OSError) as exc:
                    stop.set()
                    _price_memo["last_error"] = type(exc).__name__
            return _price_memo["warmer"]

    def _quote_price_status():
        if not _price_lock.acquire(blocking=False):
            return {"ready": None, "last_error": "busy", "thread_alive": None}
        try:
            now = _time.monotonic()
            prepared = _price_memo["prepared"]
            selected_at = _price_memo["at"]
            ready = ((selected_at is not None and 0 <= now - selected_at < _price_window_s)
                     or (prepared is not None and 0 <= now - prepared[0] < _price_window_s))
            warmer = _price_memo["warmer"]
            alive = bool(warmer and warmer[1].is_alive())
            return {"ready": ready, "last_error": _price_memo["last_error"], "thread_alive": alive,
                    # Seconds since the freshest price (2026-10-15): the early quote needs < 1 s.
                    "age_s": freshest_price_age_s(now, selected_at, prepared)}
        finally:
            _price_lock.release()

    price_fn = _live_quote_price
    injected_usdc_fn = usdc_fn
    usdc_fn = usdc_fn or (lambda: quote_balance_usd_or_none(
        address, chain, rpc_url, http=http, price_fn=price_fn))
    injected_token_fn = token_fn
    token_fn = token_fn or (lambda mint: erc20_balance(
        address, mint, rpc_url, http=http, decimals_fn=decimals_fn))
    entry_token_fn = injected_token_fn or (lambda mint: erc20_balance_with_decimals(
        address, mint, rpc_url, http=http, decimals_fn=decimals_fn))
    # Reconciliation needs a read that can FAIL rather than report a confident zero (see
    # erc20_balance_checked). An injected token_fn is a test seam and is trusted as-is.
    token_checked_fn = injected_token_fn or (lambda mint: erc20_balance_checked(
        address, mint, rpc_url, http=http, decimals_fn=decimals_fn))

    _booking_receipt, _confirmed_receipt = {}, {}

    def _canonical_receipt(tx_hash):
        """The swap's status-1 receipt, canonical on this endpoint, read once per transaction for
        both booking amounts (every chain): the confirmation's own receipt when it has one, else a
        read. None, and not remembered, while it proves nothing: a retry reads again."""
        if tx_hash in _booking_receipt:
            return _booking_receipt[tx_hash]
        receipt = _confirmed_receipt.pop(str(tx_hash).lower(), None)
        if receipt is None:
            try:
                receipt = _rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash],
                               http=http).get("result")
            except Exception:
                return None
        if (not isinstance(receipt, dict) or receipt.get("status") not in ("0x1", "0x01", 1)
                or not _canonical_receipt_on_rpc(receipt, tx_hash, rpc_url, http=http)):
            return None
        _booking_receipt.clear()
        _booking_receipt[tx_hash] = receipt
        return receipt

    def _receipt_token(tx_hash, token):
        """The coin this swap NETTED to us, from its own canonical receipt (in minus out)."""
        receipt = _canonical_receipt(tx_hash)
        atomic = receipt_token_net(receipt, token, address) if receipt is not None else 0
        if atomic <= 0:
            return None
        decimals = decimals_fn(token) if decimals_fn is not None else erc20_decimals(
            token, rpc_url, http=http,
        )
        if decimals is None:
            return None
        return atomic / (10 ** int(decimals)), int(decimals)

    def _receipt(tx_hash):
        """The raw receipt, for the executed-pool decode after a fill. None on any failure. The
        canonical receipt the booking used when it has one (no second read)."""
        if tx_hash in _booking_receipt:
            return _booking_receipt[tx_hash]
        try:
            receipt = _rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash], http=http).get("result")
            if chain in {"1", "57073"} and not _canonical_receipt_on_rpc(
                    receipt, tx_hash, rpc_url, http=http):
                return None
            return receipt
        except Exception:
            return None

    def _receipt_spent(tx_hash):
        """Quote-token amount this transaction actually SPENT, read from the chain, not a balance.

        The balance-delta measurement needs two reads and both fail together when the RPC degrades,
        which is how a CONFIRMED swap became an `anomaly` and a real position was dropped (WAAI,
        2026-09-04). The transaction itself already carries the answer: a WETH-in entry moves it as
        a NET Transfer from us in its canonical receipt (a refund comes back), and a native-in
        entry sends it as `value`, read only when the receipt shows no quote-token outflow.
        Returns the amount in quote-token units, or None when neither is readable.
        """
        receipt = _canonical_receipt(tx_hash)
        if receipt is None:
            return None
        atomic = -receipt_token_net(receipt, quote_token, address)
        if atomic > 0:
            return atomic / (10 ** int(quote["decimals"]))
        try:
            tx = _rpc(rpc_url, "eth_getTransactionByHash", [tx_hash], http=http).get("result")
            value = int(str(tx.get("value") or "0x0"), 16)
        except Exception:
            return None
        if tx.get("blockHash") != receipt.get("blockHash") or value <= 0:
            return None
        return value / (10 ** int(quote["decimals"]))

    def _receipt_quote_usd(tx_hash, quote_token_usd):
        receipt = _canonical_receipt(tx_hash)
        if receipt is None:
            return None
        atomic, source = exit_proceeds_atomic(
            tx_hash, rpc_url, owner=address, quote_token=quote_token, http=http, receipt=receipt,
            native=quote.get("kind") == "eth" and chain not in {"1", "57073"})
        if atomic is None:
            return None
        logger.warning("evm.exit_proceeds tx=%s source=%s atomic=%s", tx_hash, source, atomic)
        return atomic / (10 ** int(quote["decimals"])) * float(quote_token_usd)

    def _execution_balance_usd_fn(snapshot_price=None, *, first_raw_read=None):
        """Return (balance_fn, quote-token USD snapshot) for one execution window.

        Stablecoin chains naturally snapshot at $1. Injected test/operator seams retain their
        existing contract. None means the WETH accounting price is unknown; callers must abort
        before broadcast rather than subtract whole-wallet values measured in different units."""
        snapshot_price = snapshot_price or quote_usd_price(
            chain, http=http, price_fn=price_fn)
        if not snapshot_price:
            return None
        if injected_usdc_fn is not None:
            return injected_usdc_fn, float(snapshot_price)
        # THE SAME ONE balanceOf AS BEFORE, and the same value (raw / 10**decimals * price; 0.0 on
        # a failed read), but the raw word is also kept for the unwrap guard and trace. A failed
        # read leaves `last_raw()` None -- unknown, never a zero balance (2026-09-28).
        holder = {"raw": None}

        def _quote_balance_usd():
            nonlocal first_raw_read
            read, first_raw_read = first_raw_read, None
            try:
                raw = (read() if read is not None else
                       erc20_balance_raw_checked(address, quote_token, rpc_url, http=http))
            except Exception:
                holder["raw"] = None
                return 0.0
            holder["raw"] = int(raw)
            return raw / (10 ** int(quote["decimals"])) * float(snapshot_price)
        _quote_balance_usd.last_raw = lambda: holder["raw"]
        return _quote_balance_usd, float(snapshot_price)

    def _peek_quote_price():
        """The memoised quote-token USD price and its age, WITHOUT fetching; None before any read.
        The gate records it right after its own balance read: that is the gate's price (P1)."""
        with _price_lock:
            _selected_price(_time.monotonic())
            at, px = _price_memo["at"], _price_memo["px"]
            source = _price_memo["source"]
        if at is None or px is None:
            return None
        snapshot = {"px": float(px), "age_ms": round((_time.monotonic() - at) * 1000.0, 1)}
        if chain == "56":
            snapshot["source"] = source
        return snapshot

    _peek_quote_price.status = _quote_price_status

    _gas_window_s = 1.0
    _gas_memo = {"at": None, "gp": None, "raw": None}
    _gas_lock = threading.Lock()

    def _node_gas_price(*, observed_at=None):
        # a BUFFERED node gas price. A legacy tx's gasPrice is treated as maxFeePerGas on EIP-1559
        # chains and is REJECTED ('max fee per gas less than block base fee') if baseFee ticks up
        # between the read and the send — observed on Robinhood/4663 where an un-buffered price
        # landed 1% under baseFee. The buffer is negligible cost (memecoin-chain baseFees are tiny).
        #
        # One entry reads this TWICE (approval, then swap signing) = 2 x ~160ms on 4663. With the
        # approval no longer waiting for its receipt those two reads are now milliseconds apart, so
        # a window shorter than the gap between them collapses them into one round trip. The window
        # is deliberately tiny: baseFee drift is exactly what the buffer above exists to absorb, and
        # anything longer would start trading that protection away for latency.
        now = _time.monotonic()
        with _gas_lock:
            at, gp = _gas_memo["at"], _gas_memo["gp"]
            if at is not None and gp is not None and (now - at) < _gas_window_s:
                if observed_at is not None:
                    observed_at.append(at)
                return gp
        raw = eth_gas_price(rpc_url, http=http)
        if raw is None:
            return None
        # The per-chain tip (chain 1: MEMECOIN_EVM_TIP_GWEI_1) rides on top of the buffered price;
        # with MEMECOIN_CHAIN1_FEE_HEADROOM it is the same dynamic tip the signer uses.
        tip_wei = int(cfg.evm_tip_gwei_for(chain) * 1e9) if chain == "1" else 0
        if chain == "1":
            from sniper.execution import chain1_protect
            tip_wei = chain1_protect.dynamic_tip_wei(chain, tip_wei) or tip_wei
        buffered = int(raw * gasprice_buffer) + tip_wei
        with _gas_lock:
            _gas_memo["at"], _gas_memo["gp"], _gas_memo["raw"] = _time.monotonic(), buffered, int(raw)
            if observed_at is not None:
                observed_at.append(_gas_memo["at"])
        return buffered

    def _expected_gas_price():
        # What one gas is expected to COST, for the gas guard (type-2 spec section 4). Legacy pays the
        # whole signed price. Type 2 pays base fee + tip: the raw node price from the same read plus
        # our tip. An upper bound, since the node price already holds the node's own suggested tip.
        # Admission reserves never use this: a node admits against the cap.
        buffered = _node_gas_price()
        tip = _type2_tip_wei({"chainId": chain})
        if buffered is None or tip is None:
            return buffered
        # The entry tip floor is what this entry signs, so the guard prices it (conservative: the
        # exit leg is costed at it too).
        tip = max(tip, cfg.evm_entry_tip_floor_wei_for(chain))
        with _gas_lock:
            raw = _gas_memo["raw"]
        return None if raw is None else raw + tip

    def _gas_price_for_tx(tx0x, *, observed_at=None):
        # the venue quote's own fee (already sized above baseFee) OR the buffered node price, whichever
        # is higher, so the swap tx always clears the live baseFee.
        quoted = tx0x.get("gasPrice") or tx0x.get("maxFeePerGas")
        if isinstance(quoted, str):
            quoted = int(quoted, 16) if quoted.startswith("0x") else int(quoted)
        gp = max(int(quoted or 0), _node_gas_price(observed_at=observed_at) or 0)
        return gp or None

    gas_guard_fn = None
    if chain in cfg.evm_gas_guard_chains():
        from sniper.services.memecoin_evm_risk import evm_gas_guard_refusal

        def gas_guard_fn(clip_usd, *, native_usd, facts=None, route=None):
            # `native_usd` is the entry's QUOTE-token USD (`sell_token_usd`). That is the native
            # price only while the quote token is wrapped native; if this chain's quote token
            # ever changes, gas must not be priced with it (Gamma LOW on 808b0bc37).
            if quote.get("kind") != "eth":
                return ("gas guard: chain %s quote token is not wrapped native, so the native "
                        "price is unknown" % chain)
            # WHAT GAS COSTS, NOT WHAT WE SIGN (2026-10-08, BUNKER 93e8a712 refused at 11.7% on the
            # signing price raw x 1.5 + tip; base + tip was ~8.1%). A type-2 transaction pays base fee
            # + tip, a legacy one its whole signed price: _expected_gas_price() is each, from the same
            # memoised read as the signed price, so this adds no call. The trace keeps both.
            signed = _node_gas_price()
            expected = _expected_gas_price()
            if facts is not None and isinstance(signed, int) and signed > 0:
                facts["signed_gwei"] = signed / 1e9
                facts["gas_price_basis"] = ("expected_type2" if _type2_tip_wei({"chainId": chain})
                                            is not None else "signed_legacy")
            return evm_gas_guard_refusal(chain, gas_price_wei=expected,
                                         clip_usd=clip_usd, native_usd=native_usd, facts=facts,
                                         route=route)

    # Pipelined-approval nonce floor (MEMECOIN_EVM_APPROVAL_PIPELINE): after an approval is
    # broadcast WITHOUT waiting for its receipt, the swap must sign at approval-nonce+1 even if
    # the node's pending count hasn't registered the approval yet. The floor is TTL-bounded so a
    # sign-failure after a floor advance can only mis-gap nonces for a few seconds, then self-heals
    # back to the chain's pending count.
    import threading as _threading
    import time as _time
    _nonce_state = {"floor": None, "floor_at": 0.0, "lock": _threading.Lock()}
    _NONCE_FLOOR_TTL_S = 30.0

    def _floored_nonce(nonce=None):
        if nonce is None:
            nonce = eth_nonce(address, rpc_url, http=http)
        if nonce is None:
            return None                                   # RPC failure stays fail-closed
        with _nonce_state["lock"]:
            floor = _nonce_state["floor"]
            if floor is not None and (_time.monotonic() - _nonce_state["floor_at"]) > _NONCE_FLOOR_TTL_S:
                floor = None
            use = nonce if floor is None else max(nonce, floor)
            _nonce_state["floor"] = use + 1
            _nonce_state["floor_at"] = _time.monotonic()
            return use

    def _release_floored_nonce(nonce):
        """Rewind only the latest unbroadcast reservation; never cross a later allocation. Returns
        True when it rewound, decided in the same lock hold as the rewind (Gamma F1, 2026-09-30)."""
        if nonce is None:
            return False
        with _nonce_state["lock"]:
            if _nonce_state["floor"] == nonce + 1:
                _nonce_state["floor"] = nonce
                _nonce_state["floor_at"] = _time.monotonic()
                return True
            return False

    def _lane_claim_nonce(pending_nonce):
        """A native fast lane's nonce (spec 2026-10-01 fast lane §2.6): the snapshot's pending
        count, claimed through THIS floor, the one every signer here shares when the chain uses it
        (pipelined chains; Base 8453 and chain 1 receipt-first with pre-approval, where the entry
        signer already takes its nonce from this floor). None when the chain has no floor, the
        count is unknown, or a fresh floor sits above the count: one of our own transactions is in
        flight and the count predates it. No RPC: the count comes from the caller."""
        if pending_nonce is None or not _uses_nonce_floor():
            return None
        with _nonce_state["lock"]:
            floor = _nonce_state["floor"]
            fresh = (floor is not None
                     and (_time.monotonic() - _nonce_state["floor_at"]) <= _NONCE_FLOOR_TTL_S)
            if fresh and floor > int(pending_nonce):
                return None
            _nonce_state["floor"] = int(pending_nonce) + 1
            _nonce_state["floor_at"] = _time.monotonic()
            return int(pending_nonce)

    def _claim_floored_nonce(nonce):
        """Consume an accepted approval's next nonce without repeating the pending-count RPC."""
        if nonce is None:
            return None
        with _nonce_state["lock"]:
            if ((_time.monotonic() - _nonce_state["floor_at"]) > _NONCE_FLOOR_TTL_S
                    or _nonce_state["floor"] != nonce):
                return None
            _nonce_state["floor"] = nonce + 1
            _nonce_state["floor_at"] = _time.monotonic()
            return nonce

    def _approval_pipelined():
        return cfg.memecoin_evm_approval_pipeline(chain)

    def _uses_nonce_floor():
        # Receipt-first chains share the floor with their background token approvals.
        return _approval_pipelined() or (chain in {"1", "8453"}
                                       and chain in cfg.memecoin_evm_preapprove_chains())

    def _bsc_gas_future(read, *, entry=False):
        # The same required gas read beside the nonce read; the caller still owns every
        # nonce claim and mutation. No extra RPC, cache window or submission worker.
        if chain != "56" and not (entry and chain in {"8453", "4663"}):
            return None
        future = Future()

        def read_gas():
            try:
                future.set_result(read())
            except BaseException as exc:
                future.set_exception(exc)

        try:
            threading.Thread(target=read_gas, name="bsc-sign-gas", daemon=True).start()
        except RuntimeError:
            return None                       # original serial read, once
        return future

    def _approve(spender, amount, *, send_fn=None, token=None, tip_floor_wei=0,
                 wait_for_receipt=True, entry=False):
        # exact-amount approve of `token` (default: the QUOTE token) for the router spender. Its
        # own nonce; an accepted pipelined approval hands nonce+1 to the next signed transaction.
        #
        # `token` exists so the EXIT can share this. It used to have its own `_approve_token`
        # which always waited for `eth_confirm` and never pipelined, so the entry paid ~0s for an
        # approval and the exit paid 2.5s for the same operation. Measured on the 4663 exit stage
        # map: exits needing an approval ran p50 6.0s against 2.3s for those that did not, and the
        # approve stage alone was p50 2,482ms -- 41% of an approving exit. One path got the fix in
        # July and the other did not, which is the shape of most of what broke this week.
        pipelined = _approval_pipelined()
        gas_observation = [] if entry and chain in {"8453", "4663"} else None
        gas_future = _bsc_gas_future(
            lambda: _node_gas_price(observed_at=gas_observation), entry=entry)
        nonce = _floored_nonce() if _uses_nonce_floor() else eth_nonce(address, rpc_url, http=http)
        gp = gas_future.result() if gas_future is not None else _node_gas_price()
        if nonce is not None and gas_observation:
            gp = _node_gas_price()  # memo owner rechecks the actual observation after nonce
        if nonce is None or gp is None:
            _release_floored_nonce(nonce)
            return False
        tx = {"to": (token or quote_token), "data": encode_approve(spender, amount), "value": 0,
              "gas": approve_gas, "gasPrice": int(gp), "nonce": nonce, "chainId": int(chain)}
        signed = (sign_tx(tx, acct, tip_floor_wei=tip_floor_wei) if tip_floor_wei
                  else sign_tx(tx, acct))
        if not signed:
            _release_floored_nonce(nonce)
            return False
        sent = (send_fn or _approve_send)(signed[0])
        if pipelined or not wait_for_receipt:
            # Pipelining commits to a swap at approval-nonce+1 WITHOUT confirming the approval, so
            # it may proceed ONLY on a POSITIVE acceptance (a tx hash or `already_known`, i.e. a
            # truthy string). `False` (positively rejected) AND `None` (transport/unclassified
            # ambiguity — the approval may or may not have landed) both fail closed: broadcasting
            # the swap on a maybe-missing approval queues it behind a nonce gap, and a later tx at
            # that nonce can unlock it unexpectedly (2026-07-23 review P0). Aborting only wastes an
            # approval's gas; it never strands a swap. A confirmed-accepted approval that later
            # drops is the executor's swap allowance-revert path (fail-closed).
            if not (isinstance(sent, str) and sent):
                if sent is False:                         # proven not accepted: nonce is reusable
                    _release_floored_nonce(nonce)
                return False
            return {"pipelined": pipelined, "nonce": nonce, "tx_hash": signed[1]}
        if sent is False:
            _release_floored_nonce(nonce)
            return False
        if chain == "8453":
            # Base approvals need the swap's same-bytes recovery too: a dropped approval
            # otherwise holds the exit for the entire 60s receipt wait (BASEPAD 68727).
            return _confirm_polled(signed[1], lambda: (send_fn or _approve_send)(signed[0])) == "confirmed"
        if chain == "56":
            # BSC includes an approval ~0.39 s after the send; the 2 s poll saw it ~1.8 s late
            # (2026-09-26). Chain 56 only: 4663's read pool also carries the live tape and 5042's
            # provider load is unmeasured, so their approval cadence is unchanged.
            return _confirm_polled(signed[1], None) == "confirmed"
        return eth_confirm(signed[1], rpc_url, http=http, max_attempts=30,
                           **({"require_canonical": True} if chain in {"1", "57073"} else {})
                           ) == "confirmed"

    # --- STANDING QUOTE-TOKEN ALLOWANCE (spec 2026-10-07-bsc-standing-allowance-design.md) ---
    # The pinned routers keep an allowance worth the cap; the book (evm_standing_allowance) counts
    # it and every rule errs low. The entry path only reads the book; top-ups run on their own
    # thread and never while an executor holds this wallet's lock.
    from sniper.execution import evm_standing_allowance as _sa
    _standing_state = {"thread": None, "event": threading.Event(), "lock": threading.Lock()}

    def _standing_key(spender):
        # THE SHARED NONCE FLOOR, NOT THE PIPELINE (Base, 2026-10-15): a receipt-first chain whose
        # background approvals share the floor (_uses_nonce_floor) qualifies; 56 is pipelined,
        # which already implies the floor.
        if (chain not in cfg.memecoin_evm_standing_allowance_chains() or not _uses_nonce_floor()
                or not _sa.pinned(chain, spender)):
            return None
        return _sa.book_key(chain, address, quote_token, spender)

    def _standing_approve(spender, amount, token_usd, used, send_fn=None):
        """{"standing": True} when the book covers `amount` (spent from it), else today's exact
        approve, which overwrites the allowance: the count goes to 0 and a top-up is woken."""
        key = _standing_key(spender)
        if key is not None:
            rid = _sa.reserve(key, amount, token_usd=token_usd)
            if rid is not None:
                used.append((key, rid))
                return {"standing": True}
        approved = _approve(spender, amount, send_fn=send_fn, entry=True)
        if key is not None:
            _sa.exact_approved(key, amount, token_usd=token_usd)
            _standing_state["event"].set()
        return approved

    def _standing_settle(used, result):
        filled = isinstance(result, dict) and result.get("status") == "filled"
        for key, rid in used:
            _sa.settle(key, rid, filled)
        if used:
            _standing_state["event"].set()

    def _standing_topup(spender):
        """One top-up attempt for `spender`: approve(spender, cap) through `_approve`, so it shares
        the nonce floor. Returns its status. Only a confirmed receipt for a positively accepted send
        raises the count (an ambiguous one never does)."""
        key = _standing_key(spender)
        if key is None or not _sa.topup_due(key):
            return "not_due"
        cap = _sa.cap_atomic(cfg.memecoin_evm_standing_allowance_cap_usd(), _sa.last_price(key),
                             int(quote["decimals"]))
        if not cap:
            return "no_price"
        from sniper.execution.exec_lock import (
            evm_exec_lock_key, intent_send, intents_outstanding, stamp_send,
            standing_intent_lock_dir, wallet_exec_lock,
        )
        intent_dir = standing_intent_lock_dir()
        sent, outcome = {}, "not_sent"
        # NEVER WHILE AN EXECUTOR HOLDS THE WALLET (Alpha): entries, exits and the lanes hold this
        # lock from quote to confirm. Held only through sign and send, never the receipt.
        with wallet_exec_lock(evm_exec_lock_key(chain, address), blocking=False) as held:
            if not held:
                return "wallet_busy"
            if intents_outstanding(chain, lock_dir=intent_dir, required=True) is not False:
                return "standing_unresolved"
            from contextlib import closing
            from sniper.persistence.db_core import get_readonly_connection
            # Unknown holding sends retain this chain's nonce after releasing the wallet lock.
            # Own and close the observer here; DB failures reach the loop logger before signing.
            with closing(get_readonly_connection()) as conn:
                conn.execute("PRAGMA busy_timeout=0")
                if m.unresolved_holding_sell_count(conn, chain):
                    return "holding_unresolved"
            if not _sa.topup_begin(key):
                return "in_flight"
            try:
                def capture(raw):
                    sent["intent"] = intent_send(raw, "rpc", lock_dir=intent_dir, required=True)
                    sent["raw"] = raw
                    sent["result"] = _approve_send(raw)
                    return sent["result"]
                # Send only: the receipt is awaited below, OUTSIDE the wallet lock. A receipt-first
                # chain (Base) would otherwise hold every entry behind it (identical on 56,
                # where the pipeline already returns at send).
                approved = _approve(spender, cap, send_fn=capture, wait_for_receipt=False)
                if "raw" in sent:
                    outcome = ("accepted" if isinstance(approved, dict) else
                               "rejected" if sent["result"] is False else "ambiguous")
            except Exception:
                logger.exception("standing allowance top-up send failed chain=%s spender=%s",
                                 chain, spender)
                outcome = "ambiguous" if "raw" in sent else "not_sent"
        tx_hash = nonce = receipt_status = None
        try:
            if "raw" in sent:
                from eth_utils import keccak
                import base64
                tx_hash = "0x" + keccak(hexstr=str(sent["raw"])).hex().removeprefix("0x")
                nonce = (decode_signed_tx_cost(base64.b64encode(
                    str(sent["raw"]).encode("ascii")).decode("ascii")) or {}).get("nonce")
            if outcome == "accepted":
                receipt_status = _confirm_polled(tx_hash, lambda: _approve_send(sent["raw"]))
            elif outcome == "ambiguous" and not _release_floored_nonce(nonce):
                # A later signer built on this nonce: only its landing unblocks it (pre-approval rule).
                receipt_status = _confirm_polled(tx_hash, lambda: _approve_send(sent["raw"]))
        finally:
            if (outcome == "rejected" or receipt_status in {"confirmed", "failed"}) \
                    and sent.get("intent") is not None:
                stamp_send(sent["raw"], intent=sent["intent"], lock_dir=intent_dir)
            final = "confirmed" if (outcome == "accepted" and receipt_status == "confirmed") else (
                outcome if outcome != "accepted" else receipt_status or "unknown")
            decided = _sa.topup_end(key, final, cap_atomic=cap)
            logger.info("standing allowance top-up chain=%s spender=%s amount_atomic=%s nonce=%s "
                        "tx_hash=%s send_status=%s receipt_status=%s decided=%s", chain, spender,
                        cap, nonce, tx_hash, outcome, receipt_status, decided)
        return decided if final == "confirmed" else final

    def _standing_bootstrap():
        keys = [key for spender in sorted(_sa.PINNED_SPENDERS.get(chain, ()))
                if (key := _standing_key(spender)) is not None
                and _sa.value(key) is None and _sa.last_price(key) is None]
        if not keys or not cfg.memecoin_evm_standing_allowance_cap_usd():
            return "not_due"
        price = quote_usd_price(chain, http=http, price_fn=price_fn)
        if not all(_sa.bootstrap_price(key, price) for key in keys):
            return "no_price"
        cap = _sa.cap_atomic(cfg.memecoin_evm_standing_allowance_cap_usd(), price, int(quote["decimals"]))
        for key in keys:
            # One allowance(owner, spender) read per pinned spender, on this thread (never an entry
            # path). A failed or below-cap read leaves the count unknown: the existing top-up runs.
            generation = _sa.seed_generation(key)
            try:
                raw = _eth_call(quote_token, SEL_ALLOWANCE + _pad_addr(address) + _pad_addr(key[3]),
                                rpc_url, http=http)
                _sa.seed_allowance(key, int(raw, 16), cap, generation)
            except Exception:
                logger.exception("standing allowance seed read failed chain=%s spender=%s", chain, key[3])
        return "seeded"

    def _standing_loop():
        event = _standing_state["event"]
        while True:
            event.wait(timeout=5.0)
            event.clear()
            try:
                _standing_bootstrap()
            except Exception:
                logger.exception("standing allowance bootstrap failed chain=%s", chain)
            for spender in sorted(_sa.PINNED_SPENDERS.get(chain, ())):
                try:
                    _standing_topup(spender)
                except Exception:
                    logger.exception("standing allowance top-up failed chain=%s spender=%s",
                                     chain, spender)

    def _standing_start():
        """Start the top-up thread once (the EVM leg calls this at build when the chain is on)."""
        with _standing_state["lock"]:
            thread = _standing_state["thread"]
            if thread is None or not thread.is_alive():
                thread = threading.Thread(target=_standing_loop, daemon=True,
                                          name="standing-allowance-%s" % chain)
                _standing_state["thread"] = thread
                thread.start()
        _standing_state["event"].set()

    def _entry_snapshot_nonce(snapshot_fn):
        """A local BSC snapshot, fenced by the same wallet/broadcast generation as its native lane."""
        import math
        from sniper.execution.exec_lock import evm_exec_lock_key
        from sniper.services.memecoin_rh_fast_lane import lane_generation_fn
        try:
            snap = snapshot_fn()
            if (not isinstance(snap, dict) or snap.get("chain") != chain
                    or snap.get("address") != address.lower()
                    or type(snap.get("nonce")) is not int or snap["nonce"] < 0):
                return None
            stamp, age, margin = (snap.get(key) for key in (
                "read_at", "max_age_s", "generation_margin_s"))
            if any(type(n) not in (int, float) or not math.isfinite(n)
                   for n in (stamp, age, margin)) or age <= 0 or margin < 0:
                return None
            generation = lane_generation_fn(evm_exec_lock_key(chain, address), chain)()
            if (type(generation) not in (int, float) or not math.isfinite(generation)
                    or stamp <= generation + margin
                    or not 0 <= _time.time() - stamp <= age):
                return None
            return snap["nonce"]
        except Exception:
            return None

    def _sign(tx0x, *, nonce_hint=None, gas_price_override=None, nonce_pin=None, fee_floor=None,
              tip_floor_wei=None, entry_signing_snapshot_fn=None, snapshot_facts=None,
              entry=False):
        # `nonce_pin` + `fee_floor`: ONLY the chain-1 same-nonce replacement (`_replace_exit_sign`).
        # `gas_price_override` exists ONLY for the exit affordability gate's reduced-fee fallback
        # below (`_exit_sign_gate`) — every other caller, entries included, passes nothing and gets
        # today's `_gas_price_for_tx` behaviour byte-for-byte. The override bypasses the max-with-
        # live-node-price floor deliberately: that floor is what protects a normal sign from a stale
        # quote, and the whole point of the fallback is to sign BELOW the live price on purpose.
        gas_observation = [] if entry and chain in {"8453", "4663"} else None
        gas_future = (_bsc_gas_future(
            lambda: _gas_price_for_tx(tx0x, observed_at=gas_observation), entry=entry)
                      if gas_price_override is None else None)
        snapshot_nonce = None
        if (chain == "56" and nonce_hint is None and nonce_pin is None
                and callable(entry_signing_snapshot_fn)):
            snapshot_nonce = _lane_claim_nonce(_entry_snapshot_nonce(entry_signing_snapshot_fn))
            if snapshot_facts is not None:
                snapshot_facts["outcome"] = "used" if snapshot_nonce is not None else "fallback"
        if nonce_pin is not None:
            nonce = int(nonce_pin)
        elif snapshot_nonce is not None:
            nonce = snapshot_nonce
        elif _uses_nonce_floor():
            nonce = _claim_floored_nonce(nonce_hint)
            if nonce is None:
                nonce = _floored_nonce()
        else:
            nonce = eth_nonce(address, rpc_url, http=http)
        try:
            gp = (int(gas_price_override) if gas_price_override is not None else
                  gas_future.result() if gas_future is not None else _gas_price_for_tx(tx0x))
            if nonce is not None and gas_observation:
                # Reuse the current memo or refresh its actual timestamp; failed reads retain
                # the quote's existing fallback without a second node attempt.
                gp = _gas_price_for_tx(tx0x)
            if nonce is None or gp is None:
                if nonce_pin is None:
                    _release_floored_nonce(nonce)
                return None
            if (snapshot_nonce is not None
                    and _entry_snapshot_nonce(entry_signing_snapshot_fn) != snapshot_nonce):
                _release_floored_nonce(snapshot_nonce)
                nonce = _floored_nonce()
                if snapshot_facts is not None:
                    snapshot_facts["outcome"] = "fallback_recheck"
                if nonce is None:
                    return None
            tx = {"to": tx0x["to"], "data": tx0x.get("data", "0x"),
                  "value": _evm_quantity(tx0x.get("value") or 0),
                  "gas": _evm_quantity(tx0x.get("gas") or default_swap_gas),
                  "gasPrice": int(gp), "nonce": nonce, "chainId": int(chain)}
        except (KeyError, TypeError, ValueError):
            if nonce_pin is None:
                _release_floored_nonce(nonce)
            return None
        except Exception:
            if nonce_pin is None:
                _release_floored_nonce(nonce)
            raise
        signed = (sign_tx(tx, acct, fee_floor=fee_floor,
                          **({"tip_floor_wei": tip_floor_wei} if tip_floor_wei else {}))
                  if fee_floor is not None
                  else sign_tx(tx, acct, tip_floor_wei=tip_floor_wei) if tip_floor_wei
                  else sign_tx(tx, acct))
        if not signed:
            if nonce_pin is None:
                _release_floored_nonce(nonce)
            return None
        raw, h = signed
        return raw, h, nonce

    def _send(raw):
        from sniper.execution.free_endpoint_registry import private_submit_endpoint
        if submission_transport is not None and private_submit_endpoint(chain) is not None:
            # A private-only chain (1) re-offers through its private transport, never custody.
            from sniper.execution.submission_wiring import make_evm_send_fn
            return make_evm_send_fn(submission_transport, chain=chain, conn=None, action="resend",
                                    owner_kind="resend", owner_id=None, record=False)(raw)
        return eth_send_raw(raw, rpc_url, http=http)

    from sniper.util.clock import real_utc_now_iso
    _now_fn = now_fn or real_utc_now_iso

    def _evm_send_for(conn, owner_id, *, action, owner_kind):
        # None transport -> the exact baseline eth_send_raw. Otherwise ONE shared same-raw-tx fanout
        # across the chain's endpoints, mapped back onto the (hash|None|False) seam. Built per call so
        # the audit key is the owning reservation/claim, on the caller thread.
        if submission_transport is None:
            return _send
        from sniper.execution.submission_wiring import make_evm_send_fn
        claim_col = {"close_claim": "close_claim_id",
                     "derisk_claim": "derisk_claim_id"}.get(owner_kind)

        def owner_id_fn():
            row = m.get_position(conn, owner_id) if claim_col else None
            return row.get(claim_col) if row else owner_id

        return make_evm_send_fn(submission_transport, chain=chain, conn=conn, action=action,
                                owner_kind=owner_kind, owner_id=owner_id,
                                owner_id_fn=(owner_id_fn if claim_col else None), now_fn=_now_fn)

    def _approve_send(raw):
        # approvals are their own signed tx (distinct nonce/identity); fan them out too. No owning
        # reservation/claim + no conn here, so no audit row is keyed (the executor's nonce/confirm
        # logic remains authoritative).
        if submission_transport is None:
            return eth_send_raw(raw, rpc_url, http=http)
        from sniper.execution.submission_wiring import make_evm_send_fn
        return make_evm_send_fn(submission_transport, chain=chain, conn=None, action="approval",
                                owner_kind="approval", owner_id=None, now_fn=_now_fn,
                                record=False)(raw)

    # tx hash -> (raw, the send function that delivered it). Only the receipt wait below reads it.
    _sent_raw = {}
    _sent_raw_lock = threading.Lock()

    def _builder_ready():
        from sniper.execution.bsc_builder_ab import PROVIDER_IDS
        return (submission_transport is not None and hasattr(submission_transport, "provider")
                and any(submission_transport.provider(pid) is not None for pid in PROVIDER_IDS))

    def _head_after_send(send_fn, heads, threads, *, enabled):
        """`send_fn`, then (when `enabled`) one eth_blockNumber on custody on a daemon thread: the
        head right after this send, numbered by send order. Never on the fill path."""
        if not enabled:
            return send_fn

        def sent(raw):
            out = send_fn(raw)
            seq = len(threads)

            def read():
                started = _time.perf_counter()
                try:
                    head = int(_rpc(rpc_url, "eth_blockNumber", [], http=http).get("result"), 16)
                except Exception:
                    head = None
                heads.append((seq, head, round((_time.perf_counter() - started) * 1000.0, 1)))
            thread = threading.Thread(target=read, name="bsc-head-after-send", daemon=True)
            threads.append(thread)
            thread.start()
            return out
        return sent

    def _builder_trace(trace, arm, swaps, heads, threads):
        """`bsc_builder_ab_send` after the entry returned: the head after the LAST send (the swap)
        and each builder's own answer for the arm-B swap. Never waits (Gamma LOW): a head read still
        in flight is recorded as missing, so a hung custody read cannot delay the next decision."""
        try:
            last = max(heads, default=None)
            if last is not None and last[0] != len(threads) - 1:
                last = None                    # the swap's own read has not landed yet
            builders = {}
            if arm["arm"] == "B" and swaps:
                from sniper.execution.bsc_builder_ab import PROVIDER_IDS
                for pid_ in PROVIDER_IDS:
                    provider = submission_transport.provider(pid_)
                    if provider is not None:
                        builders[pid_] = provider.outcome(swaps[-1]) or {"answer": "not_sent"}
            trace.mark_substage("bsc_builder_ab_send", arm=arm["arm"],
                                head_after_send=None if last is None else last[1],
                                head_read_ms=None if last is None else last[2],
                                builders=builders)
        except Exception:
            logger.exception("bsc builder A/B trace failed")

    def _remember_sent(send_fn):
        from eth_utils import keccak

        def remembered(raw):
            try:
                h = "0x" + keccak(hexstr=str(raw)).hex().removeprefix("0x")
                with _sent_raw_lock:
                    if len(_sent_raw) > 64:
                        _sent_raw.clear()
                    _sent_raw[h.lower()] = (raw, send_fn)
            except Exception:
                pass                      # remembering is best-effort; the send itself is not
            return send_fn(raw)
        return remembered

    def _reoffer_for(h):
        with _sent_raw_lock:
            hit = _sent_raw.get(str(h).lower())
        if hit is None:
            return None
        raw, send_fn = hit
        return lambda: send_fn(raw)

    def _confirm(h, stop_fn=None):
        # Attempt indexes for the identical-bytes re-offer, per chain cadence (see eth_confirm).
        resend = _reoffer_for(h)
        try:
            state = _confirm_polled(h, resend, stop_fn)
            # The booking reads this receipt next: keep the one the confirmation already holds
            # (same endpoint); the canonical block check still runs on it.
            if isinstance(getattr(state, "receipt", None), dict):
                _confirmed_receipt.clear()
                _confirmed_receipt[str(h).lower()] = state.receipt
            return state
        finally:
            with _sent_raw_lock:
                _sent_raw.pop(str(h).lower(), None)

    def _exit_confirm(h):
        """The full exit's receipt wait. On chain 1 it ends early, as `unknown`, once the Protect
        watcher flags the send as reverting and MEMECOIN_CHAIN1_PROTECT_REPLACE is on, so the
        recovery pass can sign the same-nonce replacement instead of waiting out the deadline."""
        if chain != "1":
            return _confirm(h)
        from sniper.execution import chain1_protect
        return _confirm(h, stop_fn=lambda: chain1_protect.replace_requested(chain, h))

    def _confirm_polled(h, resend, stop_fn=None):
        if chain == "57073":
            return eth_confirm(h, rpc_url, http=http, max_attempts=36, poll_s=2.0,
                               fast_poll_s=0.25, fast_attempts=8, require_canonical=True,
                               resend_fn=resend, resend_at_attempts=(7, 9, 17) if resend else ())
        if chain == "4663":
            # Robinhood receipts usually appear in the first seconds. Observe them promptly, then
            # poll every 2s: 8*0.25 + 28*2 = 58s. It was 118s, past the watchdog's 90s sweep limit,
            # so a dropped send got the runner killed (BANK on Arc, 2026-09-16). `unknown` goes to
            # reservation recovery, which proves a drop from the nonce and the token balance.
            return eth_confirm(h, rpc_url, http=http, max_attempts=36, poll_s=2.0,
                               fast_poll_s=0.25, fast_attempts=8,
                               resend_fn=resend, resend_at_attempts=(7, 9, 17) if resend else ())
        if chain == "5042":
            # Arc blocks are 0.5s. The default 120s poll runs on the main loop, so a dropped send
            # outlived the watchdog's stale limit and got the runner killed (BANK, 2026-09-16).
            # 40*0.25 + 15*2 = 40s; `unknown` then goes to reservation recovery, which is the path
            # built for exactly this.
            return eth_confirm(h, rpc_url, http=http, max_attempts=55, poll_s=2.0,
                               fast_poll_s=0.25, fast_attempts=40,
                               resend_fn=resend, resend_at_attempts=(11, 31, 41) if resend else ())
        if chain == "56":
            # BSC blocks are ~0.45 s and a receipt was included ~0.39 s after the send, but the 2 s
            # cadence first looked again at 2 s: 56 accept -> confirmation seen p50 2.067 s
            # (n=36, 30 d to 2026-09-26). 8*0.25 + 29*2 = 60s, the same bound as before. The
            # re-offers stay at 4 s and 14 s (attempts 9 and 14), where the 2 s cadence put them.
            return eth_confirm(h, rpc_url, http=http, max_attempts=37, poll_s=2.0,
                               fast_poll_s=0.25, fast_attempts=8,
                               resend_fn=resend, resend_at_attempts=(9, 14) if resend else ())
        if chain == "8453":
            # O1DOLL's approval and swap waits used a 2s receipt cadence. Keep receipt-first
            # approval and the 60s bound, but observe the first 2s promptly; identical-byte
            # reoffers retain nominal 4s/14s times. At most seven initial receipt reads are added.
            return eth_confirm(h, rpc_url, http=http, max_attempts=37, poll_s=2.0,
                               fast_poll_s=0.25, fast_attempts=8,
                               resend_fn=resend, resend_at_attempts=(9, 14) if resend else (),
                               **({"stop_fn": stop_fn} if stop_fn is not None else {}))
        # Other receipt-first chains retain their existing cadence and reoffer schedule.
        return eth_confirm(h, rpc_url, http=http, max_attempts=30,
                           resend_fn=resend, resend_at_attempts=(2, 7) if resend else (),
                           **({"stop_fn": stop_fn} if stop_fn is not None else {}),
                           **({"require_canonical": True} if chain == "1" else {}))

    def _confirm_pre_transaction(h):
        # Bounded receipt wait for a pipelined unwrap. Short on purpose: the swap cannot be
        # admitted until this lands, so the wait replaces a rejection, it does not add to a
        # working path. Pool failover and read-error awareness live in the shared helper.
        attempts = max(1, int(round(cfg.MEMECOIN_EVM_UNWRAP_CONFIRM_S / 0.25)))
        return confirm_receipt_across_pool(h, rpc_url, http=http, attempts=attempts, poll_s=0.25)

    def _native_visible_across_pool(min_wei, *, poll_s=0.1):
        """True when every healthy pool member reports at least `min_wei` of native balance.

        Bounded by MEMECOIN_EVM_NATIVE_VISIBLE_WAIT_S. A member that cannot be read is skipped —
        an unreadable endpoint is not evidence that the balance is missing, and the send path
        already treats an unreachable member as unusable.
        """
        from sniper.execution import evm_rpc_pool as _pool
        try:
            need = int(min_wei)
        except (TypeError, ValueError):
            return True
        if need <= 0:
            return True
        budget = float(getattr(cfg, "MEMECOIN_EVM_NATIVE_VISIBLE_WAIT_S", 1.0) or 0.0)
        if budget <= 0:
            return True
        members = [u for u in _pool.pool_for(rpc_url) if _pool.available(u)] or [rpc_url]
        deadline = _time.monotonic() + budget
        pending = list(members)
        while pending:
            still = []
            for member in pending:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    still.append(member)
                    continue
                try:
                    # Read this endpoint directly; pooled RPC can select another member.
                    status, answer, _ = (http or _http)(
                        "POST", member, body=json.dumps({
                            "jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                            "params": [address, "latest"],
                        }), timeout=remaining)
                    balance = (int(answer["result"], 16)
                               if status == 200 and not answer.get("error") else None)
                except Exception:
                    balance = None
                if balance is None or balance >= need:
                    continue            # unreadable is skipped; sufficient is done
                still.append(member)
            if not still:
                return True
            if _time.monotonic() >= deadline:
                logger.warning(
                    "native balance not visible on %d of %d endpoints after %.2fs; "
                    "not broadcasting the swap", len(still), len(members), budget)
                return False
            pending = still
            _time.sleep(min(poll_s, max(0.0, deadline - _time.monotonic())))
        return True

    def _confirm_once(h):
        # single-shot receipt check for reservation recovery — NOT the ~120s fill-wait poll (which
        # would stall the runner's maintenance pass inline for every stuck reservation, every cycle).
        # Returns `not_found` for a CLEAN empty receipt and `unknown` only when the read itself
        # failed. `eth_confirm` cannot express that difference (see eth_receipt_state).
        return eth_receipt_state(h, rpc_url, http=http, require_canonical=chain in {"1", "57073"})

    # EXIT AFFORDABILITY GATE (2026-09-03, PRC 49252, prevention half of a two-part fix; the
    # repair/recovery half is eth_receipt_state above). A node validates gas*price+value against
    # COMMITTED native balance at ADMISSION -- before the tx ever reaches a mempool -- so a signed
    # exit that cannot clear that check is refused by EVERY endpoint, unmineable, and its nonce
    # cannot move. Nothing downstream can recover that: the provably-dead recovery proofs need the
    # nonce to advance, and re-offering the identical bytes into the identical shortfall repeats the
    # same refusal forever. PRC 49252 sat this way for 27 minutes and went from +23.6% to -6.2%
    # before a human rescued it by hand -- native was 0.00276 ETH against a signed need of 0.0048
    # ETH, and custody WETH was 0.00000 (the whole 4663 bankroll was already in the bag).
    #
    # EXIT ONLY, DELIBERATELY -- entries are untouched (a different function, `_sign` at its default
    # `gas_price_override=None`, still calls the exact code path it always has). An entry that
    # cannot afford its gas simply does not open: existing, correct, no consequence. An exit that
    # cannot afford its gas strands money already at risk, which is the state this gate exists to
    # never produce. Fail closed throughout: an unreadable balance is NOT proof of enough, matching
    # every other balance read in this file and in memecoin_evm_gas_topup.py.
    _EXIT_UNWRAP_MARGIN_PCT = 0.10          # unwrap `shortfall * 1.10`: headroom for gas-price
    # drift between this read and the unwrap actually landing -- NOT the trading-bankroll reserve.
    # That reserve explicitly YIELDS for an open position that needs gas to exit; see
    # memecoin_evm_gas_topup.plan_gas_topup's `reserve_released_for_exit` branch, same policy, same
    # reasoning, applied here at the point of signing instead of on the periodic top-up pass.
    _EXIT_REDUCED_FEE_MARGIN = 1.1          # a reduced gasPrice must still clear the live node
    # price by this margin, or the tx just sits unmined -- trading one stuck state for another.

    def _exit_sign_gate(tx0x, *, nonce_hint=None, conn, position_id, owner_kind):
        """Refuse to emit a signed exit the chain cannot admit. See the module comment above.

        In order: sign at the live price if native already covers it (today's behaviour,
        unchanged) -> unwrap WETH to cover the shortfall and WAIT for it to land, then sign (the
        unwrap is never pipelined against the exit that spends it -- see commit f7090634 for
        exactly this mistake made on the entry side) -> sign at a reduced gasPrice that still
        clears the live node price by `_EXIT_REDUCED_FEE_MARGIN` -> refuse, mark the position
        `stuck_bag`, and alert. Every branch that gives up returns None, exactly like a failed
        `_sign` -- the executor already treats that as "could not sign this attempt" and retries
        on its own schedule; this adds no new retry behaviour, only a refusal the chain would have
        forced anyway, before it costs a nonce.
        """
        from sniper.execution.evm_wallet import erc20_balance_raw, native_balance_raw_or_none
        from sniper.services.memecoin_evm_gas_topup import UNWRAP_GAS_LIMIT, encode_withdraw
        try:
            # Same fields, same parsing, same exception set `_sign` itself guards against below --
            # a malformed tx0x must refuse here exactly as it would inside `_sign`, not raise past
            # a gate whose entire point is to fail closed.
            gas_limit = _evm_quantity(tx0x.get("gas") or default_swap_gas)
            value = _evm_quantity(tx0x.get("value") or 0)
        except (KeyError, TypeError, ValueError):
            logger.warning("exit gate pos=%s: malformed transaction -- refusing to sign", position_id)
            return None
        gp = _gas_price_for_tx(tx0x)
        if gp is None:
            logger.warning("exit gate pos=%s: gas price unreadable -- refusing to sign", position_id)
            return None
        need = gas_limit * int(gp) + value
        native = native_balance_raw_or_none(address, rpc_url, http=http)
        exit_floor = cfg.evm_exit_tip_floor_wei_for(chain)
        tip = _type2_tip_wei({"chainId": chain}) if exit_floor else None
        if tip is not None and native is not None:
            from sniper.execution import chain1_protect
            cap = chain1_protect.headroom_max_fee_wei(chain, gp, gas_limit, max(tip, exit_floor))
            # A thin gas wallet keeps the existing affordable fee rather than holding the exit.
            if native >= gas_limit * cap + value:
                signed = _sign(tx0x, nonce_hint=nonce_hint, tip_floor_wei=exit_floor)
                if signed and not chain1_protect.replacement_affordable(signed[0], native):
                    # These bytes remain local: no persistence or send occurs until this callback
                    # returns. Release only its latest floor allocation, never a later signer's.
                    if _uses_nonce_floor():
                        _release_floored_nonce(signed[2])
                    return None
                return signed
        if native is not None and native >= need:
            return _sign(tx0x, nonce_hint=nonce_hint)
        weth = None
        if native is None:
            logger.warning(
                "exit gate pos=%s: native balance unreadable, need=%d wei -- refusing to sign",
                position_id, need)
        else:
            shortfall = need - native
            # erc20_balance_raw already fails closed to 0 on any read failure (its own docstring:
            # "Fail-closed -> 0"), so an unreadable WETH balance reads as "nothing to unwrap" and
            # correctly falls through below -- no separate None-check needed or possible here.
            # Only wrapped-native quote assets support withdraw(). ARC USDC does not.
            weth = (erc20_balance_raw(address, quote_token, rpc_url, http=http)
                    if chain in native_quote_chains() else 0)
            unwrap_amount = int(shortfall * (1.0 + _EXIT_UNWRAP_MARGIN_PCT))
            unwrap_need = UNWRAP_GAS_LIMIT * int(gp)
            if weth >= unwrap_amount and native >= unwrap_need:
                unwrap_tx = {"to": quote_token, "data": encode_withdraw(unwrap_amount),
                            "value": 0, "gas": UNWRAP_GAS_LIMIT}
                signed = _sign(unwrap_tx, nonce_hint=nonce_hint)
                if signed:
                    raw, tx_hash, unwrap_nonce = signed
                    sent = _evm_send_for(
                        conn, position_id, action="exit_gas_unwrap", owner_kind=owner_kind)(raw)
                    if sent and _confirm_pre_transaction(tx_hash) == "confirmed":
                        logger.warning(
                            "exit gate pos=%s: unwrapped %d wei WETH to cover a %d wei native "
                            "shortfall (tx=%s)", position_id, unwrap_amount, shortfall, tx_hash)
                        return _sign(tx0x, nonce_hint=None)
                    logger.warning(
                        "exit gate pos=%s: gas unwrap did not confirm (tx=%s) -- trying the next "
                        "fallback", position_id, tx_hash)
            # Reduced-fee fallback. Only meaningful when the shortfall is in the FEE, not the
            # VALUE being sent -- if value alone already exceeds native, no gasPrice reduction helps.
            if value < native:
                reduced_gp = (native - value) // gas_limit
                node_gp = eth_gas_price(rpc_url, http=http)
                if (reduced_gp > 0 and node_gp
                        and reduced_gp >= int(node_gp * _EXIT_REDUCED_FEE_MARGIN)):
                    logger.warning(
                        "exit gate pos=%s: reducing gasPrice %d -> %d (native=%d, need=%d) to "
                        "stay admissible", position_id, gp, reduced_gp, native, need)
                    return _sign(tx0x, nonce_hint=nonce_hint, gas_price_override=reduced_gp)
            logger.warning(
                "exit gate pos=%s: insufficient native for exit gas need=%d have=%d weth=%s -- "
                "refusing to sign an unadmittable transaction", position_id, need, native, weth)
        try:
            m.mark_stuck_bag(conn, position_id)
        except Exception:
            logger.exception("exit gate pos=%s: could not mark stuck_bag", position_id)
        try:
            from sniper.services.memecoin_alerts import send_alert
            send_alert("EXIT-GAS",
                       f"pos#{position_id} insufficient native for exit gas -- refusing to sign "
                       f"an unadmittable transaction, held for manual rescue")
        except Exception:
            logger.exception("exit gate pos=%s: could not send alert", position_id)
        return None

    def _make_exit_sign_fn(conn, position_id, owner_kind):
        """Bind conn/position_id/owner_kind into `_exit_sign_gate`'s (tx, nonce_hint) contract --
        the same shape `_sign` already has, so this is a drop-in `sign_fn` replacement for the exit
        and derisk executors, which know nothing about affordability or which claim owns them.
        `owner_kind` MUST match the caller's own `_evm_send_for` owner_kind (close_claim for a full
        exit, derisk_claim for a partial one) -- the gate's own unwrap submission is tagged with it
        too, or a derisk's gas top-up would write into the position's close_claim tracking instead
        of its derisk_claim tracking."""
        def _sign_fn(tx0x, *, nonce_hint=None):
            return _exit_sign_gate(tx0x, nonce_hint=nonce_hint, conn=conn, position_id=position_id,
                                   owner_kind=owner_kind)
        return _sign_fn

    def _recover_lane_entry(conn, pending):
        """A fast-lane buy (4663, or the BSC 56 lane) bound before a restart: booked from its own
        receipt, strict first and then its unbound net (Gamma MEDIUM: never a confirmed bag left
        unbooked; Codex EVM-03: never a block's balance delta). None when no canonical receipt
        exists yet or it proves nothing, which leaves the row reserved for the stale sweep."""
        from sniper.execution.evm_swap_executor import recover_confirmed_lane_entry
        receipt = _receipt(pending["tx_sig"])
        if receipt is None:
            return None
        try:
            tx = _rpc(rpc_url, "eth_getTransactionByHash", [pending["tx_sig"]], http=http).get("result")
            block = _rpc(rpc_url, "eth_getBlockByNumber", [receipt["blockNumber"], False],
                         http=http).get("result")
            if not (tx and block):
                return None
            mint = str(pending["mint"]).lower()
            decimals = decimals_fn(mint) if decimals_fn else None
            if decimals is None:
                decimals = erc20_decimals(mint, rpc_url, http=http)
            source = None
            if chain == "56":                 # the coin's pair, from the lane buy's own calldata
                from sniper.services.memecoin_bsc_fast_lane import coin_pool_of
                source = coin_pool_of(tx)
            booked = None
            if chain != "56" or source is not None:
                booked = recover_confirmed_lane_entry(conn, pending, receipt, tx, block,
                                                      owner=address,
                                                      decimals_fn=lambda _m: int(decimals),
                                                      network_id=chain, source=source)
            return booked or recover_confirmed_lane_entry(
                conn, pending, receipt, tx, block, owner=address,
                decimals_fn=lambda _m: int(decimals), network_id=chain, bound=False)
        except Exception:
            logger.exception("%s fast-lane entry recovery failed pending=%s", chain, pending.get("id"))
            return None

    def _recover_receipt_entry(conn, pending):
        """An ordinary buy (not a lane, not 1/57073) that confirmed without being booked in its own
        call: its canonical receipt's net amounts, never a balance delta. None leaves the row to the
        native pre-transaction recovery and the stale sweep, as before."""
        from sniper.execution.evm_swap_executor import recover_confirmed_receipt_entry
        receipt = _canonical_receipt(pending["tx_sig"])
        if receipt is None:
            return None
        try:
            tx = _rpc(rpc_url, "eth_getTransactionByHash", [pending["tx_sig"]], http=http).get("result")
            block = _rpc(rpc_url, "eth_getBlockByNumber", [receipt["blockNumber"], False],
                         http=http).get("result")
            if not (tx and block):
                return None
            return recover_confirmed_receipt_entry(
                conn, pending, receipt, tx, block, owner=address, quote_token=quote_token,
                quote_decimals=int(quote["decimals"]),
                decimals_fn=decimals_fn or (lambda token: erc20_decimals(token, rpc_url, http=http)))
        except Exception:
            logger.exception("%s receipt entry recovery failed pending=%s", chain, pending.get("id"))
            return None

    def recover_confirmed_entry_fn(conn, pending, now_iso):
        if ((chain, pending.get("signal_source")) in (("4663", "rh_fast_lane"), ("8453", "rh_fast_lane"),
                                                      ("56", "bsc_fast_lane"))
                and pending.get("tx_sig")):
            return _recover_lane_entry(conn, pending)
        if (chain not in {"1", "57073"} and pending.get("tx_sig")
                and pending.get("evm_quote_asset_usd") is not None
                and pending.get("recovery_status") != "native_pre_transaction_ambiguous"):
            recovered = _recover_receipt_entry(conn, pending)
            if recovered:
                return recovered
        if chain in {"1", "57073"} and pending.get("recovery_status") != "native_pre_transaction_ambiguous":
            receipt = _receipt(pending.get("tx_sig")) if pending.get("tx_sig") else None
            if receipt is not None:
                try:
                    tx = _rpc(rpc_url, "eth_getTransactionByHash", [pending["tx_sig"]],
                              http=http).get("result")
                    block = _rpc(rpc_url, "eth_getBlockByNumber",
                                 [receipt["blockNumber"], False], http=http).get("result")
                    if tx and block:
                        from sniper.execution.evm_swap_executor import recover_confirmed_ink_entry
                        recovered = recover_confirmed_ink_entry(
                            conn, pending, receipt, tx, block, owner=address,
                            weth=quote_token, decimals_fn=(
                                decimals_fn or (lambda token: erc20_decimals(
                                    token, rpc_url, http=http))))
                        if recovered:
                            return recovered
                except Exception:
                    logger.exception("%s confirmed entry recovery failed pending=%s", chain, pending["id"])
            if pending.get("recovery_status") != "orphaned":
                return None
        return recover_confirmed_native_pre_transaction(
            conn, pending, address=address, quote_token=quote_token,
            transaction_fn=lambda h: _rpc(
                rpc_url, "eth_getTransactionByHash", [h], http=http).get("result"),
            sign_fn=_sign,
            send_fn=_evm_send_for(conn, pending["id"], action="entry_cleanup",
                                  owner_kind="reservation"),
            confirm_fn=_confirm_once, now_iso=now_iso,
        )

    def _lifi_receipt(h):
        if _confirm(h) not in ("confirmed", "failed"):
            return None
        try:
            return _rpc(
                rpc_url, "eth_getTransactionReceipt", [h], http=http,
            ).get("result")
        except Exception:
            return None

    def _lifi_allowance(token, owner, spender):
        data = ("0x" + SEL_ALLOWANCE + owner[2:].lower().rjust(64, "0")
                + spender[2:].lower().rjust(64, "0"))
        raw = eth_call_raw(token, data, rpc_url, http=http)
        if not isinstance(raw, str) or not raw.startswith("0x"):
            raise RuntimeError("LI.FI allowance read failed")
        return int(raw, 16)

    def _lifi_estimate_gas(tx):
        call = {
            "from": address, "to": tx.get("to"), "data": tx.get("data", "0x"),
            "value": hex(_evm_quantity(tx.get("value") or 0)),
        }
        answer = _rpc(rpc_url, "eth_estimateGas", [call], http=http)
        if answer.get("error") is not None:
            raise RuntimeError("eth_estimateGas RPC error (%s)" % rung_error_text(
                RuntimeError(str(answer["error"]))))
        result = answer.get("result")
        if result is None:
            raise RuntimeError("eth_estimateGas result is absent")
        return _evm_quantity(result)

    def _lifi_construct_tx(tx, gas):
        nonce = eth_nonce(address, rpc_url, http=http)
        gas_price = _gas_price_for_tx(tx)
        if nonce is None or gas_price is None:
            raise RuntimeError("LI.FI transaction fields are absent")
        return {
            "from": address, "to": tx["to"], "data": tx.get("data", "0x"),
            "value": _evm_quantity(tx.get("value") or 0), "gas": int(gas),
            "gasPrice": int(gas_price), "nonce": int(nonce), "chainId": int(chain),
        }

    def _lifi_allowance_at_block(token, owner, spender, block_number):
        data = ("0x" + SEL_ALLOWANCE + owner[2:].lower().rjust(64, "0")
                + spender[2:].lower().rjust(64, "0"))
        call = {"to": token, "data": data}
        result = _rpc(
            rpc_url, "eth_call", [call, hex(int(block_number))], http=http,
        ).get("result")
        if not isinstance(result, str) or not result.startswith("0x"):
            raise RuntimeError("LI.FI allowance proof is absent")
        return int(result, 16)

    def _lifi_simulate(tx, block_number):
        call = {
            key: value for key, value in tx.items()
            if key in {"from", "to", "data", "value", "gas", "gasPrice"}
        }
        for key in ("value", "gas", "gasPrice"):
            if key in call:
                call[key] = hex(_evm_quantity(call[key]))
        result = _rpc(
            rpc_url, "eth_call", [call, hex(int(block_number))], http=http,
        )
        return "result" in result and result.get("error") is None

    def _lifi_sign_complete(tx):
        nonce = int(tx["nonce"])
        signed = sign_tx({key: value for key, value in tx.items() if key != "from"}, acct)
        return (*signed, nonce) if signed else None

    def _lifi_recover_confirmed(_lifecycle, _receipt, _now_iso, *, conn=None):
        # A receipt alone cannot prove the decoded swap amounts and custody deltas.
        return False

    def _lifi_reconcile_drop(
            lifecycle, *, finalized_block_number, finalized_block_hash,
            replacement_receipt):
        try:
            detail = json.loads(lifecycle.get("detail_json") or "{}")
            baseline = detail["balance_baseline"]
            input_token = str(baseline["input_token"]).lower()
            output_token = str(baseline["output_token"]).lower()
            input_before = int(baseline["input_atomic"])
            output_before = int(baseline["output_atomic"])
            owner = str(lifecycle["custody_wallet"]).lower()
            spender = str(lifecycle["spender"]).lower()
            block_number = int(finalized_block_number)
            block_tag = hex(block_number)
            if owner != address.lower() or any(
                    len(value) != 42 or not value.startswith("0x")
                    for value in (input_token, output_token, owner, spender)):
                return {"complete": False}
            receipt_block = _evm_quantity(replacement_receipt["blockNumber"])
            if receipt_block > block_number \
                    or replacement_receipt.get("status") not in ("0x1", "0x01", 1) \
                    or not str(finalized_block_hash).startswith("0x"):
                return {"complete": False}

            def read(token, data):
                value = _rpc(
                    rpc_url, "eth_call", [{"to": token, "data": data}, block_tag],
                    http=http,
                ).get("result")
                if not isinstance(value, str) or not value.startswith("0x"):
                    raise ValueError("LI.FI finalized read is invalid")
                return int(value, 16)

            balance_data = "0x70a08231" + _pad_addr(owner)
            input_after = read(input_token, balance_data)
            output_after = read(output_token, balance_data)
            allowance = read(
                input_token, "0x" + SEL_ALLOWANCE + _pad_addr(owner) + _pad_addr(spender),
            )
            tracked = {input_token, output_token}
            custody_topic = "0x" + owner[2:].rjust(64, "0")
            receipt_effect = False
            for log in replacement_receipt.get("logs") or ():
                topics = log.get("topics") or ()
                if not topics or str(topics[0]).lower() != _TRANSFER_TOPIC:
                    continue
                token = str(log.get("address") or "").lower()
                if len(topics) < 3:
                    if token in tracked:
                        receipt_effect = True
                    continue
                if token in tracked or custody_topic in {
                        str(topics[1]).lower(), str(topics[2]).lower()}:
                    receipt_effect = True
            proof = {
                "block_number": block_number,
                "block_hash": str(finalized_block_hash).lower(),
                "input_token": input_token,
                "input_before": str(input_before),
                "input_after": str(input_after),
                "output_token": output_token,
                "output_before": str(output_before),
                "output_after": str(output_after),
            }
            settlement_effect = (
                receipt_effect or input_after != input_before
                or output_after != output_before
            )
            return {
                "complete": True,
                "settlement_effect": settlement_effect,
                "allowance_atomic": allowance,
                "balance_sha256": hashlib.sha256(json.dumps(
                    proof, sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest(),
            }
        except (KeyError, TypeError, ValueError, RuntimeError, OSError, TimeoutError):
            logger.exception("LI.FI finalized recovery evidence is incomplete")
            return {"complete": False}

    lifi_local = {
        "nonce_fn": lambda: eth_nonce(address, rpc_url, http=http),
        "gas_price_fn": _node_gas_price,
        "sign_fn": lambda tx: sign_tx(tx, acct),
        "send_fn": _approve_send,
        "receipt_fn": _lifi_receipt,
        "allowance_fn": _lifi_allowance,
        "approve_gas": approve_gas,
        "estimate_gas_fn": _lifi_estimate_gas,
        "construct_tx_fn": _lifi_construct_tx,
        "allowance_at_block_fn": _lifi_allowance_at_block,
        "simulate_fn": _lifi_simulate,
        "sign_complete_fn": _lifi_sign_complete,
        "send_swap_fn_factory": _evm_send_for,
        "balance_raw_fn": lambda token: erc20_balance_raw(
            address, token, rpc_url, http=http),
        "balance_raw_checked_fn": lambda token: erc20_balance_raw_checked(
            address, token, rpc_url, http=http),
        "native_balance_fn": lambda: native_balance_or_none(
            address, rpc_url, http=http),
        "recover_confirmed_fn": _lifi_recover_confirmed,
        "reconcile_drop_fn": _lifi_reconcile_drop,
    }

    _preview_trace = threading.local()
    # ponytail: one recent hint is enough because funded entries run serially per chain.
    # A concurrent overwrite only removes the speed gain. Safety then uses its normal quote path.
    _entry_probe_hint = {
        "mint": None, "tokens": 0, "at": 0.0, "usd": None,
        "quote": None, "quote_token_usd": None, "observed_at": None, "rungs": (),
        "quote_duration_ms": None, "slippage_bps": None,
    }
    _entry_probe_hint_lock = threading.Lock()
    _ENTRY_PROBE_HINT_TTL_S = 15.0
    _ENTRY_QUOTE_TTL_S = 3.0

    def _quote_http(*args, **kwargs):
        if not getattr(_preview_trace, "active", False):
            return http(*args, **kwargs) if http is not None else _http(*args, **kwargs)
        base = http or _http
        try:
            result = base(*args, **kwargs)
        except Exception as exc:
            _preview_trace.exception = exc
            raise
        _preview_trace.response_seen = True
        try:
            status, payload = result[0], result[1]
            if status == 429:
                _preview_trace.failure = "rate_limit"
            elif isinstance(status, int) and status >= 500:
                _preview_trace.failure = "rpc_transport"
            elif (getattr(_preview_trace, "phase", None) == "balance"
                  and status == 200 and isinstance(payload, dict)
                  and payload.get("error") is not None):
                _preview_trace.failure = "rpc_transport"
            elif status == 200 and not isinstance(payload, (dict, list)):
                _preview_trace.failure = "malformed_response"
        except Exception:
            _preview_trace.failure = "malformed_response"
        return result

    if callable(getattr(http, "owned_metadata_headers", None)):
        _quote_http.owned_metadata_headers = http.owned_metadata_headers

    relay_output_guard_verified = False
    if chain == "4663" and cfg.MEMECOIN_EVM_RELAY_ENABLED:
        from sniper.execution.evm_relay_quote import warm_relay_code_hashes
        try:
            protect_output = cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT > 0
            warmed = warm_relay_code_hashes(rpc_url, http=_quote_http, output_guard=protect_output)
            relay_output_guard_verified = protect_output and warmed is True
        except Exception as exc:
            logger.warning("Relay code-hash warmup failed class=%s", type(exc).__name__)
        logger.info("Relay wallet output guard verified=%s", relay_output_guard_verified)

    def _venue_quote(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        """Executable direct quote; injected seams and the live V3 builder share one contract."""
        if venue_quote_fn is not None:
            return venue_quote_fn(
                sell_token, buy_token, amount_atomic, slippage_bps=slippage_bps)
        from sniper.execution.evm_dex_quote import dex_v3_quote
        return dex_v3_quote(chain, sell_token, buy_token, amount_atomic, address,
                            http=_quote_http, slippage_bps=slippage_bps)

    # Entry + safety sell-check quotes: V3 first, V2 Router02 fallback on blind chains — the
    # SAME sell chain the real exit sweep uses, so "can we sell it?" is answered by the venue
    # that would actually do the selling (theveeman 0xf056… refusal, 2026-07-16).
    def _record_rung(record):
        # Per-THREAD: the entry, the exit sweep and the pre-entry safety check share one ladder.
        if getattr(_preview_trace, "rungs", None) is None:
            _preview_trace.rungs = []
        _preview_trace.rungs.append(record)

    from sniper.execution.evm_pons_v2_quote import dex_pons_v2_quote
    _venue_quote_combined = venue_quote_with_v2_fallback(
        _venue_quote, chain=chain, quote_token=quote_token,
        taker=address, http=_quote_http, acct=acct, rpc_url=rpc_url,
        pons_v2_quote_fn=(dex_pons_v2_quote if venue_quote_fn is None else None),
        on_rung=_record_rung, relay_output_guard_verified=relay_output_guard_verified)

    def _record_route_miss(miss_mint, code, detail):
        # NEVER on the trading path's own connection -- this is a diagnostic side-write from deep
        # inside a quote probe that has no `conn` of its own, so it opens and closes one short-lived
        # write for exactly this row. See memecoin_entry_route_misses: the point is that the NEXT
        # `no_route_all_venues` leaves evidence behind instead of being reconstructed after the fact.
        try:
            from sniper.persistence.db_core import connection_scope
            with connection_scope() as _c:
                m.record_entry_route_miss(
                    _c, mint=miss_mint, network_id=chain, code=code, detail=detail,
                    rungs=list(getattr(_preview_trace, "rungs", None) or ()), observed_at=_now_fn())
        except Exception:
            logger.exception("entry route miss record failed mint=%s", miss_mint)

    def prepare_entry_quote_fn(mint, usd, *, on_started=None, route_hint=None):
        """One attempt's exact quote, without mutating the capacity probe's shared hint.
        The same measured ladder runs; this envelope is consumed only for its exact custody,
        chain, mint, clip, slippage and atomic input by execute_fn."""
        started = _time.perf_counter()
        _preview_trace.rungs = []
        memo = _peek_quote_price()
        if not memo or not 0 <= memo["age_ms"] <= 1000:
            return None
        price = memo["px"]
        atomic = int(round(float(usd) / float(price) * 10 ** int(quote["decimals"])))
        if atomic <= 0:
            return None
        # 4663's safety-bearing executor starts at this tolerance, as the capacity probe does.
        slip = (int(round(cfg.MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT * 100))
                if chain == "4663" else cfg.evm_entry_slippage_bps_for(chain))
        envelope = {"mint": str(mint).lower(), "chain": chain, "atomic": atomic,
                "taker": address.lower(), "usd": float(usd), "quote_token_usd": float(price),
                "slippage_bps": slip}
        result = _venue_quote_combined(
            quote_token, mint, atomic, slippage_bps=slip,
            ladder_started_fn=(None if on_started is None else
                               lambda start, wait: on_started(start, wait, envelope)),
            **({"route_hint": route_hint} if route_hint else {}))
        return dict(envelope, quote=result, observed_at=_now_fn(),
                    rungs=tuple(getattr(_preview_trace, "rungs", None) or ()),
                    quote_duration_ms=(_time.perf_counter() - started) * 1000)
    prepare_entry_quote_fn.early_deadline = True

    def entry_quote_probe_fn(mint, usd, *, route_hint=None):
        """Return the typed miss for one read-only entry quote. `route_hint`: the source-route
        pool (evm_source_route), so a probe quote the entry later reuses ran the source rung."""
        quote_started = _time.perf_counter()
        _preview_trace.rungs = []
        snapshot_price = quote_usd_price(chain, http=http, price_fn=price_fn)
        if not snapshot_price:
            return "quote_token_price_unavailable"
        atomic = int(round(float(usd) / float(snapshot_price) * 10 ** int(quote["decimals"])))
        if atomic <= 0:
            return "quote_token_price_unavailable"
        slippage_bps = (int(round(cfg.MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT * 100))
                        if chain == "4663" else cfg.evm_entry_slippage_bps_for(chain))
        try:
            result = _venue_quote_combined(
                quote_token, mint, atomic, slippage_bps=slippage_bps,
                **({"route_hint": route_hint} if route_hint else {}))
        except Exception:
            _record_route_miss(mint, "provider_rpc_transport", "_venue_quote_combined raised")
            return "provider_rpc_transport"
        if result is not None:
            try:
                tokens = int(result.get("buyAmount") or 0)
            except (AttributeError, TypeError, ValueError):
                tokens = 0
            if tokens > 0:
                with _entry_probe_hint_lock:
                    _entry_probe_hint.update(
                        mint=str(mint).lower(), tokens=tokens, at=_time.monotonic(),
                        usd=round(float(usd), 2), quote=dict(result),
                        quote_token_usd=float(snapshot_price), observed_at=_now_fn(),
                        rungs=tuple(getattr(_preview_trace, "rungs", None) or ()),
                        quote_duration_ms=(_time.perf_counter() - quote_started) * 1000.0,
                        slippage_bps=slippage_bps)
            return None
        code, detail = classify_quote_miss(
            list(getattr(_preview_trace, "rungs", None) or ()))
        _record_route_miss(mint, code, detail)
        return code

    def entry_probe_tokens_fn(mint):
        with _entry_probe_hint_lock:
            if (_entry_probe_hint["mint"] != str(mint).lower()
                    or _time.monotonic() - _entry_probe_hint["at"] > _ENTRY_PROBE_HINT_TTL_S):
                return None
            return _entry_probe_hint["tokens"]

    def venue_quote_with_miss_fn(sell_token, buy_token, amount_atomic, *, slippage_bps=None,
                                 **ladder_filter):
        """`_venue_quote_combined`, plus WHY a miss happened. A bare `None` return cannot tell a
        rung that could not READ (infra-blind: rpc_transport, rate limit, timeout) from one that
        read cleanly and found no route -- and token safety needs that distinction so an
        RPC/429 failure at the exact clip size is reported as blind, never as proof the coin
        can't be sold. See `classify_quote_miss` for the code vocabulary."""
        _preview_trace.rungs = []
        # `ladder_filter` is `only_rung` / `exclude_rungs`, for the dip watch's winner ticks.
        result = _venue_quote_combined(
            sell_token, buy_token, amount_atomic, slippage_bps=slippage_bps, **ladder_filter)
        if result is not None:
            return result, None
        code, _detail = classify_quote_miss(list(getattr(_preview_trace, "rungs", None) or ()))
        return None, code

    def _take_entry_quote(mint, usd):
        with _entry_probe_hint_lock:
            if (_entry_probe_hint["quote"] is None
                    or _entry_probe_hint["mint"] != str(mint).lower()
                    or _time.monotonic() - _entry_probe_hint["at"] > _ENTRY_QUOTE_TTL_S
                    or _entry_probe_hint["usd"] != round(float(usd), 2)):
                return None
            cached = dict(_entry_probe_hint)
            _entry_probe_hint["quote"] = None
            return cached

    def _sell_plausible_fn(mint):
        """A sell output below 1/20 of what we paid is the dust-pool signature (SUIT 50977).

        Only for the position this thread is exiting; anything unknown returns None (no check),
        so entries and the pre-entry sell check keep their exact behaviour."""
        same_mint = getattr(_preview_trace, "dust_mint", None) == str(mint or "").lower()
        if same_mint and getattr(_preview_trace, "take_largest", False):
            # ANCHOR BOUND / OVERRIDE (spec 2026-10-01 §3.2): this attempt sells the LARGEST
            # answer, so every answer is "implausible" and the dust wait below collects the rest.
            # No new call: the ladder already asked every venue.
            return lambda _buy_atomic: False
        entry_usd = getattr(_preview_trace, "dust_entry_usd", None)
        if not entry_usd or not same_mint:
            return None

        def _plausible(buy_atomic):
            px = quote_usd_price(chain, http=http, price_fn=price_fn)
            if not px:
                return True
            value = buy_atomic / (10 ** int(quote["decimals"])) * float(px)
            return value >= float(entry_usd) / 20.0
        return _plausible

    def _with_exit_value(result):
        """AN EXIT IS VALUED ON THE WHOLE BAG (2026-09-27, operator). The V2 rung's buyAmount prices
        the bag cut by the 15% max-tax policy; every rail read it: 14 V2-quoted BSC exits filled a
        median +15.6% above their decision quote on real taxes of 1-5%. Called by preview_exit_fn
        only: the entry's sell-route check, the ladder's ranking and the sell's own quote keep the
        cut. buyAmount becomes the value; minBuyAmount and the calldata keep the cut."""
        head = result[0] if isinstance(result, tuple) and result else result
        if not isinstance(head, dict):
            return result
        if "_mixed_exit" in head:
            from sniper.execution.evm_mixed_hop_router import exit_value_amount
            value = exit_value_amount(head, taker=address, http=_quote_http)
        elif "_fot_exit" in head:
            from sniper.execution.evm_v2_exit_quote import exit_value_amount
            value = exit_value_amount(head, http=_quote_http)
        else:
            return result
        head = (dict(head, buyAmount=str(value), _cut_buyAmount=head.get("buyAmount"),
                     _value_basis="whole_bag_less_allowance")
                if value else dict(head, _value_basis="max_tax_cut"))
        return (head,) + tuple(result[1:]) if isinstance(result, tuple) else head

    def _exit_venue_quote(mint, amount_atomic, *, slippage_bps):
        """Use the exact V3 -> V2 -> V4 ladder that admitted the funded entry.

        Entry/exit venue symmetry is a custody invariant: a token bought through the V4 rung must
        not become stranded merely because the exit helper stopped after V2.
        """
        stored_route = getattr(_preview_trace, "evm_route", None)
        mixed_route = (isinstance(stored_route, dict)
                       and stored_route.get("protocol") == "pcs_mixed_v3_v2")
        result = None
        if mixed_route:
            # A POSITION OPENED ON THE MIXED HOP EXITS ON IT (spec 2026-09-25). Its single-venue
            # "routes" are the dust pools the entry avoided, and the priority ladder would take the
            # first of them. This reads the STORED route, not the switch, so turning entries off
            # never strands a held bag. If the mixed route has gone (a pool drained), fall through
            # to the whole ladder and its dust check below.
            from sniper.execution.evm_mixed_hop_router import mixed_exit_quote
            why = blind = None
            try:
                result, why = mixed_exit_quote(
                    chain, mint, quote_token, amount_atomic, address, route=stored_route,
                    http=_quote_http, slippage_bps=slippage_bps)
            except Exception as exc:
                blind = "timeout" if isinstance(exc, TimeoutError) else getattr(
                    exc, "failure_type", "rpc_transport")
            _record_rung({"rung": "mixed_v3_v2_stored", "hit": result is not None,
                          "skipped": False, "blind_reason": blind,
                          **({"reason": why} if why else {})})
        if result is None and isinstance(stored_route, dict) and stored_route.get("bridge") \
                and len(stored_route.get("tokens") or ()) == 3:
            # A POSITION THE BRIDGE RUNG OPENED EXITS ON ITS BRIDGE, OR BETTER (2026-10-02). The
            # priority ladder would sell into the thin direct pool the entry avoided (PI/WBNB read
            # -99.86%). One batch: the stored pair token at every fee-tier pair, plus the direct
            # pools and connectors; the largest output wins. Reads the STORED route, not the
            # switch. Any failure falls through to the whole ladder and its dust check.
            from sniper.execution import evm_dex_quote as _dq
            why = blind = None
            try:
                bridge_token = str(stored_route["tokens"][1]).lower()
                paths = _dq.v3_bridge_paths(chain, mint, quote_token, allowlist=None,
                                            pair_tokens_fn=lambda *_a: (bridge_token,))
                result = _dq.dex_v3_quote(chain, mint, quote_token, amount_atomic, address,
                                          http=_quote_http, slippage_bps=slippage_bps,
                                          extra_paths=paths)
                if result is None:
                    why = "no_bridge_exit_route"
            except Exception as exc:
                result = None
                blind = "timeout" if isinstance(exc, TimeoutError) else getattr(
                    exc, "failure_type", "rpc_transport")
            _record_rung({"rung": "v3_bridge_stored", "hit": result is not None,
                          "skipped": False, "blind_reason": blind,
                          **({"reason": why} if why else {})})
        if result is None and isinstance(stored_route, dict) \
                and stored_route.get("protocol") == "uniswap_v4_multihop":
            from sniper.execution.evm_v4_multihop import dex_v4_multihop_exit_quote
            result = dex_v4_multihop_exit_quote(
                chain, mint, amount_atomic, address, route=stored_route,
                http=_quote_http, slippage_bps=slippage_bps, acct=acct, rpc_url=rpc_url)
            _record_rung({"rung": "v4_multihop_stored", "hit": result is not None,
                          "skipped": False, "blind_reason": None})
        elif result is None:
            plausible_fn = _sell_plausible_fn(mint)
            if plausible_fn is None:
                result = _venue_quote_combined(
                    mint, quote_token, amount_atomic, slippage_bps=slippage_bps,
                )
            else:
                try:
                    result = _venue_quote_combined(
                        mint, quote_token, amount_atomic, slippage_bps=slippage_bps,
                        sell_plausible_fn=plausible_fn,
                    )
                except TypeError:
                    # an injected quote seam without the dust-check argument: behaviour unchanged
                    result = _venue_quote_combined(
                        mint, quote_token, amount_atomic, slippage_bps=slippage_bps,
                    )
        if isinstance(result, dict):
            result = dict(result, _requested_slippage_bps=slippage_bps)
        elif isinstance(result, tuple) and result and isinstance(result[0], dict):
            result = (dict(result[0], _requested_slippage_bps=slippage_bps),) + result[1:]
        return result

    def _use_position_route(position):
        try:
            _preview_trace.evm_route = json.loads(position.get("evm_route_json") or "null")
        except (TypeError, ValueError):
            _preview_trace.evm_route = None
        # The dust check's reference: what we paid for this position (always known, no DB read).
        _preview_trace.dust_mint = str(position.get("mint") or "").lower()
        try:
            _preview_trace.dust_entry_usd = float(position.get("entry_notional") or 0.0) or None
        except (TypeError, ValueError):
            _preview_trace.dust_entry_usd = None

    def execute_fn(conn, pid, mint, usd, now_iso, trace=None, safety_gate_fn=None, quote_safety_fn=None,
                   presign_check_fn=None,
                   impact_cap_pct=None, chase_max_pct=None, wallet_chase_cap_pct=None,
                   wallet_chase_cap_expires_at=None, trace_attempt_base=1, chase_reference=None,
                   early_quote=None, dip_cap=None, entry_signing_snapshot_fn=None, source_block=None):
        row = m.pending_row(conn, pid)
        if not row:
            return {"status": "aborted", "reason": "no pending row"}
        # THE 56 BUILDER A/B (MEMECOIN_BSC_BUILDER_AB, 2026-10-09): the arm is fixed before the
        # build; arm B marks its swap for the builder providers. Both arms read the head right after
        # the send, off the fill path, so the read scores send-to-land blocks.
        builder_arm, builder_swaps, builder_heads, builder_threads = None, [], [], []
        if chain == "56" and _builder_ready():
            from sniper.execution import bsc_builder_ab
            if bsc_builder_ab.knob_on():
                builder_arm = bsc_builder_ab.assign(row.get("attempt_id"))
        if builder_arm is not None and trace is not None:
            trace.mark_substage("bsc_builder_ab", **builder_arm)
        prepared_native_float = []

        def native_float_probe(_quote):
            """Consume the first prepared read once; later attempts obtain their own probe."""
            if prepared_native_float:
                return prepared_native_float.pop()
            from sniper.execution.evm_native_float import LANE_SNAPSHOT, NativeFloatProbe
            from sniper.execution.evm_wallet import native_balance_raw_or_none
            lane = LANE_SNAPSHOT.get()
            snapshot_fn = (lane[1] if lane is not None and lane[0] == chain
                           and cfg.memecoin_evm_native_float_snapshot() else None)
            partial = chain in cfg.memecoin_evm_native_spendable_chains()
            floor = None
            floor_fn = None
            book_facts = {}
            gas_price_fn = _node_gas_price

            def read_native():
                native = native_balance_raw_or_none(address, rpc_url, http=http, retries=0)
                if chain in {"56", "8453"} and native:
                    _node_gas_price()       # warm the existing signer memo off the entry path
                return native

            def ready_gas_price():
                with _gas_lock:
                    at = _gas_memo["at"]
                    return (_gas_memo["gp"] if at is not None
                            and _time.monotonic() - at < _gas_window_s else None)

            def actual_book_floor():
                from contextlib import closing
                from sniper.persistence.db_core import get_readonly_connection
                from sniper.services.memecoin_evm_risk import required_entry_native
                with closing(get_readonly_connection(book_path)) as observer:
                    observer.execute("PRAGMA busy_timeout=0")
                    return int(required_entry_native(observer, chain, own_pending_id=pid, facts=book_facts) * 1e18)

            if partial and chain not in {"56", "8453"}:
                # The full-book reserve sizing kept back (Gamma LOW-2), from the same inputs the
                # gate used; a failed read leaves it unknown and the probe keeps the full unwrap.
                try:
                    from sniper.services.memecoin_evm_risk import required_entry_native
                    floor = int(required_entry_native(conn, chain, own_pending_id=pid, facts=book_facts) * 1e18)
                except Exception:
                    logger.exception("native float: full-book reserve unreadable chain=%s", chain)
            elif chain in {"56", "8453"}:
                # Both floats retain the full book's gas; read its ledger on the probe worker.
                from sniper.persistence.db_core import _connection_db_path
                from sniper.services.memecoin_evm_gas_topup import chain_gas_need_native
                book_path = _connection_db_path(conn)  # local connection metadata, no ledger I/O
                if book_path is None:
                    return None
                book, _, _ = chain_gas_need_native(chain, cfg)
                floor = int(max(book, cfg.gas_reserve_min_native_for(chain)) * 1e18)
                floor_fn = actual_book_floor
                gas_price_fn = ready_gas_price
            return NativeFloatProbe(
                read_native, quote_token=quote_token, gas_price_fn=gas_price_fn,
                low_wei=cfg.MEMECOIN_EVM_GAS_TOPUP_LOW_WEI if chain != "8453" else
                        int(cfg.gas_reserve_min_native_for(chain) * 10 ** 18), partial=partial,
                reserve_floor_wei=floor, reserve_floor_fn=floor_fn,
                wait_for_read=chain not in {"56", "8453"}, book_facts=book_facts,
                snapshot_fn=snapshot_fn if chain in {"56", "8453"} else None)

        if (chain == "56" and not force_lifi and early_quote is not None and trace_attempt_base == 1
                and chain in (cfg.memecoin_evm_native_float_chains()
                              | cfg.memecoin_evm_native_spendable_chains())):
            try:
                prepared_native_float.append(native_float_probe(None))
            except Exception:
                prepared_native_float.append(None)  # optional evidence keeps the existing unwrap
        first_reads = None
        if (chain in {"56", "8453", "4663"} and not force_lifi and early_quote is not None
                and trace_attempt_base == 1
                and chain in cfg.memecoin_evm_balance_beside_ladder_chains()):
            # Read raw custody quantities beside the early join; its accepted frame prices them.
            pool = ThreadPoolExecutor(max_workers=2)
            try:
                first_reads = (pool.submit(injected_usdc_fn if injected_usdc_fn is not None else
                    lambda: erc20_balance_raw_checked(address, quote_token, rpc_url, http=http)),
                    pool.submit(entry_token_fn, mint))
            except BaseException:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            pool.shutdown(wait=False)
        try:
            cached_entry = None
            if not force_lifi and early_quote is not None and trace_attempt_base == 1:
                candidate = early_quote.take(mint, usd, max_age_s=_ENTRY_QUOTE_TTL_S)
                if (isinstance(candidate, dict) and candidate.get("chain") == chain
                        and candidate.get("taker") == address.lower()
                        and candidate.get("mint") == str(mint).lower()
                        and candidate.get("usd") == float(usd)):
                    cached_entry = dict(candidate, early=True)
            if cached_entry is None and not force_lifi:
                cached_entry = _take_entry_quote(mint, usd)
            balance_started = _time.perf_counter()
            balance_snapshot = _execution_balance_usd_fn(
                (cached_entry or {}).get("quote_token_usd"),
                first_raw_read=(first_reads[0].result
                                if first_reads is not None and injected_usdc_fn is None else None))
        except BaseException:
            for future in first_reads or ():
                future.cancel()
            raise
        # decompose the quote stage: the USD snapshot (live ETH/USD HTTP on WETH-quoted chains)
        # was invisible inside quote_ms — canary latency work needs it separately attributable.
        if trace is not None:
            try:
                marker = getattr(trace, "mark_substage", None)
                if marker is not None:
                    marker("evm.balance_usd_snapshot",
                           duration_ms=(_time.perf_counter() - balance_started) * 1000.0,
                           outcome="ok" if balance_snapshot else "unavailable",
                           reused_capacity_price=bool(cached_entry))
            except Exception:
                pass
        if balance_snapshot is None:
            for future in first_reads or ():
                future.cancel()
            m.release_pending(conn, pid, "quote_token_price_unavailable")
            return {"status": "aborted", "reason": "quote-token price unavailable"}
        execution_balance_fn, sell_token_usd = balance_snapshot
        first_balances_fn = (None if first_reads is None else lambda: (
            first_reads[0].result() if injected_usdc_fn is not None else execution_balance_fn(),
            first_reads[1].result()))
        cached_quote = (cached_entry or {}).get("quote")
        cached_available = cached_entry is not None
        first_float_quote = True

        def quote_at_snapshot(token_mint, amount_usd, *, slippage_bps=None, exclude_rungs=()):
            nonlocal cached_quote, cached_available, first_float_quote
            if not first_float_quote:
                prepared_native_float.clear()
            first_float_quote = False
            _preview_trace.rungs = []            # one ledger per quote attempt, never cumulative
            if force_lifi:
                return None
            atomic = int(round(float(amount_usd) / sell_token_usd * 10 ** int(quote["decimals"])))
            if atomic <= 0:
                return None
            requested_slippage_bps = (int(slippage_bps) if slippage_bps is not None else
                                      cfg.evm_entry_slippage_bps_for(chain))
            if (cached_available
                    and cached_entry.get("slippage_bps") == requested_slippage_bps):
                result = dict(cached_quote) if isinstance(cached_quote, dict) else None
                cached_quote, cached_available = None, False
                try:
                    quoted_atomic = (cached_entry.get("atomic") if result is None
                                     else result.get("sellAmount"))
                    exact_amount = int(quoted_atomic or 0) == atomic
                except (AttributeError, TypeError, ValueError):
                    exact_amount = False
                if exact_amount:
                    _preview_trace.rungs = list(cached_entry.get("rungs") or ())
                    if result is None:
                        return None
                    result["_sniper_quote_observed_at"] = cached_entry.get("observed_at")
                    result["_sniper_capacity_quote"] = True
                    if cached_entry.get("early"):
                        result["_sniper_early_quote"] = True
                    result["_sniper_rungs"] = list(cached_entry.get("rungs") or ())
                    result["_sniper_quote_duration_ms"] = cached_entry.get("quote_duration_ms")
                    _preview_trace.rungs = list(cached_entry.get("rungs") or ())
                    return result
            # THE SOURCE'S OWN POOL rides on the event (evm_source_route); None leaves the ladder
            # call exactly as before. `exclude_rungs` is the executor's recheck after a source-route
            # winner failed a price gate: today's rungs alone answer it.
            from sniper.execution import evm_source_route as _src
            _src.seed_pairs(chain, chase_reference, token_mint)     # the source's V2/V3 pair token
            hint = _src.attempt_hint(chain, chase_reference, token_mint)
            extra = {}
            if hint is not None:
                extra["route_hint"] = hint
            if exclude_rungs:
                extra["exclude_rungs"] = tuple(exclude_rungs)
            result = _venue_quote_combined(
                quote_token, token_mint, atomic, slippage_bps=requested_slippage_bps, **extra)
            if isinstance(result, dict):
                result = dict(result)
                result["_sniper_rungs"] = list(getattr(_preview_trace, "rungs", None) or ())
            return result

        def quote_miss_at_snapshot():
            """Why the last `quote_at_snapshot` came back empty. See `classify_quote_miss`."""
            if force_lifi:
                return {"code": "no_route", "detail": "LI.FI canary", "rungs": []}
            rungs = list(getattr(_preview_trace, "rungs", None) or ())
            code, detail = classify_quote_miss(rungs)
            return {"code": code, "detail": detail, "rungs": rungs}

        entry_approval_send = _evm_send_for(
            conn, pid, action="entry_approval", owner_kind="reservation")
        next_nonce = None
        entry_snapshot_allowed = True

        standing_used = []

        def approve_entry(spender, amount):
            nonlocal next_nonce, entry_snapshot_allowed
            prepared_native_float.clear()
            approved = _standing_approve(spender, amount, sell_token_usd, standing_used,
                                         send_fn=entry_approval_send)
            if approved and not (isinstance(approved, dict) and approved.get("standing")):
                entry_snapshot_allowed = False
            if isinstance(approved, dict) and approved.get("nonce") is not None:
                next_nonce = int(approved["nonce"]) + 1
            return approved

        def sign_entry(tx):
            nonlocal next_nonce, entry_snapshot_allowed
            prepared_native_float.clear()
            nonce_hint, next_nonce = next_nonce, None
            # The entry SWAP alone gets the tip floor (chain 1, 2026-10-09); 0 = today's bytes. The
            # plain path signs its WETH unwrap pre-transaction here too, and that keeps today's tip.
            from sniper.execution.evm_native_float import WITHDRAW_SELECTOR
            unwrap = str(tx.get("data") or "").lower().startswith(WITHDRAW_SELECTOR)
            if unwrap:
                entry_snapshot_allowed = False
            snapshot_facts = {}
            if chain == "56" and callable(entry_signing_snapshot_fn) and not entry_snapshot_allowed:
                snapshot_facts = {"outcome": "fallback", "reason": "wallet_mutation"}
            signed = _sign(tx, nonce_hint=nonce_hint,
                           entry=True,
                           tip_floor_wei=0 if unwrap else cfg.evm_entry_tip_floor_wei_for(chain),
                           entry_signing_snapshot_fn=(entry_signing_snapshot_fn
                                                      if entry_snapshot_allowed else None),
                           snapshot_facts=snapshot_facts)
            if snapshot_facts and trace is not None:
                try:
                    trace.mark_substage("evm.entry_signing_snapshot", signed=bool(signed),
                                        **snapshot_facts)
                except Exception:
                    pass
            if signed and not unwrap and builder_arm is not None and builder_arm["arm"] == "B":
                from sniper.execution import bsc_builder_ab
                bsc_builder_ab.mark(signed[1])
                builder_swaps.append(signed[1])
            return signed

        def pipeline_native_pre_transaction(pre):
            """Submit a WETH unwrap ahead of its nonce+1 swap, and wait until its ETH really exists.

            AN APPROVAL MAY BE PIPELINED; AN UNWRAP THE SWAP SPENDS MAY NOT. Nonce order decides
            EXECUTION order once both transactions are mined. It does not decide ADMISSION: a node
            validates the nonce+1 swap's `gas * price + value` against COMMITTED state, and an
            unmined predecessor's balance increase is not credited there. So the swap asks to spend
            ETH that does not exist yet and the node answers `insufficient funds for gas * price +
            value`. An approval is balance-neutral and carries no such dependency, which is why it
            is the only pre-transaction this chain may pipeline.

            Attempt 7204b30c (2026-09-03, SATURN/4663) is the case: the unwrap was accepted at
            13:10:06.373, the swap broadcast 5ms later at nonce+1, and all three providers rejected
            it. The 13:06:26 entry ran the identical path and filled — it won the same race. A race
            this rail cannot see is not a latency win, it is an entry that silently does not happen.
            """
            data = str(pre.get("data") or "").lower()
            try:
                amount = int(data[10:], 16)
                native_value = _evm_quantity(pre.get("value") or 0)
            except (TypeError, ValueError):
                return None
            if (str(pre.get("to") or "").lower() != str(quote_token).lower()
                    or not data.startswith("0x2e1a7d4d") or amount <= 0 or native_value != 0):
                return None
            nonlocal next_nonce
            nonce_hint, next_nonce = next_nonce, None
            signed = _sign(pre, nonce_hint=nonce_hint, entry=True)
            if not signed:
                return {"status": "sign_failed"}
            raw, tx_hash, nonce = signed
            gas_facts = signed_gas_facts(raw)
            # A crash after this binding remains visible to reservation recovery. The later swap
            # binding replaces this pre-transaction identity before the swap broadcast.
            m.set_pending_broadcast(conn, pid, tx_hash, nonce, _now_fn())
            sent = _evm_send_for(
                conn, pid, action="pre_tx", owner_kind="reservation")(raw)
            if sent is False:
                _release_floored_nonce(nonce)
                return {"status": "rejected", "tx_hash": tx_hash, "nonce": nonce, **gas_facts}
            # Both accepted and ambiguous sends require receipt proof before cleanup or swap. A
            # receipt that says REVERTED is proof, not ambiguity: its nonce is consumed (never
            # released) and nothing was unwrapped, so the executor releases the reservation.
            state = pre_transaction_status(_confirm_pre_transaction(tx_hash))
            if state != "confirmed":
                return {"status": state, "tx_hash": tx_hash, "nonce": nonce, **gas_facts}
            # A confirmed unwrap can remain absent from another endpoint's committed state.
            if not _native_visible_across_pool(amount):
                return {"status": "not_visible", "tx_hash": tx_hash, "nonce": nonce, **gas_facts}
            next_nonce = int(nonce) + 1
            return {"status": "accepted", "tx_hash": tx_hash, "nonce": nonce, **gas_facts}

        def _native_before_unwrap():
            """The wallet's native wei on custody, read once before a plain-path unwrap goes out."""
            from sniper.execution.evm_wallet import native_balance_raw_or_none
            return native_balance_raw_or_none(address, rpc_url, http=http, retries=0)

        def cleanup_native(amount_atomic):
            import hashlib
            cleanup_tx = {
                "to": quote_token, "data": "0xd0e30db0", "value": int(amount_atomic),
                "gas": WETH_CALL_GAS_LIMIT,
            }
            signed = _sign(cleanup_tx, entry=True)
            if not signed:
                return "unknown"
            raw, tx_hash, nonce = signed
            if isinstance(raw, str):
                raw_bytes = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
            else:
                raw_bytes = bytes(raw)
            if not m.bind_pending_cleanup(
                    conn, pid, tx_sig=tx_hash, nonce=nonce, amount_atomic=amount_atomic,
                    payload_sha256=hashlib.sha256(raw_bytes).hexdigest(), signed_at=_now_fn()):
                return "unknown"
            sent = _evm_send_for(
                conn, pid, action="entry_cleanup", owner_kind="reservation")(raw)
            if sent is False:
                m.mark_pending_cleanup_status(conn, pid, "explicit_rejected")
                return "unknown"
            status = _confirm(tx_hash)
            m.mark_pending_cleanup_status(conn, pid, status)
            return status

        def execute_lifi(miss):
            if lifi_entry_fn is None:
                return None
            return lifi_entry_fn(
                conn=conn, pending_id=pid, mint=mint, usd_amount=usd,
                now_iso=now_iso, direct_miss=miss, trace=trace,
                sell_token=quote_token, sell_token_usd=sell_token_usd,
                local=lifi_local,
                safety_gate_fn=(lambda: lifi_safety_fn(mint))
                if callable(lifi_safety_fn) else safety_gate_fn,
                impact_cap_pct=impact_cap_pct, chase_max_pct=chase_max_pct,
            )

        # The per-entry gas guard: the entry's quote-token USD, and the trace's numbers through a
        # caller-owned dict (tagged, so the executor passes one; Alpha 2026-10-07).
        _entry_gas_guard = None
        if gas_guard_fn is not None:
            def _entry_gas_guard(usd, facts=None, route=None):
                return gas_guard_fn(usd, native_usd=sell_token_usd, facts=facts, route=route)
            _entry_gas_guard.fills_facts = True
            _entry_gas_guard.takes_route = True      # priced by the quote's route class (2026-10-07)
        result = None
        entry_dispatched = set()
        def _entry_dispatch(tx_hash, selected_quote):
            entry_dispatched.add(str(tx_hash).lower())
            preparation, ready = entry_preparation_at_ingress(trace) if chain == '4663' else (None, None)
            bsc_execution_observer.dispatch(
                row.get('attempt_id'), tx_hash,
                trace.receipt_context().get('receipt_clock')
                if trace is not None and hasattr(trace, 'receipt_context') else None,
                route_lane='shared', venue=(selected_quote.get('route') or {}).get('protocol'),
                quote_asset=quote_token, ready_at_original_ingress=ready,
                entry_preparation=preparation)

        def _entry_confirm(tx_hash):
            state = _confirm(tx_hash)
            # execute_entry_evm also confirms an unwrap. Only a durably dispatched SWAP qualifies;
            # approvals, unwraps, cleanup and exits keep their existing plain confirmation seam.
            if (chain == '4663' and bsc_execution_observer is not None and state == 'confirmed'
                    and str(tx_hash).lower() in entry_dispatched
                    and isinstance(getattr(state, 'receipt', None), dict)):
                try:
                    bsc_execution_observer.observed(
                        row.get('attempt_id'), tx_hash, state.receipt,
                        observed_ns=state.observed_monotonic_ns, boot_id=state.observed_boot_id)
                except Exception:
                    # A lost measurement never delays or vetoes accounting.
                    try:
                        bsc_execution_observer.record_gap({'reason': 'confirmation_publication_failed',
                                                          'attempt_id': row.get('attempt_id')})
                    except Exception:
                        pass
            return state
        try:
            result = execute_entry_evm(
                conn, pending_id=pid, mint=mint, network_id=chain, usd_amount=usd,
                strategy=row["strategy"], source_user_id=row["source_user_id"], now_iso=now_iso,
                quote_fn=quote_at_snapshot, quote_miss_fn=quote_miss_at_snapshot,
                approve_fn=approve_entry, sign_fn=sign_entry,
                send_fn=_head_after_send(
                    _remember_sent(_evm_send_for(conn, pid, action="entry", owner_kind="reservation")),
                    builder_heads, builder_threads, enabled=builder_arm is not None),
                confirm_fn=_entry_confirm if bsc_execution_observer is not None else _confirm,
                usdc_fn=execution_balance_fn, token_fn=entry_token_fn,
                first_balances_fn=first_balances_fn,
                pending=row, trace=trace, sell_decimals=int(quote["decimals"]),
                sell_token_usd=sell_token_usd,
                receipt_token_fn=_receipt_token,
                receipt_spent_fn=_receipt_spent,
                receipt_fn=_receipt,
                revert_reason_fn=lambda h: evm_revert_reason(h, rpc_url, http=http),
                retry_slippage_bps=int(round(
                    cfg.MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT * 100)),
                cleanup_native_fn=cleanup_native,
                safety_gate_fn=safety_gate_fn, quote_safety_fn=quote_safety_fn,
                presign_check_fn=presign_check_fn,
                gas_guard_fn=_entry_gas_guard,
                # EVERY CHAIN BOOKS FROM ITS RECEIPT (Alpha 2026-10-06, after Codex EVM-03): the
                # wallet balance window carried every other transfer into the wallet; 296 of 302
                # ordinary fills 09-06..10-06 were booked from it.
                require_receipt_amounts=True,
                entry_preflight_fn=lambda entry_quote: preflight_weth_entry_quote(
                    entry_quote, quote_token=quote_token, taker=address, rpc_url=rpc_url, http=http),
                chase_max_pct=chase_max_pct,
                wallet_chase_cap_pct=wallet_chase_cap_pct,
                wallet_chase_cap_expires_at=wallet_chase_cap_expires_at,
                native_pre_pipeline_fn=(pipeline_native_pre_transaction
                                        if chain == "4663"
                                        and cfg.memecoin_evm_approval_pipeline(chain) else None),
                # The plain path's unwrap: the same visibility check, at most one read per still-short
                # member every 0.25 s (public Base hosts), within MEMECOIN_EVM_NATIVE_VISIBLE_WAIT_S.
                native_visible_fn=lambda wei: _native_visible_across_pool(wei, poll_s=0.25),
                native_balance_fn=_native_before_unwrap,
                lifi_entry_fn=(execute_lifi if lifi_entry_fn is not None else None),
                trace_attempt_base=trace_attempt_base,
                native_float_fn=(native_float_probe
                                 if chain in (cfg.memecoin_evm_native_float_chains()
                                              | cfg.memecoin_evm_native_spendable_chains())
                                 else None),
                release_nonce_fn=_release_floored_nonce, chase_reference=chase_reference,
                source_block=source_block, dip_cap=dip_cap,
                sender_dispatch_fn=_entry_dispatch if bsc_execution_observer is not None else None,
                booked_fn=((lambda position_id: _start_preapproval(conn, position_id))
                           if chain in cfg.memecoin_evm_preapprove_chains() and _uses_nonce_floor()
                           else None))
            if builder_arm is not None and trace is not None:
                _builder_trace(trace, builder_arm, builder_swaps, builder_heads, builder_threads)
            if (bsc_execution_observer is not None and result.get("status") == "filled"
                    and result.get("tx_hash") in _booking_receipt):
                try:
                    bsc_execution_observer.booked(row.get("attempt_id"), result["tx_hash"],
                                                 _booking_receipt[result["tx_hash"]], result["position_id"])
                except Exception:
                    pass
            return result
        finally:
            _standing_settle(standing_used, result)    # an exception counts as not filled

    # --- exit path (S3-EVM) ---
    from sniper.execution.evm_exit_executor import execute_exit_evm
    from sniper.execution.evm_wallet import erc20_balance_raw

    if chain == "56":
        _execute_entry_fn = execute_fn

        @functools.wraps(_execute_entry_fn)
        def execute_fn(conn, pid, mint, usd, now_iso, *args, **kwargs):
            result = _execute_entry_fn(conn, pid, mint, usd, now_iso, *args, **kwargs)
            # AFTER the fill has returned (never on its path): prewarm the flap quote-token exit's
            # eligibility for this coin off-thread, so its first exit reads memory (Alpha LOW2).
            from sniper.execution import evm_flap_quote_exit as _fqe
            _fqe.prewarm_after_fill(mint, rpc_url, result)
            return result

    def _exit_quote(mint, tokens_human, slip):
        # size the sell from the EXACT raw on-chain balance (never a human float: memecoin balances
        # exceed 2^53 and the round-trip mis-sizes the sell -> revert/false-stuck or dust).
        _preview_trace.rungs = []
        atomic = erc20_balance_raw(address, mint, rpc_url, http=http)
        if atomic <= 0:
            return None
        return _exit_venue_quote(mint, atomic, slippage_bps=int(round(slip * 100)))

    def _exit_quote_miss(_slip):
        rungs = list(getattr(_preview_trace, "rungs", None) or ())
        code, detail = classify_quote_miss(rungs)
        return {"code": code, "detail": detail, "rungs": rungs}

    def preview_exit_fn(position):
        """Quote the exact raw held balance and convert quote-token output to USD."""
        import math
        from dataclasses import replace
        if chain == "56":
            from sniper.settings import memecoin as _cfg_flap
            if _cfg_flap.MEMECOIN_EVM_FLAP_QUOTE_EXIT:
                # The flap quote-token exit's eligibility, read OFF this thread once per coin, so
                # the first exit reads memory instead of paying getTokenV6 (Alpha, 2026-10-02).
                from sniper.execution import evm_flap_quote_exit as _fqe
                _fqe.prewarm((position or {}).get("mint"), rpc_url)
        from sniper.analysis.memecoin_quote_failures import QuoteFailure
        from sniper.execution.exit_preview import ExitPreview, PreviewResult
        from sniper.settings.memecoin import MEMECOIN_EXIT_SLIPPAGE_LADDER
        from sniper.util.clock import real_utc_now_iso
        _use_position_route(position)
        _preview_trace.rungs = []
        _preview_trace.active = True
        _preview_trace.failure = None
        _preview_trace.exception = None
        _preview_trace.response_seen = False
        _preview_trace.phase = "balance"
        try:
            atomic = erc20_balance_raw(address, position["mint"], rpc_url, http=_quote_http)
        finally:
            _preview_trace.active = False
        if atomic <= 0:
            exc = getattr(_preview_trace, "exception", None)
            if isinstance(exc, TimeoutError):
                failure = QuoteFailure.PROVIDER_TIMEOUT
            elif exc is not None:
                failure = QuoteFailure.RPC_TRANSPORT
            else:
                failure = {
                    "rate_limit": QuoteFailure.RATE_LIMIT,
                    "rpc_transport": QuoteFailure.RPC_TRANSPORT,
                    "malformed_response": QuoteFailure.MALFORMED_RESPONSE,
                }.get(getattr(_preview_trace, "failure", None), QuoteFailure.TOKEN_REJECTED)
            return PreviewResult.failed(
                failure, provider_id=f"{chain}:direct")
        input_quantity = None
        try:
            decimals = cached_decimals_fn(position["mint"]) if cached_decimals_fn else None
            if (decimals is None and decimals_fn is not None
                    and (position.get("_exit_custom_cap") or position.get("_mcap_exit_target"))):
                decimals = decimals_fn(position["mint"])
            if type(decimals) is int and 0 <= decimals <= 255:
                quantity = atomic / (10 ** decimals)
                if math.isfinite(quantity) and quantity > 0:
                    input_quantity = quantity
        except Exception:
            pass  # A failed custom-cap metadata read stays unknown; normal exits only read cache.

        def with_quantity(result):
            if isinstance(result, PreviewResult) and isinstance(result.preview, ExitPreview):
                return replace(result, preview=replace(result.preview, input_quantity=input_quantity))
            return result

        if chain == "56" and _cfg_flap.MEMECOIN_EVM_FLAP_QUOTE_EXIT:
            eligible = _fqe.cached_eligibility(position["mint"])
            if eligible is None:
                return PreviewResult.failed(QuoteFailure.MALFORMED_RESPONSE, provider_id=f"{chain}:direct")
            if eligible:
                return with_quantity(_fqe.preview_exit(
                    position["mint"], atomic,
                    chain=_fqe.Chain(rpc_url, address, http=_quote_http),
                    addresses=_fqe.addresses(_cfg_flap), http=_quote_http,
                    price_fn=lambda: quote_usd_price(chain, http=http, price_fn=price_fn)))
        _preview_trace.active = True
        _preview_trace.failure = None
        _preview_trace.exception = None
        _preview_trace.response_seen = False
        _preview_trace.phase = "quote"
        try:
            # FIRST RUNG, not the last (Codex P1, 2026-09-13). The executor reuses this quote's
            # transaction at rung 0 when the balance matches, and calldata carries its minimum
            # output: a last-rung preview signed a 25% floor where the rung allows 3%. The value
            # below reads buyAmount (expected output), which does not depend on slippage.
            result = _exit_venue_quote(
                position["mint"], atomic,
                slippage_bps=int(round(MEMECOIN_EXIT_SLIPPAGE_LADDER[0] * 100)))
            # The DECISION's value only: the sell's own quote (_exit_quote) keeps the cut and gains
            # no read on the send path.
            result = _with_exit_value(result)
        except TimeoutError:
            return PreviewResult.failed(
                QuoteFailure.PROVIDER_TIMEOUT, provider_id=f"{chain}:direct")
        except Exception:
            return PreviewResult.failed(
                QuoteFailure.RPC_TRANSPORT, provider_id=f"{chain}:direct")
        finally:
            _preview_trace.active = False
        if result is None:
            exc = getattr(_preview_trace, "exception", None)
            if isinstance(exc, TimeoutError):
                return PreviewResult.failed(
                    QuoteFailure.PROVIDER_TIMEOUT, provider_id=f"{chain}:direct")
            if exc is not None:
                return PreviewResult.failed(
                    QuoteFailure.RPC_TRANSPORT, provider_id=f"{chain}:direct")
            failure_name = getattr(_preview_trace, "failure", None)
            failure = {
                "rate_limit": QuoteFailure.RATE_LIMIT,
                "rpc_transport": QuoteFailure.RPC_TRANSPORT,
                "malformed_response": QuoteFailure.MALFORMED_RESPONSE,
            }.get(failure_name)
            if failure is None:
                code, _detail = classify_quote_miss(
                    list(getattr(_preview_trace, "rungs", None) or ()))
                failure = QUOTE_MISS_FAILURES.get(code, QuoteFailure.MALFORMED_RESPONSE)
            if failure == QuoteFailure.NO_ROUTE and position.get("lifi_dependent") \
                    and lifi_preview_fn is not None:
                return with_quantity(lifi_preview_fn(
                    position=position, amount_atomic=atomic,
                    direct_miss=_exit_quote_miss(None), local=lifi_local,
                    sell_token=quote_token,
                    sell_token_usd=quote_usd_price(chain, http=http, price_fn=price_fn),
                ))
            return PreviewResult.failed(failure, provider_id=f"{chain}:direct")
        try:
            quote_units = int(result["buyAmount"]) / (10 ** int(quote["decimals"]))
            quote_price = quote_usd_price(chain, http=http, price_fn=price_fn)
            expected = quote_units * float(quote_price)
        except (TypeError, ValueError, KeyError):
            return PreviewResult.failed(
                QuoteFailure.MALFORMED_RESPONSE, provider_id=f"{chain}:direct")
        if not math.isfinite(expected) or expected <= 0:
            return PreviewResult.failed(
                QuoteFailure.INSUFFICIENT_LIQUIDITY, provider_id=f"{chain}:direct")
        route = ((result.get("transaction") or {}).get("to") or "0x")
        from sniper.execution.exit_preview import quote_evidence
        evidence = quote_evidence(
            result, input_atomic=atomic, quote_asset=quote_token,
            quote_asset_decimals=int(quote["decimals"]), quote_asset_usd=float(quote_price),
            slippage_bps=int(round(MEMECOIN_EXIT_SLIPPAGE_LADDER[0] * 100)),
        )
        return PreviewResult.succeeded(
            ExitPreview(expected, atomic, route, real_utc_now_iso(), quote=result, quote_evidence=evidence,
                        input_quantity=input_quantity),
            provider_id=f"{chain}:direct")

    def _approve_token(token, spender, amount):
        """The EXIT's approval. Delegates to `_approve` so it inherits the pipelining the entry
        has had since July, including its fail-closed rules: a pipelined approval proceeds only on
        a POSITIVE acceptance, and both a rejection and a transport ambiguity abort rather than
        queue a swap behind a nonce gap.

        `_sign` on this path is already pipeline-aware and claims approval-nonce+1, so before this
        it was paired with an approver that never set a floored nonce -- a signer expecting a
        pipeline and an approver not running one."""
        return _approve(spender, amount, token=token,
                        tip_floor_wei=cfg.evm_exit_tip_floor_wei_for(chain))

    def _token_allowance(token, spender):
        try:
            return int(_eth_call(token, SEL_ALLOWANCE + _pad_addr(address) + _pad_addr(spender),
                                 rpc_url, http=http), 16)
        except Exception:
            return None

    def _preapprove_position(db_path, position_id, spender=None, route_protocol=None):
        """Approve a booked V4 position's token for Permit2 now, so its exit does not wait for it.

        `route_protocol` names the entry's protocol when the booking keeps no evm_route_json: the
        4663 fast lane books from its receipt, and its own buys are always V4 swaps (2026-10-08).

        Spec 2026-09-30-memecoin-4663-approve-at-entry-design.md. Runs on its own daemon thread
        (`_start_preapproval`) and its own DB connections, through THIS executor's `_approve`, so
        it shares the nonce floor and lock the exits sign under. Exact `balanceOf`, never MAX.
        The exit is unchanged: its allowance check at `latest` skips a mined pre-approval, and a
        pending or failed one leaves it approving exactly as before.
        """
        from sniper.execution.evm_v4_quote import V4
        from sniper.persistence.db_core import connection_scope
        permit2 = str((V4.get(chain) or {}).get("permit2") or "").lower()
        spender = str(spender or permit2).lower()
        with connection_scope(db_path) as c:
            pos = m.get_position(c, position_id) or {}
        mint = str(pos.get("mint") or "").lower()
        rec = {"position_id": position_id, "network_id": chain, "token": mint,
               "spender": spender, "created_at": _now_fn()}
        try:
            route = json.loads(pos.get("evm_route_json") or "null") or {}
        except (TypeError, ValueError):
            route = {}
        sent = {}
        if not permit2 or spender != permit2:
            status = "refused_spender"
        elif not pos.get("is_open") or pos.get("close_claim_id"):
            status = "skipped_closing"
        elif not str(route_protocol or route.get("protocol") or "").startswith("uniswap_v4"):
            status = "skipped_route"
        else:
            status = _preapprove_send(mint, spender, rec, sent)
        rec["send_status"] = status
        with connection_scope(db_path) as c:
            row_id = m.record_evm_preapproval(c, rec)
        if status not in ("accepted", "ambiguous"):
            return
        raw = sent["raw"]
        # The approval may or may not be in a pool. Give its nonce back to the node's count, so no
        # exit is signed over a gap it might not fill (the 2026-07-23 P0 in `_approve`). The rewind
        # never crosses a later allocation: a signer that already took nonce+1 has built on this
        # approval, and only its landing unblocks that signer, so only then are the identical
        # bytes re-offered. After a rewind they never are: any signer may now hold that nonce, and
        # the worst case is one exit approving for itself, which is today's path.
        rewound = status == "ambiguous" and _release_floored_nonce(rec["nonce"])
        receipt_status = _confirm_polled(
            rec["tx_hash"], None if rewound else (lambda: _approve_send(raw)))
        gas_used = gas_price = None
        if receipt_status in ("confirmed", "failed"):
            receipt = _receipt(rec["tx_hash"]) or {}
            try:
                gas_used = int(receipt["gasUsed"], 16)
                gas_price = int(receipt["effectiveGasPrice"], 16)
            except (KeyError, TypeError, ValueError):
                pass
        with connection_scope(db_path) as c:
            m.resolve_evm_preapproval(c, row_id, receipt_status=receipt_status, gas_used=gas_used,
                                      effective_gas_price_wei=gas_price, resolved_at=_now_fn())

    def _preapprove_send(mint, spender, rec, sent):
        """Read balance and allowance, send an exact approval when short. Returns send_status.
        An unreadable balance or allowance is unknown: nothing is sent and the exit approves."""
        try:
            balance = int(erc20_balance_raw_checked(address, mint, rpc_url, http=http))
        except Exception:
            return "skipped_read"
        allowance = _token_allowance(mint, spender)
        if allowance is None:
            return "skipped_read"
        if balance <= 0:
            return "skipped_balance_zero"   # a node lagging the fill, not an allowance that covers
        if allowance >= balance:
            return "skipped_allowance"
        rec["amount_atomic"] = str(balance)

        def capture(raw):
            sent["raw"] = raw
            sent["result"] = _approve_send(raw)
            return sent["result"]

        approved = _approve(spender, balance, send_fn=capture, token=mint,
                            tip_floor_wei=cfg.evm_exit_tip_floor_wei_for(chain),
                            wait_for_receipt=False)
        if "raw" not in sent:
            return "not_sent"
        from eth_utils import keccak
        rec["tx_hash"] = "0x" + keccak(hexstr=str(sent["raw"])).hex().removeprefix("0x")
        import base64
        rec["nonce"] = (decode_signed_tx_cost(
            base64.b64encode(str(sent["raw"]).encode("ascii")).decode("ascii")) or {}).get("nonce")
        if isinstance(approved, dict):
            return "accepted"
        if sent["result"] is False:
            return "rejected"             # `_approve` already released the nonce
        return "ambiguous"

    def _start_preapproval(conn, position_id):
        # The thread writes through its own connection to the entry's own ledger file.
        db_path = conn.execute("PRAGMA database_list").fetchone()[2]
        if not db_path:
            return                        # in-memory ledger: nothing another connection can see
        threading.Thread(target=_preapprove_logged, args=(db_path, position_id),
                         name="preapprove-%s-%s" % (chain, position_id), daemon=True).start()

    def _preapprove_logged(db_path, position_id):
        try:
            _preapprove_position(db_path, position_id)
        except Exception:
            logger.exception("pre-approval failed chain=%s pos=%s", chain, position_id)

    def _token_decimals_for_reuse(mint):
        """Decimals to compare the confirmed preview's raw amount with the human balance."""
        if decimals_fn is not None:
            return decimals_fn(mint)
        from sniper.execution.evm_wallet import erc20_decimals
        return erc20_decimals(mint, rpc_url, http=http)

    def _stop_quote_checker(conn, position, quote_asset_usd, exit_reason="b_stop"):
        def _check_stop_quote(fresh, payload, tokens):
            from sniper.execution.evm_exit_executor import _quoted_unit_price
            from sniper.services.memecoin_real_runner import confirm_execution_stop, confirm_execution_target, _keep_route_quote
            from sniper.execution.exit_preview import quote_evidence

            if exit_reason in {"real_tp", "mcap_target"}:
                try:
                    unit = _quoted_unit_price(payload, tokens, quote_asset_usd, int(quote["decimals"]))
                    expected = unit * tokens if unit is not None else None
                    minimum = (int(payload.get("otherAmountThreshold") if payload.get("otherAmountThreshold") is not None
                                   else payload.get("minBuyAmount"))
                               / (10 ** int(quote["decimals"])) * float(quote_asset_usd))
                except (KeyError, TypeError, ValueError, OverflowError):
                    expected, minimum = None, None
                return confirm_execution_target(conn, fresh, exit_reason, expected, tokens,
                                                minimum_usdc=minimum)
            unit = _quoted_unit_price(payload, tokens, quote_asset_usd, int(quote["decimals"]))
            expected = unit * tokens if unit is not None else None
            _keep_route_quote(dict(fresh, open_qty=tokens), {
                "expected_usdc": expected,
                "quote_evidence": quote_evidence(
                    payload, quote_asset=quote_token, quote_asset_decimals=int(quote["decimals"]),
                    quote_asset_usd=quote_asset_usd),
            }, "execution_quote")
            return confirm_execution_stop(
                conn, fresh, expected, tokens,
                current_mcap=position.get("_exit_current_mcap"),
                bag_unknown=bool(position.get("_exit_bag_unknown")),
                confirmed_custom_stop=bool(position.get("_exit_custom_stop_confirmed")),
            )
        return _check_stop_quote

    _RUNG_KEYS = ("rung", "hit", "skipped", "blind_reason", "reason", "round_trip_pct", "duration_ms")

    def _anchor_check_for(position, exit_reason, quote_asset_usd, inner, audit, unit_price_fn=None):
        """The anchor hold (spec 2026-10-01 §3.2) composed AFTER `inner` (the b_stop confirmation),
        so a stop that defers never counts as a held attempt. Chain off: `inner`, untouched.

        Before the quote it sets the thread's take-largest flag on a bound/override attempt; the
        caller clears it. Each held tick and held sale lands in `audit`, written after the
        executor returns so nothing is committed between the check and the signature."""
        _preview_trace.take_largest = False
        from sniper.settings import memecoin as cfg
        if chain not in cfg.memecoin_evm_exit_anchor_chains():
            return inner
        from sniper.services import memecoin_exit_anchor as A
        hold_s = cfg.MEMECOIN_EVM_EXIT_ANCHOR_HOLD_S
        min_pct = cfg.MEMECOIN_EVM_EXIT_ANCHOR_MIN_PCT
        mode = A.forced(position["id"], hold_s=hold_s)
        _preview_trace.take_largest = mode is not None

        def _check(fresh, payload, tokens):
            allowed, detail = inner(fresh, payload, tokens) if inner is not None else (True, {})
            if not allowed:
                return allowed, detail
            from sniper.execution.evm_exit_executor import _quoted_unit_price
            anchor = A.effective_anchor(fresh)
            unit = (unit_price_fn(payload, tokens) if unit_price_fn else
                    _quoted_unit_price(payload, tokens, quote_asset_usd, int(quote["decimals"])))
            if anchor is None or unit is None:
                return allowed, detail             # unknown is today's behaviour, never a hold
            decision, held_s = A.decide(fresh["id"], unit >= anchor * min_pct / 100.0,
                                        hold_s=hold_s, mode=mode, reason=exit_reason)
            if decision == A.PASS:
                return allowed, detail
            from sniper.util.clock import real_utc_now_iso
            route = payload.get("route") if isinstance(payload.get("route"), dict) else {}
            audit.append({
                "observed_at": real_utc_now_iso(), "decision": decision,
                "route": route.get("protocol") or payload.get("_sniper_winner"),
                "value_usd": unit * float(tokens), "anchor_price": anchor, "qty": float(tokens),
                "held_s": round(held_s, 3), "exit_reason": exit_reason,
                "rungs_json": json.dumps(
                    [{k: r[k] for k in _RUNG_KEYS if r.get(k) is not None}
                     for r in (getattr(_preview_trace, "rungs", None) or ()) if isinstance(r, dict)],
                    separators=(",", ":"))})
            if decision == A.HELD:
                logger.warning("exit anchor HOLD pos=%s chain=%s value=$%.2f anchor_value=$%.2f "
                               "held_s=%.1f reason=%s", fresh["id"], chain, unit * float(tokens),
                               anchor * float(tokens), held_s, exit_reason)
                return False, {"code": "held_anchor"}
            return allowed, detail
        return _check

    def _write_anchor_audit(conn, position, audit, result=None):
        # A bound attempt the exit dust guard then HELD sold nothing: it is recorded as such and
        # never pages "sold at bound" (the guard pages its own hold once per episode).
        dust_held = isinstance(result, dict) and result.get("status") == "dust_route_hold"
        for row in audit:
            if dust_held and row.get("decision") == "sold_at_bound":
                row = {**row, "decision": "dust_held_at_bound"}
            try:
                m.record_exit_anchor_audit(conn, position_id=position["id"], network_id=chain, **row)
            except Exception:                    # an audit row must never fail a funded exit
                logger.exception("exit anchor audit write failed pos=%s", position.get("id"))
            if row["decision"] == "sold_at_bound":
                try:
                    from sniper.services.memecoin_alerts import alert_exit_anchor_bound
                    alert_exit_anchor_bound(position, chain, row)
                except Exception:
                    logger.exception("exit anchor bound page failed pos=%s", position.get("id"))

    def _flap_quote_exit(conn, position, exit_reason, now_iso):
        """The flap.sh quote-token exit (evm_flap_quote_exit.runner_exit) with this executor's own
        seams, or None for the ladder. Runs under exit_fn's serialize_executor lock."""
        from sniper.execution import evm_flap_quote_exit as fqe
        from sniper.settings import memecoin as cfg

        fqe.prewarm(position.get("mint"), rpc_url)
        exit_reason = fqe.initiating_reason(fqe.read_journal(fqe.journal_path(position["id"])), exit_reason)
        anchor_audit = []

        def confirm(fresh, payload, qty):
            from sniper.services.memecoin_real_runner import executable_exit_allowed, confirm_execution_target, _EXECUTABLE_PRICE_REASONS
            if exit_reason not in _EXECUTABLE_PRICE_REASONS:
                return True, {"code": "event_exit"}
            if exit_reason in {"real_tp", "mcap_target"}:
                # The two-leg estimate is not a minimum bound into one signed transaction.
                return confirm_execution_target(conn, fresh, exit_reason,
                                                payload.get("expected_usdc"), qty,
                                                minimum_usdc=None)
            return executable_exit_allowed(
                conn, fresh, exit_reason, {"expected_usdc": payload["expected_usdc"]},
                override=m.effective_exit_override(conn, fresh["id"]),
                current_mcap=position.get("_exit_current_mcap"), quote_qty=qty)

        check = _anchor_check_for(position, exit_reason, None, confirm, anchor_audit,
                                  unit_price_fn=lambda payload, qty: payload["expected_usdc"] / qty)

        dust_held = []          # the guard held this run: its refusal reaches the runner as "aborted"

        def actual_quote_check(pos, expected, floor_usd, qty):
            from sniper.services import memecoin_exit_dust_guard as dust
            from sniper.services.memecoin_exit_intent import exit_floor_refused
            from sniper.services.memecoin_exits import last_accepted_mid
            fresh = m.get_position(conn, pos["id"])
            payload = {"expected_usdc": expected, "floor_usd": floor_usd,
                       "route": {"protocol": "flap_quote_exit"}}
            allowed, detail = check(fresh, payload, qty)
            if allowed:
                held = dust.hold_reason(conn, fresh, exit_reason=exit_reason, floor_usd=floor_usd,
                                        sell_qty=qty, now_iso=now_iso, expected_usd=expected)
                if held:
                    dust_held.append(held)
                    allowed, detail = False, {"code": "dust_route_hold"}
                elif exit_floor_refused(None, payload, fresh, qty,
                        floor_usd_fn=lambda *_: floor_usd,
                        reference_value_usd_fn=lambda pos, qty: (
                            (last_accepted_mid(conn, chain, pos["mint"], now_iso) or 0.) * qty),
                        logger=logger, position_id=fresh["id"]):
                    allowed, detail = False, {"code": "exit_floor_refused"}
            detail.update(execution_quote=True, route="flap_quote_exit", floor_usd=floor_usd)
            try:
                m.record_exit_trigger_audit(conn, position_id=pos["id"],
                    reason=exit_reason + "_execution", display_price=None, preview_usdc=expected,
                    allowed=allowed, detail=json.dumps(detail, sort_keys=True), checked_at=now_iso,
                    lane="flap_execution")
            except Exception:
                logger.exception("flap execution confirmation audit failed pos=%s", pos["id"])
                return False, "execution_audit_unavailable"
            return allowed, detail.get("code")

        def run_factory(pos, path):
            pid = pos["id"]
            return fqe.Run(
                conn, pid, chain=fqe.Chain(
                    rpc_url, address,
                    send_fn=_remember_sent(_evm_send_for(conn, pid, action="exit",
                                                         owner_kind="close_claim")),
                    approve_send_fn=_approve_send, read_budget=fqe.read_budget(pid)),
                sign_fn=lambda tx: sign_tx(tx, acct), journal_path=path, price_fn=price_fn,
                addresses=fqe.addresses(cfg), slippage_bps=fqe.DEFAULT_SLIPPAGE_BPS,
                step_budget_s=fqe.STEP_BUDGET_S, quote_check_fn=actual_quote_check, exit_reason=exit_reason,
                out=lambda line: logger.warning("[memecoin] flap quote exit pos=%s %s", pid, line))

        def ladder_value(pos):
            # For grading only: the ladder's whole-balance answer, logged beside the core's.
            _use_position_route(pos)
            quote_ = _exit_quote(pos["mint"], None, cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER[0])
            return int(quote_["buyAmount"]) if quote_ and quote_.get("buyAmount") else None

        result = None
        try:
            result = fqe.runner_exit(conn, position, exit_reason, now_iso, run_factory=run_factory,
                                     eligible_fn=lambda mint: fqe.cached_eligibility(mint) is True,
                                     ladder_value_fn=ladder_value)
            return result
        finally:
            _preview_trace.take_largest = False
            _write_anchor_audit(conn, position, anchor_audit,
                                {"status": "dust_route_hold"} if dust_held else result)

    def _dust_hold_fn(conn, exit_reason, now_iso, floor_usd_fn):
        """The executor's dust-route hold (memecoin_exit_dust_guard), on this exit's own floor."""
        from sniper.services import memecoin_exit_dust_guard as dust

        def hold(intent, quote_payload, pos, sell_qty):
            try:
                floor = floor_usd_fn(intent, quote_payload)
            except Exception:
                floor = None
            return dust.hold_reason(conn, pos, exit_reason=exit_reason, floor_usd=floor,
                                    sell_qty=sell_qty, now_iso=now_iso,
                                    quote_payload=quote_payload)
        return hold

    def _replace_exit_sign(tx0x, *, nonce, replaces):
        """Sign the chain-1 same-nonce replacement: the held nonce, both fees past Protect's
        replacement rule over the held bytes. No unwrap (it would spend this nonce): native that
        cannot cover gas x maxFee + value refuses, and the held sell stands."""
        from sniper.execution import chain1_protect
        from sniper.execution.evm_wallet import native_balance_raw_or_none
        floor = chain1_protect.replacement_fee_floor(replaces)
        if floor is None or not isinstance(nonce, int):
            return None
        try:
            need = (_evm_quantity(tx0x.get("gas") or default_swap_gas) * int(floor[0])
                    + _evm_quantity(tx0x.get("value") or 0))
        except (KeyError, TypeError, ValueError):
            return None
        native = native_balance_raw_or_none(address, rpc_url, http=http)
        if native is None or native < need:
            logger.warning("chain1 replace: native %s under %d wei for the bumped fee -- not signed",
                           native, need)
            return None
        signed = _sign(tx0x, nonce_pin=nonce, fee_floor=floor,
                       tip_floor_wei=cfg.evm_exit_tip_floor_wei_for(chain))
        # The signed maxFee may sit above the floor (headroom cap): a node refuses on the SIGNED
        # worst case, so check that, not the floor (Gamma LOW-4).
        if signed and not chain1_protect.replacement_affordable(signed[0], native):
            logger.warning("chain1 replace: native %s under the signed worst case -- not sent", native)
            return None
        return signed

    def replace_close_fn(conn, position):
        """Chain-1 Protect same-nonce replacement of a held full exit (`replace_exit_evm`), called
        by the recovery pass in the exit lane. The ladder, floor and dust guards are the close's."""
        from sniper.execution.evm_exit_executor import replace_exit_evm
        from sniper.execution.exec_tiering import exit_tier_plan
        from sniper.settings import memecoin as cfg
        from sniper.util.clock import real_utc_now_iso
        if chain != "1":
            return {"status": "skipped", "reason": "not_chain_1"}
        _use_position_route(position)
        snapshot = _execution_balance_usd_fn()
        if snapshot is None:
            return {"status": "skipped", "reason": "quote-token price unavailable"}
        _balance_fn, sell_token_usd = snapshot
        now_iso = real_utc_now_iso()
        plan = exit_tier_plan(position, cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER, chain=chain, cfg=cfg)

        # The full close's exit-floor sanity (UNIFROG 27097), exactly as exit_fn's.
        def _floor_usd_fn(intent, _quote_payload):
            try:
                atomic = int(getattr(intent, "min_output_atomic", 0) or 0)
                if atomic <= 0 or not sell_token_usd:
                    return None
                return (atomic / (10 ** int(quote["decimals"]))) * float(sell_token_usd)
            except (TypeError, ValueError, ZeroDivisionError):
                return None

        def _reference_value_usd_fn(pos, tokens_human):
            try:
                from sniper.services.memecoin_exits import last_accepted_mid
                mid = last_accepted_mid(conn, chain, pos["mint"], now_iso)
                if not mid or not tokens_human:
                    return None
                return float(tokens_human) * float(mid)
            except Exception:
                return None

        # No stop re-check: the operator's ruling, see replace_exit_evm.
        return replace_exit_evm(
            conn, position, now_iso, quote_fn=_exit_quote, sign_fn=_replace_exit_sign,
            send_fn=_remember_sent(_evm_send_for(
                conn, position["id"], action="exit", owner_kind="close_claim")),
            token_balance_fn=token_fn, slippage_ladder=plan["slippage_ladder"],
            quote_asset_usd=sell_token_usd, floor_usd_fn=_floor_usd_fn,
            reference_value_usd_fn=_reference_value_usd_fn,
            dust_hold_fn=_dust_hold_fn(conn, position.get("close_exit_reason") or "b_stop",
                                       now_iso, _floor_usd_fn),
            max_replacements=cfg.MEMECOIN_CHAIN1_PROTECT_REPLACE_MAX)

    def exit_fn(conn, position, exit_reason, now_iso):
        import time
        from sniper.execution.exec_tiering import exit_tier_plan
        from sniper.settings import memecoin as cfg
        if chain == "56" and cfg.MEMECOIN_EVM_FLAP_QUOTE_EXIT:
            flap = _flap_quote_exit(conn, position, exit_reason, now_iso)
            if flap is not None:
                return flap
        _use_position_route(position)
        balance_snapshot = _execution_balance_usd_fn()
        if balance_snapshot is None:
            return {"status": "aborted", "reason": "quote-token price unavailable"}
        execution_balance_fn, _sell_token_usd = balance_snapshot
        plan = exit_tier_plan(position, cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER,
                              chain=chain, cfg=cfg)
        stop_check = (_stop_quote_checker(conn, position, _sell_token_usd, exit_reason)
                      if exit_reason in {"b_stop", "real_tp", "mcap_target"} else None)
        anchor_audit = []
        quote_check = _anchor_check_for(position, exit_reason, _sell_token_usd, stop_check,
                                        anchor_audit)

        # Exit-floor sanity (UNIFROG 27097). The signed floor is converted to USD through the
        # same bridge the preview uses -- quote-token atomic / 10**decimals x quote-token USD --
        # and compared against the bag valued at the newest NON-quarantined tape mark. The
        # reference has to come from outside the quote being signed: a quote taken during a
        # corrupted-tape window is internally consistent and still authorises selling for
        # nothing. Both seams return None on any doubt, and the check allows the exit when it
        # cannot judge, so a price-source outage can never strand a position.
        def _floor_usd_fn(intent, _quote_payload):
            try:
                atomic = int(getattr(intent, "min_output_atomic", 0) or 0)
                if atomic <= 0 or not _sell_token_usd:
                    return None
                return (atomic / (10 ** int(quote["decimals"]))) * float(_sell_token_usd)
            except (TypeError, ValueError, ZeroDivisionError):
                return None

        def _reference_value_usd_fn(pos, tokens_human):
            try:
                from sniper.services.memecoin_exits import last_accepted_mid
                mid = last_accepted_mid(conn, chain, pos["mint"], now_iso)
                if not mid or not tokens_human:
                    return None
                return float(tokens_human) * float(mid)
            except Exception:
                return None

        started = time.monotonic()
        def _lifi_exit(**kwargs):
            if lifi_exit_fn is None:
                return None
            return lifi_exit_fn(
                conn=conn, local=lifi_local, sell_token=quote_token,
                sell_token_usd=_sell_token_usd, **kwargs)

        def _terminal_finalize(exit_kwargs):
            matches = [
                row for row in m.unresolved_lifi_lifecycles(conn, chain, address)
                if row.get("position_id") == position["id"]
            ]
            if len(matches) != 1:
                return False
            row = matches[0]
            return m.finalize_lifi_close(
                conn, lifecycle_id=row["lifecycle_id"], expected_state=row["state"],
                exit_kwargs=exit_kwargs, created_at=now_iso,
            )

        # EXIT STAGE MAP (2026-09-02). A funded 4663 exit costs 5-7 SECONDS from decision to closed
        # (real_tp n=27 avg 6344ms, b_stop n=8 avg 6949ms, vol_death n=4 avg 7172ms; solana p50
        # 1438ms) and `memecoin_exit_execution_attempts` records only `total_ms`, so WHERE those
        # seconds go has never been observed. The entry path has a stage map and it disproved the
        # obvious guess: on 4663 `chain_confirmation_ms` is p50 405ms while `quote_ms` is p50 1871ms
        # and p90 9152ms. The chain is fast; our quoting is not. The exit is unmeasured and must
        # stop being guessed about.
        #
        # Wrapped at the CALL SITE rather than inside execute_exit_evm: the executor runs a
        # slippage ladder and any of these may be called several times, so accumulating per stage
        # here counts total time in each stage without threading state through the ladder, and
        # without editing money logic to measure it. Counts are recorded too -- a stage entered 4
        # times is a retry story, and a mean would hide it.
        _stage_ms, _stage_n = {}, {}

        def _stage_map_json(stage_ms, stage_n):
            """Flat {stage}_ms / {stage}_n so a query can read one key without unpacking nesting.
            Returns None on an empty map so an exit that reached no stage stores NULL rather than
            an empty object that would read as "measured, all zero"."""
            if not stage_ms:
                return None
            flat = {}
            for name, ms in stage_ms.items():
                flat[f"{name}_ms"] = round(float(ms), 1)
                flat[f"{name}_n"] = int(stage_n.get(name, 0))
            return json.dumps(flat, sort_keys=True)

        def _timed(name, fn):
            if fn is None:
                return None

            def wrapper(*args, **kwargs):
                mark = time.monotonic()
                try:
                    return fn(*args, **kwargs)
                finally:
                    # `finally`, so a raising stage still reports the time it burned. A stage that
                    # fails slowly is the interesting one and must not vanish from the map.
                    _stage_ms[name] = _stage_ms.get(name, 0.0) + (time.monotonic() - mark) * 1000.0
                    _stage_n[name] = _stage_n.get(name, 0) + 1
            return wrapper

        from sniper.util.clock import real_utc_now_iso
        def _capacity_partial(fraction):
            override = m.effective_exit_override(conn, position["id"]) or {}
            if exit_reason == "mcap_target" or (exit_reason == "real_tp"
                                                and override.get("tp_target_mcap") is not None):
                return {"status": "deferred", "reason": "custom_target_partial_unconfirmed"}
            return derisk_fn(conn, position, fraction, now_iso, stop_check=exit_reason == "b_stop")

        try:
            result = execute_exit_evm(
                conn, position, exit_reason, now_iso,
                quote_fn=_timed("quote", _exit_quote), approve_fn=_timed("approve", _approve_token),
                sign_fn=_timed("sign", _make_exit_sign_fn(conn, position["id"], "close_claim")),
                send_fn=_timed("send", _remember_sent(_evm_send_for(
                    conn, position["id"], action="exit", owner_kind="close_claim"))),
                confirm_fn=_timed("confirm", _exit_confirm),
                usdc_fn=execution_balance_fn, token_balance_fn=token_fn,
                allowance_fn=_token_allowance,
                receipt_proceeds_fn=lambda h: _receipt_quote_usd(h, _sell_token_usd),
                require_receipt_proceeds=True,   # every chain: the sell's own receipt (Alpha 2026-10-06)
                quote_asset_usd=_sell_token_usd,
                quote_asset_decimals=int(quote["decimals"]),
                slippage_ladder=plan["slippage_ladder"],
                decision_state_id=position.get("_engine_decision_state_id"),
                decision_state_id_fn=position.get("_engine_current_state_id_fn"),
                floor_usd_fn=_floor_usd_fn, reference_value_usd_fn=_reference_value_usd_fn,
                dust_hold_fn=_dust_hold_fn(conn, exit_reason, now_iso, _floor_usd_fn),
                quote_miss_fn=_exit_quote_miss,
                lifi_quote_fn=(_lifi_exit if lifi_exit_fn is not None else None),
                terminal_finalize_fn=_terminal_finalize,
                partial_exit_fn=_capacity_partial,
                # a bound/override attempt must reach the ladder: the prepared preview is one answer
                prepared_preview=(None if getattr(_preview_trace, "take_largest", False)
                                  else position.get("_confirmed_exit_preview")),
                quote_check_fn=quote_check,
                token_decimals_fn=_token_decimals_for_reuse,
                now_fn=real_utc_now_iso)
        finally:
            _preview_trace.take_largest = False
        _total_ms = (time.monotonic() - started) * 1000.0
        _write_anchor_audit(conn, position, anchor_audit, result)
        try:
            # key=value so this is greppable and aggregatable without a schema change. Unmeasured
            # remainder is stated explicitly rather than left for a reader to subtract and get wrong.
            _named = sum(_stage_ms.values())
            _other = _total_ms - _named
            if _other < 0:
                # Currently unreachable: the five wrapped stages never call one another and the
                # executor has no threads, so their intervals are disjoint and inside the window.
                # If this ever fires that invariant has broken and the whole map is wrong, which is
                # worth saying out loud rather than clamping to a quiet zero (Gamma, review of
                # b7eead50). The clamp stays so the log line itself cannot mislead.
                logger.warning(
                    "exit stage map INVARIANT BROKEN pos=%s: stages sum to %.0fms of a %.0fms "
                    "window; a stage now nests or runs concurrently and the map is unreliable",
                    position["id"], _named, _total_ms)
            # `preview_ms` is bought in the SWEEP, before this seam's clock starts, so it is NOT
            # inside total_ms. Printed separately and added into decision_ms, which is the number
            # that answers "how long from deciding to exit until the position is closed".
            _preview_ms = float(position.get("_exit_preview_ms") or 0.0)
            # A preview bought in the batched pre-pass has no attributable cost. Reporting it as
            # `preview_ms=0` would read as FREE and bias every batched exit to look cheaper, so the
            # source is named and the number is withheld rather than faked. `decision_ms` is then
            # only a true decision-to-closed figure for preview_src=own.
            _preview_src = str(position.get("_exit_preview_src") or "none")
            _preview_txt = "unknown" if _preview_src == "batch" else f"{_preview_ms:.0f}"
            _decision_txt = ("unknown" if _preview_src == "batch"
                             else f"{_total_ms + _preview_ms:.0f}")
            # WARNING, NOT INFO (2026-09-02). The runner configures no logging level, so the root
            # logger sits at WARNING and every `logger.info` on this path is DISCARDED. Proven on
            # the first funded exit after this map shipped: position 48336 closed at 15:14:28Z,
            # `memecoin_exit_execution_attempts` has its row with total_ms=6907, the WARNING-level
            # `vol_death:` line for the same exit is in the log, and `exit stage map` appears zero
            # times. We waited a day for an instrument that could not have printed. One line per
            # funded exit is not log noise, and this is measurement we deliberately built.
            logger.warning(
                "exit stage map pos=%s chain=%s reason=%s status=%s decision_ms=%s total_ms=%.0f "
                "preview_ms=%s preview_src=%s %s other_ms=%.0f",
                position["id"], chain, exit_reason, result.get("status"),
                _decision_txt, _total_ms, _preview_txt, _preview_src,
                " ".join(f"{k}_ms={v:.0f} {k}_n={_stage_n[k]}"
                         for k, v in sorted(_stage_ms.items(), key=lambda kv: -kv[1])),
                max(0.0, _other))
        except Exception:                          # telemetry must never break a funded exit
            logger.exception("exit stage map log failed pos=%s", position.get("id"))
        try:
            m.record_exit_execution(
                conn, position_id=position["id"], network_id=chain, exit_reason=exit_reason,
                attempted_at=now_iso, status=result.get("status") or "unknown",
                sig=result.get("tx_hash"), total_ms=_total_ms,
                depth_first_rung_pct=(plan["slippage_ladder"][0]
                                      if plan["slippage_ladder"] else None),
                tier_variant=plan["tier_variant"],
                # WHERE the time went, on the row rather than only in a log line. The map was
                # already computed for the log above; ten of those lines survived rotation against
                # 469 exit attempts, so this is the same measurement made durable and queryable.
                stage_ms_json=_stage_map_json(_stage_ms, _stage_n),
                # The CAUSE, not just the status. `reason` is the balance/route refusal,
                # `failure_type` the exhausted-ladder one -- both write `stuck` and only
                # this separates them.
                failure_type=(result.get("reason") or result.get("failure_type")))
        except Exception:
            # Audit-only: the swap already happened and `result` is returned either way,
            # so this must never raise -- but it must never be invisible. A bare `pass`
            # here discards the very cause `0d0f258f` was written to record.
            logger.exception("exit attempt audit write failed pos=%s", position.get("id"))
        return result

    def _partial_exit_quote(mint, tokens_human, slip, fraction, *, atomic=None):
        # fraction × the EXACT raw balance (same 2^53-safe rule as the full exit). int() floors,
        # so a partial can never round UP into selling more than held.
        if atomic is not None:
            part = int(atomic)
        else:
            atomic = erc20_balance_raw(address, mint, rpc_url, http=http)
            part = int(atomic * float(fraction))
        if part <= 0:
            return None
        return _exit_venue_quote(mint, part, slippage_bps=int(round(slip * 100)))

    def derisk_fn(conn, position, fraction, now_iso, *, stop_check=False, source_request=None):
        # EVM analog of the Solana partial exit (2026-07-17 — derisk on 4663 previously did not
        # exist: every trigger logged 'no derisk_fn (skipped)' and rode full size).
        from sniper.settings import memecoin as cfg
        if chain == "56" and cfg.MEMECOIN_EVM_FLAP_QUOTE_EXIT:
            from sniper.execution import evm_flap_quote_exit as fqe
            if fqe.eligible(position.get("mint"), rpc_url):
                # Full exits only for this class: a partial would ride the ladder's dust route.
                return {"status": "derisk_failed", "reason": "flap quote-token coin: full exits only"}
        _use_position_route(position)
        from sniper.execution.evm_exit_executor import execute_partial_exit_evm
        balance_snapshot = _execution_balance_usd_fn()
        if balance_snapshot is None:
            return {"status": "derisk_failed", "reason": "quote-token price unavailable"}
        execution_balance_fn, _sell_token_usd = balance_snapshot
        import time
        from sniper.execution.exec_tiering import exit_tier_plan
        from sniper.settings import memecoin as cfg
        plan = exit_tier_plan(position, cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER,
                              chain=chain, cfg=cfg)

        # Same exit-floor sanity as the full close. `sell_quantity` is the fraction actually sold,
        # so a small de-risk is not measured against the whole bag.
        def _floor_usd_fn(intent, _quote_payload):
            try:
                atomic = int(getattr(intent, "min_output_atomic", 0) or 0)
                if atomic <= 0 or not _sell_token_usd:
                    return None
                return (atomic / (10 ** int(quote["decimals"]))) * float(_sell_token_usd)
            except (TypeError, ValueError, ZeroDivisionError):
                return None

        def _reference_value_usd_fn(pos, sell_quantity):
            try:
                from sniper.services.memecoin_exits import last_accepted_mid
                mid = last_accepted_mid(conn, chain, pos["mint"], now_iso)
                if not mid or not sell_quantity:
                    return None
                return float(sell_quantity) * float(mid)
            except Exception:
                return None

        anchor_audit = []
        quote_check = _anchor_check_for(
            position, "derisk", _sell_token_usd,
            _stop_quote_checker(conn, position, _sell_token_usd) if stop_check else None, anchor_audit)
        started = time.monotonic()
        try:
            result = execute_partial_exit_evm(
                conn, position, fraction, now_iso, quote_fn=_partial_exit_quote,
                approve_fn=_approve_token,
                sign_fn=_make_exit_sign_fn(conn, position["id"], "derisk_claim"),
                send_fn=_remember_sent(_evm_send_for(conn, position["id"], action="partial_exit",
                                                     owner_kind="derisk_claim")),
                confirm_fn=_confirm,
                usdc_fn=execution_balance_fn, token_balance_fn=token_fn,
                receipt_proceeds_fn=lambda h: _receipt_quote_usd(h, _sell_token_usd),
                require_receipt_proceeds=True,   # every chain: the sell's own receipt (Alpha 2026-10-06)
                quote_asset_usd=_sell_token_usd,
                quote_asset_decimals=int(quote["decimals"]),
                quote_check_fn=quote_check,
                slippage_ladder=plan["slippage_ladder"],
                decision_state_id=position.get("_engine_decision_state_id"),
                decision_state_id_fn=position.get("_engine_current_state_id_fn"),
                floor_usd_fn=_floor_usd_fn, reference_value_usd_fn=_reference_value_usd_fn,
                dust_hold_fn=_dust_hold_fn(conn, "derisk", now_iso, _floor_usd_fn),
                quote_miss_fn=_exit_quote_miss,
                lifi_quote_fn=(
                    (lambda **kwargs: lifi_exit_fn(
                        conn=conn, local=lifi_local, sell_token=quote_token,
                        sell_token_usd=_sell_token_usd, **kwargs))
                    if lifi_exit_fn is not None and source_request is None else None),
                **({"source_request": source_request, "token_snapshot_fn": source_partial_snapshot,
                    "receipt_fill_fn": lambda sig, mint, intent=None: source_partial_receipt_fn(
                        sig, mint, dict(intent or {}, quote_asset_usd=_sell_token_usd))}
                   if source_request is not None else {}))
        finally:
            _preview_trace.take_largest = False
        try:
            m.record_exit_execution(
                conn, position_id=position["id"], network_id=chain, exit_reason="source_partial" if source_request else "derisk",
                attempted_at=now_iso, status=result.get("status") or "unknown",
                sig=result.get("tx_hash"), total_ms=(time.monotonic() - started) * 1000.0,
                depth_first_rung_pct=(plan["slippage_ladder"][0]
                                      if plan["slippage_ladder"] else None),
                tier_variant=plan["tier_variant"],
                # The CAUSE, not just the status. `reason` is the balance/route refusal,
                # `failure_type` the exhausted-ladder one -- both write `stuck` and only
                # this separates them.
                failure_type=(result.get("reason") or result.get("failure_type")))
        except Exception:
            # Audit-only: the swap already happened and `result` is returned either way,
            # so this must never raise -- but it must never be invisible. A bare `pass`
            # here discards the very cause `0d0f258f` was written to record.
            logger.exception("derisk attempt audit write failed pos=%s", position.get("id"))
        _write_anchor_audit(conn, position, anchor_audit, result)
        return result

    def source_partial_snapshot(mint):
        from types import SimpleNamespace
        atomic = erc20_balance_raw_checked(address, mint, rpc_url, http=http)
        decimals = decimals_fn(mint) if decimals_fn else erc20_decimals(mint, rpc_url, http=http)
        if type(atomic) is not int or type(decimals) is not int or not 0 <= decimals <= 255:
            raise ValueError('exact partial balance unavailable')
        return SimpleNamespace(atomic=atomic, decimals=decimals, human=atomic / 10 ** decimals)

    def source_partial_receipt_fn(sig, mint, intent=None):
        receipt = _canonical_receipt(sig)
        if receipt is None:
            return None
        atomic = -receipt_token_net(receipt, mint, address)
        frozen = intent or {}
        decimals = frozen.get('sell_decimals')
        price = frozen.get('quote_asset_usd')
        if type(atomic) is not int or atomic <= 0 or type(decimals) is not int or price is None:
            return None
        received = _receipt_quote_usd(sig, price)
        if received is None:
            return None
        return dict(sold_atomic=atomic, decimals=decimals, received=received)

    def source_partial_fn(conn, position, request, now_iso):
        facts = json.loads(request['request_json'])
        fraction = int(facts['source_sold_atomic']) / int(facts['source_pre_atomic'])
        return derisk_fn(conn, position, fraction, now_iso, source_request=request)

    return {"execute_fn": execute_fn, "exit_fn": exit_fn, "derisk_fn": derisk_fn,
            "source_partial_fn": source_partial_fn, "source_partial_receipt_fn": source_partial_receipt_fn,
            "source_partial_snapshot_fn": source_partial_snapshot,
            "source_partial_proof_snapshot_fn": lambda mint: eth_proof_snapshot(
                address, mint, rpc_url, http=http, decimals_fn=decimals_fn, include_atomic=True),
            "bsc_execution_observer": bsc_execution_observer,
            "preview_exit_fn": preview_exit_fn,
            "replace_close_fn": replace_close_fn,
            # WARM THE QUOTE-TOKEN PRICE MEMO (2026-09-02). `execute_fn`'s first act is
            # `_execution_balance_usd_fn`, one live Binance ETH/USD read, and the entry quote cannot
            # start without it because it sizes the input amount. Measured on 63 filled 4663
            # entries: 411ms p50, 7350ms p90 — 27.4% of the serial entry path, and 66 of 66 missed
            # the 1.0s memo, because the only earlier fetch happens in the runner's balance poll
            # seconds beforehand. Calling this at entry start puts the SAME read into the dead time
            # beside the reservation (532ms p50) and the safety worker, so the executor's read is a
            # memo hit. It is not a longer TTL: the window stays 1.0s and the funded live-pricing
            # invariant is untouched. This only makes a staleness the memo already permits actually
            # get used.
            "warm_quote_price_fn": (lambda: quote_usd_price(chain, http=http, price_fn=price_fn)),
            "quote_price_peek_fn": _peek_quote_price,
            "start_quote_price_warmer_fn": _start_quote_price_warmer,
            "execution_balance_usd_fn": _execution_balance_usd_fn,
            "entry_quote_probe_fn": entry_quote_probe_fn,
            "prepare_entry_quote_fn": prepare_entry_quote_fn,
            "entry_probe_tokens_fn": entry_probe_tokens_fn,
            "venue_quote_fn": _venue_quote_combined,
            "take_quote_rungs_fn": functools.partial(take_rungs, _preview_trace),
            "venue_quote_with_miss_fn": venue_quote_with_miss_fn,
            "quote_token": quote_token,
            "address": address, "chain_id": chain,
            "usdc_fn": usdc_fn, "token_fn": token_fn, "token_checked_fn": token_checked_fn,
            "rpc_url": rpc_url,
            "acct": acct, "http": http, "_approve": _approve, "_sign": _sign, "_send": _send,
            "_remember_sent": _remember_sent,
            "_confirm": _confirm, "_confirm_once": _confirm_once,
            "_exit_sign_gate": _exit_sign_gate, "_make_exit_sign_fn": _make_exit_sign_fn,
            "confirmed_nonce_fn": lambda: eth_nonce(address, rpc_url, http=http, block_tag="latest"),
            # "pending" counts our own unmined transactions too, so pending == latest is the
            # node saying it holds NOTHING of ours. The recovery needs both numbers to tell a
            # sell that was never admitted from one still sitting in a mempool.
            "pending_nonce_fn": lambda: eth_nonce(address, rpc_url, http=http, block_tag="pending"),
            # Claim only after maintenance's bounded pure reads have completed.
            "gas_claim_nonce_fn": (_floored_nonce
                if chain == "8453" and _uses_nonce_floor() else None),
            "gas_release_nonce_fn": (_release_floored_nonce
                if chain == "8453" and _uses_nonce_floor() else None),
            # ONE BLOCK'S VIEW for the protocol-expiry dead proof (spec v3 §3.7): chain time, the
            # mined nonce and the coin balance, all at the same block. None on any failure, and
            # None is no proof.
            "proof_snapshot_fn": lambda mint: eth_proof_snapshot(
                address, mint, rpc_url, http=http, decimals_fn=decimals_fn),
            "recover_confirmed_entry_fn": recover_confirmed_entry_fn,
            # The 4663 fast lane's nonce through the shared floor, its rewind, and today's buffer.
            "lane_claim_nonce_fn": _lane_claim_nonce,
            "lane_release_nonce_fn": _release_floored_nonce,
            "gasprice_buffer": gasprice_buffer,
            # OP-stack fee of one of our transactions (Ink Task 8): canonical receipt + the
            # GasPriceOracle at its block. None off the OP-stack fee chains.
            # Despite the key's name, on an L1 fee chain (1) this is the mainnet receipt reader:
            # gasUsed x effectiveGasPrice. The leg's fee reconcile thread starts for either.
            "op_fee_fn": ((lambda tx_hash: _op_fees.read_tx_fee(tx_hash, rpc_url, http=http))
                          if chain in _op_fees.OP_STACK_FEE_CHAINS else
                          (lambda tx_hash: _op_fees.read_l1_tx_fee(tx_hash, rpc_url, http=http))
                          if chain in _op_fees.L1_FEE_CHAINS else None),
            # The entry gas guard (spec §4.3), on the price the entry will sign with: the same
            # memoized read `sign` makes a moment later, so it costs no extra request.
            "gas_guard_fn": gas_guard_fn,
            "gas_price_fn": _node_gas_price,
            # (number, timestamp) of the chain head, for Ink's coherent safety state (Task 5a).
            "head_fn": ((lambda: latest_head(rpc_url, http=http)) if chain == "57073" else None),
            "preapprove_position_fn": _preapprove_position,
            "standing_approve_fn": _standing_approve, "standing_settle_fn": _standing_settle,
            "standing_topup_fn": _standing_topup, "standing_start_fn": _standing_start,
            "standing_bootstrap_fn": _standing_bootstrap,
            "_node_gas_price": _node_gas_price, "_expected_gas_price": _expected_gas_price,
            "lifi_local": lifi_local}
