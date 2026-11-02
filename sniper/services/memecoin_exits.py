"""Per-strategy exit sweep. One batched price_map (from the runner's ~15s bracket pass) drives
every arm's time/price exits: control=SL/TP, momentum_a=rollover machine, alpha_b=wide rails.
Mirror-on-sell is handled in the copy engine (control+alpha_b); momentum_a exits ONLY here."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_copy_trader import _realized_from_exits, record_noquote_cause
from sniper.services.memecoin_momentum import momentum_decision
from sniper.services.memecoin_brackets import (
    combo_f_exit_reason, control_exit_reason, peak_lock_e_exit_reason, trail_d_exit_reason,
)
from sniper.services.memecoin_policy import paper_policy_is_valid, paper_policy_value
from sniper.execution.solana_quote import modeled_fill, quote_for


def _exit_quote_fn(mint, network_id, qty, mid=None, liq=None):
    """Price one paper exit. Production ignores `mid`/`liq` and uses an EXECUTABLE route quote —
    a real route already reflects true depth, so modelled liquidity is redundant there. Both are
    carried so an offline caller (tests) can price from the pool mid through the same seam."""
    from sniper.services.memecoin_copy_trader import executable_exit_fill
    return executable_exit_fill(mint, network_id, qty)

logger = logging.getLogger(__name__)

# Reject an incoming tick when new_mid >= this ratio × the best-known prior price for the same
# (chain, mint) among open positions. Rationale: a dexscreener glitch tick at ~5000x on
# Jotchua/07-14 landed at the moment of exit sweep and cost the paper leaderboard a phantom
# +$376k across two arms — the fingerprint was always the same, price AND liquidity BOTH jumped
# ~10x in one 15s sweep as if a different pool was returned. Real memecoin pumps arrive as a
# rising sequence of marks; a single-sweep 10x+ is virtually always data corruption. Only up-
# moves are guarded — a real rug can legitimately produce a 10x-down tick and we want the stop
# to fire on that. Env-tunable if we ever want to loosen for a specific test.
_SPIKE_RATIO_LIMIT = float(os.getenv("MEMECOIN_SPIKE_RATIO_LIMIT", "10.0"))
# The mirror bound, for implausible DOWN-ticks. Deliberately looser than the up-side: a real
# collapse is a normal market event and must still fire the stop, so only a move no honest
# tape produces in one sweep is rejected. UNIFROG (27097, 2026-08-05) fell 397x in ONE sweep
# and was back within 233x of the spike 28s later -- see the docstring on _quarantine_spikes.
_COLLAPSE_RATIO_LIMIT = float(os.getenv("MEMECOIN_COLLAPSE_RATIO_LIMIT", "20.0"))

# Pair-identity alarm (position 40687 / PURR copycat, 2026-08-19): 661 of 694 marks sat
# quarantined for 25 minutes at a near-constant 0.1114 while the accepted tape moved at
# 2.2-2.6e-05. A run of quarantines that AGREE WITH EACH OTHER is not glitch spam — it is a
# coherent alternative price series, i.e. the source prices a different pool/pair for the
# mint. The guard must keep quarantining (do not weaken it); this only stops the silence.
_PAIR_IDENTITY_STREAK = int(os.getenv("MEMECOIN_PAIR_IDENTITY_ALARM_STREAK", "30"))
_PAIR_IDENTITY_BAND = float(os.getenv("MEMECOIN_PAIR_IDENTITY_ALARM_BAND", "3.0"))
_PAIR_IDENTITY_ALARM_INTERVAL_S = float(os.getenv("MEMECOIN_PAIR_IDENTITY_ALARM_INTERVAL_S", "60"))
_PAIR_ALARM_LAST: dict = {}


def _pair_identity_alarm(conn, key, now_iso):
    """Log one alarm when the newest _PAIR_IDENTITY_STREAK marks for `key` are ALL
    quarantined and their mids agree within _PAIR_IDENTITY_BAND. Never raises."""
    import time as _time
    last = _PAIR_ALARM_LAST.get(key)
    if last is not None and _time.monotonic() - last < _PAIR_IDENTITY_ALARM_INTERVAL_S:
        return
    try:
        chain_id, mint = key
        rows = conn.execute(
            "SELECT mid, quality FROM memecoin_position_marks "
            "WHERE chain_id IS ? AND mint=? ORDER BY sweep_ts DESC LIMIT ?",
            (chain_id, mint, _PAIR_IDENTITY_STREAK)).fetchall()
    except Exception:
        logger.exception("pair-identity alarm lookup failed (guard unaffected)")
        return
    if len(rows) < _PAIR_IDENTITY_STREAK or any(r[1] != "quarantined_spike" for r in rows):
        return
    mids = [float(r[0]) for r in rows if r[0] and float(r[0]) > 0]
    if len(mids) < _PAIR_IDENTITY_STREAK or max(mids) / min(mids) > _PAIR_IDENTITY_BAND:
        return
    _PAIR_ALARM_LAST[key] = _time.monotonic()
    accepted = last_accepted_mid(conn, chain_id, mint, now_iso, window_s=86400.0)
    logger.error(
        "pair-identity alarm: chain=%s mint=%s — the last %d marks are all quarantined and "
        "agree with each other (mid %.6g..%.6g) but not with the accepted tape (%s). The "
        "mark source likely prices a DIFFERENT pool/pair for this mint. Check DexScreener "
        "pair selection and the bound pool's quote mint.",
        chain_id, mint, _PAIR_IDENTITY_STREAK, min(mids), max(mids),
        f"last accepted {accepted:.6g}" if accepted else "no accepted mark in 24h")


def _parse(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _held_s(opened_at, now_iso):
    a, b = _parse(opened_at), _parse(now_iso)
    if not (a and b):
        return 0.0
    held = (b - a).total_seconds()
    if held < 0:
        # Cross-clock: opened_at was stamped on the HOST clock (+offset ahead) before the real-UTC
        # clock switch, but now_iso is real-UTC -> the naive diff undershoots true age by the
        # offset and goes negative for young legacy positions, silently disabling their time cap.
        # Recover true age with the learned host->real offset so the cap still fires. Only applied
        # when negative, so same-clock positions are untouched and nothing closes prematurely.
        from sniper.util.clock import offset_s
        held = max(0.0, held + offset_s())
    return held


# The promotion study's arms, swept on their FROZEN funded rails. Named here and cross-checked
# against the policy resolver by a guard test, so an arm can never again be added to the open path
# and forgotten here.
STUDY_ARMS = ("funded_clone", "profile_matched_v1")

# The label a rail never uses. Shared with nothing, so a reader grouping by exit_reason cannot
# pool these with a rail decision even by accident.
SWEEP_CLOSURE = "sweep_closure"


def _sweep_closure(conn, pos, now_iso):
    """Close a position the sweep never evaluated, WITHOUT inventing an outcome.

    THE BACKLOG. 108 study positions (54 per arm) opened while the exit sweep had no branch for
    their arms, and sat 4 hours against 780-second rails. When the fix lands they would all close
    at once, and under a rail's exit_reason a deploy-triggered mass closure would enter exit-reason
    distributions, hold-duration statistics and every future rail comparison as though the rails
    had decided it. They did not: nothing decided, because nothing looked.

    NO FILL AND A NULL REALIZED PNL. Any price we stamped at these ages would be an invention —
    the coin moved for hours after the rails would have exited, so a mark now prices a position
    that should not have existed. `realized_pnl` stays NULL: **NULL is the unknown, 0 is a known
    zero**, and booking a zero would put 108 fabricated break-evens into the arm statistics. The
    outcomes are unmeasured by construction and must read that way.

    A LABEL CARRIES ITSELF; a timestamp boundary needs the reader to know the rule. That is why
    the distinct exit_reason exists rather than "filter by opened_at < the deploy".
    """
    if not m.claim_close(conn, pos["id"]):
        return None
    m.finalize_close(conn, pos["id"], now_iso, None, exit_reason=SWEEP_CLOSURE)
    return {"position_id": pos["id"], "strategy": pos["strategy"],
            "exit_reason": SWEEP_CLOSURE,
            # Label first, then the measurement — the prefix contract the vol_death audit guard
            # enforces, so a grouping read keys on a stable head rather than a drifting number.
            "detail": "%s: opened %s, never swept, no outcome measured"
                      % (SWEEP_CLOSURE, pos.get("opened_at"))}


def _paper_vol_death_reason(conn, pos, now_iso):
    """`vol_death` for a paper position, decided by the FUNDED rail's own evaluator.

    THE SAME FUNCTION THE FUNDED SWEEP CALLS (`memecoin_real_runner.py:5037`), so a knob move
    shares the decision function. Frozen parameters and position-specific quote witnesses
    can still differ; this is not proof of paper/funded equivalence.

    ⚠️ IT IS JUDGED ONLY WHERE SWAP EVIDENCE EXISTS, AND THAT IS 18% OF PAPER COINS. The rail reads
    `memecoin_position_marks` with `source='fastlane_sol_swap'`, and that tape subscribes per open
    FUNDED position — measured 2026-09-24, 19 of 106 solana paper coins carry any such mark, and
    the 20 coins funded also held are the same set. A paper-only coin therefore has no swap
    evidence, ever.

    THAT IS SAFE BY CONSTRUCTION AND UNSAFE TO READ NAIVELY. The rail refuses to accumulate silence
    unless the stream is provably alive — "quiet is not blind", written after a 926 s outage would
    have fired it on every position at once — so a coin with no marks yields a blind reason and
    never a would-fire. But its firing RATE on paper is measured over coins funded happened to
    hold, so covered and uncovered holds are graded separately and never pooled, which is the
    operator's standing ruling on the funded side too. The `why` is persisted for exactly that
    split.
    """
    if str(pos.get("network_id") or "").strip().lower() != "solana":
        return None
    exit_s = float(paper_policy_value(pos, "voldeath_exit_s", 0.0) or 0.0)
    if exit_s <= 0:
        return None
    try:
        from sniper.settings import memecoin as S      # lazy, matching this module's convention
        from sniper.services.memecoin_volume_death import (
            POST_THRESHOLD_REASONS, armed_exit_decision, stream_healthy)
        fire, silence, why = armed_exit_decision(
            conn, pos, now_iso=now_iso,
            held_s=_held_s(pos.get("opened_at"), now_iso),
            exit_s=exit_s,
            min_opened_at=str(getattr(S, "MEMECOIN_VOLDEATH_ARM_MIN_OPENED_AT", "") or ""),
            stream_ok=stream_healthy(conn, now_iso=now_iso,
                                     chain_id=pos.get("network_id")),
            require_state_witness=bool(getattr(S, "MEMECOIN_VOLDEATH_STATE_WITNESS", True)),
            require_onchain=False)  # paper has no funded entry-bound vault identity
    except Exception:
        logger.exception("paper vol_death evaluation failed for %s", pos.get("id"))
        fire, silence, why = False, None, "evaluation_error"
    if (why in ("blind_frozen", "swap_lane_uncovered", "unmeasurable", "evaluation_error")
            or why in POST_THRESHOLD_REASONS):
        try:
            # Its own reason, never shared with the funded `vol_death` row: the audit UPSERTS on
            # (position_id, reason, checked_minute), so a shared label would replace one verdict
            # with the other whenever both land in the same minute.
            m.record_exit_trigger_audit(
                conn, position_id=pos["id"], reason="paper_vol_death",
                display_price=None, preview_usdc=None, allowed=bool(fire),
                detail="%s silence=%s" % (why, "unknown" if silence is None else "%.1fs" % silence),
                checked_at=now_iso, lane="paper")
        except Exception:
            logger.exception("could not record the paper vol_death witness for %s", pos.get("id"))
    return "vol_death" if fire else None


def _frozen_real_shadow_reason(pos, mid, now_iso, *, conn=None, unquotable=True):
    """Evaluate the entry-frozen paper shadow rails without importing funded policy state.
    `unquotable=False` (the on-chain tick engine): a missing tick is our blindness, not a coin
    without a route, so it never fires the unquotable rail."""
    held = _held_s(pos.get("opened_at"), now_iso)
    entry = pos.get("entry_price") or 0.0
    if mid and mid > 0 and entry > 0:
        pnl = (mid / entry - 1.0) * 100.0
        stop = abs(float(paper_policy_value(pos, "stop_pct", 0.0)))
        catastrophe = abs(float(paper_policy_value(pos, "catastrophe_pct", 0.0)))
        if stop > 0 and pnl <= -stop:
            return "b_stop"
        if catastrophe > 0 and pnl <= -catastrophe:
            return "catastrophe"
        tp = float(paper_policy_value(pos, "tp_pct", 0.0))
        if tp > 0 and pnl >= tp:
            return "real_tp"
        derisk = float(paper_policy_value(pos, "derisk_pct", 0.0))
        if derisk > 0 and not pos.get("derisked_at") and pnl >= derisk:
            return "derisk"
        trail = float(paper_policy_value(pos, "trail_pct", 0.0))
        arm = float(paper_policy_value(pos, "trail_arm_pct", 0.0))
        peak = pos.get("peak_price") or entry
        armed = peak >= entry * (1.0 + arm / 100.0)
        if trail > 0 and armed and mid <= peak * (1.0 - trail / 100.0):
            return "real_trail"
    unquotable_s = float(paper_policy_value(pos, "unquotable_exit_s", 0.0))
    if unquotable and (not mid or mid <= 0) and unquotable_s > 0 and not pos.get("stuck_bag"):
        if _held_s(pos.get("last_quote_ts") or pos.get("opened_at"), now_iso) > unquotable_s:
            return "unquotable"
    # BEFORE THE TIMECAP, AFTER THE PRICE RAILS. A position whose price rail already fired this
    # sweep is closed by that rail on both sides; silence only decides what the timecap would
    # otherwise have held to the end of its window — which is the 15% of funded exits (32 of 206 in
    # 7 days) that had no paper counterpart at all before this.
    if conn is not None:
        vd = _paper_vol_death_reason(conn, pos, now_iso)
        if vd:
            return vd
    max_hold_s = float(paper_policy_value(pos, "max_hold_s", 0.0))
    return "b_timecap" if max_hold_s > 0 and held > max_hold_s else None


def _track_peak(conn, pos, mid, now_iso):
    """Update high-water mark for a non-momentum arm each sweep, so time_to_peak_s / max_gain_pct
    are real (momentum_a tracks its own peak via momentum_decision). Mutates pos in place too."""
    peak = pos.get("peak_price") or (pos["entry_price"] or 0.0)
    if mid > peak:
        m.update_position_peak(conn, pos["id"], mid, now_iso, pos.get("no_new_high_sweeps") or 0)
        pos["peak_price"], pos["peak_ts"] = mid, now_iso


def _close(conn, pos, mid, liq, now_iso, reason, *, exit_price=None, source="dexscreener",
           check_plausible=False):
    """Claim + exit fill + finalize, with peak instrumentation. Normally models slippage on
    `mid`; pass `exit_price` to book an explicit fill with no slippage (e.g. an entry-priced
    0-PnL close for a position we could never value)."""
    if not m.claim_close(conn, pos["id"]):
        return None
    qty = m.open_qty(conn, pos["id"])
    entry = pos["entry_price"] or 0.0
    if qty <= 0:
        reason = "zero_qty_close"
        m.set_position_instrumentation(conn, pos["id"], time_to_peak_s=0.0, max_gain_pct=0.0)
        m.finalize_close(conn, pos["id"], now_iso,
                         _realized_from_exits(conn, pos["id"], entry), exit_reason=reason)
        return {"position_id": pos["id"], "strategy": pos["strategy"], "exit_reason": reason}
    censor_cause = censor_rail = None
    if exit_price is None:
        # EXECUTABLE whole-bag sell, not a mid. Pricing the exit off the same stale DexScreener
        # mid as the entry compounded the optimism on both legs: on a dumping coin the mid lags
        # and paper "sold" above where a route would actually fill. An unavailable quote becomes
        # a NOQUOTE close — which the report censors — rather than a fabricated price.
        exit_price, quote_source = _exit_quote_fn(pos["mint"], pos.get("network_id"), qty, mid, liq)
        if exit_price is not None and (source == "dexscreener" or check_plausible) and not _quote_plausible(
                pos, exit_price, mid):
            # A quote DID come back and this rejected it, so the cause is the veto, never the
            # quote's own reason. Recorded separately for exactly that: it is the one censor
            # that says nothing about whether the coin could be priced.
            exit_price, quote_source = None, "plausibility_veto"
        if exit_price is None:
            censor_cause, censor_rail = quote_source or "quote_unavailable", reason
            exit_price, source, reason = entry, "noquote", f"{reason}_noquote"
        else:
            source = quote_source
    price = exit_price
    m.insert_fill(conn, position_id=pos["id"], kind="bracket_exit", price=price, qty=qty,
                  notional=price * qty, fill_source=source, fraction=1.0, created_at=now_iso,
                  # the price is the ENTRY price, substituted because no quote came back
                  price_attribution=(m.PLACEHOLDER_PRICE_ATTRIBUTION
                                     if source == "noquote" else None))
    peak = pos.get("peak_price") or entry
    tpk = _held_s(pos["opened_at"], pos.get("peak_ts") or pos["opened_at"])
    max_gain = ((peak / entry - 1.0) * 100.0) if entry > 0 else None
    m.set_position_instrumentation(conn, pos["id"], time_to_peak_s=tpk, max_gain_pct=max_gain)
    m.finalize_close(conn, pos["id"], now_iso,
                     _realized_from_exits(conn, pos["id"], entry), exit_reason=reason)
    if censor_cause is not None:
        record_noquote_cause(conn, pos["id"], censor_cause, censor_rail, now_iso)
    return {"position_id": pos["id"], "strategy": pos["strategy"], "exit_reason": reason}


def _quote_plausible(pos, price, mid):
    """False when a route quote is above the configured multiple of max(live mark, peak)."""
    from sniper.settings import memecoin as cfg
    limit = float(getattr(cfg, "MEMECOIN_PAPER_EXIT_MAX_QUOTE_TO_MARK", 0.0) or 0.0)
    try:
        reference = max(float(mid or 0.0), float(pos.get("peak_price") or 0.0))
        price = float(price)
    except (TypeError, ValueError):
        return True
    if limit <= 0 or reference <= 0 or price <= reference * limit:
        return True
    logger.warning("paper exit quote rejected as implausible, booking a censored noquote close: "
                   "pos=%s mint=%s quote=%s mark=%s peak=%s limit=%sx",
                   pos.get("id"), pos.get("mint"), price, mid, pos.get("peak_price"), limit)
    return False


def recover_orphaned_paper_closes(conn, now_iso):
    """Settle PAPER positions that a death between `claim_close` and `finalize_close` stranded
    (is_open=0, closed_at NULL) — the paper twin of `recover_orphaned_closes`.

    `_close` claims (committing is_open=0) and only then runs `_exit_quote_fn`, a route-quote RPC.
    That makes the paper window seconds wide, not sub-millisecond, so an ordinary restart strands
    positions here. Unrecovered they are counted as closes worth exactly $0.00 and escape the
    `_noquote` censor (NULL exit_reason), silently flattening every arm's PnL and win rate.

    Three cases, split on what the booked full-exit legs actually prove:
      executable leg booked -> the modelled sell happened at a transactable price; finalize as a
                               measured outcome.
      only NON-executable   -> the close happened but was never OBSERVED (noquote / stale cleanup
      legs booked              mark). Finalize, but keep a `_noquote` reason so the report CENSORS
                               it. Stamping `recovered_orphan_close` here laundered a censored row
                               into evidence — the exact failure this function exists to prevent
                               (audit 2026-08-03 finding 1).
      no booked fill        -> REOPEN and let the next sweep price it properly. Safe here precisely
                               because paper never broadcasts: unlike the funded path there is no
                               chain state that could double-sell.

    `closed_at` is the newest booked leg's time, NOT when recovery ran — otherwise every recovered
    row's hold is inflated by however long the orphan sat, which feeds median_hold_s and the
    timecap analyses. Idempotent: a finalized row is no longer in `orphaned_closes`.
    Returns the count handled."""
    n = 0
    for pos in m.orphaned_closes(conn, mode="paper"):
        pid = pos["id"]
        legs = m.full_exit_legs(conn, pid)
        if legs["count"] > 0:
            realized = m.realized_after_exit(conn, pid, legs["notional"], pos.get("entry_notional"))
            observed = legs["executable_count"] > 0
            reason = "recovered_orphan_close" if observed else "recovered_orphan_close_noquote"
            m.finalize_close(conn, pid, legs["last_at"] or now_iso, realized, exit_reason=reason)
            logger.warning("paper orphan finalized from booked proceeds: pos=%s mint=%s "
                           "received=%.4f realized=%s observed=%s reason=%s",
                           pid, pos.get("mint"), legs["notional"], realized, observed, reason)
        else:
            m.reopen_position(conn, pid)
            logger.warning("paper orphan reopened (claim won, nothing was sold): pos=%s mint=%s",
                           pid, pos.get("mint"))
        n += 1
    return n


# Source classes for dedupe. `executable` rows are whole-bag route quotes; everything else is
# a pool mid. Two classes, because they answer different questions about the same coin and
# must both be recordable in the same window.
_EXECUTABLE_SOURCES = ("fastlane_rpc",)
PAPER_SWEEP_SOURCE = "paper_dexscreener"     # the paper sweep's provider rows on the mark tape
# `fastlane_sol_state` prices the pool's own reserves each slot (2026-08-14) — same venue truth
# as a swap mark, so it shares the swap dedupe class and the open-position 1s write floor.
_SWAP_SOURCES = ("fastlane_sol_swap", "fastlane_sol_state",
                 # The EVM post-close tape reads the pool's own swap state, the same venue truth,
                 # so it belongs to the swap class rather than to `mid`: sharing the mid class
                 # would let a provider mid and this series silence each other inside the floor,
                 # which is the collision the per-class dedupe exists to prevent.
                 "onchain_tape_post_close_post_swap_anchor")


def _source_class(source):
    # THE PAPER ENGINE'S OWN TICK IS ITS OWN SERIES (Gamma LOW, paper step 1): in 'mid' it shared the
    # per-coin floor with provider mids, and the two silenced each other inside it.
    if str(source or "").startswith("paper_tick:"):
        return "tick"
    if (source or "") in _EXECUTABLE_SOURCES:
        return "executable"
    if (source or "") in _SWAP_SOURCES:
        return "swap"
    return "mid"


def _open_real_keys(conn):
    """(network_id, mint) of every OPEN real position — bounded by MEMECOIN_MAX_CONCURRENT.
    Deliberately real-only: the paper book carries ~860 open positions, and giving those the
    fast floor would reinstate exactly the row growth the 30s dedupe exists to prevent."""
    try:
        keys = {(r[0], r[1]) for r in conn.execute(
            "SELECT DISTINCT network_id, mint FROM memecoin_positions "
            "WHERE is_open=1 AND execution_mode='real'")}
        # POST-CLOSE WINDOW (2026-09-23, operator): a coin closed within
        # MEMECOIN_POST_CLOSE_MARK_S keeps the open floor, or the 30 s universe floor would thin
        # the very minutes the window exists to record.
        from sniper.settings import memecoin as cfg
        for pos in m.recently_closed_real_positions(
                conn, within_s=getattr(cfg, "MEMECOIN_POST_CLOSE_MARK_S", 0.0)):
            keys.add((pos.get("network_id"), pos.get("mint")))
        return keys
    except Exception:
        logger.exception("open-position key lookup failed (falling back to the universe floor)")
        return set()


def _pair_selection_json(stats):
    """The stats read's `pair_selection` record as JSON when the fallback TRIGGERED, else None.

    An untriggered record says only "both picks are today's pick", and DexScreener marks run at about
    33k rows a day, so a record on every row would add about 15 MB a day. A DexScreener mark with NULL
    here therefore means "not triggered". Never fails a mark write.
    """
    record = (stats or {}).get("pair_selection")
    if not isinstance(record, dict) or not record.get("triggered"):
        return None
    try:
        return json.dumps(record, allow_nan=False)
    except (TypeError, ValueError):
        return None


def write_marks(conn, price_map, now_iso, *, min_interval_s=None, open_min_interval_s=None):
    """Persist the sweep's price mark, ONE shared row per (chain_id, mint, sweep). Log-only; never
    raises into the sweep. price_map keys are (chain_id, mint) -> {price, liquidity, mcap}.

    Per-coin write dedupe (2026-07-17): a mark younger than MEMECOIN_MARK_WRITE_MIN_INTERVAL_S for
    the same (chain, mint) skips — the ~15s sweep was re-writing TTL-cached (unchanged) prices as
    new rows, ~20k rows/2h on 4663. DB-derived (MAX(sweep_ts) per coin), so it is restart-safe and
    isolated per test DB. Exit decisions read the live price_map, never these rows.

    Open-position floor (2026-07-22, Phase 2 Unit B): coins with an OPEN REAL position use
    MEMECOIN_MARK_WRITE_MIN_INTERVAL_OPEN_S instead. The 30s universe floor is a RECORDING limit,
    and the Solana tape is pinned against it (24h histogram: 64.8% of gaps in 30-35s, 0.1% under
    30s) — which is why position 12110's executable peak exceeded every mark mid in its window and
    4663 rail decisions could not be audited from this tape at all. The row-growth argument holds
    for thousands of tracked coins, not for the <=7 rows that every rail post-mortem reads.

    NOTE: `executable_exit_value` is written EQUAL to `mid` today — it is a reserved alias for a
    future per-mark executable quote, NOT a net-of-slippage number, and nothing reads it (the app
    reads `mid`; the replay reads `mid` and applies a separate p75 realized-slippage haircut from
    memecoin_exec_latency.impact_pct). It is a per-token PRICE, not a dollar total — do not compare
    it to entry_notional or mid*qty."""
    if min_interval_s is None:
        from sniper.settings.memecoin import MEMECOIN_MARK_WRITE_MIN_INTERVAL_S
        min_interval_s = MEMECOIN_MARK_WRITE_MIN_INTERVAL_S
    if open_min_interval_s is None:
        from sniper.settings.memecoin import MEMECOIN_MARK_WRITE_MIN_INTERVAL_OPEN_S
        open_min_interval_s = MEMECOIN_MARK_WRITE_MIN_INTERVAL_OPEN_S
    try:
        last, now_dt = {}, None
        open_keys = (_open_real_keys(conn)
                     if open_min_interval_s is not None
                     and float(open_min_interval_s) != float(min_interval_s) else set())
        # Dedup lookup: only the coins we're about to write need their last mark — NOT the whole tape.
        # The unfiltered MAX(sweep_ts) GROUP BY scanned all ~500k rows every call (~0.6s); scoping it to
        # this sweep's coins turns it into per-coin index seeks on idx_mc_marks_uniq(chain_id,mint,...).
        pm_keys = list({(k if isinstance(k, tuple) else (None, k)) for k in (price_map or {})})
        _any_floor = (float(min_interval_s or 0) > 0
                      or (bool(open_keys) and float(open_min_interval_s or 0) > 0))
        if _any_floor and pm_keys:
            now_dt = _parse(now_iso)
            clause = " OR ".join("(chain_id=? AND mint=?)" for _ in pm_keys)
            params = [v for key in pm_keys for v in key]
            # Dedupe PER SOURCE CLASS, not per coin (2026-07-22 review): a pool-mid write and
            # an executable (fast-lane) write are two different measurements of the same coin,
            # and keying on (chain, mint) alone let whichever landed first silence the other.
            # The chart grew two series while the tape could only ever hold one of them.
            #
            # SQL groups by RAW source (MAX per source), but several raw sources collapse into
            # one class here — so take the MAX across the class in Python, or a dict overwrite
            # would keep whichever raw row iterated LAST, not the newest in the class, and admit
            # a fresh mark that is actually inside the floor of a newer sibling source.
            last = {}
            for r in conn.execute(
                    "SELECT chain_id, mint, MAX(sweep_ts), source FROM memecoin_position_marks "
                    "WHERE quality != 'quarantined_spike' AND (" + clause + ") "
                    "GROUP BY chain_id, mint, source", params):
                k = (r[0], r[1], _source_class(r[3]))
                ts = _parse(r[2])
                if ts is not None and (last.get(k) is None or ts > last[k]):
                    last[k] = ts
        for key, st in (price_map or {}).items():
            chain_id, mint = key if isinstance(key, tuple) else (None, key)
            mid = (st or {}).get("price")
            if not mid or mid <= 0:
                continue
            candidate_iso = (st or {}).get("observed_at") or now_iso
            candidate_dt = _parse(candidate_iso)
            if candidate_dt is None:
                continue
            if now_dt is None:
                now_dt = _parse(now_iso)
            if (st or {}).get("observed_at") and (
                now_dt is None or candidate_dt > now_dt
            ):
                continue
            prev = last.get((chain_id, mint, _source_class(st.get("source"))))
            # (network_id, mint) identity, never mint alone — 07-15 keying finding.
            floor = (float(open_min_interval_s) if (chain_id, mint) in open_keys
                     else float(min_interval_s))
            if prev is not None and candidate_dt <= prev:
                continue
            if prev is not None and (candidate_dt - prev).total_seconds() < floor:
                continue
            _buys, _sells = st.get("txns_m5_buys"), st.get("txns_m5_sells")
            try:
                _txns = (None if _buys is None and _sells is None
                         else int(float(_buys or 0)) + int(float(_sells or 0)))
            except (TypeError, ValueError):
                _txns = None
            conn.execute(
                "INSERT OR IGNORE INTO memecoin_position_marks("
                "chain_id, mint, sweep_ts, mid, executable_exit_value, liquidity, mcap, source, quality,"
                " volume_m5, volume_h1, txns_m5, pair_selection_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (chain_id, mint, candidate_iso, mid, mid, st.get("liquidity"),
                 # TWO PROVIDERS, TWO KEY NAMES. DexScreener returns `market_cap`; the EVM price
                 # batch returns `mcap`. Reading only `mcap` stored NULL for every DexScreener row,
                 # which is why 4663 marks were 12.3% populated before the GeckoTerminal removal and
                 # 0% after -- and why an operator mcap target had no current number to cross.
                 st.get("mcap") or st.get("market_cap"),
                 st.get("source", "dexscreener"), st.get("quality", "live"),
                 st.get("volume_m5"), st.get("volume_h1"), _txns,
                 _pair_selection_json(st)))
            # Backfill entry_liquidity from the FIRST mark on an open position that has none.
            # The column existed but was written on 0 of 294 funded positions, because the
            # funded executors never pass it and memecoin_token_liquidity holds only chain
            # 4663 — Solana has no rows there. The marks path already carries liquidity on
            # 2.49M of 3.19M rows, so the first mark is the earliest liquidity we actually
            # observe. It is a few seconds AFTER entry, not at entry, so treat it as a close
            # proxy and never as the exact entry-time book.
            if st.get("liquidity"):
                conn.execute(
                    "UPDATE memecoin_positions SET entry_liquidity=? "
                    "WHERE mint=? AND is_open=1 AND entry_liquidity IS NULL",
                    (st.get("liquidity"), mint))
        conn.commit()
    except Exception:
        logger.exception("position mark write failed (sweep unaffected)")


def _prior_prices(conn):
    """(chain_id, mint) -> best-known plausible prior mid for the spike guard.

    Uses max(last_quote_price, entry_price) across all OPEN positions on that (chain, mint) — a
    fresh entry that has never been quoted still has an entry_price, so the very first sweep is
    still guarded. Positions across strategies (control / momentum_a / alpha_b / real_shadow /
    real) all share the same (chain, mint) baseline. One tiny query per sweep, no per-position
    round-trips.
    """
    prior: dict[tuple, float] = {}
    try:
        rows = conn.execute(
            "SELECT network_id, mint, last_quote_price, entry_price "
            "FROM memecoin_positions WHERE is_open=1"
        ).fetchall()
    except Exception:
        logger.exception("prior-price lookup failed (spike guard degraded to pass-through)")
        return prior
    for row in rows:
        try:
            chain, mint = m.coin_identity(row[0], row[1])
            best = max(float(row[2] or 0.0), float(row[3] or 0.0))
        except Exception:
            continue
        if best <= 0:
            continue
        key = (chain, mint)
        cur = prior.get(key, 0.0)
        if best > cur:
            prior[key] = best
    return prior


# The reference must be RECENT. A long window means a genuine one-tick collapse is quarantined
# and its floor compared against a stale pre-rug mark for the whole window -- the position cannot
# exit at all. Two minutes: open positions are marked every 1-2s, so a live coin always has a
# reference, while a real rug releases both guards within two minutes instead of an hour.
_ACCEPTED_MID_WINDOW_S = float(os.getenv("MEMECOIN_ACCEPTED_MID_WINDOW_S", "120"))


def _last_accepted_mids(conn, now_iso, keys, *, window_s=None):
    """{(chain_id, mint): mid} -- newest NON-quarantined mid STRICTLY BEFORE `now_iso`.

    Bounded on BOTH sides. Without an upper bound a backdated or replayed sweep reads marks from
    its own future, and the strict `<` keeps the candidate's own mark out of its own reference
    even when the writer stamps it with this very sweep's timestamp.

    Queried per candidate key so it rides the (chain_id, mint) index: the unfiltered form scanned
    all 1.88M marks at ~0.2s per call against a 1s funded cadence.
    """
    window = float(_ACCEPTED_MID_WINDOW_S if window_s is None else window_s)
    now = _parse(now_iso)
    if now is None or not keys:
        return {}
    floor_ts = (now - timedelta(seconds=window)).isoformat()
    now_ts = now.isoformat()
    latest: dict[tuple, float] = {}
    for key in keys:
        try:
            chain_id, mint = key
        except (TypeError, ValueError):
            continue
        try:
            # A former bag's post-close ratio is observation-only, including after re-entry.
            row = conn.execute(
                "SELECT mid FROM memecoin_position_marks "
                "WHERE chain_id IS ? AND mint=? AND quality != 'quarantined_spike' AND mid > 0 "
                "AND COALESCE(source,'')!='onchain_tape_post_close_post_swap_anchor' "
                "AND sweep_ts >= ? AND sweep_ts < ? "
                "ORDER BY sweep_ts DESC LIMIT 1",
                (chain_id, mint, floor_ts, now_ts)).fetchone()
        except Exception:
            logger.exception("accepted-mid lookup failed (collapse guard degraded to pass-through)")
            continue
        if row and row[0] and float(row[0]) > 0:
            latest[key] = float(row[0])
    return latest


def last_accepted_mid(conn, chain_id, mint, now_iso, *, window_s=None):
    """Newest NON-quarantined mid for one coin, strictly before `now_iso`, or None.

    The reference the exit-floor sanity check needs: derived OUTSIDE the quote being signed,
    immune to a tick the collapse guard has rejected, and RECENT -- an aged-out reference returns
    None so the floor check allows the exit rather than stranding a genuinely collapsed position.
    """
    key = (chain_id, mint)
    return _last_accepted_mids(conn, now_iso, (key,), window_s=window_s).get(key)


def _quarantine_spikes(conn, price_map, now_iso, *, limit=None, collapse_limit=None):
    """Return a filtered price_map with implausible up-spike ticks removed.

    Rejection rule: new_mid >= `limit` × best-known prior mid for the same (chain, mint) among
    open positions. Quarantined ticks are still persisted to memecoin_position_marks with
    quality='quarantined_spike' so forensics keep the evidence — the exit decision path just
    never sees them, so it can't fabricate a phantom TP/PnL from the glitch.

    The bad tick that motivated this guard (Jotchua, 07-14): prior mid $0.003402 -> new mid
    $17.099 in ONE 15s sweep (~5000x), liquidity jumped 10x too. Both `control` and
    `real_shadow` arms exited at the phantom price, each booking +$376k paper PnL. See
    finding-memecoin-bad-tick-spike-guard-2026-07-14 for the DB proof.

    DOWN-side, added 2026-08-05 after UNIFROG (position 27097, REAL, 4663). The tape printed
    0.0002514992, then 0.0000006323 on the very next sweep -- a 397x collapse -- held it for five
    marks over seven seconds, and was back at 0.0002288854 twenty-eight seconds later. The minimum
    mid over the whole rest of the coin's life was 233x ABOVE the spike, so the collapse was never
    real. It cost real money twice over: `catastrophe` crossed its 80% threshold on the corrupted
    price (the honest decline had only reached 74%), and the exit quote taken inside the window set
    `min_output_atomic` to ~$0.02, so the slippage floor authorised selling a ~$6 bag for $0.02.

    A real rug must still fire the stop, so the down bound is measured against the last ACCEPTED
    tape mark rather than the entry: a genuine collapse walks the reference down sweep by sweep and
    stays admissible, while a single-sweep implausibility is rejected. If a broken source holds the
    price down for a long time nothing new is accepted, the position stops being quotable, and the
    existing `unquotable_exit_s` rail owns it -- which is the honest outcome, because at that point
    we do not know the price.
    """
    limit = float(limit if limit is not None else _SPIKE_RATIO_LIMIT)
    collapse = float(collapse_limit if collapse_limit is not None else _COLLAPSE_RATIO_LIMIT)
    prior = _prior_prices(conn)
    # RAW keys, exactly as write_marks persists them -- not the coin_identity-normalised form the
    # up-side `prior` map uses. Normalising here turned every lookup into a miss, and the miss then
    # fell back to a full scan of 1.88M marks.
    candidate_keys = tuple(
        key if isinstance(key, tuple) else (None, key) for key in (price_map or {})
    )
    accepted = _last_accepted_mids(conn, now_iso, candidate_keys) if collapse > 1 else {}
    clean: dict = {}
    quarantined: list = []
    for key, st in (price_map or {}).items():
        mid = (st or {}).get("price") or 0.0
        prior_key = key if isinstance(key, tuple) else m.coin_identity(None, key)
        prior_mid = prior.get(prior_key, 0.0)
        accepted_mid = accepted.get(key if isinstance(key, tuple) else (None, key), 0.0)
        spiked_up = prior_mid > 0 and mid > 0 and (mid / prior_mid) >= limit
        collapsed = (
            collapse > 1 and accepted_mid > 0 and mid > 0
            and (accepted_mid / mid) >= collapse
        )
        if spiked_up or collapsed:
            reference = prior_mid if spiked_up else accepted_mid
            ratio = (mid / prior_mid) if spiked_up else (accepted_mid / mid)
            quarantined.append((prior_key, mid, reference, ratio,
                                (st or {}).get("liquidity"), (st or {}).get("source")))
            try:
                chain_id, mint = prior_key
                conn.execute(
                    "INSERT OR IGNORE INTO memecoin_position_marks("
                    "chain_id, mint, sweep_ts, mid, executable_exit_value, liquidity, mcap, "
                    "source, quality) VALUES(?,?,?,?,?,?,?,?,?)",
                    (chain_id, mint, now_iso, mid, mid, (st or {}).get("liquidity"),
                     (st or {}).get("mcap") or (st or {}).get("market_cap"),
                     (st or {}).get("source", "dexscreener"),
                     "quarantined_spike"))
            except Exception:
                logger.exception("quarantine mark write failed (sweep unaffected)")
        else:
            clean[key] = st
    if quarantined:
        try:
            conn.commit()
        except Exception:
            pass
        logger.warning(
            "sweep: quarantined %d implausible spike(s) at %s: %s",
            len(quarantined), now_iso,
            [(k, round(mid, 12), round(pr, 12),
              (f"{ratio:.0f}x up" if mid > pr else f"{ratio:.0f}x down"), liq, src)
             for k, mid, pr, ratio, liq, src in quarantined[:5]])
        for key in {q[0] for q in quarantined}:
            _pair_identity_alarm(conn, key, now_iso)
    return clean


def filter_implausible_ticks(conn, price_map, now_iso, *, limit=None, collapse_limit=None):
    """Public entry point for the tick guard, for callers that persist marks themselves.

    The funded fast lane writes its exact-route map with `write_marks` and only afterwards runs
    `real_exit_sweep`, so a tick validated inside the sweep has ALREADY been persisted as an
    accepted mark -- it becomes its own reference, the ratio collapses to 1x and the guard cannot
    fire. Callers must filter BEFORE their first write. Idempotent: re-filtering a clean map is a
    no-op, so the sweep's own internal call stays harmless.
    """
    return _quarantine_spikes(conn, price_map, now_iso, limit=limit, collapse_limit=collapse_limit)


def study_arm_preflight(conn, now_iso, *, backlog_s=6 * 3600.0):
    """Two silent failures the study arms can enter. Returns a list of warning strings.

    BOTH ARE SILENT BY CONSTRUCTION, which is why they need a voice rather than a test.

    ONE: an unset `MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE` with a backlog present. The empty cutoff is
    the correct default and is inert almost always, but a position opened before the sweep began
    evaluating this arm was never judged by any rail, and with no cutoff it silently takes rails
    that did not decide it. The absence of a boundary looks exactly like the absence of a backlog,
    and only one of those is safe.

    TWO: an open study position whose frozen assignment does not parse. `paper_policy_value` falls
    back on a malformed row, so EVERY rail reverts to a default and the position stops being the
    arm it was assigned to while still reporting under that arm's name. No error, no exception, no
    visible difference in the ledger. A test fixture of mine hit this exact state and the arm read
    as pure fallback.
    """
    from sniper.services.memecoin_policy import paper_policy_is_valid
    from sniper.settings import memecoin as S          # lazy, matching this module's convention
    warnings = []
    cutoff = str(getattr(S, "MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE", "") or "")
    for arm in STUDY_ARMS:
        for pos in m.open_positions_by_strategy(conn, arm):
            opened = str(pos.get("opened_at") or "")
            if not cutoff and opened and _held_s(opened, now_iso) > backlog_s:
                warnings.append(
                    "%s position %s opened %s and MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE is unset: a "
                    "backlog position no sweep ever evaluated will take rails that did not decide "
                    "it" % (arm, pos.get("id"), opened))
            if not paper_policy_is_valid(pos):
                warnings.append(
                    "%s position %s has no valid frozen assignment: every rail silently falls back "
                    "to a default, so it is not running the arm it reports as"
                    % (arm, pos.get("id")))
    for w in warnings:
        logger.warning("study arm preflight: %s", w)
    return warnings


def _v1_engine(pos):
    from sniper.services.memecoin_paper_ticks import V1, exit_engine
    return exit_engine(pos) == V1


def _v1_tick_written_within_s(pos, S):
    """How old the v1 engine's last_quote_ts can be on a live position: it writes every
    PERSIST_QUOTE_S, a tick may be MEMECOIN_PAPER_TICK_MAX_AGE_S old, and a 56 bag quote comes once
    per MEMECOIN_PAPER_TICK_56_PERIOD_S."""
    from sniper.services.memecoin_paper_ticks import PERSIST_QUOTE_S
    bound = PERSIST_QUOTE_S + float(S.MEMECOIN_PAPER_TICK_MAX_AGE_S)
    if str(pos.get("network_id")) == "56":
        bound += float(S.MEMECOIN_PAPER_TICK_56_PERIOD_S)
    return bound


def _age_s(iso, now_iso):
    """Seconds from `iso` to `now_iso`; infinity when either cannot be read (never fresh)."""
    then, now = _parse(iso), _parse(now_iso)
    return (now - then).total_seconds() if then is not None and now is not None else float("inf")


def _without_v1_only_coins(conn, price_map):
    """The sweep's map minus the coins that only onchain_ticks_v1 paper positions hold (paper step 1,
    spec 2026-10-08): their marks are the v1 engine's own ticks, `paper_tick:<kind>`, so a provider
    mid beside them would be a second basis on the same chart. A coin a v0 position holds keeps its
    provider mark. Exit decisions still read the full map; only the mark write is narrowed."""
    from sniper.execution.solana_quote import quote_key
    from sniper.services.memecoin_paper_ticks import V1, exit_engine
    v1, v0 = set(), set()
    for pos in m.list_open_positions(conn, mode="paper"):
        (v1 if exit_engine(pos) == V1 else v0).add(quote_key(pos.get("network_id"), pos["mint"]))
    drop = v1 - v0
    if not drop:
        return price_map
    return {key: st for key, st in (price_map or {}).items()
            if (key if isinstance(key, tuple) else quote_key(None, key)) not in drop}


def sweep_exits(conn, price_map, now_iso, *, cfg=None, tc=None):
    """`tc` = optional ticks-DB connection. When None (today's callers) behaviour is byte-identical;
    when supplied, momentum_a positions on 4663 may be priced from the tick tape (see `_tape_mid`)."""
    from sniper.settings import memecoin as S
    closed = []
    # Corrupt forward assignments may use current settings only for lifecycle safety; exclude them
    # durably before any fallback can affect a decision result.
    for pos in m.list_open_positions(conn, mode="paper"):
        if (pos.get("experiment_status") in ("eligible", "excluded_fill")
                and not paper_policy_is_valid(pos)):
            m.set_experiment_status(conn, pos["id"], "invalid_policy")
    # THE PAPER SWEEP NAMES ITS PROVIDER ROWS (operator via Alpha, 2026-10-08, Douyin 72229). The mark
    # tape is keyed (chain, mint), so a paper twin's DexScreener mids landed on the funded coin's chart
    # as 'dexscreener'. They are written as 'paper_dexscreener'; own-source rows keep their labels.
    # No decision here reads the label (the closes name their own price_source below).
    price_map = {key: (dict(st, source=PAPER_SWEEP_SOURCE)
                       if isinstance(st, dict) and st.get("source") in (None, "dexscreener") else st)
                 for key, st in (price_map or {}).items()}
    # Filter implausible up-spikes BEFORE anything else reads price_map — this way write_marks
    # sees only clean ticks (quarantined ones are already logged with quality='quarantined_spike')
    # and every downstream exit decision is protected from a glitched tick fabricating a phantom TP.
    price_map = _quarantine_spikes(conn, price_map, now_iso)
    # LIQUIDITY ENRICHMENT (2026-07-22): blind-chain (4663) quote sources carry no liquidity, so
    # every paper exit there booked modeled_fill's unknown-liquidity CAP (0.5) — halving each
    # close vs mid and distorting all arm evidence (found via the replay reconciliation,
    # 2026-07-21). Fill the gap from the runner-persisted pool reserves
    # (memecoin_token_liquidity, <=2h fresh) so slippage models the actual pool; coins with no
    # stored reserve keep the conservative cap. write_marks runs AFTER this, so the marks tape
    # inherits the enriched value and the replay convention stays consistent with live fills.
    for _key, _st in (price_map or {}).items():
        if not isinstance(_st, dict) or (_st.get("liquidity") or 0) > 0:
            continue
        if not isinstance(_key, tuple):
            continue                       # bare-mint legacy keys carry no chain identity
        _stored = m.get_token_liquidity(conn, _key[0], _key[1], now_iso=now_iso)
        if _stored:
            _st["liquidity"] = _stored
    write_marks(conn, _without_v1_only_coins(conn, price_map), now_iso)

    def _mid_liq(pos):
        st = quote_for(price_map, pos["mint"], pos.get("network_id")) or {}
        return st.get("price"), st.get("liquidity")

    # 4663 tape pricing. quote_usd_price() is a NETWORK call, so it is resolved lazily and at most
    # once per sweep — only when a 4663 position actually needs pricing. With the arm still blocked
    # on 4663 nothing reaches it, so this costs nothing today.
    _tape_quote_usd = []

    def _tape_mid(pos):
        if tc is None or str(pos.get("network_id") or "") != "4663":
            return (None, None)
        if not _tape_quote_usd:
            try:
                from sniper.execution.evm_wallet import quote_usd_price
                _tape_quote_usd.append(quote_usd_price("4663"))
            except Exception:
                logger.exception("tape mid: quote USD lookup failed")
                _tape_quote_usd.append(None)
        _now_dt = _parse(now_iso)
        now_ts = _now_dt.timestamp() if _now_dt else None
        if now_ts is None:
            return (None, None)
        return tape_mid_for_position(tc, pos, quote_usd=_tape_quote_usd[0], now_ts=now_ts)

    def _timecap_no_quote(pos, max_hold_s, base):
        """Fire a TIME cap for a position we can't price this sweep (unquotable mint) so it
        does not ride open forever (zombie leak). Close at the LAST live mid we saw
        (conservative slippage, liquidity unknown -> cap), tagged `_stale`; if we never got a
        quote, close at entry (0 PnL) tagged `_noquote` so no PnL is fabricated for a position
        we could never value. Price-based exits still require a live quote — only time fires here."""
        if not pos["entry_price"] or _held_s(pos["opened_at"], now_iso) <= max_hold_s:
            return None
        last = pos.get("last_quote_price")
        if last and last > 0:
            return _close(conn, pos, last, None, now_iso, base + "_stale", source="dexscreener_stale")
        return _close(conn, pos, None, None, now_iso, base + "_noquote",
                      exit_price=pos["entry_price"], source="noquote")

    # control: SL/TP on mid (no time cap by design -> unquotable control positions just wait
    # for a quote; only the time-capped arms below force-close when dark).
    for pos in m.open_positions_by_strategy(conn, "control"):
        mid, liq = _mid_liq(pos)
        if not mid or mid <= 0 or not pos["entry_price"]:
            continue
        m.update_position_last_quote(conn, pos["id"], mid, now_iso)
        _track_peak(conn, pos, mid, now_iso)   # real peak -> real exit-liquidity meter
        pnl = (mid / pos["entry_price"] - 1.0) * 100.0
        reason = control_exit_reason(
            pnl,
            paper_policy_value(pos, "stop_pct", S.MEMECOIN_STOP_LOSS_PCT),
            paper_policy_value(pos, "tp_pct", S.MEMECOIN_TAKE_PROFIT_PCT),
        )
        if reason:
            r = _close(conn, pos, mid, liq, now_iso, reason)
            if r:
                closed.append(r)

    # scalp_c / scalp_c2: forward PAPER experiments. The exact policy JSON was frozen in the position
    # row at entry; never call resolve_policy here or a config/code change would rewrite an open
    # trade. Both arms are policy-driven and share this branch — the strategy column keeps them apart.
    from sniper.services.memecoin_policy import deserialize_policy, policy_exit_reason
    for pos in (m.open_positions_by_strategy(conn, "scalp_c")
                + m.open_positions_by_strategy(conn, "scalp_c2")):
        try:
            policy = deserialize_policy(pos.get("assigned_policy_json"))
        except ValueError:
            logger.exception("scalp position has no valid frozen policy pos=%s", pos["id"])
            continue
        mid, liq = _mid_liq(pos)
        if not mid or mid <= 0 or not pos["entry_price"]:
            r = _timecap_no_quote(pos, policy.max_hold_s, "scalp_timecap")
            if r:
                closed.append(r)
            continue
        m.update_position_last_quote(conn, pos["id"], mid, now_iso)
        _track_peak(conn, pos, mid, now_iso)
        pnl = (mid / pos["entry_price"] - 1.0) * 100.0
        reason = policy_exit_reason(policy, pnl, _held_s(pos["opened_at"], now_iso))
        if reason:
            r = _close(conn, pos, mid, liq, now_iso, f"scalp_{reason}")
            if r:
                closed.append(r)

    # momentum_a: rollover machine + peak persistence
    for pos in m.open_positions_by_strategy(conn, "momentum_a"):
        mid, liq = _mid_liq(pos)
        price_source = "dexscreener" if mid else None
        _t_mid, _t_src = _tape_mid(pos)
        if _t_mid:
            mid, price_source = _t_mid, _t_src      # tape beats DexScreener on 4663
        if not mid or mid <= 0 or not pos["entry_price"]:
            r = _timecap_no_quote(
                pos, paper_policy_value(pos, "max_hold_s", S.MEMECOIN_A_MAX_HOLD_S),
                "a_timecap",
            )
            if r:
                closed.append(r)
            continue
        m.update_position_last_quote(conn, pos["id"], mid, now_iso)
        d = momentum_decision(
            entry_price=pos["entry_price"], peak_price=pos.get("peak_price") or pos["entry_price"],
            no_new_high_sweeps=pos.get("no_new_high_sweeps") or 0, price=mid,
            held_s=_held_s(pos["opened_at"], now_iso),
            rollover_sweeps=paper_policy_value(
                pos, "rollover_sweeps", S.MEMECOIN_A_ROLLOVER_SWEEPS),
            trail_pct=paper_policy_value(pos, "trail_pct", S.MEMECOIN_A_TRAIL_PCT),
            stop_pct=paper_policy_value(pos, "stop_pct", S.MEMECOIN_A_STOP_PCT),
            max_hold_s=paper_policy_value(pos, "max_hold_s", S.MEMECOIN_A_MAX_HOLD_S))
        # peak_ts advances only on a new high
        new_peak_ts = now_iso if d["peak_price"] > (pos.get("peak_price") or 0) else pos.get("peak_ts")
        m.update_position_peak(conn, pos["id"], d["peak_price"], new_peak_ts, d["no_new_high_sweeps"])
        if d["exit_reason"]:
            pos2 = m.get_position(conn, pos["id"])   # re-read with updated peak for instrumentation
            r = _close(conn, pos2, mid, liq, now_iso, d["exit_reason"],
                       source=price_source or "dexscreener")
            if r:
                closed.append(r)

    # alpha_b: wide catastrophe stop + long time cap (NO TP; mirror handled in copy engine)
    for pos in m.open_positions_by_strategy(conn, "alpha_b"):
        mid, liq = _mid_liq(pos)
        if not mid or mid <= 0 or not pos["entry_price"]:
            r = _timecap_no_quote(
                pos, paper_policy_value(pos, "max_hold_s", S.MEMECOIN_B_MAX_HOLD_S),
                "b_timecap",
            )
            if r:
                closed.append(r)
            continue
        m.update_position_last_quote(conn, pos["id"], mid, now_iso)
        _track_peak(conn, pos, mid, now_iso)   # real peak -> real exit-liquidity meter
        pnl = (mid / pos["entry_price"] - 1.0) * 100.0
        stop_pct = paper_policy_value(pos, "stop_pct", S.MEMECOIN_B_STOP_PCT)
        max_hold_s = paper_policy_value(pos, "max_hold_s", S.MEMECOIN_B_MAX_HOLD_S)
        reason = ("b_stop" if pnl <= -stop_pct
                  else "b_timecap" if _held_s(pos["opened_at"], now_iso) > max_hold_s
                  else None)
        if reason:
            r = _close(conn, pos, mid, liq, now_iso, reason)
            if r:
                closed.append(r)

    # trail_d: mirror-sell (copy engine) + a trailing stop that ARMS after +activate% then exits
    # trail% off the peak, with a wide catastrophe stop + timecap. Tracks its own peak (the trailing
    # logic reads peak_price). Unquotable -> wide timecap so a dark bag can't ride forever.
    for pos in m.open_positions_by_strategy(conn, "trail_d"):
        mid, liq = _mid_liq(pos)
        if not mid or mid <= 0 or not pos["entry_price"]:
            r = _timecap_no_quote(
                pos, paper_policy_value(pos, "max_hold_s", S.MEMECOIN_TRAIL_D_MAX_HOLD_S),
                "d_timecap",
            )
            if r:
                closed.append(r)
            continue
        m.update_position_last_quote(conn, pos["id"], mid, now_iso)
        _track_peak(conn, pos, mid, now_iso)          # advance peak BEFORE reading it for the trail
        pos = m.get_position(conn, pos["id"])         # re-read so peak_price includes this tick
        reason = trail_d_exit_reason(
            entry_price=pos["entry_price"], mid=mid, peak_price=pos.get("peak_price"),
            held_s=_held_s(pos["opened_at"], now_iso),
            activate_pct=paper_policy_value(
                pos, "activate_pct", S.MEMECOIN_TRAIL_D_ACTIVATE_PCT),
            trail_pct=paper_policy_value(pos, "trail_pct", S.MEMECOIN_TRAIL_D_TRAIL_PCT),
            stop_pct=paper_policy_value(pos, "stop_pct", S.MEMECOIN_TRAIL_D_STOP_PCT),
            max_hold_s=paper_policy_value(pos, "max_hold_s", S.MEMECOIN_TRAIL_D_MAX_HOLD_S))
        if reason:
            r = _close(conn, pos, mid, liq, now_iso, reason)
            if r:
                closed.append(r)

    # peak_lock_e: PAPER-only challenger. It shares trail_d's activation/stop/timecap registration
    # but harvests after N no-new-high sweeps instead of waiting for a percentage drawdown.
    for pos in m.open_positions_by_strategy(conn, "peak_lock_e"):
        mid, liq = _mid_liq(pos)
        if not mid or mid <= 0 or not pos["entry_price"]:
            max_hold_s = paper_policy_value(pos, "max_hold_s", S.MEMECOIN_PEAK_E_MAX_HOLD_S)
            r = (_timecap_no_quote(pos, max_hold_s, "e_timecap") if max_hold_s > 0 else None)
            if r:
                closed.append(r)
            continue
        m.update_position_last_quote(conn, pos["id"], mid, now_iso)
        prior_peak = pos.get("peak_price") or pos["entry_price"]
        if mid > prior_peak:
            peak, peak_ts, stalled = mid, now_iso, 0
        else:
            peak = prior_peak
            peak_ts = pos.get("peak_ts") or pos["opened_at"]
            stalled = (pos.get("no_new_high_sweeps") or 0) + 1
        m.update_position_peak(conn, pos["id"], peak, peak_ts, stalled)
        pos = m.get_position(conn, pos["id"])
        reason = peak_lock_e_exit_reason(
            entry_price=pos["entry_price"], mid=mid, peak_price=pos.get("peak_price"),
            no_new_high_sweeps=pos.get("no_new_high_sweeps"),
            held_s=_held_s(pos["opened_at"], now_iso),
            activate_pct=paper_policy_value(
                pos, "activate_pct", S.MEMECOIN_PEAK_E_ACTIVATE_PCT),
            stall_sweeps=paper_policy_value(
                pos, "stall_sweeps", S.MEMECOIN_PEAK_E_STALL_SWEEPS),
            stop_pct=paper_policy_value(pos, "stop_pct", S.MEMECOIN_PEAK_E_STOP_PCT),
            max_hold_s=paper_policy_value(pos, "max_hold_s", S.MEMECOIN_PEAK_E_MAX_HOLD_S),
        )
        if reason:
            r = _close(conn, pos, mid, liq, now_iso, reason)
            if r:
                closed.append(r)

    # combo_f: partial de-risk 50% at the first +20% crossing, remainder trails 30% off the running
    # peak; catastrophe stop always live; NO trail before the partial (mirror + stop protect it).
    # The P1/P2 combination from the 2026-07-22 stop/derisk redesign study — PAPER challenger,
    # matched-pair vs control. Mirror-sell is handled in the copy engine (combo_f in MIRROR_ARMS).
    for pos in m.open_positions_by_strategy(conn, "combo_f"):
        mid, liq = _mid_liq(pos)
        if not mid or mid <= 0 or not pos["entry_price"]:
            max_hold_s = paper_policy_value(pos, "max_hold_s", S.MEMECOIN_COMBO_F_MAX_HOLD_S)
            if max_hold_s > 0:
                r = _timecap_no_quote(pos, max_hold_s, "f_timecap")
                if r:
                    closed.append(r)
            continue
        m.update_position_last_quote(conn, pos["id"], mid, now_iso)
        _track_peak(conn, pos, mid, now_iso)
        pos = m.get_position(conn, pos["id"])         # re-read so peak_price includes this tick
        reason = combo_f_exit_reason(
            entry_price=pos["entry_price"], mid=mid, peak_price=pos.get("peak_price"),
            derisked=bool(pos.get("derisked_at")),
            held_s=_held_s(pos["opened_at"], now_iso),
            trigger_pct=paper_policy_value(pos, "trigger_pct", S.MEMECOIN_COMBO_F_TRIGGER_PCT),
            trail_pct=paper_policy_value(pos, "trail_pct", S.MEMECOIN_COMBO_F_TRAIL_PCT),
            stop_pct=paper_policy_value(pos, "stop_pct", S.MEMECOIN_COMBO_F_STOP_PCT),
            max_hold_s=paper_policy_value(pos, "max_hold_s", S.MEMECOIN_COMBO_F_MAX_HOLD_S))
        if reason == "f_partial":
            r = _combo_partial(
                conn, pos, mid, liq, now_iso,
                paper_policy_value(pos, "fraction", S.MEMECOIN_COMBO_F_FRACTION),
            )
            if r:
                closed.append(r)
        elif reason:
            r = _close(conn, pos, mid, liq, now_iso, reason)
            if r:
                closed.append(r)

    # real_shadow: a PAPER observation of the real-leg rails. New cohorts use the immutable
    # entry-time copy; legacy/operator-modified rows retain the historical mutable decision path.
    for pos in m.open_positions_by_strategy(conn, "real_shadow"):
        mid, liq = _mid_liq(pos)
        ov = m.effective_exit_override(conn, pos["id"])   # mirror per-position overrides on the paper leg
        cur_mcap = (quote_for(price_map, pos["mint"], pos.get("network_id")) or {}).get("market_cap")
        frozen = not ov and pos.get("experiment_status") in ("eligible", "excluded_fill")
        if not frozen:
            from sniper.services.memecoin_real_runner import alpha_b_real_exit_reason
            decide = lambda value: alpha_b_real_exit_reason(
                pos, value, now_iso, override=ov, current_mcap=cur_mcap)
        else:
            decide = lambda value: _frozen_real_shadow_reason(pos, value, now_iso, conn=conn)
        if not mid or mid <= 0 or not pos["entry_price"]:
            # unquotable: only the TIME rails can fire (no price to stop on). Reuse the real
            # decision with mid=None so the SAME timecap/unquotable thresholds apply.
            reason = decide(None)
            if reason:
                r = _timecap_no_quote(pos, 0.0, reason)   # close at last mid / entry, tagged reason
                if r:
                    closed.append(r)
            continue
        m.update_position_last_quote(conn, pos["id"], mid, now_iso)
        _track_peak(conn, pos, mid, now_iso)
        reason = decide(mid)
        if frozen and reason in ("b_stop", "catastrophe"):
            hits = int(pos.get("stop_hits") or 0) + 1
            m.set_position_stop_hits(conn, pos["id"], hits)
            needed = int(paper_policy_value(
                pos, "stop_debounce_sweeps", S.MEMECOIN_REAL_STOP_CONFIRM_SWEEPS))
            if hits < needed:
                reason = None
        elif frozen and pos.get("stop_hits"):
            m.set_position_stop_hits(conn, pos["id"], 0)
        if reason == "derisk":
            r = _shadow_derisk(conn, pos, mid, liq, now_iso)   # partial: recover entry, ride rest
            if r:
                closed.append(r)
        elif reason:
            r = _close(conn, pos, mid, liq, now_iso, reason)
            if r:
                closed.append(r)

    # THE PROMOTION STUDY'S ARMS. Added 2026-08-29 after they were found opening and never
    # closing: 53 + 53 open, average age 97 minutes, zero closed all day, against funded rails
    # whose max hold is 780 SECONDS. The arms were wired into the OPEN path (resolve_paper_arm_policy)
    # and not into this one, which dispatches by an explicit per-strategy list — so the sweep never
    # looked at them and the only thing that could ever have closed them was the 14-DAY backstop
    # below. A study whose positions cannot close cannot produce a verdict, and the book grows
    # without bound.
    #
    # ALWAYS THE FROZEN RAILS, never the live funded decision. `funded_clone` tracks funded by
    # RESOLVING at entry and freezing the result; re-resolving here would let a mid-study funded
    # change rewrite the baseline's rails underneath the comparison, which is the drift the
    # contrast exists to exclude. `profile_matched_v1`'s take-profit is fitted per wallet and
    # exists only in its frozen assignment, so there is no live equivalent to read.
    # THE BACKLOG BOUNDARY. Positions opened before the cutoff were never evaluated by any sweep,
    # so they close under their own label rather than under a rail that did not decide them. Empty
    # cutoff (the default) means no boundary and every position takes its rails, so this is inert
    # everywhere except the one deploy it exists for.
    study_arm_preflight(conn, now_iso)
    _sweep_cutoff = str(getattr(S, "MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE", "") or "")
    from sniper.services.memecoin_paper_ticks import V1, exit_engine
    for arm in STUDY_ARMS:
        for pos in m.open_positions_by_strategy(conn, arm):
            if exit_engine(pos) == V1:
                # ON-CHAIN TICKS (spec 2026-10-08, Gamma 1): the tick engine owns EVERY rail of a v1
                # position, time rails included, so the data can say which loop decided. Only the
                # arm-agnostic backstop below still sees it.
                continue
            if _sweep_cutoff and str(pos.get("opened_at") or "") < _sweep_cutoff:
                r = _sweep_closure(conn, pos, now_iso)
                if r:
                    closed.append(r)
                continue
            mid, liq = _mid_liq(pos)
            if not mid or mid <= 0 or not pos["entry_price"]:
                # No price: only the TIME rails can fire, same convention as real_shadow.
                reason = _frozen_real_shadow_reason(pos, None, now_iso, conn=conn)
                if reason:
                    r = _timecap_no_quote(pos, 0.0, reason)
                    if r:
                        closed.append(r)
                continue
            m.update_position_last_quote(conn, pos["id"], mid, now_iso)
            _track_peak(conn, pos, mid, now_iso)
            reason = _frozen_real_shadow_reason(pos, mid, now_iso, conn=conn)
            if reason in ("b_stop", "catastrophe"):
                # Same bad-tick debounce the frozen shadow applies, from the arm's OWN frozen
                # sweep count — a single glitched tick must not close a study position either.
                hits = int(pos.get("stop_hits") or 0) + 1
                m.set_position_stop_hits(conn, pos["id"], hits)
                needed = int(paper_policy_value(
                    pos, "stop_debounce_sweeps", S.MEMECOIN_REAL_STOP_CONFIRM_SWEEPS))
                if hits < needed:
                    reason = None
            elif pos.get("stop_hits"):
                m.set_position_stop_hits(conn, pos["id"], 0)
            if reason == "derisk":
                r = _shadow_derisk(conn, pos, mid, liq, now_iso)
                if r:
                    closed.append(r)
            elif reason:
                r = _close(conn, pos, mid, liq, now_iso, reason)
                if r:
                    closed.append(r)

    # UNIFORM MAX-HOLD BACKSTOP (2026-07-21): every arm above has rails, but nothing bounded a
    # position an arm's policy never closes — control has no timecap by design, so a coin pinned
    # between SL and TP rode open for 13 days. One arm-agnostic cap, applied identically across
    # arms so cross-arm comparisons stay fair: quotable -> close at this sweep's mid; dark -> the
    # same last-mid/entry convention as _timecap_no_quote. PAPER ONLY — real positions belong to
    # real_exit_sweep. Default 14d; 0 disables.
    cap_s_fallback = (getattr(S, "MEMECOIN_PAPER_MAX_HOLD_D", 0.0) or 0.0) * 86400.0
    for pos in m.list_open_positions(conn):
        if str(pos.get("execution_mode") or "paper") == "real":
            continue
        cap_s = paper_policy_value(pos, "paper_max_hold_s", cap_s_fallback)
        if cap_s <= 0:
            continue
        if not pos.get("entry_price") or _held_s(pos["opened_at"], now_iso) <= cap_s:
            continue
        mid, liq = _mid_liq(pos)
        if (not mid or mid <= 0) and _v1_engine(pos) and (pos.get("last_quote_price") or 0) > 0:
            # A v1 POSITION IS PRICED BY ITS OWN TICK (paper step 2): the sweep's map no longer
            # carries its coin, and its last_quote_price is the engine's tick, not a provider mid.
            # The close is the exact exit quote either way; the tick is its plausibility reference
            # while fresh, and only a label once it is older than the engine's own tick age.
            fresh = _age_s(pos.get("last_quote_ts"), now_iso) <= _v1_tick_written_within_s(pos, S)
            r = _close(conn, pos, pos["last_quote_price"], None, now_iso,
                       "max_hold_cap" if fresh else "max_hold_cap_stale",
                       source="paper_tick_last" if fresh else "paper_tick_stale",
                       check_plausible=fresh)
        else:
            r = (_close(conn, pos, mid, liq, now_iso, "max_hold_cap")
                 if (mid and mid > 0)
                 else _timecap_no_quote(pos, cap_s, "max_hold_cap"))
        if r:
            closed.append(r)
    return closed


def _combo_partial(conn, pos, mid, liq, now_iso, fraction):
    """One-shot FIXED-FRACTION partial for the combo_f arm: sell `fraction` of the open quantity
    at the modeled fill, keep the rest OPEN, mark derisked_at so it never fires twice and so the
    remainder trail arms (combo_f_exit_reason keys on `derisked`). Differs from _shadow_derisk
    only in sizing: fixed fraction, not entry-notional recovery."""
    from sniper.execution.solana_quote import modeled_fill
    if pos.get("derisked_at"):
        return None
    qty = m.open_qty(conn, pos["id"])
    if qty <= 0 or not (0.0 < fraction < 1.0):
        return None
    sell_qty = qty * fraction
    price = modeled_fill(mid, mid * sell_qty, liq, "sell")
    if not m.book_partial_and_stamp(conn, position_id=pos["id"], at=now_iso, price=price,
                                    qty=sell_qty, notional=price * sell_qty, fraction=fraction):
        return None                                  # one-shot already fired (atomic claim lost)
    return {"position_id": pos["id"], "strategy": pos["strategy"], "exit_reason": "f_partial"}


def _shadow_derisk(conn, pos, mid, liq, now_iso):
    """One-shot de-risk for the real_shadow arm: sell the fraction that recovers the entry
    notional (fraction = entry_notional / current value, ~0.5 at +100%), keep the rest OPEN, mark
    derisked_at so it never fires twice. Mirrors memecoin_real_runner._run_derisk in paper. The
    remainder later exits via mirror/stop/timecap; realized then sums the derisk + final legs."""
    from sniper.execution.solana_quote import modeled_fill
    if pos.get("derisked_at"):
        return None
    qty = m.open_qty(conn, pos["id"])
    value = mid * qty
    entry_notional = pos.get("entry_notional") or 0.0
    if qty <= 0 or value <= 0 or entry_notional <= 0:
        return None
    fraction = entry_notional / value
    if fraction >= 0.9 or fraction <= 0.0:
        return None   # mark fell back near/below entry between the rail check and now
    sell_qty = qty * fraction
    price = modeled_fill(mid, mid * sell_qty, liq, "sell")
    if not m.book_partial_and_stamp(conn, position_id=pos["id"], at=now_iso, price=price,
                                    qty=sell_qty, notional=price * sell_qty, fraction=fraction):
        return None                                  # one-shot already fired (atomic claim lost)
    return {"position_id": pos["id"], "strategy": pos["strategy"], "exit_reason": "derisk_partial"}


def paper_unquotable_cleanup(conn, now_iso, max_age_s=None):
    """Close open PAPER positions whose last successful quote (or open, if never quoted) is older
    than `max_age_s` (default MEMECOIN_PAPER_UNQUOTABLE_S, 6h) — the paper analog of the real
    leg's unquotable zombie rail (2026-07-11: operator kept seeing dead no-data rows pinned to
    the board; without a quote no bracket can ever fire, so they sat forever). Closes at the
    last known price (real-ish realized) or flat at entry when never quoted (realized ~0, never
    fabricated). exit_reason='unquotable' so studies can filter. Returns [(position_id, realized)]."""
    from sniper.settings.memecoin import MEMECOIN_PAPER_UNQUOTABLE_S
    fallback_age_s = float(MEMECOIN_PAPER_UNQUOTABLE_S)
    out = []
    for pos in m.list_open_positions(conn, mode="paper"):
        if (pos.get("experiment_status") in ("eligible", "excluded_fill")
                and not paper_policy_is_valid(pos)):
            m.set_experiment_status(conn, pos["id"], "invalid_policy")
            pos["experiment_status"] = "invalid_policy"
        age_limit = (float(max_age_s) if max_age_s is not None else float(
            paper_policy_value(pos, "paper_unquotable_s", fallback_age_s)
        ))
        if age_limit <= 0:
            continue
        ref = pos.get("last_quote_ts") or pos.get("opened_at")
        if _held_s(ref, now_iso) <= age_limit:
            continue
        if not m.claim_close(conn, pos["id"]):
            continue
        qty = m.open_qty(conn, pos["id"])
        price = pos.get("last_quote_price") or pos.get("entry_price") or 0.0
        m.insert_fill(conn, position_id=pos["id"], kind="manual_close", price=price, qty=qty,
                      notional=price * qty, fill_source="cleanup", fraction=1.0,
                      created_at=now_iso,
                      # `cleanup` covers BOTH a genuine stale quote and an entry-price fallback,
                      # so the source alone cannot tell them apart — mark only the fallback.
                      price_attribution=(None if pos.get("last_quote_price")
                                         else m.PLACEHOLDER_PRICE_ATTRIBUTION))
        realized = _realized_from_exits(conn, pos["id"], pos["entry_price"])
        m.finalize_close(conn, pos["id"], now_iso, realized, exit_reason="unquotable")
        out.append((pos["id"], realized))
    return out


def tape_mid_for_position(tc, pos, *, quote_usd, now_ts):
    """USD mid for a 4663 position from the recorded tick tape, or (None, None).

    4663 ONLY: the tape covers that chain unfiltered, and it exists precisely because momentum_a's
    15-min hold expired before the first DexScreener quote ever landed there ($0.00 over 138 closes).
    Fail-closed on a missing pool, missing pool_meta, unresolved decimals, an empty block path, or a
    tape older than MEMECOIN_TAPE_PRICE_MAX_AGE_S. Returns (price, "tape") so the caller can
    attribute the close to its price source.
    """
    from sniper.analysis.tape_price import tape_price_usd
    from sniper.persistence import db_ticks as _t
    from sniper.settings import memecoin as _cfg

    if str(pos.get("network_id") or "") != "4663":
        return (None, None)
    mint = pos.get("mint")
    if not mint:
        return (None, None)
    mint = str(mint).lower()
    try:
        # Resolve the pool from the MINT, matching the BASE side only (spec §4.3b).
        # NOT quote_route_target — that is the router/settler we route through, not the pool, and
        # has zero ticks by construction. NOT token0-OR-token1 either — WETH (the 4663 quote asset)
        # matches 168 pools that way. More than one usable pool => ambiguous => fail closed.
        metas = tc.execute(
            "SELECT pool, quote_side, token0_decimals, token1_decimals FROM pool_meta "
            "WHERE chain_id='4663' AND quote_side IS NOT NULL "
            "AND token0_decimals IS NOT NULL AND token1_decimals IS NOT NULL "
            "AND ((quote_side=0 AND LOWER(token1)=?) OR (quote_side=1 AND LOWER(token0)=?))",
            (mint, mint)).fetchall()
        if len(metas) > 1:
            # Ambiguous by mint alone. Narrow to the protocol this position's own fill used
            # before refusing. Shared with the FUNDED resolver rather than duplicated — this is
            # the paper twin of memecoin_exit_materializer.resolve_pool_for_position.
            from sniper.services.memecoin_exit_materializer import pools_on_route_protocol
            keep = {str(p).lower() for p in pools_on_route_protocol(
                tc, "4663", [str(mm["pool"]).lower() for mm in metas],
                pos.get("quote_route_target"))}
            metas = [mm for mm in metas if str(mm["pool"]).lower() in keep]
        if len(metas) != 1:
            return (None, None)          # 0 = not resolvable yet; >1 = ambiguous -> fail closed
        meta = metas[0]
        pool = str(meta["pool"]).lower()
        qs = int(meta["quote_side"])
        quote_dec = meta["token0_decimals"] if qs == 0 else meta["token1_decimals"]
        base_dec = meta["token1_decimals"] if qs == 0 else meta["token0_decimals"]
        rows = tc.execute(
            "SELECT kind, block_no, amount0, amount1 FROM pool_ticks "
            "WHERE chain_id='4663' AND pool=? AND kind IN ('v2_swap','v3_swap') "
            "AND block_no IS NOT NULL ORDER BY block_no DESC LIMIT 200", (pool,)).fetchall()
        swaps = [dict(r) for r in rows]
        if not swaps:
            return (None, None)
        bt = _t.block_ts_map(tc, "4663", {s["block_no"] for s in swaps})
        timed = []
        for s in swaps:
            _dt = _parse(bt.get(s["block_no"]))
            ep = _dt.timestamp() if _dt else None
            if ep is None:
                continue
            timed.append({**s, "block_ts": ep})
        if not timed:
            return (None, None)
        newest = max(s["block_ts"] for s in timed)
        max_age = float(getattr(_cfg, "MEMECOIN_TAPE_PRICE_MAX_AGE_S", 90.0))
        age = float(now_ts) - float(newest)
        if age < 0 or age > max_age:
            return (None, None)          # stale, or event time ahead of the clock -> fail closed
        px = tape_price_usd(timed, quote_side=qs, base_decimals=base_dec,
                            quote_decimals=quote_dec, quote_usd=quote_usd)
        return (px, "tape") if px else (None, None)
    except Exception:
        logger.exception("tape mid failed pos=%s", pos.get("id"))
        return (None, None)
