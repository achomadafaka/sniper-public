"""Task 9 — crash safety of the shared fast submission on the real executor paths.

The transport lives inside the send_fn seam, so the executor's persist-before-broadcast and
submitted-unknown guarantees must survive it: an ACCEPTED-or-AMBIGUOUS aggregate that never reaches
chain confirmation stays submitted-unknown (position open / reservation held, never released or
double-submitted); only an all-provider EXPLICIT_REJECTED takes the existing safe reset path.
"""
import sqlite3

import pytest

from sniper.execution.solana_exit_executor import execute_exit
from sniper.execution.solana_swap_executor import execute_entry
from sniper.execution.submission_transport import (
    AttemptState, SubmissionAttempt, SubmissionResult,
)
from sniper.execution.submission_wiring import make_solana_send_fn
from sniper.persistence import db_memecoin as m
from _memecoin_signed_tx import signed_tx as _fast_tx


class StubTransport:
    def __init__(self, state):
        self.state = state
        self.calls = 0

    def submit(self, request):
        self.calls += 1
        return SubmissionResult(
            self.state, request.tx_identity,
            (SubmissionAttempt("solana:custody_rpc", self.state, request.tx_identity, 1.0, "200",
                               None),),
            request.tx_identity if self.state is AttemptState.ACCEPTED else None,
            request.signed_payload_sha256)


def _signed3(tx):
    return (tx.signed_b64, tx.signature,
            {"blockhash": tx.blockhash, "last_valid_height": tx.last_valid_height})


def _conn(tmp_path, name="t.db"):
    c = sqlite3.connect(str(tmp_path / name))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open(c, mint="MintX"):
    return m.open_position(
        c, source_user_id="u1", source_handle="h", mint=mint, ticker="X",
        entry_price=0.0004, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=-1.0,
        their_size_usd=0.0, opened_at="2026-07-25T00:00:00+00:00", strategy="alpha_b",
        execution_mode="real", network_id="solana")


_EXIT_QUOTE = {"otherAmountThreshold": "990000", "inAmount": "1000000", "routePlan": []}


def _run_exit(c, pid, transport, confirm):
    tx = _fast_tx()
    pos = m.get_position(c, pid)
    send_fn = make_solana_send_fn(
        transport, conn=c, action="exit", owner_kind="close_claim", owner_id=pid,
        now_fn=lambda: "2026-07-25T00:00:03Z")
    return execute_exit(
        c, pos, "cut", "2026-07-25T00:00:03Z",
        quote_fn=lambda mint, tokens, slip: dict(_EXIT_QUOTE),
        sign_fn=lambda quote: _signed3(tx),
        send_fn=send_fn, confirm_fn=lambda sig: confirm,
        usdc_fn=lambda: 0.0, token_balance_fn=lambda mint: 1000.0,
        slippage_ladder=[0.03])


def test_fast_exit_ambiguous_stays_open_submitted_unknown(tmp_path):
    c = _conn(tmp_path)
    pid = _open(c)
    result = _run_exit(c, pid, StubTransport(AttemptState.AMBIGUOUS), confirm="unknown")
    assert result["status"] == "unknown"
    assert m.get_position(c, pid)["is_open"] == 1               # never closed on ambiguity
    intent = c.execute("SELECT status FROM memecoin_exit_intents WHERE position_id=?",
                       (pid,)).fetchone()
    assert intent["status"] == "submitted_unknown"
    claim_id = m.get_position(c, pid)["close_claim_id"]
    audit = c.execute(
        "SELECT owner_kind, owner_id FROM memecoin_submission_attempts WHERE action='exit'"
    ).fetchone()
    assert audit["owner_kind"] == "close_claim" and audit["owner_id"] == claim_id


def test_fast_exit_accepted_but_unconfirmed_stays_open(tmp_path):
    c = _conn(tmp_path)
    pid = _open(c)
    # provider ACCEPTED, but chain confirmation never arrives (crash-after-acceptance) -> unknown
    result = _run_exit(c, pid, StubTransport(AttemptState.ACCEPTED), confirm="unknown")
    assert result["status"] == "unknown"
    assert m.get_position(c, pid)["is_open"] == 1


def test_fast_exit_all_rejected_never_double_books(tmp_path):
    c = _conn(tmp_path)
    pid = _open(c)
    # every provider explicitly rejected on the only rung -> ladder exhausted -> stuck bag, still open,
    # never a fabricated close. (release/re-sign is the safe path when ALL providers reject.)
    result = _run_exit(c, pid, StubTransport(AttemptState.EXPLICIT_REJECTED), confirm="unknown")
    assert result["status"] == "stuck"
    row = m.get_position(c, pid)
    assert row["is_open"] == 1 and row["closed_at"] is None


def test_fast_entry_accepted_unconfirmed_keeps_reservation_and_binding(tmp_path):
    c = _conn(tmp_path)
    pid = m.reserve_pending(c, mint="MintX", source_user_id="u1", strategy="alpha_b",
                            notional=50.0, reserved_at="2026-07-25T00:00:00+00:00",
                            network_id="solana")
    tx = _fast_tx()
    transport = StubTransport(AttemptState.ACCEPTED)
    send_fn = make_solana_send_fn(
        transport, conn=c, action="entry", owner_kind="reservation", owner_id=pid,
        now_fn=lambda: "2026-07-25T00:00:03Z", backend_label="auto_free", persist=True)
    result = execute_entry(
        c, pending_id=pid, mint="MintX", usd_amount=50.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-07-25T00:00:03Z",
        quote_fn=lambda mint, usd: {"priceImpactPct": "0.001", "outAmount": "1000000",
                                    "inAmount": "50000000", "otherAmountThreshold": "990000"},
        reverse_quote_fn=lambda mint, amount: {
            # price-neutral: 1,000,000 token atomic are the $50 clip (50 USDC atomic each).
            "inAmount": str(amount), "outAmount": str(int(amount) * 50),
            "otherAmountThreshold": str(int(amount) * 50)},
        sign_fn=lambda quote: (tx.signed_b64, tx.signature),
        send_fn=send_fn, confirm_fn=lambda sig: "unknown",
        usdc_fn=lambda: 100.0, token_fn=lambda mint: 0.0)
    assert result["status"] == "unknown"
    # reservation held (never released), and the durable binding was persisted before broadcast
    assert m.pending_row(c, pid)["status"] == "reserved"
    assert m.entry_submission_binding(c, pid)["signed_payload_b64"] == tx.signed_b64
    assert transport.calls == 1


class _CrashProvider:
    """A provider that raises mid-submit — a crash after the bytes may have crossed the wire."""
    provider_id = "solana:custody_rpc"

    def submit(self, request):
        raise RuntimeError("crash after write")


class _NotSentProvider:
    def __init__(self, provider_id):
        self.provider_id = provider_id

    def submit(self, request):
        return SubmissionAttempt(self.provider_id, AttemptState.NOT_SENT, None, 0.0, None,
                                 "unavailable")


def test_crash_after_provider_write_keeps_position_open_submitted_unknown(tmp_path):
    from sniper.execution.submission_transport import SubmissionTransport
    c = _conn(tmp_path)
    pid = _open(c)
    transport = SubmissionTransport([_CrashProvider()], deadline_s=1.0)   # crash -> AMBIGUOUS
    try:
        result = _run_exit(c, pid, transport, confirm="unknown")
    finally:
        transport.close()
    assert result["status"] == "unknown"
    assert m.get_position(c, pid)["is_open"] == 1                        # never closed on a crash


def test_all_not_sent_keeps_position_open_never_fabricates_rejection(tmp_path):
    from sniper.execution.submission_transport import SubmissionTransport
    c = _conn(tmp_path)
    pid = _open(c)
    # every real provider proved nothing was sent (sidecar down + public quarantined) -> AMBIGUOUS,
    # never an EXPLICIT_REJECTED that would fabricate a "safe" release.
    transport = SubmissionTransport(
        [_NotSentProvider("solana:public_rpc"), _NotSentProvider("solana:tpu")], deadline_s=1.0)
    try:
        result = _run_exit(c, pid, transport, confirm="unknown")
    finally:
        transport.close()
    assert result["status"] == "unknown"
    assert m.get_position(c, pid)["is_open"] == 1


def test_audit_flush_failure_does_not_change_the_seam_outcome(tmp_path):
    # a record_submission_attempts failure must be swallowed — the accepted identity still returns.
    c = _conn(tmp_path)
    pid = m.reserve_pending(c, mint="MintX", source_user_id="u1", strategy="alpha_b",
                            notional=50.0, reserved_at="2026-07-25T00:00:00+00:00",
                            network_id="solana")
    tx = _fast_tx()
    transport = StubTransport(AttemptState.ACCEPTED)
    send_fn = make_solana_send_fn(
        transport, conn=c, action="entry", owner_kind="reservation", owner_id=pid,
        now_fn=lambda: "2026-07-25T00:00:03Z", backend_label="auto_free", persist=True)
    import sniper.persistence.db_memecoin as _m
    orig = _m.record_submission_attempts
    _m.record_submission_attempts = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit down"))
    try:
        assert send_fn(tx.signed_b64, validity={"blockhash": tx.blockhash,
                                                "last_valid_height": tx.last_valid_height}) == tx.signature
    finally:
        _m.record_submission_attempts = orig
    assert m.entry_submission_binding(c, pid)["signed_payload_b64"] == tx.signed_b64
    assert transport.calls == 1


def test_restart_conflicting_entry_never_reclaims_prior_signed_owner(tmp_path):
    # a restart re-drives entry with DIFFERENT signed bytes: the immutable bind CAS refuses, ZERO
    # providers see the new bytes, and the seam is submitted_unknown (None) — never a re-sign/reclaim.
    c = _conn(tmp_path)
    pid = m.reserve_pending(c, mint="MintX", source_user_id="u1", strategy="alpha_b",
                            notional=50.0, reserved_at="2026-07-25T00:00:00+00:00",
                            network_id="solana")
    first, second = _fast_tx(), _fast_tx()
    transport = StubTransport(AttemptState.ACCEPTED)
    send_fn = make_solana_send_fn(
        transport, conn=c, action="entry", owner_kind="reservation", owner_id=pid,
        now_fn=lambda: "2026-07-25T00:00:03Z", backend_label="auto_free", persist=True)
    assert send_fn(first.signed_b64, validity={"blockhash": first.blockhash,
                                               "last_valid_height": first.last_valid_height}) == first.signature
    assert send_fn(second.signed_b64, validity={"blockhash": second.blockhash,
                                                "last_valid_height": second.last_valid_height}) is None
    assert transport.calls == 1                                          # the second never broadcast
    assert m.entry_submission_binding(c, pid)["tx_sig"] == first.signature


def test_fast_entry_all_explicitly_rejected_releases_reservation(tmp_path):
    c = _conn(tmp_path)
    pid = m.reserve_pending(c, mint="MintX", source_user_id="u1", strategy="alpha_b",
                            notional=50.0, reserved_at="2026-07-25T00:00:00+00:00",
                            network_id="solana")
    tx = _fast_tx()
    transport = StubTransport(AttemptState.EXPLICIT_REJECTED)
    send_fn = make_solana_send_fn(
        transport, conn=c, action="entry", owner_kind="reservation", owner_id=pid,
        now_fn=lambda: "2026-07-25T00:00:03Z", backend_label="pooled_rpc", persist=True)
    result = execute_entry(
        c, pending_id=pid, mint="MintX", usd_amount=50.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-07-25T00:00:03Z",
        quote_fn=lambda mint, usd: {"priceImpactPct": "0.001", "outAmount": "1000000",
                                    "inAmount": "50000000", "otherAmountThreshold": "990000"},
        reverse_quote_fn=lambda mint, amount: {
            # price-neutral: 1,000,000 token atomic are the $50 clip (50 USDC atomic each).
            "inAmount": str(amount), "outAmount": str(int(amount) * 50),
            "otherAmountThreshold": str(int(amount) * 50)},
        sign_fn=lambda quote: _signed3(tx), send_fn=send_fn,
        confirm_fn=lambda sig: (_ for _ in ()).throw(AssertionError("must not confirm rejected tx")),
        usdc_fn=lambda: 100.0, token_fn=lambda mint: 0.0)
    assert result["status"] == "failed" and result["reason"] == "broadcast_rejected"
    assert m.pending_row(c, pid)["status"] == "failed"
