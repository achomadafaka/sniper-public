"""Task 15 hook (Lambda): the daily pods reallocation, called by the Office floor's housekeeping thread.
NOT on the fill path: it never runs inside run_turn or submit, and it takes the ledger write lock only inside L.transfer (one short transaction per booking). It is idempotent, so the caller may
tick every cycle: after the day's booking a tick is a SELECT and a lookup. The caller names the epochs: only unbroken EVALUATED epochs, the roster's decision, never inferred here. Time comes
from the caller's corrected clock; an unknown clock or FX books nothing. One epoch failing is logged and returned, never raised into the floor and never stops the next epoch."""
import logging
import math

from sniper.office import pod_floor as F

log = logging.getLogger("office.pod_tick")


def tick(conn, rule, *, epoch_ids, clock, fx):
    """-> {epoch_id: ['booked'|'replayed'|'none'|'error:<reason>']}, or {'skipped': 'clock_unknown'}. fx() -> (sol_usd, sol_at)."""
    try:
        now = clock()
    except Exception:                                 # a dependency failure never reaches the floor
        log.exception("pods tick clock failed")
        return {"skipped": "clock_unknown"}
    if now is None or isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        return {"skipped": "clock_unknown"}
    try:
        got = fx()
    except Exception:
        log.exception("pods tick fx failed")
        return {"skipped": "fx_unknown"}
    if not isinstance(got, (tuple, list)) or len(got) != 2:      # exactly (sol_usd, sol_at); (None, None) is a well-formed "unknown" and books nothing
        log.error("pods tick fx malformed: %r", got)
        return {"skipped": "fx_unknown"}
    sol_usd, sol_at = got
    out = {}
    for epoch_id in epoch_ids:
        try:
            done = F.reallocate(conn, rule, epoch_id=epoch_id, now=float(now), sol_usd=sol_usd, sol_at=sol_at)
            out[epoch_id] = [status for _id, status in done] or ["none"]
        except Exception as exc:                      # one epoch must not stop the others or reach the floor
            log.exception("pods tick failed epoch=%s", epoch_id)
            out[epoch_id] = ["error:%s" % exc]
    return out
