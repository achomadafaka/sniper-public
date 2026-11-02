"""Priority 3 — real-leg wiring (DISARMED): one curated buy on the real path flows
arm-gate -> S1 risk gate + reserve -> token-safety gate -> entry executor, with Tier-0 per-stage
latency captured. Injected seams (token_safety_fn, execute_fn) so it's tested without live HTTP;
the real bindings are attached at arm-time. Rejections/unsafe never reach the executor and free
the reservation."""
import sqlite3
from types import SimpleNamespace
import pytest
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_leg as leg
from sniper.services import memecoin_arming
from sniper.settings import memecoin as cfg


class _Ev:
    def __init__(self, mint="MINT"):
        self.mint = mint; self.source_user_id = "u1"; self.created_at = "t"


def _c(monkeypatch, tmp_path, armed=True):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", armed)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", armed)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c, str(tmp_path / "kill")


def _run(c, kill, ev, *, safe=(True, "ok"), exec_result=None, sol=1.0, usdc=300.0,
         impact_cap_pct=None, chase_max_pct=None):
    calls = {"exec": 0}
    def execute_fn(conn, pid, mint, size, now_iso, impact_cap_pct=None, chase_max_pct=None):
        calls["exec"] += 1
        calls["impact_cap_pct"] = impact_cap_pct
        calls["chase_max_pct"] = chase_max_pct
        return exec_result or {"status": "filled", "sig": "SIG1", "position_id": pid}
    r = leg.real_entry(c, ev, day="2026-07-09", usdc_balance=usdc, sol_balance=sol,
                       now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
                       token_safety_fn=lambda mint, **kw: safe, execute_fn=execute_fn, kill_path=kill,
                       impact_cap_pct=impact_cap_pct, chase_max_pct=chase_max_pct)
    return r, calls


def test_impact_cap_forwarded_to_executor(monkeypatch, tmp_path):
    # the mcap-tiered cap resolved upstream must reach the Solana executor unchanged
    c, kill = _c(monkeypatch, tmp_path)
    r, calls = _run(c, kill, _Ev(), impact_cap_pct=12.0)
    assert r["status"] == "filled" and calls["impact_cap_pct"] == 12.0


def test_chase_cap_forwarded_to_compatible_executor(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    result, calls = _run(c, kill, _Ev(), chase_max_pct=30.0)

    assert result["status"] == "filled"
    assert calls["chase_max_pct"] == 30.0


def _leg_mint_evidence(monkeypatch, tmp_path, *, bad=None, decimals=6, token2022=False):
    c, kill = _c(monkeypatch, tmp_path)
    ev = _Ev()
    trace = SimpleNamespace(attempt_id="mint-attempt", mark=lambda *a: None,
                            mark_substage=lambda *a, **k: None)
    read = ({"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {
        "program": "spl-token", "parsed": {"type": "mint", "info": {
            "decimals": decimals, "supply": "1000000", "mintAuthority": None,
            "freezeAuthority": None, "isInitialized": True}}}}, None)
    if token2022:
        from sniper.execution.solana_token_safety import TOKEN_2022_PROGRAM
        read[0]["owner"] = TOKEN_2022_PROGRAM
        read[0]["data"]["program"] = "spl-token-2022"
    ev.network_id = "solana"
    ev.mint_account_read = read
    ev.entry_prereads = SimpleNamespace(trace=trace, chain="solana", mint=ev.mint, _mint=read)
    if bad == "trace": ev.entry_prereads.trace = object()
    if bad == "mint": ev.entry_prereads.mint = "another"
    if bad == "chain": ev.entry_prereads.chain = "56"
    if bad == "raw_tuple": ev.mint_account_read = (read[0], None)
    if bad == "failed": ev.mint_account_read = ev.entry_prereads._mint = (read[0], "down")
    if bad == "program": read[0]["data"]["program"] = "other"
    if bad == "account_type": read[0]["data"]["parsed"]["type"] = "account"
    if bad == "owner": read[0]["owner"] = "other"
    if bad == "uninitialized": read[0]["data"]["parsed"]["info"]["isInitialized"] = False
    if bad == "initialization_unknown": read[0]["data"]["parsed"]["info"].pop("isInitialized")
    if bad == "attempt": trace.attempt_id = None
    if bad == "disabled": monkeypatch.setattr(cfg, "MEMECOIN_REAL_TOKEN_SAFETY_ENABLED", False)
    got = {}
    def execute(conn, pid, mint, size, now_iso, **kwargs):
        got.update(kwargs)
        return {"status": "filled", "sig": "SIG1", "position_id": pid}
    with c:
        result = leg.real_entry(c, ev, day="2026-07-09", usdc_balance=300., sol_balance=1.,
            now_iso="2026-07-09T00:00:00Z", trade_size=10., trace=trace,
            token_safety_fn=lambda mint, **kw: (True, "ok"), execute_fn=execute, kill_path=kill)
    c.close()
    assert result["status"] == "filled"
    return got.get("mint_read_evidence")


@pytest.mark.parametrize("decimals", [0, 6, 18])
def test_leg_hands_successful_same_attempt_mint_evidence(monkeypatch, tmp_path, decimals):
    assert _leg_mint_evidence(monkeypatch, tmp_path, decimals=decimals) == {
        "network_id": "solana", "mint": "MINT", "attempt_id": "mint-attempt",
        "decimals": decimals}


def test_leg_reuses_initialized_token2022_mint_read(monkeypatch, tmp_path):
    assert _leg_mint_evidence(monkeypatch, tmp_path, token2022=True) == {
        "network_id": "solana", "mint": "MINT", "attempt_id": "mint-attempt", "decimals": 6}


@pytest.mark.parametrize("bad", ["trace", "mint", "chain", "raw_tuple", "failed", "program",
                                    "account_type", "owner", "attempt", "disabled",
                                    "uninitialized", "initialization_unknown"])
def test_leg_never_hands_unbound_or_unusable_mint_read(monkeypatch, tmp_path, bad):
    assert _leg_mint_evidence(monkeypatch, tmp_path, bad=bad) is None


@pytest.mark.parametrize("decimals", [None, True, 6., "6", -1, 19])
def test_leg_keeps_unknown_mint_decimals_unknown(monkeypatch, tmp_path, decimals):
    assert _leg_mint_evidence(monkeypatch, tmp_path, decimals=decimals) is None


def test_reservation_refusal_carries_blocker_into_trace(monkeypatch, tmp_path):
    import json
    import time
    from sniper.services.memecoin_entry_trace import EntryAttemptTrace
    from sniper.services.memecoin_real_runner import _finish_trace_for_result
    from sniper.services import memecoin_real_runner as runner
    c, kill = _c(monkeypatch, tmp_path)
    pending_id = m.reserve_pending(c, mint="MINT", source_user_id="other", strategy="control",
                                   notional=10., reserved_at="t", attempt_id="other-attempt")
    ev = _Ev()
    trace = EntryAttemptTrace.start(c, ev, "2026-09-26T00:00:00Z", time)
    result, calls = _run(c, kill, ev)
    assert calls["exec"] == 0
    monkeypatch.setattr(runner, "_record_entry_shadows_best_effort", lambda *a: None)
    monkeypatch.setattr(runner, "_record_radar_best_effort", lambda *a: None)
    monkeypatch.setattr(runner, "_notify_entry_projection", lambda *a: None)
    assert _finish_trace_for_result(trace, result)
    row = c.execute("SELECT gate_inputs_json FROM memecoin_entry_attempts WHERE attempt_id=?",
                    (trace.attempt_id,)).fetchone()
    assert json.loads(row[0])["duplicate_blocker"] == {
        "kind": "reservation", "reservation_id": pending_id, "attempt_id": "other-attempt"}


def test_disarmed_returns_without_reserving(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path, armed=False)
    r, calls = _run(c, kill, _Ev())
    assert r["status"] == "disarmed" and calls["exec"] == 0
    assert m.pending_totals(c) == (0, 0.0)


def test_kill_switch_blocks(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path, armed=True)
    memecoin_arming.trip_kill_switch(kill)
    r, calls = _run(c, kill, _Ev())
    assert r["status"] == "disarmed" and calls["exec"] == 0


def test_unsafe_token_releases_reservation_no_exec(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    r, calls = _run(c, kill, _Ev(), safe=(False, "live freeze authority"))
    assert r["status"] == "unsafe" and calls["exec"] == 0
    assert m.pending_totals(c) == (0, 0.0)         # reservation freed


def test_happy_path_reaches_executor_and_records_latency(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    r, calls = _run(c, kill, _Ev())
    assert r["status"] == "filled" and calls["exec"] == 1
    assert "timing" in r and r["timing"]["total_ms"] >= 0
    row = c.execute("SELECT mint, status, total_ms FROM memecoin_exec_latency").fetchone()
    assert row["mint"] == "MINT" and row["status"] == "filled" and row["total_ms"] is not None


def test_exec_latency_uses_executor_legacy_fallback_source_price(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    result = {
        "status": "filled", "sig": "SIG1", "position_id": 9,
        "source_price": 1.25, "price_attribution": "legacy_source_chain",
        "quote_price": 1.5, "fill_price": 1.75,
        "source_drift_pct": 20.0, "execution_slippage_pct": 16.6666666667,
        "total_copy_drift_pct": 40.0,
    }
    _run(c, kill, _Ev(), exec_result=result)
    row = dict(c.execute("SELECT * FROM memecoin_exec_latency").fetchone())
    assert row["source_price"] == 1.25
    assert row["price_attribution"] == "legacy_source_chain"
    assert row["source_drift_pct"] == 20.0
    assert row["total_copy_drift_pct"] == 40.0


def test_s1_gate_rejection_short_circuits(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    # SOL below the full-book exit reserve -> S1 rejects before token-safety/executor
    r, calls = _run(c, kill, _Ev(), sol=0.0001)
    assert r["status"] == "rejected" and calls["exec"] == 0
    assert m.pending_totals(c) == (0, 0.0)


def test_a_failed_solana_sign_says_why(caplog):
    """Five funded entries have died as the bare words "sign failed" with no recoverable reason
    (2026-08-14, 08-24, 08-30 x2, 09-04) because the signer swallowed its exception. Fail-closed is
    correct; silent is not."""
    import logging
    from sniper.execution.solana_real_seams import sign_swap_tx

    with caplog.at_level(logging.ERROR):
        assert sign_swap_tx("not-valid-base64!!", object()) is None
    assert any("sign_swap_tx failed" in r.message for r in caplog.records), \
        "the cause must reach the log, not just a None"
