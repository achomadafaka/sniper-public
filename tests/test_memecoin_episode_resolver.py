import sqlite3
from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.services.memecoin_episode import episode_facts, resolve_episode

def _db():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c); return c

def test_first_signal_opens_episode():
    c = _db()
    eid, is_new = resolve_episode(c, "solana", "MINTA", "s1", "2026-07-12T10:00:00+00:00")
    assert is_new is True and eid

def test_second_signal_same_open_token_reuses_episode():
    c = _db()
    e1, _ = resolve_episode(c, "solana", "MINTA", "s1", "2026-07-12T10:00:00+00:00")
    e2, is_new = resolve_episode(c, "solana", "MINTA", "s2", "2026-07-12T10:05:00+00:00")
    assert e2 == e1 and is_new is False
    assert c.execute("SELECT consensus_cluster_count FROM memecoin_token_episodes WHERE episode_id=?",
                     (e1,)).fetchone()[0] == 2

def test_closed_episode_lets_a_new_one_open():
    c = _db()
    e1, _ = resolve_episode(c, "solana", "MINTA", "s1", "2026-07-12T10:00:00+00:00")
    c.execute("UPDATE memecoin_token_episodes SET status='closed' WHERE episode_id=?", (e1,)); c.commit()
    e2, is_new = resolve_episode(c, "solana", "MINTA", "s2", "2026-07-13T10:00:00+00:00")
    assert is_new is True and e2 != e1


def test_episode_identity_is_network_and_mint():
    c = _db()
    base, _ = resolve_episode(c, "8453", "0xSame", "base-1",
                              "2026-07-12T10:00:00+00:00")
    robin, _ = resolve_episode(c, "4663", "0xSame", "robin-1",
                               "2026-07-12T10:00:01+00:00")
    assert base != robin


def test_duplicate_event_does_not_increment_episode_sequence():
    c = _db()
    eid, _ = resolve_episode(
        c, "solana", "MINTA", "s1", "2026-07-12T10:00:00+00:00",
        source_price=1.0, executable_price=1.1,
    )
    same, is_new = resolve_episode(
        c, "solana", "MINTA", "s1", "2026-07-12T10:00:01+00:00",
        source_price=9.0, executable_price=9.0,
    )
    assert same == eid and is_new is False
    facts = episode_facts(c, eid, current_executable_price=1.21)
    assert facts["episode_entry_sequence"] == 1
    assert facts["episode_first_source_price"] == 1.0
    assert facts["episode_first_executable_price"] == 1.1
    assert abs(facts["episode_executable_runup_pct"] - 10.0) < 1e-9


def test_quiet_gap_rolls_episode_without_manual_close():
    c = _db()
    first, _ = resolve_episode(c, "solana", "MINTA", "s1",
                               "2026-07-12T10:00:00+00:00")
    second, is_new = resolve_episode(c, "solana", "MINTA", "s2",
                                     "2026-07-12T11:00:01+00:00")
    assert is_new is True and second != first
    old = c.execute(
        "SELECT status, ended_ts FROM memecoin_token_episodes WHERE episode_id=?", (first,),
    ).fetchone()
    assert old["status"] == "closed"
    assert old["ended_ts"] == "2026-07-12T11:00:01+00:00"
