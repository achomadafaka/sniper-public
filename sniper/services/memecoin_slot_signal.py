"""Slot-lane SIGNAL: armed owners' pump.fun buys from free `processed` logs, with the source slot.

Spec: docs/superpowers/specs/2026-09-26-solana-slot-lane-design.md (step 1). One asyncio WebSocket
on its own daemon thread, `logsSubscribe` at `processed`, one owner address per subscription. A
matching frame becomes a frozen SlotBuy passed to `on_buy` ON THIS THREAD; the lane queues it. No
DB access and no RPC on the frame path: the roster comes from the caller's `owners_fn`, re-read
every 5 s in a worker thread and diffed into sub/unsub calls.

Layouts are the official IDLs (github.com/pump-fun/pump-public-docs idl/pump.json TradeEvent,
idl/pump_amm.json BuyEvent), checked on real frames captured 2026-09-26
(tests/fixtures/memecoin/slot_signal_frames.json):
  * pump curve TradeEvent reserves are POST-trade: the curve token account's post-balance minus
    the 206.9e12 migration reserve equals real_token_reserves exactly; the pre-balance does not.
  * Pump AMM BuyEvent pool reserves are PRE-trade: the next event in the same transaction carries
    base - base_amount_out and quote + quote_amount_in + lp_fee. The event names the POOL, never
    the mint or the pool's quote mint, so a pump_amm SlotBuy has mint=None.
  * S = notification context.slot, which equalled the confirmed transaction slot on 3 of 3
    captured frames. A processed frame can still come from a fork that is later dropped.
  * The token program is read one level INSIDE the pump CPI (pump_curve_buy.token_program_from_logs):
    on the captured curve buy the transaction's first token invoke is legacy Tokenkeg (an ATA
    create) while the pump CPI moves the coin with Token-2022.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, replace, field
from typing import Callable, Optional

from sniper.research.memecoin_opportunity.decoders import BorshReader, anchor_event_bytes, base58_encode
from sniper.services.memecoin_slot_skips import note as _skip_note   # fill timing (2026-10-02)
from sniper.services.memecoin_opportunity_live import parse_solana_log_notification, program_data_logs

logger = logging.getLogger("memecoin")

# The newest Solana head slot this process's slot signal has seen, and its monotonic time. The
# runner reads it when it writes an attempt row, so an on-chain fill gets recv-S in slots like the
# slot lane (fill timing, 2026-10-02). Written on slot notifications only, never on a buy.
HEAD = {"slot": None, "mono": None}

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
_TRADE = bytes([189, 219, 127, 211, 78, 230, 97, 238])
_CREATE = bytes([27, 114, 169, 77, 222, 235, 99, 118])
_AMM_BUY = bytes([103, 244, 82, 31, 44, 245, 119, 119])
_AMM_SELL = bytes([62, 47, 55, 10, 165, 3, 220, 42])   # anchor "event:SellEvent"
_SLOT_SUB_ID = 999_999_999  # request id of the slotSubscribe; owner requests count up from 1
ROSTER_REFRESH_S = 5.0   # owners_fn is the lane's cached dict; the lane refreshes it every 10 s


@dataclass(frozen=True)
class SlotBuy:
    venue: str                    # "pump_curve" | "pump_amm"
    source_user_id: str
    handle: Optional[str]
    owner: str
    signature: str
    slot: int                     # notification context.slot == the transaction's slot
    mint: Optional[str]           # None on pump_amm: its event names only the pool
    pool: Optional[str]           # pump_amm pool; None on the curve
    sol_amount: int               # lamports into the curve/pool, net of fees
    token_amount: int             # token atomic units received
    virtual_sol_reserves: Optional[int]    # curve only, POST-trade
    virtual_token_reserves: Optional[int]  # curve only, POST-trade
    real_sol_reserves: int        # curve: real reserves POST-trade; pump_amm: pool quote PRE-trade
    real_token_reserves: int      # curve: real reserves POST-trade; pump_amm: pool base PRE-trade
    token_program: Optional[str]  # curve: pump_curve_buy.token_program_from_logs; None = unknown
    trade_event_log: str          # the raw "Program data: ..." event line (fees, creator, quote mint)
    recv_monotonic: float         # time.monotonic() at socket receipt, before any decode
    recv_utc: str                 # corrected UTC ISO at receipt
    # The processed head this socket had seen when the frame arrived (slotNotification max) and
    # the monotonic time it was seen. None = no slot reading yet, never 0 (2026-09-26).
    head_at_recv: Optional[int] = None
    head_at_recv_mono: Optional[float] = None
    # Set when the frame is the owner's but cannot be copied as one buy (two own buy events): the
    # lane rejects it with this reason on its own decision line instead of never seeing it.
    refusal: Optional[str] = None
    # A non-pump venue's own swap event (raydium_cpmm: raydium_cpmm_buy.CpmmSwap, raydium_launchlab:
    # raydium_launchlab_buy.LaunchLabTrade), None on pump.
    venue_event: Optional[object] = None
    # The RoutedBuy this buy was converted from (CPMM, LaunchLab), None otherwise: the owner's own
    # USDC spend prices an exotic quote asset, and a live refusal writes its routed row from it.
    routed: Optional[object] = None
    # The quote asset the venue buy spends (CPMM: the swap's input mint; LaunchLab: the pool's quote
    # mint when the pool is cached at conversion), None = unknown. Decides the per-quote live lists.
    quote_mint: Optional[str] = None
    # Set only on an exotic-quote buy the on-demand worker hands back LIVE (memecoin_slot_lane
    # exotic quotes): {"leg", "quote": (decimals, usd, fee bps, fee max), "quote_mint", "leg_ms",
    # "quote_usd_paid", "requeued_mono"}. Not part of equality or the hash.
    ondemand: Optional[dict] = field(default=None, compare=False)
    # The confirmed watcher's exact signer/payment proof on a resumed routed buy.
    source_initiation: Optional[dict] = field(default=None, compare=False)
    # Set only on a curve buy the graduation watcher hands back LIVE (memecoin_slot_graduation): the
    # canonical PumpSwap pool it found, its reserves and fees, and the watch's timings. The buy itself
    # (the SOURCE's curve TradeEvent) still prices the caps. Not part of equality or the hash.
    graduation: Optional[dict] = field(default=None, compare=False)
    receipt_evidence: Optional[object] = field(default=None, compare=False)
    signal_feed: str = 'helius'
    proof_ready_monotonic: Optional[float] = field(default=None, compare=False)
    proof_provenance: Optional[str] = field(default=None, compare=False)
    decoded_monotonic: Optional[float] = field(default=None, compare=False)
    source_authorized: Optional[bool] = field(default=None, compare=False)
    # A two-leg PumpSwap chain refused by the decoder (CHAIN_REFUSAL): its (leg 1 pool, leg 2 pool), for the
    # lane to name the hop from its pool cache. None otherwise. Not part of equality or the hash.
    legs: Optional[tuple] = field(default=None, compare=False)
    # A rabbit-led fire (memecoin_rabbit_signal, MEMECOIN_SLOT_LANE_RABBIT_BUY): the frame's facts the
    # attempt row records (signature_basis, route, read_ms, read_slot, ...); None on every other buy.
    rabbit: Optional[dict] = field(default=None, compare=False)
    # Exact pre-buy token amount in source-owned transaction-touched accounts of this mint.
    # Raw PumpSwap logs cover only its participating base account. Neither is owner-wide inventory;
    # None means unknown. Rabbit's synthetic holder zero is not evidence for this field.
    source_pre_atomic: Optional[int] = None
    # Only a matching prior CreateEvent in this transaction proves the curve's cashback flag.
    curve_cashback_enabled: Optional[bool] = None


@dataclass(frozen=True)
class SlotSell:
    """The owner's own SELL of one coin in one transaction (source-sell mirror, 2026-09-27).

    Checked on 9 real source sells (tests/fixtures/memecoin/source_sell_frames.json): the curve
    TradeEvent's token_amount equals the owner's pre minus post balance exactly, and the PumpSwap
    SellEvent's user_base_token_reserves equals the owner's PRE-sell balance exactly.
    """
    venue: str                    # "pump_curve" | "pump_amm"
    source_user_id: str
    handle: Optional[str]
    owner: str
    signature: str
    slot: int
    mint: Optional[str]           # None on pump_amm: its event names only the pool
    pool: Optional[str]           # pump_amm pool; None on the curve
    sold_atomic: int              # token atomic units the owner sold in this transaction
    pre_atomic: Optional[int]     # the owner's balance before it: pump_amm only; None = unknown
    recv_monotonic: float
    recv_utc: str


def _curve_sell(raw, owner_raw):
    r = BorshReader(raw[8:])
    mint = r.take(32)
    _sol_amount, token_amount, is_buy = r.u64(), r.u64(), r.bool()
    if is_buy or r.take(32) != owner_raw:
        return None
    return dict(venue="pump_curve", mint=base58_encode(mint), pool=None,
                sold_atomic=token_amount, pre_atomic=None)


def _amm_sell(raw, owner_raw):
    # Same head as BuyEvent: disc, i64 timestamp, 13 u64 (base_amount_in, min_quote_amount_out,
    # user_base_token_reserves, ...), pool, user.
    if len(raw) < 184 or raw[152:184] != owner_raw:
        return None
    r = BorshReader(raw[16:])
    base_in, _min_out, user_base = r.u64(), r.u64(), r.u64()
    return dict(venue="pump_amm", mint=None, pool=base58_encode(raw[120:152]),
                sold_atomic=base_in, pre_atomic=user_base)


_SELL_DECODERS = {PUMP_PROGRAM: (_TRADE, _curve_sell), PUMP_AMM_PROGRAM: (_AMM_SELL, _amm_sell)}


def decode_owner_sells(frame, owner, owner_raw, source_user_id, handle=None, *,
                       recv_monotonic, recv_utc):
    """The owner's own sells in one logsNotification, one SlotSell per coin (or pool). Pure.

    Only an event whose actor IS the subscribed owner counts, exactly as for buys. Several own sell
    events of one coin in one transaction (a split route) add up; the PumpSwap pre-balance is then
    the largest reported, which is the first event's. A body that will not parse RAISES.
    """
    parsed = parse_solana_log_notification(frame)
    if not parsed or parsed["err"] or parsed["slot"] <= 0:
        return []
    merged = {}
    for program, (disc, fn) in _SELL_DECODERS.items():
        for _index, line in program_data_logs(parsed["logs"], program):
            raw = anchor_event_bytes(line)
            hit = fn(raw, owner_raw) if raw[:8] == disc else None
            if not hit or hit["sold_atomic"] <= 0:
                continue
            key = (hit["venue"], hit["mint"] or hit["pool"])
            if key in merged:
                prev = merged[key]
                prev["sold_atomic"] += hit["sold_atomic"]
                if prev["pre_atomic"] is not None and hit["pre_atomic"] is not None:
                    prev["pre_atomic"] = max(prev["pre_atomic"], hit["pre_atomic"])
            else:
                merged[key] = hit
    return [SlotSell(source_user_id=source_user_id, handle=handle, owner=owner,
                     signature=parsed["signature"], slot=parsed["slot"],
                     recv_monotonic=recv_monotonic, recv_utc=recv_utc, **hit)
            for hit in merged.values()]


def _curve(raw, owner_raw, any_user=False):
    r = BorshReader(raw[8:])
    mint = r.take(32)
    sol_amount, token_amount, is_buy = r.u64(), r.u64(), r.bool()
    user = r.take(32)
    if not is_buy or (user != owner_raw and not any_user):
        return None
    r.i64()
    vsol, vtok, rsol, rtok = r.u64(), r.u64(), r.u64(), r.u64()
    return dict(venue="pump_curve", mint=base58_encode(mint), pool=None, sol_amount=sol_amount,
                token_amount=token_amount, virtual_sol_reserves=vsol, virtual_token_reserves=vtok,
                real_sol_reserves=rsol, real_token_reserves=rtok)


def _amm(raw, owner_raw, any_user=False):
    # disc, i64 timestamp, 13 u64 (base_amount_out .. user_quote_amount_in), pool, user, ...
    if len(raw) < 184 or (raw[152:184] != owner_raw and not any_user):
        return None
    r = BorshReader(raw[16:])
    base_out, _max_in, user_base, _uq, pool_base, pool_quote, quote_in = (r.u64() for _ in range(7))
    return dict(venue="pump_amm", mint=None, pool=base58_encode(raw[120:152]), sol_amount=quote_in,
                token_amount=base_out, virtual_sol_reserves=None, virtual_token_reserves=None,
                real_sol_reserves=pool_quote, real_token_reserves=pool_base,
                source_pre_atomic=user_base)


_DECODERS = {PUMP_PROGRAM: (_TRADE, _curve), PUMP_AMM_PROGRAM: (_AMM_BUY, _amm)}


CHAIN_REFUSAL = "multi_own_buy:chain"
TWO_POOLS_REFUSAL = "multi_own_buy:two_pools"


def _amm_accounts(line):
    """(pool, owner's base account, owner's quote account) from a PumpSwap BuyEvent line."""
    raw = anchor_event_bytes(line)
    return base58_encode(raw[120:152]), base58_encode(raw[184:216]), base58_encode(raw[216:248])


def _two_amm_refusal(found):
    """The exact refusal of two own PumpSwap buy events (Alpha 2026-10-16; 11 frames since 10-09, none on
    one pool), and the legs for the lane, from the events alone:
    - CHAIN: leg 1's base account is leg 2's quote account (SOL -> A, then A -> the coin in a pool quoted in
      A; 10 of 11). The lane names it from its pool cache (chain_refusal). Never a buy of A: the owner
      spends A inside the same transaction.
    - TWO_POOLS: both legs paid from one quote account into different base accounts (1 of 11, 2gCFS). It
      was a split route into ONE coin: the second leg's base account belonged to another wallet, which
      logs cannot show, so it is never split into two buys. The confirmed watcher values it from balances.
    - otherwise None (today's multi_own_buy:2xpump_amm)."""
    (h1, l1, _i1), (h2, l2, _i2) = sorted(found, key=lambda f: f[2])
    (pool1, base1, quote1), (pool2, base2, quote2) = _amm_accounts(l1), _amm_accounts(l2)
    if pool1 == pool2:
        return None
    if base1 == quote2:
        return CHAIN_REFUSAL, (pool1, pool2)
    if quote1 == quote2 and base1 != base2:
        return TWO_POOLS_REFUSAL, (pool1, pool2)
    return None


def decode_owner_buy(frame, owner, owner_raw, source_user_id, handle=None, *,
                     recv_monotonic, recv_utc, head_at_recv=None, head_at_recv_mono=None, receipt_evidence=None, signal_feed='helius'):
    """The owner's own buy in one logsNotification, or None. Pure; no I/O.

    Only an event whose actor IS the subscribed owner counts: an owner named as fee recipient,
    creator or co-signer of someone else's trade is not a signal. A matching discriminator whose
    body will not parse RAISES, so the caller counts a layout change instead of reading silence.

    TWO OWN BUY EVENTS ARE REFUSED OUT LOUD (2026-09-27). They used to return None, and from 03:49Z
    notdecu's 22 route buys of 823Nje... (a PumpSwap buy of the curve's quote token, then a buy on
    that non-SOL-quoted curve, via FLASHX) left no decision line at all. Such a frame now returns a
    SlotBuy carrying `refusal = "multi_own_buy:<venues>"` (the curve event's fields when there is
    one), which the lane rejects on its own log line.
    """
    from sniper.execution.pump_curve_buy import token_program_from_logs
    parsed = parse_solana_log_notification(frame)
    if not parsed or parsed["err"] or parsed["slot"] <= 0:
        return None
    found, creates = [], []
    for program, (disc, fn) in _DECODERS.items():
        for index, line in program_data_logs(parsed["logs"], program):
            raw = anchor_event_bytes(line)
            if program == PUMP_PROGRAM and raw[:8] == _CREATE:
                from sniper.research.memecoin_opportunity.pumpfun import decode_pumpfun_event
                creates.append((index, decode_pumpfun_event(program, line)))
            hit = fn(raw, owner_raw) if raw[:8] == disc else None
            if hit:
                found.append((hit, line, index))
    if not found:
        return None
    if len(found) > 1:
        from collections import Counter
        counts = Counter(h["venue"] for h, _l, _i in found)
        venues = "+".join(("%dx%s" % (n, v)) if n > 1 else v for v, n in sorted(counts.items()))
        hit, line, _index = min((f for f in found if f[0]["venue"] == "pump_curve"),
                                key=lambda f: f[2], default=found[0])
        refusal, legs = "multi_own_buy:" + venues, None
        if venues == "2xpump_amm":
            named = _two_amm_refusal(found)
            if named is not None:
                refusal, legs = named
        return SlotBuy(source_user_id=source_user_id, handle=handle, owner=owner,
                       signature=parsed["signature"], slot=parsed["slot"], token_program=None,
                       trade_event_log=line, recv_monotonic=recv_monotonic, recv_utc=recv_utc,
                       head_at_recv=head_at_recv, head_at_recv_mono=head_at_recv_mono,
                       refusal=refusal, receipt_evidence=receipt_evidence, legs=legs,
                       signal_feed=signal_feed, decoded_monotonic=time.monotonic(),
                       source_authorized=True, **hit)
    hit, line, index = found[0]
    token_program = (token_program_from_logs(parsed["logs"], index)
                     if hit["venue"] == "pump_curve" else None)
    cashback = None
    if hit["venue"] == "pump_curve" and token_program:
        prior = [event for at, event in creates if at < index]
        matching = [event for event in prior if event.token_id == hit["mint"]]
        if len(matching) == 1 and not any(event.blocker is not None for event in prior):
            from sniper.execution.pump_native_exit import _pda, _pk
            creation = matching[0]
            canonical = str(_pda([b"bonding-curve", bytes(_pk(hit["mint"]))], PUMP_PROGRAM))
            value = creation.facts.get("cashback_enabled")
            if (creation.curve_id == canonical and creation.facts.get("token_program") == token_program
                    and type(value) is bool):
                cashback = value
    return SlotBuy(source_user_id=source_user_id, handle=handle, owner=owner,
                   signature=parsed["signature"], slot=parsed["slot"], token_program=token_program,
                   trade_event_log=line, recv_monotonic=recv_monotonic, recv_utc=recv_utc,
                   head_at_recv=head_at_recv, head_at_recv_mono=head_at_recv_mono,
                   receipt_evidence=receipt_evidence, signal_feed=signal_feed,
                   decoded_monotonic=time.monotonic(), source_authorized=True,
                   curve_cashback_enabled=cashback, **hit)


# --- routed buys (2026-09-27, operator: "widen the slot lane to read routed buys") ---------------
#
# A buy routed through an aggregator (DFlow, Jupiter, a launchpad router) logs no pump event with
# the owner as actor, so decode_owner_buy cannot see it. The owner's TOKEN ACCOUNTS still change.
# The same socket carries programSubscribe on Token and Token-2022 filtered to the owner (memcmp
# owner@32, processed, jsonParsed). A USDC-paid buy is the owner's coin account rising and its USDC
# account falling in one slot, bound to the one successful transaction of the owner's that the
# slot's logs frames name. Measured 2026-09-27 on the research key over 1 h and 39 owners: 388
# notifications/h (notdecu 16/h against its 19,538 logs frames/h), arriving in the same push as
# the logs frame (p50 0 ms); 13 of 13 routed USDC buys showed both accounts in the buy's slot.

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# Never "the coin": wrapped SOL or a stable moving is a different trade or a mixed route.
_NOT_A_COIN = frozenset({USDC_MINT, "So11111111111111111111111111111111111111112",
                         "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"})
ROUTED_PROGRAMS = (("token", TOKEN_PROGRAM), ("token2022", TOKEN_2022_PROGRAM))
ROUTED_PAIR_WAIT_S = 0.4          # ~1.8 slots at 218 ms (2026-10-10) for the other half; then the watcher/J7 own it
ROUTED_BASELINE_SPACING_S = 0.5   # between baseline reads (HTTP on the socket's own key)
ROUTED_EARLY_CAP = 1024           # notifications held for one owner before its baseline lands
ROUTED_BASELINE_RETRY_S = 30.0    # a failed baseline read waits this long, doubling per failure,
ROUTED_BASELINE_RETRY_MAX_S = 1800.0  # up to this: a persistently failing owner costs ~2 reads/30 min


@dataclass(frozen=True)
class RoutedBuy:
    """One USDC-paid buy of one coin by an owner, proven from its own token accounts."""
    source_user_id: str
    handle: Optional[str]
    owner: str
    signature: str                # the one successful transaction of the owner's in `slot`
    slot: int
    mint: str
    token_delta_atomic: int       # the coin account's rise
    token_decimals: Optional[int]
    usdc_spent_atomic: int        # the USDC account's fall: gross, fees included, like the watcher
    recv_monotonic: float         # socket receipt of the pair's FIRST half
    recv_utc: str                 # corrected UTC ISO at that receipt
    paired_monotonic: float       # receipt of the half that completed the pair
    # CPMM swaps (raydium_cpmm_buy.CpmmSwap) and LaunchLab trades (raydium_launchlab_buy.
    # LaunchLabTrade) decoded from the owner's own logs frame for this slot, when their lane modes
    # are on; () otherwise.
    venue_events: tuple = ()
    paired_head_slot: Optional[int] = None  # socket head when the pair became complete
    signal_feed: str = field(default='helius', compare=False)
    receipt_evidence: Optional[object] = field(default=None, compare=False)
    # A refused direct multi-buy proves no whole route; only the confirmed receipt may resume it.
    requires_receipt_proof: bool = False
    decoded_monotonic: Optional[float] = field(default=None, compare=False)
    source_authorized: Optional[bool] = field(default=None, compare=False)
    # Shyft's compiled source CPI supplies unsigned canonical read targets, never source authority.
    damm_read_hint: Optional[object] = field(default=None, compare=False)
    # The processed transaction's signer/payment/acquisition proof, only with
    # MEMECOIN_SLOT_LANE_ROUTED_PROCESSED_PROOF (memecoin_shyft_signal.routed_buys); None = confirmed only.
    source_initiation: Optional[dict] = field(default=None, compare=False)
    # Exact pre tokens in this transaction's source-owned accounts of the mint, not owner-wide.
    # Set by validated receipt metadata; the legacy slot/account pairer leaves it unknown.
    source_pre_atomic: Optional[int] = None


def parse_token_notification(frame):
    """A jsonParsed programNotification for one SPL token account, as a dict, or None when the
    account data is not parsed token state (a closed account, base64). Shape checked on captured
    Token and Token-2022 frames (tests/fixtures/memecoin/slot_routed_token_frames.json)."""
    try:
        result = frame["params"]["result"]
        value = result["value"]
        account = value["account"]
        info = account["data"]["parsed"]["info"]
        amount = info["tokenAmount"]
        return {"slot": int(result["context"]["slot"]), "pubkey": value["pubkey"],
                "mint": info["mint"], "owner": info["owner"], "amount": int(amount["amount"]),
                "decimals": int(amount["decimals"]), "program": account["owner"]}
    except (KeyError, TypeError, ValueError):
        return None


def expiry_cause(pair):
    """Why a pair with balance movement never emitted, in _check's own order (routed-pair spec step 0,
    2026-10-07): its refusal ("other_quote", e.g. a temporary wSOL hop; "lane", the lane's own buy),
    "sold" (USDC did not fall, no coin rose, and USDC rose or a coin fell: a sell paid in USDC or SOL,
    the coin account fell or closed; a plain transfer in or out reads the same), "usdc_not_spent"
    (e.g. a SOL-paid route), "coins=<n>", "coin_fell", "no_logs", or
    "complete_unchecked" (every condition held but no event re-checked it before the wait ended)."""
    coins = [d for d in pair["coins"].values() if d]
    if pair["refused"]:
        return str(pair["refused"])
    if pair["usdc"] >= 0 and not any(d > 0 for d in coins) and (pair["usdc"] > 0 or coins):
        return "sold"
    if pair["usdc"] >= 0:
        return "usdc_not_spent"
    if len(coins) != 1:
        return "coins=%d" % len(coins)
    if coins[0] <= 0:
        return "coin_fell"
    if not pair["sigs"]:
        return "no_logs"
    return "complete_unchecked"


class RoutedPairer:
    """Pairs owners' token-account notifications with their logs frames, per (owner, slot). Pure: no
    I/O, one thread (the socket's).

    A pair emits ONE RoutedBuy when, for owner O in slot S: exactly one coin account rose, O's USDC
    fell, no other quote or stable moved, the lane did not decode O's frame in S as its own pump
    buy, and exactly one successful logs frame of O's names S. It emits on the first complete view.
    A rise is measured against the account's last known amount: the baseline read (one per token
    program) or a later notification. An account absent from a completed baseline is new, prior 0.
    An owner missing either baseline has UNKNOWN priors and emits nothing; unknown is never 0.

    Not handled, on purpose: SOL-paid routes (lamports are not a token account; the watcher and J7
    keep them); a second transaction of O's in S that arrives AFTER the pair emitted (counted as
    ambiguous_after, since the balances cannot be bound to either); proceeds or coins that land in
    another wallet.
    """

    def __init__(self, wait_s=None, stats=None, measurement=None, connection_generation=0):
        self.measurement = measurement
        self.connection_generation = connection_generation
        self.wait_s = ROUTED_PAIR_WAIT_S if wait_s is None else float(wait_s)
        self._who = {}       # owner -> (source_user_id, handle)
        self._base = {}      # (owner, kind) -> the baseline read's context slot
        self._amounts = {}   # owner -> {token account: last known atomic amount}
        self._early = {}     # owner -> [(kind, slot, account, amount)] seen before that baseline
        self._gen = {}       # owner -> bumped when held notifications overflow (see on_token)
        self._slots = {}     # (owner, slot) -> the pair being assembled
        # A caller's dict (the socket's status) keeps the counts across reconnects.
        self.stats = stats if stats is not None else {}
        for key in ("emitted", "ambiguous", "ambiguous_after", "expired"):
            self.stats.setdefault(key, 0)

    def watch(self, owner, source_user_id, handle):
        self._who[owner] = (source_user_id, handle)

    def watching(self, owner):
        return owner in self._who

    def forget(self, owner):
        self._who.pop(owner, None)
        self._amounts.pop(owner, None)
        self._early.pop(owner, None)
        for kind, _program in ROUTED_PROGRAMS:
            self._base.pop((owner, kind), None)
        for key in [k for k in self._slots if k[0] == owner]:
            del self._slots[key]

    def ready(self, owner):
        return all((owner, kind) in self._base for kind, _program in ROUTED_PROGRAMS)

    def generation(self, owner):
        """A baseline read issued at one generation must not be applied at another."""
        return self._gen.get(owner, 0)

    def set_baseline(self, owner, kind, context_slot, accounts):
        context_slot = int(context_slot)
        amounts = self._amounts.setdefault(owner, {})
        amounts.update((str(a), int(v)) for a, v in accounts.items())
        self._base[(owner, kind)] = context_slot
        keep = []
        for early in self._early.pop(owner, ()):
            if early[0] != kind:
                keep.append(early)
            elif early[1] > context_slot:
                amounts[early[2]] = early[3]      # newer than the read: that account's latest amount
        if keep:
            self._early[owner] = keep

    def on_token(self, owner, kind, slot, account, mint, amount, decimals, *, recv_monotonic,
                 recv_utc):
        if owner not in self._who:
            return []
        slot, amount = int(slot), int(amount)
        base = self._base.get((owner, kind))
        if base is None:
            early = self._early.setdefault(owner, [])
            if len(early) >= ROUTED_EARLY_CAP:
                # A dropped notification would leave a stale prior, so the owner starts over: no
                # baseline, no amounts, and a read issued before this point is refused on arrival.
                self._early.pop(owner, None)
                self._amounts.pop(owner, None)
                for k, _program in ROUTED_PROGRAMS:
                    self._base.pop((owner, k), None)
                self._gen[owner] = self.generation(owner) + 1
                _coverage_gap(self.measurement, "routed_pair", "baseline_overflow",
                    self.connection_generation, recv_monotonic, recv_monotonic)
                return []
            early.append((kind, slot, account, amount))
            return []
        if slot <= base:
            return []                             # the baseline read already holds this state
        amounts = self._amounts.setdefault(owner, {})
        delta = amount - amounts.get(account, 0)
        amounts[account] = amount
        if not delta or not self.ready(owner):
            return []
        pair = self._pair(owner, slot, recv_monotonic, recv_utc)
        if mint == USDC_MINT:
            pair["usdc"] += delta
        elif mint in _NOT_A_COIN:
            pair["refused"] = "other_quote"
        else:
            pair["coins"][mint] = pair["coins"].get(mint, 0) + delta
            pair["decimals"][mint] = decimals
        return self._check(owner, slot, recv_monotonic)

    def on_logs(self, owner, slot, signature, *, lane, recv_monotonic, recv_utc, venue_events=(),
                receipt_evidence=None, requires_receipt_proof=False):
        """One SUCCESSFUL logs frame of the owner's. `lane` = the lane decoded it as its own buy.
        `venue_events` = the frame's decoded CPMM swaps and any-actor pump buy events, carried onto
        the pair's RoutedBuy."""
        if owner not in self._who or not signature:
            return []
        pair = self._pair(owner, int(slot), recv_monotonic, recv_utc, receipt_evidence=receipt_evidence)
        pair["sigs"].add(signature)
        pair["events"].extend(venue_events)
        pair["requires_receipt_proof"] |= requires_receipt_proof
        if lane:
            pair["refused"] = "lane"
        return self._check(owner, int(slot), recv_monotonic)

    def expire(self, now):
        """Drop pairs older than the wait. Returns how many had balance movement and never paired."""
        cutoff = now - self.wait_s
        expired = 0
        for key in [k for k, p in self._slots.items() if p["first"] < cutoff]:
            pair = self._slots.pop(key)
            if pair["done"] is None and (pair["coins"] or pair["usdc"]):
                expired += 1
                why = expiry_cause(pair)
                _coverage_gap(self.measurement, "routed_pair", "pair_expired",
                    self.connection_generation, pair["first"], now)
                by = self.stats.setdefault("expired_by", {})
                by[why] = by.get(why, 0) + 1
                for sig in pair["sigs"]:
                    _skip_note(sig, "pair_expired", why)  # fill timing (2026-10-02; cause 10-07)
        self.stats["expired"] += expired
        return expired

    def _pair(self, owner, slot, mono, utc, *, receipt_evidence=None):
        pair = self._slots.get((owner, slot))
        if pair is None:
            pair = self._slots[(owner, slot)] = {
                "first": mono, "utc": utc, "usdc": 0, "coins": {}, "decimals": {}, "sigs": set(),
                "refused": None, "done": None, "events": [], "receipt_evidence": receipt_evidence,
                "requires_receipt_proof": False}
            if receipt_evidence is None and self.measurement is not None:
                try:
                    pair["receipt_evidence"] = self.measurement.identity(mono, self.connection_generation)
                except Exception:
                    pass
        return pair

    def _check(self, owner, slot, now):
        pair = self._slots[(owner, slot)]
        coins = [(m, d) for m, d in pair["coins"].items() if d]
        if (pair["refused"] or pair["usdc"] >= 0 or len(coins) != 1 or coins[0][1] <= 0
                or not pair["sigs"]):
            return []
        if pair["done"] == "emitted":
            if len(pair["sigs"]) > 1 and not pair.get("late"):
                pair["late"] = True
                self.stats["ambiguous_after"] += 1
            return []
        if pair["done"]:
            return []
        if len(pair["sigs"]) > 1:
            pair["done"] = "ambiguous"
            self.stats["ambiguous"] += 1
            for sig in pair["sigs"]:
                _skip_note(sig, "ambiguous", "%d signatures in one slot" % len(pair["sigs"]))
            return []
        pair["done"] = "emitted"
        self.stats["emitted"] += 1
        (mint, delta), = coins
        (signature,) = pair["sigs"]
        uid, handle = self._who[owner]
        return [RoutedBuy(source_user_id=uid, handle=handle, owner=owner, signature=signature,
                          slot=slot, mint=mint, token_delta_atomic=delta,
                          token_decimals=pair["decimals"].get(mint),
                          usdc_spent_atomic=-pair["usdc"], recv_monotonic=pair["first"],
                          receipt_evidence=pair.get("receipt_evidence"),
                          recv_utc=pair["utc"], paired_monotonic=now,
                          requires_receipt_proof=pair["requires_receipt_proof"],
                          decoded_monotonic=time.monotonic(), source_authorized=True,
                          venue_events=tuple(
                              e for x in pair["events"] for e in (
                                  routed_pump_buys(x.frame, True) if isinstance(x, _PumpLater)
                                  else (x,))))]


def ws_url_from_http(http_url):
    """https -> wss, http -> ws, query kept; "" on anything else."""
    url = str(http_url or "").strip()
    for http, ws in (("https://", "wss://"), ("http://", "ws://")):
        if url.startswith(http) and len(url) > len(http):
            return ws + url[len(http):]
    return ""


def _shares_funded_key(ws_url, env=os.environ):
    """True when ws_url carries the api key of the funded custody RPC (never subscribe on it)."""
    from sniper.execution.solana_rpc_pool import key_id
    from urllib.parse import parse_qs, urlsplit
    params = parse_qs(urlsplit(str(ws_url or '')).query)
    if not (params.get('api-key') or params.get('api_key')):
        return False
    funded = [env.get('MEMECOIN_SOLANA_RPC_URL') or '']
    funded += (env.get('MEMECOIN_SOLANA_RPC_POOL_URLS') or '').split(',')
    return key_id(ws_url) in {key_id(url.strip()) for url in funded if url.strip()}


def _owner_bytes(owner):
    from solders.pubkey import Pubkey
    try:
        return bytes(Pubkey.from_string(str(owner)))
    except ValueError:
        logger.warning("slot signal: malformed owner address %r skipped", owner)
        return None


def _receipt_identity(measurement, mono, generation):
    if measurement is not None:
        try:
            return measurement.identity(mono, generation)
        except Exception:
            pass
    return None


def _publish_receipt(measurement, buy, feed, generation, *, state=None):
    if measurement is None:
        return
    try:
        evidence = dict(getattr(buy, 'receipt_evidence', None) or
                        measurement.identity(buy.recv_monotonic, generation))
        routed = isinstance(buy, RoutedBuy) or bool(getattr(buy, 'refusal', None))
        graduation = (not routed and getattr(buy, 'venue', None) == 'pump_curve'
                      and buy.real_token_reserves == 0)
        stratum = ('routed_confirmed_proof' if routed else 'graduation' if graduation
                   else 'native_local_proof')
        venue = getattr(buy, 'venue', None)
        if routed:
            venues = {({'CpmmSwap': 'raydium_cpmm', 'LaunchLabTrade': 'raydium_launchlab',
                        'DammV2Seen': 'meteora_damm_v2', 'DbcSeen': 'meteora_dbc'}.get(type(event).__name__)
                       or getattr(event, 'venue', None)) for event in getattr(buy, 'venue_events', ())}
            venue = next(iter(venues)) if len(venues) == 1 else None
        paid_buy = (routed or (type(getattr(buy, 'sol_amount', None)) is int and buy.sol_amount > 0
                    and type(getattr(buy, 'token_amount', None)) is int and buy.token_amount > 0))
        evidence.update(source_authorized=getattr(buy, 'source_authorized', None),
            source_venue_authorized=evidence.get('source_venue_' + str(venue) + '_authorized'),
            source_stratum=stratum, source_buy_state='proof_pending' if routed else 'successful' if paid_buy else 'unknown',
            decoded_monotonic=getattr(buy, 'decoded_monotonic', None), slot_status='processed')
        measurement.record_source(network_id='solana', source_sig=buy.signature,
            source_wallet=buy.owner, mint=buy.mint, pool=getattr(buy, 'pool', None),
            venue=venue, feed=feed,
            recv_monotonic=buy.recv_monotonic,
            connection_generation=evidence.get('connection_generation', generation),
            roster_generation=evidence.get('roster_generation'),
            config_generation=evidence.get('config_generation'),
            decode_state=state or ('pool_unresolved' if buy.mint is None else 'decoded_buy'),
            evidence=dict(evidence, eligibility='eligibility_unknown', source_slot=buy.slot))
    except Exception:
        _coverage_gap(measurement, feed, 'receipt_publication_failed', generation,
                      buy.recv_monotonic, buy.recv_monotonic, 1)


def _coverage_gap(measurement, component, reason, generation, start, end, lost_count=None, **detail):
    """`detail` (scalars) lands in the gap row's evidence: what was dropped and why."""
    if measurement is not None:
        try:
            measurement.record_gap(component=component, reason=reason,
                connection_generation=generation, mono_start=start, mono_end=end, lost_count=lost_count,
                **detail)
        except Exception:
            pass


def _unknown_receipt(measurement, *, feed, generation, mono, state, signature=None,
                     owner=None, evidence=None):
    if measurement is not None:
        try:
            evidence = evidence or measurement.identity(mono, generation)
            measurement.record_source(network_id='solana', source_sig=signature,
                source_wallet=owner, mint=None, pool=None, venue=None, feed=feed,
                recv_monotonic=mono, connection_generation=generation,
                roster_generation=evidence.get('roster_generation'),
                config_generation=evidence.get('config_generation'), decode_state=state,
                evidence=dict(evidence, eligibility='eligibility_unknown', source_authorized=None,
                              source_venue_authorized=None, source_stratum=None, source_buy_state='unknown',
                              decoded_monotonic=None, slot_status='processed'))
        except Exception:
            _coverage_gap(measurement, feed, "receipt_publication_failed", generation, mono, mono, 1)


def _rabbit_receipt(measurement, *, feed, signature, owner, slot, mono, generation, head):
    """One owner-signed RabbitStream frame BEFORE execution, as decode_state rabbit_pre_execution: its
    arrival, slot and the processed head at that moment, never a buy. It may never execute, so no
    reader takes it for a processed or observed one (slot_status pre_execution)."""
    if measurement is not None:
        try:
            evidence = measurement.identity(mono, generation)
            measurement.record_source(network_id='solana', source_sig=signature, source_wallet=owner,
                mint=None, pool=None, venue=None, feed=feed, recv_monotonic=mono,
                connection_generation=generation, roster_generation=evidence.get('roster_generation'),
                config_generation=evidence.get('config_generation'), decode_state='rabbit_pre_execution',
                evidence=dict(evidence, slot=slot, head_at_recv=head, eligibility='eligibility_unknown',
                              source_authorized=None, source_stratum=None, source_buy_state='unknown',
                              slot_status='pre_execution'))
        except Exception:
            _coverage_gap(measurement, feed, "receipt_publication_failed", generation, mono, mono, 1)


def start_signal_thread(on_buy: Callable[[SlotBuy], None], *, owners_fn: Callable[[], dict],
                        ws_url: str, status: dict, stop: Optional[threading.Event] = None,
                        connect=None, on_sell: Optional[Callable[[SlotSell], None]] = None,
                        on_routed: Optional[Callable[[RoutedBuy], None]] = None,
                        baseline_fn: Optional[Callable[[str], dict]] = None, shyft_config=None, measurement=None,
                        infra_provider='legacy', on_migration=None, prepare_config=None, on_prepare=None, on_damm_creation=None,
                        rabbit=None, preconf=None, rabbit_sol_usd_fn=None, rabbit_tape_watch_fn=None):
    """Start the socket on a daemon thread and return the thread at once.

    owners_fn() -> {owner_address: (source_user_id, handle)}; `status` is updated in place:
    connected, subscriptions, last_frame_mono, reconnects, decode_errors. `on_sell`, when given,
    receives the owners' own sells from frames that hold no own buy, on this thread.

    `on_routed`, when given, adds two programSubscribe per owner (Token, Token-2022) on the SAME
    socket and receives each RoutedBuy on this thread. `baseline_fn(owner)` returns
    {"token": (context_slot, {account: amount}), "token2022": (...)}; it runs in a worker thread,
    one owner at a time, once both of that owner's subscriptions are acknowledged. status["routed"]
    counts it all. A refused routed subscription never touches the owner's logs subscription.
    """
    status.update(connected=False, subscriptions=0, last_frame_mono=None, reconnects=0,
                  decode_errors=0, owners=0, events=0, sells=0, last_error=None, head_slot=None,
                  head_slot_mono=None)
    if on_routed is not None:
        status["routed"] = dict(subs_ok=0, subs_refused=0, ready=0, emitted=0, ambiguous=0,
                                ambiguous_after=0, expired=0, baseline_failed=0, last_error=None)
    if infra_provider not in {'legacy', 'shyft'}:
        raise ValueError('provider_unknown')
    if infra_provider == 'shyft':
        if shyft_config is None:
            raise ValueError('shyft configuration required')
        from sniper.services.memecoin_shyft_signal import live_config
        token, urls = shyft_config
        live_config({'MEMECOIN_SOLANA_INFRA_PROVIDER': 'shyft', 'MEMECOIN_SHYFT_LIVE_ENABLED': '1',
                     'MEMECOIN_SHYFT_GRPC_TOKEN': token, 'MEMECOIN_SHYFT_GRPC_URLS': ','.join(urls)})
    elif not ws_url or _shares_funded_key(ws_url):
        raise ValueError("slot signal needs its own ws url, never the funded custody key")
    if prepare_config is not None:
        if infra_provider != 'shyft' or on_prepare is None:
            raise ValueError('rabbit preparation needs Shyft and a preparation-only callback')
        from sniper.services.memecoin_shyft_prepare import prepare_config as validate_prepare
        prepare_token, prepare_url = prepare_config
        validate_prepare({'MEMECOIN_SHYFT_PREPARE_ENABLED': '1',
            'MEMECOIN_SHYFT_PREPARE_GRPC_URL': prepare_url, 'MEMECOIN_SHYFT_GRPC_TOKEN': prepare_token})
    stop = stop or threading.Event()
    if shyft_config is not None:
        import grpc  # fail before starting any lane worker if dependencies are missing
        from sniper.services.memecoin_shyft_signal import pb
    thread = threading.Thread(target=lambda: asyncio.run(_run_signals(on_buy, owners_fn, ws_url, status,
                                                              stop, connect, on_sell, on_routed,
                                                              baseline_fn, shyft_config, measurement=measurement,
                                                              infra_provider=infra_provider,
                                                              on_migration=on_migration,
                                                              on_damm_creation=on_damm_creation,
                                                              prepare_config=prepare_config,
                                                              on_prepare=on_prepare,
                                                              rabbit=rabbit, preconf=preconf,
                                                              rabbit_sol_usd_fn=rabbit_sol_usd_fn,
                                                              rabbit_tape_watch_fn=rabbit_tape_watch_fn)),
                              name="slot-signal", daemon=True)
    thread.start()
    return thread


async def _run_signals(on_buy, owners_fn, ws_url, status, stop, connect, on_sell, on_routed,
                       baseline_fn, shyft_config, measurement=None, infra_provider='legacy', on_migration=None,
                       prepare_config=None, on_prepare=None, on_damm_creation=None, rabbit=None, preconf=None,
                       rabbit_sol_usd_fn=None, rabbit_tape_watch_fn=None):
    if infra_provider not in {'legacy', 'shyft'} or (infra_provider == 'shyft' and shyft_config is None):
        raise ValueError('signal provider configuration invalid')
    if shyft_config is None:
        return await _run(on_buy, owners_fn, ws_url, status, stop, connect, on_sell,
                          on_routed, baseline_fn, **({"measurement": measurement} if measurement is not None else {}))
    from sniper.services.memecoin_grpc_wakeup import install_grpc_wakeup_handler
    install_grpc_wakeup_handler()
    from sniper.services.memecoin_shyft_signal import run_feed
    from sniper.services.memecoin_feed_arrivals import StampedArrivals
    from urllib.parse import urlsplit
    seen = {}
    migrations_seen = {}
    sells_seen = {}
    def sell_sink(rows):
        fresh = []
        for sell in rows:
            key = sell.signature, sell.owner, sell.mint or sell.pool
            if key in sells_seen:
                continue
            sells_seen[key] = None
            fresh.append(sell)
            if len(sells_seen) > 4096:
                del sells_seen[next(iter(sells_seen))]
        if fresh:
            on_sell(fresh)
    damm_seen = {}
    def damm_sink(signature):
        if signature in damm_seen:
            return
        damm_seen[signature] = None
        if len(damm_seen) > 4096:
            del damm_seen[next(iter(damm_seen))]
        on_damm_creation(signature)
    def migration_sink(signature, pool):
        key = signature, pool
        if key in migrations_seen:
            return
        migrations_seen[key] = None
        if len(migrations_seen) > 4096:
            del migrations_seen[next(iter(migrations_seen))]
        on_migration(pool)
    refused = {}  # A diagnostic refusal cannot own the native/routed execution race.
    landing = {'watch': None,   # memecoin_rabbit_signal.LandingWatch when rabbit buys are on
               'reads': None}   # its RabbitBuyer: joins each rabbit read to the processed copy
    from sniper.services.memecoin_shyft_prepare import owner_signer
    feeds = status.setdefault('signal_feeds', {})
    token, urls = shyft_config
    from sniper.settings import memecoin as cfg
    # The RabbitStream arm (MEMECOIN_SLOT_LANE_RABBIT_SIGNAL): the existing prepare stream's owner-signed
    # pre-execution frames, on this process's clock beside the processed feeds. Off: no arm at all.
    rabbit_feed = ('rabbit:' + urlsplit(prepare_config[1]).hostname
                   if prepare_config is not None and cfg.MEMECOIN_SLOT_LANE_RABBIT_SIGNAL else None)
    # HELIUS PRECONFIRMATIONS (MEMECOIN_SLOT_LANE_PRECONF, memecoin_preconf_signal): the third feed,
    # its frames through the same owner-frame path as rabbit's. `preconf` = {"url": wss://...}.
    from sniper.services import memecoin_preconf_signal as preconf_mod
    # ONE FEED PER SOURCE (MEMECOIN_SLOT_LANE_PRECONF_SOURCES, 2026-10-17): a frame does not say its
    # source, so each source has its own socket and feed name; `preconf_feed` is the first one.
    preconf_sources = (tuple((preconf or {}).get('sources') or cfg.MEMECOIN_SLOT_LANE_PRECONF_SOURCES)
                       if preconf is not None and preconf.get('url') else ())
    preconf_feeds = tuple(preconf_mod.feed_for(source) for source in preconf_sources)
    preconf_feed = preconf_feeds[0] if preconf_feeds else None
    # Lead per signature on ONE population (StampedArrivals): every owner-signed frame stamped at
    # receipt on each feed (`raw`), paired as a buy once ANY decoder reads it as one (`buys`).
    arrivals = StampedArrivals((('helius',) if infra_provider == 'legacy' else ())
                           + tuple('shyft:' + urlsplit(url).hostname for url in urls)
                           + ((rabbit_feed,) if rabbit_feed else ())
                           + preconf_feeds)
    status['arrival_metrics'] = arrivals
    status['raw_arrival_metrics'] = arrivals.raw
    frames, preconf_frames = None, {}
    if rabbit_feed or preconf_feed:
        from sniper.services.memecoin_rabbit_signal import FrameLog
    if rabbit_feed:
        frames = FrameLog(feed=rabbit_feed, arrivals_fn=arrivals.raw.arrivals, peer=preconf_feed,
                          exclude=preconf_feeds)
    for source, feed in zip(preconf_sources, preconf_feeds):
        # The other preconf sources are pre-execution feeds too: never a processed arrival.
        preconf_frames[feed] = FrameLog(feed=feed, arrivals_fn=arrivals.raw.arrivals, label='preconf',
                                        peer=rabbit_feed, source=source,
                                        exclude=tuple(f for f in preconf_feeds if f != feed))
    prep = None
    if prepare_config is not None:
        from sniper.services.memecoin_shyft_prepare import (PendingPreparation, preparation_target,
                                                            run_prepare_feed)
        prep = PendingPreparation()
        prep_status = status['preparation'] = {'pending': prep.stats, 'feed': {}, 'callback_errors': 0}
        def prepare_sink(candidate):
            if prep.offer(candidate, now=time.monotonic()):
                try:
                    on_prepare(candidate)
                except Exception:
                    prep_status['callback_errors'] += 1
            # After the preparation hand-off (Gamma 2026-10-15: p50 3.2 us off that path).
            target = preparation_target(candidate) if rabbit_feed else None
            if target is not None and target['venue'] != 'raydium_cpmm':   # a CPMM swap names no side
                arrivals.mark_buy(candidate['signature'], candidate['owner'])
            if frames is not None and target is not None:
                frames.note(candidate['signature'], candidate['owner'], venue=target['venue'])

    def sink(name):
        stats = feeds.setdefault(name, {'wins': 0, 'duplicates': 0, 'last_signature': None})
        stats.setdefault('native_wins', 0)
        stats.setdefault('native_duplicates', 0)
        def offer(buy):
            key = (buy.signature, buy.owner)
            if key in seen or (buy.refusal and key in refused):
                stats['duplicates'] += 1
                stats['native_duplicates'] += 1
                _publish_receipt(measurement, buy, name, 0, state='duplicate_buy')
                arrivals.observe(buy.signature, buy.owner, name, buy.recv_monotonic)
                return
            claimed = refused if buy.refusal else seen
            claimed[key] = buy.recv_monotonic
            stats.update(wins=stats['wins'] + 1, last_signature=buy.signature)
            stats['native_wins'] += 1
            if landing['watch'] is not None and not buy.refusal:
                landing['watch'].observe_buy(buy.owner, buy.mint, buy.recv_monotonic,
                                             signature=buy.signature, slot=getattr(buy, 'slot', None))
            evidence = buy.receipt_evidence
            if buy.signal_feed != name or (measurement is not None and evidence is None):
                evidence = evidence or _receipt_identity(
                    measurement, buy.recv_monotonic, stats.get('connection_generation', 0))
                buy = replace(buy, signal_feed=name, receipt_evidence=evidence)
            try:
                on_buy(buy)
            finally:
                _publish_receipt(measurement, buy, name, 0)
            arrivals.observe(buy.signature, buy.owner, name, buy.recv_monotonic)
            if landing['reads'] is not None and not buy.refusal:
                try:                        # after the hand-off: memory only, never on the buy's path
                    landing['reads'].observe_processed(buy)
                except Exception:
                    logger.exception("[memecoin] slot lane rabbit read join failed sig=%s", buy.signature)
            if len(claimed) > 8192:
                for old in list(claimed)[:4096]:
                    del claimed[old]
        offer.measurement = measurement
        offer.publishes_receipts = True
        return offer

    def routed_sink(name):
        stats = feeds.setdefault(name, {'wins': 0, 'duplicates': 0, 'last_signature': None})
        stats.setdefault('routed_wins', 0)
        stats.setdefault('routed_duplicates', 0)
        def offer(buy):
            key = (buy.signature, buy.owner)
            if key in seen:
                stats['duplicates'] += 1
                stats['routed_duplicates'] += 1
                _publish_receipt(measurement, buy, name, 0, state='duplicate_buy')
                return
            seen[key] = buy.recv_monotonic
            stats.update(wins=stats['wins'] + 1, last_signature=buy.signature)
            stats['routed_wins'] += 1
            arrivals.mark_buy(buy.signature, buy.owner)
            if landing['watch'] is not None:
                landing['watch'].observe_buy(buy.owner, getattr(buy, 'mint', None), buy.recv_monotonic,
                                             signature=buy.signature, slot=getattr(buy, 'slot', None))
            evidence = buy.receipt_evidence
            if buy.signal_feed != name or (measurement is not None and evidence is None):
                evidence = evidence or _receipt_identity(
                    measurement, buy.recv_monotonic, stats.get('connection_generation', 0))
                buy = replace(buy, signal_feed=name, receipt_evidence=evidence)
            try:
                on_routed(buy)
            finally:
                _publish_receipt(measurement, buy, name, 0)
            if len(seen) > 8192:
                for old in list(seen)[:4096]:
                    del seen[old]
        offer.measurement = measurement
        offer.publishes_receipts = True
        return offer

    metadata = asyncio.Queue(maxsize=64)
    worker_status = status['metadata_worker'] = dict(capacity=64, offered=0, overflow=0,
        decoded=0, errors=0, roster_refused=0, last_error=None, metadata_refused={})
    def publish_pending(name, update, receipt, frozen, generation, signature=None):
        if generation is not None:
            from sniper.services.memecoin_shyft_signal import _publish_empty_receipt
            _publish_empty_receipt(update, measurement, feed=name, generation=generation,
                mono=receipt['recv_monotonic'], evidence=frozen, signature=signature)

    def metadata_sink(name):
        def offer(update, **receipt):
            signed = owner_signer(update, receipt.get('receipt_owners') or owners_fn())
            if signed is not None:             # the processed side of the lead, at receipt
                arrivals.stamp(signed[0], signed[1], name, receipt['recv_monotonic'])
            if landing['watch'] is not None:
                landing['watch'].observe(update, receipt['recv_monotonic'])
            pending = receipt.pop('_pending_unknown', None)
            try:
                frozen = receipt.pop('receipt_evidence', None)
                if frozen is None and measurement is not None:
                    frozen = _receipt_identity(measurement, receipt['recv_monotonic'],
                        feeds[name].get('connection_generation', 0))
                receipt_owners = receipt.pop('receipt_owners', None)
                if receipt_owners is None:
                    receipt_owners = dict(owners_fn())
                metadata.put_nowait((name, update, receipt, receipt_owners, frozen, pending))
                worker_status['offered'] += 1
            except asyncio.QueueFull:
                worker_status['overflow'] += 1
                publish_pending(name, update, receipt, frozen, pending)
                _coverage_gap(measurement, name, 'metadata_overflow',
                    pending if pending is not None else feeds[name].get('connection_generation', 0), receipt['recv_monotonic'],
                    receipt['recv_monotonic'], 1)
        return offer

    async def decode_metadata():
        from sniper.services.memecoin_shyft_signal import routed_buys
        while not stop.is_set():
            name, update, receipt, owners, frozen, pending = await metadata.get()
            buys = ()
            try:
                if prep is not None:
                    from sniper.services.memecoin_shyft_prepare import preparation_candidate
                    info = update.transaction.transaction
                    if info.HasField('meta') and info.meta.HasField('err'):
                        import base58
                        if len(info.signature) == 64:
                            prep.discard_signature(base58.b58encode(info.signature).decode(), now=time.monotonic())
                    elif info.HasField('meta'):
                        structure = preparation_candidate(update, owners,
                            recv_monotonic=receipt['recv_monotonic'],
                            generation=feeds[name].get('connection_generation', 0))
                        if structure is not None:
                            prep.take(structure, now=time.monotonic())
                if on_routed is not None:
                    why = []
                    buys = await asyncio.to_thread(routed_buys, update, owners, **receipt,
                        signal_feed=name, receipt_evidence=frozen, why=why)
                    if why:                  # bounded: fixed slugs, token tags, exception types
                        refused_by = worker_status['metadata_refused']
                        refused_by[why[0]] = refused_by.get(why[0], 0) + 1
                current = dict(owners_fn())
                for buy in buys:
                    if current.get(buy.owner) != (buy.source_user_id, buy.handle):
                        worker_status['roster_refused'] += 1
                        _publish_receipt(measurement, buy, name,
                            feeds[name].get('connection_generation', 0), state='roster_refused_unknown')
                        continue
                    worker_status['decoded'] += 1
                    routed_sink(name)(buy)
            except asyncio.CancelledError:
                _coverage_gap(measurement, name, 'metadata_cancelled',
                    pending if pending is not None else feeds[name].get('connection_generation', 0),
                    receipt['recv_monotonic'], receipt['recv_monotonic'], 1)
                raise
            except Exception as exc:
                worker_status['errors'] += 1
                worker_status['last_error'] = type(exc).__name__
                _coverage_gap(measurement, name, 'metadata_decode_error',
                    pending if pending is not None else feeds[name].get('connection_generation', 0), receipt['recv_monotonic'],
                    receipt['recv_monotonic'])
            finally:
                publish_pending(name, update, receipt, frozen, pending,
                    buys[0].signature if buys else None)
                metadata.task_done()

    legacy_keys = ('connected', 'subscriptions', 'last_frame_mono', 'reconnects', 'decode_errors',
                   'owners', 'events', 'sells', 'last_error', 'head_slot', 'head_slot_mono',
                   'requested_owners', 'acknowledged_owners', 'routed')
    tasks = []
    if infra_provider == 'legacy':
        helius_offer = sink('helius')
        helius_status = feeds['helius']
        helius_status.update({k: status[k] for k in legacy_keys if k in status})
        tasks.append(asyncio.create_task(_run(helius_offer, owners_fn, ws_url, helius_status, stop,
            connect, sell_sink if on_sell else None, routed_sink('helius') if on_routed else None, baseline_fn)))
    def update_status():
        if infra_provider == 'legacy':
            status.update({k: helius_status[k] for k in legacy_keys if k in helius_status})
            return
        wanted = tuple(sorted(owners_fn()))
        ready = [row for row in feeds.values() if row.get('connected') is True
                 and row.get('requested_owners') == row.get('acknowledged_owners') == wanted]
        status.update(connected=bool(ready), requested_owners=wanted,
            acknowledged_owners=wanted if ready else (), subscriptions=len(wanted) if ready else 0,
            owners=len(wanted), reconnects=sum(row.get('reconnects', 0) for row in feeds.values()),
            decode_errors=sum(row.get('decode_errors', 0) for row in feeds.values()),
            events=sum(row.get('decoded', 0) for row in feeds.values()),
            last_frame_mono=max((row.get('last_frame_mono') for row in ready
                                if row.get('last_frame_mono') is not None), default=None),
            last_error=None if ready else 'no_region_roster_ack')
        head = max((row for row in ready if row.get('head_slot') is not None),
                   key=lambda row: row['head_slot'], default={})
        status.update(head_slot=head.get('head_slot'), head_slot_mono=head.get('head_slot_mono'))
        if head and (HEAD['slot'] is None or head['head_slot'] > HEAD['slot']):
            HEAD.update(slot=head['head_slot'], mono=head.get('head_slot_mono'))
    if on_routed is not None or prep is not None:
        tasks.append(asyncio.create_task(decode_metadata()))
    def shyft_head():
        heads = [(f['head_slot'], f.get('head_slot_mono')) for name, f in feeds.items()
                 if name.startswith('shyft:') and type(f.get('head_slot')) is int]
        return max(heads, key=lambda h: h[0]) if heads else (None, None)
    # THE PRE-EXECUTION FIRE (needs the lane's backstop: its read client, the source_not_landed exit
    # and the breakers). ONE buyer for rabbit and preconf frames: a signature fires once. No backstop:
    # no hook, no watch. The hot knobs (MEMECOIN_SLOT_LANE_RABBIT_BUY, _PRECONF) are read per signal.
    buyer = None
    if (rabbit_feed or preconf_feed) and rabbit is not None:
        from sniper.services.memecoin_rabbit_signal import LandingWatch, RabbitBuyer
        landing['watch'] = LandingWatch(backstop=rabbit, head_fn=shyft_head,
                                        status=status.setdefault('rabbit_landing', {}))
        buyer = RabbitBuyer(seen=seen, owners_fn=owners_fn, on_buy=on_buy, backstop=rabbit,
                            feed=rabbit_feed or preconf_feed, status=status.setdefault('rabbit_buy', {}),
                            landing=landing['watch'], head_fn=shyft_head)
        landing['reads'] = buyer
        buyer.frames = frames
        if frames is not None:
            status['rabbit_buy']['frame_log'] = frames.stats
        for feed, log in preconf_frames.items():
            buyer.frame_logs[feed] = log
        if getattr(cfg, 'MEMECOIN_SLOT_LANE_RABBIT_DFLOW_AMM', False):
            # Beta's DFlow -> pump_amm decoder (memecoin_rabbit_dflow_amm), after the curve handler.
            from sniper.services.memecoin_rabbit_dflow_amm import DflowAmmHandler
            buyer.handlers.append(DflowAmmHandler(read_fn=buyer.read))
        # Beta's OKX proVF4 and Jupiter route_v2 -> pump_amm decoders (memecoin_rabbit_okx_amm,
        # memecoin_rabbit_jupiter_amm): they price the source's SOL leg on the lane's cached sol_usd, so
        # without that reader neither is registered, and amm_unwired names the routes left out.
        from sniper.services.memecoin_rabbit_jupiter_amm import JupiterAmmHandler
        from sniper.services.memecoin_rabbit_okx_amm import OkxAmmHandler
        for router in (OkxAmmHandler, JupiterAmmHandler):
            if not getattr(cfg, router.knob, False):
                continue
            if rabbit_sol_usd_fn is None:
                status['rabbit_buy'].setdefault('amm_unwired', {})[router.route] = 'no sol_usd reader'
            else:
                buyer.handlers.append(router(read_fn=buyer.read, sol_usd_fn=rabbit_sol_usd_fn))
        status['rabbit_buy']['handlers'] = [handler.route for handler in buyer.handlers]
        # Each AMM handler's counters, read by the heartbeat through status() (its own lock, JSON-safe).
        status['rabbit_buy']['handler_status'] = {
            handler.route: handler.status for handler in buyer.handlers if callable(getattr(handler, 'status', None))}
        tasks.append(asyncio.create_task(buyer.keep_warm(stop)))
        tasks.append(asyncio.create_task(landing['watch'].run(stop)))
        # THE POOL TAPE (memecoin_rabbit_tape, MEMECOIN_SLOT_LANE_RABBIT_TAPE): one processed stream on
        # the funded Shyft token, on its own thread and loop; the fire reads it from memory and falls
        # back to its RPC read on any miss. Off, or no Shyft config: no stream, every read is RPC.
        from sniper.services import memecoin_rabbit_tape as tape_mod
        # Only the lane passes the watch set (the pool cache's addresses); a caller without one (the
        # unit tests' signal loops) gets no stream and no connection.
        if tape_mod.knob_on() and shyft_config is not None and rabbit_tape_watch_fn is not None:
            buyer.tape = tape_mod.PoolTape()
            from sniper.services.memecoin_slot_slippage import make_guard
            rabbit.slippage_guard = make_guard(buyer.tape, head_fn=buyer.head_fn)
            status['slippage_guard'] = rabbit.slippage_guard.stats
            tape_status = status.setdefault('rabbit_tape', {})
            tape_status['store'] = buyer.tape.status
            # A separate HTTP pool for the fixed, paced background snapshots; entry reads keep
            # their own client. Unavailable setup leaves the existing miss -> RPC path intact.
            snapshot_kwargs = {}
            try:
                from sniper.services.memecoin_research_rpc import funded_dependency_url
                from sniper.execution.pump_curve_buy import keepalive_custody_rpc
                snapshot_url = funded_dependency_url('tape')
                if snapshot_url is not None:
                    snapshot_kwargs['snapshot_factory'] = lambda: keepalive_custody_rpc(snapshot_url)
            except Exception as exc:
                buyer.tape._snapshots['last_error'] = 'init:' + type(exc).__name__
            tape_mod.start_tape_thread(shyft_config[1][0], shyft_config[0], buyer.tape,
                                       rabbit_tape_watch_fn, tape_status, stop, **snapshot_kwargs)
    if prep is not None:
        prepare_token, prepare_url = prepare_config
        def rabbit_frame(signature, owner, slot, mono, generation):
            # Event loop; three bounded in-memory writes, no I/O (the journal and the frame line drain
            # off-loop).
            arrivals.stamp(signature, owner, rabbit_feed, mono)
            _rabbit_receipt(measurement, feed=rabbit_feed, signature=signature, owner=owner, slot=slot,
                            mono=mono, generation=generation, head=shyft_head()[0])
            frames.frame(signature, owner, slot, mono)
        rabbit_kwargs = ({'on_owner_frame': rabbit_frame, 'on_owner_miss': frames.note,
                          'on_rejected_candidate': frames.rejected}
                         if rabbit_feed else {})
        if rabbit_feed:
            tasks.append(asyncio.create_task(frames.run(stop)))
        if rabbit_feed and buyer is not None:
            rabbit_kwargs['on_candidate'] = buyer.offer
        tasks.append(asyncio.create_task(run_prepare_feed(prepare_url, prepare_token, owners_fn,
            prepare_sink, prep_status['feed'], stop, **rabbit_kwargs)))
    if preconf_feeds:
        from sniper.services.memecoin_alerts import send_alert
        from sniper.util.state_paths import state_path
        # ONE meter, cap and credit-file writer for every source's subscription.
        meter = preconf_mod.CreditMeter(int(getattr(cfg, 'MEMECOIN_SLOT_LANE_PRECONF_DAILY_CREDITS', 50000)),
                                        path=preconf.get('credit_path') or state_path(*preconf_mod.CREDIT_FILE))
        writer = {}
        by_source = status['preconf_sources'] = {}
        for source, feed in zip(preconf_sources, preconf_feeds):
            pstatus = by_source[source] = {'feed': {}, 'frame_log': preconf_frames[feed].stats, 'refused': {},
                                           'owner_frames': 0, 'candidates': 0, 'offered': 0,
                                           'credits': meter.status}
            preconf_sink = preconf_mod.FrameSink(feed=feed, owners_fn=owners_fn, arrivals=arrivals,
                                                 frames=preconf_frames[feed], status=pstatus, buyer=buyer,
                                                 prep=prep, on_prepare=on_prepare,
                                                 prep_status=prep_status if prep is not None else None)
            tasks.append(asyncio.create_task(preconf_frames[feed].run(stop)))
            tasks.append(asyncio.create_task(preconf_mod.run_preconf_feed(
                preconf['url'], owners_fn, pstatus['feed'], stop, on_frame=preconf_sink, meter=meter,
                connect=preconf.get('connect'), alert_fn=preconf.get('alert_fn') or send_alert,
                source=source, writer=writer)))
        status['preconf'] = by_source[preconf_sources[0]]   # the first source, the shape before 10-17
    for url in urls:
        name = 'shyft:' + urlsplit(url).hostname
        offer = sink(name)
        kwargs = {'on_metadata': metadata_sink(name)} if on_routed is not None or prep is not None else {}
        if on_sell is not None:
            kwargs['on_sell'] = sell_sink
        if on_damm_creation is not None:
            kwargs['on_damm_creation'] = damm_sink
        if on_migration is not None:
            kwargs['on_migration'] = migration_sink
        tasks.append(asyncio.create_task(run_feed(url, token, owners_fn, offer, feeds[name], stop, **kwargs)))
    try:
        while not stop.is_set():
            await asyncio.sleep(.1)
            if prep is not None:
                prep.expire(now=time.monotonic())
            update_status()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        while not metadata.empty():
            name, update, receipt, owners, frozen, pending = metadata.get_nowait()
            publish_pending(name, update, receipt, frozen, pending)
            _coverage_gap(measurement, name, 'metadata_shutdown',
                pending if pending is not None else feeds[name].get('connection_generation', 0),
                receipt['recv_monotonic'], receipt['recv_monotonic'], 1)
            metadata.task_done()
        update_status()


def _routed_subscribe(request_id, program, owner):
    owner_filter = {"memcmp": {"offset": 32, "bytes": owner}}
    # Legacy Token accounts are exactly 165 bytes; Token-2022 accounts carry extensions after them.
    filters = [{"dataSize": 165}, owner_filter] if program == TOKEN_PROGRAM else [owner_filter]
    return {"jsonrpc": "2.0", "id": request_id, "method": "programSubscribe", "params": [
        program, {"encoding": "jsonParsed", "commitment": "processed", "filters": filters}]}


def _damm_v2_seen(frame, enabled):
    """(DammV2Seen(),) when cp-amm ran in this frame; () when off, absent or unreadable. DAMM v2's
    event is not in the logs (emit_cpi): the marker only says the coin may have a DAMM v2 pool. Never
    raises: a decode failure must not cost the routed pair it rides on."""
    if not enabled:
        return ()
    try:
        from sniper.execution.meteora_damm_v2_buy import DammV2Seen, damm_v2_invoked
        logs = frame["params"]["result"]["value"].get("logs")
        return (DammV2Seen(),) if damm_v2_invoked(logs) else ()
    except Exception:
        logger.warning("slot signal: DAMM v2 marker failed", exc_info=True)
        return ()


def _note_owner_acked(owner):
    """One owner's restart-blackout END (memecoin_onchain_lane.LANE_DOWN). Never raises."""
    try:
        from sniper.services.memecoin_onchain_lane import LANE_DOWN
        LANE_DOWN.owner_acked(owner, time.time())
    except Exception:
        logger.exception("slot signal: lane_down owner ack failed")


def _note_lane_ready(owners):
    """Every owner answered: the restart blackout's log line (memecoin_onchain_lane.LANE_DOWN), first time
    only. Never raises into the socket loop."""
    try:
        from sniper.services.memecoin_onchain_lane import LANE_DOWN
        line = LANE_DOWN.slot_lane_ready(time.time(), owners=owners)
        if line:
            logger.warning(line)
    except Exception:
        logger.exception("slot signal: lane_down ready mark failed")


def _dbc_seen(frame, enabled):
    """(DbcSeen(),) when Meteora DBC ran in this frame; () when off, absent or unreadable. DBC's event
    is self-CPI, not in the logs: the marker only says the coin may be on a DBC curve. Never raises."""
    if not enabled:
        return ()
    try:
        from sniper.execution.meteora_dbc_buy import DbcSeen, dbc_invoked
        logs = frame["params"]["result"]["value"].get("logs")
        return (DbcSeen(),) if dbc_invoked(logs) else ()
    except Exception:
        logger.warning("slot signal: DBC marker failed", exc_info=True)
        return ()


def _launchlab_trades(frame, enabled):
    """The TradeEvents LaunchLab itself emitted in this frame; () when off or unreadable. Never
    raises: a decode failure must not cost the routed pair it rides on."""
    if not enabled:
        return ()
    try:
        from sniper.execution.raydium_launchlab_buy import trade_events
        return tuple(trade_events(frame["params"]["result"]["value"].get("logs")))
    except Exception:
        logger.warning("slot signal: LaunchLab trade decode failed", exc_info=True)
        return ()


def _cpmm_swaps(frame, enabled):
    """The CPMM swaps the CPMM program itself emitted in this frame; () when off or unreadable.
    Never raises: a decode failure must not cost the routed pair it rides on."""
    if not enabled:
        return ()
    try:
        from sniper.execution.raydium_cpmm_buy import swap_events
        return tuple(swap_events(frame["params"]["result"]["value"].get("logs")))
    except Exception:
        logger.warning("slot signal: CPMM swap decode failed", exc_info=True)
        return ()


def routed_venue_events(frame, *, cpmm_on, launchlab_on, pump_on, damm_on, dbc_on):
    """Existing routed preparation evidence, including mode-gated pool-discovery markers."""
    return (_cpmm_swaps(frame, cpmm_on) + _launchlab_trades(frame, launchlab_on)
            + _damm_v2_seen(frame, damm_on) + _dbc_seen(frame, dbc_on)
            + ((_PumpLater(frame),) if pump_on else ()))


@dataclass(frozen=True)
class RoutedPumpBuy:
    """A pump buy event in an owner's routed frame, WHOEVER its actor: router proVF4 names its own
    account (ARu4n5mF...), not the owner. `fields` = the decoder's dict as items (hashable)."""
    venue: str                    # "pump_curve" | "pump_amm"
    fields: tuple
    trade_event_log: str
    token_program: Optional[str]  # curve: token_program_from_logs; None on pump_amm


def routed_pump_buys(frame, enabled):
    """Every pump buy event in this logs frame, any actor; () when off or unreadable.
    Never raises: a decode failure must not cost the routed pair it rides on."""
    if not enabled:
        return ()
    try:
        from sniper.execution.pump_curve_buy import token_program_from_logs
        logs = frame["params"]["result"]["value"].get("logs") or []
        found = []
        for program, (disc, fn) in _DECODERS.items():
            for index, line in program_data_logs(logs, program):
                raw = anchor_event_bytes(line)
                hit = fn(raw, None, any_user=True) if raw[:8] == disc else None
                if hit:
                    found.append(RoutedPumpBuy(
                        venue=hit["venue"], fields=tuple(hit.items()), trade_event_log=line,
                        token_program=(token_program_from_logs(logs, index)
                                       if hit["venue"] == "pump_curve" else None)))
        return tuple(found)
    except Exception:
        logger.warning("slot signal: routed pump decode failed", exc_info=True)
        return ()


# THE SOURCE'S POST-TRADE PRICE / ITS AVERAGE FILL, the chase reference's ratio. Outside
# [0.98, 50] it is implausible and dropped (memecoin_source_valuation re-exports these).
IMPACT_RATIO_MIN, IMPACT_RATIO_MAX = 0.98, 50.0
# ROUTED LEGS (2026-09-28, spec 2026-09-28-memecoin-chase-post-trade-reference-design.md, amendment
# "routed USDC buys"). A routed buy's post-trade price comes from the ONE venue event that bought the
# coin's whole delta. The binding is by amount, because a LaunchLab or PumpSwap event names its pool
# and not its coin: delta <= out <= delta / 0.9. The slack is a Token-2022 transfer fee (1% and 3%
# on the six LaunchLab coins of the 16 real receipts). A split route has out < delta on every leg.
LEG_TRANSFER_FEE_MAX_BPS = 1000
ROUTED_REFERENCE_NOTE = "average includes upstream leg and router fees"


def routed_leg_ratio(events, mint, delta, bound=None):
    """(ratio, venue) for the ONE venue event that bought the coin's whole `delta` (atomic), else
    (None, reason). Pure. Every venue uses the constant-product BASE-side ratio B_pre / (B_pre - out),
    which is unit-free, so it never needs the quote's USD price:
      pump curve  B_pre = virtual_token_reserves (post-trade) + token_amount; the event names the coin
      PumpSwap    B_pre = pool_base_token_reserves (pre-trade)
      LaunchLab   B_pre = virtual_base - real_base_before, only on the constant-product curve: the
                  event must satisfy curve_out exactly (any other curve type fails it)
      CPMM        B_pre = output_vault_before; the event names its output mint
    Events are told apart by shape, so this imports nothing a live frame has not already loaded.
    Concentrated-liquidity and orderbook legs have no event here and never bind. `bound(out)`, when
    given, must also hold: the watcher, with the receipt in hand, checks that a non-owner account's
    balance of the coin fell by exactly `out` (Lambda's review 3), which the amount alone cannot."""
    if not isinstance(delta, int) or delta <= 0:
        return None, "no_delta"
    legs, unverified = [], 0
    for ev in events or ():
        try:
            if isinstance(ev, RoutedPumpBuy):
                f = dict(ev.fields)
                if ev.venue == "pump_curve" and f.get("mint") != mint:
                    continue
                out = int(f["token_amount"])
                pre = (int(f["virtual_token_reserves"]) + out if ev.venue == "pump_curve"
                       else int(f["real_token_reserves"]))
                venue = ev.venue
            elif hasattr(ev, "virtual_base"):             # raydium_launchlab_buy.LaunchLabTrade
                from sniper.execution.raydium_launchlab_buy import curve_out
                out = ev.real_base_after - ev.real_base_before
                net = ev.real_quote_after - ev.real_quote_before
                if not ev.is_buy or out != curve_out(net, ev.virtual_base, ev.virtual_quote,
                                                     ev.real_base_before, ev.real_quote_before):
                    continue
                pre, venue = ev.virtual_base - ev.real_base_before, "raydium_launchlab"
            elif hasattr(ev, "output_vault_before"):      # raydium_cpmm_buy.CpmmSwap
                if ev.output_mint != mint:
                    continue
                out, pre, venue = ev.output_amount, ev.output_vault_before, "raydium_cpmm"
            else:
                continue
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue                                      # an unreadable event binds nothing
        if delta <= out and out * (10000 - LEG_TRANSFER_FEE_MAX_BPS) <= delta * 10000:
            if bound is not None and not bound(out):
                unverified += 1
                continue
            legs.append((pre, out, venue))
    if len(legs) != 1:
        return None, ("ambiguous_leg" if legs else "leg_unverified" if unverified else "no_whole_leg")
    pre, out, venue = legs[0]
    ratio = pre / (pre - out) if pre > out else None
    if ratio is None or not (math.isfinite(ratio) and IMPACT_RATIO_MIN <= ratio <= IMPACT_RATIO_MAX):
        return None, "ratio_implausible"
    return ratio, venue


def routed_post_facts(events_fn, mint, delta, source_price, bound=None):
    """The valuation fields a routed buy's post-trade price adds: source_impact_ratio,
    source_post_price (= the average per received token x the ratio) and impact_leg, or
    impact_unknown with the reason. `events_fn` decodes lazily; a decode failure is its own reason,
    never an empty route. Never raises: a routed buy keeps its row and its average fill."""
    try:
        ratio, why = routed_leg_ratio(events_fn(), mint, delta, bound)
    except Exception:
        logger.warning("routed leg decode failed mint=%s", mint, exc_info=True)
        return {"impact_unknown": "leg_decode_error"}
    if ratio is None:
        return {"impact_unknown": why}
    if not (isinstance(source_price, (int, float)) and math.isfinite(source_price) and source_price > 0):
        return {"impact_unknown": "no_source_price", "source_impact_ratio": ratio, "impact_leg": why}
    return {"source_impact_ratio": ratio, "source_post_price": source_price * ratio, "impact_leg": why}


class _PumpLater:
    """A logs frame whose pump events are decoded only if its pair emits (about 10 an hour),
    never for the thousands of frames an hour that never pair: the decode costs p50 111 us, on
    the socket thread that also carries the lane's own buys."""
    __slots__ = ("frame",)

    def __init__(self, frame):
        self.frame = frame


def _logs_slot_and_signature(frame):
    """(slot, signature) of a SUCCESSFUL logsNotification, else None."""
    result = frame["params"]["result"]
    value = result["value"]
    if value.get("err") is not None or not value.get("signature"):
        return None
    return int(result["context"]["slot"]), value["signature"]


def _emit_routed(buys, on_routed, *, head_slot=None, measurement=None, generation=0):
    for buy in buys:
        if isinstance(head_slot, int) and not isinstance(head_slot, bool) and head_slot > 0:
            buy = replace(buy, paired_head_slot=head_slot)
        try:
            on_routed(buy)
        except Exception:
            logger.exception("slot signal on_routed failed for %s", buy.signature)
        finally:
            if not getattr(on_routed, "publishes_receipts", False):
                _publish_receipt(measurement, buy, "helius", generation)


def _note_baseline_failure(failed, owner, now):
    last, count = failed.get(owner, (now, 0))
    failed[owner] = (now, count + 1)


def _baseline_retry_due(failed, owner, now):
    """True when an owner's baseline may be read: never failed, or its backoff has elapsed."""
    if owner not in failed:
        return True
    last, count = failed[owner]
    return now - last >= min(ROUTED_BASELINE_RETRY_S * 2 ** (count - 1), ROUTED_BASELINE_RETRY_MAX_S)


async def _routed_baseline(owner, pairer, baseline_fn, gate, routed, owners_fn, failed_at):
    """Read one owner's token accounts (both programs) and hand them to the pairer. One at a time
    through `gate` and spaced, so a roster of ~40 owners warms up in ~20 s instead of bursting."""
    async with gate:
        generation = pairer.generation(owner)
        try:
            got = await asyncio.to_thread(baseline_fn, owner)
            if pairer.watching(owner) and pairer.generation(owner) == generation:
                for kind, _program in ROUTED_PROGRAMS:
                    slot, accounts = got[kind]
                    pairer.set_baseline(owner, kind, slot, accounts)
            failed_at.pop(owner, None)
        except Exception as exc:  # the owner stays not ready; a later roster pass retries
            _note_baseline_failure(failed_at, owner, time.monotonic())
            routed["baseline_failed"] += 1
            routed["last_error"] = "baseline: %s" % type(exc).__name__
        routed["ready"] = sum(1 for o in owners_fn() if pairer.ready(o))
        await asyncio.sleep(ROUTED_BASELINE_SPACING_S)


async def _run(on_buy, owners_fn, ws_url, status, stop, connect, on_sell=None, on_routed=None,
               baseline_fn=None, measurement=None):
    measurement = measurement or getattr(on_buy, "measurement", None)
    import websockets
    connect = connect or (lambda u: websockets.connect(u, max_size=None, ping_interval=20))
    seen, backoff, generation = {}, 1.0, 0
    routed = status.get("routed") if on_routed is not None else None
    from sniper.execution.raydium_cpmm_buy import lane_mode as _cpmm_mode
    cpmm_on = routed is not None and _cpmm_mode() != "off"    # read once per thread start
    from sniper.execution.raydium_launchlab_buy import lane_mode as _launchlab_mode
    launchlab_on = routed is not None and _launchlab_mode() != "off"
    from sniper.settings import memecoin as _cfg
    pump_on = routed is not None and bool(_cfg.MEMECOIN_SLOT_LANE_ROUTED_PUMP)
    from sniper.execution.meteora_damm_v2_buy import lane_mode as _damm_mode
    damm_on = routed is not None and _damm_mode() != "off"
    from sniper.execution.meteora_dbc_buy import lane_mode as _dbc_mode
    dbc_on = routed is not None and _dbc_mode() != "off"
    gate = asyncio.Semaphore(1)
    while not stop.is_set():
        try:
            async with connect(ws_url) as ws:
                generation += 1
                status["connection_generation"] = generation
                status.update(connected=True, last_error=None, requested_owners=(), acknowledged_owners=())
                subs, pending, want, next_id, refresh_at = {}, {}, {}, 1, 0.0
                # Routed state lives per connection: a reconnect re-reads every baseline.
                pairer = RoutedPairer(stats=routed, measurement=measurement,
                    connection_generation=generation) if routed is not None else None
                routed_subs, routed_pending, routed_acked, baselining, failed_at = {}, {}, {}, {}, {}
                if routed is not None:
                    routed.update(subs_ok=0, subs_refused=0, ready=0)
                # HEAD SLOT on the same socket: the lane refuses to send once the head is past S+1,
                # which saves the fee of a guaranteed guard failure.
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": _SLOT_SUB_ID, "method": "slotSubscribe"}))
                while not stop.is_set():
                    if time.monotonic() >= refresh_at:
                        refresh_at = time.monotonic() + ROSTER_REFRESH_S
                        try:
                            new = {}
                            for owner, (uid, handle) in (await asyncio.to_thread(owners_fn)).items():
                                raw = _owner_bytes(owner)
                                if raw is not None:
                                    new[owner] = (raw, uid, handle)
                        except Exception as exc:  # a failed or malformed roster keeps the live one
                            logger.exception("slot signal roster refresh failed")
                            status["last_error"] = "roster: %r" % (exc,)
                            new = None
                        if new is not None:
                            for sid, owner in list(subs.items()):
                                if owner not in new:
                                    del subs[sid]
                                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 0,
                                                              "method": "logsUnsubscribe", "params": [sid]}))
                            if pairer is not None:
                                for sid, (owner, _kind) in list(routed_subs.items()):
                                    if owner not in new:
                                        del routed_subs[sid]
                                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 0,
                                                                  "method": "programUnsubscribe", "params": [sid]}))
                                for owner in [o for o in routed_acked if o not in new]:
                                    del routed_acked[owner]
                                    pairer.forget(owner)
                            for owner in set(new) - set(subs.values()) - set(pending.values()):
                                pending[next_id] = owner
                                await ws.send(json.dumps({"jsonrpc": "2.0", "id": next_id,
                                                          "method": "logsSubscribe", "params": [
                                                              {"mentions": [owner]}, {"commitment": "processed"}]}))
                                next_id += 1
                                if pairer is not None and owner not in routed_acked:
                                    routed_acked[owner] = set()
                                    for _kind, program in ROUTED_PROGRAMS:
                                        routed_pending[next_id] = (owner, _kind)
                                        await ws.send(json.dumps(_routed_subscribe(next_id, program, owner)))
                                        next_id += 1
                            want = new
                            status.update(owners=len(want), subscriptions=len(subs),
                                          requested_owners=tuple(sorted(want)),
                                          acknowledged_owners=tuple(sorted(set(subs.values()) & set(want))))
                            if pairer is not None:
                                routed["subs_ok"] = len(routed_subs)
                                # A failed baseline, or one refused after an overflow, is retried here.
                                for owner, kinds in routed_acked.items():
                                    task = baselining.get(owner)
                                    if (baseline_fn is not None and len(kinds) == len(ROUTED_PROGRAMS)
                                            and owner in want and not pairer.ready(owner)
                                            and (task is None or task.done())
                                            and _baseline_retry_due(failed_at, owner,
                                                                    time.monotonic())):
                                        pairer.watch(owner, want[owner][1], want[owner][2])
                                        baselining[owner] = asyncio.ensure_future(_routed_baseline(
                                            owner, pairer, baseline_fn, gate, routed, lambda: want,
                                            failed_at))
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, refresh_at - time.monotonic()))
                    except asyncio.TimeoutError:
                        if pairer is not None:
                            pairer.expire(time.monotonic())
                        continue
                    mono = time.monotonic()           # stamped before any parsing
                    backoff = 1.0  # reset only once the server talks: a connect-then-drop loop backs off
                    from sniper.util.clock import real_utc_now_iso
                    utc = real_utc_now_iso()
                    try:
                        frame = json.loads(msg)
                        if frame.get("method") == "slotNotification":
                            slot = int(frame["params"]["result"]["slot"])
                            if status["head_slot"] is None or slot > status["head_slot"]:
                                # max, never latest: a fork's lower slot must not rewind the head
                                status.update(head_slot=slot, head_slot_mono=mono)
                                HEAD.update(slot=slot, mono=mono)   # fill timing: recv-S (2026-10-02)
                            continue
                        if frame.get("id") == _SLOT_SUB_ID:
                            if not isinstance(frame.get("result"), int):
                                status["last_error"] = "slotSubscribe refused: %s" % frame.get("error")
                            continue
                        if pairer is not None and frame.get("id") in routed_pending:
                            owner, kind = routed_pending.pop(frame["id"])
                            if isinstance(frame.get("result"), int):
                                routed_subs[frame["result"]] = (owner, kind)
                                routed_acked.setdefault(owner, set()).add(kind)
                                if (len(routed_acked[owner]) == len(ROUTED_PROGRAMS) and owner in want
                                        and baseline_fn is not None):
                                    pairer.watch(owner, want[owner][1], want[owner][2])
                                    baselining[owner] = asyncio.ensure_future(_routed_baseline(
                                        owner, pairer, baseline_fn, gate, routed, lambda: want,
                                        failed_at))
                            else:
                                routed["subs_refused"] += 1
                                routed["last_error"] = "subscribe refused: %s" % frame.get("error")
                            routed["subs_ok"] = len(routed_subs)
                            continue
                        if frame.get("id") in pending:
                            owner = pending.pop(frame["id"])
                            if isinstance(frame.get("result"), int):
                                subs[frame["result"]] = owner
                                _note_owner_acked(owner)        # this owner's lane_down END
                            else:
                                status["last_error"] = "subscribe refused: %s" % frame.get("error")
                            status.update(subscriptions=len(subs),
                                              acknowledged_owners=tuple(sorted(set(subs.values()) & set(want))))
                            if want and not any(o in want for o in pending.values()):
                                _note_lane_ready(len(want))     # every owner's request answered
                            continue
                        if frame.get("method") == "programNotification":
                            if pairer is not None:
                                pairer.expire(mono)
                                who = routed_subs.get((frame.get("params") or {}).get("subscription"))
                                note = parse_token_notification(frame) if who else None
                                if note is not None and who[0] in want and note["owner"] == who[0]:
                                    _emit_routed(pairer.on_token(
                                        who[0], who[1], note["slot"], note["pubkey"], note["mint"],
                                        note["amount"], note["decimals"], recv_monotonic=mono,
                                        recv_utc=utc), on_routed, head_slot=status["head_slot"],
                                        measurement=measurement, generation=generation)
                            continue
                        if frame.get("method") != "logsNotification":
                            continue
                        status["last_frame_mono"] = mono
                        owner = subs.get((frame.get("params") or {}).get("subscription"))
                        if owner not in want:
                            continue
                        raw, uid, handle = want[owner]
                        # One event loop: every slotNotification that arrived before this frame
                        # has already moved the head, so this is the head known at receipt.
                        frozen_receipt = _receipt_identity(measurement, mono, generation)
                        ev = decode_owner_buy(frame, owner, raw, uid, handle,
                                              recv_monotonic=mono, recv_utc=utc,
                                              head_at_recv=status["head_slot"],
                                              head_at_recv_mono=status["head_slot_mono"],
                                              receipt_evidence=frozen_receipt)
                        # Dispatch the buy before decoding other legs: a mixed rotation still
                        # mirrors its sell without adding work before entry dispatch.
                        duplicate = ev is not None and (ev.signature, owner) in seen
                        if ev is not None and not duplicate:
                            seen[(ev.signature, owner)] = mono
                            status["events"] += 1
                            try:
                                on_buy(ev)
                            except Exception:
                                logger.exception("slot signal on_buy failed for %s", ev.signature)
                            finally:
                                if not getattr(on_buy, "publishes_receipts", False):
                                    _publish_receipt(measurement, ev, "helius", generation)
                        elif ev is not None:
                            _publish_receipt(measurement, ev, "helius", generation, state="duplicate_buy")
                        else:
                            _unknown_receipt(measurement, feed="helius", generation=generation,
                                mono=mono, state="empty_decode_unknown", owner=owner, evidence=frozen_receipt,
                                signature=((frame.get("params") or {}).get("result") or {}).get("value", {}).get("signature"))
                        if len(seen) > 4096:  # ponytail: FIFO trim; repeats older than 2k buys are harmless
                            for key in list(seen)[:2048]:
                                del seen[key]
                        sells = (decode_owner_sells(frame, owner, raw, uid, handle,
                                                    recv_monotonic=mono, recv_utc=utc)
                                 if on_sell is not None else ())
                        # Routed bookkeeping only for frames the lane does not act on, AFTER the
                        # decode: the lane's own buys below carry no added work before on_buy.
                        routed_logs = (_logs_slot_and_signature(frame)
                                       if (ev is None or ev.refusal) and pairer is not None and pairer.watching(owner)
                                       else None)
                    except Exception:
                        status["decode_errors"] += 1
                        _coverage_gap(measurement, "helius", "decode_error", generation, mono, mono)
                        logger.exception("slot signal frame not decoded")
                        continue
                    fresh_sells = []
                    for sell in sells:
                        if (sell.signature, owner, sell.mint or sell.pool) in seen:
                            continue
                        seen[(sell.signature, owner, sell.mint or sell.pool)] = mono
                        status["sells"] += 1
                        fresh_sells.append(sell)
                    if fresh_sells:
                        try:
                            on_sell(fresh_sells)
                        except Exception:
                            logger.exception("slot signal on_sell failed for %s", sell.signature)
                    if routed_logs is not None:
                        pairer.expire(mono)
                        _emit_routed(pairer.on_logs(owner, routed_logs[0], routed_logs[1], lane=False,
                                                    recv_monotonic=mono, recv_utc=utc,
                                                    receipt_evidence=frozen_receipt,
                                                    requires_receipt_proof=bool(ev and ev.refusal),
                                                    venue_events=routed_venue_events(frame, cpmm_on=cpmm_on,
                                                        launchlab_on=launchlab_on, pump_on=pump_on,
                                                        damm_on=damm_on, dbc_on=dbc_on)),
                                     on_routed, head_slot=status["head_slot"],
                                     measurement=measurement, generation=generation)
                    if ev is None or ev.refusal:
                        continue
                    if pairer is not None and pairer.watching(owner):
                        # The lane's own buy: its slot can never pair as a routed buy too.
                        pairer.on_logs(owner, ev.slot, ev.signature, lane=True,
                                       recv_monotonic=mono, recv_utc=utc, receipt_evidence=frozen_receipt)
        except asyncio.CancelledError:
            status.update(connected=False, subscriptions=0, acknowledged_owners=())
            raise
        except Exception as exc:
            _coverage_gap(measurement, "helius", "connection_lost", generation,
                          status.get("last_frame_mono"), time.monotonic())
            logger.warning("slot signal socket down: %r; retry in %.0fs", exc, backoff)
            status.update(connected=False, subscriptions=0, acknowledged_owners=(), last_error=repr(exc)[:200],
                          reconnects=status["reconnects"] + 1)
            if routed is not None:
                routed.update(subs_ok=0, ready=0)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
    status.update(connected=False, acknowledged_owners=())


# --- pump.fun migrations: the pools the lane should know before their first buy -----------------

PUMP_MIGRATION_ACCOUNT = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
MIGRATION_SILENCE_S = 120.0
_MIGRATION_SUB_ID = 1


class _SilentSubscription(Exception):
    pass


def start_migration_thread(on_pool: Callable[[str], None], *, ws_url: str, status: dict,
                           stop: Optional[threading.Event] = None, connect=None,
                           silence_s: float = MIGRATION_SILENCE_S, backoff_s: float = 1.0,
                           program=PUMP_MIGRATION_ACCOUNT, notification_target=None, on_frame=None):
    """Hand every verified PumpSwap pool pump.fun migrates into to `on_pool`, on this
    thread (the pool cache's `offer_stream`, which only enqueues).

    WHY (2026-09-27, Alpha task 1). Dougherty 65991 and lemon 65992 landed S+7: the lane decoded
    both buys at S and refused `pool_unknown`. 20 of the 27 lane-buildable pools it missed since
    2026-09-26 21:07Z had migrated within 24 h of the buy; neither incident's mint was in any ledger
    table beforehand. One `logsSubscribe` on pump's migration account at `confirmed` on the KEYLESS
    read socket: about 2,800 frames a day at ~10.8 KB, ~70% of them a real migration whose pAMM
    CreatePoolEvent names the pool (the rest are repeat "already migrated" calls, which carry none).
    A probe (scripts/research/pump_migration_stream_probe.py) heard 10 of 10 chain migrations 1.1-1.9 s
    after their block, then went SILENT with no close, so a subscription quiet for `silence_s`
    reconnects. Migrations inside a gap are not backfilled: their pools miss-then-learn as before.
    `status`: connected, frames, pools, reconnects, last_frame_mono, last_error (a class name, never
    a URL). `on_frame(signature, pool_or_None)`, when given, receives every successful frame INSTEAD
    of `on_pool` (MigrationFanIn: several sockets, each frame taken once)."""
    status.update(connected=False, frames=0, pools=0, reconnects=0, last_frame_mono=None,
                  last_error=None)
    if not ws_url or _shares_funded_key(ws_url):
        raise ValueError("the migration listener needs its own ws url, never the funded custody key")
    stop = stop or threading.Event()
    thread = threading.Thread(
        target=lambda: asyncio.run(_run_migrations(on_pool, ws_url, status, stop, connect,
                                                   float(silence_s), float(backoff_s), program,
                                                   notification_target, on_frame)),
        name="slot-lane-migrations", daemon=True)
    thread.start()
    return thread


async def _run_migrations(on_pool, ws_url, status, stop, connect, silence_s, backoff_s,
                          program=PUMP_MIGRATION_ACCOUNT, notification_target=None, on_frame=None):
    import websockets
    from sniper.execution.pumpswap_buy import create_pool_event
    seen_pools = {}
    connect = connect or (lambda u: websockets.connect(u, max_size=262144, max_queue=16, ping_interval=20))
    backoff = backoff_s
    while not stop.is_set():
        try:
            async with connect(ws_url) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": _MIGRATION_SUB_ID,
                                          "method": "logsSubscribe", "params": [
                                              {"mentions": [program]},
                                              {"commitment": "confirmed"}]}))
                status["connected"] = True
                heard = time.monotonic()
                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=min(1.0, silence_s))
                    except asyncio.TimeoutError:
                        if time.monotonic() - heard > silence_s:
                            raise _SilentSubscription("silent %.0f s" % silence_s)
                        continue
                    heard, backoff = time.monotonic(), backoff_s
                    frame = json.loads(msg)
                    if frame.get("id") == _MIGRATION_SUB_ID:
                        if not isinstance(frame.get("result"), int):
                            raise RuntimeError("subscribe refused")
                        continue
                    if frame.get("method") != "logsNotification":
                        continue
                    status["frames"] += 1
                    status["last_frame_mono"] = heard
                    value = frame["params"]["result"]["value"]
                    if value.get("err"):
                        continue
                    if notification_target is None:
                        event = create_pool_event(value.get("logs"))
                        target = event["pool"] if event and event["pump_migrated"] else None
                    else:
                        target = notification_target(value)
                    if target and on_frame is None:
                        if target in seen_pools:
                            continue
                        seen_pools[target] = None
                        if len(seen_pools) > 4096:
                            seen_pools.pop(next(iter(seen_pools)))
                    if target:
                        status["pools"] += 1
                    if on_frame is not None:
                        on_frame(value.get("signature"), target)
                    elif target:
                        on_pool(target)
        except Exception as exc:  # the listener is an optimization: log, back off, reconnect
            if stop.is_set():
                break
            status["reconnects"] += 1
            status["last_error"] = (str(exc) if isinstance(exc, _SilentSubscription)
                                    else type(exc).__name__)
            logger.warning("slot lane migration listener: %s; reconnecting", status["last_error"])
        finally:
            status["connected"] = False
        if stop.is_set():
            break
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


# --- two migration sockets (2026-10-07, operator order via Alpha) ---------------------------------
#
# The keyless public socket STALLS: it accepts the subscription and then stops delivering, so only
# the 120 s silence check recovers it, 5-14 times an hour on 2026-10-07 (up to 10-28 deaf minutes an
# hour) while the chain carried ~1.1 migration-account transactions a minute. A second subscription
# on the research Helius socket (never the funded key, never the signal socket's key) runs beside it;
# each keeps its own thread, silence check and status, and every frame is taken once.

class MigrationFanIn:
    """Several migration listener sockets -> one stream. Each frame's signature is taken once, from
    whichever socket heard it first, and its pool reaches `on_pool` once. Any thread."""

    KEEP = 4096                          # signatures remembered (~1.5 days of migrations)

    def __init__(self, on_pool, *, clock=time):
        import collections
        self.on_pool, self.clock = on_pool, clock
        self._lock = threading.Lock()
        self._seen = collections.OrderedDict()
        self.stats = {"unique_frames": 0, "duplicates": 0, "pools": 0, "last_unique_mono": None,
                      "first_by": {}}

    def frame_fn(self, socket):
        return lambda signature, target: self.frame(socket, signature, target)

    def seen(self, signature):
        with self._lock:
            return signature in self._seen

    def frame(self, socket, signature, target):
        key = signature if signature else ("unsigned", target)
        with self._lock:
            if key in self._seen:
                self.stats["duplicates"] += 1
                return False
            self._seen[key] = None
            while len(self._seen) > self.KEEP:
                self._seen.popitem(last=False)
            self.stats["unique_frames"] += 1
            self.stats["last_unique_mono"] = self.clock.monotonic()
            self.stats["first_by"][socket] = self.stats["first_by"].get(socket, 0) + 1
            if target:
                self.stats["pools"] += 1
        if target:
            self.on_pool(target)
        return True


MIGRATION_DEAF_FLAT_S = 600.0      # pooled frames flat this long before the chain is asked
MIGRATION_DEAF_CHECK_S = 60.0      # at most one chain read this often while flat


class MigrationDeafWatch:
    """One WARNING line, one page and `deaf` in the heartbeat when the pooled stream has been flat for
    `flat_s` while the chain shows a successful migration-account transaction newer than our last
    frame that no socket delivered. A quiet chain is not deafness: the chain is read only once the
    stream has been flat that long, at most once per `check_s`. Recovery is one line."""

    def __init__(self, fanin, *, chain_newest_fn, alert_fn=None, clock=time, wall=time.time,
                 flat_s=MIGRATION_DEAF_FLAT_S, check_s=MIGRATION_DEAF_CHECK_S):
        self.fanin, self.chain_newest_fn, self.alert_fn = fanin, chain_newest_fn, alert_fn
        self.clock, self.wall, self.flat_s, self.check_s = clock, wall, float(flat_s), float(check_s)
        self.started = clock.monotonic()
        self._next_read = 0.0
        self.stats = {"deaf": False, "deaf_since": None, "episodes": 0, "chain_reads": 0,
                      "chain_errors": 0, "last_check_flat_s": None}

    def check(self):
        now = self.clock.monotonic()
        last = self.fanin.stats["last_unique_mono"] or self.started
        flat = now - last
        self.stats["last_check_flat_s"] = round(flat, 1)
        if flat < self.flat_s:
            if self.stats["deaf"]:
                self.stats.update(deaf=False, deaf_since=None)
                logger.warning("[memecoin] slot lane migration listeners hearing again after a deaf episode")
            return self.stats["deaf"]
        if self.stats["deaf"] or now < self._next_read:
            return self.stats["deaf"]
        self._next_read = now + self.check_s
        self.stats["chain_reads"] += 1
        try:
            newest = self.chain_newest_fn()
        except Exception:
            self.stats["chain_errors"] += 1
            logger.warning("slot lane migration deaf check: chain read failed", exc_info=True)
            return False
        if not newest:
            return False
        signature, block_time = newest
        if self.fanin.seen(signature) or block_time is None or block_time <= self.wall() - flat:
            return False                                   # nothing on chain since our last frame
        self.stats.update(deaf=True, deaf_since=round(self.wall() - flat, 1))
        self.stats["episodes"] += 1
        text = ("migration listeners DEAF: no frame for %.0f s on any socket while the chain has newer "
                "migration-account tx %s..." % (flat, str(signature)[:12]))
        logger.warning("[memecoin] slot lane %s", text)
        if self.alert_fn is not None:
            try:
                self.alert_fn(text)
            except Exception:
                logger.exception("slot lane migration deaf page failed")
        return True

    def run(self, stop=None, every_s=30.0):
        while stop is None or not stop.is_set():
            try:
                self.check()
            except Exception:
                logger.exception("slot lane migration deaf check failed")
            if stop is not None:
                stop.wait(every_s)
            else:
                time.sleep(every_s)


def research_migration_ws(env=os.environ):
    """(ws url, None) for the second migration socket on the research Helius key, or (None, reason).
    Refused when no research key is set, when it is the funded custody key (start_migration_thread
    also refuses that), or when it is the signal socket's key (ONCHAIN_WATCH_HELIUS_RPC_URL): that key
    carries the funded copy signal and its credits stay its own."""
    from sniper.execution.solana_rpc_pool import key_id
    from sniper.services.memecoin_research_rpc import funded_overlap, research_urls
    urls = research_urls(env)
    if not urls:
        return None, "no_research_key"
    url = urls[0]
    if funded_overlap([url], env):
        return None, "research_key_is_funded"
    signal = (env.get("ONCHAIN_WATCH_HELIUS_RPC_URL") or "").strip()
    if signal and key_id(signal) == key_id(url):
        return None, "research_key_is_the_signal_key"
    ws = ws_url_from_http(url)
    return (ws, None) if ws else (None, "research_url_unusable")


def chain_newest_migration(rpc):
    """(signature, blockTime) of the newest successful transaction mentioning pump's migration account,
    or None. `rpc(method, params)` returns the JSON-RPC result. One getSignaturesForAddress."""
    for row in rpc("getSignaturesForAddress", [PUMP_MIGRATION_ACCOUNT, {"limit": 5}]) or ():
        if not row.get("err") and row.get("signature"):
            return row["signature"], row.get("blockTime")
    return None


def start_migration_listeners(on_pool, *, sockets, status, chain_newest_fn=None, alert_fn=None,
                              stop=None, connect=None, clock=time, silence_s=MIGRATION_SILENCE_S,
                              backoff_s=1.0, watch=True):
    """One start_migration_thread per (name, ws url) in `sockets`, all feeding one MigrationFanIn. The
    FIRST socket's status is `status` itself (the heartbeat's existing prewarm fields); every other
    socket's is status[name]; a socket whose url is None is recorded as status[name] = {"disabled":
    reason}. status["pooled"] holds the fan-in's and the deafness watch's counters."""
    fanin = MigrationFanIn(on_pool, clock=clock)
    started = []
    for i, (name, (url, why)) in enumerate(sockets.items()):
        own = status if i == 0 else status.setdefault(name, {})
        if not url:
            own.update(disabled=why or "no_url")
            continue
        try:
            start_migration_thread(None, ws_url=url, status=own, stop=stop, connect=connect,
                                   silence_s=silence_s, backoff_s=backoff_s,
                                   on_frame=fanin.frame_fn(name))
            started.append(name)
        except ValueError as exc:                      # the funded-key refusal: that socket stays off
            own.update(disabled=str(exc)[:80])
    # Every started socket's key exists before its first frame, so write_heartbeat's dict(first_by)
    # never sees the dict grow under it (Gamma LOW): a frame only changes a value.
    with fanin._lock:
        for name in started:
            fanin.stats["first_by"].setdefault(name, 0)
    pooled = status["pooled"] = {"sockets": started, **{"fanin": fanin.stats}}
    if watch and chain_newest_fn is not None and started:
        deaf = MigrationDeafWatch(fanin, chain_newest_fn=chain_newest_fn, alert_fn=alert_fn, clock=clock)
        pooled["watch"] = deaf.stats
        threading.Thread(target=deaf.run, kwargs={"stop": stop}, name="slot-lane-migrations-deaf",
                         daemon=True).start()
    return fanin
