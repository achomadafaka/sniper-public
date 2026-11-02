"""Task 6/9 — Solana factory integration: the shared transport is wired into every send seam.

Proves (a) entry, full-exit, and partial-exit all build their send adapter from the SAME injected
transport object with the right owner/action/persist, and (b) a full fast entry drives the transport,
persists the durable binding, records the attempt audit, and books the position — all offline.
"""
import json
import sqlite3

import pytest
from solders.keypair import Keypair

from sniper.execution import submission_wiring as W
from sniper.execution.solana_real_seams import build_real_solana_executor
from sniper.execution.submission_transport import (
    AttemptState, SubmissionAttempt, SubmissionResult,
)
from sniper.persistence import db_memecoin as m
from _memecoin_signed_tx import signed_tx as _fast_tx


class StubTransport:
    def __init__(self, state=AttemptState.ACCEPTED):
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


class Bal(float):
    """A balance that answers both the exit's numeric compare and the entry's snapshot parts."""

    def __new__(cls, human, atomic, decimals):
        obj = super().__new__(cls, human)
        obj.atomic = atomic
        obj.decimals = decimals
        obj.human = human
        return obj


def _conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _reserve(c):
    # Transport-only fixtures use manual authority; automatic copies require slot bounds.
    c.execute("INSERT INTO memecoin_pending_entries (mint, source_user_id, strategy, "
              "reserved_notional_usd, reserved_at, status, network_id, signal_source) VALUES "
              "(?,?,?,?,?, 'reserved', 'solana', 'manual')", ("MintX", "u1", "real", 50.0,
                                                     "2026-07-25T00:00:00Z"))
    c.commit()
    return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def test_entry_exit_derisk_share_one_transport_with_correct_wiring(monkeypatch, tmp_path):
    captured = {}
    real = W.make_solana_send_fn

    def spy(transport, **kw):
        captured[kw["action"]] = {"transport": transport, "owner_kind": kw["owner_kind"],
                                  "persist": kw["persist"]}
        return real(transport, **kw)
    monkeypatch.setattr("sniper.execution.submission_wiring.make_solana_send_fn", spy)

    c = _conn(tmp_path)
    pid = _reserve(c)
    transport = StubTransport()

    def http(method, url, headers=None, body=None, timeout=10):
        return 500, {"error": "abort quote"}, None      # force a clean early abort after wiring

    ex = build_real_solana_executor(
        keypair=Keypair(), rpc_url="rpc", http=http, usdc_fn=lambda: 100.0,
        token_fn=lambda mint: Bal(0, 0, 6), submission_transport=transport,
        fast_tx_builder=lambda quote: None, backend_label="auto_free")

    ex["execute_fn"](c, pid, "MintX", 50.0, "2026-07-25T00:00:01Z")
    ex["exit_fn"](c, {"id": 999, "mint": "MintX", "entry_notional": 10.0}, "cut",
                  "2026-07-25T00:00:02Z")
    ex["derisk_fn"](c, {"id": 999, "mint": "MintX", "entry_notional": 10.0}, 0.5,
                    "2026-07-25T00:00:02Z")

    assert set(captured) == {"entry", "exit", "partial_exit"}
    # one shared transport object across all three seams (invariant: entry+exit use the same object)
    assert (captured["entry"]["transport"] is captured["exit"]["transport"]
            is captured["partial_exit"]["transport"] is transport)
    assert captured["entry"]["persist"] is True and captured["entry"]["owner_kind"] == "reservation"
    assert captured["exit"]["owner_kind"] == "close_claim" and captured["exit"]["persist"] is False
    assert captured["partial_exit"]["owner_kind"] == "derisk_claim"


def test_full_fast_entry_uses_transport_persists_binding_and_books(tmp_path):
    c = _conn(tmp_path)
    pid = _reserve(c)
    transport = StubTransport(AttemptState.ACCEPTED)
    tx = _fast_tx()
    usdc = iter([100.0, 90.0, 90.0, 90.0])
    tok = iter([Bal(0, 0, 6), Bal(1.0, 1_000_000, 6), Bal(1.0, 1_000_000, 6)])

    def http(method, url, headers=None, body=None, timeout=10):
        if method == "GET" and "amount=990000" in url:
            # Price-neutral: 990,000 token atomic are all but 1% of the $50 clip, so the sell
            # returns ~$49.50. It read $0.90 while it only had to prove a route EXISTS, and the
            # entry round-trip cap reads this same quote now.
            return 200, {"inAmount": "990000", "outAmount": "49500000",
                         "otherAmountThreshold": "49000000"}, None
        if method == "GET" and "/quote" in url:
            return 200, {"priceImpactPct": "0.001", "outAmount": "1000000",
                         "inAmount": "50000000", "otherAmountThreshold": "990000"}, None
        if method == "POST":
            meth = json.loads(body).get("method")
            if meth == "getSignatureStatuses":
                return 200, {"result": {"value": [{"confirmationStatus": "confirmed",
                                                   "err": None}]}}, None
        return 200, {}, None

    ex = build_real_solana_executor(
        keypair=Keypair(), rpc_url="rpc", http=http, usdc_fn=lambda: next(usdc),
        token_fn=lambda mint: next(tok), submission_transport=transport,
        fast_tx_builder=lambda quote: (tx.signed_b64, tx.signature,
                                       {"blockhash": tx.blockhash,
                                        "last_valid_height": tx.last_valid_height}),
        backend_label="auto_free")

    result = ex["execute_fn"](c, pid, "MintX", 50.0, "2026-07-25T00:00:01Z")
    assert result["status"] == "filled"
    assert transport.calls == 1                          # the shared transport carried the entry
    binding = m.entry_submission_binding(c, pid)
    assert binding["signed_payload_b64"] == tx.signed_b64
    assert binding["submission_blockhash"] == tx.blockhash
    assert binding["submission_last_valid_height"] == tx.last_valid_height
    assert binding["submission_backend"] == "auto_free"
    audit = list(c.execute("SELECT state, owner_id FROM memecoin_submission_attempts"))
    assert audit and audit[0]["state"] == "accepted" and audit[0]["owner_id"] == str(pid)


def test_conflicting_second_fast_entry_never_broadcasts_or_overwrites_binding(tmp_path):
    c = _conn(tmp_path)
    pid = _reserve(c)
    first_tx = _fast_tx()
    transport = StubTransport(AttemptState.ACCEPTED)
    send_fn = W.make_solana_send_fn(
        transport, conn=c, action="entry", owner_kind="reservation", owner_id=pid,
        now_fn=lambda: "2026-07-25T00:00:01Z", backend_label="pooled_rpc", persist=True)
    assert send_fn(first_tx.signed_b64, validity={
        "blockhash": first_tx.blockhash,
        "last_valid_height": first_tx.last_valid_height,
    }) == first_tx.signature
    assert transport.calls == 1

    second_tx = _fast_tx()
    assert send_fn(second_tx.signed_b64, validity={
        "blockhash": second_tx.blockhash,
        "last_valid_height": second_tx.last_valid_height,
    }) is None
    assert transport.calls == 1
    binding = m.entry_submission_binding(c, pid)
    assert binding["tx_sig"] == first_tx.signature
    assert binding["signed_payload_b64"] == first_tx.signed_b64


class RecordingTransport:
    """Captures every request and what the ledger held at dispatch; answers a positive refusal so
    the executor stops after the send (no confirm, no receipt)."""

    def __init__(self, conn=None, pending_id=None):
        self.requests, self.bound_at_dispatch = [], []
        self._conn, self._pid = conn, pending_id

    def submit(self, request):
        self.requests.append(request)
        if self._conn is not None:
            self.bound_at_dispatch.append(
                (m.entry_submission_binding(self._conn, self._pid) or {}).get("tx_sig"))
        return SubmissionResult(
            AttemptState.EXPLICIT_REJECTED, request.tx_identity,
            (SubmissionAttempt("solana:custody_rpc", AttemptState.EXPLICIT_REJECTED, None, 1.0,
                               "200", "test_refusal"),),
            None, request.signed_payload_sha256)


def _slot_message(keypair):
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.system_program import TransferParams, transfer
    ix = transfer(TransferParams(from_pubkey=keypair.pubkey(), to_pubkey=Keypair().pubkey(),
                                 lamports=1))
    return MessageV0.try_compile(keypair.pubkey(), [ix], [], Hash.default())


def test_slot_lane_request_skips_preflight_and_audits_async_after_the_bind(tmp_path):
    # RR-05 (2026-09-26): the lane's custody leg preflighted at the finalized bank and answered
    # "blockhash not found" on every lane send, and its caller-thread audit waited 2,331 ms for
    # the ledger lock. The slot request must carry skip_preflight, audit off the caller thread,
    # and still bind its identity BEFORE any provider sees the bytes.
    c = _conn(tmp_path)
    pid = _reserve(c)
    kp = Keypair()
    transport = RecordingTransport(c, pid)
    ex = build_real_solana_executor(
        keypair=kp, rpc_url="rpc", http=lambda *a, **k: (500, {}, None), usdc_fn=lambda: 100.0,
        token_fn=lambda mint: Bal(0, 0, 6), submission_transport=transport,
        fast_tx_builder=lambda quote: None, backend_label="auto_free")
    result = ex["slot_execute_fn"](c, m.pending_row(c, pid), _slot_message(kp),
                                   now_iso="2026-09-26T22:40:00Z")
    assert result["status"] == "failed"
    [req] = transport.requests
    assert req.skip_preflight is True
    assert transport.bound_at_dispatch == [req.tx_identity]      # identity bound before dispatch
    db = str(tmp_path / "t.db")
    assert W.flush_async_audit(5.0)
    assert W.async_audit_stats()[db]["written"] == 1              # went through the writer
    assert [r["reason_code"] for r in c.execute(
        "SELECT reason_code FROM memecoin_submission_attempts")] == ["test_refusal"]


def test_ordinary_entry_after_a_slot_send_keeps_preflight_and_inline_audit(tmp_path):
    # Competing lanes on ONE executor and ONE transport: the slot send's flags must not leak into
    # the J7 entry that follows (per-request flags, no shared mutable state).
    c = _conn(tmp_path)
    pid = _reserve(c)
    c.execute("INSERT INTO memecoin_pending_entries (mint, source_user_id, strategy, "
              "reserved_notional_usd, reserved_at, status, network_id, signal_source) VALUES "
              "('MintS', 'u2', 'real', 50.0, '2026-07-25T00:00:00Z', 'reserved', 'solana', 'manual')")
    c.commit()
    slot_pid = c.execute("SELECT id FROM memecoin_pending_entries WHERE mint='MintS'").fetchone()[0]
    tx = _fast_tx()
    kp = Keypair()
    transport = RecordingTransport()

    def http(method, url, headers=None, body=None, timeout=10):
        if method == "GET" and "amount=990000" in url:
            return 200, {"inAmount": "990000", "outAmount": "49500000",
                         "otherAmountThreshold": "49000000"}, None
        if method == "GET" and "/quote" in url:
            return 200, {"priceImpactPct": "0.001", "outAmount": "1000000",
                         "inAmount": "50000000", "otherAmountThreshold": "990000"}, None
        return 200, {}, None

    ex = build_real_solana_executor(
        keypair=kp, rpc_url="rpc", http=http, usdc_fn=lambda: 100.0,
        token_fn=lambda mint: Bal(0, 0, 6), submission_transport=transport,
        fast_tx_builder=lambda quote: (tx.signed_b64, tx.signature,
                                       {"blockhash": tx.blockhash,
                                        "last_valid_height": tx.last_valid_height}),
        backend_label="auto_free")
    ex["slot_execute_fn"](c, m.pending_row(c, slot_pid), _slot_message(kp),
                          now_iso="2026-09-26T22:40:00Z")
    ex["execute_fn"](c, pid, "MintX", 50.0, "2026-07-25T00:00:01Z")
    assert [r.skip_preflight for r in transport.requests] == [True, False]
    # inline: the J7 row exists the moment execute_fn returns, with no flush
    assert c.execute("SELECT COUNT(*) FROM memecoin_submission_attempts WHERE owner_id=?",
                     (str(pid),)).fetchone()[0] == 1
    assert W.flush_async_audit(5.0)
