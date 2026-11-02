"""Task 3 — durable submission binding + append-only attempt audit.

Before the first network write the caller must persist the signed identity + payload + validity;
``bind_entry_submission`` returns a durable marker only after the CAS commits, and refuses a
non-reserved / missing row (fail-closed). The attempt audit stores hashes, never the payload, and
keys rows to the owning reservation/claim. Migration is additive + idempotent.
"""
import sqlite3

import pytest

from sniper.execution.submission_transport import AttemptState, SubmissionAttempt
from sniper.persistence import db_memecoin as m


def _conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _reserve(c, status="reserved"):
    c.execute(
        "INSERT INTO memecoin_pending_entries "
        "(mint, source_user_id, strategy, reserved_notional_usd, reserved_at, status, network_id) "
        "VALUES (?,?,?,?,?,?,?)",
        ("MintX", "u1", "real", 50.0, "2026-07-25T00:00:00Z", status, "solana"))
    c.commit()
    return c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def test_migration_adds_binding_columns_and_audit_table(tmp_path):
    c = _conn(tmp_path)
    pe_cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_pending_entries)")}
    for col in ("signed_payload_b64", "signed_payload_sha256", "submission_blockhash",
                "submission_last_valid_height", "submission_backend", "submission_bound_at"):
        assert col in pe_cols
    audit_cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_submission_attempts)")}
    assert {"chain_id", "action", "owner_kind", "owner_id", "tx_identity",
            "signed_payload_sha256", "provider_id", "state", "latency_ms",
            "response_code", "reason_code", "created_at"} <= audit_cols
    # the audit table stores hashes only — never the signed payload bytes
    assert "signed_payload_b64" not in audit_cols
    assert "signed_payload" not in audit_cols


def test_ensure_is_idempotent(tmp_path):
    c = _conn(tmp_path)
    m.ensure_memecoin_tables(c)          # second run must not raise
    m.ensure_memecoin_tables(c)


def test_bind_entry_submission_cas_writes_and_returns_marker(tmp_path):
    c = _conn(tmp_path)
    pid = _reserve(c)
    ok = m.bind_entry_submission(
        c, pid, tx_identity="SIG1", signed_payload_b64="AAAA",
        signed_payload_sha256="hashhash", blockhash="BH", last_valid_height=123,
        nonce=None, backend="auto_free", bound_at="2026-07-25T00:00:01Z")
    assert ok is True
    binding = m.entry_submission_binding(c, pid)
    assert binding["signed_payload_b64"] == "AAAA"
    assert binding["signed_payload_sha256"] == "hashhash"
    assert binding["submission_blockhash"] == "BH"
    assert binding["submission_last_valid_height"] == 123
    assert binding["submission_backend"] == "auto_free"
    assert binding["tx_sig"] == "SIG1"


def test_bind_entry_submission_is_immutable_except_same_signed_identity(tmp_path):
    c = _conn(tmp_path)
    pid = _reserve(c)
    first = dict(
        tx_identity="SIG1", signed_payload_b64="AAAA", signed_payload_sha256="hash1",
        blockhash="BH1", last_valid_height=123, nonce=None, backend="pooled_rpc",
        bound_at="2026-07-25T00:00:01Z")
    assert m.bind_entry_submission(c, pid, **first) is True
    replay = {**first, "last_valid_height": 999, "backend": "different",
              "bound_at": "2026-07-25T00:00:09Z"}
    assert m.bind_entry_submission(c, pid, **replay) is True
    replayed = m.entry_submission_binding(c, pid)
    assert replayed["submission_last_valid_height"] == 123
    assert replayed["submission_backend"] == "pooled_rpc"
    assert replayed["submission_bound_at"] == "2026-07-25T00:00:01Z"

    conflicting = {**first, "tx_identity": "SIG2", "signed_payload_b64": "BBBB",
                   "signed_payload_sha256": "hash2", "blockhash": "BH2"}
    assert m.bind_entry_submission(c, pid, **conflicting) is False
    binding = m.entry_submission_binding(c, pid)
    assert binding["tx_sig"] == "SIG1"
    assert binding["signed_payload_b64"] == "AAAA"
    assert binding["signed_payload_sha256"] == "hash1"


def test_unbound_pre_transaction_replacement_requires_its_exact_identity(tmp_path):
    c = _conn(tmp_path)
    pid = _reserve(c)
    m.set_pending_broadcast(c, pid, "0xpre", 6, "2026-07-25T00:00:01Z")
    fields = dict(
        tx_identity="0xswap", signed_payload_b64="0xraw",
        signed_payload_sha256="hash", blockhash=None, last_valid_height=None,
        nonce=7, backend="auto_free", bound_at="2026-07-25T00:00:02Z",
    )

    assert not m.bind_entry_submission(c, pid, replace_unbound="0xother", **fields)
    assert m.entry_submission_binding(c, pid)["tx_sig"] == "0xpre"
    assert m.bind_entry_submission(c, pid, replace_unbound="0xpre", **fields)
    binding = m.entry_submission_binding(c, pid)
    assert binding["tx_sig"] == "0xswap"
    assert binding["nonce"] == 7


def test_bind_refuses_non_reserved_row(tmp_path):
    c = _conn(tmp_path)
    pid = _reserve(c, status="confirmed")
    ok = m.bind_entry_submission(
        c, pid, tx_identity="SIG1", signed_payload_b64="AAAA",
        signed_payload_sha256="h", blockhash=None, last_valid_height=None,
        nonce=None, backend="rpc", bound_at="2026-07-25T00:00:01Z")
    assert ok is False
    assert m.entry_submission_binding(c, pid)["signed_payload_b64"] is None


def test_bind_missing_row_returns_false(tmp_path):
    c = _conn(tmp_path)
    ok = m.bind_entry_submission(
        c, 999999, tx_identity="SIG1", signed_payload_b64="AAAA",
        signed_payload_sha256="h", blockhash=None, last_valid_height=None,
        nonce=None, backend="rpc", bound_at="2026-07-25T00:00:01Z")
    assert ok is False


def test_record_attempts_appends_keyed_to_owner_without_payload(tmp_path):
    c = _conn(tmp_path)
    pid = _reserve(c)
    attempts = [
        SubmissionAttempt("solana:custody_rpc", AttemptState.ACCEPTED, "SIG1", 12.5, "200", None),
        SubmissionAttempt("solana:public_rpc", AttemptState.AMBIGUOUS, None, 80.0, None, "deadline"),
        SubmissionAttempt("solana:tpu", AttemptState.NOT_SENT, None, 0.0, None, "sidecar_unavailable"),
    ]
    m.record_submission_attempts(
        c, chain_id="solana", action="entry", owner_kind="reservation", owner_id=str(pid),
        tx_identity="SIG1", signed_payload_sha256="hashhash", attempts=attempts,
        created_at="2026-07-25T00:00:02Z")
    rows = list(c.execute(
        "SELECT provider_id, state, reason_code, owner_id, signed_payload_sha256 "
        "FROM memecoin_submission_attempts ORDER BY id"))
    assert len(rows) == 3
    assert rows[0]["provider_id"] == "solana:custody_rpc"
    assert rows[0]["state"] == "accepted"
    assert rows[1]["state"] == "ambiguous"
    assert rows[1]["reason_code"] == "deadline"
    # a NOT_SENT attempt is persisted verbatim (observability), never altering execution authority
    assert rows[2]["state"] == "not_sent"
    assert rows[2]["reason_code"] == "sidecar_unavailable"
    assert all(r["owner_id"] == str(pid) for r in rows)
    assert all(r["signed_payload_sha256"] == "hashhash" for r in rows)


def test_recent_submission_attempts_returns_sanitized_rows_newest_first(tmp_path):
    c = _conn(tmp_path)
    for pid in ("solana:custody_rpc", "solana:public_rpc", "solana:tpu"):
        m.record_submission_attempts(
            c, chain_id="solana", action="entry", owner_kind="reservation", owner_id="1",
            tx_identity="SIG", signed_payload_sha256="hash",
            attempts=[SubmissionAttempt(pid, AttemptState.ACCEPTED, "SIG", 3.0, "200", None)],
            created_at="2026-07-26T00:00:00Z")
    rows = m.recent_submission_attempts(c, limit=2)
    assert len(rows) == 2                                 # newest-first, bounded by limit
    assert set(rows[0]) == {"provider_id", "state", "reason_code", "latency_ms"}   # no payload/url
    assert rows[0]["provider_id"] == "solana:tpu"         # the last recorded is first


def test_failed_attempt_audit_rolls_back_its_transaction(tmp_path):
    c = _conn(tmp_path)
    c.execute(
        "CREATE TRIGGER reject_submission_audit BEFORE INSERT ON memecoin_submission_attempts "
        "BEGIN SELECT RAISE(ABORT, 'audit blocked'); END")
    c.commit()
    attempt = SubmissionAttempt(
        "sender", AttemptState.ACCEPTED, "SIG1", 1.0, "200", None)
    with pytest.raises(sqlite3.IntegrityError):
        m.record_submission_attempts(
            c, chain_id="solana", action="entry", owner_kind="reservation", owner_id="1",
            tx_identity="SIG1", signed_payload_sha256="hashhash", attempts=[attempt],
            created_at="2026-07-25T00:00:02Z")
    assert c.in_transaction is False


@pytest.mark.parametrize("payload", [
    {"result": "SIG1", "error": {"code": -32429}},
    {"jsonrpc": "1.0", "id": 1,
     "error": {"code": -32429, "message": "signature verification failure"}},
    {"jsonrpc": "2.0", "id": 2,
     "error": {"code": -32429, "message": "signature verification failure"}},
    {"error": {"code": -32429.0, "message": "signature verification failure"}},
    {"error": {"code": "-32429", "message": "signature verification failure"}},
])
def test_malformed_credit_reply_keeps_signed_reservation_and_unknown_send(tmp_path, payload):
    import json
    from sniper.execution.solana_fast_submit import SolanaRpcProvider
    from sniper.execution.solana_rpc_pool import RotatingSolanaRpcHttp
    from sniper.execution.submission_transport import SubmissionTransport, build_request
    from sniper.execution.submission_wiring import aggregate_to_seam, guarded_submit

    c = _conn(tmp_path)
    pid = _reserve(c)
    calls = []

    def http(_method, url, headers=None, body=None, timeout=None):
        calls.append((url, json.loads(body)["params"][0]))
        return ((200, payload, None)
                if url == "https://first" else
                (200, {"error": {"message": "signature verification failure"}}, None))

    pool = RotatingSolanaRpcHttp(http, ("https://first", "https://second"), env={})
    transport = SubmissionTransport([SolanaRpcProvider(pool, "https://first")], deadline_s=1)
    request = build_request(chain_id="solana", action="entry", tx_identity="SIG1",
                            signed_payload="AAAA", owner_kind="reservation", owner_id=pid)
    try:
        result = guarded_submit(transport, request, persist_fn=lambda req: m.bind_entry_submission(
            c, pid, tx_identity=req.tx_identity, signed_payload_b64=req.signed_payload,
            signed_payload_sha256=req.signed_payload_sha256, blockhash="BH", last_valid_height=123,
            nonce=None, backend="auto_free", bound_at="2026-07-25T00:00:01Z"))
        assert result.state is AttemptState.AMBIGUOUS
        assert aggregate_to_seam(result) is None
        assert calls == [("https://first", "AAAA")]
        binding = m.entry_submission_binding(c, pid)
        assert (binding["tx_sig"], binding["signed_payload_b64"]) == ("SIG1", "AAAA")
        assert c.execute("SELECT status FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone()[0] == "reserved"
    finally:
        transport.close()
        c.close()
