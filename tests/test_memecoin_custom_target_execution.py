"""Custom targets must survive the actual signing quote, including its encoded floor."""
import pytest

from sniper.persistence import db_memecoin as m
from test_memecoin_exit_executor import _c
from test_memecoin_solana_exit_now_fn_wired import _build_executor as solana_executor, DECISION_AT
from test_memecoin_evm_exit_now_fn_wired import (
    _build_executor as evm_executor, _open_position as evm_position,
)


@pytest.mark.parametrize("chain", ["solana", "8453"])
@pytest.mark.parametrize("reason", ["real_tp", "mcap_target"])
@pytest.mark.parametrize("case", ["expected_below", "floor_below", "floor_met", "minimum_unknown"])
def test_live_target_checks_exact_proceeds_and_signed_minimum(monkeypatch, chain, reason, case):
    if chain == "solana":
        conn, pos = _c()
        build = solana_executor
        expected_cap, floor_cap = 30_000, 28_000
    else:
        from sniper.execution import evm_wallet as wallet
        from test_memecoin_evm_exit_executor import _c as evm_conn
        monkeypatch.setitem(wallet.CHAIN_QUOTE, "8453",
                            {"token": wallet.CHAIN_USDC["8453"], "decimals": 6, "kind": "usd"})
        conn = evm_conn()
        pos = evm_position(conn)
        build = evm_executor
        expected_cap, floor_cap = 1_500_000, 1_400_000
    conn.execute("UPDATE memecoin_positions SET fdv_supply=1000000 WHERE id=?", (pos["id"],))
    conn.commit()
    pos = m.get_position(conn, pos["id"])
    target = {"expected_below": expected_cap + 1,
              "floor_below": (expected_cap + floor_cap) / 2,
              "floor_met": floor_cap,
              "minimum_unknown": floor_cap}[case]
    override = {"tp_pct": 0, "tp_target_mcap": target} if reason == "real_tp" else None
    monkeypatch.setattr(m, "effective_exit_override", lambda *_: override)
    monkeypatch.setattr(m, "active_exit_target", lambda *_: {"mcap_low": target} if reason == "mcap_target" else None)
    if case == "minimum_unknown":
        if chain == "solana":
            import test_memecoin_solana_exit_now_fn_wired as fixture
            original = fixture._http

            def no_minimum(method, url, **kwargs):
                status, body, error = original(method, url, **kwargs)
                if method == "GET" and "/quote" in url:
                    body.pop("otherAmountThreshold", None)
                return status, body, error
            monkeypatch.setattr(fixture, "_http", no_minimum)
        else:
            import test_memecoin_evm_exit_now_fn_wired as fixture
            original = fixture._quote

            def no_minimum(*args, **kwargs):
                payload = original(*args, **kwargs)
                payload.pop("otherAmountThreshold", None)
                return payload
            monkeypatch.setattr(fixture, "_quote", no_minimum)
    result = build([0.0, 15.0])["exit_fn"](conn, pos, reason, DECISION_AT)
    assert result["status"] == ("closed" if case == "floor_met" else "deferred"), result
    if case != "floor_met":
        fresh = m.get_position(conn, pos["id"])
        assert fresh["is_open"] and fresh["close_sig"] is None and fresh["close_claim_id"] is None
    audit = conn.execute("SELECT allowed, detail FROM memecoin_exit_trigger_audit WHERE lane='execution'").fetchone()
    assert audit is not None and bool(audit["allowed"]) == (case == "floor_met")
    conn.close()


@pytest.mark.parametrize("chain", ["solana", "8453"])
def test_unreadable_current_target_cannot_fall_through_executor_exception_policy(monkeypatch, chain):
    if chain == "solana":
        conn, pos = _c()
        build = solana_executor
    else:
        from sniper.execution import evm_wallet as wallet
        from test_memecoin_evm_exit_executor import _c as evm_conn
        monkeypatch.setitem(wallet.CHAIN_QUOTE, "8453",
                            {"token": wallet.CHAIN_USDC["8453"], "decimals": 6, "kind": "usd"})
        conn = evm_conn()
        pos = evm_position(conn)
        build = evm_executor

    def failed_read(*args):
        raise RuntimeError("override read unavailable")
    monkeypatch.setattr(m, "effective_exit_override", failed_read)
    result = build([0.0, 15.0])["exit_fn"](conn, pos, "real_tp", DECISION_AT)
    assert result["status"] == "deferred", result
    assert m.get_position(conn, pos["id"])["close_sig"] is None
    conn.close()


@pytest.mark.parametrize("reason,custom", [("real_tp", True), ("mcap_target", True), ("real_tp", False)])
def test_route_capacity_fallback_cannot_drop_a_custom_target(monkeypatch, reason, custom):
    from sniper.execution import evm_exit_executor as executor, evm_wallet as wallet
    from test_memecoin_evm_exit_executor import _c as evm_conn
    monkeypatch.setitem(wallet.CHAIN_QUOTE, "8453",
                        {"token": wallet.CHAIN_USDC["8453"], "decimals": 6, "kind": "usd"})
    conn = evm_conn()
    pos = evm_position(conn)
    monkeypatch.setattr(m, "effective_exit_override", lambda *_: {"tp_target_mcap": 10} if custom else None)
    partials = []
    monkeypatch.setattr(executor, "execute_partial_exit_evm",
                        lambda *a, **kw: partials.append(kw) or {"status": "derisked"})
    monkeypatch.setattr(executor, "execute_exit_evm", lambda *a, **kw: kw["partial_exit_fn"](0.1))
    result = evm_executor([0, 15])["exit_fn"](conn, pos, reason, DECISION_AT)
    assert result["status"] == ("deferred" if custom else "derisked")
    assert bool(partials) != custom
    conn.close()


def test_flap_two_leg_estimate_cannot_prove_a_signed_custom_tp_floor(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from sniper.execution import evm_flap_quote_exit as flap, evm_real_seams as seams
    from sniper.settings import memecoin as cfg
    from test_memecoin_evm_exit_executor import _c as evm_conn
    from eth_account import Account
    conn = evm_conn()
    pos = evm_position(conn)
    conn.execute("UPDATE memecoin_positions SET fdv_supply=1000000 WHERE id=?", (pos["id"],))
    conn.commit()
    pos = m.get_position(conn, pos["id"])
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_QUOTE_EXIT", True)
    monkeypatch.setattr(cfg, "memecoin_evm_exit_anchor_chains", lambda: set())
    monkeypatch.setattr(m, "effective_exit_override", lambda *_: {"tp_pct": 0, "tp_target_mcap": 1})
    monkeypatch.setattr(flap, "prewarm", lambda *a: None)
    monkeypatch.setattr(flap, "journal_path", lambda _: tmp_path / "journal")
    monkeypatch.setattr(flap, "Run", lambda *a, **kw: SimpleNamespace(**kw))

    def run(conn, position, reason, now, **kw):
        job = kw["run_factory"](position, tmp_path / "journal")
        allowed, code = job.quote_check_fn(position, 20, 19, 10)
        return {"status": "closed" if allowed else "deferred", "reason": code}
    monkeypatch.setattr(flap, "runner_exit", run)
    ex = seams.build_real_evm_executor(acct=Account.create(), chain_id="56", rpc_url="rpc",
                                       http=lambda *a, **kw: (200, {}, None))
    result = ex["exit_fn"](conn, pos, "real_tp", DECISION_AT)
    assert result == {"status": "deferred", "reason": "execution_target_minimum_unknown"}
    conn.close()


def test_target_floor_uses_the_same_field_precedence_as_the_signed_intent(monkeypatch):
    from sniper.execution import evm_wallet as wallet
    from test_memecoin_evm_exit_executor import _c as evm_conn
    import test_memecoin_evm_exit_now_fn_wired as fixture
    monkeypatch.setitem(wallet.CHAIN_QUOTE, "8453",
                        {"token": wallet.CHAIN_USDC["8453"], "decimals": 6, "kind": "usd"})
    conn = evm_conn()
    pos = evm_position(conn)
    conn.execute("UPDATE memecoin_positions SET fdv_supply=1000000 WHERE id=?", (pos["id"],))
    conn.commit()
    monkeypatch.setattr(m, "effective_exit_override", lambda *_: {"tp_pct": 0, "tp_target_mcap": 1450000})
    original = fixture._quote

    def conflicting_floor(*args, **kwargs):
        return {**original(*args, **kwargs), "minBuyAmount": "14999999"}
    monkeypatch.setattr(fixture, "_quote", conflicting_floor)
    result = evm_executor([0, 15])["exit_fn"](conn, m.get_position(conn, pos["id"]), "real_tp", DECISION_AT)
    assert result == {"status": "deferred", "reason": "execution_target_floor_not_met"}
    conn.close()
