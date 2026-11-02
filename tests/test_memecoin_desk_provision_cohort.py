"""Desk cohort U provisioning: a dry run prints the frozen policy and its id and writes nothing;
--confirm writes the same id to both stores (temporary DBs only)."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as ledger
from sniper.persistence import db_memecoin_desk as store

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("provision", ROOT / "scripts" / "memecoin_desk_provision_cohort.py")
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)
ARGS = ["--judge-p-admit", "0.28", "--round-trip-loss-pct-max", "8"]


@pytest.fixture(autouse=True)
def crawler(monkeypatch):
    """A stand-in CRAWLER module (Lambda's seat is not on this branch); one test removes it."""
    import sys
    import types
    mod = types.ModuleType("sniper.services.memecoin_desk_crawler")
    mod.CRAWLER_VERSION = "desk-crawler-test"
    monkeypatch.setitem(sys.modules, "sniper.services.memecoin_desk_crawler", mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_DESK_RAIL_BOUNDS", "tp_pct:15-150,sl_pct:10-40,trail_pct:8-30")
    funded = sqlite3.connect(tmp_path / "ledger.db")
    ledger.ensure_memecoin_tables(funded)
    funded.close()
    return tmp_path


def _out(capsys):
    text = capsys.readouterr().out
    body = json.loads(text[:text.index("\nMEMECOIN_DESK_U_COHORT_ID=")])
    cid = text.split("MEMECOIN_DESK_U_COHORT_ID=", 1)[1].split()[0]
    return body, cid, text


def test_a_dry_run_prints_the_frozen_policy_and_id_and_writes_nothing(env, capsys):
    assert P.main(ARGS + ["--desk-db", str(env / "desk.db"), "--ledger-db", str(env / "ledger.db")]) == 0
    body, cid, text = _out(capsys)
    pol = body["policy"]
    assert (body["producer"], body["prompt_version"]) == ("U", "desk-judge-v18")
    assert {k: pol[k] for k in ("clip_usd", "max_open", "daily_halt_usd", "cumulative_kill_usd", "enter_ttl_max_s",
                                "boundary_max_s", "wait_max_s", "judge_p_admit", "evidence_wait_s",
                                "model_billing_basis", "ranker_version", "select_version", "label_version",
                                "sr_post_p", "sr_all_p")} == {
        "clip_usd": 25, "max_open": 5, "daily_halt_usd": 200, "cumulative_kill_usd": -200, "enter_ttl_max_s": 30,
        "boundary_max_s": 120, "wait_max_s": 300, "judge_p_admit": 0.28, "evidence_wait_s": 60,
        "model_billing_basis": "subscription_flat", "ranker_version": "u-runners-v1",
        "select_version": "desk-judge-select-v1", "label_version": "u-label-v2", "sr_post_p": 0.25,
        "sr_all_p": 0.08}
    assert pol["crawler_version"] == "desk-crawler-test"
    assert pol["hard_filter"] == {"version": "hf2", "round_trip_loss_pct_max": 8.0, "transfer_hook_allowlist": []}
    assert pol["rail_bounds"]["sl_pct"] == [10.0, 40.0]
    assert "dry run: nothing written" in text and cid.startswith("desk-cohort:U:")
    assert not (env / "desk.db").exists()
    funded = sqlite3.connect(env / "ledger.db")
    assert funded.execute("SELECT COUNT(*) FROM memecoin_desk_cohorts").fetchone()[0] == 0
    funded.close()


def test_confirm_provisions_the_printed_id_in_both_stores_and_a_rerun_is_the_same_cohort(env, capsys):
    args = ARGS + ["--desk-db", str(env / "desk.db"), "--ledger-db", str(env / "ledger.db")]
    assert P.main(args) == 0
    _, dry_id, _ = _out(capsys)
    assert P.main(args + ["--confirm"]) == 0
    _, cid, text = _out(capsys)
    assert cid == dry_id and ("provisioned %s" % cid) in text
    desk = store.connect(env / "desk.db")
    assert store.get_cohort(desk, cid)["policy"]["select_version"] == "desk-judge-select-v1"
    desk.close()
    assert P.main(args + ["--confirm"]) == 0 and ("already provisioned %s" % cid) in capsys.readouterr().out


def test_unparseable_rail_bounds_refuse(env, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_RAIL_BOUNDS", "tp_pct:15-150")     # no sl_pct
    with pytest.raises(ValueError, match="rail_bounds"):
        P.main(ARGS)


def test_the_id_is_the_one_the_ledger_derives(tmp_path):
    from sniper.settings.memecoin import parse_desk_rail_bounds
    policy = P.frozen_policy(judge_p_admit=0.28, round_trip_loss_pct_max=8.0,
                             hook_allowlist=[], rail_bounds=parse_desk_rail_bounds("sl_pct:10-40"))
    conn = sqlite3.connect(tmp_path / "l.db")
    ledger.ensure_memecoin_tables(conn)
    got = ledger.register_desk_cohort(conn, producer="U", policy=policy, prompt_version=P.PROMPT_VERSION,
                                      created_at="2026-10-21T16:00:00+00:00")
    conn.close()
    assert got == P.cohort_id(policy)


def test_the_cluster_watchdog_starts_the_pinned_worker_and_never_signals_a_fake_pid(tmp_path):
    import os
    import shutil
    import subprocess
    import time
    rel = tmp_path / "releases" / "A" / "scripts"
    rel.mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "memecoin_desk_cluster_watchdog.sh", rel)
    shutil.copytree(ROOT / "scripts" / "lib", rel / "lib")
    (rel / "memecoin_desk_cluster_worker.py").write_text("")
    (tmp_path / "current").symlink_to(tmp_path / "releases" / "A")
    fake_py = tmp_path / "python3"
    fake_py.write_text('#!/bin/sh\necho "$1 $2" > "%s"\n' % (tmp_path / "started"))
    fake_py.chmod(0o755)
    proc = tmp_path / "proc"
    child = subprocess.Popen(["sleep", "30"])
    try:
        d = proc / str(child.pid)
        d.mkdir(parents=True)
        old = str(tmp_path / "releases" / "OLD" / "scripts" / "memecoin_desk_cluster_worker.py")
        (d / "cmdline").write_bytes(b"/usr/bin/python3\0" + old.encode() + b"\0")
        (d / "environ").write_bytes(b"SNIPER_STATE_ROOT=" + str(tmp_path / "state").encode() + b"\0")
        out = subprocess.run(["bash", str(tmp_path / "current" / "scripts" / "memecoin_desk_cluster_watchdog.sh")],
                             env=dict(os.environ, SNIPER_STATE_ROOT=str(tmp_path / "state"),
                                      SNIPER_PYTHON=str(fake_py), DESK_CLUSTER_WATCHDOG_PROC_ROOT=str(proc)),
                             capture_output=True, text=True, timeout=30)
        for _ in range(50):
            if (tmp_path / "started").exists():
                break
            time.sleep(0.05)
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        assert child.returncode is None, "the watchdog signalled a pid from a fake process table"
    finally:
        child.kill()
        child.wait()
    pinned = str((tmp_path / "releases" / "A" / "scripts").resolve() / "memecoin_desk_cluster_worker.py")
    assert "stopping superseded desk cluster worker pid=%d" % child.pid in out.stderr
    assert (tmp_path / "started").read_text().split() == [pinned, "--loop"]


def test_judge_p_admit_has_no_default(env, capsys):
    with pytest.raises(SystemExit):                                   # argparse: required
        P.main(ARGS[2:])
    with pytest.raises(ValueError, match="judge_p_admit"):
        P.main(["--judge-p-admit", "0"] + ARGS[2:])


def test_no_crawler_in_the_release_refuses_to_provision(env, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "sniper.services.memecoin_desk_crawler", None)   # import fails
    with pytest.raises(ValueError, match="crawler_version"):
        P.main(ARGS)


def test_the_trigger_flag_freezes_the_amendment_11_names_and_changes_the_cohort_id():
    from sniper.settings.memecoin import parse_desk_rail_bounds
    kw = dict(judge_p_admit=1.0, round_trip_loss_pct_max=6.0, hook_allowlist=[],
              rail_bounds=parse_desk_rail_bounds("sl_pct:10-40"))
    old, new = P.frozen_policy(**kw), P.frozen_policy(**kw, trigger=True)
    assert (old["ranker_version"], old["select_version"]) == ("u-runners-v1", "desk-judge-select-v1")
    assert (new["ranker_version"], new["select_version"]) == ("u-inline-v1", "desk-judge-select-v2")
    assert P.cohort_id(old) != P.cohort_id(new)                                       # a new regime is a new cohort, never an edit
    with pytest.raises(ValueError, match="Pump.fun curve only"):
        P.frozen_policy(**kw, trigger=True, venue="pumpswap")
    with pytest.raises(SystemExit):                                                   # the flag parses; a bad venue still refuses
        P.main(ARGS + ["--trigger", "--venue", "nonsense"])


def test_winner_reentry_and_tolerance_are_frozen_only_when_asked(monkeypatch):
    """2026-10-23: re-entry is off unless the cohort carries the exact rule the desk store reads."""
    import importlib
    from sniper.persistence.db_memecoin_desk import _reentry_policy
    import json as _json
    P = importlib.import_module("scripts.memecoin_desk_provision_cohort")
    bounds = {"sl_pct": (10.0, 40.0), "tp_pct": (15.0, 150.0), "trail_pct": (8.0, 30.0)}
    base = P.frozen_policy(judge_p_admit=1.0, round_trip_loss_pct_max=6.0, hook_allowlist=[], rail_bounds=bounds, trigger=True)
    assert "winner_reentry" not in base and "verdict_tolerance_pct" not in base
    rule = {"version": "winner-reentry-v1", "per_hour": 2, "window_s": 1800.0}
    pol = P.frozen_policy(judge_p_admit=1.0, round_trip_loss_pct_max=6.0, hook_allowlist=[], rail_bounds=bounds,
                          trigger=True, winner_reentry=rule, verdict_tolerance_pct=6.0)
    assert _reentry_policy(_json.dumps(pol)) == rule and pol["verdict_tolerance_pct"] == 6.0
    assert P.cohort_id(pol, prompt_version="desk-judge-v19") != P.cohort_id(base, prompt_version="desk-judge-v19")


def test_mcap_band_is_frozen_into_the_hard_filter_only_when_asked():
    """Operator 2026-10-23: 15k floor as a new cohort; absent flag keeps the 30k-100k policy byte-identical."""
    import importlib
    from sniper.services.memecoin_desk_contract import mcap_band
    P = importlib.import_module("scripts.memecoin_desk_provision_cohort")
    bounds = {"sl_pct": (10.0, 40.0), "tp_pct": (15.0, 150.0), "trail_pct": (8.0, 30.0)}
    base = P.frozen_policy(judge_p_admit=1.0, round_trip_loss_pct_max=6.0, hook_allowlist=[], rail_bounds=bounds, trigger=True)
    band = P.frozen_policy(judge_p_admit=1.0, round_trip_loss_pct_max=6.0, hook_allowlist=[], rail_bounds=bounds,
                           trigger=True, mcap_band=(15000.0, 100000.0))
    assert "mcap_band" not in base["hard_filter"] and mcap_band(base["hard_filter"]) == (30000.0, 100000.0)
    assert mcap_band(band["hard_filter"]) == (15000.0, 100000.0)


def test_v23_freezes_forecast_facts_and_reentry_v3(env, capsys):
    assert P.main(ARGS + ["--standing-orders", "--v23", "--winner-reentry-per-hour", "2",
                          "--desk-db", str(env / "desk.db"), "--ledger-db", str(env / "ledger.db")]) == 0
    pol = _out(capsys)[0]["policy"]
    assert pol["forecast"] == "forecast-v1" and pol["facts"] == "v23-facts-v1"
    assert pol["winner_reentry"] == {"version": "winner-reentry-v3", "per_hour": 2, "window_s": 1800.0, "loss_soft": True}
    from sniper.services import memecoin_desk as desk
    desk._v23_policy(pol)                                     # the frozen keys pass the provisioning check


def test_v23_needs_standing_orders():
    with pytest.raises(ValueError):
        P.frozen_policy(judge_p_admit=0.28, round_trip_loss_pct_max=8.0, hook_allowlist=[],
                        rail_bounds={"sl_pct": (10, 40)}, v23=True)


def test_freeze_answer_budget_from_the_env(env, capsys, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS", "700")
    assert P.main(ARGS + ["--standing-orders", "--v23", "--freeze-answer-budget",
                          "--desk-db", str(env / "desk.db"), "--ledger-db", str(env / "ledger.db")]) == 0
    assert _out(capsys)[0]["policy"]["judge_max_output_tokens"] == 700
    monkeypatch.delenv("MEMECOIN_DESK_JUDGE_MAX_OUTPUT_TOKENS")
    with pytest.raises(SystemExit):
        P.main(ARGS + ["--standing-orders", "--v23", "--freeze-answer-budget",
                       "--desk-db", str(env / "desk.db"), "--ledger-db", str(env / "ledger.db")])


def test_drift_requeue_flag_freezes_the_rule(env, capsys):
    assert P.main(ARGS + ["--standing-orders", "--v23", "--drift-requeue",
                          "--desk-db", str(env / "desk.db"), "--ledger-db", str(env / "ledger.db")]) == 0
    pol = _out(capsys)[0]["policy"]
    assert pol["drift_requeue"] == "drift-requeue-v1"
    from sniper.services import memecoin_desk as desk
    desk._v23_policy(pol)
    with pytest.raises(ValueError):
        desk._v23_policy({**pol, "drift_requeue": "v0"})


def test_exit_observer_flag_freezes_the_contract(env, capsys, tmp_path):
    assert P.main(ARGS + ["--standing-orders", "--judge-model", "claude-opus-5-5", "--exit-observer", "exit-observer-v1",
                          "--desk-db", str(env / "desk.db"), "--ledger-db", str(env / "ledger.db")]) == 0
    pol = _out(capsys)[0]["policy"]
    assert pol["exit_observer"] == {"version": "exit-observer-v1", "grid": [2, 3, 4, 6], "baseline_drop_pct": 10.0}
    assert pol["judge_model"] == "claude-opus-5-5"
    from sniper.persistence import db_memecoin as ledger
    from sniper.persistence import db_memecoin_desk as store
    from sniper.services import memecoin_desk as desk
    import sqlite3
    lc = sqlite3.connect(tmp_path / "l.db"); ledger.ensure_memecoin_tables(lc)
    dc = store.connect(tmp_path / "d.db")
    for bad in ({**pol["exit_observer"], "grid": [2, 3]}, {**pol["exit_observer"], "version": "x"},
                {**pol["exit_observer"], "population": "all_market"}, "on",
                {**pol["exit_observer"], "version": "exit-observer-v2"}, {**pol["exit_observer"], "baseline_drop_pct": 15.0},
                {**pol["exit_observer"], "grid": [2.0, 3.0, 4.0, 6.0]}):
        with pytest.raises(ValueError, match="exit_observer"):
            desk.provision_cohort(dc, lc, producer="U", policy={**pol, "exit_observer": bad},
                                  prompt_version="desk-judge-v22", created_at="2026-10-23T22:00:00+00:00")
    assert desk.provision_cohort(dc, lc, producer="U", policy=pol, prompt_version="desk-judge-v22",
                                 created_at="2026-10-23T22:00:00+00:00")


def test_entry_gate_wait_flag_freezes_v1(env, capsys):
    assert P.main(ARGS + ["--standing-orders", "--v23", "--entry-gate-wait",
                          "--desk-db", str(env / "desk.db"), "--ledger-db", str(env / "ledger.db")]) == 0
    pol = _out(capsys)[0]["policy"]
    assert pol["entry_gate"] == "entry-gate-v1"
    from sniper.services import memecoin_desk as desk
    desk._v23_policy(pol)


def test_px_hold_max_wait_flag_freezes_and_out_of_range_refuses(env, capsys):
    assert P.main(ARGS + ["--standing-orders", "--v23", "--px-hold-max-wait-s", "600",
                          "--desk-db", str(env / "desk.db"), "--ledger-db", str(env / "ledger.db")]) == 0
    assert _out(capsys)[0]["policy"]["standing_orders"]["px_hold_max_wait_s"] == 600
    with pytest.raises((ValueError, SystemExit)):
        P.main(ARGS + ["--standing-orders", "--px-hold-max-wait-s", "601",
                       "--desk-db", str(env / "desk.db"), "--ledger-db", str(env / "ledger.db")])


def test_cluster_flag_freezes_the_parser_contract_without_trigger(env, capsys):
    from sniper.services.memecoin_cluster_rpc import desk_definition
    base = ARGS + ["--desk-db", str(env / "desk.db"), "--ledger-db", str(env / "ledger.db")]
    assert P.main(base) == 0
    without = _out(capsys)[0]["policy"]
    assert "cluster" not in without
    assert P.main(base + ["--cluster"]) == 0
    withc = _out(capsys)[0]["policy"]
    assert withc["cluster"] == desk_definition()
    assert {k: v for k, v in withc.items() if k != "cluster"} == without          # nothing else changes


def test_watch_list_and_structure_sheet_flags_freeze_their_keys(env, capsys):
    base = ARGS + ["--desk-db", str(env / "desk.db"), "--ledger-db", str(env / "ledger.db")]
    assert P.main(base) == 0
    without = _out(capsys)[0]["policy"]
    assert "watch_list" not in without and "sheet" not in without["structure"]
    assert P.main(base + ["--watch-list", "--structure-sheet", "facts-v2"]) == 0
    pol = _out(capsys)[0]["policy"]
    assert pol["watch_list"] == {"version": "watch-list-v1", "max_looks": 6, "life_s": 900, "cadence_s": [60, 180, 300, 600],
                                 "relook_seats_per_h": 20}
    assert pol["structure"] == {**without["structure"], "sheet": "facts-v2"}


PREREG = "docs/audits/2026-10-27-feed-intake-base-rate-preregistration.md@61de43949"


def test_an_intake_cohort_confirms_only_with_a_verified_preregistration(env, capsys):
    """Zeta dec74ade2 P2 + Alpha: --confirm on PumpSwap is refused except an --intake cohort that binds a (mechanism test; the
    real cohort binds the JUDGE cohort's OWN prereg, Alpha 2026-10-27, not this collection read)
    pre-registration existing in git; the binding (path + full commit) is part of the frozen policy."""
    args = ["--judge-p-admit", "1", "--round-trip-loss-pct-max", "8", "--venue", "pumpswap",
            "--desk-db", str(env / "desk.db"), "--ledger-db", str(env / "ledger.db")]
    with pytest.raises(SystemExit):
        P.main(args + ["--confirm"])                                        # plain PumpSwap: still refused
    with pytest.raises(SystemExit):
        P.main(args + ["--intake", "--confirm"])                            # intake without a prereg
    with pytest.raises(SystemExit):
        P.main(args + ["--intake", "--intake-prereg", "docs/nope.md@61de43949", "--confirm"])   # not in git
    assert P.main(args + ["--intake", "--intake-prereg", PREREG, "--confirm"]) == 0
    body, cid, text = _out(capsys)
    pol = body["policy"]
    assert ("provisioned %s" % cid) in text
    assert pol["preregistration"]["path"] == PREREG.split("@")[0] and len(pol["preregistration"]["commit"]) == 40
    assert pol["hard_filter"]["intake"] == {"version": "intake-v1", "mcap_safety": [5000.0, 50000000.0], "liq_min_quote_sol": 40.0}
    assert pol["select_version"] == "intake-v1" and pol["venue"] == "pumpswap" and "mcap_band" not in pol["hard_filter"]
    assert pol["judge_calls_per_hour"] == 40 and pol["judge_model"] == "gpt-6-luna"
    assert pol["judge_provider"] == "codex_cli" and pol["judge_effort"] == "low"
    desk = store.connect(env / "desk.db")
    assert store.get_cohort(desk, cid)["policy"]["admission_status"] == "paper"
    desk.close()


def test_intake_refuses_a_band_or_the_curve(env):
    base = ["--judge-p-admit", "1", "--round-trip-loss-pct-max", "8", "--intake", "--intake-prereg", PREREG]
    with pytest.raises(ValueError, match="intake"):
        P.main(base)                                                        # venue defaults to pumpfun
    with pytest.raises(ValueError, match="intake"):
        P.main(base + ["--venue", "pumpswap", "--mcap-band-lo-usd", "5000"])
