"""Candidate lists for seats from the PumpSwap tape (Alpha 2026-10-31). Reported, never a gate: no order path reads this.

graduated_liquid: pools whose newest stored swap shows real quote at or above MIN_REAL_QUOTE_SOL. They tend to have prints to fill and exit on.
buyer_growth: pools with at least MIN_BUYERS distinct buyers in the last 30 min and at least GROWTH times the 30 min before. A proxy: holder growth itself is not sourced.
On one 60 min forward window neither showed an edge over all pools; these widen what a seat sees and do not predict.
Stored pools and stored rows only: a lost swap is not counted, and a pool outside the stored set cannot appear, so absence from a list says nothing.
A newest row without a reserve leaves the pool out of graduated_liquid and is counted."""
import json
import math
import sqlite3
from pathlib import Path

MIN_REAL_QUOTE_SOL = 85.0
MIN_BUYERS = 20
GROWTH = 1.5
WINDOW_S = 1800.0
MAX_ITEMS = 40
MAX_AGE_S = 1800.0   # two 15 min cron periods: one late run must not blank the lists
SOL_MAX_AGE_S = 120.0
FILE_NAME = 'candidate_lists.json'
READ_BUDGET_OPS = 200_000_000


def _sol(desk_dir, now):
    """SOL/USD only when dated and fresh at now; anything else None, and the lists then show no USD."""
    try:
        beat = json.loads((Path(desk_dir) / 'universe.heartbeat').read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(beat, dict):
        return None
    sol, age, ts = beat.get('sol_usd'), beat.get('sol_age_s'), beat.get('ts')
    good = lambda x: type(x) in (int, float) and math.isfinite(x)
    if not (good(sol) and sol > 0 and good(age) and age >= 0 and good(ts) and ts <= now and age + now - ts <= SOL_MAX_AGE_S):
        return None
    return sol


def build(desk_dir, now):
    empty = lambda reason: dict(state='unknown', reason=reason, as_of=now, graduated_liquid=[], buyer_growth=[])
    try:
        conn = sqlite3.connect((Path(desk_dir) / 'pumpswap' / 'tape.db').resolve().as_uri() + '?mode=ro', uri=True, timeout=1.0)
    except (sqlite3.Error, OSError):
        return empty('tape_unreadable')
    sol = _sol(desk_dir, now)      # before the scan: it takes 38 s, and the heartbeat read after it would be later than now
    left = [READ_BUDGET_OPS // 10000]

    def tick():
        left[0] -= 1
        return left[0] < 0
    conn.set_progress_handler(tick, 10000)
    pools = {}
    try:
        for pool, mint, at, slot, buy, actor, quote, index, ordinal in conn.execute(
                "SELECT pool,mint,received_at,slot,is_buy,actor,post_quote_reserve_atomic,tx_index,ordinal FROM swaps_ps INDEXED BY swaps_ps_received WHERE received_at>? AND received_at<=? "
                "AND quote_mint='So11111111111111111111111111111111111111112' ORDER BY received_at", (now - 2 * WINDOW_S, now)):
            row = pools.setdefault(pool, dict(mint=mint, new=set(), old=set()))
            order = (slot if type(slot) is int else -1, index if type(index) is int else -1, ordinal if type(ordinal) is int else -1)
            if order >= row.get('order', (-1, -1, -1)):              # chain order: a delayed receipt never replaces newer state
                row.update(order=order, at=at, slot=slot, quote=quote)
            if buy == 1 and actor:
                (row['new'] if at > now - WINDOW_S else row['old']).add(actor)
    except sqlite3.Error as exc:
        return empty('read_budget_exceeded' if left[0] < 0 else 'tape_unreadable')
    finally:
        conn.close()
    liquid, growth, no_reserve = [], [], 0
    for pool, r in pools.items():
        item = dict(pool=pool, mint=r['mint'], last_swap_age_s=round(now - r['at'], 1), last_slot=r['slot'])
        try:
            quote = None if r['quote'] is None else int(r['quote']) / 1e9
        except (TypeError, ValueError):
            quote = None
        if quote is None:
            no_reserve += 1
        else:
            item.update(real_quote_sol=round(quote, 3), liq_usd=round(quote * sol, 2) if sol else None)
        if quote is not None and quote >= MIN_REAL_QUOTE_SOL:
            liquid.append(item)
        new, old = len(r['new']), len(r['old'])
        if new >= MIN_BUYERS and new >= GROWTH * old:
            growth.append(dict(item, buyers_30m=new, buyers_prior_30m=old))
    liquid.sort(key=lambda i: -i['real_quote_sol'])
    growth.sort(key=lambda i: (-i['buyers_30m'], i['pool']))
    return dict(state='known', as_of=now, pools_seen=len(pools), newest_reserve_missing=no_reserve, sol_known=sol is not None, thresholds=dict(min_real_quote_sol=MIN_REAL_QUOTE_SOL, min_buyers=MIN_BUYERS, growth=GROWTH, window_s=WINDOW_S),
                graduated_liquid_n=len(liquid), buyer_growth_n=len(growth), graduated_liquid=liquid[:MAX_ITEMS], buyer_growth=growth[:MAX_ITEMS],
                note='Reported, not a gate and not a prediction. Stored rows only; absence from a list says nothing. A funder or buyer count is not control.')


def write(desk_dir, path, now):
    """Off the tool path: the read took 38 s on the live tape (2026-10-31), far too long inside a seat call. Atomic replace; a failed build leaves its typed unknown in the file."""
    out = build(desk_dir, now)
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(out))
    tmp.replace(path)
    return out


def read(path, now):
    """The last written list with its age. Missing, unreadable or older than MAX_AGE_S is unknown: an old list is not a current one."""
    try:
        out = json.loads(Path(path).read_text())
        age = now - out['as_of']
    except (OSError, ValueError, KeyError, TypeError):
        return dict(state='unknown', reason='lists_unavailable', graduated_liquid=[], buyer_growth=[])
    if not 0 <= age <= MAX_AGE_S:
        return dict(state='unknown', reason='lists_stale', age_s=age, graduated_liquid=[], buyer_growth=[])
    return dict(out, age_s=round(age, 1))
