import sqlite3

from sniper.execution.lifi_executor import (
    LifiLifecycle,
    approve_exact_and_wait,
    finalize_lifi_failed_entry,
    lifi_entry_blocked,
    revoke_and_wait,
)
from sniper.persistence import db_memecoin as m


NOW = "2026-08-30T20:00:00+00:00"
CUSTODY = "0x1111111111111111111111111111111111111111"
TOKEN = "0x2222222222222222222222222222222222222222"
SPENDER = "0x3333333333333333333333333333333333333333"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _lifecycle(conn, *, send_result="0xaaa", receipt_status="0x1", allowance=1000):
    pending_id = m.reserve_pending(
        conn, mint=TOKEN, source_user_id="wallet", strategy="control", notional=10,
        reserved_at=NOW, network_id="4663",
    )
    lifecycle_id = m.start_lifi_lifecycle(
        conn, pending_id, chain_id="4663", custody_wallet=CUSTODY, token=TOKEN,
        release_sha="a" * 40, manifest_hash="b" * 64, created_at=NOW,
    )
    events = []

    def sign(tx):
        events.append(("sign", tx))
        return "0xsigned", "0xaaa"

    def send(raw):
        events.append(("send", raw))
        return send_result

    def receipt(tx_hash):
        events.append(("receipt", tx_hash))
        if receipt_status is None:
            return None
        return {
            "transactionHash": tx_hash, "status": receipt_status,
            "blockNumber": "0x2a",
        }

    state = {"allowance": allowance}
    return LifiLifecycle(
        conn=conn,
        lifecycle_id=lifecycle_id,
        custody=CUSTODY,
        chain_id="4663",
        nonce_fn=lambda: 7,
        gas_price_fn=lambda: 2,
        sign_fn=sign,
        send_fn=send,
        receipt_fn=receipt,
        allowance_fn=lambda _token, _owner, _spender: state["allowance"],
        now_fn=lambda: NOW,
    ), pending_id, events, state


def test_exact_approval_persists_identity_before_send_and_waits_for_receipt():
    conn = _conn()
    lifecycle, _pending_id, events, _state = _lifecycle(conn)

    result = approve_exact_and_wait(lifecycle, TOKEN, SPENDER, 1000)

    assert result.status == "confirmed"
    assert result.block_number == 42
    assert [name for name, _value in events] == ["sign", "send", "receipt"]
    signed = conn.execute(
        "SELECT * FROM memecoin_lifi_authority_events "
        "WHERE lifecycle_id=? AND state='approval_signed'",
        (lifecycle.lifecycle_id,),
    ).fetchone()
    assert signed["nonce"] == "7"
    assert signed["token"] == TOKEN
    assert signed["spender"] == SPENDER
    assert signed["amount_atomic"] == "1000"
    assert signed["tx_hash"] == "0xaaa"
    assert signed["signed_payload_sha256"]
    tx = events[0][1]
    assert tx["to"] == TOKEN
    assert tx["data"].startswith("0x095ea7b3" + "0" * 24 + SPENDER[2:])
    assert tx["data"].endswith(format(1000, "064x"))
    latest = m.latest_lifi_lifecycle(conn, lifecycle.lifecycle_id)
    assert latest["state"] == "approval_confirmed"
    assert latest["block_number"] == "42"


def test_ambiguous_approval_stays_unresolved_and_never_revokes():
    conn = _conn()
    lifecycle, _pending_id, events, _state = _lifecycle(
        conn, send_result=None, receipt_status=None,
    )

    result = approve_exact_and_wait(lifecycle, TOKEN, SPENDER, 1000)

    assert result.status == "submitted_unknown"
    assert [name for name, _value in events] == ["sign", "send"]
    assert m.latest_lifi_lifecycle(conn, lifecycle.lifecycle_id)["state"] \
        == "approval_submitted_unknown"
    assert lifi_entry_blocked(conn, "4663", CUSTODY)
    assert revoke_and_wait(lifecycle, TOKEN, SPENDER).status == "blocked_unknown_submission"

    assert m.append_lifi_event(
        conn, lifecycle.lifecycle_id, expected_state="approval_submitted_unknown",
        new_state="revoke_submitted_unknown", event_type="recovery_probe", created_at=NOW,
    )
    assert revoke_and_wait(lifecycle, TOKEN, SPENDER).status == "blocked_unknown_submission"


def test_revoke_is_durable_and_requires_confirmed_zero_allowance():
    conn = _conn()
    lifecycle, _pending_id, events, state = _lifecycle(conn)
    assert approve_exact_and_wait(lifecycle, TOKEN, SPENDER, 1000).status == "confirmed"
    state["allowance"] = 0

    result = revoke_and_wait(lifecycle, TOKEN, SPENDER)

    assert result.status == "allowance_zero"
    revoke = conn.execute(
        "SELECT * FROM memecoin_lifi_authority_events "
        "WHERE lifecycle_id=? AND state='revoke_signed'",
        (lifecycle.lifecycle_id,),
    ).fetchone()
    assert revoke["amount_atomic"] == "0"
    assert events[-3][1]["data"].endswith("0" * 64)
    assert not lifi_entry_blocked(conn, "4663", CUSTODY)


def test_failed_entry_releases_only_after_confirmed_zero_allowance():
    conn = _conn()
    lifecycle, pending_id, _events, state = _lifecycle(conn)
    assert approve_exact_and_wait(lifecycle, TOKEN, SPENDER, 1000).status == "confirmed"

    assert not finalize_lifi_failed_entry(lifecycle, pending_id, "failed_check")
    assert m.pending_row(conn, pending_id)["status"] == "reserved"

    state["allowance"] = 0
    assert revoke_and_wait(lifecycle, TOKEN, SPENDER).status == "allowance_zero"
    assert finalize_lifi_failed_entry(lifecycle, pending_id, "failed_check")
    assert m.pending_row(conn, pending_id)["status"] == "failed"
    assert m.latest_lifi_lifecycle(conn, lifecycle.lifecycle_id)["state"] == "entry_failed"


def test_failed_cas_sends_no_bytes():
    conn = _conn()
    lifecycle, _pending_id, events, _state = _lifecycle(conn)
    assert m.append_lifi_event(
        conn, lifecycle.lifecycle_id, expected_state="reserved", new_state="invalid",
        event_type="invalid", created_at=NOW,
    )

    result = approve_exact_and_wait(lifecycle, TOKEN, SPENDER, 1000, expected_state="reserved")

    assert result.status == "cas_conflict"
    assert [name for name, _value in events] == ["sign"]


def test_reverted_approval_records_the_observed_zero_allowance():
    conn = _conn()
    lifecycle, _pending_id, _events, _state = _lifecycle(
        conn, receipt_status="0x0", allowance=0,
    )

    result = approve_exact_and_wait(lifecycle, TOKEN, SPENDER, 1000)

    assert result.status == "reverted"
    assert result.allowance_atomic == 0
    latest = m.latest_lifi_lifecycle(conn, lifecycle.lifecycle_id)
    assert latest["state"] == "approval_reverted"
    assert latest["allowance_atomic"] == "0"


def test_direct_entry_authority_is_not_part_of_lifi_block_state():
    conn = _conn()
    lifecycle, _pending_id, _events, _state = _lifecycle(
        conn, send_result=None, receipt_status=None,
    )
    approve_exact_and_wait(lifecycle, TOKEN, SPENDER, 1000)

    assert lifi_entry_blocked(conn, "4663", CUSTODY)
    assert not hasattr(lifecycle, "direct_entry_blocked")


def test_open_lifi_position_uses_a_slot_but_does_not_block_another_entry():
    conn = _conn()
    lifecycle, _pending_id, _events, _state = _lifecycle(conn)
    position_id = m.open_position(
        conn, source_user_id="wallet", source_handle="wallet", mint=TOKEN, ticker="TKN",
        entry_price=1, entry_notional=10, entry_fill_source="chain", entry_lag_s=1,
        their_size_usd=10, opened_at=NOW, strategy="control", execution_mode="real",
        network_id="4663", lifi_dependent=True,
    )
    assert m.append_lifi_event(
        conn, lifecycle.lifecycle_id, expected_state="reserved", new_state="position_open",
        event_type="position_linked", position_id=position_id, created_at=NOW,
    )

    assert m.lifi_slots_used(conn, "4663", CUSTODY) == 1
    assert not lifi_entry_blocked(conn, "4663", CUSTODY)
