#!/usr/bin/env python3
"""One-shot $25 funded probe for the impulse roster (operator-run).

    python3 scripts/research/impulse_oneshot_arm.py arm      # operator pulls the trigger
    python3 scripts/research/impulse_oneshot_arm.py watch    # auto-started by arm
    python3 scripts/research/impulse_oneshot_arm.py disarm   # manual rollback any time
    python3 scripts/research/impulse_oneshot_arm.py status

ARM: backs up .env, sets fixed $25 sizing + sub-1M mcap ceiling + 15% chase cap, pauses
Rowdy for a clean single-arm experiment, arms the 6 impulse wallets on Solana (force
override, audited), restarts the pinned runner, verifies health, and starts the watcher.

WATCH: waits for the FIRST new real position from the roster, then runs the full disarm.
A 24h timeout also disarms. DISARM: revokes the 6, restores the .env backup, unpauses
Rowdy, restarts, verifies. The open position (if any) keeps its frozen exit policy.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:               # bare `python3 scripts/research/X.py`
    sys.path.insert(0, str(REPO))           # puts scripts/research on path, not repo
from sniper.persistence.db_core import get_readonly_connection
sys.path.insert(0, str(REPO))
ENV = REPO / ".env"
ENV_BACKUP = REPO / "data" / ".env.impulse-oneshot.backup"
STATE = REPO / "data" / "impulse_oneshot.state.json"
HEARTBEAT = REPO / "data" / "memecoin_runner.heartbeat"
RUNNER_PID = REPO / "data" / "memecoin_runner.pid"
LOG = REPO / "data" / "impulse_oneshot.log"

ROSTER = {
    "eb960be1-cca6-5bef-9977-d7c63ed46d3b": "MasterChief117",
    "f62ddfdd-eeb1-5749-ad3b-a4cd65c319a9": "jester",
    "aefe2ddd-c580-5245-a2f5-e4ed62f7ef10": "frankdegods",
    "d676a05f-0b3e-57d1-86dc-d6986927c6c8": "Aurelius0121",
    "c05654ac-2ce2-513d-be13-76b81b34a6e1": "Thepennyflippe",
    "5e911042-e839-569e-ab0e-2ab7c4389b95": "casino847",
}
ROWDY = "e89522ff-73b5-52f3-b1e3-ef8f60e90775"
ENV_SET = {
    "MEMECOIN_REAL_SIZING": "fixed",
    "MEMECOIN_TRADE_SIZE_USD": "25",
    "MEMECOIN_REAL_MAX_MCAP_USD": "1000000",
    "MEMECOIN_REAL_CHASE_MAX_PCT": "15",
}
REASON = "impulse-roster one-shot $25 probe 2026-08-20 (operator order; evidence: 30s/60s impulse screen f2bd6872, not copy-PnL)"


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def conn_rw():
    # wal-ok: read-WRITE handle; the read-only helper cannot serve a writer. Callers must close it.
    c = sqlite3.connect(REPO / "sniper_tracker.db", timeout=30)
    c.row_factory = sqlite3.Row
    return c


def rewrite_env(updates):
    lines = ENV.read_text().splitlines()
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in updates and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={updates[key]}"
            seen.add(key)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    ENV.write_text("\n".join(lines) + "\n")


def restart_runner():
    pid = RUNNER_PID.read_text().strip() if RUNNER_PID.exists() else ""
    if pid and Path(f"/proc/{pid}").exists():
        log(f"SIGTERM runner pid={pid}")
        os.kill(int(pid), 15)
    log("waiting for the cron watchdog relaunch (<=90s) ...")
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(5)
        try:
            hb = json.loads(HEARTBEAT.read_text())
            new_pid = RUNNER_PID.read_text().strip()
            if new_pid and new_pid != pid and Path(f"/proc/{new_pid}").exists() \
                    and time.time() - hb["ts"] < 30 and hb.get("auth_ok"):
                log(f"runner healthy pid={new_pid} hb_age={time.time()-hb['ts']:.0f}s")
                return True
        except Exception:
            pass
    log("RUNNER DID NOT COME BACK HEALTHY — investigate now (watchdog log, heartbeat)")
    return False


def set_armed(c, uid, armed):
    from sniper.persistence.db_memecoin import set_wallet_chain_armed
    out = set_wallet_chain_armed(c, uid, "solana", armed, actor="operator",
                                 reason=REASON, force=True)
    log(f"{'arm' if armed else 'disarm'} {ROSTER.get(uid, uid)}: {out['action']} chains={out['chains']}")
    return out


def arm():
    if STATE.exists():
        print("state file exists — already armed? run status/disarm first"); sys.exit(1)
    c = conn_rw()
    open_now = c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE closed_at IS NULL "
                         "AND execution_mode='real'").fetchone()[0]
    if open_now:
        print(f"refusing: {open_now} real position(s) already open"); sys.exit(1)
    baseline = c.execute("SELECT COALESCE(MAX(id),0) FROM memecoin_positions").fetchone()[0]
    shutil.copy2(ENV, ENV_BACKUP)
    log(f"env backed up to {ENV_BACKUP}")
    rewrite_env(ENV_SET)
    log(f"env set: {ENV_SET}")
    c.execute("UPDATE memecoin_wallets SET paused=1 WHERE source_user_id=?", (ROWDY,))
    c.commit()
    log("Rowdy paused for the experiment (unpaused at disarm)")
    for uid in ROSTER:
        set_armed(c, uid, True)
    STATE.write_text(json.dumps({"armed_at": time.time(), "baseline_position_id": baseline}))
    if not restart_runner():
        print("ABORT: runner unhealthy. Run disarm to roll back."); sys.exit(1)
    subprocess.Popen([sys.executable, __file__, "watch"],
                     stdout=open(LOG, "a"), stderr=subprocess.STDOUT,
                     start_new_session=True)
    log("watcher started; roster disarms after the FIRST entry or 24h")
    status()


def disarm():
    c = conn_rw()
    for uid in ROSTER:
        try:
            set_armed(c, uid, False)
        except Exception as exc:
            log(f"disarm {ROSTER[uid]} failed: {exc!r}")
    c.execute("UPDATE memecoin_wallets SET paused=0 WHERE source_user_id=?", (ROWDY,))
    c.commit()
    log("Rowdy unpaused")
    if ENV_BACKUP.exists():
        shutil.copy2(ENV_BACKUP, ENV)
        log("env restored from backup")
    STATE.unlink(missing_ok=True)
    restart_runner()
    status()


def watch():
    if not STATE.exists():
        return
    state = json.loads(STATE.read_text())
    baseline = state["baseline_position_id"]
    log(f"watch: baseline position id {baseline}")
    c = sqlite3.connect(f"file:{REPO/'sniper_tracker.db'}?mode=ro", uri=True)
    while True:
        if not STATE.exists():          # manual disarm happened
            return
        row = c.execute(
            "SELECT id, mint FROM memecoin_positions WHERE id > ? AND execution_mode='real' "
            "ORDER BY id LIMIT 1", (baseline,)).fetchone()
        if row:
            log(f"FIRST POSITION observed id={row[0]} mint={row[1]} — disarming roster")
            break
        if time.time() - state["armed_at"] > 86400:
            log("24h timeout with no entry — disarming roster")
            break
        time.sleep(5)
    c.close()
    disarm()


def status():
    c = get_readonly_connection(str(REPO / "sniper_tracker.db"))
    c.row_factory = sqlite3.Row
    print("\narmed wallets:")
    for r in c.execute("SELECT handle, real_chains, paused FROM memecoin_wallets "
                       "WHERE real_approved=1"):
        print(f"  {r['handle']:<18} chains={r['real_chains']} paused={r['paused']}")
    for r in c.execute("SELECT id, mint, entry_notional, opened_at, closed_at FROM "
                       "memecoin_positions WHERE execution_mode='real' "
                       "ORDER BY id DESC LIMIT 3"):
        print(f"  pos {r['id']} {r['mint'][:10]} ${r['entry_notional']} "
              f"opened={r['opened_at']} closed={r['closed_at']}")
    try:
        hb = json.loads(HEARTBEAT.read_text())
        print(f"heartbeat age {time.time()-hb['ts']:.0f}s auth_ok={hb.get('auth_ok')}")
    except Exception as exc:
        print(f"heartbeat unreadable: {exc!r}")
    print(f"state: {'ARMED (one-shot pending)' if STATE.exists() else 'not armed'}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"arm": arm, "disarm": disarm, "watch": watch, "status": status}[cmd]()
