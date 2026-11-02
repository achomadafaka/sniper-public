from sniper.execution import evm_real_seams as seams
import sqlite3
import pytest

from sniper.persistence import db_memecoin as db
from sniper.execution.evm_swap_executor import recover_confirmed_ink_entry
from sniper.services.memecoin_evm_onchain_watch import TRANSFER_TOPIC


HASH = "0x" + "a" * 64
BLOCK = "0x" + "b" * 64


def _transfer(token, sender, recipient, amount, index):
    return {"address": token, "topics": [TRANSFER_TOPIC,
            "0x" + "0" * 24 + sender[2:], "0x" + "0" * 24 + recipient[2:]],
            "data": hex(amount), "transactionHash": HASH, "blockHash": BLOCK,
            "logIndex": hex(index)}


@pytest.mark.parametrize("network,native", [("57073", False), ("1", False), ("1", True)])
def test_confirmed_buy_recovers_once_from_own_canonical_receipt(network, native):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    owner, pool = "0x" + "1" * 40, "0x" + "2" * 40
    mint, weth = "0x" + "3" * 40, "0x4200000000000000000000000000000000000006"
    pending_id = db.reserve_pending(
        conn, mint=mint, source_user_id="source", strategy="alpha_b", notional=10.0,
        reserved_at="2026-09-29T09:00:00Z", network_id=network)
    assert db.bind_entry_submission(
        conn, pending_id, tx_identity=HASH, signed_payload_b64="0xraw",
        signed_payload_sha256="a" * 64, blockhash=None, last_valid_height=None,
        nonce=1, backend="evm", bound_at="2026-09-29T09:00:01Z",
        evm_quote_asset_usd=2000.0)
    pending = db.pending_row(conn, pending_id)
    receipt = {"transactionHash": HASH, "blockHash": BLOCK, "blockNumber": "0x10",
               "status": "0x1", "from": owner,
               "logs": [_transfer(weth, owner, pool, 5 * 10**15, 0),
                        _transfer(mint, pool, owner, 5 * 10**18, 1)]}
    router = "0x66a9893cc07d91d95644aedd05d03f95e1dba8af"
    if network == "1":
        receipt["logs"] = ([_transfer(mint, pool, owner, 5 * 10**18, 1)] if native else
                           [_transfer(weth, owner, router, 5 * 10**15, 0),
                            _transfer(mint, pool, owner, 5 * 10**18, 1)])
    tx = {"hash": HASH, "from": owner, "blockHash": BLOCK, "blockNumber": "0x10",
          "to": router if network == "1" else pool,
          "value": hex(5 * 10**15) if native else "0x0"}
    block = {"hash": BLOCK, "number": "0x10", "timestamp": hex(1790672402)}
    recovered = recover_confirmed_ink_entry(
        conn, pending, receipt, tx, block, owner=owner, weth=weth,
        decimals_fn=lambda _mint: 18)
    assert recovered["status"] == "filled"
    assert db.pending_row(conn, pending_id)["status"] == "confirmed"
    assert db.open_qty(conn, recovered["position_id"]) == 5.0
    assert recover_confirmed_ink_entry(
        conn, pending, receipt, tx, block, owner=owner, weth=weth,
        decimals_fn=lambda _mint: 18) is None
    assert conn.execute("SELECT count(*) FROM memecoin_positions").fetchone()[0] == 1


def test_missing_ink_quote_basis_keeps_confirmed_buy_reserved():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    pending_id = db.reserve_pending(
        conn, mint="0x" + "3" * 40, source_user_id="source", strategy="alpha_b",
        notional=10.0, reserved_at="2026-09-29T09:00:00Z", network_id="57073")
    db.set_pending_broadcast(conn, pending_id, HASH, 1, "2026-09-29T09:00:01Z")
    assert recover_confirmed_ink_entry(
        conn, db.pending_row(conn, pending_id), {}, {}, {}, owner="0x" + "1" * 40,
        weth="0x4200000000000000000000000000000000000006",
        decimals_fn=lambda _mint: 18) is None
    assert db.pending_row(conn, pending_id)["status"] == "reserved"


def test_ink_quote_basis_is_immutable_with_signed_bytes_and_clears_on_failed_receipt():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    pending_id = db.reserve_pending(
        conn, mint="0x" + "3" * 40, source_user_id="source", strategy="alpha_b",
        notional=10.0, reserved_at="2026-09-29T09:00:00Z", network_id="57073")
    binding = dict(tx_identity=HASH, signed_payload_b64="0xraw",
                   signed_payload_sha256="a" * 64, blockhash=None,
                   last_valid_height=None, nonce=1, backend="evm",
                   bound_at="2026-09-29T09:00:01Z")
    assert db.bind_entry_submission(conn, pending_id, **binding, evm_quote_asset_usd=2000.0)
    assert not db.bind_entry_submission(conn, pending_id, **binding,
                                        evm_quote_asset_usd=3000.0)
    assert db.pending_row(conn, pending_id)["evm_quote_asset_usd"] == 2000.0
    assert db.reset_entry_submission_after_failed_receipt(conn, pending_id, HASH)
    assert db.pending_row(conn, pending_id)["evm_quote_asset_usd"] is None

    assert db.bind_entry_submission(conn, pending_id, **binding)
    assert not db.bind_entry_submission(conn, pending_id, **binding,
                                        evm_quote_asset_usd=2000.0)


@pytest.fixture
def failed_receipt_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    yield conn
    conn.close()


def _signed_recovery_pending(conn, network="4663"):
    pending_id = db.reserve_pending(
        conn, mint="0x" + "3" * 40, source_user_id="source", strategy="alpha_b",
        notional=10.0, reserved_at="2026-09-29T09:00:00Z", network_id=network,
        attempt_id="failed-receipt-attempt", origin_event_id="failed-receipt-origin")
    binding = dict(tx_identity=HASH, signed_payload_b64="0xraw",
                   signed_payload_sha256="a" * 64, blockhash="bound-blockhash",
                   last_valid_height=123, nonce=1, backend="evm",
                   bound_at="2026-09-29T09:00:01Z", evm_quote_asset_usd=2000.0)
    assert db.bind_entry_submission(conn, pending_id, **binding)
    return pending_id, binding


@pytest.mark.parametrize("network,reason", [
    ("4663", "rh_fast_reverted"), ("56", "bsc_fast_reverted"),
])
def test_terminal_failed_receipt_reset_releases_in_one_transition_and_prevents_rebind(
        failed_receipt_conn, network, reason):
    conn = failed_receipt_conn
    pending_id, binding = _signed_recovery_pending(conn, network)
    # Observe every row transition: there must never be an unbound live reservation
    # between clearing the failed signature and releasing its funded authority.
    conn.executescript("""
        CREATE TEMP TABLE receipt_reset_transitions(status TEXT, tx_sig TEXT, reason TEXT);
        CREATE TEMP TRIGGER observe_receipt_reset AFTER UPDATE ON memecoin_pending_entries
        BEGIN
            INSERT INTO receipt_reset_transitions VALUES
                (NEW.status, NEW.tx_sig, NEW.resolution_reason);
        END;
    """)
    assert db.reset_entry_submission_after_failed_receipt(
        conn, pending_id, HASH, release_reason=reason)
    row = db.pending_row(conn, pending_id)
    assert row["status"] == "failed" and row["resolution_reason"] == reason
    assert row["broadcast_at"] is None
    assert all(value is None for value in db.entry_submission_binding(conn, pending_id).values())
    assert row["attempt_id"] == "failed-receipt-attempt"
    assert row["origin_event_id"] == "failed-receipt-origin"
    assert [tuple(row) for row in conn.execute("SELECT * FROM receipt_reset_transitions")] == [
        ("failed", None, reason)]
    rebound = dict(binding, tx_identity="0x" + "c" * 64,
                   signed_payload_b64="0xnew", signed_payload_sha256="c" * 64)
    assert not db.bind_entry_submission(conn, pending_id, **rebound)
    assert db.pending_row(conn, pending_id) == row


def test_default_failed_receipt_reset_preserves_the_bounded_retry(failed_receipt_conn):
    conn = failed_receipt_conn
    pending_id, binding = _signed_recovery_pending(conn)
    assert db.reset_entry_submission_after_failed_receipt(conn, pending_id, HASH)
    row = db.pending_row(conn, pending_id)
    assert row["status"] == "reserved" and row["resolution_reason"] is None
    assert row["broadcast_at"] is None
    assert all(value is None for value in db.entry_submission_binding(conn, pending_id).values())
    rebound = dict(binding, tx_identity="0x" + "c" * 64,
                   signed_payload_b64="0xnew", signed_payload_sha256="c" * 64)
    assert db.bind_entry_submission(conn, pending_id, **rebound)
    assert db.pending_row(conn, pending_id)["tx_sig"] == rebound["tx_identity"]


@pytest.mark.parametrize("status,tx_identity", [
    ("reserved", "0x" + "c" * 64), ("failed", HASH), ("confirmed", HASH),
])
def test_terminal_failed_receipt_reset_requires_the_exact_live_binding(
        failed_receipt_conn, status, tx_identity):
    conn = failed_receipt_conn
    pending_id, _binding = _signed_recovery_pending(conn)
    conn.execute("UPDATE memecoin_pending_entries SET status=? WHERE id=?", (status, pending_id))
    conn.commit()
    before = db.pending_row(conn, pending_id)
    assert not db.reset_entry_submission_after_failed_receipt(
        conn, pending_id, tx_identity, release_reason="rh_fast_reverted")
    assert db.pending_row(conn, pending_id) == before


@pytest.mark.parametrize("network", ["57073", "1"])
def test_confirmed_swap_without_booking_keeps_exposure_even_if_balance_zero(network):
    from sniper.services.memecoin_real_runner import recover_stale_reservations
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    pending_id = db.reserve_pending(
        conn, mint="0x" + "3" * 40, source_user_id="source", strategy="alpha_b",
        notional=10.0, reserved_at="2026-09-29T09:00:00Z", network_id=network)
    assert db.bind_entry_submission(
        conn, pending_id, tx_identity=HASH, signed_payload_b64="0xraw",
        signed_payload_sha256="a" * 64, blockhash=None, last_valid_height=None,
        nonce=1, backend="evm", bound_at="2026-09-29T09:00:01Z",
        evm_quote_asset_usd=2000.0)
    actions = recover_stale_reservations(
        conn, confirm_for=lambda *_a: "confirmed", now_iso="2026-09-29T09:20:00Z",
        confirmed_entry_for=lambda *_a: None,
        confirmed_nonce_for=lambda _chain: 2,
        token_balance_for=lambda *_a: 0.0)
    assert actions == [(pending_id, "orphaned_confirmed")]
    assert db.pending_row(conn, pending_id)["status"] == "reserved"


def test_ink_provisional_receipt_cannot_finalize_fill(monkeypatch):
    calls = []
    def rpc(_url, method, params, http=None):
        calls.append(method)
        return {"result": {"transactionHash": HASH, "blockHash": "0x" + "0" * 64,
                           "blockNumber": "0x12", "status": "0x1"}}
    monkeypatch.setattr(seams, "_rpc", rpc)
    result = seams.eth_confirm(HASH, "rpc", max_attempts=1, sleep=lambda _s: None,
                               require_canonical=True)
    assert result == "unknown"
    assert calls == ["eth_getTransactionReceipt"]


def test_ink_receipt_requires_matching_canonical_header(monkeypatch):
    receipt = {"transactionHash": HASH, "blockHash": BLOCK,
               "blockNumber": "0x12", "status": "0x1", "logs": []}
    block_hash = ["0x" + "c" * 64]
    def rpc(_url, method, _params, http=None):
        return {"result": receipt if method == "eth_getTransactionReceipt"
                else {"hash": block_hash[0], "number": "0x12"}}
    monkeypatch.setattr(seams, "_rpc", rpc)
    assert seams.eth_receipt_state(HASH, "rpc", require_canonical=True) == "unknown"
    block_hash[0] = BLOCK
    assert seams.eth_receipt_state(HASH, "rpc", require_canonical=True) == "confirmed"
    receipt["logs"] = [{"transactionHash": HASH, "blockHash": BLOCK}]
    assert seams.eth_receipt_state(HASH, "rpc", require_canonical=True) == "unknown"


def test_the_recovery_finds_the_attempts_fill_by_its_index_and_books_nothing():
    """The executor booked this swap (a fill for the attempt exists) but the reservation is still
    'reserved'. The recovery must see the fill through the attempt index, not an 18 ms scan by
    hash, and must not book a second position."""
    import pytest
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    owner, pool = "0x" + "1" * 40, "0x" + "2" * 40
    mint, weth = "0x" + "3" * 40, "0x4200000000000000000000000000000000000006"
    pending_id = db.reserve_pending(
        conn, mint=mint, source_user_id="source", strategy="alpha_b", notional=10.0,
        reserved_at="2026-09-29T09:00:00Z", network_id="57073", attempt_id="ink-attempt")
    assert db.bind_entry_submission(
        conn, pending_id, tx_identity=HASH, signed_payload_b64="0xraw",
        signed_payload_sha256="a" * 64, blockhash=None, last_valid_height=None,
        nonce=1, backend="evm", bound_at="2026-09-29T09:00:01Z", evm_quote_asset_usd=2000.0)
    position = db.open_position(
        conn, source_user_id="other", source_handle="(real)", mint=mint, ticker="",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=None,
        their_size_usd=None, opened_at="2026-09-29T09:00:02Z", strategy="alpha_b",
        execution_mode="real", network_id="57073", attempt_id="ink-attempt")
    db.insert_fill(conn, position_id=position, kind="entry", price=1.0, qty=5.0, notional=10.0,
                   fill_source="chain", fraction=1.0, created_at="2026-09-29T09:00:02Z",
                   tx_sig=HASH, attempt_id="ink-attempt")
    receipt = {"transactionHash": HASH, "blockHash": BLOCK, "blockNumber": "0x10",
               "status": "0x1", "from": owner,
               "logs": [_transfer(weth, owner, pool, 5 * 10**15, 0),
                        _transfer(mint, pool, owner, 5 * 10**18, 1)]}
    tx = {"hash": HASH, "from": owner, "blockHash": BLOCK, "blockNumber": "0x10",
          "to": pool, "value": "0x0"}
    block = {"hash": BLOCK, "number": "0x10", "timestamp": hex(1790672402)}
    statements = []
    conn.set_trace_callback(statements.append)
    with pytest.raises(RuntimeError, match="already has a fill"):
        recover_confirmed_ink_entry(conn, db.pending_row(conn, pending_id), receipt, tx, block,
                                    owner=owner, weth=weth, decimals_fn=lambda _mint: 18)
    conn.set_trace_callback(None)
    assert conn.execute("SELECT count(*) FROM memecoin_positions").fetchone()[0] == 1
    assert not any("tx_sig=?" in sql or "tx_sig='" in sql for sql in statements
                   if "memecoin_fills" in sql), "the duplicate check scanned fills by hash"


@pytest.mark.parametrize('network', ['1', '57073'])
def test_missing_receipt_and_advanced_nonce_never_discard_receipt_backed_buy(network):
    from sniper.services.memecoin_real_runner import recover_stale_reservations
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    pid = db.reserve_pending(conn, mint='0x' + '3' * 40, source_user_id='source',
        strategy='alpha_b', notional=10, reserved_at='2026-09-29T09:00:00Z', network_id=network)
    db.bind_entry_submission(conn, pid, tx_identity=HASH, signed_payload_b64='raw',
        signed_payload_sha256='a' * 64, blockhash=None, last_valid_height=None,
        nonce=1, backend='evm', bound_at='2026-09-29T09:00:01Z', evm_quote_asset_usd=2000)
    recover_stale_reservations(conn, confirm_for=lambda *_: 'not_found',
        confirm_proof_for=lambda *_: 'not_found', confirmed_nonce_for=lambda *_: 2,
        token_balance_for=lambda *_: 10, now_iso='2026-09-29T09:20:00Z')
    assert db.pending_row(conn, pid)['status'] == 'reserved'
