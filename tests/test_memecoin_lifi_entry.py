import sqlite3
from types import SimpleNamespace

import pytest

from sniper.execution.evm_swap_executor import execute_entry_evm
from sniper.execution.lifi_executor import execute_lifi_entry
from sniper.execution.lifi_route import DecodedQuote, GateResult, QuotaGuard
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_evm_leg import evm_real_entry


WETH = "0x1111111111111111111111111111111111111111"
TOKEN = "0x2222222222222222222222222222222222222222"
SPENDER = "0x3333333333333333333333333333333333333333"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _pending(conn):
    return m.reserve_pending(
        conn, mint=TOKEN, source_user_id="wallet", strategy="control", notional=10,
        reserved_at="2026-08-30T20:00:00+00:00", network_id="4663",
    )


def _abort(conn, pending_id, code, lifi):
    return execute_entry_evm(
        conn,
        pending_id=pending_id,
        mint=TOKEN,
        network_id="4663",
        usd_amount=10,
        strategy="control",
        source_user_id="wallet",
        now_iso="2026-08-30T20:00:00+00:00",
        quote_fn=lambda *_args: None,
        quote_miss_fn=lambda: {"code": code, "detail": code, "rungs": []},
        lifi_entry_fn=lifi,
        approve_fn=lambda *_args: True,
        sign_fn=lambda *_args: ("0xraw", "0xhash", 1),
        send_fn=lambda *_args: True,
        confirm_fn=lambda *_args: "confirmed",
        usdc_fn=lambda: 10,
        token_fn=lambda _mint: (0, 18),
    )


@pytest.mark.parametrize(
    "code",
    ["provider_rate_limit", "provider_timeout", "provider_rpc_transport", "quote_unattributed"],
)
def test_only_exact_direct_no_route_can_call_lifi(code):
    conn = _conn()
    calls = []
    result = _abort(conn, _pending(conn), code, lambda miss: calls.append(miss))

    assert result["status"] == "aborted"
    assert calls == []


def test_exact_direct_no_route_calls_lifi_once_and_keeps_attribution_separate():
    conn = _conn()
    calls = []

    def lifi(miss):
        calls.append(miss)
        return {"status": "aborted", "reason": "lifi_no_route", "lifi_failure_type": "no_route"}

    result = _abort(conn, _pending(conn), "no_route", lifi)

    assert len(calls) == 1
    assert result["direct_quote_miss_code"] == "no_route"
    assert result["lifi_failure_type"] == "no_route"


def test_a_valid_direct_quote_never_calls_lifi_even_after_a_later_refusal():
    conn = _conn()
    pending_id = _pending(conn)
    calls = []
    quote = {
        "sellAmount": "1000",
        "transaction": {"to": SPENDER, "data": "0xdead"},
        "issues": {"allowance": {"spender": SPENDER}},
    }

    result = execute_entry_evm(
        conn, pending_id=pending_id, mint=TOKEN, network_id="4663", usd_amount=10,
        strategy="control", source_user_id="wallet", now_iso="2026-08-30T20:00:00+00:00",
        quote_fn=lambda *_args: quote, approve_fn=lambda *_args: False,
        sign_fn=lambda *_args: None, send_fn=lambda *_args: None,
        confirm_fn=lambda *_args: "unknown", usdc_fn=lambda: 10,
        token_fn=lambda _mint: (0, 18), lifi_entry_fn=lambda miss: calls.append(miss),
    )

    assert result["reason"] == "approve failed"
    assert calls == []


def _decoded(input_token, output_token, input_atomic, output_min, suffix):
    return DecodedQuote(
        calldata_sha256=suffix * 64,
        decoded_semantics_sha256=suffix * 64,
        fee_atomic=input_atomic * 25 // 10_000,
        input_atomic=input_atomic,
        output_min_atomic=output_min,
        path=(input_token, input_token, output_token),
        request_sha256=suffix * 64,
        response_sha256=suffix.upper() * 64,
        spender=SPENDER,
        swap_input_atomic=input_atomic - input_atomic * 25 // 10_000,
        target=SPENDER,
        tool="nordstern",
    )


class Client:
    def __init__(self, decoded):
        self.decoded = iter(decoded)
        self.calls = []

    def quote(self, request, role, stage):
        self.calls.append((request, role, stage))
        decoded = next(self.decoded)
        return SimpleNamespace(
            ok=True, decoded=decoded, request_sha256=decoded.request_sha256,
            response_sha256=decoded.response_sha256, received_at="now",
            quota={"limit": 12000, "remaining": 11999, "reset": 7200},
            failure_type=None,
        )


def test_four_request_sequence_proofs_continuity_gates_and_execution():
    quotes = [
        _decoded(WETH, TOKEN, 1000, 950, "a"),
        _decoded(TOKEN, WETH, 950, 920, "b"),
        _decoded(WETH, TOKEN, 1000, 960, "c"),
        _decoded(TOKEN, WETH, 960, 930, "d"),
    ]
    client = Client(quotes)
    events = []
    gate_names = []

    def request_fn(side, amount, stage):
        events.append("request:" + stage)
        return {"side": side, "fromAmount": str(amount), "stage": stage}

    def proof(result, stage):
        events.append("proof:" + stage)
        return {"stage": stage, "hash": result.response_sha256}

    def approve(token, spender, amount):
        events.append("approve")
        assert (token, spender, amount) == (WETH, SPENDER, 1000)
        return SimpleNamespace(status="confirmed")

    def gate(name, buy, sell):
        gate_names.append((name, buy, sell))
        return GateResult(True, "ok")

    result = execute_lifi_entry(
        direct_miss={"code": "no_route"},
        input_atomic=1000,
        request_fn=request_fn,
        client=client,
        proof_fn=proof,
        approval_fn=approve,
        execute_fn=lambda buy, sell, proofs: {
            "status": "filled", "buy": buy, "sell": sell, "proofs": proofs,
        },
        cleanup_fn=lambda reason: events.append("cleanup:" + reason),
        quota_guard=QuotaGuard(remaining=7210),
        slots_used=0,
        in_flight=0,
        gate_fn=gate,
        roundtrip_cap_bps=1000,
        trace_fn=lambda stage, data: events.append("trace:" + stage),
    )

    assert result["status"] == "filled"
    assert len(client.calls) == 4
    assert [stage for _request, _role, stage in client.calls] == [
        "initial_buy", "initial_sell", "refreshed_buy", "final_sell",
    ]
    assert client.calls[1][0]["fromAmount"] == "950"
    assert client.calls[3][0]["fromAmount"] == "960"
    assert [name for name, _buy, _sell in gate_names] == [
        "token_safety", "impact", "chase", "execution_premium", "post_peak",
        "gas_sufficiency",
    ] * 2
    assert all(buy is quotes[0] and sell is quotes[1] for _name, buy, sell in gate_names[:6])
    assert all(buy is quotes[2] and sell is quotes[3] for _name, buy, sell in gate_names[6:])
    assert events.index("approve") > events.index("proof:initial_sell")
    assert events.index("request:refreshed_buy") > events.index("approve")


def test_canary_uses_the_seven_request_quota_boundary():
    quotes = [
        _decoded(WETH, TOKEN, 1000, 950, "a"),
        _decoded(TOKEN, WETH, 950, 920, "b"),
        _decoded(WETH, TOKEN, 1000, 960, "c"),
        _decoded(TOKEN, WETH, 960, 930, "d"),
    ]
    client = Client(quotes)

    result = execute_lifi_entry(
        direct_miss={"code": "no_route"}, input_atomic=1000,
        request_fn=lambda side, amount, stage: {
            "side": side, "fromAmount": str(amount), "stage": stage,
        },
        client=client, proof_fn=lambda result, stage: (result, stage),
        approval_fn=lambda *_args: SimpleNamespace(status="confirmed"),
        execute_fn=lambda *_args: {"status": "filled"}, cleanup_fn=lambda _reason: None,
        quota_guard=QuotaGuard(remaining=7), slots_used=0, in_flight=0,
        gate_fn=lambda *_args: GateResult(True, "ok"), roundtrip_cap_bps=1000,
        canary=True,
    )

    assert result == {"status": "filled"}


def test_post_approval_refusal_starts_cleanup_and_never_requests_a_fifth_quote():
    quotes = [
        _decoded(WETH, TOKEN, 1000, 950, "a"),
        _decoded(TOKEN, WETH, 950, 920, "b"),
        _decoded(WETH, TOKEN, 1000, 960, "c"),
        _decoded(TOKEN, WETH, 960, 700, "d"),
    ]
    client = Client(quotes)
    cleanup = []

    result = execute_lifi_entry(
        direct_miss={"code": "no_route"}, input_atomic=1000,
        request_fn=lambda side, amount, stage: {
            "side": side, "fromAmount": str(amount), "stage": stage,
        },
        client=client, proof_fn=lambda result, stage: stage,
        approval_fn=lambda *_args: SimpleNamespace(status="confirmed"),
        execute_fn=lambda *_args: pytest.fail("execution must not start"),
        cleanup_fn=cleanup.append, quota_guard=QuotaGuard(remaining=7210),
        slots_used=0, in_flight=0, gate_fn=lambda *_args: GateResult(True, "ok"),
        roundtrip_cap_bps=1000,
    )

    assert result["status"] == "refused"
    assert result["reason"] == "roundtrip_cap"
    assert cleanup == ["roundtrip_cap"]
    assert len(client.calls) == 4


def test_quota_refusal_and_early_quote_failure_use_zero_or_one_request():
    client = Client([])
    refused = execute_lifi_entry(
        direct_miss={"code": "no_route"}, input_atomic=1000,
        request_fn=lambda *_args: {}, client=client, proof_fn=lambda *_args: None,
        approval_fn=lambda *_args: None, execute_fn=lambda *_args: None,
        cleanup_fn=lambda *_args: None, quota_guard=QuotaGuard(remaining=7209),
        slots_used=0, in_flight=0, gate_fn=lambda *_args: True, roundtrip_cap_bps=1000,
    )
    assert refused["reason"] == "insufficient_quota"
    assert client.calls == []

    class FailedClient:
        def __init__(self):
            self.calls = 0

        def quote(self, *_args, **_kwargs):
            self.calls += 1
            return SimpleNamespace(
                ok=False, failure_type="rate_limited", quota={}, received_at="now",
            )

    failed = FailedClient()
    result = execute_lifi_entry(
        direct_miss={"code": "no_route"}, input_atomic=1000,
        request_fn=lambda *_args: {}, client=failed, proof_fn=lambda *_args: None,
        approval_fn=lambda *_args: None, execute_fn=lambda *_args: None,
        cleanup_fn=lambda *_args: None, quota_guard=QuotaGuard(remaining=7210),
        slots_used=0, in_flight=0, gate_fn=lambda *_args: True, roundtrip_cap_bps=1000,
    )
    assert result["lifi_failure_type"] == "rate_limited"
    assert failed.calls == 1


def test_initial_buy_quota_drop_aborts_before_approval_or_later_quote():
    guard = QuotaGuard(remaining=7210)
    quotes = iter([
        _decoded(WETH, TOKEN, 1000, 950, "a"),
        _decoded(TOKEN, WETH, 950, 920, "b"),
        _decoded(WETH, TOKEN, 1000, 960, "c"),
        _decoded(TOKEN, WETH, 960, 930, "d"),
    ])

    class QuotaClient:
        def __init__(self):
            self.calls = []

        def quote(self, request, role, stage):
            self.calls.append((request, role, stage))
            decoded = next(quotes)
            if len(self.calls) == 1:
                guard.observe_response({
                    "limit": 12000,
                    "remaining": 7208,
                    "reset": 7200,
                })
            return SimpleNamespace(
                ok=True, decoded=decoded, request_sha256=decoded.request_sha256,
                response_sha256=decoded.response_sha256, received_at="now",
                quota={"limit": 12000, "remaining": 7208, "reset": 7200},
                failure_type=None,
            )

    client = QuotaClient()
    approvals = []
    executions = []
    result = execute_lifi_entry(
        direct_miss={"code": "no_route"}, input_atomic=1000,
        request_fn=lambda side, amount, stage: {
            "side": side, "fromAmount": str(amount), "stage": stage,
        },
        client=client, proof_fn=lambda result, stage: (result, stage),
        approval_fn=lambda *_args: (
            approvals.append(True) or SimpleNamespace(status="confirmed")
        ),
        execute_fn=lambda *_args: executions.append(True) or {"status": "filled"},
        cleanup_fn=lambda _reason: None, quota_guard=guard,
        slots_used=0, in_flight=0,
        gate_fn=lambda *_args: GateResult(True, "ok"), roundtrip_cap_bps=1000,
    )

    assert result["status"] == "aborted"
    assert len(client.calls) == 1
    assert approvals == []
    assert executions == []


def test_direct_route_safety_reaches_the_executor(monkeypatch):
    from sniper.services import memecoin_arming
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    conn = _conn()
    calls = []
    event = SimpleNamespace(mint=TOKEN, source_user_id="wallet", network_id="4663")

    def execute(_conn, _pending_id, _mint, _size, _now, *, safety_gate_fn):
        calls.append(safety_gate_fn)
        return {"status": "aborted", "reason": "lifi_no_route"}

    result = evm_real_entry(
        conn, event, route="evm:4663", day="2026-08-30", usdc_balance=100,
        native_balance=1, now_iso="2026-08-30T20:00:00+00:00", trade_size=10,
        token_safety_fn=lambda *_args, **_kwargs: (
            False, "configured sell venue returned no executable quote"
        ),
        execute_fn=execute,
    )

    assert result["reason"] == "lifi_no_route"
    assert len(calls) == 1


class _FailingClient:
    """A client whose quote comes back not-ok with a given failure type."""
    def __init__(self, failure_type):
        self.failure_type = failure_type

    def quote(self, request, role, stage):
        return SimpleNamespace(
            ok=False, decoded=None, request_sha256="r", response_sha256="s",
            received_at="now", quota={"limit": 12000, "remaining": 11999, "reset": 7200},
            failure_type=self.failure_type,
        )


def _abort_reason(failure_type):
    return execute_lifi_entry(
        direct_miss={"code": "no_route"}, input_atomic=1000,
        request_fn=lambda side, amount, stage: {"side": side},
        client=_FailingClient(failure_type),
        proof_fn=lambda result, stage: {},
        approval_fn=lambda *_a: SimpleNamespace(status="confirmed"),
        execute_fn=lambda *_a, **_k: {"status": "filled"},
        cleanup_fn=lambda reason: None,
        quota_guard=QuotaGuard(remaining=7210), slots_used=0, in_flight=0,
        gate_fn=lambda name, buy, sell: GateResult(True, "ok"),
        roundtrip_cap_bps=1000,
    )["reason"]


def test_no_route_names_the_outcome_not_the_messenger():
    """LI.FI is asked ONLY after the direct ladder already returned no_route, so `no_route` here
    means every venue agreed. Reporting that as "lifi_no_route" made an ordinary market condition
    read as a LI.FI fault, and the operator concluded LI.FI was blocking the system on 2026-09-04
    while it was answering normally and entries were still filling."""
    assert _abort_reason("no_route") == "no_route_all_venues"


@pytest.mark.parametrize("failure_type", ["rate_limited", "provider_error", "unauthorized",
                                          "internal_error"])
def test_a_real_lifi_fault_still_carries_its_name(failure_type):
    """Everything that IS LI.FI's fault must stay attributable to LI.FI."""
    assert _abort_reason(failure_type) == "lifi_" + failure_type
