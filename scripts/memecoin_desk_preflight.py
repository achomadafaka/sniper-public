#!/usr/bin/env python3
"""Desk cohort U ACTIVATION PREFLIGHT (docs/runbooks/2026-10-21-desk-u-activation.md). Read-only: it writes
nothing, makes no model call and changes no setting. In runbook order it prints one line per check,
PASS, WARN or BLOCKED, with the exact fix, and exits 1 when anything is BLOCKED.

.env knobs are checked by NAME only; no value is ever printed (an *_ENABLED knob is only compared with 1).
The Claude login is read with `claude auth status --json` (no model call); crontab with `crontab -l`.

    SNIPER_STATE_ROOT=/home/bigpapi/Desktop/Sniper /home/bigpapi/.pyenv/versions/3.13.0/bin/python3 scripts/memecoin_desk_preflight.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REQUIRED_KNOBS = ("MEMECOIN_DESK_ENABLED", "MEMECOIN_DESK_CLIP_USD", "MEMECOIN_DESK_CHAINS",
                  "MEMECOIN_DESK_MAX_CONCURRENT", "MEMECOIN_DESK_DAILY_LOSS_USD", "MEMECOIN_DESK_RAIL_BOUNDS",
                  "MEMECOIN_RESEARCH_HELIUS_RPC_URLS", "MEMECOIN_DESK_U_COHORT_ID", "MEMECOIN_DESK_SEAT_CALLS_PER_HOUR",
                  "MEMECOIN_DESK_CRAWLER_CALLS_PER_HOUR", "MEMECOIN_DESK_CRAWLER_CALLS_PER_DAY")
ENABLED_KNOBS = ("MEMECOIN_DESK_ENABLED", "MEMECOIN_DESK_CRAWLER_ENABLED", "MEMECOIN_DESK_UNIVERSE_ENABLED",
                 "MEMECOIN_DESK_X_PRODUCER_ENABLED", "MEMECOIN_DESK_ASSEMBLER_ENABLED", "MEMECOIN_DESK_JUDGE_ENABLED")
# runbook switch-on order; the JUDGE is last because it is the only one that queues money intents
REQUIRED_WATCHDOGS = ("memecoin_desk_crawler_watchdog.sh", "memecoin_desk_universe_watchdog.sh",
                      "memecoin_desk_x_producer_watchdog.sh", "memecoin_desk_cluster_watchdog.sh",
                      "memecoin_desk_ranker_watchdog.sh", "memecoin_desk_assembler_watchdog.sh",
                      "memecoin_desk_judge_watchdog.sh")
OPTIONAL_CRON = ("memecoin_desk_trending_watchdog.sh", "memecoin_desk_dexscreener_watchdog.sh",
                 "memecoin_desk_intake_pumpfun_watchdog.sh",
                 "memecoin_desk_intake_dexscreener_watchdog.sh",
                 "memecoin_desk_fomo_intake_watchdog.sh",
                 "memecoin_desk_paper_engine_watchdog.sh", "memecoin_desk_risk_watcher_paper_watchdog.sh",
                 "memecoin_desk_offchain_watchdog.sh", "memecoin_desk_risk_shadow.py", "memecoin_desk_postmortem.py",
                 "memecoin_desk_checker.py")
SNAPSHOT_READERS = ("memecoin_desk_assembler.py", "memecoin_desk_cluster_worker.py", "memecoin_desk_ranker_worker.py")
REQUIRED_WORKERS = ("U producer", "X producer", "crawler", "cluster", "ranker", "assembler", "JUDGE")


def env_knobs(path):
    """{name: value} from an env file; values stay in memory, never printed."""
    out = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip().removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
    return out


def _run(argv, timeout=20):
    try:
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")}
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
        return p.returncode, (p.stdout + p.stderr) if argv[:3] == ["gh", "auth", "status"] else (
            p.stdout if p.returncode == 0 else (p.stdout + p.stderr))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)


def check_login(auth=lambda: _run(["claude", "auth", "status", "--json"])):
    rc, out = auth()
    try:
        state = json.loads(out)
    except (TypeError, ValueError):
        return "BLOCKED", "claude CLI auth status unreadable (rc=%s)" % rc, "install / repair the claude CLI"
    if state.get("loggedIn") is True:
        return "PASS", "claude CLI logged in (%s)" % state.get("authMethod"), None
    return "BLOCKED", "claude CLI not logged in", "operator: run `claude` and /login (the JUDGE, crawler and seats need it)"


def check_knobs(knobs):
    if knobs is None:
        return "BLOCKED", "state root .env unreadable", "check SNIPER_STATE_ROOT and the .env file"
    missing = [k for k in REQUIRED_KNOBS if not knobs.get(k)]
    off = [k for k in ENABLED_KNOBS if knobs.get(k, "").strip() != "1"]
    if missing or off:
        return ("BLOCKED", "missing: %s; not 1: %s" % (", ".join(missing) or "none", ", ".join(off) or "none"),
                "operator: add the runbook §2 lines to .env")
    return "PASS", "%d required knobs present, %d enable knobs at 1" % (len(REQUIRED_KNOBS), len(ENABLED_KNOBS)), None


PREREG = ROOT / "docs" / "audits" / "2026-10-21-desk-u-ranker-preregistration.md"


def check_prereg(path=None):
    """The p_admit derivation (ranker pre-registration amendment 6) is frozen in THIS release before the
    cohort is provisioned, not after (Gamma)."""
    try:
        text = Path(path or PREREG).read_text()
    except OSError:
        return "BLOCKED", "ranker pre-registration missing from this release", "deploy a release that carries it"
    if not any(line.startswith("## Amendment 6") for line in text.splitlines()):
        return ("BLOCKED", "ranker pre-registration has no Amendment 6 (the judge_p_admit derivation)",
                "Gamma freezes amendment 6 and it ships in the release before provisioning")
    return "PASS", "ranker pre-registration carries Amendment 6", None


MEASUREMENT_FIELDS = ("judge_p_admit", "hard_filter", "sr_post_p", "sr_all_p", "crawler_version", "ranker_version",
                      "select_version", "label_version")


def measurement_rows(policy):
    """The cohort's frozen measurement fields, printed for the operator to compare with the decisions
    (none is a secret). judge_p_admit at exactly 1.0 is the dry-run placeholder: WARN."""
    rows = [("INFO", "cohort %s = %s" % (k, json.dumps(policy.get(k), sort_keys=True)), None)
            for k in MEASUREMENT_FIELDS]
    if policy.get("judge_p_admit") == 1.0:
        rows.append(("WARN", "judge_p_admit is exactly 1.0, the dry-run placeholder",
                     "provision with amendment 6's value (a new cohort)"))
    return rows


def check_cohort(knobs, desk_db, ledger_db):
    cid = (knobs or {}).get("MEMECOIN_DESK_U_COHORT_ID")
    if not cid:
        return "BLOCKED", "no MEMECOIN_DESK_U_COHORT_ID", "operator: provision (runbook §1) and add the printed id"
    from sniper.services import memecoin_desk_crawler as CR
    from sniper.services import memecoin_desk_judge as J
    from sniper.services import memecoin_desk_ranker as R
    from sniper.services import memecoin_desk_ranker_label as L
    found = {}
    for name, path, table in (("desk", desk_db, "desk_cohorts"), ("ledger", ledger_db, "memecoin_desk_cohorts")):
        if not Path(path).is_file():
            found[name] = None
            continue
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        try:
            row = conn.execute("SELECT policy_json FROM %s WHERE cohort_id=?" % table, (cid,)).fetchone()
            found[name] = json.loads(row[0]) if row else None
        except sqlite3.Error:
            found[name] = None
        finally:
            conn.close()
    if not found.get("desk") or not found.get("ledger"):
        return ("BLOCKED", "cohort not provisioned in %s" % " and ".join(k for k, v in found.items() if not v),
                "operator: scripts/memecoin_desk_provision_cohort.py ... --confirm (runbook §1)")
    policy = found["desk"].get("policy", found["desk"])
    trigger = policy.get("select_version") == J.TRIGGER_SELECT_VERSION      # the amendment-11 pair is this release's other valid one
    want = {"crawler_version": CR.CRAWLER_VERSION,
            "ranker_version": R.INLINE_RANKER_VERSION if trigger else R.RANKER_VERSION,
            "select_version": J.TRIGGER_SELECT_VERSION if trigger else J.SELECT_VERSION, "label_version": L.LABEL_VERSION}
    wrong = [k for k, v in want.items() if policy.get(k) != v]
    if trigger:                      # the cluster fact's definition is part of the trigger cohort: the worker's constants must equal it
        from sniper.services import memecoin_cluster_rpc as CL
        if policy.get("cluster") != CL.desk_definition(True):
            wrong.append("cluster")
        elif (knobs or {}).get("MEMECOIN_DESK_CLUSTER_FUNDED_BY", "1").strip().lower() in ("0", "false", "off", "no", ""):
            wrong.append("cluster funded_by knob (the cohort froze the fallback; the worker would run without it)")
    if wrong:
        return ("BLOCKED", "cohort policy versions differ from this release: %s" % ", ".join(wrong),
                "a new cohort (provision again with this release), never an edit")
    return ("PASS", "cohort provisioned in both stores; versions match this release (crawler %s)" % CR.CRAWLER_VERSION,
            None, policy)


def check_cron(crontab=lambda: _run(["crontab", "-l"])):
    rc, text = crontab()
    if rc is None or (rc != 0 and "no crontab" not in str(text)):
        first = (str(text).strip().splitlines() or [""])[0][:120]
        return [("BLOCKED", "crontab -l failed (rc=%s: %s)" % (rc, first),
                 "run the preflight from the operator's own shell (a sandbox cannot read the crontab)")]
    text = text if rc == 0 else ""
    rows = []
    for w in REQUIRED_WATCHDOGS:
        installed = any(w in line and not line.lstrip().startswith("#") for line in text.splitlines())
        rows.append(("PASS", "cron %s installed" % w, None) if installed else
                    ("BLOCKED", "cron %s missing" % w, "operator: install the line from the script's header"))
    for w in OPTIONAL_CRON:
        if not any(w in line and not line.lstrip().startswith("#") for line in text.splitlines()):
            rows.append(("WARN", "cron %s not installed (optional)" % w, "install when that seat is wanted"))
    return rows


def check_workers(desk_dir, proc_root="/proc"):
    from sniper.routes.api_memecoin_desk import workers
    rows = []
    for w in workers(desk_dir, proc_root=proc_root):
        if w["worker"] in REQUIRED_WORKERS:
            rows.append(("PASS", "%s running" % w["worker"], None) if w["state"] == "running" else
                        ("BLOCKED", "%s %s" % (w["worker"], w["state"]), "its watchdog cron line starts it"))
    return rows


def _release_of(script, proc_root):
    for d in Path(proc_root).glob("[0-9]*"):
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if len(argv) > 1 and os.path.basename(argv[1].decode(errors="replace")) == script:
            return Path(argv[1].decode(errors="replace")).resolve().parents[1]
    return None


def _crawler_era(release):
    try:
        text = (release / "sniper" / "services" / "memecoin_desk_universe.py").read_text()
    except OSError:
        return None
    line = next((l for l in text.splitlines() if l.startswith("COMPLETENESS")), "")
    return '"absent"' in line


def check_release_order(proc_root="/proc"):
    """Gamma's deploy-order rule: U on crawler-era code ('absent' in COMPLETENESS) while a snapshot
    reader runs an older release makes the readers refuse U's snapshots."""
    u = _release_of("memecoin_desk_universe_worker.py", proc_root)
    if u is None:
        return "WARN", "U not running: release order not checkable", "start U through its watchdog"
    readers = {s: _release_of(s, proc_root) for s in SNAPSHOT_READERS}
    if _crawler_era(u):
        old = [s for s, r in readers.items() if r is not None and _crawler_era(r) is False]
        if old:
            return ("BLOCKED", "U is on crawler-era code but %s run an older release" % ", ".join(old),
                    "move every snapshot reader to U's release in the same release move")
    return "PASS", "U and the running snapshot readers agree on the snapshot contract", None


def check_gh(gh=lambda: _run(["gh", "auth", "status"])):
    """By what gh SAYS, never its exit code alone: this gh exits 0 while printing 'Failed to log in ... The
    token ... is invalid' (Alpha, 2026-10-21)."""
    rc, out = gh()
    out = str(out or "")
    if rc == 0 and "Logged in to" in out and "Failed to log in" not in out and "is invalid" not in out:
        return "PASS", "gh logged in", None
    reason = next((l.strip(" -X") for l in out.splitlines() if "invalid" in l or "Failed" in l), "rc=%s" % rc)
    # WARN, not BLOCKED (operator 2026-10-21, "No git needed"): deploys run locally from the hand-set
    # origin/main ref, so GitHub access does not gate activation
    return "WARN", "gh not logged in (%s)" % reason[:100], "only for PRs: gh auth login -h github.com"


def check_git_remote(ls_remote=lambda: _run(["git", "-C", str(ROOT), "ls-remote", "--exit-code", "origin", "HEAD"],
                                            timeout=15)):
    """The deploy fetch itself: can this host read origin."""
    rc, out = ls_remote()
    if rc == 0:
        return "PASS", "git can read origin", None
    first = (str(out or "").strip().splitlines() or ["rc=%s" % rc])[0][:100]
    return "WARN", "git cannot read origin (%s)" % first, "only for a remote fetch: gh auth login, then gh auth setup-git"


def run(*, state_root, auth=None, crontab=None, gh=None, ls_remote=None, proc_root="/proc"):
    desk_dir = Path(state_root) / "data" / "desk"
    knobs = env_knobs(Path(state_root) / ".env")
    rows = [("PASS", "state root %s is outside any release" % state_root, None),
            check_login(*(auth,) if auth else ()),
            check_knobs(knobs),
            check_prereg()]
    cohort = check_cohort(knobs, Path(state_root) / "data" / "memecoin_desk.db", Path(state_root) / "sniper_tracker.db")
    rows.append(cohort[:3])
    if len(cohort) > 3:
        rows += measurement_rows(cohort[3])
    rows += check_cron(*(crontab,) if crontab else ())
    rows += check_workers(desk_dir, proc_root=proc_root)
    rows.append(check_release_order(proc_root=proc_root))
    rows.append(check_gh(*(gh,) if gh else ()))
    rows.append(check_git_remote(*(ls_remote,) if ls_remote else ()))
    return rows


def main(argv=None):
    from sniper.util.state_paths import require_state_root_outside_release
    root = require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    rows = run(state_root=root)
    for status, what, fix in rows:
        print("%-7s %s%s" % (status, what, ("  -> " + fix) if fix and status != "PASS" else ""))
    blocked = sum(1 for r in rows if r[0] == "BLOCKED")
    print("%s: %d blocked, %d warn, %d pass" % ("BLOCKED" if blocked else "READY", blocked,  # INFO rows not counted
                                                 sum(1 for r in rows if r[0] == "WARN"),
                                                 sum(1 for r in rows if r[0] == "PASS")))
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
