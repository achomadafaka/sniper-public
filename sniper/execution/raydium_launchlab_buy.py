"""Direct Raydium LaunchLab BUY for the slot lane (2026-09-27). BUILD ONLY; shadow first.

Every fact here was checked on live frames before it was written down: the 59 routed LaunchLab
source buys of the venue census v2 (7 d to 2026-09-27), their pools, 31 global configs and 2 platform
configs, all read on the research key. The IDL is the program's own on-chain Anchor IDL
(raydium_launchpad 0.2.0).

THE SOURCE'S OWN BUY IS IN THE LOGS the lane already streams: `TradeEvent`, emitted by LaunchLab
itself as `Program data:` while it executes (discriminator bddb7fd34ee661ee, 147 bytes): pool_state,
then 13 u64 (total_base_sell, virtual_base, virtual_quote, real_base_before, real_quote_before,
real_base_after, real_quote_after, amount_in, amount_out, protocol_fee, platform_fee, creator_fee,
share_fee), trade_direction (0 buy), pool_status (0 fund), exact_in. All 59 census buys were routed
buy_exact_in at CPI depth 2 (Titan, DFlow, proVF4, Jupiter), so the event is attributed by the
invoke stack, as for CPMM.

THE OUTPUT, exact to the atomic unit on 59 of 59 (constant-product curve, curve_type 0 on all):
  amount_in (event) = the instruction's amount_in - the quote's Token-2022 transfer fee (ceil)
  protocol = ceil(amount_in x trade_fee_rate / 1e6)                    (global config)
  platform = ceil(amount_in x (trade_fee_rate + fee_rate) / 1e6) - protocol  (platform config)
  out = floor((virtual_base - real_base) x net / (virtual_quote + real_quote + net)),
        net = amount_in - protocol - platform - creator - share
  real_quote_after - real_quote_before = net and real_base_after - real_base_before = out (59/59).
Every census pool had creator_fee_rate 0 and share_fee 0, so a non-zero creator rate is refused
rather than guessed. The coin (base) is Token-2022 on 59 of 59; 55 charge a transfer fee (1% on 37,
3% on 18), which the buyer receives net of (ceil).

buy_exact_in: discriminator faea0d7bd59c13ec, amount_in, minimum_amount_out, share_fee_rate (u64,
always 0 here); 15 IDL accounts plus 3 remaining, matched on 59 of 59: the System program, the
platform fee vault PDA [platform_config, quote_mint] and the creator fee vault PDA [creator,
quote_mint]. authority = PDA ["vault_auth_seed"] (WLHv2UAZ...), event_authority = PDA
["__event_authority"] (2DPAtwB8...). pool_state (429 bytes) token_program_flag: bit 0 = base is
Token-2022, bit 1 = quote is Token-2022 (59 of 59 against the live instruction accounts).
"""
from __future__ import annotations

import base64
import math
import struct
import time
from dataclasses import dataclass

LAUNCHLAB_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
LAUNCHLAB_AUTHORITY = "WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh"
EVENT_AUTHORITY = "2DPAtwB8L12vrMRExbLuyGnC7n2J5LNoZQSejeQGpwkr"
TRADE_EVENT_DISCRIMINATOR = bytes.fromhex("bddb7fd34ee661ee")
BUY_EXACT_IN_DISCRIMINATOR = bytes.fromhex("faea0d7bd59c13ec")
POOL_STATE_DISCRIMINATOR = bytes.fromhex("f7ede3f5d7c3de46")
TRADE_EVENT_LEN = 147
POOL_STATE_LEN = 429
GLOBAL_CONFIG_LEN = 371
PLATFORM_CONFIG_LEN = 944
FEE_DENOMINATOR = 1_000_000
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM_PROGRAM = "11111111111111111111111111111111"


@dataclass(frozen=True)
class LaunchLabTrade:
    pool: str
    total_base_sell: int
    virtual_base: int
    virtual_quote: int
    real_base_before: int
    real_quote_before: int
    real_base_after: int
    real_quote_after: int
    amount_in: int
    amount_out: int
    protocol_fee: int
    platform_fee: int
    creator_fee: int
    share_fee: int
    is_buy: bool
    pool_status: int
    exact_in: bool


def _key(raw):
    from solders.pubkey import Pubkey
    return str(Pubkey.from_bytes(bytes(raw)))


def parse_trade_event(raw):
    """One LaunchLab TradeEvent from its bytes (discriminator included). ValueError otherwise."""
    if len(raw) != TRADE_EVENT_LEN or raw[:8] != TRADE_EVENT_DISCRIMINATOR:
        raise ValueError("not a %d-byte LaunchLab TradeEvent" % TRADE_EVENT_LEN)
    return LaunchLabTrade(_key(raw[8:40]), *struct.unpack_from("<13Q", raw, 40),
                          raw[144] == 0, raw[145], bool(raw[146]))


def trade_events(logs):
    """Every TradeEvent LaunchLab itself emitted in these logs, in order."""
    from sniper.execution.raydium_cpmm_buy import program_data
    return [parse_trade_event(raw) for raw in program_data(logs, LAUNCHLAB_PROGRAM)
            if raw[:8] == TRADE_EVENT_DISCRIMINATOR]


@dataclass(frozen=True)
class LaunchLabPool:
    status: int
    base_decimals: int
    quote_decimals: int
    supply: int
    total_base_sell: int
    virtual_base: int
    virtual_quote: int
    real_base: int
    real_quote: int
    global_config: str
    platform_config: str
    base_mint: str
    quote_mint: str
    base_vault: str
    quote_vault: str
    creator: str
    token_program_flag: int

    @property
    def base_program(self):
        return TOKEN_2022 if self.token_program_flag & 1 else TOKEN_PROGRAM

    @property
    def quote_program(self):
        return TOKEN_2022 if self.token_program_flag & 2 else TOKEN_PROGRAM


def parse_pool_state(raw):
    if len(raw) != POOL_STATE_LEN or raw[:8] != POOL_STATE_DISCRIMINATOR:
        raise ValueError("not a %d-byte LaunchLab PoolState" % POOL_STATE_LEN)
    supply, total_sell, vb, vq, rb, rq = struct.unpack_from("<6Q", raw, 21)
    keys = [_key(raw[o:o + 32]) for o in (141, 173, 205, 237, 269, 301, 333)]
    return LaunchLabPool(raw[17], raw[18], raw[19], supply, total_sell, vb, vq, rb, rq, *keys, raw[365])


@dataclass(frozen=True)
class Rates:
    curve_type: int
    trade_fee_rate: int
    platform_fee_rate: int
    creator_fee_rate: int


def parse_global_config(raw):
    """(curve_type, trade_fee_rate, quote_mint)."""
    if len(raw) != GLOBAL_CONFIG_LEN:
        raise ValueError("global_config is %d bytes, not %d" % (len(raw), GLOBAL_CONFIG_LEN))
    return raw[16], struct.unpack_from("<Q", raw, 27)[0], _key(raw[83:115])


def parse_platform_config(raw):
    """(fee_rate, creator_fee_rate)."""
    if len(raw) != PLATFORM_CONFIG_LEN:
        raise ValueError("platform_config is %d bytes, not %d" % (len(raw), PLATFORM_CONFIG_LEN))
    return struct.unpack_from("<Q", raw, 104)[0], struct.unpack_from("<Q", raw, 720)[0]


def fees(amount, trade_rate, platform_rate):
    """(protocol_fee, platform_fee) on `amount` (the input after its transfer fee)."""
    amount = int(amount)
    protocol = -(-amount * int(trade_rate) // FEE_DENOMINATOR)
    total = -(-amount * (int(trade_rate) + int(platform_rate)) // FEE_DENOMINATOR)
    return protocol, total - protocol


def curve_out(net, virtual_base, virtual_quote, real_base, real_quote):
    reserve_out, reserve_in = int(virtual_base) - int(real_base), int(virtual_quote) + int(real_quote)
    if int(net) <= 0 or reserve_out <= 0 or reserve_in <= 0:
        raise ValueError("no output: net %d, reserves %d/%d" % (int(net), reserve_in, reserve_out))
    return reserve_out * int(net) // (reserve_in + int(net))


def event_output(ev):
    """The source buy's output, recomputed from its own event (the unit-exact rule)."""
    net = ev.amount_in - ev.protocol_fee - ev.platform_fee - ev.creator_fee - ev.share_fee
    return curve_out(net, ev.virtual_base, ev.virtual_quote, ev.real_base_before, ev.real_quote_before)


def transfer_fee(amount, bps, maximum):
    if not bps:
        return 0
    return min(-(-int(amount) * int(bps) // 10_000), int(maximum))


def expected_buy(amount_in, *, virtual_base, virtual_quote, real_base, real_quote, total_base_sell,
                 rates, in_fee=(0, 0), out_fee=(0, 0)):
    """(gross out, out after the coin's transfer fee) for our own buy_exact_in of `amount_in`
    (quote atomic, before the quote's own transfer fee). ValueError when the curve would cap the buy
    (it re-prices at the curve's end, which this lane does not model) or a rate was never seen."""
    if rates.curve_type != 0:
        raise ValueError("curve type %d is not the constant-product curve" % rates.curve_type)
    if rates.creator_fee_rate:
        raise ValueError("a non-zero creator fee rate was never seen live")
    arrived = int(amount_in) - transfer_fee(amount_in, *in_fee)
    protocol, platform = fees(arrived, rates.trade_fee_rate, rates.platform_fee_rate)
    gross = curve_out(arrived - protocol - platform, virtual_base, virtual_quote, real_base, real_quote)
    if gross >= int(total_base_sell) - int(real_base):
        raise ValueError("the buy reaches the end of the curve")
    return gross, gross - transfer_fee(gross, *out_fee)


def buy_exact_in_data(amount_in, minimum_out):
    amount_in, minimum_out = int(amount_in), int(minimum_out)
    if amount_in <= 0 or minimum_out <= 0:
        raise ValueError("buy_exact_in needs a positive amount and a positive floor")
    return BUY_EXACT_IN_DISCRIMINATOR + struct.pack("<QQQ", amount_in, minimum_out, 0)


def fee_vaults(pool):
    """(platform fee vault, creator fee vault) PDAs for this pool's quote mint."""
    from solders.pubkey import Pubkey
    program, quote = Pubkey.from_string(LAUNCHLAB_PROGRAM), bytes(Pubkey.from_string(pool.quote_mint))
    return tuple(str(Pubkey.find_program_address([bytes(Pubkey.from_string(owner)), quote], program)[0])
                 for owner in (pool.platform_config, pool.creator))


def buy_exact_in_accounts(*, user, pool_address, pool):
    """The 18 accounts in the live frames' order (15 IDL + System, platform and creator fee vaults)."""
    from sniper.execution.pump_native_exit import _pk, ata
    user = _pk(user)
    platform_vault, creator_vault = fee_vaults(pool)
    return [
        (user, True, True),
        (_pk(LAUNCHLAB_AUTHORITY), False, False),
        (_pk(pool.global_config), False, False),
        (_pk(pool.platform_config), False, False),
        (_pk(pool_address), False, True),
        (ata(user, _pk(pool.base_mint), _pk(pool.base_program)), False, True),
        (ata(user, _pk(pool.quote_mint), _pk(pool.quote_program)), False, True),
        (_pk(pool.base_vault), False, True),
        (_pk(pool.quote_vault), False, True),
        (_pk(pool.base_mint), False, False),
        (_pk(pool.quote_mint), False, False),
        (_pk(pool.base_program), False, False),
        (_pk(pool.quote_program), False, False),
        (_pk(EVENT_AUTHORITY), False, False),
        (_pk(LAUNCHLAB_PROGRAM), False, False),
        (_pk(SYSTEM_PROGRAM), False, False),
        (_pk(platform_vault), False, True),
        (_pk(creator_vault), False, True),
    ]


def buy_exact_in_instruction(*, user, pool_address, pool, amount_in, minimum_out):
    from solders.instruction import AccountMeta, Instruction
    from sniper.execution.pump_native_exit import _pk
    metas = [AccountMeta(k, s, w) for k, s, w in
             buy_exact_in_accounts(user=user, pool_address=pool_address, pool=pool)]
    return Instruction(_pk(LAUNCHLAB_PROGRAM), buy_exact_in_data(amount_in, minimum_out), metas)


# --- the slot lane: pool cache, market facts, builder, shadow verifier ---------------------------
# Phase (a), 2026-09-27 (Alpha GO): only quote assets the lane already holds or keeps a warm leg
# for (SOL via the PumpSwap builder's leg, USDC with none, ZEC via the CPMM builder's leg when
# MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG is on). SHADOW ONLY: the lane builds and times, never sends.

# The coin-extension allowlist is shared with CPMM (raydium_cpmm_buy.coin_refusal / coin_facts).
from sniper.execution.raydium_cpmm_buy import (  # noqa: E402
    SUPPORTED_COIN_EXTENSIONS, USDC_MINT as _USDC_MINT, WSOL_MINT as _WSOL_MINT, coin_facts,
    coin_refusal, epoch_now, quote_facts, refresh_epoch)

MAX_PENDING = 64


# LAUNCHLAB LIVE, PER QUOTE ASSET (as CPMM's, design section 7). wSOL and USDC only (2026-09-28): an
# exotic quote stays shadow, because its live leg and the residue sell path are not built. A test
# pins exactly this set.
LAUNCHLAB_LIVE_QUOTES = frozenset({_WSOL_MINT, _USDC_MINT})


def launchlab_live():
    """Whether LaunchLab runs live: MEMECOIN_SLOT_LANE_LAUNCHLAB="live" (2026-09-28, operator "go
    live with everything"). Only the quote assets in LAUNCHLAB_LIVE_QUOTES send."""
    return lane_mode() == "live"


def launchlab_live_for(quote_mint):
    """Whether a LaunchLab buy spending `quote_mint` may be reserved and sent."""
    return launchlab_live() and quote_mint in LAUNCHLAB_LIVE_QUOTES


def lane_mode():
    """off | shadow | live. Anything unrecognised is off."""
    from sniper.settings import memecoin as cfg
    mode = str(getattr(cfg, "MEMECOIN_SLOT_LANE_LAUNCHLAB", "off") or "off").strip().lower()
    return mode if mode in ("shadow", "live") else "off"


class LaunchLabPoolCache:
    """pool -> {"pool": LaunchLabPool, "rates": Rates, "coin": coin_facts}, learned OFF the signal
    path, as CpmmPoolCache: `get` on the lane thread never reads the network (a miss is queued);
    `learn` (the MissLearner's thread or the prefetch thread) reads the pool, then its uncached
    configs and its coin in one batch. Configs are shared by many pools (31 global, 2 platform in
    the census) and cached for the process. Single writer per pool; readers see whole dicts."""

    def __init__(self, *, rpc, quote_usd=None):
        import threading
        self.rpc = rpc
        self._facts, self._configs, self._pending = {}, {}, []
        self.quote_usd = quote_usd if quote_usd is not None else {}   # shared with the CPMM cache
        self._lock = threading.Lock()
        self.stats = {"learned": 0, "learn_failed": 0, "queued": 0}

    def get(self, pool):
        facts = self._facts.get(pool)
        if facts is None:
            with self._lock:
                if pool not in self._pending and len(self._pending) < MAX_PENDING:
                    self._pending.append(pool)
                    self.stats["queued"] += 1
        return facts

    def learn_pending(self):
        refresh_epoch(self.rpc)
        with self._lock:
            todo, self._pending = self._pending, []
        for pool in todo:
            if pool in self._facts:
                continue
            try:
                self.learn(pool)
            except Exception:
                self.stats["learn_failed"] += 1
                import logging
                logging.getLogger(__name__).warning("launchlab pool learn failed pool=%s", pool,
                                                    exc_info=True)

    def learn(self, pool):
        from sniper.execution.pump_native_exit import _rpc_result
        value = ((_rpc_result(self.rpc, "getMultipleAccounts", [[pool], {"encoding": "base64"}])
                  or {}).get("value") or [None])[0]
        if not value or value.get("owner") != LAUNCHLAB_PROGRAM:
            raise ValueError("not a LaunchLab pool: %s" % pool)
        state = parse_pool_state(base64.b64decode(value["data"][0]))
        wanted = [k for k in (state.global_config, state.platform_config) if k not in self._configs]
        quote_read_at = time.monotonic()       # read start: a slow response never extends freshness
        batch = (_rpc_result(self.rpc, "getMultipleAccounts", [wanted + [state.base_mint, state.quote_mint],
                                                               {"encoding": "jsonParsed"}])
                 or {}).get("value") or []
        if len(batch) != len(wanted) + 2:
            raise ValueError("config/coin read came back short for pool %s" % pool)
        for key, acc in zip(wanted, batch):
            raw = base64.b64decode((acc or {}).get("data", [""])[0])
            self._configs[key] = (parse_global_config(raw) if key == state.global_config
                                  else parse_platform_config(raw))
        curve, trade, quote = self._configs[state.global_config]
        platform, creator = self._configs[state.platform_config]
        if quote != state.quote_mint:
            raise ValueError("pool %s quote %s is not its config's %s" % (pool, state.quote_mint, quote))
        facts = {"pool": state, "rates": Rates(curve, trade, platform, creator),
                 "coin": coin_facts(batch[-2]), "coin_refusal": coin_refusal(batch[-2]),
                 # the quote mint's account, for the on-demand leg's quote_facts at use (its fee
                 # can depend on the epoch then, not at learn)
                 "quote_value": batch[-1], "quote_read_at": quote_read_at,
                 "mint_values": {state.quote_mint: batch[-1]}}
        self._facts[pool] = facts
        self.stats["learned"] += 1
        return facts


def market_facts(buy, pools, sol_usd, quote=None):
    """(reason, facts) for a LaunchLab slot buy from its event and the pool cache only (no network).
    Price and size are after the source's trade, from the event's own reserves. `quote` =
    (decimals, USD per whole token) from an on-demand leg, for a quote asset the lane keeps no leg
    for; None = only the lane's own quote assets."""
    from sniper.execution.raydium_cpmm_buy import USDC_MINT, WSOL_MINT, supported_quotes
    ev = getattr(buy, "venue_event", None)
    if not isinstance(ev, LaunchLabTrade) or not ev.is_buy:
        return "slot_lane_event_unreadable", {}
    facts = pools.get(ev.pool)
    if facts is None:
        return "pool_unknown", {}
    pool, rates, coin = facts["pool"], facts["rates"], facts["coin"]
    if pool.base_mint != getattr(buy, "mint", None):
        return "slot_lane_event_mint_mismatch", {}
    quotes = supported_quotes()
    if quote is None and pool.quote_mint not in quotes:
        return "slot_lane_launchlab_quote_not_supported", {"quote": pool.quote_mint}
    if coin is None:
        return "slot_lane_launchlab_coin_%s" % (facts.get("coin_refusal") or "unsupported"), {}
    if rates.curve_type != 0 or rates.creator_fee_rate:
        return "slot_lane_launchlab_rates_unsupported", {}
    if quote is not None:
        qdec, quote_usd = int(quote[0]), float(quote[1] or 0.0)
    else:
        qdec = quotes[pool.quote_mint]
        quote_usd = (1.0 if pool.quote_mint == USDC_MINT else float(sol_usd or 0.0)
                     if pool.quote_mint == WSOL_MINT else float(pools.quote_usd.get(pool.quote_mint) or 0.0))
    if quote_usd <= 0:
        return "slot_lane_launchlab_quote_price_unknown", {}
    supply, cdec, _bps, _max = coin
    try:
        reserve_in = ev.virtual_quote + ev.real_quote_after
        reserve_out = ev.virtual_base - ev.real_base_after
        price = (reserve_in / 10 ** qdec) / (reserve_out / 10 ** cdec) * quote_usd
        their_usd = ev.amount_in / 10 ** qdec * quote_usd
    except (TypeError, ValueError, ZeroDivisionError):
        return "slot_lane_price_unreadable", {}
    if not (price > 0 and their_usd > 0):
        return "slot_lane_price_unreadable", {}
    return None, {"price_usd": price, "mcap_usd": price * supply / 10 ** cdec,
                  "their_usd": their_usd, "mint": pool.base_mint, "pool": ev.pool}


class SlotLaneLaunchLabBuilder:
    """The slot lane's LaunchLab builder; the same interface as the pump and CPMM builders. It keeps
    no leg of its own: `leg_for(quote_mint)` returns the lane's warm leg for that quote (wSOL from
    the PumpSwap builder, ZEC from the CPMM builder), and a USDC pool needs none. `build` makes no
    network call."""

    def __init__(self, *, user, rpc, leg_for, quote_usd=None, max_leg_age_s=None,
                 compute_unit_limit=None, priority_fee_microlamports=None, clock=None):
        import time
        from sniper.execution.pump_curve_buy import DEFAULT_COMPUTE_UNIT_LIMIT, MAX_LEG_AGE_S
        self.user = str(user)
        self.pools = LaunchLabPoolCache(rpc=rpc, quote_usd=quote_usd)
        self.leg_for = leg_for
        self._alt_cache = {}                  # the on-demand leg's lookup tables (worker threads)
        self.max_leg_age_s = float(MAX_LEG_AGE_S if max_leg_age_s is None else max_leg_age_s)
        self.compute_unit_limit = int(compute_unit_limit or DEFAULT_COMPUTE_UNIT_LIMIT)
        self.priority_fee_microlamports = priority_fee_microlamports
        self.clock = clock or time.monotonic
        self.last_build = None

    REASON_PREFIX = "slot_lane_launchlab"

    def refresh(self, clip_usd, snapshot=None):
        self.pools.learn_pending()
        return True

    def ondemand_quote(self, buy):
        """(quote mint, quote facts (decimals, fee bps, fee max) or None, refusal or None) for the
        on-demand leg worker, on the WORKER's thread: an unknown pool is learned here."""
        facts = self.pools.get(buy.venue_event.pool)
        if facts is None:
            try:
                facts = self.pools.learn(buy.venue_event.pool)
            except Exception:
                return None, None, "pool_unknown"
        quote = quote_facts(facts.get("quote_value"), epoch=epoch_now())
        return facts["pool"].quote_mint, quote, None

    accepts_priority_lamports = True     # memecoin_slot_lane.dispatch_build passes the crowded fee

    def build(self, buy, clip_usd, snapshot, *, funding_lamports=None, leg=None, quote=None, tip=None,
              priority_lamports=None):
        """(unsigned MessageV0, validity) or (None, reason). `leg` + `quote` = (decimals, USD, fee
        bps, fee max): an on-demand leg for a quote asset the lane keeps no leg for (phase (b))."""
        from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import MessageV0
        from solders.null_signer import NullSigner
        from solders.transaction import VersionedTransaction
        from sniper.execution.pump_curve_buy import (
            DEFAULT_PRIORITY_FEE_MICROLAMPORTS, SOLANA_PACKET_LIMIT, buy_same_slot, capped_min_out, clip_atomic,
            bounded_whole_average, leg_clip_refusal, leg_route_keys, price_limit_trace,
            routed_average_price_usd)
        from sniper.execution.pump_native_exit import ATA_PROGRAM, _pk, ata
        from sniper.execution.raydium_cpmm_buy import USDC_MINT, WSOL_MINT
        from sniper.settings import memecoin as cfg
        if funding_lamports is not None:
            return None, "native_funding_not_supported_on_launchlab"
        reason, facts = market_facts(buy, self.pools, getattr(snapshot, "sol_usd", None),
                                     quote=quote[:2] if quote else None)
        if reason:
            return None, reason
        if not getattr(snapshot, "blockhash", None):
            return None, "no_blockhash"
        ev = buy.venue_event
        known = self.pools.get(ev.pool)
        pool, rates, (_supply, cdec, fee_bps, fee_max) = known["pool"], known["rates"], known["coin"]
        user = _pk(self.user)
        ixs, tables = [], []
        in_fee = tuple(quote[2:4]) if quote else (0, 0)
        if pool.quote_mint != USDC_MINT:
            if leg is None:
                leg = self.leg_for(pool.quote_mint) if self.leg_for else None
            if leg is None or self.clock() - leg["fetched_at"] > self.max_leg_age_s:
                return None, "leg_stale"
            if leg.get("output_mint", WSOL_MINT) != pool.quote_mint:
                return None, "leg_wrong_quote"       # never spend one asset's leg on another's pool
            refused = leg_clip_refusal(leg, clip_usd)
            if refused:
                return None, refused
            amount_in, cost_usdc = int(leg["min_out_lamports"]), int(leg["usdc_atomic"])
            ixs, tables = list(leg["ixs"]), list(leg["tables"])
        else:
            amount_in = cost_usdc = clip_atomic(clip_usd)
        try:
            gross, expected = expected_buy(
                amount_in, virtual_base=ev.virtual_base, virtual_quote=ev.virtual_quote,
                real_base=ev.real_base_after, real_quote=ev.real_quote_after,
                total_base_sell=ev.total_base_sell, rates=rates, in_fee=in_fee,
                out_fee=(fee_bps, fee_max))
            cap_pct = cfg.solana_wallet_chase_cap_pct(
                getattr(buy, "source_user_id", None), cfg.MEMECOIN_REAL_CHASE_MAX_PCT)
            # The hard cap's reference is the source's AVERAGE, from its routed receipt (every
            # slot buy on this venue is routed); facts["price_usd"] is the post-trade price (SOL-1).
            # Bounded by the trade event's own average (N1 residual 1, direct path).
            average = bounded_whole_average(
                routed_average_price_usd(getattr(buy, "routed", None), cdec),
                facts["their_usd"] / (ev.amount_out / 10 ** cdec) if ev.amount_out > 0 else None)
            min_out, output_floor, price_floor = capped_min_out(
                expected, cap_pct, source_price_usd=facts["price_usd"],
                source_cost_usdc_atomic=cost_usdc, source_price_cap_pct=cap_pct, base_decimals=cdec,
                source_average_price_usd=average,
                copy_cap_pct=cfg.copy_premium_cap(getattr(buy, "source_user_id", None), "solana")[0],
                same_slot=buy_same_slot(buy))
        except ValueError as exc:
            return None, str(exc)[:120]
        coin, coin_prog = _pk(pool.base_mint), _pk(pool.base_program)
        ixs.append(Instruction(_pk(ATA_PROGRAM), bytes([1]), [      # 1 = CreateIdempotent
            AccountMeta(user, True, True), AccountMeta(ata(user, coin, coin_prog), False, True),
            AccountMeta(user, False, False), AccountMeta(coin, False, False),
            AccountMeta(_pk(SYSTEM_PROGRAM), False, False), AccountMeta(coin_prog, False, False)]))
        ixs.append(buy_exact_in_instruction(user=self.user, pool_address=ev.pool, pool=pool,
                                            amount_in=amount_in, minimum_out=min_out))
        if leg is not None and pool.quote_mint == WSOL_MINT:
            wsol_ata = ata(user, _pk(WSOL_MINT), _pk(TOKEN_PROGRAM))
            ixs.append(Instruction(_pk(TOKEN_PROGRAM), bytes([9]), [          # 9 = CloseAccount
                AccountMeta(wsol_ata, False, True), AccountMeta(user, False, True),
                AccountMeta(user, True, False)]))
        cu_limit, leg_hops, cu_estimate = (
            cfg.slot_lane_exotic_cu(leg, "raydium_launchlab", self.compute_unit_limit) if quote is not None
            else (self.compute_unit_limit, None, None))       # an exotic leg may need more (2026-10-07)
        # A treated crowded send's priority from THIS build's CU limit (pump_curve_buy.static_priority).
        from sniper.execution.pump_curve_buy import static_priority
        price, priority_bid = static_priority(self, cu_limit, tip, priority_lamports,
                                              DEFAULT_PRIORITY_FEE_MICROLAMPORTS)
        head = [set_compute_unit_limit(cu_limit), set_compute_unit_price(price)]
        from sniper.execution.solana_sender_ab import tip_instructions
        ixs = list(ixs) + list(tip_instructions(user, tip))     # Sender tip, last (SENDER_ALL)
        message = MessageV0.try_compile(user, head + ixs, tables, Hash.from_string(snapshot.blockhash))
        wire = bytes(VersionedTransaction(message, [NullSigner(user)]))
        if len(wire) > SOLANA_PACKET_LIMIT:
            return None, "transaction is %d bytes, over the %d-byte packet" % (len(wire), SOLANA_PACKET_LIMIT)
        amounts = {"spend_lamports": amount_in, "expected_tokens_out": expected,
                   "output_floor_atomic": output_floor, "source_price_floor_atomic": price_floor,
                   "min_tokens_out": min_out, "source_average_price_usd": average}
        self.last_build = {**amounts, "bytes": len(wire), "pool": ev.pool,
                           "leg_age_s": (self.clock() - leg["fetched_at"]) if leg else None}
        return message, {**({"priority_bid": priority_bid} if priority_bid else {}),
                         "blockhash": snapshot.blockhash, "cu_limit": cu_limit, "leg_hops": leg_hops,
                         "cu_estimate": cu_estimate,
                         "last_valid_height": getattr(snapshot, "last_valid_height", None),
                         "target_mint": pool.base_mint,
                         "expected_tokens_out": expected, "min_tokens_out": min_out,
                         "expected_gross": gross,
                         "pool": ev.pool, "quote_mint": pool.quote_mint,
                         # the unsigned wire bytes, for the shadow verifier's simulation only
                         "unsigned_b64": base64.b64encode(wire).decode(),
                         "route_target": ",".join((leg_route_keys(leg) if leg else []) + [ev.pool]),
                         "pump_price_limit": price_limit_trace(
                             amounts, source_price_usd=facts["price_usd"], cap_pct=cap_pct,
                             cost_usdc_atomic=cost_usdc, base_decimals=cdec, same_slot=buy_same_slot(buy))}


def simulated_outcome(value):
    """(ok, amount_out) from a simulateTransaction result value. The ERROR decides: a failed
    simulation still logs the TradeEvent before the slippage check (seen on chain, 2026-09-27), so
    an event alone is never read as a fill."""
    if not isinstance(value, dict):
        return False, None
    events = trade_events(value.get("logs") or [])
    if value.get("err") is not None:
        return False, None
    return (True, events[0].amount_out) if events else (False, None)


def shadow_line(buy, result, sim):
    """One stamped-by-the-caller line per LaunchLab shadow event, for the shadow read."""
    import json
    ok, out, err, slot = sim if sim else (None, None, None, None)
    gross = result.get("expected_gross")
    return ("[memecoin] slot lane launchlab %s %s %s pool=%s S=%s -> %s age_ms=%s build_ms=%s "
            "exp=%s min=%s gross=%s sim_ok=%s sim_out=%s sim_err=%s sim_slot=%s sim_minus_gross=%s "
            "quote=%s leg_ms=%s residue_usd=%s sig=%s recv=%s" % (
                result.get("lane_mode") or "shadow",
                getattr(buy, "handle", "?"), str(buy.mint)[:8], str(buy.venue_event.pool)[:8],
                buy.slot, result.get("reason"),
                "%.1f" % result["signal_age_ms"] if result.get("signal_age_ms") is not None else None,
                "%.1f" % result["build_ms"] if result.get("build_ms") is not None else None,
                result.get("expected_tokens_out"), result.get("min_tokens_out"), gross, ok, out,
                json.dumps(err, separators=(",", ":")) if err is not None else None, slot,
                (out - gross) if out is not None and gross is not None else None,
                str(result.get("quote"))[:8] if result.get("quote") else None,
                "%.1f" % result["leg_ms"] if result.get("leg_ms") is not None else None,
                "%.4f" % result["residue_usd"] if result.get("residue_usd") is not None else None,
                getattr(buy, "signature", None), getattr(buy, "recv_utc", None)))


SIM_TIMEOUT_S = 2.0
SIM_IDLE_WAIT_S = 5.0


def http_rpc(url, *, timeout_s=SIM_TIMEOUT_S):
    """A JSON-RPC call on `url` with its own socket timeout (connect and each read), for the shadow
    verifier: never the custody RPC's seam, never its 10 s default."""
    def rpc(method, params):
        import json
        import urllib.request
        req = urllib.request.Request(url, data=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read())
        if isinstance(body, dict) and "error" in body:
            raise RuntimeError("%s: %s" % (method, body["error"]))
        return (body or {}).get("result")
    return rpc


class ShadowVerifier:
    """A bounded worker that simulates each shadow-built LaunchLab transaction once (never sends),
    off the lane thread, and logs whether it would have landed and at what output. A full queue
    drops the event and counts it; nothing here can refuse, delay or send an entry.

    Its RPC is the lane's own watch key (http_rpc, SIM_TIMEOUT_S), never the custody RPC that
    funded sends use; None = log the build without simulating. `idle` is the lane's idle Event: a
    simulation never STARTS while the lane is handling a buy (it waits up to SIM_IDLE_WAIT_S, then
    skips and counts sim_skipped). The lane never waits for it, so a buy arriving mid-simulation
    runs beside it; the two share no RPC."""

    def __init__(self, *, rpc, log, idle=None, maxsize=16, start=True):
        import queue
        import threading
        self.rpc, self.log, self.idle = rpc, log, idle
        self._q = queue.Queue(maxsize=maxsize)
        self.stats = {"logged": 0, "dropped": 0, "sim_failed": 0, "sim_ok": 0, "sim_err": 0,
                      "sim_skipped": 0}
        if start:
            threading.Thread(target=self._run, name="slot-lane-launchlab-verify", daemon=True).start()

    def offer(self, buy, result):
        import queue
        try:
            self._q.put_nowait((buy, result))
        except queue.Full:
            self.stats["dropped"] += 1

    def verify_once(self, buy, result):
        from sniper.execution.pump_native_exit import _rpc_result
        from sniper.execution.raydium_cpmm_buy import simulation_value
        sim = None
        if result.get("unsigned_b64") and self.rpc is not None:
            if self.idle is not None and not self.idle.wait(timeout=SIM_IDLE_WAIT_S):
                self.stats["sim_skipped"] += 1
                self.log(shadow_line(buy, result, None))
                self.stats["logged"] += 1
                return
            try:
                res = _rpc_result(self.rpc, "simulateTransaction", [result["unsigned_b64"], {
                    "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
                    "commitment": "processed"}]) or {}
                value = simulation_value(res)
                if value is None:
                    self.stats["sim_failed"] += 1
                else:
                    ok, out = simulated_outcome(value)
                    sim = (ok, out, value["err"], (res.get("context") or {}).get("slot"))
                    self.stats["sim_ok" if ok else "sim_err"] += 1
            except Exception:
                self.stats["sim_failed"] += 1
                import logging
                logging.getLogger(__name__).warning("launchlab shadow simulation failed pool=%s",
                                                    buy.venue_event.pool, exc_info=True)
        self.log(shadow_line(buy, result, sim))
        self.stats["logged"] += 1

    def _run(self):
        while True:
            buy, result = self._q.get()
            try:
                self.verify_once(buy, result)
            except Exception:
                import logging
                logging.getLogger(__name__).warning("launchlab shadow line failed", exc_info=True)


# --- phase (b): the on-demand quote leg, SHADOW ONLY (Alpha GO, 2026-09-27) -----------------------
# 31 quote mints over 59 census buys; a warm leg per asset is not affordable, so an exotic-quote buy
# gets ONE Jupiter USDC -> quote leg (a /quote and a /swap-instructions on the funded key, counted
# as slot_leg) when it arrives. ExactOut routed on 1 of the first 4 mints probed (PENGU; Link, RAWR,
# ALLINU: NO_ROUTES_FOUND), so the leg is ExactIn and the quote it leaves over (leg out minus the
# min_out the buy spends) is RECORDED per event for a later sweep; nothing sweeps it yet.

ONDEMAND_MAX_IN_FLIGHT = 2
PAPER_JUP_BASE = "https://api.jup.ag/swap/v1"
PAPER_JUP_TIMEOUT_S = 3.0
LEG_RATE_LIMIT_BACKOFF_S = 60.0
# A LIVE leg re-reads its quote mint when the cached facts are older than this (Gamma LOW2,
# 2026-10-02): a paused, frozen-by-default or hooked quote cannot be spent, and its residue would
# stick.
QUOTE_RECHECK_S = 300.0


def paper_jup_call(method, path, body=None):
    """(status, json) from Jupiter on the PAPER/capture key, api.jup.ag directly. While LaunchLab is
    shadow-only this is the on-demand leg's only client (Alpha, 2026-09-27): never the funded seam,
    never keyless. No key -> (0, None); a transport failure -> (-1, None); no retry here."""
    import json as _json
    import os
    import urllib.error
    import urllib.request
    from sniper.execution.solana_real_seams import PAPER_JUP_API_KEY_VAR
    key = (os.environ.get(PAPER_JUP_API_KEY_VAR) or "").strip()
    if not key:
        return 0, None
    req = urllib.request.Request(PAPER_JUP_BASE + path, data=body.encode() if body else None,
                                 method=method, headers={"x-api-key": key,
                                                         "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=PAPER_JUP_TIMEOUT_S) as resp:
            return resp.status, _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return -1, None
# No dex pin (exotic quotes live on their own venues); a cap on the route's accounts keeps the leg
# plus the 18-account LaunchLab buy inside one packet (the build still refuses an oversized one).
ONDEMAND_QUOTE_PARAMS = "&maxAccounts=32"


def _read_rpc(builder):
    """A builder's read RPC for the worker: its own `read_rpc` when it names one (the pump curve, which
    keeps no pool cache; DAMM v2, whose pool cache reads on the learner's paced closure; DBC), else
    its pool cache's."""
    read = getattr(builder, "read_rpc", None)
    return read if read is not None else builder.pools.rpc


EXOTIC_WARM_WINDOW_S = 60.0       # the old window; the live one is MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S
EXOTIC_WARM_MAX = 2               # the old cap; the live one follows MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX
EXOTIC_WARM_REFRESH_S = 1.5       # re-quoted at most this often, as the SOL legs (LEG_REFRESH_S)
# Served only while this much younger than MAX_LEG_AGE_S (Gamma LOW-1): the builders refuse a leg past
# that bound on the lane after the requeue; speculative freshness starts at the original GET.
EXOTIC_WARM_SERVE_MARGIN_S = 1.0
# The warmer is speculative load on the shared funded Jupiter key: no refresh while any funded-role
# call saw a 429 this recently (Alpha (b), 2026-10-15; Gamma MEDIUM, p90 ~578/min).
from sniper.execution.solana_real_seams import FUNDED_JUPITER_429_QUIET_S as WARM_FUNDED_429_QUIET_S


def warm_legs_on():
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS", False))


def warm_max():
    """The most warm legs at once (MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX); 2 or less = the old warmer."""
    from sniper.settings import memecoin as cfg
    value = getattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX", EXOTIC_WARM_MAX)
    return max(EXOTIC_WARM_MAX, value) if type(value) is int else EXOTIC_WARM_MAX


def warm_window_s():
    """How long a warm leg stays warm after its last live use (MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S)."""
    from sniper.settings import memecoin as cfg
    return float(getattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S", EXOTIC_WARM_WINDOW_S))


# The warm leg is a USDC -> quote-token swap into our own quote account: nothing in it names the coin
# or its pool (Alpha (i), 2026-10-15: TTWofwAg was 13 of 19 exotic legs from 10-14 12:17Z, 17 of 19
# cold, because the leg was bound to one pool and kept 60 s). So it is bound to the quote token alone;
# every buy still runs its own live quote-safety check (the worker's _live_quote) before it looks.
WARM_QUOTE_FIELDS = ("quote_mint", "quote_token_program", "quote_decimals")


def _quote_only(facts):
    return None if facts is None else {key: facts.get(key) for key in WARM_QUOTE_FIELDS}


HOT_SHADOW_WINDOW_S = 600.0       # a sent leg keeps its quote mint hot this long (shadow only)
HOT_SHADOW_SNAP_PCT = 10.0        # a hot leg serves a buy whose clip it is at most this far under


class HotQuoteShadow:
    """HOT-QUOTE WARM LEGS, SHADOW ONLY (Alpha 2026-10-07, option (a); never live without a fresh
    operator call: it changes spend). Every REQUEUED live leg (past the worker's admission; the lane
    may still refuse the buy, so join the line's sig= to the send outcome) is judged against the last
    requeued leg of the same quote mint on ANY venue: it would have hit if that leg is at most HOT_SHADOW_WINDOW_S old and
    its clip is at or under this buy's and within HOT_SHADOW_SNAP_PCT of it (the buy would spend the
    smaller, snapped clip). `warm_mints` counts the quote mints a live version would keep warm now,
    i.e. its Jupiter load (2 calls per EXOTIC_WARM_REFRESH_S each). No Jupiter call, no buy changes.
    Called from the worker threads after the hand-back, under its own lock."""

    def __init__(self, *, clock=None, window_s=HOT_SHADOW_WINDOW_S, snap_pct=HOT_SHADOW_SNAP_PCT):
        import threading
        self.clock = clock or time
        self.window_s, self.snap = float(window_s), float(snap_pct) / 100.0
        self._lock = threading.Lock()
        self._last = {}                       # quote mint -> (clip atomic, venue, monotonic)
        self.stats = {"checked": 0, "would_hit": 0}

    def observe(self, venue, quote_mint, clip_atomic):
        now, clip = self.clock.monotonic(), int(clip_atomic)
        with self._lock:
            prev = self._last.get(quote_mint)
            self._last[quote_mint] = (clip, venue, now)
            for key in [k for k, (_c, _v, t) in self._last.items() if now - t > self.window_s]:
                del self._last[key]
            age = None if prev is None else now - prev[2]
            hit = (prev is not None and age <= self.window_s
                   and (1.0 - self.snap) * clip <= prev[0] <= clip)
            self.stats["checked"] += 1
            self.stats["would_hit"] += int(hit)
            warm = len(self._last)
        return {"would_hit": hit, "snapped_atomic": prev[0] if hit else None,
                "prev_clip": None if prev is None else prev[0], "prev_venue": None if prev is None else prev[1],
                "prev_age_s": age, "warm_mints": warm}


def _warm_facts(builder, buy):
    facts = (builder.ondemand_facts(buy) if hasattr(builder, 'ondemand_facts')
             else builder.pools.get(buy.pool))
    from sniper.execution.meteora_dbc_buy import DbcPool
    if isinstance(buy.venue_event, DbcPool):
        source = buy.venue_event
        facts = dict(facts, _identity=source, pool=source.pool, config=source.config,
                     base_mint=source.coin, quote_mint=source.quote,
                     base_token_program=source.coin_program, quote_token_program=source.quote_program,
                     quote_decimals=source.quote_facts[0])
    return facts


def _since(entry):
    """When a warm leg's window started: its last live use or its latest seed, whichever is later. A
    seed is never later than a use unless a re-seed renewed it, so at the old knob this is the old rule."""
    return max(entry['candidate_at'], entry['last_use'] if entry['last_use'] is not None else entry['candidate_at'])


class WarmHttpBudget(RuntimeError):
    """Local speculative capacity exhaustion; never a provider status."""


def exotic_round_trip_on():
    from sniper.settings import memecoin as cfg
    return getattr(cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_ROUND_TRIP', True)


class WarmExoticLegs:
    """One leg per (venue, quote, clip), one owner, at most 2 * capacity HTTP starts per 1.5 s.

    PumpSwap's round-trip evidence needs three starts; oldest legs go first within that same cap.

    The cap is max(2, min(MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX, inventory clips)) (Alpha GO 2026-10-16):
    one pool seeds a leg per clip, so a fixed 2 evicted the smallest clip in the seed pass. Above 2, a
    re-seed of the same binding renews the window, measured from max(candidate_at, last_use). At 2 or
    less the warmer is the old one: cap 2, 4 starts, the first seed's clock."""

    def __init__(self, *, clock=None, fetch_fn=None, window_s=None,
                 max_warm=None, refresh_after_s=EXOTIC_WARM_REFRESH_S, max_age_s=None,
                 backoff_s=None, http=None):
        import collections
        import threading
        from sniper.execution.pump_curve_buy import MAX_LEG_AGE_S
        self.clock = clock or time
        self.fetch_fn = fetch_fn or self._fetch
        self.window_s = float(warm_window_s() if window_s is None else window_s)
        self.ceiling = warm_max() if max_warm is None else max(EXOTIC_WARM_MAX, int(max_warm))
        self.max_warm = EXOTIC_WARM_MAX              # until the first clip inventory
        self.renew = self.ceiling > EXOTIC_WARM_MAX
        self.refresh_after_s = max(1.5, float(refresh_after_s))
        self.max_age_s = float(MAX_LEG_AGE_S - EXOTIC_WARM_SERVE_MARGIN_S if max_age_s is None else max_age_s)
        self.backoff_s = float(LEG_RATE_LIMIT_BACKOFF_S if backoff_s is None else backoff_s)
        self._lock, self._refresh_lock = threading.Lock(), threading.Lock()
        self._wake = threading.Event()
        self._thread = None
        self._entries = collections.OrderedDict()
        self._inflight = {}
        self._inventory = None
        self._generation = 0
        self._backoff_until, self._cycle_at = 0.0, None
        self._http, self._http_starts, self._actual_statuses = http, collections.deque(), []
        self._http_lock = threading.Lock()
        self._alt_cache = {}
        self.stats = dict(noted=0, hits=0, misses=0, seeded=0, refreshed=0, refresh_failed=0,
                          rate_limited=0, expired=0, evicted=0, busy=0, http_calls=0, last_error=None,
                          funded_429_skips=0, renewed=0, cap=self.max_warm,
                          last_failure=None, failure_stages={})
        # WHY A REFRESH FAILED (2026-10-17, Codex speed audit item 3): the stage and a non-secret
        # category per failure, counted by "stage:category" (bounded) and warned once a minute each.
        self._probe = None
        self._failure_warned = {}

    @staticmethod
    def _binding(venue, quote_mint, atoms, builder, facts, quote_params=None):
        """What makes two legs the same leg: the venue's builder, the quote token (mint, program,
        decimals), the clip, our wallet, the quote parameters and slippage. Never the coin or pool."""
        from sniper.execution import pump_curve_buy as P
        return (venue, quote_mint, atoms, str(getattr(builder, 'user', '')),
                quote_params if quote_params is not None else getattr(builder, 'ONDEMAND_QUOTE_PARAMS', ONDEMAND_QUOTE_PARAMS),
                P.DEFAULT_LEG_SLIPPAGE_BPS, 'solana',
                tuple((key, (facts or {}).get(key)) for key in WARM_QUOTE_FIELDS))

    def set_clip_inventory(self, atoms):
        inventory = tuple(sorted(set(a for a in atoms if type(a) is int and a > 0)))
        with self._lock:
            if inventory == self._inventory:
                return
            self._inventory = inventory
            self.max_warm = self.stats['cap'] = max(EXOTIC_WARM_MAX, min(self.ceiling, len(inventory)))
            self._generation += 1
            for key in tuple(self._entries):
                if key[2] not in inventory:
                    del self._entries[key]
            for entry in self._entries.values():
                entry['generation'] = self._generation
        self.wake()

    def _put(self, venue, quote_mint, atoms, *, builder, facts, leg, used, buy=None):
        key, now = (venue, quote_mint, int(atoms)), self.clock.monotonic()
        if key[2] <= 0:
            return
        binding = self._binding(venue, quote_mint, key[2], builder, facts)
        with self._lock:
            if not used and self._inventory is not None and key[2] not in self._inventory:
                return
            prev = self._entries.get(key)
            same = prev is not None and prev['binding'] == binding
            if same and not used and (prev['facts'] is facts or prev['leg'] is not None):
                if self.renew:               # the same leg, seen again: its window starts over
                    prev['candidate_at'] = now
                    self.stats['renewed'] += 1
                return                       # a seed never replaces a same-quote leg that is warm
            if key not in self._entries:
                if len(set(self._entries) | set(self._inflight)) >= self.max_warm:
                    candidates = [(k, e) for k, e in self._entries.items()
                                  if k not in self._inflight and (used or e['last_use'] is None)]
                    if not candidates:
                        self.stats['busy'] += 1
                        return
                    victim = min(candidates, key=lambda pair: _since(pair[1]))[0]
                    del self._entries[victim]
                    self.stats['evicted'] += 1
            self._generation += 1
            self._entries[key] = dict(key=key, leg=leg, builder=builder, facts=facts, buy=buy,
                binding=binding, generation=self._generation,
                candidate_at=prev['candidate_at'] if same and not self.renew else now,
                last_use=now if used else (prev['last_use'] if same else None))
            self.stats['noted' if used else 'seeded'] += 1
        self.wake()

    def seed(self, venue, quote_mint, usdc_atomic, *, builder, facts):
        if not facts or (facts.get('quote_mint') != quote_mint and quote_mint not in (facts.get('mints') or {})):
            return
        if venue == 'pump_amm' and facts.get('pump_migrated') is not True:
            return
        peek = getattr(builder.pools, 'cached', None)
        if peek is not None and facts.get('pool'):
            current = peek(facts['pool'])
            if current is None or self._binding(venue, quote_mint, usdc_atomic, builder, facts) != self._binding(venue, quote_mint, usdc_atomic, builder, current):
                return
            facts = current
        self._put(venue, quote_mint, usdc_atomic, builder=builder, facts=facts, leg=None, used=False)

    def note(self, venue, quote_mint, usdc_atomic, *, leg, builder, facts=None, buy=None):
        self._put(venue, quote_mint, usdc_atomic, builder=builder, facts=facts, leg=leg, used=True, buy=buy)

    def get(self, venue, quote_mint, usdc_atomic, *, builder=None, facts=None, quote_params=None):
        key, now = (venue, quote_mint, int(usdc_atomic)), self.clock.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            leg = entry['leg'] if entry is not None else None
            valid = entry is not None and now - _since(entry) <= self.window_s
            if builder is not None and valid:
                valid = entry['binding'] == self._binding(venue, quote_mint, key[2], builder, facts, quote_params)
            stamp = None if leg is None else leg.get('fetched_at')
            if not valid or type(stamp) not in (int, float) or not 0 <= now - stamp <= self.max_age_s:
                self.stats['misses'] += 1
                return None
            self.stats['hits'] += 1
            return leg

    def keys(self):
        with self._lock:
            return list(self._entries)

    def wake(self):
        self._wake.set()

    def start(self):
        import threading
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name='slot-lane-exotic-warm', daemon=True)
            self._thread.start()

    def _run(self):
        while True:
            self._wake.wait(self.refresh_after_s)
            self._wake.clear()
            self.refresh_once()

    def _expire(self, now):
        for key, entry in tuple(self._entries.items()):
            if now - _since(entry) > self.window_s:
                del self._entries[key]
                self.stats['expired'] += 1

    def refresh_once(self):
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            now = self.clock.monotonic()
            with self._lock:
                self._expire(now)
                if now < self._backoff_until or (self._cycle_at is not None and now - self._cycle_at < self.refresh_after_s):
                    return
                due = [dict(e) for e in self._entries.values() if
                       (e['last_use'] is not None or self._inventory is not None)
                       and (e['leg'] is None or now - float(e['leg']['fetched_at']) >= self.refresh_after_s)]
                # Three-phase PumpSwap refreshes share the existing start budget. Serve the
                # oldest/missing leg first so one successful refresh cannot starve another key.
                due.sort(key=lambda e: (e['leg'] or {}).get('fetched_at', float('-inf')))
                if not due:
                    return
                from sniper.execution.solana_real_seams import funded_429_age_s
                age = funded_429_age_s()
                if age is not None and age < WARM_FUNDED_429_QUIET_S:
                    self.stats['funded_429_skips'] += 1
                    return
                self._cycle_at = now
                bindings = {entry['binding'] for entry in self._entries.values()}
            self._alt_cache = {binding: cache for binding, cache in self._alt_cache.items() if binding in bindings}
            for entry in due:
                self._trim_http_starts()
                reverse_due = (entry.get('buy') is not None and exotic_round_trip_on()
                               and (entry['key'][0] == 'pump_amm' or
                                    entry['key'][0] == 'pump_curve'
                                    and getattr(entry['buy'], 'graduation', None) is not None))
                if len(self._http_starts) > 2 * self.max_warm - (3 if reverse_due else 2):
                    continue
                age = funded_429_age_s()   # a funded 429 mid-cycle stops the rest too (Gamma LOW-1)
                if age is not None and age < WARM_FUNDED_429_QUIET_S:
                    self.stats['funded_429_skips'] += 1
                    return
                with self._lock:
                    current = self._entries.get(entry['key'])
                    if current is None or current['generation'] != entry['generation']:
                        continue
                    self._inflight[entry['key']] = entry['generation']
                self._actual_statuses = []
                self.stats['last_error'] = None
                self._probe = probe = {'stage': 'quote_safety', 'category': None, 'http_status': None}
                try:
                    leg, statuses = self.fetch_fn(entry)
                except Exception as exc:
                    self.stats['last_error'] = type(exc).__name__
                    if probe['category'] is None:
                        probe['category'] = _failure_category(exc)
                    leg, statuses = None, []
                finally:
                    with self._lock:
                        self._inflight.pop(entry['key'], None)
                statuses = list(statuses) + self._actual_statuses
                with self._lock:
                    if any(status in (401, 429) for status in statuses):
                        self._backoff_until = self.clock.monotonic() + self.backoff_s
                        self.stats['rate_limited'] += 1
                        return
                    current = self._entries.get(entry['key'])
                    valid = (current is not None and current['generation'] == entry['generation']
                             and current['binding'] == entry['binding'] and current['facts'] is entry['facts']
                             and entry['binding'] == self._binding(*entry['key'], entry['builder'], entry['facts']))
                    if leg is None or int(leg.get('min_out_lamports') or 0) <= 0 or not valid:
                        self.stats['refresh_failed'] += 1
                        if leg is not None:
                            probe.update(stage='other', category='min_out' if valid else 'binding_changed')
                        failed = (probe['stage'], probe['category'] or 'refused', probe['http_status'])
                    else:
                        failed = None
                        current['leg'] = leg
                        self.stats['refreshed'] += 1
                if failed is not None:
                    self._note_failure(*failed, entry['key'][1], entry['key'][2])
        finally:
            self._refresh_lock.release()

    FAILURE_KINDS_MAX = 24

    def _note_failure(self, stage, category, http_status, quote_mint, clip_atoms):
        """Record one failed refresh: stats (heartbeat) and one WARNING per (stage, category) a minute."""
        name = '%s:%s' % (stage, category)
        now = self.clock.monotonic()
        with self._lock:
            self.stats['last_failure'] = dict(stage=stage, category=category, http_status=http_status,
                                              quote_mint=quote_mint, clip_atoms=clip_atoms)
            # COPY ON WRITE: the heartbeat reads stats on the runner's thread without this lock.
            kinds = dict(self.stats['failure_stages'])
            if name not in kinds and len(kinds) >= self.FAILURE_KINDS_MAX:
                name = 'other:overflow'
            kinds[name] = kinds.get(name, 0) + 1
            self.stats['failure_stages'] = kinds
            warned = self._failure_warned.get(name)
            due = warned is None or now - warned >= 60.0
            if due:
                self._failure_warned[name] = now
        if due:
            import logging
            logging.getLogger(__name__).warning('exotic warm refresh failed stage=%s category=%s http_status=%s quote_mint=%s '
                           'clip_atoms=%s (count %d)', stage, category, http_status, quote_mint, clip_atoms,
                           self.stats['failure_stages'].get(name, 0))

    def _trim_http_starts(self):
        now = self.clock.monotonic()
        while self._http_starts and self._http_starts[0] <= now - 1.5:
            self._http_starts.popleft()

    def request_http(self, method, url, *, headers=None, body=None):
        """Shared sockets only; each caller owns its request budget and result accounting."""
        from sniper.execution.fast_http import PooledHttpClient
        with self._http_lock:
            if self._http is None:
                # Two funded workers and the single warmer can overlap without waiting on a socket.
                self._http = PooledHttpClient(max_connections=4, max_keepalive=4)
            client = self._http
        return client.request(method, url, headers=headers, body=body)

    def _warm_http(self, method, url, *, headers=None, body=None):
        from sniper.execution.solana_real_seams import funded_429_age_s
        self._trim_http_starts()
        if len(self._http_starts) >= 2 * self.max_warm:
            raise WarmHttpBudget('warm_http_budget')
        # THE FUNDED QUIET WINDOW, AT EVERY DISPATCH (Codex F8): the cycle and per-leg checks pass
        # before a leg's /quote GET; a funded 429 during that GET (or the keyed host's own 429 on
        # this leg) must still stop the leg's /swap-instructions POST and any failover host.
        age = funded_429_age_s()
        if age is not None and age < WARM_FUNDED_429_QUIET_S:
            self.stats['funded_429_skips'] += 1
            raise WarmHttpBudget('funded_429_quiet')
        self._http_starts.append(self.clock.monotonic())
        self.stats['http_calls'] += 1
        result = self.request_http(method, url, headers=headers, body=body)
        self._actual_statuses.append(result[0])
        return result

    def _prepare_quote_facts(self, entry):
        """Retain validated safety in the existing Pump CAS or a private admitted snapshot."""
        from sniper.execution.raydium_cpmm_buy import epoch_now, quote_facts
        from sniper.execution.pump_native_exit import _rpc_result
        facts, builder, mint = entry['facts'], entry['builder'], entry['key'][1]
        if facts is None:
            return True  # legacy admitted cold legs already passed the worker's safety check
        cached = getattr(getattr(builder, 'pools', None), 'cached', None)
        if cached is not None and facts.get('pool'):
            snapshot = cached(facts['pool'])
            if snapshot is None:
                # The coin's pool left the cache: the leg is the quote token's, so keep it on a
                # quote-only safety check (Alpha (i), 2026-10-15) instead of letting it go cold.
                quote_only = _quote_only(facts)
                with self._lock:
                    current = self._entries.get(entry['key'])
                    if current is None or current['generation'] != entry['generation'] or current['facts'] is not facts:
                        return False
                    current['facts'] = entry['facts'] = quote_only
                return self._prepare_quote_facts(entry)
            if snapshot is not facts:
                if self._binding(*entry['key'], builder, snapshot) != entry['binding']:
                    return False
                with self._lock:
                    current = self._entries.get(entry['key'])
                    if current is None or current['generation'] != entry['generation'] or current['facts'] is not facts:
                        return False
                    current['facts'] = entry['facts'] = snapshot
                facts = snapshot
        now = self.clock.monotonic()
        stamp = facts.get('quote_read_at')
        value = (facts.get('mint_values') or {}).get(mint)
        if entry.get('quote_safety') is not None:
            stamp, value = entry['quote_safety']
        fresh = type(stamp) in (int, float) and 0 <= now - stamp < QUOTE_RECHECK_S
        if not fresh or value is None:
            publish = getattr(getattr(builder, 'pools', None), 'publish_quote_snapshot', None)
            value = (_rpc_result(_read_rpc(builder), 'getAccountInfo',
                                 [mint, {'encoding': 'jsonParsed'}]) or {}).get('value')
            if publish is None or not facts.get('pool'):     # a quote-only warm leg has no pool
                original = (facts.get('mint_values') or {}).get(mint)
                expected_owner = facts.get('quote_token_program') or (original or {}).get('owner')
                expected_decimals = facts.get('quote_decimals')
                if expected_decimals is None and original:
                    original_safety = quote_facts(original, epoch=epoch_now())
                    expected_decimals = original_safety[0] if original_safety else None
                safety = quote_facts(value, epoch=epoch_now()) if value else None
                if (not expected_owner or safety is None or safety[0] != expected_decimals
                        or value.get('owner') != expected_owner
                        or int(safety[1] or 0) or int(safety[2] or 0)):
                    return False
                with self._lock:
                    current = self._entries.get(entry['key'])
                    if current is None or current['generation'] != entry['generation'] or current['facts'] is not facts:
                        return False
                    current['quote_safety'] = entry['quote_safety'] = (now, value)
                return True
            snapshot = publish(facts['pool'], expected_facts=facts, value=value, read_at=now)
            if snapshot is None:
                return False
            with self._lock:
                current = self._entries.get(entry['key'])
                if current is None or current['generation'] != entry['generation'] or current['facts'] is not facts:
                    return False
                binding = self._binding(*entry['key'], builder, snapshot)
                current['facts'], current['binding'] = snapshot, binding
                entry['facts'], entry['binding'] = snapshot, binding
            facts = snapshot
            value = (facts.get('mint_values') or {}).get(mint)
        safety = quote_facts(value, epoch=epoch_now()) if value else None
        return safety is not None and not int(safety[1] or 0) and not int(safety[2] or 0)

    def _fetch(self, entry):
        from sniper.execution import pump_curve_buy as P
        venue, quote_mint, atoms = entry['key']
        builder, statuses = entry['builder'], []
        probe = self._probe if self._probe is not None else {}
        probe.update(stage='quote_safety', category=None, http_status=None)
        if not self._prepare_quote_facts(entry):
            probe['category'] = 'refused'
            return None, statuses
        start = self.clock.monotonic()
        def jup(method, path, body=None):
            probe.update(stage='quote_fetch' if method == 'GET' else 'instruction_build', category=None)
            status, payload = P._funded_jup_call(method, path, body, http=self._warm_http)
            statuses.append(status)
            probe['http_status'] = status
            if status != 200:
                probe['category'] = _http_category(status, payload)
            if any(s in (401, 429) for s in self._actual_statuses):
                raise RuntimeError('warm_provider_backoff')
            if method == 'GET' and not self._valid_quote(payload, quote_mint, atoms):
                if status == 200:
                    probe.update(stage='quote_identity',
                                 category=self._quote_identity_failure(payload, quote_mint, atoms))
                raise ValueError('warm_quote_identity')
            return status, payload
        def rpc(method, params):
            result = _read_rpc(builder)(method, params)
            value = (result or {}).get('result', result)
            if method in ('getAccountInfo', 'getMultipleAccounts') and params[1].get('encoding') == 'base64':
                probe.update(stage='alt_validation', category=None)
                accounts = value.get('value') if isinstance(value, dict) else None
                if method == 'getAccountInfo':
                    accounts = [accounts]
                if not isinstance(accounts, list) or len(accounts) != (1 if method == 'getAccountInfo' else len(params[0])):
                    probe['category'] = 'warm_alt_accounts'
                    raise ValueError('warm_alt_accounts')
                import base64
                from solders.address_lookup_table_account import AddressLookupTable
                for account in accounts:
                    if not isinstance(account, dict) or account.get('owner') != P.ALT_PROGRAM:
                        probe['category'] = 'warm_alt_owner' if account else 'missing:account'
                        raise ValueError('warm_alt_owner')
                    table = AddressLookupTable.deserialize(base64.b64decode(account['data'][0], validate=True))
                    if int(table.meta.deactivation_slot) != 2 ** 64 - 1:
                        probe['category'] = 'warm_alt_deactivated'
                        raise ValueError('warm_alt_deactivated')
            return result
        cache = self._alt_cache.setdefault(entry['binding'], {})
        leg = P.prefetch_usdc_sol_leg(atoms, P.DEFAULT_LEG_SLIPPAGE_BPS, user=builder.user,
            jup_call=jup, rpc=rpc, alt_cache=cache,
            quote_params=entry['binding'][4], clock=self.clock.monotonic, unwrap=False, output_mint=quote_mint)
        if leg is None and probe.get('category') is None:
            probe['category'] = 'no_leg'          # prefetch_usdc_sol_leg's None at this stage
        if leg is not None:
            if any(account.is_signer and str(account.pubkey) != str(builder.user)
                   for ix in leg['ixs'] for account in ix.accounts):
                probe.update(stage='signer_identity', category='warm_signer_identity')
                raise ValueError('warm_signer_identity')
            leg['fetched_at'] = start
            leg['tables'] = tuple(leg['tables'])
            leg['reverse'] = self._reverse_leg(P, entry, leg, statuses, probe, start)
        return leg, statuses

    def _reverse_leg(self, P, entry, leg, statuses, probe, start):
        def jup(method, path):
            status, payload = P._funded_jup_call(method, path, None, http=self._warm_http)
            statuses.append(status)
            return status, payload
        return _fetch_exotic_reverse(P, entry, leg, jup_call=jup, start=start,
                                     probe=probe, stats=self.stats)

    @staticmethod
    def _valid_quote(quote, mint, atoms):
        return _quote_identity_failure(quote, mint, atoms) is None

    @staticmethod
    def _quote_identity_failure(quote, mint, atoms):
        return _quote_identity_failure(quote, mint, atoms)


def _fetch_exotic_reverse(P, entry, leg, *, jup_call, start, probe=None, stats=None):
    """Prequote a retained PumpSwap event or verified graduation pool snapshot, off the lane.

    A first seed has no event/reserve evidence: it stays unknown until the existing worker
    notes a buy, including one the new round-trip gate refuses. No speculative reserve read.
    """
    if not exotic_round_trip_on() or entry.get('buy') is None:
        return None
    probe = {} if probe is None else probe
    stats = {} if stats is None else stats
    from sniper.execution import pumpswap_buy as PS
    quote_mint, facts, buy = entry['key'][1], entry['facts'], entry['buy']
    try:
        if entry['key'][0] == 'pump_curve' and getattr(buy, 'graduation', None) is not None:
            # This remains a CURVE source. Its watcher already read the migrated pool; use
            # that cached snapshot only to choose a fungible reverse input. At fire the
            # current graduation model must still cover that exact amount and fresh floor.
            from sniper.services.memecoin_slot_graduation import pool_refusal
            grad = buy.graduation
            facts, state = dict(grad['pool_facts']), dict(grad['state'])
            source = P.parse_trade_event(buy.trade_event_log)
            verified = dict(state, venue='pumpswap', account=grad['pool'], pool_facts=facts,
                            base_token_program=facts['base_token_program'],
                            quote_token_program=facts['quote_token_program'])
            if (source['mint'] != buy.mint or source['mint'] != facts['base_mint']
                    or source.get('quote_mint') != quote_mint or facts['quote_mint'] != quote_mint
                    or state['virtual_quote'] != facts['virtual_quote']
                    or pool_refusal(verified, buy.mint, quote_mint) is not None):
                return None
            lp, protocol, creator = state['fees']
            if any(type(fee) is not int or fee < 0 for fee in (lp, protocol, creator)):
                return None
            # The same zero-trade numeric head as pumpswap_buy.graduation_event. No source
            # PumpSwap trade or protocol fee recipient is invented for this curve event.
            facts = dict(facts, pool=grad['pool'])
            event = dict(pool=grad['pool'], pool_base_token_reserves=state['base_reserve'],
                         pool_quote_token_reserves=state['quote_reserve'], base_amount_out=0,
                         quote_amount_in_with_lp_fee=0, lp_fee_basis_points=lp,
                         protocol_fee_basis_points=protocol, coin_creator_fee_basis_points=creator,
                         cashback_fee_basis_points=0)
        elif entry['key'][0] == 'pump_amm':
            event = PS.parse_buy_event(buy.trade_event_log)
        else:
            return None
        if (facts['pool'] != event['pool'] or facts['quote_mint'] != quote_mint
                or leg['output_mint'] != quote_mint or leg['usdc_atomic'] != entry['key'][2]):
            raise ValueError('reverse pool binding')
        model = PS.exotic_pool_round_trip(event, facts, leg)
        amount = model['quote_back_atomic']
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    path = ('/quote?inputMint=%s&outputMint=%s&amount=%d&slippageBps=%d'
            % (quote_mint, P.USDC_MINT, amount, P.DEFAULT_LEG_SLIPPAGE_BPS))
    try:
        probe.update(stage='reverse_quote', category=None)
        status, payload = jup_call('GET', path)
        if status != 200 or _quote_identity_failure(payload, P.USDC_MINT, amount,
                                                    input_mint=quote_mint) is not None:
            raise ValueError('reverse_quote_identity')
    except Exception:
        stats['reverse_failed'] = stats.get('reverse_failed', 0) + 1
        return None
    stats['reverse_ok'] = stats.get('reverse_ok', 0) + 1
    return {'input_mint': quote_mint, 'output_mint': P.USDC_MINT, 'in_atomic': amount,
            'out_usdc_atomic': int(payload['outAmount']),
            'min_usdc_atomic': int(payload['otherAmountThreshold']),
            'usdc_atomic': leg['usdc_atomic'], 'quote_spend_atomic': leg['min_out_lamports'],
            'fetched_at': start, 'context_slot': payload.get('contextSlot')}


def _quote_identity_failure(quote, mint, atoms, *, input_mint=None):
    """None for a quote the leg accepts, else the first check it fails ('missing:<field>' or
    'invalid:<check>'): the same predicate _valid_quote always was, named (2026-10-17)."""
    from sniper.execution import pump_curve_buy as P
    if not isinstance(quote, dict):
        return 'invalid:not_object'
    try:
        for key in ('inAmount', 'outAmount', 'otherAmountThreshold'):
            value = quote[key]
            if not (type(value) is int or type(value) is str and value.isdecimal()):
                return 'invalid:' + key
        output, floor = int(quote['outAmount']), int(quote['otherAmountThreshold'])
        checks = (
            ('inputMint', lambda: quote['inputMint'] == (P.USDC_MINT if input_mint is None else input_mint)),
            ('outputMint', lambda: quote['outputMint'] == mint),
            ('inAmount_exact', lambda: int(quote['inAmount']) == atoms),
            ('swapMode', lambda: quote['swapMode'] == 'ExactIn'),
            ('slippageBps', lambda: type(quote['slippageBps']) is int
             and quote['slippageBps'] == P.DEFAULT_LEG_SLIPPAGE_BPS),
            ('amounts', lambda: 0 < floor <= output <= 2 ** 64 - 1),
            ('routePlan', lambda: bool(quote['routePlan'])),
            ('routePlan_rows', lambda: all(
                isinstance(row, dict) and isinstance(row.get('swapInfo'), dict)
                and row['swapInfo'].get('ammKey') and row['swapInfo'].get('label')
                for row in quote['routePlan'])))
        for name, ok in checks:
            if not ok():
                return 'invalid:' + name
    except KeyError as exc:
        return 'missing:%s' % (exc.args[0] if exc.args and isinstance(exc.args[0], str) else 'field')
    except (TypeError, ValueError):
        return 'invalid:type'
    return None


def _http_category(status, payload):
    """'http_<status>' plus the provider's short error code when it sends one (never a body or URL)."""
    code = (payload or {}).get('errorCode') if isinstance(payload, dict) else None
    if isinstance(code, str) and code.replace('_', '').isalnum() and len(code) <= 48:
        return 'http_%s:%s' % (status, code)
    return 'http_%s' % (status,)


def _failure_category(exc):
    """A warm_* code the warmer raised, else the exception class (its message can carry hosts)."""
    text = str(exc)
    return text if text.startswith('warm_') and text.replace('_', '').isalnum() else type(exc).__name__


class OnDemandLegWorker:
    """SHADOW ONLY: builds a venue buy (LaunchLab phase (b); CPMM exotic quotes) whose quote asset
    the lane keeps no leg for. `builders` maps a venue to its builder, which provides
    ondemand_quote(buy), build(..., leg=, quote=) and REASON_PREFIX; one worker serves every venue,
    so the in-flight bound and the paper key's backoff are shared.

    ITS JUPITER CLIENT IS THE PAPER KEY (paper_jup_call), never the funded seam: a shadow 429 must
    not cool the funded hosts. A 429 or 401 skips that event, counts leg_rate_limited and stops legs
    for LEG_RATE_LIMIT_BACKOFF_S (events meanwhile count leg_backoff); no retry, no fallback.

    NEVER ON THE LANE THREAD (Alpha): the lane only calls `offer`, which never blocks; at most
    ONDEMAND_MAX_IN_FLIGHT buys are in flight on this worker's own threads and a further one is
    dropped and counted (`busy`). In shadow the routed row was written before the conversion, so a
    dropped or failed buy already has its one path. Each result goes to `offer_fn(buy, result)`
    (the venue's verifier: simulated once, logged with the leg's time and the residue)."""

    def __init__(self, *, builders, admission_fn, snapshot_fn, clip_fn, offer_fn, jup_call=None,
                 max_in_flight=ONDEMAND_MAX_IN_FLIGHT, clock=None, start=True, live_fn=None,
                 requeue_fn=None, refuse_fn=None, cap_open_fn=None, warm=None):
        import queue
        import threading
        import time
        self.builders, self.admission_fn, self.snapshot_fn = dict(builders), admission_fn, snapshot_fn
        self.clip_fn, self.offer_fn, self.jup_call = clip_fn, offer_fn, jup_call
        # LIVE (spec 2026-10-02-memecoin-slot-lane-exotic-quotes-live-design.md): for a venue where
        # `live_fn(venue)` is true the leg is quoted on the FUNDED seam (role slot_leg), and a buy that
        # passes admission goes back to the lane thread via `requeue_fn(buy with ondemand)`; every
        # refusal goes to `refuse_fn(buy, reason)` (the routed row). A shadow venue never changes.
        self.live_fn, self.requeue_fn, self.refuse_fn = live_fn, requeue_fn, refuse_fn
        self.cap_open_fn = cap_open_fn
        self.warm = warm                      # WarmExoticLegs (MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS) or None
        self._quote_seen = {}                 # quote mint -> (monotonic read, quote facts | None)
        self.hot_shadow = HotQuoteShadow(clock=clock or time)       # measurement only (option (a))
        self.max_in_flight, self.clock = int(max_in_flight), clock or time
        self._q, self._lock, self._in_flight = queue.SimpleQueue(), threading.Lock(), 0
        self._http, self._http_lock = None, threading.Lock()
        self._backoff_until = 0.0
        self.stats = {"offered": 0, "busy": 0, "leg_failed": 0, "built": 0, "refused": 0,
                      "leg_rate_limited": 0, "leg_backoff": 0, "requeued": 0, "live_refused": 0,
                      "hot_shadow_would_hit": 0,
                      **{"offered_" + venue: 0 for venue in self.builders}}
        if start:
            for n in range(self.max_in_flight):
                threading.Thread(target=self._run, name="slot-lane-ondemand-leg-%d" % n,
                                 daemon=True).start()

    def _hot_shadow(self, buy, quote_mint, clip_atomic, leg_ms, warm_hit):
        """After the hand-back, never before it: judge this requeued leg (the lane may still refuse its
        buy) for the hot-quote shadow and log one line. Evidence only; never raises into the worker."""
        import logging
        try:
            v = self.hot_shadow.observe(buy.venue, quote_mint, clip_atomic)
            self.stats["hot_shadow_would_hit"] += int(v["would_hit"])
            logging.getLogger(__name__).warning(
                "[memecoin] slot lane exotic hot quote shadow venue=%s quote=%s clip=%d would_hit=%d "
                "snapped=%s prev_clip=%s prev_venue=%s prev_age_s=%s warm_mints=%d leg_ms=%.0f warm_leg=%d sig=%s",
                buy.venue, str(quote_mint)[:8], clip_atomic, int(v["would_hit"]), v["snapped_atomic"],
                v["prev_clip"], v["prev_venue"],
                None if v["prev_age_s"] is None else "%.1f" % v["prev_age_s"], v["warm_mints"], leg_ms,
                int(bool(warm_hit)), getattr(buy, "signature", None))
        except Exception:  # noqa: BLE001 - evidence only
            logging.getLogger(__name__).warning("slot lane hot quote shadow failed", exc_info=True)

    def offer(self, buy, *, clip_usd=None, trial=None, quote_params=None):
        """Lane thread: never blocks. True = this worker took the buy (a venue it serves).
        `quote_params` (an exotic RE-QUOTE, memecoin_slot_lane.REQUOTE_PARAMS) replaces the venue's
        leg query for this buy and skips the warm legs."""
        venue = ("pump_amm" if (getattr(buy, "graduation", None) or {}).get("cash_leg_repair") is True
                 else getattr(buy, "venue", None))
        if venue not in self.builders:
            return False
        with self._lock:
            if self._in_flight >= self.max_in_flight:
                self.stats["busy"] += 1
                return False
            self._in_flight += 1
            self.stats["offered"] += 1
            self.stats["offered_" + venue] += 1
        self._q.put((buy, clip_usd, trial, quote_params))
        return True

    def _run(self):
        while True:
            buy, clip_usd, trial, quote_params = self._q.get()
            try:
                self.offer_fn(buy, self.process(buy, clip_usd=clip_usd, trial=trial,
                                                quote_params=quote_params))
            except Exception:
                import logging
                logging.getLogger(__name__).warning("on-demand leg failed sig=%s",
                                                    getattr(buy, "signature", None), exc_info=True)
                self._refused("slot_lane_exotic_worker_error", self.clock.monotonic(), buy,
                              live=bool(self.live_fn and self.live_fn(
                                  "pump_amm" if (getattr(buy, "graduation", None) or {}).get("cash_leg_repair") is True
                                  else buy.venue)))
            finally:
                with self._lock:
                    self._in_flight -= 1

    def _refused(self, reason, t0, buy, live=False, **extra):
        self.stats["refused"] += 1
        if live:
            self.stats["live_refused"] += 1
            if self.refuse_fn is not None:
                try:
                    self.refuse_fn(buy, reason)
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("exotic refusal fallback failed sig=%s",
                                                          getattr(buy, "signature", None))
        return {"status": "live_refused" if live else "shadow", "reason": reason, "leg_mode": "ExactIn",
                "signal_age_ms": (t0 - float(getattr(buy, "recv_monotonic", t0))) * 1000.0, **extra}

    def _live_quote(self, builder, quote_mint, quote, *, facts=None):
        """(quote facts, refusal) for a LIVE leg: facts re-read from chain when older than
        QUOTE_RECHECK_S (quote_facts refuses a hooked, non-transferable, frozen-default or paused
        mint), and no quote that carries a transfer fee (Gamma LOW2)."""
        from sniper.execution.raydium_cpmm_buy import epoch_now, quote_facts
        prefix = builder.REASON_PREFIX
        now = self.clock.monotonic()
        seen = self._quote_seen.get(quote_mint)
        read_at = (facts or {}).get("quote_read_at")
        bound_fresh = (type(read_at) in (int, float) and read_at >= 0
                       and 0 <= now - read_at < QUOTE_RECHECK_S)
        if seen is None or now - seen[0] >= QUOTE_RECHECK_S or (bound_fresh and read_at >= seen[0]):
            if (type(read_at) in (int, float) and read_at >= 0
                    and 0 <= now - read_at < QUOTE_RECHECK_S):
                # Validate the value and stamp from ONE published snapshot, even if a learner
                # replaced it since ondemand_quote. New unsafe facts supersede cached safety.
                value = ((facts or {}).get("mint_values") or {}).get(quote_mint)
                seen = (read_at, quote_facts(value, epoch=epoch_now()) if value else None)
            else:
                from sniper.execution.pump_native_exit import _rpc_result
                try:
                    value = (_rpc_result(_read_rpc(builder), "getAccountInfo",
                                         [quote_mint, {"encoding": "jsonParsed"}]) or {}).get("value")
                except Exception:
                    return None, prefix + "_quote_unread"
                seen = (now, quote_facts(value, epoch=epoch_now()) if value else None)
            self._quote_seen[quote_mint] = seen
        quote = seen[1]
        if quote is None:
            return None, prefix + "_quote_unmovable"
        if int(quote[1] or 0) or int(quote[2] or 0):
            return None, prefix + "_quote_fee_bearing"
        return quote, None

    def process(self, buy, *, clip_usd=None, trial=None, quote_params=None):
        from sniper.execution.pump_curve_buy import leg_metrics
        with leg_metrics(clock=self.clock.monotonic) as timings:
            result = self._process(buy, clip_usd=clip_usd, trial=trial,
                                   quote_params=quote_params, timings=timings)
            result['leg_timings_ms'] = dict(timings)
            return result

    def request_http(self, method, url, *, headers=None, body=None):
        if self.warm is not None:
            return self.warm.request_http(method, url, headers=headers, body=body)
        from sniper.execution.fast_http import PooledHttpClient
        with self._http_lock:
            if self._http is None:
                self._http = PooledHttpClient(max_connections=self.max_in_flight,
                                              max_keepalive=self.max_in_flight)
            client = self._http
        return client.request(method, url, headers=headers, body=body)

    def _process(self, buy, *, clip_usd=None, trial=None, quote_params=None, timings):
        from sniper.execution.pump_curve_buy import (
            DEFAULT_LEG_SLIPPAGE_BPS, USDC_MINT, WSOL_MINT, clip_atomic, prefetch_usdc_sol_leg,
            prefetch_timed)
        t0 = self.clock.monotonic()
        # The worker's own part of receipt->decision (Lambda 2026-10-16): the signal's age when this
        # worker starts, the venue quote read, and the worker's total until the hand-back.
        timings["signal_age_at_worker"] = (t0 - float(buy.recv_monotonic)) * 1000.0
        grad = getattr(buy, "graduation", None) or {}
        cash_repair = grad.get("cash_leg_repair") is True
        venue = "pump_amm" if cash_repair else buy.venue
        builder = self.builders[venue]
        prefix = builder.REASON_PREFIX
        live = bool(self.live_fn and self.live_fn(venue))
        if live and self.cap_open_fn is not None and not self.cap_open_fn(venue):
            return self._refused("slot_lane_exotic_cap", t0, buy, live=True)
        if cash_repair:
            # The source is still the original curve trade. Its newly migrated pool, rather
            # than CurveTradeEvent's quote fields, binds this one exact wrapped-SOL cash leg.
            from sniper.execution.pumpswap_buy import TOKEN_PROGRAM, WSOL_MINT
            facts = grad.get("pool_facts") or {}
            deadline = grad.get("cash_leg_deadline")
            if (facts.get("quote_mint") != WSOL_MINT
                    or facts.get("quote_token_program") != TOKEN_PROGRAM
                    or facts.get("quote_decimals", 9) != 9
                    or type(deadline) not in (int, float) or not math.isfinite(deadline)
                    or t0 >= deadline):
                return self._refused("slot_lane_exotic_aged_out", t0, buy, live=live)
            quote_mint, quote, refusal = WSOL_MINT, (9, 0, 0), None
        else:
            with prefetch_timed("ondemand_quote"):
                quote_mint, quote, refusal = builder.ondemand_quote(buy)
        if refusal:
            return self._refused(refusal, t0, buy, live=live, quote=quote_mint)
        if quote is None:
            return self._refused(prefix + "_quote_unmovable", t0, buy, live=live, quote=quote_mint)
        if live:
            facts = grad["pool_facts"] if cash_repair else _warm_facts(builder, buy)
            with prefetch_timed("quote_safety"):
                quote, refusal = self._live_quote(builder, quote_mint, quote, facts=facts)
            if refusal:
                return self._refused(refusal, t0, buy, live=True, quote=quote_mint)
        qdec, qbps, qmax = quote
        clip = self.clip_fn() if clip_usd is None else clip_usd
        if not clip:
            return self._refused("no_clip", t0, buy, live=live, quote=quote_mint)
        warming = bool(live and self.warm is not None and warm_legs_on() and quote_params is None
                       and not cash_repair)
        leg = self.warm.get(buy.venue, quote_mint, clip_atomic(clip), builder=builder, facts=facts) if warming else None
        warm_hit = leg is not None
        reverse_missing = (live and not cash_repair and quote_mint not in (USDC_MINT, WSOL_MINT)
                           and (buy.venue == 'pump_amm' or buy.venue == 'pump_curve' and grad)
                           and exotic_round_trip_on() and (leg is None or leg.get('reverse') is None))
        statuses = []
        if (not warm_hit or reverse_missing) and t0 < self._backoff_until:
            self.stats["leg_backoff"] += 1
            return self._refused(prefix + "_leg_backoff", t0, buy, live=live, quote=quote_mint)
        if live:
            from sniper.execution import pump_curve_buy
            def client(method, path, body=None):
                return pump_curve_buy._funded_jup_call(method, path, body, http=self.request_http)
        else:
            client = self.jup_call or paper_jup_call

        def jup(method, path, body=None):
            status, payload = client(method, path, body)
            statuses.append(status)
            return status, payload
        from sniper.settings import memecoin as cfg
        packet_preflight = (live and venue == "raydium_launchlab" and quote_params is None
                            and str(getattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_REQUOTE", "off")
                                    or "off").strip().lower() == "oversize")
        leg_params = quote_params or getattr(builder, "ONDEMAND_QUOTE_PARAMS", ONDEMAND_QUOTE_PARAMS)
        if packet_preflight:
            from sniper.services.memecoin_slot_lane import MAX_SIGNAL_AGE_S, REQUOTE_PARAMS
        # A repair stays on this bounded worker and the original receipt clock; it never renews
        # a pending hold or sets requote_attempts (which exempts a buy from the lane's age gate).
        for packet_attempt in range(2):
            if not warm_hit:
                leg = prefetch_usdc_sol_leg(clip_atomic(clip), DEFAULT_LEG_SLIPPAGE_BPS, user=builder.user,
                                            jup_call=jup, rpc=_read_rpc(builder),
                                            alt_cache=builder._alt_cache,
                                            quote_params=leg_params,
                                            clock=builder.clock,
                                            unwrap=False, output_mint=quote_mint)
            if (reverse_missing and leg is not None and leg.get('reverse') is None
                    and not any(s in (401, 429) for s in statuses)):
                entry = {'key': (buy.venue, quote_mint, clip_atomic(clip)), 'facts': facts, 'buy': buy}
                leg = dict(leg)  # A forward-only warm leg is shared; attach proof to our own copy.
                with prefetch_timed('reverse_quote'):
                    leg['reverse'] = _fetch_exotic_reverse(pump_curve_buy, entry, leg, jup_call=jup,
                                                          start=leg['fetched_at'])
            t_leg = self.clock.monotonic()
            leg_ms = (t_leg - t0) * 1000.0
            if any(status in (401, 429) for status in statuses):
                self._backoff_until = t_leg + LEG_RATE_LIMIT_BACKOFF_S
                self.stats["leg_rate_limited"] += 1
                return self._refused(prefix + "_leg_rate_limited", t0, buy, live=live,
                                     quote=quote_mint, leg_ms=leg_ms)
            if 0 in statuses:
                return self._refused(prefix + "_paper_key_missing", t0, buy, live=live, quote=quote_mint)
            if cash_repair and t_leg >= deadline:
                return self._refused("slot_lane_exotic_aged_out", t0, buy, live=live,
                                     quote=quote_mint, leg_ms=leg_ms)
            if leg is None or int(leg["out_lamports"]) <= 0 or int(leg["min_out_lamports"]) <= 0:
                self.stats["leg_failed"] += 1
                return self._refused(prefix + "_leg_failed", t0, buy, live=live, quote=quote_mint,
                                     leg_ms=leg_ms)
            fits = getattr(builder, "ondemand_fits", None)
            if fits is not None and not fits(buy, leg):
                # Before the lane reserves: a leg whose transaction cannot fit one packet is refused
                # here, never after a reservation (pump curve, 2026-10-07: a 2-hop leg made 1,446 bytes).
                return self._refused(prefix + "_packet_oversize", t0, buy, live=live, quote=quote_mint,
                                     leg_ms=leg_ms)
            quote_usd_paid = (leg["usdc_atomic"] / 1e6) / (leg["out_lamports"] / 10 ** qdec)
            # Gamma LOW1: the leg's own rate carries its price impact, which would raise the source's
            # USD price and loosen the cap by that much on a thin quote. The pool's LP fee stays in.
            try:
                impact = min(max(float((leg.get("quote") or {}).get("priceImpactPct") or 0.0), 0.0), 0.5)
            except (TypeError, ValueError):
                impact = 0.0
            quote_usd = quote_usd_paid * (1.0 - impact)
            residue_q = int(leg["out_lamports"]) - int(leg["min_out_lamports"])
            extra = {"quote": quote_mint, "leg_ms": leg_ms, "residue_q": residue_q, "warm_leg": warm_hit,
                     "residue_usd": residue_q / 10 ** qdec * quote_usd, "quote_usd_paid": quote_usd_paid,
                     "price_impact": impact}
            reason = self.admission_fn(buy, (qdec, quote_usd))
            if reason:
                return self._refused(reason, t0, buy, live=live, **extra)
            if not packet_preflight:
                break
            msg, detail = builder.build(buy, float(clip), self.snapshot_fn(), leg=leg,
                                        quote=(qdec, quote_usd, qbps, qmax))
            if msg is not None:
                break
            import re
            packet_overflow = re.fullmatch(
                r"transaction is [0-9]+ bytes, over the 1232-byte packet", str(detail))
            if not packet_overflow or packet_attempt:
                return self._refused(str(detail), t0, buy, live=True, **extra)
            if not 0 <= self.clock.monotonic() - float(buy.recv_monotonic) < MAX_SIGNAL_AGE_S:
                return self._refused("slot_lane_exotic_aged_out", t0, buy, live=True, **extra)
            leg_params = REQUOTE_PARAMS[0]
            leg, warm_hit, warming = None, False, False
            statuses.clear()
        if live:
            import dataclasses
            if (packet_preflight
                    and not 0 <= self.clock.monotonic() - float(buy.recv_monotonic) < MAX_SIGNAL_AGE_S):
                return self._refused("slot_lane_exotic_aged_out", t0, buy, live=True, **extra)
            if warming:
                # Only after admission passes (Gamma LOW-3): a refused buy cannot send, and noting
                # its leg would spend 60 s of funded refreshes on it.
                self.warm.note(buy.venue, quote_mint, clip_atomic(clip), leg=leg, builder=builder, facts=facts, buy=buy)
            self.stats["requeued"] += 1
            ondemand = {
                "leg": leg, "quote": (qdec, quote_usd, qbps, qmax), "quote_mint": quote_mint,
                "requested_clip_usd": clip_usd, "leg_ms": round(leg_ms, 1), "quote_usd_paid": quote_usd_paid,
                "warm_leg": warm_hit,
                # A buy that already carried a leg is a resize: the lane allows only one. A re-quote
                # is not a resize: it keeps the buy's own resize flag and counts its attempts.
                "resized": (True if cash_repair else
                            bool((getattr(buy, "ondemand", None) or {}).get("resized"))
                            if quote_params else getattr(buy, "ondemand", None) is not None),
                **({"requote_attempts": int((getattr(buy, "ondemand", None) or {}).get("requote_attempts") or 0) + 1,
                    "requoted": quote_params, "requote_offered_mono": t0} if quote_params else {}),
                **({"trial": dict(trial)} if trial is not None else {}),
                "requeued_mono": self.clock.monotonic()}
            timings["worker"] = (ondemand["requeued_mono"] - t0) * 1000.0
            ondemand["leg_timings_ms"] = dict(timings)
            self.requeue_fn(dataclasses.replace(buy, ondemand=ondemand))
            self._hot_shadow(buy, quote_mint, clip_atomic(clip), leg_ms, warm_hit)
            return {"status": "requeued", "reason": "requeued", "leg_mode": "ExactIn", **extra,
                    "signal_age_ms": (t0 - float(getattr(buy, "recv_monotonic", t0))) * 1000.0}
        msg, detail = builder.build(buy, float(clip), self.snapshot_fn(), leg=leg,
                                         quote=(qdec, quote_usd, qbps, qmax))
        t_built = self.clock.monotonic()
        if msg is None:
            return self._refused(str(detail), t0, buy, **extra)
        self.stats["built"] += 1
        keys = ("expected_tokens_out", "min_tokens_out", "expected_gross", "unsigned_b64")
        return {"status": "shadow", "leg_mode": "ExactIn", **extra, **{k: detail.get(k) for k in keys},
                "reason": "built exp=%s min=%s" % (detail.get("expected_tokens_out"),
                                                   detail.get("min_tokens_out")),
                "signal_age_ms": (t0 - float(getattr(buy, "recv_monotonic", t0))) * 1000.0,
                "build_ms": (t_built - t0) * 1000.0}
