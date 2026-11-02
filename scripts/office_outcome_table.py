#!/usr/bin/env python3
"""Office outcome table writer (read-only on every tape; its one write target is --out, under a flock). One pass: read the feed's coins
(desk_candidate_emissions, first emission = sheet_at), read each coin's prints from the PumpSwap tape (live + archive) and the curve tape,
and append the horizons that have matured since the last pass. Rows are written once and never changed (sniper/office/outcomes.py).

    python3 scripts/office_outcome_table.py --out data/office/outcomes.db [--limit N] [--dry]

Retention: ~5 rows per coin, ~1 KB per coin, so ~5 MB per 5,000 coins; nothing is pruned. Archive scratch copies live in a per-run child of --scratch that is removed on exit; anything else under --scratch is never touched.
No provider calls, no network, no funded database. Our own fills are not excluded from the prints (clip 25 USD)."""
import argparse
import bisect
import collections
import fcntl
import glob
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from sniper.office import outcomes as O
from sniper.services import memecoin_desk_ps_archive as AR

from sniper.services.memecoin_desk_ps_tape import TapeUnreadable

NETWORK = 'solana'
PAD_S = O.REF_MAX_AGE_S
RETENTION_S = 2 * 3600.      # MEMECOIN_DESK_PUMPSWAP_RETENTION_H default: live rows outside a keep window are deleted after this
PRUNE_MARGIN_S = 600.        # the prune runs in batches behind its cursor: a row this close to the cut may already be gone


def ro(path):
    return sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=5) if Path(path).exists() else None


def archive_days(archive_dir):
    """[(day, packed)] for keep-<day>.db and keep-<day>.db.zst, oldest first. A day in both forms raises: a late plain file must never hide packed rows."""
    days = {}
    for path in glob.glob(str(Path(archive_dir) / 'keep-*.db')) + glob.glob(str(Path(archive_dir) / 'keep-*.db.zst')):
        name = Path(path).name
        packed = name.endswith('.zst')
        day = name[len('keep-'):-len('.db.zst' if packed else '.db')]
        if day in days: raise ValueError('archive_day_ambiguous: %s' % day)
        days[day] = packed
    return sorted(days.items())


def universe(desk_db):
    """{mint: sheet_at} from the feed's candidate emissions: the first emission per mint, UTC epoch seconds."""
    conn = ro(desk_db)
    if conn is None: raise ValueError('outcome_desk_db_missing')
    try:
        return {m: datetime.fromisoformat(t).timestamp() for m, t in conn.execute('SELECT mint,MIN(emitted_at) FROM desk_candidate_emissions WHERE network_id=? GROUP BY mint', (NETWORK,))}
    finally:
        conn.close()


def market_at_emission(candidate, ticket):
    """-> (mcap_usd, liq_usd, source, supply_known_at) at the coin's first emission; a part that cannot be proven is None. Emission mcap is absent on every intake row (supply unknown then), so mcap is the emission's virtual-inclusive
    price times the hard filter's own mint read of supply, which lands seconds after the print; market_source says so. liq_usd is the emission's real quote reserve only, one side, no virtual. No read time for the supply is a null mcap, never a fallback."""
    fields = ((candidate or {}).get('snapshot') or {}).get('fields') or {}
    good = lambda x: type(x) in (int, float) and math.isfinite(x) and x > 0
    row = fields.get('liq_usd') or {}
    liq = row.get('value') if row.get('completeness') == 'complete' and good(row.get('value')) else None
    row = fields.get('mcap_usd') or {}
    if row.get('completeness') == 'complete' and good(row.get('value')):
        return row['value'], liq, 'emission_field', row.get('known_at')
    stage = next((s for s in (ticket or {}).get('stages') or [] if isinstance(s, dict) and s.get('stage') == 'hard_filter'), {})
    safety = (((stage.get('inputs') or {}).get('evidence') or {}).get('checks') or {}).get('token_safety') or {}
    state = (fields.get('pool_state') or {}).get('value') or {}
    partial = (None, liq, 'liq_only' if liq is not None else None, None)
    try:
        supply_at = datetime.fromisoformat(stage['inputs']['evidence']['known_at']).timestamp()
        base, quote, virtual, supply = (int(x) for x in (state['base_reserve_atomic'], state['quote_reserve_atomic'], state['virtual_quote_atomic'], safety['supply_atomic']))
        sol = state['sol_usd']
        if base <= 0 or supply <= 0 or quote <= 0 or virtual < 0 or not good(sol): raise ValueError
        mcap = (quote + virtual) / 1e9 * sol * supply / base      # supply and base share the mint's decimals
    except (KeyError, TypeError, ValueError, OverflowError):
        return partial
    return (mcap, liq, 'emission_reserves_x_hard_filter_supply', supply_at) if good(mcap) else partial


def add_market_inputs(coins, desk_db, known):
    """Fills market fields on coins not yet stored; a stored coin keeps its row, so none is looked up. Failure leaves them NULL: unknown, never zero."""
    fresh = [c for c in coins if c['mint'] not in known]
    if not fresh: return
    from sniper.persistence import db_memecoin_desk as desk
    conn = ro(desk_db)
    if conn is None: return
    conn.row_factory = sqlite3.Row
    try:
        for i in range(0, len(fresh), 200):
            part = {c['mint']: c for c in fresh[i:i + 200]}
            first = {}
            for r in conn.execute('SELECT mint,ticket_id,candidate_json,emitted_at FROM desk_candidate_emissions WHERE network_id=? AND mint IN (%s)' % ','.join('?' * len(part)), (NETWORK, *part)):
                if r['mint'] not in first or r['emitted_at'] < first[r['mint']]['emitted_at']: first[r['mint']] = r
            for mint, r in first.items():
                try:
                    part[mint]['mcap_usd'], part[mint]['liq_usd'], part[mint]['market_source'], part[mint]['supply_known_at'] = market_at_emission(json.loads(r['candidate_json']), desk.get_ticket(conn, r['ticket_id']))
                except (ValueError, TypeError, KeyError, sqlite3.Error):
                    pass
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def _days(lo, hi):
    d = int(lo // 86400)
    return [datetime.fromtimestamp(x * 86400, timezone.utc).strftime('%Y-%m-%d') for x in range(d, int(hi // 86400) + 1)]


class Curve:
    """pump.fun curve tape: data/desk/swaps/<day>.db, price from the trade's own virtual reserves. Coverage of a day file = its last received_at."""
    def __init__(self, desk_dir):
        self.dir, self.ends, self.launch = Path(desk_dir), {}, {}

    def _end(self, day, conn):
        if day not in self.ends: self.ends[day] = conn.execute('SELECT MAX(received_at) FROM swaps').fetchone()[0]
        return self.ends[day]

    def prints(self, mint, lo, hi):
        from sniper.services.memecoin_desk_ranker import price
        out, end = [], None
        for day in _days(lo, hi):
            conn = ro(self.dir / 'swaps' / ('%s.db' % day))
            if conn is None: continue
            try:
                out += [(t, price(q, k)) for t, q, k in conn.execute('SELECT received_at,virtual_quote,virtual_token FROM swaps WHERE mint=? AND received_at>=? AND received_at<?', (mint, lo, hi))]
                e = self._end(day, conn)
                if out and e is not None: end = max(end or 0, e)
            finally:
                conn.close()
        return out, end

    def creator(self, mint, lo, hi):
        for day in _days(lo - 86400, hi):
            conn = ro(self.dir / 'launches' / ('%s.db' % day))
            if conn is None: continue
            try:
                row = conn.execute('SELECT creator FROM launches WHERE mint=?', (mint,)).fetchone()
            finally:
                conn.close()
            if row and row[0]: return row[0]
        return None


class PumpSwap:
    """PumpSwap tape through the desk's own reader (proven virtual quote or no price). Live tape covers up to its newest receipt; an archive day covers a pool
    from its keep window start to its end. Archive days have no gap table, so gap_check is 'unavailable' for coins priced only from them."""
    def __init__(self, desk_dir, archive_dir, scratch, pools, now=None, retention_s=RETENTION_S, packed_floor=None):
        self.packed_floor, self.packed_skipped = packed_floor, []     # packed days before this UTC day cannot hold a pending window; None reads them all
        from sniper.services import memecoin_desk_ps_tape as T
        self.T, self.dir, self.pools, self.scratch = T, Path(desk_dir), pools, Path(scratch)
        self.live_end = None
        live = ro(T.tape_path(desk_dir))
        if live is not None:
            try: self.live_end = live.execute('SELECT MAX(received_at) FROM swaps_ps').fetchone()[0]
            finally: live.close()
        self._loss = None
        self._archive, self._archive_dir = None, archive_dir      # built on first use: a run with nothing pending copies no archive day (12 GB of scratch otherwise)
        self.skipped = []
        self.now, self.ret_s = time.time() if now is None else now, retention_s
        self.live_keep, self.selection = {}, {}          # pool -> [(from, until)] ; pool -> [(start, end)]
        live = ro(T.tape_path(desk_dir))
        if live is not None:
            try:
                for pool, a, b in live.execute('SELECT pool,keep_from,keep_until FROM ps_keep'): self.live_keep.setdefault(pool, []).append((a, b))
                for pool, a, b in live.execute('SELECT pool,start_at,end_at FROM ps_pool_selection'): self.selection.setdefault(pool, []).append((a, b))
            except sqlite3.Error: self.live_keep = self.selection = None       # unreadable: no coverage claim at all (tape_end None below)
            finally: live.close()

    @property
    def archive(self):
        if self._archive is None:
            self._archive = self._build_archive(self._archive_dir) if self._archive_dir else []
        return self._archive

    @archive.setter
    def archive(self, value):
        self._archive = value

    def _build_archive(self, archive_dir):
        """One pass over each plain archive day, keeping only the feed's canonical pools, with the indexes the desk reader requires."""
        out, self.scratch = [], self.scratch
        self.scratch.mkdir(parents=True, exist_ok=True)
        wanted = set(self.pools.values())
        for day, packed in archive_days(archive_dir):
            if packed and self.packed_floor is not None and day < self.packed_floor:
                self.packed_skipped.append(day); continue          # unpacking costs GB of scratch every hour
            dst_path = self.scratch / ('keep-%s.scratch.db' % day)
            with AR.open_day(archive_dir, day, scratch_dir=self.scratch) as src:
                dst = sqlite3.connect(dst_path)
                try:
                    ddl = src.execute("SELECT sql FROM sqlite_master WHERE name='swaps_ps'").fetchone()[0]
                    dst.execute(ddl)
                    cols = [r[1] for r in src.execute('PRAGMA table_info(swaps_ps)')]
                    marks = ','.join('?' * len(cols))
                    rows = (r for r in src.execute('SELECT %s FROM swaps_ps' % ','.join(cols)) if r[0] in wanted)
                    dst.executemany('INSERT INTO swaps_ps VALUES(%s)' % marks, rows)
                    dst.execute('CREATE INDEX swaps_ps_pool_slot ON swaps_ps(pool,slot)')
                    dst.execute('CREATE INDEX swaps_ps_pool_received ON swaps_ps(pool,received_at)')
                    dst.execute('CREATE TABLE ps_keep(pool TEXT PRIMARY KEY,keep_from REAL,keep_until REAL,signature TEXT,slot INTEGER) WITHOUT ROWID')
                    keep = {r[0]: (r[1], r[2]) for r in src.execute('SELECT pool,keep_from,keep_until FROM ps_keep')}
                    dst.executemany('INSERT OR IGNORE INTO ps_keep VALUES(?,?,?,NULL,NULL)', [(p, *keep[p]) for p in wanted if p in keep])
                    dst.commit()
                    out.append((dst_path, keep))
                finally:
                    dst.close()
        return out

    def _sliced(self, mint, lo, hi, depth=0, pre=False):
        """The desk reader refuses a window over 20,000 swaps (ps_swaps:row_limit). A busy coin is read in halves; each half seeds its own virtual-quote proof
        from before its start, so slicing changes no price. Past 12 halvings (about 1 s) the window is unreadable and the error stands."""
        try:
            got = self.T.load_ps_swaps(self.dir, mint, lo, hi, None, base_decimals=6, with_gaps=pre)
            return got[0] if pre else got              # pre=True: the reader's price-only ':pre' rows (the state right after a lost swap) come with the rows
        except self.T.TapeUnreadable as exc:
            if str(exc) != 'ps_swaps:row_limit' or depth >= 12: raise
            mid = (lo + hi) / 2
            return self._sliced(mint, lo, mid, depth + 1, pre) + self._sliced(mint, mid, hi, depth + 1, pre)

    def _load(self, path, mint, lo, hi, pre=False):
        T = self.T
        real = T._ro
        T._ro = lambda _d: sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=5)    # load_ps_swaps closes what it is given: a fresh handle per call
        try: return self._sliced(mint, lo, hi, 0, pre)
        finally: T._ro = real

    def _loss_model(self):
        """Loss and capture-gap retention plus the attributed losses, read once from the live tape: complete-scope losses per pool (times), unknown-scope losses (pool list empty, so they
        could touch any pool), and the first time each loss table covers. A window starting before max(loss_from, gaps_from) cannot be checked. Unreadable = TapeUnreadable, never empty."""
        if getattr(self, '_loss', None) is None:
            live = ro(self.T.tape_path(self.dir))
            if live is None: raise self.T.TapeUnreadable('ps_loss_attribution')
            try:
                by_pool, unknown, events = {}, [], {}
                for t, scope, pj, slot, disc in live.execute("SELECT received_at,scope,pools_json,slot,discriminator FROM ps_loss_attribution ORDER BY received_at"):
                    if scope == 'complete':
                        for pool in json.loads(pj):
                            by_pool.setdefault(pool, []).append(t); events.setdefault(pool, []).append((t, slot, disc))
                    else: unknown.append(t)
                lf = live.execute("SELECT MIN(received_at) FROM ps_loss_attribution").fetchone()[0]
                gf = live.execute("SELECT MIN(at) FROM gaps_ps").fetchone()[0]
                spans = [(x, y) for x, y in self.T.ps_gap_intervals(self.dir, 0.0, self.now + 3600.0)]       # every capture stop as an interval, open ones run to now
                if live.execute("SELECT 1 FROM sqlite_master WHERE name='ps_capture_intervals'").fetchone():       # the hole between one capture session's end and the next one's start is a capture stop too
                    ivs = live.execute('SELECT start_at,through_at FROM ps_capture_intervals ORDER BY start_at').fetchall()
                    spans += [(a_[1], b_[0]) for a_, b_ in zip(ivs, ivs[1:]) if a_[1] is not None and b_[0] is not None and b_[0] > a_[1]]
            except (sqlite3.Error, ValueError, TypeError): raise self.T.TapeUnreadable('ps_loss_attribution')
            finally: live.close()
            self._loss = dict(by_pool=by_pool, events=events, unknown=unknown, loss_from=lf, gaps_from=gf, spans=spans)
        return self._loss

    def checked(self, lo):
        """True when the loss tables cover a window starting at lo (so a window with no loss is clean, not unobserved)."""
        m = self._loss_model()
        return m['loss_from'] is not None and m['gaps_from'] is not None and lo >= max(m['loss_from'], m['gaps_from'])

    def _pool_losses(self, pool, lo, hi):
        """Receipt times of complete-scope losses the attribution assigns to THIS pool inside (lo, hi]."""
        ts = self._loss_model()['by_pool'].get(pool, [])
        return [t for t in ts[bisect.bisect_right(ts, lo):bisect.bisect_right(ts, hi)]]

    def window_gaps(self, pool, a, b):
        """-> (points, spans) for the window (a, b]: capture-gap points and this pool's complete-scope losses inside it, and every capture-stop INTERVAL that overlaps it (a stop that began
        before the window and ends after it is a whole-window loss; a clipped point at the read edge would let it pass)."""
        m = self._loss_model()
        pts = [t for t in self.T.ps_point_gaps(self.dir, a, b) if a < t <= b] + self._pool_losses(pool, a, b)
        return sorted(set(pts)), [(x, y) for x, y in m['spans'] if y > a and x <= b]

    def bracket(self, mint, pool, a, b):
        """R6.1: bracket_losses() for the pool's complete-scope losses in (a, b]. Reads the window again with the reader's ':pre' rows (live tape plus the archive days that cover the pool),
        from a - PAD_S so the print before the first loss is seen. All of the pool's losses from a - PAD_S to b take part, so a second loss in a gap blocks it."""
        lo, ev = a - PAD_S, self._loss_model()['events'].get(pool, [])
        near = [e for e in ev if lo < e[0] <= b]
        if not any(a < e[0] <= b and e[2] in O.BRACKET_KINDS for e in near): return None
        rows = []
        if self.live_end is not None and lo < self.live_end:
            rows += self._sliced(mint, lo, min(math.nextafter(b, math.inf), self.live_end + 1e-6), 0, True)
        for path, keep in self.archive:
            k = keep.get(pool)
            if k is None or k[1] < lo or k[0] > b: continue
            rows += self._load(path, mint, lo, math.nextafter(b, math.inf), True)
        seen, merged = set(), []
        for r in sorted(rows, key=lambda r: (r['received_at'], r['slot'], r['ordinal'])):
            key = (r['signature'], r['ordinal'])
            if key not in seen: seen.add(key); merged.append(r)
        merged.sort(key=lambda r: (r['slot'], r['ordinal']))
        out = O.bracket_losses([r for r in merged if r['received_at'] <= b], near)
        return dict(out, times={t for t in out['times'] if a < t <= b}, blocked={t: w for t, w in out['blocked'].items() if a < t <= b})

    def capture_points(self, a, b):
        """Capture-gap receipt times in (a, b], kept apart from the pool's loss times: a capture discontinuity always censors."""
        return [t for t in self.T.ps_point_gaps(self.dir, a, b) if a < t <= b]

    def loss_counts(self, pool, a, b):
        """(complete-scope losses naming the pool, unknown-scope losses) in (a, b], or None when the window is outside the loss tables' retention."""
        if not self.checked(a): return None
        m = self._loss_model()
        ts, un = m['by_pool'].get(pool, []), m['unknown']
        return (bisect.bisect_right(ts, b) - bisect.bisect_right(ts, a), bisect.bisect_right(un, b) - bisect.bisect_right(un, a))

    def coverage_end(self, pool, start, hi):
        """The end of the stored span of THIS pool that is contiguous from `start` (the first time rows are needed), or None when `start` is not covered. Intervals, never a
        maximum: keep windows (live ps_keep + archive keeps), the unpruned live tail for a pool in the stored set M (it has a keep window), and for a pool outside M only its
        feed-listing intervals (ps_pool_selection, an open row runs to the live end) inside the unpruned tail. Holes between intervals are never bridged; no selection and no keep
        window means unknown. Live rows outside a keep window are deleted RETENTION_S after receipt (8fc3eaa5, 2026-10-30)."""
        if self.live_keep is None or self.live_end is None: return None
        keeps = sorted(self.live_keep.get(pool, []) + [k for _, ks in self.archive for k in ([ks[pool]] if pool in ks else [])])
        live_from = self.now - self.ret_s + PRUNE_MARGIN_S         # live rows received after this are not yet pruned
        spans = [(a, b) for a, b in keeps]
        if keeps:
            spans.append((max(live_from, min(a for a, _ in keeps)), self.live_end))        # M pool: its unpruned tail
        elif self.selection is not None:
            for a, b in self.selection.get(pool, []):
                spans.append((max(a, live_from), self.live_end if b is None else min(b, self.live_end)))
        spans = sorted(s for s in spans if s[0] <= s[1])
        end = None
        for a, b in spans:
            if end is None:
                if a <= start <= b: end = b
            elif a <= end: end = max(end, b)                       # touching or overlapping extends the contiguous span; a hole stops it
        return end

    def prints(self, mint, lo, hi, start=None):
        """-> prints, tape_end, creator, gaps, gap_check. start: the first time rows are needed for coverage (default lo)."""
        T, pool = self.T, self.pools.get(mint)
        rows, end, gaps, gap_check = [], None, [], 'unavailable'
        if self.live_end is not None and lo < self.live_end:
            got = self._sliced(mint, lo, min(hi, self.live_end + 1e-6))
            if got: rows += got
        for path, keep in self.archive:
            k = keep.get(pool)
            if k is None or k[1] < lo or k[0] > hi: continue
            got = self._load(path, mint, lo, hi)
            if got:
                rows += got
        if self.live_end is not None and self.checked(lo):     # capture gaps and pool-attributed losses are retained longer than the swaps: any window they cover is checked, whichever tape priced it
            gaps, gap_check = T.ps_gaps(self.dir, lo, hi) + self._pool_losses(pool, lo, hi), 'checked'
        end = self.coverage_end(pool, lo if start is None else start, hi) if rows else None      # the contiguous stored span of THIS pool, never the live tape's global end
        seen, merged = set(), []
        for r in sorted(rows, key=lambda r: (r['received_at'], r['signature'], r['ordinal'])):
            key = (r['signature'], r['ordinal'])
            if key not in seen: seen.add(key); merged.append(r)
        creator = next((r['coin_creator'] for r in merged if r.get('coin_creator')), None)
        return [(r['received_at'], r['price_sol']) for r in merged], end, creator, gaps, gap_check


def load_versions(path):
    """[(effective_from, version)] oldest first from the decoder-version file; an unreadable or empty file refuses the pass (an unstamped row would be unfixable, rows are write-once)."""
    try: doc = json.loads(Path(path).read_text())
    except (OSError, ValueError): raise ValueError('outcome_decoder_file_unreadable') from None
    out = [(v['effective_from'], v['version']) for v in doc.get('versions', [])]
    if not out or out != sorted(out) or len({n for _, n in out}) != len(out): raise ValueError('outcome_decoder_file_invalid')
    return out


def process(universe_map, curve, ps, existing, *, now, limit=None, versions=((0, O.DEFAULT_DECODER),), diag=None):
    """-> (coins, horizon rows). existing: {(mint, horizon_s)} already written."""
    coins, horizons, n = [], [], 0
    for mint, t0 in sorted(universe_map.items(), key=lambda kv: kv[1]):
        pending = [h for h in O.HORIZONS if (mint, h) not in existing and now >= t0 + h + O.SETTLE_S]
        if not pending: continue
        if limit is not None and n >= limit: break
        n += 1
        lo = t0 - PAD_S
        # A row for horizon h is causal and immutable, so it is computed from a read that stops at t0+h and from nothing later: its sources, gap check and tape end
        # come from its own window, never from a longer pending one (a late print or capture gap cannot change an earlier row). Windows nest, so the extra reads cost under 2x.
        rows, creator, csrc, price_src = [], None, None, set()
        for h in sorted(pending):
            hi = math.nextafter(t0 + h, math.inf)       # nothing received after t0+h reaches the row or its metadata (no +1 s pad)
            try:
                cp, cend = curve.prints(mint, lo, hi)
                pp, pend, c, gaps, gap_check = ps.prints(mint, lo, hi, start=t0) if ps is not None else ([], None, None, [], 'unavailable')
            except TapeUnreadable:
                rows.append(O.horizon_row([], t0, h, now=now, tape_end=None, unreadable=True, decoder=O.decoder_version(versions, lo, t0 + h)))
                continue
            if creator is None and c: creator, csrc = c, 'ps_tape'
            cp, pp = [x for x in cp if x[0] <= t0 + h], [x for x in pp if x[0] <= t0 + h]      # belt and braces on the reader's inclusive bound
            tape_end = pend if pp else cend          # a coin with PumpSwap rows is covered by its PumpSwap span only: an old curve print or the curve tape's global end proves nothing about the pool
            price_src.update(sname for sname, got in (('curve', cp), ('pumpswap', pp)) if got)
            if not (gap_check == 'checked' and (pp or not cp)): gap_check = 'unavailable'        # losses are checked only where PumpSwap rows were read; a curve reference neither adds nor removes them
            pool_h = getattr(ps, 'pools', {}).get(mint) if ps is not None else None
            gpoints, gspans, loss, brk, cap = gaps, [], None, None, []
            if gap_check == 'checked' and ps is not None and hasattr(ps, 'window_gaps'):
                cap = ps.capture_points(t0, t0 + h) if hasattr(ps, 'capture_points') else []
                gpoints, gspans = ps.window_gaps(pool_h, t0, t0 + h)       # this horizon's own window: points, pool losses, overlapping capture-stop intervals
                loss = ps.loss_counts(pool_h, t0, t0 + h)
                if hasattr(ps, 'bracket') and pool_h is not None and loss is not None and loss[0]:
                    try: brk = ps.bracket(mint, pool_h, t0, t0 + h)
                    except TapeUnreadable: brk = None       # unreadable bracket evidence = not bracketed (the window stays censored)
            rows.append(O.horizon_row(O.clean(cp + pp), t0, h, now=now, tape_end=tape_end, gaps=gpoints, gap_check=gap_check, loss=loss, gap_spans=gspans, bracketed=brk, capture_points=cap,
                                      decoder=O.decoder_version(versions, lo, t0 + h)))
            if diag is not None and rows[-1] is not None and rows[-1]['status'] == 'censored_gap':     # why the window censors: printed in the run stats only, never stored
                free, ev = (brk['times'] if brk else set()), (ps._loss_model()['events'].get(pool_h, []) if hasattr(ps, '_loss_model') else [])
                inside = [e for e in ev if t0 < e[0] <= t0 + h and e[0] not in free]
                blocked = brk['blocked'] if brk else {}
                why = {'loss:' + blocked.get(e[0], 'kind:' + str(e[2])) for e in inside}
                if cap: why.add('capture_gap_point')
                if gspans: why.add('capture_stop')
                diag.append((h, tuple(sorted(why)) or ('none',)))
        if creator is None: creator, csrc = curve.creator(mint, lo, math.nextafter(t0 + max(pending), math.inf)), 'launches'
        if creator is None: csrc = None
        coins.append(dict(network_id=NETWORK, mint=mint, sheet_at=t0, creator=creator, creator_source=csrc, price_source='+'.join(sorted(price_src, key=('curve', 'pumpswap').index)) or None))
        horizons += [(NETWORK, mint, r) for r in rows if r is not None]
    return coins, horizons


def creator_rows(desk_dir, archive_dir, runner_db, unavailable=None, packed_read=None, scratch=None):
    """Every (mint, creator) any tape or table names, not only the feed's coins: curve launches, scanner profiles, the live PumpSwap tape and each archive day.
    source_at is the earliest time that source saw the pair (launch block time, scan time, first receipt)."""
    out = []
    for path in sorted(glob.glob(str(Path(desk_dir) / 'launches' / '*.db'))):
        conn = ro(path)
        try: out += [(NETWORK, m, c, 'launches', float(t)) for m, c, t in conn.execute('SELECT mint,creator,block_time FROM launches WHERE creator IS NOT NULL AND block_time IS NOT NULL')]
        finally: conn.close()
    conn = ro(runner_db)
    if conn is None and unavailable is not None: unavailable.append('runner_signals')     # named, never an empty source
    if conn is not None:
        try:
            for m, c, t in conn.execute('SELECT mint,creator,created_at FROM runner_signals WHERE creator IS NOT NULL AND created_at IS NOT NULL'):
                try: out.append((NETWORK, m, c, 'runner_signals', datetime.fromisoformat(t).timestamp()))
                except ValueError: continue
        finally: conn.close()
    SQL = 'SELECT mint,coin_creator,MIN(received_at) FROM swaps_ps WHERE mint IS NOT NULL AND coin_creator IS NOT NULL GROUP BY mint,coin_creator'
    def read(conn, source): return [(NETWORK, m, c, source, float(t)) for m, c, t in conn.execute(SQL)]
    conn = ro(Path(desk_dir) / 'pumpswap' / 'tape.db')
    if conn is None:
        if unavailable is not None: unavailable.append('ps_live')
    else:
        try: out += read(conn, 'ps_live')
        finally: conn.close()
    for day, packed in archive_days(archive_dir):
        if packed and packed_read is not None and day in packed_read: continue      # a packed day is immutable and first write wins: once is enough
        try:
            with AR.open_day(archive_dir, day, scratch_dir=scratch) as conn:
                out += read(conn, 'ps_archive')
            if packed and packed_read is not None: packed_read.add(day)
        except (OSError, sqlite3.Error, subprocess.CalledProcessError):
            if unavailable is not None: unavailable.append('keep-%s' % day)
    return out


def existing_keys(out):
    if not Path(out).exists(): return set()
    conn = sqlite3.connect(out)
    try: return {(m, h) for m, h in conn.execute('SELECT mint,horizon_s FROM outcome_horizons')}
    except sqlite3.OperationalError: return set()
    finally: conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desk-dir', type=Path, default=ROOT / 'data/desk')
    parser.add_argument('--desk-db', type=Path, default=ROOT / 'data/memecoin_desk.db')
    parser.add_argument('--archive-dir', type=Path, default=ROOT / 'data/archive/pumpswap')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, default=Path.home() / '.cache/office-outcomes-scratch')
    parser.add_argument('--decoder-file', type=Path, default=ROOT / 'config/office/tape_decoder_versions.json')
    parser.add_argument('--runner-db', type=Path, default=ROOT / 'data/runner_signals.db')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--dry', action='store_true')
    args = parser.parse_args(argv)
    now = time.time()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(args.out) + '.lock', 'w')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: print(json.dumps({'state': 'refused', 'reason': 'outcome_writer_busy'}), file=sys.stderr); return 2
    own = None
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))      # a killed run still leaves through the finally below and removes its scratch
    try:
        uni = universe(args.desk_db)
        from sniper.services import memecoin_desk_ps_tape as T
        pools = {m: T.canonical_pool(m) for m in uni}
        args.scratch.mkdir(parents=True, exist_ok=True)
        own = tempfile.mkdtemp(prefix='run-', dir=args.scratch)       # this run's own child: a shared or foreign scratch root is never emptied
        done = existing_keys(args.out)
        pending = [t0 for m, t0 in uni.items() if any((m, h) not in done for h in O.HORIZONS)]
        floor = datetime.fromtimestamp(min(pending) - 86400, timezone.utc).date().isoformat() if pending else '9999-12-31'   # one day back: a day file can hold the window's start
        ps = PumpSwap(args.desk_dir, args.archive_dir, own, pools, packed_floor=floor)
        diag = []
        coins, horizons = process(uni, Curve(args.desk_dir), ps, existing_keys(args.out), now=now, limit=args.limit, versions=load_versions(args.decoder_file), diag=diag)
        lm = ps._loss_model()
        stats = {'checked_from': 'first loss/gap record (observed lower bound, not proof the writers ran continuously since)', 'loss_first': lm['loss_from'], 'gaps_first': lm['gaps_from'], 'universe': len(uni), 'coins_processed': len(coins), 'horizon_rows': len(horizons), 'archive_days': len(ps._archive) if ps._archive is not None else 0,
                 'status': {s: sum(1 for _, _, r in horizons if r['status'] == s) for s in O.STATUSES},
                 'censor_reasons': {str(h): dict(collections.Counter('+'.join(w) for hh, w in diag if hh == h)) for h in O.HORIZONS}}
        if not args.dry:
            conn = sqlite3.connect(args.out, timeout=30)
            try:
                O.ensure_schema(conn)
                add_market_inputs(coins, args.desk_db, {r[0] for r in conn.execute('SELECT mint FROM outcome_coins')})
                O.write(conn, asof=now, coins=coins, horizons=horizons)
                gone = []
                conn.execute('CREATE TABLE IF NOT EXISTS outcome_packed_days_read(day TEXT PRIMARY KEY)')
                read_days = {r[0] for r in conn.execute('SELECT day FROM outcome_packed_days_read')}
                found = creator_rows(args.desk_dir, args.archive_dir, args.runner_db, unavailable=gone, packed_read=read_days, scratch=own)
                O.write_creators(conn, found)
                with conn: conn.executemany('INSERT OR IGNORE INTO outcome_packed_days_read VALUES(?)', [(d,) for d in read_days])
                stats['creator_sources_unavailable'] = gone
                stats['mint_creators'] = conn.execute('SELECT COUNT(*),COUNT(DISTINCT mint) FROM mint_creators').fetchone()
            finally: conn.close()
        print(json.dumps(dict(stats, dry=args.dry))); return 0
    finally:
        if own: shutil.rmtree(own, ignore_errors=True)
        lock.close()


if __name__ == '__main__': raise SystemExit(main())
