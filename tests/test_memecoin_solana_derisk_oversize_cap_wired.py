"""Codex audit #4 (35948d32..d7ef0f2c): `execute_partial_exit`'s oversize-retry sets `cap_accounts`
and calls `_quote(quote_fn, ..., cap_accounts)`, which tries `quote_fn(mint, tokens, slip,
max_accounts=cap_accounts)` first. The real `derisk_fn`'s `_partial_quote` (solana_real_seams.py)
took only `(mint, tokens_human, slip)` -- no `max_accounts` parameter -- so that call raised
TypeError, `_quote` fell back to the uncapped call, and `jupiter_exit_quote` never saw the cap.
The retry existed to stop the SAME oversized-account-count payload from being signed again; without
the cap reaching the quote, the retry rung asks for an identically oversized route.

This drives the real `derisk_fn` returned by `build_real_solana_executor` (not `_partial_quote` or
`execute_partial_exit` with a hand-made callback), because a replacement quote_fn that already
accepts `max_accounts` would pass even with the production seam broken.
"""
import base64
import json
import sqlite3

from solders.keypair import Keypair

from _memecoin_signed_tx import signed_tx as _fast_tx
from sniper.execution.solana_real_seams import build_real_solana_executor
from sniper.persistence import db_memecoin as m

DECISION_AT = "2026-09-13T12:00:00+00:00"
CAP = 48


def _open_position(conn):
    pid = m.open_position(
        conn, source_user_id="u1", source_handle="(real)", mint="MintX", ticker="",
        entry_price=0.02, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=-1.0,
        their_size_usd=0.0, opened_at=DECISION_AT, strategy="alpha_b", execution_mode="real",
        network_id="solana")
    m.insert_fill(conn, position_id=pid, kind="entry", price=0.02, qty=500.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at=DECISION_AT)
    return m.get_position(conn, pid)


def _make_http(quote_urls):
    def _http(method, url, headers=None, body=None, timeout=10):
        if method == "POST":
            meth = json.loads(body).get("method")
            if meth == "getTokenAccountsByOwner":
                # the full identity #532 requires (tests/test_solana_token_balance_truth.py account())
                owner, mint = json.loads(body)["params"][0], json.loads(body)["params"][1]["mint"]
                return 200, {"result": {"value": [{"pubkey": "acct", "account": {
                    "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {"parsed": {
                        "type": "account", "info": {"owner": owner, "mint": mint, "state": "initialized",
                                                    "tokenAmount": {"amount": "500000000", "decimals": 6}}}}}}]}}, None
            if meth == "getSignatureStatuses":
                return 200, {"result": {"value": [{"confirmationStatus": "confirmed",
                                                   "err": None}]}}, None
            return 200, {}, None
        if method == "GET" and "/quote" in url:
            quote_urls.append(url)
            return 200, {"priceImpactPct": "0.01", "outAmount": "15000000",
                         "inAmount": "500000000", "otherAmountThreshold": "14000000"}, None
        return 200, {}, None
    return _http


def _make_fast_tx_builder(oversized_calls):
    """Signs OVERSIZED on the first call (payload > 1232 bytes decoded), normal after."""
    tx = _fast_tx()
    calls = {"n": 0}

    def builder(quote):
        calls["n"] += 1
        if calls["n"] <= oversized_calls:
            oversized_b64 = base64.b64encode(b"\x00" * 1300).decode()
            return oversized_b64, f"oversized-sig-{calls['n']}", {}
        return tx.signed_b64, tx.signature, {"blockhash": tx.blockhash,
                                             "last_valid_height": tx.last_valid_height}
    return builder


def test_the_real_derisk_fn_forwards_the_oversize_cap_to_the_quote(monkeypatch):
    monkeypatch.setattr(
        "sniper.settings.memecoin.MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS", CAP)

    class _StubTransport:
        def submit(self, request):
            from sniper.execution.submission_transport import (
                AttemptState, SubmissionAttempt, SubmissionResult)
            return SubmissionResult(
                AttemptState.ACCEPTED, request.tx_identity,
                (SubmissionAttempt("solana:custody_rpc", AttemptState.ACCEPTED,
                                   request.tx_identity, 1.0, "200", None),),
                request.tx_identity, request.signed_payload_sha256)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    position = _open_position(conn)

    quote_urls = []
    ex = build_real_solana_executor(
        keypair=Keypair(), rpc_url="rpc", http=_make_http(quote_urls),
        usdc_fn=lambda: 0.0, token_fn=lambda mint: 500.0,
        submission_transport=_StubTransport(),
        fast_tx_builder=_make_fast_tx_builder(oversized_calls=1),
        backend_label="auto_free")

    result = ex["derisk_fn"](conn, position, 0.25, DECISION_AT)

    assert result["status"] == "derisked", result
    assert len(quote_urls) >= 2, "need a quote before AND after the oversize retry"
    assert "maxAccounts=%d" % CAP not in quote_urls[0], (
        "the first (oversized) quote must not already carry the cap")
    assert "maxAccounts=%d" % CAP in quote_urls[1], (
        "the retry rung's quote must forward the cap to jupiter_exit_quote — "
        "derisk_fn's _partial_quote(mint, tokens_human, slip) drops it on a TypeError fallback")
