"""Item 4 (runner wiring) — the DORMANT real-money leg, wired into the poll loop.

`build_real_context` constructs the real leg ONLY when armed (MEMECOIN_REAL_ENABLED) with creds
present (a funded keypair file + a Solana RPC url). Absent either -> returns None and the paper runner
is byte-identical. When present it exposes:
  - execute_fn / exit_fn  — the bound Jupiter/RPC/solders executor (item 4 seams)
  - token_safety_fn       — pre-entry freeze/mint/fee/sell-route gate (item 15)
  - usdc_fn / sol_fn / token_fn — live JSON-RPC balance reads

The real leg REUSES the paper alpha_b EXIT DECISION (catastrophe stop / 12h timecap / mirror-on-sell)
but EXECUTES on-chain through exit_fn (chain-delta PnL, slippage ladder, stuck-bag) rather than a
modeled fill. Entry reuses real_entry (arm-gate -> S1 reserve -> token-safety -> executor) behind a
tight staleness gate. NOTHING here is integration-tested; a live smoke test (item 22) gates any arm.
"""
from __future__ import annotations

import logging
import functools
import json
import re
import math
import os
import queue
import collections
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sniper.analysis.exit_state_types import market_state_from_dict
from sniper.execution.entry_attribution import positive_price
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_alerts as alerts
from sniper.services import memecoin_entry_opportunity as entry_opportunity
from sniper.services import memecoin_exit_market_state as mkstate
from sniper.services import memecoin_exit_tape as _exit_tape
from sniper.execution.solana_rpc_pool import current_label as _current_rpc_label, rpc_label as _rpc_label
from sniper.services.memecoin_exit_brain import CUT, DERISK, TAKE_PROFIT, exit_engine
from sniper.services.memecoin_exits import _held_s, _parse as _parse_ts
from sniper.services.memecoin_funded_policy import (
    ENGINE_ENVELOPE_POLICY_ID, band_anchor_trail_pct, band_is_reservable,
    engine_policy_runtime_capable)
from sniper.services.memecoin_real_leg import network_matches_mint_family, real_entry
from sniper.services.memecoin_cluster_service import ClusterGateClient
from sniper.services.memecoin_origin import bypasses_selection
from sniper.services.memecoin_pons_metadata import (
    LIVE_SUPPLY_GUARD_MAX_AGE_S, LIVE_SUPPLY_MAX_AGE_S, read_cached_supply, read_live_supply)
from sniper.services.memecoin_runner import is_stale_entry
from sniper.services.memecoin_source_initiation import source_initiation_refusal
from sniper.settings import memecoin as cfg

from sniper.util.log_sample import log_sampled

logger = logging.getLogger(__name__)

_UNPARSEABLE_PAGED = set()


def _safe_alert(fn, *args):
    """Fire an operational alert best-effort — a send failure (or missing token) must never break the
    trade/exit path. send_telegram already fails soft without a token, so this is a no-op when unconfigured."""
    try:
        fn(*args)
    except Exception:
        logger.exception("memecoin alert failed")


def _report_unparseable_contracts(conn, positions, now_iso, lane):
    """Write one durable audit and page once for each invalid position."""
    from sniper.services.memecoin_telegram import send_telegram

    for pos in positions:
        pid = pos.get("id")
        try:
            exists = conn.execute(
                "SELECT 1 FROM memecoin_exit_trigger_audit "
                "WHERE position_id=? AND reason='contract_unparseable' LIMIT 1",
                (pid,),
            ).fetchone()
            if not exists:
                m.record_exit_trigger_audit(
                    conn, position_id=pid, reason="contract_unparseable",
                    display_price=None, preview_usdc=None, allowed=False,
                    detail=json.dumps({"code": "contract_unparseable", "held": True}),
                    checked_at=now_iso, lane=lane,
                )
        except Exception:
            logger.exception("invalid contract audit failed for position %s", pid)
        log_sampled(
            logger.error, ("contract_unparseable", pid),
            "invalid funded contract pos=%s mint=%s; the runner holds this position",
            pid, pos.get("mint"),
        )
        if pid not in _UNPARSEABLE_PAGED:
            _UNPARSEABLE_PAGED.add(pid)
            _safe_alert(
                send_telegram,
                "Funded contract invalid. Position %s is held. Mint: %s"
                % (pid, pos.get("mint")),
            )


def _wallet_is_armed(conn, ev, identity):
    """Does this wallet hold funded authority for THIS chain?

    Telegram is an operator channel for the funded book. A wallet we merely watch produces no
    funded outcome, so its REFUSALS are noise: measured 2026-08-06, 84 of the ~173 entry alerts
    in 24h were `unroutable_chain` on wallets with no authority on that chain at all — half the
    traffic, none of it actionable. Only refusals are gated; fills and failures always alert.
    Unknown inputs return True, so a lookup failure is never silent.
    """
    try:
        uid = getattr(ev, "source_user_id", None)
        chain = getattr(identity, "network_id", None) or getattr(ev, "network_id", None)
        if conn is None or not uid or not chain:
            return True
        return uid in m.real_approved_user_ids(conn, chain)
    except Exception:
        logger.exception("armed lookup failed for the alert gate — alerting rather than silencing")
        return True


import threading as _threading
# Retries re-run the whole funded hook, which re-emits the entry alerts. A refused fresh-pool buy
# already paged once on its first attempt; paging again on each of up to 12 re-quotes is the exact
# flood the operator saw (Tortuga ENTRY-ROUTE-REFUSAL fired per attempt, 2026-08-14). This flag,
# set by the retry lane around each re-attempt, silences the REFUSAL page and the RETRYABLE
# slippage-revert page — a retry that actually FILLS still pages (money moved), and a
# non-retryable failed still pages (money at risk). First failure and give-up each page once.
_entry_alert_suppress = _threading.local()


def _alert_entry_result(mint, notional, res, *, handle=None, ticker=None,
                        source_user_id=None, source_size_usd=None, source_price=None,
                        entry_lag_s=None, policy=None, network_id=None, source_id=None,
                        armed=True):
    from sniper.services.memecoin_telegram import notification_lifecycle_active
    if notification_lifecycle_active():
        return
    res = res or {}
    st = res.get("status")

    sig = res.get("sig") or res.get("tx_hash")
    if st == "filled" and res.get("booked_by") == "receipt_recovery":
        # NOT A NEW FILL (Gamma LOW on d9acb6959): the receipt recovery booked this swap while the
        # executor waited, and the position may already be closed. "filled $X" would announce an
        # entry that is not happening now.
        logger.warning("entry alert not sent: mint=%s tx=%s was booked by the receipt recovery",
                       mint, sig)
        return
    if st == "filled":
        # the executor spent the reserved (bankroll/fraction) clip, not the caller's static
        # fallback — alert the true spend when the result carries it.
        spent = res.get("notional") or res.get("spent") or notional
        _safe_alert(
            lambda *a: alerts.alert_entry(
                *a, handle=handle, ticker=ticker, source_user_id=source_user_id,
                source_size_usd=source_size_usd, source_price=source_price,
                entry_lag_s=entry_lag_s, policy=policy, network_id=network_id,
                source_id=source_id,
            ),
                    mint, res.get("fill_price") or 0.0, spent, sig)
    elif st == "failed":
        # a RETRYABLE revert re-attempt stays silent (per-attempt flood); first + give-up page
        if not (_is_transient_entry_miss(res) and getattr(_entry_alert_suppress, "on", False)):
            _safe_alert(alerts.alert_failed_tx, mint, sig, res.get("reason"))
    elif st and st != "not_approved":
        # REFUSALS ONLY are gated on funded authority (2026-08-06). `filled` and `failed` above
        # are deliberately NOT gated: those mean money moved or is stuck, and silencing one
        # because a wallet-table lookup disagreed is the worst failure this could have. An
        # earlier draft gated the whole function and a test proved it could swallow a fill.
        if not armed or getattr(_entry_alert_suppress, "on", False):
            return
        # A transient fresh-pool miss (no quote / no route) is handled by the re-quote lane: it
        # will fill silently or page ONCE on give-up. Do not page each attempt (Tortuga fired per
        # re-attempt, 2026-08-14). Only when the retry lane is enabled — otherwise the one-shot
        # page is still the operator's only signal.
        if _is_transient_entry_miss(res):
            from sniper.settings import memecoin as _cfg
            if float(getattr(_cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 0) or 0) > 0:
                return
        _safe_alert(
            lambda: alerts.alert_entry_result(
                mint, res, ceiling=cfg.MEMECOIN_REAL_MAX_MCAP_USD,
                handle=handle, ticker=ticker,
            ),
        )


def _alert_exit_result(mint, pid, reason, res, *, handle=None, ticker=None, entry_notional=None):
    st = (res or {}).get("status")
    from sniper.services.memecoin_telegram import notification_lifecycle_active
    if st in {"closed", "derisked"} and notification_lifecycle_active():
        return
    if st == "closed" and (res.get("proceeds_source") == "unknown" or res.get("realized") is None):
        return            # unknown realized (unpriced close or partial) was paged already; never $0
    if st == "closed":
        _safe_alert(lambda *a: alerts.alert_exit(*a, handle=handle, ticker=ticker,
                                                 entry_notional=entry_notional),
                    mint, res.get("realized") or 0.0, reason,
                    res.get("sig") or res.get("tx_hash"))
    elif st == "stuck" and res.get("reason") == "token_balance_zero":
        _safe_alert(lambda *a: alerts.alert_exit_balance_zero(*a, handle=handle, ticker=ticker),
                    mint, pid)
    elif st == "stuck":
        _safe_alert(lambda *a: alerts.alert_stuck_bag(*a, reason=res.get("reason") or
                                                    res.get("failure_type")), mint, pid)
    elif st == "failed":
        _safe_alert(alerts.alert_failed_tx, mint, res.get("sig") or res.get("tx_hash"),
                    res.get("reason"))
    # de-risk outcomes were SILENT before 2026-07-17 (pos 6605: unknown-confirm burned the
    # rail with no alert, no log — operator saw a lying "derisked" badge). Name every outcome.
    elif st == "derisked":
        _safe_alert(lambda *a: alerts.alert_derisk(*a, handle=handle, ticker=ticker),
                    mint, res.get("received") or 0.0, res.get("sig") or res.get("tx_hash"))
    elif st == "unknown":
        fraction = res.get("fallback_fraction")
        partial = (reason == "derisk" or
                   res.get("fallback_reason") == "route_capacity" and
                   type(fraction) in (int, float) and 0.0 < fraction < 1.0)
        logger.warning("%s confirm UNKNOWN pos=%s mint=%s sig=%s — recovery pass will settle",
                       "derisk" if partial else "full close", pid, mint,
                       res.get("sig") or res.get("tx_hash"))
        alert = alerts.alert_derisk_unknown if partial else alerts.alert_close_unknown
        _safe_alert(lambda *a: alert(*a, handle=handle, ticker=ticker),
                    mint, res.get("sig") or res.get("tx_hash"))
    elif st == "derisk_failed":
        logger.warning("derisk FAILED pos=%s mint=%s reason=%s (next sweep retries)",
                       pid, mint, res.get("reason"))


def _alert_engine_decision(pos, engine_audit, res):
    """WO-12 — one Telegram line on every ACTED engine decision, so the operator sees the engine
    think during the canary. DISPLAY ONLY: fired post-execution via _safe_alert, so it can never
    delay or change a funded exit (a send failure is swallowed)."""
    from sniper.services.memecoin_telegram import enqueue_telegram
    action = engine_audit.get("candidate_action")
    codes = ",".join(engine_audit.get("reason_codes") or [])
    status = (res or {}).get("status", "?")
    frac = engine_audit.get("proposed_fraction")
    frac_s = f" frac={float(frac):.2f}" if frac else ""
    label = pos.get("ticker") or str(pos.get("mint") or "")[:8]
    enqueue_telegram(
        f"\U0001F916 ENGINE {action} {label} pos={pos.get('id')}{frac_s} "
        f"status={status} [{codes}]",
        kind="operational:ENGINE",
        source_id=f"position:{pos.get('id')}")

def stop_hit_counts(hits_so_far, started_at, now_iso, spacing_s):
    """Does this stop confirmation count toward a SWEEP-count debounce? Pure. Returns
    (counts, breach_started_at).

    A sweep count means "held about N ticks" (settings: one fast-lane sweep per
    MEMECOIN_FASTLANE_TICK_S), not "seen N times". Rail wakes (2026-09-27) run extra sweeps between
    ticks, so a second confirmation 0.3 s after the first would satisfy a 2-sweep debounce in 0.3 s.
    Hit k+1 therefore counts only once the breach is k x `spacing_s` old. The breach clock is the
    same `stop_breach_started_at` the seconds window uses, stamped at the first counted hit.

    Every unreadable case counts, toward selling, like stop_breach_confirmed: no prior hit, no or
    unparseable stamp (a row from before this rule), or a clock that ran backwards.
    """
    if not hits_so_far or hits_so_far < 1 or not started_at:
        return True, now_iso
    start, now = _parse_ts(started_at), _parse_ts(now_iso)
    try:
        elapsed = (now - start).total_seconds()
    except TypeError:                      # missing, or one stamp naive and one aware
        return True, now_iso
    if elapsed < 0:
        return True, now_iso
    return elapsed >= hits_so_far * float(spacing_s), started_at


def stop_breach_confirmed(started_at, now_iso, window_s):
    """Has a nominated stop persisted for `window_s` of WALL CLOCK? Pure, no I/O, no clock.

    Returns (confirmed, elapsed_s). `confirmed` True means SELL.

    Every failure mode resolves toward selling, deliberately. This runs on a policy family with no
    mirror and no trail, so the stop and the -80% catastrophe floor are the only downside exits —
    a window that cannot resolve must not become an indefinite hold. Specifically:
      * window <= 0            -> confirmed (feature off; the caller's sweep counter governs)
      * missing/unparseable ts -> confirmed (we cannot prove the breach is young)
      * clock moved backwards  -> confirmed (elapsed is not trustworthy)

    This replaces a converter that turned a seconds target into a FROZEN HIT COUNT against a
    nominal tick rate. That was not a duration: the runner counts detections, the fast lane sleeps
    after variable work so 1s is a floor, and a ~31s slow-lane fallback stretched a "75 second"
    window to 38 MINUTES.
    """
    try:
        window = float(window_s)
    except (TypeError, ValueError):
        return True, None
    if not (window > 0) or window != window:
        return True, None
    start = _parse_iso_or_none(started_at)
    now = _parse_iso_or_none(now_iso)
    if start is None or now is None:
        return True, None
    elapsed = (now - start).total_seconds()
    if elapsed < 0:
        return True, elapsed
    return elapsed >= window, elapsed


def _parse_iso_or_none(ts):
    from datetime import datetime
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def rails_blind_context(*, pnl_pct, stop_pct, tp_pct, trail_pct, trail_armed, derisk_pct,
                        catastrophe_pct, stall_rule=None, near_pct=10.0):
    """Describe WHICH rails a blind position actually has and how close the nearest one is.

    A `rails_blind` row used to say only "exact price unresolved". That reads identically for two
    opposite situations, and the difference is the whole meaning of the row:

      * the position has a live rail sitting near the current price and cannot confirm it — a
        fault, and money is exposed;
      * the position's near rails are switched off or far away, so there is nothing an exact
        price would decide — the demand gate is correctly spending nothing.

    Position 30938 (ETHICS) spent four days emitting the second kind while being read as the
    first. The operator had set `sl_pct=0` on it, so its only nearby rail was gone, but the frozen
    policy still carried a trail and a de-risk, which is what `exact_policy` keys on. Nothing was
    broken; the log could not say so.

    Distances are in PnL POINTS and always positive-toward-the-rail: a stop at -60 with the mid at
    -62.5 is -2.5, i.e. already through it. Pure function, no I/O.
    """
    def _f(v):
        try:
            out = float(v)
        except (TypeError, ValueError):
            return None
        return out if out == out and out not in (float("inf"), float("-inf")) else None

    pnl = _f(pnl_pct)
    rails = {}
    stop, tp = _f(stop_pct), _f(tp_pct)
    derisk, trail, cat = _f(derisk_pct), _f(trail_pct), _f(catastrophe_pct)
    if stop is not None and stop > 0:
        rails["stop"] = pnl + stop if pnl is not None else None
    if cat is not None and cat > 0:
        rails["catastrophe"] = pnl + cat if pnl is not None else None
    if tp is not None and tp > 0:
        rails["tp"] = tp - pnl if pnl is not None else None
    if derisk is not None and derisk > 0:
        rails["derisk"] = derisk - pnl if pnl is not None else None
    if trail_armed and trail is not None and trail > 0:
        rails["trail"] = 0.0 if pnl is not None else None
    if stall_rule:
        rails["stall"] = 0.0 if pnl is not None else None
    nearest = min(((key, distance) for key, distance in rails.items() if distance is not None),
                  key=lambda kv: kv[1], default=(None, None))
    return {
        "price_known": pnl is not None,
        "active_rails": sorted(rails),
        "nearest_rail": nearest[0],
        "nearest_rail_distance_pct": (round(nearest[1], 2) if nearest[1] is not None else None),
        # The judgement the reader needs, stated rather than inferred from five numbers.
        "consequential": bool(nearest[1] is not None and nearest[1] <= float(near_pct)),
    }


def _has_active_loss_rail(pos, override, cfg):
    """True iff SOME rail could fire on the exact-vs-nominal price gap: a stop, a catastrophe
    floor, or an mcap SL. RAILS-BLIND exists to warn that those rails read an optimistic price —
    when all of them are disabled (operator turned stops + catastrophe off, 2026-08-14), a blind
    exact quote misleads nothing, and paging about it is pure noise (GARYFREAK pos#37881)."""
    try:
        _tp, _tpm, stop_mag, sl_mcap = effective_real_thresholds(pos, override)
        if (stop_mag or 0) > 0 or sl_mcap is not None:
            return True
        from sniper.services.memecoin_funded_policy import effective_funded_policy
        frozen, effective = effective_funded_policy(pos, override=override)
        cat = (float(effective["catastrophe_pct"]) if frozen is not None
               else float(getattr(cfg, "MEMECOIN_REAL_CATASTROPHE_PCT", 0) or 0))
        return cat > 0
    except Exception:
        return True   # fail-LOUD: if we cannot prove the rails are off, keep alerting


def effective_real_thresholds(pos, override=None):
    """Per-position effective real exit thresholds: an operator/template exit override wins over the
    per-chain cfg rails. Returns (tp_pct, tp_target_mcap, stop_mag, sl_target_mcap). override
    None/empty -> the cfg defaults, i.e. DEFAULT-INERT: byte-identical behaviour to no override, so
    deploying this changes nothing for a position until an override is set on it."""
    from sniper.services.memecoin_funded_policy import effective_funded_policy
    frozen, effective = effective_funded_policy(pos, override=override)
    ov = override or {}
    if frozen is not None:
        return (float(effective["tp_pct"]), effective.get("tp_target_mcap"),
                float(effective["stop_pct"]), effective.get("sl_target_mcap"))
    cfg_stop = cfg.real_stop_pct_for_chain(pos.get("network_id"))   # per-chain magnitude; 4663 = 0
    stop_mag = abs(float(ov["sl_pct"])) if ov.get("sl_pct") is not None else cfg_stop
    # A trail-only row is a complete "bare trail" instruction, not a partial merge with mutable
    # process defaults. Ladder rows that want staged TP/de-risk semantics persist those fields.
    bare_trail = (ov.get("trail_pct") is not None and ov.get("tp_pct") is None
                  and ov.get("tp_target_mcap") is None)
    default_tp, _default_derisk, _default_trail, _default_arm = _default_exit_rails(pos)
    tp_pct = (float(ov["tp_pct"]) if ov.get("tp_pct") is not None
              else 0.0 if bare_trail else default_tp)
    return tp_pct, ov.get("tp_target_mcap"), stop_mag, ov.get("sl_target_mcap")


def exact_price_relevant(pos, *, trail_pct, derisk_pct, stall_rule, override=None):
    """Could this position EVER need an exact route quote?

    A STATIC policy property, not "does it need one now" — the demand gate decides that, and it
    still economises the spend. This only decides eligibility.

    A rail that decides on PRICE needs a price the bag can actually fetch, so a take-profit and a
    stop count exactly as much as a trail or a de-risk. Both were omitted. follow-30-50-v1 and
    tp-only-v1 zero trail AND de-risk — their only rails are the TP and the stop — so they read as
    irrelevant and the prepass skipped them with no counter, no log, and no audit row. On EVM the
    fast lane quotes every open position anyway; on Solana, which `in_scope` excludes by design,
    nothing did. Position 36012 sat open 28 minutes with `last_executable_price` NULL and every
    miss counter at 0 while its +30% TP and -50% stop read the raw mid (2026-08-12), and every
    wallet on the follow-30-50 allowlist shares that shape.
    """
    if (pos.get("_mcap_exit_target") or (override or {}).get("tp_target_mcap") is not None
            or (override or {}).get("sl_target_mcap") is not None):
        return True
    if trail_pct > 0 or derisk_pct > 0 or stall_rule is not None:
        return True
    # Narrow on purpose. A policy that still MIRRORS the source wallet out keeps a non-price exit,
    # so declining to quote it is a defensible economy. A policy with mirror OFF has none: its
    # take-profit, its stop and the catastrophe floor are the only discretionary exits, which is
    # exactly why they cannot decide on a mid the bag would not fetch. Widening this to every
    # stop-bearing position instead would quote the whole book on the three sweep callers that
    # pass no demand_fn (runner loop 761/790/948) — the 2026-07-27 hammering pattern.
    from sniper.services.memecoin_funded_policy import effective_funded_policy, is_exit_trial_v4
    frozen, effective = effective_funded_policy(pos, override=override)
    # DEGEN EXIT TRIAL V4 mirrors, yet its TP and floor decide on the whole-bag exact quote, and the
    # pre-registered V0 grade needs the exit_decision route quote every sweep (spec 2026-10-01).
    if frozen is not None and is_exit_trial_v4(frozen):
        return True
    if frozen is None or (effective or {}).get("mirror_on_sell", True):
        return False
    tp_pct, _tp_mcap, stop_mag, _sl_mcap = effective_real_thresholds(pos, override)
    return bool(float(tp_pct or 0.0) > 0 or float(stop_mag or 0.0) > 0)


def _derisk_sizing(pos, override=None):
    """(mode, fraction) for the de-risk partial, from the position's frozen policy. Legacy
    positions (no contract) and v1 contracts size recover-entry; v2 fraction-mode contracts carry
    their fixed fraction. Shared by nomination sizing and executable confirmation."""
    from sniper.services.memecoin_funded_policy import effective_funded_policy
    frozen, effective = effective_funded_policy(pos, override=override)
    if frozen is not None and str(effective.get("derisk_mode")) == "fraction":
        try:
            return "fraction", float(effective.get("derisk_fraction") or 0.0)
        except (TypeError, ValueError):
            return "recover_entry", 0.0
    return "recover_entry", 0.0


def _effective_catastrophe_pct(pos, override=None):
    """Effective catastrophe floor: the frozen contract's, else the cfg default. Mirrors the branch
    inside alpha_b_real_exit_reason exactly, so a caller outside that function can test a reading's
    MAGNITUDE against the same floor -- the returned reason label alone cannot: b_stop's own check
    runs first and returns before catastrophe is ever considered, so any position with a tighter
    frozen stop (every standard funded 4663 contract: stop_pct=35 < catastrophe_pct=80) reports
    b_stop for a death spiral straight through the catastrophe floor. 'catastrophe' as a REASON
    only fires when the frozen stop is 0/disabled -- it is not the only way to reach that depth."""
    from sniper.services.memecoin_funded_policy import effective_funded_policy
    frozen, effective = effective_funded_policy(pos, override=override)
    return (float(effective["catastrophe_pct"]) if frozen is not None
            else cfg.MEMECOIN_REAL_CATASTROPHE_PCT)


def _effective_derisk_pct(pos, override=None):
    """Effective one-shot de-risk threshold: an explicit override wins; a bare/ladder trail row with no
    derisk field means 'no derisk stage' (0); else the cfg default. Shared so the trail arming decision
    reads the same derisk state the derisk rail does."""
    from sniper.services.memecoin_funded_policy import effective_funded_policy
    frozen, effective = effective_funded_policy(pos, override=override)
    if frozen is not None:
        return float(effective["derisk_pct"])
    ov = override or {}
    if ov.get("derisk_pct") is not None:
        return float(ov["derisk_pct"])
    if ov.get("trail_pct") is not None:
        return 0.0
    return _default_exit_rails(pos)[1]


def _stall_rule(pos):
    """The position's frozen no-new-high stall rule, or None. A malformed/absent rule is None, so
    a position without one behaves exactly as it did before this rail existed."""
    try:
        from sniper.services.memecoin_copy_arm import position_stall_rule
        return position_stall_rule(pos)
    except Exception:
        logger.exception("stall rule read failed pos=%s; rail inactive", pos.get("id"))
        return None


def _position_in_rollover_cohort(pos):
    """Compare position and rollout anchor as UTC instants. Empty is back-compatible for pure
    callers/tests; an armed funded runner rejects an empty cutoff during startup validation."""
    cutoff = (cfg.MEMECOIN_REAL_ROLLOVER_MIN_OPENED_AT or "").strip()
    if not cutoff:
        return True
    op, cut = _parse_ts(pos.get("opened_at")), _parse_ts(cutoff)
    if (op is None or cut is None or op.tzinfo is None or cut.tzinfo is None
            or op.utcoffset() is None or cut.utcoffset() is None):
        return False
    return op.astimezone(timezone.utc) >= cut.astimezone(timezone.utc)


def _default_exit_rails(pos):
    """Return (tp, derisk, trail, arm) for this position's complete default policy cohort."""
    from sniper.services.memecoin_funded_policy import frozen_policy_from_position
    frozen = frozen_policy_from_position(pos)
    if frozen is not None:
        return (frozen.tp_pct, frozen.derisk_pct, frozen.trail_pct, frozen.trail_arm_pct)
    if not _position_in_rollover_cohort(pos):
        return cfg.MEMECOIN_REAL_LEGACY_TP_PCT, 0.0, 0.0, cfg.MEMECOIN_REAL_TRAIL_ARM_PCT
    return (cfg.MEMECOIN_REAL_TP_PCT, cfg.MEMECOIN_REAL_DERISK_PCT,
            cfg.MEMECOIN_REAL_TRAIL_PCT, cfg.MEMECOIN_REAL_TRAIL_ARM_PCT)


def derisk_confirmed(pos):
    """Did the de-risk actually RECOVER PRINCIPAL — as opposed to merely having been fired?

    Two different questions were being answered by one flag (2026-07-22, position 9003).
    `derisked_at` is set even on an UNKNOWN confirm, deliberately: the sell may have landed and
    re-firing a maybe-landed sell risks a double-sell. That guard is right, and it still stands —
    `derisked_at` alone continues to mean "do not fire the de-risk again". It just cannot also
    mean "the stake is back", which is what licenses a trail.

    `derisk_sig` is the discriminator already carried on the row: it is persisted ONLY while the
    de-risk is unverified, and cleared by settle_position_derisk_sig (it landed) or
    clear_position_derisk (it provably did not). So a confirmed de-risk is a stamp with no
    outstanding signature.

    9003 is the case: unknown confirm -> stamp + sig -> the trail armed on an imaginary recovered
    principal and closed the position at -65% with prior_proceeds=0, 371s after the stamp and
    829s before the recovery pass could have re-armed the rail.
    """
    return bool(pos.get("derisked_at")) and not pos.get("derisk_sig")


def effective_trail(pos, override=None):
    """Single source of truth for the trailing-stop policy, shared by BOTH the nomination
    (alpha_b_real_exit_reason) and the executable confirmation (executable_exit_allowed) so the two can
    never diverge. (2026-07-18: they did — nomination used the cfg default while confirmation hardcoded
    0.0, leaving the default trail permanently dead on priced venues; 4663 only "worked" by skipping
    confirmation entirely.) Returns (trail_pct, armed):
      * OPERATOR / ladder override (trail_pct explicitly set): a trail paired with a de-risk stage arms
        only after the entry stake is recovered (derisked_at set, or no derisk stage); a bare trail arms
        immediately. Bypasses the rollout cohort — an explicit per-position instruction.
      * DEFAULT rollover trail (no override, cfg MEMECOIN_REAL_TRAIL_PCT): arms once the exact-balance
        high-water crosses +MEMECOIN_REAL_TRAIL_ARM_PCT for positions in the rollout cohort."""
    from sniper.services.memecoin_funded_policy import effective_funded_policy
    frozen, effective = effective_funded_policy(pos, override=override)
    ov = override or {}
    override_trail = ov.get("trail_pct")
    entry = pos.get("entry_price") or 0.0
    peak = pos.get("executable_peak_price") or entry
    if frozen is not None:
        trail_pct = float(effective["trail_pct"])
        if effective["trail_requires_derisk"]:
            armed = derisk_confirmed(pos)
        else:
            arm_pct = float(effective["trail_arm_pct"])
            armed = peak > 0 and entry > 0 and peak >= entry * (1.0 + arm_pct / 100.0)
        return trail_pct, armed
    if override_trail is not None:
        derisk_pct = _effective_derisk_pct(pos, override)
        # Same rule on the operator-override path: a trail paired with a de-risk stage arms only
        # once the stake is actually back. A bare trail (no de-risk stage) still arms immediately.
        armed = derisk_confirmed(pos) or derisk_pct <= 0
        # DESK TRAIL ARMING (operator 2026-10-26, Gamma replay docs/audits/2026-10-26-desk-trail-arming-replay.md:
        # a trail armed at entry sold normal early dips; arming at +15% was better on 32 trades). Desk rails only;
        # unset/0 = today. The stop and TP are untouched.
        desk_arm = cfg.desk_trail_arm_pct() if pos.get("signal_source") == "desk" else 0.0
        if armed and desk_arm > 0:
            armed = peak > 0 and entry > 0 and peak >= entry * (1.0 + desk_arm / 100.0)
        return float(override_trail), armed
    _tp_pct, _derisk_pct, trail_pct, arm_pct = _default_exit_rails(pos)
    armed = peak > 0 and entry > 0 and peak >= entry * (1.0 + arm_pct / 100.0)
    return trail_pct, armed


def alpha_b_real_exit_reason(pos, mid, now_iso, *, override=None, current_mcap=None):
    """The rails for a REAL position, decoupled from the paper alpha_b study knobs: catastrophe
    stop (MEMECOIN_REAL_STOP_PCT), full take-profit (MEMECOIN_REAL_TP_PCT), one-shot de-risk
    (MEMECOIN_REAL_DERISK_PCT: sell back the entry, ride the rest), timecap
    (MEMECOIN_REAL_MAX_HOLD_S) — price rails may be 0, but armed max-hold must be positive.
    A per-position `override` (operator or system template, from effective_exit_override) replaces
    the tp/stop thresholds — by % or, with `current_mcap`, an absolute target market cap. Operator
    overrides may LOOSEN; None -> exact cfg behaviour (default-inert). Mirror-on-sell is feed-driven,
    handled separately. Pure. `mid` None/<=0 = unquotable this sweep -> only the TIME cap and the
    unquotable zombie guard can fire.
    Returns 'b_stop'|'real_tp'|'derisk'|'real_trail'|'peak_stall'|'b_timecap'|'unquotable'|None."""
    entry = pos.get("entry_price") or 0.0
    held = _held_s(pos.get("opened_at"), now_iso)
    from sniper.services.memecoin_funded_policy import effective_funded_policy
    frozen, effective = effective_funded_policy(pos, override=override)
    tp_pct, tp_mcap, stop_mag, sl_mcap = effective_real_thresholds(pos, override)
    if mid and mid > 0 and entry > 0:
        pnl_pct = (mid / entry - 1.0) * 100.0
        if sl_mcap is not None and current_mcap and current_mcap <= sl_mcap:
            return "b_stop"
        if stop_mag > 0 and pnl_pct <= -stop_mag:
            return "b_stop"
        # CATASTROPHE FLOOR: absolute max-loss backstop. Fires even when stop_mag is 0 — i.e. on
        # 4663 (chain default 0) or when an override disabled/widened the stop. A tighter stop above
        # already returned; this only catches death spirals, never normal -30/-50% dips. Debounced
        # like b_stop in the sweep (bad-tick guard).
        cat = (float(effective["catastrophe_pct"]) if frozen is not None
               else cfg.MEMECOIN_REAL_CATASTROPHE_PCT)
        if cat > 0 and pnl_pct <= -cat:
            return "catastrophe"
        if tp_mcap is not None and current_mcap and current_mcap >= tp_mcap:
            return "real_tp"
        if tp_pct > 0 and pnl_pct >= tp_pct:
            return "real_tp"
        derisk_pct = _effective_derisk_pct(pos, override)
        if derisk_pct > 0 and not pos.get("derisked_at") and pnl_pct >= derisk_pct:
            return "derisk"
        # PEAK-STALL rail (2026-08-04): the defining rule of the peak_lock_e / momentum_a copy
        # arms — harvest once the position stops making new executable highs. Sits below the loss
        # rails (a stalled position that is also through its stop exits as a stop) and above the
        # trail, which peak_lock_e does not use. The counter is advanced by the sweep; this is a
        # pure read of it, so the rail stays testable without a runner.
        stall = _stall_rule(pos)
        if stall is not None:
            peak = pos.get("executable_peak_price") or entry
            peak_gain = (peak / entry - 1.0) * 100.0 if entry > 0 else 0.0
            # ELAPSED SECONDS since the last new executable high — never a sweep count. The paper
            # arms were measured at a ~15s bracket sweep; this loop runs as fast as 1s, so counting
            # sweeps would fire the rule ~15x early. `executable_peak_at` is stamped by the sweep
            # when it advances the peak; absent (no high recorded yet) the clock has not started.
            stalled_s = _held_s(pos.get("executable_peak_at"), now_iso) \
                if pos.get("executable_peak_at") else None
            if (stalled_s is not None
                    and peak_gain >= stall["activate_pct"]
                    and (pnl_pct > 0 or not stall["require_profit"])
                    and stalled_s >= stall["stall_seconds"]):
                return "peak_stall"
        # TRAIL rail: exit once the mid gives back trail_pct from the high-water peak. The threshold and
        # arming come from effective_trail() — the SAME helper executable_exit_allowed re-checks against
        # exact-balance proceeds — so nomination and confirmation cannot diverge (2026-07-18 fix).
        trail_pct, trail_armed = effective_trail(pos, override)
        peak = pos.get("executable_peak_price") or entry
        if (trail_pct > 0 and trail_armed and peak > 0
                and mid <= peak * (1.0 - trail_pct / 100.0)):
            return "real_trail"
    unquotable_s = (float(effective["unquotable_exit_s"]) if frozen is not None
                    else cfg.MEMECOIN_REAL_UNQUOTABLE_EXIT_S)
    max_hold_s = (float(effective["max_hold_s"]) if frozen is not None
                  else cfg.MEMECOIN_REAL_MAX_HOLD_S)
    if (not mid or mid <= 0) and unquotable_s > 0 and not pos.get("stuck_bag"):
        quote_age = _held_s(pos.get("last_quote_ts") or pos.get("opened_at"), now_iso)
        if quote_age > unquotable_s:
            return "unquotable"
    if max_hold_s > 0 and held > max_hold_s:
        return "b_timecap"
    return None


# LP-CATASTROPHIC EXIT (copy lane, Solana; operator 2026-10-21, Alpha design call). Its evidence is
# the bound pool's own liquidity, not a price, so it is deliberately NOT in _EXECUTABLE_PRICE_REASONS
# (a pulled pool's preview cannot be required to cross the -80% catastrophe floor first), not in
# _STOP_DEBOUNCE_REASONS (one complete slot is the event) and not in _LOSS_EXIT_REASONS (the
# first-seconds no-loss window must not hold a rug). It sells through the same exit_fn seam as every
# rail. Blind minutes are audited, never silent; the first fire per position pages.
LP_CATASTROPHIC_REASON = "lp_catastrophic"
# Shared by the periodic sweep and the hot lanes (Gamma LOW 2): one lock, held for dict work only.
_LP_LOCK = threading.Lock()
_LP_STATE = {}              # position id -> last rail state ("measured" or "blind:<why>")
_LP_PAGED = set()           # positions whose fire already paged
_LP_CHECK_MS = collections.deque(maxlen=512)    # per-position check wall time, ms
_LP_BLIND = collections.Counter()               # blind checks by why, since start
# ponytail: _LP_STATE keeps one short string per position ever checked; prune on close if it grows.


def lp_rail_status(cache=None):
    """Heartbeat: the per-position LP check's own time (deploy gate: p90 < 1 ms), blind checks by
    reason, and the complete rows refused LP authority for lacking a transaction identity."""
    with _LP_LOCK:
        ms, count, blind = list(_LP_CHECK_MS), len(_LP_CHECK_MS), dict(_LP_BLIND)
    ms.sort()
    pick = lambda q: round(ms[min(len(ms) - 1, int(q * len(ms)))], 4) if ms else None
    return {"check_ms": {"n": count, "p50": pick(0.5), "p90": pick(0.9),
                         "max": round(ms[-1], 4) if ms else None},
            "blind_checks": blind,
            "unsigned_complete_rows": getattr(cache, "lp_unsigned_rows", None)}


def _lp_transition(conn, pos, state, now_iso, lane):
    """One audit row per rail-state TRANSITION (Gamma LOW 3): becoming blind, a new blind reason,
    or becoming measurable again. A steady state writes nothing on the exit path."""
    new = "measured" if state.get("state") == "measured" else "blind:%s" % state.get("why")
    with _LP_LOCK:
        old = _LP_STATE.get(pos["id"])
        _LP_STATE[pos["id"]] = new
        if new != "measured":
            _LP_BLIND[state.get("why") or "unknown"] += 1
    if new == old or (old is None and new == "measured"):
        return
    m.record_exit_trigger_audit(
        conn, position_id=pos["id"], reason="lp_blind" if new != "measured" else "lp_measurable",
        display_price=None, preview_usdc=None, allowed=False,
        detail=json.dumps(dict(state, previous=old), sort_keys=True), checked_at=now_iso, lane=lane)


def _lp_catastrophic_reason(conn, pos, cache, now_iso, lane):
    """'lp_catastrophic' when the bound pool lost >= the configured share of its liquidity since bind,
    else None. Never raises into the sweep; a broken read is no fire, and is logged."""
    frac = cfg.MEMECOIN_REAL_LP_CATASTROPHIC_FRAC
    if not cfg.MEMECOIN_REAL_LP_CATASTROPHIC_EXIT or frac is None:
        return None
    if m.normalized_coin_network(pos.get("network_id"), pos.get("mint")) != "solana":
        return None
    read = getattr(cache, "lp_removal", None)
    if read is None:
        return None
    started = time.perf_counter()
    try:
        state = read(pos["mint"])
        _lp_transition(conn, pos, state, now_iso, lane)
        if state.get("state") != "measured":
            return None
        if state["fraction"] < frac:
            return None
        detail = dict(state, threshold=frac)
        m.record_exit_trigger_audit(
            conn, position_id=pos["id"], reason=LP_CATASTROPHIC_REASON, display_price=None,
            preview_usdc=None, allowed=True, detail=json.dumps(detail, sort_keys=True),
            checked_at=now_iso, lane=lane)
        logger.warning("lp_catastrophic: pos=%s mint=%s pool=%s L %s -> %s (%.1f%%) slot=%s",
                       pos["id"], pos["mint"], state["pool"], state["l_before"], state["l_after"],
                       100.0 * state["fraction"], state["slot"])
        with _LP_LOCK:
            first_page = pos["id"] not in _LP_PAGED
            _LP_PAGED.add(pos["id"])
        if first_page:
            from sniper.services.memecoin_alerts import send_alert
            send_alert("LP-CATASTROPHIC-EXIT",
                       "pos %s %s: bound pool %s liquidity %s -> %s (-%.1f%%, threshold %.0f%%) at "
                       "slot %s; selling now" % (
                           pos["id"], pos.get("ticker") or pos["mint"][:8], state["pool"][:12],
                           state["l_before"], state["l_after"], 100.0 * state["fraction"],
                           100.0 * frac, state["slot"]))
        return LP_CATASTROPHIC_REASON
    except Exception:
        logger.exception("lp catastrophic check failed pos=%s", pos.get("id"))
        return None
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with _LP_LOCK:
            _LP_CHECK_MS.append(elapsed_ms)


_EXECUTABLE_PRICE_REASONS = {"real_tp", "mcap_target", "b_stop", "derisk", "real_trail", "catastrophe",
                             "peak_stall"}


def _timed_preview(pos, preview_fn):
    """Buy an exit preview and accumulate its cost on the position, in milliseconds.

    THE EXIT STAGE MAP COULD NOT SEE THIS QUOTE, and it is the one that decides the exit. The
    executor's clock (`started`, evm_real_seams.py) opens when execute_exit_evm is ENTERED, but the
    preview is bought here in the sweep, several steps earlier, to confirm the rail. So both
    `memecoin_exit_execution_attempts.total_ms` and the new stage map excluded it: the "5-7 second
    exit" reported to the operator is executor-entry-to-closed, NOT decision-to-closed, and the true
    figure is higher by exactly this.

    That mattered most on the reasons I had just changed. Handing the executor the preview removes
    the SECOND quote for real_tp / real_trail / peak_stall, which shows up as a smaller `quote_ms`,
    while the FIRST quote stayed invisible -- so the map would have reported the exit getting
    cheaper by a constant it could not account for, on precisely those reasons. If the preview turns
    out to be the expensive one (the entry path's `quote_ms` is p50 1871ms, p90 9152ms) the map
    would have pointed at the wrong stage entirely. Found by Gamma reviewing b7eead50; the
    instrument had the same blind spot as the thing it was built to measure.

    Accumulates rather than assigns: a position may be previewed at more than one site in a single
    sweep, and the exit is charged for all of it. Cleared with `_confirmed_exit_preview`.
    """
    mark = time.monotonic()
    try:
        return preview_fn(pos)
    finally:
        pos["_exit_preview_ms"] = (pos.get("_exit_preview_ms") or 0.0) + (
            time.monotonic() - mark) * 1000.0
        # "own" beats a prior "batch": this position paid for a quote of its own, and that cost IS
        # attributable even if an earlier batched one was not.
        pos["_exit_preview_src"] = "own"
_STOP_DEBOUNCE_REASONS = {"b_stop", "catastrophe"}      # bad-tick guarded (wick into the floor)
# EVERY reason that SELLS AT A LOSS. The hard no-loss-sell window refuses all of these inside its
# first seconds; take-profit, de-risk and trail are deliberately absent because blocking a profit
# exit is enormous tail risk for no measured gain (80 TPs affected at a 60s window, +$21 in
# aggregate, worst case -$1,706).
_LOSS_EXIT_REASONS = {"b_stop", "catastrophe", "vol_death"}
# TP CONFIRMATION (2026-08-29 operator). Its OWN set, never merged into the stop's. The stop is
# debounced against a bad tick reading low; the take-profit is held to sell into CONTINUATION.
# They are opposite intentions that happen to share an elapsed-time helper, and a single shared
# set would silently debounce the stop the moment someone widened it — there is a guard test.
_TP_CONFIRM_REASONS = {"real_tp"}


def _tp_confirm_window_s():
    """ONE take-profit window for every chain and policy: the env value, clamped.

    2026-09-03 (operator): the 4663 override that forced 3.0s here is gone. A per-chain constant
    that outranked `MEMECOIN_REAL_TP_CONFIRM_SECONDS` made that setting a lie on the only chain
    where it ran, and 3.0s could not be satisfied by a 1.6s sweep cadence on a coin that spikes
    for one sample. The take-profit now fires on the touch unless the env says otherwise.
    """
    return cfg.tp_confirm_window_s()


def _preview_value(preview, field, default=None):
    if preview is None:
        return default
    if isinstance(preview, dict):
        return preview.get(field, default)
    return getattr(preview, field, default)


# How old a previously-RESOLVED exact price may be and still bound the loss rails. Beyond this
# it is not evidence about now, so the rails fall back to nominal and `rails_blind` says so. 15
# minutes: long enough to ride a normal provider outage, short enough that a recovered coin is
# not stopped out on a price from a different regime.
_LAST_EXACT_MAX_AGE_S = 900.0

# Consecutive blind sweeps before the operator is paged. ~5 sweeps of a funded loop is well past
# transient provider noise while still far inside a stop's debounce window.
_RAILS_BLIND_ALERT_SWEEPS = 5

# EDGE STATE for the blind-but-idle rail log (2026-09-02). position id -> monotonic start.
#
# This was the only per-sweep line among the eleven that CI's log level was discarding, so it logs
# both edges instead of the state. Entry says a rail went blind; the exit says how long it lasted,
# which is the number an incident actually needs.
#
# A position that CLOSES while blind never reaches the exit edge, so entries are pruned by age on
# insert. `ponytail:` bounded by _BLIND_IDLE_MAX_AGE_S rather than by the sweep's position set —
# pruning against the live set would need the whole set threaded in here for a dict that holds at
# most a handful of ids.
_BLIND_IDLE_SINCE = {}
_BLIND_IDLE_MAX_AGE_S = 3600.0


def _blind_idle_clear(pos):
    """Log the exit edge with the blind duration, and drop the state. Silent if never blind."""
    started = _BLIND_IDLE_SINCE.pop(pos.get("id"), None)
    if started is None:
        return
    logger.warning(
        "real rails blind-but-idle CLEARED pos=%s mint=%s — blind window ended after %.1fs",
        pos.get("id"), pos.get("mint"), max(0.0, time.monotonic() - started))


def _blind_idle_prune(now_monotonic):
    for pid, started in list(_BLIND_IDLE_SINCE.items()):
        if now_monotonic - started > _BLIND_IDLE_MAX_AGE_S:
            _BLIND_IDLE_SINCE.pop(pid, None)
# The rails count as blind only after NO exact price has resolved for this long. Three demand
# heartbeats: one missed heartbeat is provider noise, three is an outage. Distinct from
# _LAST_EXACT_MAX_AGE_S (900s), which bounds how long a stale price may still ARM a loss rail.
_RAILS_BLIND_STALE_S = 3.0 * float(getattr(cfg, "MEMECOIN_EXACT_QUOTE_HEARTBEAT_S", 60.0))


def _keep_route_quote(pos, preview, seam, *, attempted=False):
    """Persist a route quote the exit path already computed. Buffered, fail-soft, decision-free.

    There is no route-space price series anywhere in this system — `executable_exit_value` is an
    alias for `mid` on 100% of 3.2M mark rows — while the exit path computes exact whole-bag quotes
    constantly and keeps only a ratcheted scalar. This keeps the values. It adds no API call and it
    can never affect what a rail decides.

    `attempted`: this site asked for the quote. A None preview is a miss only then; a site that
    never asked (no rail needs the exact price, no preview fn) records nothing.
    """
    try:
        from sniper.services import memecoin_route_series as _rs
        from sniper.util.clock import real_utc_now_iso as _rs_now
        _rs.record(pos, preview, seam=seam, now_iso=_rs_now(), value_fn=_preview_value,
                   attempted=attempted)
    except Exception:
        pass          # an observer must never reach the money path it observes


def _quoted_bag_qty(pos, preview, decimals_fn):
    """The quantity the exact quote itself valued (amount_atomic / 10**decimals), or None when
    the amount or the token's decimals are unknown."""
    try:
        atomic = int(_preview_value(preview, "amount_atomic") or 0)
        decimals = decimals_fn(pos["mint"]) if decimals_fn is not None else None
    except Exception:
        logger.exception("quoted bag quantity: decimals read failed pos=%s", pos.get("id"))
        return None
    if atomic <= 0 or not isinstance(decimals, int) or isinstance(decimals, bool) or decimals < 0:
        return None
    return atomic / (10 ** decimals)


def _executable_unit_price(conn, pos, preview, *, decimals_fn=None):
    """Exact remaining-bag unit price, safe before and after partial exits.

    An exit fill with an unknown quantity (an unpriced partial whose intent could not say it) makes
    ledger open_qty the quantity BEFORE that sale, so the price would read low by the partial's
    fraction. Only then is the bag the quote's own amount (Alpha 2026-10-07, narrowed (a));
    unknown decimals leave the unit price unknown. Every other position keeps ledger open_qty."""
    expected = _preview_value(preview, "expected_usdc")
    try:
        expected = float(expected)
    except (TypeError, ValueError):
        return None
    qty = float(m.open_qty(conn, pos["id"]) or 0.0)
    if m.exit_qty_unknown(conn, pos["id"]):
        qty = _quoted_bag_qty(pos, preview, decimals_fn)
        if qty is None:
            return None
    # STASH IT ON THE POSITION. `open_qty` is NOT a column on memecoin_positions — it is computed
    # here — so the sweep's pos dicts never carry it, and the route-series recorder was writing
    # unit_price NULL on every peak_preview / exit_decision / exit_audit row while the system knew
    # the quantity perfectly well. Its "never fake a per-unit price" branch was being fed a STARVED
    # dict rather than a genuine unknown, which is the worse failure: a correct guard producing a
    # wrong answer because its input lied.
    # The key name already means exactly this in the equity-mark path, so the two agree.
    if qty > 0:
        pos["open_qty"] = qty
    if not math.isfinite(expected) or expected <= 0 or not math.isfinite(qty) or qty <= 0:
        return None
    price = expected / qty
    return price if math.isfinite(price) and price > 0 else None


def _exit_anchor_tick(pos, unit):
    """The anchor-checked EVM exit's sticky level after this preview (spec 2026-10-01 §3.1), or
    None when the chain is off or the tick leaves it. It rides set_position_last_executable's
    UPDATE, so the exit below reads the anchor this very tick wrote."""
    from sniper.settings import memecoin as cfg
    if str(pos.get("network_id") or "") not in cfg.memecoin_evm_exit_anchor_chains():
        return None
    from sniper.services import memecoin_exit_anchor as anchor
    return anchor.preview_anchor(pos["id"], anchor.effective_anchor(pos), unit,
                                 min_pct=cfg.MEMECOIN_EVM_EXIT_ANCHOR_MIN_PCT,
                                 hold_s=cfg.MEMECOIN_EVM_EXIT_ANCHOR_HOLD_S)


def _exit_anchor_redrive(pos):
    """The held event exit (the mirror sell) this tick re-drives, or None; chain off is None.

    The operator's per-position mirror hold and the contract's mirror_on_sell are read again on
    every re-drive, exactly as real_mirror_on_sell reads them: a hold set during the anchor hold
    stops the sale (Gamma LOW 2 on e0faedf70)."""
    from sniper.settings import memecoin as cfg
    if str(pos.get("network_id") or "") not in cfg.memecoin_evm_exit_anchor_chains():
        return None
    from sniper.services import memecoin_exit_anchor as anchor
    reason = anchor.redrive_reason(pos["id"])
    if reason != "mirror":
        return reason
    if pos.get("mirror_disabled") or pos.get("tracking_paused_at"):
        return None
    from sniper.services.memecoin_funded_policy import effective_funded_policy
    frozen, effective = effective_funded_policy(pos, override=None)
    if frozen is not None and not effective["mirror_on_sell"]:
        return None
    return reason


def _native_band(pos, cache, now_iso, amount_atomic, decimals_fn=None):
    """The bound pool's native rung for this bag (memecoin_native_exit_rung), or None (unreadable):
    {low, high, pair, quote_usd, mark_unit}. Cache reads only; the pool's virtual quote is fetched
    once per pool in the background and is None (high unknown) until it lands."""
    from sniper.services import memecoin_native_exit_rung as rung
    pair = cache.exit_rung_pair(pos["mint"], now_ts=time.monotonic(),
                                max_age_s=cfg.MEMECOIN_SOLANA_NATIVE_RUNG_MAX_AGE_S)
    if pair is None or not amount_atomic:
        return None
    from sniper.services.memecoin_live_marks import fresh_quote_usd
    quote_usd = fresh_quote_usd([pair["quote_mint"]], now_iso=now_iso).get(pair["quote_mint"])
    if quote_usd is None:
        return None
    low, high = rung.band(pair, amount_atomic, quote_usd,
                          fee_low_bps=cfg.MEMECOIN_SOLANA_NATIVE_RUNG_FEE_LOW_BPS,
                          fee_high_bps=cfg.MEMECOIN_SOLANA_NATIVE_RUNG_FEE_HIGH_BPS,
                          virtual=rung.virtual_quote(pair["pool"]))
    if low is None:
        return None
    decimals = None
    if decimals_fn is not None:
        try:
            decimals = decimals_fn(pos["mint"])
        except Exception:
            decimals = None
    return {"low": low, "high": high, "pair": pair, "quote_usd": quote_usd,
            "mark_unit": rung.mark_unit(pair, quote_usd, decimals) if decimals is not None else None}


def configure_native_exit_hint(context, cache, *, now_fn):
    """Attach the runner's existing reserve cache once. False means known foreign pool or a
    healthy exact-size Jupiter route; None means unknown. An existing Jupiter route only triggers
    a native build on True (severe divergence). Blind exits can still build without a hint.
    The simulated native USDC floor still decides route selection."""
    setter = (context or {}).get("set_native_exit_hint_fn")
    if cache is None or not callable(setter):
        return False

    def hint(mint, atomic, quote):
        from sniper.services import memecoin_native_exit_rung as rung
        from sniper.services.memecoin_live_marks import fresh_quote_usd
        try:
            if (quote.get("inputMint") != mint or quote.get("outputMint") != rung.USDC_MINT
                    or int(quote["inAmount"]) != atomic or atomic <= 0):
                return None
            expected = int(quote["outAmount"]) / 1_000_000.0
            if not math.isfinite(expected) or expected <= 0:
                return None
            pair = cache.exit_rung_pair(mint, now_ts=time.monotonic(),
                                        max_age_s=cfg.MEMECOIN_SOLANA_NATIVE_RUNG_MAX_AGE_S)
            if pair is None:
                return None
            virtual = rung.cached_virtual_quote(pair["pool"])
            if virtual == rung.NOT_PUMPSWAP:
                return False
            if virtual is None:
                return None
            usd = fresh_quote_usd([pair["quote_mint"]], now_iso=now_fn()).get(pair["quote_mint"])
            high = rung.native_rung_usd(pair, atomic, usd,
                                        cfg.MEMECOIN_SOLANA_NATIVE_RUNG_FEE_LOW_BPS, virtual)
            divergence = cfg.MEMECOIN_SOLANA_NATIVE_RUNG_DIVERGENCE_PCT
            if high is None or not math.isfinite(divergence) or not 0 <= divergence < 100:
                return None
            return expected < high * (1.0 - divergence / 100.0)
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    setter(hint)
    return True


def _native_stop_rung(pos, preview, detail, display_price, cache, override, now_iso, decimals_fn=None):
    """A Jupiter-confirmed b_stop judged against the bound pool's native rung
    (memecoin_native_exit_rung). Any failure here keeps today's verdict (the stop stands)."""
    if (not cfg.MEMECOIN_SOLANA_NATIVE_RUNG or cache is None
            or str(pos.get("network_id") or "solana") != "solana"):
        return True, detail
    from sniper.services import memecoin_native_exit_rung as rung
    try:
        expected = float(_preview_value(preview, "expected_usdc"))
        unit = detail.get("executable_unit_price")
        quantity = expected / float(unit) if unit else None
        if not quantity or not math.isfinite(quantity):
            return True, detail
        quoted_at = _preview_value(preview, "quoted_at")
        jupiter_age = _held_s(quoted_at, now_iso) if quoted_at else None
        evidence = _preview_value(preview, "quote_evidence") or {}
        native = _native_band(pos, cache, now_iso, _preview_value(preview, "amount_atomic"), decimals_fn)
        if native is not None and jupiter_age is not None and native["pair"]["seen_age_s"] > jupiter_age + 2.0:
            native = None            # the pool read is older than the Jupiter quote: no evidence, stop stands
        bound = cache.exit_rung_pair(pos["mint"], now_ts=time.monotonic(), max_age_s=1e9)
        if bound is None or rung.virtual_quote(bound["pool"]) in (None, rung.NOT_PUMPSWAP):
            # No bound AMM pool, or one not (yet) verified PumpSwap (Raydium, Meteora DLMM/CLMM:
            # the vault ratio is not their price): today's single rung.
            return True, detail
        detail.update(rung_jupiter_age_s=jupiter_age,
                      rung_jupiter_context_slot=(evidence.get("context_slot")
                                                 if isinstance(evidence, dict) else None))
        if native is not None:
            pair = native["pair"]
            detail.update(rung_pool=pair["pool"], rung_pool_slot=pair["slot"],
                          rung_pool_age_s=pair["seen_age_s"], rung_quote_usd=native["quote_usd"],
                          rung_mark_unit=native["mark_unit"])
        from sniper.services.memecoin_funded_policy import effective_funded_policy
        frozen, effective = effective_funded_policy(pos, override=override)
        max_hold = float((effective or {}).get("unquotable_exit_s") or 900.0) if frozen is not None else 900.0
        if rung.is_exotic(bound):
            # The native value never prices the quote-token -> SOL leg Jupiter may be pricing (Gamma LOW).
            max_hold = min(max_hold, cfg.MEMECOIN_SOLANA_NATIVE_RUNG_EXOTIC_HOLD_S)
        detail["rung_max_hold_s"] = max_hold
        fire, judged = rung.judge(
            pos["id"], jupiter_usd=expected,
            native_low=None if native is None else native["low"],
            native_high=None if native is None else native["high"], quantity=quantity,
            display_price=float(display_price) if display_price else None,
            entry_price=float(pos.get("entry_price") or 0.0),
            stop_pct=effective_real_thresholds(pos, override)[2],
            catastrophe_pct=_effective_catastrophe_pct(pos, override),
            divergence_pct=cfg.MEMECOIN_SOLANA_NATIVE_RUNG_DIVERGENCE_PCT,
            max_hold_s=max_hold, now_ts=time.monotonic())
        detail.update(judged)
        if not fire:
            logger.warning("real b_stop held pos=%s mint=%s code=%s jupiter_usd=%.4f native_low=%s "
                           "native_high=%s display=%s", pos["id"], pos["mint"], detail.get("code"), expected,
                           detail.get("rung_native_low_usd"), detail.get("rung_native_high_usd"),
                           display_price)
        return fire, detail
    except Exception:
        logger.exception("native stop rung failed pos=%s (the stop stands)", pos.get("id"))
        return True, detail


def _native_blind_view(conn, pos, cache, override, now_iso, decimals_fn):
    """While Jupiter is blind: what the native rung alone says about the stop (audit only; selling
    through the pool is phase B). None when off, not Solana, no bound pool or unreadable."""
    if (not cfg.MEMECOIN_SOLANA_NATIVE_RUNG or cache is None or decimals_fn is None
            or str(pos.get("network_id") or "solana") != "solana"):
        return None
    from sniper.services import memecoin_native_exit_rung as rung
    try:
        qty = float(m.open_qty(conn, pos["id"]) or 0.0)
        decimals = decimals_fn(pos["mint"])
        if qty <= 0 or decimals is None:
            return None
        native = _native_band(pos, cache, now_iso, int(round(qty * 10 ** int(decimals))), decimals_fn)
        if native is None:
            return None
        view = rung.blind_view(native_low=native["low"], native_high=native["high"], quantity=qty,
                               entry_price=float(pos.get("entry_price") or 0.0),
                               stop_pct=effective_real_thresholds(pos, override)[2],
                               catastrophe_pct=_effective_catastrophe_pct(pos, override))
        view.update(pool=native["pair"]["pool"], pool_slot=native["pair"]["slot"],
                    pool_age_s=native["pair"]["seen_age_s"], mark_unit=native["mark_unit"])
        if view["native_stop"]:
            log_sampled(logger.warning, ("native_blind_stop", pos["id"]),
                        "real rails BLIND pos=%s mint=%s: the native rung alone is past the stop "
                        "(%.1f%%); no Jupiter route to sell (phase B sells through the pool)",
                        pos["id"], pos["mint"], view["native_best_pnl_pct"])
        return view
    except Exception:
        logger.exception("native blind view failed pos=%s", pos.get("id"))
        return None


def confirm_execution_stop(conn, pos, expected, quantity, *, current_mcap=None, bag_unknown=False,
                           confirmed_custom_stop=False):
    """Confirm the selected execution quote and reset a refused stop.

    `bag_unknown`: the sweep's b_stop could not value the bag (an exit of unknown quantity and no
    decimals) and stood only on the understated total-proceeds fallback, not on an mcap crossing.
    Passed by the sweep, never re-derived here (Alpha 2026-10-07)."""
    from sniper.util.clock import real_utc_now_iso
    try:
        override = m.effective_exit_override(conn, pos["id"])
        _, _, stop_mag, sl_mcap = effective_real_thresholds(pos, override)
        custom_stop_only = sl_mcap is not None and (stop_mag == 0 or confirmed_custom_stop)
        allowed, detail = executable_exit_allowed(
            conn, pos, "b_stop", {"expected_usdc": expected, "input_quantity": quantity},
            override=override, current_mcap=current_mcap, quote_qty=quantity)
    except Exception:
        if not confirmed_custom_stop:
            raise
        logger.exception("execution custom stop check failed pos=%s", pos.get("id"))
        custom_stop_only = True
        allowed, detail = False, {"code": "execution_custom_stop_check_failed"}
    detail["execution_quote"] = True
    # Percent stops REFUSE ONLY ON EVIDENCE (2026-09-11, Gamma's review D2). This is a SECOND witness:
    # sweep already confirmed the stop on an exact whole-balance quote. Only a computed value above
    # the stop ("executable_threshold_not_met") may overrule it. An unreadable execution quote is
    # "unknown", not "hold": refusing on it reset the debounce, returned `deferred`, paged nothing,
    # and a real stop could ride to the -80% catastrophe rail. A custom-cap-only stop needs a
    # readable cap; its market mark or old sweep verdict cannot replace that evidence.
    if not allowed and detail.get("code") != "executable_threshold_not_met" and custom_stop_only:
        if detail.get("code") != "execution_custom_stop_check_failed":
            detail["unreadable_code"] = detail.get("code")
            detail["code"] = "execution_custom_stop_unknown_hold"
    elif not allowed and detail.get("code") != "executable_threshold_not_met" and bag_unknown:
        # The sweep's verdict came from the understated fallback, so it is no evidence: HOLD until a
        # readable witness can value the bag (Gamma/Alpha 2026-10-07). Only in this state.
        detail["unreadable_code"] = detail.get("code")
        detail["code"] = "execution_quote_unreadable_bag_unknown_hold"
        logger.warning("execution stop check unreadable pos=%s code=%s on a bag the sweep could not "
                       "value -- holding", pos["id"], detail["unreadable_code"])
        _page_unit_price_unknown(pos)
    elif not allowed and detail.get("code") != "executable_threshold_not_met":
        detail["unreadable_code"] = detail.get("code")
        detail["code"] = "execution_quote_unreadable_sweep_verdict_stands"
        allowed = True
        logger.warning("execution stop check unreadable pos=%s code=%s expected_usd=%s -- selling on "
                       "the sweep's confirmed stop", pos["id"], detail["unreadable_code"], expected)
    # Bookkeeping must never become a verdict: a `database is locked` here refused stops.
    try:
        # OWN REASON (2026-09-11, Gamma D6): the audit upserts on (position, reason, minute), so a
        # `b_stop` row here overwrote the sweep's confirmation row from the same minute and the
        # ledger could not show both verdicts (the V5 replay gate joins on reason=exit_reason).
        m.record_exit_trigger_audit(
            conn, position_id=pos["id"], reason="b_stop_execution", display_price=None,
            preview_usdc=expected, allowed=allowed, detail=json.dumps(detail, sort_keys=True),
            checked_at=real_utc_now_iso(), lane="execution",
        )
    except Exception:
        logger.exception("execution stop audit write failed pos=%s (verdict unchanged)", pos["id"])
    if not allowed:
        try:
            m.set_position_stop_hits(conn, pos["id"], 0)
            m.set_position_stop_breach_started(conn, pos["id"], None)
        except Exception:
            logger.exception("execution stop debounce reset failed pos=%s (still refused)", pos["id"])
        logger.warning("execution stop refused pos=%s code=%s expected_usd=%s",
                       pos["id"], detail["code"], expected)
    return allowed, detail


def confirm_execution_target(conn, pos, reason, expected, quantity, *, minimum_usdc=None):
    """Re-read the current TP and require a custom target's encoded floor before signing."""
    try:
        allowed, detail = executable_exit_allowed(
            conn, pos, reason, {"expected_usdc": expected},
            override=m.effective_exit_override(conn, pos["id"]), quote_qty=quantity)
        detail["execution_quote"] = True
        target = detail.get("target_mcap")
        if (allowed and target is not None
                and m.shared_mint_open_real_count(conn, pos["mint"], pos.get("network_id")) > 1):
            allowed = False
            detail["code"] = "execution_target_quantity_unbound"
        if allowed and target is not None:
            try:
                minimum = float(minimum_usdc) if not isinstance(minimum_usdc, bool) else float("nan")
            except (TypeError, ValueError):
                minimum = float("nan")
            floor_cap, basis = _mcap_at_price(pos, minimum / float(quantity))
            if floor_cap is None:
                allowed = False
                detail["code"] = "execution_target_minimum_unknown"
            else:
                detail.update(minimum_output_usdc=minimum, minimum_output_mcap=floor_cap,
                              minimum_mcap_basis=basis)
                if floor_cap < float(target):
                    allowed = False
                    detail["code"] = "execution_target_floor_not_met"
    except Exception:
        # The executors retain the sweep verdict on a raised check; a custom TP must fail closed.
        logger.exception("execution target check failed pos=%s", pos.get("id"))
        allowed, detail = False, {"code": "execution_target_check_failed", "execution_quote": True}
    try:
        from sniper.util.clock import real_utc_now_iso
        m.record_exit_trigger_audit(
            conn, position_id=pos["id"], reason=f"{reason}_execution", display_price=None,
            preview_usdc=expected, allowed=allowed, detail=json.dumps(detail, sort_keys=True),
            checked_at=real_utc_now_iso(), lane="execution")
    except Exception:
        logger.exception("execution target audit write failed pos=%s (verdict unchanged)", pos.get("id"))
    return allowed, detail


_UNIT_PRICE_UNKNOWN_PAGED = set()
_UNIT_PRICE_UNKNOWN_LOCK = threading.Lock()


def _page_unit_price_unknown(pos):
    """Page the operator once per position per process: its bag cannot be valued."""
    with _UNIT_PRICE_UNKNOWN_LOCK:
        if pos["id"] in _UNIT_PRICE_UNKNOWN_PAGED:
            return
        _UNIT_PRICE_UNKNOWN_PAGED.add(pos["id"])
    text = ("pos %s %s: unit price unknown: qty NULL partial + no decimals. Catastrophe and trail "
            "cannot confirm; b_stop sells only on a readable execution quote. Set the partial's qty "
            "from its receipt (scripts/memecoin_backfill_derisk.py --fill)."
            % (pos["id"], pos.get("ticker") or pos.get("mint")))
    logger.warning(text)
    try:
        alerts.send_alert("unit_price_unknown", text)
    except Exception:
        logger.exception("unit price unknown page failed pos=%s", pos["id"])


def executable_exit_allowed(conn, pos, reason, preview, *, override=None, current_mcap=None,
                            engine_fraction=None, engine_exit=False, quote_qty=None,
                            decimals_fn=None):
    """Confirm a display-price TP/stop/de-risk against exact-balance executable proceeds. Honors the
    same per-position override as the reason decision (default-inert without one) so an operator TP
    at +30% is confirmed at +30%, not the cfg default. Custom mcap targets use the exact sell's
    unit price times the position's target supply; a market mark cannot authorize a crossing.

    `engine_fraction`/`engine_exit` (the exit-engine-v3 adapter only): the engine's own frozen
    decision rules already decided to fire — this function's exact-balance floor above (expected
    quote > 0, entry_notional > 0) still applies unconditionally, but the reason-specific PERCENT
    trigger recheck below is bypassed, because the engine-envelope policy zeroed that trigger to
    off. This never widens what the sweep will execute: it only lets a decision the engine already
    made under the monotonic-risk-reduction property through the same audited confirmation path
    every other exit uses."""
    expected = _preview_value(preview, "expected_usdc")
    try:
        expected = float(expected)
    except (TypeError, ValueError):
        expected = float("nan")
    if not math.isfinite(expected) or expected <= 0:
        # WHY the preview came back empty, not merely THAT it did. `PreviewResult` already carries
        # a typed `failure` and the provider that produced it, and both were discarded here — the
        # layer below knew and the boundary threw it away, which is the defect class this file has
        # been full of all day.
        #
        # It is the number the whole 4663 investigation reduces to: HOOD demanded an exact quote on
        # ~74 fastlane ticks and got 6, an 8% success rate, and NOTHING recorded whether those 68
        # misses were the token, the route or the provider. Those are three different fixes.
        #
        # Diagnostic only. The refusal, its `code`, and every fail-closed decision are unchanged;
        # callers match on `code`, and these are new keys beside it.
        detail = {"code": "preview_unavailable"}
        failure = _preview_value(preview, "failure")
        if failure is not None:
            detail["preview_failure"] = getattr(failure, "value", None) or str(failure)
        provider = _preview_value(preview, "provider_id")
        if provider:
            detail["preview_provider"] = str(provider)
        group = _preview_value(preview, "provider_group")
        if group:
            detail["preview_provider_group"] = str(group)
        return False, detail
    entry = float(pos.get("entry_notional") or 0.0)
    if entry <= 0:
        return False, {"code": "entry_notional_invalid"}
    proceeds = m.derisk_proceeds(conn, pos["id"])        # None while a partial is unpriced
    prior = float(proceeds or 0.0)
    projected = prior + expected
    pnl_pct = (projected / entry - 1.0) * 100.0
    detail = {
        "code": "threshold_confirmed",
        "projected_proceeds": round(projected, 8),
        "projected_pnl_pct": round(pnl_pct, 8),
        "prior_proceeds": None if proceeds is None else round(prior, 8),
    }
    if quote_qty is None:
        executable_price = _executable_unit_price(conn, pos, preview, decimals_fn=decimals_fn)
    else:
        try:
            qty = float(quote_qty)
        except (TypeError, ValueError):
            qty = float("nan")
        if not math.isfinite(qty) or qty <= 0:
            return False, {"code": "quote_quantity_invalid"}
        executable_price = expected / qty
        detail["quote_qty"] = qty
    entry_price = float(pos.get("entry_price") or 0.0)
    unit_pnl_pct = (
        (executable_price / entry_price - 1.0) * 100.0
        if executable_price is not None and entry_price > 0 else None
    )
    detail["executable_unit_price"] = (
        round(executable_price, 12) if executable_price is not None else None)
    detail["executable_unit_pnl_pct"] = (
        round(unit_pnl_pct, 8) if unit_pnl_pct is not None else None)
    # Nomination uses remaining-bag unit price vs entry. After a de-risk, recovered principal must
    # not mask a collapsing remainder during exact TP/stop/catastrophe confirmation. Synthetic pure
    # callers without fill rows retain the legacy total-proceeds fallback.
    threshold_pnl_pct = unit_pnl_pct if unit_pnl_pct is not None else pnl_pct
    # A partial of unknown quantity and no decimals: the bag cannot be valued, and the
    # total-proceeds fallback is short that partial. TP and catastrophe refuse; real_trail refuses
    # on its own (no finite price). b_stop keeps the fallback: a real
    # crash must still sell, and its execution witness re-checks on the chain quantity and overrules
    # a false stop (Alpha 2026-10-07).
    bag_unknown = unit_pnl_pct is None and quote_qty is None and m.exit_qty_unknown(conn, pos["id"])
    if bag_unknown:
        detail["bag_unknown"] = True
        _page_unit_price_unknown(pos)
    tp_pct, tp_mcap, stop_mag, sl_mcap = effective_real_thresholds(pos, override)
    if reason == "mcap_target":
        band = m.active_exit_target(conn, pos["id"])
        if band is None:
            return False, {**detail, "code": "mcap_target_inactive"}
        tp_mcap = band["mcap_low"]
    executable_mcap = None
    if tp_mcap is not None or sl_mcap is not None:
        executable_mcap, basis = _target_mcap_at_quote(pos, preview, executable_price,
                                                     quote_qty=quote_qty)
        detail.update(executable_mcap=executable_mcap, executable_mcap_basis=basis,
                      target_mcap=sl_mcap if reason == "b_stop" else tp_mcap)
    if reason in ("real_tp", "mcap_target"):
        mcap_crossed = (tp_mcap is not None and executable_mcap is not None
                        and executable_mcap >= float(tp_mcap))
        if mcap_crossed:
            detail["mcap_tp_confirmed"] = True
        allowed = mcap_crossed or (tp_pct > 0 and not bag_unknown and threshold_pnl_pct >= tp_pct)
        if reason == "mcap_target":
            allowed = mcap_crossed
    elif reason == "b_stop":
        mcap_crossed = (sl_mcap is not None and executable_mcap is not None
                        and executable_mcap <= float(sl_mcap))
        if mcap_crossed:
            detail["mcap_stop_confirmed"] = True
        percent_crossed = stop_mag > 0 and threshold_pnl_pct <= -stop_mag
        detail["percent_stop_confirmed"] = percent_crossed
        allowed = mcap_crossed or percent_crossed
        if bag_unknown and allowed and not mcap_crossed:
            detail["stop_on_fallback"] = True      # no evidence for the execution witness to keep
    elif reason == "derisk":
        if engine_fraction is not None:
            # Engine-driven partial (TAKE_PROFIT/DERISK, v3 prereg §2.2/2.3): the engine already
            # decided to fire under the frozen monotonic-risk-reduction rules; the engine-envelope
            # policy zeroed derisk_pct, so there is no legacy percent trigger to recheck here.
            detail["mode"] = "engine"
            detail["fraction"] = round(float(engine_fraction), 12)
            allowed = 0.0 < float(engine_fraction) < 0.9
        else:
            derisk_pct = _effective_derisk_pct(pos, override)
            mode, policy_fraction = _derisk_sizing(pos, override)
            if mode == "fraction":
                # v2 fixed-fraction partial: the exact whole-bag proceeds must confirm the trigger;
                # the sell size is the policy fraction, independent of prior recovery.
                detail["mode"] = "fraction"
                detail["fraction"] = round(policy_fraction, 12)
                allowed = (derisk_pct > 0 and pnl_pct >= derisk_pct
                           and 0.0 < policy_fraction < 0.9)
            else:
                needed = max(0.0, entry - prior)
                fraction = needed / expected if expected > 0 else float("inf")
                detail["fraction"] = round(fraction, 12) if math.isfinite(fraction) else None
                allowed = (derisk_pct > 0 and pnl_pct >= derisk_pct
                           and needed > 0 and 0.0 < fraction < 0.9)
    elif reason == "real_trail":
        if engine_exit:
            # Engine CUT (v3 prereg §2.1): the engine already decided under its frozen precedence
            # (LP pull / structure break / unsellable) — the engine-envelope policy zeroed trail_pct,
            # so there is no legacy giveback distance to recheck; the exact-balance floor above still
            # gated this call.
            detail["mode"] = "engine"
            allowed = True
        else:
            # SAME effective_trail() the nomination used — threshold + arming from one source of
            # truth, so a default-config trail (no override) is confirmed against
            # cfg.MEMECOIN_REAL_TRAIL_PCT, not the 0.0 that used to make it permanently
            # unconfirmable on priced venues.
            trail_pct, trail_armed = effective_trail(pos, override)
            peak = float(pos.get("executable_peak_price") or pos.get("entry_price") or 0.0)
            executable_price = executable_price if executable_price is not None else float("nan")
            trail_floor = peak * (1.0 - trail_pct / 100.0)
            detail["executable_price"] = (round(executable_price, 12)
                                          if math.isfinite(executable_price) else None)
            detail["trail_floor"] = round(trail_floor, 12) if trail_floor > 0 else None
            detail["trail_armed"] = trail_armed
            allowed = (trail_pct > 0 and trail_armed and peak > 0
                       and math.isfinite(executable_price) and executable_price <= trail_floor)
    elif reason == "catastrophe":
        from sniper.services.memecoin_funded_policy import effective_funded_policy
        frozen, effective = effective_funded_policy(pos, override=override)
        catastrophe_pct = (float(effective["catastrophe_pct"]) if frozen is not None
                            else cfg.MEMECOIN_REAL_CATASTROPHE_PCT)
        allowed = (catastrophe_pct > 0 and not bag_unknown
                   and threshold_pnl_pct <= -catastrophe_pct)
    else:
        return True, detail
    if not allowed:
        # Gamma LOW: a bag running without a percent TP/catastrophe/trail must not read as an
        # ordinary uncrossed threshold in the trace.
        detail["code"] = ("threshold_unknown_exit_qty" if bag_unknown
                          else "executable_threshold_not_met")
    return allowed, detail


def _provider_role_identity(url):
    """Startup-only, credential-free identity of an actual constructed client URL."""
    import hashlib
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    parts = urlsplit(url)
    host = parts.hostname
    authority = host + ((':' + str(parts.port)) if parts.port not in (None, 443) else '')
    canonical = urlunsplit((parts.scheme.lower(), authority, parts.path or '/',
        urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True))), ''))
    return {'hostname': host, 'url_fingerprint': hashlib.sha256(canonical.encode()).hexdigest()}


def _custody_checked_http(http, ready):
    def checked(*args, **kwargs):
        if not ready():
            raise RuntimeError('Solana custody identity unverified')
        return http(*args, **kwargs)
    return checked


def build_real_context(*, http=None, own_sidecar=True, allow_shyft_unavailable=False):
    """Assemble the real leg, or None if disarmed / creds missing (-> paper runner unchanged)."""
    if not cfg.MEMECOIN_REAL_ENABLED:
        return None
    keypair_path = cfg.MEMECOIN_SOLANA_KEYPAIR
    rpc_urls = cfg.solana_funded_rpc_urls()
    rpc_url = rpc_urls[0] if rpc_urls else ""
    fallback_rpc_url = rpc_urls[-1] if rpc_urls else ""
    if not rpc_url or not (keypair_path and os.path.exists(keypair_path)):
        logger.warning("memecoin real leg armed but creds missing (keypair=%s rpc=%s) -> real leg OFF",
                       bool(keypair_path and os.path.exists(keypair_path)), bool(rpc_url))
        return None
    from sniper.services.memecoin_solana_provider import infra_provider, provider_urls
    shyft = infra_provider() == "shyft"
    from sniper.settings.memecoin_submission import solana_submit_config, _validate_solana
    _submit_cfg = solana_submit_config()
    _validate_solana(_submit_cfg)
    read_rpc_url = provider_urls("read")[0] if shyft else cfg.solana_read_rpc()
    tpu_rpc_url = None
    if _submit_cfg.mode == "auto_free" and own_sidecar:
        tpu_rpc_url = (provider_urls("tpu")[0] if shyft else
                       getattr(cfg, "MEMECOIN_TPU_RPC_URL", "") or fallback_rpc_url)
    from sniper.execution.solana_wallet import (
        load_keypair, sol_balance_read, token_balance, usdc_balance)
    from sniper.execution.solana_real_seams import build_real_solana_executor
    from sniper.execution.solana_token_safety import token_safety_ok

    kp = load_keypair(keypair_path)
    pubkey = str(kp.pubkey())
    # Shyft auto_free is custody + local TPU; legacy also includes official public RPC.
    from sniper.execution.submission_backends import build_solana_backend, register_client
    _rpc_pool = None
    _rpc_http = http
    _rpc_http_direct = None     # set only when a rotating pool is installed over the raw client
    _owns_rpc_http = False
    if len(rpc_urls) > 1:
        from sniper.execution.solana_rpc_pool import RotatingSolanaRpcHttp
        if _rpc_http is None:
            from sniper.execution.fast_http import PooledHttpClient, warm_keepalive_expiry
            _client = register_client(PooledHttpClient(
                read_timeout_s=_submit_cfg.timeout_ms / 1000.0,
                keepalive_expiry_s=warm_keepalive_expiry(
                    _submit_cfg.warm_interval_s, _submit_cfg.timeout_ms / 1000.0,
                    serial_calls=len(rpc_urls) + (0 if shyft else 2))))
            _rpc_http = _client.request
            _owns_rpc_http = True
        # Keep a handle on the UNROTATED client. The reservation-recovery proof read must not be
        # able to answer from a second endpoint: a null from a node that never saw the signature
        # releases a reservation just as a null from the custody node does. See _confirm_once.
        _rpc_http_direct = _rpc_http
        # A funded key out of credit pages HELIUS-CREDITS-EXHAUSTED on its own thread; the read
        # that found it returns at once (it may be on the entry path).
        from sniper.services.memecoin_research_rpc import funded_credit_page
        _rpc_pool = RotatingSolanaRpcHttp(_rpc_http, rpc_urls,
                                          on_credit_exhausted=funded_credit_page())
        _rpc_http = _rpc_pool
    # Sidecar resolution failure leaves RPC delivery available; invalid role config failed above.
    _tpu_sidecar = None
    _tpu_reason = "not_owned" if not own_sidecar else "not_configured"
    if _submit_cfg.mode == "auto_free" and own_sidecar:
        from sniper.execution.solana_tpu_sidecar import resolve_tpu_sidecar
        try:
            _tpu_resolution = resolve_tpu_sidecar(rpc_url=tpu_rpc_url)
            _tpu_sidecar = _tpu_resolution.process
            _tpu_reason = _tpu_resolution.reason
        except Exception as exc:
            logger.warning("memecoin TPU sidecar resolver exception class=%s",
                           type(exc).__name__)
            _tpu_sidecar = None
            _tpu_reason = "resolver_exception"
    _submit_transport, _fast_tx_builder = build_solana_backend(
        keypair=kp, custody_rpc_url=rpc_url, http=_rpc_http, tpu_sidecar=_tpu_sidecar,
        warm_external_http=_owns_rpc_http,
        **({'allow_unavailable_custody': True} if allow_shyft_unavailable and shyft else {}))
    custody_ready = getattr(_submit_transport, 'custody_ready_fn', lambda: True)
    if allow_shyft_unavailable and shyft and _submit_transport is not None:
        _rpc_http = _custody_checked_http(_rpc_http or _submit_transport.custody_http, custody_ready)
        if _rpc_http_direct is not None:
            _rpc_http_direct = _custody_checked_http(_rpc_http_direct, custody_ready)
    from sniper.services.memecoin_entry_bids import start_entry_bids
    entry_bids = start_entry_bids(read_rpc_url)

    def select_entry_bid(**kwargs):
        entry_bids.register(kwargs['pool'])
        return entry_bids.select(**kwargs)

    ex = build_real_solana_executor(
        keypair=kp, rpc_url=rpc_url, http=_rpc_http, submission_transport=_submit_transport,
        fast_tx_builder=_fast_tx_builder, backend_label=_submit_cfg.mode,
        proof_http=_rpc_http_direct, read_rpc_url=read_rpc_url, entry_bids=entry_bids)

    from sniper.execution.solana_wallet import UsdcBankrollRead
    bankroll = UsdcBankrollRead(pubkey, rpc_url, http=_rpc_http)

    def usdc_fn():
        """Bankroll read: None on failure, NEVER a confident zero. `token_balance` collapses every
        failure to 0.0, so a transient RPC fault (429, cold pool during a restart) read a funded
        wallet as $0 and the risk gate refused with "insufficient bankroll" — a confident wrong
        reason for "we could not see". Live 2026-08-26 14:57Z: the operator's manual entry was
        refused mid-restart with $108 in the wallet. Same class as the gas top-up unwrap
        (0d45c97a) and the SOL side, which already refuses honestly on None."""
        # The wallet's USDC account alone while the last owner scan (<= 5 min) found only it; the
        # scan otherwise and on any miss (UsdcBankrollRead, 2026-10-07).
        try:
            return bankroll.read()
        except Exception as exc:
            # Stamped and with its cause: this line reaches runner.log through logging's last-resort
            # handler, which prints the bare message, so without both it could not be placed in time
            # or explained (2026-09-27: the cause was the funded pool having no healthy endpoint).
            logger.warning("%s usdc balance read failed; reporting unknown, not zero: %s",
                           datetime.now(timezone.utc).isoformat(), repr(exc)[:200])
            return None
    usdc_fn.take_stats = bankroll.take_stats

    def sol_fn():
        return sol_balance_read(pubkey, rpc_url, http=_rpc_http)

    def token_fn(mint):
        return token_balance(pubkey, mint, rpc_url, http=_rpc_http)

    def token_checked_fn(mint):
        # raises on a failed read instead of reporting zero — reconciliation only
        from sniper.execution.solana_wallet import token_balance_checked
        return token_balance_checked(pubkey, mint, rpc_url, http=_rpc_http)

    def block_height_fn():
        # READ rpc only, and confirmed commitment: a processed-commitment height could report the
        # chain past a blockhash window that has not actually closed for a committed transaction.
        return read_block_height(rpc_url, _rpc_http, read_url=read_rpc_url)

    def mint_read_fn(mint):
        # The token-safety mint read, callable ahead of the band (MEMECOIN_SOLANA_BAND_MCAP_BASIS).
        from sniper.execution.solana_token_safety import read_mint_account
        return read_mint_account(mint, rpc_url, _rpc_http)

    def token_safety_fn(mint, pre_read=None):
        if pre_read is not None:
            # The same attempt already read this mint for its band: judge that read, never a
            # second one. Identical verdict (tests/test_memecoin_band_mcap_basis.py).
            from sniper.execution.solana_token_safety import token_safety_verdict
            return token_safety_verdict(
                *pre_read,
                max_transfer_fee_bps=cfg.MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS,
                allow_live_fee_authority=bool(
                    getattr(cfg, "MEMECOIN_SOLANA_ALLOW_LIVE_TRANSFER_FEE_AUTHORITY", False)))
        return token_safety_ok(
            mint, rpc_url,
            max_transfer_fee_bps=cfg.MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS,
            http=_rpc_http)

    # Serialize every real execution on this wallet behind a cross-process lock — the runner main
    # thread, the follow-activity thread, and the web process all execute against this same Solana
    # wallet and price fills by whole-wallet balance delta; overlap corrupts the delta and can defeat
    # the daily-loss breaker (2026-07-15 review, CRITICAL-1). Key by pubkey (Solana's own balance).
    from sniper.execution.exec_lock import serialize_executor
    from sniper.execution import solana_real_seams as _srs
    from sniper.services import memecoin_exit_rebroadcast as _reoffer
    from sniper.services.memecoin_exit_rebroadcast import (
        short_timeout_http as _short_timeout_http,
        stored_payload_sender as _stored_payload_sender)

    def maintenance_call_fn(url, method, params):
        status, result = _srs._rpc_call(url, method, params, _rpc_http)
        if status != 200 or not isinstance(result, dict) or result.get("error") is not None:
            raise RuntimeError("withdrawal RPC read unavailable")
        return result

    def maintenance_send_fn(signed, url, **kwargs):
        return _srs.rpc_send(signed, url, http=_rpc_http, **kwargs)

    def maintenance_confirm_fn(signature, url, **kwargs):
        if not custody_ready():
            return 'unavailable'
        return _srs.rpc_confirm(signature, url, http=_rpc_http, **kwargs)

    def withdraw_send_factory_fn(conn, intent_id):
        if _submit_transport is None:
            return lambda signed, **_kw: _srs.rpc_send(
                signed, rpc_url, http=_rpc_http, preflight_commitment="confirmed",
                classify_preflight=True)
        from sniper.execution.submission_wiring import make_solana_send_fn
        from sniper.util.clock import real_utc_now_iso
        return make_solana_send_fn(
            _submit_transport, conn=conn, action="withdraw", owner_kind="withdraw_intent",
            owner_id=intent_id, now_fn=real_utc_now_iso, backend_label=_submit_cfg.mode,
            persist=False, record_async=True, preflight_commitment="confirmed")

    _k = "sol-%s" % pubkey
    provider_roles = {}
    if shyft:
        actual_tpu_url = getattr(_tpu_sidecar, '_rpc_url', None)
        provider_roles['provider_roles'] = {
            'provider': 'shyft', 'funded': [_provider_role_identity(u) for u in rpc_urls],
            'read': _provider_role_identity(read_rpc_url),
            'tpu': ({**_provider_role_identity(actual_tpu_url), 'constructed': True}
                    if actual_tpu_url else None)}
    volume_cache = None
    try:
        from sniper.services.memecoin_solana_volume_cache import start_volume_cache
        volume_cache = start_volume_cache(own_owners=(pubkey,))
    except Exception:
        logger.exception("Solana background volume cache unavailable; coverage stays unknown")
    return {**provider_roles, "custody_ready_fn": custody_ready, "execute_fn": serialize_executor(ex["execute_fn"], _k, withdrawal_action="entry"),
            "exit_fn": serialize_executor(
                ex["exit_fn"], _k, release_unsigned_close_on_error=True, withdrawal_action="exit"),
            "derisk_fn": (serialize_executor(ex["derisk_fn"], _k, withdrawal_action="derisk")
                          if ex.get("derisk_fn") else None),
            "source_partial_fn": (serialize_executor(ex["source_partial_fn"], _k, withdrawal_action="derisk")
                                  if ex.get("source_partial_fn") else None),
            "source_partial_receipt_fn": ex.get("source_partial_receipt_fn"),
            "source_partial_snapshot_fn": ex.get("source_partial_snapshot_fn"),
            "source_partial_proof_snapshot_fn": ex.get("source_partial_proof_snapshot_fn"),
            "preview_exit_fn": ex.get("preview_exit_fn"),   # read-only quote, no balance mutation
            "set_native_exit_hint_fn": ex.get("set_native_exit_hint_fn"),
            # Token decimals (getTokenSupply, cached per mint), read only to value a bag whose ledger
            # has an exit of unknown quantity (_executable_unit_price).
            "decimals_fn": lambda mint: _srs.token_supply(mint, rpc_url, http=_rpc_http)[1],
            # Read-only exact-balance exit quote; the other-holdings sell signs it under the lock.
            "exit_quote_fn": ex.get("exit_quote_fn"),
            "confirm_fn": ex["confirm_fn"], "confirm_once_fn": ex["confirm_once_fn"],
            # The close recovery's pinned re-offer: the executor's own send seam, fed a sell's
            # stored bytes. Unused unless "solana" is in MEMECOIN_PINNED_CLOSE_REOFFER_CHAINS.
            # Its own short timeout, and the node's words for a refused copy.
            "reoffer_send_fn": _stored_payload_sender(
                lambda signed: _srs.rpc_send(
                    signed, rpc_url, http=_short_timeout_http(_rpc_http or _srs._http)),
                reason_fn=_srs.take_send_error),
            # The copy's own receipt read: one getSignatureStatuses, capped like its send.
            "reoffer_confirm_once_fn": lambda sig: _srs.rpc_confirm(
                sig, rpc_url, max_attempts=1, poll_s=0, http=_short_timeout_http(
                    _rpc_http or _srs._http, _reoffer.RECEIPT_TIMEOUT_S)),
            # Pinned archival read; recovery uses it ONLY to corroborate a not_found. See
            # make_confirm_proof_for and _confirm_proof_once.
            "confirm_proof_once_fn": ex.get("confirm_proof_once_fn"),
            "recover_confirmed_entry_fn": ex.get("recover_confirmed_entry_fn"),
            "receipt_proceeds_fn": ex.get("receipt_proceeds_fn"),
            # Read-only: the early entry quote (memecoin_early_quote) runs these off the lock.
            "entry_quote_fn": ex.get("entry_quote_fn"),
            "native_shadow_start_fn": ex.get("native_shadow_start_fn"),
            # SLOT LANE (2026-09-26). Unwrapped on purpose: the lane takes the same wallet lock
            # itself, NON-blocking, before it reserves (memecoin_slot_lane.SlotLane.handle).
            "slot_execute_fn": ex.get("slot_execute_fn"), "slot_lock_key": _k,
            "entry_slot_guard_ready": ex.get("entry_slot_guard_ready") is True,
            "slot_deferred_ready": bool(ex.get("slot_deferred_ready")),
            # Sender A/B (2026-10-01): True only when the transport holds the Sender provider.
            "sender_ab_ready": bool(ex.get("sender_ab_ready")),
            "nozomi_ab_ready": bool(ex.get("nozomi_ab_ready")),
            "entry_bid_fn": select_entry_bid if entry_bids is not None else None,
            "entry_tip_fn": entry_bids.tip_lamports if entry_bids is not None else None,
            "entry_bid_status_fn": entry_bids.status if entry_bids is not None else None,
            "latest_blockhash_fn": lambda: latest_blockhash(rpc_url, _rpc_http),
            "token_safety_fn": token_safety_fn, "mint_read_fn": mint_read_fn,
            "usdc_fn": usdc_fn, "sol_fn": sol_fn,
            "token_fn": token_fn, "token_checked_fn": token_checked_fn,
            # Read-only chain height, for proving a de-risk signature is past its blockhash window.
            "block_height_fn": block_height_fn,
            "read_rpc_url": read_rpc_url,
            "withdraw_rpc_url": rpc_url,
            "withdraw_call_fn": lambda _url, method, params: maintenance_call_fn(rpc_url, method, params),
            "rpc_call_fn": maintenance_call_fn,
            "withdraw_send_factory_fn": withdraw_send_factory_fn,
            "rpc_send_fn": maintenance_send_fn, "rpc_confirm_fn": maintenance_confirm_fn,
            "pubkey": pubkey, "kill_path": None,
            "solana_volume_cache": volume_cache,
            "submission_backend": _submit_cfg.mode,
            "rpc_pool_status_fn": (_rpc_pool.snapshot if _rpc_pool is not None else
                                   lambda: {"active_index": 0, "endpoint_count": 1,
                                            "exhausted_count": 0,
                                            "fallback_active": not shyft}),
            "tpu_status_fn": lambda: _tpu_status(_tpu_sidecar, _tpu_reason)}


def _tpu_status(process, fallback_reason):
    base = {
        "configured": process is not None,
        "ready": False,
        "running": False,
        "restart_count": 0,
        "leader_count": None,
        "last_health_age_s": None,
        "reason": str(fallback_reason or "not_configured"),
    }
    if process is None:
        return base
    try:
        raw = process.sanitized_status()
    except Exception:
        return {**base, "configured": True, "reason": "status_unavailable"}
    ready = bool(raw.get("ready"))
    return {
        "configured": True,
        "ready": ready,
        "running": bool(raw.get("running")),
        "restart_count": int(raw.get("restart_count") or 0),
        "leader_count": raw.get("leader_count"),
        "last_health_age_s": raw.get("last_health_age_s"),
        "reason": "ready" if ready else str(raw.get("reason") or fallback_reason or "unavailable"),
    }


def tpu_status_for_contexts(contexts):
    solana = (contexts or {}).get("solana") or {}
    status_fn = solana.get("tpu_status_fn")
    if callable(status_fn):
        try:
            return status_fn()
        except Exception:
            pass
    return _tpu_status(None, "not_configured")


def _rpc_pool_status(raw=None):
    """Project the pool snapshot onto the heartbeat.

    THIS IS A WHITELIST, so a field added to `RotatingSolanaRpcHttp.snapshot()` does NOT appear in
    the heartbeat until it is named here. Found 2026-08-25: `degraded`, `blocked_indexes`,
    `last_reason` and `last_rotation_at` were added to the snapshot so an operator could answer "are
    we on the fastest path", and this projection silently dropped all four -- the heartbeat kept
    reporting the same four counters it always had, which is exactly the blindness the new fields
    were written to remove.

    `last_rotation_at` is a `time.monotonic()` stamp and is meaningless outside this process, so it
    is emitted as an AGE in seconds instead of the raw value.
    """
    import time as _time
    raw = raw or {}
    blocked = raw.get("blocked_indexes") or []
    try:
        blocked = [int(i) for i in blocked]
    except (TypeError, ValueError):
        blocked = []
    rotated_at = raw.get("last_rotation_at")
    try:
        rotation_age_s = (round(_time.monotonic() - float(rotated_at), 1)
                          if rotated_at is not None else None)
    except (TypeError, ValueError):
        rotation_age_s = None
    reason = raw.get("last_reason")
    return {
        "active_index": max(0, int(raw.get("active_index") or 0)),
        "endpoint_count": max(0, int(raw.get("endpoint_count") or 0)),
        "exhausted_count": max(0, int(raw.get("exhausted_count") or 0)),
        "fallback_active": bool(raw.get("fallback_active", False)),
        # not on the primary endpoint -> the operator's actual question
        "degraded": bool(raw.get("degraded", False)),
        "blocked_indexes": blocked,
        "last_reason": (None if reason is None else str(reason)),
        "rotation_age_s": rotation_age_s,
        # The call counter (spec 2026-10-01 burst read coalescing §5). Telemetry only. UNITS DIFFER:
        # calls_by_label {label: {method: n}} since process start counts JSON-RPC METHODS (a batch
        # counts once per member); calls_per_minute [[minute_epoch_s, {lane_class: n}]] for the
        # last hour counts HTTP REQUESTS, lane_class in entry/entry_preread/exit/other.
        # call_count_failures > 0 means counts were dropped: empty calls is then unknown, not zero.
        "calls_by_label": dict(raw.get("calls") or {}),
        "calls_per_minute": list(raw.get("minutes") or []),
        "call_count_failures": max(0, int(raw.get("count_failures") or 0)),
    }


def rpc_pool_status_for_contexts(contexts):
    solana = (contexts or {}).get("solana") or {}
    status_fn = solana.get("rpc_pool_status_fn")
    result = None
    if callable(status_fn):
        try:
            result = _rpc_pool_status(status_fn())
        except Exception:
            pass
    result = result if result is not None else _rpc_pool_status()
    if 'provider_roles' in solana:
        result['provider_roles'] = solana['provider_roles']
    if callable(solana.get('entry_bid_status_fn')):
        result['entry_bids'] = solana['entry_bid_status_fn']()
    return result


# --- multi-venue: contexts keyed by network_id ('solana' | chain id) ---

def entry_route_closed(contexts, chain):
    """True when an entry on `chain` must be dropped at the context gate, BEFORE any market read:
    no context, or a recovery-only context (a chain whose entries are not open but whose inventory
    still needs exits; Gamma MEDIUM on ee7bf1112, rule 5.2: on main such a buy cost zero reads)."""
    ctx = (contexts or {}).get(chain)
    return ctx is None or bool(isinstance(ctx, dict) and ctx.get("recovery_only"))


def _funded_evm_network_ids():
    from sniper.services.memecoin_chains import CHAINS
    return tuple(c["network_ids"][0] for c in CHAINS if c["funded"] and c["key"] != "solana")


def build_real_evm_contexts(*, http=None, conn=None, start_standing=False):
    """{chain_id: evm_context} for every allowlisted EVM chain that has creds (RPC + keypair), plus
    an exit-and-recovery context for every funded-capable EVM chain with open real inventory or an
    unresolved reservation, allowlisted or not (Gamma H3, 2026-09-30; Ink had this alone). A
    recovery context never re-opens entries: memecoin_evm_leg refuses a chain out of the allowlist.
    Empty when disarmed with nothing to recover, so the EVM leg is absent, never half-wired."""
    recovery = set()
    if conn is not None:
        recovery = m.evm_recovery_chains(conn, _funded_evm_network_ids())
    if not cfg.MEMECOIN_REAL_ENABLED and not recovery:
        return {}
    from sniper.services.memecoin_evm_leg import build_real_evm_context
    out = {}
    chains = set(cfg.MEMECOIN_EVM_ALLOWLIST if cfg.MEMECOIN_REAL_ENABLED else ())
    chains |= recovery
    for chain in sorted(chains):
        chain = str(chain)
        # ENTRIES OPEN only when funded trading is on, the chain is allowlisted AND its execution
        # venue is direct. Anything else with inventory gets a recovery-only context (Gamma HIGH on
        # ee7bf1112: for_recovery skips the venue switch, so an allowlisted chain with its venue
        # disabled and a bag open would have traded again). The runner's context gate refuses a
        # recovery-only context before any read (`entry_route_closed`).
        entry_open = (cfg.MEMECOIN_REAL_ENABLED and chain in cfg.MEMECOIN_EVM_ALLOWLIST
                      and cfg.memecoin_evm_exec_venue(chain) == "direct")
        recovering = chain in recovery and not entry_open
        if (str(chain) == "57073" and cfg.MEMECOIN_REAL_ENABLED
                and str(chain) in cfg.MEMECOIN_EVM_ALLOWLIST):
            reason = cfg.ink_capability_reason()
            if reason:
                raise RuntimeError(f"57073 admission capability unavailable: {reason}")
        ctx = build_real_evm_context(chain, http=http, conn=conn, start_standing=start_standing,
                                     **({"for_recovery": True} if recovering else {}))
        if ctx is not None:
            if recovering:
                ctx = {**ctx, "recovery_only": True}
            out[str(chain)] = ctx
            continue
        if str(chain) == "57073":
            raise RuntimeError("57073 context missing for enabled admission or inventory recovery")
        if recovering:
            # Open inventory with no executor. PAGE AND CONTINUE, never raise (Gamma, retracting
            # the raise on ee7bf1112): a crash-loop would take every other chain's exits dark,
            # Solana's included, to protect one chain's bag.
            logger.error("no recovery context for chain %s with open inventory: its exits and "
                         "recovery are dark until its RPC/keypair/V3 config is fixed", chain)
            try:
                from sniper.services.memecoin_alerts import send_alert
                send_alert("RECOVERY-GAP", f"chain {chain} holds open real inventory but no executor "
                           "could be built: its exits and recovery are dark")
            except Exception:
                logger.exception("recovery gap alert failed for chain %s", chain)
            continue
        # LOUD, NOT SILENT (2026-09-16). An allowlisted chain that cannot build a context used
        # to just produce zero entries with no signal anywhere pointing at why. Diagnostic-only
        # reads (no side effect, no change to what build_real_evm_context itself decided).
        chain_s = str(chain)
        if cfg.memecoin_evm_exec_venue(chain_s) != "direct":
            why = "no execution venue (MEMECOIN_EVM_EXEC_VENUE_%s is not 'direct')" % chain_s
        elif cfg.memecoin_evm_v3_addresses(chain_s) is None:
            why = "no V3 router/quoter configured"
        else:
            why = "credentials missing (RPC or keypair)"
        logger.warning("no execution path for chain %s (%s) — 0 entries will fire on it",
                       chain_s, why)
    return out


def build_all_contexts(*, http=None, own_sidecar=True, conn=None, allow_shyft_unavailable=False,
                       start_standing=False):
    """The venue map: 'solana' -> Solana context (if configured) + each configured EVM chain. Empty
    dict = real leg fully off (paper unchanged). Solana-only = {'solana': rc} (identical to before)."""
    cfg.validate_real_exit_policy()
    # Submission config gate: pooled RPC defaults need no new credentials; accelerated third-party
    # modes refuse startup rather than silently degrading.
    from sniper.settings.memecoin_submission import validate_submission_config
    validate_submission_config(funded_evm_chains=cfg.MEMECOIN_EVM_ALLOWLIST)
    contexts = {}
    sol = build_real_context(http=http, own_sidecar=own_sidecar,
        **({'allow_shyft_unavailable': True} if allow_shyft_unavailable else {}))
    if sol is not None:
        contexts["solana"] = sol
    contexts.update(build_real_evm_contexts(http=http, conn=conn, start_standing=start_standing))
    return contexts


def lifi_status_for_contexts(conn, contexts, now_iso):
    """Build the sanitized LI.FI state from runner-owned objects."""
    from sniper.execution.lifi_route import QuotaGuard

    try:
        _manifest, manifest_hash = cfg.load_lifi_manifest()
    except RuntimeError:
        manifest_hash = None
    try:
        mode = cfg.memecoin_lifi_mode()
    except RuntimeError:
        mode = "invalid"
    venue = (contexts or {}).get("4663") or {}
    recovery = venue.get("lifi_recovery_context") or {}
    custody = str(venue.get("address") or recovery.get("custody_wallet") or "").lower() or None
    guard = recovery.get("quota_guard")
    remaining = getattr(guard, "remaining", recovery.get("remaining"))
    in_flight = int(getattr(guard, "in_flight", recovery.get("in_flight", 0)) or 0)
    effective_limit = getattr(guard, "limit", recovery.get("effective_limit"))
    rows = m.unresolved_lifi_lifecycles(conn, "4663", custody) if custody else []
    states = [str(row.get("state") or "") for row in rows]
    slots_used = m.lifi_slots_used(conn, "4663", custody) if custody else 0
    slot_limit = 0
    if effective_limit is not None:
        try:
            while slot_limit < 100 \
                    and int(effective_limit) >= QuotaGuard.exit_reserve(slot_limit + 1) + 4:
                slot_limit += 1
        except (TypeError, ValueError):
            slot_limit = 0
    reasons = []
    authenticated = mode == "authenticated_funded" and bool(recovery.get("authenticated"))
    if mode == "disabled":
        reasons.append("disabled")
    elif mode == "public_probe":
        reasons.append("public_mode")
    else:
        try:
            cfg.validate_lifi_config(conn=conn, custody_wallet=custody)
        except RuntimeError:
            reasons.append("configuration_invalid")
        if not venue:
            reasons.append("no_4663_context")
        if not authenticated:
            reasons.append("unauthenticated")
        snapshot = recovery.get("recovery_snapshot")
        if snapshot is None or not snapshot.scan_complete:
            reasons.append("recovery_incomplete")
        if remaining is None or effective_limit is None:
            reasons.append("quota_unknown")
        if any(state.startswith("approval_") for state in states):
            reasons.append("unresolved_approval")
        if any(state.startswith("swap_") for state in states):
            reasons.append("unresolved_swap")
        if any(state.startswith("revoke_") for state in states):
            reasons.append("unresolved_revoke")
        if slot_limit and slots_used >= slot_limit:
            reasons.append("slot_limit")
        if isinstance(guard, QuotaGuard) \
                and not guard.admit_entry(slots_used, in_flight=in_flight).allowed:
            reasons.append("insufficient_quota")
    return {
        "corrected_at": now_iso,
        "runner_heartbeat_at": now_iso,
        "release_sha": str(recovery.get("release_sha") or os.getenv(
            "MEMECOIN_LIFI_ACTIVE_RELEASE_SHA", "")),
        "manifest_hash": manifest_hash,
        "chain_id": "4663",
        "custody_wallet": custody,
        "mode": mode,
        "authenticated": authenticated,
        "effective_limit": effective_limit,
        "remaining": remaining,
        "reset_s": getattr(guard, "reset", recovery.get("reset_s")),
        "in_flight": in_flight,
        "exit_reserve": QuotaGuard.exit_reserve(slots_used),
        "slots_used": slots_used,
        "slot_limit": slot_limit,
        "entry_admitted": not reasons,
        "disable_reasons": list(dict.fromkeys(reasons)),
        "unresolved_approvals": sum(state.startswith("approval_") for state in states),
        "unresolved_swaps": sum(state.startswith("swap_") for state in states),
        "unresolved_revokes": sum(state.startswith("revoke_") for state in states),
        "unresolved_finality": sum(state in {
            "approval_signed", "approval_submitted", "approval_submitted_unknown",
            "swap_signed", "swap_submitted", "swap_submitted_unknown",
            "revoke_signed", "revoke_submitted", "revoke_submitted_unknown",
        } for state in states),
    }


def lifi_alert_baseline_ready(contexts, status):
    """Return true when LI.FI startup has a stable state for alert comparison."""
    reasons = set((status or {}).get("disable_reasons") or ())
    if reasons & {"configuration_invalid", "no_4663_context"}:
        return True
    if (status or {}).get("mode") != "authenticated_funded":
        return False
    recovery = ((contexts or {}).get("4663") or {}).get("lifi_recovery_context") or {}
    return bool(recovery.get("recovery_bootstrap_complete"))


def lifi_recovery_loop(
        context, heartbeat_ready, *, connection_factory, now_fn,
        stop_event=None, sleep_fn=time.sleep, period_s=5.0):
    """Run bounded LI.FI recovery only after the first runner heartbeat."""
    from sniper.execution.lifi_executor import recover_lifi_lifecycles

    heartbeat_ready.wait()
    authenticate = context.get("authenticate_fn")
    next_authentication_probe = 0.0
    first_pass = True
    while stop_event is None or not stop_event.is_set():
        if (callable(authenticate) and not context.get("authentication_probe_complete")
                and time.monotonic() >= next_authentication_probe):
            # Retry only in this background worker; never make an entry wait for authentication.
            next_authentication_probe = time.monotonic() + 60.0
            try:
                context["authentication_probe_complete"] = bool(authenticate())
            except Exception:
                logger.exception("LI.FI authentication probe failed")
        conn = None
        try:
            conn = connection_factory()
            context["recovery_snapshot"] = recover_lifi_lifecycles(
                conn, context, now_fn())
        except Exception:
            if conn is not None:
                conn.rollback()
            logger.exception("LI.FI recovery pass failed")
        finally:
            if conn is not None:
                conn.close()
            if first_pass:
                context["recovery_bootstrap_complete"] = True
                first_pass = False
        if stop_event is not None and stop_event.is_set():
            break
        sleep_fn(period_s)


def start_lifi_recovery_worker(
        contexts, heartbeat_ready, *, connection_factory, now_fn,
        thread_factory=threading.Thread):
    """Start one recovery worker without a startup wait."""
    context = ((contexts or {}).get("4663") or {}).get("lifi_recovery_context")
    if context is None:
        return False
    thread_factory(
        target=lifi_recovery_loop,
        kwargs={
            "context": context,
            "heartbeat_ready": heartbeat_ready,
            "connection_factory": connection_factory,
            "now_fn": now_fn,
        },
        name="memecoin-lifi-recovery",
        daemon=True,
    ).start()
    return True


def missing_evm_context_chains(contexts, *, allowlist=None):
    """Sorted EVM allowlist members that failed to build a funded context at startup."""
    expected = set(cfg.MEMECOIN_EVM_ALLOWLIST if allowlist is None else allowlist)
    present = {str(chain) for chain in (contexts or {}) if str(chain) != "solana"}
    return sorted((str(chain) for chain in expected if str(chain) not in present),
                  key=lambda value: (len(value), value))


def read_block_height(rpc_url, http=None, *, read_url=None, log_fn=None):
    """Confirmed block height for the dead-signature proof: pooled custody first, read endpoint next.

    THE PROOF WAS THE HAMMER (2026-09-09). `sig_provably_dead` asks for the height every sweep
    while a close is pinned. This read went to the custody endpoint UNPOOLED, the endpoint answered
    429, the proof threw, the claim stayed pinned, and the next sweep asked again: 1,102 of the
    1,233 HTTP 429s in two hours were this one call, and fries (55293) sat un-exitable behind a
    close that had provably expired 5,000 blocks earlier. A height is a public fact; any healthy
    node can answer it. A failure still returns None -- the proof stays fail-closed -- but it is
    now a failure of every endpoint, not of one rate limit.
    """
    from sniper.execution.evm_wallet import _rpc as _json_rpc
    from sniper.settings.memecoin import solana_read_rpc
    params = [{"commitment": "confirmed"}]
    urls = [(rpc_url, http)]
    fallback = read_url if read_url is not None else solana_read_rpc()
    if fallback and fallback != rpc_url:
        urls.append((fallback, None))
    for i, (url, seam) in enumerate(urls):
        try:
            result = _json_rpc(url, "getBlockHeight", params, http=seam) if seam is not None \
                else _json_rpc(url, "getBlockHeight", params)
            value = (result or {}).get("result")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if i and log_fn:
                    log_fn("block height read from the read endpoint after custody failed")
                return value
        except Exception as exc:
            if log_fn:
                log_fn("block height read failed on %s: %s" % (
                    (url or "").split("//")[-1].split("/")[0], type(exc).__name__))
            continue
    return None


def latest_blockhash(rpc_url, http=None):
    """(blockhash, last_valid_block_height) at `confirmed` from the pooled custody RPC, or None.

    For the slot lane's prefetcher, never an entry path: the lane refuses on a missing or old
    blockhash instead of reading one while a signal waits."""
    from sniper.execution.evm_wallet import _rpc as _json_rpc
    try:
        result = _json_rpc(rpc_url, "getLatestBlockhash", [{"commitment": "confirmed"}], http=http)
        value = ((result or {}).get("result") or {}).get("value") or {}
        blockhash, height = value.get("blockhash"), value.get("lastValidBlockHeight")
        if isinstance(blockhash, str) and blockhash and isinstance(height, int):
            return blockhash, height
    except Exception:
        logger.warning("slot lane blockhash read failed", exc_info=True)
    return None


def context_for(contexts, network_id):
    """Route by network_id ('solana' | chain id). None if that venue isn't configured — the caller
    then SKIPS + logs, never handing the position to the wrong leg."""
    return (contexts or {}).get(network_id or "solana")


def context_for_position(contexts, pos):
    """Route a POSITION to its venue with a defensive mint<->network cross-check: an 0x mint tagged
    'solana' (or a base58 mint tagged an EVM chain) is data corruption — refuse to route (fail-closed)
    rather than hand an 0x mint to the Solana executor. Makes the no-cross-venue property self-defending
    instead of purely dependent on the write-time invariant."""
    network_id = m.normalized_coin_network(pos.get("network_id"), pos.get("mint"))
    is_evm_mint = (pos.get("mint") or "").startswith("0x")
    routes_solana = network_id == "solana"
    if is_evm_mint == routes_solana:   # 0x->solana or base58->evm = mismatch
        logger.error("venue/mint mismatch: mint=%s network=%s pos=%s — refusing to route (fail-closed)",
                     pos.get("mint"), network_id, pos.get("id"))
        return None
    return context_for(contexts, network_id)


_GATE_BALANCE_TTL_S = float(os.getenv("MEMECOIN_GATE_BALANCE_TTL_S", "30") or 30)
# THE SOLANA GATE READ IS BOUNDED (2026-09-27). An unreadable USDC balance is now a transient miss,
# and the retry lane re-runs the gate INLINE on the main loop every poll. At 12:52Z one gate spent
# ~14 s in the hook while the funded pool had no healthy endpoint; repeated, that stalls the loop
# the watchdog kills. A read past this deadline is unknown (None). Its daemon thread is abandoned,
# not awaited: it ends when its HTTP call returns (the pool's own bound, ~14 s seen), so with the
# lane's 6 s cadence at most ~3 abandoned reads are alive at once, and none of them ever writes the
# gate cache. The healthy read's p90 is ~110 ms (Alpha's figure). 0 = no deadline. EVM gates stay unbounded: their
# 4663 reads (ETH/USD plus two chain reads) legitimately ran ~2 s.
_GATE_BALANCE_DEADLINE_S = float(os.getenv("MEMECOIN_GATE_BALANCE_DEADLINE_S", "2"))
_READ_TIMED_OUT = object()
_gate_balance_cache = {}
_gate_balance_generation = {}
_gate_balance_lock = threading.Lock()


def _gate_balance_invalidate(chain, on_advance=None):
    """Drop a chain's cached pre-gate balances. Call this BEFORE any spend.

    The cache is only sound while the balance cannot have moved. We are the only routine
    spender, so dropping the cache before every outgoing transaction guarantees a served
    value never predates our own money leaving.

    It also advances the chain's GENERATION (2026-09-27). A read already in flight when the spend
    began returns a pre-spend value; without the generation it wrote that value into the cache
    AFTER this drop, and a failed read inside the TTL was then served money that had left.

    `on_advance(generation, stamp)` runs UNDER the generation lock (2026-10-15): the attempt that
    spends records its own advance atomically with it, so a reader checking `stale(own_fn=...)`
    can never see the new generation without seeing whose it is."""
    with _gate_balance_lock:
        _gate_balance_cache.pop(str(chain), None)
        _gate_balance_generation[str(chain)] = _gate_balance_generation.get(str(chain), 0) + 1
        generation = _gate_balance_generation[str(chain)]
        try:
            stamp = time.monotonic()
        except Exception:
            stamp = None
        if on_advance is not None:
            try:
                on_advance(generation, stamp)
            except Exception:
                pass                       # attribution only; the advance stands
        return generation, stamp


_QUOTE_BALANCE_UNAVAILABLE = "quote balance unavailable"


def _quote_balance_unavailable_reason(failures):
    """The gate's refusal when its quote balance is unknown, naming what the read met.

    The prefix is what the retry lane (`_is_transient_entry_miss`) and the triage scripts match.
    A 429 names `provider_rpc_rate_limit`, so the lane waits MEMECOIN_ENTRY_RETRY_RPC_429_S instead
    of re-asking the endpoint that just refused us (HALCYON, 2026-10-07: 9 attempts in 62 s)."""
    cls = (failures or {}).get("quote")
    if not cls:
        return _QUOTE_BALANCE_UNAVAILABLE
    if cls == "http_429":
        return "%s (provider_rpc_rate_limit: %s)" % (_QUOTE_BALANCE_UNAVAILABLE, cls)
    return "%s (%s)" % (_QUOTE_BALANCE_UNAVAILABLE, cls)


def _gate_balance_apply_cache(chain, kind, value, now_ts, generation=None, read_meta=None):
    """Store a good pre-gate balance, or serve a recent one when the read failed.

    A transient RPC failure used to refuse the entry outright ("quote balance unavailable").
    A failed read is an instrument failure, not a balance change: the balance does not move
    because we could not see it. Returns (value, served_from_cache). `generation` is the chain's
    generation when the read STARTED; a good value is cached only if no spend began since."""
    key = str(chain)
    if read_meta is not None:
        read_meta[kind] = None
    if value is not None:
        with _gate_balance_lock:
            if generation is None or _gate_balance_generation.get(key, 0) == generation:
                _gate_balance_cache.setdefault(key, {})[kind] = (value, now_ts)
        if read_meta is not None:
            read_meta[kind] = now_ts
        return value, False
    with _gate_balance_lock:
        entry = _gate_balance_cache.get(key, {}).get(kind)
    if entry is None:
        return None, False
    cached, ts = entry
    age = now_ts - ts
    if age > _GATE_BALANCE_TTL_S:
        return None, False
    if read_meta is not None:
        read_meta[kind] = ts
    logger.warning(
        "gate balance served from cache chain=%s kind=%s age=%.1fs value=%s "
        "— live read failed, balance cannot have moved since",
        chain, kind, age, cached)
    return cached, True


def _gate_balances(chain, usdc_fn, native_fn, now_ts=None, deadline_s=None, failures=None,
                   read_meta=None):
    """Read both pre-gate balances concurrently, with a short-TTL fallback per balance.

    Unlike _concurrent_reads this does NOT re-raise: at the gate a raising read and a None
    read both mean "unavailable", and both are eligible for the cache fallback. The executor's
    own pre/post balance reads must stay live and are deliberately NOT routed through here —
    they measure spend and receipt deltas, which a cached value would corrupt.

    `failures` (a dict the caller owns, one per attempt) receives what a failed read met, keyed
    "quote"/"native": the EVM read's own class (`evm_wallet.last_read_failure`, read back on the
    reader's thread right after the call) or the class of what it raised."""
    from sniper.execution.evm_wallet import (
        clear_read_failure, last_read_failure, read_failure_class)
    if now_ts is None:
        now_ts = time.monotonic()
    with _gate_balance_lock:
        generation = _gate_balance_generation.get(str(chain), 0)

    def _safe(fn, kind):
        def _run():
            clear_read_failure()
            try:
                value = fn()
            except Exception as exc:
                logger.warning("gate balance read raised chain=%s kind=%s", chain, kind,
                               exc_info=True)
                if failures is not None:
                    failures[kind] = read_failure_class(exc)
                return None
            if value is None and failures is not None:
                failures[kind] = last_read_failure()
            return value
        return _run

    quote_raw, native_raw = _concurrent_reads(_safe(usdc_fn, "quote"), _safe(native_fn, "native"),
                                              deadline_s=deadline_s)
    if quote_raw is _READ_TIMED_OUT or native_raw is _READ_TIMED_OUT:
        logger.warning("%s gate balance read timed out after %.1fs chain=%s kind=%s",
                       datetime.now(timezone.utc).isoformat(), float(deadline_s), chain,
                       ",".join(k for k, v in (("quote", quote_raw), ("native", native_raw))
                                if v is _READ_TIMED_OUT))
        quote_raw = None if quote_raw is _READ_TIMED_OUT else quote_raw
        native_raw = None if native_raw is _READ_TIMED_OUT else native_raw
    quote, quote_cached = _gate_balance_apply_cache(chain, "quote", quote_raw, now_ts, generation,
                                                    read_meta)
    native, native_cached = _gate_balance_apply_cache(chain, "native", native_raw, now_ts,
                                                      generation, read_meta)
    if read_meta is not None:
        stamps = [read_meta[k] for k in ("quote", "native") if read_meta[k] is not None]
        read_meta["started"] = min(stamps) if stamps else now_ts
    return quote, native, (quote_cached or native_cached)


def quote_price_warmer_status(contexts, chains=None):
    """{chain: the quote-price warmer's status} for each chain in `chains` (2026-10-15, Alpha):
    ready, last_error, thread_alive and age_s from the chain's own `quote_price_peek_fn.status`.
    56 was the only chain the heartbeat showed (fastlane.bsc_price_preparation); a chain without
    a context or a status reads unavailable, and a status that raises names the exception.
    `chains` None = MEMECOIN_EVM_QUOTE_PRICE_WARMER_CHAINS. NEVER RAISES: it is evaluated as a
    write_heartbeat argument, outside that function's own guard, so a raise here would skip the
    whole heartbeat; anything unexpected becomes {"_error": {"last_error": <exception class>}},
    the same per-key dict shape as a chain's status."""
    try:
        if chains is None:
            chains = cfg.memecoin_evm_quote_price_warmer_chains()
        out = {}
        for chain in sorted(str(c) for c in chains):
            status = {"ready": None, "last_error": "unavailable", "thread_alive": None, "age_s": None}
            ctx = context_for(contexts or {}, chain)
            peek = ctx.get("quote_price_peek_fn") if isinstance(ctx, dict) else None
            fn = getattr(peek, "status", None)
            if callable(fn):
                try:
                    status = dict(status, **fn())
                except Exception as exc:
                    status["last_error"] = type(exc).__name__
            out[chain] = status
        return out
    except Exception as exc:
        return {"_error": {"last_error": type(exc).__name__}}


class GateBalanceWarmer:
    """Keeps the gate balance cache of MEMECOIN_EVM_GATE_BALANCE_WARM_CHAINS fresh (2026-10-15).

    On a quiet chain the cache was older than MEMECOIN_EVM_EARLY_SIZING_CACHE_S (300 s) at nearly
    every signal, so the early quote sized on a fresh ~280 ms read and launched its ladder no
    sooner than the entry's own (6 of 29 4663 early quotes 10-12..10-15 were still sizing). This
    thread runs the gate's own read, `_gate_balances`, every MEMECOIN_EVM_GATE_BALANCE_WARM_S: the
    same generation guard decides whether a value is written (never when a spend began during the
    read), and a failed read writes nothing. Reads only; two custody calls per chain per pass."""

    def __init__(self, contexts_fn):
        self._contexts_fn = contexts_fn
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._stats = {"passes": 0, "reads_ok": 0, "reads_failed": 0, "last_pass_at": None,
                       "last_error": None}

    def run_once(self):
        chains = sorted(cfg.memecoin_evm_gate_balance_warm_chains())
        contexts = self._contexts_fn() if chains else None
        ok = failed = 0
        error = None
        for chain in chains:
            ctx = context_for(contexts or {}, chain)
            if (not isinstance(ctx, dict) or ctx.get("recovery_only")
                    or ctx.get("usdc_fn") is None or ctx.get("native_fn") is None):
                continue
            failures = {}
            try:
                with _rpc_label("gate_balance_warm:%s" % chain):
                    _gate_balances(chain, ctx["usdc_fn"], ctx["native_fn"], failures=failures)
            except Exception as exc:                  # _gate_balances does not raise; belt only
                failures["quote"] = failures["native"] = type(exc).__name__
                logger.exception("gate balance warm failed chain=%s", chain)
            bad = [kind for kind in ("quote", "native") if failures.get(kind)]
            failed += len(bad)
            ok += 2 - len(bad)
            if bad:
                error = "%s:%s" % (chain, ",".join("%s=%s" % (k, failures[k]) for k in bad))
        with self._lock:
            self._stats["passes"] += 1
            self._stats["reads_ok"] += ok
            self._stats["reads_failed"] += failed
            self._stats["last_pass_at"] = time.monotonic()
            if error is not None:
                self._stats["last_error"] = error

    def run_forever(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("gate balance warm pass failed")
            self._stop.wait(cfg.memecoin_evm_gate_balance_warm_s())

    def stop(self):
        self._stop.set()

    def status(self):
        with self._lock:
            out = dict(self._stats)
        at = out.pop("last_pass_at")
        out["last_pass_age_s"] = None if at is None else round(time.monotonic() - at, 1)
        out["chains"] = sorted(cfg.memecoin_evm_gate_balance_warm_chains())
        return out


def _concurrent_reads(*fns, deadline_s=None):
    """Run independent pre-gate balance reads at the same time and return their values in order.

    These are read-only RPC/HTTP probes with no ordering dependency, but they were evaluated as
    positional arguments — i.e. strictly sequentially — so the gate paid their sum. On 4663 that was
    a Binance ETH/USD read plus two chain reads at ~160ms each, all before the reservation was even
    attempted (measured reservation stage: 2084ms).

    Failure semantics are preserved EXACTLY: a raising read re-raises from this call just as it did
    when evaluated inline, and a read returning None still reaches the gate as None (which refuses
    with an honest reason rather than masquerading as a zero balance). Order of results is fixed, so
    a caller can never mix up which balance is which.

    With `deadline_s`, a read still running at the deadline is abandoned (daemon thread, result
    discarded) and its slot is `_READ_TIMED_OUT`; its error, if it ever raises, is never seen."""
    if not fns:
        return []
    box = [None] * len(fns)
    errors = [None] * len(fns)
    # The caller's funded RPC call counter label rides into the read threads (thread-local).
    try:
        label = _current_rpc_label()
    except Exception:
        label = "?"

    def _run(i, fn):
        try:
            with _rpc_label(label):
                box[i] = fn()
        except BaseException as exc:               # re-raised below on the caller's thread
            errors[i] = exc

    workers = [threading.Thread(target=_run, args=(i, fn), daemon=True,
                                name=f"entry-preread-{i}") for i, fn in enumerate(fns)]
    for w in workers:
        w.start()
    deadline = time.monotonic() + float(deadline_s) if deadline_s else None
    for w in workers:
        w.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
    late = {i for i, w in enumerate(workers) if w.is_alive()}
    for i, exc in enumerate(errors):
        if exc is not None and i not in late:
            raise exc
    return [_READ_TIMED_OUT if i in late else v for i, v in enumerate(box)]


class _EntryPreReads:
    """The Solana gate's bankroll read and the token-safety mint read, started beside the market
    data GET instead of after it (2026-09-27, Alpha GO "B").

    Measured on the Solana general path (2026-09-26T21:30Z..09-27T20:5xZ): the DexScreener GET
    starts at detection and costs p50 233 / p90 493 ms; only after it did the entry read the
    bankroll (p50 44 / p90 87 ms) and, after the reservation, the mint (22 / 43 ms). Neither read
    depends on the GET. The balance read is `_gate_balances` itself, so its deadline, cache
    fallback and generation guard are unchanged; its deadline counts from when it STARTS. Results
    are read only after `join`, on the thread that consumes them, and are bound to one attempt's
    trace so a re-hooked event object can never be served an older attempt's reads."""

    def __init__(self, sol, mint, trace, *, read_mint, chain="solana", native_key="sol_fn",
                 deadline_s=_GATE_BALANCE_DEADLINE_S, label="entry_preread:unknown"):
        # EVM (2026-09-27, Alpha GO "A"): the same object carries an EVM chain's gate reads, with
        # that chain's `native_fn` and no deadline, exactly as its in-line read ran. `generation`
        # is the chain's spend generation when the read STARTED; see `stale`.
        self.trace = trace
        self.chain = str(chain)
        self.mint = mint
        self._label = label                   # the funded RPC call counter's label for both reads
        self._native_key = native_key
        self._deadline_s = deadline_s
        with _gate_balance_lock:
            self.generation = _gate_balance_generation.get(self.chain, 0)
        self.started = time.monotonic()
        self.balance_read_started = self.started
        self.snapshot_debits_fn = sol.get("slot_snapshot_debits_fn")
        self._balances = None
        self._balance_read_ms = None
        self.failures = {}                    # what a failed balance read met; see _gate_balances
        self._mint = None
        self._mint_error = None
        self._threads = {"balances": threading.Thread(
            target=self._read_balances, args=(sol,), daemon=True, name="entry-early-balances")}
        if read_mint and sol.get("mint_read_fn") is not None:
            self._threads["mint"] = threading.Thread(
                target=self._read_mint, args=(sol["mint_read_fn"], mint), daemon=True,
                name="entry-early-mint")
        for worker in self._threads.values():
            worker.start()

    def _read_balances(self, sol):
        began = time.monotonic()
        read_meta = {}
        with _rpc_label(self._label):
            self._balances = _gate_balances(self.chain, sol["usdc_fn"], sol[self._native_key],
                                            deadline_s=self._deadline_s, failures=self.failures,
                                            read_meta=read_meta)
        self.balance_read_started = min(self.started, read_meta["started"])
        self._balance_read_ms = (time.monotonic() - began) * 1000.0

    def stale(self, *, evidence=None, own_fn=None):
        """True once one of our own spends on this chain began after the read started: every spend
        calls `_gate_balance_invalidate` first, which advances the generation.

        `own_fn` (2026-10-15, MEMECOIN_EVM_EARLY_SIZING_OWN_GEN): the attempt's own recorded advance,
        `(chain, generation, stamp)` or None, read under the same lock that recorded it. An advance
        that is EXACTLY that one -- read generation + 1, and the only spend since the read is this
        attempt's own entry -- is not stale for this attempt's early quote: its clip comes from the
        same pre-spend read, and the executor takes the quote only at that exact clip. Any other
        spend in between leaves it stale. 17 of 29 4663 early quotes (10-12..10-15) died here."""
        with _gate_balance_lock:
            current = _gate_balance_generation.get(self.chain, 0)
            own = None
            if own_fn is not None and cfg.memecoin_evm_early_sizing_own_gen():
                try:
                    own = own_fn()
                except Exception:
                    own = None
            own_only = bool(own and str(own[0]) == self.chain and own[1] == current
                            and current == self.generation + 1)
            if evidence is not None:
                evidence.update(pre_generation=self.generation, current_generation=current,
                                **({"own_generation_only": True} if own_only else {}))
            return current != self.generation and not own_only

    def _read_mint(self, read_fn, mint):
        try:
            with _rpc_label(self._label):
                self._mint = read_fn(mint)
        except Exception as exc:              # reported by the consumer, with the mint
            self._mint_error = exc

    def _joined(self, name):
        worker = self._threads.get(name)
        if worker is None:
            return False
        # The balance read ends by its own deadline; the slack covers thread scheduling. A mint
        # read still running past it is left to the caller's fresh read (today's path).
        worker.join(None if self._deadline_s is None else max(
            0.0, self.started + self._deadline_s + 0.25 - time.monotonic()))
        return not worker.is_alive()

    def balances(self):
        """(usdc, native, served_from_cache, read_ms), or None when the read never finished."""
        if not self._joined("balances"):
            return None
        return (*self._balances, self._balance_read_ms)

    def mint_read(self, mint):
        """The (value, failure) pair; None when the read raised; _READ_TIMED_OUT when it did not
        finish or never ran."""
        if mint != self.mint or not self._joined("mint"):
            return _READ_TIMED_OUT
        if self._mint_error is not None:
            logger.warning("early mint read raised mint=%s: %r", mint, self._mint_error)
            return None
        return self._mint


def _start_evm_post_trade_read(ev, chain, *, trace=None):
    """Start the J7 lane's source receipt read for the EVM chase reference, on its own 2-worker pool
    (spec 2026-09-28-memecoin-evm-chase-post-trade-reference-design.md). Nothing ever waits on it:
    each chase gate peeks, and a read that has not finished keeps the average fill. Only an EVM chain
    listed in MEMECOIN_REAL_CHASE_REFERENCE_EVM_CHAINS under post_trade, only a raybot origin (the
    on-chain lane's rows carry the watcher's decode instead), once per event. Never raises."""
    try:
        from sniper.services import memecoin_evm_post_trade as pt
        chain = str(chain or "solana")
        if chain == "solana" or not pt.enabled(chain) or pt.read_host(chain) is None:
            return None
        origin = pt.raybot_origin(getattr(ev, "fomo_event_id", None))
        if origin is None:
            return None
        tx_hash, trader, mint = origin
        ref = getattr(ev, "evm_chase_reference", None)
        if ref is None:
            ref = ev.evm_chase_reference = pt.EvmChaseReference(
                chain, future=pt.submit_receipt_reference(chain, tx_hash, mint, trader))
        if trace is not None and ref.chain == chain:
            trace.evm_receipt_timing = (chain, tx_hash, ref)
        return ref
    except Exception:
        logger.exception("EVM post-trade read not started for %s", getattr(ev, "mint", None))
        return None


def _evm_receipt_timing_late_best_effort(trace):
    """Retain an already-read source block; no wait, RPC or funded authority."""
    try:
        bound = getattr(trace, "evm_receipt_timing", None)
        if bound is None:
            return False
        chain, tx_hash, ref = bound
        payload = ref.payload()
        observation = (payload or {}).get("source_receipt_observation")
        if not isinstance(observation, dict):
            return False
        block = observation.get("source_block_number")
        if (observation.get("network_id") != chain or observation.get("source_tx") != tx_hash
                or type(block) is not int or block <= 0):
            return False
        return trace.mark_substage("evm_source_receipt_observation", observation=observation)
    except Exception:
        logger.exception("EVM receipt timing not recorded (funded result unchanged)")
        return False


def _start_entry_prereads(ev, contexts, trace, chain="solana"):
    """Start a chain's gate reads beside the market-data GET, bound to this attempt's `trace`.
    None means today's path: flag off, no trace to bind to, no balance seams for the chain, or an
    event that cannot carry the handle. EVM chains read `native_fn` with no deadline, as in line."""
    chain = str(chain or "solana")
    evm = chain != "solana"
    flag = "MEMECOIN_ENTRY_EARLY_GATE_READS_EVM" if evm else "MEMECOIN_ENTRY_EARLY_GATE_READS"
    if not bool(getattr(cfg, flag, False)) or trace is None:
        return None
    pre = _entry_prereads_for(ev, trace)
    if pre is not None:
        return pre
    native_key = "native_fn" if evm else "sol_fn"
    sol = context_for(contexts, chain)
    if not isinstance(sol, dict) or sol.get("usdc_fn") is None or sol.get(native_key) is None:
        return None
    try:
        ev.entry_prereads = None               # prove the event can carry it BEFORE any read starts
    except (AttributeError, TypeError):
        return None
    read_mint = not evm and bool(cfg.MEMECOIN_REAL_TOKEN_SAFETY_ENABLED
                                 or getattr(cfg, "MEMECOIN_SOLANA_BAND_MCAP_BASIS", "") == "onchain")
    ev.entry_prereads = _EntryPreReads(
        sol, ev.mint, trace, read_mint=read_mint, chain=chain, native_key=native_key,
        deadline_s=None if evm else _GATE_BALANCE_DEADLINE_S,
        label=_decision_label("entry_preread", ev))
    return ev.entry_prereads


def _early_quote_clip(ev, pre, db_path, *, with_alts=False):
    """(clip, None) when the risk envelope would approve this entry now, else (None, reason). Runs
    on the early quote's thread: the SAME envelope reserve_entry evaluates (build_memecoin_risk_
    context + memecoin_entry_allowed), on the gate's own early balances, at the market regime scale,
    on a read-only connection of its own. An estimate only: the executor uses the quote solely for
    the clip the reservation actually approved.

    with_alts (C1 clip_mismatch, Alpha 2026-10-07): (clip, None, {"alt_clips": [...]}). At a market
    scale of 1.0 a coin the classification later calls hot_narrow (it needs the GET) is reserved at
    MEMECOIN_FUNDED_REGIME_RISK_SCALE instead (15 of 17 C1 discards since 10-01, exactly 0.75 x); the
    same envelope at that scale names the clip, which the early quote also quotes. A refusal stays a
    2-tuple."""
    from sniper.execution.solana_wallet import native_sol_balance_value
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.services import memecoin_arming as _arming
    from sniper.services.memecoin_market_state import market_risk_scale
    from sniper.services.memecoin_risk import build_memecoin_risk_context, memecoin_entry_allowed
    from sniper.util.clock import real_utc_now_iso
    if not _arming.real_entries_allowed():
        return None, "disarmed"
    read = pre.balances()
    if read is None or read[0] is None:
        return None, "balance_unread"
    usdc, native = read[0], native_sol_balance_value(read[1])
    now_iso = real_utc_now_iso()
    ro = get_readonly_connection(db_path)
    try:
        scale = market_risk_scale(ro, decision_at=now_iso,
                                  signal_source=getattr(ev, "signal_source", None))
        market_scale = scale
        # The winner re-entry ladder scales the clip the reservation will approve; quoting the
        # full clip here would only buy a second quote at the executor.
        step = m.winner_reentry_step(ro, "solana", ev.mint, now_iso,
                                     signal_source=getattr(ev, "signal_source", None))
        if step is not None and step["factor"] is not None:
            scale *= step["factor"]
        # TRIAL ARM: the reservation caps an open trial's entry at its clip (trial_refusal's marker,
        # size_cap_usd, after the regime scale); quoting the full clip bought a $123.61 quote for a
        # $20 trial entry (trial 5, 2026-10-02 00:52:56Z). No manual exemption here: manual awareness
        # stays in the selection hook (test_memecoin_manual_gate_bypass), and a capped estimate on a
        # manual buy only costs the executor its own quote at the reserved clip.
        trial = _trial_for_entry(ro, ev, "solana")
        from sniper.services.memecoin_wallet_caps import entry_cap_usd
        from sniper.services.memecoin_risk import solana_clip_ceiling_usd
        # Warm the wallet's raised ceiling at threshold zero or a qualifying known source mcap;
        # reservation still authorizes the exact final clip. Unknown mcap keeps bounded alternates.
        from types import SimpleNamespace
        source_band = SimpleNamespace(network_id="solana", facts={
            "effective_mcap": getattr(ev, "their_market_cap", None)})
        own = solana_clip_ceiling_usd(source_band, "solana", getattr(ev, "source_user_id", None))
        clip_cap = entry_cap_usd(getattr(ev, "source_user_id", None), "solana",
                                 existing_cap=(trial or {}).get("clip_usd"), ceiling_usd=own)
        # The leg's cross-wallet re-entry scope, so an entry the reservation admits keeps its quote.
        from sniper.services.memecoin_wallet_caps import cross_wallet_reentry_closes_by
        closes_by = cross_wallet_reentry_closes_by(getattr(ev, "source_user_id", None), "solana")
        snapshot_fn = getattr(pre, "snapshot_debits_fn", None)
        evidence = (snapshot_fn(pre.balance_read_started) if snapshot_fn is not None else ())
        ctx = build_memecoin_risk_context(ro, usdc_balance=usdc, sol_balance=native,
                                          day=now_iso[:10], risk_scale=scale,
                                          size_cap_usd=clip_cap, reentry_closes_by=closes_by,
                                          clip_ceiling_usd=own, snapshot_debits=evidence)
        decision = memecoin_entry_allowed(ctx, mint=ev.mint, trade_size=ctx.trade_size_usd)
    except ValueError:
        return None, "snapshot debit unavailable"
    finally:
        ro.close()
    if not decision.allowed:
        return None, "risk:%s" % str(decision.reason or "").split(":")[0].split("(")[0].strip()[:40]
    if not with_alts:
        return float(ctx.trade_size_usd), None
    regime, clip = float(cfg.MEMECOIN_FUNDED_REGIME_RISK_SCALE), float(ctx.trade_size_usd)

    def alt_clips():
        """On the alternates' thread, never before the first quote, on its own read-only
        connection (each envelope ~5 ms live). FIRST the clip an above-nano coin reserves at this
        scale (2026-10-08: the band is resolved after this quote starts; onchain_watch events carry
        no source mcap), THEN today's clip at the hot_narrow scale. EarlyEntryQuote keeps two."""
        from sniper.services.memecoin_risk import solana_above_nano_ceiling_usd
        from sniper.services.memecoin_wallet_caps import raised_clip_wallet_allowed
        # shortcut: unknown source has three prepared clips; reorder only after measured size misses.
        # At threshold 0 the primary already carries the wallet's raised ceiling; a wallet the list
        # does not allow can never reserve it. Either way: no raised alternate.
        raised = (solana_above_nano_ceiling_usd()
                  if own is None and raised_clip_wallet_allowed(getattr(ev, "source_user_id", None))
                  else None)
        envelopes = []
        if raised is not None:
            envelopes.append((scale, raised))
        if market_scale == 1.0 and 0 < regime < 1:
            envelopes.append((scale * regime, own))
        out = []
        ro2 = get_readonly_connection(db_path)
        try:
            evidence = (snapshot_fn(pre.balance_read_started) if snapshot_fn is not None else ())
            for alt_scale, ceiling in envelopes:
                cap = (clip_cap if ceiling == own else entry_cap_usd(
                    getattr(ev, "source_user_id", None), "solana",
                    existing_cap=(trial or {}).get("clip_usd"), ceiling_usd=ceiling))
                alt = build_memecoin_risk_context(ro2, usdc_balance=usdc, sol_balance=native,
                                                  day=now_iso[:10], risk_scale=alt_scale,
                                                  size_cap_usd=cap, reentry_closes_by=closes_by,
                                                  clip_ceiling_usd=ceiling,
                                                  snapshot_debits=evidence)
                size = float(alt.trade_size_usd)
                if (memecoin_entry_allowed(alt, mint=ev.mint, trade_size=alt.trade_size_usd).allowed
                        and size != clip and size not in out):
                    out.append(size)
        except ValueError:
            return []
        finally:
            ro2.close()
        return out
    return clip, None, {"alt_clips_fn": alt_clips}


def _early_cached_balances(chain, pre, *, facts=None):
    """The chain's cached pre-gate (quote, native) for the early sizing, or None (HUSH, 2026-10-07).
    Only from the CURRENT spend generation, the one `pre` started in (every spend invalidates the
    cache and advances it), both values present, the quote balance above 0, and each at most
    MEMECOIN_EVM_EARLY_SIZING_CACHE_S old. Anything else is today's fresh read."""
    key = str(chain)
    with _gate_balance_lock:
        current = _gate_balance_generation.get(key, 0)
        if facts is not None:
            facts.update(pre_generation=getattr(pre, "generation", None), current_generation=current)
        if current != getattr(pre, "generation", None):
            return None
        entry = dict(_gate_balance_cache.get(key) or {})
    quote, native = entry.get("quote"), entry.get("native")
    if quote is None or native is None:
        return None
    now = time.monotonic()
    limit = float(getattr(cfg, "MEMECOIN_EVM_EARLY_SIZING_CACHE_S", 300.0) or 0.0)
    if any(now - ts > limit for _value, ts in (quote, native)):
        return None
    try:
        if not float(quote[0]) > 0:
            return None
    except (TypeError, ValueError):
        return None
    return quote[0], native[0]


def _early_evm_quote_clip(ev, pre, db_path, ctx, chain):
    """Estimate with the EVM risk envelope on a private read-only handle; never reserve.
    Classification may subsequently lower the clip. The executor then discards this quote.
    Returns (clip, refusal, facts); facts["balance_source"] is "cache" or "fresh"."""
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.services import memecoin_arming as arming
    from sniper.services.memecoin_evm_risk import build_evm_risk_context, evm_entry_allowed
    from sniper.services.memecoin_market_state import market_risk_scale
    from sniper.util.clock import real_utc_now_iso
    facts = {"balance_source": "fresh", "pre_generation": getattr(pre, "generation", None),
             "current_generation": None}
    def own_invalidation():
        quote = getattr(ev, "entry_evm_early_quote", None)
        return getattr(quote, "own_invalidation", None)

    def stale():
        return (pre.stale(evidence=facts, own_fn=own_invalidation)
                if isinstance(pre, _EntryPreReads) else pre.stale())
    if not arming.real_entries_allowed(ctx.get("kill_path")) or arming.in_quiet_hours():
        return None, "disarmed", facts
    # BSC's cached USD valuation can drift cents from the same attempt's already-running gate read.
    cached_sizing = getattr(cfg, "MEMECOIN_EVM_EARLY_CACHED_SIZING", False) and str(chain) != "56"
    balances = _early_cached_balances(chain, pre, facts=facts) if cached_sizing else None
    if balances is not None:
        facts["balance_source"] = "cache"
    elif cached_sizing and not stale():
        peek = ctx.get('early_quote_balances_fn')
        fields = _gate_price_fields(ctx)
        price, age = fields.get('quote_price_usd'), fields.get('quote_price_age_ms')
        if (callable(peek) and not (type(price) in (int, float) and math.isfinite(price) and price > 0
                and type(age) in (int, float) and 0 <= age <= 1000)):
            warm = ctx.get('warm_quote_price_fn')
            if callable(warm):
                try:
                    warm()  # Private worker; shares the gate's live price fetch, not its balance wait.
                except Exception:
                    pass
                fields = _gate_price_fields(ctx)
                price, age = fields.get('quote_price_usd'), fields.get('quote_price_age_ms')
        if (callable(peek) and type(price) in (int, float) and math.isfinite(price) and price > 0
                and type(age) in (int, float) and 0 <= age <= 1000):
            try:
                balances = peek(price)
            except Exception:
                balances = None
            if stale():
                balances = None
            if balances is not None:
                facts['balance_source'] = 'lane_snapshot'
    if balances is None:
        balances = pre.balances()
        if balances is None or balances[0] is None:
            return None, "balance_unavailable", facts
        if stale():
            return None, "balance_stale_generation", facts
    now = real_utc_now_iso()
    conn = get_readonly_connection(db_path)
    try:
        uid = getattr(ev, "source_user_id", None)
        if cfg.MEMECOIN_REAL_ROSTER_GATE_ENABLED and uid not in m.real_approved_user_ids(conn, network_id=chain):
            return None, "not_approved", facts
        scale = market_risk_scale(conn, decision_at=now, signal_source=getattr(ev, "signal_source", None))
        step = m.winner_reentry_step(conn, chain, ev.mint, now, signal_source=getattr(ev, "signal_source", None))
        if step is not None and step["factor"] is not None:
            scale *= step["factor"]
        trial = _trial_for_entry(conn, ev, chain)
        from sniper.services.memecoin_wallet_caps import entry_cap_usd
        clip_cap = entry_cap_usd(uid, chain, existing_cap=(trial or {}).get("clip_usd"))
        from sniper.services.memecoin_wallet_caps import cross_wallet_reentry_closes_by
        closes_by = cross_wallet_reentry_closes_by(uid, chain)   # the leg's re-entry scope
        native_price = _sizing_native_price(_gate_price_fields(ctx), ev, chain)
        risk = build_evm_risk_context(
            conn, usdc_balance=balances[0], native_balance=balances[1], day=now[:10],
            network_id=chain, risk_scale=scale, size_cap_usd=clip_cap,
            native_quote_price_usd=native_price, reentry_closes_by=closes_by)
        decision = evm_entry_allowed(risk, mint=ev.mint, trade_size=risk.trade_size_usd)
        return ((float(risk.trade_size_usd), None, facts) if decision.allowed
                else (None, "risk_refused", facts))
    finally:
        conn.close()


def _early_safety_wait_s():
    from sniper.services.memecoin_evm_leg import _FUNDED_EVM_SAFETY_WAIT_S
    return _FUNDED_EVM_SAFETY_WAIT_S


def _early_evm_safety_fn(ctx, chain=None, reference=None):
    """With MEMECOIN_EVM_EARLY_CACHED_SIZING: the chain's token safety, called the way the leg's
    parallel worker calls it (deadline, usd, timings only when the function takes them), for the
    early worker to run beside its quote. None with the knob off or without a safety function.
    `reference`: the event's chase reference; its receipt pool becomes the sell probe's exit hint
    (MEMECOIN_EVM_SAFETY_EXIT_HINT_CHAINS), peeked when the check starts."""
    tsf = ctx.get("token_safety_fn") if isinstance(ctx, dict) else None
    if not getattr(cfg, "MEMECOIN_EVM_EARLY_CACHED_SIZING", False) or not callable(tsf):
        return None
    from sniper.services.memecoin_evm_leg import (
        _executor_supports_kwarg, _names_kwarg, safety_exit_hint_kwargs)

    def safety(mint, clip, deadline, timings):
        kwargs = {}
        if _executor_supports_kwarg(tsf, "deadline"):
            kwargs["deadline"] = deadline
        if _executor_supports_kwarg(tsf, "usd"):
            kwargs["usd"] = clip
        if _names_kwarg(tsf, "timings"):
            kwargs["timings"] = timings
        kwargs.update(safety_exit_hint_kwargs(tsf, chain, reference))
        return tsf(mint, **kwargs)
    return safety


def _start_early_evm_quote(conn, ev, contexts, trace, pre, chain):
    """Shared EVM routes, off the entry thread beside the market read. No wait here."""
    chain = str(chain)
    if chain not in {"56", "8453", "4663", "1", "5042"} or pre is None or trace is None:
        return None
    if (bypasses_selection(getattr(ev, "signal_source", None))
            or getattr(ev, "signal_source", None) == "bsc_fast_lane"
            or getattr(ev, "delivery_lane", None) == J7_SHADOW_LANE):
        return None
    # The 4663 Relay pre-buy rides the native lane's executor too: no shared-route quote either.
    if getattr(ev, 'signal_source', None) in {'rh_fast_lane', 'relay_prebuy'} and not getattr(ev, 'shared_evm_route', False):
        return None
    existing = getattr(ev, "entry_evm_early_quote", None)
    if existing is not None and existing.trace is trace:
        return existing
    ctx = context_for(contexts, chain)
    quote_fn = ctx.get("prepare_entry_quote_fn") if isinstance(ctx, dict) else None
    if not callable(quote_fn):
        return None
    from sniper.persistence.db_core import _connection_db_path
    db_path = _connection_db_path(conn)
    if not db_path:
        return None
    try:   # the source's V2/V3 pair token, for the early ladder's path search (evm_source_route)
        from sniper.execution.evm_source_route import seed_pairs
        seed_pairs(chain, getattr(ev, "evm_chase_reference", None), ev.mint)
    except Exception:
        logger.exception("source pair seed failed chain=%s mint=%s", chain, ev.mint)
    # THE SOURCE'S OWN POOL rides on the early ladder too (evm_source_route, 2026-10-15): the
    # executor waits for this quote when it can take it, so an unhinted early ladder would hold
    # the entry behind today's discovery while the hinted one could have answered.
    from sniper.execution import evm_source_route as _src
    hint = _src.attempt_hint(chain, getattr(ev, "evm_chase_reference", None), ev.mint)
    if hint is not None:
        unhinted = quote_fn

        def quote_fn(mint, usd, *, on_started=None, _fn=unhinted, _hint=hint):
            return _fn(mint, usd, on_started=on_started, route_hint=_hint)
        quote_fn.early_deadline = getattr(unhinted, "early_deadline", False)
    try:
        from sniper.services.memecoin_evm_early_quote import EarlyEvmQuote
        ev.entry_evm_early_quote = None
        ev.entry_evm_early_quote = EarlyEvmQuote(
            mint=ev.mint, trace=trace, quote_fn=quote_fn,
            sizing_fn=lambda: _early_evm_quote_clip(ev, pre, db_path, ctx, chain),
            safety_fn=_early_evm_safety_fn(ctx, chain, getattr(ev, "evm_chase_reference", None)),
            safety_wait_s=_early_safety_wait_s())
        try:
            trace.mark_substage("evm.early_quote_start", started=True)
        except Exception:
            pass
        return ev.entry_evm_early_quote
    except Exception:
        logger.exception("EVM early quote could not start chain=%s", chain)
        return None


def _start_early_entry_quote(conn, ev, contexts, trace, pre):
    """Start this attempt's early Jupiter entry quote (memecoin_early_quote), or None: flag off,
    no trace or early reads to bind to, no quote seam, a pre-screen refusal (counted), an
    in-memory ledger, or an event that cannot carry the handle. Never raises."""
    from sniper.services import memecoin_early_quote as eq

    def skip(why):
        """EVERY RETURN WITHOUT A QUOTE NAMES ITS CAUSE ON THE TRACE (2026-10-02, Alpha): 6 of 12
        on-chain sends carried no early_quote mark at all, so the silent returns could not be told
        apart. Instrument only: counted, marked, nothing else."""
        eq.note_skip(why)
        if trace is not None:
            try:
                trace.mark_substage("early_quote", used=False, reason="skip:" + why)
            except Exception:
                logger.warning("early quote skip mark failed", exc_info=True)
        return None
    try:
        if not eq.enabled():
            return skip("flag_off")
        if trace is None:
            return skip("no_trace")
        if pre is None:
            return skip("no_prereads")
        if getattr(ev, "delivery_lane", None) == J7_SHADOW_LANE:
            return skip("j7_shadow")           # a shadow decision quotes nothing
        sol = context_for(contexts, "solana")
        quote_fn = sol.get("entry_quote_fn") if isinstance(sol, dict) else None
        if quote_fn is None:
            return skip("no_quote_seam")
        from sniper.services import memecoin_arming as _arming
        why = eq.cheap_prescreen(ev, quiet_hours=_arming.in_quiet_hours(), mcap_ceiling=(
            _wallet_mcap_ceiling(cfg.MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET, "solana",
                                 getattr(ev, "source_user_id", None), cfg.MEMECOIN_REAL_MAX_MCAP_USD)))
        if why is not None:
            # C1's per-attempt instrument (2026-10-01): every attempt says whether an early quote
            # ran, so "consumed on >= 70% of on-chain lane sends" is read from the trace alone.
            return skip(why)
        from sniper.persistence.db_core import _connection_db_path
        db_path = _connection_db_path(conn)
        if not db_path:
            return skip("no_db_path")
        ev.entry_early_quote = None            # prove the event can carry it BEFORE any work
        shadow_start = sol.get("native_shadow_start_fn")
        attempt_id = getattr(trace, "attempt_id", None)
        ev.entry_early_quote = eq.EarlyEntryQuote(
            mint=ev.mint, quote_fn=quote_fn, trace=trace,
            sizing_fn=lambda: _early_quote_clip(ev, pre, db_path, with_alts=True),
            shadow_fn=(None if shadow_start is None else
                       lambda mint, clip: shadow_start(mint, clip, attempt_id)))
        try:
            trace.mark_substage("early_quote_start", started=True)
        except Exception:
            logger.warning("early quote start mark failed", exc_info=True)
        return ev.entry_early_quote
    except Exception as exc:
        logger.warning("early entry quote did not start mint=%s", getattr(ev, "mint", None),
                       exc_info=True)
        return skip("error:%s" % type(exc).__name__)


def _entry_prereads_for(ev, trace):
    """This attempt's early reads, or None."""
    pre = getattr(ev, "entry_prereads", None)
    return pre if pre is not None and trace is not None and pre.trace is trace else None


def _settle_early_mint_read(ev, trace):
    """Hand a successful early mint read to the leg's safety gate, as the on-chain band does.
    A failed, raised or unfinished read is never kept: the gate then reads fresh, as before."""
    pre = _entry_prereads_for(ev, trace)
    if pre is None or getattr(ev, "mint_account_read", None) is not None:
        return
    read = pre.mint_read(getattr(ev, "mint", None))
    if isinstance(read, tuple) and len(read) == 2 and read[1] is None and read[0] is not None:
        try:
            ev.mint_account_read = read
        except (AttributeError, TypeError):
            pass


FAST_LANE_SOURCES = frozenset({"rh_fast_lane", "bsc_fast_lane", "relay_prebuy"})


def _sizing_native_price(price_fields, ev, chain):
    """The native USD price the EVM risk envelope may value spare native with, else None (spare
    native then adds no sizing equity). ONE RULE FOR BOTH SIZERS (2026-10-15, Alpha): the early quote
    and the gate both call this, so their clips cannot diverge on it. They did: the gate excluded
    the fast lanes and the early quote did not, so every rh/bsc fast-lane entry threw its early
    quote away with clip_mismatch (4663 position 73895: early 102.86, gate 51.40).

    A fresh price only (0..1000 ms in the memo). The fixed native fast-lane pilots keep sizing on
    wrapped cash alone unless their chain is in MEMECOIN_EVM_FAST_LANE_NATIVE_EQUITY_CHAINS
    (call-time; empty or malformed = that exclusion). The float cover and the pilot clip still
    bound what such an entry spends."""
    try:
        px, age = price_fields.get("quote_price_usd"), price_fields.get("quote_price_age_ms")
        if px is None or age is None or isinstance(px, bool) or isinstance(age, bool):
            return None
        px, age = float(px), float(age)
        if not (math.isfinite(px) and px > 0 and math.isfinite(age) and 0 <= age <= 1000):
            return None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    if (getattr(ev, "signal_source", None) in FAST_LANE_SOURCES
            and str(chain) not in cfg.memecoin_evm_fast_lane_native_equity_chains()):
        return None
    return px


def _gate_price_fields(ctx):
    """The gate's quote-token USD price (P1) for its balance-read mark, or {} when unknown.

    Read from the executor bundle's price memo WITHOUT fetching (2026-09-28): together with the
    executor's own price (P2, on `evm.unwrap_facts`) it names a sizing-vs-quote price skew."""
    peek = (ctx or {}).get("quote_price_peek_fn")
    if not callable(peek):
        return {}
    try:
        got = peek()
    except Exception:
        return {}
    if not isinstance(got, dict) or got.get("px") is None:
        return {}
    fields = {"quote_price_usd": got.get("px"), "quote_price_age_ms": got.get("age_ms")}
    if "source" in got:
        fields["quote_price_source"] = got["source"]
    return fields


def _make_evm_entry_fn(contexts):
    """The router's evm_entry_fn: dispatch an allowlisted EVM buy to its chain's context. No context
    for the chain -> evm_unarmed drop (counted, never sent to the Solana executor)."""
    from sniper.services.memecoin_evm_leg import evm_real_entry

    def evm_entry_fn(conn, ev, *, route, day, now_iso, trade_size, kill_path=None, clock=None,
                     alert_fn=None, trace=None, classification=None, impact_cap_pct=None,
                     chase_max_pct=None, wallet_chase_cap_pct=None,
                     wallet_chase_cap_expires_at=None, capacity_clip=None, size_cap_usd=None,
                     trial=None, **_ignored):
        chain = route.split(":", 1)[1] if ":" in route else route
        ctx = contexts.get(str(chain))
        if ctx is None:
            m.record_network_drop(conn, chain, ev.mint, "evm_unarmed", now_iso)
            return {"status": "evm_unarmed", "route": route}
        # A CONTEXT'S HARD CAP (Codex N18, 2026-10-07): the RH fast lane's shared routes carry the
        # pilot clip as entry_size_cap_usd, and this adapter used to drop it, so bankroll sizing
        # could reserve more (masked on 2026-10-07 only because MEMECOIN_EVM_TIER_CLIP_USD_4663
        # equals the pilot clip, 175). Merged downward into the caller's cap before any read; every
        # _enter below forwards it to the reservation. A present cap that is not a finite positive
        # number refuses: an unknown pilot clip is not an uncapped one. A caller cap that is not
        # (memecoin_evm_risk ignores it) never widens past the context cap.
        if "entry_size_cap_usd" in ctx:
            context_cap = ctx.get("entry_size_cap_usd")
            try:
                context_cap = (None if isinstance(context_cap, (bool, str)) or context_cap is None
                               else float(context_cap))
            except (TypeError, ValueError, OverflowError):
                context_cap = None
            if context_cap is None or not math.isfinite(context_cap) or context_cap <= 0:
                return {"status": "rejected", "reason": "entry size cap unavailable"}
            try:
                caller_cap = float(size_cap_usd) if size_cap_usd is not None else None
            except (TypeError, ValueError, OverflowError):
                caller_cap = None
            usable = caller_cap is not None and math.isfinite(caller_cap) and caller_cap > 0
            size_cap_usd = min(context_cap, caller_cap) if usable else context_cap
        # A (2026-09-27): the reads started beside the market-data GET. A read that one of our own
        # spends on this chain has overtaken is discarded and read again; the race accepted is the
        # one the in-line read always had (another attempt's fill before this reservation).
        _gate_read_started = time.monotonic()
        _pre = _entry_prereads_for(ev, trace)
        _early = None
        _stale = False
        if _pre is not None and getattr(_pre, "chain", None) == str(chain):
            _early = _pre.balances()
            if _early is not None and _pre.stale():
                _early, _stale = None, True
        gate_failures = {}
        if _early is not None:
            quote_usd, native_bal, balance_from_cache, _early_read_ms = _early
            gate_failures = _pre.failures
        else:
            quote_usd, native_bal, balance_from_cache = _gate_balances(
                chain, ctx["usdc_fn"], ctx["native_fn"], failures=gate_failures)
        price_fields = _gate_price_fields(ctx)  # peek only; never another provider read
        native_price = _sizing_native_price(price_fields, ev, chain)
        try:
            if trace is not None:
                trace.mark_substage("timing_gate_balance_read",
                                    network_id=str(chain), quote_balance_usd=quote_usd,
                                    native_balance_native_units=native_bal,
                                    gas_price_wei=None,  # This tuple does not read gas; probe evidence does.
                                    duration_ms=(time.monotonic() - _gate_read_started) * 1000.0,
                                    served_from_cache=bool(balance_from_cache),
                                    early=_early is not None, early_stale=_stale,
                                    read_ms=_early_read_ms if _early is not None else None,
                                    quote_read_failure=gate_failures.get("quote"),
                                    native_read_failure=gate_failures.get("native"),
                                    **price_fields)
        except Exception:
            logger.exception("gate balance read timing not recorded for %s", ev.mint)
        if quote_usd is None:
            return {"status": "rejected",
                    "reason": _quote_balance_unavailable_reason(gate_failures)}

        def _enter(size, clip, *, trace_attempt_base=1):
            # We are about to spend, so the cached pre-gate balances are now stale by
            # definition. Drop them before the transaction, never after.
            early_quote = getattr(ev, "entry_evm_early_quote", None)
            owned = (trace is not None and early_quote is not None
                     and getattr(early_quote, "trace", None) is trace)
            # Recorded under the generation lock, with the advance (see _gate_balance_invalidate).
            _gate_balance_invalidate(
                chain, on_advance=((lambda g, at: early_quote.note_invalidation(chain, g, at))
                                   if owned else None))
            return evm_real_entry(
                conn, ev, route=route, day=day, usdc_balance=quote_usd,
                native_balance=native_bal, now_iso=now_iso, trade_size=size,
                native_quote_price_usd=native_price,
                token_safety_fn=ctx["token_safety_fn"], execute_fn=ctx["execute_fn"],
                kill_path=kill_path, clock=clock, alert_fn=alert_fn, trace=trace,
                classification=classification,
                impact_cap_pct=impact_cap_pct, chase_max_pct=chase_max_pct,
                wallet_chase_cap_pct=wallet_chase_cap_pct,
                wallet_chase_cap_expires_at=wallet_chase_cap_expires_at,
                capacity_clip=clip, trace_attempt_base=trace_attempt_base,
                warm_price_fn=ctx.get("warm_quote_price_fn"), size_cap_usd=size_cap_usd,
                head_fn=ctx.get("head_fn"), trial=trial,
                early_quote=(getattr(ev, "entry_evm_early_quote", None)
                             if getattr(getattr(ev, "entry_evm_early_quote", None), "trace", None) is trace
                             and trace_attempt_base == 1 else None),
                entry_signing_snapshot_fn=ctx.get("entry_signing_snapshot_fn"),
                native_funding_precheck_fn=ctx.get("native_funding_precheck_fn"))

        result = _enter(trade_size, capacity_clip)
        next_trace_attempt_base = 2
        if (result.get("status") == "aborted"
                and result.get("quote_miss_code") in {
                    "provider_rate_limit", "provider_rpc_transport", "provider_timeout",
                    "provider_malformed_response",
                    # The v4 blind sites split out of a shared "rpc_transport" on 2026-09-02.
                    # A guard cannot catch what its own list omits: without these four the rename
                    # would silently STOP the provider retry on every v4 failure, which is the
                    # largest miss class on 4663.
                    "provider_discovery_all_blind", "provider_poolkey_read_failed",
                    "provider_poolkey_read_timeout", "provider_poolkey_malformed",
                    # 2026-09-03: an empty or failed discovery on a possibly-fresh pool is
                    # unproven, not terminal. This one re-entry is what turns "no_route" on an
                    # 18-second-old pool into a fill a few seconds later.
                    "provider_discovery_rpc_failed", "provider_discovery_unproven",
                    # A ladder that timed out waiting on every rung told us nothing about the
                    # coin (Codex audit #12) -- the same transient-infrastructure class as
                    # "provider_timeout", so it gets the same one-shot immediate re-quote.
                    "provider_ladder_deadline",
                    # NOTHING WAS MEASURED, the same class as the ten codes above and NOT the
                    # "no_route" this set deliberately excludes (Codex EX3, 2026-09-20). Two
                    # producers share this code: evm_v4_multihop's own deadline/request budget
                    # (audit E4, 2026-09-18, note "search_stopped:*") and a fixed block it could
                    # not read (EX1, 2026-09-20, note "decision_block_unavailable"). The scheduled
                    # retry lane already retries this code through its "no quote" prefix; the
                    # immediate lane had no comment explaining its absence, which in this codebase
                    # means an oversight, not a decision. Zero occurrences in 51,023 recorded entry
                    # attempts back to 2026-07-15 (checked in trace_json, both producers): this is
                    # a latent-consistency fix, not a response to anything that has cost a fill.
                    "provider_search_limit",
                    # Our own GeckoTerminal cooldown (2026-09-28): typed `provider_rate_limit`
                    # until then, so it keeps that code's one-shot immediate re-quote.
                    "provider_discovery_cooldown",
                }):
            try:
                if trace is not None:
                    trace.mark_substage(
                        "evm.provider_quote_retry",
                        prior_code=result.get("quote_miss_code"),
                    )
            except Exception:
                pass
            result = _enter(
                trade_size, capacity_clip, trace_attempt_base=next_trace_attempt_base)
            next_trace_attempt_base += 1
        # WHAT WAS APPROVED, NOT WHAT WAS CONFIGURED (2026-09-24, attempt 2cf2b654). The retry used
        # to halve the configured base, so on a wallet holding less than that base it asked for more
        # than was spendable and the risk context refused its OWN retry: $49.79 in the wallet, a
        # base of 100, a retry of $50, "insufficient bankroll". The size that matters is the one the
        # gate approved for the first attempt, which `evm_real_entry` now carries back; the base
        # stays the ceiling. An older result shape without the key falls back to the previous
        # arithmetic, and the clamp below covers that case.
        effective_clip = capacity_clip if capacity_clip is not None else trade_size
        approved_clip = result.get("approved_clip_usd") if isinstance(result, dict) else None
        try:
            if approved_clip is not None and float(approved_clip) > 0:
                effective_clip = min(float(effective_clip), float(approved_clip))
        except (TypeError, ValueError):
            pass
        # AND NEVER MORE THAN THE WALLET HOLDS AT THIS SAME SNAPSHOT. `quote_usd` is the balance the
        # first attempt was sized against, so the retry cannot ask the risk context for money that
        # was not there a second ago. Belt to the brace above, and the only guard when the result
        # carries no approved clip.
        try:
            if quote_usd is not None and float(quote_usd) > 0:
                effective_clip = min(float(effective_clip), float(quote_usd))
        except (TypeError, ValueError):
            pass
        if (bypasses_selection(getattr(ev, "signal_source", None))
                or result.get("status") != "aborted"
                or result.get("quote_miss_code") != "route_capacity"):
            return result
        try:
            floor = float(cfg.min_trade_floor_usd_for(ev.network_id))
            retry_start = max(floor, round(float(effective_clip) / 2.0, 2))
        except (TypeError, ValueError):
            return result
        if retry_start >= float(effective_clip):
            return result
        from sniper.execution import evm_source_route as _src
        retry_clip, retry_floor = reduce_clip_for_route_capacity(
            chain, ev.mint, retry_start, ctx,
            route_hint=_src.attempt_hint(chain, getattr(ev, "evm_chase_reference", None), ev.mint))
        if retry_floor is not None or retry_clip is None or retry_clip >= float(effective_clip):
            return result
        try:
            if trace is not None:
                trace.mark_substage("evm.route_capacity_retry", prior_clip=effective_clip,
                                    retry_clip=retry_clip)
        except Exception:
            pass
        if classification is not None and trace is not None:
            # A different clip needs a different immutable contract. Never reuse a signed or
            # unresolved reservation, even if an executor mislabeled it as a quote miss.
            pending = m.pending_row(conn, result.get("pending_id")) or {}
            if (pending.get("status") != "failed" or pending.get("attempt_id") != trace.attempt_id
                    or any(pending.get(k) is not None for k in (
                        "tx_sig", "broadcast_at", "signed_payload_sha256", "submission_bound_at"))):
                return result
            from sniper.services.memecoin_entry_trace import EntryAttemptTrace
            parent = trace
            _momentum_own_late_best_effort(parent)
            _tape_move_late_best_effort(parent)
            parent.finish(*_trace_terminal(result), pending_id=pending["id"])
            trace = EntryAttemptTrace.start(
                conn, ev, parent._absolute_at(parent._now()), parent._clock)
            parent.retry_trace = trace
            trace._shadow_context = dict(parent._shadow_context)
            if hasattr(parent, "evm_receipt_timing"):
                trace.evm_receipt_timing = parent.evm_receipt_timing
            if hasattr(parent, "copy_cap_band_without_override"):
                trace.copy_cap_band_without_override = parent.copy_cap_band_without_override
            trace.mark_substage("evm.route_capacity_parent", parent_attempt_id=parent.attempt_id,
                                prior_clip=effective_clip, retry_clip=retry_clip)
        return _enter(retry_clip, retry_clip, trace_attempt_base=next_trace_attempt_base)
    return evm_entry_fn


_REJECT_SOLANA = {"status": "aborted", "reason": "no solana leg configured"}


def _funded_market_stats(stats, decision_at, *, conn=None, chain_id=None, mint=None, ev=None):
    """Copy existing stats, derive pair age, and fall back to our own pool store for depth.

    E6 OFF (DexScreener out, 2026-09-29): the classification's liquidity is OUR number only. It
    feeds telemetry alone (`liquidity_class` gates nothing; see memecoin_market_state), and it lands
    in this COPY: the caller's snapshot keeps the provider's liquidity, which the dust-pair check
    (E4) reads to decide whether a price is real."""
    out = dict(stats or {})
    if out.get("pair_age_s") is None and out.get("token_age_s") is None:
        age = _pair_age_s(out, decision_at)
        if age is not None:
            out["pair_age_s"] = age
    if chain_id and cfg.dexscreener_off("E6", chain_id):
        out["liquidity"], out["liquidity_source"] = _own_liquidity(
            conn, chain_id, mint, decision_at, ev)
        return out
    _fill_depth_from_pool_store(out, decision_at, conn=conn, chain_id=chain_id, mint=mint)
    return out


def _own_liquidity(conn, chain_id, mint, decision_at, ev):
    """(usd, source) from our own sources, or (None, None). The pool store's cached depth (4663), else
    the quote reserve the source's own trade implies (Solana watcher and handoff rows)."""
    max_age_s = float(getattr(cfg, "MEMECOIN_POOL_DEPTH_MAX_AGE_S", 0) or 0)
    if conn is not None and mint and max_age_s > 0:
        depth = m.pool_depth_usd(conn, str(chain_id), str(mint), now_iso=decision_at,
                                 max_age_s=max_age_s)
        if depth is not None:
            return depth, _pool_depth_source()
    implied = positive_price(getattr(ev, "implied_quote_reserve_usd", None))
    if implied:
        # The leg travels in the label, so a reader can split venues (Gamma LOW 1).
        return implied, "implied_source_trade:%s" % (getattr(ev, "implied_quote_leg", None) or "unknown")
    return None, None


def _sell_share_input(st, onchain_m5, chain_id):
    """(stats the sell-share gate reads, basis, our own split or None). E7 (2026-09-29, Alpha GO).

    Our split comes from the tape read the volume gate already made (no new read) and is returned
    whatever the switch says, for the shadow record. With E7 off on a chain that HAS our tape, the
    gate reads it; an unmeasured window gives {} and the gate admits, as on a missing DexScreener
    field. A chain without our tape (Solana) keeps DexScreener even under a bare E7: switching it
    off there would silently stop the gate gating. 56 reads the BSC observer's source-pair split."""
    from sniper.services.memecoin_onchain_volume import own_sell_share_split
    own = own_sell_share_split(onchain_m5)
    if not cfg.dexscreener_off("E7", chain_id):
        return st, "m5_txn_counts", own
    # 56's own tape is the BSC observer (its source-pair counts, entry-gates spec step 4).
    if str(chain_id) not in set(getattr(cfg, "MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS", ()) or ()) | {"56"}:
        return st, "m5_txn_counts_no_own_tape", own
    return (own or {}), "onchain_pool_counts", own


def _telemetry_liquidity(st, classification, chain_id):
    """The liquidity the chase telemetry records: the provider's, or with E6 off the classification's
    own number (None when it has none). Measurement only."""
    if cfg.dexscreener_off("E6", chain_id):
        return ((getattr(classification, "facts", None) or {}).get("liquidity")
                if classification is not None else None)
    return st.get("liquidity") if isinstance(st, dict) else None


def _pair_age_s(stats, decision_at):
    """Seconds from the provider's `pair_created_at` (ms) to `decision_at`, or None."""
    try:
        created_ms = float((stats or {}).get("pair_created_at"))
        decision = datetime.fromisoformat(str(decision_at).replace("Z", "+00:00"))
        if (math.isfinite(created_ms) and created_ms > 0
                and decision.tzinfo is not None and decision.utcoffset() is not None):
            return max(0.0, decision.timestamp() - created_ms / 1000.0)
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _fill_depth_from_pool_store(stats, decision_at, *, conn, chain_id, mint):
    """Use our own cached pool depth when the PROVIDER returned nothing. In place; never raises.

    2026-09-22, operator: measured on 4663, the provider returned no pair at all on 1,982 of the
    2,254 attempts since 09-15, so `liquidity_class` read `unknown` while our `pool_meta` already
    knew pools for 2,060 of them — and for 70.2% it knew them BEFORE the decision, a median 14
    hours before. This reads the precomputed cache (one indexed SELECT, no RPC: finding a coin's
    live pool means reading dozens of empty ones, which a 60-second-old coin's decision cannot
    afford).

    IT ONLY EVER FILLS A HOLE. A provider number wins whenever one exists, so this cannot move a
    classification the provider already decided. The value is the VIRTUAL local liquidity
    2*L*sqrt(P) of the current tick (Codex review 2026-09-23, F6: not a floor and not TVL; it
    overstates a narrow concentrated position by orders of magnitude). Measured at a median 0.27
    of the provider's TVL on coins where both existed, so it is labelled `liquidity_source` and
    the raw figure is kept beside it for any reader that cares.
    """
    if conn is None or not chain_id or not mint:
        return
    try:
        provider = stats.get("liquidity")
        if isinstance(provider, (int, float)) and not isinstance(provider, bool) \
                and math.isfinite(float(provider)) and float(provider) >= 0:
            return                                   # the provider answered (0 included)
        from sniper.settings import memecoin as cfg
        from sniper.services import memecoin_pool_depth as pool_depth
        max_age_s = float(getattr(cfg, "MEMECOIN_POOL_DEPTH_MAX_AGE_S", 0) or 0)
        if max_age_s <= 0:
            return                                   # the fallback is off
        depth = m.pool_depth_usd(conn, str(chain_id), str(mint),
                                 now_iso=decision_at, max_age_s=max_age_s)
        if depth is None:
            return
        stats["liquidity"] = depth
        stats["liquidity_usd"] = depth
        stats["liquidity_source"] = pool_depth.SOURCE
        stats["liquidity_pool_store_usd"] = depth
    except Exception:
        logger.exception("pool-store depth fallback failed for %s on %s", mint, chain_id)


def _mark_pair_selection(trace, stats):
    """Record the DexScreener pair pick and the activity-fallback pick from the same read.

    Observation only (2026-09-17, operator order B): it lands in trace_json before any gate reads the
    stats, so a chase refusal carries it too. A failure is logged and never touches the entry.
    """
    record = (stats or {}).get("pair_selection")
    if trace is None or not isinstance(record, dict):
        return
    try:
        trace.mark_substage("dex_pair_selection", **record)
    except Exception:
        logger.exception("dex pair selection substage not recorded")


def _set_event_market_ticker(ev, stats):
    """Set the event ticker from market data when the event has no ticker."""
    if getattr(ev, "ticker", None):
        return
    symbol = (stats or {}).get("symbol")
    if isinstance(symbol, str) and symbol.strip():
        ev.ticker = symbol.strip()


def _mark_probe(trace, name, duration_ms=None, **metadata):
    """One pre-ladder probe substage. Measurement only: it can never raise into an entry."""
    try:
        if duration_ms is None:
            trace.mark_substage(name, **metadata)
        else:
            trace.mark_substage(name, duration_ms=duration_ms, **metadata)
    except Exception:
        pass


def _decision_head_fields(network_id, *, now_mono=None):
    """The Solana head slot this process's slot signal last saw, and its age, at our decision: fill
    timing's recv-S for an on-chain fill (2026-10-02). {} off Solana or with no head yet. Two dict
    reads, no call."""
    if str(network_id or "") != "solana":
        return {}
    from sniper.services.memecoin_slot_signal import HEAD
    slot, mono = HEAD.get("slot"), HEAD.get("mono")
    if slot is None or mono is None:
        return {}
    now = time.monotonic() if now_mono is None else now_mono
    return {"head_slot": slot, "head_age_ms": round((now - mono) * 1000.0, 3)}


def _warm_mint_age(trace, classification, network_id, mint, source_user_id=None):
    """The coin-age floor's mint evidence, resolved BEFORE the reservation (2026-09-28, Alpha GO).

    Only when the pair age is under the chain's floor, i.e. only on what would be a refusal: the
    gate reads memecoin_coin_age's cache inside the reservation transaction, where no I/O may run.
    Bounded at memecoin_coin_age.WAIT_S. Never raises into the entry."""
    try:
        age = ((getattr(classification, "facts", None) or {}).get("pair_age_s")
               if classification is not None else None)
        floor = cfg.min_coin_age_s_for(network_id, source_user_id)
        if floor <= 0:
            return
        if not isinstance(age, (int, float)) or isinstance(age, bool):
            # AN UNKNOWN PAIR AGE (operator yes 2026-09-28): start the mint lookup WITHOUT waiting.
            # It runs beside the reservation and the quote; the executor reads its cache just
            # before signing (memecoin_risk.coin_age_presign_check). Solana only, knob-gated.
            from sniper.services.memecoin_coin_age import unknown_pair_check_enabled, warm
            if str(network_id) == "solana" and unknown_pair_check_enabled():
                mint_age = warm(network_id, mint, wait_s=0.0)
                if trace is not None:
                    trace.mark_substage("coin_age_evidence", pair_age_s=None, mint_age_s=mint_age,
                                        floor_s=floor, wait_ms=0.0, background=True)
            return
        if float(age) >= floor:
            return
        from sniper.services.memecoin_coin_age import warm
        started = time.monotonic()
        mint_age = warm(network_id, mint)
        if trace is not None:
            trace.mark_substage("coin_age_evidence", pair_age_s=float(age), mint_age_s=mint_age,
                                floor_s=floor, wait_ms=(time.monotonic() - started) * 1000.0)
    except Exception:
        logger.exception("coin age evidence failed for %s", mint)


def _classify_funded_draft(conn, ev, identity, decision_at, context, market_stats=None):
    if context is None:
        return None
    from sniper.services.memecoin_market_state import classify_funded_entry
    stats = context.get("market_stats") if market_stats is None else market_stats
    return classify_funded_entry(
        conn,
        network_id=identity.network_id,
        mint=ev.mint,
        decision_at=decision_at,
        signal_source=getattr(ev, "signal_source", None) or "global_feed",
        effective_mcap=context.get("effective_mcap"),
        assumed_micro=bool(context.get("assumed_micro")),
        market_stats=_funded_market_stats(stats, decision_at, conn=conn,
                                          chain_id=identity.network_id, mint=ev.mint, ev=ev),
        base_clip_usd=None,
        consensus_n_60m=context.get("consensus_n_60m"),
        mcap_basis=context.get("mcap_basis"),
        priced_mcap_shadow=getattr(ev, "priced_mcap_shadow", None),
        momentum_own=_momentum_own_peek(context.get("momentum_own")),
    )


def _persist_attempt_classification(conn, trace, classification):
    if trace is None or classification is None or not getattr(trace, "attempt_id", None):
        return False
    payload, digest = m.canonical_funded_json(classification.to_dict())
    return m.set_entry_attempt_classification(
        conn,
        trace.attempt_id,
        classification_json=payload,
        classification_hash=digest,
        classification_version=classification.classification_version,
    )


def _persist_terminal_classification_best_effort(
        conn, ev, identity, decision_at, trace, *, effective_mcap=None,
        assumed_micro=False, consensus_n_60m=None, market_stats=None, mcap_basis=None):
    try:
        classification = _classify_funded_draft(
            conn, ev, identity, decision_at,
            {
                "effective_mcap": effective_mcap,
                "assumed_micro": assumed_micro,
                "consensus_n_60m": consensus_n_60m,
                "market_stats": market_stats,
                "mcap_basis": mcap_basis,
            },
            market_stats=market_stats or {},
        )
        _persist_attempt_classification(conn, trace, classification)
    except Exception:
        logger.exception("terminal funded classification audit failed mint=%s",
                         getattr(ev, "mint", None))


def _fmt_price(value):
    """Compact price for a refusal reason. Memecoin prices span many orders of magnitude, so a
    fixed precision either truncates a real price to 0.00 or prints noise."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "?"
    return ("%.8f" % v).rstrip("0").rstrip(".") if v >= 1e-8 else "%.3e" % v


def is_manual_entry(ev) -> bool:
    """True for an operator's own manual entry.

    SELECTION gates are bypassed for these; SAFETY gates are not (operator decision 2026-08-27).
    The distinction is the one the operator drew: a gate that judges whether the coin is a GOOD BUY
    does not apply when the operator IS the decision, but a gate that protects the capital still
    does. Bypassed here: event chase, first-seen chase, revival/volume floor, coin-age ceiling,
    coin-band reservable, sell-share. Still enforced: kill switch, token safety, impact cap,
    round-trip cap, bankroll/reservation/balance.

    The source-buy floor and the chase price pass-through were already manual-exempt before this
    (2026-08-23); this makes the rest of the selection chain consistent with them instead of
    refusing a manual buy for a reason that only means something when copying somebody.
    """
    # The DESK seat is the decision too (spec desk-execution-plumbing §1.5): same SELECTION bypass.
    # Capacity is NOT in this split — the legs keep it for desk via memecoin_origin.enforces_capacity.
    from sniper.services.memecoin_origin import bypasses_selection
    return bypasses_selection(getattr(ev, "signal_source", None))


def _first_buy_window_applies(ev, network_id=None):
    return (entry_opportunity.enabled() and not is_manual_entry(ev)
            and getattr(ev, "delivery_lane", None) != J7_SHADOW_LANE
            and getattr(ev, "side", "buy") == "buy"
            and m.normalized_coin_network(network_id or getattr(ev, "network_id", None),
                                          getattr(ev, "mint", None)) in {"solana", "sol"})


def reduce_clip_for_route_capacity(network_id, mint, clip, venue, *, route_hint=None):
    """Return the largest tested 4663 clip, before a reservation exists. `route_hint` (the source
    pool, evm_source_route) rides into each probe ladder, whose quote the entry may reuse."""
    probe = (venue or {}).get("entry_quote_probe_fn")
    if str(network_id) != "4663" or not callable(probe):
        return clip, None
    try:
        floor = float(cfg.MEMECOIN_MIN_TRADE_FLOOR_USD)
        candidate = round(float(clip), 2)
    except (TypeError, ValueError):
        return clip, None
    while True:
        try:
            miss = (probe(mint, candidate, route_hint=route_hint) if route_hint
                    else probe(mint, candidate))
        except Exception:
            miss = "provider_rpc_transport"
        if miss != "route_capacity":
            return candidate, None
        if candidate <= floor:
            return None, candidate
        candidate = max(floor, round(candidate / 2.0, 2))


def source_buy_band_reason(their_usd, abs_min, abs_max, tolerance_pct=0.0, judged_usd=None):
    """The absolute source-buy band decision, or None to pass.

    THE FLOOR JUDGES THE TRADER'S NET INTO THE COIN, the same amount the ratio floor judges
    (operator rule 2026-09-13; degen_ak AGNET 4663, 2026-10-02: a $100 add-on after $498 was refused
    "under absolute floor $197" while the ratio leg of the same gate judged $598). `judged_usd` is
    that net; it only ever RAISES what the floor judges, so None, or a net below the buy, judges the
    single buy as before. THE CEILING stays on the single buy.

    A module-level function so the gate and its tests share ONE implementation. A test that
    restates this arithmetic proves only that the restatement is self-consistent.

    `their_usd` is the SOURCE'S OWN REPORTED size — not our fill, not exact. The conjunction this
    band comes from was measured on that field, so this must read that field.

    UNKNOWN PASSES. A size the source did not report says nothing about the trade, the same posture
    a missing m5 reading takes. Both bounds are inclusive: a buy exactly at the floor or exactly at
    the ceiling is inside the band.
    """
    if their_usd is None:
        return None
    floor_judged = their_usd if judged_usd is None or not judged_usd > their_usd else judged_usd
    # THE SAME BOUNDARY TOLERANCE AS THE RATIO FLOOR (2026-09-28, Alpha GO): judged at
    # abs_min x (1 - tolerance/100); the reason keeps quoting the configured floor.
    if abs_min > 0 and floor_judged < abs_min * (1.0 - max(0.0, float(tolerance_pct or 0.0)) / 100.0):
        rendered = ("$%.0f" % their_usd if floor_judged == their_usd else
                    "$%.0f + $%.0f earlier = $%.0f" % (their_usd, floor_judged - their_usd, floor_judged))
        return "source buy %s under absolute floor $%.0f" % (rendered, abs_min)
    if abs_max > 0 and their_usd > abs_max:
        return "source buy $%.0f over absolute ceiling $%.0f" % (their_usd, abs_max)
    return None


def onchain_m5_txns_lower_bound(evidence):
    """Distinct transactions on one pool, source excluded, as a lower bound on m5 trades.
    Legacy swap counts cannot prove transaction counts. Trust only measured, verified windows."""
    if not isinstance(evidence, dict) or str(evidence.get("reason") or "") != "ok":
        return None
    if evidence.get("basis") == "source_unknown_tape_bound":
        return None                     # the source's own swap may be one of the counted trades
    try:
        if evidence.get("usd") is None or int(evidence.get("unverified_swaps") or 0) > 0:
            return None
        pools = [int(p["txns"]) for p in (evidence.get("pools") or ())
                 if isinstance(p, dict) and p.get("txns") is not None]
        # Never sum pools: a multi-hop transaction can touch several of the coin's pools.
        if evidence.get("pools"):
            txns = max(pools) if pools else None
        else:
            txns = int(evidence.get("txns"))
    except (TypeError, ValueError):
        return None
    return txns if txns is not None and txns >= 0 else None


def onchain_h1_lower_bound(evidence):
    """The on-chain m5 sum as a LOWER BOUND on the hour, or None when it cannot serve as one.

    ⚠️ A 300-SECOND SUM IS A BOUND ON THE HOUR, NEVER AN ESTIMATE OF IT. It proves the coin traded
    at least this much; it says nothing about the rest of the hour, so it may only ever RAISE the
    hourly organic figure and must never reach the ceiling or an m5 leg.

    FEEDME (4663, 0de1e0bb, 2026-09-23T18:23:24Z) is the case: the provider row was a stub --
    `volume_h1 $0.62`, liquidity $0.69, m5 absent, a seven-minute-old Pons-curve coin it had not
    indexed -- and the coin was refused on "h1 organic volume $1 under floor $3000" while our own
    tape read 55 swaps and $5,502.47 organic in the same five minutes, coverage partial, protocol
    lag 0.57 s, source transaction seen. Before this the on-chain sum reached only
    `m5_floor_lower_bound`; on 4663 and 8453 `h1_floor_lower_bound` was never set at all, since its
    only assignment is the Solana feed-buy path.

    TRUSTWORTHY MEANS THE WINDOW MEASURED SOMETHING. `reason == "ok"` and no unverified swaps: a
    `hash_unverified` or `protocol_lagging` window already returns `usd` None and so contributes
    nothing by construction, and the explicit test means a future reason code cannot leak in.
    """
    if not isinstance(evidence, dict):
        return None
    if str(evidence.get("reason") or "") != "ok":
        return None
    try:
        if int(evidence.get("unverified_swaps") or 0) > 0:
            return None
    except (TypeError, ValueError):
        return None
    try:
        usd = float(evidence.get("usd"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(usd) or usd < 0:
        return None
    return usd


def source_buy_family_reason(*, their_usd, source_mcap, prior_net_usd, ratio,
                             unknown_mcap_floor, abs_min, abs_max, family_active,
                             is_manual=False, floor_tolerance_pct=None):
    """The WHOLE source-buy family decision, or None to pass, plus what it read.

    Five legs, in the order the entry gate applies them: the mcap RATIO floor, the cumulative
    earlier net into the same coin, the unknown-mcap fallback floor, and then the absolute band --
    which runs only when nothing above already refused, so one entry produces one reason.

    A MODULE-LEVEL FUNCTION BECAUSE TWO CALLERS NEED THE SAME ANSWER. The promotion shadow ran only
    the absolute band and called the family a pass on buys the funded path refuses: cap $200,000,
    buy $900, ratio 0.008 needs $1,600 (2026-09-23 review, R4). A shadow that restates a money rule
    drifts from it -- that is also what R3 was -- so neither side restates it now.

    `their_usd` is the SOURCE'S OWN REPORTED size, not our fill; the evidence behind these bounds
    was measured on that field. `ratio` arrives ALREADY RESOLVED through `_band_source_buy_ratio`,
    because the caller needs it to decide whether the cumulative read is worth making at all.
    `family_active` is the caller's `ratio > 0 or unknown_mcap_floor > 0 or band_ratios`, kept as an
    argument so an all-zero ratio with a populated band map still enters the block.

    UNKNOWN IS NOT SYMMETRIC HERE, and this is the part a reimplementation gets wrong: an unknown
    size PASSES the absolute band (a field the source did not report says nothing) but FAILS the
    ratio floor (there is nothing to compare against a floor we do have).

    Returns `(reason, info)`; `info` carries floor_usd, ratio_pct, judged_usd and band_reason for
    the trace, which must keep reporting exactly what it reported before, plus
    floor_tolerance_pct and effective_floor_usd -- the boundary tolerance and the bar a buy was
    actually judged against, beside the nominal floor rather than instead of it.
    """
    # RESOLVED HERE, NOT BY THE CALLERS. Both the money path and the promotion shadow run this
    # function precisely so neither can restate the rule; a knob passed in by each of them
    # separately is a knob one of them will eventually forget.
    if floor_tolerance_pct is None:
        from sniper.settings import memecoin as _cfg

        floor_tolerance_pct = getattr(_cfg, "MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT", 0.0)
    # CLAMPED, never trusted: a negative tolerance would silently TIGHTEN a funded entry gate,
    # which is the one direction this change must never be able to move.
    try:
        tolerance = max(0.0, float(floor_tolerance_pct or 0.0))
    except (TypeError, ValueError):
        tolerance = 0.0
    # THE TRADER'S NET INTO THE COIN, not the one buy (operator 2026-09-13). Only ever raises the
    # judged amount; an unreadable history (None) or a net <= 0 judges the single buy as before.
    # The ratio floor and the absolute floor judge this same amount; the ceiling does not.
    judged_usd = their_usd
    if their_usd is not None and prior_net_usd is not None and prior_net_usd > 0:
        judged_usd = their_usd + prior_net_usd
    info = {"floor_usd": None, "ratio_pct": None, "judged_usd": judged_usd, "band_reason": None,
            "floor_tolerance_pct": tolerance, "effective_floor_usd": None,
            "effective_abs_floor_usd": (abs_min * (1.0 - tolerance / 100.0) if abs_min > 0 else None)}
    source_reason = None
    if not is_manual and family_active:
        if source_mcap is not None and ratio > 0:
            source_floor = source_mcap * ratio
            info["floor_usd"] = source_floor
            # The bar a buy is actually judged against. The NOMINAL floor keeps being reported
            # beside it, because the reason string has always quoted the configured rule and an
            # operator reading "under mcap floor 0.800% ($1493)" must still see that number.
            effective_floor = source_floor * (1.0 - tolerance / 100.0)
            info["effective_floor_usd"] = effective_floor
            ratio_pct = None if judged_usd is None else 100.0 * judged_usd / source_mcap
            info["ratio_pct"] = ratio_pct
            if judged_usd is None or judged_usd < effective_floor:
                if judged_usd is None:
                    rendered = "unknown"
                elif judged_usd != their_usd:
                    rendered = "$%.0f + $%.0f earlier = $%.0f (%.3f%%)" % (
                        their_usd, judged_usd - their_usd, judged_usd, ratio_pct)
                else:
                    rendered = "$%.0f (%.3f%%)" % (their_usd, ratio_pct)
                source_reason = "source buy %s under mcap floor %.3f%% ($%.0f)" % (
                    rendered, 100.0 * ratio, source_floor)
        elif source_mcap is None and unknown_mcap_floor > 0:
            info["floor_usd"] = unknown_mcap_floor
            if their_usd is None or their_usd < unknown_mcap_floor:
                rendered = "unknown" if their_usd is None else "$%.0f" % their_usd
                source_reason = "source buy %s under unknown-mcap floor $%.0f" % (
                    rendered, unknown_mcap_floor)
    # The band runs only when the ratio has not already refused, so one entry produces one reason.
    # UNKNOWN SIZE PASSES, the same posture as a missing m5 reading: a field the source did not
    # report says nothing about the trade.
    if source_reason is None and their_usd is not None and not is_manual:
        info["band_reason"] = source_buy_band_reason(their_usd, abs_min, abs_max, tolerance,
                                                     judged_usd=judged_usd)
        source_reason = info["band_reason"]
    return source_reason, info


def _band_source_buy_ratio(band_ratios, network_id, mcap, default):
    """The source-buy floor ratio for this coin's funded band; `default` when the band is unlisted."""
    from sniper.services.memecoin_chains import canonical_chain_id
    from sniper.services.memecoin_market_state import _coin_band
    band = _coin_band(mcap, False)
    chain = canonical_chain_id(network_id) or str(network_id or "")
    ratio = default
    for key in (f"{chain}:{band}".lower(), band):
        if key in band_ratios:
            ratio = band_ratios[key]
            break
    cap = cfg.MEMECOIN_REAL_SOURCE_BUY_FLOOR_CAP_USD_BY_CHAIN.get(chain, 0)
    if mcap is not None and mcap >= 250_000 and cap > 0:
        ratio = min(ratio, cap / mcap)
    return ratio


def _source_signature(origin_event_id):
    """The Solana source signature an attempt's origin names (onchain:<sig>, swap:raybot:<sig>:..,
    slotlane:<sig>), or None."""
    origin = str(origin_event_id or "")
    for prefix in ("onchain:", "slotlane:", "swap:raybot:"):
        if origin.startswith(prefix):
            return origin[len(prefix):].split(":", 1)[0] or None
    return None


def _fast_lane_refusal(ev, chain, trace):
    """The fast-lane-only refusal, carrying the slot lane's own decision for this source transaction
    when it made one (Alpha, 2026-10-06 night: jpegkite read native_fast_lane_unavailable while the
    slot lane had refused it at the source-buy floor). The reason identifier is unchanged; the
    decision rides as `slot_lane_skip` and the trace substage of the same name."""
    return _with_slot_lane_decision(
        {'status': 'rejected', 'chain': chain, 'reason': 'native_fast_lane_unavailable'}, ev, trace)


def _with_slot_lane_decision(res, ev, trace):
    """`res` plus the slot lane's own decision for this source transaction, when it made one, as
    `slot_lane_skip` and the trace substage of the same name. Display only: never changes or breaks
    the refusal. Also on a Solana missing_receipt_proof (Alpha 2026-10-07, STC 2dab63c2: the card
    said only missing_receipt_proof while the slot lane had refused slot_lane_price_unreadable)."""
    try:
        from sniper.services.memecoin_slot_skips import decision_for
        sig = _source_signature(getattr(ev, "fomo_event_id", None))
        decided = decision_for(sig) if sig else None
    except Exception:  # noqa: BLE001 - an explanation never changes or breaks the refusal
        logger.exception("slot lane decision lookup failed")
        decided = None
    if decided:
        outcome, why = decided
        res["slot_lane_skip"] = "%s: %s" % (outcome, why) if why else str(outcome)
        if trace is not None:
            try:
                trace.mark_substage("slot_lane_skip", outcome=outcome, reason=why)
            except Exception:  # noqa: BLE001
                logger.exception("slot lane decision not recorded")
    return res


def _wallet_mcap_ceiling(by_wallet, network_id, source_user_id, default):
    """The funded mcap ceiling for this wallet on this chain; `default` when no override is listed."""
    from sniper.services.memecoin_chains import canonical_chain_id
    chain = canonical_chain_id(network_id) or str(network_id or "")
    return by_wallet.get(f"{chain}:{source_user_id}".lower(), default)


_OWN_M5_REUSE_S = 5.0


def _real_now_s():
    from sniper.util.clock import real_utc_now
    return float(real_utc_now())


def _finite_number(value):
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _bigcap_lift(ev, network_id, mcap, ceiling, *, trace=None, evidence_fn=None):
    """The lifted ceiling for a would-be over_ceiling refusal, or None (today's refusal stands).

    BIG-CAP CEILING LIFT (operator GO via Alpha, 2026-10-06; settings MEMECOIN_REAL_BIGCAP_LIFT_*).
    Only on a listed chain, only over the GLOBAL ceiling (a per-wallet mcap ceiling is never raised),
    only up to the lift maximum, only for a source buy >= the minimum, and only when our OWN tape's
    5-minute volume (gate_evidence: local stores, no provider) is >= the ratio x mcap. That volume is
    a lower bound (known pools, the source transaction excluded by hash), so it can prove the bar and
    never invent it. ABSENT NEVER LIFTS: an unknown own volume keeps the refusal. Every decision on a
    listed chain lands on the attempt trace as `bigcap_lift` so it can be graded per chain."""
    from sniper.services.memecoin_chains import canonical_chain_id
    chain = canonical_chain_id(network_id) or str(network_id or "")
    if chain not in _evm_chain_knob(getattr(cfg, "MEMECOIN_REAL_BIGCAP_LIFT_CHAINS", ""),
                                    "MEMECOIN_REAL_BIGCAP_LIFT_CHAINS"):
        return None
    lift_max = _finite_number(getattr(cfg, "MEMECOIN_REAL_BIGCAP_LIFT_MAX_MCAP_USD", None))
    ratio = _finite_number(getattr(cfg, "MEMECOIN_REAL_BIGCAP_LIFT_MIN_V5_MCAP_RATIO", None))
    min_src = _finite_number(getattr(cfg, "MEMECOIN_REAL_BIGCAP_LIFT_MIN_SOURCE_USD", None))
    uid = getattr(ev, "source_user_id", None)
    rec = {"decision": "not_lifted", "chain": chain, "mcap": _finite_number(mcap),
           "ceiling": ceiling, "lift_max": lift_max, "min_v5_mcap_ratio": ratio,
           "source_usd": _finite_number(getattr(ev, "their_size_usd", None)), "min_source_usd": min_src}

    def done(why, lifted=None):
        rec["why"] = why
        if trace is not None:
            try:
                trace.mark_substage("bigcap_lift", **rec)
            except Exception:
                logger.exception("bigcap lift not recorded")
        return lifted

    if any(v is None or v <= 0 for v in (lift_max, ratio, min_src)):
        # Codex N9: an invalid threshold keeps the refusal; it never becomes a zero requirement.
        return done("config_invalid")
    if f"{chain}:{uid}".lower() in (getattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", None) or {}):
        return done("wallet_ceiling")
    if rec["mcap"] is None or rec["mcap"] <= 0:
        return done("mcap_unknown")
    if not ceiling or lift_max <= float(ceiling):
        return done("lift_not_above_ceiling")
    if rec["mcap"] > lift_max:
        return done("mcap_over_lift_max")
    if rec["source_usd"] is None or rec["source_usd"] < min_src or rec["source_usd"] <= 0:
        return done("source_under_min")
    try:
        if evidence_fn is None:
            from sniper.services.memecoin_volume_writer import gate_evidence as evidence_fn
        evidence = evidence_fn(chain, getattr(ev, "mint", None), getattr(ev, "fomo_event_id", None),
                               source_tokens=_source_tokens(ev))
    except Exception:
        logger.exception("bigcap lift: own m5 read failed mint=%s", getattr(ev, "mint", None))
        return done("own_v5_error")
    evidence = evidence if isinstance(evidence, dict) else {}
    rec.update(own_v5=evidence.get("usd"), v5_reason=evidence.get("reason"),
               v5_source="onchain_m5:%s" % (evidence.get("coverage") or "unknown"),
               v5_covered_s=evidence.get("covered_s"),
               v5_age_s=evidence.get("newest_block_age_s"),
               v5_measured_at=evidence.get("measured_at"))
    if evidence:
        try:
            ev._bigcap_own_m5 = (chain, evidence)           # the volume gate reuses it while fresh
        except Exception:
            pass
    usd = _finite_number(evidence.get("usd")) if evidence.get("usd") is not None else None
    if usd is None or usd < 0:
        return done("own_v5_unknown")                       # absent is never zero, never a pass
    if usd < ratio * rec["mcap"]:
        return done("own_v5_under_ratio")
    rec["decision"] = "lifted"
    return done("lifted", lift_max)


# THE TAPE REASONS THAT MEAN "NOT MEASURED" (one list for the entry gate and the revival watch;
# Gamma on 485333d25: a reason the tape returns but this list lacks falls through to the provider's
# m5, so the writer and the reader never meet). `source_tx_absent` is blind only after the entry
# gate's venue-gap verdict, which the revival watch does not run.
ONCHAIN_M5_BLIND_REASONS = ("window_incomplete", "feed_stale", "no_feed", "header_missing",
                            "no_known_pool", "quote_not_priceable", "source_tx_absent",
                            "protocol_lagging", "source_venue_untaped")


def _source_tokens(ev):
    """Whole tokens the source bought in its transaction, or None: the receipt proof's acquired
    amount when the event carries one, else J7's size over price (their_price is often post-trade,
    which under-counts and so errs toward fewer blinds). Rule 3 per venue (ULTRONICS 2026-10-07)."""
    fill = ((getattr(ev, "source_evidence", None) or {}).get("source_fill")
            if isinstance(getattr(ev, "source_evidence", None), dict) else None)
    try:
        if isinstance(fill, dict) and fill.get("acquired_atomic") is not None:
            value = int(fill["acquired_atomic"]) / 10 ** int(fill["token_decimals"])
            return value if math.isfinite(value) and value > 0 else None
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    size = _finite_number(getattr(ev, "their_size_usd", None))
    price = _finite_number(getattr(ev, "their_price", None))
    if size is None or price is None or size <= 0 or price <= 0:
        return None
    value = size / price
    return value if math.isfinite(value) else None


_TAPE_MOVE_CHAINS = ("4663", "8453")


def _read_tape_move_stamped(chain, mint, origin):
    from sniper.services import memecoin_evm_tape_marks as tape_marks
    from sniper.services.memecoin_volume_writer import _gate_source_tx
    started = time.perf_counter()
    measured_at_s = _real_now_s()
    try:
        tx = _gate_source_tx(chain, mint, origin)
        out = (tape_marks.source_move(chain, mint, tx, measured_at_s) if tx
               else {"move_pct": None, "why": "source_tx_unknown"})
    except Exception:
        logger.exception("tape move shadow read failed chain=%s mint=%s", chain, mint)
        out = {"move_pct": None, "why": "read_failed"}
    return {**out, "measured_at_s": measured_at_s, "ready_mono": time.monotonic(),
            "read_ms": round((time.perf_counter() - started) * 1000.0, 3)}


def _tape_move_payload(holder):
    future = holder.get("future")
    if future is None or not future.done():
        return {"move_pct": None, "why": holder.get("why", "read_not_ready")}
    try:
        out = dict(future.result(timeout=0))
        out.pop("ready_mono", None)
        return out
    except TimeoutError:
        return {"move_pct": None, "why": "queue_expired"}
    except Exception:
        return {"move_pct": None, "why": "read_failed"}


def _shadow_tape_move(trace, ev, chain, st):
    """Measurement only: queue local reads, then peek. Never wait or write from the worker."""
    if str(chain) not in _TAPE_MOVE_CHAINS or trace is None:
        return
    try:
        holder = getattr(trace, "tape_move_shadow", None)
        if holder is None:
            display = positive_price((st or {}).get("price"))
            source = positive_price(getattr(ev, "their_price", None))
            holder = {"decision_at_s": _real_now_s(), "decision_mono": time.monotonic(),
                      "display_move_pct": ((display / source - 1.0) * 100.0
                                           if display and source else None), "future": None}
            trace.tape_move_shadow = holder
            try:
                holder["future"] = _momentum_read_pool().submit(
                    _read_tape_move_stamped, str(chain), ev.mint, getattr(ev, "fomo_event_id", None))
            except queue.Full:
                holder["why"] = "queue_full"
        payload = _tape_move_payload(holder)
        holder["peeked"] = payload["why"] == "read_not_ready"
        trace.mark_substage("chase_tape_move_shadow", **payload,
                            decision_at_s=holder["decision_at_s"],
                            display_move_pct=holder["display_move_pct"], wait_ms=0.0)
    except Exception:
        logger.exception("tape move shadow not recorded for %s", getattr(ev, "mint", None))


def _tape_move_late_best_effort(trace):
    """At attempt finish, record already-ready late evidence on the trace's owner thread."""
    holder = getattr(trace, "tape_move_shadow", None) if trace is not None else None
    if not holder or not holder.get("peeked"):
        return False
    try:
        return trace.mark_substage(
            "chase_tape_move_shadow_late", **_tape_move_payload(holder),
            decision_at_s=holder["decision_at_s"], display_move_pct=holder["display_move_pct"],
            observed_phase="late_at_finish", wait_ms=0.0)
    except Exception:
        logger.exception("late tape move shadow not recorded")
        return False


def _source_usd_for_bound(ev):
    """The source's paid USD for the J7 tape bound (spec 2026-10-15 m5 provider lag, option d): the
    size the gate excludes, 0.0 when MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED is off (the gate then
    excludes nothing anywhere), None when it is on and the size is unknown (then no bound)."""
    if not getattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", False):
        return 0.0
    size = _finite_number(getattr(ev, "their_size_usd", None))
    return size if size is not None and size > 0 else None


def _own_m5_for_gate(ev, network_id, stats):
    """The volume gate's own-tape m5: the big-cap lift's read of this same event when it is under
    _OWN_M5_REUSE_S old, else a fresh gate_evidence read (a re-asked event never reuses a stale one).
    A J7 signal (no source tx) gets `source_tx_unknown` here with no read; its tape bound is read
    only if the gate is about to refuse (_m5_redecide_with_source_unknown_bound)."""
    from sniper.services.memecoin_volume_writer import gate_evidence
    memo = getattr(ev, "_bigcap_own_m5", None)
    if isinstance(memo, tuple) and len(memo) == 2 and memo[0] == str(network_id):
        at = _finite_number((memo[1] or {}).get("measured_at"))
        if at is not None and 0 <= _real_now_s() - at <= _OWN_M5_REUSE_S:
            return memo[1]
    return gate_evidence(network_id, ev.mint, getattr(ev, "fomo_event_id", None), stats=stats,
                         source_tokens=_source_tokens(ev), source_pool=_receipt_pool(ev))


def _m5_redecide_with_source_unknown_bound(decision, ev, network_id, stats, evidence, volume_kwargs,
                                           decide):
    """(decision, bound_evidence or None). THE J7 TAPE BOUND, ON A WOULD-BE REFUSAL ONLY (spec
    2026-10-15 m5 provider lag, option d; Alpha under CLAUDE.md rule 2): an admit never reads the
    tape. When `decision` refuses, the gate's evidence was `source_tx_unknown` and
    MEMECOIN_M5_SOURCE_UNKNOWN_TAPE_BOUND is on, the tape bound (total less the source's paid USD)
    becomes the m5 and h1 floor lower bounds in `volume_kwargs` and `decide()` runs the same
    volume_alive_decision again. The bound only raises organic volume, so this equals reading it
    up front: an admit is still an admit, and a refusal becomes the eager read's answer. A blind
    tape or an unknown source size changes nothing."""
    if (decision[0] or not isinstance(evidence, dict)
            or evidence.get("reason") != "source_tx_unknown"
            or not getattr(cfg, "MEMECOIN_M5_SOURCE_UNKNOWN_TAPE_BOUND", False)):
        return decision, None
    from sniper.services.memecoin_volume_writer import gate_evidence
    bound = gate_evidence(network_id, ev.mint, getattr(ev, "fomo_event_id", None), stats=stats,
                          source_usd=_source_usd_for_bound(ev))
    if not isinstance(bound, dict) or bound.get("basis") != "source_unknown_tape_bound":
        return decision, None
    volume_kwargs["m5_floor_lower_bound"] = bound.get("usd")
    volume_kwargs["h1_floor_lower_bound"] = onchain_h1_lower_bound(bound)
    return decide(), bound


def _receipt_pool(ev):
    """{pool, venue, mint_is_c1} from the source receipt's decode when it is already in hand (a peek,
    never a wait), else None. 56's tape reads the pool that moved the mint from it."""
    ref = getattr(ev, "evm_chase_reference", None)
    try:
        post = ref.payload() if ref is not None else None
    except Exception:
        return None
    if not isinstance(post, dict) or not post.get("pool"):
        return None
    return {"pool": post.get("pool"), "venue": post.get("venue"), "mint_is_c1": post.get("mint_is_c1")}


def _wallet_source_buy_ratio(network_id, source_user_id, default, *, by_wallet=None):
    """Solana wallet override after band selection. Zero is an explicit ratio waiver."""
    overrides = (cfg.MEMECOIN_SOLANA_MIN_SOURCE_BUY_MCAP_RATIO_BY_WALLET
                 if by_wallet is None else by_wallet)
    if not overrides:
        return default
    from sniper.services.memecoin_chains import canonical_chain_id
    if canonical_chain_id(network_id) != 'solana':
        return default
    return overrides.get(str(source_user_id or '').lower(), default)


def _wallet_mcap_floor(by_wallet, network_id, source_user_id, default):
    """The funded mcap FLOOR for this wallet on this chain; `default` when no override is listed."""
    from sniper.services.memecoin_chains import canonical_chain_id
    chain = canonical_chain_id(network_id) or str(network_id or "")
    return by_wallet.get(f"{chain}:{source_user_id}".lower(), default)


def _trial_for_entry(conn, ev, network_id):
    """The open trial's contract marker for this entry's (wallet, chain), or None. One read on the
    open-trial index. A failed read gives None: the reservation re-reads the trial inside its
    transaction and refuses an unmarked trial entry, so a miss here can refuse, never over-size."""
    try:
        row = m.open_trial(conn, getattr(ev, "source_user_id", None), network_id)
    except Exception:
        logger.exception("trial lookup failed wallet=%s chain=%s",
                         getattr(ev, "source_user_id", None), network_id)
        return None
    if row is None:
        return None
    from sniper.services.memecoin_trial import marker
    return marker(row)


def _pons_test_arm(conn, ev, network_id):
    """Arm the Pons creator-tax test for this entry and return its key, or None.

    Eligible only for a listed wallet on 4663, a Pons curve whose round trip is over the normal
    budget and within the test cap, and while fewer than MEMECOIN_PONS_TEST_MAX_TRADES funded test
    positions exist. A test position is a funded pons_v2 position whose stored round trip is over
    the normal budget: nothing else can be one, because the rung refuses those without the test.
    """
    if str(network_id) != "4663" or str(getattr(ev, "source_user_id", "") or "").lower() \
            not in cfg.MEMECOIN_PONS_TEST_WALLETS:
        return None
    try:
        import json as _json
        from sniper.execution import evm_pons_v2_quote as _pons
        from sniper.execution.evm_real_seams import _entry_tp_pct
        from sniper.execution.evm_v4_multihop import round_trip_within_budget
        tp = _entry_tp_pct(network_id)
        used = 0
        for (raw,) in conn.execute(
                "SELECT evm_route_json FROM memecoin_positions WHERE execution_mode='real' "
                "AND network_id='4663' AND evm_route_json LIKE '%pons_v2%'").fetchall():
            try:
                rt = (_json.loads(raw) or {}).get("round_trip_pct")
            except (TypeError, ValueError):
                continue
            if rt is not None and not round_trip_within_budget(rt, tp, chain="4663"):
                used += 1
        if used >= cfg.MEMECOIN_PONS_TEST_MAX_TRADES:
            return None
        rt = _pons.test_round_trip_pct(network_id, ev.mint)
        if rt is None or round_trip_within_budget(rt, tp, chain=str(network_id)) \
                or abs(rt) > cfg.MEMECOIN_PONS_TEST_MAX_ROUND_TRIP_PCT:
            return None
        key = (str(network_id), str(ev.mint).lower())
        _pons.TEST_ALLOW[key] = cfg.MEMECOIN_PONS_TEST_MAX_ROUND_TRIP_PCT
        logger.warning("pons creator-tax test armed mint=%s round_trip=%.2f%% used=%d/%d clip=$%.0f",
                       ev.mint, rt, used, cfg.MEMECOIN_PONS_TEST_MAX_TRADES,
                       cfg.MEMECOIN_PONS_TEST_CLIP_USD)
        return key
    except Exception:
        logger.exception("pons creator-tax test check failed for %s; normal budget applies", ev.mint)
        return None


def over_nano_chase_cap(cap, network_id, is_nano):
    """The quote chase cap after the Robinhood above-nano ceiling. 0 means no cap.

    Runs AFTER the chase_past_entry waiver on purpose: the waived wallets' trades are in the losing
    band this ceiling removes (settings MEMECOIN_REAL_4663_OVER_NANO_QUOTE_CHASE_MAX_PCT).
    """
    ceiling = float(getattr(cfg, "MEMECOIN_REAL_4663_OVER_NANO_QUOTE_CHASE_MAX_PCT", 0) or 0)
    if str(network_id) != "4663" or is_nano or ceiling <= 0:
        return cap
    cap = float(cap or 0)
    return ceiling if cap <= 0 else min(cap, ceiling)


def _market_data_unavailable_reason(network_id):
    """None when a market-data provider exists for `network_id`; else the honest refusal reason.

    An allowlisted chain with no FOMO_TO_DEX entry gets `st = {}` from every read on the entry
    path, and the gates downstream (m5/h1 volume, execution premium, the chase gate, sell-share
    cap, coin-age ceiling, winner-re-entry price) fail OPEN on missing data by design — correct
    for an occasional gap on a covered chain, wrong for a chain with no provider at all. This is
    the one check that turns "every gate quietly admits" into a stated refusal, so a future
    chain added to MEMECOIN_EVM_ALLOWLIST without its FOMO_TO_DEX entry cannot trade blind.
    """
    from sniper.execution.solana_quote import FOMO_TO_DEX
    chain = str(network_id or "")
    if chain in FOMO_TO_DEX:
        return None
    return f"no market-data provider for chain {chain} (not in FOMO_TO_DEX)"


def _decision_label(kind, ev):
    """The funded RPC call counter's label for one decision: its kind and its lane."""
    lane = getattr(ev, "delivery_lane", None) or getattr(ev, "signal_source", None) or "unknown"
    return "%s:%s" % (kind, lane)


def _entry_rpc_labelled(fn):
    """Label the decision thread's funded RPC requests (spec 2026-10-01 burst coalescing §5).
    Telemetry only: the label is thread-local and restored on return."""
    @functools.wraps(fn)
    def wrapped(conn, ev, *args, **kwargs):
        with _rpc_label(_decision_label("entry", ev)):
            return fn(conn, ev, *args, **kwargs)
    return wrapped


def _exit_rpc_labelled(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with _rpc_label("exit:%s" % kwargs.get("lane", "periodic_sweep")):
            return fn(*args, **kwargs)
    return wrapped


def _entry_startup_refusal(contexts, network_id):
    context = (contexts or {}).get(network_id)
    ready = context.get('entry_ready_fn') if isinstance(context, dict) else None
    if ready is not None and not ready():
        return {'status': 'deferred', 'chain': network_id, 'reason': 'shyft_handover_pending'}
    return None


def cached_solana_volume_decision(cache, ev, *, mcap_usd, now_iso):
    """The ordinary lane uses the native lane's cache and already-published SOL/USD."""
    if cache is None:
        return None
    from sniper.services.memecoin_slot_signal import PUMP_PROGRAM, PUMP_AMM_PROGRAM
    program = getattr(ev, "source_program", None)
    if program == PUMP_PROGRAM:
        venue, pool = "pump_curve", ev.mint
    elif program == PUMP_AMM_PROGRAM:
        venue, pool = "pump_amm", getattr(ev, "source_pool", None)
    else:
        return None
    from sniper.services.memecoin_live_marks import WSOL, fresh_quote_usd
    return cache.check(mint=ev.mint, venue=venue, market_pool=pool,
        source_signature=m.source_tx_of(getattr(ev, "fomo_event_id", None)),
        mcap_usd=mcap_usd, sol_usd=fresh_quote_usd((WSOL,), now_iso=now_iso).get(WSOL))


def solana_volume_status_for_contexts(contexts):
    """Heartbeat reads the background worker's latest published status without I/O."""
    cache = (context_for(contexts, "solana") or {}).get("solana_volume_cache")
    return cache.status() if cache is not None else {
        "state": "unknown" if cfg.MEMECOIN_SOLANA_VOLUME_GATE_ENABLED else "disabled",
        "pools": 0, "last_error": None}


@_entry_rpc_labelled
def real_entry_hook(conn, ev, contexts, *, now_fn, trade_size, max_age_s, stats_fn=None,
                    impact_cap_pct=None, resolved_identity=None, trace=None, trace_clock=None,
                    decision_at=None, resolve_fn=None, classification_context=None,
                    cluster_decide_fn=None):
    """One curated BUY on the real path: drop it if the pump is already over (tight staleness), skip
    it if the coin has already RUN away from the trader's entry (chase guard), else run real_entry
    (the network router). base58 -> the Solana context; 0x -> the EVM dispatch. Entry alerts fire
    ONCE here on the final result (works for either venue)."""
    trace_anchor = decision_at or now_fn()
    # Bound ONCE, before any gate reads it: the selection chain below must make one consistent
    # decision about what kind of entry this is. See is_manual_entry for the gate split.
    _manual_entry = is_manual_entry(ev)
    # THE GLOBAL FEED IS A TELESCOPE, NOT A TRADING SIGNAL (2026-09-06, operator approved).
    # Bound here with the other once-per-entry selection facts. Measured over the 14 days to
    # 2026-09-06: global_feed produced 16 real fills for -$79.9 and 6,383 paper positions for
    # -$8,772.6, while follow_activity produced 394 real fills for +$942.8. It stays running
    # because it is the ONLY way we see wallets we do not already follow -- 24 armed wallets came
    # from it -- but it should not open funded positions. A manual entry is never gated by this:
    # the operator asking for a coin is not the firehose.
    if (not _manual_entry
            and str(getattr(ev, "signal_source", "") or "") == "global_feed"
            and not bool(getattr(cfg, "MEMECOIN_REAL_GLOBAL_FEED_ENTRIES", True))):
        # TERMINATE THE TRACE (2026-09-08). This was the only refusal in this function that did
        # not, and `_hook_raw` cannot cover it: it sets `handed_off = True` before calling us
        # precisely because the hook owns the trace from here. 33 attempt rows in one day carried
        # `terminal_status IS NULL` — a decision started and no verdict ever recorded — and every
        # one was the global-feed lane. `identity` is not bound yet and is not needed: the caller
        # already wrote the network, and this gate judges the signal's ORIGIN, not the coin.
        res = {"status": "rejected", "reason": "global_feed entries disabled"}
        _finish_trace_for_result(trace, res, identity=resolved_identity)
        return res
    # PER-WALLET SELECTION EXEMPTIONS (2026-08-28, operator order). Resolved ONCE, here, for the
    # same reason _manual_entry is: the selection chain must make one consistent decision about
    # what this entry is excused from. Read-only, fail-closed to the empty set, and every family
    # in it is a SELECTION gate — the reader filters against EXEMPTIBLE_GATE_FAMILIES, so a row
    # naming a safety gate can never arrive here.
    try:
        _exempt = m.entry_gate_exemptions(
            conn, getattr(ev, "source_user_id", None),
            getattr(resolved_identity, "network_id", None) or getattr(ev, "network_id", None)
            or ("solana" if not str(ev.mint or "").startswith("0x") else None))
    except Exception:
        logger.exception("exemption lookup failed for %s — applying every gate", ev.mint)
        _exempt = frozenset()
    _waived_decisive: set[str] = set()
    _waived_unevaluated: set[str] = set()

    def _waived(family: str) -> bool:
        """True if this wallet is excused from `family`, recording the waiver as DECISIVE.

        CALL THIS AS THE LAST CONJUNCT of a gate's condition and nowhere else. Python short-
        circuits, so reaching this call means every other part of the gate was already true — the
        gate WOULD have refused and the waiver is what changed the outcome. Called earlier in a
        condition, it records a waiver that may have changed nothing, which is the defect this
        replaces: the field stored every configured exemption, so it answered "what was on" when
        the question is "what mattered".
        """
        if family in _exempt:
            _waived_decisive.add(family)
            return True
        return False

    def _waived_early(family: str) -> bool:
        """True if excused, recording the waiver as UNEVALUATED.

        For the two chase gates, whose predicates sit AFTER the exemption check — one calls
        chase_ok(), the other does a first_seen_buy_price() lookup. When the waiver short-circuits
        them the predicate never runs, so whether the gate would have refused is genuinely
        unknown. Moving the predicate first would put a DB read on the funded entry critical path
        to serve an attribution field, which is the wrong trade. So the waiver is recorded as
        unknown rather than counted as decisive.
        """
        if family in _exempt:
            _waived_unevaluated.add(family)
            return True
        return False

    if trace is None:
        trace = _start_trace_best_effort(conn, ev, trace_anchor, trace_clock)
    _set_entry_shadow_context(trace, ev, trade_size)
    identity = resolved_identity
    try:
        if identity is None:
            identity = resolve_entry_identity(conn, ev, resolve_fn=resolve_fn)
        _mark_trace_best_effort(trace, "chain_resolution")
        if identity.status != "resolved":
            res = _identity_failure_result(identity)
            _finish_trace_for_result(trace, res, identity=identity)
            return res
        _entry_window = None
        if not _manual_entry and _first_buy_window_applies(ev, identity.network_id):
            if getattr(ev, "revival_reask", False):
                _window_reason = "source_buy_timer_retry_disabled"
            else:
                _entry_window, _window_reason = entry_opportunity.event_window(
                    conn, ev, observed_at=trace_anchor)
            if _window_reason:
                res = {"status": "rejected", "reason": _window_reason}
                _finish_trace_for_result(trace, res, identity=identity)
                return res
            if trace is not None:
                trace.mark_substage("source_entry_window", **_entry_window)
        startup_refusal = _entry_startup_refusal(contexts, identity.network_id)
        if startup_refusal is not None:
            _finish_trace_for_result(trace, startup_refusal, identity=identity)
            return startup_refusal
        # FAIL-CLOSED MARKET-DATA GATE, runs before any gate below can read a market-data field.
        # Must be here, not folded into a downstream gate's fail-open path — this checks whether
        # the chain has a provider AT ALL, not whether one poll came back empty. SELECTION, like
        # every coin-quality gate this hook owns (see test_memecoin_manual_gate_bypass.py): a
        # manual click is the operator's own judgement of the coin, replacing the automated one.
        _no_market_data = _market_data_unavailable_reason(identity.network_id)
        if _no_market_data and not _manual_entry:
            res = {"status": "rejected", "reason": _no_market_data}
            _finish_trace_for_result(trace, res, identity=identity)
            return res

        # The early time is only the trace anchor. Resolution and mcap work may block; funded
        # staleness, risk-day, reservation, and execution stamps use a fresh real-UTC sample.
        _crowd = (getattr(ev, "signal_source", None) == "desk"
                  and (getattr(ev, "desk", None) or {}).get("cohort_id") == "large-buyer-crowd-v1")
        _crowd_mono = time.monotonic() if _crowd else None
        now_iso = now_fn()
        if _crowd:
            ev._crowd_clock_anchor = (now_iso, _crowd_mono)
        if is_stale_entry(getattr(ev, "created_at", None), now_iso, max_age_s):
            _mark_trace_best_effort(trace, "market_data")
            _persist_attempt_classification(
                conn, trace,
                _classify_funded_draft(
                    conn, ev, identity, now_iso, classification_context,
                    market_stats=(classification_context or {}).get("market_stats"),
                ),
            )
            res = {"status": "stale", "reason": f"entry older than {max_age_s:g}s"}
            _alert_entry_result(ev.mint, trade_size, res,
                                handle=getattr(ev, "source_handle", None),
                                ticker=getattr(ev, "ticker", None), armed=_wallet_is_armed(conn, ev, identity))
            _finish_trace_for_result(trace, res, identity=identity)
            return res

        # SOURCE-BUY CONVICTION FLOOR. A copied buy must be a minimum fraction of the entry market
        # cap. A coin without a usable mcap uses the operator's smaller fixed fallback. Manual buys
        # have no copied source trade. A wallet exemption remains a deliberate selection override.
        ratio = max(0.0, float(cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO or 0.0))
        unknown_mcap_floor = max(
            0.0, float(cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD or 0.0))
        # ABSOLUTE SOURCE-BUY BAND (2026-09-08, operator). Both default 0 = OFF.
        #
        # The ratio above is a FRACTION OF MARKET CAP and it runs backwards across coin sizes:
        # measured on armed-wallet buys since 09-01 the configured 0.008 passes 97.6% at nano
        # (<$50k mcap, median buy $500) and refuses 84% at mid ($1-5M, median buy $4,996). It
        # refuses least where the coins are smallest. There was no absolute floor and no upper
        # bound at all.
        #
        # These ADD to the ratio, they do not replace it: the ratio carries its own signal (the
        # 0.01-0.03 bucket is +$844.78 over n=327 while the configured 0.008 sits inside a LOSING
        # 0.005-0.01 bucket, -$262.09 over n=137), so the operator sets the combination.
        #
        # WHAT THE UPPER BOUND IS AND IS NOT. Buys >= $2,500 lose -$453 over n=89, but split by
        # coin size that is n=10 +$1.45 under $250k mcap and n=79 -$454.64 at or above it. Source
        # size is proxying for COIN size. The ceiling is worth having because it is half of a
        # replicated conjunction — turnover 10-50% with a $1k-$10k source buy, n=44, +21.72%,
        # 75% win, surviving both chains, both time halves and the removal of the best two days
        # and of Degen — NOT because a large buy is independently bad. Do not read it that way.
        abs_min = cfg.min_source_buy_usd_for(getattr(identity, "network_id", None) or ev.network_id,
                                             getattr(ev, "source_user_id", None))
        abs_max = max(0.0, float(getattr(cfg, "MEMECOIN_REAL_MAX_SOURCE_BUY_USD", 0.0) or 0.0))
        if trace is not None:
            # MEASUREMENT ONLY (2026-09-28): splits identity/staleness from the source-buy family,
            # whose one read is the cumulative prior-net query on memecoin_events.
            _mark_probe(trace, "probe_pre_source_floor")
        # THE ADD-ON REFUSAL (memecoin_addon_guard, operator 2026-10-17): judged from the in-memory
        # ring (no ledger read unless it says refuse), then this buy is noted, before any quote. Same
        # rule on every chain this hook serves; the slot lane judges its own buys the same way.
        if getattr(ev, "side", "buy") == "buy" and not _manual_entry:
            from sniper.services.memecoin_addon_guard import RING as _addon_ring, held_in_ledger, judge as _addon_judge
            _net = getattr(identity, "network_id", None) or getattr(ev, "network_id", None)
            _tx = m.source_tx_of(getattr(ev, "fomo_event_id", None))
            _addon = _addon_judge(_net, getattr(ev, "source_user_id", None), ev.mint, _tx,
                                  getattr(ev, "their_size_usd", None),
                                  held_fn=lambda: held_in_ledger(conn, _net, ev.mint))
            _addon_ring.note_buy(_net, getattr(ev, "source_user_id", None), ev.mint, _tx,
                                 getattr(ev, "their_size_usd", None), getattr(ev, "their_price", None))
            if _addon is not None:
                if trace is not None:
                    trace.mark_substage("addon_refusal", **_addon[1])
                res = {"status": "rejected", "reason": _addon[0]}
                _alert_entry_result(ev.mint, trade_size, res, handle=getattr(ev, "source_handle", None),
                                    ticker=getattr(ev, "ticker", None), armed=_wallet_is_armed(conn, ev, identity))
                _finish_trace_for_result(trace, res, identity=identity)
                return res
        source_reason = None
        source_floor = None
        source_mcap = None
        source_ratio_pct = None
        source_band_reason = None
        prior_net = None
        # HOISTED so the absolute band can run with the ratio off, and so the trace no longer has
        # to ask `"their_usd" in locals()` for a name that was conditionally bound.
        #
        # THIS IS THE SOURCE'S OWN REPORTED SIZE, not our fill and not exact. The conjunction was
        # measured on this field, so the gate must read this field; replacing it with a computed
        # value would stop it measuring what the evidence measured.
        their_usd = getattr(ev, "their_size_usd", None)
        try:
            their_usd = None if their_usd is None else float(their_usd)
        except (TypeError, ValueError):
            their_usd = None
        if their_usd is not None and (not math.isfinite(their_usd) or their_usd < 0):
            their_usd = None
        band_ratios = getattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", None) or {}
        if not is_manual_entry(ev) and (
                ratio > 0 or unknown_mcap_floor > 0 or band_ratios):
            for candidate_mcap in (
                    (classification_context or {}).get("effective_mcap"),
                    getattr(ev, "their_market_cap", None),
            ):
                try:
                    candidate_mcap = float(candidate_mcap)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(candidate_mcap) and candidate_mcap > 0:
                    source_mcap = candidate_mcap
                    break
            if source_mcap is not None:
                ratio = _band_source_buy_ratio(
                    band_ratios, getattr(identity, "network_id", None) or ev.network_id,
                    source_mcap, ratio)
        ratio = _wallet_source_buy_ratio(
            getattr(identity, "network_id", None) or ev.network_id,
            getattr(ev, "source_user_id", None), ratio)
        # THE CUMULATIVE READ, the family's only I/O (the decision itself stays pure). Made when
        # the ratio leg will judge the net, AND when the absolute floor would refuse the single buy
        # (degen_ak AGNET 4663, 2026-10-02): the floor judges the same net, with the ratio family
        # on or off. A buy that clears the floor alone with the ratio off reads nothing, as before
        # (the read is p50 0.13 ms / p90 0.25 ms on 202 production traces, 2026-10-01/02).
        if not is_manual_entry(ev) and their_usd is not None and (
                (source_mcap is not None and ratio > 0) or (abs_min > 0 and their_usd < abs_min)):
            window = float(getattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", 0) or 0)
            if window > 0:
                prior_net = m.source_prior_net_buy_usd(
                    conn, source_user_id=getattr(ev, "source_user_id", None),
                    network_id=getattr(identity, "network_id", None) or getattr(ev, "network_id", None),
                    mint=getattr(ev, "mint", None), before_iso=getattr(ev, "created_at", None),
                    window_s=window, exclude_event_id=getattr(ev, "fomo_event_id", None),
                    judged_usd=their_usd)
        # ONE EVALUATOR, TWO CALLERS. The promotion shadow calls this same function with its own
        # causal inputs; it used to run only the absolute band and pass buys this refuses.
        source_reason, _source_info = source_buy_family_reason(
            their_usd=their_usd, source_mcap=source_mcap, prior_net_usd=prior_net, ratio=ratio,
            unknown_mcap_floor=unknown_mcap_floor, abs_min=abs_min, abs_max=abs_max,
            family_active=bool(ratio > 0 or unknown_mcap_floor > 0 or band_ratios),
            is_manual=is_manual_entry(ev))
        source_floor = _source_info["floor_usd"]
        source_ratio_pct = _source_info["ratio_pct"]
        source_band_reason = _source_info["band_reason"]
        if trace is not None:
            try:
                trace.mark_substage(
                    "source_buy_floor",
                    source_size_usd=their_usd,
                    # WHAT WAS JUDGED, NOT ONLY THIS BUY (2026-09-25, SHI 4663). A $877 buy was
                    # admitted on $1,232 bought earlier in the window; the row showed the $877 and
                    # ratio_pct 0.83% with nothing to connect them, and read as nonsense.
                    judged_usd=_source_info["judged_usd"],
                    prior_net_usd=prior_net,
                    effective_mcap=source_mcap,
                    ratio_pct=source_ratio_pct,
                    floor_usd=source_floor,
                    # BOTH FLOORS, because only one of them decided. `floor_usd` is the configured
                    # rule and `effective_floor_usd` is the bar this buy was actually judged
                    # against; with the tolerance at 0 they are equal and the trace reads exactly
                    # as it always did. Without the effective one on the row, a buy admitted by the
                    # tolerance is indistinguishable from one that simply cleared the floor.
                    effective_floor_usd=_source_info["effective_floor_usd"],
                    effective_abs_floor_usd=_source_info["effective_abs_floor_usd"],
                    floor_tolerance_pct=_source_info["floor_tolerance_pct"],
                    rejected=bool(source_reason),
                    configured_mcap_ratio=ratio,
                    configured_unknown_mcap_floor_usd=unknown_mcap_floor,
                    configured_abs_min_usd=abs_min,
                    configured_abs_max_usd=abs_max,
                    rejected_by_band=bool(source_band_reason),
                    inactive_state=("disabled"
                                    if ratio <= 0 and unknown_mcap_floor <= 0
                                    and abs_min <= 0 and abs_max <= 0
                                    else "out_of_scope"),
                )
            except Exception:
                logger.exception("source buy floor trace failed")
        if source_reason is not None and not _manual_entry and not _waived("source_buy_floor"):
            _persist_attempt_classification(
                conn, trace,
                _classify_funded_draft(
                    conn, ev, identity, now_iso, classification_context,
                    market_stats=(classification_context or {}).get("market_stats"),
                ),
            )
            res = {"status": "rejected", "reason": source_reason}
            _alert_entry_result(ev.mint, trade_size, res,
                                handle=getattr(ev, "source_handle", None),
                                ticker=getattr(ev, "ticker", None),
                                armed=_wallet_is_armed(conn, ev, identity))
            _finish_trace_for_result(trace, res, identity=identity)
            return res

    # CHASE GUARD. The real path had none — its only protection against an already-pumped signal was
    # the staleness gate. That is adequate for the live feed, but follow-activity polls every 300s, so
    # its signals are 120-900s old BY CONSTRUCTION and the coin has had minutes to run. Buying a coin
    # that already doubled since the trader bought is not copying him, it is buying his exit liquidity.
    # Fail-OPEN when we cannot price it: a gate that fires on ABSENT data is the bug class that cost
    # us the whole EVM leg (honeypot) and the sub-$1M winners (mcap on feed gaps).
        cached_stats = ((classification_context or {}).get("market_stats")
                        if classification_context is not None else None)
        st = _snapshot_priced(dict(cached_stats or {}))
        _set_event_market_ticker(ev, st)
        our_price = st.get("price")
        # Resolve the chase limit before the optional market-data read.
        # The executor uses this limit when the display price is absent.
        event_chase_cap = (cfg.MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT
                           if is_fresh_follow_event(ev, cfg.MEMECOIN_REAL_FRESH_FOLLOW_MAX_LAG_S)
                           else cfg.MEMECOIN_REAL_CHASE_MAX_PCT)
        # A wallet with a MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET entry has its band lifted to
        # that value on every chain (2026-10-08, 397397); everyone else's is unchanged. The display
        # pre-check and the quote cap use the lifted cap; `band_without` runs the same steps from the
        # cap WITHOUT the override, for Gamma's copy_cap_override trace (663c9882) only.
        event_chase_cap_base = event_chase_cap
        event_chase_cap = cfg.wallet_band_cap_pct(
            getattr(ev, "source_user_id", None), identity.network_id, event_chase_cap)
        # Fetch market stats whenever we CAN, not only when the chase cap is armed. The two were
        # one condition, so turning the cap off also turned the market-data read off: the
        # classifier then saw `market_stats={}`, mcap became unknown, MEMECOIN_REAL_MCAP_ASSUME_MICRO
        # promoted it to micro, and the operator's mcap CEILING silently stopped applying. Disabling
        # a chase guard must never widen the size gate.
        if stats_fn is not None:
            if cached_stats is None:
                if identity.network_id == "solana":
                    _start_early_entry_quote(                      # option 4: quote rides it too
                        conn, ev, contexts, trace,
                        _start_entry_prereads(ev, contexts, trace))    # B: reads ride the GET
                else:
                    # A SHADOW EVM DECISION READS NOTHING IT WILL NOT USE (2026-09-29): the EVM
                    # native balance is consumed only by the leg, which shadow never reaches.
                    if getattr(ev, "delivery_lane", None) != J7_SHADOW_LANE:
                        pre = _start_entry_prereads(ev, contexts, trace, chain=identity.network_id)
                        _start_early_evm_quote(conn, ev, contexts, trace, pre, identity.network_id)
                    _start_evm_post_trade_read(ev, identity.network_id, trace=trace)
                try:
                    st = stats_fn(ev.mint, network_id=identity.network_id) or {}
                except TypeError:
                    st = stats_fn(ev.mint) or {}
                except Exception:
                    st = {}
            st = _snapshot_priced(st or {})
            _set_event_market_ticker(ev, st)
            our_price = (st or {}).get("price")
            _mark_trace_best_effort(trace, "market_data")
            _mark_pair_selection(trace, st)
            # PRE-LADDER PROBE, MEASUREMENT ONLY (2026-09-27, Alpha GO). The stretch from here to
            # chase_first_seen reads p50 188 ms on funded 4663 fills while its work times at a few
            # ms alone; a timed SELECT 1 on this connection says whether it was busy.
            # SOLANA TOO (2026-09-28, Alpha GO): J7 Solana's reservation stage is p50 288 ms with no
            # network call in it (DB reads and writes only), and nothing split it.
            _probe_evm = trace is not None
            if _probe_evm:
                _probe_at = time.monotonic()
                try:
                    conn.execute("SELECT 1").fetchone()
                except Exception:
                    pass
                _mark_probe(trace, "probe_conn_select1", (time.monotonic() - _probe_at) * 1000.0)
            # PRE-ENTRY FLOW, OBSERVATION ONLY (2026-08-25). Records what the local Solana tape
            # saw on this coin before we decided. It gates NOTHING: no threshold exists, and one
            # must not be invented from a distribution -- the working precedent here
            # (execution_premium_decision) took its 2.0x from a realized -52.14% cliff. This is
            # how we accumulate the outcomes needed to derive one.
            # Lands in trace_json.substage_events, NOT in the classification: `classification` is
            # a funded-contract field (memecoin_funded_reconcile.py), so adding to it would change
            # the contract hash and could break reconciliation.
            # Measured 0.23-0.54 ms per call on the live tape (the 0.05 ms figure is
            # pool_flow_window alone, on an already-open handle; this opens its own read-only
            # connection). Every failure returns None, is SWALLOWED, and is COUNTED -- see
            # OBSERVE_COUNTS: an observer that silently returns None forever must not look like a
            # quiet market.
            if identity.network_id == "solana":
                # SIGNAL-TIME POOL RESOLVE, enqueued BEFORE the gates and independent of the
                # outcome: a refusal wants the pool as much as a fill does. Cheap and synchronous
                # (an indexed lookup and a list append); the RPC happens on the resolver's thread.
                if getattr(cfg, "MEMECOIN_SIGNAL_POOL_RESOLVER_ENABLED", False):
                    try:
                        import time as _sprt
                        from sniper.services import memecoin_signal_pool_resolver as _spr
                        _spr.enqueue(
                            ev.mint, first_seen=getattr(ev, "created_at", None),
                            now=_sprt.monotonic(),
                            ttl_s=getattr(cfg, "MEMECOIN_SIGNAL_POOL_RESOLVER_TTL_S", 900.0),
                            max_pending=getattr(
                                cfg, "MEMECOIN_SIGNAL_POOL_RESOLVER_MAX_PENDING", 64))
                    except Exception:
                        pass                   # observation must never touch a funded entry
                try:
                    from sniper.services.memecoin_solana_tape import observe_pre_entry_flow
                    _flow = observe_pre_entry_flow(ev.mint, now_iso=now_iso)
                    if _flow and trace is not None:
                        trace.mark_substage(
                            "pre_entry_flow",
                            swaps_n=_flow.get("swaps_n"), makers_n=_flow.get("makers_n"),
                            buy_n=_flow.get("buy_n"), sell_n=_flow.get("sell_n"),
                            gross_quote=_flow.get("gross_quote"),
                            reserve_quote=_flow.get("reserve_quote"),
                            turnover=_flow.get("turnover"),
                            maker_ratio=_flow.get("maker_ratio"),
                            reserve_at=_flow.get("reserve_at"))
                except Exception:
                    pass                       # never let observation touch a funded entry
            # SIGNAL-TIME PoolKey WARM for the EVM chains, the same shape as the Solana resolve
            # above and for the same reason: enqueued BEFORE the gates, independent of the
            # outcome, cheap and synchronous. A stored PoolKey answers in ~10ms where a live
            # resolve costs ~840ms and fails under load — and of 475 funded 4663 attempts that
            # reserved capital over 48h, 64 filled while 153 died on "no quote". This warms the
            # coin arriving NOW rather than one we already hold.
            if (str(identity.network_id) != "solana"
                    and getattr(cfg, "MEMECOIN_EVM_KEY_PREWARM_ENABLED", False)
                    and getattr(ev, "delivery_lane", None) != J7_SHADOW_LANE):   # the lane warms its own
                try:
                    import time as _kpt
                    from sniper.services import memecoin_evm_key_prewarm as _kp
                    _kp.enqueue(
                        identity.network_id, ev.mint, now=_kpt.monotonic(),
                        ttl_s=getattr(cfg, "MEMECOIN_EVM_KEY_PREWARM_TTL_S", 900.0),
                        max_pending=getattr(cfg, "MEMECOIN_EVM_KEY_PREWARM_MAX_PENDING", 64))
                except Exception:
                    pass                       # observation must never touch a funded entry
            if _probe_evm:
                _mark_probe(trace, "probe_pre_classify")
            classification = _classify_funded_draft(
                conn, ev, identity, now_iso, classification_context, market_stats=st)
            if _probe_evm:
                _mark_probe(trace, "probe_classified")
            fresh_follow = is_fresh_follow_event(
                ev, cfg.MEMECOIN_REAL_FRESH_FOLLOW_MAX_LAG_S)
            # THE EVM PRE-QUOTE REFERENCE (2026-09-28): the source's post-trade price when it was
            # taken on the pool whose DISPLAY price this leg compares; else the average fill.
            _pre_ref = None
            if getattr(ev, "evm_chase_reference", None) is not None:
                from sniper.services.memecoin_evm_post_trade import pre_quote_reference
                _pre_ref, _pre_basis, _pre_note = pre_quote_reference(ev, st)
                # Knob off: note None and avg_fill, and the trace stays today's (Beta F1).
                if trace is not None and (_pre_note is not None or _pre_basis != "avg_fill"):
                    try:
                        trace.mark_substage("chase_pre_quote_reference", reference_price=_pre_ref,
                                            reference_basis=_pre_basis, reference_note=_pre_note,
                                            source_price=getattr(ev, "their_price", None))
                    except Exception:
                        pass                   # a trace mark never decides an entry
                if _pre_basis == "avg_fill":
                    _pre_ref = None
            # E3 (DexScreener out, phase A): an id so the switch is one edit. KEPT ON by decision
            # (2026-09-28): the coins it alone refused fell a median 23% in five minutes.
            if (event_chase_cap > 0 and not _manual_entry
                    and not cfg.dexscreener_off("E3", identity.network_id)
                    and not _waived_early("chase_past_entry") and not chase_ok(
                    ev, our_price=our_price, max_pct=event_chase_cap, reference=_pre_ref)):
                # WOULD-REFUSE ONLY. The extra quote fires here and nowhere else, so the accept
                # path's quote budget is unchanged — measured at 4.3 of these a day.
                #
                # It runs on the FUNDED key deliberately: this prices a live funded entry decision,
                # which is funded work. It is not measurement riding the funded budget.
                from sniper.execution.solana_real_seams import jupiter_entry_quote
                _src_impact, _impact_why = chase_impact_allowance(
                    ev, network_id=identity.network_id,
                    quote_fn=lambda usd: jupiter_entry_quote(ev.mint, usd))
                _eff_cap = effective_chase_cap(
                    event_chase_cap, _src_impact,
                    ceiling_pct=cfg.MEMECOIN_CHASE_SOURCE_IMPACT_MAX_PCT)
                gap = chase_gap_pct(ev, our_price=our_price, reference=_pre_ref)
                # MEASUREMENT ONLY. Never read on this pass; recorded so the reference-size and
                # estimator questions get answered from the REFUSED population instead of from
                # judgement. Both inputs are already in hand here, so this adds no network call and
                # nothing that can fail slowly. An absent input is recorded as absent, never
                # substituted.
                _chase_liq, _model_impact = None, None
                try:
                    _chase_liq = _telemetry_liquidity(st, classification, identity.network_id)
                    _model_impact = model_impact_pct(
                        getattr(ev, "their_size_usd", None), _chase_liq)
                except Exception:
                    # A telemetry failure must never change a refusal.
                    logger.exception("model impact telemetry failed for %s", ev.mint)
                # RECORD ALL FOUR, so the refusal grading can split "the gate was right" from
                # "the gate charged him his own footprint". A decision recorded only as its
                # outcome cannot be graded against the alternative.
                try:
                    m.update_entry_attempt_fields(
                        conn, getattr(trace, "attempt_id", None),
                        chase_naive_premium_pct=gap,
                        chase_source_impact_pct=_src_impact,
                        chase_effective_cap_pct=_eff_cap,
                        chase_base_cap_pct=event_chase_cap,
                        # The INPUT verbatim, not just the derived number: the registered control
                        # recomputes the extended cap over historical refusals, and a recompute
                        # that re-reads the size from a table that has since changed would grade a
                        # different rule than the one that ran.
                        chase_source_size_usd=(float(getattr(ev, "their_size_usd", 0) or 0)
                                               or None),
                        chase_impact_formula=SOURCE_IMPACT_FORMULA,
                        # WHY there was no allowance. Without it a refusal with impact=None needs
                        # a live probe to explain, which is exactly what it cost.
                        chase_impact_skip_reason=_impact_why,
                        # The free second estimate and THE LIQUIDITY IT USED, side by side with the
                        # quote-derived one above. Storing the input is the point: a derived value
                        # whose input was not kept cannot be re-graded when the model changes.
                        chase_liquidity_usd=_chase_liq,
                        chase_model_impact_pct=_model_impact,
                        chase_model_formula=MODEL_IMPACT_FORMULA,
                        chase_clamp_hit=(1 if (_src_impact is not None
                                               and _src_impact
                                               > cfg.MEMECOIN_CHASE_SOURCE_IMPACT_MAX_PCT)
                                         else 0),
                        chase_decided_by=("source_impact_allowance"
                                          if (_src_impact and gap is not None
                                              and gap <= _eff_cap) else "base_cap"))
                except Exception:
                    logger.exception("could not record the chase decision for %s", ev.mint)
                if gap is not None and gap <= _eff_cap:
                    logger.warning(
                        "chase ADMITTED pos-candidate %s: premium %+.1f%% within %.1f%% "
                        "(cap %.0f%% + %.1f%% of his own impact on $%.0f)",
                        ev.mint, gap, _eff_cap, event_chase_cap, _src_impact or 0.0,
                        float(getattr(ev, "their_size_usd", 0) or 0))
                else:
                    _persist_attempt_classification(conn, trace, classification)
                    res = {"status": "skipped_chase",
                           "reason": "coin already ran %+.0f%% past their entry "
                                     "(cap %.0f%%)" % (gap, _eff_cap)}
                    _alert_entry_result(ev.mint, trade_size, res,
                                        handle=getattr(ev, "source_handle", None),
                                        ticker=getattr(ev, "ticker", None),
                                        armed=_wallet_is_armed(conn, ev, identity))
                    _finish_trace_for_result(trace, res, identity=identity)
                    return res
            elif (trace is not None and event_chase_cap > 0 and not _manual_entry
                    and not cfg.dexscreener_off("E3", identity.network_id)
                    and not _waived_early("chase_past_entry")):
                # THE PASS BRANCH, RECORDED (entry capture P1, Alpha GO 2026-09-29). The chase
                # columns were written only when refusing, so every fill carried NULL. One
                # in-memory trace substage, from values already in hand: no I/O, no quote, no
                # DB write; the trace is persisted with the attempt's terminal row as before.
                # The gate's own conditions are repeated (the refusal `if` above stays exactly as
                # it was, for its source guards); `_waived_early` only adds to a set, and a
                # waived family is False here as it was there.
                try:
                    trace.mark_substage(
                        "chase_display_check",
                        outcome="unknown" if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY else "passed",
                        gap_pct=(None if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY else
                                 chase_gap_pct(ev, our_price=our_price, reference=_pre_ref)),
                        reference_basis=("intervening_market" if
                                         cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY else None),
                        cap_pct=event_chase_cap, our_price=our_price, reference_price=_pre_ref)
                except Exception:
                    pass                   # a trace mark never decides an entry
        else:
            _mark_trace_best_effort(trace, "market_data")
            _mark_pair_selection(trace, st)
            classification = _classify_funded_draft(
                conn, ev, identity, now_iso, classification_context, market_stats=st)

        _warm_mint_age(trace, classification, identity.network_id, getattr(ev, "mint", None),
                       getattr(ev, "source_user_id", None))
        quote_chase_cap = event_chase_cap
        band_without = event_chase_cap_base
        wallet_chase_cap_pct = None
        wallet_chase_cap_expires_at = None
        is_nano = (classification is not None
                   and classification.coin_band in {"nano", "assumed_nano"})
        if is_nano:
            if str(identity.network_id) == "4663":
                source_price = positive_price(getattr(ev, "their_price", None))
                display_price = positive_price((st or {}).get("price"))
                try:
                    display_drift = (abs(display_price / source_price - 1.0) * 100.0
                                     if source_price and display_price else None)
                except (TypeError, ValueError, ZeroDivisionError):
                    display_drift = None
                try:
                    display_limit = float(
                        cfg.MEMECOIN_REAL_4663_NANO_DISPLAY_DRIFT_MAX_PCT)
                except (TypeError, ValueError):
                    display_limit = 10.0
                prices_match = (display_drift is not None and math.isfinite(display_drift)
                                 and display_drift <= display_limit)
                quote_chase_cap, band_without = (
                    (max(event_chase_cap, cfg.MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT),
                     max(event_chase_cap_base, cfg.MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT))
                    if prices_match else (cfg.MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT,) * 2)
                if prices_match and is_fresh_follow_event(
                        ev, cfg.MEMECOIN_REAL_FRESH_FOLLOW_MAX_LAG_S):
                    wallet_chase_cap_pct = m.wallet_chase_cap(
                        conn, getattr(ev, "source_user_id", None), network_id="4663",
                        coin_band=classification.coin_band)
                    if wallet_chase_cap_pct is not None:
                        try:
                            created_at = datetime.fromisoformat(
                                str(getattr(ev, "created_at", "")).replace("Z", "+00:00"))
                            if created_at.tzinfo is not None and created_at.utcoffset() is not None:
                                wallet_chase_cap_expires_at = (
                                    created_at + timedelta(seconds=float(
                                        cfg.MEMECOIN_REAL_FRESH_FOLLOW_MAX_LAG_S))).isoformat()
                        except (TypeError, ValueError, OverflowError):
                            wallet_chase_cap_pct = None
                if trace is not None:
                    try:
                        liquidity = positive_price((classification.facts or {}).get("liquidity"))
                        source_size = positive_price(getattr(ev, "their_size_usd", None))
                        trace.mark_substage(
                            "wallet_chase_cap", display_drift_pct=display_drift,
                            base_cap_pct=quote_chase_cap,
                            wallet_cap_pct=wallet_chase_cap_pct,
                            expires_at=wallet_chase_cap_expires_at,
                            source_size_to_display_liquidity_pct=(
                                100.0 * source_size / liquidity
                                if source_size and liquidity else None),
                        )
                    except Exception:
                        pass
            else:
                quote_chase_cap = max(event_chase_cap, cfg.MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT)
                band_without = max(event_chase_cap_base, cfg.MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT)
        if _waived_early("chase_past_entry") and not is_nano:
            quote_chase_cap = band_without = 0.0
        quote_chase_cap = over_nano_chase_cap(quote_chase_cap, identity.network_id, is_nano)
        band_without = over_nano_chase_cap(band_without, identity.network_id, is_nano)
        if trace is not None and quote_chase_cap != band_without:
            # Read by the executors' copy_cap_override substage only, never by a gate.
            trace.copy_cap_band_without_override = band_without

        # FIRST-SEEN chase guard (2026-07-16). The guard above compares vs the COPIED EVENT's price
        # and is therefore blind to copying a late buyer: the armed trigger structurally fires on
        # the 3rd+ buyer, and every fresh-coin real Solana loss in the 14d matched window entered
        # 25-262 min after the coin's first feed event at +101%..+986% above its first-seen price
        # (SOLdiers x3 -$66, SCAM -$25, febu -$24). Anchor = earliest positive buy price our own
        # feed recorded for this exact (network_id, mint). Fail-OPEN on missing data.
        # The fresh-follow bypass is REMOVED (2026-08-23). It assumed a low-lag follow is an early
        # copy of the coin, so the coin-origin ratio could not matter. A fresh follow of the source
        # wallet's OWN RE-BUY breaks that assumption: their second buy resets the reference price
        # this hook compares against, so the drift gate reads the run as small and opens at the top.
        #
        # Live, Veyr / sambaxbt, both events fresh follows of the same wallet:
        #   14:24:56  they buy 1.801e-05   our exact quote 2.442e-05  = +35.5%  refused (cap 30%)
        #   14:31:41  they buy 4.801e-05   our exact quote 5.607e-05  = +16.8%  FILLED
        #   14:33:08  b_stop, -$17.64 on a $36.78 clip — the whole day's profit
        # We refused 2.442e-05 as too expensive and paid 5.608e-05 seven minutes later, which is
        # 3.11x the first-seen price against this 3.0x cap. The guard never ran: `first_price` was
        # None in the persisted classification because the bypass skipped the lookup entirely.
        if (cfg.MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO > 0 and not _manual_entry
                and not _waived_early("chase_first_seen")):
            price_now = our_price if (our_price or 0) else getattr(ev, "their_price", None)
            first = m.first_seen_buy_price(
                conn, identity.network_id, ev.mint,
                since_iso=_first_seen_floor(now_iso, cfg.MEMECOIN_REAL_FIRST_SEEN_WINDOW_S))
            _first_ok = first_seen_chase_ok(first_price=first, price_now=price_now,
                                            max_ratio=cfg.MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO)
            try:
                if trace is not None:
                    trace.mark_substage("chase_first_seen", allowed=_first_ok,
                                        first_price=first, price_now=price_now,
                                        cap_ratio=cfg.MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO)
            except Exception:
                logger.exception("first-seen substage not recorded for %s", ev.mint)
            if not _first_ok:
                _persist_attempt_classification(conn, trace, classification)
                res = {"status": "skipped_chase",
                       "reason": "coin ran %.1fx above first-seen price (cap %.1fx)"
                                 % (float(price_now) / float(first),
                                    cfg.MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO)}
                _alert_entry_result(ev.mint, trade_size, res,
                                    handle=getattr(ev, "source_handle", None),
                                    ticker=getattr(ev, "ticker", None), armed=_wallet_is_armed(conn, ev, identity))
                _finish_trace_for_result(trace, res, identity=identity)
                return res

        elif cfg.MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO <= 0:
            try:
                if trace is not None:
                    trace.mark_substage("chase_first_seen", cap_ratio=cfg.MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO)
            except Exception:
                logger.exception("first-seen substage not recorded for %s", ev.mint)

        # The hourly floor applies at every market cap. The m5 rules retain their own scope.
        vol_h1 = (st or {}).get("volume_h1")
        _signal_usd = (float(getattr(ev, "their_size_usd", 0) or 0)
                       if getattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", False) else 0.0)
        try:
            _volume_mcap = float((classification.facts or {}).get("effective_mcap"))
            if not math.isfinite(_volume_mcap) or _volume_mcap <= 0:
                _volume_mcap = None
        except (AttributeError, TypeError, ValueError):
            _volume_mcap = None
        if identity.network_id == "solana" and not _manual_entry:
            _cached_volume = cached_solana_volume_decision(
                (context_for(contexts, "solana") or {}).get("solana_volume_cache"), ev,
                mcap_usd=_volume_mcap, now_iso=now_iso)
            if _cached_volume is not None and _cached_volume.state == "refuse":
                if trace is not None:
                    trace.mark_substage("solana_volume_cache", state=_cached_volume.state,
                        reason=_cached_volume.reason, volume_usd=_cached_volume.volume_usd,
                        required_usd=_cached_volume.required_usd)
                res = {"status": "rejected", "reason": "solana_volume_under_floor",
                       "volume_usd": _cached_volume.volume_usd,
                       "required_usd": _cached_volume.required_usd}
                _finish_trace_for_result(trace, res, identity=identity)
                return res
        # THE SOURCE BUY'S AGE AT THIS DECISION. A negative value is not an error: the feed
        # timestamps a third of 4663 buys AFTER our own decision, and such a buy cannot be in the
        # provider's window either.
        _source_age_s = None
        try:
            _decided = _parse_ts(decision_at)
            _sourced = _parse_ts(getattr(ev, "created_at", None))
            if _decided is not None and _sourced is not None:
                _source_age_s = (_decided - _sourced).total_seconds()
        except Exception:
            logger.exception("source-buy age not computed for %s", getattr(ev, "mint", None))
        if trace is not None:
            _mark_probe(trace, "probe_pre_volume")
        _volume_active, _volume_boundary, _effective_m5_ceiling, _volume_kwargs = volume_gate_args(
            st, mcap=_volume_mcap, network_id=identity.network_id,
            coin_band=getattr(classification, "coin_band", None),
            source_user_id=getattr(ev, "source_user_id", None), signal_usd=_signal_usd,
            source_age_s=_source_age_s, decision_at=trace_anchor)
        _onchain_m5 = _own_m5_for_gate(ev, identity.network_id, st)
        if trace is not None:
            # The gate's own m5 read, marked apart from the tape-move shadow below (2026-10-14: that
            # substage's 5-11 ms window held this read; the shadow itself measured +0.09 ms p90).
            _mark_probe(trace, "onchain_m5_read")
        _shadow_tape_move(trace, ev, identity.network_id, st)
        if _onchain_m5 is not None:
            _volume_kwargs["m5_floor_lower_bound"] = _onchain_m5.get("usd")
            # The same sum bounds the HOUR from below. See `onchain_h1_lower_bound`.
            _volume_kwargs["h1_floor_lower_bound"] = onchain_h1_lower_bound(_onchain_m5)
            _volume_kwargs["m5_txns_lower_bound"] = onchain_m5_txns_lower_bound(_onchain_m5)
            # SWAPS WE SAW BUT COULD NOT PRICE MAKE THE FLOOR UNDECIDABLE (2026-09-22, GIGABYTE
            # 51a1a1b6: 51 swaps in a 4663 pool, none priceable, gate refused on the provider's
            # $309). 4663 carries ~17k pools quoted in tokenized equities (NVDA, META, HOOD, SPCX)
            # for which no honest USD exists, so this is a standing class and not one coin.
            try:
                _unpriced_swaps = sum(int(p.get("swaps") or 0)
                                      for p in (_onchain_m5.get("unpriced_pools") or []))
            except (TypeError, ValueError, AttributeError):
                _unpriced_swaps = 0
            _blind_reason = str(_onchain_m5.get("reason") or "")
            # AN ABSENT SOURCE TRANSACTION IS ONLY BLIND WHEN THE PROVIDER SAYS THE COIN TRADED
            # SOMEWHERE ELSE (2026-09-23). `excluded_tx_seen` false means the source's swap did not
            # land on a pool we know for this mint -- a venue we do not ingest, an unmapped pool, or
            # a multi-hop route whose own leg produced no tick. MSTR proved it is not an indexing
            # delay: its transaction was in the tape 0.88 s before this gate ran. Both numbers are
            # already computed here, so this adds no call and no read to the entry path.
            # A lagging protocol is decided BEFORE the venue comparison: a window that is missing
            # its own swaps cannot be called quiet on the strength of a provider agreeing with it.
            if _blind_reason == "source_tx_absent":
                from sniper.services.memecoin_onchain_volume import venue_gap_verdict
                _provider_organic_m5 = organic_volume(
                    (st or {}).get("volume_m5"), _signal_usd, source_age_s=_source_age_s,
                    index_lag_s=cfg.provider_index_lag_s(identity.network_id))
                _venue_blind, _venue_detail = venue_gap_verdict(
                    _onchain_m5.get("source_absent_usd"), _provider_organic_m5,
                    min_usd=float(getattr(cfg, "MEMECOIN_ONCHAIN_M5_VENUE_GAP_MIN_USD", 0.0) or 0.0),
                    ratio=float(getattr(cfg, "MEMECOIN_ONCHAIN_M5_VENUE_GAP_RATIO", 0.0) or 0.0))
                _onchain_m5 = dict(_onchain_m5)
                _onchain_m5.update(_venue_detail)
                if not _venue_blind:
                    # The coin is quiet on every instrument we hold, so the window stands and the
                    # floor decides. The sum keeps its own coverage label: the source's own swap is
                    # still missing from it, which is why it is `partial`, not `complete`.
                    _onchain_m5["usd"] = _onchain_m5.get("source_absent_usd")
                    _onchain_m5["coverage"] = "partial_source_tx_absent_quiet"
                    _onchain_m5["reason"] = "ok"
                    _blind_reason = "ok"
                    _volume_kwargs["m5_floor_lower_bound"] = _onchain_m5.get("usd")
            # `source_tx_absent` (2026-09-23, MRLN): the source wallet's own swap is missing from
            # the tape, so the tape does not cover the venue it traded on and the sum came from
            # somewhere else. Blind, exactly like a stale feed.
            # `protocol_lagging` (2026-09-23): the subscription that carries THIS coin's pools was
            # minutes behind on a socket whose heads were current, so the window is missing swaps
            # that certainly happened. Blind, exactly like a stale feed.
            # `source_venue_untaped` (2026-10-07, ULTRONICS): the source tx is on the tape but most
            # of its coin came from a venue the tape did not record. Blind, exactly like a stale feed.
            _blind = _blind_reason in ONCHAIN_M5_BLIND_REASONS
            if _onchain_m5.get("usd") is None and (_unpriced_swaps > 0 or _blind):
                # A LANE THAT DID NOT MEASURE CANNOT REFUSE (2026-09-22, JOLLYBOT d058ca73: the
                # observer had just reset its window, the lane said window_incomplete, the gate
                # fell back to the provider's $0.49 and refused a coin that ran +98% in 90 s).
                # Unknown is never zero: only a COMPLETE on-chain window with zero swaps is a
                # real zero. The ceiling keeps its own rule.
                if identity.network_id != "56":
                    _volume_kwargs["m5_floor_undecidable"] = True
                _onchain_m5 = dict(_onchain_m5)
                _onchain_m5["measurement_basis"] = (
                    "bsc_m5_required_unknown" if identity.network_id == "56" else
                    "onchain_unpriced_undecidable" if _unpriced_swaps > 0 else
                    "onchain_blind_undecidable")
                _onchain_m5["unpriced_swaps"] = _unpriced_swaps
            if trace is not None:
                try:
                    trace.mark_substage("onchain_volume_m5", **_onchain_m5)
                except Exception:
                    logger.exception("on-chain volume evidence not recorded")
        _alive, _need, _why = volume_alive_decision(vol_h1, _volume_mcap, **_volume_kwargs)
        (_alive, _need, _why), _m5_bound = _m5_redecide_with_source_unknown_bound(
            (_alive, _need, _why), ev, identity.network_id, st, _onchain_m5, _volume_kwargs,
            lambda: volume_alive_decision(vol_h1, _volume_mcap, **_volume_kwargs))
        if _m5_bound is not None and trace is not None:
            try:
                trace.mark_substage("onchain_volume_m5_source_unknown_bound", **_m5_bound)
            except Exception:
                logger.exception("J7 tape-bound evidence not recorded")
        # VOLUME GATES OFF BY CHAIN (operator 2026-09-29: "remove the volume filters for bsc and
        # base"). Every volume family (h1 floor, m5 turnover floor/ceiling, m5 txns, unreadable m5)
        # is skipped on a listed chain. The decision is still computed and recorded as the would-be
        # refusal, so the admitted-without-volume cohort can be graded later. Empty set = today.
        if not _alive and str(identity.network_id) in cfg.MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS:
            if trace is not None:
                try:
                    trace.mark_substage("volume_gate_off_chain", would_refuse=_why, need_usd=_need,
                                        chain=str(identity.network_id))
                except Exception:
                    logger.exception("volume gate off-chain evidence not recorded")
            logger.warning("[real] volume gate skipped on chain %s (would refuse: %s, need %s) %s",
                        identity.network_id, _why, _need, ev.mint)
            _alive = True
        # Keep each reason family separate for the refusal record and the operator alert.
        _vol_family = ("m5_txns_under_floor" if _why == "m5_txns_under_floor"
                       else "m5_txns_unreadable" if _why == "m5_txns_unreadable"
                       else "m5_turnover_over_ceiling" if _why.endswith("over_ceiling")
                       else "m5_turnover_under_big_floor"
                       if _why == "m5_turnover_under_big_floor"
                       # ITS OWN FAMILY, like the big floor above. It is a dollar bar with its own
                       # evidence, so an m5 RATIO exemption must not waive it, and the revival watch
                       # (which matches `m5_turnover_under_floor` exactly) must not re-ask on it.
                       else "m5_volume_under_abs_floor"
                       if _why == "m5_volume_under_abs_floor"
                       else "m5_turnover_under_floor" if _why.startswith("m5")
                       else "turnover_under_floor")
        _waived_m5_reason = None
        _hourly_review = None
        if (not _alive and not _manual_entry and _why.startswith("m5")
                and _waived(_vol_family)):
            # An m5 exemption does not exempt the hourly floor.
            _waived_m5_reason = _why
            _alive, _need, _why = volume_alive_decision(
                vol_h1, _volume_mcap, abs_floor=_volume_kwargs["abs_floor"],
                turnover_floor=_volume_kwargs["turnover_floor"], signal_usd=_signal_usd,
                pair_age_s=_volume_kwargs["pair_age_s"])
            _vol_family = "turnover_under_floor"
        # SOLANA HOURLY FLOOR REVIEW (operator spec 2026-09-17). Only a Solana hourly-floor refusal
        # that no manual policy waives. A failure inside the review leaves the refusal standing.
        if not _alive and not _manual_entry and not _waived(_vol_family):
            _decision_kwargs = (dict(abs_floor=_volume_kwargs["abs_floor"],
                                     turnover_floor=_volume_kwargs["turnover_floor"],
                                     signal_usd=_signal_usd,
                                     pair_age_s=_volume_kwargs["pair_age_s"])
                                if _waived_m5_reason else dict(_volume_kwargs))
            try:
                _hourly_review = solana_hourly_floor_review(
                    conn, network_id=identity.network_id, mint=ev.mint,
                    origin_event_id=getattr(ev, "fomo_event_id", None),
                    # T is the FROZEN entry-decision time (the trace anchor), never a later sample.
                    attempt_id=getattr(trace, "attempt_id", None), decision_at=trace_anchor, stats=st,
                    vol_h1=vol_h1, mcap=_volume_mcap, decision_kwargs=_decision_kwargs, why=_why,
                    need=_need, effective_ceiling=_effective_m5_ceiling,
                    source_created_at=getattr(ev, "created_at", None))
            except Exception:
                logger.exception("solana hourly floor review failed for %s; refusal stands", ev.mint)
                _hourly_review = None
            if _hourly_review is not None:
                try:
                    if trace is not None:
                        trace.mark_substage("solana_hourly_floor_evidence", **_hourly_review)
                except Exception:
                    logger.exception("solana hourly floor evidence not recorded for %s", ev.mint)
                if _hourly_review["final_gate_decision"] == "admit":
                    _alive, _why = True, _hourly_review["final_reason"]
        # Observation failures must not change the computed volume decision.
        try:
            if trace is not None:
                trace.mark_substage(
                    "volume_gate", active=_volume_active, mcap=_volume_mcap,
                    boundary=_volume_boundary, allowed=_alive, reason=_why,
                    decision_kwargs=(dict(abs_floor=_volume_kwargs["abs_floor"],
                                          turnover_floor=_volume_kwargs["turnover_floor"],
                                          signal_usd=_signal_usd,
                                          pair_age_s=_volume_kwargs["pair_age_s"])
                                     if _waived_m5_reason else dict(_volume_kwargs)),
                    source_age_s=_source_age_s,
                    required_usd=_need, volume_m5=(st or {}).get("volume_m5"),
                    volume_h1=vol_h1, txns=_m5_txn_count(st),
                    txns_m5_buys=(st or {}).get("txns_m5_buys"),
                    txns_m5_sells=(st or {}).get("txns_m5_sells"),
                    txns_floor=cfg.MEMECOIN_REAL_MIN_TXNS_M5,
                    floor_over_band=cfg.MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND,
                    h1_floor=_volume_kwargs["turnover_floor"],
                    h1_scope=("solana_nano_micro_off" if identity.network_id == "solana"
                              and _volume_kwargs["abs_floor"] == 0
                              and _volume_kwargs["turnover_floor"] == 0
                              and (cfg.MEMECOIN_REAL_MIN_VOLUME_H1_USD > 0
                                   or cfg.MEMECOIN_REAL_MIN_TURNOVER_H1 > 0)
                              else "all_market_caps"),
                    h1_abs_floor=_volume_kwargs["abs_floor"],
                    h1_abs_floor_basis=h1_abs_floor_for(identity.network_id, _volume_mcap)[1],
                    # Which band floor set h1_floor, so the forward read can split it (2026-09-28).
                    h1_floor_scope=("solana_micro" if solana_micro_h1_floor(
                        identity.network_id, getattr(classification, "coin_band", None),
                        _volume_mcap) > 0 else None),
                    # A MISSING HOURLY INPUT IS NOT A PASS (Alpha, 2026-09-28). The micro floor keeps
                    # the relative leg's unknown handling (admit), and says so here: the gate's
                    # organic h1 is still DexScreener's listed-pair sum, which the operator's
                    # "no dex anywhere" order will replace with our own tape.
                    h1_floor_input=(("missing" if vol_h1 is None else "provider_h1")
                                    if solana_micro_h1_floor(
                                        identity.network_id, getattr(classification, "coin_band", None),
                                        _volume_mcap) > 0 else None),
                    pair_age_s=_volume_kwargs.get("pair_age_s"),
                    waived_m5_reason=_waived_m5_reason,
                    configured_m5_floor=cfg.MEMECOIN_REAL_MIN_TURNOVER_M5,
                    configured_ceiling=cfg.MEMECOIN_REAL_MAX_TURNOVER_M5,
                    source_volume_excluded_usd=_signal_usd,
                    **_volume_notification_evidence(
                        volume_m5=(st or {}).get("volume_m5"), signal_usd=_signal_usd,
                        mcap=_volume_mcap, effective_ceiling=_effective_m5_ceiling,
                        reason=_why, need=_need,
                        floor_lower_bound=_volume_kwargs.get("m5_floor_lower_bound"),
                        source_age_s=_source_age_s,
                        index_lag_s=cfg.provider_index_lag_s(identity.network_id)),
                )
        except Exception:
            logger.exception("volume gate evidence not recorded")
        if not _alive and not _manual_entry and not _waived(_vol_family):
            _persist_attempt_classification(conn, trace, classification)
            _gate_stats = dict(st or {})
            _gate_stats["onchain_m5"] = _onchain_m5
            _gate_stats["provider_market_cap"] = _gate_stats.get("market_cap")
            _gate_stats["market_cap"] = _volume_mcap
            _gate_inputs = _volume_gate_inputs(
                _gate_stats, ev, _why, _need,
                # the leg that actually ran: over-band above the boundary, in-band below it
                effective_ratio_floor=(
                    (getattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_M5", 0.0) or 0.0)
                    if _volume_active
                    else cfg.inband_min_turnover_m5_for_chain(identity.network_id)),
                effective_h1_abs_floor=_volume_kwargs["abs_floor"],
                effective_h1_ratio_floor=_volume_kwargs["turnover_floor"])
            if _vol_family.startswith("m5_txns"):
                # A COUNT IS NOT DOLLARS. The dollar template below would print "$0" for a leg
                # whose bar is a trade count, which reads as "no volume" and is a different claim.
                _n = _m5_txn_count(st)
                res = {"status": "rejected",
                       "reason": ("m5 trades %s under floor %d (%s)"
                                  % ("unreadable" if _n is None else str(_n),
                                     int(getattr(cfg, "MEMECOIN_REAL_MIN_TXNS_M5", 0) or 0), _why)),
                       "gate_inputs_json": _gate_inputs}
            else:
                _vshown = (st or {}).get("volume_m5") if _why.startswith("m5") else vol_h1
                _over = _why.endswith("over_ceiling")
                _volume_name = "m5" if _why.startswith("m5") else "h1"
                if not _over and getattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", False):
                    _vshown = organic_volume(
                        float(_vshown or 0), _signal_usd,
                        source_age_s=None if _waived_m5_reason else _source_age_s,
                        index_lag_s=cfg.provider_index_lag_s(identity.network_id))
                    if (_why.startswith("m5_turnover_under") or _why == "m5_volume_under_abs_floor") and _volume_kwargs.get("m5_floor_lower_bound") is not None:
                        _vshown = max(_vshown, _volume_kwargs["m5_floor_lower_bound"])
                    if (_volume_name == "h1" and _hourly_review is not None
                            and _hourly_review.get("feed_buy_usd") is not None):
                        _vshown = max(_vshown, _hourly_review["feed_buy_usd"])
                    _volume_name += " organic"
                # THE FIGURE THE GATE DECIDED ON (2026-09-28, Alpha). An hourly floor leg reads
                # max(provider organic, our tape's m5 sum as a lower bound); c88c1b88 decided on
                # the tape's $1,698 and printed the provider's "$1".
                if (not _over and _volume_name.startswith("h1")
                        and _volume_kwargs.get("h1_floor_lower_bound") is not None):
                    _vshown = max(float(_vshown or 0), float(_volume_kwargs["h1_floor_lower_bound"]))
                # NO "revival guard" SUFFIX (2026-09-25, operator). Every volume refusal wore it,
                # including an 85-second-old coin that was reviving nothing. The gate token alone
                # names the leg; a revival is claimed below only when the revival watch is armed.
                _age_note = ""
                if (_why == "under_abs_floor"
                        and _h1_floor_share(_volume_kwargs.get("pair_age_s")) < 1.0):
                    _age_note = ", pro-rated to pair age %.0fs" % float(_volume_kwargs["pair_age_s"])
                # THE FLOOR'S BASIS (2026-09-28): a market-cap-scaled hourly floor says so.
                _floor_basis = (h1_abs_floor_for(identity.network_id, _volume_mcap)[1]
                                if _why == "under_abs_floor" else None)
                if _floor_basis:
                    _age_note = ", " + _floor_basis + _age_note
                res = {"status": "rejected",
                       "reason": "%s volume $%.0f %s $%.0f (%s%s)"
                                 % (_volume_name,
                                    float(_vshown or 0), "OVER ceiling" if _over else "under floor",
                                    _need, _why, _age_note),
                       "gate_inputs_json": _gate_inputs}
            # REVIVAL WATCH (operator, 2026-08-26): an m5-FLOOR refusal earns ONE delayed re-ask
            # if the coin's tape wakes up inside the window. Enqueue only — no reservation, no
            # slot, no capital, and no admission authority. On clearance the ORIGINAL event goes
            # back through this same hook and every gate here runs again.
            # An event whose buy another lane also judges (the 4663 fast lane, `no_revival`) never
            # arms a watch: that lane's own event owns the one re-ask, and a second watch would
            # take one of the few slots and re-ask the same buy twice.
            if (getattr(cfg, "MEMECOIN_REVIVAL_WATCH_ENABLED", False)
                    and not getattr(ev, "no_revival", False)
                    and not _first_buy_window_applies(ev, identity.network_id)):
                try:
                    from sniper.services import memecoin_revival_watch as _rw
                    if _rw.should_watch(
                            _why, mcap=_volume_mcap,
                            max_mcap=getattr(cfg, "MEMECOIN_REVIVAL_WATCH_MAX_MCAP_USD", 0.0)):
                        import time as _rwt
                        _ok, _evicted = _rw.enqueue(
                            _rw.WATCHES, event=ev, mint=ev.mint,
                            origin_event_id=getattr(ev, "fomo_event_id", None),
                            refused_at=now_iso, mcap=_volume_mcap,
                            needed=_need, now=_rwt.monotonic(),
                            network_id=identity.network_id,
                            coin_band=getattr(classification, "coin_band", None),
                            window_s=cfg.revival_watch_window_s(identity.network_id),
                            poll_s=cfg.revival_watch_poll_s(identity.network_id),
                            max_watches=getattr(cfg, "MEMECOIN_REVIVAL_WATCH_MAX", 4))
                        if _ok:
                            logger.warning("revival watch armed mint=%s need=%.0f mcap=%s",
                                        ev.mint, float(_need or 0),
                                        (st or {}).get("market_cap"))
                            # INSIDE the parenthesis, so the gate-token matchers still read it.
                            if res["reason"].endswith(")"):
                                res["reason"] = res["reason"][:-1] + ", revival watch armed)"
                except Exception:
                    # A watch is an optional extra. It must never change the refusal it observes.
                    logger.exception("revival watch enqueue failed for %s", ev.mint)
            _alert_entry_result(ev.mint, trade_size, res,
                                handle=getattr(ev, "source_handle", None),
                                ticker=getattr(ev, "ticker", None),
                                armed=_wallet_is_armed(conn, ev, identity))
            _finish_trace_for_result(trace, res, identity=identity)
            return res

        # DEAD-COIN GATE (operator via Alpha 2026-10-07, SWCH 71539). Old AND quiet: age is the coin's
        # (max of pair and token age, as the volume gate's), volume is the gate's organic h1 (the
        # source's buy excluded when the provider can hold it; our own tape's lower bound counts as
        # activity). Recorded on every chain; refuses only on MEMECOIN_REAL_DEAD_COIN_CHAINS. No new
        # read: every input is already in hand. A manual entry is recorded, never refused.
        _dc_h1 = organic_volume(vol_h1, _signal_usd, source_age_s=_source_age_s,
                                index_lag_s=cfg.provider_index_lag_s(identity.network_id))
        if _dc_h1 is not None and _volume_kwargs.get("h1_floor_lower_bound") is not None:
            _dc_h1 = max(_dc_h1, float(_volume_kwargs["h1_floor_lower_bound"]))
        _dc_age = _volume_kwargs.get("pair_age_s")
        _dc_dead, _dc_reason = dead_coin_decision(
            _dc_age, _dc_h1, min_age_s=cfg.MEMECOIN_REAL_DEAD_COIN_MIN_AGE_S,
            max_h1_usd=cfg.MEMECOIN_REAL_DEAD_COIN_MAX_H1_USD)
        _dc_listed = str(identity.network_id) in cfg.MEMECOIN_REAL_DEAD_COIN_CHAINS
        _dc_enforced = bool(_dc_dead and _dc_listed and not _manual_entry)
        try:
            if trace is not None:
                trace.mark_substage("dead_coin_gate", would_refuse=_dc_dead, enforced=_dc_enforced,
                                    reason=_dc_reason, age_s=_dc_age, organic_h1_usd=_dc_h1,
                                    provider_h1_usd=vol_h1,
                                    min_age_s=cfg.MEMECOIN_REAL_DEAD_COIN_MIN_AGE_S,
                                    max_h1_usd=cfg.MEMECOIN_REAL_DEAD_COIN_MAX_H1_USD,
                                    chain=str(identity.network_id))
        except Exception:
            logger.exception("dead coin gate evidence not recorded")
        if _dc_dead and _dc_listed and not _manual_entry:
            _persist_attempt_classification(conn, trace, classification)
            res = {"status": "rejected",
                   "reason": "dead coin: %.0fd old, h1 organic $%.0f under $%.0f (dead_coin)"
                             % (float(_dc_age) / 86400.0, float(_dc_h1),
                                cfg.MEMECOIN_REAL_DEAD_COIN_MAX_H1_USD)}
            _alert_entry_result(ev.mint, trade_size, res,
                                handle=getattr(ev, "source_handle", None),
                                ticker=getattr(ev, "ticker", None),
                                armed=_wallet_is_armed(conn, ev, identity))
            _finish_trace_for_result(trace, res, identity=identity)
            return res

        # SELL-SHARE GATE (operator, 2026-08-26). Refuse a tape that is overwhelmingly one-sided
        # against us. COUNTS, not volume — the reason string says so, because the operator's 80%
        # was chosen about volume and the pool flow that would measure volume is unresolved at
        # entry on 0 of 99 funded positions. Fail-open on every unknown.
        # The reading is recorded on EVERY attempt, admitted or refused, so outcomes can re-derive
        # the true threshold later.
        _ss_cap = getattr(cfg, "MEMECOIN_REAL_MAX_SELL_SHARE_PCT", 0.0)
        if _ss_cap and float(_ss_cap) > 0:
            from sniper.services.memecoin_sell_share import count_sell_share, sell_share_decision
            _ss_input, _ss_basis, _ss_own = _sell_share_input(st, _onchain_m5, identity.network_id)
            _ss_ok, _ss_share, _ss_total, _ss_reason = sell_share_decision(
                _ss_input, cap_pct=_ss_cap,
                min_txns=getattr(cfg, "MEMECOIN_REAL_SELL_SHARE_MIN_TXNS", 8))
            # RECORDING IS FAIL-SOFT; THE DECISION IS NOT. Instrumentation must never be able to
            # change a funded result — that is the contract
            # test_trace_finish_failure_does_not_change_funded_result exists to pin, and an
            # unguarded mark_substage here broke it. `_ss_ok` is already computed above, so a trace
            # that cannot record still admits or refuses exactly the same way.
            try:
                if trace is not None:
                    _own_share, _own_total = count_sell_share(_ss_own)
                    trace.mark_substage("sell_share", allowed=_ss_ok, reason=_ss_reason,
                                        share_pct=_ss_share, txns=_ss_total,
                                        # E7 SHADOW (2026-09-29): our own split beside the input
                                        # the gate used, whichever it was, so agreement keeps
                                        # being measured before and after the switch.
                                        own_share_pct=_own_share, own_txns=_own_total,
                                        own_pool=(_ss_own or {}).get("pool"),
                                        own_covered_s=(_ss_own or {}).get("covered_s"),
                                        cap_pct=_ss_cap,
                                        min_txns=getattr(cfg, "MEMECOIN_REAL_SELL_SHARE_MIN_TXNS", 8),
                                        basis=_ss_basis)
            except Exception:
                logger.exception("sell-share substage not recorded for %s", ev.mint)
            if not _ss_ok and not _manual_entry and not _waived("sell_share"):
                _persist_attempt_classification(conn, trace, classification)
                res = {"status": "rejected", "reason": _ss_reason}
                _alert_entry_result(ev.mint, trade_size, res,
                                    handle=getattr(ev, "source_handle", None),
                                    ticker=getattr(ev, "ticker", None),
                                    armed=_wallet_is_armed(conn, ev, identity))
                _finish_trace_for_result(trace, res, identity=identity)
                return res

        else:
            try:
                if trace is not None:
                    trace.mark_substage("sell_share", cap_pct=_ss_cap)
            except Exception:
                logger.exception("sell-share substage not recorded for %s", ev.mint)

        # COIN-AGE CEILING (2026-08-17, "Sorajiro"): an old range-bound coin has no expansion
        # phase to ride — 3-day evidence: <1h coins broke even, every older bucket lost (-$234
        # combined, 32% wins). Uses the classification's own pair_age fact; fail-OPEN on unknown
        # age (an unindexed pool is fresh by construction — the winners bucket).
        pair_age = _funded_market_stats(st, now_iso).get("pair_age_s") if st else None
        if pair_age is None and classification is not None:
            pair_age = classification.facts.get("pair_age_s")
        if (cfg.MEMECOIN_REAL_MAX_PAIR_AGE_S > 0 and pair_age is not None
                and not _manual_entry and not _waived("coin_age_ceiling")
                and float(pair_age) > cfg.MEMECOIN_REAL_MAX_PAIR_AGE_S):
            _persist_attempt_classification(conn, trace, classification)
            res = {"status": "rejected",
                   "reason": "coin is %.1fh old (ceiling %.1fh) — no-movement class"
                             % (float(pair_age) / 3600.0,
                                cfg.MEMECOIN_REAL_MAX_PAIR_AGE_S / 3600.0)}
            _alert_entry_result(ev.mint, trade_size, res,
                                handle=getattr(ev, "source_handle", None),
                                ticker=getattr(ev, "ticker", None),
                                armed=_wallet_is_armed(conn, ev, identity))
            _finish_trace_for_result(trace, res, identity=identity)
            return res

        # WINNER RE-ENTRY GUARDS (2026-08-27, operator order). The blanket cooldown no longer
        # refuses a coin whose last real close WON, so the two things it was incidentally
        # protecting need saying explicitly. SELECTION altitude, because both need a PRICE and the
        # risk gate rightly never sees one.
        #
        # The 07-09 re-entry study never split winners out. This is a policy change on operator
        # judgment with the ledger as the grader, not a finding.
        #
        # Only a coin classification marked as a winner re-entry pays for the lookup (2026-09-28):
        # it costs ~1 ms on the entry path, and before this gate it ran on EVERY entry, first
        # entries included, whenever the flag was on. The mark exists only on an in-scope chain.
        if (not _manual_entry and cfg.MEMECOIN_REENTRY_WINNER_UNBLOCKED
                and classification is not None
                and (classification.facts or {}).get("reentry_rank")):
            _last = None
            try:
                _last = m.last_real_close_for_coin(conn, identity.network_id, ev.mint)
            except Exception:
                logger.exception("winner re-entry lookup failed for %s — admitting", ev.mint)
            if _last and _last.get("won"):
                _cap = int(cfg.MEMECOIN_REENTRY_WINNER_DAILY_CAP)
                _n = m.real_campaigns_today_for_coin(
                    conn, identity.network_id, ev.mint, now_iso)
                if _cap > 0 and _n >= _cap and not _waived("winner_reentry_daily_cap"):
                    _persist_attempt_classification(conn, trace, classification)
                    res = {"status": "rejected",
                           "reason": "winner re-entry daily cap: %d real campaign(s) on this coin "
                                     "today (cap %d)" % (_n, _cap)}
                    _alert_entry_result(ev.mint, trade_size, res,
                                        handle=getattr(ev, "source_handle", None),
                                        ticker=getattr(ev, "ticker", None),
                                        armed=_wallet_is_armed(conn, ev, identity))
                    _finish_trace_for_result(trace, res, identity=identity)
                    return res
                _exit_unit = _last.get("exit_unit_price")
                _max_pct = float(cfg.MEMECOIN_REENTRY_WINNER_MAX_ABOVE_EXIT_PCT)
                # SPACE MIX, STATED. `our_price` is the CHART mid; `_exit_unit` is route-realized
                # (exit notional / qty). Mixing them is deliberate — there is no chart price of our
                # own exit to compare against, and re-quoting the route here would put a provider
                # call on the entry path for a gate that is only a bound. The error has a KNOWN
                # DIRECTION: the chart sits BELOW route fills (2026-08-25), so this ratio is
                # understated and the gate UNDER-blocks. It is a ceiling, not a measurement, and
                # MEMECOIN_REAL_MAX_EXEC_PREMIUM still bounds what we actually pay.
                #
                # Fails OPEN on a missing price, like every other guard here: a gate that fires on
                # ABSENT data is the bug class that cost the whole EVM leg. The daily cap above
                # still bounds an unpriceable winner.
                if (_max_pct > 0 and _exit_unit and _exit_unit > 0
                        and not _waived("winner_reentry_price")
                        and our_price and float(our_price) > _exit_unit * (1.0 + _max_pct / 100.0)):
                    _over = (float(our_price) / _exit_unit - 1.0) * 100.0
                    _persist_attempt_classification(conn, trace, classification)
                    res = {"status": "rejected",
                           "reason": "winner re-entry above our exit: %+.0f%% over the %s we sold "
                                     "at (cap %.0f%%)" % (_over, _fmt_price(_exit_unit), _max_pct)}
                    _alert_entry_result(ev.mint, trade_size, res,
                                        handle=getattr(ev, "source_handle", None),
                                        ticker=getattr(ev, "ticker", None),
                                        armed=_wallet_is_armed(conn, ev, identity))
                    _finish_trace_for_result(trace, res, identity=identity)
                    return res

        # Classification already has the admission band. Refuse here before live balance reads;
        # the reservation seam repeats this as the authoritative fail-safe for other callers.
        if (classification is not None and not _manual_entry
                and not _waived("coin_band_unreservable")
                and not band_is_reservable(
                    classification.coin_band, signal_source=classification.signal_source)):
            _persist_attempt_classification(conn, trace, classification)
            res = {
                "status": "rejected",
                "reason": f"coin band is not reservable: {classification.coin_band!r}",
            }
            _alert_entry_result(
                ev.mint, trade_size, res,
                handle=getattr(ev, "source_handle", None),
                ticker=getattr(ev, "ticker", None),
                armed=_wallet_is_armed(conn, ev, identity),
            )
            _finish_trace_for_result(trace, res, identity=identity)
            return res

        # SELECTION PASSED. Record which exemptions were in force, so "how do exempted entries
        # perform" is a query rather than archaeology. Written once here rather than threaded
        # through twenty _finish_trace_for_result call sites.
        #
        # WHAT MATTERED, NOT WHAT WAS ON. This recorded every configured exemption, so an entry
        # from a wallet excused from six gates recorded six whether or not any of them changed
        # anything — and "how do exempted entries perform" then pools entries a waiver saved with
        # entries that would have passed regardless. Only waivers that actually decided are
        # recorded plain. A family suffixed `?` was waived before its own predicate ran, so
        # whether it decided is unknown; never count those as decisive.
        #
        # What was CONFIGURED stays answerable from memecoin_wallet_gate_exemptions by wallet and
        # time, so nothing is lost by narrowing this column to the decisive set.
        #
        # NULL means no waiver decided anything. Writing it is best-effort: an attribution record
        # must never be able to refuse a funded entry.
        _waived_note = sorted(_waived_decisive) + ["%s?" % f for f in sorted(_waived_unevaluated)]
        if _waived_note and trace is not None:
            try:
                m.update_entry_attempt_fields(
                    conn, getattr(trace, "attempt_id", None),
                    exemptions_applied=",".join(_waived_note))
            except Exception:
                logger.exception("could not record exemptions_applied for %s", ev.mint)
        _set_entry_shadow_context(trace, ev, trade_size, selection_gates_complete=True)

        # EVM route capacity is checked once, after the concurrent live balance reads in
        # `_make_evm_entry_fn`. A pre-balance probe used the same slow quote path twice.
        capacity_clip = None

        sol = context_for(contexts, "solana")
        is_solana = identity.network_id == "solana"
        _snapshot_debits_fn = None
        if sol and is_solana:
            # Same short-TTL fallback as the EVM gate. A failed read here does not refuse
            # loudly — it lands as 0.0 spendable in the risk context and silently declines
            # the entry, so the lost signal looks like a sizing decision rather than an
            # instrument failure.
            _gate_read_started = time.monotonic()
            _pre = _entry_prereads_for(ev, trace)
            _early = _pre.balances() if _pre is not None else None
            if _early is not None:
                sol_usdc, sol_native, _sol_cached, _early_read_ms = _early
                _balance_read_started = _pre.balance_read_started
            else:
                _read_meta = {}
                sol_usdc, sol_native, _sol_cached = _gate_balances(
                    "solana", sol["usdc_fn"], sol["sol_fn"], deadline_s=_GATE_BALANCE_DEADLINE_S,
                    read_meta=_read_meta)
                _balance_read_started = _read_meta["started"]
            _history_fn = sol.get("slot_snapshot_debits_fn")
            if _history_fn is not None:
                _snapshot_debits_fn = lambda: _history_fn(_balance_read_started)
            # MEASUREMENT ONLY (2026-09-27, Alpha: L1 step 0). This read sits inside the reservation
            # stage with no mark of its own; its size decides whether a warm snapshot is worth
            # building. Recording is fail-soft and cannot change the entry. With the early read
            # (B), duration_ms is what the entry BLOCKED on and read_ms is the read's own wall.
            try:
                if trace is not None:
                    trace.mark_substage("timing_gate_balance_read",
                                        duration_ms=(time.monotonic() - _gate_read_started) * 1000.0,
                                        served_from_cache=bool(_sol_cached),
                                        early=_early is not None,
                                        read_ms=_early_read_ms if _early is not None else None)
            except Exception:
                logger.exception("gate balance read timing not recorded for %s", ev.mint)
            _settle_early_mint_read(ev, trace)
            from sniper.execution.solana_wallet import native_sol_balance_value
            sol_native = native_sol_balance_value(sol_native)
        else:
            sol_usdc, sol_native = 0.0, 0.0
        def cluster_block_price_fn(mint):
            if stats_fn is None:
                return None
            try:
                fresh_stats = stats_fn(mint, network_id="solana") or {}
            except TypeError:
                fresh_stats = stats_fn(mint) or {}
            return fresh_stats.get("price")

        # J7 SHADOW STOPS HERE (MEMECOIN_J7_SOLANA_MODE): every gate above has run; the leg would
        # size, quote, reserve and sign. The decision is recorded, nothing is spent.
        if getattr(ev, "delivery_lane", None) == J7_SHADOW_LANE:
            res = {"status": "shadow", "reason": "j7_shadow: pre-entry checks passed; reservation and quote checks not run"}
            _finish_trace_for_result(trace, res, identity=identity)
            return res
        # About to spend on whichever leg real_entry routes to; cached pre-gate balances
        # are stale from here on.
        _gate_balance_invalidate("solana")
        _pons_key = _pons_test_arm(conn, ev, identity.network_id)
        _size_cap = None
        if _pons_key is not None:
            _size_cap = float(cfg.MEMECOIN_PONS_TEST_CLIP_USD)
            trade_size = min(float(trade_size), _size_cap)
        from sniper.services.memecoin_wallet_caps import entry_cap_usd
        from sniper.services.memecoin_risk import solana_clip_ceiling_usd
        # ABOVE NANO (2026-10-08): the band's ceiling, or this cap would hold the reservation at 175.
        _size_cap = entry_cap_usd(getattr(ev, "source_user_id", None), identity.network_id,
                                 existing_cap=_size_cap,
                                 ceiling_usd=solana_clip_ceiling_usd(classification, identity.network_id,
                                                                     getattr(ev, "source_user_id", None)))
        if _size_cap is not None:
            trade_size = min(float(trade_size), _size_cap)
        # TRIAL ARM (spec 2026-10-01 section 5.2): an open trial on this (wallet, chain) sizes the
        # entry to its clip and is handed to the reservation, which validates it again inside its
        # transaction and freezes the marker. A manual buy is the operator's own, never a trial.
        _trial = None if _manual_entry else _trial_for_entry(conn, ev, identity.network_id)
        if _trial is not None:
            trade_size = min(float(trade_size), float(_trial["clip_usd"]))
        if trace is not None:
            _mark_probe(trace, "probe_pre_leg")
        _sol_execute = sol["execute_fn"] if sol else (lambda *a, **k: dict(_REJECT_SOLANA))
        if identity.network_id == 'solana' and not _manual_entry:
            _sol_execute = functools.partial(_sol_execute, entry_head_fn=entry_opportunity.head)
        try:
            res = real_entry(
                conn, ev, day=now_iso[:10],
                usdc_balance=sol_usdc,
                sol_balance=sol_native,
                now_iso=now_iso, trade_size=trade_size,
                token_safety_fn=(sol["token_safety_fn"] if sol else (lambda mint: (False, "no solana leg"))),
                execute_fn=_sol_execute,
                kill_path=(sol.get("kill_path") if sol else None),
                evm_entry_fn=_make_evm_entry_fn(contexts), impact_cap_pct=impact_cap_pct,
                chase_max_pct=quote_chase_cap,
                wallet_chase_cap_pct=wallet_chase_cap_pct,
                wallet_chase_cap_expires_at=wallet_chase_cap_expires_at,
                capacity_clip=capacity_clip, size_cap_usd=_size_cap, trial=_trial,
                resolved_identity=identity, trace=trace, classification=classification,
                cluster_decide_fn=cluster_decide_fn,
                cluster_block_price_fn=cluster_block_price_fn,
                **({"snapshot_debits_fn": _snapshot_debits_fn} if _snapshot_debits_fn else {}))
        finally:
            if _pons_key is not None:
                from sniper.execution.evm_pons_v2_quote import TEST_ALLOW
                TEST_ALLOW.pop(_pons_key, None)
        if res.get("status") == "filled" and res.get("position_id"):
            # the executor only knows the mint — stamp who/what from the feed event so the
            # cockpit and the later EXIT alert name the coin + trader, not '? ((real))'.
            # origin_event_id is normally already stamped via the pending-row provenance
            # (entry-speed-coverage); the COALESCE here is the WS3 backstop for any fill
            # whose reservation lacked it.
            m.set_position_identity(conn, res["position_id"],
                                    ticker=getattr(ev, "ticker", None),
                                    source_handle=getattr(ev, "source_handle", None),
                                    origin_event_id=getattr(ev, "fomo_event_id", None))
            # Pin total supply from the ENTRY snapshot, after the fill, so an mcap target set in
            # the first seconds is already on the FDV basis. No fdv here -> the entry's own mint
            # read (no extra call), else the sweep pins later while X1 is on.
            # set_position_identity committed the fill metadata above. Finish this optional
            # pin's write before trace.finish, which refuses a caller-owned transaction.
            try:
                pin_entry_fdv_supply(conn, {"id": res["position_id"],
                                            "network_id": identity.network_id},
                                     st, getattr(ev, "mint_account_read", None))
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception("fdv supply pin commit failed pos=%s", res["position_id"])
        _alert_entry_result(ev.mint, trade_size, res,
                            handle=getattr(ev, 'source_handle', None),
                            ticker=getattr(ev, 'ticker', None),
                            source_user_id=getattr(ev, 'source_user_id', None),
                            source_size_usd=getattr(ev, 'their_size_usd', None),
                            source_price=getattr(ev, 'their_price', None),
                            entry_lag_s=getattr(ev, 'lag_s', None),
                            policy=cfg.MEMECOIN_REAL_POLICY,
                            network_id=getattr(identity, 'network_id', None) or getattr(ev, 'network_id', None),
                            source_id=getattr(ev, 'fomo_event_id', None),
                            armed=_wallet_is_armed(conn, ev, identity))
        _finish_trace_for_result(trace, res, identity=identity)
        return res
    except Exception as exc:
        _finish_trace_error_best_effort(trace, exc, identity=identity)
        raise


# tp-vs-derisk-ride A/B (2026-07-16) CLOSED 2026-07-21: enrollment structurally ended at the
# funded-contract cutover (TP_PCT=0 + contract policies own exits), 12/30 ride closes accrued
# (-$7.76) vs even-id control 19 (-$123.12) — insufficient n, no verdict. assign_tp_ab_arm
# removed; historical 'ride' override rows remain in the audit trail and keep their priority
# slot in _EXIT_OVERRIDE_PRIORITY. Brain: finding/memecoin-tp-ab-closeout-2026-07-21.


def assign_micro_policy(conn, position_id, *, eff_mc, assumed_micro):
    """Micro-ladder override (source='micro') for a freshly filled micro-cap real position:
    derisk at +MEMECOIN_REAL_MICRO_DERISK_PCT (recover the entry stake), NO full TP (keep the
    30%-hit100 tail), trail the remainder at MEMECOIN_REAL_MICRO_TRAIL_PCT giveback from peak,
    position-scoped stop -MEMECOIN_REAL_MICRO_SL_PCT (covers 4663 micros without touching the
    chain-wide no-stop decision). Outranks the tp-ab 'ride' row — micro is its own cohort.
    Read gate (pre-registered): >=30 closes, PnL + drop-top-1 vs the tp-ab arms."""
    if not cfg.MEMECOIN_REAL_MICRO_POLICY_ENABLED:
        return None
    band = ladder_band_of(eff_mc, assumed_micro)
    if band is None:
        return None
    derisk, trail, sl = _LADDER_RAILS[band]()
    m.set_position_exit_override(
        conn, position_id, "micro", tp_pct=0.0, derisk_pct=derisk, trail_pct=trail,
        sl_pct=sl, actor="system",
        note="%s ladder v2 (2026-07-17): derisk@+%g%%, trail %g%% giveback, stop -%g%%, no TP"
             % (band, derisk, trail, sl))
    return band


# BAND SPLIT (2026-07-17, operator: no one-fit-all): NANO = assumed-micro (triple-unresolvable
# mcap means minutes-old = the smallest class) or resolved < NANO. MICRO < 250K. 250K-1M stays
# with the tp-ab A/B (pre-registered read). MID = 1-2M consensus-gated scalps (57% hit20 armed,
# 58% round-trip -> earliest-fire derisk +30, trail 35). Same override source; cohorts split at
# read time on positions.entry_mcap (NULL = assumed/nano).
_LADDER_RAILS = {
    "nano": lambda: (cfg.MEMECOIN_REAL_NANO_DERISK_PCT, cfg.MEMECOIN_REAL_NANO_TRAIL_PCT,
                     cfg.MEMECOIN_REAL_NANO_SL_PCT),
    "micro": lambda: (cfg.MEMECOIN_REAL_MICRO_DERISK_PCT, cfg.MEMECOIN_REAL_MICRO_TRAIL_PCT,
                      cfg.MEMECOIN_REAL_MICRO_SL_PCT),
    "mid": lambda: (cfg.MEMECOIN_REAL_MID_DERISK_PCT, cfg.MEMECOIN_REAL_MID_TRAIL_PCT,
                    cfg.MEMECOIN_REAL_MID_SL_PCT),
}


def ladder_band_of(eff_mc, assumed_micro):
    """Which ladder band (if any) a fill belongs to. None = default/A-B rails."""
    if assumed_micro or (eff_mc is not None and eff_mc < cfg.MEMECOIN_REAL_NANO_MCAP_USD):
        return "nano"
    if eff_mc is not None and eff_mc < cfg.MEMECOIN_REAL_MICRO_MCAP_USD:
        return "micro"
    if (eff_mc is not None and cfg.MEMECOIN_REAL_MID_LO_MCAP_USD <= eff_mc
            < cfg.MEMECOIN_REAL_MID_HI_MCAP_USD):
        return "mid"
    return None


def micro_entry_capacity_ok(conn):
    """Hardening: at most MEMECOIN_REAL_MICRO_MAX_CONCURRENT open micro-ladder positions —
    a wave of fresh unresolvable coins must not fill the whole book with the riskiest class.
    0 = cap off."""
    cap = cfg.MEMECOIN_REAL_MICRO_MAX_CONCURRENT
    if cap <= 0:
        return True
    return m.open_micro_position_count(conn) < cap


def _iso_epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


SIZE_MIRROR_MAX_RAW_FRACTION = 1.5   # above this the token rebuild is inconsistent, not a size


def source_sell_share(trades, *, opened_at, lookback_s):
    """How much of the copied wallet's holding it has SOLD since our entry, from its own trades.

    `trades` are that wallet's buys and sells of this coin (dicts with side, their_price,
    their_size_usd, created_at), the triggering sell included. Tokens are usd / price, so the unit
    is the event's own price and never needs decimals. Returns (fraction, detail) where fraction is
    sold_since_entry / (held_at_entry + bought_since_entry), capped at 1, or (None, detail) when the
    holding is unknown. Any unpriced trade within the inventory window makes the holding unknown.

    CUMULATIVE ON PURPOSE. A trader who sells 20% and then 20% has de-risked 40%; judging each sell
    alone would call both a trim. Buys before our entry older than `lookback_s` do not count, so a
    stale campaign whose exit we never saw cannot shrink every later sell's share.
    """
    t_open = _iso_epoch(opened_at)
    held = bought = sold = cost = buy_tokens = 0.0
    last_buy = last_sell = None
    unpriced_inventory = False
    for t in trades:
        ts = _iso_epoch(t.get("created_at"))
        try:
            px, usd = float(t.get("their_price") or 0), float(t.get("their_size_usd") or 0)
        except (TypeError, ValueError):
            px = usd = 0.0
        after = t_open is not None and ts is not None and ts > t_open
        if px <= 0 or usd <= 0:
            unpriced_inventory = unpriced_inventory or (
                t_open is None or ts is None or t_open - ts <= lookback_s)
            continue
        tokens = usd / px
        if t.get("side") == "buy":
            if not after and t_open is not None and ts is not None and t_open - ts > lookback_s:
                continue
            if after:
                bought += tokens
            else:
                held += tokens
            cost += usd
            buy_tokens += tokens
            last_buy = ts
        elif t.get("side") == "sell":
            if after:
                sold += tokens
                last_sell = t
            else:
                held = max(0.0, held - tokens)
    base = held + bought
    detail = {"held_tokens": held, "bought_since_tokens": bought, "sold_tokens": sold,
              "their_pnl_pct": None, "since_their_buy_s": None, "their_sell_usd": None}
    if last_sell is not None:
        detail["their_sell_usd"] = float(last_sell.get("their_size_usd") or 0)
        sell_ts = _iso_epoch(last_sell.get("created_at"))
        if last_buy is not None and sell_ts is not None:
            detail["since_their_buy_s"] = round(sell_ts - last_buy, 1)
        if buy_tokens > 0 and cost > 0:
            detail["their_pnl_pct"] = round(
                (float(last_sell["their_price"]) / (cost / buy_tokens) - 1.0) * 100.0, 2)
    if base <= 0 or unpriced_inventory:
        return None, detail
    raw = sold / base
    detail["raw_fraction"] = round(raw, 4)
    if raw > SIZE_MIRROR_MAX_RAW_FRACTION:
        # SELLING MORE THAN IT HELD IS NOT A SIZE, IT IS A UNIT MISMATCH (Beta, 2026-09-15). 10 of
        # 40 funded triggers at 0.30 read above 1.5x, one at 899x: their_price differs in unit or
        # basis between the buy and sell events, so usd / price does not measure tokens. Unknown,
        # so the position holds on rails.
        detail["inconsistent"] = True
        return None, detail
    return min(1.0, raw), detail


def _size_aware_mirror(conn, ev, pos, ctx, decision_at, *, first_sell=False):
    """The size-aware copy-sell for one open position. See MEMECOIN_REAL_MIRROR_SIZE_CHAINS."""
    trades = m.source_trades_for_coin(conn, pos.get("source_user_id"), pos.get("mint"),
                                      pos.get("network_id"))
    event_id = getattr(ev, "fomo_event_id", None)
    if event_id and not any(t.get("fomo_event_id") == event_id for t in trades):
        # The runner records the event before this hook, but never rely on that ordering.
        trades.append({"fomo_event_id": event_id, "side": "sell",
                       "their_price": getattr(ev, "their_price", None),
                       "their_size_usd": getattr(ev, "their_size_usd", None),
                       "created_at": getattr(ev, "created_at", None)})
    fraction, facts = source_sell_share(
        # The CONFIRMED FILL, not decision start (Beta review, 2026-09-15): opened_at is stamped
        # before quote/sign/send/confirm, opened_recorded_at at/after the fill -- same class of bug
        # as finding-opened-at-is-decision-start-not-the-fill-2026-09-03. A trader sell landing in
        # that gap has not sold anything WE held yet. opened_at stays only as a fallback for a row
        # that predates the column.
        trades, opened_at=pos.get("opened_recorded_at") or pos.get("opened_at"),
        lookback_s=cfg.MEMECOIN_REAL_MIRROR_SIZE_LOOKBACK_S)
    minimum = float(cfg.MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION)
    if first_sell:
        # DEGEN EXIT TRIAL V4: the source's first sell after our fill is a full exit, whatever its
        # size, known or not. The size facts above stay in the audit row.
        code = "first_sell"
    elif fraction is None:
        code = "size_inconsistent" if facts.get("inconsistent") else "size_unknown"
    elif fraction >= minimum:
        code = "size_mirror"
    else:
        code = "size_hold"
    detail = {"code": code, "origin_event_id": event_id,
              "sold_fraction": None if fraction is None else round(fraction, 4),
              "min_fraction": minimum, "network_id": pos.get("network_id"),
              "mint": pos.get("mint"), **facts}
    m.record_exit_trigger_audit(
        conn, position_id=pos["id"], reason="mirror", display_price=None, preview_usdc=None,
        allowed=(code in ("size_mirror", "first_sell")), detail=json.dumps(detail, sort_keys=True),
        checked_at=decision_at, lane="mirror_on_sell")
    if code not in ("size_mirror", "first_sell"):
        # Unknown holds too: without the trader's holding the sell cannot be sized, and the rails
        # (stop, TP, vol_death, time) still guard the position.
        logger.warning("real mirror: HOLD pos=%s mint=%s %s sold=%s of holding (min %.0f%%)",
                       pos["id"], pos.get("mint"), code,
                       "unknown" if fraction is None else "%.0f%%" % (fraction * 100),
                       minimum * 100)
        return {"status": "mirror_held", "reason": code, "position_id": pos["id"],
                "sold_fraction": fraction}
    logger.warning("real mirror: SELL pos=%s mint=%s %s sold=%s of holding (min %.0f%%)",
                   pos["id"], pos.get("mint"), code,
                   "unknown" if fraction is None else "%.0f%%" % (fraction * 100), minimum * 100)
    res = ctx["exit_fn"](conn, pos, "mirror", decision_at)
    _alert_exit_result(pos["mint"], pos["id"], "mirror", res, handle=pos.get("source_handle"),
                       ticker=pos.get("ticker"), entry_notional=pos.get("entry_notional"))
    if (res or {}).get("status") == "closed":
        check_wallet_demotion(conn, pos.get("source_user_id"), network_id=pos.get("network_id"))
    return res


def _stale_sell_for_reentry(conn, pos, ev):
    """The refusal detail when a SELL predates the buy a winner re-entry copied, else None.

    Spec 2026-09-28 amendment B (Alpha), refined by Lambda's F1. A re-entry buys a coin its source
    may already have traded in an earlier round; a sell from that round delivered late would
    otherwise mirror-close the new position at once. The sell's source time (`created_at`) is
    compared with the ORIGIN BUY's source time (the attempt's `source_created_at`), the same feed
    clock: a round-2 sell made after that buy but before our decision start still mirrors. Only
    positions sealed with a `reentry_rank` are checked, so a first entry keeps today's mirror
    exactly. Any unreadable time mirrors as today."""
    try:
        contract = json.loads(pos.get("funded_contract_json") or "null") or {}
        rank = ((contract.get("classification") or {}).get("facts") or {}).get("reentry_rank")
    except (TypeError, ValueError, AttributeError):
        return None
    if not rank or not pos.get("attempt_id"):
        return None
    row = conn.execute("SELECT source_created_at FROM memecoin_entry_attempts WHERE attempt_id=?",
                       (pos["attempt_id"],)).fetchone()
    sold, bought = (_parse_iso_or_none(getattr(ev, "created_at", None)),
                    _parse_iso_or_none(row[0] if row else None))
    if sold is None or bought is None:
        return None
    from datetime import timezone
    sold, bought = (t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in (sold, bought))
    if sold >= bought:
        return None
    return "sell at %s predates the re-entry %s origin buy at %s" % (
        sold.isoformat(), rank, bought.isoformat())

_SOURCE_ORIGIN = re.compile(r"^(?:onchain|bscfast|rhfast):([^:]+):(0x[0-9a-f]{64}):(0x[0-9a-f]{40})$")


def _copied_source_block(conn, pos):
    """The block of the source buy this position copied, or None: the attempt's origin names the
    source transaction (onchain:/bscfast:/rhfast:<chain>:<tx>:<mint>), and our own detection of
    it holds its block (onchain_wallet_detections.source_slot)."""
    try:
        row = conn.execute("SELECT origin_event_id FROM memecoin_entry_attempts WHERE attempt_id=?",
                           (pos.get("attempt_id"),)).fetchone()
        hit = _SOURCE_ORIGIN.match(str(row[0] or "").lower()) if row else None
        if not hit:
            return None
        slot = conn.execute("SELECT source_slot FROM onchain_wallet_detections WHERE signature=?",
                            ("%s:%s:%s" % hit.groups(),)).fetchone()
        return int(slot[0]) if slot and slot[0] is not None else None
    except Exception:
        logger.exception("onchain sell mirror: source block lookup failed pos=%s", pos.get("id"))
        return None


def _entry_pending(conn, uid, chain, mint):
    """True while a reservation for this (wallet, chain, coin) is unresolved: our fill may land."""
    return conn.execute(
        "SELECT 1 FROM memecoin_pending_entries WHERE status='reserved' AND network_id=? "
        "AND lower(mint)=? AND source_user_id=? LIMIT 1", (chain, mint, uid)).fetchone() is not None


def real_onchain_sell_mirror(conn, row, contexts, *, now_fn, lane="onchain_sell_mirror"):
    """Our EVM watcher saw an allowlisted wallet SELL (one onchain_evm_sell_shadow row): exit our
    open position in that coin when the sale was at least MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION of
    its holding (operator 2026-10-07, china_sniper_1 on 56: "mirror sells, whatever happens first").

    The fraction is exact: sold_atomic / pre_atomic, both from the sale's own block. Unknown (no
    pre balance) HOLDS on the rails, as the size-aware J7 mirror does. A sale detected before our
    fill sold a holding we did not copy and is ignored. The operator's per-position hold
    (mirror_disabled) and a paused position win. One decision per (position, sell tx): the audit
    row keyed `onchain_sell:<tx>` is the durable intent. Proved unsigned deferrals stay pending;
    unknown attempts retain ownership and never resend blindly. Legacy audit rows remain decided.
    Exits only; nothing here touches an entry."""
    uid, chain = row.get("source_user_id"), str(row.get("network_id") or "")
    mint, tx_hash = str(row.get("mint") or "").lower(), str(row.get("tx_hash") or "").lower()
    from sniper.services.memecoin_onchain_lane import mirror_wallets
    if not uid or uid not in mirror_wallets(conn, chain):
        return None
    pending = next((item for item in m.pending_onchain_sell_mirrors(conn, chain)
                    if item["id"] == row.get("id")), None)
    pos = (m.get_position(conn, pending["mirror_position_id"]) if pending is not None else
           m.get_open_position(conn, uid, mint, cfg.MEMECOIN_REAL_POLICY, mode="real", network_id=chain))
    if not pos:
        # A SALE WHILE OUR ENTRY IS IN FLIGHT (Gamma MEDIUM on 92756ef1d): the lane holds the row and
        # asks again once the reservation resolves, so a fast flip mirrors the moment we fill.
        return {"status": "entry_pending"} if _entry_pending(conn, uid, chain, mint) else None
    key = "onchain_sell:%s" % tx_hash
    audit = conn.execute("SELECT detail FROM memecoin_exit_trigger_audit WHERE position_id=? "
                         "AND reason='mirror' AND checked_minute=?", (pos["id"], key)).fetchone()
    try:
        previous = json.loads(audit[0]) if audit else {}
    except (TypeError, ValueError):
        previous = {}
    if audit and (not isinstance(previous, dict) or previous.get("mirror_state") not in ("pending", "executing")
                  or previous.get("sell_row_id") != row.get("id")):
        return {"status": "already_decided", "position_id": pos["id"]}
    if pos.get("is_open") == 0:
        if audit:
            m.compare_exit_trigger_audit_detail(conn, position_id=pos["id"], reason="mirror", key=key,
                previous_detail=audit[0], detail=json.dumps({**previous, "mirror_state": "done"}, sort_keys=True),
                checked_at=now_fn())
        return {"status": "already_closed", "position_id": pos["id"]}
    if pos.get("is_open") != 1:
        return {"status": "mirror_pending", "reason": "position_unknown", "position_id": pos["id"]}
    if audit:
        row = {**row, "sold_atomic": previous.get("sold_atomic"), "pre_atomic": previous.get("pre_atomic"),
               "post_atomic": previous.get("post_atomic"),
               "block_number": previous.get("block_number"), "detected_at": previous.get("sale_detected_at"),
               "proof": previous.get("proof")}
    sold_at = _parse_iso_or_none(row.get("detected_at"))
    filled_at = _parse_iso_or_none(pos.get("opened_recorded_at") or pos.get("opened_at"))
    if sold_at is not None and filled_at is not None:
        from datetime import timedelta, timezone
        from sniper.util.clock import offset_s
        sold_at, filled_at = (t if t.tzinfo else t.replace(tzinfo=timezone.utc)
                              for t in (sold_at, filled_at))
        # The watcher stamps HOST time; the fill is corrected real UTC. Same clock before comparing
        # (never diff host against corrected stamps).
        sold_at = sold_at - timedelta(seconds=float(offset_s() or 0.0))
    try:
        sold, pre = int(row.get("sold_atomic") or 0), int(row.get("pre_atomic") or 0)
    except (TypeError, ValueError):
        sold, pre = 0, 0
    fraction = (sold / pre) if sold > 0 and pre > 0 else None
    minimum = float(cfg.MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION)
    full_only = minimum >= 1.0
    from sniper.services.memecoin_funded_policy import is_source_mirror_tp100_position
    exact_mirror = is_source_mirror_tp100_position(pos)
    balances_known = all(type(value) is int or (
        isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]*", value))
        for value in (row.get("sold_atomic"), row.get("pre_atomic"), row.get("post_atomic")))
    full_sale = balances_known and sold > 0 and sold == pre and int(row["post_atomic"]) == 0
    source_block = previous.get("source_block") if audit else _copied_source_block(conn, pos)
    try:
        sale_block = int(row["block_number"]) if row.get("block_number") is not None else None
    except (TypeError, ValueError):
        sale_block = None
    if pos.get("tracking_paused_at"):
        code = "tracking_paused"
    elif pos.get("mirror_disabled"):
        code = "mirror_disabled"
    elif source_block is not None and sale_block is not None and sale_block < source_block:
        # THE REFERENCE IS HIS BUY, NOT OUR FILL (Gamma MEDIUM on 92756ef1d): a sale after the buy
        # we copied sold what we copied, even when it landed before our fill (a fast flip). The
        # same block counts as after: selling an older holding in his own buy block is not his
        # pattern. Only an unknown source block falls back to our fill time.
        code = "sold_before_source_buy"
    elif (source_block is None or sale_block is None) and (sold_at is None or filled_at is None):
        code = "time_unknown"
    elif (source_block is None or sale_block is None) and sold_at < filled_at:
        code = "sold_before_fill"
    elif fraction is None:
        code = "size_unknown"
    elif exact_mirror:
        code = ("source_partial" if balances_known and 0 < sold <= pre
                and int(row["post_atomic"]) == pre - sold else "size_unknown")
    elif full_only and not full_sale:
        code = "size_hold" if balances_known and 0 < sold < pre else "size_unknown"
    elif fraction >= minimum:
        code = "size_mirror"
    else:
        code = "size_hold"
    decision_at = now_fn()
    if code == "source_partial":
        from sniper.services.memecoin_manual_exit_intents import enqueue_source_partial
        intent_id = enqueue_source_partial(
            conn, position_id=pos["id"], network_id=chain, source_owner=row.get("address"),
            mint=mint, source_signature=tx_hash, source_event_index=0,
            source_sold_atomic=sold, source_pre_atomic=pre, now_iso=decision_at,
            source_proof={"proof": row.get("proof"), "block_number": row.get("block_number"),
                          "sell_row_id": row.get("id"), "detected_at": row.get("detected_at")})
        m.record_exit_trigger_audit(
            conn, position_id=pos["id"], reason="mirror", display_price=None, preview_usdc=None,
            allowed=True, detail=json.dumps({"code": code, "mirror_state": "done",
                "sell_row_id": row.get("id"), "source_request_id": intent_id,
                "sold_atomic": str(sold), "pre_atomic": str(pre)}, sort_keys=True),
            checked_at=decision_at, lane=lane, key=key, replace=False)
        return {"status": "source_partial_queued", "position_id": pos["id"], "intent_id": intent_id}
    detail = {"code": code, "sell_tx": tx_hash, "proof": row.get("proof"),
              "sold_atomic": str(sold), "pre_atomic": str(pre) if pre else None,
              "post_atomic": str(row["post_atomic"]) if row.get("post_atomic") is not None else None,
              "sold_fraction": None if fraction is None else round(min(fraction, 1.0), 4),
              "min_fraction": minimum, "network_id": chain, "mint": mint,
              "sale_detected_at": row.get("detected_at"), "block_number": row.get("block_number"),
              "source_block": source_block, "sell_row_id": row.get("id"),
              "mirror_state": "pending" if code == "size_mirror" else "done"}
    if not audit:
        m.record_exit_trigger_audit(
            conn, position_id=pos["id"], reason="mirror", display_price=None, preview_usdc=None,
            allowed=(code == "size_mirror"), detail=json.dumps(detail, sort_keys=True),
            checked_at=decision_at, lane=lane, key=key, replace=False)
        audit = conn.execute("SELECT detail FROM memecoin_exit_trigger_audit WHERE position_id=? "
                             "AND reason='mirror' AND checked_minute=?", (pos["id"], key)).fetchone()
        try:
            previous = json.loads(audit[0]) if audit else {}
        except (TypeError, ValueError):
            previous = {}
        if (not isinstance(previous, dict) or previous.get("mirror_state") not in ("pending", "executing", "done")
                or previous.get("sell_row_id") != row.get("id")):
            return {"status": "already_decided", "position_id": pos["id"]}
    if code != "size_mirror":
        if previous.get("mirror_state") == "pending":
            m.compare_exit_trigger_audit_detail(conn, position_id=pos["id"], reason="mirror", key=key,
                previous_detail=audit[0], detail=json.dumps(detail, sort_keys=True), checked_at=decision_at,
                allowed=False)
        logger.warning("onchain sell mirror: HOLD pos=%s mint=%s %s tx=%s sold=%s (min %.0f%%)",
                       pos["id"], mint, code, tx_hash[:12],
                       "unknown" if fraction is None else "%.0f%%" % (min(fraction, 1.0) * 100),
                       minimum * 100)
        return {"status": "mirror_held", "reason": code, "position_id": pos["id"],
                "sold_fraction": fraction}
    if previous.get("mirror_state") == "done":
        return {"status": "already_decided", "position_id": pos["id"]}
    if any(pos.get(field) for field in ("close_claim_id", "close_sig", "close_pending_at",
                                       "derisk_claim_id", "derisk_sig")):
        return {"status": "mirror_pending", "reason": "close_owned", "position_id": pos["id"]}
    if previous.get("mirror_state") != "pending":
        return {"status": "mirror_pending", "reason": "execution_unknown", "position_id": pos["id"]}
    ctx = context_for_position(contexts, pos)
    if ctx is None:
        logger.warning("onchain sell mirror: no context for network=%s pos=%s (skipped)", chain, pos["id"])
        return {"status": "mirror_pending", "reason": "context_missing", "position_id": pos["id"]}
    detail["mirror_state"] = "executing"
    executing = json.dumps(detail, sort_keys=True)
    if not m.compare_exit_trigger_audit_detail(conn, position_id=pos["id"], reason="mirror", key=key,
            previous_detail=audit[0], detail=executing, checked_at=decision_at):
        return {"status": "mirror_pending", "reason": "audit_owned", "position_id": pos["id"]}
    logger.warning("onchain sell mirror: SELL pos=%s mint=%s tx=%s sold=%.0f%% of holding (min %.0f%%)",
                   pos["id"], mint, tx_hash[:12], min(fraction, 1.0) * 100, minimum * 100)
    res = ctx["exit_fn"](conn, pos, "mirror", decision_at)
    from sniper.execution.exec_lock import UnchangedWalletResult
    if isinstance(res, UnchangedWalletResult):
        detail.update(mirror_state="pending", pending_reason=res.get("reason"))
        m.compare_exit_trigger_audit_detail(conn, position_id=pos["id"], reason="mirror", key=key,
            previous_detail=executing, detail=json.dumps(detail, sort_keys=True), checked_at=decision_at)
        return {"status": "mirror_pending", "reason": res.get("reason"), "position_id": pos["id"]}
    fresh = m.get_position(conn, pos["id"])
    if ((res or {}).get("status") in ("closed", "already_closed")
            or (fresh is not None and fresh.get("is_open") == 0)):
        detail["mirror_state"] = "done"
        m.compare_exit_trigger_audit_detail(conn, position_id=pos["id"], reason="mirror", key=key,
            previous_detail=executing, detail=json.dumps(detail, sort_keys=True), checked_at=decision_at)
    else:
        return {"status": "mirror_pending", "position_id": pos["id"],
                "reason": "close_owned" if any((fresh or {}).get(field) for field in
                    ("close_claim_id", "close_sig", "close_pending_at", "derisk_claim_id", "derisk_sig"))
                    else "execution_unknown"}
    _alert_exit_result(pos["mint"], pos["id"], "mirror", res, handle=pos.get("source_handle"),
                       ticker=pos.get("ticker"), entry_notional=pos.get("entry_notional"))
    if (res or {}).get("status") == "closed":
        check_wallet_demotion(conn, pos.get("source_user_id"), network_id=pos.get("network_id"))
    return res


def real_mirror_on_sell(conn, ev, contexts, *, now_fn):
    """A curated SELL: exit only when exact local evidence does not show continued confidence.

    A source partial sell or another fresh active armed holder suppresses only this feed-driven
    mirror. Independent price/time/manual safety rails remain active. Any missing or malformed
    evidence holds in full-only mode: USD size cannot prove a zero token balance.
    """
    pos = m.get_open_position(
        conn, ev.source_user_id, ev.mint, cfg.MEMECOIN_REAL_POLICY, mode="real",
        network_id=getattr(ev, "network_id", None),
    )
    if not pos:
        # The sell's chain tag is unreliable (feed omits networkId on a chunk of curated EVM
        # events; follow_activity never resolves one) while real entries always store a
        # concrete chain — an exact-bucket miss here would hold a funded bag through the
        # trader's dump. Fall back to the network-agnostic row; refuse only a genuine
        # cross-chain ambiguity (loud, so the operator can close manually).
        cands = m.open_positions_for_coin(
            conn, ev.source_user_id, ev.mint, cfg.MEMECOIN_REAL_POLICY, mode="real")
        if len(cands) == 1:
            pos = cands[0]
        elif cands:
            logger.warning(
                "real mirror-close ambiguous across chains uid=%s mint=%s n=%d — NOT closing",
                ev.source_user_id, ev.mint, len(cands))
            return None
        else:
            return None
    # DATA ONLY (operator 2026-09-30): a J7 sell never exits a position on a listed EVM chain,
    # whatever its contract says. Keyed on the POSITION's chain: J7 sells often carry none.
    if getattr(ev, "signal_source", None) == "follow_activity":
        from sniper.services.memecoin_chains import canonical_chain_id as _canon
        if str(_canon(pos.get("network_id")) or "") in _j7_data_only_evm_chains():
            return {"status": "j7_data_only", "position_id": pos["id"]}
    if pos.get("tracking_paused_at"):
        return {"status": "tracking_paused", "position_id": pos["id"]}
    from sniper.services.memecoin_funded_policy import is_source_mirror_tp100_position
    if is_source_mirror_tp100_position(pos):
        # Feed USD amounts cannot size a token sale; the receipt-backed mirrors own this profile.
        return {"status": "mirror_held", "reason": "exact_ratio_unproven", "position_id": pos["id"]}
    stale = _stale_sell_for_reentry(conn, pos, ev)
    if stale:
        logger.warning("real mirror: pos=%s mint=%s stale sell ignored: %s (rails still active)",
                       pos["id"], pos.get("mint"), stale)
        return {"status": "stale_sell", "position_id": pos["id"], "reason": stale}
    from sniper.services.memecoin_funded_policy import effective_funded_policy, is_exit_trial_v4
    frozen, effective = effective_funded_policy(pos, override=None)
    contract_mirror_disabled = frozen is not None and not effective["mirror_on_sell"]
    first_sell = frozen is not None and is_exit_trial_v4(frozen) and not contract_mirror_disabled
    from sniper.services.memecoin_chains import canonical_chain_id
    size_chain = (canonical_chain_id(pos.get("network_id")) or str(pos.get("network_id") or "")
                  ).lower() in cfg.MEMECOIN_REAL_MIRROR_SIZE_CHAINS
    full_only = cfg.MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION >= 1.0
    if (size_chain or first_sell) and not pos.get("mirror_disabled") and not full_only:
        # SIZE-AWARE COPY-SELL (2026-09-15): an operator overlay on the frozen contract for the
        # listed chains. The per-position operator hold (mirror_disabled) still wins, above.
        ctx = context_for_position(contexts, pos)
        if ctx is None:
            logger.warning("real mirror: no context for network=%s pos=%s (skipped)",
                           pos.get("network_id"), pos["id"])
            return None
        return _size_aware_mirror(conn, ev, pos, ctx, now_fn(), first_sell=first_sell)
    if pos.get("mirror_disabled") or (contract_mirror_disabled and not size_chain):
        # Operator override: hold this position past the trader's sell. Only the copy-exit is
        # suppressed — the exit sweep's TP / stop / de-risk / timecap / unquotable rails still guard
        # it, so it is never left unprotected. Loud so the operator sees the mirror was skipped.
        logger.warning("real mirror: pos=%s mint=%s mirror_disabled by operator -> HOLD "
                    "(rails still active)", pos["id"], pos.get("mint"))
        return {"status": "mirror_disabled", "position_id": pos["id"]}
    ctx = context_for_position(contexts, pos)
    if ctx is None:
        logger.warning("real mirror: no context for network=%s pos=%s (skipped)",
                       pos.get("network_id"), pos["id"])
        return None
    decision_at = now_fn()
    event_id = getattr(ev, "fomo_event_id", None)
    minimum = cfg.MEMECOIN_REAL_MIRROR_MIN_REMAINING_USD
    source_remaining = m.source_remaining_usd_for_event(
        conn, event_id, source_user_id=pos.get("source_user_id"),
        network_id=pos.get("network_id"), mint=pos.get("mint"),
    )
    holders = []
    if full_only:
        # Only the receipt-backed on-chain mirrors can prove token liquidation. Even an explicit
        # USD zero can hide token dust; neither a rebuilt share nor the V4 first sell overrides it.
        code = "size_hold" if source_remaining is not None and source_remaining > 0 else "size_unknown"
    elif source_remaining is not None and source_remaining >= minimum:
        code = "source_still_holds"
    else:
        holders = m.active_armed_holders_for_coin(
            conn,
            network_id=pos.get("network_id"),
            mint=pos.get("mint"),
            exclude_user_id=pos.get("source_user_id"),
            observed_at=decision_at,
            window_s=cfg.MEMECOIN_REAL_MIRROR_HOLDER_WINDOW_S,
            min_remaining_usd=minimum,
        )
        code = "armed_consensus" if holders else "mirror_allowed"
    handles = sorted({h.get("source_handle") or h.get("source_user_id") for h in holders})
    detail = {
        "code": code,
        "origin_event_id": event_id,
        "source_remaining_usd": source_remaining,
        "consensus_count": len(holders),
        "consensus_handles": handles,
        "network_id": pos.get("network_id"),
        "mint": pos.get("mint"),
        "holder_window_s": cfg.MEMECOIN_REAL_MIRROR_HOLDER_WINDOW_S,
        "min_remaining_usd": minimum,
        "min_fraction": cfg.MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION,
    }
    m.record_exit_trigger_audit(
        conn, position_id=pos["id"], reason="mirror", display_price=None,
        preview_usdc=None, allowed=(code == "mirror_allowed"),
        detail=json.dumps(detail, sort_keys=True), checked_at=decision_at,
        # A wallet sell is its own lane: neither loop chose this moment, the trader did.
        lane="mirror_on_sell",
    )
    if code != "mirror_allowed":
        logger.warning(
            "real mirror: HOLD pos=%s network=%s mint=%s reason=%s source_remaining=%s "
            "armed_holders=%d",
            pos["id"], pos.get("network_id"), pos.get("mint"), code,
            source_remaining, len(holders),
        )
        return {
            "status": "mirror_held",
            "reason": code,
            "position_id": pos["id"],
            "source_remaining_usd": source_remaining,
            "consensus_count": len(holders),
            "consensus_handles": handles,
        }
    res = ctx["exit_fn"](conn, pos, "mirror", decision_at)
    _alert_exit_result(pos["mint"], pos["id"], "mirror", res, handle=pos.get("source_handle"),
                       ticker=pos.get("ticker"), entry_notional=pos.get("entry_notional"))
    if (res or {}).get("status") == "closed":
        check_wallet_demotion(conn, pos.get("source_user_id"),
                      network_id=pos.get("network_id"))
    return res


DERISK_REARM_MIN_AGE_S = 20 * 60          # blockhash long expired — not_found is then decisive
DERISK_REARM_NOT_FOUND_HITS = 3           # spaced explicit not_found reads before re-arming


def sig_provably_dead(pos, *, height_fn, balance_fn):
    """True when this exit signature can NEVER land and provably never did.

    Used by BOTH recovery paths. The de-risk path has always had it; the full-close path went
    without and stranded position 37331 on 2026-08-14 (see `recover_unverified_closes`). The proof
    is identical for either, because both persist the same `last_valid_height` + `balance_snapshot`
    on their intent — only the claim column they own differs.

    `DERISK_REARM_MIN_AGE_S` is a 20-minute PROXY for "the blockhash window is long gone" — its own
    comment says so. A Solana blockhash dies after ~150 slots (~60 s), and the exact expiry is
    already recorded on the intent as `last_valid_height`, so the proxy waits about 20x longer than
    the fact it stands in for. That wait is not free: `claim_position_close` refuses while
    `derisk_sig` is set, so for those 20 minutes the position cannot be exited AT ALL — not by the
    rails, not by the operator's close button. Live on 2026-08-12, position 36517 (MOONDENG) sat
    wedged at +125% and had to be reconciled by hand.

    Two independent facts, and BOTH are required:

      1. the chain is past `last_valid_height`  -> the transaction can never land in the future;
      2. the on-chain balance still equals the intent's `balance_snapshot` -> it never landed in
         the past.

    (1) alone is not enough: a lagging node can answer `not_found` for a sell that landed just
    before expiry, which is exactly why the existing path demands three spaced reads. (2) closes
    that hole with committed state instead of another opinion about the same transaction.

    Fail-closed everywhere. A missing intent, a missing height, an unreadable balance, or any
    exception returns False and leaves the 20-minute path in charge.
    """
    try:
        intent = json.loads(pos.get("exit_intent_json") or "{}")
    except (TypeError, ValueError):
        return False
    last_valid = intent.get("last_valid_height")
    snapshot = intent.get("balance_snapshot")
    if not isinstance(last_valid, (int, float)) or not isinstance(snapshot, (int, float)):
        return False
    try:
        height = height_fn()
        balance = balance_fn(pos["mint"])
    except Exception:
        logger.exception("derisk liveness probe failed pos=%s", pos.get("id"))
        return False
    if not isinstance(height, (int, float)) or balance is None:
        return False
    if float(height) <= float(last_valid):
        return False                       # still inside the window; the sell may yet land
    # A landed partial would have REDUCED the bag. Require the full snapshot to still be held;
    # tolerate only floating-point noise, never a real decrease.
    return float(balance) >= float(snapshot) * 0.999999


# PROTOCOL EXPIRY (eth-mainnet spec v3 §3.7, step 3.2). A chain whose sends are private (chain 1,
# Flashbots) hides a held transaction from custody's pending pool, so `pending == latest` proves
# nothing there. The one proof that signed swap bytes can no longer execute is their OWN deadline
# passing on CHAIN time. Rungs an expiry chain may return, and the route kind whose swap carries the
# deadline (`evm_real_seams._SWAP_DEADLINE_WORD`); a rung missing here fails a test, because its
# rows would otherwise be held forever.
def _expiry_proven_rungs():
    from sniper.execution.evm_source_route import SOURCE_RUNGS
    # Every source-route rung builds through the V4 builders, so its swap carries the V4 deadline.
    return {"v3": "v3", "v2": "v2", "v4": "v4", "v4_multihop": "v4",
            **{name: "v4" for name in SOURCE_RUNGS}}


EXPIRY_PROVEN_RUNGS = _expiry_proven_rungs()


def evm_swap_expired(network_id, signed_payload, latest_block_ts):
    """None off the expiry chains (the caller keeps its own proof). On one: an evidence string when
    the chain's latest block time is past the swap's decoded deadline plus the margin, else False.

    False is a HOLD and covers every gap: no payload, an unknown selector, undecodable bytes, an
    unread or unreadable block time, or a deadline that has not passed. `latest_block_ts` is the
    reader's RESULT, so a reader that raised must be passed as None by the caller.
    """
    chain = str(network_id)
    if chain not in cfg.memecoin_evm_expiry_chains():
        return None
    from sniper.execution.evm_real_seams import decode_signed_swap_deadline
    deadline = decode_signed_swap_deadline(signed_payload)
    if deadline is None:
        return False
    if isinstance(latest_block_ts, bool) or not isinstance(latest_block_ts, int):
        return False
    margin = cfg.memecoin_evm_expiry_margin_s(chain)
    if latest_block_ts <= deadline + margin:
        return False
    return "chain time %d is past the swap deadline %d + %d s" % (latest_block_ts, deadline, margin)


def proof_snapshot(read):
    """One block's view `{block, ts, nonce, balance}` from a reader, or None when the read failed,
    raised, or is not whole. On an expiry chain the dead proof takes every fact from this, so the
    chain time, the nonce and the bag can never come from three different heights."""
    try:
        snap = read()
    except Exception:
        logger.exception("evm proof snapshot failed")
        return None
    if not isinstance(snap, dict):
        return None
    for key, kinds in (("block", (int,)), ("ts", (int,)), ("nonce", (int,)),
                       ("balance", (int, float))):
        value = snap.get(key)
        if isinstance(value, bool) or not isinstance(value, kinds):
            return None
    return snap


def evm_close_provably_dead(pos, *, confirmed_nonce_fn, balance_fn,
                            pending_nonce_fn=None, native_balance_fn=None,
                            now_iso=None, min_age_s=None, proof_snapshot_fn=None):
    """A reason string when an EVM close can NEVER land and provably never did, else None.

    The reason is the evidence, not a category: the caller writes it straight into the release log
    so a funded ledger mutation can be audited without re-reading the chain.

    `sig_provably_dead` CANNOT RUN ON EVM. It requires `last_valid_height`, and an EVM exit intent
    records `last_valid_height: null` — a blockhash expiry is a Solana concept. An EVM transaction
    stays valid until its NONCE is consumed, so the mined nonce is the equivalent fact. The Solana
    branch also needs `block_height_fn`, which the EVM context does not provide at all, so that
    branch's guard was permanently False on 4663 and the auto-release could never fire there.

    That is why a human has now freed a funded bag by hand THREE times: position 37331 (RIDEABULL)
    at +48% on 2026-08-14, position 48554 (RIZZLER) on 2026-09-02, whose claim was pinned on a sell
    dropped from the mempool entirely, and position 49252 (PRC) on 2026-09-03 — the case the second
    proof below exists for.

    Two independent facts, and BOTH are required:

      1. the account's MINED nonce is past the intent's nonce -> that nonce is already spent by a
         transaction which is NOT this one. If it were this one the receipt read would have
         answered confirmed or failed rather than not_found, which is the caller's precondition.
         So this signature can never mine.
      2. the on-chain balance still holds the full snapshot -> it never landed in the past.

    (1) alone is not enough: a lagging node can answer not_found for a sell that landed moments
    earlier. (2) closes that with committed state rather than another opinion about the same
    transaction. A landed PARTIAL would have reduced the bag, so the comparison tolerates
    floating-point noise and never a real decrease — identical to the Solana proof.

    Fail-closed everywhere. A missing intent, a missing nonce, an unreadable balance or any
    exception returns None and the claim stays held for a human. An outage releases nothing.
    """
    try:
        intent = json.loads(pos.get("exit_intent_json") or "{}")
    except (TypeError, ValueError):
        return None
    nonce = intent.get("nonce")
    snapshot = intent.get("balance_snapshot")
    if not isinstance(nonce, int) or not isinstance(snapshot, (int, float)):
        return None
    # ON AN EXPIRY CHAIN EVERY FACT COMES FROM ONE BLOCK (step 3.2, Gamma MEDIUM). The nonce, the
    # bag and the chain time are each a hedged read: taken separately at "latest" they can describe
    # three heights, and a fresher chain time beside a staler nonce and bag calls a sell that
    # landed `expired`. The snapshot reads the latest block once and the other two at its number.
    expiry_chain = str(pos.get("network_id")) in cfg.memecoin_evm_expiry_chains()
    chain_ts = None
    if expiry_chain:
        snap = proof_snapshot(lambda: proof_snapshot_fn(pos["mint"])) if callable(
            proof_snapshot_fn) else None
        if snap is None:
            return None
        mined_nonce, balance, chain_ts = snap["nonce"], snap["balance"], snap["ts"]
    else:
        try:
            mined_nonce = confirmed_nonce_fn()
            balance = balance_fn(pos["mint"])
        except Exception:
            logger.exception("evm dropped-close probe failed pos=%s", pos.get("id"))
            return None
        if not isinstance(mined_nonce, int) or balance is None:
            return None
    bag_intact = float(balance) >= float(snapshot) * 0.999999
    if mined_nonce > nonce:
        if not bag_intact:
            return None                    # a landed partial: not this transaction's story
        return ("nonce %d spent by another transaction, past intent nonce %d, bag %s still holds "
                "snapshot %s" % (mined_nonce, nonce, balance, snapshot))

    # SECOND PROOF — THE NONCE IS STILL FREE AND NO NODE HOLDS THE SELL.
    #
    # Proof one cannot reach the case that actually pins bags. A sell the node never ADMITTED never
    # moves the nonce, so `mined_nonce > nonce` stays false forever and the claim is held for a
    # human. PRC 49252 on 2026-09-03 is the exact shape: latest == pending == 1335 == the intent
    # nonce, receipt null and hash unknown on all three endpoints, full bag held — pinned through
    # its own take-profit and closed 31 minutes later at -$7.46 against $122.12 projected.
    #
    # `pending` counts our own unmined transactions. So `latest == pending == intent nonce` is the
    # node stating it holds NOTHING of ours AND that this nonce is still unspent. That is a
    # stronger statement than "no receipt", which one lagging node can get wrong.
    #
    # WHY RELEASING IS SAFE HERE. The re-sell reuses this same nonce. Two transactions sharing one
    # nonce can never both mine — at most one is included, ever. The guarantee is nonce
    # exclusivity, not a mempool opinion, so even a node that lied about holding nothing cannot
    # produce a double sell.
    #
    # All four facts are required and every read failure returns None, so an outage releases
    # nothing.
    if not bag_intact or mined_nonce != nonce:
        return None                        # a landed partial, or a nonce behind the intent
    # ON AN EXPIRY CHAIN THE SECOND PROOF IS THE SELL'S OWN DEADLINE (step 3.2). Custody cannot see
    # a privately held sell, so `pending == latest` is no evidence there; and the re-sell reuses
    # this nonce, which must not reach the relay while the old bytes could still execute.
    if expiry_chain:
        expired = evm_swap_expired(
            pos.get("network_id"), intent.get("signed_payload_b64"), chain_ts)
        if not expired:
            return None
        return ("expired_unmined: %s at block %d, nonce %d still unspent, bag %s still holds "
                "snapshot %s" % (expired, snap["block"], nonce, balance, snapshot))
    if pending_nonce_fn is None:
        return None
    if min_age_s is None:
        min_age_s = float(getattr(cfg, "MEMECOIN_EVM_DROPPED_CLOSE_MIN_AGE_S", 60.0) or 60.0)
    age = _seconds_since(pos.get("close_submitted_at"), now_iso)
    if age is None or age < float(min_age_s):
        return None                        # too young, or an unreadable stamp: hold it
    try:
        pending_nonce = pending_nonce_fn()
    except Exception:
        logger.exception("evm dropped-close pending-nonce probe failed pos=%s", pos.get("id"))
        return None
    if not isinstance(pending_nonce, int) or pending_nonce != mined_nonce:
        return None                        # a node still holds something of ours: it may yet mine

    # WHICH CAUSE. Both release, but they are different defects with different owners, so the label
    # has to survive into the log for anyone counting them apart later. Unaffordable gas is a
    # pre-mempool refusal (prevention: an affordability gate at signing); everything else is an
    # eviction or a drop after admission.
    label = "evicted_unmined"
    try:
        from sniper.execution.evm_real_seams import decode_signed_tx_cost
        cost = decode_signed_tx_cost(intent.get("signed_payload_b64"))
        native = native_balance_fn() if callable(native_balance_fn) else None
        if cost and native is not None and float(native) * 1e18 < float(cost["worst_case_wei"]):
            label = "never_admitted_gas"
    except Exception:
        logger.exception("dropped-close cause label failed pos=%s", pos.get("id"))
    return ("%s: latest == pending == intent nonce %d, bag %s still holds snapshot %s, "
            "unmined for %.0fs" % (label, nonce, balance, snapshot, age))


# The de-risk path's original name. Kept so its own tests and call site read unchanged.
derisk_provably_dead = sig_provably_dead


def _seconds_since(stamp_iso, now_iso):
    """Seconds between an ISO stamp and now, or None if either is unreadable.

    None is not zero. A missing or malformed `close_submitted_at` must hold the claim, never
    satisfy an age floor that exists to stop a just-broadcast sell being declared dead.
    """
    if not stamp_iso:
        return None
    try:
        then = datetime.fromisoformat(str(stamp_iso).replace("Z", "+00:00"))
        now = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
               if now_iso else datetime.now(timezone.utc))
        if then.tzinfo is None or now.tzinfo is None:
            return None
        return max(0.0, (now - then).total_seconds())
    except (TypeError, ValueError):
        return None


def _iso_seconds_ago(now_iso, seconds):
    from datetime import timedelta
    dt = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    return (dt - timedelta(seconds=float(seconds))).isoformat()


def recover_unverified_derisks(conn, contexts, now_iso, *, network_ids=None):
    """Settle unknown-confirm derisk partials (2026-07-17, pos 6605: the one-shot burned
    silently on an unknown confirm and the coin rode full size off a +204% peak). Per
    position with a persisted derisk_sig and no booked derisk fill, one single-shot
    confirm read via the position's OWN venue:

      confirmed          -> the sell DID land: keep the one-shot, stop rechecking, alert
                            (proceeds are unbooked — reconcile owns the drift; operator settles).
      failed             -> provably reverted: clear the one-shot -> the rail re-arms.
      not_found          -> decisive only when repeated: 3 spaced explicit reads AND the
                            stamp older than 20 min (blockhash window long gone) -> re-arm.
      pending/unavailable-> keep waiting; never re-arm on ambiguity (double-sell risk).

    CLOSED positions are in the queue too (2026-07-22, pos 9003 — the trail closed it 371s
    after an unknown-confirm derisk, 829s before not_found became decisive, and the old
    is_open=1 filter then stranded the sig forever). The state machine is identical; only
    the language and the alerts differ, because a closed row has no rail to re-arm. Settling
    it is what keeps `derisked_at` honest and clears the audit debt.
    """
    def recovery_lock(pos):
        return _derisk_recovery_lock(contexts, pos)

    stale_bound = _iso_seconds_ago(now_iso, float(getattr(
        cfg, "MEMECOIN_DERISK_PENDING_STALE_S", 600.0) or 600.0))
    for pos in m.stale_derisk_pendings(conn, stale_bound):
        if network_ids is not None and m.normalized_coin_network(pos.get("network_id"), pos.get("mint")) not in network_ids:
            continue
        with recovery_lock(pos) as held:
            if not held:
                continue
            released = m.release_stale_derisk_pending(
                conn, pos["id"], pos.get("derisk_claim_id"), pos.get("derisk_pending_at"))
        if released:
            logger.warning("derisk pending claim released: pos=%s mint=%s pending_since=%s",
                           pos["id"], pos.get("mint"), pos.get("derisk_pending_at"))

    # Age paces orphan checks; only the executor's actual lock establishes absence.
    min_age_s = float(getattr(cfg, "MEMECOIN_DERISK_RECOVERY_MIN_AGE_S", 120.0) or 0.0)
    submitted_before = _iso_seconds_ago(now_iso, min_age_s) if min_age_s > 0 else None
    for pos in m.unverified_derisks(conn, submitted_before_iso=submitted_before):
        if network_ids is not None and m.normalized_coin_network(pos.get("network_id"), pos.get("mint")) not in network_ids:
            continue
        with recovery_lock(pos) as held:
            if not held:
                continue
        ctx = context_for_position(contexts, pos) or {}
        confirm_once = ctx.get("confirm_once_fn")
        if confirm_once is None:
            continue
        # Never hold the execution lock across RPC, balance reads, or alerts.
        try:
            state = confirm_once(pos["derisk_sig"])
        except Exception:
            logger.exception("derisk recovery confirm failed pos=%s", pos["id"])
            continue
        is_open = bool(pos.get("is_open"))
        age_s = _held_s(pos.get("derisked_at"), now_iso)
        height_fn, balance_fn = ctx.get("block_height_fn"), ctx.get("token_checked_fn")
        age_decisive = (int(pos.get("derisk_recheck_hits") or 0) + 1 >= DERISK_REARM_NOT_FOUND_HITS
                        and (not is_open or age_s > DERISK_REARM_MIN_AGE_S))
        dead = (state == "not_found" and not age_decisive and is_open and height_fn and balance_fn
                and derisk_provably_dead(pos, height_fn=height_fn, balance_fn=balance_fn))
        claim_id, sig = pos.get("derisk_claim_id"), pos.get("derisk_sig")
        rearmed_reason = None
        with recovery_lock(pos) as held:
            if not held:
                continue
            current = m.get_position(conn, pos["id"])
            if (not current or current.get("derisk_claim_id") != claim_id
                    or current.get("derisk_sig") != sig):
                continue
            if state == "confirmed":
                settled, needs_operator = _settle_derisk_outcome(conn, pos, state, claim_id, sig, now_iso)
                if not settled:
                    continue
            elif state == "failed":
                if not _settle_derisk_outcome(conn, pos, state, claim_id, sig, now_iso)[0]:
                    continue
                rearmed_reason = "tx reverted on-chain"
            elif state == "not_found":
                hits = m.bump_derisk_recheck(
                    conn, pos["id"], claim_id=claim_id, sig=sig, exact_claim=True)
                if hits is None:
                    continue
                decisive = dead or (hits >= DERISK_REARM_NOT_FOUND_HITS and (
                    not is_open or age_s > DERISK_REARM_MIN_AGE_S))
                if decisive:
                    if not m.clear_position_derisk(
                            conn, pos["id"], claim_id=claim_id, sig=sig, exact_claim=True):
                        continue
                    m.set_exit_intent_status(conn, claim_id, "dropped", now_iso)
                    rearmed_reason = ("tx provably dead" if dead else
                                      f"tx dropped ({hits} not_found reads)")
            elif state in ("pending", "unavailable"):
                m.set_exit_intent_status(conn, claim_id, "submitted_unknown", now_iso)
        handle, ticker = pos.get("source_handle"), pos.get("ticker")
        if state == "confirmed":
            logger.warning("derisk recovery: sig LANDED pos=%s mint=%s open=%s — booked unpriced, "
                           "quantity %s; realized stays unknown until its receipt prices it",
                           pos["id"], pos["mint"], is_open,
                           "UNKNOWN (operator)" if needs_operator else "from its intent")
            _safe_alert(lambda *a: alerts.alert_derisk_landed_late(
                *a, handle=handle, ticker=ticker, operator=needs_operator), pos["mint"], sig)
        elif rearmed_reason:
            logger.warning("derisk recovery: pos=%s mint=%s — %s",
                           pos["id"], pos["mint"], rearmed_reason)
            if is_open:
                _safe_alert(lambda *a: alerts.alert_derisk_rearmed(*a, handle=handle,
                                                                   ticker=ticker),
                            pos["mint"], rearmed_reason)

    _price_unpriced_derisks(conn, contexts, network_ids, recovery_lock)


def _source_partial_intent(conn, pos):
    sig = pos.get('derisk_sig')
    if not sig:
        return None
    return m.exit_intent_for_tx(conn, pos['id'], 'partial', sig)


def _settle_source_partial_outcome(conn, contexts, pos, state, claim_id, sig, now_iso):
    """Only exact failure or this signed transaction's actual receipt settles a source trim."""
    if state in ('failed', 'dropped'):
        return (m.reset_source_partial_submission(conn, pos['id'], claim_id, sig, now_iso,
                    intent_status=state) and m.release_source_partial(conn, pos['id'], claim_id))
    if state != 'confirmed':
        return False
    ctx = context_for_position(contexts, pos) or {}
    receipt_fn = ctx.get('source_partial_receipt_fn')
    intent = _source_partial_intent(conn, pos)
    if receipt_fn is None or not intent:
        return False
    try:
        proof = receipt_fn(sig, pos['mint'], intent)
        if (not isinstance(proof, dict) or type(proof.get('sold_atomic')) is not int
                or proof['sold_atomic'] != int(intent.get('sell_amount_atomic', 0))
                or type(proof.get('decimals')) is not int or proof['decimals'] != intent.get('sell_decimals')):
            return False
        return m.book_source_partial_fill(conn, position_id=pos['id'], claim_id=claim_id,
            tx_sig=sig, at=now_iso, sold_atomic=proof['sold_atomic'], sell_decimals=proof['decimals'],
            notional=proof['received'], fraction=float(intent['fraction']))
    except Exception:
        logger.exception('source partial receipt unavailable pos=%s', pos['id'])
        return False


def _source_partial_provably_dead(conn, pos, ctx):
    """Protocol expiry or a spent nonce, with the exact pre-sign bag still held. No age proof."""
    from sniper.services.memecoin_exit_intent import ExitIntent, PARTIAL
    raw = _source_partial_intent(conn, pos)
    intent = ExitIntent.from_json(json.dumps(raw)) if raw else None
    if (intent is None or intent.action != PARTIAL or not intent.signed_payload_b64
            or intent.position_id != pos['id'] or intent.claim_id != pos.get('derisk_claim_id')):
        return False
    expected = int(intent.balance_snapshot_atomic)
    network = m.normalized_coin_network(pos.get('network_id'), pos.get('mint'))
    try:
        if network == 'solana':
            read = ctx.get('source_partial_proof_snapshot_fn')
            if (read is None or type(intent.last_valid_height) is not int
                    or intent.last_valid_height < 0):
                return False
            snap = read(pos['mint'])
            if (not isinstance(snap, dict) or any(type(snap.get(k)) is not int or snap[k] < 0
                    for k in ('block_height', 'anchor_slot', 'balance_slot', 'balance_atomic', 'decimals'))):
                return False
            return (snap['block_height'] > intent.last_valid_height
                    and snap['balance_slot'] >= snap['anchor_slot']
                    and snap['balance_atomic'] == expected and snap['decimals'] == intent.sell_decimals)
        read = ctx.get('source_partial_proof_snapshot_fn')
        if read is None or type(intent.nonce) is not int or intent.nonce < 0:
            return False
        snap = read(pos['mint'])
        if (not isinstance(snap, dict) or any(type(snap.get(k)) is not int or snap[k] < 0
                for k in ('block', 'ts', 'nonce', 'balance_atomic', 'decimals'))
                or snap['balance_atomic'] != expected or snap['decimals'] != intent.sell_decimals):
            return False
        if snap['nonce'] > intent.nonce:
            return True
        if snap['nonce'] != intent.nonce:
            return False
        from sniper.execution.evm_real_seams import decode_signed_swap_deadline
        deadline = decode_signed_swap_deadline(intent.signed_payload_b64)
        margin = cfg.memecoin_evm_expiry_margin_s(network)
        return (type(deadline) is int and deadline > 0 and type(margin) is int and margin >= 0
                and snap['ts'] > deadline + margin)
    except Exception:
        logger.exception('source partial dead proof unavailable pos=%s', pos['id'])
        return False


def recover_source_partials(conn, contexts, now_iso, *, network_ids=None):
    """Off-entry worker recovery, independent of earlier partial fills and manual queue leases."""
    from sniper.services import memecoin_manual_exit_intents as source_requests
    rows = conn.execute("SELECT * FROM memecoin_manual_exit_intents WHERE action='source_partial' "
                        "AND state IN ('claimed','submitted_unknown') ORDER BY requested_at,rowid").fetchall()
    for raw in rows:
        request = dict(raw)
        pos = m.get_position(conn, request['position_id'])
        if not pos:
            continue
        network = m.normalized_coin_network(pos.get('network_id'), pos.get('mint'))
        if network_ids is not None and network not in network_ids:
            continue
        claim_id, sig = request.get('claim_id'), request.get('sig')
        ctx = context_for_position(contexts, pos) or {}
        fn = ctx.get('source_partial_fn')
        lock = getattr(fn, 'recovery_lock', None)
        if lock is None:
            continue
        # Full-sale requests use the existing full-close owner and its recovery exclusively.
        facts = json.loads(request['request_json'])
        if facts['source_sold_atomic'] == facts['source_pre_atomic']:
            if not pos.get('is_open'):
                source_requests.resolve(conn, request['intent_id'], state=source_requests.DONE,
                    now_iso=now_iso, result_status='already_closed', sig=sig)
            elif sig and not pos.get('close_claim_id'):
                outcome = conn.execute('SELECT status FROM memecoin_exit_intents WHERE claim_id=?',
                                       (claim_id,)).fetchone()
                if outcome and outcome[0] in ('failed','dropped'):
                    conn.execute("UPDATE memecoin_manual_exit_intents SET state='queued',sig=NULL,"
                                 "claim_id=NULL WHERE intent_id=? AND sig=?", (request['intent_id'], sig))
                    conn.commit()
            continue
        with lock() as held:
            if not held:
                continue
            current = m.get_position(conn, pos['id'])
            if not current or current.get('derisk_claim_id') != claim_id or current.get('derisk_sig') != sig:
                continue
            if sig is None:
                m.release_source_partial(conn, pos['id'], claim_id)
                continue
        confirm = ctx.get('confirm_once_fn')
        if confirm is None:
            continue
        try:
            state = confirm(sig)
            if state == 'not_found' and _source_partial_provably_dead(conn, pos, ctx):
                # The original signature must still be absent after the exact proof reads.
                state = 'dropped' if confirm(sig) == 'not_found' else 'unknown'
        except Exception:
            logger.exception('source partial confirmation unavailable pos=%s', pos['id'])
            continue
        # Read the actual receipt before reacquiring the wallet lock; the CAS below owns booking.
        if state == 'confirmed':
            reader = ctx.get('source_partial_receipt_fn')
            intent = _source_partial_intent(conn, pos)
            try:
                proof = reader(sig, pos['mint'], intent) if reader and intent else None
            except Exception:
                proof = None
            def frozen_receipt(*_):
                return proof
            settle_context = dict(ctx, source_partial_receipt_fn=frozen_receipt)
        else:
            settle_context = ctx
        with lock() as held:
            if not held:
                continue
            current = m.get_position(conn, pos['id'])
            if (current and current.get('derisk_claim_id') == claim_id
                    and current.get('derisk_sig') == sig):
                _settle_source_partial_outcome(conn, {network:settle_context}, current, state,
                                               claim_id, sig, now_iso)


def _derisk_recovery_lock(contexts, pos):
    """The position's partial executor's lock, taken without blocking; yields whether it is held."""
    from contextlib import nullcontext
    ctx = context_for_position(contexts, pos) or {}
    lock = getattr(ctx.get("derisk_fn"), "recovery_lock", None)
    # An absent execution identity cannot prove that its owner is gone.
    return lock() if lock is not None else nullcontext(False)


def _settle_derisk_outcome(conn, pos, state, claim_id, sig, now_iso):
    """Under the recovery lock, on the exact claim and sig: book a confirmed partial unpriced, or
    clear a failed one. Returns (settled, needs_operator); any other state settles nothing."""
    if state == "confirmed":
        # Codex N14: book the landed partial as an unpriced fill (the durable work item the
        # pricing pass settles from its receipt), never clear it unbooked.
        qty, fraction = _derisk_intent_qty(
            m.exit_intent_for_tx(conn, pos["id"], "derisk", sig), pos["id"], claim_id)
        return m.settle_confirmed_derisk_unpriced(
            conn, position_id=pos["id"], claim_id=claim_id, sig=sig, qty=qty,
            fraction=fraction, at=pos.get("derisked_at") or now_iso), qty is None
    if state == "failed":
        if not m.clear_position_derisk(conn, pos["id"], claim_id=claim_id, sig=sig, exact_claim=True):
            return False, False
        m.set_exit_intent_status(conn, claim_id, "failed", now_iso)
        return True, False
    return False, False


def settle_derisk_for_manual_close(conn, contexts, pos, now_iso):
    """The manual close's settle step (73882, 2026-10-15): the close was refused already_closing and
    waited 120 s for the recovery pass. None while MEMECOIN_MANUAL_CLOSE_SETTLES_PARTIAL is off."""
    if not getattr(cfg, "MEMECOIN_MANUAL_CLOSE_SETTLES_PARTIAL", False):
        return None
    return settle_derisk_before_close(conn, contexts, pos, now_iso)


def settle_derisk_before_close(conn, contexts, pos, now_iso, *, by="manual"):
    """One confirm read of the unknown-confirm partial in front of a close, no age gate, with the
    recovery's own lock discipline (absence check, read outside the lock, the exact claim and sig
    under it). Used by the manual close and by RailDeriskSettler.

    None: nothing blocks the close. 'confirmed' / 'failed': settled, the close may claim.
    'unknown': nothing changed. A not_found is unknown here, never a clear and never a recheck hit:
    only the paced recovery may decide a drop, so an unknown partial is never sold twice."""
    claim_id, sig = pos.get("derisk_claim_id"), pos.get("derisk_sig")
    if claim_id is None and sig is None:
        return None
    if _source_partial_intent(conn, pos) is not None:
        recover_source_partials(conn, contexts, now_iso,
            network_ids={m.normalized_coin_network(pos.get('network_id'), pos.get('mint'))})
        fresh = m.get_position(conn, pos['id'])
        return 'confirmed' if fresh and not fresh.get('derisk_claim_id') else 'unknown'
    confirm_once = (context_for_position(contexts, pos) or {}).get("confirm_once_fn")
    if sig is None or confirm_once is None:
        return "unknown"                     # an unsigned claim is its executor's, in flight
    with _derisk_recovery_lock(contexts, pos) as held:
        if not held:
            return "unknown"
    try:
        state = confirm_once(sig)
    except Exception:
        logger.exception("manual close: partial confirm failed pos=%s", pos["id"])
        return "unknown"
    if state not in ("confirmed", "failed"):
        return "unknown"
    with _derisk_recovery_lock(contexts, pos) as held:
        if not held:
            return "unknown"
        current = m.get_position(conn, pos["id"])
        if (not current or current.get("derisk_claim_id") != claim_id
                or current.get("derisk_sig") != sig):
            return "unknown"
        settled, needs_operator = _settle_derisk_outcome(conn, current, state, claim_id, sig, now_iso)
    if not settled:
        return "unknown"
    logger.warning("close settle (%s): partial %s %s pos=%s mint=%s%s", by, sig, state, pos["id"],
                   pos.get("mint"), " (booked unpriced)" if state == "confirmed" else "")
    if state == "confirmed":
        _safe_alert(lambda *a: alerts.alert_derisk_landed_late(
            *a, handle=pos.get("source_handle"), ticker=pos.get("ticker"), operator=needs_operator),
            pos["mint"], sig)
    return state


RAIL_SETTLE_OFFER_S = 2.0         # a skipped position is offered at most this often
RAIL_SETTLE_QUEUE = 16


class RailDeriskSettler:
    """Rails behind an unknown-confirm partial (Alpha GO 2026-10-15). real_exit_sweep skips a
    position whose de-risk claim or sig is set, so no rail ran, the stop included, until the 120 s
    recovery settled it. The sweep offers such a position here and moves on: offer() takes a lock,
    a dict and a non-blocking put, never an RPC or a DB write, so the exit tick never waits. This
    thread runs settle_derisk_before_close on its own connection; the next tick sweeps a settled
    row. MEMECOIN_RAIL_CLOSE_SETTLES_PARTIAL=0 offers nothing."""

    def __init__(self, contexts, *, connect, now_fn=None, clock=time.monotonic,
                 settle_fn=None, offer_every_s=RAIL_SETTLE_OFFER_S):
        from sniper.util.clock import real_utc_now_iso
        self.contexts, self.connect, self.clock = contexts, connect, clock
        self.now_fn = now_fn or real_utc_now_iso
        self.settle_fn = settle_fn or functools.partial(settle_derisk_before_close, by="rail")
        self.offer_every_s = float(offer_every_s)
        self._queue = queue.Queue(maxsize=RAIL_SETTLE_QUEUE)
        self._lock = threading.Lock()
        self._offered_at = {}
        self._stopping = threading.Event()
        self._thread = None
        self.stats = dict(offered=0, throttled=0, dropped=0, confirmed=0, failed=0, unknown=0,
                          errors=0, last_error=None)

    def offer(self, pos):
        try:
            if not getattr(cfg, "MEMECOIN_RAIL_CLOSE_SETTLES_PARTIAL", False):
                return False
            pid, now = int(pos["id"]), self.clock()
            with self._lock:
                last = self._offered_at.get(pid)
                if last is not None and now - last < self.offer_every_s:
                    self.stats["throttled"] += 1
                    return False
                self._offered_at = {k: t for k, t in self._offered_at.items() if now - t < 60.0}
                self._offered_at[pid] = now
            self._queue.put_nowait(pid)
            self.stats["offered"] += 1
            return True
        except queue.Full:
            self.stats["dropped"] += 1
            return False
        except Exception:
            logger.exception("rail derisk settler: offer failed")   # never the sweep's problem
            return False

    def run_once(self, timeout=None):
        pid = self._queue.get(timeout=timeout) if timeout else self._queue.get_nowait()
        conn = None
        try:
            conn = self.connect()
            pos = m.get_position(conn, pid)
            if (not pos or not pos.get("is_open") or (pos.get("execution_mode") or "") != "real"
                    or pos.get("close_claim_id") or pos.get("close_sig")):
                return None
            state = self.settle_fn(conn, self.contexts, pos, self.now_fn())
            if state in ("confirmed", "failed", "unknown"):
                self.stats[state] += 1
            return state
        except Exception as exc:
            self.stats["errors"] += 1
            self.stats["last_error"] = type(exc).__name__
            logger.exception("rail derisk settler: settle failed pos=%s", pid)
            return None
        finally:
            if conn is not None:
                conn.close()

    def _run(self):
        while not self._stopping.is_set():
            try:
                self.run_once(timeout=1.0)
            except queue.Empty:
                continue

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="rail-derisk-settler", daemon=True)
            self._thread.start()

    def stop(self):
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def status(self):
        return dict(self.stats, alive=bool(self._thread is not None and self._thread.is_alive()),
                    queued=self._queue.qsize())


def _derisk_intent_qty(intent, position_id, claim_id):
    """(qty, fraction) of a landed partial, by the executor's own formula (claim-time balance x
    fraction), only from the exact intent of that claim. (None, None) when it cannot say."""
    try:
        if (not isinstance(intent, dict) or int(intent.get("position_id")) != int(position_id)
                or str(intent.get("action")) != "derisk"
                or (claim_id is not None and str(intent.get("claim_id")) != str(claim_id))):
            return None, None
        fraction, balance = float(intent.get("fraction")), float(intent.get("balance_snapshot"))
    except (TypeError, ValueError):
        return None, None
    qty = balance * fraction
    ok = 0.0 < fraction < 1.0 and math.isfinite(qty) and qty > 0
    return (qty, fraction) if ok else (None, None)


def _frozen_receipt_proceeds(receipt_proceeds, pos, intent, sig):
    """USD proceeds of `sig` from its receipt, valued at the intent's frozen quote_asset_usd when
    it carries one. Without it, EVM is unpriced (Codex N13: never a later mark) and Solana's reader,
    which returns USDC, is asked without a price."""
    try:
        frozen = float((intent or {}).get("quote_asset_usd"))
    except (TypeError, ValueError):
        frozen = float("nan")
    if math.isfinite(frozen) and frozen > 0:
        return receipt_proceeds(sig, quote_asset_usd=frozen)
    if m.normalized_coin_network(pos.get("network_id"), pos.get("mint")) == "solana":
        return receipt_proceeds(sig)
    return None


def _price_unpriced_derisks(conn, contexts, network_ids, recovery_lock):
    """Receipt read outside the wallet lock; the write under it, so an exit executor that read
    derisk_proceeds as unknown cannot book its close after this priced the partial (realized
    would then stay NULL with nothing left to revisit it)."""
    for row in m.unpriced_derisk_fills(conn):
        if network_ids is not None and m.normalized_coin_network(row.get("network_id"), row.get("mint")) not in network_ids:
            continue
        receipt_proceeds = (context_for_position(contexts, row) or {}).get("receipt_proceeds_fn")
        if receipt_proceeds is None:
            continue
        sig = row["derisk_tx"]
        try:
            received = _frozen_receipt_proceeds(
                receipt_proceeds, row, m.exit_intent_for_tx(conn, row["id"], "derisk", sig), sig)
        except Exception:
            logger.exception("unpriced derisk receipt failed pos=%s", row["id"])
            continue
        if received is None or not math.isfinite(received) or received <= 0:
            continue
        with recovery_lock(row) as held:
            booked = held and m.reconcile_unpriced_derisk(
                conn, fill_id=row["fill_id"], position_id=row["id"], sig=sig, notional=received,
                fill_source="chain_confirmed_receipt_reconciled")
        if booked:
            logger.warning("unpriced derisk reconciled from receipt: pos=%s mint=%s received=%.6f",
                           row["id"], row.get("mint"), received)


def recover_orphaned_closes(conn, now_iso, *, network_ids=None):
    """Settle positions claimed for close (is_open=0) that a crash left unfinalized (closed_at NULL).

    WO-9 crash-injection surfaced this window: the exit executor does claim_close -> insert_fill ->
    finalize_close as three adjacent commits with no I/O between them, so a hard kill / power loss
    can strand a position is_open=0 with no close bookkeeping — invisible to the open sweep, unclosed
    on the ledger. Two cases, distinguished by whether a full-exit leg was already booked:

      fill booked, finalize missing -> the sell landed; finalize from the booked proceeds
                                       (realized = full-exit received + de-risk proceeds - entry).
      no booked fill                -> quarantine. Legacy code broadcast before claim_close, so this
                                       state cannot prove that nothing sold; reopening can double-sell.

    Idempotent: a row already finalized (closed_at set) is not in `orphaned_closes`. Returns the
    count recovered."""
    n = 0
    for pos in m.orphaned_closes(conn):
        if network_ids is not None and m.normalized_coin_network(pos.get("network_id"), pos.get("mint")) not in network_ids:
            continue
        pid = pos["id"]
        count, received = m.full_exit_proceeds(conn, pid)
        if count > 0:
            realized = m.realized_after_exit(conn, pid, received, pos.get("entry_notional"))
            m.finalize_close(conn, pid, now_iso, realized, exit_reason="recovered_orphan_close")
            logger.warning("orphaned close finalized (crash between claim and finalize): pos=%s "
                           "mint=%s received=%.2f realized=%s", pid, pos.get("mint"), received,
                           realized)
        else:
            logger.error("legacy orphaned close quarantined (submission truth unknown): pos=%s "
                         "mint=%s — operator reconciliation required", pid, pos.get("mint"))
        n += 1
    return n


def _resume_flap_quote_exit(conn, contexts, pos, now_iso):
    """True when `pos` is a chain-56 flap quote-token exit past leg 1 (its close_sig is leg 1): call
    its exit again so the next step runs even when no exit trigger re-fires (the step budget returns
    `pending` between steps). Through the venue's serialized exit_fn, so under the wallet lock."""
    if str(pos.get("network_id")) != "56" or not getattr(cfg, "MEMECOIN_EVM_FLAP_QUOTE_EXIT", False):
        return False
    from sniper.execution import evm_flap_quote_exit as fqe
    if not fqe.resume_due(pos["id"]):
        return False
    if not fqe.resume_admit(pos["id"]):
        return True  # The journal still owns leg 1; a retry cap is not a completed full close.
    ctx = context_for_position(contexts, pos)
    if not ctx or not ctx.get("exit_fn"):
        return True                        # nothing to resume with; never the generic recovery
    try:
        res = ctx["exit_fn"](conn, pos, "manual", now_iso)
        logger.warning("flap quote exit resumed by recovery pos=%s -> %s %s", pos["id"],
                       (res or {}).get("status"), (res or {}).get("reason") or "")
    except Exception:
        logger.exception("flap quote exit resume failed pos=%s", pos["id"])
    return True


def _adopt_landed_replaced_close(conn, pos, confirm_once, now_iso):
    """A chain-1 replacement keeps the earlier hashes of the same nonce in the intent's
    `replaced_tx_identities`. At most one of them can land. When one confirmed, move the claim's
    identity to it (compare-and-set) so the close books on THAT hash and its receipt, and return the
    re-read position; else None. The intent keeps the replacement's quote fields: the booking reads
    only the identity, the claim, the balance snapshot and the frozen quote-asset price."""
    try:
        intent = json.loads(pos.get("exit_intent_json") or "{}")
        replaced = [str(h) for h in (intent.get("replaced_tx_identities") or [])]
    except (TypeError, ValueError, AttributeError):
        return None
    for tx in replaced:
        try:
            if confirm_once(tx) != "confirmed":
                continue
        except Exception:
            logger.exception("replaced close confirm failed pos=%s tx=%s", pos["id"], tx)
            continue
        adopted = json.dumps({**intent, "tx_identity": tx, "replaced_tx_identities": [
            h for h in replaced if h != tx] + [str(pos["close_sig"])]}, sort_keys=True)
        if m.swap_close_identity(conn, pos["id"], pos["close_claim_id"], pos["close_sig"], tx,
                                 expected_intent_json=pos["exit_intent_json"],
                                 new_intent_json=adopted, at=now_iso):
            logger.warning("full close: the replaced sell landed pos=%s sig=%s (replacement %s "
                           "never can: same nonce)", pos["id"], tx, pos["close_sig"])
            return m.get_position(conn, pos["id"])
    return None


def recover_unverified_closes(conn, contexts, now_iso, *, network_ids=None):
    """Recover only provably unsigned claims; inspect signed full exits without re-arming them.

    A durable signature is submitted-unknown. Even `failed`/`not_found` reads are not enough for an
    automatic second full-balance sell: recovery leaves the claim owned until explicit reconciliation.
    """
    for pos in m.unpriced_full_exits(conn):
        if network_ids is not None and m.normalized_coin_network(pos.get("network_id"), pos.get("mint")) not in network_ids:
            continue
        ctx = context_for_position(contexts, pos)
        receipt_proceeds = (ctx or {}).get("receipt_proceeds_fn")
        if receipt_proceeds is None:
            continue
        try:
            received = _frozen_receipt_proceeds(
                receipt_proceeds, pos,
                m.exit_intent_for_tx(conn, pos["id"], "close", pos["close_sig"]), pos["close_sig"])
        except Exception:
            logger.exception("unpriced close receipt failed pos=%s", pos["id"])
            continue
        quantity = pos.get("unpriced_qty")
        if received is None or received <= 0 or quantity is None or quantity <= 0:
            continue
        if m.reconcile_unpriced_full_exit(
                conn, position_id=pos["id"], sig=pos["close_sig"],
                price=received / quantity, qty=quantity, notional=received,
                fill_source="chain_confirmed_receipt_reconciled"):
            logger.warning("unpriced close reconciled from receipt: pos=%s mint=%s",
                           pos["id"], pos.get("mint"))

    stale_bound = _iso_seconds_ago(now_iso, float(getattr(
        cfg, "MEMECOIN_CLOSE_PENDING_STALE_S", 600.0) or 600.0))
    for pos in m.stale_close_pendings(conn, stale_bound):
        if network_ids is not None and m.normalized_coin_network(pos.get("network_id"), pos.get("mint")) not in network_ids:
            continue
        if m.release_stale_close_pending(
                conn, pos["id"], pos.get("close_claim_id"), pos.get("close_pending_at")):
            logger.warning("unsigned close claim released after executor death: pos=%s mint=%s",
                           pos["id"], pos.get("mint"))

    held = 0
    for pos in m.unverified_closes(conn):
        if network_ids is not None and m.normalized_coin_network(pos.get("network_id"), pos.get("mint")) not in network_ids:
            continue
        held += 1
        if _resume_flap_quote_exit(conn, contexts, pos, now_iso):
            continue
        ctx = context_for_position(contexts, pos)
        confirm_once = (ctx or {}).get("confirm_once_fn")
        state = "unavailable"
        if confirm_once is not None:
            try:
                state = confirm_once(pos["close_sig"])
            except Exception:
                logger.exception("full-close recovery confirm failed pos=%s", pos["id"])
            if state != "confirmed":
                landed = _adopt_landed_replaced_close(conn, pos, confirm_once, now_iso)
                if landed is not None:
                    pos, state = landed, "confirmed"
        if state == "confirmed" and not pos.get("lifi_dependent"):
            receipt_proceeds = (ctx or {}).get("receipt_proceeds_fn")
            received = None
            intent = {}
            quantity = None
            quote_asset_usd = None
            try:
                intent = json.loads(pos.get("exit_intent_json") or "{}")
                if isinstance(intent, dict):
                    candidate = float(intent.get("balance_snapshot"))
                    if (int(intent.get("position_id")) == pos["id"]
                            and str(intent.get("claim_id")) == str(pos["close_claim_id"])
                            and str(intent.get("tx_identity")) == str(pos["close_sig"])
                            and math.isfinite(candidate)):
                        quantity = candidate
            except (TypeError, ValueError, KeyError):
                quantity = None
            try:
                candidate = float((intent or {}).get("quote_asset_usd"))
                if math.isfinite(candidate) and candidate > 0:
                    quote_asset_usd = candidate
            except (TypeError, ValueError, AttributeError):
                pass
            if receipt_proceeds is not None:
                try:
                    received = _frozen_receipt_proceeds(
                        receipt_proceeds, pos, {"quote_asset_usd": quote_asset_usd},
                        pos["close_sig"])
                except Exception:
                    logger.exception("full-close recovery receipt failed pos=%s", pos["id"])
            if received is not None and received > 0 and quantity is not None and quantity > 0:
                reason = pos.get("close_exit_reason") or "recovered_confirmed_close"
                settled = m.book_full_exit_and_finalize(
                    conn, position_id=pos["id"], claim_id=pos["close_claim_id"],
                    sig=pos["close_sig"], closed_at=now_iso,
                    realized_pnl=m.realized_after_exit(conn, pos["id"], received,
                                                       pos.get("entry_notional")),
                    exit_reason=reason, fill_kind=m.full_exit_fill_kind(reason),
                    price=received / quantity, qty=quantity, notional=received,
                )
                if settled:
                    logger.warning("full close recovered from confirmed receipt: pos=%s mint=%s",
                                   pos["id"], pos.get("mint"))
                    continue
            if quantity is not None and quantity > 0:
                reason = pos.get("close_exit_reason") or "recovered_confirmed_close"
                settled = m.book_full_exit_and_finalize(
                    conn, position_id=pos["id"], claim_id=pos["close_claim_id"],
                    sig=pos["close_sig"], closed_at=now_iso, realized_pnl=None,
                    exit_reason=reason, fill_kind=m.full_exit_fill_kind(reason),
                    price=None, qty=quantity, notional=None,
                    fill_source="chain_confirmed_unpriced",
                )
                if settled:
                    logger.warning("full close recovered without USD proceeds: pos=%s mint=%s",
                                   pos["id"], pos.get("mint"))
                    continue
        # A `not_found` read alone stays ambiguous — a lagging node answers that for a sell which
        # landed just before expiry. Exact evidence settles it: past `last_valid_height` the
        # transaction can never land, and an intact bag proves it never did. Until this fires the
        # position cannot be exited AT ALL, by the rails or by the operator's button, which is how
        # 37331 sat frozen at +48% on 2026-08-14 until it was released by hand.
        height_fn = (ctx or {}).get("block_height_fn")
        balance_fn = (ctx or {}).get("token_checked_fn")
        nonce_fn = (ctx or {}).get("confirmed_nonce_fn")
        dead = None
        evm_proof = False
        if state == "not_found" and height_fn and balance_fn and sig_provably_dead(
                pos, height_fn=height_fn, balance_fn=balance_fn):
            dead = "chain past last_valid_height, full bag intact"
        elif state == "not_found" and nonce_fn and balance_fn:
            evm_proof = True
            # The proof returns its own evidence string, so a funded ledger mutation can be audited
            # from the log without re-reading the chain — and without a second round of reads that
            # could disagree with the ones the proof actually used.
            dead = evm_close_provably_dead(
                pos, confirmed_nonce_fn=nonce_fn, balance_fn=balance_fn,
                pending_nonce_fn=(ctx or {}).get("pending_nonce_fn"),
                native_balance_fn=(ctx or {}).get("native_fn"), now_iso=now_iso,
                proof_snapshot_fn=(ctx or {}).get("proof_snapshot_fn")) or None
        if dead:
            # EVM: never under a copy that may still be sent (memecoin_exit_rebroadcast): the
            # nonce may be free and the re-armed exit would sign a new sell beside the old bytes.
            # At most one copy's deadline (8 s), then the next sweep releases.
            from sniper.services.memecoin_exit_rebroadcast import copy_in_flight
            if evm_proof and copy_in_flight(pos.get("close_sig")):
                logger.warning("full close: release deferred, a re-offered copy is in flight "
                               "pos=%s sig=%s", pos["id"], pos.get("close_sig"))
                continue
            m.set_exit_intent_status(conn, pos.get("close_claim_id"), "dropped", now_iso)
            if m.release_dropped_close_claim(
                    conn, pos["id"], pos.get("close_claim_id"), pos.get("close_sig")):
                logger.warning(
                    "full close: sig provably dead pos=%s mint=%s sig=%s (%s) — claim RELEASED, "
                    "exit re-armed", pos["id"], pos.get("mint"), pos.get("close_sig"), dead)
                _safe_alert(lambda *a: alerts.alert_close_rearmed(
                    *a, handle=pos.get("source_handle"), ticker=pos.get("ticker")),
                    pos["mint"], "close tx dropped (blockhash expired, bag intact)")
                continue
        # CHAIN-1 PROTECT REPLACEMENT (operator via Alpha, 2026-10-08). Protect simulated the held
        # sell as a revert on consecutive polls, so it will sit until its deadline. Sign a SAME-NONCE
        # replacement here, in the exit lane (the watcher thread never signs). Never a release: the
        # claim, and the dead proofs above, stay exactly as they are.
        if state not in ("confirmed", "failed"):
            from sniper.execution import chain1_protect
            replace_fn = (ctx or {}).get("replace_close_fn")
            chain = m.normalized_coin_network(pos.get("network_id"), pos.get("mint"))
            if replace_fn is not None and chain1_protect.replace_requested(chain, pos.get("close_sig")):
                # One attempt per flag (a retry needs 2 fresh reverting polls, ~24 s) and at most
                # MEMECOIN_CHAIN1_PROTECT_REPLACE_MAX attempts per close claim, whatever each one did:
                # the 2 s chain-1 sweep shares its thread with chain-1 entries (Gamma MEDIUM-2).
                chain1_protect.consume_revert(pos.get("close_sig"))
                if not chain1_protect.take_attempt(
                        pos.get("close_claim_id"), cfg.MEMECOIN_CHAIN1_PROTECT_REPLACE_MAX):
                    continue
                try:
                    replace_fn(conn, pos)
                except Exception:
                    logger.exception("chain1 replace failed pos=%s (the held sell stands)", pos["id"])
                continue
        # NOT DEAD, NOT LANDED: the sell may still be in flight. Re-offer its OWN stored bytes.
        #
        # Identical bytes are the identical transaction with the identical nonce, so the chain can
        # include it at most once -- idempotence by construction, not a mitigation. It never signs,
        # never re-quotes, never touches the claim; resolution stays with the proofs above.
        #
        # Bounded per submission, because idempotent is not free. Three bags pinned on 2026-09-02
        # were never lost in transit: a node refuses a transaction when gasLimit * gasPrice exceeds
        # the wallet balance whatever the swap consumes, so every sell was rejected BEFORE the
        # mempool. Re-offering those bytes earns the identical refusal, and only the bound stops it
        # becoming the 2026-08-21 quota finding again.
        #
        # PER-CHAIN SWITCH, EMPTY BY DEFAULT (2026-09-30). This block read `_send` and
        # `_confirm_once` from the context and no context ever carried them, so it never ran. The
        # contexts now carry `reoffer_send_fn` beside `confirm_once_fn`; whether a chain re-offers
        # is MEMECOIN_PINNED_CLOSE_REOFFER_CHAINS, the operator's decision.
        if (state == "not_found" and pos.get("exit_intent_json")
                and m.normalized_coin_network(pos.get("network_id"), pos.get("mint"))
                in cfg.memecoin_pinned_close_reoffer_chains()):
            _send_raw = (ctx or {}).get("reoffer_send_fn")
            _confirm_once = (ctx or {}).get("reoffer_confirm_once_fn")      # capped at 2 s
            if _send_raw and _confirm_once:
                # DETACHED: the copy's receipt read and send run on a daemon thread and never
                # hold this sweep. EVM's never-admitted proof frees the claim at `_release_age`
                # with the nonce unspent, and no copy may arrive after it (Solana: no such release).
                _release_age = (None if nonce_fn is None
                                else cfg.MEMECOIN_EVM_DROPPED_CLOSE_MIN_AGE_S)
                try:
                    from sniper.services import memecoin_exit_rebroadcast as _reb
                    _outcome = _reb.rebroadcast_pinned_exit_detached(
                        pos["exit_intent_json"], pos.get("close_submitted_at"), now_iso=now_iso,
                        receipt_fn=lambda h, _once=_confirm_once: (
                            None if str(_once(h)) == "not_found" else {"state": "present"}),
                        send_fn=_send_raw, release_age_s=_release_age)
                    if _outcome == "dispatched":
                        logger.warning(
                            "pinned close re-offer started pos=%s mint=%s sig=%s — identical "
                            "bytes, same nonce", pos["id"], pos.get("mint"), pos.get("close_sig"))
                except Exception:
                    logger.exception("pinned close re-offer failed pos=%s", pos.get("id"))
        # A RECEIPT WITH status 0x0 IS PROOF, AND IT WAS BEING TREATED AS AN UNKNOWN.
        # `eth_confirm` returns "failed" for exactly one thing: a MINED receipt whose status is
        # 0x0. A reverted transaction changes no state and moves no tokens, so the bag is intact by
        # construction — no balance read required, which matters because this fires during the
        # provider trouble that usually caused the revert.
        #
        # That is STRICTLY STRONGER evidence than the `not_found` branch above, which has to infer
        # "it never landed" from an expiry height PLUS an intact bag — and only that weaker case
        # released. So a provably reverted close pinned the position forever.
        #
        # RIZZLER 48554, 2026-09-02: a b_stop fired on a quote reading -38.6%, the sell reverted
        # (0x2f6f86c6…, status 0x0, 739k gas), and the position sat un-exitable while the next two
        # readings said +23% and +33%. Five further exit attempts all returned `stuck`. The log
        # below said "remains submitted_unknown … never auto-released" even though `lifecycle` had
        # already computed "failed" — the message named a state the code had not chosen.
        if state == "failed":
            m.set_exit_intent_status(conn, pos.get("close_claim_id"), "failed", now_iso)
            if m.release_dropped_close_claim(
                    conn, pos["id"], pos.get("close_claim_id"), pos.get("close_sig")):
                logger.warning(
                    "full close REVERTED on chain pos=%s mint=%s sig=%s (receipt status 0x0, no "
                    "tokens moved) — claim RELEASED, exit re-armed",
                    pos["id"], pos.get("mint"), pos.get("close_sig"))
                _safe_alert(lambda *a: alerts.alert_close_rearmed(
                    *a, handle=pos.get("source_handle"), ticker=pos.get("ticker")),
                    pos["mint"], "close tx reverted on chain (receipt status 0x0, bag intact)")
                continue
            logger.error(
                "full close reverted but the claim would not release pos=%s mint=%s sig=%s — "
                "held for a human", pos["id"], pos.get("mint"), pos.get("close_sig"))
            _alert_close_claim_held(pos, now_iso)
            continue
        lifecycle = ({"confirmed": "confirmed", "failed": "failed"}.get(
            state, "submitted_unknown"))
        m.set_exit_intent_status(conn, pos.get("close_claim_id"), lifecycle, now_iso)
        log_sampled(logger.error, ("close_submitted_unknown", pos["id"]),
                    "full close remains submitted_unknown: pos=%s mint=%s sig=%s chain_state=%s "
                    "— never auto-released", pos["id"], pos.get("mint"), pos.get("close_sig"), state)
        _alert_close_claim_held(pos, now_iso)
    return held


# One page per stranded close, at the point where a human must look at it. Position 37331 froze at
# 12:05 and the ONLY symptom was a button answering `already_closing` — nothing paged, because the
# stuck-bag alert covers an un-exitable TOKEN, not an un-exitable CLAIM.
CLOSE_CLAIM_HELD_ALERT_S = float(getattr(cfg, "MEMECOIN_CLOSE_CLAIM_HELD_ALERT_S", 600.0) or 600.0)
_close_claim_alerted = set()


def _alert_close_claim_held(pos, now_iso):
    claim_id = pos.get("close_claim_id")
    if not claim_id or claim_id in _close_claim_alerted:
        return
    if _held_s(pos.get("close_submitted_at"), now_iso) < CLOSE_CLAIM_HELD_ALERT_S:
        return
    _close_claim_alerted.add(claim_id)
    _safe_alert(lambda *a: alerts.alert_close_claim_held(
        *a, handle=pos.get("source_handle"), ticker=pos.get("ticker")),
        pos["mint"], pos["id"])


# --- exit-engine-v3 funded authority adapter (2026-07-24 prereg; flag-gated by
#     MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS at reservation time, wide-envelope canary) ---
#
# The engine (memecoin_exit_brain.propose_v5) never invents an exit route: CUT maps onto the
# existing 'real_trail' reason, DERISK/TAKE_PROFIT onto the existing partial de-risk executor. It
# runs ONLY for a position whose FROZEN contract already selected the engine-envelope-v1 policy
# (tp/derisk/trail/stop zeroed there — see memecoin_funded_policy._engine_envelope_policy), and
# only when the envelope's own live rails (catastrophe / unquotable / timecap, still computed by
# alpha_b_real_exit_reason exactly as for any other position) found nothing this sweep — the
# envelope always wins a tie. DEFER/RIDE take no action; the envelope rails alone still guard the
# position, identical to a non-capable engine state.


def _connect_exit_state():
    """Open the worker-owned file without any pre-hook schema or WAL writes."""
    from sniper.persistence import db_exit_state
    return db_exit_state.connect_exit_state_existing(busy_ms=250)


def _engine_authoritative_ids(positions, frozen_by_id, *, limit):
    """Oldest-position-id-first bound (prereg §5 N=5 canary): at most `limit` open
    engine-authoritative positions may be engine-driven THIS sweep; any beyond it run the
    envelope's rails only, same as every other position. The chain must ALSO be currently listed in
    the LIVE `MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS` (material defect (b), Codex 2026-07-24
    activation-blocker review): the frozen contract's policy_id alone is not a runtime kill switch —
    an operator clearing the flag must demote every existing engine-envelope position on that chain
    to envelope-only rails at the very next sweep, not just block new reservations."""
    ids = sorted(
        pos["id"] for pos in positions
        if (frozen_by_id.get(pos["id"]) is not None
            and frozen_by_id[pos["id"]].policy_id == ENGINE_ENVELOPE_POLICY_ID
            and engine_policy_runtime_capable(frozen_by_id[pos["id"]])
            and m.normalized_coin_network(pos.get("network_id"), pos.get("mint"))
                in cfg.engine_authoritative_chains())
    )
    return set(ids[:max(0, int(limit))])


def _state_age_s(computed_time, now_iso):
    """Seconds between a MarketState's own `computed_time` and the sweep's `now_iso`, or None when
    either is unparseable — distinct from 0.0 (which would wrongly pass a freshness gate)."""
    computed, now = _parse_ts(computed_time), _parse_ts(now_iso)
    if computed is None or now is None:
        return None
    return (now - computed).total_seconds()


def _read_engine_state(xc, pos, now_iso):
    """The latest materialized MarketState for one position, validated fresh and identity-matched
    before the engine may ever act on it — or None (never raises: a missing exit-state DB, a
    missing table, an absent row, a malformed state_json, or a failed validation all fall back to
    envelope-only, exactly like a non-capable state).

    P0-1 (Codex 2026-07-24 activation-blocker review): `db_exit_state.latest_state` selects strictly
    by position_id with no other check — a January snapshot for a since-recycled position_id (a DB
    reset/backup restore, or any other id reuse) would otherwise be read as authoritative for a
    brand-new July position on the same id. Three independent guards, ALL must hold:
      1. identity  — state.mint/chain_id must match the position's mint/network_id.
      2. version   — state.engine_version/feature_version must match the CURRENT feature-computation
                      code (a row materialized under older feature code is not read as current).
      3. freshness — state.computed_time must be within MEMECOIN_ENGINE_STATE_MAX_AGE_S of `now_iso`.
    """
    if xc is None:
        return None
    try:
        from sniper.persistence import db_exit_state
        row = db_exit_state.latest_state(xc, pos["id"])
        if not row or not row.get("state_json"):
            return None
        state = market_state_from_dict(json.loads(row["state_json"]))
        state_id = row.get("id")
    except Exception:
        logger.exception("engine exit: state read failed pos=%s", pos.get("id"))
        return None
    expected_chain = str(pos.get("network_id") or "")
    expected_mint = str(pos.get("mint") or "")
    if state.chain_id != expected_chain or state.mint != expected_mint:
        logger.warning(
            "engine exit pos=%s: state identity mismatch (state chain=%s mint=%s vs position "
            "chain=%s mint=%s) -> envelope only", pos.get("id"), state.chain_id, state.mint,
            expected_chain, expected_mint)
        return None
    if state.engine_version != mkstate.ENGINE_VERSION or state.feature_version != mkstate.FEATURE_VERSION:
        logger.warning(
            "engine exit pos=%s: state version stale (state engine=%s feature=%s vs current "
            "engine=%s feature=%s) -> envelope only", pos.get("id"), state.engine_version,
            state.feature_version, mkstate.ENGINE_VERSION, mkstate.FEATURE_VERSION)
        return None
    age_s = _state_age_s(state.computed_time, now_iso)
    if age_s is None or age_s < 0 or age_s > cfg.MEMECOIN_ENGINE_STATE_MAX_AGE_S:
        logger.warning(
            "engine exit pos=%s: state stale or unreadable (computed_time=%s age_s=%s max=%s) -> "
            "envelope only", pos.get("id"), state.computed_time, age_s,
            cfg.MEMECOIN_ENGINE_STATE_MAX_AGE_S)
        return None
    return state, state_id


def _engine_audit_record(pos, state, decision, now_iso, state_id=None, *, engine_version,
                         future_context=None):
    """Build this tick's completeable audit record without touching SQLite."""
    actionable = decision.action in (CUT, DERISK, TAKE_PROFIT)
    return {
        "position_id": pos["id"],
        "decided_at": now_iso,
        "state_id": state_id,
        "engine_version": engine_version,
        "feature_version": state.feature_version,
        "candidate_action": decision.action,
        "proposed_fraction": decision.proposed_fraction,
        "reason_codes": list(decision.reason_codes),
        # propose_v5 is consulted only after the fixed envelope returned no action.
        "legacy_action": "hold",
        "divergent": actionable,
        "executable_confirmed": False,
        "censored": False,
        "input_block_ts": state.source_event_time,
        "no_action_reason": decision.no_action_reason,
        "outcome": (None if actionable else f"no_action:{str(decision.action).lower()}"),
        "future_context": future_context,
    }


def _flush_engine_decisions(xc, records):
    """Best-effort telemetry flush after every funded position hook has returned."""
    if xc is None:
        return
    from sniper.persistence import db_exit_state
    for rec in records:
        try:
            db_exit_state.write_candidate_decision(xc, rec)
        except Exception:
            logger.exception(
                "engine exit: deferred decision audit failed pos=%s (funded hooks complete)",
                rec.get("position_id"))


# pos_id -> monotonic ts of the last executable-gate REFUSAL of an engine CUT (cooldown store;
# process-lifetime, cleared on successful map or expiry — a restart simply retries, which is safe)
_ENGINE_CUT_COOLDOWN = {}


def _map_engine_decision(decision):
    """Pure: CandidateDecision -> (reason, fraction). CUT maps onto the existing 'real_trail' full
    exit reason; DERISK/TAKE_PROFIT map onto the existing 'derisk' partial reason with the engine's
    fraction; RIDE/DEFER (or no decision) is (None, None) — no action, no new exit route ever
    invented. Structural, not by discipline: this is the ONLY place a CandidateDecision becomes a
    reason string, and it can only ever produce one of these two existing reasons or nothing."""
    if decision is None:
        return None, None
    if decision.action == CUT:
        return decision.exit_reason, None
    if decision.action in (DERISK, TAKE_PROFIT):
        return "derisk", decision.proposed_fraction
    return None, None


def _reduce_engine_controller(conn, pos, state, preview, now_iso):
    """Fold one funded-sweep observation and persist it with CAS.

    This is the sole production writer for WO-5 controller state. The hot lane maintains the same
    reducer in memory and only wakes this funded sweep; it never writes the funded ledger. Repeated
    materialization of the same decoded event and cached quote is a no-op, so controller revisions
    do not continuously supersede an execution intent.
    """
    from sniper.analysis.exit_controller import Observation, controller_state_from_row, reduce
    from sniper.analysis.memecoin_quote_failures import is_infra

    exact_mid = _executable_unit_price(conn, pos, preview)
    _keep_route_quote(pos, preview, "peak_preview")
    quote_value = _preview_value(preview, "expected_usdc")
    quote_at = _preview_value(preview, "quoted_at")
    try:
        quote_value = float(quote_value) if quote_value is not None else None
    except (TypeError, ValueError):
        quote_value = None
    if quote_value is not None and (not math.isfinite(quote_value) or quote_value <= 0):
        quote_value = None

    failure = getattr(preview, "failure", None)
    infra_failure = bool(failure is not None and is_infra(failure))
    # The WO-4 pre-pass already applies provider-wide outage classification and mutates these two
    # in-memory counters. Use its verdict for untyped failures instead of reclassifying an outage as
    # token-specific here.
    if exact_mid is None and failure is None:
        infra_failure = bool(pos.get("exact_quote_infra_misses"))
    route_failure = bool(
        exact_mid is None and not infra_failure and pos.get("exact_quote_misses"))

    route_degraded = None
    current = controller_state_from_row(pos)
    threshold = getattr(cfg, "MEMECOIN_ROUTE_DEGRADATION_PCT", None)
    if quote_value is not None and current.route_quote_value is not None and threshold is not None:
        try:
            prior = float(current.route_quote_value)
            drop = (prior - quote_value) / prior
            route_degraded = prior > 0 and drop >= float(threshold)
        except (TypeError, ValueError, ZeroDivisionError):
            route_degraded = None

    try:
        net = int(state.net_flow_quote) if state.net_flow_quote is not None else None
    except (TypeError, ValueError):
        net = None
    structure_break = bool(
        state.latest_event_id is not None and state.nominal_price is not None
        and state.prior_low is not None and state.nominal_price < state.prior_low
        and net is not None and net < 0)
    structure_recovered = bool(
        state.nominal_price is not None and state.prior_low is not None
        and state.nominal_price >= state.prior_low)
    obs = Observation(
        now=now_iso, event_id=state.latest_event_id, structure_break=structure_break,
        event_time=state.latest_event_time or state.source_event_time,
        event_block=state.latest_event_block_no,
        event_log_index=state.latest_event_log_index,
        structure_recovered=structure_recovered,
        net_flow_negative=(None if net is None else net < 0), exec_price=exact_mid,
        route_quote_value=quote_value if exact_mid is not None else None,
        route_quote_at=quote_at if exact_mid is not None else None,
        route_degraded=route_degraded, route_failure=route_failure,
        infra_failure=infra_failure)

    for _attempt in range(2):
        next_state = reduce(current, obs)
        if next_state.controller_revision == current.controller_revision:
            pos["_engine_quote_fresh"] = _quote_is_fresh(quote_at, now_iso)
            return current
        if m.persist_controller_state_cas(
                conn, pos["id"], current.controller_revision, **next_state.to_fields()):
            pos.update(next_state.to_fields())
            pos["controller_revision"] = next_state.controller_revision
            pos["_engine_quote_fresh"] = _quote_is_fresh(quote_at, now_iso)
            return next_state
        row = conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pos["id"],)).fetchone()
        if row is None:
            return current
        pos.update(dict(row))
        current = controller_state_from_row(pos)
    return current


def persist_hot_controller_state(conn, position_id, hot_state):
    """CAS-copy only transaction-grounded structure fields from the in-memory hot reducer.

    This is not execution authority. It preserves decoded event hysteresis before the funded sweep
    independently recomputes the action. Quote/route/peak fields remain owned by the funded sweep.
    """
    from sniper.analysis.exit_controller import ControllerState, controller_state_from_row
    if not isinstance(hot_state, ControllerState):
        return False
    try:
        hot_order = (int(hot_state.controller_last_event_block),
                     int(hot_state.controller_last_event_log_index))
    except (TypeError, ValueError):
        return False
    fields = {
        "controller_last_event_id": hot_state.controller_last_event_id,
        "controller_last_event_block": hot_state.controller_last_event_block,
        "controller_last_event_log_index": hot_state.controller_last_event_log_index,
        "structure_break_since": hot_state.structure_break_since,
        "consecutive_break_events": hot_state.consecutive_break_events,
        "negative_flow_since": hot_state.negative_flow_since,
    }
    # A funded quote observation can advance the same controller revision between our read and CAS.
    # Reload and merge rather than losing the hot lane's second same-slot confirmation. Three
    # consecutive losses return None: the caller must suppress this wake instead of recomputing from
    # an older persisted count and pretending the handoff succeeded.
    for _attempt in range(3):
        row = m.get_position(conn, position_id)
        if not row or not row.get("is_open"):
            return False
        current = controller_state_from_row(row)
        try:
            current_order = (int(current.controller_last_event_block),
                             int(current.controller_last_event_log_index))
        except (TypeError, ValueError):
            current_order = None
        if current_order is not None and hot_order < current_order:
            return False
        # Equal chain order may carry a different same-slot transaction identity. Only a strictly
        # higher corroboration count is new information; otherwise copying it just churns identity
        # and revision without increasing funded evidence.
        if (current_order == hot_order
                and hot_state.consecutive_break_events <= current.consecutive_break_events):
            return False
        if m.persist_controller_state_cas(
                conn, position_id, current.controller_revision, **fields):
            return True
    return None


def _quote_is_fresh(quoted_at, now_iso):
    age_s = _state_age_s(quoted_at, now_iso)
    return bool(age_s is not None and 0.0 <= age_s <= cfg.MEMECOIN_ENGINE_STATE_MAX_AGE_S)


def _engine_decision_for(xc, pos, policy, now_iso, *, future_context=None):
    """Return one decision plus its in-memory audit record; never write in the funded path."""
    if future_context is None:
        future_context = pos.get("_engine_future_context")
    loaded = _read_engine_state(xc, pos, now_iso)
    if loaded is None:
        logger.warning("engine exit pos=%s mint=%s: no materialized state -> envelope only",
                    pos["id"], pos.get("mint"))
        return None, None
    state, state_id = loaded
    try:
        pos["_exit_reserve_base"] = int(state.reserve_base)
    except (TypeError, ValueError):
        pos["_exit_reserve_base"] = None
    # trail_pct here is the position's coin-band v1 ANCHOR — a reference scale for propose_v5's
    # vol-vs-trail comparison only (v3 prereg §6). NOT `policy.trail_pct`, which is 0/off under
    # engine-envelope-v1 by design: the executed trail rail stays surrendered to the engine
    # regardless of this reference (band_anchor_trail_pct never reaches an executable rail here).
    from sniper.analysis.exit_controller import controller_state_from_row
    manifest = policy.decision_policy_manifest or {}
    registered = manifest.get("registered_parameters") or {}
    selected = manifest.get("selected_parameters") or {}
    anchors = registered.get("band_anchor_trail_pct") or {}
    contract = {
        "trail_pct": anchors.get(policy.coin_band, band_anchor_trail_pct(policy.coin_band)),
        "coin_band": policy.coin_band,
        "held_s": _held_s(pos.get("opened_at"), now_iso),
        "derisked_at": pos.get("derisked_at"),
        "exact_quote_misses": pos.get("exact_quote_misses"),
        # WO-5B route/no-progress feed input (all NULL/0 until the reducer is wired live ->
        # conservative). now_iso lets executable_no_progress measure elapsed from the peak time.
        "controller_state": controller_state_from_row(pos),
        "now_iso": now_iso,
        "quote_fresh": bool(pos.get("_engine_quote_fresh")),
        "partial_lp_escalation": selected.get("partial_lp_escalation"),
    }
    engine_version, proposer = exit_engine(policy.decision_engine_version)
    try:
        decision = proposer(contract, state, cfg)
    except Exception:
        logger.exception("engine exit: %s failed pos=%s mint=%s", engine_version,
                         pos["id"], pos.get("mint"))
        return None, None
    logger.warning("engine exit pos=%s mint=%s action=%s fraction=%s reasons=%s",
               pos["id"], pos.get("mint"), decision.action, decision.proposed_fraction,
               ",".join(decision.reason_codes))
    pos["_engine_decision_state_id"] = state_id
    def _current_state_id():
        from sniper.persistence import db_exit_state
        row = db_exit_state.latest_state(xc, pos["id"])
        return row.get("id") if row else None
    pos["_engine_current_state_id_fn"] = _current_state_id
    return decision, _engine_audit_record(
        pos, state, decision, now_iso, state_id=state_id, engine_version=engine_version,
        future_context=future_context)


_UNTYPED_QUOTE_FAILURE = "unquoted"   # a failure the seam couldn't type -> token-specific fail-safe


def fdv_supply_from_snapshot(st):
    """Total supply implied by one market snapshot, or None.

    fdv and price come from the same DexScreener pair, so fdv/price is the token's total supply
    whatever that pair's depth. A snapshot whose fdv is below its own circulating market cap says
    total < circulating, which cannot be true, and pins nothing."""
    # Fastlane carries this original observation beside newer swap/exact prices.
    st = (st or {}).get("fdv_snapshot", st)
    try:
        price = float((st or {}).get("price") or 0)
        fdv = float((st or {}).get("fdv") or 0)
        circ = float((st or {}).get("market_cap") or 0)
    except (TypeError, ValueError, AttributeError):
        return None
    if not (math.isfinite(price) and math.isfinite(fdv) and price > 0 and fdv > 0):
        return None
    if math.isfinite(circ) and circ > 0 and fdv < circ * 0.999:
        return None
    supply = fdv / price
    return supply if math.isfinite(supply) and supply > 0 else None


def fdv_supply_from_mint_read(read):
    """Total supply in whole tokens from a token-safety mint read `(value, failure)`, or None.

    The entry's early mint read (`ev.mint_account_read`) carries it at no extra call. The guards are
    `_onchain_source_mcap`'s. Pure."""
    try:
        val, failure = read
        if failure is not None or not isinstance(val, dict):
            return None
        info = val["data"]["parsed"]["info"]
        supply, decimals = int(info["supply"]), info["decimals"]
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 18:
            return None
        if supply <= 0:
            return None
        whole = supply / 10 ** decimals
        return whole if math.isfinite(whole) and whole > 0 else None
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def pin_fdv_supply(conn, pos, st, *, fallback_supply=None):
    """Pin the position's total supply from `st`, once, on the FDV chains only. `fallback_supply`
    (an already-validated supply) is pinned only when `st` implies none.

    Returns the pinned supply (already stored or written now) or None. Bookkeeping: a failed write
    is logged and the target stays on the provider basis; it never raises into the caller."""
    have = pos.get("fdv_supply")
    if have and have > 0:
        return have
    if str(pos.get("network_id") or "solana") not in cfg.MEMECOIN_REAL_MCAP_FDV_CHAINS:
        return None
    supply = fdv_supply_from_snapshot(st)
    if supply is None:
        supply = fallback_supply
    if supply is None:
        return None
    try:
        stored = m.pin_position_fdv_supply(conn, pos["id"], supply)
    except Exception:
        logger.exception("fdv supply pin failed pos=%s", pos.get("id"))
        return None
    if stored and stored > 0:
        pos["fdv_supply"] = stored
        return stored
    return None


_DISPLAY_SUPPLY_LOCK = threading.Lock()
_DISPLAY_SUPPLY_POOL = None             # its own worker: never queues ahead of an entry's supply read
_DISPLAY_SUPPLY_READS = {}              # (chain, mint) -> in-flight Future
_DISPLAY_SUPPLY_RETRY_AT = {}           # (chain, mint) -> monotonic time the next read may start
DISPLAY_SUPPLY_RETRY_S = 300.0


def pin_display_total_supply(conn, pos, *, now_mono=None):
    """DISPLAY ONLY (operator order "a", 2026-10-02): pin a position's on-chain total supply,
    the supply GMGN's market cap uses, so the cards read $221K where the entry ratio read $175K. No
    gate, target or band reads the column. Never waits: a cached supply pins now, a miss starts ONE
    read on a dedicated worker and a later sweep pins its result; a failed read waits
    DISPLAY_SUPPLY_RETRY_S. Returns the stored supply or None. Bookkeeping: never raises."""
    global _DISPLAY_SUPPLY_POOL
    have = pos.get("display_total_supply")
    if have and have > 0:
        return have
    chain = str(pos.get("network_id") or "solana")
    if chain == "solana" and (pos.get("fdv_supply") or 0) > 0:
        return pos["fdv_supply"]
    if chain != "solana" and chain not in (cfg.MEMECOIN_EVM_ALLOWLIST or ()):
        return None                     # an unresolved chain has no RPC
    from sniper.services import memecoin_evm_price as _ep
    key = (chain, pos["mint"])
    supply = _ep.cached_total_supply(chain, pos["mint"]) if chain != "solana" else None
    if supply is None:
        now = time.monotonic() if now_mono is None else now_mono
        with _DISPLAY_SUPPLY_LOCK:
            fut = _DISPLAY_SUPPLY_READS.get(key)
            if fut is None:
                if now < _DISPLAY_SUPPLY_RETRY_AT.get(key, 0.0):
                    return None
                try:
                    if _DISPLAY_SUPPLY_POOL is None:
                        from concurrent.futures import ThreadPoolExecutor
                        _DISPLAY_SUPPLY_POOL = ThreadPoolExecutor(
                            max_workers=1, thread_name_prefix="display-supply")
                    if chain == "solana":
                        from sniper.execution.solana_real_seams import token_supply
                        _DISPLAY_SUPPLY_READS[key] = _DISPLAY_SUPPLY_POOL.submit(
                            token_supply, pos["mint"], cfg.solana_read_rpc())
                    else:
                        _DISPLAY_SUPPLY_READS[key] = _DISPLAY_SUPPLY_POOL.submit(
                            _ep.evm_total_supply, *key)
                except Exception:
                    logger.exception("display supply read not started chain=%s mint=%s", *key)
                    _DISPLAY_SUPPLY_RETRY_AT[key] = now + DISPLAY_SUPPLY_RETRY_S
                return None
            if not fut.done():
                return None
            del _DISPLAY_SUPPLY_READS[key]
        try:
            supply = fut.result(timeout=0)[0]
        except Exception:
            logger.exception("display supply read failed chain=%s mint=%s", *key)
            supply = None
        if not (supply and supply > 0):
            _DISPLAY_SUPPLY_RETRY_AT[key] = now + DISPLAY_SUPPLY_RETRY_S
            return None
    try:
        stored = m.pin_position_display_total_supply(conn, pos["id"], supply)
    except Exception:
        logger.exception("display supply pin failed pos=%s", pos.get("id"))
        return None
    pos["display_total_supply"] = stored
    return stored


def pin_entry_fdv_supply(conn, pos, st, mint_read):
    """Post-fill total-supply pin. With X1 off, the entry's validated mint read wins over any
    carried provider snapshot: its fdv/price can retain already-burned tokens (ROUTER/Instinct,
    2026-10-05). An unavailable mint read keeps the provider fallback. X1-on keeps its legacy pin.

    Also logs both supplies whatever X1 is, MEASUREMENT ONLY: does the mint supply at entry equal
    DexScreener's, and how often is the mint read absent? It changes no decision. The line carries a
    host-clock UTC stamp because the runner's WARNING lines have none."""
    chain = str(pos.get("network_id") or "solana")
    if chain not in cfg.MEMECOIN_REAL_MCAP_FDV_CHAINS:
        return None
    dex = fdv_supply_from_snapshot(st)
    mint = fdv_supply_from_mint_read(mint_read)
    logger.warning("fdv pin sources at=%s pos=%s dex_supply=%s mint_supply=%s ratio=%s mint_read=%s",
                   datetime.now(timezone.utc).isoformat(timespec="milliseconds"), pos.get("id"),
                   "%.10g" % dex if dex else None, "%.10g" % mint if mint else None,
                   "%.6f" % (dex / mint) if dex and mint else None,
                   "absent" if mint_read is None else "ok" if mint else "unusable")
    fallback = mint if cfg.dexscreener_off("X1", chain) else None
    return pin_fdv_supply(conn, pos, None if fallback is not None else st,
                          fallback_supply=fallback)


def _mcap_at_price(pos, price):
    """(mcap, basis) at `price`: the pinned total supply ('fdv'), else the entry ratio
    ('entry_ratio', today's basis). (None, None) when neither is known."""
    if not price or not math.isfinite(price) or price <= 0:
        return None, None
    supply = pos.get("fdv_supply")
    if supply and supply > 0:
        value = price * supply
        return (value, "fdv") if math.isfinite(value) else (None, None)
    e_mcap = pos.get("entry_mcap") or 0.0
    e_px = pos.get("entry_price") or 0.0
    if e_mcap > 0 and e_px > 0:
        value = price * (e_mcap / e_px)
        return (value, "entry_ratio") if math.isfinite(value) else (None, None)
    return None, None


def _target_mcap_at_quote(pos, preview, price, *, quote_qty=None):
    """Custom caps use the quote's own human quantity when the provider supplied it."""
    missing = object()
    quantity = quote_qty if quote_qty is not None else _preview_value(preview, "input_quantity", missing)
    if quantity is not missing:
        try:
            if isinstance(quantity, bool):
                return None, None
            quantity = float(quantity)
            if not math.isfinite(quantity) or quantity <= 0:
                return None, None
            price = float(_preview_value(preview, "expected_usdc")) / quantity
        except (TypeError, ValueError, OverflowError):
            return None, None
    return _mcap_at_price(pos, price)


def _resolve_override_and_mcap(conn, pos, st, display_mid, frozen_by_id, cfg):
    """Resolve the position's effective exit override (INCLUDING the auto-template selection) and its
    live/derived market cap EXACTLY as the exit sweep does. Shared by the WO-4 miss pre-pass and the
    engine loop so `exact_relevant` (= exact_policy or pid in engine_ids) is computed IDENTICALLY in
    both — care-point #2; a hand-replicated version would diverge on the template_mcap fallback and
    advance the wrong positions. Idempotent: the template is set only when absent, so the second
    caller re-reads the same override."""
    # ORDER MATTERS, AND IT WAS WRONG (2026-09-04, operator: "custom TP never fires").
    #
    # A STORED MARK IS A SNAPSHOT; THE DERIVATION IS ALWAYS CURRENT. Market cap is price x supply,
    # supply is effectively constant for anything the safety gate lets us hold, so
    # `price x (entry_mcap / entry_price)` is exact whenever we have a live price -- and we always
    # do here, because `display_mid` is what the sweep just priced the position with. The marks
    # table, by contrast, carries mcap only when the source supplied one, and almost none do: the
    # DexScreener path writes `st.get("mcap")` while DexScreener's own key is `market_cap`, so its
    # rows store NULL. On 4663 that is 0% of marks since the GeckoTerminal removal and 12.3% before.
    #
    # Reading the newest mark BEFORE deriving therefore preferred a rare, hours-old number over an
    # exact current one -- and because it is non-zero it satisfied `if not mcap`, so the derivation
    # never ran. CRCL (position 49849, funded) is the case: operator target 670,000, peak derived
    # mcap while open 4,188,152 (crossed 6.25x), yet only 10 of its 13,257 marks carried an mcap at
    # all and the highest stored value was 185,667 -- under the target, so the take-profit could
    # never nominate. It exited `manual` because the operator closed it by hand.
    #
    # A stale mark is still better than nothing, so it stays as the LAST resort for a position with
    # no usable entry basis.
    mcap = st.get("market_cap") or st.get("mcap")
    basis = "provider_mcap" if mcap else None
    if not mcap:
        e_mcap = pos.get("entry_mcap") or 0.0
        e_px = pos.get("entry_price") or 0.0
        if display_mid and display_mid > 0 and e_mcap > 0 and e_px > 0:
            mcap, basis = display_mid * (e_mcap / e_px), "entry_ratio"
    if not mcap:
        mark = conn.execute(
            "SELECT mcap FROM memecoin_position_marks "
            "WHERE chain_id=? AND mint=? AND mcap>0 "
            "AND COALESCE(quality,'')!='quarantined_spike' "
            "ORDER BY sweep_ts DESC LIMIT 1",
            m.coin_identity(pos.get("network_id"), pos["mint"]),
        ).fetchone()
        mcap, basis = (mark[0], "mark_mcap") if mark else (None, None)
    # Today's basis. The auto-template choice keeps it: it is a band classification, not a target.
    provider_basis_mcap = mcap
    # DISPLAY CAPS READ FDV ONCE THE SUPPLY IS PINNED (2026-09-25). GMGN and similar sites show
    # price x TOTAL supply; the provider marketCap is circulating on some coins (Hogdog 64977:
    # 35.3K circulating vs 44.1K FDV), so every target set from those sites sat 20% off. After the
    # pin there is NO fallback to a circulating number: a tick without a price reads the newest
    # mark's price x the same supply, so the trigger cannot jump between bases tick to tick.
    if pos.get("fdv_supply") and pos["fdv_supply"] > 0:
        mcap, basis = _mcap_at_price(pos, display_mid)
        if not mcap:
            mark = conn.execute(
                "SELECT mid FROM memecoin_position_marks "
                "WHERE chain_id=? AND mint=? AND mid>0 "
                "AND COALESCE(quality,'')!='quarantined_spike' "
                "ORDER BY sweep_ts DESC LIMIT 1",
                m.coin_identity(pos.get("network_id"), pos["mint"]),
            ).fetchone()
            mcap, basis = (mark[0] * pos["fdv_supply"], "fdv_mark") if mark else (None, None)
    pos["_mcap_basis"] = basis
    override = m.effective_exit_override(conn, pos["id"])
    template_mcap = pos.get("entry_mcap") or provider_basis_mcap
    if (frozen_by_id[pos["id"]] is None
            and cfg.MEMECOIN_AUTO_TEMPLATES == "on" and override is None
            and template_mcap and template_mcap > 0):
        from sniper.services.memecoin_policy import choose_entry_template
        tmpl = choose_entry_template(template_mcap)
        if tmpl:
            m.set_position_exit_override(conn, pos["id"], tmpl[0], actor="auto-template", **tmpl[1])
            override = m.effective_exit_override(conn, pos["id"])
    pos["_exit_custom_cap"] = any((override or {}).get(key) is not None
                                 for key in ("tp_target_mcap", "sl_target_mcap"))
    return override, mcap


def _wo4_miss_prepass(positions, *, conn, contexts, price_map, frozen_by_id, engine_ids, cfg,
                      preview_by_position, demand_fn=None, now_iso=None):
    """WO-4: decide exact_quote_misses for the WHOLE sweep BEFORE any engine reads them, so a
    provider outage (every exact quote fails together while charts still price the coins) cannot
    manufacture a false unsellable-CUT. This owns the miss-counter writes; the engine loop must not
    touch them. Populates `preview_by_position` (this sweep only) so the engine loop reuses the exact
    quotes instead of re-quoting. Only exact-relevant, chart-priceable (mid>0) positions enter the
    counter — a dead chart stays out, exactly as before. Returns the populated preview map."""
    from sniper.analysis.memecoin_quote_failures import (
        ADVANCE, OPS, RESET, QuoteFailure, is_infra, sweep_miss_verdicts,
    )
    from sniper.execution.solana_quote import quote_for

    preview_by_position = dict(preview_by_position or {})
    outcomes, previews, boundaries, candidates = [], {}, {}, []
    # Provider groups that answered 429 this sweep, and groups whose exact quote succeeded. None in
    # `rate_limited_groups` = a 429 from a quote that named no provider -> the global hold.
    rate_limited_groups, succeeded_groups = set(), set()
    for pos in positions:
        ctx = context_for_position(contexts, pos)
        if ctx is None:
            continue
        st = quote_for(price_map, pos["mint"], pos.get("network_id")) or {}
        display_mid = st.get("price")
        override, _mcap = _resolve_override_and_mcap(conn, pos, st, display_mid, frozen_by_id, cfg)
        trail_pct, trail_armed = effective_trail(pos, override)
        derisk_pct = _effective_derisk_pct(pos, override)
        # A copy-arm stall rule is exact-price-relevant for the SAME reason a trail is: it decides
        # off the executable peak. It MUST be listed here, not only in the main loop — a position
        # the prepass considers irrelevant is skipped by the DEMAND GATE below and then quoted
        # unconditionally in the main loop, which is precisely the provider-hammering pattern the
        # demand gate was added to stop (ops streak 165, 2026-07-27). peak_lock_e zeroes trail and
        # derisk, so without this it took that path on every 1s tick.
        exact_relevant = (
            exact_price_relevant(pos, trail_pct=trail_pct, derisk_pct=derisk_pct,
                                 stall_rule=_stall_rule(pos), override=override)
            or pos["id"] in engine_ids
        )
        if not exact_relevant:
            continue
        # DEMAND GATE (2026-07-27). `exact_relevant` is a STATIC policy property — "could this
        # position ever need an exact price" — not "does it need one now". Quoting on it every tick
        # is what took the exact-quote provider down at a 1s cadence (ops streak 165). When a
        # demand_fn is supplied, skip positions the cheap mid proves are far from every rail.
        # A skipped position is NOT appended to candidates, so it never reaches `outcomes` and can
        # never be mistaken for a quote failure or an unsellable token.
        if demand_fn is not None and pos["id"] not in preview_by_position:
            try:
                if not demand_fn(pos, display_mid=display_mid, trail_pct=trail_pct,
                                 derisk_pct=derisk_pct, trail_armed=trail_armed):
                    # Record the skip EXPLICITLY. The engine loop below refetches whenever a
                    # position is absent from this map (`preview_cached`), so merely `continue`-ing
                    # moved the quote from here to there and cut nothing — the gate looked live
                    # while Helius still 429'd (2026-07-27, 5/12 identity probes failing).
                    # A None entry means "resolved: no exact price this tick"; rails evaluate on the
                    # mid, and the confirmation path still fetches fresh when one actually fires.
                    preview_by_position.setdefault(pos["id"], None)
                    continue
            except Exception:
                logger.exception("wo4 demand gate failed pos=%s — quoting (fail open)", pos["id"])
        candidates.append((pos, ctx, display_mid))

    # Independent network quotes run concurrently. The old serial pre-pass made an urgent first
    # position wait for every later provider timeout before its CUT could reach the funded loop.
    missing = [(pos, ctx) for pos, ctx, _mid in candidates
               if pos["id"] not in preview_by_position]
    if missing:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        asked = set()

        def fetch(item):
            pos, ctx = item
            fn = ctx.get("preview_exit_fn")
            if fn is None:
                return pos, None
            asked.add(pos["id"])
            return pos, fn(pos)

        with ThreadPoolExecutor(max_workers=min(len(missing), 8),
                                thread_name_prefix="exit-preview") as pool:
            future_to_pos = {pool.submit(fetch, item): item[0] for item in missing}
            for future in as_completed(future_to_pos):
                pos = future_to_pos[future]
                try:
                    _pos, preview = future.result()
                except Exception:
                    logger.exception("wo4 pre-pass preview failed pos=%s mint=%s",
                                     pos["id"], pos["mint"])
                    preview = None
                if preview is None and pos["id"] in asked:
                    # The sweep records only previews it can see; an asked-for nothing is a miss here.
                    _keep_route_quote(pos, None, "exit_decision", attempted=True)
                previews[pos["id"]] = preview

    # PRICEABLE WITHOUT A DEXSCREENER MID (phase A, Alpha 2026-09-28). A failed exact quote counts
    # only on a position that is priceable: today that is "DexScreener has a mid". With the exit
    # switches off there is often no mid at all, which would silence the unsellable counter and the
    # quote-infra alerts on exactly those chains, so an exact price resolved within
    # _LAST_EXACT_MAX_AGE_S also counts. A position that never priced stays out, as a dead chart does.
    from sniper.settings.memecoin import dexscreener_off
    if now_iso is None:
        from sniper.util.clock import real_utc_now_iso
        now_iso = real_utc_now_iso()

    def _own_priceable(pos):
        """Per chain: only where X1 or X2 is switched off, and only on a recent exact price."""
        chain = m.normalized_coin_network(pos.get("network_id"), pos.get("mint"))
        if not (dexscreener_off("X1", chain) or dexscreener_off("X2", chain)):
            return False
        at = pos.get("last_executable_at")
        age = _held_s(at, now_iso) if at and pos.get("last_executable_price") else None
        return age is not None and 0 <= age <= _LAST_EXACT_MAX_AGE_S

    for pos, _ctx, display_mid in candidates:
        preview = (preview_by_position[pos["id"]] if pos["id"] in preview_by_position
                   else previews.get(pos["id"]))
        previews[pos["id"]] = preview
        exact_mid = _executable_unit_price(conn, pos, preview,
                                           decimals_fn=(_ctx or {}).get("decimals_fn"))
        failure = getattr(preview, "failure", None)
        # The group the quote itself named (group, else provider id). The ":legacy" fill-in below
        # is a label for outcomes, never a hold key: holding a made-up group holds nothing real.
        named_group = (getattr(preview, "provider_group", None)
                       or getattr(preview, "provider_id", None))
        if failure is QuoteFailure.RATE_LIMIT:
            rate_limited_groups.add(named_group)
        elif exact_mid and exact_mid > 0 and named_group:
            succeeded_groups.add(named_group)
        if named_group and getattr(demand_fn, "note_group", None) is not None:
            demand_fn.note_group(pos["id"], named_group)
        provider = (getattr(preview, "provider_id", None)
                    or f"{m.normalized_coin_network(pos.get('network_id'), pos.get('mint'))}:legacy")
        provider_group = getattr(preview, "provider_group", None) or provider
        if exact_mid and exact_mid > 0:
            outcomes.append((pos["id"], None, provider_group))    # exact quote SUCCEEDED -> reset
        elif (display_mid and display_mid > 0) or _own_priceable(pos):
            outcomes.append((pos["id"], failure or _UNTYPED_QUOTE_FAILURE, provider_group))
        boundaries[pos["id"]] = provider

    # The provider said SLOW DOWN, so stop asking for a while. Re-spending the same rate into a
    # closed door guarantees the next sweep is another 429, and each one advances the ops streak:
    # one throttle episode walked the QUOTE-INFRA ladder from milestone 1 to milestone 5 in ~25s
    # and paged twice (2026-08-11 05:27). Draining the shared budget routes the next sweeps through
    # the DEMAND GATE's existing skip path — no outcome row, no ops streak, no false unsellable —
    # and rails read the mid meanwhile. A real outage still pages: every attempt after the cooldown
    # fails, and RAILS-BLIND fires on exact-price staleness whatever this budget does.
    #
    # PER PROVIDER GROUP (2026-09-28). The hold is keyed on the group that answered 429: a Base RPC
    # 429 on position 66253 used to hold both budgets for every chain, and new Solana fills went
    # quote-blind for 6.5-20.2 s inside their first minute. A 429 from a quote that named no
    # provider (None) still holds everything, exactly as before.
    budget = getattr(demand_fn, "budget", None)
    rail_budget = getattr(demand_fn, "rail_budget", None)
    if budget is not None and rate_limited_groups:
        cooldown_s = float(getattr(cfg, "MEMECOIN_EXACT_QUOTE_RATE_LIMIT_COOLDOWN_S", 15.0))
        for group in sorted(rate_limited_groups, key=lambda g: (g is not None, str(g))):
            budget.penalize(cooldown_s, group=group)
            # The RAIL bucket must yield too. It was exempt, and that exemption is what kept the
            # 2026-08-14 ban alive: a rails-blind position draws the rail bucket, so the blinder it
            # got the harder it asked — 50 requests/min against a provider already refusing, which
            # held the ban open so the quote could never land, so it stayed blind. Self-sustaining
            # by construction. It takes the BASE cooldown and never escalates, so a live
            # take-profit is slowed, not starved.
            if rail_budget is not None:
                rail_budget.penalize(cooldown_s, escalate=False, group=group)
            log_sampled(logger.warning, ("exact_quote_rate_limited", group),
                        "exact-quote provider rate-limited — holding %s quote budgets %.0fs "
                        "(refresh=%s rail=%s)",
                        "ALL (provider unnamed)" if group is None else "group %s" % group,
                        cooldown_s, budget.snapshot(),
                        rail_budget.snapshot() if rail_budget is not None else None)
    if budget is not None:
        # A quote came back. The escalation counts CONSECUTIVE 429s, so end that group's episode
        # here rather than let it decay on a timer against a provider that is already answering.
        # The global episode ends on any answer, unless an unnamed 429 came in this same sweep.
        for group in succeeded_groups - rate_limited_groups:
            budget.relieve(group=group)
            if rail_budget is not None:
                rail_budget.relieve(group=group)
        if None not in rate_limited_groups and any(f is None for _pid, f, _grp in outcomes):
            budget.relieve()
            if rail_budget is not None:
                rail_budget.relieve()

    verdicts = sweep_miss_verdicts(outcomes)
    unsellable_n = int(getattr(cfg, "MEMECOIN_REAL_UNSELLABLE_SWEEPS", 0) or 0)
    pos_by_id = {p["id"]: p for p in positions}
    outcome_by_id = {pid: (failure, provider_group) for pid, failure, provider_group in outcomes}
    infra_alert_changes = []
    for pid, verdict in verdicts.items():
        pos = pos_by_id.get(pid)
        if pos is None:
            continue
        failure, provider = outcome_by_id.get(pid, (None, "unknown"))
        boundary = boundaries.get(pid, provider)
        prior_infra = int(pos.get("exact_quote_infra_misses") or 0)
        if verdict == RESET:
            if prior_infra:
                m.set_position_exact_quote_infra_misses(conn, pid, 0)
                pos["exact_quote_infra_misses"] = 0
                infra_alert_changes.append({
                    "position_id": pid, "provider": provider, "before": prior_infra,
                    "after": 0, "failure": None,
                })
            if pos.get("exact_quote_misses"):
                m.set_position_exact_quote_misses(conn, pid, 0)
                pos["exact_quote_misses"] = 0
        elif verdict == ADVANCE:
            if prior_infra:
                m.set_position_exact_quote_infra_misses(conn, pid, 0)
                pos["exact_quote_infra_misses"] = 0
                infra_alert_changes.append({
                    "position_id": pid, "provider": provider, "before": prior_infra,
                    "after": 0, "failure": None,
                })
            misses = (pos.get("exact_quote_misses") or 0) + 1
            m.set_position_exact_quote_misses(conn, pid, misses)
            pos["exact_quote_misses"] = misses
            if unsellable_n > 0 and misses >= unsellable_n:
                logger.warning("position %s mint=%s UNSELLABLE: exact quote failed %s consecutive "
                               "sweeps while the chart still prices it", pid, pos.get("mint"), misses)
        elif verdict == OPS:                       # infra / sweep-wide outage -> ops streak, no miss
            streak = prior_infra + 1
            m.set_position_exact_quote_infra_misses(conn, pid, streak)
            pos["exact_quote_infra_misses"] = streak
            log_sampled(logger.warning, ("exact_quote_infra", pid),
                        "position %s mint=%s exact-quote INFRA failure (ops streak %s, failure=%s "
                        "provider=%s) — provider outage suspected; NOT advancing unsellable misses",
                        pid, pos.get("mint"), streak, getattr(failure, "value", failure), provider)
            alert_failure = (getattr(failure, "value", failure)
                             if is_infra(failure) else "provider_wide_failure")
            infra_alert_changes.append({
                "position_id": pid, "provider": provider, "before": prior_infra,
                "after": streak, "failure": alert_failure,
                "boundary": boundary if boundary != provider else None,
            })
    for event in alerts.quote_infra_episode_events(infra_alert_changes):
        _safe_alert(alerts.alert_quote_infra_episode, event)
    preview_by_position.update(previews)
    return preview_by_position


def _fill_age_s(pos, now_iso):
    """Seconds since the fill was RECORDED, not since the decision started.

    `opened_at` is the decision start -- position 50664's `opened_at` equals its
    `classification.decision_at` exactly, and across the 8 most recent funded 4663 rows it runs a
    median 3.48s (max 7.52s) ahead of `opened_recorded_at`. Measuring a fresh-fill window from it
    spends a fifth of a 20s window before the coin is even held. Prefer the recorded stamp; fall
    back to `opened_at` when it is absent so an older row still gets a window.
    """
    return _held_s(pos.get("opened_recorded_at") or pos.get("opened_at"), now_iso)


def _launchpad_curve_price_fallback(positions, price_map, now_iso):
    """Fill a `price_map` gap for a 4663 curve-phase position with a direct on-chain read
    (`source="launchpad_curve"`, `quality="live"`) -- DexScreener has no row for a coin that has
    not graduated yet, and this sweep otherwise leaves it unpriced. Only fills an ABSENT key; an
    existing DexScreener (or any other) entry is never touched or overridden. Default OFF
    (`MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED`); a read failure for one mint leaves that mint
    unpriced, exactly as an absent DexScreener row already does -- it never raises into the sweep.
    """
    if not bool(getattr(cfg, "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", False)):
        return price_map
    # With X1 off on 4663 every 4663 position is absent from the map, so this would make one serial
    # funded-RPC read per position per sweep (Beta R3). Exact-policy decisions never read this
    # price, and the display falls to the exact preview (which prices curve coins itself).
    if cfg.dexscreener_off("X1", "4663"):
        return price_map
    candidates = [p for p in positions if str(p.get("network_id")) == "4663"
                 and m.coin_identity(p.get("network_id"), p["mint"]) not in price_map]
    if not candidates:
        return price_map
    from sniper.execution.evm_launchpad_curve import mark_price_entry
    rpc_url = cfg.evm_rpc_for_chain("4663")
    if not rpc_url:
        return price_map
    for pos in candidates:
        try:
            entry = mark_price_entry("4663", pos["mint"], rpc_url, observed_at=now_iso)
        except Exception:
            logger.exception("launchpad curve mark fallback failed mint=%s", pos.get("mint"))
            continue
        if entry is not None:
            price_map[m.coin_identity(pos.get("network_id"), pos["mint"])] = entry
    return price_map


def _record_exit_decision(conn, pos, *, reason, lane, price, basis, mark, preview, preview_src,
                          now_iso):
    """The mark a funded exit decision read (Gamma 2b, spec 2026-10-08 paper exit ticks): basis
    exact_preview | last_executable | display | None (blind), with its source and age. Built AFTER
    its executor ran and only QUEUED here (Gamma LOW-1): the paper-ticks thread writes it on its own
    connection, so this thread makes no extra ledger write. Measurement only: never raises, never
    read by a decision. An absent mark records None, never a guessed source."""
    try:
        age_s = source = kind = None
        if basis == "exact_preview":
            source, kind = "exact_preview:%s" % (preview_src or "fresh"), "bag_exact"
            at = _preview_value(preview, "quoted_at")
            age_s = _held_s(at, now_iso) if at else None
        elif basis == "last_executable":
            source, kind = "last_executable", "bag_exact"
            at = pos.get("last_executable_at")
            age_s = _held_s(at, now_iso) if at else None
        elif basis == "display":
            ev = _display_evidence(mark, pos.get("entry_price"), price, now_iso)
            source, kind, age_s = ev.get("display_source"), "display", ev.get("display_age_s")
        from sniper.services.memecoin_paper_ticks import queue_exit_decision
        queue_exit_decision(pos["id"], json.dumps({
            "engine": "funded", "lane": lane, "reason": reason, "price": price, "basis": basis,
            "source": source, "tick_kind": kind,
            "age_ms": None if age_s is None else round(float(age_s) * 1000.0, 1)},
            sort_keys=True))
    except Exception:
        logger.exception("exit decision record failed pos=%s", pos.get("id"))


def _display_evidence(mark, entry_price, display_price, now_iso):
    """Basis, source, ages and PnL of the price an exit audit row displays. Display only; never
    raises. A DexScreener mid (`fastlane_mid`) carries `observed_at` (its value last CHANGED) and
    `fetched_at` (the provider was last CHECKED); an exact route mark, or no mark at all (the row
    then shows the exact price), is basis "route"."""
    try:
        from sniper.services.memecoin_fastlane import SOURCE as _EXACT_SOURCE
        mark = mark if isinstance(mark, dict) else None
        if mark is None:
            return {}
        source = mark.get("source") if mark.get("price") else None
        out = {"display_basis": "market" if source and source != _EXACT_SOURCE else "route",
               "display_source": source,
               "display_changed_at": mark.get("observed_at") if source else None,
               "display_checked_at": mark.get("fetched_at") if source else None,
               "display_age_s": None, "display_pnl_pct": None}
        try:
            if display_price and entry_price:
                out["display_pnl_pct"] = (float(display_price) / float(entry_price) - 1.0) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        try:
            if out["display_changed_at"]:
                out["display_age_s"] = round((
                    datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                    - datetime.fromisoformat(str(out["display_changed_at"]).replace("Z", "+00:00"))
                ).total_seconds(), 1)
        except (TypeError, ValueError):
            pass
        return out
    except Exception:  # noqa: BLE001 - a label must never touch the exit
        logger.debug("exit display evidence failed", exc_info=True)
        return {}


def _sweep_price_map(positions, *, price_fn, swap_mark_cache, now_ts, now_iso=None):
    """The exit sweep's price map when no caller handed it one.

    Today (X1 on): one funded DexScreener batch for the book. funded=True: this is the money path
    (catastrophe stop + timecap); a failure trips BOTH breaker cells, and a research throttle can
    never blind this sweep.

    X1 OFF (DexScreener out, phase A), PER CHAIN: the switched-off chains' keys are not asked for. Their
    own marks: on Solana the swap-mark cache's decision map, priced with our own quote-asset USD (fail
    closed per asset: memecoin_live_marks.fresh_quote_usd), for the held positions only; an EVM chain has none here, so the sweep
    displays and decides on the exact preview, exactly as it does today when DexScreener has no row.
    Rail decisions of exact-policy positions never read this map.
    """
    if price_fn is not None:
        return price_fn(list({p["mint"] for p in positions})) or {}
    keys = list({m.coin_identity(p.get("network_id"), p["mint"]) for p in positions})
    ds_keys = [k for k in keys if not cfg.dexscreener_off("X1", k[0])]
    out = {}
    if ds_keys:
        from sniper.execution import solana_quote
        out.update(solana_quote.dexscreener_stats_batch_keyed(
            ds_keys, funded=True, consumer="X1") or {})
    own_keys = set(keys) - set(ds_keys)
    if not any(k[0] == "solana" for k in own_keys) or swap_mark_cache is None:
        return out                       # an EVM chain switched off: the exact preview decides
    from sniper.services.memecoin_live_marks import fresh_quote_usd
    try:
        quote_usd = fresh_quote_usd(swap_mark_cache.quote_mints(), now_iso=now_iso)
        marks = swap_mark_cache.fresh_price_map(quote_usd, now_ts=now_ts, max_age_s=5.0) or {}
    except Exception:
        logger.exception("exit sweep: own marks unavailable; the exact preview decides")
        return out
    out.update({key: mark for key, mark in marks.items() if key in own_keys})
    return out


_PEAK_CONFIRM_S = 10.0
_PEAK_TICKS_MAX = 4096
_PEAK_TICKS = collections.OrderedDict()      # position id -> {source: (mid, monotonic)}
_PEAK_TICKS_LOCK = threading.Lock()


def _confirmed_nominal_peak(position_id, source, mid, *, now_mono=None):
    """The display peak a tick may set: min(this tick, the best OTHER fresh tick), where other is
    another source's tick within _PEAK_CONFIRM_S, or, only when no other source is fresh, this
    source's previous one. None when no
    other fresh tick exists (the first tick, or one source quiet for 10 s). A single bad tick (ANIMA
    73930: one fastlane_sol_state tick at 1.884e-04 while every swap read ~1.2e-04, +70% vs +13%;
    73892: one swap print at 4.5x) cannot move the peak; a move a second tick confirms still does.
    _quarantine_spikes (10x) does not catch these. Memory only, bounded; the funded trail never
    reads the nominal peak (executable_peak_price is separate)."""
    now = time.monotonic() if now_mono is None else now_mono
    with _PEAK_TICKS_LOCK:
        ticks = _PEAK_TICKS.pop(position_id, None) or {}
        fresh = {s: t[0] for s, t in ticks.items() if now - t[1] <= _PEAK_CONFIRM_S}
        # OTHER SOURCES FIRST (Gamma BLOCK on 318f4ee4d): a source never confirms itself while another
        # source is fresh (ANIMA's state source printed three bad ticks in a row). Only when no other
        # source is fresh does this source's own previous tick count.
        others = [v for s, v in fresh.items() if s != source] or ([fresh[source]] if source in fresh else [])
        ticks[source] = (float(mid), now)
        _PEAK_TICKS[position_id] = ticks
        while len(_PEAK_TICKS) > _PEAK_TICKS_MAX:
            _PEAK_TICKS.popitem(last=False)
    return min(float(mid), max(others)) if others else None


# EXIT-SWEEP WALL TIME (2026-10-21, Gamma/Alpha): the "before" half of the LP-exit latency gate.
# Observation only, no knob, nothing reads it to decide. A bounded window per lane (a one-position
# hot-lane sweep and a whole-book periodic sweep are different quantities and are never pooled).
_SWEEP_TIMING_WINDOW = 512
_SWEEP_TIMING = {}            # lane -> {"ms": deque, "count": int}
_SWEEP_TIMING_LOCK = threading.Lock()


def _timed_sweep(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        started = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            lane = str(kwargs.get("lane") or "periodic_sweep")
            with _SWEEP_TIMING_LOCK:
                slot = _SWEEP_TIMING.setdefault(
                    lane, {"ms": collections.deque(maxlen=_SWEEP_TIMING_WINDOW), "count": 0})
                slot["ms"].append(elapsed_ms)
                slot["count"] += 1
    return wrapper


def exit_sweep_timing():
    """{lane: {n, count, p50_ms, p90_ms, max_ms}} over the last _SWEEP_TIMING_WINDOW sweeps."""
    with _SWEEP_TIMING_LOCK:
        snap = {lane: (list(slot["ms"]), slot["count"]) for lane, slot in _SWEEP_TIMING.items()}
    out = {}                       # sort outside the lock: a sweep thread never waits on it
    for lane, (ms, count) in snap.items():
        ms.sort()
        if not ms:
            continue
        pick = lambda q: round(ms[min(len(ms) - 1, int(q * len(ms)))], 3)
        out[lane] = {"n": len(ms), "count": count, "p50_ms": pick(0.5), "p90_ms": pick(0.9),
                     "max_ms": round(ms[-1], 3)}
    return out


def reset_exit_sweep_timing():
    with _SWEEP_TIMING_LOCK:
        _SWEEP_TIMING.clear()


@_timed_sweep
@_exit_rpc_labelled
def real_exit_sweep(conn, contexts, *, price_fn=None, now_fn=None, position_ids=None,
                    preview_by_position=None, demand_fn=None, lane="periodic_sweep",
                    reserve_witness_cache=None, network_ids=None, derisk_settler=None):
    """Catastrophe-stop / timecap sweep for real alpha_b positions — decide with the paper rails,
    execute on-chain via EACH position's OWN venue exit_fn (a Solana position never reaches the EVM
    executor and vice versa). Runs regardless of the kill switch (exits are never gated)."""
    from sniper.execution.solana_quote import quote_for
    from sniper.util.clock import real_utc_now_iso
    now_fn = now_fn or real_utc_now_iso
    now_iso = now_fn()
    positions = m.open_positions_by_strategy(conn, cfg.MEMECOIN_REAL_POLICY, mode="real")
    if network_ids is not None:
        positions = [pos for pos in positions
                     if m.normalized_coin_network(pos.get("network_id"), pos.get("mint")) in network_ids]
    if position_ids is not None:
        wanted = {int(pid) for pid in position_ids}
        positions = [pos for pos in positions if int(pos["id"]) in wanted]
    from sniper.services.memecoin_funded_policy import (
        CONTRACT_UNPARSEABLE, MANUAL_UNMANAGED_POLICY_ID, frozen_policy_or_unparseable,
        is_exit_trial_v4 as _is_exit_trial_v4)
    frozen_by_id = {
        pos["id"]: frozen_policy_or_unparseable(pos) for pos in positions
    }
    invalid = [
        pos for pos in positions
        if frozen_by_id[pos["id"]] is CONTRACT_UNPARSEABLE
    ]
    if invalid:
        _report_unparseable_contracts(conn, invalid, now_iso, lane)
        invalid_ids = {pos["id"] for pos in invalid}
        positions = [pos for pos in positions if pos["id"] not in invalid_ids]
    recovery_scope = {"network_ids": network_ids} if network_ids is not None else {}
    recover_unverified_derisks(conn, contexts, now_iso, **recovery_scope)
    recover_unverified_closes(conn, contexts, now_iso, **recovery_scope)
    recover_orphaned_closes(conn, now_iso, **recovery_scope)
    # Recovery and the caller's paper-bracket pass may have written on this long-lived handle.
    # Never carry those writes into provider HTTP/RPC waits: they pin the main WAL until the
    # entire exit sweep returns.
    conn.commit()
    if not positions:
        return []
    price_map = _sweep_price_map(positions, price_fn=price_fn,
                                 swap_mark_cache=reserve_witness_cache, now_ts=time.monotonic(),
                                 now_iso=now_iso)
    # Reject implausible up-spike ticks BEFORE any downstream read — the same guard the paper sweep
    # runs (memecoin_exits.sweep_exits). Critical now that this sweep ratchets peak_price: a bad tick
    # (Jotchua $0.0034->$17.099, ~5000x) would otherwise permanently corrupt an open real position's
    # displayed MFE. Quarantined mints fall through to the direct preview / stay unquoted this
    # sweep, exactly as an absent price does. (The exit FIRE path is separately re-confirmed on-chain.)
    from sniper.services.memecoin_exits import _quarantine_spikes, write_marks
    price_map = _launchpad_curve_price_fallback(positions, price_map, now_iso)
    price_map = _quarantine_spikes(conn, price_map, now_iso)
    engine_ids = _engine_authoritative_ids(
        positions, frozen_by_id, limit=cfg.MEMECOIN_ENGINE_MAX_POSITIONS)
    xc = None
    if engine_ids or cfg.MEMECOIN_EXIT_DEPTH_AWARE_SLIPPAGE_ENABLED:
        try:
            xc = _connect_exit_state()
        except Exception:
            logger.exception(
                "engine exit: exit-state DB unavailable (positions fall back to envelope-only)")
    # Pin each FDV-chain position's total supply from this sweep's snapshot (once per position),
    # BEFORE the commit below, so the write never rides into the remote quotes that follow.
    for pos in positions:
        pin_fdv_supply(conn, pos, quote_for(price_map, pos["mint"], pos.get("network_id")))
        pin_display_total_supply(conn, pos)
        pos["_mcap_exit_target"] = m.active_exit_target(conn, pos["id"])
    # Spike quarantine can persist evidence. Exact route previews below are remote calls and must
    # not inherit that transaction.
    conn.commit()
    # WO-4: decide the unsellable-miss counter for the WHOLE sweep up front — before any engine reads
    # exact_quote_misses — so a provider outage (every exact quote fails together) cannot manufacture
    # a false unsellable-CUT. Also caches the exact quotes for reuse in the loop below.
    preview_by_position = _wo4_miss_prepass(
        positions, conn=conn, contexts=contexts, price_map=price_map, frozen_by_id=frozen_by_id,
        engine_ids=engine_ids, cfg=cfg, preview_by_position=preview_by_position,
        demand_fn=demand_fn, now_iso=now_iso)
    # ARMED VOLUME-DEATH per-sweep health memo (Beta review, 2026-08-26): stream health is a
    # global fact per CHAIN; computing it per position repeated a ~122ms read up to cap times
    # inside the exit loop. Once per sweep per chain, and only when the rail is armed.
    _vd_health = {}

    def _vd_stream_ok(chain, _now):
        if chain not in _vd_health:
            try:
                from sniper.services.memecoin_volume_death import stream_healthy as _vd_sh
                _vd_health[chain] = _vd_sh(conn, now_iso=_now, chain_id=chain)
            except Exception:
                _vd_health[chain] = False        # unreadable health = blind = frozen, never fired
        return _vd_health[chain]
    results = []
    engine_audits = []
    for pos in positions:
        # A CLOSE ALREADY IN FLIGHT NEEDS NOTHING FROM THIS SWEEP.
        #
        # `open_positions_by_strategy` selects on is_open alone, and nothing below ever looked at
        # the close state, so a position mid-exit was swept exactly like an idle one: it bought an
        # exact route preview (a REMOTE call), re-evaluated its rails, re-fired its TP, and the
        # executor then bailed at `claim_position_close` in ~0.12ms with `already_closing`.
        #
        # The executor's 0.12ms made that look free. It is not — the executor is the LAST step, and
        # `memecoin_exit_execution_attempts.total_ms` only measures the executor. The preview in
        # front of it is the cost, and it is invisible in that table.
        #
        # Measured on 4663 real_tp: 267 `already_closing` rows over just THREE positions. Position
        # 46888 held a close for 16m58s and was re-fired 139 times, each attempt preceded by its own
        # exact quote. All three eventually closed with a signature at a profit, so nothing was lost
        # — the waste is a remote quote per tick, contending with the very exit trying to finish.
        # Since MEMECOIN_EXACT_QUOTE_ALWAYS went live (2026-09-02 ~10:36Z) the sweep quotes every
        # open position every 1s tick, so that same 17-minute close would now burn about 1000.
        #
        # The predicate is `claim_position_close`'s own, exactly: it refuses while any of these four
        # is set, so a sweep that proceeds past them can only ever produce `already_closing`. Same
        # columns, one definition, and no extra read — `positions` is already SELECT *.
        #
        # A stale claim is NOT stranded by this: recovery runs from `recover_stale_reservations` and
        # the submission-reset paths, never from this sweep, and the executor was returning
        # `already_closing` for those ticks anyway. What is given up is the mark write for the few
        # seconds a close is in flight; a position being sold does not need a rail evaluation, and
        # the post-close tape is separately known to be selection-biased.
        if (pos.get("close_claim_id") or pos.get("close_sig")
                or pos.get("derisk_claim_id") or pos.get("derisk_sig")):
            # An unknown partial alone (no close in flight) held every rail until the 120 s
            # recovery; the settler reads it off this thread (RailDeriskSettler).
            if derisk_settler is not None and not (pos.get("close_claim_id") or pos.get("close_sig")):
                derisk_settler.offer(pos)
            continue
        ctx = context_for_position(contexts, pos)
        if ctx is None:
            logger.warning("real exit sweep: no context for network=%s pos=%s (skipped)",
                           pos.get("network_id"), pos["id"])
            continue
        # LP EMERGENCY, read first (Codex review): a lock and a dict read, no I/O. Applied below
        # OVER whatever price rail nominates, so no stop/TP deferral can hold it.
        _lp_reason = (_lp_catastrophic_reason(conn, pos, reserve_witness_cache, now_iso, lane)
                      if reserve_witness_cache is not None else None)
        if cfg.MEMECOIN_EXIT_DEPTH_AWARE_SLIPPAGE_ENABLED and xc is not None:
            loaded_depth = _read_engine_state(xc, pos, now_iso)
            if loaded_depth is not None:
                try:
                    pos["_exit_reserve_base"] = int(loaded_depth[0].reserve_base)
                except (TypeError, ValueError):
                    pos["_exit_reserve_base"] = None
        st = quote_for(price_map, pos["mint"], pos.get("network_id")) or {}
        display_mid = st.get("price")
        mid = display_mid
        # An operator mcap band precedes the %-rails, confirmed on the exact sell below.
        # mcap (live -> mark-tape -> entry-ratio) + effective override incl. auto-template selection.
        # Shared with the WO-4 miss pre-pass so exact_relevant is computed identically (care-point #2).
        override, mcap = _resolve_override_and_mcap(conn, pos, st, display_mid, frozen_by_id, cfg)
        # Any active funded trail is evaluated on the exact full-balance sell quote every sweep.
        # The same exact mid also nominates the rollout cohort's de-risk rail, preventing a nominal
        # display pump from selling principal below the configured executable threshold.
        trail_pct, _trail_armed = effective_trail(pos, override)
        derisk_pct = _effective_derisk_pct(pos, override)
        # A peak-stall copy arm is peak-based exactly like the trail, so it needs the same exact
        # quote: peak_lock_e sets trail_pct and derisk_pct to 0, which would otherwise leave
        # exact_policy False, `executable_peak_price` never advanced, and the stall counter
        # measuring a peak the position cannot sell into. (momentum_a already qualifies via its
        # trail, so this only actually adds peak_lock_e.)
        stall_rule = _stall_rule(pos)
        exact_policy = exact_price_relevant(
            pos, trail_pct=trail_pct, derisk_pct=derisk_pct, stall_rule=stall_rule,
            override=override)
        # engine-envelope-v1 zeroes trail_pct/derisk_pct (so exact_policy reads False), but its
        # positions still need real exact-quote attempts: propose_v5's CUT-unsellable branch reads
        # exact_quote_misses, which without this stayed 0 forever under envelope (material defect
        # (a), Codex 2026-07-24 activation-blocker review — the branch was structurally unreachable).
        exact_relevant = exact_policy or pos["id"] in engine_ids
        preview_cached = preview_by_position is not None and pos["id"] in preview_by_position
        preview = (preview_by_position or {}).get(pos["id"])
        if preview_cached:
            # UNATTRIBUTABLE, NOT FREE. The batched pre-pass quotes every open position
            # concurrently, so there is no honest per-position cost to charge -- but reporting
            # `preview_ms=0` would make this exit look CHEAPER than one that bought its own, and the
            # bias runs the same direction every time. Naming the source keeps "we did not measure
            # it" distinct from "it cost nothing", so a consumer can exclude these rows instead of
            # averaging zeros into a median. Gamma, reviewing 5111a07f.
            pos["_exit_preview_src"] = "batch"
        asked_here = False
        if not preview_cached and (exact_relevant or not display_mid or display_mid <= 0):
            pv_fn = ctx.get("preview_exit_fn")
            if pv_fn is not None:
                asked_here = True
                try:
                    preview = _timed_preview(pos, pv_fn)
                except Exception:
                    logger.exception("real exit preview failed pos=%s mint=%s", pos["id"], pos["mint"])
        exact_mid = _executable_unit_price(conn, pos, preview, decimals_fn=ctx.get("decimals_fn"))
        if exact_mid and exact_mid > 0:
            # The volume-death exact-quote witness reads this tape (structural pools only).
            from sniper.services.memecoin_volume_death import record_exact_quote
            record_exact_quote(pos["id"], quoted_at=_preview_value(preview, "quoted_at") or now_iso,
                               price=exact_mid)
        if pos.get("signal_source") == "desk":
            # Validate both tightening sources against this sweep's quote, before its buffered
            # route-series observation can reach SQLite. A failed preview cannot use older history.
            import time as _t
            from sniper.services import memecoin_desk_attention as _att
            from sniper.services import memecoin_desk_risk_apply as _risk_apply
            from sniper.util.clock import real_utc_now_iso as _risk_now
            current_quote = {"position_id": pos["id"], "mint": pos["mint"],
                             "network_id": pos.get("network_id"), "seam": "exit_decision",
                             "observed_at": _risk_now(), "quoted_at": _preview_value(preview, "quoted_at"),
                             "expected_usdc": _preview_value(preview, "expected_usdc"),
                             "open_qty": _preview_value(preview, "input_quantity"), "unit_price": exact_mid} if exact_mid else {}
            pos["_desk_risk_exit"] = _risk_apply.apply_orders(
                conn, pos, now_epoch=_t.time(), now_iso=now_iso, current_quote=current_quote)
            override = m.effective_exit_override(conn, pos["id"])
            _att.on_exact_price(pos, exact_mid, now_epoch=_t.time())
            if _risk_apply.apply_auto(
                    conn, pos, now_epoch=_t.time(), now_iso=now_iso, price=exact_mid,
                    current_quote=current_quote) is not None:
                override = m.effective_exit_override(conn, pos["id"])
                pos["_attention_tightened"] = True
            trail_pct, _trail_armed = effective_trail(pos, override)
            derisk_pct = _effective_derisk_pct(pos, override)
            exact_policy = exact_price_relevant(
                pos, trail_pct=trail_pct, derisk_pct=derisk_pct, stall_rule=stall_rule, override=override)
        pos["_exit_bag_quote"] = _preview_value(preview, "expected_usdc")
        # A ROUTABLE BAG IS NOT A STUCK BAG (2026-09-23). `stuck_bag` had no clearing path anywhere
        # in the tree, so a ladder that exhausted once left the position flagged for the rest of its
        # life -- paging the operator, reading DEAD in the stuck-bag report, and drawing
        # `stuck_backoff`'s retry delay on a bag that could by then be sold. This is the one place
        # that holds the evidence which justifies clearing it: a FRESH whole-bag route quote taken
        # this sweep. Positive value means the whole position just priced end-to-end, so the ladder's
        # verdict is stale. Deliberately NOT cleared on a mark or a mid -- see clear_stuck_bag.
        if pos.get("stuck_bag") and (pos.get("_exit_bag_quote") or 0) > 0:
            try:
                if m.clear_stuck_bag(conn, pos["id"], now_iso=now_iso):
                    logger.warning("stuck bag CLEARED pos=%s mint=%s: whole-bag route quote $%.2f",
                                   pos["id"], pos["mint"], float(pos["_exit_bag_quote"]))
            except Exception:                  # never let bookkeeping break a sweep
                logger.exception("clear_stuck_bag failed pos=%s", pos["id"])
        _keep_route_quote(pos, preview, "exit_decision", attempted=asked_here)
        pos["_exit_bag_base"] = _preview_value(preview, "amount_atomic")
        if not display_mid or display_mid <= 0:
            mid = exact_mid
        # Rail NOMINATION uses the exact executable price when the policy needs it AND it resolved;
        # if the exact quote FAILED this sweep, fall back to the display mid so a transient
        # Jupiter/0x/GT failure can NOT blind the loss rails (b_stop/catastrophe). Actual EXECUTION of
        # any exact reason is still gated by executable_exit_allowed() against a real preview below, so
        # a display-only pump can never sell principal — the confirmation layer, not nomination,
        # enforces the exact-proceeds floor. (2026-07-18: `exact_mid if exact_policy else mid` set
        # decision_mid=None on any exact-quote failure, silently skipping EVERY rail — audit-confirmed
        # dark on the blind 4663/illiquid paths.)
        # ...AND THE NOMINAL MID IS NOT A PRICE THIS CHAIN CAN DECIDE ON. Measured 2026-09-03 over
        # 1,554 4663 marks paired within 15s since 09-01: |dexscreener - executable| is 11.1% at
        # the median, 37.8% at p90, 116.2% at p99, 575.9% at worst, and 53.0% of marks disagree by
        # more than 10%. A -30% stop cannot be nominated off a series carrying that error; the
        # fallback is not conservative, it is noise, and it points both ways.
        #
        # Position 49013 (2026-09-03) is the live case: 23 consecutive IDENTICAL dexscreener marks
        # at 1.986e-05 across 32 seconds while the curve's executable price was 3.99e-05, and the
        # b_stop debounce took a hit at a fabricated -47%. The 29763 outage cited above as the
        # reason for this fallback is the other direction, and the fallback did not save it either
        # — that comment records `stop_hits stayed 0`, because the nominal read -28.4% while the
        # bag was at -62.8%. It hid a real stop there and invented one here.
        #
        # So an exact-policy position decides on an exact price, or on the last exact price we
        # actually resolved (below), or on nothing. Deciding on nothing is the BLIND state, which
        # the block underneath already records, alerts and bounds — an honest gap, not a wrong
        # number. Nominal still drives display and mcap, which is what it is fit for.
        decision_mid = (exact_mid if (exact_mid and exact_mid > 0)
                        else (None if exact_policy else mid))
        # RAILS-BLIND (2026-08-06): the fallback above keeps the rails RUNNING, but on a mark that
        # can be arbitrarily better than what the bag would actually fetch. Position 29763 sat at
        # -62.8% executable against a $17.30 pool while the nominal mid read -28.4%; its -50% stop
        # never nominated once (stop_hits stayed 0) and it produced no audit row at all, because
        # the 4663 RPC was returning HTTP 429 and the exact preview never resolved. A 429 is
        # correctly graded infra, so `exact_quote_misses` stays 0 and the unsellable path never
        # engages either — leaving the loss rails silently reading an optimistic price.
        #
        # This does NOT force a sell: an unresolvable exact quote often means the exit cannot be
        # executed either, and dumping into that is how you realize the worst possible fill. It
        # records the blind state so it is auditable and alertable instead of invisible.
        # LAST-RESOLVED EXACT FALLBACK. A failed exact quote used to hand the rails the nominal
        # mid, which on a thin pool is roughly double what the bag fetches. Instead, keep the last
        # exact price we actually resolved and take the WORSE of it and nominal. Monotonically
        # conservative in both directions: it can only make a LOSS rail more likely and a PROFIT
        # rail less likely, never the reverse. Selling is unaffected -- executable_exit_allowed
        # still demands a FRESH preview and returns `preview_unavailable` without one, so nothing
        # is ever sold on a stale price. What this buys is that the stop ARMS and its debounce
        # counts during the outage, so it fires on the first sweep whose quote resolves instead of
        # restarting from zero. Bounded by age: a very old exact price is not evidence about now.
        # A DUST-ROUTE preview (memecoin_exit_dust_guard) is not a "last good" executable price:
        # the guard draws its line under that price, so a dust read must not overwrite it.
        from sniper.services import memecoin_exit_dust_guard as _dust
        _dust_route = _dust.sweep_dust_route(pos, exact_mid, now_iso, conn)
        if exact_mid and exact_mid > 0 and not _dust_route:
            m.set_position_last_executable(conn, pos["id"], exact_mid, now_iso,
                                           anchor=_exit_anchor_tick(pos, exact_mid))
        elif exact_policy:
            # No longer "the WORSE of stale and nominal" — nominal is gone from this decision, so
            # the last resolved exact price is the whole fallback. It is a price the bag really
            # fetched, only older, and the age bound is what keeps it evidence about now.
            #
            # THE BACKSTOP, NAMED HERE RATHER THAN IN A COMMIT MESSAGE (Gamma, Protocol C review
            # 2026-09-03). Dropping the `stale_exact < decision_mid` test means this price is now
            # adopted unconditionally, bounded only by _LAST_EXACT_MAX_AGE_S. That bound is 900s
            # and 94% of funded positions close inside it (measured 09-02), so the bound is longer
            # than the thing it bounds: under an outage a 15-minute-old price can NOMINATE a stop.
            # It cannot SELL one. `executable_exit_allowed` rechecks every percent reason against
            # a fresh exact preview and returns `preview_unavailable` without one. That recheck is
            # the entire safety argument for this line — if it is ever relaxed, this bound has to
            # come down with it. Tightening 900s is a separate change and wants its own evidence.
            stale_exact = pos.get("last_executable_price")
            stale_at = pos.get("last_executable_at")
            stale_age_s = _held_s(stale_at, now_iso) if stale_at else None
            if (stale_exact and float(stale_exact) > 0 and stale_age_s is not None
                    and stale_age_s <= _LAST_EXACT_MAX_AGE_S):
                decision_mid = float(stale_exact)
        # THE MARK THE RAILS READ (Gamma 2b, spec 2026-10-08 paper exit ticks): kept here, written
        # beside the exit only after its executor has run (_record_exit_decision); nothing reads it.
        decision_basis = ("exact_preview" if (exact_mid and exact_mid > 0)
                          else "last_executable" if (exact_policy and decision_mid)
                          else "display" if decision_mid else None)
        decision_preview_src = pos.get("_exit_preview_src")
        # BLIND means the exact price is STALE, not "not fetched this sweep". The WO-4 demand
        # gate skips mid-band positions on purpose (its None is "resolved: no exact price this
        # tick"), and a healthy exact-policy position refreshes on the demand heartbeat — counting
        # those skips as blind paged once per minute per healthy position (pos 30226, 2026-08-07:
        # 276 rails_blind audit rows with miss counters 0/0 and the external whole-bag quote equal
        # to the nominal mid). The rails are blind once NOTHING has resolved for several
        # heartbeats — which still catches the 29763 outage class, where no exact price ever
        # resolves at all.
        _blind_exact_age_s = (
            _held_s(pos.get("last_executable_at"), now_iso)
            if pos.get("last_executable_at") else None)
        rails_blind = (bool(exact_policy) and not (exact_mid and exact_mid > 0)
                       and (_blind_exact_age_s is None
                            or _blind_exact_age_s > _RAILS_BLIND_STALE_S))
        blind_streak = (int(pos.get("rails_blind_streak") or 0) + 1) if rails_blind else 0
        if blind_streak != int(pos.get("rails_blind_streak") or 0):
            m.set_position_rails_blind_streak(conn, pos["id"], blind_streak)
            pos["rails_blind_streak"] = blind_streak
        # Alert EXACTLY at the threshold: one missed quote is routine provider noise and belongs in
        # the log; a sustained blind window on a funded position is a fault — UNLESS the position
        # has no active loss rail, in which case a blind exact price misleads nothing (2026-08-14).
        if blind_streak == _RAILS_BLIND_ALERT_SWEEPS and _has_active_loss_rail(pos, override, cfg):
            _safe_alert(alerts.alert_rails_blind, pos["mint"], pos["id"], blind_streak,
                        ((decision_mid / float(pos["entry_price"]) - 1.0) * 100.0
                         if decision_mid and pos.get("entry_price") else None))
        if rails_blind:
            from sniper.services.memecoin_funded_policy import effective_funded_policy
            _eff_tp, _, _eff_stop, _ = effective_real_thresholds(pos, override)
            _frozen, _eff_policy = effective_funded_policy(pos, override=override)
            _blind_view = rails_blind_context(
                pnl_pct=((decision_mid / float(pos["entry_price"]) - 1.0) * 100.0
                         if decision_mid and pos.get("entry_price") else None),
                stop_pct=_eff_stop, tp_pct=_eff_tp, trail_pct=trail_pct,
                trail_armed=_trail_armed, derisk_pct=derisk_pct,
                catastrophe_pct=(_eff_policy or {}).get("catastrophe_pct"),
                stall_rule=stall_rule)
            m.record_exit_trigger_audit(
                conn, position_id=pos["id"], reason="rails_blind",
                display_price=display_mid or mid,
                preview_usdc=_preview_value(preview, "expected_usdc"), allowed=False,
                detail=json.dumps({
                    **_blind_view,
                    "code": ("exact_price_unresolved_proximity_unknown" if not _blind_view["price_known"]
                             else "exact_price_unresolved_near_rail" if _blind_view.get("consequential")
                             else "exact_price_unresolved_no_near_rail"),
                    "decision_basis": "nominal_mid",
                    "nominal_mid": decision_mid,
                    "last_exact_age_s": _blind_exact_age_s,
                    "exact_quote_misses": pos.get("exact_quote_misses") or 0,
                    "exact_quote_infra_misses": pos.get("exact_quote_infra_misses") or 0,
                    "override_active": bool(override),
                    # WHAT THE BOUND POOL SAYS WHILE JUPITER IS BLIND (2026-10-17): audit only.
                    "native": _native_blind_view(conn, pos, reserve_witness_cache, override, now_iso,
                                                 ctx.get("decimals_fn")),
                }, sort_keys=True, default=str),
                checked_at=now_iso,
                lane=lane,
            )
            if _blind_view.get("consequential"):
                logger.warning(
                    "real rails BLIND pos=%s mint=%s — exact price unresolved with %s only "
                    "%.1f points away; that rail cannot see true proceeds (nominal mid %s)",
                    pos["id"], pos["mint"], _blind_view.get("nearest_rail"),
                    _blind_view.get("nearest_rail_distance_pct") or 0.0, decision_mid,
                )
            elif pos["id"] not in _BLIND_IDLE_SINCE:
                # Log the visibility edge once, including unknown proximity when price is absent.
                # The exit edge carries its duration; repeated blind sweeps do not add noise.
                _now_mono = time.monotonic()
                _blind_idle_prune(_now_mono)
                _BLIND_IDLE_SINCE[pos["id"]] = _now_mono
                if not _blind_view["price_known"]:
                    logger.warning(
                        "real rails BLIND pos=%s mint=%s — rail proximity unknown "
                        "(active=%s); nominal price unavailable",
                        pos["id"], pos["mint"], ",".join(_blind_view["active_rails"]) or "none")
                else:
                    logger.warning(
                        "real rails blind-but-idle pos=%s mint=%s — no rail within reach "
                        "(active=%s, nearest=%s at %s points); nominal mid %s",
                        pos["id"], pos["mint"], ",".join(_blind_view.get("active_rails") or []) or "none",
                        _blind_view.get("nearest_rail"), _blind_view.get("nearest_rail_distance_pct"),
                        decision_mid,
                    )
        else:
            _blind_idle_clear(pos)
        # UNSELLABLE detection: the exact_quote_misses counter (a genuinely unsellable bag fails the
        # exact quote every sweep while its chart still prices it — measured: a live 4663 pool
        # returned $2.82 on a $300 chart-implied bag) is now advanced by the WO-4 sweep pre-pass
        # BEFORE this loop, so a provider outage (all quotes fail together) can't manufacture a false
        # CUT. `pos["exact_quote_misses"]` is already the outage-suppressed value the engine reads.
        # last_quote drives the unquotable-zombie guard (staleness) and the timecap's close price. It
        # tracks PRICEABILITY, not exact-route availability: a display price means the coin is priceable,
        # so the timer must still advance from it even under exact_policy (the zombie guard is only for
        # coins we can't price AT ALL). Keep it on the best available mid.
        quote_mid = decision_mid if decision_mid and decision_mid > 0 else mid
        if quote_mid and quote_mid > 0:
            m.update_position_last_quote(conn, pos["id"], quote_mid, now_iso)
        # Nominal peak remains a display/UI surface. The funded trail never reads it.
        nominal_mid = display_mid if display_mid and display_mid > 0 else exact_mid
        if nominal_mid and nominal_mid > 0:
            peak = pos.get("peak_price") or (pos.get("entry_price") or 0.0)
            # CONFIRMED BY A SECOND TICK (2026-10-17, ANIMA 73930 / 73892): one tick no longer sets the
            # peak; it advances only to what another fresh tick (another source, or this source's
            # previous tick) agrees with (_confirmed_nominal_peak).
            confirmed = _confirmed_nominal_peak(
                pos["id"], (st.get("source") if display_mid and display_mid > 0 else "exact") or "unknown",
                nominal_mid)
            if confirmed is not None and confirmed > peak:
                m.update_position_peak(conn, pos["id"], confirmed, now_iso,
                                       pos.get("no_new_high_sweeps") or 0)
                peak = confirmed
            if nominal_mid > peak and (confirmed is None or nominal_mid > confirmed):
                # A lone tick above the confirmed peak: held out of peak_price, kept for the card's
                # 'unconfirmed' mark (display only).
                m.update_position_peak_unconfirmed(conn, pos["id"], nominal_mid)
        if exact_policy and exact_mid and exact_mid > 0:
            executable_peak = pos.get("executable_peak_price") or (pos.get("entry_price") or 0.0)
            if exact_mid > executable_peak:
                # NO-NEW-HIGH CLOCK (2026-08-04): stamp WHEN the executable high was set, for the
                # copy-arm peak-stall rail. Elapsed time, not a sweep counter — the funded fast
                # lane ticks up to 15x faster than the paper sweep those arms were measured on, so
                # a frozen sweep COUNT would silently tighten the rule from ~30s to ~2s.
                #
                # Stamped whenever the executable high advances, NOT only when a stall arm wants
                # it (2026-09-08). The old `peak_at=(now_iso if stall_rule is not None else None)`
                # left the column NULL on 244 of 244 funded positions in 7 days, so any reader of
                # the clock saw "no high recorded yet" on a position that had made many. Both
                # readers gate on their own arm — the peak-stall rail on `stall is not None`, and
                # `executable_no_progress` on a registered window — so no position gains a rail
                # from the stamp. It is guarded by the strict new-high test above, so the clock
                # measures time since the last high and is not reset by an ordinary sweep.
                #
                # This does NOT make the no-progress rail reachable on its own: funded contracts
                # carry `engine_authoritative=False`, so `propose_v5` is not the authority for
                # these positions and the WO-5A reducer only runs for the N=5 engine canary.
                # `controller_revision` is 0 on all 1042 funded rows, which is that state, not a
                # fault. The stamp is what the clock needs; the engine is a separate decision.
                m.update_position_executable_peak(
                    conn, pos["id"], exact_mid, peak_at=now_iso)
                pos["executable_peak_price"] = exact_mid
                pos["executable_peak_at"] = now_iso
        if not mcap:
            # Pinned supply first (FDV basis), else today's entry ratio. See _mcap_at_price.
            mcap, _basis = _mcap_at_price(pos, exact_mid)
            if mcap:
                pos["_mcap_basis"] = _basis
        target = pos.get("_mcap_exit_target")
        # Custom targets read the fresh whole-bag sell, never a market mark or stale exact fallback.
        target_cap = None
        if (target or (override or {}).get("tp_target_mcap") is not None
                or (override or {}).get("sl_target_mcap") is not None):
            target_cap, _target_basis = _target_mcap_at_quote(pos, preview, exact_mid)
        if target and target_cap is not None and target_cap >= target["mcap_low"]:
            reason = "mcap_target"
        else:
            reason = alpha_b_real_exit_reason(
                pos, decision_mid, now_iso, override=override, current_mcap=target_cap,
            )
        if _lp_reason is not None:
            # PRECEDENCE: a validated LP catastrophe replaces any price nomination (b_stop,
            # catastrophe, TP, trail, mcap target). Those can wait on debounce, the no-loss window,
            # an executable confirmation or the TP window; lp_catastrophic is in none of those sets.
            reason = _lp_reason
        # ARMED VOLUME-DEATH (2026-08-26, operator). Below every price rail and the timecap — a
        # position that is also through its stop exits as the stop — and ABOVE the engine adapter:
        # this is an envelope rail, and the envelope wins ties. Fires only for the armed cohort,
        # only on a healthy stream (blind tape freezes it), only on the coin's own measured
        # silence. The check needs the DB for the quiet-vs-blind discriminator, which is why it
        # lives here and not in the pure reason function.
        if (reason is None and getattr(cfg, "MEMECOIN_VOLDEATH_ARMED", 0)
                and getattr(frozen_by_id[pos["id"]], "policy_id", None)
                not in (MANUAL_UNMANAGED_POLICY_ID, "source-mirror-tp100-v1")
                and not _is_exit_trial_v4(frozen_by_id[pos["id"]])):
            try:
                from sniper.services.memecoin_volume_death import (
                    armed_exit_decision, POST_THRESHOLD_REASONS, HELD_WITNESS_REASONS, mint_audit_note,
                    held_log_due)
                from sniper.services.memecoin_funded_policy import voldeath_exit_s
                # THE POSITION'S OWN THRESHOLD WINS. Frozen in the contract at reservation, so an
                # env edit cannot change the exit rule of a position that is already open. None is
                # LEGACY, not malformed — pre-lift contracts never carried the key and fall back to
                # the env value, which is what those positions were opened under.
                _vd_fire, _vd_silence, _vd_why = armed_exit_decision(
                    conn, pos, now_iso=now_iso,
                    held_s=_held_s(pos.get("opened_at"), now_iso),
                    exit_s=voldeath_exit_s(pos, getattr(cfg, "MEMECOIN_VOLDEATH_EXIT_S", 60.0)),
                    min_opened_at=getattr(cfg, "MEMECOIN_VOLDEATH_ARM_MIN_OPENED_AT", ""),
                    stream_ok=_vd_stream_ok(pos.get("network_id"), now_iso),
                    require_state_witness=getattr(
                        cfg, "MEMECOIN_VOLDEATH_STATE_WITNESS", True),
                    reserve_witness_cache=reserve_witness_cache,
                    exit_quote_evidence=_preview_value(preview, "quote_evidence"))
                # EXIT COVERAGE THAT NEVER COMES BACK pages once (2026-09-26, PAIDCATS 65854 /
                # 65860). Every verdict goes in: a non-blind one is what ends an episode. Alert
                # only; never raises, never sends while holding its lock.
                alerts.note_voldeath_coverage(pos["id"], _vd_why, mint=pos.get("mint"),
                                              handle=pos.get("source_handle"),
                                              ticker=pos.get("ticker"))
                # PERSIST WHICH EVIDENCE DECIDED, because a log line cannot be queried and the
                # operator's ruling — grade covered and uncovered holds separately, never pooled —
                # needs a column to split on. `exit_reason` is 'vol_death' for both cases, so
                # without this the split is unenforceable and my earlier "labelled" claim was true
                # only of the log. Written just for the four post-threshold decisions, so it is at
                # most one row per position per minute, and best-effort: an audit record must never
                # be able to refuse or force a funded exit.
                # THE CLOCK BELOW THE THRESHOLD, NOT ONLY THE VERDICT (2026-09-23, dec0ded
                # 64243). The audit wrote a row only once silence had already crossed exit_s, so
                # "was the rail late?" could only be answered from the on-chain tape -- which
                # retains about ONE HOUR, and the question is always asked the next morning. This
                # persists the pre-threshold silence per minute, which is exactly the series that
                # makes lateness measurable after the tape is gone.
                #
                # OBSERVATION ONLY, and on its OWN reason: `memecoin_exit_trigger_audit` upserts on
                # (position_id, reason, checked_minute), so sharing the `vol_death` reason would
                # REPLACE the real verdict whenever both land in the same minute -- the lossy-upsert
                # trap this ledger has already paid for. At most one row per open position per
                # minute, best-effort, and it can neither fire nor hold an exit.
                if _vd_why == "measured":
                    try:
                        m.record_exit_trigger_audit(
                            conn, position_id=pos["id"], reason="vol_death_watch",
                            display_price=decision_mid, preview_usdc=None, allowed=False,
                            detail="%s silence=%.1fs" % (_vd_why, _vd_silence or -1.0),
                            checked_at=now_iso, lane=lane)
                    except Exception:
                        logger.exception("could not record the vol_death clock for %s",
                                         pos.get("id"))
                if _vd_why in POST_THRESHOLD_REASONS:
                    try:
                        m.record_exit_trigger_audit(
                            conn, position_id=pos["id"], reason="vol_death",
                            display_price=decision_mid, preview_usdc=None,
                            allowed=bool(_vd_fire),
                            detail="%s silence=%.1fs%s" % (_vd_why, _vd_silence or -1.0, mint_audit_note(
                                reserve_witness_cache, pos["id"], _vd_why)),
                            checked_at=now_iso, lane=lane)
                    except Exception:
                        logger.exception("could not record the vol_death witness for %s",
                                         pos.get("id"))
                # EVERY OTHER VERDICT IS A BLIND MINUTE, AND IT LEFT NO TRACE (Gamma G5, operator
                # 2026-10-08: "make sure volume death works everywhere"). Coverage was 89% on
                # Solana, 25-36% on 4663/56, ~5% on 8453 and 0% on 1/5042, and the gaps could not
                # be split by cause because only the rows above were written. Own reason, so the
                # (position, reason, minute) upsert never replaces a watch or fire row; at most one
                # row per position per minute; observation only, it can neither fire nor hold.
                if _vd_why != "measured" and _vd_why not in POST_THRESHOLD_REASONS:
                    try:
                        m.record_exit_trigger_audit(
                            conn, position_id=pos["id"], reason="vol_death_blind",
                            display_price=decision_mid, preview_usdc=None, allowed=False,
                            detail="%s chain=%s silence=%.1fs" % (
                                _vd_why, pos.get("network_id"),
                                _vd_silence if _vd_silence is not None else -1.0),
                            checked_at=now_iso, lane=lane)
                    except Exception:
                        logger.exception("could not record the vol_death blind minute for %s",
                                         pos.get("id"))
                if _vd_fire:
                    reason = "vol_death"
                    # `why` names WHICH evidence fired it: `measured_state_still` is the pool's own
                    # reserves corroborating, `measured_no_state_witness` is swap silence alone on
                    # a coin the reserve lane never watched. The two are not the same claim and
                    # must never be pooled when this rail is graded.
                    logger.warning("vol_death: pos=%s mint=%s silence=%.1fs (exit_s=%s) why=%s",
                                   pos["id"], pos["mint"], _vd_silence,
                                   getattr(cfg, "MEMECOIN_VOLDEATH_EXIT_S", 60.0), _vd_why)
                elif _vd_why in HELD_WITNESS_REASONS and held_log_due(reserve_witness_cache, pos["id"], _vd_why):
                    # HELD. Loud, because this is the 44813 case and its rate is the whole
                    # justification for the second witness.
                    logger.warning("vol_death HELD: pos=%s mint=%s swap silence=%.1fs but %s",
                                   pos["id"], pos["mint"], _vd_silence or -1.0, _vd_why)
            except Exception:
                # The rail must never take the sweep down; a broken read means no fire this pass.
                logger.exception("armed volume-death check failed pos=%s", pos.get("id"))
        # engine-authoritative adapter (2026-07-24 v3 prereg): only when the envelope's own live
        # rails (catastrophe / unquotable / timecap, computed above) found NOTHING this sweep — the
        # envelope always wins a tie — and only for a position inside the N=5 canary bound.
        engine_fraction = None
        engine_exit = False
        engine_audit = None
        if reason is None and pos["id"] in engine_ids:
            loaded_controller = _read_engine_state(xc, pos, now_iso)
            if loaded_controller is not None:
                try:
                    _reduce_engine_controller(
                        conn, pos, loaded_controller[0], preview, now_iso)
                except Exception:
                    # Controller persistence cannot suppress the envelope's fixed safety rails.
                    logger.exception("engine controller update failed pos=%s", pos["id"])
            future_context = None
            try:
                from sniper.services.memecoin_holder_evidence import future_context_at
                future_context = future_context_at(
                    conn, pos, decision_at=now_iso,
                    max_age_s=cfg.MEMECOIN_HOLDER_EVIDENCE_MAX_AGE_S)
            except Exception:
                # Observation analytics can disappear, but can never suppress a funded rail.
                logger.exception("engine future context unavailable pos=%s", pos["id"])
            pos["_engine_future_context"] = future_context
            decision, engine_audit = _engine_decision_for(
                xc, pos, frozen_by_id[pos["id"]], now_iso)
            if engine_audit is not None:
                engine_audits.append(engine_audit)
            mapped_reason, engine_fraction = _map_engine_decision(decision)
            if mapped_reason == "real_trail":
                # Unactionable-CUT cooldown: a CUT the executable gate refused (e.g. unquotable)
                # re-proposes every sweep forever — same decision, same refusal, a no-op loop of
                # funded-sweep wakeups. Suppress the REMAP (never the decision log) for the
                # cooldown window after a refusal; any successful action clears it.
                import time as _time
                cd = _ENGINE_CUT_COOLDOWN.get(pos["id"])
                cooldown_s = float(getattr(cfg, "MEMECOIN_ENGINE_CUT_COOLDOWN_S", 300.0) or 0)
                if cd is not None and cooldown_s > 0 and _time.monotonic() - cd < cooldown_s:
                    mapped_reason = None
                else:
                    _ENGINE_CUT_COOLDOWN.pop(pos["id"], None)
            if mapped_reason is not None:
                reason = mapped_reason
                engine_exit = (mapped_reason == "real_trail")
            # RIDE / DEFER (or no materialized state) -> no action; envelope rails still guard it.
        if reason is None:
            # A HELD MIRROR SELL IS RE-DRIVEN HERE (anchor-checked exit, spec 2026-10-01 §3.2.2):
            # it fired once on the source's sell event and no rail asks again.
            reason = _exit_anchor_redrive(pos)
        if pos.pop("_attention_tightened", False) and reason is not None:
            # an auto-tighten followed by an exit in the SAME sweep (Gamma note 3): its own integrity line
            from sniper.services import memecoin_desk_attention as _att
            _att.bump("auto_tighten_then_exit")
        _risk_exit = pos.pop("_desk_risk_exit", None)
        if _risk_exit is not None:
            # RISK-LIVE EXIT: the rails won if any fired; otherwise the model's exit takes the shared
            # exit claim below like any other reason.
            import time as _t
            from sniper.services import memecoin_desk_risk_apply as _risk_apply
            _risk_apply.ack_exit(conn, pos, _risk_exit, now_epoch=_t.time(), now_iso=now_iso,
                                 status="refused" if reason is not None else "applied",
                                 reason=("rail_won:%s" % reason) if reason is not None else "nominated")
            if reason is None:
                reason = _risk_apply.EXIT_REASON
        # TP CONFIRMATION WINDOW. On the first mark at or above the take-profit, stamp the breach
        # and HOLD; fire only once the breach has been continuous for the window, and fire at the
        # CURRENT mark rather than at the target — selling into continuation is the entire point.
        #
        # THE LEAK IS REAL AND ACCEPTED. A coin that reverts inside the window loses the +25% exit
        # and is owned by the trail, stop and timecap afterwards. The operator was shown that and
        # took it; it is the cost of the hypothesis, not an oversight.
        #
        # It shares `stop_breach_confirmed` because that function is pure elapsed-time arithmetic
        # with no opinion about direction, and its failure modes all resolve toward CONFIRMED —
        # which for a take-profit means falling back to today's instant behaviour rather than
        # deferring a profit indefinitely on a clock fault. Same helper, opposite rail, and the
        # reason sets stay separate.
        _tp_window = _tp_confirm_window_s()
        if reason in _TP_CONFIRM_REASONS and _tp_window > 0:
            _blind_chain = m.normalized_coin_network(
                pos.get("network_id"), pos.get("mint")) == "4663"
            if _blind_chain and not (exact_mid and exact_mid > 0):
                # 4663 only, and it is a property of the CHAIN, not of a policy id: its display
                # marks come from DexScreener, which is stale often enough that a held take-profit
                # must start and confirm from an exact whole-bag quote. A display mark can keep the
                # loss rails visible; it cannot sell a profitable bag. (It was gated on the
                # follow-30-50-v3/v4 policy ids, which is why removing the TP-window override
                # orphaned it. Same behaviour, correct predicate.)
                if pos.get("tp_breach_started_at"):
                    m.set_position_tp_breach_started(conn, pos["id"], None)
                    pos["tp_breach_started_at"] = None
                continue
            tp_started = pos.get("tp_breach_started_at")
            if not tp_started:
                tp_started = now_iso
                m.set_position_tp_breach_started(conn, pos["id"], tp_started)
                pos["tp_breach_started_at"] = tp_started
            tp_ok, tp_elapsed = stop_breach_confirmed(tp_started, now_iso, _tp_window)
            if not tp_ok:
                m.record_exit_trigger_audit(
                    conn, position_id=pos["id"], reason=reason,
                    display_price=display_mid or mid,
                    preview_usdc=_preview_value(preview, "expected_usdc"), allowed=False,
                    detail=json.dumps({"code": "tp_confirm_pending",
                                       "elapsed_s": (round(tp_elapsed, 2)
                                                     if tp_elapsed is not None else None),
                                       "window_s": _tp_window,
                                       "breach_started_at": tp_started}, sort_keys=True),
                    checked_at=now_iso, lane=lane,
                )
                logger.warning("real tp unconfirmed (%.1fs/%.1fs held) pos=%s mint=%s — riding",
                            tp_elapsed or 0.0, _tp_window, pos["id"], pos["mint"])
                continue
            if _blind_chain:
                m.set_position_tp_breach_started(conn, pos["id"], None)
                pos["tp_breach_started_at"] = None
        elif pos.get("tp_breach_started_at") and decision_mid and decision_mid > 0:
            # A QUOTED MARK BELOW THE TARGET CLEARS THE STAMP. The breach must be CONTINUOUS, so a
            # coin that touches, dips and touches again starts its window over and the take-profit
            # re-arms. Gated on a quoted mid: an unquoted sweep proves nothing about the price and
            # must not reset a window that is genuinely running.
            m.set_position_tp_breach_started(conn, pos["id"], None)
            pos["tp_breach_started_at"] = None

        # stop debounce: one bad tick reading −stop% must not market-sell a healthy coin into
        # the wick. Require the condition across N consecutive sweeps; any quoted non-stop
        # sweep releases the counter. TP/derisk/mirror are never debounced.
        required_stop_hits = (frozen_by_id[pos["id"]].stop_debounce_sweeps
                              if frozen_by_id[pos["id"]] is not None
                              else cfg.MEMECOIN_REAL_STOP_CONFIRM_SWEEPS)
        # Elapsed-time confirmation REPLACES the sweep count when enabled, because a hit count
        # cannot bound a duration: the same 75 "sweeps" is 75s on the 1s fast lane and 38 minutes
        # on the ~31s slow lane. The window is clamped (MEMECOIN_REAL_STOP_CONFIRM_MAX_S) so no
        # lane, clock fault or config can defer a stop beyond a known ceiling.
        _confirm_window = cfg.stop_confirm_window_s()
        # INSTANT at depth (2026-09-03 operator). A read at or below MEMECOIN_REAL_STOP_INSTANT_PCT
        # sells on the FIRST observation, skipping BOTH confirmation mechanisms. Measured cadence on
        # 4663 is p50 1.73s with 0% of reads at or under 1.2s, so "one more read" is ~3.5s away
        # typically, 6.0s at p99, and one gap today was 35.6s. At -50% that wait is not protection.
        #
        # It can only make a LOSS rail fire SOONER. It never defers a rail, never blocks one, and
        # never nominates one the frozen thresholds did not already nominate above — `reason` is
        # already b_stop or catastrophe here. The executable confirmation below still runs, so this
        # cannot sell without exact-balance proceeds.
        # ON AN EXACT QUOTE ONLY, never a nominal mark. This is the whole reason the bypass is safe
        # to have: the debounce exists to stop ONE bad tick market-selling a healthy coin, and
        # nominal marks manufacture exactly those -- 23 consecutive IDENTICAL dexscreener marks
        # gave LOCK a fabricated -47% on 2026-09-03 while its executable price was 2x that. A
        # fabricated tick must still be made to prove itself across two reads. An exact whole-bag
        # quote is a price the bag can actually fetch, so a single one at this depth is evidence.
        # Consequence: 4663 funded (exact_policy) gets the instant exit; a nominal-only lane keeps
        # the full two-read count, which is the correct answer for both.
        _instant_pct = float(getattr(cfg, "MEMECOIN_REAL_STOP_INSTANT_PCT", 0.0) or 0.0)
        _instant_entry = pos.get("entry_price") or 0.0
        _instant_loss = ((exact_mid / _instant_entry - 1.0) * 100.0
                         if (exact_mid and exact_mid > 0 and _instant_entry > 0) else None)
        # Grace window: a b_stop this fresh loses the instant bypass and falls back to the normal
        # debounce below -- see MEMECOIN_REAL_STOP_INSTANT_GRACE_S. Gated on MAGNITUDE past the
        # frozen catastrophe floor, never the reason label: b_stop's own check returns before
        # catastrophe is ever considered (alpha_b_real_exit_reason above), so on a standard funded
        # 4663 contract (stop_pct=35 < catastrophe_pct=80) a death spiral straight through -80%
        # still reports reason="b_stop" -- position 50341 at -90.9%/6.4s held is a real example.
        # Age is read from opened_recorded_at (stamped when open_position() runs, i.e. at/after the
        # confirmed fill) rather than opened_at (the pre-quote/sign/send/confirm decision-start
        # timestamp both executors reuse) -- same class of bug as
        # finding-opened-at-is-decision-start-not-the-fill-2026-09-03; opened_at is kept only as a
        # fallback for a row that predates the column.
        _instant_grace_s = float(getattr(cfg, "MEMECOIN_REAL_STOP_INSTANT_GRACE_S", 0.0) or 0.0)
        _grace_catastrophe_pct = (_effective_catastrophe_pct(pos, override)
                                  if (_instant_grace_s > 0 and reason == "b_stop") else 0.0)
        _past_catastrophe_floor = (_grace_catastrophe_pct > 0 and _instant_loss is not None
                                   and _instant_loss <= -_grace_catastrophe_pct)
        _in_grace = (_instant_grace_s > 0 and reason == "b_stop" and not _past_catastrophe_floor
                     and _held_s(pos.get("opened_recorded_at") or pos.get("opened_at"), now_iso)
                     < _instant_grace_s)
        # HARD NO-LOSS-SELL WINDOW (2026-09-04, operator). Stricter than `_in_grace` above, which
        # only removes the single-read bypass: this refuses a LOSS exit outright for the first
        # `MEMECOIN_REAL_NO_LOSS_SELL_S` seconds after the FILL. Measured across the funded
        # history it is positive at every window tried (15s +$23 through 60s +$124), because a
        # fast stop sells at a local low -- for holds under 60s the price was above our exit
        # 67-75% of the time within 15-60s. Catastrophe is included: loss exits inside 20s number
        # 18 all-time with ZERO catastrophes, and inside 60s, 66 with one.
        _no_loss_sell_s = float(getattr(cfg, "MEMECOIN_REAL_NO_LOSS_SELL_S", 0.0) or 0.0)
        _fill_age = _fill_age_s(pos, now_iso)
        if (_no_loss_sell_s > 0 and reason in _LOSS_EXIT_REASONS
                and _fill_age < _no_loss_sell_s):
            # CLEAR THE REASON, DO NOT `continue`. The loop's own `elif reason:` already treats a
            # falsy reason as "no exit this pass", so the rest of the iteration -- marks, peak
            # tracking, telemetry, the rails-deaf check -- still runs. Skipping the whole iteration
            # would silently drop that bookkeeping for exactly the positions under stress.
            # WARNING, not INFO: the root level is WARNING on the funded runner, so an INFO line
            # is a line that does not exist -- and a deferred loss exit is exactly the event a
            # post-mortem needs to see. There is a guard test for this.
            logger.warning("no-loss-sell window: pos=%s reason=%s held %.1fs of %.0fs — deferred",
                           pos.get("id"), reason, _fill_age, _no_loss_sell_s)
            reason = None
        # NEVER INSTANT ON A DUST ROUTE (memecoin_exit_dust_guard): DAODUN's -99.8% was one route's
        # dust, and "first read sells" signed 9 sells on it. The normal confirmation runs instead.
        _sell_now = (reason in _STOP_DEBOUNCE_REASONS and _instant_pct > 0 and not _in_grace
                     and _instant_loss is not None and _instant_loss <= -_instant_pct
                     and not _dust_route)
        if _sell_now:
            # NO stop_hits BUMP HERE, deliberately. I added one first, to preserve the arming
            # evidence that `stop_hits` carries during an outage. It is erased three steps later:
            # the executable-confirmation failure path resets stop_hits to 0 for exactly these two
            # reasons, so the bump only LOOKED like evidence. The durable trace is the exit-trigger
            # audit row written unconditionally below -- and it is strictly better, because it names
            # WHY the armed stop could not sell (preview_unavailable) instead of just counting.
            # Logged as well, because no debounce_pending row is also what an unchecked fire looks
            # like.
            logger.warning(
                "real %s INSTANT at %.1f%% (<= -%.1f%%) pos=%s mint=%s — first read sells, "
                "confirmation skipped", reason, _instant_loss, _instant_pct, pos["id"], pos["mint"])
        elif reason in _STOP_DEBOUNCE_REASONS and _confirm_window > 0:
            started = pos.get("stop_breach_started_at")
            if not started:
                started = now_iso
                m.set_position_stop_breach_started(conn, pos["id"], started)
                pos["stop_breach_started_at"] = started
            confirmed, elapsed = stop_breach_confirmed(started, now_iso, _confirm_window)
            if not confirmed:
                m.record_exit_trigger_audit(
                    conn, position_id=pos["id"], reason=reason,
                    display_price=display_mid or mid,
                    preview_usdc=_preview_value(preview, "expected_usdc"), allowed=False,
                    detail=json.dumps({"code": "breach_pending",
                                       "elapsed_s": round(elapsed, 2) if elapsed is not None else None,
                                       "window_s": _confirm_window,
                                       "breach_started_at": started}, sort_keys=True),
                    checked_at=now_iso, lane=lane,
                )
                logger.warning("real %s unconfirmed (%.0fs/%.0fs elapsed) pos=%s mint=%s — holding",
                            reason, elapsed or 0.0, _confirm_window, pos["id"], pos["mint"])
                continue
        elif reason in _STOP_DEBOUNCE_REASONS and required_stop_hits > 1:
            hits = pos.get("stop_hits") or 0
            # 0.75 of a tick, not a whole one: consecutive regular fast-lane quotes land under 1.0 s
            # apart 34.9% of the time (quote-time jitter, 24 h to 2026-09-27) and under 0.75 s 7.2%.
            counts, started = stop_hit_counts(hits, pos.get("stop_breach_started_at"), now_iso,
                                              0.75 * float(cfg.MEMECOIN_FASTLANE_TICK_S))
            if started != pos.get("stop_breach_started_at"):
                m.set_position_stop_breach_started(conn, pos["id"], started)
                pos["stop_breach_started_at"] = started
            if counts:
                hits += 1
                m.set_position_stop_hits(conn, pos["id"], hits)
            if hits < required_stop_hits:
                m.record_exit_trigger_audit(
                    conn, position_id=pos["id"], reason=reason,
                    display_price=display_mid or mid,
                    preview_usdc=_preview_value(preview, "expected_usdc"), allowed=False,
                    detail=json.dumps({
                        "code": "debounce_pending", "hits": hits,
                        "required_hits": required_stop_hits,
                    }, sort_keys=True),
                    checked_at=now_iso,
                    lane=lane,
                )
                logger.warning("real %s unconfirmed (%d/%d sweeps) pos=%s mint=%s — holding",
                            reason, hits, required_stop_hits, pos["id"], pos["mint"])
                continue
        elif decision_mid and decision_mid > 0:
            # A quoted non-stop sweep RELEASES both mechanisms, identically. The breach must be
            # continuous: a coin that recovers and dips again starts its window over.
            if (pos.get("stop_hits") or 0) > 0:
                m.set_position_stop_hits(conn, pos["id"], 0)
            if pos.get("stop_breach_started_at"):
                m.set_position_stop_breach_started(conn, pos["id"], None)
                pos["stop_breach_started_at"] = None
        stop_on_fallback = False         # set by the price confirmation below
        custom_stop_confirmed = False
        if reason in _EXECUTABLE_PRICE_REASONS:
            # ALWAYS confirm + audit an executable reason against exact-balance proceeds — including the
            # blind-chain path (4663) that precomputed `preview` above as a price fallback. Gating this
            # on `preview is None` let 4663 exits skip the guard entirely and write zero audit rows
            # (2026-07-18 fix); reuse the precomputed preview, only fetching one when absent.
            _tape_src = "confirm" if preview is None else pos.get("_exit_preview_src")
            if preview is None:
                preview_fn = ctx.get("preview_exit_fn")
                if preview_fn is not None:
                    try:
                        preview = _timed_preview(pos, preview_fn)
                        _keep_route_quote(pos, preview, "exit_audit", attempted=True)
                    except Exception:
                        _keep_route_quote(pos, None, "exit_audit", attempted=True)
                        logger.exception("real exit preview failed pos=%s mint=%s", pos["id"], pos["mint"])
            allowed, detail = executable_exit_allowed(
                conn, pos, reason, preview, override=override, current_mcap=mcap,
                decimals_fn=ctx.get("decimals_fn"),
                engine_fraction=engine_fraction, engine_exit=engine_exit)
            if allowed and reason == "b_stop" and not detail.get("mcap_stop_confirmed"):
                # THE BOUND POOL IS THE STOP'S NATIVE RUNG on Solana (73927, 73930).
                # An explicit market-cap stop already crossed its own authoritative threshold.
                allowed, detail = _native_stop_rung(pos, preview, detail, display_mid or mid,
                                                    reserve_witness_cache, override, now_iso,
                                                    ctx.get("decimals_fn"))
            stop_on_fallback = bool(detail.get("stop_on_fallback"))
            custom_stop_confirmed = bool(
                allowed and reason == "b_stop" and detail.get("mcap_stop_confirmed")
                and not detail.get("percent_stop_confirmed"))
            # Which market cap an mcap target was read on: fdv | fdv_mark | provider_mcap |
            # entry_ratio | mark_mcap. Two bases differ by 20% on some coins; the row must say which.
            detail["market_mcap"] = mcap
            detail["market_mcap_basis"] = pos.get("_mcap_basis")
            detail["current_mcap"] = detail.get("executable_mcap", mcap)
            detail["mcap_basis"] = detail.get("executable_mcap_basis", pos.get("_mcap_basis"))
            # WHAT THE DISPLAY PRICE WAS (2026-09-27, display only: the decision is already made).
            # The row's display_price is the market mid; label it so a -2.0% mid beside a -35.4%
            # exact confirmation reads as a stale display, not as a bad exit.
            if detail.get("executable_unit_pnl_pct") is not None:
                detail["decision_basis"] = "route"
            detail.update(_display_evidence(st, pos.get("entry_price"), display_mid or mid, now_iso))
            m.record_exit_trigger_audit(
                conn, position_id=pos["id"], reason=reason, display_price=display_mid or mid,
                preview_usdc=_preview_value(preview, "expected_usdc"), allowed=allowed,
                detail=json.dumps(detail, sort_keys=True), checked_at=now_iso, lane=lane,
            )
            # EXIT TAPE (spec 2026-09-30 §3a): an in-memory append, no I/O; never raises.
            # decision_at=now_iso is the attempt's attempted_at, the exact join key.
            _exit_tape.record_confirm(pos, reason=reason, decision_at=now_iso, lane=lane,
                                      preview=preview, allowed=allowed, detail=detail,
                                      preview_src=_tape_src)
            if not allowed:
                if reason in _STOP_DEBOUNCE_REASONS:
                    m.set_position_stop_hits(conn, pos["id"], 0)
                if engine_audit is not None:
                    engine_audit["executable_confirmed"] = False
                    engine_audit["outcome"] = f"refused:{detail.get('code')}"
                if engine_exit:
                    import time as _time
                    _ENGINE_CUT_COOLDOWN[pos["id"]] = _time.monotonic()
                logger.warning("real %s rejected by executable quote pos=%s detail=%s",
                            reason, pos["id"], detail.get("code"))
                continue
            if engine_audit is not None:
                engine_audit["executable_confirmed"] = True
        if reason == "derisk":
            res = _run_derisk(conn, ctx, pos, decision_mid, now_iso, preview=preview,
                              fraction_override=engine_fraction)
            _record_exit_decision(conn, pos, reason=reason, lane=lane, price=decision_mid,
                                  basis=decision_basis, mark=st, preview=preview,
                                  preview_src=decision_preview_src, now_iso=now_iso)
            if engine_audit is not None:
                engine_audit["outcome"] = str((res or {}).get("status") or "no_result")
            if res is not None:
                results.append(res)
        elif reason:
            # A BAG WHOSE LADDER ALREADY FAILED DOES NOT GET RE-DRIVEN EVERY TICK.
            #
            # Measured on 4663 since 2026-08-30: 83 stuck attempts across FIVE positions. Position
            # 46888 ran 65 failing ladders over 31 minutes -- 8.0s, 8.0s, 10.2s, 10.5s, 11.1s --
            # re-fired every 10-16s with no memory, by three rails interleaved. A stuck attempt
            # walks EVERY slippage rung and quotes at each, so the failing path costs strictly more
            # than the working one. `manual` always pierces: the operator hand-closed 4 of 7 stuck
            # positions and must never be blocked while rescuing a bag.
            #
            # PLACED HERE, AFTER `record_exit_trigger_audit` AND BEFORE `exit_fn`, deliberately.
            # Earlier would skip the preview quote too and save another second or two, but it would
            # also stop recording that the rail WANTED to fire, and that audit row is how we know a
            # take-profit was reached at all. The ladder is 8-11s against a preview of one quote, so
            # suppressing the ladder alone takes nearly all of the waste and costs no visibility.
            try:
                from sniper.services.memecoin_stuck_backoff import stuck_backoff
                _blocked, _why = stuck_backoff(
                    conn, pos["id"], pos.get("network_id"), exit_reason=reason, now_iso=now_iso)
            except Exception:
                # Never let the saver of RPC become the reason a position cannot leave.
                logger.exception("stuck backoff raised pos=%s — allowing exit", pos["id"])
                _blocked, _why = False, None
            if _blocked:
                if engine_audit is not None:
                    engine_audit["outcome"] = f"suppressed:{_why}"
                continue
            # HAND THE EXECUTOR THE QUOTE WE ALREADY BOUGHT, for every reason that bought one.
            # This was scoped to _STOP_DEBOUNCE_REASONS by 2ed78097 ("Fix EVM exit quote capacity"),
            # but the preview is fetched a few lines above for EVERY reason in
            # _EXECUTABLE_PRICE_REASONS -- real_tp, real_trail and peak_stall included -- and used
            # to confirm the rail. Those three then DROPPED it, so the executor quoted the same bag
            # again inside its slippage ladder. A take-profit paid for two route quotes and used
            # one, on the money path, at the moment the position is trying to sell.
            #
            # Safe by construction rather than by argument: `_prepared_quote` in evm_exit_executor
            # accepts the preview ONLY when `amount_atomic` equals the live token balance exactly
            # and the payload carries a `transaction` dict. Any mismatch returns None and the ladder
            # quotes fresh, which is today's behaviour. So this can remove a quote, never substitute
            # a wrong one.
            #
            # Condition is `preview is not None`, not a reason set: a reason that did not buy a
            # preview has nothing to hand over, and a new executable reason should inherit this
            # without anyone remembering to add it to a second list.
            if preview is not None:
                pos["_confirmed_exit_preview"] = preview
            pos["_exit_current_mcap"] = mcap
            # Handed to the execution witness explicitly (confirm_execution_stop's bag_unknown).
            pos["_exit_bag_unknown"] = stop_on_fallback
            pos["_exit_custom_stop_confirmed"] = custom_stop_confirmed
            try:
                res = ctx["exit_fn"](conn, pos, reason, now_iso)
            finally:
                pos.pop("_confirmed_exit_preview", None)
                pos.pop("_exit_current_mcap", None)
                pos.pop("_exit_bag_unknown", None)
                pos.pop("_exit_custom_stop_confirmed", None)
                pos.pop("_exit_preview_ms", None)
                pos.pop("_exit_preview_src", None)
            _record_exit_decision(conn, pos, reason=reason, lane=lane, price=decision_mid,
                                  basis=decision_basis, mark=st, preview=preview,
                                  preview_src=decision_preview_src, now_iso=now_iso)
            if engine_audit is not None:
                engine_audit["outcome"] = str((res or {}).get("status") or "no_result")
            if reason == "b_stop":
                # Shadow telemetry must not delay funded submission. Evaluate only after exit_fn
                # has returned; this never suppresses or changes the result.
                _record_protected_hold_shadow(conn, pos, mid, preview, now_iso, lane)
            _alert_exit_result(pos["mint"], pos["id"], reason, res,
                               handle=pos.get("source_handle"), ticker=pos.get("ticker"),
                               entry_notional=pos.get("entry_notional"))
            if (res or {}).get("status") == "closed":
                check_wallet_demotion(conn, pos.get("source_user_id"),
                      network_id=pos.get("network_id"))
                if reason == "mcap_target":
                    m.mark_exit_target_filled(conn, pos["id"], now_iso)
            results.append(res)
        # WO-12: notify on ACTED engine decisions only (envelope-rail exits keep their own alerts).
        # Post-execution + best-effort, so telemetry can never delay or alter a funded exit.
        if engine_audit is not None and reason is not None:
            _safe_alert(_alert_engine_decision, pos, engine_audit, res)
    # Audit contention is intentionally below the complete funded-position loop. A lock can delay
    # or drop telemetry here, but can never delay this sweep's confirmation or executor calls.
    _flush_engine_decisions(xc, engine_audits)
    # Observability, AFTER the funded exit hooks: persist 4663 liquidity (GT reserve_in_usd) for the
    # display. The web can't reach GT (per-IP rate limit) but the runner can — reuse the pool discovery
    # it already makes (cached ~10 min, no meaningful added load), capture the deepest pool's reserve,
    # write it to memecoin_token_liquidity. Best-effort; never affects exits. (2026-07-18)
    try:
        from sniper.execution.evm_v4_quote import _gt_pools_for_mint, top_pool_reserve_usd
        for pos in positions:
            if str(pos.get("network_id")) != "4663":
                continue
            _gt_pools_for_mint("4663", pos["mint"])                 # warm discovery (10-min cached)
            reserve = top_pool_reserve_usd("4663", pos["mint"])
            if reserve is not None:
                m.set_token_liquidity(conn, "4663", pos["mint"], reserve, now_iso)
    except Exception:
        logger.exception("real sweep: 4663 liquidity persist failed (exits unaffected)")
    # Observability, AFTER the funded exit hooks (never before submission): persist this sweep's
    # priced marks so EVERY open real position has an in-house chart tape, same as the paper sweep.
    # Priced chains (Solana/priced-EVM) were the gap — open real Solana positions had zero marks, so
    # the in-card chart showed "collecting" forever. Log-only, deduped, never raises into the sweep.
    # STALE UNTIL 2026-09-03, corrected: this said 4663 has an empty price_map here. The
    # FOMO_TO_DEX gap is closed -- '4663' maps to 'robinhood' and the chain carries 80,872
    # dexscreener marks over 3 days across 178 mints. A genuinely blind chain still falls
    # through to the blind-capture worker; 4663 is no longer one.
    write_marks(conn, price_map, now_iso)
    if xc is not None:
        try:
            xc.close()
        except Exception:
            logger.exception("engine exit: exit-state DB close failed (harmless)")
    return results


def _record_protected_hold_shadow(conn, pos, display_price, preview, now_iso,
                                  lane="periodic_sweep"):
    """Count fresh armed-holder consensus without suppressing the funded stop."""
    try:
        from sniper.services.memecoin_holder_evidence import fresh_positive_holders
        holders = fresh_positive_holders(
            conn,
            network_id=pos.get("network_id"),
            mint=pos.get("mint"),
            exclude_user_id=pos.get("source_user_id"),
            now_iso=now_iso,
            max_age_s=cfg.MEMECOIN_HOLDER_EVIDENCE_MAX_AGE_S,
            min_remaining_usd=cfg.MEMECOIN_REAL_MIRROR_MIN_REMAINING_USD,
        )
        handles = sorted({
            holder.get("source_handle") or holder.get("source_user_id")
            for holder in holders
        })
        detail = {
            "authority": "shadow_only",
            "would_protect": bool(holders),
            "funded_stop_suppressed": False,
            "consensus_count": len(holders),
            "consensus_handles": handles,
            "network_id": pos.get("network_id"),
            "mint": pos.get("mint"),
            "max_evidence_age_s": cfg.MEMECOIN_HOLDER_EVIDENCE_MAX_AGE_S,
            "min_remaining_usd": cfg.MEMECOIN_REAL_MIRROR_MIN_REMAINING_USD,
        }
        m.record_exit_trigger_audit(
            conn,
            position_id=pos["id"],
            reason="protected_hold_shadow",
            display_price=display_price,
            preview_usdc=_preview_value(preview, "expected_usdc"),
            allowed=bool(holders),
            detail=json.dumps(detail, sort_keys=True),
            checked_at=now_iso,
            lane=lane,
        )
    except Exception:
        logger.exception(
            "protected-hold shadow evaluation failed pos=%s (funded stop unchanged)",
            pos.get("id"),
        )


def _run_derisk(conn, ctx, pos, mid, now_iso, *, preview=None, fraction_override=None):
    """Size the one-shot de-risk partial from exact-balance executable proceeds. The display mid
    only nominates the action; it never sizes this on-chain partial. Two sizing modes, from the
    position's frozen policy: 'recover_entry' (sell what recovers the unrecovered entry stake —
    legacy) and 'fraction' (v2: sell a FIXED policy fraction, banking part of the move without
    the ~0.83 bag sale recover-entry sizing would imply at a +20% trigger). `fraction_override`
    (the exit-engine-v3 adapter only) bypasses the frozen policy's sizing mode entirely — the
    caller (an engine TAKE_PROFIT/DERISK decision) already computed the fraction; the sanity bound
    below still applies. The underlying `derisk_fn` primitive is itself a one-shot guarded by
    `derisked_at` regardless of caller, so an engine partial and a legacy partial can never both fire."""
    derisk_fn = ctx.get("derisk_fn")
    if derisk_fn is None:
        logger.warning("derisk: venue for pos=%s has no derisk_fn (skipped)", pos["id"])
        return None
    entry_notional = pos.get("entry_notional") or 0.0
    expected = _preview_value(preview, "expected_usdc")
    try:
        expected = float(expected)
    except (TypeError, ValueError):
        return None
    if expected <= 0:
        return None
    if fraction_override is not None:
        fraction = float(fraction_override)
    else:
        mode, policy_fraction = _derisk_sizing(pos)
        if mode == "fraction":
            fraction = policy_fraction
        else:
            prior = float(m.derisk_proceeds(conn, pos["id"]) or 0.0)
            needed = max(0.0, float(entry_notional) - prior)
            if needed <= 0:
                return None
            fraction = needed / expected
    if fraction >= 0.9 or fraction <= 0.0:
        return None   # mark fell back near/below entry between rail-check and now -> not derisk-worthy
    res = derisk_fn(conn, pos, fraction, now_iso)
    _alert_exit_result(pos["mint"], pos["id"], "derisk", res, handle=pos.get("source_handle"),
                       ticker=pos.get("ticker"), entry_notional=pos.get("entry_notional"))
    return res


def reconcile_scopes(contexts):
    """(main_scope, worker_scope) for real_reconcile. The Ethereum worker, which exists only when
    chain 1 has a context, reconciles chain 1; the main loop reconciles EVERY other network,
    including one with open inventory and no context (it stays 'unreadable' there, never silent).
    worker_scope is None when there is no worker."""
    worker = {"1"} & set(contexts or {})
    return {"exclude_network_ids": worker}, ({"network_ids": worker} if worker else None)


def real_reconcile(conn, contexts, *, alert_fn=None, network_ids=None, exclude_network_ids=()):
    """Ledger vs on-chain drift check for real open positions — each position's balance read routed to
    its OWN venue (an EVM position reconciles against its EVM balance, not the Solana RPC).
    Scope with `reconcile_scopes(contexts)`."""
    from sniper.services.memecoin_reconcile import reconcile_real_positions

    def balance_fn(pos):
        ctx = context_for_position(contexts, pos)
        if ctx is None:
            raise RuntimeError("no context for network %s" % pos.get("network_id"))
        # the CHECKED reader raises on a failed read; the plain token_fn reports 0.0, which
        # reconciliation cannot tell apart from an emptied wallet (see memecoin_reconcile).
        return (ctx.get("token_checked_fn") or ctx["token_fn"])(pos["mint"])
    return reconcile_real_positions(conn, balance_fn=balance_fn, alert_fn=alert_fn or alerts.alert_drift,
                                    network_ids=network_ids, exclude_network_ids=exclude_network_ids)


def evm_entry_never_admitted(row, *, now_iso, confirmed_nonce, pending_nonce_for,
                             token_balance_for, min_age_s=None, proof_snapshot_for=None):
    """A reason string when a reserved EVM ENTRY can never land and provably never did, else None.

    The mirror of `evm_close_provably_dead`'s SECOND proof, which exists because a transaction the
    node never admitted never moves the nonce. For a close that pins a bag; for an entry it pins
    the NOTIONAL, and on a small bankroll the notional is the whole bankroll, so the bot cannot
    send the very transaction whose nonce would free the row. Reservation 1967 held the funded 4663
    leg for 3h20m that way on 2026-09-04.

    Four facts, all required, every read failure returns None so an outage releases nothing:

      1. the account's mined nonce still EQUALS this reservation's nonce -- unspent, so proof one
         (nonce superseded) cannot apply and this branch is the only one that can;
      2. `pending` equals `latest` -- the node states it holds NOTHING of ours, which is stronger
         than "no receipt", a single lagging node can get that wrong;
      3. the wallet holds NONE of the coin. An entry that landed would have BOUGHT it, so a zero
         balance is proof it never did. This is the close proof's bag test, inverted: there an
         intact bag proves the SELL never landed, here an absent bag proves the BUY never landed;
      4. it has been unmined past a floor, so a just-broadcast entry is never called dead.

    ON AN EXPIRY CHAIN (chain 1, private sends) facts 2 and 4 are replaced (step 3.2). Custody
    cannot see a privately held swap, so `pending == latest` is no evidence either way. The proof
    is the swap's OWN deadline, decoded from the bound bytes: chain time past deadline + margin,
    the nonce still unspent and a coin balance of exactly zero, ALL AT ONE BLOCK (the proof
    snapshot), so a fresher chain time can never sit beside a staler nonce. A bound row with no
    payload, an unknown selector or a failed snapshot holds the row.

    The caller must clear the signed payload when this returns, in the same transaction.
    """
    if token_balance_for is None:
        return None
    try:
        nonce = int(row.get("nonce"))
    except (TypeError, ValueError):
        return None
    if not isinstance(confirmed_nonce, int) or isinstance(confirmed_nonce, bool):
        return None
    if confirmed_nonce != nonce:
        return None
    if str(row.get("network_id")) in cfg.memecoin_evm_expiry_chains():
        snap = (proof_snapshot(lambda: proof_snapshot_for(row.get("network_id"), row.get("mint")))
                if proof_snapshot_for is not None else None)
        if snap is None:
            return None
        if snap["nonce"] != nonce:
            return None                    # spent by that block (it landed, or was replaced)
        expired = evm_swap_expired(row.get("network_id"), row.get("signed_payload_b64"), snap["ts"])
        if not expired:
            return None
        if float(snap["balance"]) != 0.0:
            return None                    # we hold the coin: it LANDED
        return ("evm_entry_expired: %s at block %d, nonce %d still unspent, wallet holds none of "
                "the coin" % (expired, snap["block"], nonce))
    if pending_nonce_for is None:
        return None
    if min_age_s is None:
        min_age_s = float(getattr(cfg, "MEMECOIN_EVM_DROPPED_ENTRY_MIN_AGE_S", 300.0) or 300.0)
    age = _seconds_since(row.get("broadcast_at") or row.get("reserved_at"), now_iso)
    if age is None or age < float(min_age_s):
        return None
    try:
        pending_nonce = pending_nonce_for(row.get("network_id"))
        balance = token_balance_for(row.get("network_id"), row.get("mint"))
    except Exception:
        logger.exception("evm dropped-entry probe failed pid=%s", row.get("id"))
        return None
    if not isinstance(pending_nonce, int) or pending_nonce != confirmed_nonce:
        return None                        # a node still holds something of ours: it may yet mine
    if isinstance(balance, bool) or not isinstance(balance, (int, float)):
        return None                        # unreadable balance: hold it
    if float(balance) != 0.0:
        return None                        # we hold the coin: it LANDED, this is an orphan not a ghost
    return ("evm_entry_never_admitted: latest == pending == nonce %d, wallet holds none of the "
            "coin, unmined for %.0fs" % (nonce, age))


def _confirmed_sig_holds_no_token(row, *, token_balance_for, confirmed_nonce_for):
    """True only when the chain PROVES a confirmed reservation left us holding none of the coin.

    ON EVM THE BOUND SIG IS THE NATIVE PRE-TRANSACTION, NOT THE SWAP (2026-09-03). The write-ahead
    binds the WETH unwrap that funds the swap, so a 'confirmed' sig proves the unwrap landed and
    says nothing about whether a coin was ever bought. When the entry then aborts, the gas top-up
    re-wraps the native and the wallet holds nothing -- but the orphan rule read 'confirmed' as "we
    hold the coin", kept the slot reserved and waited for a human. Live: reservation 1955 held the
    ENTIRE 4663 bankroll ($98.79) for two hours on a zero token balance, froze every 4663 entry,
    and the chain could have answered three seconds after the abort.

    TWO INDEPENDENT PROOFS, both required; either one missing keeps the row reserved:
      1. the CHECKED token read says exactly zero. The plain `token_fn` reports 0.0 on a FAILED
         read, which is indistinguishable from an empty wallet, so it must never be used here.
      2. the account nonce has passed the entry's own nonce, so nothing signed for that entry can
         still land.
    Solana is excluded on purpose: there the bound sig IS the swap, so 'confirmed' already means
    the coin is ours and the orphan hold is correct.
    """
    if token_balance_for is None or confirmed_nonce_for is None:
        return False
    if (str(row.get("network_id")) in {"1", "57073"}
            and row.get("recovery_status") != "native_pre_transaction_ambiguous"):
        # Ink's bound signature is the swap itself. A later zero balance cannot prove that
        # a confirmed buy did not spend funds; keep its reservation until receipt booking.
        return False
    if m.normalized_coin_network(row.get("network_id"), row.get("mint")) == "solana":
        return False
    try:
        entry_nonce = int(row.get("nonce"))
        confirmed_nonce = confirmed_nonce_for(row.get("network_id"))
    except (TypeError, ValueError):
        return False
    if isinstance(confirmed_nonce, bool) or not isinstance(confirmed_nonce, int):
        return False
    if confirmed_nonce <= entry_nonce:
        return False
    try:
        balance = token_balance_for(row.get("network_id"), row.get("mint"))
    except Exception:
        return False                    # unreadable -> stay reserved, the fail-closed direction
    if isinstance(balance, bool) or not isinstance(balance, (int, float)):
        return False
    return float(balance) == 0.0


def recover_stale_reservations(conn, *, confirm_for, now_iso, max_age_s=None, alert_fn=None,
                               height_for=None, expiry_proof_s=None, confirm_proof_for=None,
                               confirmed_entry_for=None, confirmed_nonce_for=None,
                               token_balance_for=None, pending_nonce_for=None,
                               proof_snapshot_for=None, network_ids=None):
    """Resolve reservations stuck in 'reserved' past max_age_s (the write-ahead's promised recovery,
    previously unimplemented). Per stale row:
      - no tx_sig and no broadcast timestamp -> provably never broadcast -> release the slot.
      - broadcast timestamp without tx_sig -> ambiguous write-ahead violation -> keep reserved.
      - tx failed  -> the tx reverted -> release the slot.
      - tx confirmed but no position -> the tx LANDED and we hold the coin with no ledger row (funds
        at risk) -> tag 'orphaned', alert ONCE, and KEEP it reserved (still blocks + counts exposure)
        for manual reconcile — never silently free a slot whose tokens we actually hold.
      - pending/unavailable -> leave reserved, retry next pass (never resubmit).
      - Solana not_found -> release only after three spaced explicit responses after 20 minutes.
      - EVM not_found -> release only when the confirmed account nonce passed every stored nonce.
    `confirm_for(network_id, tx_sig)` routes to the right venue.

    EARLY EXACT-PROOF SCAN (2026-08-25, operator). `max_age_s` decides when a row is even LOOKED at,
    and at 600s it also delayed the one release that needs no waiting. A Solana blockhash lives about
    150 slots; once the chain height passes the reservation's OWN recorded
    `submission_last_valid_height`, those exact signed bytes can NEVER land. That proof is available
    roughly 90 seconds after broadcast, and reservation 855 (happyseb) still held its mint and an
    exposure slot for 10 minutes after being dead for 1302 blocks.
    `expiry_proof_s` (0 = off, the old behaviour) scans rows that young, and a row younger than
    `max_age_s` may take THAT release and nothing else.

    WHY THAT SUBSET IS SAFE, STATED CAREFULLY. `bind_entry_submission` writes the sig and the height
    BEFORE network I/O, so a row carrying both may still have a LIVE executor between bind and send —
    do not read the binding as proof that anything was broadcast. Safety does not rest on executor
    lifetime at all. It rests on the release condition: the chain height must already be past the
    binding, so the blockhash is expired at the moment we release, and any bytes that executor sends
    afterwards can never land. A transaction that DID land answers confirmed or pending, never
    not_found, and the `status != not_found` guard below skips it. The residual hole is a lagging node
    answering not_found for a landed transaction, which is what the two spaced reads already guard and
    is unchanged here.

    The other branches stay on max_age_s because they lack that property: `never_broadcast` releases a
    row with NO sig, where nothing bounds when the executor might still send, and the confirmed-orphan
    and failed branches mutate state or alert on evidence that is not time-critical.
    The release conditions themselves are unchanged: 2+ spaced not_found reads, 60s span, and chain
    height past the binding. Only the clock that admits the row to the scan moved.
    Returns [(pending_id, action)]."""
    from datetime import datetime, timedelta
    if max_age_s is None:
        max_age_s = cfg.MEMECOIN_RESERVATION_STALE_S
    if expiry_proof_s is None:
        expiry_proof_s = getattr(cfg, "MEMECOIN_RESERVATION_EXPIRY_PROOF_S", 0) or 0
    try:
        now_dt = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
        scan_age_s = min(max_age_s, expiry_proof_s) if expiry_proof_s > 0 else max_age_s
        cutoff = (now_dt - timedelta(seconds=scan_age_s)).isoformat()
    except (ValueError, TypeError):
        return []
    out = []
    for row in m.stale_reserved_rows(conn, cutoff):
        if network_ids is not None and str(row.get("network_id") or "solana") not in network_ids:
            continue
        pid, mint, sig = row["id"], row["mint"], row.get("tx_sig")
        cleanup_sig = row.get("cleanup_tx_sig")
        if cleanup_sig:
            cleanup_status = confirm_for(row.get("network_id"), cleanup_sig)
            m.mark_pending_cleanup_status(conn, pid, cleanup_status)
            if cleanup_status == "confirmed":
                m.release_pending(conn, pid, "native_cleanup_recovered")
                out.append((pid, "released_cleanup_confirmed"))
                continue
        # A row admitted only by the early scan is restricted to the exact expiry proof, which needs
        # BOTH a sig and a height binding, so anything lacking either is skipped before any chain
        # read. That also covers the no-sig case: `never_broadcast` would otherwise free a slot whose
        # executor has nothing bounding when it might still send, and unlike the expiry proof there
        # is no expired blockhash to make a late send harmless.
        # The pair is a PRECONDITION for the proof, never evidence that a broadcast happened: the
        # write-ahead persists both before network I/O. See the docstring.
        early_only = _held_s(row.get("reserved_at"), now_iso) < max_age_s
        if early_only and not sig:
            continue
        if not sig:
            if row.get("broadcast_at"):
                if row.get("recovery_status") != "ambiguous_broadcast":
                    m.mark_pending_recovery_status(conn, pid, "ambiguous_broadcast")
                    if alert_fn:
                        _safe_alert(alert_fn, mint, pid)
                out.append((pid, "ambiguous_broadcast"))
                continue
            m.release_pending(conn, pid, "never_broadcast")
            out.append((pid, "released_never_broadcast"))
            continue
        status = confirm_for(row.get("network_id"), sig)
        # NEGATIVE EVIDENCE NEEDS A TRUSTED WITNESS (peer review 2026-08-25). The read above goes
        # through the rotating pool on purpose: confirmed, failed and pending are POSITIVE evidence
        # — the answering node saw the transaction — and orphan detection must keep working during a
        # custody outage, because an orphan is funds already at risk. Pinning every verdict would
        # have stalled ALL recovery whenever custody rate-limits, and stuck reservations correlate
        # with flaky submission rather than being independent of it.
        # not_found is the exception: it is the only verdict that RELEASES a slot, and a null only
        # means the answering node has no record. Re-read that one through the pinned archival
        # client and take ITS answer. A pinned "unavailable" therefore leaves the row reserved,
        # which is the fail-closed direction.
        if status == "not_found" and confirm_proof_for is not None:
            proof_status = confirm_proof_for(row.get("network_id"), sig)
            if proof_status is not None:
                status = proof_status
        if (status == "not_found" and confirmed_nonce_for is not None
                and m.normalized_coin_network(row.get("network_id"), mint) != "solana"):
            try:
                nonces = [int(value) for value in (row.get("nonce"), row.get("cleanup_nonce"))
                          if value is not None]
                confirmed_nonce = confirmed_nonce_for(row.get("network_id"))
            except (TypeError, ValueError):
                nonces, confirmed_nonce = [], None
            if (nonces and isinstance(confirmed_nonce, int) and confirmed_nonce > max(nonces)
                    and str(row.get("network_id")) not in {"1", "57073"}):
                m.release_pending(conn, pid, "evm_nonces_superseded")
                out.append((pid, "released_evm_nonces_superseded"))
                continue
            # THE NONCE NEVER MOVES ON ITS OWN, SO THE RULE ABOVE CAN DEADLOCK (2026-09-04).
            # Releasing above needs the account nonce to PASS this one. A transaction the node
            # never admitted never moves it, so that never happens by itself -- and the reservation
            # meanwhile holds its notional, which on a small bankroll is all of it. The bot then
            # cannot trade, so it sends nothing, so the nonce stays put, so the row is never
            # released. Circular, and it stopped the funded 4663 leg for 3h20m on reservation 1967
            # ($55.42 against a $55.30 balance). It only broke because the operator happened to
            # sell an unrelated position by hand, which consumed the nonce.
            #
            # Same four facts the CLOSE path already proves in `evm_close_provably_dead`, with the
            # bag test inverted for an entry: an entry that landed would have BOUGHT the coin, so
            # holding none of it is the proof it never did.
            reason = evm_entry_never_admitted(
                row, now_iso=now_iso,
                confirmed_nonce=confirmed_nonce,
                pending_nonce_for=pending_nonce_for,
                token_balance_for=token_balance_for,
                proof_snapshot_for=proof_snapshot_for)
            if reason:
                # Destroy the rebroadcastable bytes in the SAME transaction as the release. The
                # signature stays valid forever; freeing the slot while anything could still put
                # those bytes on the wire is how a released row becomes an orphan bag.
                m.clear_pending_signed_payload(conn, pid)
                m.release_pending(conn, pid, reason)
                out.append((pid, "released_evm_never_admitted"))
                continue
        if early_only and status != "not_found":
            if not (status == "confirmed" and confirmed_entry_for is not None):
                continue        # only a receipt recovery may run early; all other paths wait
        if status == "failed":
            m.release_pending(conn, pid, "tx_failed_recovery")
            out.append((pid, "released_failed"))
        elif status == "pending":
            m.record_pending_recovery_check(conn, pid, status, now_iso)
        elif (status == "not_found"
              and m.normalized_coin_network(row.get("network_id"), mint) == "solana"):
            proof = m.record_pending_recovery_check(conn, pid, status, now_iso)
            try:
                first = datetime.fromisoformat(
                    str(proof.get("first_not_found_at")).replace("Z", "+00:00"))
                proof_span = (now_dt - first).total_seconds()
            except (TypeError, ValueError):
                proof_span = -1.0
            # EXACT proof beats the 20-minute proxy (same upgrade the de-risk and full-close paths
            # got): past the reservation's OWN submission_last_valid_height the buy can never land,
            # and a SECOND spaced not_found read closes the lagging-node hole. Live 2026-08-14,
            # reservation 434: blockhash 423 blocks expired, zero tokens delivered, and it still
            # blocked its mint, an exposure slot, and two deploys because the proxy owned it.
            # Fail-closed on every gap: no binding, no probe, probe error, or a lone read all keep
            # the row reserved and the proxy in charge.
            lvh = row.get("submission_last_valid_height")
            if (lvh is not None and height_for is not None
                    and int(proof.get("not_found_checks") or 0) >= 2
                    and proof_span >= 60):
                try:
                    height = height_for(row.get("network_id"))
                except Exception:
                    height = None
                if isinstance(height, (int, float)) and float(height) > float(lvh):
                    m.release_pending(conn, pid, "dropped_blockhash_expired_proven")
                    out.append((pid, "released_dropped"))
                    continue
            # The 20-minute PROXY is time-based, not evidence-based, so the early scan must never
            # reach it. Its own _held_s bound below already forbids that: 1200s is far past any
            # max_age_s, and early rows are by definition younger than max_age_s.
            if (_held_s(row.get("reserved_at"), now_iso) >= 1200
                    and int(proof.get("not_found_checks") or 0) >= 3
                    and proof_span >= 120):
                m.release_pending(conn, pid, "expired_unfound_proven")
                out.append((pid, "released_expired"))
        elif status == "confirmed":
            if confirmed_entry_for is not None:
                try:
                    recovered = confirmed_entry_for(conn, row, now_iso)
                except Exception:
                    recovered = None
                if ((isinstance(recovered, dict)
                     and recovered.get("status") in ("filled", "cleanup_confirmed"))
                        or (isinstance(recovered, int) and not isinstance(recovered, bool)
                            and recovered > 0)):
                    out.append((pid, "recovered_confirmed"))
                    continue
            if _confirmed_sig_holds_no_token(
                    row, token_balance_for=token_balance_for,
                    confirmed_nonce_for=confirmed_nonce_for):
                m.release_pending(conn, pid, "evm_confirmed_pretransaction_no_token_proven")
                out.append((pid, "released_pretransaction_no_token"))
                continue
            if row.get("recovery_status") != "orphaned":       # alert once on the transition
                m.mark_pending_orphaned(conn, pid)
                if alert_fn:
                    _safe_alert(alert_fn, mint, pid)
            out.append((pid, "orphaned_confirmed"))
        # unavailable/unrecognized -> leave reserved, retry next pass
    return out


def make_real_fns_installer(ctx, *, build_fn=None, cooldown_s=30.0, clock=None):
    """Heal a web process whose funded executor build failed at startup.

    The web server built its real context ONCE at import and cached any failure forever: a single
    bad moment at boot (RPC flake, mid-edit env) meant every manual sell answered 503 "real
    execution not configured" until an operator restarted the service — live 2026-08-14, four
    sells refused while STONKS needed out. The routes call this before each manual action; when
    the fns are present it is a no-op, otherwise it retries the build at most once per cooldown so
    a genuinely broken env cannot turn every click into an RPC storm. Still fail-closed: until a
    build SUCCEEDS the routes keep their loud 503, never a fabricated fill."""
    import time as _t
    state = {"next": 0.0}
    clock = clock or _t.monotonic

    def ensure():
        if ctx.get("real_mark_fn") is not None:
            return True
        now = clock()
        if now < state["next"]:
            return False
        state["next"] = now + float(cooldown_s)
        try:
            build = build_fn or (lambda: build_all_contexts(
                own_sidecar=False, conn=ctx.get("conn")))
            contexts = build()
            if not contexts:
                return False
            # NEVER AN EXECUTOR (2026-09-05). This lazy path installed `make_real_buy_fn` -- the
            # in-process executor the web had already given up on 2026-08-26 -- so one flaky boot
            # re-opened the 44706 divergence through the recovery door. Both money actions cross
            # to the runner as intents; the web keeps only the read-only mark from its contexts.
            from sniper.services.memecoin_manual_intents import make_enqueue_buy_fn
            from sniper.services.memecoin_manual_exit_intents import make_enqueue_exit_fn
            from sniper.util.clock import real_utc_now_iso as _now
            ctx["real_buy_fn"] = make_enqueue_buy_fn(_now)
            ctx["real_exit_intent_fn"] = make_enqueue_exit_fn(_now)
            ctx["real_mark_fn"] = make_real_mark_fn(contexts)
            logger.warning("real context RECOVERED lazily — manual buy/sell intents re-enabled")
            return True
        except Exception:
            logger.exception("lazy real context rebuild failed; manual actions stay 503")
            return False

    return ensure


# How often a STILL-tripped kill switch re-states itself in the log. A one-shot beacon cannot
# report continued death: on 2026-09-02 the switch tripped at 20:33:26Z and the funded leg stood
# down for 11.6 hours while the log said nothing at all. LOG ONLY — the switch already sends its
# one Telegram page and the operator gets one page per real event.
_KILL_RELOG_S = float(os.getenv("MEMECOIN_KILL_RELOG_S", "900"))
_KILL_LOG_STATE = {}
_KILL_DEPLOY_GRACE_S = float(os.getenv("MEMECOIN_KILL_DEPLOY_GRACE_S", "180"))


def _kill_tripped_age_s(kill_path):
    """Seconds since the switch was tripped, taken from the FILE. `prev_tripped` resets on every
    restart, so process-local state would report a fresh outage on a leg that has been dark for
    hours — which is exactly how the 11.6 h stand-down stayed invisible."""
    from sniper.services.memecoin_arming import _kill_path as _resolve
    try:
        return max(0.0, time.time() - os.path.getmtime(_resolve(kill_path)))
    except Exception:
        return None


def check_kill_switch_alert(kill_path, prev_tripped, *, now_fn=None, state=None):
    """Fire alert_kill_switch once on a False->True crossing. Returns the current tripped state (the
    caller carries it as prev_tripped so a persistently-tripped switch doesn't re-alert every cycle).

    The crossing, the clear, and a throttled STILL-TRIPPED line also go to the LOG, at WARNING —
    this process configures no logging level, so the root logger sits at WARNING and a line at INFO
    would not exist. Before this, a tripped switch was invisible in the log AND in the refusal tape
    (it surfaces as a bare `disarmed`), so its only witnesses were one Telegram message and the
    file's mtime.
    """
    from sniper.services.memecoin_arming import is_kill_tripped
    now_fn = time.monotonic if now_fn is None else now_fn
    state = _KILL_LOG_STATE if state is None else state
    now = is_kill_tripped(kill_path)
    # A DEPLOY'S OWN TRIP IS NOT AN OUTAGE (2026-09-11). Every deploy trips the switch for ~20 s
    # while the runner restarts, and the fresh runner paged "KILL-SWITCH tripped" at start-up —
    # four pages in one afternoon, each read by the operator as "the kill switch is on". A trip
    # written by `runtime-deploy` pages only if it outlives the grace, i.e. the deploy left it on.
    deploy_trip = now and _kill_trip_source(kill_path) == "runtime-deploy"
    if now and not prev_tripped:
        age = _kill_tripped_age_s(kill_path)
        if deploy_trip and age is not None and age < _KILL_DEPLOY_GRACE_S:
            state["deploy_trip_pending"] = True
            logger.warning("kill switch tripped by a deploy (age %ds) — not paged unless it outlives "
                           "%ds", int(age), int(_KILL_DEPLOY_GRACE_S))
        else:
            _safe_alert(alerts.alert_kill_switch)
        logger.warning("kill switch TRIPPED%s — funded entries stand down; exits are unaffected",
                       _kill_down_suffix(kill_path))
        state["last_logged_at"] = now_fn()
    elif now and state.get("deploy_trip_pending"):
        age = _kill_tripped_age_s(kill_path)
        if not deploy_trip or age is None or age >= _KILL_DEPLOY_GRACE_S:
            state.pop("deploy_trip_pending", None)
            _safe_alert(alerts.alert_kill_switch)
            logger.warning("kill switch STILL TRIPPED after a deploy%s — paged",
                           _kill_down_suffix(kill_path))
            state["last_logged_at"] = now_fn()
    elif now:
        last = state.get("last_logged_at")
        if last is None or now_fn() - last >= _KILL_RELOG_S:
            logger.warning("kill switch STILL TRIPPED%s — funded entries still standing down",
                           _kill_down_suffix(kill_path))
            state["last_logged_at"] = now_fn()
    elif prev_tripped:
        logger.warning("kill switch CLEARED — funded entries may resume")
        state.pop("last_logged_at", None)
        state.pop("deploy_trip_pending", None)
    return now


def _kill_trip_source(kill_path):
    """Read the source through the shared switch parser."""
    from sniper.services.memecoin_arming import kill_switch_source
    return kill_switch_source(kill_path)


def _kill_down_suffix(kill_path):
    """` (down 420m)` when the file gives a trip time, else empty. Never invents a duration."""
    age = _kill_tripped_age_s(kill_path)
    return "" if age is None else " (down %dm)" % int(age // 60)


def check_daily_loss_alert(conn, day, prev_tripped):
    """Fire alert_daily_loss once on a fresh latch. Returns the current tripped state."""
    now = float(cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD or 0) > 0 and m.is_daily_loss_tripped(conn, day)
    if now and not prev_tripped:
        _safe_alert(alerts.alert_daily_loss, day, m.daily_risk_realized_pnl(conn, day))
    return now


def check_gas_alert(rc, open_count, prev_low):
    """Fire alert_gas_low once when SOL first drops below the full-book exit reserve (entries will
    start blocking). Returns the current low state."""
    from sniper.execution.solana_wallet import native_sol_balance_value
    sol = native_sol_balance_value(rc["sol_fn"]())
    if sol is None:
        logger.warning("native SOL balance read failed; GAS-LOW state is unchanged")
        return prev_low
    from sniper.services.memecoin_risk import required_entry_sol
    required = required_entry_sol(open_count, cfg.MEMECOIN_GAS_ENTRY_SOL,
                                  cfg.MEMECOIN_GAS_EXIT_SOL, cfg.MEMECOIN_GAS_RETRY_FACTOR)
    low = sol < required
    if low and not prev_low:
        _safe_alert(alerts.alert_gas_low, sol, required)
    return low


EVM_GAS_ALERT_REARM_S = 6 * 3600.0
EVM_GAS_ALERT_LOOKBACK_S = 60.0   # a refusal older than this at the first pass is history


def check_evm_gas_alert(conn, result, state, *, now_ts=None, rearm_s=EVM_GAS_ALERT_REARM_S):
    """The EVM twin of check_gas_alert (2026-09-27). Called after each EVM gas top-up pass with
    that pass's own result, so it reads nothing the pass did not already read except one indexed
    ledger query. Pages GAS-LOW when a funded entry on the chain refused for native gas since the
    last call (again only `rearm_s` after the last page), and GAS-OK once when native covers one
    entry again. An unreadable native pages nothing and leaves `state` untouched: unknown is
    neither low nor recovered. `state` is the caller's dict, one entry per chain, touched only
    by the one thread that runs the top-up pass. Returns `state`."""
    result = result or {}
    chain = str(result.get("chain_id") or "")
    native_wei = result.get("native_before_wei")
    now = time.time() if now_ts is None else float(now_ts)
    _check_gas_pass_held(chain, result.get("state"), state, now)
    if not chain or native_wei is None:
        return state
    now_iso = datetime.fromtimestamp(now, timezone.utc).isoformat()
    st = state.get(chain)
    if st is None:
        st = state[chain] = {"low": False, "paged_at": None, "since": datetime.fromtimestamp(
            now - EVM_GAS_ALERT_LOOKBACK_S, timezone.utc).isoformat()}
    from sniper.services.memecoin_evm_risk import required_entry_native
    have = int(native_wei) / 1e18
    weth = int(result["weth_wei"]) / 1e18 if result.get("weth_wei") is not None else None
    need = required_entry_native(conn, chain)
    refused = m.native_gas_refusal_since(conn, chain, st["since"])
    st["since"] = now_iso
    if refused and (not st["low"] or now - float(st["paged_at"] or 0.0) >= float(rearm_s)):
        _safe_alert(alerts.alert_evm_gas_low, chain, have, need, weth)
        st.update(low=True, paged_at=now)
    elif st["low"] and have >= need:
        _safe_alert(alerts.alert_evm_gas_ok, chain, have, need, weth)
        st.update(low=False, paged_at=None)
    return state


EVM_GAS_PASS_HELD_PAGE_S = 15 * 60.0
_GAS_PASS_HOLD_STATES = ("reservation_in_flight", "reservation_read_failed")


def _check_gas_pass_held(chain, pass_state, state, now, *, page_s=EVM_GAS_PASS_HELD_PAGE_S):
    """GAS-PASS-HELD once per hold (Gamma LOW on 33f2d619e): a reservation that never settles (an
    ambiguous entry) holds the chain's gas pass in both directions, and its results carry no
    native balance, so the GAS-LOW check above never sees them. Logged and paged once when one
    hold has lasted `page_s`; any other pass result ends the hold."""
    if not chain:
        return
    held = state.setdefault("held:" + chain, {"since": None, "paged": False})
    if pass_state not in _GAS_PASS_HOLD_STATES:
        held.update(since=None, paged=False)
        return
    if held["since"] is None:
        held["since"] = now
    elif not held["paged"] and now - held["since"] >= page_s:
        logger.warning("EVM gas pass held chain=%s state=%s for %.0fs", chain, pass_state,
                       now - held["since"])
        _safe_alert(alerts.alert_evm_gas_pass_held, chain, (now - held["since"]) / 60.0, pass_state)
        held["paged"] = True


def make_confirm_for(contexts):
    """Route a reservation's chain-confirm to its venue's SINGLE-SHOT status check. Recovery runs
    inline in the loop's maintenance pass, so it must NOT use the executor's fill-wait poll (up to
    ~40s Solana / ~120s EVM) — a stuck reservation would otherwise stall the copy path + real-exit
    sweep every cycle. No context -> 'unavailable' (leave reserved, fail-safe)."""
    def confirm_for(network_id, sig):
        ctx = context_for(contexts, network_id)
        if ctx is None:
            return "unavailable"
        confirm = ctx.get("confirm_once_fn") or ctx.get("confirm_fn")
        return confirm(sig) if confirm else "unavailable"
    return confirm_for


def make_confirm_proof_for(contexts):
    """Route a reservation's NEGATIVE-evidence recheck to its venue's PINNED status read.

    Recovery reads status through the rotating pool, because every verdict except not_found is
    positive evidence — a node answering confirmed/failed/pending SAW the transaction — and those
    drive orphan detection, which must survive a custody outage. Only not_found releases a slot, and
    only that one needs a node whose history we trust; this router supplies it. A venue with no
    pinned read returns None so the caller keeps the pooled answer unchanged (EVM: its rows never
    reach the Solana-gated not_found release at all)."""
    def confirm_proof_for(network_id, sig):
        ctx = context_for(contexts, network_id)
        if ctx is None:
            return None
        fn = ctx.get("confirm_proof_once_fn")
        return fn(sig) if fn else None
    return confirm_proof_for


def make_confirmed_nonce_for(contexts):
    """Read the confirmed account nonce for EVM reservation recovery."""
    def confirmed_nonce_for(network_id):
        ctx = context_for(contexts, network_id)
        fn = (ctx or {}).get("confirmed_nonce_fn")
        return fn() if fn else None
    return confirmed_nonce_for


def make_proof_snapshot_for(contexts):
    """One block's view (chain time, mined nonce, coin balance) for the protocol-expiry dead proof."""
    def proof_snapshot_for(network_id, mint):
        ctx = context_for(contexts, network_id)
        fn = (ctx or {}).get("proof_snapshot_fn")
        return fn(mint) if fn else None
    return proof_snapshot_for


def make_pending_nonce_for(contexts):
    """Read the PENDING account nonce, which counts our own unmined transactions.

    `latest == pending` is the node stating it holds nothing of ours. That is what makes the
    never-admitted proof stronger than "no receipt", which one lagging node can get wrong.
    """
    def pending_nonce_for(network_id):
        ctx = context_for(contexts, network_id)
        fn = (ctx or {}).get("pending_nonce_fn")
        return fn() if fn else None
    return pending_nonce_for


def make_token_balance_for(contexts):
    """Read the wallet's balance of ONE coin for reservation recovery.

    CHECKED reader only. `token_fn` answers 0.0 when the read FAILS, and a release that frees a
    funded slot must never rest on a number a broken RPC can fabricate.
    """
    def token_balance_for(network_id, mint):
        ctx = context_for(contexts, network_id)
        fn = (ctx or {}).get("token_checked_fn")
        if fn is None:
            raise RuntimeError("no checked token reader for network %s" % network_id)
        return fn(mint)
    return token_balance_for


def make_venue_exit_fn(contexts):
    """A single exit_fn(conn, pos, reason, now) that routes by the position's venue — for the web
    server's manual take-profit button, so it works for a real position on ANY venue (not just
    Solana). No venue context -> {'status': 'no_venue'} (the route surfaces a loud error, never a
    modeled fake-close)."""
    def exit_fn(conn, pos, reason, now_iso):
        ctx = context_for_position(contexts, pos)
        if ctx is None:
            return {"status": "no_venue", "network": pos.get("network_id")}
        return ctx["exit_fn"](conn, pos, reason, now_iso)
    return exit_fn


def make_venue_partial_exit_fn(contexts):
    """A single partial_fn(conn, pos, fraction, now) that routes an operator's MANUAL partial sell
    (sell a fraction, keep the rest open) to the position's venue — reusing the EXACT-balance derisk
    executor the runner uses, so a manual 'take 25%' behaves identically to an automated de-risk
    (slippage ladder, exact on-chain balance, claim guard, kind='derisk' fill). Returns the venue
    result dict; {'status':'no_venue'} if the chain isn't configured here, {'status':'no_partial'} if
    the venue has no partial-exit executor bound. NOTE: the derisk primitive sets derisked_at, so a
    manual partial makes the automatic +derisk one-shot moot (the caller's UI discloses this)."""
    def partial_fn(conn, pos, fraction, now_iso):
        ctx = context_for_position(contexts, pos)
        if ctx is None:
            return {"status": "no_venue", "network": pos.get("network_id")}
        pf = ctx.get("derisk_fn")
        if pf is None:
            return {"status": "no_partial", "network": pos.get("network_id")}
        return pf(conn, pos, fraction, now_iso)
    return partial_fn


def make_venue_source_partial_exit_fn(contexts):
    """Route a durable source request; a complete source sale uses the full-close lifecycle."""
    def partial_fn(conn, pos, request, now_iso):
        for hold in ("tracking_paused_at", "mirror_disabled"):
            if pos.get(hold):
                return {"status": hold}
        ctx = context_for_position(contexts, pos)
        if ctx is None:
            return {"status": "no_venue", "network": pos.get("network_id")}
        facts = json.loads(request["request_json"])
        if int(facts["source_sold_atomic"]) == int(facts["source_pre_atomic"]):
            return ctx["exit_fn"](conn, pos, "mirror", now_iso)
        fn = ctx.get("source_partial_fn")
        if fn is None:
            return {"status": "no_partial", "network": pos.get("network_id")}
        return fn(conn, pos, request, now_iso)
    return partial_fn


def _pool_depth_source():
    """The `liquidity_source` label our own pool-store depth carries. Imported lazily, like every
    other use of that module here, so nothing is added to this module's import cost."""
    from sniper.services import memecoin_pool_depth as pool_depth
    return pool_depth.SOURCE


def _snapshot_priced(st):
    """The market snapshot with price/mcap/fdv blanked when the pair behind them is dust.

    DexScreener ranks a coin's pairs by quote-side depth, but while the real pool is younger than
    the indexer's lag the ONLY listed pairs can be $12 seedings (ESKER 2026-09-05: display price
    3.4x the wallet's fill from a $12.18 pair; the real $22.7k pool sat at 1.5x). A number from
    such a pair is not a price; the gates that compare prices must see UNKNOWN and fail open to
    the source fill, exactly as they do when the provider is dark. Liquidity itself stays so the
    refusal record still says which pair was read.
    """
    st = dict(st or {})
    floor = float(getattr(cfg, "MEMECOIN_REAL_SNAPSHOT_MIN_PAIR_LIQUIDITY_USD", 0) or 0)
    liq = st.get("liquidity")
    try:
        dust = floor > 0 and liq is not None and float(liq) < floor
    except (TypeError, ValueError):
        dust = False
    # OUR OWN VIRTUAL DEPTH CANNOT CONDEMN A REAL PRICE (F6 recheck, 2026-09-23). When the provider
    # returns no pair, `_fill_depth_from_pool_store` substitutes the virtual local liquidity
    # 2*L*sqrt(P) of our cached pool state -- a median 0.27 of the provider's TVL where both
    # existed, and orders of magnitude ABOVE the actual value on a narrow concentrated position.
    # Reading that as a dust pair NULLS price, market cap and fdv, and the price gates then fail
    # open to the source fill. That decision is not taken on a number this system computed about a
    # pool it has not measured: the reading is recorded under its own key and the gates keep their
    # prices. Tightening -- the gates stay armed rather than failing open.
    if dust and str(st.get("liquidity_source") or "") == _pool_depth_source():
        st["dust_pair_liquidity_unverified_usd"] = liq
        return st
    if dust:
        st["dust_pair_liquidity_usd"] = liq
        for k in ("price", "market_cap", "fdv"):
            st[k] = None
    return st


def _desk_entry_refusal(conn, *, mint, network_id, size_usd, size_pct_equity, override,
                        desk_ticket_id, desk_rails=None, desk_metadata=None):
    """The runner re-checks a desk intent with ITS settings (the queue's producer may hold an older
    snapshot), then the desk book. Local reads only: no provider call before the entry hook."""
    from sniper.services.memecoin_origin import desk_lane_open, crowd_admission_refusal
    if not desk_lane_open(desk_metadata):
        return "desk_disabled"
    if not desk_ticket_id:
        return "desk_ticket_missing"
    from sniper.util.clock import real_utc_now_iso
    refusal = crowd_admission_refusal(desk_metadata, mint=mint,
                                     now_iso=real_utc_now_iso(), now_host=time.time())
    if refusal:
        return refusal
    from sniper.services.memecoin_known_group_canary import _admission_refusal
    refusal = _admission_refusal(conn, desk_metadata, mint=mint, now_iso=real_utc_now_iso())
    if refusal:
        return refusal
    if m.normalized_coin_network(network_id, mint) not in cfg.MEMECOIN_DESK_CHAINS:
        return "desk_chain_not_allowed"
    if override or size_pct_equity is not None:
        return "desk_intent_malformed"
    try:
        size = float(size_usd)
    except (TypeError, ValueError):
        return "desk_intent_malformed"
    if not (math.isfinite(size) and 0 < size <= float(cfg.MEMECOIN_DESK_CLIP_USD or 0.0)):
        return "desk_clip_exceeded"
    # The runner's CURRENT bounds, not the producer's: a direct call, or rails queued before the
    # operator tightened the bounds, must not reach the contract (Codex review).
    from sniper.services.memecoin_manual_intents import validate_desk_rails
    ok, why = validate_desk_rails(desk_rails, cfg.MEMECOIN_DESK_RAIL_BOUNDS)
    if not ok:
        return f"desk_rails_invalid: {why}"
    from sniper.services.memecoin_origin import desk_book_refusal
    from sniper.util.clock import real_utc_now_iso
    return desk_book_refusal(conn, now_iso=real_utc_now_iso(), desk_metadata=desk_metadata)


def make_real_buy_fn(contexts, stats_fn=None, blind_mcap_fn=None):
    """The operator's manual real entry — the 'I like this watch-only signal, enter it' path
    (spec §6). Synthesizes a fresh BUY event and runs the FULL gate chain via real_entry_hook
    (kill switch -> risk gate/reservation with the same fraction clip -> token safety ->
    executor). The position keeps the source trader for attribution. Its manual policy has no
    automatic exits. The per-wallet allowlist is deliberately NOT checked here — manual entry on
    a watch-only wallet is the whole point."""
    from sniper.util.clock import real_utc_now_iso

    class _ManualEv:
        side = "buy"
        signal_source = "manual"
        source_handle = None
        their_size_usd = 0.0
        their_price = None
        their_market_cap = None
        fomo_event_id = None
        override = False

        def __init__(self, mint, uid, ticker, network_id, origin_event_id=None,
                     signal_source="manual"):
            # An INSTANCE attribute, never a mutation of the class one: buy_fn may run on more
            # than one thread (spec desk-execution-plumbing §3 item 3).
            self.signal_source = signal_source
            self.mint = mint
            # Optional source attribution. Manual entries never mirror a source trader's sell.
            self.source_user_id = uid or None
            self.ticker = ticker
            self.network_id = network_id
            self.fomo_event_id = origin_event_id or None
            observed_at = real_utc_now_iso()
            self.created_at = observed_at   # fresh by construction -> staleness gate passes
            self.received_at = observed_at
            self.received_at_normalized = observed_at
            self.clock_domain = "observed_real_utc"

    def _manual_classification_context(mint, network_id):
        # A hook call without classification_context reserves WITHOUT a funded contract, and
        # the post-cutover reconcile gate then refuses every armed startup (YAMINE 07-19 →
        # 26-min funded outage on the 07-22 reboot). Resolve mcap the same way the automated
        # hook does. An unresolved value keeps the same assumed-micro convention.
        # Keep `price` for the EVM premium safety gate. The manual guards still bypass chase policy.
        chain = m.normalized_coin_network(network_id, mint)
        st = {}
        try:
            sf = stats_fn
            if sf is None:
                from sniper.execution.solana_quote import dexscreener_stats
                sf = dexscreener_stats
            try:
                st = sf(mint, network_id=chain) or {}
            except TypeError:
                st = sf(mint) or {}
        except Exception:
            st = {}
        # BAND SOURCE PIN (2026-08-30) -- see the funded path. Same rule here so a manual
        # entry and an automatic one on the same coin cannot band differently.
        _blind_chain = not dex_mcap_allowed(chain)
        eff_mc = None if _blind_chain else (_snapshot_priced(st).get("market_cap") or None)
        if not eff_mc and _blind_chain:
            try:
                bf = blind_mcap_fn if blind_mcap_fn is not None else _default_blind_mcap_fn()
                eff_mc = bf(chain, mint) or None
            except Exception:
                eff_mc = None
        return {
            "effective_mcap": eff_mc,
            "assumed_micro": eff_mc is None,
            "consensus_n_60m": None,
            "market_stats": st,
        }

    cluster_client = (
        ClusterGateClient(cfg.MEMECOIN_CLUSTER_GATE_SOCKET)
        if cfg.MEMECOIN_REAL_CLUSTER_GATE_ENABLED else None
    )

    def buy_fn(conn, mint, source_user_id=None, ticker=None, network_id=None, size_usd=None,
               size_pct_equity=None, origin_event_id=None, override=False,
               signal_source="manual", desk_ticket_id=None, desk_rails=None, desk_metadata=None):
        if signal_source == "desk":
            refusal = _desk_entry_refusal(conn, mint=mint, network_id=network_id, size_usd=size_usd,
                                          size_pct_equity=size_pct_equity, override=override,
                                          desk_ticket_id=desk_ticket_id, desk_rails=desk_rails,
                                          desk_metadata=desk_metadata)
            if refusal:
                return {"status": "rejected", "reason": refusal}
            source_user_id = None   # the desk has no wallet; never borrow one for attribution
        elif signal_source != "manual":
            return {"status": "rejected", "reason": f"unknown intent origin {signal_source!r}"}
        ev = _ManualEv(mint, source_user_id, ticker, network_id, origin_event_id,
                       signal_source=signal_source)
        ev.override = bool(override)
        # The seat's rails ride to the reservation, where they are frozen into the funded contract
        # BEFORE any signing (Codex audit #1): every booking path, recovery included, reads them.
        ev.desk = ({"ticket_id": desk_ticket_id, "rails": dict(desk_rails or {}),
                    **dict(desk_metadata or {})}
                   if signal_source == "desk" else None)
        try:
            if size_usd is not None and size_pct_equity is not None:
                raise ValueError("a manual buy must have one size input")
            if size_pct_equity is not None:
                from sniper.services.memecoin_risk import manual_percent_size, snapshot_funding_values
                chain = m.normalized_coin_network(network_id, mint)
                venue = context_for(contexts, chain)
                if venue is None:
                    return {"status": "rejected", "reason": f"no funded venue for {chain}"}
                balance_read_started = time.monotonic()
                quote_balance = venue["usdc_fn"]()
                if quote_balance is None:
                    return {"status": "rejected", "reason": "quote balance unavailable"}
                snapshot_fn = venue.get("slot_snapshot_debits_fn") if chain == "solana" else None
                sizing_transaction = snapshot_fn is not None and not conn.in_transaction
                if sizing_transaction:
                    conn.execute("BEGIN")
                try:
                    # Pin the ledger snapshot before reading retained IDs. The callback can see
                    # later dispatches; those fail closed if absent from this snapshot.
                    snap = m.real_open_snapshot(conn, network=chain)
                    if snapshot_fn is not None:
                        if (isinstance(quote_balance, bool)
                                or not isinstance(quote_balance, (int, float))
                                or not math.isfinite(quote_balance) or quote_balance < 0):
                            raise ValueError("invalid manual quote balance")
                        debit_ids, _ = snapshot_funding_values(snapshot_fn(balance_read_started))
                        debit = m.settled_snapshot_usdc_debit(
                            conn, chain, snapshot_debits=debit_ids)
                        quote_balance = max(0.0, float(quote_balance) - debit)
                    _, pending = m.pending_totals(conn, network=chain)
                    clip = manual_percent_size(
                        quote_balance=quote_balance,
                        # MARK for sizing, COST for exposure headroom (2026-09-06).
                        open_notional_usd=snap["mark_notional"],
                        pending_notional_usd=pending,
                        global_exposure_usd=m.funded_book_cost_snapshot(conn)["notional"],
                        max_total_exposure_usd=cfg.MEMECOIN_MAX_TOTAL_EXPOSURE_USD,
                        pct=float(size_pct_equity),
                    )
                finally:
                    if sizing_transaction:
                        conn.rollback()
                clip, capacity_floor = reduce_clip_for_route_capacity(chain, mint, clip, venue)
                if capacity_floor is not None:
                    return {
                        "status": "rejected",
                        "reason": "route capacity is below the trade floor",
                        "resolved_size_usd": capacity_floor,
                    }
            else:
                clip = (float(size_usd) if size_usd is not None
                        else cfg.MEMECOIN_MANUAL_TRADE_SIZE_USD)
        except (TypeError, ValueError):
            return {"status": "rejected", "reason": "invalid manual size"}
        # OPERATOR OVERRIDE CAP (2026-09-22, operator order): "these entries need to be capped at
        # 175 even if the [size] is more than that". Enforced HERE, after clip is resolved from
        # EITHER size input, so neither path can exceed it -- server-side, unconditional, never
        # read from the request. The operator can move the number by changing the setting; the
        # request itself cannot.
        if ev.override:
            override_cap = float(getattr(cfg, "MEMECOIN_MANUAL_OVERRIDE_MAX_USD", 175.0) or 0.0)
            if override_cap > 0:
                clip = min(clip, override_cap)
        classification_context = _manual_classification_context(mint, network_id)
        impact_cap = cfg.entry_impact_cap_for_mcap(
            1.0 if classification_context["assumed_micro"]
            else classification_context["effective_mcap"])
        # A MANUAL BUY IS A FUNDED ENTRY DECISION (2026-09-08). This path calls `real_entry_hook`
        # directly instead of going through `make_real_hook`'s `_attempt`, so it did not register
        # itself and an entry-knob apply could land in the MIDDLE of it — reading one threshold
        # from before the operator's change and another from after. The copy path has been guarded
        # since the knob overlay shipped; this one was missed because it is a second call site for
        # the same decision.
        from sniper.services.memecoin_entry_knobs import entry_decision
        with entry_decision():
            result = real_entry_hook(
                conn, ev, contexts, now_fn=real_utc_now_iso, trade_size=clip,
                max_age_s=cfg.MEMECOIN_REAL_ENTRY_MAX_AGE_S,
                cluster_decide_fn=(cluster_client.decide if cluster_client else None),
                impact_cap_pct=impact_cap,
                classification_context=classification_context,
            ) or {}
        return {**result, "resolved_size_usd": clip}
    return buy_fn


def make_real_mark_fn(contexts):
    """A mark_fn(positions) -> {(network_id, norm_mint): price} for unpriced real positions —
    Robinhood 4663 is 100% missing_pair on DexScreener, so the accounting endpoint marked those
    bags at COST BASIS and overstated equity (M1). Uses each position's OWN venue preview_exit_fn
    (a read-only 0x sell quote of the exact held balance) to derive an EXECUTABLE mark:
    price = expected_usdc / remaining open quantity — the SAME exact unit price the exit sweep
    uses, so displayed equity stays correct after partial exits. Best-effort:
    a failed quote is simply omitted (the position stays counted in unquotable_n, never fake-0)."""
    def mark_fn(positions):
        out = {}
        for pos in positions or []:
            ctx = context_for_position(contexts, pos)
            if ctx is None:
                continue
            pv_fn = ctx.get("preview_exit_fn")
            open_qty = pos.get("open_qty") or 0.0
            if pv_fn is None or open_qty <= 0:
                continue
            kept = False
            try:
                _mk_preview = pv_fn(pos)
                _keep_route_quote(pos, _mk_preview, "equity_mark", attempted=True)
                kept = True
                exp = _preview_value(_mk_preview, "expected_usdc")
                exp = float(exp)
            except (TypeError, ValueError):
                if not kept:
                    _keep_route_quote(pos, None, "equity_mark", attempted=True)
                continue
            except Exception:
                if not kept:
                    _keep_route_quote(pos, None, "equity_mark", attempted=True)
                logger.exception("real mark preview failed pos=%s mint=%s",
                                 pos.get("id"), pos.get("mint"))
                continue
            if math.isfinite(exp) and exp > 0:
                out[m.accounting_mark_key(pos.get("network_id"), pos["mint"])] = (
                    exp / open_qty
                )
        return out
    return mark_fn


def check_wallet_demotion(conn, source_user_id, network_id=None, alert_fn=None):
    """Pre-registered auto-demote guardrail (spec §4), scoped to ONE chain (2026-07-30): after a
    real close, revoke the wallet's authority ON THAT CHAIN when its real realized there is
    <= -MEMECOIN_REAL_DEMOTE_LOSS_USD or its last MEMECOIN_REAL_DEMOTE_CONSEC closes there are all
    losses. One alert per demotion; the paper copy + Telegram watch continue — only real ENTRIES on
    that chain stop. Returns True iff demoted.

    Revoking globally would reintroduce chain pooling in the one place that moves real money: a
    wallet that fails on 4663 would be silently disarmed on solana too. When the demoted chain was
    the wallet's LAST armed chain, `real_approved` is cleared as well so the roster stays truthful.

    Both inputs are scoped to the wallet's CURRENT funded cohort (`wallet_real_cohort_start`), so a
    demoted-then-re-approved wallet is judged on what it does next, not on the losses it was already
    revoked for. A caller that cannot name a chain never demotes."""
    chain = str(network_id or "").strip().lower()
    if not source_user_id or not chain:
        return False
    if source_user_id not in m.real_approved_user_ids(conn, network_id=chain):
        return False
    rec = m.wallet_real_record(conn, source_user_id, network_id=chain)
    loss_hit = (cfg.MEMECOIN_REAL_DEMOTE_LOSS_USD > 0
                and rec["realized_total"] <= -cfg.MEMECOIN_REAL_DEMOTE_LOSS_USD)
    consec_hit = (cfg.MEMECOIN_REAL_DEMOTE_CONSEC > 0
                  and rec["consecutive_losses"] >= cfg.MEMECOIN_REAL_DEMOTE_CONSEC)
    if not (loss_hit or consec_hit):
        return False
    reason = (f"{chain}: cohort realized ${rec['realized_total']:+.2f} over "
              f"{rec['closed_count']} closes / {rec['consecutive_losses']} consecutive losses")
    # One transactional chain revoke. It also anchors a NEW cohort for this chain, so a
    # later re-arm is judged on what the wallet does next rather than re-firing on the
    # losses it was already revoked for. Clearing the last chain clears the flag too.
    m.set_wallet_chain_armed(conn, source_user_id, chain, False,
                             actor="auto_demote", reason=reason)
    row = conn.execute("SELECT handle FROM memecoin_wallets WHERE source_user_id=?",
                       (source_user_id,)).fetchone()
    handle = (row[0] if row and row[0] else source_user_id)
    _safe_alert(alert_fn or alerts.alert_real_demote, f"{handle} ({chain})",
                rec["realized_total"], rec["consecutive_losses"])
    return True


def _m5_txn_count(st):
    """buys + sells in the last 5 minutes, or None when the provider gave neither.

    The two keys arrive from the same payload and co-occur on 98% of Solana snapshots, so a
    present side with an absent sibling is a genuine zero rather than missing data — summing with
    the absent side as 0 is right, and returning None only when BOTH are absent keeps "no data"
    distinct from "no trades". Those are different facts and the gate treats them differently.
    """
    if not isinstance(st, dict):
        return None
    buys, sells = st.get("txns_m5_buys"), st.get("txns_m5_sells")
    if buys is None and sells is None:
        return None
    total = 0
    for part in (buys, sells):
        if part is None:
            continue
        try:
            total += int(float(part))
        except (TypeError, ValueError):
            return None
    return total


def subtraction_provenance(volume, signal_usd, *, source_age_s=None, index_lag_s=0.0):
    """Why the source-buy subtraction was or was not applied, for the gate record.

    278 4663 refusals carried no trace of this decision, which is why an arithmetic error on a
    number the provider could not hold stayed invisible until PERSON.
    """
    try:
        sig = max(0.0, float(signal_usd or 0.0))
    except (TypeError, ValueError):
        sig = 0.0
    out = {"source_age_s": (None if source_age_s is None else float(source_age_s)),
           "index_lag_s": float(index_lag_s or 0.0), "signal_usd": sig,
           "subtraction_applied": False, "reason": None}
    if volume is None:
        out["reason"] = "no_volume"
        return out
    v = float(volume)
    # THE PROVIDER CANNOT CONTAIN WHAT IT HAS NOT INDEXED (2026-09-16, operator). On a chain with a
    # measured index lag, a buy younger than that lag is not in the window we just read. A NEGATIVE
    # age is the same case: the feed timestamps a third of 4663 buys AFTER our own decision.
    if out["index_lag_s"] > 0 and source_age_s is not None and float(source_age_s) < out["index_lag_s"]:
        out["reason"] = "source_buy_younger_than_provider_index_lag"
        return out
    if v < sig:
        # A volume below the buy provably excludes it (2026-09-12, WMR).
        out["reason"] = "volume_below_source_buy"
        return out
    out["subtraction_applied"] = True
    out["reason"] = "applied"
    return out


def dead_coin_decision(age_s, organic_h1_usd, *, min_age_s, max_h1_usd):
    """(dead, reason) for the dead-coin gate. Pure. True only when BOTH are known and the coin is at
    least `min_age_s` old with organic hourly volume under `max_h1_usd`; None when either input is
    unknown (age_unknown / h1_unknown: never a refusal); False otherwise (young / active), or "off"
    when either knob is not positive."""
    try:
        min_age, max_h1 = float(min_age_s or 0.0), float(max_h1_usd or 0.0)
    except (TypeError, ValueError):
        return False, "off"
    if not (min_age > 0 and max_h1 > 0):
        return False, "off"
    try:
        age = None if age_s is None else float(age_s)
        h1 = None if organic_h1_usd is None else float(organic_h1_usd)
    except (TypeError, ValueError):
        return None, "age_unknown"
    if age is None or not math.isfinite(age):
        return None, "age_unknown"
    if h1 is None or not math.isfinite(h1):
        return None, "h1_unknown"
    if age < min_age:
        return False, "young"
    if h1 >= max_h1:
        return False, "active"
    return True, "dead_coin"


def organic_volume(volume, signal_usd, *, source_age_s=None, index_lag_s=0.0):
    """Provider volume minus the copied trader's own buy, when the provider can contain that buy.

    ONLY WHEN IT CAN (2026-09-12, operator: "do all three"). We read 1-2 s after the source buy and
    DexScreener's 5-minute window runs 1-2 minutes behind on 4663, so the buy is usually NOT in the
    number yet. A volume below the buy provably excludes it; subtracting there counted the trader
    against himself twice. WMR (4663, 22:59Z): provider $64, buy $994 -> refused as "$0 organic".
    At or above the buy the subtraction stays (the accepted conservative bias of 2026-08-26).
    """
    if volume is None:
        return None
    v = float(volume)
    provenance = subtraction_provenance(volume, signal_usd, source_age_s=source_age_s,
                                        index_lag_s=index_lag_s)
    if not provenance["subtraction_applied"]:
        return v
    return max(0.0, v - provenance["signal_usd"])


def _volume_notification_evidence(*, volume_m5, signal_usd, mcap, effective_ceiling, reason, need,
                                  floor_lower_bound=None, source_age_s=None, index_lag_s=0.0):
    """Record the tested volume, including source age and the on-chain lower bound.

    Preserve the legacy organic field. It omits source age and provider index lag.
    The decided field and tested field use the final gate input.
    """
    raw = float(volume_m5) if volume_m5 is not None else None
    organic = organic_volume(raw, signal_usd)
    decided_organic = organic_volume(raw, signal_usd, source_age_s=source_age_s,
                                     index_lag_s=index_lag_s)
    ceiling = reason == "m5_turnover_over_ceiling"
    m5 = reason.startswith("m5_turnover") or reason in {
        "m5_volume_unreadable", "m5_volume_under_abs_floor"}
    try:
        floor_lower_bound = float(floor_lower_bound)
        if not math.isfinite(floor_lower_bound) or floor_lower_bound < 0:
            floor_lower_bound = None
    except (TypeError, ValueError):
        floor_lower_bound = None
    bound_used = (m5 and not ceiling and floor_lower_bound is not None
                  and float(floor_lower_bound) > (organic or 0.0)
                  and (organic is not None or (need is not None and float(floor_lower_bound) >= need)))
    if bound_used:
        organic = float(floor_lower_bound)
    bound_used = (m5 and not ceiling and floor_lower_bound is not None
                  and floor_lower_bound > (decided_organic or 0.0)
                  and (decided_organic is not None
                       or (need is not None and floor_lower_bound >= need)))
    if bound_used:
        decided_organic = floor_lower_bound
    tested = (raw if ceiling else decided_organic) if m5 else None
    return {"effective_ceiling": effective_ceiling, "organic_volume_m5": organic,
            "decided_organic_volume_m5": decided_organic,
            "tested_volume_m5": tested, "threshold": need if m5 else None,
            "effective_ratio": tested / mcap if tested is not None and mcap else None,
            "units": "USD", "measurement_basis": "raw_volume_m5" if ceiling else
            "onchain_v4_lower_bound" if bound_used else
            "source_adjusted_volume_m5" if m5 else "not_m5_decision"}


def _volume_gate_inputs(st, ev, why, need, effective_ratio_floor=None, *,
                        source_age_s=None, index_lag_s=0.0, provider=None, snapshot_at=None,
                        effective_h1_abs_floor=None, effective_h1_ratio_floor=None):
    """The exact inputs this refusal was decided on, as compact JSON, for LATER GRADING.

    Every threshold argument any of us had on 2026-08-30 ran aground on samples of 9 to 30,
    because a gate that refuses before it quotes leaves nothing behind. Reconstructing the inputs
    afterwards from `memecoin_entry_snapshots` is possible but lossy: one mint carries up to 854
    snapshots and the nearest-in-time join still moved a measured refusal rate from 61.7% to
    25.4%. Storing what the gate ACTUALLY read removes that error entirely.

    Write-only, best-effort, never raises into a funded decision. It records the refusal that
    already happened; it cannot cause one.
    """
    import json as _json
    st = st if isinstance(st, dict) else {}
    try:
        return _json.dumps({
            "why": why,
            "mcap": st.get("market_cap"),
            "provider_mcap": st.get("provider_market_cap"),
            "onchain_m5": st.get("onchain_m5"),
            "volume_m5": st.get("volume_m5"),
            "volume_h1": st.get("volume_h1"),
            "txns_m5_buys": st.get("txns_m5_buys"),
            "txns_m5_sells": st.get("txns_m5_sells"),
            "txns_m5_total": _m5_txn_count(st),
            "liquidity": st.get("liquidity"),
            "their_size_usd": (float(getattr(ev, "their_size_usd", 0) or 0) or None),
            "required_usd": (float(need) if isinstance(need, (int, float)) else None),
            "txns_floor": int(getattr(cfg, "MEMECOIN_REAL_MIN_TXNS_M5", 0) or 0),
            # THE FLOOR THAT DECIDED, not the one that happens to be a module constant
            # (2026-09-09). This recorded MEMECOIN_REAL_MIN_TURNOVER_M5 unconditionally — the
            # OVER-BAND leg, 0.005 — while `_volume_active` is true only above
            # MEMECOIN_REAL_STRICT_VOLUME_MIN_MCAP_USD (500,000), so every nano/micro/small coin
            # is judged by the IN-BAND leg instead. ZOGCASH and 589 were both refused against
            # 0.1000 and both recorded `ratio_floor: 0.005`, a 20x discrepancy, on the field any
            # study of this gate would read. `ratio_floor_over_band` already carries the other leg,
            # so nothing is lost by making this one honest.
            "ratio_floor": float(
                effective_ratio_floor if effective_ratio_floor is not None
                else (getattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_M5", 0) or 0)),
            "h1_ratio_floor": float(cfg.MEMECOIN_REAL_MIN_TURNOVER_H1 if effective_h1_ratio_floor is None
                                    else effective_h1_ratio_floor),
            "h1_abs_floor": float(cfg.MEMECOIN_REAL_MIN_VOLUME_H1_USD if effective_h1_abs_floor is None
                                  else effective_h1_abs_floor),
            "ratio_floor_over_band": float(
                getattr(cfg, "MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND", 0) or 0),
            "band_max_mcap": float(
                getattr(cfg, "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD", 0) or 0),
        }, separators=(",", ":"))
    except Exception:
        logger.exception("gate-input capture failed (refusal unchanged)")
        return None


def volume_alive_decision(vol_h1, mcap, *, abs_floor, turnover_floor,
                          vol_m5=None, m5_floor=0.0, m5_ceiling=0.0, m5_band_max_mcap=0.0,
                          m5_ceiling_max_mcap=0.0, m5_floor_over_band=0.0, m5_required=False,
                          signal_usd=0.0, m5_txns=None, m5_txns_floor=0,
                          m5_txns_required=False, source_age_s=None, index_lag_s=0.0,
                          m5_floor_lower_bound=None, h1_floor_lower_bound=None,
                          m5_abs_floor_usd=0.0, m5_floor_undecidable=False, pair_age_s=None,
                          m5_txns_lower_bound=None):
    """Alive-token MINIMUM (pure). The bar scales with mcap: a flat dollar floor blocks the
    wrong side twice — it admits a dead $10M coin at $50k/h and refuses an alive $50k coin at
    $5k/h. Measured 2026-08-19 (n=493 entries with 60s tape response): the response is monotone
    in volume_h1/mcap — under 0.3%/h turnover only 5% of entries popped +5% and 49% of ticks
    were dead; above 3.6%/h, 39-49% popped.

    The M5 leg is the "moving NOW" check (operator, Momota postmortem): h1 aggregates are
    backward-looking — a −63% crash an hour ago left 59%/h h1 turnover on a coin that was flat
    at entry. `vol_m5 >= mcap * m5_floor` reads the last 5 minutes instead, and it runs FIRST
    because the operator ranks it above the h1 leg. Its evidence is directional, not a proven
    dead zone (our history rarely entered flat coins — n=6 in that cell): m5 turnover >10%/h
    popped +5% on 48% of entries vs 24-29% below.

      - a missing field fails OPEN for its own leg only (chase-guard convention)
      - mcap known -> the RELATIVE bars decide (m5 first, then h1)
      - mcap unknown -> the flat abs_floor on vol_h1 decides (the old revival guard)
    OUR TAPE WHERE THE PROVIDER IS SILENT (E2 step 1, 2026-10-08). `m5_floor_lower_bound`,
    `h1_floor_lower_bound` and `m5_txns_lower_bound` come from our own tape with the source's
    transaction excluded: LOWER bounds. With the provider's number absent, a bound proves a floor it
    reaches and nothing else; such a pass says `own_tape_proven` instead of `no_volume_data`. A
    floor it misses stays unknown and keeps today's posture. It never decides the ceiling. The
    label claims the tape only when EVERY active floor leg was proven by the tape or answered by the
    provider (Gamma on 6e08fb97a); one unknown active leg keeps `no_volume_data`.
    Returns (allow, required_usd_or_None, reason)."""
    legs = {"tape": 0, "unknown": 0}

    def _quiet_label():
        return "own_tape_proven" if legs["tape"] and not legs["unknown"] else "no_volume_data"
    try:
        mc = float(mcap) if mcap else None
    except (TypeError, ValueError):
        mc = None
    # SIGNAL-VOLUME EXCLUSION (2026-08-26, operator): the copied wallet's own buy is part of the
    # observed volume, so on a small coin the signal can BE the volume that passes the floor — a
    # $2.5k buy on a $20k coin satisfies a 10% floor with zero organic flow behind it. The FLOOR
    # legs therefore judge volume MINUS the signal's own size: we need evidence of a market beyond
    # the one trade we are reacting to. The CEILING keeps the RAW number — float-churn is churn
    # whoever traded it, and subtracting from a safety bound would loosen it.
    # Known bias, accepted: if the aggregator has not yet indexed the trader's buy when we read
    # (we react within seconds), subtracting it double-counts and the floor runs STRICTER than
    # intended. That errs toward refusing marginal coins, which is the chosen direction.
    try:
        _sig = max(0.0, float(signal_usd or 0.0))
    except (TypeError, ValueError):
        _sig = 0.0
    m5_organic = organic_volume(vol_m5, _sig, source_age_s=source_age_s,
                                index_lag_s=index_lag_s)
    try:
        bound = float(m5_floor_lower_bound)
        if not math.isfinite(bound) or bound < 0:
            bound = None
    except (TypeError, ValueError):
        bound = None
    if bound is not None and m5_organic is not None:
        m5_organic = max(m5_organic, bound)
    # M5 TRADE-COUNT LEG (2026-08-30, operator-approved). Runs FIRST and, unlike every other leg
    # here, OUTSIDE the `if mc and mc > 0` block — a count needs no market cap. That is the point:
    # 47.8% of attempts reaching the market-data stage have no resolvable mcap, so the ratio legs
    # below are structurally inert on nearly half the flow and this leg is not.
    #
    # It asks the operator's question — is anyone OTHER than the trader we are copying active —
    # without the subtraction his literal phrasing needed. `volume_m5 - their_size_usd` is not
    # computable: 34.7% of snapshots carry volume_m5 BELOW their_size_usd because the aggregator
    # has not indexed their fill yet, so the subtraction goes negative for a timing reason rather
    # than a liquidity one. A count is immune to that.
    #
    # A count is NOT a dollar amount. The middle return slot is the USD bar the caller prints, so
    # this leg returns None there rather than smuggling a count through a money field.
    try:
        _txn_floor = int(m5_txns_floor or 0)
    except (TypeError, ValueError):
        _txn_floor = 0
    if _txn_floor > 0:
        if m5_txns is None:
            try:
                _txn_bound = int(m5_txns_lower_bound)
            except (TypeError, ValueError):
                _txn_bound = None
            if _txn_bound is not None and _txn_bound >= _txn_floor:
                legs["tape"] += 1               # at least this many trades, so the floor holds
            else:
                legs["unknown"] += 1
                if m5_txns_required:
                    return False, None, "m5_txns_unreadable"
        else:
            try:
                _txns = int(m5_txns)
            except (TypeError, ValueError):
                _txns = None
            if _txns is None:
                legs["unknown"] += 1
                if m5_txns_required:
                    return False, None, "m5_txns_unreadable"
            elif _txns < _txn_floor:
                return False, None, "m5_txns_under_floor"
    # M5 DOLLAR FLOOR (2026-09-18, operator). A THIN market, never an ABSENT one — see
    # cfg.micro_min_volume_m5_usd_for for the measurement. The caller scopes it to the micro band
    # and to the chains with evidence, so this leg only has to express the shape:
    #
    #   organic is None  -> unknown. This leg is silent; the ratio legs keep their own posture.
    #   organic == 0     -> the only five-minute volume was the buy we copy. A market that has not
    #                       started is not a market that stopped, and that cell is our second-best
    #                       (33% fast take-profits). Admitted.
    #   0 < organic < bar-> a real tape, too thin to move inside two minutes. Refused.
    #
    # It runs with the other m5 legs, before the ceiling, in the order the operator fixed.
    try:
        _m5_abs = float(m5_abs_floor_usd or 0.0)
    except (TypeError, ValueError):
        _m5_abs = 0.0
    # AN UNDECIDABLE FLOOR IS ONE LEG, NOT THE VERDICT (Codex review 2026-09-23, A1). The first
    # version returned an overall allow here, which skipped the hourly floor and the ceiling that
    # have their own inputs. Now the undecidable m5 leg is REMEMBERED and every later leg still
    # runs; only a full pass is relabelled `onchain_unpriced_undecidable`.
    undecidable = None

    def _ok(bar, reason):
        return (True, undecidable[0], undecidable[1]) if undecidable else (True, bar, reason)

    if _m5_abs > 0 and m5_organic is None:
        legs["tape" if bound is not None and bound >= _m5_abs else "unknown"] += 1
    if _m5_abs > 0 and m5_organic is not None and 0 < float(m5_organic) < _m5_abs:
        if not m5_floor_undecidable:
            return False, _m5_abs, "m5_volume_under_abs_floor"
        undecidable = (_m5_abs, "onchain_unpriced_undecidable")
    h1_organic = organic_volume(vol_h1, _sig, source_age_s=source_age_s,
                                index_lag_s=index_lag_s)
    # SOLANA HOURLY FLOOR, S1 (operator spec 2026-09-17-solana-hourly-floor-activity-design). Prior
    # feed buys are a partial DOLLAR estimate: the hourly legs read max(provider organic, feed), never
    # the sum, and no m5 leg or ceiling reads it. A missing provider value stays missing.
    try:
        h1_bound = float(h1_floor_lower_bound)
        if not math.isfinite(h1_bound) or h1_bound < 0:
            h1_bound = None
    except (TypeError, ValueError):
        h1_bound = None
    if h1_bound is not None and h1_organic is not None:
        h1_organic = max(h1_organic, h1_bound)
    if mc and mc > 0:
        # The m5 BAND applies only inside the mcap range it was FITTED on. Measured population:
        # p10 $9,936, median $31,432, p90 $153,663, and ONE row of 62 above $250k. With the mcap
        # filter now off at both ends, an unscoped band would extrapolate a threshold derived from
        # ~$31k coins onto coins of any size — on a $5M coin, 0.10 turnover is $500k of five-minute
        # volume and 1.50 is $7.5M, which is not the regime anything here was measured in.
        # Above the bound the band is SILENT (fails open), it does not switch to some other rule.
        band_ok = (not m5_band_max_mcap) or float(m5_band_max_mcap) <= 0 \
            or mc <= float(m5_band_max_mcap)
        # OVER-BAND FLOOR (2026-08-26, operator). "Silent above the band" left big coins with NO
        # moving-NOW check at all: the h1 leg is backward-looking, so a coin that crashed dead an
        # hour ago still carried enough h1 to pass. All three of today's manual-close giants did
        # exactly that (mcap $9.3M/$2.3M/$1.1M: h1 passed 1%/h, m5 sat at 0.2%/0.05%/0.3%).
        # The fitted 10% cannot extrapolate up, so the over-band leg uses ITS OWN reduced ratio.
        # Measured on every funded big-cap close carrying m5: under 1% turnover n=3 all losses;
        # 1%+ n=8, +$41.6. Thin, but zero measured winners below the bar. Distinct reason code so
        # the revival watch (in-band, <=100k scope) and refusal capture keep their exact-string
        # semantics.
        eff_floor = float(m5_floor or 0.0) if band_ok else float(m5_floor_over_band or 0.0)
        floor_reason = "m5_turnover_under_floor" if band_ok else "m5_turnover_under_big_floor"
        if eff_floor > 0:
            # A partial lower bound can prove a floor, but cannot prove insufficient total volume.
            if m5_organic is None and bound is not None and bound >= mc * eff_floor:
                m5_organic = bound
                legs["tape"] += 1
            if m5_organic is None:
                legs["unknown"] += 1
                # MISSING M5 FAIL-CLOSED (2026-08-26, operator; same posture as the mcap gate).
                # 44693 entered a floor-scoped coin with m5 unreadable because the leg failed open.
                # Opt-in via m5_required so the pure function's default behaviour is unchanged.
                if m5_required:
                    return False, mc * eff_floor, "m5_volume_unreadable"
            elif m5_organic < mc * eff_floor:
                # THE FLOOR IS UNDECIDABLE, NOT FAILED (2026-09-22, GIGABYTE ref 51a1a1b6). Our own
                # lane saw swaps in this coin's pool and could not price ANY of them, so the only
                # number left is the provider's — and that provider is measured to undercount: on
                # 2026-09-16 it reported ONE of six swaps, $2,394.60 against a chain truth of
                # $3,450. Refusing a FLOOR on that is treating an unknown as a zero, which rule 1
                # forbids. The ceiling and the zero-swap case are untouched: a ceiling refusal on
                # an under-measurement is conservative, and no swaps at all is a real observation.
                if not m5_floor_undecidable:
                    return False, mc * eff_floor, floor_reason
                undecidable = undecidable or (mc * eff_floor, "onchain_unpriced_undecidable")
        # M5 CEILING (2026-08-25). Both bounds are required and NEITHER WORKS ALONE. Measured over
        # 62 funded closes carrying m5 volume: floor 0.10 alone keeps 48 for +$0.63, ceiling 1.50
        # alone keeps 56 for +$6.54, and the two TOGETHER keep 42 for +$57.66 while refusing 20 that
        # lost -$108.14. The band is positive in both time-halves (+$52.05, +$5.61) where each bound
        # alone turns negative in the second half.
        # THE UPPER END IS A SMALL-CAP FLOAT-CHURN CHECK, NOT A "TOO MUCH VOLUME" RULE (operator,
        # corrected 2026-08-25). An earlier version of this comment said it "loses at EVERY coin
        # size, not only small ones". THAT IS WRONG on the measured data. Splitting the 6
        # over-ceiling closes at $150k:
        #     under 150k   n=4   0% win   avg -51.8%   (-70.8, -50.6, -44.9, -41.0)
        #     150k and up  n=2  50% win   avg +15.9%   (TRASHY -3.9, Nibbles +35.7)
        # Every breach that actually hurt was a micro-cap. The mechanism is float turnover, not
        # activity: on a $10k coin, trading 2-6x the whole market cap in five minutes means the
        # float is being dumped, which is the rug signature. On a $455k coin 1.86x is a busy day.
        # Do NOT read a high ratio on a larger coin as the same event.
        #
        # 2026-08-25 (operator): the ceiling was given its OWN bound, because it and the floor stop
        # being true at different sizes - see MEMECOIN_REAL_TURNOVER_CEILING_MAX_MCAP_USD. It used
        # to share the band bound; at 250k it refused 5 closes and ALL 5 were losses, with the one
        # profitable breach (Nibbles, $455k) already exempt. n=4 and n=2, so the direction is stark
        # and the sample is thin - revisit if either cell grows.
        # The ceiling may carry its OWN mcap bound (m5_ceiling_max_mcap). Unset, it falls back to
        # the shared band bound, which is the behaviour before that knob existed. The floor never
        # reads this value: the two legs stop being true at different sizes, so scoping the ceiling
        # must not silence the floor with it.
        # INTERSECT, never replace (peer review 2026-08-25). Assigning `ceil_ok = mc <= ceiling_max`
        # discarded band_ok, so a ceiling_max set ABOVE the band bound would fire the ceiling above
        # the range the band was fitted on -- the exact extrapolation the band bound exists to
        # prevent. Harmless while 150k < 250k, latent the moment someone raises it.
        ceil_ok = band_ok
        if m5_ceiling_max_mcap and float(m5_ceiling_max_mcap) > 0:
            ceil_ok = band_ok and mc <= float(m5_ceiling_max_mcap)
        # The ceiling stays on the provider's number. Our tape's sum is NOT a bound on what the
        # ceiling measures: it spans every known pool of the coin (arbitrage pools run up to 20x a
        # single pair's count), while the ceiling was fitted on one pair's m5. A bound may admit,
        # never refuse (test_a_lower_bound_can_admit_but_never_refuse).
        if ceil_ok and vol_m5 is not None and m5_ceiling and float(m5_ceiling) > 0:
            cap5 = mc * float(m5_ceiling)
            if float(vol_m5) > cap5:
                return False, cap5, "m5_turnover_over_ceiling"
        if h1_organic is not None and turnover_floor and float(turnover_floor) > 0:
            need1 = mc * float(turnover_floor)
            if h1_organic < need1:
                return False, need1, "turnover_under_floor"
            return _ok(need1, "ok")
        if h1_organic is None and turnover_floor and float(turnover_floor) > 0:
            need1 = mc * float(turnover_floor)
            legs["tape" if h1_bound is not None and h1_bound >= need1 else "unknown"] += 1
            if vol_h1 is None and vol_m5 is None:
                label = _quiet_label()
                return _ok(need1 if label == "own_tape_proven" else None, label)
        if vol_h1 is None:
            if vol_m5 is not None:
                return _ok(None, "ok")
            return _ok(None, _quiet_label())
        # both relative legs off: the flat floor still guards, exactly as before this leg existed
    if vol_h1 is None:
        floor_usd = float(abs_floor or 0.0) * _h1_floor_share(pair_age_s)
        if floor_usd > 0:
            legs["tape" if h1_bound is not None and h1_bound >= floor_usd else "unknown"] += 1
        label = _quiet_label()
        return _ok((floor_usd or None) if label == "own_tape_proven" else None, label)
    # THE FLAT FLOOR ASKS THE SAME QUESTION AS THE OTHER LEGS (2026-09-16). It subtracted the
    # source buy itself, so the index-lag rule would have fixed the ratio legs and left this one
    # still crediting the provider with a buy it had not indexed.
    _abs_organic = organic_volume(vol_h1, signal_usd, source_age_s=source_age_s,
                                  index_lag_s=index_lag_s)
    if h1_bound is not None and _abs_organic is not None:
        _abs_organic = max(_abs_organic, h1_bound)
    floor_usd = float(abs_floor or 0.0) * _h1_floor_share(pair_age_s)
    if floor_usd > 0 and float(_abs_organic or 0.0) < floor_usd:
        return False, floor_usd, "under_abs_floor"
    return _ok(floor_usd or None, "ok")


# A PAIR YOUNGER THAN AN HOUR HAS NOT HAD AN HOUR (operator, 2026-09-25; CLAIMS caaa6a99 on Solana
# was 85 s old, h1 == m5 == $2,339, refused against the $3,000 hourly floor). Its h1 is its whole
# life, so the flat hourly floor is pro-rated by the pair's age -- but never below five minutes,
# the shortest window the provider reports, so a coin seconds old is not judged on a sliver of its
# opening burst. Unknown, negative or hour-plus age: the whole floor, exactly as before.
# ponytail: only the FLAT floor is pro-rated; the relative h1 leg (MEMECOIN_REAL_MIN_TURNOVER_H1)
# is 0 in production. Give it the same share if it is ever switched on.
_H1_FLOOR_MIN_AGE_S = 300.0


def _h1_floor_share(pair_age_s):
    """The fraction of the hourly floor a pair of this age is held to: 1.0 unless 0 <= age < 1 h."""
    try:
        age = float(pair_age_s)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(age) or age < 0 or age >= 3600.0:
        return 1.0
    return max(age, _H1_FLOOR_MIN_AGE_S) / 3600.0


def h1_abs_floor_for(network_id, mcap):
    """(floor_usd, basis) for the hourly DOLLAR floor; basis None means the flat floor.

    THE FLOOR SCALES WITH THE COIN where a chain names a ratio (2026-09-28, operator via Alpha:
    "the 4663 h1 floor is too high for small coins"). floor = min(MEMECOIN_REAL_MIN_VOLUME_H1_USD,
    r x effective mcap) with r = MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_<chain> (only 4663 has one).
    It can only LOOSEN: a coin whose r x mcap reaches the flat floor keeps the flat floor, and an
    unknown market cap keeps it too. A chain-specific USD floor overrides the global floor;
    absent means inherit, explicit zero disables this dollar leg even with unknown market cap.
    The pair-age pro-rating in volume_alive_decision still applies.
    """
    override = getattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD_%s" % network_id, None)
    flat = float((cfg.MEMECOIN_REAL_MIN_VOLUME_H1_USD if override is None else override) or 0.0)
    ratio = float(getattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_%s" % network_id, 0.0) or 0.0)
    try:
        mc = float(mcap)
    except (TypeError, ValueError):
        mc = None
    if (ratio <= 0 or flat <= 0 or mc is None or not math.isfinite(mc) or mc <= 0
            or ratio * mc >= flat):
        return flat, None
    return ratio * mc, "%.2f%% of mcap $%.0f" % (ratio * 100.0, mc)


def solana_micro_h1_floor(network_id, coin_band, mcap):
    """The Solana micro band's own hourly turnover floor, or 0 (spec 2026-09-28-memecoin-solana-micro-
    h1-turnover-floor-design.md). The relative h1 leg of volume_alive_decision judges it: ORGANIC h1
    over `mcap`, which the entry hook passes as facts.effective_mcap. An unknown mcap has no band."""
    if str(network_id) != "solana" or coin_band != "micro" or mcap is None or mcap <= 0:
        return 0.0
    try:
        return max(0.0, float(getattr(cfg, "MEMECOIN_REAL_SOLANA_MICRO_MIN_TURNOVER_H1", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def volume_gate_args(st, *, mcap, network_id, coin_band, source_user_id, signal_usd,
                     source_age_s=None, decision_at=None):
    """The volume gate's arguments, built in ONE place for the entry hook and the revival watch.

    Returns (active, boundary, effective_m5_ceiling, kwargs for volume_alive_decision).

    WHY ONE PLACE (2026-09-12). The revival watch kept its own copy of these arguments and missed
    the 2026-09-08 in-band floors. It judged every in-band refusal against a floor of 0, so all 40
    watches since 2026-08-31 "cleared" on their first poll with no data (ITEMS on 4663 read
    `vol_m5 0 >= mcap 0 x 0.000`), re-asked one second later on the same stale snapshot, and were
    spent. 0 of 58 re-asks ever filled.
    """
    # A new pool cannot make an old token young. Missing age keeps the full floor.
    ages = []
    for age in ((st or {}).get("pair_age_s"), _pair_age_s(st, decision_at),
                (st or {}).get("token_age_s")):
        try:
            if age is not None and math.isfinite(float(age)):
                ages.append(float(age))
        except (TypeError, ValueError, OverflowError):
            pass
    boundary = float(getattr(cfg, "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD", 0.0) or 0.0)
    if source_user_id in getattr(cfg, "MEMECOIN_REAL_STRICT_VOLUME_SOURCE_IDS", frozenset()):
        boundary = float(getattr(cfg, "MEMECOIN_REAL_STRICT_VOLUME_MIN_MCAP_USD", 0.0) or 0.0)
    active = bool(mcap is not None and boundary > 0 and mcap > boundary)
    # IN-BAND CEILING (2026-09-08), symmetric with the floor below. When the coin is in band the
    # caller passes `m5_band_max_mcap=0`, so `band_ok` is True and `ceil_ok` follows it — a non-zero
    # ceiling supplied HERE fires, where the above-band one never could. PER CHAIN AND BAND
    # (2026-09-08 operator): solana nano tolerates turnover to 1.00, everything else stops at 0.50.
    # `coin_band` is the canonical band, so the nano cutoff is not re-derived here.
    ceiling = (getattr(cfg, "MEMECOIN_REAL_MAX_TURNOVER_M5", 0.0) if active
               else cfg.inband_max_turnover_m5_for(network_id, coin_band))
    solana_small_h1_off = (str(network_id) == "solana" and coin_band in ("nano", "micro")
                           and mcap is not None and mcap > 0
                           and not cfg.MEMECOIN_REAL_SOLANA_SMALL_H1_FLOOR_ENABLED)
    return active, boundary, ceiling, dict(
        abs_floor=0.0 if solana_small_h1_off else h1_abs_floor_for(network_id, mcap)[0],
        turnover_floor=max(0.0 if solana_small_h1_off else cfg.MEMECOIN_REAL_MIN_TURNOVER_H1,
                           solana_micro_h1_floor(network_id, coin_band, mcap)),
        vol_m5=(st or {}).get("volume_m5"),
        # IN-BAND FLOOR (2026-09-08). `m5_floor` is the leg `band_ok` selects, and band_ok means
        # mcap AT OR BELOW the boundary — the exact case `active` excludes. Below the boundary the
        # in-band floor speaks; above it, the over-band floor decides. PER CHAIN (2026-09-08
        # operator): 4663 wants 0.02 and Solana wants 0.10. See cfg.inband_min_turnover_m5_for_chain.
        m5_floor=(getattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_M5", 0.0) if active
                  else cfg.inband_min_turnover_m5_for_chain(network_id)),
        # MICRO-BAND DOLLAR FLOOR (2026-09-18). Scoped HERE, where the canonical band and the chain
        # are both known, so the pure decision keeps one number and no band arithmetic.
        m5_abs_floor_usd=cfg.micro_min_volume_m5_usd_for(network_id, coin_band),
        m5_band_max_mcap=boundary if active else 0.0,
        m5_floor_over_band=(getattr(cfg, "MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND", 0.0)
                            if active else 0.0),
        m5_ceiling=ceiling,
        m5_required=(active and bool(getattr(cfg, "MEMECOIN_REAL_M5_REQUIRED", 0))
                     or str(network_id) == "56" and
                     cfg.inband_min_turnover_m5_for_chain(network_id) > 0),
        m5_txns=_m5_txn_count(st),
        m5_txns_floor=getattr(cfg, "MEMECOIN_REAL_MIN_TXNS_M5", 0) if active else 0,
        source_age_s=source_age_s,
        pair_age_s=max(ages) if ages else None,
        index_lag_s=cfg.provider_index_lag_s(network_id),
        m5_txns_required=active and bool(getattr(cfg, "MEMECOIN_REAL_TXNS_M5_REQUIRED", 0)),
        signal_usd=signal_usd)


SOLANA_HOURLY_FLOOR_EVIDENCE_VERSION = 1
_HOURLY_FLOOR_REASONS = ("turnover_under_floor", "under_abs_floor")


def solana_hourly_floor_review(conn, *, network_id, mint, origin_event_id, attempt_id, decision_at,
                               stats, vol_h1, mcap, decision_kwargs, why, need, effective_ceiling,
                               source_created_at):
    """Review a Solana hourly-floor refusal with S1 and S2. None when the refusal is out of scope.

    Spec: docs/superpowers/specs/2026-09-17-solana-hourly-floor-activity-design.md.
    S1: prior feed buys satisfy the hourly DOLLAR floor through the pure evaluator (max, never sum).
    S2: one page of mint-address signatures; a valid count at or above the reviewed cutoff grants
    `h1_floor_activity_exception`. The count never becomes dollars. An unset cutoff records evidence
    only. The review changes no other refusal family and no later gate.
    """
    from datetime import datetime
    from sniper.services import memecoin_solana_address_activity as activity

    if str(network_id or "") != "solana" or why not in _HOURLY_FLOOR_REASONS:
        return None
    st = stats or {}
    decided = datetime.fromisoformat(str(decision_at).replace("Z", "+00:00"))
    decision_ts = int(decided.timestamp())
    source_sig = m.event_transaction_signature(origin_event_id)
    kwargs = dict(decision_kwargs or {})
    organic_h1 = organic_volume(vol_h1, kwargs.get("signal_usd") or 0.0,
                                source_age_s=kwargs.get("source_age_s"),
                                index_lag_s=kwargs.get("index_lag_s") or 0.0)
    try:
        source_created = datetime.fromisoformat(str(source_created_at).replace("Z", "+00:00"))
        anchor_gap_s = round((decided - source_created).total_seconds(), 3)
    except (TypeError, ValueError):
        anchor_gap_s = None
    record = {
        "evidence_version": SOLANA_HOURLY_FLOOR_EVIDENCE_VERSION, "chain": "solana", "mint": mint,
        "origin_event_id": origin_event_id, "attempt_id": attempt_id, "decision_time": decision_at,
        "selected_provider_pool": st.get("pair_address"), "selected_provider_dex": st.get("dex_id"),
        "source_signature": source_sig, "method_version": activity.METHOD_VERSION,
        "provider_h1_usd": vol_h1, "provider_m5_usd": st.get("volume_m5"),
        "organic_h1_usd": organic_h1, "effective_floor_usd": need,
        "m5_ceiling_ratio": effective_ceiling, "original_decision": why,
    }
    feed = m.solana_prior_feed_buys(conn, mint, decision_at=decision_at,
                                    source_signature=source_sig, source_event_id=origin_event_id)
    record.update({
        "feed_buy_usd": feed["feed_buy_usd"], "feed_eligible_count": feed["eligible_count"],
        "feed_duplicate_count": feed["duplicate_count"],
        "feed_excluded_source_count": feed["excluded_source_count"],
        "feed_unknown_row_count": feed["unknown_row_count"],
        "feed_contradictory_count": feed["contradictory_count"],
        "feed_late_receipt_count": feed["late_receipt_count"],
        # Every counted row with its identity_kind (signature | trade_id), on admit and refusal.
        "feed_rows": feed["rows"],
    })
    kwargs["h1_floor_lower_bound"] = feed["feed_buy_usd"]
    feed_alive, _feed_need, feed_why = volume_alive_decision(vol_h1, mcap, **kwargs)
    activity_fields = {
        "activity_address_type": activity.ADDRESS_TYPE, "activity_anchor": source_sig,
        "activity_anchor_gap_s": anchor_gap_s, "activity_valid_signature_count": None,
        "activity_failed_count": None, "activity_missing_time_count": None,
        "activity_page_saturated": None, "activity_oldest_block_time": None,
        "activity_returned_count": None, "activity_cutoff": None, "activity_result": "not_run",
        "activity_exception_used": False, "activity_call_count": 0, "activity_elapsed_ms": None,
        "activity_budget_state": None,
    }
    if feed_alive:
        record.update(activity_fields, final_gate_decision="admit",
                      final_reason="h1_floor_met_by_feed_buys")
        logger.warning("solana hourly floor met by feed buys mint=%s origin=%s feed_usd=%.2f rows=%s",
                       mint, origin_event_id, feed["feed_buy_usd"] or 0.0,
                       json.dumps([[r["identity_kind"], r["identity"], r["handle"], round(r["usd"], 2), r["traded_at"]]
                                   for r in feed["rows"]]))
        return record
    probe, disabled_reason = activity.runtime_probe()
    if source_sig is None or not mint:
        # No validated source signature or no mint: unknown, and no RPC call.
        activity_fields.update(activity_result="unknown", activity_budget_state="no_source_signature")
    elif probe is None:
        activity_fields.update(activity_result="disabled", activity_budget_state=disabled_reason)
    else:
        out = probe.probe(mint, decision_ts=decision_ts, source_signature=source_sig)
        activity_fields.update({
            "activity_valid_signature_count": out.get("valid_signature_count"),
            "activity_failed_count": out.get("failed_count"),
            "activity_missing_time_count": out.get("missing_time_count"),
            "activity_page_saturated": out.get("page_saturated"),
            "activity_oldest_block_time": out.get("oldest_block_time"),
            "activity_returned_count": out.get("returned_count"),
            "activity_cutoff": out.get("cutoff"), "activity_result": out.get("result_type"),
            "activity_exception_used": bool(out.get("exception_used")),
            "activity_call_count": out.get("call_count"), "activity_elapsed_ms": out.get("elapsed_ms"),
            "activity_budget_state": out.get("budget_state"),
        })
    record.update(activity_fields)
    if activity_fields["activity_exception_used"]:
        record.update(final_gate_decision="admit", final_reason="h1_floor_activity_exception")
        logger.warning("solana hourly floor activity exception mint=%s origin=%s valid_signatures=%s "
                       "cutoff=%s saturated=%s", mint, origin_event_id,
                       activity_fields["activity_valid_signature_count"],
                       activity_fields["activity_cutoff"], activity_fields["activity_page_saturated"])
    else:
        record.update(final_gate_decision="refuse", final_reason=feed_why)
    return record


def revival_watch_volume_cleared(stats, event, *, mcap=None, network_id=None, coin_band=None,
                                 evidence_out=None, decision_at=None):
    """Return true when a FRESH read passes the same volume gate that refused the event.

    Missing mcap is not clearance: the absence of evidence cannot be evidence of a revival.

    `evidence_out`, when given a dict, is updated in place with ONE poll's evidence: the on-chain
    bound (usd, reason, coverage, covered_s, measured_at -- the same fields the entry gate records
    via `trace.mark_substage("onchain_volume_m5", ...)`), the decision itself (allowed,
    decision_reason), and source_age_s exactly as passed to `volume_alive_decision` (never
    computed here -- this call never passes one, so it is recorded as None). Left untouched (an
    empty dict) when `mcap` fails validation and no read or decision happens at all. Purely
    observational: it never changes `mcap`, `kwargs`, or the returned decision.
    """
    st = stats or {}
    if mcap is None:
        mcap = getattr(event, "their_market_cap", None)
    try:
        mcap = float(mcap)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(mcap) or mcap <= 0:
        return False
    signal_usd = (float(getattr(event, "their_size_usd", 0) or 0)
                  if getattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", False) else 0.0)
    _active, _boundary, _ceiling, kwargs = volume_gate_args(
        st, mcap=mcap, network_id=network_id, coin_band=coin_band,
        source_user_id=getattr(event, "source_user_id", None), signal_usd=signal_usd,
        decision_at=decision_at)
    from sniper.services.memecoin_volume_writer import gate_evidence
    evidence = gate_evidence(network_id, getattr(event, "mint", None),
                             getattr(event, "fomo_event_id", None), stats=st,
                             source_tokens=_source_tokens(event))
    if evidence is not None:
        kwargs["m5_floor_lower_bound"] = evidence.get("usd")
        kwargs["h1_floor_lower_bound"] = onchain_h1_lower_bound(evidence)
        kwargs["m5_txns_lower_bound"] = onchain_m5_txns_lower_bound(evidence)
        # The entry gate's rule: a lane that did not measure cannot refuse (not on 56, whose m5 is
        # required). `source_tx_absent` keeps the provider here: its blindness needs the venue-gap
        # verdict, which only the entry gate computes.
        if (evidence.get("usd") is None and str(network_id) != "56"
                and evidence.get("reason") in ONCHAIN_M5_BLIND_REASONS
                and evidence.get("reason") != "source_tx_absent"):
            kwargs["m5_floor_undecidable"] = True
    allowed, _need, _reason = volume_alive_decision(st.get("volume_h1"), mcap, **kwargs)
    if evidence_out is not None:
        # Same test as the `m5_floor_lower_bound` branch above (`is not None`, not truthiness) --
        # harmless today since gate_evidence never returns an empty dict, but one test everywhere
        # this value is checked means a future reader never has to wonder if the difference between
        # the two was deliberate.
        evidence_out["usd"] = evidence.get("usd") if evidence is not None else None
        evidence_out["reason"] = evidence.get("reason") if evidence is not None else None
        evidence_out["coverage"] = evidence.get("coverage") if evidence is not None else None
        evidence_out["covered_s"] = evidence.get("covered_s") if evidence is not None else None
        # Beside it, never instead of it: `covered_s` is the window requested and cannot
        # fail, so the coverage actually seen is `covered_s - covered_gap_s`.
        evidence_out["covered_gap_s"] = (evidence.get("covered_gap_s")
                                         if evidence is not None else None)
        evidence_out["measured_at"] = evidence.get("measured_at") if evidence is not None else None
        evidence_out["allowed"] = bool(allowed)
        evidence_out["decision_reason"] = _reason
        evidence_out["source_age_s"] = kwargs.get("source_age_s")
    return bool(allowed)


def m5_floor_evidence(stats, event, *, mcap, network_id, coin_band, decision_at=None,
                      source_age_s=None):
    """The funded volume gate's m5 leg for one event, OBSERVATIONAL ONLY (Gamma via Alpha,
    2026-10-07: grade age x mcap x turnover on funded_clone paper entries before the operator's
    m5-floor question). Built from the gate's own pieces and no new provider call: the stats
    already in hand, volume_gate_args, the own-tape gate_evidence (a local read) and
    volume_alive_decision. Never decides anything.

    Fields: organic_m5_usd (the own-tape lower bound the gate reads, None when the tape has no
    bound, see own_reason), provider_organic_m5_usd (stats volume_m5 less the source's buy where the
    provider can contain it), tested_m5_usd and tested_ratio (what the m5 floor is compared with,
    over mcap), m5_floor_ratio and m5_floor_usd (the floor this mcap's band applies),
    m5_abs_floor_usd, pair_age_s (the gate's oldest known age), coin_band, would_allow and
    would_reason (the whole volume decision, h1 legs included). A blind or unpriced tape makes the
    m5 floor undecidable exactly as in the entry hook (not on 56): m5_floor_undecidable and
    own_measurement_basis say so, and source_tx_absent first takes the hook's venue-gap verdict. An unusable mcap gives
    {"reason": "mcap_unknown"}: the gate's ratio legs need one."""
    try:
        mc = float(mcap)
    except (TypeError, ValueError):
        mc = None
    if mc is None or not math.isfinite(mc) or mc <= 0:
        return {"reason": "mcap_unknown"}
    st = stats or {}
    signal_usd = (float(getattr(event, "their_size_usd", 0) or 0)
                  if getattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", False) else 0.0)
    _active, _boundary, _ceiling, kwargs = volume_gate_args(
        st, mcap=mc, network_id=network_id, coin_band=coin_band,
        source_user_id=getattr(event, "source_user_id", None), signal_usd=signal_usd,
        source_age_s=source_age_s, decision_at=decision_at)
    provider = organic_volume(kwargs.get("vol_m5"), signal_usd, source_age_s=source_age_s,
                              index_lag_s=kwargs.get("index_lag_s") or 0.0)
    from sniper.services.memecoin_volume_writer import gate_evidence
    evidence = gate_evidence(network_id, getattr(event, "mint", None),
                             getattr(event, "fomo_event_id", None), stats=st,
                             source_tokens=_source_tokens(event),
                             source_usd=_source_usd_for_bound(event))     # the entry gate's read
    basis = None
    if evidence is not None:
        # The entry hook's rule, step for step (Alpha, 2026-10-07: the paper verdict fell back to
        # the provider's m5 on a blind tape, where the live gate stands down).
        evidence = dict(evidence)
        kwargs["m5_floor_lower_bound"] = evidence.get("usd")
        kwargs["h1_floor_lower_bound"] = onchain_h1_lower_bound(evidence)
        kwargs["m5_txns_lower_bound"] = onchain_m5_txns_lower_bound(evidence)
        try:
            unpriced = sum(int(p.get("swaps") or 0) for p in (evidence.get("unpriced_pools") or []))
        except (TypeError, ValueError, AttributeError):
            unpriced = 0
        reason = str(evidence.get("reason") or "")
        if reason == "source_tx_absent":
            from sniper.services.memecoin_onchain_volume import venue_gap_verdict
            venue_blind, detail = venue_gap_verdict(
                evidence.get("source_absent_usd"), provider,
                min_usd=float(getattr(cfg, "MEMECOIN_ONCHAIN_M5_VENUE_GAP_MIN_USD", 0.0) or 0.0),
                ratio=float(getattr(cfg, "MEMECOIN_ONCHAIN_M5_VENUE_GAP_RATIO", 0.0) or 0.0))
            evidence.update(detail)
            if not venue_blind:
                evidence.update(usd=evidence.get("source_absent_usd"), reason="ok",
                                coverage="partial_source_tx_absent_quiet")
                reason = "ok"
                kwargs["m5_floor_lower_bound"] = evidence.get("usd")
        if evidence.get("usd") is None and (unpriced > 0 or reason in ONCHAIN_M5_BLIND_REASONS):
            if str(network_id) != "56":
                kwargs["m5_floor_undecidable"] = True
            basis = ("bsc_m5_required_unknown" if str(network_id) == "56" else
                     "onchain_unpriced_undecidable" if unpriced > 0 else "onchain_blind_undecidable")
    bound = evidence.get("usd") if evidence is not None else None
    allowed, _need, why = volume_alive_decision(st.get("volume_h1"), mc, **kwargs)
    band_max = float(kwargs.get("m5_band_max_mcap") or 0.0)
    ratio = float((kwargs.get("m5_floor") if band_max <= 0 or mc <= band_max
                   else kwargs.get("m5_floor_over_band")) or 0.0)
    # The gate's own combination: the larger of the two, and the bound alone only when it
    # already clears the floor (volume_alive_decision).
    if provider is not None and bound is not None:
        tested = max(float(provider), float(bound))
    elif provider is None and bound is not None and float(bound) >= mc * ratio:
        tested = float(bound)
    else:
        tested = provider
    return {"organic_m5_usd": bound,
            "own_reason": evidence.get("reason") if evidence is not None else "chain_not_taped",
            "own_coverage": evidence.get("coverage") if evidence is not None else None,
            "own_measurement_basis": basis,
            "m5_floor_undecidable": bool(kwargs.get("m5_floor_undecidable")),
            "provider_organic_m5_usd": provider,
            "tested_m5_usd": tested, "tested_ratio": tested / mc if tested is not None else None,
            "m5_floor_ratio": ratio, "m5_floor_usd": mc * ratio,
            "m5_abs_floor_usd": float(kwargs.get("m5_abs_floor_usd") or 0.0),
            "pair_age_s": kwargs.get("pair_age_s"), "source_age_s": source_age_s,
            "mcap_usd": mc, "coin_band": coin_band,
            "would_allow": bool(allowed), "would_reason": why}


def _mcap_absurd_usd():
    try:
        return float(getattr(cfg, "MEMECOIN_REAL_ABSURD_MCAP_USD", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def dex_mcap_allowed(chain):
    """May DexScreener supply the market cap that decides this chain's BAND?

    False on a DexScreener-blind chain: the blind source owns the band there.

    THE BAND DRIVES EXIT POLICY, not just admission, which is why this is pinned rather than
    left to whichever source answers first. While 4663 had no FOMO_TO_DEX entry `st` was always
    empty on it, so the blind path was already the only band source; adding the entry (which the
    six market-data ENTRY gates need) would have switched the band source silently. Measured
    2026-08-30 across the nano/micro/small boundary regions we actually trade, 18 of 39 tokens
    (46%) change band between the two sources, and 9 of 11 tokens where GeckoTerminal has no cap
    (82%) move off out_of_scope into a real band.

    The disagreement is NOT FDV-vs-circulating -- DexScreener marketCap == fdv on 28 of 34
    tokens, median GT/DS 0.9925 -- and it is not price: GeckoTerminal agrees with our own route
    quotes at 0.967 median over ~4,000 paired marks. It is the supply basis, and it is
    unresolved. Settling it costs totalSupply eth_calls on an endpoint that is already rate
    limited, so an entry-gate repair does not get to decide it.

    There is deliberately NO fallback to `st` when the blind source is empty. That cohort is the
    82% row above, so a fallback would move exactly the bands this exists to hold still.
    """
    from sniper.settings import memecoin as _cfg
    return chain not in (_cfg.MEMECOIN_DEX_BLIND_CHAINS or ())


def _set_priced_shadow(ev, shadow):
    try:
        ev.priced_mcap_shadow = shadow
    except (AttributeError, TypeError):
        pass                              # an immutable event: no shadow fact, the value still gates


def _priced_history_mcap(conn, ev, chain, decision_at, resolved):
    """The shadow mcap at the source's price where Solana history `resolved`, set on
    `ev.priced_mcap_shadow` as (mcap, basis) and returned; None (and the shadow cleared, so an event
    asked twice never carries an old one) when it does not apply, cannot be priced or reads absurd. A proven pump mint is
    source price x 1e9 (`pump_supply`): its supply only shrinks after mint, so 1e9 can overstate by
    ~1.8%, never understate. Else the readings' implied supply at the source price
    (`history_priced`). No I/O; never raises (a failure is logged and leaves today's decision)."""
    _set_priced_shadow(ev, None)
    if not resolved or chain != "solana":
        return None
    try:
        value, basis = _pump_supply_mcap(ev), "pump_supply"
        if value is None:
            value, basis = m.resolve_mint_mcap(
                conn, getattr(ev, "mint", None), network_id=chain,
                since_iso=cfg.real_history_since_iso(decision_at),
                price=getattr(ev, "their_price", None), priced=True), "history_priced"
        absurd = _mcap_absurd_usd()
        if value is None or (absurd and value > absurd):
            return None
        _set_priced_shadow(ev, (value, basis))
        return value
    except Exception:
        logger.exception("priced mcap shadow failed mint=%s", getattr(ev, "mint", None))
        return None


def _priced_ceiling_refuses(priced_mcap, ceiling):
    """MEMECOIN_MCAP_HISTORY_PRICED: True when the priced shadow is over the ceiling. Off, no shadow,
    or no ceiling: False. AUTO_TEMPLATES admits a scalp-sized coin exactly as today's branch does."""
    if not (getattr(cfg, "MEMECOIN_MCAP_HISTORY_PRICED", False) and priced_mcap and ceiling):
        return False
    if cfg.MEMECOIN_AUTO_TEMPLATES == "on" and priced_mcap >= cfg.MEMECOIN_SCALP_MCAP_USD:
        return False
    return priced_mcap > ceiling


def _pump_supply_mcap(ev):
    """Source price x 1e9 for a Solana detection that proves a pump.fun mint
    (memecoin_onchain_lane.pump_mint_proven), else None. No I/O: ~31 us for the PDA."""
    try:
        price = float(getattr(ev, "their_price", None))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(price) and price > 0):
        return None
    from sniper.services.memecoin_onchain_lane import pump_mint_proven
    from sniper.services.memecoin_slot_lane import PUMP_TOTAL_SUPPLY
    return price * PUMP_TOTAL_SUPPLY if pump_mint_proven(ev) else None


def mcap_entry_decision(event_mc, resolved_mc, ceiling, floor=0.0):
    """Real-entry mcap gate (pure). `event_mc` = the feed event's their_market_cap; `resolved_mc` =
    same-mint history when the event omits it. Returns (allow, effective_mc, reason).
      - applies the SAME ceiling to a resolved value (a feed gap must not fire a large cap, AND a
        genuinely small coin must not be dropped just because THIS event lacked the field)
      - applies the SAME floor to a resolved value, for the same reason in reverse
      - truly unpriceable (both missing) -> conservative skip.
    Replaces the old `not mc or mc > ceiling`, which fail-closed-skipped small coins on feed gaps
    (~5% of approved buys; provably lost sub-$1M Solana winners like jester +68, PrimeT +39).

    FLOOR (2026-08-25, operator). A coin under the floor is refused. The floor is a COST decision,
    not a performance one, and the distinction matters because the performance evidence does not
    support it: sub-15k reads -16.1% cutting at 08-19 and +1.7% cutting at 08-21, on nine trades'
    difference, so no floor is derivable from realized PnL. What IS stable is the entry cost --
    median route/display is +36.7% under 30k against +9.8% over 250k -- and that we pay before the
    coin does anything.
    KNOWN COST OF THE FLOOR: sub-30k fills were 41% winners and produced 6 of the 22 fills that
    ever returned +50% or more, i.e. 27% of the book's large tail. This refuses that tail on
    purpose. It is the operator's call and it is recorded here so nobody later reads it as
    evidence-derived.

    A floor of 0 is OFF, matching the ceiling's convention. Order matters: an unresolved mcap
    still skips BEFORE either bound is applied, so the floor never converts an unknown into a
    refusal with a misleading reason."""
    # CORRUPT-MCAP GUARD. A garbage reading must be rejected as UNRESOLVABLE, not as a ceiling
    # breach, and it must be rejected even with the ceiling off. The over-ceiling population carries
    # a max of $1.53 TRILLION and 11 rows above $10B (0.40%) — those are broken data, not coins, and
    # downstream banding/policy selection reads this value. Placing the check here rather than at
    # the ceiling means it survives the ceiling being removed entirely, which is exactly the
    # configuration this now runs in.
    absurd = _mcap_absurd_usd()
    raw = event_mc if event_mc else resolved_mc
    if absurd > 0 and raw and float(raw) > absurd:
        return False, None, "mcap_unresolvable"
    have_ceiling = bool(ceiling) and ceiling > 0
    have_floor = bool(floor) and floor > 0
    if not have_ceiling and not have_floor:
        # Still resolve the mcap: downstream banding reads eff_mc, and returning the event's
        # empty field with the ceiling off banded every fresh follow_activity coin out_of_scope
        # (BRAINROT 2026-08-19 — $9.3K coin refused as out_of_scope).
        return True, (event_mc if event_mc else resolved_mc), "no_ceiling"
    mc = event_mc if event_mc else resolved_mc
    if not mc:
        return False, None, "mcap_unresolved"
    if have_ceiling and mc > ceiling:
        return False, mc, "over_ceiling"
    if have_floor and mc < floor:
        return False, mc, "under_floor"
    return True, mc, "ok"


def chase_gap_pct(ev, *, our_price, reference=None):
    """How far ABOVE the trader's entry our fill would be, in %. None when either price is unknown.

    The trader's entry is `reference` when the caller bound one (an EVM post-trade price on the
    display pair, memecoin_evm_post_trade.pre_quote_reference), else the event's chase reference
    (the Solana on-chain lane's post-trade price under MEMECOIN_REAL_CHASE_REFERENCE=post_trade),
    else their average fill."""
    theirs = (reference or getattr(ev, "chase_reference_price", None)
              or getattr(ev, "their_price", None))
    try:
        theirs, ours = float(theirs or 0), float(our_price or 0)
    except (TypeError, ValueError):
        return None
    if theirs <= 0 or ours <= 0:
        return None
    return (ours / theirs - 1.0) * 100.0


SOURCE_IMPACT_FORMULA = "jupiter_loss_fraction_to_paid_price_premium_v2"

# WHY an allowance was not granted. A bare `None` conflated five different facts, and the
# difference decides whether a refusal was the gate working or the gate blind — the same split the
# vol_death witness needed between `no_route` and `provider_error`. It cost a live probe on
# 2026-08-29 to answer "why did this refusal get no allowance".
IMPACT_OK = "ok"
IMPACT_NO_SIZE = "no_source_size"
IMPACT_NOT_SOLANA = "not_solana"
IMPACT_NO_QUOTE = "no_quote"
IMPACT_UNPARSEABLE = "unparseable_impact"
IMPACT_NONPOSITIVE = "nonpositive_impact"
# Jupiter returns exactly 1 when the route is saturated. Measured on mint 5BVSdqHxRt: '1' at $499,
# at $100 AND at $10 — a value that does not move with size is a SENTINEL, not a measurement, and
# i/(1-i) on it is infinite. Rejecting it is right because it carries no information about the
# source's footprint, not because a 100% impact is implausible.
IMPACT_SATURATED = "saturated_impact"


def source_impact_pct(ev, *, quote_fn, network_id=None):
    """Estimate the source trade's paid-price premium from a Jupiter quote.

    Jupiter reports the lost input-value fraction. It does not report the final pool-price move.
    A loss fraction `i` gives a paid-price premium of `i / (1 - i)`.

    The quote uses the current book, not the source trade's earlier book. The clamp bounds this
    model error. A missing or invalid input keeps the original chase cap.
    """
    network = str(network_id or getattr(ev, "network_id", "") or "").strip().lower()
    if network != "solana":
        return None, IMPACT_NOT_SOLANA
    size = getattr(ev, "their_size_usd", None)
    try:
        size = float(size or 0)
    except (TypeError, ValueError):
        return None, IMPACT_NO_SIZE
    if not math.isfinite(size) or size <= 0:
        return None, IMPACT_NO_SIZE
    try:
        quote = quote_fn(size)
    except Exception:
        logger.exception("source-impact quote failed for %s", getattr(ev, "mint", None))
        return None, IMPACT_NO_QUOTE
    if not isinstance(quote, dict):
        return None, IMPACT_NO_QUOTE
    try:
        impact_fraction = float(quote.get("priceImpactPct"))
    except (TypeError, ValueError):
        return None, IMPACT_UNPARSEABLE
    if not math.isfinite(impact_fraction) or impact_fraction <= 0:
        return None, IMPACT_NONPOSITIVE
    if impact_fraction >= 1:
        return None, IMPACT_SATURATED
    return impact_fraction / (1.0 - impact_fraction) * 100.0, IMPACT_OK


# MEASUREMENT ONLY, added 2026-09-02. A second, free estimate of the source's price impact,
# recorded beside the quote-derived one so the two can be graded against each other on real rows.
# It is NEVER read by a decision. See
# docs/superpowers/specs/2026-09-02-source-impact-chase-allowance-4663-design.md.
MODEL_IMPACT_FORMULA = "constant_product_liquidity_floor_uncapped_v1"


def model_impact_pct(size_usd, liquidity_usd):
    """A constant-product floor on the source's impact, in PAID-PREMIUM percent.

    Same space as `source_impact_pct`, so the two columns are directly comparable. `slippage_frac`
    already returns the fraction of mid the fill moves against us, and `modeled_fill` applies it as
    `mid * (1 + slip)` — that IS the paid premium, so it needs no `i/(1-i)` conversion. Jupiter's
    `priceImpactPct` is a lost-input-value fraction and DOES. Two impact numbers in different
    spaces is the defect class that produced a $3.47M fake PnL on 2026-07-30; they are converted to
    one space here, once.

    Returns None — never a number — when either input is missing or non-positive.

    Deliberately NOT a bare `slippage_frac` call. That helper answers unknown liquidity with its
    cap (0.5), which is conservative for a paper fill because the modeled fill gets worse, and
    FAIL-OPEN here because an absent input would justify the maximum allowance. A helper reused
    across a change of caller keeps a failure direction that was chosen for somebody else. Missing
    liquidity means no estimate, therefore no allowance.

    Also uncapped: the cap censors the estimate exactly where the pool is thinnest, and a censored
    value cannot be graded against a quote. The thin end is real, not hypothetical — the minimum
    liquidity in the 4663 gate population is $8, and $497 into $8 gives 6212.5%.

    WARNING: the uncapped value is FAIL-OPEN BY CONSTRUCTION. A larger impact widens the chase cap,
    so an over-estimate is the unsafe direction. It is safe today only because the single consumer,
    `effective_chase_cap`, clamps it with `min(impact_pct, ceiling_pct)`. Any second reader added
    later must clamp too. This is the same trap this function was written to avoid: a value carries
    a failure direction that was decided by its one existing caller.
    """
    try:
        size = float(size_usd)
        liquidity = float(liquidity_usd)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(size) or not math.isfinite(liquidity):
        return None
    if size <= 0 or liquidity <= 0:
        return None
    from sniper.execution.solana_quote import slippage_frac
    frac = slippage_frac(size, liquidity, cap=float("inf"))
    try:
        frac = float(frac)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(frac) or frac < 0:
        return None
    return frac * 100.0


# THE RATIO ESTIMATOR (2026-09-02). Blocker 3 of the 4663 chase allowance: `source_impact_pct`
# does not consume a quote, it consumes ONE FIELD — `quote.get("priceImpactPct")` — and no EVM
# provider emits it. Repointing its `quote_fn` at a 4663 quoter moves the refusal population from
# `not_solana` at 22 of 22 to `unparseable_impact` at 100%: a different label on the same zero.
#
# This asks the book directly instead. Two quotes on the SAME pool at the SAME instant:
#
#     impact = price_at_dust / price_at_size - 1
#
# One provider, one book, no external mid. Dividing against a DexScreener mid would mix two
# providers with different pool selection and different timestamps, which is the defect class that
# produced the WETH/USD error on 2026-07-30.
#
# It returns the POOL-PRICE MOVE, the same physical quantity `model_impact_pct` estimates — so on
# 4663 the two pair like with like, unlike the Solana pairing where a pool-move floor is graded
# against Jupiter's realized route loss including fees and hops.
RATIO_IMPACT_FORMULA = "same_pool_dust_to_size_price_ratio_v1"

IMPACT_NO_DUST_QUOTE = "no_dust_quote"
IMPACT_DUST_NOT_SMALLER = "dust_not_smaller_than_size"


def ratio_impact_pct(ev, *, quote_fn, dust_usd=None, network_id=None):
    """Estimate the source trade's paid-price premium from two quotes on one book.

    `quote_fn(usd)` must return a mapping carrying tokens-out for a USD input under `buyAmount`
    (the EVM venue contract) or `outAmount` (the Jupiter contract). Returns
    `(premium_pct, reason)`, the same contract as `source_impact_pct`, so the two are
    interchangeable at the call site.

    CHAIN-AGNOSTIC BY CONSTRUCTION. It needs no provider impact field, so it works wherever a
    quoter answers "how many tokens for this much USD" — which is every venue we run.

    Fails CLOSED. Every unusable input returns `(None, reason)` and the caller keeps its base cap.
    A missing dust quote is NOT treated as zero impact: absent evidence of a footprint must not
    grant an allowance, which is the direction `slippage_frac`'s cap gets wrong for this caller.

    COST: two calls to `quote_fn`, therefore two round trips. `dex_v3_quote` batches its own
    fee-tier scan into one JSON-RPC batch, but that batching is INSIDE a single quote and does not
    span two of them. Do not cite the 1.15x-for-four figure for this; making the pair one round
    trip needs a batch-aware quoter and is a separate change.
    """
    if dust_usd is None:
        from sniper.settings.memecoin import MEMECOIN_CHASE_IMPACT_DUST_USD
        dust_usd = MEMECOIN_CHASE_IMPACT_DUST_USD
    try:
        size = float(getattr(ev, "their_size_usd", None) or 0)
        dust = float(dust_usd or 0)
    except (TypeError, ValueError):
        return None, IMPACT_NO_SIZE
    if not math.isfinite(size) or size <= 0:
        return None, IMPACT_NO_SIZE
    if not math.isfinite(dust) or dust <= 0:
        return None, IMPACT_NO_DUST_QUOTE
    # A dust size at or above the source's own size measures nothing: the "reference" would carry
    # as much impact as the thing it is the reference for, and the ratio collapses toward 1.
    if dust >= size:
        return None, IMPACT_DUST_NOT_SMALLER

    def _unit_price(usd):
        """Tokens per USD at `usd`, or None."""
        try:
            quote = quote_fn(usd)
        except Exception:
            logger.exception("ratio-impact quote failed for %s", getattr(ev, "mint", None))
            return None
        if not isinstance(quote, dict):
            return None
        raw = quote.get("buyAmount")
        if raw is None:
            raw = quote.get("outAmount")
        try:
            tokens = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(tokens) or tokens <= 0:
            return None
        return tokens / usd

    p_size = _unit_price(size)
    if p_size is None:
        return None, IMPACT_NO_QUOTE
    p_dust = _unit_price(dust)
    if p_dust is None:
        return None, IMPACT_NO_DUST_QUOTE
    # A real book gives strictly fewer tokens per dollar as the order grows. p_dust <= p_size means
    # either a flat quoter, a cached answer served twice, or a saturated route — none of which
    # carries information about the source's footprint. Same reasoning as IMPACT_SATURATED: reject
    # because the number is uninformative, not because the magnitude is implausible.
    if p_dust <= p_size:
        return None, IMPACT_NONPOSITIVE
    premium = (p_dust / p_size - 1.0) * 100.0
    if not math.isfinite(premium) or premium <= 0:
        return None, IMPACT_NONPOSITIVE
    return premium, IMPACT_OK


def effective_chase_cap(base_cap, impact_pct, *, ceiling_pct):
    """The base cap widened by the source's measured footprint, clamped.

    The ceiling exists so a whale nuking a dust pool cannot open the gate to infinity: a $50k buy
    into a $70k coin would otherwise justify almost any premium, and at that point we are not
    copying a trade, we are buying the crater.
    """
    try:
        base = float(base_cap)
    except (TypeError, ValueError):
        return base_cap
    if impact_pct is None:
        return base
    try:
        allowance = min(float(impact_pct), float(ceiling_pct))
    except (TypeError, ValueError):
        return base
    return base + max(0.0, allowance)


IMPACT_POST_TRADE_REFERENCE = "post_trade_reference"


def chase_impact_allowance(ev, *, network_id, quote_fn):
    """(impact_pct, why) for the pre-quote chase leg. An event whose gap is measured from the
    source's POST-trade price already has the source's own impact removed, so it gets NO allowance
    and no quote is spent on one: an allowance on top would remove the impact twice (the 2026-09-28
    chase_source_split error). Everything else is source_impact_pct, as before."""
    if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
        return None, "intervening_market"
    if getattr(ev, "chase_reference_price", None):
        return None, IMPACT_POST_TRADE_REFERENCE
    return source_impact_pct(ev, network_id=network_id, quote_fn=quote_fn)


def chase_ok(ev, *, our_price, max_pct, reference=None):
    """False iff the coin has already run `max_pct` beyond the trader's entry.

    There was NO chase guard anywhere on the real path — its only protection against a stale or
    already-pumped signal was the 120s staleness gate. That is adequate for the live feed but NOT for
    follow-activity, whose 300s poll cadence makes its signals 120-900s old BY CONSTRUCTION. Buying a
    coin that has already doubled since the trader bought is not copying him, it is buying his exit
    liquidity.

    Buying BELOW the trader is never refused — a better price than he got is a gift.

    FAIL-OPEN on unknown prices. A gate that fires on ABSENT data is the exact bug class that cost us
    the entire EVM leg (the honeypot check) and the sub-$1M winners (the mcap gate on feed gaps). If
    we cannot compute the gap we do not invent one; the other gates still apply.
    """
    if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
        return True  # Display/average prices cannot prove same-pool marginal movement.
    gap = chase_gap_pct(ev, our_price=our_price, reference=reference)
    if gap is None:
        return True
    return gap <= float(max_pct)


def is_fresh_follow_event(ev, max_lag_s):
    if getattr(ev, "signal_source", None) != "follow_activity":
        return False
    try:
        lag = float(getattr(ev, "lag_s", None))
        limit = float(max_lag_s)
    except (TypeError, ValueError):
        return False
    return 0.0 <= lag <= limit and limit >= 0.0


def _first_seen_floor(now_iso, window_s):
    """Oldest anchor the chase guard may use, or None for unbounded (window_s <= 0)."""
    try:
        window = float(window_s)
    except (TypeError, ValueError):
        return None
    if window <= 0 or not now_iso:
        return None
    try:
        return _iso_seconds_ago(now_iso, window)
    except Exception:
        return None


def first_seen_chase_ok(*, first_price, price_now, max_ratio):
    """False iff the coin already ran more than `max_ratio`x above the price at which OUR OWN feed
    first showed it. The per-event chase guard above is blind to copying a LATE buyer: when the
    armed wallet itself bought the top, their_price ≈ our_price and chase_ok passes — but we are
    still buying a coin that ran +101%..+986% since first sighting (every fresh-coin real Solana
    loss in the 2026-07 14d matched window). FAIL-OPEN on unknown prices, same doctrine as
    chase_ok; max_ratio <= 0 disables."""
    try:
        first, now_p, ratio_cap = float(first_price or 0), float(price_now or 0), float(max_ratio or 0)
    except (TypeError, ValueError):
        return True
    if first <= 0 or now_p <= 0 or ratio_cap <= 0:
        return True
    return (now_p / first) <= ratio_cap


@dataclass(frozen=True)
class ResolvedEntryIdentity:
    network_id: str
    status: str
    source: str


def resolve_entry_identity(conn, ev, resolve_fn=None) -> ResolvedEntryIdentity:
    """Resolve canonical entry identity once without overwriting the raw event evidence."""
    mint = str(getattr(ev, "mint", "") or "")
    event_network = str(getattr(ev, "network_id", "") or "").strip()
    if event_network not in ("", "evm-unknown"):
        status = ("resolved" if network_matches_mint_family(event_network, mint)
                  else "mismatch")
        return ResolvedEntryIdentity(event_network, status, "explicit_event")
    if not mint.startswith("0x"):
        return ResolvedEntryIdentity("solana", "resolved", "mint_family")

    networks = m.concrete_event_networks(conn, mint)
    if len(networks) == 1:
        status = ("resolved" if network_matches_mint_family(networks[0], mint)
                  else "mismatch")
        return ResolvedEntryIdentity(networks[0], status, "event_history")
    if len(networks) > 1:
        return ResolvedEntryIdentity("evm-unknown", "ambiguous", "conflicting_history")

    if resolve_fn is None:
        try:
            from sniper.execution.solana_quote import resolve_evm_chain as resolve_fn
        except Exception:
            resolve_fn = None
    if resolve_fn is not None:
        try:
            resolved = str(resolve_fn(mint) or "").strip()
        except Exception:
            logger.exception("chain resolution failed for %s (buy stays unknown -> refused)", mint)
            resolved = ""
        if resolved and resolved != "evm-unknown":
            status = ("resolved" if network_matches_mint_family(resolved, mint)
                      else "mismatch")
            return ResolvedEntryIdentity(resolved, status, "external_resolver")
    return ResolvedEntryIdentity("evm-unknown", "unresolved", "none")


J7_SHADOW_LANE = "j7_shadow"
_WATCHER_HEALTH = {"at": -1e9, "ok": False, "why": "unread"}
_WATCHER_HEALTH_TTL_S = 1.0


def solana_watcher_healthy(now_mono=None, now_wall=None, path=None):
    """True while the Solana on-chain watcher is up (MEMECOIN_J7_SHADOW_WATCHER_MAX_AGE_S).

    Read at most once a second (a small JSON file); every J7 Solana buy in shadow asks. Anything it
    cannot read is UNHEALTHY, which puts J7 back to live: the failure direction of this gate is the
    pre-shadow behaviour. A state change is logged once. It cannot see a socket that is connected but
    silently undelivered (the publicnode shape): the watcher still beats on its receive timeout."""
    max_age = float(getattr(cfg, "MEMECOIN_J7_SHADOW_WATCHER_MAX_AGE_S", 0.0) or 0.0)
    if max_age <= 0:
        return True
    now_mono = time.monotonic() if now_mono is None else now_mono
    cached = _WATCHER_HEALTH
    if path is None and now_mono - cached["at"] < _WATCHER_HEALTH_TTL_S:
        return cached["ok"]
    try:
        from sniper.util.state_paths import state_path
        beat = json.loads((path or state_path("data", "onchain_watch.heartbeat")).read_text())
        ts = datetime.fromisoformat(str(beat["ts"]).replace("Z", "+00:00"))
        age = (now_wall or datetime.now(timezone.utc)) - ts
        subs, failed = int(beat.get("subs") or 0), int(beat.get("subs_failed") or 0)
        if beat.get("connected") is False:
            # The watcher writes a FRESH beat while it backs off between reconnects (2026-09-28),
            # so the age rule alone would read a dark watcher as healthy. Absent = an older
            # watcher, judged by age and subs as before.
            ok, why = False, "disconnected since %s" % beat.get("disconnected_since")
        elif not timedelta(seconds=-5) <= age <= timedelta(seconds=max_age):
            ok, why = False, "heartbeat age %.1fs" % age.total_seconds()
        elif subs <= 0 or failed > 0:
            ok, why = False, "subs %d failed %d" % (subs, failed)
        else:
            ok, why = True, "ok"
    except Exception as exc:
        ok, why = False, "heartbeat unreadable (%s)" % type(exc).__name__
    if path is None:
        if ok != cached["ok"] or cached["why"] == "unread":
            logger.warning("j7 shadow: solana watcher %s (%s); J7 Solana buys %s",
                           "healthy" if ok else "UNHEALTHY", why, "shadow" if ok else "LIVE")
        _WATCHER_HEALTH.update(at=now_mono, ok=ok, why=why)
    return ok


_EVM_WATCHER_HEALTH = {}          # chain -> {"at", "ok", "why", "acked"}; whole-entry writes only
_J7_SHADOW_EVM_WARNED = set()


def _evm_chain_knob(raw, knob):
    """The canonical EVM chain ids named in an EVM chain-list knob's value. Solana (it has its own
    knobs) and unknown names are dropped, an unknown one warned once per knob. Empty = frozenset()."""
    if not raw or not str(raw).strip():
        return frozenset()
    from sniper.services.memecoin_chains import canonical_chain_id
    out = set()
    for part in (p.strip() for p in str(raw).split(",")):
        if not part:
            continue
        canon = canonical_chain_id(part)
        if canon is None:
            if (knob, part) not in _J7_SHADOW_EVM_WARNED:
                _J7_SHADOW_EVM_WARNED.add((knob, part))
                logger.warning("%s: unknown chain %r ignored", knob, part)
            continue
        if str(canon) != "solana":
            out.add(str(canon))
    return frozenset(out)


def _j7_shadow_evm_chains(raw=None):
    """The EVM chains of MEMECOIN_J7_SHADOW_EVM_CHAINS. Empty = no EVM shadow, today."""
    raw = getattr(cfg, "MEMECOIN_J7_SHADOW_EVM_CHAINS", "") if raw is None else raw
    return _evm_chain_knob(raw, "MEMECOIN_J7_SHADOW_EVM_CHAINS")


def _j7_data_only_evm_chains(raw=None):
    """The EVM chains of MEMECOIN_J7_DATA_ONLY_EVM_CHAINS, where J7 never touches a funded decision
    (operator 2026-09-30). Empty = today."""
    raw = getattr(cfg, "MEMECOIN_J7_DATA_ONLY_EVM_CHAINS", "") if raw is None else raw
    return _evm_chain_knob(raw, "MEMECOIN_J7_DATA_ONLY_EVM_CHAINS")


def _evm_beat_path(chain):
    """The EVM watcher's push beat for `chain`; 4663 keeps the unsuffixed name, as the watcher does."""
    from sniper.util.state_paths import state_path
    chain = str(chain)
    return state_path("data", "onchain_evm_watch.heartbeat" if chain == "4663"
                      else "onchain_evm_watch.%s.heartbeat" % chain)


def evm_watcher_healthy(chain, now_mono=None, now_wall=None, path=None):
    """The addresses the EVM watcher's socket has ACKED on `chain` while it is healthy, else None.

    Healthy (spec 2026-09-29-memecoin-j7-shadow-evm-buys-design.md): a readable PUSH beat no older
    than MEMECOIN_J7_SHADOW_EVM_MAX_AGE_S (and at most 5 s in the future), coverage known and not low,
    no suspect log silence on the socket or any subscription, and an acked transfer set present.
    Anything unknown is UNHEALTHY, which keeps J7 live on that chain. Cached 1 s per chain; a state
    change is logged once. It cannot see a dead transfer filter behind a live socket (transfers is
    `no_reference` by design) or a failing tx-read host; those surface as MISS in the hourly read."""
    chain = str(chain)
    now_mono = time.monotonic() if now_mono is None else now_mono
    cached = _EVM_WATCHER_HEALTH.get(chain)
    if path is None and cached is not None and now_mono - cached["at"] < _WATCHER_HEALTH_TTL_S:
        return cached["acked"]
    acked = None
    try:
        beat = json.loads((path or _evm_beat_path(chain)).read_text())
        ts = datetime.fromisoformat(str(beat["ts"]).replace("Z", "+00:00"))
        age = (now_wall or datetime.now(timezone.utc)) - ts
        max_age = float(getattr(cfg, "MEMECOIN_J7_SHADOW_EVM_MAX_AGE_S", 15.0) or 15.0)
        subs = beat.get("subscriptions") if isinstance(beat.get("subscriptions"), dict) else {}
        if beat.get("mode") != "push":
            why = "mode %s" % beat.get("mode")
        elif not timedelta(seconds=-5) <= age <= timedelta(seconds=max_age):
            why = "heartbeat age %.1fs" % age.total_seconds()
        elif beat.get("coverage") is None:
            why = "coverage unknown"
        elif beat.get("coverage_low_since") is not None:
            why = "coverage low since %s" % beat.get("coverage_low_since")
        elif beat.get("log_silence") == "suspect" or any(
                isinstance(v, dict) and v.get("verdict") == "suspect" for v in subs.values()):
            why = "log silence suspect"
        elif not isinstance(beat.get("transfer_addresses"), list):
            why = "no acked transfer set (watcher before 2026-09-29)"
        else:
            why = "ok"
            acked = frozenset(str(a).strip().lower() for a in beat["transfer_addresses"])
    except Exception as exc:
        why = "heartbeat unreadable (%s)" % type(exc).__name__
    ok = acked is not None
    if path is None:
        if cached is None or ok != cached["ok"]:
            logger.warning("j7 shadow: evm watcher %s %s (%s); J7 %s buys %s", chain,
                           "healthy" if ok else "UNHEALTHY", why, chain, "shadow" if ok else "LIVE")
        _EVM_WATCHER_HEALTH[chain] = {"at": now_mono, "ok": ok, "why": why, "acked": acked}
    return acked


def _j7_evm_covered(conn, chain, source_user_id):
    """True when every verified address of the wallet on `chain` (the watcher's own definition) is
    in the transfer filter the healthy watcher acked. Anything unreadable is uncovered (live)."""
    try:
        from sniper.services import axiom_address_verify as axv
        mine = {str(a).lower() for a, (uid, _h) in axv.evm_watched_owners(conn, chain).items()
                if str(uid) == str(source_user_id)}
    except Exception:
        logger.exception("j7 shadow: %s coverage unreadable; the buy stays live", chain)
        return False
    if not mine:
        return False
    acked = evm_watcher_healthy(chain)
    return acked is not None and mine <= acked


def j7_shadow_event(conn, ev):
    """A copy of `ev` tagged delivery_lane='j7_shadow' when this J7 buy only records its decision,
    else None. Solana (MEMECOIN_J7_SOLANA_MODE=shadow): covered = the one definition of "watched"
    (armed_verified_owners), which the slot lane and the confirmed watcher also read; an unreadable
    registry is UNCOVERED and the buy stays live. EVM (MEMECOIN_J7_SHADOW_EVM_CHAINS, 2026-09-29):
    covered = every address of the wallet in evm_watched_owners is in the healthy watcher's acked
    transfer filter (_j7_evm_covered). Sells are never shadowed on either."""
    import copy
    if (getattr(ev, "side", None) != "buy"
            or getattr(ev, "signal_source", None) != "follow_activity"):
        return None
    network = m.normalized_coin_network(getattr(ev, "network_id", None),
                                        getattr(ev, "mint", None) or "")
    if network == "solana":
        if str(getattr(cfg, "MEMECOIN_J7_SOLANA_MODE", "live")) != "shadow":
            return None
        if not getattr(cfg, "MEMECOIN_J7_SOLANA_SHADOW_UNCOVERED", False):
            try:
                from sniper.services.memecoin_address_registry import armed_verified_owners
                covered = {str(v[0]) for v in (armed_verified_owners(conn) or {}).values()}
            except Exception:
                logger.exception("j7 shadow: coverage unreadable; the buy stays live")
                return None
            if str(getattr(ev, "source_user_id", "")) not in covered:
                return None
        if not solana_watcher_healthy():
            return None
    else:
        data_only = _j7_data_only_evm_chains()
        chains = _j7_shadow_evm_chains()
        if not chains and not data_only:
            return None
        from sniper.services.memecoin_chains import canonical_chain_id
        chain = canonical_chain_id(network)
        if chain is None:
            return None
        # DATA ONLY (operator 2026-09-30): a J7 buy on a listed chain shadows whatever the
        # coverage or the watcher's health says. Neither is read, so nothing unknown can make J7
        # live; the watcher and slot lanes are that chain's only entry sources.
        if str(chain) not in data_only:
            if str(chain) not in chains:
                return None
            if not _j7_evm_covered(conn, str(chain), getattr(ev, "source_user_id", "")):
                return None
    tagged = copy.copy(ev)
    tagged.delivery_lane = J7_SHADOW_LANE
    return tagged


def _start_trace_best_effort(conn, ev, decision_at, trace_clock=None):
    caller_owned_transaction = bool(getattr(conn, "in_transaction", False))
    if caller_owned_transaction:
        return None
    try:
        from sniper.services.memecoin_entry_trace import EntryAttemptTrace
        trace = EntryAttemptTrace.start(conn, ev, decision_at, trace_clock or time)
        # MEASUREMENT ONLY (2026-09-28, Alpha GO): the committed shell-row INSERT, the ledger
        # write-lock wait the slot lane's own `attempt_row` exposes. The trace's clock starts
        # before the INSERT, so this mark's elapsed is that write.
        _mark_probe(trace, "attempt_row", **_decision_head_fields(
            ev.get("network_id") if isinstance(ev, dict) else getattr(ev, "network_id", None)))
        _mark_pre_trace_substages(trace, ev)
        return trace
    except Exception:
        try:
            if not caller_owned_transaction and getattr(conn, "in_transaction", False):
                conn.rollback()
        except Exception:
            logger.exception("memecoin entry trace rollback failed")
        logger.exception("memecoin entry trace start failed (funded path unaffected)")
        return None


def _mark_pre_trace_substages(trace, ev):
    """MEASUREMENT ONLY (2026-10-17, Alpha): what a lane measured before this attempt existed (the
    on-chain lane's native_lane_hold, the EVM lane's evm.intent_warm), marked on the attempt trace
    as it starts. In memory; persisted with the trace. MEMECOIN_PRE_TRACE_SUBSTAGES=0: nothing."""
    from sniper.settings import memecoin as cfg
    stages = getattr(ev, "pre_trace_substages", None)
    if trace is None or not stages or not cfg.MEMECOIN_PRE_TRACE_SUBSTAGES:
        return
    for name, fields in stages:
        try:
            trace.mark_substage(name, **fields)
        except Exception:
            logger.exception("pre-trace substage %s failed (funded path unaffected)", name)


def _mark_trace_best_effort(trace, stage):
    if trace is None:
        return False
    try:
        return trace.mark(stage)
    except Exception:
        logger.exception("memecoin entry trace mark failed stage=%s (funded path unaffected)", stage)
        return False


_TRACE_REFUSALS = {
    "not_approved", "skipped_mcap", "stale", "skipped_chase", "disarmed",
    "dropped_base_asset", "ambiguous_network", "unresolved_network", "unroutable_chain",
    "network_mint_mismatch", "rejected", "pre_arm_backlog", "unknown_source", "source_initiation_unknown",
}


def _identity_failure_result(identity):
    if identity.status == "ambiguous":
        status = "ambiguous_network"
    elif identity.status == "mismatch":
        status = "network_mint_mismatch"
    else:
        status = "unresolved_network"
    result = {"status": status, "chain": identity.network_id}
    if status == "network_mint_mismatch":
        result["reason"] = status
    return result


def _trace_terminal(result):
    result = result or {}
    status = str(result.get("status") or "unknown")
    if status in _TRACE_REFUSALS:
        terminal_status = "refused"
        reason = result.get("reason") or result.get("code") or status
    elif status in ("dropped_network", "evm_unarmed"):
        terminal_status = "refused"
        reason = "unsupported_or_unarmed_network"
    elif status == "unsafe":
        terminal_status = "safety_failed"
        reason = result.get("reason") or "unsafe"
    else:
        terminal_status = status
        reason = result.get("reason") or result.get("code") or status
    return terminal_status, str(reason)


def _record_entry_shadows_best_effort(trace, result):
    """Append read-only entry shadows without changing the funded result."""
    if trace is None:
        return False
    try:
        from sniper.services.memecoin_entry_shadow import evaluate_entry_shadows
        payload = evaluate_entry_shadows(
            trace.conn,
            attempt_id=trace.attempt_id,
            current_position_id=(result or {}).get("position_id"),
            result=result,
            entry_context=trace.shadow_context(),
        )
    except Exception:
        logger.exception("memecoin entry shadow evaluation failed (funded result unchanged)")
        payload = {
            "version": "funded_entry_shadow_v1",
            "small_band_v1": {"state": "unknown", "reason": "evaluator_error"},
            "prior_5_negative_v1": {"state": "unknown", "reason": "evaluator_error"},
        }
    try:
        return trace.mark_substage("funded_entry_shadow_v1", **payload)
    except Exception:
        logger.exception("memecoin entry shadow trace failed (funded result unchanged)")
        return False


def _set_entry_shadow_context(trace, ev, trade_size, *, selection_gates_complete=False):
    """Store local entry facts for an observation-only terminal shadow."""
    if trace is None:
        return False
    try:
        return trace.set_shadow_context(
            source_size_usd=getattr(ev, "their_size_usd", None),
            candidate_clip_usd=trade_size,
            selection_gates_complete=selection_gates_complete,
        )
    except Exception:
        logger.exception("memecoin entry shadow context failed (funded result unchanged)")
        return False


def _notify_entry_projection(attempt_id=None, origin_event_id=None):
    """Queue exact identity after the durable result. Never read the ledger here."""
    try:
        from sniper.services.memecoin_telegram import notification_lifecycle_active, enqueue_entry_notification
        if notification_lifecycle_active():
            enqueue_entry_notification(attempt_id=attempt_id, origin_event_id=origin_event_id)
    except Exception:
        logger.exception("memecoin result notification queue failed")


def _finish_trace_for_result(trace, result, *, identity=None):
    trace = getattr(trace, "retry_trace", trace)
    if trace is None:
        return False
    terminal_status, terminal_reason = _trace_terminal(result)
    fields = {}
    if identity is not None:
        fields["network_id"] = identity.network_id
    for key in ("pending_id", "position_id", "fill_id"):
        if (result or {}).get(key) is not None:
            fields[key] = result[key]
    tx_sig = (result or {}).get("sig") or (result or {}).get("tx_hash")
    if tx_sig is not None:
        fields["tx_sig"] = tx_sig
    for key in (
        "quote_price", "quote_observed_at", "quote_age_ms", "quote_id",
        "quote_route_target", "price_attribution", "fill_price", "source_drift_pct",
        "execution_slippage_pct", "total_copy_drift_pct", "gate_inputs_json",
    ):
        if (result or {}).get(key) is not None:
            fields[key] = result[key]
    try:
        _evm_receipt_timing_late_best_effort(trace)
        _momentum_own_late_best_effort(trace)
        _tape_move_late_best_effort(trace)
        _record_entry_shadows_best_effort(trace, result)
        from sniper.services.memecoin_addon_label import record_addon_label
        record_addon_label(trace, result)          # a label at finish, never a gate (never raises)
        changed = trace.finish(terminal_status, terminal_reason, **fields)
        if not getattr(trace, "j7_shadow", False):     # the fast lane's own attempt carries the card
            _notify_entry_projection(trace.attempt_id)
        _record_radar_best_effort(trace)
        return changed
    except Exception:
        logger.exception("memecoin entry trace finish failed (funded result unchanged)")
        return False


def _finish_trace_error_best_effort(trace, exc, *, identity=None):
    trace = getattr(trace, "retry_trace", trace)
    if trace is None:
        return False
    message = str(exc).strip().replace("\n", " ")[:200]
    reason = type(exc).__name__ + (f": {message}" if message else "")
    fields = {"network_id": identity.network_id} if identity is not None else {}
    try:
        _evm_receipt_timing_late_best_effort(trace)
        _momentum_own_late_best_effort(trace)
        _tape_move_late_best_effort(trace)
        _record_entry_shadows_best_effort(trace, None)
        changed = trace.finish("error", reason, **fields)
        _notify_entry_projection(trace.attempt_id)
        _record_radar_best_effort(trace)
        return changed
    except Exception:
        logger.exception("memecoin entry trace error-finalize failed (exception unchanged)")
        return False


def _record_radar_best_effort(trace):
    """Terminal telemetry only; a rating failure cannot alter the funded result.

    OFF BY DEFAULT since 2026-09-22. The radar crons stopped on 2026-08-21 and the Launch radar
    sub-tab is removed, so this had no reader left, yet the funded runner still wrote one row per
    terminal attempt. Set MEMECOIN_ENTRY_RADAR_RATINGS_ENABLED=1 to resume the write.
    """
    if trace is None:
        return None
    from sniper.settings import memecoin as _cfg
    if not getattr(_cfg, "MEMECOIN_ENTRY_RADAR_RATINGS_ENABLED", False):
        return None
    try:
        from sniper.services.memecoin_entry_radar import record_attempt_rating
        return record_attempt_rating(trace.conn, trace.attempt_id)
    except Exception:
        logger.exception("memecoin entry radar write failed (funded result unchanged)")
        return None


def resolve_entry_chain(ev, *, resolve_fn=None):
    """The chain to gate this buy on — RESOLVED, not the raw feed tag.

    The fomo feed omits the chain on ~27% of curated EVM buys, tagging them `evm-unknown`. We must
    resolve the real chain so the buy routes to the right venue and clears the venue guard (a chain
    we have no leg for is refused). `route_real_entry` already carried a resolver for exactly this.

    Measured cost of NOT resolving (raw-tag gate, real_shadow arm, 7d): 12 paper WINNERS refused,
    $640.67 of paper PnL — the second-largest blocker in the funnel.

    Rules, in order:
      * a base58 mint is Solana by construction — the tag is irrelevant, never burn a lookup;
      * a chain the feed NAMED is authoritative and is NEVER second-guessed (a resolver mistake
        must not smuggle a held-out-chain token past the allowlist);
      * only an absent/`evm-unknown` tag on an 0x mint is resolved;
      * unresolvable -> stays unknown and is still refused, but by a decision we can SEE.
    """
    mint = getattr(ev, "mint", "") or ""
    if not mint.startswith("0x"):
        return "solana"
    chain = (getattr(ev, "network_id", "") or "").strip()
    if chain not in ("", "evm-unknown"):
        return chain                       # the feed named it -> authoritative
    if resolve_fn is None:
        try:
            from sniper.execution.solana_quote import resolve_evm_chain as resolve_fn
        except Exception:
            return chain or "evm-unknown"
    try:
        return (resolve_fn(mint) or "").strip() or (chain or "evm-unknown")
    except Exception:
        logger.exception("chain resolution failed for %s (buy stays unknown -> refused)", mint)
        return chain or "evm-unknown"


def _real_decision_reason(res):
    """A human one-liner for the real-leg outcome recorded on the event (WS2). Most gate results
    already carry an explicit `reason`; synthesize one for the early-return gates that don't."""
    if not res:
        return None
    if res.get("reason"):
        return res["reason"]
    st = res.get("status")
    if st == "unroutable_chain":
        return f"no configured leg for chain {res.get('chain')}"
    if st == "not_approved":
        return f"wallet not armed (chain {res.get('chain')})"
    if st == "unknown_source":
        return "source identity not found"
    if st == "skipped_mcap":
        return f"mcap ${res.get('mcap')}" if res.get("mcap") is not None else "mcap gate"
    if st == "filled":
        n = res.get("notional")
        return f"filled ${n:.2f}" if isinstance(n, (int, float)) else "filled"
    return None


def _record_real_decision(conn, ev, res, *, decision_at):
    """WS2: stamp the real-leg outcome onto the originating event row so the Activity/Decisions
    surface shows what the algo actually did with each buy (never overwrites the paper `action`).
    Best-effort — a recording failure must never break the copy/real path."""
    if not res or not res.get("status"):
        return
    fid = getattr(ev, "fomo_event_id", None)
    try:
        m.set_event_real_decision(
            conn, fid, res["status"], reason=_real_decision_reason(res), now_iso=decision_at,
        )
        # unroutable_chain returns from the hook BEFORE the leg's drop-counter runs, so the cockpit
        # route-drops KPI never saw it — count it here for parity with dropped_network.
        if res["status"] == "unroutable_chain":
            m.record_network_drop(conn, res.get("chain") or "evm-unknown",
                                  getattr(ev, "mint", None), "unroutable_chain",
                                  getattr(ev, "received_at", None) or getattr(ev, "created_at", None))
    except Exception:
        logger.exception("real decision recording failed for event %s", fid)


def _default_blind_mcap_fn():
    """Lazy GT-price x on-chain-totalSupply mcap lookup for DexScreener-blind chains (4663).
    Built once, best-effort: any failure returns None (the gate then falls to assume-micro or
    the conservative skip). Uses the SAME battle-tested factories the position marks use."""
    state = {}

    def lookup(chain, mint):
        try:
            if "mcap_for" not in state:
                from sniper.services.memecoin_evm_price import (
                    build_evm_price_for, build_evm_mcap_for)
                state["price_for"] = build_evm_price_for()
                state["mcap_for"] = build_evm_mcap_for()
            priced = state["price_for"](chain, mint)
            price = priced[0] if isinstance(priced, tuple) else priced
            if not price or price <= 0:
                return None
            return state["mcap_for"](chain, mint, price)
        except Exception:
            logger.exception("blind-chain live mcap lookup failed chain=%s mint=%s", chain, mint)
            return None

    return lookup


_EVM_SUPPLY_POOL = None                 # created on first use; see _start_evm_supply_read


def _start_evm_supply_read(chain, mint):
    """E1 on EVM (2026-09-29): start the coin's on-chain totalSupply read on its own small pool, so
    it runs BESIDE the market-data GET instead of after it. The future answers
    memecoin_evm_price.evm_total_supply's (supply, cache_hit, read_ms). None if it cannot start.
    ponytail: 4 workers; a burst of new coins queues, and a queued read past the wait refuses."""
    global _EVM_SUPPLY_POOL
    try:
        if _EVM_SUPPLY_POOL is None:
            from concurrent.futures import ThreadPoolExecutor
            _EVM_SUPPLY_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="evm-supply")
        from sniper.services import memecoin_evm_price as _ep
        return _EVM_SUPPLY_POOL.submit(lambda: _ep.evm_total_supply(chain, mint))
    except Exception:
        logger.exception("evm supply read not started chain=%s mint=%s", chain, mint)
        return None


_MOMENTUM_POOL = None                   # created once under the lock; see _momentum_read_pool
_MOMENTUM_POOL_LOCK = threading.Lock()


def _momentum_read_pool():
    global _MOMENTUM_POOL
    with _MOMENTUM_POOL_LOCK:
        if _MOMENTUM_POOL is None:
            from sniper.services.memecoin_shadow_reads import ShadowReads
            _MOMENTUM_POOL = ShadowReads(workers=2, maxsize=32)
        return _MOMENTUM_POOL


def _read_momentum_stamped(chain, mint):
    from sniper.services.memecoin_volume_writer import read_momentum
    try:
        out = read_momentum(chain, mint)
    except Exception:
        logger.exception("own momentum read failed chain=%s mint=%s", chain, mint)
        out = {"reason": "read_failed", "points": []}
    out["ready_mono"] = time.monotonic()
    return out


def _read_solana_momentum_stamped(mint):
    from sniper.services.memecoin_sol_momentum import read_momentum
    try:
        out = read_momentum(mint)
    except Exception:
        logger.exception("solana own momentum read failed mint=%s", mint)
        out = {"reason": "read_failed", "points": []}
    out["ready_mono"] = time.monotonic()
    return out


def _start_momentum_read(chain, mint):
    """E14 SHADOW (spec 2026-10-08 funded momentum own data): start the coin's own momentum read
    beside the market-data GET (on the slot lane, at the buy's decode). Returns the holder the
    classifier peeks, or None on a chain with no own source. 4663/8453 read the own tape (measured
    p50 3.8 ms, p90 4.7 ms); Solana reads our own marks on the mark tape (step 0 of spec 2026-10-09
    Solana pre-signal feed; p50 1.97 ms, p90 2.02 ms). Nothing waits on either."""
    if str(chain) == "solana":
        # No import here: the reader imports on the worker, never on the slot lane's decode.
        task = (_read_solana_momentum_stamped, mint)
    else:
        from sniper.services.memecoin_volume_writer import MOMENTUM_CHAINS
        if str(chain) not in MOMENTUM_CHAINS:
            return None
        task = (_read_momentum_stamped, str(chain), mint)
    try:
        return {"future": _momentum_read_pool().submit(*task),
                "classified_mono": None, "peeked": None}
    except queue.Full:
        return {"future": None, "classified_mono": None, "peeked": None, "reason": "queue_full"}
    except Exception:
        logger.exception("own momentum read not started chain=%s mint=%s", chain, mint)
        return None


def _momentum_own_peek(holder):
    """The own read if it is ALREADY done, else `read_not_ready`. NEVER WAITS (Gamma, required):
    a lane that classifies without the GET must not pay for a shadow. `wait_would_ms` is how long
    a switched path would have waited, here 0; a late read reports it on the trace at finish."""
    if not holder:
        return None
    now = time.monotonic()
    holder["classified_mono"] = now
    future = holder.get("future")
    if future is None:
        holder["peeked"] = "not_started"
        return {"reason": holder.get("reason", "read_not_ready"), "wait_would_ms": 0.0}
    if not future.done():
        holder["peeked"] = "not_ready"
        return {"reason": "read_not_ready"}
    holder["peeked"] = "ready"
    try:
        out = dict(future.result(timeout=0) or {})
    except TimeoutError:
        return {"reason": "queue_expired", "wait_would_ms": 0.0}
    except Exception:
        return {"reason": "read_failed", "wait_would_ms": 0.0}
    ready = out.pop("ready_mono", now)
    out["wait_would_ms"] = max(0.0, (ready - now) * 1000.0)
    return out


def _momentum_own_late_best_effort(trace):
    """At trace finish, on the attempt's own thread: a read that was not ready at classification
    lands as the `momentum_own_late` substage with the wait a switched path would have paid."""
    holder = getattr(trace, "momentum_own", None) if trace is not None else None
    if not holder or holder.get("peeked") != "not_ready":
        return False
    try:
        from sniper.services.memecoin_market_state import own_momentum_facts
        classified = holder.get("classified_mono")
        future = holder.get("future")
        if future is None or not future.done():
            return trace.mark_substage(
                "momentum_own_late", reason="not_ready_at_finish",
                wait_would_ms_min=(time.monotonic() - classified) * 1000.0)
        try:
            read = dict(future.result(timeout=0) or {})
        except TimeoutError:
            read = {"reason": "queue_expired"}
        except Exception:
            read = {"reason": "read_failed"}
        ready = read.pop("ready_mono", None)
        facts = own_momentum_facts(read)
        facts["wait_would_ms"] = (max(0.0, (ready - classified) * 1000.0)
                                  if ready is not None and classified is not None else None)
        return trace.mark_substage("momentum_own_late", **facts)
    except Exception:
        logger.exception("own momentum late mark failed")
        return False


def _mark_evm_own_supply(trace, name, **fields):
    """The own stage's measurement on the attempt's trace (Alpha b). Never decides anything."""
    if trace is None:
        return
    try:
        trace.mark_substage(name, **fields)
    except Exception:
        logger.exception("evm own supply mark failed")


def _cached_evm_source_mcap(chain, mint, price, trace=None, *, live_max_age_s=LIVE_SUPPLY_MAX_AGE_S):
    """Source USD price × fresh background-verified supply; no RPC or lock wait on entry.

    `live_max_age_s` bounds a fast lane's own supply read (the volume store's own bound is fixed):
    the band source keeps the lanes' TTL, the over-ceiling guard alone accepts an older read."""
    started = time.monotonic()
    px = positive_price(price)
    evidence = (read_cached_supply(chain, mint) if px is not None
                else {"supply": None, "outcome": "no_source_price", "observed": None})
    if px is not None and evidence["supply"] is None:
        # The volume store holds Pons curves only; a fast lane's own verified read covers the rest.
        live = read_live_supply(chain, mint, max_age_s=live_max_age_s)
        if live["supply"] is not None:
            evidence = dict(live, outcome="lane_verified")
    mcap = px * evidence["supply"] if px is not None and evidence["supply"] is not None else None
    outcome = evidence["outcome"]
    if mcap is not None and (not math.isfinite(mcap) or mcap <= 0):
        mcap, outcome = None, "invalid_product"
    _mark_evm_own_supply(trace, "evm_cached_supply", outcome=outcome,
                         observed_at=evidence["observed"], mcap=mcap,
                         read_ms=round((time.monotonic() - started) * 1000, 3))
    return mcap


def _evm_own_band_mcap(future, price, trace=None):
    """Our own EVM band mcap: the source fill `price` x the future's on-chain supply, or None.

    Called once the GET has returned, it waits at most MEMECOIN_EVM_OWN_MCAP_WAIT_MS for the read.
    A read that is not done, failed, or found no supply is None (the caller refuses
    mcap_unresolved), never a stale or borrowed supply. It records outcome, the read's own ms,
    the cache hit and how long THIS stage waited, so the wait is measured, not assumed."""
    import concurrent.futures as _cf
    wait_s = max(0.0, float(getattr(cfg, "MEMECOIN_EVM_OWN_MCAP_WAIT_MS", 150.0) or 0.0)) / 1000.0
    t0 = time.monotonic()
    supply = hit = read_ms = None
    if future is None:
        outcome = "not_started"
    else:
        try:
            supply, hit, read_ms = future.result(timeout=wait_s)
            outcome = "ok"
        except _cf.TimeoutError:
            outcome = "wait_expired"
        except Exception:
            logger.exception("evm own supply read failed")
            outcome = "read_failed"
    waited_ms = round((time.monotonic() - t0) * 1000.0, 1)
    mcap = None
    if outcome == "ok":
        try:
            px = float(price) if price is not None else 0.0
        except (TypeError, ValueError):
            px = 0.0
        if not supply or not supply > 0:
            outcome = "no_supply"
        elif not (math.isfinite(px) and px > 0):
            outcome = "no_price"
        else:
            mcap = px * float(supply)
            if not (math.isfinite(mcap) and mcap > 0):
                mcap, outcome = None, "no_price"
    _mark_evm_own_supply(trace, "evm_own_supply", outcome=outcome, read_ms=read_ms,
                         cache_hit=hit, waited_ms=waited_ms, mcap=mcap)
    return mcap


def _default_evm_source_mcap_fn():
    """Last-resort EVM mcap on a DexScreener-covered chain: on-chain totalSupply x the source fill.

    C3S (BSC, 2026-09-17): the feed sent market cap 0, the coin had no same-mint history, and its
    only DexScreener pair was quoted in another memecoin with no price, so the gate refused
    mcap_unresolved. The trader's own fill price is a real USD price at the signal, and supply is
    one cached ERC-20 read through the factory the position marks use. None on any failure, so the
    gate keeps its old outcome."""
    state = {}

    def lookup(chain, mint, price):
        try:
            if not price or float(price) <= 0:
                return None
            if "mcap_for" not in state:
                from sniper.services.memecoin_evm_price import build_evm_mcap_for
                state["mcap_for"] = build_evm_mcap_for()
            return state["mcap_for"](chain, mint, float(price))
        except Exception:
            logger.exception("evm source-priced mcap lookup failed chain=%s mint=%s", chain, mint)
            return None

    return lookup


def _onchain_source_mcap(price, read):
    """The source's fill price x the mint's on-chain supply, from a token-safety mint read
    `(value, failure)`; None when either is missing or unusable. Pure."""
    try:
        val, failure = read
        if failure is not None or not isinstance(val, dict):
            return None
        info = val["data"]["parsed"]["info"]
        supply, decimals = int(info["supply"]), info["decimals"]
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 18:
            return None
        price = float(price)
        if supply <= 0 or not math.isfinite(price) or price <= 0:
            return None
        mcap = price * supply / 10 ** decimals
        return mcap if math.isfinite(mcap) and mcap > 0 else None
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _onchain_band_mcap(ev, contexts, pre=None):
    """Stage 3 of the Solana band mcap when MEMECOIN_SOLANA_BAND_MCAP_BASIS=onchain (2026-09-27).

    The mint read is the TOKEN-SAFETY read, moved ahead of the band: a successful one is kept on the
    event as `mint_account_read`, and the leg's safety gate judges it instead of reading again. A
    failed read is never kept (the gate reads fresh, as before). No source price means no read."""
    price = getattr(ev, "their_price", None)
    try:
        if price is None or not float(price) > 0:
            return None
    except (TypeError, ValueError):
        return None
    ctx = context_for(contexts, "solana")
    read_fn = ctx.get("mint_read_fn") if isinstance(ctx, dict) else None
    if read_fn is None:
        return None
    # B (2026-09-27): the read may already be running beside the GET. Its outcome is used exactly
    # as a fresh one would be; only a read that did not finish (or never ran) is taken fresh here.
    read = pre.mint_read(getattr(ev, "mint", None)) if pre is not None else _READ_TIMED_OUT
    if read is None:
        return None
    if read is _READ_TIMED_OUT:
        try:
            read = read_fn(ev.mint)
        except Exception:
            logger.exception("band mint read failed mint=%s", getattr(ev, "mint", None))
            return None
    if isinstance(read, tuple) and len(read) == 2 and read[1] is None and read[0] is not None:
        try:
            ev.mint_account_read = read
        except (AttributeError, TypeError):
            pass                              # an immutable event: the gate simply reads again
    return _onchain_source_mcap(price, read)



def _mint_supply_atomic(read):
    """The mint's raw supply from a token-safety mint read `(value, failure)`, or None."""
    try:
        val, failure = read
        if failure is not None or not isinstance(val, dict):
            return None
        supply = int(val["data"]["parsed"]["info"]["supply"])
        return supply if supply > 0 else None
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _route_band_mcap(conn, ev, contexts, trace, pre):
    """E1 route stage on Solana (spec 2026-09-28-memecoin-e1-solana-own-mcap-design.md, Alpha's
    amendment): OUR entry quote at the clip the reservation will size, times the supply from the mint
    read the prereads already started. The quote is handed to the executor as this attempt's early
    quote (same seam, same clip, same trace), so the entry pays ONE quote, not two. Supply and
    tokens out are both raw atoms of this mint, so decimals cancel: mcap = supply x clip / out.

    None, and nothing handed, when anything is missing: no prereads or trace, an unfinished or
    failed mint read, no quote seam, a sizing refusal, a quote without outAmount, or an event that
    already carries an early quote. The caller then falls back to executable_mcap, as before."""
    try:
        # A SHADOW DECISION QUOTES NOTHING (Gamma M1): it keeps today's executable_mcap path exactly.
        if pre is None or trace is None or getattr(ev, "delivery_lane", None) == J7_SHADOW_LANE:
            return None
        # Only THIS attempt's early quote blocks the stage (Gamma L1): a retry can carry an earlier
        # attempt's object, already taken, which would otherwise send it to a second quote.
        held = getattr(ev, "entry_early_quote", None)
        if held is not None and getattr(held, "trace", None) is trace:
            return None
        read = pre.mint_read(getattr(ev, "mint", None))
        supply = None if read is _READ_TIMED_OUT or read is None else _mint_supply_atomic(read)
        if supply is None:
            return None
        sol = context_for(contexts, "solana")
        quote_fn = sol.get("entry_quote_fn") if isinstance(sol, dict) else None
        from sniper.persistence.db_core import _connection_db_path
        db_path = _connection_db_path(conn) if conn is not None else None
        if quote_fn is None or not db_path:
            return None
        from sniper.services import memecoin_early_quote as eq
        early = eq.EarlyEntryQuote(mint=ev.mint, quote_fn=quote_fn, trace=trace, start=False,
                                   sizing_fn=lambda: _early_quote_clip(ev, pre, db_path))
        early.run()                                   # on this thread: the stage needs the answer
        out = int((early.quote or {}).get("outAmount") or 0)
        if not early.clip or out <= 0:
            return None
        mcap = supply * float(early.clip) / out
        if not (math.isfinite(mcap) and mcap > 0):
            return None
        ev.entry_early_quote = early                   # the executor takes it like any early quote
        return mcap
    except Exception:
        logger.exception("route band mcap failed mint=%s", getattr(ev, "mint", None))
        return None

def _default_solana_mcap_fn():
    """Last-resort solana mcap: OUR route price x on-chain supply. Best-effort, None on any
    failure, so the gate still falls through to assume-micro exactly as before.

    Only reached when the feed, same-mint history AND DexScreener have all produced nothing --
    i.e. a provider outage or a mint too fresh to be indexed. It costs one cached getTokenSupply
    plus one quote, and in normal operation it never runs at all."""
    state = {}

    def lookup(mint):
        try:
            if "rpc" not in state:
                from sniper.settings.memecoin import MEMECOIN_SOLANA_RPC_URL
                state["rpc"] = MEMECOIN_SOLANA_RPC_URL
            if not state["rpc"]:
                return None
            from sniper.execution.solana_real_seams import executable_mcap
            return executable_mcap(mint, state["rpc"])
        except Exception:
            logger.exception("solana executable-mcap lookup failed mint=%s", mint)
            return None

    return lookup


# These funded entry outcomes are transient. Each outcome releases the reservation before a retry.
# All other outcomes stop the retry.
_UNRESOLVED_SIGNED_PREFIX = "unresolved signed transaction on "   # memecoin_real_leg's guard
_IN_PROGRESS_REFUSAL = "one position per coin: another entry attempt was in progress at decision time"


def _reservation_entry_miss(res):
    """A known reservation is a temporary collision; the retry reruns this event's own gates."""
    blocker = (res or {}).get("duplicate_blocker") or {}
    return ((res or {}).get("status") == "rejected" and (res or {}).get("reason") == _IN_PROGRESS_REFUSAL
            and isinstance(blocker, dict) and blocker.get("kind") == "reservation"
            and type(blocker.get("reservation_id")) is int and blocker["reservation_id"] > 0)

_RETRY_TRANSIENT = {
    ("aborted", "no quote"),
    ("aborted", "token decimals unavailable"),
    ("route_failed", "no planned-quantity sell route"),
    ("rejected", "quote balance unavailable"),
    # THE SOLANA SIDE OF THE SAME UNKNOWN (2026-09-27). Solana passes a failed bankroll read to the
    # risk gate as None, which refuses 'USDC balance unavailable' ("refuse honestly and retryably").
    # It was not here, so the 12:33Z outage buy was never re-asked, and the watcher's later
    # detection of it was dropped as duplicate_of_feed: dead on every lane. The gate read it
    # re-runs is bounded (_GATE_BALANCE_DEADLINE_S). A real $0 refuses with another reason.
    ("rejected", "USDC balance unavailable"),
    ("rejected", "native balance read failed (rpc) — refusing fail-closed, balance unknown"),
    ("unsafe", "safety_exception: safety check timed out"),
    ("unsafe", "safety_deadline_exceeded"),
    ("unsafe", "code read failed (fail-closed)"),
    # THE NODE'S STATE, NOT THE COIN'S (MCAT, 2026-09-23). A transport failure, a JSON-RPC error
    # body and an account our node cannot see yet each refuse the entry and each say nothing about
    # the token, so the lane re-asks them on its own cadence and every gate runs again. The fourth
    # outcome of that read, `mint account unparseable (fail-closed)`, IS about the token and is
    # deliberately absent from this set.
    ("unsafe", "mint read failed (rpc) — fail-closed"),
    ("unsafe", "mint read rejected by node (fail-closed)"),
    ("unsafe", "mint account not visible yet (fail-closed)"),
    ("unsafe", "sell simulation failed (fail-closed)"),
    # A CONFIRMED revert with NO reason at all. This was written for the Solana revert
    # (Jupiter 6001 slippage, J61gb… 2026-08-17) on the belief that only Solana returned a bare
    # `failed` — and one week later solana_swap_executor started keeping the chain's own words, so
    # the reason is never None and this stopped matching anything. The slippage class it existed
    # to serve is matched positively below; this stays for an older seam that still returns bare.
    ("failed", None),
}


def _is_transient_entry_miss(res):
    if not res:
        return False
    # A ROUTE PRICED AND DECLINED ON COST IS AN ANSWER, NOT A MISS (2026-09-13, OP829 2e3b7a5d).
    # `route_over_budget` is reported only when every rung that ran PRICED the route (a blind rung
    # keeps its provider_* code and still retries), so re-asking gets the same fees back: OP829
    # re-quoted six times in 48 s at -11.79% .. -11.83% against the 10% budget, and the operator's
    # alert waited 51 s for the last one.
    # A MARGINAL MISS IS ONE READ OF A MOVING COST (2026-09-17, SKIBIDI 0x38931deb -15.09% against
    # the 15% budget, -13.35% on the same route thirteen minutes later). The venue names it
    # separately, so only that class re-asks; a priced refusal beyond the margin stays one-shot.
    if "route_over_budget_marginal" in str(res.get("reason") or ""):
        return True
    if (res.get("quote_miss_code") == "route_over_budget"
            or str(res.get("reason") or "").startswith("no quote (route_over_budget")):
        return False
    # A ROUTE THROUGH A DRAINED CURVE IS A ROUTING GAP, NOT A VERDICT ON THE COIN (2026-09-23).
    # GIGACAT graduated seven minutes after launch and Jupiter had not indexed the new AMM pool,
    # so its only route ran through the bonding curve (0.00141732 SOL) and priced $175 at 87.16%
    # impact. The SAME $175 quoted at 0.0468% through the AMM pool once Jupiter found it -- and
    # that pool already existed when we refused. This is the class the re-quote lane exists for:
    # one instant's read of a route that is about to change, on the existing 6 s / 60 s cadence.
    if str(res.get("reason") or "").startswith("route_via_completed_curve"):
        return True
    if (res.get("status"), res.get("reason")) in _RETRY_TRANSIENT:
        return True
    # The gate's unknown quote balance now names what its read met (2026-10-07); every variant is
    # the same transient class as the bare string in _RETRY_TRANSIENT above.
    if res.get("status") == "rejected" and str(res.get("reason") or "").startswith(
            _QUOTE_BALANCE_UNAVAILABLE + " ("):
        return True
    # The typed EVM quote miss (671dfb39) reads "no quote (no_route: ...)" — the exact-pair set
    # silently stopped matching it, so EVM fresh-pool misses never retried (KARMA 0xc9ce5584,
    # 2026-08-17: GT indexed the V4 pool minutes after the one-shot refusal). Every "no quote"
    # variant is one instant's read of the route ladder — the class the re-quote lane exists for.
    status = res.get("status")
    reason = str(res.get("reason") or "")
    if status == "aborted" and reason.startswith("no quote"):
        return True
    # OUR OWN IN-FLIGHT TRANSACTION IS NOT A VERDICT ON THE COIN (2026-09-28). The re-ask meets the
    # same chain-wide guard, so it cannot sign while the blocker is unresolved or ambiguous; if the
    # blocker filled this coin, "one position per coin" ends it. See MEMECOIN_ENTRY_RETRY_UNRESOLVED_S.
    if (status == "rejected" and reason.startswith(_UNRESOLVED_SIGNED_PREFIX)) or _reservation_entry_miss(res):
        return float(getattr(cfg, "MEMECOIN_ENTRY_RETRY_UNRESOLVED_S", 0) or 0) > 0
    # A BUILD REFUSED BEFORE SIGNING IS ONE INSTANT'S BUILD (2026-09-12). PENIS (JE3HT7, Token-2022,
    # 3% transfer fee) aborted at 18:18:49Z with "route build refused: Raydium simulation refused the
    # Token-2022 buy" (Raydium router custom 0x26) while the coin ran 1.35x over its chart price.
    # The same buy built and simulated clean on BOTH Jupiter and Raydium 40 minutes later, and the
    # J7 copy of the same signal 1.4 s later was refused only because this attempt still held the
    # reservation. Nothing was signed, so a re-quote costs a quote and a simulation, and every gate
    # runs again; the lane's window and attempt cap bound it.
    if status == "aborted" and reason.startswith("route build refused"):
        return True
    # A SLIPPAGE REVERT IS A MOVED PRICE, NOT A REFUSED ROUTE (2026-09-08).
    # `("failed", None)` above was the only path here for a confirmed revert and it has matched
    # nothing since 2026-08-24, so no Solana revert has re-quoted in two weeks. Match the class
    # positively instead, and ONLY this class: a minimum-out failure means the route was still
    # valid and the price moved under it, which is exactly what a re-quote fixes. Every other
    # InstructionError is a program refusing the route on its merits — a mint constraint, an
    # unsupported token program, insufficient funds — and re-quoting the same venue refuses
    # identically, so those stay one-shot.
    #
    # Matched on the chain's own words, never on a numeric code: 0x1771 IS Jupiter's slippage
    # error, but 6001 in another Anchor program means something else, and guessing which program
    # emitted a bare code is how a mint-constraint refusal becomes a retry loop. A revert whose
    # program logged only a code therefore stays one-shot. Under-retrying is the safe direction.
    #
    # The re-attempt is bounded by the retry window and interval, and the chase cap runs again on
    # the new quote, so a coin running away is refused rather than followed up.
    if status == "failed" and "slippage" in reason.lower():
        return True
    # A VENUE THAT REFUSED ON CHAIN, NOW EXCLUDED (2026-09-13, BURNED 8a545100). The paragraph
    # above keeps program refusals one-shot because re-quoting the same venue refuses identically.
    # `venue_excluded` means that venue is now excluded for this coin, so the re-quote is a
    # different route by construction. The transaction reverted, so no funds moved; the window,
    # the attempt cap and every gate on the new quote bound it.
    if status == "failed" and res.get("venue_excluded"):
        return True
    # `sell_sim_rpc_unknown` is the sell simulation's "transport/RPC truth unavailable" status
    # (`_rpc_eth_call_result` returned None), the same class as "sell simulation failed" above.
    # It carries a long fixed suffix, so match the prefix. `sell_sim_reverted_unknown` stays
    # one-shot: that is the CHAIN answering, not the wire.
    if status == "unsafe" and reason.startswith("sell_sim_rpc_unknown"):
        return True
    # A REPEATED SELL-SIMULATION MIN-OUT IS A MOVED PRICE, NOT AN UNSELLABLE COIN (2026-09-17).
    # The router ran the sell to its final minimum check, so the token moved. FOMOX 0x3f439a39
    # (DegenCapitalLLC, 15:31:35Z) refused this way while the custody RPC was quarantined and the
    # coin was 1.58x its first-seen price; the same funded check passed clean 30 minutes later, and
    # holder->PoolManager transfers showed no transfer tax. A real sell tax misses the 8% minimum
    # on every re-attempt, so it stays refused; the retry window and attempt cap bound it, and
    # every gate runs again on the new quote.
    if status == "unsafe" and reason.startswith("sell_sim_min_out_repeated"):
        return True
    # AN EXECUTION-PREMIUM REFUSAL IS ONE INSTANT'S ROUTE, NOT A VERDICT ON THE COIN (2026-09-04).
    # The premium measures OUR quote against the CHART, so a refusal says the only route we could
    # find was priced far away from the market -- which on a fresh 4663 coin is usually a statement
    # about which pools are visible yet, not about the coin. Measured: this chain's log index does
    # not surface a new pool's Initialize for 38-61s, so a coin whose only route at t+5s is a thin
    # two-hop can have a sane DIRECT pool minutes later. X (2026-09-04) quoted at 7.3x the chart
    # through the only route in existence and stopped out at -90.9%.
    #
    # Re-queueing costs nothing and risks nothing: the retry re-runs the WHOLE decision, so the
    # premium cap must pass on the new quote before anything fills. A coin that never gets a sane
    # route is refused at the end of the window exactly as it is refused now.
    #
    # KNOBBED OFF BY THE OPERATOR (2026-09-24, STOOK 64575 -$101.57). This class differs from every
    # other one here: the refusal is about PRICE, not about route visibility, so a re-queue follows
    # a coin that has already run away. STOOK refused at +47% and +38% against a 35% cap and filled
    # 22 s later at the third attempt. The knob is read from the settings module at call time so
    # the funded path and a test see the same value; its code default is 1, i.e. this branch keeps
    # today's behaviour anywhere the env var is absent.
    if str(reason).startswith("exact quote is ") and "above source price" in reason:
        return bool(getattr(cfg, "MEMECOIN_REAL_CHASE_RETRY_ENABLED", True))
    # `no_route_all_venues` IS THE SAME CLASS, AND IT WAS THE ONLY ONE STILL TERMINAL (2026-09-04).
    # It means every venue was asked and none had a route AT THAT INSTANT. On a fresh 4663 coin
    # that is a statement about which pools are VISIBLE yet, not about the coin: this chain's log
    # index does not surface a new pool's Initialize for 38-61s, and both indexers need minutes to
    # list a mint, while a copy signal arrives seconds after the wallet's buy.
    #
    # The evidence is already written down twice in this repo and was never wired to the retry
    # lane. evm_v4_multihop's own note records GPU refused `no_route_all_venues` at 10:21 with the
    # very same route quoting fine fourteen minutes later. CHILL (0x5c3a0e5f, DegenCapitalLLC)
    # repeated it today: aborted at 18:10:51, and its native-paired 10% pool 0x91e4bccd quoted
    # 0.01 ETH into 111,801 tokens minutes afterwards.
    #
    # Same safety argument as the premium retry above: the retry re-runs the WHOLE decision, so
    # safety, the premium cap, entry max-age and every other gate must pass on the new quote before
    # anything fills. A coin that never finds a route is refused at the end of the window exactly
    # as it is refused now.
    if status == "aborted" and reason == "no_route_all_venues":
        return True
    if status == "route_failed":
        from sniper.execution.evm_token_safety import EVM_ROUTE_UNAVAILABLE_PREFIX
        if reason.startswith(EVM_ROUTE_UNAVAILABLE_PREFIX):
            return True
        # Two simulation reverts are statements about a STALE ROUTE, not about the coin. Both are
        # retryable for the same reason, and the retry re-runs the whole decision, so a coin that
        # is genuinely unroutable is refused again at the end of the window, cheaply.
        #
        # PoolNotInitialized() (0x486aa307) is the simulating node's VIEW of the pool: 69ELEVEN
        # 0xbc191b1d was refused this way at 22:25:38 on 2026-09-04 for a pool initialised at
        # 22:10:07, and the identical route simulated OK on two members ten minutes later.
        #
        # CurveGraduated() (0x025ac17e) is the Pons bonding curve reporting that it graduated
        # between our quote and our simulation. VLAD 0xd7bac75b (DegenCapitalLLC) died this way on
        # 2026-09-08: the pons_v2 rung quoted twice at round_trip -5.87%, the first simulation
        # reverted on min-out with the quote price 23.8% over source, the second reverted
        # CurveGraduated() 1.1s later with it 38.9% over. A graduated coin has a REAL pool, so the
        # next pass is the one that can find it on v2/v3/v4 — refusing outright throws the coin
        # away at the exact moment it becomes routable.
        return (reason.startswith("entry_simulation_reverted")
                and any(sel in reason for sel in ("0x486aa307", "0x025ac17e")))
    return False


_GRADUATION_NO_ROUTE_REASONS = frozenset({
    "no quote (no_route: jupiter 400 NO_ROUTES_FOUND)",
    "no quote (no_route: jupiter 400 NO_ROUTES_FOUND; raydium ROUTE_NOT_FOUND)",
})


def _entry_retry_delay(res, interval_s, *, attempts=0, network_id=None,
                      slot_lane_failure_reason=None):
    """Use the known GT cooldown for the first two provider-rate retries. An RPC 429 waits longer.

    RPC RATE LIMIT (2026-09-14). The GT cooldown is 3 s, which suits GeckoTerminal. An RPC 429 is
    the custody endpoint's quota, shared with every exit read; PLCANARY re-quoted 11 times in 3
    minutes into it (about 15 reads per ladder). It waits MEMECOIN_ENTRY_RETRY_RPC_429_S instead."""
    reason = str((res or {}).get("reason") or "")
    if reason.startswith(_UNRESOLVED_SIGNED_PREFIX) or _reservation_entry_miss(res):
        from sniper.settings import memecoin as cfg
        return float(getattr(cfg, "MEMECOIN_ENTRY_RETRY_UNRESOLVED_S", interval_s) or interval_s)
    if "provider_rpc_rate_limit" in reason:
        from sniper.settings import memecoin as cfg
        return max(float(interval_s), float(getattr(cfg, "MEMECOIN_ENTRY_RETRY_RPC_429_S", 20.0)))
    # A MARGINAL ROUND-TRIP MISS IS ONE READ OF A MOVING COST (73849, 2026-10-14: 56 Flap priced
    # 15.80% against the budget, filled on the generic 6 s re-ask 21 blocks late). Re-ask it after
    # MEMECOIN_ENTRY_RETRY_MARGINAL_S for the first three attempts, then the normal interval. The
    # same gates judge every re-ask; 0 restores the interval. A 429 above keeps its own longer wait.
    if "route_over_budget_marginal" in reason and int(attempts) < 3:
        from sniper.settings import memecoin as cfg
        fast = float(getattr(cfg, "MEMECOIN_ENTRY_RETRY_MARGINAL_S", 0.0) or 0.0)
        if 0 < fast < float(interval_s):
            return fast
    # INSTINCT (7f2098c3): Pump completed before our S+1 send; its replacement pool was created
    # three slots later. The immediate fallback saw NO_ROUTES_FOUND, then waited eight seconds.
    # Only a conclusive graduation + known clean no-route answers gets three one-second worker
    # re-quotes (enqueue, then the first two reschedules). `no_route` alone is not proof: the
    # classifier also emits it when Jupiter missed but Raydium timed out or returned 429.
    if str(network_id or "").lower() in {"solana", "sol"}:
        from sniper.services.memecoin_onchain_lane import PUMP_GRADUATION_REASON
        if (slot_lane_failure_reason == PUMP_GRADUATION_REASON and int(attempts) < 2
                and (res or {}).get("status") == "aborted"
                and reason in _GRADUATION_NO_ROUTE_REASONS):
            return min(float(interval_s), 1.0)
        return float(interval_s)
    # `provider_discovery_cooldown` IS the GT cooldown (typed provider_rate_limit before 2026-09-28).
    if int(attempts) < 2 and ("provider_rate_limit" in reason
                              or "provider_discovery_cooldown" in reason):
        from sniper.settings import memecoin as cfg
        cooldown = float(getattr(cfg, "MEMECOIN_GT_429_COOLDOWN_S", interval_s) or interval_s)
        if 0 < cooldown < float(interval_s):
            return cooldown
    return float(interval_s)


def _copy_event_from_json(event_json):
    from sniper.feeds.fomo_feed import CopyEvent
    import json as _json
    data = _json.loads(event_json)
    return CopyEvent(**{k: data.get(k) for k in CopyEvent.__dataclass_fields__})


def _retry_payload(ev):
    """The JSON-safe dict the retry queue stores for `ev`, or None when its lane is not retried.

    A feed CopyEvent is a dataclass. The on-chain lane's OnchainEv is a plain class: asdict()
    raised on it (2026-09-26), then ea94beab skipped it, so a transient miss on that lane was never
    re-quoted (review section 4). It now goes through an explicit field list. The slot lane's
    event stays unretried on purpose: its misses fall back to the on-chain lane, and a retry of
    its own could become a second owner of a signed send that may still land."""
    from dataclasses import asdict as _asdict, is_dataclass as _is_dataclass
    if _is_dataclass(ev):
        return _asdict(ev)
    if getattr(ev, "signal_source", None) == "onchain_watch":
        from sniper.services.memecoin_onchain_lane import retry_payload
        try:
            return retry_payload(ev)
        except Exception:
            logger.exception("retry enqueue: on-chain event not serialisable")
    return None


def _retry_event_from_json(event_json):
    """The event a retry row stores: an on-chain payload rebuilds its OnchainEv, anything else
    the CopyEvent it was. Raises on an unreadable row."""
    import json as _json
    from sniper.services.memecoin_onchain_lane import RETRY_EVENT_KIND, event_from_retry_payload
    data = _json.loads(event_json)
    if isinstance(data, dict) and data.get("event_kind") == RETRY_EVENT_KIND:
        return event_from_retry_payload(data)
    return _copy_event_from_json(event_json)


def _maybe_enqueue_retry(conn, ev, res, now_iso):
    """Queue a BUY for re-quote iff it missed transiently and the lane is enabled. Idempotent on
    the event id (a repeat miss just reschedules). Never raises."""
    if getattr(ev, "delivery_lane", None) == J7_SHADOW_LANE or _first_buy_window_applies(ev):
        return False
    if getattr(ev, "side", None) == "buy" and (res or {}).get("status") == "skipped_chase":
        # DIP RETRY (operator 2026-09-30): a named wallet's chase refusal waits for its price to
        # come back, watched off the loop. Out of scope it falls through to today's path.
        import json as _json
        from sniper.services import memecoin_dip_retry
        payload = _retry_payload(ev)
        if payload is not None and memecoin_dip_retry.maybe_enqueue(
                conn, ev, res, now_iso, event_json=_json.dumps(payload)):
            return True
    if getattr(ev, "side", None) != "buy" or not _is_transient_entry_miss(res):
        return False
    from sniper.settings import memecoin as cfg
    if float(getattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 0) or 0) <= 0:
        return False
    import json as _json
    payload = _retry_payload(ev)
    if payload is None:
        return False
    interval = float(getattr(cfg, "MEMECOIN_ENTRY_RETRY_INTERVAL_S", 8) or 8)
    # A PROVIDER we could not read, versus a pool that is not indexed yet. `quote_miss_code` is
    # already the ladder's word for it: `classify_quote_miss` emits `provider_<blind_reason>` only
    # when some rung reported blindness.
    network_id = getattr(ev, "network_id", "") or "solana"
    blind = (str(network_id).lower() not in {"solana", "sol"}
             and str((res or {}).get("quote_miss_code") or "").startswith("provider_"))
    return m.enqueue_entry_retry(
        conn, getattr(ev, "fomo_event_id", None), getattr(ev, "mint", ""),
        getattr(ev, "network_id", "") or "solana", _json.dumps(payload), now_iso,
        retry_after_s=_entry_retry_delay(
            res, interval, network_id=network_id,
            slot_lane_failure_reason=getattr(ev, "slot_lane_failure_reason", None)), blind=blind)


def _run_dip_hits(conn, hook, now_iso, *, host="main", network_id=None, hits_lock=None):
    """The dip retry's hits (sniper/services/memecoin_dip_retry.py), on this thread, through the
    same non-enqueuing attempt path and page suppression as a retry. Independent of the blind
    lane's window. Never raises.

    ONE PASS AT A TIME (2026-09-30). The main loop's period is 5 s, so a dip that lasts under a
    second was decided seconds late. The runner's dip hit lane (`host="dip_lane"`) hosts the hits:
    the watch wakes it the moment a row is due. The main loop runs them only while that lane has
    not passed for LANE_STALE_S, and no two passes ever overlap (HITS_LOCK), because two would end
    (and log) one row twice."""
    from sniper.services import memecoin_dip_retry
    if host == "main":
        if memecoin_dip_retry.lane_alive():
            return
        memecoin_dip_retry.page_stale_lane(conn)     # never raises; before the lock, which a hung
    elif host == "dip_lane":
        memecoin_dip_retry.lane_seen()
    lock = hits_lock if hits_lock is not None else memecoin_dip_retry.HITS_LOCK
    if not lock.acquire(blocking=False):
        return
    try:
        from sniper.settings import memecoin as cfg
        if (entry_opportunity.enabled()
                and (network_id is None or m.normalized_coin_network(network_id, None) in {"solana", "sol"})):
            for row in m.dip_retry_rows(conn):
                if m.normalized_coin_network(row.get("network_id"), row.get("mint")) not in {"solana", "sol"}:
                    continue
                try:
                    ev = _retry_event_from_json(row["event_json"])
                except Exception:
                    ev = None
                if ev is not None and not _first_buy_window_applies(ev, "solana"):
                    continue
                m.dequeue_entry_retry(conn, row["fomo_event_id"])
                m.record_dip_retry_hit(conn, fomo_event_id=row["fomo_event_id"],
                    source_user_id=getattr(ev, "source_user_id", None), mint=row["mint"],
                    hit_at=now_iso, watch_premium_pct=None, cap_pct=None,
                    status="policy_disabled", reason="source_buy_timer_retry_disabled",
                    position_id=None, network_id="solana")

        def attempt(c, ev):
            _entry_alert_suppress.on = True          # a re-attempt's REFUSAL never re-pages
            try:
                return getattr(hook, "attempt", hook)(c, ev)
            finally:
                _entry_alert_suppress.on = False

        def onchain_skip(c, ev, now):
            if getattr(ev, "signal_source", None) != "onchain_watch":
                return None
            from sniper.services.memecoin_onchain_lane import retry_skip_reason
            return retry_skip_reason(c, ev, now, max_age_s=float(
                getattr(cfg, "MEMECOIN_ONCHAIN_LANE_MAX_AGE_S", 45.0)))

        tally = memecoin_dip_retry.run_hits(conn, attempt, now_iso,
                                            event_from_json=_retry_event_from_json,
                                            onchain_skip_fn=onchain_skip, network_id=network_id,
                                            excluded_networks=getattr(hook, "excluded_retry_networks", ()))
        if tally.get("hits") or tally.get("dropped"):
            logger.warning("dip retry: %s", tally)
    except Exception:
        logger.exception("dip retry pass failed")
    finally:
        lock.release()


def run_entry_retry_lane(conn, hook, now_iso, *, enqueue_on_transient=None, network_id=None,
                         include_dips=True):
    """One pass over the fresh-pool re-quote queue. Re-runs `hook` on each due event; on a fill or
    any non-transient verdict the row is dequeued, on another transient miss it is rescheduled, and
    on window/attempt exhaustion the row is reaped (the hook is NOT run for a reaped row — its last
    real_decision already stands). Returns a small tally. Never raises."""
    from sniper.settings import memecoin as cfg
    if include_dips:
        _run_dip_hits(conn, hook, now_iso)
    window = float(getattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 0) or 0)
    if window <= 0:
        return {"retried": 0, "filled": 0, "gave_up": 0}
    interval = float(getattr(cfg, "MEMECOIN_ENTRY_RETRY_INTERVAL_S", 8) or 8)
    max_attempts = int(getattr(cfg, "MEMECOIN_ENTRY_RETRY_MAX_ATTEMPTS", 12) or 12)
    # A retry needs one full interval before the absolute window ends. Without this guard, a slow
    # hook starts just before expiry and can keep its reservation past the promised window.
    blind_window = float(getattr(cfg, "MEMECOIN_ENTRY_RETRY_BLIND_WINDOW_S", 0) or 0)
    reaped = m.reap_entry_retries(
        conn, now_iso, window_s=max(0.0, window - interval), max_attempts=max_attempts,
        blind_window_s=(None if blind_window <= 0 else max(0.0, blind_window - interval)),
        network_id=network_id)

    def _gave_up(row, seconds):
        from sniper.services.memecoin_telegram import notification_lifecycle_active
        if notification_lifecycle_active():
            _notify_entry_projection(origin_event_id=row.get("fomo_event_id"))
            return
        # ONE page when a fresh-pool buy never became routable in its window — the operator's only
        # signal for this coin, since the per-attempt refusals were suppressed above.
        mint16 = (row.get("mint") or "")[:16]
        _safe_alert(lambda _m=mint16: alerts.send_alert(
            "ENTRY-GAVE-UP",
            "%s… never landed in %ds — retry window exhausted" % (_m, int(seconds))))

    for row in reaped:
        _gave_up(row, window)
    gave_up = len(reaped)
    retried = filled = 0
    # Wait locally while another owner holds this coin; its release never resets our window.
    held = m.pending_open_coin_ids(conn)
    if network_id is not None:
        held = {coin for coin in held if coin[0] == str(network_id)}
    for row in m.due_entry_retries(conn, now_iso, window_s=window, max_attempts=max_attempts,
                                  network_id=network_id, excluded_coins=held):
        fid = row["fomo_event_id"]
        try:
            ev = _retry_event_from_json(row["event_json"])
        except Exception:
            logger.exception("retry lane: unreadable event %s — dropping", fid)
            m.dequeue_entry_retry(conn, fid)
            continue
        if _first_buy_window_applies(ev, row.get("network_id")):
            m.dequeue_entry_retry(conn, fid)
            gave_up += 1
            continue
        if getattr(ev, "signal_source", None) == "onchain_watch":
            # An on-chain retry keeps the lane's own freshness (MEMECOIN_ONCHAIN_LANE_MAX_AGE_S
            # from detection, Alpha 2026-09-27) and yields to a feed attempt on the same tx.
            from sniper.services.memecoin_onchain_lane import retry_skip_reason
            lane_age = float(getattr(cfg, "MEMECOIN_ONCHAIN_LANE_MAX_AGE_S", 45.0))
            skip = retry_skip_reason(conn, ev, now_iso, max_age_s=lane_age)
            if skip is not None:
                m.dequeue_entry_retry(conn, fid)
                logger.warning("retry lane: on-chain %s dropped (%s)", fid, skip)
                if skip == "stale_for_lane":
                    gave_up += 1
                    _gave_up(dict(row), lane_age)
                continue
        attempt = getattr(hook, "attempt", hook)   # non-enqueuing path; falls back for test stubs
        _entry_alert_suppress.on = True             # a re-attempt's REFUSAL never re-pages
        try:
            res = attempt(conn, ev)
        except Exception:
            logger.exception("retry lane: hook raised for %s", fid)
            m.bump_entry_retry(conn, fid, now_iso, retry_after_s=interval)
            continue
        finally:
            _entry_alert_suppress.on = False
        if res and res.get("status") == "duplicate_delivery":
            # Another owner is still deciding this source. This retry did no provider work;
            # leave its durable row due rather than treating the collision as a terminal gate.
            continue
        retried += 1
        if res and res.get("status") == "filled":
            filled += 1
            m.dequeue_entry_retry(conn, fid)
        elif _is_transient_entry_miss(res):
            m.bump_entry_retry(
                conn, fid, now_iso,
                retry_after_s=_entry_retry_delay(res, interval, attempts=row.get("attempts", 0),
                    network_id=row.get("network_id") or "solana",
                    slot_lane_failure_reason=getattr(ev, "slot_lane_failure_reason", None)),
            )
        else:
            m.dequeue_entry_retry(conn, fid)   # a real gate decided — stop retrying
        _notify_entry_projection(origin_event_id=fid)
    return {"retried": retried, "filled": filled, "gave_up": gave_up}


def make_real_hook(conn_unused, contexts, *, now_fn, trade_size, max_age_s, resolve_fn=None,
                   stats_fn=None, trace_clock=None, blind_mcap_fn=None):
    """Fold entry (buys) + mirror-on-sell (sells) into ONE callable for run_once's fresh-event loop,
    routing each event to its venue via `contexts`. BUYS are additionally gated on the per-wallet
    real allowlist (memecoin_wallets.real_approved) — the operator trades a hand-picked subset while
    the paper copy + Telegram watch cover the whole curated roster. SELLS are never gated: any real
    position (including a manual entry on a watch-only wallet) must mirror-exit when its trader sells."""
    if blind_mcap_fn is None:
        blind_mcap_fn = _default_blind_mcap_fn()
    cluster_client = (
        ClusterGateClient(cfg.MEMECOIN_CLUSTER_GATE_SOCKET)
        if cfg.MEMECOIN_REAL_CLUSTER_GATE_ENABLED else None
    )

    sole_yellowstone = str(cfg.MEMECOIN_SOLANA_INFRA_PROVIDER).lower() == "shyft"
    def _hook_raw(conn, ev, *, decision_at=None):
        from sniper.services.memecoin_j7_backup import is_j7_buy, prepare, native_refusal
        if (sole_yellowstone and getattr(ev, "side", None) == "buy"
                and getattr(ev, "network_id", None) == "solana"
                and getattr(ev, "signal_source", None) in {"follow_activity", "feed", "global_feed"}
                and not (cfg.MEMECOIN_J7_SOLANA_VERIFIED_BACKUP and is_j7_buy(ev))):
            return {"status": "hint_only", "chain": "solana", "reason": "yellowstone_source_required"}
        if getattr(ev, "side", None) == "buy":
            if cfg.MEMECOIN_J7_SOLANA_VERIFIED_BACKUP and is_j7_buy(ev):
                proven, why = prepare(conn, ev)
                if proven is None:
                    if why == 'j7_opaque_hint':
                        return {'status': 'hint_only', 'chain': 'solana', 'reason': why}
                    result = {'status': 'source_initiation_unknown', 'chain': 'solana', 'reason': why}
                    trace = _start_trace_best_effort(conn, ev, decision_at or now_fn(), trace_clock)
                    _finish_trace_for_result(trace, result)
                    return result
                ev = proven
                resume = getattr(hook, 'receipt_resume', None)
                if callable(resume) and resume(ev):
                    result = {'status': 'deferred', 'chain': 'solana', 'reason': 'slot_lane_receipt_resume'}
                    trace = _start_trace_best_effort(conn, ev, decision_at or now_fn(), trace_clock)
                    _finish_trace_for_result(trace, result)
                    return result
            if (getattr(ev, 'j7_verified_backup', False)
                    or getattr(ev, 'j7_native_failure_backup', False)):
                if is_stale_entry(ev.created_at, decision_at or now_fn(), cfg.MEMECOIN_ONCHAIN_LANE_MAX_AGE_S):
                    result = {'status': 'stale', 'chain': 'solana', 'reason': 'j7_confirmed_receipt_stale'}
                    trace = _start_trace_best_effort(conn, ev, decision_at or now_fn(), trace_clock)
                    _finish_trace_for_result(trace, result)
                    return result
                # Recheck native ownership on retries; the shared reservation CAS arbitrates races.
                why = native_refusal(conn, ev)
                if not cfg.MEMECOIN_J7_SOLANA_VERIFIED_BACKUP or why:
                    return {'status': 'rejected', 'chain': 'solana', 'reason': why or 'j7_backup_disabled'}
            # No refusal before the mcap stage may classify with an earlier pass's priced shadow
            # (Gamma LOW on c209efe12): cleared here, set again only by _priced_history_mcap.
            _set_priced_shadow(ev, None)
            decision_at = decision_at or now_fn()
            trace = _start_trace_best_effort(conn, ev, decision_at, trace_clock)
            if trace is not None and getattr(ev, "delivery_lane", None) == J7_SHADOW_LANE:
                trace.j7_shadow = True
            _set_entry_shadow_context(trace, ev, trade_size)
            identity = None
            handed_off = False
            try:
                identity = resolve_entry_identity(conn, ev, resolve_fn=resolve_fn)
                _mark_trace_best_effort(trace, "chain_resolution")
                if identity.status != "resolved":
                    res = _identity_failure_result(identity)
                    _persist_terminal_classification_best_effort(
                        conn, ev, identity, decision_at, trace)
                    _finish_trace_for_result(trace, res, identity=identity)
                    return res
                chain = identity.network_id
                _window_active = _first_buy_window_applies(ev, chain)
                startup_refusal = None if _window_active else _entry_startup_refusal(contexts, chain)
                if startup_refusal is not None:
                    _finish_trace_for_result(trace, startup_refusal, identity=identity)
                    return startup_refusal
                initiation_refusal = source_initiation_refusal(ev, chain)
                if initiation_refusal is not None:
                    res = {"status": "source_initiation_unknown", "chain": chain,
                           "reason": initiation_refusal}
                    if chain == "solana":
                        res = _with_slot_lane_decision(res, ev, trace)
                    _persist_terminal_classification_best_effort(
                        conn, ev, identity, decision_at, trace,
                        effective_mcap=getattr(ev, "their_market_cap", None))
                    _finish_trace_for_result(trace, res, identity=identity)
                    return res
                if not _window_active and entry_route_closed(contexts, chain):
                    res = {"status": "unroutable_chain", "chain": chain}
                    _persist_terminal_classification_best_effort(
                        conn, ev, identity, decision_at, trace,
                        effective_mcap=getattr(ev, "their_market_cap", None))
                    _finish_trace_for_result(trace, res, identity=identity)
                    return res
                if (not is_manual_entry(ev)
                        and not m.funded_source_is_identified(conn, ev.source_user_id)):
                    res = {"status": "unknown_source", "chain": chain}
                    _persist_terminal_classification_best_effort(
                        conn, ev, identity, decision_at, trace,
                        effective_mcap=getattr(ev, "their_market_cap", None))
                    _finish_trace_for_result(trace, res, identity=identity)
                    return res
                # Authority is PER CHAIN (2026-07-30): being armed on solana does not authorise a
                # 4663 entry. `chain` is already resolved and venue-checked immediately above —
                # that guard answers capability, this one answers authority.
                #
                # ROSTER GATE, operator-switchable 2026-08-13, default ON. Off means ANY wallet the
                # feed reports may open a funded position, not just the armed roster. The money is
                # still bounded by MEMECOIN_MAX_CONCURRENT, MEMECOIN_MAX_TOTAL_EXPOSURE_USD and the
                # daily loss limit, so this widens WHICH signals fill the book rather than how much
                # is at risk per se.
                #
                # It does change the RAILS, which is the non-obvious part. follow-30-50-v1 is keyed
                # by handle in MEMECOIN_FOLLOW3050_WALLETS, and a wallet that is not on that list
                # freezes the BAND DEFAULT contract instead — funded-nano-v2 and friends, which run
                # mirror_on_sell=True with a trail and a de-risk. So with the roster gate off, most
                # new positions do NOT get the 30/70 no-mirror policy the armed six get.
                armed_on_chain = ev.source_user_id in m.real_approved_user_ids(
                    conn, network_id=chain,
                )
                if cfg.MEMECOIN_REAL_ROSTER_GATE_ENABLED and not armed_on_chain:
                    res = {"status": "not_approved", "chain": chain}
                    _persist_terminal_classification_best_effort(
                        conn, ev, identity, decision_at, trace,
                        effective_mcap=getattr(ev, "their_market_cap", None))
                    _finish_trace_for_result(trace, res, identity=identity)
                    return res
                if _window_active:
                    if getattr(ev, "revival_reask", False):
                        _window_reason = "source_buy_timer_retry_disabled"
                    else:
                        _, _window_reason = entry_opportunity.event_window(
                            conn, ev, observed_at=decision_at)
                    if _window_reason:
                        res = {"status": "rejected", "chain": chain, "reason": _window_reason}
                        _finish_trace_for_result(trace, res, identity=identity)
                        return res
                    startup_refusal = _entry_startup_refusal(contexts, chain)
                    if startup_refusal is not None:
                        _finish_trace_for_result(trace, startup_refusal, identity=identity)
                        return startup_refusal
                    if entry_route_closed(contexts, chain):
                        res = {"status": "unroutable_chain", "chain": chain}
                        _finish_trace_for_result(trace, res, identity=identity)
                        return res
                fast_lane_refuses = (chain == 'solana' and cfg.MEMECOIN_SOLANA_FAST_LANE_ONLY
                                     and not is_manual_entry(ev)
                                     and not getattr(ev, 'native_failure_fallback', False)
                                     and not (cfg.MEMECOIN_J7_SOLANA_VERIFIED_BACKUP
                                              and getattr(ev, 'j7_verified_backup', False)))
                # An ON-CHAIN row refuses LATER, after the roster and mcap/floor gates and before
                # the first reservation or quote, so its refusal names the real reason first (RARI:
                # an mcap-ceiling refusal read native_fast_lane_unavailable). J7 and every other
                # source keep this early refusal: no market read for a buy that cannot be funded.
                # It follows the roster gate (Alpha GO 2026-10-07): a J7 Solana buy from a wallet not
                # armed there read native_fast_lane_unavailable (387 rows in 16 h) and inflated
                # fast-lane-miss counts. Every gate above it is config or a local read; only labels move.
                if fast_lane_refuses and getattr(ev, 'signal_source', None) != 'onchain_watch':
                    res = _fast_lane_refusal(ev, chain, trace)
                    _persist_terminal_classification_best_effort(
                        conn, ev, identity, decision_at, trace,
                        effective_mcap=getattr(ev, 'their_market_cap', None))
                    _finish_trace_for_result(trace, res, identity=identity)
                    return res
                # THE BSC LANE'S POLICY REFUSAL IS FINAL FOR ITS J7 COPY (73798, 2026-10-12): the lane
                # refused the source buy on its receipt mcap; J7 re-decided it on its own event mcap and
                # filled at -33.7%. One dict read in this process, the lane's own memory.
                if (chain == '56' and getattr(ev, 'signal_source', None) == 'follow_activity'
                        and not is_manual_entry(ev)):
                    from sniper.services.memecoin_bsc_fast_lane import policy_decision_for
                    from sniper.services.memecoin_evm_post_trade import raybot_origin
                    origin = raybot_origin(str(getattr(ev, 'fomo_event_id', None) or '').lower())
                    decided = policy_decision_for(tx_hash=origin[0] if origin else None, mint=ev.mint,
                                                  source_user_id=ev.source_user_id)
                    if decided is not None:
                        res = {'status': 'rejected', 'chain': chain,
                               'reason': 'bsc_fast_lane_decided: ' + decided}
                        _persist_terminal_classification_best_effort(
                            conn, ev, identity, decision_at, trace,
                            effective_mcap=getattr(ev, 'their_market_cap', None))
                        _finish_trace_for_result(trace, res, identity=identity)
                        return res
                # Below the fast-lane refusal (Gamma note on 503f37e32): an armed J7 Solana buy with no
                # source price must keep the fast-lane label, the only one the J7 card silencers match.
                if (cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT > 0
                        and not cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY and not is_manual_entry(ev)
                        and positive_price(getattr(ev, 'their_price', None)) is None):
                    res = {'status': 'rejected', 'chain': chain, 'reason': 'copy_source_price_unavailable'}
                    _persist_terminal_classification_best_effort(conn, ev, identity, decision_at, trace)
                    _finish_trace_for_result(trace, res, identity=identity)
                    return res
                if (armed_on_chain
                        and (getattr(ev, "signal_source", None) == "follow_activity"
                             or getattr(ev, "j7_native_failure_backup", False))):
                    arm_started_at = m.wallet_real_cohort_start(
                        conn, ev.source_user_id, network_id=chain, include_initial=True,
                    )
                    source_at = _parse_ts(getattr(ev, "created_at", None))
                    arm_at = _parse_ts(arm_started_at)
                    try:
                        predates_arm = (
                            source_at is not None and arm_at is not None and source_at < arm_at
                        )
                    except (TypeError, ValueError):
                        predates_arm = False
                    if predates_arm:
                        res = {
                            "status": "pre_arm_backlog",
                            "reason": (
                                f"follow source event {ev.created_at} predates the {chain} arm "
                                f"at {arm_started_at}"
                            ),
                        }
                        _persist_terminal_classification_best_effort(
                            conn, ev, identity, decision_at, trace,
                            effective_mcap=getattr(ev, "their_market_cap", None),
                        )
                        _alert_entry_result(
                            ev.mint, trade_size, res,
                            handle=getattr(ev, "source_handle", None),
                            ticker=getattr(ev, "ticker", None),
                            armed=True,
                        )
                        _finish_trace_for_result(trace, res, identity=identity)
                        return res
                # Resolve missing market cap from exact-chain history, then apply the same ceiling.
                mc = getattr(ev, "their_market_cap", None)
                # PRICE THE RESOLVED MCAP AT THIS ENTRY'S PRICE (2026-09-18). Without it the
                # resolver returns the median of the window's readings, which on a coin that just
                # moved describes an earlier price: COINU DDndHihss… resolved to $126,036 and
                # passed the $250k ceiling while its own chart said $266,708 and price x supply
                # said $313,818. The ceiling then measured a coin we were not buying.
                own_evm = chain != "solana" and chain in _evm_chain_knob(
                    getattr(cfg, "MEMECOIN_EVM_OWN_MCAP_CHAINS", ""), "MEMECOIN_EVM_OWN_MCAP_CHAINS")
                resolved = None if mc else m.resolve_mint_mcap(
                    conn, getattr(ev, "mint", None), network_id=chain,
                    since_iso=cfg.real_history_since_iso(decision_at),
                    price=getattr(ev, "their_price", None), priced=chain != "solana",
                    supply_band=own_evm)
                # CZ (56, 2026-10-03): two week-old readings made the ordinary resolver ignore
                # today's source price and refuse $348 against a stale $393 floor. EVERY EVM CHAIN
                # (d31fed65, 4663, 2026-10-06): a $242,892 median of two readings 4.8 h apart
                # passed the $250k ceiling while this buy's price put the coin at ~$410k. Sparse
                # history is priced at this buy from the same rows; no price is unknown, never the
                # median. Own-mcap chains keep the supply band: an exact supply read follows there.
                # THE PRICED SHADOW (523c9968, Alpha 2026-10-01). Where Solana history resolved, the
                # mcap at THIS price rides on the event: a proven pump mint at source price x 1e9,
                # else the readings' implied supply at the source price. It is recorded beside
                # today's value in the classification and, with MEMECOIN_MCAP_HISTORY_PRICED on,
                # tested by the ceiling ALONE (below). Band, floor and everything downstream keep
                # today's `resolved`.
                priced_mcap = _priced_history_mcap(conn, ev, chain, decision_at, resolved)
                ceiling = _wallet_mcap_ceiling(
                    cfg.MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET, chain,
                    getattr(ev, "source_user_id", None), cfg.MEMECOIN_REAL_MAX_MCAP_USD)
                mcap_floor = _wallet_mcap_floor(
                    cfg.MEMECOIN_REAL_MIN_MCAP_USD_BY_WALLET, chain,
                    getattr(ev, "source_user_id", None), cfg.MEMECOIN_REAL_MIN_MCAP_USD)
                allow, eff_mc, reason = mcap_entry_decision(
                    mc, resolved, ceiling, mcap_floor)
                mcap_basis = (("event" if mc else "history" if resolved else None)
                              if eff_mc is not None else None)
                market_stats = None
                if (not allow and cfg.MEMECOIN_AUTO_TEMPLATES == "on"
                        and eff_mc is not None and eff_mc >= cfg.MEMECOIN_SCALP_MCAP_USD):
                    allow = True
                refusal_mcap = None
                if allow and _priced_ceiling_refuses(priced_mcap, ceiling):
                    allow, reason, refusal_mcap = False, "over_ceiling", priced_mcap
                # A feed cap must not bypass the supply ceiling the on-chain lane enforced.
                if allow and eff_mc is not None and chain == "4663" and ceiling:
                    cached_mc = _cached_evm_source_mcap(
                        chain, ev.mint, getattr(ev, "their_price", None), trace,
                        live_max_age_s=LIVE_SUPPLY_GUARD_MAX_AGE_S)
                    if cached_mc is not None:
                        cached_allow, _, cached_reason = mcap_entry_decision(cached_mc, None, ceiling)
                        if (not cached_allow and cached_reason == "over_ceiling"
                                and not (cfg.MEMECOIN_AUTO_TEMPLATES == "on"
                                         and cached_mc >= cfg.MEMECOIN_SCALP_MCAP_USD)):
                            allow, reason, refusal_mcap = False, cached_reason, cached_mc
                # E1 ON EVM (2026-09-29): on a listed chain our own mcap replaces DexScreener's.
                # The supply read starts HERE, beside the GET below, never after it.
                supply_future = (_start_evm_supply_read(chain, ev.mint)
                                 if own_evm and eff_mc is None else None)
                # E14 SHADOW: our own momentum read starts here too; the classifier only peeks it.
                momentum_own = _start_momentum_read(chain, ev.mint)
                if momentum_own is not None and trace is not None:
                    trace.momentum_own = momentum_own
                if eff_mc is None and fast_lane_refuses:
                    # READ-FREE (Gamma MED-1, 2026-10-06): the mcap now needs a read, and that
                    # read starts the funded early quote and prereads. A row fast-lane-only will
                    # refuse anyway spends none of it: refuse here with the cheap stages' verdict.
                    res = {**_fast_lane_refusal(ev, chain, trace),
                           'slot_fallback_class': getattr(ev, 'slot_fallback_class', None)}
                    _persist_terminal_classification_best_effort(
                        conn, ev, identity, decision_at, trace,
                        effective_mcap=getattr(ev, 'their_market_cap', None))
                    _finish_trace_for_result(trace, res, identity=identity)
                    return res
                if eff_mc is None and stats_fn is not None:
                    # LIVE-MCAP FALLBACK (latency-cliff work, 2026-07-15): follow_activity
                    # events carry their_market_cap=0.0 and a FRESH catch on a new coin has no
                    # same-mint feed history yet -> 'unresolved' would fail-closed-skip exactly
                    # the <60s catches the fast lane exists for. One live lookup (~300ms; this
                    # path is seconds-late by construction) resolves it; still fail-closed when
                    # the lookup has no mcap. eff_mc then also feeds the tiered impact cap.
                    # B: the gate reads ride this GET on EVERY chain (2026-09-28). The hook then gets
                    # these stats cached and starts no reads of its own, so an EVM event without a
                    # source mcap (27 of 51 4663 fills since 09-21) read its balances in line after
                    # the reservation gates: 270-300 ms blocking where the early read costs ~40.
                    _pre = _start_entry_prereads(ev, contexts, trace, chain=chain)
                    if chain == "solana":
                        _start_early_entry_quote(conn, ev, contexts, trace, _pre)
                    else:
                        _start_early_evm_quote(conn, ev, contexts, trace, _pre, chain)
                    _start_evm_post_trade_read(ev, chain, trace=trace)
                    try:
                        st = stats_fn(ev.mint, network_id=chain) or {}
                    except TypeError:
                        st = stats_fn(ev.mint) or {}
                    except Exception:
                        st = {}
                    market_stats = dict(st or {})
                    _set_event_market_ticker(ev, market_stats)
                    # BAND SOURCE PIN (2026-08-30). On a DEX-blind chain the BLIND source owns
                    # the market cap; DexScreener populates everything else in `st`.
                    #
                    # This inverts nothing that was ever exercised. While 4663 had no
                    # FOMO_TO_DEX entry `st` was always empty there, so the blind path below was
                    # already the only band source. Adding the mapping would have made
                    # DexScreener the band source silently -- and the band drives EXIT POLICY,
                    # not just admission. Measured across the nano/micro/small boundary regions
                    # we actually trade, 18 of 39 tokens (46%) change band between the two
                    # sources, and 9 of 11 tokens where GeckoTerminal has no cap (82%) move off
                    # out_of_scope into a real band. The disagreement is NOT FDV-vs-circulating
                    # (DexScreener marketCap == fdv on 28 of 34 tokens, median GT/DS 0.9925) and
                    # is not price (GT agrees with our own route quotes at 0.967 median over
                    # ~4,000 paired marks). It is the supply basis, and it is unresolved. An
                    # entry-gate repair must not decide it. There is deliberately NO fallback to
                    # `st` when the blind source is empty: that cohort is the 82% one, so a
                    # fallback would move exactly the bands this pin exists to hold still.
                    # E1 (2026-09-28): with MEMECOIN_SOLANA_DEX_MCAP_FALLBACK=0 DexScreener never sets
                    # a Solana band; a coin the on-chain basis cannot price goes to the route stage.
                    live_mc = (st.get("market_cap") if dex_mcap_allowed(chain) and not own_evm and (
                        chain != "solana" or cfg.MEMECOIN_SOLANA_DEX_MCAP_FALLBACK) else None)
                    stage3_basis = "dexscreener"
                    # STAGE 3 ON-CHAIN (2026-09-27, operator via Alpha, option B). Behind the flag
                    # only; the default reads nothing more and bands exactly as before.
                    if (chain == "solana"
                            and cfg.MEMECOIN_SOLANA_BAND_MCAP_BASIS == "onchain"):
                        onchain_mc = _onchain_band_mcap(ev, contexts, pre=_pre)
                        if onchain_mc:
                            live_mc, stage3_basis = onchain_mc, "onchain_source_fill"
                        else:
                            stage3_basis = "dexscreener_fallback"
                    if live_mc:
                        allow, eff_mc, reason = mcap_entry_decision(
                            live_mc, None, ceiling, mcap_floor)
                        mcap_basis = stage3_basis
                        if (not allow and cfg.MEMECOIN_AUTO_TEMPLATES == "on"
                                and eff_mc is not None
                                and eff_mc >= cfg.MEMECOIN_SCALP_MCAP_USD):
                            allow = True
                if own_evm and eff_mc is None:
                    live_mc = _evm_own_band_mcap(supply_future, getattr(ev, "their_price", None),
                                                 trace)
                    if live_mc:
                        allow, eff_mc, reason = mcap_entry_decision(
                            live_mc, None, ceiling, mcap_floor)
                        mcap_basis = "evm_source"
                        if (not allow and cfg.MEMECOIN_AUTO_TEMPLATES == "on"
                                and eff_mc is not None
                                and eff_mc >= cfg.MEMECOIN_SCALP_MCAP_USD):
                            allow = True
                # SOLANA LAST RESORT (2026-08-24): every other mcap source came back empty, which
                # during the DexScreener outage meant ASSUME_MICRO promoted the coin to
                # assumed_nano and the $250k ceiling never applied. Price it off our own route
                # instead. Same fail-open contract as the blind-chain branch below: None leaves
                # the old behaviour untouched, so this can only ADD a refusal the ceiling should
                # already have made, never admit something it would have blocked.
                if not allow and eff_mc is None and chain == "solana":
                    live_mc, route_basis = None, "route"
                    if not cfg.MEMECOIN_SOLANA_DEX_MCAP_FALLBACK:
                        live_mc = _route_band_mcap(conn, ev, contexts, trace,
                                                   _entry_prereads_for(ev, trace))
                        route_basis = "route_clip" if live_mc else "route"   # Gamma L2: split them
                    # A J7 SHADOW DECISION QUOTES NOTHING (Alpha on Gamma M1, 2026-09-28): the route
                    # price is a funded-seam Jupiter quote, and a 429 there cools the funded hosts.
                    # Shadow then records mcap_unresolved / assumed on the ~0.5% no other stage prices.
                    if not live_mc and getattr(ev, "delivery_lane", None) != J7_SHADOW_LANE:
                        live_mc = _default_solana_mcap_fn()(ev.mint)
                    if live_mc:
                        allow, eff_mc, reason = mcap_entry_decision(
                            live_mc, None, ceiling, mcap_floor)
                        mcap_basis = route_basis
                        if (not allow and cfg.MEMECOIN_AUTO_TEMPLATES == "on"
                                and eff_mc is not None
                                and eff_mc >= cfg.MEMECOIN_SCALP_MCAP_USD):
                            allow = True
                if (not allow and eff_mc is None
                        and chain in cfg.MEMECOIN_DEX_BLIND_CHAINS):
                    # Prefer verified supply already collected by the volume observer. Source
                    # USD fill price avoids a price-provider round trip; a missing/stale cache
                    # preserves the established GT fallback. The existing cap/floor still decides.
                    live_mc = _cached_evm_source_mcap(chain, ev.mint,
                                                     getattr(ev, "their_price", None), trace)
                    blind_basis = "evm_source_cached"
                    if live_mc is None:
                        live_mc = blind_mcap_fn(chain, ev.mint)
                        blind_basis = "blind"
                    if live_mc:
                        allow, eff_mc, reason = mcap_entry_decision(
                            live_mc, None, ceiling, mcap_floor)
                        mcap_basis = blind_basis
                        if (not allow and cfg.MEMECOIN_AUTO_TEMPLATES == "on"
                                and eff_mc is not None
                                and eff_mc >= cfg.MEMECOIN_SCALP_MCAP_USD):
                            allow = True
                source_price = getattr(ev, "their_price", None)
                if (not allow and eff_mc is None and reason == "mcap_unresolved"
                        and chain != "solana" and chain not in cfg.MEMECOIN_DEX_BLIND_CHAINS
                        and source_price and not own_evm):   # the own stage already tried, bounded
                    live_mc = _default_evm_source_mcap_fn()(chain, ev.mint, source_price)
                    if live_mc:
                        allow, eff_mc, reason = mcap_entry_decision(
                            live_mc, None, ceiling, mcap_floor)
                        mcap_basis = "evm_source"
                        if (not allow and cfg.MEMECOIN_AUTO_TEMPLATES == "on"
                                and eff_mc is not None
                                and eff_mc >= cfg.MEMECOIN_SCALP_MCAP_USD):
                            allow = True
                assumed_micro = False
                # `reason in (...)`: with the ceiling OFF the gate answers "no_ceiling" with
                # allow=True, so the old `not allow` guard skipped this promotion and an
                # unresolvable fresh coin banded out_of_scope instead of assumed_nano
                # (BRAINROT 2026-08-19). Unknown-mcap promotion is band policy, not ceiling
                # policy — it applies in both gate outcomes.
                if (eff_mc is None and reason in ("mcap_unresolved", "no_ceiling")
                        and (chain == "solana" or chain in cfg.MEMECOIN_DEX_BLIND_CHAINS)
                        and cfg.MEMECOIN_REAL_MCAP_ASSUME_MICRO):
                    # ASSUME-MICRO (2026-07-16): all three resolvers empty on Solana = a
                    # minutes-old micro by construction (big caps always have a DS pair or
                    # feed history). Fail-closed here refused ~16 armed buys/day, all in the
                    # sub-$1M pocket — the exact 07-11 bug class one resolver layer deeper.
                    # Bounded by token safety, the micro-tier impact cap, clip size, stop/mirror.
                    # NAME IS STALE (audit 2026-08-05): this lifts the MCAP gate only, and bands
                    # the coin "assumed_nano", not "micro". With RESERVABLE_BANDS = small,mid the
                    # entry is still refused at the band floor downstream — a later, deliberate
                    # funded decision that supersedes this block's 07-16 rationale. See
                    # settings/memecoin.py MEMECOIN_REAL_MCAP_ASSUME_MICRO before "fixing" either.
                    if chain == "solana" and cfg.MEMECOIN_SOLANA_MCAP_UNRESOLVED == "refuse":
                        # E1 (2026-09-28): no own stage priced this coin. With the DexScreener
                        # fallback off, banding it assumed_nano would let a coin DexScreener used to
                        # price skip the ceiling, so it is refused instead. 0 of 1,340 Solana
                        # classifications reached this branch since 2026-09-27 09:00Z.
                        allow, reason = False, "mcap_unresolved"
                    else:
                        allow, eff_mc, reason, assumed_micro = True, None, "assumed_micro", True
                        mcap_basis = "assumed"
                if not allow and reason == "over_ceiling":
                    # BIG-CAP CEILING LIFT (2026-10-06): our own 5-minute volume can lift the
                    # global ceiling on a listed chain. It reads only local stores, and only here,
                    # on a refusal: an entry admitted today pays nothing for it.
                    lifted = _bigcap_lift(ev, chain, refusal_mcap or eff_mc, ceiling, trace=trace)
                    if lifted:
                        ceiling = lifted
                        allow, eff_mc, reason = mcap_entry_decision(eff_mc, None, ceiling, mcap_floor)
                        refusal_mcap = None
                        if allow and _priced_ceiling_refuses(priced_mcap, ceiling):
                            allow, reason, refusal_mcap = False, "over_ceiling", priced_mcap
                if not allow:
                    _mark_trace_best_effort(trace, "market_data")
                    # SAY THE NUMBERS (2026-09-09). `reason` is a stable IDENTIFIER — tests pin the
                    # tuple and the refusal families key on it — so the values are added here, at
                    # the render, not inside the gate. `over_ceiling` was the single largest
                    # refusal for armed wallets (95 attempts, 30 coins in one hour) and it carried
                    # no mcap and no ceiling, so reading it required opening the source to learn it
                    # meant mcap at all. Every other gate already prints its bar.
                    _detail = reason
                    _shown = refusal_mcap or eff_mc      # the priced value when it refused
                    if reason in ("over_ceiling", "under_floor") and _shown:
                        _bar = ceiling if reason == "over_ceiling" else mcap_floor
                        _detail = "mcap $%s %s $%s (%s)" % (
                            format(int(_shown), ","),
                            "over ceiling" if reason == "over_ceiling" else "under floor",
                            format(int(_bar or 0), ","), reason)
                    res = {"status": "skipped_mcap", "mcap": _shown, "reason": _detail}
                    _persist_terminal_classification_best_effort(
                        conn, ev, identity, decision_at, trace,
                        effective_mcap=eff_mc, assumed_micro=assumed_micro,
                        market_stats=market_stats, mcap_basis=mcap_basis)
                    _alert_entry_result(
                        ev.mint, trade_size, res,
                        handle=getattr(ev, "source_handle", None),
                        ticker=getattr(ev, "ticker", None),
                        armed=_wallet_is_armed(conn, ev, identity),
                    )
                    _finish_trace_for_result(trace, res, identity=identity)
                    return res
                age_cap = (cfg.MEMECOIN_REAL_FOLLOW_MAX_AGE_S
                           if getattr(ev, "signal_source", "global_feed") == "follow_activity"
                           else max_age_s)
                # A REVIVAL RE-ASK IS THE ORIGINAL FRESH SIGNAL, ASKED ONCE MORE (2026-09-12). The
                # watch waits for a lagging provider to show the volume; with the 60 s follow cap a
                # re-ask after that wait was refused as stale, so a longer window did nothing. The
                # budget grows by exactly the watch window, and every other gate runs again.
                if getattr(ev, "revival_reask", False) and age_cap:
                    age_cap = float(age_cap) + cfg.revival_watch_window_s(chain)
                if getattr(ev, 'j7_verified_backup', False):
                    age_cap = min(age_cap, cfg.MEMECOIN_ONCHAIN_LANE_MAX_AGE_S)
                # assumed-micro positions tier as micro (that IS the assumption) — the flat
                # unknown-mcap base (5%) would refuse the thin-pool entries this path exists for.
                # CONSENSUS gate above $1M (2026-07-17): the 1-2M band is only alive WITH company
                # (solo 20% hit20 vs 49% with >=1 prior buyer/60m, n=630 matured signals). This is
                # conditioning at the armed trigger, NOT waiting for consensus (the wait-cost trap):
                # the armed buy still fires the entry; we just require the coin to already have a
                # second buyer. Fail-OPEN on an unreadable count (data absence never blocks).
                prior = None
                if (allow and eff_mc and cfg.MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP > 0
                        and eff_mc >= cfg.MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP):
                    prior = m.prior_buyers_60m(
                        conn, mint=ev.mint, network_id=chain,
                        source_user_id=ev.source_user_id,
                        before_iso=getattr(ev, "created_at", None))
                    if (prior is not None
                            and prior < cfg.MEMECOIN_REAL_CONSENSUS_MIN_BUYERS):
                        res = {"status": "skipped_mcap", "mcap": eff_mc,
                               "reason": "no consensus above $%.0fK mcap (%d prior buyers/60m, "
                                         "need >=%d)" % (cfg.MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP / 1e3,
                                                         prior, cfg.MEMECOIN_REAL_CONSENSUS_MIN_BUYERS)}
                        _persist_terminal_classification_best_effort(
                            conn, ev, identity, decision_at, trace,
                            effective_mcap=eff_mc, assumed_micro=assumed_micro,
                            consensus_n_60m=prior, market_stats=market_stats)
                        _alert_entry_result(ev.mint, trade_size, res,
                                            handle=getattr(ev, "source_handle", None),
                                            ticker=getattr(ev, "ticker", None), armed=_wallet_is_armed(conn, ev, identity))
                        _finish_trace_for_result(trace, res, identity=identity)
                        return res
                # MODELLED IMPACT, EVM ONLY (2026-09-23, operator: "build the impact gate").
                # The existing cap below is enforced from the VENUE'S QUOTED impact inside the
                # Solana executor; `execute_entry_evm` accepts no such argument, so an EVM entry has
                # never had one. The five quiet-hours refusals this was built from were all EVM, and
                # they split on depth: a $37.50 clip moved ARENA 0.32% and CSTR 13.32%, both
                # reproduced exactly by `clip_impact_pct` from the same pool state.
                # DEFAULT OFF (`MEMECOIN_ENTRY_MAX_IMPACT_PCT` is 0), and an impact that cannot be
                # computed NEVER blocks: an unreadable pool must not become a refusal.
                if str(chain) != "solana" and cfg.MEMECOIN_ENTRY_MAX_IMPACT_PCT > 0:
                    from sniper.services import memecoin_pool_depth as _pool_depth
                    _impact_inputs = None
                    try:
                        _impact_inputs = m.pool_impact_inputs(
                            conn, str(chain), str(ev.mint), now_iso=decision_at,
                            max_age_s=cfg.MEMECOIN_POOL_DEPTH_MAX_AGE_S)
                    except Exception:
                        logger.exception("impact gate: cached pool state read failed for %s",
                                         ev.mint)
                    _impact_ok, _impact_why, _impact_pct = _pool_depth.impact_verdict(
                        trade_size, _impact_inputs, cfg.MEMECOIN_ENTRY_MAX_IMPACT_PCT)
                    if not _impact_ok:
                        res = {"status": "rejected", "reason": _impact_why,
                               "impact_pct": _impact_pct}
                        _persist_terminal_classification_best_effort(
                            conn, ev, identity, decision_at, trace,
                            effective_mcap=eff_mc, assumed_micro=assumed_micro,
                            consensus_n_60m=prior, market_stats=market_stats)
                        _alert_entry_result(
                            ev.mint, trade_size, res,
                            handle=getattr(ev, "source_handle", None),
                            ticker=getattr(ev, "ticker", None),
                            armed=_wallet_is_armed(conn, ev, identity))
                        _finish_trace_for_result(trace, res, identity=identity)
                        return res
                impact_cap = cfg.entry_impact_cap_for_mcap(1.0 if assumed_micro else eff_mc)
                micro_class = (cfg.MEMECOIN_REAL_MICRO_POLICY_ENABLED
                               and ladder_band_of(eff_mc, assumed_micro) in ("nano", "micro"))
                if micro_class and not micro_entry_capacity_ok(conn):
                    res = {"status": "rejected",
                           "reason": "micro concurrency cap: %d open micro positions"
                                     % m.open_micro_position_count(conn)}
                    _persist_terminal_classification_best_effort(
                        conn, ev, identity, decision_at, trace,
                        effective_mcap=eff_mc, assumed_micro=assumed_micro,
                        consensus_n_60m=prior, market_stats=market_stats)
                    _alert_entry_result(ev.mint, trade_size, res,
                                        handle=getattr(ev, "source_handle", None),
                                        ticker=getattr(ev, "ticker", None), armed=_wallet_is_armed(conn, ev, identity))
                    _finish_trace_for_result(trace, res, identity=identity)
                    return res
                if fast_lane_refuses:
                    res = {**_fast_lane_refusal(ev, chain, trace),
                           'slot_fallback_class': getattr(ev, 'slot_fallback_class', None)}
                    _persist_terminal_classification_best_effort(
                        conn, ev, identity, decision_at, trace,
                        effective_mcap=eff_mc, assumed_micro=assumed_micro,
                        consensus_n_60m=prior, market_stats=market_stats)
                    _finish_trace_for_result(trace, res, identity=identity)
                    return res
                if (chain == 'solana' and trace is not None
                        and getattr(ev, 'slot_fallback_class', None) is not None):
                    # Gamma LOW-3: the admitted fallback's class on its own attempt row, so the
                    # forward window can grade fills per class (rejections carry it in `res`).
                    try:
                        trace.mark_substage('slot_fallback', slot_fallback_class=ev.slot_fallback_class,
                                            admitted=bool(getattr(ev, 'native_failure_fallback', False)))
                    except Exception:
                        logger.exception('slot fallback class stamp failed for %s', ev.fomo_event_id)
                handed_off = True
                res = real_entry_hook(
                    conn, ev, contexts, now_fn=now_fn, trade_size=trade_size,
                    max_age_s=age_cap, stats_fn=stats_fn, resolved_identity=identity,
                    trace=trace, trace_clock=trace_clock, decision_at=decision_at,
                    cluster_decide_fn=(cluster_client.decide if cluster_client else None),
                    impact_cap_pct=impact_cap,
                    classification_context={
                        "effective_mcap": eff_mc,
                        "assumed_micro": assumed_micro,
                        "consensus_n_60m": prior,
                        "market_stats": market_stats,
                        "mcap_basis": mcap_basis,
                        "momentum_own": momentum_own,
                    },
                )
                return res
            except Exception as exc:
                if not handed_off:
                    _finish_trace_error_best_effort(trace, exc, identity=identity)
                raise
        if getattr(ev, "side", None) == "sell":
            return real_mirror_on_sell(conn, ev, contexts, now_fn=now_fn)
        return None

    def _attempt(conn, ev, *, retry=True):
        # attempt + record, WITHOUT enqueuing — the retry lane uses this so its own re-attempts do
        # not re-enter the queue.
        is_buy = getattr(ev, "side", None) == "buy"
        decision_at = now_fn() if is_buy else None
        if not is_buy:
            return _hook_raw(conn, ev, decision_at=decision_at)
        _window_active = _first_buy_window_applies(ev)
        if _window_active and (retry or getattr(ev, "revival_reask", False)):
            return {"status": "rejected", "chain": "solana", "reason": "source_buy_timer_retry_disabled"}
        startup_refusal = (None if _window_active else
                           _entry_startup_refusal(contexts, getattr(ev, 'network_id', None)))
        if startup_refusal is not None:
            _record_real_decision(conn, ev, startup_refusal, decision_at=decision_at)
            return startup_refusal
        # ENTRY-KNOB STABILITY (2026-09-08). Both call sites into a funded BUY decision pass through
        # here, so this is the one place that can promise a decision never straddles a knob change.
        # The applier runs in the main cycle and defers while this is held; a sell holds nothing,
        # because it reads no entry threshold and would only delay an operator's change.
        from sniper.services.memecoin_entry_knobs import entry_decision
        origin = str(getattr(ev, "fomo_event_id", "") or "")
        claim = ((origin, str(getattr(ev, "source_user_id", "") or ""))
                 if origin.startswith("onchain:56:") else None)
        with entry_decision(origin=claim, delivery=not retry) as owner:
            if not owner:
                return {"status": "duplicate_delivery", "reason": "source decision already owned"}
            from sniper.services.memecoin_j7_backup import is_j7_buy
            shadow = (None if cfg.MEMECOIN_J7_SOLANA_VERIFIED_BACKUP and is_j7_buy(ev)
                      else j7_shadow_event(conn, ev))
            if shadow is None:
                res = _hook_raw(conn, ev, decision_at=decision_at)
            else:
                was = getattr(_entry_alert_suppress, "on", False)
                _entry_alert_suppress.on = True        # a shadow decision never pages
                try:
                    res = _hook_raw(conn, shadow, decision_at=decision_at)
                finally:
                    _entry_alert_suppress.on = was
                res = {**(res or {}), "j7_shadow": True}
            _record_real_decision(conn, ev, res, decision_at=decision_at)
            return res

    def hook(conn, ev):
        # WS2: record every real-path BUY outcome onto its event (both call sites — global feed and
        # follow-activity — go through this one hook, so recording here covers both).
        res = None
        try:
            res = _attempt(conn, ev, retry=False)
        finally:
            if str(getattr(ev, "network_id", "")) in {"56", "4663"} and getattr(ev, "side", None) == "buy":
                context = contexts.get(str(ev.network_id))
                observer = context.get("bsc_execution_observer") if isinstance(context, dict) else None
                if observer is not None:
                    try:
                        observer.record_outcome({
                            "signal_id": getattr(ev, "fomo_event_id", None),
                            "mint": getattr(ev, "mint", None),
                            "source_user_id": getattr(ev, "source_user_id", None),
                            "source_feed": getattr(ev, "signal_source", None),
                            "receipt_clock": getattr(ev, "receipt_clock", None),
                            "reason": (res or {}).get("reason") or (res or {}).get("status", "raised"),
                            "attempt_id": (res or {}).get("attempt_id"),
                            "tx_hash": (res or {}).get("tx_hash"),
                            "status": (res or {}).get("status", "raised"),
                            "eligibility": True if (res or {}).get("status") == "filled" else None})
                    except Exception:
                        pass
        # Fresh-pool retry: a transient no-quote/no-route buy is queued for re-attempt as the pool
        # indexes (module helper so the decision is directly testable). Never a shadow decision.
        if not (res or {}).get("j7_shadow") and (res or {}).get("status") != "duplicate_delivery":
            _maybe_enqueue_retry(conn, ev, res, now_fn())
        return res

    hook.attempt = _attempt   # the retry lane's non-enqueuing attempt path
    # The bare decision: no decision record on the event row and no knob-stability hold. Only the
    # 4663 fast lane's shadow uses it, on a read-only connection whose transaction it rolls back.
    hook.raw = _hook_raw
    return hook
