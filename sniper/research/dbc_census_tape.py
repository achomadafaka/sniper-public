"""DBC shadow census tape schema (design signed off by Gamma 2026-10-23). Schema + summary only.

No process, subscription or provider call lives here. The census worker (not built) writes through
`insert_swap`; reads go through `summary`. Unknown is NULL, never 0: a field the decoder could not read,
a bucket after a stopped run, a quote class with no known quote mint.
"""
import sqlite3
import time

WSOL = 'So11111111111111111111111111111111111111112'
SYS_DEFAULT = '11111111111111111111111111111111'   # default key: unset OR native; never read as SOL

SCHEMA = """
CREATE TABLE IF NOT EXISTS run(
  run_id TEXT PRIMARY KEY, started_ts REAL NOT NULL, window_s REAL NOT NULL,
  source_filter TEXT NOT NULL,        -- the subscription filter, stated; census = sample of it
  stop_reason TEXT, stop_ts REAL,     -- NULL = still running / clean end at window_s
  dup_ignored INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS swaps(
  run_id TEXT NOT NULL, sig TEXT NOT NULL, ordinal INTEGER NOT NULL,
  slot INTEGER, tx_index INTEGER, block_time REAL, arrival_ts REAL NOT NULL,
  slot_lag_s REAL,                    -- arrival_ts - block_time; corrected-clock caveat, not our lag
  program_id TEXT, pool TEXT, config TEXT, mint TEXT,
  quote_mint TEXT, quote_decimals INTEGER, base_decimals INTEGER,
  direction TEXT, amount_in TEXT, amount_out TEXT, fee_json TEXT,
  commitment TEXT, err TEXT,          -- err NULL = unknown, '' = success, text = failed
  pool_create_slot INTEGER, is_first_swap_for_pool INTEGER,
  PRIMARY KEY(sig, ordinal));
CREATE TABLE IF NOT EXISTS tx_minutes(   -- every DBC tx the subscription delivered, swap or not, for the coverage check
  run_id TEXT NOT NULL, minute INTEGER NOT NULL, txs INTEGER NOT NULL, failed INTEGER NOT NULL,
  PRIMARY KEY(run_id, minute));
CREATE TABLE IF NOT EXISTS drops(run_id TEXT NOT NULL, ts REAL NOT NULL, n INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS skips(run_id TEXT NOT NULL, ts REAL NOT NULL, reason TEXT NOT NULL);
"""


def connect(path):
    c = sqlite3.connect(path, timeout=30)
    c.execute('PRAGMA journal_mode=WAL')   # the resolver thread writes through its own connection
    c.executescript(SCHEMA)
    return c


def start_run(c, run_id, started_ts, window_s, source_filter):
    c.execute('INSERT INTO run(run_id,started_ts,window_s,source_filter) VALUES(?,?,?,?)',
              (run_id, started_ts, window_s, source_filter))
    c.commit()


def stop_run(c, run_id, stop_reason, stop_ts):
    c.execute('UPDATE run SET stop_reason=?, stop_ts=? WHERE run_id=?', (stop_reason, stop_ts, run_id))
    c.commit()


def quote_class(quote_mint):
    """SOL only for WSOL. The default key is also the unset value in account layouts, so it is
    'other_or_unset' until the DBC quote layout is verified from a raw account. None = unread."""
    if quote_mint is None:
        return None
    if quote_mint == WSOL:
        return 'SOL'
    return 'other_or_unset' if quote_mint == SYS_DEFAULT else 'other'


COLS = ('slot', 'tx_index', 'block_time', 'program_id', 'pool', 'config', 'mint', 'quote_mint', 'quote_decimals',
        'base_decimals', 'direction', 'amount_in', 'amount_out', 'fee_json', 'commitment', 'err',
        'pool_create_slot', 'is_first_swap_for_pool')


def insert_swap(c, run_id, sig, ordinal, arrival_ts, **f):
    """Returns True if stored, False on a duplicate (sig, ordinal), which is counted. Absent field = NULL."""
    bad = set(f) - set(COLS)
    if bad:
        raise TypeError(f'unknown swap fields {sorted(bad)}')
    bt = f.get('block_time')
    lag = None if bt is None else arrival_ts - bt
    cur = c.execute(f'INSERT OR IGNORE INTO swaps(run_id,sig,ordinal,arrival_ts,slot_lag_s,{",".join(COLS)}) '
                    f'VALUES(?,?,?,?,?,{",".join("?" * len(COLS))})',
                    (run_id, sig, ordinal, arrival_ts, lag, *[f.get(k) for k in COLS]))
    if cur.rowcount == 0:
        c.execute('UPDATE run SET dup_ignored=dup_ignored+1 WHERE run_id=?', (run_id,))
    return cur.rowcount == 1


def count_tx(c, run_id, ts, failed):
    c.execute('INSERT INTO tx_minutes VALUES(?,?,1,?) ON CONFLICT(run_id,minute) DO UPDATE SET '
              'txs=txs+1, failed=failed+?', (run_id, int(ts // 60), int(bool(failed)), int(bool(failed))))


def record_drop(c, run_id, ts, n):
    c.execute('INSERT INTO drops VALUES(?,?,?)', (run_id, ts, n))


def record_skip(c, run_id, ts, reason):
    c.execute('INSERT INTO skips VALUES(?,?,?)', (run_id, ts, reason))


def summary(c, run_id, bucket_s, now=None):
    """One row per (bucket, config, quote_class). Buckets use arrival_ts, not block_time.
    Fields: swaps (failed included), buys, sells, failed, err_unknown (err NULL), pools, mints (distinct per
    bucket x config x class), notional_quote_net (quote units; NET of the input-side trading+protocol fee when the fee is taken on the input
    side: C1 sample 2026-10-23, 1 of 5 real swaps had vault delta = amount_in + fees; fee_json stays raw so gross is
    recomputable once the fee side is known; successful and unknown-err swaps only; NULL if any row lacks decimals or amount),
    dropped (drop-oldest count in bucket; >0 = swaps is a lower bound), partial (bucket holds stop_ts).
    NULL, never 0: buckets at/after stop_ts, and future buckets of a run still going (now < bucket start).
    A bucket with no swaps inside the observed window has swaps=0 and config/quote_class None."""
    now = time.time() if now is None else now
    started, window, stop_ts = c.execute('SELECT started_ts,window_s,stop_ts FROM run WHERE run_id=?',
                                         (run_id,)).fetchone()
    end_obs = stop_ts if stop_ts is not None else now
    nulls = dict(config=None, quote_class=None, swaps=None, buys=None, sells=None, failed=None,
                 err_unknown=None, pools=None, mints=None, notional_quote_net=None, dropped=None, partial=0)
    out = []
    for i in range(int(window // bucket_s)):
        lo, hi = started + i * bucket_s, started + (i + 1) * bucket_s
        if lo >= end_obs:
            out.append(dict(nulls, bucket_start=lo))
            continue
        partial = int(hi > end_obs)
        dropped = c.execute('SELECT COALESCE(SUM(n),0) FROM drops WHERE run_id=? AND ts>=? AND ts<?',
                            (run_id, lo, min(hi, end_obs))).fetchone()[0]
        agg = {}
        for cfg, qm, d, ai, ao, qd, err, pool, mint in c.execute(
                'SELECT config,quote_mint,direction,amount_in,amount_out,quote_decimals,err,pool,mint FROM swaps '
                'WHERE run_id=? AND arrival_ts>=? AND arrival_ts<?', (run_id, lo, hi)):
            a = agg.setdefault((cfg, quote_class(qm)), dict(swaps=0, buys=0, sells=0, failed=0, err_unknown=0,
                                                            pools=set(), mints=set(), notional=0.0))
            a['swaps'] += 1
            a['buys'] += d == 'buy'
            a['sells'] += d == 'sell'
            a['failed'] += bool(err)
            a['err_unknown'] += err is None
            a['pools'].add(pool)
            a['mints'].add(mint)
            q = ai if d == 'buy' else ao if d == 'sell' else None
            if a['notional'] is not None and not err:   # failed txs moved no funds
                a['notional'] = None if (q is None or qd is None) else a['notional'] + int(q) / 10 ** qd
        if not agg:
            out.append(dict(nulls, bucket_start=lo, swaps=0, buys=0, sells=0, failed=0, err_unknown=0, pools=0,
                            mints=0, dropped=dropped, partial=partial))
        for (cfg, qc), a in sorted(agg.items(), key=lambda kv: str(kv[0])):
            out.append(dict(bucket_start=lo, config=cfg, quote_class=qc, swaps=a['swaps'], buys=a['buys'],
                            sells=a['sells'], failed=a['failed'], err_unknown=a['err_unknown'],
                            pools=len(a['pools'] - {None}), mints=len(a['mints'] - {None, ''}),
                            notional_quote_net=a['notional'], dropped=dropped, partial=partial))
    return out
