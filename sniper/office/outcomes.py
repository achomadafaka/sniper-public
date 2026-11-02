"""Office outcome table (operator order 2026-10-30, Head's top gap): per feed coin, causal outcomes at fixed horizons from the sheet time.
Pure rules plus the append-only store. Seats read the store through db_read; nothing here touches a funded path, a provider or a network.

Causal means a row for horizon h uses only prints at or before sheet_at+h, so the row computed on a tape cut at sheet_at+h equals the row computed on the full tape.
A horizon with no row is immature (a left join reads NULL); a row is written once it has matured and is never updated. Unknown stays typed:
no price reference, no prints in the window, a tape that ends early, or a capture gap each get their own status and NULL numbers, never 0."""
from bisect import bisect_left, bisect_right
import math
import re
import sqlite3

HORIZONS = (60, 300, 900, 1800, 3600)
DEFAULT_DECODER = 'ps-decode-v1'
SETTLE_S = 60.          # late prints for the horizon's last seconds land before it is read
REF_MAX_AGE_S = 120.    # p0 is the last print at or before the sheet, at most this old
COLLAPSE_FRAC = 0.5     # collapsed = a print at or below half of p0 inside the window
VERSION = 'office-outcomes-v6'
QUIET_S = 60.           # an ok row's last print is at most this old at the horizon; older means the coin went quiet, never a held price
STATUSES = ('ok', 'ok_bracketed', 'ok_unchecked', 'no_ref_price', 'no_tape', 'tape_ended_capture', 'tape_ended_quiet', 'censored_gap', 'tape_unreadable')
GAP_CHECKS = ('checked', 'unavailable')
# R6.1 (Gamma, Codex-corrected, written before any return was read). BoostBuyAndBurn is a plain buy the tape missed (one real tx checked, fixture pumpswap_boost_20261030); InitBoost is on the list BY RULE, its buy
# semantics are NOT proven (a migration CPI event). A window whose only losses are such events with a stored
# print before and after is 'ok_bracketed': a stratum APART from 'ok'. BRACKET_SCREEN is a screen on observed endpoint displacement, never a bound on what happened inside the gap.
BRACKET_KINDS = frozenset({'3f451c16305cc2b9', 'ae7c4af90451f611'})
BRACKET_SCREEN = 0.10
_NUM = (int, float)


def _ok(x): return type(x) in _NUM and math.isfinite(x)


def clean(prints):
    """(time, price) pairs with a finite time and a finite positive price, oldest first. An unpriced print is dropped, never read as zero."""
    return sorted((t, p) for t, p in prints if _ok(t) and _ok(p) and p > 0)


def decoder_version(versions, lo, hi):
    """The tape decoder version(s) the window [lo, hi] was recorded under. versions: [(effective_from, name)] oldest first. One name, or 'mixed:a>b' when a boundary falls inside the window."""
    def at(t):
        cur = None
        for eff, name in versions:
            if eff <= t: cur = name
        return cur
    a, b = at(lo), at(hi)
    if a is None or b is None: raise ValueError('outcome_decoder_unknown')
    return a if a == b else 'mixed:%s>%s' % (a, b)


def bracket_losses(rows, losses):
    """-> {'times': receipt times of the losses that are bracketed, 'n', 'disp_max'}. rows: the pool's tape rows from a with_gaps read (dicts with slot, received_at, signature, price_sol, price_only), ordered
    as the reader returns them. losses: EVERY complete-scope loss of the pool near the window as (received_at, slot, discriminator), whatever its cause. A loss is bracketed when it is a boost or init-boost
    loss, a stored priced print exists with slot < loss slot (prev) and another with slot > loss slot (next), no OTHER loss has a slot strictly between them, and the next print's PRE-state price row (the
    reader's price_only ':pre' row) exists. Displacement = |next_pre / prev_price - 1|; above BRACKET_SCREEN the loss is NOT bracketed (it censors). It bounds nothing inside the gap. Anything missing: not bracketed."""
    real = [r for r in rows if not r.get('price_only') and _ok(r.get('price_sol')) and r['price_sol'] > 0 and type(r.get('slot')) is int]
    pre = {(r['signature'], r['ordinal']): r for r in rows if r.get('price_only') and _ok(r.get('price_sol')) and r['price_sol'] > 0}      # the reader prints ':pre' at ordinal - 0.5 of the print after the loss
    slots = [r['slot'] for r in real]
    times, disp, blocked = set(), [], {}       # blocked: loss time -> why it was not bracketed (diagnostic only, never stored in a row)
    for idx, (t, slot, disc) in enumerate(losses):
        if disc not in BRACKET_KINDS: blocked[t] = 'kind:' + str(disc); continue
        if type(slot) is not int: blocked[t] = 'no_slot'; continue
        i = bisect_left(slots, slot)                  # first real print with slot >= loss slot
        j = bisect_right(slots, slot)                 # first real print with slot > loss slot
        if i == 0 or j >= len(real): blocked[t] = 'no_prev_print' if i == 0 else 'no_next_print'; continue
        prev, nxt = real[i - 1], real[j]
        if any(k != idx and (prev['slot'] < os_ < nxt['slot'] if type(os_) is int else prev['received_at'] <= ot <= nxt['received_at'])      # another loss, even one with no slot, in the gap
               for k, (ot, os_, od) in enumerate(losses)): blocked[t] = 'other_loss_in_gap'; continue
        row_pre = pre.get((nxt['signature'] + ':pre', nxt['ordinal'] - 0.5))
        if row_pre is None: blocked[t] = 'no_pre_row'; continue
        d = abs(row_pre['price_sol'] / prev['price_sol'] - 1)
        if d > BRACKET_SCREEN: blocked[t] = 'screen'; continue
        times.add(t); disp.append(d)
    return dict(times=times, n=len(disp), disp_max=max(disp) if disp else None, blocked=blocked)


def horizon_row(prints, t0, h, *, now, tape_end, gaps=(), gap_check='unavailable', unreadable=False, decoder=DEFAULT_DECODER, loss=None, gap_spans=(), bracketed=None, capture_points=()):
    """None while the horizon is immature. Else a dict of the table's numeric columns plus status.
    prints: clean() output. tape_end: the last time the coin's tape is known to cover (None = unknown). gaps: capture-gap and pool-attributed loss times; only used when gap_check is 'checked'.
    gap_spans: capture-stop intervals (start, end); any that overlaps (t0, t0+h] censors the window, including one that began before it and ends after it.
    loss: (complete-scope losses naming this pool, unknown-scope losses) counted in (t0, t0+h], or None when the window is outside the loss tables' retention. A return computed where no loss check was possible is
    'ok_unchecked', never 'ok'. bracketed: bracket_losses() output; its loss times do not censor, and a window kept only because of them is 'ok_bracketed'."""
    if not (_ok(t0) and _ok(now) and h in HORIZONS) or gap_check not in GAP_CHECKS: raise ValueError('outcome_input_invalid')
    end = t0 + h
    if now < end + SETTLE_S: return None
    times = [t for t, _ in prints]
    row = dict(horizon_s=h, matured_at=end + SETTLE_S, status=None, p0=None, p0_age_s=None, p_end=None, last_print_age_s=None, n_prints=0,
               ret=None, peak_ret=None, trough_ret=None, max_drawdown=None, collapsed=None, collapse_s=None, gap_check=gap_check,
               tape_decoder_version=decoder, loss_complete_n=None, loss_unknown_n=None, loss_share_complete=None, loss_share_all=None, bracket_n=None, bracket_endpoint_displacement_max=None)
    free = (set(bracketed['times']) if bracketed else set()) - set(capture_points)      # a capture discontinuity is never forgiven, even at a bracketed loss's receipt time
    i = bisect_right(times, t0)
    ref = prints[i - 1] if i and t0 - times[i - 1] <= REF_MAX_AGE_S else None
    window = prints[i:bisect_right(times, end)]
    row['n_prints'] = len(window)
    if window: row['last_print_age_s'] = end - window[-1][0]
    if loss is not None and gap_check == 'checked':
        cn, un = loss
        row.update(loss_complete_n=cn, loss_unknown_n=un, loss_share_complete=cn / (len(window) + cn) if len(window) + cn else None,
                   loss_share_all=(cn + un) / (len(window) + cn + un) if len(window) + cn + un else None)
    if ref is not None: row.update(p0=ref[1], p0_age_s=t0 - ref[0])
    if unreadable: row.update(status='tape_unreadable', p0=None, p0_age_s=None, n_prints=0)     # the tape could not be read: nothing about the coin is known
    elif ref is None: row['status'] = 'no_ref_price'
    elif not _ok(tape_end) or tape_end < end: row['status'] = 'tape_ended_capture'      # stored data stops before the horizon: capture, selection or retention, not the coin
    elif gap_check == 'checked' and (any(t0 < g <= end and g not in free for g in gaps) or any(b > t0 and a <= end for a, b in gap_spans)): row['status'] = 'censored_gap'
    elif not window: row['status'] = 'no_tape'
    elif end - window[-1][0] > QUIET_S: row['status'] = 'tape_ended_quiet'     # covered, but the last print is old: the coin went quiet; no exit price is known, never scored
    else:
        p0, peak, worst, run = ref[1], ref[1], ref[1], ref[1]
        drawdown = 0.
        first_collapse = None
        for t, p in window:
            peak, worst = max(peak, p), min(worst, p)
            run = max(run, p)
            drawdown = min(drawdown, p / run - 1)
            if first_collapse is None and p <= COLLAPSE_FRAC * p0: first_collapse = t - t0
        used = [g for g in gaps if t0 < g <= end and g in free] if gap_check == 'checked' else []
        row.update(status='ok_bracketed' if used else 'ok' if gap_check == 'checked' else 'ok_unchecked', p_end=window[-1][1], last_print_age_s=end - window[-1][0], ret=window[-1][1] / p0 - 1, peak_ret=peak / p0 - 1,
                   trough_ret=worst / p0 - 1, max_drawdown=drawdown, collapsed=int(first_collapse is not None), collapse_s=first_collapse)
        if used:       # R6.1: ret is exact (two stored prints). Observed only: peak_ret is a lower bound on the true peak; trough_ret and max_drawdown are stored NEGATIVE, so their MAGNITUDES are lower bounds (numerically upper bounds). collapsed=0 is unknown
            row.update(bracket_n=len(used), bracket_endpoint_displacement_max=bracketed['disp_max'])
            if not row['collapsed']: row.update(collapsed=None, collapse_s=None)
    return row


def rows_for_coin(prints, t0, *, now, tape_end, gaps=(), gap_check='unavailable', unreadable=False, decoder=DEFAULT_DECODER, loss=None, gap_spans=(), bracketed=None, capture_points=()):
    return [r for r in (horizon_row(prints, t0, h, now=now, tape_end=tape_end, gaps=gaps, gap_check=gap_check, unreadable=unreadable, decoder=decoder, loss=loss, gap_spans=gap_spans, bracketed=bracketed, capture_points=capture_points) for h in HORIZONS) if r is not None]


# ---- store ---------------------------------------------------------------------------------------------------------------------------

def ensure_schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS outcome_coins(network_id TEXT NOT NULL,mint TEXT NOT NULL,sheet_at REAL NOT NULL,first_seen_at REAL NOT NULL,'
                 'creator TEXT,creator_source TEXT,price_source TEXT,PRIMARY KEY(network_id,mint))')
    conn.execute('CREATE TABLE IF NOT EXISTS outcome_horizons(network_id TEXT NOT NULL,mint TEXT NOT NULL,horizon_s INTEGER NOT NULL,matured_at REAL NOT NULL,'
                 'status TEXT NOT NULL,p0 REAL,p0_age_s REAL,p_end REAL,last_print_age_s REAL,n_prints INTEGER NOT NULL,ret REAL,peak_ret REAL,trough_ret REAL,'
                 'max_drawdown REAL,collapsed INTEGER,collapse_s REAL,gap_check TEXT NOT NULL,tape_decoder_version TEXT NOT NULL,loss_complete_n INTEGER,loss_unknown_n INTEGER,loss_share_complete REAL,loss_share_all REAL,bracket_n INTEGER,bracket_endpoint_displacement_max REAL,PRIMARY KEY(network_id,mint,horizon_s),'
                 "CHECK(status IN ('ok','ok_bracketed','ok_unchecked','no_ref_price','no_tape','tape_ended_capture','tape_ended_quiet','censored_gap','tape_unreadable')),CHECK(gap_check IN ('checked','unavailable')),"
                 "CHECK(status IN ('ok','ok_bracketed','ok_unchecked') OR (ret IS NULL AND peak_ret IS NULL AND trough_ret IS NULL AND max_drawdown IS NULL AND collapsed IS NULL)))")
    # Who made each coin, for ANY mint we have seen, not only the feed's. One row per (mint, creator, source): two sources that disagree both stay, and a mint with no row is unknown.
    conn.execute('CREATE TABLE IF NOT EXISTS mint_creators(network_id TEXT NOT NULL,mint TEXT NOT NULL,creator TEXT NOT NULL,source TEXT NOT NULL,source_at REAL NOT NULL,'
                 "PRIMARY KEY(network_id,mint,creator,source),CHECK(source IN ('launches','runner_signals','ps_live','ps_archive')))")
    conn.execute('CREATE INDEX IF NOT EXISTS mint_creators_creator ON mint_creators(creator)')
    # Forward only (Alpha 2026-10-31): a coin written before these columns stays NULL = not recorded, never restated.
    have = {r[1] for r in conn.execute('PRAGMA table_info(outcome_coins)')}
    for name, kind in (('mcap_usd', 'REAL'), ('liq_usd', 'REAL'), ('market_source', 'TEXT'), ('supply_known_at', 'REAL')):
        if name not in have: conn.execute('ALTER TABLE outcome_coins ADD COLUMN %s %s' % (name, kind))
    conn.execute('CREATE TABLE IF NOT EXISTS outcome_meta(id INTEGER PRIMARY KEY CHECK(id=1),asof REAL NOT NULL,version TEXT NOT NULL,rule TEXT NOT NULL)')
    for table in ('outcome_coins', 'outcome_horizons', 'mint_creators'):
        for verb in ('UPDATE', 'DELETE'):
            conn.execute("CREATE TRIGGER IF NOT EXISTS %s_no_%s BEFORE %s ON %s BEGIN SELECT RAISE(ABORT,'outcome_append_only'); END" % (table, verb.lower(), verb, table))


RULE = ('horizons %s s from sheet_at; p0 = last print at or before sheet_at within %d s; ret = last print <= sheet_at+h over p0 minus 1; collapsed = print <= %.1f x p0; '
        'ok_bracketed (R6.1) = a window whose only losses are bracketed BoostBuyAndBurn or InitBoost losses, which are exceptions BY RULE when bracketed (that a BoostBuyAndBurn loss was a plain buy is shown by one real transaction; for InitBoost buy semantics are NOT proven, it is on the list by rule): ret is exact, peak_ret is a lower bound on the true peak, trough_ret and max_drawdown are stored negative so their MAGNITUDES are lower bounds, collapsed=0 is unknown (NULL), rail contacts inside the gap are unknown; never merged with ok. '
        'a row exists only after sheet_at+h+%d s; no row = immature. ok needs the last print within %d s of the horizon end; collapsed counts only inside ok rows (and collapsed=1, never 0, inside ok_bracketed rows), so a coin that '
        'collapsed and then went quiet or lost capture is tape_ended_quiet / tape_ended_capture and is NOT counted collapsed. Coverage is the pool\'s own contiguous stored span (source-specific, no maximum over sources); code merged at ca9bc39b9. Descriptive market measurement, not skill or edge evidence.'
        % (list(HORIZONS), REF_MAX_AGE_S, COLLAPSE_FRAC, SETTLE_S, QUIET_S))


def write(conn, *, asof, coins, horizons):
    """coins: dicts for outcome_coins; horizons: (network_id, mint, row). Existing keys are kept (first write wins); asof only moves forward."""
    if not _ok(asof): raise ValueError('outcome_asof_invalid')
    with conn:
        for c in coins:
            conn.execute('INSERT INTO outcome_coins(network_id,mint,sheet_at,first_seen_at,creator,creator_source,price_source,mcap_usd,liq_usd,market_source,supply_known_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                         (c['network_id'], c['mint'], c['sheet_at'], asof, c.get('creator'), c.get('creator_source'), c.get('price_source'), c.get('mcap_usd'), c.get('liq_usd'), c.get('market_source'), c.get('supply_known_at')))
        for network_id, mint, r in horizons:
            conn.execute('INSERT INTO outcome_horizons VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                         (network_id, mint, r['horizon_s'], r['matured_at'], r['status'], r['p0'], r['p0_age_s'], r['p_end'], r['last_print_age_s'], r['n_prints'],
                          r['ret'], r['peak_ret'], r['trough_ret'], r['max_drawdown'], r['collapsed'], r['collapse_s'], r['gap_check'], r['tape_decoder_version'],
                          r['loss_complete_n'], r['loss_unknown_n'], r['loss_share_complete'], r['loss_share_all'], r['bracket_n'], r['bracket_endpoint_displacement_max']))
        conn.execute('INSERT INTO outcome_meta VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET asof=max(asof,excluded.asof),version=excluded.version,rule=excluded.rule',
                     (asof, VERSION, RULE))


def write_creators(conn, rows):
    """rows: (network_id, mint, creator, source, source_at). First write of a key wins, so source_at is the earliest sighting by that source."""
    with conn:
        for r in rows:
            conn.execute('INSERT INTO mint_creators VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING', r)
