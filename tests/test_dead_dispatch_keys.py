"""The dead-key auditor: a key is checked through the producer's own transform, not the raw column.

The audit exists because a string-keyed dispatch table goes dead SILENTLY when the producer's
vocabulary moves, and its unit test keeps passing because the test builds the input by hand.

These tests pin the part that makes the audit valid rather than merely plausible. A first version
read `terminal_status`/`terminal_reason` directly and reported 7 of 10 keys dead on 2026-09-08 —
entirely an artefact of the persist path mapping `unsafe`->`safety_failed`, refusals->`refused`, and
`str(None)`->the status string. Through the mapping the answer was 1 dead key, which is the true one.
"""
import importlib.util
import pathlib
import sqlite3

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "research" / "dead_dispatch_keys.py"
_spec = importlib.util.spec_from_file_location("dead_dispatch_keys", _SRC)
D = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(D)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_entry_attempts(attempt_id TEXT, terminal_status TEXT, "
              "terminal_reason TEXT, decision_started_at TEXT)")
    return c


def _row(c, status, reason, when="now"):
    c.execute("INSERT INTO memecoin_entry_attempts VALUES(?,?,?,datetime('now',?))",
              (f"a{status}{reason}{when}", status, reason,
               "-1 days" if when == "now" else "-90 days"))


# --- the classification --------------------------------------------------------------------

def test_a_key_with_recent_rows_is_live():
    assert D.classify({"ever": 5, "recent": 2}, 14) == "live"


def test_a_key_that_fired_before_and_is_silent_now_is_DEAD():
    """The signature of a moved vocabulary: it worked, then stopped."""
    assert D.classify({"ever": 19, "recent": 0}, 14) == "DEAD"


def test_a_key_no_row_ever_carried_is_NEVER_not_DEAD():
    """Distinct verdicts on purpose. NEVER may mean the condition is simply rare."""
    assert D.classify({"ever": 0, "recent": 0}, 14) == "NEVER"


# --- the mapping, which is what makes the audit correct -------------------------------------

def test_the_audit_finds_an_unsafe_key_recorded_as_safety_failed(conn):
    """`unsafe` is never persisted; `_trace_terminal` maps it. A raw-column audit would miss this."""
    _row(conn, "safety_failed", "safety_deadline_exceeded")
    conn.commit()
    rows = {r["key"]: r for r in D.audit_retry_transient(conn, 14)}
    hit = rows[("unsafe", "safety_deadline_exceeded")]
    assert hit["persisted"] == ("safety_failed", "safety_deadline_exceeded")
    assert hit["ever"] == 1 and hit["recent"] == 1
    assert D.classify(hit, 14) == "live", "the raw column holds no 'unsafe' row at all"


def test_a_none_reason_is_persisted_as_the_status_string_not_as_NULL(conn):
    """`_trace_terminal` does `str(reason)`, so ("failed", None) lands as ("failed", "failed").

    Querying `terminal_reason IS NULL` for it returns zero and reads as NEVER, which is the exact
    mis-instrumentation this test exists to prevent.
    """
    _row(conn, "failed", "failed", when="old")
    conn.commit()
    rows = {r["key"]: r for r in D.audit_retry_transient(conn, 14)}
    hit = rows[("failed", None)]
    assert hit["persisted"] == ("failed", "failed")
    assert hit["ever"] == 1 and hit["recent"] == 0
    assert D.classify(hit, 14) == "DEAD"


def test_an_empty_ledger_reports_every_key_as_NEVER_not_as_dead(conn):
    rows = D.audit_retry_transient(conn, 14)
    assert rows, "the table must not be empty or this audit checks nothing"
    assert all(D.classify(r, 14) == "NEVER" for r in rows)


def test_every_key_in_the_live_table_is_covered_by_the_audit(conn):
    from sniper.services.memecoin_real_runner import _RETRY_TRANSIENT
    covered = {r["key"] for r in D.audit_retry_transient(conn, 14)}
    assert covered == set(_RETRY_TRANSIENT), "a key added to the table must be audited too"
