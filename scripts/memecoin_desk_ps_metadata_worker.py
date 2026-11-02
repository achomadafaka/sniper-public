#!/usr/bin/env python3
"""PumpSwap metadata worker (operator order via Alpha, 2026-10-22). Its own process, nice 19, never on the fill path.

For each canonical pool already mapped (create_pool or curve-derived binding) in the PS tape (data/desk/pumpswap/tape.db, read-only) it makes ONE Helius research-key
getAccountInfo (jsonParsed) on the BASE mint (decimals, supply, token program) and writes it ONCE to
data/desk/pumpswap/metadata.db with metadata_known_at = the receipt time, the context slot and the source. The pool account is read
only with MEMECOIN_DESK_PS_META_POOL_ACCOUNT=1 (default 0) and kept as raw base64, undecoded. Nothing is guessed: a failed read is
unknown (a failures row), never 0.

Limits: the research key's shared pacer (SharedPacer.for_url), per-minute and per-day caps that SURVIVE restarts (calls table),
only mappings observed within the indexed scan/replay windows are eligible; original map_known_at before the first start is never read.
The historical pre_deploy_unread count is unknown; its separately named observed lower bound covers only cached witnessed mappings.
Pages use the received_at index, a persisted event cursor and a 30-second replay (older late inserts are outside coverage), at most 3 failures per key then
abandoned, and the FIRST 429 or error stops the worker for a cool-down (gap row; stop_until persisted) before the watchdog's next
start may read again. Knob: MEMECOIN_DESK_PS_METADATA_ENABLED (default 0: exits at once).
    python3 scripts/memecoin_desk_ps_metadata_worker.py [--once]
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.services import memecoin_desk_ps_metadata as MD  # noqa: E402

PER_MIN = 20
PER_DAY = 1000
MAX_FAILURES = 3
STOP_429_S = 900.0
STOP_ERROR_S = 300.0
POLL_S = 5.0
WSOL = "So11111111111111111111111111111111111111112"


class Rpc:
    def __init__(self, url, pacer, *, timeout_s=5.0):
        self.url, self.pacer, self.timeout_s = url, pacer, timeout_s

    def __call__(self, method, params, gate=None):
        """(result, None) or (None, error slug). One paced call, no retry. `gate()` runs AFTER the pacer delay, immediately before
        the send: it charges the durable budget and returns False when the cap is spent ("budget_exhausted", nothing sent)."""
        self.pacer.acquire()
        if gate is not None and not gate():
            return None, "budget_exhausted"
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                reply = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                self.pacer.pause(60.0)
                return None, "rate_limited"
            return None, "http_%d" % exc.code
        except (TimeoutError, OSError):
            return None, "timeout"
        except ValueError:
            return None, "malformed"
        if not isinstance(reply, dict) or "error" in reply or "result" not in reply:
            err = reply.get("error") if isinstance(reply, dict) else None
            if isinstance(err, dict) and err.get("code") == 429:
                self.pacer.pause(60.0)
                return None, "rate_limited"
            return None, "rpc_error"
        return reply["result"], None


def ro(path,*,timeout=10):
    p = Path(path)
    return sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=timeout) if p.exists() else None


def priority_only_mode(env):
    return env.get('MEMECOIN_LARGE_BUYER_ENABLED','0').strip()=='1' and env.get('MEMECOIN_DESK_ENABLED','0').strip()=='0'


def first_reach_priority(arm_path=None,*,now):
    """Bounded observational lookup priority, never funded qualification or absence proof."""
    unknown=lambda reason:dict(state='unknown',reason=reason,mints=[],truncated=False)
    if arm_path is None:return dict(state='not_requested',reason=None,mints=[],truncated=False)
    if type(now) not in (int,float) or not math.isfinite(now):return unknown('priority_clock_unknown')
    conn=None
    try:
        conn=ro(arm_path,timeout=.05)
        if conn is None:return unknown('arm_missing')
        conn.row_factory=sqlite3.Row
        deadline=time.monotonic()+.05
        conn.set_progress_handler(lambda:time.monotonic()>=deadline,100)
        rows=conn.execute('SELECT pool,wallet,mint,signature,trigger_at,detected_at,detect_lag_s,detection_state,source_slot,source_tx_index,source_ordinal,source_signature FROM triggers ORDER BY rowid DESC LIMIT 128').fetchall()
        mints=[];unproven=0
        for row in rows:
            at,detected,lag=row['trigger_at'],row['detected_at'],row['detect_lag_s']
            if type(at) not in (int,float) or not math.isfinite(at):unproven+=1;continue
            if not 0<=now-at<60:continue
            if (row['detection_state']!='known' or any(type(v) not in (int,float) or not math.isfinite(v) for v in (detected,lag))
                    or not 0<=lag<=5 or detected>now or abs(detected-at-lag)>1e-6
                    or type(row['source_slot']) is not int or row['source_slot']<=0
                    or type(row['source_tx_index']) is not int or row['source_tx_index']<=0
                    or type(row['source_ordinal']) is not int or row['source_ordinal']<0
                    or not isinstance(row['source_signature'],str) or not row['source_signature'] or row['source_signature']!=row['signature']):
                unproven+=1;continue
            reach=conn.execute('SELECT crowd_state FROM reaches WHERE pool=? AND at=? AND wallet=?',(row['pool'],at,row['wallet'])).fetchone()
            if reach is None or reach[0]!='known':unproven+=1;continue
            mint=row['mint']
            if not isinstance(mint,str) or not mint:unproven+=1;continue
            if mint not in mints and len(mints)<64:mints.append(mint)
        partial=len(rows)==128 or unproven>0
        return dict(state='partial' if partial else 'known',reason='bounded_tail_or_unproven_rows' if partial else None,mints=mints,truncated=len(rows)==128)
    except (OSError,sqlite3.Error):
        return unknown('priority_source_unreadable')
    finally:
        if conn is not None:conn.close()


def get_state(conn, key, default=None):
    row = conn.execute("SELECT v FROM state WHERE k=?", (key,)).fetchone()
    return default if row is None else row[0]


def set_state(conn, key, value):
    conn.execute("INSERT INTO state(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
    conn.commit()


def budget_left(conn, now):
    minute = conn.execute("SELECT COUNT(*) FROM calls WHERE at > ?", (now - 60,)).fetchone()[0]
    day = conn.execute("SELECT COUNT(*) FROM calls WHERE at > ?", (now - 86400,)).fetchone()[0]
    return PER_MIN - minute, PER_DAY - day


class ScanBudgetInterrupted(Exception):
    """An unfinished local page; its returned prefix has already been committed."""
    def __init__(self, rows_read=0):
        self.rows_read=rows_read
        super().__init__('scan_budget')


SCAN_WINDOW_S = 3600.0
RECHECK_S = 30.0
SCAN_BATCH = 2000
SCAN_MIN_BATCH = 16
SCAN_BUDGET_S = 0.25
SCAN_SQL = ("SELECT mint,pool,map_known_at,received_at,signature,ordinal,map_status,quote_mint "
            "FROM swaps_ps INDEXED BY swaps_ps_received WHERE received_at>=? AND received_at<? "
            "AND (received_at,signature,ordinal)>(?,?,?) ORDER BY received_at,signature,ordinal LIMIT ?")
_SCAN_DDL = (
    "CREATE TABLE IF NOT EXISTS observed_mappings(mint TEXT NOT NULL,pool TEXT NOT NULL,known_at REAL NOT NULL,PRIMARY KEY(mint,pool))",
    "CREATE TABLE IF NOT EXISTS mapping_cursor(id INTEGER PRIMARY KEY CHECK(id=1),received_at REAL NOT NULL,signature TEXT NOT NULL,"
    "ordinal INTEGER NOT NULL,complete INTEGER NOT NULL,through_at REAL NOT NULL,source_identity TEXT NOT NULL)",
)


def refresh_mappings(tape, meta, *, now):
    """One bounded indexed page, cached mappings and cursor committed together.

    Replay the last 30 s after exhausting a pass; older late inserts are outside this reader's coverage.
    The original mapping witness time remains authoritative, never the scan receipt or window minimum.
    """
    for sql in _SCAN_DDL:
        meta.execute(sql)
    meta.commit()
    if type(now) not in (int,float) or not math.isfinite(now):
        raise ValueError('scan_clock_unknown')
    path=tape.execute('PRAGMA database_list').fetchone()[2]
    stat=Path(path).stat();identity='%s:%s'%(stat.st_dev,stat.st_ino)
    previous=meta.execute('SELECT received_at,signature,ordinal,complete,through_at,source_identity FROM mapping_cursor WHERE id=1').fetchone()
    floor=now-SCAN_WINDOW_S
    reset=previous is None or previous[5]!=identity
    if reset:
        cursor=(floor,'',-1);through=floor
    else:
        at,sig,ordinal,complete,through,_=previous
        if now<through:
            raise ValueError('scan_clock_regressed')
        cursor=(max(floor,through-RECHECK_S),'',-1) if complete else (at,sig,ordinal)
    minimum=min(SCAN_MIN_BATCH,SCAN_BATCH)
    batch=max(minimum,min(SCAN_BATCH,int(get_state(meta,'scan_batch_size',SCAN_BATCH))))
    deadline=time.monotonic()+SCAN_BUDGET_S
    budget_hit=False
    def progress():
        nonlocal budget_hit
        budget_hit=time.monotonic()>=deadline
        return budget_hit
    tape.set_progress_handler(progress,1000)
    rows=[];interrupted=False;reader=None
    try:
        reader=tape.execute(SCAN_SQL,(max(floor,cursor[0]),now,*cursor,batch))
        while True:
            row=reader.fetchone()
            if row is None:
                break
            rows.append(row)
            if time.monotonic()>=deadline:
                interrupted=True
                break
    except sqlite3.OperationalError as exc:
        if budget_hit and getattr(exc,"sqlite_errorcode",None)==sqlite3.SQLITE_INTERRUPT:
            interrupted=True
        else:
            raise
    finally:
        tape.set_progress_handler(None,0)
        if reader is not None:
            reader.close()
    complete=not interrupted and len(rows)<batch
    next_batch=max(minimum,batch//2) if interrupted else min(SCAN_BATCH,batch*2)
    last=tuple(rows[-1][i] for i in (3,4,5)) if rows else cursor
    with meta:
        if reset and (rows or not interrupted):
            meta.execute('INSERT INTO gaps VALUES(?,?,?)',(now,'scan_bootstrap','historical_mapping_count_unknown'))
        elif floor>through:
            meta.execute('INSERT INTO gaps VALUES(?,?,?)',(now,'scan_window_gap',json.dumps({'from':through,'until':floor})))
        for mint,pool,known,received,sig,ordinal,status,quote in rows:
            if (type(received) not in (int,float) or not math.isfinite(received) or not isinstance(sig,str)
                    or not sig or type(ordinal) is not int or ordinal<0):
                raise ValueError('scan_identity_unknown')
            if (status not in ('mapped','mapped_curve_derived') or quote!=WSOL or not isinstance(mint,str) or not mint or not isinstance(pool,str) or not pool
                    or type(known) not in (int,float) or not math.isfinite(known)):
                continue
            meta.execute('INSERT INTO observed_mappings VALUES(?,?,?) ON CONFLICT(mint,pool) DO UPDATE SET known_at=MIN(known_at,excluded.known_at)',
                         (mint,pool,known))
        if rows or not interrupted:
            meta.execute('INSERT OR REPLACE INTO mapping_cursor VALUES(1,?,?,?,?,?,?)',
                         (*last,int(complete),now if complete else max(floor,through),identity))
        meta.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
                     ('scan_batch_size',next_batch))
    if interrupted:
        raise ScanBudgetInterrupted(len(rows))
    return len(rows)


def candidates(tape, meta, not_before,*,priority_mints=()):
    """Pending locally observed mappings; never aggregate the source tape."""
    out,seen=[],set();priority={mint:i for i,mint in enumerate(priority_mints)}
    for mint,pool,known in meta.execute('SELECT mint,pool,known_at FROM observed_mappings ORDER BY known_at,mint,pool'):
        if mint in seen:
            continue
        seen.add(mint)
        if known<not_before and mint not in priority or meta.execute('SELECT 1 FROM mints WHERE mint=?',(mint,)).fetchone():
            continue
        if meta.execute("SELECT COUNT(*) FROM failures WHERE kind='mint' AND key=?",(mint,)).fetchone()[0]>=MAX_FAILURES:
            continue
        out.append((mint,pool,known))
    return sorted(out,key=lambda row:priority.get(row[0],len(priority)))


def pre_deploy_unread(tape, meta, not_before):
    """Observed lower bound only; the lifetime historical count is unknown."""
    return meta.execute('SELECT COUNT(*) FROM (SELECT mint FROM observed_mappings GROUP BY mint HAVING MIN(known_at)<?) o '
                        'WHERE NOT EXISTS(SELECT 1 FROM mints m WHERE m.mint=o.mint)',(not_before,)).fetchone()[0]


def pool_candidates(tape, meta, not_before):
    """Pending pool reads survive cursor advancement independently of mint reads."""
    return [(pool,known) for pool,known in meta.execute('SELECT pool,MIN(known_at) FROM observed_mappings GROUP BY pool ORDER BY MIN(known_at)')
            if known>=not_before and not meta.execute('SELECT 1 FROM pool_accounts WHERE pool=?',(pool,)).fetchone()
            and meta.execute("SELECT COUNT(*) FROM failures WHERE kind='pool' AND key=?",(pool,)).fetchone()[0]<MAX_FAILURES]


def run_once(tape_path, meta_path, rpc, *, now_fn=time.time, pool_account=False, priority_mints=(), priority_only=False):
    """One pass: returns stats. Mint reads and pool reads are scheduled INDEPENDENTLY (a cached mint never loses its pending
    pool read). Each send is gated after the pacer delay by the durable budget; the first error stops the worker."""
    stats = {"read": 0, "pool_read": 0, "failed": 0, "skipped_budget": 0, "stopped": None, "pre_deploy_unread": None, "pre_deploy_unread_observed_lower_bound": None, "source_rows": 0, "scan_partial": False, "budget_interrupts": 0, "budget_interrupts_total": 0}
    meta = MD.connect(meta_path)
    try:
        now = now_fn()
        stats["budget_interrupts_total"]=int(get_state(meta,"scan_budget_interrupts",0))
        if get_state(meta, "read_not_before") is None:
            set_state(meta, "read_not_before", now)
        if get_state(meta, "stop_until", 0.0) > now:
            stats["stopped"] = "cool_down"
            return stats
        tape = ro(tape_path)
        if tape is None:
            stats["stopped"]="tape_missing"
            return stats
        not_before = get_state(meta, "read_not_before")
        try:
            stats["source_rows"] = refresh_mappings(tape, meta, now=now)
            pending = candidates(tape, meta, not_before, priority_mints=priority_mints) if priority_mints else candidates(tape, meta, not_before)
            if priority_only:pending=[row for row in pending if row[0] in priority_mints]
            pools = pool_candidates(tape, meta, not_before) if pool_account and not priority_only else []
            stats["pre_deploy_unread_observed_lower_bound"] = pre_deploy_unread(tape, meta, not_before)
        except ScanBudgetInterrupted as exc:
            stats['source_rows']=exc.rows_read
            stats['scan_partial']=True
            stats['budget_interrupts']=1
            stats['budget_interrupts_total']+=1
            set_state(meta,'scan_budget_interrupts',stats['budget_interrupts_total'])
            return stats
        except (sqlite3.Error, ValueError, OSError) as exc:
            stats["stopped"]="tape_unreadable"
            meta.execute("INSERT INTO gaps VALUES(?,?,?)",(now,"scan_failed",type(exc).__name__))
            meta.commit()
            return stats
        finally:
            tape.close()

        def gate():
            per_min, per_day = budget_left(meta, now_fn())
            if per_min <= 0 or per_day <= 0:
                return False
            meta.execute("INSERT INTO calls(at) VALUES(?)", (now_fn(),))
            meta.commit()                                               # charged immediately before the send
            return True

        def stop(kind, key, err, received):
            meta.execute("INSERT INTO failures(kind, key, error, at) VALUES(?, ?, ?, ?)", (kind, key, err, received))
            meta.execute("INSERT INTO gaps(at, cause, detail) VALUES(?, ?, ?)", (received, "stopped:" + err, key))
            set_state(meta, "stop_until", received + (STOP_429_S if err == "rate_limited" else STOP_ERROR_S))
            stats["failed"] += 1
            stats["stopped"] = err

        for mint, _pool, _known in pending:
            result, err = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}], gate)
            received = now_fn()
            if err == "budget_exhausted":
                stats["skipped_budget"] += 1
                return stats
            row = None
            if err is None:
                row, err = MD.parse_mint_account(result)
            if err is not None:
                stop("mint", mint, err, received)
                return stats                                            # the first error stops the worker
            meta.execute("INSERT OR IGNORE INTO mints VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (mint, row["decimals"], row["supply_atomic"], row["token_program"], row["mint_authority"],
                          row["freeze_authority"], received, row["slot"], MD.SOURCE, json.dumps(row, sort_keys=True)))
            meta.commit()
            stats["read"] += 1
        for pool, _known in pools:
            res, err = rpc("getAccountInfo", [pool, {"encoding": "base64", "commitment": "confirmed"}], gate)
            got = now_fn()
            if err == "budget_exhausted":
                stats["skipped_budget"] += 1
                return stats
            value = (res or {}).get("value") if err is None and isinstance(res, dict) else None
            ctx_slot = ((res or {}).get("context") or {}).get("slot") if isinstance(res, dict) else None
            if err is None and value and isinstance(value.get("data"), list) and value["data"] and type(ctx_slot) is int:
                meta.execute("INSERT OR IGNORE INTO pool_accounts VALUES(?,?,?,?,?,?,?)",
                             (pool, value.get("owner"), value.get("lamports"), value["data"][0], got, ctx_slot,
                              "helius_research:get_account_info:base64"))
                meta.commit()
                stats["pool_read"] += 1
            else:
                stop("pool", pool, err or "account_missing", got)
                return stats
        return stats
    finally:
        meta.close()


def main(argv=None, *, rpc=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import require_state_root_outside_release, state_root
    require_state_root_outside_release()
    load_env_files(str(state_root()))
    if os.getenv("MEMECOIN_DESK_PS_METADATA_ENABLED", "0").strip() != "1":
        print("[ps-metadata] MEMECOIN_DESK_PS_METADATA_ENABLED is not 1; exiting")
        return 0
    os.nice(19)
    base = Path(state_root()) / "data" / "desk" / "pumpswap"
    base.mkdir(parents=True, exist_ok=True)
    lock = open(base / "metadata.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[ps-metadata] another worker holds the lock; exiting")
        return 0
    if rpc is None:
        sys.path.insert(0, str(ROOT / "scripts" / "research"))
        from slot_lane_send_monitor import pick_research_url
        from sniper.services.memecoin_research_pacer import SharedPacer
        url = pick_research_url(os.environ)
        rpc = Rpc(url, SharedPacer.for_url(url))
    pool_account = os.getenv("MEMECOIN_DESK_PS_META_POOL_ACCOUNT", "0").strip() == "1"
    while True:
        priority_only=priority_only_mode(os.environ)
        priority_path=Path(state_root())/'data/office/v2/large-buyer-arm/arm.db' if priority_only else None
        priority=first_reach_priority(priority_path,now=time.time())
        stats = run_once(base / "tape.db", base / "metadata.db", rpc, pool_account=pool_account, priority_mints=priority['mints'], priority_only=priority_only)
        stats.update(priority_only=priority_only,priority_state=priority['state'],priority_reason=priority['reason'],priority_mints=len(priority['mints']),priority_truncated=priority['truncated'])
        if stats["read"] or stats["pool_read"] or stats["failed"] or stats["stopped"] or stats["budget_interrupts"] or stats.get("priority_state") not in ("known","not_requested"):
            print(json.dumps(stats), flush=True)
        if a.once or stats["stopped"]:
            return 0 if not stats["stopped"] or stats["stopped"] == "cool_down" else 3
        time.sleep(POLL_S)


if __name__ == "__main__":
    raise SystemExit(main())
