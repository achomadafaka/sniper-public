"""The bound PumpSwap pool as the stop's native rung on Solana positions (2026-10-17, Alpha).

WHY.
- 73927 Gigabite (exotic, SPCX-quoted): one Jupiter whole-bag quote read 5.96e-05 (-54.6%) while the
  entry pool read 1.275e-04 (-2.9%); b_stop sold for 38.01 USDC (-56%) on a transient bad route.
- Gamma since 10-06: 13 of 203 funded Solana positions had a blind window (no exact quote >= 30 s,
  median 2 min, max 56 min), every one a per-mint route failure; three blind b_stops crossed -25%
  on the on-chain mark 4-12 min before the exit and sold at -35 to -38%.

THE NATIVE RUNG. The exact bag sold into the position's bound AMM pool (sol_position_pools), priced
from the swap-mark stream's vault pair (no I/O) and the quote asset's cached USD, as a BAND:
- low:  real reserves only, fee MEMECOIN_SOLANA_NATIVE_RUNG_FEE_HIGH_BPS (above PumpSwap's top tier);
- high: reserves + the Pool account's virtual quote [245:253], fee MEMECOIN_SOLANA_NATIVE_RUNG_FEE_LOW_BPS.
ONLY A VERIFIED PUMPSWAP POOL (account owner pAMMBay6..., a Pool account) has a rung: at most two
background READ RPC calls resolve a bound coin vault through its authority, never on this thread;
until it lands, and for every other AMM
(Raydium, Meteora DLMM/CLMM: the vault ratio is not their price), the rung is unreadable.
A stop is HELD only when even the low bound sits above it; the low bound at or past the stop fires
it. Unreadable is never zero, and never a hold.

THE STOP (b_stop, Jupiter-confirmed):
- Native rung unreadable or stale: the stop stands (audit code route_unreadable), as today.
- Jupiter within X% (MEMECOIN_SOLANA_NATIVE_RUNG_DIVERGENCE_PCT) of the native low bound: the rungs
  agree, the stop stands.
- Catastrophe stays on the WORST rung: Jupiter at or past the floor always fires.
- Diverged and the low bound at or past the stop: fires. Diverged otherwise: held (route_divergent).
- A hold lasts at most the contract's unquotable_exit_s from its first tick, or
  MEMECOIN_SOLANA_NATIVE_RUNG_EXOTIC_HOLD_S (120) on a pool quoted in neither SOL nor USDC: the native
  value prices the coin in the quote asset and that asset at its cached USD, never the quote-token ->
  SOL route Jupiter may be pricing. Every tick re-decides.
JUPITER BLIND (no exact quote): `blind_view` reports whether the native rung alone would stop; the
sweep records it on its rails_blind audit row. It does not sell: selling through the bound pool is
phase B (memecoin native exit), which this view will drive.
Every decision records which rung decided and the on-chain mark (`rung_mark_unit`)."""
from __future__ import annotations

import base64
import logging
import math
import threading

logger = logging.getLogger(__name__)

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

_HELD_SINCE = {}
_VIRTUAL_QUOTE = {}           # pool -> virtual quote atoms (PumpSwap Pool [245:253]) or NOT_PUMPSWAP
NOT_PUMPSWAP = -1             # the pool account is not a PumpSwap Pool: no native rung, ever
_VQ_INFLIGHT = set()
_LOCK = threading.Lock()


def cached_virtual_quote(pool):
    """Read a completed identity verdict without starting a worker or issuing a request."""
    with _LOCK:
        return _VIRTUAL_QUOTE.get(pool)


def virtual_quote(pool, *, fetch_fn=None):
    """The pool's virtual quote, NOT_PUMPSWAP, or None while unknown (a background read is started
    once). ONLY A VERIFIED PUMPSWAP POOL GETS A RUNG: `amm` bindings include Raydium, Meteora DLMM and
    CLMM pools, where the vault ratio is not the price (DLMM median 4.40x the swap mark)."""
    with _LOCK:
        if pool in _VIRTUAL_QUOTE:
            return _VIRTUAL_QUOTE[pool]
        if pool in _VQ_INFLIGHT:
            return None
        _VQ_INFLIGHT.add(pool)

    def run():
        try:
            value = (fetch_fn or _fetch_virtual_quote)(pool)
            if type(value) is int and (value == NOT_PUMPSWAP or value >= 0):
                with _LOCK:
                    _VIRTUAL_QUOTE[pool] = value
        except Exception:
            logger.warning("native rung: pool %s virtual quote unreadable", pool, exc_info=True)
        finally:
            with _LOCK:
                _VQ_INFLIGHT.discard(pool)
    threading.Thread(target=run, name="native-rung-pool", daemon=True).start()
    return None


def _fetch_virtual_quote(pool):
    from solders.pubkey import Pubkey
    from sniper.execution.pump_native_exit import TOKEN_PROGRAM, TOKEN_2022_PROGRAM
    from sniper.execution.pumpswap_buy import (
        AMM_PROGRAM, POOL_ACCOUNT_DISCRIMINATOR, POOL_VIRTUAL_QUOTE_OFFSET, parse_pool_account)
    from sniper.execution.solana_real_seams import _rpc_call
    from sniper.settings.memecoin import solana_read_rpc
    url = solana_read_rpc()
    if not url:
        return None
    def read(address):
        status, body = _rpc_call(url, "getAccountInfo", [address, {
            "encoding": "base64", "commitment": "confirmed"}], None)
        if status != 200 or not isinstance(body, dict) or body.get("error") is not None:
            return None
        result = body.get("result")
        value = result.get("value") if isinstance(result, dict) else None
        if isinstance(value, dict) and value.get("owner"):
            try:
                Pubkey.from_string(value["owner"])
            except (TypeError, ValueError):
                return None
        return value if isinstance(value, dict) else None
    value = read(pool)
    if value is None or not value.get("owner"):
        return None
    vault_mint = None
    try:
        if value["owner"] in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            raw = base64.b64decode(value["data"][0], validate=True)
            if len(raw) < 165 or raw[108] != 1:
                return None
            vault_mint = str(Pubkey.from_bytes(raw[:32]))
            value = read(str(Pubkey.from_bytes(raw[32:64])))
            if value is None or not value.get("owner"):
                return None
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if value.get("owner") != AMM_PROGRAM:
        return NOT_PUMPSWAP
    try:
        raw = base64.b64decode(value["data"][0], validate=True)
        if len(raw) < 8:
            return None
        if raw[:8] != POOL_ACCOUNT_DISCRIMINATOR:
            return NOT_PUMPSWAP
        if len(raw) < POOL_VIRTUAL_QUOTE_OFFSET + 8:
            return None
        facts = parse_pool_account(value["data"][0])
        if vault_mint is not None and (facts["pool_base_token_account"] != pool
                                       or facts["base_mint"] != vault_mint):
            return NOT_PUMPSWAP
        return facts["virtual_quote"] if facts["virtual_quote"] >= 0 else None
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def native_rung_usd(pair, amount_atomic, quote_usd, fee_bps, virtual=0):
    """USD the exact bag fetches sold into the pool, or None. PumpSwap sell: gross quote out =
    (quote + virtual) * in / (base + in), capped at the real quote reserve, fee taken on the output."""
    try:
        coin, quote = int(pair["coin_atoms"]), int(pair["quote_atoms"])
        dec, amount, usd = int(pair["quote_decimals"]), int(amount_atomic), float(quote_usd)
        virtual = int(virtual)
    except (KeyError, TypeError, ValueError):
        return None
    if coin <= 0 or quote <= 0 or amount <= 0 or virtual < 0 or not math.isfinite(usd) or usd <= 0:
        return None
    gross = min((quote + virtual) * amount // (coin + amount), quote)
    out_atoms = gross * (10_000 - int(fee_bps)) // 10_000
    value = out_atoms / 10 ** dec * usd
    return value if math.isfinite(value) and value > 0 else None


def band(pair, amount_atomic, quote_usd, *, fee_low_bps, fee_high_bps, virtual):
    """(low, high) USD for a VERIFIED PumpSwap pool (virtual is its virtual quote), else (None, None):
    unknown ownership (None) and NOT_PUMPSWAP both mean no rung."""
    if virtual is None or virtual == NOT_PUMPSWAP or virtual < 0:
        return None, None
    return (native_rung_usd(pair, amount_atomic, quote_usd, fee_high_bps, 0),
            native_rung_usd(pair, amount_atomic, quote_usd, fee_low_bps, virtual))


def mark_unit(pair, quote_usd, base_decimals):
    """The on-chain spot (real reserves) in USD per token, for the trace; None when unknown."""
    try:
        spot = (int(pair["quote_atoms"]) / 10 ** int(pair["quote_decimals"])) / (
            int(pair["coin_atoms"]) / 10 ** int(base_decimals))
        value = spot * float(quote_usd)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _loss(unit, entry_price):
    return (unit / entry_price - 1.0) * 100.0 if unit and entry_price > 0 else None


def judge(pos_id, *, jupiter_usd, native_low, native_high, quantity, display_price, entry_price,
          stop_pct, catastrophe_pct, divergence_pct, max_hold_s, now_ts):
    """(fire: bool, detail) for a b_stop the Jupiter preview already confirmed.

    HELD ONLY ON NATIVE EVIDENCE, AND ONLY WHEN EVEN THE PESSIMISTIC POOL IS ABOVE THE STOP (Gamma BLOCK
    B1/B2 on 801014b45): phase A cannot sell natively, so a hold is safe only then. An unreadable or
    stale native rung keeps the stop (route_unreadable is an audit code, never a hold); the display
    price never decides."""
    unit = (lambda usd: usd / quantity if (usd and quantity) else None)
    jup_unit, low_unit, high_unit = unit(jupiter_usd), unit(native_low), unit(native_high)
    detail = {"rung_jupiter_usd": round(jupiter_usd, 6),
              "rung_native_low_usd": None if native_low is None else round(native_low, 6),
              "rung_native_high_usd": None if native_high is None else round(native_high, 6),
              "rung_jupiter_unit": jup_unit, "rung_native_low_unit": low_unit,
              "rung_native_high_unit": high_unit, "rung_display_price": display_price}

    def release(verdict, decided):
        with _LOCK:
            _HELD_SINCE.pop(pos_id, None)
        detail.update(rung_verdict=verdict, rung_decided=decided)
        return True, detail
    jup_loss = _loss(jup_unit, entry_price)
    if jup_loss is not None and catastrophe_pct > 0 and jup_loss <= -catastrophe_pct:
        return release("catastrophe_worst_rung", "jupiter")
    if low_unit is None or jup_unit is None:
        return release("route_unreadable", "jupiter")
    if jup_unit >= low_unit * (1.0 - divergence_pct / 100.0):
        return release("rungs_agree", "jupiter")
    low_loss = _loss(low_unit, entry_price)
    if low_loss is not None and low_loss <= -stop_pct:
        return release("native_confirms_stop", "native")
    with _LOCK:
        since = _HELD_SINCE.setdefault(pos_id, float(now_ts))
    held = float(now_ts) - since
    detail["rung_held_s"] = round(held, 3)
    if max_hold_s and held >= max_hold_s:
        return release("hold_expired", "jupiter")
    detail.update(code="route_divergent", rung_verdict="route_divergent", rung_decided="native")
    return False, detail


def is_exotic(pair):
    return bool(pair) and pair.get("quote_mint") not in (WSOL_MINT, USDC_MINT)


def blind_view(*, native_low, native_high, quantity, entry_price, stop_pct, catastrophe_pct):
    """What the native rung alone says while Jupiter is blind: {native_stop: bool | None, ...}.
    True only when even the best known bound is at or past the stop (or the catastrophe floor)."""
    best = native_high if native_high is not None else native_low
    best_unit = best / quantity if (best and quantity) else None
    loss = _loss(best_unit, entry_price)
    return {"native_low_usd": native_low, "native_high_usd": native_high, "native_best_unit": best_unit,
            "native_best_pnl_pct": None if loss is None else round(loss, 4),
            "native_stop": None if loss is None else bool(
                loss <= -stop_pct or (catastrophe_pct > 0 and loss <= -catastrophe_pct))}
