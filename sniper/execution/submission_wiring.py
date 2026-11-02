"""Adapters that present the shared submission transport as the executors' existing ``send_fn`` seam.

Both chain executors already treat ``send_fn(payload)`` as tri-state — a truthy identity means
accepted (proceed to chain confirmation), ``None`` means ambiguous (leave the position/reservation
in submitted-unknown), ``False`` means an explicit rejection (safe to release / re-sign). The shared
transport's aggregate maps onto exactly those values. The Solana entry executor additionally lets a
persistent adapter own the stronger immutable full-payload binding before broadcast.

The adapter derives the transaction identity from the signed payload (never from the transport), runs
the fanout, and records the per-provider attempt audit on the CALLER thread (workers never touch
SQLite). For the Solana entry path — a single, unambiguous send — it optionally persists the durable
full-payload binding via a CAS BEFORE any provider is called: if the CAS fails, ZERO providers
receive bytes and the seam returns ``None`` so any older signed owner remains protected.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import queue
import threading
import time

import rlp          # at module top ON PURPOSE: `evm_raw_chain_id` guards chain-1 bytes, so a missing
                    # decoder must fail at startup, never read every transaction as "not chain 1"

from sniper.execution.submission_transport import (
    AttemptState, SubmissionResult, build_request, _entry_request_slot_refusal,
)
from sniper.persistence import db_memecoin as m

log = logging.getLogger(__name__)

# A Solana packet is capped at 1232 bytes. A larger signed transaction is rejected by every node
# that evaluates it ("transaction too large"), but the keyless TPU sidecar only delivers datagrams
# and reports ACCEPTED, so aggregate() saw an accept and the caller recorded a broadcast. The
# reservation then blocked its mint and the exposure slot for ~14 minutes until the recovery path
# proved the blockhash expired. Live 2026-08-23, reservations 664 and 665; 5 of 5 oversized
# payloads since 2026-08-16 died this way, 0 of 86 in-limit ones did.
SOLANA_PACKET_LIMIT = 1232


def solana_tx_identity(signed_b64):
    from solders.transaction import VersionedTransaction
    vt = VersionedTransaction.from_bytes(base64.b64decode(signed_b64))
    return str(vt.signatures[0])


def solana_blockhash(signed_b64):
    from solders.transaction import VersionedTransaction
    try:
        vt = VersionedTransaction.from_bytes(base64.b64decode(signed_b64))
        return str(vt.message.recent_blockhash)
    except Exception:
        return None


def evm_tx_identity(raw_hex):
    from eth_utils import keccak
    hexpart = raw_hex[2:] if str(raw_hex).startswith("0x") else str(raw_hex)
    return "0x" + keccak(bytes.fromhex(hexpart)).hex()


def evm_raw_chain_id(raw_hex):
    """The chain id a signed raw EVM transaction commits to (EIP-155 legacy `v`, or an EIP-2718
    typed transaction's first field), or None when the bytes cannot be read. Used to keep chain-1
    bytes on their private-only branch whatever path tries to send them."""
    try:
        text = str(raw_hex or "")
        data = bytes.fromhex(text[2:] if text.startswith("0x") else text)
        if not data:
            return None
        if data[0] >= 0xc0:                                  # legacy RLP list
            v = int.from_bytes(rlp.decode(data)[6], "big")
            return (v - 35) // 2 if v >= 35 else None
        if data[0] in (1, 2, 3, 4):                          # typed: chainId is the first field
            return int.from_bytes(rlp.decode(data[1:])[0], "big")
    except Exception:
        return None
    return None


def aggregate_to_seam(result: SubmissionResult):
    """ACCEPTED -> the tx identity (truthy); EXPLICIT_REJECTED -> False; AMBIGUOUS -> None."""
    if result.state is AttemptState.ACCEPTED:
        return result.tx_identity
    if result.state is AttemptState.EXPLICIT_REJECTED:
        return False
    return None


def guarded_submit(transport, request, *, persist_fn=None, record_fn=None, observe_fn=None):
    """Persist-gated fanout. When ``persist_fn`` is given it must return a truthy durable marker
    BEFORE any provider is contacted; a falsy marker yields a synthetic EXPLICIT_REJECTED result and
    the transport is never called (zero provider bytes). ``record_fn`` writes the attempt audit on
    the caller thread and can never change the outcome. Slot refusals stay ambiguous because the
    request may already belong to a signed reservation from an earlier call."""
    refused = _entry_request_slot_refusal(request)
    if refused:
        return SubmissionResult(AttemptState.AMBIGUOUS, request.tx_identity, (), None,
                                request.signed_payload_sha256, refused)
    if persist_fn is not None and not persist_fn(request):
        return SubmissionResult(
            AttemptState.EXPLICIT_REJECTED, request.tx_identity, (), None,
            request.signed_payload_sha256)
    if observe_fn is not None:
        try:
            observe_fn("dispatch")
        except Exception:
            log.debug("submission timing observation failed", exc_info=True)
    # Binding or observation can wait across slots; an existing signed owner stays ambiguous.
    refused = _entry_request_slot_refusal(request)
    if refused:
        return SubmissionResult(AttemptState.AMBIGUOUS, request.tx_identity, (), None,
                                request.signed_payload_sha256, refused)
    result = transport.submit(request)
    if observe_fn is not None:
        try:
            observe_fn("ack")
        except Exception:
            log.debug("submission timing observation failed", exc_info=True)
    if record_fn is not None:
        try:
            record_fn(result)
        except Exception:
            pass                                     # audit is best-effort; never affects execution
    return result


def _sanitize_tpu_timing(timing):
    if not isinstance(timing, dict) or "request_min_context_slot" not in timing:
        return None
    guard = timing.get("tpu_guard")
    if not isinstance(guard, dict) or type(guard.get("version")) is not int or guard["version"] != 1:
        return None
    integers = ("estimated_slot", "minimum_slot", "available_leader_count")
    if any(type(guard.get(k)) is not int or not 0 <= guard[k] <= 2**64 - 1 for k in integers):
        return None
    age = guard.get("age_since_advance_ms")
    if type(age) not in (int, float) or age < 0:
        return None
    try:
        if not math.isfinite(age):
            return None
    except OverflowError:
        return None
    if any(type(guard.get(k)) is not bool for k in ("clock_frozen", "behind_minimum")):
        return None
    minimum = timing["request_min_context_slot"]
    if minimum is not None and (type(minimum) is not int or not 0 <= minimum <= 2**64 - 1):
        return None
    return {"tpu_guard": {k: guard[k] for k in (
        "version", *integers, "age_since_advance_ms", "clock_frozen", "behind_minimum")},
        "request_min_context_slot": minimum}


class _AuditWriter:
    """Writes submission audit rows for one ledger file on its own thread and connection.

    WHY (2026-09-26): the slot lane's audit INSERT ran on the caller thread after the first ACCEPT
    and waited for the ledger write lock held by another thread -- 2,331 ms on reservation 4022 and
    830 ms on 4029 -- while the lane held the wallet lock that funded exits wait on. The rows are
    display/audit only (read by memecoin_submission_status), so they can land later; the identity
    bind and the fill booking stay synchronous. One consumer per file keeps the rows in order.
    Rows still queued when the process dies are lost; a full queue drops and counts."""
    MAXSIZE = 1000

    def __init__(self, db_path):
        self.db_path = db_path
        self.q = queue.Queue(maxsize=self.MAXSIZE)
        self.dropped = self.failed = self.written = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._run, name="memecoin-submit-audit", daemon=True).start()

    def put(self, row):
        try:
            self.q.put_nowait(row)
        except queue.Full:
            with self._lock:
                self.dropped += 1
            log.warning("submission audit queue full; dropped owner=%s identity=%s dropped=%d",
                        row.get("owner_id"), row.get("tx_identity"), self.dropped)

    def _run(self):
        from sniper.persistence.db_core import get_connection
        conn = None
        while True:
            row = self.q.get()
            try:
                if conn is None:
                    conn = get_connection(self.db_path)
                self.written += m.record_submission_attempts(conn, **row)
                for attempt in row["attempts"]:
                    timing = getattr(attempt, "provider_timing", None)
                    diagnostic = _sanitize_tpu_timing(timing)
                    if diagnostic is not None and attempt.provider_id == "solana:tpu":
                        print("submission TPU guard " + json.dumps({
                            "owner_id": row["owner_id"], "tx_identity": row["tx_identity"],
                            "payload_sha256": row["signed_payload_sha256"],
                            **diagnostic}, sort_keys=True))
                    if (isinstance(timing, dict) and timing.get("clock") == "perf_counter"
                            and all(type(timing.get(k)) in (int, float) for k in
                                    ("post_started_monotonic", "post_finished_monotonic"))):
                        # Public, post-result evidence on the existing audit thread/log handler.
                        print("submission provider final " + json.dumps({
                            "provider_id": attempt.provider_id, "state": attempt.state.value,
                            "tx_identity": row["tx_identity"],
                            "payload_sha256": row["signed_payload_sha256"],
                            "observed_at": row["created_at"], "clock": "perf_counter",
                            "post_started_monotonic": timing["post_started_monotonic"],
                            "post_finished_monotonic": timing["post_finished_monotonic"]},
                            sort_keys=True))
            except Exception:
                self.failed += 1
                log.exception("async submission audit write failed owner=%s identity=%s",
                              row.get("owner_id"), row.get("tx_identity"))
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None                      # reconnect for the next row
            finally:
                self.q.task_done()


_AUDIT_WRITERS = {}
_AUDIT_WRITERS_LOCK = threading.Lock()


def _audit_writer(db_path):
    with _AUDIT_WRITERS_LOCK:
        writer = _AUDIT_WRITERS.get(db_path)
        if writer is None:
            writer = _AUDIT_WRITERS[db_path] = _AuditWriter(db_path)
        return writer


def async_audit_stats():
    """{db_path: queued/dropped/failed/written} for the async submission audit writers."""
    with _AUDIT_WRITERS_LOCK:
        writers = dict(_AUDIT_WRITERS)
    return {p: {"queued": w.q.unfinished_tasks, "dropped": w.dropped, "failed": w.failed,
                "written": w.written} for p, w in writers.items()}


def flush_async_audit(timeout_s=5.0):
    """Wait until every queued audit row is written or failed. True when drained."""
    deadline = time.monotonic() + timeout_s
    while any(s["queued"] for s in async_audit_stats().values()):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _db_file(conn):
    """The main database FILE behind `conn`, or None (in-memory) so the caller writes inline."""
    row = conn.execute("PRAGMA database_list").fetchone()
    return (row[2] or None) if row else None


def _record_fn(conn, *, chain_id, action, owner_kind, owner_id, identity, sha256, now_fn,
               async_write=False):
    if conn is None:
        return None

    def record(result):
        row = dict(chain_id=chain_id, action=action, owner_kind=owner_kind, owner_id=owner_id,
                   tx_identity=identity, signed_payload_sha256=sha256, attempts=result.attempts,
                   created_at=(now_fn() if now_fn else None))   # stamped HERE: "transport returned"
        path = _db_file(conn) if async_write else None
        if path:
            writer = _audit_writer(path)
            writer.put(row)
            # Final answers append separately on the existing writer; no worker uses `conn`.
            def record_late(future):
                try:
                    writer.put({**row, "attempts": (future.result(),),
                                "created_at": now_fn() if now_fn else None})
                except Exception:
                    log.exception("late submission audit failed identity=%s", identity)
            for future in result.late_results:
                future.add_done_callback(record_late)
            return
        m.record_submission_attempts(conn, **row)
    return record


def _resolve_owner_id(conn, owner_kind, owner_id, owner_id_fn):
    if owner_id_fn is not None:
        return owner_id_fn()
    claim_col = {"close_claim": "close_claim_id",
                 "derisk_claim": "derisk_claim_id"}.get(owner_kind)
    if conn is not None and claim_col is not None:
        row = m.get_position(conn, owner_id)
        if row and row.get(claim_col):
            return row[claim_col]
    return owner_id


def make_solana_send_fn(transport, *, conn=None, action, owner_kind, owner_id, owner_id_fn=None,
                        now_fn=None, backend_label="fast", persist=False, record=True,
                        skip_preflight=False, observe_fn=None, record_async=False,
                        preflight_commitment=None, entry_max_slot=None, entry_head_fn=None):
    """A ``send_fn(signed_b64) -> identity|None|False`` backed by the shared transport. With
    ``persist=True`` (Solana entry) the durable full-payload binding is CAS-written before broadcast.
    ``skip_preflight=True`` (slot lane only) makes every RPC leg send without preflight.
    ``preflight_commitment`` binds the simulation bank to this send's blockhash commitment.
    ``entry_max_slot`` is a frozen source deadline; ``entry_head_fn`` reads only the local head.
    ``record_async=True`` (slot lane only) queues the attempt audit to a background writer instead of
    writing it on the caller thread; the persist bind above stays synchronous."""
    def send_fn(signed_b64, *, validity=None):
        size = len(base64.b64decode(signed_b64))
        if size > SOLANA_PACKET_LIMIT:
            # No node can ever take these bytes -> reject BEFORE the identity bind and before any
            # provider, so the caller releases the reservation now instead of after the 600s
            # recovery window. Never re-sign here; the caller owns that decision.
            log.warning("solana submit refused action=%s owner=%s size=%d limit=%d reason=oversized_tx",
                        action, owner_id, size, SOLANA_PACKET_LIMIT)
            return False
        identity = solana_tx_identity(signed_b64)
        payload_blockhash = solana_blockhash(signed_b64)
        validity = validity if isinstance(validity, dict) else {}
        stated_blockhash = validity.get("blockhash")
        if stated_blockhash is not None and str(stated_blockhash) != str(payload_blockhash):
            return False                            # metadata cannot bind different signed bytes
        blockhash = stated_blockhash or payload_blockhash
        last_valid_height = validity.get("last_valid_height")
        created_at = now_fn() if now_fn else None
        resolved_owner_id = _resolve_owner_id(conn, owner_kind, owner_id, owner_id_fn)
        request = build_request(
            chain_id="solana", action=action, tx_identity=identity, signed_payload=signed_b64,
            blockhash=blockhash, last_valid_height=last_valid_height,
            owner_kind=owner_kind,
            owner_id=(None if resolved_owner_id is None else str(resolved_owner_id)),
            created_at=created_at, skip_preflight=skip_preflight,
            preflight_commitment=preflight_commitment,
            entry_max_slot=entry_max_slot, entry_head_fn=entry_head_fn)
        send_fn.last_request = request      # the slot lane re-sends exactly these bytes (2026-09-28)
        persist_fn = None
        if persist and conn is not None:
            def persist_fn(req):
                if observe_fn is not None:
                    try:
                        observe_fn("identity_start")
                    except Exception:
                        log.debug("identity timing observation failed", exc_info=True)
                bound = m.bind_entry_submission(
                    conn, owner_id, tx_identity=req.tx_identity,
                    signed_payload_b64=req.signed_payload,
                    signed_payload_sha256=req.signed_payload_sha256, blockhash=req.blockhash,
                    last_valid_height=req.last_valid_height, nonce=None, backend=backend_label,
                    bound_at=(req.created_at or (now_fn() if now_fn else None)))
                if observe_fn is not None:
                    try:
                        observe_fn("identity_bound")
                    except Exception:
                        log.debug("identity timing observation failed", exc_info=True)
                return bound
        record_fn = _record_fn(
            conn if record else None, chain_id="solana", action=action, owner_kind=owner_kind,
            owner_id=(None if resolved_owner_id is None else str(resolved_owner_id)),
            identity=identity,
            sha256=request.signed_payload_sha256, now_fn=now_fn, async_write=record_async)
        result = guarded_submit(
            transport, request, persist_fn=persist_fn, record_fn=record_fn, observe_fn=observe_fn)
        # A failed immutable bind can mean an older signed identity already owns this reservation.
        # That is unknown/submitted state, never proof that nothing was sent. Provider rejections
        # have attempt rows and retain the normal False mapping.
        if persist and result.state is AttemptState.EXPLICIT_REJECTED and not result.attempts:
            return None
        return aggregate_to_seam(result)
    send_fn.accepts_validity = True
    send_fn.persists_identity = bool(persist and conn is not None)
    send_fn.last_request = None
    return send_fn


# RELAY-DOWN (Gamma MEDIUM on 735d89e70, 2026-09-30). A private-only chain has ONE sender and no
# fallback, by the operator's choice, and that covers EXITS and stops too: while the relay does not
# answer, a bag on that chain cannot be sold. So RELAY_DOWN_AFTER sends in a row that the relay did
# not answer (ambiguous, or proved not sent) page at once; any answer, an acceptance or the relay's
# own rejection, ends the streak, and the first answer after a page says so. Counted on the CALLER
# thread (every chain-1 send goes through `make_evm_send_fn`), so the page never runs on a
# transport worker.
RELAY_DOWN_AFTER = 3
_relay_streak = {}                    # chain -> [unanswered sends in a row, paged]
_relay_lock = threading.Lock()


def _note_private_relay(chain, result):
    """Track one private-only send's outcome and page on the edge. Never raises."""
    try:
        from sniper.execution.free_endpoint_registry import private_submit_endpoint
        private = private_submit_endpoint(chain)
        if private is None:
            return
        answered = result.state in (AttemptState.ACCEPTED, AttemptState.EXPLICIT_REJECTED)
        if answered and not result.attempts:
            return                    # the persist gate refused: the relay was never asked
        with _relay_lock:
            streak = _relay_streak.setdefault(str(chain), [0, False])
            if answered:
                kind = "RELAY-UP" if streak[1] else None
                streak[0], streak[1] = 0, False
            else:
                streak[0] += 1
                kind = "RELAY-DOWN" if streak[0] >= RELAY_DOWN_AFTER and not streak[1] else None
                streak[1] = streak[1] or kind is not None
            count = streak[0]
        if kind is None:
            return
        from sniper.services import memecoin_alerts
        if kind == "RELAY-UP":
            memecoin_alerts.send_alert(kind, "chain %s private relay %s answers again"
                                       % (chain, private[0]), icon="🟢")
        else:
            memecoin_alerts.send_alert(
                kind, "chain %s private relay %s did not answer %d sends in a row. There is no "
                "public fallback by design, so entries AND exits (stops included) on chain %s are "
                "blocked until it answers." % (chain, private[0], count, chain))
    except Exception:
        log.exception("private relay watch failed (the send verdict is unchanged)")


def _watch_private_status(chain, result, identity, action):
    """P0 (2026-10-08): a private-only chain's accepted send is watched on the relay's own status
    API off the caller's path (sniper/execution/chain1_protect.py). Never raises."""
    try:
        from sniper.execution.free_endpoint_registry import private_submit_endpoint
        if private_submit_endpoint(chain) is None or result.state is not AttemptState.ACCEPTED:
            return
        from sniper.execution import chain1_protect
        chain1_protect.watch_status(identity, chain=str(chain), action=action)
    except Exception:
        log.exception("private status watch not started (the send verdict is unchanged)")


def make_evm_send_fn(transport, *, chain, conn=None, action, owner_kind, owner_id,
                     owner_id_fn=None, now_fn=None, record=True):
    """A ``send_fn(raw_hex) -> identity|None|False`` backed by the shared transport for one EVM
    chain. The raw signed transaction is broadcast verbatim to every configured endpoint."""
    def send_fn(raw_hex):
        identity = evm_tx_identity(raw_hex)
        created_at = now_fn() if now_fn else None
        resolved_owner_id = _resolve_owner_id(conn, owner_kind, owner_id, owner_id_fn)
        request = build_request(
            chain_id=str(chain), action=action, tx_identity=identity, signed_payload=raw_hex,
            owner_kind=owner_kind,
            owner_id=(None if resolved_owner_id is None else str(resolved_owner_id)),
            created_at=created_at)
        record_fn = _record_fn(
            conn if record else None, chain_id=str(chain), action=action, owner_kind=owner_kind,
            owner_id=(None if resolved_owner_id is None else str(resolved_owner_id)),
            identity=identity,
            sha256=request.signed_payload_sha256, now_fn=now_fn)
        result = guarded_submit(transport, request, record_fn=record_fn)
        _note_private_relay(chain, result)
        _watch_private_status(chain, result, identity, action)
        if result.state is AttemptState.EXPLICIT_REJECTED and result.message:
            # THE WORDS, ON THE CALLER'S THREAD (E9, 2026-09-23). `aggregate_to_seam` keeps its
            # identity/None/False contract; the diagnosis travels beside it in the thread-local
            # the EVM executor already takes immediately after this call. Imported here, not at
            # module scope: `evm_real_seams` imports this module from inside its own functions.
            #
            # SOLANA IS NOT WIRED: `solana_swap_executor` writes its own bare reason and never
            # reads that slot, so giving the Solana path words is its own change.
            from sniper.execution.evm_real_seams import record_send_rejection
            record_send_rejection(result.message)
        return aggregate_to_seam(result)
    return send_fn
