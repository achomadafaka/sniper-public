"""On-chain detection lane (stage 2): armed wallets' chain-confirmed buys -> real entry.

The standalone research watcher (`scripts/research/onchain_wallet_watch.py`) subscribes to the
confirmed wallets' addresses and writes each detected buy to `onchain_wallet_detections` about
2 seconds after chain confirmation. The fomo feed reports the same buy 3-15 seconds later, and
sometimes never (two frankdegods buys on 2026-08-20/21 had no feed row hours later). The
measured impulse edge decays inside ~10 seconds, so the detection row is the fastest copyable
signal we have.

This lane polls that table and feeds each FRESH detection through the SAME `real_hook`
callable the feed lanes use. Every gate still runs: chain capability, the per-chain roster
gate, mcap resolution + ceiling, turnover, safety checks, risk caps, and the funded contract.
The lane adds no authority: an unarmed wallet's detection is filtered here and would also
refuse `not_approved` in the hook.

Buys only. The watcher does not classify sells, so exits rely on the frozen position rails
(take-profit, stop, catastrophe, timecap) plus any fomo-reported sell mirror.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from sniper.services.memecoin_refusal_capture import SLIPPAGE_FAIL_REASONS

logger = logging.getLogger(__name__)


def _heartbeat_path():
    # State-root resolved, NOT __file__-relative: the pinned release runs from its own
    # code_root, so a __file__-derived path writes into .runtime/.../releases/<sha>/data
    # (that exact bug shipped on 2026-08-21 and the file landed in the release dir).
    from sniper.util.state_paths import state_path
    return state_path("data", "onchain_lane.heartbeat")


_WAKE_KEYS = ("bound", "datagrams", "bind_failures", "last_error")


def write_lane_heartbeat(*, cursor, now_iso, path=None, wake=None, workers=None):
    """One JSON line per sweep. A stale file means the lane thread died while the runner
    heartbeat stayed green — the runner does not know about this thread. `wake` is the
    cross-process wake's counters (memecoin_onchain_wake.LaneWake.stats), whitelisted."""
    payload = {"ts": now_iso, "cursor": cursor}
    if workers is not None:
        snapshot = {network: dict(state) for network, state in workers.items()}
        stamps = [state.get("ts") for state in snapshot.values()]
        payload.update(workers=snapshot, coordinator_ts=now_iso,
                       ts=min(stamps) if stamps and all(stamps) else None)
    if wake is not None:
        payload["wake"] = {key: wake.get(key) for key in _WAKE_KEYS}
    try:
        (path if path is not None else _heartbeat_path()).write_text(json.dumps(payload))
    except OSError:
        pass


def _parse_iso(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _finite_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def paid_size_usd(row):
    """The source's size on the lanes' one basis (MEMECOIN_SOURCE_SIZE_BASIS, default gross, the J7
    basis). The watcher stores the pump pool leg NET of fees as source_size_usd and the paid amount
    as source_valuation_json.gross_swap_size_usd (Roveryaps 46hQdz8Qnv: 196.55 net, 199.01 paid).
    Route and balance-delta rows already hold what the owner paid. A missing, unreadable or
    below-net gross keeps the stored size."""
    from sniper.settings import memecoin as cfg
    net = _finite_number(row["source_size_usd"])
    if net is None or str(getattr(cfg, "MEMECOIN_SOURCE_SIZE_BASIS", "gross")) == "net":
        return net
    try:
        valuation = json.loads(row["source_valuation_json"] or "{}")
    except (IndexError, KeyError, TypeError, ValueError):
        return net
    if not isinstance(valuation, dict) or valuation.get("basis") != "pump_event_net_sol":
        return net
    gross = _finite_number(valuation.get("gross_swap_size_usd"))
    return gross if gross is not None and gross >= net else net


def source_time(row):
    """The instant a detection's AGE is measured from (2026-09-28, Alpha).

    A live detection: its arrival stamp, detected_at, exactly as before. A GAP-BACKFILL detection
    (source_valuation_json.backfill): detected_at is when the backfill READ it, up to the lane's
    whole window after the buy, so its age comes from the source block's time (block_time,
    floored to the second: an age at most 1 s too old, the safe side). A backfill row without a
    block time has no known age and returns None, which no freshness check passes."""
    def get(key):
        try:
            return row[key]
        except (KeyError, IndexError):
            return None
    try:
        backfill = bool(json.loads(get("source_valuation_json") or "{}").get("backfill"))
    except (TypeError, ValueError, AttributeError):
        backfill = False
    if not backfill:
        return get("detected_at")
    block_time = get("block_time")
    if not block_time:
        return None
    return datetime.fromtimestamp(int(block_time), timezone.utc).isoformat()


def chase_reference(row):
    """(price, basis) the chase gate compares our quote with (2026-09-28, operator: "measure what we
    can actually control"). Under MEMECOIN_REAL_CHASE_REFERENCE=post_trade, the source's POST-trade
    price from the watcher's receipt valuation (source_post_price); otherwise, or when that price is
    absent, zero, negative or not finite, None and "avg_fill": the gate keeps the source's average
    fill, as before. Spec 2026-09-28-memecoin-chase-post-trade-reference-design.md."""
    from sniper.settings import memecoin as cfg
    if str(getattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "avg_fill")) != "post_trade":
        return None, "avg_fill"
    try:
        valuation = json.loads(row["source_valuation_json"] or "{}")
    except (IndexError, KeyError, TypeError, ValueError):
        return None, "avg_fill"
    post = _finite_number(valuation.get("source_post_price")) if isinstance(valuation, dict) else None
    if not post:
        return None, "avg_fill"
    # RISK BOUND (Lambda F1): never more than MEMECOIN_CHASE_POST_TRADE_MAX_RATIO x the average fill.
    avg = _finite_number(row["source_price"])
    ceiling = cfg.chase_post_trade_max_ratio()        # below 1 falls back to 2.0 (Beta F6)
    if avg and post > avg * ceiling:
        return avg * ceiling, "post_trade_clamped"
    return post, "post_trade"


def reference_note(row, basis):
    """What the executor's chase substage must know about a post-trade reference, or None:
    a routed leg's stated looseness (its USDC average carries upstream leg and router fees; Alpha,
    2026-09-28) and a clamp (Lambda's review 6: `post_trade_clamped` never reached the executor,
    which re-derives the basis from the price alone, nor the retry rebuild)."""
    from sniper.services.memecoin_slot_signal import ROUTED_REFERENCE_NOTE
    from sniper.settings import memecoin as cfg
    try:
        leg = json.loads(row["source_valuation_json"] or "{}").get("impact_leg")
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        leg = None
    parts = [f"routed_leg:{leg}; {ROUTED_REFERENCE_NOTE}"] if leg else []
    if basis == "post_trade_clamped":
        parts.append("post_trade_clamped at %sx the average fill"
                     % cfg.chase_post_trade_max_ratio())
    return "; ".join(parts) or None


def evm_chase_reference(row, network_id):
    """The EvmChaseReference for an EVM detection row carrying `evm_post`, else None."""
    if str(network_id) == "solana":
        return None
    try:
        post = json.loads(row["source_valuation_json"] or "{}").get("evm_post")
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        return None
    if not isinstance(post, dict):
        return None
    from sniper.services.memecoin_evm_post_trade import EvmChaseReference
    return EvmChaseReference(network_id, post=post)


# Gamma LOW 1 (2026-09-29): Q = spent / (r - 1) holds on a CONSTANT-PRODUCT leg only. The pump
# curve's ratio is over its VIRTUAL reserves, and a launchlab or unlabelled leg is some other reserve.
CONSTANT_PRODUCT_LEGS = frozenset({"pump_amm", "raydium_cpmm"})


def implied_quote_reserve(row):
    """(quote reserve in USD, leg) implied by the source's own constant-product trade, or
    (None, None). The watcher's `source_impact_ratio` is the post-trade spot over the AVERAGE fill,
    so r = 1 + dQ/Q and Q = dQ / (r - 1) (Gamma, docs/audits/2026-09-29-dexscreener-e6-e7-own-sources.md).
    The leg is the routed valuation's `impact_leg`, else the pump program that emitted the event."""
    from sniper.services.memecoin_slot_signal import PUMP_AMM_PROGRAM, PUMP_PROGRAM
    try:
        valuation = json.loads(row["source_valuation_json"] or "{}")
        leg = valuation.get("impact_leg") or {PUMP_PROGRAM: "pump_curve",
                                              PUMP_AMM_PROGRAM: "pump_amm"}.get(
                                                  valuation.get("program_id"))
        if leg not in CONSTANT_PRODUCT_LEGS:
            return None, None
        r = float(valuation.get("source_impact_ratio"))
        spent = float(valuation.get("source_size_usd"))
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        return None, None
    if not (math.isfinite(r) and math.isfinite(spent) and r > 1.0 and spent > 0):
        return None, None
    q = spent / (r - 1.0)
    return (q, leg) if math.isfinite(q) and q > 0 else (None, None)


def pump_mint_proven(ev):
    """True when this Solana detection proves its coin is a pump.fun mint (1e9 minted, supply only
    shrinks), from the watcher's receipt and no read: the event came from pump's curve, which trades
    only mints pump created, or from the canonical PumpSwap pool that pump's ["pool-authority", mint]
    PDA creates on migration (the slot lane's not_pump_mint test, derived). Never the "...pump"
    suffix: anyone can grind it. False on another chain, another pool, or a row from before the
    watcher kept the pool."""
    from sniper.services.memecoin_slot_signal import PUMP_AMM_PROGRAM, PUMP_PROGRAM
    program, pool = getattr(ev, "source_program", None), getattr(ev, "source_pool", None)
    if getattr(ev, "network_id", None) != "solana" or program not in (PUMP_PROGRAM, PUMP_AMM_PROGRAM):
        return False
    if program == PUMP_PROGRAM:
        return True
    if not pool:
        return False
    from sniper.execution.solana_native_entry_quote import accounts_for
    try:
        return accounts_for(ev.mint)["pool"] == pool
    except (TypeError, ValueError):
        return False


def _source_program_and_pool(row):
    """The watcher's (program_id, pool) for this detection, or (None, None)."""
    try:
        valuation = json.loads(row["source_valuation_json"] or "{}")
        return valuation.get("program_id"), valuation.get("pool")
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        return None, None


class OnchainEv:
    """The event shape `real_hook` expects, built from one detection row.

    The watcher supplies an on-chain source price and source size when it can prove both. The
    hook resolves the live market cap itself and keeps the ordinary chase and source-size gates.
    """
    side = "buy"
    signal_source = "onchain_watch"
    their_market_cap = None
    ticker = None
    clock_domain = "observed_real_utc"
    source_timestamp_trust = "observed_detection"
    slot_lane_failure_reason = None
    relay_origin_error = None
    j7_verified_backup = False
    j7_native_failure_backup = False

    def __init__(self, row):
        self.mint = row["mint"]
        self.source_user_id = row["source_user_id"]
        self.source_handle = row["handle"]
        self.network_id = str(row["network_id"] or "solana")
        def optional(key):
            try:
                return row[key]
            except (KeyError, IndexError):
                return None
        self.source_wallet = optional("address")
        self.source_block_number = optional("source_slot")
        from sniper.util.receipt_clock import freeze_receipt_clock
        self.receipt_clock, self.receipt_stages = None, ()
        try:
            valuation = json.loads(optional("source_valuation_json") or "{}")
            self.j7_native_failure_backup = valuation.get("j7_native_failure_backup") is True
            self.source_initiation = valuation.get("source_initiation")
            self.source_pre_atomic = valuation.get("source_pre_atomic")
            # The watcher's Relay verification error (Relay option A names the refusal from it).
            self.relay_origin_error = valuation.get("relay_origin_error")
            self.receipt_clock = freeze_receipt_clock(valuation.get("receipt_clock"))
            stages = valuation.get("receipt_stages")
            if isinstance(stages, (list, tuple)):
                self.receipt_stages = tuple(dict(stage) for stage in stages if isinstance(stage, dict))
        except (TypeError, ValueError, AttributeError):
            self.source_initiation = self.relay_origin_error = None
            self.source_pre_atomic = None
        self.source_evidence = {"source_initiation": self.source_initiation,
                                "source_pre_atomic": self.source_pre_atomic,
                                "network_id": self.network_id, "mint": self.mint,
                                "source_wallet": self.source_wallet,
                                "source_block_number": self.source_block_number}
        self.their_size_usd = paid_size_usd(row)
        self.their_price = _finite_number(row["source_price"])
        # The chase gate's reference; their_price stays the average fill for every other use.
        self.chase_reference_price, self.chase_reference_basis = chase_reference(row)
        self.chase_reference_note = (reference_note(row, self.chase_reference_basis)
                                     if self.chase_reference_price else None)
        # EVM rows: the watcher's post-trade facts for the leg the source bought through. They are
        # bound to OUR pool at each gate (memecoin_evm_post_trade), never used as a price here.
        self.evm_chase_reference = evm_chase_reference(row, self.network_id)
        # E6 telemetry (2026-09-29): the quote reserve this trade implies on a constant-product
        # leg, spent / (r - 1), and which leg. Telemetry only; None when unknown.
        self.implied_quote_reserve_usd, self.implied_quote_leg = implied_quote_reserve(row)
        # The pump-mint proof the funded gate prices a pump coin's history mcap with (523c9968).
        self.source_program, self.source_pool = _source_program_and_pool(row)
        self.fomo_event_id = f"onchain:{row['signature']}"
        detected = row["detected_at"]
        self.created_at = source_time(row)     # the arrival stamp, or a backfill's block time
        self.received_at = detected
        self.received_at_normalized = detected


RETRY_EVENT_KIND = "onchain"
_ORIGIN_PREFIX = "onchain:"


def retry_payload(ev):
    """The fields the entry retry lane stores for an OnchainEv (review section 4, 2026-09-27).

    OnchainEv is a plain class, so the lane's dataclass serialisation skipped it and a transient
    miss on this lane was never re-quoted. The field list is explicit: rebuilding through CopyEvent
    would drop `source_timestamp_trust` and the trace would call a chain-observed time a claimed
    one. Unknown source size and price stay null, never 0."""
    return {"event_kind": RETRY_EVENT_KIND, "side": ev.side, "signal_source": ev.signal_source,
            "mint": ev.mint, "source_user_id": ev.source_user_id,
            "source_handle": ev.source_handle, "network_id": ev.network_id,
            "their_size_usd": ev.their_size_usd, "their_price": ev.their_price,
            "fomo_event_id": ev.fomo_event_id, "detected_at": ev.created_at,
            "clock_domain": ev.clock_domain, "source_timestamp_trust": ev.source_timestamp_trust,
            "delivery_lane": getattr(ev, "delivery_lane", None),
            "slot_lane_failure_reason": getattr(ev, "slot_lane_failure_reason", None),
            # Gamma MED-2: an admitted fallback stays admitted on its retry or dip re-attempt.
            # The class rides; admission is re-judged against the knob in force at rebuild.
            "slot_fallback_class": getattr(ev, "slot_fallback_class", None),
            "j7_verified_backup": getattr(ev, "j7_verified_backup", False),
            "j7_native_failure_backup": getattr(ev, "j7_native_failure_backup", False),
            "source_wallet": getattr(ev, "source_wallet", None),
            "source_block_number": getattr(ev, "source_block_number", None),
            "receipt_clock": getattr(ev, "receipt_clock", None),
            "receipt_stages": getattr(ev, "receipt_stages", ()),
            "source_initiation": getattr(ev, "source_initiation", None),
            "source_pre_atomic": getattr(ev, "source_pre_atomic", None),
            "chase_reference_price": getattr(ev, "chase_reference_price", None),
            "chase_reference_note": getattr(ev, "chase_reference_note", None),
            # The pump-mint proof and the E6 telemetry (Codex review 2026-10-06): dropped here, a
            # pump curve buy proved a pump mint on its first attempt and not on its retry.
            "source_program": getattr(ev, "source_program", None),
            "source_pool": getattr(ev, "source_pool", None),
            "implied_quote_reserve_usd": getattr(ev, "implied_quote_reserve_usd", None),
            "implied_quote_leg": getattr(ev, "implied_quote_leg", None),
            "evm_post": (ev.evm_chase_reference.payload()
                         if getattr(ev, "evm_chase_reference", None) is not None else None)}


def event_from_retry_payload(data):
    """The OnchainEv a retry_payload came from, with the same origin id. Raises ValueError."""
    origin = str(data.get("fomo_event_id") or "")
    if data.get("event_kind") != RETRY_EVENT_KIND or not origin.startswith(_ORIGIN_PREFIX):
        raise ValueError("not an on-chain retry payload")
    ev = OnchainEv({"mint": data["mint"], "source_user_id": data["source_user_id"],
                    "address": data.get("source_wallet"), "source_slot": data.get("source_block_number"),
                    "handle": data["source_handle"], "network_id": data["network_id"],
                    "source_size_usd": data["their_size_usd"], "source_price": data["their_price"],
                    "signature": origin[len(_ORIGIN_PREFIX):], "detected_at": data["detected_at"],
                    # The first attempt's reference (None if it ran under avg_fill), re-judged
                    # under the knob in force now.
                    "source_valuation_json": json.dumps(
                        {"source_post_price": data.get("chase_reference_price"),
                         "receipt_clock": data.get("receipt_clock"),
                         "receipt_stages": data.get("receipt_stages", ()),
                         "source_initiation": data.get("source_initiation"),
                         "source_pre_atomic": data.get("source_pre_atomic"),
                         "evm_post": data.get("evm_post"), "program_id": data.get("source_program"),
                         "pool": data.get("source_pool")})})
    ev.implied_quote_reserve_usd = data.get("implied_quote_reserve_usd")
    ev.implied_quote_leg = data.get("implied_quote_leg")
    if data.get("delivery_lane"):
        ev.delivery_lane = data["delivery_lane"]
    ev.slot_lane_failure_reason = data.get("slot_lane_failure_reason")
    ev.chase_reference_note = data.get("chase_reference_note") if ev.chase_reference_price else None
    from sniper.settings import memecoin as cfg
    ev.slot_fallback_class = data.get("slot_fallback_class")
    ev.j7_verified_backup = data.get('j7_verified_backup') is True
    ev.j7_native_failure_backup = data.get('j7_native_failure_backup') is True
    ev.native_failure_fallback = ev.slot_fallback_class in cfg.MEMECOIN_SOLANA_FALLBACK_CLASSES
    return ev


def retry_skip_reason(conn, ev, now_iso, *, max_age_s):
    """Why a queued on-chain retry must not run, or None (Alpha, 2026-09-27).

    stale_for_lane      older than the lane's own freshness: a retry never admits a staler signal
                        than the lane's first attempt would (unparseable time counts as stale).
    duplicate_of_feed   J7 has since run this very transaction through the hook; its attempt, and
                        its own retry row, own the decision.
    """
    from sniper.services.memecoin_runner import is_stale_entry
    if is_stale_entry(ev.created_at, now_iso, max_age_s):
        return "stale_for_lane"
    if already_attempted_from_another_lane(conn, ev.fomo_event_id[len(_ORIGIN_PREFIX):],
                                          ev.created_at, source_user_id=ev.source_user_id):
        return "duplicate_of_feed"
    return None


def wait_for_next_sweep(wake, poll_s):
    """Sleep until the next sweep: `poll_s` (floor 0.25 s), or at once when an in-process writer
    sets `wake` after inserting a detection (the slot lane's routed handoff, 2026-09-27). With
    nothing setting it this is exactly the old sleep. Clearing after the wait loses nothing: a
    writer inserts its row BEFORE it sets the event, so the sweep that follows sees the row.
    Returns True when woken early."""
    timeout = max(0.25, float(poll_s))
    if wake is None:
        time.sleep(timeout)
        return False
    woke = wake.wait(timeout)
    wake.clear()
    return woke


def already_attempted_from_another_lane(conn, signature, detected_at, *, lookback_s=120.0,
                                        source_user_id=None):
    """Legacy boolean duplicate guard, including queued on-chain retries."""
    return _other_lane_attempt_state(conn, signature, detected_at, lookback_s=lookback_s,
                                     source_user_id=source_user_id) in (
        "attempted", "bsc_native_pending", "rh_lane_pending")


# A NATIVE FAST LANE THAT LET GO UNSIGNED (2026-10-14, Alpha; the Base lane's first live minutes):
# a pre-send abort (lock busy, float short, nonce in flight, a stale snapshot, post_peak, a refused
# copy floor, a retracted source...) releases its reservation and spent nothing, so it is never the
# decision for the buy: the ordinary lane judges the same transaction. Narrow, like 56's float_short:
# the lane's own attempt, terminal 'aborted', no signature, submission or send mark on it, and its
# pending row released ('failed') with the lane's own release reason and every nonce, signature and
# submission column NULL. Anything that may have reached a node stays "attempted".
_UNSIGNED_LANE_ABORT = """(signal_source IN ('rh_fast_lane', 'bsc_fast_lane')
    AND terminal_status='aborted' AND COALESCE(tx_sig, '')=''
    AND position_id IS NULL AND fill_id IS NULL
    AND signing_completed_at IS NULL AND signing_ms IS NULL
    AND transaction_submitted_at IS NULL AND transaction_submitted_ms IS NULL
    AND CASE WHEN json_valid(trace_json) THEN json_type(trace_json)='object'
        AND json_type(trace_json,'$.submission_events')='array'
        AND json_array_length(trace_json,'$.submission_events')=0 ELSE 0 END
    AND COALESCE(trace_json, '') NOT LIKE '%rh_fast_lane_send%'
    AND COALESCE(trace_json, '') NOT LIKE '%bsc_fast_lane_send%'
    AND EXISTS (SELECT 1 FROM memecoin_pending_entries p
        WHERE p.id=memecoin_entry_attempts.pending_id
        AND p.attempt_id=memecoin_entry_attempts.attempt_id
        AND p.network_id=memecoin_entry_attempts.network_id
        AND LOWER(p.mint)=LOWER(memecoin_entry_attempts.mint)
        AND p.source_user_id=memecoin_entry_attempts.source_user_id
        AND p.origin_event_id=memecoin_entry_attempts.origin_event_id
        AND p.signal_source=memecoin_entry_attempts.signal_source
        AND p.status='failed'
        AND (substr(p.resolution_reason, 1, 8)='rh_fast_'
            OR substr(p.resolution_reason, 1, 10)='base_fast_'
            OR substr(p.resolution_reason, 1, 9)='bsc_fast_')
        AND p.tx_sig IS NULL AND p.broadcast_at IS NULL AND p.nonce IS NULL
        AND p.signed_payload_b64 IS NULL AND p.signed_payload_sha256 IS NULL
        AND p.submission_blockhash IS NULL AND p.submission_last_valid_height IS NULL
        AND p.submission_backend IS NULL AND p.submission_bound_at IS NULL
        AND p.cleanup_tx_sig IS NULL AND p.cleanup_nonce IS NULL
        AND p.cleanup_amount_atomic IS NULL AND p.cleanup_status IS NULL
        AND p.cleanup_signed_at IS NULL AND p.cleanup_signed_payload_sha256 IS NULL))"""


# A NATIVE LANE ATTEMPT STILL IN FLIGHT AND UNSIGNED (Gamma BLOCK on d59e3d2c2): its terminal
# is not written yet, nothing on it or on its reservation was signed, bound or sent. The ordinary
# lane waits on this ROW STATE (56's bsc_native_pending shape), never on the 1 s hold alone: the
# lane can still be pre-send after the hold expires. Anything bound or sent stays "attempted".
_RH_LANE_IN_FLIGHT = """(signal_source='rh_fast_lane'
    AND terminal_status IS NULL AND terminal_reason IS NULL AND completed_at IS NULL
    AND COALESCE(tx_sig, '')='' AND position_id IS NULL AND fill_id IS NULL
    AND signing_completed_at IS NULL AND signing_ms IS NULL
    AND transaction_submitted_at IS NULL AND transaction_submitted_ms IS NULL
    AND (trace_json IS NULL OR CASE WHEN json_valid(trace_json)
        THEN json_type(trace_json)='object'
        AND (json_type(trace_json,'$.submission_events') IS NULL
            OR (json_type(trace_json,'$.submission_events')='array'
                AND json_array_length(trace_json,'$.submission_events')=0)) ELSE 0 END)
    AND COALESCE(trace_json, '') NOT LIKE '%rh_fast_lane_send%'
    AND NOT EXISTS (SELECT 1 FROM memecoin_pending_entries p
        WHERE p.attempt_id=memecoin_entry_attempts.attempt_id
        AND (p.tx_sig IS NOT NULL OR p.broadcast_at IS NOT NULL OR p.nonce IS NOT NULL
            OR p.signed_payload_b64 IS NOT NULL OR p.signed_payload_sha256 IS NOT NULL
            OR p.submission_backend IS NOT NULL OR p.submission_bound_at IS NOT NULL)))"""


def _other_lane_attempt_state(conn, signature, detected_at, *, lookback_s=120.0,
                              source_user_id=None, include_onchain=False):
    """Return attempted, bsc_native_pending, rh_lane_pending, lane_released_unsigned, or None.

    FCAT 04253d70 (2026-09-23 20:36Z): J7 delivered probablyinsider's buy 0.24 s after the chain
    and the hook aborted it on the round-trip cap; this lane delivered the SAME signature 0.7 s
    later, hit the reservation the first attempt still held, and paged "one position per coin:
    already held or reserved" -- a second card with the wrong reason for a coin that was refused
    on cost. 110 such J7-first pairs in the week to 2026-09-23 against 28 where this lane was
    first (1 fill). Both origin ids carry the signature verbatim (J7: "swap:<venue>:<sig>:...",
    here "onchain:<sig>"), so the join is the signature itself.

    Bounded by `decision_started_at >= detected_at - lookback_s` so the index on
    decision_started_at takes it (0.02 ms p50 on 54,179 rows, measured 2026-09-23); the
    unbounded LIKE was 2.8 ms. An unreadable table answers None: this lane then behaves exactly
    as before, one duplicate card rather than a missed entry.
    """
    if not signature:
        return None
    ts = _parse_iso(detected_at)
    if ts is None:
        return None
    since = (ts - timedelta(seconds=float(lookback_s))).isoformat()
    extra, index, args, state_rank, order = "", "", [since], "0", ""
    pending_state = "bsc_native_pending"
    select_args = []
    sources = "signal_source NOT IN ('onchain_watch', 'slot_lane')"
    signature = str(signature)
    if ":" in signature:
        # EVM watcher keys are chain:tx:mint; feed origins carry only the raw tx.
        # Scope the new match to that chain/coin/wallet, never a substring on another asset.
        match = re.fullmatch(r"([0-9]+):(0x[0-9a-fA-F]{64}):(0x[0-9a-fA-F]{40})", signature)
        if match is None:
            return None
        chain, transaction, mint = match.groups()
        # Case-insensitive mint matching cannot use the coin index's second column.
        # Keep the existing bounded-time index instead of scanning the chain's whole history.
        index = " INDEXED BY idx_mc_entry_view_started"
        extra = " AND network_id=? AND LOWER(mint)=?"
        args.extend((chain, mint.lower()))
        if source_user_id is not None:
            extra += " AND source_user_id=?"
            args.append(source_user_id)
        pattern = "%:" + transaction + ":%"
        if chain == "56" and source_user_id is not None:
            # An incomplete native trace only holds its detection. Its pending_id is attached at
            # finish, so requiring that link now consumes buys between begin/reserve/finish.
            # Only a terminal unsigned shortage permits entry, with its reservation proved by PK.
            # Other decisions rank first, pending owners second, and unsigned refusals last.
            state_rank = """CASE WHEN signal_source='bsc_fast_lane'
                AND delivery_lane='bsc_fast_lane' AND network_id='56'
                AND origin_event_id=? AND tx_sig IS NULL
                AND position_id IS NULL AND fill_id IS NULL
                AND signing_completed_at IS NULL AND signing_ms IS NULL
                AND transaction_submitted_at IS NULL AND transaction_submitted_ms IS NULL
                AND (trace_json IS NULL OR CASE WHEN json_valid(trace_json)
                    THEN json_type(trace_json)='object'
                    AND (json_type(trace_json,'$.submission_events') IS NULL
                        OR (json_type(trace_json,'$.submission_events')='array'
                            AND json_array_length(trace_json,'$.submission_events')=0)) ELSE 0 END)
                AND COALESCE(trace_json, '') NOT LIKE '%rh_fast_lane_send%'
                AND COALESCE(trace_json, '') NOT LIKE '%bsc_fast_lane_send%'
                THEN CASE WHEN terminal_status IS NULL AND terminal_reason IS NULL
                    AND completed_at IS NULL THEN 1
                WHEN """ + _UNSIGNED_LANE_ABORT + """
                THEN 2 ELSE 0 END ELSE 0 END"""
            select_args = ["bscfast:" + signature]
            order = " ORDER BY other_lane_rank ASC"
            if include_onchain:
                # A completed handoff remains authoritative after the in-memory claim expires.
                sources = ("(signal_source NOT IN ('onchain_watch', 'slot_lane') OR "
                           "(signal_source='onchain_watch' AND origin_event_id=?))")
                args.insert(1, "onchain:" + signature)
        elif source_user_id is not None:
            # 4663 and 8453: the native lane's attempt in flight ranks pending (wait), its unsigned
            # release ranks last (not the decision); every other decision ranks first.
            state_rank = ("CASE WHEN " + _RH_LANE_IN_FLIGHT + " THEN 1 WHEN " + _UNSIGNED_LANE_ABORT
                          + " THEN 2 ELSE 0 END")
            order = " ORDER BY other_lane_rank ASC"
            pending_state = "rh_lane_pending"
    else:
        pattern = "%" + signature + "%"
    args.append(pattern)
    from sniper.settings import memecoin as cfg
    j7_nonowning = ""
    # An unsigned J7 refusal that deferred to the native lane (j7_native_*) is not a decision either:
    # the slot lane's own outcome decides (DexGemsReal 3kjkcUA5, 2026-10-11 21:09Z, lost otherwise).
    # Missing-receipt traces persist as 'refused', so match that exact reason too.
    if ':' not in signature and cfg.MEMECOIN_J7_SOLANA_VERIFIED_BACKUP:
        j7_nonowning = """ AND NOT (COALESCE(delivery_lane,'') IN ('j7_wallets','j7_pump') AND network_id='solana'
            AND terminal_status IS NOT NULL
            AND (COALESCE(terminal_reason,'')='native_fast_lane_unavailable'
                 OR COALESCE(terminal_status,'')='source_initiation_unknown'
                 OR COALESCE(terminal_reason,'')='j7_receipt_not_observed'
                 OR COALESCE(terminal_reason,'') LIKE 'j7_native_%')
            AND tx_sig IS NULL AND position_id IS NULL AND fill_id IS NULL
            AND signing_completed_at IS NULL AND signing_ms IS NULL
            AND transaction_submitted_at IS NULL AND transaction_submitted_ms IS NULL
            AND (trace_json IS NULL OR CASE WHEN json_valid(trace_json)
                THEN json_type(trace_json)='object'
                AND (json_type(trace_json,'$.submission_events') IS NULL
                    OR (json_type(trace_json,'$.submission_events')='array'
                        AND json_array_length(trace_json,'$.submission_events')=0)) ELSE 0 END)
            AND NOT EXISTS (SELECT 1 FROM memecoin_pending_entries p
                WHERE p.attempt_id=memecoin_entry_attempts.attempt_id))"""
    try:
        row = conn.execute(
            "SELECT " + state_rank + " AS other_lane_rank FROM memecoin_entry_attempts" + index +
            " WHERE decision_started_at >= ? AND " + sources +
            # A J7 SHADOW decision spent nothing (MEMECOIN_J7_SOLANA_MODE): the fast lane still owns it.
            " AND COALESCE(delivery_lane, '') != 'j7_shadow'"
            # A 4663 deposit-proved row (memecoin_rh_deposit_proof.DEPOSIT_DELIVERY_LANE) that
            # finished without a send was refused on ESTIMATED economics: this lane still judges
            # the buy on its receipt (Gamma LOW-1, Alpha 2026-10-07). In flight or sent, it counts,
            # and so does a row whose reservation bound a submission: a crash after the broadcast
            # leaves no tx_sig and no send mark, but the bound identity says bytes may be out.
            " AND NOT (COALESCE(delivery_lane, '') = 'rh_fast_lane_deposit'"
            " AND terminal_status IS NOT NULL AND COALESCE(tx_sig, '') = ''"
            " AND COALESCE(trace_json, '') NOT LIKE '%rh_fast_lane_send%'"
            " AND NOT EXISTS (SELECT 1 FROM memecoin_pending_entries p"
            " WHERE p.attempt_id = memecoin_entry_attempts.attempt_id"
            " AND p.submission_bound_at IS NOT NULL))" + extra +
            " AND origin_event_id LIKE ?" + j7_nonowning + order + " LIMIT 1",
            select_args + args).fetchone()
    except Exception:  # noqa: BLE001 - a missing or locked table must not stop the lane
        logger.debug("onchain lane: duplicate check unreadable for %s", signature, exc_info=True)
        return None
    if row is None:
        return None
    return ("attempted", pending_state, "lane_released_unsigned")[row[0]]


MAX_SLOT_DEFERRED = 64
FALLBACK_LANE = "onchain_fallback_after_slot_lane"
# A source frame with two own buy events (decode_owner_buy's `multi_own_buy:<venues>`): the slot lane
# rejects it in _handle before any read, write or send, so it never sent (Alpha GO 2026-10-14, Gamma's
# current-code losses: 7 of 14, six a bigwarzeth Xsc9-curve burst). The ordinary path judges it once.
SHAPE_UNSUPPORTED_CLASS = "slot_shape_unsupported"
MULTI_OWN_BUY_PREFIX = "multi_own_buy:"

# THE RESTART BLACKOUT (Alpha GO 2026-10-07; operator-flagged loss EELDREW, ResellCalendar, 67b8832d).
# A Solana buy that lands while the runner is down never reaches the slot lane, so fast-lane-only
# refused its queued detection on the next start. Such a detection gets the fallback class `lane_down`
# (admitted through MEMECOIN_SOLANA_FALLBACK_CLASSES) when its arrival falls between the previous
# runner's last heartbeat and the moment the new slot lane subscribed to THAT owner (its own
# logsSubscribe ack: an owner acked earlier is already traded, Gamma MED), capped at LANE_DOWN_MAX_S
# after this process started. The watcher's detected_at follows the processed frame by at most 2.65 s (n=1089,
# 2026-10-07), so the arrival must sit LANE_DOWN_ARRIVAL_SLACK_S past the heartbeat. Host epoch seconds
# throughout. Restart only: a mid-run socket reconnect is not covered.
LANE_DOWN_CLASS = "lane_down"
LANE_DOWN_LANE = "onchain_lane_down"
LANE_DOWN_ARRIVAL_SLACK_S = 3.0
LANE_DOWN_MAX_S = 120.0


def _iso_epoch(value):
    ts = _parse_iso(value)
    return ts.timestamp() if ts is not None and ts.tzinfo is not None else None


def _epoch_iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class LaneDownWindow:
    """One process's restart blackout. `open` runs once at startup, before any lane thread; the slot
    signal thread records each owner's first ack (`owner_acked`) and logs when all are answered
    (`slot_lane_ready`); the on-chain workers read `covers`. Floats only, one writer each."""

    def __init__(self):
        self.start = self.process_start = self.ready = None
        self.acks = {}                    # owner address -> host epoch of its first logsSubscribe ack

    def open(self, heartbeat, *, started_at, now):
        """Set START from the previous runner's heartbeat. Returns the startup log line."""
        self.process_start = float(now)
        beat = heartbeat if isinstance(heartbeat, dict) else {}
        ts = _finite_number(beat.get("ts"))
        why = ("no heartbeat" if not beat
               else "heartbeat is this process's own" if beat.get("started_at") == started_at
               else "heartbeat has no usable ts" if ts is None or ts > self.process_start
               else None)
        if why is not None:
            return "[memecoin] lane_down window: none (%s)" % why
        self.start = ts
        return ("[memecoin] lane_down window: START=%s (previous runner %s, last heartbeat) END=slot-lane "
                "ready, cap=%s" % (_epoch_iso(ts), beat.get("started_at"),
                                   _epoch_iso(self.process_start + LANE_DOWN_MAX_S)))

    def owner_acked(self, owner, now):
        """This owner's END: the slot lane trades its frames from here on. First ack only."""
        self.acks.setdefault(owner, float(now))

    def slot_lane_ready(self, now, owners=None):
        """Every owner's request answered: the log line naming the window's last END, first time only."""
        if self.ready is not None:
            return None
        self.ready = float(now)
        first = min(self.acks.values(), default=None)
        return "[memecoin] lane_down window: END=%s (every owner answered, owners=%s, first ack %s)" % (
            _epoch_iso(self.ready), owners, _epoch_iso(first) if first is not None else None)

    def covers(self, row):
        """Whether this Solana detection's arrival provably fell inside the blackout for its owner."""
        def get(key):
            try:
                return row[key]
            except (KeyError, IndexError):
                return None
        if self.start is None or str(get("network_id") or "solana") != "solana":
            return False
        seen = _iso_epoch(get("detected_at"))
        if seen is None:
            return False
        end = self.process_start + LANE_DOWN_MAX_S
        acked = self.acks.get(get("address"))
        if acked is not None:
            end = min(end, acked)          # detected_at follows the frame: before the ack, unseen
        return seen - LANE_DOWN_ARRIVAL_SLACK_S > self.start and seen < end


LANE_DOWN = LaneDownWindow()
PUMP_GRADUATION_REASON = "chain_error:BondingCurveComplete"
_SLOT_FUNDED_OWNER_SQL = ("(p.status IN ('reserved','confirmed') OR "
    "(a.tx_sig IS NOT NULL AND COALESCE(p.status,'') != 'failed'))")


def slot_funded_owner(conn, signature):
    """Any exact-origin durable native owner, regardless of age. An unreadable owner stays held."""
    try:
        return conn.execute(
            "SELECT 1 FROM memecoin_entry_attempts a LEFT JOIN memecoin_pending_entries p "
            "ON p.attempt_id=a.attempt_id WHERE a.signal_source='slot_lane' "
            "AND a.origin_event_id=? AND " + _SLOT_FUNDED_OWNER_SQL + " LIMIT 1",
            ("slotlane:" + str(signature),)).fetchone() is not None
    except Exception:
        logger.warning("native receipt owner unreadable sig=%s", signature, exc_info=True)
        return True


def slot_lane_outcome(conn, signature, detected_at, *, lookback_s=120.0):
    """What the slot lane did with this very transaction: slot_lane_verdict's outcome alone."""
    return slot_lane_verdict(conn, signature, detected_at, lookback_s=lookback_s)[0]


def slot_lane_verdict(conn, signature, detected_at, *, lookback_s=120.0):
    """(outcome, fallback_class) for this very transaction, from committed rows and the lane's
    in-process registries.

    outcome:
    None        no slot attempt: the ordinary path, as before this existed.
    in_flight   an attempt with no conclusive result: a reservation still `reserved` (possibly a
                signed, bound, maybe-landing send), an attempt row with no terminal status, or a
                CONFIRMED_PENDING / EXOTIC_PENDING hold.
    filled      its reservation confirmed: the transaction is copied; never fall back.
    failed      its reservation released, which release_pending does only on a CONFIRMED failure:
                landed and failed, never broadcast (build or pre-sign refusal), rejected by every
                leg, or proven expired. Also an exotic hold released with no attempt, a regime-scaled
                clip (slot_lane.SCALED_CLIP_REASON), a trial wallet (memecoin_trial.TRIAL_UNMARKED)
                and a missing source receipt: nothing was held for those.
    graduated   the released reservation specifically reports Pump's BondingCurveComplete;
                the ordinary fallback may re-quote a missing replacement route promptly. Also a
                graduation pool watch released with no attempt row (`graduation_released`).
    decided     refused before reserving, or a confirmed native minimum-out failure.
    unreadable  the slot state could not be read: a refusal, never `None` (operator order
                2026-10-06: an unreadable state must never look like "no slot attempt").

    fallback_class (MEMECOIN_SOLANA_FALLBACK_CLASSES decides which admit, fast-lane-only on): the
    slot lane failed MECHANICALLY. `landed_fail` (a signed send landed and failed on chain),
    `build_refused` (refused before signing), `exotic_released` (an exotic leg expired or was
    released unsent), `routed_handoff` (the live lane handed a routed buy off for having no live
    venue), `slot_shape_unsupported` (a multi_own_buy frame, rejected unsent). None otherwise: its
    policy, sizing and proof refusals (a worker policy refusal of an exotic leg included, 5a), and
    plain absence, never admit.

    Solana only: the slot lane never runs on an EVM chain:tx:mint key.
    """
    if not signature or ":" in str(signature):
        return None, None
    ts = _parse_iso(detected_at)
    if ts is None:
        return None, None
    from sniper.services import memecoin_slot_lane as lane
    sig, now = str(signature), time.monotonic()
    if lane.CONFIRMED_PENDING.holds(sig, now=now) or lane.GRADUATION_PENDING.holds(sig, now=now):
        # A graduation watch (memecoin_slot_graduation) is reading this coin's canonical PumpSwap
        # pool, or has just handed the buy back to the lane: one path at a time, bounded by its hold.
        return "in_flight", None
    exotic_attempt_held, exotic_release = lane.EXOTIC_PENDING.attempt_state(
        sig, now=now, requote_enabled=lane.exotic_requote_mode() != "off")
    if exotic_attempt_held:
        # An exotic RE-QUOTE (memecoin_slot_lane._requote_over_packet) is out on the leg worker
        # AFTER an attempt row exists (its build was over the packet): defer to its hold as to a
        # first leg's. An unsigned graduation cash repair has the same ownership requirement,
        # independent of the optional packet-requote knob.
        return "in_flight", None
    since = (ts - timedelta(seconds=float(lookback_s))).isoformat()
    try:
        row = conn.execute(
            "SELECT a.terminal_status, p.status, COALESCE(p.resolution_reason, a.terminal_reason),"
            " COALESCE(p.tx_sig,a.tx_sig), " + _SLOT_FUNDED_OWNER_SQL + " FROM memecoin_entry_attempts a"
            " LEFT JOIN memecoin_pending_entries p ON p.attempt_id = a.attempt_id"
            " WHERE (a.decision_started_at >= ? OR " + _SLOT_FUNDED_OWNER_SQL + ")"
            " AND a.signal_source = 'slot_lane' AND a.origin_event_id = ? "
            "ORDER BY CASE WHEN p.status='confirmed' THEN 0 WHEN " + _SLOT_FUNDED_OWNER_SQL +
            " THEN 1 ELSE 2 END, a.decision_started_at DESC LIMIT 1",
            (since, "slotlane:" + sig)).fetchone()
    except Exception:  # noqa: BLE001 - unreadable is a refusal, never "no attempt"
        logger.warning("onchain lane: slot state unreadable for %s", sig, exc_info=True)
        return "unreadable", None
    from sniper.services.memecoin_slot_skips import peek_decision_for
    decided = peek_decision_for(sig)
    volume_refused = decided == ("rejected", lane.VOLUME_REFUSAL)
    if (volume_refused and row is not None and not row[4] and row[1] != "confirmed"
            and (row[1] == "failed" or row[0] is not None)):
        # A later volume refusal supersedes an older mechanical failure, even after its cache
        # expires. A funded owner or unresolved attempt remains authoritative.
        return "decided", None
    if row is None:
        # An exotic-quote leg still out on the slot lane's worker (Gamma M2, 2026-10-02): no attempt
        # row exists until the lane reserves, so the in-process registry answers. Bounded by the
        # buy's signal age; the lane wakes this lane when it lets go.
        if lane.EXOTIC_PENDING.holds(sig, now=now):
            return "in_flight", None
        if volume_refused:
            return "decided", None
        released = lane.EXOTIC_PENDING.release_kind(sig, now)
        if released is not None:
            # The hold ended with no attempt row (Gamma MED-1): one ordinary attempt is safe. A worker
            # POLICY refusal (5a, 2026-10-07) has no fallback class: fast-lane-only refuses it.
            return "failed", (None if released == "policy" else "exotic_released")
        if lane.ROUTED_HANDOFFS.released(sig, now=now):
            return None, "routed_handoff"
        graduation = lane.GRADUATION_PENDING.release_kind(sig, now)
        if graduation is not None:
            # The source completed the curve and the pool watch ended with no attempt row (no pool by
            # the deadline, or a refusal): today's graduation fallback, once. A policy release: none.
            return "graduated", (None if graduation == "policy" else "graduation_released")
        if decided and decided[0] == "rejected" and str(decided[1] or "").startswith(MULTI_OWN_BUY_PREFIX):
            # The lane's latest word on this signature, so a later attempt or hand-off replaces it.
            return "failed", SHAPE_UNSUPPORTED_CLASS
        return None, None
    terminal, pending, reason, tx_sig, funded_owner = row[0], row[1], row[2], row[3], row[4]
    reason = str(reason or "")
    if pending is None and reason == lane.EXOTIC_PENDING_REASON:
        if lane.EXOTIC_PENDING.holds(sig, now=now):
            return "in_flight", None
        return "failed", (None if lane.EXOTIC_PENDING.release_kind(sig, now) == "policy"
                          else "exotic_released")
    if pending == "confirmed":
        return "filled", None
    if pending == "failed":
        # A price refusal must not become a fresh-price buy through the ordinary/J7 backup.
        if reason in SLIPPAGE_FAIL_REASONS:
            return "decided", None
        if not tx_sig and lane.policy_refusal(reason):
            return "decided", None
        if (not funded_owner and not tx_sig and reason.startswith("slot_lane_build")
                and exotic_release == "policy"):
            # A worker or re-entry gate refused the unsigned repair after this older build failed.
            # Its policy is authoritative; the old mechanical failure cannot admit a J7 backup.
            return "decided", None
        klass = ("landed_fail" if tx_sig and reason.startswith("chain_error:") else
                 "build_refused" if not tx_sig and reason.startswith("slot_lane_build") else None)
        return ("graduated" if reason == PUMP_GRADUATION_REASON else "failed"), klass
    if funded_owner:
        return "in_flight", None
    from sniper.services.memecoin_trial import TRIAL_UNMARKED
    # A trial wallet (spec 2026-10-01): the lane cannot carry the trial clip and marker, so the
    # reservation refused it with nothing held. Same as a scaled clip: one ordinary attempt when
    # fast-lane-only is off; a policy refusal, so no fallback class when it is on.
    if pending is None and (terminal == "source_initiation_unknown"
                            or reason == lane.SCALED_CLIP_REASON
                            or reason.startswith(TRIAL_UNMARKED)):
        return "failed", None
    if pending == "reserved" or terminal is None:
        return "in_flight", None
    return "decided", None


# One page per slot-lane outage, not one per signal (operator order 2026-10-06). The Solana
# on-chain worker is the only caller; the lock keeps the transition single anyway.
_OUTAGE = {"down": False}
_OUTAGE_LOCK = threading.Lock()


def _page_slot_lane_outage(detail):
    try:
        from sniper.services.memecoin_alerts import send_alert
        send_alert("SLOT-LANE UNREADABLE",
                   "Solana on-chain fallbacks refused until its state reads again: %s" % detail,
                   icon="🔴")
    except Exception:  # noqa: BLE001 - a page must never stop the lane
        logger.exception("slot lane outage page failed")


def _note_slot_state(readable, signature):
    with _OUTAGE_LOCK:
        was, _OUTAGE["down"] = _OUTAGE["down"], not readable
    if not readable and not was:
        _page_slot_lane_outage("first refused sig=%s" % signature)
    elif readable and was:
        logger.warning("onchain lane: slot state readable again (sig=%s)", signature)


def _fallback_already_attempted(conn, signature, detected_at, *, lookback_s=120.0):
    """True when this lane already sent this signature through the hook (the ONE fallback)."""
    ts = _parse_iso(detected_at)
    since = (ts - timedelta(seconds=float(lookback_s))).isoformat() if ts else ""
    try:
        return conn.execute(
            "SELECT 1 FROM memecoin_entry_attempts WHERE decision_started_at >= ?"
            " AND signal_source = 'onchain_watch' AND origin_event_id = ? LIMIT 1",
            (since, "onchain:" + str(signature))).fetchone() is not None
    except Exception:  # noqa: BLE001 - unreadable: refuse the fallback rather than risk a second
        logger.debug("onchain lane: fallback guard unreadable for %s", signature, exc_info=True)
        return True


def startup_cursor(conn, *, now_fn, max_age_s, network_id=None):
    """Cursor before the oldest fresh detection in the bounded network tail.

    A fresh row written seconds before a runner restart still fires; anything older is a
    missed pop and must never fire on a reboot.
    """
    now = _parse_iso(now_fn())
    where, args = (" WHERE network_id=?", (str(network_id),)) if network_id is not None else ("", ())
    row = conn.execute(
        "SELECT id, detected_at FROM onchain_wallet_detections" + where + " ORDER BY id DESC LIMIT 50", args
    ).fetchall()
    # A retry can append an older observation after a fresh one. The greatest stale id
    # would skip that fresh row; begin before the oldest fresh row in the bounded tail.
    fresh_ids = []
    for r in row:
        ts = _parse_iso(r["detected_at"])
        if ts is not None and now is not None and (now - ts).total_seconds() <= max_age_s:
            fresh_ids.append(r["id"])
    if fresh_ids:
        return min(fresh_ids) - 1
    return max((r["id"] for r in row), default=0)


def _run_hook(conn, real_hook, row, *, fallback=False, graduated=False, slot_class=None):
    ev = OnchainEv(row)
    if str(ev.network_id) in ('4663', '8453'):
        hold = row.get('_feed_hold') if isinstance(row, dict) else None
        ev.pre_trace_substages = [('native_lane_hold',
                                   hold or {'hold_ms': 0.0, 'hold_outcome': 'no_hold'})]
    from sniper.settings import memecoin as cfg
    if fallback:
        ev.delivery_lane = FALLBACK_LANE     # recorded on the attempt row: fallback fills split out
        if graduated:
            ev.slot_lane_failure_reason = PUMP_GRADUATION_REASON
    # Publish proof-first arrivals when native precedence applies, including verified J7 backups.
    if ev.network_id == 'solana' and (fallback or cfg.MEMECOIN_SOLANA_FAST_LANE_ONLY
                                    or cfg.MEMECOIN_J7_SOLANA_VERIFIED_BACKUP):
        resume = getattr(real_hook, 'receipt_resume', None)
        if callable(resume):
            try:
                if resume(ev):
                    return {'status': 'deferred', 'reason': 'slot_lane_receipt_resume'}
            except Exception:
                logger.exception('native receipt resume failed sig=%s', row['signature'])
    if ev.network_id == 'solana':
        ev.slot_fallback_class = slot_class          # stamped on the attempt; rides retries
        if slot_class == LANE_DOWN_CLASS:
            ev.delivery_lane = LANE_DOWN_LANE        # graded apart from slot and fallback fills
    if ev.network_id == 'solana' and cfg.MEMECOIN_SOLANA_FAST_LANE_ONLY:
        # Only a MECHANICAL slot-lane failure (slot_lane_verdict's class, read once by _route)
        # warrants an ordinary attempt. Its policy and proof refusals, an ambiguous send and plain
        # absence never become a late automatic purchase; the runner refuses them after the roster
        # and mcap gates so the refusal carries the real reason first.
        ev.native_failure_fallback = slot_class in cfg.MEMECOIN_SOLANA_FALLBACK_CLASSES
    if (ev.network_id == 'solana' and cfg.MEMECOIN_J7_SOLANA_VERIFIED_BACKUP):
        from sniper.services.memecoin_j7_backup import has_exact_hint
        from sniper.services.memecoin_slot_skips import decision_for
        from sniper.services.memecoin_source_initiation import DIRECT_PROOF_VERSION, source_initiation_refusal
        # A slot-lane REJECTION of this source buy is the decision (fast-lane-only, operator
        # 2026-10-06): the backup must not re-decide it on another price reference (73834/73822,
        # 2026-10-14). Mechanical fallback classes keep their own path via native_failure_fallback.
        # ponytail: in-memory only; a restart between the rejection and the confirmed receipt
        # (~0.1-0.5 s) forgets it. Read memecoin_slot_lane_skips if that window ever matters.
        rejected = (decision_for(str(row['signature'])) or (None,))[0] == 'rejected'
        if (not rejected
                and (cfg.MEMECOIN_SOLANA_INFRA_PROVIDER == "shyft" or has_exact_hint(conn, ev))
                and isinstance(ev.source_initiation, dict)
                and ev.source_initiation.get('proof_version') == DIRECT_PROOF_VERSION
                and source_initiation_refusal(ev, 'solana') is None):
            ev.j7_verified_backup = True
            ev.delivery_lane = 'j7_verified_backup'
    try:
        result = real_hook(conn, ev)
    except Exception as exc:  # noqa: BLE001 - one bad row must not stall the lane
        logger.exception("onchain lane hook failed sig=%s mint=%s", row["signature"], row["mint"])
        return {"status": "error", "reason": str(exc)[:200]}
    _note_prebuy_source(row, result)
    _note_policy_verdict(row, result)
    return result


# THE ON-CHAIN LANE'S POLICY VERDICT IS FINAL FOR THE 4663 FAST LANE'S LATER SHARED SEND (Gamma LOW
# on 59bdefa72, Alpha 2026-10-14). A deposit-proved shared route now waits for its receipt, so this
# lane judges the source buy first; the fast lane must not re-judge it ~2 s later on a fresher quote.
# The same in-memory registry as 56's (policy_decision_for), keyed by the exact source tx. Mechanical
# and transient refusals (no quote, route capacity, a timeout, a pending attempt) are not decisions.
# No source_user_id: a 4663 note never matches 56's tx-less wallet-scope lookup.
ONCHAIN_POLICY_FINAL = ("hook:skipped_mcap:", "hook:skipped_chase:", "hook:rejected:source buy ",
                        "hook:rejected:copy price minimum is ", "hook:rejected:re-entry cooldown",
                        "hook:rejected:one position per coin: open funded position")


def _note_policy_verdict(row, result):
    if str(row["network_id"]) != "4663":
        return
    try:
        from sniper.services.memecoin_bsc_fast_lane import note_policy_decision
        from sniper.services.memecoin_rh_fast_lane import hook_reason
        _chain, _, rest = str(row["signature"]).partition(":")
        note_policy_decision(rest.partition(":")[0], row["mint"], None, hook_reason(result),
                             final=ONCHAIN_POLICY_FINAL)
    except Exception:  # noqa: BLE001 - a note never stops the lane
        logger.exception("onchain lane: policy verdict note failed")


def _note_prebuy_source(row, result):
    """This lane's verdict on a 4663 source buy, for the Relay pre-buy's source-fill counters."""
    from sniper.settings import memecoin as cfg
    chain = str(row["network_id"])
    if not (cfg.MEMECOIN_RH_RELAY_PREBUY and chain == "4663" or cfg.MEMECOIN_BSC_RELAY_PREBUY and chain == "56"):
        return
    try:
        from sniper.services.memecoin_rh_relay_prebuy import note_source_fill
        _chain, _, rest = str(row["signature"]).partition(":")
        note_source_fill(chain, row["mint"], row["source_user_id"], rest.partition(":")[0], "onchain_watch",
                         (result or {}).get("reason"))
    except Exception:  # noqa: BLE001 - a counter never stops the lane
        logger.exception("onchain lane: relay prebuy source note failed")


# A 4663 RELAY PRE-BUY STILL DECIDING (Alpha 2026-10-14, on Gamma's MEDIUM 2): its trace row is written and
# its terminal is not. While it is, the source's own buy of that coin waits here (Beta's rh_lane_pending
# shape: by row state, within the detection's own freshness), then is judged as today: one position per coin
# refuses it only when the pre-buy holds or may hold the coin. Its origin carries the Relay order id, never
# the source transaction, so the per-transaction lookup above never sees it.
def relay_prebuy_in_flight(conn, mint, detected_at, *, network_id="4663", lookback_s=120.0):
    """True while a relay_prebuy attempt of this coin on `network_id` (4663 or 56) has no terminal.
    Unreadable: False (judged as today)."""
    ts = _parse_iso(detected_at)
    if ts is None:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM memecoin_entry_attempts INDEXED BY idx_mc_entry_view_started "
            "WHERE decision_started_at >= ? AND signal_source='relay_prebuy' AND network_id=? "
            "AND LOWER(mint)=? AND terminal_status IS NULL AND completed_at IS NULL LIMIT 1",
            ((ts - timedelta(seconds=float(lookback_s))).isoformat(), str(network_id),
             str(mint).lower())).fetchone() is not None
    except Exception:  # noqa: BLE001 - a locked table must not stop the lane
        logger.debug("onchain lane: relay prebuy lookup unreadable for %s", mint, exc_info=True)
        return False


def _route(conn, real_hook, row, deferred, *, now=None):
    """The result for one fresh, armed detection, or None while it stays deferred."""
    key = (row["signature"], row["source_user_id"], str(row["network_id"] or "solana"),
           row["mint"])
    other_lane = _other_lane_attempt_state(conn, row["signature"], row["detected_at"],
                                           source_user_id=row["source_user_id"],
                                           include_onchain=str(row["network_id"]) == "56")
    if other_lane == "attempted":
        # Counted, never paged: the feed lane's attempt IS the decision for this transaction.
        deferred.pop(key, None)
        return {"status": "skipped", "reason": "duplicate_of_feed"}
    if other_lane == "rh_lane_pending":
        # The native lane (4663, 8453) is still deciding this buy unsigned: wait for its verdict or
        # its unsigned release, by row state, within the row's own freshness bound.
        if key in deferred:
            return None
        if len(deferred) >= MAX_SLOT_DEFERRED:
            return {'status': 'skipped', 'reason': 'rh_fast_lane_handoff_capacity'}
        deferred[key] = dict(row, _held_for='rh_fast_lane', _hold_t0=time.monotonic())
        return {'status': 'deferred', 'reason': 'rh_fast_lane_in_flight'}
    if str(row['network_id']) in ('4663', '8453'):
        # THE NATIVE FAST LANE DECIDES FIRST (4663; Base 8453 shares the lane, plan 2026-10-14).
        # THE 4663 FAST LANE DECIDES FIRST (2026-10-14, misses C, 73809). While the live rh lane
        # holds this exact (tx, mint), its verdict is pending: wait for it (bounded by the lane's
        # hold, about 1 s) instead of racing it on another price reference. Its attempt row then
        # skips this one above; a refused deposit-lane row is re-judged here as before.
        from sniper.services.memecoin_rh_fast_lane import LANE_HOLDS
        _chain, _, rest = str(row['signature']).partition(':')
        tx, _, mint = rest.partition(':')
        if LANE_HOLDS.holds(tx, mint):
            if key in deferred:
                return None
            if len(deferred) < MAX_SLOT_DEFERRED:   # full: today's behaviour, no wait
                deferred[key] = dict(row, _held_for='rh_fast_lane', _hold_t0=time.monotonic(),
                                     _hold_kind='hold')
                return {'status': 'deferred', 'reason': 'rh_fast_lane_in_flight'}
        elif (deferred.get(key) or {}).get('_held_for') == 'rh_fast_lane':
            held = deferred.pop(key, None)
            # PER-ATTEMPT HOLD RECORD (2026-10-17, Alpha): the wait and how it ended ride this row
            # to the attempt trace (native_lane_hold), so "decision minus hold" reads per attempt.
            # A wait on the lane's in-flight ROW (no LaneHolds hold) ends as row_wait; a hold the
            # LaneHolds cap evicted is unknown.
            t0 = held.get('_hold_t0')
            row = dict(held, _feed_hold={
                'hold_ms': None if t0 is None else (time.monotonic() - t0) * 1000.0,
                'hold_outcome': LANE_HOLDS.ended(tx, mint) or ('unknown' if held.get('_hold_kind') == 'hold'
                                                               else 'row_wait')})
    if str(row['network_id']) in ('4663', '56'):
        # A RELAY PRE-BUY OF THIS COIN STILL DECIDING (4663; 56 since 2026-10-14, before 56's own hand-off
        # wait): wait on its row, then judge as today.
        from sniper.settings import memecoin as cfg
        knob = cfg.MEMECOIN_RH_RELAY_PREBUY if str(row['network_id']) == '4663' else cfg.MEMECOIN_BSC_RELAY_PREBUY
        if knob and relay_prebuy_in_flight(conn, row['mint'], row['detected_at'],
                                           network_id=str(row['network_id'])):
            if key in deferred:
                return None
            if len(deferred) >= MAX_SLOT_DEFERRED:     # full: today's behaviour, no wait
                pass
            else:
                deferred[key] = dict(row, _held_for='relay_prebuy')
                return {'status': 'deferred', 'reason': 'relay_prebuy_in_flight'}
        elif (deferred.get(key) or {}).get('_held_for') == 'relay_prebuy':
            deferred.pop(key, None)                    # the pre-buy ended: judged as a fresh detection
    if str(row['network_id']) == '56':
        try:
            sent = json.loads(row['source_valuation_json'] or '{}').get('bsc_fast_lane_sent') is True
            age = (now - _parse_iso(source_time(row))).total_seconds()
        except (ValueError, TypeError, AttributeError):
            sent, age = False, 0
        # No sleep or cursor rewind. A lost hint expires from SOURCE arrival, never publication.
        # A known unfinished owner retains the row through its original freshness bound; this
        # grants no ordinary entry, including while its pending row is already signed or bound.
        if (other_lane == "bsc_native_pending" or
                (sent and other_lane != "lane_released_unsigned" and 0 <= age < 2.0)):
            if key in deferred:
                return None
            if len(deferred) >= MAX_SLOT_DEFERRED:
                return {'status': 'skipped', 'reason': 'bsc_fast_lane_handoff_capacity'}
            deferred[key] = dict(row, _held_for='bsc_fast_lane')
            return {'status': 'deferred', 'reason': 'bsc_fast_lane_handoff'}
        if (deferred.get(key) or {}).get('_held_for') == 'bsc_fast_lane':
            deferred.pop(key, None)
    outcome, slot_class = slot_lane_verdict(conn, row["signature"], row["detected_at"])
    if ":" not in str(row["signature"]):
        _note_slot_state(outcome != "unreadable", row["signature"])
    if outcome == "unreadable":
        # Refused, not consumed (Gamma LOW-2): a transient read error (SQLITE_BUSY) re-reads on
        # the next sweep until the detection's own freshness bound ends it.
        if key in deferred:
            return None
        if len(deferred) >= MAX_SLOT_DEFERRED:
            return {"status": "skipped", "reason": "slot_fallback_capacity"}
        deferred[key] = dict(row, _held_for="unreadable")
        return {"status": "deferred", "reason": "slot_lane_state_unreadable"}
    if (deferred.get(key) or {}).get("_held_for") == "unreadable":
        deferred.pop(key, None)                # readable again: judged as a fresh detection
    if outcome is None and key not in deferred:
        if slot_class is None and LANE_DOWN.covers(row):
            slot_class = LANE_DOWN_CLASS        # the slot lane was down: it never saw this buy
        result = _run_hook(conn, real_hook, row, slot_class=slot_class)
        if isinstance(result, dict) and result.get('status') == 'deferred':
            deferred[key] = dict(row)
        return result
    if outcome is None:
        from sniper.settings import memecoin as cfg
        if cfg.MEMECOIN_SOLANA_INFRA_PROVIDER == "shyft" and cfg.MEMECOIN_J7_SOLANA_VERIFIED_BACKUP:
            from sniper.services.memecoin_source_initiation import DIRECT_PROOF_VERSION, source_initiation_refusal
            candidate = OnchainEv(row)
            if (isinstance(candidate.source_initiation, dict)
                    and candidate.source_initiation.get("proof_version") == DIRECT_PROOF_VERSION
                    and source_initiation_refusal(candidate, "solana") is None):
                deferred.pop(key, None)
                return _run_hook(conn, real_hook, row, slot_class=slot_class)
    if outcome in (None, "in_flight"):
        # A signed slot send may still land: hold the detection, never block, never rewind.
        if key in deferred:
            return None
        if len(deferred) >= MAX_SLOT_DEFERRED:
            return {"status": "skipped", "reason": "slot_fallback_capacity"}
        deferred[key] = dict(row)
        from sniper.services import memecoin_slot_lane
        hold = memecoin_slot_lane.EXOTIC_PENDING.hold_left_ms(str(row["signature"]), time.monotonic())
        if hold is not None:
            # One line per detection deferred on an exotic leg's hold (Gamma: the canary reads it).
            return {"status": "deferred", "reason": "slot_lane_exotic_hold sig=%s hold_left_ms=%.0f"
                    % (row["signature"], hold)}
        return {"status": "deferred", "reason": "slot_lane_unresolved"}
    deferred.pop(key, None)
    if outcome == "filled":
        return {"status": "skipped", "reason": "slot_lane_filled"}
    if outcome == "decided":
        return {"status": "skipped", "reason": "duplicate_of_slot_lane"}
    if _fallback_already_attempted(conn, row["signature"], row["detected_at"]):
        return {"status": "skipped", "reason": "slot_fallback_already_attempted"}
    result = _run_hook(conn, real_hook, row, fallback=True, graduated=outcome == "graduated",
                       slot_class=slot_class)
    if result.get('status') == 'deferred':
        deferred[key] = dict(row)
    return result


def sweep_onchain_detections(conn, real_hook, *, cursor, now_fn, max_age_s,
                             approved_ids_fn=None, deferred=None, network_id=None):
    """Process detections newer than `cursor`, then return (new_cursor, results).

    Fresh + armed rows go through `real_hook`; everything else advances the cursor silently.
    One hook exception does not block the rest of the batch, but the failed row's id still
    advances the cursor — a detection is a moment, not a queue item worth retrying late.

    SLOT-LANE FALLBACK (2026-09-26, f4f73eec). A row whose transaction the slot lane is still
    handling is held in `deferred` (the caller's dict, owned by the lane thread) and re-checked
    at the top of every sweep: a slot fill drops it for good, a confirmed slot failure gives it
    ONE ordinary attempt, and it expires with the detection's own freshness (`max_age_s`). The
    cursor never rewinds. Without a `deferred` dict an unresolved slot attempt is skipped, as the
    duplicate rule did before.
    """
    if approved_ids_fn is None:
        from sniper.persistence import db_memecoin as m

        def approved_ids_fn(c, network_id):
            return m.real_approved_user_ids(c, network_id=network_id)

    held = deferred if deferred is not None else {}
    now = _parse_iso(now_fn())
    results = []

    def fresh(row):
        ts = _parse_iso(source_time(row))
        return ts is not None and now is not None and (now - ts).total_seconds() <= max_age_s

    for key, row in list(held.items()):
        # Ink's canonical journal and Base's preconfirmations can both retract a row (2026-10-09).
        retracted = (str(row.get("network_id") or "") in ("57073", "8453") and
                     conn.execute("SELECT source_observation_state FROM onchain_wallet_detections "
                                  "WHERE signature=?", (row["signature"],)).fetchone()[0] == "retracted")
        if retracted:
            held.pop(key, None)
            res = {"status": "skipped", "reason": "source_retracted"}
        elif not fresh(row):
            held.pop(key, None)
            # A wait on the native lane (4663/8453: its hold or its in-flight row) is named apart
            # from the slot lane's fallback (Gamma LOW on 15b613fae).
            res = {"status": "skipped", "reason": {"rh_fast_lane": "rh_fast_lane_wait_expired",
                                                   "relay_prebuy": "relay_prebuy_wait_expired"}.get(
                row.get("_held_for"), "slot_fallback_expired")}
        else:
            res = _route(conn, real_hook, row, held, now=now)
        if res is not None:
            results.append({"id": row["id"], "signature": row["signature"],
                            "mint": row["mint"], "handle": row["handle"], "result": res})

    rows = conn.execute(
        "SELECT id, signature, source_user_id, handle, address, source_slot, network_id, mint, source_size_usd, "
        "source_price, detected_at, block_time, source_valuation_json, source_observation_state "
        "FROM onchain_wallet_detections WHERE "
        + ("network_id=? AND " if network_id is not None else "") + "id > ? ORDER BY id",
        (str(network_id), cursor) if network_id is not None else (cursor,),
    ).fetchall()
    armed_by_network = {}
    new_cursor = cursor
    for row in rows:
        new_cursor = max(new_cursor, row["id"])
        if row["source_observation_state"] == "retracted":
            continue
        if not fresh(row):
            continue
        row_network = str(row["network_id"] or "solana")
        armed = armed_by_network.get(row_network)
        if armed is None:
            armed = set(approved_ids_fn(conn, row_network))
            armed_by_network[row_network] = armed
        if row["source_user_id"] not in armed:
            continue
        res = _route(conn, real_hook, row, held, now=now)
        if deferred is None and res is not None and res.get("status") == "deferred":
            held.clear()
            res = {"status": "skipped", "reason": "duplicate_of_feed"}
        if res is not None:
            results.append({"id": row["id"], "signature": row["signature"],
                            "mint": row["mint"], "handle": row["handle"], "result": res})
    return new_cursor, results


_MIRROR_MALFORMED_LOGGED = set()


def armed_mirror_scope(conn, network_id, env=None):
    """Every wallet armed on `network_id` now (real_approved_user_ids) when
    MEMECOIN_REAL_ONCHAIN_SELL_MIRROR_WALLETS lists "<network_id>:*" (2026-10-17, Alpha GO), plus the
    source of every OPEN real position on that chain (Gamma LOW-1: the mirror follows the source of a
    position we hold, so a disarm after our entry keeps it until the position closes); else empty.
    Read per sweep, so arming, disarming and closing apply at the next pass."""
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    scope = cfg.onchain_sell_mirror_scope(network_id, env=env)
    if scope["malformed"]:
        raw = (env if env is not None else os.environ).get("MEMECOIN_REAL_ONCHAIN_SELL_MIRROR_WALLETS", "")
        if raw not in _MIRROR_MALFORMED_LOGGED:
            _MIRROR_MALFORMED_LOGGED.add(raw)
            logger.warning("onchain sell mirror: %d malformed MEMECOIN_REAL_ONCHAIN_SELL_MIRROR_WALLETS "
                           "entr%s ignored (want chain:uid or chain:*)", scope["malformed"],
                           "y" if scope["malformed"] == 1 else "ies")
    if not scope["all_armed"]:
        return frozenset()
    held = {row[0] for row in conn.execute(
        "SELECT DISTINCT source_user_id FROM memecoin_positions WHERE is_open=1 AND execution_mode='real' "
        "AND network_id=? AND source_user_id IS NOT NULL", (str(network_id),))}
    return frozenset(m.real_approved_user_ids(conn, network_id=str(network_id))) | frozenset(held)


def mirror_wallets(conn, network_id, env=None):
    """The wallets whose decoded sales the on-chain mirror acts on: listed uids plus the armed scope."""
    from sniper.settings import memecoin as cfg
    return cfg.onchain_sell_mirror_wallets(network_id, env=env) | armed_mirror_scope(conn, network_id, env=env)


def sell_startup_cursor(conn, network_id, wallets):
    """The sell sweep's first cursor: just before the first sale of a listed wallet detected at or
    after the oldest of their OPEN real positions on this chain, so a sale during a restart still
    mirrors; durable pending source intents also rewind to their source row, including fast flips
    before our fill. With neither open positions nor pending mirrors, the table's end."""
    wallets = sorted(wallets)
    if not wallets:
        return 0
    from sniper.persistence import db_memecoin as m
    pending_ids = [row["id"] for row in m.pending_onchain_sell_mirrors(conn, network_id)
                   if row["source_user_id"] in wallets]
    marks = ",".join("?" for _ in wallets)
    oldest = conn.execute(
        "SELECT MIN(COALESCE(opened_recorded_at, opened_at)) FROM memecoin_positions WHERE is_open=1 "
        "AND execution_mode='real' AND network_id=? AND source_user_id IN (%s)" % marks,
        (str(network_id), *wallets)).fetchone()[0]
    if oldest is None:
        return (min(pending_ids) - 1 if pending_ids else
                int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM onchain_evm_sell_shadow").fetchone()[0]))
    first = conn.execute(
        "SELECT MIN(id) FROM onchain_evm_sell_shadow WHERE network_id=? AND detected_at >= ?",
        (str(network_id), oldest)).fetchone()[0]
    if pending_ids:
        first = min(pending_ids + ([first] if first is not None else []))
    if first is None:
        return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM onchain_evm_sell_shadow").fetchone()[0])
    return int(first) - 1


SELL_PENDING_HOLD_S = 600.0     # how long a sale waits for our in-flight entry to resolve


def sweep_onchain_sells(conn, sell_hook, *, cursor, network_id, wallets, held=None,
                        clock=time.monotonic):
    """Feed each new onchain_evm_sell_shadow row of a listed wallet on this chain to `sell_hook`;
    return (new_cursor, results). A hook failure is logged and the cursor still advances: the
    position's rails keep guarding it, and the hook's own record makes a re-read a no-op.

    Durable mirror intents are re-read regardless of the advanced cursor until safely decided.
    A row the hook answers `entry_pending` (the sale landed while our copy of that buy was still
    in flight, Gamma MEDIUM on 92756ef1d) goes to `held` (the caller's dict, owned by the sell
    thread) and is asked again at the top of every sweep until it decides or SELL_PENDING_HOLD_S
    passes; the cursor never rewinds."""
    from sniper.persistence import db_memecoin as m
    held = held if held is not None else {}
    results = []
    asked = set()

    def ask(row):
        asked.add(row["id"])
        try:
            return sell_hook(conn, dict(row))
        except Exception:
            logger.exception("onchain sell mirror failed id=%s tx=%s", row["id"], row["tx_hash"])
            return {"status": "error"}

    for row_id, (row, since) in list(held.items()):
        res = ask(row)
        if (res or {}).get("status") == "entry_pending" and clock() - since < SELL_PENDING_HOLD_S:
            continue
        held.pop(row_id, None)
        if res is not None and (res or {}).get("status") != "entry_pending":
            results.append({"id": row_id, "mint": row["mint"], "handle": row["handle"], "result": res})
    rows = conn.execute(
        "SELECT id, network_id, tx_hash, address, source_user_id, handle, mint, sold_atomic, "
        "pre_atomic, post_atomic, proof, block_number, detected_at FROM onchain_evm_sell_shadow "
        "WHERE network_id=? AND id > ? ORDER BY id", (str(network_id), cursor)).fetchall()
    by_id = {row["id"]: dict(row) for row in rows}
    for row in m.pending_onchain_sell_mirrors(conn, network_id):
        by_id.setdefault(row["id"], row)
    new_cursor = cursor
    for row in (by_id[row_id] for row_id in sorted(by_id)):
        new_cursor = max(new_cursor, row["id"])
        if row["id"] in asked:
            continue
        if row["source_user_id"] not in wallets:
            continue
        res = ask(row)
        if (res or {}).get("status") == "entry_pending":
            held[row["id"]] = (dict(row), clock())
            continue
        if res is not None:
            results.append({"id": row["id"], "mint": row["mint"], "handle": row["handle"],
                            "result": res})
    return new_cursor, results


def fanout_network_wake(wakes):
    for wake in wakes.values():
        wake.set()


def start_network_workers(network_ids, *, conn_factory, real_hook, now_fn, max_age_s, poll_s,
                          stop=None, finish_fn=None, log_fn=None, sell_hook=None, sell_wallets_fn=None):
    """One consumer per funded chain; connections, cursors, deferred rows and wakes never cross.

    The common datagram belongs to the runner's coordinator, which fans its hint out to these
    independent events. Same-wallet execution remains serialized by the hook's existing lock.
    """
    import threading
    stop = stop if stop is not None else threading.Event()
    wakes = {str(network): threading.Event() for network in network_ids}
    status = {network: dict(ts=None, cursor=0, phase="starting", errors=0) for network in wakes}
    threads = []

    def worker(network):
        conn, cursor, deferred, errors = None, 0, {}, 0
        try:
            conn = conn_factory()
            cursor = startup_cursor(conn, now_fn=now_fn, max_age_s=max_age_s, network_id=network)
            while not stop.is_set():
                status[network] = {**status[network], "cursor": cursor, "phase": "sweeping"}
                try:
                    cursor, hits = sweep_onchain_detections(
                        conn, real_hook, cursor=cursor, now_fn=now_fn, max_age_s=max_age_s,
                        deferred=deferred, network_id=network)
                    if network in sell_wakes:
                        sell_wakes[network].set()   # the sell thread follows each buy sweep
                    if finish_fn is not None:
                        finish_fn(conn)
                    else:
                        conn.rollback()
                    status[network] = dict(ts=now_fn(), cursor=cursor, phase="idle", errors=errors)
                    if log_fn is not None:
                        for hit in hits:
                            result = hit["result"] or {}
                            log_fn("[memecoin] onchain lane: %s %s -> %s %s chain=%s%s" % (
                                hit["handle"], hit["mint"][:8], result.get("status"),
                                result.get("reason") or "", network,
                                " slot_lane=%s" % result["slot_lane_skip"]
                                if result.get("slot_lane_skip") else ""))
                except Exception as exc:
                    errors += 1
                    if finish_fn is not None:
                        finish_fn(conn, failed=True)
                    else:
                        conn.rollback()
                    status[network] = {**status[network], "phase": "error", "errors": errors,
                                       "last_error": type(exc).__name__}
                    logger.exception("onchain lane network %s sweep failed", network)
                wait_for_next_sweep(wakes[network], poll_s)
        except Exception as exc:
            status[network] = {**status[network], "phase": "stopped", "errors": errors + 1,
                               "last_error": type(exc).__name__}
            logger.exception("onchain lane network %s stopped", network)
        finally:
            if conn is not None:
                conn.close()

    # ON-CHAIN SELL MIRROR (2026-10-07): its OWN thread and connection per chain, woken right after
    # each buy sweep, so an exit (seconds of quote/sign/confirm) never holds the buy sweep. Only
    # chains with listed wallets (MEMECOIN_REAL_ONCHAIN_SELL_MIRROR_WALLETS, read every pass).
    sell_wakes = {network: threading.Event() for network in wakes} if sell_hook is not None else {}

    def sell_worker(network):
        conn, cursor, held = None, None, {}
        try:
            conn = conn_factory()
            while not stop.is_set():
                wallets = (frozenset(sell_wallets_fn(network)) | armed_mirror_scope(conn, network)
                           if sell_wallets_fn is not None else frozenset())
                if wallets:
                    try:
                        if cursor is None:
                            cursor = sell_startup_cursor(conn, network, wallets)
                        cursor, sold = sweep_onchain_sells(
                            conn, sell_hook, cursor=cursor, network_id=network, wallets=wallets,
                            held=held)
                        if finish_fn is not None:
                            finish_fn(conn)
                        else:
                            conn.rollback()
                        if log_fn is not None:
                            for hit in sold:
                                result = hit["result"] or {}
                                log_fn("[memecoin] onchain sell mirror: %s %s -> %s %s chain=%s" % (
                                    hit["handle"], hit["mint"][:8], result.get("status"),
                                    result.get("reason") or "", network))
                    except Exception:
                        if finish_fn is not None:
                            finish_fn(conn, failed=True)
                        else:
                            conn.rollback()
                        logger.exception("onchain sell mirror %s sweep failed", network)
                wait_for_next_sweep(sell_wakes[network], poll_s)
        except Exception:
            logger.exception("onchain sell mirror %s stopped", network)
        finally:
            if conn is not None:
                conn.close()

    for network in wakes:
        thread = threading.Thread(target=worker, args=(network,),
                                  name="onchain-lane-" + network, daemon=True)
        thread.start()
        threads.append(thread)
    for network in sell_wakes:
        threading.Thread(target=sell_worker, args=(network,), name="onchain-sells-" + network,
                         daemon=True).start()
    return wakes, status, threads
