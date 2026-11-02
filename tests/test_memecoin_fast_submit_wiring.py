"""Task 6/7 — wiring adapter: shared transport as a drop-in send_fn.

Proves the adapter derives the tx identity from the signed payload, maps the aggregate outcome onto
the existing send_fn seam values (ACCEPTED->identity, AMBIGUOUS->None, EXPLICIT_REJECTED->False),
records the attempt audit on the CALLER thread, and — with a persist gate — sends to ZERO providers
when the durable binding CAS fails. Same adapter object is reusable across entry + exit.
"""
import sqlite3

import pytest
from solders.hash import Hash

from sniper.execution import submission_wiring as W
from sniper.execution.submission_transport import (
    AttemptState, SubmissionAttempt, SubmissionResult, build_request,
)
from sniper.persistence import db_memecoin as m
from _memecoin_signed_tx import signed_tx as _signed_tx


class StubTransport:
    def __init__(self, state):
        self.state = state
        self.calls = 0
        self.thread_ids = []

    def submit(self, request):
        import threading
        self.calls += 1
        self.thread_ids.append(threading.get_ident())
        return SubmissionResult(
            self.state, request.tx_identity,
            (SubmissionAttempt("p", self.state, request.tx_identity, 1.0, "200", None),),
            request.tx_identity if self.state is AttemptState.ACCEPTED else None,
            request.signed_payload_sha256)


def _conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _reserve(c):
    c.execute("INSERT INTO memecoin_pending_entries (mint, source_user_id, strategy, "
              "reserved_notional_usd, reserved_at, status, network_id) VALUES "
              "(?,?,?,?,?, 'reserved', 'solana')", ("M", "u", "real", 50.0, "2026-07-25T00:00:00Z"))
    c.commit()
    return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]



# -- the funded default path finally has words for a rejected broadcast (E9, 2026-09-23) --------
#
# `eth_send_raw` is the executor's send_fn only when NO submission transport is configured. With
# one -- `auto_free`, the funded default on every chain -- a positive rejection came back through
# `aggregate_to_seam` as a bare False having never touched the marker E8 left behind, so the
# executor's reason stayed the bare class. The adapter now publishes the aggregate's message on
# the caller thread, where `take_send_rejection` already reads it.


class _RejectingTransport:
    """A transport whose fanout ends in a positive rejection carrying the node's own words."""

    def __init__(self, message, state=AttemptState.EXPLICIT_REJECTED):
        self.message = message
        self.state = state

    def submit(self, request):
        return SubmissionResult(
            self.state, request.tx_identity,
            (SubmissionAttempt("rpc_a", self.state, None, 1.0, "200", "rejected: nonce too low"),),
            None, request.signed_payload_sha256, self.message)


def test_evm_send_fn_publishes_the_rejection_words_for_the_executor(tmp_path):
    from sniper.execution.evm_real_seams import take_send_rejection
    c = _conn(tmp_path)
    take_send_rejection()                              # start from an empty slot, as the executor does
    send_fn = W.make_evm_send_fn(
        _RejectingTransport("rpc_a: rejected: nonce too low"), chain="8453", conn=c,
        action="entry", owner_kind="reservation", owner_id=None,
        now_fn=lambda: "2026-07-25T00:00:03Z")
    assert send_fn("0x02aabbcc") is False              # the seam's contract is unchanged
    assert take_send_rejection() == "rpc_a: rejected: nonce too low"
    assert take_send_rejection() is None               # taken, so it cannot describe a later send


@pytest.mark.parametrize("state", [AttemptState.ACCEPTED, AttemptState.AMBIGUOUS])
def test_evm_send_fn_leaves_the_slot_empty_when_nothing_was_rejected(tmp_path, state):
    from sniper.execution.evm_real_seams import take_send_rejection
    c = _conn(tmp_path)
    take_send_rejection()
    send_fn = W.make_evm_send_fn(
        _RejectingTransport("rpc_a: rejected: nonce too low", state=state), chain="8453", conn=c,
        action="entry", owner_kind="reservation", owner_id=None,
        now_fn=lambda: "2026-07-25T00:00:03Z")
    send_fn("0x02aabbcc")
    assert take_send_rejection() is None


def test_persist_gate_rejection_has_no_words_because_no_provider_was_contacted():
    """The synthetic EXPLICIT_REJECTED from the persist gate contacted ZERO providers, so it has
    no node words by construction. It must stay absent rather than borrow a neighbour's."""
    transport = StubTransport(AttemptState.ACCEPTED)
    req = build_request(chain_id="solana", action="entry", tx_identity="SIG", signed_payload="AA")
    res = W.guarded_submit(transport, req, persist_fn=lambda r: False)
    assert res.state is AttemptState.EXPLICIT_REJECTED
    assert res.message is None
    assert transport.calls == 0

def test_identity_derivation_solana_and_evm():
    tx = _signed_tx()
    assert W.solana_tx_identity(tx.signed_b64) == tx.signature
    from eth_utils import keccak
    assert W.evm_tx_identity("0x02aabb") == "0x" + keccak(bytes.fromhex("02aabb")).hex()


def test_aggregate_to_seam_mapping():
    r_ok = SubmissionResult(AttemptState.ACCEPTED, "ID", (), "p", "h")
    r_amb = SubmissionResult(AttemptState.AMBIGUOUS, "ID", (), None, "h")
    r_rej = SubmissionResult(AttemptState.EXPLICIT_REJECTED, "ID", (), None, "h")
    assert W.aggregate_to_seam(r_ok) == "ID"
    assert W.aggregate_to_seam(r_amb) is None
    assert W.aggregate_to_seam(r_rej) is False


def test_guarded_submit_persist_failure_sends_to_zero_providers():
    transport = StubTransport(AttemptState.ACCEPTED)
    req = build_request(chain_id="solana", action="entry", tx_identity="SIG", signed_payload="AA")
    res = W.guarded_submit(transport, req, persist_fn=lambda r: False)
    assert res.state is AttemptState.EXPLICIT_REJECTED
    assert transport.calls == 0                       # NO provider received bytes


def test_guarded_submit_persist_success_calls_transport_and_records():
    transport = StubTransport(AttemptState.ACCEPTED)
    req = build_request(chain_id="solana", action="entry", tx_identity="SIG", signed_payload="AA")
    recorded = {}
    W.guarded_submit(transport, req, persist_fn=lambda r: True,
                     record_fn=lambda result: recorded.update(state=result.state))
    assert transport.calls == 1
    assert recorded["state"] is AttemptState.ACCEPTED


def test_solana_send_fn_maps_and_records_audit(tmp_path):
    c = _conn(tmp_path)
    pid = _reserve(c)
    tx = _signed_tx()
    transport = StubTransport(AttemptState.ACCEPTED)
    send_fn = W.make_solana_send_fn(
        transport, conn=c, action="entry", owner_kind="reservation", owner_id=pid,
        now_fn=lambda: "2026-07-25T00:00:03Z", backend_label="auto_free")
    result = send_fn(tx.signed_b64)
    assert result == tx.signature                     # ACCEPTED -> identity
    rows = list(c.execute("SELECT owner_id, state, action FROM memecoin_submission_attempts"))
    assert rows and rows[0]["owner_id"] == str(pid) and rows[0]["state"] == "accepted"


@pytest.mark.parametrize("skip", [False, True])
def test_solana_send_fn_carries_skip_preflight_to_the_request(tmp_path, skip):
    c = _conn(tmp_path)
    pid = _reserve(c)
    tx = _signed_tx()
    seen = []
    transport = StubTransport(AttemptState.ACCEPTED)
    inner = transport.submit
    transport.submit = lambda request: (seen.append(request), inner(request))[1]
    kwargs = {"skip_preflight": True} if skip else {}      # absent == today's default
    send_fn = W.make_solana_send_fn(
        transport, conn=c, action="entry", owner_kind="reservation", owner_id=pid,
        now_fn=lambda: "2026-07-25T00:00:03Z", backend_label="auto_free", persist=True, **kwargs)
    assert send_fn(tx.signed_b64) == tx.signature
    assert seen[0].skip_preflight is skip


def test_solana_send_fn_preflight_commitment_is_bound_to_each_request():
    tx = _signed_tx()
    transport = StubTransport(AttemptState.ACCEPTED)
    seen = []
    inner = transport.submit
    transport.submit = lambda request: (seen.append(request), inner(request))[1]
    withdrawal = W.make_solana_send_fn(
        transport, action="withdraw", owner_kind="withdrawal", owner_id=1,
        preflight_commitment="confirmed")
    entry = W.make_solana_send_fn(
        transport, action="entry", owner_kind="reservation", owner_id=2)
    assert withdrawal(tx.signed_b64) == tx.signature
    assert entry(tx.signed_b64) == tx.signature
    assert [request.preflight_commitment for request in seen] == ["confirmed", None]
    assert seen[0].signed_payload == seen[1].signed_payload == tx.signed_b64


def test_solana_send_fn_invalid_preflight_commitment_contacts_no_provider():
    tx = _signed_tx()
    transport = StubTransport(AttemptState.ACCEPTED)
    with pytest.raises(ValueError, match="preflight_commitment"):
        send_fn = W.make_solana_send_fn(
            transport, action="withdraw", owner_kind="withdrawal", owner_id=1,
            preflight_commitment="recent")
        send_fn(tx.signed_b64)
    assert transport.calls == 0


def test_async_audit_returns_while_another_writer_holds_the_lock(tmp_path):
    # Slot lane 2026-09-26: after the first ACCEPT the caller-thread audit INSERT waited 2,331 ms
    # (4022) for the ledger write lock another thread held, while the lane held the wallet lock.
    # The lock is taken INSIDE submit, i.e. after the synchronous persist bind, as it was live;
    # 600 ms is the hold that makes an inline write measurably late against the 50 ms bound.
    import threading
    import time
    db = str(tmp_path / "t.db")
    c = _conn(tmp_path)
    c.execute("PRAGMA journal_mode=WAL")
    pid = _reserve(c)
    tx = _signed_tx()
    blocker = sqlite3.connect(db, timeout=5, isolation_level=None, check_same_thread=False)

    class LockTakingTransport:
        def submit(self, request):
            blocker.execute("BEGIN IMMEDIATE")
            threading.Timer(0.6, lambda: blocker.execute("COMMIT")).start()
            return SubmissionResult(AttemptState.ACCEPTED, request.tx_identity, (
                SubmissionAttempt("solana:public_rpc", AttemptState.ACCEPTED, request.tx_identity,
                                  111.4, "200", None),
                SubmissionAttempt("solana:tpu", AttemptState.AMBIGUOUS, None, 2000.0, None,
                                  "deadline"),
            ), request.tx_identity, request.signed_payload_sha256)

    send_fn = W.make_solana_send_fn(
        LockTakingTransport(), conn=c, action="entry", owner_kind="reservation", owner_id=pid,
        now_fn=lambda: "2026-09-26T22:00:00Z", backend_label="auto_free", persist=True,
        record_async=True)
    t0 = time.monotonic()
    assert send_fn(tx.signed_b64) == tx.signature
    assert time.monotonic() - t0 < 0.05                 # did not wait for the held lock
    assert m.entry_submission_binding(c, pid)["signed_payload_b64"] == tx.signed_b64
    assert W.flush_async_audit(5.0)
    rows = [tuple(r) for r in c.execute(
        "SELECT provider_id, state, reason_code, created_at, owner_id "
        "FROM memecoin_submission_attempts ORDER BY id")]
    assert rows == [                                   # the loser's receipt too, caller's stamp
        ("solana:public_rpc", "accepted", None, "2026-09-26T22:00:00Z", str(pid)),
        ("solana:tpu", "ambiguous", "deadline", "2026-09-26T22:00:00Z", str(pid)),
    ]
    assert W.async_audit_stats()[db] == {"queued": 0, "dropped": 0, "failed": 0, "written": 2}
    blocker.close()


def test_async_audit_full_queue_drops_and_counts(tmp_path):
    import queue
    w = W._AuditWriter(str(tmp_path / "unused.db"))
    w.q = queue.Queue(maxsize=1)                        # the writer thread still waits on the old one
    w.put({"owner_id": 1})
    w.put({"owner_id": 2})
    assert w.dropped == 1


def test_solana_entry_send_fn_persists_binding_before_submit(tmp_path):
    c = _conn(tmp_path)
    pid = _reserve(c)
    tx = _signed_tx()
    transport = StubTransport(AttemptState.ACCEPTED)
    send_fn = W.make_solana_send_fn(
        transport, conn=c, action="entry", owner_kind="reservation", owner_id=pid,
        now_fn=lambda: "2026-07-25T00:00:03Z", backend_label="auto_free", persist=True)
    assert send_fn(tx.signed_b64) == tx.signature
    binding = m.entry_submission_binding(c, pid)
    assert binding["signed_payload_b64"] == tx.signed_b64
    assert binding["submission_backend"] == "auto_free"
    assert binding["submission_blockhash"] == str(Hash.default())


def test_solana_entry_send_fn_persist_failure_is_ambiguous_no_submit(tmp_path):
    c = _conn(tmp_path)
    # confirmed row -> bind CAS (WHERE status='reserved') fails -> zero providers, seam False
    c.execute("INSERT INTO memecoin_pending_entries (mint, source_user_id, strategy, "
              "reserved_notional_usd, reserved_at, status, network_id) VALUES "
              "(?,?,?,?,?, 'confirmed', 'solana')", ("M", "u", "real", 50.0, "2026-07-25T00:00:00Z"))
    c.commit()
    pid = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    tx = _signed_tx()
    transport = StubTransport(AttemptState.ACCEPTED)
    send_fn = W.make_solana_send_fn(
        transport, conn=c, action="entry", owner_kind="reservation", owner_id=pid,
        now_fn=lambda: "2026-07-25T00:00:03Z", backend_label="auto_free", persist=True)
    assert send_fn(tx.signed_b64) is None
    assert transport.calls == 0                       # persistence failed -> nothing broadcast


def test_evm_send_fn_maps_ambiguous_to_none(tmp_path):
    c = _conn(tmp_path)
    transport = StubTransport(AttemptState.AMBIGUOUS)
    send_fn = W.make_evm_send_fn(
        transport, chain="8453", conn=c, action="exit", owner_kind="close_claim", owner_id=7,
        now_fn=lambda: "2026-07-25T00:00:03Z")
    assert send_fn("0x02aabbcc") is None              # AMBIGUOUS -> None (leave open, submitted_unknown)


def test_evm_send_fn_rejected_maps_to_false(tmp_path):
    c = _conn(tmp_path)
    transport = StubTransport(AttemptState.EXPLICIT_REJECTED)
    send_fn = W.make_evm_send_fn(
        transport, chain="8453", conn=c, action="approval", owner_kind="approval", owner_id=None,
        now_fn=lambda: "2026-07-25T00:00:03Z")
    assert send_fn("0x02aabbcc") is False             # all-rejected -> False (safe re-sign)


def test_heartbeat_carries_async_audit_counters_by_file_name(tmp_path):
    # A dead audit writer or dropped rows must be visible without a log grep (Alpha, 2026-09-26).
    import json
    from sniper.services import memecoin_runner as run
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), submit_audit={
        "/x/y/sniper_tracker.db": {"queued": 2, "dropped": 1, "failed": 0, "written": 7,
                                   "extra": 9}})
    got = json.loads(path.read_text())["submit_audit"]
    assert got == {"sniper_tracker.db": {"queued": 2, "dropped": 1, "failed": 0, "written": 7}}
