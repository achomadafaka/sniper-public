"""OKX proVF4 -> pump_amm pre-execution decoder: one RabbitBuyer handler (Alpha GO 2026-10-17).

WHAT IT READS. 49 armed Fomo buys in the 7 days to 2026-10-17 went through OKX's DEX router: all
swap_tob, one OKX instruction, USDC-paid, Fomo's executor signer 0 and the owner signer 1. OKX swaps
USDC -> SOL on hop 1 (Kipseli, Aquifer, Tessera, BisonFi or a CLMM/DLMM) and buys the coin on hop 2,
on its canonical PumpSwap pool alone or split with DLMM/CLMM legs of the same coin. Everything after
match is DflowAmmHandler's: the one read, identity, the read_stale bound and pre-ness, the fee tier,
the exact-quote-in model, the impact band and the synthesized BuyEvent.

WHAT DIFFERS FROM DFLOW (match, memory only):
- THE IN-AMOUNT. OKX's PumpSwap leg is buy_exact_quote_in with min_base_out 1, and its spend is set AT
  EXECUTION from hop 1's SOL output; no argument carries it. It is bound as
      sol_in = amount_in (USDC atomic) x 1e3 / sol_usd x pump_weight / 1e4
  with sol_usd from the lane prefetcher's snapshot (no call), refused as sol_usd_stale beyond the
  lane's own 30 s age. Against the executed spend: -7..+7 bps where our price was at most 120 s old
  (n=13). No shading: a 10 bps miss moves the modelled post price by about 1 bps.
  expect_amount_out is not used (executed out / expected: 0.28..1.07).
- THE ROUTE (okx_swap.pump_weight): exactly two hops, every hop-1 route at index 1 (it delivers
  SOL; index 2 swaps USDC straight into the coin, which overstated the SOL 11-23%), and exactly one
  PumpSwap leg, in hop 2. Anything else: okx_route_unmodelled.
- NAMED ACCOUNTS, not a search: [0] payer == owner, [1] the owner's USDC account, [2] the owner's
  coin account (Token or Token-2022), [3] USDC, [4] the coin; all writable where they must be. The
  coin's canonical (mint, WSOL) PumpSwap pool named by the swap and writable, else pool_noncanonical
  (8 of 49 bought pools quoted in xStocks).
- THE JOIN additionally pages when our in-amount estimate misses the executed spend by more than
  SPEND_DRIFT_PAGE_BPS: the guard on the route reading above. The executed spend is the exact-quote
  event's gross (quote_amount_in, proven against its fee parts); any other event kind pages.
Not handled: SOL-paid OKX routes (0 of 49), hop-1 index-2 routes, three-hop routes, other OKX
instructions (swap, swap_tob_v3, proxy_swap...): refused by name.
"""
from __future__ import annotations

import collections
import math
import time

from sniper.execution import okx_swap
from sniper.services.memecoin_rabbit_dflow_amm import (USDC_MINT, DflowAmmHandler, _ata, canonical_pool,
                                                     fee_config_address)

ROUTE = "okx_pump_amm"
SPEND_DRIFT_PAGE_BPS = 50


def sol_usd_reader(snapshot_fn, clock=time):
    """The handler's sol_usd_fn over the lane prefetcher: () -> (sol_usd, age in seconds or None)."""
    def read():
        snap = snapshot_fn()
        seen = (getattr(snap, "observed_mono", None) or {}).get("sol_usd")
        return getattr(snap, "sol_usd", None), (clock.monotonic() - seen if seen is not None else None)
    return read


class OkxAmmHandler(DflowAmmHandler):
    """The RabbitBuyer handler for OKX swap_tob -> PumpSwap routes. `sol_usd_fn()` returns the lane's
    cached (sol_usd, age_s); see sol_usd_reader."""

    route = ROUTE
    router_program = okx_swap.OKX_PROGRAM
    page_title = "RABBIT OKX AMM MODEL"
    knob = "MEMECOIN_SLOT_LANE_RABBIT_OKX_AMM"
    prefix = "okx"                     # refusal names and the buy's rabbit fields

    def __init__(self, *, read_fn, sol_usd_fn, alert_fn=None):
        super().__init__(read_fn=read_fn, alert_fn=alert_fn)
        self.sol_usd_fn = sol_usd_fn
        self.stats["spend_drift_bps"] = collections.deque(maxlen=256)

    def match(self, candidate, update=None):
        from sniper.execution.pump_curve_buy import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, WSOL_MINT
        from sniper.services.memecoin_slot_lane import MAX_AGE_S
        from sniper.settings import memecoin as cfg
        if not getattr(cfg, self.knob, False):
            return None
        swaps = [ix for ix in candidate.get("instructions") or () if ix[0] == self.router_program]
        if not swaps:
            return None
        if len(swaps) != 1:
            return self._refuse(self.prefix + "_multi_swap")
        _program, accounts, data = swaps[0]
        routed = self._route_amount(bytes(data))
        if routed is None:
            return None                                        # not a PumpSwap route: not this handler's
        share, why = routed
        if share is None:
            return self._refuse(why)
        numerator, denominator, extra = share
        owner = candidate.get("owner")
        if owner not in tuple(candidate.get("signers") or ()):
            return self._refuse("owner_not_signer")
        keys, writable = list(candidate.get("account_keys") or ()), candidate.get("writable")
        if writable is None or len(writable) != len(keys):
            return self._refuse("writable_unknown")
        is_writable = {key: bool(flag) for key, flag in zip(keys, writable)}
        accounts = tuple(accounts)
        if len(accounts) < 5:
            return self._refuse(self.prefix + "_accounts_short")
        payer, source, destination, source_mint, mint = accounts[:5]
        if payer != owner:
            return self._refuse(self.prefix + "_payer_not_owner")
        if (source_mint != USDC_MINT or source != _ata(owner, USDC_MINT, TOKEN_PROGRAM)
                or not is_writable.get(source)):
            return self._refuse(self.prefix + "_source_not_usdc")
        holders = {program: _ata(owner, mint, program) for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)}
        named = tuple(p for p, a in holders.items() if a == destination and is_writable.get(a))
        if len(named) != 1:
            return self._refuse("holder_unbound")
        try:
            pool = canonical_pool(mint)
        except Exception:
            return self._refuse("pool_noncanonical")
        if pool not in accounts or not is_writable.get(pool):
            return self._refuse("pool_noncanonical")
        try:
            sol_usd, age = self.sol_usd_fn()
            sol_usd = float(sol_usd)
        except Exception:
            return self._refuse("sol_usd_stale")
        if not (math.isfinite(sol_usd) and sol_usd > 0 and type(age) in (int, float)
                and 0 <= age <= MAX_AGE_S["sol_usd"]):
            return self._refuse("sol_usd_stale")
        sol_in = int(numerator * 1000 / (denominator * sol_usd))      # USDC 6 decimals -> lamports 9
        if sol_in <= 0:
            return self._refuse(self.prefix + "_amount_zero")
        plan = dict(owner=owner, mint=mint, pool=pool, sol_in=sol_in, holders=holders,
                    frame_slot=candidate.get("slot"), signature=candidate.get("signature"),
                    fee_config=fee_config_address(), quote_vault=_ata(pool, WSOL_MINT, TOKEN_PROGRAM),
                    base_vaults={p: _ata(pool, mint, p) for p in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)},
                    writable_holders=named, route_extra=tuple(extra),
                    sol_usd=sol_usd, sol_usd_age_s=round(float(age), 3), **extra)
        with self._lock:
            self.stats["matched"] += 1
        return plan, None

    def _route_amount(self, data):
        """((numerator, denominator, plan extras), None): the USDC atomic that reaches PumpSwap as SOL is
        numerator / denominator; (None, reason) refuses; None: not a PumpSwap route, not this handler's."""
        if data[:8] != okx_swap.SWAP_TOB:
            return None, "okx_not_swap_tob"
        decoded = okx_swap.decode_swap_tob(data)
        if decoded is None:
            return None, "okx_decode"
        if not any(dex == okx_swap.PUMP_AMM_DEX for dex, _w, _i in decoded["routes"]):
            return None
        weight = okx_swap.pump_weight(decoded)
        if weight is None:
            return None, "okx_route_unmodelled"
        return (decoded["amount_in"] * weight, okx_swap.WEIGHT_TOTAL,
                dict(amount_in=decoded["amount_in"], pump_weight=weight)), None

    def build(self, candidate, plan, state, fill, **kw):
        buy = super().build(candidate, plan, state, fill, **kw)
        buy.rabbit.update({"%s_%s" % (self.prefix, key): plan[key] for key in plan["route_extra"]},
                          sol_usd=plan["sol_usd"], sol_usd_age_s=plan["sol_usd_age_s"])
        return buy

    def _executed_spend(self, ev):
        """OKX's leg is buy_exact_quote_in: the event's quote_amount_in is the gross SOL spent, proven
        against its fee parts. None for any other event kind."""
        from sniper.execution.pumpswap_buy import buy_event_gross_quote
        return buy_event_gross_quote(dict(ev, ix_name="buy_exact_quote_in"))

    def _join_reasons(self, fill, spend):
        if spend is None:
            return ["the executed event is not an exact-quote-in buy"]
        drift = (fill["sol_in"] - spend) * 10_000 / spend
        with self._lock:
            self.stats["spend_drift_bps"].append(round(drift, 1))
        if abs(drift) > SPEND_DRIFT_PAGE_BPS:
            return ["in-amount estimate %d vs executed %d (%+.1f bps)" % (fill["sol_in"], spend, drift)]
        return []
