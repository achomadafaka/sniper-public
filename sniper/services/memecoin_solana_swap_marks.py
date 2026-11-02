"""Create short-lived USD marks from confirmed Solana vault swap deltas."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import queue
import struct
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from sniper.services.memecoin_solana_amounts import USDC, WSOL
from sniper.services.memecoin_solana_ws import (
    PUMP_TOKEN_DECIMALS, CurveTape, VaultTape, run_vault_stream)

logger = logging.getLogger("memecoin")

SOURCE = "fastlane_sol_swap"
# Pool-STATE marks (2026-08-14, operator: "get the prices from the same place fomo places its
# fills from, not what they show"). A `sol_sync` frame carries the ABSOLUTE reserves of both
# vaults — the exact state the next fill executes against — and was discarded. Pricing it gives a
# mark on every slot where the pool moved at all, instead of only on completed two-sided swaps,
# and it is the venue's own number rather than an aggregator's display.
STATE_SOURCE = "fastlane_sol_state"
# A pump bonding curve's own reserves (2026-09-27): exact spot from the curve account.
CURVE_SOURCE = "fastlane_sol_curve"
QUALITY = "live"
SUPPORTED_QUOTES = frozenset({WSOL, USDC})

# A STATE mark prices `quote_vault / coin_vault`. On a constant-product pool that ratio IS the spot
# price. On concentrated liquidity it is not, because the vaults hold liquidity for every tick or
# bin. Measured 2026-09-22 on `memecoin_position_marks`, STATE mid over the swap mid of the same
# mint within 5 s: meteora_dlmm median 4.40x (p90 29.5x, 82% off by >20%), raydium_clmm median
# 0.805 (p10 0.546, p90 23.2x, 61% off), against raydium_cpmm 1.012 (p10 0.979) and pumpswap_amm
# 0.923 (p10 0.797, p90 0.983).
#
# The pool's PROGRAM cannot gate this: `sol_pool_vaults.program` holds the SPL vault owner, it is
# NULL for 64% of pools, and pumpswap derives one owner per pool, so it classifies 23.1% of
# STATE-emitting mints. So the gate is the DATA: a STATE mark is admitted only when a recent SWAP
# mark for the same mint and the same quote asset agrees with it inside this band.
#
# THE BAND IS TIED TO THE TIGHTEST FUNDED LOSS RAIL, NOT TO A PERCENTILE. The funded stop, the
# catastrophe floor and the mcap SL read the nominal mid. A 10% bound means an admitted STATE mark
# can never move the read more than 10% against the swap that corroborated it, so it cannot trip a
# 35% stop by itself. The low side is the acute one: raydium_clmm sits BELOW 1 (p10 0.546, a mark
# 45% under the trade price) exactly where pumpswap also sits, which is why no band separates them
# cleanly and why this one is deliberately tight.
#
# REJECTION IS FREE, ACCEPTANCE IS NOT. Corroboration REQUIRES a fresh swap mark, so a rejected
# STATE mark leaves that swap mark in the price map: the mint keeps a price and the lane is exactly
# where it stands today. A wrong acceptance costs a false stop or catastrophe on a funded position.
STATE_CORROBORATION_LO = 0.90
STATE_CORROBORATION_HI = 1.10


@dataclass(frozen=True)
class PoolBinding:
    mint: str
    pool: str
    coin_vault: str
    quote_vault: str
    quote_mint: str
    # The SPL vault OWNER (sol_pool_vaults.program; not the venue's program id). Only the LP rail
    # reads it, to decide whether the pool is a proven constant-product venue.
    vault_owner: str | None = None
    # The pool's venue program id, recorded at bind from the bind transaction (NULL = unknown).
    venue_program: str | None = None


@dataclass(frozen=True)
class QuoteMark:
    mint: str
    pool: str
    quote_mint: str
    quote_per_token: float
    slot: int
    observed_ts: float
    observed_at: str
    source: str = SOURCE


def probe_vault_activity(vault, window_start_ts, now_ts, *, rpc_call=None, min_context_slot=0):
    """A recent confirmed RPC head and vault history must both prove the silence."""
    try:
        if (not math.isfinite(float(window_start_ts))
                or not isinstance(min_context_slot, int) or isinstance(min_context_slot, bool)
                or min_context_slot < 0):
            return "unavailable"
        first_second = math.floor(float(window_start_ts))
    except (TypeError, ValueError, OverflowError):
        return "unavailable"
    if rpc_call is None:
        from sniper.services.memecoin_solana_blocktime import _rpc_call
        from sniper.settings.memecoin import solana_read_rpc
        # These observation keys are separate from the funded custody RPC and its credit pool.
        urls = tuple(dict.fromkeys(filter(None, (
            os.getenv("MEMECOIN_VOLDEATH_SOLANA_PROOF_RPC_URL", "").strip(),
            os.getenv("ONCHAIN_WATCH_HELIUS_RPC_URL", "").strip(),
            os.getenv("MEMECOIN_SOL_H1_ACTIVITY_RPC_URL", "").strip(),
            solana_read_rpc(),
        ))))[:2]
        quiet_at = None
        for url in urls:
            verdict = probe_vault_activity(
                vault, window_start_ts, now_ts,
                rpc_call=lambda method, params, _url=url:
                _rpc_call(_url, method, params, timeout=2), min_context_slot=min_context_slot)
            if verdict == "moving":
                return "moving"
            if verdict == "still":
                quiet_at = time.monotonic()
        return ("still" if quiet_at is not None and time.monotonic() - quiet_at <= 3
                else "unavailable")
    try:
        head = rpc_call("getSlot", [{"commitment": "confirmed"}])
        if (not isinstance(head, int) or isinstance(head, bool) or head <= 0
                or head < min_context_slot):
            return "unavailable"
        head_time = rpc_call("getBlockTime", [head])
        if (not isinstance(head_time, int) or isinstance(head_time, bool)
                or not -3 <= time.time() - head_time <= 3):
            return "unavailable"
        rows = rpc_call("getSignaturesForAddress", [vault, {
            "commitment": "confirmed", "minContextSlot": head, "limit": 100,
        }])
        if not isinstance(rows, list) or len(rows) > 100:
            return "unavailable"
        if not rows:
            return "unavailable"  # a funded pool's own entry touched this account
        prior_slot = head
        activity = False
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("signature"), str)
                    or not row["signature"]):
                return "unavailable"
            slot, stamp = row.get("slot"), row.get("blockTime")
            if (not isinstance(slot, int) or isinstance(slot, bool)
                    or not 0 < slot <= prior_slot
                    or not isinstance(stamp, int) or isinstance(stamp, bool) or stamp <= 0):
                return "unavailable"
            prior_slot = slot
            activity |= stamp >= first_second  # blockTime has one-second resolution
        if not activity and not -3 <= time.time() - head_time <= 3:
            return "unavailable"  # RPC took too long to certify the final seconds
        return "moving" if activity else "still"
    except Exception:
        logger.exception("Solana vault activity RPC unavailable vault=%s", vault)
        return "unavailable"


# THE MINT-WIDE WITNESS (2026-09-28, docs/superpowers/specs/2026-09-28-memecoin-voldeath-mint-witness-design.md).
# The vault probe proves ONE pool quiet. In 4 of 21 funded Solana vol_death fires since 09-22 the
# coin kept trading on OTHER pools of the mint, so the fire was a fake silence. One read of the
# mint's newest signatures closes that. Window [now - (exit_s - 2), now - 1]: blockTime is whole
# validator seconds, a silence-based lower edge vetoed PHISH 66467 on the last trade before its
# silence, and the fire's own second holds the trades that resumed at the fire (Gamma's fit on
# the real windows, tests/fixtures/memecoin/voldeath_mint_window_frames.json).
# 100, not 20 (Lambda's review): on a busy mint twenty rows newer than the window's upper edge
# would fill the page, read as unknown, and fire. The vault probe reads 100 too.
MINT_PAGE = 100
_MINT_LO_MARGIN_S = 2.0
_MINT_HI_MARGIN_S = 1.0
# Created at import and never mutated. Two workers, so a stuck read (DNS is not bounded by the
# socket timeout) cannot starve the next one; a queued read times out as `unknown`, which fires.
_MINT_READ_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sol-voldeath-mint")
# At most two reads in flight (Lambda F3): a third is refused as `busy` (unknown -> fire) instead of
# queueing, so an outage never builds a backlog that bursts into the key when it recovers. A slot
# is released when its read finishes; a timed-out read is cancelled if it has not started.
_MINT_READ_SLOTS = threading.BoundedSemaphore(2)


def mint_window_verdict(rows, *, now_ts, exit_s, own_sigs, limit=MINT_PAGE):
    """(moving, newest_blocktime, count, newest_sig) | (still, None, 0, None) |
    (unknown, cause, 0, None) for one
    getSignaturesForAddress page of the mint (newest first from the API; the verdict does not rely
    on the order). Pure."""
    if not isinstance(rows, list):
        return ("unknown", "malformed", 0, None)
    lo = float(now_ts) - (float(exit_s) - _MINT_LO_MARGIN_S)
    hi = float(now_ts) - _MINT_HI_MARGIN_S
    newest, newest_sig, count, reached_back = None, None, 0, False
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("signature"), str):
            return ("unknown", "malformed", 0, None)
        bt = row.get("blockTime")
        if bt is None:
            continue                              # not yet assigned: unknown for this row, skipped
        if not isinstance(bt, (int, float)) or isinstance(bt, bool):
            return ("unknown", "malformed", 0, None)
        if bt < lo:
            reached_back = True               # the page reaches back past the window
            continue                          # (no early break: the verdict never depends on order)
        if bt > hi or row.get("err") is not None or row["signature"] in own_sigs:
            continue
        count += 1
        if newest is None or bt > newest:
            newest, newest_sig = bt, row["signature"]
    if count:
        return ("moving", newest, count, newest_sig)
    if not reached_back and len(rows) >= limit:
        return ("unknown", "page_short_of_window", 0, None)   # a full page that never reached back
    return ("still", None, 0, None)


def own_mint_signatures(mint, *, conn=None):
    """Every transaction signature WE put on this mint, across all funded positions: fills,
    de-risk and close sigs, and pending-entry sigs (Lambda F2). Read in the background probe from
    the read-only ledger, never on the exit sweep."""
    sql = ("SELECT tx_sig FROM memecoin_fills WHERE tx_sig IS NOT NULL AND position_id IN "
           "(SELECT id FROM memecoin_positions WHERE execution_mode='real' AND mint=?) "
           "UNION SELECT derisk_sig FROM memecoin_positions WHERE execution_mode='real' AND mint=? "
           "AND derisk_sig IS NOT NULL "
           "UNION SELECT close_sig FROM memecoin_positions WHERE execution_mode='real' AND mint=? "
           "AND close_sig IS NOT NULL "
           "UNION SELECT tx_sig FROM memecoin_pending_entries WHERE mint=? AND tx_sig IS NOT NULL "
           "UNION SELECT cleanup_tx_sig FROM memecoin_pending_entries WHERE mint=? "
           "AND cleanup_tx_sig IS NOT NULL")
    if conn is not None:
        return {r[0] for r in conn.execute(sql, (mint,) * 5).fetchall()}
    from contextlib import closing
    from sniper.persistence.db_core import get_readonly_connection
    # Never the cwd: a bare "sniper_tracker.db" resolves against the process's directory, and a
    # 0-byte decoy sits in $HOME. No path means unknown, which fires as before.
    path = os.getenv("DB_PATH") or (os.path.join(os.environ["SNIPER_STATE_ROOT"], "sniper_tracker.db")
                                    if os.getenv("SNIPER_STATE_ROOT") else None)
    if not path:
        raise RuntimeError("no DB_PATH or SNIPER_STATE_ROOT")
    with closing(get_readonly_connection(path)) as c:
        return {r[0] for r in c.execute(sql, (mint,) * 5).fetchall()}


def _mint_rpc_url():
    """The vol_death proof chain without the funded read lane: never the custody RPC, never
    solana_read_rpc(), never the research key."""
    for name in ("MEMECOIN_VOLDEATH_SOLANA_PROOF_RPC_URL", "ONCHAIN_WATCH_HELIUS_RPC_URL",
                 "MEMECOIN_SOL_H1_ACTIVITY_RPC_URL"):
        url = os.getenv(name, "").strip()
        if url:
            return url
    return ""


def _mint_signatures(url, mint, timeout):
    """One getSignaturesForAddress call that RAISES on a JSON-RPC error instead of reading it as
    an empty result (a rejection is not a quiet mint)."""
    import json
    import urllib.request
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                       "params": [mint, {"limit": MINT_PAGE, "commitment": "confirmed"}]}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 Sniper/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        answer = json.loads(r.read().decode())
    if answer.get("error") is not None:
        raise RuntimeError("json-rpc error %s" % str(answer.get("error"))[:80])
    return answer.get("result")


def probe_mint_activity(mint, *, now_ts, exit_s, own_sigs, rpc_call=None, deadline_s=0.4):
    """The mint's newest signatures judged by mint_window_verdict, within `deadline_s` of wall
    clock. Any failure is (unknown, cause, 0, None), which the caller FIRES on."""
    if rpc_call is None:
        url = _mint_rpc_url()
        if not url:
            return ("unknown", "no_url", 0, None)
        call = lambda: _mint_signatures(url, mint, deadline_s)   # noqa: E731
    else:
        call = lambda: rpc_call("getSignaturesForAddress",   # noqa: E731
                                [mint, {"limit": MINT_PAGE, "commitment": "confirmed"}])
    if not _MINT_READ_SLOTS.acquire(blocking=False):
        return ("unknown", "busy", 0, None)
    try:
        future = _MINT_READ_POOL.submit(call)
    except Exception:
        _MINT_READ_SLOTS.release()
        raise
    future.add_done_callback(lambda _f: _MINT_READ_SLOTS.release())
    try:
        rows = future.result(timeout=deadline_s)
    except TimeoutError:
        future.cancel()
        return ("unknown", "timeout", 0, None)
    except Exception as exc:
        return ("unknown", "provider:%s" % type(exc).__name__, 0, None)
    return mint_window_verdict(rows, now_ts=now_ts, exit_s=exit_s, own_sigs=set(own_sigs or ()))


def bound_curve_for_position(position_id, mint):
    """The curve PDA of a position's entry-bound native-SOL Pump curve, or None.

    For a position whose stored entry route is only a label (the slot lane writes 'slot_lane'),
    this binding, resolved from our own entry transaction by resolve_position_pool, is the entry
    identity. None for an AMM binding (the tape keeps its coin vault, not the ammKey), a missing or
    ambiguous binding, or any shape other than pool == coin_vault != quote_vault on native-sol."""
    from sniper.persistence.db_ticks import connect_sol_tape_readonly
    from sniper.services.memecoin_solana_amounts import NATIVE_SOL
    try:
        conn = connect_sol_tape_readonly()
        try:
            rows = conn.execute(
                "SELECT p.pool,p.venue_kind,v.coin_vault,v.quote_vault,v.quote_mint "
                "FROM sol_position_pools p "
                "JOIN sol_pool_vaults v ON v.pool=p.pool AND v.mint=p.mint "
                "WHERE p.position_id=? AND p.mint=?",
                (position_id, mint),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.exception("Solana position binding lookup failed position=%s", position_id)
        return None
    if len(rows) != 1:
        return None
    pool, kind, coin_vault, quote_vault, quote_mint = rows[0]
    if (kind != "pump_curve" or quote_mint != NATIVE_SOL or not quote_vault or not coin_vault
            or pool != coin_vault or quote_vault == coin_vault):
        return None
    return str(quote_vault)


def probe_position_activity(position_id, mint, window_start_ts, now_ts,
                            current_pool_key=None, min_context_slot=0):
    """Resolve the entry-bound venue account off the exit path, then check its history.

    Returns moving | still | unavailable:<cause>. Causes: binding_missing (no single usable
    binding), route_unknown (no single exit pool key), identity_mismatch (the curve or quote asset
    does not match the binding), provider (the history read failed), lookup_failed.
    ⚠️ AN AMM BINDING'S `pool` IS ITS COIN VAULT, never the route's ammKey (65858: pool = coin vault
    68wRQDTV..., route ammKey 7cQBNz...), so it cannot be compared with the exit pool key here:
    1710f7b1 did, and would have blinded every AMM position. For an AMM the "sell routes through
    another pool" protection is vol_death's own check of the exit ammKey against the entry keys.

    PUMP CURVE IDENTITY (2026-09-26, PAIDCATS 65854, Codex ctb audit P0). resolve_position_pool
    stores the TOKEN VAULT as the binding's pool (and coin_vault) and the curve PDA in quote_vault,
    with quote_mint native-sol. The exit route's pool key is the CURVE. This compared the route with
    the token vault, which never matches a real curve, so every curve position returned unavailable
    before any probe and vol_death could not fire (onchain_pending/unavailable 23:06-23:10Z, closed
    by hand at -$34.61). The route is now compared with the bound curve; the token vault, which
    every curve buy and sell touches, is still what gets probed."""
    from sniper.persistence.db_ticks import connect_sol_tape_readonly
    from sniper.services.memecoin_solana_amounts import NATIVE_SOL
    try:
        conn = connect_sol_tape_readonly()
        try:
            rows = conn.execute(
                "SELECT p.pool,p.venue_kind,v.coin_vault,v.quote_vault,v.quote_mint "
                "FROM sol_position_pools p "
                "JOIN sol_pool_vaults v ON v.pool=p.pool AND v.mint=p.mint "
                "WHERE p.position_id=? AND p.mint=?",
                (position_id, mint),
            ).fetchall()
        finally:
            conn.close()
        if len(rows) != 1 or rows[0][1] not in ("amm", "pump_curve") or not rows[0][2]:
            return "unavailable:binding_missing"
        pool, kind, coin_vault, quote_vault, quote_mint = rows[0]
        if not current_pool_key:
            return "unavailable:route_unknown"
        if kind == "pump_curve":
            if (pool != coin_vault or quote_mint != NATIVE_SOL or not quote_vault
                    or quote_vault == coin_vault or current_pool_key != quote_vault):
                return "unavailable:identity_mismatch"
        verdict = probe_vault_activity(str(coin_vault), window_start_ts, now_ts,
                                       min_context_slot=min_context_slot)
        return verdict if verdict in ("moving", "still") else "unavailable:provider"
    except Exception:
        logger.exception("Solana position vault lookup failed position=%s", position_id)
        return "unavailable:lookup_failed"


# Complete same-slot rows from VaultTape._row: both vaults moved in the slot, so balance0/balance1
# are one consistent post-slot state. sol_sync (one side) is deliberately absent.
LP_COMPLETE_KINDS = ("sol_swap", "sol_burn", "sol_mint")
# PROVEN CONSTANT-PRODUCT VENUES (Codex review 2026-10-21; coverage per Alpha). Only on these does
# x*y stay flat on a swap; on CLMM/DLMM real vault balances shrink near a range edge with no LP
# pulled (Codex's case: L=1, sqrt range 0.5..2, sqrtP 1 -> 1.99 reads as an 87.8% withdrawal).
# A pool is eligible when its bind transaction named a constant-product VENUE PROGRAM
# (sol_pool_vaults.venue_program: PumpSwap, Raydium CPMM, Raydium AMM v4), or, for pools bound
# before that column, when its vault owner is Raydium CPMM's authority (the constant
# raydium_cpmm_buy.CPMM_AUTHORITY signs against) or Raydium AMM v4's authority (owner of the 5
# AMM v4 pools in sol_pool_vaults, and of both AMM v4 true pulls in the 2026-10-21 LP replay).
# Everything else, unknown included, is blind.
from sniper.services.memecoin_solana_amounts import (  # noqa: E402
    PUMPSWAP_PROGRAM, RAYDIUM_AMM_V4_PROGRAM, RAYDIUM_CPMM_PROGRAM)
LP_CONSTANT_PRODUCT_PROGRAMS = frozenset({PUMPSWAP_PROGRAM, RAYDIUM_CPMM_PROGRAM,
                                          RAYDIUM_AMM_V4_PROGRAM})
RAYDIUM_AMM_V4_AUTHORITY = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
LP_CONSTANT_PRODUCT_OWNERS = frozenset({"GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",
                                        RAYDIUM_AMM_V4_AUTHORITY})


def lp_venue_eligible(owner, program):
    return program in LP_CONSTANT_PRODUCT_PROGRAMS or owner in LP_CONSTANT_PRODUCT_OWNERS


class SolanaSwapMarkCache:
    """Store confirmed swap marks until the source disconnects or the mark expires."""

    def __init__(self, on_mark=None, onchain_probe=None, mint_probe=None, own_sigs_fn=None):
        self._lock = threading.Lock()
        self._marks = {}
        # STATE MARKS GET THEIR OWN SLOT (2026-09-22). Both kinds shared one entry per mint, so
        # VaultTape emitting `sol_sync` then `sol_swap` in the same slot destroyed the state mark
        # before the fast lane could persist it. A STATE row survived only when the newest event
        # was one-sided: the SWAP-only share of a mint's marks runs 96% at one mark and 4% above
        # 300, which is a sampling artifact, not a property of the pool.
        self._state_marks = {}
        # CURVE MARKS (2026-09-27): a bonding curve's spot from its own account. Kept apart from the
        # swap/state maps so their corroboration rules stay exactly as they were, and never fed to
        # the reserve evidence below: vol_death's decisions do not change with this source.
        self._curve_marks = {}
        # Raw reserve observations are exit evidence, not prices. Keep them separate from the
        # corroborated, one-price-per-mint mark map. A bounded history can only lose coverage.
        self._reserve_times = {}
        self._reserve_last = {}
        self._reserve_changed = {}
        # POOL LIQUIDITY for the LP-catastrophic rail (2026-10-21): (mint, pool) ->
        # {"max_l", "max_slot", "l", "slot", "at"}, fed ONLY by complete same-slot rows (both vaults
        # moved in the slot, so their post balances are one consistent state). A one-sided sol_sync
        # pairs a fresh vault with a stale one and never touches it. One pool per mint: a rebind
        # drops the old pool, and the new pool is blind until its own first complete row.
        self._lp = {}
        self._lp_bound = {}           # mint -> (current bound pool, vault owner), from the stream
        self.lp_unsigned_rows = 0     # complete rows refused LP authority: no transaction identity
        # Funded native-stop evidence: (slot, complete pair or None, monotonic receipt).
        # Display observations without balances must never freshen an older reserve pair.
        self._exit_rung_reserves = {}
        # DISPLAY ONLY (S27-05 steps 2-3, 2026-09-27), per exact (mint, pool), read only by
        # display_marks / quote_decimals; no decision reads them:
        #   _display_pair       latest known reserves (slot, (coin, quote)) from sync rows, swap
        #                       post-balances and subscription seeds
        #   _display_change     (slot, monotonic) of the latest reserve CHANGE
        #   _display_seen       monotonic of the latest reserve observation of any kind
        #   _display_last_swap  (the last swap mark, its post-trade reserves or None); survives an
        #                       expected rollover (invalidate_prices)
        #   _rollover_ts        monotonic of the latest expected rollover
        #   _display_pair_at    ISO observed_at of the reserves held in _display_pair
        #   _display_curve_real ("solana", mint) -> (real_sol lamports, slot, observed_at) of a held
        #                       curve: its real SOL deposit, for the displayed liquidity
        self._display_pair = {}
        self._display_pair_at = {}
        self._display_curve_real = {}
        self._display_change = {}
        self._display_seen = {}
        self._display_last_swap = {}
        self._rollover_ts = None
        self._quote_decimals = {}
        self._onchain_probe = onchain_probe or probe_position_activity
        self._onchain_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sol-voldeath")
        self._onchain_futures = {}
        self._onchain_activity = {}  # bound venue -> highest slot, generation, reserve pair, valid
        # The mint-wide witness: mint -> (newest_blocktime, count, veto_until_ts), and the last
        # mint verdict per position for the vol_death audit detail. Behind self._lock.
        self._mint_probe = mint_probe or probe_mint_activity
        self._own_sigs_fn = own_sigs_fn or own_mint_signatures
        self._mint_veto = {}          # mint -> (newest_blocktime, count, newest_sig); expiry per caller
        self._mint_notes = {}         # position -> last mint outcome, for the audit detail
        self._veto_since = {}         # position -> first veto of the current episode (F1 cap)
        self._held_logged = set()     # positions whose current veto episode was logged (F10)
        # Hot TP lane (2026-08-22): called after every STORED mark, never for refused rows.
        # Must be cheap and must never raise into the stream thread.
        self._on_mark = on_mark

    def _corroborated(self, state_mark):
        """True when a fresh SWAP mark for this mint agrees with `state_mark` inside the band.

        Caller holds no lock; this takes none either, so call it OUTSIDE the lock or from a context
        that already holds one consistently. Same QUOTE ASSET required: a ratio across two quote
        units is not a ratio.
        """
        swap = self._marks.get(("solana", state_mark.mint))
        if swap is None or swap.quote_mint != state_mark.quote_mint:
            return False
        try:
            ratio = float(state_mark.quote_per_token) / float(swap.quote_per_token)
        except (TypeError, ValueError, ZeroDivisionError):
            return False
        if not math.isfinite(ratio):
            return False
        return STATE_CORROBORATION_LO <= ratio <= STATE_CORROBORATION_HI

    def record(self, row, binding, *, now_ts, observed_at):
        if (row.get("pool") == binding.pool
                and row.get("kind") in ("sol_sync", "sol_swap", "sol_seed")):
            self._observe_exit_rung(row, binding, now_ts)
        if row.get("pool") == binding.pool and row.get("kind") in LP_COMPLETE_KINDS:
            # LP AUTHORITY NEEDS TRANSACTION IDENTITY (Codex/Gamma, 062bef02e): only a pair from one
            # named transaction is a real pool state. An unsigned pair (the websocket fallback) can
            # cross two transactions in one slot, so it stays blind for the LP rail and is counted;
            # its price/exit-rung observation above is unchanged.
            if row.get("tx_sig"):
                self._observe_liquidity(row, binding, observed_at)
            else:
                with self._lock:
                    self.lp_unsigned_rows += 1
        # NOTE (position 40687, PURR copycat, 2026-08-19): an exotic-quote pool CAN lie —
        # the owner washed the on-chain ratio ~3,400x and this lane printed a 4,560x wrong
        # USD mark. Do NOT fix that with a quote-mint allowlist here: the 2026-08-14
        # PEECOIN/GLDx design requires exotic quotes with a real USD market to price
        # (tests/test_memecoin_dynamic_quote_binding.py). The exit-side spike guard plus
        # its pair-identity alarm own the wash-ratio case.
        if row.get("kind") == "sol_sync":
            return self._record_state(row, binding, now_ts=now_ts, observed_at=observed_at)
        if row.get("kind") == "sol_curve_state":
            return self._record_curve(row, binding, now_ts=now_ts, observed_at=observed_at)
        if row.get("kind") == "sol_seed":
            return self._record_seed(row, binding, now_ts=now_ts, observed_at=observed_at)
        if row.get("kind") != "sol_swap" or row.get("pool") != binding.pool:
            return False
        try:
            amount0 = int(row.get("amount0"))
            amount1 = int(row.get("amount1"))
        except (TypeError, ValueError):
            return False
        decimals0 = row.get("decimals0")
        decimals1 = row.get("decimals1")
        if (
            not isinstance(decimals0, int)
            or isinstance(decimals0, bool)
            or not isinstance(decimals1, int)
            or isinstance(decimals1, bool)
        ):
            return False
        if not 0 <= decimals0 <= 18 or not 0 <= decimals1 <= 18:
            return False
        if amount0 == 0 or amount1 == 0 or (amount0 > 0) == (amount1 > 0):
            return False
        quote_per_token = (abs(amount1) / (10**decimals1)) / (
            abs(amount0) / (10**decimals0)
        )
        if not math.isfinite(quote_per_token) or quote_per_token <= 0:
            return False
        slot = self._movement_slot(row, binding, None, force=True)
        if slot is None:
            return False
        mark = QuoteMark(
            mint=binding.mint,
            pool=binding.pool,
            quote_mint=binding.quote_mint,
            quote_per_token=quote_per_token,
            slot=slot,
            observed_ts=float(now_ts),
            observed_at=str(observed_at),
        )
        with self._lock:
            self._marks[("solana", binding.mint)] = mark
            self._quote_decimals[binding.quote_mint] = decimals1
            try:
                post = (int(row["balance0"]), int(row["balance1"]))
            except (KeyError, TypeError, ValueError):
                post = None
            self._note_onchain_activity(binding, slot, post, force=True)
            self._display_last_swap[(binding.mint, binding.pool)] = (mark, post)
            self._display_observe(binding.mint, binding.pool, slot, post, now_ts, observed_at)
        self._notify(mark)
        return True

    def _note_onchain_activity(self, binding, slot, pair, *, force=False):
        """Caller holds the cache lock; identical reserve refreshes cannot invalidate a proof."""
        venues = self._onchain_activity.setdefault(binding.mint, {})
        key = (binding.pool, binding.quote_mint)
        prior = venues.get(key)
        if (not force and pair is not None and prior is not None and prior[3]
                and pair == prior[2]):
            return
        valid = isinstance(slot, int) and not isinstance(slot, bool) and slot > 0
        venues[key] = (
            max(slot if valid else 0, prior[0] if prior else 0),
            prior[1] + 1 if prior else 1, pair, valid)

    def _movement_slot(self, row, binding, pair, *, force=False):
        """Recognized movement with an unknown slot cannot leave an older still proof usable."""
        slot = row.get("block_no")
        if isinstance(slot, str) and slot.isdecimal():
            slot = int(slot)
        if isinstance(slot, int) and not isinstance(slot, bool) and slot > 0:
            return slot
        with self._lock:
            self._note_onchain_activity(binding, None, pair, force=force)
        return None

    def _observe_exit_rung(self, row, binding, now_ts):
        """Keep complete reserves separate from display freshness. Unknown movement invalidates
        the slot: a later frame from that same slot cannot prove transaction order, so recovery
        requires a strictly higher complete slot. Older frames never refresh accepted evidence."""
        slot = row.get("block_no")
        if isinstance(slot, str) and slot.isdecimal():
            slot = int(slot)
        valid_slot = type(slot) is int and slot > 0
        pair = None
        try:
            if not valid_slot or any(type(row.get(d)) is not int or not 0 <= row[d] <= 18
                                     for d in ("decimals0", "decimals1")):
                raise ValueError("unreadable reserve metadata")
            if row["kind"] == "sol_swap":
                a, b = int(row["amount0"]), int(row["amount1"])
                if not a or not b or (a > 0) == (b > 0):
                    raise ValueError("unreadable swap")
                candidate = int(row["balance0"]), int(row["balance1"])
            else:
                candidate = int(row["amount0"]), int(row["amount1"])
            if min(candidate) > 0:
                pair = candidate
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
        key = (binding.mint, binding.pool)
        with self._lock:
            previous = self._exit_rung_reserves.get(key)
            slot = slot if valid_slot else previous[0] if previous else 0
            if previous is not None and slot < previous[0]:
                return
            if previous is not None and slot == previous[0]:
                if pair != previous[1]:
                    self._exit_rung_reserves[key] = (slot, None, previous[2])
                return                  # a duplicate cannot refresh age; conflicting order is unknown
            self._exit_rung_reserves[key] = (slot, pair, float(now_ts))

    # --- display-only reserve bookkeeping (S27-05; never read by a decision) -------------------
    DISPLAY_LAST_SWAP_CAP = 256   # ponytail: oldest-evicted bound; a process restart also clears

    def _display_observe(self, mint, pool, slot, pair, now_ts, observed_at=None):
        """Caller holds self._lock. Record one reserve observation for the exact (mint, pool): a
        pair different from the last known one is a CHANGE at `slot`. A pair of None (a swap row
        without post-balances) is an observation with no comparable reserves. Another pool seen
        for the same mint drops that mint's other carried swaps (a rebind or graduation)."""
        key = (mint, pool)
        for other in [k for k in self._display_last_swap if k[0] == mint and k[1] != pool]:
            self._display_last_swap.pop(other, None)
        self._display_seen[key] = float(now_ts)
        if pair is None:
            return
        prev = self._display_pair.get(key)
        if prev is not None and prev[1] != pair:
            self._display_change[key] = (int(slot), float(now_ts))
        if prev is None or int(slot) >= prev[0]:
            self._display_pair[key] = (int(slot), pair)
            self._display_pair_at[key] = observed_at
        if len(self._display_last_swap) > self.DISPLAY_LAST_SWAP_CAP:
            oldest = min(self._display_last_swap, key=lambda k: self._display_last_swap[k][0].observed_ts)
            self._display_last_swap.pop(oldest, None)

    def _record_seed(self, row, binding, *, now_ts, observed_at=None):
        """A subscription seed's reserves: display bookkeeping ONLY. Not a state mark, not a
        reserve observation for reserve_witness (the vol_death silence rail), not a price."""
        if row.get("pool") != binding.pool:
            return False
        try:
            pair = (int(row["amount0"]), int(row["amount1"]))
            slot = int(row["block_no"])
        except (KeyError, TypeError, ValueError):
            return False
        decimals1 = row.get("decimals1")
        with self._lock:
            if isinstance(decimals1, int) and not isinstance(decimals1, bool) and 0 <= decimals1 <= 18:
                self._quote_decimals.setdefault(binding.quote_mint, decimals1)
            self._display_observe(binding.mint, binding.pool, slot, pair, now_ts, observed_at)
        return True

    def _record_curve(self, row, binding, *, now_ts, observed_at):
        """Price a pump curve from its virtual reserves: SOL per whole token, quoted in WSOL (the
        contract agreed with the rail wake). A completed curve prices nothing and drops its mark;
        the AMM binding takes the coin over."""
        key = ("solana", binding.mint)
        if row.get("pool") != binding.pool:
            return False
        if row.get("complete"):
            slot = self._movement_slot(row, binding, None, force=True)
            with self._lock:
                if slot is not None:
                    self._note_onchain_activity(binding, slot, None, force=True)
                self._curve_marks.pop(key, None)
                self._display_curve_real.pop(key, None)
            return False
        try:
            vsol = int(row["virtual_sol"]) / 1e9
            vtok = int(row["virtual_token"]) / 10**PUMP_TOKEN_DECIMALS
            quote_per_token = vsol / vtok
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return False
        if not math.isfinite(quote_per_token) or quote_per_token <= 0:
            return False
        pair = (int(row["virtual_token"]), int(row["virtual_sol"]))
        slot = self._movement_slot(row, binding, pair)
        if slot is None:
            return False
        mark = QuoteMark(mint=binding.mint, pool=binding.pool, quote_mint=WSOL,
                         quote_per_token=quote_per_token, slot=slot, observed_ts=float(now_ts),
                         observed_at=str(observed_at), source=CURVE_SOURCE)
        real_sol = row.get("real_sol")
        with self._lock:
            self._note_onchain_activity(binding, slot, pair)
            self._curve_marks[key] = mark
            if isinstance(real_sol, int) and not isinstance(real_sol, bool) and real_sol >= 0:
                self._display_curve_real[key] = (real_sol, slot, str(observed_at))
            else:
                self._display_curve_real.pop(key, None)
        self._notify(mark)
        return True

    def _notify_state(self, mark):
        """Wake the hot TP lane for a STATE mark only when a swap corroborates it.

        The wake is a level reader: it starts a quote pass against the mark's price. An
        uncorroborated concentrated-pool mark must not reach it.
        """
        with self._lock:
            ok = self._corroborated(mark)
        if ok:
            self._notify(mark)

    def _notify(self, mark):
        if self._on_mark is None:
            return
        try:
            self._on_mark(mark)
        except Exception:  # the stream thread must outlive any hook fault
            pass

    def _record_state(self, row, binding, *, now_ts, observed_at):
        """Price the pool's RESERVES: spot = quote_reserve/coin_reserve in human units. amount0/1
        are absolute balances in a sync row, never deltas — a negative or zero side is a delta
        smuggled in or an empty pool, and both refuse."""
        if row.get("pool") != binding.pool:
            return False
        try:
            coin = int(row.get("amount0"))
            quote = int(row.get("amount1"))
        except (TypeError, ValueError):
            return False
        decimals0, decimals1 = row.get("decimals0"), row.get("decimals1")
        for d in (decimals0, decimals1):
            if not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 18:
                return False
        if coin <= 0 or quote <= 0:
            return False
        quote_per_token = (quote / (10**decimals1)) / (coin / (10**decimals0))
        if not math.isfinite(quote_per_token) or quote_per_token <= 0:
            return False
        slot = self._movement_slot(row, binding, (coin, quote))
        if slot is None:
            return False
        mark = QuoteMark(
            mint=binding.mint, pool=binding.pool, quote_mint=binding.quote_mint,
            quote_per_token=quote_per_token, slot=slot, observed_ts=float(now_ts),
            observed_at=str(observed_at), source=STATE_SOURCE,
        )
        with self._lock:
            self._note_onchain_activity(binding, slot, (coin, quote))
            self._state_marks[("solana", binding.mint)] = mark
            self._quote_decimals[binding.quote_mint] = decimals1
            self._display_observe(binding.mint, binding.pool, slot, (coin, quote), now_ts, observed_at)
            try:
                stamp = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
                if stamp.tzinfo is not None:
                    key = (binding.mint, binding.pool)
                    previous = self._reserve_last.get(key)
                    if previous is None or stamp > previous[1]:
                        pair = (coin, quote)
                        if previous is not None and pair != previous[0]:
                            self._reserve_changed[key] = stamp
                        self._reserve_last[key] = (pair, stamp)
                        self._reserve_times.setdefault(key, deque(maxlen=512)).append(stamp)
            except (TypeError, ValueError, OverflowError):
                pass
        self._notify_state(mark)
        return True

    def set_lp_bindings(self, bindings):
        """The stream's CURRENT bindings (epoch start and every mid-epoch merge). A mint whose bound
        pool changed loses the old pool's state and peak at once, so a gap after a rebind can never
        apply the old pool's evidence to the new binding (Codex review). Never raises."""
        try:
            bound, pools = {}, {}
            for b in dict(bindings or {}).values():
                pools.setdefault(b.mint, set()).add(b.pool)
                bound[b.mint] = (b.pool, getattr(b, "vault_owner", None), getattr(b, "venue_program", None))
            for mint, seen in pools.items():
                if len(seen) > 1:
                    bound.pop(mint, None)        # two pools for one mint: no authority, blind
        except Exception:
            return
        with self._lock:
            for key in [k for k in self._lp if bound.get(k[0], (None,))[0] != k[1]]:
                del self._lp[key]
            self._lp_bound = bound

    def _observe_liquidity(self, row, binding, observed_at):
        """L = isqrt(coin * quote) from a complete row's post balances. Integer math, no float error.
        A complete pair with a ZERO side is a measured drain (L = 0), not missing data. Never raises
        into the stream thread; a malformed or negative row is simply not evidence."""
        try:
            coin, quote = int(row["balance0"]), int(row["balance1"])
            slot = int(row.get("block_no"))
        except (KeyError, TypeError, ValueError):
            return
        if coin < 0 or quote < 0:
            return
        liquidity = math.isqrt(coin * quote)
        key = (binding.mint, binding.pool)
        with self._lock:
            bound = self._lp_bound.get(binding.mint)
            if bound is None or bound[0] != binding.pool:
                return                                    # not the mint's current bound pool
            entry = self._lp.get(key)
            if entry is not None and slot < entry["slot"]:
                return                                    # an older slot never rewinds the state
            if entry is None or liquidity > entry["max_l"]:
                entry = dict(entry or {}, max_l=liquidity, max_slot=slot)
            entry = dict(entry, l=liquidity, slot=slot, at=str(observed_at))
            self._lp[key] = entry

    def lp_removal(self, mint):
        """The bound pool's liquidity drop since bind, or a blind state. Never a guess:
        {"state": "measured", "fraction", "l_before", "l_after", "slot", "max_slot", "pool", "at"}
        or {"state": "blind", "why": ...}. A lock and a dict read; no I/O."""
        with self._lock:
            bound = self._lp_bound.get(mint)
            if bound is None:
                return {"state": "blind", "why": "unbound"}
            pool, owner, program = bound
            if not lp_venue_eligible(owner, program):
                return {"state": "blind", "why": "venue_not_proven_constant_product", "pool": pool}
            entry = self._lp.get((mint, pool))
            if entry is None:
                return {"state": "blind", "why": "no_complete_row_since_bind", "pool": pool}
            entry = dict(entry)
        if entry["max_l"] <= 0:
            return {"state": "blind", "why": "empty_pool", "pool": pool}
        return {"state": "measured", "fraction": 1.0 - entry["l"] / entry["max_l"],
                "l_before": entry["max_l"], "l_after": entry["l"], "slot": entry["slot"],
                "max_slot": entry["max_slot"], "pool": pool, "at": entry["at"]}

    def reserve_witness(self, *, mint, since_iso, window_start_iso, now_iso,
                        health_window_s=60.0):
        """Read raw vault movement for the funded silence rail; never read reserve-ratio prices."""
        try:
            since, start, now = (
                datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                for value in (since_iso, window_start_iso, now_iso))
            health = float(health_window_s)
            if (any(value.tzinfo is None for value in (since, start, now))
                    or not since <= start <= now or not math.isfinite(health) or health <= 0):
                return "reader_dark"
        except (TypeError, ValueError, OverflowError):
            return "reader_dark"
        with self._lock:
            keys = [key for key, times in self._reserve_times.items()
                    if key[0] == mint and any(since <= ts <= now for ts in times)]
            if len(keys) != 1:  # zero coverage or ambiguous pool identity
                return "uncovered_transient"
            key = keys[0]
            changed = self._reserve_changed.get(key)
            if changed is not None and start <= changed <= now:
                return "moving"
            if not any(since <= ts <= start for ts in self._reserve_times[key]):
                return "uncovered_transient"  # no pre-silence baseline for this hold
            alive = any(now - observed <= timedelta(seconds=health)
                        for _pair, observed in self._reserve_last.values()
                        if observed <= now)
            return "still" if alive else "reader_dark"

    def onchain_witness(self, *, position_id, mint, window_start_iso, now_iso,
                        current_pool_key=None, exit_s=None, veto_max_s=None):
        """Read a fresh independent vault-history probe without blocking the exit sweep. With
        `exit_s`, a still vault is followed by the mint-wide read (mint_moving veto)."""
        try:
            start = datetime.fromisoformat(str(window_start_iso).replace("Z", "+00:00"))
            now = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
            if start.tzinfo is None or now.tzinfo is None or start > now:
                return "unavailable"
        except (TypeError, ValueError):
            return "unavailable"
        if position_id is None or not mint:
            return "unavailable"
        start_ts, now_ts = start.timestamp(), now.timestamp()

        def run_probe():
            try:
                verdict = self._onchain_probe(position_id, mint, start_ts, now_ts,
                                              current_pool_key, minimum_slot)
            except Exception:
                logger.exception("Solana vault activity probe failed mint=%s", mint)
                verdict = "unavailable"
            if verdict == "still" and exit_s:
                verdict = self._mint_check(position_id, mint, now_ts, exit_s, veto_max_s)
            return verdict, time.monotonic()

        with self._lock:
            key = (position_id, mint)
            venues = self._onchain_activity.get(mint, {})
            activity = tuple(sorted((venue, value[0], value[1])
                for venue, value in venues.items()))
            if any(not value[3] for value in venues.values()):
                return "unavailable"
            minimum_slot = max((value[1] for value in activity), default=0)
            tick = time.monotonic()
            prior = self._onchain_futures.get(key)
            if (prior is not None and prior[2] == current_pool_key
                    and prior[3] == activity and prior[1].done()):
                try:
                    verdict, finished = prior[1].result()
                except Exception:
                    logger.exception("Solana vault activity probe failed mint=%s", mint)
                    verdict, finished = "unavailable", tick
                if tick - finished <= 3.0 and prior[0] <= start_ts:
                    typed = str(verdict).startswith("unavailable:")
                    return (verdict if verdict in ("moving", "still", "moving:mint") or typed
                            else "unavailable")
            if prior is None or prior[2] != current_pool_key or prior[1].done():
                self._onchain_futures[key] = (
                    start_ts, self._onchain_pool.submit(run_probe), current_pool_key, activity)
                if len(self._onchain_futures) > 64:
                    self._onchain_futures = {k: v for k, v in self._onchain_futures.items()
                                             if not v[1].done()}
                return "pending"
            return "pending"

    def _mint_check(self, position_id, mint, now_ts, exit_s, veto_max_s=None):
        """"moving:mint" or "still" after a still vault. A cached veto stands until its newest
        transaction leaves THIS caller's window (F7). A veto episode longer than `veto_max_s` past
        exit_s gives up and fires (F1): a mint trading every 27 s would otherwise hold forever. Any
        failure, including an exception inside the probe, is unknown and returns "still"."""
        stamp = datetime.fromtimestamp(float(now_ts), tz=timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            veto = self._mint_veto.get(mint)
            cached = veto is not None and now_ts < veto[0] + float(exit_s) - _MINT_LO_MARGIN_S
        if cached:
            state, detail, count, sig = "moving", veto[0], veto[1], veto[2]
        else:
            try:
                own = self._own_sigs_fn(mint)
            except Exception as exc:
                return self._mint_unknown(position_id, mint, stamp, "own_sigs:%s" % type(exc).__name__)
            try:
                state, detail, count, sig = self._mint_probe(mint, now_ts=now_ts, exit_s=exit_s,
                                                             own_sigs=set(own or ()))
            except Exception as exc:
                state, detail, count, sig = "unknown", "raised:%s" % type(exc).__name__, 0, None
        if state != "moving":
            with self._lock:
                self._veto_since.pop(position_id, None)
                self._held_logged.discard(position_id)
            if state == "still":
                with self._lock:
                    self._mint_notes[position_id] = "mint=still"
                return "still"
            return self._mint_unknown(position_id, mint, stamp, detail)
        newest = datetime.fromtimestamp(float(detail), tz=timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            first = self._veto_since.setdefault(position_id, float(now_ts))
            capped = veto_max_s is not None and float(now_ts) - first >= float(veto_max_s)
            if not cached:
                self._mint_veto[mint] = (float(detail), count, sig)
        if capped:
            with self._lock:
                self._veto_since.pop(position_id, None)
                self._held_logged.discard(position_id)
            return self._mint_unknown(position_id, mint, stamp, "veto_cap_%.0fs" % float(veto_max_s))
        with self._lock:
            self._mint_notes[position_id] = "mint=moving%s newest=%s count=%s sig=%s" % (
                ":cached" if cached else "", newest, count, sig)
        if not cached:
            # One stamped line per veto READ with the newest signature, so a later getTransaction
            # pass can grade spurious vetoes (a transfer, an ATA create) from the log.
            logger.warning("%s vol_death mint_moving: pos=%s mint=%s newest_tx=%s newest_sig=%s "
                           "count=%d window_s=%.0f", stamp, position_id, mint, newest, sig, count,
                           float(exit_s) - _MINT_LO_MARGIN_S)
        return "moving:mint"

    def _mint_unknown(self, position_id, mint, stamp, cause):
        with self._lock:
            self._mint_notes[position_id] = "mint=unknown:%s" % cause
        logger.warning("%s vol_death mint witness unknown: pos=%s mint=%s cause=%s (fires as before)",
                       stamp, position_id, mint, cause)
        return "still"

    def held_log_due(self, position_id):
        """True once per veto episode: the runner's HELD warning for mint_moving (F10)."""
        with self._lock:
            if position_id in self._held_logged:
                return False
            self._held_logged.add(position_id)
            return True

    def mint_note(self, position_id):
        """The last mint-witness outcome for the position's vol_death audit detail, or ''."""
        with self._lock:
            return self._mint_notes.get(position_id, "")

    def quote_mints(self):
        with self._lock:
            return ({mark.quote_mint for mark in self._marks.values()}
                    | {mark.quote_mint for mark in self._state_marks.values()}
                    | {mark.quote_mint for mark in self._curve_marks.values()})

    def bindings_by_mint(self):
        """mint -> quote_mint for every mint this cache has marked (hot TP lane needs the
        quote unit to convert a stream mark to USD without a DB read)."""
        with self._lock:
            out = {mark.mint: mark.quote_mint for mark in self._curve_marks.values()}
            out.update({mark.mint: mark.quote_mint for mark in self._state_marks.values()})
            out.update({mark.mint: mark.quote_mint for mark in self._marks.values()})
            return out

    def fresh_price_map(self, quote_usd, *, now_ts, max_age_s):
        """The lane's price for each mint. ONE entry per mint, because `write_marks` stores one
        shared row per (chain, mint, sweep) and the funded exit sweep reads this map directly.

        The SWAP mark is the default: it is the trade's own execution price. A STATE mark replaces
        it only when it is NEWER (the pool moved after the last trade, which is the whole value of
        the state lane) AND a fresh swap mark corroborates it inside the band. Otherwise the swap
        mark stands, so a refused STATE mark costs the mint nothing.
        """
        with self._lock:
            marks = dict(self._marks)
            for key, state in self._state_marks.items():
                swap = marks.get(key)
                if swap is None:
                    continue                      # no corroborating swap: never admit a bare state
                if float(now_ts) - swap.observed_ts > float(max_age_s):
                    continue                      # the corroborating swap is stale
                if state.observed_ts <= swap.observed_ts:
                    continue                      # the swap is newer; it already says it better
                if self._corroborated(state):
                    marks[key] = state
            # A curve mark does not age out: an acknowledged accountSubscribe pushes every change,
            # so a quiet curve's last state is still its state. It lives until the epoch
            # invalidates it (rollover or disconnect), then the seed read restores it.
            curves = dict(self._curve_marks)
        out = {}
        for key, mark in list(marks.items()) + list(curves.items()):
            if mark.source != CURVE_SOURCE and float(now_ts) - mark.observed_ts > float(max_age_s):
                continue
            try:
                reference = float(quote_usd.get(mark.quote_mint))
                price = mark.quote_per_token * reference
            except (TypeError, ValueError):
                continue
            if not math.isfinite(price) or price <= 0:
                continue
            out[key] = {
                "price": price,
                "liquidity": None,
                "mcap": None,
                "source": getattr(mark, "source", SOURCE),
                "quality": QUALITY,
                "observed_at": mark.observed_at,
            }
        return out

    def display_marks(self, quote_usd, *, now_ts, max_age_s, carry=False, change_grace_s=1.0,
                      rollover_grace_s=2.0):
        """DISPLAY ONLY (S27-05, 2026-09-27): the swap and curve marks with their identity, for the
        live-marks publisher. Never read by an exit decision; `fresh_price_map` stays the decision
        map. STATE marks are excluded: state and swap sit 4-10% apart and never share one display
        series. A mark whose quote asset has no USD is absent, never zero. Same precedence as
        `fresh_price_map`: a curve mark wins over a swap mark for the same mint.

        CARRY (operator 2026-09-27: "go on quiet pools", then "fix the reconnect"). A swap mark older
        than max_age_s stays, flagged `carried`, while its exact (mint, pool) still shows the
        reserves the swap left (its post-trade balances). BY RESERVE IDENTITY, NOT SLOT ORDER (DR-06,
        docs/audits/2026-09-27-post-review-delta.md): a later change in the SAME slot withdraws it
        too. The swap's own first vault frame arrives before its swap row, and the row sets the
        pair back to the post-trade reserves, so its own frames never withdraw it. Any other pair
        withdraws it after `change_grace_s`, the time a trade's two vault frames take to arrive. A
        swap without post-trade reserves is never carried (unknown is not unchanged). Across an
        expected rollover the mark must also be confirmed: a reserve observation of that pool AFTER
        the rollover (the subscription seed, a sync or a swap), or `rollover_grace_s` while the
        seed is pending.

        RESERVES (operator 2026-09-27 via Alpha: replace DexScreener liquidity where something better
        exists). Each mark carries the real quote-side deposit of its own market in quote units:
        `reserve_quote` (a curve's real SOL, or an AMM pool's quote vault), with `reserve_slot` and
        `reserve_at`. None when unknown, including an AMM pool not yet observed after a rollover.
        The publisher decides whether a pool's vault is its depth (the pool program); this only
        reports what the stream holds."""
        with self._lock:
            marks = dict(self._marks)
            curves = dict(self._curve_marks)
            last_swaps = dict(self._display_last_swap)
            pairs = dict(self._display_pair)
            pairs_at = dict(self._display_pair_at)
            curve_real = dict(self._display_curve_real)
            quote_dec = dict(self._quote_decimals)
            changes = dict(self._display_change)
            seen = dict(self._display_seen)
            rollover = self._rollover_ts
        candidates = list(marks.items()) + list(curves.items())
        if carry:
            live = {k[1] for k in marks} | {k[1] for k in curves}
            for (mint, _pool), (mark, _post) in last_swaps.items():
                if mint not in live:
                    candidates.append((("solana", mint), mark))
        out = {}
        for key, mark in candidates:
            carried = False
            if mark.source != CURVE_SOURCE and float(now_ts) - mark.observed_ts > float(max_age_s):
                if not carry:
                    continue
                pool_key = (mark.mint, mark.pool)
                entry = last_swaps.get(pool_key)
                post = entry[1] if entry is not None and entry[0] is mark else None
                pair = pairs.get(pool_key)
                if post is None or pair is None:
                    continue
                if pair[1] != post:
                    change = changes.get(pool_key)
                    if change is None or float(now_ts) - change[1] >= float(change_grace_s):
                        continue
                if rollover is not None and mark.observed_ts < rollover:
                    confirmed = seen.get(pool_key, float("-inf")) >= rollover
                    if not confirmed and float(now_ts) - rollover > float(rollover_grace_s):
                        continue
                carried = True
            try:
                reference = float((quote_usd or {}).get(mark.quote_mint))
                price = mark.quote_per_token * reference
            except (TypeError, ValueError):
                continue
            if not math.isfinite(price) or price <= 0:
                continue
            reserve = None
            if mark.source == CURVE_SOURCE:
                real = curve_real.get(key)
                if real is not None:
                    reserve = (real[0] / 1e9, real[1], real[2])       # lamports -> SOL
            else:
                pool_key = (mark.mint, mark.pool)
                held, dec = pairs.get(pool_key), quote_dec.get(mark.quote_mint)
                if (held is not None and dec is not None
                        and (rollover is None or seen.get(pool_key, float("-inf")) >= rollover)):
                    reserve = (held[1][1] / 10**dec, held[0], pairs_at.get(pool_key))
            out[key] = {"price_usd": price, "source": mark.source, "pool": mark.pool,
                        "quote_mint": mark.quote_mint, "quote_usd": reference,
                        "slot": int(mark.slot), "observed_at": mark.observed_at,
                        "carried": carried,
                        "venue": "curve" if mark.source == CURVE_SOURCE else "amm",
                        "reserve_quote": reserve[0] if reserve else None,
                        "reserve_slot": reserve[1] if reserve else None,
                        "reserve_at": reserve[2] if reserve else None}
        return out

    def exit_rung_pair(self, mint, *, now_ts, max_age_s):
        """The bound pool's raw reserves for the exotic exit rung (2026-10-17, Alpha; memecoin_exotic_
        exit_rung), or None. ONE pool for the mint, an AMM (not curve) mark naming its quote mint,
        observed within `max_age_s` and after any expected rollover, and the quote decimals known.
        {pool, quote_mint, quote_decimals, coin_atoms, quote_atoms, slot, seen_age_s}. No I/O."""
        with self._lock:
            if ("solana", mint) in self._curve_marks:
                return None
            keys = [key for key in self._exit_rung_reserves if key[0] == mint]
            if len(keys) != 1:
                return None
            key = keys[0]
            slot, pair, seen = self._exit_rung_reserves[key]
            age = float(now_ts) - seen
            if pair is None or not math.isfinite(age) or not 0 <= age <= float(max_age_s):
                return None
            if self._rollover_ts is not None and seen < self._rollover_ts:
                return None
            mark = (self._marks.get(("solana", mint)) or self._state_marks.get(("solana", mint)))
            if mark is None or mark.pool != key[1]:
                return None
            decimals = self._quote_decimals.get(mark.quote_mint)
        try:
            coin, quote = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            return None
        if coin <= 0 or quote <= 0 or not isinstance(decimals, int):
            return None
        return {"pool": key[1], "quote_mint": mark.quote_mint, "quote_decimals": decimals,
                "coin_atoms": coin, "quote_atoms": quote, "slot": int(slot),
                "seen_age_s": round(age, 3)}

    def quote_decimals(self):
        """{quote_mint: decimals} for every quote asset the stream has reported. Display only."""
        with self._lock:
            return dict(self._quote_decimals)

    def invalidate(self):
        with self._lock:
            self._display_pair.clear()
            self._display_pair_at.clear()
            self._display_curve_real.clear()
            self._display_change.clear()
            self._display_seen.clear()
            self._display_last_swap.clear()
            self._rollover_ts = None
            self._marks.clear()
            self._state_marks.clear()
            self._curve_marks.clear()
            self._reserve_times.clear()
            self._reserve_last.clear()
            self._reserve_changed.clear()
            self._lp.clear()                 # blind after a coverage reset, never a breach
            self._lp_bound.clear()
            self._exit_rung_reserves.clear()
            self._onchain_futures.clear()
            self._onchain_activity.clear()

    def invalidate_prices(self, now_ts=None):
        """Expire executable marks at an expected epoch rollover; retain raw reserve evidence. The
        display-only reserve bookkeeping and last swaps are retained too, stamped with the
        rollover time (display_marks then requires a post-rollover observation)."""
        with self._lock:
            self._rollover_ts = float(time.monotonic() if now_ts is None else now_ts)
            self._exit_rung_reserves = {
                key: (slot, None, seen) for key, (slot, _pair, seen) in self._exit_rung_reserves.items()}
            self._marks.clear()
            self._state_marks.clear()
            self._curve_marks.clear()
            self._display_curve_real.clear()     # restored with the curve mark by the seed read


BIND_MAX_ATTEMPTS = 5


def bind_open_positions(tc, mconn, positions, get_tx_fn, *, attempts,
                        resolve_fn=None, max_attempts=BIND_MAX_ATTEMPTS):
    """Bind an open Solana position to its pool now, off our own entry fill transaction.

    The tape worker resolves the same binding, with the same function and the same evidence
    (`memecoin_fills.tx_sig`, written at open with 0.0 s lag), but only once per
    MEMECOIN_SOL_TAPE_PERIOD_S. At the shipped 300 s that measured a median 276 s bind lag
    (range 92-811 s) on 2026-08-12, and 6 of the last 17 funded Solana positions closed before
    they ever bound. An unbound position gets no swap mark, so its rails decide on the
    DexScreener mid, whose value changes every 30.5 s. The positions that need a fast exit are
    exactly the short ones that never bound.

    Spends at most one getTransaction per position per call, and at most `max_attempts` for the
    position: a just-submitted signature can read as absent for a moment, but a fill that never
    decodes must not re-spend an RPC every loop. The tape pass still retries what is given up here.

    Never raises. A failed bind leaves the DexScreener fallback exactly as it was.
    """
    if resolve_fn is None:
        from sniper.services.memecoin_solana_tape import resolve_position_pool as resolve_fn
    live = {pos.get("id") for pos in positions}
    for stale in [pid for pid in attempts if pid not in live]:
        attempts.pop(stale, None)
    bound = 0
    for pos in positions:
        if str(pos.get("network_id") or "solana") != "solana":
            continue
        pid = pos.get("id")
        if pid is None or attempts.get(pid, 0) >= max_attempts:
            continue
        try:
            vault = resolve_fn(tc, mconn, pos, get_tx_fn)
        except Exception:
            vault = None
        if vault:
            bound += 1
        # A binding is durable, so a bound position stops being asked; an unbound one gets one
        # more look until the cap.
        attempts[pid] = max_attempts if vault else attempts.get(pid, 0) + 1
    return bound


def build_price_book(positions, sol_conn):
    """Build a vault book from durable pool bindings for open Solana positions."""
    vaults = {}
    bindings = {}
    # The vault owner feeds only the LP rail's venue check. A tape predating the column reads NULL
    # (blind on that rail), never an error that would cost the whole price book.
    _cols = {r[1] for r in sol_conn.execute("PRAGMA table_info(sol_pool_vaults)")}
    owner = "v.program" if "program" in _cols else "NULL"
    venue = "v.venue_program" if "venue_program" in _cols else "NULL"
    for pos in positions:
        if str(pos.get("network_id") or "solana") != "solana":
            continue
        row = sol_conn.execute(
            "SELECT p.pool,v.coin_vault,v.quote_vault,v.quote_mint,p.venue_kind," + owner + ","
            + venue + " "
            "FROM sol_position_pools p JOIN sol_pool_vaults v ON v.pool=p.pool "
            "WHERE p.position_id=? AND p.mint=?",
            (pos.get("id"), str(pos.get("mint") or "")),
        ).fetchone()
        if not row:
            continue
        if row[4] == "pump_curve":
            # A native-SOL Pump curve is a program account, not an SPL token vault: subscribe the
            # CURVE (the binding's quote_vault is its PDA) base64 and price its reserves.
            curve = str(row[2])
            binding = PoolBinding(mint=str(pos["mint"]), pool=curve, coin_vault=str(row[1]),
                                  quote_vault=curve, quote_mint=WSOL)
            vaults[curve] = CurveTape(curve, mint=curve)
            bindings[curve] = binding
            continue
        binding = PoolBinding(
            mint=str(pos["mint"]),
            pool=str(row[0]),
            coin_vault=str(row[1]),
            quote_vault=str(row[2]),
            quote_mint=str(row[3]),
            vault_owner=(str(row[5]) if row[5] else None),
            venue_program=(str(row[6]) if row[6] else None),
        )
        tape = VaultTape(
            binding.coin_vault, binding.quote_vault, mint=binding.pool
        )
        vaults[binding.coin_vault] = tape
        vaults[binding.quote_vault] = tape
        bindings[binding.pool] = binding
    return vaults, bindings


def epoch_summary(vaults, bindings):
    """The one line the runner prints per stream epoch (the logger.info line below never reached
    memecoin_runner.log): subscribed accounts, how many are pump curves, and bound pools."""
    curves = sum(1 for tape in vaults.values() if hasattr(tape, "on_raw"))
    return "sol price stream epoch: %d vaults (%d curves), %d pools" % (
        len(vaults), curves, len(bindings))


def curve_seed_fn(read_rpc_url, *, rpc_call=None, expected_mint_fn=None):
    """`seed_fn` for the vault stream: ONE getAccountInfo (base64, confirmed) per acknowledged
    curve subscription, returning (slot, raw bytes) or None. The stream runs it on a thread after
    the ack, so the seed can only be newer than the subscription it completes; a failed or
    malformed read is None, and the curve keeps no price until its next push."""
    if rpc_call is None:
        from sniper.services.memecoin_solana_blocktime import _rpc_call

        def rpc_call(method, params):
            return _rpc_call(read_rpc_url, method, params, timeout=2)

    def seed(account):
        import base64
        res = rpc_call("getAccountInfo",
                       [str(account), {"encoding": "base64", "commitment": "confirmed"}])
        if not isinstance(res, dict):
            return None
        slot = (res.get("context") or {}).get("slot")
        value = res.get("value")
        data = value.get("data") if isinstance(value, dict) else None
        if expected_mint_fn is not None:
            from sniper.execution.pump_native_exit import PUMP_PROGRAM, _pda, _pk
            try:
                mint = expected_mint_fn(str(account))
                if (not mint or value.get('owner') != PUMP_PROGRAM
                        or str(_pda([b'bonding-curve', bytes(_pk(mint))], PUMP_PROGRAM)) != str(account)):
                    return None
            except (TypeError, ValueError, AttributeError):
                return None
        if (not isinstance(slot, int) or isinstance(slot, bool) or not isinstance(data, list)
                or len(data) != 2 or data[1] != "base64"):
            return None
        try:
            return slot, base64.b64decode(data[0], validate=True)
        except (TypeError, ValueError):
            return None

    return seed


def vault_seed_fn(read_rpc_url, *, rpc_call=None, expected_mint_fn=None):
    """`vault_seed_fn` for the vault stream (operator 2026-09-27: "fix the reconnect"): ONE
    getAccountInfo (jsonParsed, confirmed: the vault subscription's own commitment) per acknowledged
    vault subscription, returning (slot, raw amount, decimals) or None. The stream applies it as
    the tape's baseline only if no notification got there first. A failed or malformed read is
    None, which leaves today's behaviour exactly."""
    if rpc_call is None:
        from sniper.services.memecoin_solana_blocktime import _rpc_call

        def rpc_call(method, params):
            return _rpc_call(read_rpc_url, method, params, timeout=2)

    def seed(account):
        res = rpc_call("getAccountInfo",
                       [str(account), {"encoding": "jsonParsed", "commitment": "confirmed"}])
        if not isinstance(res, dict):
            return None
        slot = (res.get("context") or {}).get("slot")
        value = res.get("value")
        info = ((((value or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                if isinstance(value, dict) and isinstance(value.get("data"), dict) else {})
        token_amount = info.get("tokenAmount") if isinstance(info, dict) else None
        if expected_mint_fn is not None:
            expected = expected_mint_fn(str(account))
            parsed = (value.get('data') or {}).get('parsed') or {} if isinstance(value, dict) else {}
            if (not isinstance(value, dict) or not expected or value.get('owner') not in TOKEN_PROGRAMS
                    or parsed.get('type') != 'account' or info.get('mint') != expected
                    or info.get('state') != 'initialized'):
                return None
        if not isinstance(slot, int) or isinstance(slot, bool) or not isinstance(token_amount, dict):
            return None
        try:
            amount = int(token_amount.get("amount"))
        except (TypeError, ValueError):
            return None
        decimals = token_amount.get("decimals")
        if not isinstance(decimals, int) or isinstance(decimals, bool):
            decimals = None
        if expected_mint_fn is not None and (decimals is None or not 0 <= decimals <= 18):
            return None
        return (slot, amount, decimals) if amount >= 0 else None

    return seed


class AccountCoverageError(RuntimeError):
    """A price stream gap clears raw witnesses as well as nominal prices."""


def _reset_account_tape(tape):
    if hasattr(tape, 'on_raw'):
        tape._slot = None
    else:
        tape._last.clear()
        tape._slot.clear()
        tape._decimals.clear()


def account_mint(account, vaults, bindings):
    tape = vaults.get(account)
    binding = bindings.get(tape.mint) if tape is not None else None
    if binding is None:
        return None
    return binding.mint if hasattr(tape, 'on_raw') or account == binding.coin_vault else binding.quote_mint


def _account_seed_valid(got, tape):
    if not isinstance(got, (tuple, list)) or len(got) != (2 if hasattr(tape, 'on_raw') else 3):
        return False
    if type(got[0]) is not int or got[0] <= 0:
        return False
    if hasattr(tape, 'on_raw'):
        from sniper.services.memecoin_solana_ws import decode_bonding_curve
        return isinstance(got[1], bytes) and decode_bonding_curve(got[1]) is not None
    return (type(got[1]) is int and got[1] >= 0
            and type(got[2]) is int and 0 <= got[2] <= 18)


def apply_account_seed(address, got, vaults, *, decimals, ordering, generation):
    """Reconcile a confirmed bank snapshot without inventing an account write version."""
    tape = vaults.get(address)
    if tape is None or not _account_seed_valid(got, tape):
        return None
    slot = got[0]
    if type(slot) is not int or slot <= 0:
        return None
    if not hasattr(tape, 'on_raw'):
        _, amount, precision = got
        if type(amount) is not int or amount < 0:
            return None
        # Mint precision is immutable and identity-validated by the strict seed reader. It
        # remains useful even when that read's balance snapshot loses the context-slot race.
        if type(precision) is int and 0 <= precision <= 18:
            decimals[address] = precision
            tape._decimals[address] = precision
        present = address in tape._last
        if present:
            return None  # Retain VaultTape's existing live-first seed contract.
    else:
        from sniper.services.memecoin_solana_ws import decode_bonding_curve
        if not isinstance(got[1], bytes) or decode_bonding_curve(got[1]) is None:
            return None
        present = tape._slot is not None
    previous = ordering.get(address)
    if previous is not None and (slot < previous[1] or (slot == previous[1] and present)):
        return None
    ordering[address] = ('http', slot, None, generation)
    if hasattr(tape, 'on_raw'):
        return tape.on_raw(slot, got[1], seed=True)
    return tape.seed_balance(address, slot, amount, decimals.get(address))


def apply_account_row(row, vaults, bindings, *, decimals, ordering=None):
    """Validate the exact bound account before mutating the existing curve/vault tape."""
    import base58
    from sniper.execution.pump_native_exit import PUMP_PROGRAM, _pda, _pk
    from sniper.services.memecoin_solana_ws import decode_bonding_curve

    address = row.get('pubkey')
    tape = vaults.get(address)
    if tape is None:
        return None
    if ordering is not None:
        slot, version = row.get('slot'), row.get('write_version')
        if type(slot) is not int or slot <= 0 or type(version) is not int or version < 0:
            _reset_account_tape(tape)
            raise AccountCoverageError('account_write_identity')
        generation = row.get('connection_generation'), row.get('watch_generation')
        previous = ordering.get(address)
        if previous is not None:
            if slot < previous[1]:
                return None
            if slot == previous[1] and (previous[0] != 'grpc' or previous[3] != generation
                                      or version <= previous[2]):
                return None
        ordering[address] = ('grpc', slot, version, generation)
    binding = bindings.get(tape.mint)
    def refuse(reason):
        _reset_account_tape(tape)
        raise AccountCoverageError(reason)
    if binding is None:
        refuse('account_binding_missing')
    if row.get('deleted') or row.get('lamports') == 0:
        refuse('account_deleted')
    raw = row.get('data')
    if not isinstance(raw, bytes):
        refuse('account_layout')
    if hasattr(tape, 'on_raw'):
        try:
            canonical = str(_pda([b'bonding-curve', bytes(_pk(binding.mint))], PUMP_PROGRAM))
        except (TypeError, ValueError):
            refuse('curve_identity')
        if (row.get('owner') != PUMP_PROGRAM or address != canonical
                or decode_bonding_curve(raw) is None):
            refuse('curve_identity')
        return tape.on_raw(row['slot'], raw, seed=bool(row.get('baseline_only') or row.get('is_startup')))
    owner = row.get('owner')
    if owner not in TOKEN_PROGRAMS or len(raw) < 165:
        refuse('account_layout')
    if raw[108] != 1 or any(struct.unpack_from('<I', raw, offset)[0] not in (0, 1)
                             for offset in (72, 109, 129)):
        refuse('account_layout')
    if owner == 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA' and len(raw) != 165:
        refuse('account_layout')
    if len(raw) > 165:
        if raw[165] != 2:  # Token-2022 account type, never the similarly shaped mint.
            refuse('account_layout')
        offset = 166
        while offset < len(raw):
            if not any(raw[offset:]):
                break
            if offset + 4 > len(raw):
                refuse('account_layout')
            _kind, length = struct.unpack_from('<HH', raw, offset)
            offset += 4 + length
            if offset > len(raw):
                refuse('account_layout')
    if base58.b58encode(raw[:32]).decode() != account_mint(address, vaults, bindings):
        refuse('account_mint_mismatch')
    amount = struct.unpack_from('<Q', raw, 64)[0]
    precision = decimals.get(address)
    if not isinstance(precision, int) or isinstance(precision, bool) or not 0 <= precision <= 18:
        precision = None
    if row.get('baseline_only'):
        if address in tape._last:
            _reset_account_tape(tape)
        return tape.seed_balance(address, row['slot'], amount, precision)
    return tape.on_balance(address, row['slot'], amount, precision,
                           tx_sig=row.get('txn_signature'), tx_bound=True)


ACCOUNT_SEED_RETRY_S = (.5, 2.0)  # Three attempts per account/generation, off the receiver path.


class AccountPriceStream:
    """One runner-owned receiver; the existing price-worker loop owns all materialization."""
    def __init__(self, url, token, *, status=None, run_fn=None):
        from sniper.services.memecoin_shyft_accounts import AccountQueue, run_accounts
        self.url, self.token = url, token
        self.status = status if status is not None else {}
        self.status.update(endpoint=urlsplit(url).hostname, commitment='confirmed',
            connection_generation=0, watch_generation=0, requested_accounts=(),
            requested_account_count=0, acknowledged_accounts=(), connected=False,
            subscription_acknowledged=False, coverage='unknown', consumer_stale=0,
            seed_failures=0, seed_errors=0)
        self.queue, self._watched = AccountQueue(), ()
        self._ordering = {}  # Consumer-owned, bounded to the prepared active watch set.
        self._run = run_fn or run_accounts
        self._stop, self.thread, self._loop, self._task = threading.Event(), None, None, None

    def watched(self):
        return self._watched

    def start(self):
        if self.thread is not None:
            return self.thread
        async def serve():
            self._loop, self._task = asyncio.get_running_loop(), asyncio.current_task()
            try:
                from sniper.services.memecoin_grpc_wakeup import install_grpc_wakeup_handler
                install_grpc_wakeup_handler()
                await self._run(self.url, self.token, self.watched, self.queue.offer,
                                self.status, self._stop)
            finally:
                self._loop = self._task = None
        def target():
            try:
                asyncio.run(serve())
            except asyncio.CancelledError:
                pass
        self.thread = threading.Thread(target=target, name='solana-accounts', daemon=True)
        self.thread.start()
        return self.thread

    def close(self):
        self._stop.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The receiver may have finished between the snapshots.
        if self.thread is not None:
            self.thread.join(1)
        self._watched = ()

    async def stream(self, vaults, on_row, *, bindings, ws_url=None, refresh_fn=None,
                     refresh_s=2.0, seed_fn=None, vault_seed_fn=None, max_age_s=2.0):
        import inspect
        from sniper.services.memecoin_shyft_accounts import _watch

        async def emit(row):
            if row is not None:
                result = on_row(row)
                if inspect.isawaitable(result):
                    await result

        started = time.monotonic()
        # An epoch boundary may drop old deltas, but a provider gap must still invalidate the
        # previous raw witnesses. Leave the first gap at the front of a bounded local handoff.
        pending_gap = None
        for _ in range(self.queue.maxsize):
            try:
                pending = self.queue.get_nowait()
            except queue.Empty:
                break
            self.queue.task_done()
            if pending.get('kind') == 'account_coverage':
                pending_gap = pending
                break
        self._watched = _watch(vaults)
        self._ordering = {key: point for key, point in self._ordering.items() if key in vaults}
        decimals, seeds, seeded = {}, {}, set()
        seed_attempts, retry_at = {}, {}
        seed_limit = asyncio.Semaphore(2)
        async def seed_one(fn, address):
            async with seed_limit:
                return await asyncio.to_thread(fn, address)
        refresh_at = started + refresh_s
        ack_deadline, ack_identity, last_watch_gap = started + 10, None, None
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if refresh_fn is not None and now >= refresh_at:
                    fresh = refresh_fn() or {}
                    for address in set(vaults).difference(fresh):
                        vaults.pop(address, None)
                        decimals.pop(address, None)
                        seeded.discard(address)
                        seed_attempts.pop(address, None)
                        retry_at.pop(address, None)
                        self._ordering.pop(address, None)
                    for address, tape in fresh.items():
                        vaults.setdefault(address, tape)
                    self._watched = _watch(vaults)
                    refresh_at = now + refresh_s
                identity = (self._watched, self.status.get('connection_generation'),
                            self.status.get('watch_generation'),
                            tuple(self.status.get('requested_accounts', ())))
                if identity != ack_identity:
                    ack_identity, ack_deadline = identity, now + 10
                    seeded.clear()
                    seed_attempts.clear()
                    retry_at.clear()
                current_ack = (self.status.get('connected') and self.status.get('subscription_acknowledged')
                    and tuple(self.status.get('requested_accounts', ())) == self._watched)
                if current_ack:
                    for address, tape in vaults.items():
                        fn = seed_fn if hasattr(tape, 'on_raw') else vault_seed_fn
                        attempts = seed_attempts.get(address, 0)
                        if (fn is not None and address not in seeded and address not in seeds
                                and attempts <= len(ACCOUNT_SEED_RETRY_S) and now >= retry_at.get(address, 0)):
                            seed_attempts[address] = attempts + 1
                            generation = self.status.get('connection_generation'), self.status.get('watch_generation')
                            seeds[address] = generation, asyncio.create_task(seed_one(fn, address))
                for address, (generation, task) in list(seeds.items()):
                    if not task.done():
                        continue
                    seeds.pop(address)
                    try:
                        got = task.result()
                        if (not current_ack or generation != (self.status.get('connection_generation'),
                                                               self.status.get('watch_generation'))):
                            seeded.discard(address)
                            self.status['consumer_stale'] += 1
                            continue
                    except Exception:
                        self.status['seed_errors'] += 1
                        got = None
                    tape = vaults.get(address)
                    if tape is None or not _account_seed_valid(got, tape):
                        self.status['seed_failures'] += 1
                        attempts = seed_attempts.get(address, 0)
                        if 0 < attempts <= len(ACCOUNT_SEED_RETRY_S):
                            retry_at[address] = now + ACCOUNT_SEED_RETRY_S[attempts - 1]
                        continue
                    seeded.add(address)
                    await emit(apply_account_seed(address, got, vaults, decimals=decimals,
                        ordering=self._ordering, generation=generation))
                try:
                    local_gap = pending_gap is not None
                    if local_gap:
                        row, pending_gap = pending_gap, None
                    else:
                        row = self.queue.get_nowait()
                except queue.Empty:
                    if not current_ack and now >= ack_deadline:
                        raise AccountCoverageError('subscription_ack_timeout')
                    await asyncio.sleep(.01)
                    continue
                try:
                    if row.get('kind') == 'account_coverage':
                        if row.get('reason') == 'watch_changed':
                            changed = (row.get('connection_generation'), row.get('watch_generation'), self._watched)
                            if changed != last_watch_gap:
                                last_watch_gap, ack_deadline = changed, now + 10
                            for tape in set(vaults.values()):
                                _reset_account_tape(tape)
                            decimals.clear()
                            seeded.clear()
                            seed_attempts.clear()
                            retry_at.clear()
                            await emit(row)
                            continue
                        raise AccountCoverageError(str(row.get('reason') or 'account_gap'))
                    if (row.get('connection_generation') != self.status.get('connection_generation')
                            or row.get('watch_generation') != self.status.get('watch_generation')):
                        self.status['consumer_stale'] += 1
                        continue
                    received = row.get('recv_monotonic')
                    if not isinstance(received, (int, float)) or not 0 <= now - received <= max_age_s:
                        raise AccountCoverageError('account_queue_stale')
                    if received < started:
                        self.status['consumer_stale'] += 1
                        continue
                    await emit(apply_account_row(row, vaults, bindings, decimals=decimals,
                                                ordering=self._ordering))
                finally:
                    if not local_gap:
                        self.queue.task_done()
        except BaseException:
            for tape in set(vaults.values()):
                _reset_account_tape(tape)
            raise
        finally:
            self._watched = ()
            for generation, task in seeds.values():
                task.cancel()
            if seeds:
                await asyncio.gather(*(task for generation, task in seeds.values()), return_exceptions=True)


TOKEN_PROGRAMS = frozenset({"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"})


def _shared_vault_authorities():
    """{vault authority: program} for programs whose vaults share ONE authority PDA. That PDA is
    owned by the System program, so reading its owner cannot name the pool program; it is mapped.
    Derived from the program ids, not typed; both matched the tape's authorities on 2026-09-27."""
    from solders.pubkey import Pubkey
    out = {}
    for program, seed in (("CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C", b"vault_and_lp_mint_auth_seed"),
                          ("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", b"amm authority")):
        pda, _bump = Pubkey.find_program_address([seed], Pubkey.from_string(program))
        out[str(pda)] = program
    return out


def pool_owner_fn(read_rpc_url, *, rpc_call=None):
    """DISPLAY ONLY: the program behind a bound pool, or None. The live-marks publisher uses it to
    tell a constant-product pool, whose quote vault is its depth, from a concentrated one, whose
    vault is not. `venue_kind` cannot: every bound Solana pool is 'amm' or 'pump_curve'.

    An AMM binding's `pool` is its COIN VAULT (the tape keys on the vault), a token account, so its
    owner is the token program (2026-09-27: on-chain liquidity never showed for any AMM mark). The
    program sits behind the vault's SPL authority: a pool account that owns its own vaults
    (PumpSwap) names it by its owner; a shared authority PDA (Raydium CPMM, v4) is mapped; any
    other authority reads as its owner (System for Meteora), which is never constant-product. A
    non-token account (a curve or pool address) keeps its own owner. The caller bounds the
    wall-clock time (up to two reads); this never runs on the stream thread."""
    if rpc_call is None:
        from sniper.services.memecoin_solana_blocktime import _rpc_call

        def rpc_call(method, params):
            return _rpc_call(read_rpc_url, method, params, timeout=2)
    shared = _shared_vault_authorities()

    def account(address, opts):
        res = rpc_call("getAccountInfo", [str(address), {"commitment": "confirmed", **opts}])
        value = res.get("value") if isinstance(res, dict) else None
        return value if isinstance(value, dict) else None

    def owner(pool):
        value = account(pool, {"encoding": "jsonParsed"})
        program = value.get("owner") if value else None
        if not isinstance(program, str) or not program:
            return None
        if program not in TOKEN_PROGRAMS:
            return program
        data = value.get("data")
        info = ((data.get("parsed") or {}).get("info") or {}) if isinstance(data, dict) else {}
        authority = info.get("owner")
        if not isinstance(authority, str) or not authority:
            return None
        if authority in shared:
            return shared[authority]
        auth = account(authority, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}})
        found = auth.get("owner") if auth else None
        return found if isinstance(found, str) and found else None

    return owner


def ws_url_from_read_rpc(url):
    """Convert a read RPC URL to its WebSocket form."""
    raw = str(url or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    scheme = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}.get(
        parsed.scheme.lower()
    )
    if scheme is None or not parsed.netloc:
        return None
    return urlunsplit(
        (scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )


async def run_price_stream_epoch(
    vaults,
    bindings,
    cache,
    *,
    ws_url,
    duration_s=30.0,
    stream_fn=run_vault_stream,
    now_ts_fn=time.monotonic,
    now_iso_fn=None,
    refresh_fn=None,
    seed_fn=None,
    vault_seed_fn=None,
):
    """Record swap marks for one bounded stream epoch.

    `refresh_fn` lets a position that opens MID-EPOCH join the live socket instead of waiting the
    epoch out. It returns `(vaults, bindings)`; both are merged into the dicts the stream and
    `on_row` already read, so the new pool starts pricing within `refresh_s`.
    """
    if now_iso_fn is None:
        from sniper.util.clock import real_utc_now_iso

        now_iso_fn = real_utc_now_iso

    # WHAT THIS EPOCH IS ACTUALLY WATCHING (2026-09-22, QQQ position 63509).
    #
    # That position held for five minutes on a pool that traded SIX times besides our own entry
    # and exit, and produced zero fastlane marks — while the SAME pool, same QQQx quote asset and
    # same code path produced its first mark NINE SECONDS after binding on 09-20 (position 62703,
    # opened 15:09:34, bound 15:09:38, first mark 15:09:47). Two explanations survived, and NO
    # persisted artefact separated them: either the subscription never covered the pool, or it did
    # and the marks died downstream. The only trace either would leave is the absence itself.
    #
    # One line per epoch ends that. It decides nothing and changes no behaviour; it records which
    # pools this epoch subscribed to, so the NEXT such position is answered from evidence instead
    # of reconstructed from what is missing hours later.
    try:
        _pools = sorted(bindings)
        logger.info("sol price stream epoch: %d vault(s), %d pool(s): %s",
                    len(vaults), len(_pools),
                    ",".join(p[:12] for p in _pools[:12]) + ("…" if len(_pools) > 12 else ""))
    except Exception:                      # observability must never cost the epoch
        pass

    cache_set_lp = getattr(cache, "set_lp_bindings", None)
    if cache_set_lp is not None:
        cache_set_lp(bindings)

    async def on_row(row):
        if row.get('kind') == 'account_coverage':
            cache.invalidate()
            if cache_set_lp is not None:
                cache_set_lp(bindings)      # still bound; the reserve evidence starts over (blind)
            return
        binding = bindings.get(str(row.get("pool") or ""))
        if binding is not None:
            cache.record(
                row,
                binding,
                now_ts=now_ts_fn(),
                observed_at=now_iso_fn(),
            )

    stream_kwargs = {"ws_url": ws_url}
    if seed_fn is not None:
        stream_kwargs["seed_fn"] = seed_fn
    if vault_seed_fn is not None:
        stream_kwargs["vault_seed_fn"] = vault_seed_fn
    if refresh_fn is not None:
        def _merge():
            fresh_vaults, fresh_bindings = refresh_fn()
            # `bindings` is what on_row resolves a pool through, so it must gain the new pool BEFORE
            # its first frame arrives; merging it first makes the ordering explicit.
            fresh = dict(fresh_bindings or {})
            # A→B→A (Codex review 2026-10-21): update() keeps a mint's superseded pool, and the
            # mint's binding is then whichever entry comes last. A fresh binding replaces EVERY
            # entry of its mint.
            fresh_mints = {b.mint for b in fresh.values()}
            for pool in [p for p, b in bindings.items() if b.mint in fresh_mints and p not in fresh]:
                del bindings[pool]
            bindings.update(fresh)
            if cache_set_lp is not None:
                cache_set_lp(bindings)
            return fresh_vaults or {}

        stream_kwargs["refresh_fn"] = _merge

    timeout_scope = asyncio.timeout(float(duration_s))
    try:
        async with timeout_scope:
            await stream_fn(vaults, on_row, **stream_kwargs)
    except TimeoutError:
        if timeout_scope.expired():
            cache.invalidate_prices()
            return
        cache.invalidate()
        raise
    except BaseException:
        cache.invalidate()
        raise
    cache.invalidate()  # an early stream return is a disconnect, not a normal epoch rollover
