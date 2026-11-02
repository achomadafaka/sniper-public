"""Direct pump.fun bonding-curve BUY, funded from USDC inside one atomic transaction. BUILD ONLY.

WHY (operator order, 2026-09-26). Across 36 J7 copy fills on Solana none landed by the source buy's
slot S+1 (best S+3, median S+7), and decision -> submission is 1,380 ms median. A processed-level
watcher sees the source buy while the head is still S or S+1, so the execution path is the blocker.
This module removes the per-signal round trips: the curve buy is computed from the source TradeEvent
itself, the USDC -> SOL leg is prefetched off the critical path, and the only work left when a
signal lands is arithmetic, one message compile and one signature.

WHAT IT BUILDS. One atomic transaction:
    compute budget -> Jupiter USDC -> SOL
    (/swap-instructions, wrapAndUnwrapSol, so the cleanup unwraps into lamports)
    -> ATA create-idempotent for the coin -> pump.fun buy_exact_sol_in(spendable = the leg's MINIMUM
    lamports out, min_tokens_out from the event's post-trade virtual reserves and our slippage cap)
The USDC delta stays the fill cost, so the funded executor's accounting holds; a better-than-minimum
leg leaves lamport dust in the wallet (gas), never an underfunded buy.

NOTHING HERE SENDS. `simulate_curve_buy` proves a build with sigVerify false; wiring is the caller's.

THE ACCOUNT LIST IS 16 IDL ACCOUNTS PLUS 2 REMAINING, verified against live buys on 2026-09-26
(slot 450780443, direct buy_exact_sol_in, sig 3oW9nU7jez...): every captured `buy` and
`buy_exact_sol_in` carries 18 accounts. The IDL names 16. The two extras, identified by PDA
brute force and the Global account:
    [16] bonding_curve_v2       PDA ["bonding-curve-v2", mint] under the pump program, READ-ONLY
    [17] buyback_fee_recipient  one of Global.buyback_fee_recipients[8], WRITABLE
The IDL order and seeds come from pump.fun's on-chain Anchor IDL (AYgC53tU..., 12,801 bytes,
read at slot 450780012). Only SOL-quoted curves are built; a curve quoted in another mint (the
TradeEvent's quote_mint) is refused, because buy_exact_sol_in cannot pay it.
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import math
import operator
import struct
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext

from sniper.execution.pump_native_exit import (
    ATA_PROGRAM, FEE_CONFIG_SEED, FEE_PROGRAM, PUMP_PROGRAM, SYSTEM_PROGRAM, TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM, USDC_MINT, _pda, _pk, _rpc_result, ata, instruction_from_json,
    lookup_table_from_account,
)

WSOL_MINT = "So11111111111111111111111111111111111111112"
# A SOL-quoted curve reports its quote mint as the system program (all zeros), or as wrapped SOL.
SOL_QUOTE_MINTS = frozenset({SYSTEM_PROGRAM, WSOL_MINT})
BUY_EXACT_SOL_IN_DISCRIMINATOR = bytes.fromhex("38fc74089edfcd5f")
BUY_EXACT_QUOTE_IN_V3_DISCRIMINATOR = bytes.fromhex("e1f7501ed5b38488")
# A curve quoted in another token (WBTC, LINK, PUMP, xStocks...) buys through buy_exact_quote_in_v2:
# args (spendable_quote_in u64, min_tokens_out u64), 27 accounts (pump.fun on-chain IDL, read
# 2026-10-07; matches probablyinsider's four DVypDG8A buys account for account).
BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR = bytes.fromhex("c2ab1c46684d5b2f")
TRADE_EVENT_DISCRIMINATOR = bytes([189, 219, 127, 211, 78, 230, 97, 238])
GLOBAL_FEE_RECIPIENT_OFFSET = 41
GLOBAL_RESERVED_FEE_RECIPIENT_OFFSET = 483
GLOBAL_BUYBACK_OFFSET = 933            # Global.buyback_fee_recipients[6], as pump_native_exit reads it
GLOBAL_DISCRIMINATOR = bytes.fromhex("a7e8e8b1c86c727f")
# Env knobs since 2026-09-27 (defaults 400,000 units and 200,000 microlamports per unit, as they
# were hard-coded). pumpswap_buy imports these two names, so both lane venues move together.
from sniper.settings.memecoin import (  # noqa: E402
    MEMECOIN_SLOT_LANE_CU_LIMIT as DEFAULT_COMPUTE_UNIT_LIMIT,
    MEMECOIN_SLOT_LANE_CU_PRICE_MICROLAMPORTS as DEFAULT_PRIORITY_FEE_MICROLAMPORTS,
)
SOLANA_PACKET_LIMIT = 1232
# THE LEG IS PINNED TO COMPACT SINGLE-HOP VENUES (measured 2026-09-26, $1 USDC -> SOL, custody user,
# with the 62-byte slot guard the lane carried until 2026-09-26). Jupiter's unpinned best was a 2-hop AlphaQ + Whirlpool route at 1,426
# bytes, over the 1,232-byte packet. Single hop: BisonFi 1,098, ZeroFi 1,103, Raydium 1,124,
# SolFi V2 1,133, Raydium CLMM 1,166, 1DEX 1,192, Whirlpool 1,224, Meteora DLMM 1,265, HumidiFi
# 1,300. BisonFi also quoted the MOST lamports of all of them (8,249,791 vs the unpinned 8,246,896),
# so the pin costs no price on a $1 clip. Raydium CLMM is left out: its tick arrays vary, and a
# rebuild minutes later assembled at 1,228 bytes WITHOUT the guard (1,290 with it). All four below
# simulated clean end to end on a live curve (units 131,809 / 144,909 / 171,502 / 217,911).
DEFAULT_LEG_QUOTE_PARAMS = "&onlyDirectRoutes=true&dexes=BisonFi,ZeroFi,Raydium,SolFi%20V2"


# --- the source event ---------------------------------------------------------------------------

def parse_trade_event(log_line):
    """Full pump.fun TradeEvent from one `Program data:` log line, as a dict. Raises ValueError.

    The research decoder (memecoin_opportunity.pumpfun) stops at creator_fee; the buy also needs
    the fields after it: quote_mint (to refuse non-SOL curves), the buyback / cashback basis
    points (part of what the buyer pays) and ix_name. Field order is the on-chain IDL's.
    """
    from sniper.research.memecoin_opportunity.decoders import BorshReader, anchor_event_bytes
    raw = anchor_event_bytes(log_line)
    if raw[:8] != TRADE_EVENT_DISCRIMINATOR:
        raise ValueError("not a pump.fun TradeEvent")
    r = BorshReader(raw[8:])
    e = {"mint": r.pubkey(), "sol_amount": r.u64(), "token_amount": r.u64(), "is_buy": r.bool(),
         "user": r.pubkey(), "timestamp": r.i64(), "virtual_sol_reserves": r.u64(),
         "virtual_token_reserves": r.u64(), "real_sol_reserves": r.u64(),
         "real_token_reserves": r.u64(), "fee_recipient": r.pubkey(), "fee_basis_points": r.u64(),
         "fee": r.u64(), "creator": r.pubkey(), "creator_fee_basis_points": r.u64(),
         "creator_fee": r.u64(), "track_volume": r.bool(), "total_unclaimed_tokens": r.u64(),
         "total_claimed_tokens": r.u64(), "current_sol_volume": r.u64(),
         "last_update_timestamp": r.i64(), "ix_name": r.string(), "mayhem_mode": r.bool(),
         "cashback_fee_basis_points": r.u64(), "cashback": r.u64(),
         "buyback_fee_basis_points": r.u64(), "buyback_fee": r.u64()}
    e["shareholders"] = [(r.pubkey(), int.from_bytes(r.take(2), "little")) for _ in range(r.u32())]
    e.update({"quote_mint": r.pubkey(), "quote_amount": r.u64(), "virtual_quote_reserves": r.u64(),
              "real_quote_reserves": r.u64(), "holder_rewards_bps": r.u64(),
              "holder_rewards": r.u64()})
    return e


def trade_event_gross_lamports(event):
    """Lamports the buyer paid into the curve, pool fees included: sol_amount + fee + creator_fee,
    or None when any leg is unreadable or negative. (buyback_fee is a share of the protocol fee,
    already inside `fee`.)"""
    try:
        legs = [int(event[k]) for k in ("sol_amount", "fee", "creator_fee")]
    except (KeyError, TypeError, ValueError):
        return None
    return sum(legs) if min(legs) >= 0 else None


def curve_source_average_usd(event, source_price_usd):
    """The source's own average fill on a SOL curve, USD per whole token: its post-trade price scaled by
    what it paid (fees included) over what it got, against the post-trade reserves. The hard cap
    (MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT) is over this. One formula for the curve buy and for the
    graduation path's PumpSwap buy of the same source. ValueError when unreadable."""
    try:
        return (float(source_price_usd) * trade_event_gross_lamports(event)
                * int(event['virtual_token_reserves'])
                / (int(event['token_amount']) * int(event['virtual_sol_reserves'])))
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError('source average price unavailable') from exc


def token_program_from_logs(logs, event_index=None):
    """The coin's token program: a token-program invoke ONE LEVEL INSIDE the pump.fun invocation
    that emitted the event at `event_index`, or None when that is not exactly one program.

    The first token invoke of the whole transaction is NOT it: a router moves USDC or wSOL with the
    classic program before the pump CPI (2026-09-26: that heuristic handed a classic program to a
    Token-2022 coin, and the ATA create failed IncorrectProgramId in sim). Nor is the first pump
    invocation's: a transaction can carry another pump event first, for another coin (Codex
    re-review, 2026-09-26). Without an event index, only one token program across every pump
    invocation is usable. Two programs inside the event's own invocation are unknown, not a guess.
    """
    stack, every, selected = [], set(), None     # stack: (depth, token programs) per pump frame
    tokens = (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)
    for index, line in enumerate(logs or ()):
        # Plain string tests, not regexes: this runs on the slot lane's signal path.
        cut = line.find(" invoke [") if line.startswith("Program ") else -1
        if cut > 0:
            try:
                depth = int(line[cut + 9:-1])
            except ValueError:
                return None
            while stack and stack[-1][0] >= depth:
                stack.pop()
            program = line[8:cut]
            if program == PUMP_PROGRAM:
                stack.append((depth, set()))
            elif stack and depth == stack[-1][0] + 1 and program in tokens:
                stack[-1][1].add(program)
                every.add(program)
        elif index == event_index:
            if not (stack and line.startswith("Program data: ")):
                return None
            selected = stack[-1][1]    # the frame's own set; later CPIs in it still land here
        elif stack and line.startswith("Program " + PUMP_PROGRAM + " "):
            if line.endswith(" success") or " failed:" in line:
                frame = stack.pop()
                if frame[1] is selected:
                    break              # the event's own invocation is complete
    programs = every if event_index is None else selected
    return next(iter(programs)) if programs is not None and len(programs) == 1 else None


def total_fee_bps(event):
    """Basis points the buyer pays on top of the net spend.

    BUYBACK IS NOT ADDED: it is a SHARE of the protocol fee, not a fee of its own. pump.fun's Global
    carries buyback_basis_points = 5000, and a live PumpSwap buy with buyback_fee_basis_points 5000
    booked a buyback fee of exactly half its protocol fee while lp + protocol + creator summed to
    the spend exactly (2026-09-26). Adding it would read a 50% fee. Cashback is kept: no live frame
    has carried a non-zero one, and counting it lowers the expected output (a looser floor)."""
    return sum(int(event.get(k) or 0) for k in (
        "fee_basis_points", "creator_fee_basis_points", "cashback_fee_basis_points"))


def curve_buy_net(spend_lamports, event):
    """The net lamports a buy_exact_sol_in of `spend_lamports` puts into the curve (the event's
    sol_amount): the largest net whose fees, each rounded UP, still fit in the spend. Exact on 19 of
    20 landed buys (2026-10-16, tests/fixtures/memecoin/rabbit_curve_buys_20261016.json; the 20th a
    capped partial fill). The floor net, spend x 1e4 // (1e4 + fee bps), was one lamport high on 10 of
    20. Cashback bps (no live frame has carried one) counts as a third fee: a lower net, a looser floor."""
    spend = int(spend_lamports)
    parts = [int(event.get(k) or 0) for k in (
        "fee_basis_points", "creator_fee_basis_points", "cashback_fee_basis_points")]
    net = spend * 10_000 // (10_000 + sum(parts))
    while net > 0 and net + sum(-(-net * bps // 10_000) for bps in parts) > spend:
        net -= 1
    return net


def expected_tokens_out(spend_lamports, event):
    """Tokens a buy_exact_sol_in of `spend_lamports` receives against the given reserves: the net
    (curve_buy_net), then the program prices ONE LAMPORT LESS than it, on the constant-product curve
    (floor), capped at the curve's real token reserves. Exact to the atomic unit on 19 of 20 landed
    buys (the 20th capped by the curve). Pass the SOURCE event's post-trade reserves to size a copy."""
    net = curve_buy_net(spend_lamports, event) - 1
    v_sol, v_tok = int(event["virtual_sol_reserves"]), int(event["virtual_token_reserves"])
    if net <= 0 or v_sol <= 0 or v_tok <= 0:
        return 0
    return min(v_tok * net // (v_sol + net), int(event["real_token_reserves"]))


USDC_DECIMALS = 6
PUMP_TOKEN_DECIMALS = 6


def source_price_floor_atomic(usdc_atomic, source_price_usd, cap_pct,
                              base_decimals=PUMP_TOKEN_DECIMALS):
    """Fewest base-token atomic units a `usdc_atomic` ticket may buy at source price x (1 + cap/100).

    Rounded UP, so the signed floor can never admit a unit price above the cap. `base_decimals` is
    the base mint's own decimals: a pump curve coin has 6, a PumpSwap pool's base mint may not, and
    reusing 6 there puts the floor off by a power of ten. Raises ValueError on a missing,
    non-finite, zero or negative input."""
    try:
        cost, price, cap = (Decimal(str(v)) for v in (usdc_atomic, source_price_usd, cap_pct))
        decimals = operator.index(base_decimals)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid source-price cap inputs") from exc
    if (not all(v.is_finite() for v in (cost, price, cap))
            or cost <= 0 or price <= 0 or cap < 0 or decimals < 0):
        raise ValueError("invalid source-price cap inputs")
    with localcontext() as ctx:
        ctx.prec = 50
        tokens = cost / 10 ** USDC_DECIMALS / (price * (1 + cap / 100)) * 10 ** decimals
        return int(tokens.to_integral_value(rounding=ROUND_CEILING))


SAME_SLOT_HEAD_MAX_AGE_S = 0.8        # two slots: an older head observation says nothing about where the transaction will land


def buy_same_slot(buy):
    """True only when the receipt proves a same-slot exposure: the frame's own head observation is FRESH (recv_monotonic - head_at_recv_mono in [0, SAME_SLOT_HEAD_MAX_AGE_S], both stamps present and finite)
    AND the head is within one slot of the source's slot S (head - S in {-1, 0}). A stale, unstamped, far or routed-pair head (no freshness stamp exists for it) is False, so the send keeps the chase
    haircut. Fields already in hand; no I/O, lock or wait."""
    head = getattr(buy, "head_at_recv", None)
    seen, recv = getattr(buy, "head_at_recv_mono", None), getattr(buy, "recv_monotonic", None)
    try:
        slot = int(getattr(buy, "slot", None))
    except (TypeError, ValueError):
        return False
    stamped = all(type(x) in (int, float) and math.isfinite(x) for x in (seen, recv))
    return (type(head) is int and head > 0 and stamped and 0.0 <= recv - seen <= SAME_SLOT_HEAD_MAX_AGE_S
            and -1 <= head - slot <= 0)


def output_haircut(same_slot=False):
    """(pct, label) of the OUTPUT haircut this send signs in market-movement-only mode; ValueError unless finite and in [0, 100)."""
    from sniper.settings import memecoin as cfg
    try:
        pct = float(cfg.MEMECOIN_REAL_SAME_SLOT_CHASE_MAX_PCT if same_slot else cfg.MEMECOIN_REAL_CHASE_MAX_PCT)      # the same-slot knob is a raw string: an unparsable value raises HERE, for this send only
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid output slippage") from exc
    if not math.isfinite(pct) or not 0 <= pct < 100:
        raise ValueError("invalid output slippage")
    return pct, ("same_slot" if same_slot else "chase")


def capped_min_out(expected, slippage_pct, *, source_price_usd=None, source_cost_usdc_atomic=None,
                   source_price_cap_pct=None, base_decimals=PUMP_TOKEN_DECIMALS,
                   source_average_price_usd, copy_cap_pct=None, same_slot=False):
    """(min_out, output_floor, price_floor) for a slot-lane buy; ValueError on refusal.

    The output floor is the old token haircut, `expected * (1 - slippage/100)`: at a cap of 30 it
    admits a unit price 42.9% over our own estimate. The price floor (2026-09-26) caps the unit
    price of the WHOLE USDC ticket at source price x (1 + cap/100); with no cap given it is 0 and
    the haircut alone applies, as before. The stronger floor is signed. When even our own impact
    on the source's post-trade reserves breaks the cap, the send could only fail, so it refuses
    here, before signing.

    `source_average_price_usd` is REQUIRED (SOL-1, Codex review 2026-10-06): the source's own
    average fill, USD per whole token, which MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT caps. The chase
    floor above is over the POST-trade price; on a high-impact source buy 30% over that was 41% over
    the average, and two builders signed it because they never passed this optional argument. With
    the hard cap on, None (an unknown average) refuses; with it off, the average is ignored.
    `copy_cap_pct` is the wallet's hard cap (settings.copy_premium_cap); None is the global. Whether
    the hard cap is ON stays the global's decision: a wallet value never turns it on.

    In market-movement-only mode the source-relative floors do not apply: both include our own
    ticket impact, and the average reference also includes the source's impact. The independent
    market-state guard owns that policy. The unlifted native output haircut remains independent
    of wallet movement limits and must be finite, in [0, 100), and leave a positive floor."""
    from sniper.settings import memecoin as cfg
    movement_only = cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY
    if movement_only:
        slippage_pct = output_haircut(same_slot)[0]               # same_slot: the lane's receipt facts say this send can land in S (case WALT e0ee3456)
    output_floor = int(expected * (1.0 - float(slippage_pct) / 100.0))
    price_floor = 0
    if not movement_only and source_price_cap_pct is not None:
        price_floor = source_price_floor_atomic(source_cost_usdc_atomic, source_price_usd,
                                                source_price_cap_pct, base_decimals)
    if not movement_only and cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT > 0:
        if source_average_price_usd is None:
            raise ValueError("source average price unavailable")
        hard = cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT if copy_cap_pct is None else float(copy_cap_pct)
        price_floor = max(price_floor, source_price_floor_atomic(
            source_cost_usdc_atomic, source_average_price_usd, hard, base_decimals))
    min_out = max(output_floor, price_floor)
    if min_out <= 0:
        raise ValueError("clip buys no tokens against these reserves")
    if min_out > expected:
        raise ValueError("source-price cap refuses the buy's own impact: floor %d over expected %d"
                         % (min_out, expected))
    return min_out, output_floor, price_floor


def routed_average_price_usd(routed, decimals):
    """The source's average fill from its routed receipt, USD per whole token: USDC spent / coins
    received, both from the owner's own token accounts (the watcher's average, DAMM's source price).
    `decimals` is the coin mint's own. None when either amount is missing or not positive, or the
    receipt's coin decimals disagree with the mint's (the receipt is then not this coin's)."""
    try:
        spent, got = operator.index(routed.usdc_spent_atomic), operator.index(routed.token_delta_atomic)
        decimals = operator.index(decimals)
    except (AttributeError, TypeError):
        return None
    seen = getattr(routed, "token_decimals", None)
    if spent <= 0 or got <= 0 or decimals < 0 or (seen is not None and seen != decimals):
        return None
    return spent / 10 ** USDC_DECIMALS / (got / 10 ** decimals)


# The hard cap's reference when no routed receipt is given: the venue event's own average (N1,
# Codex night review 2026-10-06). A routed buy passes its WHOLE receipt's average instead.
EVENT_AVERAGE = object()


def hard_cap_average(buy, decimals):
    """EVENT_AVERAGE for a buy without a routed receipt; for a routed one, the WHOLE receipt's
    average (USDC paid / coins acquired, routed_average_price_usd), None when unknown. A split route
    (Codex N1: $2,000 into this venue, $2,100 for twice the coins over the whole route) prices the
    source's average from everything it paid and got, never from the venue leg alone."""
    routed = getattr(buy, "routed", None)
    return EVENT_AVERAGE if routed is None else routed_average_price_usd(routed, decimals)


def bounded_whole_average(whole, event_average):
    """A routed buy's whole average checked against its own venue event's average (N1 residual 1,
    Alpha 2026-10-07), with zero added latency: both come from the frame in hand. Unchanged while
    the hard cap is off. None (unknown: the cap refuses) when either is missing; raises ValueError
    when the whole average exceeds the event's by more than MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO,
    because then the owner's USDC fall carries more than the route. Lower is never refused: it only
    tightens the cap."""
    from sniper.settings import memecoin as cfg
    if cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT <= 0:
        return whole
    if whole is None or not (isinstance(event_average, (int, float)) and math.isfinite(event_average)
                             and event_average > 0):
        return None
    ratio = whole / event_average
    if ratio > cfg.MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO:
        raise ValueError("routed average %.3fx its venue event's (max %.2fx): unknown"
                         % (ratio, cfg.MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO))
    return whole


def leg_route_keys(leg):
    """The prefetched USDC -> SOL leg's Jupiter ammKeys, in route order."""
    plan = ((leg or {}).get("quote") or {}).get("routePlan") or ()
    return [str(step["swapInfo"]["ammKey"]) for step in plan
            if isinstance(step, dict) and ((step.get("swapInfo") or {}).get("ammKey"))]


def leg_dex(leg):
    """The prefetched leg's one Jupiter dex label (its routePlan has exactly one step), or None."""
    plan = ((leg or {}).get("quote") or {}).get("routePlan") or ()
    labels = [str((step.get("swapInfo") or {}).get("label")) for step in plan if isinstance(step, dict)]
    return labels[0] if len(labels) == 1 else None


# CU CLASSES DEMOTED UNTIL RESTART (Gamma MED on a81d7d381). A pump program upgrade that costs ~15%
# more CU would fail EVERY limited send of a class at once, each failure a lost trade. The first slot
# send of a class-limited build that runs out of compute units demotes its class to the lane default
# for the rest of this process. Written by the lane thread only (it alone sends), add-only, read by
# the builders on that same thread: a membership test, never a capture-and-restore.
CU_DEMOTED = set()


def demote_cu_class(cls):
    """Demote `cls` for this process. True the first time (the caller logs it), False after."""
    if cls is None or cls in CU_DEMOTED:
        return False
    CU_DEMOTED.add(cls)
    return True


def cu_exhausted(result):
    """True when a failed send's chain error or logs say the compute-unit meter ran out."""
    import json as _json
    try:
        err = _json.dumps((result or {}).get("chain_err"), default=str)
    except (TypeError, ValueError):
        err = str((result or {}).get("chain_err"))
    text = err + " " + str((result or {}).get("program_error") or "")
    # Three wordings: the chain's error enum, the meter's log line, and the runtime's own
    # `Program <id> failed: Computational budget exceeded` (program_error; 4oFrhiTQ, 2026-10-07).
    return ("ComputationalBudgetExceeded" in text or "exceeded CUs meter" in text
            or "Computational budget exceeded" in text)


def build_cu_limit(builder, venue, funding, *, leg=None, token_program=None, native_variant=None):
    """(compute-unit limit, measured class or None, class key) for one slot-lane build: the builder's
    own limit when it was given one, else the per-class table (settings.slot_lane_cu_class), else the
    lane default; a demoted class signs the lane default. The key (venue, funding, leg dex or native
    instruction layout, coin token
    program) is what the table was asked, recorded on every build so a class can later be justified
    from the ledger (spec 2026-10-09 CU right-sizing, section 9). Pure lookup, no I/O."""
    from sniper.settings import memecoin as cfg
    dex = leg_dex(leg) if funding == "leg" else native_variant if funding == "native" else None
    key = (venue, funding, dex, token_program)
    if builder._cu_explicit:
        return builder.compute_unit_limit, None, key
    cls = cfg.slot_lane_cu_class(venue, funding, leg_dex=dex, token_program=token_program)
    if cls is None or cls in CU_DEMOTED:
        return cfg.MEMECOIN_SLOT_LANE_CU_LIMIT, None, key
    return cfg.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS[cls], cls, key


def select_priority_fee(builder, pool, cu_limit, tip, override_lamports=None):
    """Read the injected immutable bid snapshot after the final CU limit; no I/O. `override_lamports`
    (memecoin_crowded_fee, a treated crowded send) raises the priority fee to that many lamports."""
    if builder.priority_fee_fn is None:
        price, bid = builder.priority_fee_microlamports, None
    else:
        bid = builder.priority_fee_fn(pool=str(pool), cu_limit=int(cu_limit),
                                      tip_lamports=tip[1] if tip else 0)
        price = bid.get("micro_lamports_per_cu") if isinstance(bid, dict) else None
        if type(price) is not int or price < 0:
            raise ValueError("priority_bid_invalid")
        bid = dict(bid)
    if override_lamports:
        return crowded_priority(price, bid, int(cu_limit), tip, int(override_lamports))
    return price, bid


def static_priority(builder, cu_limit, tip, override_lamports, default):
    """(price, priority_bid) for a builder with no sampled bid (CPMM, LaunchLab, DAMM v2, DBC): its
    static compute-unit price (`default` when unset), raised for a treated crowded send to the target
    from THIS build's own cu_limit (crowded_priority: never past MAX_SEND with the tip, never below the
    static price). No override: the static price and no bid record, exactly as before."""
    price = int(getattr(builder, "priority_fee_microlamports", None) or default)
    if not override_lamports:
        return price, None
    return crowded_priority(price, None, int(cu_limit), tip, int(override_lamports))


def crowded_priority(price, bid, cu_limit, tip, target_lamports):
    """The crowded send's compute-unit price: at least `target_lamports` of priority fee, never past
    MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS for priority + tip together, never below the
    normal bid. The fee is ceil(price x limit / 1e6), so the price is rounded up toward the target
    and down under the cap."""
    from sniper.settings import memecoin as cfg
    tip_lamports = int(tip[1]) if tip else 0
    room = int(cfg.MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS) - tip_lamports
    target = min(int(target_lamports), room)
    base = dict(bid or {}, crowded_target_lamports=target_lamports, crowded_room_lamports=room)
    if target <= 0:
        return price, dict(base, crowded="no_room")
    unit = min(-(-target * 1_000_000 // cu_limit), room * 1_000_000 // cu_limit)
    if unit <= price:
        return price, dict(base, crowded="normal_bid_higher")
    lamports = -(-unit * cu_limit // 1_000_000)
    return unit, dict(base, crowded="raised", micro_lamports_per_cu=unit, priority_fee_lamports=lamports,
                      tip_lamports=tip_lamports, combined_fee_lamports=lamports + tip_lamports,
                      priority_source="crowded_override", cu_limit=cu_limit)


def curve_route_target(mint, leg=None):
    """The entry route in the shape the J7 path writes to quote_route_target (Jupiter ammKeys,
    comma-joined): the leg's keys, then the coin-facing key, the bonding-curve PDA. vol_death
    matches the exit route's token-facing ammKey against these keys; a venue label never matches
    (2026-09-26, 65860 held as route_changed for good)."""
    return ",".join(leg_route_keys(leg) + [str(_pda([b"bonding-curve", bytes(_pk(mint))],
                                                    PUMP_PROGRAM))])


def curve_source_price_usd(event, sol_usd):
    """USD per whole token at the source event's POST-trade virtual reserves, or None."""
    try:
        price = ((int(event["virtual_sol_reserves"]) / 1e9)
                 / (int(event["virtual_token_reserves"]) / 10 ** PUMP_TOKEN_DECIMALS) * float(sol_usd))
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    return price if math.isfinite(price) and price > 0 else None


# --- the pump instruction -----------------------------------------------------------------------

def buy_exact_sol_in_data(spendable_lamports, min_tokens_out, track_volume=False):
    spend, floor = int(spendable_lamports), int(min_tokens_out)
    if spend <= 0 or floor <= 0:
        raise ValueError("buy_exact_sol_in needs a positive spend and a positive token floor")
    # OptionBool is a one-bool struct in the IDL: one byte, always sent.
    return BUY_EXACT_SOL_IN_DISCRIMINATOR + struct.pack("<QQ?", spend, floor, bool(track_volume))


def curve_buy_accounts(*, user, mint, token_program, creator, fee_recipient, buyback_fee_recipient):
    """The 18 accounts of pump.fun buy_exact_sol_in, IDL order + 2 remaining, as
    (pubkey, is_signer, is_writable)."""
    user, mint, tp = _pk(user), _pk(mint), _pk(token_program)
    fee = _pk(fee_recipient)
    if fee == _pk(SYSTEM_PROGRAM):
        raise ValueError("no_protocol_fee_recipient")
    curve = _pda([b"bonding-curve", bytes(mint)], PUMP_PROGRAM)
    return [
        (_pda([b"global"], PUMP_PROGRAM), False, False),
        (fee, False, True),
        (mint, False, False),
        (curve, False, True),
        (ata(curve, mint, tp), False, True),
        (ata(user, mint, tp), False, True),
        (user, True, True),
        (_pk(SYSTEM_PROGRAM), False, False),
        (tp, False, False),
        (_pda([b"creator-vault", bytes(_pk(creator))], PUMP_PROGRAM), False, True),
        (_pda([b"__event_authority"], PUMP_PROGRAM), False, False),
        (_pk(PUMP_PROGRAM), False, False),
        (_pda([b"global_volume_accumulator"], PUMP_PROGRAM), False, False),
        (_pda([b"user_volume_accumulator", bytes(user)], PUMP_PROGRAM), False, True),
        (_pda([b"fee_config", FEE_CONFIG_SEED], FEE_PROGRAM), False, False),
        (_pk(FEE_PROGRAM), False, False),
        (_pda([b"bonding-curve-v2", bytes(mint)], PUMP_PROGRAM), False, False),
        (_pk(buyback_fee_recipient), False, True),
    ]


def curve_buy_v3_accounts(*, user, mint, token_program, buyback_fee_recipient):
    """SOL-quoted v3: 17 IDL accounts, with no mutable creator-vault dependency."""
    user, mint, tp = _pk(user), _pk(mint), _pk(token_program)
    quote = _pk(WSOL_MINT)
    curve = _pda([b"bonding-curve", bytes(mint)], PUMP_PROGRAM)
    return [
        (_pda([b"global"], PUMP_PROGRAM), False, False),
        (mint, False, False), (quote, False, False),
        (tp, False, False), (_pk(TOKEN_PROGRAM), False, False),
        (curve, False, True), (ata(curve, mint, tp), False, True),
        (ata(curve, quote, TOKEN_PROGRAM), False, True),
        (user, True, True), (ata(user, mint, tp), False, True),
        (ata(user, quote, TOKEN_PROGRAM), False, True),
        (_pda([b"user_volume_accumulator", bytes(user)], PUMP_PROGRAM), False, True),
        (_pda([b"fee_config", FEE_CONFIG_SEED], FEE_PROGRAM), False, False),
        (_pk(buyback_fee_recipient), False, True),
        (_pk(SYSTEM_PROGRAM), False, False),
        (_pda([b"__event_authority"], PUMP_PROGRAM), False, False),
        (_pk(PUMP_PROGRAM), False, False),
    ]


def curve_buy_instructions(event, spend_lamports, slippage_pct, *, user, token_program,
                           buyback_fee_recipient, source_price_usd=None,
                           source_cost_usdc_atomic=None, source_price_cap_pct=None,
                           whole_average_usd=EVENT_AVERAGE, copy_cap_pct=None, v3=False, same_slot=False):
    """[create-idempotent coin ATA, buy_exact_sol_in] and the amounts, from the source event alone.
    Raises ValueError on anything that cannot be bought on a SOL-quoted curve. The minimum out is
    `capped_min_out`: with a source-price cap it is a unit-price cap on the whole ticket."""
    from solders.instruction import AccountMeta, Instruction
    if not event.get("is_buy"):
        raise ValueError("source event is not a buy")
    if event.get("quote_mint") not in SOL_QUOTE_MINTS:
        raise ValueError("curve is quoted in %s, not SOL" % event.get("quote_mint"))
    if int(event.get("real_token_reserves") or 0) <= 0:
        raise ValueError("curve is complete: no real token reserves left")
    if token_program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise ValueError("unknown token program %r" % token_program)
    expected = expected_tokens_out(spend_lamports, event)
    from sniper.settings import memecoin as cfg
    average = None
    if cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT > 0:
        average = curve_source_average_usd(event, source_price_usd)
        if whole_average_usd is not EVENT_AVERAGE:      # a routed receipt's whole average
            average = bounded_whole_average(whole_average_usd, average)   # None refuses below
    min_out, output_floor, price_floor = capped_min_out(
        expected, slippage_pct, source_price_usd=source_price_usd,
        source_cost_usdc_atomic=source_cost_usdc_atomic, source_price_cap_pct=source_price_cap_pct,
        source_average_price_usd=average, copy_cap_pct=copy_cap_pct, same_slot=same_slot)
    user_pk, mint_pk, tp_pk = _pk(user), _pk(event["mint"]), _pk(token_program)
    create_ata = Instruction(_pk(ATA_PROGRAM), bytes([1]), [      # 1 = CreateIdempotent
        AccountMeta(user_pk, True, True), AccountMeta(ata(user_pk, mint_pk, tp_pk), False, True),
        AccountMeta(user_pk, False, False), AccountMeta(mint_pk, False, False),
        AccountMeta(_pk(SYSTEM_PROGRAM), False, False), AccountMeta(tp_pk, False, False)])
    accounts = (curve_buy_v3_accounts(user=user_pk, mint=mint_pk, token_program=tp_pk,
                                    buyback_fee_recipient=buyback_fee_recipient) if v3 else
                curve_buy_accounts(user=user_pk, mint=mint_pk, token_program=tp_pk,
                                   creator=event["creator"], fee_recipient=event["fee_recipient"],
                                   buyback_fee_recipient=buyback_fee_recipient))
    metas = [AccountMeta(k, s, w) for k, s, w in accounts]
    discriminator = BUY_EXACT_QUOTE_IN_V3_DISCRIMINATOR if v3 else BUY_EXACT_SOL_IN_DISCRIMINATOR
    # The trailing false is partial_fill for v3: true can bypass min_tokens_out on Mayhem.
    data = discriminator + buy_exact_sol_in_data(spend_lamports, min_out)[8:]
    buy = Instruction(_pk(PUMP_PROGRAM), data, metas)
    return [create_ata, buy], {"spend_lamports": int(spend_lamports), "expected_tokens_out": expected,
                               "output_floor_atomic": output_floor,
                               "source_price_floor_atomic": price_floor, "min_tokens_out": min_out,
                               "source_average_price_usd": average,
                               "instruction_discriminator": discriminator.hex()}


# --- a curve quoted in another token (2026-10-07, operator "Fix" via Alpha) ----------------------

# THE PUMP ADDRESS LOOKUP TABLE (2026-10-07, Alpha). A quote-curve buy is a Jupiter leg plus 27
# accounts; a 1-hop leg built 1,111-1,317 bytes and a 2-hop one 1,383-1,446, against the 1,232-byte
# packet. These 12 keys are named by every quote-curve buy of one wallet and by no leg; in a table
# they save 152 bytes. scripts/memecoin_create_pump_alt.py creates the table (the operator runs
# --confirm) and writes its address to PUMP_ALT_STATE; the curve builder reads it once off the send
# path and uses it only when it holds exactly these keys and was never deactivated.
ALT_PROGRAM = "AddressLookupTab1e1111111111111111111111111"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
PUMP_ALT_STATE = ("data", "slot_lane_pump_alt.json")
PUMP_ALT_RETRY_S = 60.0
_U64_MAX = 2 ** 64 - 1


def pump_alt_keys(user):
    """The 12 table addresses for `user`'s quote-curve buys, in table order."""
    return [str(k) for k in (
        _pda([b"global"], PUMP_PROGRAM), _pda([b"__event_authority"], PUMP_PROGRAM), _pk(PUMP_PROGRAM),
        _pk(FEE_PROGRAM), _pda([b"fee_config", FEE_CONFIG_SEED], FEE_PROGRAM),
        _pda([b"global_volume_accumulator"], PUMP_PROGRAM),
        _pda([b"user_volume_accumulator", bytes(_pk(user))], PUMP_PROGRAM),
        _pk(TOKEN_PROGRAM), _pk(TOKEN_2022_PROGRAM), _pk(ATA_PROGRAM), _pk(SYSTEM_PROGRAM),
        _pk(COMPUTE_BUDGET_PROGRAM))]


def verified_pump_alt(address, account, user):
    """(AddressLookupTableAccount, None) when `account` (getAccountInfo base64 value) is an active
    lookup table owned by the ALT program holding exactly pump_alt_keys(user), else (None, why)."""
    import base64 as _b64
    from solders.address_lookup_table_account import AddressLookupTable, AddressLookupTableAccount
    if not account:
        return None, "table_absent"
    if account.get("owner") != ALT_PROGRAM:
        return None, "table_owner_%s" % account.get("owner")
    try:
        table = AddressLookupTable.deserialize(_b64.b64decode(account["data"][0]))
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return None, "table_unreadable_%s" % type(exc).__name__
    if int(table.meta.deactivation_slot) != _U64_MAX:
        return None, "table_deactivated"
    # Whoever holds the authority can deactivate the table under a running lane: ours, or frozen.
    if table.meta.authority is not None and str(table.meta.authority) != str(user):
        return None, "table_authority_foreign"
    held = [str(a) for a in table.addresses]
    if len(held) != 12 or set(held) != set(pump_alt_keys(user)):
        return None, "table_keys_mismatch"
    return AddressLookupTableAccount(_pk(address), list(table.addresses)), None


def quote_curve_tokens_out(spend_quote_atomic, event):
    """Tokens buy_exact_quote_in_v2 of `spend_quote_atomic` receives against the event's quote
    reserves. The net is the largest amount whose protocol, creator and cashback fees, each rounded
    UP, fit the spend; the curve then prices ONE ATOM LESS than that net (constant product, floor),
    capped at the real token reserves. Fitted on all four DVypDG8A frames: the exact-in buy
    (2Bha6MmU1s: 118,090 -> net 115,830, priced 115,829 -> 3,126,491,284,716 tokens) and the
    three exact-tokens buys (their nets, the event's quote_amount, price unreduced) are exact."""
    spend = int(spend_quote_atomic)
    bps = [int(event.get(k) or 0) for k in (
        "fee_basis_points", "creator_fee_basis_points", "cashback_fee_basis_points")]
    net = spend * 10_000 // (10_000 + sum(bps))
    while net > 0 and net + sum(-(-net * b // 10_000) for b in bps) > spend:
        net -= 1
    priced = net - 1
    v_quote, v_tok = int(event["virtual_quote_reserves"]), int(event["virtual_token_reserves"])
    if priced <= 0 or v_quote <= 0 or v_tok <= 0:
        return 0
    return min(v_tok * priced // (v_quote + priced), int(event["real_token_reserves"]))


def buy_exact_quote_in_v2_data(spendable_quote, min_tokens_out):
    spend, floor = int(spendable_quote), int(min_tokens_out)
    if spend <= 0 or floor <= 0:
        raise ValueError("buy_exact_quote_in_v2 needs a positive spend and a positive token floor")
    return BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR + struct.pack("<QQ", spend, floor)


def quote_curve_buy_accounts(*, user, mint, quote_mint, base_token_program, quote_token_program,
                             creator, fee_recipient, buyback_fee_recipient):
    """The 27 accounts of buy_exact_quote_in_v2 (IDL order), as (pubkey, is_signer, is_writable).
    `user` is the one signer, as in the IDL."""
    user, mint, quote = _pk(user), _pk(mint), _pk(quote_mint)
    btp, qtp = _pk(base_token_program), _pk(quote_token_program)
    curve = _pda([b"bonding-curve", bytes(mint)], PUMP_PROGRAM)
    fee, buyback = _pk(fee_recipient), _pk(buyback_fee_recipient)
    if fee == _pk(SYSTEM_PROGRAM):
        raise ValueError("no_protocol_fee_recipient")
    vault = _pda([b"creator-vault", bytes(_pk(creator))], PUMP_PROGRAM)
    volume = _pda([b"user_volume_accumulator", bytes(user)], PUMP_PROGRAM)
    return [
        (_pda([b"global"], PUMP_PROGRAM), False, False),
        (mint, False, False),
        (quote, False, False),
        (btp, False, False),
        (qtp, False, False),
        (_pk(ATA_PROGRAM), False, False),
        (fee, False, True),
        (ata(fee, quote, qtp), False, True),
        (buyback, False, True),
        (ata(buyback, quote, qtp), False, True),
        (curve, False, True),
        (ata(curve, mint, btp), False, True),
        (ata(curve, quote, qtp), False, True),
        (user, True, True),
        (ata(user, mint, btp), False, True),
        (ata(user, quote, qtp), False, True),
        (vault, False, True),
        (ata(vault, quote, qtp), False, True),
        (_pda([b"sharing-config", bytes(mint)], FEE_PROGRAM), False, False),
        (_pda([b"global_volume_accumulator"], PUMP_PROGRAM), False, False),
        (volume, False, True),
        (ata(volume, quote, qtp), False, True),
        (_pda([b"fee_config", FEE_CONFIG_SEED], FEE_PROGRAM), False, False),
        (_pk(FEE_PROGRAM), False, False),
        (_pk(SYSTEM_PROGRAM), False, False),
        (_pda([b"__event_authority"], PUMP_PROGRAM), False, False),
        (_pk(PUMP_PROGRAM), False, False),
    ]


def quote_curve_gross(event):
    """Quote atoms the buyer paid: the net into the curve plus protocol and creator fees (the
    exact-in spend on 2Bha6MmU1s: 115,830 + 1,101 + 1,159 = 118,090). None when unreadable."""
    try:
        legs = [int(event[k]) for k in ("quote_amount", "fee", "creator_fee")]
    except (KeyError, TypeError, ValueError):
        return None
    return sum(legs) if min(legs) >= 0 and legs[0] > 0 else None


def quote_curve_prices(event, quote_decimals, quote_usd, base_decimals=PUMP_TOKEN_DECIMALS):
    """(post-trade price, source average price), USD per whole token, from the event's quote fields
    at `quote_usd`; (None, None) when any input is missing, zero or non-finite."""
    try:
        tokens = int(event["virtual_token_reserves"]) / 10 ** int(base_decimals)
        post = int(event["virtual_quote_reserves"]) / 10 ** int(quote_decimals) / tokens * float(quote_usd)
        gross = quote_curve_gross(event)
        average = (gross / 10 ** int(quote_decimals) * float(quote_usd)
                   / (int(event["token_amount"]) / 10 ** int(base_decimals)))
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None, None
    if not all(math.isfinite(v) and v > 0 for v in (post, average)):
        return None, None
    return post, average


def quote_curve_ixs(event, spend_quote_atomic, min_out, *, user, base_token_program, quote_token_program,
                    buyback_fee_recipient):
    """[create-idempotent coin ATA, buy_exact_quote_in_v2(spend, min_out)]; no price judgement."""
    from solders.instruction import AccountMeta, Instruction
    user_pk, mint_pk, btp = _pk(user), _pk(event["mint"]), _pk(base_token_program)
    create_ata = Instruction(_pk(ATA_PROGRAM), bytes([1]), [      # 1 = CreateIdempotent
        AccountMeta(user_pk, True, True), AccountMeta(ata(user_pk, mint_pk, btp), False, True),
        AccountMeta(user_pk, False, False), AccountMeta(mint_pk, False, False),
        AccountMeta(_pk(SYSTEM_PROGRAM), False, False), AccountMeta(btp, False, False)])
    metas = [AccountMeta(k, s, w) for k, s, w in quote_curve_buy_accounts(
        user=user_pk, mint=mint_pk, quote_mint=event["quote_mint"], base_token_program=btp,
        quote_token_program=quote_token_program, creator=event["creator"],
        fee_recipient=event["fee_recipient"], buyback_fee_recipient=buyback_fee_recipient)]
    return [create_ata, Instruction(_pk(PUMP_PROGRAM), buy_exact_quote_in_v2_data(spend_quote_atomic, min_out),
                                    metas)]


def quote_curve_buy_instructions(event, spend_quote_atomic, slippage_pct, *, user, base_token_program,
                                 quote_token_program, buyback_fee_recipient, source_price_usd,
                                 source_cost_usdc_atomic, source_price_cap_pct,
                                 source_average_price_usd, base_decimals, copy_cap_pct=None, same_slot=False):
    """[create-idempotent coin ATA, buy_exact_quote_in_v2] and the amounts. The user's quote ATA is
    the leg's own output account (its setup creates it). Raises ValueError on anything that cannot
    be bought; the minimum out is `capped_min_out`, as on the SOL curve."""
    if not event.get("is_buy"):
        raise ValueError("source event is not a buy")
    if event.get("quote_mint") in SOL_QUOTE_MINTS:
        raise ValueError("curve is quoted in SOL")
    if int(event.get("real_token_reserves") or 0) <= 0:
        raise ValueError("curve is complete: no real token reserves left")
    for program in (base_token_program, quote_token_program):
        if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            raise ValueError("unknown token program %r" % program)
    expected = quote_curve_tokens_out(spend_quote_atomic, event)
    min_out, output_floor, price_floor = capped_min_out(
        expected, slippage_pct, source_price_usd=source_price_usd,
        source_cost_usdc_atomic=source_cost_usdc_atomic, source_price_cap_pct=source_price_cap_pct,
        base_decimals=base_decimals, source_average_price_usd=source_average_price_usd,
        copy_cap_pct=copy_cap_pct, same_slot=same_slot)
    return quote_curve_ixs(event, spend_quote_atomic, min_out, user=user,
                           base_token_program=base_token_program,
                           quote_token_program=quote_token_program,
                           buyback_fee_recipient=buyback_fee_recipient), {
                               "spend_lamports": int(spend_quote_atomic),
                               "expected_tokens_out": expected, "output_floor_atomic": output_floor,
                               "source_price_floor_atomic": price_floor, "min_tokens_out": min_out,
                               "source_average_price_usd": source_average_price_usd}


# --- the prefetched USDC -> SOL leg ---------------------------------------------------------------

# PREFETCH CALL TIMING (b0f346d1, 2026-10-06). The slot lane's prefetch thread sets `sink` to a dict
# (and `clock` to its own) for one extra-refresh pass; legs add their milliseconds per call. Per
# THREAD: an on-demand worker never has a sink, so it records nothing and never shares one.
PREFETCH_TIMINGS = threading.local()


@contextlib.contextmanager
def leg_metrics(*, clock=time.monotonic):
    """Collect worker subcalls without prefetch-only logging or egress telemetry."""
    missing = object()
    names = ('sink', 'clock', 'emit_slow')
    saved = {name: getattr(PREFETCH_TIMINGS, name, missing) for name in names}
    sink = {}
    PREFETCH_TIMINGS.sink, PREFETCH_TIMINGS.clock = sink, clock
    PREFETCH_TIMINGS.emit_slow = False
    try:
        yield sink
    finally:
        for name, value in saved.items():
            if value is missing:
                delattr(PREFETCH_TIMINGS, name)
            else:
                setattr(PREFETCH_TIMINGS, name, value)


@contextlib.contextmanager
def prefetch_timed(name):
    """Add this block's milliseconds to the prefetch thread's sink under `name`; nothing without one."""
    sink = getattr(PREFETCH_TIMINGS, "sink", None)
    clock = getattr(PREFETCH_TIMINGS, "clock", None) or time.monotonic
    began = clock() if sink is not None else 0.0
    try:
        yield
    finally:
        if sink is not None:
            sink[name] = sink.get(name, 0.0) + (clock() - began) * 1000.0


LEG_CALL_SLOW_MS = 500.0
LEG_CALL_LINE_EVERY_S = 10.0             # at most one slow line per host this often (Alpha)
_SLOW_LAST = {}                          # host -> [last line at, suppressed since]; prefetch thread only
_EGRESS = {}                             # host -> (src, iface, resolved at); the worker writes
_EGRESS_WORKER = {"thread": None}        # the one leg-egress daemon; prefetch thread only
_IFACES = []                             # [(address, interface)] from one `ip -o -4 addr` read


def _leg_call_egress(base):
    """(source address, interface, age in s) the kernel routes `base`'s host through, from the cache,
    or ("unknown", "unknown", None). NEVER BLOCKS (SOL-2, Codex review 2026-10-07): the resolve
    (getaddrinfo, which has no timeout, plus a UDP route probe and one `ip addr`) ran inside the
    slow line's finally and held a finished leg unpublished on a stalled DNS. A missing or stale
    (> 300 s) entry now starts ONE leg-egress daemon to refresh it; a hung resolver leaves that one
    thread stuck and the facts unknown. Prefetch thread only (finding ca32c4b6)."""
    import threading
    from urllib.parse import urlparse
    try:
        host = urlparse(str(base)).hostname
    except (TypeError, ValueError):
        return "unknown", "unknown", None
    hit = _EGRESS.get(host)
    if host and (hit is None or time.monotonic() - hit[2] > 300.0):
        worker = _EGRESS_WORKER.get("thread")
        if worker is None or not worker.is_alive():
            worker = threading.Thread(target=_egress_resolve, args=(host,), name="leg-egress", daemon=True)
            _EGRESS_WORKER["thread"] = worker
            worker.start()
    if hit is None:
        return "unknown", "unknown", None
    return hit[0], hit[1], round(time.monotonic() - hit[2], 1)


def _egress_resolve(host):
    """The leg-egress daemon: resolve `host`, probe its route (a UDP connect sends nothing), name
    the interface. Any failure leaves the cache as it was."""
    import socket
    import subprocess
    try:
        ip = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((ip, 443))
            src = probe.getsockname()[0]
    except (OSError, IndexError, TypeError, ValueError):
        return
    if not _IFACES:
        try:
            out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True,
                                 timeout=2).stdout
            _IFACES.extend((f[3].split("/")[0], f[1]) for f in (l.split() for l in out.splitlines())
                           if len(f) > 3)
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    _EGRESS[host] = (src, next((name for address, name in _IFACES if address == src), "unknown"),
                     time.monotonic())


@contextlib.contextmanager
def _leg_call(name):
    """prefetch_timed, plus ONE warning line for a call over LEG_CALL_SLOW_MS on the prefetch thread:
    the call, its ms, the Jupiter host that answered, the HTTP status (or the exception), how many
    hosts it tried, the response size and the route. Yields a dict the caller fills with the result."""
    sink = getattr(PREFETCH_TIMINGS, "sink", None)
    clock = getattr(PREFETCH_TIMINGS, "clock", None) or time.monotonic
    began, out = clock(), {}
    try:
        with prefetch_timed(name):
            yield out
    except Exception as exc:
        out["status"] = "exception:%s" % type(exc).__name__
        raise
    finally:
        ms = (clock() - began) * 1000.0
        if sink is not None and getattr(PREFETCH_TIMINGS, "emit_slow", True) and ms > LEG_CALL_SLOW_MS:
            try:
                from sniper.execution import solana_real_seams as seams
                base = getattr(seams._JUP_LAST, "base", None)
                last = _SLOW_LAST.setdefault(base, [None, 0])
                now = clock()
                # never `return` here: in a finally it would swallow the call's own exception
                if last[0] is not None and now - last[0] < LEG_CALL_LINE_EVERY_S:
                    last[1] += 1                            # held back; the next line counts it
                else:
                    suppressed, last[:] = last[1], [now, 0]
                    src, iface, egress_age = _leg_call_egress(base)
                    logging.getLogger(__name__).warning(
                        "[memecoin] slot lane leg call slow call=%s ms=%.0f host=%s status=%s tries=%s "
                        "bytes=%s src=%s iface=%s egress_age_s=%s suppressed=%d", name, ms, base,
                        out.get("status"), getattr(seams._JUP_LAST, "tries", None), out.get("bytes"),
                        src, iface, egress_age, suppressed)
            except Exception:  # noqa: BLE001 - telemetry never fails the refresh
                logging.getLogger(__name__).exception("slot lane leg call line failed")


def _payload_bytes(payload):
    try:
        return len(json.dumps(payload))
    except (TypeError, ValueError):
        return None


def prefetch_usdc_sol_leg(usdc_atomic, slippage_bps, *, user, jup_call, rpc, alt_cache=None,
                          quote_params=DEFAULT_LEG_QUOTE_PARAMS, clock=time.monotonic, unwrap=True,
                          output_mint=WSOL_MINT):
    """Quote + /swap-instructions for USDC -> SOL, unwrapped to lamports, with its lookup tables.

    Runs OFF the critical path (the caller refreshes it every ~2 s for the clip). Its minimum
    lamports out is what the curve buy spends. `alt_cache` maps table address -> account and is
    filled here, so a refresh with the same tables costs no RPC. `quote_params` is appended to the
    /quote query (e.g. "&dexes=..." or "&onlyDirectRoutes=true") so the caller can pin a route
    that fits the packet. Returns None on any failure."""
    # `output_mint` other than wSOL (2026-09-27, the CPMM ZEC leg): the output stays in the user's
    # token account for that mint, and the leg names its mint so no builder spends it on another pool.
    path = ("/quote?inputMint=%s&outputMint=%s&amount=%d&slippageBps=%d%s"
            % (USDC_MINT, output_mint, int(usdc_atomic), int(slippage_bps), quote_params or ""))
    with _leg_call("quote") as call:
        status, quote = jup_call("GET", path)
        call["status"] = status
        if (getattr(PREFETCH_TIMINGS, "sink", None) is not None
                and getattr(PREFETCH_TIMINGS, "emit_slow", True)):
            call["bytes"] = _payload_bytes(quote)
    if status != 200 or not isinstance(quote, dict) or not quote.get("otherAmountThreshold"):
        return None
    from sniper.execution.entry_attribution import public_quote
    with _leg_call("swap_instructions") as call:
        status, built = jup_call("POST", "/swap-instructions", body=json.dumps({
            "quoteResponse": public_quote(quote), "userPublicKey": str(user),
            # unwrap=True lands lamports (the curve spends native SOL); unwrap=False leaves wSOL in
            # the user's wSOL ATA, which is what a PumpSwap pool debits.
            "wrapAndUnwrapSol": bool(unwrap) and output_mint == WSOL_MINT,
            "dynamicComputeUnitLimit": False}))
        call["status"] = status
        if (getattr(PREFETCH_TIMINGS, "sink", None) is not None
                and getattr(PREFETCH_TIMINGS, "emit_slow", True)):
            call["bytes"] = _payload_bytes(built)
    if status != 200 or not isinstance(built, dict) or not built.get("swapInstruction"):
        return None
    with prefetch_timed("instruction_parse"):
        ixs = [instruction_from_json(i) for i in built.get("setupInstructions") or ()]
        ixs.append(instruction_from_json(built["swapInstruction"]))
        if built.get("cleanupInstruction"):
            ixs.append(instruction_from_json(built["cleanupInstruction"]))
    cache = alt_cache if alt_cache is not None else {}
    keys = built.get("addressLookupTableAddresses") or ()
    missing = list(dict.fromkeys(key for key in keys if key not in cache))
    fetched = {}
    for start in range(0, len(missing), 100):
        batch = missing[start:start + 100]
        single = len(batch) == 1
        with prefetch_timed("alt_rpc"):
            result = _rpc_result(rpc, "getAccountInfo" if single else "getMultipleAccounts",
                                 [batch[0] if single else batch, {"encoding": "base64"}])
        if not isinstance(result, dict):
            return None
        accounts = [result.get("value")] if single else result.get("value")
        if not isinstance(accounts, list) or len(accounts) != len(batch):
            return None
        with prefetch_timed("alt_parse"):
            for key, acc in zip(batch, accounts):
                data = acc.get("data") if isinstance(acc, dict) else None
                if not isinstance(data, (list, tuple)) or not data or not isinstance(data[0], str):
                    return None
                try:
                    fetched[key] = lookup_table_from_account(key, data[0])
                except (ValueError, TypeError):
                    return None
    # Publish only after every requested account has decoded, including later bounded batches.
    cache.update(fetched)
    tables = [cache[key] for key in keys]
    return {"ixs": ixs, "tables": tables, "usdc_atomic": int(usdc_atomic),
            "min_out_lamports": int(quote["otherAmountThreshold"]),
            "out_lamports": int(quote["outAmount"]), "fetched_at": clock(), "quote": quote,
            "output_mint": output_mint}


# --- assembly -----------------------------------------------------------------------------------

def build_curve_buy_message(event, leg, *, user, token_program, buyback_fee_recipient, blockhash,
                            slippage_pct, compute_unit_limit=None,
                            priority_fee_microlamports=None, source_price_usd=None,
                            source_price_cap_pct=None, tip=None, whole_average_usd=EVENT_AVERAGE,
                            copy_cap_pct=None, v3=False, same_slot=False):
    """UNSIGNED MessageV0 (payer = user) for leg + curve buy, and the amounts. `tip` = (tip account,
    lamports) appends the Sender A/B tip transfer last (solana_sender_ab).

    `leg` is a `prefetch_usdc_sol_leg` result; `blockhash` a cached recent blockhash string. The
    packet check signs with a NullSigner (64 zero bytes), so the size is the live size. Raises
    ValueError when the curve cannot be bought or the transaction would not fit a packet; the
    caller treats that as a refusal, never a retry with other numbers."""
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.null_signer import NullSigner
    from solders.transaction import VersionedTransaction
    buy_ixs, amounts = curve_buy_instructions(
        event, leg["min_out_lamports"], slippage_pct, user=user, token_program=token_program,
        buyback_fee_recipient=buyback_fee_recipient, source_price_usd=source_price_usd,
        source_cost_usdc_atomic=leg["usdc_atomic"], source_price_cap_pct=source_price_cap_pct,
        whole_average_usd=whole_average_usd, copy_cap_pct=copy_cap_pct, v3=v3, same_slot=same_slot)
    ixs = [set_compute_unit_limit(int(compute_unit_limit or DEFAULT_COMPUTE_UNIT_LIMIT)),
           set_compute_unit_price(int(DEFAULT_PRIORITY_FEE_MICROLAMPORTS if priority_fee_microlamports is None
                                      else priority_fee_microlamports))]
    ixs += list(leg["ixs"]) + buy_ixs
    if tip:
        from sniper.execution.solana_sender_ab import tip_instructions
        ixs += tip_instructions(user, tip)
    message = MessageV0.try_compile(_pk(user), ixs, leg["tables"], Hash.from_string(blockhash))
    size = len(bytes(VersionedTransaction(message, [NullSigner(_pk(user))])))
    if size > SOLANA_PACKET_LIMIT:
        raise ValueError("transaction is %d bytes, over the %d-byte packet" % (size, SOLANA_PACKET_LIMIT))
    return message, {"bytes": size, **amounts}


def build_curve_buy(event, leg, *, signers, **kw):
    """Signed VersionedTransaction bytes (a Keypair live, a NullSigner to simulate), for the
    harness and for simulation. Returns {"tx": bytes, "bytes": int, **amounts}."""
    from solders.transaction import VersionedTransaction
    message, info = build_curve_buy_message(event, leg, **kw)
    raw = bytes(VersionedTransaction(message, list(signers)))
    return {"tx": raw, **info, "bytes": len(raw)}


# --- the slot-lane seam -------------------------------------------------------------------------

DEFAULT_LEG_SLIPPAGE_BPS = 150
# The label the leg's Jupiter calls are COUNTED under (JUPITER_LOAD by_role and the per-minute
# memecoin_jupiter_load sink). Counting only: the calls still spend the funded key at funded
# priority. Before 2026-09-27 they were counted nowhere, ~2-4 calls/s by the code's own triggers.
SLOT_LEG_ROLE = "slot_leg"
LEG_REFRESH_AFTER_S = 1.0
RECIPIENT_REREAD_S = 300.0   # fee-recipient re-read cadence (Gamma LOW 2026-10-08: Pump may rotate them)


def keep_fee_config_fresh(builder, read):
    """Prefetch thread. `read()` makes one config-account read and sets the builder's fee recipients.
    The first read stays inline (retried each refresh while none is held); after it, one daemon thread
    re-reads every RECIPIENT_REREAD_S, never more than one in flight, so a hanging read (10 s HTTP
    timeout) never stalls the leg refresh past MAX_LEG_AGE_S. A failed re-read keeps the pair held."""
    if builder._buyback is None:
        read()
        builder._config_read_at = builder.clock()
        return
    if builder._config_read_at is None:          # a pair set from outside starts the interval now
        builder._config_read_at = builder.clock()
    if builder._config_rereading or builder.clock() - builder._config_read_at < RECIPIENT_REREAD_S:
        return
    builder._config_rereading = True

    def reread():
        try:
            read()
        except Exception:
            logging.getLogger(__name__).warning("slot lane fee config re-read failed builder=%s",
                                                type(builder).__name__, exc_info=True)
        finally:
            builder._config_read_at = builder.clock()
            builder._config_rereading = False

    threading.Thread(target=reread, name="slot-fee-config", daemon=True).start()
MAX_LEG_AGE_S = 3.0
# The reservation is the authority (2026-09-27, Alpha): the leg spends at most the approved clip
# and at least this share of it. Below it the leg was sized for another regime scale (0.75 in
# production). Between is drift: the clip is min(equity/3, cap) x scale and equity carries the
# open book's MARKS, so one held coin moving 10% moves the clip ~3% between the leg's quote and
# the reservation. 0.99 would have refused that drift and fallen back at S+7 (Alpha's correction).
MIN_LEG_SHARE_OF_CLIP = 0.90


def clip_atomic(clip_usd):
    """The USDC atomic amount a clip means (6 decimals, rounded to the nearest unit)."""
    return int(round(float(clip_usd) * 1_000_000))


def leg_clip_refusal(leg, clip_usd):
    """"leg_over_clip" / "leg_under_clip" when the leg's USDC is outside [90%, 100%] of the
    approved clip, else None. Spend may never exceed the reservation, nor fall to another scale."""
    clip = clip_atomic(clip_usd)
    if leg["usdc_atomic"] > clip:
        return "leg_over_clip"
    if leg["usdc_atomic"] < clip * MIN_LEG_SHARE_OF_CLIP:
        return "leg_under_clip"
    return None


def matching_cash_leg(builder, clip_usd):
    """Read immutable prefetch snapshots only; never resize an instruction or quote here."""
    legs = getattr(builder, "_cash_legs", ()) + (builder._leg,)
    for leg in legs:
        if (leg is not None and 0 <= builder.clock() - leg["fetched_at"] <= builder.max_leg_age_s
                and leg_clip_refusal(leg, clip_usd) is None):
            return leg
    return None


def cash_leg_snapshot(builder):
    """The builder's published cash legs, for the heartbeat: {usdc_atomic: {age_s, fetched_at,
    primary}}. Reads the two published references once (whole tuples, never mutated): no lock,
    no I/O. fetched_at is the builder's monotonic stamp; age_s is now minus it."""
    # _leg FIRST, then _cash_legs (Gamma info on 7d3727dc9): publish() assigns _cash_legs before _leg,
    # so a sample between the two pairs the new tuple with the old primary; the published tuple wins
    # each size's stamp, and `primary` is only a flag (the primary's size).
    primary, now = getattr(builder, "_leg", None), builder.clock()
    legs = getattr(builder, "_cash_legs", ())
    primary_key = str(int(primary["usdc_atomic"])) if primary is not None else None
    out = {}
    for leg in ((primary,) if primary is not None else ()) + tuple(legs):
        if leg is None:
            continue
        key = str(int(leg["usdc_atomic"]))
        out[key] = {"age_s": round(now - float(leg["fetched_at"]), 3),
                    "fetched_at": round(float(leg["fetched_at"]), 3), "primary": key == primary_key}
    return out


def refresh_cash_legs(builder, clip_usd, alternate_clip_usd=None, *, unwrap=True, prefetch_fn=None, extra_clip_usds=()):
    """Prefetch-thread only. Publish full, regime and wallet-cap clip sizes as immutable snapshots.

    A failed refresh retains its old timestamp; it cannot make a stale leg fresh. The primary
    leg remains available when the secondary quote fails. Both use this builder's wrap shape.
    Funded 429s pause preparation before each size and physical dispatch; held stamps still age.
    """
    from sniper.execution.solana_real_seams import CashJupiterQuiet, cash_jupiter_preparation
    keys = tuple(dict.fromkeys(clip_atomic(c) for c in (clip_usd, alternate_clip_usd, *extra_clip_usds) if c is not None))
    held = getattr(builder, "_cash_legs", ()) + (builder._leg,)
    # PUBLISHED AS EACH SIZE COMPLETES (Codex speed audit 706f4245f, item 2): a fresh full-size leg
    # was invisible until every slower secondary size had answered or timed out. Each publication
    # carries the current plan's sizes only (others pruned), every not-yet-refreshed size with its
    # ORIGINAL stamp, and the primary compatibility field. Same requests, same checks, one writer.
    latest = {key: next((l for l in held if l is not None and l["usdc_atomic"] == key), None) for key in keys}

    def publish():
        builder._cash_legs = tuple(latest[key] for key in keys if latest[key] is not None)
        builder._leg = latest[keys[0]] if keys else None

    updated = []
    for key in keys:
        leg = latest[key]
        if leg is None or builder.clock() - leg["fetched_at"] >= builder.refresh_after_s:
            try:
                with cash_jupiter_preparation():
                    fresh = (prefetch_fn or prefetch_usdc_sol_leg)(
                        key, builder.leg_slippage_bps, user=builder.user,
                        jup_call=builder.jup_call, rpc=builder.rpc, alt_cache=builder._alt_cache,
                        clock=builder.clock, unwrap=unwrap)
            except CashJupiterQuiet:
                break
            except Exception:
                import logging
                logging.getLogger(__name__).warning("slot lane cash leg refresh failed atoms=%s", key,
                                                    exc_info=True)
                fresh = None
            if fresh is not None:
                leg = latest[key] = fresh
                publish()
        if leg is not None:
            updated.append(leg)
    publish()
    if len(updated) == 2 and not getattr(builder, "_cash_pair_logged", False):
        import logging
        logging.getLogger(__name__).info("slot lane cash pair warm builder=%s atoms=%s unwrap=%s",
                                        type(builder).__name__, list(keys), unwrap)
        builder._cash_pair_logged = True
    return matching_cash_leg(builder, clip_usd) is not None


class SlotLaneCurveBuilder:
    """The slot lane's curve builder: keeps one USDC -> SOL leg warm and builds from a SlotBuy.

    `refresh(clip_usd, snapshot)` runs on the lane's PREFETCH thread (~every 0.5 s) and re-quotes
    only when the leg is older than LEG_REFRESH_AFTER_S or the clip changed. `build` runs on the
    lane thread and makes NO network call. One writer: `self._leg` is replaced whole, never mutated,
    so the lane cannot read a half-written leg.

    Default seams are the funded ones: Jupiter under the FUNDED key role (so paper and capture
    quotes yield to it) and the custody RPC for the one-time Global read and lookup tables.
    """

    def __init__(self, *, user, jup_call=None, rpc=None, leg_slippage_bps=DEFAULT_LEG_SLIPPAGE_BPS,
                 refresh_after_s=LEG_REFRESH_AFTER_S, max_leg_age_s=MAX_LEG_AGE_S,
                 compute_unit_limit=None, priority_fee_microlamports=None, clock=time.monotonic, priority_fee_fn=None):
        self.user = str(user)
        self.jup_call = jup_call or _funded_jup_call
        self.rpc = rpc or _custody_rpc
        self.leg_slippage_bps = int(leg_slippage_bps)
        self.refresh_after_s, self.max_leg_age_s = float(refresh_after_s), float(max_leg_age_s)
        self.compute_unit_limit = int(compute_unit_limit or DEFAULT_COMPUTE_UNIT_LIMIT)
        self._cu_explicit = compute_unit_limit is not None
        self.priority_fee_microlamports = priority_fee_microlamports
        self.priority_fee_fn = priority_fee_fn
        self.clock = clock
        self._leg = None
        self._alt_cache = {}
        self._buyback = None
        self._fee_config = None          # immutable (normal recipient, Mayhem recipient, buyback)
        self._config_read_at, self._config_rereading = None, False
        self.last_build = None           # amounts and size of the newest build, for the trace
        # A curve quoted in another token: mint -> (monotonic read, jsonParsed value), written by
        # the on-demand worker's threads (ondemand_quote), read by the lane thread's build. Each
        # entry is replaced whole under the lock.
        self._mints = {}
        self._mint_lock = threading.Lock()
        # The pump lookup table (PUMP_ALT_STATE): read on the prefetch thread, replaced whole once.
        self.pump_alt = None
        self.pump_alt_path = None            # None = the state path
        self._pump_alt_next, self._pump_alt_done = 0.0, False

    REASON_PREFIX = "slot_lane_curve"
    # The quote leg's Jupiter accounts cap. The 27-account buy leaves little of the packet: on STC
    # BiR7qY3f (2026-10-07) a 1-hop USDC -> WBTC leg (15 accounts) built 1,220 bytes, a 2-hop leg
    # 1,446. The worker also assembles the transaction before the lane reserves (ondemand_fits).
    ONDEMAND_QUOTE_PARAMS = "&maxAccounts=20"

    @property
    def read_rpc(self):
        """The on-demand worker's reads (quote mint re-check, leg lookup tables): the custody RPC."""
        return self.rpc

    def _mint_value(self, mint):
        with self._mint_lock:
            return self._mints.get(str(mint))

    def ondemand_quote(self, buy):
        """(quote mint, quote facts (decimals, fee bps, fee max) or None, refusal or None) for the
        on-demand leg worker, on the WORKER's thread. One getMultipleAccounts reads the quote mint and
        the coin, re-read after QUOTE_RECHECK_S; the coin must be a Token or Token-2022 mint every
        extension of which the builds cover, with no transfer fee."""
        from sniper.execution.raydium_cpmm_buy import epoch_now, quote_facts
        from sniper.execution.raydium_launchlab_buy import QUOTE_RECHECK_S
        try:
            event = parse_trade_event(getattr(buy, "trade_event_log", None) or "")
        except ValueError:
            return None, None, "slot_lane_event_unreadable"
        quote_mint, mint = event.get("quote_mint"), event.get("mint")
        if quote_mint in SOL_QUOTE_MINTS or not quote_mint:
            return quote_mint, None, self.REASON_PREFIX + "_quote_is_sol"
        if mint != getattr(buy, "mint", None):
            return quote_mint, None, "event_mint_mismatch"
        now = self.clock()
        stale = [m for m, seen in ((m, self._mint_value(m)) for m in (quote_mint, mint))
                 if seen is None or now - seen[0] >= QUOTE_RECHECK_S]
        if stale:
            try:
                values = (_rpc_result(self.rpc, "getMultipleAccounts", [
                    stale, {"encoding": "jsonParsed", "commitment": "confirmed"}]) or {}).get("value") or []
            except Exception:
                return quote_mint, None, self.REASON_PREFIX + "_quote_unread"
            if len(values) != len(stale) or any(v is None for v in values):
                return quote_mint, None, self.REASON_PREFIX + "_quote_unread"
            with self._mint_lock:
                for m, value in zip(stale, values):
                    self._mints[m] = (now, value)
        coin = self.coin_facts(mint)
        if coin is None:
            return quote_mint, None, self.REASON_PREFIX + "_coin_unmovable"
        return quote_mint, quote_facts(self._mint_value(quote_mint)[1], epoch=epoch_now()), None

    def ondemand_fits(self, buy, leg):
        """Whether leg + coin ATA + buy (no tip; the lane drops a tip that overflows) fits one
        packet, assembled with placeholder amounts (sizes do not depend on them). Worker thread."""
        try:
            event = parse_trade_event(getattr(buy, "trade_event_log", None) or "")
            event, buyback = self._resolved_fee_event(event)
            coin = self.coin_facts(event["mint"])
            seen = self._mint_value(event["quote_mint"])
            program = ((seen or (None, {}))[1] or {}).get("owner")
            if coin is None or program is None or buyback is None:
                return False
            buy_ixs = quote_curve_ixs(event, max(1, int(leg["min_out_lamports"])), 1, user=self.user,
                                      base_token_program=coin["token_program"],
                                      quote_token_program=program, buyback_fee_recipient=buyback)
            _assemble(self.user, list(leg["ixs"]) + buy_ixs, self._tables(leg), "1" * 32,
                      DEFAULT_COMPUTE_UNIT_LIMIT, DEFAULT_PRIORITY_FEE_MICROLAMPORTS, None)
        except (ValueError, KeyError, TypeError):
            return False
        return True

    def ondemand_facts(self, buy):
        """The worker's quote-safety snapshot: the quote mint's raw value and the start of its read,
        so its live re-check reuses this read inside QUOTE_RECHECK_S (as CPMM and LaunchLab)."""
        try:
            quote_mint = parse_trade_event(getattr(buy, "trade_event_log", None) or "")["quote_mint"]
        except (ValueError, KeyError):
            return None
        seen = self._mint_value(quote_mint)
        return None if seen is None else {"quote_read_at": seen[0], "mint_values": {quote_mint: seen[1]}}

    def coin_facts(self, mint):
        """{"token_program", "decimals"} for a coin ondemand_quote read and accepted, else None."""
        from sniper.execution.raydium_cpmm_buy import coin_facts
        seen = self._mint_value(mint)
        value = seen[1] if seen else None
        facts = coin_facts(value) if value else None
        program = (value or {}).get("owner")
        if facts is None or facts[2] or facts[3] or program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            return None
        return {"token_program": program, "decimals": int(facts[1])}

    def _refresh_pump_alt(self):
        """Prefetch thread: load the pump lookup table once. No state file yet, or a failed read,
        retries every PUMP_ALT_RETRY_S (the operator may create it while the runner is up); a table
        that fails verification is logged once and never used."""
        import logging
        if self._pump_alt_done or self.clock() < self._pump_alt_next:
            return
        self._pump_alt_next = self.clock() + PUMP_ALT_RETRY_S
        log = logging.getLogger(__name__)
        path = self.pump_alt_path
        if path is None:
            from sniper.util.state_paths import state_path
            path = str(state_path(*PUMP_ALT_STATE))
        try:
            with open(path) as fh:
                state = json.load(fh)
            address = state["address"]
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("slot lane pump lookup table state unreadable path=%s; not used", path,
                        exc_info=True)
            self._pump_alt_done = True
            return
        if state.get("status") != "active":
            return                           # the operator's script has not finished: try later
        try:
            account = (_rpc_result(self.rpc, "getAccountInfo", [
                str(address), {"encoding": "base64", "commitment": "confirmed"}]) or {}).get("value")
        except Exception:
            log.warning("slot lane pump lookup table read failed; retrying", exc_info=True)
            return
        table, why = verified_pump_alt(address, account, self.user)
        self._pump_alt_done = True
        if table is None:
            log.warning("slot lane pump lookup table %s not used: %s", address, why)
            return
        self.pump_alt = table
        # warning: the runner's root level is WARNING, so an info line never showed the pickup.
        log.warning("slot lane pump lookup table %s in use (12 keys)", address)

    def _tables(self, leg):
        return list(leg["tables"]) + ([self.pump_alt] if self.pump_alt is not None else [])

    def refresh(self, clip_usd, snapshot=None, *, alternate_clip_usd=None, extra_clip_usds=()):
        """Keep the leg for `clip_usd` fresh. Returns True when a leg for that clip is held."""
        self._refresh_pump_alt()
        keep_fee_config_fresh(self, self._read_global)
        return refresh_cash_legs(self, clip_usd, alternate_clip_usd, extra_clip_usds=extra_clip_usds)

    def _read_global(self):
        from solders.pubkey import Pubkey
        acc = (_rpc_result(self.rpc, "getAccountInfo", [
            str(_pda([b"global"], PUMP_PROGRAM)), {"encoding": "base64"}]) or {}).get("value")
        if not acc:
            return
        try:
            if (not isinstance(acc, dict) or acc.get("owner") != PUMP_PROGRAM
                    or acc.get("executable") is not False):
                raise ValueError("invalid Global owner or executable flag")
            if acc["data"][1] != "base64":
                raise ValueError("invalid Global encoding")
            raw = base64.b64decode(acc["data"][0], validate=True)
            if (len(raw) < GLOBAL_BUYBACK_OFFSET + 32 or raw[:8] != GLOBAL_DISCRIMINATOR
                    or raw[8] != 1):
                raise ValueError("invalid Global data")
            recipients = tuple(str(Pubkey.from_bytes(raw[offset:offset + 32])) for offset in (
                GLOBAL_FEE_RECIPIENT_OFFSET, GLOBAL_RESERVED_FEE_RECIPIENT_OFFSET, GLOBAL_BUYBACK_OFFSET))
            if SYSTEM_PROGRAM in recipients:
                raise ValueError("unset Global fee recipient")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logging.getLogger(__name__).warning("slot lane Global fee config refused: %s", exc)
            return
        self._buyback = recipients[2]      # shared refresh helper's readiness flag
        self._fee_config = recipients
        # Global.creator_fee_configurable (byte 1045, as pump_native_exit reads it): the desk's native SOL sell
        # needs it to trust a per-curve creator fee override. None = unreadable, which the sell refuses.
        self._creator_fee_configurable = bool(raw[1045]) if len(raw) > 1045 and raw[1045] in (0, 1) else None

    def _resolved_fee_event(self, event):
        """v3 source fees stay in the curve; our v1 buy needs a Global recipient."""
        config = self._fee_config
        if event.get("fee_recipient") == SYSTEM_PROGRAM:
            mode = event.get("mayhem_mode")
            if config is None or not isinstance(mode, bool):
                raise ValueError("no_protocol_fee_recipient")
            event = dict(event, fee_recipient=config[int(mode)])
        return event, config[2] if config is not None else self._buyback

    def matching_leg(self, clip_usd):
        return matching_cash_leg(self, clip_usd)

    accepts_priority_lamports = True     # memecoin_slot_lane.dispatch_build passes the crowded fee

    def build(self, buy, clip_usd, snapshot, *, funding_lamports=None, tip=None, leg=None, quote=None,
              priority_lamports=None):
        """(unsigned MessageV0, {"blockhash", "last_valid_height"}) or (None, reason). `leg` +
        `quote` (decimals, USD, fee bps, fee max) come from the on-demand worker for a curve quoted
        in another token. `priority_lamports`: a treated crowded send's priority fee."""
        from sniper.settings import memecoin as cfg
        if (leg is not None or quote is not None
                or getattr(buy, "quote_mint", None) not in (None, *SOL_QUOTE_MINTS)):
            if funding_lamports is not None:
                return None, "native_funding_not_supported_on_a_quote_curve"
            return self._build_quote_curve(buy, clip_usd, snapshot, leg, quote, tip,
                                           priority_lamports=priority_lamports)
        if funding_lamports is None:
            leg = self.matching_leg(clip_usd) or self._leg
            if leg is None or self.clock() - leg["fetched_at"] > self.max_leg_age_s:
                return None, "leg_stale"
            refused = leg_clip_refusal(leg, clip_usd)
            if refused:
                return None, refused
        else:
            try:
                funding_lamports = int(funding_lamports)
                native_usd = funding_lamports / 1e9 * float(snapshot.sol_usd)
                if (not math.isfinite(native_usd) or funding_lamports <= 0
                        or native_usd > float(clip_usd) + 1e-9
                        or native_usd < float(clip_usd) * MIN_LEG_SHARE_OF_CLIP):
                    return None, "native_amount_outside_clip"
            except (TypeError, ValueError, OverflowError):
                return None, "native_amount_unreadable"
            leg = {"ixs": [], "tables": [], "usdc_atomic": clip_atomic(clip_usd),
                   "min_out_lamports": funding_lamports, "out_lamports": funding_lamports,
                   "quote": {}}
        if self._buyback is None:
            return None, "no_buyback_recipient"
        token_program = getattr(buy, "token_program", None)
        if not token_program:
            return None, "token_program_unknown"
        line = getattr(buy, "trade_event_log", None)
        if not line:
            return None, "no_trade_event"
        if not getattr(snapshot, "blockhash", None):
            return None, "no_blockhash"
        cap_pct = cfg.solana_wallet_chase_cap_pct(
            getattr(buy, "source_user_id", None), cfg.MEMECOIN_REAL_CHASE_MAX_PCT)
        copy_cap_pct = cfg.copy_premium_cap(getattr(buy, "source_user_id", None), "solana")[0]
        try:
            event = parse_trade_event(line)
            if event["mint"] != getattr(buy, "mint", None):
                return None, "event_mint_mismatch"
            event, buyback = self._resolved_fee_event(event)
            source_price = curve_source_price_usd(event, getattr(snapshot, "sol_usd", None))
            if source_price is None:
                return None, "source_price_unreadable"
            # Only same-transaction launch evidence proves cashback off; zero fees do not.
            v3 = getattr(buy, "curve_cashback_enabled", None) is False
            cu_limit, cu_class, cu_key = build_cu_limit(self, "pump_curve_v3" if v3 else "pump_curve",
                                                "leg" if funding_lamports is None else "native",
                                                leg=leg, token_program=token_program)
            priority_price, priority_bid = select_priority_fee(
                self, curve_route_target(event["mint"]), cu_limit, tip, priority_lamports)
            message, info = build_curve_buy_message(
                event, leg, user=self.user, token_program=token_program,
                buyback_fee_recipient=buyback, blockhash=snapshot.blockhash,
                slippage_pct=cap_pct, source_price_usd=source_price, source_price_cap_pct=cap_pct,
                compute_unit_limit=cu_limit,
                priority_fee_microlamports=priority_price, tip=tip,
                whole_average_usd=hard_cap_average(buy, PUMP_TOKEN_DECIMALS),
                copy_cap_pct=copy_cap_pct, v3=v3, same_slot=buy_same_slot(buy))
        except ValueError as exc:
            return None, str(exc)[:120]
        leg_age_s = self.clock() - leg["fetched_at"] if funding_lamports is None else None
        self.last_build = {**info, "leg_usdc_atomic": leg["usdc_atomic"], "leg_age_s": leg_age_s,
                           "priority_bid": priority_bid}
        return message, {"blockhash": snapshot.blockhash, "leg_age_s": leg_age_s, "cu_limit": cu_limit,
                         "priority_bid": priority_bid,
                         "cu_class": cu_class, "cu_key": cu_key,
                         "last_valid_height": snapshot.last_valid_height,
                         "target_mint": event["mint"],
                         "entry_slippage": {
                             "venue": "pump_curve", "program": PUMP_PROGRAM,
                             "instruction_discriminator": info["instruction_discriminator"],
                             "mint": event["mint"], "pool": curve_route_target(event["mint"]),
                             "quote_mint": WSOL_MINT, "base_token_program": token_program,
                             "quote_token_program": TOKEN_PROGRAM,
                             "spend_atomic": info["spend_lamports"],
                             "min_tokens_atomic": info["min_tokens_out"],
                             "source_sig": getattr(buy, "signature", None),
                             "source_slot": getattr(buy, "slot", None)},
                         **({"funding_asset": "sol", "funding_amount_atomic": funding_lamports}
                            if funding_lamports is not None else {}),
                         "route_target": curve_route_target(event["mint"], leg),
                         "pump_source_frame": {
                             "event_log": line,
                             "reference_basis": ("pre_execution_estimate" if isinstance(
                                 getattr(buy, "source_initiation", None), dict) and
                                 buy.source_initiation.get("payment_basis") == "pre_execution_estimate"
                                 else "source_trade_event"),
                             "source_sig": getattr(buy, "signature", None),
                             "source_slot": getattr(buy, "slot", None)},
                         "pump_price_limit": price_limit_trace(
                             info, source_price_usd=source_price, cap_pct=cap_pct,
                             copy_cap_pct=copy_cap_pct,
                             cost_usdc_atomic=leg["usdc_atomic"],
                             base_decimals=PUMP_TOKEN_DECIMALS, same_slot=buy_same_slot(buy))}


    def _build_quote_curve(self, buy, clip_usd, snapshot, leg, quote, tip, priority_lamports=None):
        """A curve quoted in another token: [compute budget, leg (USDC -> quote), coin ATA,
        buy_exact_quote_in_v2, tip]. The spend is the leg's MINIMUM out; what the leg
        returns above it stays in the quote account for the residue sweep."""
        from sniper.settings import memecoin as cfg
        line = getattr(buy, "trade_event_log", None)
        if not line:
            return None, "no_trade_event"
        try:
            event = parse_trade_event(line)
            event, buyback = self._resolved_fee_event(event)
        except ValueError as exc:
            return None, str(exc)[:120]
        if event["mint"] != getattr(buy, "mint", None):
            return None, "event_mint_mismatch"
        quote_mint = event["quote_mint"]
        coin = self.coin_facts(event["mint"])
        if coin is None:
            return None, "token_program_unknown"
        if leg is None or quote is None:
            return None, "exotic_leg_missing"
        seen = self._mint_value(quote_mint)
        quote_program = ((seen or (None, {}))[1] or {}).get("owner")
        qdec = ((((seen or (None, {}))[1] or {}).get("data") or {}).get("parsed") or {}).get(
            "info", {}).get("decimals")
        if qdec is None or quote_program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            return None, "quote_decimals_unknown"
        if leg.get("output_mint") != quote_mint:
            return None, "leg_wrong_quote"
        if self.clock() - leg["fetched_at"] > self.max_leg_age_s:
            return None, "leg_stale"
        refused = leg_clip_refusal(leg, clip_usd)
        if refused:
            return None, refused
        if int(quote[0]) != int(qdec):
            return None, "quote_decimals_mismatch"
        quote_usd = float(quote[1])
        if self._buyback is None:
            return None, "no_buyback_recipient"
        if not getattr(snapshot, "blockhash", None):
            return None, "no_blockhash"
        source_price, average = quote_curve_prices(event, qdec, quote_usd, coin["decimals"])
        if source_price is None:
            return None, "source_price_unreadable"
        whole = hard_cap_average(buy, coin["decimals"])
        if whole is not EVENT_AVERAGE:       # a routed receipt's whole average; None refuses
            try:
                average = bounded_whole_average(whole, average)
            except ValueError as exc:
                return None, str(exc)[:120]
        cap_pct = cfg.solana_wallet_chase_cap_pct(
            getattr(buy, "source_user_id", None), cfg.MEMECOIN_REAL_CHASE_MAX_PCT)
        copy_cap_pct = cfg.copy_premium_cap(getattr(buy, "source_user_id", None), "solana")[0]
        # The lane default: no measured CU class covers a quote-token curve buy yet, raised when the
        # leg's composition needs more (settings.slot_lane_exotic_cu, 2026-10-07).
        cu_limit, leg_hops, cu_estimate = ((self.compute_unit_limit, None, None) if self._cu_explicit
                                           else cfg.slot_lane_exotic_cu(leg, "pump_curve",
                                                                        cfg.MEMECOIN_SLOT_LANE_CU_LIMIT))
        try:
            buy_ixs, info = quote_curve_buy_instructions(
                event, leg["min_out_lamports"], cap_pct, user=self.user,
                base_token_program=coin["token_program"], quote_token_program=quote_program,
                buyback_fee_recipient=buyback, source_price_usd=source_price,
                source_cost_usdc_atomic=leg["usdc_atomic"], source_price_cap_pct=cap_pct,
                source_average_price_usd=average, base_decimals=coin["decimals"],
                copy_cap_pct=copy_cap_pct, same_slot=buy_same_slot(buy))
            pool = curve_route_target(event["mint"])
            priority_price, priority_bid = select_priority_fee(
                self, pool, cu_limit, tip, priority_lamports)
            message, size = _assemble(self.user, list(leg["ixs"]) + buy_ixs, self._tables(leg),
                                      snapshot.blockhash, cu_limit, priority_price, tip)
        except ValueError as exc:
            return None, str(exc)[:120]
        leg_age_s = self.clock() - leg["fetched_at"]
        info = {"bytes": size, **info}
        self.last_build = {**info, "leg_usdc_atomic": leg["usdc_atomic"], "leg_age_s": leg_age_s,
                           "priority_bid": priority_bid}
        return message, {"blockhash": snapshot.blockhash, "leg_age_s": leg_age_s, "cu_limit": cu_limit,
                         "priority_bid": priority_bid,
                         "cu_class": None, "leg_hops": leg_hops, "cu_estimate": cu_estimate,
                         "last_valid_height": snapshot.last_valid_height,
                         "target_mint": event["mint"], "quote_mint": quote_mint,
                         "min_tokens_out": info["min_tokens_out"],
                         "route_target": curve_route_target(event["mint"], leg),
                         "entry_slippage": {
                             "venue": "pump_curve", "program": PUMP_PROGRAM,
                             "instruction_discriminator": BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR.hex(),
                             "mint": event["mint"], "pool": pool,
                             "quote_mint": quote_mint, "base_token_program": coin["token_program"],
                             "quote_token_program": quote_program,
                             "spend_atomic": info["spend_lamports"],
                             "min_tokens_atomic": info["min_tokens_out"],
                             "source_sig": getattr(buy, "signature", None),
                             "source_slot": getattr(buy, "slot", None)},
                         "pump_price_limit": price_limit_trace(
                             info, source_price_usd=source_price, cap_pct=cap_pct,
                             copy_cap_pct=copy_cap_pct,
                             cost_usdc_atomic=leg["usdc_atomic"], base_decimals=coin["decimals"],
                             same_slot=buy_same_slot(buy))}


def _assemble(user, ixs, tables, blockhash, compute_unit_limit, priority_fee_microlamports, tip):
    """(UNSIGNED MessageV0, its signed size) for [compute budget, *ixs, tip]; ValueError over a packet."""
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.null_signer import NullSigner
    from solders.transaction import VersionedTransaction
    head = [set_compute_unit_limit(int(compute_unit_limit or DEFAULT_COMPUTE_UNIT_LIMIT)),
            set_compute_unit_price(int(DEFAULT_PRIORITY_FEE_MICROLAMPORTS if priority_fee_microlamports is None
                                      else priority_fee_microlamports))]
    if tip:
        from sniper.execution.solana_sender_ab import tip_instructions
        ixs = list(ixs) + list(tip_instructions(user, tip))
    message = MessageV0.try_compile(_pk(user), head + list(ixs), tables, Hash.from_string(blockhash))
    size = len(bytes(VersionedTransaction(message, [NullSigner(_pk(user))])))
    if size > SOLANA_PACKET_LIMIT:
        raise ValueError("transaction is %d bytes, over the %d-byte packet" % (size, SOLANA_PACKET_LIMIT))
    return message, size


def price_limit_trace(amounts, *, source_price_usd, cap_pct, cost_usdc_atomic, base_decimals,
                      copy_cap_pct=None, same_slot=False):
    """The signed price limit as the lane writes it to the trace (substage slot_price_limit)."""
    from sniper.settings import memecoin as cfg
    return {"source_price_usd": source_price_usd, "cap_pct": cap_pct,
            "chase_cap_pct": cap_pct, "source_average_cap_pct": copy_cap_pct,
            # the hard cap's reference; source_price_usd is the chase cap's POST-trade price
            "source_average_price_usd": amounts.get("source_average_price_usd"),
            "cost_usdc_atomic": int(cost_usdc_atomic), "base_decimals": int(base_decimals),
            "spend_lamports": amounts["spend_lamports"],
            "expected_tokens_atomic": amounts["expected_tokens_out"],
            "output_floor_atomic": amounts["output_floor_atomic"],
            "price_floor_atomic": amounts["source_price_floor_atomic"],
            "min_tokens_atomic": amounts["min_tokens_out"],
            **({"price_basis": "expected_output_slippage",
                "output_slippage_pct": output_haircut(same_slot)[0],
                "output_haircut": output_haircut(same_slot)[1]}                  # which haircut was signed: same_slot or chase
               if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY else {})}


def _funded_jup_call(method, path, body=None, *, http=None):
    from sniper.execution import solana_real_seams as seams
    try:
        # One count per HTTP call (a refresh is a /quote plus a /swap-instructions), in the total
        # AND the role, so the role partition still sums to the total.
        seams.note_role_quote(role=SLOT_LEG_ROLE)
    except Exception:  # noqa: BLE001 - a counter must never refuse the leg
        pass
    with seams.jupiter_role(seams.ROLE_FUNDED):
        return seams._jup_call(method, path, body=body, **({"http": http} if http is not None else {}))


def _custody_rpc(method, params):
    from sniper.execution.solana_real_seams import _rpc_call
    from sniper.settings import memecoin as cfg
    urls = cfg.solana_funded_rpc_urls()
    _status, body = _rpc_call(urls[0] if urls else '', method, params, None)
    if isinstance(body, dict) and "error" in body:   # an error is an error, never a null result
        raise RuntimeError("%s: %s" % (method, body["error"]))
    return (body or {}).get("result")


POOL_READ_KEEPALIVE_S = 60.0   # idle keep-alive of the pool-read connection; the learner pings inside it
POOL_READ_TIMEOUT_S = 5.0
POOL_READ_PING_TIMEOUT_S = 0.5


def keepalive_custody_rpc(rpc_url=None, *, ready_fn=None):
    """`_custody_rpc` on ONE keep-alive connection pool, for pool-fact reads (2026-10-07).

    A PumpSwap pool miss is learned by two dependent reads inside the lane's 250 ms budget. Through
    urllib each read paid a fresh TCP + TLS handshake; two of 73 misses since 2026-09-29 ran out of
    budget. Same node, same JSON, same errors: a non-200 answer, a non-JSON body or a JSON-RPC
    error raises; a null account stays None. `ping()` is a getHealth bounded by
    POOL_READ_PING_TIMEOUT_S that the idle learner uses to keep the connection open."""
    import json
    from sniper.execution.fast_http import PooledHttpClient
    if rpc_url is None:
        from sniper.settings import memecoin as cfg
        urls = cfg.solana_funded_rpc_urls()
        rpc_url = urls[0] if urls else ''
    client = PooledHttpClient(max_connections=4, max_keepalive=4, read_timeout_s=POOL_READ_TIMEOUT_S,
                              keepalive_expiry_s=POOL_READ_KEEPALIVE_S)

    def rpc(method, params, timeout=None):
        if ready_fn is not None and not ready_fn():
            raise RuntimeError('custody_identity_unverified')
        status, body, _date = client.request("POST", rpc_url, timeout=timeout,
                                             body=json.dumps({"jsonrpc": "2.0", "id": 1,
                                                              "method": method, "params": params}))
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError("%s: http %s" % (method, status))
        if "error" in body:                    # an error is an error, never a null result
            raise RuntimeError("%s: %s" % (method, body["error"]))
        return body.get("result")
    rpc.ping = lambda: rpc("getHealth", [], timeout=POOL_READ_PING_TIMEOUT_S)
    return rpc


def buyback_fee_recipient_from_global(global_data_b64):
    """Global.buyback_fee_recipients[6]; any of the eight is accepted, this one is what sell_v2
    already uses. Cached by the caller and re-read every RECIPIENT_REREAD_S: an admin update rotates it."""
    from solders.pubkey import Pubkey
    raw = base64.b64decode(global_data_b64)
    return str(Pubkey.from_bytes(raw[GLOBAL_BUYBACK_OFFSET:GLOBAL_BUYBACK_OFFSET + 32]))


def simulate_curve_buy(tx_bytes, *, rpc, commitment="processed"):
    """simulateTransaction, sigVerify false, replaceRecentBlockhash true, against the PROCESSED bank
    by default. The node's default bank is ~30 slots behind processed, so a slot guard built for
    the current head fails its lower bound there (seen 2026-09-26: 3 of 3, custom 6001 at ix 2)."""
    sim = _rpc_result(rpc, "simulateTransaction", [base64.b64encode(tx_bytes).decode(), {
        "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
        "commitment": commitment}]) or {}
    return sim.get("value") if isinstance(sim, dict) else None
