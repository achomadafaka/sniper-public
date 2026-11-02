"""Finishes 9ff18f3d (2026-09-09, Alpha review): `execute_exit` grew a `now_fn` parameter but the
one LIVE call site -- `exit_fn` inside `build_real_solana_executor`, `solana_real_seams.py` --
never passed it, so close_submitted_at and closed_at still collapsed to the frozen decision-time
`now_iso` in production. This drives the real seam exactly as the live caller does
(`ex["exit_fn"](conn, position, exit_reason, now_iso)`, see
`sniper.services.memecoin_real_runner.make_venue_exit_fn`), not `execute_exit` with hand-made
kwargs, so it fails if the wiring at the call site regresses even though the executor's own unit
tests (tests/test_solana_exit_executor_timestamps.py) still pass.
"""
import json
import sqlite3

from solders.keypair import Keypair

from _memecoin_signed_tx import signed_tx as _fast_tx
from sniper.execution.solana_real_seams import build_real_solana_executor
from sniper.persistence import db_memecoin as m

DECISION_AT = "2026-09-09T12:00:00+00:00"


class _StubTransport:
    def __init__(self):
        self.calls = 0

    def submit(self, request):
        from sniper.execution.submission_transport import AttemptState, SubmissionAttempt, SubmissionResult
        self.calls += 1
        return SubmissionResult(
            AttemptState.ACCEPTED, request.tx_identity,
            (SubmissionAttempt("solana:custody_rpc", AttemptState.ACCEPTED, request.tx_identity,
                               1.0, "200", None),),
            request.tx_identity, request.signed_payload_sha256)


def _open_position(conn):
    pid = m.open_position(
        conn, source_user_id="u1", source_handle="(real)", mint="MintX", ticker="",
        entry_price=0.02, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=-1.0,
        their_size_usd=0.0, opened_at=DECISION_AT, strategy="alpha_b", execution_mode="real",
        network_id="solana")
    m.insert_fill(conn, position_id=pid, kind="entry", price=0.02, qty=500.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at=DECISION_AT)
    return m.get_position(conn, pid)


def _http(method, url, headers=None, body=None, timeout=10):
    if method == "POST":
        request = json.loads(body)
        meth = request.get("method")
        if meth == "getTokenAccountsByOwner":
            owner, mint = request["params"][0], request["params"][1]["mint"]
            return 200, {"result": {"value": [{"pubkey": "token-account", "account": {
                "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {"parsed": {
                "type": "account", "info": {"owner": owner, "mint": mint,
                "tokenAmount": {"amount": "500000000", "decimals": 6}}}}}}]}}, None
        if meth == "getSignatureStatuses":
            return 200, {"result": {"value": [{"confirmationStatus": "confirmed",
                                               "err": None}]}}, None
        return 200, {}, None
    if method == "GET" and "/quote" in url:
        return 200, {"priceImpactPct": "0.001", "outAmount": "15000000",
                     "inAmount": "500000000", "otherAmountThreshold": "14000000"}, None
    return 200, {}, None


def _build_executor(usdc_values):
    tx = _fast_tx()
    usdc = iter(usdc_values)
    return build_real_solana_executor(
        keypair=Keypair(), rpc_url="rpc", http=_http, usdc_fn=lambda: next(usdc),
        token_fn=lambda mint: 500.0, submission_transport=_StubTransport(),
        fast_tx_builder=lambda quote: (tx.signed_b64, tx.signature,
                                       {"blockhash": tx.blockhash,
                                        "last_valid_height": tx.last_valid_height}),
        backend_label="auto_free")


def test_the_live_seam_stamps_three_independent_instants_not_one_frozen_value():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    position = _open_position(conn)
    ex = _build_executor([0.0, 15.0])

    result = ex["exit_fn"](conn, position, "mirror_exit", DECISION_AT)

    assert result["status"] == "closed"
    p = m.get_position(conn, position["id"])
    assert p["close_submitted_at"] != DECISION_AT, "still stamping the frozen decision instant"
    assert p["closed_at"] != DECISION_AT, "still stamping the frozen decision instant"
    assert p["close_submitted_at"] <= p["closed_at"], "submit must not be recorded after close"


def test_the_live_seam_still_closes_when_the_real_clock_raises(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    position = _open_position(conn)
    # Built BEFORE the patch: the executor's OWN submission-audit clock (unrelated to this
    # change, bound once at build time in `_send_fn_for`) keeps working. Only `exit_fn`'s
    # close_submitted_at/closed_at clock is re-imported fresh on every call and sees the raise --
    # that is the wiring this test targets, not the pre-existing audit timestamp.
    ex = _build_executor([0.0, 15.0])

    def _raising_clock():
        raise RuntimeError("clock down")
    monkeypatch.setattr("sniper.util.clock.real_utc_now_iso", _raising_clock)

    result = ex["exit_fn"](conn, position, "mirror_exit", DECISION_AT)

    assert result["status"] == "closed", "a raising clock must never fail a real sell"
    p = m.get_position(conn, position["id"])
    assert p["close_submitted_at"] == DECISION_AT
    assert p["closed_at"] == DECISION_AT
