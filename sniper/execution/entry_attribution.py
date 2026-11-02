"""Pure quote provenance and buy-drift helpers for funded memecoin entries."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from fractions import Fraction


_INTERNAL_QUOTE_PREFIX = "_sniper_"


def _positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


positive_price = _positive


def market_movement_check(*, source_reserves=None, current_reserves=None, cap_pct):
    """Compare fee-free marginal Q/B on already identity/ancestry-bound SAME-pool snapshots.

    Callers prove pool, mint, quote and snapshot order. Raw atomic units cancel; neither ticket
    size, fees nor output minimum belongs in this ratio. Unknown proceeds by operator policy.
    """
    fields = {"basis": "intervening_market", "cap_pct": cap_pct, "movement_pct": None}
    try:
        cap = Fraction(str(cap_pct))
        if cap <= 0:
            return {**fields, "state": "pass", "reason": "cap_off"}
        for reserves in (source_reserves, current_reserves):
            if (not isinstance(reserves, (tuple, list)) or len(reserves) != 2
                    or any(type(value) is not int or value <= 0 for value in reserves)):
                raise ValueError("unavailable reserves")
        sq, sb = source_reserves
        cq, cb = current_reserves
        movement = (Fraction(cq * sb, cb * sq) - 1) * 100
        return {**fields, "state": "refuse" if movement > cap else "pass",
                "reason": "market_moved" if movement > cap else "within_market_cap",
                "movement_pct": float(movement)}
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return {**fields, "state": "unknown", "reason": "market_price_unavailable"}


def balance_parts(value):
    """Accept legacy float seams and entry-only ``(balance, decimals)`` seams."""
    if isinstance(value, tuple) and len(value) == 2:
        return float(value[0]), value[1]
    return float(value), None


def trace_mark(trace, stage):
    if trace is None:
        return None
    try:
        trace.mark(stage)
        return trace.stage_completed_at(stage)
    except Exception:
        return None


def trace_mark_submission(trace, stage, attempt_no, **metadata):
    """Mark a retry-aware stage, falling back to legacy one-shot trace doubles."""
    if trace is None:
        return None
    try:
        marker = getattr(trace, "mark_submission", None)
        if marker is not None:
            marker(stage, attempt_no, **metadata)
        else:
            trace.mark(stage)
        return trace.stage_completed_at(stage)
    except Exception:
        return None


def trace_now(trace):
    if trace is None:
        return None
    try:
        return trace.reconciled_now()
    except Exception:
        return None


def quote_age_ms(trace, status, *, quote_observed_at=None, confirmation_observed_at=None):
    """Confirmation-minus-quote age from the trace's single local clock domain."""
    if trace is None or status not in ("confirmed", "failed"):
        return None
    try:
        start = datetime.fromisoformat(
            quote_observed_at or trace.stage_completed_at("quote")
        )
        end = datetime.fromisoformat(
            confirmation_observed_at or trace.stage_completed_at("chain_confirmation")
        )
        return max(0.0, (end - start).total_seconds() * 1000.0)
    except (TypeError, ValueError):
        return None


def buy_drift(source_price, quote_price, fill_price):
    """Buy drift in percent; positive means the copied entry paid more."""
    source = _positive(source_price)
    quote = _positive(quote_price)
    fill = _positive(fill_price)
    return {
        "source_drift_pct": ((quote / source - 1.0) * 100.0
                             if source is not None and quote is not None else None),
        "execution_slippage_pct": ((fill / quote - 1.0) * 100.0
                                   if quote is not None and fill is not None else None),
        "total_copy_drift_pct": ((fill / source - 1.0) * 100.0
                                 if source is not None and fill is not None else None),
    }


def slot_model_benchmark(price_limit, *, spent_usd, acquired_tokens):
    """Compare a landed fill with the frozen source-reserve ticket model, never a live quote."""
    if not isinstance(price_limit, dict):
        return None
    try:
        cost, expected, minimum = (price_limit[key] for key in (
            "cost_usdc_atomic", "expected_tokens_atomic", "min_tokens_atomic"))
        decimals = price_limit["base_decimals"]
        if (any(type(value) is not int or value <= 0 for value in (cost, expected, minimum))
                or minimum > expected or type(decimals) is not int or not 0 <= decimals <= 255
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in (spent_usd, acquired_tokens))):
            return None
        expected_tokens, minimum_tokens = expected / 10 ** decimals, minimum / 10 ** decimals
        model_price = (cost / 1e6) / expected_tokens
        fill_price = spent_usd / acquired_tokens
        metrics = {
            "expected_tokens": expected_tokens,
            "model_price_usd": model_price,
            "fill_price_usd": fill_price,
            "fill_vs_model_pct": (fill_price / model_price - 1) * 100,
            "output_shortfall_pct": (1 - acquired_tokens / expected_tokens) * 100,
            "min_output_margin_pct": (acquired_tokens / minimum_tokens - 1) * 100,
        }
        if not all(math.isfinite(value) for value in metrics.values()):
            return None
        return {"basis": "source_reserve_model", "expected_tokens_atomic": expected,
                "min_tokens_atomic": minimum, **metrics}
    except (KeyError, TypeError, ValueError, OverflowError, ArithmeticError):
        return None


def curve_receipt_event(receipt, *, owner, mint):
    """Preserve one matching Pump buy event from an already fetched successful receipt."""
    from sniper.execution.pump_curve_buy import PUMP_PROGRAM, parse_trade_event
    from sniper.services.memecoin_opportunity_live import program_data_logs
    try:
        meta, slot = receipt["meta"], receipt["slot"]
        signatures = receipt["transaction"]["signatures"]
        if (meta["err"] is not None or type(slot) is not int or slot <= 0
                or not isinstance(signatures, list) or not signatures
                or not isinstance(signatures[0], str) or not signatures[0]):
            return None
        matching = []
        for _, line in program_data_logs(meta["logMessages"], PUMP_PROGRAM):
            try:
                event = parse_trade_event(line)
            except ValueError:
                continue
            if event["is_buy"] and event["user"] == owner and event["mint"] == mint:
                matching.append(line)
        if len(matching) != 1:
            return None
        return {"basis": "confirmed_receipt_trade_event", "event_log": matching[0],
                "slot": slot, "tx_sig": signatures[0]}
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return None


def public_quote(quote):
    """Remove local instrumentation before a vendor receives the quote object."""
    return {key: value for key, value in (quote or {}).items()
            if not str(key).startswith(_INTERNAL_QUOTE_PREFIX)}


def quote_fields(quote):
    quote = quote or {}
    return {
        "quote_price": _positive(quote.get("_sniper_quote_price")),
        "quote_observed_at": quote.get("_sniper_quote_observed_at"),
        "quote_id": quote.get("_sniper_quote_id"),
        "quote_route_target": quote.get("_sniper_route_target"),
    }


def _route_target(quote, network_id):
    """Where the swap actually routes. Solana: the ammKey legs, else the VENUE that answered.

    The bare `"jupiter"` literal used to be the only fallback, and it was actively wrong. A quote
    from the Raydium venue fallback (`_quote_with_fallback`, taken when Jupiter's own
    priceImpactPct > 5% -- the PEECOIN fix, 2026-08-14) is normalized by `_ray_quote` into the
    Jupiter shape the executor reads, and that normalized dict carries NO routePlan at all. So the
    loop below found no ammKey legs and the column recorded `jupiter` for the one case where the
    route was definitively NOT Jupiter. `_venue` was sitting in the object the whole time and is
    already read at three other call sites.

    TEMPORAL BOUNDARY -- this changes what the column MEANS for new rows. Rows written between
    2026-08-14T19:38 (the Raydium fallback shipping) and this commit store `jupiter` where the
    venue was raydium: 21 in memecoin_entry_attempts, 11 in memecoin_positions. They are NOT
    backfilled, because a stored value is a record of what we believed at the time. Any query
    spanning that date must treat `jupiter` as ambiguous and `raydium` as reliable-forward-only.

    This column remains the ROUTER/SETTLER, never pool identity -- `pool_meta` is the canonical
    position->pool resolver (memecoin_exits.py:996, exit_materializer.py:62,
    hot_supervisor.py:757). Naming the venue correctly does not make it a pool key."""
    if str(network_id) == "solana":
        route = []
        for leg in quote.get("routePlan") or ():
            key = ((leg or {}).get("swapInfo") or {}).get("ammKey")
            if key:
                route.append(str(key))
        if route:
            return ",".join(route)
        venue = quote.get("_venue")
        return str(venue) if venue else "jupiter"
    return str(((quote.get("transaction") or {}).get("to") or "0x")).lower()


def _atomic_amounts(quote, network_id):
    if str(network_id) == "solana":
        return quote.get("inAmount"), quote.get("outAmount")
    return quote.get("sellAmount"), quote.get("buyAmount")


def bind_quote_metadata(quote, *, network_id, mint, quote_observed_at,
                        sell_decimals, buy_decimals=None, sell_token_usd=1.0):
    """Bind deterministic, non-secret metadata to the executable quote response."""
    if not isinstance(quote, dict):
        return quote
    sell_atomic, buy_atomic = _atomic_amounts(quote, network_id)
    route = _route_target(quote, network_id)
    try:
        sell_canonical = str(int(sell_atomic))
        buy_canonical = str(int(buy_atomic))
    except (TypeError, ValueError):
        sell_canonical = buy_canonical = None
    quote_id = None
    if sell_canonical is not None and buy_canonical is not None and quote_observed_at:
        payload = {
            "v": 1,
            "network_id": str(network_id),
            "mint": (str(mint).lower() if str(mint).startswith("0x") else str(mint)),
            "sell_amount_atomic": sell_canonical,
            "buy_amount_atomic": buy_canonical,
            "route_target": route.lower() if str(network_id) != "solana" else route,
            "quote_observed_at": str(quote_observed_at),
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")
        quote_id = "q1:" + hashlib.sha256(encoded).hexdigest()
    bound = dict(quote)
    bound.update({
        "_sniper_quote_observed_at": quote_observed_at,
        "_sniper_quote_id": quote_id,
        "_sniper_route_target": route,
        "_sniper_sell_decimals": sell_decimals,
        "_sniper_buy_decimals": buy_decimals,
        "_sniper_sell_token_usd": sell_token_usd,
    })
    bound["_sniper_quote_price"] = quote_unit_price(bound, buy_decimals=buy_decimals)
    return bound


def quote_unit_price(quote, *, buy_decimals=None):
    """USD per bought token from the exact atomic amounts in a bound quote."""
    if not isinstance(quote, dict):
        return None
    network_id = "solana" if "outAmount" in quote and "sellAmount" not in quote else "evm"
    sell_atomic, buy_atomic = _atomic_amounts(quote, network_id)
    try:
        sell_decimals = int(quote.get("_sniper_sell_decimals"))
        decimals = int(buy_decimals if buy_decimals is not None
                       else quote.get("_sniper_buy_decimals"))
        sell_units = int(sell_atomic) / (10 ** sell_decimals)
        buy_units = int(buy_atomic) / (10 ** decimals)
        sell_token_usd = float(quote.get("_sniper_sell_token_usd", 1.0))
        value = sell_units * sell_token_usd / buy_units
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def copy_cap_min_out(pay_atomic, pay_usd, source_price, decimals, cap_pct, *, pay_decimals=18):
    """The smallest coin output (atomic) a native buy paying `pay_atomic` may sign so its worst
    price stays within `cap_pct` over the source's wallet-net average (USD per whole coin), rounded
    up. 0 when the cap is off; None when a price or the decimals are unusable (refuse). Pure."""
    if cap_pct <= 0:
        return 0
    try:
        src, usd = Fraction(str(float(source_price))), Fraction(str(float(pay_usd)))
        decimals = int(decimals)
        if src <= 0 or usd <= 0 or decimals < 0 or int(pay_atomic) <= 0:
            return None
        return math.ceil(Fraction(int(pay_atomic), 10 ** pay_decimals) * usd * 10 ** decimals
                         / (src * (1 + Fraction(str(cap_pct)) / 100)))
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None


def copy_minimum_refusal(quote, source_price, cap_pct):
    """EVM guaranteed output price, using bound atomic units. No provider reads."""
    source = positive_price(source_price)
    if cap_pct <= 0 or source is None:
        return None
    if (quote.get('route') or {}).get('protocol') == 'relay_same_chain':
        from sniper.execution.evm_relay_quote import relay_output_minimum
        if relay_output_minimum(quote) is None:
            return 'copy price signed minimum unproved for Relay route'
    try:
        minimum, expected = int(quote['minBuyAmount']), int(quote['buyAmount'])
        worst = quote_unit_price({**quote, 'buyAmount': str(minimum)})
        if minimum <= 0 or minimum > expected or worst is None:
            return 'copy price minimum unavailable'
    except (KeyError, TypeError, ValueError, OverflowError):
        return 'copy price minimum unavailable'
    if worst > source * (1 + cap_pct / 100):
        return 'copy price minimum is %+.1f%% above source fill (cap %.1f%%)' % (
            (worst / source - 1) * 100, cap_pct)
    return None
