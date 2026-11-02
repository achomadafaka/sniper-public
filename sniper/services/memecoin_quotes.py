"""Executable route quoting for Solana and Robinhood chain 4663.

RENAMED FROM `memecoin_v6_paper_quotes` (2026-08-30). The old name was wrong and the wrongness
was dangerous: this is generic quoting infrastructure that the FUNDED EXIT SWEEP depends on --
`memecoin_exits.py` -> `memecoin_copy_trader.executable_exit_fill` -> `quote_exit` here -- plus
the refusal capture, the sol tape worker and the dashboard. Exactly three lines in 968 referenced
anything V6, and both are settings constants; there are no V6 tables and no `db_memecoin_v6`
import. With V6 stood down as a lane, a module called `v6_paper_quotes` reads as dead code, and
archiving it would have taken funded exits, the dashboard and the radar down together. The
contents are unchanged by that rename -- only the name was ever V6.

Quote-only modeled fills. A ``ModeledLiveFill`` is a fresh executable route quote turned into
fill evidence -- nothing here signs, sends, reserves, or loads a funded key, and the persisted
``quote_payload`` carries only amounts and route identity (never calldata, permit, or a
transaction). Entries have no price fallback: a missing/stale/zero quote or unresolved decimals
raises rather than guessing from a source-trader price, DexScreener, GeckoTerminal, or a mid.

A ``ModeledLiveFill`` is a fresh executable route quote turned into fill evidence — nothing here
signs, sends, reserves, or loads a funded key, and the persisted ``quote_payload`` carries only
amounts and route identity (never calldata, permit, or a transaction). Entries have no price
fallback: a missing/stale/zero quote or unresolved decimals raises rather than guessing from a
source-trader price, DexScreener, GeckoTerminal, or a chart mid.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from collections import OrderedDict
from collections.abc import Mapping
from enum import Enum
import functools
import json

from sniper.analysis.memecoin_quote_failures import (
    TOKEN_CLASSES, QuoteFailure as TypedQuoteFailure,
)
from sniper.execution.evm_v4_quote import v4_quote_amount_out
from sniper.execution.evm_dex_quote import dex_v3_quote
from sniper.execution.evm_v2_exit_quote import dex_v2_exit_quote
from sniper.execution.evm_wallet import (
    CHAIN_QUOTE, _http as _evm_http, erc20_decimals, quote_usd_price,
)
from sniper.execution.solana_wallet import USDC_MINT, _http as _solana_http
from sniper.services.memecoin_v6_engine import fingerprint
from sniper.settings.memecoin_v6_paper import (
    MEMECOIN_V6_EVM_FACTORIES_4663, MEMECOIN_V6_EVM_FACTORIES_BY_CHAIN,
    MEMECOIN_V6_EVM_V2_CONNECTORS_4663, MEMECOIN_V6_EVM_V2_CONNECTORS_BY_CHAIN,
    MEMECOIN_V6_EVM_V2_ROUTER_4663, MEMECOIN_V6_EVM_V2_ROUTER_BY_CHAIN,
    MEMECOIN_V6_EVM_V2_SLIPPAGE_BPS, MEMECOIN_V6_EVM_V2_TAX_BUFFER_BPS,
    MEMECOIN_V6_EVM_V3_ADDRESSES_4663, MEMECOIN_V6_EVM_V3_ADDRESSES_BY_CHAIN,
    MEMECOIN_V6_EVM_V3_FEE_TIERS,
    MEMECOIN_V6_EVM_V3_SLIPPAGE_BPS, MEMECOIN_V6_PAPER_QUOTE_MAX_AGE_S,
)
from sniper.util.clock import real_utc_now_iso

import logging

logger = logging.getLogger(__name__)

USDC_LABEL = "USDC"
USDC_DECIMALS = 6
# A fixed nonzero paper-only address that encodes 4663 quote eth_calls. It never holds a key and is
# never a signer — only the recipient slot in a read-only quote call.
PAPER_QUOTE_TAKER = "0x" + "00" * 19 + "01"
# TWO HOSTS, ONE PROVIDER — the funded seam's pattern, copied because the paper lane had neither.
# `lite-api` is the KEYLESS rate-limited tier and it BANS BY IP: on 2026-08-14 it answered 429 to
# every request from this host for over an hour while `api.jup.ag` served complete quotes. The
# funded runner already quotes Jupiter from this same IP at ~162 calls per 1 RPC call, so a keyless
# paper consumer is competing with funded for the same keyless budget.
from sniper.execution.solana_real_seams import (        # noqa: E402
    ROLE_PAPER, note_role_quote as _note_role_quote,
    note_role_rate_limited as _note_role_rate_limited)

JUPITER_LITE_URL = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_PORTAL_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_QUOTE_URL = JUPITER_LITE_URL          # kept: existing readers import this name

# Quote failure reasons. THESE WERE ONE VALUE UNTIL 2026-08-28 and that is the whole problem: a 429
# and a genuinely unroutable coin both became `None`, surfaced as `skipped_no_executable_quote`, and
# 95% of curated-time buy events were written off as unpriceable with no way to tell which cause it
# was. Split BEFORE the key lands, so the key's effect is attributable rather than assumed.
QUOTE_OK = None
QUOTE_NO_ROUTE = "no_route"
QUOTE_RATE_LIMITED = "rate_limited"
QUOTE_PROVIDER_ERROR = "provider_error"

# JUPITER SAYS "NO ROUTE" WITH A 400, NOT A 200. Verified live 2026-08-28: a syntactically valid but
# untradable mint returns HTTP 400 with
#     {"error":"The token ... is not tradable","errorCode":"TOKEN_NOT_TRADABLE"}
# so classifying purely on status would have filed every genuinely unroutable coin as
# `provider_error` and the split would have been useless in exactly the direction it exists to
# measure. The error CODE is the discriminator, not the status.
_NO_ROUTE_CODES = frozenset({
    "TOKEN_NOT_TRADABLE", "COULD_NOT_FIND_ANY_ROUTE", "NO_ROUTES_FOUND",
    "ROUTE_PLAN_DOES_NOT_CONSUME_ALL_THE_AMOUNT", "CIRCULAR_ARBITRAGE_IS_DISABLED",
})


def _classify_error_body(payload) -> str:
    """`no_route` when the provider ANSWERED that the token cannot be routed, else provider_error."""
    if not isinstance(payload, dict):
        return QUOTE_PROVIDER_ERROR
    code = str(payload.get("errorCode") or "").upper()
    if code in _NO_ROUTE_CODES:
        return QUOTE_NO_ROUTE
    text = str(payload.get("error") or "").lower()
    if "not tradable" in text or "no route" in text or "could not find any route" in text:
        return QUOTE_NO_ROUTE
    return QUOTE_PROVIDER_ERROR


def _error_payload(exc):
    """Read a JSON body off an HTTPError once, tolerating anything."""
    try:
        import json as _j
        return _j.loads(exc.read().decode(errors="replace") or "{}")
    except Exception:
        return None


def _paper_jupiter_key():
    """The PAPER key, never the funded one. Sharing `MEMECOIN_JUPITER_API_KEY` would spend the
    funded quota on research traffic — the 2026-08-21 trap with a new consumer. Read by name only;
    the value is placed in `.env` by the operator and never appears in code or logs.

    SEPARATION FROM FUNDED IS REAL; SEPARATION FROM CAPTURE IS NOT (verified 2026-08-30, by
    comparing value fingerprints, never the values). The three env vars hold TWO distinct keys:
    funded is its own, and `MEMECOIN_PAPER_JUPITER_API_KEY` and `MEMECOIN_CAPTURE_JUPITER_API_KEY`
    are byte-identical. Jupiter rate-limits per ACCOUNT rather than per key, so paper and capture
    share one 10/s bucket and capture CAN starve paper. Three key names do not mean three budgets.
    No funded exposure either way."""
    import os
    return (os.getenv("MEMECOIN_PAPER_JUPITER_API_KEY") or "").strip()


_KEY_ANNOUNCED = [False]


def _jupiter_hosts():
    """Portal first WHEN KEYED, because the key is issued for the portal tier and `lite-api`
    honours no key — sending one there buys nothing. Keyless, order is unchanged from before.

    ANNOUNCE THE KEY STATE ONCE. A key that is set in `.env` but absent from THIS process's
    environment produces behaviour identical to no key at all: `os.getenv` returns None, the host
    order silently reverts to keyless, and every downstream number looks like a capacity result
    when it is a configuration miss. The runner loads `.env` itself so the lane is fine, but a
    research script, a smoke harness or a one-off import is not, and nothing would say so. Same
    pattern and same reason as `solana_real_seams`, which declares which key role a process spends
    at startup. Logged once per process, never the value.
    """
    key = _paper_jupiter_key()
    if not _KEY_ANNOUNCED[0]:
        _KEY_ANNOUNCED[0] = True
        logger.info("jupiter(paper): key configured=%s, host order=%s", bool(key),
                    "portal-first" if key else "lite-first (KEYLESS)")
    if key:
        return ((JUPITER_PORTAL_URL, key), (JUPITER_LITE_URL, ""))
    return ((JUPITER_LITE_URL, ""), (JUPITER_PORTAL_URL, ""))
_V3_GET_POOL = "1698ee82"
_V2_GET_PAIR = "e6a43905"
_POOL_BINDING_CACHE = OrderedDict()
_POOL_BINDING_CACHE_MAX = 1024


def jupiter_quote_detailed(input_mint, output_mint, amount_atomic, slippage_bps, http=None):
    """Returns `(payload, reason, host)`. `reason` is None on success, else one of the QUOTE_*
    constants. `host` is the hostname that produced the final answer, so a later read can attribute
    a failure rate to a tier — the gap `JUPITER_LOAD` had to close afterwards, closed here at birth.

    Tries the second host only when the first fails, and returns the LAST host's reason. A 429 on
    every host is `rate_limited`; an honest empty route is `no_route`. These must never collapse.
    """
    request = http or _solana_http
    reason, host = QUOTE_PROVIDER_ERROR, None
    # COUNTED AGAINST THE PAPER ROLE, at the one place this lane issues a quote. This module has
    # its OWN host ladder and never reaches `solana_real_seams._jup_call`, so the shared counter
    # could not see it — the promotion study's resumption gate was waiting on a paper number that
    # this path structurally could not produce. Role is passed EXPLICITLY rather than read from
    # the ambient context: this lane is paper by construction, and inferring it would make the
    # count depend on whether a caller remembered a scope.
    _note_role_quote(ROLE_PAPER)
    for base, key in _jupiter_hosts():
        host = base.split("/")[2]
        url = (f"{base}?inputMint={input_mint}&outputMint={output_mint}"
               f"&amount={int(amount_atomic)}&slippageBps={int(slippage_bps)}&swapMode=ExactIn")
        headers = {"x-api-key": key} if key else None
        try:
            status, payload, _date = request("GET", url, headers) if headers else request("GET", url)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == 429:
                reason = QUOTE_RATE_LIMITED
                _note_role_rate_limited(ROLE_PAPER)
                continue
            # A 4xx carries the provider's own explanation. `no_route` is a terminal ANSWER — trying
            # the second host cannot make an untradable token tradable — so it returns immediately
            # rather than burning a request on a foregone conclusion.
            classified = _classify_error_body(_error_payload(exc)) if code else QUOTE_PROVIDER_ERROR
            if classified == QUOTE_NO_ROUTE:
                return None, QUOTE_NO_ROUTE, host
            reason = classified
            continue
        if status == 429:
            reason = QUOTE_RATE_LIMITED
            _note_role_rate_limited(ROLE_PAPER)
            continue
        if status != 200 or not isinstance(payload, dict):
            classified = _classify_error_body(payload)
            if classified == QUOTE_NO_ROUTE:
                return None, QUOTE_NO_ROUTE, host
            reason = classified
            continue
        if payload.get("error") or "outAmount" not in payload:
            # The provider answered on a 200 and said there is no route. Same terminal answer.
            return None, _classify_error_body(payload) or QUOTE_NO_ROUTE, host
        return payload, QUOTE_OK, host
    return None, reason, host


def _jupiter_quote(input_mint, output_mint, amount_atomic, slippage_bps, http=None):
    """Back-compatible shim: payload or None. Callers that need the cause use the detailed form."""
    payload, _reason, _host = jupiter_quote_detailed(
        input_mint, output_mint, amount_atomic, slippage_bps, http=http)
    return payload


LEGACY_FILL_MODEL = "modeled_live_quote"
ROUTE_FLOOR_FILL_MODEL = "route-floor-v1"


def _scored_out_atomic(raw: dict, *, fill_model_version: str, label: str) -> tuple[int, int]:
    """Return ``(scored, expected)`` output amounts for one Jupiter quote.

    Under ``route-floor-v1`` the scored amount is ``otherAmountThreshold`` -- the
    slippage floor the route guarantees -- and ``outAmount`` is kept only as a
    diagnostic. Every other fill model keeps scoring ``outAmount``, so existing
    fomo-source positions are untouched and their accrued evidence still means what
    it meant.

    Both legs must use the same model. Scoring the floor on one leg and the expected
    amount on the other manufactures a spread that does not exist.
    """
    expected = _positive_int(raw.get("outAmount"), "outAmount")
    if fill_model_version != ROUTE_FLOOR_FILL_MODEL:
        return expected, expected
    threshold = raw.get("otherAmountThreshold")
    if threshold is None:
        raise QuoteUnavailable(
            f"{label}: route-floor-v1 requires otherAmountThreshold",
            failure=TypedQuoteFailure.NO_ROUTE,
        )
    return _positive_int(threshold, "otherAmountThreshold"), expected


def jupiter_entry_quote_detailed(mint, usd_amount, *, http=None, slippage_bps=300):
    """`(payload, reason, host)` — the cause-preserving form the paper lane needs."""
    return jupiter_quote_detailed(
        USDC_MINT, mint, int(round(float(usd_amount) * 10 ** USDC_DECIMALS)), slippage_bps,
        http=http)


def jupiter_entry_quote(mint, usd_amount, *, http=None, slippage_bps=300):
    return _jupiter_quote(
        USDC_MINT, mint, int(round(float(usd_amount) * 1_000_000)), slippage_bps, http)


def jupiter_exit_quote(mint, amount_atomic, slippage_pct, *, http=None):
    request = http or _solana_http
    observed = {}

    def tracked(*args, **kwargs):
        try:
            response = request(*args, **kwargs)
        except Exception as exc:
            observed["exception"] = exc
            raise
        observed["response"] = response
        return response

    raw = _jupiter_quote(
        mint, USDC_MINT, amount_atomic, int(round(float(slippage_pct) * 100)), tracked)
    if raw is not None:
        return raw
    exc = observed.get("exception")
    if isinstance(exc, TimeoutError):
        failure = TypedQuoteFailure.PROVIDER_TIMEOUT
    elif exc is not None:
        failure = TypedQuoteFailure.RPC_TRANSPORT
    else:
        response = observed.get("response")
        if not isinstance(response, tuple) or len(response) < 2:
            failure = TypedQuoteFailure.MALFORMED_RESPONSE
        else:
            status, payload = response[0], response[1]
            message = (str(payload.get("error") or payload.get("message") or "").lower()
                       if isinstance(payload, Mapping) else "")
            if status == 429:
                failure = TypedQuoteFailure.RATE_LIMIT
            elif isinstance(status, int) and status >= 500:
                failure = TypedQuoteFailure.RPC_TRANSPORT
            elif "liquid" in message:
                failure = TypedQuoteFailure.INSUFFICIENT_LIQUIDITY
            elif "token" in message or "mint" in message:
                failure = TypedQuoteFailure.TOKEN_REJECTED
            elif "route" in message:
                failure = TypedQuoteFailure.NO_ROUTE
            else:
                failure = TypedQuoteFailure.MALFORMED_RESPONSE
    raise QuoteUnavailable("solana exit quote unavailable", failure=failure)


def token_decimals(mint, rpc_url, *, http=None):
    """SPL decimals for `mint`, or None when the read failed (never a guess).

    CACHED PER (mint, rpc_url), SUCCESSES ONLY (2026-09-26). Every paper Solana close made one
    uncached getTokenSupply on the funded custody URL from the main loop, 16.8/h over 7 days, and
    one of those reads held the loop into the 2026-09-22 10:31Z sweep_stale kill. A mint's decimals
    cannot change, so there is no TTL. A failure raises inside the cached helper and lru_cache never
    stores an exception, so an unknown is re-read next time rather than remembered. 0 is a real
    answer (a 0-decimal mint) and is cached like any other. The mint string is the identity: this
    function is Solana-only (EVM uses erc20_decimals), and a base58 mint cannot collide with a 0x
    address because base58 has no '0'. Only the default transport is cached: an injected `http`
    (a test stub, the refusal capture's per-call instrument) reads through exactly as before, so
    no seam ever sees another seam's answer and no instrument object is kept alive by the cache.
    """
    try:
        if http is not None:
            return _token_decimals_read(mint, rpc_url, http)
        return _token_decimals_cached(mint, rpc_url)
    except Exception:
        return None


@functools.lru_cache(maxsize=4096)
def _token_decimals_cached(mint, rpc_url):
    return _token_decimals_read(mint, rpc_url, None)


def _token_decimals_read(mint, rpc_url, http):
    request = http or _solana_http
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint],
    })
    status, payload, _date = request("POST", rpc_url, body=body)
    if status != 200:
        raise RuntimeError("getTokenSupply HTTP %s" % status)
    return int(payload["result"]["value"]["decimals"])


def usd_to_quote_atomic(chain_id, usd, *, http=None, price_fn=None):
    quote = CHAIN_QUOTE.get(str(chain_id))
    price = quote_usd_price(chain_id, http=http, price_fn=price_fn)
    if not quote or not price:
        return None
    amount = int(round(float(usd) / price * 10 ** int(quote["decimals"])))
    return amount if amount > 0 else None


def _evm_call_fn(rpc_url, http=None):
    request = http or _evm_http

    def post(payload):
        if not rpc_url:
            return None
        try:
            _status, response, _detail = request("POST", rpc_url, body=json.dumps(payload))
        except Exception:
            return None
        return response

    def call(to, data):
        response = post({
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        })
        if not isinstance(response, dict) or response.get("error"):
            return None
        return response.get("result")

    def batch(calls):
        response = post([
            {"jsonrpc": "2.0", "id": index, "method": "eth_call",
             "params": [{"to": to, "data": data}, "latest"]}
            for index, (to, data) in enumerate(calls, 1)
        ])
        if not isinstance(response, list):
            return None
        by_id = {item.get("id"): item.get("result") for item in response
                 if isinstance(item, dict) and not item.get("error")}
        return [by_id.get(index) for index in range(1, len(calls) + 1)]

    call.batch = batch
    return call


def _evm_provider_failure(rpc_url, http=None):
    """Return an infrastructure failure when the read RPC itself is not usable."""
    if not rpc_url:
        return TypedQuoteFailure.MALFORMED_RESPONSE
    request = http or _evm_http
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
    try:
        status, payload, _detail = request("POST", rpc_url, body=body)
    except TimeoutError:
        return TypedQuoteFailure.PROVIDER_TIMEOUT
    except Exception:
        return TypedQuoteFailure.RPC_TRANSPORT
    if status == 429:
        return TypedQuoteFailure.RATE_LIMIT
    if status != 200:
        return TypedQuoteFailure.RPC_TRANSPORT
    if not isinstance(payload, Mapping) or payload.get("error") or not payload.get("result"):
        return TypedQuoteFailure.MALFORMED_RESPONSE
    return None


class QuoteUnavailable(Exception):
    """No fresh executable route quote could be obtained (fail-closed; no price fallback)."""

    def __init__(self, message: str, *, failure: TypedQuoteFailure | None = None):
        super().__init__(message)
        self.failure = failure


class QuoteStale(Exception):
    """A quote was obtained but is older than the allowed freshness window."""


class IdentityInvalid(Exception):
    """The signal/position identity or requested paper notional is structurally invalid."""


class QuoteStatus(str, Enum):
    HEALTHY = "healthy"
    ROUTE_ABSENT = "route_absent"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    STALE = "stale"


@dataclass(frozen=True)
class QuoteFailure:
    status: QuoteStatus
    provider: str
    observed_at: str
    detail_code: str
    failure_class: str | None = None
    evidence_id: str = field(init=False)

    def __post_init__(self):
        if self.status is QuoteStatus.HEALTHY:
            raise ValueError("QuoteFailure status cannot be healthy")
        if not self.provider or not self.detail_code:
            raise ValueError("quote failure provider and detail_code must not be empty")
        object.__setattr__(self, "evidence_id", "quote-" + fingerprint({
            "status": self.status.value, "provider": self.provider,
            "observed_at": (self.observed_at if self.status is QuoteStatus.ROUTE_ABSENT else None),
            "detail_code": self.detail_code,
        }))


@dataclass(frozen=True)
class ModeledLiveFill:
    network_id: str
    mint: str
    side: str
    token_qty_atomic: int
    token_decimals: int
    quote_in_atomic: int
    quote_in_asset: str
    quote_in_decimals: int
    quote_out_atomic: int
    quote_out_asset: str
    quote_out_decimals: int
    unit_price: str
    observed_at: str
    quote_provider: str
    route_binding: Mapping[str, object]
    quote_payload: Mapping[str, object]
    quote_fingerprint: str
    costs: Mapping[str, object]
    net_pnl_computable: bool
    quote_age_s: float
    fill_kind: str = "modeled_live_quote"


def _failure_code(exc: BaseException) -> str:
    message = str(exc).strip().lower().replace("-", "_").replace(" ", "_")
    compact = "_".join(part for part in message.split("_") if part)
    return (compact or type(exc).__name__.lower())[:80]


def quote_exit_outcome(position, qty_atomic, *, adapter=None, observed_at=None,
                       now_fn=real_utc_now_iso, **kwargs) -> ModeledLiveFill | QuoteFailure:
    """Normalize a read-only exit quote into typed evidence without hiding provider failures."""
    quote_adapter = adapter or quote_exit
    provider = "jupiter" if position.get("network_id") == "solana" else "evm_route"
    at = observed_at or now_fn()
    try:
        outcome = quote_adapter(position, qty_atomic, **kwargs)
    except QuoteStale as exc:
        return QuoteFailure(QuoteStatus.STALE, provider, at, _failure_code(exc))
    except QuoteUnavailable as exc:
        code = _failure_code(exc)
        typed = getattr(exc, "failure", None)
        explicit_absence = typed in TOKEN_CLASSES
        status = (QuoteStatus.ROUTE_ABSENT if explicit_absence
                  else QuoteStatus.PROVIDER_UNAVAILABLE)
        return QuoteFailure(
            status, provider, at, code,
            failure_class=(typed.value if isinstance(typed, TypedQuoteFailure) else None))
    except (TimeoutError, ConnectionError, OSError) as exc:
        return QuoteFailure(
            QuoteStatus.PROVIDER_UNAVAILABLE, provider, at, _failure_code(exc))
    if isinstance(outcome, QuoteFailure):
        return outcome
    if not isinstance(outcome, ModeledLiveFill) and not hasattr(outcome, "quote_out_atomic"):
        return QuoteFailure(
            QuoteStatus.PROVIDER_UNAVAILABLE, provider, at, "malformed_provider_response")
    quote_observed_at = getattr(outcome, "observed_at", None)
    if quote_observed_at:
        try:
            cached_age_s = _age_s(str(quote_observed_at), at)
        except (TypeError, ValueError):
            return QuoteFailure(
                QuoteStatus.PROVIDER_UNAVAILABLE, provider, at, "malformed_quote_timestamp")
        if cached_age_s > MEMECOIN_V6_PAPER_QUOTE_MAX_AGE_S:
            return QuoteFailure(
                QuoteStatus.STALE, provider, at, "cached_quote_age_exceeded")
    return outcome


def _age_s(started_iso: str, observed_iso: str) -> float:
    start = datetime.fromisoformat(started_iso.replace("Z", "+00:00"))
    obs = datetime.fromisoformat(observed_iso.replace("Z", "+00:00"))
    return (obs - start).total_seconds()


def _positive_int(value, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise QuoteUnavailable(f"{label} is not an integer: {value!r}")
    if parsed <= 0:
        raise QuoteUnavailable(f"{label} must be positive: {parsed}")
    return parsed


# EVM chains whose quotes this module will serve. 8453 joined 2026-09-06: Base paper and V6 quotes
# had been dead since 08-29 with 450 `IdentityInvalid` events, because this set named 4663 alone
# while the runner had been carrying Base signals the whole time. Every chain here takes the same
# 0x-address check; the check is the chain-generic one, not a 4663 one.
# NOT "1" (Gamma M-A, 2026-09-30). Paper quotes read through evm_rpc_for_chain, which for chain 1
# is the operator's metered Infura CUSTODY key; every chain-1 J7 buy (45 wallets, 100-175/day, up to
# ~26 calls a quote) would draw on the budget funded chain-1 exits need. Chain-1 paper stays
# IdentityInvalid until a non-custody paper host is chosen and its load measured.
_EVM_QUOTE_NETWORKS = ("4663", "8453", "56")


def _validated_identity(network_id, mint):
    network = str(network_id or "").strip().lower()
    token = str(mint or "").strip()
    if network not in {"solana", *_EVM_QUOTE_NETWORKS}:
        raise IdentityInvalid(f"unsupported network identity: {network_id!r}")
    if not token:
        raise IdentityInvalid("mint identity is empty")
    if network in _EVM_QUOTE_NETWORKS:
        if len(token) != 42 or not token.lower().startswith("0x"):
            raise IdentityInvalid(f"invalid {network} mint identity: {mint!r}")
        try:
            int(token[2:], 16)
        except ValueError as exc:
            raise IdentityInvalid(f"invalid {network} mint identity: {mint!r}") from exc
        token = token.lower()
    return network, token


def _unit_price(pay_atomic: int, pay_decimals: int, token_qty_atomic: int,
                token_decimals_: int) -> str:
    pay = Decimal(pay_atomic) / (Decimal(10) ** pay_decimals)
    tokens = Decimal(token_qty_atomic) / (Decimal(10) ** token_decimals_)
    if tokens <= 0:
        raise QuoteUnavailable("token quantity is non-positive")
    return format(pay / tokens, "f")


def _normalize_fill(*, network_id, mint, side, token_qty_atomic, token_decimals_,
                    quote_in_atomic, quote_in_asset, quote_in_decimals,
                    quote_out_atomic, quote_out_asset, quote_out_decimals,
                    unit_price, quote_provider, route_binding, quote_payload, costs,
                    net_pnl_computable, started_iso, now_fn, max_age_s) -> ModeledLiveFill:
    if not isinstance(route_binding, Mapping) or not route_binding:
        raise QuoteUnavailable("route_binding must be a non-empty mapping")
    observed_at = now_fn()
    age = _age_s(started_iso, observed_at)
    if age < 0:
        raise QuoteUnavailable("quote observed before it was requested")
    if age > (MEMECOIN_V6_PAPER_QUOTE_MAX_AGE_S if max_age_s is None else max_age_s):
        raise QuoteStale(f"quote age {age:.3f}s exceeds max {max_age_s}")
    payload = dict(quote_payload)
    return ModeledLiveFill(
        network_id=network_id, mint=mint, side=side,
        token_qty_atomic=token_qty_atomic, token_decimals=token_decimals_,
        quote_in_atomic=quote_in_atomic, quote_in_asset=quote_in_asset,
        quote_in_decimals=quote_in_decimals, quote_out_atomic=quote_out_atomic,
        quote_out_asset=quote_out_asset, quote_out_decimals=quote_out_decimals,
        unit_price=unit_price, observed_at=observed_at, quote_provider=quote_provider,
        route_binding=dict(route_binding), quote_payload=payload,
        quote_fingerprint=fingerprint(payload), costs=dict(costs),
        net_pnl_computable=bool(net_pnl_computable), quote_age_s=age,
    )


def _weth_meta(chain="4663"):
    """The chain's WETH quote token and its decimals. Both funded EVM chains quote in WETH."""
    meta = CHAIN_QUOTE.get(str(chain)) or {}
    token = meta.get("token")
    decimals = meta.get("decimals")
    if not token or decimals is None:
        raise QuoteUnavailable(f"no {chain} quote-token configured")
    return token, int(decimals)


def _jupiter_route_binding(raw):
    plan = raw.get("routePlan")
    if not isinstance(plan, list) or not plan:
        raise QuoteUnavailable(
            "jupiter route binding missing", failure=TypedQuoteFailure.NO_ROUTE)
    amm_keys = []
    for leg in plan:
        swap = leg.get("swapInfo") if isinstance(leg, Mapping) else None
        amm_key = swap.get("ammKey") if isinstance(swap, Mapping) else None
        if not amm_key:
            raise QuoteUnavailable("jupiter route leg missing ammKey")
        amm_keys.append(str(amm_key))
    return {"venue": "jupiter", "amm_keys": amm_keys, "route_plan": plan}


def _exact_evm_route(route):
    if not isinstance(route, Mapping):
        return False
    protocol = route.get("protocol")
    if protocol == "uniswap_v4":
        return isinstance(route.get("pool_key"), Mapping) and bool(route["pool_key"])
    if protocol == "uniswap_v3":
        tokens, fees = route.get("tokens"), route.get("fees")
        return (isinstance(tokens, list) and len(tokens) >= 2
                and isinstance(fees, list) and len(fees) == len(tokens) - 1)
    if protocol == "uniswap_v2":
        tokens = route.get("tokens")
        return bool(route.get("router")) and isinstance(tokens, list) and len(tokens) >= 2
    return False


def _address_word(value):
    raw = str(value or "").strip().lower()
    if len(raw) != 42 or not raw.startswith("0x"):
        return None
    try:
        int(raw[2:], 16)
    except ValueError:
        return None
    return raw[2:].rjust(64, "0")


def _returned_address(raw):
    if not isinstance(raw, str) or not raw.startswith("0x"):
        return None
    body = raw[2:]
    if len(body) < 64:
        return None
    address = "0x" + body[-40:].lower()
    return None if address == "0x" + "00" * 20 else address


def _bind_evm_route_pools(route, *, market_token, call_fn, factories=None, chain=None):
    """Resolve every V2/V3 route leg against its verified factory using read-only calls."""
    if not isinstance(route, Mapping):
        return None
    bound = dict(route)
    protocol = bound.get("protocol")
    if protocol not in {"uniswap_v2", "uniswap_v3"}:
        return bound
    tokens = bound.get("tokens")
    if not isinstance(tokens, list) or len(tokens) < 2:
        return None
    token_words = [_address_word(token) for token in tokens]
    market = str(market_token or "").lower()
    normalized_tokens = [str(token).lower() for token in tokens]
    if any(word is None for word in token_words) or market not in normalized_tokens:
        return None
    if factories is None:
        factories = MEMECOIN_V6_EVM_FACTORIES_BY_CHAIN.get(
            str(chain), MEMECOIN_V6_EVM_FACTORIES_4663) if chain is not None \
            else MEMECOIN_V6_EVM_FACTORIES_4663
    config = dict(factories)
    fees = bound.get("fees")
    if protocol == "uniswap_v3" and (
            not isinstance(fees, list) or len(fees) != len(tokens) - 1):
        return None
    normalized_fees = []
    if protocol == "uniswap_v3":
        try:
            normalized_fees = [int(value) for value in fees]
        except (TypeError, ValueError):
            return None
        if any(fee <= 0 for fee in normalized_fees):
            return None
    factory_key = "v3_factory" if protocol == "uniswap_v3" else "v2_factory"
    factory = str(config.get(factory_key) or "").lower()
    if _address_word(factory) is None:
        return None
    cache_key = (
        protocol, factory, tuple(normalized_tokens),
        tuple(normalized_fees), market,
    )
    cached = _POOL_BINDING_CACHE.get(cache_key)
    if cached is not None:
        _POOL_BINDING_CACHE.move_to_end(cache_key)
        return dict(cached)
    pools = []
    for index, (left, right) in enumerate(zip(token_words, token_words[1:])):
        selector = _V3_GET_POOL if protocol == "uniswap_v3" else _V2_GET_PAIR
        data = "0x" + selector + left + right
        if protocol == "uniswap_v3":
            fee = normalized_fees[index]
            data += format(fee, "064x")
        try:
            pool = _returned_address(call_fn(factory, data))
        except Exception:
            return None
        if pool is None:
            return None
        pools.append(pool)
    market_index = normalized_tokens.index(market)
    if market_index == 0:
        market_pool = pools[0]
    elif market_index == len(normalized_tokens) - 1:
        market_pool = pools[-1]
    else:
        return None
    bound["pools"] = pools
    bound["market_pool"] = market_pool
    _POOL_BINDING_CACHE[cache_key] = dict(bound)
    _POOL_BINDING_CACHE.move_to_end(cache_key)
    while len(_POOL_BINDING_CACHE) > _POOL_BINDING_CACHE_MAX:
        _POOL_BINDING_CACHE.popitem(last=False)
    return bound


def _evm_best_amount_out(chain, sell_token, buy_token, amount_atomic, *, taker, rpc_url, http,
                         gt_http, market_token=None):
    """Best amountOut across verified pure-V4, V3, and V2 routes — quote evidence only, no calldata
    is retained. ``None`` when no route quotes."""
    best = None   # (amount_out, route)
    v4 = v4_quote_amount_out(chain, sell_token, buy_token, amount_atomic,
                             rpc_url=rpc_url, http=http, gt_http=gt_http)
    if v4 and v4.get("amount_out"):
        pool_key = v4.get("pool_key")
        if isinstance(pool_key, Mapping) and pool_key:
            best = (
                int(v4["amount_out"]),
                {
                    "protocol": "uniswap_v4",
                    "pool_fee": v4.get("pool_fee"),
                    "pool_id": v4.get("pool_id"),
                    "pool_key": dict(pool_key),
                    "zero_for_one": bool(v4.get("zero_for_one")),
                },
            )
    call_fn = _evm_call_fn(rpc_url, http=http)
    key = str(chain)
    # PER CHAIN, WITH NO CROSS-CHAIN DEFAULT. A missing entry leaves the rung with no addresses and
    # it declines; borrowing another chain's router would quote against a contract that does not
    # exist there, which reads as `no_route` only by luck.
    v3_addr = MEMECOIN_V6_EVM_V3_ADDRESSES_BY_CHAIN.get(key) or {}
    v2_router = MEMECOIN_V6_EVM_V2_ROUTER_BY_CHAIN.get(key)
    v2_connectors = MEMECOIN_V6_EVM_V2_CONNECTORS_BY_CHAIN.get(key) or ()
    route_calls = (
        (dex_v3_quote, "uniswap_v3", {
            "cfg_addr": v3_addr,
            "fee_tiers": MEMECOIN_V6_EVM_V3_FEE_TIERS, "connectors": (),
            "slippage_bps": MEMECOIN_V6_EVM_V3_SLIPPAGE_BPS, "call_fn": call_fn,
        }),
        (dex_v2_exit_quote, "uniswap_v2", {
            "cfg_router": v2_router,
            "connector_tokens": v2_connectors,
            "slippage_bps": MEMECOIN_V6_EVM_V2_SLIPPAGE_BPS,
            "tax_buffer_bps": MEMECOIN_V6_EVM_V2_TAX_BUFFER_BPS, "call_fn": call_fn,
        }),
    )
    for fn, proto, options in route_calls:
        res = fn(chain, sell_token, buy_token, amount_atomic, taker, http=http, **options)
        if not res:
            continue
        out = int(res.get("buyAmount") or 0)
        route = res.get("route") if isinstance(res.get("route"), Mapping) else {"protocol": proto}
        if proto in {"uniswap_v2", "uniswap_v3"}:
            route = _bind_evm_route_pools(
                route, market_token=market_token or buy_token, call_fn=call_fn, chain=key)
            if route is None:
                continue
        if out > 0 and (best is None or out > best[0]):
            best = (out, dict(route))
    if best is None:
        return None
    return {"amount_out": best[0], "route": best[1]}


def _evm_bound_amount_out(chain, sell_token, buy_token, amount_atomic, *, route, taker,
                          rpc_url, http):
    """Try the persisted V2/V3 route family before broad venue discovery."""
    if not _exact_evm_route(route):
        return None
    protocol = route.get("protocol")
    if protocol not in {"uniswap_v2", "uniswap_v3"}:
        return None
    call_fn = _evm_call_fn(rpc_url, http=http)
    key = str(chain)
    tokens = [str(token).lower() for token in route.get("tokens") or ()]
    sell, buy = str(sell_token).lower(), str(buy_token).lower()
    if tokens and tokens[0] == buy and tokens[-1] == sell:
        tokens.reverse()
    if not tokens or tokens[0] != sell or tokens[-1] != buy:
        return None
    connectors = tuple(tokens[1:-1])
    if protocol == "uniswap_v2":
        result = dex_v2_exit_quote(
            chain, sell_token, buy_token, amount_atomic, taker, http=http,
            cfg_router=(route.get("router")
                        or MEMECOIN_V6_EVM_V2_ROUTER_BY_CHAIN.get(key)),
            connector_tokens=connectors,
            slippage_bps=MEMECOIN_V6_EVM_V2_SLIPPAGE_BPS,
            tax_buffer_bps=MEMECOIN_V6_EVM_V2_TAX_BUFFER_BPS, call_fn=call_fn)
    else:
        fees = tuple(int(fee) for fee in route.get("fees") or ())
        result = dex_v3_quote(
            chain, sell_token, buy_token, amount_atomic, taker, http=http,
            cfg_addr=MEMECOIN_V6_EVM_V3_ADDRESSES_BY_CHAIN.get(key) or {},
            fee_tiers=tuple(dict.fromkeys(fees)),
            connectors=tuple((connector, fees[-1]) for connector in connectors),
            slippage_bps=MEMECOIN_V6_EVM_V3_SLIPPAGE_BPS, call_fn=call_fn)
    if not result or not result.get("buyAmount"):
        return None
    found = result.get("route") if isinstance(result.get("route"), Mapping) else route
    bound = _bind_evm_route_pools(found, market_token=sell_token, call_fn=call_fn, chain=key)
    return {"amount_out": int(result["buyAmount"]), "route": bound or dict(route)}


def quote_entry(signal, notional_usd, *, fill_model_version=LEGACY_FILL_MODEL,
                http=None, now_fn=real_utc_now_iso,
                jupiter_entry_fn=jupiter_entry_quote, token_decimals_fn=token_decimals,
                evm_quote_fn=None, usd_to_quote_fn=usd_to_quote_atomic,
                erc20_decimals_fn=erc20_decimals, rpc_url=None, solana_rpc_url=None,
                evm_rpc_url=None, gt_http=None,
                max_age_s=None) -> ModeledLiveFill:
    """One live executable BUY quote for ``notional_usd`` of ``signal.mint`` -> ModeledLiveFill.
    Raises QuoteUnavailable/QuoteStale; never falls back to a non-executable price."""
    network_id, mint = _validated_identity(signal.network_id, signal.mint)
    notional = Decimal(str(notional_usd))
    if not notional.is_finite() or notional <= 0:
        raise IdentityInvalid(f"paper notional must be finite and positive: {notional_usd!r}")
    started = now_fn()

    if network_id == "solana":
        # ONE PASS. The first version called the legacy quote and then, on failure, re-ran the whole
        # host sequence to recover the reason: FOUR HTTP calls per failure
        # (lite -> portal -> lite -> portal), doubling load in exactly the 429 condition the split
        # exists to measure. The detailed form yields (payload, reason, host) together, so a failure
        # costs what it always did. An injected `jupiter_entry_fn` that returns only a payload still
        # works and simply reports an unknown reason — it is never called twice.
        detailed = getattr(jupiter_entry_fn, "detailed", None)
        if jupiter_entry_fn is jupiter_entry_quote:
            detailed = jupiter_entry_quote_detailed
        if detailed is not None:
            raw, reason, host = detailed(mint, float(notional), http=http)
        else:
            raw, reason, host = jupiter_entry_fn(mint, float(notional), http=http), None, None
        if not raw:
            exc = QuoteUnavailable("solana entry quote unavailable: %s" % (reason or "unknown"))
            exc.quote_reason = reason or QUOTE_PROVIDER_ERROR
            exc.quote_host = host
            raise exc
        route_binding = _jupiter_route_binding(raw)
        in_atomic = _positive_int(raw.get("inAmount"), "inAmount")
        out_atomic, expected_out_atomic = _scored_out_atomic(
            raw, fill_model_version=fill_model_version, label="solana entry quote"
        )
        dec = token_decimals_fn(mint, solana_rpc_url or rpc_url, http=http)
        if dec is None:
            raise QuoteUnavailable("solana token decimals unresolved")
        dec = int(dec)
        return _normalize_fill(
            network_id=network_id, mint=mint, side="buy",
            token_qty_atomic=out_atomic, token_decimals_=dec,
            quote_in_atomic=in_atomic, quote_in_asset=USDC_LABEL, quote_in_decimals=USDC_DECIMALS,
            quote_out_atomic=out_atomic, quote_out_asset=mint, quote_out_decimals=dec,
            unit_price=_unit_price(in_atomic, USDC_DECIMALS, out_atomic, dec),
            quote_provider="jupiter",
            route_binding=route_binding,
            quote_payload={"in_atomic": in_atomic, "out_atomic": out_atomic,
                           "expected_out_atomic": expected_out_atomic,
                           "fill_model_version": fill_model_version,
                           "route_plan": raw.get("routePlan") or [],
                           "slippage_bps": raw.get("slippageBps"),
                           "price_impact_pct": raw.get("priceImpactPct")},
            costs={"slippage_bps": raw.get("slippageBps"),
                   "price_impact_pct": raw.get("priceImpactPct"),
                   "network_fee_quote_atomic": None},
            net_pnl_computable=False, started_iso=started, now_fn=now_fn, max_age_s=max_age_s)

    if network_id in _EVM_QUOTE_NETWORKS:
        chain = int(network_id)
        weth, weth_dec = _weth_meta(network_id)
        in_atomic = usd_to_quote_fn(chain, notional, http=http)
        if not in_atomic or int(in_atomic) <= 0:
            raise QuoteUnavailable(f"{network_id} USD->WETH conversion unavailable")
        in_atomic = int(in_atomic)
        evm_fn = evm_quote_fn or _evm_best_amount_out
        evm_rpc = evm_rpc_url or rpc_url
        ev = evm_fn(chain, weth, mint, in_atomic, taker=PAPER_QUOTE_TAKER, rpc_url=evm_rpc,
                    http=http, gt_http=gt_http, market_token=mint)
        if not ev or not ev.get("amount_out"):
            raise QuoteUnavailable(f"{network_id} entry route quote unavailable")
        out_atomic = _positive_int(ev.get("amount_out"), "amount_out")
        route = ev.get("route") if isinstance(ev.get("route"), Mapping) else {}
        if not _exact_evm_route(route):
            raise QuoteUnavailable(f"{network_id} entry route binding missing")
        dec = erc20_decimals_fn(mint, evm_rpc, http=http)
        if dec is None:
            raise QuoteUnavailable(f"{network_id} token decimals unresolved")
        dec = int(dec)
        return _normalize_fill(
            network_id=network_id, mint=mint, side="buy",
            token_qty_atomic=out_atomic, token_decimals_=dec,
            quote_in_atomic=in_atomic, quote_in_asset="WETH", quote_in_decimals=weth_dec,
            quote_out_atomic=out_atomic, quote_out_asset=mint, quote_out_decimals=dec,
            unit_price=_unit_price(in_atomic, weth_dec, out_atomic, dec),
            quote_provider="evm_route",
            route_binding=dict(route),
            quote_payload={"in_atomic": in_atomic, "out_atomic": out_atomic, "route": dict(route)},
            costs={"quote_token": "WETH", "network_fee_quote_atomic": None},
            net_pnl_computable=False, started_iso=started, now_fn=now_fn, max_age_s=max_age_s)

    raise QuoteUnavailable(f"unsupported network for quote: {network_id!r}")


def quote_exit(position, qty_atomic, *, fill_model_version=LEGACY_FILL_MODEL,
               http=None, now_fn=real_utc_now_iso,
               jupiter_exit_fn=jupiter_exit_quote, evm_quote_fn=None,
               evm_bound_quote_fn=None,
               erc20_decimals_fn=erc20_decimals, rpc_url=None, evm_rpc_url=None, gt_http=None,
               slippage_pct=None, max_age_s=None) -> ModeledLiveFill:
    """One live executable SELL quote for exactly ``qty_atomic`` bag tokens -> ModeledLiveFill.
    Raises QuoteUnavailable/QuoteStale; never falls back to a non-executable price."""
    network_id, mint = _validated_identity(position["network_id"], position["mint"])
    qty = _positive_int(qty_atomic, "qty_atomic")
    token_dec = position.get("token_decimals")
    net_computable = False
    started = now_fn()

    if network_id == "solana":
        pct = slippage_pct if slippage_pct is not None else 3.0
        raw = jupiter_exit_fn(mint, qty, pct, http=http)
        if not raw:
            raise QuoteUnavailable(
                "solana exit quote unavailable", failure=TypedQuoteFailure.NO_ROUTE)
        route_binding = _jupiter_route_binding(raw)
        out_atomic, expected_out_atomic = _scored_out_atomic(
            raw, fill_model_version=fill_model_version, label="solana exit quote"
        )
        if token_dec is None:
            raise QuoteUnavailable("solana exit token decimals unresolved")
        token_dec = int(token_dec)
        return _normalize_fill(
            network_id=network_id, mint=mint, side="sell",
            token_qty_atomic=qty, token_decimals_=token_dec,
            quote_in_atomic=qty, quote_in_asset=mint, quote_in_decimals=token_dec,
            quote_out_atomic=out_atomic, quote_out_asset=USDC_LABEL,
            quote_out_decimals=USDC_DECIMALS,
            unit_price=_unit_price(out_atomic, USDC_DECIMALS, qty, token_dec),
            quote_provider="jupiter",
            route_binding=route_binding,
            quote_payload={"in_atomic": qty, "out_atomic": out_atomic,
                           "expected_out_atomic": expected_out_atomic,
                           "fill_model_version": fill_model_version,
                           "route_plan": raw.get("routePlan") or [],
                           "slippage_bps": raw.get("slippageBps")},
            costs={"slippage_bps": raw.get("slippageBps"),
                   "price_impact_pct": raw.get("priceImpactPct"),
                   "network_fee_quote_atomic": None},
            net_pnl_computable=net_computable, started_iso=started, now_fn=now_fn,
            max_age_s=max_age_s)

    if network_id in _EVM_QUOTE_NETWORKS:
        chain = int(network_id)
        weth, weth_dec = _weth_meta(network_id)
        evm_fn = evm_quote_fn or _evm_best_amount_out
        bound_fn = evm_bound_quote_fn or _evm_bound_amount_out
        evm_rpc = evm_rpc_url or rpc_url
        persisted_route = position.get("route_binding")
        ev = bound_fn(
            chain, mint, weth, qty, route=persisted_route, taker=PAPER_QUOTE_TAKER,
            rpc_url=evm_rpc, http=http) if isinstance(persisted_route, Mapping) else None
        if not ev:
            ev = evm_fn(
                chain, mint, weth, qty, taker=PAPER_QUOTE_TAKER, rpc_url=evm_rpc, http=http,
                gt_http=gt_http, market_token=mint)
        if not ev or not ev.get("amount_out"):
            failure = _evm_provider_failure(evm_rpc, http=http) or TypedQuoteFailure.NO_ROUTE
            raise QuoteUnavailable(
                f"{network_id} exit route quote unavailable", failure=failure)
        out_atomic = _positive_int(ev.get("amount_out"), "amount_out")
        route = ev.get("route") if isinstance(ev.get("route"), Mapping) else {}
        if not _exact_evm_route(route):
            raise QuoteUnavailable(
                f"{network_id} exit route binding missing", failure=TypedQuoteFailure.NO_ROUTE)
        if token_dec is None:
            dec = erc20_decimals_fn(mint, evm_rpc, http=http)
            if dec is None:
                raise QuoteUnavailable(f"{network_id} exit token decimals unresolved")
            token_dec = int(dec)
        token_dec = int(token_dec)
        return _normalize_fill(
            network_id=network_id, mint=mint, side="sell",
            token_qty_atomic=qty, token_decimals_=token_dec,
            quote_in_atomic=qty, quote_in_asset=mint, quote_in_decimals=token_dec,
            quote_out_atomic=out_atomic, quote_out_asset="WETH", quote_out_decimals=weth_dec,
            unit_price=_unit_price(out_atomic, weth_dec, qty, token_dec),
            quote_provider="evm_route",
            route_binding=dict(route),
            quote_payload={"in_atomic": qty, "out_atomic": out_atomic, "route": dict(route)},
            costs={"quote_token": "WETH", "network_fee_quote_atomic": None},
            net_pnl_computable=net_computable, started_iso=started, now_fn=now_fn,
            max_age_s=max_age_s)

    raise QuoteUnavailable(f"unsupported network for quote: {network_id!r}")
