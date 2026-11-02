"""Operator exclusion sticks against the follow-sync (delete/pause alone don't)."""
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_follow_sync import sync_followed


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def test_excluded_wallet_survives_sync_even_if_still_followed():
    c = _conn()
    m.add_wallet(c, "u1", "alice", source="follow")
    m.set_wallet_excluded(c, "u1", True)                 # operator cut
    assert m.curated_user_ids(c) == set()                # excluded -> not curated
    # sync sees u1 still in the fomo following graph -> must NOT un-pause/re-activate it
    sync_followed(c, None, user_fn=lambda a: {"id": "u1", "userHandle": "alice"},
                  following_fn=lambda a, uid: ([{"id": "u1", "userHandle": "alice"}], True))
    w = m.get_wallet_by_handle(c, "alice")
    assert w["excluded"] == 1 and w["paused"] == 1
    assert m.curated_user_ids(c) == set()                # still cut after sync


def test_resume_reincludes_excluded_wallet():
    c = _conn()
    m.add_wallet(c, "u1", "alice", source="follow")
    m.set_wallet_excluded(c, "u1", True)
    m.set_wallet_paused(c, "u1", False)                  # a plain unpause does NOT clear excluded
    assert "u1" not in m.curated_user_ids(c)
    m.set_wallet_excluded(c, "u1", False)                # explicit re-include
    assert "u1" in m.curated_user_ids(c)


def test_exclusion_clears_all_funded_authority():
    c = _conn()
    m.add_wallet(c, "u1", "alice", source="follow")
    m.set_wallet_chain_armed(c, "u1", "4663", True, force=True)

    m.set_wallet_excluded(c, "u1", True)
    row = c.execute(
        "SELECT excluded,paused,real_approved,real_chains FROM memecoin_wallets "
        "WHERE source_user_id='u1'"
    ).fetchone()

    assert dict(row) == {
        "excluded": 1,
        "paused": 1,
        "real_approved": 0,
        "real_chains": None,
    }
