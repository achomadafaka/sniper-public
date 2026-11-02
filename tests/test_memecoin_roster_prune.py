"""The prune retires roster wallets J7 no longer tracks — and refuses when it cannot tell.

The dangerous failure is not "a wallet survived". It is retiring the roster because a socket was
down. An empty tracked set is indistinguishable from "J7 tracks nobody", and acting on it would
retire all 553 wallets, so both the empty and the stale case must refuse before anything moves.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sniper.persistence import db_memecoin as m
from scripts.memecoin_roster_prune import freshness_error, report


NOW = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


# --- the refusals -------------------------------------------------------------------------------

def test_an_empty_tracked_set_refuses():
    assert "empty" in freshness_error(None, NOW, max_age_min=15)
    assert "empty" in freshness_error("", NOW, max_age_min=15)


def test_a_stale_list_refuses():
    err = freshness_error(_iso(16), NOW, max_age_min=15)
    assert err and "16.0 minutes old" in err


def test_a_fresh_list_passes():
    assert freshness_error(_iso(14), NOW, max_age_min=15) is None
    assert freshness_error(_iso(0), NOW, max_age_min=15) is None


def test_an_unparseable_stamp_refuses():
    """An unreadable stamp is not fresh. Failing open here retires the roster."""
    assert freshness_error("not-a-date", NOW, max_age_min=15) is not None


def test_a_future_stamp_refuses():
    """The host has run hours ahead before; a future stamp would make any list look fresh."""
    assert freshness_error(_iso(-30), NOW, max_age_min=15) is not None


def test_prune_refuses_a_replaced_account_even_with_fresh_complete_old_lists(tmp_path, monkeypatch):
    from scripts import memecoin_roster_prune as prune
    from sniper.feeds.j7_feed import account_fingerprint
    from sniper.persistence import db_core
    from sniper.util import envfiles, state_paths
    c = _conn()
    old = account_fingerprint("old-token")
    for kind in ("fomo", "pump"):
        m.replace_j7_tracked(c, 0, kind, [{"source_user_id": "u1"}],
                             seen_at=datetime.now(timezone.utc).isoformat(),
                             account_fingerprint=old)
    token_file = tmp_path / ".j7.env"
    token_file.write_text("J7_TOKEN=new-token\n")
    monkeypatch.setenv("J7_ENV_FILE", str(token_file))
    monkeypatch.setattr(envfiles, "load_env_files", lambda *_: None)
    monkeypatch.setattr(state_paths, "state_root", lambda: tmp_path)
    monkeypatch.setattr(state_paths, "configure_memecoin_state_env", lambda: None)
    monkeypatch.setattr(db_core, "get_connection", lambda: c)
    assert prune.main(["--dry-run"]) == 2


# --- selection ----------------------------------------------------------------------------------

def test_an_empty_tracked_table_selects_NOBODY():
    """THE 553-WALLET ACCIDENT. No stored list must never read as "J7 tracks nobody"."""
    c = _conn()
    m.add_wallet(c, "u1", "alice")
    m.add_wallet(c, "u2", "bob")
    assert m.wallets_not_tracked(c) == []


def test_only_untracked_wallets_are_selected():
    c = _conn()
    m.add_wallet(c, "u1", "alice")
    m.add_wallet(c, "u2", "bob")
    m.replace_j7_tracked(c, 0, "fomo", [{"source_user_id": "u1"}], seen_at=_iso(1))
    rows = m.wallets_not_tracked(c)
    assert [r["source_user_id"] for r in rows] == ["u2"]


def test_a_wallet_tracked_on_the_pump_list_is_kept():
    """Pump is a separate roster on the same account; missing it would retire live wallets."""
    c = _conn()
    m.add_wallet(c, "p1", "pumper")
    m.replace_j7_tracked(c, 0, "fomo", [], seen_at=_iso(1))
    m.replace_j7_tracked(c, 0, "pump", [{"source_user_id": "p1"}], seen_at=_iso(1))
    assert m.wallets_not_tracked(c) == []


# --- the retire is a retire, never a delete -----------------------------------------------------

def test_retire_keeps_the_row_and_its_identity():
    c = _conn()
    m.add_wallet(c, "u2", "bob")
    c.execute("UPDATE memecoin_wallets SET real_approved=1 WHERE source_user_id='u2'")
    c.commit()
    assert m.remove_wallet(c, "u2") is True
    row = c.execute("SELECT * FROM memecoin_wallets WHERE source_user_id='u2'").fetchone()
    assert row is not None, "the wallet was DELETED; retire must never erase identity"
    assert row["handle"] == "bob"
    assert row["excluded"] == 1 and row["paused"] == 1 and row["real_approved"] == 0


def test_the_audit_row_names_the_operator_and_the_reason():
    c = _conn()
    m.add_wallet(c, "u2", "bob")
    m.record_wallet_audit(c, "u2", "retire", actor="operator",
                          reason="not on J7 lists 2026-09-22", now_iso=NOW.isoformat())
    c.commit()
    row = c.execute("SELECT * FROM memecoin_wallet_approvals WHERE source_user_id='u2'").fetchone()
    assert row["actor"] == "operator"
    assert row["reason"] == "not on J7 lists 2026-09-22"
    assert row["action"] == "retire"


# --- the report the operator reads before confirming --------------------------------------------

def test_the_report_leads_with_counts_and_names_approved_wallets():
    rows = [
        {"source_user_id": "a", "handle": "armed", "real_approved": 1, "paused": 0, "excluded": 0},
        {"source_user_id": "b", "handle": "idle", "real_approved": 0, "paused": 1, "excluded": 0},
        {"source_user_id": "c", "handle": None, "real_approved": 0, "paused": 0, "excluded": 1},
    ]
    text = report(rows)
    assert "3 roster wallets are on no J7 tracked list" in text
    assert "real_approved=1 : 1" in text
    assert "paused=1        : 1" in text
    assert "excluded=1      : 1" in text
    assert "armed" in text and "(no handle)" in text
