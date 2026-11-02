#!/usr/bin/env python3
"""PS tape-reader day-end census (Gamma). Binds docs/audits/2026-10-28-ps-tape-reader-retrain-admissibility-preregistration.md
sections 2-4 and Amendment 1. Read-only, bounded per pool through swaps_ps_pool_received, run with `nice -n 19`.
PRICE LABELS ONLY: no model, no score, no PnL. Counts per UTC day of the graduation receipt (ps_grads.received_at >= t_w; receipt = ps_keep.keep_from + 60):

  created (graduated pools, ps_grads) -> kept (ps_keep window >= 1,260 s) -> window_complete (capture intervals cover
  [receipt, receipt+1200 s] and the tape has reached it) -> with_8_swaps -> labelled coins (>= 1 resolved primary sample)

Primary target (20 %, 10 %, 300 s): sample every 30 s from age 10 s to 900 s, >= 8 swaps so far, entry = first post-swap
price at or after t + 1.5 s, up barrier +20 % first = positive, -10 % first = negative, neither in 300 s = zero.
Same-slot hit of both barriers = censored `same_slot`. Feature window needs age >= 300 s (`feature_incomplete` otherwise).
Samples whose [t-300, t+300] holds a PROVEN OWN-POOL loss receipt (JUDGE-path memecoin_desk_ps_losses.losses receipt_details class
`own_pool_losses`; unreadable = all) are censored `tape_loss`; receipts the decoder cannot attribute (`unknown_loss_receipts`,
`global_loss_receipts`) are ignored and counted per pool-window (`ignored_receipt:*`). Real quote reserve < 40 SOL at the sample = `reserve_lt_40sol`.
NOT applied (needs FX/supply): the 5k..50M USD market-cap safety bound. Price = post_quote_reserve / post_base_reserve.

  nice -n 19 python3 scripts/research/ps_tape_reader_census.py --day 2026-10-29 [--t-w 1793180450.80161]
  python3 scripts/research/ps_tape_reader_census.py --self-check
"""
import argparse
import bisect
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

T_W = 1793180450.80161          # prereg: t_w = 10:40:50Z 10-28 (Amendment 1 shows 09:40:50Z; the task fact and Alpha's episode both say this value)
UP, DOWN, HORIZON, STEP, FIRST, MAXAGE, FEAT, LAT = 0.20, 0.10, 300.0, 30.0, 10.0, 900.0, 300.0, 1.5
KEEP_LEN, MIN_SWAPS, MIN_RESERVE = 1200.0, 8, 40_000_000_000


def label(prices, t0, tt, up=UP, down=DOWN, horizon=HORIZON):
    """prices: time-sorted [(t, slot, price)]. Entry at first price >= tt+LAT; returns (label|None, reason)."""
    times = [p[0] for p in prices]
    i = bisect.bisect_left(times, tt + LAT)
    if i >= len(prices) or prices[i][0] > tt + LAT + 60:
        return None, 'no_entry'
    p0, end = prices[i][2], tt + horizon
    if p0 <= 0:
        return None, 'bad_price'
    for t, slot, p in prices[i + 1:]:
        if t > end:
            return 0, ''
        hit_up, hit_dn = p >= p0 * (1 + up), p <= p0 * (1 - down)
        if hit_up or hit_dn:
            if hit_up and hit_dn:
                return None, 'same_slot'
            same = [q for (t2, s2, q) in prices[i + 1:] if s2 == slot and t2 <= end]
            if any(q >= p0 * (1 + up) for q in same) and any(q <= p0 * (1 - down) for q in same):
                return None, 'same_slot'
            return (1 if hit_up else -1), ''
    return None, 'tape_ends'


def covered(ivs, lo, hi):
    cur = lo
    for s, t in ivs:
        if s <= cur < t:
            cur = t
        if cur >= hi:
            return True
    return cur >= hi


WAL_PAUSE_BYTES, WAL_MAX_WAIT_S = 256 * 1024 * 1024, 600


def wal_guard(db, sleep=time.sleep, size=lambda p: p.stat().st_size if p.exists() else 0, pause=WAL_PAUSE_BYTES, max_wait=WAL_MAX_WAIT_S):
    """Hold no tape.db snapshot while the live WAL is large; the writer's checkpoint needs reader gaps (Alpha 2026-10-31:
    a nice-19 run pinned it to 2.8 GB). Raises if the WAL does not fall, so a stuck run stops instead of pinning it."""
    wal, waited = Path(str(db) + '-wal'), 0
    while size(wal) > pause:
        if waited >= max_wait:
            raise RuntimeError('wal_guard: %s still %d bytes after %d s' % (wal, size(wal), waited))
        sleep(5)
        waited += 5


def tape_read(db, sql, args=()):
    """One tape.db read: own connection, fully fetched, closed before return, so no snapshot outlives the statement."""
    wal_guard(db)
    c = sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def swap_order(r):
    """ORDER BY received_at,slot,tx_index,ordinal with SQLite's rule: NULL sorts first (tx_index is nullable)."""
    return tuple((r[i] is not None, 0 if r[i] is None else r[i]) for i in (0, 1, 4, 5))


def archive_days(archive_dir, d0):
    """The plain archive days a coin of UTC day d0 can land in: d0 and every later day, because a keep extension can move
    keep_until (the archive day) past d0 + 1. A compressed one stops the run: skipping it would silently count those
    coins as `not_kept_1260` or `lt_8_swaps`."""
    first = 'keep-%s.db' % datetime.fromtimestamp(d0, timezone.utc).strftime('%Y-%m-%d')
    packed = [f.name for f in Path(archive_dir).glob('keep-*.db.zst') if f.name >= first]
    if packed:
        raise RuntimeError('archive day compressed, unpack through memecoin_desk_ps_archive.open_day: %s' % sorted(packed))
    return sorted(f for f in Path(archive_dir).glob('keep-*.db') if f.name >= first)


def run(day, t_w, no_loss=False, min_reserve=MIN_RESERVE, max_pools=None, db=ROOT / 'data/desk/pumpswap/tape.db', desk_dir=ROOT / 'data/desk', archive_dir=ROOT / 'data/archive/pumpswap'):
    from sniper.services import memecoin_desk_ps_losses as L
    arch_conns = []
    d0 = datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()
    arch = archive_days(archive_dir, d0) if archive_dir and archive_dir.is_dir() else []
    for f in arch:        # expired keep windows move to a daily archive (memecoin_desk_universe_worker.archive_keep_ps)
        arch_conns.append(sqlite3.connect(f.resolve().as_uri() + '?mode=ro', uri=True, timeout=5))    # never attached to tape.db
    ivs = sorted((s, t) for _, s, t in tape_read(db, 'SELECT session,start_at,through_at FROM ps_capture_intervals'))
    tape_end = max(t for _, t in ivs)
    out = Counter()
    reasons, pos, ignored = Counter(), Counter(), Counter()
    coins = []
    # Universe = graduated pools (ps_grads migration receipt). The `pools` table also holds ~0.02 SOL dust pools that
    # are flagged pump_migrated; they have no ps_grads row and never reach the 40 SOL floor.
    grads = tape_read(db, 'SELECT pool,mint,min(received_at) FROM ps_grads WHERE received_at>=? AND received_at<? AND received_at>=? '
                      'GROUP BY pool ORDER BY 3', (d0, d0 + 86400, t_w))
    keep = {}
    kq = 'SELECT pool,keep_from,keep_until FROM ps_keep'
    for p, a, b in tape_read(db, kq) + [r for ac in arch_conns for r in ac.execute(kq).fetchall()]:
        k = keep.get(p)
        keep[p] = (min(a, k[0]), max(b, k[1])) if k else (a, b)
    for pool, mint, grx in grads[:max_pools]:
        out['created'] += 1
        k = keep.get(pool)
        if not k or k[1] - k[0] < KEEP_LEN + 60 - 1:
            reasons['not_kept_1260'] += 1
            continue
        rx = k[0] + 60          # keep_from = receipt - 60 s
        out['kept'] += 1
        if rx + KEEP_LEN > tape_end or not covered(ivs, rx, rx + KEEP_LEN):
            reasons['window_incomplete'] += 1
            continue
        out['window_complete'] += 1
        sel = ('SELECT received_at,slot,post_quote_reserve_atomic,post_base_reserve_atomic,tx_index,ordinal,signature FROM swaps_ps '
               'WHERE pool=? AND received_at>=? AND received_at<=?')
        wa = (pool, rx, rx + KEEP_LEN + 5)
        merged = set(tape_read(db, sel, wa))     # the set dedupes a batch split across tape and archive, as UNION did
        for ac in arch_conns:
            merged.update(ac.execute(sel, wa).fetchall())
        rows = [r[:4] for r in sorted(merged, key=swap_order)]
        prices, q = [], []
        for t, slot, pq, pb in rows:
            try:
                a, b = int(pq), int(pb)
            except (TypeError, ValueError):
                continue
            if b > 0:
                prices.append((t, slot, a / b))
                q.append(a)
        if len(prices) >= MIN_SWAPS:
            out['with_8_swaps'] += 1
        else:
            reasons['lt_8_swaps'] += 1
            continue
        try:
            det = []
            wal_guard(db)
            read_started = time.monotonic()
            L.losses(str(desk_dir), pool, rx - 60, rx + KEEP_LEN + 5, receipt_details=det)
            # Operator ruling 2026-10-29 (prereg Amendment 2): censor only proven own-pool losses; every receipt the decoder
            # cannot attribute to a pool is IGNORED here and counted, never applied to all pools.
            time.sleep(time.monotonic() - read_started)   # at most half duty on tape.db, so the checkpoint gets gaps
            lt = [x[1] for x in det if x[2] == 'own_pool_losses']
            for x in det:
                if x[2] != 'own_pool_losses':
                    ignored[x[2]] += 1
        except L.LossUnreadable as x:
            lt, reasons['loss_unreadable:' + str(x)] = None, reasons['loss_unreadable:' + str(x)] + 1
        times = [p[0] for p in prices]
        res = []
        age = FIRST
        while age <= MAXAGE:
            tt = rx + age
            n = bisect.bisect_right(times, tt)
            if n < MIN_SWAPS:
                reasons['sample_lt_8'] += 1
            elif age < FEAT:
                reasons['feature_incomplete'] += 1
            elif q[n - 1] < min_reserve:
                reasons['reserve_lt_40sol'] += 1
            elif not no_loss and (lt is None or any(tt - FEAT <= x <= tt + HORIZON + LAT for x in lt)):
                reasons['tape_loss'] += 1
            else:
                lab, why = label(prices, rx, tt)
                if lab is None:
                    reasons[why] += 1
                else:
                    res.append(lab)
            age += STEP
        if res:
            out['labelled_coins'] += 1
            out['labelled_samples'] += len(res)
            pos['pos'] += sum(1 for r in res if r == 1)
            pos['n'] += len(res)
            coins.append((mint, len(res), sum(1 for r in res if r == 1)))
    reasons.update({'ignored_receipt:' + k: v for k, v in ignored.items()})
    return out, reasons, pos, coins


def self_check():
    flat = [(float(i), 1000 + i, 1.0) for i in range(0, 400, 5)]
    assert label(flat, 0, 10) == (0, ''), label(flat, 0, 10)
    up = [(float(i), 1000 + i, 1.0 if i < 100 else 1.3) for i in range(0, 400, 5)]
    assert label(up, 0, 10) == (1, ''), label(up, 0, 10)
    dn = [(float(i), 1000 + i, 1.0 if i < 100 else 0.85) for i in range(0, 400, 5)]
    assert label(dn, 0, 10) == (-1, '')
    same = [(0.0, 1, 1.0), (5.0, 2, 1.0), (50.0, 7, 1.3), (50.0, 7, 0.8)] + [(float(i), 9, 1.0) for i in range(60, 400, 20)]
    assert label(same, 0, 0)[1] == 'same_slot', label(same, 0, 0)
    assert label([(0.0, 1, 1.0)], 0, 10)[1] == 'no_entry'
    mixed = [(1.0, 5, 0, 0, 2, 0, 'a'), (1.0, 5, 0, 0, None, 1, 'b'), (1.0, 5, 0, 0, 1, 0, 'c'), (1.0, 5, 0, 0, None, 0, 'd')]
    assert [r[6] for r in sorted(mixed, key=swap_order)] == ['d', 'b', 'c', 'a']
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        Path(td, 'keep-2026-10-29.db').touch()
        Path(td, 'keep-2026-10-27.db').touch()
        d29 = datetime(2026, 10, 29, tzinfo=timezone.utc).timestamp()
        Path(td, 'keep-2026-10-31.db').touch()     # a keep extension can archive past d + 1 (Zeta)
        assert [f.name for f in archive_days(td, d29)] == ['keep-2026-10-29.db', 'keep-2026-10-31.db']
        Path(td, 'keep-2026-11-02.db.zst').touch()
        try:
            archive_days(td, d29)
            raise AssertionError('compressed day not refused')
        except RuntimeError:
            pass
    waits = []
    sizes = iter([900, 900, 10])
    wal_guard('x', sleep=waits.append, size=lambda p: next(sizes), pause=100)
    assert waits == [5, 5], waits
    try:
        wal_guard('x', sleep=lambda s: None, size=lambda p: 900, pause=100, max_wait=10)
        raise AssertionError('guard did not stop')
    except RuntimeError:
        pass
    assert covered([(0, 50), (50, 100)], 10, 90) and not covered([(0, 50), (60, 100)], 10, 90)
    print('self-check ok')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--day')
    p.add_argument('--t-w', type=float, default=T_W)
    p.add_argument('--no-loss', action='store_true', help='SENSITIVITY ONLY: skip tape_loss censoring')
    p.add_argument('--min-reserve-sol', type=float, default=40, help='SENSITIVITY ONLY')
    p.add_argument('--max-pools', type=int, help='short proof run: first N graduated pools only')
    p.add_argument('--data-root', default=str(ROOT), help='checkout holding data/ (a worktree has none)')
    p.add_argument('--counts-only', action='store_true', help='H days (Amendment 4): counts only, no rate or censor detail')
    p.add_argument('--self-check', action='store_true')
    a = p.parse_args()
    if a.self_check:
        return self_check()
    out, reasons, pos, coins = run(a.day, a.t_w, a.no_loss, int(a.min_reserve_sol * 1e9), a.max_pools, Path(a.data_root) / 'data/desk/pumpswap/tape.db',
                                 Path(a.data_root) / 'data/desk', Path(a.data_root) / 'data/archive/pumpswap')
    n = pos['n']
    if a.counts_only:
        print(json.dumps(dict(day=a.day, read_at=time.time(), t_w=a.t_w, counts=dict(out)), indent=1))
        return
    print(json.dumps(dict(day=a.day, read_at=time.time(), t_w=a.t_w, counts=dict(out), censor_or_drop_reasons=dict(reasons),
                          labelled_samples=n, positive_rate=(pos['pos'] / n if n else None),
                          note='price labels only; mcap 5k..50M bound not applied; day may be partial if read before 00:00Z'), indent=1))


if __name__ == '__main__':
    main()
