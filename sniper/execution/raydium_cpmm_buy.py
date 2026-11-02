"""Direct Raydium CPMM BUY for the slot lane (lever 1, 2026-09-27). BUILD ONLY; shadow first.

Design: docs/superpowers/specs/2026-09-27-slot-lane-any-venue-design.md, section 6. Every fact here
was checked on live frames before it was written down (80 routed CPMM source buys from the venue
census, 83 swaps, 29 pools, 5 amm configs; research key, 2026-09-27 22:04-22:08Z):

THE SOURCE'S OWN SWAP IS IN THE LOGS the lane already streams: `SwapEvent`, emitted by the CPMM
program itself as `Program data:` while it executes, discriminator 40c6cde8260871e2, 170 bytes:
pool, input_vault_before, output_vault_before, input_amount, output_amount, input_transfer_fee,
output_transfer_fee (u64), base_input (bool), input_mint, output_mint, trade_fee, creator_fee (u64),
creator_fee_on_input (bool). ROUTERS EMIT AN EVENT WITH THE SAME NAME, so the same discriminator (16
of the census lines were 169- and 221-byte router events). A line is CPMM's only when the CPMM
program is the one executing, which `swap_events` tracks on the invoke stack.

THE OUTPUT, exact to the atomic unit on 83 of 83: net_in = input_amount - trade_fee - creator_fee
(creator fee on input); out = floor(output_vault_before * net_in / (input_vault_before + net_in)).
The event's input_amount is what ARRIVED, already net of the input's Token-2022 transfer fee: the
sender paid input_amount + input_transfer_fee (none of the 83 census swaps had one; the first live
one, ANTHROPIC into AjxqTiS6 on 2026-09-27, showed the fee = ceil(3% of that gross) exactly, the
fees split on input_amount, and the curve on its net reproduced the output to the unit). The fee split, exact on 83 of 83 from the pool's own config and
flag: total = ceil(amount x (trade_rate + creator_rate) / 1e6), creator = floor(total x
creator_rate / (trade_rate + creator_rate)), trade = total - creator, with creator_rate = 0 when the
pool's enable_creator_fee is off (6 of 83). A creator fee charged on the OUTPUT was never seen, so
this module refuses it rather than guess.

THE COIN'S TRANSFER FEE. 72 of the 83 census swaps paid a Token-2022 transfer fee on the output (3%
on 56, 1% on 16), rounded up (exact on 72 of 72); the minimum out the program checks is after it.

pool_state (637 bytes): amm_config @8, token_0_vault @72, token_1_vault @104, token_0_mint @168,
token_1_mint @200, token_0_program @232, token_1_program @264, observation @296, mint decimals @331
and @332, creator_fee_on @389, enable_creator_fee @390. amm_config (236 bytes): trade_fee_rate @12,
protocol_fee_rate @20, fund_fee_rate @28, creator_fee_rate @108 (ppm of the trade). swap_base_input:
discriminator 8fbe5adac41e33de, amount_in u64, minimum_amount_out u64, 13 accounts as below; the
authority is the PDA ["vault_and_lp_mint_auth_seed"] (GpMZbSM2..., matched the live frames).
"""
from __future__ import annotations

import base64
import json
import re
import struct
import threading
import time
from dataclasses import dataclass

CPMM_PROGRAM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
CPMM_AUTHORITY = "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL"
SWAP_EVENT_DISCRIMINATOR = bytes.fromhex("40c6cde8260871e2")
SWAP_BASE_INPUT_DISCRIMINATOR = bytes.fromhex("8fbe5adac41e33de")
SWAP_EVENT_LEN = 170
POOL_STATE_LEN = 637
AMM_CONFIG_LEN = 236
FEE_DENOMINATOR = 1_000_000
_INVOKE = re.compile(r"^Program (\S+) invoke \[\d+\]")
_EXIT = re.compile(r"^Program (\S+) (?:success|failed)")


@dataclass(frozen=True)
class CpmmSwap:
    pool: str
    input_vault_before: int
    output_vault_before: int
    input_amount: int
    output_amount: int
    input_transfer_fee: int
    output_transfer_fee: int
    base_input: bool
    input_mint: str
    output_mint: str
    trade_fee: int
    creator_fee: int
    creator_fee_on_input: bool


def _key(raw):
    from solders.pubkey import Pubkey
    return str(Pubkey.from_bytes(bytes(raw)))


def parse_swap_event(raw):
    """One CPMM SwapEvent from its bytes (discriminator included). ValueError on any other shape."""
    if len(raw) != SWAP_EVENT_LEN or raw[:8] != SWAP_EVENT_DISCRIMINATOR:
        raise ValueError("not a %d-byte CPMM SwapEvent" % SWAP_EVENT_LEN)
    b = raw[8:]
    amounts = struct.unpack_from("<6Q", b, 32)
    trade_fee, creator_fee = struct.unpack_from("<2Q", b, 145)
    return CpmmSwap(_key(b[:32]), *amounts, bool(b[80]), _key(b[81:113]), _key(b[113:145]),
                    trade_fee, creator_fee, bool(b[161]))


def program_data(logs, program):
    """The decoded `Program data:` payloads `program` itself emitted in these logs, in order: the
    invoke stack says who is executing, so a router's same-named event is never attributed to it."""
    stack, out = [], []
    for line in logs or ():
        m = _INVOKE.match(line)
        if m:
            stack.append(m.group(1))
            continue
        m = _EXIT.match(line)
        if m and stack and stack[-1] == m.group(1):
            stack.pop()
            continue
        if line.startswith("Program data: ") and stack and stack[-1] == program:
            try:
                out.append(base64.b64decode(line[len("Program data: "):]))
            except ValueError:
                continue
    return out


def swap_events(logs):
    """Every SwapEvent the CPMM program itself emitted in these logs, in order."""
    return [parse_swap_event(raw) for raw in program_data(logs, CPMM_PROGRAM)
            if raw[:8] == SWAP_EVENT_DISCRIMINATOR]


@dataclass(frozen=True)
class PoolState:
    amm_config: str
    vault_0: str
    vault_1: str
    mint_0: str
    mint_1: str
    program_0: str
    program_1: str
    observation: str
    decimals_0: int
    decimals_1: int
    protocol_fees_0: int
    protocol_fees_1: int
    fund_fees_0: int
    fund_fees_1: int
    creator_fee_on: int
    enable_creator_fee: bool
    creator_fees_0: int
    creator_fees_1: int


def parse_pool_state(raw):
    if len(raw) != POOL_STATE_LEN:
        raise ValueError("pool_state is %d bytes, not %d" % (len(raw), POOL_STATE_LEN))
    keys = [_key(raw[o:o + 32]) for o in (8, 72, 104, 168, 200, 232, 264, 296)]
    p0, p1, f0, f1 = struct.unpack_from("<4Q", raw, 341)
    c0, c1 = struct.unpack_from("<2Q", raw, 397)
    return PoolState(*keys, raw[331], raw[332], p0, p1, f0, f1, raw[389], bool(raw[390]), c0, c1)


@dataclass(frozen=True)
class AmmConfig:
    trade_fee_rate: int
    protocol_fee_rate: int
    fund_fee_rate: int
    creator_fee_rate: int


def parse_amm_config(raw):
    if len(raw) != AMM_CONFIG_LEN:
        raise ValueError("amm_config is %d bytes, not %d" % (len(raw), AMM_CONFIG_LEN))
    trade, protocol, fund = struct.unpack_from("<3Q", raw, 12)
    return AmmConfig(trade, protocol, fund, struct.unpack_from("<Q", raw, 108)[0])


def fee_split(amount, trade_rate, creator_rate):
    """(trade_fee, creator_fee) on `amount` (the input after its transfer fee)."""
    rate = int(trade_rate) + int(creator_rate)
    if rate == 0:
        return 0, 0
    total = -(-int(amount) * rate // FEE_DENOMINATOR)
    creator = total * int(creator_rate) // rate
    return total - creator, creator


def curve_out(net_in, reserve_in, reserve_out):
    net_in, reserve_in, reserve_out = int(net_in), int(reserve_in), int(reserve_out)
    if net_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        raise ValueError("no output: net_in %d, reserves %d/%d" % (net_in, reserve_in, reserve_out))
    return reserve_out * net_in // (reserve_in + net_in)


def creator_rate_for(pool, config):
    return int(config.creator_fee_rate) if pool.enable_creator_fee else 0


def creator_fee_on_input(pool, input_is_0):
    """creator_fee_on: 0 both tokens, 1 token 0 only, 2 token 1 only (all enabled census pools were
    on the input side). None = charged on the output, which no live frame has shown."""
    on = pool.creator_fee_on
    if on == 0 or (on == 1 and input_is_0) or (on == 2 and not input_is_0):
        return True
    return None


def event_output(ev):
    """The source swap's output, recomputed from its own event (the unit-exact rule)."""
    if not ev.creator_fee_on_input and ev.creator_fee:
        raise ValueError("creator fee on the output side is not supported")
    net = ev.input_amount - ev.trade_fee - ev.creator_fee       # input_amount already arrived net
    return curve_out(net, ev.input_vault_before, ev.output_vault_before)


def post_trade_reserves(ev, config):
    """(input reserve, output reserve) after the source's swap. The protocol and fund shares of the
    trade fee are held back as counters, not reserves; the creator fee too. UNVERIFIED against a
    fresh pool read: shadow mode compares it (docs, section 6)."""
    protocol = ev.trade_fee * int(config.protocol_fee_rate) // FEE_DENOMINATOR
    fund = ev.trade_fee * int(config.fund_fee_rate) // FEE_DENOMINATOR
    reserve_in = (ev.input_vault_before + ev.input_amount      # what arrived in the vault
                  - protocol - fund - (ev.creator_fee if ev.creator_fee_on_input else 0))
    return reserve_in, ev.output_vault_before - ev.output_amount


def transfer_fee(amount, bps, maximum):
    if not bps:
        return 0
    return min(-(-int(amount) * int(bps) // 10_000), int(maximum))


def expected_buy(amount_in, reserve_in, reserve_out, pool, config, *, input_is_0,
                 out_fee_bps=0, out_fee_max=0, in_fee=(0, 0)):
    """(gross out, out after the coin's transfer fee) for our own buy of `amount_in` (input atomic,
    as sent). `in_fee` = the input asset's Token-2022 transfer fee (bps, max): the pool receives the
    amount less it (ceil), and the fees split on what arrived. SOL and USDC carry none."""
    on_input = creator_fee_on_input(pool, input_is_0)
    creator_rate = creator_rate_for(pool, config)
    if on_input is None and creator_rate:
        raise ValueError("creator fee on the output side is not supported")
    arrived = int(amount_in) - transfer_fee(amount_in, *in_fee)
    trade, creator = fee_split(arrived, config.trade_fee_rate, creator_rate)
    gross = curve_out(arrived - trade - creator, reserve_in, reserve_out)
    return gross, gross - transfer_fee(gross, out_fee_bps, out_fee_max)


def swap_base_input_data(amount_in, minimum_out):
    amount_in, minimum_out = int(amount_in), int(minimum_out)
    if amount_in <= 0 or minimum_out <= 0:
        raise ValueError("swap_base_input needs a positive amount and a positive floor")
    return SWAP_BASE_INPUT_DISCRIMINATOR + struct.pack("<QQ", amount_in, minimum_out)


def swap_base_input_accounts(*, user, pool, state, input_is_0):
    """The 13 accounts in the live frames' order: payer, authority, amm_config, pool_state, input
    token account, output token account, input vault, output vault, input token program, output
    token program, input mint, output mint, observation state."""
    from sniper.execution.pump_native_exit import _pk, ata
    user = _pk(user)
    if input_is_0:
        vin, vout, pin, pout, min_, mout = (state.vault_0, state.vault_1, state.program_0,
                                             state.program_1, state.mint_0, state.mint_1)
    else:
        vin, vout, pin, pout, min_, mout = (state.vault_1, state.vault_0, state.program_1,
                                             state.program_0, state.mint_1, state.mint_0)
    return [
        (user, True, False),
        (_pk(CPMM_AUTHORITY), False, False),
        (_pk(state.amm_config), False, False),
        (_pk(pool), False, True),
        (ata(user, _pk(min_), _pk(pin)), False, True),
        (ata(user, _pk(mout), _pk(pout)), False, True),
        (_pk(vin), False, True),
        (_pk(vout), False, True),
        (_pk(pin), False, False),
        (_pk(pout), False, False),
        (_pk(min_), False, False),
        (_pk(mout), False, False),
        (_pk(state.observation), False, True),
    ]


def swap_base_input_instruction(*, user, pool, state, input_is_0, amount_in, minimum_out):
    from solders.instruction import AccountMeta, Instruction
    from sniper.execution.pump_native_exit import _pk
    metas = [AccountMeta(k, s, w) for k, s, w in swap_base_input_accounts(
        user=user, pool=pool, state=state, input_is_0=input_is_0)]
    return Instruction(_pk(CPMM_PROGRAM), swap_base_input_data(amount_in, minimum_out), metas)


# --- the slot lane: pool cache, market facts, builder --------------------------------------------

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ZEC_MINT = "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS"   # Zcash, 8 decimals, classic SPL
SUPPORTED_QUOTES = {WSOL_MINT: 9, USDC_MINT: 6}   # phase 1; other quote assets need their own leg
LEG_QUOTES = {ZEC_MINT: 8}                        # phase 2b: quote assets reached by a warm leg
# The wSOL leg's pinned dexes carry no USDC/ZEC pool. The deepest one is an Orca Whirlpool
# (GTHKH8s8..., ~$3M on 2026-09-27); one direct hop keeps the packet small.
LEG_QUOTE_PARAMS = {ZEC_MINT: "&onlyDirectRoutes=true&dexes=Whirlpool"}
MAX_PENDING = 64


def cpmm_live():
    """Whether CPMM runs live: MEMECOIN_SLOT_LANE_CPMM="live" (2026-09-28, operator "go live with
    everything", Alpha's spec). Only the quote assets in CPMM_LIVE_QUOTES send."""
    return lane_mode() == "live"


# CPMM LIVE, PER QUOTE ASSET (design section 7). SOL and USDC only (2026-09-28): an exotic quote
# (ZEC, stocks, launchpad tokens) stays shadow, because its live leg and the residue sell path are
# not built. A test pins exactly this set.
CPMM_LIVE_QUOTES = frozenset({WSOL_MINT, USDC_MINT})


def cpmm_live_for(quote_mint):
    """Whether a CPMM buy quoted in `quote_mint` may be reserved and sent."""
    return cpmm_live() and quote_mint in CPMM_LIVE_QUOTES


def zec_leg_on():
    """The ZEC leg spends the FUNDED Jupiter seam about 1.1 calls/s, and no shadow path may call that
    seam (Alpha, 2026-09-27: a 429 there cools the funded hosts for 60 s and costs live entries). So
    MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG takes effect only once ZEC ITSELF is live, not merely CPMM: with
    CPMM live on SOL/USDC, a ZEC pool is still shadow and its leg would be a shadow path."""
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG", False)) and cpmm_live_for(ZEC_MINT)


def supported_quotes():
    """Quote mint -> decimals the lane can build today: SOL and USDC, plus ZEC when its leg is on."""
    out = dict(SUPPORTED_QUOTES)
    if zec_leg_on():
        out.update(LEG_QUOTES)
    return out


def lane_mode():
    """off | shadow | live. Anything unrecognised is off."""
    from sniper.settings import memecoin as cfg
    mode = str(getattr(cfg, "MEMECOIN_SLOT_LANE_CPMM", "off") or "off").strip().lower()
    return mode if mode in ("shadow", "live") else "off"


def _mint_facts(parsed_value, epoch=None):
    """(supply, decimals, transfer fee bps, fee maximum) from a jsonParsed mint, or None. A fee
    schedule that changes at an epoch applies its newer fee from that epoch on and its older fee
    before it (Token-2022 get_epoch_fee); with the current epoch unknown (None) it is None, never
    a guess."""
    info = (((parsed_value or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
    try:
        supply, decimals = int(info["supply"]), int(info["decimals"])
    except (KeyError, TypeError, ValueError):
        return None
    bps, maximum = 0, 0
    for ext in info.get("extensions") or ():
        if ext.get("extension") == "transferFeeConfig":
            state = ext.get("state") or {}
            newer, older = state.get("newerTransferFee") or {}, state.get("olderTransferFee") or {}
            if (newer.get("transferFeeBasisPoints") != older.get("transferFeeBasisPoints")
                    or newer.get("maximumFee") != older.get("maximumFee")):
                if epoch is None:
                    return None
                try:
                    newer = newer if epoch >= int(newer["epoch"]) else older
                except (KeyError, TypeError, ValueError):
                    return None
            bps, maximum = int(newer["transferFeeBasisPoints"]), int(newer["maximumFee"])
    return supply, decimals, bps, maximum


# The current epoch for fee-changing quote mints: ONE cached value, refreshed by the pool-learning
# threads (the prefetch thread's learn_pending), at most once per EPOCH_REFRESH_S; the lane thread
# never reads it from the network. A value older than EPOCH_MAX_AGE_S, or one whose epoch may have
# ended since the read (its remaining slots at MIN_SLOT_S each), is unknown: such a quote refuses.
EPOCH_REFRESH_S = 600.0
EPOCH_MAX_AGE_S = 1800.0
MIN_SLOT_S = 0.35               # Solana slots run ~0.40 s; a faster epoch end is assumed, never a later one
# The prefetch thread also refreshes the funded legs, balances and blockhash in turn: one slow read
# there ages all of them (Lambda, 2026-09-28). The epoch read gets its own bound (Alpha).
EPOCH_READ_TIMEOUT_S = 0.5
_EPOCH = {"value": None, "tried": None}     # value = (epoch, read at, safe until), replaced whole
_EPOCH_LOCK = threading.Lock()


def refresh_epoch(rpc, clock=None):
    """One getEpochInfo, unless one was tried within EPOCH_REFRESH_S or another thread is reading.
    The caller waits at most EPOCH_READ_TIMEOUT_S: the read runs on its own daemon thread and an
    answer after the bound is dropped. A failed or late read keeps the last value (it ages out) and
    waits the full EPOCH_REFRESH_S before the next try."""
    import logging
    import time
    log = logging.getLogger(__name__)
    now = (clock or time.monotonic)()
    if not _EPOCH_LOCK.acquire(blocking=False):
        return
    try:
        if _EPOCH["tried"] is not None and now - _EPOCH["tried"] < EPOCH_REFRESH_S:
            return
        _EPOCH["tried"] = now
        from sniper.execution.pump_native_exit import _rpc_result
        box = []

        def read():
            try:
                box.append(_rpc_result(rpc, "getEpochInfo", []) or {})
            except Exception:
                log.warning("epoch read failed", exc_info=True)
        reader = threading.Thread(target=read, name="slot-lane-epoch-read", daemon=True)
        reader.start()
        reader.join(EPOCH_READ_TIMEOUT_S)
        if reader.is_alive():
            log.warning("epoch read timed out after %.1f s; keeping the last value", EPOCH_READ_TIMEOUT_S)
            return
        if not box:
            return
        try:
            info = box[0]
            left = int(info["slotsInEpoch"]) - int(info["slotIndex"])
            _EPOCH["value"] = (int(info["epoch"]), now, now + left * MIN_SLOT_S)
        except (KeyError, TypeError, ValueError):
            log.warning("epoch read unreadable: %r", box[0])
    finally:
        _EPOCH_LOCK.release()


def epoch_now(clock=None):
    """The cached epoch, or None when unknown, older than EPOCH_MAX_AGE_S, or possibly over.
    Memory only: safe on any thread."""
    import time
    value = _EPOCH["value"]
    if value is None:
        return None
    epoch, read_at, safe_until = value
    now = (clock or time.monotonic)()
    return epoch if now - read_at <= EPOCH_MAX_AGE_S and now < safe_until else None


# Coin extensions the venue builds cover (CPMM and LaunchLab share this list): 55 of 59 LaunchLab
# census coins carry exactly these three, the other 4 only the metadata pair. Any other extension
# refuses: a transfer hook needs accounts the build does not pass; a frozen default state, a pause or
# a non-transferable mint fails the transfer; a permanent delegate can take the coin back.
SUPPORTED_COIN_EXTENSIONS = frozenset({"transferFeeConfig", "metadataPointer", "tokenMetadata"})


def coin_refusal(parsed_value):
    """None when a coin's every extension is one the builds cover and its mint reads (fixed fee
    schedule); else why not: "extension_<name>" for the first unsupported one, or
    "unreadable_or_fee_changing". The shadow lines carry it, so refusals count by extension."""
    info = (((parsed_value or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
    for ext in info.get("extensions") or ():
        if ext.get("extension") not in SUPPORTED_COIN_EXTENSIONS:
            return "extension_%s" % ext.get("extension")
    return None if _mint_facts(parsed_value) is not None else "unreadable_or_fee_changing"


def quote_facts(parsed_value, epoch=None):
    """(decimals, transfer fee bps, fee maximum) for a QUOTE mint a build can move, or None: a
    transfer hook with a program (accounts the build does not pass), a non-transferable mint, a
    frozen default account state or a paused mint, or a fee schedule changing at an epoch while
    `epoch` (epoch_now()) is unknown; with it known, that epoch's fee (older or newer). A
    permanent delegate, scaled UI amount or confidential-transfer config does not stop a transfer
    (simulated on chain with NVDAx and ANTHROPIC, 2026-09-27), so shadow builds them. Shared by the
    CPMM and LaunchLab on-demand legs."""
    info = (((parsed_value or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
    for ext in info.get("extensions") or ():
        name, state = ext.get("extension"), ext.get("state") or {}
        if ((name == "transferHook" and state.get("programId")) or name == "nonTransferable"
                or (name == "defaultAccountState" and state.get("accountState") == "frozen")
                or (name == "pausableConfig" and state.get("paused"))):
            return None
    facts = _mint_facts(parsed_value, epoch)
    return (facts[1], facts[2], facts[3]) if facts else None


def coin_facts(parsed_value):
    """(supply, decimals, transfer fee bps, fee maximum) for a coin the builds cover; else None."""
    return None if coin_refusal(parsed_value) else _mint_facts(parsed_value)


class CpmmPoolCache:
    """pool -> {state, config, mints}, learned OFF the signal path. `get` on the lane thread never
    reads the network: a miss is queued and the prefetch thread learns it (miss-then-learn, as
    PumpSwapPoolCache). Single writer (the prefetch thread); readers see whole dicts."""

    def __init__(self, *, rpc):
        import threading
        self.rpc = rpc
        self._facts, self._configs, self._pending = {}, {}, []
        # USD per whole quote token for leg-reached quotes (ZEC), from the warm leg's own quote.
        # Written by the prefetch thread only.
        self.quote_usd = {}
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
            try:
                self.learn(pool)
            except Exception:
                self.stats["learn_failed"] += 1
                import logging
                logging.getLogger(__name__).warning("cpmm pool learn failed pool=%s", pool,
                                                    exc_info=True)

    def learn(self, pool):
        from sniper.execution.pump_native_exit import _rpc_result
        value = (_rpc_result(self.rpc, "getAccountInfo", [pool, {"encoding": "base64"}]) or {}).get("value")
        if not value or value.get("owner") != CPMM_PROGRAM:
            raise ValueError("not a CPMM pool: %s" % pool)
        state = parse_pool_state(base64.b64decode(value["data"][0]))
        config = self._configs.get(state.amm_config)
        if config is None:
            cv = (_rpc_result(self.rpc, "getAccountInfo", [state.amm_config, {"encoding": "base64"}])
                  or {}).get("value")
            config = self._configs[state.amm_config] = parse_amm_config(base64.b64decode(cv["data"][0]))
        quote_read_at = time.monotonic()       # read start: a slow response never extends freshness
        mints = (_rpc_result(self.rpc, "getMultipleAccounts", [[state.mint_0, state.mint_1],
                                                                {"encoding": "jsonParsed"}]) or {}).get("value") or []
        parsed = {state.mint_0: mints[0] if mints else None,
                  state.mint_1: mints[1] if len(mints) > 1 else None}
        facts = {"state": state, "config": config,
                 "mints": {mint: _mint_facts(value) for mint, value in parsed.items()},
                 # a coin's refusal; the quote side (SOL, USDC, ZEC) is read from the same batch
                 "refusals": {mint: coin_refusal(value) for mint, value in parsed.items()},
                 # each mint's account, for the on-demand leg's quote_facts at use (its fee can
                 # depend on the epoch then, not at learn)
                 "mint_values": parsed, "quote_read_at": quote_read_at}
        self._facts[pool] = facts
        self.stats["learned"] += 1
        return facts


def market_facts(buy, pools, sol_usd, quote=None):
    """(reason, facts) for a CPMM slot buy from its event and the pool cache only (no network).
    `quote` = (decimals, USD per whole token) from an on-demand leg for a quote asset the lane keeps
    no leg for; None = only the lane's own quote assets."""
    ev = getattr(buy, "venue_event", None)
    if not isinstance(ev, CpmmSwap):
        return "slot_lane_event_unreadable", {}
    quotes = supported_quotes()
    if quote is None and ev.input_mint not in quotes:
        return "slot_lane_cpmm_quote_not_supported", {}
    facts = pools.get(ev.pool)
    if facts is None:
        return "pool_unknown", {}
    state, config = facts["state"], facts["config"]
    refusal = (facts.get("refusals") or {}).get(ev.output_mint)
    if refusal:
        return "slot_lane_cpmm_coin_%s" % refusal, {}
    coin = facts["mints"].get(ev.output_mint)
    if coin is None or ev.output_mint not in (state.mint_0, state.mint_1):
        return "supply_unknown", {}
    if creator_fee_on_input(state, ev.input_mint == state.mint_0) is None and creator_rate_for(state, config):
        return "slot_lane_cpmm_creator_fee_on_output", {}
    if quote is not None:
        qdec, quote_usd = int(quote[0]), float(quote[1] or 0.0)
    else:
        qdec = quotes[ev.input_mint]
        quote_usd = (1.0 if ev.input_mint == USDC_MINT else float(sol_usd or 0.0)
                     if ev.input_mint == WSOL_MINT else float(pools.quote_usd.get(ev.input_mint) or 0.0))
    if quote_usd <= 0:
        return "slot_lane_cpmm_quote_price_unknown", {}
    supply, cdec, _bps, _max = coin
    try:
        reserve_in, reserve_out = post_trade_reserves(ev, config)
        price = (reserve_in / 10 ** qdec) / (reserve_out / 10 ** cdec) * quote_usd
        their_usd = (ev.input_amount + ev.input_transfer_fee) / 10 ** qdec * quote_usd   # as paid
    except (TypeError, ValueError, ZeroDivisionError):
        return "slot_lane_price_unreadable", {}
    if not (price > 0 and their_usd > 0):
        return "slot_lane_price_unreadable", {}
    return None, {"price_usd": price, "mcap_usd": price * supply / 10 ** cdec,
                  "their_usd": their_usd, "mint": ev.output_mint, "pool": ev.pool}


class SlotLaneCpmmBuilder:
    """The slot lane's CPMM builder; the same interface as the pump builders. A WSOL pool spends the
    PumpSwap builder's prefetched USDC -> SOL leg (the same shape: output left in the wSOL ATA); a
    USDC pool needs no leg. A ZEC pool (MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG) spends this builder's own
    USDC -> ZEC leg, kept warm by `refresh` on the funded key (role slot_leg). `build` makes no
    network call."""

    def __init__(self, *, user, rpc, leg_fn, max_leg_age_s=None, compute_unit_limit=None,
                 priority_fee_microlamports=None, clock=None, jup_call=None, refresh_after_s=None):
        import time
        from sniper.execution.pump_curve_buy import (
            DEFAULT_COMPUTE_UNIT_LIMIT, LEG_REFRESH_AFTER_S, MAX_LEG_AGE_S)
        self.user = str(user)
        self.rpc = rpc
        self.pools = CpmmPoolCache(rpc=rpc)
        self.leg_fn = leg_fn                  # the PumpSwap builder's wSOL leg
        self.jup_call = jup_call              # funded, counted as slot_leg (pump_curve_buy)
        self.refresh_after_s = float(LEG_REFRESH_AFTER_S if refresh_after_s is None else refresh_after_s)
        self._legs = {}                       # quote mint -> warm leg (ZEC), prefetch thread only
        self._retry_at = {}                   # quote mint -> no re-quote before (after a failure)
        self._alt_cache = {}
        self.max_leg_age_s = float(MAX_LEG_AGE_S if max_leg_age_s is None else max_leg_age_s)
        self.compute_unit_limit = int(compute_unit_limit or DEFAULT_COMPUTE_UNIT_LIMIT)
        self.priority_fee_microlamports = priority_fee_microlamports
        self.clock = clock or time.monotonic
        self.last_build = None

    def refresh(self, clip_usd, snapshot=None):
        """Learn queued pools; keep a leg warm for each leg-reached quote asset that is on (ZEC).
        The wSOL leg is the PumpSwap builder's; it refreshes that one itself."""
        from sniper.execution.pump_curve_buy import (
            DEFAULT_LEG_SLIPPAGE_BPS, _funded_jup_call, clip_atomic, prefetch_usdc_sol_leg)
        self.pools.learn_pending()
        for mint, decimals in supported_quotes().items():
            if mint not in LEG_QUOTES:
                continue
            key, leg, now = clip_atomic(clip_usd), self._legs.get(mint), self.clock()
            if leg is not None and now - leg["fetched_at"] > self.max_leg_age_s:
                # A price older than the build would accept must not price the next source buy.
                self._legs.pop(mint, None)
                self.pools.quote_usd.pop(mint, None)
                leg = None
            if (leg is not None and leg["usdc_atomic"] == key
                    and now - leg["fetched_at"] < self.refresh_after_s) or now < self._retry_at.get(mint, 0.0):
                continue
            fresh = prefetch_usdc_sol_leg(key, DEFAULT_LEG_SLIPPAGE_BPS, user=self.user,
                                          jup_call=self.jup_call or _funded_jup_call, rpc=self.rpc,
                                          alt_cache=self._alt_cache, clock=self.clock,
                                          quote_params=LEG_QUOTE_PARAMS[mint],
                                          unwrap=False, output_mint=mint)
            if fresh is None:
                # A failing route must not re-quote every 0.5 s tick on the funded key.
                self._retry_at[mint] = now + self.refresh_after_s
                continue
            self._legs[mint] = fresh
            try:
                self.pools.quote_usd[mint] = (fresh["usdc_atomic"] / 1e6) / (fresh["out_lamports"] / 10 ** decimals)
            except ZeroDivisionError:
                pass
        return True

    def _leg_for(self, quote_mint):
        return self.leg_fn() if quote_mint == WSOL_MINT and self.leg_fn else self._legs.get(quote_mint)

    REASON_PREFIX = "slot_lane_cpmm"

    def ondemand_quote(self, buy):
        """(quote mint, quote facts (decimals, fee bps, fee max) or None, refusal or None) for the
        on-demand leg worker. Runs on the WORKER's thread: an unknown pool is learned here (one pool
        read, its config if new, its two mints), never on the lane thread."""
        ev = buy.venue_event
        facts = self.pools.get(ev.pool)
        if facts is None:
            try:
                facts = self.pools.learn(ev.pool)
            except Exception:
                return ev.input_mint, None, "pool_unknown"
        return ev.input_mint, quote_facts((facts.get("mint_values") or {}).get(ev.input_mint),
                                          epoch=epoch_now()), None

    accepts_priority_lamports = True     # memecoin_slot_lane.dispatch_build passes the crowded fee

    def build(self, buy, clip_usd, snapshot, *, funding_lamports=None, leg=None, quote=None, tip=None,
              priority_lamports=None):
        """(unsigned MessageV0, validity) or (None, reason). `leg` + `quote` = (decimals, USD, fee
        bps, fee max): an on-demand leg for a quote asset the lane keeps no leg for."""
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
        from sniper.execution.pump_native_exit import ATA_PROGRAM, SYSTEM_PROGRAM, TOKEN_PROGRAM, _pk, ata
        from sniper.settings import memecoin as cfg
        if funding_lamports is not None:
            return None, "native_funding_not_supported_on_cpmm"
        reason, facts = market_facts(buy, self.pools, getattr(snapshot, "sol_usd", None),
                                     quote=quote[:2] if quote else None)
        if reason:
            return None, reason
        if not getattr(snapshot, "blockhash", None):
            return None, "no_blockhash"
        ev = buy.venue_event
        pool = self.pools.get(ev.pool)
        state, config = pool["state"], pool["config"]
        _supply, cdec, fee_bps, fee_max = pool["mints"][ev.output_mint]
        input_is_0 = ev.input_mint == state.mint_0
        user = _pk(self.user)
        ixs, tables = [], []
        in_fee = tuple(quote[2:4]) if quote else (0, 0)
        if ev.input_mint != USDC_MINT:
            if leg is None:
                leg = self._leg_for(ev.input_mint)
            if leg is None or self.clock() - leg["fetched_at"] > self.max_leg_age_s:
                return None, "leg_stale"
            if leg.get("output_mint", WSOL_MINT) != ev.input_mint:
                return None, "leg_wrong_quote"       # never spend one asset's leg on another's pool
            refused = leg_clip_refusal(leg, clip_usd)
            if refused:
                return None, refused
            amount_in, cost_usdc = int(leg["min_out_lamports"]), int(leg["usdc_atomic"])
            ixs, tables = list(leg["ixs"]), list(leg["tables"])
        else:
            amount_in = cost_usdc = clip_atomic(clip_usd)
        reserve_in, reserve_out = post_trade_reserves(ev, config)
        try:
            gross, expected = expected_buy(amount_in, reserve_in, reserve_out, state, config,
                                            input_is_0=input_is_0, out_fee_bps=fee_bps,
                                            out_fee_max=fee_max, in_fee=in_fee)
            cap_pct = cfg.solana_wallet_chase_cap_pct(
                getattr(buy, "source_user_id", None), cfg.MEMECOIN_REAL_CHASE_MAX_PCT)
            # The hard cap's reference is the source's AVERAGE, from its routed receipt (every
            # slot buy on this venue is routed); facts["price_usd"] is the post-trade price (SOL-1).
            # Bounded by the swap event's own average (N1 residual 1, direct path): coins received
            # net of the coin's transfer fee, input as paid.
            received = ev.output_amount - ev.output_transfer_fee
            average = bounded_whole_average(
                routed_average_price_usd(getattr(buy, "routed", None), cdec),
                facts["their_usd"] / (received / 10 ** cdec) if received > 0 else None)
            min_out, output_floor, price_floor = capped_min_out(
                expected, cap_pct, source_price_usd=facts["price_usd"],
                source_cost_usdc_atomic=cost_usdc, source_price_cap_pct=cap_pct, base_decimals=cdec,
                source_average_price_usd=average,
                copy_cap_pct=cfg.copy_premium_cap(getattr(buy, "source_user_id", None), "solana")[0],
                same_slot=buy_same_slot(buy))
        except ValueError as exc:
            return None, str(exc)[:120]
        coin, coin_prog = _pk(ev.output_mint), _pk(state.program_1 if input_is_0 else state.program_0)
        ixs.append(Instruction(_pk(ATA_PROGRAM), bytes([1]), [      # 1 = CreateIdempotent
            AccountMeta(user, True, True), AccountMeta(ata(user, coin, coin_prog), False, True),
            AccountMeta(user, False, False), AccountMeta(coin, False, False),
            AccountMeta(_pk(SYSTEM_PROGRAM), False, False), AccountMeta(coin_prog, False, False)]))
        ixs.append(swap_base_input_instruction(user=self.user, pool=ev.pool, state=state,
                                               input_is_0=input_is_0, amount_in=amount_in,
                                               minimum_out=min_out))
        if leg is not None and ev.input_mint == WSOL_MINT:
            wsol_ata = ata(user, _pk(WSOL_MINT), _pk(TOKEN_PROGRAM))
            ixs.append(Instruction(_pk(TOKEN_PROGRAM), bytes([9]), [          # 9 = CloseAccount
                AccountMeta(wsol_ata, False, True), AccountMeta(user, False, True),
                AccountMeta(user, True, False)]))
        cu_limit, leg_hops, cu_estimate = (
            cfg.slot_lane_exotic_cu(leg, "raydium_cpmm", self.compute_unit_limit) if quote is not None
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
        size = len(wire)
        if size > SOLANA_PACKET_LIMIT:
            return None, "transaction is %d bytes, over the %d-byte packet" % (size, SOLANA_PACKET_LIMIT)
        amounts = {"spend_lamports": amount_in, "expected_tokens_out": expected,
                   "output_floor_atomic": output_floor, "source_price_floor_atomic": price_floor,
                   "min_tokens_out": min_out, "source_average_price_usd": average}
        self.last_build = {**amounts, "bytes": size, "pool": ev.pool,
                           "leg_age_s": (self.clock() - leg["fetched_at"]) if leg else None}
        return message, {**({"priority_bid": priority_bid} if priority_bid else {}),
                         "blockhash": snapshot.blockhash, "cu_limit": cu_limit, "leg_hops": leg_hops,
                         "cu_estimate": cu_estimate,
                         "last_valid_height": getattr(snapshot, "last_valid_height", None),
                         "target_mint": ev.output_mint,
                         "expected_tokens_out": expected, "min_tokens_out": min_out,
                         "expected_gross": gross,
                         "event_reserve_in": reserve_in, "event_reserve_out": reserve_out,
                         "input_is_0": input_is_0, "pool": ev.pool,
                         # the unsigned wire bytes, for the shadow verifier's simulation only
                         "unsigned_b64": base64.b64encode(wire).decode(),
                         "route_target": ",".join((leg_route_keys(leg) if leg else []) + [ev.pool]),
                         "pump_price_limit": price_limit_trace(
                             amounts, source_price_usd=facts["price_usd"], cap_pct=cap_pct,
                             cost_usdc_atomic=cost_usdc, base_decimals=cdec, same_slot=buy_same_slot(buy))}


def fresh_reserves(rpc, pool, *, input_is_0):
    """(input reserve, output reserve, context slot) read now: vault amounts minus the pool's
    protocol, fund and creator counters (the formula the on-chain simulation matched to the unit)."""
    from sniper.execution.pump_native_exit import _rpc_result
    head = _rpc_result(rpc, "getMultipleAccounts", [[pool], {"encoding": "base64"}]) or {}
    state = parse_pool_state(base64.b64decode(head["value"][0]["data"][0]))
    res = _rpc_result(rpc, "getMultipleAccounts", [[state.vault_0, state.vault_1],
                                                   {"encoding": "base64"}]) or {}
    v0, v1 = (struct.unpack_from("<Q", base64.b64decode(v["data"][0]), 64)[0] for v in res["value"])
    r0 = v0 - state.protocol_fees_0 - state.fund_fees_0 - state.creator_fees_0
    r1 = v1 - state.protocol_fees_1 - state.fund_fees_1 - state.creator_fees_1
    return ((r0, r1) if input_is_0 else (r1, r0)) + (((res.get("context") or {}).get("slot")),)


def simulation_value(res):
    """The simulateTransaction reply's value dict, or None when it carries no answer: a null result
    or value, an error envelope, a value with no err key. No answer is unknown (sim_failed), never a
    program error nor a landing (Gamma LOW on SOL-2, 2026-10-06)."""
    value = res.get("value") if isinstance(res, dict) else None
    return value if isinstance(value, dict) and "err" in value else None


def simulated_outcome(value, pool):
    """(ok, gross output of our swap on `pool`) from a simulateTransaction result value. The ERROR
    decides, never an event alone (a failed LaunchLab simulation logs its event before the
    slippage check); only an event on our own pool is read (a leg can route through CPMM too)."""
    if not isinstance(value, dict) or value.get("err") is not None:
        return False, None
    events = [e for e in swap_events(value.get("logs") or []) if e.pool == pool]
    return (True, events[-1].output_amount) if events else (False, None)


def shadow_line(buy, result, fresh, sim=None):
    """One stamped-by-the-caller line per CPMM shadow event, for the shadow read. sim_out is the
    simulated swap's gross output (before the coin's transfer fee); exp and min are after it."""
    ev = buy.venue_event
    fin, fout, fslot = fresh if fresh else (None, None, None)
    ok, out, err, sslot = sim if sim else (None, None, None, None)
    ein, eout = result.get("event_reserve_in"), result.get("event_reserve_out")
    delta = lambda f, e: (f - e) if f is not None and e is not None else None
    return ("[memecoin] slot lane cpmm %s %s %s pool=%s S=%s -> %s age_ms=%s build_ms=%s "
            "exp=%s min=%s gross=%s reserve_in_delta=%s reserve_out_delta=%s fresh_slot=%s "
            "sim_ok=%s sim_out=%s sim_err=%s sim_slot=%s quote=%s leg_ms=%s residue_usd=%s "
            "sig=%s recv=%s" % (
                result.get("lane_mode") or "shadow",
                getattr(buy, "handle", "?"), str(buy.mint)[:8], str(ev.pool)[:8], buy.slot,
                result.get("reason"),
                "%.1f" % result["signal_age_ms"] if result.get("signal_age_ms") is not None else None,
                "%.1f" % result["build_ms"] if result.get("build_ms") is not None else None,
                result.get("expected_tokens_out"), result.get("min_tokens_out"),
                result.get("expected_gross"), delta(fin, ein), delta(fout, eout), fslot, ok, out,
                json.dumps(err, separators=(",", ":")) if err is not None else None, sslot,
                str(ev.input_mint)[:8],
                "%.1f" % result["leg_ms"] if result.get("leg_ms") is not None else None,
                "%.4f" % result["residue_usd"] if result.get("residue_usd") is not None else None,
                getattr(buy, "signature", None), getattr(buy, "recv_utc", None)))


class ShadowVerifier:
    """A bounded worker that reads each shadow-built pool once, off the lane thread, and logs the
    event line with the post-trade-reserve vs fresh-read delta; with `sim_rpc` it also simulates
    the build once (never sends) and logs whether it would have landed. A full queue drops the
    event and counts it; nothing here can refuse, delay or send an entry.

    The fresh read uses `rpc` (the builder's, as before). The simulation uses `sim_rpc`, the lane's
    own watch key with a 2 s timeout (raydium_launchlab_buy.http_rpc), never the custody RPC, and it
    never STARTS while the lane thread handles a buy (`idle`, as the LaunchLab verifier)."""

    def __init__(self, *, rpc, log, maxsize=64, sim_rpc=None, idle=None, start=True):
        import queue
        import threading
        self.rpc, self.log, self.sim_rpc, self.idle = rpc, log, sim_rpc, idle
        self._q = queue.Queue(maxsize=maxsize)
        self.stats = {"logged": 0, "dropped": 0, "read_failed": 0, "sim_ok": 0, "sim_err": 0,
                      "sim_failed": 0, "sim_skipped": 0}
        if start:
            threading.Thread(target=self._run, name="slot-lane-cpmm-verify", daemon=True).start()

    def offer(self, buy, result):
        import queue
        try:
            self._q.put_nowait((buy, result))
        except queue.Full:
            self.stats["dropped"] += 1

    def _simulate(self, buy, result):
        from sniper.execution.pump_native_exit import _rpc_result
        from sniper.execution.raydium_launchlab_buy import SIM_IDLE_WAIT_S
        if not result.get("unsigned_b64") or self.sim_rpc is None:
            return None
        if self.idle is not None and not self.idle.wait(timeout=SIM_IDLE_WAIT_S):
            self.stats["sim_skipped"] += 1
            return None
        try:
            res = _rpc_result(self.sim_rpc, "simulateTransaction", [result["unsigned_b64"], {
                "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
                "commitment": "processed"}]) or {}
        except Exception:
            self.stats["sim_failed"] += 1
            import logging
            logging.getLogger(__name__).warning("cpmm shadow simulation failed pool=%s",
                                                buy.venue_event.pool, exc_info=True)
            return None
        value = simulation_value(res)
        if value is None:
            self.stats["sim_failed"] += 1
            return None
        ok, out = simulated_outcome(value, buy.venue_event.pool)
        self.stats["sim_ok" if ok else "sim_err"] += 1
        return ok, out, value.get("err"), (res.get("context") or {}).get("slot")

    def verify_once(self, buy, result):
        fresh = None
        if result.get("event_reserve_in") is not None:
            try:
                fresh = fresh_reserves(self.rpc, buy.venue_event.pool,
                                       input_is_0=bool(result.get("input_is_0")))
            except Exception:
                self.stats["read_failed"] += 1
                import logging
                logging.getLogger(__name__).warning("cpmm shadow fresh read failed pool=%s",
                                                    buy.venue_event.pool, exc_info=True)
        sim = self._simulate(buy, result)
        self.log(shadow_line(buy, result, fresh, sim))
        self.stats["logged"] += 1

    def _run(self):
        while True:
            buy, result = self._q.get()
            try:
                self.verify_once(buy, result)
            except Exception:
                import logging
                logging.getLogger(__name__).warning("cpmm shadow line failed", exc_info=True)
