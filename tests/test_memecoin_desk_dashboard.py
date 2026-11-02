"""Desk tab API: read-only, answers with everything switched off, per-cohort caps, today's funnel,
workers by /proc, reports (temporary DBs only)."""
import hashlib
import sqlite3
from pathlib import Path

from flask import Flask

from sniper.persistence import db_memecoin as ledger
from sniper.persistence import db_memecoin_desk as store
from sniper.routes import api_memecoin_desk as D


def _client(tmp_path):
    app = Flask(__name__)
    app.register_blueprint(D.create_api_memecoin_desk_blueprint({
        "desk_dir": str(tmp_path / "desk"), "desk_db": str(tmp_path / "memecoin_desk.db"),
        "ledger_db": str(tmp_path / "ledger.db")}))
    return app.test_client()


def test_everything_switched_off_still_answers_not_running(tmp_path):
    body = _client(tmp_path).get("/api/memecoin/desk/status").get_json()
    assert body["display_only"] is True and "never pooled" in body["no_tuning"]
    assert body["cohorts"] == {"available": False, "reason": "ledger_missing"}
    assert body["tickets"] == {"available": False, "reason": "desk_db_missing"}
    assert body["ranker"] is None and body["reports"] == {"latest_postmortem": None, "checker": None}
    assert {w["worker"] for w in body["workers"]} >= {"U producer", "X producer", "crawler", "cluster", "ranker",
                                                      "assembler", "JUDGE", "RISK shadow"}


def test_caps_funnel_and_reports_are_read_without_a_write(tmp_path):
    desk = tmp_path / "desk"
    (desk / "postmortem").mkdir(parents=True)
    (desk / "postmortem" / "2026-10-20.md").write_text("# report\n")
    funded = sqlite3.connect(tmp_path / "ledger.db")
    ledger.ensure_memecoin_tables(funded)
    cid = ledger.register_desk_cohort(funded, producer="U", policy={"clip_usd": 25}, prompt_version="desk-judge-v12",
                                      created_at="2026-10-21T00:00:00+00:00")
    funded.commit()
    funded.close()
    store.connect(tmp_path / "memecoin_desk.db").close()
    dbs = [tmp_path / "ledger.db", tmp_path / "memecoin_desk.db"]
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in dbs]
    body = _client(tmp_path).get("/api/memecoin/desk/status").get_json()
    (cohort,) = body["cohorts"]["cohorts"]
    assert cohort["cohort_id"] == cid and cohort["kill_limit_usd"] == -200.0 and cohort["killed"] is False
    assert set(body["cohorts"]["desk"]) == {"open", "max_open", "daily_realized_usd", "daily_halt_usd", "daily_latched"}
    assert body["tickets"]["available"] is True and body["tickets"]["emitted"] == 0
    assert body["reports"]["latest_postmortem"] == "2026-10-20"
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in dbs] == before    # read-only
    report = _client(tmp_path).get("/api/memecoin/desk/postmortem/2026-10-20").get_json()
    assert report["markdown"] == "# report\n"
    assert _client(tmp_path).get("/api/memecoin/desk/postmortem/..%2Fsecret").status_code in (400, 404)


def test_a_worker_is_running_only_with_a_python_process_on_its_script(tmp_path):
    proc = tmp_path / "proc"
    for pid, argv in ((10, [b"/usr/bin/python3", b"/r/scripts/memecoin_desk_ranker_worker.py", b"--loop"]),
                      (11, [b"vim", b"memecoin_desk_crawler.py"])):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
    (tmp_path / "ranker").mkdir()
    (tmp_path / "ranker" / "status.json").write_text("{}")
    states = {w["worker"]: w["state"] for w in D.workers(tmp_path, proc_root=proc)}
    assert states["ranker"] == "running" and states["crawler"] == "not running"
    import os
    os.utime(tmp_path / "ranker" / "status.json", (1, 1))
    assert {w["worker"]: w["state"] for w in D.workers(tmp_path, proc_root=proc)}["ranker"] == "stale"


def test_a_desk_db_without_tables_reads_not_provisioned(tmp_path):
    sqlite3.connect(tmp_path / "memecoin_desk.db").close()        # the file exists, nothing provisioned
    assert _client(tmp_path).get("/api/memecoin/desk/status").get_json()["tickets"] == {
        "available": False, "reason": "desk_not_provisioned"}


def test_copy_lane_counts_real_closes_in_window_and_fills_without_fee(tmp_path):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    recent, old = (now - timedelta(days=1)).isoformat(), (now - timedelta(days=8)).isoformat()
    funded = sqlite3.connect(tmp_path / "ledger.db")
    ledger.ensure_memecoin_tables(funded)
    for pid, mode, closed, pnl, reason, net, src in (
            (1, "real", recent, 10.0, "tp", "solana", "slot_lane"),
            (2, "real", recent, -20.0, "b_stop", "solana", "slot_lane"),
            (3, "real", recent, None, "b_stop", "56", "bsc_fast_lane"),    # unpriced, never a 0
            (4, "paper", recent, 99.0, "tp", "solana", "slot_lane"),       # paper: excluded
            (5, "real", old, -50.0, "b_stop", "solana", "slot_lane")):     # outside 7 days: excluded
        funded.execute("INSERT INTO memecoin_positions(id, mint, execution_mode, is_open, closed_at, realized_pnl, "
                       "exit_reason, network_id, signal_source) VALUES(?,?,?,0,?,?,?,?,?)",
                       (pid, "m%d" % pid, mode, closed, pnl, reason, net, src))
    funded.execute("INSERT INTO memecoin_fills(position_id, kind, network_fee_lamports) VALUES(1,'entry',5000)")
    funded.execute("INSERT INTO memecoin_fills(position_id, kind) VALUES(1,'bracket_exit')")
    funded.execute("INSERT INTO memecoin_fills(position_id, kind) VALUES(4,'entry')")
    funded.commit()
    funded.close()
    before = hashlib.sha256((tmp_path / "ledger.db").read_bytes()).hexdigest()
    body = _client(tmp_path).get("/api/memecoin/desk/copy-lane").get_json()
    assert hashlib.sha256((tmp_path / "ledger.db").read_bytes()).hexdigest() == before    # read-only
    reasons = {r["key"]: r for r in body["by_exit_reason"]}
    assert reasons["b_stop"] == {"key": "b_stop", "closes": 2, "wins": 0, "unpriced": 1, "realized_usd": -20.0}
    assert reasons["tp"]["closes"] == 1 and reasons["tp"]["realized_usd"] == 10.0
    assert {r["key"]: r["closes"] for r in body["by_chain"]} == {"solana": 2, "56": 1}
    assert {(f["chain"], f["kind"]): (f["fills"], f["no_fee"]) for f in body["fills_fee"]} == {
        ("solana", "entry"): (1, 0), ("solana", "exit"): (1, 1)}


def test_copy_lane_without_a_ledger_says_so(tmp_path):
    assert _client(tmp_path).get("/api/memecoin/desk/copy-lane").get_json() == {
        "display_only": True, "available": False, "reason": "ledger_missing"}


def test_copy_lane_no_fee_follows_reconciled_fees_and_survives_a_ledger_without_the_tables(tmp_path):
    # Alpha/Beta 2026-10-21: the panel and fills_without_fee share one predicate; a fill whose fee
    # the reconciler recorded stops counting; a ledger without the per-tx tables still answers
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    funded = sqlite3.connect(tmp_path / "ledger.db")
    ledger.ensure_memecoin_tables(funded)
    funded.execute("DROP TABLE memecoin_sol_tx_fees")                     # the live ledger before the DDL
    funded.execute("INSERT INTO memecoin_positions(id, mint, execution_mode, is_open, closed_at, realized_pnl, "
                   "exit_reason, network_id, signal_source) VALUES(1,'m','real',0,?,1.0,'tp','solana','slot_lane')",
                   (recent,))
    funded.execute("INSERT INTO memecoin_fills(position_id, kind, tx_sig) VALUES(1,'entry','SIG1')")
    funded.execute("INSERT INTO memecoin_fills(position_id, kind, tx_sig) VALUES(1,'bracket_exit','SIG2')")
    funded.commit()
    no_fee = lambda: {f["kind"]: f["no_fee"] for f in
                      _client(tmp_path).get("/api/memecoin/desk/copy-lane").get_json()["fills_fee"]}
    assert no_fee() == {"entry": 1, "exit": 1}
    ledger.ensure_memecoin_tables(funded)                                 # the runner's ensure path creates it
    ledger.record_sol_tx_fee(funded, tx_sig="SIG1", kind="entry",
                             fee={"fee_lamports": 5000, "tip_lamports": 0, "status": "complete"})
    ledger.record_sol_tx_fee(funded, tx_sig="SIG2", kind="bracket_exit",
                             fee={"fee_lamports": 5000, "tip_lamports": None, "status": "unknown:tip"})
    funded.close()
    assert no_fee() == {"entry": 0, "exit": 1}
    funded = sqlite3.connect(tmp_path / "ledger.db")
    assert ledger.fills_without_fee(funded) == {"solana": {"bracket_exit": 1}}
