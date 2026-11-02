"""pump.fun fee basis points from the pump_fees FeeConfig accounts, for a buy with no TradeEvent.

The slot lane prices a copy from the SOURCE's TradeEvent, which carries the fees the program
charged. A general-path (J7) decision has no event, so its native quote reads the fees the way the
program chooses them. Verified read-only on 2026-09-28 against 60 live buys (research key,
flat-gated; tests/fixtures/memecoin/pump_fee_config_frames.json):

- FeeConfig layout: discriminator 8, bump u8, admin 32, flat_fees {lp, protocol, creator} u64 x3,
  fee_tiers vec<{market_cap_lamports_threshold u128, fees {lp, protocol, creator}}>.
- PumpSwap (the AMM config): a CANONICAL pump pool (creator = PDA["pool-authority", mint]) takes the
  highest tier whose threshold is <= its market cap, where
  mcap_lamports = (vault quote + the Pool's virtual_quote) x base supply // vault base.
  Any other pool takes the flat fees. 30/30 (23 canonical, 7 not); a vault-only market cap
  misses one at a tier boundary.
- Curve (the curve config): protocol bps from the config's one tier (95). The CREATOR bps is per coin and
  lives in the bonding-curve account (verified 2026-10-23, 1,000 random mints against the tape's own
  fees: 996 exact, 4 mixed 0/x on the tape): the u64 at 115 (IDL creator_fee_bps) when non-zero, else the config's creator bps
  (30). A cashback coin (byte 82 = 1, 5% of coins) shows creator 0 on the tape because the program
  charges the same 30 bps as cashback_fee_basis_points (4/4 decoded events), so its total is unchanged. A MAYHEM curve (byte 81 = 1) charges a regular trader
  exactly the same way, (95, 30): 806,796 non-agent rows on agent-traded mints over 3 days, zero
  exceptions. The 3 of 5 fixture mayhem buys at (0, 0) are the fee-free Mayhem AGENT's own trades.
  A config with more than one tier is still None, never a guess.
"""
from __future__ import annotations

import base64
import struct

FEE_CONFIG_DISCRIMINATOR = bytes.fromhex("8f3492bbdb7b4c9b")
CURVE_MAYHEM_OFFSET = 81          # bonding curve: ... complete @48, creator @49..81, is_mayhem_mode @81
CURVE_V3_LENGTH = 166             # the IDL's BondingCurve (7616681e)
CURVE_LEGACY_LENGTHS = (125, 151)  # recorded live accounts in tests/fixtures/memecoin/pump_fee_config_frames.json (2026-09-28): the config creator bps applies
CURVE_CREATOR_FEE_OFFSET = 115    # BondingCurve.creator_fee_bps u64 LE (IDL 7616681e), 0 = the config default; is_cashback @82, quote mint @83..114


def parse_fee_config(data_b64):
    """{"flat": (lp, protocol, creator), "tiers": [(threshold_lamports, (lp, protocol, creator))]},
    tiers in account order (ascending thresholds on both live configs). ValueError when unreadable."""
    raw = base64.b64decode(data_b64)
    if raw[:8] != FEE_CONFIG_DISCRIMINATOR or len(raw) < 8 + 1 + 32 + 24 + 4:
        raise ValueError("not a pump_fees FeeConfig (%d bytes)" % len(raw))
    o = 8 + 1 + 32
    flat = struct.unpack_from("<3Q", raw, o)
    o += 24
    n = struct.unpack_from("<I", raw, o)[0]
    o += 4
    if len(raw) < o + n * 40:
        raise ValueError("FeeConfig tiers run past the account (%d tiers)" % n)
    tiers = []
    for _ in range(n):
        lo, hi = struct.unpack_from("<QQ", raw, o)
        tiers.append((lo + (hi << 64), tuple(struct.unpack_from("<3Q", raw, o + 16))))
        o += 40
    if any(a[0] > b[0] for a, b in zip(tiers, tiers[1:])):
        raise ValueError("FeeConfig tiers are not ascending")
    return {"flat": tuple(flat), "tiers": tiers}


def _tier(config, mcap_lamports):
    chosen = None
    for threshold, fees in config["tiers"]:
        if threshold <= mcap_lamports:
            chosen = fees
    return chosen


def amm_fee_bps(config, pool_facts, *, base_supply, base_reserve, quote_reserve):
    """(lp, protocol, creator) bps a PumpSwap buy pays now, or None when unreadable. `pool_facts` is
    pumpswap_buy.parse_pool_account's dict (creator, base_mint, virtual_quote); reserves are the
    vault balances (atomic)."""
    from sniper.execution.pumpswap_buy import is_pump_migrated_pool
    try:
        if not is_pump_migrated_pool(pool_facts):
            return tuple(config["flat"])
        base_reserve = int(base_reserve)
        if base_reserve <= 0:
            return None
        mcap = (int(quote_reserve) + int(pool_facts["virtual_quote"])) * int(base_supply) // base_reserve
    except (KeyError, TypeError, ValueError):
        return None
    return _tier(config, mcap)


def curve_fee_bps(config, curve_data_b64):
    """(protocol, creator) bps a pump.fun curve buy pays, or None: an unreadable or too short account, or a
    config with more than one tier (a curve market-cap basis we have not verified). A mayhem curve is priced
    like any other for a regular trader; the fee-free Mayhem agent's own (0, 0) never comes from here."""
    try:
        raw = base64.b64decode(curve_data_b64)
    except (TypeError, ValueError):
        return None
    if len(raw) not in CURVE_LEGACY_LENGTHS and len(raw) != CURVE_V3_LENGTH:
        return None                                    # a truncated or unknown layout is never guessed
    if len(config["tiers"]) > 1:
        return None
    _lp, protocol, creator = (config["tiers"][0][1] if config["tiers"] else config["flat"])
    if len(raw) == CURVE_V3_LENGTH:
        override = struct.unpack_from("<Q", raw, CURVE_CREATOR_FEE_OFFSET)[0]
        if override >= 10_000:                         # not a basis-point count: unmodelled, never clamped
            return None
        creator = override or creator
    return protocol, creator
