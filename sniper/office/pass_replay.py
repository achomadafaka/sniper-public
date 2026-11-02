"""PASS replay (Head request 11318, Alpha 2026-10-30): every hard_filter PASS replayed on stored tape under frozen rails, per entry delay and rail variant.

DESCRIPTIVE AND MODELLED. In-sample (the PASS set and rail values were chosen after the crew had looked at these coins). Fills are modelled: entry is the last print at or
before the entry time, cost is a fixed fee plus the PASS ticket's own sell_probe round trip, size and landing are not modelled. Replay rails fire on the FULL tape the instant a level
is crossed, which overstates live rail quality (live marks can be blind); the mark-blind variant evaluates rails only on coarse ticks to show the gap. Variants are hypotheses, not
policy. No edge, skill or go-live claim follows from any figure here. Pure functions: no database, no tape."""
from bisect import bisect_right
import math

MAX_HOLD_S = 3600.0
REF_MAX_AGE_S = 120.0     # same reference rule as the outcome table: entry price is the last print at or before the entry time, at most this old
QUIET_S = 60.0            # a covered window whose last print is older than this at the end is a coin gone quiet, not an exit
FEE_FRAC = 0.19 / 25.0    # office ledger costs_usd / spend (0.76 percent) as a labelled constant; per order, in addition to the probe round trip
STATUSES = ('ok', 'ok_unchecked', 'no_print_at_t0', 'no_print_in_horizon', 'tape_ended_coin_quiet', 'tape_ended_capture_stopped', 'censored_pool_loss', 'tape_unreadable', 'immature')


def _ok(x):
    return type(x) in (int, float) and math.isfinite(x)


def rail(pnl_pct, peak_ratio, ratio, *, sl, tp, trail, cat):
    """The ledger's order exactly (sniper/office/ledger._rail): catastrophe, stop, tp, trail. A 0 disables a rail. peak_ratio is the running maximum of value/spend."""
    if cat > 0 and pnl_pct <= -cat:
        return 'catastrophe'
    if sl > 0 and pnl_pct <= -sl:
        return 'stop'
    if tp > 0 and pnl_pct >= tp:
        return 'tp'
    if trail > 0 and peak_ratio > 1.0 and ratio <= peak_ratio * (1 - trail / 100.0):
        return 'trail'
    return None


def simulate(prints, t_entry, rails, *, tape_end, gaps=(), blind_s=None, max_hold_s=MAX_HOLD_S, asof=None, gap_spans=()):
    """prints: [(time, price)] oldest first, finite positive prices only. rails: dict(sl, tp, trail, cat) in percent (0 = off).
    tape_end: last time the pool's stored tape is contiguous from the window start (None = unknown). gaps: pool-attributed loss times.
    blind_s: evaluate rails only at ticks t_entry + k*blind_s on the last print at or before the tick.
    asof: the run's knowledge time; nothing after it is read, a hold that has not elapsed by it and has no rail yet is 'immature' (not a capture stop).
    -> dict(status, entry_price, rail, exit_at, exit_price, gross_ret, peak_ret, trough_ret, last_print_age_s). Numbers are None unless status is ok."""
    out = dict(status=None, entry_price=None, rail=None, exit_at=None, exit_price=None, gross_ret=None, peak_ret=None, trough_ret=None, last_print_age_s=None)
    if not _ok(t_entry):
        raise ValueError('replay_input_invalid')
    if asof is not None:
        prints = [x for x in prints if x[0] <= asof]
        if t_entry > asof:
            out['status'] = 'immature'
            return out
    times = [t for t, _ in prints]
    i = bisect_right(times, t_entry)
    if not i or t_entry - times[i - 1] > REF_MAX_AGE_S:
        out['status'] = 'no_print_at_t0'
        return out
    p0 = prints[i - 1][1]
    end = t_entry + max_hold_s
    out['entry_price'] = p0
    window = prints[i:bisect_right(times, end)]
    if blind_s:
        ticks, k, last = [], 1, prints[i - 1]
        j = 0
        horizon = end if asof is None else min(end, asof)       # a tick after the run's knowledge time was never observed
        while t_entry + k * blind_s <= horizon:
            tt = t_entry + k * blind_s
            while j < len(window) and window[j][0] <= tt:
                last = window[j]
                j += 1
            ticks.append((tt, last[1], last[0]))
            k += 1
        marks = ticks
    else:
        marks = [(t, p, t) for t, p in window]
    peak = worst = 1.0
    fired = None
    for t, p, seen in marks:
        ratio = p / p0
        peak, worst = max(peak, ratio), min(worst, ratio)
        why = rail((ratio - 1.0) * 100.0, peak, ratio, sl=rails['sl'], tp=rails['tp'], trail=rails['trail'], cat=rails['cat'])
        if why:
            fired = (t, p, why, ratio, seen)
            break
    stop_at = fired[0] if fired else end
    if asof is not None:
        gaps = [g for g in gaps if g <= asof]
    if any(t_entry < g <= stop_at for g in gaps) or any(b > t_entry and a <= stop_at for a, b in gap_spans):
        out['status'] = 'censored_pool_loss'      # a pool-attributed loss inside the span: the rail may have fired earlier on data we do not have
        return out
    if fired:
        t, p, why, ratio, seen = fired
        if not _ok(tape_end) or tape_end < t:        # a rail is scored only where the stored span is proven through the exit itself
            out['status'] = 'tape_ended_capture_stopped'
            return out
        out.update(status='ok', rail=why, exit_at=t, exit_price=p, gross_ret=ratio - 1.0, peak_ret=peak - 1.0, trough_ret=worst - 1.0, last_print_age_s=t - seen)
        return out
    if asof is not None and asof < end:
        out['status'] = 'immature'                     # the hold has not elapsed at the run time and no rail has fired yet
        return out
    if not _ok(tape_end) or tape_end < end:
        out['status'] = 'tape_ended_capture_stopped'   # stored data ends before the hold ends and no rail fired inside it: the exit is unknown
        return out
    if not window:
        out['status'] = 'no_print_in_horizon'
        return out
    last_t = window[-1][0]
    if end - last_t > QUIET_S:
        out['status'] = 'tape_ended_coin_quiet'      # covered but silent at the end: cannot exit, price unknown, never scored
        return out
    ratio = window[-1][1] / p0
    out.update(status='ok', rail='max_hold', exit_at=end, exit_price=window[-1][1], gross_ret=ratio - 1.0, peak_ret=peak - 1.0, trough_ret=worst - 1.0, last_print_age_s=end - last_t)
    return out


def net_ret(gross_ret, probe_round_trip_loss_pct, fee_frac=FEE_FRAC):
    """ret_net_entry_cost = gross - sell_probe round trip (frozen at the hard filter, at probe size) - fixed fee. None when there is no gross or no probe: absent is not free."""
    if gross_ret is None or not _ok(probe_round_trip_loss_pct):
        return None
    return gross_ret - probe_round_trip_loss_pct / 100.0 - fee_frac
