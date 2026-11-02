"""The ledger join of scripts/research/source_rebuy_funded.py on a fixture DB.

The fixture lives here rather than in the script's --selftest because the brain index scans
scripts/ for CREATE/INSERT text and would list a read-only research script as a ledger writer.
"""
import sqlite3

from scripts.research import source_rebuy_funded as s

FIXTURE = """
    CREATE TABLE memecoin_positions (id, network_id, source_user_id, source_handle, mint, opened_at,
        origin_event_id, entry_notional, realized_pnl, execution_mode, is_open, exit_reason);
    CREATE TABLE memecoin_events (source_user_id, mint, network_id, side, fomo_event_id, received_at);
    INSERT INTO memecoin_positions VALUES (1,'solana','u','h','M','2026-09-20T00:00:10+00:00',
        'onchain:SIGX',100,-40,'real',0,'b_stop');
    INSERT INTO memecoin_events VALUES ('u','M','solana','buy','swap:raybot:SIGX:a:m','2026-09-20T00:00:05+00:00');
"""


def test_trigger_and_other_sources_do_not_make_a_rebuy():
    conn = sqlite3.connect(":memory:")
    conn.executescript(FIXTURE)
    # the J7 copy of the trigger itself is not prior history
    r = s.load(conn, "2026-09-12")[0]
    assert (r["cls"], r["cross"]) == ("FIRST", "SOLO")
    # another source's sell is not his own: still FIRST, but OTHER_SOLD
    conn.execute("INSERT INTO memecoin_events VALUES ('v','M','solana','sell','swap:raybot:V1:a:m',"
                 "'2026-09-19T23:30:00+00:00')")
    r = s.load(conn, "2026-09-12")[0]
    assert (r["cls"], r["cross"]) == ("FIRST", "OTHER_SOLD")
    # his own earlier sell makes the copied buy a re-buy
    conn.execute("INSERT INTO memecoin_events VALUES ('u','M','solana','sell','swap:raybot:OLD:a:m',"
                 "'2026-09-19T23:00:00+00:00')")
    assert s.load(conn, "2026-09-12")[0]["cls"] == "REBUY_AFTER_SELL"


def test_selftest_runs():
    s.selftest()
