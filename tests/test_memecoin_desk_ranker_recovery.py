"""Codex's ranker crash-recovery witnesses on d55441c22 (crash at every write phase, a restart on the
same or the next minute, repeated cycles), ported unchanged except the import."""
from pathlib import Path
import sqlite3

import pytest

import test_memecoin_desk_ranker as F       # the ranker tests' tape fixture

W = F.W


class InjectedCrash(RuntimeError):
    pass


class CrashConnection:
    def __init__(self, conn, kind, phase):
        self.conn, self.kind, self.phase = conn, kind, phase

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def __enter__(self):
        self.conn.__enter__()
        if self.kind == "registry" and self.phase == "after_rows_before_registry":
            raise InjectedCrash(self.phase)
        return self

    def __exit__(self, *exc):
        if self.kind == "registry" and exc[0] is None:
            if self.phase == "before_registry_commit":
                self.conn.rollback()
                raise InjectedCrash(self.phase)
            if self.phase == "after_registry_commit":
                self.conn.__exit__(*exc)
                raise InjectedCrash(self.phase)
        return self.conn.__exit__(*exc)

    def executemany(self, *args, **kwargs):
        if self.kind == "day" and self.phase == "before_rows":
            raise InjectedCrash(self.phase)
        return self.conn.executemany(*args, **kwargs)


def inject_crash(monkeypatch, phase):
    real_rw = W.rw

    def rw(path, ddl):
        conn = real_rw(path, ddl)
        return CrashConnection(conn, "registry" if path.name == "candidates.db" else "day", phase)

    monkeypatch.setattr(W, "rw", rw)
    return real_rw


def assert_no_closed_candidate_without_row(base, oid, rank_at):
    candidate = F._cand(base, oid)
    if candidate.get("closed_at") is not None:
        assert oid in F._rows(base, rank_at), candidate


def assert_cycle_totals_match_rows(base, rank_at):
    conn = sqlite3.connect(Path(base) / "ranker" / (F.DAY + ".db"))
    try:
        recorded = conn.execute("SELECT n_rows,n_scored,n_shortlisted,n_unscored FROM cycles WHERE rank_at=?",
                                (rank_at,)).fetchone()
        actual = conn.execute("SELECT COUNT(*), COALESCE(SUM(rank IS NOT NULL),0), "
                              "COALESCE(SUM(shortlisted),0), COALESCE(SUM(rank IS NULL),0) "
                              "FROM rank_rows WHERE rank_at=?", (rank_at,)).fetchone()
        assert recorded == actual, {"cycle_totals": recorded, "stored_rank_rows": actual}
    finally:
        conn.close()


def left_band_fixture(base):
    tape = F.Tape(base)
    candidate = F._candidate(tape, 0, F.T0 + 10)
    tape.swap(candidate["mint"], F.T0 + 50, price_x=3.0)
    tape.heartbeat(F.T0 + 59)
    return tape, candidate, F.T0 + 60


def test_future_slot_cannot_change_past_tape_health(tmp_path):
    tape = F.Tape(tmp_path)
    rank_at = F.T0 + 120
    tape.swap("HEAD", rank_at - 1)
    conn = tape.files.conn("swaps", rank_at)
    conn.execute("UPDATE swaps SET slot=100")
    tape.files.commit()
    before = W.tape_health([conn], [], rank_at)
    assert before == ("ok", 1.0)
    tape.swap("FUTURE", rank_at + 1)
    conn.execute("UPDATE swaps SET slot=501 WHERE mint='FUTURE'")
    tape.files.commit()
    assert W.tape_health([conn], [], rank_at) == before


@pytest.mark.parametrize("phase", ["before_rows", "after_rows_before_registry", "before_registry_commit",
                                    "after_registry_commit"])
def test_crash_restart_preserves_denominator_and_totals(tmp_path, monkeypatch, phase):
    tape, candidate, rank_at = left_band_fixture(tmp_path)
    oid = candidate["ticket_id"]
    real_rw = inject_crash(monkeypatch, phase)
    with pytest.raises(InjectedCrash, match=phase):
        W.run_cycle(tmp_path, rank_at)
    assert_no_closed_candidate_without_row(tmp_path, oid, rank_at)
    rows_after_crash = F._rows(tmp_path, rank_at)
    if phase == "before_rows":
        assert rows_after_crash == {}
    else:
        assert rows_after_crash[oid] == (None, "left_band", 0)
    monkeypatch.setattr(W, "rw", real_rw)
    W.run_cycle(tmp_path, rank_at)
    assert F._rows(tmp_path, rank_at)[oid] == (None, "left_band", 0)
    assert F._cand(tmp_path, oid)["close_reason"] == "left_band"
    assert_no_closed_candidate_without_row(tmp_path, oid, rank_at)
    assert_cycle_totals_match_rows(tmp_path, rank_at)


def test_repeated_completed_cycle_preserves_totals(tmp_path):
    tape, candidate, rank_at = left_band_fixture(tmp_path)
    first = W.run_cycle(tmp_path, rank_at)
    assert_cycle_totals_match_rows(tmp_path, rank_at)
    before = F._rows(tmp_path, rank_at)
    second = W.run_cycle(tmp_path, rank_at)
    assert F._rows(tmp_path, rank_at) == before
    assert_cycle_totals_match_rows(tmp_path, rank_at)
    assert second == first


def test_next_cycle_restart_recovers_prior_durable_shortlist(tmp_path, monkeypatch):
    tape = F.Tape(tmp_path)
    candidate = F._candidate(tape, 0, F.T0 + 10)
    oid, rank_at = candidate["ticket_id"], F.T0 + 60
    tape.heartbeat(rank_at - 1)
    real_rw = inject_crash(monkeypatch, "after_rows_before_registry")
    with pytest.raises(InjectedCrash):
        W.run_cycle(tmp_path, rank_at)
    assert F._rows(tmp_path, rank_at)[oid] == (1, "complete", 1)
    assert F._cand(tmp_path, oid) == {}
    monkeypatch.setattr(W, "rw", real_rw)
    tape.swap(candidate["mint"], rank_at + 30, price_x=3.0)
    tape.heartbeat(rank_at + 59)
    W.run_cycle(tmp_path, rank_at + 60)  # main resumes the current boundary after a restart
    registered = F._cand(tmp_path, oid)
    assert registered["close_reason"] == "left_band"
    assert registered["first_shortlisted_at"] == rank_at, {
        "durable_prior_row": F._rows(tmp_path, rank_at)[oid], "registry": registered}
    assert W.shortlisted(tmp_path, oid)


# ---------------------------------------------------------------- Codex's legacy-schema witnesses on f5a030786

LEGACY_DAY_DDL = """CREATE TABLE rank_rows(rank_at REAL NOT NULL, ranker_version TEXT NOT NULL,
    opportunity_id TEXT NOT NULL, mint TEXT, rank INTEGER, score REAL, inputs_hash TEXT, features TEXT,
    completeness TEXT NOT NULL, shortlisted INTEGER NOT NULL, PRIMARY KEY(rank_at, opportunity_id))"""


def old_day_database(base, rank_at, oid="desk:U:legacy-shortlisted"):
    path = Path(base) / "ranker" / (F.DAY + ".db")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(LEGACY_DAY_DDL)
    conn.execute("INSERT INTO rank_rows VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (rank_at, "u-runners-v1", oid, "LEGACY", 1, 0.0, "hash", "{}", "complete", 1))
    conn.commit()
    conn.close()
    return oid


def test_existing_ten_column_day_database_is_upgraded_without_losing_rows(tmp_path):
    tape = F.Tape(tmp_path)
    rank_at = F.T0 + 120
    old_oid = old_day_database(tmp_path, rank_at - 60)
    candidate = F._candidate(tape, 0, F.T0 + 70)
    tape.heartbeat(rank_at - 1)
    W.run_cycle(tmp_path, rank_at)
    assert old_oid in F._rows(tmp_path, rank_at - 60)
    assert candidate["ticket_id"] in F._rows(tmp_path, rank_at)
    assert_cycle_totals_match_rows(tmp_path, rank_at)


def test_reconcile_does_not_silently_lose_legacy_shortlist(tmp_path):
    rank_at = F.T0 + 120
    old_oid = old_day_database(tmp_path, rank_at - 60)
    cand_db = W.rw(tmp_path / "ranker" / "candidates.db", W.DDL_CAND)
    try:
        W.reconcile(cand_db, tmp_path, rank_at)
    finally:
        cand_db.close()
    recovered = F._cand(tmp_path, old_oid)
    assert recovered.get("first_shortlisted_at") == rank_at - 60, recovered
