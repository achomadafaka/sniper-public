from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sniper.feeds.fomo_feed import normalize_feed_item
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_arming


@pytest.fixture(autouse=True)
def _no_consensus_gate(monkeypatch):
    """These tests exercise identity/provenance/trace mechanics, not the 2026-07-17 consensus
    gate — their fixture events have no prior buyers and would otherwise refuse above $1M."""
    from sniper.settings import memecoin as _cfg
    monkeypatch.setattr(_cfg, "MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP", 0.0)
from sniper.services import memecoin_real_runner as runner
from sniper.services.memecoin_evm_risk import reserve_entry_evm
from sniper.services.memecoin_real_runner import resolve_entry_identity
from sniper.services.memecoin_risk import reserve_entry
from sniper.settings import memecoin as cfg


NOW = "2026-07-15T12:00:00+00:00"


class FakeClock:
    def __init__(self):
        self.elapsed = 100.0

    def perf_counter(self):
        return self.elapsed

    def advance(self, seconds):
        self.elapsed += seconds


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _event(*, network_id="4663", mint="0xabc", event_id="event-1", uid="u1",
           market_cap=1_000_000.0, source="global_feed"):
    return SimpleNamespace(
        side="buy",
        source_user_id=uid,
        source_handle="alice",
        mint=mint,
        ticker="ABC",
        network_id=network_id,
        their_price=0.25,
        their_market_cap=market_cap,
        their_size_usd=5_000.0,
        is_first_buy=True,
        fomo_event_id=event_id,
        created_at="2026-07-15T11:59:59+00:00",
        received_at="legacy-host-clock",
        received_at_normalized="2026-07-15T11:59:59.900000+00:00",
        clock_domain="observed_real_utc",
        lag_s=0.1,
        signal_source=source,
    )


def _insert_event_network(conn, *, mint, network_id, event_id):
    conn.execute(
        "INSERT INTO memecoin_events(fomo_event_id, mint, network_id) VALUES (?,?,?)",
        (event_id, mint, network_id),
    )
    conn.commit()


def _arm(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663", "8453"})


def _approve(conn, uid="u1", chains=("4663",)):
    """Arming is per chain (2026-07-30) - the declared chains are now actually applied."""
    m.add_wallet(conn, uid, "alice")
    m.set_wallet_real_approved(conn, uid, True, chains=list(chains), force=True)


def _evm_context(seen):
    def execute(conn, pending_id, mint, size, now_iso):
        seen["pending"] = m.pending_row(conn, pending_id)
        return {"status": "unknown", "reason": "confirmation unknown", "tx_hash": "0xtx"}

    return {
        "usdc_fn": lambda: 300.0,
        "native_fn": lambda: 1.0,
        "token_safety_fn": lambda mint, **kw: (True, "ok"),
        "execute_fn": execute,
        "kill_path": None,
    }


def _solana_context(seen):
    def execute(conn, pending_id, mint, size, now_iso, impact_cap_pct=None,
                entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        seen["pending"] = m.pending_row(conn, pending_id)
        seen["entry_window"] = (entry_min_slot, entry_max_slot, entry_head_fn)
        return {"status": "unknown", "reason": "confirmation unknown", "sig": "sig"}

    return {
        "usdc_fn": lambda: 300.0,
        "sol_fn": lambda: 1.0,
        "token_safety_fn": lambda mint, **kw: (
            True,
            "ok transfer_fee_bps=300 fee_limit_bps=300 fee_authority_immutable=true",
        ),
        "execute_fn": execute,
        "kill_path": None,
    }


def test_explicit_4663_never_calls_resolver():
    conn = _conn()
    resolver = Mock(side_effect=AssertionError("resolver must not run"))

    result = resolve_entry_identity(conn, _event(network_id="4663"), resolve_fn=resolver)

    assert result.network_id == "4663"
    assert result.status == "resolved"
    assert result.source == "explicit_event"
    assert resolver.call_count == 0


def test_indexed_event_history_distinguishes_one_multiple_and_none():
    conn = _conn()
    _insert_event_network(conn, mint="0xone", network_id="4663", event_id="one-1")
    _insert_event_network(conn, mint="0xone", network_id="4663", event_id="one-2")
    _insert_event_network(conn, mint="0xmany", network_id="4663", event_id="many-1")
    _insert_event_network(conn, mint="0xmany", network_id="8453", event_id="many-2")

    assert m.concrete_event_networks(conn, "0xone") == ("4663",)
    assert m.concrete_event_networks(conn, "0xmany") == ("4663", "8453")
    assert m.concrete_event_networks(conn, "0xnone") == ()


def test_conflicting_history_stays_ambiguous_without_external_guess():
    conn = _conn()
    _insert_event_network(conn, mint="0xabc", network_id="4663", event_id="a")
    _insert_event_network(conn, mint="0xabc", network_id="8453", event_id="b")
    resolver = Mock(return_value="4663")

    result = resolve_entry_identity(
        conn, _event(network_id="evm-unknown"), resolve_fn=resolver,
    )

    assert result.status == "ambiguous"
    assert result.network_id == "evm-unknown"
    assert result.source == "conflicting_history"
    assert resolver.call_count == 0


def test_single_history_network_resolves_locally_without_external_call():
    conn = _conn()
    _insert_event_network(conn, mint="0xabc", network_id="4663", event_id="a")
    resolver = Mock(side_effect=AssertionError("local evidence must win"))

    result = resolve_entry_identity(
        conn, _event(network_id="evm-unknown"), resolve_fn=resolver,
    )

    assert result.network_id == "4663"
    assert result.status == "resolved"
    assert result.source == "event_history"
    assert resolver.call_count == 0


def test_explicit_4663_reaches_pending_with_exact_event_provenance(monkeypatch):
    _arm(monkeypatch)
    conn = _conn()
    _approve(conn)
    resolver = Mock(side_effect=AssertionError("resolver must not run"))
    seen = {}
    ev = _event(network_id="4663")
    hook = runner.make_real_hook(
        None,
        {"4663": _evm_context(seen)},
        now_fn=lambda: NOW,
        trade_size=10.0,
        max_age_s=120,
        resolve_fn=resolver,
        trace_clock=FakeClock(),
    )

    result = hook(conn, ev)

    assert result["status"] == "unknown"
    assert resolver.call_count == 0
    row = seen["pending"]
    assert row["network_id"] == "4663"
    assert row["origin_event_id"] == "event-1"
    assert row["attempt_id"]
    assert row["signal_source"] == "global_feed"
    assert row["source_price"] == pytest.approx(0.25)
    assert row["source_market_cap"] == pytest.approx(1_000_000.0)
    assert row["observed_at"] == ev.received_at_normalized
    assert row["clock_domain"] == "observed_real_utc"


def test_unknown_evm_resolves_once_and_raw_event_stays_unchanged(monkeypatch):
    _arm(monkeypatch)
    conn = _conn()
    _approve(conn)
    resolver = Mock(return_value="4663")
    monkeypatch.setattr("sniper.execution.solana_quote.resolve_evm_chain", resolver)
    seen = {}
    ev = _event(network_id="evm-unknown")
    hook = runner.make_real_hook(
        None,
        {"4663": _evm_context(seen)},
        now_fn=lambda: NOW,
        trade_size=10.0,
        max_age_s=120,
        resolve_fn=resolver,
        trace_clock=FakeClock(),
    )

    result = hook(conn, ev)

    assert result["status"] == "unknown"
    assert resolver.call_count == 1
    assert ev.network_id == "evm-unknown"
    assert seen["pending"]["network_id"] == "4663"
    trace = conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone()
    assert trace["network_id"] == "4663"
    assert trace["chain_resolution_completed_at"] is not None
    assert trace["market_data_completed_at"] is not None
    assert trace["reservation_completed_at"] is not None
    assert trace["safety_checks_completed_at"] is not None
    assert trace["terminal_status"] == "unknown"
    assert trace["terminal_reason"] == "confirmation unknown"


def test_conflicting_history_finishes_ambiguous_without_reservation(monkeypatch):
    _arm(monkeypatch)
    conn = _conn()
    _approve(conn)
    _insert_event_network(conn, mint="0xabc", network_id="4663", event_id="a")
    _insert_event_network(conn, mint="0xabc", network_id="8453", event_id="b")
    resolver = Mock(return_value="4663")
    hook = runner.make_real_hook(
        None, {}, now_fn=lambda: NOW, trade_size=10.0, max_age_s=120,
        resolve_fn=resolver, trace_clock=FakeClock(),
    )

    result = hook(conn, _event(network_id="evm-unknown"))

    assert result["status"] == "ambiguous_network"
    assert resolver.call_count == 0
    assert m.pending_totals(conn) == (0, 0.0)
    trace = conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone()
    assert trace["terminal_status"] == "refused"
    assert trace["terminal_reason"] == "ambiguous_network"


def test_missing_history_and_failed_external_resolution_finishes_unresolved(monkeypatch):
    _arm(monkeypatch)
    conn = _conn()
    _approve(conn)
    resolver = Mock(return_value=None)
    hook = runner.make_real_hook(
        None, {}, now_fn=lambda: NOW, trade_size=10.0, max_age_s=120,
        resolve_fn=resolver, trace_clock=FakeClock(),
    )

    result = hook(conn, _event(network_id="evm-unknown"))

    assert result["status"] == "unresolved_network"
    assert resolver.call_count == 1
    assert m.pending_totals(conn) == (0, 0.0)
    trace = conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone()
    assert trace["terminal_status"] == "refused"
    assert trace["terminal_reason"] == "unresolved_network"


def test_explicit_supported_identity_without_context_finishes_unroutable(monkeypatch):
    _arm(monkeypatch)
    conn = _conn()
    resolver = Mock(side_effect=AssertionError("explicit network must not resolve"))
    hook = runner.make_real_hook(
        None, {}, now_fn=lambda: NOW, trade_size=10.0, max_age_s=120,
        resolve_fn=resolver, trace_clock=FakeClock(),
    )

    result = hook(conn, _event(network_id="4663"))

    assert result == {"status": "unroutable_chain", "chain": "4663"}
    assert resolver.call_count == 0
    assert m.pending_totals(conn) == (0, 0.0)
    trace = conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone()
    assert trace["network_id"] == "4663"
    assert trace["terminal_status"] == "refused"
    assert trace["terminal_reason"] == "unroutable_chain"


@pytest.mark.parametrize(
    ("network_id", "mint"),
    [("4663", "So1Base58Mint"), ("solana", "0xEvmMint")],
)
def test_concrete_network_mint_family_mismatch_refuses_before_approval_or_execution(
    monkeypatch, network_id, mint,
):
    conn = _conn()
    raw_event = _event(network_id=network_id, mint=mint)
    raw_identity = (raw_event.network_id, raw_event.mint)
    monkeypatch.setattr(
        m,
        "real_approved_user_ids",
        Mock(side_effect=AssertionError("approval must not run for inconsistent identity")),
    )
    contexts = {
        "solana": {
            "usdc_fn": Mock(side_effect=AssertionError("balance read must not run")),
            "sol_fn": Mock(side_effect=AssertionError("balance read must not run")),
            "token_safety_fn": Mock(side_effect=AssertionError("safety must not run")),
            "execute_fn": Mock(side_effect=AssertionError("executor must not run")),
        },
        "4663": {
            "usdc_fn": Mock(side_effect=AssertionError("balance read must not run")),
            "native_fn": Mock(side_effect=AssertionError("balance read must not run")),
            "token_safety_fn": Mock(side_effect=AssertionError("safety must not run")),
            "execute_fn": Mock(side_effect=AssertionError("executor must not run")),
        },
    }
    hook = runner.make_real_hook(
        None, contexts, now_fn=lambda: NOW, trade_size=10.0, max_age_s=120,
        trace_clock=FakeClock(),
    )

    result = hook(conn, raw_event)

    assert result == {
        "status": "network_mint_mismatch",
        "reason": "network_mint_mismatch",
        "chain": network_id,
    }
    assert m.pending_totals(conn) == (0, 0.0)
    assert (raw_event.network_id, raw_event.mint) == raw_identity
    trace = conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone()
    assert trace["terminal_status"] == "refused"
    assert trace["terminal_reason"] == "network_mint_mismatch"


@pytest.mark.parametrize(
    ("approved", "market_cap", "expected_status", "expected_reason"),
    [
        (False, 1_000_000.0, "not_approved", "not_approved"),
        (True, 300_000_000.0, "skipped_mcap", "over_ceiling"),
        (True, None, "skipped_mcap", "mcap_unresolved"),
    ],
)
def test_pre_entry_refusals_finish_once_with_deterministic_reason(
    monkeypatch, approved, market_cap, expected_status, expected_reason,
):
    _arm(monkeypatch)
    # 2026-07-16: with assume-micro on (default), an unresolved 4663 mcap no longer refuses —
    # pin the flag off here; this test is about refusal traces finishing exactly once.
    # blind_mcap_fn injected so the unresolved row never attempts a live GT/RPC lookup.
    from sniper.settings import memecoin as _cfg
    monkeypatch.setattr(_cfg, "MEMECOIN_REAL_MCAP_ASSUME_MICRO", False)
    conn = _conn()
    if approved:
        _approve(conn)
    else:
        m.add_wallet(conn, "u1", "alice")
    hook = runner.make_real_hook(
        None, {"4663": _evm_context({})}, now_fn=lambda: NOW,
        trade_size=10.0, max_age_s=120,
        trace_clock=FakeClock(), blind_mcap_fn=lambda chain, mint: None,
    )

    result = hook(conn, _event(network_id="4663", market_cap=market_cap))

    assert result["status"] == expected_status
    trace = conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone()
    assert trace["terminal_status"] == "refused"
    # The mcap hook renders the numbers around the identifier (2026-09-09), so the family token is
    # the SUFFIX rather than the whole string. This test is about a refusal trace finishing exactly
    # once with a deterministic reason — the token is what makes it deterministic, and it is still
    # exact for every non-mcap case.
    if expected_reason in ("over_ceiling", "under_floor"):
        assert trace["terminal_reason"].endswith("(%s)" % expected_reason)
    else:
        assert trace["terminal_reason"] == expected_reason
    assert conn.execute("SELECT COUNT(*) FROM memecoin_entry_attempts").fetchone()[0] == 1


def test_unexpected_pre_entry_exception_finishes_once_and_reraises(monkeypatch):
    conn = _conn()
    m.add_wallet(conn, "u1", "alice")
    monkeypatch.setattr(
        m, "real_approved_user_ids", Mock(side_effect=RuntimeError("approval read failed")),
    )
    hook = runner.make_real_hook(
        None, {"4663": _evm_context({})}, now_fn=lambda: NOW,
        trade_size=10.0, max_age_s=120,
        trace_clock=FakeClock(),
    )

    with pytest.raises(RuntimeError, match="approval read failed"):
        hook(conn, _event(network_id="4663"))

    trace = conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone()
    assert trace["terminal_status"] == "error"
    assert trace["terminal_reason"] == "RuntimeError: approval read failed"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_entry_attempts").fetchone()[0] == 1


def test_trace_start_failure_does_not_change_funded_result(monkeypatch):
    _arm(monkeypatch)
    conn = _conn()
    seen = {}
    monkeypatch.setattr(
        "sniper.services.memecoin_entry_trace.EntryAttemptTrace.start",
        Mock(side_effect=sqlite3.OperationalError("trace unavailable")),
    )

    event = _event(network_id="solana", mint="So1Mint")
    event.source_block_number = 100
    result = runner.real_entry_hook(
        conn,
        event,
        {"solana": _solana_context(seen)},
        now_fn=lambda: NOW,
        trade_size=10.0,
        max_age_s=120,
        trace_clock=FakeClock(),
    )

    assert result["status"] == "unknown"
    assert seen["pending"]["mint"] == "So1Mint"
    assert seen["entry_window"][:2] == (None, None)
    assert callable(seen["entry_window"][2])
    assert conn.execute("SELECT COUNT(*) FROM memecoin_entry_attempts").fetchone()[0] == 0


def test_trace_start_skips_successful_write_inside_caller_transaction():
    conn = _conn()
    conn.execute("CREATE TABLE caller_work(value TEXT)")
    conn.commit()
    conn.execute("INSERT INTO caller_work(value) VALUES ('uncommitted')")

    trace = runner._start_trace_best_effort(
        conn, _event(network_id="solana", mint="So1Mint"), NOW, FakeClock(),
    )

    assert trace is None
    assert conn.execute("SELECT value FROM caller_work").fetchone()[0] == "uncommitted"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_entry_attempts").fetchone()[0] == 0
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0] == 0


def test_trace_start_skip_preserves_caller_transaction_when_start_would_fail(monkeypatch):
    conn = _conn()
    conn.execute("CREATE TABLE caller_work(value TEXT)")
    conn.commit()
    conn.execute("INSERT INTO caller_work(value) VALUES ('uncommitted')")
    start = Mock(side_effect=sqlite3.OperationalError("trace unavailable"))
    monkeypatch.setattr(
        "sniper.services.memecoin_entry_trace.EntryAttemptTrace.start", start,
    )

    trace = runner._start_trace_best_effort(
        conn, _event(network_id="solana", mint="So1Mint"), NOW, FakeClock(),
    )

    assert trace is None
    assert start.call_count == 0
    assert conn.execute("SELECT value FROM caller_work").fetchone()[0] == "uncommitted"
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0] == 0


def test_trace_start_partial_transaction_is_rolled_back_before_funded_reservation(monkeypatch):
    _arm(monkeypatch)
    conn = _conn()
    seen = {}

    def broken_start(trace_conn, *args, **kwargs):
        trace_conn.execute("BEGIN")
        raise sqlite3.OperationalError("trace commit unavailable")

    monkeypatch.setattr(
        "sniper.services.memecoin_entry_trace.EntryAttemptTrace.start", broken_start,
    )

    event = _event(network_id="solana", mint="So1Mint")
    event.source_block_number = 100
    result = runner.real_entry_hook(
        conn,
        event,
        {"solana": _solana_context(seen)},
        now_fn=lambda: NOW,
        trade_size=10.0,
        max_age_s=120,
        trace_clock=FakeClock(),
    )

    assert result["status"] == "unknown"
    assert seen["pending"]["mint"] == "So1Mint"
    assert seen["entry_window"][:2] == (None, None)
    assert callable(seen["entry_window"][2])


def test_resolution_latency_uses_fresh_time_for_staleness(monkeypatch):
    _arm(monkeypatch)
    conn = _conn()
    _approve(conn)
    wall = {"now": "2026-07-15T12:00:00+00:00"}
    event = _event(network_id="evm-unknown")
    event.created_at = "2026-07-15T11:58:05+00:00"
    seen = {}

    def resolve(_mint):
        wall["now"] = "2026-07-15T12:00:10+00:00"
        return "4663"

    hook = runner.make_real_hook(
        None,
        {"4663": _evm_context(seen)},
        now_fn=lambda: wall["now"],
        trade_size=10.0,
        max_age_s=120,
        resolve_fn=resolve,
        trace_clock=FakeClock(),
    )

    result = hook(conn, event)

    assert result["status"] == "stale"
    assert "pending" not in seen
    assert m.pending_totals(conn) == (0, 0.0)
    trace = conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone()
    assert trace["decision_started_at"] == "2026-07-15T12:00:00+00:00"
    assert trace["market_data_completed_at"] is not None


def test_resolution_crossing_utc_day_uses_fresh_time_for_risk_reservation_and_execution(
    monkeypatch,
):
    _arm(monkeypatch)
    conn = _conn()
    _approve(conn)
    wall = {"now": "2026-07-15T23:59:59+00:00"}
    event = _event(network_id="evm-unknown")
    event.created_at = "2026-07-15T23:59:50+00:00"
    seen = {}
    original_latch = m.check_and_latch_daily_loss

    def resolve(_mint):
        wall["now"] = "2026-07-16T00:00:01+00:00"
        return "4663"

    def capture_day(risk_conn, day, *args, **kwargs):
        seen["risk_day"] = day
        return original_latch(risk_conn, day, *args, **kwargs)

    context = _evm_context(seen)
    execute = context["execute_fn"]

    def capture_execution(*args):
        seen["execution_now"] = args[-1]
        return execute(*args)

    context["execute_fn"] = capture_execution
    monkeypatch.setattr(m, "check_and_latch_daily_loss", capture_day)
    hook = runner.make_real_hook(
        None,
        {"4663": context},
        now_fn=lambda: wall["now"],
        trade_size=10.0,
        max_age_s=120,
        resolve_fn=resolve,
        trace_clock=FakeClock(),
    )

    result = hook(conn, event)

    assert result["status"] == "unknown"
    assert seen["risk_day"] == "2026-07-16"
    assert seen["pending"]["reserved_at"] == "2026-07-16T00:00:01+00:00"
    assert seen["execution_now"] == "2026-07-16T00:00:01+00:00"


def test_market_data_trace_covers_mcap_and_chase_lookups(monkeypatch):
    _arm(monkeypatch)
    conn = _conn()
    _approve(conn)
    seen = {}
    clock = FakeClock()

    def resolve_mcap(*args, **kwargs):
        clock.advance(2.0)
        return 1_000_000.0

    def stats(*args, **kwargs):
        clock.advance(3.0)
        return {"price": 0.25}

    monkeypatch.setattr(m, "resolve_mint_mcap", resolve_mcap)
    hook = runner.make_real_hook(
        None,
        {"4663": _evm_context(seen)},
        now_fn=lambda: NOW,
        trade_size=10.0,
        max_age_s=120,
        stats_fn=stats,
        trace_clock=clock,
    )

    result = hook(conn, _event(network_id="4663", market_cap=0.0))

    assert result["status"] == "unknown"
    trace = conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone()
    assert trace["market_data_ms"] == pytest.approx(5_000.0)
    assert trace["total_ms"] == pytest.approx(5_000.0)


def test_manual_funded_buy_creates_exactly_one_trace(monkeypatch):
    _arm(monkeypatch)
    conn = _conn()
    seen = {}
    buy = runner.make_real_buy_fn({"solana": _solana_context(seen)})

    result = buy(conn, "So1Manual", "u-manual", ticker="MAN", network_id="solana")

    assert result["status"] == "unknown"
    rows = conn.execute("SELECT * FROM memecoin_entry_attempts").fetchall()
    assert len(rows) == 1
    assert rows[0]["signal_source"] == "manual"
    assert rows[0]["origin_event_id"] is None
    assert rows[0]["reservation_completed_at"] is not None
    assert rows[0]["safety_checks_completed_at"] is not None
    trace = json.loads(rows[0]["trace_json"])
    substage = next(
        item for item in trace["substage_events"]
        if item["name"] == "token_safety"
    )
    assert substage["allowed"] is True
    assert "fee_limit_bps=300" in substage["reason"]
    assert seen["pending"]["attempt_id"] == rows[0]["attempt_id"]


@pytest.mark.parametrize(
    ("result", "terminal_status", "terminal_reason"),
    [
        ({"status": "disarmed"}, "refused", "disarmed"),
        ({"status": "dropped_base_asset"}, "refused", "dropped_base_asset"),
        ({"status": "dropped_network"}, "refused", "unsupported_or_unarmed_network"),
        ({"status": "evm_unarmed"}, "refused", "unsupported_or_unarmed_network"),
        ({"status": "rejected", "reason": "risk refused"}, "refused", "risk refused"),
        ({"status": "unsafe", "reason": "sell simulation failed"},
         "safety_failed", "sell simulation failed"),
        ({"status": "route_failed", "reason": "configured route unavailable"},
         "route_failed", "configured route unavailable"),
        ({"status": "filled"}, "filled", "filled"),
        ({"status": "failed", "reason": "send failed"}, "failed", "send failed"),
        ({"status": "aborted", "reason": "impact cap"}, "aborted", "impact cap"),
        ({"status": "unknown", "reason": "confirmation unknown"},
         "unknown", "confirmation unknown"),
    ],
)
def test_every_normal_leg_result_finishes_once_with_deterministic_terminal(
    monkeypatch, result, terminal_status, terminal_reason,
):
    conn = _conn()
    monkeypatch.setattr(runner, "real_entry", lambda *args, **kwargs: dict(result))
    monkeypatch.setattr(runner, "_alert_entry_result", lambda *args, **kwargs: None)

    returned = runner.real_entry_hook(
        conn,
        _event(network_id="solana", mint="So1Mint"),
        {},
        now_fn=lambda: NOW,
        trade_size=10.0,
        max_age_s=120,
        trace_clock=FakeClock(),
    )

    assert returned == result
    trace = conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone()
    assert trace["terminal_status"] == terminal_status
    assert trace["terminal_reason"] == terminal_reason
    assert conn.execute("SELECT COUNT(*) FROM memecoin_entry_attempts").fetchone()[0] == 1


def test_trace_finish_failure_does_not_change_funded_result(monkeypatch):
    conn = _conn()
    trace = SimpleNamespace(
        attempt_id="attempt-1",
        mark=Mock(return_value=True),
        finish=Mock(side_effect=sqlite3.OperationalError("trace finish unavailable")),
    )
    result = {"status": "filled", "position_id": 17}
    monkeypatch.setattr(runner, "real_entry", lambda *args, **kwargs: dict(result))
    monkeypatch.setattr(runner, "_alert_entry_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(m, "set_position_identity", lambda *args, **kwargs: None)

    returned = runner.real_entry_hook(
        conn,
        _event(network_id="solana", mint="So1Mint"),
        {},
        now_fn=lambda: NOW,
        trade_size=10.0,
        max_age_s=120,
        trace=trace,
    )

    assert returned == result
    assert trace.finish.call_count == 1


def test_filled_entry_fdv_pin_does_not_strand_its_trace(monkeypatch):
    conn = _conn()
    conn.execute(
        "INSERT INTO memecoin_positions(id,mint,network_id,execution_mode,is_open,"
        "entry_price,opened_at) VALUES(65622,'So1Mint','solana','real',1,0.00002,?)",
        (NOW,),
    )
    conn.commit()
    monkeypatch.setattr(runner, "real_entry", lambda *args, **kwargs: {
        "status": "filled", "position_id": 65622,
    })
    monkeypatch.setattr(runner, "_alert_entry_result", lambda *args, **kwargs: None)

    result = runner.real_entry_hook(
        conn, _event(network_id="solana", mint="So1Mint"), {},
        now_fn=lambda: NOW, trade_size=10.0, max_age_s=120,
        trace_clock=FakeClock(),
        classification_context={
            "effective_mcap": 20_000.0, "assumed_micro": False,
            "market_stats": {"price": 0.00002, "market_cap": 20_000.0,
                             "fdv": 21_000.0, "liquidity": 20_000.0},
        },
    )

    assert result["status"] == "filled"
    assert conn.execute("SELECT fdv_supply FROM memecoin_positions WHERE id=65622").fetchone()[0] == pytest.approx(1_050_000_000)
    assert conn.execute("SELECT terminal_status FROM memecoin_entry_attempts").fetchone()[0] == "filled"
    assert not conn.in_transaction


@pytest.mark.parametrize(
    ("reserve_fn", "network_id", "mint", "gas_kwargs"),
    [
        (reserve_entry, "solana", "So1Mint", {"sol_balance": 1.0}),
        (reserve_entry_evm, "4663", "0xabc", {"native_balance": 1.0}),
    ],
)
def test_atomic_reservations_keep_provenance_insert_inside_one_transaction(
    monkeypatch, reserve_fn, network_id, mint, gas_kwargs,
):
    _arm(monkeypatch)
    conn = _conn()
    sql = []
    conn.set_trace_callback(sql.append)
    kwargs = dict(
        mint=mint,
        source_user_id="u1",
        strategy="alpha_b",
        day="2026-07-15",
        usdc_balance=300.0,
        now_iso=NOW,
        origin_event_id="event-1",
        attempt_id="attempt-1",
        signal_source="global_feed",
        source_price=0.25,
        source_market_cap=1_000_000.0,
        observed_at="2026-07-15T11:59:59.9+00:00",
        clock_domain="observed_real_utc",
        **gas_kwargs,
    )
    if reserve_fn is reserve_entry_evm:
        kwargs["network_id"] = network_id

    decision, pending_id = reserve_fn(conn, **kwargs)

    assert decision.allowed and pending_id is not None
    begin = next(i for i, statement in enumerate(sql) if statement == "BEGIN IMMEDIATE")
    insert = next(
        i for i, statement in enumerate(sql)
        if statement.startswith("INSERT INTO memecoin_pending_entries")
    )
    commit = next(i for i, statement in enumerate(sql[insert:], insert) if statement == "COMMIT")
    assert begin < insert < commit
    assert "COMMIT" not in sql[begin + 1:insert]
    row = m.pending_row(conn, pending_id)
    assert row["origin_event_id"] == "event-1"
    assert row["attempt_id"] == "attempt-1"
    assert row["signal_source"] == "global_feed"
    assert row["source_price"] == pytest.approx(0.25)
    assert row["source_market_cap"] == pytest.approx(1_000_000.0)
    assert row["clock_domain"] == "observed_real_utc"


def test_copy_event_persists_normalized_receipt_and_clock_domain():
    conn = _conn()
    raw = {
        "id": "event-clock",
        "type": "large_buy",
        "userId": "u1",
        "networkId": 4663,
        "tokenAddress": "0xabc",
        "createdAt": "2026-07-15T11:59:59Z",
        "body": {"price": 0.25, "marketCap": 1_000_000, "currentSizeUsd": 5_000},
    }

    event = normalize_feed_item(raw, NOW, None)
    assert event.received_at_normalized == NOW
    assert event.clock_domain == "observed_real_utc"
    assert m.insert_event(conn, event.__dict__)
    row = conn.execute(
        "SELECT received_at, received_at_normalized, clock_domain "
        "FROM memecoin_events WHERE fomo_event_id='event-clock'",
    ).fetchone()
    assert tuple(row) == (NOW, NOW, "observed_real_utc")


def test_manual_buy_reserves_with_funded_contract(monkeypatch):
    """Regression (2026-07-21): manual /real/buy reserved WITHOUT a funded contract
    (classification_context was never passed), so the post-cutover reconcile gate refused
    every armed startup after the next restart — a 26-min funded outage on the 07-22 reboot."""
    _arm(monkeypatch)
    conn = _conn()
    seen = {}
    buy = runner.make_real_buy_fn(
        {"solana": _solana_context(seen)},
        stats_fn=lambda mint, **kw: {"market_cap": 400_000.0, "liquidity": 50_000.0,
                                     "price": 0.001},
    )

    result = buy(conn, "So1Manual", "u-manual", ticker="MAN", network_id="solana")

    assert result["status"] == "unknown"
    pending = seen["pending"]
    assert pending["funded_contract_json"], "manual reservation must carry a funded contract"
    assert pending["funded_contract_hash"]
    assert pending["funded_contract_version"] >= 1
    attempt = conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone()
    assert attempt["funded_contract_json"] == pending["funded_contract_json"]
    assert attempt["funded_contract_hash"] == pending["funded_contract_hash"]

    from sniper.services.memecoin_funded_reconcile import (
        validate_funded_contract_reconciliation,
    )
    errors = validate_funded_contract_reconciliation(
        conn, cutover_at="2026-07-01T00:00:00+00:00")
    assert errors == [], f"startup gate must accept a manual reservation: {errors}"


def test_manual_buy_unresolved_mcap_stamps_assumed_micro_contract(monkeypatch):
    """A manual buy whose mcap resolves nowhere (no DS pair, blind chain quiet) must still
    write a contract via the same assumed-micro convention as the automated path — never a
    contract-less reservation."""
    _arm(monkeypatch)
    conn = _conn()
    seen = {}
    buy = runner.make_real_buy_fn(
        {"solana": _solana_context(seen)},
        stats_fn=lambda mint, **kw: {},
        blind_mcap_fn=lambda chain, mint: None,
    )

    result = buy(conn, "So1Manual2", "u-manual", ticker="MAN2", network_id="solana")

    assert result["status"] == "unknown"
    pending = seen["pending"]
    assert pending["funded_contract_json"]
    import json as _json
    contract = _json.loads(pending["funded_contract_json"])
    assert contract["classification"]["coin_band"] == "assumed_nano"
