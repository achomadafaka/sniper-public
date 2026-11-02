"""WO-5A — deterministic exit-controller reducer. PURE: no DB, no network, no clock.

The controller carries the exit-decision *hysteresis* the stateless proposer cannot: how many
distinct structure-break events have confirmed, when negative flow / route degradation began, the
consecutive route/infra failure streaks, and the executable peak (price + the time it was set). All
of it lives as columns on `memecoin_positions` (WO-5 part 1 + WO-5A), so it survives a runner restart
by construction — reload the row, keep reducing.

Determinism guarantees that make restart reconstruction byte-identical for BOTH the 30 s pull
materializer and the sub-second hot lane (there is no lane branch here):

- Break counts advance ONLY on a new chain-grounded event identity (chain, pool, tx sig, log index).
  Reprocessing the same event — a duplicate WS notification, or the same decoded row seen again on
  the next 30 s materialization — does not double-count. Vault-delta-only rows (tx_sig=None) are not
  chain-grounded (WO-2 invariant) and can never advance a break count.
- Route SUCCESS samples dedup by quote identity (the quote's own fetch time): the same cached 3 s
  prewarm quote observed twice is one sample, so degradation is measured across DISTINCT quotes.
- The executable peak's price and timestamp advance atomically — no-progress is measured from a real
  peak time, never from position age (age alone never DERISKs; see `executable_no_progress`).

Ships INERT: the columns default NULL/0 and nothing in the live decision path reads them until WO-5B
wires per-action sizing under the WO-7-registered persistence/no-progress windows.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime
from typing import Optional

# The reducer-writable controller columns, mirrored in db_memecoin.CONTROLLER_STATE_COLS.
# `controller_revision` is the CAS guard (managed by persist_controller_state_cas), never here.
WRITABLE_FIELDS = (
    "controller_last_event_id", "controller_last_event_block",
    "controller_last_event_log_index", "structure_break_since", "consecutive_break_events",
    "negative_flow_since", "route_degradation_since", "route_failure_streak",
    "infra_failure_streak", "executable_peak_price", "executable_peak_at",
    "route_quote_value", "route_quote_at",
)


@dataclass(frozen=True)
class ControllerState:
    """The persisted exit-decision hysteresis for one open position. Mirrors the memecoin_positions
    controller columns 1:1 so `from_row`/`to_fields` are trivial and reconstruction cannot drift."""
    controller_revision: int = 0
    controller_last_event_id: Optional[str] = None
    controller_last_event_block: Optional[int] = None
    controller_last_event_log_index: Optional[int] = None
    structure_break_since: Optional[str] = None
    consecutive_break_events: int = 0
    negative_flow_since: Optional[str] = None
    route_degradation_since: Optional[str] = None
    route_failure_streak: int = 0
    infra_failure_streak: int = 0
    executable_peak_price: Optional[float] = None
    executable_peak_at: Optional[str] = None
    route_quote_value: Optional[float] = None
    route_quote_at: Optional[str] = None

    def to_fields(self) -> dict:
        """The writable columns (everything except the CAS-managed revision) for persistence."""
        return {f: getattr(self, f) for f in WRITABLE_FIELDS}


@dataclass(frozen=True)
class Observation:
    """One tick's decoded evidence. `event_time` anchors chain-derived onsets; `now` anchors
    quote/RPC observations. Keeping them separate makes replay, pull, and hot reductions agree."""
    now: str
    event_time: Optional[str] = None
    event_id: Optional[str] = None        # chain-grounded id of a decoded structure-break event
    event_block: Optional[int] = None     # monotonic chain order for replay/restart dedup
    event_log_index: Optional[int] = None
    structure_break: bool = False         # the decoded event confirms a structure break
    structure_recovered: bool = False     # decoded evidence structure recovered -> reset hysteresis
    net_flow_negative: Optional[bool] = None   # tri-state; None = unknown -> hold onset
    exec_price: Optional[float] = None    # fresh whole-bag executable price this tick
    route_quote_value: Optional[float] = None  # whole-bag route quote value (a SUCCESS sample)
    route_quote_at: Optional[str] = None  # the quote's own fetch time = its identity (with value)
    route_degraded: Optional[bool] = None      # route health for this SUCCESS sample; None = hold
    route_failure: bool = False           # token/route-specific quote failure (no quote resolved)
    infra_failure: bool = False           # infra-class quote failure (separate streak)


def event_identity(chain_id, pool, tx_sig, log_index) -> Optional[str]:
    """Canonical chain-grounded event id. None when there is no tx signature: a vault-delta-only row
    is not transaction-grounded (WO-2) and may never advance a break count."""
    if tx_sig is None:
        return None
    return f"{chain_id}|{pool}|{tx_sig}|{log_index}"


def reduce(prev: ControllerState, obs: Observation, policy=None) -> ControllerState:
    """Fold one observation into the controller state. Pure and total — never raises, never reads a
    clock. `policy` is accepted for forward-compat (the registered windows live in the predicates
    below, not the reduction) and is currently unused."""
    changes: dict = {}

    # --- structure-break hysteresis, gated by a monotonic chain high-water ---
    # An id alone only dedups the immediately repeated row. It cannot reject A,B,A history replay.
    # Once ordered evidence is available, never accept an unordered or older event again.
    previous_order = None
    observed_order = None
    try:
        if (prev.controller_last_event_block is not None
                and prev.controller_last_event_log_index is not None):
            previous_order = (int(prev.controller_last_event_block),
                              int(prev.controller_last_event_log_index))
        if obs.event_block is not None and obs.event_log_index is not None:
            observed_order = (int(obs.event_block), int(obs.event_log_index))
    except (TypeError, ValueError):
        observed_order = None
    metadata_promotion = bool(
        obs.event_id is not None
        and previous_order is None
        and observed_order is not None
        and obs.event_id == prev.controller_last_event_id)
    event_is_new = bool(
        obs.event_id is not None
        and ((previous_order is None and observed_order is not None and not metadata_promotion)
             or (previous_order is None and observed_order is None
                 and obs.event_id != prev.controller_last_event_id)
             or (previous_order is not None and observed_order is not None
                 and (observed_order > previous_order
                      or (observed_order == previous_order
                          and obs.event_id != prev.controller_last_event_id
                          and prev.consecutive_break_events < 2)))))
    if event_is_new:
        changes["controller_last_event_id"] = obs.event_id
        if observed_order is not None:
            changes["controller_last_event_block"] = observed_order[0]
            changes["controller_last_event_log_index"] = observed_order[1]
    elif metadata_promotion:
        # The pull lane can add monotonic order to an event first seen by the hot lane. Persist the
        # stronger identity without treating the same chain event as a second confirmation.
        changes["controller_last_event_block"] = observed_order[0]
        changes["controller_last_event_log_index"] = observed_order[1]

    if obs.structure_recovered and event_is_new:
        changes.update(consecutive_break_events=0, structure_break_since=None)
    elif obs.structure_break and event_is_new:
        changes.update(
            # The registered structure confirmation threshold is two events. Saturate there: once
            # confirmed, an out-of-order A,B,A replay cannot manufacture a third confirmation.
            consecutive_break_events=min(2, prev.consecutive_break_events + 1),
            structure_break_since=prev.structure_break_since or obs.event_time or obs.now)

    # --- negative-flow onset (unknown holds, positive clears) ---
    if obs.net_flow_negative is True:
        changes["negative_flow_since"] = prev.negative_flow_since or obs.event_time or obs.now
    elif obs.net_flow_negative is False:
        changes["negative_flow_since"] = None

    # --- route observations ---
    # A SUCCESS sample (a resolved whole-bag quote) dedups by quote identity: same (value, at) as the
    # last recorded -> already sampled, ignore. A distinct quote records value+time, moves the
    # degradation onset per its health, and a good quote clears the failure streak.
    same_quote = (obs.route_quote_at is not None and obs.route_quote_at == prev.route_quote_at
                  and obs.route_quote_value == prev.route_quote_value)
    if obs.route_quote_at is not None and obs.route_quote_value is not None and not same_quote:
        changes.update(route_quote_value=obs.route_quote_value, route_quote_at=obs.route_quote_at,
                       route_failure_streak=0, infra_failure_streak=0)
        if obs.route_degraded is True:
            changes["route_degradation_since"] = (
                prev.route_degradation_since or obs.route_quote_at or obs.now)
        elif obs.route_degraded is False:
            changes["route_degradation_since"] = None
    if obs.route_failure:
        changes["route_failure_streak"] = changes.get(
            "route_failure_streak", prev.route_failure_streak) + 1
        changes["route_degradation_since"] = (
            changes.get("route_degradation_since", prev.route_degradation_since) or obs.now)
    if obs.infra_failure:
        changes["infra_failure_streak"] = prev.infra_failure_streak + 1

    # --- executable peak: price and time advance atomically, only on a strictly higher price ---
    if obs.exec_price is not None and (prev.executable_peak_price is None
                                       or float(obs.exec_price) > float(prev.executable_peak_price)):
        changes.update(executable_peak_price=float(obs.exec_price),
                       executable_peak_at=obs.route_quote_at or obs.now)

    changes = {name: value for name, value in changes.items() if getattr(prev, name) != value}
    if not changes:
        return prev
    changes["controller_revision"] = prev.controller_revision + 1
    return replace(prev, **changes)


def _epoch(iso) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _elapsed_s(since, now) -> Optional[float]:
    a, b = _epoch(since), _epoch(now)
    return (b - a) if (a is not None and b is not None) else None


def executable_no_progress(state: ControllerState, *, now, no_progress_window_s,
                           quote_fresh) -> bool:
    """True only when a REAL executable peak time exists, its registered no-progress window has
    elapsed, AND a whole-bag quote is fresh. Missing any of the three -> False. Age alone is never
    sufficient (an unregistered window -> False), so this predicate can never DERISK on age.
    Non-numeric/unparseable timestamps fail closed to False."""
    if no_progress_window_s is None or not quote_fresh or state.executable_peak_at is None:
        return False
    elapsed = _elapsed_s(state.executable_peak_at, now)
    return elapsed is not None and elapsed >= float(no_progress_window_s)


def structure_cut_confirmed(state: ControllerState, *, now, persistence_window_s=None,
                            min_break_events=2) -> bool:
    """Structure-CUT hysteresis: two unique decoded confirming events OR the registered persistence
    window elapsed since onset. `min_break_events` is a count (the v1/v2 prereg confirm=2), not a
    fitted threshold. Catastrophic and unsellable rules bypass this predicate — they stay immediate."""
    if min_break_events is not None and state.consecutive_break_events >= int(min_break_events):
        return True
    if persistence_window_s is not None and state.structure_break_since is not None:
        elapsed = _elapsed_s(state.structure_break_since, now)
        return elapsed is not None and elapsed >= float(persistence_window_s)
    return False


def controller_state_from_row(row) -> ControllerState:
    """Reconstruct the controller state from a memecoin_positions row (sqlite3.Row or dict). Absent
    columns fall back to the field default — a pre-migration row reconstructs to the inert state.
    Lane-agnostic by construction: the same row yields the same state on the pull and hot lanes."""
    get = (row.get if hasattr(row, "get") else (lambda k, d=None: row[k] if k in row.keys() else d))
    kw = {}
    for f in fields(ControllerState):
        v = get(f.name, None)
        if v is not None:
            kw[f.name] = v
    return ControllerState(**kw)
