"""Meteora DBC (dynamic bonding curve) buys on the slot lane, LIVE (operator order 2026-10-07 via
Alpha: "we need that live no time for shadows"). Trigger: position 71651 ELON, a routed DBC buy on
an xStock-quoted curve that the lane handed off `no_slot_venue`; the on-chain lane filled it ~2 s
later at +19.3% vs the source. Facts: docs/superpowers/specs/2026-10-07-slot-lane-meteora-dbc-venue-
parked.md.

WHAT THE LANE CAN SEE. DBC emits its swap event by self-CPI: the processed logs show only
`Program dbcij3... invoke`, never the pool. So the frame carries a `DbcSeen` marker and EVERY DBC
routed buy is learned by mint on the MissLearner's DBC thread, inside its budget: getProgramAccounts
(dataSize 424, memcmp base_mint@136), then ONE getMultipleAccounts of [config, coin, quote vault]
(measured n=13 p50 112 / p90 153 ms). The quote mint lives in the config, so the quote VAULT names it
(cross-checked against the config) and its token program. SOL and USDC need nothing more; an exotic
quote's mint is read once more and kept for the worker's QUOTE_RECHECK_S. No cache across buys: each buy carries the facts read for it
(`SlotBuy.venue_event`), so the curve state it is judged on is at most a learn old.

PRICE AND FLOOR. As DAMM v2: the source's own routed average (USDC spent / tokens received), and the
signed minimum out is the price floor at that average x (1 + chase cap) on the USDC the leg spends.
The swap spends the leg's GUARANTEED quote amount (its min out). No curve math on the fill path.

REFUSED (each hands the buy off to the on-chain lane): a coin with an extension the builds do not
cover (transfer hook first among them; swap2_with_transfer_hook is never built), a migrated curve, a
complete curve (quote_reserve >= migration_quote_threshold), a rate-limiter pool (base fee mode 2:
it needs the instructions sysvar and a single-swap rule), and at build a curve our buy plus the
source's own would complete: ExactIn past the curve end reverts (AmountLeftIsNotZero) and the learn
read may predate the source's buy, so both are counted.

Layout (official IDL v0.2.1, checked on ELON's pool ETaY7dvA and config Gc1HX36M, 2026-10-07):
VirtualPool 424 bytes, config@72 base_mint@136 base_vault@168 quote_vault@200 quote_reserve@240
is_migrated@305; PoolConfig 1048 bytes, quote_mint@8 base_fee_mode@130 migration_quote_threshold@264.
swap2 (amount_0 u64, amount_1 u64, swap_mode u8; 0 = ExactIn, amount_1 = minimum out), 15 accounts.

MEMECOIN_SLOT_LANE_DBC: off (default) | live. Exotic quotes (and USDC) also need "meteora_dbc" on
MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
POOL_AUTHORITY = "FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM"     # PDA ["pool_authority"]
EVENT_AUTHORITY = "8Ks12pbrD6PXxfty1hVQiE9sc289zgU1zHkvXhrSdriF"    # PDA ["__event_authority"]
POOL_DISCRIMINATOR = hashlib.sha256(b"account:VirtualPool").digest()[:8]
CONFIG_DISCRIMINATOR = hashlib.sha256(b"account:PoolConfig").digest()[:8]
# A hooked-coin curve is a TransferHookPool (also 424 bytes, so the size filter returns it) on a
# ConfigWithTransferHook (1128 bytes); both seen on 8V2jTnUb, 2026-10-07. Refused by name.
HOOK_POOL_DISCRIMINATOR = hashlib.sha256(b"account:TransferHookPool").digest()[:8]
HOOK_CONFIG_DISCRIMINATOR = hashlib.sha256(b"account:ConfigWithTransferHook").digest()[:8]
SWAP2_DISCRIMINATOR = hashlib.sha256(b"global:swap2").digest()[:8]
POOL_LEN, CONFIG_LEN = 424, 1048
OFF_CONFIG, OFF_BASE_MINT, OFF_BASE_VAULT, OFF_QUOTE_VAULT = 72, 136, 168, 200
OFF_QUOTE_RESERVE, OFF_IS_MIGRATED = 240, 305
OFF_QUOTE_MINT, OFF_BASE_FEE_MODE, OFF_THRESHOLD = 8, 130, 264
RATE_LIMITER = 2
EXACT_IN = 0
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
REASON_PREFIX = "slot_lane_dbc"
# A learn converts only this fresh: two buys of one coin share one read, a later buy reads again.
FACTS_MAX_AGE_S = 1.0
_INVOKE = "Program %s invoke" % PROGRAM


def lane_mode():
    """off | live. Anything else, "shadow" included, is off: the operator ordered no shadow phase."""
    from sniper.settings import memecoin as cfg
    mode = str(getattr(cfg, "MEMECOIN_SLOT_LANE_DBC", "off") or "off").strip().lower()
    return "live" if mode == "live" else "off"


def dbc_live():
    return lane_mode() == "live"


def non_sol_listed():
    """A USDC- or exotic-quoted curve builds only with "meteora_dbc" on the exotic live list."""
    from sniper.settings import memecoin as cfg
    return "meteora_dbc" in (getattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", None) or ())


@dataclass(frozen=True)
class DbcSeen:
    """A routed frame in which DBC ran. It names nothing: the event is not in the logs."""


def dbc_invoked(logs):
    return any(isinstance(line, str) and line.startswith(_INVOKE) for line in logs or ())


@dataclass(frozen=True)
class DbcPool:
    """One learn's facts, carried on the buy (SlotBuy.venue_event). coin_facts = (supply, decimals,
    fee bps, fee max) or None; quote_facts = (decimals, fee bps, fee max) or None (unmovable)."""
    pool: str
    config: str
    coin: str
    quote: str
    base_vault: str
    quote_vault: str
    coin_program: str
    quote_program: str
    coin_facts: Optional[tuple]
    coin_refusal: Optional[str]
    quote_facts: Optional[tuple]
    quote_reserve: int
    threshold: int
    is_migrated: int
    base_fee_mode: int
    read_mono: float
    quote_value: Optional[dict] = field(default=None, compare=False, hash=False)


def _key(raw, off):
    from solders.pubkey import Pubkey
    return str(Pubkey.from_bytes(raw[off:off + 32]))


def parse_pool(raw):
    if len(raw) != POOL_LEN or raw[:8] != POOL_DISCRIMINATOR:
        raise ValueError("not a DBC VirtualPool account")
    return {"config": _key(raw, OFF_CONFIG), "base_mint": _key(raw, OFF_BASE_MINT),
            "base_vault": _key(raw, OFF_BASE_VAULT), "quote_vault": _key(raw, OFF_QUOTE_VAULT),
            "quote_reserve": struct.unpack_from("<Q", raw, OFF_QUOTE_RESERVE)[0],
            "is_migrated": raw[OFF_IS_MIGRATED]}


def parse_config(raw):
    if len(raw) != CONFIG_LEN or raw[:8] != CONFIG_DISCRIMINATOR:
        raise ValueError("not a DBC PoolConfig account")
    return {"quote_mint": _key(raw, OFF_QUOTE_MINT), "base_fee_mode": raw[OFF_BASE_FEE_MODE],
            "threshold": struct.unpack_from("<Q", raw, OFF_THRESHOLD)[0]}


# The two quote mints whose facts never change: classic Token, no extensions.
STATIC_QUOTES = {WSOL_MINT: (9, 0, 0), USDC_MINT: (6, 0, 0)}


def _values(rpc, method, params):
    from sniper.execution.pump_native_exit import _rpc_result
    return _rpc_result(rpc, method, params)


def find_pool(rpc, coin, *, clock=time.monotonic, quote_cache=None):
    """(DbcPool, None), or (None, why): no_pool, ambiguous (one base mint has one curve), unread (a
    raised, error, short or malformed answer: unknown, never "no pool"), config_unreadable,
    transfer_hook_pool / transfer_hook_config (a hooked-coin curve: refused before any mint read).
    `quote_cache` (the learner's own dict, one writer) keeps an exotic quote mint's value for
    QUOTE_RECHECK_S."""
    from sniper.execution.raydium_cpmm_buy import coin_facts, coin_refusal, epoch_now, quote_facts
    from sniper.execution.raydium_launchlab_buy import QUOTE_RECHECK_S
    try:
        rows = _values(rpc, "getProgramAccounts", [PROGRAM, {
            "encoding": "base64", "commitment": "confirmed",
            "filters": [{"dataSize": POOL_LEN}, {"memcmp": {"offset": OFF_BASE_MINT, "bytes": coin}}]}])
    except Exception:
        return None, "unread"
    if not isinstance(rows, list):
        return None, "unread"
    if not rows:
        return None, "no_pool"
    if len(rows) > 1:
        return None, "ambiguous"
    try:
        address = rows[0]["pubkey"]
        raw = base64.b64decode(rows[0]["account"]["data"][0])
        if raw[:8] == HOOK_POOL_DISCRIMINATOR:
            return None, "transfer_hook_pool"
        pool = parse_pool(raw)
    except (KeyError, IndexError, TypeError, ValueError):
        return None, "unread"
    if pool["base_mint"] != coin:
        return None, "unread"
    try:
        values = (_values(rpc, "getMultipleAccounts", [[pool["config"], coin, pool["quote_vault"]], {
            "encoding": "jsonParsed", "commitment": "confirmed"}]) or {}).get("value")
        read_mono = clock()
        config_value, coin_value, vault_value = values
        if config_value.get("owner") != PROGRAM:
            return None, "unread"
        if base64.b64decode(config_value["data"][0])[:8] == HOOK_CONFIG_DISCRIMINATOR:
            return None, "transfer_hook_config"
        config = parse_config(base64.b64decode(config_value["data"][0]))
        vault_mint = vault_value["data"]["parsed"]["info"]["mint"]
        quote_program, coin_program = vault_value["owner"], coin_value["owner"]
    except Exception:
        return None, "unread"
    quote = config["quote_mint"]
    if vault_mint != quote:
        return None, "unread"
    if not config["threshold"]:
        return None, "config_unreadable"
    quote_value = None
    if quote in STATIC_QUOTES:
        q_facts = STATIC_QUOTES[quote]
    else:
        cached = (quote_cache or {}).get(quote)
        if cached is not None and 0 <= read_mono - cached[0] < QUOTE_RECHECK_S:
            read_at, quote_value = cached
        else:
            try:
                quote_value = (_values(rpc, "getAccountInfo", [quote, {
                    "encoding": "jsonParsed", "commitment": "confirmed"}]) or {}).get("value")
            except Exception:
                return None, "unread"
            if not quote_value:
                return None, "unread"
            read_at = clock()
            if quote_cache is not None:
                quote_cache[quote] = (read_at, quote_value)
        q_facts = quote_facts(quote_value, epoch=epoch_now())
    return DbcPool(pool=address, config=pool["config"], coin=coin, quote=quote,
                   base_vault=pool["base_vault"], quote_vault=pool["quote_vault"],
                   coin_program=coin_program, quote_program=quote_program,
                   coin_facts=coin_facts(coin_value), coin_refusal=coin_refusal(coin_value),
                   quote_facts=q_facts, quote_reserve=int(pool["quote_reserve"]),
                   threshold=int(config["threshold"]), is_migrated=int(pool["is_migrated"]),
                   base_fee_mode=int(config["base_fee_mode"]), read_mono=read_mono,
                   quote_value=quote_value), None


def pool_refusal(facts):
    """Why a learned curve cannot be bought, or None. Order: gone first, then the coin."""
    if facts.is_migrated:
        return REASON_PREFIX + "_migrated"
    if facts.quote_reserve >= facts.threshold:
        return REASON_PREFIX + "_curve_complete"
    if facts.base_fee_mode == RATE_LIMITER:
        return REASON_PREFIX + "_rate_limiter"
    if facts.coin_refusal:
        # The slot lane runs no token-safety simulation: an extension the builds do not cover
        # (transfer hook: gated; permanent delegate: seizable; pausable) is handed off, never built.
        return "%s_coin_%s" % (REASON_PREFIX, facts.coin_refusal)
    if facts.coin_facts is None:
        return "supply_unknown"
    return None


def dbc_slot_buy(routed, facts):
    """(SlotBuy, None) for a routed buy on a learned curve, or (None, why). No I/O."""
    from sniper.services.memecoin_slot_signal import SlotBuy
    if facts is None:
        return None, "pool_unknown"
    refused = pool_refusal(facts)
    if refused:
        return None, refused
    return SlotBuy(venue="meteora_dbc", source_user_id=routed.source_user_id, handle=routed.handle,
                   owner=routed.owner, signature=routed.signature, slot=routed.slot, mint=routed.mint,
                   pool=facts.pool, sol_amount=int(routed.usdc_spent_atomic),
                   token_amount=int(routed.token_delta_atomic), virtual_sol_reserves=None,
                   virtual_token_reserves=None, real_sol_reserves=0, real_token_reserves=0,
                   token_program=facts.coin_program, trade_event_log="",
                   recv_monotonic=routed.recv_monotonic, recv_utc=routed.recv_utc,
                   signal_feed=getattr(routed, "signal_feed", "helius"),
                   venue_event=facts, routed=routed, quote_mint=facts.quote), None


def market_facts(buy, sol_usd, quote=None):
    """(reason, facts) from the facts the buy carries (no network). The price is the source's routed
    average whatever the curve's quote. SOL needs nothing more; USDC needs the exotic listing; any
    other quote also needs the on-demand leg's `quote` (without it: REASON_PREFIX_quote_not_supported,
    the worker's cue)."""
    from sniper.execution.meteora_damm_v2_buy import source_price_usd
    facts = getattr(buy, "venue_event", None)
    if not isinstance(facts, DbcPool):
        return "pool_unknown", {}
    refused = pool_refusal(facts)
    if refused:
        return refused, {}
    if facts.quote != WSOL_MINT and (not non_sol_listed() or (facts.quote != USDC_MINT and quote is None)):
        return REASON_PREFIX + "_quote_not_supported", {}
    routed = getattr(buy, "routed", None)
    if routed is None:
        return "supply_unknown", {}
    supply, decimals, _bps, _max = facts.coin_facts
    price = source_price_usd(routed, decimals)
    if price is None:
        return "slot_lane_price_unreadable", {}
    return None, {"price_usd": price, "mcap_usd": price * supply / 10 ** decimals,
                  "their_usd": int(routed.usdc_spent_atomic) / 1e6, "mint": buy.mint,
                  "pool": facts.pool, "their_usd_basis": "routed_usdc_spent"}


class DbcMintLearner:
    """The MissLearner's DBC venue: its key is the COIN (the frame names no pool). `learn(coin)` runs
    find_pool on the MissLearner's DBC thread and keeps the answer for `fresh(coin)`; RoutedDbcMiss
    converts from it. One writer (that thread); `fresh` reads whole tuples."""

    KEEP = 64

    def __init__(self, rpc, *, clock=time.monotonic, log=None):
        self.rpc, self.clock, self.log = rpc, clock, log
        self._found = collections.OrderedDict()      # coin -> DbcPool (newest last)
        self._quotes = {}                            # exotic quote mint -> (read monotonic, value)
        self._lock = threading.Lock()
        self.stats = {"found": 0, "refused": {}}

    def learn(self, coin):
        facts, why = find_pool(self.rpc, coin, clock=self.clock, quote_cache=self._quotes)
        if facts is None:
            self.stats["refused"][why] = self.stats["refused"].get(why, 0) + 1
            if self.log is not None:
                self.log("[memecoin] slot lane dbc first sight %s refused %s" % (str(coin)[:8], why))
            return False
        with self._lock:
            self._found[coin] = facts
            self._found.move_to_end(coin)
            while len(self._found) > self.KEEP:
                self._found.popitem(last=False)
        self.stats["found"] += 1
        return True

    def fresh(self, coin):
        facts = self._found.get(coin)
        if facts is None or not 0 <= self.clock() - facts.read_mono <= FACTS_MAX_AGE_S:
            return None
        return facts


def swap2_instruction(*, user, facts, input_account, output_account, amount_in, minimum_out):
    """DBC swap2, ExactIn: (amount_in, minimum out, mode 0) in the 15 accounts of the verified layout.
    No referral: the optional account is the program id."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey
    pk = Pubkey.from_string
    metas = [
        AccountMeta(pk(POOL_AUTHORITY), False, False),
        AccountMeta(pk(facts.config), False, False),
        AccountMeta(pk(facts.pool), False, True),
        AccountMeta(pk(str(input_account)), False, True),
        AccountMeta(pk(str(output_account)), False, True),
        AccountMeta(pk(facts.base_vault), False, True),
        AccountMeta(pk(facts.quote_vault), False, True),
        AccountMeta(pk(facts.coin), False, False),
        AccountMeta(pk(facts.quote), False, False),
        AccountMeta(pk(str(user)), True, False),
        AccountMeta(pk(facts.coin_program), False, False),
        AccountMeta(pk(facts.quote_program), False, False),
        AccountMeta(pk(PROGRAM), False, False),
        AccountMeta(pk(EVENT_AUTHORITY), False, False),
        AccountMeta(pk(PROGRAM), False, False),
    ]
    data = SWAP2_DISCRIMINATOR + struct.pack("<QQB", int(amount_in), int(minimum_out), EXACT_IN)
    return Instruction(pk(PROGRAM), data, metas)


class SlotLaneDbcBuilder:
    """The slot lane's DBC builder; the same interface as the other venue builders. A SOL curve spends
    the shared USDC -> SOL leg (output left in the wSOL ATA, closed after), a USDC curve spends the
    clip from the USDC account, any other quote the on-demand leg's minimum from its own account.
    `build` makes no network call."""

    REASON_PREFIX = REASON_PREFIX

    def __init__(self, *, user, rpc, leg_fn, max_leg_age_s=None, compute_unit_limit=None,
                 priority_fee_microlamports=None, clock=None):
        from sniper.execution.pump_curve_buy import DEFAULT_COMPUTE_UNIT_LIMIT, MAX_LEG_AGE_S
        self.user = str(user)
        self.rpc = rpc
        self.leg_fn = leg_fn
        self.max_leg_age_s = float(MAX_LEG_AGE_S if max_leg_age_s is None else max_leg_age_s)
        self.compute_unit_limit = int(compute_unit_limit or DEFAULT_COMPUTE_UNIT_LIMIT)
        self.priority_fee_microlamports = priority_fee_microlamports
        self.clock = clock or time.monotonic
        self.last_build = None
        self._alt_cache = {}                   # shared on-demand leg worker, per builder

    def refresh(self, clip_usd, snapshot=None):
        return True                           # nothing to prefetch: each buy carries its facts

    @property
    def read_rpc(self):
        """The on-demand worker's reads (quote mint re-check, leg lookup tables): the custody RPC. It
        keeps no pool cache, so without this every cold exotic leg raised AttributeError."""
        return self.rpc

    def ondemand_quote(self, buy):
        facts = getattr(buy, "venue_event", None)
        if not isinstance(facts, DbcPool):
            return None, None, "pool_unknown"
        return facts.quote, facts.quote_facts, None

    def ondemand_facts(self, buy):
        """The learn's quote mint value and its read time, so the live worker re-reads it only past
        QUOTE_RECHECK_S."""
        facts = getattr(buy, "venue_event", None)
        if not isinstance(facts, DbcPool) or facts.quote_value is None:
            return {}
        return {"quote_read_at": facts.read_mono, "mint_values": {facts.quote: facts.quote_value}}

    def _funding(self, facts, clip_usd, leg, quote, snapshot):
        """(leg-shaped funding, input account or None for wSOL, quote USD per whole token) or why not."""
        from sniper.execution.pump_curve_buy import clip_atomic, leg_clip_refusal
        from sniper.execution.pump_native_exit import TOKEN_PROGRAM, _pk, ata
        user = _pk(self.user)
        if facts.quote == USDC_MINT:
            spend = clip_atomic(clip_usd)
            return ({"ixs": [], "tables": [], "usdc_atomic": spend, "min_out_lamports": spend,
                     "fetched_at": None}, ata(user, _pk(USDC_MINT), _pk(TOKEN_PROGRAM)), 1.0)
        if facts.quote == WSOL_MINT:
            if leg is None:
                leg = self.leg_fn() if self.leg_fn else None
            if leg is not None and leg.get("output_mint", WSOL_MINT) != WSOL_MINT:
                return "leg_wrong_quote"
            quote_usd = getattr(snapshot, "sol_usd", None)
        else:
            if leg is None or quote is None:
                return "exotic_leg_missing"
            if leg.get("output_mint") != facts.quote:
                return "leg_wrong_quote"
            quote_usd = quote[1]
        if leg is None or self.clock() - leg["fetched_at"] > self.max_leg_age_s:
            return "leg_stale"
        refused = leg_clip_refusal(leg, clip_usd)
        if refused:
            return refused
        if facts.quote == WSOL_MINT:
            return leg, None, quote_usd
        return leg, ata(user, _pk(facts.quote), _pk(facts.quote_program)), quote_usd

    accepts_priority_lamports = True     # memecoin_slot_lane.dispatch_build passes the crowded fee

    def build(self, buy, clip_usd, snapshot, *, funding_lamports=None, leg=None, quote=None, tip=None,
              priority_lamports=None):
        """(unsigned MessageV0, validity) or (None, reason)."""
        from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import MessageV0
        from solders.null_signer import NullSigner
        from solders.transaction import VersionedTransaction
        from sniper.execution.pump_curve_buy import (
            DEFAULT_PRIORITY_FEE_MICROLAMPORTS, SOLANA_PACKET_LIMIT, leg_route_keys,
            source_price_floor_atomic)
        from sniper.execution.pump_native_exit import ATA_PROGRAM, SYSTEM_PROGRAM, TOKEN_PROGRAM, _pk, ata
        from sniper.settings import memecoin as cfg
        if funding_lamports is not None:
            return None, "native_funding_not_supported_on_dbc"
        if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
            # The source's average is not an executable output quote. An unsigned build refusal
            # preserves the existing verified backup without signing a zero or synthetic floor.
            return None, "output_quote_unavailable"
        facts = getattr(buy, "venue_event", None)
        if (isinstance(facts, DbcPool) and facts.quote not in (WSOL_MINT, USDC_MINT)
                and (leg is None or quote is None)):
            return None, "exotic_leg_missing"
        reason, market = market_facts(buy, getattr(snapshot, "sol_usd", None),
                                      quote=tuple(quote[:2]) if quote else None)
        if reason:
            return None, reason
        if not getattr(snapshot, "blockhash", None):
            return None, "no_blockhash"
        got = self._funding(facts, clip_usd, leg, quote, snapshot)
        if isinstance(got, str):
            return None, got
        leg, input_account, quote_usd = got
        amount_in, cost_usdc = int(leg["min_out_lamports"]), int(leg["usdc_atomic"])
        # ExactIn past the curve end reverts; the learn read may predate the source's own buy.
        q_decimals = facts.quote_facts[0] if facts.quote_facts else None
        if not quote_usd or not quote_usd > 0 or q_decimals is None:
            return None, REASON_PREFIX + "_curve_unknown"
        their_q = int(market["their_usd"] / float(quote_usd) * 10 ** int(q_decimals))
        if amount_in + their_q >= facts.threshold - facts.quote_reserve:
            return None, REASON_PREFIX + "_curve_completing"
        _supply, decimals, _bps, _max = facts.coin_facts
        cap_pct = cfg.solana_wallet_chase_cap_pct(
            getattr(buy, "source_user_id", None), cfg.MEMECOIN_REAL_CHASE_MAX_PCT)
        try:
            min_out = source_price_floor_atomic(cost_usdc, market["price_usd"], cap_pct,
                                                base_decimals=decimals)
        except ValueError as exc:
            return None, str(exc)[:120]
        if not 0 <= int(min_out) <= 2 ** 64 - 1:
            return None, "price floor outside u64"
        user = _pk(self.user)
        coin = _pk(buy.mint)
        out_ata = ata(user, coin, _pk(facts.coin_program))
        wsol_ata = ata(user, _pk(WSOL_MINT), _pk(TOKEN_PROGRAM))
        ixs = list(leg["ixs"])
        ixs.append(Instruction(_pk(ATA_PROGRAM), bytes([1]), [      # 1 = CreateIdempotent
            AccountMeta(user, True, True), AccountMeta(out_ata, False, True),
            AccountMeta(user, False, False), AccountMeta(coin, False, False),
            AccountMeta(_pk(SYSTEM_PROGRAM), False, False),
            AccountMeta(_pk(facts.coin_program), False, False)]))
        ixs.append(swap2_instruction(user=self.user, facts=facts,
                                     input_account=wsol_ata if input_account is None else input_account,
                                     output_account=out_ata, amount_in=amount_in, minimum_out=min_out))
        if input_account is None:            # the SOL leg's wSOL account is closed; a quote ATA stays
            ixs.append(Instruction(_pk(TOKEN_PROGRAM), bytes([9]), [      # 9 = CloseAccount
                AccountMeta(wsol_ata, False, True), AccountMeta(user, False, True),
                AccountMeta(user, True, False)]))
        cu_limit, leg_hops, cu_estimate = (
            cfg.slot_lane_exotic_cu(leg, "meteora_dbc", self.compute_unit_limit) if quote is not None
            else (self.compute_unit_limit, None, None))       # an exotic leg may need more (2026-10-07)
        # A treated crowded send's priority from THIS build's CU limit (pump_curve_buy.static_priority).
        from sniper.execution.pump_curve_buy import static_priority
        price, priority_bid = static_priority(self, cu_limit, tip, priority_lamports,
                                              DEFAULT_PRIORITY_FEE_MICROLAMPORTS)
        head = [set_compute_unit_limit(cu_limit), set_compute_unit_price(price)]
        from sniper.execution.solana_sender_ab import tip_instructions
        ixs = list(ixs) + list(tip_instructions(user, tip))     # Sender tip, last (SENDER_ALL)
        message = MessageV0.try_compile(user, head + ixs, list(leg["tables"]),
                                        Hash.from_string(snapshot.blockhash))
        wire = bytes(VersionedTransaction(message, [NullSigner(user)]))
        if len(wire) > SOLANA_PACKET_LIMIT:
            return None, "transaction is %d bytes, over the %d-byte packet" % (len(wire), SOLANA_PACKET_LIMIT)
        self.last_build = {"bytes": len(wire), "pool": facts.pool, "min_tokens_out": min_out,
                           "leg_age_s": (self.clock() - leg["fetched_at"]
                                         if leg.get("fetched_at") is not None else None)}
        return message, {**({"priority_bid": priority_bid} if priority_bid else {}),
                         "blockhash": snapshot.blockhash, "cu_limit": cu_limit, "leg_hops": leg_hops,
                         "cu_estimate": cu_estimate,
                         "last_valid_height": getattr(snapshot, "last_valid_height", None),
                         "target_mint": buy.mint, "min_tokens_out": min_out, "pool": facts.pool,
                         "unsigned_b64": base64.b64encode(wire).decode(),
                         "route_target": ",".join(leg_route_keys(leg) + [facts.pool]),
                         # The chase cap the minimum out was built with (Gamma's pre-registration).
                         "pump_price_limit": {
                             "source_price_usd": market["price_usd"], "cap_pct": cap_pct,
                             "cost_usdc_atomic": cost_usdc, "base_decimals": int(decimals),
                             "spend_lamports": amount_in, "expected_tokens_atomic": None,
                             "output_floor_atomic": None, "price_floor_atomic": min_out,
                             "min_tokens_atomic": min_out, "price_basis": "source_average"}}
