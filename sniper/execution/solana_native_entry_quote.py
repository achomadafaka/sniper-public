"""The J7 native quote, SHADOW ONLY (design section 12, B1; Alpha GO 2026-09-28).

A general-path Solana entry on a pump.fun curve or a canonical PumpSwap pool can be priced from
chain state instead of Jupiter's /quote. This module computes that native quote BESIDE the Jupiter
quote and logs the two side by side. It is never used: execute_entry hands it the Jupiter quote
and moves on without waiting.

What it reads, on its own daemon thread, starting when execute_entry starts:
1. ONE getMultipleAccounts: the bonding curve PDA ["bonding-curve", mint], the canonical PumpSwap
   pool PDA ["pool", 0u16, PDA["pool-authority", mint], mint, wSOL] (11/11 on the fee fixture),
   and both pump_fees FeeConfig accounts.
2. For a graduated coin, a second getMultipleAccounts for the pool's two vaults.
Fees come from pump_fees (60 live buys; a mayhem curve is refused).

What it compares: Jupiter's COIN hop (the routePlan step with inputMint wSOL and outputMint the
coin) against the native output for that hop's own SOL input, on the reserves just read. That
isolates the pool model from the USDC->SOL leg. `same_pool` says whether Jupiter used the account
we read.

Modes (MEMECOIN_ENTRY_NATIVE_QUOTE): off | shadow | live. "live" runs as shadow until Alpha's GO.
The live gate: n >= 20 qualifying decisions, native within 0.5% of Jupiter on the same reserves
slot, 0 model misses.
"""
from __future__ import annotations

import base64
import logging
import struct
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

WSOL_MINT = "So11111111111111111111111111111111111111112"
CURVE_LABELS = ("Pump.fun",)
AMM_LABELS = ("Pump.fun Amm",)
TOKEN_AMOUNT_OFFSET = 64          # SPL / Token-2022 account: mint 0, owner 32, amount 64


def mode():
    """off | shadow. "live" runs as shadow (refused in code until Alpha's GO); anything else off."""
    from sniper.settings import memecoin as cfg
    value = str(getattr(cfg, "MEMECOIN_ENTRY_NATIVE_QUOTE", "shadow") or "off").strip().lower()
    return "shadow" if value in ("shadow", "live") else "off"


def qualifies(mint):
    """A pump.fun mint by its vanity suffix: a cheap pre-filter so a non-pump coin costs no read.
    The chain read decides the venue; a ground "...pump" mint that is not pump's reads no curve."""
    return isinstance(mint, str) and mint.endswith("pump")


def accounts_for(mint, quote_mint=None):
    """{"curve", "pool", "curve_fee_config", "amm_fee_config"} addresses, all derived. `quote_mint`
    names the pool of a curve quoted in another token (pump migrates it into coin/quote, never
    coin/wSOL); None is wSOL."""
    from sniper.execution.pump_native_exit import FEE_CONFIG_SEED, FEE_PROGRAM, PUMP_PROGRAM, _pda, _pk
    from sniper.execution.pumpswap_buy import AMM_FEE_CONFIG_SEED, AMM_PROGRAM
    base = bytes(_pk(mint))
    authority = _pda([b"pool-authority", base], PUMP_PROGRAM)
    return {
        "curve": str(_pda([b"bonding-curve", base], PUMP_PROGRAM)),
        "pool": str(_pda([b"pool", struct.pack("<H", 0), bytes(authority), base,
                          bytes(_pk(quote_mint or WSOL_MINT))], AMM_PROGRAM)),
        "curve_fee_config": str(_pda([b"fee_config", FEE_CONFIG_SEED], FEE_PROGRAM)),
        "amm_fee_config": str(_pda([b"fee_config", AMM_FEE_CONFIG_SEED], FEE_PROGRAM)),
    }


def _data(account):
    return (account or {}).get("data", [None])[0] if isinstance(account, dict) else None


def read_native_state(mint, rpc, *, commitment=None, quote_mint=None, allow_mayhem=False):
    """The venue and its reserves and fees right now, or {"reason": ...}. `rpc(method, params)`
    returns the JSON-RPC result. Never raises for a refusal; a transport error propagates.
    `commitment` (e.g. "processed", the graduation watch) is passed on both reads; None keeps the
    node's default. A PumpSwap answer also carries the pool's facts, its base token program (the base
    vault's owner), the base decimals and supply, so a buy can be built from it. `quote_mint` reads
    the coin/quote pool of a curve quoted in another token (accounts_for); its fees are the wSOL
    tier rule's, UNVERIFIED for such a pool (HI FAtxUWPp 2026-10-07: modelled creator 75 bps, its
    BuyEvents charged 100), so they price our clip only and the source-price floor bounds the fill.
    A Mayhem curve is refused unless `allow_mayhem`; the curve answer then carries `mayhem_mode` for the buy
    builder's fee recipient."""
    opts = {"encoding": "base64", **({"commitment": commitment} if commitment else {})}
    from sniper.execution import pump_fees as PF
    from sniper.execution.pump_native_exit import PUMP_PROGRAM
    from sniper.execution.pumpswap_buy import AMM_PROGRAM, parse_pool_account
    keys = accounts_for(mint, quote_mint)
    order = ["curve", "pool", "curve_fee_config", "amm_fee_config"]
    res = rpc("getMultipleAccounts", [[keys[k] for k in order], dict(opts)]) or {}
    values = dict(zip(order, (res.get("value") or [None] * len(order))))
    slot = (res.get("context") or {}).get("slot")
    curve = values.get("curve")
    if isinstance(curve, dict) and curve.get("owner") == PUMP_PROGRAM:
        raw = base64.b64decode(_data(curve) or "")
        if len(raw) < 82:
            return {"reason": "curve_unreadable", "slot": slot}
        if raw[48] == 0:                               # not complete: the curve is the venue
            try:
                config = PF.parse_fee_config(_data(values.get("curve_fee_config")))
            except (TypeError, ValueError):
                return {"reason": "fee_config_unreadable", "slot": slot, "venue": "curve"}
            if raw[81] != 0 and not allow_mayhem:
                return {"reason": "mayhem_or_fee_unmodelled", "slot": slot, "venue": "curve"}
            fees = PF.curve_fee_bps(config, _data(curve))
            if fees is None:
                return {"reason": "mayhem_or_fee_unmodelled", "slot": slot, "venue": "curve"}
            v_tok, v_sol, r_tok = struct.unpack_from("<3Q", raw, 8)
            return {"venue": "curve", "account": keys["curve"], "slot": slot,
                    "virtual_token_reserves": v_tok, "virtual_sol_reserves": v_sol,
                    "real_token_reserves": r_tok, "mayhem_mode": raw[81] != 0, "fee_basis_points": fees[0],
                    "creator_fee_basis_points": fees[1], "cashback_fee_basis_points": 0,
                    "fees": (0, fees[0], fees[1])}
    pool = values.get("pool")
    if not (isinstance(pool, dict) and pool.get("owner") == AMM_PROGRAM):
        return {"reason": "no_curve_or_canonical_pool", "slot": slot}
    try:
        facts = parse_pool_account(_data(pool))
        config = PF.parse_fee_config(_data(values.get("amm_fee_config")))
    except (TypeError, ValueError):
        return {"reason": "pool_or_fee_unreadable", "slot": slot, "venue": "pumpswap"}
    # A quote-token pool also reads its quote mint (decimals); a wSOL read is the same three accounts.
    extra = [quote_mint] if quote_mint is not None else []
    vaults = rpc("getMultipleAccounts", [[facts["pool_base_token_account"],
                                           facts["pool_quote_token_account"], facts["base_mint"], *extra],
                                          dict(opts)]) or {}
    base_acc, quote_acc, mint_acc = (vaults.get("value") or [None, None, None])[:3]
    quote_decimals = 9
    if quote_mint is not None:
        try:
            quote_decimals = base64.b64decode(_data((vaults.get("value") or [None] * 4)[3]))[44]
        except (TypeError, ValueError, IndexError):
            return {"reason": "vault_unreadable", "slot": slot, "venue": "pumpswap"}
    try:
        base_reserve = struct.unpack_from("<Q", base64.b64decode(_data(base_acc)), TOKEN_AMOUNT_OFFSET)[0]
        quote_reserve = struct.unpack_from("<Q", base64.b64decode(_data(quote_acc)), TOKEN_AMOUNT_OFFSET)[0]
        mint_raw = base64.b64decode(_data(mint_acc))
        supply, decimals = struct.unpack_from("<Q", mint_raw, 36)[0], mint_raw[44]
    except (TypeError, ValueError, struct.error, IndexError):
        return {"reason": "vault_unreadable", "slot": slot, "venue": "pumpswap"}
    fees = PF.amm_fee_bps(config, facts, base_supply=supply, base_reserve=base_reserve,
                          quote_reserve=quote_reserve)
    if fees is None:
        return {"reason": "fee_unmodelled", "slot": slot, "venue": "pumpswap"}
    return {"venue": "pumpswap", "account": keys["pool"], "slot": slot,
            "vault_slot": (vaults.get("context") or {}).get("slot"),
            "base_reserve": base_reserve, "quote_reserve": quote_reserve,
            "virtual_quote": int(facts["virtual_quote"]), "fees": tuple(fees),
            "pool_facts": facts, "base_decimals": int(decimals), "base_supply": int(supply),
            "base_token_program": (base_acc or {}).get("owner"),
            "quote_token_program": (quote_acc or {}).get("owner"),
            **({"quote_decimals": int(quote_decimals)} if quote_mint is not None else {})}


def native_out(state, sol_in):
    """Coin atomic a buy spending `sol_in` lamports receives on `state` (the lane's own exact math)."""
    if state.get("venue") == "curve":
        from sniper.execution.pump_curve_buy import expected_tokens_out
        return expected_tokens_out(sol_in, state)
    from sniper.execution.pumpswap_buy import expected_base_out
    return expected_base_out(sol_in, state["base_reserve"], state["quote_reserve"],
                             state["virtual_quote"], sum(state["fees"]))


def coin_hop(quote, mint):
    """Jupiter's hop into the coin, as its swapInfo dict, or None."""
    for step in (quote or {}).get("routePlan") or ():
        info = (step or {}).get("swapInfo") or {}
        if info.get("outputMint") == mint:
            return info
    return None


def shadow_line(mint, state, quote, *, usd_amount, read_ms, jup_ms, native_first, attempt_id):
    hop = coin_hop(quote, mint)
    fields = {"venue": state.get("venue"), "reason": state.get("reason") or "ok",
              "read_ms": "%.1f" % read_ms, "jup_ms": "%.1f" % jup_ms if jup_ms is not None else None,
              "native_first": native_first, "slot": state.get("slot"),
              "vault_slot": state.get("vault_slot"), "fees": state.get("fees")}
    if hop is not None:
        fields.update(jup_label=hop.get("label"), jup_in_mint=str(hop.get("inputMint"))[:8],
                      jup_in=hop.get("inAmount"), jup_out=hop.get("outAmount"),
                      jup_slot=hop.get("updateContextSlot"),
                      same_pool=hop.get("ammKey") == state.get("account"))
    slippage = (quote or {}).get("slippageBps")
    fields["jup_min"] = (quote or {}).get("otherAmountThreshold")
    fields["jup_total_out"] = (quote or {}).get("outAmount")
    if not state.get("reason") and hop is not None and hop.get("inputMint") == WSOL_MINT:
        try:
            out = native_out(state, int(hop["inAmount"]))
            jup = int(hop["outAmount"])
            fields["native_out"] = out
            fields["delta_pct"] = "%.4f" % ((out / jup - 1.0) * 100.0) if jup > 0 else None
            if slippage is not None:
                fields["native_min"] = out * (10_000 - int(slippage)) // 10_000
            fields["native_usd_per_token_x1e9"] = (
                "%.6f" % (float(usd_amount) / out * 1e9) if out > 0 else None)
        except (KeyError, TypeError, ValueError):
            fields["reason"] = "hop_unreadable"
    elif not state.get("reason"):
        fields["reason"] = "jupiter_no_sol_hop" if hop is not None else "jupiter_no_coin_hop"
    return ("native quote shadow %s attempt=%s %s at=%s" % (
        str(mint)[:8], attempt_id,
        " ".join("%s=%s" % (k, v) for k, v in fields.items()),
        datetime.now(timezone.utc).isoformat()))


class NativeQuoteShadow:
    """One entry's native quote, read on its own thread and logged ONCE beside the Jupiter quote.

    execute_entry calls `offer_jupiter(quote)` and returns at once; whichever of the read and the
    offer finishes second writes the line. A read that never gets a Jupiter quote (the entry
    stopped first) logs nothing. Nothing here can raise into the entry."""

    def __init__(self, mint, usd_amount, *, rpc, attempt_id=None, log=None, clock=time.monotonic,
                 start=True):
        self.mint, self.usd_amount, self.rpc, self.attempt_id = mint, usd_amount, rpc, attempt_id
        self.log = log or logger.warning
        self.clock = clock
        self.started = clock()
        self._lock = threading.Lock()
        self._state = None
        self._read_ms = None
        self._quote = None
        self._offered_at = None
        self._native_first = None
        self._emitted = False
        if start:
            threading.Thread(target=self.read, name="native-quote-shadow", daemon=True).start()

    def read(self):
        began = self.clock()
        try:
            state = read_native_state(self.mint, self.rpc)
        except Exception as exc:
            state = {"reason": "read_failed:%s" % type(exc).__name__}
        with self._lock:
            self._state, self._read_ms = state, (self.clock() - began) * 1000.0
            ready = self._quote is not None
        if ready:
            self._emit()

    def offer_jupiter(self, quote):
        with self._lock:
            if self._quote is not None or not isinstance(quote, dict):
                return
            self._quote, self._offered_at = quote, self.clock()
            self._native_first = self._state is not None
            ready = self._state is not None
        if ready:
            self._emit()

    def _emit(self):
        with self._lock:
            if self._emitted:
                return
            self._emitted = True
            state, quote, read_ms = self._state, self._quote, self._read_ms
            jup_ms = (self._offered_at - self.started) * 1000.0 if self._offered_at else None
            native_first = self._native_first
        try:
            self.log(shadow_line(self.mint, state, quote, usd_amount=self.usd_amount,
                                 read_ms=read_ms, jup_ms=jup_ms, native_first=native_first,
                                 attempt_id=self.attempt_id))
        except Exception:
            logger.warning("native quote shadow line failed mint=%s", self.mint, exc_info=True)
