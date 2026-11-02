from __future__ import annotations

import sqlite3
import json

import pytest

from sniper.research.memecoin_opportunity.paper_adapter import (
    ADAPTER_VERSION,
    FILL_MODEL_VERSION,
    UNFILLED_REASONS,
    PaperFillRequest,
    append_paper_fill_receipt,
    append_paper_fill_request,
    expired_requests,
    paper_request_id,
    pending_requests,
    sweep_expired_requests,
)
from sniper.research.memecoin_opportunity.store import ImmutableConflict, connect


def _request(**overrides) -> PaperFillRequest:
    values = {
        "request_id": "r1",
        "alert_id": "a1",
        "launch_id": "l1",
        "network_id": "solana",
        "venue_id": "pumpfun_solana",
        "token_id": "t1",
        "creator_id": "c1",
        "launch_mode": "standard",
        "state_fingerprint": "sf",
        "rule_version": "pump-confirmation-truth-v5",
        "evaluator_version": "alert-outcome-v4",
        "adapter_version": ADAPTER_VERSION,
        "fill_model_version": FILL_MODEL_VERSION,
        "clip_usd_micros": 25_000_000,
        "decision_at": "2026-08-05T00:00:00Z",
        "decision_age_ms": 20_000,
        "intent_at": "2026-08-05T00:00:00Z",
        "expires_at": "2026-08-05T00:00:30Z",
        "local_qty_atomic": 1_050,
    }
    values.update(overrides)
    return PaperFillRequest(**values)


def _conn(tmp_path):
    return connect(tmp_path / "opportunity.sqlite")


def _economics(**overrides):
    values = {
        "expected_out_atomic": 1_000,
        "floor_out_atomic": 940,
        "quote_provider": "jupiter",
        "quote_in_asset": "USDC",
        "quote_in_atomic": 25_000_000,
        "quote_in_decimals": 6,
        "quote_out_asset": "mint-1",
        "quote_out_decimals": 6,
        "slippage_bps": 300,
        "route_binding": {"amm": "Amm1"},
        "quote_payload": {"outAmount": 1_000, "otherAmountThreshold": 940},
        "quote_started_at": "2026-08-05T00:00:01Z",
        "quote_completed_at": "2026-08-05T00:00:02Z",
    }
    values.update(overrides)
    return values


def test_request_id_is_deterministic_and_version_scoped():
    kwargs = {
        "alert_id": "a1",
        "adapter_version": ADAPTER_VERSION,
        "fill_model_version": FILL_MODEL_VERSION,
        "clip_usd_micros": 25_000_000,
    }
    assert paper_request_id(**kwargs) == paper_request_id(**kwargs)
    assert paper_request_id(**{**kwargs, "adapter_version": "radar-paper-adapter-v2"}) != paper_request_id(**kwargs)
    assert paper_request_id(**{**kwargs, "fill_model_version": "modeled"}) != paper_request_id(**kwargs)
    assert paper_request_id(**{**kwargs, "clip_usd_micros": 50_000_000}) != paper_request_id(**kwargs)


def test_repeated_request_append_is_a_no_op(tmp_path):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())
    append_paper_fill_request(conn, _request())
    assert conn.execute("SELECT COUNT(*) FROM op_paper_fill_requests").fetchone()[0] == 1


def test_exact_atomic_quantities_can_exceed_sqlite_integer_range(tmp_path):
    conn = _conn(tmp_path)
    quantity = 2**80
    append_paper_fill_request(conn, _request(local_qty_atomic=quantity))
    append_paper_fill_receipt(
        conn,
        request_id="r1",
        status="filled",
        now_iso="2026-08-05T00:00:03Z",
        quote_started_at="2026-08-05T00:00:01Z",
        quote_completed_at="2026-08-05T00:00:02Z",
        economics=_economics(
            expected_out_atomic=quantity,
            floor_out_atomic=quantity - 1,
        ),
    )

    request = conn.execute(
        "SELECT local_qty_atomic FROM op_paper_fill_requests"
    ).fetchone()
    receipt = conn.execute(
        "SELECT expected_out_atomic,floor_out_atomic FROM op_paper_fills"
    ).fetchone()
    assert int(request["local_qty_atomic"]) == quantity
    assert int(receipt["expected_out_atomic"]) == quantity
    assert int(receipt["floor_out_atomic"]) == quantity - 1


def test_repeated_request_with_different_content_is_an_immutable_conflict(tmp_path):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())

    with pytest.raises(ImmutableConflict, match="op_paper_fill_requests"):
        append_paper_fill_request(conn, _request(state_fingerprint="different"))


def test_pending_and_expired_are_derived_not_stored(tmp_path):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())
    assert [row["request_id"] for row in pending_requests(conn)] == ["r1"]
    assert expired_requests(conn, now_iso="2026-08-05T00:00:29Z") == ()
    assert [row["request_id"] for row in expired_requests(conn, now_iso="2026-08-05T00:00:31Z")] == ["r1"]

    append_paper_fill_receipt(
        conn, request_id="r1", status="entry_unfilled",
        unfilled_reason="capacity_expired", now_iso="2026-08-05T00:00:31Z",
    )
    assert pending_requests(conn) == ()
    assert expired_requests(conn, now_iso="2026-08-05T00:01:00Z") == ()


def test_receipt_rejects_unknown_unfilled_reason(tmp_path):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())
    with pytest.raises(ValueError, match="unfilled reason"):
        append_paper_fill_receipt(
            conn, request_id="r1", status="entry_unfilled",
            unfilled_reason="because", now_iso="2026-08-05T00:00:31Z",
        )


def test_filled_receipt_requires_both_expected_and_floor(tmp_path):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())
    with pytest.raises(ValueError, match="floor_out_atomic"):
        append_paper_fill_receipt(
            conn, request_id="r1", status="filled", now_iso="2026-08-05T00:00:02Z",
            economics=_economics(floor_out_atomic=None),
        )
    with pytest.raises(ValueError, match="expected_out_atomic"):
        append_paper_fill_receipt(
            conn, request_id="r1", status="filled", now_iso="2026-08-05T00:00:02Z",
            economics=_economics(expected_out_atomic=0),
        )


def test_filled_receipt_rejects_a_floor_above_expected(tmp_path):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())

    with pytest.raises(ValueError, match="cannot exceed expected"):
        append_paper_fill_receipt(
            conn, request_id="r1", status="filled", now_iso="2026-08-05T00:00:03Z",
            quote_started_at="2026-08-05T00:00:01Z",
            quote_completed_at="2026-08-05T00:00:02Z",
            economics=_economics(floor_out_atomic=1_001),
        )


@pytest.mark.parametrize("missing", [
    "quote_provider", "quote_in_asset", "quote_in_atomic", "quote_in_decimals",
    "quote_out_asset", "quote_out_decimals", "route_binding", "quote_payload",
])
def test_filled_receipt_requires_complete_executable_quote_evidence(tmp_path, missing):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())
    economics = _economics()
    economics[missing] = None

    with pytest.raises(ValueError, match=missing):
        append_paper_fill_receipt(
            conn, request_id="r1", status="filled", now_iso="2026-08-05T00:00:03Z",
            quote_started_at="2026-08-05T00:00:01Z",
            quote_completed_at="2026-08-05T00:00:02Z",
            economics=economics,
        )


def test_filled_receipt_cannot_carry_a_reason(tmp_path):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())
    with pytest.raises(ValueError, match="cannot carry"):
        append_paper_fill_receipt(
            conn, request_id="r1", status="filled", unfilled_reason="no_route",
            now_iso="2026-08-05T00:00:02Z",
            economics=_economics(),
        )


def test_receipt_records_both_clocks_separately(tmp_path):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())
    append_paper_fill_receipt(
        conn, request_id="r1", status="filled", now_iso="2026-08-05T00:00:03Z",
        quote_started_at="2026-08-05T00:00:01Z",
        quote_completed_at="2026-08-05T00:00:02Z",
        economics=_economics(),
    )
    row = conn.execute("SELECT * FROM op_paper_fills WHERE request_id='r1'").fetchone()
    assert row["queue_delay_ms"] == 1_000       # intent -> quote start
    assert row["quote_latency_ms"] == 1_000     # quote start -> quote complete
    assert row["receipt_latency_ms"] == 3_000   # decision -> receipt, end to end
    # v5's own modeled decision clock is untouched on the request row.
    assert conn.execute(
        "SELECT decision_age_ms FROM op_paper_fill_requests WHERE request_id='r1'"
    ).fetchone()[0] == 20_000


def test_receipt_requires_an_existing_request(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="no paper fill request"):
        append_paper_fill_receipt(
            conn, request_id="ghost", status="entry_unfilled",
            unfilled_reason="no_route", now_iso="2026-08-05T00:00:31Z",
        )


def test_expiry_sweep_appends_exactly_one_terminal_receipt(tmp_path):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())
    assert sweep_expired_requests(conn, now_iso="2026-08-05T00:00:31Z") == 1
    assert sweep_expired_requests(conn, now_iso="2026-08-05T00:00:32Z") == 0
    rows = conn.execute(
        "SELECT status,unfilled_reason FROM op_paper_fills WHERE request_id='r1'"
    ).fetchall()
    assert len(rows) == 1
    assert (rows[0]["status"], rows[0]["unfilled_reason"]) == ("entry_unfilled", "capacity_expired")


def test_second_receipt_for_one_request_is_impossible(tmp_path):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())
    append_paper_fill_receipt(
        conn, request_id="r1", status="entry_unfilled", unfilled_reason="no_route",
        now_iso="2026-08-05T00:00:05Z",
    )
    with pytest.raises(sqlite3.IntegrityError):
        append_paper_fill_receipt(
            conn, request_id="r1", status="filled", now_iso="2026-08-05T00:00:06Z",
            quote_started_at="2026-08-05T00:00:01Z",
            quote_completed_at="2026-08-05T00:00:02Z",
            economics=_economics(),
        )


def test_launch_mode_is_constrained():
    with pytest.raises(ValueError, match="launch_mode"):
        _request(launch_mode="premium")


def test_every_reason_is_accepted_by_the_ledger(tmp_path):
    conn = _conn(tmp_path)
    for index, reason in enumerate(sorted(UNFILLED_REASONS)):
        request_id = f"r{index}"
        append_paper_fill_request(conn, _request(request_id=request_id))
        append_paper_fill_receipt(
            conn, request_id=request_id, status="entry_unfilled",
            unfilled_reason=reason, now_iso="2026-08-05T00:00:31Z",
        )
    assert conn.execute("SELECT COUNT(*) FROM op_paper_fills").fetchone()[0] == len(UNFILLED_REASONS)


# --- driving pending requests to terminal receipts ----------------------------

from sniper.research.memecoin_opportunity.paper_adapter import (  # noqa: E402
    QuoteDeferred,
    QuoteRejected,
    drive_paper_adapter,
)


def test_drive_scores_the_floor_and_keeps_the_expected_amount(tmp_path):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())
    counters = drive_paper_adapter(
        conn, quote_fn=lambda row: _economics(), now_iso="2026-08-05T00:00:02Z",
    )
    assert counters == {"filled": 1}
    row = conn.execute("SELECT * FROM op_paper_fills WHERE request_id='r1'").fetchone()
    assert row["status"] == "filled"
    assert row["floor_out_atomic"] == "940"
    assert row["expected_out_atomic"] == "1000"
    assert row["queue_delay_ms"] == 1_000
    assert row["quote_latency_ms"] == 1_000
    assert row["quote_fingerprint"]


def test_drive_maps_typed_rejections_to_reasons(tmp_path):
    conn = _conn(tmp_path)
    for index, reason in enumerate(["no_route", "quote_unavailable", "malformed_quote"]):
        append_paper_fill_request(conn, _request(request_id=f"r{index}"))

    def _reject(row):
        raise QuoteRejected(
            {"r0": "no_route", "r1": "quote_unavailable", "r2": "malformed_quote"}[
                str(row["request_id"])
            ]
        )

    counters = drive_paper_adapter(
        conn, quote_fn=_reject, now_iso="2026-08-05T00:00:02Z",
    )
    assert counters == {"no_route": 1, "quote_unavailable": 1, "malformed_quote": 1}
    assert conn.execute(
        "SELECT COUNT(*) FROM op_paper_fills WHERE status='entry_unfilled'"
    ).fetchone()[0] == 3


def test_drive_leaves_a_deferred_probe_request_pending(tmp_path):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())

    def _defer(_row):
        raise QuoteDeferred

    assert drive_paper_adapter(
        conn, quote_fn=_defer, now_iso="2026-08-05T00:00:02Z",
    ) == {}
    assert [row["request_id"] for row in pending_requests(conn)] == ["r1"]


def test_drive_quotes_without_any_downstream_consumer(tmp_path):
    """Radar's receipt is its own entry-realism evidence, not a handoff to V6.

    The adapter used to refuse to quote unless the V6 consumer heartbeat was fresh, so
    stopping V6 silently ended Radar's measurement. No heartbeat exists in this test.
    """
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())
    calls = []

    counters = drive_paper_adapter(
        conn, quote_fn=lambda row: calls.append(row) or _economics(),
        now_iso="2026-08-05T00:00:02Z",
    )
    assert counters == {"filled": 1}
    assert len(calls) == 1
    row = conn.execute(
        "SELECT status,unfilled_reason FROM op_paper_fills WHERE request_id='r1'"
    ).fetchone()
    assert (row["status"], row["unfilled_reason"]) == ("filled", None)


def test_drive_expires_a_request_past_its_deadline_without_quoting(tmp_path):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())
    calls = []
    counters = drive_paper_adapter(
        conn, quote_fn=lambda row: calls.append(row) or _economics(),
        now_iso="2026-08-05T00:00:31Z",
    )
    assert counters == {"capacity_expired": 1}
    assert calls == []


def test_drive_is_idempotent_across_passes(tmp_path):
    conn = _conn(tmp_path)
    append_paper_fill_request(conn, _request())
    drive_paper_adapter(conn, quote_fn=lambda row: _economics(),
                        now_iso="2026-08-05T00:00:02Z")
    assert drive_paper_adapter(conn, quote_fn=lambda row: _economics(),
                               now_iso="2026-08-05T00:00:03Z") == {}
    assert conn.execute("SELECT COUNT(*) FROM op_paper_fills").fetchone()[0] == 1
