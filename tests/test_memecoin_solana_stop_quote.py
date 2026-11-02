import pytest

from sniper.execution import solana_exit_executor as executor
from sniper.persistence import db_memecoin as m
from test_memecoin_exit_executor import _c, _Deps
from test_memecoin_solana_exit_now_fn_wired import _build_executor, DECISION_AT


def test_live_solana_stop_refuses_recovered_execution_quote(monkeypatch):
    conn, pos = _c()
    monkeypatch.setattr(m, "effective_exit_override", lambda *_: {"sl_pct": -35})
    m.set_position_stop_hits(conn, pos["id"], 2)
    m.set_position_stop_breach_started(conn, pos["id"], DECISION_AT)
    result = _build_executor([0.0, 15.0])["exit_fn"](conn, pos, "b_stop", DECISION_AT)
    assert result["status"] == "deferred"
    row = m.get_position(conn, pos["id"])
    assert row["is_open"] and row["close_sig"] is None and row["close_claim_id"] is None
    assert row["stop_hits"] == 0 and row["stop_breach_started_at"] is None
    audit = conn.execute("SELECT allowed,detail FROM memecoin_exit_trigger_audit").fetchone()
    assert not audit["allowed"] and '"execution_quote": true' in audit["detail"]
    conn.close()


@pytest.mark.parametrize("retry,broken", [(False, False), (True, False)])
def test_stop_quote_check_precedes_each_solana_signature(retry, broken):
    conn, pos = _c()
    deps = _Deps(["failed"] if retry else [])
    checks, signs = [], []

    def check(fresh, quote, qty, slip):
        checks.append(qty)
        if broken:
            raise ValueError("invalid output")
        return retry and len(checks) == 1, {"code": "executable_threshold_not_met"}

    def sign(quote):
        signs.append(quote)
        return b"tx", "sig"

    result = executor.execute_exit(
        conn, pos, "b_stop", DECISION_AT, quote_fn=deps.quote_fn, sign_fn=sign,
        send_fn=deps.send_fn, confirm_fn=deps.confirm_fn, usdc_fn=deps.usdc_fn,
        token_balance_fn=deps.token_fn, sleep_fn=lambda _: None,
        slippage_ladder=[3, 10], quote_check_fn=check,
    )
    assert result["status"] == "deferred"
    assert len(signs) == int(retry) and checks == [500.0] * (1 + int(retry))
    row = m.get_position(conn, pos["id"])
    assert row["is_open"] and row["close_sig"] is None and row["close_claim_id"] is None
    conn.close()


def test_a_crashed_stop_check_does_not_hold_a_confirmed_stop():
    """2026-09-11 (Gamma D2): a check that cannot read its quote is "unknown", not "hold". The sweep
    already confirmed the stop on an exact quote, so the exit proceeds to sign."""
    conn, pos = _c()
    deps = _Deps(["confirmed"])
    signs = []

    def check(fresh, quote, qty, slip):
        raise ValueError("invalid output")

    def sign(quote):
        signs.append(quote)
        return b"tx", "sig"

    result = executor.execute_exit(
        conn, pos, "b_stop", DECISION_AT, quote_fn=deps.quote_fn, sign_fn=sign,
        send_fn=deps.send_fn, confirm_fn=deps.confirm_fn, usdc_fn=deps.usdc_fn,
        token_balance_fn=deps.token_fn, sleep_fn=lambda _: None,
        slippage_ladder=[3, 10], quote_check_fn=check,
    )
    assert result["status"] != "deferred"
    assert len(signs) == 1
    conn.close()


def test_a_confirmed_stop_signs_the_quote_exactly_as_a_non_stop_exit_would(monkeypatch):
    """The stop check reads the execution quote. It must not change the object that is signed: an
    added key reaches the Jupiter swap build. Same route and balances, two exits: a mirror exit runs
    no check, a stop runs it and passes. The signer must receive identical quotes."""
    from solders.keypair import Keypair
    from _memecoin_signed_tx import signed_tx
    from sniper.execution.solana_real_seams import build_real_solana_executor
    from test_memecoin_solana_exit_now_fn_wired import _StubTransport, _http

    jupiter = {"priceImpactPct": "0.001", "outAmount": "5000000",   # 500 tokens for $5:
               "inAmount": "500000000", "otherAmountThreshold": "4600000"}  # -50% on a $0.02 entry

    def cheap_http(method, url, headers=None, body=None, timeout=10):
        if method == "GET" and "/quote" in url:
            return 200, dict(jupiter), None
        return _http(method, url, headers=headers, body=body, timeout=timeout)

    monkeypatch.setattr(m, "effective_exit_override", lambda *_: {"sl_pct": -35})
    tx = signed_tx()
    signed = {}
    for reason in ("mirror_exit", "b_stop"):
        conn, pos = _c()
        seen = []
        usdc = iter([0.0, 5.0])

        def builder(quote, _seen=seen):
            _seen.append(dict(quote))
            return tx.signed_b64, tx.signature, {"blockhash": tx.blockhash,
                                                 "last_valid_height": tx.last_valid_height}

        ex = build_real_solana_executor(
            keypair=Keypair(), rpc_url="rpc", http=cheap_http, usdc_fn=lambda: next(usdc),
            token_fn=lambda mint: 500.0, submission_transport=_StubTransport(),
            fast_tx_builder=builder, backend_label="auto_free")
        result = ex["exit_fn"](conn, pos, reason, DECISION_AT)
        assert result["status"] == "closed", (reason, result)
        assert seen, f"{reason}: nothing reached the signer"
        signed[reason] = seen[0]
        if reason == "b_stop":                         # the stop went THROUGH the check, not around it
            audit = conn.execute("SELECT allowed, detail FROM memecoin_exit_trigger_audit "
                                 "WHERE lane='execution'").fetchone()
            assert audit is not None and audit["allowed"] == 1
            assert '"execution_quote": true' in audit["detail"]
        conn.close()
    assert signed["b_stop"] == signed["mirror_exit"]
    assert signed["b_stop"] == jupiter, "a key was added to the quote on its way to the signer"
