"""Task 7 — EVM factory integration: the shared transport is wired into every EVM send seam.

Proves entry(swap+pre-tx), exit, partial-exit, and approvals all route through the SAME injected
transport, with approvals fanning out too. Offline (StubTransport + real eth_account signing + a
minimal fake RPC).
"""
import pytest
import json
import sqlite3

from eth_account import Account

from sniper.execution import submission_wiring as W
from sniper.execution.evm_real_seams import build_real_evm_executor
from sniper.execution.submission_transport import (
    AttemptState, SubmissionAttempt, SubmissionResult,
)
from sniper.persistence import db_memecoin as m


@pytest.fixture(autouse=True)
def _route_cost_is_not_what_this_file_tests():
    """Every hit measures cheap, so these fixtures pin what they are actually about.

    The entry ladder measures a round trip by quoting the reverse of the bag a hit would buy
    (2026-09-06). A fixture with no `sellAmount`, or one answering the same amount in both
    directions, cannot be measured and the hit is correctly REFUSED as `round_trip_unmeasured`.
    Nothing in this file is about the cost of a route, so without this each test would be asserting
    on a refusal it was never written to describe. The tests that ARE about measurement live in
    tests/test_memecoin_evm_measured_ladder.py.
    """
    import sniper.execution.evm_real_seams as _S
    original = _S._round_trip_for_hit
    _S._round_trip_for_hit = lambda *_a, **_k: (-3.0, None)
    try:
        yield
    finally:
        _S._round_trip_for_hit = original


class StubTransport:
    def __init__(self, state=AttemptState.ACCEPTED):
        self.state = state
        self.calls = 0

    def submit(self, request):
        self.calls += 1
        return SubmissionResult(
            self.state, request.tx_identity,
            (SubmissionAttempt("8453:custody_rpc", self.state, request.tx_identity, 1.0, "200", None),),
            request.tx_identity if self.state is AttemptState.ACCEPTED else None,
            request.signed_payload_sha256)


def test_evm_send_seams_share_one_transport(monkeypatch):
    captured = {}
    real = W.make_evm_send_fn

    def spy(transport, **kw):
        captured[kw["action"]] = {"transport": transport, "owner_kind": kw["owner_kind"]}
        return real(transport, **kw)
    monkeypatch.setattr("sniper.execution.submission_wiring.make_evm_send_fn", spy)

    transport = StubTransport()
    # 8453 quotes in WETH since 2026-09-06, so sizing reads an ETH/USD mark. Without one the entry
    # aborts BEFORE the wiring this test is about, and `captured` stays empty.
    monkeypatch.setattr("sniper.execution.evm_real_seams.quote_usd_price",
                        lambda *_a, **_k: 2500.0)

    def http(method, url, headers=None, body=None, timeout=10):
        return 500, {"error": "abort"}, None            # entry aborts after wiring

    ex = build_real_evm_executor(
        acct=Account.create(), chain_id="8453", rpc_url="rpc", http=http,
        submission_transport=transport, backend_label="auto_free")

    class _Pending:
        def __getitem__(self, k):
            return {"strategy": "real", "source_user_id": "u"}[k]
    # execute_fn needs a pending row; patch pending_row to a stub so we reach the wiring
    monkeypatch.setattr("sniper.persistence.db_memecoin.pending_row",
                        lambda conn, pid: {"strategy": "real", "source_user_id": "u",
                                           "network_id": "8453"})
    monkeypatch.setattr("sniper.persistence.db_memecoin.validated_pending_funded_contract",
                        lambda conn, pending: {})
    monkeypatch.setattr("sniper.persistence.db_memecoin.release_pending",
                        lambda conn, pid, reason: None)
    monkeypatch.setattr("sniper.persistence.db_memecoin.get_position", lambda conn, pid: None)

    ex["execute_fn"](None, 1, "0xabc", 50.0, "2026-07-25T00:00:01Z")
    ex["exit_fn"](None, {"id": 9, "mint": "0xabc", "entry_notional": 10.0}, "cut",
                  "2026-07-25T00:00:02Z")
    ex["derisk_fn"](None, {"id": 9, "mint": "0xabc", "entry_notional": 10.0}, 0.5,
                    "2026-07-25T00:00:02Z")

    assert {"entry", "exit", "partial_exit"} <= set(captured)
    txs = {v["transport"] for v in captured.values()}
    assert txs == {transport}                           # one shared object across all EVM seams
    assert captured["entry"]["owner_kind"] == "reservation"
    assert captured["exit"]["owner_kind"] == "close_claim"
    assert captured["partial_exit"]["owner_kind"] == "derisk_claim"


def test_evm_approval_fans_out_through_transport():
    transport = StubTransport(AttemptState.ACCEPTED)
    acct = Account.create()

    def http(method, url, headers=None, body=None, timeout=10):
        meth = json.loads(body).get("method") if body else None
        if meth == "eth_getTransactionCount":
            return 200, {"result": "0x1"}, None
        if meth == "eth_gasPrice":
            return 200, {"result": "0x3b9aca00"}, None
        if meth == "eth_getTransactionReceipt":
            return 200, {"result": {"status": "0x1"}}, None
        return 200, {"result": None}, None

    ex = build_real_evm_executor(
        acct=acct, chain_id="8453", rpc_url="rpc", http=http,
        submission_transport=transport, backend_label="auto_free")
    # approve a spender for an exact amount — the raw approval tx must fan out through the transport
    sent = ex["_approve"]("0x000000000000000000000000000000000000dEaD", 12345)
    assert transport.calls == 1                          # approval broadcast via the shared transport
    assert sent                                          # truthy (dict when pipelined, True otherwise)


def test_pipelined_entry_approval_attempt_is_keyed_to_reservation(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.reserve_pending(
        conn, mint="0x" + "22" * 20, source_user_id="u", strategy="real",
        notional=10.0, reserved_at="2026-07-28T00:00:00Z", network_id="4663",
    )
    transport = StubTransport(AttemptState.ACCEPTED)
    spender = "0x" + "44" * 20

    def quote(_sell, _buy, amount, *, slippage_bps=None):
        return {
            "sellAmount": str(amount), "buyAmount": str(10**18),
            "issues": {"allowance": {"spender": spender}},
            "transaction": {"to": spender, "data": "0x", "value": "0",
                            "gas": "500000", "gasPrice": "1000000000"},
        }

    def http(_method, _url, headers=None, body=None, timeout=10):
        method = json.loads(body)["method"]
        if method == "eth_getTransactionCount":
            return 200, {"result": "0x5"}, None
        if method == "eth_gasPrice":
            return 200, {"result": "0x3b9aca00"}, None
        if method == "eth_getTransactionReceipt":
            return 200, {"result": {"status": "0x1"}}, None
        return 200, {"result": "0x0"}, None

    from evm_receipt_fake import wrap
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    cash = iter((100.0, 90.0))
    tokens = iter((0.0, 1.0))
    acct = Account.create()
    ex = build_real_evm_executor(
        acct=acct, chain_id="4663", rpc_url="rpc",
        http=wrap(http, owner=acct.address, mint="0x" + "22" * 20,
                  quote=CHAIN_QUOTE["4663"]["token"], coin=1, paid=5 * 10 ** 15),
        venue_quote_fn=quote, usdc_fn=lambda: next(cash), token_fn=lambda _mint: next(tokens),
        price_fn=lambda: 2000.0, submission_transport=transport, backend_label="auto_free",
    )
    result = ex["execute_fn"](
        conn, pid, "0x" + "22" * 20, 10.0, "2026-07-28T00:00:01Z",
    )
    assert result["status"] == "filled"
    row = conn.execute(
        "SELECT chain_id, action, owner_kind, owner_id FROM memecoin_submission_attempts "
        "WHERE action='entry_approval'"
    ).fetchone()
    assert dict(row) == {"chain_id": "4663", "action": "entry_approval",
                         "owner_kind": "reservation", "owner_id": str(pid)}
