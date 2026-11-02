"""Anchor-checked EVM exit (spec docs/superpowers/specs/2026-10-01-memecoin-evm-anchor-checked-exit-design.md).

The EVM sell ladder takes the first venue that answers. A dislocated tick (HOMER 67676: the direct
pair silent for one tick while a connector path quoted the $100 bag at ~$0) or a far worse multihop
route can sell a bag for nothing. This module holds the two small state machines the rule needs;
the money decisions stay in the seams that call it.

1. The ANCHOR (sweep side): a sticky USD-per-token level, written by the exit sweep in the same
   UPDATE as last_executable_price. A preview >= X x anchor moves it; a low preview never does,
   unless the low level persists HOLD_S over >= 2 ticks, when it re-bases to the latest low tick.
2. The HOLD (executor side): a sell quote below X x anchor x qty is not signed (`held_anchor`,
   returned at once). The first quote >= X sells (`sold_recovered`). Once HOLD_S has passed since
   the first hold AND >= 2 attempts were held, the next attempt sells the largest answer
   (`sold_at_bound`, paged). The operator's "sell at any price" sells the largest answer at once
   (`sold_override`).

Both states are in-process (one runner owns every exit); a restart forgets them, which can only
lengthen one hold by up to HOLD_S. Off (MEMECOIN_EVM_EXIT_ANCHOR_CHAINS empty) none of this runs.
"""
from __future__ import annotations

import threading
import time

# A held episode whose last attempt is this old is over: the rail stopped asking to sell. Without
# it a stale episode would read as "bound reached" on an unrelated later exit and sell the largest
# answer with no hold. ponytail: fixed constant; a knob when a measured exit cadence needs one.
EPISODE_GAP_S = 60.0

HELD = "held"
PASS = "pass"
SOLD_RECOVERED, SOLD_AT_BOUND, SOLD_OVERRIDE = "sold_recovered", "sold_at_bound", "sold_override"
OVERRIDE, BOUND = "override", "bound"

_clock = time.monotonic
_lock = threading.Lock()
_low_runs = {}        # position id -> [first low preview tick, ticks]       (sweep)
_holds = {}           # position id -> {"first", "last", "n"}                 (executor)
_overrides = set()    # position ids the operator asked to sell at any price
_redrive = {}         # position id -> held exit reason the sweep must re-drive

# EVENT EXITS NO RAIL RE-NOMINATES. A rail (stop, TP, trail, de-risk) asks again on the next
# sweep tick while its condition holds, so a hold simply ends when it stops asking. The copy's
# mirror sell fires once, on the source's sell event: held, it would be dropped. The sweep
# re-drives it each tick until it sells (the executor's bound ends the hold).
REDRIVE_REASONS = frozenset({"mirror"})


def effective_anchor(position):
    """The stored anchor, else the entry fill's unit price (the seed), else None (rule off)."""
    for key in ("exit_anchor_price", "entry_price"):
        try:
            value = float((position or {}).get(key) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return None


def preview_anchor(position_id, anchor, unit, *, min_pct, hold_s, now=None):
    """The anchor this sweep tick writes, or None to leave the stored one.

    `anchor` is the effective anchor before the tick, `unit` the preview's USD per token."""
    now = _clock() if now is None else now
    pid = int(position_id)
    with _lock:
        if not anchor or anchor <= 0 or unit >= anchor * float(min_pct) / 100.0:
            _low_runs.pop(pid, None)
            return float(unit)
        run = _low_runs.setdefault(pid, [now, 0])
        run[1] += 1
        if now - run[0] >= float(hold_s) and run[1] >= 2:
            del _low_runs[pid]
            return float(unit)                     # a persistent low level is the new level
        return None


def _live_hold(pid, now):
    hold = _holds.get(pid)
    if hold is not None and now - hold["last"] > EPISODE_GAP_S:
        del _holds[pid]
        return None
    return hold


def request_override(position_id):
    with _lock:
        _overrides.add(int(position_id))


def clear_override(position_id):
    with _lock:
        _overrides.discard(int(position_id))


def forced(position_id, *, hold_s, now=None):
    """Before the quote: OVERRIDE or BOUND when this attempt must sell the largest answer."""
    now = _clock() if now is None else now
    pid = int(position_id)
    with _lock:
        if pid in _overrides:
            return OVERRIDE
        hold = _live_hold(pid, now)
        if hold is not None and now - hold["first"] >= float(hold_s) and hold["n"] >= 2:
            return BOUND
    return None


def redrive_reason(position_id, *, now=None):
    """The held event exit the sweep re-drives this tick, or None."""
    now = _clock() if now is None else now
    pid = int(position_id)
    with _lock:
        if pid in _redrive and _live_hold(pid, now) is None:
            del _redrive[pid]                      # the episode ended without a sale
        return _redrive.get(pid)


def decide(position_id, passes, *, hold_s, mode=None, reason=None, now=None):
    """After the quote: PASS / SOLD_* (sign it) or HELD (do not sign). Returns (decision, held_s).

    `mode` is what `forced` said BEFORE this attempt's quote. Only an attempt that was forced asked
    the ladder for the largest answer, so only it may sell below the anchor (Gamma MEDIUM 1 on
    e0faedf70): an attempt that crosses HOLD_S mid-quote is held, and the next one is forced.
    The override is not consumed here: it lives as long as the operator's intent (the drain sets
    it before each attempt and clears it after), so a failed sale does not need a second press."""
    now = _clock() if now is None else now
    pid = int(position_id)
    with _lock:
        hold = _live_hold(pid, now)
        held_s = (now - hold["first"]) if hold is not None else 0.0
        if mode == OVERRIDE:
            decision = SOLD_OVERRIDE
        elif passes:
            decision = PASS if hold is None else SOLD_RECOVERED
        elif mode == BOUND:
            decision = SOLD_AT_BOUND
        else:
            if hold is None:
                hold = _holds[pid] = {"first": now, "n": 0}
            hold["n"] += 1
            hold["last"] = now
            if reason in REDRIVE_REASONS:
                _redrive[pid] = reason
            return HELD, held_s
        _holds.pop(pid, None)                      # also ends a re-drive (redrive_reason)
        return decision, (held_s if decision != PASS else 0.0)


def reset():
    """Tests only."""
    with _lock:
        _low_runs.clear()
        _holds.clear()
        _overrides.clear()
        _redrive.clear()
