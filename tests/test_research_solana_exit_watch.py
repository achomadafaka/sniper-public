"""The ledger joins of scripts/research/solana_exit_watch.py on a fixture DB.

The fixture lives here rather than in the script's --selftest because the brain index scans
scripts/ for CREATE/INSERT text and would list a read-only research script as a ledger writer.
"""
import contextlib
import sqlite3

from scripts.research import solana_exit_watch as w

FIXTURE = """
    CREATE TABLE memecoin_positions(id, mint, execution_mode, network_id, opened_recorded_at, closed_at, close_claim_id);
    CREATE TABLE memecoin_position_marks(chain_id, mint, sweep_ts, source);
    CREATE TABLE memecoin_exit_trigger_audit(id INTEGER PRIMARY KEY, position_id, reason, allowed, checked_at, detail, lane);
    CREATE TABLE memecoin_submission_attempts(action, owner_kind, owner_id, created_at);
    INSERT INTO memecoin_positions VALUES(1,'M','real','solana','2026-09-27T11:00:00.000000+00:00',NULL,'CL');
    INSERT INTO memecoin_position_marks VALUES('solana','M','2026-09-27T11:10:00.000000+00:00','fastlane_sol_swap'),
        ('solana','M','2026-09-27T11:30:00.000000+00:00','fastlane_sol_swap'),
        ('solana','M','2026-09-27T11:31:00.000000+00:00','fastlane_sol_swap'),
        ('solana','M','2026-09-27T11:32:00.000000+00:00','fastlane_mid');
    INSERT INTO memecoin_exit_trigger_audit(position_id,reason,allowed,checked_at,detail,lane) VALUES
        (1,'vol_death',1,'2026-09-27T11:20:00.000000+00:00','measured_state_still silence=40s','fastlane_poll'),
        (1,'vol_death',0,'2026-09-27T11:25:00.000000+00:00','state_moving silence=10s','fastlane_poll'),
        (1,'mirror_source_sell',1,'2026-09-27T11:40:00.000000+00:00',
         '{"seen_at":"2026-09-27T11:40:00.000000+00:00","recv_to_decision_ms":3.0,"lock_wait_ms":0.5}','source_sell_mirror');
    INSERT INTO memecoin_submission_attempts VALUES('exit','close_claim','CL','2026-09-27T11:40:00.250000+00:00');
"""


def test_ledger_joins_on_fixture(tmp_path):
    with contextlib.closing(sqlite3.connect(str(tmp_path / "ledger.db"))) as c:
        c.executescript(FIXTURE)
        since, cut, now = w.ts("2026-09-27T11:00:00Z"), w.ts("2026-09-27T11:24:00Z"), w.ts("2026-09-27T11:44:00Z")
        d = w.density(c, since, cut, now)
        assert d["before"] == {"marks": 1, "held_min": 24.0, "per_min": 0.04}
        assert d["after"]["marks"] == 2          # the fastlane_mid mark is not a swap
        v = w.voldeath(c, since, cut)
        assert v["before"][("measured_state_still", 1)] == 1
        assert v["after"][("state_moving", 0)] == 1
        m = w.mirror(c, since)
        assert m[0]["receipt_to_send_ms"] == 250.0 and m[0]["lock_wait_ms"] == 0.5


def test_pure_selftest_still_runs():
    w.selftest()
