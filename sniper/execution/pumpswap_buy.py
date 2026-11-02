"""Direct PumpSwap (pAMM) BUY, funded from USDC inside one atomic transaction. BUILD ONLY.

The PumpSwap twin of pump_curve_buy.py for the slot lane (buy.venue == "pump_amm"). One transaction:
    compute budget -> Jupiter USDC -> SOL (wrapAndUnwrapSol FALSE: the output stays in
    the user's wSOL ATA, which the pool debits) -> ATA create-idempotent for the coin ->
    pAMM buy_exact_quote_in(spendable = the leg's MINIMUM out, min_base_amount_out from the event)
    -> close the wSOL ATA (unwraps the leg's surplus to lamports and returns its rent)

WHAT THE EVENT DOES NOT CARRY (pAMM on-chain IDL, slot 450784628). A BuyEvent names the pool and its
PRE-trade vault reserves, fees, coin_creator and the protocol fee recipient, but NOT the base mint,
the quote mint or the pool vaults. Those live in the Pool account, which `PumpSwapPoolCache` reads
once per pool OFF the signal path; a pool not yet learned refuses `pool_unknown` (operator ruling
via Alpha, 2026-09-26: miss-then-learn). Pools learned before a restart are re-read at start from
their persisted addresses (2026-09-27, Alpha). Since 2026-09-27 (Alpha, task 1) the cache also reads
every pool pump.fun MIGRATES into, heard from the migration account's logs, before its first buy.

THE PRICE NEEDS A FIELD THE IDL DOES NOT NAME. The Pool account is 301 bytes; the IDL's fields end at
byte 245, and the u64 at [245:253] is a VIRTUAL QUOTE reserve (17,584,505,596 lamports on the pool
read). The program prices against vault quote + that virtual amount. Without it the expected output
of a live buy was 7% high on one pool and 15% on another; with it, exact to the atomic unit on
both exact-in and exact-out buys (2026-09-26). Exact-in prices ONE UNIT LESS than the net quote,
as the curve does. Post-trade reserves: base - base_amount_out, quote + quote_amount_in_with_lp_fee
(checked on two consecutive buys of one pool).

25 OR 26 ACCOUNTS: the IDL's 23, then PDA ["pool-v2", base_mint] (read-only) only for a nonzero
coin creator, then a buyback fee recipient (read-only) and its quote-mint ATA (writable).
Only wSOL-quoted pools are built.

THE EVIDENCE (Lambda, 2026-10-07, Gamma FAIL c63be39c42dd). On a default-creator pool the program reads
remaining account [23] as the buyback fee recipient, so a pool-v2 PDA there reverted 6053
BuybackFeeRecipientNotAuthorized: Roveryaps 22hNSUXL (09-30 22:09Z) and probablyinsider 4g2ToCku (10-07
12:55Z). 43 source buys on 43 distinct pools: the 41 with a real coin_creator all carry 26 accounts with
pool-v2 at [23]; the 2 with the default creator both carry 25. Not the selector: pool index, bytes
243/244 and 245..301, the token program (SPL-Token 2Jrkp1PW takes 26), the quote mint, buy versus
buy_exact_quote_in. Accounts [0..22], creator_vault included, are identical in both layouts. The buyback
recipient is a membership check on GlobalConfig.buyback_fee_recipients (sources use every index 0..7).
tests/test_pumpswap_buy_layout.py pins four of those real instructions, two per layout.
"""
from __future__ import annotations

import base64
import logging
import math
import operator
import struct
import time

from sniper.execution.pump_curve_buy import (
    DEFAULT_COMPUTE_UNIT_LIMIT, DEFAULT_PRIORITY_FEE_MICROLAMPORTS, build_cu_limit,
    LEG_REFRESH_AFTER_S, MAX_LEG_AGE_S, DEFAULT_LEG_SLIPPAGE_BPS, SOLANA_PACKET_LIMIT, WSOL_MINT,
    EVENT_AVERAGE, _custody_rpc, _funded_jup_call, bounded_whole_average, buy_same_slot, capped_min_out, clip_atomic,
    hard_cap_average, select_priority_fee,
    keepalive_custody_rpc,
    leg_clip_refusal, leg_route_keys,
    prefetch_usdc_sol_leg, price_limit_trace, USDC_MINT,
)
from sniper.execution.pump_native_exit import (
    ATA_PROGRAM, FEE_PROGRAM, SYSTEM_PROGRAM, TOKEN_2022_PROGRAM, TOKEN_PROGRAM, _pda, _pk,
    _rpc_result, ata,
)

logger = logging.getLogger(__name__)

AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
BUY_EXACT_QUOTE_IN_DISCRIMINATOR = bytes.fromhex("c62e1552b4d9e870")
BUY_EVENT_DISCRIMINATOR = bytes.fromhex("67f4521f2cf57777")
AMM_FEE_CONFIG_SEED = bytes.fromhex("0c14defc825ec67694250818bb654065f4298d3156d571b4d4f8090c18e9a863")
POOL_ACCOUNT_DISCRIMINATOR = bytes.fromhex("f19a6d0411b16dbc")
POOL_VIRTUAL_QUOTE_OFFSET = 245
GLOBAL_CONFIG_BUYBACK_OFFSET = 643       # GlobalConfig.buyback_fee_recipients[0]
# GlobalConfig.protocol_fee_recipients[0]: discriminator 8, admin 32, lp_fee u64, protocol_fee u64,
# disable_flags u8, then the 8 recipients (2026-10-07: 3 of 3 live buys named a member of this list).
GLOBAL_CONFIG_PROTOCOL_RECIPIENTS_OFFSET = 57


# --- the source event ---------------------------------------------------------------------------

_BUY_EVENT_FIELDS = (
    ("timestamp", "i64"), ("base_amount_out", "u64"), ("max_quote_amount_in", "u64"),
    ("user_base_token_reserves", "u64"), ("user_quote_token_reserves", "u64"),
    ("pool_base_token_reserves", "u64"), ("pool_quote_token_reserves", "u64"),
    ("quote_amount_in", "u64"), ("lp_fee_basis_points", "u64"), ("lp_fee", "u64"),
    ("protocol_fee_basis_points", "u64"), ("protocol_fee", "u64"),
    ("quote_amount_in_with_lp_fee", "u64"), ("user_quote_amount_in", "u64"), ("pool", "pubkey"),
    ("user", "pubkey"), ("user_base_token_account", "pubkey"),
    ("user_quote_token_account", "pubkey"), ("protocol_fee_recipient", "pubkey"),
    ("protocol_fee_recipient_token_account", "pubkey"), ("coin_creator", "pubkey"),
    ("coin_creator_fee_basis_points", "u64"), ("coin_creator_fee", "u64"), ("track_volume", "bool"),
    ("total_unclaimed_tokens", "u64"), ("total_claimed_tokens", "u64"),
    ("current_sol_volume", "u64"), ("last_update_timestamp", "i64"),
    ("min_base_amount_out", "u64"), ("ix_name", "string"), ("cashback_fee_basis_points", "u64"),
    ("cashback", "u64"), ("buyback_fee_basis_points", "u64"), ("buyback_fee", "u64"),
)


def parse_buy_event(log_line):
    """pAMM BuyEvent from one `Program data:` line, as a dict. Raises ValueError."""
    from sniper.research.memecoin_opportunity.decoders import BorshReader, anchor_event_bytes
    raw = anchor_event_bytes(log_line)
    if raw[:8] != BUY_EVENT_DISCRIMINATOR:
        raise ValueError("not a PumpSwap BuyEvent")
    r = BorshReader(raw[8:])
    read = {"i64": r.i64, "u64": r.u64, "pubkey": r.pubkey, "bool": r.bool, "string": r.string}
    return {name: read[kind]() for name, kind in _BUY_EVENT_FIELDS}


EXACT_QUOTE_BUYS = ("buy_exact_quote_in", "buy_exact_quote_in_v2")


def buy_event_gross_quote(event):
    """Quote atomic the buyer paid, pool fees included, or None when the event contradicts itself.
    Pump's exact-quote events (buy_exact_quote_in and, since 2026-10-08, buy_exact_quote_in_v2)
    reverse the two legacy field meanings; either way the paid amount must equal
    quote_amount_in_with_lp_fee + protocol_fee + coin_creator_fee + cashback."""
    try:
        gross = int(event["quote_amount_in"] if event.get("ix_name") in EXACT_QUOTE_BUYS
                    else event["user_quote_amount_in"])
        cashback = int(event.get("cashback", 0))
        parts = (int(event["quote_amount_in_with_lp_fee"]) + int(event["protocol_fee"])
                 + int(event["coin_creator_fee"]) + cashback)
    except (KeyError, TypeError, ValueError):
        return None
    return gross if gross == parts and gross > 0 and cashback >= 0 else None


def post_trade_reserves(event):
    """(base, quote) vault reserves after the source buy; the event carries them PRE-trade."""
    quote_in = int(event["quote_amount_in_with_lp_fee"])
    if event.get("ix_name") in ("buy_v2", "buy_exact_quote_in_v2"):
        # v2 retains creator and non-buyback protocol fees in the pool's quote vault.
        gross, buyback = buy_event_gross_quote(event), int(event["buyback_fee"])
        if (gross is None or event["cashback"]
                or not 0 <= buyback <= int(event["protocol_fee"])):
            raise ValueError("v2 quote reserves unproved")
        quote_in = gross - buyback
    return (int(event["pool_base_token_reserves"]) - int(event["base_amount_out"]),
            int(event["pool_quote_token_reserves"]) + quote_in)


def total_fee_bps(event):
    """lp + protocol + coin creator (+ cashback). Buyback is a share of the protocol fee, not a fee."""
    return sum(int(event.get(k) or 0) for k in (
        "lp_fee_basis_points", "protocol_fee_basis_points", "coin_creator_fee_basis_points",
        "cashback_fee_basis_points"))


def source_price_usd(event, pool_facts, sol_usd, *, quote_decimals=9, quote_usd=None):
    """USD per whole base token after the source buy, or None.

    Post-trade vault quote plus the Pool account's virtual quote, over post-trade vault base in
    the base mint's OWN decimals (from the pool cache's mint read), the same basis the lane's
    admission prices on. None when the decimals are unknown: a guessed 6 is off by a power of ten
    on any other mint. A non-SOL quote passes its own decimals and USD price (USDC 1.0; an exotic
    quote the on-demand leg's rate)."""
    decimals = pool_facts.get("base_decimals")
    if decimals is None:
        return None
    try:
        base_post, quote_post = post_trade_reserves(event)
        price = ((int(quote_post) + int(pool_facts["virtual_quote"])) / 10 ** int(quote_decimals)
                 / (int(base_post) / 10 ** int(decimals))
                 * float(sol_usd if quote_usd is None else quote_usd))
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def expected_quote_out(base_in, base_reserve, quote_reserve, virtual_quote, fee_components):
    """Conservative sell output, fees rounded up separately and limited to the quote vault."""
    quote = int(quote_reserve) + int(virtual_quote)
    if int(base_in) <= 0 or quote <= 0 or int(base_reserve) <= 0:
        return 0
    gross = quote * int(base_in) // (int(base_reserve) + int(base_in))
    out = gross - sum(-(-gross * part // 10_000) for part in fee_components)
    return max(0, min(out, int(quote_reserve)))


def exotic_pool_round_trip(event, facts, leg):
    """Model our exact spend/buy/sell on the source's post-buy reserves, without I/O.

    Retaining only the net buy input in the quote vault understates sell proceeds: the LP fee
    also remains there. This is a conservative model, not an executable sell or a future fill.
    """
    try:
        base_post, quote_post = post_trade_reserves(event)
        vq = int(facts["virtual_quote"])
        fees = tuple(int(event.get(k) or 0) for k in (
            "lp_fee_basis_points", "protocol_fee_basis_points", "coin_creator_fee_basis_points",
            "cashback_fee_basis_points"))
        fee = sum(fees)
        spend, usdc_in = leg["min_out_lamports"], leg["usdc_atomic"]
        if (not all(type(x) is int and 0 < x <= 2 ** 64 - 1 for x in (spend, usdc_in))
                or base_post <= 0 or quote_post < 0 or vq < 0
                or any(part < 0 for part in fees) or not 0 <= fee < 10_000):
            raise ValueError("invalid pool or leg")
        tokens = expected_base_out(spend, base_post, quote_post, vq, fee)
        net = spend * 10_000 // (10_000 + fee) - 1
        base_after, quote_after = base_post - tokens, quote_post + net
        quote_back = expected_quote_out(tokens, base_after, quote_after, vq, fees)
        if tokens <= 0 or net <= 0 or quote_back <= 0:
            raise ValueError("empty model")
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"state": "unmeasured", "reason": "pool_or_leg_unreadable"}
    return {"state": "measured", "usdc_in_atomic": usdc_in, "quote_spend_atomic": spend,
            "tokens_atomic": tokens, "quote_back_atomic": quote_back, "fee_bps": fee,
            "reserves_post_source": [base_post, quote_post],
            "reserves_after_us": [base_after, quote_after], "virtual_quote": vq}


def exotic_round_trip(event, facts, leg, *, clock_now, max_age_s):
    """Use one fresh exact reverse minimum; never extrapolate a quote to a different input.

    A current modeled return below the warmed reverse input is unknown. A larger return keeps
    the cached USDC minimum unchanged; surplus quote tokens are deliberately worth zero here.
    """
    model = exotic_pool_round_trip(event, facts, leg)
    if model["state"] != "measured":
        return model
    reverse = None
    if facts.get("quote_mint") == USDC_MINT:
        usdc_back = model["quote_back_atomic"]
    else:
        reverse = leg.get("reverse")
        if not isinstance(reverse, dict):
            return {"state": "unmeasured", "reason": "no_reverse_leg"}
        try:
            stamps = (leg["fetched_at"], reverse["fetched_at"])
            if (not all(type(t) in (int, float) and math.isfinite(t)
                        and 0 <= clock_now - t <= max_age_s for t in stamps)
                    or stamps[0] != stamps[1]):
                return {"state": "unmeasured", "reason": "reverse_leg_stale"}
            amount, out, floor = (reverse[k] for k in ("in_atomic", "out_usdc_atomic", "min_usdc_atomic"))
            if (not all(type(x) is int and 0 < x <= 2 ** 64 - 1 for x in (amount, out, floor))
                    or floor > out or reverse["input_mint"] != facts["quote_mint"]
                    or reverse["output_mint"] != USDC_MINT
                    or reverse["usdc_atomic"] != model["usdc_in_atomic"]
                    or reverse["quote_spend_atomic"] != model["quote_spend_atomic"]):
                raise ValueError("reverse binding")
        except (KeyError, TypeError, ValueError, OverflowError):
            return {"state": "unmeasured", "reason": "reverse_leg_invalid"}
        if amount > model["quote_back_atomic"]:
            return {"state": "unmeasured", "reason": "reverse_input_exceeds_return"}
        usdc_back = floor
    return {**model, "cost_pct": (1.0 - usdc_back / model["usdc_in_atomic"]) * 100.0,
            "usdc_back_atomic": usdc_back,
            "forward_quote": {"in": model["usdc_in_atomic"], "out": int(leg.get("out_lamports") or 0),
                              "min_out": model["quote_spend_atomic"],
                              "context_slot": (leg.get("quote") or {}).get("contextSlot")},
            "reverse_quote": (None if reverse is None else {
                "in": reverse["in_atomic"], "out_usdc": reverse["out_usdc_atomic"],
                "min_usdc": reverse["min_usdc_atomic"], "context_slot": reverse.get("context_slot")}),
            "source_slot": event.get("slot")}


def expected_base_out(spend_quote, base_reserve, quote_reserve, virtual_quote, fee_bps):
    """base_amount_out of buy_exact_quote_in(spend_quote) against the given vault reserves plus the
    pool's virtual quote. Exact to the unit on live buys (net - 1, floor)."""
    net = int(spend_quote) * 10_000 // (10_000 + int(fee_bps)) - 1
    quote = int(quote_reserve) + int(virtual_quote)
    if net <= 0 or quote <= 0 or int(base_reserve) <= 0:
        return 0
    return int(base_reserve) * net // (quote + net)


# --- the pool --------------------------------------------------------------------------------

def parse_pool_account(data_b64):
    """Pool identities and its signed i128 virtual quote reserves, or ValueError."""
    from solders.pubkey import Pubkey
    raw = base64.b64decode(data_b64)
    if raw[:8] != POOL_ACCOUNT_DISCRIMINATOR or len(raw) < POOL_VIRTUAL_QUOTE_OFFSET + 16:
        raise ValueError("not a PumpSwap Pool account (%d bytes)" % len(raw))
    pk = lambda off: str(Pubkey.from_bytes(raw[off:off + 32]))  # noqa: E731
    return {"creator": pk(11), "base_mint": pk(43), "quote_mint": pk(75),
            "pool_base_token_account": pk(139),
            "pool_quote_token_account": pk(171), "coin_creator": pk(211),
            "virtual_quote": int.from_bytes(
                raw[POOL_VIRTUAL_QUOTE_OFFSET:POOL_VIRTUAL_QUOTE_OFFSET + 16], "little", signed=True)}


def mint_supply_and_decimals(data_b64):
    """{"base_supply": atomic int, "base_decimals": int} from an SPL / Token-2022 mint account
    (the base layout both programs share: supply u64 at 36, decimals u8 at 44)."""
    raw = base64.b64decode(data_b64)
    if len(raw) < 45:
        raise ValueError("mint account too short (%d bytes)" % len(raw))
    return {"base_supply": struct.unpack_from("<Q", raw, 36)[0], "base_decimals": raw[44]}


def is_pump_migrated_pool(facts):
    """True when the pool was created by pump.fun's migration: its creator is the pump program's
    ["pool-authority", base_mint] PDA (checked on a live pool, 2026-09-26). A structural test, not a
    mint-suffix guess: anyone can grind a vanity "...pump" mint."""
    from sniper.execution.pump_native_exit import PUMP_PROGRAM
    try:
        expected = _pda([b"pool-authority", bytes(_pk(facts["base_mint"]))], PUMP_PROGRAM)
    except (KeyError, ValueError):
        return False
    return str(expected) == str(facts.get("creator"))


# MIGRATION PREWARM (2026-09-27, Alpha task 1). pump.fun migrates about 700 coins a day into
# canonical wSOL PumpSwap pools; a listener on its migration account (memecoin_slot_signal.
# start_migration_thread) hands each new pool to `offer_stream`, so the lane knows it before its
# first buy. Kept in memory and on disk, newest last. 2,048 held only ~19 h (2026-10-06: ~85-105
# pools stored an hour), and 11 of 95 PumpSwap misses 2026-09-27..10-06 were pools the lane had
# seen 19 h to 8.5 d before; 8,192 (~3.5 d, 9 of those 11) costs a restart ~4x the warm reads,
# newest first (2026-10-07).
POOL_STORE_CAP = 8192      # most recently learned pools kept, in memory and across restarts
# Pools read per prefetch tick: ONE getMultipleAccounts (<= 100) plus its mints. That tick also
# refreshes the legs, balances and blockhash, and on Wi-Fi the custody RPC took ~4.2 s per call
# (2026-09-27 12:49:35Z), so a restart's warm takes one tick per 100 pools instead of stalling one.
WARM_PER_TICK = 100
# The persisted warm's pace on the custody key (2026-10-07, Alpha/Gamma on 096ffe442): 8,192 pools
# unpaced were ~250 calls at ~3.4 a second for ~70 s after every restart. Streamed pools and a
# miss's own learn are never paced.
WARM_MAX_CALLS_PER_S = 2.0
WARM_RETRY_MAX_S = 60.0     # a stuck batch's backoff cap on the custody RPC (SOL-1, Alpha 2026-10-07)
STREAM_RETRY_TICKS = 20    # ~10 s of ticks for the custody node to show a just-migrated pool
PREPARATION_BATCH_CAP = 64
PREPARATION_QUEUE_CAP = 64
PREPARATION_RETENTION_S = 2.0
_MULTIPLE_ACCOUNTS_MAX = 100
CREATE_POOL_EVENT_DISCRIMINATOR = bytes.fromhex("b1310cd2a076a774")


def create_pool_event(logs):
    """{index, creator, base_mint, quote_mint, pool, canonical, pump_migrated} from a transaction's logs, or None.

    pAMM's CreatePoolEvent, as `Program data:` in the migration's logs (verified on a live
    MigrateV2, 2026-09-27): index u16 at 16, creator at 18, base mint at 50, quote mint at 82, pool at
    173. `canonical` is a pump migration into a wSOL pool: index 0, quote wSOL, creator the
    ["pool-authority", base_mint] PDA. The event is only a READ TARGET: the cache stores a pool only
    after reading it from chain, and takes pump_migrated from the ACCOUNT's creator."""
    from solders.pubkey import Pubkey
    from sniper.execution.pump_native_exit import PUMP_PROGRAM
    for line in logs or ():
        if not line.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(line[len("Program data: "):])
        except ValueError:
            continue
        if raw[:8] != CREATE_POOL_EVENT_DISCRIMINATOR or len(raw) < 205:
            continue
        pk = lambda off: str(Pubkey.from_bytes(raw[off:off + 32]))  # noqa: E731
        ev = {"index": struct.unpack_from("<H", raw, 16)[0], "creator": pk(18),
              "base_mint": pk(50), "quote_mint": pk(82), "pool": pk(173)}
        authority = _pda([b"pool-authority", bytes(_pk(ev["base_mint"]))], PUMP_PROGRAM)
        ev["pump_migrated"] = ev["index"] == 0 and ev["creator"] == str(authority)
        ev["canonical"] = ev["pump_migrated"] and ev["quote_mint"] == WSOL_MINT
        return ev
    return None


def load_pool_addresses(path, cap=POOL_STORE_CAP):
    """The persisted pool addresses, oldest first, or [] when the file is absent or unreadable.
    Anything that is not a base58 32-byte address is skipped: the file names pools to re-read,
    it never supplies a fact."""
    import json
    from solders.pubkey import Pubkey
    try:
        with open(path) as fh:
            rows = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("pumpswap pool store unreadable path=%s; warming nothing", path, exc_info=True)
        return []
    if not isinstance(rows, list):
        logger.warning("pumpswap pool store is not a list path=%s; warming nothing", path)
        return []
    out = []
    for row in rows:
        try:
            if isinstance(row, str) and str(Pubkey.from_string(row)) == row and row not in out:
                out.append(row)
        except ValueError:
            continue
    return out[-int(cap):]


class PumpSwapPoolCache:
    """pool -> static pool facts. `get` (lane thread) never reads the chain; a miss is queued and
    `learn_pending` (prefetch thread) reads each queued pool ONCE, success or not, so a pool that
    is not a PumpSwap pool cannot make the prefetch thread re-read it every tick.

    ACROSS RESTARTS (2026-09-27). The cache lived only in memory, so every deploy sent pools the
    lane knew back to `pool_unknown` (hashbergers' pool 4TcaXZVY: learned 01:08Z, wiped by the
    01:30Z deploy, refused again 01:36:45Z). With `store_path`, the addresses of stored pools, and
    nothing else, are kept in a small JSON list; the first `learn_pending` re-reads all of them
    from chain in bulk (`warm`), through the same checks as `learn`. Every fact a build signs
    with therefore comes from chain, never from the file: a corrupt or edited file can cost a
    missed or an extra read, never a wrong mint, vault or virtual quote."""

    def __init__(self, *, rpc, store_path=None, store_cap=POOL_STORE_CAP, clock=time.monotonic,
                 on_pool_known=None):
        import collections
        import threading
        self.rpc = rpc
        self.on_pool_known = on_pool_known
        self._pools = {}
        self._pending = set()
        self._stream = {}                # pool -> ticks tried; the migration listener adds
        self._candidates = collections.OrderedDict()  # unsigned references, never usable facts
        self._candidate_seen = collections.OrderedDict()
        # pool -> corrected-UTC epoch the migration listener heard it created (decision features,
        # spec 2026-10-14). Measurement only; bounded by store_cap; on_migrated alone writes it.
        self._migrated_at = collections.OrderedDict()
        self._lock = threading.Lock()    # the lane and the listener add, the prefetch thread drains
        self._read_ms = collections.deque(maxlen=256)
        self._warm_acc = None
        self._warm_started_calls = 0     # warm's single prefetch owner; includes failed HTTP starts
        # For the heartbeat (slot_lane.prewarm); written on the prefetch thread only, except
        # `streamed`, which offer_stream counts under the lock.
        self.stats = {"known": 0, "streamed": 0, "stream_stored": 0, "stream_dropped": 0,
                      "read_ms_p50": None, "read_ms_p90": None, "warm_unresolved_streak": 0,
                      "warm_retry_n": 0, "prefetch_batches": 0,
                      "candidate_queued": 0, "candidate_duplicate": 0, "candidate_overflow": 0,
                      "candidate_invalid": 0, "candidate_expired": 0, "candidate_stored": 0,
                      "candidate_unavailable": 0, "candidate_calls": 0}
        self.store_path = store_path
        self.store_cap = int(store_cap)
        self._order = []                 # stored pools, learn order (prefetch thread only)
        self._warm = load_pool_addresses(store_path, self.store_cap) if store_path else []
        self._warm_retry = []            # unresolved persisted pools, read after _warm (SOL-1)
        self._warm_report = None
        self.clock = clock
        self._warm_next = 0.0            # no persisted warm before this (prefetch thread only)
        self._retry_next = 0.0           # no _warm_retry read before this (backoff by streak)

    def cached(self, pool):
        """Read published pool facts without queuing a miss or taking the learner lock."""
        return self._pools.get(str(pool))

    def get(self, pool):
        hit = self._pools.get(str(pool))
        if hit is None:
            with self._lock:
                self._pending.add(str(pool))
        return hit

    def publish_quote_snapshot(self, pool, *, expected_facts, value, read_at):
        """Publish one identity-bound parsed read; even an unsafe read supersedes old safety."""
        import copy
        from sniper.execution.raydium_cpmm_buy import epoch_now, quote_facts
        from sniper.execution.raydium_launchlab_buy import QUOTE_RECHECK_S
        if (type(read_at) not in (int, float) or not math.isfinite(read_at)
                or read_at < 0 or not 0 <= self.clock() - read_at < QUOTE_RECHECK_S):
            return None
        previous_stamp = expected_facts.get("quote_read_at") if isinstance(expected_facts, dict) else None
        if type(previous_stamp) in (int, float) and read_at < previous_stamp:
            return None
        try:
            value = copy.deepcopy(value)
            parsed = value["data"]["parsed"]
            decimals = parsed["info"]["decimals"]
            if (value["owner"] != expected_facts["quote_token_program"]
                    or parsed["type"] != "mint" or type(decimals) is not int
                    or decimals != expected_facts["quote_decimals"]
                    or not expected_facts.get("quote_mint")):
                return None
            # Missing hook/pause state is unknown, not proof the extension is inactive.
            extensions = parsed["info"].get("extensions", [])
            if not isinstance(extensions, list):
                return None
            for extension in extensions:
                name, state = extension["extension"], extension.get("state")
                if name in ("transferHook", "pausableConfig", "defaultAccountState"):
                    if not isinstance(state, dict):
                        return None
                    if name == "transferHook" and ("programId" not in state or
                            not (state["programId"] is None or
                                 type(state["programId"]) is str and state["programId"])):
                        return None
                    if name == "pausableConfig" and type(state.get("paused")) is not bool:
                        return None
                    if name == "defaultAccountState" and state.get("accountState") not in ("initialized", "frozen"):
                        return None
            safety = quote_facts(value, epoch=epoch_now())
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        snapshot = {**expected_facts, "mint_values": {expected_facts["quote_mint"]: value},
                    "quote_read_at": read_at, "quote_facts": safety}
        with self._lock:
            if self._pools.get(str(pool)) is not expected_facts:
                return None
            self._pools[str(pool)] = snapshot
        return snapshot

    def state(self, pool):
        """"known", "queued" (a learn is still due) or "unknown" (read and not stored). Ask it on
        the thread that runs `learn_pending`, after it: a learn in flight elsewhere reads unknown."""
        pool = str(pool)
        if pool in self._pools:
            return "known"
        with self._lock:
            return "queued" if pool in self._pending else "unknown"

    def offer_stream(self, pool):
        """A pool the migration listener heard being created; read from chain on the next ticks.
        Any thread. It never touches the miss queue, so `state` (the handoff's view) is unchanged."""
        pool = str(pool)
        with self._lock:
            if pool not in self._pools and pool not in self._stream:
                self._stream[pool] = 0
                self.stats["streamed"] += 1

    def note_migration(self, pool, observed_at):
        """When the migration listener heard this pool created. No read; any thread; bounded."""
        pool = str(pool)
        with self._lock:
            self._migrated_at.pop(pool, None)
            self._migrated_at[pool] = float(observed_at)
            while len(self._migrated_at) > max(1, self.store_cap):
                self._migrated_at.popitem(last=False)

    def migration_observed_at(self, pool):
        """The heard time, or None (learned another way, or evicted). Lock-free, like `cached`."""
        return self._migrated_at.get(str(pool))

    def offer_candidates(self, addresses, *, received_at):
        """Bounded unsigned references; only the existing prefetch owner may verify them."""
        from solders.pubkey import Pubkey
        try:
            addresses = tuple(addresses)
            valid = (0 < len(addresses) <= PREPARATION_BATCH_CAP
                     and len(addresses) == len(set(addresses))
                     and all(isinstance(key, str) and str(Pubkey.from_string(key)) == key for key in addresses))
        except (ValueError, TypeError):
            valid = False
        now = self.clock()
        with self._lock:
            if not valid:
                self.stats['candidate_invalid'] += 1
                return False
            if (type(received_at) not in (int, float) or not math.isfinite(received_at)
                    or not 0 <= now - received_at < PREPARATION_RETENTION_S):
                self.stats['candidate_expired'] += 1
                return False
            for key, stamp in tuple(self._candidate_seen.items()):
                if not 0 <= now - stamp < PREPARATION_RETENTION_S:
                    del self._candidate_seen[key]
            if addresses in self._candidate_seen:
                self.stats['candidate_duplicate'] += 1
                return False
            if len(self._candidates) >= PREPARATION_QUEUE_CAP:
                self.stats['candidate_overflow'] += 1
                return False
            self._candidates[addresses] = received_at
            self._candidate_seen[addresses] = received_at
            while len(self._candidate_seen) > PREPARATION_QUEUE_CAP * 2:
                self._candidate_seen.popitem(last=False)
            self.stats['candidate_queued'] += 1
            return True

    def learn_pending(self):
        stored = self._learn_streamed_and_persisted()
        with self._lock:
            queued, self._pending = self._pending, set()
        out = {}
        for pool in queued:
            try:
                out[pool] = self.learn(pool)
            except Exception:
                # A failed read must not skip the leg refresh that follows it; the next miss
                # queues the pool again.
                logger.warning("pumpswap pool learn failed pool=%s", pool, exc_info=True)
                out[pool] = False
        if stored or any(out.values()):
            self._save()
        self.stats["known"] = len(self._pools)
        # Retries never stop (SOL03, retry for ever): the dead set's size is the gauge (Gamma LOW).
        self.stats["warm_retry_n"] = len(self._warm_retry)
        if self._read_ms:
            ordered = sorted(self._read_ms)
            self.stats["read_ms_p50"] = ordered[len(ordered) // 2]
            self.stats["read_ms_p90"] = ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))]
        return out

    def _learn_streamed_and_persisted(self):
        """At most WARM_PER_TICK pools this tick, in ONE read: streamed (newest first), then
        persisted (newest first). A streamed pool the node does not show yet is re-read next tick,
        and dropped after STREAM_RETRY_TICKS. Returns how many pools were stored."""
        now = self.clock()
        candidate, candidate_stamp = (), None
        with self._lock:
            streamed = list(reversed(self._stream))[:WARM_PER_TICK]
            for refs, stamp in tuple(self._candidates.items()):
                if not 0 <= now - stamp < PREPARATION_RETENTION_S:
                    del self._candidates[refs]
                    self.stats['candidate_expired'] += 1
            if self._candidates and now >= self._warm_next:
                refs, stamp = next(iter(self._candidates.items()))
                if len(set(streamed).union(refs)) <= WARM_PER_TICK:
                    candidate, candidate_stamp = refs, stamp
                    del self._candidates[refs]
        candidate_new = set(candidate).difference(self._pools)
        persisted = []
        take = WARM_PER_TICK - len(set(streamed).union(candidate))
        if take > 0 and self._warm and now >= self._warm_next:
            persisted, self._warm = self._warm[-take:][::-1], self._warm[:-take]
        elif take > 0 and self._warm_retry and now >= max(self._warm_next, self._retry_next):
            persisted, self._warm_retry = self._warm_retry[-take:][::-1], self._warm_retry[:-take]
        if not streamed and not persisted and not candidate:
            return 0
        self.stats['prefetch_batches'] += 1
        calls_before = self._warm_started_calls
        try:
            report = self.warm(streamed + list(candidate) + persisted, persisted=persisted)
        except Exception:
            # Unavailable, not refused (SOL-1): every pool of the batch stays unresolved.
            logger.warning("pumpswap pool warm failed pools=%d", len(streamed) + len(candidate) + len(persisted),
                           exc_info=True)
            report = {"persisted": len(persisted), "stored": 0,
                      "calls": self._warm_started_calls - calls_before, "ms": 0.0,
                      "unresolved": streamed + list(candidate) + persisted}
        if candidate or persisted:
            self._warm_next = self.clock() + max(1, report['calls']) / WARM_MAX_CALLS_PER_S
        if candidate:
            unresolved = tuple(ref for ref in candidate if ref in set(report.get('unresolved') or ()))
            self.stats['candidate_stored'] += sum(ref in self._pools for ref in candidate_new)
            self.stats['candidate_unavailable'] += len(unresolved)
            self.stats['candidate_calls'] += report['calls']
            if unresolved and self.clock() - candidate_stamp < PREPARATION_RETENTION_S:
                with self._lock:
                    if len(self._candidates) < PREPARATION_QUEUE_CAP:
                        self._candidates[unresolved] = candidate_stamp
                    else:
                        self.stats['candidate_overflow'] += 1
        back = []
        if persisted:
            # AN UNAVAILABLE READ IS NOT A REFUSAL (SOL-1, Codex review 2026-10-07). This batch was cut
            # from _warm before its read, so a timeout or an incomplete answer dropped it at the next
            # save (150 stored -> 50). Its unresolved pools go BEHIND the other pending ones, so one
            # stuck batch never blocks them (Alpha); only a
            # complete read that refuses a pool drops it. They wait in their own queue, read only
            # after every first read, so a stuck batch never re-chunks with (and fails) the rest.
            # The streak shows a stuck batch.
            unresolved = set(report.get("unresolved") or ())
            back = [pool for pool in persisted if pool in unresolved]
            # The custody RPC is the funded read lane (Gamma LOW): a stuck batch's next retry waits
            # min(WARM_RETRY_MAX_S, pace x 2^streak); a complete read resets the streak. First reads
            # keep the normal pace, and nothing is dropped.
            if back:
                self._warm_retry = back[::-1] + self._warm_retry
                streak = self.stats["warm_unresolved_streak"] = self.stats["warm_unresolved_streak"] + 1
                pace = max(1, report["calls"]) / WARM_MAX_CALLS_PER_S
                self._retry_next = self.clock() + min(WARM_RETRY_MAX_S, pace * 2 ** min(streak, 32))
            else:
                self.stats["warm_unresolved_streak"] = 0
        with self._lock:
            for pool in streamed:
                if pool in self._pools:
                    del self._stream[pool]
                    self.stats["stream_stored"] += 1
                else:
                    self._stream[pool] += 1
                    if self._stream[pool] >= STREAM_RETRY_TICKS:
                        del self._stream[pool]
                        self.stats["stream_dropped"] += 1
        if persisted:
            done = {"persisted": len(persisted) - len(back), "calls": report["calls"], "ms": report["ms"],
                    "stored": sum(1 for pool in persisted if pool in self._pools),
                    "unresolved": len(back),
                    "unresolved_streak_max": self.stats["warm_unresolved_streak"]}
            acc = self._warm_acc or dict.fromkeys(done, 0)
            self._warm_acc = {key: (max if key == "unresolved_streak_max" else operator.add)(acc[key], done[key])
                              for key in done}
            if not self._warm and not self._warm_retry:
                self._warm_report, self._warm_acc = self._warm_acc, None
        return report["stored"]

    def warm(self, pools, persisted=()):
        """Re-read `pools` and their mints in bulk (<= 100 accounts per call) and store each one
        that passes the same checks as `learn`. Returns a report. Pools in `persisted` come from
        the store newest first and are older than anything learned live, so each goes to the FRONT
        of the recency order; the file then keeps its order across restarts and eviction stays
        oldest-first. A pool whose pool or mint read came back null, missing or short is reported
        "unresolved" (unavailable), never refused (SOL-1), and so is one whose OWN item in a
        full-length answer is null (review SOL03, 2026-10-08, operator: retry for ever on the
        _warm_retry backoff, never retired). A wrong owner, an unreadable layout or an unsupported
        token program is still a definitive drop."""
        persisted = set(persisted)
        started, calls = time.monotonic(), 0
        pools = [p for p in dict.fromkeys(str(x) for x in pools) if p not in self._pools]
        parsed, unresolved = {}, []
        for i in range(0, len(pools), _MULTIPLE_ACCOUNTS_MAX):
            chunk = pools[i:i + _MULTIPLE_ACCOUNTS_MAX]
            self._warm_started_calls += 1
            values = self._multiple(chunk)
            calls += 1
            if len(values) != len(chunk):
                unresolved.extend(chunk)        # a null, missing or short answer is unavailable (SOL-1)
                continue
            for pool, acc in zip(chunk, values):
                if acc is None:
                    unresolved.append(pool)     # this pool's own item is null: unavailable (SOL03)
                    continue
                if acc and acc.get("owner") == AMM_PROGRAM:
                    try:
                        parsed[pool] = parse_pool_account(acc["data"][0])
                    except (ValueError, KeyError, IndexError, TypeError):
                        continue
        mints = list(dict.fromkeys(m for f in parsed.values() for m in (f["base_mint"], f["quote_mint"])))
        accounts, missing = {}, set()
        for i in range(0, len(mints), _MULTIPLE_ACCOUNTS_MAX):
            chunk = mints[i:i + _MULTIPLE_ACCOUNTS_MAX]
            self._warm_started_calls += 1
            got = self._multiple(chunk)
            calls += 1
            if len(got) != len(chunk):
                missing.update(chunk)
                continue
            for mint, acc in zip(chunk, got):
                if acc is None:
                    missing.add(mint)           # a null mint item leaves its pools unresolved (SOL03)
                else:
                    accounts[mint] = acc
        stored = 0
        for pool, facts in parsed.items():
            if facts["base_mint"] in missing or facts["quote_mint"] in missing:
                unresolved.append(pool)
                continue
            try:
                stored += bool(self._store(pool, facts, accounts.get(facts["base_mint"]),
                                           accounts.get(facts["quote_mint"]),
                                           oldest=pool in persisted))
            except (ValueError, KeyError, IndexError, TypeError):
                continue
        return {"persisted": len(pools), "stored": stored, "calls": calls,
                "ms": (time.monotonic() - started) * 1000.0, "unresolved": unresolved}

    def take_warm_report(self):
        """The last warm's report, once; None after that or when there was none."""
        report, self._warm_report = self._warm_report, None
        return report

    def _multiple(self, addresses):
        started = time.monotonic()
        try:
            return (_rpc_result(self.rpc, "getMultipleAccounts", [
                list(addresses), {"encoding": "base64", "commitment": "confirmed"}]) or {}).get(
                    "value") or []
        finally:
            self._read_ms.append((time.monotonic() - started) * 1000.0)

    def _store(self, pool, facts, base_account, quote_account, oldest=False):
        """Complete and keep `facts` when both mints are SPL / Token-2022 accounts. True when kept."""
        owners = [(m or {}).get("owner") for m in (base_account, quote_account)]
        if not all(o in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) for o in owners):
            return False
        facts["pool"] = str(pool)
        facts["base_token_program"], facts["quote_token_program"] = owners
        facts.update(mint_supply_and_decimals(base_account["data"][0]))
        # A non-SOL quote (USDC, xStocks, launchpad tokens; exotic quotes live 2026-10-02) prices in
        # its own decimals; unreadable stays unknown (None), and only a non-SOL build needs it.
        try:
            facts["quote_decimals"] = (9 if facts.get("quote_mint") == WSOL_MINT else
                                       mint_supply_and_decimals(quote_account["data"][0])["base_decimals"])
        except (KeyError, TypeError, ValueError):
            facts["quote_decimals"] = None
        facts["pump_migrated"] = is_pump_migrated_pool(facts)
        # Two writers since 2026-09-27: the prefetch thread and the lane's miss learner.
        with self._lock:
            old = self._pools.get(str(pool))
            if old and all(old.get(key) == val for key, val in facts.items()):
                for key in ("mint_values", "quote_read_at", "quote_facts"):
                    if key in old:
                        facts[key] = old[key]
            self._pools[str(pool)] = facts
            if str(pool) in self._order:
                self._order.remove(str(pool))
            if oldest:
                self._order.insert(0, str(pool))
            else:
                self._order.append(str(pool))
            while len(self._order) > self.store_cap:     # the oldest learned pool is forgotten too
                self._pools.pop(self._order.pop(0), None)
        if not oldest and self.on_pool_known is not None:
            try:
                self.on_pool_known(str(pool), facts)
            except Exception:
                self.stats["pool_known_callback_failed"] = self.stats.get("pool_known_callback_failed", 0) + 1
        return True

    def _save(self):
        """Persist the stored pools' addresses, newest last, capped. Best-effort: a failed write
        costs the next restart a re-learn, never the lane a tick.

        Persisted addresses the restart warm has not read yet stay in the file, as the oldest (Codex
        N4, 2026-10-06: the first warmed batch rewrote the store with only the pools read so far, so
        a second restart before the warm finished forgot the rest). An address is dropped only once
        it has been read and refused. Prefetch thread only, as is `_warm`."""
        if not self.store_path:
            return
        with self._lock:
            order = [a for a in self._warm_retry + self._warm if a not in self._pools] + self._order
            order = order[-self.store_cap:]
        try:
            from sniper.util.atomic import atomic_write_json
            atomic_write_json(self.store_path, order)
        except Exception:
            logger.warning("pumpswap pool store write failed path=%s", self.store_path, exc_info=True)

    def keep_warm(self):
        """One bounded ping on the pool-read connection so a miss finds it open; nothing when the
        reader has no ping (urllib or a test's). Never raises: a failed ping only costs the next
        read a handshake."""
        ping = getattr(self.rpc, "ping", None)
        if ping is None:
            return
        try:
            ping()
        except Exception:
            self.stats["keep_warm_failed"] = self.stats.get("keep_warm_failed", 0) + 1

    def learn(self, pool):
        """Read the Pool account and its mints' owners (token programs). True when stored.

        Both reads at `confirmed`: a pool migrated seconds ago does not exist yet at the node's
        FINALIZED default (~32 slots behind), and a pool read as absent keeps refusing pool_unknown
        (2026-09-26, the default 210ac150 fixed for the mint read)."""
        acc = (_rpc_result(self.rpc, "getAccountInfo", [
            str(pool), {"encoding": "base64", "commitment": "confirmed"}]) or {}).get("value")
        if not acc or acc.get("owner") != AMM_PROGRAM:
            return False
        facts = parse_pool_account(acc["data"][0])
        mints = self._multiple([facts["base_mint"], facts["quote_mint"]])
        if len(mints) != 2:
            return False
        return self._store(pool, facts, mints[0], mints[1])


# --- the instruction ---------------------------------------------------------------------------

def buy_exact_quote_in_data(spendable_quote, min_base_out, track_volume=False):
    spend, floor = int(spendable_quote), int(min_base_out)
    if spend <= 0 or floor <= 0:
        raise ValueError("buy_exact_quote_in needs a positive spend and a positive floor")
    return BUY_EXACT_QUOTE_IN_DISCRIMINATOR + struct.pack("<QQ?", spend, floor, bool(track_volume))


def pumpswap_buy_accounts(*, user, pool, pool_facts, protocol_fee_recipient,
                          protocol_fee_recipient_token_account, buyback_fee_recipient):
    """pAMM buy_exact_quote_in: IDL order, optional pool-v2, then the two buyback accounts."""
    user, pool = _pk(user), _pk(pool)
    base, quote = _pk(pool_facts["base_mint"]), _pk(pool_facts["quote_mint"])
    btp, qtp = _pk(pool_facts["base_token_program"]), _pk(pool_facts["quote_token_program"])
    creator = _pk(pool_facts["coin_creator"])
    vault_auth = _pda([b"creator_vault", bytes(creator)], AMM_PROGRAM)
    accounts = [
        (pool, False, True),
        (user, True, True),
        (_pda([b"global_config"], AMM_PROGRAM), False, False),
        (base, False, False),
        (quote, False, False),
        (ata(user, base, btp), False, True),
        (ata(user, quote, qtp), False, True),
        (_pk(pool_facts["pool_base_token_account"]), False, True),
        (_pk(pool_facts["pool_quote_token_account"]), False, True),
        (_pk(protocol_fee_recipient), False, False),
        (_pk(protocol_fee_recipient_token_account), False, True),
        (btp, False, False),
        (qtp, False, False),
        (_pk(SYSTEM_PROGRAM), False, False),
        (_pk(ATA_PROGRAM), False, False),
        (_pda([b"__event_authority"], AMM_PROGRAM), False, False),
        (_pk(AMM_PROGRAM), False, False),
        (ata(vault_auth, quote, qtp), False, True),
        (vault_auth, False, False),
        (_pda([b"global_volume_accumulator"], AMM_PROGRAM), False, False),
        (_pda([b"user_volume_accumulator", bytes(user)], AMM_PROGRAM), False, True),
        (_pda([b"fee_config", AMM_FEE_CONFIG_SEED], FEE_PROGRAM), False, False),
        (_pk(FEE_PROGRAM), False, False),
    ]
    if str(creator) != SYSTEM_PROGRAM:
        accounts.append((_pda([b"pool-v2", bytes(base)], AMM_PROGRAM), False, False))
    return accounts + [
        (_pk(buyback_fee_recipient), False, False),
        (ata(buyback_fee_recipient, quote, qtp), False, True),
    ]


def pumpswap_buy_instructions(event, pool_facts, spend_quote, slippage_pct, *, user,
                              buyback_fee_recipient, source_price_usd=None,
                              source_cost_usdc_atomic=None, source_price_cap_pct=None,
                              close_wsol=True, allow_non_sol=False, whole_average_usd=EVENT_AVERAGE,
                              source_average_usd=None, copy_cap_pct=None, same_slot=False):
    """[create coin ATA, buy_exact_quote_in, close wSOL ATA] and the amounts. ValueError on refusal.
    `source_average_usd` replaces the average read from `event`: a graduation buy's event is the
    pool's state, not the source's trade, so the source's own curve average is passed in.
    The minimum out is `capped_min_out` in the pool's base decimals, the curve's contract. A non-SOL
    pool builds only with `allow_non_sol` (the builder's listed-venue gate) and never closes wSOL:
    the quote is spent from the user's own quote account."""
    from solders.instruction import AccountMeta, Instruction
    non_sol = pool_facts["quote_mint"] != WSOL_MINT
    if non_sol and not allow_non_sol:
        raise ValueError("pool is quoted in %s, not wSOL" % pool_facts["quote_mint"])
    base_res, quote_res = post_trade_reserves(event)
    expected = expected_base_out(spend_quote, base_res, quote_res, pool_facts["virtual_quote"],
                                 total_fee_bps(event))
    decimals = pool_facts.get("base_decimals")
    if source_price_cap_pct is not None and decimals is None:
        raise ValueError("base decimals unknown")
    from sniper.settings import memecoin as cfg
    average = None
    if cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT > 0:
        if source_average_usd is not None:
            average = float(source_average_usd)
        else:
            try:
                average = (float(source_price_usd) * buy_event_gross_quote(event) * base_res
                           / (int(event['base_amount_out']) * (quote_res + int(pool_facts['virtual_quote']))))
            except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
                raise ValueError('source average price unavailable') from exc
        # A routed receipt's whole average replaces the venue's on BOTH branches (review SOL01,
        # operator option (a) 2026-10-08): a graduation's explicit curve average skipped it, so a split
        # route signed its floor on the dearer curve leg. Bounded as every other builder; None refuses.
        if whole_average_usd is not EVENT_AVERAGE:
            average = bounded_whole_average(whole_average_usd, average)
    min_out, output_floor, price_floor = capped_min_out(
        expected, slippage_pct, source_price_usd=source_price_usd,
        source_cost_usdc_atomic=source_cost_usdc_atomic, source_price_cap_pct=source_price_cap_pct,
        base_decimals=decimals if decimals is not None else 0, source_average_price_usd=average,
        copy_cap_pct=copy_cap_pct, same_slot=same_slot)
    user_pk, base = _pk(user), _pk(pool_facts["base_mint"])
    btp, qtp = _pk(pool_facts["base_token_program"]), _pk(pool_facts["quote_token_program"])
    create_ata = Instruction(_pk(ATA_PROGRAM), bytes([1]), [      # 1 = CreateIdempotent
        AccountMeta(user_pk, True, True), AccountMeta(ata(user_pk, base, btp), False, True),
        AccountMeta(user_pk, False, False), AccountMeta(base, False, False),
        AccountMeta(_pk(SYSTEM_PROGRAM), False, False), AccountMeta(btp, False, False)])
    metas = [AccountMeta(k, s, w) for k, s, w in pumpswap_buy_accounts(
        user=user_pk, pool=event["pool"], pool_facts=pool_facts,
        protocol_fee_recipient=event["protocol_fee_recipient"],
        protocol_fee_recipient_token_account=event["protocol_fee_recipient_token_account"],
        buyback_fee_recipient=buyback_fee_recipient)]
    buy = Instruction(_pk(AMM_PROGRAM), buy_exact_quote_in_data(spend_quote, min_out), metas)
    wsol_ata = ata(user_pk, WSOL_MINT, qtp)
    close = Instruction(qtp, bytes([9]), [                            # 9 = CloseAccount
        AccountMeta(wsol_ata, False, True), AccountMeta(user_pk, False, True),
        AccountMeta(user_pk, True, False)])
    return [create_ata, buy] + ([close] if close_wsol and not non_sol else []), {"spend_lamports": int(spend_quote),
                                      "expected_tokens_out": expected,
                                      "output_floor_atomic": output_floor,
                                      "source_price_floor_atomic": price_floor,
                                      "min_tokens_out": min_out, "mint": pool_facts["base_mint"],
                                      "source_average_price_usd": average}


def build_pumpswap_buy_message(event, pool_facts, leg, *, user, buyback_fee_recipient, blockhash,
                               slippage_pct, compute_unit_limit=None,
                               priority_fee_microlamports=None, source_price_usd=None,
                               source_price_cap_pct=None, close_wsol=True, tip=None,
                               allow_non_sol=False, whole_average_usd=EVENT_AVERAGE,
                               source_average_usd=None, copy_cap_pct=None, lookup_tables=(), same_slot=False):
    """UNSIGNED MessageV0 (payer = user) and amounts; ValueError over the packet or on refusal.
    `tip` = (tip account, lamports) appends the Sender A/B tip transfer last (solana_sender_ab)."""
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.null_signer import NullSigner
    from solders.transaction import VersionedTransaction
    buy_ixs, amounts = pumpswap_buy_instructions(
        event, pool_facts, leg["min_out_lamports"], slippage_pct, user=user,
        buyback_fee_recipient=buyback_fee_recipient, source_price_usd=source_price_usd,
        source_cost_usdc_atomic=leg["usdc_atomic"], source_price_cap_pct=source_price_cap_pct,
        close_wsol=close_wsol, allow_non_sol=allow_non_sol, whole_average_usd=whole_average_usd,
        source_average_usd=source_average_usd, copy_cap_pct=copy_cap_pct, same_slot=same_slot)
    ixs = [set_compute_unit_limit(int(compute_unit_limit or DEFAULT_COMPUTE_UNIT_LIMIT)),
           set_compute_unit_price(int(DEFAULT_PRIORITY_FEE_MICROLAMPORTS if priority_fee_microlamports is None
                                      else priority_fee_microlamports))]
    leg_ixs = list(leg["ixs"])
    ixs += leg_ixs + buy_ixs
    if tip:
        from sniper.execution.solana_sender_ab import tip_instructions
        ixs += tip_instructions(user, tip)
    message = MessageV0.try_compile(_pk(user), ixs, leg["tables"], Hash.from_string(blockhash))
    size = len(bytes(VersionedTransaction(message, [NullSigner(_pk(user))])))
    tables = {table.key: table for table in leg["tables"]}
    extra = []
    for table in lookup_tables:
        if table.key in tables:
            if table != tables[table.key]:
                raise ValueError("slot_lane_lookup_table_conflict")
        else:
            tables[table.key] = table
            extra.append(table)
    if extra:
        # A held table can enlarge a packet when its keys already occur in the funding leg.
        compact = MessageV0.try_compile(_pk(user), ixs, list(leg["tables"]) + extra,
                                        Hash.from_string(blockhash))
        compact_size = len(bytes(VersionedTransaction(compact, [NullSigner(_pk(user))])))
        if compact_size < size:
            message, size = compact, compact_size
    if size > SOLANA_PACKET_LIMIT:
        raise ValueError("transaction is %d bytes, over the %d-byte packet" % (size, SOLANA_PACKET_LIMIT))
    if pool_facts["quote_mint"] != WSOL_MINT:
        protected = {_pk(event["pool"]), _pk(pool_facts["pool_base_token_account"]),
                     _pk(pool_facts["pool_quote_token_account"])}
        # Writability is global in the compiled message, including readonly prefix metas.
        amounts["funding_prefix_disjoint"] = not any(meta.pubkey in protected
            for ix in leg_ixs for meta in ix.accounts)
    return message, {"bytes": size, **amounts}


def buyback_fee_recipient_from_global_config(data_b64):
    from solders.pubkey import Pubkey
    raw = base64.b64decode(data_b64)
    return str(Pubkey.from_bytes(raw[GLOBAL_CONFIG_BUYBACK_OFFSET:GLOBAL_CONFIG_BUYBACK_OFFSET + 32]))


def protocol_fee_recipient_from_global_config(data_b64):
    from solders.pubkey import Pubkey
    raw = base64.b64decode(data_b64)
    off = GLOBAL_CONFIG_PROTOCOL_RECIPIENTS_OFFSET
    return str(Pubkey.from_bytes(raw[off:off + 32]))


def graduation_event(state, pool, protocol_fee_recipient, quote_mint=WSOL_MINT, quote_token_program=TOKEN_PROGRAM):
    """A BuyEvent-shaped view of a canonical pool's CURRENT state for a graduation buy: the vault
    reserves as pre-trade reserves with no trade (so post-trade = now), the fee split read_native_state
    chose by the program's own tier rule, and a protocol fee recipient from GlobalConfig. It prices our
    clip only; the caps still come from the source's curve trade. A coin/<quote> pool (a quote-token
    curve) pays the protocol fee into the recipient's <quote> account; the defaults are wSOL's."""
    lp, protocol, creator = (int(x) for x in state["fees"])
    return {"pool": str(pool), "pool_base_token_reserves": int(state["base_reserve"]),
            "pool_quote_token_reserves": int(state["quote_reserve"]), "base_amount_out": 0,
            "quote_amount_in_with_lp_fee": 0, "lp_fee_basis_points": lp,
            "protocol_fee_basis_points": protocol, "coin_creator_fee_basis_points": creator,
            "cashback_fee_basis_points": 0, "protocol_fee_recipient": str(protocol_fee_recipient),
            "protocol_fee_recipient_token_account": str(ata(_pk(protocol_fee_recipient), _pk(quote_mint),
                                                            _pk(quote_token_program)))}


class SlotLanePumpSwapBuilder:
    """The slot lane's PumpSwap builder. Same interface as SlotLaneCurveBuilder; its leg stays wSOL.

    `refresh(clip_usd, snapshot)` keeps the leg warm AND learns every pool a build missed, both on the
    prefetch thread. `build` makes no network call."""

    def __init__(self, *, user, jup_call=None, rpc=None, leg_slippage_bps=DEFAULT_LEG_SLIPPAGE_BPS,
                 refresh_after_s=LEG_REFRESH_AFTER_S, max_leg_age_s=MAX_LEG_AGE_S,
                 compute_unit_limit=None, priority_fee_microlamports=None, clock=time.monotonic,
                 pool_store_path=None, priority_fee_fn=None):
        self.user = str(user)
        self.jup_call = jup_call or _funded_jup_call
        self.rpc = rpc or _custody_rpc
        # Pool facts read on one kept-alive connection (a miss learns inside 250 ms); an injected
        # rpc reads everything, as before.
        self.pools = PumpSwapPoolCache(rpc=rpc or keepalive_custody_rpc(), store_path=pool_store_path)
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
        self._protocol_recipient = None
        self._config_read_at, self._config_rereading = None, False
        self.last_build = None

    def _read_global_config(self):
        """One GlobalConfig read sets both recipients; an unset (zero) slot sets neither."""
        acc = (_rpc_result(self.rpc, "getAccountInfo", [
            str(_pda([b"global_config"], AMM_PROGRAM)), {"encoding": "base64"}]) or {}).get("value")
        if acc:
            buyback = buyback_fee_recipient_from_global_config(acc["data"][0])
            protocol = protocol_fee_recipient_from_global_config(acc["data"][0])
            if SYSTEM_PROGRAM not in (buyback, protocol):
                self._buyback, self._protocol_recipient = buyback, protocol

    def refresh(self, clip_usd, snapshot=None, *, alternate_clip_usd=None, extra_clip_usds=()):
        self.pools.learn_pending()
        from sniper.execution.pump_curve_buy import keep_fee_config_fresh, refresh_cash_legs
        keep_fee_config_fresh(self, self._read_global_config)
        return refresh_cash_legs(self, clip_usd, alternate_clip_usd, unwrap=False,
                                 prefetch_fn=prefetch_usdc_sol_leg, extra_clip_usds=extra_clip_usds)

    def matching_leg(self, clip_usd):
        from sniper.execution.pump_curve_buy import matching_cash_leg
        return matching_cash_leg(self, clip_usd)

    REASON_PREFIX = "slot_lane_pump"

    def ondemand_quote(self, buy):
        """(quote mint, (decimals, 0, 0) or None, refusal or None) for the on-demand leg worker, on
        the WORKER's thread: an unknown pool is learned here. The live worker re-reads the quote mint
        itself (transfer fee, pause, hook), so the cached decimals are all this returns."""
        try:
            event = parse_buy_event(getattr(buy, "trade_event_log", None) or "")
        except ValueError:
            return None, None, "slot_lane_event_unreadable"
        facts = self.pools.get(event["pool"])
        if facts is None:
            try:
                self.pools.learn(event["pool"])
            except Exception:
                return None, None, "pool_unknown"
            facts = self.pools.get(event["pool"])
            if facts is None:
                return None, None, "pool_unknown"
        qdec = facts.get("quote_decimals")
        return facts.get("quote_mint"), ((int(qdec), 0, 0) if qdec is not None else None), None

    def _non_sol_funding(self, facts, clip_usd, funding_lamports, leg, quote):
        """(leg, quote decimals, quote USD) for a pool quoted in anything but SOL, or the refusal.
        USDC spends the clip straight from the wallet's USDC account (no leg, quote USD 1.0); any
        other quote spends the on-demand leg's minimum out, priced at that leg's own rate. Only when
        "pump_amm" is on MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES (spec 2026-10-02 exotic quotes)."""
        from sniper.settings import memecoin as cfg
        quote_mint = facts.get("quote_mint")
        if "pump_amm" not in (getattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", None) or ()):
            return "pool is quoted in %s, not wSOL" % quote_mint
        if funding_lamports is not None:
            return "native_funding_not_supported_on_a_non_sol_pool"
        if facts.get("quote_token_program") not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            return "quote token program unsupported"
        qdec = facts.get("quote_decimals")
        if qdec is None:
            return "quote_decimals_unknown"
        if quote_mint == USDC_MINT:
            spend = clip_atomic(clip_usd)
            return {"ixs": [], "tables": [], "usdc_atomic": spend, "min_out_lamports": spend,
                    "fetched_at": None}, int(qdec), 1.0
        if leg is None or quote is None:
            return "exotic_leg_missing"
        if leg.get("output_mint") != quote_mint:
            return "leg_wrong_quote"
        stamp = leg.get("fetched_at")
        if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                or not 0 <= self.clock() - stamp <= self.max_leg_age_s):
            return "leg_stale"
        refused = leg_clip_refusal(leg, clip_usd)
        if refused:
            return refused
        if (getattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_ROUND_TRIP", True)
                and leg.get("usdc_atomic") != clip_atomic(clip_usd)):
            return "exotic_round_trip_clip_mismatch"
        if int(quote[0]) != int(qdec):
            return "quote_decimals_mismatch"
        return leg, int(qdec), float(quote[1])

    accepts_priority_lamports = True     # memecoin_slot_lane.dispatch_build passes the crowded fee

    def build(self, buy, clip_usd, snapshot, *, funding_lamports=None, tip=None, leg=None, quote=None,
              priority_lamports=None, lookup_tables=()):
        """(unsigned MessageV0, {"blockhash", "last_valid_height"}) or (None, reason). `leg` and
        `quote` (decimals, USD, fee bps, fee max) come from the on-demand worker for an exotic
        quote; a USDC pool needs neither. `priority_lamports`: a treated crowded send's priority fee."""
        from sniper.settings import memecoin as cfg
        # The pool is looked up FIRST, so a miss is queued for learning even when this build fails
        # for another reason (a stale leg on the first signal would otherwise never teach the pool).
        line = getattr(buy, "trade_event_log", None)
        if not line:
            return None, "no_trade_event"
        # A GRADUATION buy (memecoin_slot_graduation): the buy is the source's CURVE trade, the pool is
        # the canonical one the watcher read. The pool's state prices our clip; the curve trade prices
        # the caps, exactly as the curve buy it replaces would have.
        grad = getattr(buy, "graduation", None)
        if grad is not None:
            if self._protocol_recipient is None:
                return None, "no_protocol_fee_recipient"
            facts = grad["pool_facts"]
            if facts.get("quote_mint") in (None, WSOL_MINT):
                event = graduation_event(grad["state"], grad["pool"], self._protocol_recipient)
            else:
                event = graduation_event(grad["state"], grad["pool"], self._protocol_recipient,
                                         facts["quote_mint"], facts.get("quote_token_program"))
        else:
            try:
                event = parse_buy_event(line)
            except ValueError as exc:
                return None, str(exc)[:120]
            if getattr(buy, "pool", None) not in (None, event["pool"]):
                return None, "event_pool_mismatch"
            facts = self.pools.get(event["pool"])
            if facts is None:
                return None, "pool_unknown"
            if event.get("protocol_fee_recipient") == SYSTEM_PROGRAM:
                # A buy_exact_quote_in_v2 source (TARDIGRADE 653VF487, 2026-10-08) keeps its fees in the
                # pool and names no recipient; our v1 buy must name a GlobalConfig member (v1 2Auf5yTD
                # filled on the same pool naming one).
                if self._protocol_recipient is None or not facts.get("quote_token_program"):
                    return None, "no_protocol_fee_recipient"
                event = dict(event, protocol_fee_recipient=self._protocol_recipient,
                             protocol_fee_recipient_token_account=str(ata(
                                 _pk(self._protocol_recipient), _pk(facts["quote_mint"]),
                                 _pk(facts["quote_token_program"]))))
        non_sol = facts.get("quote_mint") != WSOL_MINT
        quote_decimals, quote_usd = 9, None
        if non_sol:
            got = self._non_sol_funding(facts, clip_usd, funding_lamports, leg, quote)
            if isinstance(got, str):
                return None, got
            leg, quote_decimals, quote_usd = got
        elif facts.get("quote_token_program") != TOKEN_PROGRAM:
            return None, "quote token program is not the supported wSOL program"
        elif funding_lamports is None:
            leg = leg if leg is not None else self.matching_leg(clip_usd) or self._leg
            if leg is None or self.clock() - leg["fetched_at"] > self.max_leg_age_s:
                return None, "leg_stale"
            refused = leg_clip_refusal(leg, clip_usd)
            if refused:
                return None, refused
        else:
            try:
                spend = int(funding_lamports)
                sol_usd = float(snapshot.sol_usd)
                clip = float(clip_usd)
                if (spend <= 0 or not math.isfinite(sol_usd) or sol_usd <= 0
                        or not math.isfinite(clip) or clip <= 0
                        or not .90 * clip <= spend / 1e9 * sol_usd <= clip):
                    return None, "native_funding_outside_clip"
                from solders.instruction import AccountMeta, Instruction
                from solders.system_program import transfer
                user_pk, wsol = _pk(self.user), _pk(WSOL_MINT)
                wsol_ata = ata(user_pk, wsol, _pk(TOKEN_PROGRAM))
                wrap = [
                    Instruction(_pk(ATA_PROGRAM), bytes([1]), [
                        AccountMeta(user_pk, True, True), AccountMeta(wsol_ata, False, True),
                        AccountMeta(user_pk, False, False), AccountMeta(wsol, False, False),
                        AccountMeta(_pk(SYSTEM_PROGRAM), False, False),
                        AccountMeta(_pk(TOKEN_PROGRAM), False, False)]),
                    transfer({"from_pubkey": user_pk, "to_pubkey": wsol_ata, "lamports": spend}),
                    Instruction(_pk(TOKEN_PROGRAM), bytes([17]), [
                        AccountMeta(wsol_ata, False, True)]),
                ]
                leg = {"ixs": wrap, "tables": [], "usdc_atomic": clip_atomic(spend / 1e9 * sol_usd),
                       "min_out_lamports": spend, "fetched_at": self.clock()}
            except (TypeError, ValueError, OverflowError):
                return None, "native_funding_invalid"
        if self._buyback is None:
            return None, "no_buyback_recipient"
        if not getattr(snapshot, "blockhash", None):
            return None, "no_blockhash"
        if facts.get("base_decimals") is None:
            return None, "base_decimals_unknown"
        grad_average = None
        if grad is not None:
            from sniper.execution.pump_curve_buy import (
                curve_source_average_usd, curve_source_price_usd, parse_trade_event)
            try:
                curve = parse_trade_event(line)
                if curve["mint"] != facts["base_mint"]:
                    return None, "graduation_mint_mismatch"
                if non_sol:
                    # A quote-token curve (HI EtVFMDoB): its pool is coin/<the curve's quote>, and the
                    # source's price is its curve trade at the fresh leg's quote USD, as the curve buy
                    # would have priced it (quote_curve_prices: post-trade, and the average).
                    from sniper.execution.pump_curve_buy import quote_curve_prices
                    if curve.get("quote_mint") != facts.get("quote_mint"):
                        return None, "graduation_quote_mismatch"
                    price, average = quote_curve_prices(curve, quote_decimals, quote_usd,
                                                        facts["base_decimals"])
                    if price is not None and cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT > 0:
                        grad_average = average
                else:
                    price = curve_source_price_usd(curve, getattr(snapshot, "sol_usd", None))
                    if price is not None and cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT > 0:
                        grad_average = curve_source_average_usd(curve, price)
            except (KeyError, ValueError) as exc:
                return None, str(exc)[:120]
        else:
            price = source_price_usd(event, facts, getattr(snapshot, "sol_usd", None),
                                     quote_decimals=quote_decimals, quote_usd=quote_usd)
        if price is None:
            return None, "source_price_unreadable"
        cap_pct = cfg.solana_wallet_chase_cap_pct(
            getattr(buy, "source_user_id", None), cfg.MEMECOIN_REAL_CHASE_MAX_PCT)
        copy_cap_pct = cfg.copy_premium_cap(getattr(buy, "source_user_id", None), "solana")[0]
        try:
            cu_limit, cu_class, cu_key = build_cu_limit(
                self, "pump_amm",
                "exotic" if non_sol else "leg" if funding_lamports is None else "native",
                leg=leg, token_program=facts.get("base_token_program"),
                native_variant=("26_accounts" if facts.get("coin_creator")
                                and facts["coin_creator"] != SYSTEM_PROGRAM else None))
            leg_hops = cu_estimate = None
            if non_sol and not self._cu_explicit:
                # An exotic leg's composition may need more than the default (2026-10-07).
                cu_limit, leg_hops, cu_estimate = cfg.slot_lane_exotic_cu(leg, "pump_amm", cu_limit)
            priority_price, priority_bid = select_priority_fee(
                self, event["pool"], cu_limit, tip, priority_lamports)
            message, info = build_pumpswap_buy_message(
                event, facts, leg, user=self.user, buyback_fee_recipient=self._buyback,
                blockhash=snapshot.blockhash, slippage_pct=cap_pct, source_price_usd=price,
                source_price_cap_pct=cap_pct,
                compute_unit_limit=cu_limit,
                priority_fee_microlamports=priority_price,
                close_wsol=funding_lamports is None and not non_sol, tip=tip,
                allow_non_sol=non_sol,
                whole_average_usd=hard_cap_average(buy, facts["base_decimals"]),
                source_average_usd=grad_average,
                copy_cap_pct=copy_cap_pct, lookup_tables=lookup_tables, same_slot=buy_same_slot(buy))
        except ValueError as exc:
            return None, str(exc)[:120]
        # E1: the exotic round trip, from memory (the lane judges it against the position's cap).
        round_trip = (exotic_round_trip(event, facts, leg, clock_now=self.clock(), max_age_s=self.max_leg_age_s)
                      if non_sol and getattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_ROUND_TRIP", True) else None)
        # A natively funded build, or a USDC pool, has no leg, so no leg age.
        leg_age_s = (self.clock() - leg["fetched_at"]
                     if funding_lamports is None and leg.get("fetched_at") is not None else None)
        self.last_build = {**info, "leg_usdc_atomic": leg["usdc_atomic"], "leg_age_s": leg_age_s,
                           "priority_bid": priority_bid}
        return message, {"blockhash": snapshot.blockhash, "leg_age_s": leg_age_s, "cu_limit": cu_limit,
                         "priority_bid": priority_bid,
                         "cu_class": cu_class, "cu_key": cu_key, "leg_hops": leg_hops,
                         "cu_estimate": cu_estimate,
                         "last_valid_height": snapshot.last_valid_height,
                         "target_mint": facts["base_mint"],
                         "entry_slippage": {
                             "venue": "pump_amm", "program": AMM_PROGRAM,
                             "mint": facts["base_mint"], "pool": event["pool"],
                             "quote_mint": facts["quote_mint"],
                             "base_token_program": facts["base_token_program"],
                             "quote_token_program": facts["quote_token_program"],
                             **({"funding_prefix_disjoint": info["funding_prefix_disjoint"]} if non_sol else {}),
                             "spend_atomic": info["spend_lamports"],
                             "min_tokens_atomic": info["min_tokens_out"],
                             "source_sig": getattr(buy, "signature", None),
                             "source_slot": getattr(buy, "slot", None),
                             "pool_facts": {key: facts[key] for key in (
                                 "base_mint", "quote_mint", "pool_base_token_account",
                                 "pool_quote_token_account")}},
                         **({"exotic_round_trip": round_trip} if round_trip is not None else {}),
                         **({"funding_asset": "sol", "funding_amount_atomic": int(funding_lamports)}
                            if funding_lamports is not None else {}),
                         # Entry route in the J7 shape: leg ammKeys, then the pool (coin-facing).
                         "route_target": ",".join(leg_route_keys(leg) + [str(event["pool"])]),
                         "pump_price_limit": price_limit_trace(
                             info, source_price_usd=price, cap_pct=cap_pct,
                             copy_cap_pct=copy_cap_pct,
                             cost_usdc_atomic=leg["usdc_atomic"],
                             base_decimals=facts["base_decimals"], same_slot=buy_same_slot(buy))}
