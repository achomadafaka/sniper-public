#!/usr/bin/env python3
"""Desk U assembler: a producer-U emission becomes a step-3 ticket that hard_filter has evaluated.

Per emission (data/desk/candidate_emissions/<UTC day>.jsonl, read-only, fresh rows only):
  1. verify_u_emission (identity + age), then ingest_candidate into the configured U cohort
     (--cohort-id / MEMECOIN_DESK_U_COHORT_ID; refused when unset, missing or not producer U; never a
     default). A non-primary emission (duplicate, active ticket, cooldown) is retained by step 3 and
     logged; it opens no funnel.
  2. safety: two paced research reads (memecoin_desk_safety) -> token_safety, holders, market_data.
  3. wait for Beta's desk-cluster/v1 row bound by ticket_id + mint + pool + snapshot_hash, at most the
     cohort's frozen policy["evidence_wait_s"] after emission; no row by then = cluster unknown:timeout
     (hard_filter refuses unknown, never a pass).
  4. right before evaluate_candidate: the sell probe is recomputed from the local U tape (swaps day
     file, gaps, heartbeat; 0 RPC; 10 s reserves cap; probe_from_tape).
  5. evaluate_candidate with the cohort's frozen hard_filter policy. Every outcome is persisted by step
     3 (the hard_filter stage) and logged on this process's own row (data/desk/assembler/<day>.jsonl)
     with the waits, the cluster latency and the probe evidence that hard_filter cannot carry yet.

Off every entry path: research key via pick_research_url + SharedPacer, the desk DB only (no ledger).
One writer of data/desk/assembler. Default: one bounded pass; --loop is explicit.

Runbook: an ingest REFUSED reason=crash_loop row means one emission line killed the process
CRASH_TRIES times and was skipped (from_day/from_offset name it; a SIGTERM mid-line also counts as a
try). If ingest committed and every later log write failed, its step-3 ticket stays open with no
stage: close it by hand. counters() reads only today's log, so a restart in the first minute after
00:00Z can spend up to --per-min extra reads.

    python3 scripts/memecoin_desk_assembler.py --desk-db data/memecoin_desk.db --cohort-id <id> --loop
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_root                      # noqa: E402


from sniper.services import memecoin_desk_safety as S               # noqa: E402

PAUSE_ON_429_S = 60.0                                                # the slot-end shadow's pause on this key
SAFETY_CALLS = 2                                                     # check_ticket's reads, reserved up front
SHARED_GAP_S = 1.1                                                   # SharedPacer.GAP_S: the key's default spacing
POLL_IDLE_S = 0.05                                                   # emissions poll (was 0.5: ~0.25 s mean added latency)
CLUSTER_POLL_S = 0.5                                                 # cluster rows are re-read at the old cadence
CRASH_TRIES = 3                                                      # starts of one line before it is refused


def pacer_gap(env=None):
    """MEMECOIN_DESK_ASSEMBLER_PACER_GAP_S: this reader's spacing on the shared research key. Default = the
    key's own 1.1 s (no change). Clamped to [0.2, 1.1]; a malformed or absent value is the default."""
    raw = (os.environ if env is None else env).get("MEMECOIN_DESK_ASSEMBLER_PACER_GAP_S")
    try:
        return min(SHARED_GAP_S, max(0.2, float(raw)))
    except (TypeError, ValueError):
        return SHARED_GAP_S


def quota_error(code, message):
    return code in (429, -32429) or bool(re.search(
        r"too many|(?:credit|usage).{0,30}(?:limit|exceed)|(?:limit|exceed).{0,30}(?:credit|usage)",
        str(message), re.I))


class Rpc:
    def __init__(self, url, pacer, *, timeout_s=5.0, clock=time.time, sleep=time.sleep):
        self.url, self.pacer, self.timeout_s, self.calls, self.ms = url, pacer, timeout_s, 0, []
        self.clock, self.sleep, self.last_error = clock, sleep, None

    def failure(self, method, slug, *, code=None, http_status=None, exc=None, message="", retryable=False):
        parsed = urllib.parse.urlsplit(self.url)
        original = str(message)
        message = original
        secrets = [self.url, parsed.username, parsed.password]
        secrets += [v for _, v in urllib.parse.parse_qsl(parsed.query)]
        secrets += [p for p in parsed.path.split("/") if len(p) >= 8]
        for secret in secrets:
            if secret:
                message = message.replace(secret, "[redacted]")
        message = re.sub(r"https?://\S+", "[redacted-url]", message)
        self.last_error = {"method": method, "hostname": parsed.hostname,
                           "code": code if type(code) is int else None, "http_status": http_status,
                           "exception_class": type(exc).__name__ if exc else None,
                           "message": message[:240], "redacted": message != original,
                           "truncated": len(message) > 240, "retryable": retryable}
        return None, slug

    def limited(self):
        self.pacer.pause(PAUSE_ON_429_S)
        if hasattr(self.pacer, "gap_s"):
            self.pacer.gap_s = SHARED_GAP_S

    def __call__(self, method, params, *, deadline=None):
        """(result, None) or (None, error slug). One paced call, no retry."""
        self.last_error = None
        if deadline is not None and self.clock() >= deadline:
            self.ms.append(0.0)
            return self.failure(method, "deadline")
        self.pacer.acquire()
        if deadline is not None and self.clock() >= deadline:
            self.ms.append(0.0)
            return self.failure(method, "deadline")
        self.calls += 1
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        t0 = time.monotonic()
        try:
            timeout = self.timeout_s if deadline is None else min(self.timeout_s, deadline - self.clock())
            if timeout <= 0:
                return self.failure(method, "deadline")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                reply = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            code, message, truncated, body_quota = None, str(exc.reason), False, False
            try:
                body = exc.read(8193)
                truncated = len(body) > 8192
                body_quota = quota_error(None, body.decode("utf-8", errors="replace"))
                decoded = json.loads(body) if body and not truncated else None
                error = decoded.get("error") if isinstance(decoded, dict) else None
                if isinstance(error, dict):
                    code, message = error.get("code"), error.get("message", message)
            except (OSError, ValueError):
                pass
            finally:
                exc.close()
            quota = body_quota or quota_error(exc.code, exc.reason) or quota_error(code, message)
            if quota:
                self.limited()
            result = self.failure(method, "rate_limited" if quota else "http_%d" % exc.code, code=code,
                                  http_status=exc.code, exc=exc, message=message,
                                  retryable=not quota and not truncated and (exc.code == 408 or 500 <= exc.code <= 599))
            self.last_error["truncated"] |= truncated
            return result
        except (TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            permanent = isinstance(reason, ssl.SSLCertVerificationError) or (
                isinstance(reason, socket.gaierror) and reason.errno != socket.EAI_AGAIN)
            return self.failure(method, "timeout", exc=exc, message=reason, retryable=not permanent)
        except ValueError as exc:
            return self.failure(method, "malformed", exc=exc)
        finally:
            self.ms.append((time.monotonic() - t0) * 1000)
        if deadline is not None and self.clock() >= deadline:
            return self.failure(method, "deadline")
        if not isinstance(reply, dict) or "error" in reply or "result" not in reply:
            err = (reply or {}).get("error") if isinstance(reply, dict) else None
            code = err.get("code") if isinstance(err, dict) else None
            message = err.get("message", "") if isinstance(err, dict) else ""
            quota = quota_error(code, message)
            if quota:
                self.limited()
            return self.failure(method, "rate_limited" if quota else "rpc_error", code=code, message=message)
        return reply["result"], None


def check_ticket(cand, rpc, sol_usd, *, clip_usd, creator_pct_max=None, deadline=None, reserve_retry=None):
    """(checks, meta) for one ticket: read 1, read 2, then the pure evaluation."""
    errors, retried = [], False

    def read(method, params):
        nonlocal retried
        result, err = (rpc(method, params, deadline=deadline) if isinstance(rpc, Rpc)
                       else rpc(method, params))
        failure = getattr(rpc, "last_error", None)
        if err and failure:
            errors.append({**failure, "attempt": 1, "elapsed_ms": round(rpc.ms[-1], 1)})
        if (err and failure and failure["retryable"] and not retried
                and deadline is not None and rpc.clock() + 0.3 < deadline
                and reserve_retry is not None and reserve_retry()):
            retried = True
            rpc.sleep(0.3)
            if rpc.clock() >= deadline:
                return result, err
            result, err = rpc(method, params, deadline=deadline)
            if err and rpc.last_error:
                errors.append({**rpc.last_error, "attempt": 2, "elapsed_ms": round(rpc.ms[-1], 1)})
        return result, err

    def metadata(meta):
        return {**meta, "rpc_errors": errors} if errors else meta

    largest, err = read("getTokenLargestAccounts", [cand["mint"], {"commitment": "confirmed"}])
    largest = largest.get("value") if err is None and isinstance(largest, dict) else None
    if not isinstance(largest, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("address"), str) or not row["address"].strip()
            for row in largest):
        return S.evaluate(cand, largest=None, accounts=None, ctx_slot=None, sol_usd=sol_usd)[0], metadata({
            "error": err or "malformed"})
    addresses = S.batch_addresses(cand["mint"], largest)
    if sol_usd is None:                              # the production assembler prices its probe from tape
        addresses = addresses[:2] + addresses[3:]
    batch, err = read("getMultipleAccounts", [addresses,
                                             {"encoding": "base64", "commitment": "confirmed"}])
    if err is not None or not isinstance(batch, dict) or len(batch.get("value") or []) != len(addresses):
        return S.evaluate(cand, largest=None, accounts=None, ctx_slot=None, sol_usd=sol_usd)[0], metadata({
            "error": err or "malformed"})
    accounts = batch["value"]
    if sol_usd is None:
        accounts = accounts[:2] + [None] + accounts[2:]  # evaluator's fixed fee slot is explicitly unknown
    checks, meta = S.evaluate(cand, largest=largest, accounts=accounts,
                              ctx_slot=(batch.get("context") or {}).get("slot"), sol_usd=sol_usd, clip_usd=clip_usd,
                              creator_pct_max=creator_pct_max)
    return checks, metadata(meta)


def tail_lines(path, offset):
    """[(row or None, end_offset)] for the new complete lines after `offset` (None = unparsable)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except FileNotFoundError:
        return []
    out, pos = [], offset
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        pos += len(line)
        try:
            out.append((json.loads(line), pos))
        except ValueError:
            out.append((None, pos))
    return out


def load_cursor(path):
    """(day, offset, tries): tries = starts of the line at offset that never finished (a crash loop)."""
    try:
        cur = json.loads(path.read_text())
        return cur["day"], int(cur["offset"]), int(cur.get("tries", 0))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_cursor(path, day_, offset, tries=0):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"day": day_, "offset": offset, "tries": tries}))
    tmp.replace(path)


def counters(out_dir, now):
    """(calls today, per-minute window) rebuilt from this process's own reservation rows, written
    before any RPC: a restart, even one mid-read, must not reset the research budget (Gamma/Codex).
    A retry has its own one-call charge, but is not another per-minute ticket.
    shortcut: previous-day minute reservations are not restored; address separately at midnight."""
    rows, _ = tail(out_dir / ("%s.jsonl" % utc_day(now)), 0)
    # admission and refilter reservations each take a minute ticket and the daily charge; a retry is daily only
    reads = [r for r in rows if r.get("stage") in ("safety_reserve", "safety_retry_reserve", "refilter_reserve")]
    return sum(r["rpc_calls"] for r in reads), [r["reserved_at"] for r in reads
        if r["stage"] in ("safety_reserve", "refilter_reserve") and now - r["reserved_at"] < 60]


def tail(path, offset):
    """New complete JSON lines after byte `offset`: (rows, new_offset)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except FileNotFoundError:
        return [], offset
    end = data.rfind(b"\n") + 1
    rows = []
    for line in data[:end].splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows, offset + end



TAPE_MAX_AGE_S = 10.0


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def ro(path):
    if not Path(path).exists():
        return None
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    return conn


def regular_fees(desk_dir, mint, now, n=S.REGULAR_FEE_TRADES):
    """(fee_bps, creator_fee_bps) of the mint's newest NON-agent trades, newest first, today's day file then yesterday's.
    A read error returns [] (the probe then says fee_unknown): never a guess."""
    out = []
    agents = sorted(S.MAYHEM_AGENT_IDS)
    for d in (utc_day(now), utc_day(now - 86400)):
        if len(out) >= n:
            break
        conn = ro(desk_dir / "swaps" / ("%s.db" % d))
        if conn is None:
            continue
        try:
            out += [tuple(r) for r in conn.execute(
                "SELECT fee_bps, creator_fee_bps FROM swaps WHERE mint=? AND actor NOT IN (%s) "
                "ORDER BY slot DESC, ordinal DESC LIMIT ?" % ",".join("?" * len(agents)), (mint, *agents, n - len(out)))]
        except sqlite3.Error:
            return []
        finally:
            conn.close()
    return out


def tape_state(desk_dir, mint, now):
    """The U tape read for probe_from_tape: (last_trade, head_slot, head_known_at, gap_last_slots,
    heartbeat_age_s, sol_usd). Today's day file, then yesterday's for a trade just before midnight."""
    try:
        beat = json.loads((desk_dir / "universe.heartbeat").read_text())
        heartbeat_age = now - float(beat["ts"])
        sol = beat.get("sol_usd") if (beat.get("sol_age_s") or 1e9) <= 120 else None
    except (OSError, ValueError, KeyError, TypeError):
        heartbeat_age, sol = None, None
    last, head = None, (None, None)
    for d in (utc_day(now), utc_day(now - 86400)):
        conn = ro(desk_dir / "swaps" / ("%s.db" % d))
        if conn is None:
            continue
        try:
            if head == (None, None):
                row = conn.execute("SELECT slot, received_at FROM swaps ORDER BY slot DESC LIMIT 1").fetchone()
                head = tuple(row) if row else (None, None)
            row = conn.execute("SELECT slot, virtual_quote, virtual_token, real_token, fee_bps, creator_fee_bps, "
                               "received_at FROM swaps WHERE mint=? ORDER BY slot DESC, ordinal DESC LIMIT 1",
                               (mint,)).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            conn.close()
        if row is not None:
            last = dict(zip(("slot", "virtual_quote", "virtual_token", "real_token", "fee_bps", "creator_fee_bps",
                             "received_at"), row))
            break
    gaps = []
    for d in {utc_day(now), utc_day(now - 86400)}:
        conn = ro(desk_dir / "gaps" / ("%s.db" % d))
        if conn is None:
            continue
        try:
            since = last["received_at"] if last else now - 3600
            gaps += [r[0] for r in conn.execute("SELECT last_slot FROM gaps WHERE at >= ? OR last_slot >= ?",
                                                (since - 1, (last or {}).get("slot") or 0))]
        except sqlite3.Error:
            gaps.append(None)                         # unreadable gap file: treat as an open gap
        finally:
            conn.close()
    if last is not None:
        last["regular_fees"] = regular_fees(desk_dir, mint, now)
    return last, head[0], head[1], gaps, heartbeat_age, sol


def cluster_rows(desk_dir, seen):
    """New desk-cluster/v1 rows by ticket_id (Beta's files, read-only)."""
    out = {}
    for path in sorted((desk_dir / "cluster_checks").glob("*.jsonl")):
        offset = seen.get(path, 0)
        rows, seen[path] = tail(path, offset)
        for row in rows:
            if row.get("schema") == "desk-cluster/v1" and row.get("ticket_id"):
                out[row["ticket_id"]] = row
    return out


def bound(row, cand):
    return all(row.get(k) == cand[k] for k in ("ticket_id", "network_id", "mint", "pool")) and \
        row.get("snapshot_hash") == cand["snapshot"]["hash"]


def assemble(cand, *, safety, cluster, probe, now, cohort_policy=None, dump_cache=None):
    """hard_filter's evidence envelope. Rows that fail their binding never enter it."""
    unknown = lambda reason: {"status": "unknown", "reason": reason}
    checks = {k: safety.get(k) or unknown("missing") for k in ("market_data", "token_safety", "holders")}
    if cluster is not None:                      # hf2: soft. Absent = 'unknown:not_yet_ready' for the JUDGE, never a wait
        from sniper.services.memecoin_desk_soft_facts import check_from_row
        checks["cluster"] = check_from_row(cluster, cohort_policy)
    checks["sell_probe"] = probe
    checks["dump_group"] = (dump_cache.lookup(mint=cand["mint"], pool=cand["pool"], now=now)
                            if dump_cache is not None else unknown("dump_group_unknown"))
    return {"network_id": cand["network_id"], "mint": cand["mint"], "pool": cand["pool"],
            "snapshot_hash": cand["snapshot"]["hash"], "known_at": iso(now), "checks": checks}


def unfinished(out_dir, now, cohort_id):
    """Tickets this process ingested (today or yesterday) that never got a hard_filter row: an
    assembler restart mid-wait. Read from its own log, so no desk SQL here."""
    ingested, finished = {}, set()
    for d in (utc_day(now - 86400), utc_day(now)):
        rows, _ = tail(out_dir / ("%s.jsonl" % d), 0)
        for row in rows:
            if row.get("cohort_id") != cohort_id:
                continue                                # another cohort's (or a smoke's) ticket
            if row.get("stage") == "ingested":
                ingested[row["ticket_id"]] = row
            elif row.get("stage") == "hard_filter":
                finished.add(row["ticket_id"])
    return [row for tid, row in ingested.items() if tid not in finished]


def main(argv=None, *, env_root=None, rpc=None, from_start=False):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    load_env_files(str(env_root or state_root()))      # cron has no environment; not at import
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--desk-db", required=True)
    p.add_argument("--cohort-id", default=os.getenv("MEMECOIN_DESK_U_COHORT_ID"))
    p.add_argument("--loop", action="store_true")
    p.add_argument("--max-tickets", type=int, default=0, help="stop after N evaluated tickets (smoke)")
    p.add_argument("--per-min", type=int, default=int(os.getenv("MEMECOIN_DESK_SAFETY_PER_MIN", "6")))
    p.add_argument("--calls-per-day", type=int, default=int(os.getenv("MEMECOIN_DESK_SAFETY_CALLS_PER_DAY", "3000")))
    p.add_argument("--clip-usd", type=float, default=25.0)
    a = p.parse_args(argv)
    if os.getenv("MEMECOIN_DESK_ASSEMBLER_ENABLED", "0").strip() != "1":
        print("[desk-assembler] MEMECOIN_DESK_ASSEMBLER_ENABLED is not 1; exiting")
        return 0
    from sniper.persistence import db_memecoin_desk as store
    from sniper.services import memecoin_desk as desk
    if not a.cohort_id:
        p.error("--cohort-id (MEMECOIN_DESK_U_COHORT_ID) is required; there is no default cohort")
    desk_conn = store.connect(a.desk_db)
    cohort = store.get_cohort(desk_conn, a.cohort_id)
    if cohort is None or cohort["producer"] != "U":
        p.error("cohort %s is not a provisioned producer-U cohort" % a.cohort_id)
    policy = cohort["policy"]
    if policy.get("venue", "pumpfun") != "pumpfun":
        p.error("emission_venue: this assembler only supports pumpfun; PumpSwap probe is not wired")
    wait_s = policy.get("evidence_wait_s")
    if type(wait_s) is not int or not 1 <= wait_s <= 120:
        p.error("cohort policy evidence_wait_s missing or out of 1..120; never defaulted")
    hard_policy = policy["hard_filter"]
    if rpc is None:
        sys.path.insert(0, str(ROOT / "scripts" / "research"))
        from slot_lane_send_monitor import pick_research_url
        from sniper.services.memecoin_research_pacer import SharedPacer
        url = pick_research_url(os.environ)
        rpc = Rpc(url, SharedPacer.for_url(url, gap_s=pacer_gap()))
    desk_dir = Path(state_root()) / "data" / "desk"
    out_dir = desk_dir / "assembler"
    out_dir.mkdir(parents=True, exist_ok=True)
    import fcntl
    lock = open(out_dir / "assembler.lock", "w")
    try:                                              # one writer: a second start exits
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-assembler] another assembler holds the lock; exiting")
        return 0
    from sniper.services.memecoin_desk_dump_group_cache import SnapshotCache
    dump_cache = SnapshotCache(desk_dir / "dump_group" / "cache.json",
        enabled=os.getenv("MEMECOIN_DESK_DUMP_GROUP_BLOCK", "1").strip() != "0").start()
    emissions = desk_dir / "candidate_emissions"
    cursor_path = out_dir / "cursor.json"
    cursor = None if from_start else load_cursor(cursor_path)
    pending, clusters, seen, done = {}, {}, {}, 0
    calls_day = utc_day(time.time())
    calls_today, window = counters(out_dir, time.time())

    def log(row):
        with open(out_dir / ("%s.jsonl" % utc_day(time.time())), "a") as fh:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str) + "\n")
        print(json.dumps({k: row.get(k) for k in ("ticket_id", "stage", "verdict", "reason")}), flush=True)

    today = utc_day(time.time())
    tries = 0
    if from_start:
        src_day, offset = today, 0
    elif cursor is not None:
        # resume where the last run stopped: the downtime backlog is ingested and SKIPs as
        # emission_stale, so the population keeps every emission (Gamma BLOCKING on 49609f921)
        src_day, offset, tries = cursor
        backlog = sum(1 for d in sorted(p.stem for p in emissions.glob("*.jsonl")) if d >= src_day
                      for _ in tail_lines(emissions / ("%s.jsonl" % d), offset if d == src_day else 0))
        log({"schema": "desk-assembler/v1", "cohort_id": a.cohort_id, "stage": "coverage", "verdict": "RESUME",
             "reason": None, "from_day": src_day, "from_offset": offset, "backlog_lines": backlog})
    else:
        # first start ever: the population begins now; what is already in the file is named, not taken
        src = emissions / ("%s.jsonl" % today)
        src_day, offset = today, src.stat().st_size if src.exists() else 0
        log({"schema": "desk-assembler/v1", "cohort_id": a.cohort_id, "stage": "coverage", "verdict": "FIRST_START",
             "reason": None, "from_day": today, "from_offset": offset,
             "skipped_lines": len(tail_lines(src, 0)) if offset else 0})
    save_cursor(cursor_path, src_day, offset, tries)

    def close_restarted(ticket_id, cand):
        # restart mid-wait: the in-memory wait is gone; close the ticket with all-unknown evidence so
        # step 3 persists a SKIP and the coin is released after its cooldown (never left 'open')
        now = time.time()
        evidence = assemble(cand, safety={}, cluster=None,
                            probe={"status": "unknown", "reason": "assembler_restart"}, now=now, dump_cache=dump_cache)
        try:
            filtered = desk.evaluate_candidate(desk_conn, ticket_id, evidence=evidence, policy=hard_policy,
                                               now_iso=iso(now))
            verdict, reason = filtered["verdict"], filtered["reason"]
        except ValueError as exc:
            verdict, reason = "REFUSED", str(exc)
        log({"schema": "desk-assembler/v1", "ticket_id": ticket_id, "cohort_id": a.cohort_id,
             "stage": "hard_filter", "verdict": verdict, "reason": reason, "recovered": "assembler_restart"})

    for row in unfinished(out_dir, time.time(), a.cohort_id):
        close_restarted(row["ticket_id"], row["candidate"])

    def handle(cand):
        nonlocal calls_day, calls_today, window
        if not isinstance(cand, dict) or cand.get("producer") != "U":
            return
        now = time.time()
        base = {"schema": "desk-assembler/v1", "ticket_id": cand.get("ticket_id"), "cohort_id": a.cohort_id}
        try:
            if cand.get("venue", "pumpfun") != "pumpfun" or cand.get("schema") == "desk-candidate/pumpswap-v1":
                raise ValueError("emission_venue")
            # identity only here (a day's window): a stale emission is still ingested and gets a
            # persisted SKIP below, so every genuine emission is in step 3
            S.verify_u_emission(cand, now=now, max_age_s=86400)
            ingested = desk.ingest_candidate(desk_conn, cand, cohort_id=a.cohort_id, now_iso=iso(now))
        except ValueError as exc:
            log({**base, "stage": "ingest", "verdict": "REFUSED", "reason": str(exc)})
            return
        tid = ingested["ticket_id"]
        if not ingested["is_primary"]:
            if (ingested["reason"] == "duplicate" and ingested["cohort_id"] == a.cohort_id
                    and ingested["state"] == "open" and not ingested["stages"] and tid not in pending):
                # the re-read of a line whose ingest committed before a crash, ahead of its 'ingested'
                # row: unfinished() cannot see it, so it is closed here (Codex 066e25c29)
                close_restarted(tid, cand)
                return
            log({**base, "stage": "ingest", "verdict": "NOT_PRIMARY", "reason": ingested["reason"]})
            return
        # before any RPC: a crash in the paced reads leaves a ticket that unfinished() closes
        log({**base, "ticket_id": tid, "stage": "ingested", "verdict": "PRIMARY", "reason": None, "candidate": cand})
        if utc_day(now) != calls_day:
            calls_day, calls_today = utc_day(now), 0
        window = [t for t in window if now - t < 60]
        t0 = time.monotonic()
        stale = now - cand["snapshot"]["emission_at"] > wait_s
        if stale or len(window) >= a.per_min or calls_today + SAFETY_CALLS > a.calls_per_day:
            cap = ("emission_stale" if stale else
                   "per_min_cap" if len(window) >= a.per_min else "daily_call_cap")
            safety, meta = {k: {"status": "unknown", "reason": cap}
                            for k in ("token_safety", "holders", "market_data")}, {"rpc_calls": 0}
        else:
            # charged before the reads: a crash mid-read cannot hand a restart a fresh budget
            log({**base, "ticket_id": tid, "stage": "safety_reserve", "verdict": None, "reason": None,
                 "reserved_at": now, "rpc_calls": SAFETY_CALLS})
            window.append(now)
            calls_today += SAFETY_CALLS
            before = rpc.calls
            def reserve_retry():
                nonlocal calls_day, calls_today
                retry_now = time.time()
                if retry_now >= cand["snapshot"]["emission_at"] + wait_s:
                    return False
                if utc_day(retry_now) != calls_day:
                    calls_day, calls_today = utc_day(retry_now), 0
                if calls_today + 1 > a.calls_per_day:
                    return False
                log({**base, "ticket_id": tid, "stage": "safety_retry_reserve", "verdict": None,
                     "reason": None, "reserved_at": retry_now, "rpc_calls": 1})
                calls_today += 1
                return True
            safety, meta = check_ticket(cand, rpc, None, clip_usd=a.clip_usd,
                                       deadline=cand["snapshot"]["emission_at"] + wait_s,
                                       reserve_retry=reserve_retry)
            meta["rpc_calls"] = rpc.calls - before
        meta["safety_ms"] = round((time.monotonic() - t0) * 1000, 1)
        meta["safety_at"] = time.time()             # facts are this old at evaluation, not known_at
        log({**base, "ticket_id": tid, "stage": "safety", "verdict": None, "reason": None,
             "safety_at": meta["safety_at"], "rpc_calls": meta["rpc_calls"]})
        pending[tid] = {"cand": cand, "safety": safety, "meta": meta,
                        "deadline": cand["snapshot"]["emission_at"] + wait_s}

    def refilter(h):
        """standing-v2 (operator 10-26): a fired order enters only on a FRESH hard filter, here (paced RPC, off the
        tape and fire threads). Same budget as admission; a capped read is unknown evidence, so the filter SKIPs by name."""
        nonlocal calls_day, calls_today, window
        cand, now, t0 = h["candidate"], time.time(), time.monotonic()
        if utc_day(now) != calls_day:
            calls_day, calls_today = utc_day(now), 0
        window = [t for t in window if now - t < 60]
        if len(window) >= a.per_min or calls_today + SAFETY_CALLS > a.calls_per_day:
            cap = "per_min_cap" if len(window) >= a.per_min else "daily_call_cap"
            safety, meta = {k: {"status": "unknown", "reason": cap} for k in ("token_safety", "holders", "market_data")}, {}
            source = "capped:" + cap
        else:
            # charged and logged BEFORE the reads, like admission: a crash mid-read cannot hand a restart a fresh budget
            log({"schema": "desk-assembler/v1", "ticket_id": h["ticket_id"], "cohort_id": a.cohort_id,
                 "stage": "refilter_reserve", "verdict": None, "reason": None, "reserved_at": now, "rpc_calls": SAFETY_CALLS})
            window.append(now)
            calls_today += SAFETY_CALLS
            safety, meta = check_ticket(cand, rpc, None, clip_usd=a.clip_usd)
            source = "fresh"
        safety_at = now = time.time()
        last, head_slot, head_at, gaps, beat_age, sol = tape_state(desk_dir, cand["mint"], now)
        probe, _ = S.probe_from_tape(
            last, head_slot=head_slot, head_known_at=head_at, heartbeat_age_s=beat_age, gap_last_slots=gaps,
            graduated=(last or {}).get("real_token") == 0, mayhem=meta.get("mayhem"), sol_usd=sol, evaluated_at=now,
            clip_usd=a.clip_usd, max_age_s=TAPE_MAX_AGE_S)
        evidence = assemble(cand, safety=safety, cluster=None, probe=probe, now=now, cohort_policy=cohort["policy"], dump_cache=dump_cache)
        fire = next((st for st in h["stages"] if st["stage"] == "trigger"), {}).get("inputs", {}).get("fire", {})
        timing = {"refilter_ms": round((time.monotonic() - t0) * 1000, 1), "fire_received_at": fire.get("received_at"),
                  # Gamma (watch-list): which safety the filter read and how old it was at the evaluation
                  "safety_source": source, "safety_age_s": round(time.time() - safety_at, 3)}
        try:
            row = desk.record_refilter(desk_conn, h["ticket_id"], evidence=evidence, now_iso=iso(now), timing=timing)
            verdict, reason = row["verdict"], row["reason"]
        except ValueError as exc:
            verdict, reason = "REFUSED", str(exc)
        log({"schema": "desk-assembler/v1", "ticket_id": h["ticket_id"], "cohort_id": a.cohort_id, "stage": "refilter",
             "verdict": verdict, "reason": reason, **timing})

    last_cluster = 0.0
    try:
        while True:
            now = time.time()
            src = emissions / ("%s.jsonl" % src_day)
            lines = tail_lines(src, offset)
            if not lines and src_day < utc_day(now):
                # this day's file is done and a newer day exists: advance only after it is fully read
                src_day, offset = min(d for d in [utc_day(now)] + [p.stem for p in emissions.glob("*.jsonl")]
                                      if d > src_day), 0
                save_cursor(cursor_path, src_day, offset)
                continue
            rows = [r for r, _ in lines]
            for cand, end in lines:
                tries += 1
                save_cursor(cursor_path, src_day, offset, tries)
                if tries > CRASH_TRIES:
                    # the same line killed the process CRASH_TRIES times (an error ingest does not type
                    # as ValueError): refuse it by name so later emissions are not held behind it (Gamma LOW 1)
                    log({"schema": "desk-assembler/v1", "cohort_id": a.cohort_id, "stage": "ingest",
                         "verdict": "REFUSED", "reason": "crash_loop",
                         "ticket_id": cand.get("ticket_id") if isinstance(cand, dict) else None,
                         "from_day": src_day, "from_offset": offset})
                else:
                    handle(cand)
                # only after the line is durable in step 3 and our log: a crash re-reads it (Codex 066e25c29)
                offset, tries = end, 0
                save_cursor(cursor_path, src_day, offset)
            if time.monotonic() - last_cluster >= CLUSTER_POLL_S:
                last_cluster = time.monotonic()
                clusters.update(cluster_rows(desk_dir, seen))
            for tid in list(pending):
                item, now = pending[tid], time.time()
                cand = item["cand"]
                row = clusters.get(cand["ticket_id"])
                cluster = row if row is not None and bound(row, cand) else None
                del pending[tid]                 # hf2: the cluster row is a JUDGE fact, the filter never waits for it
                last, head_slot, head_at, gaps, beat_age, sol = tape_state(desk_dir, cand["mint"], now)
                probe, probe_ev = S.probe_from_tape(
                    last, head_slot=head_slot, head_known_at=head_at, heartbeat_age_s=beat_age, gap_last_slots=gaps,
                    graduated=(last or {}).get("real_token") == 0, mayhem=item["meta"].get("mayhem"), sol_usd=sol,
                    evaluated_at=now, clip_usd=a.clip_usd, max_age_s=TAPE_MAX_AGE_S)
                evidence = assemble(cand, safety=item["safety"], cluster=cluster, probe=probe, now=now,
                                    cohort_policy=cohort["policy"], dump_cache=dump_cache)
                try:
                    filtered = desk.evaluate_candidate(desk_conn, tid, evidence=evidence, policy=hard_policy,
                                                       now_iso=iso(now))
                    verdict, reason = filtered["verdict"], filtered["reason"]
                except ValueError as exc:
                    verdict, reason = "REFUSED", str(exc)
                emitted = cand["snapshot"]["emission_at"]
                log({"schema": "desk-assembler/v1", "ticket_id": tid, "cohort_id": a.cohort_id, "stage": "hard_filter",
                     "verdict": verdict, "reason": reason, "mint": cand["mint"], "emission_at": emitted,
                     "evaluated_at": now, "emission_to_filter_s": round(now - emitted, 3),
                     "cluster_wait": "row" if cluster else ("unbound_row" if row is not None else "not_yet_ready"),
                     "cluster_latency_s": (round(datetime.fromisoformat(cluster["known_at"]).timestamp() - emitted, 3)
                                           if cluster else None),
                     "checks": {k: v.get("status") + ":" + str(v.get("reason")) for k, v in evidence["checks"].items()},
                     "safety_meta": item["meta"], "probe_evidence": probe_ev, "sol_usd": sol,
                     "tape": {"head_slot": head_slot, "head_age_s": round(now - head_at, 3) if head_at else None,
                              "heartbeat_age_s": beat_age, "gaps": gaps}})
                done += 1
                if a.max_tickets and done >= a.max_tickets:
                    return 0
            for h in desk.list_pending_refilters(desk_conn):
                refilter(h)
            if not a.loop and not pending and not rows:
                return 0
            time.sleep(POLL_IDLE_S)
    except KeyboardInterrupt:
        return 0
    finally:
        dump_cache.close()
        desk_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
