"""Slot lane: turn one processed source buy into a funded entry that lands as early as possible.

Spec: docs/superpowers/specs/2026-09-26-solana-slot-lane-design.md. Default OFF
(MEMECOIN_SOLANA_SLOT_LANE_ENABLED). Runs on its own thread with its own ledger connection.

The path from signal to bytes makes NO network read. Everything it needs arrives already fetched:
the decoded source event (its post-trade reserves give the price), and a `PrefetchSnapshot` that
a separate refresher keeps warm (SOL/USD, USDC and SOL balances, a recent blockhash, the Jupiter
USDC->SOL leg). An input older than its bound refuses; it is never re-read on the path.

Admission is the in-memory subset the operator approves (spec, ruling 1): wallet armed on solana,
kill switch, the funded mcap ceiling, the source-buy floor, then `reserve_entry`, which owns
one-per-coin, concurrency, exposure, bankroll, the daily-loss latch and the funded contract.

Automatic first-buy policy: persist the first opportunity before selection. Operator 2026-10-20:
quality-approved copies may land in any slot. Missing source holdings or fresh head evidence holds.
Already-sent transactions still reconcile.

Two venues: pump.fun bonding-curve buys (pump_curve_buy.SlotLaneCurveBuilder) and PumpSwap buys
(pumpswap_buy.SlotLanePumpSwapBuilder). A PumpSwap event names the pool, not the mint, so its
mint, virtual quote reserve and supply come from that builder's pool cache (`amm_market_facts`),
which also learns every pool pump.fun migrates into before its first buy (migration prewarm).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from types import SimpleNamespace

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_entry_opportunity as entry_policy
from sniper.services.memecoin_decision_features import from_slot_buy
from sniper.services.memecoin_slot_skips import decision_for as _skip_decision, note as _skip_note, finish_note as _finish_skip_note
from sniper.services.memecoin_address_registry import armed_verified_owners
from sniper.services.memecoin_trial import TRIAL_UNMARKED
from sniper.services.memecoin_rabbit_signal import (DELIVERY_LANE as RABBIT_LANE, PROVENANCE as RABBIT_PROVENANCE,
                                                    is_rabbit)
from sniper.settings import memecoin as cfg

logger = logging.getLogger(__name__)

SIGNAL_SOURCE = "slot_lane"
PUMP_TOTAL_SUPPLY = 1_000_000_000.0   # pump.fun mints 1e9 tokens (6 decimals); pinned, not read
SOL_DECIMALS = 9
PUMP_TOKEN_DECIMALS = 6
VOLUME_REFUSAL = "solana_volume_under_floor"


def cached_volume_decision(cache, buy, facts, snap, *, now_mono=None):
    """Admission's complete cache-only check; unknown identity has no enforceable evidence."""
    if cache is None:
        return None
    venue = getattr(buy, "venue", "pump_curve")
    if venue not in ("pump_curve", "pump_amm"):
        return None
    mint = facts.get("mint")
    pool = mint if venue == "pump_curve" else getattr(buy, "pool", None)
    return cache.check(mint=mint, venue=venue, market_pool=pool,
                       source_signature=buy.signature, mcap_usd=facts.get("mcap_usd"),
                       sol_usd=snap.sol_usd, now_mono=now_mono)


@dataclass(frozen=True)
class PrefetchSnapshot:
    """Warm inputs, each with the monotonic instant it was observed. None = never observed."""
    sol_usd: float | None
    usdc_balance: float | None
    sol_balance: float | None
    blockhash: str | None
    last_valid_height: int | None
    observed_mono: dict   # name -> time.monotonic() of its last successful refresh


# Blockhash: Solana keeps a blockhash valid for 150 blocks, about 32.7 s at the 218 ms slots measured
# since 2026-10-10 (60 s at 400 ms), so a 20 s-old cached one leaves about 12.7 s, ample for a
# sub-second send but a third of the old margin.
MAX_AGE_S = {"sol_usd": 30.0, "balances": 10.0, "blockhash": 20.0}
LEG_REFRESH_S = 1.5      # each venue's Jupiter leg is re-quoted at most this often
# Receipt freshness and receipt-resume age bound; the signed S/S+1 deadline is independent.
MAX_SIGNAL_AGE_S = 0.5
def monotonic_ms(later, earlier):
    """Unknown/backwards clock readings stay unknown; a measured zero remains zero."""
    if isinstance(later, bool) or isinstance(earlier, bool):
        return None
    try:
        elapsed = (float(later) - float(earlier)) * 1000.
        return elapsed if math.isfinite(elapsed) and elapsed >= 0 else None
    except (TypeError, ValueError):
        return None


class LaneBuyQueue:
    """Stamp each enqueue without changing the source receipt or any admission deadline."""

    def __init__(self, *, clock=time):
        import queue
        self.queue, self.clock = queue.SimpleQueue(), clock

    def put(self, buy):
        self.queue.put((buy, self.clock.monotonic()))

    def get(self, *, timeout=None):
        return self.queue.get(timeout=timeout)

    def empty(self):
        return self.queue.empty()


class QueueTiming:
    """Lane-owner telemetry; counts ALL handle calls including pre-attempt refusals and retries."""

    def __init__(self):
        from collections import deque
        self.samples = deque(maxlen=512)
        self.counts = dict(handled=0, queue_expired=0, already_old=0, wallet_busy=0, unknown=0)

    def record(self, result):
        self.counts["handled"] += 1
        timing = result.get("queue_timing") or {}
        queued, before, age = (timing.get(k) for k in
                              ("queue_ms", "receipt_to_enqueue_ms", "receipt_to_decision_ms"))
        if queued is not None:
            self.samples.append(queued)
        if any(value is None for value in (queued, before, age)):
            self.counts["unknown"] += 1
        elif result.get("reason") == "slot_lane_signal_too_old":
            if before > MAX_SIGNAL_AGE_S * 1000:
                self.counts["already_old"] += 1
            elif age > MAX_SIGNAL_AGE_S * 1000:
                self.counts["queue_expired"] += 1
            else:  # expiry crossed inside the handler; queue attribution is unknown
                self.counts["unknown"] += 1
        if result.get("reason") == "slot_lane_wallet_busy":
            self.counts["wallet_busy"] += 1

    def snapshot(self):
        from statistics import median
        rows = sorted(self.samples)
        return {**self.counts, "sample": "lane_handle_calls_including_retries",
                "window": len(rows), "queue_ms": {
                    "p50": median(rows) if rows else None,
                    "p90": rows[math.ceil(.9 * len(rows))-1] if rows else None,
                    "max": rows[-1] if rows else None}}

# A buy whose regime scale differs from the scale the prefetched leg was sized for is refused
# before reserving. memecoin_onchain_lane.slot_lane_outcome treats this reason as provably unsent
# (one fallback).
SCALED_CLIP_REASON = "slot_lane_scaled_clip"
# EXOTIC RE-QUOTE (MEMECOIN_SLOT_LANE_EXOTIC_REQUOTE, Alpha GO 2026-10-07): a build over the packet goes
# back to the on-demand worker for a tighter leg, these route shapes in order, each at most once.
REQUOTE_PARAMS = ("&maxAccounts=20", "&onlyDirectRoutes=true")
REQUOTE_PENDING_REASON = "slot_lane_requote_pending"
# The processed copy of a buy a rabbit-led fire still owns (memecoin_rabbit_signal).
RABBIT_DUPLICATE_REASON = "slot_lane_rabbit_led_duplicate"
# The re-quote's hold from the hand-back: a leg is p50 151 / p90 308 / max 408 ms (n=26, 2026-10-07);
# past it the sweep gives the buy its one fallback.
REQUOTE_HOLD_S = 1.5


def exotic_requote_mode():
    """off | oversize from MEMECOIN_SLOT_LANE_EXOTIC_REQUOTE; anything else is off."""
    value = str(getattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_REQUOTE", "off") or "off").strip().lower()
    return "oversize" if value == "oversize" else "off"


def requote_attempts(buy):
    od = getattr(buy, "ondemand", None)
    return int((od or {}).get("requote_attempts") or 0)


def packet_bytes(detail):
    """The byte count a build refusal 'transaction is N bytes, over ...' names, or None."""
    import re
    hit = re.search(r"transaction is (\d+) bytes", str(detail or ""))
    return int(hit.group(1)) if hit else None


def _wire_bytes(message):
    """The signed wire size of an unsigned MessageV0 (payer = its first key), or None."""
    try:
        from solders.null_signer import NullSigner
        from solders.transaction import VersionedTransaction
        return len(bytes(VersionedTransaction(message, [NullSigner(message.account_keys[0])])))
    except Exception:
        return None


# How long a quote-token graduation hand-back holds its fresh on-demand leg (memecoin_slot_graduation):
# the worker's leg is ~0.15 s (HI EtVFMDoB 142 ms); past this the on-chain lane gets its one fallback.
GRADUATION_LEG_HOLD_S = 2.0
# Refusals the ordinary on-chain lane can still judge, so they are handed to it (see Handoff).
HANDOFF_REASONS = frozenset({"pool_unknown", "slot_lane_price_unreadable",
                             # a curve quoted in another token: slot_lane_price_unreadable until
                             # 2026-10-07; its hand-off is still skipped_unsized (no curve size)
                             "slot_lane_curve_quote_not_sol", "slot_lane_curve_quote_not_supported",
                             "slot_lane_curve_usdc_quote_unsupported"})
HANDOFF_BASIS = "slot_lane_handoff"
HANDOFF_BUSY_TIMEOUT_MS = 150
NATIVE_ATA_RENT_RESERVE_LAMPORTS = 5_000_000  # each target/wSOL ATA; overstates standard rent
# Venues the lane builds but never reserves or sends, whatever the lane's own mode (the CPMM build
# is shadow-only: MEMECOIN_SLOT_LANE_CPMM has no live mode yet). A test pins that none reaches a send.
SHADOW_ONLY_VENUES = frozenset({"raydium_cpmm", "raydium_launchlab", "meteora_damm_v2", "meteora_dbc"})
# A venue buy refused for its quote asset alone: the on-demand leg worker's cue.
ONDEMAND_QUOTE_REASONS = frozenset({"slot_lane_cpmm_quote_not_supported",
                                    "slot_lane_launchlab_quote_not_supported",
                                    "slot_lane_pump_quote_not_supported",
                                    "slot_lane_damm_v2_quote_not_supported",
                                    "slot_lane_curve_quote_not_supported",
                                    "slot_lane_dbc_quote_not_supported"})
# EXOTIC QUOTES LIVE (spec 2026-10-02-memecoin-slot-lane-exotic-quotes-live-design.md). A venue on
# MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES hands such a buy to the on-demand worker, which quotes its leg
# on the funded seam and puts it back on the lane queue with `ondemand` set; the lane then runs the
# ordinary live path. Its sends count under "<venue>:exotic", 2 an hour unless overridden (Gamma's
# canary), apart from the venue's own SOL/USDC sends.
EXOTIC_VENUES = ("raydium_cpmm", "raydium_launchlab", "pump_amm", "meteora_damm_v2", "pump_curve",
                 "meteora_dbc")
EXOTIC_CANARY_CAP = 2
EXOTIC_PENDING_REASON = "slot_lane_exotic_leg_pending"
EXOTIC_AGED_OUT_REASON = "slot_lane_exotic_aged_out"


def exotic_live(venue):
    """Whether `venue` sends exotic-quote buys live: named on MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES
    AND live in its own mode."""
    if venue not in EXOTIC_VENUES or venue not in (
            getattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", None) or ()):
        return False
    if venue == "raydium_cpmm":
        from sniper.execution import raydium_cpmm_buy
        return bool(raydium_cpmm_buy.cpmm_live())
    if venue in ("pump_amm", "pump_curve"):  # neither has a mode of its own: the lane's
        return lane_mode() == "live"
    if venue == "meteora_damm_v2":
        from sniper.execution import meteora_damm_v2_buy
        return bool(meteora_damm_v2_buy.damm_live())
    if venue == "meteora_dbc":
        from sniper.execution import meteora_dbc_buy
        return bool(meteora_dbc_buy.dbc_live())
    from sniper.execution import raydium_launchlab_buy
    return bool(raydium_launchlab_buy.launchlab_live())


def exotic_cap_key(venue):
    return "%s:exotic" % venue


class PendingLegs:
    """Signatures whose exotic leg is out on the worker: signature -> (buy, deadline monotonic).

    ONE PATH PER SOURCE BUY (Gamma M1/M2, 2026-10-02). While an entry is held,
    memecoin_onchain_lane.slot_lane_outcome answers `in_flight`, so the on-chain lane defers instead
    of racing the lane's reservation. Exactly one party resolves an entry, under this lock: the
    worker's refusal (`claim`, then the routed row), the expiry sweep (`expired`, then the routed
    row), or the lane thread after its re-entry has run (`claim`, after the attempt row exists).
    The deadline is the buy's receipt + MAX_SIGNAL_AGE_S, so the on-chain lane waits at most that.
    A process restart drops the registry; the watcher's own later row still reaches the on-chain
    lane.

    RELEASED WITHOUT AN ATTEMPT (Gamma MED-1, 2026-10-02). A detection the on-chain lane deferred on
    a hold must not wait forever once the hold ends with no slotlane:<sig> row: every resolution
    records the release, and for RELEASE_TTL_S (slot_lane_outcome's lookback) slot_lane_outcome
    answers `failed` for it, which is that lane's ONE fallback. An attempt row still decides first."""

    RELEASE_TTL_S = 120.0

    def __init__(self):
        self._lock = threading.Lock()
        self._held = {}
        self._released = {}                    # signature -> (monotonic release time, how)

    def __len__(self):
        with self._lock:
            return len(self._held)

    def add(self, buy, deadline):
        with self._lock:
            if buy.signature in self._held:
                return False
            self._held[buy.signature] = (buy, float(deadline))
            return True

    def holds(self, signature, now):
        with self._lock:
            entry = self._held.get(signature)
            return entry is not None and now < entry[1]

    def attempt_state(self, signature, now, *, requote_enabled):
        """Observe attempt ownership and a prior policy release under the existing hold lock."""
        with self._lock:
            entry = self._held.get(signature)
            held = (entry is not None and now < entry[1]
                    and (requote_enabled or
                         (getattr(entry[0], "graduation", None) or {}).get("cash_leg_repair") is True))
            at, how = self._released.get(signature, (None, None))
            released = how if at is not None and now - at < self.RELEASE_TTL_S else None
            return held, released

    def extend(self, signature, deadline):
        """Move a HELD signature's deadline out to `deadline` (never in) in place, so no reader ever
        sees it released. False when it is not held."""
        with self._lock:
            entry = self._held.get(signature)
            if entry is None:
                return False
            self._held[signature] = (entry[0], max(entry[1], float(deadline)))
            return True

    def claim(self, signature, now=None, how="claimed"):
        now = time.monotonic() if now is None else now
        with self._lock:
            entry = self._held.pop(signature, None)
            if entry is not None:
                self._release(signature, now, how)
        return entry[0] if entry is not None else None

    def expired(self, now):
        with self._lock:
            gone = [sig for sig, (_buy, deadline) in self._held.items() if now >= deadline]
            for sig in gone:
                self._release(sig, now, "expired")
            return [self._held.pop(sig)[0] for sig in gone]

    def mark(self, signature, now):
        """Record a release with no hold: a routed buy the live lane handed off unsent."""
        with self._lock:
            self._release(signature, now, "marked")

    def released(self, signature, now):
        return self.release_kind(signature, now) is not None

    def release_kind(self, signature, now):
        """How a signature was released inside RELEASE_TTL_S: "claimed", "expired", "marked" or
        "policy" (a worker policy refusal: no fallback class); else None."""
        with self._lock:
            at, how = self._released.get(signature, (None, None))
            return how if at is not None and now - at < self.RELEASE_TTL_S else None

    def hold_left_ms(self, signature, now):
        """Milliseconds left on a held signature's deadline, or None when it is not held."""
        with self._lock:
            entry = self._held.get(signature)
        return max(0.0, (entry[1] - now) * 1000.0) if entry is not None else None

    def _release(self, signature, now, how):
        """Under the lock. Old markers are dropped as new ones arrive (a few exotic buys an hour)."""
        self._released[signature] = (now, how)
        for sig in [s for s, (at, _how) in self._released.items() if now - at >= self.RELEASE_TTL_S]:
            del self._released[sig]


EXOTIC_PENDING = PendingLegs()
CONFIRMED_PENDING = PendingLegs()
# Routed buys the LIVE lane handed off because it has no live venue for them (no send, no attempt
# row). memecoin_onchain_lane.slot_lane_verdict reads this as the `routed_handoff` fallback class;
# a restart drops it, and a dropped mark refuses (absence never admits).
ROUTED_HANDOFFS = PendingLegs()
# Curve buys whose canonical PumpSwap pool the graduation watcher is reading or has just handed back
# (memecoin_slot_graduation). The on-chain lane defers while one is held; a release with no new
# attempt row is the `graduation_released` fallback class.
GRADUATION_PENDING = PendingLegs()


def _entry_opportunity_refusal(reason):
    return str(reason or '').startswith(('first_source_buy_', 'source_first_buy_',
                                        'source_buy_', 'source_already_holding', 'entry_slot_'))


def _source_pre_atomic(buy):
    pre = getattr(buy, 'source_pre_atomic', None)
    routed_pre = getattr(getattr(buy, 'routed', None), 'source_pre_atomic', None)
    return routed_pre if pre is None or type(routed_pre) is int and routed_pre > 0 else pre


def _source_initiation(buy, mint):
    from sniper.services.memecoin_source_initiation import solana_program_initiation
    return getattr(buy, 'source_initiation', None) or solana_program_initiation(buy, mint)


def policy_refusal(reason):
    """True for an admission refusal that is POLICY: the kill switch, the roster, the mcap ceiling and
    the source-buy family (every one of its reasons starts "source buy "), plus the cached-price veto.
    Under fast-lane-only a policy refusal never becomes a late ordinary purchase (2026-10-07:
    7KEPApdb, CwSNUU54 and DVypDG8A did, as exotic_released fallbacks). Other venue, cache, quote,
    leg and capacity refusals are mechanical
    and keep their one fallback; so does an unrecognised reason, which keeps today's behaviour rather
    than silently losing a buy. Pinned against the real producers in test_slot_lane_exotic_live."""
    reason = str(reason or "")
    return (reason in ("kill_switch", "not_approved", "cached_price_beyond_limit",
                       "intervening_market_cap", VOLUME_REFUSAL)
            or reason.startswith(("mcap $", "source buy "))
            or _entry_opportunity_refusal(reason))


@dataclass(frozen=True)
class ExoticRefusal:
    """A live leg the worker refused, put on the lane queue: the lane thread writes its one fallback,
    on the routed handoff's own connection (never from the worker's thread)."""
    buy: object
    reason: str


def sweep_expired_legs(pending, now, fallback_fn):
    """Lane thread, between queue items: every leg past its deadline gets its one fallback."""
    expired = pending.expired(now)
    for buy in expired:
        try:
            fallback_fn(buy)
        except Exception:
            logger.exception("slot lane exotic expiry fallback failed sig=%s", buy.signature)
    return len(expired)


def outcome_mode(lane_mode, buy, result):
    """"live" when the lane would have SENT this buy (lane mode live, venue not shadow-only, and not a
    shadow build), else "shadow". The venue verifiers' lines carry it (Alpha 2026-10-06: a live-lane
    DAMM v2 refusal printed "damm_v2 shadow" and read as a shadow-only venue)."""
    if result.get("status") == "shadow" or lane_mode != "live" or shadow_only(buy):
        return "shadow"
    return "live"


def with_processed_routed_proof(buy):
    """`buy` resumed on its own processed transaction's proof (MEMECOIN_SLOT_LANE_ROUTED_PROCESSED_PROOF),
    with the same checks a confirmed resume passes in ReceiptResumer.offer; `buy` unchanged when the
    knob is off, it already has a proof, or any check fails (it then waits for the confirmed receipt
    exactly as before). Pure: no I/O."""
    from sniper.services.memecoin_source_initiation import DIRECT_PROOF_VERSION, _quantity
    routed = getattr(buy, "routed", None)
    proof = getattr(routed, "source_initiation", None)
    if (not cfg.MEMECOIN_SLOT_LANE_ROUTED_PROCESSED_PROOF or not isinstance(proof, dict)
            or getattr(buy, "source_initiation", None) is not None):
        return buy
    try:
        acquired, payment = int(proof["acquired_atomic"]), int(proof["payment_atomic"])
        if (proof.get("proof_version") != DIRECT_PROOF_VERSION or proof.get("tx_hash") != buy.signature
                or proof.get("payer") != buy.owner or proof.get("beneficiary") != buy.owner
                or proof.get("mint") != buy.mint or _quantity(proof.get("block_number")) != buy.slot
                or proof.get("source_user_id") != buy.source_user_id
                or routed.signature != buy.signature or routed.owner != buy.owner
                # The built venue leg is PART of the proven acquisition (a split route's leg is smaller).
                or not (type(buy.token_amount) is int and 0 < buy.token_amount <= acquired)
                or acquired != routed.token_delta_atomic or payment != routed.usdc_spent_atomic):
            return buy
        route = _quantity(proof.get("route_payment_atomic"))
        if route is not None and 0 < route <= routed.usdc_spent_atomic:
            routed = dataclasses.replace(routed, usdc_spent_atomic=route)
        elif cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT > 0 or getattr(routed, "requires_receipt_proof", False):
            return buy                       # the copy-premium cap needs the bound route payment
    except (KeyError, TypeError, ValueError, AttributeError):
        return buy
    return dataclasses.replace(buy, source_initiation=dict(proof), routed=routed,
                               proof_ready_monotonic=getattr(routed, "decoded_monotonic", None)
                               or buy.recv_monotonic, proof_provenance="processed_receipt")


def proof_lane(buy):
    """(delivery_lane, source_slot_provenance) for a buy carrying a receipt proof."""
    if is_rabbit(buy):
        return RABBIT_LANE, RABBIT_PROVENANCE
    if getattr(buy, "proof_provenance", None) == "processed_receipt":
        return "slot_lane_processed", "processed_receipt"
    return "slot_lane_confirmed_receipt", getattr(buy, "proof_provenance", None) or "confirmed_receipt"


def shadow_only(buy):
    """Whether this buy may only be built and timed, never reserved or sent: its venue is in
    SHADOW_ONLY_VENUES, except a CPMM or LaunchLab buy whose quote asset is on that venue's live list
    (raydium_cpmm_buy.cpmm_live_for, raydium_launchlab_buy.launchlab_live_for; both EMPTY in code).
    An unknown quote asset is shadow."""
    venue = getattr(buy, "venue", "pump_curve")
    if (getattr(buy, "graduation", None) or {}).get("cash_leg_repair") is True:
        return not exotic_live("pump_amm")
    quote = (getattr(buy, "quote_mint", None)
             or getattr(getattr(buy, "venue_event", None), "input_mint", None))
    if venue == "raydium_cpmm":
        from sniper.execution.raydium_cpmm_buy import cpmm_live_for
        return not (cpmm_live_for(quote) or (quote is not None and exotic_live(venue)))
    if venue == "raydium_launchlab":
        from sniper.execution.raydium_launchlab_buy import launchlab_live_for
        return not (quote is not None and (launchlab_live_for(quote) or exotic_live(venue)))
    if venue == "meteora_damm_v2":
        from sniper.execution.meteora_damm_v2_buy import WSOL_MINT, damm_live
        return not (damm_live() and (quote == WSOL_MINT or (quote is not None and exotic_live(venue))))
    if venue == "meteora_dbc":
        from sniper.execution.meteora_dbc_buy import WSOL_MINT, dbc_live
        return not (dbc_live() and (quote == WSOL_MINT or (quote is not None and exotic_live(venue))))
    return venue in SHADOW_ONLY_VENUES
# A pool miss is learned at once and the buy retried ONCE (MissLearner), inside this budget from
# the miss (Alpha: <= 250 ms); past it the buy is handed off. At most this many misses wait.
MISS_LEARN_BUDGET_S = 0.25
MISS_LEARN_MAX_WAITING = 8
# The PumpSwap learner pings its kept-alive pool-read connection after this long idle, so a miss
# pays no TCP + TLS handshake (2026-10-07). Inside pump_curve_buy.POOL_READ_KEEPALIVE_S.
MISS_LEARN_KEEP_WARM_S = 20.0
# Routed buys (memecoin_slot_signal.RoutedPairer): their detection rows carry this basis.
ROUTED_BASIS = "slot_lane_routed"
ROUTED_MODES = ("off", "shadow", "live")


def stale_input(snapshot, now_mono):
    """The first missing or over-age input's name, or None when every input is fresh."""
    checks = (("sol_usd", snapshot.sol_usd), ("balances", snapshot.usdc_balance),
              ("balances", snapshot.sol_balance), ("blockhash", snapshot.blockhash))
    for name, value in checks:
        seen = snapshot.observed_mono.get(name)
        if value is None or seen is None or now_mono - seen > MAX_AGE_S[name]:
            return name
    return None


def curve_price_usd(buy, sol_usd):
    """USD per token from the event's post-trade virtual reserves, or None when unreadable."""
    try:
        vsol = float(buy.virtual_sol_reserves) / 10 ** SOL_DECIMALS
        vtok = float(buy.virtual_token_reserves) / 10 ** PUMP_TOKEN_DECIMALS
        price = vsol / vtok * float(sol_usd)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def curve_market_facts(buy, sol_usd, quote=None):
    """(reason, facts) for a pump curve buy. A SOL curve prices from the SlotBuy's own post-trade
    reserves, as before. A curve quoted in another token (every SOL field zero; 2026-10-07, 13 coins
    since 09-26) parses its event: with "pump_curve" listed exotic-live it prices from the event's
    quote fields at USDC $1, or at `quote` = (decimals, USD) from the on-demand leg, and without a
    quote refuses slot_lane_curve_quote_not_supported, the worker's cue. Unlisted:
    slot_lane_curve_quote_not_sol. Either refusal names the quote mint."""
    price = curve_price_usd(buy, sol_usd)
    if price is not None:
        paid, basis = paid_quote_atomic(int(buy.sol_amount), _curve_gross_lamports(buy))
        return None, {"price_usd": price, "mcap_usd": price * PUMP_TOTAL_SUPPLY,
                      "their_usd": paid / 10 ** SOL_DECIMALS * float(sol_usd),
                      "their_usd_basis": basis, "mint": buy.mint}
    from sniper.execution.pump_curve_buy import SOL_QUOTE_MINTS, USDC_MINT, parse_trade_event
    if int(getattr(buy, "virtual_sol_reserves", None) or 0) != 0:
        return "slot_lane_price_unreadable", {}
    try:
        event = parse_trade_event(getattr(buy, "trade_event_log", None) or "")
    except (TypeError, ValueError):
        return "slot_lane_price_unreadable", {}
    quote_mint = event.get("quote_mint")
    if quote_mint in SOL_QUOTE_MINTS or event.get("mint") != getattr(buy, "mint", None):
        return "slot_lane_price_unreadable", {}
    named = {"quote_mint": quote_mint}
    if not exotic_live("pump_curve"):
        return "slot_lane_curve_quote_not_sol", named
    if quote_mint == USDC_MINT:
        # ponytail: none of the 13 quote-token curves since 09-26 was USDC-quoted; one would need
        # the coin's mint read without a worker leg. Build it when one appears.
        return "slot_lane_curve_usdc_quote_unsupported", named
    if quote is None:
        return "slot_lane_curve_quote_not_supported", named
    return quote_curve_facts(event, quote)


def quote_curve_facts(event, quote):
    """(reason, facts) from a quote-curve TradeEvent at `quote` = (decimals, USD per whole quote)."""
    from sniper.execution.pump_curve_buy import PUMP_TOKEN_DECIMALS, quote_curve_gross, quote_curve_prices
    price, _average = quote_curve_prices(event, quote[0], quote[1], PUMP_TOKEN_DECIMALS)
    gross = quote_curve_gross(event)
    if price is None or gross is None:
        return "slot_lane_price_unreadable", {"quote_mint": event.get("quote_mint")}
    return None, {"price_usd": price, "mcap_usd": price * PUMP_TOTAL_SUPPLY,
                  "their_usd": gross / 10 ** int(quote[0]) * float(quote[1]),
                  "their_usd_basis": "gross", "mint": event["mint"], "quote_mint": event["quote_mint"]}


def paid_quote_atomic(net, gross):
    """(atomic, basis) the source-buy gates judge: what the trader PAID into the swap under
    MEMECOIN_SOURCE_SIZE_BASIS=gross (the J7 basis), else the pool leg net of fees. A gross that is
    unreadable or below the net falls back to the net, never to unknown."""
    if (str(getattr(cfg, "MEMECOIN_SOURCE_SIZE_BASIS", "gross")) != "net"
            and gross is not None and gross >= net):
        return gross, "gross"
    return net, "net"


def amm_market_facts(buy, pools, sol_usd, quote=None, *, cached_pool=None):
    """(reason, facts) for a PumpSwap buy, from the event line and the builder's pool cache only.

    A PumpSwap BuyEvent names the pool, not the mint, and carries PRE-trade vault reserves. The
    mint, the pool's virtual quote reserve and the base supply come from the pool cache; a miss
    refuses `pool_unknown` and is queued for the prefetch thread to learn (operator ruling via
    Alpha: miss-then-learn; the migration prewarm learns most pools before their first buy). The
    price is POST-trade: vault reserves after the source buy plus the
    Pool account's virtual quote, which is what the program prices against. The mcap uses the base
    mint's REAL supply (a burned pump coin holds less than 1e9) and is built only for pools created
    by pump.fun's migration; any other pool refuses, because its supply is not a pump supply.

    A POOL QUOTED IN ANYTHING BUT SOL (E2, spec 2026-10-02 exotic quotes; all 54 census pools are
    migrations) prices only with "pump_amm" listed live: USDC at $1 in its own decimals, any other
    quote at `quote` = (decimals, USD) from the on-demand leg; without one it refuses
    slot_lane_pump_quote_not_supported, the worker's cue. Unlisted: slot_lane_quote_not_sol."""
    from sniper.execution import pumpswap_buy as ps
    from sniper.execution.pump_curve_buy import USDC_MINT, WSOL_MINT
    try:
        ev = ps.parse_buy_event(getattr(buy, "trade_event_log", None) or "")
    except (TypeError, ValueError):
        return "slot_lane_event_unreadable", {}
    if getattr(buy, "pool", None) not in (None, ev["pool"]):
        return "slot_lane_event_pool_mismatch", {}
    pool = cached_pool if cached_pool is not None else pools.get(ev["pool"])
    if pool is None:
        return "pool_unknown", {}
    quote_mint = pool.get("quote_mint")
    if quote_mint == WSOL_MINT:
        qdec, quote_usd = SOL_DECIMALS, sol_usd
    elif not exotic_live("pump_amm"):
        return "slot_lane_quote_not_sol", {}
    elif pool.get("quote_decimals") is None:
        return "slot_lane_quote_decimals_unknown", {}
    elif quote is not None:
        qdec, quote_usd = int(pool["quote_decimals"]), quote[1]
        if int(quote[0]) != qdec:
            return "slot_lane_quote_decimals_mismatch", {}
    elif quote_mint == USDC_MINT:
        qdec, quote_usd = int(pool["quote_decimals"]), 1.0
    else:
        return "slot_lane_pump_quote_not_supported", {}
    if not pool.get("pump_migrated"):
        return "slot_lane_not_pump_mint", {}
    supply, decimals = pool.get("base_supply"), pool.get("base_decimals")
    if not supply or decimals is None:
        return "supply_unknown", {}
    try:
        base_post, quote_post = ps.post_trade_reserves(ev)
        tokens = float(base_post) / 10 ** int(decimals)
        price = (float(quote_post) + float(pool["virtual_quote"])) / 10 ** qdec / tokens * float(quote_usd)
    except (TypeError, ValueError, ZeroDivisionError, KeyError):
        return "slot_lane_price_unreadable", {}
    if not (math.isfinite(price) and price > 0):
        return "slot_lane_price_unreadable", {}
    # The source's own net quote into the pool (lp fee excluded), the same basis as the curve's
    # sol_amount; it holds for exact-in and exact-out buys alike.
    net_in = int(ev["quote_amount_in_with_lp_fee"]) - int(ev["lp_fee"])
    paid, basis = paid_quote_atomic(net_in, ps.buy_event_gross_quote(ev))
    return None, {"price_usd": price, "mcap_usd": price * float(supply) / 10 ** int(decimals),
                  "their_usd": paid / 10 ** qdec * float(quote_usd),
                  "their_usd_basis": basis, "mint": pool["base_mint"], "quote_mint": quote_mint,
                  "pool": ev["pool"]}


def _curve_gross_lamports(buy):
    """The curve buy's paid lamports from its own TradeEvent line, or None (unreadable, another
    trader's event, or a sell). Never raises."""
    from sniper.execution.pump_curve_buy import parse_trade_event, trade_event_gross_lamports
    try:
        event = parse_trade_event(getattr(buy, "trade_event_log", None) or "")
    except (TypeError, ValueError):
        return None
    if (event.get("is_buy") is not True or event.get("mint") != getattr(buy, "mint", None)
            or int(event.get("sol_amount") or -1) != int(buy.sol_amount)):
        return None
    return trade_event_gross_lamports(event)


def _receipt_provenance(buy):
    """Optional diagnostics cannot veto funded execution, even on legacy/injected buys."""
    try:
        original = getattr(buy, 'receipt_evidence', None) or getattr(
            getattr(buy, 'routed', None), 'receipt_evidence', None)
        if not original:
            return {}
        fields = dict(original)
        for value in fields.values():
            if not isinstance(value, (str, int, float, bool, type(None))):
                return {}
            if isinstance(value, (int, float)) and not math.isfinite(value):
                return {}
        allowed = ('producer_run_id', 'boot_id', 'clock_domain', 'recv_monotonic',
                   'connection_generation', 'roster_generation', 'config_generation', 'roster_json')
        return {key: value for key, value in fields.items() if key in allowed}
    except Exception:
        return {}


def _receipt_facts(buy, result=None):
    try:
        original = _receipt_provenance(buy)
        if not original:
            return None
        result = result or {}
        reason = result.get('reason') or ''
        policy = policy_refusal(reason)
        facts = dict(original, source_slot=getattr(buy, "slot", None),
            stratum=("graduation" if getattr(buy, "graduation", None) is not None else
                     "routed_confirmed_proof" if (getattr(buy, "routed", None) is not None
                                                or hasattr(buy, "paired_monotonic")) else
                     "native_local_proof"),
            source_wallet=buy.owner, source_sig=buy.signature,
            mint=result.get('mint') or getattr(buy, 'mint', None), pool=getattr(buy, 'pool', None),
            network_id='solana', policy_refused=policy, independent_inputs_complete=policy)
        for key, value in (result.get("stage_spans_ms") or {}).items():
            facts["stage_" + key] = value
        for key, value in (result.get("queue_timing") or {}).items():
            if key.endswith("_ms"):
                facts[key] = value
        for key in ('their_usd', 'mcap_usd', 'price_usd', 'source_impact_ratio'):
            value = result.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                facts[key] = value
        return facts
    except Exception:
        return None


def receipt_head(buy):
    """The processed head the signal socket knew when this buy arrived, as trace facts.

    `head_at_recv_minus_s` is feed-local head skew, not remaining bank time. A delayed head
    notification may report S-1 while the transaction already belongs to S. None is unknown."""
    head, seen = getattr(buy, "head_at_recv", None), getattr(buy, "head_at_recv_mono", None)
    basis = "receipt"
    routed = getattr(buy, "routed", None)
    if head is None and routed is not None:
        # A ROUTED buy (2026-10-16, MIDWAY 7de9dad9): no frame head, so the head the feed knew when its
        # pair became complete, the earliest the lane could act on it (RoutedBuy.paired_head_slot). The
        # first half's head is at most this, so "early" stays conservative. Its age is unknown.
        head, seen, basis = getattr(routed, "paired_head_slot", None), None, "routed_pair_complete"
    recv = getattr(buy, "recv_monotonic", None)
    evidence = getattr(buy, "receipt_evidence", None)
    evidence = evidence if isinstance(evidence, Mapping) else {}
    received = {"head_skew_basis": "feed_local_head_minus_source_slot",
                "connection_generation": getattr(buy, "connection_generation",
                                                  evidence.get("connection_generation")),
                "slot_status": getattr(buy, "slot_status", evidence.get("slot_status"))}
    if type(head) is not int or head <= 0:
        return {"head_at_recv": None, "head_at_recv_age_ms": None,
                "head_at_recv_minus_s": None, "head_at_recv_basis": None, **received}
    age = ((float(recv) - float(seen)) * 1000.0
           if isinstance(seen, (int, float)) and isinstance(recv, (int, float)) else None)
    return {"head_at_recv": head, "head_at_recv_age_ms": age,
            "head_at_recv_minus_s": head - int(buy.slot), "head_at_recv_basis": basis, **received}


# The handoff valuation's recv_slot label, by receipt_head's basis (Gamma LOW-1 on 0e28f2e85). A routed
# buy's head is the one its pair completed at, a later and different reading than a frame's processed
# receipt; memecoin_fill_timing's detect stage reads recv_slot, so the row must say which it is.
RECV_SLOT_BASES = {"receipt": "processed_receipt", "routed_pair_complete": "routed_pair_complete"}


def recv_slot_fields(buy):
    """{recv_slot, recv_slot_basis} for a handoff valuation; both None when the head is unknown."""
    head = receipt_head(buy)
    return {"recv_slot": head["head_at_recv"],
            "recv_slot_basis": RECV_SLOT_BASES.get(head["head_at_recv_basis"])}


def slot_offsets(buy, result):
    """Receipt head minus S and landing slot minus S for one handled buy; None when unknown."""
    landing = (result or {}).get("landing_slot")
    return {"head_at_recv_minus_s": receipt_head(buy)["head_at_recv_minus_s"],
            "landing_minus_s": (landing - int(buy.slot)
                                if isinstance(landing, int) and landing > 0 else None)}


EXOTIC_SWEEP_S = 0.05


def exotic_worker_line(buy, result):
    """One line per LIVE on-demand leg the worker resolved: handed back, or refused (and why)."""
    def num(key, fmt="%.0f"):
        value = result.get(key)
        return fmt % value if isinstance(value, (int, float)) else None
    return ("[memecoin] slot lane exotic %s %s %s -> %s %s leg_ms=%s age_ms=%s quote=%s quote_usd_paid=%s "
            "impact=%s warm=%d sig=%s" % (
                getattr(buy, "venue", "?"), getattr(buy, "handle", "?"),
                str(getattr(buy, "mint", None) or "?")[:8], result.get("status"), result.get("reason"),
                num("leg_ms"), num("signal_age_ms"), str(result.get("quote") or "?")[:8],
                num("quote_usd_paid", "%.6g"), num("price_impact", "%.4f"), bool(result.get("warm_leg")),
                getattr(buy, "signature", None)))


def result_log_line(buy, result):
    line = "[memecoin] slot lane %s %s S=%s -> %s %s ms=%s head-S=%s recv-S=%s land-S=%s" % (
        getattr(buy, "handle", "?"),
        str(getattr(buy, "mint", None) or getattr(buy, "pool", None) or "?")[:8],
        getattr(buy, "slot", "?"), result.get("status"), result.get("reason") or "",
        "%.1f" % (result.get("build_ms") or result.get("lane_ms") or 0.0),
        result.get("head_minus_s"), result.get("head_at_recv_minus_s"), result.get("landing_minus_s"))
    if result.get("quote_mint") and result.get("status") == "rejected":
        line += " quote=%s" % str(result["quote_mint"])[:8]      # which token a curve is quoted in
    exotic = result.get("exotic")
    if exotic:
        # Alpha 2026-10-02: the age at re-entry per exotic buy, so coverage is read, not assumed.
        line += " exotic %s age_ms=%s queue_ms=%s leg_ms=%s quote=%s" % tuple(
            exotic.get(k) for k in ("venue", "age_ms", "queue_ms", "leg_ms", "quote"))
    return line + ' feed=' + getattr(buy, 'signal_feed', 'helius')


def chain_refusal(legs, pools):
    """The name of a two-leg PumpSwap chain's refusal (decode_owner_buy's CHAIN_REFUSAL), from the pool
    cache only: multi_own_buy:chain_exotic_quote:<A> (A = leg 1's base mint, the hop), chain_quote_usdc
    when leg 1 is quoted in USDC, pool_unresolved when leg 1's pool is not cached. No read, no write.
    The multi_own_buy: prefix keeps the on-chain lane's slot_shape_unsupported class."""
    from sniper.execution.pump_curve_buy import USDC_MINT
    facts = pools.get(legs[0]) if pools is not None and legs else None
    if not facts or not facts.get("base_mint"):
        return "multi_own_buy:pool_unresolved"
    if facts.get("quote_mint") == USDC_MINT:
        return "multi_own_buy:chain_quote_usdc"
    return "multi_own_buy:chain_exotic_quote:" + facts["base_mint"]


def admission_refusal(buy, *, armed_ids, sol_usd, kill_tripped, amm_pools=None, cpmm_pools=None,
                      launchlab_pools=None, venue_quote=None, damm_pools=None, prior_net_fn=None):
    """(reason, facts). reason None = admitted to the reservation step. Pure, no I/O except the
    source-buy floor's config reads and, ONLY when a floor refuses the single buy, `prior_net_fn`.
    `amm_pools` / `cpmm_pools` are the PumpSwap and CPMM builders' pool caches; without its cache a
    venue's buy refuses before anything reads its event.

    `prior_net_fn(buy, their_usd)` is the copied trader's earlier net into this coin, or None
    (slot_prior_net on the caller's connection). The source-buy floors judge THE TRADER'S NET, as
    the hook's do (operator 2026-09-13; PACKZ 2026-10-02: DegenCapitalLLC's $91 add-on after $1.0k
    refused "under absolute floor $197"): only ever raised, never for the ceiling."""
    from sniper.services.memecoin_real_runner import (
        _band_source_buy_ratio, _wallet_mcap_ceiling, _wallet_source_buy_ratio, source_buy_family_reason)
    if kill_tripped:
        return "kill_switch", {}
    venue = getattr(buy, "venue", "pump_curve")
    if (venue not in ("pump_curve", "pump_amm", "raydium_cpmm", "raydium_launchlab",
                      "meteora_damm_v2", "meteora_dbc")
            or (venue == "pump_amm" and amm_pools is None)
            or (venue == "raydium_cpmm" and cpmm_pools is None)
            or (venue == "raydium_launchlab" and launchlab_pools is None)
            or (venue == "meteora_damm_v2" and damm_pools is None)):
        return "slot_lane_venue_%s" % venue, {}
    if buy.source_user_id not in armed_ids:
        return "not_approved", {}
    if venue in ("raydium_cpmm", "raydium_launchlab"):
        if venue == "raydium_cpmm":
            from sniper.execution.raydium_cpmm_buy import market_facts
            reason, facts = market_facts(buy, cpmm_pools, sol_usd, quote=venue_quote)
        else:
            from sniper.execution.raydium_launchlab_buy import market_facts
            reason, facts = market_facts(buy, launchlab_pools, sol_usd, quote=venue_quote)
        if reason:
            return reason, facts
        mcap, their_usd = facts["mcap_usd"], facts["their_usd"]
    elif venue == "pump_amm":
        reason, facts = amm_market_facts(buy, amm_pools, sol_usd, quote=venue_quote)
        if reason:
            return reason, facts
        mcap, their_usd = facts["mcap_usd"], facts["their_usd"]
    elif venue == "meteora_damm_v2":
        from sniper.execution.meteora_damm_v2_buy import market_facts as damm_market_facts
        reason, facts = damm_market_facts(buy, damm_pools, sol_usd, quote=venue_quote)
        if reason:
            return reason, facts
        mcap, their_usd = facts["mcap_usd"], facts["their_usd"]
    elif venue == "meteora_dbc":
        from sniper.execution.meteora_dbc_buy import market_facts as dbc_market_facts
        reason, facts = dbc_market_facts(buy, sol_usd, quote=venue_quote)    # facts ride on the buy
        if reason:
            return reason, facts
        mcap, their_usd = facts["mcap_usd"], facts["their_usd"]
    else:
        reason, facts = curve_market_facts(buy, sol_usd, quote=venue_quote)
        if reason:
            return reason, facts
        mcap, their_usd = facts["mcap_usd"], facts["their_usd"]
    routed = getattr(buy, "routed", None)
    if getattr(routed, "requires_receipt_proof", False):
        paid = routed.usdc_spent_atomic
        proof = getattr(buy, "source_initiation", None)
        if type(paid) is int and paid > 0:
            from sniper.services.memecoin_source_initiation import _quantity
            gross = _quantity(proof.get("payment_atomic")) if isinstance(proof, dict) else None
            paid, basis = paid_quote_atomic(paid, gross)
            their_usd = paid / 10**6
        else:
            their_usd, basis = None, "unknown"
        facts.update(their_usd=their_usd, their_usd_basis=basis)
    ceiling = _wallet_mcap_ceiling(
        getattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", None) or {}, "solana",
        buy.source_user_id, float(cfg.MEMECOIN_REAL_MAX_MCAP_USD or 0.0))
    if ceiling and mcap > ceiling:
        return "mcap $%.0f over ceiling $%.0f" % (mcap, ceiling), facts
    ratio = max(0.0, float(cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO or 0.0))
    band_ratios = getattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", None) or {}
    if mcap is not None:
        ratio = _band_source_buy_ratio(band_ratios, "solana", mcap, ratio)
    ratio = _wallet_source_buy_ratio("solana", buy.source_user_id, ratio)
    unknown_floor = max(0.0, float(cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD or 0.0))
    family = dict(
        their_usd=their_usd, source_mcap=mcap, ratio=ratio, unknown_mcap_floor=unknown_floor,
        abs_min=cfg.min_source_buy_usd_for("solana", buy.source_user_id),
        abs_max=max(0.0, float(getattr(cfg, "MEMECOIN_REAL_MAX_SOURCE_BUY_USD", 0.0) or 0.0)),
        family_active=bool(ratio > 0 or unknown_floor > 0 or band_ratios))
    reason, info = source_buy_family_reason(prior_net_usd=None, **family)
    # THE PRIOR NET IS READ ONLY WHEN A FLOOR REFUSED THE SINGLE BUY ("under ..."): a buy that clears
    # on its single size, or one the single-buy ceiling refuses, reads nothing and pays nothing. The
    # net only ever raises what the floors judge, so a buy the single size passes cannot change.
    # A read error or no positive net leaves the single-buy verdict, as before.
    if reason and prior_net_fn is not None and their_usd is not None and " under " in reason:
        probe = buy
        if (getattr(buy, "mint", None) is None and facts.get("mint")
                and dataclasses.is_dataclass(buy)):
            # A PumpSwap buy names only its pool: the read keys on the coin (Gamma 2026-10-07,
            # jpegkite e310b20a: mint None read 0.0 and the lane judged $99 against $1,598).
            probe = dataclasses.replace(buy, mint=facts["mint"])
        try:
            prior = prior_net_fn(probe, their_usd)
        except Exception:
            logger.exception("slot lane prior-net read failed sig=%s (single buy judged)",
                             getattr(buy, "signature", None))
            prior = None
        if prior is not None and prior > 0:
            reason, info = source_buy_family_reason(prior_net_usd=prior, **family)
            info["prior_net_usd"] = prior
    facts["source_buy_floor"] = info
    return reason, facts


def _addon_held(conn, mint):
    """The add-on guard's holding read, on the lane's own connection; only when the ring says refuse."""
    from sniper.services.memecoin_addon_guard import held_in_ledger
    return held_in_ledger(conn, "solana", mint)


_THREAD_RO = threading.local()


def thread_readonly_conn():
    """This thread's read-only ledger handle, opened once (get_readonly_connection: autocommit,
    query_only, the process's DB_PATH) and reused for the thread's life. A dead or closed handle is
    reopened once; if the new one fails too, the error reaches the caller (single buy judged)."""
    conn = getattr(_THREAD_RO, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    from sniper.persistence.db_core import get_readonly_connection
    conn = _THREAD_RO.conn = get_readonly_connection()
    return conn


def slot_prior_net(conn, buy, their_usd):
    """The copied trader's earlier net USD into this coin, the hook's own read (window, both feeds,
    one trade counted once), keyed on this lane's origin so other reports of the same transaction are
    not earlier. None when the window is off or the history is unreadable (the single buy is judged).
    Measured on production data 2026-10-02: p50 0.104 / p90 0.151 ms on the last 45 slot-lane
    attempts, p50 0.059 / p90 0.436 ms on the last 300 Solana detections; read only on a floor
    refusal."""
    window = float(getattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", 0) or 0)
    if window <= 0:
        return None
    return m.source_prior_net_buy_usd(
        conn, source_user_id=buy.source_user_id, network_id="solana", mint=getattr(buy, "mint", None),
        before_iso=getattr(buy, "recv_utc", None), window_s=window,
        exclude_event_id="slotlane:%s" % buy.signature, judged_usd=their_usd)


class SlotLane:
    """One lane instance per runner process. `handle` is called on the lane thread only."""

    def __init__(self, *, conn, executor, build_fn, snapshot_fn, armed_ids_fn,
                 kill_tripped_fn, now_iso_fn, lock_key, head_slot_fn=None, mode="shadow",
                 shadow_clip_fn=None, amm_pools=None, clock=time, handoff=None, leg_scale_fn=None,
                 sol_buffer_cap_usd=0.0, cpmm_pools=None, miss_learner=None, launchlab_pools=None,
                 routed_handoff=None, damm_pools=None, wake=None, exotic_offer=None,
                 pending_legs=None, cash_leg_fn=None, graduation_offer=None, cu_demotion_sink=None,
                 confirmations=None, volume_cache=None):
        self.conn = conn
        self.confirmations = confirmations
        self.volume_cache = volume_cache
        # PERSISTED CU DEMOTION (2026-10-09): `cu_demotion_sink(cls, row)` writes a new demotion's ledger
        # row on its own thread (cu_demotion_writer) so a restart cannot re-arm the class; None = this
        # process only, as before.
        self.cu_demotion_sink = cu_demotion_sink
        # GRADUATION DIRECT: `graduation_offer(buy, trigger=...) -> bool` starts the canonical PumpSwap
        # pool watch for a curve buy that cannot land (True = held, LIVE); None = no watcher.
        self.graduation_offer = graduation_offer
        self.graduation_stats = {"reentered": 0, "aged_out": 0, "sent": 0, "offered_presend": 0,
                                 "offered_revert": 0}
        # EXOTIC QUOTES LIVE: `exotic_offer(buy) -> bool` hands a live exotic-quote buy to the
        # on-demand worker (None = no worker: such a buy refuses as before); `pending_legs` holds its
        # signature until exactly one path resolves it (PendingLegs).
        self.exotic_offer = exotic_offer
        self.cash_leg_fn = cash_leg_fn  # cached matching leg only; no network call
        self.pending_legs = pending_legs if pending_legs is not None else EXOTIC_PENDING
        self.exotic_stats = {"offered": 0, "busy": 0, "reentered": 0, "sent": 0, "aged_out": 0,
                             "expired": 0, "worker_refused": 0, "reentry_age_ms": None}
        # The on-chain lane's wake, or None. Set after every finished live attempt, once its
        # release and terminal row are committed: a detection held while this attempt was in
        # flight is re-checked at once instead of at the lane's next 1 s poll (2026-10-02).
        self.wake = wake
        self.damm_pools = damm_pools          # the DAMM v2 builder's coin -> pool cache, or None
        self.miss_learner = miss_learner      # MissLearner, or None: a pool miss hands off at once
        self.cpmm_pools = cpmm_pools          # the CPMM builder's pool cache, or None (no CPMM)
        self.launchlab_pools = launchlab_pools  # the LaunchLab builder's pool cache, or None
        # RoutedHandoff, or None: a LIVE CPMM buy (no routed row was written for it) refused before
        # any reservation writes its routed row here, so it reaches the on-chain lane once.
        self.routed_handoff = routed_handoff
        self.leg_scale_fn = leg_scale_fn      # -> the regime scale the prefetched legs fit
        self.sol_buffer_cap_usd = float(sol_buffer_cap_usd)
        self.handoff = handoff                # Handoff, or None: refusals are only logged
        self.amm_pools = amm_pools            # the PumpSwap builder's pool cache, or None (curve only)
        # "live" sends funded transactions; anything else is SHADOW: admission and build run and
        # are timed, nothing is reserved, signed or sent. Default shadow, so a knob typo is safe.
        self.mode = str(mode or "shadow").strip().lower()
        self.busy = False  # published by the sole lane thread; collector never executes money
        self.head_slot_fn = head_slot_fn      # -> (processed head slot, monotonic stamp) | None
        self.shadow_clip_fn = shadow_clip_fn  # -> the clip to build with in shadow mode
        self.lock_key = lock_key          # the same "sol-<pubkey>" key serialize_executor uses
        self.executor = executor          # build_real_solana_executor(...) dict
        # Sender A/B (2026-10-01): arms and tips only when the transport holds the Sender provider.
        self.sender_ab = bool(executor.get("sender_ab_ready")) if isinstance(executor, dict) else False
        # Nozomi A/B (2026-10-09): arms only when the transport holds the Nozomi provider.
        self.nozomi_ab = bool(executor.get("nozomi_ab_ready")) if isinstance(executor, dict) else False
        self.entry_tip_fn = executor.get("entry_tip_fn") if isinstance(executor, dict) else None
        self.build_fn = build_fn          # (buy, clip_usd, snapshot) -> (msg, validity) | (None, reason)
        self.snapshot_fn = snapshot_fn
        self.armed_ids_fn = armed_ids_fn
        self.kill_tripped_fn = kill_tripped_fn
        self.now_iso_fn = now_iso_fn
        self.clock = clock
        # THE FIRST LIVE CPMM + LAUNCHLAB SENDS (2026-09-28, Alpha): send times over a sliding hour
        # for the cap, and the canary count per venue. Lane thread only, like everything here.
        import collections
        self._venue_live_sends = collections.defaultdict(collections.deque)   # venue -> send times
        self._canary = {}
        self._rabbit_sigs = collections.OrderedDict()   # signatures a rabbit-led fire reached this lane with
        self.rabbit_backstop = None   # memecoin_rabbit_signal.RabbitBackstop when rabbit buys are wired
        self.crowded = None           # memecoin_crowded_fee.CrowdedFee, set by start_slot_lane

    CANARY_SENDS = 5

    def _venue_live_capped(self, buy):
        """None, or the refusal for a live CPMM/LaunchLab/DAMM v2 buy once ITS VENUE'S cap for this
        hour is spent (MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR, overridden per venue by
        MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE; 0 = none). Each venue counts its own
        sends. Refused before any reservation, so the routed row carries the buy to the on-chain
        lane."""
        venue = getattr(buy, "venue", "pump_curve")
        key = self._cap_key(buy)
        if venue not in SHADOW_ONLY_VENUES and key == venue:
            return None
        cap = venue_live_cap(key)
        sends, now = self._venue_live_sends[key], self.clock.monotonic()
        while sends and now - sends[0] >= 3600.0:
            sends.popleft()
        return "slot_lane_venue_live_cap" if len(sends) >= cap else None

    def exotic_cap_open(self, venue):
        """The worker's pre-leg check, from ITS thread: counts this hour's exotic sends without
        touching the deque (the lane thread alone pops it). An unreadable count says open; the lane's
        own check before the reservation is the one that refuses."""
        key, now = exotic_cap_key(venue), self.clock.monotonic()
        try:
            sent = sum(1 for t in list(self._venue_live_sends.get(key) or ()) if now - t < 3600.0)
        except RuntimeError:                  # the deque changed under the copy
            return True
        return sent < venue_live_cap(key)

    @staticmethod
    def _cap_key(buy):
        """The cap and canary key: "<venue>:exotic" for a buy back from the worker, and for a pump AMM
        buy on a pool quoted in anything but SOL (USDC included: a new funding shape); else the venue."""
        from sniper.execution.pump_curve_buy import SOL_QUOTE_MINTS
        venue = getattr(buy, "venue", "pump_curve")
        if (getattr(buy, "graduation", None) or {}).get("cash_leg_repair") is True:
            venue = "pump_amm"
        if getattr(buy, "ondemand", None) is not None:
            return exotic_cap_key(venue)
        if (venue in ("pump_amm", "meteora_damm_v2", "pump_curve", "meteora_dbc")
                and getattr(buy, "quote_mint", None) not in (None, *SOL_QUOTE_MINTS)):
            return exotic_cap_key(venue)
        return venue

    def _canary_line(self, buy, detail, result):
        """One line for each of the first CANARY_SENDS live sends per new venue: what we expected,
        the floor we signed, where it landed and what happened. An exotic-quote send counts under
        its own key, so its first five get lines too."""
        venue = self._cap_key(buy)
        n = self._canary.get(venue, 0) + 1
        if n > self.CANARY_SENDS:
            return
        self._canary[venue] = n
        detail = detail if isinstance(detail, dict) else {}
        landing = result.get("landing_slot")
        logger.warning(
            "[memecoin] slot lane LIVE CANARY %s %d/%d %s %s S=%s exp=%s min=%s landing=%s "
            "land-S=%s outcome=%s/%s sig=%s at=%s", venue, n, self.CANARY_SENDS,
            getattr(buy, "handle", "?"), str(getattr(buy, "mint", "?"))[:8], buy.slot,
            detail.get("expected_tokens_out"), detail.get("min_tokens_out"), landing,
            (landing - int(buy.slot)) if isinstance(landing, int) else None,
            result.get("status"), result.get("reason"), result.get("sig"),
            datetime.now(timezone.utc).isoformat())

    def handle(self, buy, *, enqueued_mono=None):
        from sniper.services.memecoin_entry_trace import _read_cpu
        decision = self.clock.monotonic()
        self._pretrace_started = self._pretrace_mark = decision
        self._pretrace_cpu = _read_cpu(self.clock)
        self._queue_timing = {
            "timing_basis": "monotonic",
            "pretrace_phase_ms": {},
            "decode_ms": monotonic_ms(getattr(buy, "decoded_monotonic", None),
                                       getattr(buy, "recv_monotonic", None)),
            "receipt_to_enqueue_ms": monotonic_ms(enqueued_mono, getattr(buy, "recv_monotonic", None)),
            "queue_ms": monotonic_ms(decision, enqueued_mono),
            "receipt_to_decision_ms": monotonic_ms(decision, getattr(buy, "recv_monotonic", None)),
            "receipt_to_proof_ms": monotonic_ms(getattr(buy, "proof_ready_monotonic", None),
                                                getattr(buy, "recv_monotonic", None)),
            "proof_to_decision_ms": monotonic_ms(decision, getattr(buy, "proof_ready_monotonic", None)),
        }
        self._dispatched = None
        buy = with_processed_routed_proof(buy)
        self._pretrace_phase("source_proof")
        try:
            result = self._handle_buy(buy)
        except Exception:
            if self._dispatched is None:
                raise
            logger.exception('slot lane post-dispatch wrapper failed pending=%s',
                             self._dispatched['pending_id'])
            result = self._dispatched  # already sent: still enqueue its receipt worker
        if enqueued_mono is not None:
            result["queue_timing"] = dict(self._queue_timing)
        return result

    def _pretrace_phase(self, name):
        if not hasattr(self, "_pretrace_mark"):
            return None
        now = self.clock.monotonic()
        self._queue_timing["pretrace_phase_ms"][name] = monotonic_ms(now, self._pretrace_mark)
        self._pretrace_mark = now
        return now

    def _handle_buy(self, buy):
        """Admit, reserve, build, send and book one source buy. Returns the result dict.

        THE RETRY PASS of a learned pool miss (MissLearner) hands off EVERY outcome but a fill or
        a shadow build, whatever the reason (signal age, stale input, a refusal, a failed or
        unknown send): the learn delay makes those likelier, and a buy neither the lane nor the
        hand-off takes is a lost fill. The on-chain lane still defers to this lane's own attempt
        on the signature (slot_lane_outcome), so a hand-off after an attempt cannot double it."""
        if is_rabbit(buy):
            self._rabbit_sigs[buy.signature] = None
            while len(self._rabbit_sigs) > 4096:
                self._rabbit_sigs.popitem(last=False)
        elif buy.signature in self._rabbit_sigs:
            # THE PROCESSED COPY OF A RABBIT-LED BUY: dropped only when that fire still owns the
            # signature (a reservation, a confirmed fill or a send); a fire refused before any of
            # those leaves this buy its ordinary path. One indexed read, for these buys only.
            from sniper.services.memecoin_onchain_lane import slot_funded_owner
            if slot_funded_owner(self.conn, buy.signature):
                return {"status": "rejected", "reason": RABBIT_DUPLICATE_REASON}
        if getattr(buy, "graduation", None) is not None and not self._graduation_leg_back(buy):
            result = self._handle_graduation(buy)
        elif getattr(buy, "ondemand", None) is not None:
            result = self._handle_requeued(buy)
        else:
            retry = self.miss_learner is not None and self.miss_learner.is_retry(buy.signature)
            result = self._handle(buy, hand_off=not retry)
            self._routed_fallback(buy, result)
            if (retry and result.get("status") not in ("filled", "shadow")
                    and self.handoff is not None and self.mode == "live"
                    and result.get("reason") != VOLUME_REFUSAL
                    and not _entry_opportunity_refusal(result.get('reason'))):
                self.handoff.offer(buy, "retry:%s" % (result.get("reason") or result.get("status")),
                                   self.snapshot_fn().sol_usd)
        # Lane-own PumpSwap misses have no attempt row or routed decision. Record their actual
        # result in the existing off-thread timing drain, before an ordinary fill can alert.
        if result.get('reason') == EXOTIC_AGED_OUT_REASON:
            previous = _skip_decision(buy.signature)
            if (previous and previous[0] not in (None, 'lane', 'handoff', 'pair_expired', 'metadata_invalid')
                    and previous[1] != EXOTIC_PENDING_REASON):
                return result  # a late unowned requeue cannot erase the owning path's result
        _skip_note(buy.signature, result.get('status'), result.get('reason'),
                   attempt_id=result.get('attempt_id'), evidence=_receipt_facts(buy, result))
        return result

    def _graduation_leg_back(self, buy):
        """A graduation's newly requested leg is owned by PendingLegs after the pool hold ends."""
        from sniper.execution.pumpswap_buy import WSOL_MINT
        from sniper.services.memecoin_slot_graduation import quote_curves_on
        grad, od = buy.graduation or {}, getattr(buy, "ondemand", None)
        pool_quote = (grad.get("pool_facts") or {}).get("quote_mint")
        if od is None or pool_quote is None:
            return False
        now = self.clock.monotonic()
        if pool_quote == WSOL_MINT:
            deadline = grad.get("cash_leg_deadline")
            # Older curve legs unwrap SOL. Only this unsigned reservation repair requests wrapped
            # SOL for the new pool, and it cannot renew the original pool snapshot's 500 ms window.
            eligible = (grad.get("cash_leg_repair") is True
                        and type(deadline) in (int, float) and math.isfinite(deadline)
                        and now < deadline and od.get("quote_mint") == WSOL_MINT
                        and (od.get("leg") or {}).get("output_mint") == WSOL_MINT)
        else:
            eligible = quote_curves_on()
        return eligible and self.pending_legs.holds(buy.signature, now=now)

    def _handle_requeued(self, buy):
        """A live exotic-quote buy back from the worker with its leg: the ordinary live path, then
        the release of its PendingLegs entry (after the attempt row exists, so the on-chain lane
        never sees a gap) and the on-chain lane's wake. An entry no longer held already had its one
        fallback (the expiry sweep or the worker's refusal): nothing is sent."""
        now = self.clock.monotonic()
        # A RECEIPT RESUME (2026-10-07, ResellCalendar GLsHzxjt x2, jpegkite: SPCX-quoted routed
        # PumpSwap buys) is owned by CONFIRMED_PENDING, which the on-chain lane defers to; its
        # PendingLegs hold ended with the first pass's missing_receipt_proof. Its resized leg is
        # owned by the hold _exotic_handoff moved it to. A requeue runs only while one of the two
        # owns it (Codex F-SOL-1): an expired hold already had its one fallback, so a late resized
        # leg must not send. The signal-age cap in _handle and the leg's age cap still apply.
        resumed = getattr(buy, "source_initiation", None) is not None
        if (not self.pending_legs.holds(buy.signature, now=now)
                and not (resumed and CONFIRMED_PENDING.holds(buy.signature, now=now))):
            self.exotic_stats["aged_out"] += 1
            return {"status": "rejected", "reason": EXOTIC_AGED_OUT_REASON}
        self.exotic_stats["reentered"] += 1
        od = buy.ondemand
        age_ms = (now - float(getattr(buy, "recv_monotonic", now) or now)) * 1000.0
        self.exotic_stats["reentry_age_ms"] = round(age_ms, 1)
        result = {}
        try:
            result = self._handle(buy, hand_off=False)
            self._routed_fallback(buy, result)
        except Exception:
            # Gamma LOW-1: an exception before any reservation still gives the buy its one path.
            self._exotic_fallback(buy)
            raise
        finally:
            # A policy refusal on the lane's own re-admission (state changed after the worker's:
            # the kill switch, the roster, a sol_usd tick over the ceiling, the prior net; Gamma LOW
            # on 1d1a8a515) comes before any attempt row, so its release carries the policy kind.
            how = "policy" if policy_refusal(result.get("reason")) else "claimed"
            if (not result.get("exotic_pending")
                    and self.pending_legs.claim(buy.signature, now=self.clock.monotonic(), how=how) is not None
                    and self.wake is not None):
                self.wake.set()
        if result.get("pending_id") is not None:
            self.exotic_stats["sent"] += 1
        result["exotic"] = {
            "venue": getattr(buy, "venue", None), "age_ms": round(age_ms, 1),
            "queue_ms": (round((now - od["requeued_mono"]) * 1000.0, 1)
                         if od.get("requeued_mono") is not None else None),
            "leg_ms": od.get("leg_ms"), "quote": str(od.get("quote_mint") or "?")[:8],
            "warm_leg": bool(od.get("warm_leg"))}
        return result

    def _handle_graduation(self, buy):
        """A curve buy back from the graduation watcher with its canonical PumpSwap pool: the ordinary
        live path (re-admission on the source's curve trade, reservation, wallet lock, sizing) built
        on the pool, then the release of its hold and the on-chain lane's wake. A hold that already
        ran out had its fallback: nothing is sent."""
        from sniper.services.memecoin_slot_graduation import AGED_OUT_REASON
        if not GRADUATION_PENDING.holds(buy.signature, now=self.clock.monotonic()):
            self.graduation_stats["aged_out"] += 1
            return {"status": "rejected", "reason": AGED_OUT_REASON}
        self.graduation_stats["reentered"] += 1
        result = {}
        try:
            result = self._handle(buy, hand_off=False)
        finally:
            how = "policy" if policy_refusal(result.get("reason")) else "claimed"
            if GRADUATION_PENDING.claim(buy.signature, now=self.clock.monotonic(), how=how) is not None and self.wake is not None:
                self.wake.set()
        if result.get("pending_id") is not None:
            self.graduation_stats["sent"] += 1
        g = buy.graduation
        result["graduation"] = {"trigger": g.get("trigger"), "pool": str(g.get("pool"))[:8],
                                "watch_ms": g.get("watch_ms"), "read_ms": g.get("read_ms")}
        return result

    def _offer_graduation(self, buy, trigger, quote_mint=None):
        """True when the graduation watcher took this curve buy LIVE (held). `quote_mint` (a quote-token
        curve, MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES=live only) watches coin/<quote>; the buy goes
        without its leg, so the hand-back quotes a fresh one."""
        from sniper.services.memecoin_slot_graduation import quote_curves_on
        if (self.graduation_offer is None or getattr(buy, "graduation", None) is not None
                or getattr(buy, "venue", None) != "pump_curve"):
            return False
        if quote_mint is None and getattr(buy, "quote_mint", None) is not None:
            return False
        if quote_mint is not None and not quote_curves_on():
            return False
        try:
            if quote_mint is None:
                taken = bool(self.graduation_offer(buy, trigger=trigger))
            else:
                plain = (dataclasses.replace(buy, ondemand=None)
                         if getattr(buy, "ondemand", None) is not None else buy)
                taken = bool(self.graduation_offer(plain, trigger=trigger, quote_mint=quote_mint))
        except Exception:
            logger.exception("slot lane graduation offer failed sig=%s", buy.signature)
            return False
        self.graduation_stats["offered_presend" if trigger == "source_completed" else "offered_revert"] += 1
        if quote_mint is not None:
            self.graduation_stats["offered_quote"] = self.graduation_stats.get("offered_quote", 0) + 1
        return taken

    def _quote_curve_graduation(self, buy, quote_mint):
        """THE SOURCE COMPLETED A QUOTE-TOKEN CURVE (HI EtVFMDoB, 2026-10-07): its own TradeEvent leaves
        no real tokens, so a curve send can only fail. Watch coin/<quote> before any leg is spent. Its
        policy checks so far are the kill switch, the venue and the roster; the price-dependent ones
        (ceiling, source-buy floors) run on the hand-back, with the fresh leg's quote. Lane-own first
        passes only, as the wSOL trigger; None leaves the buy to the exotic path as before."""
        from sniper.services.memecoin_slot_graduation import PENDING_REASON, quote_curve_completed, quote_curves_on
        if (not quote_curves_on() or self.mode != "live" or shadow_only(buy)
                or getattr(buy, "graduation", None) is not None or getattr(buy, "routed", None) is not None
                or getattr(buy, "ondemand", None) is not None
                or getattr(buy, "source_initiation", None) is not None
                or quote_curve_completed(buy) != quote_mint):
            return None
        if not self._offer_graduation(buy, "source_completed", quote_mint=quote_mint):
            return None
        return {"status": "rejected", "reason": PENDING_REASON, "graduation_pending": True,
                "quote_mint": quote_mint}

    def _exotic_fallback(self, buy):
        """The one path of an exotic buy the lane will not send: its routed row (a routed buy), and
        in every case the on-chain lane's wake, so a detection it deferred is judged at once. A
        lane-own buy has no routed row: the watcher's own row is its path."""
        routed = getattr(buy, "routed", None)
        if routed is not None and self.routed_handoff is not None:
            try:
                self.routed_handoff.write(routed)
            except Exception:
                logger.exception("slot lane exotic fallback write failed sig=%s", buy.signature)
        if self.wake is not None:
            self.wake.set()

    def resolve_exotic_refusal(self, refusal):
        """Lane thread: a worker refusal, if its entry is still held (else it already had its path).
        A policy refusal releases as "policy", which slot_lane_outcome gives no fallback class."""
        buy = self.pending_legs.claim(refusal.buy.signature,
                                      now=self.clock.monotonic(),
                                      how="policy" if policy_refusal(refusal.reason) else "claimed")
        if buy is None:
            return False
        self.exotic_stats["worker_refused"] += 1
        _skip_note(buy.signature, "rejected", refusal.reason)
        self._exotic_fallback(buy)
        return True

    def _expired_exotic_fallback(self, buy):
        _skip_note(buy.signature, "rejected", EXOTIC_AGED_OUT_REASON)
        self._exotic_fallback(buy)

    def expire_exotic(self, now):
        """Lane thread, between queue items: legs past their deadline get their fallback."""
        n = sweep_expired_legs(self.pending_legs, now, self._expired_exotic_fallback)
        self.exotic_stats["expired"] += n
        return n

    def _exotic_handoff(self, buy, reason, *, clip_usd=None, trial=None):
        """A live exotic-quote refusal of the quote alone: offer the buy to the worker and hold its
        signature. A refused offer (worker full, none running) leaves the refusal as it is, so the
        routed row is written at once (Gamma M1)."""
        venue = getattr(buy, "venue", None)
        od = getattr(buy, "ondemand", None)
        # ONE resize per buy (2026-10-07, jpegkite DVypDG8A x2): the first leg is sized before the
        # coin is classified (roster clip, wallet cap), so a coin scaled below it needs a second.
        # Gating on "no requested clip" blocked every wallet-capped buy, whose first leg already
        # names one, and the lane refused slot_lane_scaled_clip with the worker idle.
        resizing = (reason in (SCALED_CLIP_REASON, TRIAL_UNMARKED) and od is not None
                    and not od.get("resized"))
        if (reason not in (*ONDEMAND_QUOTE_REASONS, SCALED_CLIP_REASON, TRIAL_UNMARKED)
                or self.mode != "live" or self.exotic_offer is None
                or (od is not None and not resizing) or not exotic_live(venue)):
            return None
        if clip_usd is None and self.shadow_clip_fn is not None:
            from sniper.services.memecoin_wallet_caps import entry_cap_usd
            from sniper.services.memecoin_risk import solana_flat_clip_ceiling_usd
            cap = entry_cap_usd(buy.source_user_id, "solana", ceiling_usd=solana_flat_clip_ceiling_usd())
            guess = self.shadow_clip_fn()
            if cap is not None and guess is not None:
                clip_usd = min(float(guess), cap)
        recv = float(getattr(buy, "recv_monotonic", 0.0) or 0.0)
        if resizing:
            # One resized leg; keep the original deadline and watcher hold across both quotes.
            now = self.clock.monotonic()
            if not self.pending_legs.holds(buy.signature, now=now):
                # A receipt resume (Codex F-SOL-1): its first leg's hold ended with the first pass, and
                # CONFIRMED_PENDING owns it until the lane loop's finish(). Move ownership to a new hold
                # on the ORIGINAL deadline before the offer, so the on-chain lane keeps deferring
                # through the second quote; refusal and expiry resolve it on the existing path.
                if (getattr(buy, "source_initiation", None) is None
                        or not CONFIRMED_PENDING.holds(buy.signature, now=now)
                        or now >= recv + MAX_SIGNAL_AGE_S
                        or not self.pending_legs.add(buy, deadline=recv + MAX_SIGNAL_AGE_S)):
                    return None
        elif not self.pending_legs.add(buy, deadline=(
                recv + MAX_SIGNAL_AGE_S if getattr(buy, "graduation", None) is None
                # A graduation hand-back is seconds past its receipt by design: its leg is held from
                # now, added before _handle_graduation releases the pool hold (no gap).
                else self.clock.monotonic() + GRADUATION_LEG_HOLD_S)):
            return None
        try:
            taken = bool(self.exotic_offer(buy, **({"clip_usd": clip_usd}
                                                   if clip_usd is not None else {}),
                                           **({"trial": trial} if trial is not None else {})))
        except Exception:
            logger.exception("slot lane exotic offer failed sig=%s", buy.signature)
            taken = False
        if not taken:
            self.pending_legs.claim(buy.signature, now=self.clock.monotonic())
            self.exotic_stats["busy"] += 1
            return None
        self.exotic_stats["offered"] += 1
        return {"status": "rejected", "reason": EXOTIC_PENDING_REASON, "exotic_pending": True}

    def _graduation_cash_repair(self, buy, clip, detail, pending, trace):
        """One off-thread wrapped-SOL quote after an unsigned migrated-pool leg mismatch.

        Own the signature before releasing either the reservation or the graduation hold. The
        worker uses the actual approved USDC clip; re-entry repeats every gate and reservation.
        No native cash-history retirement, snapshot renewal, or entry-path RPC is permitted here.
        """
        from sniper.execution.pumpswap_buy import TOKEN_PROGRAM, WSOL_MINT
        grad = getattr(buy, "graduation", None)
        if (detail not in ("leg_over_clip", "leg_under_clip") or not grad
                or grad.get("cash_leg_repair") or getattr(buy, "ondemand", None) is not None
                or self.mode != "live" or self.exotic_offer is None
                or not exotic_live("pump_amm") or pending.get("tx_sig") is not None
                or pending.get("status") != "reserved"
                or pending.get("signed_payload_sha256") is not None
                or pending.get("signed_payload_b64") is not None
                or pending.get("broadcast_at") is not None
                or pending.get("submission_bound_at") is not None
                or pending.get("funding_asset") != "usdc"):
            return None
        pool = grad.get("pool_facts") or {}
        if (pool.get("quote_mint") != WSOL_MINT or pool.get("quote_token_program") != TOKEN_PROGRAM
                or pool.get("quote_decimals", 9) != 9):
            return None
        now, read_at = self.clock.monotonic(), grad.get("requeued_mono")
        left = GRADUATION_PENDING.hold_left_ms(buy.signature, now)
        if (type(read_at) not in (int, float) or not math.isfinite(read_at)
                or not 0 <= now - read_at < MAX_SIGNAL_AGE_S
                or left is None or left <= 0 or not math.isfinite(clip) or clip <= 0):
            return None
        deadline = min(read_at + MAX_SIGNAL_AGE_S, now + left / 1000.0)
        candidate = dataclasses.replace(buy, graduation={**grad, "cash_leg_repair": True,
                                                        "cash_leg_deadline": deadline})
        if not self.pending_legs.add(candidate, deadline=deadline):
            return None
        try:
            taken = bool(self.exotic_offer(candidate, clip_usd=clip))
        except Exception:
            logger.exception("slot lane graduation cash quote offer failed sig=%s", buy.signature)
            taken = False
        if not taken:
            self.pending_legs.claim(buy.signature, now=self.clock.monotonic())
            self.exotic_stats["busy"] += 1
            return None
        m.release_pending(self.conn, pending["id"], "slot_lane_build:%s" % detail)
        self.exotic_stats["offered"] += 1
        trace.mark_substage("slot_graduation_cash_quote", reason=detail, clip_usd=clip,
                            deadline_left_ms=round((deadline - now) * 1000.0, 1))
        return {"status": "rejected", "reason": EXOTIC_PENDING_REASON, "exotic_pending": True}

    def _routed_fallback(self, buy, result):
        """A refusal before reservation leaves a routed buy to the confirmed watcher.

        From reservation onward the attempt row decides whether another attempt is safe.
        A pool miss the MissLearner took is its to settle. Inert for every other buy.
        """
        routed = getattr(buy, "routed", None)
        if (routed is None or self.routed_handoff is None or self.mode != "live" or shadow_only(buy)
                or result.get("reason") == VOLUME_REFUSAL
                or _entry_opportunity_refusal(result.get('reason'))
                or result.get("exotic_pending") or result.get("graduation_pending")
                or result.get("pending_id") is not None or result.get("status") in ("filled", "shadow")
                or result.get("reason") == "pool_unknown_learning"):
            return
        try:
            result["routed_fallback"] = self.routed_handoff.write(routed)
        except Exception:
            logger.exception("slot lane routed fallback failed sig=%s", buy.signature)

    def _entry_opportunity(self, buy, *, mint=None):
        if (not entry_policy.enabled() or self.mode != 'live' or shadow_only(buy)
                or buy.source_user_id not in self.armed_ids_fn()):
            return None, None
        routed = getattr(buy, 'routed', None)
        mint = mint or getattr(buy, 'mint', None) or getattr(routed, 'mint', None)
        if mint is None and getattr(buy, 'venue', None) == 'pump_amm':
            cached = getattr(self.amm_pools, 'cached', None)
            pool = (cached(getattr(buy, 'pool', None)) if cached is not None else
                    self.amm_pools.get(getattr(buy, 'pool', None))
                    if isinstance(self.amm_pools, Mapping) else None)
            mint = (pool or {}).get('base_mint')
        from sniper.services.memecoin_source_initiation import source_initiation_refusal
        source = SimpleNamespace(
            signal_source=SIGNAL_SOURCE, fomo_event_id='slotlane:' + buy.signature,
            source_user_id=buy.source_user_id, source_wallet=buy.owner,
            source_block_number=buy.slot, mint=mint, source_initiation=_source_initiation(buy, mint),
            source_timestamp_trust=proof_lane(buy)[1] if is_rabbit(buy) else 'observed_detection')
        if source_initiation_refusal(source, 'solana') is not None:
            return None, None  # Existing proof/receipt handoff retains ownership of unproven routes.
        return entry_policy.evaluate(self.conn, source_user_id=buy.source_user_id, mint=mint,
            source_signature=buy.signature, source_slot=buy.slot,
            source_pre_atomic=_source_pre_atomic(buy),
            observed_at=self.now_iso_fn(), head_fn=self.head_slot_fn or entry_policy.head,
            clock=self.clock.monotonic)

    def _handle(self, buy, hand_off=True):
        self._pretrace_phase("routing")
        window, reason = self._entry_opportunity(buy)
        self._pretrace_phase("entry_opportunity")
        if reason:
            return {'status': 'rejected', 'reason': reason}
        refusal = getattr(buy, "refusal", None)
        if refusal:
            # The decoder could not read this frame as one buy (two own buy events): reject it here
            # so it is a decision line like any other, never a silent drop. No read, no write.
            from sniper.services.memecoin_slot_signal import CHAIN_REFUSAL
            if refusal == CHAIN_REFUSAL:
                refusal = chain_refusal(getattr(buy, "legs", None), self.amm_pools)
            return {"status": "rejected", "reason": refusal}
        t0 = self.clock.perf_counter()
        # The first-opportunity claim above survives selection misses, including stale input.
        # Legacy callers still refuse age before any ledger write.
        waited = self.clock.monotonic() - float(getattr(buy, "recv_monotonic", 0.0) or 0.0)
        # A graduation buy is late by design (the pool did not exist yet): its hold is its deadline,
        # and the caps against the source's curve price judge the landing.
        # A re-quoted buy is late by design too: its hold (REQUOTE_HOLD_S) is its deadline.
        if waited > MAX_SIGNAL_AGE_S and getattr(buy, "graduation", None) is None and not requote_attempts(buy):
            return {"status": "rejected", "reason": "slot_lane_signal_too_old",
                    "waited_ms": waited * 1000.0}
        snap = self.snapshot_fn()
        stale = stale_input(snap, self.clock.monotonic())
        if stale:
            return {"status": "rejected", "reason": "slot_lane_stale_%s" % stale}
        reason, facts = admission_refusal(
            buy, armed_ids=self.armed_ids_fn(), sol_usd=snap.sol_usd,
            kill_tripped=self.kill_tripped_fn(), amm_pools=self.amm_pools,
            cpmm_pools=self.cpmm_pools, launchlab_pools=self.launchlab_pools,
            damm_pools=self.damm_pools,
            venue_quote=(tuple(buy.ondemand["quote"][:2])
                         if getattr(buy, "ondemand", None) is not None else None),
            prior_net_fn=lambda b, usd: slot_prior_net(self.conn, b, usd))
        if not reason and self.volume_cache is not None:
            volume = cached_volume_decision(self.volume_cache, buy, facts, snap)
            if volume is not None and volume.state == "refuse":
                facts = dict(facts, solana_volume=dict(state=volume.state, reason=volume.reason,
                    volume_usd=volume.volume_usd, required_usd=volume.required_usd))
                reason = VOLUME_REFUSAL
        self._pretrace_phase("admission")
        if window is not None:
            facts = dict(facts, entry_opportunity=window)
        # THE ADD-ON RING (memecoin_addon_guard): judged in _traced (before any reservation or build),
        # then this buy is noted, in memory, whatever its verdict; only a priced buy is noted.
        from sniper.services.memecoin_addon_guard import RING as _addon_ring, judge as _addon_judge
        addon = (_addon_judge("solana", buy.source_user_id, facts.get("mint"), buy.signature, facts.get("their_usd"),
                              held_fn=lambda: _addon_held(self.conn, facts.get("mint")))
                 if not reason and facts.get("mint") else None)
        _addon_ring.note_buy("solana", buy.source_user_id, facts.get("mint"), buy.signature,
                             facts.get("their_usd"), facts.get("price_usd"))
        if addon is not None:
            facts = dict(facts, addon_refusal=addon)
        self._pretrace_phase("addon")
        if reason:
            if reason == "slot_lane_curve_quote_not_supported" and facts.get("quote_mint"):
                held = self._quote_curve_graduation(buy, facts["quote_mint"])
                if held is not None:
                    return held
            pending = self._exotic_handoff(buy, reason)
            if pending is not None:
                return pending
            # Refusals before any write stay out of the ledger: the J7 lane sees the same buy a
            # second later and records its own verdict, so a row here would double-count it.
            # A buy refused only because this lane cannot price it yet is handed to the on-chain
            # lane instead, which judges it like any detection (J7 does not see every wallet).
            # The learner owns only the venues it has a pool cache for (offer() says no otherwise).
            # A rabbit-led buy is never learned or handed off: the hand-off books an observed
            # detection, and the processed copy of the same buy follows on its own.
            if (reason == "pool_unknown" and self.miss_learner is not None and not is_rabbit(buy)
                    and self.miss_learner.offer(buy)):
                return {"status": "rejected", "reason": "pool_unknown_learning", **facts}
            if (hand_off and reason in HANDOFF_REASONS and self.handoff is not None
                    and self.mode == "live" and not is_rabbit(buy)):
                self.handoff.offer(buy, reason, snap.sol_usd)
            return {"status": "rejected", "reason": reason, **facts}
        if (getattr(buy, "venue", None) in ("pump_amm", "pump_curve") and facts.get("quote_mint")
                and getattr(buy, "quote_mint", None) is None and dataclasses.is_dataclass(buy)):
            # A lane-own PumpSwap buy names only its pool, a quote-token curve buy only its coin: the
            # quote decides the cap key and that it can never be paid in native SOL (E2).
            buy = dataclasses.replace(buy, quote_mint=facts["quote_mint"])
        # THE SOURCE COMPLETED THE CURVE (its own TradeEvent leaves no real tokens): a curve send can
        # only revert BondingCurveComplete, so a lane-own buy goes straight to the pool watch, after
        # admission (a policy refusal never starts one).
        from sniper.services.memecoin_slot_graduation import PENDING_REASON, curve_completed
        if (self.mode == "live" and getattr(buy, "routed", None) is None
                and getattr(buy, "ondemand", None) is None
                and getattr(buy, "source_initiation", None) is None and curve_completed(buy)
                and self._offer_graduation(buy, "source_completed")):
            return {"status": "rejected", "reason": PENDING_REASON, "graduation_pending": True, **facts}
        if self.mode != "live" or shadow_only(buy):
            return dict(self._shadow(buy, snap, facts, t0), signal_age_ms=waited * 1000.0)
        capped = self._venue_live_capped(buy)
        if capped:
            return {"status": "rejected", "reason": capped}
        if self.confirmations is not None and not self.confirmations.available():
            return {"status": "rejected", "reason": "slot_lane_confirmation_busy"}
        # Serialize admission and durable dispatch. Receipt-first accounting permits confirmation
        # to finish separately without attributing another entry or exit to this transaction.
        from sniper.execution.exec_lock import wallet_exec_lock
        ownership_started = self.clock.perf_counter()
        with wallet_exec_lock(self.lock_key, blocking=False) as held:
            self._queue_timing["wallet_ownership_ms"] = (self.clock.perf_counter() - ownership_started) * 1000
            if not held:
                return {"status": "rejected", "reason": "slot_lane_wallet_busy"}
            if m.unresolved_withdraws(self.conn, "solana"):
                return {"status": "rejected", "reason": "withdrawal_unresolved"}
            self._pretrace_phase("withdrawal")
            self.busy = True
            try:
                result = self._traced(buy, snap, facts, t0)
                if result.get('_settlement') is not None:
                    self.confirmations.register(result)
                    self._dispatched = result
                return result
            finally:
                self.busy = False

    MAX_HEAD_AGE_S = 1.0

    def _boundary(self, buy):
        """Head slot, its age and the signal's age right now: recorded, never a refusal."""
        now = self.clock.monotonic()
        head, head_age = None, None
        try:
            head, seen = (self.head_slot_fn() if self.head_slot_fn else None) or (None, None)
            head = int(head) if head is not None else None
            head_age = now - float(seen) if seen is not None else None
        except Exception:
            head, head_age = None, None
        return {"source_slot": int(buy.slot),
                "head_slot": head, "head_age_ms": head_age * 1000 if head_age is not None else None,
                "head_fresh": head is not None and head_age is not None and head_age <= self.MAX_HEAD_AGE_S,
                "signal_age_ms": (now - float(getattr(buy, "recv_monotonic", 0.0) or 0.0)) * 1000,
                "receipt_to_dispatch_ms": monotonic_ms(now, getattr(buy, "recv_monotonic", None)),
                "proof_to_dispatch_ms": monotonic_ms(now, getattr(buy, "proof_ready_monotonic", None))}

    def _shadow(self, buy, snap, facts, t0):
        """Build exactly what live would send, time it, and send nothing."""
        clip = self.shadow_clip_fn() if self.shadow_clip_fn else None
        if not clip:
            return {"status": "shadow", "reason": "no_clip", **facts}
        built, detail = self.build_fn(buy, float(clip), snap)
        head = self.head_slot_fn() if self.head_slot_fn else None
        reason = "built" if built is not None else str(detail)
        extra = {}
        if built is not None and isinstance(detail, dict) and detail.get("expected_tokens_out"):
            reason += " exp=%s min=%s" % (detail["expected_tokens_out"], detail.get("min_tokens_out"))
            extra = {k: detail.get(k) for k in ("expected_tokens_out", "min_tokens_out",
                                                "event_reserve_in", "event_reserve_out", "input_is_0",
                                                "unsigned_b64", "expected_gross")}
        elif built is not None and isinstance(detail, dict) and detail.get("unsigned_b64"):
            # A build with no expected output (DAMM v2: its minimum is the price floor) still hands
            # its bytes and floor to the shadow verifier.
            reason += " min=%s" % detail.get("min_tokens_out")
            extra = {k: detail.get(k) for k in ("min_tokens_out", "unsigned_b64")}
        return {"status": "shadow", "reason": reason, **extra,
                "build_ms": (self.clock.perf_counter() - t0) * 1000.0,
                "head_minus_s": (int(head[0]) - int(buy.slot)) if head and head[0] else None,
                **facts}

    def _traced(self, buy, snap, facts, t0):
        from sniper.services.memecoin_entry_trace import EntryAttemptTrace, _read_cpu, _cpu_window
        now_iso = self.now_iso_fn()
        observed_mono = self.clock.monotonic()
        received_iso = (datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
                        - timedelta(seconds=observed_mono - buy.recv_monotonic)).isoformat()
        price_age = observed_mono - snap.observed_mono["sol_usd"]
        source_evidence = {
            "their_size_usd": facts["their_usd"], "source_user_id": buy.source_user_id,
            "network_id": "solana", "mint": facts["mint"], "signature": buy.signature,
            "pool": facts.get("pool"),
            "source_sig": buy.signature, "source_wallet": buy.owner,
            "source_pre_atomic": _source_pre_atomic(buy),
            "source_slot": int(buy.slot), "source_slot_provenance": "processed_context_slot",
            "source_timestamp_trust": "observed_detection",
            "valuation_basis": "swap_net_sol_input_x_cached_sol_usd", "sol_usd": snap.sol_usd,
            "valuation_observed_at": (datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
                                      - timedelta(seconds=price_age)).isoformat(),
            "valuation_age_ms": price_age * 1000,
        }
        source_evidence.update(_receipt_provenance(buy))
        if facts.get('entry_opportunity') is not None:
            source_evidence['entry_opportunity'] = facts['entry_opportunity']
        initiation = _source_initiation(buy, facts['mint'])
        confirmed = getattr(buy, 'source_initiation', None) is not None
        proof_delivery, proof_provenance = proof_lane(buy)
        if confirmed:
            source_evidence['source_slot_provenance'] = proof_provenance
        source_evidence['source_initiation'] = initiation
        rabbit = getattr(buy, "rabbit", None)
        trust = "observed_detection"
        if rabbit is not None:
            # PRE-EXECUTION (memecoin_rabbit_signal): the source's buy as it is expected to execute,
            # priced from our own read at signal, never an observed fill.
            trust = proof_provenance
            source_evidence.update(source_timestamp_trust=trust, valuation_basis=rabbit["valuation_basis"],
                                   rabbit=dict(rabbit))
        ev = SimpleNamespace(
            fomo_event_id="slotlane:%s" % buy.signature, signal_source=SIGNAL_SOURCE,
            network_id="solana", mint=facts["mint"], source_user_id=buy.source_user_id,
            source_handle=getattr(buy, "handle", None), ticker=None, side="buy",
            created_at=received_iso, their_price=facts["price_usd"],
            their_market_cap=facts["mcap_usd"], received_at_normalized=received_iso,
            clock_domain="observed_real_utc", delivery_lane=(proof_delivery if confirmed
                                                            else "slot_lane_processed"),
            source_timestamp_trust=trust,
            their_size_usd=facts["their_usd"], source_evidence=source_evidence,
            source_wallet=buy.owner, source_block_number=buy.slot, source_initiation=initiation)
        # E14 SHADOW ON SOLANA (spec 2026-10-09 Solana pre-signal feed, step 0): our own marks' momentum
        # read starts at the buy's decode, on the E14 pool. The classifier only peeks it (no GET here).
        from sniper.services.memecoin_real_runner import _start_momentum_read
        self._pretrace_phase("metadata")
        momentum_own = _start_momentum_read("solana", facts["mint"])
        pretrace_end = self._pretrace_phase("momentum_setup")
        self._queue_timing["pretrace_total_ms"] = monotonic_ms(
            pretrace_end, getattr(self, "_pretrace_started", None))
        self._queue_timing["pretrace_cpu_ms"] = _cpu_window(
            getattr(self, "_pretrace_cpu", None), _read_cpu(self.clock),
            self._queue_timing["pretrace_total_ms"])["cpu_ms"]
        trace = EntryAttemptTrace.start(self.conn, ev, now_iso, self.clock)
        trace.momentum_own = momentum_own
        # First substage on purpose: its window is the committed shell-row INSERT (which takes the
        # ledger write lock), so a lock wait shows as wall far above CPU here and nowhere else.
        trace.mark_substage("attempt_row")
        if facts.get('entry_opportunity') is not None:
            trace.mark_substage('entry_opportunity', **facts['entry_opportunity'])
        trace.mark_substage("slot_receipt", duration_ms=0.0, source_slot=int(buy.slot),
                            signal_feed=getattr(buy, 'signal_feed', 'helius'),
                            venue=getattr(buy, "venue", "pump_curve"),
                            routed=getattr(buy, "routed", None) is not None, **receipt_head(buy),
                            **self._queue_timing)
        if rabbit is not None:
            trace.mark_substage("rabbit_signal", duration_ms=0.0, clock="monotonic", **rabbit)
        timings = (getattr(buy, 'ondemand', None) or {}).get('leg_timings_ms')
        if isinstance(timings, dict):
            trace.mark_substage('slot_route_preparation', duration_ms=0.0,
                                leg_timings_ms=dict(timings), clock='monotonic')
        grad = getattr(buy, "graduation", None)
        if grad is not None:
            trace.mark_substage("slot_graduation", trigger=grad.get("trigger"), pool=grad.get("pool"),
                                pool_slot=(grad.get("state") or {}).get("slot"),
                                watch_ms=grad.get("watch_ms"), read_ms=grad.get("read_ms"),
                                queue_ms=(round((self.clock.monotonic() - grad["requeued_mono"]) * 1000.0, 1)
                                          if grad.get("requeued_mono") is not None else None))
        addon = facts.get("addon_refusal")
        if addon is not None:
            # The add-on refusal (memecoin_addon_guard): an attempt row with the rule and its numbers,
            # before any reservation, build or quote (Gamma's forward read needs the row).
            trace.mark_substage("addon_refusal", **addon[1])
            return self._finish_traced(buy, {"status": "rejected", "reason": addon[0]}, trace, facts, t0)
        result = self._reserve_and_execute(buy, ev, trace, snap, facts, now_iso)
        if result.get("_settlement") is not None:
            result["attempt_id"] = trace.attempt_id
            complete = result.pop("_complete", lambda final: final)
            def finish(final):
                trace.conn = self.conn
                return self._finish_traced(buy, complete(final), trace, facts, t0,
                                           expected_note_attempt=trace.attempt_id)
            result["_complete"] = finish
            return result
        return self._finish_traced(buy, result, trace, facts, t0)

    def _finish_traced(self, buy, result, trace, facts, t0, *, expected_note_attempt=None):
        result["lane_ms"] = (self.clock.perf_counter() - t0) * 1000.0
        rabbit = getattr(self, "rabbit_backstop", None)     # a lane built without __init__ has none
        if rabbit is not None and is_rabbit(buy):
            try:
                rabbit.note_result(result, int(buy.slot), feed=getattr(buy, "signal_feed", None))
            except Exception:
                logger.exception("slot lane rabbit result note failed sig=%s", buy.signature)
        from sniper.services.memecoin_fill_timing import execution_stage_spans
        result["stage_spans_ms"] = execution_stage_spans(
            {"trace_json": {"substage_events": getattr(trace, '_substage_events', ())}})
        # THE CURVE COMPLETED UNDER US: watch its canonical pool. Before the wake below, so the
        # on-chain lane reads the hold instead of re-quoting Jupiter into NO_ROUTES.
        from sniper.services.memecoin_slot_graduation import GRADUATION_REASON, quote_curve_completed, quote_curves_on
        if is_rabbit(buy):
            pass                  # never a pool watch on a pre-execution signal; the processed copy may
        elif str(result.get("reason") or "") == GRADUATION_REASON:
            result["graduation"] = self._offer_graduation(buy, "bonding_curve_complete")
        elif (quote_curves_on()
              and str(result.get("reason") or "").startswith("slot lane build refused: curve is complete")
              and getattr(buy, "quote_mint", None) is not None
              and quote_curve_completed(buy) == buy.quote_mint):
            # A quote-token curve the build found complete (a pass the pre-leg trigger does not see:
            # a resume, a requeued leg). Nothing was signed; its reservation is already released.
            result["graduation"] = self._offer_graduation(buy, "build_refused_complete",
                                                          quote_mint=buy.quote_mint)
        self._record_classification(trace)
        from sniper.services.memecoin_addon_label import record_addon_label
        record_addon_label(trace, result, judged_usd=facts.get("their_usd"))   # a label, never a gate
        try:
            changed = trace.finish(str(result.get("status") or "unknown"),
                         str(result.get("reason") or result.get("status") or "unknown")[:200],
                         **{key: result[key] for key in ("pending_id", "position_id", "fill_id",
                                                        "gate_inputs_json") if key in result},
                         **({"tx_sig": result["sig"]} if result.get("sig") else {}))
            if (changed and expected_note_attempt is not None
                    and result.get("status") not in (None, "submitted", "submitted_unknown")):
                _finish_skip_note(buy.signature, expected_note_attempt,
                                  result["status"], result.get("reason"),
                                  evidence=_receipt_facts(buy, result))
        except Exception:
            logger.exception("slot lane trace finish failed mint=%s", facts["mint"])
        if self.wake is not None:
            self.wake.set()
        return result

    def _record_classification(self, trace):
        """AFTER the outcome, never before a send: the late own-momentum read, and this attempt's
        classification when nothing persisted it. A `slot_lane_scaled_clip` rejection used to leave
        classification_json NULL (5 since 10-01), so a runup-made scale could not be told from a
        regime-made one (Gamma on spec 2026-10-09). Best effort; never changes the result."""
        from sniper.persistence import db_memecoin as m
        from sniper.services.memecoin_real_runner import _momentum_own_late_best_effort
        _momentum_own_late_best_effort(trace)
        classification = getattr(trace, "slot_classification", None)
        if classification is None:
            return
        try:
            row = self.conn.execute("SELECT classification_json FROM memecoin_entry_attempts "
                                    "WHERE attempt_id=?", (trace.attempt_id,)).fetchone()
            if row is not None and row[0] is None:
                # Its own commit: the trace refuses to write inside a caller-owned transaction, and
                # finish takes no classification. Measured p50/p90 on a WAL ledger in the commit body.
                payload, digest = m.canonical_funded_json(classification.to_dict())
                m.set_entry_attempt_classification(
                    self.conn, trace.attempt_id, classification_json=payload,
                    classification_hash=digest,
                    classification_version=classification.classification_version)
        except Exception:
            logger.exception("slot lane classification record failed attempt=%s",
                             getattr(trace, "attempt_id", None))

    def _requote_over_packet(self, buy, clip, detail, pid, trace):
        """MEMECOIN_SLOT_LANE_EXOTIC_REQUOTE=oversize: an exotic build refused 'over the packet'
        (3fe2e25ea2aa, 1,293 bytes, a lost fill) goes back to the on-demand worker for a leg on the
        next REQUOTE_PARAMS shape, the reservation released. The worker re-prices and re-admits it at
        its own quote and hands it back like any requeued leg, so the lane thread never waits. Its
        hold (still the leg's) is moved out to REQUOTE_HOLD_S before anything else can see a release.
        Never a third shape; a buy the hold does not own (a receipt resume) is not re-quoted. The
        pending result, or None to refuse as before."""
        od = getattr(buy, "ondemand", None)
        n = requote_attempts(buy)
        now = self.clock.monotonic()
        if (exotic_requote_mode() != "oversize" or od is None or self.exotic_offer is None
                or "byte packet" not in str(detail) or n >= len(REQUOTE_PARAMS)
                or (getattr(buy, "graduation", None) or {}).get("cash_leg_repair")
                or not self.pending_legs.holds(buy.signature, now=now)):
            return None
        params = REQUOTE_PARAMS[n]
        try:
            trial = od.get("trial")
            taken = bool(self.exotic_offer(buy, clip_usd=clip, quote_params=params,
                                           **({"trial": trial} if trial is not None else {})))
        except Exception:
            logger.exception("slot lane exotic re-quote offer failed sig=%s", buy.signature)
            taken = False
        if not taken:
            return None
        self.pending_legs.extend(buy.signature, deadline=now + REQUOTE_HOLD_S)
        # 'slot_lane_build...' keeps the build_refused fallback class (slot_lane_verdict) for the
        # buy if the re-quote ends without a newer attempt row.
        m.release_pending(self.conn, pid, "slot_lane_build:requote:%s" % str(detail)[:30])
        self.exotic_stats["requoted"] = self.exotic_stats.get("requoted", 0) + 1
        trace.mark_substage("slot_requote", reason="over_packet", attempt=n + 1, params=params,
                            bytes_before=packet_bytes(detail))
        return {"status": "rejected", "reason": REQUOTE_PENDING_REASON, "exotic_pending": True}

    def _nozomi_arm(self, buy, trace):
        """Gamma's arm (nozomi-sha256-attempt-odd-v1) for an eligible wallet while the provider is ready,
        marked before the reservation and the build; None (no substage) otherwise."""
        if not self.nozomi_ab:
            return None
        from sniper.execution.solana_sender_ab import nozomi_arm_for, nozomi_wallet_enabled
        if not nozomi_wallet_enabled(getattr(buy, "source_user_id", None)):
            return None
        arm = nozomi_arm_for(trace.attempt_id, tip_lamports_fn=getattr(self, 'entry_tip_fn', None))
        trace.mark_substage("nozomi_ab", **arm)
        return arm

    def _sender_arm(self, buy, trace):
        """The pre-registered arm (sha256-attempt-odd-v1), marked before the build; None when the
        A/B is not ready. CPMM and LaunchLab stay on today's path without an arm, unless
        MEMECOIN_SLOT_LANE_SENDER_ALL is ab (the hash on every venue) or all (arm B everywhere)."""
        if not self.sender_ab:
            return None
        from sniper.execution import solana_sender_ab as sender_ab
        mode = cfg.MEMECOIN_SLOT_LANE_SENDER_ALL
        if mode in ("ab", "all"):
            arm = (sender_ab.assign_all if mode == "all" else sender_ab.assign_ab_all)(trace.attempt_id)
            trace.mark_substage("sender_ab", **arm)
            return arm
        if getattr(buy, "venue", "pump_curve") not in sender_ab.AB_VENUES:
            trace.mark_substage("sender_ab", arm=None, rule=sender_ab.RULE, reason="venue_not_in_ab")
            return None
        arm = sender_ab.assign(trace.attempt_id)
        trace.mark_substage("sender_ab", **arm)
        return arm

    def _trial_marker(self, source_user_id):
        try:
            row = m.open_trial(self.conn, source_user_id, "solana")
        except Exception:
            logger.exception("slot lane: trial lookup failed wallet=%s", source_user_id)
            return None
        if row is None:
            return None
        from sniper.services.memecoin_trial import marker
        return marker(row)

    def _demote_cu_class_if_exhausted(self, cu_class, result):
        """A class-limited send that ran out of compute units demotes its class to the lane default
        (pump_curve_buy.demote_cu_class), with one line, and hands the demotion to the sink, which
        persists it off this thread so it outlives the process. Never raises."""
        from sniper.execution.pump_curve_buy import cu_exhausted, demote_cu_class
        try:
            if cu_exhausted(result) and demote_cu_class(cu_class):
                limit = cfg.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS.get(cu_class)
                logger.warning(
                    "[memecoin] slot lane CU class %s DEMOTED to the lane default %d: "
                    "send %s ran out of compute units at its class limit %s (%s)", "/".join(cu_class),
                    cfg.MEMECOIN_SLOT_LANE_CU_LIMIT, result.get("sig"), limit, result.get("reason"))
                if self.cu_demotion_sink is not None:
                    self.cu_demotion_sink(cu_class, {"sig": result.get("sig"), "signed_limit": limit,
                                                     "reason": str(result.get("reason") or "")[:200],
                                                     "demoted_at": self.now_iso_fn()})
        except Exception:
            logger.exception("slot lane CU demotion check failed sig=%s", result.get("sig"))

    def _record_residue(self, buy, ondemand, result, trace, now_iso):
        """After a filled exotic send: the quote asset the leg left, for the residue sweep. Never
        raises: the fill is booked whatever happens here."""
        from sniper.services.memecoin_quote_residue import record_fill_residue
        residue = result.get("quote_residue_atomic")
        if residue is None:
            logger.warning("[memecoin] slot lane exotic fill with no readable quote residue sig=%s "
                           "quote=%s", result.get("sig"), ondemand.get("quote_mint"))
            return
        quote = ondemand.get("quote") or (None, None)
        try:
            record_fill_residue(self.conn, quote_mint=ondemand.get("quote_mint"), residue_atomic=residue,
                                quote_usd=quote[1], decimals=quote[0], venue=getattr(buy, "venue", None),
                                attempt_id=trace.attempt_id, entry_sig=result.get("sig"),
                                recorded_at=now_iso)
        except Exception:
            logger.exception("slot lane exotic residue record failed sig=%s", result.get("sig"))

    def _scaled_sol_guess(self, classification, leg_scale):
        """The clip a scaled coin would reserve, for judging whether the SOL buffer can carry it, or
        None (refuse slot_lane_scaled_clip). Arithmetic on the cached roster clip, which was sized
        at `leg_scale`; reserve_entry still sizes the real clip and refuses what SOL cannot fund."""
        from sniper.services.memecoin_risk import scaled_funded_clip
        if not cfg.MEMECOIN_SLOT_LANE_SCALED_SOL or self.shadow_clip_fn is None:
            return None
        try:
            clip = float(self.shadow_clip_fn() or 0.0)
            if not (math.isfinite(leg_scale) and leg_scale > 0 and math.isfinite(clip) and clip > 0):
                return None
            return scaled_funded_clip(base_clip_usd=clip / leg_scale,
                                      risk_scale=float(classification.risk_scale),
                                      floor_usd=float(cfg.MEMECOIN_MIN_TRADE_FLOOR_USD))
        except (TypeError, ValueError):
            return None

    def _matching_cash_leg(self, buy, clip):
        from sniper.execution.pump_curve_buy import leg_clip_refusal
        od = getattr(buy, "ondemand", None)
        try:
            leg = od["leg"] if od is not None else (
                self.cash_leg_fn(buy, clip) if self.cash_leg_fn is not None else None)
            atoms = leg["usdc_atomic"] if leg is not None else None
            return type(atoms) is int and atoms > 0 and leg_clip_refusal(leg, clip) is None
        except (KeyError, TypeError, ValueError):
            return False

    def _reserve_and_execute(self, buy, ev, trace, snap, facts, now_iso):
        from sniper.services.memecoin_source_initiation import source_initiation_refusal
        initiation_refusal = source_initiation_refusal(ev, 'solana')
        if initiation_refusal is not None:
            if initiation_refusal == 'missing_receipt_proof' and self.routed_handoff is not None:
                self.routed_handoff.remember(buy)
            return {'status': 'source_initiation_unknown', 'reason': initiation_refusal}
        if facts.get('entry_opportunity') is None:
            window, reason = self._entry_opportunity(buy, mint=facts['mint'])
            if reason:
                return {'status': 'rejected', 'reason': reason}
            if window is not None:
                facts['entry_opportunity'] = window
                trace.mark_substage('entry_opportunity', **window)
        from sniper.services.memecoin_market_state import classify_funded_entry
        from sniper.services.memecoin_risk import reserve_entry, risk_refusal_result
        from sniper.services.memecoin_wallet_caps import cross_wallet_reentry_closes_by
        from sniper.services.memecoin_risk import solana_clip_ceiling_usd, solana_flat_clip_ceiling_usd
        # THE FLAT CEILING (threshold 0, operator 2026-10-08): the one leg_plan sized the prefetched
        # USDC leg with, so the USDC reservation and wallet cap must carry exactly it (None = today).
        flat_ceiling = solana_flat_clip_ceiling_usd()
        from sniper.services.memecoin_real_runner import _momentum_own_peek
        classification_started = self.clock.perf_counter()
        classification = classify_funded_entry(
            self.conn, network_id="solana", mint=facts["mint"], decision_at=now_iso,
            signal_source=SIGNAL_SOURCE, effective_mcap=facts["mcap_usd"], assumed_micro=False,
            market_stats={"price": facts["price_usd"]}, base_clip_usd=None, consensus_n_60m=None,
            momentum_own=_momentum_own_peek(getattr(trace, "momentum_own", None)))
        trace.slot_classification = classification
        # DECISION FEATURES (spec 2026-10-14): two lock-free cache gets and a partial; built at
        # finish with the trace's decision_at. Measurement only, never read by a gate, and never
        # able to fail the entry.
        try:
            pool = getattr(buy, "pool", None) if getattr(buy, "venue", None) == "pump_amm" else None
            pools = self.amm_pools if pool else None
            trace.note_decision_features(partial(
                from_slot_buy, buy=buy, their_usd=facts.get("their_usd"), sol_usd=snap.sol_usd,
                pool_facts=pools.cached(pool) if pools is not None else None,
                migration_observed_at=(pools.migration_observed_at(pool)
                                       if pools is not None else None)))
        except Exception:
            logger.exception("slot lane decision features capture failed mint=%s", facts.get("mint"))
        trace.mark_substage("slot_classification",
                            duration_ms=(self.clock.perf_counter() - classification_started) * 1000)
        trace.mark("market_data")
        try:
            leg_scale = float(self.leg_scale_fn()) if self.leg_scale_fn else 1.0
        except (TypeError, ValueError):
            leg_scale = math.nan               # unreadable: every coin reads as scaled, and refuses
        scaled = classification is not None and float(classification.risk_scale) != leg_scale
        # In memory, no I/O: lands in trace_json at finish, on every row (Gamma on spec 2026-10-09).
        trace.mark_substage(
            "slot_leg_scale", scaled=scaled,
            risk_scale=float(classification.risk_scale) if classification is not None else None,
            leg_scale=leg_scale if math.isfinite(leg_scale) else None)
        scaled_guess = self._scaled_sol_guess(classification, leg_scale) if scaled else None
        if scaled and scaled_guess is None:
            # The leg was prefetched for the market's scale (leg_plan); a coin scaled otherwise
            # (hot_narrow in a normal market, or a regime flip before the leg re-quotes) would
            # build leg_over_clip / leg_under_clip after reserving. Refuse here instead; nothing is
            # reserved or signed, and the confirmed watcher gives this signature one ordinary
            # attempt, which quotes its own clip. The builders' [90%, 100%] bound still holds the
            # spend to the reservation if a torn read gets past this. With
            # MEMECOIN_SLOT_LANE_SCALED_SOL on, a scaled coin may still go SOL-funded (below).
            return {"status": "rejected", "reason": SCALED_CLIP_REASON}
        reserve_args = dict(mint=facts["mint"], source_user_id=buy.source_user_id,
            strategy=cfg.MEMECOIN_REAL_POLICY, day=now_iso[:10],
            usdc_balance=snap.usdc_balance, sol_balance=snap.sol_balance, now_iso=now_iso,
            kill_tripped=False, classification=classification,
            origin_event_id=ev.fomo_event_id, attempt_id=trace.attempt_id,
            signal_source=SIGNAL_SOURCE, source_price=facts["price_usd"],
            source_market_cap=facts["mcap_usd"], observed_at=ev.received_at_normalized,
            clock_domain="observed_real_utc",
            # Cross-wallet re-entry (operator 2026-10-08): the same scope as the Solana leg.
            reentry_closes_by=cross_wallet_reentry_closes_by(buy.source_user_id, "solana"),
            clip_ceiling_usd=flat_ceiling)
        if self.confirmations is not None:
            reserve_args["snapshot_debits_fn"] = lambda: self.confirmations.snapshot_funding(
                snap.observed_mono["balances"])
        # TRIAL ARM: native SOL needs no leg; an async cash leg must fit the trial clip.
        # One indexed read; a failed read gives no marker and reservation refuses the trial.
        od = getattr(buy, "ondemand", None)
        # Keep the queued trial identity even if it ends while the worker quotes. The
        # reservation validates this original marker again; never turn it into a full clip.
        trial = (od.get("trial") if od is not None else None) or self._trial_marker(buy.source_user_id)
        clip_guess = (float(trial["clip_usd"]) if trial is not None
                      else scaled_guess if scaled
                      else self.shadow_clip_fn() if self.shadow_clip_fn else None)
        from sniper.services.memecoin_wallet_caps import entry_cap_usd
        wallet_cap = entry_cap_usd(buy.source_user_id, "solana", ceiling_usd=flat_ceiling)
        uncapped_guess = clip_guess
        if wallet_cap is not None:
            reserve_args["size_cap_usd"] = wallet_cap
            if clip_guess is not None:
                clip_guess = min(float(clip_guess), wallet_cap)
        if od is not None and od.get("requested_clip_usd") is not None:
            reserve_args["size_cap_usd"] = min(
                float(od["requested_clip_usd"]), float(reserve_args.get("size_cap_usd", math.inf)))
            if clip_guess is not None:
                clip_guess = min(float(clip_guess), reserve_args["size_cap_usd"])
        wallet_scaled = clip_guess != uncapped_guess

        def sol_funds(usd):
            """Native SOL (after entry and exit gas) and the buffer cap both cover `usd`."""
            return (usd is not None and self.sol_buffer_cap_usd > 0
                    and 0 < float(usd) <= self.sol_buffer_cap_usd
                    and snap.sol_balance is not None and snap.sol_usd is not None
                    and (float(snap.sol_balance) - float(cfg.MEMECOIN_GAS_ENTRY_SOL)
                         - float(cfg.MEMECOIN_GAS_EXIT_SOL) * float(cfg.MEMECOIN_GAS_RETRY_FACTOR))
                    * float(snap.sol_usd) >= float(usd))
        sol_venue = (getattr(buy, "venue", "pump_curve") in ("pump_curve", "pump_amm")
                     # a pool quoted in anything but SOL cannot be paid in native SOL (E2)
                     and not self._cap_key(buy).endswith(":exotic"))
        prefer_sol = sol_venue and sol_funds(clip_guess)
        if (scaled or wallet_scaled or trial is not None) and not prefer_sol:
            # An event-specific leg can fund this scale; the builder checks the authoritative
            # reservation again. Otherwise quote that leg on the existing worker, never here.
            if not self._matching_cash_leg(buy, clip_guess):
                pending_leg = self._exotic_handoff(
                    buy, TRIAL_UNMARKED if trial is not None else SCALED_CLIP_REASON,
                    clip_usd=clip_guess, trial=trial)
                if pending_leg is not None:
                    return pending_leg
                if trial is not None:
                    return {"status": "rejected", "reason": "%s: slot lane carries a trial only SOL-funded"
                                                            % TRIAL_UNMARKED}
                return {"status": "rejected", "reason": SCALED_CLIP_REASON}
        if scaled:
            trace.mark_substage("slot_scaled_sol" if prefer_sol else "slot_scaled_leg", risk_scale=float(classification.risk_scale),
                                leg_scale=leg_scale, clip_guess=round(float(clip_guess), 2),
                                leg_source="ondemand" if od is not None else "prefetched")
        # ABOVE NANO (operator 2026-10-08), ON THE SOL-FUNDED TRY ONLY. The USDC leg was prefetched
        # before the coin was known (leg_plan) and must be 90-100% of the reserved clip, so a raised
        # reservation would refuse its own leg; the USDC try and the SOL-short fallback below keep
        # today's clip. SOL needs no leg, and MEMECOIN_SOL_EXEC_BUFFER_CAP_USD still bounds it. A
        # trial keeps its clip and an on-demand leg its requested clip, so neither is raised.
        # Raised only when SOL and the buffer cap can fund the raised clip: a SOL try the funding
        # check refuses would fall back to USDC, slower than today's SOL-funded fill at 175.
        # LISTED WALLETS (operator 2026-10-08 ~14:20Z; Alpha call (a)): with a raised-clip wallet list
        # set, the leg stays at the unlisted majority's clip (no flat ceiling), so a listed wallet is
        # raised here or not at all. SOL short of the raised clip falls back to today's path (SOL or
        # the prefetched leg at today's clip), never the on-demand leg: speed over $25. Traced.
        raise_wanted = (solana_clip_ceiling_usd(classification, "solana", buy.source_user_id)
                        if trial is None and od is None else None)
        sol_raise = raise_wanted if prefer_sol and sol_funds(raise_wanted) else None
        if raise_wanted is not None and sol_raise is None and raise_wanted != flat_ceiling:
            trace.mark_substage("raised_clip_fallback", wanted_usd=float(raise_wanted),
                                reason="sol_short" if sol_venue else "not_sol_venue",
                                funding="sol" if prefer_sol else "usdc_leg",
                                clip_guess=None if clip_guess is None else float(clip_guess))
        nozomi_arm = self._nozomi_arm(buy, trace)
        nozomi_tip = ((nozomi_arm["tip_account"], nozomi_arm["tip_lamports"])
                      if nozomi_arm and nozomi_arm["arm"] == "B" else None)
        reserve_args["entry_tip_lamports"] = nozomi_tip[1] if nozomi_tip else 0
        sol_args = (dict(funding_asset="sol", funding_sol_usd=snap.sol_usd,
                         sol_buffer_cap_usd=self.sol_buffer_cap_usd,
                         # A Nozomi arm-B tip leaves the same SOL: reserve it beside the ATA rent.
                         funding_overhead_atomic=NATIVE_ATA_RENT_RESERVE_LAMPORTS * (
                             2 if (getattr(buy, "venue", "pump_curve") == "pump_amm"
                                   or getattr(buy, "graduation", None) is not None) else 1)
                         + (nozomi_tip[1] if nozomi_tip else 0),
                         **(dict(clip_ceiling_usd=sol_raise, size_cap_usd=entry_cap_usd(
                             buy.source_user_id, "solana", ceiling_usd=sol_raise))
                            if sol_raise else {}))
                    if prefer_sol else {})
        reservation_started = self.clock.perf_counter()
        reservation_timings = [{}]
        entry_sol_budget = {}
        try:
            decision, pid = reserve_entry(self.conn, **{**reserve_args, **sol_args}, trial=trial,
                                          entry_sol_budget=entry_sol_budget,
                                          timing=reservation_timings[-1])
            if (prefer_sol and not decision.allowed and "SOL buffer insufficient" in decision.reason):
                if trial is not None:
                    pending_leg = self._exotic_handoff(buy, TRIAL_UNMARKED,
                                                       clip_usd=clip_guess, trial=trial)
                    return pending_leg or {"status": "rejected", "reason": "%s: slot lane SOL buffer short for the "
                                                                          "trial clip" % TRIAL_UNMARKED}
                elif scaled:
                    # Never the USDC fallback on a scaled coin: its leg does not fit the clip.
                    return {"status": "rejected", "reason": SCALED_CLIP_REASON}
                # The first attempt is sig-less and unsent. A competing reservation may have taken
                # native funds since the warm balance read; the existing USDC route remains eligible.
                else:
                    reservation_timings.append({})
                    decision, pid = reserve_entry(self.conn, **reserve_args,
                                                  entry_sol_budget=entry_sol_budget,
                                                  timing=reservation_timings[-1])
        finally:
            trace.mark_substage("slot_reservation",
                                duration_ms=(self.clock.perf_counter() - reservation_started) * 1000,
                                attempts=reservation_timings)
            trace.mark("reservation")
        if not decision.allowed or pid is None:
            return risk_refusal_result(decision)
        from sniper.services.memecoin_wallet_caps import mark_reentry_lift
        mark_reentry_lift(trace, decision, reserve_args["reentry_closes_by"])
        clip = float(decision.approved_size_usdc)
        pending = m.pending_row(self.conn, pid)
        native_lamports = (int(pending["funding_amount_atomic"])
                           if pending.get("funding_asset") == "sol" else None)
        funding = {"funding_lamports": native_lamports} if native_lamports is not None else {}
        if nozomi_tip:
            tip = nozomi_tip                  # in place of the Sender tip: no Sender arm, no Sender POST
        else:
            arm = self._sender_arm(buy, trace)
            tip = (arm["tip_account"], arm["tip_lamports"]) if arm and arm["arm"] == "B" else None
        # CROWDED PRIORITY FEE (memecoin_crowded_fee): early sources, treated or a 1-in-4 holdout.
        crowd = (self.crowded.arm(buy, trace.attempt_id, head_minus_s=receipt_head(buy)["head_at_recv_minus_s"])
                 if self.crowded is not None else None)
        if crowd is not None:
            trace.mark_substage("crowded_fee", duration_ms=0.0, **crowd)
        priority = ({"priority_lamports": crowd["target_lamports"]}
                    if crowd is not None and crowd["arm"] == "treated" else {})
        build_started = self.clock.perf_counter()
        built, detail = self.build_fn(buy, clip, snap, **funding, **({"tip": tip} if tip else {}), **priority)
        if tip and built is None and "byte packet" in str(detail):
            # The tipped transaction is over the packet: build arm A's instead. Nothing is signed yet.
            built, detail = self.build_fn(buy, clip, snap, **funding, **priority)
            tip = None
            if nozomi_tip:
                trace.mark_substage("nozomi_ab_fallback", sent="untipped", reason="tip_over_packet")
            else:
                trace.mark_substage("sender_ab_fallback", sent="A", reason="tip_over_packet")
        cu_class = detail.get("cu_class") if isinstance(detail, dict) else None
        built_bid = detail.get("priority_bid") if isinstance(detail, dict) else None   # record_slot_build pops it
        detail = record_slot_build(trace, detail,
                                   duration_ms=(self.clock.perf_counter() - build_started) * 1000)
        if tip and isinstance(detail, dict):
            detail = {**detail, ("nozomi_tip_lamports" if nozomi_tip else "sender_tip_lamports"): tip[1]}
        if priority and isinstance(detail, dict):
            # The signing cap lifts to this for a treated send only (solana_sender_ab.cap_entry_priority_fee).
            detail = {**detail, "crowded_priority_lamports": priority["priority_lamports"]}
        ondemand = getattr(buy, "ondemand", None)
        if ondemand is not None and isinstance(detail, dict):
            # The executor reads this quote asset's residue from the fill's own receipt.
            detail = {**detail, "residue_quote_mint": ondemand.get("quote_mint")}
        if requote_attempts(buy):
            od = buy.ondemand
            offered = od.get("requote_offered_mono")
            trace.mark_substage("slot_requote_result", attempt=requote_attempts(buy), params=od.get("requoted"),
                                outcome="fit" if built is not None else "still_over" if "byte packet" in str(detail)
                                else "refused", bytes_after=_wire_bytes(built) if built is not None
                                else packet_bytes(detail),
                                requote_ms=(round((self.clock.monotonic() - offered) * 1000.0, 1)
                                            if offered is not None else None))
        if built is None:
            repaired = self._graduation_cash_repair(buy, clip, detail, pending, trace)
            if repaired is not None:
                return repaired
            requoted = self._requote_over_packet(buy, clip, detail, pid, trace)
            if requoted is not None:
                return requoted
            m.release_pending(self.conn, pid, "slot_lane_build:%s" % str(detail)[:40])
            return {"status": "aborted", "reason": "slot lane build refused: %s" % detail, "pending_id": pid}
        from sniper.services.memecoin_risk import ATA_RENT_SOL, ENTRY_ATA_COUNT
        rent_lamports = round(ENTRY_ATA_COUNT * ATA_RENT_SOL * 1e9)
        if native_lamports is not None:
            rent_lamports = max(rent_lamports, int(pending.get("funding_overhead_atomic") or 0)
                                - reserve_args["entry_tip_lamports"])
        detail = {**detail, "entry_sol_budget": dict(entry_sol_budget),
                  "entry_rent_lamports": rent_lamports}
        if (detail.get("target_mint") != pending["mint"]
                and (native_lamports is not None or detail.get("target_mint") is not None)):
            m.release_pending(self.conn, pid, "slot_lane_target_mismatch")
            return {"status": "aborted", "reason": "slot_lane_target_mismatch", "pending_id": pid}
        if native_lamports is not None and (detail.get("funding_asset") != "sol" or
                                            detail.get("funding_amount_atomic") != native_lamports):
            m.release_pending(self.conn, pid, "slot_lane_funding_mismatch")
            return {"status": "aborted", "reason": "slot_lane_funding_mismatch", "pending_id": pid}
        price_limit = detail.get("pump_price_limit") if isinstance(detail, dict) else None
        if price_limit:
            trace.mark_substage("slot_price_limit", **price_limit)
            override = cfg.copy_cap_override_fields(
                getattr(buy, "source_user_id", None), "solana", lane="slot",
                price=_expected_unit_price(price_limit),
                average=price_limit.get("source_average_price_usd"),
                post=price_limit.get("source_price_usd"), band_pct=cfg.MEMECOIN_REAL_CHASE_MAX_PCT)
            if override is not None:
                trace.mark_substage("copy_cap_override", **override)
            log_cap_lift_once(getattr(buy, "source_user_id", None), price_limit, override)
        from sniper.execution.pump_curve_buy import WSOL_MINT
        round_trip = detail.get("exotic_round_trip") if isinstance(detail, dict) else None
        round_trip_required = (getattr(buy, "venue", None) == "pump_amm"
                               and facts.get("quote_mint") not in (None, WSOL_MINT))
        if (round_trip_required or round_trip is not None) and getattr(
                cfg, "MEMECOIN_SLOT_LANE_EXOTIC_ROUND_TRIP", True):
            # E1 (Alpha 2026-10-17): an exotic-quote buy's round trip, priced in the build from the warm
            # leg and its reverse quote (no call here), judged against the position's own cap before
            # anything is signed. Unmeasured (a cold leg, no reverse) is a miss, never a blind fill.
            from sniper.execution.solana_swap_executor import _contract_tp_pct
            cap = cfg.entry_roundtrip_cap_pct(cfg.MEMECOIN_ENTRY_IMPACT_CAP_PCT, _contract_tp_pct(pending))
            if not isinstance(round_trip, dict):
                round_trip = {"state": "unmeasured", "reason": "missing_evidence"}
            cost = round_trip.get("cost_pct")
            if (round_trip.get("state") == "measured"
                    and (type(cost) not in (int, float) or not math.isfinite(cost))):
                round_trip = {"state": "unmeasured", "reason": "invalid_cost"}
            trace.mark_substage("exotic_round_trip", cap_pct=cap, **round_trip)
            if round_trip.get("state") != "measured":
                m.release_pending(self.conn, pid, "exotic_round_trip_unmeasured")
                return {"status": "aborted", "pending_id": pid,
                        "reason": "exotic_round_trip_unmeasured:%s" % round_trip.get("reason")}
            if round_trip["cost_pct"] > cap:
                m.release_pending(self.conn, pid, "exotic_round_trip")
                return {"status": "aborted", "pending_id": pid,
                        "reason": "exotic_round_trip cost=%.1f%% cap=%.1f%%" % (round_trip["cost_pct"], cap)}
        guard = getattr(self.rabbit_backstop, "slippage_guard", None)
        if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY and guard is None:
            from sniper.execution.entry_attribution import market_movement_check
            trace.mark_substage("copy_market_movement", **market_movement_check(
                cap_pct=cfg.copy_premium_cap(getattr(buy, "source_user_id", None), "solana")[0]))
        if guard is not None:
            try:
                slippage = guard(buy, detail)
                if not isinstance(slippage, dict):
                    slippage = {"state": "unknown", "reason": "invalid_cached_verdict"}
            except Exception:
                slippage = {"state": "unknown", "reason": "cached_check_unavailable"}
                logger.exception("slot lane cached slippage check unavailable")
            trace.mark_substage("slot_slippage_check", **slippage)
            if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
                from sniper.execution.entry_attribution import market_movement_check
                movement = slippage.get("market_movement") or market_movement_check(
                    cap_pct=cfg.copy_premium_cap(getattr(buy, "source_user_id", None), "solana")[0])
                trace.mark_substage("copy_market_movement", **movement)
            if slippage.get("state") == "refuse":
                reason = ("intervening_market_cap" if slippage.get("reason") == "intervening_market_cap"
                          else "cached_price_beyond_limit")
                released = m.release_unsigned_blocked_entry(self.conn, pid, reason)
                return {"status": "aborted" if released else "unknown",
                        "reason": reason, "pending_id": pid}
        trace.mark_substage("slot_post_build", **self._boundary(buy))
        verdict = (self.rabbit_backstop.not_landed(buy.signature)
                   if self.rabbit_backstop is not None and is_rabbit(buy) else None)
        if verdict:
            # SOURCE_NOT_LANDED before we signed (memecoin_rabbit_signal): nothing goes out.
            m.release_pending(self.conn, pid, "%s:unsigned" % verdict)
            return {"status": "rejected", "reason": verdict, "pending_id": pid}
        new_venue = (getattr(buy, "venue", "pump_curve") in SHADOW_ONLY_VENUES
                     or self._cap_key(buy).endswith(":exotic"))
        if new_venue:
            self._venue_live_sends[self._cap_key(buy)].append(self.clock.monotonic())
        result = self.executor["slot_execute_fn"](
            self.conn, pending, built, validity=detail, now_iso=now_iso,
            trace=trace, sol_usd=snap.sol_usd, boundary_fn=lambda: self._boundary(buy),
            entry_head_fn=self.head_slot_fn or entry_policy.head,
            **({"defer_confirmation": True} if self.confirmations is not None else {}))
        result["pending_id"] = pid
        if priority and result.get("sig"):
            # Out on the wire: its SIGNED priority fee counts against today's cap, failed or not. The
            # entry-bid cap may change the build's price at signing (2026-10-15: 10M built, 520k
            # signed); with no entry_bid_signed the built message was signed as built.
            bid = built_bid if isinstance(built_bid, dict) else None
            built_lamports = bid.get("priority_fee_lamports") if bid is not None else None
            signed = trace.last_substage("entry_bid_signed") if hasattr(trace, "last_substage") else None
            sent = signed.get("priority_fee_lamports") if signed is not None else built_lamports
            trace.mark_substage("crowded_fee_sent", duration_ms=0.0, arm="treated", sent_lamports=sent,
                                built_lamports=built_lamports, crowded=(bid or {}).get("crowded"),
                                tip_lamports=(signed or bid or {}).get("tip_lamports"))
            from sniper.services.memecoin_crowded_fee import NOT_RAISED
            if (bid or {}).get("crowded") not in NOT_RAISED:   # the normal bid is no crowded spend (F9)
                self.crowded.sent(sent)
        def after_settlement(result):
            if cu_class and result.get("status") == "failed":
                self._demote_cu_class_if_exhausted(cu_class, result)
            if ondemand is not None and result.get("status") == "filled":
                self._record_residue(buy, ondemand, result, trace, now_iso)
            if new_venue:
                try:
                    self._canary_line(buy, detail, result)
                except Exception:
                    logger.exception("slot lane canary line failed mint=%s", getattr(buy, "mint", None))
            if result.get("status") == "filled" and result.get("position_id"):
                # The executor knows only the mint and writes '(real)'. Stamp the roster handle after the
                # fill, as the ordinary hook does for a feed fill: all 9 slot-lane positions to
                # 2026-09-27 read '(real)' on every handle-keyed card and stat. Never overwrites a name.
                try:
                    m.set_position_identity(self.conn, result["position_id"],
                                            source_handle=getattr(buy, "handle", None))
                except Exception:
                    logger.exception("slot lane handle stamp failed pos=%s", result["position_id"])
            return result

        if result.get("_settlement") is not None:
            result["_complete"] = after_settlement
            return result
        return after_settlement(result)


class SlotSettlement:
    def __init__(self, buy, result, completed_mono):
        self.buy, self.result, self.completed_mono = buy, result, completed_mono


class CashSettlement:
    def __init__(self, pending_id, observed_mono):
        self.pending_id, self.observed_mono = pending_id, observed_mono


class SlotConfirmations:
    """Two fixed receipt workers; the lane alone owns admission, debit retirement and callbacks."""

    MAX_ACTIVE = 2
    MAX_RETAINED = 256

    def __init__(self, *, conn_factory, put, clock=time, start_worker=None):
        import queue
        import threading
        self.conn_factory, self.put, self.clock = conn_factory, put, clock
        self.jobs = queue.SimpleQueue()  # the lane's active count bounds submissions; put never waits
        self.debits = {}  # pending ID -> completion time, or None while unresolved
        self.active = set()
        self.unresolved = frozenset()
        self.gas_debits = {}
        self._cash_view = (float('-inf'), ())  # immutable publication for ordinary entry threads
        self.stats = {'active': 0, 'retained': 0, 'completed': 0, 'max_active': self.MAX_ACTIVE}
        start_worker = start_worker or (lambda target: threading.Thread(
            target=target, name='slot-lane-settlement', daemon=True).start())
        for _ in range(self.MAX_ACTIVE):
            start_worker(self._work)

    def available(self):
        return (len(self.active) < self.MAX_ACTIVE and
                (len(self.debits) < self.MAX_RETAINED or
                 any(at is not None for at in self.debits.values())))

    def snapshot_debits(self, observed_mono):
        return self.snapshot_funding(observed_mono)['pending_ids']

    def snapshot_funding(self, observed_mono):
        floor, records = self._cash_view
        if not isinstance(observed_mono, (int, float)) or math.isnan(observed_mono) or observed_mono < floor:
            raise ValueError('cash snapshot predates retained settlement history')
        relevant = [(pid, gas) for pid, finished, gas in records
                    if finished is None or observed_mono < finished]
        return {'pending_ids': tuple(pid for pid, gas in relevant),
                'extra_sol_atomic': sum(gas for pid, gas in relevant)}

    def _publish(self, floor=None):
        self._cash_view = (self._cash_view[0] if floor is None else floor,
                          tuple((pid, at, self.gas_debits.get(pid, 0)) for pid, at in self.debits.items()))

    def register(self, result):
        if not self.available():
            raise RuntimeError('settlement capacity must be checked before signing')
        if len(self.debits) >= self.MAX_RETAINED:
            oldest, finished = min(((pid, at) for pid, at in self.debits.items() if at is not None),
                                   key=lambda record: record[1])
            del self.debits[oldest]
            self.gas_debits.pop(oldest, None)
            self._publish(max(self._cash_view[0], finished))
        pid = result['pending_id']
        self.debits[pid] = None  # registered before a worker can finalize the reservation
        self.gas_debits[pid] = int(result.get('_gas_debit_lamports') or 0)
        self.active.add(pid)
        self._publish()
        self.stats.update(active=len(self.active), retained=len(self.debits))

    def offer(self, buy, result):
        if result['pending_id'] not in self.active:
            self.register(result)
        self.jobs.put_nowait((buy, dict(result)))

    def completed(self, pending_id, completed_mono, *, status=None):
        self.active.discard(pending_id)
        unknown = status == 'unknown'
        self.debits[pending_id] = None if unknown else completed_mono
        if unknown:
            self.unresolved = self.unresolved | {pending_id}
        self._publish()
        self.stats.update(active=len(self.active), retained=len(self.debits),
                          completed=self.stats['completed'] + 1)

    def recovered(self, pending_id, observed_mono):
        if pending_id in self.unresolved:
            self.debits[pending_id] = observed_mono
            self.unresolved = self.unresolved - {pending_id}
            self._publish()

    def _work(self):
        while True:
            buy, dispatched = self.jobs.get()
            conn = None
            result = {key: value for key, value in dispatched.items() if not key.startswith('_')}
            try:
                conn = self.conn_factory()
                result.update(dispatched['_settlement'].resolve(conn))
            except Exception:
                logger.exception('slot lane settlement worker failed pending=%s', result['pending_id'])
                result.update(status='unknown', reason='settlement_worker_error')
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        logger.exception('slot lane settlement connection close failed')
            result['_complete'] = dispatched.get('_complete')
            self.put(SlotSettlement(buy, result, self.clock.monotonic()))


class MissLearner:
    """A PumpSwap `pool_unknown` refusal -> read that pool NOW on this object's thread and give the
    lane the same buy ONCE more (2026-09-27, Alpha GO).

    WHY. In the lane's first 25.7 h, 5 of the 7 PumpSwap fills that did not go via the lane were
    pool misses (hashbergers, sunnydog06, limfork, fmpumpguy, redemptionjizzy). The pool cache
    learns a miss on the prefetch thread's next tick, after its Jupiter leg refresh: the hand-off
    wrote those rows 131-782 ms after the refusal, past the lane's 0.5 s signal-age gate, and J7
    or the watcher filled them. 4 of the 5 coins had never been seen before that buy, so no
    prewarm could have known the pool.

    Rules (Alpha's four conditions):
    - Budget MISS_LEARN_BUDGET_S from the miss. A learn that lands inside it retries the buy; past
      it (a timer fires), or when the read fails, the buy is handed off at once. One line per miss
      logs learn_ms whatever the outcome.
    - At most MISS_LEARN_MAX_WAITING misses wait; a further miss is handed off by the lane at once.
      One read per pool in flight: a second miss on that pool waits for the same read.
    - A signature is offered once, ever: never re-queued twice. The retry pass hands off every
      outcome but a fill (SlotLane.handle).
    Threads: `offer` and `is_retry` run on the lane thread; `learn_once` on this learner's thread;
    `_expire` on a timer thread. Each miss settles exactly once, under the lock."""

    def __init__(self, *, pools, put, handoff_fn, log, clock=time, budget_s=MISS_LEARN_BUDGET_S,
                 max_waiting=MISS_LEARN_MAX_WAITING, timer=None, keep_warm_s=MISS_LEARN_KEEP_WARM_S):
        import collections
        import queue
        import threading
        # `pools`: one venue -> pool cache mapping (each cache has learn(pool)), or a single cache,
        # which is the PumpSwap one. A buy of a venue with no cache here is never taken.
        self.venue_pools = dict(pools) if isinstance(pools, dict) else {"pump_amm": pools}
        self.pools = self.venue_pools.get("pump_amm")
        self.put, self.handoff_fn, self.log, self.clock = put, handoff_fn, log, clock
        self.budget_s, self.max_waiting = float(budget_s), int(max_waiting)
        self.keep_warm_s = float(keep_warm_s)
        self.timer = timer or threading.Timer
        self._lock = threading.Lock()
        # Pools to read, one entry per read in flight: ONE QUEUE AND ONE THREAD PER VENUE, so a
        # shadow-only venue's read can never sit in front of a PumpSwap (funded) miss. `_jobs` is
        # the PumpSwap queue.
        self._jobs = queue.SimpleQueue()
        self._venue_jobs = {venue: queue.SimpleQueue() for venue in self.venue_pools
                            if venue != "pump_amm"}
        self._waiting = {}                     # signature -> the miss, until it settles
        self._by_pool = {}                     # pool -> signatures waiting on its one read
        self._pool_cache = {}                  # pool -> the venue cache that reads it
        self._offered = collections.OrderedDict()   # every signature ever offered (FIFO-capped)
        self._retried = collections.OrderedDict()   # signatures put back on the lane
        # Counts change only while holding the existing learner lock. Heartbeat copies this map
        # off path; no extra entry lock, read or retry is introduced.
        self.stats = dict(offered=0, duplicates=0, unsupported=0, queue_full=0, waiting=0,
                          read_started=0, read_failed=0, retry_ready=0, expired=0, late=0,
                          unlearnable=0, orphaned_reads=0)

    def offer(self, buy):
        """Lane thread. True = this learner now owns the buy and the lane must not hand it off."""
        sig, pool = buy.signature, getattr(buy, "pool", None)
        cache = self.venue_pools.get(getattr(buy, "venue", None))
        with self._lock:
            if not pool or cache is None:
                self.stats['unsupported'] += 1
                return False
            if sig in self._offered:
                self.stats['duplicates'] += 1
                return False
            self._offered[sig] = True
            while len(self._offered) > 4096:
                self._offered.popitem(last=False)
            venue = getattr(buy, "venue", None)
            if sum(1 for m in self._waiting.values() if m["venue"] == venue) >= self.max_waiting:
                full = True
                self.stats['queue_full'] += 1
            else:
                full = False
                read = pool not in self._by_pool
                self._by_pool.setdefault(pool, []).append(sig)
                self._pool_cache[pool] = cache
                self._waiting[sig] = {"buy": buy, "pool": pool, "t0": self.clock.monotonic(),
                                      "venue": venue}
                self.stats['offered'] += 1
                self.stats['waiting'] = len(self._waiting)
                self.stats['read_started'] += int(read)
        if full:
            self._line(buy, 0.0, None, "handoff:queue_full")
            return False
        expire = self.timer(self.budget_s, self._expire, args=(sig,))
        expire.daemon = True
        with self._lock:
            if sig in self._waiting:
                self._waiting[sig]["timer"] = expire
        expire.start()
        if read:
            self._venue_jobs.get(venue, self._jobs).put(pool)
        return True

    def is_retry(self, signature):
        with self._lock:
            return signature in self._retried

    def run(self, venue="pump_amm"):
        import queue
        import threading
        jobs = self._venue_jobs.get(venue, self._jobs)
        # Only the PumpSwap thread pings: the other venues' caches do not share its connection.
        # The ping runs on its own thread, one at a time, so a slow ping never delays a learn.
        warm = getattr(self.pools, "keep_warm", None) if venue == "pump_amm" else None
        pinger = None
        while True:
            try:
                pool = jobs.get(timeout=self.keep_warm_s if warm is not None else None)
            except queue.Empty:
                if pinger is None or not pinger.is_alive():
                    pinger = threading.Thread(target=warm, name="slot-lane-pool-ping", daemon=True)
                    pinger.start()
                continue
            self.learn_once(pool)

    def learn_once(self, pool):
        started = self.clock.monotonic()
        read_hint = None
        with self._lock:
            cache = self._pool_cache.pop(pool, None) or self.pools
            waiting = [self._waiting[sig] for sig in self._by_pool.get(pool, ()) if sig in self._waiting]
            if waiting and all(miss["venue"] == "meteora_damm_v2"
                               and isinstance(miss["buy"], RoutedDammMiss) for miss in waiting):
                from sniper.execution.meteora_damm_v2_buy import DammV2ReadHint
                targets = None
                for miss in waiting:
                    routed = miss["buy"].routed
                    hint = getattr(routed, "damm_read_hint", None)
                    if (not isinstance(hint, DammV2ReadHint) or not hint.owner
                            or hint.owner != routed.owner or hint.coin != routed.mint or hint.coin != pool):
                        read_hint = None
                        break
                    current = tuple(getattr(hint, key) for key in (
                        "pool", "mint_a", "mint_b", "vault_a", "vault_b", "program_a", "program_b"))
                    if targets is not None and targets != current:
                        read_hint = None
                        break
                    if targets is None:
                        read_hint, targets = hint, current
        try:
            learned = bool(cache.learn(pool, read_hint=read_hint) if read_hint is not None else cache.learn(pool))
        except Exception:
            logger.warning("slot lane miss learn failed pool=%s", pool, exc_info=True)
            learned = False
        read_ms = (self.clock.monotonic() - started) * 1000.0
        with self._lock:
            sigs = self._by_pool.pop(pool, [])
            self.stats['read_failed'] += int(not learned)
            self.stats['orphaned_reads'] += int(not sigs)
        if not sigs:
            # Every waiter's budget ran out first: its line says read_ms=-, so the read's own time
            # is logged here, or the tail that refuses pool_learn_budget is never measured.
            self._late_line(pool, read_ms, learned)
        for sig in sigs:
            self._settle(sig, learned, read_ms)

    def _late_line(self, pool, read_ms, learned):
        try:
            self.log("[memecoin] slot lane miss-learn late read %s read_ms=%.1f learned=%d" % (
                str(pool)[:8], read_ms, bool(learned)))
        except Exception:
            logger.exception("slot lane miss-learn log failed")

    def _expire(self, sig):
        self._settle(sig, None, None)

    def _settle(self, sig, learned, read_ms):
        with self._lock:
            miss = self._waiting.pop(sig, None)
            if miss is None:
                return                                   # already settled by the other path
            if learned is None:                          # the budget ran out: leave the read to
                waiting = self._by_pool.get(miss["pool"])  # settle any later miss on that pool
                if waiting and sig in waiting:
                    waiting.remove(sig)
            learn_ms = (self.clock.monotonic() - miss["t0"]) * 1000.0
            retry = learned is True and learn_ms <= self.budget_s * 1000.0
            self.stats['waiting'] = len(self._waiting)
            self.stats['retry_ready' if retry else 'expired' if learned is None else
                       'late' if learned else 'unlearnable'] += 1
            if retry:
                self._retried[sig] = True
                while len(self._retried) > 4096:
                    self._retried.popitem(last=False)
        if learned is not None and miss.get("timer") is not None:
            miss["timer"].cancel()
        convert = getattr(miss["buy"], "convert", None)
        if retry and convert is not None:
            # B1: a routed pool miss is converted AGAIN now that the pool is known, with every
            # check pump_slot_buy makes; a buy that no longer qualifies is handed off with why.
            try:
                converted, why = convert()
            except Exception:
                logger.exception("slot lane routed miss conversion failed sig=%s", sig)
                converted, why = None, "conversion_error"
            if converted is None:
                outcome = "handoff:" + str(why)
                self.handoff_fn(miss["buy"], str(why))
            else:
                outcome = "retry"
                self.put(converted)
        elif retry:
            outcome = "retry"
            self.put(miss["buy"])
        else:
            outcome = "handoff:" + ("budget" if learned is None else "late" if learned else "unlearnable")
            self.handoff_fn(miss["buy"], "pool_unknown")
        self._line(miss["buy"], learn_ms, read_ms, outcome)

    def _line(self, buy, learn_ms, read_ms, outcome):
        if outcome.startswith('handoff:'):
            _skip_note(buy.signature, 'rejected', 'pool_learn_' + outcome.split(':', 1)[1])
        try:
            # A routed miss (B1) says so: on a hand-off, learn_ms is the delay its routed row took
            # beyond today's immediate write (Gamma LOW 2 on d5c2e223f).
            self.log("[memecoin] slot lane miss-learn %s %s S=%s learn_ms=%.1f read_ms=%s -> %s%s" % (
                getattr(buy, "handle", "?"), str(getattr(buy, "pool", None) or "?")[:8],
                getattr(buy, "slot", "?"), learn_ms,
                "%.1f" % read_ms if read_ms is not None else "-", outcome,
                " routed=1" if isinstance(buy, RoutedPoolMiss) else ""))
        except Exception:
            logger.exception("slot lane miss-learn log failed")


class Handoff:
    """Slot-lane refusals the ordinary on-chain lane can still judge, written for it as detections.

    WHY (2026-09-27). Tekkerrss bought 54bzG3hj... on PumpSwap at 23:12:00Z (S=450818643, $489 at
    ~$21k mcap). The lane refused it `pool_unknown` because its pool cache had not learned the pool
    yet, J7 does not relay that wallet and the confirmed watcher did not cover it, so no lane made
    an attempt. A non-SOL-quoted curve (untaxxable, 2FLwcVAh...) refused `slot_lane_price_unreadable`
    the same way, and the watcher, which classifies SOL/USDC spends only, wrote nothing either.

    A handed-off buy becomes an ordinary `onchain_wallet_detections` row: the on-chain lane runs
    every gate on it, dedups by signature against J7 and the slot lane, and applies its own
    freshness. Only a buy the lane could SIZE is handed off; an unsized one is skipped (logged
    `skipped_unsized`) so it cannot displace the watcher's valued row for the same signature.
    The row is marked `basis = slot_lane_handoff` in `source_valuation_json`. Its
    `detected_at` is the lane's receipt time, the host clock the watcher also writes. A signature
    the watcher already wrote keeps the watcher's row (INSERT OR IGNORE).

    Threads: `offer` runs on the lane thread and only enqueues. `flush` runs on the prefetch thread
    AFTER its refresh, which is where the pool cache learns, so a pool is judged after its one read
    and never during it. The insert uses this object's own connection with a short busy timeout:
    a held write lock keeps the item for the next tick instead of aging the legs and balances the
    same thread refreshes.
    """

    def __init__(self, *, pools, conn_factory, max_age_s, clock=time, max_waiting=64, wake=None):
        import queue
        self.wake = wake                      # the on-chain lane's wake; set after a stored row
        self.pools = pools                    # the PumpSwap pool cache, or None (curve only)
        self.conn_factory = conn_factory
        self.max_age_s = float(max_age_s)     # the on-chain lane's own freshness bound
        self.clock = clock
        self.max_waiting = int(max_waiting)
        self._offers = queue.SimpleQueue()
        self._waiting = []                    # prefetch thread only
        self._conn = None

    def offer(self, buy, reason, sol_usd):
        self._offers.put((buy, reason, sol_usd))

    def offer_busy(self, buy, sol_usd):
        """A known, sizeable PumpSwap arriving during confirmation takes the existing fallback.

        Pure cached facts and one queue offer only: no miss learning, DB write, RPC or lock on
        the socket. The ordinary executor still blocks on its existing wallet execution lock.
        """
        if getattr(buy, "venue", None) != "pump_amm" or self.pools is None:
            return False
        cached = getattr(self.pools, "cached", None)
        if cached is None:
            return False
        pool = cached(getattr(buy, "pool", None))
        if pool is None:
            return False
        reason, facts = amm_market_facts(buy, self.pools, sol_usd, cached_pool=pool)
        if reason is not None or not facts.get("their_usd", 0) > 0:
            return False
        self.offer(buy, "slot_lane_wallet_busy", sol_usd)
        return True

    def flush(self):
        """Settle every waiting buy that can be settled now. Returns one dict per settled buy."""
        import queue
        while True:
            try:
                self._waiting.append(self._offers.get_nowait())
            except queue.Empty:
                break
        done, keep = [], []
        for item in self._waiting:
            outcome, mint = self._settle(*item)
            if outcome is None:
                keep.append(item)
            else:
                done.append(self._result(item[0], mint, outcome))
        while len(keep) > self.max_waiting:
            done.append(self._result(keep.pop(0)[0], None, "overflow"))
        self._waiting = keep
        return done

    @staticmethod
    def _result(buy, mint, outcome):
        return {"signature": buy.signature, "handle": getattr(buy, "handle", None),
                "mint": mint, "outcome": outcome}

    def _settle(self, buy, reason, sol_usd):
        """(outcome, mint); outcome None keeps the buy waiting for its pool or the write lock."""
        if self.clock.monotonic() - float(getattr(buy, "recv_monotonic", 0.0) or 0.0) > self.max_age_s:
            return "expired", getattr(buy, "mint", None)
        venue = getattr(buy, "venue", "pump_curve")
        pool = getattr(buy, "pool", None)
        mint, size, price = getattr(buy, "mint", None), None, None
        if venue == "pump_amm":
            state = self.pools.state(pool) if self.pools is not None and pool else "unknown"
            if state == "queued":
                return None, None
            if state != "known":
                return "pool_unlearnable", None
            mint = (self.pools.get(pool) or {}).get("base_mint")
            why, market = amm_market_facts(buy, self.pools, sol_usd)
            if why is None and market.get("their_usd", 0) > 0:
                size, price = market["their_usd"], market["price_usd"]
        if not mint:
            return "mint_unknown", None
        if size is None:
            # AN UNSIZED ROW IS NOT HANDED OFF (2026-09-27, Gamma's triage 7). It can only fail the
            # ratio floor, and written first it displaced the watcher's VALUED row for the same
            # signature (INSERT OR IGNORE): notdecu BAfpFwAh, 5 of 12 non-SOL-quote buys. The lane's
            # own refusal line keeps the signal; the watcher's row, when there is one, is judged.
            return "skipped_unsized", mint
        valuation = {"basis": HANDOFF_BASIS, "reason": reason, "venue": venue, "pool": pool,
                     "source_slot": int(buy.slot), **recv_slot_fields(buy), "sol_usd": sol_usd,
                     "source_size_usd": size, "source_price": price}
        from sniper.services.memecoin_source_initiation import solana_program_initiation
        valuation['source_initiation'] = solana_program_initiation(buy, mint)
        if valuation['source_initiation'] is None:
            return 'awaiting_signer_proof', mint
        conn = self._connection()
        try:
            stored = m.record_onchain_detection(
                conn, signature=buy.signature, source_user_id=buy.source_user_id,
                handle=getattr(buy, "handle", None), address=buy.owner, mint=mint,
                detected_at=buy.recv_utc, created_at=utc_now_iso(), source_size_usd=size,
                source_price=price, source_slot=buy.slot,
                source_valuation_json=json.dumps(valuation, separators=(",", ":")))
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "locked" in str(exc) or "busy" in str(exc):
                return None, mint
            logger.exception("slot lane handoff write failed sig=%s mint=%s", buy.signature, mint)
            return "write_failed", mint
        except Exception:
            conn.rollback()
            logger.exception("slot lane handoff write failed sig=%s mint=%s", buy.signature, mint)
            return "write_failed", mint
        if stored and self.wake is not None:
            self.wake.set()                   # the lane sweeps this row now, not at its next poll
        return ("inserted" if stored else "duplicate"), mint

    def _connection(self):
        if self._conn is None:
            self._conn = self.conn_factory()
            self._conn.execute("PRAGMA busy_timeout=%d" % HANDOFF_BUSY_TIMEOUT_MS)
        return self._conn


CAP_LIFT_LOGGED = set()


def log_cap_lift_once(source_user_id, price_limit, override):
    """Once per listed cap-lift wallet per process, on its first slot build: the cap the lane used
    and the min-out bound it signed, as a multiple of the source's average (2.5 at 150), so the first
    grade can confirm the lane signed what the spec says (Alpha, 2026-10-09). Never raises."""
    uid = str(source_user_id or "").lower()
    if not uid or uid in CAP_LIFT_LOGGED or uid not in cfg.MEMECOIN_REAL_COPY_PREMIUM_LIFT_WALLETS:
        return None
    try:
        dec = int(price_limit["base_decimals"])
        paid_per_token = (int(price_limit["cost_usdc_atomic"]) / 1e6) / (int(price_limit["min_tokens_atomic"]) / 10 ** dec)
        bound = paid_per_token / float(price_limit["source_average_price_usd"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        bound = None
    CAP_LIFT_LOGGED.add(uid)
    line = ("[memecoin] cap lift first build wallet=%s cap_used=%s band_used=%s min_out=%s "
            "min_out_bound_x_source_avg=%s" % (
                uid, (override or {}).get("cap_used"), (override or {}).get("band_used"),
                (price_limit or {}).get("min_tokens_atomic"), None if bound is None else round(bound, 4)))
    logger.warning(line)
    return line


def cu_entry_key(cls):
    """The class's own table entry as a string (n, recorded max, sorted token programs). A reviewed
    table change gives a new key, so an older demotion row stops applying only then."""
    n, mx, programs = cfg.SLOT_LANE_CU_CLASSES[cls]
    return "%d:%d:%s" % (n, mx, ",".join(sorted(programs)))


def _cu_class_key(cls):
    return "/".join(str(part) for part in cls)


def cu_demotion_writer(conn_factory, *, alert_fn):
    """The lane's demotion sink: `sink(cls, row)` pages and writes the ledger row on a daemon thread
    with its own connection, and returns that thread. A failed write is logged and paged; the
    in-process demotion still holds."""
    def write(cls, row):
        alert_fn("slot_cu_demoted", "class %s demoted to the lane default %d: %s ran out of compute "
                 "units at %s" % (_cu_class_key(cls), cfg.MEMECOIN_SLOT_LANE_CU_LIMIT, row.get("sig"),
                                  row.get("signed_limit")))
        try:
            conn = conn_factory()
            try:
                m.record_cu_demotion(conn, class_key=_cu_class_key(cls), entry_key=cu_entry_key(cls), **row)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.exception("slot lane CU demotion write failed class=%s sig=%s", _cu_class_key(cls),
                             row.get("sig"))
            alert_fn("slot_cu_demotion_write_failed", "class %s stays demoted in this process only: "
                     "its ledger row was not written, so a restart re-arms it" % _cu_class_key(cls))

    def sink(cls, row):
        thread = threading.Thread(target=write, args=(cls, dict(row)), name="slot-lane-cu-demotion",
                                  daemon=True)
        thread.start()
        return thread
    return sink


def load_cu_demotions(conn_factory, *, alert_fn):
    """Load persisted demotions of the CURRENT table entries into CU_DEMOTED; returns the classes.
    Run before the lane thread starts. A ledger without the table is a known zero. Any other failure
    demotes every class (the default limit is the safe side) and pages once."""
    from sniper.execution.pump_curve_buy import CU_DEMOTED
    current = {(_cu_class_key(cls), cu_entry_key(cls)): cls for cls in cfg.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS}
    try:
        conn = conn_factory()
        try:
            rows = m.cu_demotion_rows(conn)
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            return _demote_all(CU_DEMOTED, alert_fn, exc)
        rows = []
    except Exception as exc:
        return _demote_all(CU_DEMOTED, alert_fn, exc)
    demoted = {current[key] for key in rows if key in current}
    CU_DEMOTED.update(demoted)
    return demoted


def _demote_all(demoted_set, alert_fn, exc):
    logger.exception("slot lane CU demotion load failed: every class signs the lane default")
    every = set(cfg.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS)
    demoted_set.update(every)
    alert_fn("slot_cu_demotion_load_failed", "every CU class signs the lane default %d this run: the "
             "demotion load failed (%s)" % (cfg.MEMECOIN_SLOT_LANE_CU_LIMIT, str(exc)[:120]))
    return every


def record_slot_build(trace, detail, *, duration_ms):
    """Mark the slot_build substage, with the prefetched leg's age when the build reports one, and
    return the build detail WITHOUT that age: the executor reads the same validity dict as before.
    A refused build's detail is its reason string and passes through untouched."""
    extra = {}
    if isinstance(detail, dict) and "leg_age_s" in detail:
        detail = dict(detail)
        age = detail.pop("leg_age_s")
        if age is not None:
            extra["leg_age_s"] = age
    if isinstance(detail, dict) and "cu_limit" in detail:
        # The compute-unit limit the build signed (per-class table, 2026-10-02): recorded, never a
        # validity field.
        detail = dict(detail)
        extra["cu_limit"] = detail.pop("cu_limit")
        if "cu_class" in detail:
            cls = detail.pop("cu_class")
            extra["cu_class"] = "/".join(cls) if cls else None
        if "cu_key" in detail:
            # What the class table was asked (venue, funding, leg dex, coin token program), on every
            # build, so a default build's class can be justified later from the ledger alone.
            key = detail.pop("cu_key") or (None, None, None, None)
            extra.update(zip(("cu_venue", "cu_funding", "cu_leg_dex", "cu_token_program"), key))
            if extra.get('cu_funding') == 'native':
                extra['cu_native_variant'] = extra['cu_leg_dex']
                extra['cu_leg_dex'] = None
    if isinstance(detail, dict) and 'priority_bid' in detail:
        detail = dict(detail)
        extra['priority_bid'] = detail.pop('priority_bid')
    if isinstance(detail, dict) and ("leg_hops" in detail or "cu_estimate" in detail):
        # An exotic leg's composition (settings.slot_lane_exotic_cu, 2026-10-07): trace only.
        detail = dict(detail)
        extra["leg_hops"] = detail.pop("leg_hops", None)
        extra["cu_estimate"] = detail.pop("cu_estimate", None)
    trace.mark_substage("slot_build", duration_ms=duration_ms, **extra)
    return detail


def routed_mode():
    """MEMECOIN_SLOT_LANE_ROUTED as one of ROUTED_MODES; anything unrecognised is "off"."""
    mode = str(getattr(cfg, "MEMECOIN_SLOT_LANE_ROUTED", "") or "").strip().lower()
    return mode if mode in ROUTED_MODES else "off"


def routed_log_line(buy, mode):
    """One line per paired routed buy. recv= and sig= let a reader join it to the watcher's row."""
    return ("[memecoin] slot lane routed %s %s %s S=%d sig=%s usdc=%.2f tokens=%d recv=%s "
            "pair_ms=%.1f" % (mode, buy.handle, str(buy.mint)[:8], buy.slot, buy.signature,
                              buy.usdc_spent_atomic / 1e6, buy.token_delta_atomic, buy.recv_utc,
                              (buy.paired_monotonic - buy.recv_monotonic) * 1000.0))


def resume_refused(why, sig, age_s):
    """One line per receipt resume the lane did not take, named, so the fallback's S+n is explained."""
    logger.warning("[memecoin] slot lane receipt resume refused %s age_ms=%s sig=%s", why,
                   "%.0f" % (age_s * 1000.0) if age_s is not None else None, sig)


def receipt_note(what, sig):
    """One line per receipt-handoff path that neither resumes nor refuses a held buy (Alpha 2026-10-15:
    8 of 16 lost routed buys had no line naming the check). Apart from resume_refused, whose line
    the lost-buy reads count."""
    logger.warning("[memecoin] slot lane receipt note %s sig=%s", what, sig)


# Why the latest offer for a signature cached no proof, read by remember's line. Bounded like the cache.
PROOF_NOTE_TTL_S = 5.0
_KEY_FIELDS = ("signature", "owner", "user", "mint", "slot")


@dataclass(frozen=True)
class ReceiptResume:
    event: object
    proof_ready_mono: float

    @property
    def signature(self):
        return self.event.source_initiation['tx_hash']


class RoutedHandoff:
    """Resume a cached native route once the watcher supplies its exact signer-bound receipt."""

    def __init__(self, *, conn_factory, wake, put=None, clock=time, owner_guard=None, proof_put=None):
        self.conn_factory, self.wake, self._conn = conn_factory, wake, None
        self.put, self.clock, self.awaiting = put, clock, PendingLegs()
        self.owner_guard, self.proof_put = owner_guard, proof_put
        self._early_proofs = {}  # exact identity -> (event, expiry); awaiting's existing lock
        self._proof_notes = {}   # signature -> (why no proof is cached, expiry); the same lock

    def _note_proof(self, sig, why, now):
        # Called under awaiting's lock.
        self._proof_notes.pop(sig, None)
        self._proof_notes[sig] = (why, now + PROOF_NOTE_TTL_S)
        while len(self._proof_notes) > 64:
            self._proof_notes.pop(next(iter(self._proof_notes)))

    def _expire_proofs(self, now):
        # Called under awaiting's lock, only on background proof or already-refused candidate paths.
        for key, (_, deadline, _) in list(self._early_proofs.items()):
            if now >= deadline:
                self._early_proofs.pop(key, None)
                self._note_proof(key[0], "expired", now)

    def _missing_proof(self, key, now):
        """Why no early proof matched `key` (under awaiting's lock): a cached proof of the same
        signature whose identity differs, the latest offer's own reason, or none arrived yet."""
        for other in self._early_proofs:
            if other[0] == key[0]:
                return "mismatch:" + ",".join(name for name, a, b in zip(_KEY_FIELDS, key, other) if a != b)
        why, deadline = self._proof_notes.pop(key[0], (None, 0.0))
        return why if why is not None and now < deadline else "none"

    def remember(self, buy):
        now = self.clock.monotonic()
        self.awaiting.expired(now)
        with self.awaiting._lock:
            self._expire_proofs(now)
        skipped = ("no_put" if self.put is None
                   else "not_routed" if getattr(buy, 'routed', None) is None
                   else "has_proof" if getattr(buy, 'source_initiation', None) is not None
                   else "awaiting_full" if len(self.awaiting) >= 64 else None)
        if skipped is None and not self.awaiting.add(buy, deadline=buy.recv_monotonic + MAX_SIGNAL_AGE_S):
            skipped = "duplicate"
        if skipped is not None:
            receipt_note("remember_skipped=%s" % skipped, getattr(buy, 'signature', None))
            return
        key = (buy.signature, buy.owner, buy.source_user_id, buy.mint, buy.slot)
        with self.awaiting._lock:
            cached = self._early_proofs.pop(key, None)
            missing = self._missing_proof(key, now) if cached is None else None
        if cached is None:
            receipt_note("awaiting early=%s" % missing, buy.signature)
        elif self.proof_put is not None:
            self.proof_put(ReceiptResume(cached[0], cached[2]))
        else:
            self.offer(cached[0], proof_ready_mono=cached[2])

    def offer(self, ev, *, proof_ready_mono=None):
        from sniper.services.memecoin_source_initiation import (
            DIRECT_PROOF_VERSION, _quantity, source_initiation_refusal)
        proof = getattr(ev, 'source_initiation', None)
        early = ("no_put" if self.put is None
                 else "not_solana" if getattr(ev, 'network_id', None) != 'solana'
                 else "no_proof" if not isinstance(proof, dict)
                 else "proof_version" if proof.get('proof_version') != DIRECT_PROOF_VERSION
                 else "initiation_refused" if source_initiation_refusal(ev, 'solana') is not None
                 else None)
        if early is not None:
            # A buy the lane is holding for this proof is a refused resume (Alpha 2026-10-07); any other
            # is a note (~80 Solana detections a day), which the candidate's remember line repeats.
            held = str(getattr(ev, 'fomo_event_id', None) or '').split(':')[-1]
            now = self.clock.monotonic()
            if held and self.awaiting.holds(held, now):
                resume_refused(early, held, None)
            else:
                with self.awaiting._lock:
                    self._note_proof(held, "skipped:" + early, now)
                receipt_note("offer_skipped=%s" % early, held)
            return False
        now, sig = self.clock.monotonic(), proof['tx_hash']
        if self.owner_guard is not None:
            try:
                owned = self.owner_guard(sig)
            except Exception:
                logger.exception('native receipt owner guard failed sig=%s', sig)
                owned = True
            if owned:
                resume_refused('native_owned', sig, None)
                return False
        if proof_ready_mono is None:
            proof_ready_mono = getattr(ev, 'proof_ready_monotonic', None)
        if proof_ready_mono is None:
            proof_ready_mono = now
        self.awaiting.expired(now)
        key = (sig, ev.source_wallet, ev.source_user_id, ev.mint, ev.source_block_number)
        # Publish before claiming; concurrent preparation can consume a proof-first receipt.
        # Valid signer proof still requires the original processed candidate's 500 ms clock.
        with self.awaiting._lock:
            self._expire_proofs(now)
            released_at, _ = self.awaiting._released.get(sig, (None, None))
            if released_at is None or now - released_at >= self.awaiting.RELEASE_TTL_S:
                self._early_proofs.setdefault(key, (ev, now + MAX_SIGNAL_AGE_S, proof_ready_mono))
                while len(self._early_proofs) > 64:
                    self._early_proofs.pop(next(iter(self._early_proofs)))
        buy = self.awaiting.claim(sig, now)
        if buy is None:
            how = self.awaiting.release_kind(sig, now)
            if how is not None:
                # "already_offered": an earlier offer took this buy (resumed, or refused and named
                # then), e.g. the mechanical fallback's own resume call after the lane's build aborted
                # (9vCmcifh 01:39Z read as a loss). "aged_out": remembered, gone before the receipt came.
                resume_refused("already_offered" if how == "claimed" else "aged_out", sig, None)
            else:
                receipt_note("early_proof_cached", sig)   # proof first: the candidate's remember takes it
            return False
        with self.awaiting._lock:
            self._early_proofs.pop(key, None)
        # Named (2026-10-06 late fills: two OmakaseOnly routed buys refused missing_receipt_proof
        # filled by the ordinary fallback at S+4, and nothing said which check stopped the resume).
        why = ("age" if now - buy.recv_monotonic > MAX_SIGNAL_AGE_S
               else "owner" if buy.owner != ev.source_wallet
               else "user" if buy.source_user_id != ev.source_user_id
               else "mint" if buy.mint != ev.mint
               else "slot" if buy.slot != ev.source_block_number
               # The built venue leg is PART of the proven acquisition (a split route's leg is smaller:
               # KKK F4g7BYDm 22:30Z, PumpSwap 17.7T of 18.3T acquired). The whole acquisition and the
               # payment still match the routed receipt exactly, below.
               else "acquired" if not (type(buy.token_amount) is int
                                       and 0 < buy.token_amount <= int(proof['acquired_atomic']))
               else "acquired_routed" if int(proof['acquired_atomic']) != buy.routed.token_delta_atomic
               else "payment" if int(proof['payment_atomic']) != buy.routed.usdc_spent_atomic
               else "pending_full" if len(CONFIRMED_PENDING) >= 64 else None)
        if why is not None:
            resume_refused(why, sig, now - buy.recv_monotonic)
            return False
        # Gamma's N1 residual 1 (2026-10-07): the whole-receipt average that caps the copy premium
        # counts only the USDC that went INTO the route (route_payment_atomic, from the receipt), not
        # platform fees or unrelated outflows. Unbound with the cap on: no resume.
        route = proof.get('route_payment_atomic')
        routed = buy.routed
        route = _quantity(route)
        if route is not None and 0 < route <= buy.routed.usdc_spent_atomic:
            routed = dataclasses.replace(buy.routed, usdc_spent_atomic=route)
        elif (cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT > 0
              or getattr(buy.routed, 'requires_receipt_proof', False)):
            # The class (EVM-03, Alpha 2026-10-07), so the forward coverage loss is countable.
            why = proof.get('route_payment_unknown') or ('out_of_range' if route is not None else 'missing')
            resume_refused("route_payment_unknown:%s" % str(why)[:48], sig, now - buy.recv_monotonic)
            return False
        resumed = dataclasses.replace(
            buy, source_initiation=dict(proof), routed=routed,
            proof_ready_monotonic=proof_ready_mono,
            proof_provenance=getattr(ev, "provenance", None)
            or (getattr(ev, "source_evidence", None) or {}).get("source_clock_provenance")
            or "confirmed_receipt")
        # Queue ownership ends after the native result is durable. Expiring it while a shell-row
        # write waits would let the ordinary lane race a native send. Original receipt age still
        # gates native admission; restart drops this hold and uses the durable pending row.
        if not CONFIRMED_PENDING.add(resumed, deadline=math.inf):
            resume_refused("pending_duplicate", sig, now - buy.recv_monotonic)
            return False
        try:
            self.put(resumed)
        except Exception:
            self.finish(resumed)
            raise
        return True

    def finish(self, buy):
        if CONFIRMED_PENDING.claim(buy.signature, self.clock.monotonic()) is not None and self.wake is not None:
            self.wake.set()

    def write(self, buy):
        # Processed balance pairs cannot prove the owner's signature. Writing first would consume
        # the unique signature and displace the confirmed watcher's signer-bound receipt.
        return "awaiting_signer_proof"


def cpmm_slot_buy(routed):
    """The routed buy as a raydium_cpmm SlotBuy when its own logs frame carried exactly one CPMM swap
    into its coin; else None. Pure. The quote asset is NOT filtered here: admission refuses an
    unsupported one by name (slot_lane_cpmm_quote_not_supported), so it reaches the shadow line.
    Filtering here made it silent: ResellCalendar's three CPMM buys of AjxqTiS6 (2026-09-27
    22:58-23:13Z) were ANTHROPIC-quoted and left no line and no counter."""
    from sniper.services.memecoin_slot_signal import SlotBuy
    swaps = [e for e in getattr(routed, "venue_events", ()) or ()
             if getattr(e, "output_mint", None) == routed.mint and getattr(e, "input_mint", None)]
    if len(swaps) != 1:
        return None
    ev = swaps[0]
    # sol_amount holds the source's net input in QUOTE atomic units (lamports or USDC), as a record;
    # CPMM admission prices from the event itself (raydium_cpmm_buy.market_facts).
    return SlotBuy(venue="raydium_cpmm", source_user_id=routed.source_user_id, handle=routed.handle,
                   owner=routed.owner, signature=routed.signature, slot=routed.slot,
                   mint=ev.output_mint, pool=ev.pool,
                   sol_amount=ev.input_amount, token_amount=ev.output_amount - ev.output_transfer_fee,
                   virtual_sol_reserves=None, virtual_token_reserves=None,
                   real_sol_reserves=ev.input_vault_before, real_token_reserves=ev.output_vault_before,
                   token_program=None, trade_event_log="", recv_monotonic=routed.recv_monotonic,
                   recv_utc=routed.recv_utc, signal_feed=getattr(routed, "signal_feed", "helius"),
                   venue_event=ev, routed=routed, quote_mint=ev.input_mint)


def pump_slot_buy(routed, amm_pools):
    """(SlotBuy, None) when the routed buy's own frame carried exactly one pump buy event into its
    coin: a curve event for that mint, or a PumpSwap event whose pool the lane's cache knows as that
    coin's migrated wSOL pool. (None, reason) when a pump event is there but does not qualify;
    (None, None) when the frame has none. No I/O: a cache miss only queues the pool for learning."""
    from sniper.execution.pump_curve_buy import WSOL_MINT
    from sniper.services.memecoin_slot_signal import RoutedPumpBuy, SlotBuy
    events = [e for e in getattr(routed, "venue_events", ()) or () if isinstance(e, RoutedPumpBuy)]
    if not events:
        return None, None
    into_coin = []
    for ev in events:
        fields = dict(ev.fields)
        if ev.venue == "pump_curve":
            if fields["mint"] == routed.mint:
                into_coin.append((ev, fields, None))
            continue
        facts = amm_pools.get(fields["pool"]) if amm_pools is not None else None
        if facts is None:
            return None, "pool_unknown"       # cannot tell whether it is the coin's pool
        if facts.get("base_mint") == routed.mint:
            into_coin.append((ev, fields, facts))
    if not into_coin:
        return None, "no_pump_event_into_coin"
    if len(into_coin) > 1:
        return None, "multi_pump_event"
    (ev, fields, facts), = into_coin
    # The lane's own static pool refusals, checked here so a buy it would refuse stays on the hand-off.
    # A non-SOL pool goes to the lane only with "pump_amm" listed live (E2 exotic quotes).
    if facts is not None and facts.get("quote_mint") != WSOL_MINT and not exotic_live("pump_amm"):
        return None, "pump_quote_not_sol"
    if facts is not None and not facts.get("pump_migrated"):
        return None, "pump_pool_not_migrated"
    fields["mint"] = routed.mint
    return SlotBuy(source_user_id=routed.source_user_id, handle=routed.handle, owner=routed.owner,
                   signature=routed.signature, slot=routed.slot, token_program=ev.token_program,
                   trade_event_log=ev.trade_event_log, recv_monotonic=routed.recv_monotonic,
                   recv_utc=routed.recv_utc, signal_feed=getattr(routed, "signal_feed", "helius"), routed=routed,
                   quote_mint=facts.get("quote_mint") if facts is not None else None, **fields), None


def launchlab_slot_buy(routed, pools=None):
    """The routed buy as a raydium_launchlab SlotBuy when its own logs frame carried exactly one
    LaunchLab buy; else None. No network: the event names its pool, not its coin or quote, so the
    quote mint comes from `pools` (the LaunchLab pool cache) when it already holds the pool (a miss
    is queued there, and the buy's quote stays None: shadow). Admission checks the pool's base mint
    against the routed coin (raydium_launchlab_buy.market_facts)."""
    from sniper.execution.raydium_launchlab_buy import LaunchLabTrade
    from sniper.services.memecoin_slot_signal import SlotBuy
    trades = [e for e in getattr(routed, "venue_events", ()) or ()
              if isinstance(e, LaunchLabTrade) and e.is_buy]
    if len(trades) != 1:
        return None
    ev = trades[0]
    known = pools.get(ev.pool) if pools is not None else None
    # sol_amount holds the source's input in QUOTE atomic units after the quote's transfer fee, as
    # a record; admission prices from the event itself.
    return SlotBuy(venue="raydium_launchlab", source_user_id=routed.source_user_id,
                   handle=routed.handle, owner=routed.owner, signature=routed.signature,
                   slot=routed.slot, mint=routed.mint, pool=ev.pool, sol_amount=ev.amount_in,
                   token_amount=ev.amount_out, virtual_sol_reserves=None, virtual_token_reserves=None,
                   real_sol_reserves=ev.real_quote_before, real_token_reserves=ev.real_base_before,
                   token_program=None, trade_event_log="", recv_monotonic=routed.recv_monotonic,
                   recv_utc=routed.recv_utc, signal_feed=getattr(routed, "signal_feed", "helius"), venue_event=ev, routed=routed,
                   quote_mint=known["pool"].quote_mint if known else None)


def make_learner_handoff(*, live, routed_handoff, handoff, sol_usd_fn):
    """The MissLearner's hand-off, as start_slot_lane wires it. A live ROUTED buy (a CPMM buy, or a
    B1 RoutedPoolMiss) writes its routed row, its one other funded path; any other buy goes to the
    ordinary hand-off. Shadow writes nothing. Module-level so the production branch is what the
    tests drive (Gamma MEDIUM on d5c2e223f: a hand-made copy hid a deleted write)."""
    def learner_handoff(buy, reason):
        if not live:
            return
        if getattr(buy, "routed", None) is not None and not shadow_only(buy):
            if routed_handoff is not None:            # a live routed buy: its routed row, once
                routed_handoff.write(buy.routed)
                if reason == "pool_unknown":
                    # Codex N5 (2026-10-06): a pool the learner could not read in time, or at all,
                    # is the same mechanical hand-off as an immediate pool miss, so it publishes the
                    # same marker (slot_lane_verdict -> routed_handoff) and wakes the on-chain lane.
                    # A conversion the learned pool refused keeps its own reason, unmarked.
                    ROUTED_HANDOFFS.mark(buy.routed.signature, time.monotonic())
                    wake = getattr(routed_handoff, "wake", None)
                    if wake is not None:
                        wake.set()
        elif handoff is not None:
            handoff.offer(buy, reason, sol_usd_fn())
    return learner_handoff


class RoutedPoolMiss:
    """B1 (2026-10-01): a routed pump buy whose PumpSwap pool the cache did not know, offered to the
    MissLearner. It carries no buy of its own: once the pool is read, `convert` runs pump_slot_buy on
    the routed buy again, with every check (the coin's pool, a SOL quote, migrated). Its `routed`
    lets the learner's hand-off write the routed row, the buy's one other path."""
    venue = "pump_amm"

    def __init__(self, routed, pool, amm_pools):
        self.routed, self.pool, self.amm_pools = routed, pool, amm_pools
        self.signature, self.handle, self.slot = routed.signature, routed.handle, routed.slot

    def convert(self):
        return pump_slot_buy(self.routed, self.amm_pools)


def damm_first_sight_on():
    """MEMECOIN_SLOT_LANE_DAMM_V2_MISS_LEARN, and only while DAMM v2 itself is live."""
    from sniper.execution.meteora_damm_v2_buy import damm_live
    return bool(getattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2_MISS_LEARN", False)) and damm_live()


def damm_route_targets_reader(readonly_conn_factory):
    """The DAMM learner owns this bounded read; every scan closes its observation-only handle."""
    from contextlib import closing
    from sniper.persistence.db_memecoin import recent_armed_solana_route_targets

    def read():
        with closing(readonly_conn_factory()) as conn:
            return recent_armed_solana_route_targets(conn)
    return read


def damm_first_sight_learner(damm_pools, *, sol_usd_fn, log, env=None):
    """The MissLearner's DAMM v2 venue (a DammV2MintLearner on its OWN ONCHAIN_WATCH_HELIUS_RPC_URL
    client, operator provider choice via Alpha 2026-10-07), or None: knob off, DAMM not live, no
    pool cache, or no usable endpoint (missing, or carrying the funded key: one log line)."""
    if damm_pools is None or not damm_first_sight_on():
        return None
    import os
    from sniper.execution.meteora_damm_v2_buy import DammV2MintLearner, mint_learner_rpc
    env = os.environ if env is None else env
    try:
        from sniper.services.memecoin_solana_provider import provider_urls
        from sniper.services.memecoin_research_rpc import funded_dependency_url
        funded_read = funded_dependency_url('pool_first_sight', env)
        rpc = mint_learner_rpc(funded_read or next(iter(provider_urls('signal', env)), ''),
                               **({"funded_purpose": True, "env": env} if funded_read is not None else {}))
    except ValueError as exc:
        log("[memecoin] slot lane damm_v2 first sight OFF: %s" % exc)
        return None
    log("[memecoin] slot lane damm_v2 first sight ON (min depth $%.0f)"
        % cfg.MEMECOIN_SLOT_LANE_DAMM_V2_MISS_MIN_DEPTH_USD)
    return DammV2MintLearner(damm_pools, rpc, sol_usd_fn=sol_usd_fn,
                             min_depth_usd=cfg.MEMECOIN_SLOT_LANE_DAMM_V2_MISS_MIN_DEPTH_USD, log=log)


class RoutedDammMiss:
    """A routed DAMM v2 buy whose coin the pool cache did not know, offered to the MissLearner (first
    sight, 2026-10-07). The learner's key is the COIN: its DammV2MintLearner finds the pool by mint and
    adopts it. `convert` then runs damm_slot_buy again with every check it makes; a buy the lane could
    only shadow (a quote not live) is handed off, never shadowed, so the buy keeps its one funded path.
    `quote_mint` is wSOL for the hand-off's shadow test: the learner offers only while DAMM is live and
    finds SOL pools (USDC ones only when non-SOL DAMM is listed)."""
    venue = "meteora_damm_v2"

    def __init__(self, routed, damm_pools):
        from sniper.execution.meteora_damm_v2_buy import WSOL_MINT
        self.routed, self.damm_pools, self.quote_mint = routed, damm_pools, WSOL_MINT
        self.pool = routed.mint
        self.signature, self.handle, self.slot = routed.signature, routed.handle, routed.slot

    def convert(self):
        from sniper.execution.meteora_damm_v2_buy import damm_slot_buy
        buy, why = damm_slot_buy(self.routed, self.damm_pools)
        if buy is not None and shadow_only(buy):
            return None, "damm_v2_shadow_quote"
        return buy, why


class RoutedDbcMiss:
    """A routed DBC buy, offered to the MissLearner (2026-10-07, operator order: live). The frame names
    no pool, so EVERY DBC buy is learned: the learner's key is the COIN, its DbcMintLearner finds the
    curve by mint, and `convert` builds the buy from that fresh read with every refusal (hooked coin,
    migrated, complete, rate limiter). A buy the lane could only shadow (a quote not live) is handed
    off, never shadowed. `quote_mint` is wSOL for the hand-off's shadow test."""
    venue = "meteora_dbc"

    def __init__(self, routed, dbc_learner):
        from sniper.execution.meteora_dbc_buy import WSOL_MINT
        self.routed, self.dbc_learner, self.quote_mint = routed, dbc_learner, WSOL_MINT
        self.pool = routed.mint
        self.signature, self.handle, self.slot = routed.signature, routed.handle, routed.slot

    def convert(self):
        from sniper.execution.meteora_dbc_buy import dbc_slot_buy
        buy, why = dbc_slot_buy(self.routed, self.dbc_learner.fresh(self.routed.mint))
        if buy is not None and shadow_only(buy):
            return None, "dbc_quote_not_live"
        return buy, why


def dbc_first_sight_learner(builders, *, log, env=None):
    """The MissLearner's DBC venue (a DbcMintLearner on its OWN ONCHAIN_WATCH_HELIUS_RPC_URL client,
    as DAMM v2's first sight), or None: DBC not live, no DBC builder, or no usable endpoint (missing,
    or carrying the funded key: one log line)."""
    from sniper.execution import meteora_dbc_buy
    if "meteora_dbc" not in builders or not meteora_dbc_buy.dbc_live():
        return None
    import os
    from sniper.execution.meteora_damm_v2_buy import mint_learner_rpc
    env = os.environ if env is None else env
    try:
        from sniper.services.memecoin_solana_provider import provider_urls
        from sniper.services.memecoin_research_rpc import funded_dependency_url
        funded_read = funded_dependency_url('pool_first_sight', env)
        rpc = mint_learner_rpc(funded_read or next(iter(provider_urls('signal', env)), ''),
                               **({"funded_purpose": True, "env": env} if funded_read is not None else {}))
    except ValueError as exc:
        log("[memecoin] slot lane dbc first sight OFF: %s" % exc)
        return None
    log("[memecoin] slot lane dbc first sight ON")
    return meteora_dbc_buy.DbcMintLearner(rpc, log=log)


def _unknown_pump_pool(routed, amm_pools):
    """The first PumpSwap pool in the routed frame the cache does not know, or None."""
    from sniper.services.memecoin_slot_signal import RoutedPumpBuy
    for ev in getattr(routed, "venue_events", ()) or ():
        if isinstance(ev, RoutedPumpBuy) and ev.venue == "pump_amm":
            pool = dict(ev.fields).get("pool")
            if pool and (amm_pools is None or amm_pools.get(pool) is None):
                return pool
    return None


def route_routed_buy(routed, *, handoff, log, put, amm_pools, cpmm_on, pump_on, lane_live,
                     launchlab_on=False, launchlab_pools=None, learner=None, pool_learn=False,
                     damm_on=False, damm_pools=None, dbc_learner=None):
    """_route_routed_buy, plus ONE line per DAMM v2 outcome (Alpha, 2026-10-02: a quiet B2 shadow must
    be provably quiet). damm_slot_buy runs at most once per routed buy, and its line is written AFTER
    every put and hand-off of the buy, so it never sits ahead of a fill or the on-chain hand-off."""
    if isinstance(routed, ReceiptResume):
        return ('receipt_resume', handoff.offer(routed.event, proof_ready_mono=routed.proof_ready_mono))
    memo = []

    def damm_once():
        if not memo:
            from sniper.execution.meteora_damm_v2_buy import damm_slot_buy
            try:
                memo.append(damm_slot_buy(routed, damm_pools))
            except Exception:
                logger.exception("slot lane damm_slot_buy conversion failed sig=%s", routed.signature)
                memo.append((None, "conversion_error"))
        return memo[0]
    decided = ("error", None)
    try:
        decided = _route_routed_buy(
            routed, handoff=handoff, log=log, put=put, amm_pools=amm_pools, cpmm_on=cpmm_on,
            pump_on=pump_on, lane_live=lane_live, launchlab_on=launchlab_on,
            launchlab_pools=launchlab_pools, learner=learner, pool_learn=pool_learn, damm_on=damm_on,
            damm_once=damm_once, damm_pools=damm_pools, dbc_learner=dbc_learner) or decided
        if decided[0] == "handoff" and lane_live and handoff is not None:
            ROUTED_HANDOFFS.mark(routed.signature, time.monotonic())
    finally:
        # Fill timing (2026-10-02): the lane's own decision for this source signature, one deque
        # append, written AFTER every put and hand-off like the line below.
        _skip_note(routed.signature, *decided, evidence=_receipt_facts(routed))
        if memo and memo[0] != (None, None):        # (None, None): cp-amm did not run, nothing to say
            buy, why = memo[0]
            log("[memecoin] slot lane damm_v2 %s %s %s sig=%s" % (
                "built" if buy is not None else why, routed.handle, str(routed.mint or "?")[:8],
                routed.signature))
    return decided


def _route_routed_buy(routed, *, handoff, log, put, amm_pools, cpmm_on, pump_on, lane_live,
                      launchlab_on, launchlab_pools, learner, pool_learn, damm_on, damm_once,
                      damm_pools=None, dbc_learner=None):
    """One paired routed buy on the lane's routed thread. ONE SOURCE BUY, ONE FUNDED PATH: a pump
    buy the live lane takes is not also written for the on-chain lane (the watcher's later row
    defers to the lane's `slotlane:<sig>` attempt), and neither is a CPMM buy whose quote asset is
    live (the lane writes its routed row itself if it refuses before reserving). Everything else
    goes to the hand-off as before; shadow conversions follow it."""
    try:
        buy, why = pump_slot_buy(routed, amm_pools) if pump_on else (None, None)
    except Exception:
        logger.exception("slot lane routed pump conversion failed sig=%s", routed.signature)
        buy, why = None, "pump_conversion_error"
    if buy is not None and lane_live:
        log(routed_log_line(routed, "lane"))
        put(buy)
        return "lane", "pump_amm"
    if buy is None and why == "pool_unknown" and lane_live and pool_learn and learner is not None:
        # B1: learn the pool and convert again inside the learner's budget; the learner writes the
        # routed row itself if it cannot. One source buy, one funded path either way.
        pool = _unknown_pump_pool(routed, amm_pools)
        if pool is not None and learner.offer(RoutedPoolMiss(routed, pool, amm_pools)):
            log("[memecoin] slot lane routed pump pool_unknown_learning %s pool=%s sig=%s"
                % (routed.handle, str(pool)[:8], routed.signature))
            return "learning", "pool_unknown"
    if buy is None and cpmm_on and lane_live:
        try:
            cpmm_buy = cpmm_slot_buy(routed)
        except Exception:
            logger.exception("slot lane cpmm_slot_buy conversion failed sig=%s", routed.signature)
            cpmm_buy = None
        if cpmm_buy is not None and not shadow_only(cpmm_buy):
            log(routed_log_line(routed, "lane"))
            put(cpmm_buy)
            return "lane", "raydium_cpmm"
    if buy is None and launchlab_on and lane_live:
        try:
            ll_buy = launchlab_slot_buy(routed, launchlab_pools)
        except Exception:
            logger.exception("slot lane launchlab_slot_buy conversion failed sig=%s", routed.signature)
            ll_buy = None
        if ll_buy is not None and not shadow_only(ll_buy):
            log(routed_log_line(routed, "lane"))
            put(ll_buy)
            return "lane", "raydium_launchlab"
    if buy is None and damm_on and lane_live:
        # B2: a cp-amm frame on a learned SOL pool goes to the lane when DAMM v2 is live; an unknown
        # coin is queued for learning by damm_slot_buy and handed off below as today.
        damm_buy, _why = damm_once()
        if damm_buy is not None and not shadow_only(damm_buy):
            log(routed_log_line(routed, "lane"))
            put(damm_buy)
            return "lane", "meteora_damm_v2"
        if (damm_buy is None and str(_why or "").startswith("pool_unknown") and learner is not None
                and damm_first_sight_on() and learner.offer(RoutedDammMiss(routed, damm_pools))):
            # FIRST SIGHT (2026-10-07): the frame names no pool, so the learner finds it by mint inside
            # its budget and retries once; past the budget, or refused, it hands off as before.
            log("[memecoin] slot lane routed damm_v2 first_sight_learning %s %s sig=%s"
                % (routed.handle, str(routed.mint or "?")[:8], routed.signature))
            return "learning", "pool_unknown"
    if (buy is None and dbc_learner is not None and lane_live and learner is not None
            and any(type(e).__name__ == "DbcSeen" for e in getattr(routed, "venue_events", ()) or ())
            and learner.offer(RoutedDbcMiss(routed, dbc_learner))):
        # DBC LIVE (2026-10-07): after every other venue, so none of them waits on it. The frame names
        # no pool: the learner reads the curve by mint inside its budget and converts once; past the
        # budget, or refused, it hands off as before.
        log("[memecoin] slot lane routed dbc learning %s %s sig=%s"
            % (routed.handle, str(routed.mint or "?")[:8], routed.signature))
        return "learning", "pool_unknown"
    handle_routed(routed, handoff, log)
    if why:
        log("[memecoin] slot lane routed pump %s %s sig=%s" % (why, routed.handle, routed.signature))
    if buy is not None:
        put(buy)                              # the lane is shadow: it only builds and times it
        return "handoff", "lane_shadow"
    def damm_convert(_r):
        return damm_once()[0]
    for on, convert in ((cpmm_on, cpmm_slot_buy),
                        (launchlab_on, lambda r: launchlab_slot_buy(r, launchlab_pools)),
                        (damm_on, damm_convert)):
        if not on:
            continue
        try:
            slot_buy = convert(routed)
        except Exception:
            logger.exception("slot lane routed conversion failed sig=%s", routed.signature)
            slot_buy = None
        # Shadow conversions only (Gamma LOW-1, 2026-10-06): a pool learned between the first
        # conversion and this one could make a live buy here, a native send on a buy this
        # function hands off (and marks), so the on-chain lane would buy it too.
        if slot_buy is not None and (not lane_live or shadow_only(slot_buy)):
            put(slot_buy)
    return "handoff", why or "no_slot_venue"


def handle_routed(buy, handoff, log):
    """One paired routed buy on the lane's routed thread: shadow logs it; live writes it first."""
    try:
        if handoff is None:
            log(routed_log_line(buy, "shadow"))
        else:
            log(routed_log_line(buy, "live") + " -> " + handoff.write(buy))
    except Exception:
        logger.exception("slot lane routed handling failed sig=%s", buy.signature)


def token_accounts_baseline(rpc_url, *, timeout_s=10.0):
    """baseline_fn for the routed pairer: an owner's token accounts per token program (jsonParsed,
    processed) over HTTP on the lane's OWN key, the one its socket uses. NOT the keyless read RPC:
    api.mainnet-beta.solana.com is the funded runner's Solana read lane on this IP (tickstream, exit
    tape), and 2 req/s there drew 429s within 4 minutes on 2026-09-17. A JSON-RPC error or a
    missing result RAISES: an unread baseline is unknown, never empty."""
    import urllib.request
    from sniper.services.memecoin_slot_signal import ROUTED_PROGRAMS

    def read(owner):
        out = {}
        for kind, program in ROUTED_PROGRAMS:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                               "params": [owner, {"programId": program},
                                          {"encoding": "jsonParsed", "commitment": "processed"}]})
            req = urllib.request.Request(rpc_url, body.encode(), {
                "Content-Type": "application/json", "User-Agent": "curl/8.5.0"})
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                reply = json.loads(resp.read())
            result = reply.get("result") if isinstance(reply, dict) else None
            if not isinstance(result, dict) or reply.get("error") is not None:
                raise ValueError("getTokenAccountsByOwner unread: %s" % (
                    reply.get("error") if isinstance(reply, dict) else type(reply).__name__,))
            out[kind] = (int(result["context"]["slot"]), {
                a["pubkey"]: int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
                for a in result["value"]})
        return out
    return read


# PREFETCH TELEMETRY (spec 2026-10-01 slot lane prefetch cadence, step 1). The prefetch thread
# logs every failed input, a line for any loop slower than PREFETCH_SLOW_LOOP_MS, and one report
# every PREFETCH_REPORT_S. Telemetry only: it never changes the snapshot, the cadence or the lane.
PREFETCH_REPORT_S = 600.0
PREFETCH_SLOW_LOOP_MS = 2000.0
PREFETCH_PHASES = ("roster", "sol_usd", "usdc", "sol", "blockhash", "extra")
PREFETCH_AGE_INPUTS = ("balances", "blockhash", "sol_usd")
PREFETCH_FAILABLE = ("usdc", "sol", "blockhash", "sol_usd")


def prefetch_report_line(report):
    work, phase, ages, failed = (report["work_ms"], report["phase_max_ms"], report["age_max_s"],
                                 report["failed"])
    return ("[memecoin] slot lane prefetch loops n=%d work_ms p50=%.0f p90=%.0f p99=%.0f max=%.0f "
            "phase_max_ms %s age_max_s %s failed %s" % (
                report["loops"], work["p50"], work["p90"], work["p99"], work["max"],
                " ".join("%s=%.0f" % (k, phase[k]) for k in PREFETCH_PHASES),
                " ".join("%s=%s" % (k, "never" if ages[k] is None else "%.1f" % ages[k])
                         for k in PREFETCH_AGE_INPUTS),
                " ".join("%s=%d" % (k, failed[k]) for k in PREFETCH_FAILABLE))) + _extra_worst_text(report) \
        + _usdc_read_text(report)


def _usdc_read_text(report):
    """' usdc_read ata n=.. p50=.. p90=.. scan n=.. p50=.. p90=..' (2026-10-07: the bankroll read's
    paths, so the ATA read's gain is measured on this loop); '' when the reader keeps none."""
    paths = report.get("usdc_read_ms")
    if not paths:
        return ""
    parts = []
    for name in ("ata", "scan"):
        ms = sorted(paths.get(name) or ())
        if ms:
            parts.append("%s n=%d p50=%.0f p90=%.0f" % (
                name, len(ms), ms[len(ms) // 2], ms[min(len(ms) - 1, int(0.9 * len(ms)))]))
        else:
            parts.append("%s n=0" % name)
    return " usdc_read " + " ".join(parts)


def _extra_worst_text(report):
    """The slowest extra pass, by call (b0f346d1): quote / swap_instructions / alt_rpc summed over
    its legs, plan, and venue:<name> per builder refresh. '' when no extra pass ran."""
    worst = report.get("extra_worst_ms") or {}
    if not worst.get("total"):
        return ""
    return " extra_worst_ms " + " ".join("%s=%.0f" % (k, v) for k, v in worst.items())


class LanePrefetcher:
    """Off-path refreshes: one owner per input group, one owner for extras. Readers take an
    immutable snapshot; balances are published as a pair only after both reads succeed."""

    PERIOD_S = {"sol_usd": 10.0, "balances": 2.0, "blockhash": 2.0}

    def __init__(self, *, usdc_fn, sol_fn, blockhash_fn, sol_usd_fn, extra_refresh_fn=None,
                 clock=time, log=None):
        import threading
        self._fns = {"usdc": usdc_fn, "sol": sol_fn, "blockhash": blockhash_fn,
                     "sol_usd": sol_usd_fn}
        self.extra_refresh_fn = extra_refresh_fn     # e.g. the builder's leg refresher(snapshot)
        self.clock = clock
        self.log = log
        self._lock = threading.Lock()
        self._report_lock = threading.Lock()  # off-path only; snapshot readers never take it
        self._values = {"sol_usd": None, "usdc_balance": None, "sol_balance": None,
                        "blockhash": None, "last_valid_height": None}
        self._seen = {}
        self._due = {}
        self._peak = {}
        self._window_started = clock.monotonic()
        self._report = self._new_report()

    @staticmethod
    def _new_report():
        return {"work": [], "phase_max": dict.fromkeys(PREFETCH_PHASES, 0.0),
                "failed": dict.fromkeys(PREFETCH_FAILABLE, 0), "extra_worst": {"total": 0.0}}

    def _positive(self, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value >= 0 else None

    def refresh_once(self, *, groups=None):
        """Refresh every input whose period has elapsed. A failed read keeps the old value AND its
        old stamp, so the lane sees it age out rather than a fresh-looking stale number."""
        now = self.clock.monotonic()
        groups = self.PERIOD_S if groups is None else groups
        ms, reasons, updates, seen = {}, {}, {}, {}
        if "sol_usd" in groups and now >= self._due.get("sol_usd", 0.0):
            raw = self._call("sol_usd", ms, reasons)
            price = self._positive(raw)
            if price:
                updates["sol_usd"], seen["sol_usd"] = price, now
                self._due["sol_usd"] = now + self.PERIOD_S["sol_usd"]
            else:
                self._failed("sol_usd", raw, ms, reasons)
        if "balances" in groups and now >= self._due.get("balances", 0.0):
            raw_usdc = self._call("usdc", ms, reasons)
            raw_sol = self._call("sol", ms, reasons)
            usdc, sol = self._positive(raw_usdc), self._positive(raw_sol)
            if usdc is not None and sol is not None:
                updates.update(usdc_balance=usdc, sol_balance=sol)
                seen["balances"] = now
                self._due["balances"] = now + self.PERIOD_S["balances"]
                # Display-only copy for the native float drift panel (2026-10-08); never raises.
                from sniper.services.memecoin_native_float import remember
                remember("solana", "native", sol)          # wSOL is not read: the panel says so
                remember("solana", "quote", usdc)          # the equity change recorder's Solana cash
            if usdc is None:
                self._failed("usdc", raw_usdc, ms, reasons)
            if sol is None:
                self._failed("sol", raw_sol, ms, reasons)
        if "blockhash" in groups and now >= self._due.get("blockhash", 0.0):
            got = self._call("blockhash", ms, reasons)
            if got:
                updates.update(blockhash=got[0], last_valid_height=got[1])
                seen["blockhash"] = now
                self._due["blockhash"] = now + self.PERIOD_S["blockhash"]
            else:
                self._failed("blockhash", got, ms, reasons)
        with self._lock:
            # Peak age: how old the input got before this refresh replaced it.
            landed = self.clock.monotonic()
            for name in seen:
                if name in self._seen:
                    self._peak[name] = max(self._peak.get(name, 0.0), landed - self._seen[name])
            self._values.update(updates)
            self._seen.update(seen)
        if "sol_usd" in updates:
            # Display-only copy for the live marks (S27-05): the UI's SOL/USD is this Jupiter
            # quote, never DexScreener. It cannot affect the lane.
            try:
                from sniper.services.memecoin_live_marks import publish_sol_usd
                from sniper.util.clock import real_utc_now_iso
                publish_sol_usd(updates["sol_usd"], real_utc_now_iso())
            except Exception:
                logger.exception("live marks SOL/USD publish failed (lane unaffected)")
        return ms

    def _call(self, name, ms, reasons):
        began, reason = self.clock.monotonic(), None
        try:
            value = self._fns[name]()
        except Exception as exc:
            logger.warning("slot lane prefetch %s failed", name, exc_info=True)
            value, reason = None, type(exc).__name__
        if name == "sol":   # a typed NativeSolBalanceRead; a failed read is None, never 0
            from sniper.execution.solana_wallet import NativeSolBalanceRead, native_sol_balance_value
            if isinstance(value, NativeSolBalanceRead) and not value.ok:
                reason = value.failure or "not_ok"
            value = native_sol_balance_value(value)
        ms[name] = (self.clock.monotonic() - began) * 1000.0
        reasons[name] = reason
        return value

    def _failed(self, name, raw, ms, reasons):
        """Count and log one failed input: its name, why, and how long the read took."""
        try:
            with self._report_lock:
                self._report["failed"][name] += 1
            if self.log is not None:
                reason = reasons.get(name) or ("none" if raw is None else "invalid")
                self.log("[memecoin] slot lane prefetch read failed name=%s ms=%.1f reason=%s" % (
                    name, ms.get(name, 0.0), reason))
        except Exception:
            logger.exception("slot lane prefetch failure log failed (lane unaffected)")

    def loop_once(self, pre_fn=None, *, groups=None, include_extra=True, extra_refresh_fn=None):
        """Time one worker pass. Empty groups run extras alone. Production assigns each group
        and the extras to exactly one worker; the default remains a synchronous fixture seam."""
        began = self.clock.monotonic()
        if pre_fn is not None:
            pre_fn()
        roster_ms = (self.clock.monotonic() - began) * 1000.0
        ms = self.refresh_once(groups=groups)
        extra_parts = {}
        extra = extra_refresh_fn or self.extra_refresh_fn
        if include_extra and extra is not None:
            from sniper.execution.pump_curve_buy import PREFETCH_TIMINGS
            PREFETCH_TIMINGS.sink, PREFETCH_TIMINGS.clock = {}, self.clock.monotonic
            extra_began = self.clock.monotonic()
            try:
                extra(self.snapshot())
            except Exception:
                logger.exception("slot lane extra prefetch failed")
            finally:
                extra_parts, PREFETCH_TIMINGS.sink = PREFETCH_TIMINGS.sink, None
            ms["extra"] = (self.clock.monotonic() - extra_began) * 1000.0
        try:
            work_ms = (self.clock.monotonic() - began) * 1000.0
            ms["roster"] = roster_ms
            with self._report_lock:
                report = self._report
                report["work"].append(work_ms)
                for phase in PREFETCH_PHASES:
                    report["phase_max"][phase] = max(report["phase_max"][phase], ms.get(phase, 0.0))
                if ms.get("extra", 0.0) > report["extra_worst"]["total"]:
                    report["extra_worst"] = {"total": ms["extra"], **(extra_parts or {})}
            if work_ms > PREFETCH_SLOW_LOOP_MS and self.log is not None:
                self.log("[memecoin] slot lane prefetch slow loop work_ms=%.0f %s" % (
                    work_ms, " ".join("%s=%.0f" % (k, ms.get(k, 0.0)) for k in PREFETCH_PHASES)))
        except Exception:
            logger.exception("slot lane prefetch loop telemetry failed (lane unaffected)")

    def take_report(self, force=False):
        """The window's report, then a fresh window; None until PREFETCH_REPORT_S has passed."""
        now = self.clock.monotonic()
        with self._report_lock:
            if not force and now - self._window_started < PREFETCH_REPORT_S:
                return None
            report, self._report = self._report, self._new_report()
            self._window_started = now
        work = sorted(report["work"])

        def q(p):
            return round(work[min(len(work) - 1, int(p * len(work)))], 1) if work else 0.0
        # None = never read good since start: unknown, never shown as fresh.
        with self._lock:
            ages = {name: (round(max(self._peak.get(name, 0.0), now - self._seen[name]), 1)
                           if name in self._seen else None)
                    for name in PREFETCH_AGE_INPUTS}
            self._peak = {}
        out = {"loops": len(work),
               "work_ms": {"p50": q(0.5), "p90": q(0.9), "p99": q(0.99),
                           "max": round(work[-1], 1) if work else 0.0},
               "phase_max_ms": {k: round(v, 1) for k, v in report["phase_max"].items()},
               "age_max_s": ages, "failed": dict(report["failed"]),
               "extra_worst_ms": {k: round(v, 1) for k, v in report["extra_worst"].items()}}
        take = getattr(self._fns.get("usdc"), "take_stats", None)
        if take is not None:
            try:
                out["usdc_read_ms"] = take()     # the bankroll read by path: ata / scan
            except Exception:  # noqa: BLE001 - telemetry never breaks the report
                logger.exception("slot lane usdc read stats failed")
        return out

    def snapshot(self):
        with self._lock:
            return PrefetchSnapshot(observed_mono=dict(self._seen), **self._values)


def _expected_unit_price(price_limit):
    """USD per whole token the build expects to pay (its clip over its expected tokens), or None."""
    try:
        tokens = int(price_limit["expected_tokens_atomic"]) / 10 ** int(price_limit["base_decimals"])
        return (int(price_limit["cost_usdc_atomic"]) / 1e6) / tokens if tokens > 0 else None
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def expected_clip_usd(conn, snapshot, day, risk_scale=1.0):
    """The clip `reserve_entry` would size right now at `risk_scale`, for prefetching the leg. The
    builder refuses a leg outside [90%, 100%] of the reserved clip (spend never exceeds the
    reservation, nor is a leg sized for another scale)."""
    from sniper.services.memecoin_risk import build_memecoin_risk_context, solana_flat_clip_ceiling_usd
    if snapshot.usdc_balance is None:
        return None
    # At threshold 0 (operator 2026-10-08) the leg sizes at the raised ceiling every reservation
    # then carries; otherwise None, today's clip.
    ctx = build_memecoin_risk_context(conn, usdc_balance=snapshot.usdc_balance,
                                      sol_balance=snapshot.sol_balance, day=day,
                                      risk_scale=risk_scale,
                                      clip_ceiling_usd=solana_flat_clip_ceiling_usd())
    clip = float(ctx.trade_size_usd or 0.0)
    return clip if clip > 0 else None


def leg_plan(conn, snapshot, now_iso):
    """(clip, scale) the legs are prefetched for: the market-wide regime scale a buy with no
    consensus and no price history will classify at (market_risk_scale, p50 0.6 ms), and the clip
    reserve_entry approves at it. MEMECOIN_SLOT_LANE_SCALED_LEG=0 pins scale 1.0 (the rollback)."""
    from sniper.services.memecoin_market_state import market_risk_scale
    scale = (market_risk_scale(conn, decision_at=now_iso, signal_source=SIGNAL_SOURCE)
             if cfg.MEMECOIN_SLOT_LANE_SCALED_LEG else 1.0)
    return expected_clip_usd(conn, snapshot, now_iso[:10], risk_scale=scale), scale


def owners_from_roster(conn, roster_path):
    """({owner_address: (source_user_id, handle)}, armed ids) for wallets armed on solana.

    THE REGISTRY DECIDES (2026-09-27, registry step 2): the verified addresses of armed profiles
    plus pump.fun ids, from `armed_verified_owners`, the same function the confirmed watcher reads.
    The handle join below gave 37 owners on the 2026-09-26 pre-cut roster; the registry, which
    joins each receipt through its J7 event by source_user_id, verifies 39 and loses none. The
    JSONL path stays for a ledger without the registry table. An armed wallet with no verified
    address is simply not watched; the J7 lane still covers it. A failed read RAISES: the caller
    keeps its last good set (`_refresh_roster`)."""
    armed = m.real_approved_user_ids(conn, network_id="solana")
    owners = armed_verified_owners(conn, armed=armed)
    if owners is None:
        owners = _owners_from_jsonl(conn, roster_path, armed)
    return owners, frozenset(armed)


def _refresh_roster(roster, status, ro, roster_path):
    """Swap in a fresh owner set. A failed read keeps the last good one, never an empty set, and
    no longer skips the rest of the prefetch iteration."""
    try:
        owners, armed = owners_from_roster(ro, roster_path)
    except Exception:
        logger.exception("slot lane roster refresh failed; keeping the last good owner set")
        return False
    import hashlib
    from types import MappingProxyType
    roster_json = json.dumps({'owners': sorted(owners.items()), 'armed': sorted(armed)},
                             separators=(',', ':'))
    # Digest the effective cached policy; never persist provider credentials or URLs.
    policy = {key: value for key, value in vars(cfg).items()
              if key.startswith('MEMECOIN_') and isinstance(value, (str, int, float, bool, tuple, frozenset, dict, list))
              and not any(word in key for word in ('KEY', 'URL', 'SECRET', 'PASSWORD', 'TOKEN', 'RPC'))}
    config_json = repr(sorted((key, repr(value)) for key, value in policy.items()))
    roster_generation = hashlib.sha256(roster_json.encode()).hexdigest()
    config_generation = hashlib.sha256(config_json.encode()).hexdigest()
    native_live = lane_mode() == 'live'
    venue_policy = dict(pump_curve=native_live, pump_amm=native_live,
        raydium_cpmm=native_live and routed_mode() == 'live' and cfg.MEMECOIN_SLOT_LANE_CPMM == 'live',
        raydium_launchlab=native_live and routed_mode() == 'live' and cfg.MEMECOIN_SLOT_LANE_LAUNCHLAB == 'live',
        meteora_damm_v2=native_live and routed_mode() == 'live' and cfg.MEMECOIN_SLOT_LANE_DAMM_V2 == 'live',
        meteora_dbc=native_live and routed_mode() == 'live' and cfg.MEMECOIN_SLOT_LANE_DBC == 'live')
    provenance = MappingProxyType(dict(
        roster_generation=roster_generation, config_generation=config_generation, roster_json=roster_json,
        frozen_cohort_id=hashlib.sha256((roster_generation + ':' + config_generation).encode()).hexdigest(),
        **{'source_venue_' + venue + '_authorized': enabled for venue, enabled in venue_policy.items()}))
    roster["owners"], roster["armed"] = owners, armed
    roster['receipt_provenance'] = provenance
    status.update(owners=len(owners), armed=len(armed))
    return True


def _owners_from_jsonl(conn, roster_path, armed):
    """The legacy source: data/solana_roster_owners.jsonl (handle -> owner) plus pump.fun ids."""
    import json
    handle_to_uid = {str(r["handle"]).lower(): r["source_user_id"] for r in conn.execute(
        "SELECT source_user_id, handle FROM memecoin_wallets WHERE handle IS NOT NULL")}
    owners = {}
    with open(roster_path) as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            uid = handle_to_uid.get(str(row.get("handle") or "").lower())
            if row.get("owner") and not row.get("error") and uid in armed:
                owners[row["owner"]] = (uid, row.get("handle"))
    # A pump.fun profile's id IS its wallet: "pump:<base58 address>". Four armed pump: wallets were
    # dark on J7 and absent from the JSONL (Gamma, 2026-09-26: notdecu, TheMisterTurtle, untaxxable,
    # hashbergers made 39 on-chain buys in 30 min that J7 never relayed), so derive the owner
    # from the id itself; only a string that parses as a 32-byte pubkey is watched.
    uid_to_handle = {uid: handle for handle, uid in handle_to_uid.items()}
    for uid in armed:
        if str(uid).startswith("pump:") and _is_pubkey(str(uid)[5:]):
            owners.setdefault(str(uid)[5:], (uid, uid_to_handle.get(uid)))
    return owners


def _is_pubkey(text):
    try:
        from solders.pubkey import Pubkey
        return str(Pubkey.from_string(text)) == text
    except Exception:
        return False


def dispatch_build(builders):
    """build_fn that routes a buy to its venue's builder; an unknown venue refuses. A buy back from
    the on-demand worker carries its leg and quote to the builder."""
    def build(buy, clip_usd, snapshot, *, funding_lamports=None, tip=None, priority_lamports=None):
        # A graduation buy is the source's curve trade built on the coin's canonical PumpSwap pool.
        builder = builders.get("pump_amm" if getattr(buy, "graduation", None) is not None
                               else getattr(buy, "venue", "pump_curve"))
        if builder is None:
            return None, "no_builder_for_%s" % getattr(buy, "venue", None)
        ondemand = getattr(buy, "ondemand", None)
        cash_leg = (cached_cash_leg(builders, buy, clip_usd)
                    if ondemand is None and funding_lamports is None
                    and getattr(buy, "venue", None) in ("raydium_cpmm", "raydium_launchlab", "meteora_damm_v2",
                                                        "meteora_dbc")
                    else None)
        given = ondemand["leg"] if ondemand is not None else cash_leg
        curve = builders.get("pump_curve")
        held_table = (getattr(curve, "pump_alt", None) if builder is builders.get("pump_amm")
                      and getattr(curve, "user", None) == getattr(builder, "user", None) else None)
        out = builder.build(buy, clip_usd, snapshot,
                            **({"funding_lamports": funding_lamports}
                               if funding_lamports is not None else {}),
                            **({"priority_lamports": priority_lamports}
                               if priority_lamports and getattr(builder, "accepts_priority_lamports", False)
                               else {}),
                            **({"tip": tip} if tip else {}),
                            **({"lookup_tables": (held_table,)} if held_table is not None else {}),
                            **({"leg": ondemand["leg"], "quote": tuple(ondemand["quote"])}
                               if ondemand is not None else {"leg": cash_leg} if cash_leg is not None else {}))
        if out[0] is None and out[1] in ("leg_over_clip", "leg_under_clip"):
            _log_leg_clip(builder, buy, clip_usd, out[1], given)
        return out
    return build


def _log_leg_clip(builder, buy, clip_usd, reason, given):
    """One line naming the approved clip and every warm leg (atoms@age) a leg_over_clip /
    leg_under_clip refusal was judged against (Alpha 2026-10-07: probablyinsider 0dbfd8c0 S+4).
    Log only; never raises into the build."""
    try:
        from sniper.execution.pump_curve_buy import clip_atomic
        now = builder.clock()
        legs = [given] if given is not None else [
            leg for leg in (*getattr(builder, "_cash_legs", ()), getattr(builder, "_leg", None))
            if leg is not None]
        seen = dict.fromkeys("%d@%.1fs" % (int(leg["usdc_atomic"]), now - float(leg["fetched_at"]))
                             for leg in legs)
        logger.warning("[memecoin] slot lane leg clip %s clip=%d legs=%s venue=%s sig=%s", reason,
                       clip_atomic(clip_usd), ",".join(seen) or "none",
                       getattr(buy, "venue", None), getattr(buy, "signature", None))
    except Exception:  # noqa: BLE001 - evidence only
        logger.debug("slot lane leg clip evidence failed", exc_info=True)


def cached_cash_leg(builders, buy, clip):
    """Share PumpSwap's wrapped SOL leg with the other SOL-quoted venue builders."""
    from sniper.execution.pump_curve_buy import WSOL_MINT
    venue = getattr(buy, "venue", "pump_curve")
    builder = builders.get(venue)
    if venue in ("raydium_cpmm", "raydium_launchlab", "meteora_damm_v2", "meteora_dbc"):
        if builder is None or getattr(buy, "quote_mint", None) != WSOL_MINT:
            return None
        builder = builders.get("pump_amm")
        return builder.matching_leg(clip) if builder is not None else None
    if venue not in ("pump_curve", "pump_amm") or builder is None:
        return None
    if venue == "pump_amm":
        pool = builder.pools.get(getattr(buy, "pool", None))
        if pool is None or pool.get("quote_mint") != WSOL_MINT:
            return None
    return builder.matching_leg(clip)


def exotic_preparation_clips(clip, scale, armed_ids, *, trials=None):
    """Background inventory only; reservation and final sizing remain authoritative."""
    from sniper.execution.pump_curve_buy import clip_atomic
    from sniper.services.memecoin_risk import scaled_funded_clip, solana_flat_clip_ceiling_usd
    from sniper.services.memecoin_wallet_caps import entry_cap_usd
    if (type(clip) not in (int, float) or not math.isfinite(clip) or clip <= 0
            or type(scale) not in (int, float) or not math.isfinite(scale) or not 0 < scale <= 1):
        return ()
    tiers = [float(clip)]
    if (cfg.MEMECOIN_SLOT_LANE_SCALED_SOL and type(scale) in (int, float)
            and math.isfinite(scale) and 0 < scale <= 1):
        target = 1.0 if scale < 1 else float(cfg.MEMECOIN_FUNDED_REGIME_RISK_SCALE)
        if math.isfinite(target) and 0 < target <= 1 and target != scale:
            alternate = scaled_funded_clip(base_clip_usd=clip / scale, risk_scale=target,
                floor_usd=float(cfg.MEMECOIN_MIN_TRADE_FLOOR_USD))
            if alternate is not None:
                tiers.append(alternate)
    atoms = set()
    for uid in armed_ids:
        cap = entry_cap_usd(uid, 'solana', ceiling_usd=solana_flat_clip_ceiling_usd())
        if cap is not None and (not math.isfinite(cap) or cap < 0):
            continue
        values = [(trials or {})[uid]] if uid in (trials or {}) else tiers
        for value in values:
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                continue
            value = min(value, cap) if cap is not None else value
            if value > 0:
                atoms.add(clip_atomic(value))
    return tuple(sorted(atoms))


def refresh_builder_legs(builders, clip, scale, snapshot, warm=None):
    """Background SOL refreshes; notify the independent exotic owner without waiting for it."""
    from sniper.services.memecoin_risk import scaled_funded_clip
    alternate = None
    if cfg.MEMECOIN_SLOT_LANE_SCALED_SOL and math.isfinite(scale) and 0 < scale <= 1:
        target = 1.0 if scale < 1 else float(cfg.MEMECOIN_FUNDED_REGIME_RISK_SCALE)
        if 0 < target <= 1 and target != scale:
            alternate = scaled_funded_clip(base_clip_usd=clip / scale, risk_scale=target,
                                           floor_usd=float(cfg.MEMECOIN_MIN_TRADE_FLOOR_USD))
    from sniper.execution.pump_curve_buy import prefetch_timed
    for venue, builder in builders.items():
        try:
            extra = ((min(clip, cfg.MEMECOIN_REAL_UNPROVEN_MAX_ENTRY_USD),)
                     if cfg.MEMECOIN_REAL_WALLET_CAPS_ENABLED and venue in ("pump_curve", "pump_amm") else ())
            # The desk's clip, on the curve builder only: the desk native curve rung takes this leg
            # (sniper/execution/desk_native_curve.py) instead of two Jupiter calls on its miss path.
            if venue == "pump_curve" and float(getattr(cfg, "MEMECOIN_DESK_CLIP_USD", 0) or 0) > 0:
                extra += (float(cfg.MEMECOIN_DESK_CLIP_USD),)
            extras = {"extra_clip_usds": extra} if extra else {}
            with prefetch_timed("venue:" + venue):
                builder.refresh(clip, snapshot, **extras, **({"alternate_clip_usd": alternate}
                                if alternate is not None and venue in ("pump_curve", "pump_amm") else {}))
        except Exception:
            logger.exception("slot lane %s refresh failed", venue)
    if warm is not None:
        try:
            with prefetch_timed("exotic_warm"):
                warm.wake()
        except Exception:
            logger.exception("slot lane warm exotic legs refresh failed")


def venue_live_cap(venue):
    """The live sends an hour allowed on one new venue: its
    MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE override, else
    MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR. 0 = none."""
    caps = getattr(cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE", None) or {}
    if venue in caps:
        return int(caps[venue])
    if str(venue).endswith(":exotic"):
        return EXOTIC_CANARY_CAP
    return int(getattr(cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR", 6) or 0)


# The module whose lane_mode() is a new venue's own flag (off | shadow | live).
VENUE_MODE_MODULES = {"raydium_cpmm": "raydium_cpmm_buy", "raydium_launchlab": "raydium_launchlab_buy",
                      "meteora_damm_v2": "meteora_damm_v2_buy", "meteora_dbc": "meteora_dbc_buy"}


def log_venue_start(log, builders, lane_mode_now):
    """At lane start, one line per enabled venue builder: its own flag, the lane's mode and, for a
    new venue, its effective live cap. A WARNING for a cap override key that names no venue (a typo
    like damm_v2=2, or meteora_damm_v2:2 with no '='): it caps nothing and the venue keeps the
    default (c3966171; Gamma LOW on 3d45455fb, 2026-10-01)."""
    import importlib
    for venue in sorted(builders):
        module = VENUE_MODE_MODULES.get(venue)
        mode = (importlib.import_module("sniper.execution." + module).lane_mode()
                if module else lane_mode_now)
        cap = (" live_cap_per_hour=%d" % venue_live_cap(venue)
               if venue in SHADOW_ONLY_VENUES else "")
        log("[memecoin] slot lane venue %s mode=%s lane_mode=%s%s" % (venue, mode, lane_mode_now, cap))
    caps = getattr(cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE", None) or {}
    for key in sorted(set(caps) - SHADOW_ONLY_VENUES - {exotic_cap_key(v) for v in EXOTIC_VENUES}):
        logger.warning("[memecoin] slot lane venue cap override %r names no slot-lane venue (%s); "
                       "it caps nothing", key, ", ".join(sorted(SHADOW_ONLY_VENUES)))


def start_slot_lane(rc, *, builder=None, builders=None, signal_start_fn, sol_usd_fn, log,
                    roster_path, conn_factory, readonly_conn_factory, clock=time, mirror=None,
                    migration_start_fn=None, routed_baseline_fn=None, onchain_wake=None,
                    damm_prewarm_start_fn=None, receipt_resume_ready=None, measurement=None,
                    migration_on_signal=False, prepare_on_signal=False, damm_on_signal=False, rabbit=None):
    """Start the signal thread, then the prefetch and lane threads. Returns a status dict.

    `builders` maps a venue ("pump_curve", "pump_amm") to an object exposing
    build(buy, clip_usd, snapshot) and refresh(clip_usd, snapshot); a lone
    `builder` means curve only. A "pump_amm" builder must also expose `pools` (its pool cache).
    `signal_start_fn(on_buy, owners_fn=..., status=...)` starts the processed-logs socket. Every
    thread owns its own SQLite connection. `mirror` (memecoin_source_sell_mirror), when given,
    receives the owners' own sells and a tap of every own buy, from the socket thread, plus this
    lane's pool cache for PumpSwap pool -> mint. `migration_start_fn(on_pool, status=...)`, when
    given with a PumpSwap builder, starts the pump.fun migration listener feeding that pool cache
    (memecoin_slot_signal.start_migration_thread); a failure to start it costs only the prewarm.

    The socket starts FIRST, before any connection is opened or worker started: it refuses a
    funded key or an empty URL by raising, and a worker started before that would be orphaned,
    re-quoting Jupiter on the FUNDED key about 2.7 times a second for the life of the process
    with no signal to serve (Codex re-review, 2026-09-26). Buys that arrive in the microseconds
    before the lane thread starts wait in the queue.

    ROUTED BUYS (routed_mode(), 2026-09-27): with `routed_baseline_fn` the socket also pairs owners'
    USDC-paid routed buys, and a thread of their own logs each one ("shadow") or writes it for the
    on-chain lane and sets `onchain_wake` ("live"). Live needs that event and the on-chain lane;
    without them it runs as shadow. The lane never builds a routed buy."""
    builders = dict(builders or {"pump_curve": builder})
    import queue
    import threading
    from sniper.services.memecoin_arming import is_kill_tripped
    from sniper.util.clock import real_utc_now, real_utc_now_iso

    status = {"started_at": real_utc_now_iso(), "handled": 0, "last_result": None,
              "signal": {}, "armed": 0, "owners": 0}
    status['entry_opportunity'] = entry_policy.snapshot()
    roster = {"owners": {}, "armed": frozenset(), "clip": None, "scale": 1.0}
    measurement_kwargs = {}
    preparation_wake = threading.Event()
    prepare_queue = queue.Queue(maxsize=128) if prepare_on_signal else None
    if prepare_queue is not None:
        status['preparation'] = dict(capacity=128, retention_s=2.0, per_tick=2, queued=0, overflow=0,
            expired=0, paced_drop=0, unsupported=0, delivered=0, errors=0)
        def queue_preparation(candidate):
            try:
                prepare_queue.put_nowait(candidate)
                status['preparation']['queued'] += 1
                preparation_wake.set()
            except queue.Full:
                status['preparation']['overflow'] += 1
        measurement_kwargs['on_prepare'] = queue_preparation
    damm_queue = queue.Queue(maxsize=64) if damm_on_signal else None
    if damm_queue is not None:
        status["damm_prewarm"] = dict(source="shyft_processed", queued=0, dropped=0, delivered=0,
                                       errors=0, ready=False)
        def queue_damm(signature):
            try:
                damm_queue.put_nowait(signature)
                status["damm_prewarm"]["queued"] += 1
            except queue.Full:
                status["damm_prewarm"]["dropped"] += 1
                status["damm_prewarm"]["last_error"] = "damm_queue_overflow"
        measurement_kwargs["on_damm_creation"] = queue_damm
    migration_queue = queue.Queue(maxsize=64) if migration_on_signal else None
    if migration_queue is not None:
        status['prewarm'] = dict(source='shyft_processed', queued=0, dropped=0, delivered=0,
                                 errors=0, ready=False)
        def queue_migration(pool):
            try:
                # Heard now; the consumer may drain later (decision features' pair age).
                migration_queue.put_nowait((pool, real_utc_now()))
                status['prewarm']['queued'] += 1
            except queue.Full:
                status['prewarm']['dropped'] += 1
                status['prewarm']['last_error'] = 'migration_queue_overflow'
        measurement_kwargs['on_migration'] = queue_migration
    if measurement is not None:
        import inspect
        measurement.provenance_fn = lambda: roster.get('receipt_provenance', {})
        status['_receipt_journals'] = (measurement,)
        parameters = inspect.signature(signal_start_fn).parameters.values()
        if any(p.name == 'measurement' or p.kind == p.VAR_KEYWORD for p in parameters):
            measurement_kwargs['measurement'] = measurement
    # The OKX pre-execution handler prices the source's SOL leg on the prefetcher's cached sol_usd, but
    # the prefetcher is built after the signal thread starts: a late reference, which raises (and so
    # refuses as sol_usd_stale, memecoin_rabbit_okx_amm) until the prefetcher exists.
    prefetch_ref = {}
    import inspect as _inspect
    if any(p.name == 'rabbit_sol_usd_fn' or p.kind == p.VAR_KEYWORD
           for p in _inspect.signature(signal_start_fn).parameters.values()):
        from sniper.services.memecoin_rabbit_okx_amm import sol_usd_reader
        measurement_kwargs['rabbit_sol_usd_fn'] = sol_usd_reader(lambda: prefetch_ref['prefetcher'].snapshot(),
                                                                 clock=clock)
    if any(p.name == 'rabbit_tape_watch_fn' or p.kind == p.VAR_KEYWORD
           for p in _inspect.signature(signal_start_fn).parameters.values()):
        # The rabbit pool tape's address filter: the FeeConfigs plus every known PumpSwap pool's vaults
        # and base mint (memecoin_rabbit_tape.watch_addresses; memory only, read on the tape thread).
        from sniper.services.memecoin_rabbit_tape import watch_addresses
        measurement_kwargs['rabbit_tape_watch_fn'] = lambda: watch_addresses(
            getattr(builders.get("pump_amm"), "pools", None))
    buys = LaneBuyQueue(clock=clock)
    collector = {"lane": None, "sol_usd": None}
    status["busy_handoffs"] = 0

    def collect_buy(buy):
        live = collector["lane"]
        if (live is not None and live.mode == "live" and live.busy and live.handoff is not None
                and live.handoff.offer_busy(buy, collector["sol_usd"])):
            status["busy_handoffs"] += 1
            return
        buys.put(buy)

    mode, routed = routed_mode(), {}
    if mode != "off" and routed_baseline_fn is not None:
        routed_buys = queue.SimpleQueue()
        routed = {"on_routed": routed_buys.put, "baseline_fn": routed_baseline_fn}
        if mode == "live" and (onchain_wake is None or not cfg.MEMECOIN_ONCHAIN_LANE_ENABLED):
            mode = "shadow"                  # nothing would read a routed row
    if mirror is None:
        signal_start_fn(collect_buy, owners_fn=lambda: roster["owners"], status=status["signal"],
                        **routed, **measurement_kwargs)
    else:
        mirror.pools = getattr(builders.get("pump_amm"), "pools", None)

        def on_buy(buy):
            collect_buy(buy)
            if not is_rabbit(buy):        # the processed copy taps the exact amount
                mirror.offer_buy(buy)
        signal_start_fn(on_buy, owners_fn=lambda: roster["owners"], status=status["signal"],
                        on_sell=mirror.offer_many, **routed, **measurement_kwargs)
    routed_handoff = None
    learner_ref = {}                          # the MissLearner, set below; read per routed buy (B1)
    if routed:
        status["signal"].setdefault("routed", {})["mode"] = mode
        def owner_guard(signature):
            from sniper.services.memecoin_onchain_lane import slot_funded_owner
            return slot_funded_owner(thread_readonly_conn(), signature)
        routed_handoff = (RoutedHandoff(conn_factory=conn_factory, wake=onchain_wake,
                                       put=buys.put, clock=clock, owner_guard=owner_guard,
                                       proof_put=routed_buys.put)
                          if mode == "live" else None)

        cpmm_on = "raydium_cpmm" in builders
        launchlab_on = "raydium_launchlab" in builders
        pump_on = bool(cfg.MEMECOIN_SLOT_LANE_ROUTED_PUMP)
        lane_live = lane_mode() == "live"         # the SlotLane below is built with the same mode
        routed_pools = getattr(builders.get("pump_amm"), "pools", None)
        pool_learn = bool(getattr(cfg, "MEMECOIN_SLOT_LANE_ROUTED_POOL_LEARN", False))
        damm_on = "meteora_damm_v2" in builders
        damm_pools = getattr(builders.get("meteora_damm_v2"), "pools", None)
        learner_ref["dbc"] = dbc_first_sight_learner(builders, log=log)

        def routed_loop():
            while True:
                routed_buy = routed_buys.get()
                try:
                    route_routed_buy(routed_buy, handoff=routed_handoff, log=log, put=buys.put,
                                     amm_pools=routed_pools, cpmm_on=cpmm_on, pump_on=pump_on,
                                     lane_live=lane_live, launchlab_on=launchlab_on,
                                     launchlab_pools=getattr(builders.get("raydium_launchlab"),
                                                             "pools", None),
                                     learner=learner_ref.get("learner"), pool_learn=pool_learn,
                                     damm_on=damm_on, damm_pools=damm_pools,
                                     dbc_learner=learner_ref.get("dbc"))
                except Exception:
                    logger.exception("slot lane routed routing failed sig=%s", routed_buy.signature)
        threading.Thread(target=routed_loop, name="slot-lane-routed", daemon=True).start()
    # One independent speculative owner; live workers only look up and note admitted exact legs.
    from sniper.execution.raydium_launchlab_buy import WARM_QUOTE_FIELDS, WarmExoticLegs, warm_legs_on
    from sniper.execution.pump_curve_buy import WSOL_MINT, USDC_MINT
    exotic_warm = WarmExoticLegs() if warm_legs_on() else None
    preparation_candidates = {}  # at most two verified quote identities
    preparation_lock = threading.Lock()

    def seed_preparation(venue, builder, facts):
        if (exotic_warm is None or not exotic_live(venue)
                or facts.get('quote_mint') in (None, WSOL_MINT, USDC_MINT)):
            return
        with preparation_lock:
            key = (venue, *(facts.get(field) for field in WARM_QUOTE_FIELDS))
            preparation_candidates.pop(key, None)
            preparation_candidates[key] = (builder, facts)
            while len(preparation_candidates) > 2:
                del preparation_candidates[next(iter(preparation_candidates))]
            inventory = roster.get('exotic_clips', ())
        for atoms in inventory:
            exotic_warm.seed(venue, facts['quote_mint'], atoms, builder=builder, facts=facts)

    if exotic_warm is not None:
        exotic_warm.start()
        status["exotic_warm"] = exotic_warm.stats
        for venue, prepared_builder in builders.items():
            pools = getattr(prepared_builder, 'pools', None)
            if pools is not None and hasattr(pools, 'on_pool_known'):
                pools.on_pool_known = lambda pool, facts, v=venue, b=prepared_builder: seed_preparation(v, b, facts)


    amm_pools = getattr(builders.get("pump_amm"), "pools", None)
    graduation_ref = {}                       # the graduation watcher, set once the lane exists

    def on_migrated(pool, observed_at=None):
        amm_pools.offer_stream(pool)
        # MEASUREMENT ONLY (spec 2026-10-14): when we heard it, for the slot lane's pair age.
        amm_pools.note_migration(pool, real_utc_now() if observed_at is None else observed_at)
        preparation_wake.set()
        watcher = graduation_ref.get("watcher")
        if watcher is not None:
            watcher.notify(pool)
    # The published cash legs per builder (Codex speed item 2's read): a fresh primary beside an
    # older secondary is visible in the heartbeat. Read on the heartbeat writer's thread, no lock.
    def cash_legs():
        from sniper.execution.pump_curve_buy import cash_leg_snapshot
        return {venue: cash_leg_snapshot(b) for venue, b in builders.items()
                if b is not None and hasattr(b, "_cash_legs")}
    status["cash_legs"] = cash_legs
    if migration_start_fn is not None and amm_pools is not None:
        status["prewarm"], status["pool_cache"] = {}, amm_pools.stats
        try:
            migration_start_fn(on_migrated, status=status["prewarm"])
            log("[memecoin] slot lane pool prewarm: migration listener started")
        except Exception:
            logger.exception("slot lane migration listener did not start; pools learn on a miss")
    if migration_queue is not None and amm_pools is not None:
        status['pool_cache'] = amm_pools.stats
    damm_pools = getattr(builders.get("meteora_damm_v2"), "pools", None)
    if damm_prewarm_start_fn is not None and damm_pools is not None:
        status["damm_prewarm"] = {}
        try:
            damm_prewarm_start_fn(damm_pools.offer_creation, status=status["damm_prewarm"])
            log("[memecoin] slot lane DAMM pool prewarm: creation listener started")
        except Exception:
            logger.exception("slot lane DAMM creation listener did not start; pools learn on a miss")
    if damm_queue is not None and damm_pools is not None:
        def damm_consumer():
            status["damm_prewarm"]["ready"] = True
            while True:
                signature = damm_queue.get()
                try:
                    damm_pools.offer_creation(signature)
                    status["damm_prewarm"]["delivered"] += 1
                except Exception:
                    status["damm_prewarm"]["errors"] += 1
                    status["damm_prewarm"]["last_error"] = "damm_consumer_error"
                finally:
                    damm_queue.task_done()
        threading.Thread(target=damm_consumer, name="slot-damm-prewarm", daemon=True).start()
    prefetcher = LanePrefetcher(usdc_fn=rc["usdc_fn"], sol_fn=rc["sol_fn"],
                                blockhash_fn=rc["latest_blockhash_fn"], sol_usd_fn=sol_usd_fn,
                                clock=clock, log=log)
    prefetch_ref['prefetcher'] = prefetcher

    def essential_loop(group):
        while True:
            try:
                prefetcher.loop_once(groups=(group,), include_extra=False)
                if group == "sol_usd":
                    collector["sol_usd"] = prefetcher.snapshot().sol_usd
                if group == "balances":  # report + bankroll telemetry stay on their read owner
                    report = prefetcher.take_report()
                    if report is not None:
                        log(prefetch_report_line(report))
            except Exception:
                logger.exception("slot lane essential prefetch failed group=%s", group)
            clock.sleep(0.5)

    def prefetch_loop():
        # Only this worker owns the readonly handle and calls builder refreshes. Slow quotes,
        # pool learning and roster reads cannot postpone balances, blockhash or SOL/USD.
        ro = None
        next_roster = 0.0

        def roster_pass():
            nonlocal next_roster
            refreshed = _refresh_roster(roster, status, ro, roster_path)
            if exotic_warm is not None:
                from sniper.services.memecoin_trial import marker
                roster['exotic_trials'] = None
                try:
                    if refreshed:
                        trials = {}
                        for uid in roster['armed']:
                            trial_row = m.open_trial(ro, uid, 'solana')
                            if trial_row is not None:
                                trials[uid] = marker(trial_row)['clip_usd']
                        roster['exotic_trials'] = trials
                except Exception:
                    logger.exception('slot lane speculative trial inventory unavailable')
                finally:
                    ro.rollback()
            next_roster = clock.monotonic() + 10.0

        def refresh_clip(snapshot):
            from sniper.execution.pump_curve_buy import prefetch_timed
            try:
                with prefetch_timed("plan"):
                    roster["clip"], roster["scale"] = leg_plan(ro, snapshot, real_utc_now_iso())
                    status["clip_usd"] = roster["clip"]   # read by the SOL BufferWatch (2026-10-21)
                    if exotic_warm is not None:
                        trials = roster.get('exotic_trials')
                        inventory = (() if trials is None else exotic_preparation_clips(
                            roster['clip'], roster['scale'], roster['armed'], trials=trials))
            finally:
                ro.rollback()  # never pin a read transaction across the slow provider calls
            if exotic_warm is not None:
                with preparation_lock:
                    changed = inventory != roster.get('exotic_clips')
                    roster['exotic_clips'] = inventory
                    candidates = tuple(preparation_candidates.items()) if changed else ()
                exotic_warm.set_clip_inventory(inventory)
                for (venue, *_quote_identity), (prepared_builder, facts) in candidates:
                    if not exotic_live(venue):
                        continue
                    for atoms in inventory:
                        exotic_warm.seed(venue, facts['quote_mint'], atoms,
                                         builder=prepared_builder, facts=facts)
            if roster["clip"]:
                refresh_builder_legs(builders, roster["clip"], roster["scale"], snapshot,
                                     warm=exotic_warm)

        next_preparation = 0.0
        try:
            while True:
                try:
                    iteration_started = clock.monotonic()
                    before_batches = (amm_pools.stats.get('prefetch_batches', 0) if amm_pools is not None else 0)
                    if ro is None:
                        ro = readonly_conn_factory()
                    if prepare_queue is not None and iteration_started >= next_preparation:
                        from sniper.services.memecoin_shyft_prepare import drain_preparations
                        consumed = drain_preparations(prepare_queue, builders, status['preparation'],
                                                      now=iteration_started)
                        if consumed:
                            next_preparation = iteration_started + 0.5
                    confirmations = getattr(collector.get('lane'), 'confirmations', None)
                    if confirmations is not None:
                        for pending_id in confirmations.unresolved:
                            pending = m.pending_row(ro, pending_id)
                            if pending is not None and pending['status'] in ('confirmed', 'failed'):
                                buys.put(CashSettlement(pending_id, clock.monotonic()))
                        ro.rollback()
                    prefetcher.loop_once(
                        groups=(), extra_refresh_fn=refresh_clip,
                        pre_fn=roster_pass if clock.monotonic() >= next_roster else None)
                    if amm_pools is not None and amm_pools.stats.get('prefetch_batches', 0) != before_batches:
                        next_preparation = max(next_preparation, iteration_started + 0.5)
                    take_warm = getattr(amm_pools, "take_warm_report", None)
                    warm = take_warm() if take_warm is not None else None
                    if warm:
                        log("[memecoin] slot lane pool warm: %d persisted -> %d stored, %d calls, "
                            "%.0f ms, %d unresolved reads, streak max %d" % (
                                warm["persisted"], warm["stored"], warm["calls"], warm["ms"],
                                warm.get("unresolved", 0), warm.get("unresolved_streak_max", 0)))
                    # After the refresh on purpose: that is where the pool cache learns.
                    for done in (handoff.flush() if handoff is not None else ()):
                        log("[memecoin] slot lane handoff %s %s -> %s" % (
                            done["handle"], str(done["mint"] or "?")[:8], done["outcome"]))
                except Exception:
                    logger.exception("slot lane extra prefetch loop failed")
                finally:
                    if ro is not None:
                        ro.rollback()
                if prepare_queue is not None or migration_queue is not None or migration_start_fn is not None:
                    from sniper.services.memecoin_shyft_prepare import wait_for_preparation
                    wait_for_preparation(preparation_wake, clock=clock, not_before=next_preparation)
                else:
                    clock.sleep(0.5)
        finally:
            if ro is not None:
                ro.close()

    amm = builders.get("pump_amm")
    # Nothing reads a handed-off row unless the on-chain lane runs.
    handoff = (Handoff(pools=getattr(amm, "pools", None), conn_factory=conn_factory,
                       max_age_s=float(cfg.MEMECOIN_ONCHAIN_LANE_MAX_AGE_S), clock=clock,
                       wake=onchain_wake)
               if cfg.MEMECOIN_ONCHAIN_LANE_ENABLED else None)
    learner = None
    # Each venue with a pool cache learns its misses the same way (2026-09-27: LaunchLab's census
    # buys hit 45 pools in 59 buys, so a plain miss would almost never build).
    learn_pools = {venue: getattr(builders.get(venue), "pools", None)
                   for venue in ("pump_amm", "raydium_cpmm", "raydium_launchlab")}
    learn_pools = {venue: pools for venue, pools in learn_pools.items() if pools is not None}
    first_sight = damm_first_sight_learner(getattr(builders.get("meteora_damm_v2"), "pools", None),
                                           sol_usd_fn=lambda: prefetcher.snapshot().sol_usd, log=log)
    if first_sight is not None:
        learn_pools["meteora_damm_v2"] = first_sight
    if learner_ref.get("dbc") is not None:
        learn_pools["meteora_dbc"] = learner_ref["dbc"]
    if cfg.MEMECOIN_SLOT_LANE_MISS_LEARN and learn_pools:
        learner_handoff = make_learner_handoff(
            live=lane_mode() == "live", routed_handoff=routed_handoff, handoff=handoff,
            sol_usd_fn=lambda: prefetcher.snapshot().sol_usd)
        learner = MissLearner(pools=learn_pools, put=buys.put, handoff_fn=learner_handoff, log=log,
                              clock=clock)
        learner_ref["learner"] = learner
        status['route_cache_churn'] = learner.stats
        threading.Thread(target=learner.run, name="slot-lane-miss-learn", daemon=True).start()
        for venue in learner._venue_jobs:        # the shadow-only venues, each on its own thread
            threading.Thread(target=lambda v=venue: learner.run(v), name="slot-lane-miss-learn-" + venue,
                             daemon=True).start()
    # PERSISTED CU DEMOTIONS (2026-10-09): loaded before any lane thread starts, so a class that ran out
    # of compute units before a restart signs the lane default from this run's first build.
    def cu_page(kind, text):
        from sniper.services.memecoin_alerts import send_alert
        send_alert(kind, text, icon="🟠")
    status["cu_demoted"] = sorted(_cu_class_key(c) for c in load_cu_demotions(readonly_conn_factory,
                                                                             alert_fn=cu_page))
    if status["cu_demoted"]:
        log("[memecoin] slot lane CU classes demoted from the ledger: %s" % ", ".join(status["cu_demoted"]))
    def observed_head():
        signal = status['signal'].copy()
        return signal.get('head_slot'), signal.get('head_slot_mono')
    lane = SlotLane(conn=conn_factory(), executor=rc, build_fn=dispatch_build(builders),
                    amm_pools=getattr(amm, "pools", None), handoff=handoff,
                    snapshot_fn=prefetcher.snapshot, armed_ids_fn=lambda: roster["armed"],
                    kill_tripped_fn=is_kill_tripped, now_iso_fn=real_utc_now_iso,
                    lock_key=rc["slot_lock_key"], mode=lane_mode(),
                    head_slot_fn=observed_head,
                    shadow_clip_fn=lambda: roster["clip"], clock=clock,
                    leg_scale_fn=lambda: roster["scale"],
                    cash_leg_fn=lambda buy, clip: cached_cash_leg(builders, buy, clip),
                    sol_buffer_cap_usd=cfg.MEMECOIN_SOL_EXEC_BUFFER_CAP_USD,
                    cpmm_pools=getattr(builders.get("raydium_cpmm"), "pools", None),
                    launchlab_pools=getattr(builders.get("raydium_launchlab"), "pools", None),
                    miss_learner=learner, routed_handoff=routed_handoff,
                    damm_pools=getattr(builders.get("meteora_damm_v2"), "pools", None),
                    wake=onchain_wake if cfg.MEMECOIN_ONCHAIN_LANE_ENABLED else None,
                    cu_demotion_sink=cu_demotion_writer(conn_factory, alert_fn=cu_page),
                    volume_cache=rc.get("solana_volume_cache"))
    status['entry_opportunity']['head_callback_ready'] = callable(lane.head_slot_fn)
    collector["lane"] = lane
    if rabbit is not None:                    # memecoin_rabbit_signal.RabbitBackstop
        lane.rabbit_backstop = rabbit
        status["rabbit_breaker"] = rabbit.status
    from sniper.services.memecoin_crowded_fee import CrowdedFee
    lane.crowded = CrowdedFee(now_iso_fn=lane.now_iso_fn)
    lane.crowded.seed(lane.conn)               # today's treated spend survives a restart
    status["crowded_fee"] = lane.crowded.status
    status["mode"] = lane.mode
    # WARM THE E14 READ POOL HERE (Gamma LOW): its first submit creates the pool and a thread
    # (0.34-1.13 ms), which must not land on the first buy's decode. A SHADOW warm-up: whatever it
    # raises is logged and the FUNDED lane starts anyway (Gamma BLOCK on bbe968dfd).
    try:
        from sniper.services import memecoin_real_runner as _rr
        _rr._momentum_read_pool().submit(lambda: None)
    except Exception:
        logger.exception("slot lane: E14 read pool warm-up failed (the lane starts without it)")
    from sniper.services import memecoin_slot_graduation as graduation
    graduation_mode = graduation.mode()
    if graduation_mode != "off" and builders.get("pump_amm") is not None and builders.get("pump_curve") is not None:
        from sniper.execution.pump_native_exit import _rpc_result
        from sniper.execution.solana_native_entry_quote import read_native_state
        graduation_rpc = _context_custody_rpc(rc)  # its own connections: never the lane's pool reads

        def graduation_read(mint, quote_mint=None):
            return read_native_state(mint, lambda method, params: _rpc_result(graduation_rpc, method, params),
                                     commitment="processed",
                                     **({"quote_mint": quote_mint} if quote_mint is not None else {}))
        watcher = graduation.GraduationWatcher(
            read_fn=graduation_read, requeue_fn=buys.put,
            wake_fn=(lane.wake.set if lane.wake is not None else (lambda: None)),
            pending=GRADUATION_PENDING, live=graduation_mode == "live" and lane.mode == "live", log=log,
            clock=clock, deadline_s=cfg.MEMECOIN_SLOT_LANE_GRADUATION_DEADLINE_S,
            poll_s=cfg.MEMECOIN_SLOT_LANE_GRADUATION_POLL_S)
        graduation_ref["watcher"] = watcher
        lane.graduation_offer = watcher.offer
        status["graduation"] = watcher.stats
        status["graduation_lane"] = lane.graduation_stats
        threading.Thread(target=watcher.run, name="slot-lane-graduation", daemon=True).start()
        log("[memecoin] slot lane graduation direct mode=%s live=%s deadline_s=%.1f poll_s=%.2f" % (
            graduation_mode, watcher.live, watcher.deadline_s, watcher.poll_s))
    if migration_queue is not None and amm_pools is not None:
        # Signal startup may have queued a migration already. Bind this consumer only after
        # graduation readiness is settled, so its wake cannot be lost to a callback-ref race.
        def migration_loop():
            while True:
                pool, observed_at = migration_queue.get()
                try:
                    on_migrated(pool, observed_at=observed_at)
                    status['prewarm']['delivered'] += 1
                except Exception:
                    status['prewarm']['errors'] += 1
                    status['prewarm']['last_error'] = 'migration_consumer_error'
                finally:
                    migration_queue.task_done()
        status['prewarm']['ready'] = True
        threading.Thread(target=migration_loop, name='slot-lane-migrations', daemon=True).start()
    try:
        log_venue_start(log, builders, lane.mode)
    except Exception:
        # A start line must never stop the lane: the signal thread is already running.
        logger.exception("slot lane venue start lines failed (lane unaffected)")
    cpmm_builder = builders.get("raydium_cpmm")
    cpmm_verifier = None
    lane_idle = threading.Event()             # set while the lane thread waits for a buy
    lane_idle.set()
    if cpmm_builder is not None:
        from sniper.execution.raydium_cpmm_buy import ShadowVerifier
        cpmm_verifier = ShadowVerifier(rpc=cpmm_builder.pools.rpc, log=log,
                                       sim_rpc=getattr(cpmm_builder, "sim_rpc", None), idle=lane_idle)
        status["cpmm_shadow"] = cpmm_verifier.stats
        status["cpmm_pools"] = cpmm_builder.pools.stats
    launchlab_builder = builders.get("raydium_launchlab")
    launchlab_verifier = None
    if launchlab_builder is not None:
        from sniper.execution.raydium_launchlab_buy import ShadowVerifier as LaunchLabVerifier
        launchlab_verifier = LaunchLabVerifier(rpc=getattr(launchlab_builder, "sim_rpc", None),
                                               log=log, idle=lane_idle)
        status["launchlab_shadow"] = launchlab_verifier.stats
        status["launchlab_pools"] = launchlab_builder.pools.stats
    damm_builder = builders.get("meteora_damm_v2")
    damm_verifier = None
    if damm_builder is not None:
        from sniper.execution.meteora_damm_v2_buy import ShadowVerifier as DammVerifier
        damm_verifier = DammVerifier(log=log, sim_rpc=getattr(damm_builder, "sim_rpc", None),
                                     idle=lane_idle)
        status["damm_v2_shadow"] = damm_verifier.stats
        status["damm_v2_pools"] = damm_builder.pools.stats
    # ONE on-demand leg worker (paper key, never the lane thread) for every venue whose flag is on.
    verifiers = {"raydium_cpmm": cpmm_verifier, "raydium_launchlab": launchlab_verifier,
                 "meteora_damm_v2": damm_verifier}
    exotic_venues = {v for v in EXOTIC_VENUES
                     if v in (getattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", None) or ())}
    ondemand_builders = {venue: builder for venue, builder, on in (
        ("raydium_launchlab", launchlab_builder, cfg.MEMECOIN_SLOT_LANE_LAUNCHLAB_ONDEMAND_LEG
         or "raydium_launchlab" in exotic_venues),
        ("raydium_cpmm", cpmm_builder, getattr(cfg, "MEMECOIN_SLOT_LANE_CPMM_ONDEMAND_LEG", False)
         or "raydium_cpmm" in exotic_venues),
        # pump AMM has no shadow leg: the worker takes its exotic quotes only when listed live (E2)
        ("pump_amm", builders.get("pump_amm"), "pump_amm" in exotic_venues),
        # DAMM v2 follows its own mode (E3): listed and shadow, the worker builds on the paper key
        ("meteora_damm_v2", builders.get("meteora_damm_v2"), "meteora_damm_v2" in exotic_venues),
        # a curve quoted in another token (2026-10-07): live only when listed, never shadow
        ("pump_curve", builders.get("pump_curve"), "pump_curve" in exotic_venues),
        # DBC (2026-10-07): live only, its exotic quotes only when listed
        ("meteora_dbc", builders.get("meteora_dbc"), "meteora_dbc" in exotic_venues))
        if on and builder is not None}
    ondemand = None
    if ondemand_builders:
        from sniper.execution.raydium_launchlab_buy import OnDemandLegWorker

        def ondemand_prior_net(buy, their_usd):
            # The leg worker's own thread: ONE read-only handle per thread, opened on the first
            # floor refusal and reused (Gamma LOW on 92164bee3: a fresh handle per read cost p50
            # 2.57 ms, nearly all schema load, against 0.02 ms reused). get_readonly_connection is
            # autocommit and query_only, so an idle handle pins no WAL; the lane thread's own
            # connection is never shared across threads.
            return slot_prior_net(thread_readonly_conn(), buy, their_usd)

        def venue_admission(buy, quote):
            return admission_refusal(buy, armed_ids=roster["armed"],
                                     sol_usd=prefetcher.snapshot().sol_usd,
                                     kill_tripped=is_kill_tripped(),
                                     amm_pools=getattr(builders.get("pump_amm"), "pools", None),
                                     cpmm_pools=getattr(cpmm_builder, "pools", None),
                                     launchlab_pools=getattr(launchlab_builder, "pools", None),
                                     damm_pools=getattr(builders.get("meteora_damm_v2"), "pools", None),
                                     venue_quote=quote, prior_net_fn=ondemand_prior_net)[0]
        def ondemand_result(buy, result):
            # A LIVE outcome is one exotic line (its fallback is the lane thread's); a shadow one is
            # the venue verifier's simulation and line, as before.
            verifier = verifiers.get(buy.venue)
            if result.get("status") in ("requeued", "live_refused") or verifier is None:
                log(exotic_worker_line(buy, result))
            else:
                verifier.offer(buy, result)
        ondemand = OnDemandLegWorker(builders=ondemand_builders, admission_fn=venue_admission,
                                     snapshot_fn=prefetcher.snapshot, clip_fn=lambda: roster["clip"],
                                     offer_fn=ondemand_result, live_fn=exotic_live,
                                     requeue_fn=buys.put,
                                     refuse_fn=lambda b, reason: buys.put(ExoticRefusal(b, reason)),
                                     cap_open_fn=lane.exotic_cap_open, warm=exotic_warm)
        status["ondemand_leg"] = ondemand.stats
        if exotic_venues & set(ondemand_builders):
            lane.exotic_offer = ondemand.offer
    status["exotic"] = lane.exotic_stats

    if rc.get("slot_deferred_ready") and lane.mode == "live":
        lane.confirmations = SlotConfirmations(conn_factory=conn_factory, put=buys.put, clock=clock)
        rc['slot_snapshot_debits_fn'] = lane.confirmations.snapshot_funding
        status["confirmation_workers"] = lane.confirmations.stats

    def finish_result(buy, result):
        timeout = EXOTIC_SWEEP_S if lane.exotic_offer is not None else None
        try:
            result.update(slot_offsets(buy, result))
        except Exception:
            logger.exception("slot lane offsets failed mint=%s", getattr(buy, "mint", "?"))
        status["last_result"] = {k: result.get(k) for k in (
            "status", "reason", "lane_ms", "build_ms", "head_minus_s", "head_at_recv_minus_s",
            "landing_slot", "landing_minus_s", "position_id", "sig")}
        log(result_log_line(buy, result))
        verifier = verifiers.get(getattr(buy, "venue", None))
        if timeout is not None:
            lane.expire_exotic(lane.clock.monotonic())
        if verifier is not None and not result.get("exotic_pending"):
            # An exotic quote goes to the on-demand leg worker when its venue's flag is on (the
            # worker's result is then the one line); offer() never blocks the lane.
            if not (ondemand is not None
                    and result.get("reason") in ONDEMAND_QUOTE_REASONS
                    and ondemand.offer(buy)):
                result["lane_mode"] = outcome_mode(lane.mode, buy, result)
                verifier.offer(buy, result)

    def lane_loop():
        import queue as _queue
        queue_stats = QueueTiming()
        status['queue_timing'] = queue_stats.snapshot()
        timeout = EXOTIC_SWEEP_S if lane.exotic_offer is not None else None
        while True:
            try:
                buy, enqueued_mono = buys.get(timeout=timeout)
            except _queue.Empty:
                lane.expire_exotic(lane.clock.monotonic())
                continue
            lane_idle.clear()
            if isinstance(buy, CashSettlement):
                lane.confirmations.recovered(buy.pending_id, buy.observed_mono)
                if buys.empty():
                    lane_idle.set()
                continue
            if isinstance(buy, SlotSettlement):
                done = buy
                buy, result = done.buy, done.result
                lane.confirmations.completed(result["pending_id"], done.completed_mono,
                                             status=result.get('status'))
                try:
                    complete = result.pop("_complete", None)
                    if complete is not None:
                        result = complete(result)
                    finish_result(buy, result)
                except Exception:
                    logger.exception("slot lane completion callback failed pending=%s", result["pending_id"])
                finally:
                    if getattr(buy, "source_initiation", None) is not None and routed_handoff is not None:
                        routed_handoff.finish(buy)
                    if buys.empty():
                        lane_idle.set()
                continue
            if isinstance(buy, ExoticRefusal):
                try:
                    if lane.resolve_exotic_refusal(buy):
                        log("[memecoin] slot lane exotic fallback %s %s -> %s sig=%s" % (
                            getattr(buy.buy, "handle", "?"), getattr(buy.buy, "venue", "?"),
                            buy.reason, buy.buy.signature))
                except Exception:
                    logger.exception("slot lane exotic refusal failed sig=%s", buy.buy.signature)
                if buys.empty():
                    lane_idle.set()
                continue
            try:
                result = lane.handle(buy, enqueued_mono=enqueued_mono)
            except Exception:
                logger.exception("slot lane handle failed mint=%s", getattr(buy, "mint", "?"))
                result = {"status": "error"}
            status["handled"] += 1
            queue_stats.record(result)
            status["queue_timing"] = queue_stats.snapshot()
            if result.get("_settlement") is not None:
                lane.confirmations.offer(buy, result)
                status["last_result"] = {key: result.get(key) for key in ("status", "pending_id", "sig")}
            else:
                try:
                    finish_result(buy, result)
                finally:
                    if getattr(buy, "source_initiation", None) is not None and routed_handoff is not None:
                        routed_handoff.finish(buy)
            if buys.empty():
                lane_idle.set()

    for group in LanePrefetcher.PERIOD_S:
        threading.Thread(target=lambda g=group: essential_loop(g),
                         name="slot-lane-prefetch-" + group, daemon=True).start()
    threading.Thread(target=prefetch_loop, name="slot-lane-prefetch", daemon=True).start()
    threading.Thread(target=lane_loop, name="slot-lane", daemon=True).start()
    status['fast_lane_only'] = cfg.MEMECOIN_SOLANA_FAST_LANE_ONLY
    status['j7_verified_backup'] = cfg.MEMECOIN_J7_SOLANA_VERIFIED_BACKUP
    status['max_copy_premium_pct'] = cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT
    status['copy_market_movement_only'] = cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY
    status['copy_market_unknown_policy'] = 'proceed' if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY else None
    status['nozomi_ab_ready'] = lane.nozomi_ab
    status['nozomi_wallet_scope'] = ('all' if '*' in cfg.MEMECOIN_SLOT_LANE_NOZOMI_AB_WALLETS
                                     else 'listed')
    status['receipt_resume_ready'] = False
    if lane.mode == 'live' and routed_handoff is not None and receipt_resume_ready is not None:
        receipt_resume_ready(routed_handoff.offer)
        status['receipt_resume_ready'] = True
    return status


def sol_usd_from_jupiter():
    """USD for 1 SOL from the funded Jupiter seam (the executors' own quote path), or None."""
    from sniper.execution.solana_real_seams import _quote_with_fallback
    from sniper.execution.solana_wallet import USDC_MINT
    quote = _quote_with_fallback("So11111111111111111111111111111111111111112", USDC_MINT,
                                 1_000_000_000, 50, None)
    out = float((quote or {}).get("outAmount") or 0) / 1e6
    return out if out > 0 else None


def _context_custody_rpc(rc):
    from sniper.execution.pump_curve_buy import keepalive_custody_rpc
    return keepalive_custody_rpc(rc.get('withdraw_rpc_url') or rc.get('rpc_url'),
                                ready_fn=rc.get('custody_ready_fn'))


def start_from_context(rc, *, log, mirror=None, onchain_wake=None, receipt_resume_ready=None,
                       measurement=None):
    """Production wiring: the Solana funded context -> a running slot lane."""
    import os
    from sniper.execution.pump_curve_buy import SlotLaneCurveBuilder
    from sniper.execution.pumpswap_buy import SlotLanePumpSwapBuilder
    from sniper.persistence.db_core import get_connection, get_readonly_connection
    if measurement is None:
        from sniper.services.memecoin_feed_arrivals import ReceiptJournal
        from uuid import uuid4
        from pathlib import Path
        try:
            boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except OSError:
            boot_id = 'unknown-boot:' + str(uuid4())
        measurement = ReceiptJournal(producer_run_id='source:' + str(uuid4()), boot_id=boot_id,
                                     clock_domain='process-monotonic:' + str(os.getpid()))
    from sniper.services.memecoin_slot_signal import (
        start_migration_thread, start_signal_thread, ws_url_from_http)
    from sniper.util.state_paths import state_path
    from sniper.services.memecoin_solana_provider import infra_provider, provider_urls
    from sniper.services.memecoin_shyft_signal import live_config
    from sniper.services.memecoin_shyft_prepare import prepare_config as rabbit_config
    from sniper.services.memecoin_slot_signal import _shares_funded_key
    profile = infra_provider()
    shyft_config = live_config()
    prepare_config = rabbit_config()
    if prepare_config is not None and profile != 'shyft':
        raise RuntimeError('rabbit preparation requires Shyft provider')
    signal_http = next(iter(provider_urls('signal')), '')
    ws_url = ws_url_from_http(signal_http) if profile == 'legacy' else None
    if profile == 'legacy' and not ws_url:
        raise RuntimeError("ONCHAIN_WATCH_HELIUS_RPC_URL is unset: the slot lane has no socket")
    from sniper.services.memecoin_research_rpc import funded_dependency_url
    funded_read = funded_dependency_url("slot_signal")
    if profile == 'shyft' and _shares_funded_key(signal_http) and funded_read is None:
        raise RuntimeError('slot signal needs its own endpoint, never the funded custody key')
    # Two legs, one per venue (the curve path stays as proven), each re-quoted at most every
    # 1.5 s: about 2.7 funded Jupiter calls a second while the lane runs, inside Developer 10 rps.
    bid_kwargs = ({'priority_fee_fn': rc['entry_bid_fn']} if rc.get('entry_bid_fn') else {})
    builders = {"pump_curve": SlotLaneCurveBuilder(user=rc["pubkey"], refresh_after_s=LEG_REFRESH_S,
                                                   rpc=_context_custody_rpc(rc),
                                                   **bid_kwargs),
                # Its pool cache keeps the addresses of pools it learned, so a restart re-reads them
                # (in bulk, from chain) instead of refusing them pool_unknown again.
                "pump_amm": SlotLanePumpSwapBuilder(
                    user=rc["pubkey"], refresh_after_s=LEG_REFRESH_S,
                    rpc=_context_custody_rpc(rc),
                    pool_store_path=str(state_path("data", "slot_lane_pumpswap_pools.json")),
                    **bid_kwargs)}
    from sniper.execution.desk_native_curve import share_curve_builder
    share_curve_builder(builders["pump_curve"])
    from sniper.execution.raydium_cpmm_buy import SlotLaneCpmmBuilder, lane_mode as cpmm_lane_mode
    if cpmm_lane_mode() != "off" and routed_mode() != "off":
        # Live for SOL/USDC pools when MEMECOIN_SLOT_LANE_CPMM=live (2026-09-28); shadow otherwise and
        # for every exotic quote. Its WSOL pools spend the PumpSwap builder's leg: no extra Jupiter call. Its
        # ZEC pools spend its own leg, only when MEMECOIN_SLOT_LANE_CPMM_ZEC_LEG is on: a third leg
        # at the same cadence, about 1.3 funded Jupiter calls a second more (role slot_leg).
        amm_builder = builders["pump_amm"]
        builders["raydium_cpmm"] = SlotLaneCpmmBuilder(
            user=rc["pubkey"], rpc=amm_builder.rpc, leg_fn=lambda: amm_builder._leg,
            refresh_after_s=LEG_REFRESH_S)
        # The shadow simulation runs on the lane's own watch key, never the custody RPC.
        from sniper.execution.raydium_launchlab_buy import http_rpc
        builders["raydium_cpmm"].sim_rpc = http_rpc(signal_http)
    from sniper.execution.raydium_cpmm_buy import WSOL_MINT
    from sniper.execution.raydium_launchlab_buy import (
        SlotLaneLaunchLabBuilder, lane_mode as launchlab_lane_mode)
    if launchlab_lane_mode() != "off" and routed_mode() != "off":
        # Shadow only, and no leg of its own: SOL pools spend the PumpSwap builder's leg, ZEC pools
        # the CPMM builder's (when that builder and its ZEC leg are on), USDC pools none.
        amm_builder, cpmm_builder = builders["pump_amm"], builders.get("raydium_cpmm")
        leg_for = (cpmm_builder._leg_for if cpmm_builder is not None
                   else lambda mint: amm_builder._leg if mint == WSOL_MINT else None)
        builders["raydium_launchlab"] = SlotLaneLaunchLabBuilder(
            user=rc["pubkey"], rpc=amm_builder.rpc, leg_for=leg_for,
            quote_usd=cpmm_builder.pools.quote_usd if cpmm_builder is not None else None)
        # The shadow simulation runs on the lane's own watch key, never the custody RPC.
        from sniper.execution.raydium_launchlab_buy import http_rpc
        builders["raydium_launchlab"].sim_rpc = http_rpc(signal_http)

    from sniper.execution.meteora_damm_v2_buy import SlotLaneDammV2Builder, lane_mode as damm_lane_mode
    if damm_lane_mode() != "off" and routed_mode() != "off":
        # B2: shadow first. Its SOL pools spend the PumpSwap builder's leg: no extra Jupiter call.
        # Pool learning uses its own paced read client, never the custody RPC or fill path. Its learned
        # coins are stored and re-read from chain at start (2026-10-02, operator yes), and a later
        # buy of a known coin is checked for a pool move: one getTransaction per learner pass.
        amm_builder = builders["pump_amm"]
        from sniper.execution.raydium_launchlab_buy import http_rpc
        from sniper.services.memecoin_slot_signal import _shares_funded_key
        read_url = rc.get("read_rpc_url") or cfg.solana_read_rpc()
        if _shares_funded_key(read_url) and funded_read is None:
            log("[memecoin] DAMM discovery disabled: read endpoint shares funded key")
        else:
            discovery_rpc = http_rpc(read_url, timeout_s=2.0)
            next_pool_read = 0.0

            def pool_rpc(method, params):
                # One cache learner owns this closure. No funded connection, entry lock, or entry wait.
                nonlocal next_pool_read
                time.sleep(max(0.0, next_pool_read - time.monotonic()))
                next_pool_read = time.monotonic() + 0.5
                return discovery_rpc(method, params)

            builders["meteora_damm_v2"] = SlotLaneDammV2Builder(
                user=rc["pubkey"], rpc=amm_builder.rpc, leg_fn=lambda: amm_builder._leg,
                pool_store_path=str(state_path("data", "slot_lane_damm_v2_pools.json")), pool_rpc=pool_rpc,
                route_targets_fn=damm_route_targets_reader(get_readonly_connection))
            from sniper.execution.raydium_launchlab_buy import http_rpc
            builders["meteora_damm_v2"].sim_rpc = http_rpc(signal_http)

    from sniper.execution.meteora_dbc_buy import SlotLaneDbcBuilder, dbc_live
    if dbc_live() and routed_mode() != "off":
        # DBC LIVE (2026-10-07, operator order): SOL curves spend the PumpSwap builder's leg, USDC the
        # clip, other quotes the on-demand leg. Each buy carries its own learned facts (the MissLearner's
        # DBC thread, on the watch key): the builder reads nothing.
        dbc_amm = builders["pump_amm"]
        builders["meteora_dbc"] = SlotLaneDbcBuilder(user=rc["pubkey"], rpc=dbc_amm.rpc,
                                                     leg_fn=lambda: dbc_amm._leg)

    # THE RABBIT-LED FIRE's backstop (memecoin_rabbit_signal.RabbitBackstop): its one curve read on
    # the funded read role on its own keep-alive client (never custody, never GT), the
    # source_not_landed exit through the source-sell mirror, and the breaker. Wired on live lanes
    # with the stage-1 arm on; MEMECOIN_SLOT_LANE_RABBIT_BUY is then read per signal (the hot kill).
    # No read role or no mirror: nothing is wired and no rabbit frame can ever fire.
    rabbit = None
    # HELIUS PRECONFIRMATIONS (memecoin_preconf_signal, operator 2026-10-17 "turn preconf on"): the third
    # signal feed, on the Shyft profile's live lane only, through the same backstop. Read at start;
    # MEMECOIN_SLOT_LANE_PRECONF is then also the per-signal hot kill for its fires.
    preconf = None
    if cfg.MEMECOIN_SLOT_LANE_PRECONF and profile == "shyft" and lane_mode() == "live":
        from sniper.services.memecoin_preconf_signal import preconf_url
        url = preconf_url()
        if url is None:
            log("[memecoin] slot lane preconf NOT WIRED: no Helius key (MEMECOIN_SLOT_LANE_PRECONF_URL or "
                "MEMECOIN_RESEARCH_HELIUS_RPC_URL)")
        else:
            preconf = {"url": url}
    if (((prepare_config is not None and cfg.MEMECOIN_SLOT_LANE_RABBIT_SIGNAL) or preconf is not None)
            and lane_mode() == "live"):
        if funded_read is None or mirror is None or not getattr(mirror, "enabled", False):
            log("[memecoin] slot lane pre-execution buys (rabbit, preconf) NOT WIRED: %s"
                % ("no funded read role" if funded_read is None else "no source-sell mirror"))
        else:
            from urllib.parse import urlsplit
            from sniper.execution.pump_curve_buy import keepalive_custody_rpc
            from sniper.services.memecoin_rabbit_signal import RabbitBackstop
            rabbit = RabbitBackstop(read=keepalive_custody_rpc(funded_read), exit_offer=mirror.offer_not_landed)
            mirror.rabbit_backstop = rabbit
            log("[memecoin] slot lane pre-execution buys wired: rabbit %s (MEMECOIN_SLOT_LANE_RABBIT_BUY=%s), "
                "preconf %s (MEMECOIN_SLOT_LANE_PRECONF=%s, daily credit cap %s); read per signal"
                % (urlsplit(prepare_config[1]).hostname if prepare_config is not None else "off",
                   cfg.MEMECOIN_SLOT_LANE_RABBIT_BUY,
                   ("beta.helius-rpc.com sources=%s" % ",".join(cfg.MEMECOIN_SLOT_LANE_PRECONF_SOURCES))
                   if preconf else "off",
                   cfg.MEMECOIN_SLOT_LANE_PRECONF, cfg.MEMECOIN_SLOT_LANE_PRECONF_DAILY_CREDITS))

    def signal_start_fn(on_buy, *, owners_fn, status, on_sell=None, **routed):
        return start_signal_thread(on_buy, owners_fn=owners_fn, ws_url=ws_url, status=status,
                                   on_sell=on_sell, shyft_config=shyft_config,
                                   infra_provider=profile, prepare_config=prepare_config,
                                   **({"rabbit": rabbit} if rabbit is not None else {}),
                                   **({"preconf": preconf} if preconf is not None else {}), **routed)

    # Routed-buy baselines go over HTTP on the socket's own key, one owner at a time (the signal
    # thread spaces them); start_signal_thread has already refused that key if it is the funded one.
    routed_baseline_fn = (token_accounts_baseline(signal_http)
                          if routed_mode() != "off" else None)

    migration_start_fn = damm_prewarm_start_fn = None
    migration_on_signal = bool(cfg.MEMECOIN_SLOT_LANE_MIGRATION_PREWARM and profile == 'shyft')
    if cfg.MEMECOIN_SLOT_LANE_MIGRATION_PREWARM and profile == 'legacy':
        # The KEYLESS read socket (never the funded key, never the signal socket's key): one
        # subscription, ~30 MB a day, no HTTP request against the read lane's rate budget.
        read_ws = ws_url_from_http(cfg.solana_read_rpc())
        # The public socket stalls; a second one on the research key runs beside it (2026-10-07).
        from sniper.services.memecoin_slot_signal import (
            chain_newest_migration, research_migration_ws, start_migration_listeners)
        research_ws = (research_migration_ws() if cfg.MEMECOIN_SLOT_LANE_MIGRATION_RESEARCH_WS
                       else (None, "off"))

        def migration_chain_newest():
            from sniper.execution.solana_real_seams import _rpc_call
            from sniper.services.memecoin_research_rpc import research_urls
            urls = research_urls()
            if not urls:
                return None

            def rpc(method, params):
                _status, body = _rpc_call(urls[0], method, params, None)
                if not isinstance(body, dict) or "error" in body:
                    raise RuntimeError("%s refused" % method)
                return body.get("result")
            return chain_newest_migration(rpc)

        def migration_page(text):
            from sniper.services.memecoin_alerts import send_alert
            send_alert("SLOT-LANE MIGRATION DEAF", text, icon="🔴")

        def migration_start_fn(on_pool, *, status):
            return start_migration_listeners(
                on_pool, sockets={"public": (read_ws, None), "research": research_ws}, status=status,
                chain_newest_fn=migration_chain_newest, alert_fn=migration_page)

        if "meteora_damm_v2" in builders:
            from sniper.execution.meteora_damm_v2_buy import PROGRAM, creation_signature

            def damm_prewarm_start_fn(on_signature, *, status):
                return start_migration_thread(on_signature, ws_url=read_ws, status=status,
                                              program=PROGRAM, notification_target=creation_signature)

    return start_slot_lane(
        rc, builders=builders, signal_start_fn=signal_start_fn, sol_usd_fn=sol_usd_from_jupiter,
        log=log, roster_path=str(state_path("data", "solana_roster_owners.jsonl")),
        conn_factory=get_connection, readonly_conn_factory=get_readonly_connection,
        mirror=mirror, migration_start_fn=migration_start_fn,
        routed_baseline_fn=routed_baseline_fn, onchain_wake=onchain_wake,
        damm_prewarm_start_fn=damm_prewarm_start_fn, receipt_resume_ready=receipt_resume_ready,
        measurement=measurement, migration_on_signal=migration_on_signal,
        prepare_on_signal=prepare_config is not None, rabbit=rabbit,
        damm_on_signal=bool(profile == "shyft" and cfg.MEMECOIN_SLOT_LANE_MIGRATION_PREWARM
                            and "meteora_damm_v2" in builders))


def lane_mode():
    return str(getattr(cfg, "MEMECOIN_SOLANA_SLOT_LANE_MODE", "shadow") or "shadow").strip().lower()


def lane_enabled():
    return str(getattr(cfg, "MEMECOIN_SOLANA_SLOT_LANE_ENABLED", "0")).strip().lower() in {
        "1", "true", "yes"}


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()
