#!/usr/bin/env python3
"""Keep every armed-wallet EVM buy's pool ticks for 20 minutes, before the one-hour tape forgets them.

Alpha's order 2026-09-28 (operator design "catch the copier drop while the source still holds", EVM
leg). data/memecoin_onchain_volume.db `pool_ticks` carries per-swap price (sqrt_price_x96), maker and
block for 4663, but retains about one hour. For each armed-wallet buy on --chain (every entry attempt,
filled or refused, one signal per source transaction and mint), this copies that mint's pools' ticks
from the source's block to source + --window-s into a research database, with the block times, the
pool metadata and any tape gaps or feed resets inside the window, so the window can be graded later.

PRE-SIGNAL HOUR (2026-09-30): each signal also keeps its pools' ticks over [src_ts - 3600 s, src_block),
fixed at first sight; `partial_start_s` is the exact part of that hour the tape had already pruned
(pre_hour_complete: <= 90 s). Spec 2026-09-30-memecoin-pre-signal-path-recording-design.md.

The source block is the block of the source transaction's own tick in the tape (basis
`source_tick`). When the tape never saw it (a Relay envelope into an untracked pool), the window
starts at the block of decision time minus 30 s (basis `decision_time`). A window whose start is
older than the tape's oldest tick is flagged `partial_start`. A window closes (no more copying) once
its end is 60 s in the past.

V4 SIGN CUTOVER AT 2026-09-29T02:30:10Z. Rows are copied as the tape stores them. V4 rows (66-char
pool id, `kind='v3_swap'`) carry PoolManager's SWAPPER sign when `observed_at` is before
2026-09-29T02:30:10Z: a buy of the coin has a NEGATIVE quote amount. They carry the house into-pool
sign from that time on: positive = into the pool, so a buy is positive, as for V3, V2 and Pons.
Lambda 05293598e was deployed in e19ab922; both volume workers restarted at 02:30:10Z. Any reader
that signs amounts must split V4 rows at that `observed_at`, or flip the earlier ones. Readers that
use absolute values or sqrt_price_x96 are unaffected. Measured on the tape: before the cutover a
positive V4 quote went with a falling coin price on 1,090 of 1,091 8453 swaps; after it, 4663 reads
into-pool.

Read-only on the tape and the ledger. Writes only --db. One run is one pass; a lock file makes an
overlapping run exit at once. Meant for cron every minute:

    * * * * * cd /home/bigpapi/Desktop/Sniper && python3 scripts/research/evm_signal_window_recorder.py >> $HOME/logs/evm_signal_windows.log 2>&1

    python3 scripts/research/evm_signal_window_recorder.py [--chain 4663] [--db data/research_signal_windows.db]
    python3 scripts/research/evm_signal_window_recorder.py --selftest
"""
import argparse
import contextlib
import fcntl
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from sniper.util.state_paths import state_root  # noqa: E402  (stdlib-only helper; no funded imports)

# State lives under SNIPER_STATE_ROOT (default: this checkout), so a copy pinned in a release still
# reads the live ledger and tape and writes the live research database.
ROOT = state_root()
LEDGER = ROOT / "sniper_tracker.db"
TAPE = ROOT / "data" / "memecoin_onchain_volume.db"
TX = re.compile(r"0x[0-9a-fA-F]{64}")
CLOSE_GRACE_S = 60
# THE PRE-SIGNAL HOUR (2026-09-30, spec 2026-09-30-memecoin-pre-signal-path-recording-design.md, Gamma
# §3a, Alpha: option (a)). Each signal also keeps [src_ts - 3600 s, src_block) of its pools. The tape
# prunes rows older than 3,600 s every 60 s and this cron runs once a minute, so the hour is fixed at
# first sight and whatever was already pruned is recorded in `partial_start_s`, never guessed. A
# window short by more than PRE_FULL_MAX_S seconds is C0 for the full-hour chart categories.
PRE_WINDOW_S = 3600.0
PRE_FULL_MAX_S = 90.0
_PRE_COLUMNS = (("pre_block", "INTEGER"), ("pre_from_ts", "REAL"), ("partial_start_s", "REAL"),
                ("pre_ticks_n", "INTEGER"))
PRE_DECISION_S = 30
TICK_COLS = ("chain_id", "pool", "kind", "block_no", "tx_sig", "log_index", "maker", "amount0", "amount1",
             "observed_at", "tick", "tick_lower", "tick_upper", "liquidity", "sqrt_price_x96", "block_hash")
DDL = """
CREATE TABLE IF NOT EXISTS signals(
  signal_key TEXT PRIMARY KEY, chain_id TEXT NOT NULL, mint TEXT NOT NULL, source_tx TEXT,
  source_user_id TEXT, source_handle TEXT, first_decided_at TEXT NOT NULL, attempts_json TEXT NOT NULL,
  src_block INTEGER, src_block_basis TEXT, src_ts REAL, end_ts REAL, pools_json TEXT,
  partial_start INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0, ticks_n INTEGER,
  last_copied_at TEXT, pre_block INTEGER, pre_from_ts REAL, partial_start_s REAL, pre_ticks_n INTEGER);
CREATE TABLE IF NOT EXISTS ticks(
  tape_id INTEGER, chain_id TEXT NOT NULL, pool TEXT NOT NULL, kind TEXT NOT NULL, block_no INTEGER,
  tx_sig TEXT, log_index INTEGER, maker TEXT, amount0 TEXT, amount1 TEXT, observed_at TEXT NOT NULL,
  tick INTEGER, tick_lower INTEGER, tick_upper INTEGER, liquidity TEXT, sqrt_price_x96 TEXT, block_hash TEXT,
  UNIQUE(chain_id, tx_sig, log_index, kind));
CREATE INDEX IF NOT EXISTS ticks_pool_block ON ticks(chain_id, pool, block_no);
CREATE TABLE IF NOT EXISTS block_times(chain_id TEXT NOT NULL, block_no INTEGER NOT NULL, block_ts TEXT NOT NULL,
  block_hash TEXT, PRIMARY KEY(chain_id, block_no));
CREATE TABLE IF NOT EXISTS pool_meta(chain_id TEXT NOT NULL, pool TEXT NOT NULL, meta_json TEXT NOT NULL,
  PRIMARY KEY(chain_id, pool));
CREATE TABLE IF NOT EXISTS tick_gaps(chain_id TEXT NOT NULL, from_block INTEGER NOT NULL, to_block INTEGER NOT NULL,
  reason TEXT NOT NULL, detected_at TEXT NOT NULL, resolved_at TEXT, UNIQUE(chain_id, from_block, to_block, reason));
CREATE TABLE IF NOT EXISTS feed_resets(chain_id TEXT, at REAL, reason TEXT, window_s REAL, UNIQUE(chain_id, at, reason));
CREATE TABLE IF NOT EXISTS runs(at TEXT NOT NULL, chain_id TEXT NOT NULL, open_n INTEGER, new_signals INTEGER,
  ticks_upserted INTEGER, closed_now INTEGER, note TEXT);
"""


def epoch(iso):
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()


def iso_s(ts):
    """The tape's block_ts format, so a string bound compares correctly: 2026-09-28T12:01:14+00:00."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def signals_from_ledger(led, chain, since_ts):
    """{signal_key: {...}} for every buy attempt on `chain` decided since `since_ts`; one signal per
    (source tx, mint). An origin with no transaction hash keys on the origin id itself."""
    out = {}
    for aid, origin, mint, uid, handle, decided, status, reason in led.execute(
            "SELECT attempt_id, origin_event_id, mint, source_user_id, source_handle, decision_started_at, "
            "terminal_status, terminal_reason FROM memecoin_entry_attempts WHERE network_id=? AND side='buy' "
            "AND decision_started_at>=? ORDER BY decision_started_at", (chain, iso_s(since_ts - 1)[:19])):
        if not mint or epoch(decided) < since_ts:
            continue
        m = TX.search(str(origin or ""))
        tx = m.group(0).lower() if m else None
        key = "%s:%s:%s" % (chain, tx or origin, mint.lower())
        s = out.setdefault(key, {"signal_key": key, "chain_id": chain, "mint": mint.lower(), "source_tx": tx,
                                 "source_user_id": uid, "source_handle": handle, "first_decided_at": decided,
                                 "attempts": []})
        s["attempts"].append({"attempt_id": aid, "status": status, "reason": (reason or "")[:160], "at": decided})
    return out


def locate(tape, sig, now):
    """(src_block, basis, src_ts, partial_start) for a signal, from the tape."""
    chain = sig["chain_id"]
    block = None
    if sig["source_tx"]:
        block = tape.execute("SELECT MIN(block_no) FROM pool_ticks WHERE chain_id=? AND tx_sig=?",
                             (chain, sig["source_tx"])).fetchone()[0]
    basis = "source_tick" if block is not None else "decision_time"
    if block is None:
        row = tape.execute("SELECT block_no FROM block_times WHERE chain_id=? AND block_ts<=? "
                           "ORDER BY block_ts DESC, block_no DESC LIMIT 1",
                           (chain, iso_s(epoch(sig["first_decided_at"]) - PRE_DECISION_S))).fetchone()
        block = row[0] if row else None
    if block is None:
        return None, basis, None, 1
    ts_row = tape.execute("SELECT block_ts FROM block_times WHERE chain_id=? AND block_no<=? "
                          "ORDER BY block_no DESC LIMIT 1", (chain, block)).fetchone()
    src_ts = epoch(ts_row[0]) if ts_row else epoch(sig["first_decided_at"]) - (PRE_DECISION_S if basis == "decision_time" else 0)
    oldest = tape.execute("SELECT MIN(block_no) FROM pool_ticks WHERE chain_id=?", (chain,)).fetchone()[0]
    return block, basis, src_ts, int(oldest is None or oldest > block)


def pre_hour_complete(partial_start_s):
    """True when the pre-signal hour is short by at most PRE_FULL_MAX_S seconds (Gamma §3a)."""
    return partial_start_s is not None and partial_start_s <= PRE_FULL_MAX_S


def locate_pre(tape, chain, src_ts):
    """(pre_block, pre_from_ts, partial_start_s) for the hour before `src_ts`, from the tape's own block
    times. A tick is observed no earlier than its block, and the tape prunes ticks by observation time
    and block times by block time, so a block the tape still times still holds its ticks."""
    want = src_ts - PRE_WINDOW_S
    row = tape.execute("SELECT MIN(block_no) FROM block_times WHERE chain_id=? AND block_ts>=?",
                       (chain, iso_s(want))).fetchone()
    first = row[0] if row else None
    oldest = tape.execute("SELECT block_no, block_ts FROM block_times WHERE chain_id=? "
                          "ORDER BY block_no LIMIT 1", (chain,)).fetchone()
    if first is None or oldest is None:
        return None, want, PRE_WINDOW_S
    if oldest[0] >= first:                        # the tape no longer times the hour's first block
        return oldest[0], want, max(0.0, epoch(oldest[1]) - want)
    return first, want, 0.0


def copy_window(tape, out, sig, row, now):
    """Upsert the window's ticks, block times, pool meta, gaps and resets. Returns ticks upserted."""
    chain, src_block, end_ts = sig["chain_id"], row["src_block"], row["end_ts"]
    pools = [p for (p,) in tape.execute("SELECT pool FROM onchain_pool_protocols WHERE chain_id=? AND mint=?",
                                        (chain, sig["mint"]))]
    end_row = tape.execute("SELECT MAX(block_no) FROM block_times WHERE chain_id=? AND block_ts<=?",
                           (chain, iso_s(end_ts))).fetchone()
    end_block = end_row[0] if end_row and end_row[0] is not None and end_ts <= now else None
    n = 0
    cols = ",".join(TICK_COLS)
    upsert = ("INSERT INTO ticks(tape_id,%s) VALUES (?,%s) ON CONFLICT(chain_id, tx_sig, log_index, kind) DO UPDATE SET "
              "block_hash=COALESCE(excluded.block_hash, ticks.block_hash), "
              "sqrt_price_x96=COALESCE(excluded.sqrt_price_x96, ticks.sqrt_price_x96), "
              "liquidity=COALESCE(excluded.liquidity, ticks.liquidity), tick=COALESCE(excluded.tick, ticks.tick), "
              "block_no=COALESCE(excluded.block_no, ticks.block_no)") % (cols, ",".join("?" * len(TICK_COLS)))
    blocks = set()
    lower = row["pre_block"] if row.get("pre_block") is not None else src_block
    for pool in pools:
        q = "SELECT id,%s FROM pool_ticks WHERE chain_id=? AND pool=? AND block_no>=?" % cols
        args = [chain, pool, lower]
        if end_block is not None:
            q += " AND block_no<=?"
            args.append(end_block)
        for r in tape.execute(q, args):
            out.execute(upsert, r)
            blocks.add(r[4])
            n += 1
        meta = tape.execute("SELECT * FROM pool_meta WHERE chain_id=? AND pool=?", (chain, pool))
        names = [d[0] for d in meta.description]
        m = meta.fetchone()
        if m:
            out.execute("INSERT OR REPLACE INTO pool_meta VALUES (?,?,?)", (chain, pool, json.dumps(dict(zip(names, m)))))
    for b in sorted(blocks):
        bt = tape.execute("SELECT block_no, block_ts, block_hash FROM block_times WHERE chain_id=? AND block_no=?",
                          (chain, b)).fetchone()
        if bt:
            out.execute("INSERT INTO block_times(chain_id, block_no, block_ts, block_hash) VALUES (?,?,?,?) "
                        "ON CONFLICT(chain_id, block_no) DO UPDATE SET "
                        "block_hash=COALESCE(excluded.block_hash, block_times.block_hash)", (chain, *bt))
    hi = end_block if end_block is not None else 1 << 62
    for g in tape.execute("SELECT chain_id, from_block, to_block, reason, detected_at, resolved_at FROM tick_gaps "
                          "WHERE chain_id=? AND from_block<=? AND to_block>=?", (chain, hi, lower)):
        out.execute("INSERT INTO tick_gaps VALUES (?,?,?,?,?,?) ON CONFLICT(chain_id, from_block, to_block, reason) "
                    "DO UPDATE SET resolved_at=excluded.resolved_at", g)
    for fr in tape.execute("SELECT chain_id, at, reason, window_s FROM volume_feed_resets WHERE chain_id=? AND at>=? "
                           "AND at<=?", (chain, row.get("pre_from_ts") or row["src_ts"], end_ts)):
        out.execute("INSERT OR IGNORE INTO feed_resets VALUES (?,?,?,?)", fr)
    return n, pools, end_block is not None


def run_once(led, tape, out, chain, now, window_s=600, lookback_s=4200, full=False):
    """One pass. `full` (the database is at its size cap) admits no NEW signal; open windows finish."""
    out.executescript(DDL)
    have = {r[1] for r in out.execute("PRAGMA table_info(signals)")}
    for name, kind in _PRE_COLUMNS:               # a database from before the pre-signal hour
        if name not in have:
            out.execute("ALTER TABLE signals ADD COLUMN %s %s" % (name, kind))
    sigs = signals_from_ledger(led, chain, now - lookback_s)
    known = {r[0]: r for r in out.execute("SELECT signal_key, closed FROM signals WHERE chain_id=?", (chain,))}
    new = upserted = closed_now = open_n = skipped_full = 0
    for key, sig in sigs.items():
        if key in known and known[key][1]:
            continue
        if key not in known and full:
            skipped_full += 1                     # counted: a full database must not read as a quiet chain
            continue
        if key not in known:
            src_block, basis, src_ts, partial = locate(tape, sig, now)
            pre_block, pre_from, pre_short = (locate_pre(tape, chain, src_ts) if src_block is not None
                                              else (None, None, None))
            out.execute("INSERT INTO signals(signal_key, chain_id, mint, source_tx, source_user_id, source_handle, "
                        "first_decided_at, attempts_json, src_block, src_block_basis, src_ts, end_ts, partial_start, "
                        "pre_block, pre_from_ts, partial_start_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (key, chain, sig["mint"], sig["source_tx"], sig["source_user_id"], sig["source_handle"],
                         sig["first_decided_at"], json.dumps(sig["attempts"]), src_block, basis, src_ts,
                         (src_ts + window_s) if src_ts is not None else None, partial,
                         pre_block, pre_from, pre_short))
            new += 1
        else:
            out.execute("UPDATE signals SET attempts_json=? WHERE signal_key=?", (json.dumps(sig["attempts"]), key))
        row = dict(zip(("src_block", "src_ts", "end_ts", "pre_block", "pre_from_ts"), out.execute(
            "SELECT src_block, src_ts, end_ts, pre_block, pre_from_ts FROM signals WHERE signal_key=?",
            (key,)).fetchone()))
        if row["src_block"] is None:
            out.execute("UPDATE signals SET closed=1, ticks_n=0 WHERE signal_key=?", (key,))
            closed_now += 1
            continue
        n, pools, bounded = copy_window(tape, out, sig, row, now)
        upserted += n
        done = bounded and now >= row["end_ts"] + CLOSE_GRACE_S
        ticks_n = out.execute("SELECT COUNT(*) FROM ticks WHERE chain_id=? AND pool IN (%s) AND block_no>=? "
                              % ",".join("?" * len(pools)) + ("AND block_no<=(SELECT MAX(block_no) FROM block_times "
                                                              "WHERE chain_id=? AND block_ts<=?)" if bounded else ""),
                              [chain, *pools, row["src_block"], *([chain, iso_s(row["end_ts"])] if bounded else [])]
                              ).fetchone()[0] if pools else 0
        pre_n = out.execute("SELECT COUNT(*) FROM ticks WHERE chain_id=? AND pool IN (%s) AND block_no>=? "
                            "AND block_no<?" % ",".join("?" * len(pools)),
                            [chain, *pools, row["pre_block"], row["src_block"]]).fetchone()[0] \
            if pools and row["pre_block"] is not None else 0
        out.execute("UPDATE signals SET pools_json=?, ticks_n=?, pre_ticks_n=?, closed=?, last_copied_at=? "
                    "WHERE signal_key=?", (json.dumps(pools), ticks_n, pre_n, int(done), iso_s(now), key))
        closed_now += done
        open_n += not done
    out.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)", (iso_s(now), chain, open_n, new, upserted, closed_now,
                                                           "size_cap_reached" if full else None))
    out.commit()
    return {"open": open_n, "new": new, "ticks_upserted": upserted, "closed_now": closed_now,
            "full": bool(full), "skipped_full": skipped_full}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default="4663")
    ap.add_argument("--db", default=str(ROOT / "data" / "research_signal_windows.db"))
    ap.add_argument("--window-s", type=float, default=1200.0)   # D (dead) needs decision + 900 s
    ap.add_argument("--max-mb", type=float, default=3000.0,
                    help="stop admitting new signals above this database size (measured ~0.9 KB/tick, "
                         "~80-400 MB/day on 4663 at 83-334 buy origins/day with the 1200 s window)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    lock = open(a.db + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("%s another recorder run holds the lock; exiting" % iso_s(time.time()))
        return 0
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % LEDGER, uri=True, timeout=30)) as led, \
            contextlib.closing(sqlite3.connect("file:%s?mode=ro" % TAPE, uri=True, timeout=30)) as tape, \
            contextlib.closing(sqlite3.connect(a.db, timeout=30)) as out:
        size = sum(Path(a.db + x).stat().st_size for x in ("", "-wal") if Path(a.db + x).exists())
        res = run_once(led, tape, out, a.chain, time.time(), a.window_s, full=size > a.max_mb * 1e6)
    print("%s chain=%s %s" % (iso_s(time.time()), a.chain, json.dumps(res)))
    return 0


def selftest():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        led, tape, out = (sqlite3.connect(str(Path(d) / n)) for n in ("l.db", "t.db", "o.db"))
        led.execute("CREATE TABLE memecoin_entry_attempts(attempt_id, origin_event_id, mint, source_user_id, "
                    "source_handle, decision_started_at, terminal_status, terminal_reason, network_id, side)")
        tape.executescript("""
          CREATE TABLE pool_ticks(id INTEGER PRIMARY KEY, chain_id, pool, kind, block_no, tx_sig, log_index, maker,
            amount0, amount1, observed_at, tick, tick_lower, tick_upper, liquidity, sqrt_price_x96, block_hash);
          CREATE TABLE block_times(chain_id, block_no, block_ts, fetched_at, block_hash);
          CREATE TABLE onchain_pool_protocols(chain_id, pool, protocol, mint);
          CREATE TABLE pool_meta(chain_id, pool, token0, token1, quote_side);
          CREATE TABLE tick_gaps(chain_id, from_block, to_block, reason, detected_at, resolved_at);
          CREATE TABLE volume_feed_resets(chain_id, at, reason, window_s);""")
        t0 = 1_800_000_000.0                       # source block 100 at t0; one block per second
        src_tx, other_tx = "0x" + "a" * 64, "0x" + "b" * 64
        for b in range(90, 800):
            tape.execute("INSERT INTO block_times(chain_id, block_no, block_ts, fetched_at, block_hash) "
                         "VALUES ('4663',?,?,?,NULL)", (b, iso_s(t0 + b - 100), "x"))
        tape.execute("INSERT INTO onchain_pool_protocols VALUES ('4663','0xpool','v4','0xmint')")
        tape.execute("INSERT INTO pool_meta VALUES ('4663','0xpool','0x0','0xmint',0)")
        ticks = [(95, "0xpre"), (100, src_tx), (150, "0xmid"), (700, "0xin"), (701, "0xout")]
        for b, tx in ticks:
            tape.execute("INSERT INTO pool_ticks(chain_id,pool,kind,block_no,tx_sig,log_index,observed_at,sqrt_price_x96) "
                         "VALUES ('4663','0xpool','v3_swap',?,?,0,'x','79228162514264337593543950336')", (b, tx))
        tape.execute("INSERT INTO tick_gaps VALUES ('4663',140,160,'stall','x',NULL)")
        led.execute("INSERT INTO memecoin_entry_attempts VALUES ('a1',?, '0xMINT','u','h',?, 'refused','h1 floor','4663','buy')",
                    ("swap:raybot:%s:0xtrader:0xmint" % src_tx, datetime.fromtimestamp(t0 + 1, timezone.utc).isoformat()))
        led.execute("INSERT INTO memecoin_entry_attempts VALUES ('a2',?, '0xmint','u','h',?, 'refused','dup','4663','buy')",
                    ("onchain:4663:%s:0xmint" % src_tx, datetime.fromtimestamp(t0 + 1.2, timezone.utc).isoformat()))
        led.execute("INSERT INTO memecoin_entry_attempts VALUES ('a3','swap:uuid-1','0xmint2','u','h',?, 'refused','x','4663','buy')",
                    (datetime.fromtimestamp(t0 + 5, timezone.utc).isoformat(),))
        r1 = run_once(led, tape, out, "4663", now=t0 + 60, window_s=600)
        assert r1["new"] == 2, r1                                   # a1+a2 are one signal; a3 has no pools
        s = out.execute("SELECT src_block, src_block_basis, closed FROM signals WHERE source_tx=?", (src_tx,)).fetchone()
        assert s == (100, "source_tick", 0), s
        s3 = out.execute("SELECT src_block_basis, src_block FROM signals WHERE mint='0xmint2'").fetchone()
        assert s3 == ("decision_time", None), s3                   # decision - 30 s precedes the tape: unlocatable
        blocks = sorted(b for (b,) in out.execute("SELECT block_no FROM ticks"))
        assert blocks == [95, 100, 150, 700, 701], blocks           # window still open: no upper bound yet
        pre = out.execute("SELECT pre_block, partial_start_s, pre_ticks_n FROM signals WHERE source_tx=?",
                          (src_tx,)).fetchone()
        assert pre == (90, 3590.0, 1), pre                          # the tape times only 10 s of the hour
        tape.execute("INSERT INTO pool_ticks(chain_id,pool,kind,block_no,tx_sig,log_index,observed_at) "
                     "VALUES ('4663','0xpool','v3_swap',702,'0xlate',0,'x')")   # lands after the window's end
        r2 = run_once(led, tape, out, "4663", now=t0 + 600 + CLOSE_GRACE_S + 1, window_s=600)
        assert r2["new"] == 0 and r2["closed_now"] >= 1, r2
        s = out.execute("SELECT closed, ticks_n FROM signals WHERE source_tx=?", (src_tx,)).fetchone()
        assert s == (1, 3), s                                       # 100, 150, 700 inside; 701 past the end
        assert out.execute("SELECT COUNT(*) FROM tick_gaps").fetchone()[0] == 1
        assert out.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 5      # idempotent; 702 is past the end
        r3 = run_once(led, tape, out, "4663", now=t0 + 700, window_s=600)
        assert r3["ticks_upserted"] == 0, r3                         # closed windows are not re-read
        led.execute("INSERT INTO memecoin_entry_attempts VALUES ('a4',?, '0xmint','u','h',?, 'refused','x','4663','buy')",
                    ("swap:raybot:0x%s:0xt:0xmint" % ("c" * 64), datetime.fromtimestamp(t0 + 690, timezone.utc).isoformat()))
        at_cap = run_once(led, tape, out, "4663", now=t0 + 700, window_s=600, full=True)
        assert (at_cap["new"], at_cap["full"], at_cap["skipped_full"]) == (0, True, 1), at_cap   # at the cap
        assert run_once(led, tape, out, "4663", now=t0 + 700, window_s=600)["new"] == 1
    print("selftest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
