"""Source facts stay bound to the exact origin, wallet, coin and chain."""
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_entry_view import entry_view

NOW = "2026-09-26T21:12:45+00:00"
IDENTITY = dict(network_id="solana", mint="MINT", source_user_id="wallet")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO onchain_wallet_detections "
              "(signature,source_user_id,address,network_id,mint,source_size_usd,detected_at,created_at) "
              "VALUES ('sig','wallet','address','solana','MINT',250,?,?)", (NOW, NOW))
    c.commit()
    m.start_entry_attempt(c, attempt_id="attempt", origin_event_id="onchain:sig",
                          decision_started_at=NOW, source_handle="Zach", **IDENTITY)
    yield c
    c.close()


def test_exact_onchain_source_reaches_view_and_position_resolver(conn):
    assert m.resolve_source_size_usd(conn, "onchain:sig", **IDENTITY) == 250
    view = entry_view(conn, attempt_id="attempt")
    assert view["source_size_usd"] == 250
    assert view["event_id"] is None  # A detection ID is not a feed-event primary key.
    assert view["source_timestamp_trust"] == "observed_detection"
    from sniper.services.memecoin_telegram import render_entry_notification
    message = render_entry_notification(view)
    assert "unknown size" not in message
    assert "detected" in message


@pytest.mark.parametrize("changed", [dict(network_id="8453"), dict(mint="mint"),
                                     dict(source_user_id="other")])
def test_wrong_identity_never_borrows_known_size(conn, changed):
    assert m.resolve_source_size_usd(conn, "onchain:sig", **(IDENTITY | changed)) is None


@pytest.mark.parametrize("origin", ["onchain:other", "feed-uuid", "onchain:sig-extra"])
def test_nearby_or_similar_origin_is_not_same_buy(conn, origin):
    assert m.resolve_source_size_usd(conn, origin, **IDENTITY) is None


@pytest.mark.parametrize("size,expected", [(0, 0), (None, None), (-1, None),
                                          (float("inf"), None), ("bad", None)])
def test_zero_unknown_and_invalid_remain_distinct(conn, size, expected):
    conn.execute("UPDATE onchain_wallet_detections SET source_size_usd=?", (size,))
    assert m.resolve_source_size_usd(conn, "onchain:sig", **IDENTITY) == expected
    conn.commit()
    assert entry_view(conn, attempt_id="attempt")["source_size_usd"] == expected


def test_slot_source_is_available_before_trace_finishes(conn):
    facts = dict(IDENTITY, signature="slot-sig", their_size_usd=354.89,
                 valuation_basis="decoded_swap_sol", source_slot=450793677)
    m.start_entry_attempt(conn, attempt_id="slot", origin_event_id="slotlane:slot-sig",
                          decision_started_at=NOW, trace_json=json.dumps({"source_evidence": facts}),
                          **IDENTITY)
    assert m.resolve_source_size_usd(conn, "slotlane:slot-sig", **IDENTITY) == 354.89
    assert entry_view(conn, attempt_id="slot")["source_size_usd"] == 354.89
    facts["signature"] = "different"
    conn.execute("UPDATE memecoin_entry_attempts SET trace_json=? WHERE attempt_id='slot'",
                 (json.dumps({"source_evidence": facts}),))
    assert m.resolve_source_size_usd(conn, "slotlane:slot-sig", **IDENTITY) is None


def test_missing_identity_does_not_authorize_detection_lookup(conn):
    assert m.resolve_source_size_usd(conn, "onchain:sig") is None


@pytest.mark.parametrize("bind", [False, True])
def test_slot_signed_identity_is_linked_in_same_commit(conn, bind):
    conn.execute("UPDATE memecoin_entry_attempts SET origin_event_id='slotlane:sig' WHERE attempt_id='attempt'")
    conn.commit()
    pid = m.reserve_pending(conn, mint="MINT", source_user_id="wallet", strategy="test",
                            notional=25, reserved_at=NOW, network_id="solana",
                            origin_event_id="slotlane:sig", attempt_id="attempt", signal_source="slot_lane")
    if bind:
        assert m.bind_entry_submission(conn, pid, tx_identity="signed", signed_payload_b64="bytes",
                                       signed_payload_sha256="hash", blockhash="block", last_valid_height=1,
                                       nonce=None, backend="rpc", bound_at=NOW)
    else:
        m.set_pending_sig(conn, pid, "signed", NOW)
    row = conn.execute("SELECT tx_sig FROM memecoin_entry_attempts WHERE attempt_id='attempt'").fetchone()
    assert row[0] == "signed"


def test_view_preserves_decision_time_reservation_blocker(conn):
    blocker = dict(kind="reservation", reservation_id=4014, attempt_id="j7-attempt")
    conn.execute("UPDATE memecoin_entry_attempts SET terminal_status='refused', "
                 "terminal_reason='one position per coin', completed_at=?, gate_inputs_json=? WHERE attempt_id='attempt'",
                 (NOW, json.dumps({"duplicate_blocker": blocker})))
    conn.commit()
    row = entry_view(conn, attempt_id="attempt")
    assert row["duplicate_blocker"] == blocker
    assert "Another entry attempt was in progress" in row["reason"]


def test_exact_guard_failure_renders_slots_fee_and_no_buy(conn):
    LIGHTHOUSE_PROGRAM = "L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95"   # historical guard rows
    guard = dict(name="slot_guard_result", reason="slot_window_expired", source_slot=450793677,
                 max_slot=450793678, landing_slot=450793680, instruction_index=3,
                 failed_program=LIGHTHOUSE_PROGRAM, program_error="AssertionFailed",
                 fee_lamports=85204, no_tokens_bought=True)
    conn.execute("UPDATE memecoin_entry_attempts SET terminal_status='failed', "
                 "terminal_reason='slot_window_expired', completed_at=?, trace_json=? WHERE attempt_id='attempt'",
                 (NOW, json.dumps({"substage_events": [guard]})))
    conn.commit()
    pid = m.reserve_pending(conn, mint="MINT", source_user_id="wallet", strategy="test",
                            notional=25, reserved_at=NOW, network_id="solana",
                            origin_event_id="onchain:sig", attempt_id="attempt")
    m.set_pending_sig(conn, pid, "signed", NOW)
    m.release_pending(conn, pid, "slot_window_expired")
    row = entry_view(conn, attempt_id="attempt")
    assert row["status"] == "transaction_failed"
    assert row["slot_guard"]["landing_slot"] == 450793680
    assert all(s in row["reason"] for s in ("Slot window expired", "450793677", "450793678",
                                           "450793680", "0.000085204 SOL", "no tokens bought"))


def test_pump_6042_renders_price_movement_and_no_purchase(conn):
    failure = dict(name="slot_pump_slippage", reason="price_moved_beyond_limit",
                   available_tokens_atomic=19_494_581_726_667,
                   min_tokens_atomic=21_600_089_757_031,
                   fee_lamports=85_204, landing_slot=450_797_211, no_tokens_bought=True)
    conn.execute("UPDATE memecoin_entry_attempts SET terminal_status='failed', "
                 "terminal_reason='chain_error:BuySlippageBelowMinTokensOut', "
                 "completed_at=?, trace_json=? WHERE attempt_id='attempt'",
                 (NOW, json.dumps({"substage_events": [failure]})))
    conn.commit()
    pid = m.reserve_pending(conn, mint="MINT", source_user_id="wallet", strategy="test",
                            notional=25, reserved_at=NOW, network_id="solana",
                            origin_event_id="onchain:sig", attempt_id="attempt")
    m.set_pending_sig(conn, pid, "signed", NOW)
    m.release_pending(conn, pid, "chain_error:BuySlippageBelowMinTokensOut")
    row = entry_view(conn, attempt_id="attempt")
    assert row["status"] == "transaction_failed"
    assert all(s in row["reason"] for s in ("Price moved beyond limit", "21.60M", "19.49M",
                                           "no purchase", "0.000085204 SOL"))
    from sniper.services.memecoin_telegram import render_entry_notification
    assert "Why: Price moved beyond limit: minimum 21.60M tokens, available 19.49M; no purchase" \
        in render_entry_notification(row)
