"""De-allowlisting an EVM chain must never strand its exits, and must never re-open its entries
(Gamma H3 on the eth-mainnet spec, 2026-09-30; Alpha: ship first, on its own).

Before: `build_real_evm_contexts` built contexts for allowlisted chains only, plus Ink while
`ink_recovery_required`. Removing 8453/56/4663/5042 from MEMECOIN_EVM_ALLOWLIST with a position
open left that position with no context: the exit sweep skips a position without one, and recovery
leaves its reservation reserved. Now every funded-capable EVM chain with open real inventory or an
unresolved reservation keeps an exit-and-recovery context (`for_recovery=True`). A chain whose
entries are not open (off the allowlist, funded trading off, or its venue not direct) gets a
RECOVERY-ONLY context, which the runner's context gate refuses before any market read (Gamma HIGH
and MEDIUM on ee7bf1112). Routing already drops non-allowlisted chains; `evm_admission_refusal` is
defense in depth. A recovery context that cannot be built pages and the runner continues.
"""
import inspect
import sqlite3

import pytest

from sniper.persistence import db_memecoin as db
from sniper.services import memecoin_evm_leg as leg
from sniper.services import memecoin_real_runner as runner
from sniper.settings import memecoin as cfg


def _ledger():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    return conn


def _open_position(conn, network):
    pid = db.open_position(
        conn, source_user_id="source", source_handle="source", mint="0x" + "b" * 40, ticker="X",
        entry_price=2.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=1.0,
        their_size_usd=10.0, opened_at="2026-09-30T09:00:00Z", strategy="alpha_b",
        execution_mode="real", network_id=network)
    db.insert_fill(conn, position_id=pid, kind="entry", price=2.0, qty=5.0, notional=10.0,
                   fill_source="chain", fraction=1.0, created_at="2026-09-30T09:00:00Z")
    conn.commit()
    return pid


@pytest.fixture
def built(monkeypatch):
    seen = []

    def build(chain, **kwargs):
        seen.append((chain, kwargs.get("for_recovery", False)))
        return {"chain_id": chain, "exit_fn": lambda: None}
    monkeypatch.setattr(leg, "build_real_evm_context", build)
    return seen


@pytest.mark.parametrize("network", ["8453", "56", "4663", "5042"])
def test_a_de_allowlisted_chain_with_an_open_position_keeps_a_recovery_context(
        monkeypatch, built, network):
    conn = _ledger()
    _open_position(conn, network)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663"} - {network})
    contexts = runner.build_real_evm_contexts(conn=conn)
    assert network in contexts and contexts[network]["exit_fn"] is not None
    assert (network, True) in built and contexts[network]["recovery_only"] is True


def test_an_unresolved_reservation_alone_keeps_the_context(monkeypatch, built):
    conn = _ledger()
    db.reserve_pending(conn, mint="0x" + "c" * 40, source_user_id="source", strategy="alpha_b",
                       notional=10.0, reserved_at="2026-09-30T09:00:00Z", network_id="56")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453"})
    assert "56" in runner.build_real_evm_contexts(conn=conn)
    assert ("56", True) in built


def test_real_disabled_still_keeps_recovery_contexts(monkeypatch, built):
    conn = _ledger()
    _open_position(conn, "8453")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453", "4663"})
    assert set(runner.build_real_evm_contexts(conn=conn)) == {"8453"}
    assert built == [("8453", True)]


def test_no_inventory_builds_nothing_extra(monkeypatch, built):
    conn = _ledger()
    _open_position(conn, "solana")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663"})
    assert set(runner.build_real_evm_contexts(conn=conn)) == {"4663"}
    assert built == [("4663", False)]


def test_a_recovery_context_that_cannot_be_built_pages_and_the_rest_still_run(monkeypatch):
    """Page and continue, never raise: a crash-loop would take every chain's exits dark."""
    conn = _ledger()
    _open_position(conn, "8453")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663"})
    monkeypatch.setattr(leg, "build_real_evm_context", lambda chain, **k: (
        {"chain_id": chain} if chain == "4663" else None))
    pages = []
    from sniper.services import memecoin_alerts
    monkeypatch.setattr(memecoin_alerts, "send_alert", lambda kind, text, **k: pages.append((kind, text)))
    contexts = runner.build_real_evm_contexts(conn=conn)
    assert set(contexts) == {"4663"}
    assert [kind for kind, _t in pages] == ["RECOVERY-GAP"] and "8453" in pages[0][1]


def test_an_allowlisted_chain_with_its_venue_disabled_is_recovery_only(monkeypatch, built):
    """Gamma HIGH on ee7bf1112: for_recovery skips the venue switch, so the context must be marked
    recovery-only and the gate must refuse its entries."""
    conn = _ledger()
    _open_position(conn, "8453")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453", "4663"})
    monkeypatch.delenv("MEMECOIN_EVM_EXEC_VENUE_8453", raising=False)          # disabled
    contexts = runner.build_real_evm_contexts(conn=conn)
    assert contexts["8453"]["recovery_only"] is True and ("8453", True) in built
    assert runner.entry_route_closed(contexts, "8453") is True


def test_an_open_chain_is_not_recovery_only_even_with_inventory(monkeypatch, built):
    conn = _ledger()
    _open_position(conn, "8453")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453"})
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_8453", "direct")
    contexts = runner.build_real_evm_contexts(conn=conn)
    assert "recovery_only" not in contexts["8453"] and built == [("8453", False)]
    assert runner.entry_route_closed(contexts, "8453") is False


def test_the_context_gate_runs_before_any_market_read():
    """Gamma MEDIUM: a recovery-only chain's buy must cost zero reads, as on main."""
    src = inspect.getsource(runner.make_real_hook)
    gate = src.index("entry_route_closed(contexts, chain)")
    assert gate < src.index("m.resolve_mint_mcap(")
    assert "if chain not in contexts:" not in src
    assert runner.entry_route_closed({}, "56") is True


@pytest.mark.parametrize("chain, reason", [("8453", "chain_not_allowlisted"),
                                           ("56", "chain_not_allowlisted"),
                                           ("57073", "ink_admission_disabled")])
def test_entries_on_a_chain_out_of_the_allowlist_refuse_even_with_a_context(monkeypatch, chain, reason):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663"})
    assert leg.evm_admission_refusal(chain) == {"status": "disarmed", "reason": reason}
    assert leg.evm_admission_refusal("4663") is None


def test_the_admission_refusal_runs_before_any_quote_in_evm_real_entry():
    src = inspect.getsource(leg.evm_real_entry)
    assert "evm_admission_refusal(chain)" in src
    assert src.index("evm_admission_refusal(chain)") < src.index("execute_fn(")


def test_the_exit_sweep_resolves_a_recovery_only_context_for_the_stranded_position(monkeypatch, built):
    """Gamma LOW: the context's presence is not enough; the sweep's own resolver must hand it to the
    position, and nothing on the exit path may skip a recovery-only context."""
    conn = _ledger()
    pid = _open_position(conn, "56")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663"})
    contexts = runner.build_real_evm_contexts(conn=conn)
    ctx = runner.context_for_position(contexts, db.get_position(conn, pid))
    assert ctx is contexts["56"] and ctx["recovery_only"] is True and ctx["exit_fn"] is not None
    assert "recovery_only" not in inspect.getsource(runner.real_exit_sweep)
