"""W4 failure taxonomy tests for wallet protocol batch runs."""

from __future__ import annotations

import sqlite3

import requests

from db import init_db

WALLET = "0x" + "b" * 40


def _connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_failure_classifier_allowed_values():
    from sniper.analysis.wallet_protocol.batch_failures import classify

    assert classify(TimeoutError("slow")) == "timeout"
    assert classify(RuntimeError("activity cap hit")) == "data_cap_hit"
    assert classify(requests.HTTPError("500")) == "api_error"
    assert classify(ValueError("bad protocol")) == "protocol_bug"
    assert classify(RuntimeError("gate_skip: sample")) == "gate_skip"


def test_from_queue_failure_sets_failure_category(tmp_path, monkeypatch):
    import scripts.run_wallet_protocol_batch as mod

    db_path = tmp_path / "batch.db"
    conn = _connect(db_path)
    try:
        init_db(conn)
        mod.enqueue_intake_wallet(conn, wallet=WALLET, source="manual")
    finally:
        conn.close()

    def _fail(wallet, *, db_path, single_wallet_args):
        raise TimeoutError("wallet analysis timed out")

    monkeypatch.setattr(mod, "_run_single_wallet_protocol", _fail)

    rc = mod.main([
        "--db", str(db_path),
        "--from-queue",
        "--limit", "1",
        "--batch-id", "fail-taxonomy",
        "--heartbeat-seconds", "60",
    ])

    assert rc == 1
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM wallet_analyzer_intake_queue").fetchone()
        assert row["status"] == "failed"
        assert row["failure_category"] == "timeout"
    finally:
        conn.close()


# REMOVED: test_brain_health_failure_category_breakdown
#
# It exercised `brain_health.check_wallet_analyzer_failure_categories()`, which was DELETED in
# dc240a80 ("re-scope brain_health.py to the live stack") along with the other legacy check_*
# functions — the wallet analyzer is frozen/legacy per CLAUDE.md, and brain_health now reports on
# what actually runs (the memecoin runner + web). The test outlived the code it tested and failed
# with `module 'scripts.brain_health' has no attribute 'check_wallet_analyzer_failure_categories'`.
#
# The live behaviour it half-covered — the failure classifier and `failure_category` persistence —
# is still asserted by test_failure_classifier_allowed_values and
# test_from_queue_failure_sets_failure_category above.
