"""Native 4663 sends use the existing same-bytes transport after durable binding."""
import sqlite3
import threading
import time

import pytest

from sniper.execution.submission_transport import (
    AttemptState, SubmissionAttempt, SubmissionTransport, sha256_hex,
)
from sniper.execution.submission_wiring import evm_tx_identity, flush_async_audit
from sniper.persistence import db_memecoin as db
from sniper.services import memecoin_rh_fast_lane as lane
from test_memecoin_rh_fast_live import ACCT, MINT, Sender, _ledger, _reserved


def _signed(chain=4663, nonce=42):
    # Fixed offline test account; no funded key or provider is consulted.
    signed = ACCT.sign_transaction({"chainId": chain, "nonce": nonce, "gasPrice": 40_000_000,
                                    "gas": 21000, "to": MINT, "value": 10 ** 16, "data": b""})
    raw = "0x" + bytes(signed.raw_transaction).hex()
    return raw, evm_tx_identity(raw)


class Trace:
    def __init__(self):
        self.marks = []

    def mark_substage(self, name, **fields):
        self.marks.append((name, fields))


class Provider:
    def __init__(self, name, *, state=AttemptState.ACCEPTED, before=None, returned=None):
        self.provider_id, self.state = name, state
        self.before, self.returned, self.requests = before, returned, []

    def submit(self, request):
        self.requests.append(request)
        if self.before is not None:
            self.before(request)
        return SubmissionAttempt(self.provider_id, self.state,
                                 self.returned or request.tx_identity, 1.0, "200")


def _native(conn, transport, *, signed=None, sender=None, lane_class=lane.LiveLane):
    signed = signed or _signed()
    released, fallback = [], []
    native = lane_class(
        ctx={"address": ACCT.address, "submission_transport": transport,
             "sign_tx_fn": lambda _tx: signed, "lane_claim_nonce_fn": lambda _n: 42,
             "lane_release_nonce_fn": released.append, "gasprice_buffer": 1.5},
        conn=conn, sender=sender, fanout_fn=fallback.append,
        rpc_read=lambda *_: None, facts_fn=lambda _m: (18, 10 ** 27),
        lock_key="offline", lock_dir=None, clip_usd=50.0, low_wei=0,
        generation_fn=lambda: 0, host_clock=time.time, max_snapshot_age_s=30,
        eth_usd_max_age_s=120, generation_margin_s=1, pilot_fills=20,
        max_sends_per_hour=6, require_exact_state=True, lock_receipt_cap_s=.2,
        alert_fn=lambda *_: None)
    if sender is None:
        # The old native branch returns an ACK without any real I/O, so RED failures are
        # assertions about the missing shared send rather than an absent-sender exception.
        native._post = lambda _raw, _payload: ("accepted", signed[1])
    native._released, native._fallback = released, fallback
    return native


def _send(native, conn, pending_id=None, trace=None):
    pending_id = _reserved(conn) if pending_id is None else pending_id
    rec = {"_perf0": time.perf_counter()}
    result = native._send({"snapshot": {"nonce": 42, "gas_price_wei": 40_000_000},
                           "eth_usd": 2500}, rec, conn, pending_id,
                          {"to": MINT, "data": "0x", "gas": 21000}, 10 ** 16, trace)
    return result, rec, pending_id


def test_native_custody_accepts_while_sequencer_is_still_blocked(monkeypatch):
    conn = _ledger()
    entered, release, bound = threading.Event(), threading.Event(), threading.Event()
    binding = {}
    real_bind = db.bind_entry_submission

    def bind(c, pending_id, **fields):
        ok = real_bind(c, pending_id, **fields)
        if ok:
            binding.update(db.entry_submission_binding(c, pending_id))
            bound.set()
        return ok
    monkeypatch.setattr(db, "bind_entry_submission", bind)

    def slow(request):
        assert bound.is_set() and binding["tx_sig"] == request.tx_identity
        assert binding["signed_payload_b64"] == request.signed_payload
        assert binding["signed_payload_sha256"] == request.signed_payload_sha256
        entered.set()
        assert release.wait(2)

    def fast(request):
        assert entered.wait(.5)
        assert bound.is_set() and binding["tx_sig"] == request.tx_identity
    sequencer = Provider("official-4663", before=slow)
    custody = Provider("custody-rpc", before=fast)
    transport = SubmissionTransport([sequencer, custody], deadline_s=.5)
    native, trace = _native(conn, transport), Trace()
    try:
        started = time.perf_counter()
        result, rec, pending_id = _send(native, conn, trace=trace)
        assert custody.requests, "native send never offered the existing custody transport"
        assert result == _signed()[1] and time.perf_counter() - started < .4
        assert entered.is_set() and not release.is_set()
        assert sequencer.requests[0] == custody.requests[0]
        request = custody.requests[0]
        assert (request.chain_id, request.action, request.owner_kind, request.owner_id) == (
            "4663", "entry", "pending", str(pending_id))
        assert binding["submission_backend"] == "rh_fast_sequencer"
        assert rec["send_reply"] == "accepted" and not native._fallback
        audit = conn.execute("SELECT * FROM memecoin_submission_attempts").fetchall()
        assert {row["provider_id"]: row["state"] for row in audit} == {
            "custody-rpc": "accepted", "official-4663": "ambiguous"}
        assert all(row["tx_identity"] == request.tx_identity and
                   row["signed_payload_sha256"] == sha256_hex(request.signed_payload) for row in audit)
        mark = next(fields for name, fields in trace.marks if name == "rh_fast_lane_send")
        assert mark["accepted_by"] == "custody-rpc"
        assert mark["provider_states"] == {"custody-rpc": "accepted", "official-4663": "ambiguous"}
        # The live row's own record (2026-10-14 rh-fast-cached-only §3.3); no receipt clock: unknown.
        assert rec["accepted_by"] == "custody-rpc" and rec["feed_to_send_ms"] is None
    finally:
        release.set()
        transport.close()
        conn.close()


@pytest.mark.parametrize("state,returned", [
    (AttemptState.NOT_SENT, None), (AttemptState.AMBIGUOUS, None),
    (AttemptState.ACCEPTED, "0x" + "ff" * 32),
])
def test_shared_unknown_keeps_signed_owner_and_never_reoffers(state, returned):
    conn = _ledger()
    provider = Provider("custody-rpc", state=state, returned=returned)
    transport = SubmissionTransport([provider], deadline_s=.1)
    native = _native(conn, transport)
    try:
        result, rec, pending_id = _send(native, conn)
        assert provider.requests, "native send skipped shared transport"
        assert result == _signed()[1] and rec["send_reply"] == "ambiguous"
        pending = db.pending_row(conn, pending_id)
        assert pending["status"] == "reserved" and pending["tx_sig"] == _signed()[1]
        assert native._released == [] and native._fallback == []
    finally:
        transport.close()
        conn.close()


def test_shared_explicit_rejection_releases_without_another_offer():
    conn = _ledger()
    provider = Provider("custody-rpc", state=AttemptState.EXPLICIT_REJECTED)
    transport = SubmissionTransport([provider], deadline_s=.1)
    native = _native(conn, transport)
    try:
        result, rec, pending_id = _send(native, conn)
        assert result["status"] == "aborted" and rec["send_reply"] == "rejected"
        assert len(provider.requests) == 1 and not native._fallback
        assert native._released == [42] and db.pending_row(conn, pending_id)["status"] == "failed"
    finally:
        transport.close()
        conn.close()


def test_shared_transport_exception_after_binding_retains_the_signed_owner(monkeypatch):
    conn = _ledger()
    provider = Provider("custody-rpc")
    transport = SubmissionTransport([provider], deadline_s=.1)
    native = _native(conn, transport)
    pending_id = _reserved(conn)

    def failed(request):
        binding = db.entry_submission_binding(conn, pending_id)
        assert binding["tx_sig"] == request.tx_identity
        assert binding["signed_payload_sha256"] == request.signed_payload_sha256
        raise RuntimeError("transport failed after binding")
    monkeypatch.setattr(transport, "submit", failed)
    try:
        with pytest.raises(RuntimeError, match="transport failed after binding"):
            _send(native, conn, pending_id)
        pending = db.pending_row(conn, pending_id)
        assert pending["status"] == "reserved" and pending["tx_sig"] == _signed()[1]
        assert pending["signed_payload_b64"] == _signed()[0]
        assert not provider.requests and not native._fallback and not native._released
    finally:
        transport.close()
        conn.close()


@pytest.mark.parametrize("signed", [
    (_signed()[0], "0x" + "ee" * 32), _signed(chain=56), ("0xinvalid", "0x" + "ee" * 32),
])
def test_malformed_or_mismatched_bound_identity_contacts_no_provider(signed):
    conn = _ledger()
    provider = Provider("custody-rpc")
    transport = SubmissionTransport([provider], deadline_s=.1)
    native = _native(conn, transport, signed=signed)
    try:
        result, _rec, pending_id = _send(native, conn)
        assert isinstance(result, dict) and result["status"] == "unknown"
        assert not provider.requests and not native._fallback and not native._released
        pending = db.pending_row(conn, pending_id)
        assert pending["status"] == "reserved" and pending["tx_sig"] == signed[1]
    finally:
        transport.close()
        conn.close()


def test_shared_binding_conflict_does_not_release_the_existing_signed_owner():
    conn = _ledger()
    pending_id = _reserved(conn)
    raw, identity = _signed(nonce=41)
    assert db.bind_entry_submission(conn, pending_id, tx_identity=identity,
        signed_payload_b64=raw, signed_payload_sha256=sha256_hex(raw), blockhash=None,
        last_valid_height=None, nonce=41, backend="rh_fast_sequencer",
        bound_at="2026-10-14T00:00:00Z", evm_quote_asset_usd=2500)
    provider = Provider("custody-rpc")
    transport = SubmissionTransport([provider], deadline_s=.1)
    native = _native(conn, transport)
    try:
        result, _rec, _pending_id = _send(native, conn, pending_id)
        assert result["status"] == "unknown"
        pending = db.pending_row(conn, pending_id)
        assert pending["status"] == "reserved" and pending["tx_sig"] == identity
        assert not provider.requests and not native._released
    finally:
        transport.close()
        conn.close()


def test_late_audit_keeps_original_owner_after_caller_connection_closes(tmp_path):
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    entered, release = threading.Event(), threading.Event()

    def slow(_request):
        entered.set()
        assert release.wait(2)

    def fast(_request):
        assert entered.wait(.5)
    transport = SubmissionTransport([Provider("official-4663", before=slow),
                                     Provider("custody-rpc", before=fast)], deadline_s=.5)
    native = _native(conn, transport)
    try:
        result, _rec, pending_id = _send(native, conn)
        assert entered.is_set(), "native adapter never dispatched the shared providers"
        conn.close()
        release.set()
        transport._executor.shutdown(wait=True)
        assert flush_async_audit(2)
        with sqlite3.connect(path) as check:
            rows = check.execute("SELECT owner_id,tx_identity,provider_id,state FROM "
                                 "memecoin_submission_attempts ORDER BY id").fetchall()
        assert (str(pending_id), result, "official-4663", "accepted") in rows
        assert all(row[:2] == (str(pending_id), result) for row in rows)
    finally:
        release.set()
        transport.close()
        conn.close()


def test_an_explicit_injected_sender_preserves_legacy_send_even_with_transport():
    conn = _ledger()
    provider = Provider("custody-rpc")
    transport = SubmissionTransport([provider], deadline_s=.1)
    sender = Sender(("ambiguous", None))
    native = _native(conn, transport, sender=sender)
    try:
        result, rec, _pending_id = _send(native, conn)
        assert result == _signed()[1] and rec["send_reply"] == "ambiguous"
        assert sender.sent == [_signed()[0]] and native._fallback == [_signed()[0]]
        assert not provider.requests
    finally:
        transport.close()
        conn.close()


def test_the_native_base_does_not_construct_a_robinhood_sender_for_bsc(monkeypatch):
    from sniper.services.memecoin_bsc_fast_lane import BscLive

    class BscBase(BscLive):
        __init__ = lane.LiveLane.__init__
    conn = _ledger()
    made = []
    monkeypatch.setattr(lane, "SequencerSender", lambda: made.append(True) or object())
    native = _native(conn, None, lane_class=BscBase)
    try:
        assert native.sender is None and native.submission_transport is None
        assert not made
    finally:
        conn.close()


def test_production_context_exposes_the_transport_already_given_to_its_executor(monkeypatch, tmp_path):
    from sniper.execution import submission_backends
    from test_memecoin_evm_context_keys import _production_context
    built = []
    real_build = submission_backends.build_evm_backend

    def build(*args, **kwargs):
        result = real_build(*args, **kwargs)
        built.append(result)
        return result
    monkeypatch.setattr(submission_backends, "build_evm_backend", build)
    ctx = _production_context(monkeypatch, tmp_path, "4663")
    assert ctx.get("submission_transport") is built[0] and built[0] is not None
