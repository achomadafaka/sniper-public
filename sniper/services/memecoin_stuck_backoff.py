"""Re-entry backoff for a bag whose sell ladder has already failed.

Measured on 4663 since 2026-08-30: 83 stuck attempts across only FIVE positions. Position 46888
alone ran 65 failing ladders over 31 minutes — 8.0s, 8.0s, 10.2s, 10.5s, 11.1s — fired every 10-16
seconds with no backoff, driven by three rails interleaved (real_tp, manual, b_timecap). That is
roughly nine minutes of continuous futile work on one unsellable position, each run walking every
slippage rung and quoting at each. It is invisible in a per-attempt mean because each attempt looks
like an ordinary 8-second exit.

A stuck attempt is slow BY CONSTRUCTION: it exhausts every rung, where a successful one stops at
the rung that fills. So the failing path costs strictly more than the working one, and repeating it
without memory is the whole defect. `mark_stuck_bag` already records the state; nothing consulted
it before re-entering.

AGE COMES FROM `memecoin_exit_execution_attempts`, not from a new column. It already stores every
attempt with its time and status, and `stuck_bag` is a bare 0/1 flag with no timestamp. No schema
change.

THE THREE CONSTRAINTS THAT MATTER, because getting them wrong turns a waste bug into a
stranded-money bug:

  - `manual` ALWAYS pierces. The operator is currently the exit mechanism on these bags — 4 of 7
    stuck positions were closed by hand, +$369.78 realized. Blocking the human rescuing a position
    would be far worse than the RPC it saves. Manual is not a rail.
  - FAIL OPEN. If the state cannot be read, ALLOW the exit. A backoff that fails closed strands a
    position. Same direction `_wallet_is_armed` takes on unknown input.
  - The wait has a CEILING and is age-based, so it decays rather than latching. An unsellable bag at
    10:00 may be sellable at 10:30.

`age` is measured from `attempted_at`, which is the START of an attempt, and a stuck ladder runs
8-11s. So the effective wait is roughly ten seconds shorter than configured. Harmless at 30s+, but
stated here so nobody re-derives it as a bug.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger("memecoin")

# Only the operator pierces. A safety rail re-driving an unsellable bag every 10 seconds is the
# same waste as any other rail, and unlike a human it is not waiting on a rescue.
_PIERCING_REASONS = frozenset({"manual"})
# `already_closing` is the 1s tick's shadow while a ladder holds the close claim — measured 0.2ms,
# and 3.4x MORE COMMON than `stuck` (279 vs 83 on 4663 since 08-30). It sits on top of the stuck
# rows in DESC order, so treating it as an outcome broke every streak at 0 and made this feature a
# no-op against the position that motivated it. It is evidence of nothing: skip it.
#
# EVERY OTHER non-stuck status BREAKS the streak, including ones added later. `closed` is a real
# fill, `already_closed` means the bag is gone, and `unknown`/`gone`/anything unrecognised mean we
# cannot tell what happened — and on the fail-open principle, what we cannot classify must never
# suppress an exit.
_SHADOW_STATUSES = frozenset({"already_closing"})
_SCAN_LIMIT = 40                    # bound the streak scan; 65 consecutive failures were observed


def _wait_seconds(streak, base, ceiling):
    """Exponential in the consecutive-failure count, capped. One failure must not buy the same
    silence as twenty, and twenty must not buy silence for ever."""
    if streak <= 0:
        return 0.0
    return min(float(ceiling), float(base) * (2.0 ** (streak - 1)))


def stuck_backoff(conn, position_id, chain_id, *, exit_reason, now_iso,
                  base_s=None, ceiling_s=None):
    """`(blocked, reason)` — True means SUPPRESS this re-entry.

    Returns `(False, None)` for a first attempt, for `manual`, and for any state we cannot read.
    """
    try:
        if str(exit_reason or "").lower() in _PIERCING_REASONS:
            return (False, None)
        from sniper.settings import memecoin as cfg
        base = base_s if base_s is not None else getattr(cfg, "MEMECOIN_STUCK_BACKOFF_BASE_S", 30.0)
        ceiling = (ceiling_s if ceiling_s is not None
                   else getattr(cfg, "MEMECOIN_STUCK_BACKOFF_CEILING_S", 300.0))
        rows = conn.execute(
            "SELECT status, attempted_at FROM memecoin_exit_execution_attempts "
            "WHERE position_id = ? AND network_id = ? ORDER BY attempted_at DESC LIMIT ?",
            (int(position_id), str(chain_id), _SCAN_LIMIT)).fetchall()
    except Exception:
        # Unreadable state -> ALLOW. Stranding a position is strictly worse than the waste.
        logger.exception("stuck backoff: state unreadable pos=%s — allowing exit", position_id)
        return (False, None)

    streak, last_at = 0, None
    for r in rows:
        status = str((r[0] if not hasattr(r, "keys") else r["status"]) or "")
        at = (r[1] if not hasattr(r, "keys") else r["attempted_at"])
        if status == "stuck":
            streak += 1
            if last_at is None:
                last_at = at
            continue
        if status in _SHADOW_STATUSES:
            continue                   # bookkeeping, not an outcome — neither counts nor breaks
        break                          # a real outcome, or one we cannot classify: streak ends
    if last_at is None:                # set only when a stuck row is seen, so this IS streak==0
        return (False, None)           # never stuck -> a first attempt is never delayed

    try:
        age = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
               - datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))).total_seconds()
    except Exception:
        logger.exception("stuck backoff: unparseable timestamp pos=%s — allowing exit", position_id)
        return (False, None)
    if age < 0:
        return (False, None)           # clock skew -> allow, never suppress on a bad clock

    wait = _wait_seconds(streak, base, ceiling)
    if age >= wait:
        return (False, None)
    reason = (f"stuck bag: {streak} consecutive failed ladders, last {age:.0f}s ago, "
              f"waiting {wait:.0f}s before re-entry")
    logger.info("stuck backoff SUPPRESSED pos=%s chain=%s reason=%s streak=%d age_s=%.0f wait_s=%.0f",
                position_id, chain_id, exit_reason, streak, age, wait)
    return (True, reason)
