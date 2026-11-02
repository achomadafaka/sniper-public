"""MEASUREMENT ONLY: what a PancakeSwap V3 -> V2 mixed hop would have quoted on chain 56.

WHY. 13 chain-56 `no_route` signals in an 8.8-day window to 2026-09-22: 8 of them are Pancake V2
pairs quoted in a NON-CONNECTOR base token (BEM, MCDon, BNCB, oANTHROPIC). Their pools were fine,
$9.9k-$22.4k with a $100 round trip at 1.4-2.5%. A single-hop V4 rung fixes 0 of 13; a V4 -> V2
mixed hop fixes 1; a PCS V3 -> V2 mixed hop fixes 3 within 10%.

WHAT IS ALREADY THERE, so this builds only the missing leg. `pair_quote_tokens` already discovers
the coin's own pair token, and `MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS` already defaults to "56", so
the V2-ONLY path through that token is live today. These signals refused anyway, which says the
base token has no V2 route back to a connector. The missing leg is therefore the BRIDGE, and it is
a V3 one: quote token -> base token on V3, then base token -> coin on V2.

THIS MODULE NEVER ADMITS AN ENTRY, and that is a safety property, not a scope note. A quote we
cannot execute atomically is an entry without its exit: the two legs would be two transactions, and
a fill on the first with a failure on the second leaves an unhedged position in a token the ladder
refused in the first place. Atomic execution through the verified SmartRouter is a separate change.
Until it lands, this only counts what would have been addressable and what it would have cost.
"""
from __future__ import annotations

REASON_DISABLED = "probe_disabled"
REASON_CHAIN = "chain_not_probed"
REASON_NO_PAIR_TOKEN = "no_pair_token_discovered"
REASON_NO_V3_BRIDGE = "no_v3_bridge_to_pair_token"
REASON_NO_V2_LEG = "no_v2_leg_to_coin"
REASON_ERROR = "probe_error"


def _enabled(cfg, chain):
    if not bool(getattr(cfg, "MEMECOIN_EVM_MIXED_HOP_PROBE_ENABLED", False)):
        return REASON_DISABLED
    if str(chain) not in getattr(cfg, "MEMECOIN_EVM_MIXED_HOP_PROBE_CHAINS", frozenset()):
        return REASON_CHAIN
    return None


def probe_v3_v2(chain, quote_token, coin, amount_atomic, taker, *, http=None,
                slippage_bps=None, cfg=None, v3_quote_fn=None, v2_quote_fn=None,
                pair_tokens_fn=None):
    """What the V3 -> V2 path would have quoted, or the reason it could not. Never raises.

    Returns a record for the rung ledger. `hit` is ALWAYS False: this is a measurement and the
    ladder must never be able to select it.
    """
    record = {"rung": "mixed_v3_v2_probe", "hit": False, "skipped": True,
              "measurement_only": True, "reason": None}
    if cfg is None:
        from sniper.settings import memecoin as cfg
    off = _enabled(cfg, chain)
    if off is not None:
        record["reason"] = off
        return record
    try:
        if pair_tokens_fn is None:
            from sniper.execution.evm_v2_exit_quote import pair_quote_tokens as pair_tokens_fn
        if v3_quote_fn is None:
            from sniper.execution.evm_dex_quote import dex_v3_quote as v3_quote_fn
        if v2_quote_fn is None:
            from sniper.execution.evm_v2_exit_quote import dex_v2_exit_quote as v2_quote_fn

        quote_token = str(quote_token).lower()
        coin = str(coin).lower()
        connectors = {str(c).lower()
                      for c in (cfg.memecoin_evm_v2_exit_config(chain) or {}).get("connectors")
                      or ()}
        bases = [str(t).lower() for t in (pair_tokens_fn(chain, coin) or ())]
        # A base token that IS a connector needs no bridge: the existing ladder already covers it,
        # and counting it here would inflate the addressable set with signals we never refused.
        bases = [b for b in bases if b not in connectors and b not in (quote_token, coin)]
        if not bases:
            record["reason"] = REASON_NO_PAIR_TOKEN
            return record

        best = None
        for base in bases:
            leg1 = v3_quote_fn(chain, quote_token, base, int(amount_atomic), taker,
                               http=http, slippage_bps=slippage_bps)
            bridged = _amount_out(leg1)
            if not bridged:
                continue
            # tax_buffer_bps=0: leg 2 SELLS the base token, and the buffer exists for a coin's own
            # transfer tax on the way out, not for the bridge asset on the way in.
            leg2 = v2_quote_fn(chain, base, coin, int(bridged), taker,
                               http=http, slippage_bps=slippage_bps, tax_buffer_bps=0)
            out = _amount_out(leg2)
            if not out:
                continue
            if best is None or out > best["coin_out_atomic"]:
                best = {"base_token": base, "bridged_atomic": int(bridged),
                        "coin_out_atomic": int(out)}
        if best is None:
            # Which leg failed is the whole point of the measurement: a missing bridge and a
            # missing coin pool ask for completely different builds.
            record["reason"] = (REASON_NO_V3_BRIDGE if not _any_bridge(
                chain, quote_token, bases, amount_atomic, taker, http, slippage_bps, v3_quote_fn)
                else REASON_NO_V2_LEG)
            record["pair_tokens"] = bases
            return record
        record.update(best)
        record["pair_tokens"] = bases
        record["reason"] = "would_quote"
        return record
    except Exception as exc:                      # a measurement must never cost a decision
        record["reason"] = REASON_ERROR
        record["detail"] = " ".join(str(exc).split())[:120]
        return record


def _any_bridge(chain, quote_token, bases, amount_atomic, taker, http, slippage_bps, v3_quote_fn):
    for base in bases:
        try:
            if _amount_out(v3_quote_fn(chain, quote_token, base, int(amount_atomic), taker,
                                       http=http, slippage_bps=slippage_bps)):
                return True
        except Exception:
            continue
    return False


def _amount_out(quote):
    """The buy amount a rung reports, or 0. Rungs answer as a dict, a 2-tuple or a 3-tuple."""
    if isinstance(quote, tuple):
        quote = quote[0] if quote and isinstance(quote[0], dict) else None
    if not isinstance(quote, dict):
        return 0
    for key in ("buyAmount", "amount_out", "toAmount"):
        try:
            value = int(quote.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0
