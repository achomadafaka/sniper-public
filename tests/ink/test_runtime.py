import sqlite3
import os
import subprocess
import sys

import pytest

from sniper.persistence import db_memecoin as db
from sniper.services import memecoin_real_runner as runner
from sniper.settings import memecoin as cfg


def test_ink_reader_requires_explicit_opt_in():
    env = os.environ.copy()
    env.pop("MEMECOIN_INK_SUBBLOCKS_ENABLED", None)
    result = subprocess.run(
        [sys.executable, "-c", "from sniper.settings import memecoin as m; "
         "assert m.MEMECOIN_INK_SUBBLOCKS_ENABLED is False"],
        env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_enabled_ink_cannot_hide_missing_context_behind_solana(monkeypatch):
    from sniper.services import memecoin_evm_leg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"57073"})
    monkeypatch.setattr(cfg, "ink_capability_reason", lambda: None)
    monkeypatch.setattr(memecoin_evm_leg, "build_real_evm_context", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError, match="57073"):
        runner.build_real_evm_contexts(conn=None)


def test_entry_off_keeps_ink_recovery_context_for_unresolved_order(monkeypatch):
    from sniper.services import memecoin_evm_leg
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    db.reserve_pending(conn, mint="0x" + "b" * 40, source_user_id="source",
                       strategy="alpha_b", notional=10.0,
                       reserved_at="2026-09-29T09:00:00Z", network_id="57073")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", set())
    seen = []
    def build(chain, **kwargs):
        seen.append((chain, kwargs.get("for_recovery")))
        return {"chain_id": chain, "exit_fn": lambda: None}
    monkeypatch.setattr(memecoin_evm_leg, "build_real_evm_context", build)
    contexts = runner.build_real_evm_contexts(conn=conn)
    assert contexts["57073"]["exit_fn"] is not None
    assert seen == [("57073", True)]


def test_removed_ink_allowlist_blocks_manual_entry_even_with_recovery_context(ink_flow):
    result = ink_flow("admission_off")
    assert result["result"]["status"] == "disarmed"
    assert result["pending_rows"] == 0
    assert result["sign_calls"] == result["send_calls"] == 0


def test_empty_web_database_does_not_break_other_chain_contexts(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", False)
    with sqlite3.connect(":memory:") as conn:
        assert runner.build_real_evm_contexts(conn=conn) == {}


def _open_ink_position(conn):
    pid = db.open_position(
        conn, source_user_id="source", source_handle="source", mint="0x" + "b" * 40, ticker="INK",
        entry_price=2.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=1.0,
        their_size_usd=10.0, opened_at="2026-09-29T09:00:00Z", strategy="alpha_b",
        execution_mode="real", network_id="57073")
    db.insert_fill(conn, position_id=pid, kind="entry", price=2.0, qty=5.0, notional=10.0,
                   fill_source="chain", fraction=1.0, created_at="2026-09-29T09:00:00Z")
    conn.commit()
    return pid


def test_entry_off_keeps_ink_recovery_context_for_an_open_position(monkeypatch):
    """Rollback with open Ink INVENTORY (not only a pending reservation): entries off, the Ink
    context is still built, for recovery, so exits and manual close keep working."""
    from sniper.services import memecoin_evm_leg
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    _open_ink_position(conn)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", set())
    seen = []
    monkeypatch.setattr(memecoin_evm_leg, "build_real_evm_context", lambda chain, **kw: (
        seen.append((chain, kw.get("for_recovery"))) or {"chain_id": chain, "exit_fn": lambda: None}))
    contexts = runner.build_real_evm_contexts(conn=conn)
    assert set(contexts) == {"57073"} and seen == [("57073", True)]


def _state_root(tmp_path, *, open_ink):
    state = tmp_path / "state"
    (state / "data").mkdir(parents=True)
    (state / "logs").mkdir()
    (state / ".fomo.env").write_text("MEMECOIN_REAL_ENABLED=0\n")
    ledger = sqlite3.connect(state / "sniper_tracker.db")
    ledger.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(ledger)
    if open_ink:
        _open_ink_position(ledger)
    ledger.close()
    return state


def test_deploy_validation_refuses_a_release_that_cannot_recover_open_ink_inventory(tmp_path):
    """The deploy gate runs the candidate release's own config validator in a fresh interpreter.
    With entries off but an open Ink position and no Ink RPC/custody configured, the release cannot
    manage that inventory, so the gate must refuse it and name the chain."""
    from pathlib import Path
    from scripts.memecoin_runtime_verify import _default_config_validator
    repo = Path(__file__).resolve().parents[2]
    with pytest.raises(ValueError, match="57073"):
        _default_config_validator(repo, _state_root(tmp_path / "a", open_ink=True))
    _default_config_validator(repo, _state_root(tmp_path / "b", open_ink=False))   # control: passes


def test_an_absent_funded_table_is_no_ink_state_but_the_other_one_still_counts():
    """build_real_evm_contexts asks this for EVERY chain's startup; it raised when only one of its
    two tables existed, which aborted context building for all chains. An absent table holds no
    Ink state; the table that does exist is still read."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    _open_ink_position(conn)
    conn.execute("DROP TABLE memecoin_pending_entries")
    assert db.ink_recovery_required(conn) is True
    # (Pending rows without a positions table is not an "absent table": pending_totals reads both,
    # and that contradiction raises, loudly.)
    conn3 = sqlite3.connect(":memory:")
    conn3.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn3)
    conn3.execute("DROP TABLE memecoin_pending_entries")
    assert db.ink_recovery_required(conn3) is False


def test_a_failing_read_never_hides_open_ink_inventory():
    """A read that FAILS is not 'no Ink state': it raises, so startup cannot drop an Ink executor
    for inventory it could not see."""
    base = sqlite3.connect(":memory:")
    base.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(base)
    _open_ink_position(base)

    class Locked:
        def execute(self, sql, *args):
            if "memecoin_positions" in sql and "sqlite_master" not in sql:
                raise sqlite3.OperationalError("database is locked")
            return base.execute(sql, *args)
    with pytest.raises(sqlite3.OperationalError):
        db.ink_recovery_required(Locked())
