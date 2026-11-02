#!/usr/bin/env python3
"""Health check for the CURRENT Sniper stack (re-scoped 2026-07-11).

The previous 2,000-line version checked the FROZEN legacy stack (systemd services,
world-analyst timers, analyzer SLAs) and produced ~13 ❌ of alarm noise while checking
nothing that actually runs. It is archived at scripts/legacy/brain_health_legacy_2026-04.py.
This one checks what exists today:

  memecoin runner (REAL MONEY) · web dashboard · watchdog crons · feed freshness
  (corrected real-UTC) · real-book sanity · capture pipelines · disk · brain reachability

Exit: 0 = healthy, 1 = warnings, 2 = failures.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
try:
    from sniper.util.envfiles import load_env_files
    load_env_files(ROOT)   # .env + .fomo.env — so the supabase/brain reachability check works
except Exception:
    pass

RESULTS = []  # (status, check, detail)  status in OK/WARN/FAIL/INFO


def add(status, check, detail=""):
    RESULTS.append((status, check, detail))


def _now_real():
    """Corrected real-UTC — the host clock runs ~+7h ahead, and all data stamps are corrected
    real-UTC, so ages MUST diff against the same axis or every age inflates by the offset."""
    try:
        from sniper.util.clock import real_utc_now_iso
        return datetime.fromisoformat(real_utc_now_iso().replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def _age_s(iso):
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return (_now_real() - dt).total_seconds()
    except (ValueError, TypeError):
        return None


def check_runner():
    try:
        hb = json.load(open("data/memecoin_runner.heartbeat"))
        age = time.time() - hb["ts"]
        if age > 120:
            add("FAIL", "memecoin runner heartbeat", f"{age:.0f}s stale (real money unmanaged!)")
        elif age > 30:
            add("WARN", "memecoin runner heartbeat", f"{age:.0f}s")
        else:
            add("OK", "memecoin runner heartbeat", f"{age:.0f}s")
        if not hb.get("auth_ok"):
            add("FAIL", "fomo auth", "auth_dead — feed blind, replace the Privy token")
        bts = hb.get("bracket_ts")
        if bts and time.time() - bts > 90:
            add("FAIL", "SL/TP sweep", f"{time.time()-bts:.0f}s since last sweep — stops are dead")
        else:
            add("OK", "SL/TP sweep", "fresh")
        # A stale on-chain volume feed costs coverage, not money: the gate falls back to the provider.
        for chain, feed in sorted((hb.get("onchain_volume_feed") or {}).items()):
            feed_age = feed.get("feed_age_s")
            feed_age = None if feed_age is None else feed_age + age
            detail = (f"{'no header' if feed_age is None else f'{feed_age:.0f}s'} since last header, "
                      f"resets in 24 h {feed.get('resets_24h')}")
            add("OK" if feed_age is not None and feed_age <= 120 else "WARN",
                f"on-chain m5 volume feed {chain}", detail)
    except Exception as e:
        add("FAIL", "memecoin runner heartbeat", f"unreadable: {e}")


def check_kill_switch():
    if os.path.exists("data/memecoin_kill_switch"):
        add("WARN", "kill switch", "TRIPPED — real entries halted (exits still run)")
    else:
        add("OK", "kill switch", "clear — real entries enabled")


def check_web():
    try:
        code = urllib.request.urlopen("http://127.0.0.1:5000/api/memecoin/health",
                                      timeout=10).status
        add("OK", "web dashboard", f"http {code}")
    except urllib.error.HTTPError as e:
        # 401 = auth wall answering = server alive
        add("OK" if e.code == 401 else "WARN", "web dashboard", f"http {e.code}")
    except Exception as e:
        add("FAIL", "web dashboard", f"unreachable: {e}")


def check_crons():
    try:
        tab = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except Exception as e:
        add("FAIL", "crontab", str(e))
        return
    for name, pat in [("memecoin watchdog", "memecoin_watchdog"),
                      ("web watchdog", "sniper_web_watchdog"),
                      ("crypto5m watchdog", "crypto5m_watchdog"),
                      ("brain sync (daily)", "brain_sync")]:
        add("OK" if pat in tab else "WARN", f"cron: {name}",
            "" if pat in tab else "missing from crontab")


def check_feed_and_captures(conn, runner_ok):
    # Cross-process corrected-clock arithmetic is fragile (a negative age = clock-domain
    # mismatch, not a future event). So: a FRESH runner heartbeat already proves the feed is
    # being polled (same-domain, reliable). Only alarm on firehose staleness when the runner is
    # ALSO down — otherwise report row counts as INFO and defer liveness to the heartbeat.
    row = conn.execute("SELECT COUNT(*), MAX(received_at) FROM memecoin_firehose").fetchone()
    n, newest = row[0], row[1]
    age = _age_s(newest)
    if n == 0:
        add("WARN", "feed firehose", "no rows")
    elif runner_ok:
        add("OK", "feed firehose", f"{n} rows (runner heartbeat proves liveness)")
    elif age is not None and age > 3600:
        add("FAIL", "feed firehose", f"runner down AND newest event {age/60:.0f}m old")
    else:
        add("WARN", "feed firehose", "runner down — feed freshness unverifiable")
    qn = conn.execute("SELECT COUNT(*) FROM memecoin_quote_ledger").fetchone()[0]
    add("INFO", "quote ledger", f"{qn} observations")


def check_real_book(conn):
    stuck = conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE is_open=1 "
                         "AND execution_mode='real' AND stuck_bag=1").fetchone()[0]
    add("OK" if stuck == 0 else "FAIL", "stuck bags", str(stuck))
    resv = conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE "
                        "status='reserved'").fetchone()[0]
    add("OK" if resv == 0 else "WARN", "live reservations",
        f"{resv} (in-flight or pending recovery)")
    opens = conn.execute("SELECT COUNT(*), COALESCE(SUM(entry_notional),0) FROM "
                         "memecoin_positions WHERE is_open=1 AND execution_mode='real'").fetchone()
    add("INFO", "real book", f"{opens[0]} open / ${opens[1]:.2f} deployed")
    day = _now_real().strftime("%Y-%m-%d")
    # LABELLED PAIR (2026-08-27). "real realized today" was all-money and read as the system's
    # result; on 08-27 that was +$10.10 against +$74.18 of robot performance, the operator's own
    # manual entries making up the difference. Both are shown and both are named — an unlabelled
    # single number here is what sent the operator looking for a system fault that was his own picks.
    from sniper.persistence.db_memecoin import robot_performance_sql, real_money_sql
    def _pnl(where):
        return conn.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM memecoin_positions "
                            "WHERE is_open=0 AND substr(closed_at,1,10)=? AND " + where,
                            (day,)).fetchone()[0]
    pnl = _pnl(real_money_sql())
    robot = _pnl(robot_performance_sql())
    add("INFO", "robot realized today", f"${robot:+.2f}")
    add("INFO", "all-money realized today", f"${pnl:+.2f} (incl. manual; breaker reads this)")
    armed = conn.execute("SELECT COUNT(*) FROM memecoin_wallets WHERE COALESCE(real_approved,0)=1 "
                         "AND paused=0 AND COALESCE(excluded,0)=0").fetchone()[0]
    add("INFO", "armed wallets", str(armed))


def check_clock():
    try:
        from sniper.util.clock import offset_s
        add("INFO", "host clock offset", f"{offset_s():+.0f}s vs real UTC (data stamps corrected)")
    except Exception:
        add("WARN", "host clock offset", "clock module unreadable")


def check_disk():
    st = os.statvfs("/")
    free_gb = st.f_bavail * st.f_frsize / 1e9
    add("OK" if free_gb > 5 else ("WARN" if free_gb > 2 else "FAIL"),
        "disk free (/)", f"{free_gb:.1f} GB")
    try:
        add("INFO", "sniper_tracker.db", f"{os.path.getsize('sniper_tracker.db')/1e6:.0f} MB")
    except OSError:
        pass


def check_brain():
    """Supabase second brain — light-touch, never a hard failure (research infra, not trading)."""
    try:
        from bot.supabase_helper import search_memories_fts
        hits = search_memories_fts("memecoin")
        add("OK", "supabase brain", f"reachable ({len(hits)} fts hits)")
    except Exception as e:
        add("WARN", "supabase brain", f"unavailable ({str(e)[:60]})")


def main():
    conn = sqlite3.connect(f"file:{ROOT}/sniper_tracker.db?mode=ro", uri=True)
    check_runner()
    runner_ok = not any(s == "FAIL" and "heartbeat" in c for s, c, _ in RESULTS)
    check_kill_switch()
    check_web()
    check_crons()
    check_feed_and_captures(conn, runner_ok)
    check_real_book(conn)
    check_clock()
    check_disk()
    check_brain()
    icons = {"OK": "✅", "WARN": "⚠️ ", "FAIL": "❌", "INFO": "ℹ️ "}
    for status, check, detail in RESULTS:
        print(f"{icons[status]} {check:<28} {detail}")
    fails = sum(1 for s, *_ in RESULTS if s == "FAIL")
    warns = sum(1 for s, *_ in RESULTS if s == "WARN")
    print(f"\n{'HEALTHY' if not fails and not warns else 'DEGRADED' if not fails else 'UNHEALTHY'}"
          f" — {fails} fail / {warns} warn")
    sys.exit(2 if fails else (1 if warns else 0))


if __name__ == "__main__":
    main()
