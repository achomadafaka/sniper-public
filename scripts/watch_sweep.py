#!/usr/bin/env python3
"""15-minute system sweep that OUTLIVES every Claude session.

WHY THIS EXISTS (2026-08-26, Beta's observation): the operator ordered 15-minute monitoring.
The watchman and its backstop were both Claude sessions, which share one failure mode — session
lifetime. Two observers with correlated deaths are closer to one observer than to two. This cron
sweep is the external clock: release-independent, session-independent, and it writes a heartbeat
a human can read cold (data/watch_sweep.heartbeat).

READ-ONLY BY CONSTRUCTION. It observes and pages; it never restarts, deploys, or mutates. The
one write is its own heartbeat/state file. Anomalies page via the system's own Telegram sender —
the same channel every other operational alert uses — deduplicated so an ongoing condition pages
once and then every 2 hours, not every 15 minutes.

Checks (each fails soft; a broken check reports itself rather than silencing the sweep):
  runner   heartbeat age, auth_ok, bracket_ts (exit sweep) age, rails-blind positions
  web      systemd active + serving pid started AFTER the current symlink flip (two-fact rule)
  intents  manual intents stuck queued/claimed past TTL
  db       WAL size (pinner regression), disk free
  sweep    its own previous heartbeat age (a gap in the log IS the finding, cold-readable)

A PROBLEM IS (KEY, DETAIL). The key is stable and identifies the CONDITION; the detail carries the
measured value and goes to the human. Dedup keys on the key alone, because the first version keyed
on the whole message and every message embedded a drifting number ("heartbeat STALE 412s", then
"...1312s"): the key never matched, so a persistent problem paged every 15 minutes instead of every
2 hours, and the state file grew a dead entry per sweep. A pager that cries every quarter hour is
one nobody reads, which is the same failure as not paging at all.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from sniper.util.envfiles import load_env_files  # noqa: E402
load_env_files(ROOT)

HEARTBEAT = os.path.join(ROOT, "data", "watch_sweep.heartbeat")
STATE = os.path.join(ROOT, "data", "watch_sweep.state.json")
REPAGE_S = 7200            # an ongoing problem pages once, then every 2h — not every sweep
WAL_LIMIT = 250_000_000
DISK_MIN_FREE_GB = 40
RUNNER_HEARTBEAT = os.path.join(ROOT, "data", "memecoin_runner.heartbeat")
BLIND_MIN_BUILDS = 3       # an unmeasured exotic hop label pages once it signed the blind CU budget this often


def _now():
    return datetime.now(timezone.utc)


def check_runner(problems):
    """Appends (key, detail). See the module docstring on why the key must not carry a number."""
    p = os.path.join(ROOT, "data", "memecoin_runner.heartbeat")
    try:
        hb = json.load(open(p))
        age = time.time() - os.path.getmtime(p)
        if age > 90:
            problems.append(("runner_heartbeat_stale", "runner heartbeat STALE %.0fs" % age))
            return
        if not hb.get("auth_ok"):
            problems.append(("runner_auth", "runner auth_ok FALSE"))
        # bracket_ts = last SUCCESSFUL exit sweep. A fresh poll loop with a dead sweep is the
        # known failure the plain age check cannot see.
        bt = hb.get("bracket_ts")
        if bt and time.time() - float(bt) > 300:
            problems.append(("exit_sweep_stale",
                             "exit sweep stale %.0fs (poll loop alive)"
                             % (time.time() - float(bt))))
        ro = hb.get("rails_observability") or {}
        if (ro.get("blind_positions") or 0) > 0:
            problems.append(("rails_blind", "RAILS-BLIND %s position(s)" % ro["blind_positions"]))
    except Exception as e:
        problems.append(("runner_heartbeat_unreadable", "runner heartbeat unreadable: %s" % e))


def check_web(problems):
    try:
        st = subprocess.run(["systemctl", "show", "sniper-web.service", "-p", "ActiveState",
                             "-p", "MainPID", "-p", "ExecMainStartTimestampMonotonic"],
                            capture_output=True, text=True).stdout
        vals = dict(line.split("=", 1) for line in st.strip().splitlines() if "=" in line)
        if vals.get("ActiveState") != "active":
            problems.append(("web_service", "web service %s" % vals.get("ActiveState")))
            return
        # TWO-FACT rule (e0ddb92d): the pointer AND the process start. A process from before a
        # flip serves old code while `current` looks correct; a moved pointer with no restart is
        # exactly the 2026-08-26 miss.
        cur = os.path.realpath(os.path.join(ROOT, ".runtime", "web", "current"))
        flip_age = time.time() - os.lstat(os.path.join(ROOT, ".runtime", "web", "current")).st_mtime
        pid = vals.get("MainPID", "0")
        if pid and pid != "0":
            with open("/proc/%s/stat" % pid) as f:
                start_ticks = int(f.read().split(")")[-1].split()[19])
            hz = os.sysconf("SC_CLK_TCK")
            with open("/proc/uptime") as f:
                uptime = float(f.read().split()[0])
            proc_age = uptime - start_ticks / hz
            if proc_age > flip_age + 5:
                problems.append((
                    "web_pre_flip",
                    "web serving a PRE-FLIP process (proc %.0fs old, flip %.0fs ago) "
                    "— restart is not a deploy" % (proc_age, flip_age)))
    except Exception as e:
        problems.append(("web_check_failed", "web check failed: %s" % e))


def check_db(problems):
    try:
        cutoff = (_now() - timedelta(minutes=5)).isoformat()
        c = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "sniper_tracker.db"), uri=True)
        stuck = c.execute(
            "SELECT COUNT(*) FROM memecoin_manual_intents "
            "WHERE state IN ('queued','claimed') AND requested_at < ?", (cutoff,)).fetchone()[0]
        if stuck:
            problems.append(("intents_stuck", "manual intents STUCK: %d" % stuck))
        c.close()
        wal = os.path.getsize(os.path.join(ROOT, "sniper_tracker.db-wal"))
        if wal > WAL_LIMIT:
            problems.append(("wal_size", "WAL %.0fMB (pinner regression?)" % (wal / 1e6)))
        st = os.statvfs(ROOT)
        free_gb = st.f_bavail * st.f_frsize / 1e9
        if free_gb < DISK_MIN_FREE_GB:
            problems.append(("disk_free", "disk %.0fGB free" % free_gb))
    except Exception as e:
        problems.append(("db_check_failed", "db check failed: %s" % e))


def check_rpc_cold(problems):
    """Cold-reach the funded RPC pool from OUTSIDE the runner.

    The runner's heartbeat reports pool health from its own warm connections — a process that
    already holds keep-alives cannot notice that fresh connections fail (DNS, egress, provider
    ban). 2026-08-28: an isolated verifier could not reach the endpoints while the heartbeat
    read healthy. Ask the configured endpoints cold until one returns a healthy JSON-RPC reply.
    An exhausted member is not a cold-connectivity outage of the pool; the runner separately
    pages credit exhaustion. Error details contain indexes only, never credential-bearing URLs.
    """
    try:
        from sniper.settings.memecoin import solana_funded_rpc_urls
        from sniper.execution.solana_wallet import _http
        urls = solana_funded_rpc_urls()
    except Exception as e:
        problems.append(("rpc_cold_unreachable",
                         "funded RPC pool cold probe configuration failed: %s" % type(e).__name__))
        return
    failures = []
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getHealth", "params": []})
    for idx, url in enumerate(urls):
        try:
            status, payload, _ = _http("POST", url, body=body, timeout=8)
        except Exception as e:
            failures.append("%d:%s" % (idx + 1, type(e).__name__))
            continue
        if (status == 200 and isinstance(payload, dict)
                and payload.get("error") is None and payload.get("result") == "ok"):
            return
        failures.append("%d:invalid_health_reply" % (idx + 1))
    if failures:
        problems.append(("rpc_cold_unreachable",
                         "funded RPC pool: no endpoint passed a cold health probe (%s)"
                         % "; ".join(failures)))


def check_gas(problems):
    """The funded wallet's SOL, checked from OUTSIDE the runner.

    2026-08-25: entries were gas-blocked from 18:35 until a manual top-up the next day, and the
    only symptom was a quiet tape. The runner's own top-up can fail silently (RPC flake, USDC
    encumbered, daily cap hit) — so the external clock reads the balance cold. Pages below the
    top-up floor's own trigger, i.e. only when the automatic layer has already failed to act."""
    try:
        from functools import partial
        from sniper.execution.solana_wallet import load_keypair, sol_balance_read, _http
        from sniper.execution.solana_rpc_pool import RotatingSolanaRpcHttp
        from sniper.settings.memecoin import (
            solana_funded_rpc_urls, MEMECOIN_GAS_TOPUP_LOW_SOL as _floor)
        kp = load_keypair(os.environ.get("MEMECOIN_SOLANA_KEYPAIR",
                                         os.path.join(ROOT, "data", "solana_keypair.json")))
        owner = str(kp.pubkey())
        urls = solana_funded_rpc_urls()
        # This independent cron read gets the runner's read-only failover semantics, including
        # the public fallback. Never mutate the runner's pool or interpret a failed read as zero.
        http = RotatingSolanaRpcHttp(partial(_http, timeout=8), urls, name="watch-sweep")
        balance = sol_balance_read(owner, urls[0], http=http)
        if not balance.ok:
            problems.append(("gas_check_failed",
                             "gas check failed: balance unknown (%s)" % balance.failure))
            return
        sol = balance.balance
        # page only when we are BELOW the auto-top-up trigger — that means the automatic layer
        # had its chance and did not act (cap hit, USDC encumbered, or the loop is broken).
        if sol < float(_floor) * 0.75:
            problems.append(("funded_gas_low",
                             "funded SOL %.4f is below the top-up trigger (%.3f) — the automatic "
                             "top-up has not acted; entries gas-block soon" % (sol, float(_floor))))
    except Exception as e:
        problems.append(("gas_check_failed", "gas check failed: %s" % type(e).__name__))


def check_sweep_gap(problems):
    """Did the sweep itself stop running?

    It cannot page about its own death — nothing inside a dead process runs. What it CAN do is
    notice, on the run that comes back, that it was gone: cron skipped, the host slept, someone
    disabled the line. That gap is the finding, and reporting it late is worth far more than not
    reporting it. The docstring promised this check from the first version; main() never called it.
    """
    try:
        gap = time.time() - os.path.getmtime(HEARTBEAT)
    except OSError:
        return                      # first ever run has no predecessor, which is not a problem
    if gap > 3 * 900 + 120:         # three missed 15-minute slots plus slack
        problems.append(("sweep_gap",
                         "the watch sweep did not run for %.0f min — cron gap, not a quiet system"
                         % (gap / 60.0)))


def new_blind_labels(seen):
    """[(label, builds)] for unmeasured exotic hop labels the runner counted (heartbeat
    slot_lane.exotic_cu_blind, settings._count_blind_hops) at least BLIND_MIN_BUILDS times and never
    paged before. A label pages once, ever: the counter is cumulative and resets on a runner restart,
    so a REPAGE_S problem would repeat forever. Unreadable or odd input is no label."""
    try:
        counts = (json.load(open(RUNNER_HEARTBEAT)).get("slot_lane") or {}).get("exotic_cu_blind")
        if not isinstance(counts, dict):
            return []
        return sorted((str(k), v) for k, v in counts.items()
                      if type(v) is int and v >= BLIND_MIN_BUILDS and str(k) not in seen)
    except Exception:
        return []


def page(text):
    try:
        from sniper.services.memecoin_telegram import send_telegram
        ok, _elapsed_ms = send_telegram("🔭 WATCH SWEEP: " + text)
        return ok is True
    except Exception:
        return False


def main():
    problems = []
    check_sweep_gap(problems)       # BEFORE the heartbeat is rewritten below
    check_runner(problems)
    check_web(problems)
    check_db(problems)
    check_rpc_cold(problems)
    check_gas(problems)

    # Dedup: page NEW problems immediately; ongoing ones repage every REPAGE_S.
    state = {}
    try:
        state = json.load(open(STATE))
    except Exception:
        pass
    last_paged = state.get("last_paged", {})
    blind_paged = set(state.get("blind_labels_paged") or [])
    blind = new_blind_labels(blind_paged)
    if blind and page("exotic CU BLIND hop label(s) signing the blind CU budget: " + ", ".join(
            "%s x%d" % (label, n) for label, n in blind) + " -- measure and add a budget"):
        blind_paged |= {label for label, _n in blind}
    now_ts = time.time()
    keys = [k for k, _d in problems]
    to_page = [(k, d) for k, d in problems if now_ts - last_paged.get(k, 0) > REPAGE_S]
    paged = False
    if to_page:
        paged = page("; ".join(d for _k, d in to_page))
        if paged:
            for k, _d in to_page:
                last_paged[k] = now_ts
    # Prune resolved conditions so a recurrence pages fresh — by KEY, so a drifting measurement
    # cannot leave an immortal entry behind.
    last_paged = {k: v for k, v in last_paged.items() if k in keys}

    tmp = STATE + ".tmp"
    json.dump({"last_paged": last_paged, "blind_labels_paged": sorted(blind_paged)}, open(tmp, "w"))
    os.replace(tmp, STATE)
    tmp = HEARTBEAT + ".tmp"
    json.dump({"ts": _now().isoformat(),
               "problems": [d for _k, d in problems],
               "keys": keys, "paged": paged}, open(tmp, "w"))
    os.replace(tmp, HEARTBEAT)
    print("OK" if not problems else "PROBLEMS: " + "; ".join(d for _k, d in problems))


if __name__ == "__main__":
    main()
