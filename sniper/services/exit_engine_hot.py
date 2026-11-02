"""Incremental per-position exit engine (fastlane spec §2.2) — event-driven, O(1) per tick.

Today a decision happens once per 30 s worker pass and recomputes everything. Measured hardware is
32 cores / 30 GB and essentially idle, so compute was never the bottleneck: the latency lives in how
fast ticks arrive and how fast a decision follows one. This holds a small ring buffer per position
and calls the selected pure proposer on EVERY new tick, so the decision is as fresh as the tape.

One-code-path refusal preserved: the ring buffer feeds the SAME pure functions
(`sniper.analysis.exit_features`) that replay and the pull materializer use. Nothing is
reimplemented here — this class only decides WHEN to recompute and keeps the series bounded.

Pure and in-memory: no DB, no network, no rail call. It returns decisions; recording and execution
are someone else's job.
"""
from __future__ import annotations

import logging
from collections import deque

from sniper.analysis.exit_features import (
    arrival_acceleration, arrival_rate_window, confirmed_pivots, exec_price_path_per_block,
    in_window, largest_lp_removal_fraction, pool_flow, realized_vol,
    v3_largest_lp_removal_fraction,
)
from sniper.analysis.exit_flex_v5 import ARRIVAL_HORIZON_UNITS, TARGET_HORIZON_S, VOL_HORIZON_UNITS
from sniper.services.memecoin_exit_brain import ENGINE_VERSION_V5, propose_v5

logger = logging.getLogger("memecoin")

_PIVOT_CONFIRM = 2        # [inherited] v1/v2 prereg
_VOL_WINDOW_S = TARGET_HORIZON_S * VOL_HORIZON_UNITS            # realized_vol window (300s)
_ARRIVAL_WINDOW_S = TARGET_HORIZON_S * ARRIVAL_HORIZON_UNITS    # flow/arrival window (60s)
_DEPTH_WINDOW_S = _ARRIVAL_WINDOW_S                             # LP-severity window == the burn window


def required_history_s(structure_window_s):
    """WO-11 (rev 2) — the ring's retention as a COMPLETENESS CONTRACT, not a "largest window × margin"
    guess. Returns the seconds of tape the ring must retain, or None to DISABLE time-eviction.

    Eviction is disabled until a finite structure-evidence window is REGISTERED, because pivots
    (`prior_low`/`prior_high`) consume the ENTIRE retained path — bounding it silently changes
    structure-CUT behavior, so no eviction may land before the operator registers that window
    (degenerate-until-registered). When registered:

      REQUIRED_HISTORY_S = max(VOL, 2×ARRIVAL, DEPTH, REGISTERED_STRUCTURE)

    The 2× on arrival is STRUCTURAL, not a safety margin: arrival *acceleration* compares two adjacent
    windows (`rate_now` over `[t-A, t]` vs `rate_prev` over `[t-2A, t-A]`), so both must be retained.
    """
    try:
        sw = float(structure_window_s)
    except (TypeError, ValueError):
        return None
    if sw <= 0:
        return None                                            # unregistered / invalid -> disabled
    return max(_VOL_WINDOW_S, 2.0 * _ARRIVAL_WINDOW_S, _DEPTH_WINDOW_S, sw)


# Memory-safety backstop ONLY — NOT a correctness bound and NOT a silent truncator. Time-eviction
# (once the structure window is registered) is the correctness mechanism. If this ceiling is ever hit
# the ring must degrade to the pull lane (set `history_incomplete`, drop the oldest to bound memory),
# never to silently truncated evidence. Sized generously (the 300s vol window at a pathological ~30
# rows/s); its exact value only shifts WHEN a busy pool degrades to pull, never correctness.
# ponytail: fixed memory ceiling; overflow is fail-closed to pull, so the number is not load-bearing.
_MAX_EVENTS = int(_VOL_WINDOW_S * 30)


class HotPositionEngine:
    """One open position's live state. Feed it ticks; it proposes on each one.

    `contract` carries the position facts the policy needs (trail_pct, held_s, exact_quote_misses).
    `state_fn` builds a MarketState from the current series — injected so this class stays pure and
    testable, and so live and replay share one construction path.
    """

    def __init__(self, position_id, contract, *, quote_side, state_fn, max_events=_MAX_EVENTS,
                 decision_fn=propose_v5, engine_version=ENGINE_VERSION_V5, structure_window_s=None):
        self.position_id = position_id
        self.contract = dict(contract or {})
        self.quote_side = quote_side
        self._state_fn = state_fn
        self._decision_fn = decision_fn
        self.engine_version = engine_version
        # WO-11: retention is a completeness contract. `_required_history_s` is None until the operator
        # registers a structure window (then time-eviction activates). `_max_events` is a MEMORY
        # backstop only — NO deque(maxlen), so overflow is a visible, fail-closed event, never a silent
        # truncation. `_max_watermark` is the max observed event time; eviction anchors on it so a late
        # older (decoded) row cannot advance the cutoff. `history_incomplete` withdraws hot sale
        # authority until a pull-complete rebuild proves the ring again.
        self._required_history_s = required_history_s(structure_window_s)
        self._max_events = int(max_events)
        self._events = deque()
        self._max_watermark = None
        self.history_incomplete = False
        self.ticks_seen = 0
        self.last_state = None
        self.last_decision = None

    def _observe_watermark(self, event):
        ts = event.get("block_ts")
        if ts is None:
            return
        ts = float(ts)
        self._max_watermark = ts if self._max_watermark is None else max(self._max_watermark, ts)

    def add_tick(self, event):
        """Append one tape event (a pool_ticks-shaped dict with block_ts), advance the watermark, then
        time-evict + enforce the memory backstop. O(1) amortized (each event is popped at most once)."""
        if event is None or event.get("block_ts") is None:
            return False
        self._events.append(event)
        self.ticks_seen += 1
        self._observe_watermark(event)
        self._evict_stale()
        self._enforce_backstop()
        return True

    def _evict_stale(self):
        """WO-11: drop events older than REQUIRED_HISTORY relative to the MAX OBSERVED event-time
        watermark (not the incoming row's time — corroboration legally inserts older decoded rows, and
        those must not advance the cutoff). DISABLED entirely until a structure window is registered
        (`_required_history_s is None`) so pivots keep their full path until eviction is contracted.

        The ring is maintained in block_ts order (live feeds in order; corroborate re-sorts before it
        touches the ring), so evicting from the LEFT removes the oldest first. Never empties the ring."""
        if self._required_history_s is None or self._max_watermark is None:
            return
        cutoff = self._max_watermark - self._required_history_s
        while len(self._events) > 1:
            front_ts = self._events[0].get("block_ts")
            if front_ts is not None and float(front_ts) >= cutoff:
                break
            self._events.popleft()

    def _enforce_backstop(self):
        """WO-11 memory backstop. On overflow we still bound memory (drop the oldest), but we NEVER
        silently truncate evidence: a dropped row means the retained tape no longer proves the full
        window, so the engine loses sale authority (`history_incomplete`) until a pull-complete rebuild
        restores it. A burst that overruns memory degrades to the pull lane, never to hidden truncation."""
        if len(self._events) <= self._max_events:
            return
        self.history_incomplete = True
        while len(self._events) > self._max_events:
            self._events.popleft()

    def features(self, *, now_ts):
        """Recompute the derived series from the ring. Deliberately NOT cached across ticks: the
        pure functions are microseconds at this size, and a stale cache is a correctness risk on the
        money path. Returns the kwargs a MarketState builder needs."""
        events = list(self._events)
        swaps = [e for e in events if e.get("kind") in (
            "v2_swap", "v3_swap", "sol_swap", "sol_curve_swap")]
        latest_swap = max(
            swaps,
            key=lambda e: (float(e.get("block_ts") or 0.0), int(e.get("block_no") or -1),
                           int(e.get("log_index") or 0), str(e.get("tx_sig") or "")),
            default=None)
        from sniper.analysis.exit_controller import event_identity
        latest_event_id = (event_identity(
            self.contract.get("chain_id") or "solana", self.contract.get("pool"),
            latest_swap.get("tx_sig"), latest_swap.get("log_index"))
            if latest_swap is not None else None)
        latest_event_block_no = latest_event_log_index = None
        if latest_swap is not None:
            try:
                latest_event_block_no = int(latest_swap.get("block_no"))
                latest_event_log_index = int(latest_swap.get("log_index") or 0)
            except (TypeError, ValueError):
                latest_event_block_no = latest_event_log_index = None
        per_block = exec_price_path_per_block(swaps, quote_side=self.quote_side)
        path = [(ts, px) for ts, px, _n, _b in per_block]
        latest = max((e["block_ts"] for e in events), default=None)
        pivots = tuple(confirmed_pivots(path, confirm=_PIVOT_CONFIRM))
        lows = [p.price for p in pivots if p.kind == "low"]
        highs = [p.price for p in pivots if p.kind == "high"]
        # windowed, exactly as the pull materializer does — an old swap/burn must not describe the
        # CURRENT market (P1, Codex 2026-07-24; same defect, sibling caller)
        flow = pool_flow(in_window(swaps, window_s=_ARRIVAL_WINDOW_S, window_end_ts=latest),
                         quote_side=self.quote_side)
        rate_now = arrival_rate_window(swaps, quote_side=self.quote_side,
                                       window_s=_ARRIVAL_WINDOW_S, window_end_ts=latest)
        rate_prev = arrival_rate_window(swaps, quote_side=self.quote_side,
                                        window_s=_ARRIVAL_WINDOW_S,
                                        window_end_ts=(latest - _ARRIVAL_WINDOW_S) if latest else None)
        syncs = [e for e in events if e.get("kind") in ("v2_sync", "sol_sync")]
        v3_swaps = [e for e in events if e.get("kind") == "v3_swap"
                    and e.get("liquidity") is not None and e.get("tick") is not None]
        reserve_base = (syncs[-1].get(f"amount{1 - int(self.quote_side)}") if syncs else None)
        active_liquidity = v3_swaps[-1].get("liquidity") if v3_swaps else None
        severity_window = in_window(events, window_s=_ARRIVAL_WINDOW_S, window_end_ts=latest)
        burns_win = [e for e in severity_window if e.get("kind") in ("v2_burn", "sol_burn")]
        v3_burns_win = [e for e in severity_window if e.get("kind") == "v3_burn"]
        if v3_burns_win:
            liq_removed = v3_largest_lp_removal_fraction(severity_window)
            depth_pull = liq_removed is None or liq_removed > 0.0
        else:
            liq_removed = (largest_lp_removal_fraction(
                severity_window, quote_side=self.quote_side) if burns_win else None)
            depth_pull = True if burns_win else None
        depth_events = [*syncs, *v3_swaps, *v3_burns_win]
        return {
            "event_count": len(events),
            "structure_points": len(path),
            "nominal_price": path[-1][1] if path else None,
            "prior_low": lows[-1] if lows else None,
            "prior_high": highs[-1] if highs else None,
            "higher_low_state": (lows[-1] > lows[-2]) if len(lows) >= 2 else None,
            "lower_high_state": (highs[-1] < highs[-2]) if len(highs) >= 2 else None,
            "confirmed_pivots": pivots,
            "realized_vol": realized_vol(path, window_s=_VOL_WINDOW_S),
            "net_flow_quote": flow.net_flow_quote,
            "buy_n": flow.buy_n, "sell_n": flow.sell_n,
            "arrival_rate": rate_now,
            "arrival_accel": arrival_acceleration(rate_now, rate_prev),
            "depth_pull_flag": depth_pull,
            "reserve_base": reserve_base,
            "active_liquidity": active_liquidity,
            "liquidity_removed_fraction": liq_removed,
            "latest_event_ts": latest,
            "latest_event_id": latest_event_id,
            "latest_event_block_no": latest_event_block_no,
            "latest_event_log_index": latest_event_log_index,
            "latest_swap_ts": max((e["block_ts"] for e in swaps), default=None),
            "latest_depth_ts": max((e["block_ts"] for e in depth_events), default=None),
        }

    def _decide(self, *, now_ts, cfg):
        """Recompute features -> state -> decision from the current ring. Shared by on_tick and the
        WO-2 corroborate re-proposal so they decide over exactly one code path."""
        feats = self.features(now_ts=now_ts)
        state = self._state_fn(feats, now_ts=now_ts)
        if state is None:
            self.last_state = None
            return None
        self.last_state = state
        opened_ts = self.contract.get("opened_ts")
        if opened_ts is not None:
            self.contract["held_s"] = max(0.0, float(now_ts) - float(opened_ts))
        # Same pure controller reducer as the funded pull sweep. Provisional vault notifications
        # have no tx_sig -> latest_event_id None -> they can wake the lane but cannot advance a
        # structure CUT. No funded DB write occurs here.
        from sniper.analysis.exit_controller import (
            ControllerState, Observation, reduce)
        from sniper.services.memecoin_exit_market_state import _iso
        controller = self.contract.get("controller_state")
        if not isinstance(controller, ControllerState):
            controller = ControllerState()
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
        self.contract["controller_state"] = reduce(controller, Observation(
            now=_iso(now_ts), event_id=state.latest_event_id,
            event_time=state.latest_event_time or state.source_event_time,
            event_block=state.latest_event_block_no,
            event_log_index=state.latest_event_log_index,
            structure_break=structure_break, structure_recovered=structure_recovered,
            net_flow_negative=(None if net is None else net < 0)))
        self.contract["now_iso"] = _iso(now_ts)
        self.last_decision = self._decision_fn(self.contract, state, cfg)
        return self.last_decision

    def warm(self, *, now_ts, cfg=None):
        """Compute one history snapshot without replaying historical rows through the controller."""
        try:
            state = self._state_fn(self.features(now_ts=now_ts), now_ts=now_ts)
            self.last_state = state
            if state is None:
                self.last_decision = None
                return None
            opened_ts = self.contract.get("opened_ts")
            if opened_ts is not None:
                self.contract["held_s"] = max(0.0, float(now_ts) - float(opened_ts))
            from sniper.services.memecoin_exit_market_state import _iso
            self.contract["now_iso"] = _iso(now_ts)
            self.last_decision = self._decision_fn(self.contract, state, cfg)
            return self.last_decision
        except Exception:
            logger.exception("hot engine history warm failed pos=%s", self.position_id)
            return None

    def on_tick(self, event, *, now_ts, cfg=None):
        """Ingest one tick and decide. Returns the CandidateDecision, or None if the tick was
        unusable. Never raises — one bad tick must not stop the engine for a live position."""
        try:
            if not self.add_tick(event):
                return None
            return self._decide(now_ts=now_ts, cfg=cfg)
        except Exception:
            logger.exception("hot engine tick failed pos=%s", self.position_id)
            return None

    def on_ticks(self, events, *, now_ts, cfg=None):
        """Ingest one atomic event group, then decide once over the complete group."""
        try:
            added = False
            for event in events or ():
                added = self.add_tick(event) or added
            return self._decide(now_ts=now_ts, cfg=cfg) if added else None
        except Exception:
            logger.exception("hot engine event group failed pos=%s", self.position_id)
            return None

    def window_slots(self, *, window_s=_ARRIVAL_WINDOW_S):
        """WO-2: the PROVISIONAL (tx_sig=None) slots inside the current flow window, as
        {block_no: block_ts}. These are the slots whose evidence feeds net-flow sign and must ALL
        be decode-grounded before a structure CUT is authorized — replacing only the trigger slot
        would leave adjacent netted corruption in the sum AND double-count decoded slots."""
        latest = max((e["block_ts"] for e in self._events), default=None)
        if latest is None:
            return {}
        return {e["block_no"]: e["block_ts"] for e in self._events
                if e.get("tx_sig") is None and e.get("block_no") is not None
                and (float(latest) - float(e["block_ts"])) <= float(window_s)}

    def provisional_slots(self):
        """Every vault-delta slot still capable of influencing this engine's current features.

        Pivots consume the retained price path, not only the 60-second flow window, so any sale that
        reads structure/volatility/arrival must ground the complete provisional portion of the ring.
        Pull-warmed rows carry tx_sig and are excluded by construction.
        """
        return {e["block_no"]: e["block_ts"] for e in self._events
                if e.get("tx_sig") is None and e.get("block_no") is not None}

    def corroborate(self, slots, decoded_rows, *, now_ts, cfg=None):
        """WO-2: replace the PROVISIONAL (tx_sig=None) vault-delta rows for `slots` (an int or an
        iterable of slot numbers) with the decoded (tx_sig-set) rows, then re-decide. Keyed by
        (slot, tx_sig): a fabricated net-delta row (fee-claim netted into a fake burn) is dropped,
        so it cannot re-trigger depth_pull on later ticks — and every replaced slot is removed, so
        decoded rows are never double-counted beside their provisional originals. Returns the
        re-proposed CandidateDecision (or None). Never raises."""
        try:
            slot_set = {slots} if isinstance(slots, int) else set(slots or ())
            # Enumeration is complete for every supplied slot before this method is called. Replace
            # the whole slot, including a prior decoded copy, so repeated account notifications cannot
            # duplicate one transaction's flow/burn evidence.
            kept = [e for e in self._events if e.get("block_no") not in slot_set]
            decoded = [r for r in (decoded_rows or ()) if r.get("block_ts") is not None]
            decoded.sort(key=lambda e: (
                float(e.get("block_ts") or 0.0), int(e.get("block_no") or -1),
                int(e.get("log_index") or 0), str(e.get("tx_sig") or "")))
            decision = None
            for row in decoded:
                kept.append(row)
                self.ticks_seen += 1
                # WO-11: a decoded row carries its ORIGINAL (older-or-equal) slot time, so the max
                # watermark never advances here — eviction cannot be pushed forward by corroboration.
                self._observe_watermark(row)
                ordered = sorted(kept, key=lambda e: (
                    float(e.get("block_ts") or 0.0), int(e.get("block_no") or -1),
                    int(e.get("log_index") or 0), str(e.get("tx_sig") or "")))
                self._events.clear()
                self._events.extend(ordered)
                self._evict_stale()
                self._enforce_backstop()
                decision = self._decide(now_ts=now_ts, cfg=cfg)
            if not decoded:
                self._events.clear()
                self._events.extend(kept)
                self._evict_stale()
                self._enforce_backstop()
                decision = self._decide(now_ts=now_ts, cfg=cfg)
            return decision
        except Exception:
            logger.exception("hot engine corroborate failed pos=%s", self.position_id)
            return None


class HotEngineSet:
    """The open-position book. Routes a tick to the engine that holds that pool."""

    def __init__(self):
        self._by_key = {}

    def track(self, key, engine):
        self._by_key[key] = engine

    def drop(self, key):
        self._by_key.pop(key, None)

    def keys(self):
        return set(self._by_key)

    def on_tick(self, key, event, *, now_ts, cfg=None):
        eng = self._by_key.get(key)
        if eng is None:
            return None                    # a tick for a pool we do not hold -> ignored
        return eng.on_tick(event, now_ts=now_ts, cfg=cfg)

    def on_ticks(self, key, events, *, now_ts, cfg=None):
        eng = self._by_key.get(key)
        if eng is None:
            return None
        return eng.on_ticks(events, now_ts=now_ts, cfg=cfg)

    def engine(self, key):
        """The engine holding this pool, or None (WO-2: the supervisor asks it for window_slots)."""
        return self._by_key.get(key)

    def corroborate(self, key, slots, decoded_rows, *, now_ts, cfg=None):
        """WO-2: route a decode corroboration to the engine holding this pool (replace provisional
        rows for `slots` with decoded rows and re-decide)."""
        eng = self._by_key.get(key)
        if eng is None:
            return None
        return eng.corroborate(slots, decoded_rows, now_ts=now_ts, cfg=cfg)
