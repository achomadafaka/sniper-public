"""Off-path desk expense requests and atomic, receipt-bound valuation on real SQLite files."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import sqlite3
import threading

import pytest

from sniper.persistence import db_memecoin as m

AT = "2026-10-21T12:00:00+00:00"
KNOWN = "2026-10-21T12:00:02+00:00"
FILL_KEYS = {"kind", "id", "tx_sig", "qty", "notional", "funding_asset",
             "funding_spent_atomic", "funding_sol_usd", "network_fee_lamports",
             "priority_fee_lamports", "rent_lamports", "returned_rent_lamports", "created_at"}


def connect(path):
    c = sqlite3.connect(str(path), timeout=3)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def ledger(tmp_path):
    c = connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(c)
    m.register_desk_cohort(c, producer="H", policy={"fixture": "cost-store"},
                           prompt_version="fixture", created_at=AT, cohort_id="cohort:H")
    yield c
    c.close()


def position(c, *, sig="entry-sig", marker=True, source="desk", mode="real", mint=None,
             network="solana", fill=True):
    pid = c.execute("INSERT INTO memecoin_positions(desk_cohort_id,signal_source,execution_mode,"
                    "network_id,mint,opened_at,is_open,entry_notional,desk_opportunity_id) "
                    "VALUES('cohort:H',?,?,?,?,?,1,25,?)",
                    (source, mode, network, mint or sig, AT, "opportunity:" + sig)).lastrowid
    if marker:
        c.execute("INSERT INTO memecoin_desk_cohort_costs "
                  "(cohort_id,event_id,kind,occurred_at,provenance,position_id) "
                  "VALUES('cohort:H',?,'execution',?,'receipt:unpriced',?)", (f"entry:{pid}", AT, pid))
    if fill:
        m.insert_fill(c, position_id=pid, kind="entry", price=1, qty=25, notional=25,
                      fill_source="chain", fraction=1, created_at=AT, tx_sig=sig,
                      network_fee_lamports=5000, priority_fee_lamports=1000,
                      rent_lamports=0, returned_rent_lamports=0, commit=False)
    c.commit()
    return pid


def pending(c, *, sig="failed-sig", status="failed", source="desk", marker=False):
    pid = c.execute("INSERT INTO memecoin_pending_entries "
                    "(desk_cohort_id,desk_opportunity_id,signal_source,network_id,mint,"
                    "reserved_at,broadcast_at,tx_sig,status,signed_payload_b64) "
                    "VALUES('cohort:H','pending-op',?,'solana','pending-coin',?,?,?,?, 'SECRET')",
                    (source, AT, AT, sig, status)).lastrowid
    if marker:
        c.execute("INSERT INTO memecoin_desk_cohort_costs "
                  "(cohort_id,event_id,kind,occurred_at,provenance) "
                  "VALUES('cohort:H',?,'execution',?,'receipt:unpriced')",
                  (f"pending:{pid}:{sig}", AT))
    c.commit()
    return pid


def get_request(c, event):
    return next(r for r in m.list_unpriced_desk_execution_costs(c, limit=100) if r["event_id"] == event)


def evidence(request):
    return {k: request[k] for k in ("cohort_id", "event_id", "network_id", "mint", "tx_sig")} | {
        "event_at": AT, "fee_lamports": "5000", "native_usd": "100", "receipt": "fixture"}


def resolve(c, request, *, usd=1, refund=0, proof=None, commit=True, known_at=KNOWN):
    return m.resolve_desk_execution_cost(c, request=request, execution_usd=usd, refund_usd=refund,
        evidence=evidence(request) if proof is None else proof, known_at=known_at,
        provenance="receipt:fixture-v1", commit=commit)


def test_refund_and_execution_price_commit_together_without_a_transient_false_kill(ledger):
    c = ledger
    pid = position(c)
    c.execute("UPDATE memecoin_positions SET is_open=0,closed_at=?,realized_pnl=-195 WHERE id=?", (AT, pid))
    c.commit()
    request = get_request(c, f"entry:{pid}")
    for _ in range(2):
        resolve(c, request, usd=10, refund=10)
    assert m.desk_cohort_accounting(c, "cohort:H")["net_usd"] == -195
    assert not m.desk_cohort_killed(c, "cohort:H")
    assert [tuple(r) for r in c.execute("SELECT event_id,usd FROM memecoin_desk_cohort_costs ORDER BY event_id")] == [
        (f"entry:{pid}", 10), (f"refund:entry:{pid}", -10)]
    proof = c.execute("SELECT evidence_json,known_at,provenance FROM memecoin_desk_cost_evidence").fetchone()
    assert json.loads(proof[0]) == evidence(request)
    assert tuple(proof[1:]) == (KNOWN, "receipt:fixture-v1")


def test_pricing_failure_rolls_back_evidence_refund_primary_and_latch(ledger):
    c = ledger
    pid = position(c)
    request = get_request(c, f"entry:{pid}")
    c.execute("CREATE TEMP TRIGGER refuse_primary BEFORE UPDATE OF usd ON memecoin_desk_cohort_costs "
              "WHEN NEW.kind='execution' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    c.commit()
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        resolve(c, request, usd=300, refund=10)
    assert not c.in_transaction
    assert c.execute("SELECT COUNT(*) FROM memecoin_desk_cost_evidence").fetchone()[0] == 0
    assert [tuple(r) for r in c.execute("SELECT event_id,usd FROM memecoin_desk_cohort_costs")] == [(f"entry:{pid}", None)]
    assert not m.desk_cohort_killed(c, "cohort:H")


def test_successful_booking_invalidates_a_pending_request_before_any_writes(ledger):
    c = ledger
    pending_id = pending(c)
    request = get_request(c, f"pending:{pending_id}:failed-sig")
    pid = position(c, sig="failed-sig")
    c.execute("UPDATE memecoin_pending_entries SET status='confirmed',desk_opportunity_id=? WHERE id=?",
              ("opportunity:failed-sig", pending_id)); c.commit()
    with pytest.raises(ValueError, match="owner|stale|pending"):
        resolve(c, request, refund=2)
    assert c.execute("SELECT COUNT(*) FROM memecoin_desk_cost_evidence").fetchone()[0] == 0
    assert [r[0] for r in c.execute("SELECT event_id FROM memecoin_desk_cohort_costs")] == [f"entry:{pid}"]


def test_old_failed_retry_keeps_exact_identity_after_later_success(ledger):
    c = ledger
    pending_id = pending(c, marker=True)
    request = get_request(c, f"pending:{pending_id}:failed-sig")
    c.execute("UPDATE memecoin_pending_entries SET tx_sig='new-success',status='confirmed' WHERE id=?", (pending_id,))
    c.commit()
    resolve(c, request)
    assert m.desk_cohort_accounting(c, "cohort:H")["known_cost_usd"] == 1


def test_proven_zero_resolves_unknown_and_exact_replay_is_immutable(ledger):
    c = ledger
    pid = position(c)
    request = get_request(c, f"entry:{pid}")
    first = resolve(c, request, usd=0)
    assert resolve(c, copy.deepcopy(request), usd=0) == first
    assert m.desk_cohort_accounting(c, "cohort:H")["unknown_costs"] == 0
    for kwargs in ({"usd": 2}, {"known_at": "2026-10-21T12:00:03+00:00"},
                   {"proof": evidence(request) | {"fee_lamports": "6000"}}):
        with pytest.raises(ValueError, match="immutable|conflict"):
            resolve(c, request, **kwargs)


def test_pagination_exposes_an_unpriceable_oldest_without_starving_later_requests(ledger):
    c = ledger
    first = position(c, sig="missing", fill=False)
    second = position(c, sig="later")
    a = m.list_unpriced_desk_execution_costs(c, limit=1)
    assert len(a) == 1 and a[0]["event_id"] == f"entry:{first}"
    assert a[0]["ownership_error"] and a[0]["fill"] is None
    b = m.list_unpriced_desk_execution_costs(c, after=(a[0]["cohort_id"], a[0]["event_id"]), limit=1)
    assert len(b) == 1 and b[0]["event_id"] == f"entry:{second}"
    assert b[0]["ownership_error"] is None
    assert set(b[0]["fill"]) == FILL_KEYS
    assert not c.in_transaction
    with pytest.raises(ValueError, match="ownership|fill|signature"):
        resolve(c, a[0])


def test_reader_includes_settled_old_partials_and_current_gaps_without_secret_fields(ledger):
    c = ledger
    pid = position(c)
    resolve(c, get_request(c, f"entry:{pid}"), usd=0)
    m.insert_fill(c, position_id=pid, kind="derisk", price=1, qty=2, notional=2,
                  fill_source="chain", fraction=.1, created_at=AT, tx_sig="old-source-partial")
    c.execute("UPDATE memecoin_positions SET close_sig='current-exit' WHERE id=?", (pid,)); c.commit()
    pending_id = pending(c)
    rows = m.list_unpriced_desk_execution_costs(c)
    assert {r["event_id"] for r in rows} == {
        f"partial:{pid}:old-source-partial", f"exit:{pid}:current-exit", f"pending:{pending_id}:failed-sig"}
    assert "SECRET" not in json.dumps(rows)
    assert all(not r["marker_exists"] for r in rows)
    assert next(r for r in rows if r["event_kind"] == "partial")["fill"]["tx_sig"] == "old-source-partial"


def test_unsigned_copy_paper_and_model_rows_do_not_enter_execution_reader(ledger):
    c = ledger
    pending(c, sig=None, status="failed")
    pending(c, sig="copy-pending", source="slot_lane")
    position(c, sig="copy", source="slot_lane")
    position(c, sig="paper", mode="paper")
    c.execute("INSERT INTO memecoin_desk_cohort_costs(cohort_id,event_id,kind,occurred_at,provenance) "
              "VALUES('cohort:H','model-call','model',?,'fixture')", (AT,)); c.commit()
    assert m.list_unpriced_desk_execution_costs(c) == []


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5, "20"])
def test_reader_rejects_unbounded_or_invalid_limits(ledger, limit):
    with pytest.raises(ValueError, match="limit"):
        m.list_unpriced_desk_execution_costs(ledger, limit=limit)


@pytest.mark.parametrize("usd,refund", [(True, 0), (float("nan"), 0), (float("inf"), 0),
                                       (-1, 0), (0, -1), (0, True), (None, 0)])
def test_invalid_prices_never_complete_the_unknown(ledger, usd, refund):
    c = ledger
    pid = position(c)
    request = get_request(c, f"entry:{pid}")
    with pytest.raises(ValueError):
        resolve(c, request, usd=usd, refund=refund)
    assert m.desk_cohort_accounting(c, "cohort:H")["unknown_costs"] == 1


@pytest.mark.parametrize("field,value", [("cohort_id", "foreign"), ("event_id", "entry:999"),
                                        ("network_id", "1"), ("mint", "foreign"), ("tx_sig", "foreign"),
                                        ("event_at", "2026-10-21T12:00:03+00:00"), ("fee", float("nan"))])
def test_foreign_or_future_or_nonfinite_evidence_is_rejected(ledger, field, value):
    c = ledger
    pid = position(c)
    request = get_request(c, f"entry:{pid}")
    with pytest.raises(ValueError):
        resolve(c, request, proof=evidence(request) | {field: value})
    assert m.desk_cohort_accounting(c, "cohort:H")["unknown_costs"] == 1


def test_resolver_refuses_implicit_caller_commit_and_preserves_wip_on_failure(ledger):
    c = ledger
    pid = position(c)
    request = get_request(c, f"entry:{pid}")
    c.execute("UPDATE memecoin_positions SET ticker='caller-wip' WHERE id=?", (pid,))
    with pytest.raises(ValueError, match="transaction"):
        resolve(c, request)
    assert c.in_transaction
    with pytest.raises(ValueError):
        resolve(c, request, proof=evidence(request) | {"tx_sig": "wrong"}, commit=False)
    assert c.in_transaction and m.get_position(c, pid)["ticker"] == "caller-wip"
    resolve(c, request, usd=200, commit=False)
    assert m.desk_cohort_killed(c, "cohort:H")
    c.rollback()
    assert not m.desk_cohort_killed(c, "cohort:H")
    assert m.desk_cohort_accounting(c, "cohort:H")["unknown_costs"] == 1


def test_concurrent_conflicting_valuations_have_one_immutable_winner(ledger):
    c = ledger
    pid = position(c)
    request = get_request(c, f"entry:{pid}")
    path = c.execute("PRAGMA database_list").fetchone()[2]
    barrier = threading.Barrier(2, timeout=3)

    def run(usd):
        connection = connect(path)
        try:
            barrier.wait()
            try:
                resolve(connection, request, usd=usd)
                return ("ok", usd)
            except ValueError:
                return ("conflict", usd)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(run, amount) for amount in (1, 200)]
        results = [f.result(timeout=8) for f in futures]
    assert sorted(state for state, _ in results) == ["conflict", "ok"]
    winner = next(usd for state, usd in results if state == "ok")
    assert m.desk_cohort_accounting(c, "cohort:H")["known_cost_usd"] == winner
    assert c.execute("SELECT COUNT(*) FROM memecoin_desk_cost_evidence").fetchone()[0] == 1


def test_excluded_copy_marker_cannot_hide_a_later_desk_page(ledger):
    c = ledger
    position(c, sig="copy", source="slot_lane")
    pid = position(c, sig="real-desk")
    rows = m.list_unpriced_desk_execution_costs(c, limit=1)
    assert len(rows) == 1 and rows[0]["event_id"] == f"entry:{pid}"


def test_readonly_enumeration_does_not_leave_a_transaction_or_allow_writes(ledger):
    from sniper.persistence.db_core import get_readonly_connection
    c = ledger
    pid = position(c)
    ro = get_readonly_connection(c.execute("PRAGMA database_list").fetchone()[2])
    try:
        rows = m.list_unpriced_desk_execution_costs(ro)
        assert rows[0]["event_id"] == f"entry:{pid}"
        assert not ro.in_transaction
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM memecoin_desk_cohort_costs")
    finally:
        ro.close()


def test_ambiguous_entry_stays_visible_and_original_fill_cannot_change_after_lookup(ledger):
    c = ledger
    pid = position(c)
    request = get_request(c, f"entry:{pid}")
    c.execute("UPDATE memecoin_fills SET tx_sig='changed-signature' WHERE position_id=?", (pid,)); c.commit()
    with pytest.raises(ValueError, match="ownership"):
        resolve(c, request)
    m.insert_fill(c, position_id=pid, kind="entry", price=1, qty=25, notional=25,
                  fill_source="chain", fraction=1, created_at=AT, tx_sig="another-signature")
    ambiguous = get_request(c, f"entry:{pid}")
    assert ambiguous["ownership_error"] and ambiguous["fill"] is None
    with pytest.raises(ValueError, match="ambiguous|ownership"):
        resolve(c, ambiguous)


@pytest.mark.parametrize("kind", ["partial", "exit"])
def test_existing_reset_marker_outlives_its_pointer_but_synthetic_request_cannot(kind, ledger):
    c = ledger
    pid = position(c)
    resolve(c, get_request(c, f"entry:{pid}"), usd=0)
    column = "derisk_sig" if kind == "partial" else "close_sig"
    c.execute(f"UPDATE memecoin_positions SET {column}='failed' WHERE id=?", (pid,)); c.commit()
    event = f"{kind}:{pid}:failed"
    stale = get_request(c, event)
    c.execute(f"UPDATE memecoin_positions SET {column}=NULL WHERE id=?", (pid,)); c.commit()
    with pytest.raises(ValueError, match="ownership"):
        resolve(c, stale)
    c.execute("INSERT INTO memecoin_desk_cohort_costs "
              "(cohort_id,event_id,kind,occurred_at,provenance,position_id) "
              "VALUES('cohort:H',?,'execution',?,'receipt:unpriced',?)", (event, AT, pid)); c.commit()
    durable = get_request(c, event)
    resolve(c, durable)
    assert m.desk_cohort_accounting(c, "cohort:H")["known_cost_usd"] == 1


def test_failed_primary_savepoint_keeps_unrelated_caller_work(ledger):
    c = ledger
    pid = position(c)
    request = get_request(c, f"entry:{pid}")
    c.execute("CREATE TEMP TRIGGER refuse_primary BEFORE UPDATE OF usd ON memecoin_desk_cohort_costs "
              "WHEN NEW.kind='execution' BEGIN SELECT RAISE(ABORT,'fixture failure'); END"); c.commit()
    c.execute("UPDATE memecoin_positions SET ticker='keep-me' WHERE id=?", (pid,))
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        resolve(c, request, usd=300, refund=2, commit=False)
    assert c.in_transaction and m.get_position(c, pid)["ticker"] == "keep-me"
    assert c.execute("SELECT COUNT(*) FROM memecoin_desk_cost_evidence").fetchone()[0] == 0
    assert [r[0] for r in c.execute("SELECT event_id FROM memecoin_desk_cohort_costs")] == [f"entry:{pid}"]
    c.commit()


def test_autocommit_handle_prices_atomically_and_evidence_cannot_be_rewritten(ledger):
    c = ledger
    pid = position(c)
    request = get_request(c, f"entry:{pid}")
    c.isolation_level = None
    with pytest.raises(ValueError, match="transaction"):
        resolve(c, request, commit=False)
    resolve(c, request)
    assert not c.in_transaction
    for sql in ("UPDATE memecoin_desk_cost_evidence SET provenance='changed'",
                "DELETE FROM memecoin_desk_cost_evidence"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            c.execute(sql)


@pytest.mark.parametrize("known_at", ["2026-10-21T11:59:59+00:00", "2026-10-21T12:00:02"])
def test_known_at_cannot_precede_occurrence_or_lose_its_timezone(ledger, known_at):
    c = ledger
    pid = position(c)
    with pytest.raises(ValueError):
        resolve(c, get_request(c, f"entry:{pid}"), known_at=known_at)


def test_refund_never_reopens_a_previously_killed_cohort(ledger):
    c = ledger
    pid = position(c)
    m.record_desk_cohort_cost(c, cohort_id="cohort:H", event_id="already-paid", usd=200,
        kind="model", occurred_at=AT, provenance="fixture")
    resolve(c, get_request(c, f"entry:{pid}"), usd=0, refund=500)
    assert m.desk_cohort_killed(c, "cohort:H")
    assert m.desk_cohort_accounting(c, "cohort:H")["net_usd"] == 300


@pytest.mark.parametrize("kind,column", [("exit", "close"), ("partial", "derisk")])
def test_synthetic_pointer_cost_keeps_its_submission_occurrence(kind, column, ledger):
    c = ledger
    pid = position(c)
    resolve(c, get_request(c, f"entry:{pid}"), usd=0)
    c.execute(f"UPDATE memecoin_positions SET {column}_sig='submitted', {column}_pending_at=? WHERE id=?",
              (AT, pid)); c.commit()
    request = get_request(c, f"{kind}:{pid}:submitted")
    assert request["occurred_at"] == AT
    resolve(c, request)
    assert m.desk_cohort_accounting(c, "cohort:H")["unknown_costs"] == 0


def test_candidate_query_anchors_position_reads_to_the_desk_cohort_index(ledger):
    c = ledger
    statements = []
    c.set_trace_callback(statements.append)
    try:
        m.list_unpriced_desk_execution_costs(c)
    finally:
        c.set_trace_callback(None)
    query = next(s for s in statements if s.startswith("WITH candidates"))
    plan = [r[3] for r in c.execute("EXPLAIN QUERY PLAN " + query)]
    assert sum("SEARCH" in row and "idx_mc_pos_desk_cohort" in row for row in plan) == 3, plan
    assert not any("idx_mc_pos_mode_open_closed" in row for row in plan), plan


def test_failed_commit_rolls_back_all_pricing_and_preserves_the_original_error(ledger):
    class FailOneCommit(sqlite3.Connection):
        fail_next_commit = False

        def commit(self):
            if self.fail_next_commit:
                self.fail_next_commit = False
                self.before_failure = (
                    self.execute("SELECT COUNT(*) FROM memecoin_desk_cost_evidence").fetchone()[0],
                    self.execute("SELECT COUNT(*) FROM memecoin_desk_cohort_costs WHERE usd IS NOT NULL").fetchone()[0],
                    self.execute("SELECT COUNT(*) FROM memecoin_desk_cohort_trips").fetchone()[0])
                self.failure = sqlite3.OperationalError("disk I/O error")
                raise self.failure
            return super().commit()

    observer = ledger
    pid = position(observer)
    path = observer.execute("PRAGMA database_list").fetchone()[2]
    c = sqlite3.connect(path, factory=FailOneCommit)
    c.row_factory = sqlite3.Row
    try:
        request = get_request(c, f"entry:{pid}")
        c.fail_next_commit = True
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error") as failure:
            resolve(c, request, usd=300, refund=10)
        assert failure.value is c.failure
        assert c.before_failure == (1, 2, 1)
        assert not c.in_transaction
        assert c.execute("SELECT COUNT(*) FROM memecoin_desk_cost_evidence").fetchone()[0] == 0
        assert [tuple(r) for r in c.execute("SELECT event_id,usd FROM memecoin_desk_cohort_costs")] == [
            (f"entry:{pid}", None)]
        assert not m.desk_cohort_killed(c, "cohort:H")
        c.commit()
        assert observer.execute("SELECT COUNT(*) FROM memecoin_desk_cost_evidence").fetchone()[0] == 0
        assert m.desk_cohort_accounting(observer, "cohort:H")["unknown_costs"] == 1
        assert not m.desk_cohort_killed(observer, "cohort:H")
    finally:
        c.close()
