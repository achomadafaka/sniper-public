from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin
from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.services.memecoin_entry_trace import EntryAttemptTrace


NOW = "2026-07-15T12:00:00+00:00"


@dataclass
class FakeClock:
    elapsed: float = 100.0

    def perf_counter(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds


class CountingConnection(sqlite3.Connection):
    commit_count: int

    def commit(self) -> None:
        self.commit_count += 1
        super().commit()


class FailingCommitConnection(CountingConnection):
    fail_next_commit: bool = False

    def commit(self) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise sqlite3.OperationalError("injected commit failure")
        super().commit()


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:", factory=CountingConnection)
    connection.commit_count = 0
    connection.row_factory = sqlite3.Row
    ensure_memecoin_tables(connection)
    connection.commit_count = 0
    return connection


@pytest.fixture
def event():
    return SimpleNamespace(
        fomo_event_id="event-1",
        source_user_id="wallet-1",
        source_handle="alice",
        network_id="4663",
        mint="0xabc",
        ticker="ABC",
        side="buy",
        their_price=0.25,
        their_market_cap=1_000_000.0,
        created_at="2026-07-15T11:59:59+00:00",
        received_at="2026-07-15T11:59:59.900000+00:00",
        clock_domain="observed_real_utc",
        signal_source="global_feed",
    )


def test_attempt_table_has_identity_stage_attribution_and_terminal_columns(conn):
    expected = {
        "attempt_id",
        "origin_event_id",
        "signal_source",
        "network_id",
        "mint",
        "source_user_id",
        "source_created_at",
        "source_timestamp_trust",
        "source_price",
        "source_market_cap",
        "observed_at",
        "clock_domain",
        "decision_started_at",
        "chain_resolution_completed_at",
        "chain_resolution_ms",
        "market_data_completed_at",
        "market_data_ms",
        "reservation_completed_at",
        "reservation_ms",
        "safety_checks_completed_at",
        "safety_checks_ms",
        "quote_completed_at",
        "quote_ms",
        "signing_completed_at",
        "signing_ms",
        "transaction_submitted_at",
        "transaction_submitted_ms",
        "chain_confirmation_completed_at",
        "chain_confirmation_ms",
        "balance_delta_completed_at",
        "balance_delta_ms",
        "persistence_completed_at",
        "persistence_ms",
        "quote_price",
        "quote_observed_at",
        "quote_age_ms",
        "quote_id",
        "fill_price",
        "source_drift_pct",
        "execution_slippage_pct",
        "total_copy_drift_pct",
        "pending_id",
        "position_id",
        "fill_id",
        "tx_sig",
        "total_ms",
        "terminal_status",
        "terminal_reason",
        "completed_at",
        "trace_json",
    }
    got = {row[1] for row in conn.execute("PRAGMA table_info(memecoin_entry_attempts)")}
    assert expected <= got


def test_attempt_trace_keeps_claimed_source_separate_and_derives_monotonic_stages(
    conn, event
):
    clock = FakeClock()
    event.delivery_lane = "websocket"

    trace = EntryAttemptTrace.start(conn, event, NOW, clock)
    clock.advance(0.025)
    assert trace.mark("chain_resolution") is True
    clock.advance(0.010)
    assert trace.finish("refused", "not approved") is True

    row = dict(conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone())
    assert row["source_created_at"] == event.created_at
    assert row["observed_at"] == event.received_at
    assert row["source_timestamp_trust"] == "claimed_untrusted"
    assert row["clock_domain"] == "observed_real_utc"
    assert row["delivery_lane"] == "websocket"
    assert row["network_id"] == "4663"
    assert row["chain_resolution_completed_at"] >= row["decision_started_at"]
    assert row["chain_resolution_ms"] == pytest.approx(25.0)
    assert row["reservation_completed_at"] is None
    assert row["terminal_reason"] == "not approved"


def test_stage_completed_at_exposes_only_the_trace_clock_stamp(conn, event):
    clock = FakeClock()
    trace = EntryAttemptTrace.start(conn, event, NOW, clock)
    assert trace.stage_completed_at("quote") is None
    clock.advance(0.125)
    trace.mark("quote")
    assert trace.stage_completed_at("quote") == "2026-07-15T12:00:00.125000+00:00"


def test_retried_submission_stages_keep_history_and_project_latest_attempt(conn, event):
    clock = FakeClock()
    trace = EntryAttemptTrace.start(conn, event, NOW, clock)
    clock.advance(0.010)
    trace.mark_submission("quote", 1, outcome="available")
    clock.advance(0.020)
    trace.mark_submission("signing", 1, outcome="signed")
    clock.advance(0.030)
    trace.mark_submission("transaction_submitted", 1, outcome="accepted")
    clock.advance(0.040)
    trace.mark_submission("chain_confirmation", 1, outcome="failed")
    first_confirmation = trace.stage_completed_at("chain_confirmation")
    clock.advance(0.050)
    trace.mark_submission("quote", 2, outcome="available")
    second_quote = trace.stage_completed_at("quote")
    clock.advance(0.060)
    trace.mark_submission("signing", 2, outcome="signed")
    clock.advance(0.070)
    trace.mark_submission("transaction_submitted", 2, outcome="accepted")
    clock.advance(0.080)
    trace.mark_submission("chain_confirmation", 2, outcome="confirmed")
    second_confirmation = trace.stage_completed_at("chain_confirmation")
    trace.finish("filled", "confirmed")

    row = dict(conn.execute(
        "SELECT quote_completed_at, chain_confirmation_completed_at, trace_json "
        "FROM memecoin_entry_attempts WHERE attempt_id=?", (trace.attempt_id,),
    ).fetchone())
    payload = __import__("json").loads(row["trace_json"])
    assert row["quote_completed_at"] == second_quote
    assert row["chain_confirmation_completed_at"] == second_confirmation
    assert second_quote > first_confirmation
    assert [event["attempt_no"] for event in payload["submission_events"]] == [1, 1, 1, 1, 2, 2, 2, 2]
    assert payload["submission_events"][3]["outcome"] == "failed"
    assert payload["submission_events"][-1]["outcome"] == "confirmed"


def test_substage_events_are_monotonic_without_fragmenting_top_level_stages(conn, event):
    clock = FakeClock()
    trace = EntryAttemptTrace.start(conn, event, NOW, clock)
    clock.advance(0.010)
    trace.mark("chain_resolution")
    clock.advance(0.005)
    assert trace.mark_substage("evm.quote_initial", outcome="available") is True
    clock.advance(0.005)
    trace.mark("reservation")
    clock.advance(0.010)
    trace.mark_substage("evm.approval", outcome="confirmed")
    trace.finish("filled", "confirmed")

    row = conn.execute(
        "SELECT chain_resolution_ms,reservation_ms,trace_json FROM memecoin_entry_attempts "
        "WHERE attempt_id=?", (trace.attempt_id,),
    ).fetchone()
    payload = __import__("json").loads(row["trace_json"])
    assert row["chain_resolution_ms"] == pytest.approx(10.0)
    assert row["reservation_ms"] == pytest.approx(10.0)  # substage at 15ms did not split it
    assert [item["name"] for item in payload["substage_events"]] == [
        "evm.quote_initial", "evm.approval",
    ]
    assert [item["elapsed_ms"] for item in payload["substage_events"]] == pytest.approx([15, 30])
    assert [item["duration_ms"] for item in payload["substage_events"]] == pytest.approx([15, 15])


def test_explicit_stage_durations_exclude_unrelated_work(conn, event):
    """A caller-owned timer must override the trace's prior-marker interval."""
    clock = FakeClock()
    trace = EntryAttemptTrace.start(conn, event, NOW, clock)
    clock.advance(6.0)
    trace.mark("reservation", duration_ms=120.0)
    clock.advance(4.0)
    trace.mark_substage("evm.balance_usd_snapshot", duration_ms=250.0, outcome="ok")
    trace.finish("refused", "test")

    row = conn.execute(
        "SELECT reservation_ms,trace_json FROM memecoin_entry_attempts WHERE attempt_id=?",
        (trace.attempt_id,),
    ).fetchone()
    payload = __import__("json").loads(row["trace_json"])
    assert row["reservation_ms"] == pytest.approx(120.0)
    assert payload["substage_events"][0]["duration_ms"] == pytest.approx(250.0)


def test_attempt_trace_does_not_trust_legacy_unknown_receipt(conn, event):
    event.clock_domain = "legacy_unknown"
    clock = FakeClock()

    EntryAttemptTrace.start(conn, event, NOW, clock)

    row = conn.execute(
        "SELECT observed_at, clock_domain FROM memecoin_entry_attempts"
    ).fetchone()
    assert tuple(row) == (None, "legacy_unknown")


def test_attempt_trace_trusts_normalized_receipt_even_when_legacy_clock_is_unknown(
    conn, event
):
    event.clock_domain = "legacy_unknown"
    event.received_at_normalized = "2026-07-15T11:59:59.950000+00:00"
    clock = FakeClock()

    EntryAttemptTrace.start(conn, event, NOW, clock)

    row = conn.execute(
        "SELECT observed_at, clock_domain FROM memecoin_entry_attempts"
    ).fetchone()
    assert tuple(row) == (event.received_at_normalized, "observed_real_utc")


def test_stage_marks_stay_in_memory_until_one_terminal_commit(conn, event):
    clock = FakeClock()
    trace = EntryAttemptTrace.start(conn, event, NOW, clock)
    assert conn.commit_count == 1

    clock.advance(0.025)
    assert trace.mark("chain_resolution") is True
    clock.advance(0.010)
    assert trace.mark("market_data") is True
    assert conn.commit_count == 1
    shell = conn.execute(
        "SELECT chain_resolution_completed_at, market_data_completed_at "
        "FROM memecoin_entry_attempts WHERE attempt_id=?",
        (trace.attempt_id,),
    ).fetchone()
    assert tuple(shell) == (None, None)

    clock.advance(0.005)
    assert trace.finish("refused", "not approved") is True
    assert conn.commit_count == 2
    row = conn.execute(
        "SELECT chain_resolution_completed_at, chain_resolution_ms, "
        "market_data_completed_at, market_data_ms, total_ms "
        "FROM memecoin_entry_attempts WHERE attempt_id=?",
        (trace.attempt_id,),
    ).fetchone()
    assert row["chain_resolution_completed_at"] is not None
    assert row["chain_resolution_ms"] == pytest.approx(25.0)
    assert row["market_data_completed_at"] is not None
    assert row["market_data_ms"] == pytest.approx(10.0)
    assert row["total_ms"] == pytest.approx(40.0)


def test_trace_finish_refuses_to_commit_caller_owned_transaction(conn, event):
    trace = EntryAttemptTrace.start(conn, event, NOW, FakeClock())
    conn.execute("CREATE TABLE caller_work(value TEXT)")
    conn.commit()
    conn.execute("INSERT INTO caller_work(value) VALUES ('uncommitted')")

    with pytest.raises(RuntimeError, match="caller-owned transaction"):
        trace.finish("refused", "not approved")

    assert conn.execute("SELECT value FROM caller_work").fetchone()[0] == "uncommitted"
    assert conn.execute(
        "SELECT terminal_status FROM memecoin_entry_attempts WHERE attempt_id=?",
        (trace.attempt_id,),
    ).fetchone()[0] is None
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0] == 0


def test_failed_trace_finish_commit_rolls_back_its_transaction_and_leaves_connection_usable(event):
    conn = sqlite3.connect(":memory:", factory=FailingCommitConnection)
    conn.commit_count = 0
    conn.row_factory = sqlite3.Row
    ensure_memecoin_tables(conn)
    trace = EntryAttemptTrace.start(conn, event, NOW, FakeClock())
    conn.fail_next_commit = True

    with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
        trace.finish("refused", "not approved")

    assert conn.in_transaction is False
    row = conn.execute(
        "SELECT terminal_status, terminal_reason FROM memecoin_entry_attempts "
        "WHERE attempt_id=?",
        (trace.attempt_id,),
    ).fetchone()
    assert tuple(row) == (None, None)
    conn.execute("CREATE TABLE after_failure(value TEXT)")
    conn.execute("INSERT INTO after_failure(value) VALUES ('usable')")
    conn.commit()
    assert conn.execute("SELECT value FROM after_failure").fetchone()[0] == "usable"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (None, "reason"),
        ("", "reason"),
        ("   ", "reason"),
        ("refused", None),
        ("refused", ""),
        ("refused", "   "),
    ],
)
def test_invalid_trace_finish_leaves_attempt_retryable(conn, event, status, reason):
    clock = FakeClock()
    trace = EntryAttemptTrace.start(conn, event, NOW, clock)

    with pytest.raises(ValueError, match="terminal_(status|reason)"):
        trace.finish(status, reason)

    row = conn.execute(
        "SELECT terminal_status, terminal_reason, completed_at "
        "FROM memecoin_entry_attempts WHERE attempt_id=?",
        (trace.attempt_id,),
    ).fetchone()
    assert tuple(row) == (None, None, None)
    assert trace.finish("refused", "not approved") is True


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (None, "reason"),
        ("", "reason"),
        ("   ", "reason"),
        ("refused", None),
        ("refused", ""),
        ("refused", "   "),
    ],
)
def test_persistence_rejects_null_or_blank_terminal_fields(conn, status, reason):
    attempt_id = "direct-attempt"
    db_memecoin.start_entry_attempt(
        conn,
        attempt_id=attempt_id,
        decision_started_at=NOW,
    )

    with pytest.raises(ValueError, match="terminal_(status|reason)"):
        db_memecoin.finish_entry_attempt(
            conn,
            attempt_id,
            status,
            reason,
            NOW,
            "{}",
        )

    row = conn.execute(
        "SELECT terminal_status, terminal_reason, completed_at "
        "FROM memecoin_entry_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    assert tuple(row) == (None, None, None)


def test_persistence_rejects_unlisted_stage(conn):
    attempt_id = "direct-attempt"
    db_memecoin.start_entry_attempt(
        conn,
        attempt_id=attempt_id,
        decision_started_at=NOW,
    )

    with pytest.raises(ValueError, match="unsupported entry-attempt stages"):
        db_memecoin.finish_entry_attempt(
            conn,
            attempt_id,
            "refused",
            "not approved",
            NOW,
            "{}",
            stages={
                "terminal_status": {
                    "completed_at": NOW,
                    "duration_ms": 1.0,
                }
            },
        )

    row = conn.execute(
        "SELECT terminal_status, terminal_reason, completed_at "
        "FROM memecoin_entry_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    assert tuple(row) == (None, None, None)


def test_exact_origin_attempt_lookup_is_indexed_and_never_crosses_events(conn):
    db_memecoin.start_entry_attempt(
        conn,
        attempt_id="attempt-event-1",
        origin_event_id="event-1",
        network_id="4663",
        mint="0xabc",
        decision_started_at="2026-07-15T12:00:00+00:00",
    )
    db_memecoin.finish_entry_attempt(
        conn,
        "attempt-event-1",
        "refused",
        "coin ran 5.2x above first-seen price (cap 2.0x)",
        "2026-07-15T12:00:00.029000+00:00",
        "{}",
        pending_id=None,
    )
    db_memecoin.start_entry_attempt(
        conn,
        attempt_id="attempt-event-2",
        origin_event_id="event-2",
        network_id="8453",
        mint="0xabc",
        decision_started_at="2026-07-15T12:00:01+00:00",
    )
    db_memecoin.finish_entry_attempt(
        conn,
        "attempt-event-2",
        "filled",
        "confirmed fill persisted",
        "2026-07-15T12:00:02+00:00",
        "{}",
        pending_id=12,
        position_id=34,
        tx_sig="0xdef",
    )

    row = db_memecoin.entry_attempt_for_origin(conn, "event-1")

    assert row["attempt_id"] == "attempt-event-1"
    assert row["terminal_status"] == "refused"
    assert row["terminal_reason"].startswith("coin ran 5.2x")
    assert row["pending_id"] is None
    assert db_memecoin.entry_attempt_for_origin(conn, "missing") is None
    assert db_memecoin.entry_attempt_for_origin(conn, "") is None
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(memecoin_entry_attempts)")}
    assert "idx_mc_entry_attempt_origin" in indexes


def test_attempt_table_check_rejects_partial_terminal_state(conn, event):
    trace = EntryAttemptTrace.start(conn, event, NOW, FakeClock())

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE memecoin_entry_attempts "
            "SET terminal_status='refused', terminal_reason=' ', completed_at=? "
            "WHERE attempt_id=?",
            (NOW, trace.attempt_id),
        )
    conn.rollback()


def test_finalized_attempt_is_immutable(conn, event):
    clock = FakeClock()
    trace = EntryAttemptTrace.start(conn, event, NOW, clock)

    assert trace.finish("refused", "not approved", pending_id=17, quote_price=0.30) is True
    assert trace.finish("filled", "confirmed", pending_id=99, quote_price=0.40) is False
    assert trace.mark("reservation") is False

    row = conn.execute(
        "SELECT terminal_status, terminal_reason, reservation_completed_at, "
        "pending_id, quote_price "
        "FROM memecoin_entry_attempts WHERE attempt_id=?",
        (trace.attempt_id,),
    ).fetchone()
    assert tuple(row) == ("refused", "not approved", None, 17, 0.30)


# --- measured vs inferred stage durations (2026-09-03) ----------------------------------------
# `mark()` falls back to `(now - self._last_mono) * 1000.0` — the gap since whatever was marked
# BEFORE it, not the stage's own work. Of the ten stage marks on the funded entry path exactly ONE
# passed a real measured duration (`reservation`, memecoin_evm_leg.py:415); the other nine inferred,
# and all ten land in identically-named `*_ms` columns that memecoin_latency_report.py aggregates
# side by side.
#
# That one ambiguity produced four wrong conclusions in a day: a phantom "~2.5s unaccounted"
# residual (the columns cannot sum to a budget); a stage ORDER putting safety before quote, when
# `quote_completed_at` actually precedes `safety_checks_completed_at` on 58 of 73 filled 4663 rows;
# `*_ms` columns disagreeing with timestamp differences (one row read `quote_ms` 2,115 against a
# 49,744 ms segment); and `safety_checks_ms` p50 1,534 ms against `evm.token_safety` p50 2,835 ms
# for the same work on the same 73 attempts — the substage was right, the stage column was not.
#
# The interval is still worth keeping: it tiles the timeline, which is what makes a real budget
# possible. It must simply not pass as the stage's own duration.


def _stages_after(conn, event, mark_fn):
    clock = FakeClock()
    trace = EntryAttemptTrace.start(conn, event, NOW, clock)
    mark_fn(trace, clock)
    trace.finish("filled", "filled")
    row = dict(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone())
    return json.loads(row["trace_json"])["stages"]


def test_an_explicit_duration_is_flagged_measured(conn, event):
    def go(trace, clock):
        clock.advance(0.5)
        trace.mark("reservation", duration_ms=120.0)
    stage = _stages_after(conn, event, go)["reservation"]
    assert stage["duration_ms"] == pytest.approx(120.0)
    assert stage["measured"] is True


def test_an_inferred_duration_is_flagged_not_measured(conn, event):
    """This is the gap since the PREVIOUS mark, not this stage's work."""
    def go(trace, clock):
        clock.advance(0.5)
        trace.mark("safety_checks")
    stage = _stages_after(conn, event, go)["safety_checks"]
    assert stage["duration_ms"] == pytest.approx(500.0)
    assert stage["measured"] is False, (
        "an inferred inter-mark interval must not present as a measured duration")


def test_both_kinds_are_distinguishable_within_one_attempt(conn, event):
    """The exact mix that misled everyone: one real duration beside inferred ones."""
    def go(trace, clock):
        clock.advance(0.2)
        trace.mark("reservation", duration_ms=120.0)
        clock.advance(0.3)
        trace.mark("safety_checks")
    stages = _stages_after(conn, event, go)
    assert stages["reservation"]["measured"] is True
    assert stages["safety_checks"]["measured"] is False


def test_submission_stages_are_inferred_too(conn, event):
    def go(trace, clock):
        clock.advance(0.4)
        trace.mark_submission("quote", 1, outcome="available")
    assert _stages_after(conn, event, go)["quote"]["measured"] is False


def test_the_safety_mark_passes_the_duration_it_already_computed():
    """`_mark_safety` computes a real duration for the `evm.token_safety` SUBSTAGE and passed
    nothing to `trace.mark("safety_checks")` on the very next line. Asserted over the AST rather
    than the source text, so this guard cannot match its own prose."""
    import ast
    import pathlib
    src = pathlib.Path("sniper/services/memecoin_evm_leg.py").read_text()
    sites = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "mark"):
            continue
        if not (node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "safety_checks"):
            continue
        sites.append({kw.arg for kw in node.keywords})
    assert sites, "no trace.mark('safety_checks') call site found"
    for kwargs in sites:
        assert "duration_ms" in kwargs, (
            "safety_checks is marked without the duration already in scope one line above")


def test_persistence_rejects_a_stage_dict_that_omits_the_measured_flag(conn, event):
    """The flag is REQUIRED at the boundary, not defaulted. A default would silently reintroduce the
    ambiguity it exists to remove: a stage whose duration might be its own work or might be the gap
    since the previous mark, with nothing in the row saying which."""
    clock = FakeClock()
    trace = EntryAttemptTrace.start(conn, event, NOW, clock)
    with pytest.raises(ValueError, match="invalid entry-attempt stage data"):
        db_memecoin.finish_entry_attempt(
            conn, trace.attempt_id, "filled", "filled", "2026-07-15T12:00:01+00:00", "{}",
            stages={"reservation": {"completed_at": "2026-07-15T12:00:01+00:00",
                                    "duration_ms": 500.0}})


def test_persistence_rejects_a_non_boolean_measured_flag(conn, event):
    clock = FakeClock()
    trace = EntryAttemptTrace.start(conn, event, NOW, clock)
    with pytest.raises(ValueError, match="must be a bool"):
        db_memecoin.finish_entry_attempt(
            conn, trace.attempt_id, "filled", "filled", "2026-07-15T12:00:01+00:00", "{}",
            stages={"reservation": {"completed_at": "2026-07-15T12:00:01+00:00",
                                    "duration_ms": 500.0, "measured": "yes"}})
