"""Desk activation preflight: read-only, BLOCKED with the exact fix, READY only when every runbook check
passes; .env values never printed (fakes for the CLI, crontab, gh and /proc; temporary files only)."""
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("preflight", ROOT / "scripts" / "memecoin_desk_preflight.py")
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)
SECRET = "sk-secret-value-never-printed"


def _proc(root, scripts):
    for i, (script, release) in enumerate(scripts, start=100):
        d = root / str(i)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"/usr/bin/python3\0" + str(release / "scripts" / script).encode() + b"\0")


def _release(base, name, *, absent):
    rel = base / name
    (rel / "scripts").mkdir(parents=True)
    (rel / "sniper" / "services").mkdir(parents=True)
    tail = ', "absent")' if absent else ")"
    (rel / "sniper" / "services" / "memecoin_desk_universe.py").write_text(
        'COMPLETENESS = ("complete", "partial", "missing", "window_not_elapsed"%s\n' % tail)
    return rel


def _ready_state(tmp_path, policy):
    from sniper.persistence import db_memecoin as ledger
    from sniper.persistence import db_memecoin_desk as store
    from sniper.services import memecoin_desk as desk
    state = tmp_path / "state"
    (state / "data").mkdir(parents=True)
    desk_conn = store.connect(state / "data" / "memecoin_desk.db")
    funded = sqlite3.connect(state / "sniper_tracker.db")
    ledger.ensure_memecoin_tables(funded)
    cid = desk.provision_cohort(desk_conn, funded, producer="U", policy=policy, prompt_version="desk-judge-v12",
                                created_at="2026-10-21T18:00:00+00:00")
    desk_conn.close()
    funded.close()
    knobs = {k: "1" for k in P.ENABLED_KNOBS}
    knobs.update({k: "x" for k in P.REQUIRED_KNOBS if k not in knobs})
    knobs["MEMECOIN_DESK_U_COHORT_ID"] = cid
    knobs["MEMECOIN_RESEARCH_HELIUS_RPC_URLS"] = SECRET
    (state / ".env").write_text("".join("%s=%s\n" % kv for kv in knobs.items()))
    return state


def _policy():
    import sys
    import types
    sys.path.insert(0, str(ROOT / "scripts"))
    prov = importlib.util.spec_from_file_location("prov", ROOT / "scripts" / "memecoin_desk_provision_cohort.py")
    mod = importlib.util.module_from_spec(prov)
    prov.loader.exec_module(mod)
    from sniper.settings.memecoin import parse_desk_rail_bounds
    return mod.frozen_policy(judge_p_admit=0.3, round_trip_loss_pct_max=5.0,
                             hook_allowlist=[], rail_bounds=parse_desk_rail_bounds("sl_pct:10-40,tp_pct:15-150"))


def _ok(rc_out):
    return lambda: rc_out


def test_fomo_intake_cron_is_optional_and_detected_without_new_knobs():
    name = "memecoin_desk_fomo_intake_watchdog.sh"
    absent = P.check_cron(crontab=_ok((0, "")))
    assert ("WARN", "cron %s not installed (optional)" % name,
            "install when that seat is wanted") in absent
    present = P.check_cron(crontab=_ok((0, "* * * * * /release/scripts/%s\n" % name)))
    assert all(name not in row[1] for row in present)


def test_everything_in_place_reads_ready_and_writes_nothing(tmp_path, monkeypatch):
    doc = tmp_path / "prereg.md"
    doc.write_text("## Amendment 6 (2026-10-22): judge_p_admit\n")
    monkeypatch.setattr(P, "PREREG", doc)
    state = _ready_state(tmp_path, _policy())
    rel = _release(tmp_path, "rel-A", absent=True)
    scripts = ["memecoin_desk_universe_worker.py", "memecoin_desk_x_producer.py", "memecoin_desk_crawler.py",
               "memecoin_desk_cluster_worker.py", "memecoin_desk_ranker_worker.py", "memecoin_desk_assembler.py",
               "memecoin_desk_judge_worker.py"]
    _proc(tmp_path / "proc", [(s, rel) for s in scripts])
    cron = "".join("* * * * * /r/scripts/%s >> /l 2>&1\n" % w for w in P.REQUIRED_WATCHDOGS + P.OPTIONAL_CRON)
    files = [state / "data" / "memecoin_desk.db", state / "sniper_tracker.db", state / ".env"]
    before = [hashlib.sha256(f.read_bytes()).hexdigest() for f in files]
    rows = P.run(state_root=state, auth=_ok((0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))),
                 crontab=_ok((0, cron)), gh=_ok((0, "github.com\n  \u2713 Logged in to github.com account a")),
                 ls_remote=_ok((0, "abc\tHEAD")), proc_root=tmp_path / "proc")
    assert [r for r in rows if r[0] not in ("PASS", "INFO")] == []
    info = [w for s_, w, _ in rows if s_ == "INFO"]
    assert 'cohort judge_p_admit = 0.3' in info and any(w.startswith("cohort hard_filter = {") for w in info)
    assert [hashlib.sha256(f.read_bytes()).hexdigest() for f in files] == before
    assert SECRET not in json.dumps(rows)


def test_each_missing_piece_is_blocked_with_its_fix(tmp_path):
    state = tmp_path / "state"
    (state / "data").mkdir(parents=True)
    (state / ".env").write_text("MEMECOIN_DESK_ENABLED=1\nMEMECOIN_RESEARCH_HELIUS_RPC_URLS=%s\n" % SECRET)
    old = _release(tmp_path, "rel-old", absent=False)
    new = _release(tmp_path, "rel-new", absent=True)
    _proc(tmp_path / "proc", [("memecoin_desk_universe_worker.py", new), ("memecoin_desk_assembler.py", old)])
    rows = P.run(state_root=state, auth=_ok((0, json.dumps({"loggedIn": False, "authMethod": "none"}))),
                 crontab=_ok((1, "no crontab for bigpapi")), gh=_ok((1, "")),
                 ls_remote=_ok((128, "fatal: could not read Username")), proc_root=tmp_path / "proc")
    blocked = {what: fix for status, what, fix in rows if status == "BLOCKED"}
    assert "claude CLI not logged in" in blocked and "/login" in blocked["claude CLI not logged in"]
    assert any(w.startswith("missing: MEMECOIN_DESK_CLIP_USD") for w in blocked)
    assert "no MEMECOIN_DESK_U_COHORT_ID" in blocked
    assert all("cron %s missing" % w in blocked for w in P.REQUIRED_WATCHDOGS)
    assert "ranker not running" in blocked
    warned = {what for status, what, _ in rows if status == "WARN"}
    assert "gh not logged in (rc=1)" in warned and "git cannot read origin (fatal: could not read Username)" in warned
    assert not any(w.startswith(("gh ", "git ")) for w in blocked)        # operator: no git needed
    assert any("crawler-era code but memecoin_desk_assembler.py" in w for w in blocked)
    assert SECRET not in json.dumps(rows)


def test_a_cohort_frozen_under_other_versions_is_blocked(tmp_path):
    policy = dict(_policy(), crawler_version="desk-crawler-v0")
    state = _ready_state(tmp_path, policy)
    status, what, fix = P.check_cohort(P.env_knobs(state / ".env"), state / "data" / "memecoin_desk.db",
                                       state / "sniper_tracker.db")[:3]
    assert status == "BLOCKED" and "crawler_version" in what and "new cohort" in fix


def test_the_placeholder_p_admit_warns_and_a_missing_amendment_6_blocks(tmp_path):
    assert ("WARN", "judge_p_admit is exactly 1.0, the dry-run placeholder",
            "provision with amendment 6's value (a new cohort)") in P.measurement_rows({"judge_p_admit": 1.0})
    assert not [r for r in P.measurement_rows({"judge_p_admit": 0.28}) if r[0] == "WARN"]
    doc = tmp_path / "prereg.md"
    doc.write_text("## Amendment 5 (x)\n")
    assert P.check_prereg(doc)[0] == "BLOCKED"
    doc.write_text("## Amendment 5 (x)\n## Amendment 6 (2026-10-22): judge_p_admit\n")
    assert P.check_prereg(doc)[0] == "PASS"


def test_main_refuses_a_state_root_inside_a_release(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path / ".runtime" / "memecoin" / "releases" / "x"))
    with pytest.raises(SystemExit, match="inside a pinned release"):
        P.main([])


def test_gh_exiting_0_with_an_invalid_token_is_blocked():
    """This host's gh exits 0 while saying the token is invalid (Alpha, 2026-10-21)."""
    out = ("github.com\n  X Failed to log in to github.com account achomadafaka (/home/x/.config/gh/hosts.yml)\n"
           "  - Active account: true\n  - The token in /home/x/.config/gh/hosts.yml is invalid.\n")
    status, what, fix = P.check_gh(lambda: (0, out))
    assert status == "WARN" and "Failed to log in" in what and "gh auth login" in fix


def test_a_trigger_cohort_matches_this_release_and_a_mixed_pair_is_blocked(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    prov = importlib.util.spec_from_file_location("prov2", ROOT / "scripts" / "memecoin_desk_provision_cohort.py")
    mod = importlib.util.module_from_spec(prov)
    prov.loader.exec_module(mod)
    from sniper.settings.memecoin import parse_desk_rail_bounds
    kw = dict(judge_p_admit=0.3, round_trip_loss_pct_max=5.0, hook_allowlist=[],
              rail_bounds=parse_desk_rail_bounds("sl_pct:10-40,tp_pct:15-150"))
    state = _ready_state(tmp_path / "ok", mod.frozen_policy(**kw, trigger=True))
    got = P.check_cohort(P.env_knobs(state / ".env"), state / "data" / "memecoin_desk.db", state / "sniper_tracker.db")
    assert got[0] == "PASS"
    mixed = dict(mod.frozen_policy(**kw, trigger=True), ranker_version="u-runners-v1")       # the trigger rule with the old ranker name
    state = _ready_state(tmp_path / "bad", mixed)
    got = P.check_cohort(P.env_knobs(state / ".env"), state / "data" / "memecoin_desk.db", state / "sniper_tracker.db")
    assert got[0] == "BLOCKED" and "ranker_version" in got[1]


def test_a_trigger_cohort_whose_cluster_block_differs_from_the_workers_constants_is_blocked(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    prov = importlib.util.spec_from_file_location("prov4", ROOT / "scripts" / "memecoin_desk_provision_cohort.py")
    mod = importlib.util.module_from_spec(prov)
    prov.loader.exec_module(mod)
    from sniper.settings.memecoin import parse_desk_rail_bounds
    kw = dict(judge_p_admit=0.3, round_trip_loss_pct_max=5.0, hook_allowlist=[],
              rail_bounds=parse_desk_rail_bounds("sl_pct:10-40,tp_pct:15-150"))
    good = mod.frozen_policy(**kw, trigger=True)
    state = _ready_state(tmp_path / "ok", good)
    assert P.check_cohort(P.env_knobs(state / ".env"), state / "data" / "memecoin_desk.db", state / "sniper_tracker.db")[0] == "PASS"
    for bad in ({"parser_version": 1}, {"max_pages": 20}, {"wrap_close_rule": "other"}):
        policy = dict(good, cluster={**good["cluster"], **bad})
        state = _ready_state(tmp_path / ("bad%s" % list(bad)[0]), policy)
        got = P.check_cohort(P.env_knobs(state / ".env"), state / "data" / "memecoin_desk.db", state / "sniper_tracker.db")
        assert got[0] == "BLOCKED" and "cluster" in got[1], bad
    policy = {k: v for k, v in good.items() if k != "cluster"}                          # a trigger cohort with no cluster block at all
    state = _ready_state(tmp_path / "none", policy)
    assert P.check_cohort(P.env_knobs(state / ".env"), state / "data" / "memecoin_desk.db", state / "sniper_tracker.db")[0] == "BLOCKED"
