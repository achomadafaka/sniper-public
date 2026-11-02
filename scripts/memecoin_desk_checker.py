#!/usr/bin/env python3
"""Desk CHECKER seat worker (sniper/services/memecoin_desk_checker.py). Daily offline batch, never on
the fill path, never a veto. For one UTC day it takes the JUDGE's terminal decisions, draws the fixed
hash sample, and asks GPT through `codex exec` for one defect enum per sheet, blinded to the verdict.
Its one writer is data/desk/checker.db (flock); the desk DB and the funded ledger are read read-only,
the verdict and the outcome are recorded next to the answer. At most --calls-per-day calls; a sheet
already checked is never re-asked. MEMECOIN_DESK_CHECKER_ENABLED=0 stops it.

`codex exec` runs under bwrap with the home directory, the state root, /tmp and /run/user hidden (only
the codex auth, its install and a scratch dir bound back), read-only, ephemeral, repo rules ignored,
stdin closed (it hangs otherwise), a JSON schema that admits only the enum, and a cleared environment
(PATH, HOME, LANG and CODEX_HOME only; the loaded .env never reaches it). No bwrap, no call.
A COPY of ~/.codex/auth.json stays readable inside: accepted for this seat, since it gets no free
text and the only party the model could send that token to is OpenAI, which issued it. bwrap here does
NOT unshare the network (the API needs it), so confidentiality rests on the token being short-lived and
the model having no usable tool. A rotated token is written back to the real file only when the copy is
the same account with only rotating fields changed (auth_writeback_refusal). Rotate the codex login if a
run ever shows the model reading files it was not given.

    python3 scripts/memecoin_desk_checker.py [--day YYYY-MM-DD] [--calls-per-day 20]
    # cron (operator): 45 0 * * * cd /home/bigpapi/Desktop/Sniper/.runtime/memecoin/current && SNIPER_STATE_ROOT=/home/bigpapi/Desktop/Sniper /home/bigpapi/.pyenv/versions/3.13.0/bin/python3 scripts/memecoin_desk_checker.py >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_checker.log 2>&1
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.services import memecoin_desk_checker as C       # noqa: E402

CLI_FAIL_STOP = 3     # consecutive exit_N answers end the run: the CLI is broken, not the sheet

DDL = ("CREATE TABLE IF NOT EXISTS checks(ticket_id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL, "
       "cohort_id TEXT, checker_version TEXT NOT NULL, model TEXT NOT NULL, u REAL NOT NULL, day TEXT NOT NULL, "
       "defect TEXT, call_error TEXT, latency_ms REAL, strings_dropped INTEGER, verdict_action TEXT, "
       "verdict_reason TEXT, reason_code INTEGER, ticket_state TEXT, realized_pnl REAL, entry_notional REAL, "
       "checked_at REAL NOT NULL)")


def isolation(tmp, codex, codex_home=None):
    """bwrap argv that hides the filesystem from the codex process (Gamma, 7c2a26f9a): a read-only
    sandbox can still run `cat`, and whatever it reads joins the conversation sent to the provider. The
    home directory (the repo, its .env, every key file), the state root, /tmp and /run/user are empty
    tmpfs; only the codex install and the scratch dir are bound back. codex-cli 0.154 needs a WRITABLE
    CODEX_HOME (read-only: 'failed to initialize in-process app-server client'), so the real ~/.codex is
    never bound: each call gets its own CODEX_HOME inside the scratch dir holding a copy of auth.json."""
    from sniper.util.state_paths import state_root
    home = Path.home()
    hidden = [str(home), "/tmp", "/run/user"]
    root = str(Path(state_root()).resolve())
    if not root.startswith(str(home) + "/"):
        hidden.append(root)
    argv = ["bwrap", "--ro-bind", "/", "/"]
    for d in hidden:
        argv += ["--tmpfs", d]
    install = str(Path(os.path.realpath(codex)).parent)
    for d in (str(home / ".npm-global"), install, str(Path(codex).parent)):
        if Path(d).exists():
            argv += ["--ro-bind", d, d]
    return argv + ["--clearenv"] + [x for k, v in sorted(minimal_env(codex_home).items()) for x in ("--setenv", k, v)] + [
        "--bind", tmp, tmp, "--dev", "/dev", "--proc", "/proc", "--unshare-pid",
                   "--die-with-parent", "--chdir", tmp, "--"]


def minimal_env(codex_home=None):
    """The whole environment codex gets (Gamma, 727794300): main() has loaded the state root's .env,
    so os.environ holds every RPC key, J7 token and Telegram secret, and bwrap passes it through;
    inside the sandbox `env` would put them all in the conversation. Only PATH, HOME, the locale and
    the per-call CODEX_HOME pass."""
    # exact names, never a prefix: a non-codex secret under a CODEX_ name would pass (Gamma LOW)
    keep = {} if codex_home is None else {"CODEX_HOME": str(codex_home)}   # never inherited from the caller
    keep.update({"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(Path.home()),
                 "LANG": os.environ.get("LANG", "C.UTF-8")})
    return keep


class CodexBusy(Exception):
    pass


def codex_lock_path():
    from sniper.util.state_paths import state_path
    return Path(state_path("data", "desk", "codex_call.lock"))


@__import__("contextlib").contextmanager
def codex_slot(timeout_s=120.0, path=None, poll_s=0.5):
    """ONE codex call at a time across the CHECKER cron and the GPT seat/replay (one ChatGPT login, one auth file):
    an exclusive flock held for the duration of a single `codex exec`. A second caller WAITS (polling) up to
    `timeout_s`, then raises CodexBusy; neither side is ever refused outright, unlike the CHECKER's run lock."""
    path = Path(path or codex_lock_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    waited = 0.0
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if waited >= timeout_s:
                    raise CodexBusy()
                time.sleep(poll_s)
                waited += poll_s
        yield
    finally:
        fh.close()                                             # closing the descriptor releases the flock


def prepare_codex_home(tmp, real_home=None):
    """tmp/codex-home (0700) holding only a copy of auth.json (0600). config.toml, sessions, plugins and
    memories of the real ~/.codex are NOT copied. Returns (codex_home, auth_copy) or (None, None) when
    the real login is missing."""
    real = Path(real_home or Path.home() / ".codex") / "auth.json"
    if not real.is_file():
        return None, None
    home = Path(tmp) / "codex-home"
    home.mkdir(mode=0o700)
    copy = home / "auth.json"
    shutil.copyfile(real, copy)
    copy.chmod(0o600)
    _INITIAL_SHA[str(copy)] = hashlib.sha256(copy.read_bytes()).hexdigest()   # in memory: the jail cannot edit it
    while len(_INITIAL_SHA) > 64:                              # a scratch path is used once; keep the table bounded
        _INITIAL_SHA.pop(next(iter(_INITIAL_SHA)))
    return home, copy


_ROTATING_TOKENS = ("id_token", "access_token", "refresh_token")
_INITIAL_SHA = {}          # copy path -> sha256 of the real auth.json when the copy was made


def _jwt_sub(token):
    import base64
    try:
        body = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))).get("sub")
    except (AttributeError, IndexError, ValueError):
        return None


def auth_writeback_refusal(old, new):
    """None when `new` is the SAME login as `old` with only the rotating fields changed; else a slug.
    The copy sits in the jail's writable scratch dir, so whatever ran inside could have rewritten it: it
    must never be able to swap the account or strip the login (Gamma, 3bd255d01)."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return "not_an_object"
    if set(old) != set(new):
        return "keys_changed"
    for k in old:
        if k not in ("tokens", "last_refresh") and old[k] != new[k]:
            return "field_changed:" + k
    ot, nt = old.get("tokens"), new.get("tokens")
    if not isinstance(ot, dict) or not isinstance(nt, dict) or set(ot) != set(nt):
        return "tokens_shape"
    if ot.get("account_id") != nt.get("account_id") or not ot.get("account_id"):
        return "account_id_changed"
    for k in ot:
        if k not in _ROTATING_TOKENS and ot[k] != nt[k]:
            return "token_field_changed:" + k
    for k in _ROTATING_TOKENS:
        if k in nt and not (isinstance(nt[k], str) and nt[k]):
            return "token_empty:" + k
    if _jwt_sub(ot.get("id_token")) != _jwt_sub(nt.get("id_token")):
        return "subject_changed"
    if not isinstance(new.get("last_refresh"), str):
        return "last_refresh_shape"
    return None


def sync_auth_back(copy, real_home=None, page=None):
    """codex may rotate the refresh token inside the copy; a rotated token that is lost would log the real
    ~/.codex out. Returns 'unchanged', 'written', 'skipped:real_rotated_meanwhile' (the real file changed while the jail ran: keep it) or 'rejected:<why>'. A write needs the SAME account and
    only rotating fields changed (auth_writeback_refusal); the previous file is kept as auth.json.pre-checker
    (0600) before an atomic rename. A mismatch leaves the real login alone and pages a defect."""
    real = Path(real_home or Path.home() / ".codex") / "auth.json"
    initial = _INITIAL_SHA.get(str(copy))
    try:
        new_b, old_b = copy.read_bytes(), real.read_bytes()
    except OSError:
        return "rejected:unreadable"
    if initial is None:
        return "rejected:no_initial_hash"                  # a copy this process did not make
    if hashlib.sha256(new_b).hexdigest() == initial:
        return "unchanged"                                 # codex did not rotate inside the jail
    if hashlib.sha256(old_b).hexdigest() != initial:
        # another process (the crew's own codex sessions share this login) rotated the REAL file while the jail ran:
        # the jail's copy is STALE, not newer. Keep the real file, drop the copy.
        return "skipped:real_rotated_meanwhile"
    try:
        why = auth_writeback_refusal(json.loads(old_b), json.loads(new_b))
    except ValueError:
        why = "not_json"
    if why is not None:
        _page_defect("desk checker: codex auth write-back REFUSED (%s); real login untouched" % why, page)
        return "rejected:" + why
    try:
        backup = real.with_name("auth.json.pre-checker")
        backup.write_bytes(old_b)
        backup.chmod(0o600)
        stage = real.with_name(".auth.json.checker-%d" % os.getpid())
        with stage.open("wb") as staged:
            os.fchmod(staged.fileno(), 0o600)
            staged.write(new_b)
            staged.flush()
            os.fsync(staged.fileno())
        os.replace(stage, real)
        directory = os.open(real.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _INITIAL_SHA[str(copy)] = hashlib.sha256(new_b).hexdigest()   # the real file is now the copy's content
        return "written"
    except OSError:
        return "rejected:write_failed"


def _log_auth(status):
    """Every write-back outcome except the boring 'unchanged' goes to the cron log (stdout), so a rotation is auditable."""
    if status != "unchanged":
        print("[desk-checker] codex auth write-back: %s" % status, flush=True)


def _page_defect(text, page=None):
    try:
        if page is not None:
            page(text)
            return
        from sniper.services.memecoin_alerts import send_alert
        send_alert("DESK-CHECKER-AUTH-DEFECT", text)
    except Exception:                                    # noqa: BLE001 - a page failure never breaks the seat
        print("[desk-checker] " + text, file=sys.stderr)


def codex_call(prompt, *, timeout_s=C.DEADLINE_S):
    """(text, call_error, latency_ms). Never raises. Never runs codex without the bwrap isolation. One codex call at a
    time across this cron and the GPT seat (codex_slot); the login copy is made INSIDE the slot, so it is the freshest
    one, and the guarded write-back finishes before the next caller's slot starts."""
    codex = shutil.which("codex")
    if not codex:
        return None, "cli_missing", None                 # no call made: not counted against the cap
    if not shutil.which("bwrap"):
        return None, "no_isolation", None                # refuse: never an unisolated codex
    try:
        slot = codex_slot()
        slot.__enter__()
    except CodexBusy:
        return None, "codex_busy", None                  # no call made: not counted against the cap
    try:
        with tempfile.TemporaryDirectory(prefix="desk-checker-") as tmp:
            schema, out = Path(tmp) / "schema.json", Path(tmp) / "answer.json"
            schema.write_text(json.dumps(C.SCHEMA))
            codex_home, auth_copy = prepare_codex_home(tmp)
            if codex_home is None:
                return None, "no_codex_login", None         # no call made: not counted against the cap
            cmd = isolation(tmp, codex, codex_home) + [codex, "exec", "--sandbox", "read-only", "--ephemeral", "--ignore-rules",
                   "--skip-git-repo-check", "-C", tmp, "-m", C.MODEL, "--output-schema", str(schema), "-o", str(out), prompt]
            started = time.monotonic()
            try:
                proc = subprocess.run(cmd, cwd=tmp, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                      timeout=timeout_s, env=minimal_env(codex_home))
            except subprocess.TimeoutExpired:
                _log_auth(sync_auth_back(auth_copy))
                return None, "timeout", (time.monotonic() - started) * 1000
            latency = (time.monotonic() - started) * 1000
            _log_auth(sync_auth_back(auth_copy))
            if proc.returncode != 0:
                return None, "exit_%d" % proc.returncode, latency
            return (out.read_text() if out.exists() else None), None, latency
    finally:
        slot.__exit__(None, None, None)


def tickets_for_day(desk_db, day):
    from sniper.persistence import db_memecoin_desk as store
    since = "%sT00:00:00+00:00" % day
    until = (datetime.fromisoformat(since) + timedelta(days=1)).isoformat()
    conn = sqlite3.connect("file:%s?mode=ro" % desk_db, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        ids = store.list_tickets_with_stage(conn, stage="vet", since_iso=since, until_iso=until)
        tickets = [store.get_ticket(conn, i) for i in ids]
    finally:
        conn.close()
    return [t for t in tickets if t["state"] not in ("open", "waiting", "queued", "unresolved", "filled")]


def outcome(ledger_db, ticket):
    from sniper.persistence import db_memecoin as ledger
    from sniper.persistence.db_core import get_readonly_connection
    close = next((s for s in ticket["stages"] if s["stage"] == "close"), None)
    pid = (close or {}).get("position_id")
    if not pid or not Path(ledger_db).is_file():
        return None, None
    conn = get_readonly_connection(str(ledger_db))
    try:
        row = ledger.get_position(conn, pid)
    finally:
        conn.close()
    return (row["realized_pnl"], row["entry_notional"]) if row is not None else (None, None)


def run(desk_db, ledger_db, out_db, day, *, cap, call=codex_call, now_fn=time.time):
    from sniper.services import memecoin_desk_contract as contract
    out = sqlite3.connect(str(out_db), timeout=30)
    out.execute("PRAGMA journal_mode=WAL")
    out.execute(DDL)
    # a row with a call_error is NOT checked: it is retried (INSERT OR REPLACE below). exit_N rows made no
    # model answer (the CLI died), so they do not spend the daily cap; a timeout or invalid_answer did.
    done = {r[0] for r in out.execute("SELECT ticket_id FROM checks WHERE call_error IS NULL")}
    spent = out.execute("SELECT COUNT(*) FROM checks WHERE day=? AND latency_ms IS NOT NULL "
                        "AND (call_error IS NULL OR call_error NOT LIKE 'exit_%')", (day,)).fetchone()[0]
    stats = {"day": day, "sampled": 0, "checked": 0, "skipped_done": 0, "capped": 0, "cli_failures": 0}
    cli_streak = 0
    try:
        for u, t in C.sample(tickets_for_day(desk_db, day)):
            stats["sampled"] += 1
            if t["ticket_id"] in done:
                stats["skipped_done"] += 1
                continue
            if spent >= cap:
                stats["capped"] += 1
                continue
            if cli_streak >= CLI_FAIL_STOP:                  # codex itself is broken: stop calling, do not loop
                stats["stopped"] = "cli_failures"
                break
            text, err, latency = None, None, None
            try:
                prompt, dropped = C.prompt(contract.render_judge_sheet(t["candidate"]))
            except ValueError:
                prompt, dropped, err = None, None, "sheet_invalid"
            if prompt is not None:
                text, err, latency = call(prompt)
            defect = C.parse(text) if err is None else None
            if err is None and defect is None:
                err = "invalid_answer"
            verdict = C.judged(t)                       # recorded here, never shown to the checker
            pnl, notional = outcome(ledger_db, t)
            with out:
                out.execute("INSERT OR REPLACE INTO checks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (t["ticket_id"], t["opportunity_id"], t["cohort_id"], C.CHECKER_VERSION, C.MODEL, u,
                             day, defect, err, latency, dropped, verdict.get("action"), verdict.get("reason"),
                             verdict.get("reason_code"), t["state"], pnl, notional, now_fn()))
            cli_failed = err is not None and err.startswith("exit_")
            cli_streak = cli_streak + 1 if cli_failed else 0
            stats["cli_failures"] += cli_failed
            spent += latency is not None and not cli_failed
            stats["checked"] += 1
    finally:
        out.close()
    return stats


def main(argv=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root, state_path
    load_env_files(str(state_root()))
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--day", default=(datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat())
    p.add_argument("--calls-per-day", type=int,
                   default=int(os.getenv("MEMECOIN_DESK_CHECKER_CALLS_PER_DAY", "20") or 20))
    p.add_argument("--desk-db", default=str(state_path("data", "memecoin_desk.db")))
    p.add_argument("--ledger-db", default=os.getenv("DB_PATH") or str(state_path("sniper_tracker.db")))
    a = p.parse_args(argv)
    if os.getenv("MEMECOIN_DESK_CHECKER_ENABLED", "1").strip() == "0":
        print("[desk-checker] MEMECOIN_DESK_CHECKER_ENABLED=0; exiting")
        return 0
    if not Path(a.desk_db).is_file():
        print("[desk-checker] refused: desk DB missing")
        return 2
    desk_dir = Path(state_root()) / "data" / "desk"
    desk_dir.mkdir(parents=True, exist_ok=True)
    lock = open(desk_dir / "checker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-checker] another checker holds the lock; exiting")
        return 0
    print(json.dumps(run(a.desk_db, a.ledger_db, desk_dir / "checker.db", a.day, cap=max(0, a.calls_per_day))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
