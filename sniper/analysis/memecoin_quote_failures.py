"""WO-4 — typed quote-failure classification for the exact-exit quote.

A failed exact-exit quote is either TOKEN-specific (the bag genuinely can't sell — a real
unsellable/rug signal that MUST advance `exact_quote_misses` toward the unsellable CUT) or INFRA
(a provider outage — Jupiter/0x/RPC down — that must NOT condemn the token). Conflating them
produced false unsellable-CUTs during outages (the live 4663 false-CUT class: exact quote fails on
every sweep while the chart still prices the coin).

Two guards, in order of strength:
  1. Per-failure class: only the four TOKEN classes advance misses; the four INFRA classes advance a
     separate ops streak and alert, never the miss counter.
  2. Provider-scoped defense in depth (works even when the seam can't type the failure): if every
     exact-relevant, chart-priceable position in one provider group fails in the same sweep (>=2),
     that group is an outage regardless of per-failure class — advance nothing.
"""
import enum


class QuoteFailure(enum.Enum):
    # token-specific — the bag itself will not sell
    NO_ROUTE = "no_route"
    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"
    TOKEN_REJECTED = "token_rejected"
    SIMULATION_REVERT = "simulation_revert"
    # Raydium: no route, while Jupiter said nothing about the route (no answer, 5xx, cooldown) and was
    # not refusing OUR access (2026-10-17, ANIMA 73930: 6.5 min of this typed malformed = infra, the
    # unsellable counters never moved while the display ran +70%). Token class: the sweep's group
    # outage defense still makes it OPS when 2+ positions have zero successes.
    ROUTE_NOT_INDEXED = "route_not_indexed"
    # infrastructure — the provider/transport failed, the token is not implicated
    RATE_LIMIT = "rate_limit"
    RPC_TRANSPORT = "rpc_transport"
    PROVIDER_TIMEOUT = "provider_timeout"
    MALFORMED_RESPONSE = "malformed_response"


TOKEN_CLASSES = frozenset({
    QuoteFailure.NO_ROUTE, QuoteFailure.INSUFFICIENT_LIQUIDITY,
    QuoteFailure.TOKEN_REJECTED, QuoteFailure.SIMULATION_REVERT, QuoteFailure.ROUTE_NOT_INDEXED})
INFRA_CLASSES = frozenset({
    QuoteFailure.RATE_LIMIT, QuoteFailure.RPC_TRANSPORT,
    QuoteFailure.PROVIDER_TIMEOUT, QuoteFailure.MALFORMED_RESPONSE})

# verdicts
RESET = "reset"        # exact quote succeeded -> zero the miss streak
ADVANCE = "advance"    # token-specific (or untyped) failure -> bump exact_quote_misses toward CUT
OPS = "ops"            # infra failure or sweep-wide outage -> bump ops streak only, never misses


def is_infra(failure):
    return failure in INFRA_CLASSES


def sweep_miss_verdicts(outcomes):
    """Decide, for one exit sweep, which positions advance the unsellable-miss counter.

    `outcomes`: `(position_id, failure[, provider_group])` for exact-relevant, chart-priceable
    positions. `failure is None` means success. A failure that is not a `QuoteFailure` is treated as
    token-specific — fail-safe toward CUT — except under the provider-scoped outage defense.

    Returns {position_id: RESET | ADVANCE | OPS}. Outage defense: within one provider group, >=2
    exact-relevant positions and zero successes makes every failure OPS regardless of class.
    """
    if not outcomes:
        return {}
    normalized = [
        (item[0], item[1], item[2] if len(item) >= 3 else "all")
        for item in outcomes
    ]
    groups = {}
    for pid, failure, group in normalized:
        groups.setdefault(group, []).append((pid, failure))
    verdicts = {}
    for members in groups.values():
        successes = sum(1 for _pid, failure in members if failure is None)
        outage = len(members) >= 2 and successes == 0
        for pid, failure in members:
            if failure is None:
                verdicts[pid] = RESET
            elif outage or is_infra(failure):
                verdicts[pid] = OPS
            else:
                verdicts[pid] = ADVANCE
    return verdicts
