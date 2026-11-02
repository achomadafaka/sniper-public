"""Jupiter route_v2 -> pump_amm pre-execution decoder: one RabbitBuyer handler (Alpha GO 2026-10-17).

43 armed Fomo buys in the 7 days to 2026-10-17 went through Jupiter v6: all route_v2, USDC-paid,
the same named first five accounts as OKX ([0] owner, [1] its USDC account, [2] its coin account,
[3] USDC, [4] the coin). Everything but the route decode is OkxAmmHandler's (and, after match,
DflowAmmHandler's): the lane's cached sol_usd, the canonical pool, the read, read_stale, pre-ness,
the fee tier, the exact-quote-in model and the join pages.

THE IN-AMOUNT. route_v2's plan names token indexes, so no weight heuristic is needed:
    sol_in = in_amount x (1e4 - platform_fee_bps) / 1e4 x share / 1e8 x 1e3 / sol_usd
with share from jupiter_route.pump_share (the SOL the input-token steps put into the PumpSwap
step's input index, times that step's bps). The platform fee comes off the USDC input: without it
the estimate ran +10..+30 bps (exactly the fee); with it -2.3..+2.8 bps where our price was at most
14 s old (n=9). PumpSwapBuyV3 runs buy_exact_quote_in and PumpSwapBuyV4 buy_exact_quote_in_v2;
both fit the exact-quote model to 2e-10 relative on the fixtures. V4 keeps the creator and
non-buyback protocol fees in the vault, so the real post price is above the v1 reserve model the
synthesized event carries: a stricter cap, never a looser one.
Not handled: SOL-paid routes, three-hop routes, other PumpSwap step variants, other Jupiter
instructions: refused by name.
"""
from __future__ import annotations

from sniper.execution import jupiter_route
from sniper.services.memecoin_rabbit_okx_amm import OkxAmmHandler

ROUTE = "jupiter_pump_amm"


class JupiterAmmHandler(OkxAmmHandler):
    route = ROUTE
    router_program = jupiter_route.JUPITER_PROGRAM
    page_title = "RABBIT JUPITER AMM MODEL"
    knob = "MEMECOIN_SLOT_LANE_RABBIT_JUPITER_AMM"
    prefix = "jup"

    def _route_amount(self, data):
        if data[:8] != jupiter_route.ROUTE_V2:
            return None, "jup_not_route_v2"
        decoded = jupiter_route.decode_route_v2(data)
        if decoded is None:
            return None, "jup_decode"
        if not jupiter_route.pump_steps(decoded):
            return None
        share, why = jupiter_route.pump_share(decoded)
        if share is None:
            return None, "jup_pump_step_unmodelled" if why == "pump_step_unmodelled" else "jup_route_unmodelled"
        fee = decoded["platform_fee_bps"]
        if not 0 <= fee < jupiter_route.BPS_TOTAL:
            return None, "jup_route_unmodelled"
        total = jupiter_route.BPS_TOTAL
        return (decoded["in_amount"] * (total - fee) * share, total ** 3,
                dict(in_amount=decoded["in_amount"], platform_fee_bps=fee, pump_share=share,
                     pump_step=jupiter_route.pump_steps(decoded)[0][0])), None
